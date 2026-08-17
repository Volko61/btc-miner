#!/usr/bin/env python3
"""Run, measure, cost, and restore a bounded SaladCloud mining experiment."""

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

SAMPLE_FIELDS = [
    "schema_version", "source", "experiment_id", "timestamp_utc", "elapsed_s", "group_status",
    "group_priority", "group_version", "group_pending_change", "container_image",
    "desired_replicas", "running_replicas", "allocating_replicas",
    "creating_replicas", "stopping_replicas", "ready_instances",
    "started_instances", "known_instances", "status_sample_ok", "sample_gpu",
    "sample_machine_id", "sample_instance_id", "sample_worker", "sample_uptime_s",
    "sample_hashrate_hs", "tracked_instances", "tracked_coverage_ratio",
    "tracked_hashrate_hs", "legacy_naive_estimated_total_hashrate_hs",
    "sample_accepted", "sample_rejected",
    "sample_miner_up", "price_usd_per_instance_hour",
    "interval_billed_instance_seconds", "cumulative_billed_instance_seconds",
    "interval_cost_usd", "cumulative_cost_usd", "api_error", "status_error",
]

INSTANCE_FIELDS = [
    "schema_version", "source", "experiment_id", "timestamp_utc", "elapsed_s",
    "instance_id", "machine_id",
    "state", "ready", "started", "version", "update_time", "cpu_percent",
    "cpu_usage_s", "cpu_usage_total_s", "memory_usage_mb",
    "memory_usage_percent", "pulling_progress", "deletion_cost",
    "latest_hashrate_hs", "latest_hashrate_age_s",
]

