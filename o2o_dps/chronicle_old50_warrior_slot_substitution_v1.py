"""Build exact Warrior-slot substitution inputs for accepted old-50 waves.

This adapter is additive to the Fury-only overlap compiler input.  It consumes
one accepted ``EXACT``/``SUBSET`` overlap row together with its exact Stage-5
wave and Stage-6 block, then emits one input for every exact Warrior GUID in
that wave.  The Stage-6 runtime schedule is first leave-one-player-out filtered
by its resolved ``actor_player_guid`` (direct, official owner, or official
controller attribution) and only then projected to the old-50 target pile.

An optional, separately content-addressed identity-evidence mapping can label a
conflict-free Stage-5 Fury slot, or resolve a nonconflicting Stage-5 unknown
slot, as ``EXACT_FURY``.  Board name/rank remain provenance only; the authority
is the exact GUID plus the lane-specific identity and same-instance ranking
evidence.  Arms, conflicting, and unevidenced unknown-spec Warriors remain
environment substitutions and can never become historical Fury
voting/comparison rows through this contract.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, TextIO

from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_wave_model_v2 as model_v2
from . import chronicle_old50_ranking_snapshot_v1 as ranking_snapshot_v1
from . import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1


SCHEMA = "chronicle_old50_warrior_slot_substitution/v1"
BUNDLE_SCHEMA = f"{SCHEMA}/bundle"
EXACT_FURY_EVIDENCE_SCHEMA = f"{SCHEMA}/exact_fury_identity_evidence"
SELECTOR_RECEIPT_SCHEMA = f"{SCHEMA}/exact_fury_selector_receipt"
REVISION = "exact_wave_all_warrior_guid_hybrid_identity_stage6_loo_v1"
STATUS = "READY_DIAGNOSTIC_ENVIRONMENT_SUBSTITUTION_NONVOTING"
EVIDENCE_STATUS = "EXACT_FURY_IDENTITY_EVIDENCE_BOUND_NONVOTING"
SELECTOR_RECEIPT_STATUS = "COMPLETE_20_INSTANCE_EXACT_FURY_SELECTOR_NONVOTING"

FORMAL_RANKING_SNAPSHOT_CONTENT_SHA256 = (
    "55ad2135c0d820ee571ba950708144046f07c69b1721a31eaf21da9f0a61495d"
)
FORMAL_RANKING_SNAPSHOT_FILE_SHA256 = (
    "16ecff9a7b5bed0a86931c1872782b635345612f7e2f49df4292836f6f8b0a5f"
)
FORMAL_RANKING_CAPTURE_RECEIPT_CONTENT_SHA256 = (
    "7869c5efd6a69061d88f8258299d9a66aed77184d1de18bebed152fe4531c422"
)
FORMAL_RANKING_CAPTURE_RECEIPT_FILE_SHA256 = (
    "321b629ecb801ae73fb8f2be8cd1f95ba988884a33f3aeff1fb6fc1d11251fd1"
)
FORMAL_LEADERBOARD_MANIFEST_FILE_SHA256 = (
    "95f82032527bb6a27d9a1820e14aaf304444b7b904bdad439740f81125704f8a"
)
FORMAL_FURY_PDF_FILE_SHA256 = (
    "d0c5bd0ab4ec641003ee32166566a5ea8213810c2849b8e9d0899fb9055aa5a2"
)
FORMAL_CHARACTER_HISTORY_CONTENT_SHA256 = (
    "ce85de50671caf1739216f5d91248a2fd73bd2fc0174ee551faef3adc4d89a31"
)
FORMAL_CHARACTER_HISTORY_FILE_SHA256 = (
    "2fc7461d8626237ae0b1ff86b270045191abf7c8766b3756b92e3d3453bcd43a"
)
FORMAL_CHARACTER_INVENTORY_CONTENT_SHA256 = (
    "c5c2b690adcd0ebc6bc49c49ceac68236e0ef55b6a3ab59b6bc35c763a5d7a8a"
)
FORMAL_CHARACTER_INVENTORY_FILE_SHA256 = (
    "fbde28eb91d4128570bf9ab8612589963a2b788051613e8deb84852da9b653f0"
)
FORMAL_EXACT_DPS_INDEX_CONTENT_SHA256 = (
    "4e506cbba15d51809fad8636a9b440ee14497b874b8c79665ba74e69d749ee9e"
)
FORMAL_EXACT_DPS_INDEX_FILE_SHA256 = (
    "ce8a07f9e0e028a0d1538676618d046ee04e00f14c4168c84a000bc194049822"
)

ATTRIBUTION_KINDS = tuple(sorted(background_v2.PLAYER_ATTRIBUTION_KINDS))
SLOT_LABELS = (
    "EXACT_FURY",
    "STAGE5_OBSERVED_FURY",
    "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY",
    "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY",
)
EXACT_FURY_SELECTION_LANES = (
    "PDF_INTENDED_FURY_EXACT_GUID",
    "GENERIC_EXACT_FURY_FULL_SCOPE",
)
_PRIORITY = {label: index for index, label in enumerate(SLOT_LABELS)}
_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_GUID_RE = re.compile(r"\A0x[0-9A-F]{16}\Z")


class ChronicleOld50WarriorSlotSubstitutionError(RuntimeError):
    """An exact Warrior-slot input could not be derived without widening it."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _optional_sha(value: Any, *, label: str) -> str | None:
    return None if value is None else _sha(value, label=label)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(core))
    value.pop("content_address", None)
    value["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256(value),
    }
    return value


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = _mapping(value.get("content_address"), label=f"{label} content_address")
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    expected = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256(core),
    }
    if dict(address) != expected:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} content address differs"
        )
    return expected["sha256"]


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50WarriorSlotSubstitutionError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50WarriorSlotSubstitutionError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be nonempty text"
        )
    return value


def _guid(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if _GUID_RE.fullmatch(text) is None or int(text[2:], 16) == 0:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be a canonical nonzero player GUID"
        )
    return text


def _integer(value: Any, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be an integer"
        )
    if value < (1 if positive else 0):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be {'positive' if positive else 'nonnegative'}"
        )
    return value


def _number(value: Any, *, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be a nonnegative number"
        )
    return value


def _wave_key(value: Mapping[str, Any]) -> tuple[str, str, int]:
    try:
        return overlap_v1._wave_key(value)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"invalid exact wave identity: {error}"
        ) from error


