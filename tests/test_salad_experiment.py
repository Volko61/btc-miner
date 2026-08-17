import base64
import csv
import json
import tempfile
import unittest
from pathlib import Path

import status
from archive_results import migrate_legacy
from salad_experiment import (
    build_run_summary,
    compute_interval_billing,
    current_tracked_hashrate,
    experiment_patch,
)


class SaladExperimentTests(unittest.TestCase):
    def test_priority_is_nested_in_update_container_patch(self):
        self.assertEqual(
            experiment_patch(10, "high"),
            {"replicas": 10, "container": {"priority": "high"}},
        )

    def test_billing_uses_running_replica_trapezoid(self):
        billed, cost = compute_interval_billing(10.0, 20.0, 2, 4, 0.35)
        self.assertEqual(billed, 30.0)
        self.assertAlmostEqual(cost, 30.0 * 0.35 / 3600.0)

    def test_tracked_hashrate_drops_stale_and_non_running_nodes(self):
        tracked = {
            "a": {"hashrate_hs": 11e9, "monotonic": 95.0},
            "b": {"hashrate_hs": 12e9, "monotonic": 20.0},
            "gone": {"hashrate_hs": 99e9, "monotonic": 99.0},
        }
        count, total = current_tracked_hashrate(tracked, {"a", "b"}, 100.0, 60.0)
        self.assertEqual(count, 1)
        self.assertEqual(total, 11e9)

    def test_summary_never_treats_legacy_extrapolation_as_tracked(self):
        row = {
            "status_sample_ok": "true",
            "tracked_coverage_ratio": "",
            "running_replicas": "10",
            "tracked_hashrate_hs": "",
            "sample_hashrate_hs": "11000000000",
            "legacy_naive_estimated_total_hashrate_hs": "110000000000",
            "sample_machine_id": "",
            "elapsed_s": "60",
            "cumulative_billed_instance_seconds": "600",
            "cumulative_cost_usd": "0.058333",
        }
        metadata = {
            "experiment_id": "legacy",
            "started_utc": "2026-01-01T00:00:00+00:00",
            "replicas": 10,
            "priority": "batch",
            "duration_minutes": 1,
            "price_usd_per_instance_hour": 0.35,
            "source": "legacy_v1",
        }
        summary = build_run_summary(metadata, [row])
        self.assertEqual(summary["complete_tracked_samples"], 0)
        self.assertEqual(summary["mean_complete_tracked_hashrate_hs"], "")
        self.assertEqual(summary["legacy_peak_naive_estimated_total_hashrate_hs"], "110000000000.000")


class LegacyArchiveTests(unittest.TestCase):
    def test_legacy_cost_is_integrated_from_running_replicas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "salad-mining-20260101T000000Z.csv"
            fields = [
                "timestamp_utc", "elapsed_s", "group_status", "desired_replicas",
                "running_replicas", "allocating_replicas", "creating_replicas",
                "sample_ok", "sample_gpu", "sample_worker", "sample_uptime_s",
                "sample_hashrate_hs", "estimated_total_hashrate_hs",
                "sample_accepted", "sample_rejected", "sample_miner_up", "error",
            ]
            with path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "timestamp_utc": "2026-01-01T00:00:10+00:00",
                    "elapsed_s": "10", "running_replicas": "2", "sample_ok": "true",
                    "desired_replicas": "4", "sample_hashrate_hs": "1",
                    "estimated_total_hashrate_hs": "2",
                })
                writer.writerow({
                    "timestamp_utc": "2026-01-01T00:00:20+00:00",
                    "elapsed_s": "20", "running_replicas": "4", "sample_ok": "true",
                    "desired_replicas": "4", "sample_hashrate_hs": "1",
                    "estimated_total_hashrate_hs": "4",
                })
            rows, summary = migrate_legacy(path, "run", "batch", 0.36, 1)
            # 10 s × 2, puis 10 s × moyenne(2,4) = 50 instance-secondes.
            self.assertEqual(rows[-1]["cumulative_billed_instance_seconds"], "50.000")
            self.assertAlmostEqual(float(summary["estimated_cost_usd"]), 0.005)


class StatusIdentityTests(unittest.TestCase):
    def test_decode_imds_jwt_payload(self):
        claims = {"sub": "machine-1", "salad_container_group_id": "group-1"}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        self.assertEqual(status.decode_jwt_payload(f"header.{encoded}.signature"), claims)


if __name__ == "__main__":
    unittest.main()
