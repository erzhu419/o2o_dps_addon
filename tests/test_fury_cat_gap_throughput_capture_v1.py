from __future__ import annotations

import unittest

from o2o_dps.fury_cat_gap_throughput_capture_v1 import (
    FuryCatGapThroughputCaptureV1Error,
    ShardThroughputCaptureV1,
    build_job_window_profile_input_v1,
    combine_capture_documents_v1,
)
from o2o_dps.optimization_throughput_profile_v1 import (
    OptimizationThroughputProfileV1Error,
    build_throughput_profile_v1,
    project_wall_time_v1,
)


def lane(*, complete: bool, eligible: bool, fatal: int = 0) -> dict:
    return {
        "completion_criterion_met": complete,
        "offline_score_eligible": eligible,
        "fatal_error_count": fatal,
        "elapsed_ms": 2_000,
        "artifact": {
            "steps": [
                {
                    "ordered_execution": {
                        "sink_events": [{"kind": "gcd"}, {"kind": "queue"}],
                        "wait_event": None,
                    }
                },
                {
                    "ordered_execution": {
                        "sink_events": [],
                        "wait_event": {"kind": "wait"},
                    }
                },
            ]
        },
    }


class FuryCatGapThroughputCaptureV1Tests(unittest.TestCase):
    def _homogeneous_capture(self, node: str, shard: int, cpu_work: int = 50) -> dict:
        capture = ShardThroughputCaptureV1(
            batch_id=f"stage1:{node}:shard-{shard:05d}",
            node=node,
            shard_index=shard,
            workload_class="screening",
            policy_id="candidate-a",
            policy_role="CANDIDATE",
            producer="candidate-producer",
            workload_strata=("single_target", "short_wave"),
        )
        capture.begin_rollout()
        with capture.phase("ROLLOUT"):
            sum(range(cpu_work))
        capture.finish_rollout(lane(complete=True, eligible=True))
        return capture.finish()

    def test_capture_partitions_rollouts_and_builds_profile_input(self) -> None:
        capture = ShardThroughputCaptureV1(
            batch_id="stage1:node001:shard-00000",
            node="node001",
            shard_index=0,
            workload_class="screening",
            input_bytes=123,
        )
        for result in (
            lane(complete=True, eligible=True),
            lane(complete=False, eligible=False),
            lane(complete=False, eligible=False, fatal=1),
        ):
            capture.begin_rollout()
            with capture.phase("ROLLOUT"):
                sum(range(50))
            with capture.phase("VALIDATION"):
                pass
            capture.finish_rollout(result)
        with capture.phase("SERIALIZATION"):
            pass
        capture.add_serialization_bytes(json_bytes=500)
        capture.set_output_bytes(compressed_bytes=100, output_bytes=180)
        document = capture.finish()
        profile = build_throughput_profile_v1(
            document["tasks"], document["batches"]
        )
        rollout = profile["by_task_kind"]["ROLLOUT"]
        self.assertEqual(3, rollout["attempted_rollout_count"])
        self.assertEqual(1, rollout["complete_valid_rollout_count"])
        self.assertEqual(1, rollout["incomplete_rollout_count"])
        self.assertEqual(1, rollout["failed_rollout_count"])
        self.assertEqual(2.0, rollout["simulated_combat_seconds"])
        self.assertEqual(9, rollout["event_count"])
        self.assertEqual(6, rollout["state_interaction_count"])
        serialization = profile["by_task_kind"]["SERIALIZATION"]
        self.assertEqual(500, serialization["json_bytes"])
        self.assertEqual(100, serialization["compressed_bytes"])
        self.assertEqual(180, serialization["output_bytes"])
        self.assertFalse(document["receipt_protocol_modified"])

    def test_unfinished_attempt_is_failed_and_missing_counters_are_null(self) -> None:
        capture = ShardThroughputCaptureV1(
            batch_id="stage1:node001:shard-00001",
            node="node001",
            shard_index=1,
            workload_class="screening",
        )
        capture.begin_rollout()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with capture.phase("ROLLOUT"):
                raise RuntimeError("boom")
        document = capture.finish()
        profile = build_throughput_profile_v1(
            document["tasks"], document["batches"]
        )
        rollout = profile["by_task_kind"]["ROLLOUT"]
        self.assertEqual(1, rollout["failed_rollout_count"])
        self.assertIsNone(rollout["event_count"])
        self.assertIsNone(rollout["state_interaction_count"])
        self.assertEqual(0, rollout["event_count_observed_task_count"])

    def test_unpublished_shard_excludes_prior_valid_rollouts(self) -> None:
        capture = ShardThroughputCaptureV1(
            batch_id="stage1:node001:shard-00002",
            node="node001",
            shard_index=2,
            workload_class="screening",
        )
        capture.begin_rollout()
        with capture.phase("ROLLOUT"):
            pass
        capture.finish_rollout(lane(complete=True, eligible=True))
        capture.mark_shard_failed()
        profile_input = capture.finish()
        profile = build_throughput_profile_v1(
            profile_input["tasks"], profile_input["batches"]
        )
        rollout = profile["by_task_kind"]["ROLLOUT"]
        self.assertEqual(0, rollout["complete_valid_rollout_count"])
        self.assertEqual(1, rollout["failed_rollout_count"])
        self.assertEqual(0.0, rollout["simulated_combat_seconds"])

    def test_nested_validation_is_excluded_from_analysis_phase(self) -> None:
        capture = ShardThroughputCaptureV1(
            batch_id="stage1:reducer",
            node="reducer",
            shard_index=0,
            workload_class="screening",
        )
        with capture.phase("ANALYSIS"):
            with capture.phase("VALIDATION"):
                sum(range(100))
            sum(range(100))
        with capture.phase("MERGE"):
            pass
        document = capture.finish()
        kinds = {row["task_kind"] for row in document["tasks"]}
        self.assertEqual({"VALIDATION", "ANALYSIS", "MERGE"}, kinds)
        profile = build_throughput_profile_v1(
            document["tasks"], document["batches"]
        )
        self.assertIn("VALIDATION", profile["by_task_kind"])
        self.assertIn("ANALYSIS", profile["by_task_kind"])
        self.assertIn("MERGE", profile["by_task_kind"])

    def test_small_capture_sidecars_combine_without_raw_shards(self) -> None:
        documents = []
        for index in range(2):
            capture = ShardThroughputCaptureV1(
                batch_id=f"stage1:node00{index + 1}:shard-{index:05d}",
                node=f"node00{index + 1}",
                shard_index=index,
                workload_class="screening",
            )
            capture.begin_rollout()
            with capture.phase("ROLLOUT"):
                pass
            capture.finish_rollout(lane(complete=True, eligible=True))
            documents.append(capture.finish())
        combined = combine_capture_documents_v1(documents)
        profile = build_throughput_profile_v1(
            combined["tasks"], combined["batches"]
        )
        self.assertEqual(2, profile["overall"]["complete_valid_rollout_count"])
        self.assertEqual(2, len(profile["batches"]))

    def test_shared_job_window_reports_node_and_cluster_utilization(self) -> None:
        documents = [
            self._homogeneous_capture("node001", 0),
            self._homogeneous_capture("node001", 1),
            self._homogeneous_capture("node002", 2),
        ]
        start = min(
            row["capture_window"]["started_at_epoch_seconds"] for row in documents
        ) - 1.0
        end = max(
            row["capture_window"]["ended_at_epoch_seconds"] for row in documents
        ) + 1.0
        joined = build_job_window_profile_input_v1(
            documents,
            {
                "job_id": "job-42",
                "started_at_epoch_seconds": start,
                "ended_at_epoch_seconds": end,
                "representative_workload_strata": [
                    ["single_target", "short_wave"]
                ],
                "nodes": [
                    {
                        "node": "node001",
                        "logical_cpu_capacity": 192,
                        "worker_count": 2,
                        "shard_indices": [0, 1],
                    },
                    {
                        "node": "node002",
                        "logical_cpu_capacity": 192,
                        "worker_count": 1,
                        "shard_indices": [2],
                    },
                ],
            },
        )
        profile = build_throughput_profile_v1(joined["tasks"], joined["batches"])
        self.assertEqual(2, len(profile["batches"]))
        self.assertEqual(1, len(profile["job_windows"]))
        cluster = profile["job_windows"][0]
        self.assertEqual(384, cluster["logical_cpu_capacity"])
        self.assertEqual(3, cluster["worker_count"])
        self.assertEqual(3, cluster["shard_count"])
        self.assertEqual("MEASURED_SHARED_JOB_WINDOW", cluster["utilization_status"])
        self.assertEqual(3, len(joined["per_shard"]))
        self.assertTrue(
            all(
                row["utilization_status"] == "MEASURED_SHARED_JOB_WINDOW"
                for row in profile["batches"]
            )
        )

    def test_mixed_shard_cpu_is_retained_but_cannot_be_projected(self) -> None:
        capture = ShardThroughputCaptureV1(
            batch_id="stage1:node001:shard-00000",
            node="node001",
            shard_index=0,
            workload_class="screening",
        )
        capture.begin_rollout()
        with capture.phase("ROLLOUT"):
            sum(range(50))
        capture.finish_rollout(lane(complete=True, eligible=True))
        document = capture.finish()
        start = document["capture_window"]["started_at_epoch_seconds"] - 1.0
        end = document["capture_window"]["ended_at_epoch_seconds"] + 1.0
        joined = build_job_window_profile_input_v1(
            [document],
            {
                "job_id": "mixed-job",
                "started_at_epoch_seconds": start,
                "ended_at_epoch_seconds": end,
                "representative_workload_strata": [
                    ["single_target", "short_wave"]
                ],
                "nodes": [
                    {
                        "node": "node001",
                        "logical_cpu_capacity": 192,
                        "worker_count": 1,
                        "shard_indices": [0],
                    }
                ],
            },
        )
        profile = build_throughput_profile_v1(joined["tasks"], joined["batches"])
        self.assertEqual(
            "UNAVAILABLE_IDENTITY_NOT_RECORDED",
            next(row for row in profile["tasks"] if row["task_kind"] == "ROLLOUT")[
                "rollout_cpu_attribution"
            ],
        )
        with self.assertRaisesRegex(
            OptimizationThroughputProfileV1Error, "exactly attributed"
        ):
            project_wall_time_v1(
                profile,
                rollout_count=100,
                job_window_id="mixed-job",
                workload_class="screening",
                workload_strata=["single_target", "short_wave"],
                producer="candidate-producer",
                policy_role="CANDIDATE",
            )

    def test_job_window_requires_complete_explicit_shard_set(self) -> None:
        document = self._homogeneous_capture("node001", 0)
        start = document["capture_window"]["started_at_epoch_seconds"] - 1.0
        end = document["capture_window"]["ended_at_epoch_seconds"] + 1.0
        with self.assertRaisesRegex(
            FuryCatGapThroughputCaptureV1Error, "missing shard capture"
        ):
            build_job_window_profile_input_v1(
                [document],
                {
                    "job_id": "incomplete-job",
                    "started_at_epoch_seconds": start,
                    "ended_at_epoch_seconds": end,
                    "representative_workload_strata": [],
                    "nodes": [
                        {
                            "node": "node001",
                            "logical_cpu_capacity": 192,
                            "worker_count": 2,
                            "shard_indices": [0, 1],
                        }
                    ],
                },
            )


if __name__ == "__main__":
    unittest.main()
