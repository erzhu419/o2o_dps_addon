from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.value_gap_attribution_v1 import diagnose_value_gap


def _fixture() -> tuple[list[dict], dict]:
    before = {
        "power": {"current": 80, "maximum": 100, "type": "rage"},
        "gcd_remaining_ms": 0,
        "mh_swing_remaining_ms": 700,
        "target_index": 0,
        "auras": [],
        "target_auras": [],
    }
    identity = {"group_id": "pair-7", "master_seed": 513, "simulator_seed": 99, "scenario_id": "h20"}
    cat_artifact = {
        "steps": [{
            "simulator_state_before": {**before, "time_ms": 100},
            "simulator_state_next_epoch": {"time_ms": 1600, "damage_done": 300},
            "proposal": {"swing_queue": "NONE"},
            "ordered_execution": {"sink_events": [{
                "source_sink": {"channel": "gcd"},
                "operation_contract": {"canonical_action": "warrior.battle_shout"},
                "simulator_acceptance": {"status": "ACCEPTED"},
            }]},
            "server_observation_after_advance": {"result_stream": {"events": [{
                "time_ms": 150, "action": {"spell_id": 1}, "outcome": "HIT", "damage": 30,
            }]}},
        }],
        "final_state": {"power": {"current": 20}},
    }
    candidate_artifact = {
        "decisions": [{
            "time_ms": 100,
            "carried_state_before": deepcopy(before),
            "v5_execution": {"ordered_events": [
                {"lane": "swing_queue", "intent": "HEROIC_STRIKE", "arguments": {},
                 "simulator_acceptance": {"status": "ACCEPTED"}},
                {"lane": "gcd", "intent": "CAST_ACTION",
                 "arguments": {"action_key": "warrior.battle_shout"},
                 "simulator_acceptance": {"status": "ACCEPTED"}},
            ]},
        }],
        "idle_advances": [{"after_time_ms": 1600, "state_after": {"state": {"damage_done": 290}}}],
        "lifecycle_receipt": {
            "final_state": {"state": {"power": {"current": 5}}},
            "terminal_dynamic_receipts": {"candidate_damage": {"receipts": [
                {"action": {"other_id": 7, "spell_id": 0}, "applied_damage": 100},
                {"action": {"other_id": 7, "spell_id": 0}, "applied_damage": 0},
                {"action": {"other_id": 0, "spell_id": 1680}, "applied_damage": 190},
            ]}},
        },
    }
    base = {
        "group_identity": identity,
        "dynamic_load_identity": {"simulator_seed": 99, "request_sha256": "same"},
        "lane_result": {
            "completion_mode": "SCENARIO_HORIZON_REACHED",
            "elapsed_ms": 1500,
            "dps": 200,
            "damage": 300,
        },
    }
    rows = [
        {**base, "policy_identity": {"policy_id": "cat.fury.profile1"},
         "lane_result": {**base["lane_result"], "artifact": cat_artifact}},
        {**base, "policy_identity": {"policy_id": "cat2new.fury.candidate"},
         "lane_result": {**base["lane_result"], "artifact": candidate_artifact,
                         "damage": 290, "dps": 193.3333333333}},
    ]
    compact = {
        "schema": "cat2new_fury_horizon_analysis/v2",
        "confirmation_id": "latest-v2",
        "selection_gate": {"status": "NO_SELECTION"},
        "contrast_results": [{
            "arm_id": "ww_wait_cat_timing", "baseline_policy_id": "cat.fury.profile1",
            "paired_n": 256, "candidate_minus_baseline_mean_dps": -2.910172784,
            "candidate_minus_baseline_standard_error_dps": 6.03,
            "wins": 115, "ties": 6, "losses": 135, "holm_adjusted_p": 1.0,
        }],
    }
    return rows, compact


class ValueGapAttributionTests(unittest.TestCase):
    def test_separates_aggregate_from_one_seed_and_no_counterfactual(self) -> None:
        rows, compact = _fixture()
        result = diagnose_value_gap(rows, compact)
        self.assertEqual(result["horizon_v2_aggregate"]["paired_contrast"]["paired_n"], 256)
        self.assertEqual(result["one_seed_group"]["master_seed"], 513)
        self.assertEqual(result["one_seed_group"]["first_accepted_control_difference"]["time_ms"], 100)
        self.assertTrue(result["one_seed_group"]["observed_before_first_difference"]["projected_fields_equal"])
        self.assertEqual(result["one_seed_group"]["candidate"]["white_attack_receipts"]["attempts"], 2)
        self.assertIsNone(result["one_seed_group"]["cat"]["white_attack_receipts"])
        self.assertEqual(result["same_prefix_counterfactual"]["status"], "NOT_EXECUTED")
        self.assertFalse(result["same_prefix_counterfactual"]["post_divergence_differences_are_causal_contributions"])

    def test_rejects_incomplete_trace(self) -> None:
        rows, compact = _fixture()
        rows[0]["lane_result"]["completion_mode"] = "INCOMPLETE"
        with self.assertRaisesRegex(ValueError, "two complete"):
            diagnose_value_gap(rows, compact)

    def test_rejects_wrong_aggregate_family(self) -> None:
        rows, compact = _fixture()
        compact["contrast_results"][0]["paired_n"] = 255
        with self.assertRaisesRegex(ValueError, "256 Cat pairs"):
            diagnose_value_gap(rows, compact)


if __name__ == "__main__":
    unittest.main()
