from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_fury_source_bound_dynamic_config_v1 as dynamic_v1
from o2o_dps import historical_fury_source_bound_dynamic_hypothesis_v2 as hypothesis_v2
from o2o_dps.fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX


class HistoricalFurySourceBoundDynamicHypothesisV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not hypothesis_v2.DEFAULT_EVIDENCE_MANIFEST.is_file():
            raise unittest.SkipTest("frozen source-bound v1 evidence is unavailable")
        cls.artifact = (
            hypothesis_v2.build_historical_fury_source_bound_dynamic_hypothesis_v2()
        )

    def test_frozen_inputs_admit_only_a450_wire_smoke_pair(self) -> None:
        artifact = self.artifact
        self.assertEqual(hypothesis_v2.SCHEMA, artifact["schema"])
        self.assertEqual(
            {
                "request_count": 9,
                "ready_for_local_native_wire_smoke_only_count": 1,
                "blocked_request_count": 8,
                "materialized_pair_count": 1,
                "future_target_registry_leak_blocked_count": 8,
                "zero_safe_execution_horizon_blocked_count": 8,
                "missing_valid_health_proxy_blocked_count": 5,
                "compiled_background_event_count": 1017,
                "blocker_counts": {
                    "FUTURE_TARGET_REGISTRY_LEAK": 8,
                    "MISSING_ZERO_HEALING_ORIGIN_KILL_BUDGET_HP": 5,
                    "ZERO_SAFE_EXECUTION_HORIZON": 8,
                },
                "ready_segment_refs": [hypothesis_v2.READY_SEGMENT_REF],
            },
            artifact["summary"],
        )
        self.assertFalse(artifact["value_ready"])
        for field in (
            "policy_value_authorized",
            "comparison_authorized",
            "training_authorized",
            "hpc_authorized",
            "deployment_authorized",
            "superiority_claim_authorized",
        ):
            self.assertFalse(artifact[field])

        ready = next(
            row for row in artifact["requests"] if row["status"] == hypothesis_v2.READY_STATUS
        )
        self.assertEqual(hypothesis_v2.READY_SEGMENT_REF, ready["segment_ref"])
        self.assertEqual([], ready["blockers"])
        self.assertEqual(
            "stable_repeat_player_727acf47884bdbda",
            ready["prototype_contract"]["bindings"][0]["prototype_id"],
        )
        self.assertEqual(
            "3321958692537367122",
            ready["seed_contract"]["derived_request_random_seed"],
        )
        self.assertEqual(
            "0xF13000EA57276C04",
            ready["hypotheses"]["target_registry"][
                "candidate_origin_target_guid"
            ],
        )
        self.assertEqual(
            4582851.0, ready["hypotheses"]["target_health"]["value"]
        )
        self.assertEqual(
            1104.0, ready["hypotheses"]["exogenous_armor"]["base_armor"]
        )
        self.assertEqual(
            20001,
            ready["hypotheses"]["execution_window"]["selected_horizon_ms"],
        )
        self.assertEqual(
            1017,
            ready["hypotheses"]["team_kill_clock"][
                "compiled_background_event_count"
            ],
        )
        self.assertEqual(
            611859.0,
            ready["hypotheses"]["team_kill_clock"][
                "compiled_background_damage"
            ],
        )
        self.assertTrue(ready["observation_leak"]["present_in_materialized_pair"])
        self.assertFalse(ready["value_ready"])

    def test_pair_load_select_and_v1_predecessor_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stable, addressed, published = (
                hypothesis_v2.publish_historical_fury_source_bound_dynamic_hypothesis_v2(
                    output_directory=Path(temporary)
                )
            )
            loaded = hypothesis_v2.load_historical_fury_source_bound_dynamic_hypothesis_v2(
                stable
            )
            self.assertEqual(published, loaded)
            self.assertTrue(addressed.is_file())
            ready = next(
                row
                for row in loaded["requests"]
                if row["status"] == hypothesis_v2.READY_STATUS
            )
            request, config = hypothesis_v2.select_local_native_wire_smoke_pair_v2(
                loaded,
                segment_ref=ready["segment_ref"],
                base_request_sha256=ready["base_request_sha256"],
            )

        target = request["encounter"]["targets"][0]
        self.assertTrue(request["encounter"]["useHealth"])
        self.assertEqual(20.001, request["encounter"]["duration"])
        self.assertEqual(config.target_health[0].health, target["stats"][HEALTH_STAT_INDEX])
        self.assertEqual(1104.0, target["stats"][ARMOR_STAT_INDEX])
        self.assertEqual(20001, config.idle_advance_horizon_ms)
        self.assertEqual(1017, len(config.background_damage_events))
        self.assertEqual(0, len(config.effective_armor_events))

        predecessor = dynamic_v1.load_historical_fury_source_bound_dynamic_config_v1()
        self.assertEqual(0, predecessor["summary"]["ready_request_count"])
        self.assertTrue(
            all(
                row["derived_request_template"] is None
                and row["dynamic_load_config"] is None
                for row in predecessor["requests"]
            )
        )

    def test_all_blocked_rows_publish_assumptions_but_no_pair(self) -> None:
        blocked = [
            row
            for row in self.artifact["requests"]
            if row["status"] == hypothesis_v2.BLOCKED_STATUS
        ]
        self.assertEqual(8, len(blocked))
        expected_hypotheses = {
            "execution_window",
            "target_registry",
            "target_health",
            "exogenous_armor",
            "attackability",
            "team_kill_clock",
        }
        for row in blocked:
            codes = {blocker["code"] for blocker in row["blockers"]}
            self.assertIn("FUTURE_TARGET_REGISTRY_LEAK", codes)
            self.assertIn("ZERO_SAFE_EXECUTION_HORIZON", codes)
            self.assertEqual(expected_hypotheses, set(row["hypotheses"]))
            self.assertEqual(
                0,
                row["hypotheses"]["execution_window"]["selected_horizon_ms"],
            )
            self.assertIsNone(row["derived_request_template"])
            self.assertIsNone(row["dynamic_load_config"])
            self.assertIsNone(row["pair_binding"])
            self.assertFalse(row["local_native_wire_smoke_authorized"])

    def test_readdressed_policy_or_pair_tampering_is_rejected(self) -> None:
        promoted = deepcopy(self.artifact)
        promoted["training_authorized"] = True
        promoted = hypothesis_v2._content_addressed(promoted)
        with self.assertRaisesRegex(
            hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error,
            "local-smoke-only boundary",
        ):
            hypothesis_v2.validate_historical_fury_source_bound_dynamic_hypothesis_v2(
                promoted
            )

        mismatched = deepcopy(self.artifact)
        ready = next(
            row
            for row in mismatched["requests"]
            if row["status"] == hypothesis_v2.READY_STATUS
        )
        ready["derived_request_template"]["encounter"]["targets"][0]["stats"][
            HEALTH_STAT_INDEX
        ] += 1
        ready["derived_request_template_sha256"] = hypothesis_v2._sha256(
            ready["derived_request_template"]
        )
        mismatched = hypothesis_v2._content_addressed(mismatched)
        with self.assertRaisesRegex(
            hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error,
            "target or background binding",
        ):
            hypothesis_v2.validate_historical_fury_source_bound_dynamic_hypothesis_v2(
                mismatched
            )

    def test_plain_content_tamper_is_rejected(self) -> None:
        tampered = json.loads(json.dumps(self.artifact))
        tampered["summary"]["blocked_request_count"] = 7
        with self.assertRaisesRegex(
            hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error,
            "content address differs",
        ):
            hypothesis_v2.validate_historical_fury_source_bound_dynamic_hypothesis_v2(
                tampered
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
