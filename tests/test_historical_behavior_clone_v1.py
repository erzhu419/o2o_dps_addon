from __future__ import annotations

import json
import unittest

from o2o_dps import historical_behavior_clone_v1 as clone_v1
from o2o_dps import chronicle_external_historical_fury_policy_v4 as policy_v4


PLAYER = "0x00000000000000A1"
TARGET = "0xF130000001000001"


def _observation(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": clone_v1.OBSERVATION_SCHEMA,
        "wave_elapsed_ms": 0,
        "observed_target_count": 1,
        "observed_dead_target_count": 0,
        "background_dps": 1000.0,
        "actor_has_last_target": False,
        "last_controllable_action": None,
        "last_controllable_lane": None,
        "last_gcd_action_age_ms": None,
        "observed_stance": None,
        "pending_queue_action": None,
        "pending_queue_age_ms": None,
    }
    value.update(updates)
    return value


def _decision(
    elapsed_ms: int,
    action_key: str,
    target_role: str = "CURRENT_ENEMY",
    *,
    last_action: str = "NONE",
) -> clone_v1.TrainingDecisionV1:
    return clone_v1.TrainingDecisionV1(
        elapsed_ms=elapsed_ms,
        action_key=action_key,
        target_role=target_role,
        feature_atoms=(
            policy_v4.FeatureAtom("sequence.last_controllable_action", last_action),
        ),
    )


def _state(trace_index: int, order: list[int]) -> dict[str, object]:
    return {
        "actor_last_observed_target_guid": TARGET if trace_index else None,
        "actor_recent_exact_trace_indices": list(range(trace_index)),
        "cutoff_exclusive_order_key": order,
        "cutoff_semantics": "strictly before current exact EventMeta order_key",
        "leave_one_player_out_background_before": {
            "excluded_focal_damage": 0,
            "filter_contract": (
                "exclude strict-prefix direct/official-owner/official-controller "
                "DMG attributed to the exact focal player GUID"
            ),
            "focal_player_guid": PLAYER,
            "included_background_damage": 0,
            "included_explicit_other_player_damage": 0,
            "included_unattributed_damage": 0,
            "name_or_guid_suffix_inference_used": False,
            "prefix_elapsed_average_background_dps": None,
            "unattributed_retained_as_explicit_unknown": True,
        },
        "observed_target_prefix_state_ref": {
            "content_sha256": "a" * 64,
            "observed_target_count": 1,
            "observed_dead_target_count": 0,
        },
        "prefix_damage_amount_total": 0,
        "prefix_player_or_unattributed_event_count": trace_index,
        "prefix_trace_event_count": trace_index,
        "prefix_trace_exclusive_index": trace_index,
        "wave_elapsed_ms": order[1] - 1_000,
    }


def _append_event(
    trace: list[dict[str, object]],
    transitions: list[dict[str, object]],
    *,
    event_type: str,
    spell_id: int,
    name: str,
    timestamp_ms: int,
) -> None:
    trace_index = len(trace)
    order = [0, timestamp_ms, trace_index + 10, 3, trace_index]
    spell = {"id": spell_id, "name": name, "official_attack_outcome": 0}
    target = {
        "guid": TARGET,
        "lane": "HOSTILE_CREATURE",
        "voting_enemy_target": True,
        "hostile_object_preserved_nonvoting": False,
        "hostile_player_preserved_nonvoting": False,
    }
    event = {
        "event_type": event_type,
        "attribution": {
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "player_guid": PLAYER,
        },
        "spell": spell,
        "target": target,
    }
    trace.append(
        {
            "trace_index": trace_index,
            "trace_kind": "EXACT_PLAYER_EVENT",
            "player_guid": PLAYER,
            "order_key": order,
            "event": event,
        }
    )
    transitions.append(
        {
            "trace_index": trace_index,
            "order_key": order,
            "feature_cutoff_is_strict_prefix": True,
            "current_event_present_in_state_before": False,
            "future_outcomes_in_state_before": False,
            "current_event_label": {
                "event_type": event_type,
                "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
                "source_lane": "FRIENDLY_PLAYER",
                "spell": spell,
                "target": target,
            },
            "state_before": _state(trace_index, order),
        }
    )


