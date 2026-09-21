from __future__ import annotations

from copy import deepcopy
import random
import unittest

from o2o_dps import chronicle_external_team_wave_model_v2 as wave_model_v2
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.development_white6603_opportunity_v8 import iter_white6603_opportunities_v8


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
    def test_white6603_prefix_features_exclude_current_label_in_both_row_views(self) -> None:
        wave = _wave()
        wave["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _event(1, 50, TEAMMATE, "START", TARGET_A, spell_id=100),
            _event(
                2, 60, ACTOR, "DMG", TARGET_A, spell_id=6603,
                attribution="EXACT_OFFICIAL_OWNER", source_guid=PET, damage=3,
            ),
            _event(3, 100, ACTOR, "DMG", TARGET_A, spell_id=6603, damage=4),
            _event(4, 110, ACTOR, "START", TARGET_A, spell_id=23881),
            _event(5, 110, ACTOR, "DMG", TARGET_A, spell_id=6603, damage=5),
            _event(6, 120, ACTOR, "DMG", TARGET_A, spell_id=6603, damage=0),
            _event(7, 130, ACTOR, "GO", TARGET_A, spell_id=100),
        ]
        wave["exact_trace"][6]["event"]["target"] = _target(
            TARGET_A, "UNKNOWN_NONVOTING"
        )
        full = response_v1.compile_wave_response_rows_v1(wave)
        sufficient = list(response_v1.iter_wave_response_sufficient_rows_v1(wave))
        keys = (
            "wave_elapsed_ms", "actor_last_direct_white6603_ms",
            "time_since_actor_direct_white6603_ms",
            "actor_has_prior_direct_hostile_start", "white6603_phase",
            "white6603_age_ms",
        )
        for left, right in zip(full, sufficient):
            self.assertEqual(
                {key: left["emission_state_before_current_event"][key] for key in keys},
                {key: right["emission_state_before_current_event"][key] for key in keys},
            )
        for row, opportunity in zip(
            sufficient, iter_white6603_opportunities_v8(sufficient)
        ):
            state = row["emission_state_before_current_event"]
            self.assertEqual(state["wave_elapsed_ms"], opportunity["elapsed_ms"])
            self.assertEqual(state["white6603_phase"], opportunity["phase"])
            self.assertEqual(state["white6603_age_ms"], opportunity["age_ms"])
            self.assertEqual(
                state["actor_has_prior_direct_hostile_start"],
                opportunity["prior_direct_hostile_start"],
            )
        owner, first_white, start, second_white, nonhostile_white, after_nonhostile = full[1:]
        owner_state = owner["emission_state_before_current_event"]
        self.assertEqual(owner_state["white6603_age_ms"], 60)
        self.assertFalse(owner_state["actor_has_prior_direct_hostile_start"])
        first_state = first_white["emission_state_before_current_event"]
        self.assertEqual(first_state["white6603_phase"], "FIRST")
        self.assertEqual(first_state["white6603_age_ms"], 100)
        self.assertIsNone(first_state["actor_last_direct_white6603_ms"])
        self.assertFalse(first_state["actor_has_prior_direct_hostile_start"])
        start_state = start["emission_state_before_current_event"]
        self.assertEqual(start_state["white6603_phase"], "REPEAT")
        self.assertEqual(start_state["actor_last_direct_white6603_ms"], 100)
        self.assertEqual(start_state["time_since_actor_direct_white6603_ms"], 10)
        self.assertFalse(start_state["actor_has_prior_direct_hostile_start"])
        second_state = second_white["emission_state_before_current_event"]
        self.assertEqual(second_state["white6603_age_ms"], 10)
        self.assertTrue(second_state["actor_has_prior_direct_hostile_start"])
        after_second = second_white["timing_state_after_previous_actor_event"]
        self.assertEqual(after_second["actor_last_direct_white6603_ms"], 100)
        self.assertEqual(
            nonhostile_white["emission_state_before_current_event"][
                "actor_last_direct_white6603_ms"
            ], 110,
        )
        self.assertEqual(
            after_nonhostile["emission_state_before_current_event"][
                "actor_last_direct_white6603_ms"
            ], 120,
        )

    def test_white6603_first_and_retarget_are_separate_prefix_choices(self) -> None:
        target_c = "0xF130000003000003"
        wave = _wave()
        wave["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _classification(1, 0, TARGET_B),
            _classification(2, 0, target_c),
            _event(3, 100, TEAMMATE, "START", TARGET_A, spell_id=100),
            _event(4, 150, TEAMMATE, "START", TARGET_B, spell_id=100),
            _event(5, 200, TEAMMATE, "START", target_c, spell_id=100),
            _event(6, 300, ACTOR, "DMG", TARGET_A, spell_id=6603, damage=30),
            _event(7, 400, ACTOR, "DMG", TARGET_B, spell_id=6603, damage=25),
        ]
        rows = response_v1.compile_wave_response_rows_v1(wave)
        sufficient = list(response_v1.iter_wave_response_sufficient_rows_v1(wave))
        first, second = rows[-2:]
        self.assertEqual(
            first["emission_state_before_current_event"]["target_state"]["target_choice_candidates"],
            sufficient[-2]["emission_state_before_current_event"]["target_state"]["target_choice_candidates"],
        )
        first_choice = response_v1._target_choice_training_observations(first)
        second_choice = response_v1._target_choice_training_observations(second)
        self.assertEqual(first_choice[0], "WHITE6603_FIRST_ACQUISITION")
        self.assertEqual(first_choice[2], TARGET_A)
        self.assertEqual(set(first_choice[1]), {TARGET_A, TARGET_B, target_c})
        self.assertEqual(second_choice[0], "WHITE6603_RETARGET")
        self.assertEqual(second_choice[2], TARGET_B)
        self.assertEqual(set(second_choice[1]), {TARGET_B, target_c})
        stayed = deepcopy(second)
        stayed["label"]["target_guid"] = TARGET_A
        stayed["label"]["target_mode"] = "STAY_ALIVE"
        self.assertIsNone(response_v1._target_choice_training_observations(stayed))
        self.assertEqual(
            second["emission_state_before_current_event"]["actor_last_target_guid"],
            TARGET_A,
        )
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        model.update(first)
        model.update(second)
        common = {
            "actor": first["actor"],
            "emission_state": first["emission_state_before_current_event"],
            "target_mode": "SWITCH_ALIVE",
            "eligible_target_guids": [TARGET_A, TARGET_B, target_c],
            "current_target_guid": None,
        }
        self.assertIsNone(
            model.sample_target_choice(
                **common, intent_kind="START", rng=random.Random(1)
            )
        )
        self.assertIsNotNone(
            model.sample_target_choice(
                **common, intent_kind="WHITE6603", rng=random.Random(1)
            )
        )

        unseen = _wave()
        unseen["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _classification(1, 0, TARGET_B),
            _event(2, 100, ACTOR, "DMG", TARGET_A, spell_id=6603, damage=30),
        ]
        self.assertIsNone(
            response_v1._target_choice_training_observations(
                response_v1.compile_wave_response_rows_v1(unseen)[0]
            )
        )
        owner = _wave()
        owner["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _event(
                1, 100, ACTOR, "DMG", TARGET_A, spell_id=6603,
                attribution="EXACT_OFFICIAL_OWNER", source_guid=PET, damage=30,
            ),
        ]
        owner_row = response_v1.compile_wave_response_rows_v1(owner)[0]
        self.assertIsNone(response_v1._target_choice_training_observations(owner_row))

    def test_splash_damage_never_retargets_direct_start_focus(self) -> None:
        wave = _wave()
        wave["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _classification(1, 0, TARGET_B),
            _event(2, 100, TEAMMATE, "START", TARGET_A, spell_id=1680),
            _event(3, 200, TEAMMATE, "DMG", TARGET_B, spell_id=1680, damage=12),
            _event(4, 250, TEAMMATE, "DMG", TARGET_A, spell_id=1680, damage=12),
            _event(5, 300, TEAMMATE, "START", TARGET_A, spell_id=23881),
        ]
        rows = response_v1.compile_wave_response_rows_v1(wave)
        self.assertEqual(rows[-1]["label"]["target_mode"], "STAY_ALIVE")
        self.assertEqual(
            rows[-1]["emission_state_before_current_event"]["actor_last_target_guid"],
            TARGET_A,
        )

    def test_target_choice_labels_use_only_prior_active_targets(self) -> None:
        wave = _wave()
        wave["exact_trace"] = [
            _classification(0, 0, TARGET_A),
            _classification(1, 0, TARGET_B),
            _event(2, 100, TEAMMATE, "START", TARGET_A, spell_id=100),
            _event(3, 200, TEAMMATE, "START", TARGET_B, spell_id=100),
            _event(4, 250, TEAMMATE, "START", TARGET_A, spell_id=100),
            _event(5, 300, ACTOR, "START", TARGET_A, spell_id=200),
        ]
        row = response_v1.compile_wave_response_rows_v1(wave)[-1]
        sufficient = list(response_v1.iter_wave_response_sufficient_rows_v1(wave))[-1]
        self.assertEqual(
            row["emission_state_before_current_event"]["target_state"]["target_choice_candidates"],
            sufficient["emission_state_before_current_event"]["target_state"]["target_choice_candidates"],
        )
        phase, features, positive = response_v1._target_choice_training_observations(row)
        self.assertEqual((phase, positive), ("START_FIRST_ACQUISITION", TARGET_A))
        self.assertEqual(set(features), {TARGET_A, TARGET_B})
        self.assertEqual(features[TARGET_A][0], "ACTION_LEADER")
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        model.update(row)
        state = row["emission_state_before_current_event"]
        choices = [
            model.sample_target_choice(
                actor=row["actor"], emission_state=state,
                target_mode="SWITCH_ALIVE", intent_kind="START",
                eligible_target_guids=[TARGET_A, TARGET_B],
                current_target_guid=None, rng=random.Random(seed),
            )["target_guid"]
            for seed in range(100)
        ]
        self.assertGreater(choices.count(TARGET_A), choices.count(TARGET_B))
        self.assertEqual(choices, [
            model.sample_target_choice(
                actor=row["actor"], emission_state=state,
                target_mode="SWITCH_ALIVE", intent_kind="START",
                eligible_target_guids=[TARGET_A, TARGET_B],
                current_target_guid=None, rng=random.Random(seed),
            )["target_guid"]
            for seed in range(100)
        ])

        # The first START on a third, previously unseen hostile produces no
        # positive and no fabricated negative candidates.
        first_unseen = deepcopy(row)
        first_unseen["label"]["target_guid"] = "0xUNSEEN"
        first_unseen["label"]["target_mode"] = "UNSEEN_HOSTILE_CURRENT_LABEL"
        self.assertIsNone(
            response_v1._target_choice_training_observations(first_unseen)
        )

    def test_duplicate_candidate_features_do_not_multiply_head_support(self) -> None:
        wave = _wave()
        row = response_v1.compile_wave_response_rows_v1(wave)[1]
        state = deepcopy(row["emission_state_before_current_event"])
        state["target_state"]["target_choice_candidates"] = [
            {"target_guid": target, "first_activity_ms": 0,
             "prefix_damage": 0, "recent_direct_start_count_3000ms": 0,
             "recent_damage_amount_3000ms": 0}
            for target in (TARGET_A, TARGET_B)
        ]
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=2, min_class_spec_events=2, min_class_events=2
        )
        feature = response_v1._target_choice_features(
            state["target_state"]["target_choice_candidates"]
        )[TARGET_A]
        for context in response_v1._context_keys(row["actor"], state, model.variant_id):
            model.target_choice_counts[(context, "START_FIRST_ACQUISITION", feature)][True] = 1
            model.target_choice_counts[(context, "WHITE6603_FIRST_ACQUISITION", feature)][True] = 1
        selected = model.sample_target_choice(
            actor=row["actor"], emission_state=state,
            target_mode="SWITCH_ALIVE", intent_kind="START",
            eligible_target_guids=[TARGET_A, TARGET_B],
            current_target_guid=None, rng=random.Random(0),
        )
        self.assertEqual(selected["context_level"], "GLOBAL")
        self.assertEqual(selected["support"], 1)
        white_selected = model.sample_target_choice(
            actor=row["actor"], emission_state=state,
            target_mode="SWITCH_ALIVE", intent_kind="WHITE6603",
            eligible_target_guids=[TARGET_A, TARGET_B],
            current_target_guid=None, rng=random.Random(0),
        )
        self.assertEqual(white_selected["context_level"], "GLOBAL")
        self.assertEqual(white_selected["support"], 1)

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
        # A GO is not a directed focus command; an owner/pet DMG cannot
        # establish the player's current hostile target either.
        self.assertEqual(teammate_damage["label"]["target_mode"], "SWITCH_ALIVE")
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
    def test_runtime_white6603_prefix_uses_the_same_prior_event_clock(self) -> None:
        runtime = response_v1.DynamicTeamRuntimeV1(
            actors=[{"player_guid": TEAMMATE, "class": "WARRIOR", "spec_key": "WARRIOR_FURY"}],
            target_health_by_guid={TARGET_A: 100},
        )
        initial = runtime.snapshot_for_actor(TEAMMATE)
        self.assertEqual(initial["white6603_phase"], "FIRST")
        self.assertEqual(initial["white6603_age_ms"], 0)
        runtime.apply_event(
            time_ms=100, actor_guid=TEAMMATE, event_type="DMG", spell_id=6603,
            spell_name="Auto Attack", attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=TEAMMATE, target_mode="STAY_ALIVE",
            observed_damage=1, requested_damage=1, rng=random.Random(1),
            actor_role="TEAMMATE_MODEL", explicit_target_guid=TARGET_A,
        )
        after = runtime.snapshot_for_actor(TEAMMATE)
        self.assertEqual(after["actor_last_direct_white6603_ms"], 100)
        self.assertEqual(after["white6603_phase"], "REPEAT")
        self.assertEqual(after["white6603_age_ms"], 0)
        runtime.advance_to(250)
        self.assertEqual(runtime.snapshot_for_actor(TEAMMATE)["white6603_age_ms"], 150)

    def test_live_aoe_result_targets_do_not_replace_start_focus(self) -> None:
        runtime = response_v1.DynamicTeamRuntimeV1(
            actors=[{"player_guid": TEAMMATE, "class": "WARRIOR", "spec_key": "WARRIOR_FURY"}],
            target_health_by_guid={TARGET_A: 100, TARGET_B: 100},
        )
        common = {
            "actor_guid": TEAMMATE,
            "spell_name": "test",
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "exact_source_guid": TEAMMATE,
            "rng": random.Random(3),
            "actor_role": "TEAMMATE_MODEL",
        }
        runtime.apply_event(
            **common, time_ms=100, event_type="START", spell_id=1680,
            target_mode="SWITCH_ALIVE", observed_damage=0,
            requested_damage=0, explicit_target_guid=TARGET_A,
        )
        runtime.apply_event(
            **common, time_ms=200, event_type="DMG", spell_id=1680,
            target_mode="SWITCH_ALIVE", observed_damage=10,
            requested_damage=10, explicit_target_guid=TARGET_B,
        )
        self.assertEqual(runtime.actors[TEAMMATE].current_target_guid, TARGET_A)
        self.assertEqual(
            runtime.snapshot_for_actor(TEAMMATE)["actor_last_target_guid"], TARGET_A
        )
        runtime.apply_event(
            **common, time_ms=300, event_type="START", spell_id=23881,
            target_mode="STAY_ALIVE", observed_damage=0,
            requested_damage=0, explicit_target_guid=TARGET_A,
        )
        self.assertEqual(runtime.actors[TEAMMATE].current_target_guid, TARGET_A)

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

    def test_first_delay_global_fallback_excludes_followup_zero_delays(self) -> None:
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=100, min_class_spec_events=100, min_class_events=100
        )
        rows = response_v1.compile_wave_response_rows_v1(_wave())
        for row in rows:
            row = deepcopy(row)
            row["label"]["inter_event_delay_ms"] = (
                4_000 if row["label"]["delay_origin"] == "WAVE_START" else 0
            )
            model.update(row)
        first = next(
            row for row in rows if row["label"]["delay_origin"] == "WAVE_START"
        )
        later = next(
            row for row in rows
            if row["label"]["delay_origin"] == "PREVIOUS_ACTOR_EVENT"
        )
        self.assertEqual(
            model.delay_counts[("GLOBAL", "WAVE_START")],
            {response_v1._delay_bucket(4_000): 2},
        )
        self.assertEqual(
            model.delay_counts[("GLOBAL", "PREVIOUS_ACTOR_EVENT")], {0: 2}
        )
        first_draw = model.sample_delay(
            actor=first["actor"],
            timing_state=first["timing_state_after_previous_actor_event"],
            rng=random.Random(3),
        )
        later_draw = model.sample_delay(
            actor=later["actor"],
            timing_state=later["timing_state_after_previous_actor_event"],
            rng=random.Random(3),
        )
        self.assertEqual(first_draw["context"], ["GLOBAL", "WAVE_START"])
        self.assertEqual(first_draw["delay_bucket"], response_v1._delay_bucket(4_000))
        self.assertEqual(later_draw["context"], ["GLOBAL", "PREVIOUS_ACTOR_EVENT"])
        self.assertEqual(later_draw["delay_ms"], 0)

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

    def test_runtime_samples_only_empirical_target_damage_pairs(self) -> None:
        model = response_v1.HierarchicalMarkedSemiMarkovV1(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        hostile = self._damage_row()
        non_hostile_zero = deepcopy(hostile)
        non_hostile_zero["label"]["target_mode"] = "NO_TARGET"
        non_hostile_zero["label"]["damage_amount"] = 0
        model.update(hostile)
        model.update(non_hostile_zero)

        hostile_bucket = response_v1._damage_bucket(
            hostile["label"]["damage_amount"]
        )
        observed_pairs = {
            (
                sample["target_mode"],
                sample["damage_bucket"],
            )
            for seed in range(500)
            for sample in (
                model.sample_emission(
                    actor=hostile["actor"],
                    emission_state=hostile["emission_state_before_current_event"],
                    rng=random.Random(seed),
                ),
            )
        }
        self.assertEqual(
            observed_pairs,
            {("SWITCH_ALIVE", hostile_bucket), ("NO_TARGET", 0)},
        )

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
            observed_damage=25,
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
            25,
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
            observed_damage=60,
            requested_damage=60,
            rng=random.Random(2),
            actor_role="TEAMMATE_MODEL",
        )
        self.assertEqual(second["target_guid"], TARGET_B)
        self.assertTrue(second["all_targets_dead"])
        self.assertEqual(second["kill_clock_ms"], 200)

    def test_future_target_introduction_is_causal_and_not_a_kill_clock(self) -> None:
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
            target_health_by_guid={TARGET_A: 5, TARGET_B: 9},
            target_introduced_at_ms_by_guid={TARGET_A: 0, TARGET_B: 200},
        )
        initial = runtime.snapshot_for_actor(TEAMMATE)
        self.assertEqual(initial["target_state"]["alive_target_guids"], [TARGET_A])
        self.assertEqual(
            [row["target_guid"] for row in initial["target_state"]["targets"]],
            [TARGET_A],
        )

        first = runtime.apply_event(
            time_ms=50,
            actor_guid=CANDIDATE,
            event_type="DMG",
            spell_id=1,
            spell_name="candidate-hit",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=CANDIDATE,
            target_mode="STAY_ALIVE",
            observed_damage=5,
            requested_damage=5,
            rng=random.Random(1),
            actor_role="CANDIDATE",
            explicit_target_guid=TARGET_A,
        )
        self.assertTrue(first["killed"])
        self.assertFalse(first["all_targets_dead"])
        self.assertIsNone(runtime.kill_clock_ms)
        self.assertEqual(runtime.alive_target_guids(), [])
        self.assertEqual(runtime.remaining_target_guids(), [TARGET_B])

        with self.assertRaisesRegex(
            response_v1.ChronicleExternalTeammateResponseModelV1Error,
            "hostile runtime damage requires a live target",
        ):
            runtime.apply_event(
                time_ms=100,
                actor_guid=TEAMMATE,
                event_type="DMG",
                spell_id=2,
                spell_name="too-early",
                attribution_kind="DIRECT_FRIENDLY_PLAYER",
                exact_source_guid=TEAMMATE,
                target_mode="STAY_ALIVE",
                observed_damage=9,
                requested_damage=9,
                rng=random.Random(2),
                actor_role="TEAMMATE_MODEL",
                explicit_target_guid=TARGET_B,
            )
        self.assertEqual(runtime.health[TARGET_B], 9)
        self.assertNotIn(
            TARGET_B,
            runtime.snapshot_for_actor(TEAMMATE)["target_state"]["alive_target_guids"],
        )

        runtime.advance_to(200)
        introduced = runtime.snapshot_for_actor(TEAMMATE)
        self.assertEqual(introduced["target_state"]["alive_target_guids"], [TARGET_B])
        self.assertEqual(
            next(
                row["first_seen_ms"]
                for row in introduced["target_state"]["targets"]
                if row["target_guid"] == TARGET_B
            ),
            200,
        )
        final = runtime.apply_event(
            time_ms=200,
            actor_guid=TEAMMATE,
            event_type="DMG",
            spell_id=3,
            spell_name="on-time",
            attribution_kind="DIRECT_FRIENDLY_PLAYER",
            exact_source_guid=TEAMMATE,
            target_mode="STAY_ALIVE",
            observed_damage=9,
            requested_damage=9,
            rng=random.Random(3),
            actor_role="TEAMMATE_MODEL",
            explicit_target_guid=TARGET_B,
        )
        self.assertTrue(final["all_targets_dead"])
        self.assertEqual(final["kill_clock_ms"], 200)

    def test_future_target_introduction_registry_must_match_health_registry(self) -> None:
        with self.assertRaisesRegex(
            response_v1.ChronicleExternalTeammateResponseModelV1Error,
            "must cover exactly",
        ):
            response_v1.DynamicTeamRuntimeV1(
                actors=(
                    {
                        "player_guid": TEAMMATE,
                        "class": "MAGE",
                        "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
                    },
                ),
                target_health_by_guid={TARGET_A: 5, TARGET_B: 9},
                target_introduced_at_ms_by_guid={TARGET_A: 0},
            )

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
            observed_damage=2,
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
            observed_damage=0,
            requested_damage=0,
            rng=random.Random(0),
            actor_role="TEAMMATE_MODEL",
        )
        self.assertEqual(next_event["target_guid"], target_c)
        self.assertNotEqual(next_event["target_guid"], sorted([TARGET_B, target_c])[0])

    def test_observed_non_hostile_damage_enters_activity_without_target_damage(
        self,
    ) -> None:
        actors = [
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
            }
        ]
        for target_mode in ("NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"):
            with self.subTest(target_mode=target_mode):
                runtime = response_v1.DynamicTeamRuntimeV1(
                    actors=actors,
                    target_health_by_guid={TARGET_A: 10},
                )
                runtime.actors[TEAMMATE].current_target_guid = TARGET_A

                transition = runtime.apply_event(
                    time_ms=100,
                    actor_guid=TEAMMATE,
                    event_type="DMG",
                    spell_id=57660,
                    spell_name="Mana Detonation",
                    attribution_kind="DIRECT_FRIENDLY_PLAYER",
                    exact_source_guid=TEAMMATE,
                    target_mode=target_mode,
                    observed_damage=7,
                    requested_damage=0,
                    rng=random.Random(1),
                    actor_role="TEAMMATE_MODEL",
                )

                self.assertIsNone(transition["target_guid"])
                self.assertEqual(7.0, transition["observed_damage"])
                self.assertEqual(0.0, transition["requested_damage"])
                self.assertEqual(0.0, transition["applied_damage"])
                self.assertEqual(0.0, transition["overkill_damage"])
                self.assertEqual(10.0, runtime.health[TARGET_A])
                self.assertEqual(
                    runtime.actors[TEAMMATE].current_target_guid, TARGET_A
                )
                snapshot = runtime.snapshot_for_actor(TEAMMATE)
                self.assertEqual(
                    7,
                    snapshot["marked_activity"]["actor"][
                        "damage_amount_3000ms"
                    ],
                )
                self.assertEqual(
                    0,
                    snapshot["target_state"]["targets"][0]["prefix_damage"],
                )

    def test_diagnostic_target_modes_never_resolve_to_random_live_target(self) -> None:
        actors = [
            {
                "player_guid": TEAMMATE,
                "class": "MAGE",
                "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
            }
        ]
        for target_mode in (
            "DEAD_TARGET_OBSERVED_DIAGNOSTIC",
            "UNSEEN_HOSTILE_CURRENT_LABEL",
        ):
            with self.subTest(target_mode=target_mode):
                runtime = response_v1.DynamicTeamRuntimeV1(
                    actors=actors,
                    target_health_by_guid={TARGET_A: 10, TARGET_B: 20},
                )
                with self.assertRaisesRegex(
                    response_v1.ChronicleExternalTeammateResponseModelV1Error,
                    "diagnostic target mode is not an actionable live target",
                ):
                    runtime.apply_event(
                        time_ms=100,
                        actor_guid=TEAMMATE,
                        event_type="DMG",
                        spell_id=57660,
                        spell_name="diagnostic",
                        attribution_kind="DIRECT_FRIENDLY_PLAYER",
                        exact_source_guid=TEAMMATE,
                        target_mode=target_mode,
                        observed_damage=7,
                        requested_damage=7,
                        rng=random.Random(1),
                        actor_role="TEAMMATE_MODEL",
                    )
                self.assertEqual({TARGET_A: 10.0, TARGET_B: 20.0}, runtime.health)
                self.assertEqual(0, runtime.time_ms)


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
