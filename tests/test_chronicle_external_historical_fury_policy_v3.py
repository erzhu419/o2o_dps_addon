from __future__ import annotations

import json
import unittest

from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
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
) -> None:
    trace_index = len(trace)
    event_index = trace_index + 10
    order = [0, timestamp_ms, event_index, 3 if event_type == "START" else 4, trace_index]
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


def _player(transitions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "player": {"guid": PLAYER, "class": "WARRIOR"},
        "prefix_transitions": transitions,
    }


def _inventory_record(
    *, event_index: int, message_ordinal: int, item_id: int
) -> dict[str, object]:
    return {
        "schema": "chronicle_combatant_info/v1",
        "instance_ref": "instance-1",
        "encounter_id": "encounter-1",
        "message_ordinal": message_ordinal,
        "anchor": {"event_index": event_index},
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


class ChronicleExternalHistoricalFuryPolicyV3Tests(unittest.TestCase):
    def test_ontology_is_exactly_fifteen_and_unknown_go_is_not_called_a_proc(self) -> None:
        self.assertEqual(len(policy_v3.ACTION_ONTOLOGY), 15)
        self.assertEqual(
            len({spec.action_key for spec in policy_v3.ACTION_ONTOLOGY}), 15
        )
        unknown_start = policy_v3.classify_action_event(
            event_type="START", spell={"id": 99999, "name": "Other Button"}
        )
        unknown_go = policy_v3.classify_action_event(
            event_type="GO", spell={"id": 99999, "name": "Other Button"}
        )
        self.assertEqual(unknown_start.role, policy_v3.ROLE_OUT_OF_ONTOLOGY)
        self.assertEqual(unknown_go.role, policy_v3.ROLE_UNRESOLVED_GO)

    def test_proc_go_is_excluded_but_real_unpaired_instant_is_retained(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append(trace, transitions, event_type="START", spell_id=23894, name="Bloodthirst", timestamp_ms=1_000)
        _append(trace, transitions, event_type="GO", spell_id=23894, name="Bloodthirst", timestamp_ms=1_010)
        _append(trace, transitions, event_type="GO", spell_id=12970, name="Flurry", timestamp_ms=1_020)
        _append(trace, transitions, event_type="GO", spell_id=12721, name="Deep Wounds", timestamp_ms=1_030)
        _append(trace, transitions, event_type="GO", spell_id=2687, name="Bloodrage", timestamp_ms=2_000)
        _append(trace, transitions, event_type="START", spell_id=1680, name="Whirlwind", timestamp_ms=3_000)

        result = policy_v3.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )

        self.assertEqual(
            [row.action_key for row in result.decisions],
            ["warrior.bloodthirst", "warrior.bloodrage", "warrior.whirlwind"],
        )
        self.assertEqual(
            result.decisions[1].event_role,
            policy_v3.ROLE_CONTROLLABLE_INSTANT_GO,
        )
        self.assertEqual(result.audit["classified:EXOGENOUS_EVENT"], 2)
        self.assertEqual(result.audit["paired_go_deduplicated"], 1)
        whirlwind_tactical = json.loads(result.decisions[-1].contexts["tactical"])
        self.assertEqual(
            whirlwind_tactical["last_prefix_action_spell"],
            "warrior.bloodrage",
        )
        self.assertEqual(
            whirlwind_tactical["previous_controllable_action"],
            "warrior.bloodthirst",
        )

    def test_queue_and_slam_go_outcomes_are_not_decision_labels(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append(trace, transitions, event_type="GO", spell_id=25286, name="Heroic Strike", timestamp_ms=1_000)
        _append(trace, transitions, event_type="GO", spell_id=45961, name="Slam", timestamp_ms=1_100)
        _append(trace, transitions, event_type="START", spell_id=25286, name="Heroic Strike", timestamp_ms=1_200)
        _append(trace, transitions, event_type="GO", spell_id=25286, name="Heroic Strike", timestamp_ms=1_210)

        result = policy_v3.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )

        self.assertEqual(
            [row.action_key for row in result.decisions],
            ["warrior.heroic_strike"],
        )
        self.assertEqual(result.audit["paired_go_deduplicated"], 1)
        self.assertEqual(result.audit["nondecision_events_excluded"], 2)
        execute_result = policy_v3.classify_action_event(
            event_type="GO", spell={"id": 20647, "name": "Execute"}
        )
        self.assertEqual(execute_result.role, policy_v3.ROLE_CONTROLLED_OUTCOME)

    def test_inventory_selection_is_causal_and_preserves_exact_fields(self) -> None:
        index = policy_v3.CharacterInventoryIndex(
            [
                _inventory_record(event_index=5, message_ordinal=0, item_id=100),
                _inventory_record(event_index=99, message_ordinal=1, item_id=200),
            ]
        )
        selected, outcome = index.select(
            instance_ref="instance-1",
            encounter_id="encounter-1",
            player_guid=PLAYER,
            event_index=10,
        )
        self.assertEqual(outcome, "matched")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.gear_slots[0]["item_id"], 100)
        self.assertEqual(selected.gear_slots[0]["enchant_id"], 2564)
        self.assertEqual(selected.gear_slots[0]["temporary_enchant_id"], 37)
        self.assertEqual(selected.talent_summary, (17, 34, 0))
        rejected, outcome = index.select(
            instance_ref="instance-1",
            encounter_id="encounter-1",
            player_guid=PLAYER,
            event_index=4,
        )
        self.assertIsNone(rejected)
        self.assertEqual(outcome, "late_info_only")

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
        diagnostic = policy_v3.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
            inventory_index=index,
        )
        tactical = json.loads(diagnostic.decisions[0].contexts["tactical"])
        full = json.loads(diagnostic.decisions[0].contexts["full"])
        self.assertTrue(tactical["static_character_inventory_observed"])
        self.assertEqual(
            full["static_character_profile"]["gear_slots"][0]["item_id"], 100
        )
        self.assertEqual(
            full["static_character_profile"]["talent_trees"],
            ["20305001302", "05050005525010051", ""],
        )

    def test_current_action_never_appears_in_its_own_prefix_features(self) -> None:
        trace: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        _append(trace, transitions, event_type="START", spell_id=1680, name="Whirlwind", timestamp_ms=1_000)
        _append(trace, transitions, event_type="GO", spell_id=2687, name="Bloodrage", timestamp_ms=2_000)
        result = policy_v3.process_player_transitions(
            player=_player(transitions),
            trace=trace,
            instance_ref="instance-1",
            encounter_id="encounter-1",
        )
        first = json.loads(result.decisions[0].contexts["tactical"])
        second = json.loads(result.decisions[1].contexts["tactical"])
        self.assertEqual(first["last_prefix_action_spell"], "NONE")
        self.assertEqual(second["last_prefix_action_spell"], "warrior.whirlwind")
        self.assertNotEqual(
            second["last_prefix_action_spell"], result.decisions[1].action_key
        )

    def test_component_evaluation_keeps_whole_components_held_out(self) -> None:
        components: dict[str, policy_v2._Aggregate] = {}
        for component, action in (("component-a", "action-a"), ("component-b", "action-b")):
            aggregate = policy_v2._Aggregate()
            aggregate.observe_episode(
                player_node=f"player:{component}", instance_node=f"instance:{component}"
            )
            aggregate.add(
                {"coarse": "coarse", "tactical": "tactical", "full": "full"},
                action,
            )
            components[component] = aggregate
        evaluation = policy_v3.evaluate_outer_components(components, fold_count=2)
        self.assertFalse(evaluation["row_random_split"])
        self.assertTrue(evaluation["each_component_held_out_exactly_once"])
        self.assertEqual(
            [fold["train_test_component_overlap_count"] for fold in evaluation["folds"]],
            [0, 0],
        )

    def test_wave_diagnostic_reuses_upstream_full_roster_component(self) -> None:
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
        player = _player(transitions)
        player.update(
            {
                "warrior_spec_lane": {"partition_key": policy_v2.FURY_LANE},
                "eligibility_observation": {
                    "historical_fury_candidate_filter_passed": True
                },
                "component_membership": {
                    "instance_node_id": "instance:one",
                    "player_node_id": "player:one",
                    "guild_node_ids": ["guild:one"],
                    "required_split_unit": (
                        "connected component of instance, guild, and player nodes"
                    ),
                    "row_random_split_allowed": False,
                },
            }
        )
        report = policy_v3.analyze_wave_records(
            [
                {
                    "wave": {
                        "instance_id": "instance-1",
                        "encounter_id": "encounter-1",
                    },
                    "exact_trace": trace,
                    "players": [player],
                }
            ],
            node_to_outer_component={
                "instance:one": "component:one",
                "player:one": "component:one",
                "guild:one": "component:one",
            },
        )
        self.assertEqual(report["action_label_audit"]["accepted_voting_labels"], 1)
        outer = report["outer_component_evaluation"]
        self.assertEqual(outer["component_count"], 1)
        self.assertFalse(outer["row_random_split"])

    def test_static_coverage_supports_only_a_bounded_diagnostic(self) -> None:
        coverage = {
            "instance_count": 84,
            "status_counts": {"VERIFIED_LOCAL_OFFICIAL_STREAM": 84},
            "gear_message_coverage": 1.0,
            "talent_message_coverage": 289_139 / 290_533,
        }
        assessment = policy_v3.quantitative_hpc_assessment(coverage)
        self.assertEqual(
            assessment["next_hpc_judgment"],
            "WORTH_ONE_BOUNDED_84_INSTANCE_DIAGNOSTIC",
        )
        self.assertIn("does not authorize adoption", assessment["scientific_scope"])
        self.assertAlmostEqual(
            assessment["frozen_v2_reduction"]["whitelist_retention"],
            182_911 / 867_659,
        )


if __name__ == "__main__":
    unittest.main()
