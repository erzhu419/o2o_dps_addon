"""Bounded conformance audit for the deployed Cat2 Fury bridge profile.

This module compares the exact Cat2 action sinks in an accepted Shadow
transition dataset with a source-derived replay of the *current* persisted
``BrainOfCat Shadow`` card stack.  It deliberately keeps three different facts
separate:

* the online label is an observed Cat2 sink;
* the replay is a Python translation of readable Lua and does not execute Lua;
* the current Cat2 SavedVariables profile was not fingerprinted in the v4
  transition rows, so it is not cryptographically bound to capture time.

The accepted smoke dataset is action-conditioned: it contains successful
mapped actions, not every macro evaluation.  Missing pre-action stance, range,
channel, auto-attack, and effective-cost fields also make strict replay
impossible.  The report therefore exposes ``NOT_EVALUABLE`` for strict
conformance and reports the useful 12-row result only as an explicitly
assumption-conditioned diagnostic.  Nothing produced here enables an expert
vote, an offline-RL transition, a performance claim, or deployment.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
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
DEFAULT_WOW_ROOT = Path(os.environ.get("BOC_WOW_ROOT", r"D:\WOW"))
DEFAULT_CHARACTER_SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CHARACTER_SAVEDVARIABLES",
        str(PROJECT_ROOT / ".local" / "SavedVariables"),
    )
)
DEFAULT_CAT2_SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CAT2_SAVEDVARIABLES",
        str(DEFAULT_CHARACTER_SAVEDVARIABLES / "Cat2.lua"),
    )
)
DEFAULT_CAT2_INSTALLED_ROOT = Path(
    os.environ.get(
        "BOC_CAT2_INSTALLED_ROOT",
        str(DEFAULT_WOW_ROOT / "Interface" / "AddOns" / "Cat2"),
    )
)
DEFAULT_BRAIN_OF_CAT_ROOT = PROJECT_ROOT.parent
DEFAULT_REPORT = (
    PROJECT_ROOT
    / "offline_data"
    / "reports"
    / "fury_cat2_profile_conformance_v1.json"
)
DEFAULT_ROWS = DEFAULT_REPORT.with_name("fury_cat2_profile_conformance_v1.rows.jsonl")

REPORT_SCHEMA = "fury_cat2_profile_conformance_report/v1"
TRANSITION_SCHEMA = "fury_shadow_transition_fragment/v1"
TRANSITION_MANIFEST_SCHEMA = "fury_shadow_transition_dataset_manifest/v1"
PROFILE_NAME = "BrainOfCat Shadow"
LANES = ("off_gcd", "queue", "gcd")
ACTION_FAMILY_BY_ID = {
    "warrior_bloodrage": "bloodrage",
    "warrior_bloodthirst": "bloodthirst",
    "warrior_whirlwind": "whirlwind",
    "warrior_execute": "execute",
    "warrior_heroic_strike": "heroic_strike",
    "warrior_cleave": "cleave",
}
WW_READY_OFFSET_SECONDS = 0.5
WW_BOUNDARY_SENSITIVITY_SECONDS = 0.05

EXPECTED_STEP_IDS = (
    "warrior_o2o_policy_brain",
    "common_auto_attack",
    "warrior_berserker_stance",
    "warrior_bloodrage",
    "warrior_execute",
    "warrior_bloodthirst",
    "warrior_whirlwind",
    "warrior_heroic_strike_alt",
)

# These are concept names rather than silently defaulted FuryExpertState
# attributes.  None exists in transition_fragment/v1.
STRICT_MISSING_STATE_FIELDS = (
    "current_stance",
    "target_melee_range",
    "channel_or_cast_gate",
    "auto_attack_active",
    "effective_execute_cost",
    "effective_whirlwind_cost",
)

DIAGNOSTIC_ASSUMPTIONS = {
    "current_stance": "BERSERKER",
    "target_melee_range": True,
    "cat2_get_channeled_seconds_lt": 0.08,
    "auto_attack_active_before_profile": True,
    "effective_execute_cost": 15.0,
    "effective_whirlwind_cost_upper_bound": 25.0,
    "profile_card_registry_matches_readable_sources": True,
    "state_capture_to_card_evaluation_does_not_cross_ready_boundary": True,
}


class FuryCat2ProfileConformanceError(RuntimeError):
    """An input bundle or profile violates the bounded audit contract."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json_bytes(raw: bytes, path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_json_object_pairs,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise FuryCat2ProfileConformanceError(
            f"cannot decode {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryCat2ProfileConformanceError(f"{label} must be a JSON object")
    return value


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise FuryCat2ProfileConformanceError(
            f"cannot read {label} {path}: {error}"
        ) from error


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCat2ProfileConformanceError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryCat2ProfileConformanceError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryCat2ProfileConformanceError(f"{label} must be non-empty text")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FuryCat2ProfileConformanceError(f"{label} must be an integer")
    if positive and value <= 0:
        raise FuryCat2ProfileConformanceError(f"{label} must be positive")
    return value


def _number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise FuryCat2ProfileConformanceError(f"{label} must be finite numeric")
    return float(value)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FuryCat2ProfileConformanceError(f"{label} must be boolean")
    return value


