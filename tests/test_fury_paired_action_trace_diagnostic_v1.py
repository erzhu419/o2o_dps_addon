from __future__ import annotations

import unittest

from o2o_dps.fury_paired_action_trace_diagnostic_v1 import diagnose_group_rows


def _rows() -> list[dict]:
    identity = {"group_id": "paired-1", "simulator_seed": 7, "scenario_id": "dummy"}
    cat_step = {
        "simulator_state_before": {
            "time_ms": 100, "power": {"current": 60},
            "mh_swing_remaining_ms": 300, "gcd_remaining_ms": 0,
        },
        "simulator_state_next_epoch": {"time_ms": 1600, "damage_done": 321},
        "proposal": {"swing_queue": "KEEP"},
        "ordered_execution": {"sink_events": [{
            "source_sink": {"channel": "gcd"},
            "operation_contract": {"canonical_action": "warrior.bloodthirst"},
            "simulator_acceptance": {"status": "ACCEPTED"},
        }]},
        "server_observation_after_advance": {"result_stream": {"events": [{
            "time_ms": 100, "action": {"spell_id": 23881},
            "outcome": "HIT", "damage": 321,
        }]}},
    }
    candidate_decision = {
        "time_ms": 100,
        "carried_state_before": {
            "power": {"current": 60}, "mh_swing_remaining_ms": 300,
            "gcd_remaining_ms": 0,
        },
        "v5_execution": {"ordered_events": [
            {
                "lane": "swing_queue", "intent": "HEROIC_STRIKE",
                "arguments": {}, "simulator_acceptance": {"status": "ACCEPTED"},
            },
            {
                "lane": "gcd", "intent": "CAST_ACTION",
                "arguments": {"action_key": "warrior.bloodthirst"},
                "simulator_acceptance": {"status": "ACCEPTED"},
            },
        ]},
    }
    base = {
        "group_identity": identity,
        "dynamic_load_identity": {"request_sha256": "request", "simulator_seed": 7},
        "lane_result": {
            "damage": 321, "elapsed_ms": 1500, "dps": 214,
            "completion_mode": "SCENARIO_HORIZON_REACHED",
        },
    }
    cat = {
        **base, "policy_identity": {"policy_id": "cat.fury.profile1"},
        "lane_result": {**base["lane_result"], "artifact": {"steps": [cat_step]}},
    }
    candidate = {
        **base, "policy_identity": {"policy_id": "cat2new.fury.candidate"},
        "lane_result": {**base["lane_result"], "artifact": {
            "decisions": [candidate_decision],
            "idle_advances": [{
                "after_time_ms": 1600,
                "state_after": {"state": {"damage_done": 321}},
            }],
        }},
    }
    return [cat, candidate]


class PairedActionTraceDiagnosticTests(unittest.TestCase):
    def test_queue_divergence_does_not_falsely_attribute_damage(self) -> None:
        report = diagnose_group_rows(_rows())
        self.assertEqual(report["simulator_seed"], 7)
        self.assertEqual(report["cat"]["typed_result_events"][0]["damage"], 321)
        self.assertIsNone(report["candidate"]["typed_result_events"])
        self.assertEqual(report["candidate"]["damage_samples"], [
            {"time_ms": 1600, "cumulative_damage": 321}
        ])
        self.assertEqual(report["first_accepted_control_divergence"]["time_ms"], 100)
        self.assertIsNone(report["first_accepted_gcd_divergence"])
        self.assertIn("not a win/loss test", report["evidence_scope"])

    def test_rejects_unpaired_dynamic_load(self) -> None:
        rows = _rows()
        rows[1]["dynamic_load_identity"] = {"request_sha256": "other"}
        with self.assertRaisesRegex(ValueError, "dynamic loads differ"):
            diagnose_group_rows(rows)


if __name__ == "__main__":
    unittest.main()
