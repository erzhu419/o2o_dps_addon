"""Truthful selection overlay for the frozen old-50 exact-Fury receipt.

V1 recovered one exact Fury GUID for each of the 20 accepted old-50 raids,
but its PDF lane described leaderboard rank as provenance-only even though the
lowest PDF rank selected the player in raids with multiple admissible chains.
This additive V2 keeps that deliberate top-player discovery target and records
the resulting outcome conditioning explicitly.  It is discovery evidence for
future expert-policy training, never held-out performance evidence.

The generic lane remains outcome-free.  Before its lexicographic selection it
removes Stage-5 Arms and conflicting observations.  Only a conflict-free
``UNKNOWN_NONVOTING`` Stage-5 slot may be promoted by the already-bound exact
same-instance Fury ranking evidence; an observed conflict is never overwritten.

V2 consumes, rather than overwrites, the immutable V1 receipt.  It performs no
network request, partition scan, policy fit, simulator run, or comparison.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from . import chronicle_external_team_wave_model_v2 as model_v2
from . import chronicle_old50_warrior_slot_substitution_v1 as v1


SCHEMA = "chronicle_old50_warrior_slot_substitution/v2/exact_fury_selector_receipt"
REVISION = "truthful_pdf_discovery_stage5_conflict_filter_v2"
STATUS = "COMPLETE_20_INSTANCE_EXACT_FURY_SELECTOR_DISCOVERY_NONVOTING"

PDF_LANE = "PDF_TOP_PLAYER_DISCOVERY_EXACT_GUID"
GENERIC_LANE = "GENERIC_OUTCOME_FREE_EXACT_FURY_FULL_SCOPE"
SELECTION_LANES = (PDF_LANE, GENERIC_LANE)

PDF_RULE = "LOWEST_FROZEN_PDF_DPS_RANK_AMONG_STAGE5_COMPATIBLE_EXACT_CHAINS"
GENERIC_RULE = (
    "LEXICOGRAPHICALLY_SMALLEST_STAGE5_COMPATIBLE_GUID_AFTER_FULL_SCOPE_FILTER"
)


class ChronicleOld50WarriorSlotSubstitutionV2Error(RuntimeError):
    """The truthful V2 selector overlay could not be derived fail-closed."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            f"{label} must be nonempty text"
        )
    return value


def _sha(value: Any, label: str) -> str:
    try:
        return v1._sha(value, label=label)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    return v1._content_addressed(core)


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    try:
        return v1._verify_content_address(value, label=label)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error


def _external_content_sha(value: Mapping[str, Any], label: str) -> str:
    try:
        return v1._external_content_sha(value, label=label)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error


def _normalized_stage5_observation(
    raw: Mapping[str, Any], *, expected_guid: str
) -> dict[str, Any]:
    observation = _mapping(raw, "Stage-5 Warrior observation")
    try:
        guid = v1._guid(
            observation.get("player_guid"), label="Stage-5 observation GUID"
        )
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
    if guid != expected_guid:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "Stage-5 observation GUID differs from its selector candidate"
        )
    if str(observation.get("player_class") or "").casefold() != "warrior":
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "selector candidate is absent from the exact Stage-5 Warrior roster"
        )
    conflicts = observation.get("field_conflicts")
    if not isinstance(conflicts, Mapping):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "Stage-5 observation field_conflicts must be an object"
        )
    return {
        "player_guid": guid,
        "player_class": "Warrior",
        "observed_spec": observation.get("player_spec"),
        "evidence_status": observation.get("spec_evidence_status"),
        "field_conflicts": deepcopy(dict(conflicts)),
        "player_name_excluded": True,
    }