def _declared_path(value: Any, owner: Path, label: str) -> Path:
    declared = Path(_text(value, label)).expanduser()
    if not declared.is_absolute():
        declared = owner.parent / declared
    return declared.resolve()


def _load_transition_bundle(
    transition_path: Path,
    manifest_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    """Validate the manifest commit marker and its exact dataset bytes."""

    first_dataset = _read_bytes(transition_path, "transition dataset")
    first_manifest = _read_bytes(manifest_path, "transition manifest")
    manifest = _load_json_bytes(first_manifest, manifest_path, "transition manifest")

    if manifest.get("schema") != TRANSITION_MANIFEST_SCHEMA:
        raise FuryCat2ProfileConformanceError(
            "transition manifest schema is not supported"
        )
    if manifest.get("record_schema") != TRANSITION_SCHEMA:
        raise FuryCat2ProfileConformanceError(
            "transition manifest record schema is not supported"
        )
    commit = _mapping(manifest.get("commit"), "transition manifest commit")
    if not (
        commit.get("state") == "complete"
        and commit.get("manifest_written_last") is True
        and commit.get("content_addressed") is True
    ):
        raise FuryCat2ProfileConformanceError(
            "transition manifest is not a complete content-addressed commit"
        )
    if _declared_path(manifest.get("output"), manifest_path, "manifest output") != transition_path:
        raise FuryCat2ProfileConformanceError(
            "transition manifest output path does not name the selected dataset"
        )
    expected_hash = _text(
        manifest.get("output_sha256"), "transition manifest output_sha256"
    ).lower()
    observed_hash = _sha256(first_dataset)
    if not hmac.compare_digest(expected_hash, observed_hash):
        raise FuryCat2ProfileConformanceError(
            "transition dataset SHA256 does not match its committed manifest"
        )

    rows: list[dict[str, Any]] = []
    try:
        decoded = first_dataset.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FuryCat2ProfileConformanceError(
            f"cannot decode transition dataset {transition_path}: {error}"
        ) from error
    for line_number, line in enumerate(decoded.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(
                line,
                parse_constant=_reject_nonfinite_json,
                object_pairs_hook=_json_object_pairs,
            )
        except ValueError as error:
            raise FuryCat2ProfileConformanceError(
                f"transition dataset line {line_number} is invalid: {error}"
            ) from error
        if not isinstance(row, dict):
            raise FuryCat2ProfileConformanceError(
                f"transition dataset line {line_number} must be an object"
            )
        if row.get("schema") != TRANSITION_SCHEMA:
            raise FuryCat2ProfileConformanceError(
                f"transition dataset line {line_number} has unsupported schema"
            )
        rows.append(row)

    declared_count = _integer(manifest.get("row_count"), "manifest row_count")
    if declared_count <= 0 or declared_count != len(rows):
        raise FuryCat2ProfileConformanceError(
            "transition manifest row_count does not match the dataset"
        )
    manifest_identities = _array(manifest.get("identities"), "manifest identities")
    row_identities: list[tuple[str, str]] = []
    declared_identities: list[tuple[str, str]] = []
    for index, row in enumerate(rows, start=1):
        identity = _mapping(row.get("identity"), f"row {index} identity")
        row_identities.append(
            (
                _text(identity.get("export_session_id"), "row export_session_id"),
                _text(identity.get("decision_id"), "row decision_id"),
            )
        )
    for index, item in enumerate(manifest_identities, start=1):
        identity = _mapping(item, f"manifest identity {index}")
        declared_identities.append(
            (
                _text(identity.get("export_session_id"), "manifest export_session_id"),
                _text(identity.get("decision_id"), "manifest decision_id"),
            )
        )
    if len(set(row_identities)) != len(row_identities):
        raise FuryCat2ProfileConformanceError(
            "transition dataset contains duplicate identities"
        )
    if row_identities != declared_identities:
        raise FuryCat2ProfileConformanceError(
            "transition manifest identities do not exactly match dataset order"
        )
    sessions = {identity[0] for identity in row_identities}
    if len(sessions) != 1 or manifest.get("export_session_id") not in sessions:
        raise FuryCat2ProfileConformanceError(
            "transition dataset is not the single session committed by the manifest"
        )
    eligibility = _mapping(
        manifest.get("eligibility_contract"), "manifest eligibility_contract"
    )
    if not (
        eligibility.get("behavior_label_eligible") is True
        and eligibility.get("offline_rl_episode_eligible") is False
        and eligibility.get("deployment_allowed") is False
    ):
        raise FuryCat2ProfileConformanceError(
            "transition manifest eligibility boundary is incompatible with this audit"
        )

    final_dataset = _read_bytes(transition_path, "transition dataset")
    final_manifest = _read_bytes(manifest_path, "transition manifest")
    if final_dataset != first_dataset or final_manifest != first_manifest:
        raise FuryCat2ProfileConformanceError(
            "transition bundle changed while it was being validated"
        )
    return rows, manifest, {
        "dataset_sha256": observed_hash,
        "manifest_sha256": _sha256(first_manifest),
    }


def _snapshot_as_mapping(snapshot: Any) -> Mapping[str, Any]:
    if isinstance(snapshot, Mapping):
        return snapshot
    to_dict = getattr(snapshot, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return _mapping(value, "Cat2 profile snapshot.to_dict()")
    if is_dataclass(snapshot):
        return _mapping(asdict(snapshot), "Cat2 profile snapshot")
    raise FuryCat2ProfileConformanceError(
        "Cat2 profile loader returned no mapping-compatible snapshot"
    )


def _default_profile_loader(
    path: Path,
    profile_name: str,
    installed_root: Path,
    brainofcat_root: Path,
) -> Any:
    """Call the shared literal-only loader without depending on its internals."""

    try:
        from . import cat2_saved_profile_v1 as loader_module
    except ImportError as error:
        raise FuryCat2ProfileConformanceError(
            "cat2_saved_profile_v1 is unavailable"
        ) from error
    for function_name in (
        "load_cat2_saved_profile",
        "load_cat2_profile_snapshot",
        "load_cat2_saved_profile_snapshot",
    ):
        function = getattr(loader_module, function_name, None)
        if not callable(function):
            continue
        try:
            return function(path, profile_name, installed_root, brainofcat_root)
        except Exception as error:  # normalized into this module's public error
            raise FuryCat2ProfileConformanceError(
                f"Cat2 profile load failed: {error}"
            ) from error
    raise FuryCat2ProfileConformanceError(
        "cat2_saved_profile_v1 exposes no supported profile loader"
    )


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalize_profile_snapshot(snapshot: Any) -> dict[str, Any]:
    root = _snapshot_as_mapping(snapshot)
    profile_value = _first_value(root, "profile", "selected_profile")
    profile = _mapping(profile_value if profile_value is not None else root, "profile")
    profile_id = _first_value(profile, "profile_id", "id")
    if profile_id is None:
        profile_id = _first_value(root, "profile_id", "selected_profile_id")
    profile_id = _integer(profile_id, "Cat2 profile id", positive=True)
    profile_name = _first_value(profile, "profile_name", "name")
    if profile_name is None:
        profile_name = _first_value(root, "profile_name", "selected_profile_name")
    profile_name = _text(profile_name, "Cat2 profile name")
    raw_steps = _first_value(profile, "steps", "card_stack")
    steps: list[dict[str, Any]] = []
    for index, value in enumerate(_array(raw_steps, "Cat2 profile steps"), start=1):
        step = _mapping(value, f"Cat2 profile step {index}")
        card_id = _text(
            _first_value(step, "card_id", "id"), f"Cat2 profile step {index} id"
        )
        enabled_value = step.get("enabled", True)
        if isinstance(enabled_value, bool):
            enabled = enabled_value
        elif type(enabled_value) is int and enabled_value in (0, 1):
            enabled = enabled_value == 1
        else:
            raise FuryCat2ProfileConformanceError(
                f"Cat2 profile step {index} enabled must be boolean or 0/1"
            )
        options_value = _first_value(step, "option_values", "optionValues", "options")
        options = {} if options_value is None else dict(
            _mapping(options_value, f"Cat2 profile step {index} options")
        )
        steps.append(
            {
                "position": index,
                "card_id": card_id,
                "enabled": enabled,
                "option_values": options,
            }
        )

    source = _first_value(root, "source", "source_file")
    source_map = source if isinstance(source, Mapping) else {}
    source_sha256 = _first_value(
        root,
        "source_sha256",
        "raw_sha256",
        "savedvariables_sha256",
        "raw_savedvariables_sha256",
    ) or _first_value(source_map, "sha256", "source_sha256")
    semantic_sha256 = _first_value(
        root,
        "semantic_sha256",
        "profile_sha256",
        "execution_semantic_sha256",
        "profile_semantic_sha256",
    ) or _first_value(profile, "semantic_sha256", "profile_sha256")
    source_bundle_sha256 = _first_value(root, "source_bundle_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot lacks a valid source SHA256"
        )
    if not isinstance(semantic_sha256, str) or len(semantic_sha256) != 64:
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot lacks a valid semantic SHA256"
        )
    if not isinstance(source_bundle_sha256, str) or len(source_bundle_sha256) != 64:
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot lacks a valid source-bundle SHA256"
        )
    selection = root.get("selection")
    selection_map = selection if isinstance(selection, Mapping) else {}
    active_profile_id = _first_value(root, "active_profile_id", "activeProfileId")
    if active_profile_id is None:
        active_profile_id = _first_value(
            selection_map, "active_profile_id", "activeProfileId"
        )
    if active_profile_id is not None:
        active_profile_id = _integer(active_profile_id, "Cat2 active profile id", positive=True)
    profile_order = _first_value(root, "profile_order", "profileOrder")
    if profile_order is None:
        profile_order = _first_value(selection_map, "profile_order", "profileOrder")
    if profile_order is not None:
        profile_order = [
            _integer(item, "Cat2 profile order id", positive=True)
            for item in _array(profile_order, "Cat2 profile order")
        ]
    return {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "active_profile_id": active_profile_id,
        "profile_order": profile_order,
        "steps": steps,
        "source_sha256": source_sha256.lower(),
        "semantic_sha256": semantic_sha256.lower(),
        "source_bundle_sha256": source_bundle_sha256.lower(),
        "loader_snapshot": dict(root),
    }


def _validate_expected_profile(profile: Mapping[str, Any]) -> None:
    if profile.get("profile_name") != PROFILE_NAME:
        raise FuryCat2ProfileConformanceError(
            f"selected Cat2 profile is not {PROFILE_NAME!r}"
        )
    steps = _array(profile.get("steps"), "normalized Cat2 steps")
    enabled_ids = tuple(
        _text(step.get("card_id"), "normalized Cat2 card id")
        for step in steps
        if _mapping(step, "normalized Cat2 step").get("enabled") is True
    )
    if enabled_ids != EXPECTED_STEP_IDS or len(steps) != len(EXPECTED_STEP_IDS):
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile does not match the exact enabled BrainOfCat Shadow bridge stack"
        )
    brain_options = _mapping(steps[0].get("option_values"), "Brain step options")
    if brain_options.get("liveMode") is not False:
        raise FuryCat2ProfileConformanceError(
            "Cat2 Brain step is not provably Shadow-only"
        )
    bloodrage_options = _mapping(
        steps[3].get("option_values"), "Bloodrage step options"
    )
    if _number(bloodrage_options.get("maximumRage"), "Bloodrage maximumRage") != 30.0:
        raise FuryCat2ProfileConformanceError(
            "Cat2 Bloodrage maximumRage is not the audited value 30"
        )
    queue_options = _mapping(
        steps[7].get("option_values"), "HeroicStrikeAlt step options"
    )
    if _number(queue_options.get("rageThreshold"), "HeroicStrikeAlt rageThreshold") != 50.0:
        raise FuryCat2ProfileConformanceError(
            "Cat2 HeroicStrikeAlt rageThreshold is not the audited value 50"
        )


