from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
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
        model.delay_counts[context].update({0: 2, 3: 3, 6: 2})
        for token in mark_counts:
            model.target_counts[(context, token)].update(
                {"STAY_ALIVE": 4, "RETARGET_RANDOM_ALIVE": 3}
            )
            model.damage_counts[(context, token)].update({0: 2, 8: 5})
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
    variant_id: str, *, current_component: str = _VALIDATION_COMPONENT
) -> LoadedResponsiveTeammateModelV1:
    model = _model(variant_id)
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


class ResponsiveTeamRuntimeStoreV1Test(unittest.TestCase):
    def _build_and_open(
        self, directory: str, variant_id: str
    ) -> tuple[
        LoadedResponsiveTeammateModelV1,
        LoadedResponsiveTeammateModelV1,
        Path,
        dict,
    ]:
        source = _loaded(variant_id)
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