def build_exact_fury_identity_evidence_v1(
    *,
    selection_lane: str,
    instance_id: str,
    character_guid: str,
    accepted_wave_count: int,
    stage5_manifest_content_sha256: str,
    stage6_manifest_content_sha256: str,
    old50_capsule_content_sha256: str,
    raw_ranking_snapshot_manifest_content_sha256: str,
    raw_ranking_capture_receipt_content_sha256: str,
    raw_ranking_instance_object_sha256: str,
    same_instance_ranking_rows: Sequence[Mapping[str, Any]],
    character_query_record_sha256: str | None = None,
    character_identity_record_sha256: str | None = None,
    character_history_manifest_content_sha256: str | None = None,
    character_history_manifest_file_sha256: str | None = None,
    character_inventory_manifest_content_sha256: str | None = None,
    character_inventory_manifest_file_sha256: str | None = None,
    exact_dps_index_manifest_content_sha256: str | None = None,
    exact_dps_index_manifest_file_sha256: str | None = None,
    player_name: str | None = None,
    board_rank: int | None = None,
    leaderboard_manifest_file_sha256: str | None = None,
    pdf_file_sha256: str | None = None,
    board_manifest_row_sha256: str | None = None,
    stage5_instance_content_sha256: str | None = None,
    exact_roster_evidence_sha256: str | None = None,
    generic_eligible_guids: Sequence[str] = (),
    pdf_candidate_population: Sequence[Mapping[str, Any]] = (),
    equivalent_exact_same_instance_fury: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one selected exact-GUID Fury raid membership.

    The current full-scope ranking contract is nine encounters plus the trash
    aggregate (10 records).  All records must be exact same-instance
    ``WARRIOR``/``Fury`` observations.  A separately bound equivalent source is
    retained for future corpora that expose the same fact without ranking rows.
    """

    lane = _text(selection_lane, label="selection_lane")
    if lane not in EXACT_FURY_SELECTION_LANES:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported exact Fury selection lane"
        )
    canonical_instance = _text(instance_id, label="instance_id")
    canonical_guid = _guid(character_guid, label="character_guid")
    normalized_rows: list[dict[str, Any]] = []
    for raw in same_instance_ranking_rows:
        row = _mapping(raw, label="ranking row")
        encounter_id = row.get("encounter_id")
        if encounter_id is not None:
            encounter_id = _text(encounter_id, label="ranking encounter_id")
        role = row.get("role")
        if role is not None:
            role = _text(role, label="ranking role")
        normalized_rows.append(
            {
                "ranking_row_sha256": _sha(
                    row.get("ranking_row_sha256"), label="ranking row SHA-256"
                ),
                "player_class": _text(
                    row.get("player_class"), label="ranking player_class"
                ).upper(),
                "spec": _text(row.get("spec"), label="ranking spec"),
                "encounter_id": encounter_id,
                "is_trash": row.get("is_trash"),
                "role": role,
            }
        )
    rows = sorted(normalized_rows, key=lambda row: row["ranking_row_sha256"])
    if len({row["ranking_row_sha256"] for row in rows}) != len(rows):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "same-instance ranking row SHA-256 values are duplicated"
        )
    nontrash_encounters = {
        row["encounter_id"]
        for row in rows
        if row["is_trash"] is False and row["encounter_id"] is not None
    }
    trash_count = sum(row["is_trash"] is True for row in rows)
    current_full_scope = (
        len(rows) == 10
        and len(nontrash_encounters) == 9
        and trash_count == 1
        and all(row["player_class"] == "WARRIOR" for row in rows)
        and all(row["spec"] == "Fury" for row in rows)
        and all(isinstance(row["is_trash"], bool) for row in rows)
    )
    equivalent: dict[str, Any] | None = None
    if equivalent_exact_same_instance_fury is not None:
        raw = _mapping(
            equivalent_exact_same_instance_fury,
            label="equivalent exact same-instance Fury evidence",
        )
        equivalent = {
            "source_content_sha256": _sha(
                raw.get("source_content_sha256"),
                label="equivalent Fury source SHA-256",
            ),
            "instance_id": _text(raw.get("instance_id"), label="equivalent instance_id"),
            "character_guid": _guid(
                raw.get("character_guid"), label="equivalent character_guid"
            ),
            "player_class": raw.get("player_class"),
            "observed_spec": raw.get("observed_spec"),
            "exact_guid_match": raw.get("exact_guid_match"),
            "same_instance": raw.get("same_instance"),
            "full_scope": raw.get("full_scope"),
            "inference_used": raw.get("inference_used"),
        }
    equivalent_valid = equivalent == {
        "source_content_sha256": (
            equivalent["source_content_sha256"] if equivalent is not None else None
        ),
        "instance_id": canonical_instance,
        "character_guid": canonical_guid,
        "player_class": "WARRIOR",
        "observed_spec": "Fury",
        "exact_guid_match": True,
        "same_instance": True,
        "full_scope": True,
        "inference_used": False,
    }
    if not current_full_scope and not equivalent_valid:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "EXACT_FURY requires all 10/10 full-scope same-instance ranking records "
            "(9 encounters plus trash) to be WARRIOR/Fury or equivalent exact evidence"
        )
    if lane == "PDF_INTENDED_FURY_EXACT_GUID":
        board = {
            "player_name": _text(player_name, label="player_name"),
            "pdf_rank": _integer(board_rank, label="board_rank", positive=True),
            "leaderboard_manifest_file_sha256": _sha(
                leaderboard_manifest_file_sha256,
                label="leaderboard manifest file SHA-256",
            ),
            "pdf_file_sha256": _sha(pdf_file_sha256, label="PDF file SHA-256"),
            "board_manifest_row_sha256": _sha(
                board_manifest_row_sha256, label="board manifest row SHA-256"
            ),
            "player_name_used_as_identity": False,
            "rank_used_as_identity": False,
        }
        selection_rule = "FROZEN_LOWEST_PDF_RANK_EXACT_GUID"
        pdf_candidates = sorted(
            [
                {
                    "pdf_rank": _integer(
                        _mapping(raw, label="PDF selector candidate").get("pdf_rank"),
                        label="PDF candidate rank",
                        positive=True,
                    ),
                    "board_manifest_row_sha256": _sha(
                        _mapping(raw, label="PDF selector candidate").get(
                            "board_manifest_row_sha256"
                        ),
                        label="PDF candidate board-row SHA-256",
                    ),
                    "exact_chain_status": _mapping(
                        raw, label="PDF selector candidate"
                    ).get("exact_chain_status"),
                    "character_guid": (
                        _guid(
                            _mapping(raw, label="PDF selector candidate").get(
                                "character_guid"
                            ),
                            label="PDF candidate character GUID",
                        )
                        if _mapping(raw, label="PDF selector candidate").get(
                            "character_guid"
                        )
                        is not None
                        else None
                    ),
                    "chain_receipt_sha256": _sha(
                        _mapping(raw, label="PDF selector candidate").get(
                            "chain_receipt_sha256"
                        ),
                        label="PDF candidate chain receipt SHA-256",
                    ),
                }
                for raw in pdf_candidate_population
            ],
            key=lambda row: (row["pdf_rank"], row["board_manifest_row_sha256"]),
        )
        if not pdf_candidates or len(
            {row["board_manifest_row_sha256"] for row in pdf_candidates}
        ) != len(pdf_candidates):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF selector candidate population is empty or duplicated"
            )
        admissible = [
            row
            for row in pdf_candidates
            if row["exact_chain_status"] == "ADMISSIBLE_EXACT_GUID_CHAIN"
        ]
        if any(
            (
                row["exact_chain_status"] == "ADMISSIBLE_EXACT_GUID_CHAIN"
                and row["character_guid"] is None
            )
            or (
                row["exact_chain_status"] == "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN"
                and row["character_guid"] is not None
            )
            or row["exact_chain_status"]
            not in {
                "ADMISSIBLE_EXACT_GUID_CHAIN",
                "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN",
            }
            for row in pdf_candidates
        ) or not admissible:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF selector exact-chain status/GUID population is invalid"
            )
        selected_pdf = admissible[0]
        if (
            selected_pdf["character_guid"] != canonical_guid
            or selected_pdf["pdf_rank"] != board_rank
            or selected_pdf["board_manifest_row_sha256"]
            != board_manifest_row_sha256
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF selected GUID is not the lowest-rank admissible exact chain"
            )
        pdf_selector = {
            "candidate_population": pdf_candidates,
            "candidate_population_count": len(pdf_candidates),
            "candidate_population_sha256": _sha256(pdf_candidates),
            "admissible_candidate_count": len(admissible),
            "selected_guid": canonical_guid,
            "selected_pdf_rank": board_rank,
            "selected_board_manifest_row_sha256": board_manifest_row_sha256,
            "selected_is_minimum_rank_then_board_row_sha": True,
            "player_name_used": False,
            "outcome_fields_used": False,
        }
        generic_selector = None
        query_identity_ref = {
            "query_record_sha256": _sha(
                character_query_record_sha256,
                label="character query record SHA-256",
            ),
            "identity_record_sha256": _sha(
                character_identity_record_sha256,
                label="character identity record SHA-256",
            ),
            "exact_guid_match_required": True,
        }
        history_ref = {
            "manifest_content_sha256": _sha(
                character_history_manifest_content_sha256,
                label="character history manifest content SHA-256",
            ),
            "manifest_file_sha256": _sha(
                character_history_manifest_file_sha256,
                label="character history manifest file SHA-256",
            ),
        }
        inventory_ref = {
            "manifest_content_sha256": _sha(
                character_inventory_manifest_content_sha256,
                label="character inventory manifest content SHA-256",
            ),
            "manifest_file_sha256": _sha(
                character_inventory_manifest_file_sha256,
                label="character inventory manifest file SHA-256",
            ),
        }
        index_ref = {
            "manifest_content_sha256": _sha(
                exact_dps_index_manifest_content_sha256,
                label="exact DPS index manifest content SHA-256",
            ),
            "manifest_file_sha256": _sha(
                exact_dps_index_manifest_file_sha256,
                label="exact DPS index manifest file SHA-256",
            ),
            "identity_match": "EXACT_PLAYER_GUID_ONLY",
        }
    else:
        if any(
            value is not None
            for value in (
                player_name,
                board_rank,
                leaderboard_manifest_file_sha256,
                pdf_file_sha256,
                board_manifest_row_sha256,
            )
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic exact-Fury lane must not consume PDF name/rank provenance"
            )
        board = {
            "player_name": None,
            "pdf_rank": None,
            "leaderboard_manifest_file_sha256": None,
            "pdf_file_sha256": None,
            "board_manifest_row_sha256": None,
            "player_name_used_as_identity": False,
            "rank_used_as_identity": False,
        }
        selection_rule = "LEXICOGRAPHICALLY_SMALLEST_CANONICAL_GUID_AFTER_FULL_SCOPE_FILTER"
        if pdf_candidate_population:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic exact-Fury lane must not carry a PDF selector population"
            )
        pdf_selector = None
        if any(
            value is not None
            for value in (
                character_query_record_sha256,
                character_identity_record_sha256,
                character_history_manifest_content_sha256,
                character_history_manifest_file_sha256,
                character_inventory_manifest_content_sha256,
                character_inventory_manifest_file_sha256,
                exact_dps_index_manifest_content_sha256,
                exact_dps_index_manifest_file_sha256,
            )
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic exact-Fury lane must not carry unrelated character-history/index pins"
            )
        eligible_guids = sorted(
            {_guid(value, label="generic eligible GUID") for value in generic_eligible_guids}
        )
        if not eligible_guids or canonical_guid != eligible_guids[0]:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic selected GUID is not the lexicographic minimum of the full eligible population"
            )
        _sha(
            stage5_instance_content_sha256,
            label="generic Stage-5 instance content SHA-256",
        )
        _sha(
            exact_roster_evidence_sha256,
            label="generic exact roster evidence SHA-256",
        )
        generic_selector = {
            "eligible_guids": eligible_guids,
            "eligible_guid_count": len(eligible_guids),
            "eligible_guids_sha256": _sha256(eligible_guids),
            "selected_guid": canonical_guid,
            "selected_guid_is_lexicographic_minimum": True,
            "eligibility_filter": (
                "exact Stage-5 roster GUID and complete 9-encounter-plus-trash "
                "WARRIOR/Fury ranking scope"
            ),
            "player_name_used": False,
            "outcome_fields_used": False,
            "ranking_role_used": False,
        }
        query_identity_ref = None
        history_ref = None
        inventory_ref = None
        index_ref = None
    basis = (
        "ALL_10_BOUND_SAME_INSTANCE_RANKING_ROWS_WARRIOR_FURY_FULL_SCOPE"
        if current_full_scope
        else "EQUIVALENT_EXACT_GUID_SAME_INSTANCE_FURY_FULL_SCOPE"
    )
    roles = sorted({row["role"] for row in rows if row["role"] is not None})
    core = {
        "schema": EXACT_FURY_EVIDENCE_SCHEMA,
        "revision": REVISION,
        "status": EVIDENCE_STATUS,
        "identity": {
            "instance_id": canonical_instance,
            "character_guid": canonical_guid,
            "player_class": "WARRIOR",
            "player_spec": "Fury",
            "identity_match": "EXACT_PLAYER_GUID_AND_INSTANCE",
        },
        "selection": {
            "selection_lane": lane,
            "selection_rule": selection_rule,
            "accepted_wave_count": _integer(
                accepted_wave_count, label="accepted_wave_count", positive=True
            ),
            "name_used": False,
            "outcome_fields_used": False,
            "pdf_selector_receipt": pdf_selector,
            "generic_selector_receipt": generic_selector,
        },
        "board_provenance_only": board,
        "source_refs": {
            "character_query_and_identity": query_identity_ref,
            "character_history": history_ref,
            "character_instance_inventory": inventory_ref,
            "exact_dps_index": index_ref,
            "offline_wave_closure": {
                "stage5_manifest_content_sha256": _sha(
                    stage5_manifest_content_sha256,
                    label="Stage-5 manifest content SHA-256",
                ),
                "stage6_manifest_content_sha256": _sha(
                    stage6_manifest_content_sha256,
                    label="Stage-6 manifest content SHA-256",
                ),
                "old50_capsule_content_sha256": _sha(
                    old50_capsule_content_sha256,
                    label="old-50 capsule content SHA-256",
                ),
                "stage5_instance_content_sha256": _optional_sha(
                    stage5_instance_content_sha256,
                    label="Stage-5 instance content SHA-256",
                ),
                "exact_roster_evidence_sha256": _optional_sha(
                    exact_roster_evidence_sha256,
                    label="exact roster evidence SHA-256",
                ),
            },
            "raw_ranking_snapshot": {
                "manifest_content_sha256": _sha(
                    raw_ranking_snapshot_manifest_content_sha256,
                    label="raw ranking snapshot manifest content SHA-256",
                ),
                "capture_receipt_content_sha256": _sha(
                    raw_ranking_capture_receipt_content_sha256,
                    label="raw ranking capture receipt content SHA-256",
                ),
                "instance_object_sha256": _sha(
                    raw_ranking_instance_object_sha256,
                    label="raw ranking instance object SHA-256",
                ),
                "same_instance_ranking_population_count": len(rows),
                "same_instance_ranking_population_sha256": _sha256(rows),
                "same_instance_ranking_rows": rows,
            },
            "equivalent_exact_same_instance_fury": equivalent,
        },
        "spec_conclusion": {
            "label": "EXACT_FURY",
            "basis": basis,
            "ranking_record_count_for_guid": len(rows),
            "distinct_encounter_count": len(nontrash_encounters),
            "trash_record_present": trash_count == 1,
            "all_bound_rows_player_class_warrior": bool(rows)
            and all(row["player_class"] == "WARRIOR" for row in rows),
            "all_bound_rows_spec_fury": bool(rows)
            and all(row["spec"] == "Fury" for row in rows),
            "ranking_roles_diagnostic_only": roles,
            "observed_spec": "Fury",
            "inference_used": False,
        },
        "scientific_boundary": {
            "identity_evidence_only": True,
            "ranking_role_is_diagnostic_only": True,
            "ranking_outcomes_used": False,
            "historical_fury_voting_eligible": False,
            "comparison_ready": False,
            "deployment_ready": False,
        },
    }
    value = _content_addressed(core)
    validate_exact_fury_identity_evidence_v1(value)
    return value


def validate_exact_fury_identity_evidence_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, label="exact Fury identity evidence")))
    if (
        raw.get("schema") != EXACT_FURY_EVIDENCE_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != EVIDENCE_STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported exact Fury identity evidence"
        )
    _verify_content_address(raw, label="exact Fury identity evidence")
    identity = _mapping(raw.get("identity"), label="evidence identity")
    instance_id = _text(identity.get("instance_id"), label="evidence instance_id")
    guid = _guid(identity.get("character_guid"), label="evidence character_guid")
    if identity != {
        "instance_id": instance_id,
        "character_guid": guid,
        "player_class": "WARRIOR",
        "player_spec": "Fury",
        "identity_match": "EXACT_PLAYER_GUID_AND_INSTANCE",
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "exact Fury evidence identity authority was weakened"
        )
    selection = _mapping(raw.get("selection"), label="evidence selection")
    lane = selection.get("selection_lane")
    expected_rule = (
        "FROZEN_LOWEST_PDF_RANK_EXACT_GUID"
        if lane == "PDF_INTENDED_FURY_EXACT_GUID"
        else "LEXICOGRAPHICALLY_SMALLEST_CANONICAL_GUID_AFTER_FULL_SCOPE_FILTER"
        if lane == "GENERIC_EXACT_FURY_FULL_SCOPE"
        else None
    )
    if (
        expected_rule is None
        or selection.get("selection_rule") != expected_rule
        or _integer(
            selection.get("accepted_wave_count"),
            label="accepted_wave_count",
            positive=True,
        )
        < 1
        or selection.get("name_used") is not False
        or selection.get("outcome_fields_used") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "exact Fury selection contract differs"
        )
    pdf_selector_raw = selection.get("pdf_selector_receipt")
    selector_raw = selection.get("generic_selector_receipt")
    if lane == "PDF_INTENDED_FURY_EXACT_GUID":
        if selector_raw is not None:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF exact-Fury lane unexpectedly carries a generic selector receipt"
            )
        pdf_selector = _mapping(pdf_selector_raw, label="PDF selector receipt")
        candidates = [
            _mapping(row, label="PDF selector candidate")
            for row in _array(
                pdf_selector.get("candidate_population"),
                label="PDF selector candidate population",
            )
        ]
        for candidate in candidates:
            _integer(candidate.get("pdf_rank"), label="PDF candidate rank", positive=True)
            _sha(
                candidate.get("board_manifest_row_sha256"),
                label="PDF candidate board-row SHA-256",
            )
            _sha(
                candidate.get("chain_receipt_sha256"),
                label="PDF candidate chain receipt SHA-256",
            )
            status = candidate.get("exact_chain_status")
            candidate_guid = candidate.get("character_guid")
            if status == "ADMISSIBLE_EXACT_GUID_CHAIN":
                _guid(candidate_guid, label="admissible PDF candidate GUID")
            elif status == "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN":
                if candidate_guid is not None:
                    raise ChronicleOld50WarriorSlotSubstitutionError(
                        "inadmissible PDF candidate unexpectedly has identity authority"
                    )
            else:
                raise ChronicleOld50WarriorSlotSubstitutionError(
                    "PDF candidate exact-chain status is unsupported"
                )
        expected_candidates = sorted(
            candidates,
            key=lambda row: (row["pdf_rank"], row["board_manifest_row_sha256"]),
        )
        admissible = [
            candidate
            for candidate in candidates
            if candidate.get("exact_chain_status") == "ADMISSIBLE_EXACT_GUID_CHAIN"
        ]
        if (
            not candidates
            or candidates != expected_candidates
            or len({row.get("board_manifest_row_sha256") for row in candidates})
            != len(candidates)
            or not admissible
            or pdf_selector.get("candidate_population_count") != len(candidates)
            or pdf_selector.get("candidate_population_sha256") != _sha256(candidates)
            or pdf_selector.get("admissible_candidate_count") != len(admissible)
            or pdf_selector.get("selected_guid") != guid
            or admissible[0].get("character_guid") != guid
            or pdf_selector.get("selected_pdf_rank")
            != admissible[0].get("pdf_rank")
            or pdf_selector.get("selected_board_manifest_row_sha256")
            != admissible[0].get("board_manifest_row_sha256")
            or pdf_selector.get("selected_is_minimum_rank_then_board_row_sha")
            is not True
            or pdf_selector.get("player_name_used") is not False
            or pdf_selector.get("outcome_fields_used") is not False
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF exact-Fury selector receipt is incomplete"
            )
    else:
        if pdf_selector_raw is not None:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic exact-Fury lane unexpectedly carries a PDF selector receipt"
            )
        selector = _mapping(selector_raw, label="generic selector receipt")
        eligible = [
            _guid(value, label="generic eligible GUID")
            for value in _array(selector.get("eligible_guids"), label="eligible GUIDs")
        ]
        if (
            not eligible
            or eligible != sorted(set(eligible))
            or selector.get("eligible_guid_count") != len(eligible)
            or selector.get("eligible_guids_sha256") != _sha256(eligible)
            or selector.get("selected_guid") != guid
            or guid != eligible[0]
            or selector.get("selected_guid_is_lexicographic_minimum") is not True
            or selector.get("eligibility_filter")
            != (
                "exact Stage-5 roster GUID and complete 9-encounter-plus-trash "
                "WARRIOR/Fury ranking scope"
            )
            or selector.get("player_name_used") is not False
            or selector.get("outcome_fields_used") is not False
            or selector.get("ranking_role_used") is not False
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic exact-Fury selector receipt is incomplete or noncausal"
            )
    board = _mapping(raw.get("board_provenance_only"), label="board provenance")
    if lane == "PDF_INTENDED_FURY_EXACT_GUID":
        _text(board.get("player_name"), label="board player_name")
        _integer(board.get("pdf_rank"), label="PDF rank", positive=True)
        _sha(
            board.get("leaderboard_manifest_file_sha256"),
            label="leaderboard manifest file SHA-256",
        )
        _sha(board.get("pdf_file_sha256"), label="PDF file SHA-256")
        _sha(board.get("board_manifest_row_sha256"), label="board row SHA-256")
    elif any(
        board.get(field) is not None
        for field in (
            "player_name",
            "pdf_rank",
            "leaderboard_manifest_file_sha256",
            "pdf_file_sha256",
            "board_manifest_row_sha256",
        )
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "generic exact-Fury lane consumed PDF name/rank provenance"
        )
    if (
        board.get("player_name_used_as_identity") is not False
        or board.get("rank_used_as_identity") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "board name/rank became identity authority"
        )
    refs = _mapping(raw.get("source_refs"), label="evidence source_refs")
    if lane == "PDF_INTENDED_FURY_EXACT_GUID":
        query = _mapping(
            refs.get("character_query_and_identity"), label="query/identity refs"
        )
        _sha(query.get("query_record_sha256"), label="query record SHA-256")
        _sha(query.get("identity_record_sha256"), label="identity record SHA-256")
        if query.get("exact_guid_match_required") is not True:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "character query/identity exact GUID requirement was weakened"
            )
        history = _mapping(refs.get("character_history"), label="history refs")
        _sha(history.get("manifest_content_sha256"), label="history content SHA-256")
        _sha(history.get("manifest_file_sha256"), label="history file SHA-256")
        inventory = _mapping(
            refs.get("character_instance_inventory"), label="character inventory refs"
        )
        _sha(
            inventory.get("manifest_content_sha256"), label="inventory content SHA-256"
        )
        _sha(inventory.get("manifest_file_sha256"), label="inventory file SHA-256")
        index = _mapping(refs.get("exact_dps_index"), label="exact DPS index refs")
        _sha(index.get("manifest_content_sha256"), label="index content SHA-256")
        _sha(index.get("manifest_file_sha256"), label="index file SHA-256")
        if index.get("identity_match") != "EXACT_PLAYER_GUID_ONLY":
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "DPS index identity match was weakened"
            )
    elif any(
        refs.get(field) is not None
        for field in (
            "character_query_and_identity",
            "character_history",
            "character_instance_inventory",
            "exact_dps_index",
        )
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "generic exact-Fury lane carries unrelated character-history/index refs"
        )
    offline = _mapping(refs.get("offline_wave_closure"), label="offline wave closure")
    for field in (
        "stage5_manifest_content_sha256",
        "stage6_manifest_content_sha256",
        "old50_capsule_content_sha256",
    ):
        _sha(offline.get(field), label=field)
    if lane == "GENERIC_EXACT_FURY_FULL_SCOPE":
        _sha(
            offline.get("stage5_instance_content_sha256"),
            label="generic Stage-5 instance content SHA-256",
        )
        _sha(
            offline.get("exact_roster_evidence_sha256"),
            label="generic exact roster evidence SHA-256",
        )
    elif (
        offline.get("stage5_instance_content_sha256") is not None
        or offline.get("exact_roster_evidence_sha256") is not None
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "PDF exact-Fury lane unexpectedly carries generic roster authority"
        )
    snapshot = _mapping(refs.get("raw_ranking_snapshot"), label="raw ranking snapshot")
    _sha(snapshot.get("manifest_content_sha256"), label="snapshot manifest SHA-256")
    _sha(
        snapshot.get("capture_receipt_content_sha256"),
        label="snapshot capture receipt SHA-256",
    )
    _sha(snapshot.get("instance_object_sha256"), label="ranking object SHA-256")
    rows = [
        _mapping(row, label="bound ranking row")
        for row in _array(
            snapshot.get("same_instance_ranking_rows"),
            label="same-instance ranking rows",
        )
    ]
    row_shas = [
        _sha(row.get("ranking_row_sha256"), label="ranking row SHA-256")
        for row in rows
    ]
    if (
        rows != sorted(rows, key=lambda row: str(row.get("ranking_row_sha256")))
        or len(set(row_shas)) != len(row_shas)
        or snapshot.get("same_instance_ranking_population_count") != len(rows)
        or snapshot.get("same_instance_ranking_population_sha256") != _sha256(rows)
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "same-instance ranking population binding differs"
        )
    nontrash = {
        row.get("encounter_id")
        for row in rows
        if row.get("is_trash") is False and row.get("encounter_id") is not None
    }
    trash_count = sum(row.get("is_trash") is True for row in rows)
    current_full_scope = (
        len(rows) == 10
        and len(nontrash) == 9
        and trash_count == 1
        and all(row.get("player_class") == "WARRIOR" for row in rows)
        and all(row.get("spec") == "Fury" for row in rows)
        and all(isinstance(row.get("is_trash"), bool) for row in rows)
    )
    equivalent_raw = refs.get("equivalent_exact_same_instance_fury")
    equivalent_valid = False
    if equivalent_raw is not None:
        equivalent = _mapping(equivalent_raw, label="equivalent Fury evidence")
        _sha(
            equivalent.get("source_content_sha256"),
            label="equivalent Fury source SHA-256",
        )
        equivalent_valid = (
            equivalent.get("instance_id") == instance_id
            and equivalent.get("character_guid") == guid
            and equivalent.get("player_class") == "WARRIOR"
            and equivalent.get("observed_spec") == "Fury"
            and equivalent.get("exact_guid_match") is True
            and equivalent.get("same_instance") is True
            and equivalent.get("full_scope") is True
            and equivalent.get("inference_used") is False
        )
    conclusion = _mapping(raw.get("spec_conclusion"), label="spec conclusion")
    expected_basis = (
        "ALL_10_BOUND_SAME_INSTANCE_RANKING_ROWS_WARRIOR_FURY_FULL_SCOPE"
        if current_full_scope
        else "EQUIVALENT_EXACT_GUID_SAME_INSTANCE_FURY_FULL_SCOPE"
    )
    roles = sorted(
        {row.get("role") for row in rows if isinstance(row.get("role"), str)}
    )
    if (
        not (current_full_scope or equivalent_valid)
        or conclusion.get("label") != "EXACT_FURY"
        or conclusion.get("basis") != expected_basis
        or conclusion.get("ranking_record_count_for_guid") != len(rows)
        or conclusion.get("distinct_encounter_count") != len(nontrash)
        or conclusion.get("trash_record_present") is not (trash_count == 1)
        or conclusion.get("all_bound_rows_player_class_warrior")
        is not (bool(rows) and all(row.get("player_class") == "WARRIOR" for row in rows))
        or conclusion.get("all_bound_rows_spec_fury")
        is not (bool(rows) and all(row.get("spec") == "Fury" for row in rows))
        or conclusion.get("ranking_roles_diagnostic_only") != roles
        or conclusion.get("observed_spec") != "Fury"
        or conclusion.get("inference_used") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "EXACT_FURY conclusion is not supported by full-scope same-instance evidence"
        )
    if _mapping(raw.get("scientific_boundary"), label="evidence boundary") != {
        "identity_evidence_only": True,
        "ranking_role_is_diagnostic_only": True,
        "ranking_outcomes_used": False,
        "historical_fury_voting_eligible": False,
        "comparison_ready": False,
        "deployment_ready": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "exact Fury evidence scientific boundary widened"
        )
    return raw


def _external_content_sha(value: Mapping[str, Any], *, label: str) -> str:
    """Verify either content-address spelling used by the frozen inputs."""

    address = _mapping(value.get("content_address"), label=f"{label} content_address")
    scope = address.get("scope")
    if (
        address.get("algorithm") != "sha256"
        or scope
        not in {
            "canonical JSON excluding content_address",
            "canonical JSON document excluding content_address",
            "canonical JSON document without content_address",
        }
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} content-address contract is unsupported"
        )
    declared = _sha(address.get("sha256"), label=f"{label} content SHA-256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    canonical = _canonical(core)
    if declared not in {
        hashlib.sha256(canonical).hexdigest(),
        hashlib.sha256(canonical + b"\n").hexdigest(),
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} content address differs"
        )
    return declared


def _read_json_document(path: str | Path, *, label: str) -> tuple[dict[str, Any], str]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = resolved.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"{label} must be a JSON object"
        )
    return value, hashlib.sha256(payload).hexdigest()


def _ranking_rows_for_guid(
    rows: Sequence[Mapping[str, Any]], guid: str
) -> list[dict[str, Any]] | None:
    selected = [row for row in rows if row.get("player_guid") == guid]
    encounter_ids = {
        row.get("encounter_id")
        for row in selected
        if row.get("encounter_id") is not None
    }
    trash_count = sum(row.get("encounter_id") is None for row in selected)
    if (
        len(selected) != 10
        or len(encounter_ids) != 9
        or trash_count != 1
        or any(str(row.get("player_class") or "").upper() != "WARRIOR" for row in selected)
        or any(row.get("player_spec") != "Fury" for row in selected)
    ):
        return None
    return [
        {
            "ranking_row_sha256": _sha256(row),
            "player_class": "WARRIOR",
            "spec": "Fury",
            "encounter_id": row.get("encounter_id"),
            "is_trash": row.get("encounter_id") is None,
            "role": row.get("player_role"),
        }
        for row in selected
    ]


def build_exact_fury_selector_receipt_v1(
    *,
    ranking_snapshot_manifest: Mapping[str, Any],
    ranking_snapshot_manifest_file_sha256: str,
    ranking_capture_receipt: Mapping[str, Any],
    ranking_capture_receipt_file_sha256: str,
    ranking_response_loader: Callable[
        [Mapping[str, Any]], tuple[Sequence[Mapping[str, Any]], str]
    ],
    stage5_manifest: Mapping[str, Any],
    stage5_manifest_file_sha256: str,
    overlap_audit: Mapping[str, Any],
    overlap_audit_file_sha256: str,
    leaderboard_manifest: Mapping[str, Any],
    leaderboard_manifest_file_sha256: str,
    fury_pdf_file_sha256: str,
    character_history_manifest: Mapping[str, Any],
    character_history_manifest_file_sha256: str,
    character_inventory_manifest: Mapping[str, Any],
    character_inventory_manifest_file_sha256: str,
    exact_dps_index_manifest: Mapping[str, Any],
    exact_dps_index_manifest_file_sha256: str,
) -> dict[str, Any]:
    """Derive the frozen 6-PDF/14-generic exact-Fury selector receipt.

    Only identity, class/spec, encounter coverage, and exact Stage-5 roster
    membership participate.  Ranking outcomes, player names, and roles never
    participate in the generic selector; board names are query provenance in
    the PDF lane and the returned exact GUID remains the identity authority.
    """

    snapshot = deepcopy(
        dict(_mapping(ranking_snapshot_manifest, label="ranking snapshot manifest"))
    )
    snapshot_sha = _external_content_sha(snapshot, label="ranking snapshot manifest")
    snapshot_file_sha = _sha(
        ranking_snapshot_manifest_file_sha256,
        label="ranking snapshot manifest file SHA-256",
    )
    capture = deepcopy(
        dict(_mapping(ranking_capture_receipt, label="ranking capture receipt"))
    )
    capture_sha = _external_content_sha(capture, label="ranking capture receipt")
    capture_file_sha = _sha(
        ranking_capture_receipt_file_sha256,
        label="ranking capture receipt file SHA-256",
    )
    if (
        snapshot.get("schema") != ranking_snapshot_v1.SCHEMA
        or capture.get("schema") != ranking_snapshot_v1.RECEIPT_SCHEMA
        or snapshot_sha != FORMAL_RANKING_SNAPSHOT_CONTENT_SHA256
        or snapshot_file_sha != FORMAL_RANKING_SNAPSHOT_FILE_SHA256
        or capture_sha != FORMAL_RANKING_CAPTURE_RECEIPT_CONTENT_SHA256
        or capture_file_sha != FORMAL_RANKING_CAPTURE_RECEIPT_FILE_SHA256
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector must consume the formally frozen ranking snapshot/receipt"
        )
    publication = _mapping(
        capture.get("publication"), label="ranking capture publication"
    )
    if (
        publication.get("manifest_content_sha256") != snapshot_sha
        or publication.get("manifest_file_sha256") != snapshot_file_sha
        or publication.get("manifest_published_after_all_raw_objects") is not True
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "ranking capture receipt does not close the frozen snapshot"
        )

    snapshot_instances = [
        _mapping(row, label="ranking snapshot instance")
        for row in _array(snapshot.get("instances"), label="ranking snapshot instances")
    ]
    instance_ids = [
        _text(row.get("instance_id"), label="ranking instance_id")
        for row in snapshot_instances
    ]
    if (
        len(instance_ids) != 20
        or instance_ids != sorted(set(instance_ids))
        or snapshot.get("instance_order") != instance_ids
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "ranking snapshot is not the exact sorted 20-instance overlap"
        )
    capture_responses = {
        _text(row.get("instance_id"), label="capture response instance_id"): row
        for row in (
            _mapping(value, label="capture response")
            for value in _array(capture.get("responses"), label="capture responses")
        )
    }
    if set(capture_responses) != set(instance_ids):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "ranking capture response population differs from its manifest"
        )
    for entry in snapshot_instances:
        response = _mapping(entry.get("response"), label="ranking response")
        object_ref = _mapping(response.get("object"), label="ranking raw object")
        receipt_row = _mapping(
            capture_responses[str(entry["instance_id"])], label="capture response"
        )
        if (
            response.get("http_status") != 200
            or response.get("response_sha256") != object_ref.get("sha256")
            or receipt_row.get("response_sha256") != object_ref.get("sha256")
            or receipt_row.get("response_size_bytes") != object_ref.get("size_bytes")
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "ranking raw object differs from the capture receipt"
            )

    stage5 = deepcopy(dict(_mapping(stage5_manifest, label="Stage-5 manifest")))
    stage5_sha = _external_content_sha(stage5, label="Stage-5 manifest")
    stage5_file_sha = _sha(
        stage5_manifest_file_sha256, label="Stage-5 manifest file SHA-256"
    )
    if (
        stage5.get("schema") != model_v2.SCHEMA
        or stage5.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or stage5.get("status") != model_v2.STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector input is not the current Stage-5 manifest"
        )
    audit, audit_sha = _validate_overlap_audit(overlap_audit)
    audit_file_sha = _sha(
        overlap_audit_file_sha256, label="overlap audit file SHA-256"
    )
    snapshot_bindings = _mapping(
        snapshot.get("source_bindings"), label="snapshot source bindings"
    )
    if (
        snapshot_bindings.get("stage5_manifest_content_sha256") != stage5_sha
        or snapshot_bindings.get("overlap_audit_content_sha256") != audit_sha
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "snapshot source bindings differ from Stage-5/overlap inputs"
        )
    audit_inputs = _mapping(audit.get("input_bindings"), label="overlap inputs")
    for snapshot_field, audit_field in (
        ("stage5_manifest_content_sha256", "stage5_manifest"),
        ("stage6_manifest_content_sha256", "stage6_manifest"),
        ("old50_capsule_content_sha256", "old50_capsule"),
    ):
        if snapshot_bindings.get(snapshot_field) != _mapping(
            audit_inputs.get(audit_field), label=f"overlap {audit_field} binding"
        ).get("content_sha256"):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "snapshot and overlap source closures differ"
            )

    accepted_counts: Counter[str] = Counter()
    for raw in _array(audit.get("rows"), label="overlap rows"):
        overlap_row = _mapping(raw, label="overlap row")
        if overlap_row.get("classification") in {"EXACT", "SUBSET"}:
            accepted_counts[
                _text(
                    _mapping(overlap_row.get("key"), label="overlap key").get(
                        "instance_id"
                    ),
                    label="overlap instance_id",
                )
            ] += 1
    if set(accepted_counts) != set(instance_ids) or any(
        accepted_counts[instance_id] < 1 for instance_id in instance_ids
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "accepted overlap rows do not cover the frozen 20 instances exactly"
        )

    stage5_entries = {
        _text(row.get("instance_id"), label="Stage-5 instance_id"): row
        for row in (
            _mapping(value, label="Stage-5 instance")
            for value in _array(stage5.get("instances"), label="Stage-5 instances")
        )
    }
    if not set(instance_ids) <= set(stage5_entries):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-5 manifest omits a frozen overlap instance"
        )

    history = deepcopy(
        dict(_mapping(character_history_manifest, label="character history manifest"))
    )
    history_sha = _external_content_sha(history, label="character history manifest")
    inventory = deepcopy(
        dict(_mapping(character_inventory_manifest, label="character inventory manifest"))
    )
    inventory_sha = _external_content_sha(
        inventory, label="character inventory manifest"
    )
    dps_index = deepcopy(
        dict(_mapping(exact_dps_index_manifest, label="exact DPS index manifest"))
    )
    dps_index_sha = _external_content_sha(dps_index, label="exact DPS index manifest")
    source_file_pins = {
        "leaderboard_manifest_file_sha256": _sha(
            leaderboard_manifest_file_sha256,
            label="leaderboard manifest file SHA-256",
        ),
        "fury_pdf_file_sha256": _sha(
            fury_pdf_file_sha256, label="Fury PDF file SHA-256"
        ),
        "character_history_manifest_file_sha256": _sha(
            character_history_manifest_file_sha256,
            label="character history manifest file SHA-256",
        ),
        "character_inventory_manifest_file_sha256": _sha(
            character_inventory_manifest_file_sha256,
            label="character inventory manifest file SHA-256",
        ),
        "exact_dps_index_manifest_file_sha256": _sha(
            exact_dps_index_manifest_file_sha256,
            label="exact DPS index manifest file SHA-256",
        ),
    }
    if (
        source_file_pins["leaderboard_manifest_file_sha256"]
        != FORMAL_LEADERBOARD_MANIFEST_FILE_SHA256
        or source_file_pins["fury_pdf_file_sha256"] != FORMAL_FURY_PDF_FILE_SHA256
        or history_sha != FORMAL_CHARACTER_HISTORY_CONTENT_SHA256
        or source_file_pins["character_history_manifest_file_sha256"]
        != FORMAL_CHARACTER_HISTORY_FILE_SHA256
        or inventory_sha != FORMAL_CHARACTER_INVENTORY_CONTENT_SHA256
        or source_file_pins["character_inventory_manifest_file_sha256"]
        != FORMAL_CHARACTER_INVENTORY_FILE_SHA256
        or dps_index_sha != FORMAL_EXACT_DPS_INDEX_CONTENT_SHA256
        or source_file_pins["exact_dps_index_manifest_file_sha256"]
        != FORMAL_EXACT_DPS_INDEX_FILE_SHA256
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "PDF identity lane differs from the frozen local source chain"
        )
    inventory_sources = _mapping(
        inventory.get("source_bindings"), label="inventory source bindings"
    )
    dps_inventory = _mapping(
        dps_index.get("source_inventory"), label="DPS-index inventory binding"
    )
    if history_sha not in _array(
        inventory_sources.get("character_history_manifest_sha256"),
        label="inventory history bindings",
    ) or (
        dps_inventory.get("content_sha256") != inventory_sha
        or dps_inventory.get("file_sha256")
        != source_file_pins["character_inventory_manifest_file_sha256"]
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "history/inventory/DPS-index exact source chain differs"
        )

    board = _mapping(leaderboard_manifest, label="leaderboard manifest")
    if (
        board.get("kind") != "chronicle_leaderboard_pdf_manifest"
        or board.get("selection_policy") != "all_rows_in_supplied_pdf_prints"
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "leaderboard manifest is not the frozen all-row PDF extraction"
        )
    board_server = _text(board.get("server"), label="leaderboard server")
    fury_entries = [
        deepcopy(dict(row))
        for row in (
            _mapping(value, label="leaderboard entry")
            for value in _array(board.get("entries"), label="leaderboard entries")
        )
        if str(row.get("board_class") or "").upper() == "WARRIOR"
        and row.get("board_spec") == "Fury"
        and row.get("observed_spec") == "Fury"
    ]
    history_by_query: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for raw in _array(history.get("characters"), label="history characters"):
        character = _mapping(raw, label="history character")
        query = _mapping(character.get("query"), label="history query")
        query_key = (
            _text(query.get("server"), label="history query server"),
            _text(query.get("realm"), label="history query realm"),
            _text(query.get("character"), label="history query character"),
        )
        if query_key in history_by_query:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "character history duplicates an exact query key"
            )
        history_by_query[query_key] = character

    selector_rows: list[dict[str, Any]] = []
    pdf_chain_receipts: list[dict[str, Any]] = []
    for snapshot_entry in snapshot_instances:
        instance_id = str(snapshot_entry["instance_id"])
        response = _mapping(snapshot_entry.get("response"), label="ranking response")
        object_ref = _mapping(response.get("object"), label="ranking raw object")
        loaded_rows, loaded_sha = ranking_response_loader(snapshot_entry)
        ranking_rows = [
            deepcopy(dict(_mapping(row, label="raw ranking row")))
            for row in loaded_rows
        ]
        expected_object_sha = _sha(
            object_ref.get("sha256"), label="ranking raw object SHA-256"
        )
        if (
            loaded_sha != expected_object_sha
            or response.get("record_count") != len(ranking_rows)
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "loaded ranking rows differ from the frozen raw object"
            )

        stage5_entry = _mapping(
            stage5_entries[instance_id], label="Stage-5 selected instance"
        )
        stage5_instance_sha = _external_content_sha(
            stage5_entry, label="Stage-5 selected instance"
        )
        roster_sha = _sha(
            stage5_entry.get("exact_roster_evidence_sha256"),
            label="Stage-5 exact roster evidence SHA-256",
        )
        provenance = _mapping(
            stage5_entry.get("instance_provenance"),
            label="Stage-5 instance provenance",
        )
        warrior_evidence = _mapping(
            provenance.get("warrior_spec_evidence"),
            label="Stage-5 Warrior evidence",
        )
        roster_observations: dict[str, Mapping[str, Any]] = {}
        for raw in _array(
            warrior_evidence.get("observations"),
            label="Stage-5 Warrior observations",
        ):
            observation = _mapping(raw, label="Stage-5 Warrior observation")
            if str(observation.get("player_class") or "").casefold() != "warrior":
                continue
            roster_guid = _guid(
                observation.get("player_guid"), label="Stage-5 roster GUID"
            )
            if roster_guid in roster_observations:
                raise ChronicleOld50WarriorSlotSubstitutionError(
                    "Stage-5 exact Warrior roster duplicates a GUID"
                )
            roster_observations[roster_guid] = observation
        if not roster_observations:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Stage-5 overlap instance has no exact Warrior roster GUID"
            )
        full_scope_by_guid = {
            guid: normalized
            for guid in sorted(roster_observations)
            if (normalized := _ranking_rows_for_guid(ranking_rows, guid)) is not None
        }
        if not full_scope_by_guid:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "overlap instance has no full-scope exact Stage-5 Warrior/Fury GUID"
            )
        slugs = {
            str(row.get("log_hashed_slug"))
            for row in ranking_rows
            if isinstance(row.get("log_hashed_slug"), str)
            and row.get("log_hashed_slug")
        }
        if len(slugs) != 1:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "ranking object does not identify one exact raid slug"
            )
        instance_slug = next(iter(slugs))
        board_candidates = sorted(
            [row for row in fury_entries if row.get("instance_slug") == instance_slug],
            key=lambda row: (
                _integer(row.get("rank"), label="PDF rank", positive=True),
                _sha256(row),
            ),
        )
        candidate_inputs: list[dict[str, Any]] = []
        detailed_candidates: list[dict[str, Any]] = []
        for board_row in board_candidates:
            board_row_sha = _sha256(board_row)
            query = {
                "server": board_server,
                "realm": _text(board_row.get("realm"), label="board realm"),
                "character": _text(
                    board_row.get("character"), label="board character"
                ),
            }
            history_character = history_by_query.get(
                (query["server"], query["realm"], query["character"])
            )
            chain_guid: str | None = None
            identity_object_sha: str | None = None
            instances_page_object_sha: str | None = None
            if history_character is not None:
                identity = _mapping(
                    history_character.get("identity"), label="history identity"
                )
                candidate_guid = _guid(
                    identity.get("guid"), label="history identity GUID"
                )
                matching_instances = [
                    value
                    for value in (
                        _mapping(row, label="history instance")
                        for row in _array(
                            history_character.get("instances"),
                            label="history instances",
                        )
                    )
                    if value.get("instance_id") == instance_id
                    and value.get("slug") == instance_slug
                    and value.get("character_guid") == candidate_guid
                ]
                pages = _array(history_character.get("pages"), label="history pages")
                if (
                    len(matching_instances) == 1
                    and len(pages) == 1
                    and str(identity.get("class") or "").upper() == "WARRIOR"
                    and identity.get("spec") == "Fury"
                    and candidate_guid in full_scope_by_guid
                ):
                    chain_guid = candidate_guid
                    identity_object_sha = _sha(
                        _mapping(
                            _mapping(
                                history_character.get("identity_response"),
                                label="history identity response",
                            ).get("object"),
                            label="history identity object",
                        ).get("sha256"),
                        label="history identity object SHA-256",
                    )
                    instances_page_object_sha = _sha(
                        _mapping(
                            _mapping(pages[0], label="history page").get("object"),
                            label="history page object",
                        ).get("sha256"),
                        label="history page object SHA-256",
                    )
            exact_status = (
                "ADMISSIBLE_EXACT_GUID_CHAIN"
                if chain_guid is not None
                else "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN"
            )
            chain_core = {
                "schema": f"{SELECTOR_RECEIPT_SCHEMA}/pdf_chain",
                "revision": REVISION,
                "instance_id": instance_id,
                "instance_slug": instance_slug,
                "pdf_rank": int(board_row["rank"]),
                "player_name_provenance": query["character"],
                "board_manifest_row_sha256": board_row_sha,
                "query_record_sha256": _sha256(query),
                "identity_response_object_sha256": identity_object_sha,
                "instances_page_object_sha256": instances_page_object_sha,
                "character_guid": chain_guid,
                "exact_chain_status": exact_status,
                "same_returned_instance_uuid_and_slug_required": True,
                "name_used_to_retrieve_authoritative_identity_only": True,
                "name_used_as_identity": False,
                "outcome_fields_used": False,
            }
            chain_receipt = _content_addressed(chain_core)
            pdf_chain_receipts.append(chain_receipt)
            detailed_candidates.append(chain_receipt)
            candidate_inputs.append(
                {
                    "pdf_rank": int(board_row["rank"]),
                    "board_manifest_row_sha256": board_row_sha,
                    "exact_chain_status": exact_status,
                    "character_guid": chain_guid,
                    "chain_receipt_sha256": chain_receipt["content_address"]["sha256"],
                }
            )
        admissible = [
            row
            for row in detailed_candidates
            if row["exact_chain_status"] == "ADMISSIBLE_EXACT_GUID_CHAIN"
        ]
        if admissible:
            selected_chain = admissible[0]
            selected_guid = str(selected_chain["character_guid"])
            lane = "PDF_INTENDED_FURY_EXACT_GUID"
            selected_ranking_rows = full_scope_by_guid[selected_guid]
            evidence = build_exact_fury_identity_evidence_v1(
                selection_lane=lane,
                instance_id=instance_id,
                character_guid=selected_guid,
                accepted_wave_count=accepted_counts[instance_id],
                stage5_manifest_content_sha256=stage5_sha,
                stage6_manifest_content_sha256=str(
                    snapshot_bindings["stage6_manifest_content_sha256"]
                ),
                old50_capsule_content_sha256=str(
                    snapshot_bindings["old50_capsule_content_sha256"]
                ),
                raw_ranking_snapshot_manifest_content_sha256=snapshot_sha,
                raw_ranking_capture_receipt_content_sha256=capture_sha,
                raw_ranking_instance_object_sha256=expected_object_sha,
                same_instance_ranking_rows=selected_ranking_rows,
                character_query_record_sha256=str(
                    selected_chain["query_record_sha256"]
                ),
                character_identity_record_sha256=str(
                    selected_chain["identity_response_object_sha256"]
                ),
                character_history_manifest_content_sha256=history_sha,
                character_history_manifest_file_sha256=source_file_pins[
                    "character_history_manifest_file_sha256"
                ],
                character_inventory_manifest_content_sha256=inventory_sha,
                character_inventory_manifest_file_sha256=source_file_pins[
                    "character_inventory_manifest_file_sha256"
                ],
                exact_dps_index_manifest_content_sha256=dps_index_sha,
                exact_dps_index_manifest_file_sha256=source_file_pins[
                    "exact_dps_index_manifest_file_sha256"
                ],
                player_name=str(selected_chain["player_name_provenance"]),
                board_rank=int(selected_chain["pdf_rank"]),
                leaderboard_manifest_file_sha256=source_file_pins[
                    "leaderboard_manifest_file_sha256"
                ],
                pdf_file_sha256=source_file_pins["fury_pdf_file_sha256"],
                board_manifest_row_sha256=str(
                    selected_chain["board_manifest_row_sha256"]
                ),
                pdf_candidate_population=candidate_inputs,
            )
            pdf_provenance = {
                "player_name": selected_chain["player_name_provenance"],
                "pdf_rank": selected_chain["pdf_rank"],
                "name_or_rank_used_as_identity": False,
            }
        else:
            selected_guid = sorted(full_scope_by_guid)[0]
            lane = "GENERIC_EXACT_FURY_FULL_SCOPE"
            selected_ranking_rows = full_scope_by_guid[selected_guid]
            evidence = build_exact_fury_identity_evidence_v1(
                selection_lane=lane,
                instance_id=instance_id,
                character_guid=selected_guid,
                accepted_wave_count=accepted_counts[instance_id],
                stage5_manifest_content_sha256=stage5_sha,
                stage6_manifest_content_sha256=str(
                    snapshot_bindings["stage6_manifest_content_sha256"]
                ),
                old50_capsule_content_sha256=str(
                    snapshot_bindings["old50_capsule_content_sha256"]
                ),
                raw_ranking_snapshot_manifest_content_sha256=snapshot_sha,
                raw_ranking_capture_receipt_content_sha256=capture_sha,
                raw_ranking_instance_object_sha256=expected_object_sha,
                same_instance_ranking_rows=selected_ranking_rows,
                stage5_instance_content_sha256=stage5_instance_sha,
                exact_roster_evidence_sha256=roster_sha,
                generic_eligible_guids=sorted(full_scope_by_guid),
            )
            pdf_provenance = None
        selected_observation = roster_observations[selected_guid]
        selector_rows.append(
            {
                "instance_id": instance_id,
                "selection_lane": lane,
                "player_guid": selected_guid,
                "player_class": "WARRIOR",
                "player_spec": "Fury",
                "pdf_provenance_only": pdf_provenance,
                "accepted_wave_count": accepted_counts[instance_id],
                "ranking_record_count_for_guid": 10,
                "distinct_encounter_count": 9,
                "trash_record_present": True,
                "ranking_roles_diagnostic_only": evidence["spec_conclusion"][
                    "ranking_roles_diagnostic_only"
                ],
                "stage5_spec_observation": {
                    "player_guid": selected_guid,
                    "player_class": "Warrior",
                    "observed_spec": selected_observation.get("player_spec"),
                    "evidence_status": selected_observation.get(
                        "spec_evidence_status"
                    ),
                    "player_name_excluded": True,
                },
                "name_used": False,
                "outcome_fields_used": False,
                "exact_fury_identity_evidence": evidence,
                "exact_fury_identity_evidence_content_sha256": evidence[
                    "content_address"
                ]["sha256"],
            }
        )

    selector_rows.sort(key=lambda row: row["instance_id"])
    pdf_chain_receipts.sort(
        key=lambda row: (
            row["instance_id"],
            row["pdf_rank"],
            row["board_manifest_row_sha256"],
        )
    )
    lane_counts = Counter(row["selection_lane"] for row in selector_rows)
    admissible_count = sum(
        row["exact_chain_status"] == "ADMISSIBLE_EXACT_GUID_CHAIN"
        for row in pdf_chain_receipts
    )
    if (
        lane_counts != Counter(
            {
                "PDF_INTENDED_FURY_EXACT_GUID": 6,
                "GENERIC_EXACT_FURY_FULL_SCOPE": 14,
            }
        )
        or len(pdf_chain_receipts) != 16
        or admissible_count != 12
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "formal selector did not reproduce the frozen 6/14 lane split and 12/16 PDF chains"
        )
    core = {
        "schema": SELECTOR_RECEIPT_SCHEMA,
        "revision": REVISION,
        "status": SELECTOR_RECEIPT_STATUS,
        "source_bindings": {
            "ranking_snapshot_manifest": {
                "content_sha256": snapshot_sha,
                "file_sha256": snapshot_file_sha,
            },
            "ranking_capture_receipt": {
                "content_sha256": capture_sha,
                "file_sha256": capture_file_sha,
            },
            "stage5_manifest": {
                "content_sha256": stage5_sha,
                "file_sha256": stage5_file_sha,
            },
            "stage6_manifest_content_sha256": snapshot_bindings[
                "stage6_manifest_content_sha256"
            ],
            "old50_capsule_content_sha256": snapshot_bindings[
                "old50_capsule_content_sha256"
            ],
            "overlap_audit": {
                "content_sha256": audit_sha,
                "file_sha256": audit_file_sha,
            },
            "leaderboard_manifest_file_sha256": source_file_pins[
                "leaderboard_manifest_file_sha256"
            ],
            "fury_pdf_file_sha256": source_file_pins["fury_pdf_file_sha256"],
            "character_history_manifest": {
                "content_sha256": history_sha,
                "file_sha256": source_file_pins[
                    "character_history_manifest_file_sha256"
                ],
            },
            "character_inventory_manifest": {
                "content_sha256": inventory_sha,
                "file_sha256": source_file_pins[
                    "character_inventory_manifest_file_sha256"
                ],
            },
            "exact_dps_index_manifest": {
                "content_sha256": dps_index_sha,
                "file_sha256": source_file_pins[
                    "exact_dps_index_manifest_file_sha256"
                ],
            },
        },
        "instance_order": instance_ids,
        "rows": selector_rows,
        "pdf_chain_receipts": pdf_chain_receipts,
        "summary": {
            "instance_count": len(selector_rows),
            "accepted_wave_count": sum(accepted_counts.values()),
            "selection_lane_counts": dict(sorted(lane_counts.items())),
            "pdf_candidate_count": len(pdf_chain_receipts),
            "pdf_admissible_exact_chain_count": admissible_count,
            "pdf_inadmissible_exact_chain_count": len(pdf_chain_receipts)
            - admissible_count,
            "exact_fury_row_count": len(selector_rows),
        },
        "scientific_boundary": {
            "identity_and_environment_substitution_input_only": True,
            "ranking_outcomes_used": False,
            "generic_name_or_role_used": False,
            "historical_fury_voting_started": False,
            "comparison_ready": False,
            "deployment_ready": False,
        },
    }
    result = _content_addressed(core)
    validate_exact_fury_selector_receipt_v1(result)
    return result


def validate_exact_fury_selector_receipt_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, label="exact Fury selector receipt")))
    if (
        raw.get("schema") != SELECTOR_RECEIPT_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != SELECTOR_RECEIPT_STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported exact Fury selector receipt"
        )
    _verify_content_address(raw, label="exact Fury selector receipt")
    bindings = _mapping(raw.get("source_bindings"), label="selector source bindings")
    snapshot = _mapping(
        bindings.get("ranking_snapshot_manifest"), label="selector ranking snapshot"
    )
    capture = _mapping(
        bindings.get("ranking_capture_receipt"), label="selector capture receipt"
    )
    history = _mapping(
        bindings.get("character_history_manifest"), label="selector history binding"
    )
    inventory = _mapping(
        bindings.get("character_inventory_manifest"),
        label="selector inventory binding",
    )
    dps_index = _mapping(
        bindings.get("exact_dps_index_manifest"), label="selector DPS-index binding"
    )
    stage5_binding = _mapping(
        bindings.get("stage5_manifest"), label="selector Stage-5 binding"
    )
    overlap_binding = _mapping(
        bindings.get("overlap_audit"), label="selector overlap binding"
    )
    fixed = (
        snapshot.get("content_sha256") == FORMAL_RANKING_SNAPSHOT_CONTENT_SHA256
        and snapshot.get("file_sha256") == FORMAL_RANKING_SNAPSHOT_FILE_SHA256
        and capture.get("content_sha256")
        == FORMAL_RANKING_CAPTURE_RECEIPT_CONTENT_SHA256
        and capture.get("file_sha256")
        == FORMAL_RANKING_CAPTURE_RECEIPT_FILE_SHA256
        and bindings.get("leaderboard_manifest_file_sha256")
        == FORMAL_LEADERBOARD_MANIFEST_FILE_SHA256
        and bindings.get("fury_pdf_file_sha256") == FORMAL_FURY_PDF_FILE_SHA256
        and history
        == {
            "content_sha256": FORMAL_CHARACTER_HISTORY_CONTENT_SHA256,
            "file_sha256": FORMAL_CHARACTER_HISTORY_FILE_SHA256,
        }
        and inventory
        == {
            "content_sha256": FORMAL_CHARACTER_INVENTORY_CONTENT_SHA256,
            "file_sha256": FORMAL_CHARACTER_INVENTORY_FILE_SHA256,
        }
        and dps_index
        == {
            "content_sha256": FORMAL_EXACT_DPS_INDEX_CONTENT_SHA256,
            "file_sha256": FORMAL_EXACT_DPS_INDEX_FILE_SHA256,
        }
        and stage5_binding.get("content_sha256")
        == ranking_snapshot_v1.STAGE5_MANIFEST_CONTENT_SHA256
        and bindings.get("stage6_manifest_content_sha256")
        == ranking_snapshot_v1.STAGE6_MANIFEST_CONTENT_SHA256
        and bindings.get("old50_capsule_content_sha256")
        == ranking_snapshot_v1.OLD50_CAPSULE_CONTENT_SHA256
        and overlap_binding.get("content_sha256")
        == ranking_snapshot_v1.OVERLAP_AUDIT_CONTENT_SHA256
    )
    if not fixed:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector receipt is not bound to the formal snapshot/source closure"
        )
    for source, label in (
        (stage5_binding, "selector Stage-5 binding"),
        (overlap_binding, "selector overlap binding"),
    ):
        _sha(source.get("content_sha256"), label=f"{label} content SHA-256")
        _sha(source.get("file_sha256"), label=f"{label} file SHA-256")

    chain_receipts = [
        deepcopy(dict(_mapping(row, label="PDF chain receipt")))
        for row in _array(raw.get("pdf_chain_receipts"), label="PDF chain receipts")
    ]
    chain_by_instance: dict[str, list[dict[str, Any]]] = {}
    for receipt in chain_receipts:
        if (
            receipt.get("schema") != f"{SELECTOR_RECEIPT_SCHEMA}/pdf_chain"
            or receipt.get("revision") != REVISION
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF chain receipt schema differs"
            )
        receipt_sha = _verify_content_address(receipt, label="PDF chain receipt")
        instance_id = _text(receipt.get("instance_id"), label="PDF chain instance_id")
        _text(receipt.get("instance_slug"), label="PDF chain instance slug")
        _integer(receipt.get("pdf_rank"), label="PDF chain rank", positive=True)
        _text(
            receipt.get("player_name_provenance"),
            label="PDF chain player-name provenance",
        )
        _sha(receipt.get("board_manifest_row_sha256"), label="PDF board-row SHA-256")
        _sha(receipt.get("query_record_sha256"), label="PDF query-record SHA-256")
        status = receipt.get("exact_chain_status")
        if status == "ADMISSIBLE_EXACT_GUID_CHAIN":
            _guid(receipt.get("character_guid"), label="PDF exact character GUID")
            _sha(
                receipt.get("identity_response_object_sha256"),
                label="PDF identity object SHA-256",
            )
            _sha(
                receipt.get("instances_page_object_sha256"),
                label="PDF instances-page object SHA-256",
            )
        elif status == "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN":
            if any(
                receipt.get(field) is not None
                for field in (
                    "character_guid",
                    "identity_response_object_sha256",
                    "instances_page_object_sha256",
                )
            ):
                raise ChronicleOld50WarriorSlotSubstitutionError(
                    "inadmissible PDF chain carries exact identity authority"
                )
        else:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "unsupported PDF exact-chain status"
            )
        if (
            receipt.get("same_returned_instance_uuid_and_slug_required") is not True
            or receipt.get("name_used_to_retrieve_authoritative_identity_only")
            is not True
            or receipt.get("name_used_as_identity") is not False
            or receipt.get("outcome_fields_used") is not False
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "PDF identity/query boundary was widened"
            )
        receipt["_validated_sha256"] = receipt_sha
        chain_by_instance.setdefault(instance_id, []).append(receipt)
    expected_chain_order = sorted(
        chain_receipts,
        key=lambda row: (
            row["instance_id"],
            row["pdf_rank"],
            row["board_manifest_row_sha256"],
        ),
    )
    if chain_receipts != expected_chain_order:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "PDF chain receipts are not deterministically ordered"
        )

    rows = [
        _mapping(row, label="selector row")
        for row in _array(raw.get("rows"), label="selector rows")
    ]
    order = [
        _text(value, label="selector instance_order")
        for value in _array(raw.get("instance_order"), label="selector instance_order")
    ]
    row_ids = [_text(row.get("instance_id"), label="selector instance_id") for row in rows]
    if (
        len(rows) != 20
        or order != sorted(set(order))
        or row_ids != order
        or len(set(row_ids)) != 20
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector receipt is not one stable row per frozen instance"
        )
    lane_counts: Counter[str] = Counter()
    accepted_wave_total = 0
    for row in rows:
        instance_id = str(row["instance_id"])
        lane = row.get("selection_lane")
        if lane not in EXACT_FURY_SELECTION_LANES:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "selector row lane is unsupported"
            )
        lane_counts[str(lane)] += 1
        guid = _guid(row.get("player_guid"), label="selector player GUID")
        evidence = validate_exact_fury_identity_evidence_v1(
            _mapping(
                row.get("exact_fury_identity_evidence"),
                label="selector exact Fury evidence",
            )
        )
        evidence_snapshot = _mapping(
            _mapping(evidence.get("source_refs"), label="selector evidence refs").get(
                "raw_ranking_snapshot"
            ),
            label="selector evidence ranking snapshot",
        )
        accepted_waves = _integer(
            row.get("accepted_wave_count"),
            label="selector accepted wave count",
            positive=True,
        )
        accepted_wave_total += accepted_waves
        conclusion = _mapping(
            evidence.get("spec_conclusion"), label="selector spec conclusion"
        )
        if (
            row.get("player_class") != "WARRIOR"
            or row.get("player_spec") != "Fury"
            or evidence["identity"]["instance_id"] != instance_id
            or evidence["identity"]["character_guid"] != guid
            or evidence["selection"]["selection_lane"] != lane
            or evidence["selection"]["accepted_wave_count"] != accepted_waves
            or row.get("exact_fury_identity_evidence_content_sha256")
            != evidence["content_address"]["sha256"]
            or evidence_snapshot.get("manifest_content_sha256")
            != snapshot["content_sha256"]
            or evidence_snapshot.get("capture_receipt_content_sha256")
            != capture["content_sha256"]
            or row.get("ranking_record_count_for_guid")
            != conclusion.get("ranking_record_count_for_guid")
            or row.get("distinct_encounter_count")
            != conclusion.get("distinct_encounter_count")
            or row.get("trash_record_present")
            != conclusion.get("trash_record_present")
            or row.get("ranking_roles_diagnostic_only")
            != conclusion.get("ranking_roles_diagnostic_only")
            or row.get("name_used") is not False
            or row.get("outcome_fields_used") is not False
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "selector row differs from its exact-Fury evidence"
            )
        stage5_observation = _mapping(
            row.get("stage5_spec_observation"), label="selector Stage-5 observation"
        )
        if (
            stage5_observation.get("player_guid") != guid
            or stage5_observation.get("player_class") != "Warrior"
            or stage5_observation.get("player_name_excluded") is not True
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "selector lost the exact Stage-5 roster/spec observation"
            )
        compact_candidates = [
            {
                "pdf_rank": receipt["pdf_rank"],
                "board_manifest_row_sha256": receipt[
                    "board_manifest_row_sha256"
                ],
                "exact_chain_status": receipt["exact_chain_status"],
                "character_guid": receipt["character_guid"],
                "chain_receipt_sha256": receipt["_validated_sha256"],
            }
            for receipt in chain_by_instance.get(instance_id, [])
        ]
        if lane == "PDF_INTENDED_FURY_EXACT_GUID":
            provenance = _mapping(
                row.get("pdf_provenance_only"), label="selector PDF provenance"
            )
            evidence_selector = _mapping(
                evidence["selection"].get("pdf_selector_receipt"),
                label="evidence PDF selector",
            )
            if (
                not compact_candidates
                or evidence_selector.get("candidate_population")
                != compact_candidates
                or provenance.get("player_name")
                != evidence["board_provenance_only"]["player_name"]
                or provenance.get("pdf_rank")
                != evidence["board_provenance_only"]["pdf_rank"]
                or provenance.get("name_or_rank_used_as_identity") is not False
            ):
                raise ChronicleOld50WarriorSlotSubstitutionError(
                    "PDF selector row differs from its full candidate population"
                )
        elif (
            row.get("pdf_provenance_only") is not None
            or compact_candidates
            or evidence["selection"].get("pdf_selector_receipt") is not None
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "generic selector consumed PDF provenance"
            )

    summary = _mapping(raw.get("summary"), label="selector summary")
    admissible_count = sum(
        row.get("exact_chain_status") == "ADMISSIBLE_EXACT_GUID_CHAIN"
        for row in chain_receipts
    )
    expected_lane_counts = {
        "GENERIC_EXACT_FURY_FULL_SCOPE": 14,
        "PDF_INTENDED_FURY_EXACT_GUID": 6,
    }
    if (
        dict(sorted(lane_counts.items())) != expected_lane_counts
        or len(chain_receipts) != 16
        or admissible_count != 12
        or summary
        != {
            "instance_count": 20,
            "accepted_wave_count": accepted_wave_total,
            "selection_lane_counts": expected_lane_counts,
            "pdf_candidate_count": 16,
            "pdf_admissible_exact_chain_count": 12,
            "pdf_inadmissible_exact_chain_count": 4,
            "exact_fury_row_count": 20,
        }
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector summary differs from the formal 6/14 receipt"
        )
    if _mapping(raw.get("scientific_boundary"), label="selector boundary") != {
        "identity_and_environment_substitution_input_only": True,
        "ranking_outcomes_used": False,
        "generic_name_or_role_used": False,
        "historical_fury_voting_started": False,
        "comparison_ready": False,
        "deployment_ready": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selector scientific boundary was widened"
        )
    return raw


def _validate_overlap_audit(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    audit = deepcopy(dict(_mapping(value, label="overlap audit")))
    if (
        audit.get("schema") != overlap_v1.AUDIT_SCHEMA
        or audit.get("revision") != overlap_v1.REVISION
        or audit.get("status") != "COMPLETE_DESCRIPTIVE_NONVOTING"
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "input is not a completed Stage6-old50 overlap audit"
        )
    try:
        digest = overlap_v1._verify_content_address(audit, label="overlap audit")
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"invalid overlap audit content address: {error}"
        ) from error
    boundary = _mapping(audit.get("scientific_boundary"), label="overlap boundary")
    if (
        boundary.get("nonvoting") is not True
        or boundary.get("comparison_ready") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "overlap audit scientific boundary was widened"
        )
    return audit, digest


def _validate_stage5_wave(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    wave = deepcopy(dict(_mapping(value, label="Stage-5 wave")))
    if (
        wave.get("schema") != model_v2.PARTITION_RECORD_SCHEMA
        or wave.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or wave.get("status") != model_v2.STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "input is not the current Stage-5 wave"
        )
    identity = _mapping(wave.get("wave"), label="Stage-5 wave identity")
    provenance = _mapping(wave.get("raid_provenance"), label="Stage-5 provenance")
    contamination = _mapping(
        provenance.get("contamination"), label="Stage-5 raw contamination"
    )
    try:
        model_v2._validate_model_wave(
            wave,
            instance_id=_wave_key(identity)[0],
            expected_contamination=contamination,
        )
        digest = model_v2._verify_content_address(wave, label="Stage-5 exact wave")
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"invalid Stage-5 exact wave: {error}"
        ) from error
    return wave, digest


def _validate_stage6_block(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    block = deepcopy(dict(_mapping(value, label="Stage-6 block")))
    identity = _mapping(block.get("wave"), label="Stage-6 wave identity")
    try:
        background_v2._validate_block(block, expected_instance_id=_wave_key(identity)[0])
        digest = background_v2._verify_content_address(
            block, label="Stage-6 exact block"
        )
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"invalid Stage-6 exact block: {error}"
        ) from error
    return block, digest


def _accepted_row(
    audit: Mapping[str, Any], *, key: tuple[str, str, int], block_sha: str
) -> Mapping[str, Any]:
    matches = []
    for raw in _array(audit.get("rows"), label="overlap rows"):
        row = _mapping(raw, label="overlap row")
        if _wave_key(_mapping(row.get("key"), label="overlap row key")) == key:
            matches.append(row)
    if len(matches) != 1:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "overlap audit must contain exactly one matching exact-wave row"
        )
    row = matches[0]
    if row.get("classification") not in {"EXACT", "SUBSET"}:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "rejected overlap row cannot produce Warrior-slot inputs"
        )
    source = _mapping(row.get("source_bindings"), label="overlap source bindings")
    if source.get("stage6_block_content_sha256") != block_sha:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "overlap row Stage-6 block content address differs"
        )
    return row


def _target_contract(
    row: Mapping[str, Any], block: Mapping[str, Any]
) -> tuple[list[str], list[dict[str, Any]], dict[str, int]]:
    join = _mapping(row.get("target_join"), label="overlap target join")
    selected = [
        _text(value, label="selected target GUID")
        for value in _array(join.get("capsule_target_guids"), label="selected targets")
    ]
    if not selected or len(set(selected)) != len(selected):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "selected target GUIDs are empty or duplicated"
        )
    registry = [
        _mapping(raw, label="Stage-6 target registry row")
        for raw in _array(block.get("target_registry"), label="Stage-6 target registry")
    ]
    stage6_by_guid = {
        _text(target.get("target_guid"), label="Stage-6 target GUID"): _integer(
            target.get("target_index"), label="Stage-6 target index"
        )
        for target in registry
    }
    if not set(selected) <= set(stage6_by_guid):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "old-50 target pile is not a subset of the Stage-6 block"
        )
    expected_relation = "EQUAL" if set(selected) == set(stage6_by_guid) else "STRICT_SUBSET"
    expected_remap = [
        {
            "target_guid": guid,
            "stage6_target_index": stage6_by_guid[guid],
            "capsule_target_index": index,
        }
        for index, guid in enumerate(selected)
    ]
    if (
        join.get("guid_relation") != expected_relation
        or join.get("index_remap") != expected_remap
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "overlap target relation/remap differs from the exact Stage-6 block"
        )
    return selected, expected_remap, {
        row["target_guid"]: row["capsule_target_index"] for row in expected_remap
    }


def _stage5_unknown_allows_external_exact_fury(spec: Mapping[str, Any]) -> bool:
    """Return whether external exact-GUID evidence may resolve this unknown lane.

    Stage 5 intentionally combines missing and conflicting observations in one
    nonvoting partition.  They are not interchangeable here: a complete exact
    same-instance ranking record may fill a missing/unknown observation, but it
    must not silently overwrite preserved Arms or conflict evidence.
    """

    if (
        spec.get("partition_key") != "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING"
        or spec.get("fury_or_arms_conflict_free_observation") is not False
    ):
        return False
    source = _mapping(
        spec.get("source_evidence"), label="Stage-5 Warrior source evidence"
    )
    conflicts = source.get("field_conflicts")
    if not isinstance(conflicts, Mapping) or conflicts:
        return False
    return spec.get("observed_spec") in {None, "Unknown", "Fury"} and source.get(
        "player_spec"
    ) in {None, "Unknown", "Fury"}


def _slot_label(
    *, spec: Mapping[str, Any], exact_evidence: Mapping[str, Any] | None
) -> str:
    partition = spec.get("partition_key")
    observed = spec.get("observed_spec")
    admitted = spec.get("fury_or_arms_conflict_free_observation") is True
    exact = spec.get("exact_guid_match") is True
    if spec.get("voting_authorized") is not False:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-5 Warrior spec lane unexpectedly authorizes voting"
        )
    if partition == "WARRIOR_FURY":
        if not (observed == "Fury" and admitted and exact):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Stage-5 Fury partition lacks conflict-free exact-GUID evidence"
            )
        return "EXACT_FURY" if exact_evidence is not None else "STAGE5_OBSERVED_FURY"
    if partition == "WARRIOR_ARMS":
        if not (observed == "Arms" and admitted and exact):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Stage-5 Arms partition lacks conflict-free exact-GUID evidence"
            )
        if exact_evidence is not None:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "external EXACT_FURY evidence conflicts with the Stage-5 Arms lane"
            )
        return "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY"
    if partition != "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING" or admitted:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported or falsely admitted unknown Warrior spec lane"
        )
    if exact_evidence is not None and not _stage5_unknown_allows_external_exact_fury(
        spec
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "external EXACT_FURY evidence cannot overwrite Stage-5 Arms or "
            "conflicting spec evidence"
        )
    return (
        "EXACT_FURY"
        if exact_evidence is not None
        else "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY"
    )


def _stage5_roster_evidence_sha(wave: Mapping[str, Any]) -> str:
    """Reconstruct the exact timeline-roster receipt from a Stage-5 wave."""

    players = []
    for raw in _array(wave.get("players"), label="Stage-5 players"):
        player = _mapping(raw, label="Stage-5 player")
        lane = _mapping(player.get("warrior_spec_lane"), label="Stage-5 spec lane")
        players.append(
            {
                "player": deepcopy(player.get("player")),
                "warrior_spec_evidence": deepcopy(lane.get("source_evidence")),
            }
        )
    try:
        return model_v2._roster_evidence_sha({"players": players})
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"could not reconstruct Stage-5 exact roster evidence: {error}"
        ) from error


def _loo_projection(
    *,
    block: Mapping[str, Any],
    focal_guid: str,
    selected_guids: Sequence[str],
    capsule_index_by_guid: Mapping[str, int],
    stage5_loo: Mapping[str, Any],
) -> dict[str, Any]:
    filter_contract = _mapping(
        stage5_loo.get("filter_contract"), label="Stage-5 LOO filter contract"
    )
    if (
        stage5_loo.get("focal_player_guid") != focal_guid
        or filter_contract.get("excluded_attribution_kinds") != list(ATTRIBUTION_KINDS)
        or filter_contract.get("exact_player_guid_match_required") is not True
        or filter_contract.get("name_or_guid_suffix_inference_used") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-5 leave-one-out contract does not bind the exact Warrior GUID"
        )
    selected = set(selected_guids)
    excluded: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    nonpile_count = 0
    nonpile_damage: int | float = 0
    for raw in _array(
        block.get("runtime_candidate_schedule"), label="Stage-6 runtime schedule"
    ):
        event = deepcopy(dict(_mapping(raw, label="Stage-6 runtime event")))
        actor = _guid(event.get("actor_player_guid"), label="runtime actor GUID")
        kind = event.get("attribution_kind")
        if kind not in ATTRIBUTION_KINDS:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Stage-6 runtime event lacks exact direct/owner/controller attribution"
            )
        target_guid = _text(event.get("target_guid"), label="runtime target GUID")
        damage = _number(event.get("damage"), label="runtime damage")
        if actor == focal_guid:
            excluded.append(event)
            continue
        if target_guid not in selected:
            nonpile_count += 1
            nonpile_damage += damage
            continue
        original_index = _integer(
            event.get("target_index"), label="runtime Stage-6 target index"
        )
        projected = deepcopy(event)
        projected["stage6_target_index"] = original_index
        projected["target_index"] = capsule_index_by_guid[target_guid]
        retained.append(projected)
    excluded_by_kind = Counter(str(row["attribution_kind"]) for row in excluded)
    damage_by_kind: Counter[str] = Counter()
    for event in excluded:
        damage_by_kind[str(event["attribution_kind"])] += event["damage"]
    excluded_damage = sum(event["damage"] for event in excluded)
    stage5_count = _integer(
        stage5_loo.get("excluded_focal_event_count"),
        label="Stage-5 excluded focal event count",
    )
    stage5_damage = _number(
        stage5_loo.get("excluded_focal_damage_amount"),
        label="Stage-5 excluded focal damage",
    )
    if len(excluded) > stage5_count or excluded_damage > stage5_damage:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-6 focal runtime events exceed the Stage-5 exact-GUID LOO receipt"
        )
    return {
        "source_schedule": "stage6_exact_block.runtime_candidate_schedule",
        "operation_order": [
            "EXACT_GUID_LEAVE_ONE_OUT",
            "OLD50_TARGET_PILE_PROJECTION",
            "STAGE6_TO_CAPSULE_TARGET_INDEX_REMAP",
        ],
        "exact_guid_leave_one_out": {
            "focal_player_guid": focal_guid,
            "predicate": "actor_player_guid != focal_player_guid",
            "resolved_attribution_kinds_removed": list(ATTRIBUTION_KINDS),
            "excluded_events": excluded,
            "excluded_event_count": len(excluded),
            "excluded_damage": excluded_damage,
            "excluded_event_count_by_attribution": {
                kind: excluded_by_kind.get(kind, 0) for kind in ATTRIBUTION_KINDS
            },
            "excluded_damage_by_attribution": {
                kind: damage_by_kind.get(kind, 0) for kind in ATTRIBUTION_KINDS
            },
            "stage5_all_event_count_upper_bound": stage5_count,
            "stage5_all_attributed_damage_upper_bound": stage5_damage,
            "name_or_guid_suffix_inference_used": False,
        },
        "target_projection": {
            "selected_target_guids": list(selected_guids),
            "unselected_nonfocal_event_count": nonpile_count,
            "unselected_nonfocal_damage": nonpile_damage,
            "unselected_target_events_enter_selected_targets": False,
            "stage6_to_capsule_target_index_remap": [
                {
                    "target_guid": guid,
                    "stage6_target_index": next(
                        int(row["target_index"])
                        for row in block["target_registry"]
                        if row["target_guid"] == guid
                    ),
                    "capsule_target_index": capsule_index_by_guid[guid],
                }
                for guid in selected_guids
            ],
        },
        "projected_schedule": retained,
        "projected_schedule_content_sha256": _sha256(retained),
        "projected_event_count": len(retained),
        "projected_damage": sum(event["damage"] for event in retained),
        "source_eventmeta_order_preserved": True,
        "future_schedule_available_as_policy_feature": False,
    }


def build_warrior_slot_substitution_bundle_v1(
    *,
    overlap_audit: Mapping[str, Any],
    stage5_wave: Mapping[str, Any],
    stage6_block: Mapping[str, Any],
    exact_fury_identity_evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Enumerate exact Warrior GUID slots for one accepted overlap wave."""

    audit, audit_sha = _validate_overlap_audit(overlap_audit)
    wave, stage5_sha = _validate_stage5_wave(stage5_wave)
    block, block_sha = _validate_stage6_block(stage6_block)
    stage5_identity = _mapping(wave.get("wave"), label="Stage-5 identity")
    stage6_identity = _mapping(block.get("wave"), label="Stage-6 identity")
    key = _wave_key(stage5_identity)
    if _wave_key(stage6_identity) != key:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-5 wave and Stage-6 block do not share one exact wave key"
        )
    source_model = _mapping(block.get("source_model"), label="Stage-6 source_model")
    if source_model.get("wave_content_sha256") != stage5_sha:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Stage-6 block is not content-bound to the supplied Stage-5 wave"
        )
    row = _accepted_row(audit, key=key, block_sha=block_sha)
    selected, remap, capsule_index = _target_contract(row, block)
    component = _mapping(row.get("component_join"), label="component join")
    contamination = _mapping(row.get("contamination"), label="overlap contamination")
    try:
        normalized_contamination = background_v2._contamination(wave)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"invalid Stage-5 contamination lane: {error}"
        ) from error
    if (
        component.get("stage6_component_id") != block.get("component_id")
        or component.get("split_authority")
        not in {"EQUIVALENT_ON_OVERLAP", "STAGE6_COARSER_COMPONENT"}
        or contamination.get("exact_stage5_wave_normalized_lane_equal") is not True
        or dict(_mapping(block.get("contamination_lane"), label="block contamination"))
        != normalized_contamination
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "component or contamination authority differs from the accepted overlap row"
        )

    evidence_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in exact_fury_identity_evidence:
        evidence = validate_exact_fury_identity_evidence_v1(raw)
        identity = _mapping(evidence.get("identity"), label="evidence identity")
        evidence_key = (
            _text(identity.get("instance_id"), label="evidence instance_id"),
            _guid(identity.get("character_guid"), label="evidence character_guid"),
        )
        if evidence_key in evidence_by_key:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "duplicate exact Fury identity evidence for instance/GUID"
            )
        evidence_by_key[evidence_key] = evidence

    roster = set(
        _guid(value, label="Stage-6 roster GUID")
        for value in _array(block.get("roster_player_guids"), label="Stage-6 roster")
    )
    slot_inputs: list[dict[str, Any]] = []
    seen_guids: set[str] = set()
    for raw_player in _array(wave.get("players"), label="Stage-5 players"):
        player = deepcopy(dict(_mapping(raw_player, label="Stage-5 player")))
        try:
            player_sha = model_v2._verify_content_address(
                player, label="Stage-5 player record"
            )
        except Exception as error:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                f"invalid Stage-5 player content address: {error}"
            ) from error
        metadata = _mapping(player.get("player"), label="Stage-5 player metadata")
        if str(metadata.get("class") or "").casefold() != "warrior":
            continue
        guid = _guid(metadata.get("guid"), label="Warrior GUID")
        if guid in seen_guids or guid not in roster:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Warrior GUID is duplicated or absent from the exact Stage-6 roster"
            )
        seen_guids.add(guid)
        external = evidence_by_key.get((key[0], guid))
        if external is not None:
            closure = _mapping(
                _mapping(external.get("source_refs"), label="exact Fury source refs").get(
                    "offline_wave_closure"
                ),
                label="exact Fury offline wave closure",
            )
            audit_inputs = _mapping(audit.get("input_bindings"), label="audit inputs")
            expected_closure = {
                "stage5_manifest_content_sha256": _mapping(
                    audit_inputs.get("stage5_manifest"), label="audit Stage-5 binding"
                ).get("content_sha256"),
                "stage6_manifest_content_sha256": _mapping(
                    audit_inputs.get("stage6_manifest"), label="audit Stage-6 binding"
                ).get("content_sha256"),
                "old50_capsule_content_sha256": _mapping(
                    audit_inputs.get("old50_capsule"), label="audit capsule binding"
                ).get("content_sha256"),
            }
            accepted_for_instance = sum(
                candidate.get("classification") in {"EXACT", "SUBSET"}
                and _mapping(candidate.get("key"), label="audit row key").get(
                    "instance_id"
                )
                == key[0]
                for candidate in _array(audit.get("rows"), label="overlap rows")
            )
            evidence_selection = _mapping(
                external.get("selection"), label="exact Fury selection"
            )
            generic_roster_mismatch = (
                evidence_selection.get("selection_lane")
                == "GENERIC_EXACT_FURY_FULL_SCOPE"
                and closure.get("exact_roster_evidence_sha256")
                != _stage5_roster_evidence_sha(wave)
            )
            if any(
                closure.get(field) != expected
                for field, expected in expected_closure.items()
            ) or evidence_selection.get(
                "accepted_wave_count"
            ) != accepted_for_instance or generic_roster_mismatch:
                raise ChronicleOld50WarriorSlotSubstitutionError(
                    "EXACT_FURY evidence differs from the overlap manifest/wave closure"
                )
        spec = deepcopy(
            dict(_mapping(player.get("warrior_spec_lane"), label="Warrior spec lane"))
        )
        label = _slot_label(spec=spec, exact_evidence=external)
        stage5_loo = _mapping(
            player.get("leave_one_player_out_background"), label="Stage-5 player LOO"
        )
        projection = _loo_projection(
            block=block,
            focal_guid=guid,
            selected_guids=selected,
            capsule_index_by_guid=capsule_index,
            stage5_loo=stage5_loo,
        )
        exact_fury = label == "EXACT_FURY"
        spec_relationship = (
            "CORROBORATES_EXTERNAL_EXACT_FURY"
            if exact_fury and spec.get("partition_key") == "WARRIOR_FURY"
            else "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_EXACT_FURY"
            if exact_fury
            else "STAGE5_OBSERVED_FURY_NO_EXTERNAL_EXACT_IDENTITY"
            if label == "STAGE5_OBSERVED_FURY"
            else "STAGE5_OBSERVED_ARMS_ENVIRONMENT_ONLY"
            if label == "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY"
            else "STAGE5_UNKNOWN_OR_CONFLICTING_ENVIRONMENT_ONLY"
        )
        environment_only = label in {
            "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY",
            "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY",
        }
        row_sha = _sha256(row)
        source_bindings = {
            "overlap_audit_content_sha256": audit_sha,
            "overlap_row_sha256": row_sha,
            "old50_capsule": deepcopy(
                _mapping(
                    _mapping(audit.get("input_bindings"), label="audit inputs").get(
                        "old50_capsule"
                    ),
                    label="old50 capsule binding",
                )
            ),
            "stage5_manifest": deepcopy(
                _mapping(audit["input_bindings"].get("stage5_manifest"), label="Stage-5 binding")
            ),
            "stage5_exact_wave_content_sha256": stage5_sha,
            "stage5_player_record_content_sha256": player_sha,
            "stage6_manifest": deepcopy(
                _mapping(audit["input_bindings"].get("stage6_manifest"), label="Stage-6 binding")
            ),
            "stage6_exact_block_content_sha256": block_sha,
            "exact_fury_identity_evidence_content_sha256": (
                external["content_address"]["sha256"] if external is not None else None
            ),
        }
        core = {
            "schema": SCHEMA,
            "revision": REVISION,
            "status": STATUS,
            "identity": {
                "instance_id": key[0],
                "encounter_id": key[1],
                "wave_ordinal": key[2],
                "scenario_id": row.get("scenario_id"),
                "slot_player_guid": guid,
            },
            "same_wave_identity": {
                "capsule_wave_id": row["source_bindings"]["capsule_wave_id"],
                "stage5_wave_id": stage5_identity.get("wave_id"),
                "stage6_wave_id": stage6_identity.get("wave_id"),
                "same_exact_instance_encounter_wave_ordinal": True,
                "ordinal_only_join_allowed": False,
            },
            "source_bindings": source_bindings,
            "slot_evidence": {
                "class": "Warrior",
                "slot_label": label,
                "selection_priority": _PRIORITY[label],
                "stage5_spec_lane": spec,
                "stage5_spec_relationship_to_slot_label": spec_relationship,
                "exact_fury_identity_evidence": deepcopy(external),
                "historical_fury_identity_tier": (
                    (
                        "PDF_QUERY_IDENTITY_HISTORY_RANKING_BOUND_EXACT_FURY"
                        if external["selection"]["selection_lane"]
                        == "PDF_INTENDED_FURY_EXACT_GUID"
                        else "STAGE5_ROSTER_FULL_SCOPE_RANKING_BOUND_EXACT_FURY"
                    )
                    if exact_fury and external is not None
                    else "STAGE5_EXACT_GUID_OBSERVED_FURY_ONLY"
                    if label == "STAGE5_OBSERVED_FURY"
                    else "NOT_HISTORICAL_FURY"
                ),
                "environment_substitution_eligible": True,
                "environment_substitution_only": environment_only,
                "future_historical_fury_adapter_eligible": exact_fury,
                "historical_fury_voting_eligible": False,
                "historical_fury_comparison_eligible": False,
                "board_player_name_or_rank_used_as_identity": False,
            },
            "join": {
                "classification": row["classification"],
                "target_guid_relation": row["target_join"]["guid_relation"],
                "selected_target_guids": selected,
                "target_index_remap": remap,
                "component_relation": deepcopy(component),
                "contamination": deepcopy(contamination),
            },
            "causal_schedule_projection": projection,
            "scientific_boundary": {
                "diagnostic_nonvoting": True,
                "historical_truth": False,
                "comparison_ready": False,
                "formal_runner_registered": False,
                "deployment_ready": False,
                "future_team_schedule_visible_to_policy": False,
            },
        }
        value = _content_addressed(core)
        validate_warrior_slot_substitution_input_v1(value)
        slot_inputs.append(value)
    if not slot_inputs:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "accepted wave contains no exact class=Warrior slot"
        )
    slot_inputs.sort(
        key=lambda value: (
            value["slot_evidence"]["selection_priority"],
            value["identity"]["slot_player_guid"],
        )
    )
    common = {
        "overlap_audit_content_sha256": audit_sha,
        "overlap_row_sha256": _sha256(row),
        "stage5_exact_wave_content_sha256": stage5_sha,
        "stage6_exact_block_content_sha256": block_sha,
    }
    core = {
        "schema": BUNDLE_SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "wave_identity": {
            "instance_id": key[0],
            "encounter_id": key[1],
            "wave_ordinal": key[2],
            "scenario_id": row.get("scenario_id"),
        },
        "source_bindings": common,
        "summary": {
            "warrior_slot_count": len(slot_inputs),
            "slot_label_counts": {
                label: sum(
                    value["slot_evidence"]["slot_label"] == label
                    for value in slot_inputs
                )
                for label in SLOT_LABELS
            },
            "exact_fury_identity_evidence_count": sum(
                value["slot_evidence"]["slot_label"] == "EXACT_FURY"
                for value in slot_inputs
            ),
            "environment_substitution_only_count": sum(
                value["slot_evidence"]["environment_substitution_only"] is True
                for value in slot_inputs
            ),
        },
        "inputs": slot_inputs,
        "execution_boundary": {
            "network_requests_made": 0,
            "heavy_jobs_started": False,
            "simulator_runs_started": False,
            "deployment_started": False,
        },
        "scientific_boundary": {
            "diagnostic_nonvoting": True,
            "comparison_ready": False,
            "historical_fury_comparison_started": False,
            "deployment_ready": False,
        },
    }
    bundle = _content_addressed(core)
    validate_warrior_slot_substitution_bundle_v1(bundle)
    return bundle


