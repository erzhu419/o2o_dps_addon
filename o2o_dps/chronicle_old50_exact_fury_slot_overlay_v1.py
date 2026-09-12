"""Compile the frozen old-50 exact-Fury selections into wave episodes.

This additive artifact joins the truthful V2 selector to the already frozen
Stage-5 player episodes, Stage-6 whole-wave schedules, and old-50 overlap
audit.  Only the twenty selected instance partitions are opened, once each
for Stage 5 and Stage 6.  The accepted overlap rows, rather than partition
position or a count prefix, determine the 470 exact waves.

PDF-selected players remain outcome-conditioned top-player discovery data.
Generic selections remain outcome-free identity discovery.  Both lanes are
eligible only as expert-training discovery episodes at this layer: neither is
held-out performance evidence or a comparison result.  Projected teammate
events are environment inputs and are never exported as policy features.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping, Sequence, TextIO

from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_wave_model_v2 as model_v2
from . import chronicle_old50_warrior_slot_substitution_v1 as slots_v1
from . import chronicle_old50_warrior_slot_substitution_v2 as selector_v2
from . import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1


RECORD_SCHEMA = "chronicle_old50_exact_fury_slot_overlay_wave/v1"
MANIFEST_SCHEMA = "chronicle_old50_exact_fury_slot_overlay_manifest/v1"
REVISION = "selected_guid_prefix_episode_exact_loo_old50_schedule_v1"
STATUS = "COMPLETE_EXPERT_TRAINING_DISCOVERY_OVERLAY_NONVOTING"
MANIFEST_PREFIX = "chronicle_old50_exact_fury_slot_overlay_v1"

ACCEPTED_OVERLAP_CLASSIFICATIONS = frozenset({"EXACT", "SUBSET"})

EXPECTED_INSTANCE_COUNT = 20
EXPECTED_WAVE_OVERLAY_COUNT = 470
EXPECTED_LANE_INSTANCE_COUNTS = {
    selector_v2.GENERIC_LANE: 14,
    selector_v2.PDF_LANE: 6,
}
EXPECTED_SELECTOR_V2_CONTENT_SHA256 = (
    "893c89a0f5cca84dbd6156959cffc3cebe466169069d0ce71630c285630de271"
)
INSTANCE_SUMMARY_FIELDS = (
    "wave_overlay_count",
    "prefix_transition_count",
    "action_trace_ref_count",
    "outcome_context_trace_ref_count",
    "target_trace_ref_count",
    "projected_teammate_event_count",
    "projected_teammate_damage",
    "excluded_focal_event_count",
    "excluded_focal_damage",
    "zero_transition_wave_count",
    "zero_teammate_schedule_wave_count",
    "selected_guid_missing_wave_count",
)


def _expected_selection_semantics(lane: str) -> dict[str, Any]:
    is_pdf = lane == selector_v2.PDF_LANE
    return {
        "leaderboard_membership_used_for_selection": is_pdf,
        "leaderboard_dps_rank_used_for_selection": is_pdf,
        "outcome_conditioned_discovery": is_pdf,
        "same_instance_ranking_dps_values_used": False,
        "generic_selection_outcome_free": not is_pdf,
        "purpose": (
            "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY"
            if is_pdf
            else "OUTCOME_FREE_EXACT_FURY_IDENTITY_DISCOVERY_ONLY"
        ),
        "heldout_performance_evidence_eligible": False,
        "comparison_outcome_eligible": False,
    }


class ChronicleOld50ExactFurySlotOverlayV1Error(RuntimeError):
    """The exact-GUID wave overlay could not be compiled fail-closed."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, label: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be an integer"
        )
    if nonnegative and value < 0:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be nonnegative"
        )
    return value


def _nonnegative_number(value: Any, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} must be a nonnegative number"
        )
    return value


def _sha(value: Any, label: str) -> str:
    try:
        return slots_v1._sha(value, label=label)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _canonical(value: Any) -> bytes:
    try:
        return slots_v1._canonical(value)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    return slots_v1._content_addressed(core)


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    try:
        return slots_v1._verify_content_address(value, label=label)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _verify_model_content_address(value: Mapping[str, Any], label: str) -> str:
    try:
        return model_v2._verify_content_address(value, label=label)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _verify_background_content_address(
    value: Mapping[str, Any], label: str
) -> str:
    try:
        return background_v2._verify_content_address(value, label=label)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_canonical_json(path_value: str | Path, label: str) -> tuple[dict[str, Any], Path, str]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} is not a regular file"
        )
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"invalid {label}: {error}"
        ) from error
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} is not canonical JSON plus LF"
        )
    return value, path, hashlib.sha256(payload).hexdigest()


def _partition_path(manifest_path: Path, entry: Mapping[str, Any], label: str) -> Path:
    partition = _mapping(entry.get("partition"), f"{label} partition")
    relative = _text(partition.get("path"), f"{label} partition path")
    pure = PurePosixPath(relative)
    if pure.name != relative or any(part in {"", ".", ".."} for part in pure.parts):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} partition must be colocated with its manifest"
        )
    path = (manifest_path.parent / relative).resolve()
    try:
        path.relative_to(manifest_path.parent.resolve())
    except ValueError as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} partition escapes its manifest directory"
        ) from error
    if not path.is_file() or path.is_symlink():
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} partition is not a regular file"
        )
    return path


def _wave_key(value: Mapping[str, Any]) -> tuple[str, str, int]:
    try:
        return slots_v1._wave_key(value)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _full_wave_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return background_v2._wave_identity(value)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def _manifest_entries(
    manifest: Mapping[str, Any], *, label: str, stage: int
) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    entries = [
        _mapping(value, f"{label} instance entry")
        for value in _array(manifest.get("instances"), f"{label} instances")
    ]
    order = [
        _text(value, f"{label} instance_order")
        for value in _array(manifest.get("instance_order"), f"{label} instance_order")
    ]
    ids = [_text(entry.get("instance_id"), f"{label} instance_id") for entry in entries]
    if ids != order or len(set(ids)) != len(ids) or not ids:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"{label} instance order/set differs"
        )
    verifier = (
        _verify_model_content_address
        if stage == 5
        else _verify_background_content_address
    )
    for entry in entries:
        verifier(entry, f"{label} instance entry")
    return order, dict(zip(ids, entries, strict=True))


@dataclass(frozen=True)
class _InputClosure:
    selector_v2: Mapping[str, Any]
    selector_v2_path: Path
    selector_v2_sha: str
    selector_v2_file_sha: str
    selector_v1: Mapping[str, Any]
    selector_v1_sha: str
    selector_v1_file_sha: str
    overlap_audit: Mapping[str, Any]
    overlap_audit_sha: str
    overlap_audit_file_sha: str
    stage5_manifest: Mapping[str, Any]
    stage5_manifest_path: Path
    stage5_sha: str
    stage5_file_sha: str
    stage6_manifest: Mapping[str, Any]
    stage6_manifest_path: Path
    stage6_sha: str
    stage6_file_sha: str
    selected_rows: Mapping[str, Mapping[str, Any]]
    accepted_rows: Mapping[str, Mapping[tuple[str, str, int], Mapping[str, Any]]]
    stage5_entries: Mapping[str, Mapping[str, Any]]
    stage6_entries: Mapping[str, Mapping[str, Any]]


