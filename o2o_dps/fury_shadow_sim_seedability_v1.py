"""Fail-closed seedability audit for accepted Fury Shadow transitions.

This module does not call WoWSims and does not mutate the O2O bridge.  It
answers a narrower question: whether each accepted live transition carries a
complete, capture-bound state that the *current* bridge could restore exactly.

The distinction is intentional.  A partial observation can be useful for a
development-only synthetic microstate, but it is not a simulator checkpoint,
does not create a counterfactual reward, and is never training- or
deployment-eligible here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSITIONS = (
    PROJECT_ROOT
    / "offline_data"
    / "online_training"
    / "fury_shadow_transitions_v1.jsonl"
)
DEFAULT_TRANSITION_MANIFEST = DEFAULT_TRANSITIONS.with_suffix(".manifest.json")
DEFAULT_CAT2_CONFORMANCE_REPORT = (
    PROJECT_ROOT
    / "offline_data"
    / "reports"
    / "fury_cat2_profile_conformance_v1.json"
)
DEFAULT_CAT2_CONFORMANCE_ROWS = DEFAULT_CAT2_CONFORMANCE_REPORT.with_name(
    "fury_cat2_profile_conformance_v1.rows.jsonl"
)
DEFAULT_REPORT = (
    PROJECT_ROOT
    / "offline_data"
    / "reports"
    / "fury_shadow_sim_seedability_v1.json"
)
DEFAULT_ROWS = DEFAULT_REPORT.with_name("fury_shadow_sim_seedability_v1.rows.jsonl")

REPORT_SCHEMA = "fury_shadow_sim_seedability_report/v1"
ROW_SCHEMA = "fury_shadow_sim_seedability_row/v1"
TRANSITION_SCHEMA = "fury_shadow_transition_fragment/v1"
TRANSITION_MANIFEST_SCHEMA = "fury_shadow_transition_dataset_manifest/v1"
CAT2_REPORT_SCHEMA = "fury_cat2_profile_conformance_report/v1"
CAT2_ROW_SCHEMA = "fury_cat2_profile_conformance_row/v1"

CLASS_EXACT = "EXACT"
CLASS_APPROX_ONLY = "APPROX_ONLY"
CLASS_REJECT = "REJECT"
CLASSIFICATIONS = (CLASS_EXACT, CLASS_APPROX_ONLY, CLASS_REJECT)
LANES = ("off_gcd", "queue", "gcd")
SUPPORTED_ACTIONS = frozenset(
    {
        "warrior_bloodrage",
        "warrior_bloodthirst",
        "warrior_whirlwind",
        "warrior_heroic_strike",
        "warrior_cleave",
        "warrior_execute",
        "warrior_o2o_cancel_queue",
    }
)
ACTION_ALLOWED_LANES = {
    "warrior_bloodrage": frozenset({"off_gcd"}),
    "warrior_bloodthirst": frozenset({"gcd"}),
    "warrior_whirlwind": frozenset({"gcd"}),
    "warrior_execute": frozenset({"gcd"}),
    "warrior_heroic_strike": frozenset({"queue"}),
    "warrior_cleave": frozenset({"queue"}),
    # The factorized policy contract models cancellation as an immediate
    # control operation in the off-GCD lane, not as a queued replacement.
    "warrior_o2o_cancel_queue": frozenset({"off_gcd"}),
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
DECISION_NUMBER_PATTERN = re.compile(r"(\d+)$")

# These are limitations of the audited bridge/prefix contract, not values that
# may be silently defaulted by this consumer.
GLOBAL_EXACT_BLOCKERS = (
    "BRIDGE_NO_CANONICAL_MID_ENCOUNTER_SEED_OR_RESTORE",
    "NO_CONTINUOUS_SIMULATOR_COMMAND_PREFIX",
    "NO_MID_ENCOUNTER_RNG_STATE",
    "NO_PENDING_EVENT_SCHEDULER_STATE",
    "NO_COMMON_COUNTERFACTUAL_HORIZON",
    "NO_PREREGISTERED_SCALAR_REWARD",
    "NO_FULL_POST_ACTION_STATE",
    "CAT2_PROFILE_NOT_SEALED_AT_CAPTURE",
)

ALWAYS_UNKNOWN_FIELDS = {
    "timers.simulator_gcd_remaining_ms": (
        "CONFLICT",
        "The row stores Cat2's reconstructed SPELLCAST timer, not an authoritative "
        "simulator GCD timer.",
    ),
    "timers.off_hand_swing_remaining_ms": (
        "UNKNOWN",
        "No capture-bound loadout proves that an off-hand timer is absent.",
    ),
    "player.current_stance": ("UNKNOWN", "Not captured before the action."),
    "player.target_melee_range": ("UNKNOWN", "No exact range snapshot is present."),
    "player.moving": ("UNKNOWN", "Movement state is absent."),
    "player.cast_or_channel": ("UNKNOWN", "Cast/channel state is absent."),
    "player.auto_attack_active": ("UNKNOWN", "Auto-attack state is absent."),
    "player.strength": ("UNKNOWN", "Dynamic Strength is absent."),
    "player.melee_attack_power": ("UNKNOWN", "Dynamic melee AP is absent."),
    "player.armor_penetration": ("UNKNOWN", "Dynamic armor penetration is absent."),
    "player.aura_identity_stacks_remaining": (
        "UNKNOWN",
        "Only buffCount is captured; aura identity, stacks, and expiry are absent.",
    ),
    "target.aura_identity_stacks_remaining": (
        "UNKNOWN",
        "Only targetBuffCount is captured; aura identity, stacks, and expiry are absent.",
    ),
    "target.base_armor": ("UNKNOWN", "Target base armor is not captured."),
    "target.effective_armor": (
        "UNKNOWN",
        "Armor after debuffs and weapon effects is not captured.",
    ),
    "equipment.capture_bound_loadout": (
        "SOURCE_DERIVED_UNSEALED",
        "The current Cat2 source profile is not fingerprinted in the v4 transition.",
    ),
    "proc.bonereavers_edge_stacks_remaining": (
        "UNKNOWN",
        "Bonereaver's Edge can change target armor by 700 per stack for 10 seconds.",
    ),
    "proc.crusader_strength_remaining": (
        "UNKNOWN",
        "Crusader can change Strength for 15 seconds.",
    ),
    "sim.encounter_elapsed_ms": (
        "UNKNOWN",
        "captured_at is a client clock, not simulator encounter time.",
    ),
    "sim.encounter_remaining_ms": ("UNKNOWN", "Encounter remaining time is absent."),
    "sim.continuous_command_prefix": (
        "UNKNOWN",
        "The accepted rows are action-conditioned and omit intervening decisions.",
    ),
    "sim.rng_state": ("UNKNOWN", "A live encounter has no simulator RNG checkpoint."),
    "sim.pending_events": (
        "UNKNOWN",
        "Pending actions, weapon attacks, ticks, tasks, and event priority are absent.",
    ),
    "evaluation.common_counterfactual_horizon": (
        "UNKNOWN",
        "Observed action-terminal windows are not a common counterfactual horizon.",
    ),
    "evaluation.scalar_reward_contract": (
        "UNKNOWN",
        "No preregistered scalar reward is present.",
    ),
    "transition.full_next_state": (
        "UNKNOWN",
        "Typed immediate outcomes are not complete post-action state snapshots.",
    ),
}


class FuryShadowSimSeedabilityError(RuntimeError):
    """An input receipt or artifact violates the v1 audit contract."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise FuryShadowSimSeedabilityError(
            f"cannot read {label} {path}: {error}"
        ) from error


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_json_object_pairs,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise FuryShadowSimSeedabilityError(f"cannot decode {label}: {error}") from error


