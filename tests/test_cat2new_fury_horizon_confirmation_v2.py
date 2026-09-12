from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from o2o_dps.cat2new_candidate_feedback_loop_v6 import IMPLEMENTATION_REVISION
from o2o_dps.cat2new_fury_horizon_confirmation_v2 import (
    BASELINE_POLICY_IDS,
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_HORIZON_MS,
    CONFIRMATION_MASTER_SEEDS,
    build_horizon_confirmation_v2,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import validate_runner_plan
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


class Cat2NewFuryHorizonConfirmationV2Tests(unittest.TestCase):
    def test_freezes_repaired_fresh_seed_dual_baseline_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge"
            bridge.write_bytes(b"long-horizon-v2-test")
            bundle = build_horizon_confirmation_v2(
                _source_plan(tuple(range(1, 257))),
                bridge_path=bridge,
                bridge_platform="linux-amd64",
                bridge_build_id="long-horizon-v2-test",
                workers_per_node=8,
                project_root=Path(__file__).resolve().parents[1],
            )

        self.assertEqual(list(range(513, 769)), list(CONFIRMATION_MASTER_SEEDS))
        self.assertEqual(list(CONFIRMATION_ARM_IDS), [
            row["arm_spec"]["arm_id"] for row in bundle["arms"]
        ])
        lock = bundle["input_lock"]
        self.assertEqual(IMPLEMENTATION_REVISION, lock["candidate_implementation_revision"])
        self.assertFalse(
            lock["repair_lineage"]["prior_result_usable_for_policy_comparison"]
        )
        self.assertTrue(lock["repair_lineage"]["fresh_seed_family_required"])
        analysis = bundle["analysis_contract"]
        self.assertEqual(list(BASELINE_POLICY_IDS), analysis["baseline_policy_ids"])
        self.assertEqual(6, analysis["multiplicity_family_size"])
        self.assertEqual(
            "NO_SELECTION", analysis["no_passing_arm_result"]
        )
        self.assertFalse(analysis["same_seed_extension_or_retuning_allowed"])
        self.assertEqual(3, bundle["arm_count"])
        self.assertEqual(256, bundle["master_seed_count"])
        self.assertFalse(bundle["deployment_allowed"])

        source_closures = set()
        for row in bundle["arms"]:
            plan = validate_runner_plan(row["runner_plan"])
            self.assertEqual(
                list(CONFIRMATION_MASTER_SEEDS),
                plan["contract"]["seed_derivation"]["master_seeds"],
            )
            self.assertEqual(
                "cat2new-fury-horizon-confirmation-v2-right-censor",
                plan["contract"]["seed_derivation"]["namespace"],
            )
            self.assertEqual(
                CONFIRMATION_HORIZON_MS,
                plan["contract"]["scenarios"][0]["horizon_ms"],
            )
            self.assertEqual(256, plan["contract"]["group_count"])
            self.assertEqual([8] * 6, [
                node["workers"] for node in row["dispatch_plan"]["nodes"]
            ])
            source_closures.add(
                row["execution_bundle_identity"]["python_source_closure_sha256"]
            )
        self.assertEqual(1, len(source_closures))


if __name__ == "__main__":
    unittest.main()
