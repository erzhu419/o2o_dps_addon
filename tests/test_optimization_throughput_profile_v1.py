from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.optimization_throughput_profile_v1 import (
    OptimizationThroughputProfileV1Error,
    build_search_workload_budget_v1,
    build_throughput_profile_v1,
    project_wall_time_v1,
)


def task(
    task_id: str,
    *,
    kind: str = "ROLLOUT",
    status: str = "COMPLETE_VALID",
    workload: str = "short_wave",
    cpu: float = 2.0,
    simulated: float = 20.0,
    attempted: int | None = None,
    valid: int | None = None,
    incomplete: int | None = None,
    failed: int | None = None,
    exact: bool = True,
) -> dict:
    if kind == "ROLLOUT":
        attempted = 1 if attempted is None else attempted
        valid = (1 if status == "COMPLETE_VALID" else 0) if valid is None else valid
        incomplete = (1 if status == "INCOMPLETE" else 0) if incomplete is None else incomplete
        failed = (1 if status == "FAILED" else 0) if failed is None else failed
        simulated = simulated if valid else 0.0
    else:
        attempted = valid = incomplete = failed = 0
    return {
        "task_id": task_id,
        "batch_id": "batch-a",
        "task_kind": kind,
        "workload_class": workload,
        "policy_role": "candidate" if kind == "ROLLOUT" else "pipeline",
        "policy_id": "candidate-a" if kind == "ROLLOUT" and exact else None,
        "producer": "candidate-producer" if kind == "ROLLOUT" and exact else None,
        "workload_strata": ["short_wave", "single_target"] if kind == "ROLLOUT" and exact else [],
        "rollout_cpu_attribution": (
            "EXACT_HOMOGENEOUS_SHARD"
            if kind == "ROLLOUT" and exact
            else "UNAVAILABLE_MIXED_SHARD"
            if kind == "ROLLOUT"
            else "NOT_APPLICABLE"
        ),
        "rollout_breakdown": [],
        "completion_status": status,
        "trace_mode": "COMPACT",
        "attempted_rollout_count": attempted,
        "complete_valid_rollout_count": valid,
        "incomplete_rollout_count": incomplete,
        "failed_rollout_count": failed,
        "user_cpu_seconds": cpu,
        "system_cpu_seconds": 0.0,
        "child_user_cpu_seconds": 0.0,
        "child_system_cpu_seconds": 0.0,
        "wall_seconds": 1.0,
        "peak_rss_bytes": 1024,
        "simulated_combat_seconds": simulated if kind == "ROLLOUT" else 0.0,
        "event_count": 100,
        "state_interaction_count": 20,
        "input_bytes": 10,
        "json_bytes": 20,
        "compressed_bytes": 5,
        "output_bytes": 6,
    }


def batch(*, shared: bool = False) -> dict:
    row = {
        "batch_id": "batch-a",
        "node": "node001",
        "wall_seconds": 2.0,
        "logical_cpu_capacity": 192,
        "peak_rss_bytes": 4096,
    }
    if shared:
        row.update(
            {
                "measurement_scope": "NODE_JOB_WINDOW",
                "job_window_id": "job-a",
                "window_started_at_epoch_seconds": 100.0,
                "window_ended_at_epoch_seconds": 102.0,
                "worker_count": 2,
                "shard_count": 2,
                "representative_workload_strata": [
                    ["single_target", "short_wave"]
                ],
            }
        )
    return row