def _proposal() -> dict[str, list[str]]:
    return {lane: [] for lane in LANES}


def _normalized_actual(row: Mapping[str, Any], row_label: str) -> dict[str, list[str]]:
    actual = _mapping(row.get("actual"), f"{row_label}.actual")
    factorized = _mapping(
        actual.get("factorized_action"), f"{row_label}.actual.factorized_action"
    )
    result = _proposal()
    for lane in LANES:
        values = _array(factorized.get(lane), f"{row_label}.actual.{lane}")
        if any(not isinstance(value, str) or not value for value in values):
            raise FuryCat2ProfileConformanceError(
                f"{row_label}.actual.{lane} contains an invalid action id"
            )
        result[lane] = list(values)
    return result


def _profile_identity_from_row(
    row: Mapping[str, Any], row_label: str
) -> tuple[int, str, str | None]:
    actual = _mapping(row.get("actual"), f"{row_label}.actual")
    executor_value = actual.get("executor")
    executor = _mapping(executor_value, f"{row_label}.actual.executor")
    if executor.get("kind") != "cat2_configuration_card_stack":
        raise FuryCat2ProfileConformanceError(
            f"{row_label} actual executor is not a Cat2 configuration card stack"
        )
    profile_id = _integer(executor.get("profile_id"), f"{row_label} profile_id", positive=True)
    profile_name = _text(executor.get("profile_name"), f"{row_label} profile_name")
    captured_hash = executor.get("profile_semantic_sha256")
    if captured_hash is not None:
        captured_hash = _text(captured_hash, f"{row_label} profile semantic hash").lower()
        if len(captured_hash) != 64:
            raise FuryCat2ProfileConformanceError(
                f"{row_label} profile semantic hash is not SHA256"
            )
    return profile_id, profile_name, captured_hash