def _compatibility(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a Stage-5 slot without overwriting Arms or conflict evidence."""

    observed = observation.get("observed_spec")
    status = observation.get("evidence_status")
    conflicts = _mapping(observation.get("field_conflicts"), "field_conflicts")
    if conflicts:
        eligible = False
        relationship = "STAGE5_SPEC_CONFLICT_EXCLUDED"
        reason = "STAGE5_FIELD_CONFLICT_PRESENT"
    elif observed == "Arms":
        eligible = False
        relationship = "STAGE5_ARMS_EXCLUDED"
        reason = "STAGE5_OBSERVED_ARMS"
    elif observed == "Fury" and status == "OBSERVED":
        eligible = True
        relationship = "STAGE5_OBSERVED_FURY_CORROBORATED"
        reason = None
    elif observed in {None, "Unknown"} and status == "UNKNOWN_NONVOTING":
        eligible = True
        relationship = "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_FURY_PROMOTION"
        reason = None
    else:
        eligible = False
        relationship = "STAGE5_UNSUPPORTED_SPEC_EVIDENCE_EXCLUDED"
        reason = "STAGE5_SPEC_EVIDENCE_NOT_PROMOTABLE"
    return {
        "eligible": eligible,
        "relationship": relationship,
        "exclusion_reason": reason,
        "arms_or_conflict_overwritten": False,
        "unknown_promoted": relationship
        == "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_FURY_PROMOTION",
    }


def _candidate_with_stage5(
    raw: Mapping[str, Any], *, observations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    candidate = deepcopy(dict(_mapping(raw, "selector candidate")))
    guid_value = candidate.get("character_guid")
    if guid_value is None:
        candidate["stage5_observation"] = None
        candidate["stage5_compatibility"] = {
            "eligible": False,
            "relationship": "NO_CAPTURED_EXACT_GUID_CHAIN",
            "exclusion_reason": "NO_CAPTURED_EXACT_GUID_CHAIN",
            "arms_or_conflict_overwritten": False,
            "unknown_promoted": False,
        }
        candidate["selection_eligible"] = False
        return candidate
    try:
        guid = v1._guid(guid_value, label="selector candidate GUID")
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
    if guid not in observations:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "exact selector candidate is absent from Stage-5 Warrior observations"
        )
    observation = _normalized_stage5_observation(
        observations[guid], expected_guid=guid
    )
    compatibility = _compatibility(observation)
    exact_chain = candidate.get("exact_chain_status")
    full_scope = candidate.get("full_scope_exact_fury")
    upstream_exact = (
        exact_chain == "ADMISSIBLE_EXACT_GUID_CHAIN" or full_scope is True
    )
    candidate["character_guid"] = guid
    candidate["stage5_observation"] = observation
    candidate["stage5_compatibility"] = compatibility
    candidate["selection_eligible"] = bool(
        upstream_exact and compatibility["eligible"] is True
    )
    return candidate


def select_instance_v2(
    *,
    instance_id: str,
    source_lane: str,
    source_selected_guid: str,
    source_candidates: Sequence[Mapping[str, Any]],
    stage5_observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Select one exact Fury slot with lane-specific, auditable semantics."""

    canonical_instance = _text(instance_id, "instance_id")
    try:
        prior_guid = v1._guid(source_selected_guid, label="V1 selected GUID")
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
    if source_lane == v1.EXACT_FURY_SELECTION_LANES[0]:
        lane = PDF_LANE
        rule = PDF_RULE
        normalized = [
            _candidate_with_stage5(raw, observations=stage5_observations)
            for raw in source_candidates
        ]
        normalized.sort(
            key=lambda row: (
                int(row.get("pdf_rank", 0)),
                str(row.get("board_manifest_row_sha256") or ""),
            )
        )
        leaderboard_conditioned = True
        rank_used = True
    elif source_lane == v1.EXACT_FURY_SELECTION_LANES[1]:
        lane = GENERIC_LANE
        rule = GENERIC_RULE
        normalized = [
            _candidate_with_stage5(raw, observations=stage5_observations)
            for raw in source_candidates
        ]
        normalized.sort(key=lambda row: str(row.get("character_guid") or ""))
        leaderboard_conditioned = False
        rank_used = False
    else:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "unsupported V1 source selection lane"
        )
    eligible = [row for row in normalized if row["selection_eligible"] is True]
    if not eligible:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "instance has no Stage-5-compatible exact Fury candidate"
        )
    selected = eligible[0]
    selected_guid = str(selected["character_guid"])
    return {
        "instance_id": canonical_instance,
        "selection_lane": lane,
        "source_v1_selection_lane": source_lane,
        "selection_rule": rule,
        "candidate_population": normalized,
        "candidate_population_count": len(normalized),
        "selection_eligible_count": len(eligible),
        "selected_guid": selected_guid,
        "source_v1_selected_guid": prior_guid,
        "selected_guid_differs_from_v1": selected_guid != prior_guid,
        "selected_stage5_observation": deepcopy(selected["stage5_observation"]),
        "selected_stage5_compatibility": deepcopy(
            selected["stage5_compatibility"]
        ),
        "selection_semantics": {
            "leaderboard_membership_used_for_selection": leaderboard_conditioned,
            "leaderboard_dps_rank_used_for_selection": rank_used,
            "outcome_conditioned_discovery": leaderboard_conditioned,
            "same_instance_ranking_dps_values_used": False,
            "generic_selection_outcome_free": not leaderboard_conditioned,
            "purpose": (
                "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY"
                if leaderboard_conditioned
                else "OUTCOME_FREE_EXACT_FURY_IDENTITY_DISCOVERY_ONLY"
            ),
            "heldout_performance_evidence_eligible": False,
            "comparison_outcome_eligible": False,
        },
    }


