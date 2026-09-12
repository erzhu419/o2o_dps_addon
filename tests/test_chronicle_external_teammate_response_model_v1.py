from __future__ import annotations

from copy import deepcopy
import random
import unittest

from o2o_dps import chronicle_external_team_wave_model_v2 as wave_model_v2
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1


ACTOR = "0x00000000000000A1"
TEAMMATE = "0x00000000000000B1"
CANDIDATE = "0x00000000000000C1"
PET = "0xF140000001000001"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"


def _player(guid: str, hero_class: str, partition: str, spec: str | None) -> dict:
    return {
        "player": {"guid": guid, "class": hero_class, "race": None},
        "warrior_spec_lane": {
            "partition_key": partition,
            "observed_spec": spec,
            "evidence_status": (
                "OBSERVED" if hero_class.upper() == "WARRIOR" else "NOT_APPLICABLE_NON_WARRIOR"
            ),
        },
    }


def _target(guid: str | None, lane: str = "HOSTILE_CREATURE") -> dict:
    return {
        "guid": guid,
        "lane": lane,
        "voting_enemy_target": guid is not None and lane == "HOSTILE_CREATURE",
    }


def _event(
    index: int,
    time_ms: int,
    actor: str,
    event_type: str,
    target_guid: str,
    *,
    spell_id: int,
    attribution: str = "DIRECT_FRIENDLY_PLAYER",
    source_guid: str | None = None,
    damage: int = 0,
) -> dict:
    event = {
        "event_type": event_type,
        "source": {"guid": source_guid or actor, "lane": "FRIENDLY_PLAYER"},
        "target": _target(target_guid),
        "spell": {"id": spell_id, "name": f"spell-{spell_id}"},
        "attribution": {
            "attribution_kind": attribution,
            "player_guid": actor,
        },
    }
    if event_type == "DMG":
        event["damage"] = {"amount": damage, "amount_source": "DMG_ONLY"}
    return {
        "trace_index": index,
        "order_key": [0, time_ms, index, 0, 0],
        "trace_kind": "EXACT_PLAYER_EVENT",
        "player_guid": actor,
        "anchor": {"offset_ms": time_ms},
        "event": event,
    }


def _classification(index: int, time_ms: int, guid: str) -> dict:
    return {
        "trace_index": index,
        "order_key": [0, time_ms, index, 1, 0],
        "trace_kind": "CLASSIFICATION_CONTEXT",
        "player_guid": None,
        "anchor": {"offset_ms": time_ms},
        "classification": {"guid": guid, "lane": "HOSTILE_CREATURE"},
    }


def _death(
    index: int, time_ms: int, guid: str, lane: str = "HOSTILE_CREATURE"
) -> dict:
    return {
        "trace_index": index,
        "order_key": [0, time_ms, index, 2, 0],
        "trace_kind": "DEATH_MARKER",
        "player_guid": None,
        "anchor": {"offset_ms": time_ms},
        "event": {
            "event_type": "DEAD",
            "target": _target(guid, lane),
            "death": {"marker_only": True, "damage_amount_added": 0},
        },
    }


def _wave() -> dict:
    trace = [
        _classification(0, 0, TARGET_A),
        _event(1, 100, TEAMMATE, "GO", TARGET_A, spell_id=100),
        _event(2, 200, ACTOR, "START", TARGET_A, spell_id=200),
        _event(
            3,
            300,
            TEAMMATE,
            "DMG",
            TARGET_A,
            spell_id=100,
            attribution="EXACT_OFFICIAL_OWNER",
            source_guid=PET,
            damage=30,
        ),
        _death(4, 350, TARGET_A, "UNKNOWN_NONVOTING"),
        _classification(5, 360, TARGET_B),
        _event(6, 400, TEAMMATE, "GO", TARGET_B, spell_id=101),
    ]
    return {
        "schema": wave_model_v2.PARTITION_RECORD_SCHEMA,
        "wave": {
            "instance_id": "instance-a",
            "encounter_id": "encounter-a",
            "encounter_ordinal": 0,
            "wave_id": "wave-a",
            "wave_ordinal": 0,
        },
        "players": [
            _player(ACTOR, "Warrior", "WARRIOR_FURY", "Fury"),
            _player(TEAMMATE, "Mage", "MAGE_UNSPECIFIED", None),
        ],
        "exact_trace": trace,
        "descriptive_outcome": {
            "reconstruction_binding": {"window": {"start_offset_ms": 0}}
        },
    }