def validate_warrior_slot_substitution_input_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, label="Warrior-slot input")))
    if (
        raw.get("schema") != SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported Warrior-slot substitution input"
        )
    _verify_content_address(raw, label="Warrior-slot input")
    identity = _mapping(raw.get("identity"), label="slot identity")
    guid = _guid(identity.get("slot_player_guid"), label="slot player GUID")
    _text(identity.get("instance_id"), label="slot instance_id")
    encounter_id = _text(identity.get("encounter_id"), label="slot encounter_id")
    wave_ordinal = _integer(identity.get("wave_ordinal"), label="slot wave_ordinal")
    _text(identity.get("scenario_id"), label="slot scenario_id")
    same_wave = _mapping(raw.get("same_wave_identity"), label="same-wave identity")
    capsule_wave_id = _text(
        same_wave.get("capsule_wave_id"), label="capsule wave_id"
    )
    stage5_wave_id = _text(
        same_wave.get("stage5_wave_id"), label="Stage-5 wave_id"
    )
    stage6_wave_id = _text(
        same_wave.get("stage6_wave_id"), label="Stage-6 wave_id"
    )
    if (
        capsule_wave_id != f"{encounter_id}:wave:{wave_ordinal}"
        or stage5_wave_id != f"{encounter_id}:external-v2-wave:{wave_ordinal}"
        or stage6_wave_id != stage5_wave_id
        or same_wave.get("same_exact_instance_encounter_wave_ordinal") is not True
        or same_wave.get("ordinal_only_join_allowed") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot input widened the exact-wave join"
        )
    bindings = _mapping(raw.get("source_bindings"), label="slot source bindings")
    for field in (
        "overlap_audit_content_sha256",
        "overlap_row_sha256",
        "stage5_exact_wave_content_sha256",
        "stage5_player_record_content_sha256",
        "stage6_exact_block_content_sha256",
    ):
        _sha(bindings.get(field), label=field)
    for field in ("old50_capsule", "stage5_manifest", "stage6_manifest"):
        bound = _mapping(bindings.get(field), label=f"{field} binding")
        _text(bound.get("locator"), label=f"{field} locator")
        _sha(bound.get("content_sha256"), label=f"{field} content SHA-256")
        _sha(bound.get("file_sha256"), label=f"{field} file SHA-256")
    evidence_sha = bindings.get("exact_fury_identity_evidence_content_sha256")
    if evidence_sha is not None:
        _sha(evidence_sha, label="exact Fury evidence content SHA-256")
    evidence = _mapping(raw.get("slot_evidence"), label="slot evidence")
    label = evidence.get("slot_label")
    if label not in SLOT_LABELS or evidence.get("selection_priority") != _PRIORITY[label]:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior slot label/priority is invalid"
        )
    if evidence.get("class") != "Warrior" or evidence.get(
        "historical_fury_voting_eligible"
    ) is not False or evidence.get("historical_fury_comparison_eligible") is not False:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior slot class or nonvoting boundary differs"
        )
    spec = _mapping(evidence.get("stage5_spec_lane"), label="Stage-5 slot spec lane")
    partition = spec.get("partition_key")
    fury_lane_valid = (
        partition == "WARRIOR_FURY"
        and spec.get("observed_spec") == "Fury"
        and spec.get("exact_guid_match") is True
        and spec.get("fury_or_arms_conflict_free_observation") is True
    )
    arms_lane_valid = (
        partition == "WARRIOR_ARMS"
        and spec.get("observed_spec") == "Arms"
        and spec.get("exact_guid_match") is True
        and spec.get("fury_or_arms_conflict_free_observation") is True
    )
    unknown_lane_valid = (
        partition == "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING"
        and spec.get("fury_or_arms_conflict_free_observation") is False
    )
    externally_resolvable_unknown_lane = (
        unknown_lane_valid and _stage5_unknown_allows_external_exact_fury(spec)
    )
    if (
        spec.get("voting_authorized") is not False
        or (
            label == "EXACT_FURY"
            and not (fury_lane_valid or externally_resolvable_unknown_lane)
        )
        or (label == "STAGE5_OBSERVED_FURY" and not fury_lane_valid)
        or (label == "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY" and not arms_lane_valid)
        or (label == "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY" and not unknown_lane_valid)
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior slot label differs from its Stage-5 spec lane"
        )
    expected_relationship = (
        "CORROBORATES_EXTERNAL_EXACT_FURY"
        if label == "EXACT_FURY" and fury_lane_valid
        else "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_EXACT_FURY"
        if label == "EXACT_FURY"
        else "STAGE5_OBSERVED_FURY_NO_EXTERNAL_EXACT_IDENTITY"
        if label == "STAGE5_OBSERVED_FURY"
        else "STAGE5_OBSERVED_ARMS_ENVIRONMENT_ONLY"
        if label == "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY"
        else "STAGE5_UNKNOWN_OR_CONFLICTING_ENVIRONMENT_ONLY"
    )
    if evidence.get("stage5_spec_relationship_to_slot_label") != expected_relationship:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior slot did not preserve its Stage-5/external spec relationship"
        )
    exact_evidence = evidence.get("exact_fury_identity_evidence")
    if label == "EXACT_FURY":
        exact = validate_exact_fury_identity_evidence_v1(
            _mapping(exact_evidence, label="slot exact Fury evidence")
        )
        exact_closure = _mapping(
            _mapping(exact.get("source_refs"), label="exact Fury source refs").get(
                "offline_wave_closure"
            ),
            label="exact Fury offline wave closure",
        )
        if (
            exact["identity"]["instance_id"] != identity["instance_id"]
            or exact["identity"]["character_guid"] != guid
            or exact["content_address"]["sha256"] != evidence_sha
            or exact_closure.get("stage5_manifest_content_sha256")
            != bindings["stage5_manifest"]["content_sha256"]
            or exact_closure.get("stage6_manifest_content_sha256")
            != bindings["stage6_manifest"]["content_sha256"]
            or exact_closure.get("old50_capsule_content_sha256")
            != bindings["old50_capsule"]["content_sha256"]
            or evidence.get("future_historical_fury_adapter_eligible") is not True
            or evidence.get("environment_substitution_only") is not False
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "EXACT_FURY slot does not match its identity-evidence closure"
            )
    elif (
        exact_evidence is not None
        or evidence_sha is not None
        or evidence.get("future_historical_fury_adapter_eligible") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "non-EXACT_FURY slot carries external exact-Fury authority"
        )
    environment_only = label in {
        "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY",
        "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY",
    }
    if (
        evidence.get("environment_substitution_eligible") is not True
        or evidence.get("environment_substitution_only") is not environment_only
        or evidence.get("board_player_name_or_rank_used_as_identity") is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior environment-substitution boundary differs"
        )
    if label == "EXACT_FURY":
        exact_lane = exact["selection"]["selection_lane"]
        expected_tier = (
            "PDF_QUERY_IDENTITY_HISTORY_RANKING_BOUND_EXACT_FURY"
            if exact_lane == "PDF_INTENDED_FURY_EXACT_GUID"
            else "STAGE5_ROSTER_FULL_SCOPE_RANKING_BOUND_EXACT_FURY"
        )
    elif label == "STAGE5_OBSERVED_FURY":
        expected_tier = "STAGE5_EXACT_GUID_OBSERVED_FURY_ONLY"
    else:
        expected_tier = "NOT_HISTORICAL_FURY"
    if evidence.get("historical_fury_identity_tier") != expected_tier:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "slot historical Fury identity tier differs from its evidence"
        )
    join = _mapping(raw.get("join"), label="slot join")
    classification = join.get("classification")
    if classification not in {"EXACT", "SUBSET"}:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot input carries a rejected overlap row"
        )
    selected = [
        _text(value, label="slot selected target GUID")
        for value in _array(
            join.get("selected_target_guids"), label="slot selected targets"
        )
    ]
    remap = [
        _mapping(value, label="slot target remap row")
        for value in _array(join.get("target_index_remap"), label="slot target remap")
    ]
    for row in remap:
        _text(row.get("target_guid"), label="remap target GUID")
        _integer(row.get("stage6_target_index"), label="remap Stage-6 target index")
        _integer(row.get("capsule_target_index"), label="remap capsule target index")
    expected_target_relation = "EQUAL" if classification == "EXACT" else "STRICT_SUBSET"
    if (
        not selected
        or len(set(selected)) != len(selected)
        or len(remap) != len(selected)
        or join.get("target_guid_relation") != expected_target_relation
        or [row.get("target_guid") for row in remap] != selected
        or [row.get("capsule_target_index") for row in remap]
        != list(range(len(selected)))
        or len({row.get("stage6_target_index") for row in remap}) != len(remap)
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot target projection is incomplete"
        )
    component = _mapping(
        join.get("component_relation"), label="slot component relation"
    )
    split_authority = component.get("split_authority")
    expected_component_relation = (
        "OVERLAP_PROJECTION_EXACT"
        if split_authority == "EQUIVALENT_ON_OVERLAP"
        else "OLD50_PROJECTION_STRICT_SUBSET_STAGE6_AUTHORITY"
        if split_authority == "STAGE6_COARSER_COMPONENT"
        else None
    )
    if (
        expected_component_relation is None
        or component.get("relation") != expected_component_relation
        or not isinstance(component.get("old50_component_id"), str)
        or not component.get("old50_component_id")
        or not isinstance(component.get("stage6_component_id"), str)
        or not component.get("stage6_component_id")
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot component split authority differs"
        )
    contamination = _mapping(
        join.get("contamination"), label="slot contamination authority"
    )
    if (
        contamination.get("exact_stage5_wave_normalized_lane_equal") is not True
        or contamination.get("authority")
        != "EXACT_STAGE5_WAVE_BOUND_COHORT_RECEIPT_VIA_STAGE6"
        or contamination.get("raw_instance_lane_compared_directly") is not False
        or contamination.get("old50_capsule_field") != "ABSENT"
        or contamination.get("player_name_used") is not False
        or not isinstance(contamination.get("stage6_training_candidate"), bool)
        or not isinstance(contamination.get("stage6_label"), str)
        or not contamination.get("stage6_label")
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot contamination authority differs"
        )
    projection = _mapping(
        raw.get("causal_schedule_projection"), label="causal schedule projection"
    )
    if projection.get("operation_order") != [
        "EXACT_GUID_LEAVE_ONE_OUT",
        "OLD50_TARGET_PILE_PROJECTION",
        "STAGE6_TO_CAPSULE_TARGET_INDEX_REMAP",
    ] or projection.get("future_schedule_available_as_policy_feature") is not False:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot causal projection order/boundary differs"
        )
    loo = _mapping(projection.get("exact_guid_leave_one_out"), label="slot LOO")
    excluded = [
        _mapping(row, label="excluded focal event")
        for row in _array(loo.get("excluded_events"), label="excluded focal events")
    ]
    if (
        loo.get("focal_player_guid") != guid
        or loo.get("resolved_attribution_kinds_removed") != list(ATTRIBUTION_KINDS)
        or loo.get("name_or_guid_suffix_inference_used") is not False
        or any(row.get("actor_player_guid") != guid for row in excluded)
        or any(row.get("attribution_kind") not in ATTRIBUTION_KINDS for row in excluded)
        or loo.get("excluded_event_count") != len(excluded)
        or loo.get("excluded_damage") != sum(row.get("damage", 0) for row in excluded)
        or any(
            not isinstance(row.get("damage"), (int, float))
            or isinstance(row.get("damage"), bool)
            or row.get("damage") < 0
            for row in excluded
        )
        or loo.get("stage5_all_event_count_upper_bound", -1) < len(excluded)
        or loo.get("stage5_all_attributed_damage_upper_bound", -1)
        < sum(row.get("damage", 0) for row in excluded)
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "exact direct/owner/controller leave-one-out accounting differs"
        )
    count_by_kind = Counter(str(row["attribution_kind"]) for row in excluded)
    damage_by_kind: Counter[str] = Counter()
    for row in excluded:
        damage_by_kind[str(row["attribution_kind"])] += row["damage"]
    if loo.get("excluded_event_count_by_attribution") != {
        kind: count_by_kind.get(kind, 0) for kind in ATTRIBUTION_KINDS
    } or loo.get("excluded_damage_by_attribution") != {
        kind: damage_by_kind.get(kind, 0) for kind in ATTRIBUTION_KINDS
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "leave-one-out attribution accounting differs"
        )
    schedule = [
        _mapping(row, label="projected schedule event")
        for row in _array(projection.get("projected_schedule"), label="projected schedule")
    ]
    selected_set = set(selected)
    remap_by_guid = {
        row["target_guid"]: row for row in remap if isinstance(row, Mapping)
    }
    prior: tuple[int, ...] | None = None
    for event in schedule:
        if event.get("actor_player_guid") == guid:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "focal event survived the projected schedule"
            )
        target_guid = event.get("target_guid")
        mapping = remap_by_guid.get(target_guid)
        if (
            target_guid not in selected_set
            or mapping is None
            or event.get("target_index") != mapping.get("capsule_target_index")
            or event.get("stage6_target_index") != mapping.get("stage6_target_index")
            or event.get("attribution_kind") not in ATTRIBUTION_KINDS
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "projected schedule target remap or attribution differs"
            )
        order = tuple(
            _integer(value, label="source EventMeta order")
            for value in _array(
                event.get("source_eventmeta_order_key"), label="source EventMeta order"
            )
        )
        if prior is not None and order <= prior:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "projected schedule is not in strict source EventMeta order"
            )
        prior = order
    target_projection = _mapping(
        projection.get("target_projection"), label="target projection"
    )
    if (
        target_projection.get("selected_target_guids") != selected
        or target_projection.get("stage6_to_capsule_target_index_remap") != remap
        or target_projection.get("unselected_target_events_enter_selected_targets")
        is not False
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "projected schedule target contract differs from the overlap join"
        )
    if (
        projection.get("projected_schedule_content_sha256") != _sha256(schedule)
        or projection.get("projected_event_count") != len(schedule)
        or projection.get("projected_damage")
        != sum(row.get("damage", 0) for row in schedule)
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "projected schedule content/accounting differs"
        )
    if _mapping(raw.get("scientific_boundary"), label="slot scientific boundary") != {
        "diagnostic_nonvoting": True,
        "historical_truth": False,
        "comparison_ready": False,
        "formal_runner_registered": False,
        "deployment_ready": False,
        "future_team_schedule_visible_to_policy": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot scientific boundary widened"
        )
    return raw