def _stage5_observations_by_instance(
    stage5_manifest: Mapping[str, Any], instance_ids: Sequence[str]
) -> tuple[dict[str, dict[str, Mapping[str, Any]]], dict[str, str]]:
    entries = {
        _text(row.get("instance_id"), "Stage-5 instance_id"): row
        for row in (
            _mapping(raw, "Stage-5 instance")
            for raw in _array(stage5_manifest.get("instances"), "Stage-5 instances")
        )
    }
    if not set(instance_ids) <= set(entries):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "Stage-5 manifest omits a V1 selector instance"
        )
    observations_by_instance: dict[str, dict[str, Mapping[str, Any]]] = {}
    entry_shas: dict[str, str] = {}
    for instance_id in instance_ids:
        entry = _mapping(entries[instance_id], "Stage-5 selected instance")
        entry_shas[instance_id] = _external_content_sha(
            entry, "Stage-5 selected instance"
        )
        provenance = _mapping(
            entry.get("instance_provenance"), "Stage-5 instance provenance"
        )
        warrior = _mapping(
            provenance.get("warrior_spec_evidence"), "Stage-5 Warrior evidence"
        )
        observations: dict[str, Mapping[str, Any]] = {}
        for raw in _array(warrior.get("observations"), "Stage-5 observations"):
            observation = _mapping(raw, "Stage-5 observation")
            if str(observation.get("player_class") or "").casefold() != "warrior":
                continue
            try:
                guid = v1._guid(
                    observation.get("player_guid"), label="Stage-5 Warrior GUID"
                )
            except Exception as error:
                raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
            if guid in observations:
                raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                    "Stage-5 Warrior observations duplicate an exact GUID"
                )
            observations[guid] = observation
        observations_by_instance[instance_id] = observations
    return observations_by_instance, entry_shas


