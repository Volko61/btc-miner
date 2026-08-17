#!/usr/bin/env python3
"""Merge Salad artifacts into stable, dashboard-ready CSV history files."""

import argparse
import csv
from pathlib import Path

from salad_experiment import (
    INSTANCE_FIELDS,
    RUN_FIELDS,
    SAMPLE_FIELDS,
    build_run_summary,
    number,
)


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def merge_rows(path, fields, new_rows, key_fields):
    existing = read_rows(path) if path.exists() else []
    merged = {}
    for row in existing + new_rows:
        normalized = {field: row.get(field, "") for field in fields}
        key = tuple(normalized.get(field, "") for field in key_fields)
        merged[key] = normalized
    rows = sorted(merged.values(), key=lambda row: tuple(row.get(field, "") for field in key_fields))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(existing), len(rows)


def migrate_legacy(path, experiment_id, priority, price, duration_minutes):
    migrated = []
    previous_elapsed = 0.0
    previous_running = None
    cumulative_billed = 0.0
    cumulative_cost = 0.0

    for source in read_rows(path):
        elapsed = number(source.get("elapsed_s"))
        running = int(number(source.get("running_replicas")))
        if previous_running is None:
            billed = max(0.0, elapsed) * running
        else:
            billed = max(0.0, elapsed - previous_elapsed) * (previous_running + running) / 2.0
        interval_cost = billed * price / 3600.0
        cumulative_billed += billed
        cumulative_cost += interval_cost

        row = {field: "" for field in SAMPLE_FIELDS}
        row.update({
            "schema_version": "2",
            "source": "legacy_v1",
            "experiment_id": experiment_id,
            "timestamp_utc": source.get("timestamp_utc", ""),
            "elapsed_s": source.get("elapsed_s", ""),
            "group_status": source.get("group_status", ""),
            "group_priority": priority,
            "desired_replicas": source.get("desired_replicas", ""),
            "running_replicas": source.get("running_replicas", ""),
            "allocating_replicas": source.get("allocating_replicas", ""),
            "creating_replicas": source.get("creating_replicas", ""),
            "status_sample_ok": source.get("sample_ok", ""),
            "sample_gpu": source.get("sample_gpu", ""),
            "sample_worker": source.get("sample_worker", ""),
            "sample_uptime_s": source.get("sample_uptime_s", ""),
            "sample_hashrate_hs": source.get("sample_hashrate_hs", ""),
            "legacy_naive_estimated_total_hashrate_hs": source.get(
                "estimated_total_hashrate_hs", ""
            ),
            "sample_accepted": source.get("sample_accepted", ""),
            "sample_rejected": source.get("sample_rejected", ""),
            "sample_miner_up": source.get("sample_miner_up", ""),
            "price_usd_per_instance_hour": f"{price:.6f}",
            "interval_billed_instance_seconds": f"{billed:.3f}",
            "cumulative_billed_instance_seconds": f"{cumulative_billed:.3f}",
            "interval_cost_usd": f"{interval_cost:.6f}",
            "cumulative_cost_usd": f"{cumulative_cost:.6f}",
            "status_error": source.get("error", ""),
        })
        migrated.append(row)
        previous_elapsed = elapsed
        previous_running = running

    started = migrated[0]["timestamp_utc"] if migrated else ""
    finished = migrated[-1]["timestamp_utc"] if migrated else ""
    metadata = {
        "schema_version": "2",
        "source": "legacy_v1",
        "experiment_id": experiment_id,
        "started_utc": started,
        "finished_utc": finished,
        "replicas": max((int(number(row["desired_replicas"])) for row in migrated), default=0),
        "priority": priority,
        "duration_minutes": duration_minutes,
        "price_usd_per_instance_hour": price,
    }
    return migrated, build_run_summary(metadata, migrated)


def timestamp_from_legacy_name(path):
    stem = path.stem
    marker = "salad-mining-"
    return stem[len(marker):] if stem.startswith(marker) else stem


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--dashboard-data", type=Path, required=True)
    parser.add_argument("--legacy-run-label", default="github")
    parser.add_argument("--legacy-priority", choices=("high", "medium", "low", "batch"), default="batch")
    parser.add_argument("--legacy-price-usd-per-instance-hour", type=float, default=0.294)
    parser.add_argument("--legacy-duration-minutes", type=int, default=60)
    args = parser.parse_args()

    sample_rows = []
    instance_rows = []
    run_rows = []

    for path in sorted(args.artifact_dir.rglob("salad-samples-*.csv")):
        sample_rows.extend(read_rows(path))
    for path in sorted(args.artifact_dir.rglob("salad-instances-*.csv")):
        instance_rows.extend(read_rows(path))
    for path in sorted(args.artifact_dir.rglob("salad-runs-*.csv")):
        run_rows.extend(read_rows(path))

    for path in sorted(args.artifact_dir.rglob("salad-mining-*.csv")):
        timestamp = timestamp_from_legacy_name(path)
        experiment_id = f"{args.legacy_run_label}-{timestamp}"
        samples, summary = migrate_legacy(
            path,
            experiment_id,
            args.legacy_priority,
            args.legacy_price_usd_per_instance_hour,
            args.legacy_duration_minutes,
        )
        sample_rows.extend(samples)
        run_rows.append(summary)

    outputs = [
        (
            args.dashboard_data / "salad_samples.csv",
            SAMPLE_FIELDS,
            sample_rows,
            ("experiment_id", "timestamp_utc"),
        ),
        (
            args.dashboard_data / "salad_instances.csv",
            INSTANCE_FIELDS,
            instance_rows,
            ("experiment_id", "timestamp_utc", "instance_id"),
        ),
        (
            args.dashboard_data / "salad_runs.csv",
            RUN_FIELDS,
            run_rows,
            ("experiment_id",),
        ),
    ]
    for path, fields, rows, keys in outputs:
        before, after = merge_rows(path, fields, rows, keys)
        print(f"{path}: {before} -> {after} rows")


if __name__ == "__main__":
    main()