def _manifest_entry(instance_id: str, *, events: int, candidate: bool = True) -> dict:
    return {
        "instance_id": instance_id,
        "contamination_lane": {"candidate_filter_passed": candidate},
        "partition": {
            "path": f"{instance_id}.jsonl.gz",
            "compressed_size_bytes": events * 10,
            "record_count": 1,
        },
        "summary": {
            "wave_count": 1,
            "player_wave_episode_count": 2,
            "prefix_transition_count": events // 2,
            "exact_event_count": events,
            "exact_trace_count": events + 1,
        },
    }


def _manifest() -> dict:
    definitions = {
        "old-a": ("component-large", 500, True),
        "train-b": ("component-large", 400, True),
        "val-c": ("component-c", 50, True),
        "val-d": ("component-d", 50, True),
        "descriptive-e": ("component-large", 20, False),
    }
    node_rows = [
        {
            "node_id": response_v1._instance_node_id(instance_id),
            "component_id": component,
        }
        for instance_id, (component, _, _) in definitions.items()
    ]
    return {
        "schema": wave_model_v2.SCHEMA,
        "content_address": {"sha256": "a" * 64},
        "instances": [
            _manifest_entry(instance_id, events=events, candidate=candidate)
            for instance_id, (_, events, candidate) in definitions.items()
        ],
        "split_graph": {
            "required_split_unit": "connected component",
            "row_random_split_allowed": False,
            "same_player_or_guild_can_cross_folds": False,
            "node_to_component": node_rows,
        },
    }


class ComponentSplitTests(unittest.TestCase):
    def test_old50_is_train_and_disconnected_components_are_validation(self) -> None:
        split = response_v1.build_component_split_v1(
            _manifest(), old50_instance_ids=["old-a"]
        )
        folds = {row["component_id"]: row["split"] for row in split["components"]}
        self.assertEqual(folds["component-large"], "TRAIN")
        self.assertEqual(folds["component-c"], "VALIDATION")
        self.assertEqual(folds["component-d"], "VALIDATION")
        self.assertEqual(split["summary"]["train"]["exact_event_count"], 900)
        self.assertEqual(split["summary"]["validation"]["exact_event_count"], 100)
        self.assertEqual(split["summary"]["candidate_total"]["instance_count"], 4)
        self.assertEqual(
            split["excluded_descriptive_nontraining_instance_ids"], ["descriptive-e"]
        )
        self.assertFalse(split["scientific_boundary"]["comparison_eligible"])

    def test_overlay_pin_is_required(self) -> None:
        overlay = {
            "schema": "chronicle_old50_exact_fury_slot_overlay_manifest/v1",
            "source_bindings": {
                "stage5_manifest": {"content_sha256": "a" * 64}
            },
            "instance_order": ["old-a"],
        }
        self.assertEqual(
            response_v1.old50_instance_ids_from_overlay_manifest_v1(
                overlay, _manifest()
            ),
            ["old-a"],
        )
        overlay["source_bindings"]["stage5_manifest"]["content_sha256"] = "b" * 64
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.old50_instance_ids_from_overlay_manifest_v1(
                overlay, _manifest()
            )

    def test_split_fails_when_no_validation_component_remains(self) -> None:
        manifest = _manifest()
        manifest["instances"] = manifest["instances"][:2]
        manifest["split_graph"]["node_to_component"] = manifest["split_graph"][
            "node_to_component"
        ][:2]
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.build_component_split_v1(
                manifest, old50_instance_ids=["old-a"]
            )

    def test_remote_plan_rejects_split_from_different_stage5_manifest(self) -> None:
        manifest = _manifest()
        split = response_v1.build_component_split_v1(
            manifest, old50_instance_ids=["old-a"]
        )
        split["source_stage5"]["content_sha256"] = "b" * 64
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.build_remote_training_plan_v1(
                manifest,
                split,
                root_protocol_reviewed=True,
                exact_dynamic_adapter_materialized_pass=True,
            )


