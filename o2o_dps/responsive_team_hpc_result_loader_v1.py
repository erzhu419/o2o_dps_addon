"""Load a source-bound HPC teammate model for responsive bridge execution.

This loader materializes only the learned B/C/D runtime views.  It preserves
the reducer's development-only boundary and derives ``current_source_held_out``
only when both the Stage-5 source digest and validation component declaration
match the content-addressed reducer result.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import re
from typing import Any, Mapping

from . import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from . import chronicle_external_teammate_response_model_v1 as response_v1
from .responsive_team_bridge_adapter_v1 import (
    LoadedResponsiveTeammateModelV1,
    TeammateModelProvenanceV1,
)


LOADER_SCHEMA = "o2o_responsive_team_hpc_result_loader/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ResponsiveTeamHpcResultLoaderV1Error(RuntimeError):
    """The reducer result cannot support the requested runtime binding."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponsiveTeamHpcResultLoaderV1Error(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsiveTeamHpcResultLoaderV1Error(f"{label} must be nonempty text")
    return value


def _sha256(value: Any, label: str) -> str:
    result = _text(value, label)
    if _SHA256.fullmatch(result) is None:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            f"{label} must be lowercase SHA-256"
        )
    return result


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


@dataclass(frozen=True)
class CurrentSourceDeclarationV1:
    """The source/component whose held-out relationship is being asserted."""

    stage5_content_sha256: str
    component_id: str
    declared_held_out: bool

    def __post_init__(self) -> None:
        _sha256(self.stage5_content_sha256, "current source Stage-5 content SHA-256")
        _text(self.component_id, "current source component_id")
        if not isinstance(self.declared_held_out, bool):
            raise ResponsiveTeamHpcResultLoaderV1Error(
                "current source declared_held_out must be boolean"
            )


_EXPECTED_MODEL_VIEWS = {
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
}


def _validate_result_identity(result: Mapping[str, Any]) -> str:
    if result.get("schema") != hpc_v1.RESULT_SCHEMA:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "unsupported teammate-response HPC result schema"
        )
    if result.get("revision") != hpc_v1.REVISION:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "unsupported teammate-response HPC result revision"
        )
    try:
        result_sha = hpc_v1._verify_content_address(result, "HPC teammate result")
    except hpc_v1.TeammateResponseHpcV1Error as error:
        raise ResponsiveTeamHpcResultLoaderV1Error(str(error)) from error
    if result.get("status") != "DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION":
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "HPC teammate result status differs from the development contract"
        )
    return result_sha


def _validate_result_boundary(
    result: Mapping[str, Any], *, joint_row_count: int
) -> tuple[str, ...]:

    evaluations = _mapping(
        result.get("development_validation"), "development validation"
    )
    if set(evaluations) != set(hpc_v1.DYNAMIC_VARIANTS):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "development validation variant set differs"
        )
    validation_row_count: int | None = None
    for variant_id in hpc_v1.DYNAMIC_VARIANTS:
        evaluation = _mapping(evaluations[variant_id], f"{variant_id} evaluation")
        if (
            evaluation.get("variant_id") != variant_id
            or evaluation.get("status")
            != "DEVELOPMENT_METRICS_INCOMPLETE_NO_ADOPTION"
        ):
            raise ResponsiveTeamHpcResultLoaderV1Error(
                f"{variant_id} development evaluation binding differs"
            )
        weights = _mapping(
            evaluation.get("weights"), f"{variant_id} validation weights"
        )
        mark_weight = _integer(
            weights.get("mark"), f"{variant_id} validation mark weight", minimum=1
        )
        delay_weight = _integer(
            weights.get("delay"), f"{variant_id} validation delay weight", minimum=1
        )
        if mark_weight != delay_weight:
            raise ResponsiveTeamHpcResultLoaderV1Error(
                f"{variant_id} validation mark/delay row counts differ"
            )
        if validation_row_count is None:
            validation_row_count = mark_weight
        elif validation_row_count != mark_weight:
            raise ResponsiveTeamHpcResultLoaderV1Error(
                "development validation row counts differ across variants"
            )
    if validation_row_count is None:  # pragma: no cover - variant set is nonempty
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "development validation row count is absent"
        )

    completeness = _mapping(result.get("completeness"), "result completeness")
    expected_workers = _integer(
        completeness.get("expected_worker_count"), "expected worker count", minimum=1
    )
    if (
        completeness.get("observed_worker_count") != expected_workers
        or completeness.get("partition_scan_count") != expected_workers
        or completeness.get("duplicate_wave_count") != 0
        or completeness.get("compiled_exact_player_row_count")
        != joint_row_count + validation_row_count
        or completeness.get("old50_stage5_overlap_split") != "TRAIN_NONHELDOUT"
        or completeness.get("old50_absent_from_stage5_not_tasked") is not True
    ):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "HPC teammate result is incomplete or its source split differs"
        )
    raw_components = completeness.get("validation_component_ids")
    if not isinstance(raw_components, list):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "validation component ids must be an array"
        )
    validation_components = tuple(
        _text(value, "validation component_id") for value in raw_components
    )
    if (
        not validation_components
        or tuple(sorted(set(validation_components))) != validation_components
        or completeness.get("validation_component_count")
        != len(validation_components)
    ):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "validation component declaration is empty, duplicate, or inconsistent"
        )

    if (
        dict(_mapping(result.get("train_model_views"), "train model views"))
        != _EXPECTED_MODEL_VIEWS
    ):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "HPC teammate model materialization views differ"
        )
    adoption = _mapping(result.get("model_adoption"), "model adoption")
    boundary = _mapping(result.get("scientific_boundary"), "scientific boundary")
    if (
        adoption.get("authorized") is not False
        or adoption.get("failure_authorizes_adoption") is not False
        or boundary.get("development_validation_only") is not True
        or boundary.get("old50_stage5_overlap_nonheldout") is not True
        or boundary.get("comparison_eligible") is not False
        or boundary.get("voting_eligible") is not False
        or boundary.get("deployment_eligible") is not False
        or boundary.get("heavy_training_complete") is not True
    ):
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "HPC teammate scientific or adoption boundary differs"
        )
    return validation_components


