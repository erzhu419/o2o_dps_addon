from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from o2o_dps import historical_fury_source_bound_dynamic_hypothesis_v2 as hypothesis_v2
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v1 as smoke_v1
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v2 as smoke_v2


SEGMENT_REF = hypothesis_v2.READY_SEGMENT_REF
PROTOTYPE_ID = "stable_repeat_player_727acf47884bdbda"
SEED = 3321958692537367122


class HistoricalFurySourceBoundPrototypeSmokeRunnerV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prepared = smoke_v2.prepare_source_bound_prototype_wire_smoke_v2(
            bundle_path=smoke_v2.DEFAULT_BUNDLE_MANIFEST,
            model_manifest_path=smoke_v2.DEFAULT_MODEL_MANIFEST,
            dynamic_hypothesis_path=smoke_v2.DEFAULT_DYNAMIC_HYPOTHESIS,
            segment_ref=SEGMENT_REF,
            prototype_id=PROTOTYPE_ID,
            seed=SEED,
        )

    def test_real_ready_pair_is_bound_without_promoting_value_authority(self) -> None:
        prepared = self.prepared
        self.assertEqual(hypothesis_v2.READY_STATUS, prepared.hypothesis_row["status"])
        self.assertFalse(prepared.hypothesis_row["value_ready"])
        self.assertTrue(
            prepared.hypothesis_row["observation_leak"][
                "present_in_materialized_pair"
            ]
        )
        self.assertEqual(20.001, prepared.prepared_v1.raid_sim_request["encounter"]["duration"])
        self.assertTrue(prepared.prepared_v1.raid_sim_request["encounter"]["useHealth"])
        self.assertEqual(
            4_582_851,
            prepared.prepared_v1.raid_sim_request["encounter"]["targets"][0][
                "stats"
            ][34],
        )
        self.assertEqual(1_104, prepared.prepared_v1.raid_sim_request["encounter"]["targets"][0]["stats"][26])
        self.assertEqual(1_017, len(prepared.prepared_v1.dynamic_config.background_damage_events))
        self.assertEqual([], list(prepared.prepared_v1.dynamic_config.effective_armor_events))
        self.assertEqual("READY", prepared.prepared_v1.dynamic_preparation_row["status"])
        self.assertFalse(prepared.v1_execution_projection["on_disk_v1_artifact_created"])
        self.assertFalse(prepared.v1_execution_projection["immutable_v1_predecessor_mutated"])

    def test_non_ready_segment_fails_before_native_execution(self) -> None:
        blocked = "sha256:0f0db3fc198af2cf572d2a1432f7dfb39f781290108754d25fcb09a14efab905"
        with self.assertRaisesRegex(
            smoke_v2.HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error,
            "exactly one wire-smoke-ready row|selected hypothesis row",
        ):
            smoke_v2.prepare_source_bound_prototype_wire_smoke_v2(
                bundle_path=smoke_v2.DEFAULT_BUNDLE_MANIFEST,
                model_manifest_path=smoke_v2.DEFAULT_MODEL_MANIFEST,
                dynamic_hypothesis_path=smoke_v2.DEFAULT_DYNAMIC_HYPOTHESIS,
                segment_ref=blocked,
                prototype_id="player_equal_q4_pooled",
                seed=SEED,
            )

    def test_horizon_survival_is_a_complete_wire_smoke_when_receipts_close(self) -> None:
        inner = {
            "rollout": {
                "bridge_runtime_evidence": {
                    "runtime_kind": "NATIVE_SUBPROCESS_BRIDGE",
                    "native_subprocess_bridge": True,
                    "python_simulated_bridge": False,
                    "load_method_invoked": "load_dynamic_v3",
                },
                "configured_completion": {
                    "criterion_met": True,
                    "terminal_reason": "SCENARIO_HORIZON_REACHED",
                },
                "scenario_complete": True,
            },
            "execution_scope": {
                "local_windows_native": True,
                "hpc_dispatch_performed": False,
            },
            "damage_lifecycle_closure": {
                "runtime_receipt_status": "COMPLETE_BOUND",
                "damage_accounting_closed": True,
            },
        }
        closure = smoke_v2._wire_smoke_closure(self.prepared, inner)
        self.assertTrue(closure["horizon_reached_without_all_targets_dead"])
        self.assertFalse(closure["all_targets_dead_required"])
        self.assertTrue(closure["wire_smoke_complete"])

        incomplete = deepcopy(inner)
        incomplete["damage_lifecycle_closure"]["runtime_receipt_status"] = "INCOMPLETE"
        self.assertFalse(
            smoke_v2._wire_smoke_closure(self.prepared, incomplete)[
                "wire_smoke_complete"
            ]
        )

    def test_runner_delegates_to_the_single_existing_native_tail(self) -> None:
        prepared = self.prepared
        inner = {"inner": "receipt"}
        built = {"built": "receipt"}
        checked = {"checked": "receipt"}
        with (
            patch.object(
                smoke_v2,
                "prepare_source_bound_prototype_wire_smoke_v2",
                return_value=prepared,
            ),
            patch.object(
                smoke_v1,
                "run_prepared_local_native_source_bound_prototype_smoke_v1",
                return_value=inner,
            ) as run_tail,
            patch.object(smoke_v2, "_build_receipt", return_value=built) as build,
            patch.object(
                smoke_v2,
                "validate_source_bound_prototype_wire_smoke_receipt_v2",
                return_value=checked,
            ) as validate,
        ):
            observed = smoke_v2.run_local_native_source_bound_prototype_wire_smoke_v2(
                bundle_path="bundle.json",
                model_manifest_path="model.json",
                dynamic_hypothesis_path="hypothesis.json",
                segment_ref=SEGMENT_REF,
                prototype_id=PROTOTYPE_ID,
                seed=SEED,
                bridge_path="bridge.exe",
                simulator_root="sim-root",
                max_decisions=123,
                max_advances=456,
            )

        self.assertIs(checked, observed)
        run_tail.assert_called_once_with(
            prepared.prepared_v1,
            bridge_path="bridge.exe",
            simulator_root="sim-root",
            max_decisions=123,
            max_advances=456,
        )
        build.assert_called_once_with(prepared, inner_receipt=inner)
        validate.assert_called_once_with(built, prepared=prepared)


if __name__ == "__main__":
    unittest.main()