def build_exact_fury_selector_receipt_v2(
    *,
    v1_selector_receipt: Mapping[str, Any],
    v1_selector_receipt_file_sha256: str,
    stage5_manifest: Mapping[str, Any],
    stage5_manifest_file_sha256: str,
) -> dict[str, Any]:
    """Build the truthful, conflict-filtered overlay over one frozen V1 receipt."""

    try:
        source = v1.validate_exact_fury_selector_receipt_v1(v1_selector_receipt)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            f"invalid V1 selector receipt: {error}"
        ) from error
    source_sha = _verify_content_address(source, "V1 selector receipt")
    source_file_sha = _sha(
        v1_selector_receipt_file_sha256, "V1 selector receipt file SHA-256"
    )
    stage5 = deepcopy(dict(_mapping(stage5_manifest, "Stage-5 manifest")))
    stage5_sha = _external_content_sha(stage5, "Stage-5 manifest")
    stage5_file_sha = _sha(stage5_manifest_file_sha256, "Stage-5 file SHA-256")
    if (
        stage5.get("schema") != model_v2.SCHEMA
        or stage5.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or stage5.get("status") != model_v2.STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V2 selector input is not the current Stage-5 manifest"
        )
    source_stage5 = _mapping(
        _mapping(source.get("source_bindings"), "V1 source bindings").get(
            "stage5_manifest"
        ),
        "V1 Stage-5 binding",
    )
    if source_stage5 != {
        "content_sha256": stage5_sha,
        "file_sha256": stage5_file_sha,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V1 selector and supplied Stage-5 manifest differ"
        )

    source_rows = [
        _mapping(raw, "V1 selector row")
        for raw in _array(source.get("rows"), "V1 selector rows")
    ]
    instance_ids = [_text(row.get("instance_id"), "V1 instance_id") for row in source_rows]
    observations, entry_shas = _stage5_observations_by_instance(stage5, instance_ids)
    rows: list[dict[str, Any]] = []
    for source_row in source_rows:
        instance_id = str(source_row["instance_id"])
        source_lane = _text(source_row.get("selection_lane"), "V1 selection lane")
        source_guid = _text(source_row.get("player_guid"), "V1 selected GUID")
        evidence = _mapping(
            source_row.get("exact_fury_identity_evidence"), "V1 exact Fury evidence"
        )
        selection = _mapping(evidence.get("selection"), "V1 evidence selection")
        if source_lane == v1.EXACT_FURY_SELECTION_LANES[0]:
            candidates = [
                deepcopy(dict(_mapping(raw, "V1 PDF candidate")))
                for raw in _array(
                    _mapping(
                        selection.get("pdf_selector_receipt"),
                        "V1 PDF selector receipt",
                    ).get("candidate_population"),
                    "V1 PDF candidate population",
                )
            ]
        else:
            eligible_guids = _array(
                _mapping(
                    selection.get("generic_selector_receipt"),
                    "V1 generic selector receipt",
                ).get("eligible_guids"),
                "V1 generic eligible GUIDs",
            )
            candidates = [
                {
                    "character_guid": guid,
                    "full_scope_exact_fury": True,
                    "exact_chain_status": None,
                }
                for guid in eligible_guids
            ]
        selected = select_instance_v2(
            instance_id=instance_id,
            source_lane=source_lane,
            source_selected_guid=source_guid,
            source_candidates=candidates,
            stage5_observations=observations[instance_id],
        )
        selected["accepted_wave_count"] = source_row.get("accepted_wave_count")
        selected["source_bindings"] = {
            "v1_selector_row_sha256": v1._sha256(source_row),
            "v1_exact_fury_identity_evidence_content_sha256": evidence[
                "content_address"
            ]["sha256"],
            "stage5_instance_content_sha256": entry_shas[instance_id],
            "stage5_exact_roster_evidence_sha256": _mapping(
                next(
                    entry
                    for entry in stage5["instances"]
                    if entry.get("instance_id") == instance_id
                ),
                "Stage-5 selected entry",
            ).get("exact_roster_evidence_sha256"),
        }
        selected["scientific_boundary"] = {
            "identity_discovery_for_future_expert_training_only": True,
            "training_episode_admission_completed": False,
            "heldout_performance_evidence_eligible": False,
            "historical_policy_voting_eligible": False,
            "comparison_ready": False,
            "deployment_ready": False,
        }
        rows.append(_content_addressed(selected))
    rows.sort(key=lambda row: row["instance_id"])

    lane_counts = Counter(row["selection_lane"] for row in rows)
    relationship_counts = Counter(
        row["selected_stage5_compatibility"]["relationship"] for row in rows
    )
    summary = {
        "instance_count": len(rows),
        "accepted_wave_count": sum(int(row["accepted_wave_count"]) for row in rows),
        "selection_lane_counts": dict(sorted(lane_counts.items())),
        "selected_stage5_relationship_counts": dict(
            sorted(relationship_counts.items())
        ),
        "pdf_outcome_conditioned_discovery_count": lane_counts.get(PDF_LANE, 0),
        "generic_outcome_free_discovery_count": lane_counts.get(GENERIC_LANE, 0),
        "selected_guid_changed_from_v1_count": sum(
            row["selected_guid_differs_from_v1"] is True for row in rows
        ),
        "arms_or_conflict_selected_count": sum(
            row["selected_stage5_compatibility"]["arms_or_conflict_overwritten"]
            is True
            for row in rows
        ),
    }
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "source_bindings": {
            "v1_selector_receipt": {
                "content_sha256": source_sha,
                "file_sha256": source_file_sha,
                "role": "IMMUTABLE_UPSTREAM_IDENTITY_POPULATION_SUPERSEDED_SELECTION_SEMANTICS",
            },
            "stage5_manifest": {
                "content_sha256": stage5_sha,
                "file_sha256": stage5_file_sha,
            },
        },
        "instance_order": [row["instance_id"] for row in rows],
        "rows": rows,
        "summary": summary,
        "execution_boundary": {
            "network_requests_made": 0,
            "stage5_partitions_opened": False,
            "heavy_jobs_started": False,
            "policy_training_started": False,
            "simulator_runs_started": False,
            "comparison_started": False,
            "deployment_started": False,
        },
        "scientific_boundary": {
            "pdf_lane_is_leaderboard_outcome_conditioned_discovery": True,
            "pdf_lane_heldout_performance_evidence_eligible": False,
            "generic_lane_is_outcome_free": True,
            "arms_or_conflicting_stage5_evidence_may_be_overwritten": False,
            "identity_overlay_only": True,
            "comparison_ready": False,
            "deployment_ready": False,
        },
    }
    result = _content_addressed(core)
    validate_exact_fury_selector_receipt_v2(result)
    return result