class CausalCompilerTests(unittest.TestCase):
    def test_timing_and_emission_prefixes_are_distinct_and_causal(self) -> None:
        rows = response_v1.compile_wave_response_rows_v1(_wave())
        self.assertEqual(len(rows), 4)
        teammate_damage = next(
            row
            for row in rows
            if row["actor"]["player_guid"] == TEAMMATE
            and row["label"]["event_type"] == "DMG"
        )
        timing = teammate_damage["timing_state_after_previous_actor_event"]
        emission = teammate_damage["emission_state_before_current_event"]
        self.assertEqual(timing["prefix_trace_exclusive_index"], 2)
        self.assertEqual(
            timing["marked_activity"]["whole_team"]["marked_event_count_3000ms"],
            1,
        )
        self.assertEqual(emission["prefix_trace_exclusive_index"], 3)
        self.assertEqual(
            emission["marked_activity"]["whole_team"]["marked_event_count_3000ms"],
            2,
        )
        self.assertEqual(teammate_damage["label"]["exact_source_guid"], PET)
        self.assertEqual(
            teammate_damage["label"]["attribution_kind"], "EXACT_OFFICIAL_OWNER"
        )
        self.assertEqual(teammate_damage["label"]["target_mode"], "STAY_ALIVE")
        self.assertNotIn(TARGET_B, timing["target_state"]["alive_target_guids"])
        self.assertFalse(timing["future_event_or_death_visible"])

        last = rows[-1]
        self.assertEqual(last["actor"]["spec_status"], "SPEC_NOT_AVAILABLE_FROM_CURRENT_ARTIFACT")
        self.assertEqual(last["emission_state_before_current_event"]["target_state"]["dead_target_guids"], [TARGET_A])
        self.assertEqual(last["emission_state_before_current_event"]["target_state"]["alive_target_guids"], [TARGET_B])
        self.assertEqual(last["label"]["target_mode"], "SWITCH_ALIVE")

    def test_current_event_never_enters_emission_state(self) -> None:
        rows = response_v1.compile_wave_response_rows_v1(_wave())
        first = rows[0]
        self.assertEqual(first["trace_index"], 1)
        self.assertEqual(first["emission_state_before_current_event"]["prefix_trace_exclusive_index"], 1)
        self.assertEqual(
            first["emission_state_before_current_event"]["marked_activity"]["whole_team"]["marked_event_count_3000ms"],
            0,
        )
        self.assertEqual(
            first["timing_state_after_previous_actor_event"]["prefix_trace_exclusive_index"],
            0,
        )
        self.assertEqual(
            first["timing_state_after_previous_actor_event"]["target_state"]["alive_target_guids"],
            [],
        )

    def test_order_and_attribution_tampering_are_rejected(self) -> None:
        wave = _wave()
        wave["exact_trace"][2]["order_key"] = deepcopy(
            wave["exact_trace"][1]["order_key"]
        )
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.compile_wave_response_rows_v1(wave)
        wave = _wave()
        wave["exact_trace"][1]["event"]["attribution"]["player_guid"] = ACTOR
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.compile_wave_response_rows_v1(wave)