class OptimizationThroughputProfileV1Tests(unittest.TestCase):
    def test_incomplete_rollout_never_reduces_cost_per_valid_rollout(self) -> None:
        incomplete = task("bad", status="INCOMPLETE", cpu=100.0)
        profile = build_throughput_profile_v1(
            [task("good", cpu=2.0), incomplete], [batch()]
        )
        self.assertEqual(1, profile["overall"]["complete_valid_rollout_count"])
        self.assertEqual(102.0, profile["overall"]["cpu_seconds"])
        self.assertEqual(
            102.0,
            profile["overall"]["cpu_seconds_per_complete_valid_rollout"],
        )
        self.assertIsNone(profile["batches"][0]["measured_effective_cores"])
        self.assertEqual(
            "UNAVAILABLE_SINGLE_SHARD_WINDOW",
            profile["batches"][0]["utilization_status"],
        )

    def test_one_row_can_measure_a_shard_without_per_rollout_telemetry(self) -> None:
        row = task(
            "shard-1",
            status="INCOMPLETE",
            cpu=30.0,
            simulated=180.0,
            attempted=100,
            valid=90,
            incomplete=8,
            failed=2,
        )
        profile = build_throughput_profile_v1([row], [batch()])
        self.assertEqual(100, profile["overall"]["attempted_rollout_count"])
        self.assertEqual(90, profile["overall"]["complete_valid_rollout_count"])
        self.assertEqual(1 / 3, profile["overall"]["cpu_seconds_per_complete_valid_rollout"])

    def test_phases_remain_separate_and_projection_uses_measured_cpu(self) -> None:
        profile = build_throughput_profile_v1(
            [
                task("rollout", cpu=2.0),
                task(
                    "analysis",
                    kind="ANALYSIS",
                    workload="horizon_analysis",
                    cpu=8.0,
                    simulated=0.0,
                ),
            ],
            [batch(shared=True)],
        )
        self.assertEqual(2.0, profile["by_task_kind"]["ROLLOUT"]["cpu_seconds"])
        self.assertEqual(8.0, profile["by_task_kind"]["ANALYSIS"]["cpu_seconds"])
        projection = project_wall_time_v1(
            profile,
            rollout_count=100,
            job_window_id="job-a",
            workload_class="short_wave",
            workload_strata=["single_target", "short_wave"],
            producer="candidate-producer",
            policy_role="candidate",
            policy_id="candidate-a",
            serial_overhead_seconds=3,
        )
        # Only the exactly attributed rollout lane is projected.  Analysis is
        # carried separately and cannot be silently charged to this producer.
        self.assertEqual(203.0, projection["projected_wall_seconds"])

    def test_duplicate_task_or_unknown_batch_is_rejected(self) -> None:
        with self.assertRaisesRegex(OptimizationThroughputProfileV1Error, "unique"):
            build_throughput_profile_v1([task("same"), task("same")], [batch()])
        wrong = task("wrong")
        wrong["batch_id"] = "absent"
        with self.assertRaisesRegex(
            OptimizationThroughputProfileV1Error, "observed batch"
        ):
            build_throughput_profile_v1([wrong], [batch()])

    def test_invalid_complete_rollout_cannot_claim_zero_simulated_time(self) -> None:
        row = task("zero")
        row["simulated_combat_seconds"] = 0.0
        with self.assertRaisesRegex(
            OptimizationThroughputProfileV1Error, "positive simulated"
        ):
            build_throughput_profile_v1([row], [batch()])

    def test_validity_counts_must_partition_attempts(self) -> None:
        row = task("bad-counts", attempted=4, valid=3)
        with self.assertRaisesRegex(
            OptimizationThroughputProfileV1Error, "partition"
        ):
            build_throughput_profile_v1([row], [batch()])

    def test_baselines_are_counted_once_per_paired_group(self) -> None:
        budget = build_search_workload_budget_v1(
            [
                {
                    "stage_id": "s1",
                    "candidate_count": 64,
                    "scenario_count": 42,
                    "seed_count": 16,
                },
                {
                    "stage_id": "s2",
                    "candidate_count": 16,
                    "scenario_count": 112,
                    "seed_count": 32,
                },
                {
                    "stage_id": "s3",
                    "candidate_count": 4,
                    "scenario_count": 343,
                    "seed_count": 64,
                },
                {
                    "stage_id": "selection",
                    "candidate_count": 2,
                    "scenario_count": 343,
                    "seed_count": 256,
                },
            ],
            baseline_count=2,
        )
        self.assertEqual(363_776, budget["candidate_rollout_count"])
        self.assertEqual(228_032, budget["baseline_rollout_count"])
        self.assertEqual(591_808, budget["total_rollout_count"])
        self.assertTrue(
            all(row["baseline_reused_across_candidates"] for row in budget["stages"])
        )

    def test_projection_refuses_profile_without_valid_rollout(self) -> None:
        row = task("failed", status="FAILED", simulated=0.0)
        profile = build_throughput_profile_v1([row], [batch(shared=True)])
        with self.assertRaisesRegex(
            OptimizationThroughputProfileV1Error, "no complete-valid"
        ):
            project_wall_time_v1(
                profile,
                rollout_count=1,
                job_window_id="job-a",
                workload_class="short_wave",
                workload_strata=["single_target", "short_wave"],
                producer="candidate-producer",
                policy_role="candidate",
            )


if __name__ == "__main__":
    unittest.main()