def _load_object(raw: bytes, label: str) -> dict[str, Any]:
    value = _decode_json(raw, label)
    if not isinstance(value, dict):
        raise FuryShadowSimSeedabilityError(f"{label} must be a JSON object")
    return value


def _load_jsonl(raw: bytes, label: str) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FuryShadowSimSeedabilityError(f"cannot decode {label}: {error}") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        row = _load_object(line.encode("utf-8"), f"{label} line {line_number}")
        rows.append(row)
    if not rows:
        raise FuryShadowSimSeedabilityError(f"{label} contains no rows")
    return rows


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryShadowSimSeedabilityError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryShadowSimSeedabilityError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryShadowSimSeedabilityError(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryShadowSimSeedabilityError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise FuryShadowSimSeedabilityError(f"{label} must be >= {minimum}")
    return value


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryShadowSimSeedabilityError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FuryShadowSimSeedabilityError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise FuryShadowSimSeedabilityError(f"{label} must be >= {minimum}")
    return result


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_text(value: Any, label: str) -> str:
    text = _text(value, label).casefold()
    if SHA256_PATTERN.fullmatch(text) is None:
        raise FuryShadowSimSeedabilityError(f"{label} must be a lowercase SHA256")
    return text


def _same_path(recorded: Any, actual: Path, label: str) -> None:
    recorded_path = Path(_text(recorded, label)).expanduser().resolve()
    if os.path.normcase(str(recorded_path)) != os.path.normcase(str(actual)):
        raise FuryShadowSimSeedabilityError(
            f"{label} points to {recorded_path}, expected {actual}"
        )


def _require_commit(
    value: Any,
    label: str,
    *,
    written_last_field: str,
) -> Mapping[str, Any]:
    commit = _mapping(value, label)
    if not (
        commit.get("state") == "complete"
        and commit.get(written_last_field) is True
        and commit.get("content_addressed") is True
    ):
        raise FuryShadowSimSeedabilityError(f"{label} is not a complete content-addressed commit")
    return commit


def _identity(row: Mapping[str, Any], label: str) -> dict[str, Any]:
    identity = _mapping(row.get("identity"), f"{label}.identity")
    return {
        "export_session_id": _text(
            identity.get("export_session_id"), f"{label}.identity.export_session_id"
        ),
        "decision_id": _text(
            identity.get("decision_id"), f"{label}.identity.decision_id"
        ),
        "sequence_in_session": _integer(
            identity.get("sequence_in_session"),
            f"{label}.identity.sequence_in_session",
            minimum=1,
        ),
    }


def _identity_key(identity: Mapping[str, Any]) -> tuple[str, str]:
    return str(identity["export_session_id"]), str(identity["decision_id"])


def _normalize_lanes(value: Any, label: str) -> dict[str, list[str]]:
    raw = _mapping(value, label)
    result: dict[str, list[str]] = {}
    for lane in LANES:
        actions = _array(raw.get(lane), f"{label}.{lane}")
        normalized = [_text(action, f"{label}.{lane} action") for action in actions]
        if len(normalized) != len(set(normalized)):
            raise FuryShadowSimSeedabilityError(f"{label}.{lane} contains duplicates")
        result[lane] = normalized
    return result


def _validate_transition_row(
    row: Mapping[str, Any], row_number: int, expected_session: str
) -> dict[str, Any]:
    label = f"transition row {row_number}"
    if row.get("schema") != TRANSITION_SCHEMA:
        raise FuryShadowSimSeedabilityError(f"{label} has an unsupported schema")
    identity = _identity(row, label)
    if identity["export_session_id"] != expected_session:
        raise FuryShadowSimSeedabilityError(f"{label} belongs to another session")
    if identity["sequence_in_session"] != row_number:
        raise FuryShadowSimSeedabilityError(
            f"{label} sequence_in_session must equal its committed row order"
        )

    _mapping(row.get("state_before"), f"{label}.state_before")
    actual = _mapping(row.get("actual"), f"{label}.actual")
    if actual.get("source") != "Cat2_exact_sink_trace":
        raise FuryShadowSimSeedabilityError(f"{label} is not an exact Cat2 sink trace")
    executor = _mapping(actual.get("executor"), f"{label}.actual.executor")
    if not (
        executor.get("kind") == "cat2_configuration_card_stack"
        and executor.get("expert") == "Cat2"
        and executor.get("entry") == "Cat2.ExecuteConfiguration"
    ):
        raise FuryShadowSimSeedabilityError(f"{label} has an unsupported executor")
    factorized = _normalize_lanes(
        actual.get("factorized_action"), f"{label}.actual.factorized_action"
    )
    action_rows = _array(actual.get("actions"), f"{label}.actual.actions")
    materialized: dict[str, list[str]] = {lane: [] for lane in LANES}
    for action_number, value in enumerate(action_rows, start=1):
        action = _mapping(value, f"{label}.actual.actions[{action_number}]")
        lane = _text(action.get("lane"), f"{label} action lane")
        action_id = _text(action.get("action_id"), f"{label} action id")
        if lane not in LANES:
            raise FuryShadowSimSeedabilityError(f"{label} has unsupported lane {lane!r}")
        if action.get("client_accepted") is not True:
            raise FuryShadowSimSeedabilityError(f"{label} contains a rejected action sink")
        materialized[lane].append(action_id)
    if materialized != factorized or not any(factorized.values()):
        raise FuryShadowSimSeedabilityError(
            f"{label} action rows do not equal its nonempty factorized action"
        )

    candidate = _mapping(row.get("candidate"), f"{label}.candidate")
    if candidate.get("executed") is not False or candidate.get("counterfactual_outcome") is not None:
        raise FuryShadowSimSeedabilityError(
            f"{label} candidate must remain unexecuted with no counterfactual outcome"
        )
    candidate_proposal = _normalize_lanes(
        candidate.get("proposal"), f"{label}.candidate.proposal"
    )
    recorded = _mapping(
        row.get("recorded_active_policy"), f"{label}.recorded_active_policy"
    )
    if recorded.get("executed") is not False or recorded.get("counterfactual_outcome") is not None:
        raise FuryShadowSimSeedabilityError(
            f"{label} recorded Brain proposal must remain unexecuted"
        )

    eligibility = _mapping(row.get("eligibility"), f"{label}.eligibility")
    required_eligibility = {
        "behavior_label_eligible": True,
        "observed_actual_immediate_outcome_usable": True,
        "scalar_reward_available": False,
        "recorded_active_policy_counterfactual_reward_available": False,
        "candidate_counterfactual_reward_available": False,
        "full_next_state_available": False,
        "td_transition_eligible": False,
        "offline_rl_episode_eligible": False,
        "deployment_allowed": False,
    }
    if any(eligibility.get(key) is not expected for key, expected in required_eligibility.items()):
        raise FuryShadowSimSeedabilityError(
            f"{label} violates the bounded transition eligibility contract"
        )
    outcome = _mapping(
        row.get("observed_actual_immediate_outcome"), f"{label}.observed outcome"
    )
    if outcome.get("scalar_reward") is not None:
        raise FuryShadowSimSeedabilityError(f"{label} invents a scalar reward")
    temporal = _mapping(row.get("temporal"), f"{label}.temporal")
    _number(
        temporal.get("observed_window_seconds"),
        f"{label}.temporal.observed_window_seconds",
        minimum=0.0,
    )
    provenance = _mapping(row.get("provenance"), f"{label}.provenance")
    if not (
        provenance.get("kind") == "OBSERVED"
        and provenance.get("source_semantics")
        == "exact_cat2_action_with_causally_linked_typed_immediate_outcome"
    ):
        raise FuryShadowSimSeedabilityError(f"{label} has unsupported provenance")
    return {
        "identity": identity,
        "actual": factorized,
        "candidate": candidate_proposal,
    }


def _load_transition_bundle(
    transition_path: Path, manifest_path: Path
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    dataset_raw = _read_bytes(transition_path, "transition dataset")
    manifest_raw = _read_bytes(manifest_path, "transition manifest")
    rows = _load_jsonl(dataset_raw, "transition dataset")
    manifest = _load_object(manifest_raw, "transition manifest")
    if manifest.get("schema") != TRANSITION_MANIFEST_SCHEMA:
        raise FuryShadowSimSeedabilityError("unsupported transition manifest schema")
    if not (
        manifest.get("kind") == "fury_shadow_transition_dataset"
        and manifest.get("record_schema") == TRANSITION_SCHEMA
    ):
        raise FuryShadowSimSeedabilityError("transition manifest kind/schema mismatch")
    _require_commit(
        manifest.get("commit"),
        "transition manifest commit",
        written_last_field="manifest_written_last",
    )
    _same_path(manifest.get("output"), transition_path, "transition manifest output")
    dataset_hash = _sha256(dataset_raw)
    if not hmac.compare_digest(
        _hash_text(manifest.get("output_sha256"), "transition output_sha256"),
        dataset_hash,
    ):
        raise FuryShadowSimSeedabilityError("transition dataset SHA256 mismatch")
    if _integer(manifest.get("row_count"), "transition row_count", minimum=1) != len(rows):
        raise FuryShadowSimSeedabilityError("transition manifest row count mismatch")
    session = _text(manifest.get("export_session_id"), "transition export_session_id")
    validated = [
        _validate_transition_row(row, index, session)
        for index, row in enumerate(rows, start=1)
    ]
    identities = [_identity_key(item["identity"]) for item in validated]
    if len(identities) != len(set(identities)):
        raise FuryShadowSimSeedabilityError("transition composite identities are not unique")
    manifest_identities = _array(manifest.get("identities"), "transition identities")
    recorded_identities = [
        (
            _text(_mapping(item, "transition identity").get("export_session_id"), "identity session"),
            _text(_mapping(item, "transition identity").get("decision_id"), "identity decision"),
        )
        for item in manifest_identities
    ]
    if recorded_identities != identities:
        raise FuryShadowSimSeedabilityError(
            "transition manifest identities do not match committed row order"
        )
    contract = _mapping(
        manifest.get("eligibility_contract"), "transition eligibility_contract"
    )
    for key, expected in {
        "behavior_label_eligible": True,
        "observed_actual_immediate_outcome_usable": True,
        "scalar_reward_available": False,
        "recorded_active_policy_counterfactual_reward_available": False,
        "candidate_counterfactual_reward_available": False,
        "full_next_state_available": False,
        "td_transition_eligible": False,
        "offline_rl_episode_eligible": False,
        "deployment_allowed": False,
    }.items():
        if contract.get(key) is not expected:
            raise FuryShadowSimSeedabilityError(
                f"transition manifest eligibility {key} must be {expected}"
            )
    return rows, manifest, {
        "dataset_sha256": dataset_hash,
        "manifest_sha256": _sha256(manifest_raw),
    }


def _load_cat2_conformance_bundle(
    report_path: Path,
    rows_path: Path,
    *,
    transition_path: Path,
    transition_manifest_path: Path,
    transition_hashes: Mapping[str, str],
    transition_rows: Sequence[Mapping[str, Any]],
    export_session_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    report_raw = _read_bytes(report_path, "Cat2 conformance report")
    rows_raw = _read_bytes(rows_path, "Cat2 conformance rows")
    report = _load_object(report_raw, "Cat2 conformance report")
    rows = _load_jsonl(rows_raw, "Cat2 conformance rows")
    if report.get("schema") != CAT2_REPORT_SCHEMA:
        raise FuryShadowSimSeedabilityError("unsupported Cat2 conformance report schema")
    if report.get("status") != "BOUNDED_DIAGNOSTIC_ONLY":
        raise FuryShadowSimSeedabilityError("Cat2 conformance report status is not bounded")
    _require_commit(
        report.get("commit"),
        "Cat2 conformance commit",
        written_last_field="report_written_last",
    )
    inputs = _mapping(report.get("inputs"), "Cat2 conformance inputs")
    _same_path(
        inputs.get("transition_dataset"), transition_path, "Cat2 transition dataset"
    )
    _same_path(
        inputs.get("transition_manifest"),
        transition_manifest_path,
        "Cat2 transition manifest",
    )
    if inputs.get("transition_manifest_commit") != "PASS":
        raise FuryShadowSimSeedabilityError("Cat2 report did not accept the transition commit")
    if inputs.get("export_session_id") != export_session_id:
        raise FuryShadowSimSeedabilityError("Cat2 report session does not match transitions")
    for key, expected in (
        ("transition_dataset_sha256", transition_hashes["dataset_sha256"]),
        ("transition_manifest_sha256", transition_hashes["manifest_sha256"]),
    ):
        if not hmac.compare_digest(_hash_text(inputs.get(key), f"Cat2 {key}"), expected):
            raise FuryShadowSimSeedabilityError(f"Cat2 report {key} mismatch")

    output = _mapping(report.get("output"), "Cat2 conformance output")
    _same_path(output.get("rows"), rows_path, "Cat2 conformance rows path")
    if output.get("row_schema") != CAT2_ROW_SCHEMA:
        raise FuryShadowSimSeedabilityError("Cat2 conformance row schema mismatch")
    if _integer(output.get("row_count"), "Cat2 row_count", minimum=1) != len(rows):
        raise FuryShadowSimSeedabilityError("Cat2 conformance row count mismatch")
    if len(rows) != len(transition_rows):
        raise FuryShadowSimSeedabilityError("Cat2 and transition row counts differ")
    rows_hash = _sha256(rows_raw)
    if not hmac.compare_digest(
        _hash_text(output.get("rows_sha256"), "Cat2 rows_sha256"), rows_hash
    ):
        raise FuryShadowSimSeedabilityError("Cat2 conformance rows SHA256 mismatch")

    profile = _mapping(report.get("profile"), "Cat2 conformance profile")
    if not (
        profile.get("shape_gate") == "PASS"
        and profile.get("all_transition_profile_id_name_match") is True
        and _integer(
            profile.get("transition_profile_id_name_match_rows"),
            "Cat2 profile identity match rows",
            minimum=0,
        )
        == len(rows)
    ):
        raise FuryShadowSimSeedabilityError("Cat2 profile identity/shape gate is not complete")
    capture_binding = _text(
        profile.get("capture_time_binding"), "Cat2 capture_time_binding"
    )
    if capture_binding != "UNSEALED_ID_NAME_ONLY":
        raise FuryShadowSimSeedabilityError(
            "v1 cannot verify a sealed Cat2 capture-time binding; a future schema "
            "must carry and validate the capture-time profile hash"
        )

    boundary = _mapping(report.get("evidence_boundary"), "Cat2 evidence_boundary")
    if not (
        boundary.get("runtime_label_provenance") == "OBSERVED_EXACT_CAT2_SINK_TRACE"
        and boundary.get("replay_provenance") == "SOURCE_DERIVED"
        and boundary.get("source_derived_is_exact_runtime") is False
        and boundary.get("sample_design")
        == "CLIENT_ACCEPTED_TERMINAL_MAPPED_ACTION_CONDITIONED"
        and boundary.get("decision_denominator_available") is False
        and boundary.get("candidate_counterfactual_reward_available") is False
        and boundary.get("performance_comparison_available") is False
    ):
        raise FuryShadowSimSeedabilityError("Cat2 evidence boundary is incompatible")
    strict = _mapping(report.get("strict_conformance"), "Cat2 strict_conformance")
    if not (
        strict.get("status") == "NOT_EVALUABLE"
        and _integer(strict.get("total_rows"), "Cat2 strict total_rows", minimum=1)
        == len(rows)
        and _integer(
            strict.get("input_complete_rows"),
            "Cat2 strict input_complete_rows",
            minimum=0,
        )
        == 0
    ):
        raise FuryShadowSimSeedabilityError(
            "Cat2 strict conformance must remain NOT_EVALUABLE with zero complete rows"
        )
    assumption_report = _mapping(
        report.get("assumption_conditioned_replay"),
        "Cat2 assumption_conditioned_replay",
    )
    if not (
        assumption_report.get("status") == "PASS"
        and _integer(
            assumption_report.get("evaluated_rows"),
            "Cat2 assumption evaluated_rows",
            minimum=1,
        )
        == len(rows)
        and _integer(
            assumption_report.get("exact_factorized_action_match_rows"),
            "Cat2 assumption match rows",
            minimum=0,
        )
        == len(rows)
        and _integer(
            assumption_report.get("mismatch_rows"),
            "Cat2 assumption mismatch rows",
            minimum=0,
        )
        == 0
    ):
        raise FuryShadowSimSeedabilityError(
            "Cat2 assumption-conditioned replay summary is inconsistent"
        )
    eligibility = _mapping(report.get("eligibility"), "Cat2 eligibility")
    for key in (
        "independent_expert_vote_allowed",
        "offline_rl_episode_eligible",
        "performance_claim_allowed",
        "deployment_allowed",
    ):
        if eligibility.get(key) is not False:
            raise FuryShadowSimSeedabilityError(f"Cat2 eligibility {key} must remain false")

    validated_rows: list[dict[str, Any]] = []
    for index, (row, transition) in enumerate(zip(rows, transition_rows), start=1):
        label = f"Cat2 conformance row {index}"
        if row.get("schema") != CAT2_ROW_SCHEMA:
            raise FuryShadowSimSeedabilityError(f"{label} has an unsupported schema")
        row_identity = _identity(row, label)
        transition_identity = _identity(transition, f"transition row {index}")
        if row_identity != transition_identity:
            raise FuryShadowSimSeedabilityError(f"{label} identity/order mismatch")
        if row.get("profile_identity_match") is not True:
            raise FuryShadowSimSeedabilityError(f"{label} profile identity does not match")
        row_strict = _mapping(row.get("strict"), f"{label}.strict")
        if not (
            row_strict.get("input_complete") is False
            and row_strict.get("status") == "NOT_EVALUABLE"
        ):
            raise FuryShadowSimSeedabilityError(f"{label} overstates strict conformance")
        assumption = _mapping(
            row.get("assumption_conditioned"), f"{label}.assumption_conditioned"
        )
        observed = _normalize_lanes(
            assumption.get("observed_factorized_action"),
            f"{label}.assumption_conditioned.observed_factorized_action",
        )
        predicted = _normalize_lanes(
            assumption.get("predicted_factorized_action"),
            f"{label}.assumption_conditioned.predicted_factorized_action",
        )
        transition_actual = _normalize_lanes(
            _mapping(
                transition.get("actual"), f"transition row {index}.actual"
            ).get("factorized_action"),
            f"transition row {index}.actual.factorized_action",
        )
        if observed != transition_actual:
            raise FuryShadowSimSeedabilityError(
                f"{label} observed action does not match the transition actual"
            )
        expected_match = predicted == observed
        match_flag = assumption.get("exact_factorized_action_match")
        if type(match_flag) is not bool or match_flag != expected_match:
            raise FuryShadowSimSeedabilityError(
                f"{label} assumption-conditioned match flag is inconsistent"
            )
        if not expected_match:
            raise FuryShadowSimSeedabilityError(
                f"{label} contradicts the report's all-row action-match claim"
            )
        if row.get("outcome_or_reward_used_for_replay") is not False:
            raise FuryShadowSimSeedabilityError(f"{label} used an outcome as replay reward")
        validated_rows.append(row)
    return report, validated_rows, {
        "report_sha256": _sha256(report_raw),
        "rows_sha256": rows_hash,
        "capture_time_binding": capture_binding,
    }


def _field(
    status: str,
    source: str,
    *,
    value: Any = None,
    value_present: bool = False,
    unknown: bool,
    exact_seed_equivalent: bool,
    note: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "source": source,
        "unknown": unknown,
        "exact_seed_equivalent": exact_seed_equivalent,
        "note": note,
    }
    if value_present:
        result["value"] = value
    return result


def _observed_number_field(
    state: Mapping[str, Any], key: str, source: str, *, minimum: float = 0.0
) -> tuple[dict[str, Any], float | None]:
    value = state.get(key)
    if value is None:
        return (
            _field(
                "UNKNOWN",
                source,
                unknown=True,
                exact_seed_equivalent=False,
                note=f"{key} is absent.",
            ),
            None,
        )
    number = _number(value, source, minimum=minimum)
    return (
        _field(
            "OBSERVED",
            source,
            value=number,
            value_present=True,
            unknown=False,
            exact_seed_equivalent=True,
            note="Captured directly in state_before.",
        ),
        number,
    )


def _known_number_field(
    state: Mapping[str, Any], key: str, known_key: str, source: str
) -> tuple[dict[str, Any], float | None]:
    if state.get(known_key) is not True:
        return (
            _field(
                "UNKNOWN",
                source,
                unknown=True,
                exact_seed_equivalent=False,
                note=f"{known_key} is not true.",
            ),
            None,
        )
    return _observed_number_field(state, key, source)


def _put_always_unknown(fields: dict[str, dict[str, Any]]) -> None:
    for name, (status, note) in ALWAYS_UNKNOWN_FIELDS.items():
        fields[name] = _field(
            status,
            "not present in transition_fragment/v1",
            unknown=True,
            exact_seed_equivalent=False,
            note=note,
        )


def _state_seedability_row(
    transition: Mapping[str, Any],
    conformance: Mapping[str, Any],
    *,
    capture_binding: str,
) -> dict[str, Any]:
    identity = _identity(transition, "transition")
    label = f"transition {identity['decision_id']}"
    state = _mapping(transition.get("state_before"), f"{label}.state_before")
    actual = _normalize_lanes(
        _mapping(transition.get("actual"), f"{label}.actual").get(
            "factorized_action"
        ),
        f"{label}.actual.factorized_action",
    )
    candidate = _normalize_lanes(
        _mapping(transition.get("candidate"), f"{label}.candidate").get("proposal"),
        f"{label}.candidate.proposal",
    )
    fields: dict[str, dict[str, Any]] = {}
    hard_rejects: list[str] = []

    for output_name, source_key in (
        ("player.health_current", "health"),
        ("player.health_maximum", "maximumHealth"),
        ("player.rage_current", "rage"),
        ("player.rage_maximum", "maximumRage"),
        ("target.health_current", "targetHealth"),
        ("target.health_percent", "targetPercentHealth"),
    ):
        fields[output_name], _ = _observed_number_field(
            state, source_key, f"state_before.{source_key}"
        )

    maximum_rage = fields["player.rage_maximum"].get("value")
    current_rage = fields["player.rage_current"].get("value")
    if current_rage is None or maximum_rage is None or current_rage > maximum_rage:
        hard_rejects.append("INVALID_OR_UNKNOWN_RAGE_STATE")

    gcd_field, gcd = _observed_number_field(state, "gcd", "state_before.gcd")
    gcd_field["status"] = "OBSERVED_CAT2_RECONSTRUCTED"
    gcd_field["exact_seed_equivalent"] = False
    gcd_field["note"] = (
        "Cat2 GetLeftGCD is an inferred SPELLCAST timer and must not be copied into "
        "the simulator GCD."
    )
    fields["timers.cat2_reconstructed_gcd_remaining_s"] = gcd_field
    for output_name, value_key, known_key in (
        ("timers.bloodrage_cooldown_remaining_s", "bloodrageCooldown", "bloodrageCooldownKnown"),
        ("timers.bloodthirst_cooldown_remaining_s", "bloodthirstCooldown", "bloodthirstCooldownKnown"),
        ("timers.whirlwind_cooldown_remaining_s", "whirlwindCooldown", "whirlwindCooldownKnown"),
        ("timers.main_hand_swing_remaining_s", "mainHandSwingRemaining", "mainHandSwingRemainingKnown"),
    ):
        fields[output_name], value = _known_number_field(
            state, value_key, known_key, f"state_before.{value_key}"
        )
        if value is None:
            hard_rejects.append(f"UNKNOWN_{value_key.upper()}")

    queued = state.get("queuedSwing")
    queued_known = state.get("queuedSwingKnown") is True
    queue_valid = queued in {"KEEP", "HEROIC_STRIKE", "CLEAVE"}
    fields["queue.current_next_swing"] = _field(
        "OBSERVED" if queued_known and queue_valid else "UNKNOWN",
        "state_before.queuedSwing",
        value=queued,
        value_present=queued is not None,
        unknown=not (queued_known and queue_valid),
        exact_seed_equivalent=queued_known and queue_valid,
        note=(
            "Observed action-bar queue state."
            if queued_known and queue_valid
            else "Queue state is unknown or unsupported."
        ),
    )
    if not (queued_known and queue_valid):
        hard_rejects.append("UNKNOWN_OR_UNSUPPORTED_QUEUE_STATE")

    for output_name, source_key in (
        ("player.buff_count", "buffCount"),
        ("target.buff_count", "targetBuffCount"),
        ("target.nearby_enemy_count", "nearbyEnemies"),
    ):
        value = state.get(source_key)
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if source_key == "nearbyEnemies":
            valid = valid and state.get("nearbyEnemiesKnown") is True and value >= 1
        fields[output_name] = _field(
            "OBSERVED" if valid else "UNKNOWN",
            f"state_before.{source_key}",
            value=value,
            value_present=value is not None,
            unknown=not valid,
            exact_seed_equivalent=valid and source_key == "nearbyEnemies",
            note=(
                "Count is observed but does not identify aura state."
                if valid and source_key != "nearbyEnemies"
                else "Observed single-frame count."
                if valid
                else "Count is missing or not known."
            ),
        )
    if fields["target.nearby_enemy_count"]["unknown"]:
        hard_rejects.append("UNKNOWN_TARGET_COUNT")

    target_health = fields["target.health_current"].get("value")
    target_percent = fields["target.health_percent"].get("value")
    if (
        isinstance(target_health, (int, float))
        and isinstance(target_percent, (int, float))
        and target_health > 0
        and 0 < target_percent <= 100
    ):
        target_max = float(target_health) / (float(target_percent) / 100.0)
        fields["target.health_maximum"] = _field(
            "DERIVED",
            "state_before.targetHealth / (state_before.targetPercentHealth / 100)",
            value=target_max,
            value_present=True,
            unknown=False,
            exact_seed_equivalent=False,
            note="Algebraically derived; the maximum was not directly captured.",
        )
    else:
        fields["target.health_maximum"] = _field(
            "UNKNOWN",
            "state_before target health fields",
            unknown=True,
            exact_seed_equivalent=False,
            note="Cannot derive a positive target maximum.",
        )
        hard_rejects.append("TARGET_HEALTH_SCALE_UNAVAILABLE")

    class_file = state.get("classFile")
    power_type = state.get("powerType")
    if class_file != "WARRIOR" or power_type != 1:
        hard_rejects.append("NOT_A_WARRIOR_RAGE_STATE")
    if not (
        state.get("inCombat") is True
        and state.get("targetExists") is True
        and state.get("targetCanAttack") is True
        and state.get("targetIsDead") is False
    ):
        hard_rejects.append("NO_LIVE_ATTACKABLE_COMBAT_TARGET")

    _put_always_unknown(fields)
    # Preserve the exact capture-time authority rather than implying that the
    # current Cat2 source profile existed unchanged during this live session.
    fields["equipment.capture_bound_loadout"]["status"] = capture_binding
    fields["equipment.capture_bound_loadout"]["note"] = (
        f"Cat2 conformance reports capture_time_binding={capture_binding}; this does "
        "not bind gear, talents, or proc state to the action snapshot."
    )

    actual_actions = [action for lane in LANES for action in actual[lane]]
    unsupported = sorted(set(actual_actions) - SUPPORTED_ACTIONS)
    if unsupported:
        hard_rejects.append("UNSUPPORTED_ACTUAL_ACTION")
    candidate_actions = [action for lane in LANES for action in candidate[lane]]
    unsupported_candidate = sorted(set(candidate_actions) - SUPPORTED_ACTIONS)
    if unsupported_candidate:
        hard_rejects.append("UNSUPPORTED_CANDIDATE_ACTION")
    actual_lane_mismatches = sorted(
        f"{lane}:{action}"
        for lane in LANES
        for action in actual[lane]
        if lane not in ACTION_ALLOWED_LANES.get(action, frozenset())
    )
    if actual_lane_mismatches:
        hard_rejects.append("INVALID_ACTUAL_ACTION_LANE")
    candidate_lane_mismatches = sorted(
        f"{lane}:{action}"
        for lane in LANES
        for action in candidate[lane]
        if lane not in ACTION_ALLOWED_LANES.get(action, frozenset())
    )
    if candidate_lane_mismatches:
        hard_rejects.append("INVALID_CANDIDATE_ACTION_LANE")

    gcd_action_with_positive_cat2_timer = bool(actual["gcd"] and gcd is not None and gcd > 0)
    conflicts = ["CAT2_GCD_IS_NOT_SIMULATOR_GCD"]
    if gcd_action_with_positive_cat2_timer:
        conflicts.append("POSITIVE_CAT2_GCD_WHILE_GCD_ACTION_WAS_CLIENT_ACCEPTED")
    if actual["off_gcd"] or candidate["off_gcd"]:
        conflicts.append("CURRENT_BEAM_PREFIX_ABSTRACTION_OMITS_OFF_GCD_LANE")

    unknown_mask = {
        name: bool(record["unknown"]) for name, record in sorted(fields.items())
    }
    unknown_fields = [name for name, unknown in unknown_mask.items() if unknown]
    exact_blockers = list(GLOBAL_EXACT_BLOCKERS)
    exact_blockers.extend(
        (
            "CAT2_GCD_NOT_SIMULATOR_EQUIVALENT",
            "PLAYER_AURA_IDENTITIES_STACKS_DURATIONS_MISSING",
            "TARGET_AURA_IDENTITIES_STACKS_DURATIONS_MISSING",
            "DYNAMIC_DAMAGE_STATS_MISSING",
            "TARGET_ARMOR_STATE_MISSING",
            "BONEREAVER_PROC_STATE_MISSING",
            "CRUSADER_PROC_STATE_MISSING",
            "STANCE_RANGE_CAST_MOVEMENT_AUTOATTACK_MISSING",
        )
    )
    if actual["off_gcd"] or candidate["off_gcd"]:
        exact_blockers.append("CURRENT_BEAM_PREFIX_CANNOT_REPLAY_OFF_GCD_LANE")
    hard_rejects = sorted(set(hard_rejects))
    if hard_rejects:
        classification = CLASS_REJECT
    elif exact_blockers or unknown_fields:
        classification = CLASS_APPROX_ONLY
    else:
        classification = CLASS_EXACT

    temporal = _mapping(transition.get("temporal"), f"{label}.temporal")
    return {
        "schema": ROW_SCHEMA,
        "identity": identity,
        "classification": classification,
        "classification_reason": (
            "minimum conditioning fields are invalid or unknown"
            if classification == CLASS_REJECT
            else "partial live observation only; synthetic approximation is not restore"
            if classification == CLASS_APPROX_ONLY
            else "all exact state and bridge requirements are satisfied"
        ),
        "actual_factorized_action": actual,
        "candidate_factorized_action": candidate,
        "cat2_conformance": {
            "profile_identity_match": conformance.get("profile_identity_match"),
            "strict_status": _mapping(
                conformance.get("strict"), f"{label} Cat2 strict"
            ).get("status"),
            "assumption_conditioned_exact_action_match": _mapping(
                conformance.get("assumption_conditioned"),
                f"{label} Cat2 assumption_conditioned",
            ).get("exact_factorized_action_match"),
            "capture_time_binding": capture_binding,
        },
        "observed_action_terminal_window_seconds": _number(
            temporal.get("observed_window_seconds"),
            f"{label}.temporal.observed_window_seconds",
            minimum=0.0,
        ),
        "field_provenance": dict(sorted(fields.items())),
        "unknown_mask": unknown_mask,
        "unknown_fields": unknown_fields,
        "action_lane_mismatches": {
            "actual": actual_lane_mismatches,
            "candidate": candidate_lane_mismatches,
        },
        "conflicts": conflicts,
        "hard_reject_reasons": hard_rejects,
        "exact_blockers": list(dict.fromkeys(exact_blockers)),
        "bridge_projection": {
            "direct_mid_encounter_seed_supported": False,
            "snapshot_restore_supported": False,
            "raw_bridge_act_supports_off_gcd": True,
            "current_beam_prefix_supports_off_gcd": False,
            "gcd_action_with_positive_cat2_timer": gcd_action_with_positive_cat2_timer,
            "approximation_is_restore": False,
        },
        "eligibility": {
            "exact_state_seed_eligible": False,
            "approximation_conditioning_eligible": classification == CLASS_APPROX_ONLY,
            "counterfactual_reward_eligible": False,
            "training_eligible": False,
            "td_transition_eligible": False,
            "offline_rl_episode_eligible": False,
            "performance_claim_allowed": False,
            "deployment_allowed": False,
        },
    }


def _decision_gap_count(rows: Sequence[Mapping[str, Any]]) -> int | None:
    values: list[int] = []
    for row in rows:
        decision_id = str(_mapping(row.get("identity"), "identity").get("decision_id", ""))
        match = DECISION_NUMBER_PATTERN.search(decision_id)
        if match is None:
            return None
        values.append(int(match.group(1)))
    return sum(right != left + 1 for left, right in zip(values, values[1:]))


def _rows_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    )


def build_fury_shadow_sim_seedability(
    transition_path: str | Path = DEFAULT_TRANSITIONS,
    transition_manifest_path: str | Path = DEFAULT_TRANSITION_MANIFEST,
    cat2_conformance_report_path: str | Path = DEFAULT_CAT2_CONFORMANCE_REPORT,
    cat2_conformance_rows_path: str | Path = DEFAULT_CAT2_CONFORMANCE_ROWS,
    *,
    rows_output_path: str | Path = DEFAULT_ROWS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate all receipts and build the read-only seedability artifacts."""

    transitions = Path(transition_path).expanduser().resolve()
    transition_manifest = Path(transition_manifest_path).expanduser().resolve()
    cat2_report = Path(cat2_conformance_report_path).expanduser().resolve()
    cat2_rows_path = Path(cat2_conformance_rows_path).expanduser().resolve()
    rows_output = Path(rows_output_path).expanduser().resolve()
    inputs = {transitions, transition_manifest, cat2_report, cat2_rows_path}
    if len(inputs) != 4:
        raise FuryShadowSimSeedabilityError("seedability input paths must be distinct")
    if rows_output in inputs:
        raise FuryShadowSimSeedabilityError("seedability rows output must not overwrite an input")

    transition_rows, manifest, transition_hashes = _load_transition_bundle(
        transitions, transition_manifest
    )
    conformance_report, conformance_rows, conformance_hashes = (
        _load_cat2_conformance_bundle(
            cat2_report,
            cat2_rows_path,
            transition_path=transitions,
            transition_manifest_path=transition_manifest,
            transition_hashes=transition_hashes,
            transition_rows=transition_rows,
            export_session_id=str(manifest["export_session_id"]),
        )
    )
    capture_binding = str(conformance_hashes["capture_time_binding"])
    output_rows = [
        _state_seedability_row(
            transition,
            conformance,
            capture_binding=capture_binding,
        )
        for transition, conformance in zip(transition_rows, conformance_rows)
    ]
    classification_counts = Counter(row["classification"] for row in output_rows)
    field_coverage: dict[str, dict[str, Any]] = {}
    all_field_names = sorted(
        {name for row in output_rows for name in row["field_provenance"]}
    )
    for name in all_field_names:
        statuses = Counter(
            row["field_provenance"][name]["status"] for row in output_rows
        )
        field_coverage[name] = {
            "status_counts": dict(sorted(statuses.items())),
            "unknown_rows": sum(row["unknown_mask"][name] for row in output_rows),
            "exact_seed_equivalent_rows": sum(
                row["field_provenance"][name]["exact_seed_equivalent"]
                for row in output_rows
            ),
        }

    actual_action_counts: Counter[str] = Counter()
    actual_lane_counts: Counter[str] = Counter()
    candidate_action_counts: Counter[str] = Counter()
    for row in output_rows:
        for lane in LANES:
            actions = row["actual_factorized_action"][lane]
            actual_lane_counts[lane] += len(actions)
            actual_action_counts.update(actions)
            candidate_action_counts.update(row["candidate_factorized_action"][lane])
    windows = [float(row["observed_action_terminal_window_seconds"]) for row in output_rows]
    gcd_conflict_rows = sum(
        row["bridge_projection"]["gcd_action_with_positive_cat2_timer"]
        for row in output_rows
    )
    rows_text = _rows_text(output_rows)
    rows_hash = _sha256(rows_text.encode("utf-8"))
    exact_count = classification_counts[CLASS_EXACT]
    approximate_count = classification_counts[CLASS_APPROX_ONLY]
    reject_count = classification_counts[CLASS_REJECT]
    report = {
        "schema": REPORT_SCHEMA,
        "status": "EXACT_SEEDING_UNAVAILABLE" if exact_count == 0 else "PARTIAL_EXACT",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "transition_dataset": str(transitions),
            "transition_dataset_sha256": transition_hashes["dataset_sha256"],
            "transition_manifest": str(transition_manifest),
            "transition_manifest_sha256": transition_hashes["manifest_sha256"],
            "transition_manifest_commit": "PASS",
            "cat2_conformance_report": str(cat2_report),
            "cat2_conformance_report_sha256": conformance_hashes["report_sha256"],
            "cat2_conformance_rows": str(cat2_rows_path),
            "cat2_conformance_rows_sha256": conformance_hashes["rows_sha256"],
            "cat2_conformance_commit": "PASS",
            "export_session_id": manifest["export_session_id"],
        },
        "classification_contract": {
            "allowed": list(CLASSIFICATIONS),
            "EXACT": (
                "capture-bound complete Markov state plus a tested canonical bridge "
                "seed/restore path and common evaluation contract"
            ),
            "APPROX_ONLY": (
                "minimum conditioning fields exist, but at least one exact field/API/"
                "evaluation requirement is absent; never equivalent to restore"
            ),
            "REJECT": "minimum conditioning state is invalid, unknown, or unsupported",
            "v1_exact_reachable_with_current_bridge": False,
        },
        "classification_counts": {
            CLASS_EXACT: exact_count,
            CLASS_APPROX_ONLY: approximate_count,
            CLASS_REJECT: reject_count,
            "total": len(output_rows),
        },
        "cat2_conformance_receipt": {
            "strict_status": _mapping(
                conformance_report.get("strict_conformance"), "Cat2 strict"
            ).get("status"),
            "capture_time_binding": capture_binding,
            "assumption_conditioned_status": _mapping(
                conformance_report.get("assumption_conditioned_replay"),
                "Cat2 assumption-conditioned replay",
            ).get("status"),
            "profile_is_capture_bound": capture_binding == "SEALED_SEMANTIC_HASH_MATCH",
        },
        "coverage": {
            "actual_action_counts": dict(sorted(actual_action_counts.items())),
            "actual_lane_counts": dict(sorted(actual_lane_counts.items())),
            "candidate_action_counts": dict(sorted(candidate_action_counts.items())),
            "positive_cat2_gcd_with_accepted_gcd_action_rows": gcd_conflict_rows,
            "actual_off_gcd_rows": sum(
                bool(row["actual_factorized_action"]["off_gcd"])
                for row in output_rows
            ),
            "candidate_off_gcd_rows": sum(
                bool(row["candidate_factorized_action"]["off_gcd"])
                for row in output_rows
            ),
            "player_buff_count_positive_rows": sum(
                row["field_provenance"]["player.buff_count"].get("value", 0) > 0
                for row in output_rows
            ),
            "target_buff_count_positive_rows": sum(
                row["field_provenance"]["target.buff_count"].get("value", 0) > 0
                for row in output_rows
            ),
            "source_decision_gap_count": _decision_gap_count(transition_rows),
            "continuous_source_decision_prefix_available": False,
            "observed_action_terminal_window_seconds": {
                "minimum": min(windows),
                "maximum": max(windows),
                "distinct_count": len(set(windows)),
                "is_common_counterfactual_horizon": False,
            },
        },
        "field_coverage": field_coverage,
        "mechanic_sensitivity": {
            "bonereavers_edge": {
                "item_id": 17076,
                "effect": "target armor minus 700 per stack, up to 3 stacks, 10 seconds",
                "capture_status": "UNRESOLVED",
                "reason": "buffCount cannot identify proc stacks or remaining duration",
            },
            "crusader": {
                "enchant_effect_id": 1900,
                "effect": "temporary Strength increase for 15 seconds",
                "capture_status": "UNRESOLVED",
                "reason": "buffCount cannot identify the proc or remaining duration",
            },
            "capture_bound_loadout_available": False,
        },
        "bridge_capability": {
            "load_from_time_zero_request_and_seed": True,
            "deterministic_full_prefix_reconstruction_for_simulator_native_state": True,
            "canonical_mid_encounter_state_seed": False,
            "snapshot_restore": False,
            "rng_checkpoint_restore": False,
            "pending_event_checkpoint_restore": False,
            "raw_act_can_submit_off_gcd": True,
            "current_beam_factorized_prefix_can_replay_off_gcd": False,
            "wait_is_independent_state_setter": False,
            "approximation_is_restore": False,
        },
        "global_exact_blockers": list(GLOBAL_EXACT_BLOCKERS),
        "evaluation_boundary": {
            "common_counterfactual_horizon_available": False,
            "scalar_reward_available": False,
            "candidate_counterfactual_outcome_available": False,
            "full_next_state_available": False,
            "actual_damage_may_be_copied_to_candidate": False,
            "simulated_approximation_may_be_labeled_observed": False,
        },
        "eligibility": {
            "exact_state_seed_rows": 0,
            "counterfactual_reward_eligible": False,
            "training_eligible": False,
            "td_transition_eligible": False,
            "offline_rl_episode_eligible": False,
            "performance_claim_allowed": False,
            "deployment_allowed": False,
        },
        "output": {
            "rows": str(rows_output),
            "rows_sha256": rows_hash,
            "row_count": len(output_rows),
            "row_schema": ROW_SCHEMA,
        },
        "commit": {
            "state": "complete",
            "report_written_last": True,
            "content_addressed": True,
        },
    }
    return report, output_rows


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _verify_inputs_unchanged(report: Mapping[str, Any]) -> None:
    inputs = _mapping(report.get("inputs"), "seedability report inputs")
    checks = (
        ("transition_dataset", "transition_dataset_sha256"),
        ("transition_manifest", "transition_manifest_sha256"),
        ("cat2_conformance_report", "cat2_conformance_report_sha256"),
        ("cat2_conformance_rows", "cat2_conformance_rows_sha256"),
    )
    for path_key, hash_key in checks:
        path = Path(_text(inputs.get(path_key), path_key)).resolve()
        current = _sha256(_read_bytes(path, path_key))
        expected = _hash_text(inputs.get(hash_key), hash_key)
        if not hmac.compare_digest(current, expected):
            raise FuryShadowSimSeedabilityError(
                f"{path_key} changed after validation and before commit"
            )


def materialize_fury_shadow_sim_seedability(
    transition_path: str | Path = DEFAULT_TRANSITIONS,
    transition_manifest_path: str | Path = DEFAULT_TRANSITION_MANIFEST,
    cat2_conformance_report_path: str | Path = DEFAULT_CAT2_CONFORMANCE_REPORT,
    cat2_conformance_rows_path: str | Path = DEFAULT_CAT2_CONFORMANCE_ROWS,
    report_path: str | Path = DEFAULT_REPORT,
    rows_path: str | Path = DEFAULT_ROWS,
) -> dict[str, Any]:
    """Write rows first and the content-addressed report commit marker last."""

    report_output = Path(report_path).expanduser().resolve()
    rows_output = Path(rows_path).expanduser().resolve()
    protected = {
        Path(transition_path).expanduser().resolve(),
        Path(transition_manifest_path).expanduser().resolve(),
        Path(cat2_conformance_report_path).expanduser().resolve(),
        Path(cat2_conformance_rows_path).expanduser().resolve(),
    }
    if report_output == rows_output or report_output in protected or rows_output in protected:
        raise FuryShadowSimSeedabilityError(
            "seedability outputs must be distinct and must not overwrite an input"
        )
    report, rows = build_fury_shadow_sim_seedability(
        transition_path,
        transition_manifest_path,
        cat2_conformance_report_path,
        cat2_conformance_rows_path,
        rows_output_path=rows_output,
    )
    rows_text = _rows_text(rows)
    rows_hash = _sha256(rows_text.encode("utf-8"))
    if report["output"]["rows_sha256"] != rows_hash:
        raise FuryShadowSimSeedabilityError(
            "serialized seedability rows differ from the validated build"
        )
    _verify_inputs_unchanged(report)
    report_text = json.dumps(
        report, ensure_ascii=False, allow_nan=False, indent=2
    ) + "\n"
    _atomic_write(rows_output, rows_text)
    _atomic_write(report_output, report_text)
    counts = report["classification_counts"]
    return {
        "status": report["status"],
        "report": str(report_output),
        "report_sha256": _sha256(report_text.encode("utf-8")),
        "rows": str(rows_output),
        "rows_sha256": rows_hash,
        "row_count": counts["total"],
        "exact_rows": counts[CLASS_EXACT],
        "approx_only_rows": counts[CLASS_APPROX_ONLY],
        "reject_rows": counts[CLASS_REJECT],
        "counterfactual_reward_eligible": False,
        "training_eligible": False,
        "deployment_allowed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit whether accepted Fury Shadow rows can seed WoWSims exactly."
    )
    parser.add_argument("--transitions", default=str(DEFAULT_TRANSITIONS))
    parser.add_argument(
        "--transition-manifest", default=str(DEFAULT_TRANSITION_MANIFEST)
    )
    parser.add_argument(
        "--cat2-conformance-report", default=str(DEFAULT_CAT2_CONFORMANCE_REPORT)
    )
    parser.add_argument(
        "--cat2-conformance-rows", default=str(DEFAULT_CAT2_CONFORMANCE_ROWS)
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--rows", default=str(DEFAULT_ROWS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = materialize_fury_shadow_sim_seedability(
            args.transitions,
            args.transition_manifest,
            args.cat2_conformance_report,
            args.cat2_conformance_rows,
            args.report,
            args.rows,
        )
    except FuryShadowSimSeedabilityError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "CLASS_APPROX_ONLY",
    "CLASS_EXACT",
    "CLASS_REJECT",
    "FuryShadowSimSeedabilityError",
    "build_fury_shadow_sim_seedability",
    "materialize_fury_shadow_sim_seedability",
]


if __name__ == "__main__":
    raise SystemExit(main())