def _load_input_closure(
    *,
    selector_v2_path: str | Path,
    selector_v1_path: str | Path,
    overlap_audit_path: str | Path,
    stage5_manifest_path: str | Path,
    stage6_manifest_path: str | Path,
) -> _InputClosure:
    selector2, selector2_path, selector2_file_sha = _read_canonical_json(
        selector_v2_path, "V2 selector receipt"
    )
    try:
        selector2 = selector_v2.validate_exact_fury_selector_receipt_v2(selector2)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"invalid V2 selector receipt: {error}"
        ) from error
    selector2_sha = _verify_content_address(selector2, "V2 selector receipt")
    if selector2_sha != EXPECTED_SELECTOR_V2_CONTENT_SHA256:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "V2 selector receipt is not the frozen formal selector"
        )

    selector1, _selector1_path, selector1_file_sha = _read_canonical_json(
        selector_v1_path, "V1 selector receipt"
    )
    try:
        selector1 = slots_v1.validate_exact_fury_selector_receipt_v1(selector1)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"invalid V1 selector receipt: {error}"
        ) from error
    selector1_sha = _verify_content_address(selector1, "V1 selector receipt")
    selector1_binding = _mapping(
        _mapping(selector2.get("source_bindings"), "V2 selector bindings").get(
            "v1_selector_receipt"
        ),
        "V2 V1-selector binding",
    )
    if dict(selector1_binding) != {
        "content_sha256": selector1_sha,
        "file_sha256": selector1_file_sha,
        "role": "IMMUTABLE_UPSTREAM_IDENTITY_POPULATION_SUPERSEDED_SELECTION_SEMANTICS",
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "V2 selector does not bind the supplied V1 receipt"
        )

    audit, _audit_path, audit_file_sha = _read_canonical_json(
        overlap_audit_path, "overlap audit"
    )
    try:
        audit, audit_sha = slots_v1._validate_overlap_audit(audit)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"invalid overlap audit: {error}"
        ) from error
    v1_bindings = _mapping(selector1.get("source_bindings"), "V1 selector bindings")
    if _mapping(v1_bindings.get("overlap_audit"), "V1 overlap binding") != {
        "content_sha256": audit_sha,
        "file_sha256": audit_file_sha,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "V1 selector does not bind the supplied overlap audit"
        )

    stage5, stage5_path, stage5_file_sha = _read_canonical_json(
        stage5_manifest_path, "Stage-5 manifest"
    )
    stage5_sha = _verify_model_content_address(stage5, "Stage-5 manifest")
    if (
        stage5.get("schema") != model_v2.SCHEMA
        or stage5.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or stage5.get("status") != model_v2.STATUS
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "input is not the current Stage-5 manifest"
        )
    stage5_binding = _mapping(
        _mapping(selector2.get("source_bindings"), "V2 selector bindings").get(
            "stage5_manifest"
        ),
        "V2 Stage-5 binding",
    )
    if dict(stage5_binding) != {
        "content_sha256": stage5_sha,
        "file_sha256": stage5_file_sha,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "V2 selector does not bind the supplied Stage-5 manifest"
        )

    stage6, stage6_path, stage6_file_sha = _read_canonical_json(
        stage6_manifest_path, "Stage-6 manifest"
    )
    stage6_sha = _verify_background_content_address(stage6, "Stage-6 manifest")
    if (
        stage6.get("schema") != background_v2.SCHEMA
        or stage6.get("implementation_revision")
        != background_v2.IMPLEMENTATION_REVISION
        or stage6.get("status") != background_v2.STATUS
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "input is not the current Stage-6 manifest"
        )

    audit_inputs = _mapping(audit.get("input_bindings"), "overlap input bindings")
    overlap_stage5 = _mapping(
        audit_inputs.get("stage5_manifest"), "overlap Stage-5 binding"
    )
    if (
        overlap_stage5.get("content_sha256") != stage5_sha
        or overlap_stage5.get("file_sha256") != stage5_file_sha
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlap audit does not bind the supplied Stage-5 manifest"
        )
    overlap_stage6 = _mapping(
        audit_inputs.get("stage6_manifest"), "overlap Stage-6 binding"
    )
    if (
        overlap_stage6.get("content_sha256") != stage6_sha
        or overlap_stage6.get("file_sha256") != stage6_file_sha
        or v1_bindings.get("stage6_manifest_content_sha256") != stage6_sha
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selector/overlap closure does not bind the supplied Stage-6 manifest"
        )
    old50 = _mapping(audit_inputs.get("old50_capsule"), "overlap old50 binding")
    if old50.get("content_sha256") != v1_bindings.get(
        "old50_capsule_content_sha256"
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selector and overlap audit bind different old-50 capsules"
        )

    stage6_input = _mapping(
        _mapping(stage6.get("input_closure"), "Stage-6 input_closure").get(
            "team_model_manifest"
        ),
        "Stage-6 Stage-5 binding",
    )
    if (
        stage6_input.get("content_sha256") != stage5_sha
        or stage6_input.get("file_sha256") != stage5_file_sha
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "Stage-6 manifest does not bind the supplied Stage-5 manifest"
        )
    if stage6.get("split_graph") != stage5.get("split_graph"):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "Stage-5 and Stage-6 split graphs differ"
        )

    _stage5_order, stage5_entries = _manifest_entries(
        stage5, label="Stage-5", stage=5
    )
    _stage6_order, stage6_entries = _manifest_entries(
        stage6, label="Stage-6", stage=6
    )
    selected_rows = {
        _text(row.get("instance_id"), "selector instance_id"): row
        for row in (
            _mapping(value, "V2 selector row")
            for value in _array(selector2.get("rows"), "V2 selector rows")
        )
    }
    selected_order = [
        _text(value, "V2 selector instance_order")
        for value in _array(selector2.get("instance_order"), "V2 instance order")
    ]
    if list(selected_rows) != selected_order or not set(selected_rows) <= set(
        stage5_entries
    ) or not set(selected_rows) <= set(stage6_entries):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected instance order/set differs from Stage-5/Stage-6"
        )

    accepted: dict[str, dict[tuple[str, str, int], Mapping[str, Any]]] = {
        instance_id: {} for instance_id in selected_order
    }
    for raw in _array(audit.get("rows"), "overlap rows"):
        row = _mapping(raw, "overlap row")
        if row.get("classification") not in ACCEPTED_OVERLAP_CLASSIFICATIONS:
            continue
        key = _wave_key(_mapping(row.get("key"), "overlap row key"))
        if key[0] not in accepted:
            continue
        if key in accepted[key[0]]:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlap audit duplicates an accepted exact wave key"
            )
        accepted[key[0]][key] = row
    for instance_id in selected_order:
        selector_row = selected_rows[instance_id]
        expected = _integer(
            selector_row.get("accepted_wave_count"),
            "selector accepted_wave_count",
            nonnegative=True,
        )
        if len(accepted[instance_id]) != expected:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "selector accepted-wave count differs from overlap audit"
            )
        stage5_entry_sha = _verify_model_content_address(
            stage5_entries[instance_id], "selected Stage-5 entry"
        )
        row_bindings = _mapping(
            selector_row.get("source_bindings"), "V2 selector-row bindings"
        )
        if row_bindings.get("stage5_instance_content_sha256") != stage5_entry_sha:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "V2 selector row does not bind its Stage-5 instance"
            )
        stage6_model_input = _mapping(
            stage6_entries[instance_id].get("model_input"),
            "selected Stage-6 model input",
        )
        if stage6_model_input.get("instance_entry_content_sha256") != stage5_entry_sha:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "Stage-6 instance does not bind the selected Stage-5 entry"
            )

    if sum(len(rows) for rows in accepted.values()) != selector2["summary"][
        "accepted_wave_count"
    ]:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "V2 selector total accepted-wave count differs from overlap audit"
        )
    return _InputClosure(
        selector_v2=selector2,
        selector_v2_path=selector2_path,
        selector_v2_sha=selector2_sha,
        selector_v2_file_sha=selector2_file_sha,
        selector_v1=selector1,
        selector_v1_sha=selector1_sha,
        selector_v1_file_sha=selector1_file_sha,
        overlap_audit=audit,
        overlap_audit_sha=audit_sha,
        overlap_audit_file_sha=audit_file_sha,
        stage5_manifest=stage5,
        stage5_manifest_path=stage5_path,
        stage5_sha=stage5_sha,
        stage5_file_sha=stage5_file_sha,
        stage6_manifest=stage6,
        stage6_manifest_path=stage6_path,
        stage6_sha=stage6_sha,
        stage6_file_sha=stage6_file_sha,
        selected_rows=selected_rows,
        accepted_rows=accepted,
        stage5_entries=stage5_entries,
        stage6_entries=stage6_entries,
    )


def _selected_player(
    stage5_wave: Mapping[str, Any], *, selected_guid: str
) -> Mapping[str, Any]:
    matches = []
    for raw in _array(stage5_wave.get("players"), "Stage-5 wave players"):
        player = _mapping(raw, "Stage-5 player")
        metadata = _mapping(player.get("player"), "Stage-5 player metadata")
        if metadata.get("guid") == selected_guid:
            matches.append(player)
    if len(matches) != 1:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected exact GUID is absent or duplicated in a Stage-5 wave"
        )
    return matches[0]


def _validate_selected_spec(
    player: Mapping[str, Any], *, selection: Mapping[str, Any]
) -> None:
    metadata = _mapping(player.get("player"), "selected player metadata")
    if str(metadata.get("class") or "").casefold() != "warrior":
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected exact GUID is not a Warrior"
        )
    spec = _mapping(player.get("warrior_spec_lane"), "selected Warrior spec lane")
    relationship = _mapping(
        selection.get("selected_stage5_compatibility"),
        "selector Stage-5 compatibility",
    ).get("relationship")
    if relationship == "STAGE5_OBSERVED_FURY_CORROBORATED":
        valid = (
            spec.get("partition_key") == "WARRIOR_FURY"
            and spec.get("observed_spec") == "Fury"
            and spec.get("exact_guid_match") is True
            and spec.get("fury_or_arms_conflict_free_observation") is True
        )
    elif relationship == "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_FURY_PROMOTION":
        try:
            valid = slots_v1._stage5_unknown_allows_external_exact_fury(spec)
        except Exception as error:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error
    else:
        valid = False
    if not valid:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected player wave spec conflicts with the V2 selector relationship"
        )