def materialize_responsive_teammate_model_from_hpc_result_v1(
    result: Mapping[str, Any],
    *,
    variant_id: str,
    current_source: CurrentSourceDeclarationV1,
) -> LoadedResponsiveTeammateModelV1:
    """Validate one reducer result and materialize its selected B/C/D view."""

    document = _mapping(result, "HPC teammate result")
    if variant_id not in hpc_v1.DYNAMIC_VARIANTS:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "responsive runtime requires a learned B/C/D variant"
        )
    result_sha = _validate_result_identity(document)
    try:
        joint = hpc_v1.deserialize_joint_training_v4(
            _mapping(
                document.get("train_joint_sufficient_statistics"),
                "train joint sufficient statistics",
            )
        )
    except hpc_v1.TeammateResponseHpcV1Error as error:
        raise ResponsiveTeamHpcResultLoaderV1Error(str(error)) from error
    if joint.row_count <= 0:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "HPC teammate training statistics contain no rows"
        )
    validation_components = _validate_result_boundary(
        document, joint_row_count=joint.row_count
    )

    result_stage5_sha = _sha256(
        document.get("source_stage5_content_sha256"),
        "result source Stage-5 content SHA-256",
    )
    same_source = current_source.stage5_content_sha256 == result_stage5_sha
    in_validation_component = (
        same_source and current_source.component_id in validation_components
    )
    if current_source.declared_held_out != in_validation_component:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            "current source held-out declaration is not proven by the result's "
            "Stage-5 digest and validation component set"
        )

    try:
        model = hpc_v1.materialize_joint_variant_v4(joint, variant_id)
        serialized_model = hpc_v1.serialize_model_v1(model)
        model_sha = hpc_v1._canonical_sha256(serialized_model)
        model.model_content_sha256 = model_sha
    except hpc_v1.TeammateResponseHpcV1Error as error:
        raise ResponsiveTeamHpcResultLoaderV1Error(str(error)) from error
    provenance = TeammateModelProvenanceV1(
        source_artifact_schema=hpc_v1.RESULT_SCHEMA,
        source_artifact_content_sha256=result_sha,
        model_content_sha256=model_sha,
        variant_id=variant_id,
        training_scope="SOURCE_BOUND_STAGE5_TRAIN_COMPONENTS_DEVELOPMENT_ONLY",
        current_source_held_out=in_validation_component,
    )
    evidence = {
        "schema": f"{LOADER_SCHEMA}/current_source_evidence",
        "result_source_stage5_content_sha256": result_stage5_sha,
        "current_source_stage5_content_sha256": current_source.stage5_content_sha256,
        "current_source_component_id": current_source.component_id,
        "validation_component_ids": list(validation_components),
        "same_stage5_source": same_source,
        "component_in_validation_split": in_validation_component,
        "current_source_held_out": in_validation_component,
        "evidence_status": (
            "BOUND_SOURCE_VALIDATION_COMPONENT"
            if in_validation_component
            else "NOT_PROVEN_HELD_OUT_BY_THIS_RESULT"
        ),
    }
    return LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=provenance,
        result_content_sha256=result_sha,
        current_source_evidence=evidence,
    )


def load_responsive_teammate_model_from_hpc_path_v1(
    path: str | Path,
    *,
    variant_id: str,
    current_source: CurrentSourceDeclarationV1,
) -> LoadedResponsiveTeammateModelV1:
    """Read a reducer JSON or JSON.GZ and materialize one responsive model."""

    source = Path(path).expanduser().resolve()
    try:
        if source.suffix == ".gz":
            with gzip.open(source, "rt", encoding="utf-8") as stream:
                value = json.load(stream)
        else:
            with source.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResponsiveTeamHpcResultLoaderV1Error(
            f"cannot read HPC teammate result: {error}"
        ) from error
    return materialize_responsive_teammate_model_from_hpc_result_v1(
        _mapping(value, "HPC teammate result"),
        variant_id=variant_id,
        current_source=current_source,
    )


__all__ = [
    "CurrentSourceDeclarationV1",
    "LOADER_SCHEMA",
    "LoadedResponsiveTeammateModelV1",
    "ResponsiveTeamHpcResultLoaderV1Error",
    "load_responsive_teammate_model_from_hpc_path_v1",
    "materialize_responsive_teammate_model_from_hpc_result_v1",
]