def validate_warrior_slot_substitution_bundle_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(_mapping(value, label="Warrior-slot bundle")))
    if (
        raw.get("schema") != BUNDLE_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "unsupported Warrior-slot bundle"
        )
    _verify_content_address(raw, label="Warrior-slot bundle")
    inputs = [
        validate_warrior_slot_substitution_input_v1(
            _mapping(row, label="Warrior-slot bundle input")
        )
        for row in _array(raw.get("inputs"), label="Warrior-slot inputs")
    ]
    if not inputs:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot bundle is empty"
        )
    expected_order = sorted(
        inputs,
        key=lambda row: (
            row["slot_evidence"]["selection_priority"],
            row["identity"]["slot_player_guid"],
        ),
    )
    guids = [row["identity"]["slot_player_guid"] for row in inputs]
    if inputs != expected_order or len(set(guids)) != len(guids):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot bundle order/GUID uniqueness differs"
        )
    summary = _mapping(raw.get("summary"), label="Warrior-slot summary")
    counts = Counter(row["slot_evidence"]["slot_label"] for row in inputs)
    if (
        summary.get("warrior_slot_count") != len(inputs)
        or summary.get("slot_label_counts")
        != {label: counts.get(label, 0) for label in SLOT_LABELS}
        or summary.get("exact_fury_identity_evidence_count")
        != counts.get("EXACT_FURY", 0)
        or summary.get("environment_substitution_only_count")
        != sum(
            row["slot_evidence"]["environment_substitution_only"] is True
            for row in inputs
        )
    ):
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot bundle summary differs from its inputs"
        )
    wave_identity = _mapping(raw.get("wave_identity"), label="bundle wave identity")
    common = _mapping(raw.get("source_bindings"), label="bundle source bindings")
    for row in inputs:
        identity = row["identity"]
        if any(
            identity[field] != wave_identity[field]
            for field in ("instance_id", "encounter_id", "wave_ordinal", "scenario_id")
        ) or any(
            row["source_bindings"][field] != common[field]
            for field in (
                "overlap_audit_content_sha256",
                "overlap_row_sha256",
                "stage5_exact_wave_content_sha256",
                "stage6_exact_block_content_sha256",
            )
        ):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "Warrior-slot bundle mixes waves or source artifacts"
            )
    if _mapping(raw.get("execution_boundary"), label="bundle execution boundary") != {
        "network_requests_made": 0,
        "heavy_jobs_started": False,
        "simulator_runs_started": False,
        "deployment_started": False,
    } or _mapping(raw.get("scientific_boundary"), label="bundle scientific boundary") != {
        "diagnostic_nonvoting": True,
        "comparison_ready": False,
        "historical_fury_comparison_started": False,
        "deployment_ready": False,
    }:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            "Warrior-slot bundle execution/scientific boundary widened"
        )
    return raw


