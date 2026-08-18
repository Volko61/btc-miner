#!/usr/bin/env python3
"""Archive per-machine ccminer events from Salad Log Explorer into dashboard CSVs."""

import argparse
import csv
import getpass
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archive_results import merge_rows, read_rows
from salad_experiment import RUN_FIELDS


EVENT_FIELDS = [
    "experiment_id", "timestamp_utc", "machine_id", "instance_id", "event_type",
    "hashrate_hs", "message",
]

MACHINE_FIELDS = [
    "experiment_id", "machine_id", "hashrate_samples", "stable_hashrate_samples",
    "mean_stable_hashrate_hs", "peak_hashrate_hs", "accepted_log_events",
    "rejected_log_events", "auth_errors", "subscribe_timeouts",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RATE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?)H/s", re.IGNORECASE)
RATE_MULTIPLIERS = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def api_time(value):
    # Salad rejects log queries carrying microsecond or nanosecond precision.
    stamp = value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def post_json(url, api_key, body, retries=5):
    encoded = json.dumps(body, separators=(",", ":")).encode()
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Salad-Api-Key": api_key,
                "User-Agent": "btc-miner-log-archive/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if (exc.code != 429 and exc.code < 500) or attempt == retries - 1:
                raise RuntimeError(f"Salad log query failed ({exc.code}): {detail}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError("Salad log query retries exhausted")


def query_pages(api_key, url, query, start_time, end_time, page_size):
    items = []
    # Salad filters and sorts on receive_time, which lags the miner's own log
    # timestamp, so pagination must walk receive_time forward, not text time.
    page_start = start_time
    while page_start < end_time:
        page = post_json(api_key=api_key, url=url, body={
            "start_time": api_time(page_start),
            "end_time": api_time(end_time),
            "query": query,
            "page_size": page_size,
            "sort_order": "asc",
        })
        page_items = page.get("items") or []
        if not page_items:
            break
        items.extend(page_items)
        newest = max(
            parse_time(item["receive_time"])
            for item in page_items if item.get("receive_time")
        )
        if len(page_items) < page_size:
            break
        # Rewind to the whole millisecond so sub-millisecond neighbours are not
        # skipped; duplicates from the overlap are removed by the caller.
        next_start = newest.replace(microsecond=(newest.microsecond // 1000) * 1000)
        if next_start <= page_start:
            next_start = page_start + timedelta(milliseconds=1)
        page_start = next_start
    return items


def query_logs(
    api_key, organization, project, group, start_time, end_time,
    page_size=100, window=timedelta(minutes=5),
):
    url = f"https://api.salad.com/api/public/organizations/{organization}/log-entries"
    query = (
        f'resource.labels.project_name = "{project}" AND '
        f'resource.labels.container_group_name = "{group}" AND '
        'resource.type = "container"'
    )
    # The log API returns 500 on wide ranges, so walk the run in short windows.
    items = []
    window_start = start_time
    while window_start < end_time:
        window_end = min(window_start + window, end_time)
        items.extend(
            query_pages(api_key, url, query, window_start, window_end, page_size)
        )
        window_start = window_end
    unique = {}
    for item in items:
        labels = (item.get("resource") or {}).get("labels") or {}
        message = item.get("text_log")
        if message is None:
            message = json.dumps(item.get("json_log"), sort_keys=True, ensure_ascii=False)
        key = (item.get("time"), labels.get("instance_id"), message)
        unique[key] = item
    return sorted(unique.values(), key=lambda item: item.get("time", ""))


def classify(message):
    lower = message.lower()
    rate = None
    if "gpu #0:" in lower:
        match = RATE_RE.search(message)
        if match:
            rate = float(match.group(1)) * RATE_MULTIPLIERS[match.group(2).upper()]
            return "hashrate", rate
    if "stratum authentication failed" in lower:
        return "auth_error", rate
    if "stratum_subscribe timed out" in lower:
        return "subscribe_timeout", rate
    if "yes!" in lower or "accepted:" in lower:
        return "share_accepted", rate
    if "booooo" in lower or "rejected:" in lower:
        return "share_rejected", rate
    if "starting on stratum" in lower:
        return "miner_start", rate
    return None, None


def build_rows(experiment_id, log_items):
    events = []
    for item in log_items:
        labels = (item.get("resource") or {}).get("labels") or {}
        message = item.get("text_log")
        if message is None:
            message = json.dumps(item.get("json_log"), sort_keys=True, ensure_ascii=False)
        message = ANSI_RE.sub("", message or "").strip()
        event_type, hashrate = classify(message)
        if not event_type:
            continue
        events.append({
            "experiment_id": experiment_id,
            "timestamp_utc": item.get("time", ""),
            "machine_id": labels.get("machine_id", ""),
            "instance_id": labels.get("instance_id", ""),
            "event_type": event_type,
            "hashrate_hs": "" if hashrate is None else f"{hashrate:.3f}",
            "message": message,
        })

    by_machine = defaultdict(list)
    for event in events:
        if event["machine_id"]:
            by_machine[event["machine_id"]].append(event)

    machines = []
    for machine_id, machine_events in sorted(by_machine.items()):
        rates = [
            float(event["hashrate_hs"])
            for event in machine_events if event["event_type"] == "hashrate"
        ]
        # Ignore only the sub-5 GH/s CUDA warm-up samples visible during the first seconds.
        stable = [rate for rate in rates if rate >= 5e9]
        machines.append({
            "experiment_id": experiment_id,
            "machine_id": machine_id,
            "hashrate_samples": len(rates),
            "stable_hashrate_samples": len(stable),
            "mean_stable_hashrate_hs": (
                f"{sum(stable) / len(stable):.3f}" if stable else ""
            ),
            "peak_hashrate_hs": f"{max(rates):.3f}" if rates else "",
            "accepted_log_events": sum(
                event["event_type"] == "share_accepted" for event in machine_events
            ),
            "rejected_log_events": sum(
                event["event_type"] == "share_rejected" for event in machine_events
            ),
            "auth_errors": sum(
                event["event_type"] == "auth_error" for event in machine_events
            ),
            "subscribe_timeouts": sum(
                event["event_type"] == "subscribe_timeout" for event in machine_events
            ),
        })
    return events, machines


def update_run_summary(path, experiment_id, machines):
    rows = read_rows(path)
    target = next((row for row in rows if row.get("experiment_id") == experiment_id), None)
    if target is None:
        raise RuntimeError(f"experiment {experiment_id} is absent from {path}")
    means = [
        float(row["mean_stable_hashrate_hs"])
        for row in machines if row["mean_stable_hashrate_hs"]
    ]
    peaks = [
        float(row["peak_hashrate_hs"])
        for row in machines if row["peak_hashrate_hs"]
    ]
    target.update({
        "log_derived_machines": len(means),
        "log_derived_mean_aggregate_hashrate_hs": (
            f"{sum(means):.3f}" if means else ""
        ),
        "log_derived_peak_sum_hashrate_hs": f"{sum(peaks):.3f}" if peaks else "",
        "log_auth_errors": sum(int(row["auth_errors"]) for row in machines),
        "log_subscribe_timeouts": sum(int(row["subscribe_timeouts"]) for row in machines),
    })
    merge_rows(path, RUN_FIELDS, rows, ("experiment_id",))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--organization", default="volko")
    parser.add_argument("--project", default="dzadzafrref")
    parser.add_argument("--group", default="mining-http")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--start-time", required=True)
    parser.add_argument("--end-time", required=True)
    parser.add_argument("--dashboard-data", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.environ.get("SALAD_API_KEY") or getpass.getpass("Salad API key: ")
    items = query_logs(
        api_key,
        args.organization,
        args.project,
        args.group,
        parse_time(args.start_time),
        parse_time(args.end_time),
    )
    events, machines = build_rows(args.experiment_id, items)
    args.dashboard_data.mkdir(parents=True, exist_ok=True)
    event_path = args.dashboard_data / "salad_miner_events.csv"
    machine_path = args.dashboard_data / "salad_machines.csv"
    merge_rows(
        event_path,
        EVENT_FIELDS,
        events,
        ("experiment_id", "timestamp_utc", "machine_id", "message"),
    )
    merge_rows(
        machine_path,
        MACHINE_FIELDS,
        machines,
        ("experiment_id", "machine_id"),
    )
    update_run_summary(
        args.dashboard_data / "salad_runs.csv", args.experiment_id, machines
    )
    print(f"Archived {len(events)} miner events for {len(machines)} machines")
    print(event_path)
    print(machine_path)


if __name__ == "__main__":
    main()