def _focal_trace_refs(
    stage5_wave: Mapping[str, Any], player: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trace = [
        _mapping(value, "Stage-5 trace row")
        for value in _array(stage5_wave.get("exact_trace"), "Stage-5 exact_trace")
    ]
    exact_indices = [
        _integer(value, "selected exact_trace_index", nonnegative=True)
        for value in _array(player.get("exact_trace_indices"), "selected trace indices")
    ]
    outcome_indices = [
        _integer(value, "selected outcome trace index", nonnegative=True)
        for value in _array(
            player.get("outcome_context_trace_indices"),
            "selected outcome trace indices",
        )
    ]
    transitions = [
        _mapping(value, "selected prefix transition")
        for value in _array(player.get("prefix_transitions"), "prefix_transitions")
    ]
    action_indices = [
        _integer(value.get("trace_index"), "transition trace_index", nonnegative=True)
        for value in transitions
    ]
    if sorted(action_indices + outcome_indices) != exact_indices:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected action/outcome trace refs do not partition exact player events"
        )

    def build_ref(index: int, role: str) -> dict[str, Any]:
        if index >= len(trace):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "selected trace reference is out of range"
            )
        source = trace[index]
        event = _mapping(source.get("event"), "selected trace event")
        target = _mapping(event.get("target"), "selected trace target")
        return {
            "trace_index": index,
            "order_key": deepcopy(source.get("order_key")),
            "event_type": event.get("event_type"),
            "learning_role": role,
            "target": {
                "guid": target.get("guid"),
                "lane": target.get("lane"),
                "voting_enemy_target": target.get("voting_enemy_target"),
            },
        }

    action_refs = [build_ref(index, "OBSERVED_ACTION_LABEL") for index in action_indices]
    outcome_refs = [
        build_ref(index, "DESCRIPTIVE_OUTCOME_CONTEXT") for index in outcome_indices
    ]
    target_refs = sorted(
        [*deepcopy(action_refs), *deepcopy(outcome_refs)],
        key=lambda row: row["trace_index"],
    )
    return action_refs, outcome_refs, target_refs