def materialize_exact_fury_selector_receipt_v1(
    *,
    ranking_snapshot_manifest_path: str | Path,
    ranking_capture_receipt_path: str | Path,
    ranking_raw_root: str | Path,
    stage5_manifest_path: str | Path,
    overlap_audit_path: str | Path,
    leaderboard_manifest_path: str | Path,
    fury_pdf_path: str | Path,
    character_history_manifest_path: str | Path,
    character_inventory_manifest_path: str | Path,
    exact_dps_index_manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Materialize the formal selector receipt entirely from local files."""

    snapshot, snapshot_file_sha = _read_json_document(
        ranking_snapshot_manifest_path, label="ranking snapshot manifest"
    )
    capture, capture_file_sha = _read_json_document(
        ranking_capture_receipt_path, label="ranking capture receipt"
    )
    stage5, stage5_file_sha = _read_json_document(
        stage5_manifest_path, label="Stage-5 manifest"
    )
    audit, audit_file_sha = _read_json_document(
        overlap_audit_path, label="overlap audit"
    )
    leaderboard, leaderboard_file_sha = _read_json_document(
        leaderboard_manifest_path, label="leaderboard manifest"
    )
    history, history_file_sha = _read_json_document(
        character_history_manifest_path, label="character history manifest"
    )
    inventory, inventory_file_sha = _read_json_document(
        character_inventory_manifest_path, label="character inventory manifest"
    )
    dps_index, dps_index_file_sha = _read_json_document(
        exact_dps_index_manifest_path, label="exact DPS index manifest"
    )
    try:
        fury_pdf_file_sha = hashlib.sha256(
            Path(fury_pdf_path).expanduser().resolve().read_bytes()
        ).hexdigest()
    except OSError as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"could not read Fury PDF: {error}"
        ) from error
    raw_root = Path(ranking_raw_root).expanduser().resolve()

    def load_ranking_response(
        entry: Mapping[str, Any],
    ) -> tuple[Sequence[Mapping[str, Any]], str]:
        response = _mapping(entry.get("response"), label="ranking response")
        object_ref = _mapping(response.get("object"), label="ranking object")
        relative = _text(
            object_ref.get("relative_path"), label="ranking object relative path"
        )
        path = (raw_root / Path(relative)).resolve()
        try:
            path.relative_to(raw_root)
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ChronicleOld50WarriorSlotSubstitutionError(
                f"could not load ranking raw object {relative}: {error}"
            ) from error
        if not isinstance(value, list):
            raise ChronicleOld50WarriorSlotSubstitutionError(
                "ranking raw object must contain a JSON array"
            )
        return value, hashlib.sha256(payload).hexdigest()

    selector = build_exact_fury_selector_receipt_v1(
        ranking_snapshot_manifest=snapshot,
        ranking_snapshot_manifest_file_sha256=snapshot_file_sha,
        ranking_capture_receipt=capture,
        ranking_capture_receipt_file_sha256=capture_file_sha,
        ranking_response_loader=load_ranking_response,
        stage5_manifest=stage5,
        stage5_manifest_file_sha256=stage5_file_sha,
        overlap_audit=audit,
        overlap_audit_file_sha256=audit_file_sha,
        leaderboard_manifest=leaderboard,
        leaderboard_manifest_file_sha256=leaderboard_file_sha,
        fury_pdf_file_sha256=fury_pdf_file_sha,
        character_history_manifest=history,
        character_history_manifest_file_sha256=history_file_sha,
        character_inventory_manifest=inventory,
        character_inventory_manifest_file_sha256=inventory_file_sha,
        exact_dps_index_manifest=dps_index,
        exact_dps_index_manifest_file_sha256=dps_index_file_sha,
    )
    output = Path(output_path).expanduser().resolve()
    payload = _canonical(selector) + b"\n"
    addressed_output = output.with_name(
        f"{output.stem}.{selector['content_address']['sha256']}{output.suffix}"
    )
    try:
        addressed_outcome = overlap_v1._write_once(addressed_output, payload)
        stable_outcome = overlap_v1._write_once(output, payload)
    except Exception as error:
        raise ChronicleOld50WarriorSlotSubstitutionError(
            f"could not publish selector receipt: {error}"
        ) from error
    return {
        "status": (
            "PUBLISHED"
            if "PUBLISHED" in {addressed_outcome, stable_outcome}
            else "RESUMED"
        ),
        "output_path": str(output),
        "addressed_output_path": str(addressed_output),
        "stable_output_status": stable_outcome,
        "addressed_output_status": addressed_outcome,
        "content_sha256": selector["content_address"]["sha256"],
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "instance_count": selector["summary"]["instance_count"],
        "accepted_wave_count": selector["summary"]["accepted_wave_count"],
        "selection_lane_counts": selector["summary"]["selection_lane_counts"],
        "pdf_candidate_count": selector["summary"]["pdf_candidate_count"],
        "network_requests_made": 0,
        "heavy_jobs_started": False,
        "comparison_started": False,
        "deployment_started": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_old50_warrior_slot_substitution_v1",
        description="Materialize the formal old50 exact-Fury selector receipt",
    )
    parser.add_argument("--ranking-snapshot-manifest", type=Path, required=True)
    parser.add_argument("--ranking-capture-receipt", type=Path, required=True)
    parser.add_argument("--ranking-raw-root", type=Path, required=True)
    parser.add_argument("--stage5-manifest", type=Path, required=True)
    parser.add_argument("--overlap-audit", type=Path, required=True)
    parser.add_argument("--leaderboard-manifest", type=Path, required=True)
    parser.add_argument("--fury-pdf", type=Path, required=True)
    parser.add_argument("--character-history-manifest", type=Path, required=True)
    parser.add_argument("--character-inventory-manifest", type=Path, required=True)
    parser.add_argument("--exact-dps-index-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = materialize_exact_fury_selector_receipt_v1(
        ranking_snapshot_manifest_path=args.ranking_snapshot_manifest,
        ranking_capture_receipt_path=args.ranking_capture_receipt,
        ranking_raw_root=args.ranking_raw_root,
        stage5_manifest_path=args.stage5_manifest,
        overlap_audit_path=args.overlap_audit,
        leaderboard_manifest_path=args.leaderboard_manifest,
        fury_pdf_path=args.fury_pdf,
        character_history_manifest_path=args.character_history_manifest,
        character_inventory_manifest_path=args.character_inventory_manifest,
        exact_dps_index_manifest_path=args.exact_dps_index_manifest,
        output_path=args.output,
    )
    (stdout or __import__("sys").stdout).write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "ATTRIBUTION_KINDS",
    "BUNDLE_SCHEMA",
    "ChronicleOld50WarriorSlotSubstitutionError",
    "EVIDENCE_STATUS",
    "EXACT_FURY_EVIDENCE_SCHEMA",
    "EXACT_FURY_SELECTION_LANES",
    "SELECTOR_RECEIPT_SCHEMA",
    "SELECTOR_RECEIPT_STATUS",
    "REVISION",
    "SCHEMA",
    "SLOT_LABELS",
    "STATUS",
    "build_exact_fury_identity_evidence_v1",
    "build_exact_fury_selector_receipt_v1",
    "build_parser",
    "build_warrior_slot_substitution_bundle_v1",
    "main",
    "materialize_exact_fury_selector_receipt_v1",
    "validate_exact_fury_identity_evidence_v1",
    "validate_exact_fury_selector_receipt_v1",
    "validate_warrior_slot_substitution_bundle_v1",
    "validate_warrior_slot_substitution_input_v1",
)