def _validate_candidate(candidate: Mapping[str, Any]) -> None:
    guid = candidate.get("character_guid")
    observation = candidate.get("stage5_observation")
    compatibility = _mapping(
        candidate.get("stage5_compatibility"), "candidate Stage-5 compatibility"
    )
    if guid is None:
        if observation is not None or candidate.get("selection_eligible") is not False:
            raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                "candidate without exact GUID became selectable"
            )
        return
    try:
        canonical_guid = v1._guid(guid, label="candidate GUID")
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
    normalized = _mapping(observation, "candidate Stage-5 observation")
    if normalized.get("player_guid") != canonical_guid:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "candidate Stage-5 observation GUID differs"
        )
    expected = _compatibility(normalized)
    if dict(compatibility) != expected:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "candidate Stage-5 compatibility differs"
        )
    upstream_exact = (
        candidate.get("exact_chain_status") == "ADMISSIBLE_EXACT_GUID_CHAIN"
        or candidate.get("full_scope_exact_fury") is True
    )
    if candidate.get("selection_eligible") is not bool(
        upstream_exact and expected["eligible"] is True
    ):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "candidate eligibility differs from exact-Fury and Stage-5 evidence"
        )


def validate_exact_fury_selector_receipt_v2(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, "V2 selector receipt")))
    if (
        raw.get("schema") != SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "unsupported V2 selector receipt"
        )
    _verify_content_address(raw, "V2 selector receipt")
    bindings = _mapping(raw.get("source_bindings"), "V2 source bindings")
    for key in ("v1_selector_receipt", "stage5_manifest"):
        binding = _mapping(bindings.get(key), f"{key} binding")
        _sha(binding.get("content_sha256"), f"{key} content SHA-256")
        _sha(binding.get("file_sha256"), f"{key} file SHA-256")
    rows = [
        _mapping(row, "V2 selector row")
        for row in _array(raw.get("rows"), "V2 selector rows")
    ]
    order = [
        _text(value, "V2 instance order")
        for value in _array(raw.get("instance_order"), "instance_order")
    ]
    row_ids = [_text(row.get("instance_id"), "V2 row instance_id") for row in rows]
    if len(rows) != 20 or order != sorted(set(order)) or row_ids != order:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V2 receipt is not one sorted row per frozen instance"
        )
    lane_counts: Counter[str] = Counter()
    relationships: Counter[str] = Counter()
    accepted_wave_count = 0
    changed = 0
    conflict_selected = 0
    for row in rows:
        _verify_content_address(row, "V2 selector row")
        lane = row.get("selection_lane")
        if lane not in SELECTION_LANES:
            raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                "V2 selector row lane is unsupported"
            )
        lane_counts[str(lane)] += 1
        candidates = [
            _mapping(candidate, "V2 candidate")
            for candidate in _array(row.get("candidate_population"), "candidate population")
        ]
        for candidate in candidates:
            _validate_candidate(candidate)
        eligible = [
            candidate
            for candidate in candidates
            if candidate.get("selection_eligible") is True
        ]
        if not eligible:
            raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                "V2 row has no eligible exact Fury candidate"
            )
        if lane == PDF_LANE:
            expected_order = sorted(
                candidates,
                key=lambda candidate: (
                    int(candidate.get("pdf_rank", 0)),
                    str(candidate.get("board_manifest_row_sha256") or ""),
                ),
            )
            expected_rule = PDF_RULE
            expected_semantics = {
                "leaderboard_membership_used_for_selection": True,
                "leaderboard_dps_rank_used_for_selection": True,
                "outcome_conditioned_discovery": True,
                "same_instance_ranking_dps_values_used": False,
                "generic_selection_outcome_free": False,
                "purpose": "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY",
                "heldout_performance_evidence_eligible": False,
                "comparison_outcome_eligible": False,
            }
        else:
            expected_order = sorted(
                candidates, key=lambda candidate: str(candidate.get("character_guid") or "")
            )
            expected_rule = GENERIC_RULE
            expected_semantics = {
                "leaderboard_membership_used_for_selection": False,
                "leaderboard_dps_rank_used_for_selection": False,
                "outcome_conditioned_discovery": False,
                "same_instance_ranking_dps_values_used": False,
                "generic_selection_outcome_free": True,
                "purpose": "OUTCOME_FREE_EXACT_FURY_IDENTITY_DISCOVERY_ONLY",
                "heldout_performance_evidence_eligible": False,
                "comparison_outcome_eligible": False,
            }
        expected_selected = next(
            candidate
            for candidate in expected_order
            if candidate.get("selection_eligible") is True
        )
        selected_compatibility = _mapping(
            row.get("selected_stage5_compatibility"), "selected compatibility"
        )
        selected_observation = _mapping(
            row.get("selected_stage5_observation"), "selected observation"
        )
        selected_guid = _text(row.get("selected_guid"), "selected GUID")
        source_guid = _text(row.get("source_v1_selected_guid"), "source V1 GUID")
        if (
            list(candidates) != expected_order
            or row.get("selection_rule") != expected_rule
            or row.get("candidate_population_count") != len(candidates)
            or row.get("selection_eligible_count") != len(eligible)
            or selected_guid != expected_selected.get("character_guid")
            or dict(selected_observation) != expected_selected.get("stage5_observation")
            or dict(selected_compatibility) != expected_selected.get("stage5_compatibility")
            or row.get("selected_guid_differs_from_v1") is not (selected_guid != source_guid)
            or _mapping(row.get("selection_semantics"), "selection semantics")
            != expected_semantics
        ):
            raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                "V2 row selection or truthful semantics differ"
            )
        if selected_compatibility.get("eligible") is not True or selected_compatibility.get(
            "arms_or_conflict_overwritten"
        ) is not False:
            raise ChronicleOld50WarriorSlotSubstitutionV2Error(
                "V2 selected an Arms, conflicting, or otherwise ineligible slot"
            )
        accepted_wave_count += int(row.get("accepted_wave_count"))
        changed += row.get("selected_guid_differs_from_v1") is True
        conflict_selected += (
            selected_compatibility.get("arms_or_conflict_overwritten") is True
        )
        relationships[str(selected_compatibility.get("relationship"))] += 1
    expected_summary = {
        "instance_count": 20,
        "accepted_wave_count": accepted_wave_count,
        "selection_lane_counts": dict(sorted(lane_counts.items())),
        "selected_stage5_relationship_counts": dict(sorted(relationships.items())),
        "pdf_outcome_conditioned_discovery_count": lane_counts.get(PDF_LANE, 0),
        "generic_outcome_free_discovery_count": lane_counts.get(GENERIC_LANE, 0),
        "selected_guid_changed_from_v1_count": changed,
        "arms_or_conflict_selected_count": conflict_selected,
    }
    if _mapping(raw.get("summary"), "V2 summary") != expected_summary:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V2 selector summary differs"
        )
    if _mapping(raw.get("execution_boundary"), "V2 execution boundary") != {
        "network_requests_made": 0,
        "stage5_partitions_opened": False,
        "heavy_jobs_started": False,
        "policy_training_started": False,
        "simulator_runs_started": False,
        "comparison_started": False,
        "deployment_started": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V2 execution boundary widened"
        )
    if _mapping(raw.get("scientific_boundary"), "V2 scientific boundary") != {
        "pdf_lane_is_leaderboard_outcome_conditioned_discovery": True,
        "pdf_lane_heldout_performance_evidence_eligible": False,
        "generic_lane_is_outcome_free": True,
        "arms_or_conflicting_stage5_evidence_may_be_overwritten": False,
        "identity_overlay_only": True,
        "comparison_ready": False,
        "deployment_ready": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            "V2 scientific boundary widened"
        )
    return raw


