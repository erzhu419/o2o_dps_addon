from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.development_white6603_runtime_head_v8 import DevelopmentWhite6603CountHeadV8
from o2o_dps.development_white6603_opportunity_v8 import white6603_opportunity_from_prefix_v8
from o2o_dps.responsive_team_bridge_adapter_v1 import TeammateModelProvenanceV1
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
    LoadedResponsiveTeammateModelV1,
)
from o2o_dps.responsive_team_runtime_store_v1 import (
    ResponsiveTeamRuntimeStoreV1Error,
    SqliteResponsiveTeammateModelV1,
    build_responsive_team_runtime_store_v1,
    open_responsive_team_runtime_store_v1,
)
from o2o_dps.responsive_team_delay_origin_store_derivation_v1 import (
    OLD_REVISION,
    derive_delay_origin_runtime_store_v1,
)


_RESULT_SHA = hashlib.sha256(b"hpc-result").hexdigest()
_STAGE5_SHA = hashlib.sha256(b"stage5-source").hexdigest()
_VALIDATION_COMPONENT = "validation-component"
_TRAIN_COMPONENT = "train-component"


def _token(event_type: str, spell_id: int) -> str:
    return json.dumps(
        [event_type, spell_id, "DIRECT_FRIENDLY_PLAYER"],
        separators=(",", ":"),
    )


def _state(*, other_actions: int = 4, other_damage: int = 511) -> dict:
    return {
        "marked_activity": {
            "other_team_including_unattributed": {
                "action_event_count_3000ms": other_actions,
                "damage_amount_3000ms": other_damage,
            }
        },
        "target_state": {"alive_target_count": 2},
        "actor_last_mark_token": None,
    }


def _actor() -> dict:
    return {
        "player_guid": "0x00000000000000AA",
        "class": "Warrior",
        "spec_key": "Fury",
    }


def _model(variant_id: str) -> response_v1.HierarchicalMarkedSemiMarkovV1:
    model = response_v1.HierarchicalMarkedSemiMarkovV1(
        variant_id=variant_id,
        min_guid_events=1,
        min_class_spec_events=1,
        min_class_events=1,
    )
    actor = _actor()
    state = _state()
    damage_token = _token("DMG", 23881)
    go_token = _token("GO", 12323)
    mark_counts = {go_token: 2, damage_token: 5}
    for context in response_v1._context_keys(actor, state, variant_id):
        model.mark_counts[context].update(mark_counts)
        for token in mark_counts:
            model.target_counts[(context, token)].update(
                {"STAY_ALIVE": 4, "RETARGET_RANDOM_ALIVE": 3}
            )
            model.damage_counts[(context, token)].update({0: 2, 8: 5})
            model.target_damage_joint_counts[(context, token)].update(
                {
                    ("STAY_ALIVE", 0): 2,
                    ("STAY_ALIVE", 8): 2,
                    ("RETARGET_RANDOM_ALIVE", 8): 3,
                }
            )
    for context in response_v1._delay_context_keys(actor, state, variant_id):
        model.delay_counts[context].update({0: 2, 3: 3, 6: 2})
    model.spell_name_counts[damage_token].update(
        {"Bloodthirst": 5, "Heroic Strike": 2}
    )
    model.spell_name_counts[go_token].update({"Death Wish": 7})
    if "GUID" in model.variant["context_levels"]:
        model.source_guid_counts[(actor["player_guid"], damage_token)].update(
            {actor["player_guid"]: 5, None: 2}
        )
        model.source_guid_counts[(actor["player_guid"], go_token)].update(
            {actor["player_guid"]: 7}
        )
    model.row_count = 7
    return model


