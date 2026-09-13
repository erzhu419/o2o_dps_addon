from __future__ import annotations

import unittest

from o2o_dps.development_wave_case_v1 import (
    INSTANCE_ID,
    TARGET_GUID,
    TARGET_HP_HYPOTHESIS,
    WATCHDOG_MS,
    adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
    build_development_wave_scenario_v1,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import (
    validate_dynamic_load_request_v3,
)
from o2o_dps.fury_dynamic_v5_baseline_adapter_v4 import (
    target_contexts_from_runner_v4,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import normalize_runner_scenarios


class DevelopmentWaveCaseV1Tests(unittest.TestCase):
    def test_controlled_reset_load_is_complete_wave_config(self) -> None:
        case = build_development_wave_case_v1(17)
        spec = case.case_spec
        self.assertEqual("MODEL_DEFINED_DEVELOPMENT", spec["scope"])
        self.assertEqual("Upper Tower of Karazhan", spec["source_instance_name"])
        self.assertEqual(INSTANCE_ID, spec["source_instance_id"])
        self.assertEqual([TARGET_GUID], spec["required_target_ids"])
        self.assertFalse(spec["historical_exact"])
        self.assertEqual("CONTROLLED_LIVE_PROFILE_NOT_SOURCE_PLAYER", spec["build_role"])
        self.assertEqual(
            "MODEL_HP_FROM_COMPLETE_NORMALIZED_KILL_BUDGET_NOT_EXACT_MAX_HP",
            spec["source_evidence"]["hp_interpretation"],
        )
        self.assertEqual(TARGET_HP_HYPOTHESIS, case.request["encounter"]["targets"][0]["stats"][34])
        self.assertEqual(50, case.request["raid"]["parties"][0]["players"][0]["warrior"]["options"]["startingRage"])
        self.assertEqual(WATCHDOG_MS, case.dynamic_load.config.idle_advance_horizon_ms)
        self.assertEqual(60, len(case.dynamic_load.config.background_damage_events))
        self.assertEqual((), case.dynamic_load.config.effective_armor_events)
        self.assertEqual((), case.dynamic_load.config.attackability_events)
        validate_dynamic_load_request_v3(case.dynamic_load, case.request)
        scenario = build_development_wave_scenario_v1(17)
        self.assertEqual(case.request, scenario["request"])
        self.assertEqual(case.dynamic_load.config.to_wire(), scenario["dynamic_load_config"])
        self.assertEqual(case.target_contexts, target_contexts_from_runner_v4(scenario["target_context_bundle"]))
        normalized = normalize_runner_scenarios([scenario])[0]
        self.assertEqual(case.dynamic_load.request_sha256, normalized["request_sha256"])

    def test_only_dead_target_receipt_plus_end_state_complete(self) -> None:
        case = build_development_wave_case_v1(17)
        rollout = {
            "configured_completion": {
                "terminal_reason": "ALL_TARGETS_DEAD",
                "all_targets_dead": True,
                "criterion_met": True,
            },
            "scenario_complete": True,
            "elapsed_ms": 8100,
            "damage_delta": 3010.0,
            "final_state": {
                "dynamic_team_background": {
                    "simulated_damage_applied": 3010.0,
                    "background_damage_applied": 123_387.0,
                    "targets": [{"target_index": 0, "dead": True}],
                }
            },
        }
        judged = adjudicate_development_wave_completion_v1(case, rollout)
        self.assertEqual("COMPLETED", judged["status"])
        self.assertTrue(judged["required_targets_dead"])
        self.assertEqual(3010.0, judged["own_effective_damage"])

        rollout["final_state"]["dynamic_team_background"]["targets"][0]["dead"] = False
        self.assertEqual(
            "FAILED_TERMINAL_INCONSISTENT_OR_INCOMPLETE",
            adjudicate_development_wave_completion_v1(case, rollout)["status"],
        )

    def test_horizon_is_censored_even_if_executor_says_complete(self) -> None:
        case = build_development_wave_case_v1(17)
        rollout = {
            "configured_completion": {
                "terminal_reason": "SCENARIO_HORIZON_REACHED",
                "all_targets_dead": False,
                "criterion_met": True,
            },
            "scenario_complete": True,
            "elapsed_ms": WATCHDOG_MS,
            "final_state": {
                "dynamic_team_background": {
                    "background_damage_applied": 80_000.0,
                    "targets": [{"target_index": 0, "dead": False}],
                }
            },
        }
        self.assertEqual(
            "CENSORED_WATCHDOG",
            adjudicate_development_wave_completion_v1(case, rollout)["status"],
        )

    def test_cat2_runner_lane_uses_lifecycle_and_dead_target(self) -> None:
        case = build_development_wave_case_v1(17)
        lane = {
            "completion_mode": "ALL_TARGETS_DEAD",
            "completion_criterion_met": True,
            "elapsed_ms": 9000,
            "damage": 2400.0,
            "artifact": {
                "lifecycle_receipt": {
                    "terminal_reason": "ENCOUNTER_FINISHED",
                    "final_state": {
                        "state": {
                            "finished": True,
                            "dynamic_team_background": {
                                "simulated_damage_applied": 2400.0,
                                "background_damage_applied": 123_997.0,
                                "targets": [{"target_index": 0, "dead": True}],
                            },
                        }
                    },
                }
            },
        }
        self.assertEqual("COMPLETED", adjudicate_development_wave_completion_v1(case, lane)["status"])
        lane["completion_mode"] = "SCENARIO_HORIZON_REACHED"
        self.assertEqual("CENSORED_WATCHDOG", adjudicate_development_wave_completion_v1(case, lane)["status"])


if __name__ == "__main__":
    unittest.main()
