from __future__ import annotations

from collections import Counter
import random
import unittest
from unittest.mock import patch

from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.upper_kara_target_head_heldout_eval_v1 import _learned_probabilities, score_compiled_rows_v1


def row(*, selected: str, previous: str | None, candidates: list[dict],
        intent_kind: str = "START") -> dict:
    return {
        "actor": {"player_guid": "p", "class": "WARRIOR", "spec_key": "WARRIOR_FURY"},
        "emission_state_before_current_event": {
            "actor_last_target_guid": previous,
            "actor_last_mark_token": None,
            "marked_activity": {"other_team_including_unattributed": {
                "action_event_count_3000ms": 0, "damage_amount_3000ms": 0,
            }},
            "target_state": {
                "alive_target_count": len(candidates),
                "target_choice_candidates": candidates,
            },
        },
        "label": {
            "event_type": "START" if intent_kind == "START" else "DMG",
            "spell_id": None if intent_kind == "START" else 6603,
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "target_mode": "SWITCH_ALIVE",
            "target_lane": "HOSTILE_CREATURE",
            "target_guid": selected,
        },
    }


def candidate(guid: str, *, first: int, actions: int) -> dict:
    return {
        "target_guid": guid,
        "first_activity_ms": first,
        "prefix_damage": 0,
        "recent_direct_start_count_3000ms": actions,
        "recent_damage_amount_3000ms": 0,
    }