def build_wave_overlay_v1(
    *,
    selector_row: Mapping[str, Any],
    overlap_row: Mapping[str, Any],
    stage5_wave: Mapping[str, Any],
    stage6_block: Mapping[str, Any],
    source_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one selected exact-GUID old-50 wave episode."""

    selection = _mapping(selector_row, "V2 selector row")
    stage5_identity = _full_wave_identity(
        _mapping(stage5_wave.get("wave"), "Stage-5 wave identity")
    )
    stage6_identity = _full_wave_identity(
        _mapping(stage6_block.get("wave"), "Stage-6 wave identity")
    )
    if stage5_identity != stage6_identity:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "Stage-5 and Stage-6 full wave identities differ"
        )
    key = _wave_key(stage5_identity)
    if _wave_key(_mapping(overlap_row.get("key"), "overlap row key")) != key:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlap row and Stage-5/Stage-6 exact wave key differ"
        )
    instance_id, encounter_id, wave_ordinal = key
    if selection.get("instance_id") != instance_id:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selector row crossed an instance boundary"
        )
    selected_guid = _text(selection.get("selected_guid"), "selected GUID")
    source_model = _mapping(stage6_block.get("source_model"), "Stage-6 source_model")
    stage5_wave_sha = _verify_model_content_address(
        stage5_wave, "Stage-5 exact wave"
    )
    stage6_block_sha = _verify_background_content_address(
        stage6_block, "Stage-6 exact block"
    )
    exact_trace_sha = _sha256(stage5_wave.get("exact_trace"))
    if (
        source_model.get("wave_content_sha256") != stage5_wave_sha
        or source_model.get("exact_trace_content_sha256") != exact_trace_sha
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "Stage-6 source does not bind the exact Stage-5 wave/trace"
        )
    try:
        accepted = slots_v1._accepted_row(
            {"rows": [overlap_row]}, key=key, block_sha=stage6_block_sha
        )
        selected_targets, remap, capsule_index = slots_v1._target_contract(
            accepted, stage6_block
        )
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error
    if selected_guid not in _array(
        stage6_block.get("roster_player_guids"), "Stage-6 roster"
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selected exact GUID is absent from the Stage-6 roster"
        )
    player = _selected_player(stage5_wave, selected_guid=selected_guid)
    _validate_selected_spec(player, selection=selection)
    player_sha = _verify_model_content_address(player, "selected Stage-5 player")
    action_refs, outcome_refs, target_refs = _focal_trace_refs(stage5_wave, player)
    transitions = deepcopy(
        _array(player.get("prefix_transitions"), "selected prefix_transitions")
    )
    try:
        projection = slots_v1._loo_projection(
            block=stage6_block,
            focal_guid=selected_guid,
            selected_guids=selected_targets,
            capsule_index_by_guid=capsule_index,
            stage5_loo=_mapping(
                player.get("leave_one_player_out_background"),
                "selected Stage-5 leave-one-out",
            ),
        )
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error
    lane = selection.get("selection_lane")
    semantics = deepcopy(
        dict(_mapping(selection.get("selection_semantics"), "selection semantics"))
    )
    is_pdf = lane == selector_v2.PDF_LANE
    if is_pdf is not (semantics.get("outcome_conditioned_discovery") is True):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selector lane and outcome-conditioning semantics differ"
        )
    if lane not in selector_v2.SELECTION_LANES:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "unsupported V2 selector lane"
        )
    component = _mapping(player.get("component_membership"), "player component membership")
    overlap_component = _mapping(
        accepted.get("component_join"), "overlap component join"
    )
    core = {
        "schema": RECORD_SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "identity": {
            "instance_id": instance_id,
            "encounter_id": encounter_id,
            "encounter_ordinal": stage5_identity["encounter_ordinal"],
            "external_wave_id": stage5_identity["wave_id"],
            "wave_ordinal": wave_ordinal,
            "selected_guid": selected_guid,
            "overlay_key": [instance_id, encounter_id, wave_ordinal, selected_guid],
        },
        "slot_selection": {
            "selection_lane": lane,
            "selection_rule": selection.get("selection_rule"),
            "selection_semantics": semantics,
            "selected_stage5_relationship": _mapping(
                selection.get("selected_stage5_compatibility"),
                "selected Stage-5 compatibility",
            ).get("relationship"),
            "selector_row_content_sha256": _verify_content_address(
                selection, "V2 selector row"
            ),
        },
        "source_bindings": {
            **deepcopy(dict(source_bindings)),
            "overlap_row_sha256": _sha256(overlap_row),
            "stage5_wave_content_sha256": stage5_wave_sha,
            "stage5_exact_trace_content_sha256": exact_trace_sha,
            "stage5_player_record_content_sha256": player_sha,
            "stage6_block_content_sha256": stage6_block_sha,
            "stage6_schedule_semantic_sha256": stage6_block.get(
                "schedule_semantic_sha256"
            ),
        },
        "focal_player_episode": {
            "player": deepcopy(player.get("player")),
            "warrior_spec_lane": deepcopy(player.get("warrior_spec_lane")),
            "eligibility_observation": deepcopy(
                player.get("eligibility_observation")
            ),
            "component_membership": deepcopy(component),
            "exact_trace_indices": deepcopy(player.get("exact_trace_indices")),
            "action_trace_refs": action_refs,
            "outcome_context_trace_refs": outcome_refs,
            "target_trace_refs": target_refs,
            "prefix_transitions": transitions,
            "leave_one_player_out_background": deepcopy(
                player.get("leave_one_player_out_background")
            ),
        },
        "split_authority": {
            "stage5_component_membership": deepcopy(component),
            "overlap_component_join": deepcopy(overlap_component),
            "required_split_unit": "connected component of instance, guild, and player nodes",
            "row_random_split_allowed": False,
            "same_player_or_guild_can_cross_folds": False,
        },
        "target_contract": {
            "classification": accepted.get("classification"),
            "selected_target_guids": selected_targets,
            "stage6_to_capsule_target_index_remap": remap,
            "ordinal_only_join_allowed": False,
        },
        "exact_guid_loo_teammate_schedule": projection,
        "scientific_boundary": {
            "expert_training_discovery_episode_eligible": True,
            "pdf_top_player_training_only_outcome_conditioned": is_pdf,
            "generic_identity_discovery_outcome_free": not is_pdf,
            "heldout_performance_evidence_eligible": False,
            "comparison_outcome_eligible": False,
            "historical_policy_voting_eligible": False,
            "future_team_schedule_visible_to_policy": False,
            "teammate_schedule_environment_input_only": True,
            "policy_training_started": False,
            "simulator_run_started": False,
            "deployment_ready": False,
        },
        "summary": {
            "prefix_transition_count": len(transitions),
            "action_trace_ref_count": len(action_refs),
            "outcome_context_trace_ref_count": len(outcome_refs),
            "target_trace_ref_count": len(target_refs),
            "projected_teammate_event_count": projection["projected_event_count"],
            "projected_teammate_damage": projection["projected_damage"],
            "excluded_focal_event_count": projection["exact_guid_leave_one_out"][
                "excluded_event_count"
            ],
            "excluded_focal_damage": projection["exact_guid_leave_one_out"][
                "excluded_damage"
            ],
        },
    }
    result = _content_addressed(core)
    validate_wave_overlay_v1(result)
    return result


def validate_wave_overlay_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, "slot overlay wave")))
    if (
        raw.get("schema") != RECORD_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "unsupported slot overlay wave"
        )
    _verify_content_address(raw, "slot overlay wave")
    identity = _mapping(raw.get("identity"), "overlay identity")
    instance_id = _text(identity.get("instance_id"), "overlay instance_id")
    encounter_id = _text(identity.get("encounter_id"), "overlay encounter_id")
    encounter_ordinal = _integer(
        identity.get("encounter_ordinal"), "overlay encounter_ordinal", nonnegative=True
    )
    wave_ordinal = _integer(
        identity.get("wave_ordinal"), "overlay wave_ordinal", nonnegative=True
    )
    guid = _text(identity.get("selected_guid"), "overlay selected_guid")
    if (
        identity.get("external_wave_id")
        != f"{encounter_id}:external-v2-wave:{wave_ordinal}"
        or identity.get("overlay_key")
        != [instance_id, encounter_id, wave_ordinal, guid]
        or encounter_ordinal < 0
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay exact-wave identity was weakened"
        )
    selection = _mapping(raw.get("slot_selection"), "overlay slot selection")
    lane = selection.get("selection_lane")
    semantics = _mapping(selection.get("selection_semantics"), "selection semantics")
    is_pdf = lane == selector_v2.PDF_LANE
    if (
        lane not in selector_v2.SELECTION_LANES
        or dict(semantics) != _expected_selection_semantics(str(lane))
        or selection.get("selection_rule")
        != (
            selector_v2.PDF_RULE
            if is_pdf
            else selector_v2.GENERIC_RULE
        )
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay selector semantics differ from its V2 lane"
        )
    _sha(selection.get("selector_row_content_sha256"), "selector row SHA-256")
    bindings = _mapping(raw.get("source_bindings"), "overlay source bindings")
    for field in (
        "selector_v2_content_sha256",
        "selector_v2_file_sha256",
        "selector_v1_content_sha256",
        "overlap_audit_content_sha256",
        "overlap_row_sha256",
        "stage5_manifest_content_sha256",
        "stage5_instance_content_sha256",
        "stage5_wave_content_sha256",
        "stage5_exact_trace_content_sha256",
        "stage5_player_record_content_sha256",
        "stage6_manifest_content_sha256",
        "stage6_instance_content_sha256",
        "stage6_block_content_sha256",
        "stage6_schedule_semantic_sha256",
        "old50_capsule_content_sha256",
    ):
        _sha(bindings.get(field), field)

    episode = _mapping(raw.get("focal_player_episode"), "focal player episode")
    player = _mapping(episode.get("player"), "focal player metadata")
    if player.get("guid") != guid or str(player.get("class") or "").casefold() != "warrior":
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay focal player identity differs"
        )
    _validate_selected_spec(
        episode,
        selection={
            "selected_stage5_compatibility": {
                "relationship": selection.get("selected_stage5_relationship")
            }
        },
    )
    exact_indices = [
        _integer(value, "overlay exact trace index", nonnegative=True)
        for value in _array(episode.get("exact_trace_indices"), "exact trace indices")
    ]
    actions = [
        _mapping(value, "action trace ref")
        for value in _array(episode.get("action_trace_refs"), "action trace refs")
    ]
    outcomes = [
        _mapping(value, "outcome trace ref")
        for value in _array(
            episode.get("outcome_context_trace_refs"), "outcome trace refs"
        )
    ]
    targets = [
        _mapping(value, "target trace ref")
        for value in _array(episode.get("target_trace_refs"), "target trace refs")
    ]
    transitions = [
        _mapping(value, "prefix transition")
        for value in _array(episode.get("prefix_transitions"), "prefix transitions")
    ]
    action_indices = [row.get("trace_index") for row in actions]
    outcome_indices = [row.get("trace_index") for row in outcomes]
    if (
        exact_indices != sorted(set(exact_indices))
        or action_indices != [row.get("trace_index") for row in transitions]
        or sorted(action_indices + outcome_indices) != exact_indices
        or targets != sorted([*actions, *outcomes], key=lambda row: row["trace_index"])
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay focal action/outcome/target trace refs differ"
        )
    for transition, reference in zip(transitions, actions, strict=True):
        current = _mapping(
            transition.get("current_event_label"), "transition current label"
        )
        current_target = _mapping(current.get("target"), "transition current target")
        expected_target = {
            "guid": current_target.get("guid"),
            "lane": current_target.get("lane"),
            "voting_enemy_target": current_target.get("voting_enemy_target"),
        }
        if (
            transition.get("order_key") != reference.get("order_key")
            or current.get("event_type") != reference.get("event_type")
            or current.get("learning_role") != "OBSERVED_ACTION_LABEL"
            or reference.get("learning_role") != "OBSERVED_ACTION_LABEL"
            or reference.get("event_type") not in model_v2.ACTION_EVENT_TYPES
            or reference.get("target") != expected_target
            or transition.get("feature_cutoff_is_strict_prefix") is not True
            or transition.get("current_event_present_in_state_before") is not False
            or transition.get("future_outcomes_in_state_before") is not False
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay prefix transition differs from its action trace ref"
            )
        try:
            model_v2._assert_prefix_contract(transition.get("state_before"))
        except Exception as error:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error
    if any(
        reference.get("learning_role") != "DESCRIPTIVE_OUTCOME_CONTEXT"
        or reference.get("event_type") not in model_v2.AMOUNT_EVENT_TYPES
        for reference in outcomes
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay outcome trace role/type differs"
        )

    target_contract = _mapping(raw.get("target_contract"), "target contract")
    selected_targets = _array(
        target_contract.get("selected_target_guids"), "selected target GUIDs"
    )
    remap = _array(
        target_contract.get("stage6_to_capsule_target_index_remap"),
        "target remap",
    )
    if (
        target_contract.get("classification") not in ACCEPTED_OVERLAP_CLASSIFICATIONS
        or target_contract.get("ordinal_only_join_allowed") is not False
        or not selected_targets
        or len(set(selected_targets)) != len(selected_targets)
        or [row.get("target_guid") for row in remap] != selected_targets
        or [row.get("capsule_target_index") for row in remap]
        != list(range(len(selected_targets)))
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay target contract differs"
        )
    projection = _mapping(
        raw.get("exact_guid_loo_teammate_schedule"), "LOO teammate schedule"
    )
    loo = _mapping(projection.get("exact_guid_leave_one_out"), "exact GUID LOO")
    excluded = [
        _mapping(row, "excluded focal event")
        for row in _array(loo.get("excluded_events"), "excluded focal events")
    ]
    schedule = [
        _mapping(row, "projected teammate event")
        for row in _array(projection.get("projected_schedule"), "projected schedule")
    ]
    excluded_damage = sum(
        _nonnegative_number(row.get("damage"), "excluded focal damage")
        for row in excluded
    )
    schedule_damage = sum(
        _nonnegative_number(row.get("damage"), "projected teammate damage")
        for row in schedule
    )
    excluded_count_by_kind = Counter(
        str(row.get("attribution_kind")) for row in excluded
    )
    excluded_damage_by_kind: Counter[str] = Counter()
    for row in excluded:
        excluded_damage_by_kind[str(row.get("attribution_kind"))] += row["damage"]

    def exact_actor_source_relation(event: Mapping[str, Any]) -> bool:
        actor = event.get("actor_player_guid")
        source = event.get("source_guid")
        kind = event.get("attribution_kind")
        return isinstance(actor, str) and bool(actor) and isinstance(source, str) and bool(
            source
        ) and (
            (kind == "DIRECT_FRIENDLY_PLAYER" and source == actor)
            or (
                kind in {"EXACT_OFFICIAL_OWNER", "EXACT_OFFICIAL_CONTROLLER"}
                and source != actor
            )
        )

    remap_by_guid = {
        row["target_guid"]: row for row in remap if isinstance(row, Mapping)
    }
    stage5_event_upper_bound = _integer(
        loo.get("stage5_all_event_count_upper_bound"),
        "Stage-5 focal event upper bound",
        nonnegative=True,
    )
    stage5_damage_upper_bound = _nonnegative_number(
        loo.get("stage5_all_attributed_damage_upper_bound"),
        "Stage-5 focal damage upper bound",
    )
    if (
        projection.get("source_schedule")
        != "stage6_exact_block.runtime_candidate_schedule"
        or projection.get("operation_order")
        != [
            "EXACT_GUID_LEAVE_ONE_OUT",
            "OLD50_TARGET_PILE_PROJECTION",
            "STAGE6_TO_CAPSULE_TARGET_INDEX_REMAP",
        ]
        or projection.get("future_schedule_available_as_policy_feature") is not False
        or projection.get("source_eventmeta_order_preserved") is not True
        or loo.get("focal_player_guid") != guid
        or loo.get("predicate") != "actor_player_guid != focal_player_guid"
        or loo.get("resolved_attribution_kinds_removed")
        != list(slots_v1.ATTRIBUTION_KINDS)
        or loo.get("name_or_guid_suffix_inference_used") is not False
        or any(row.get("actor_player_guid") != guid for row in excluded)
        or any(
            row.get("attribution_kind") not in slots_v1.ATTRIBUTION_KINDS
            or not exact_actor_source_relation(row)
            for row in excluded
        )
        or loo.get("excluded_event_count") != len(excluded)
        or loo.get("excluded_damage") != excluded_damage
        or loo.get("excluded_event_count_by_attribution")
        != {
            kind: excluded_count_by_kind.get(kind, 0)
            for kind in slots_v1.ATTRIBUTION_KINDS
        }
        or loo.get("excluded_damage_by_attribution")
        != {
            kind: excluded_damage_by_kind.get(kind, 0)
            for kind in slots_v1.ATTRIBUTION_KINDS
        }
        or stage5_event_upper_bound < len(excluded)
        or stage5_damage_upper_bound < excluded_damage
        or any(row.get("actor_player_guid") == guid for row in schedule)
        or any(
            row.get("attribution_kind") not in slots_v1.ATTRIBUTION_KINDS
            or not exact_actor_source_relation(row)
            for row in schedule
        )
        or projection.get("projected_schedule_content_sha256") != _sha256(schedule)
        or projection.get("projected_event_count") != len(schedule)
        or projection.get("projected_damage") != schedule_damage
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay exact-GUID LOO schedule differs"
        )
    target_projection = _mapping(
        projection.get("target_projection"), "LOO target projection"
    )
    if (
        target_projection.get("selected_target_guids") != selected_targets
        or target_projection.get("stage6_to_capsule_target_index_remap") != remap
        or target_projection.get("unselected_target_events_enter_selected_targets")
        is not False
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay LOO target projection differs"
        )
    prior: tuple[int, ...] | None = None
    for event in schedule:
        target_guid = event.get("target_guid")
        mapping = remap_by_guid.get(target_guid)
        order = tuple(
            _integer(value, "schedule order component", nonnegative=True)
            for value in _array(
                event.get("source_eventmeta_order_key"), "schedule order key"
            )
        )
        if (
            mapping is None
            or event.get("target_index") != mapping.get("capsule_target_index")
            or event.get("stage6_target_index") != mapping.get("stage6_target_index")
            or (prior is not None and order <= prior)
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay projected schedule target/order differs"
            )
        prior = order
    boundary = _mapping(raw.get("scientific_boundary"), "overlay boundary")
    if boundary != {
        "expert_training_discovery_episode_eligible": True,
        "pdf_top_player_training_only_outcome_conditioned": is_pdf,
        "generic_identity_discovery_outcome_free": not is_pdf,
        "heldout_performance_evidence_eligible": False,
        "comparison_outcome_eligible": False,
        "historical_policy_voting_eligible": False,
        "future_team_schedule_visible_to_policy": False,
        "teammate_schedule_environment_input_only": True,
        "policy_training_started": False,
        "simulator_run_started": False,
        "deployment_ready": False,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay scientific boundary widened"
        )
    summary = _mapping(raw.get("summary"), "overlay summary")
    if summary != {
        "prefix_transition_count": len(transitions),
        "action_trace_ref_count": len(actions),
        "outcome_context_trace_ref_count": len(outcomes),
        "target_trace_ref_count": len(targets),
        "projected_teammate_event_count": len(schedule),
        "projected_teammate_damage": sum(row.get("damage", 0) for row in schedule),
        "excluded_focal_event_count": len(excluded),
        "excluded_focal_damage": sum(row.get("damage", 0) for row in excluded),
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay summary differs"
        )
    return raw


def _stream_selected_partition(
    *,
    path: Path,
    entry: Mapping[str, Any],
    instance_id: str,
    accepted_keys: set[tuple[str, str, int]],
    stage: int,
) -> tuple[dict[tuple[str, str, int], dict[str, Any]], dict[str, int]]:
    partition = _mapping(entry.get("partition"), f"Stage-{stage} partition")
    logical = hashlib.sha256()
    logical_bytes = 0
    row_count = 0
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    with path.open("rb") as source:
        hashing_raw = overlap_v1._HashingRaw(source)
        with io.BufferedReader(hashing_raw) as buffered:
            with gzip.GzipFile(fileobj=buffered, mode="rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    logical.update(raw_line)
                    logical_bytes += len(raw_line)
                    row_count += 1
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ChronicleOld50ExactFurySlotOverlayV1Error(
                            f"invalid Stage-{stage} row {line_number}: {error}"
                        ) from error
                    row = _mapping(value, f"Stage-{stage} row")
                    key = _wave_key(_mapping(row.get("wave"), f"Stage-{stage} wave"))
                    if key not in accepted_keys:
                        continue
                    if raw_line != _canonical(row) + b"\n":
                        raise ChronicleOld50ExactFurySlotOverlayV1Error(
                            f"selected Stage-{stage} row is noncanonical JSONL"
                        )
                    try:
                        if stage == 5:
                            model_v2._validate_model_wave(
                                row,
                                instance_id=instance_id,
                                expected_contamination=_mapping(
                                    entry.get("contamination_lane"),
                                    "Stage-5 entry contamination",
                                ),
                            )
                        else:
                            background_v2._validate_block(
                                row, expected_instance_id=instance_id
                            )
                    except Exception as error:
                        raise ChronicleOld50ExactFurySlotOverlayV1Error(
                            f"invalid Stage-{stage} partition row: {error}"
                        ) from error
                    if key in selected:
                        raise ChronicleOld50ExactFurySlotOverlayV1Error(
                            f"Stage-{stage} partition duplicates an accepted wave"
                        )
                    selected[key] = dict(row)
    if (
        hashing_raw.bytes_read != partition.get("compressed_size_bytes")
        or hashing_raw.digest.hexdigest() != partition.get("compressed_file_sha256")
        or row_count != partition.get("record_count")
        or logical_bytes != partition.get("logical_size_bytes")
        or logical.hexdigest() != partition.get("logical_content_sha256")
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"Stage-{stage} partition count/size/hash differs after its single scan"
        )
    if set(selected) != accepted_keys:
        missing = len(accepted_keys - set(selected))
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            f"Stage-{stage} partition is missing {missing} accepted waves"
        )
    return selected, {
        "partition_open_count": 1,
        "partition_sequential_scan_count": 1,
        "compressed_bytes": hashing_raw.bytes_read,
        "logical_bytes": logical_bytes,
        "source_record_count": row_count,
        "selected_record_count": len(selected),
        "selected_record_semantic_validation_count": len(selected),
        "unselected_record_semantic_validation_count": 0,
    }


@dataclass(frozen=True)
class _PartitionBuild:
    temporary_path: Path
    final_path: Path
    compressed_file_sha256: str
    entry: Mapping[str, Any]


def _write_output_partition(
    *,
    output_directory: Path,
    instance_id: str,
    rows: Sequence[Mapping[str, Any]],
    selector_row: Mapping[str, Any],
    stage5_entry: Mapping[str, Any],
    stage6_entry: Mapping[str, Any],
    stage5_scan: Mapping[str, int],
    stage6_scan: Mapping[str, int],
) -> _PartitionBuild:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{instance_id}.", suffix=".jsonl.gz.tmp", dir=output_directory
    )
    temporary = Path(name)
    logical = hashlib.sha256()
    logical_size = 0
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
                for row in rows:
                    payload = _canonical(row) + b"\n"
                    handle.write(payload)
                    logical.update(payload)
                    logical_size += len(payload)
            raw.flush()
            os.fsync(raw.fileno())
        logical_sha = logical.hexdigest()
        compressed_sha = _sha256_file(temporary)
        final = output_directory / f"{instance_id}.{logical_sha}.jsonl.gz"
        summaries = [
            _mapping(row.get("summary"), "overlay wave summary") for row in rows
        ]
        lane = selector_row["selection_lane"]
        entry_core = {
            "instance_id": instance_id,
            "status": STATUS,
            "selector": {
                "selector_row_content_sha256": _verify_content_address(
                    selector_row, "V2 selector row"
                ),
                "selected_guid": selector_row["selected_guid"],
                "selection_lane": lane,
                "outcome_conditioned_discovery": lane == selector_v2.PDF_LANE,
                "accepted_wave_count": selector_row["accepted_wave_count"],
            },
            "input_partitions": {
                "stage5": {
                    "instance_entry_content_sha256": _verify_model_content_address(
                        stage5_entry, "Stage-5 instance entry"
                    ),
                    "partition": deepcopy(stage5_entry.get("partition")),
                    "single_scan": deepcopy(dict(stage5_scan)),
                },
                "stage6": {
                    "instance_entry_content_sha256": _verify_background_content_address(
                        stage6_entry, "Stage-6 instance entry"
                    ),
                    "partition": deepcopy(stage6_entry.get("partition")),
                    "single_scan": deepcopy(dict(stage6_scan)),
                },
            },
            "partition": {
                "path": final.name,
                "record_schema": RECORD_SCHEMA,
                "record_count": len(rows),
                "logical_content_sha256": logical_sha,
                "logical_size_bytes": logical_size,
                "compressed_file_sha256": compressed_sha,
                "compressed_size_bytes": temporary.stat().st_size,
                "gzip_mtime": 0,
            },
            "summary": {
                "wave_overlay_count": len(rows),
                "prefix_transition_count": sum(
                    row["prefix_transition_count"] for row in summaries
                ),
                "action_trace_ref_count": sum(
                    row["action_trace_ref_count"] for row in summaries
                ),
                "outcome_context_trace_ref_count": sum(
                    row["outcome_context_trace_ref_count"] for row in summaries
                ),
                "target_trace_ref_count": sum(
                    row["target_trace_ref_count"] for row in summaries
                ),
                "projected_teammate_event_count": sum(
                    row["projected_teammate_event_count"] for row in summaries
                ),
                "projected_teammate_damage": sum(
                    row["projected_teammate_damage"] for row in summaries
                ),
                "excluded_focal_event_count": sum(
                    row["excluded_focal_event_count"] for row in summaries
                ),
                "excluded_focal_damage": sum(
                    row["excluded_focal_damage"] for row in summaries
                ),
                "zero_transition_wave_count": sum(
                    row["prefix_transition_count"] == 0 for row in summaries
                ),
                "zero_teammate_schedule_wave_count": sum(
                    row["projected_teammate_event_count"] == 0 for row in summaries
                ),
                "selected_guid_missing_wave_count": 0,
            },
        }
        return _PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=compressed_sha,
            entry=_content_addressed(entry_core),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _manifest_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lane_counts = Counter(entry["selector"]["selection_lane"] for entry in entries)
    result = {
        "instance_count": len(entries),
        **{
            key: sum(int(entry["summary"][key]) for entry in entries)
            for key in INSTANCE_SUMMARY_FIELDS
        },
        "selection_lane_instance_counts": dict(sorted(lane_counts.items())),
        "pdf_outcome_conditioned_instance_count": lane_counts.get(
            selector_v2.PDF_LANE, 0
        ),
        "generic_outcome_free_instance_count": lane_counts.get(
            selector_v2.GENERIC_LANE, 0
        ),
        "pdf_outcome_conditioned_wave_count": sum(
            entry["summary"]["wave_overlay_count"]
            for entry in entries
            if entry["selector"]["selection_lane"] == selector_v2.PDF_LANE
        ),
        "generic_outcome_free_wave_count": sum(
            entry["summary"]["wave_overlay_count"]
            for entry in entries
            if entry["selector"]["selection_lane"] == selector_v2.GENERIC_LANE
        ),
        "stage5_selected_partition_open_count": sum(
            entry["input_partitions"]["stage5"]["single_scan"][
                "partition_open_count"
            ]
            for entry in entries
        ),
        "stage6_selected_partition_open_count": sum(
            entry["input_partitions"]["stage6"]["single_scan"][
                "partition_open_count"
            ]
            for entry in entries
        ),
    }
    return result


def _publish_partition(build: _PartitionBuild) -> str:
    if build.final_path.exists():
        if (
            not build.final_path.is_file()
            or build.final_path.is_symlink()
            or _sha256_file(build.final_path)
            != build.compressed_file_sha256
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                f"divergent immutable partition: {build.final_path}"
            )
        build.temporary_path.unlink()
        return "RESUMED"
    build.temporary_path.replace(build.final_path)
    return "PUBLISHED"


def _write_once(path: Path, payload: bytes) -> str:
    try:
        return overlap_v1._write_once(path, payload)
    except Exception as error:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(str(error)) from error


def materialize_exact_fury_slot_overlay_v1(
    *,
    selector_v2_path: str | Path,
    selector_v1_path: str | Path,
    overlap_audit_path: str | Path,
    stage5_manifest_path: str | Path,
    stage6_manifest_path: str | Path,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Sequentially materialize the selected 20-instance/470-wave overlay."""

    closure = _load_input_closure(
        selector_v2_path=selector_v2_path,
        selector_v1_path=selector_v1_path,
        overlap_audit_path=overlap_audit_path,
        stage5_manifest_path=stage5_manifest_path,
        stage6_manifest_path=stage6_manifest_path,
    )
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    builds: list[_PartitionBuild] = []
    try:
        source_common = {
            "selector_v2_content_sha256": closure.selector_v2_sha,
            "selector_v2_file_sha256": closure.selector_v2_file_sha,
            "selector_v1_content_sha256": closure.selector_v1_sha,
            "overlap_audit_content_sha256": closure.overlap_audit_sha,
            "stage5_manifest_content_sha256": closure.stage5_sha,
            "stage6_manifest_content_sha256": closure.stage6_sha,
            "old50_capsule_content_sha256": _mapping(
                _mapping(
                    closure.overlap_audit.get("input_bindings"),
                    "overlap input bindings",
                ).get("old50_capsule"),
                "old50 capsule binding",
            )["content_sha256"],
        }
        for instance_id in closure.selector_v2["instance_order"]:
            selector_row = closure.selected_rows[instance_id]
            accepted = closure.accepted_rows[instance_id]
            accepted_keys = set(accepted)
            stage5_entry = closure.stage5_entries[instance_id]
            stage6_entry = closure.stage6_entries[instance_id]
            stage5_rows, stage5_scan = _stream_selected_partition(
                path=_partition_path(
                    closure.stage5_manifest_path, stage5_entry, "Stage-5"
                ),
                entry=stage5_entry,
                instance_id=instance_id,
                accepted_keys=accepted_keys,
                stage=5,
            )
            stage6_rows, stage6_scan = _stream_selected_partition(
                path=_partition_path(
                    closure.stage6_manifest_path, stage6_entry, "Stage-6"
                ),
                entry=stage6_entry,
                instance_id=instance_id,
                accepted_keys=accepted_keys,
                stage=6,
            )
            per_instance_bindings = {
                **source_common,
                "stage5_instance_content_sha256": _verify_model_content_address(
                    stage5_entry, "Stage-5 selected instance"
                ),
                "stage6_instance_content_sha256": _verify_background_content_address(
                    stage6_entry, "Stage-6 selected instance"
                ),
            }
            rows = []
            for key in sorted(accepted):
                row = build_wave_overlay_v1(
                    selector_row=selector_row,
                    overlap_row=accepted[key],
                    stage5_wave=stage5_rows[key],
                    stage6_block=stage6_rows[key],
                    source_bindings=per_instance_bindings,
                )
                rows.append(row)
            if len(rows) != selector_row["accepted_wave_count"]:
                raise ChronicleOld50ExactFurySlotOverlayV1Error(
                    "compiled instance wave count differs from selector"
                )
            builds.append(
                _write_output_partition(
                    output_directory=output,
                    instance_id=instance_id,
                    rows=rows,
                    selector_row=selector_row,
                    stage5_entry=stage5_entry,
                    stage6_entry=stage6_entry,
                    stage5_scan=stage5_scan,
                    stage6_scan=stage6_scan,
                )
            )
        entries = [deepcopy(dict(build.entry)) for build in builds]
        summary = _manifest_summary(entries)
        expected = closure.selector_v2["summary"]
        if (
            summary["instance_count"] != expected["instance_count"]
            or summary["wave_overlay_count"] != expected["accepted_wave_count"]
            or summary["selection_lane_instance_counts"]
            != expected["selection_lane_counts"]
            or summary["selected_guid_missing_wave_count"] != 0
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "compiled overlay coverage differs from the V2 selector"
            )
        core = {
            "schema": MANIFEST_SCHEMA,
            "revision": REVISION,
            "status": STATUS,
            "source_bindings": {
                "selector_v2": {
                    "content_sha256": closure.selector_v2_sha,
                    "file_sha256": closure.selector_v2_file_sha,
                },
                "selector_v1_closure_carrier": {
                    "content_sha256": closure.selector_v1_sha,
                    "file_sha256": closure.selector_v1_file_sha,
                    "selection_semantics_superseded_by_v2": True,
                },
                "overlap_audit": {
                    "content_sha256": closure.overlap_audit_sha,
                    "file_sha256": closure.overlap_audit_file_sha,
                },
                "stage5_manifest": {
                    "content_sha256": closure.stage5_sha,
                    "file_sha256": closure.stage5_file_sha,
                },
                "stage6_manifest": {
                    "content_sha256": closure.stage6_sha,
                    "file_sha256": closure.stage6_file_sha,
                },
                "old50_capsule_content_sha256": source_common[
                    "old50_capsule_content_sha256"
                ],
            },
            "instance_order": list(closure.selector_v2["instance_order"]),
            "instances": entries,
            "split_contract": {
                "stage5_split_graph_preserved": deepcopy(
                    closure.stage5_manifest.get("split_graph")
                ),
                "stage6_split_graph_equal": closure.stage6_manifest.get(
                    "split_graph"
                )
                == closure.stage5_manifest.get("split_graph"),
                "required_split_unit": "connected component of instance, guild, and player nodes",
                "row_random_split_allowed": False,
                "same_player_or_guild_can_cross_folds": False,
            },
            "selection_and_episode_contract": {
                "one_selected_guid_per_instance": True,
                "selected_guid_applied_only_to_same_instance": True,
                "join_key": [
                    "instance_id",
                    "encounter_id",
                    "wave_ordinal",
                    "selected_guid",
                ],
                "stage5_stage6_full_wave_identity_equal_required": True,
                "accepted_wave_authority": "FROZEN_OVERLAP_AUDIT_EXACT_OR_SUBSET_ROWS",
                "prefix_transitions_preserved": True,
                "action_outcome_target_trace_refs_preserved": True,
                "exact_guid_loo_before_target_projection": True,
            },
            "streaming_contract": {
                "selected_instance_count": len(entries),
                "unselected_instance_partitions_opened": 0,
                "stage5_partition_open_count": summary[
                    "stage5_selected_partition_open_count"
                ],
                "stage6_partition_open_count": summary[
                    "stage6_selected_partition_open_count"
                ],
                "single_process": True,
                "one_instance_materialized_at_a_time": True,
                "gzip_mtime": 0,
            },
            "scientific_boundary": {
                "expert_training_discovery_overlay_complete": True,
                "pdf_lane_training_only_and_outcome_conditioned": True,
                "generic_lane_outcome_free": True,
                "heldout_performance_evidence_eligible": False,
                "comparison_outcome_eligible": False,
                "historical_policy_voting_eligible": False,
                "future_teammate_schedule_visible_to_policy": False,
                "policy_training_started": False,
                "simulator_run_started": False,
                "comparison_started": False,
                "deployment_started": False,
            },
            "execution_boundary": {
                "network_requests_made": 0,
                "heavy_jobs_started": False,
                "policy_training_started": False,
                "simulator_runs_started": False,
                "comparison_started": False,
                "deployment_started": False,
                "frozen_historical_policy_v2_v5_modified": False,
            },
            "summary": summary,
        }
        manifest = _content_addressed(core)
        validate_overlay_manifest_v1(manifest)
        manifest_payload = _canonical(manifest) + b"\n"
        manifest_sha = manifest["content_address"]["sha256"]
        addressed = output / f"{MANIFEST_PREFIX}.{manifest_sha}.manifest.json"
        stable = output / "manifest.json"
        partition_statuses = [_publish_partition(build) for build in builds]
        addressed_status = _write_once(addressed, manifest_payload)
        stable_status = _write_once(stable, manifest_payload)
        return {
            "status": (
                "PUBLISHED"
                if "PUBLISHED"
                in {*partition_statuses, addressed_status, stable_status}
                else "RESUMED"
            ),
            "manifest_path": str(stable),
            "content_addressed_manifest_path": str(addressed),
            "content_sha256": manifest_sha,
            "manifest_file_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "partitions": [str(build.final_path) for build in builds],
            "summary": summary,
            "network_requests_made": 0,
            "heavy_jobs_started": False,
            "policy_training_started": False,
            "simulator_runs_started": False,
            "comparison_started": False,
            "deployment_started": False,
        }
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)