def _loaded(
    variant_id: str,
    *,
    current_component: str = _VALIDATION_COMPONENT,
    model_override: response_v1.HierarchicalMarkedSemiMarkovV1 | None = None,
) -> LoadedResponsiveTeammateModelV1:
    model = model_override if model_override is not None else _model(variant_id)
    serialized = hpc_v1.serialize_model_v1(model)
    model_sha = hpc_v1._canonical_sha256(serialized)
    model.model_content_sha256 = model_sha
    held_out = current_component == _VALIDATION_COMPONENT
    provenance = TeammateModelProvenanceV1(
        source_artifact_schema=hpc_v1.RESULT_SCHEMA,
        source_artifact_content_sha256=_RESULT_SHA,
        model_content_sha256=model_sha,
        variant_id=variant_id,
        training_scope="SOURCE_BOUND_STAGE5_TRAIN_COMPONENTS_DEVELOPMENT_ONLY",
        current_source_held_out=held_out,
    )
    return LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=provenance,
        result_content_sha256=_RESULT_SHA,
        current_source_evidence={
            "result_source_stage5_content_sha256": _STAGE5_SHA,
            "current_source_stage5_content_sha256": _STAGE5_SHA,
            "current_source_component_id": current_component,
            "validation_component_ids": [_VALIDATION_COMPONENT],
            "current_source_held_out": held_out,
        },
    )


def _actionability_model() -> response_v1.HierarchicalMarkedSemiMarkovV1:
    model = _model(response_v1.ABLATION_D)
    go_token = _token("GO", 12323)
    damage_token = _token("DMG", 23881)
    for context in response_v1._context_keys(_actor(), _state(), model.variant_id):
        go_joint = {
            ("NO_TARGET", 0): 1,
            ("NON_HOSTILE_OR_UNKNOWN", 0): 1,
        }
        damage_joint = {
            ("STAY_ALIVE", 8): 1,
            ("SWITCH_ALIVE", 8): 1,
            ("DEAD_TARGET_OBSERVED_DIAGNOSTIC", 8): 2,
            ("UNSEEN_HOSTILE_CURRENT_LABEL", 8): 1,
        }
        for token, joint in ((go_token, go_joint), (damage_token, damage_joint)):
            model.target_damage_joint_counts[(context, token)].clear()
            model.target_damage_joint_counts[(context, token)].update(joint)
            model.target_counts[(context, token)].clear()
            model.damage_counts[(context, token)].clear()
            for (mode, bucket), count in joint.items():
                model.target_counts[(context, token)][mode] += count
                model.damage_counts[(context, token)][bucket] += count
    return model


def _white_v8_actionability_model(
    *, white_actionable: bool = True
) -> response_v1.HierarchicalMarkedSemiMarkovV1:
    model = _actionability_model()
    white_token = _token("DMG", 6603)
    joint = (
        {
            ("NON_HOSTILE_OR_UNKNOWN", 0): 1,
            ("STAY_ALIVE", 8): 1,
            ("UNSEEN_HOSTILE_CURRENT_LABEL", 8): 2,
        }
        if white_actionable else
        {("UNSEEN_HOSTILE_CURRENT_LABEL", 8): 4}
    )
    for context in response_v1._context_keys(_actor(), _state(), model.variant_id):
        model.mark_counts[context][white_token] = 4
        for (mode, bucket), count in joint.items():
            model.target_damage_joint_counts[(context, white_token)][mode, bucket] = count
            model.target_counts[(context, white_token)][mode] += count
            model.damage_counts[(context, white_token)][bucket] += count
    for counts in model.delay_counts.values():
        counts[0] += 4
    model.spell_name_counts[white_token]["Auto Attack"] = 4
    if "GUID" in model.variant["context_levels"]:
        model.source_guid_counts[(_actor()["player_guid"], white_token)][
            _actor()["player_guid"]
        ] = 4
    model.row_count = 11
    return model


def _white_v8_state() -> dict:
    state = _state()
    state.update({
        "wave_elapsed_ms": 100,
        "white6603_phase": "FIRST",
        "white6603_age_ms": 100,
        "actor_has_prior_direct_hostile_start": False,
    })
    return state


def _white_v8_head() -> DevelopmentWhite6603CountHeadV8:
    context = ("CLASS_SPEC", "Warrior", "Fury", "FIRST", response_v1._delay_bucket(100), 0)
    return DevelopmentWhite6603CountHeadV8(
        {context: Counter({True: 9, False: 1})},
        minimums={"CLASS_SPEC": 1, "CLASS": 1, "CLASS_PHASE": 1, "GLOBAL": 1},
    )


