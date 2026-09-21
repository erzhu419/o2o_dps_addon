from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
import random
import tempfile
import unittest

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
    ResponsiveTeamHpcResultLoaderV1Error,
    load_responsive_teammate_model_from_hpc_path_v1,
    materialize_responsive_teammate_model_from_hpc_result_v1,
)
from tests.test_chronicle_external_teammate_response_model_v1 import _wave


STAGE5_SHA = "5" * 64
VALIDATION_COMPONENT = "component-validation"
VALIDATION_ROW_COUNT = 7


def _synthetic_result() -> dict:
    joint = hpc_v1._JointTrainingCountsV4(
        min_guid_events=1,
        min_class_spec_events=1,
        min_class_events=1,
    )
    for row in response_v1.iter_wave_response_sufficient_rows_v1(_wave()):
        joint.update(row)
    serialized = hpc_v1.serialize_joint_training_v4(joint)
    row_count = serialized["row_count"]
    core = {
        "schema": hpc_v1.RESULT_SCHEMA,
        "revision": hpc_v1.REVISION,
        "status": "DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION",
        "source_stage5_content_sha256": STAGE5_SHA,
        "completeness": {
            "expected_worker_count": 2,
            "observed_worker_count": 2,
            "partition_scan_count": 2,
            "duplicate_wave_count": 0,
            "compiled_exact_player_row_count": row_count + VALIDATION_ROW_COUNT,
            "validation_component_ids": [VALIDATION_COMPONENT],
            "validation_component_count": 1,
            "old50_stage5_overlap_split": "TRAIN_NONHELDOUT",
            "old50_absent_from_stage5_not_tasked": True,
        },
        "train_joint_sufficient_statistics": serialized,
        "train_model_views": {
            response_v1.ABLATION_B: {
                "materialization": "PROJECT_C_V4_DROP_GUID_AND_SOURCE_GUID",
                "exact": True,
            },
            response_v1.ABLATION_C: {
                "materialization": "BASE_C_V4_JOINT_TARGET_DAMAGE",
                "exact": True,
            },
            response_v1.ABLATION_D: {
                "materialization": "C_V4_CLASS_GLOBAL_PLUS_D_GUID_CLASS_SPEC_DELTA",
                "shared_spell_and_source_guid_tables_from_c": True,
                "exact": True,
            },
        },
        "development_validation": {
            variant_id: {
                "variant_id": variant_id,
                "status": "DEVELOPMENT_METRICS_INCOMPLETE_NO_ADOPTION",
                "weights": {
                    "mark": VALIDATION_ROW_COUNT,
                    "delay": VALIDATION_ROW_COUNT,
                    "target": VALIDATION_ROW_COUNT - 1,
                    "positive_damage": 2,
                    "dynamic_kill_clock": 0,
                },
            }
            for variant_id in hpc_v1.DYNAMIC_VARIANTS
        },
        "model_adoption": {
            "authorized": False,
            "failure_authorizes_adoption": False,
        },
        "scientific_boundary": {
            "development_validation_only": True,
            "old50_stage5_overlap_nonheldout": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
            "heavy_training_complete": True,
        },
    }
    return hpc_v1._content_addressed(core)