def validate_overlay_manifest_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, "overlay manifest")))
    if (
        raw.get("schema") != MANIFEST_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "unsupported overlay manifest"
        )
    _verify_content_address(raw, "overlay manifest")
    entries = [
        _mapping(value, "overlay instance entry")
        for value in _array(raw.get("instances"), "overlay instances")
    ]
    order = _array(raw.get("instance_order"), "overlay instance_order")
    if (
        len(entries) != EXPECTED_INSTANCE_COUNT
        or len(order) != EXPECTED_INSTANCE_COUNT
        or [entry.get("instance_id") for entry in entries] != order
        or len(set(order)) != len(order)
        or any(not isinstance(instance_id, str) or not instance_id for instance_id in order)
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest instance order/set differs"
        )
    sources = _mapping(raw.get("source_bindings"), "manifest source bindings")
    for label in (
        "selector_v2",
        "overlap_audit",
        "stage5_manifest",
        "stage6_manifest",
    ):
        binding = _mapping(sources.get(label), f"{label} binding")
        _sha(binding.get("content_sha256"), f"{label} content SHA-256")
        _sha(binding.get("file_sha256"), f"{label} file SHA-256")
        if (
            label == "selector_v2"
            and binding.get("content_sha256")
            != EXPECTED_SELECTOR_V2_CONTENT_SHA256
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "manifest does not bind the frozen V2 selector"
            )
    selector_v1 = _mapping(
        sources.get("selector_v1_closure_carrier"),
        "selector V1 closure binding",
    )
    _sha(selector_v1.get("content_sha256"), "selector V1 content SHA-256")
    _sha(selector_v1.get("file_sha256"), "selector V1 file SHA-256")
    if selector_v1.get("selection_semantics_superseded_by_v2") is not True:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "selector V1 semantics were not superseded by V2"
        )
    _sha(
        sources.get("old50_capsule_content_sha256"),
        "old50 capsule content SHA-256",
    )

    for entry in entries:
        instance_id = _text(entry.get("instance_id"), "overlay entry instance_id")
        if entry.get("status") != STATUS:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay instance status differs"
            )
        _verify_content_address(entry, "overlay instance entry")
        selector = _mapping(entry.get("selector"), "overlay instance selector")
        _sha(
            selector.get("selector_row_content_sha256"),
            "selector row content SHA-256",
        )
        _text(selector.get("selected_guid"), "selected exact GUID")
        lane = selector.get("selection_lane")
        if lane not in selector_v2.SELECTION_LANES:
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay instance selector lane differs"
            )
        is_pdf = lane == selector_v2.PDF_LANE
        accepted_wave_count = _integer(
            selector.get("accepted_wave_count"),
            "selector accepted wave count",
            nonnegative=True,
        )
        if (
            accepted_wave_count == 0
            or selector.get("outcome_conditioned_discovery") is not is_pdf
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay instance selector semantics differ"
            )

        partition = _mapping(entry.get("partition"), "overlay partition")
        logical_sha = _sha(
            partition.get("logical_content_sha256"),
            "overlay partition logical SHA-256",
        )
        _sha(
            partition.get("compressed_file_sha256"),
            "overlay partition compressed SHA-256",
        )
        partition_path = _text(partition.get("path"), "overlay partition path")
        summary = _mapping(entry.get("summary"), "overlay instance summary")
        if set(summary) != set(INSTANCE_SUMMARY_FIELDS):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay instance summary fields differ"
            )
        for field in INSTANCE_SUMMARY_FIELDS:
            _integer(summary.get(field), f"overlay instance {field}", nonnegative=True)
        wave_count = int(summary["wave_overlay_count"])
        if (
            partition.get("record_schema") != RECORD_SCHEMA
            or _integer(
                partition.get("record_count"),
                "overlay partition record count",
                nonnegative=True,
            )
            != wave_count
            or accepted_wave_count != wave_count
            or wave_count == 0
            or _integer(
                partition.get("logical_size_bytes"),
                "overlay partition logical size",
                nonnegative=True,
            )
            == 0
            or _integer(
                partition.get("compressed_size_bytes"),
                "overlay partition compressed size",
                nonnegative=True,
            )
            == 0
            or partition.get("gzip_mtime") != 0
            or partition_path != f"{instance_id}.{logical_sha}.jsonl.gz"
            or summary["selected_guid_missing_wave_count"] != 0
            or summary["target_trace_ref_count"]
            != summary["action_trace_ref_count"]
            + summary["outcome_context_trace_ref_count"]
            or summary["zero_transition_wave_count"] > wave_count
            or summary["zero_teammate_schedule_wave_count"] > wave_count
        ):
            raise ChronicleOld50ExactFurySlotOverlayV1Error(
                "overlay instance partition descriptor differs"
            )
        for stage in ("stage5", "stage6"):
            stage_input = _mapping(
                _mapping(entry.get("input_partitions"), "input partitions").get(stage),
                f"{stage} input partition",
            )
            _sha(
                stage_input.get("instance_entry_content_sha256"),
                f"{stage} instance entry SHA-256",
            )
            input_partition = _mapping(
                stage_input.get("partition"), f"{stage} partition descriptor"
            )
            _text(input_partition.get("path"), f"{stage} partition path")
            _sha(
                input_partition.get("logical_content_sha256"),
                f"{stage} logical SHA-256",
            )
            _sha(
                input_partition.get("compressed_file_sha256"),
                f"{stage} compressed SHA-256",
            )
            input_record_count = _integer(
                input_partition.get("record_count"),
                f"{stage} source record count",
                nonnegative=True,
            )
            input_logical_bytes = _integer(
                input_partition.get("logical_size_bytes"),
                f"{stage} source logical bytes",
                nonnegative=True,
            )
            input_compressed_bytes = _integer(
                input_partition.get("compressed_size_bytes"),
                f"{stage} source compressed bytes",
                nonnegative=True,
            )
            scan = _mapping(stage_input.get("single_scan"), f"{stage} single scan")
            if (
                scan.get("partition_open_count") != 1
                or scan.get("partition_sequential_scan_count") != 1
                or scan.get("selected_record_count") != wave_count
                or scan.get("selected_record_semantic_validation_count") != wave_count
                or scan.get("unselected_record_semantic_validation_count") != 0
                or scan.get("source_record_count") != input_record_count
                or scan.get("logical_bytes") != input_logical_bytes
                or scan.get("compressed_bytes") != input_compressed_bytes
                or input_record_count < wave_count
            ):
                raise ChronicleOld50ExactFurySlotOverlayV1Error(
                    "selected source partition was not scanned exactly once"
                )
    expected_summary = _manifest_summary(entries)
    summary = _mapping(raw.get("summary"), "overlay manifest summary")
    if dict(summary) != expected_summary:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest summary differs"
        )
    if (
        summary.get("instance_count") != EXPECTED_INSTANCE_COUNT
        or summary.get("wave_overlay_count") != EXPECTED_WAVE_OVERLAY_COUNT
        or summary.get("selection_lane_instance_counts")
        != EXPECTED_LANE_INSTANCE_COUNTS
        or summary.get("pdf_outcome_conditioned_instance_count") != 6
        or summary.get("generic_outcome_free_instance_count") != 14
        or summary.get("pdf_outcome_conditioned_wave_count", 0) <= 0
        or summary.get("generic_outcome_free_wave_count", 0) <= 0
        or summary.get("pdf_outcome_conditioned_wave_count")
        + summary.get("generic_outcome_free_wave_count")
        != EXPECTED_WAVE_OVERLAY_COUNT
        or summary.get("stage5_selected_partition_open_count")
        != EXPECTED_INSTANCE_COUNT
        or summary.get("stage6_selected_partition_open_count")
        != EXPECTED_INSTANCE_COUNT
        or summary.get("selected_guid_missing_wave_count") != 0
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest does not close the frozen 20-instance/470-wave scope"
        )

    split = _mapping(raw.get("split_contract"), "manifest split contract")
    _mapping(
        split.get("stage5_split_graph_preserved"),
        "preserved Stage-5 split graph",
    )
    if (
        split.get("stage6_split_graph_equal") is not True
        or split.get("required_split_unit")
        != "connected component of instance, guild, and player nodes"
        or split.get("row_random_split_allowed") is not False
        or split.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest split contract differs"
        )
    episode_contract = _mapping(
        raw.get("selection_and_episode_contract"),
        "manifest selection and episode contract",
    )
    if episode_contract != {
        "one_selected_guid_per_instance": True,
        "selected_guid_applied_only_to_same_instance": True,
        "join_key": [
            "instance_id",
            "encounter_id",
            "wave_ordinal",
            "selected_guid",
        ],
        "stage5_stage6_full_wave_identity_equal_required": True,
        "accepted_wave_authority": "FROZEN_OVERLAP_AUDIT_EXACT_OR_SUBSET_ROWS",
        "prefix_transitions_preserved": True,
        "action_outcome_target_trace_refs_preserved": True,
        "exact_guid_loo_before_target_projection": True,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest selection/episode contract differs"
        )
    streaming = _mapping(raw.get("streaming_contract"), "manifest streaming contract")
    if streaming != {
        "selected_instance_count": EXPECTED_INSTANCE_COUNT,
        "unselected_instance_partitions_opened": 0,
        "stage5_partition_open_count": EXPECTED_INSTANCE_COUNT,
        "stage6_partition_open_count": EXPECTED_INSTANCE_COUNT,
        "single_process": True,
        "one_instance_materialized_at_a_time": True,
        "gzip_mtime": 0,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest streaming contract differs"
        )
    boundary = _mapping(raw.get("scientific_boundary"), "manifest boundary")
    if boundary != {
        "expert_training_discovery_overlay_complete": True,
        "pdf_lane_training_only_and_outcome_conditioned": True,
        "generic_lane_outcome_free": True,
        "heldout_performance_evidence_eligible": False,
        "comparison_outcome_eligible": False,
        "historical_policy_voting_eligible": False,
        "future_teammate_schedule_visible_to_policy": False,
        "policy_training_started": False,
        "simulator_run_started": False,
        "comparison_started": False,
        "deployment_started": False,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay manifest scientific boundary widened"
        )
    execution = _mapping(raw.get("execution_boundary"), "execution boundary")
    if execution != {
        "network_requests_made": 0,
        "heavy_jobs_started": False,
        "policy_training_started": False,
        "simulator_runs_started": False,
        "comparison_started": False,
        "deployment_started": False,
        "frozen_historical_policy_v2_v5_modified": False,
    }:
        raise ChronicleOld50ExactFurySlotOverlayV1Error(
            "overlay execution boundary widened"
        )
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_old50_exact_fury_slot_overlay_v1",
        description="Compile the selected old-50 exact-Fury wave overlay",
    )
    parser.add_argument("--selector-v2", type=Path, required=True)
    parser.add_argument("--selector-v1", type=Path, required=True)
    parser.add_argument("--overlap-audit", type=Path, required=True)
    parser.add_argument("--stage5-manifest", type=Path, required=True)
    parser.add_argument("--stage6-manifest", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = materialize_exact_fury_slot_overlay_v1(
        selector_v2_path=args.selector_v2,
        selector_v1_path=args.selector_v1,
        overlap_audit_path=args.overlap_audit,
        stage5_manifest_path=args.stage5_manifest,
        stage6_manifest_path=args.stage6_manifest,
        output_directory=args.output_directory,
    )
    (stdout or __import__("sys").stdout).write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "ChronicleOld50ExactFurySlotOverlayV1Error",
    "MANIFEST_SCHEMA",
    "RECORD_SCHEMA",
    "REVISION",
    "STATUS",
    "build_parser",
    "build_wave_overlay_v1",
    "main",
    "materialize_exact_fury_slot_overlay_v1",
    "validate_overlay_manifest_v1",
    "validate_wave_overlay_v1",
)