def _state_bool(state: Mapping[str, Any], key: str, label: str) -> bool:
    return _boolean(state.get(key), f"{label}.{key}")


def _known_number(
    state: Mapping[str, Any], value_key: str, known_key: str, label: str
) -> float:
    if state.get(known_key) is not True:
        raise FuryCat2ProfileConformanceError(
            f"{label}.{value_key} is not explicitly known"
        )
    return _number(state.get(value_key), f"{label}.{value_key}")


def _assumption_conditioned_replay(
    row: Mapping[str, Any],
    profile: Mapping[str, Any],
    row_label: str,
) -> tuple[dict[str, list[str]], list[dict[str, Any]], dict[str, Any]]:
    """Replay the fixed current stack under the report's named assumptions."""

    state = _mapping(row.get("state_before"), f"{row_label}.state_before")
    rage = _number(state.get("rage"), f"{row_label}.state_before.rage")
    target_health = _number(
        state.get("targetPercentHealth"),
        f"{row_label}.state_before.targetPercentHealth",
    )
    target_exists = _state_bool(state, "targetExists", f"{row_label}.state_before")
    in_combat = _state_bool(state, "inCombat", f"{row_label}.state_before")
    result = _proposal()
    trace: list[dict[str, Any]] = []
    ww_cooldown: float | None = None

    for step in _array(profile.get("steps"), "normalized Cat2 steps"):
        step_map = _mapping(step, "normalized Cat2 step")
        if step_map.get("enabled") is not True:
            continue
        card_id = _text(step_map.get("card_id"), "normalized card id")
        options = _mapping(step_map.get("option_values"), f"{card_id} options")
        selected = False
        stops = False
        detail = "guard_false"

        if card_id == "warrior_o2o_policy_brain":
            detail = "shadow_observer_pass_through"
        elif card_id == "common_auto_attack":
            detail = "assumed_already_active_no_sink"
        elif card_id == "warrior_berserker_stance":
            detail = "assumed_already_berserker"
        elif card_id == "warrior_bloodrage":
            cooldown = _known_number(
                state,
                "bloodrageCooldown",
                "bloodrageCooldownKnown",
                f"{row_label}.state_before",
            )
            maximum = _number(options.get("maximumRage"), "Bloodrage maximumRage")
            if in_combat and target_exists and rage < maximum and cooldown <= 0.0:
                result["off_gcd"].append("warrior_bloodrage")
                selected = True
                detail = "off_gcd_emitted_then_continue"
        elif card_id == "warrior_execute":
            if target_exists and rage >= 15.0 and target_health < 19.9:
                result["gcd"].append("warrior_execute")
                selected = True
                stops = True
                detail = "gcd_emitted_stop"
        elif card_id == "warrior_bloodthirst":
            cooldown = _known_number(
                state,
                "bloodthirstCooldown",
                "bloodthirstCooldownKnown",
                f"{row_label}.state_before",
            )
            if target_exists and rage >= 30.0 and cooldown <= 0.0:
                result["gcd"].append("warrior_bloodthirst")
                selected = True
                stops = True
                detail = "gcd_emitted_stop"
        elif card_id == "warrior_whirlwind":
            ww_cooldown = _known_number(
                state,
                "whirlwindCooldown",
                "whirlwindCooldownKnown",
                f"{row_label}.state_before",
            )
            if (
                target_exists
                and rage >= 25.0
                and ww_cooldown < WW_READY_OFFSET_SECONDS
            ):
                result["gcd"].append("warrior_whirlwind")
                selected = True
                stops = True
                detail = "gcd_emitted_stop_with_lt_0_5_ready_offset"
        elif card_id == "warrior_heroic_strike_alt":
            threshold = _number(
                options.get("rageThreshold"), "HeroicStrikeAlt rageThreshold"
            )
            if target_exists and rage >= threshold:
                nearby = _known_number(
                    state,
                    "nearbyEnemies",
                    "nearbyEnemiesKnown",
                    f"{row_label}.state_before",
                )
                result["queue"].append(
                    "warrior_cleave" if nearby >= 2.0 else "warrior_heroic_strike"
                )
                selected = True
                detail = "queue_emitted_end_of_stack"
        else:  # profile validation should make this unreachable
            raise FuryCat2ProfileConformanceError(
                f"unsupported enabled Cat2 card {card_id!r}"
            )

        trace.append(
            {
                "position": step_map.get("position"),
                "card_id": card_id,
                "selected": selected,
                "stopped_sequence": stops,
                "detail": detail,
            }
        )
        if stops:
            break

    boundary = {
        "whirlwind_ready_offset_seconds": WW_READY_OFFSET_SECONDS,
        "sensitivity_window_seconds": WW_BOUNDARY_SENSITIVITY_SECONDS,
        "whirlwind_cooldown_at_capture": ww_cooldown,
        "whirlwind_ready_offset_margin_seconds": (
            None
            if ww_cooldown is None
            else abs(ww_cooldown - WW_READY_OFFSET_SECONDS)
        ),
        "state_to_card_latency_sensitive": (
            ww_cooldown is not None
            and ww_cooldown >= WW_READY_OFFSET_SECONDS
            and ww_cooldown - WW_READY_OFFSET_SECONDS
            <= WW_BOUNDARY_SENSITIVITY_SECONDS
        ),
    }
    return result, trace, boundary