def materialize_exact_fury_selector_receipt_v2(
    *,
    v1_selector_receipt_path: str | Path,
    stage5_manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    try:
        source, source_file_sha = v1._read_json_document(
            v1_selector_receipt_path, label="V1 selector receipt"
        )
        stage5, stage5_file_sha = v1._read_json_document(
            stage5_manifest_path, label="Stage-5 manifest"
        )
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(str(error)) from error
    receipt = build_exact_fury_selector_receipt_v2(
        v1_selector_receipt=source,
        v1_selector_receipt_file_sha256=source_file_sha,
        stage5_manifest=stage5,
        stage5_manifest_file_sha256=stage5_file_sha,
    )
    output = Path(output_path).expanduser().resolve()
    payload = v1._canonical(receipt) + b"\n"
    addressed = output.with_name(
        f"{output.stem}.{receipt['content_address']['sha256']}{output.suffix}"
    )
    try:
        addressed_status = v1.overlap_v1._write_once(addressed, payload)
        stable_status = v1.overlap_v1._write_once(output, payload)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionV2Error(
            f"could not publish V2 selector receipt: {error}"
        ) from error
    return {
        "status": (
            "PUBLISHED"
            if "PUBLISHED" in {addressed_status, stable_status}
            else "RESUMED"
        ),
        "output_path": str(output),
        "addressed_output_path": str(addressed),
        "content_sha256": receipt["content_address"]["sha256"],
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "summary": receipt["summary"],
        "network_requests_made": 0,
        "heavy_jobs_started": False,
        "comparison_started": False,
        "deployment_started": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_old50_warrior_slot_substitution_v2",
        description="Build the truthful old50 exact-Fury selector overlay V2",
    )
    parser.add_argument("--v1-selector-receipt", type=Path, required=True)
    parser.add_argument("--stage5-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = materialize_exact_fury_selector_receipt_v2(
        v1_selector_receipt_path=args.v1_selector_receipt,
        stage5_manifest_path=args.stage5_manifest,
        output_path=args.output,
    )
    (stdout or __import__("sys").stdout).write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "ChronicleOld50WarriorSlotSubstitutionV2Error",
    "GENERIC_LANE",
    "PDF_LANE",
    "REVISION",
    "SCHEMA",
    "SELECTION_LANES",
    "STATUS",
    "build_exact_fury_selector_receipt_v2",
    "build_parser",
    "main",
    "materialize_exact_fury_selector_receipt_v2",
    "select_instance_v2",
    "validate_exact_fury_selector_receipt_v2",
)
