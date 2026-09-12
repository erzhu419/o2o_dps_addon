from __future__ import annotations

from dataclasses import replace
import unittest

from o2o_dps import chronicle_external_historical_fury_policy_v4 as policy_v4
from o2o_dps import chronicle_external_historical_fury_policy_v3 as policy_v3


PLAYER = "0x00000000000000A1"
TARGET = "0xF130000001000001"


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
            "observed_target_count": int(trace_index > 0),
            "observed_dead_target_count": 0,
        },
        "prefix_damage_amount_total": 0,
        "prefix_player_or_unattributed_event_count": trace_index,
        "prefix_trace_event_count": trace_index,
        "prefix_trace_exclusive_index": trace_index,
        "wave_elapsed_ms": order[1] - 1_000,
    }


def _append(
    trace: list[dict[str, object]],
    transitions: list[dict[str, object]],
    *,
    event_type: str,
    spell_id: int,
    name: str,
    timestamp_ms: int,
) -> dict[str, object]:
    trace_index = len(trace)
    event_index = trace_index + 10
    order = [
        0,
        timestamp_ms,
        event_index,
        3 if event_type == "START" else 4,
        trace_index,
    ]
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
    transition = {
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
    transitions.append(transition)
    return transition


def _player(transitions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "player": {"guid": PLAYER, "class": "WARRIOR"},
        "prefix_transitions": transitions,
    }


def _inventory_record(item_id: int) -> dict[str, object]:
    return {
        "schema": "chronicle_combatant_info/v1",
        "instance_ref": "instance-1",
        "encounter_id": "encounter-1",
        "message_ordinal": 0,
        "anchor": {"event_index": 5},
        "player": {"guid": PLAYER},
        "gear": [
            {
                "slot_index": 0,
                "item_id": item_id,
                "enchant_id": 2564,
                "temporary_enchant_id": 37,
                "gem_enchant_ids": [3001, 0],
            }
        ],
        "talents": {
            "summary": [17, 34, 0],
            "trees": ["20305001302", "05050005525010051", ""],
        },
    }


def _single_bloodthirst(
    *, inventory_index: policy_v3.CharacterInventoryIndex | None = None
) -> policy_v4.V4Decision:
    trace: list[dict[str, object]] = []
    transitions: list[dict[str, object]] = []
    _append(
        trace,
        transitions,
        event_type="START",
        spell_id=23894,
        name="Bloodthirst",
        timestamp_ms=1_000,
    )
    result = policy_v4.process_player_transitions(
        player=_player(transitions),
        trace=trace,
        instance_ref="instance-1",
        encounter_id="encounter-1",
        inventory_index=inventory_index,
    )
    return result.decisions[0]


class ChronicleExternalHistoricalFuryPolicyV4Tests(unittest.TestCase):
    def test_current_stage5_shape_keeps_all_dynamic_fields_explicitly_missing(self) -> None:
        decision = _single_bloodthirst()
        dynamic = decision.feature_view.dynamic
        self.assertEqual(dynamic.source, "CURRENT_STAGE5_FIELD_ABSENT")
        self.assertEqual(dynamic.atoms, ())
        self.assertEqual(
            dynamic.missing_fields,
            policy_v4.DYNAMIC_FIELD_NAMES,
        )
        self.assertTrue(
            all(value == policy_v4.MISSING for value in dynamic.buckets.values())
        )
        self.assertEqual(
            decision.feature_view.contexts_for_arm(policy_v4.ARM_B),
            decision.feature_view.contexts_for_arm(policy_v4.ARM_C),
        )
        availability = policy_v4.summarize_feature_availability([decision])
        self.assertFalse(availability["missing_values_imputed"])
        self.assertEqual(
            set(availability["missing_count_by_field"]),
            set(policy_v4.DYNAMIC_FIELD_NAMES),
        )

    def test_explicit_prefix_envelope_is_bucketed_without_filling_absent_fields(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        transition = _append(
            trace,
            transitions,
            event_type="START",
            spell_id=23894,
            name="Bloodthirst",
            timestamp_ms=1_000,
        )
        state = transition["state_before"]
        assert isinstance(state, dict)
        state[policy_v4.PREFIX_STATE_KEY] = {
            "schema": policy_v4.PREFIX_STATE_SCHEMA,
            "cutoff_exclusive_order_key": state["cutoff_exclusive_order_key"],
            "rage": 29,
            "gcd_remaining_ms": 0,
            "cooldowns": {
                "warrior.bloodthirst": 700,
                "warrior.whirlwind": 7_000,
            },
            "auras": {
                "flurry": {"active": True, "stacks": 3, "remaining_ms": 1_200}
            },
            "target_health_percent": 19.9,
            "main_hand_swing_remaining_ms": 400,
            "next_swing_queue": "warrior.heroic_strike",
        }
        decision = policy_v4.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        ).decisions[0]
        buckets = decision.feature_view.dynamic.buckets
        self.assertEqual(buckets["rage"], "25_29")
        self.assertEqual(buckets["gcd_remaining_ms"], "READY")
        self.assertEqual(
            buckets["cooldowns.warrior.bloodthirst"], "WITHIN_1500MS"
        )
        self.assertEqual(
            buckets["cooldowns.warrior.whirlwind"], "LATER_THAN_5000MS"
        )
        self.assertEqual(buckets["auras.flurry.active"], "ACTIVE")
        self.assertEqual(buckets["auras.flurry.stacks"], "2_3")
        self.assertEqual(
            buckets["target_health_percent"], "EXECUTE_AT_OR_BELOW_20"
        )
        self.assertEqual(
            buckets["main_hand_swing_remaining_ms"], "WITHIN_500MS"
        )
        self.assertEqual(
            buckets["off_hand_swing_remaining_ms"], policy_v4.MISSING
        )
        self.assertIn(
            "off_hand_swing_remaining_ms",
            decision.feature_view.dynamic.missing_fields,
        )

    def test_dynamic_envelope_must_share_the_label_prefix_cutoff(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        transition = _append(
            trace,
            transitions,
            event_type="START",
            spell_id=23894,
            name="Bloodthirst",
            timestamp_ms=1_000,
        )
        state = transition["state_before"]
        assert isinstance(state, dict)
        state[policy_v4.PREFIX_STATE_KEY] = {
            "schema": policy_v4.PREFIX_STATE_SCHEMA,
            "cutoff_exclusive_order_key": [9, 9, 9, 9, 9],
        }
        with self.assertRaisesRegex(
            policy_v4.HistoricalFuryPolicyV4Error,
            "dynamic state cutoff",
        ):
            policy_v4.process_player_transitions(
                player=_player(transitions),
                trace=trace,
                instance_ref="instance-1",
                encounter_id="encounter-1",
            )

    def test_current_action_is_excluded_and_queue_start_is_cleared_only_by_prefix_outcome(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append(trace, transitions, event_type="START", spell_id=25286, name="Heroic Strike", timestamp_ms=1_000)
        _append(trace, transitions, event_type="START", spell_id=23894, name="Bloodthirst", timestamp_ms=1_100)
        _append(trace, transitions, event_type="GO", spell_id=25286, name="Heroic Strike", timestamp_ms=1_200)
        _append(trace, transitions, event_type="START", spell_id=1680, name="Whirlwind", timestamp_ms=1_300)
        result = policy_v4.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )
        self.assertEqual(
            [row.action_key for row in result.decisions],
            [
                "warrior.heroic_strike",
                "warrior.bloodthirst",
                "warrior.whirlwind",
            ],
        )
        first_atoms = {
            atom.family: atom.value
            for atom in result.decisions[0].feature_view.prefix_atoms
        }
        second_atoms = {
            atom.family: atom.value
            for atom in result.decisions[1].feature_view.prefix_atoms
        }
        third_atoms = {
            atom.family: atom.value
            for atom in result.decisions[2].feature_view.prefix_atoms
        }
        self.assertEqual(first_atoms["sequence.last_controllable_action"], "NONE")
        self.assertEqual(
            second_atoms["sequence.last_controllable_action"],
            "warrior.heroic_strike",
        )
        self.assertNotEqual(
            second_atoms["sequence.last_controllable_action"],
            result.decisions[1].action_key,
        )
        self.assertEqual(
            second_atoms["queue.prefix_observation"], "warrior.heroic_strike"
        )
        self.assertEqual(
            third_atoms["queue.prefix_observation"],
            "NO_PENDING_QUEUE_START_OBSERVED",
        )

    def test_exact_inventory_changes_provenance_but_never_B_or_C_model_keys(self) -> None:
        first = _single_bloodthirst(
            inventory_index=policy_v3.CharacterInventoryIndex(
                [_inventory_record(100)]
            )
        )
        second = _single_bloodthirst(
            inventory_index=policy_v3.CharacterInventoryIndex(
                [_inventory_record(200)]
            )
        )
        self.assertNotEqual(
            first.feature_view.provenance["exact_inventory"],
            second.feature_view.provenance["exact_inventory"],
        )
        self.assertFalse(
            first.feature_view.provenance["exact_inventory_used_as_model_context"]
        )
        for arm in (policy_v4.ARM_B, policy_v4.ARM_C):
            self.assertEqual(
                first.feature_view.contexts_for_arm(arm),
                second.feature_view.contexts_for_arm(arm),
            )

    def test_unseen_dynamic_atom_backs_off_to_identical_prefix_distribution(self) -> None:
        base = _single_bloodthirst().feature_view
        observed = replace(
            base,
            dynamic=policy_v4.ObservedDynamicState(
                buckets={"rage": "25_29"},
                atoms=(policy_v4.FeatureAtom("dynamic.rage_bucket", "25_29"),),
                missing_fields=(),
                source="TEST",
            ),
        )
        other = replace(
            base,
            dynamic=policy_v4.ObservedDynamicState(
                buckets={"rage": "30_39"},
                atoms=(policy_v4.FeatureAtom("dynamic.rage_bucket", "30_39"),),
                missing_fields=(),
                source="TEST",
            ),
        )
        unseen = replace(
            base,
            dynamic=policy_v4.ObservedDynamicState(
                buckets={"rage": "10_14"},
                atoms=(policy_v4.FeatureAtom("dynamic.rage_bucket", "10_14"),),
                missing_fields=(),
                source="TEST",
            ),
        )
        aggregate = policy_v4.AdditiveAggregate()
        aggregate.add(observed, "action-a", arm=policy_v4.ARM_C)
        aggregate.add(other, "action-b", arm=policy_v4.ARM_C)
        self.assertEqual(aggregate.decision_count, 2)
        merged = policy_v4.AdditiveAggregate()
        merged.merge(aggregate)
        self.assertEqual(merged.decision_cells, aggregate.decision_cells)
        self.assertEqual(merged.context_counts, aggregate.context_counts)
        b_probabilities, b_matched, b_unknown = policy_v4.distribution(
            merged, unseen, arm=policy_v4.ARM_B
        )
        c_probabilities, c_matched, c_unknown = policy_v4.distribution(
            merged, unseen, arm=policy_v4.ARM_C
        )
        self.assertEqual(b_probabilities, c_probabilities)
        self.assertEqual(b_unknown, c_unknown)
        self.assertEqual(b_matched, c_matched)
        self.assertNotIn("dynamic.rage_bucket", c_matched)
        self.assertIn("base.wave_coarse", c_matched)

    def test_v4_reuses_v3_proc_and_instant_decision_semantics(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append(trace, transitions, event_type="GO", spell_id=12970, name="Flurry", timestamp_ms=1_000)
        _append(trace, transitions, event_type="GO", spell_id=2687, name="Bloodrage", timestamp_ms=1_100)
        result = policy_v4.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )
        v3_result = policy_v3.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )
        self.assertEqual(
            [row.action_key for row in result.decisions], ["warrior.bloodrage"]
        )
        self.assertEqual(
            [row.action_key for row in result.decisions],
            [row.action_key for row in v3_result.decisions],
        )
        self.assertEqual(result.audit["classified:EXOGENOUS_EVENT"], 1)

    def test_ablation_plan_is_fixed_and_does_not_authorize_hpc(self) -> None:
        plan = policy_v4.load_fixed_ablation_plan()
        self.assertEqual(
            [row["id"] for row in plan["variants"]], list(policy_v4.ARMS)
        )
        self.assertFalse(plan["hyperparameter_search"])
        self.assertFalse(plan["execution"]["currently_authorized"])
        self.assertFalse(
            plan["execution"]["current_stage5_dynamic_state_envelope_available"]
        )
        self.assertEqual(
            plan["metrics"],
            [
                "top1_accuracy",
                "top3_accuracy",
                "contextual_log_loss",
                "expected_calibration_error",
            ],
        )
        self.assertTrue(plan["label_universe"]["same_labels_required_across_arms"])
        self.assertTrue(plan["split"]["same_folds_required_across_arms"])


if __name__ == "__main__":
    unittest.main()