class HeldoutTargetHeadEvalTests(unittest.TestCase):
    def test_retarget_denominator_excludes_previous_target(self) -> None:
        candidates = [candidate("A", first=0, actions=0),
                      candidate("B", first=1, actions=0),
                      candidate("C", first=2, actions=4)]
        rows = [row(selected="C", previous=None, candidates=candidates),
                row(selected="C", previous="A", candidates=candidates)]
        no_model = score_compiled_rows_v1(rows)
        first = no_model["metrics"]["FIRST_ACQUISITION"]
        retarget = no_model["metrics"]["RETARGET"]
        self.assertEqual(1, first["eligible_choices"])
        self.assertEqual(1 / 3, first["uniform_expected_accuracy"])
        self.assertEqual(1, retarget["eligible_choices"])
        self.assertEqual(1 / 2, retarget["uniform_expected_accuracy"])
        self.assertEqual(1, no_model["coverage_gate"]["phase_precheck"]["RETARGET"])

        model = response.HierarchicalMarkedSemiMarkovV1()
        features = response._target_choice_features(candidates[1:])
        model.target_choice_counts[(("GLOBAL",), "START_RETARGET", features["B"])] = Counter({False: 9, True: 1})
        model.target_choice_counts[(("GLOBAL",), "START_RETARGET", features["C"])] = Counter({True: 9, False: 1})
        scored = score_compiled_rows_v1(rows, model=model)
        metric = scored["metrics"]["RETARGET"]
        self.assertEqual(1, metric["learned_supported_choices"])
        self.assertGreater(metric["learned_expected_accuracy"], 0.5)
        self.assertEqual(1.0, metric["learned_top1_accuracy"])
        self.assertEqual(0, model.row_count)  # held-out rows never train

    def test_unseen_and_stay_rows_do_not_vote(self) -> None:
        candidates = [candidate("A", first=0, actions=0), candidate("B", first=1, actions=1)]
        unseen = row(selected="C", previous=None, candidates=candidates)
        stay = row(selected="A", previous="A", candidates=candidates)
        scored = score_compiled_rows_v1([unseen, stay])
        self.assertEqual(2, scored["direct_player_start_rows"])
        self.assertEqual(0, scored["metrics"]["FIRST_ACQUISITION"]["eligible_choices"])
        self.assertEqual(0, scored["metrics"]["RETARGET"]["eligible_choices"])

    def test_same_feature_candidates_count_support_once_like_runtime(self) -> None:
        candidates = [candidate("A", first=0, actions=0), candidate("B", first=0, actions=0)]
        heldout = row(selected="B", previous=None, candidates=candidates)
        features = response._target_choice_features(candidates)
        self.assertEqual(features["A"], features["B"])
        model = response.HierarchicalMarkedSemiMarkovV1(min_guid_events=5)
        contexts = response._context_keys(
            heldout["actor"], heldout["emission_state_before_current_event"], model.variant_id
        )
        guid_context = next(context for context in contexts if context[0] == "GUID")
        model.target_choice_counts[(guid_context, "START_FIRST_ACQUISITION", features["A"])] = Counter({True: 1, False: 2})
        model.target_choice_counts[(("GLOBAL",), "START_FIRST_ACQUISITION", features["A"])] = Counter({True: 8, False: 2})
        probabilities, level = _learned_probabilities(model, heldout, "START_FIRST_ACQUISITION", features)
        sampled = model.sample_target_choice(
            actor=heldout["actor"],
            emission_state=heldout["emission_state_before_current_event"],
            target_mode="SWITCH_ALIVE",
            intent_kind="START",
            eligible_target_guids=["A", "B"],
            current_target_guid=None,
            rng=random.Random(1),
        )
        self.assertEqual("GLOBAL", level)
        self.assertEqual("GLOBAL", sampled["context_level"])
        self.assertEqual(0.5, probabilities["B"])

    def test_white_head_has_separate_phase_and_explicit_player_view(self) -> None:
        candidates = [candidate("A", first=0, actions=0),
                      candidate("B", first=1, actions=3)]
        white = row(selected="B", previous=None, candidates=candidates, intent_kind="WHITE6603")
        start = row(selected="A", previous=None, candidates=candidates)
        focal = row(selected="B", previous=None, candidates=candidates, intent_kind="WHITE6603")
        focal["actor"]["player_guid"] = "0x0000000000576754"
        model = response.HierarchicalMarkedSemiMarkovV1()
        features = response._target_choice_features(candidates)
        model.target_choice_counts[(("GLOBAL",), "WHITE6603_FIRST_ACQUISITION", features["B"])] = Counter({True: 10})
        model.target_choice_counts[(("GLOBAL",), "WHITE6603_FIRST_ACQUISITION", features["A"])] = Counter({False: 10})
        scored = score_compiled_rows_v1([white, start, focal], model=model)
        teammate = score_compiled_rows_v1([white, start], model=model)
        self.assertEqual(2, scored["white_metrics"]["FIRST_ACQUISITION"]["eligible_choices"])
        self.assertEqual(1, teammate["white_metrics"]["FIRST_ACQUISITION"]["eligible_choices"])
        self.assertEqual(1, scored["metrics"]["FIRST_ACQUISITION"]["eligible_choices"])
        self.assertEqual(2, scored["direct_player_white_6603_rows"])
        self.assertGreater(scored["white_metrics"]["FIRST_ACQUISITION"]["learned_expected_accuracy"], 0.5)
        self.assertEqual(0.5, scored["metrics"]["FIRST_ACQUISITION"]["learned_expected_accuracy"])

    def test_frozen_v6_unprefixed_start_phase_scores_in_start_view(self) -> None:
        heldout = row(selected="B", previous=None, candidates=[
            candidate("A", first=0, actions=0), candidate("B", first=1, actions=1),
        ])
        features = response._target_choice_features(
            heldout["emission_state_before_current_event"]["target_state"]["target_choice_candidates"]
        )
        with patch.object(response, "_target_choice_training_observations",
                          return_value=("FIRST_ACQUISITION", features, "B")):
            scored = score_compiled_rows_v1([heldout])
        self.assertEqual(1, scored["metrics"]["FIRST_ACQUISITION"]["eligible_choices"])
        self.assertEqual(0, scored["white_metrics"]["FIRST_ACQUISITION"]["eligible_choices"])

    def test_frozen_v6_white_row_without_candidate_state_is_unavailable(self) -> None:
        white = row(selected="B", previous=None, candidates=[
            candidate("A", first=0, actions=0), candidate("B", first=1, actions=0),
        ], intent_kind="WHITE6603")
        del white["emission_state_before_current_event"]["target_state"]["target_choice_candidates"]
        with patch.object(response, "_target_choice_training_observations", return_value=None):
            scored = score_compiled_rows_v1([white])
        self.assertEqual("UNAVAILABLE_IN_COMPILER", scored["white_head_status"])
        self.assertEqual(1, scored["white_coverage_gate"]["compiler_prefix_candidates_unavailable_rows"])


if __name__ == "__main__":
    unittest.main()
