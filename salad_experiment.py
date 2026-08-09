#!/usr/bin/env python3
"""Run and record a bounded SaladCloud mining experiment.

The script is intentionally dependency-free so it can run on a GitHub-hosted
runner. It restores the container group's initial replica count, priority and
running/stopped state even when sampling fails or the process is interrupted.
"""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


STOP_REQUESTED = False


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url, method="GET", headers=None, body=None, timeout=30):
    # Salad's Cloudflare policy rejects Python urllib's TLS/HTTP fingerprint on
    # GitHub-hosted runners (Error 1010). curl is preinstalled on those runners
    # and is also available in the local development environments we support.
    command = [
        "curl", "--fail-with-body", "--silent", "--show-error",
        "--max-time", str(timeout), "--request", method,
        "--header", "Accept: application/json",
        "--user-agent", "btc-miner-experiment/1.0",
    ]
    for key, value in (headers or {}).items():
        command.extend(("--header", f"{key}: {value}"))
    if body is not None:
        command.extend((
            "--header", "Content-Type: application/merge-patch+json",
            "--data-binary", json.dumps(body, separators=(",", ":")),
        ))
    command.append(url)
    response = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 5)
    if response.returncode:
        detail = (response.stderr + " " + response.stdout).strip()
        raise RuntimeError(f"{method} {url} failed: {detail[:500]}")
    raw = response.stdout.strip()
    return json.loads(raw) if raw else None


def state_counts(group):
    state = group.get("current_state") or {}
    counts = state.get("instance_status_counts") or {}
    return state.get("status", "unknown"), counts