class ModelAndRuntimeTests(unittest.TestCase):
    def _damage_row(self) -> dict:
        return next(
            row
            for row in response_v1.compile_wave_response_rows_v1(_wave())
            if row["actor"]["player_guid"] == TEAMMATE
            and row["label"]["event_type"] == "DMG"
        )

    def test_guid_model_samples_separate_delay_and_live_emission_heads(self) -> None:
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        row = self._damage_row()
        for _ in range(5):
            model.update(row)
        delay = model.sample_delay(
            actor=row["actor"],
            timing_state=row["timing_state_after_previous_actor_event"],
            rng=random.Random(7),
        )
        emission = model.sample_emission(
            actor=row["actor"],
            emission_state=row["emission_state_before_current_event"],
            rng=random.Random(7),
        )
        self.assertEqual(delay["context_level"], "GUID")
        self.assertEqual(emission["context_level"], "GUID")
        self.assertEqual(emission["event_type"], "DMG")
        self.assertGreater(emission["sampled_damage"], 0)
        self.assertEqual(emission["exact_source_guid"], PET)

    def test_merge_preserves_sufficient_statistics(self) -> None:
        left = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        right = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        row = self._damage_row()
        left.update(row)
        right.update(row)
        left.merge(right)
        self.assertEqual(left.row_count, 2)
        sampled = left.sample_emission(
            actor=row["actor"],
            emission_state=row["emission_state_before_current_event"],
            rng=random.Random(1),
        )
        self.assertEqual(sampled["support"], 2)

    def test_ablation_arms_change_executable_contexts_and_keep_a_untouched(self) -> None:
        wave = _wave()
        control = response_v1.fixed_historical_exact_guid_schedule_control_v1(wave)
        self.assertEqual(control["variant"]["variant_id"], response_v1.ABLATION_A)
        self.assertEqual(control["exact_trace"], wave["exact_trace"])
        self.assertFalse(control["trace_mutated"])
        control["exact_trace"][0]["trace_index"] = 99
        self.assertEqual(wave["exact_trace"][0]["trace_index"], 0)
        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.HierarchicalMarkedSemiMarkovV1(
                variant_id=response_v1.ABLATION_A
            )

        row = self._damage_row()
        model_b = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_B,
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        model_b.update(row)
        self.assertFalse(any(key[0] == "GUID" for key in model_b.mark_counts))
        unseen_same_class_spec = deepcopy(row["actor"])
        unseen_same_class_spec["player_guid"] = "0x00000000000000B2"
        sampled_b = model_b.sample_emission(
            actor=unseen_same_class_spec,
            emission_state=row["emission_state_before_current_event"],
            rng=random.Random(3),
        )
        self.assertEqual(sampled_b["context_level"], "CLASS_SPEC")
        self.assertIsNone(sampled_b["exact_source_guid"])

        model_c = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_C,
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        model_c.update(row)
        self.assertTrue(any(key[0] == "GUID" for key in model_c.mark_counts))
        sampled_c = model_c.sample_emission(
            actor=row["actor"],
            emission_state=row["emission_state_before_current_event"],
            rng=random.Random(3),
        )
        self.assertEqual(sampled_c["context_level"], "GUID")

        changed_state = deepcopy(row["emission_state_before_current_event"])
        changed_state["marked_activity"]["other_team_including_unattributed"][
            "action_event_count_3000ms"
        ] = 99
        changed_state["marked_activity"]["other_team_including_unattributed"][
            "damage_amount_3000ms"
        ] = 999_999
        c_before = response_v1._context_keys(
            row["actor"], row["emission_state_before_current_event"], response_v1.ABLATION_C
        )
        c_after = response_v1._context_keys(
            row["actor"], changed_state, response_v1.ABLATION_C
        )
        d_before = response_v1._context_keys(
            row["actor"], row["emission_state_before_current_event"], response_v1.ABLATION_D
        )
        d_after = response_v1._context_keys(
            row["actor"], changed_state, response_v1.ABLATION_D
        )
        self.assertNotEqual(c_before, c_after)
        self.assertEqual(d_before, d_after)

        with self.assertRaises(response_v1.ChronicleExternalTeammateResponseModelV1Error):
            response_v1.ablation_variant_v1("E_UNKNOWN")

    def test_candidate_damage_changes_prefix_and_kill_clock_retargets(self) -> None:
        actors = [
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
            },
            {
                "player_guid": CANDIDATE,
                "class": "WARRIOR",
                "spec_key": "WARRIOR_FURY",
            },
        ]
        runtime = response_v1.DynamicTeamRuntimeV1(
            actors=actors,
            target_health_by_guid={TARGET_A: 20, TARGET_B: 50},
        )
        runtime.actors[TEAMMATE].current_target_guid = TARGET_A
        first = runtime.apply_event(
            time_ms=100,
            actor_guid=CANDIDATE,
            event_type="DMG",
            spell_id=1,
            spell_name="candidate-hit",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=CANDIDATE,
            target_mode="STAY_ALIVE",
            requested_damage=25,
            rng=random.Random(1),
            actor_role="CANDIDATE",
            explicit_target_guid=TARGET_A,
        )
        self.assertTrue(first["killed"])
        self.assertEqual(first["target_guid"], TARGET_A)
        self.assertIsNone(runtime.actors[TEAMMATE].current_target_guid)
        self.assertEqual(
            next(
                row["retargeted_to"]
                for row in first["retargets"]
                if row["actor_guid"] == TEAMMATE
            ),
            None,
        )
        state = runtime.snapshot_for_actor(TEAMMATE)
        self.assertEqual(state["target_state"]["dead_target_guids"], [TARGET_A])
        self.assertEqual(
            state["marked_activity"]["other_team_including_unattributed"]["damage_amount_3000ms"],
            20,
        )
        second = runtime.apply_event(
            time_ms=200,
            actor_guid=TEAMMATE,
            event_type="DMG",
            spell_id=2,
            spell_name="teammate-hit",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=TEAMMATE,
            target_mode="STAY_ALIVE",
            requested_damage=60,
            rng=random.Random(2),
            actor_role="TEAMMATE_MODEL",
        )
        self.assertEqual(second["target_guid"], TARGET_B)
        self.assertTrue(second["all_targets_dead"])
        self.assertEqual(second["kill_clock_ms"], 200)

    def test_retarget_after_death_is_sampled_at_next_live_emission(self) -> None:
        target_c = "0xF130000003000003"
        actors = [
            {"player_guid": TEAMMATE, "class": "MAGE", "spec_key": "MAGE_SPEC_NOT_AVAILABLE"},
            {"player_guid": CANDIDATE, "class": "WARRIOR", "spec_key": "WARRIOR_FURY"},
        ]
        runtime = response_v1.DynamicTeamRuntimeV1(
            actors=actors,
            target_health_by_guid={TARGET_A: 1, TARGET_B: 10, target_c: 10},
        )
        runtime.actors[TEAMMATE].current_target_guid = TARGET_A
        runtime.apply_event(
            time_ms=100,
            actor_guid=CANDIDATE,
            event_type="DMG",
            spell_id=1,
            spell_name="candidate-hit",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=CANDIDATE,
            target_mode="STAY_ALIVE",
            requested_damage=2,
            rng=random.Random(1),
            actor_role="CANDIDATE",
            explicit_target_guid=TARGET_A,
        )
        self.assertIsNone(runtime.actors[TEAMMATE].current_target_guid)
        next_event = runtime.apply_event(
            time_ms=200,
            actor_guid=TEAMMATE,
            event_type="GO",
            spell_id=2,
            spell_name="teammate-action",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=TEAMMATE,
            target_mode="STAY_ALIVE",
            requested_damage=0,
            rng=random.Random(0),
            actor_role="TEAMMATE_MODEL",
        )
        self.assertEqual(next_event["target_guid"], target_c)
        self.assertNotEqual(next_event["target_guid"], sorted([TARGET_B, target_c])[0])