class ResponsiveTeamRuntimeStoreV1Test(unittest.TestCase):
    def _build_and_open(
        self, directory: str, variant_id: str,
        model_override: response_v1.HierarchicalMarkedSemiMarkovV1 | None = None,
    ) -> tuple[
        LoadedResponsiveTeammateModelV1,
        LoadedResponsiveTeammateModelV1,
        Path,
        dict,
    ]:
        source = _loaded(variant_id, model_override=model_override)
        path = Path(directory) / f"{variant_id}.sqlite3"
        manifest = dict(build_responsive_team_runtime_store_v1(path, source))
        opened = open_responsive_team_runtime_store_v1(
            path,
            expected_result_content_sha256=_RESULT_SHA,
            expected_model_content_sha256=source.provenance.model_content_sha256,
            variant_id=variant_id,
            current_source=CurrentSourceDeclarationV1(
                stage5_content_sha256=_STAGE5_SHA,
                component_id=_VALIDATION_COMPONENT,
                declared_held_out=True,
            ),
        )
        return source, opened, path, manifest

    def test_no_v8_head_keeps_the_v7_actionable_seed_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, opened, _, _ = self._build_and_open(
                temporary, response_v1.ABLATION_D, _actionability_model()
            )
            try:
                observed = []
                for seed in range(8):
                    sampled = opened.model.sample_actionable_emission(
                        actor=_actor(), emission_state=_state(), rng=random.Random(seed)
                    )
                    self.assertNotIn("development_white6603_head_v8", sampled)
                    observed.append((
                        sampled["event_type"], sampled["spell_id"],
                        sampled["target_mode"], sampled["damage_bucket"],
                        sampled["spell_name"],
                    ))
                self.assertEqual(observed, [
                    ("GO", 12323, "NO_TARGET", 0, "Death Wish"),
                    ("DMG", 23881, "SWITCH_ALIVE", 8, "Bloodthirst"),
                    ("DMG", 23881, "STAY_ALIVE", 8, "Bloodthirst"),
                    ("DMG", 23881, "SWITCH_ALIVE", 8, "Bloodthirst"),
                    ("DMG", 23881, "SWITCH_ALIVE", 8, "Heroic Strike"),
                    ("GO", 12323, "NON_HOSTILE_OR_UNKNOWN", 0, "Death Wish"),
                    ("DMG", 23881, "STAY_ALIVE", 8, "Bloodthirst"),
                    ("GO", 12323, "NON_HOSTILE_OR_UNKNOWN", 0, "Death Wish"),
                ])
            finally:
                opened.model.close()

    def test_v8_white_head_conditions_on_actionable_joint_without_fabricating_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, opened, _, _ = self._build_and_open(
                temporary, response_v1.ABLATION_D, _white_v8_actionability_model()
            )
            try:
                baseline = opened.model.sample_actionable_emission(
                    actor=_actor(), emission_state=_state(), rng=random.Random(42)
                )
                head = _white_v8_head()
                pilot = white6603_opportunity_from_prefix_v8({
                    "actor": _actor(),
                    "emission_state_before_current_event": _white_v8_state(),
                    "label": {
                        "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
                        "event_type": "GO", "spell_id": 1,
                    },
                })
                self.assertEqual(
                    head.predict(actor=_actor(), emission_state=_white_v8_state())["context"],
                    list(pilot["contexts"][0]),
                )
                opened.model.use_development_white6603_head_v8(head)
                seen = set()
                for seed in range(256):
                    sampled = opened.model.sample_actionable_emission(
                        actor=_actor(), emission_state=_white_v8_state(),
                        rng=random.Random(seed),
                    )
                    audit = sampled["development_white6603_head_v8"]
                    self.assertEqual(audit["status"], "PREFIX_HEAD_CONDITIONED_ON_ACTIONABLE")
                    self.assertEqual(audit["head_alpha"], 1.0)
                    self.assertAlmostEqual(audit["raw_white_probability"], 10 / 12)
                    self.assertAlmostEqual(
                        audit["actionable_white_probability"],
                        (10 / 12 * 2 / 4) / (10 / 12 * 2 / 4 + 2 / 12 * 4 / 7),
                    )
                    self.assertEqual(sampled["spell_id"] == 6603, audit["sampled_white_branch"])
                    self.assertNotIn(sampled["target_mode"], {
                        "DEAD_TARGET_OBSERVED_DIAGNOSTIC",
                        "UNSEEN_HOSTILE_CURRENT_LABEL",
                    })
                    if sampled["spell_id"] == 6603:
                        self.assertIn(
                            (sampled["target_mode"], sampled["damage_bucket"]),
                            {("NON_HOSTILE_OR_UNKNOWN", 0), ("STAY_ALIVE", 8)},
                        )
                    seen.add((sampled["spell_id"], sampled["target_mode"]))
                self.assertIn((6603, "NON_HOSTILE_OR_UNKNOWN"), seen)
                self.assertIn((6603, "STAY_ALIVE"), seen)
                self.assertTrue(any(spell_id != 6603 for spell_id, _ in seen))
                self.assertTrue(any(
                    opened.model.sample_emission(
                        actor=_actor(), emission_state=_white_v8_state(),
                        rng=random.Random(seed),
                    )["target_mode"] == "UNSEEN_HOSTILE_CURRENT_LABEL"
                    for seed in range(128)
                ))

                no_support = _white_v8_state()
                no_support["white6603_phase"] = "REPEAT"
                backoff = opened.model.sample_actionable_emission(
                    actor=_actor(), emission_state=no_support, rng=random.Random(42)
                )
                self.assertEqual(
                    backoff.pop("development_white6603_head_v8"),
                    {"status": "NO_PREFIX_SUPPORT_V7_BACKOFF"},
                )
                self.assertEqual(backoff, {key: value for key, value in baseline.items()
                                           if key != "development_white6603_head_v8"})
                selected_half = DevelopmentWhite6603CountHeadV8(
                    head.counts, minimums=head.minimums, alpha=0.5
                )
                opened.model.use_development_white6603_head_v8(selected_half)
                half = opened.model.sample_actionable_emission(
                    actor=_actor(), emission_state=_white_v8_state(),
                    rng=random.Random(42),
                )["development_white6603_head_v8"]
                self.assertEqual(half["head_alpha"], 0.5)
                self.assertAlmostEqual(half["raw_white_probability"], 9.5 / 11)
            finally:
                opened.model.close()

    def test_v8_white_head_with_diagnostic_only_white_has_zero_actionable_probability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, opened, _, _ = self._build_and_open(
                temporary, response_v1.ABLATION_D,
                _white_v8_actionability_model(white_actionable=False),
            )
            try:
                opened.model.use_development_white6603_head_v8(_white_v8_head())
                for seed in range(64):
                    sampled = opened.model.sample_actionable_emission(
                        actor=_actor(), emission_state=_white_v8_state(),
                        rng=random.Random(seed),
                    )
                    self.assertNotEqual(sampled["spell_id"], 6603)
                    self.assertEqual(
                        sampled["development_white6603_head_v8"]["actionable_white_probability"],
                        0.0,
                    )
            finally:
                opened.model.close()

    def test_target_choice_head_matches_read_only_sqlite_sampler(self) -> None:
        variant_id = response_v1.ABLATION_C
        model = _model(variant_id)
        state = _state()
        state["target_state"]["target_choice_candidates"] = [
            {
                "target_guid": "0xA", "first_activity_ms": 0,
                "prefix_damage": 20, "recent_direct_start_count_3000ms": 2,
                "recent_damage_amount_3000ms": 20,
            },
            {
                "target_guid": "0xB", "first_activity_ms": 100,
                "prefix_damage": 0, "recent_direct_start_count_3000ms": 0,
                "recent_damage_amount_3000ms": 0,
            },
        ]
        features = response_v1._target_choice_features(
            state["target_state"]["target_choice_candidates"]
        )
        for context in response_v1._context_keys(_actor(), state, variant_id):
            model.target_choice_counts[
                (context, "START_FIRST_ACQUISITION", features["0xA"])
            ][True] += 5
            model.target_choice_counts[
                (context, "START_FIRST_ACQUISITION", features["0xB"])
            ][False] += 5
            model.target_choice_counts[
                (context, "WHITE6603_FIRST_ACQUISITION", features["0xA"])
            ][False] += 7
            model.target_choice_counts[
                (context, "WHITE6603_FIRST_ACQUISITION", features["0xB"])
            ][True] += 7
        with tempfile.TemporaryDirectory() as temporary:
            source, opened, _path, _manifest = self._build_and_open(
                temporary, variant_id, model_override=model
            )
            args = {
                "actor": _actor(), "emission_state": state,
                "target_mode": "SWITCH_ALIVE", "intent_kind": "START",
                "eligible_target_guids": ["0xA", "0xB"],
                "current_target_guid": None,
            }
            for seed in range(50):
                self.assertEqual(
                    source.model.sample_target_choice(**args, rng=random.Random(seed)),
                    opened.model.sample_target_choice(**args, rng=random.Random(seed)),
                )
                white_args = {**args, "intent_kind": "WHITE6603"}
                self.assertEqual(
                    source.model.sample_target_choice(
                        **white_args, rng=random.Random(seed)
                    ),
                    opened.model.sample_target_choice(
                        **white_args, rng=random.Random(seed)
                    ),
                )
            self.assertIsNone(
                opened.model.sample_target_choice(
                    **{**args, "eligible_target_guids": ["0xA"]},
                    rng=random.Random(1),
                )
            )
            opened.model.close()

    def test_sqlite_sampler_is_exact_for_b_c_and_d_rng_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for variant_id in hpc_v1.DYNAMIC_VARIANTS:
                source, opened, _path, manifest = self._build_and_open(
                    temporary, variant_id
                )
                self.assertIsInstance(
                    opened.model, SqliteResponsiveTeammateModelV1
                )
                self.assertEqual(source.provenance, opened.provenance)
                self.assertFalse(
                    opened.current_source_evidence[
                        "rollout_worker_loaded_reducer_json"
                    ]
                )
                self.assertFalse(
                    manifest["scientific_boundary"]["store_build_proves_held_out"]
                )
                for seed in range(100):
                    self.assertEqual(
                        source.model.sample_delay(
                            actor=_actor(),
                            timing_state=_state(),
                            rng=random.Random(seed),
                        ),
                        opened.model.sample_delay(
                            actor=_actor(),
                            timing_state=_state(),
                            rng=random.Random(seed),
                        ),
                    )
                    self.assertEqual(
                        source.model.sample_emission(
                            actor=_actor(),
                            emission_state=_state(),
                            rng=random.Random(seed),
                        ),
                        opened.model.sample_emission(
                            actor=_actor(),
                            emission_state=_state(),
                            rng=random.Random(seed),
                        ),
                    )
                opened.model.close()

    def test_sqlite_global_first_wake_cannot_sample_followup_zero_delay(self) -> None:
        model = _model(response_v1.ABLATION_D)
        model.minimums.update({"GUID": 100, "CLASS_SPEC": 100, "CLASS": 100})
        model.delay_counts.clear()
        model.delay_counts[("GLOBAL", "WAVE_START")][5] = 1
        model.delay_counts[("GLOBAL", "PREVIOUS_ACTOR_EVENT")][0] = 6
        with tempfile.TemporaryDirectory() as temporary:
            source = _loaded(response_v1.ABLATION_D, model_override=model)
            path = Path(temporary) / "separate-delay-heads.sqlite3"
            build_responsive_team_runtime_store_v1(path, source)
            opened = open_responsive_team_runtime_store_v1(
                path,
                expected_result_content_sha256=_RESULT_SHA,
                expected_model_content_sha256=source.provenance.model_content_sha256,
                variant_id=response_v1.ABLATION_D,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=_STAGE5_SHA,
                    component_id=_VALIDATION_COMPONENT,
                    declared_held_out=True,
                ),
            )
            for state, expected_origin, bucket in (
                (_state(), "WAVE_START", 5),
                ({**_state(), "actor_last_mark_token": _token("GO", 1)},
                 "PREVIOUS_ACTOR_EVENT", 0),
            ):
                sampled = opened.model.sample_delay(
                    actor=_actor(), timing_state=state, rng=random.Random(9)
                )
                self.assertEqual(sampled["context"], ["GLOBAL", expected_origin])
                self.assertEqual(sampled["delay_bucket"], bucket)
            opened.model.close()

    def test_old_pooled_global_delay_head_cannot_build_new_store(self) -> None:
        model = _model(response_v1.ABLATION_D)
        model.delay_counts.clear()
        model.delay_counts[("GLOBAL",)][0] = model.row_count
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                ResponsiveTeamRuntimeStoreV1Error,
                "lacks delay-origin-specific global support",
            ):
                build_responsive_team_runtime_store_v1(
                    Path(temporary) / "old-head.sqlite3",
                    _loaded(response_v1.ABLATION_D, model_override=model),
                )

    def test_frozen_pooled_store_derives_exact_origin_heads_without_retraining(self) -> None:
        model = _model(response_v1.ABLATION_D)
        model.minimums.update({"GUID": 100, "CLASS_SPEC": 100, "CLASS": 100})
        model.delay_counts.clear()
        first = _state()
        later = {**_state(), "actor_last_mark_token": _token("GO", 1)}
        for context in response_v1._delay_context_keys(
            _actor(), first, model.variant_id
        ):
            model.delay_counts[context][5] = 1
        for context in response_v1._delay_context_keys(
            _actor(), later, model.variant_id
        ):
            model.delay_counts[context][0] = 6
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "frozen-v2.sqlite3"
            build_responsive_team_runtime_store_v1(
                source_path, _loaded(response_v1.ABLATION_D, model_override=model)
            )
            # Recreate the exact old pooled-delay key layout in this tiny store.
            with sqlite3.connect(source_path) as connection:
                pooled: dict[tuple, dict[int, int]] = {}
                rows = connection.execute(
                    "SELECT distribution_id, key_json FROM distribution_lookup "
                    "WHERE head = 'delay_counts'"
                ).fetchall()
                for distribution_id, key_json in rows:
                    key = tuple(json.loads(key_json))
                    old_key = (key[0], *key[2:])
                    counts = pooled.setdefault(old_key, {})
                    for value_json, count in connection.execute(
                        "SELECT value_json, count FROM distribution_choice "
                        "WHERE distribution_id = ?", (distribution_id,)
                    ):
                        bucket = json.loads(value_json)
                        counts[bucket] = counts.get(bucket, 0) + count
                    connection.execute(
                        "DELETE FROM distribution_choice WHERE distribution_id = ?",
                        (distribution_id,),
                    )
                    connection.execute(
                        "DELETE FROM distribution_lookup WHERE distribution_id = ?",
                        (distribution_id,),
                    )
                for key, counts in pooled.items():
                    cursor = connection.execute(
                        "INSERT INTO distribution_lookup "
                        "(head, key_json, support) VALUES (?, ?, ?)",
                        ("delay_counts", json.dumps(key, separators=(",", ":")),
                         sum(counts.values())),
                    )
                    connection.executemany(
                        "INSERT INTO distribution_choice "
                        "(distribution_id, ordinal, value_json, count) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            (cursor.lastrowid, ordinal, json.dumps(bucket), count)
                            for ordinal, (bucket, count) in enumerate(sorted(counts.items()))
                        ),
                    )
                old_manifest = json.loads(connection.execute(
                    "SELECT value_json FROM metadata WHERE key = 'manifest'"
                ).fetchone()[0])
                old_manifest["revision"] = OLD_REVISION
                connection.execute(
                    "UPDATE metadata SET value_json = ? WHERE key = 'manifest'",
                    (json.dumps(old_manifest),),
                )
            connection.close()
            destination = Path(temporary) / "derived-v3.sqlite3"
            manifest = derive_delay_origin_runtime_store_v1(source_path, destination)
            self.assertEqual(manifest["derivation"]["first_global_support"], 1)
            self.assertEqual(manifest["derivation"]["followup_global_support"], 6)
            self.assertNotEqual(
                manifest["model_content_sha256"], old_manifest["model_content_sha256"]
            )
            with sqlite3.connect(source_path) as connection:
                unchanged = json.loads(connection.execute(
                    "SELECT value_json FROM metadata WHERE key = 'manifest'"
                ).fetchone()[0])
            connection.close()
            self.assertEqual(unchanged["revision"], OLD_REVISION)
            opened = open_responsive_team_runtime_store_v1(
                destination,
                expected_result_content_sha256=_RESULT_SHA,
                expected_model_content_sha256=manifest["model_content_sha256"],
                variant_id=response_v1.ABLATION_D,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=_STAGE5_SHA,
                    component_id=_VALIDATION_COMPONENT,
                    declared_held_out=True,
                ),
            )
            self.assertEqual(opened.model.sample_delay(
                actor=_actor(), timing_state=first, rng=random.Random(0)
            )["delay_bucket"], 5)
            self.assertEqual(opened.model.sample_delay(
                actor=_actor(), timing_state=later, rng=random.Random(0)
            )["delay_bucket"], 0)
            opened.model.close()
            with sqlite3.connect(source_path) as connection:
                class_first = connection.execute(
                    "SELECT distribution_id FROM distribution_lookup "
                    "WHERE head = 'delay_counts' AND key_json LIKE ?",
                    ('["CLASS",%',),
                ).fetchall()
                first_id = next(
                    distribution_id for (distribution_id,) in class_first
                    if json.loads(connection.execute(
                        "SELECT key_json FROM distribution_lookup "
                        "WHERE distribution_id = ?", (distribution_id,)
                    ).fetchone()[0])[2] == "__NONE__"
                )
                connection.execute(
                    "UPDATE distribution_lookup SET support = support + 1 "
                    "WHERE distribution_id = ?", (first_id,)
                )
                connection.execute(
                    "UPDATE distribution_choice SET count = count + 1 "
                    "WHERE distribution_id = ?", (first_id,)
                )
            connection.close()
            rejected = Path(temporary) / "rejected.sqlite3"
            with self.assertRaisesRegex(
                ResponsiveTeamRuntimeStoreV1Error,
                "CLASS delay distribution does not reconstruct old GLOBAL",
            ):
                derive_delay_origin_runtime_store_v1(source_path, rejected)
            self.assertFalse(rejected.exists())

    def test_actionable_projection_conditions_joint_counts_without_rewriting_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _loaded(
                response_v1.ABLATION_D, model_override=_actionability_model()
            )
            path = Path(temporary) / "actionable.sqlite3"
            build_responsive_team_runtime_store_v1(path, source)
            opened = open_responsive_team_runtime_store_v1(
                path,
                expected_result_content_sha256=_RESULT_SHA,
                expected_model_content_sha256=source.provenance.model_content_sha256,
                variant_id=response_v1.ABLATION_D,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=_STAGE5_SHA,
                    component_id=_VALIDATION_COMPONENT,
                    declared_held_out=True,
                ),
            )
            try:
                seen = set()
                for seed in range(256):
                    sampled = opened.model.sample_actionable_emission(
                        actor=_actor(), emission_state=_state(), rng=random.Random(seed)
                    )
                    seen.add(sampled["target_mode"])
                    self.assertEqual(sampled["context_level"], "GUID")
                    self.assertEqual(sampled["support"], 7)
                    self.assertEqual(
                        sampled["runtime_actionability_projection"],
                        {
                            "excluded_diagnostic_support": 3,
                            "actionable_support": 4,
                        },
                    )
                    self.assertEqual(
                        sampled["target_damage_joint_support"],
                        2 if sampled["event_type"] == "GO" else 5,
                    )
                self.assertEqual(
                    seen,
                    {"NO_TARGET", "NON_HOSTILE_OR_UNKNOWN", "STAY_ALIVE", "SWITCH_ALIVE"},
                )
                self.assertEqual(len(opened.model._actionable_projection_cache), 1)
                self.assertTrue(
                    any(
                        opened.model.sample_emission(
                            actor=_actor(), emission_state=_state(), rng=random.Random(seed)
                        )["target_mode"]
                        == "DEAD_TARGET_OBSERVED_DIAGNOSTIC"
                        for seed in range(256)
                    )
                )
                self.assertEqual(
                    source.provenance.model_content_sha256,
                    opened.provenance.model_content_sha256,
                )
            finally:
                opened.model.close()

    def test_actionable_projection_backs_off_from_diagnostic_only_guid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = _actionability_model()
            guid_context = response_v1._context_keys(
                _actor(), _state(), model.variant_id
            )[0]
            go_token = _token("GO", 12323)
            damage_token = _token("DMG", 23881)
            for token, count in ((go_token, 2), (damage_token, 5)):
                model.target_damage_joint_counts[(guid_context, token)].clear()
                model.target_damage_joint_counts[(guid_context, token)].update(
                    {("DEAD_TARGET_OBSERVED_DIAGNOSTIC", 8): count}
                )
            source = _loaded(model.variant_id, model_override=model)
            path = Path(temporary) / "fallback.sqlite3"
            build_responsive_team_runtime_store_v1(path, source)
            opened = open_responsive_team_runtime_store_v1(
                path,
                expected_result_content_sha256=_RESULT_SHA,
                expected_model_content_sha256=source.provenance.model_content_sha256,
                variant_id=model.variant_id,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=_STAGE5_SHA,
                    component_id=_VALIDATION_COMPONENT,
                    declared_held_out=True,
                ),
            )
            try:
                sampled = opened.model.sample_actionable_emission(
                    actor=_actor(), emission_state=_state(), rng=random.Random(1)
                )
                self.assertEqual(sampled["context_level"], "CLASS_SPEC")
                self.assertEqual(sampled["runtime_actionability_projection"], {
                    "excluded_diagnostic_support": 3,
                    "actionable_support": 4,
                })
                self.assertEqual(len(opened.model._actionable_projection_cache), 2)
            finally:
                opened.model.close()

    def test_store_contains_normalized_distributions_not_reducer_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, opened, path, manifest = self._build_and_open(
                temporary, response_v1.ABLATION_C
            )
            opened.model.close()
            self.assertGreater(manifest["distribution_count"], 0)
            self.assertGreater(manifest["choice_count"], 0)
            self.assertEqual(
                manifest["model_content_sha256"],
                source.provenance.model_content_sha256,
            )
            connection = sqlite3.connect(path)
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertEqual(
                    names,
                    {"metadata", "distribution_lookup", "distribution_choice"},
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM metadata"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_open_rejects_identity_or_heldout_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _loaded(response_v1.ABLATION_C)
            path = Path(temporary) / "model.sqlite3"
            build_responsive_team_runtime_store_v1(path, source)
            common = {
                "path": path,
                "expected_result_content_sha256": _RESULT_SHA,
                "expected_model_content_sha256": source.provenance.model_content_sha256,
                "variant_id": response_v1.ABLATION_C,
            }
            with self.assertRaisesRegex(
                ResponsiveTeamRuntimeStoreV1Error, "identity differs"
            ):
                open_responsive_team_runtime_store_v1(
                    **{**common, "expected_model_content_sha256": "0" * 64},
                    current_source=CurrentSourceDeclarationV1(
                        stage5_content_sha256=_STAGE5_SHA,
                        component_id=_VALIDATION_COMPONENT,
                        declared_held_out=True,
                    ),
                )
            with self.assertRaisesRegex(
                ResponsiveTeamRuntimeStoreV1Error,
                "held-out declaration is not proven",
            ):
                open_responsive_team_runtime_store_v1(
                    **common,
                    current_source=CurrentSourceDeclarationV1(
                        stage5_content_sha256=_STAGE5_SHA,
                        component_id=_TRAIN_COMPONENT,
                        declared_held_out=True,
                    ),
                )

    def test_nonheldout_source_remains_nonheldout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _loaded(
                response_v1.ABLATION_D, current_component=_TRAIN_COMPONENT
            )
            path = Path(temporary) / "model.sqlite3"
            build_responsive_team_runtime_store_v1(path, source)
            opened = open_responsive_team_runtime_store_v1(
                path,
                expected_result_content_sha256=_RESULT_SHA,
                expected_model_content_sha256=source.provenance.model_content_sha256,
                variant_id=response_v1.ABLATION_D,
                current_source=CurrentSourceDeclarationV1(
                    stage5_content_sha256=_STAGE5_SHA,
                    component_id=_TRAIN_COMPONENT,
                    declared_held_out=False,
                ),
            )
            try:
                self.assertFalse(opened.provenance.current_source_held_out)
                self.assertEqual(
                    opened.current_source_evidence["evidence_status"],
                    "NOT_PROVEN_HELD_OUT_BY_THIS_RESULT",
                )
            finally:
                opened.model.close()

    def test_builder_does_not_replace_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.sqlite3"
            path.write_bytes(b"owned")
            with self.assertRaisesRegex(
                ResponsiveTeamRuntimeStoreV1Error, "already exists"
            ):
                build_responsive_team_runtime_store_v1(
                    path, _loaded(response_v1.ABLATION_B)
                )
            self.assertEqual(path.read_bytes(), b"owned")


if __name__ == "__main__":
    unittest.main()