def install_signal_handlers():
    def stop(_signum, _frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def write_summary(path, metadata, rows, error=None):
    valid = [r for r in rows if r["sample_ok"] == "true"]
    aggregate = [float(r["estimated_total_hashrate_hs"]) for r in valid]
    peak_running = max((int(r["running_replicas"]) for r in rows), default=0)
    mean_hashrate = sum(aggregate) / len(aggregate) if aggregate else 0.0
    peak_hashrate = max(aggregate, default=0.0)
    requested_seconds = int(metadata["duration_minutes"]) * 60
    observed_seconds = float(rows[-1]["elapsed_s"]) if rows else 0.0

    lines = [
        "# SaladCloud mining experiment",
        "",
        f"- Started (UTC): `{metadata['started_utc']}`",
        f"- Requested: **{metadata['replicas']} replicas**, **{metadata['priority']} priority**, "
        f"**{metadata['duration_minutes']} minutes**",
        f"- Peak running replicas: **{peak_running}**",
        f"- Valid gateway samples: **{len(valid)} / {len(rows)}**",
        f"- Mean estimated aggregate hashrate: **{mean_hashrate / 1e9:.3f} GH/s**",
        f"- Peak estimated aggregate hashrate: **{peak_hashrate / 1e9:.3f} GH/s**",
        f"- Observed duration: **{observed_seconds:.1f} / {requested_seconds} seconds**",
        "",
        "> Aggregate hashrate is estimated as the load-balanced replica sample multiplied by "
        "the API-reported running replica count.",
    ]
    if error:
        lines.extend(["", f"Experiment error: `{error}`"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args):
    install_signal_handlers()
    api_key = os.environ.get("SALAD_API_KEY")
    if not api_key:
        raise RuntimeError("SALAD_API_KEY is not set")

    base = (
        "https://api.salad.com/api/public/organizations/"
        f"{args.organization}/projects/{args.project}/containers/{args.group}"
    )
    headers = {"Salad-Api-Key": api_key}
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"salad-mining-{run_id}.csv"
    summary_path = output_dir / "summary.md"

    original = request_json(base, headers=headers)
    original_status, _ = state_counts(original)
    original_replicas = int(original["replicas"])
    original_priority = original.get("priority") or "batch"
    was_running = original_status not in ("stopped", "stopping")
    metadata = {
        "started_utc": utc_now(),
        "replicas": args.replicas,
        "priority": args.priority,
        "duration_minutes": args.duration_minutes,
        "original_replicas": original_replicas,
        "original_priority": original_priority,
        "original_status": original_status,
    }

    fields = [
        "timestamp_utc", "elapsed_s", "group_status", "desired_replicas",
        "running_replicas", "allocating_replicas", "creating_replicas",
        "sample_ok", "sample_gpu", "sample_worker", "sample_uptime_s",
        "sample_hashrate_hs", "estimated_total_hashrate_hs",
        "sample_accepted", "sample_rejected", "sample_miner_up", "error",
    ]
    rows = []
    experiment_error = None
    started = time.monotonic()

    try:
        request_json(
            base,
            method="PATCH",
            headers=headers,
            body={"replicas": args.replicas, "container": {"priority": args.priority}},
        )
        if not was_running:
            request_json(base + "/start", method="POST", headers=headers)

        deadline = started + args.duration_minutes * 60
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fields)
            writer.writeheader()
            while time.monotonic() < deadline and not STOP_REQUESTED:
                loop_started = time.monotonic()
                row = {field: "" for field in fields}
                row["timestamp_utc"] = utc_now()
                row["elapsed_s"] = f"{loop_started - started:.1f}"
                try:
                    group = request_json(base, headers=headers)
                    status, counts = state_counts(group)
                    running = int(counts.get("running_count", 0) or 0)
                    row.update({
                        "group_status": status,
                        "desired_replicas": group.get("replicas", ""),
                        "running_replicas": running,
                        "allocating_replicas": counts.get("allocating_count", 0) or 0,
                        "creating_replicas": counts.get("creating_count", 0) or 0,
                    })
                    sample = request_json(args.status_url, timeout=15)
                    sample_hashrate = float(sample.get("hashrate_hs", 0) or 0)
                    row.update({
                        "sample_ok": "true",
                        "sample_gpu": sample.get("gpu", ""),
                        "sample_worker": sample.get("worker", ""),
                        "sample_uptime_s": sample.get("uptime_s", ""),
                        "sample_hashrate_hs": f"{sample_hashrate:.3f}",
                        "estimated_total_hashrate_hs": f"{sample_hashrate * running:.3f}",
                        "sample_accepted": sample.get("accepted", ""),
                        "sample_rejected": sample.get("rejected", ""),
                        "sample_miner_up": str(bool(sample.get("miner_up"))).lower(),
                    })
                except Exception as exc:
                    row["sample_ok"] = "false"
                    row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                writer.writerow(row)
                csv_file.flush()
                print(
                    f"[{row['elapsed_s']:>7}s] running={row['running_replicas'] or '?'} "
                    f"sample={row['sample_hashrate_hs'] or '?'} H/s",
                    flush=True,
                )
                remaining = args.interval_seconds - (time.monotonic() - loop_started)
                if remaining > 0:
                    time.sleep(remaining)
    except Exception as exc:
        experiment_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cleanup_errors = []
        try:
            request_json(
                base,
                method="PATCH",
                headers=headers,
                body={
                    "replicas": original_replicas,
                    "container": {"priority": original_priority},
                },
            )
        except Exception as exc:
            cleanup_errors.append(f"restore config: {exc}")
        if not was_running:
            try:
                request_json(base + "/stop", method="POST", headers=headers)
            except Exception as exc:
                cleanup_errors.append(f"restore stopped state: {exc}")
        if cleanup_errors:
            extra = "; ".join(cleanup_errors)
            experiment_error = f"{experiment_error}; {extra}" if experiment_error else extra
        write_summary(summary_path, metadata, rows, experiment_error)
        print(f"CSV: {csv_path}", flush=True)
        print(f"Summary: {summary_path}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--organization", default="volko")
    parser.add_argument("--project", default="dzadzafrref")
    parser.add_argument("--group", default="mining-http")
    parser.add_argument(
        "--status-url",
        default="https://clementine-splitpea-u7f4mjls9w2wwh94.salad.cloud/status",
    )
    parser.add_argument("--replicas", type=int, default=10)
    parser.add_argument("--priority", choices=("high", "medium", "low", "batch"), default="high")
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--interval-seconds", type=int, default=10)
    parser.add_argument("--output-dir", default="results")
    args = parser.parse_args()
    if not 1 <= args.replicas <= 500:
        parser.error("--replicas must be between 1 and 500")
    if not 1 <= args.duration_minutes <= 360:
        parser.error("--duration-minutes must be between 1 and 360")
    if not 5 <= args.interval_seconds <= 300:
        parser.error("--interval-seconds must be between 5 and 300")
    run(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
