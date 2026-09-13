from __future__ import annotations

import unittest

from o2o_dps.development_wave_action_gap_diagnostic_v1 import (
    project_action_artifact_v1,
)


class DevelopmentWaveActionGapDiagnosticV1Test(unittest.TestCase):
    def test_projection_counts_only_accepted_sinks_and_typed_results(self) -> None:
        artifact = {
            "steps": [{
                "simulator_state_before": {
                    "time_ms": 100, "power": {"current": 35},
                    "mh_swing_remaining_ms": 1200,
                },
                "ordered_execution": {"sink_events": [
                    {
                        "simulator_acceptance": {"status": "ACCEPTED"},
                        "source_sink": {"channel": "gcd", "operation": "CastSpellByName"},
                        "operation_contract": {"canonical_action": "warrior.bloodthirst"},
                    },
                    {
                        "simulator_acceptance": {"status": "REJECTED"},
                        "source_sink": {"channel": "swing_queue", "operation": "QueueSpellByName"},
                    },
                ]},
                "server_observation_after_advance": {
                    "result_stream": {"events": [{
                        "action": {"spell_id": 23894}, "outcome": "HIT",
                    }]},
                },
            }],
            "final_state": {"power": {"current": 5}},
            "interventions": [{
                "decision_index": 1, "combat_elapsed_s": 2.0, "rage": 35,
                "cat_reserve_rage": 40, "candidate_reserve_rage": 30,
            }],
        }
        projection = project_action_artifact_v1(artifact)
        self.assertEqual(projection["accepted_counts"], {"gcd:warrior.bloodthirst": 1})
        self.assertEqual(projection["typed_result_counts"], {"23894:HIT": 1})
        self.assertEqual(projection["final_rage"], 5)
        self.assertEqual(projection["interventions"][0]["cat_reserve_rage"], 40)


if __name__ == "__main__":
    unittest.main()
