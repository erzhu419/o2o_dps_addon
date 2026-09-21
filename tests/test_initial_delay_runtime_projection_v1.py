from __future__ import annotations

from collections import Counter
import random
import unittest

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    ABLATION_D,
    DynamicTeamRuntimeV1,
    HierarchicalMarkedSemiMarkovV1,
    _context_keys,
    _delay_context_keys,
)


class InitialDelayRuntimeProjectionTests(unittest.TestCase):
    def test_initial_delay_matches_stage5_alive_zero_without_changing_live_registry(self) -> None:
        actor = {"player_guid": "player", "class": "WARRIOR", "spec_key": "WARRIOR_FURY"}
        runtime = DynamicTeamRuntimeV1(
            actors=(actor,),
            target_health_by_guid={"A": 1_000, "B": 1_000, "C": 1_000},
            target_introduced_at_ms_by_guid={"A": 0, "B": 0, "C": 0},
        )
        live = runtime.snapshot_for_actor("player")
        training = {
            **live,
            "target_state": {"alive_target_count": 0},
        }
        self.assertEqual(len(live["target_state"]["alive_target_guids"]), 3)
        self.assertEqual(
            _delay_context_keys(actor, live, ABLATION_D),
            _delay_context_keys(actor, training, ABLATION_D),
        )
        self.assertEqual(_delay_context_keys(actor, live, ABLATION_D)[-2],
                         ("CLASS", "WAVE_START", "WARRIOR", "__NONE__", "0"))
        self.assertEqual(_context_keys(actor, live, ABLATION_D)[-2],
                         ("CLASS", "WARRIOR", "__NONE__", "3-5"))
        self.assertEqual(len(runtime.alive_target_guids()), 3)

        model = HierarchicalMarkedSemiMarkovV1(variant_id=ABLATION_D)
        class_key = ("CLASS", "WAVE_START", "WARRIOR", "__NONE__", "0")
        model.delay_counts[class_key] = Counter({2: 100})
        model.delay_counts[("GLOBAL", "WAVE_START")] = Counter({8: 100})
        sampled = runtime.propose_delay(model, actor_guid="player", rng=random.Random(7))
        self.assertEqual(sampled["context_level"], "CLASS")
        self.assertEqual(sampled["delay_bucket"], 2)
        self.assertEqual(len(runtime.alive_target_guids()), 3)

    def test_previous_event_delay_keeps_current_alive_bucket(self) -> None:
        actor = {"player_guid": "player", "class": "WARRIOR", "spec_key": "WARRIOR_FURY"}
        state = {
            "actor_last_mark_token": "[\"START\",1,\"DIRECT_FRIENDLY_PLAYER\"]",
            "marked_activity": {"other_team_including_unattributed": {
                "action_event_count_3000ms": 0,
                "damage_amount_3000ms": 0,
            }},
            "target_state": {"alive_target_count": 3},
        }
        self.assertEqual(
            _delay_context_keys(actor, state, ABLATION_D)[-2],
            ("CLASS", "PREVIOUS_ACTOR_EVENT", "WARRIOR",
             state["actor_last_mark_token"], "3-5"),
        )
        self.assertEqual(state["target_state"]["alive_target_count"], 3)


if __name__ == "__main__":
    unittest.main()