class HistoricalBehaviorCloneV1Tests(unittest.TestCase):
    def test_compiler_is_explicitly_pooled_and_closes_exact_ontology(self) -> None:
        model = clone_v1.compile_training_episodes_v1(
            [
                [
                    _decision(100, "warrior.bloodthirst"),
                    _decision(
                        250,
                        "warrior.heroic_strike",
                        last_action="warrior.bloodthirst",
                    ),
                ],
                [_decision(50, "warrior.whirlwind")],
            ]
        )

        self.assertEqual(model["cohort_contract"]["cohort_id"], "POOLED_CLEAN_FURY")
        self.assertFalse(model["cohort_contract"]["top_player_selection_used"])
        self.assertFalse(model["claim_boundary"]["top_player_policy"])
        self.assertEqual(
            [row["action_key"] for row in model["action_ontology"]],
            list(clone_v1.ACTION_KEYS),
        )
        self.assertEqual(model["training_summary"]["episode_count"], 2)
        self.assertEqual(model["training_summary"]["decision_count"], 3)
        self.assertEqual(model["delay_head"]["global"]["sample_count"], 3)
        clone_v1.validate_model_v1(json.loads(json.dumps(model)))

    def test_mark_distribution_is_conditioned_on_exact_legal_mask(self) -> None:
        model = clone_v1.compile_training_episodes_v1(
            [
                [_decision(100, "warrior.bloodthirst")]
                for _ in range(20)
            ]
            + [[_decision(100, "warrior.whirlwind")]]
        )
        result = clone_v1.predict_mark_distribution_v1(
            model,
            _observation(),
            ["warrior.whirlwind", "warrior.cleave"],
        )

        self.assertEqual(
            set(result["probabilities"]),
            {"warrior.whirlwind", "warrior.cleave"},
        )
        self.assertAlmostEqual(sum(result["probabilities"].values()), 1.0)
        self.assertNotIn("warrior.bloodthirst", result["probabilities"])
        self.assertTrue(result["conditioning_applied_before_sampling"])

    def test_runtime_waits_then_keeps_proposal_until_acceptance(self) -> None:
        model = clone_v1.compile_training_episodes_v1(
            [[_decision(100, "warrior.bloodthirst")]]
        )
        runtime = clone_v1.HistoricalBehaviorCloneRuntimeV1(model, seed=7)

        waiting = runtime.propose(
            now_ms=0,
            observation=_observation(),
            legal_actions=["warrior.bloodthirst"],
        )
        self.assertEqual(waiting["kind"], "WAIT")
        self.assertEqual(waiting["wait_ms"], 100)

        proposal = runtime.propose(
            now_ms=100,
            observation=_observation(wave_elapsed_ms=100),
            legal_actions=["warrior.bloodthirst"],
        )
        repeated = runtime.propose(
            now_ms=100,
            observation=_observation(wave_elapsed_ms=100),
            legal_actions=["warrior.whirlwind"],
        )
        self.assertEqual(proposal, repeated)
        self.assertEqual(proposal["kind"], "ACTION")
        self.assertEqual(proposal["action_key"], "warrior.bloodthirst")
        runtime.record_submission(
            proposal_id=proposal["proposal_id"], accepted=True, now_ms=100
        )
        self.assertEqual(runtime.last_action_key, "warrior.bloodthirst")
        self.assertGreaterEqual(runtime.next_action_at_ms, 100)

    def test_empty_legal_mask_waits_without_consuming_a_decision(self) -> None:
        model = clone_v1.compile_training_episodes_v1(
            [[_decision(0, "warrior.bloodthirst")]]
        )
        runtime = clone_v1.HistoricalBehaviorCloneRuntimeV1(model, seed=3)
        waiting = runtime.propose(
            now_ms=0, observation=_observation(), legal_actions=[]
        )
        self.assertEqual(waiting["kind"], "WAIT")
        self.assertEqual(waiting["reason"], "NO_LEGAL_ONTOLOGY_ACTION")
        proposal = runtime.propose(
            now_ms=1,
            observation=_observation(wave_elapsed_ms=1),
            legal_actions=["warrior.bloodthirst"],
        )
        self.assertEqual(proposal["proposal_id"], 0)

    def test_stage5_compiler_reuses_v3_pairing_and_v4_prefix_features(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append_event(
            trace,
            transitions,
            event_type="START",
            spell_id=25286,
            name="Heroic Strike",
            timestamp_ms=1_000,
        )
        _append_event(
            trace,
            transitions,
            event_type="GO",
            spell_id=25286,
            name="Heroic Strike",
            timestamp_ms=1_100,
        )
        _append_event(
            trace,
            transitions,
            event_type="START",
            spell_id=23894,
            name="Bloodthirst",
            timestamp_ms=1_300,
        )
        wave = {
            "wave": {
                "instance_id": "instance-1",
                "encounter_id": "encounter-1",
                "wave_id": "wave-1",
            },
            "exact_trace": trace,
            "players": [
                {
                    "player": {"guid": PLAYER, "class": "WARRIOR"},
                    "warrior_spec_lane": {"partition_key": "WARRIOR_FURY"},
                    "eligibility_observation": {
                        "historical_fury_candidate_filter_passed": True
                    },
                    "prefix_transitions": transitions,
                }
            ],
        }

        model = clone_v1.compile_pooled_clean_fury_stage5_v1([wave])

        self.assertEqual(model["training_summary"]["decision_count"], 2)
        self.assertEqual(
            model["training_summary"]["action_count"]["warrior.heroic_strike"],
            1,
        )
        self.assertEqual(
            model["training_summary"]["action_count"]["warrior.bloodthirst"],
            1,
        )
        audit = model["training_summary"]["source_audit"]
        self.assertEqual(audit["paired_go_deduplicated"], 1)
        self.assertEqual(audit["strict_training_decisions_retained"], 2)

    def test_unknown_legal_action_is_rejected(self) -> None:
        model = clone_v1.compile_training_episodes_v1(
            [[_decision(0, "warrior.bloodthirst")]]
        )
        with self.assertRaisesRegex(
            clone_v1.HistoricalBehaviorCloneV1Error, "outside the ontology"
        ):
            clone_v1.predict_mark_distribution_v1(
                model, _observation(), ["warrior.not_real"]
            )


if __name__ == "__main__":
    unittest.main()