class ResponsiveTeamHpcResultLoaderV1Tests(unittest.TestCase):
    @staticmethod
    def _readdress(result: dict) -> dict:
        core = deepcopy(result)
        core.pop("content_address", None)
        return hpc_v1._content_addressed(core)

    def test_materializes_each_dynamic_variant_with_bound_provenance(self) -> None:
        result = _synthetic_result()
        declaration = CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=VALIDATION_COMPONENT,
            declared_held_out=True,
        )
        result_sha = result["content_address"]["sha256"]
        reference_row = next(
            response_v1.iter_wave_response_sufficient_rows_v1(_wave())
        )

        for variant_id in hpc_v1.DYNAMIC_VARIANTS:
            with self.subTest(variant_id=variant_id):
                loaded = materialize_responsive_teammate_model_from_hpc_result_v1(
                    result,
                    variant_id=variant_id,
                    current_source=declaration,
                )
                self.assertEqual(variant_id, loaded.model.variant_id)
                self.assertGreater(loaded.model.row_count, 0)
                self.assertEqual(result_sha, loaded.result_content_sha256)
                self.assertEqual(
                    result_sha,
                    loaded.provenance.source_artifact_content_sha256,
                )
                self.assertEqual(variant_id, loaded.provenance.variant_id)
                self.assertTrue(loaded.provenance.current_source_held_out)
                self.assertEqual(
                    "BOUND_SOURCE_VALIDATION_COMPONENT",
                    loaded.current_source_evidence["evidence_status"],
                )
                delay = loaded.model.sample_delay(
                    actor=reference_row["actor"],
                    timing_state=reference_row[
                        "timing_state_after_previous_actor_event"
                    ],
                    rng=random.Random(1),
                )
                emission = loaded.model.sample_emission(
                    actor=reference_row["actor"],
                    emission_state=reference_row[
                        "emission_state_before_current_event"
                    ],
                    rng=random.Random(2),
                )
                self.assertGreaterEqual(delay["delay_ms"], 0)
                self.assertGreater(delay["support"], 0)
                self.assertGreater(emission["support"], 0)

    def test_does_not_infer_held_out_across_source_or_component(self) -> None:
        result = _synthetic_result()
        for declaration in (
            CurrentSourceDeclarationV1(
                stage5_content_sha256="6" * 64,
                component_id=VALIDATION_COMPONENT,
                declared_held_out=True,
            ),
            CurrentSourceDeclarationV1(
                stage5_content_sha256=STAGE5_SHA,
                component_id="component-train-or-unresolved",
                declared_held_out=True,
            ),
            CurrentSourceDeclarationV1(
                stage5_content_sha256=STAGE5_SHA,
                component_id=VALIDATION_COMPONENT,
                declared_held_out=False,
            ),
        ):
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(
                    ResponsiveTeamHpcResultLoaderV1Error,
                    "held-out declaration is not proven",
                ):
                    materialize_responsive_teammate_model_from_hpc_result_v1(
                        result,
                        variant_id=response_v1.ABLATION_C,
                        current_source=declaration,
                    )

        unresolved = materialize_responsive_teammate_model_from_hpc_result_v1(
            result,
            variant_id=response_v1.ABLATION_C,
            current_source=CurrentSourceDeclarationV1(
                stage5_content_sha256="6" * 64,
                component_id="external-component",
                declared_held_out=False,
            ),
        )
        self.assertFalse(unresolved.provenance.current_source_held_out)
        self.assertEqual(
            "NOT_PROVEN_HELD_OUT_BY_THIS_RESULT",
            unresolved.current_source_evidence["evidence_status"],
        )

    def test_rejects_revision_and_content_address_drift(self) -> None:
        declaration = CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=VALIDATION_COMPONENT,
            declared_held_out=True,
        )
        wrong_revision = _synthetic_result()
        wrong_revision["revision"] = "future-revision"
        with self.assertRaisesRegex(
            ResponsiveTeamHpcResultLoaderV1Error, "revision"
        ):
            materialize_responsive_teammate_model_from_hpc_result_v1(
                wrong_revision,
                variant_id=response_v1.ABLATION_C,
                current_source=declaration,
            )

        wrong_address = _synthetic_result()
        wrong_address["source_stage5_content_sha256"] = "7" * 64
        with self.assertRaisesRegex(
            ResponsiveTeamHpcResultLoaderV1Error, "content address"
        ):
            materialize_responsive_teammate_model_from_hpc_result_v1(
                wrong_address,
                variant_id=response_v1.ABLATION_C,
                current_source=declaration,
            )

    def test_rejects_invalid_validation_row_accounting(self) -> None:
        declaration = CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=VALIDATION_COMPONENT,
            declared_held_out=True,
        )
        mutations = (
            (
                "compiled total",
                lambda result: result["completeness"].__setitem__(
                    "compiled_exact_player_row_count",
                    result["completeness"]["compiled_exact_player_row_count"] - 1,
                ),
                "incomplete or its source split differs",
            ),
            (
                "mark versus delay",
                lambda result: result["development_validation"][
                    response_v1.ABLATION_B
                ]["weights"].__setitem__("delay", VALIDATION_ROW_COUNT - 1),
                "mark/delay row counts differ",
            ),
            (
                "cross variant",
                lambda result: result["development_validation"][
                    response_v1.ABLATION_D
                ]["weights"].update(
                    {
                        "mark": VALIDATION_ROW_COUNT + 1,
                        "delay": VALIDATION_ROW_COUNT + 1,
                    }
                ),
                "row counts differ across variants",
            ),
            (
                "nonpositive",
                lambda result: result["development_validation"][
                    response_v1.ABLATION_C
                ]["weights"].update({"mark": 0, "delay": 0}),
                "validation mark weight must be an integer >= 1",
            ),
            (
                "boolean",
                lambda result: result["development_validation"][
                    response_v1.ABLATION_C
                ]["weights"].update({"mark": True, "delay": True}),
                "validation mark weight must be an integer >= 1",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                result = _synthetic_result()
                mutate(result)
                result = self._readdress(result)
                with self.assertRaisesRegex(
                    ResponsiveTeamHpcResultLoaderV1Error, message
                ):
                    materialize_responsive_teammate_model_from_hpc_result_v1(
                        result,
                        variant_id=response_v1.ABLATION_C,
                        current_source=declaration,
                    )

    def test_reads_small_gzip_result_without_a_large_artifact_dependency(self) -> None:
        result = _synthetic_result()
        declaration = CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=VALIDATION_COMPONENT,
            declared_held_out=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-result.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False)
            loaded = load_responsive_teammate_model_from_hpc_path_v1(
                path,
                variant_id=response_v1.ABLATION_D,
                current_source=declaration,
            )
        self.assertEqual(response_v1.ABLATION_D, loaded.model.variant_id)
        self.assertTrue(loaded.provenance.current_source_held_out)


if __name__ == "__main__":
    unittest.main()