RUN_FIELDS = [
    "schema_version", "source", "experiment_id", "started_utc", "finished_utc",
    "requested_replicas",
    "priority", "duration_requested_s", "duration_observed_s",
    "price_usd_per_instance_hour", "peak_running_replicas",
    "seconds_to_first_running", "seconds_to_full_capacity",
    "billed_instance_seconds", "gpu_hours", "estimated_cost_usd",
    "valid_status_samples", "complete_tracked_samples",
    "mean_complete_tracked_hashrate_hs", "peak_tracked_hashrate_hs",
    "peak_sample_hashrate_hs", "legacy_mean_naive_estimated_total_hashrate_hs",
    "legacy_peak_naive_estimated_total_hashrate_hs", "unique_machines_sampled", "error",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url, method="GET", headers=None, body=None, timeout=30):
    # Cloudflare rejette urllib sur les runners hébergés ; curl y est préinstallé.
    command = [
        "curl", "--fail-with-body", "--silent", "--show-error",
        "--max-time", str(timeout), "--request", method,
        "--header", "Accept: application/json",
        "--user-agent", "btc-miner-experiment/2.0",
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
    return state.get("status", "unknown"), state.get("instance_status_counts") or {}


def instance_items(response):
    if not response:
        return []
    return response.get("instances") or response.get("items") or []


def experiment_patch(replicas, priority):
    # L'API renvoie `priority` à la racine, mais le schéma PATCH l'attend dans
    # `container`. Cette asymétrie est volontaire dans l'API SaladCloud.
    return {"replicas": replicas, "container": {"priority": priority}}


def install_signal_handlers():
    def stop(_signum, _frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_interval_billing(previous_time, now, previous_running, running, rate):
    """Trapèze entre deux relevés ; erreur maximale bornée par l'intervalle."""
    if previous_time is None or previous_running is None:
        return 0.0, 0.0
    seconds = max(0.0, now - previous_time)
    billed_seconds = seconds * (previous_running + running) / 2.0
    return billed_seconds, billed_seconds * rate / 3600.0


def current_tracked_hashrate(tracked, running_machine_ids, now, stale_after):
    values = []
    for machine_id in running_machine_ids:
        sample = tracked.get(machine_id)
        if sample and now - sample["monotonic"] <= stale_after:
            values.append(sample["hashrate_hs"])
    return len(values), sum(values)


def build_run_summary(metadata, rows, error=None):
    valid = [row for row in rows if row["status_sample_ok"] == "true"]
    complete = [
        row for row in rows
        if number(row["tracked_coverage_ratio"]) >= 0.999
        and int(number(row["running_replicas"])) > 0
    ]
    tracked = [number(row["tracked_hashrate_hs"]) for row in complete]
    samples = [number(row["sample_hashrate_hs"]) for row in valid]
    first_running = next(
        (number(row["elapsed_s"]) for row in rows if number(row["running_replicas"]) > 0),
        None,
    )
    full_capacity = next(
        (
            number(row["elapsed_s"]) for row in rows
            if int(number(row["running_replicas"])) >= metadata["replicas"]
        ),
        None,
    )
    last = rows[-1] if rows else {}
    billed_seconds = number(last.get("cumulative_billed_instance_seconds"))
    machines = {row["sample_machine_id"] for row in valid if row["sample_machine_id"]}
    legacy_naive = [
        number(row["legacy_naive_estimated_total_hashrate_hs"])
        for row in valid if row["legacy_naive_estimated_total_hashrate_hs"] != ""
    ]
    return {
        "schema_version": metadata.get("schema_version", "2"),
        "source": metadata.get("source", "native_v2"),
        "experiment_id": metadata["experiment_id"],
        "started_utc": metadata["started_utc"],
        "finished_utc": metadata.get("finished_utc") or utc_now(),
        "requested_replicas": metadata["replicas"],
        "priority": metadata["priority"],
        "duration_requested_s": metadata["duration_minutes"] * 60,
        "duration_observed_s": f"{number(last.get('elapsed_s')):.1f}",
        "price_usd_per_instance_hour": f"{metadata['price_usd_per_instance_hour']:.6f}",
        "peak_running_replicas": max(
            (int(number(row["running_replicas"])) for row in rows), default=0
        ),
        "seconds_to_first_running": "" if first_running is None else f"{first_running:.1f}",
        "seconds_to_full_capacity": "" if full_capacity is None else f"{full_capacity:.1f}",
        "billed_instance_seconds": f"{billed_seconds:.3f}",
        "gpu_hours": f"{billed_seconds / 3600.0:.6f}",
        "estimated_cost_usd": f"{number(last.get('cumulative_cost_usd')):.6f}",
        "valid_status_samples": len(valid),
        "complete_tracked_samples": len(complete),
        "mean_complete_tracked_hashrate_hs": (
            f"{sum(tracked) / len(tracked):.3f}" if tracked else ""
        ),
        "peak_tracked_hashrate_hs": f"{max(tracked, default=0.0):.3f}",
        "peak_sample_hashrate_hs": f"{max(samples, default=0.0):.3f}",
        "legacy_mean_naive_estimated_total_hashrate_hs": (
            f"{sum(legacy_naive) / len(legacy_naive):.3f}" if legacy_naive else ""
        ),
        "legacy_peak_naive_estimated_total_hashrate_hs": (
            f"{max(legacy_naive):.3f}" if legacy_naive else ""
        ),
        "unique_machines_sampled": len(machines),
        "error": error or "",
    }


def write_summaries(markdown_path, run_csv_path, metadata, rows, error=None):
    summary = build_run_summary(metadata, rows, error)
    with run_csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RUN_FIELDS)
        writer.writeheader()
        writer.writerow(summary)

    def display_seconds(value):
        return "non atteint" if value == "" else f"{number(value):.1f} s"

    mean_hashrate = number(summary["mean_complete_tracked_hashrate_hs"]) / 1e9
    peak_hashrate = number(summary["peak_tracked_hashrate_hs"]) / 1e9
    lines = [
        "# SaladCloud mining experiment",
        "",
        f"- Experiment: `{summary['experiment_id']}`",
        f"- Started (UTC): `{summary['started_utc']}`",
        f"- Requested: **{summary['requested_replicas']} replicas**, "
        f"**{summary['priority']} priority**, **{metadata['duration_minutes']} minutes**",
        f"- Peak running replicas: **{summary['peak_running_replicas']}**",
        f"- Time to first running replica: **{display_seconds(summary['seconds_to_first_running'])}**",
        f"- Time to full capacity: **{display_seconds(summary['seconds_to_full_capacity'])}**",
        f"- Billed GPU-hours (estimated from running state): **{number(summary['gpu_hours']):.4f}**",
        f"- Estimated Salad cost: **${number(summary['estimated_cost_usd']):.4f}** "
        f"at ${metadata['price_usd_per_instance_hour']:.3f}/GPU-hour",
        f"- Unique machines sampled: **{summary['unique_machines_sampled']}**",
        f"- Complete tracked samples: **{summary['complete_tracked_samples']}**",
        f"- Mean complete aggregate hashrate: **{mean_hashrate:.3f} GH/s**",
        f"- Peak tracked aggregate hashrate: **{peak_hashrate:.3f} GH/s**",
        "",
        "> Costs are integrated from API-reported running instances. Salad bills per second; "
        "allocation and image-download time is not charged.",
    ]
    if error:
        lines.extend(["", f"Experiment error: `{error}`"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sample_path = output_dir / f"salad-samples-{experiment_id}.csv"
    instance_path = output_dir / f"salad-instances-{experiment_id}.csv"
    run_path = output_dir / f"salad-runs-{experiment_id}.csv"
    summary_path = output_dir / "summary.md"

    original = request_json(base, headers=headers)
    original_status, _ = state_counts(original)
    original_replicas = int(original["replicas"])
    original_priority = original.get("priority") or "batch"
    was_running = original_status not in ("stopped", "stopping")
    metadata = {
        "schema_version": "2",
        "source": "native_v2",
        "experiment_id": experiment_id,
        "started_utc": utc_now(),
        "replicas": args.replicas,
        "priority": args.priority,
        "duration_minutes": args.duration_minutes,
        "price_usd_per_instance_hour": args.price_usd_per_instance_hour,
        "original_replicas": original_replicas,
        "original_priority": original_priority,
        "original_status": original_status,
    }

    rows = []
    experiment_error = None
    started = time.monotonic()
    previous_sample_time = None
    previous_running = None
    cumulative_billed_seconds = 0.0
    cumulative_cost = 0.0
    tracked = {}

    try:
        request_json(
            base,
            method="PATCH",
            headers=headers,
            body=experiment_patch(args.replicas, args.priority),
        )
        if not was_running:
            request_json(base + "/start", method="POST", headers=headers)

        deadline = started + args.duration_minutes * 60
        with (
            sample_path.open("w", newline="", encoding="utf-8") as sample_file,
            instance_path.open("w", newline="", encoding="utf-8") as instance_file,
        ):
            sample_writer = csv.DictWriter(sample_file, fieldnames=SAMPLE_FIELDS)
            instance_writer = csv.DictWriter(instance_file, fieldnames=INSTANCE_FIELDS)
            sample_writer.writeheader()
            instance_writer.writeheader()

            while time.monotonic() < deadline and not STOP_REQUESTED:
                loop_started = time.monotonic()
                elapsed = loop_started - started
                timestamp = utc_now()
                row = {field: "" for field in SAMPLE_FIELDS}
                row.update({
                    "schema_version": "2",
                    "source": "native_v2",
                    "experiment_id": experiment_id,
                    "timestamp_utc": timestamp,
                    "elapsed_s": f"{elapsed:.1f}",
                    "price_usd_per_instance_hour": f"{args.price_usd_per_instance_hour:.6f}",
                })
                instances = []
                running = previous_running or 0

                try:
                    group = request_json(base, headers=headers)
                    instances = instance_items(request_json(base + "/instances", headers=headers))
                    status, counts = state_counts(group)
                    running = int(counts.get("running_count", 0) or 0)
                    row.update({
                        "group_status": status,
                        "group_priority": group.get("priority", ""),
                        "group_version": group.get("version", ""),
                        "group_pending_change": str(bool(group.get("pending_change"))).lower(),
                        "container_image": (group.get("container") or {}).get("image", ""),
                        "desired_replicas": group.get("replicas", ""),
                        "running_replicas": running,
                        "allocating_replicas": counts.get("allocating_count", 0) or 0,
                        "creating_replicas": counts.get("creating_count", 0) or 0,
                        "stopping_replicas": counts.get("stopping_count", 0) or 0,
                        "ready_instances": sum(bool(item.get("ready")) for item in instances),
                        "started_instances": sum(bool(item.get("started")) for item in instances),
                        "known_instances": len(instances),
                    })
                except Exception as exc:
                    row["api_error"] = f"{type(exc).__name__}: {exc}"

                billed_seconds, interval_cost = compute_interval_billing(
                    previous_sample_time,
                    loop_started,
                    previous_running,
                    running,
                    args.price_usd_per_instance_hour,
                )
                cumulative_billed_seconds += billed_seconds
                cumulative_cost += interval_cost
                row.update({
                    "interval_billed_instance_seconds": f"{billed_seconds:.3f}",
                    "cumulative_billed_instance_seconds": f"{cumulative_billed_seconds:.3f}",
                    "interval_cost_usd": f"{interval_cost:.6f}",
                    "cumulative_cost_usd": f"{cumulative_cost:.6f}",
                })

                try:
                    sample = request_json(args.status_url, timeout=15)
                    sample_hashrate = number(sample.get("hashrate_hs"))
                    machine_id = sample.get("machine_id") or ""
                    if machine_id:
                        tracked[machine_id] = {
                            "hashrate_hs": sample_hashrate,
                            "monotonic": loop_started,
                        }
                    row.update({
                        "status_sample_ok": "true",
                        "sample_gpu": sample.get("gpu", ""),
                        "sample_machine_id": machine_id,
                        "sample_instance_id": sample.get("instance_id", ""),
                        "sample_worker": sample.get("worker", ""),
                        "sample_uptime_s": sample.get("uptime_s", ""),
                        "sample_hashrate_hs": f"{sample_hashrate:.3f}",
                        "sample_accepted": sample.get("accepted", ""),
                        "sample_rejected": sample.get("rejected", ""),
                        "sample_miner_up": str(bool(sample.get("miner_up"))).lower(),
                    })
                except Exception as exc:
                    row["status_sample_ok"] = "false"
                    row["status_error"] = f"{type(exc).__name__}: {exc}"

                running_items = [item for item in instances if item.get("state") == "running"]
                running_machine_ids = {
                    item.get("machine_id") for item in running_items if item.get("machine_id")
                }
                stale_after = max(60.0, args.interval_seconds * max(1, running) * 2.5)
                tracked_count, tracked_hashrate = current_tracked_hashrate(
                    tracked, running_machine_ids, loop_started, stale_after
                )
                coverage = tracked_count / len(running_machine_ids) if running_machine_ids else 0.0
                row.update({
                    "tracked_instances": tracked_count,
                    "tracked_coverage_ratio": f"{coverage:.6f}",
                    "tracked_hashrate_hs": f"{tracked_hashrate:.3f}",
                })

                for item in instances:
                    machine_id = item.get("machine_id") or ""
                    latest = tracked.get(machine_id)
                    instance_writer.writerow({
                        "schema_version": "2",
                        "source": "native_v2",
                        "experiment_id": experiment_id,
                        "timestamp_utc": timestamp,
                        "elapsed_s": f"{elapsed:.1f}",
                        "instance_id": item.get("id", ""),
                        "machine_id": machine_id,
                        "state": item.get("state", ""),
                        "ready": str(bool(item.get("ready"))).lower(),
                        "started": str(bool(item.get("started"))).lower(),
                        "version": item.get("version", ""),
                        "update_time": item.get("update_time", ""),
                        "cpu_percent": item.get("cpu_percent", ""),
                        "cpu_usage_s": item.get("cpu_usage", ""),
                        "cpu_usage_total_s": item.get("cpu_usage_total", ""),
                        "memory_usage_mb": item.get("memory_usage_mb", ""),
                        "memory_usage_percent": item.get("memory_usage_percent", ""),
                        "pulling_progress": item.get("pulling_progress", ""),
                        "deletion_cost": item.get("deletion_cost", ""),
                        "latest_hashrate_hs": (
                            f"{latest['hashrate_hs']:.3f}" if latest else ""
                        ),
                        "latest_hashrate_age_s": (
                            f"{loop_started - latest['monotonic']:.1f}" if latest else ""
                        ),
                    })

                rows.append(row)
                sample_writer.writerow(row)
                sample_file.flush()
                instance_file.flush()
                print(
                    f"[{row['elapsed_s']:>7}s] priority={row['group_priority'] or '?'} "
                    f"running={row['running_replicas'] or '?'} "
                    f"tracked={tracked_count}/{len(running_machine_ids)} "
                    f"hashrate={tracked_hashrate / 1e9:.3f} GH/s "
                    f"cost=${cumulative_cost:.4f}",
                    flush=True,
                )
                previous_sample_time = loop_started
                previous_running = running
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
                body=experiment_patch(original_replicas, original_priority),
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
        write_summaries(summary_path, run_path, metadata, rows, experiment_error)
        print(f"Samples: {sample_path}", flush=True)
        print(f"Instances: {instance_path}", flush=True)
        print(f"Run: {run_path}", flush=True)
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
    parser.add_argument("--price-usd-per-instance-hour", type=float, default=0.35)
    parser.add_argument("--output-dir", default="results")
    args = parser.parse_args()
    if not 1 <= args.replicas <= 500:
        parser.error("--replicas must be between 1 and 500")
    if not 1 <= args.duration_minutes <= 360:
        parser.error("--duration-minutes must be between 1 and 360")
    if not 5 <= args.interval_seconds <= 300:
        parser.error("--interval-seconds must be between 5 and 300")
    if args.price_usd_per_instance_hour < 0:
        parser.error("--price-usd-per-instance-hour must be non-negative")
    run(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
