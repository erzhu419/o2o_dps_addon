from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.cat2new_fury_screening_v1 import (
    FROZEN_SCREENING_ARMS_V1,
    SUMMARY_SCHEMA,
    Cat2NewFuryScreeningV1Error,
    build_screening_plans_v1,
    summarize_screening_reductions_v1,
)
from o2o_dps.fury_multiseed_hpc_reducer_v3 import REDUCTION_SCHEMA_V3
from o2o_dps.fury_multiseed_hpc_worker_v3 import (
    _bind_cat2_factory_to_plan_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    validate_runner_plan,
)
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan


def _stats(count: int) -> dict[str, int | float]:
    return {
        "rollout_count": count,
        "damage_sum": 1.0,
        "elapsed_ms_sum": count,
        "dps_sum": 1.0,
        "dps_squared_sum": 1.0,
        "completion_count": count,
        "offline_score_eligible_count": count,
        "omitted_lane_count_sum": 0,
        "fatal_error_count_sum": 0,
    }


def _reduction(delta_sum: float, *, candidate_count: int = 256) -> dict[str, object]:
    return {
        "schema": REDUCTION_SCHEMA_V3,
        "status": "COMPLETE_DIAGNOSTIC_SIMULATOR_ONLY_NONVOTING",
        "policy_sufficient_statistics": {
            CAT2NEW_POLICY_ID: _stats(candidate_count),
            CAT_POLICY_ID: _stats(256),
        },
        "paired_sufficient_statistics": {
            "candidate_minus_cat": {
                "pair_count": 256,
                "dps_delta_sum": delta_sum,
                "dps_delta_squared_sum": delta_sum * delta_sum,
                "both_offline_score_eligible_count": 256,
            }
        },
        "dynamic_runtime_receipts_complete": True,
        "formal_comparison_status": "BLOCKED",
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


class Cat2NewFuryScreeningV1Tests(unittest.TestCase):
    def test_registry_is_exact_frozen_two_by_two_by_two(self) -> None:
        self.assertEqual(8, len(FROZEN_SCREENING_ARMS_V1))
        self.assertEqual(8, len({row.arm_id for row in FROZEN_SCREENING_ARMS_V1}))
        observed = {
            (
                row.single_target_priority,
                row.filler,
                row.two_hand_slam_mode,
            )
            for row in FROZEN_SCREENING_ARMS_V1
        }
        expected = {
            (priority, filler, slam)
            for priority in ("BLOODTHIRST_FIRST", "WHIRLWIND_FIRST")
            for filler in ("WAIT", "HAMSTRING")
            for slam in ("DISABLED", "CAT_TIMING")
        }
        self.assertEqual(expected, observed)
        self.assertTrue(
            all(row.factory_path.startswith("o2o_dps.") for row in FROZEN_SCREENING_ARMS_V1)
        )

    def test_rebuilds_eight_independent_content_bound_plans_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge"
            bridge.write_bytes(b"screening-bridge-v1")
            bundle = build_screening_plans_v1(
                _plan(tuple(range(1, 257))),
                bridge_path=bridge,
                bridge_platform="linux-amd64",
                bridge_build_id="screening-test-build",
                workers_per_node=8,
            )
        self.assertEqual(8, bundle["arm_count"])
        self.assertFalse(bundle["scientific_result_available"])
        plan_shas = set()
        profile_shas = set()
        closure_shas = set()
        expected_source_sha = hashlib.sha256(
            (
                Path(__file__).resolve().parents[1]
                / "o2o_dps"
                / "cat2new_fury_parametric_policy_v1.py"
            ).read_bytes()
        ).hexdigest()
        factory_module = importlib.import_module(
            "o2o_dps.cat2new_fury_parametric_policy_v1"
        )
        for row in bundle["arms"]:
            plan = validate_runner_plan(row["runner_plan"])
            dispatch = row["dispatch_plan"]
            plan_shas.add(plan["plan_sha256"])
            profile_shas.add(row["candidate_policy_identity"]["profile_sha256"])
            closure_shas.add(
                row["execution_bundle_identity"]["python_source_closure_sha256"]
            )
            self.assertEqual(plan["plan_sha256"], dispatch["runner_plan_sha256"])
            self.assertEqual(256, dispatch["master_seed_count"])
            self.assertEqual(1024, dispatch["expected_rollout_count"])
            self.assertEqual(
                expected_source_sha,
                row["candidate_policy_identity"]["source_sha256"],
            )
            factory_name = row["arm_spec"]["factory_path"].rsplit(":", 1)[1]
            bound_config = _bind_cat2_factory_to_plan_v3(
                getattr(factory_module, factory_name)(), plan
            )
            self.assertEqual(row["factory_config"], bound_config)
            self.assertEqual(
                row["arm_spec"]["factory_path"],
                "o2o_dps.cat2new_fury_parametric_policy_v1:"
                + factory_name,
            )
        self.assertEqual(8, len(plan_shas))
        self.assertEqual(8, len(profile_shas))
        self.assertEqual(1, len(closure_shas))

    def test_summary_ranks_only_fully_eligible_arms_by_paired_mean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths: dict[str, Path] = {}
            for index, arm in enumerate(FROZEN_SCREENING_ARMS_V1):
                value = _reduction(float(index * 256))
                if index == 3:
                    value = _reduction(float(index * 256), candidate_count=255)
                path = root / f"{arm.arm_id}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                paths[arm.arm_id] = path
            summary = summarize_screening_reductions_v1(paths)
        self.assertEqual(SUMMARY_SCHEMA, summary["schema"])
        self.assertEqual(7, summary["ranked_arm_count"])
        self.assertEqual(
            FROZEN_SCREENING_ARMS_V1[-1].arm_id,
            summary["ranking"][0]["arm_id"],
        )
        self.assertEqual(7.0, summary["ranking"][0]["candidate_minus_cat_mean_dps"])
        self.assertEqual(
            [FROZEN_SCREENING_ARMS_V1[3].arm_id],
            [row["arm_id"] for row in summary["excluded_arms"]],
        )
        self.assertFalse(summary["comparison_ready"])
        self.assertFalse(summary["scientific_result_available"])

    def test_summary_requires_exactly_eight_frozen_ids(self) -> None:
        with self.assertRaisesRegex(Cat2NewFuryScreeningV1Error, "exactly"):
            summarize_screening_reductions_v1({})


if __name__ == "__main__":
    unittest.main()