class RemotePlanTests(unittest.TestCase):
    def test_plan_is_server_side_blocked_and_keeps_fixed_schedule_ablation(self) -> None:
        manifest = _manifest()
        split = response_v1.build_component_split_v1(
            manifest, old50_instance_ids=["old-a"]
        )
        plan = response_v1.build_remote_training_plan_v1(manifest, split)
        self.assertEqual(plan["status"], "BLOCKED_NOT_EXECUTED")
        self.assertFalse(plan["execution"]["launched"])
        self.assertEqual(plan["execution"]["whole_instance_task_count"], 4)
        self.assertEqual(plan["cost"]["exact_event_rows_scanned_once"], 1_000)
        variants = {row["variant_id"]: row for row in plan["ablation"]}
        self.assertEqual(
            variants[response_v1.ABLATION_A]["historical_eventmeta_schedule"],
            "REPLAY_UNCHANGED",
        )
        self.assertEqual(
            variants[response_v1.ABLATION_B]["context_levels"],
            ["CLASS_SPEC", "CLASS", "GLOBAL"],
        )
        self.assertEqual(
            variants[response_v1.ABLATION_C]["context_levels"][0], "GUID"
        )
        self.assertFalse(
            variants[response_v1.ABLATION_D][
                "include_other_team_action_and_damage_intensity"
            ]
        )
        self.assertEqual(
            set(plan["prerequisite_blockers"]),
            {
                "EXACT_DYNAMIC_ADAPTER_MATERIALIZED_ARTIFACT_PASS_PENDING",
                "ROOT_PROTOCOL_REVIEW_PENDING",
            },
        )
        ready = response_v1.build_remote_training_plan_v1(
            manifest,
            split,
            root_protocol_reviewed=True,
            exact_dynamic_adapter_materialized_pass=True,
        )
        self.assertEqual(ready["status"], "PREPARED_NOT_EXECUTED")
        self.assertEqual(ready["prerequisite_blockers"], [])


if __name__ == "__main__":
    unittest.main()