def build_fury_cat2_profile_conformance(
    transition_path: str | Path = DEFAULT_TRANSITIONS,
    transition_manifest_path: str | Path = DEFAULT_TRANSITION_MANIFEST,
    cat2_savedvariables_path: str | Path = DEFAULT_CAT2_SAVEDVARIABLES,
    cat2_installed_root: str | Path = DEFAULT_CAT2_INSTALLED_ROOT,
    brainofcat_root: str | Path = DEFAULT_BRAIN_OF_CAT_ROOT,
    *,
    profile_name: str = PROFILE_NAME,
    profile_loader: Callable[[Path, str, Path, Path], Any] | None = None,
    profile_snapshot: Any | None = None,
    rows_output_path: str | Path = DEFAULT_ROWS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the bounded report without writing an output file."""

    transitions = Path(transition_path).expanduser().resolve()
    transition_manifest = Path(transition_manifest_path).expanduser().resolve()
    cat2_savedvariables = Path(cat2_savedvariables_path).expanduser().resolve()
    cat2_installed = Path(cat2_installed_root).expanduser().resolve()
    brainofcat = Path(brainofcat_root).expanduser().resolve()
    selected_profile_name = _text(profile_name, "profile name")
    if selected_profile_name != PROFILE_NAME:
        raise FuryCat2ProfileConformanceError(
            f"v1 only audits the fixed profile {PROFILE_NAME!r}"
        )
    if len(
        {
            transitions,
            transition_manifest,
            cat2_savedvariables,
            cat2_installed,
            brainofcat,
        }
    ) != 5:
        raise FuryCat2ProfileConformanceError("audit input paths must be distinct")

    rows, manifest, transition_hashes = _load_transition_bundle(
        transitions, transition_manifest
    )
    cat2_bytes_before = _read_bytes(cat2_savedvariables, "Cat2 SavedVariables")
    if profile_snapshot is None:
        selected_loader = profile_loader or _default_profile_loader
        try:
            loaded = selected_loader(
                cat2_savedvariables,
                selected_profile_name,
                cat2_installed,
                brainofcat,
            )
        except FuryCat2ProfileConformanceError:
            raise
        except Exception as error:
            raise FuryCat2ProfileConformanceError(
                f"Cat2 profile load failed: {error}"
            ) from error
    else:
        loaded = profile_snapshot
    profile = _normalize_profile_snapshot(loaded)
    _validate_expected_profile(profile)
    loader_snapshot = profile["loader_snapshot"]
    source_bundle = _mapping(
        loader_snapshot.get("source_bundle"), "Cat2 profile source_bundle"
    )
    if not (
        source_bundle.get("scope") == "DIRECT_RUNTIME_DEPENDENCY_PINNED"
        and source_bundle.get("pin_status") == "PINNED_EXACT"
        and source_bundle.get("transitive_dependency_closure_claimed") is False
    ):
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot does not pin the complete direct runtime dependency scope"
        )
    if loader_snapshot.get("authority_state") != "CURRENT_UNSEALED_SOURCE_PROFILE":
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot has an unsupported authority state"
        )
    cat2_bytes_after = _read_bytes(cat2_savedvariables, "Cat2 SavedVariables")
    if cat2_bytes_after != cat2_bytes_before:
        raise FuryCat2ProfileConformanceError(
            "Cat2 SavedVariables changed while the conformance audit was loading it"
        )
    if not hmac.compare_digest(
        profile["source_sha256"], _sha256(cat2_bytes_before)
    ):
        raise FuryCat2ProfileConformanceError(
            "Cat2 profile snapshot raw hash does not match the selected SavedVariables"
        )

    profile_identity_matches = 0
    captured_hash_values: set[str] = set()
    captured_hash_rows = 0
    family_counts: Counter[str] = Counter()
    action_id_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    exact_matches = 0
    boundary_sensitive = 0
    row_reports: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        identity = _mapping(row.get("identity"), f"row {row_index} identity")
        decision_id = _text(identity.get("decision_id"), "decision id")
        row_label = f"transition {decision_id}"
        eligibility = _mapping(row.get("eligibility"), f"{row_label}.eligibility")
        if not (
            eligibility.get("behavior_label_eligible") is True
            and eligibility.get("offline_rl_episode_eligible") is False
            and eligibility.get("deployment_allowed") is False
        ):
            raise FuryCat2ProfileConformanceError(
                f"{row_label} violates the bounded transition eligibility contract"
            )
        provenance = _mapping(row.get("provenance"), f"{row_label}.provenance")
        if not (
            provenance.get("kind") == "OBSERVED"
            and provenance.get("source_semantics")
            == "exact_cat2_action_with_causally_linked_typed_immediate_outcome"
        ):
            raise FuryCat2ProfileConformanceError(
                f"{row_label} is not an observed exact Cat2 action label"
            )
        row_profile_id, row_profile_name, captured_hash = _profile_identity_from_row(
            row, row_label
        )
        identity_match = (
            row_profile_id == profile["profile_id"]
            and row_profile_name == profile["profile_name"]
        )
        profile_identity_matches += int(identity_match)
        if captured_hash is not None:
            captured_hash_values.add(captured_hash)
            captured_hash_rows += 1
        actual = _normalized_actual(row, row_label)
        for lane in LANES:
            lane_counts[lane] += len(actual[lane])
        actual_actions = [action for lane in LANES for action in actual[lane]]
        if not actual_actions:
            raise FuryCat2ProfileConformanceError(
                f"{row_label} contains no action-conditioned behavior label"
            )
        for action in actual_actions:
            action_id_counts[action] += 1
            family = ACTION_FAMILY_BY_ID.get(action)
            if family is None:
                raise FuryCat2ProfileConformanceError(
                    f"{row_label} has an action outside the audited Cat2 profile: {action!r}"
                )
            family_counts[family] += 1
        predicted, card_trace, boundary = _assumption_conditioned_replay(
            row, profile, row_label
        )
        exact_match = identity_match and predicted == actual
        exact_matches += int(exact_match)
        boundary_sensitive += int(boundary["state_to_card_latency_sensitive"])
        row_reports.append(
            {
                "schema": "fury_cat2_profile_conformance_row/v1",
                "identity": {
                    "export_session_id": identity.get("export_session_id"),
                    "decision_id": decision_id,
                    "sequence_in_session": identity.get("sequence_in_session"),
                },
                "profile_identity_match": identity_match,
                "strict": {
                    "input_complete": False,
                    "missing_pre_action_fields": list(STRICT_MISSING_STATE_FIELDS),
                    "status": "NOT_EVALUABLE",
                },
                "assumption_conditioned": {
                    "predicted_factorized_action": predicted,
                    "observed_factorized_action": actual,
                    "exact_factorized_action_match": exact_match,
                    "card_trace": card_trace,
                },
                "boundary_sensitivity": boundary,
                "outcome_or_reward_used_for_replay": False,
            }
        )

    total = len(rows)
    all_identity_match = profile_identity_matches == total
    captured_hash_match = (
        captured_hash_rows == total
        and len(captured_hash_values) == 1
        and profile["semantic_sha256"] in captured_hash_values
    )
    if captured_hash_match:
        capture_binding = "SEALED_SEMANTIC_HASH_MATCH"
    elif not captured_hash_values:
        capture_binding = "UNSEALED_ID_NAME_ONLY"
    elif captured_hash_values == {profile["semantic_sha256"]}:
        capture_binding = "PARTIALLY_SEALED_SEMANTIC_HASH_MATCH"
    else:
        capture_binding = "SEALED_SEMANTIC_HASH_MISMATCH"
    captured_hash_mismatch = capture_binding in {
        "PARTIALLY_SEALED_SEMANTIC_HASH_MATCH",
        "SEALED_SEMANTIC_HASH_MISMATCH",
    }
    diagnostic_status = "PASS" if (
        all_identity_match
        and exact_matches == total
        and not captured_hash_mismatch
    ) else "FAIL"
    source_snapshot = profile["loader_snapshot"]
    savedvariables_receipt = source_snapshot.get("savedvariables")
    savedvariables_map = (
        savedvariables_receipt
        if isinstance(savedvariables_receipt, Mapping)
        else {}
    )
    source_mtime = _first_value(
        source_snapshot, "source_mtime_ns", "mtime_ns"
    ) or _first_value(savedvariables_map, "mtime_ns")
    source_size = _first_value(
        source_snapshot, "source_size_bytes", "size_bytes"
    ) or _first_value(savedvariables_map, "size_bytes")

    rows_output = Path(rows_output_path).expanduser().resolve()
    rows_text = "".join(
        json.dumps(
            row, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        + "\n"
        for row in row_reports
    )
    report = {
        "schema": REPORT_SCHEMA,
        "status": "BOUNDED_DIAGNOSTIC_ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "transition_dataset": str(transitions),
            "transition_dataset_sha256": transition_hashes["dataset_sha256"],
            "transition_manifest": str(transition_manifest),
            "transition_manifest_sha256": transition_hashes["manifest_sha256"],
            "transition_manifest_commit": "PASS",
            "export_session_id": manifest.get("export_session_id"),
            "cat2_savedvariables": str(cat2_savedvariables),
            "cat2_installed_root": str(cat2_installed),
            "brainofcat_root": str(brainofcat),
            "cat2_savedvariables_sha256": profile["source_sha256"],
            "cat2_savedvariables_size_bytes": source_size,
            "cat2_savedvariables_mtime_ns": source_mtime,
        },
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_name": profile["profile_name"],
            "active_profile_id": profile["active_profile_id"],
            "profile_order": profile["profile_order"],
            "ordered_enabled_card_ids": [
                step["card_id"] for step in profile["steps"] if step["enabled"]
            ],
            "execution_semantic_sha256": profile["semantic_sha256"],
            "source_bundle_sha256": profile["source_bundle_sha256"],
            "authority_state": source_snapshot.get("authority_state"),
            "shape_gate": "PASS",
            "all_transition_profile_id_name_match": all_identity_match,
            "transition_profile_id_name_match_rows": profile_identity_matches,
            "capture_time_binding": capture_binding,
            "capture_time_profile_hash_rows": captured_hash_rows,
            "capture_time_profile_hash_present": captured_hash_rows == total,
        },
        "evidence_boundary": {
            "runtime_label_provenance": "OBSERVED_EXACT_CAT2_SINK_TRACE",
            "replay_provenance": "SOURCE_DERIVED",
            "lua_executed_offline": False,
            "source_derived_is_exact_runtime": False,
            "sample_design": "CLIENT_ACCEPTED_TERMINAL_MAPPED_ACTION_CONDITIONED",
            "no_action_or_wait_rows_available": 0,
            "decision_denominator_available": False,
            "candidate_counterfactual_reward_available": False,
            "performance_comparison_available": False,
        },
        "strict_conformance": {
            "status": "NOT_EVALUABLE",
            "total_rows": total,
            "input_complete_rows": 0,
            "missing_pre_action_fields_in_every_row": list(
                STRICT_MISSING_STATE_FIELDS
            ),
            "exact_decision_agreement_rate": None,
            "precision": None,
            "recall": None,
        },
        "assumption_conditioned_replay": {
            "status": diagnostic_status,
            "assumptions": dict(DIAGNOSTIC_ASSUMPTIONS),
            "evaluated_rows": total,
            "exact_factorized_action_match_rows": exact_matches,
            "exact_factorized_action_match_rate": exact_matches / total,
            "mismatch_rows": total - exact_matches,
            "actual_action_counts": dict(sorted(family_counts.items())),
            "actual_action_id_counts": dict(sorted(action_id_counts.items())),
            "actual_lane_counts": dict(sorted(lane_counts.items())),
            "whirlwind_ready_boundary_sensitive_rows": boundary_sensitive,
            "claim": (
                "positive action-branch replay under named assumptions; not full "
                "decision-policy or runtime conformance"
            ),
        },
        "eligibility": {
            "cat2_profile_identity_as_baseline": (
                all_identity_match and not captured_hash_mismatch
            ),
            "independent_expert_vote_allowed": False,
            "offline_rl_episode_eligible": False,
            "performance_claim_allowed": False,
            "deployment_allowed": False,
        },
        "output": {
            "rows": str(rows_output),
            "rows_sha256": _sha256(rows_text.encode("utf-8")),
            "row_count": len(row_reports),
            "row_schema": "fury_cat2_profile_conformance_row/v1",
        },
        "commit": {
            "state": "complete",
            "report_written_last": True,
            "content_addressed": True,
        },
    }
    return report, row_reports


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


def materialize_fury_cat2_profile_conformance(
    transition_path: str | Path = DEFAULT_TRANSITIONS,
    transition_manifest_path: str | Path = DEFAULT_TRANSITION_MANIFEST,
    cat2_savedvariables_path: str | Path = DEFAULT_CAT2_SAVEDVARIABLES,
    cat2_installed_root: str | Path = DEFAULT_CAT2_INSTALLED_ROOT,
    brainofcat_root: str | Path = DEFAULT_BRAIN_OF_CAT_ROOT,
    report_path: str | Path = DEFAULT_REPORT,
    rows_path: str | Path = DEFAULT_ROWS,
    *,
    profile_name: str = PROFILE_NAME,
    profile_loader: Callable[[Path, str, Path, Path], Any] | None = None,
) -> dict[str, Any]:
    report_output = Path(report_path).expanduser().resolve()
    rows_output = Path(rows_path).expanduser().resolve()
    protected = {
        Path(transition_path).expanduser().resolve(),
        Path(transition_manifest_path).expanduser().resolve(),
        Path(cat2_savedvariables_path).expanduser().resolve(),
        Path(cat2_installed_root).expanduser().resolve(),
        Path(brainofcat_root).expanduser().resolve(),
    }
    if (
        report_output == rows_output
        or report_output in protected
        or rows_output in protected
    ):
        raise FuryCat2ProfileConformanceError(
            "conformance outputs must be distinct and must not overwrite an input"
        )
    report, row_records = build_fury_cat2_profile_conformance(
        transition_path,
        transition_manifest_path,
        cat2_savedvariables_path,
        cat2_installed_root,
        brainofcat_root,
        profile_name=profile_name,
        profile_loader=profile_loader,
        rows_output_path=rows_output,
    )
    rows_text = "".join(
        json.dumps(
            row, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        + "\n"
        for row in row_records
    )
    rows_sha256 = _sha256(rows_text.encode("utf-8"))
    if report["output"]["rows_sha256"] != rows_sha256:
        raise FuryCat2ProfileConformanceError(
            "serialized conformance rows differ from the validated build"
        )
    report_text = json.dumps(
        report, ensure_ascii=False, allow_nan=False, indent=2
    ) + "\n"
    # The compact report is the commit marker and is replaced only after all
    # content-addressed rows have reached their destination.
    _atomic_write(rows_output, rows_text)
    _atomic_write(report_output, report_text)
    diagnostic = report["assumption_conditioned_replay"]
    return {
        "status": report["status"],
        "report": str(report_output),
        "report_sha256": _sha256(report_text.encode("utf-8")),
        "rows": str(rows_output),
        "rows_sha256": rows_sha256,
        "strict_conformance": report["strict_conformance"]["status"],
        "strict_input_complete_rows": report["strict_conformance"][
            "input_complete_rows"
        ],
        "assumption_conditioned_status": diagnostic["status"],
        "assumption_conditioned_match_rows": diagnostic[
            "exact_factorized_action_match_rows"
        ],
        "row_count": diagnostic["evaluated_rows"],
        "independent_expert_vote_allowed": False,
        "deployment_allowed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit current Cat2 profile against accepted Fury Shadow actions."
    )
    parser.add_argument("--transitions", default=str(DEFAULT_TRANSITIONS))
    parser.add_argument(
        "--transition-manifest", default=str(DEFAULT_TRANSITION_MANIFEST)
    )
    parser.add_argument(
        "--cat2-savedvariables", default=str(DEFAULT_CAT2_SAVEDVARIABLES)
    )
    parser.add_argument(
        "--cat2-installed-root", default=str(DEFAULT_CAT2_INSTALLED_ROOT)
    )
    parser.add_argument(
        "--brainofcat-root", default=str(DEFAULT_BRAIN_OF_CAT_ROOT)
    )
    parser.add_argument("--profile-name", default=PROFILE_NAME)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--rows", default=str(DEFAULT_ROWS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        receipt = materialize_fury_cat2_profile_conformance(
            arguments.transitions,
            arguments.transition_manifest,
            arguments.cat2_savedvariables,
            arguments.cat2_installed_root,
            arguments.brainofcat_root,
            arguments.report,
            arguments.rows,
            profile_name=arguments.profile_name,
        )
    except FuryCat2ProfileConformanceError as error:
        print(f"Cat2 conformance audit failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
