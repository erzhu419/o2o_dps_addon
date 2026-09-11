"""Compile prefix-causal historical Warrior action generators.

This layer consumes only ``player_wave_episode`` rows emitted by
``chronicle_team_wave_model_v1``.  It fits separate smoothed backoff models for
observed Fury and Arms players, evaluates them with whole
guild/player/instance connected components held out, and emits immutable,
content-addressed model and evaluation documents.

The Fury lane is a future voting-baseline candidate; the Arms lane is always a
cross-spec diagnostic.  This compiler never promotes either lane into a live
comparison.  Even a passing internal fidelity gate still requires a separate
simulator/runtime admission gate.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any

from .chronicle_team_wave_model_v1 import (
    SCHEMA as TEAM_MODEL_SCHEMA,
    validate_team_wave_model_manifest,
)


JSONMap = dict[str, Any]
SCHEMA = "chronicle_historical_warrior_policy/v1"
SCHEMA_VERSION = 1
IMPLEMENTATION_REVISION = "v1.1_component_boundary_nontraining_prefix_backoff"
MODEL_KIND = "chronicle_historical_warrior_policy_model"
EVALUATION_KIND = "chronicle_historical_warrior_policy_evaluation"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_team_wave_model"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "behavior_models"
    / "chronicle_historical_warrior_policy"
    / "v1"
)

FURY_LANE = "WARRIOR_FURY"
ARMS_LANE = "WARRIOR_ARMS"
LANES = (FURY_LANE, ARMS_LANE)
TRAINING_CONTAMINATION_STATUSES = frozenset(
    ("POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH")
)
NONVOTING_CONTAMINATION_STATUSES = frozenset(
    (
        "SUSPECT_36YD_RANGE_BUG",
        "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
        "UNKNOWN_NONVOTING",
    )
)
ALL_CONTAMINATION_STATUSES = (
    "NO_KNOWN_RULE_MATCH",
    "POSTFIX_KNOWN_CLEAN",
    "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
    "SUSPECT_36YD_RANGE_BUG",
    "UNKNOWN_NONVOTING",
)

DEFAULT_FOLD_COUNT = 5
DEFAULT_SPLIT_SEED = 20260911
DEFAULT_SMOOTHING_ALPHA = 0.5
DEFAULT_BACKOFF_STRENGTH = 8.0
START_CAST_PAIR_MAX_MS = 10_000
CONTEXT_LEVELS = ("coarse", "tactical", "full")
UNKNOWN_ACTION_KEY = "__HELDOUT_UNKNOWN_ACTION__"

# These are preregistered sufficiency/fidelity thresholds, not tuned on the
# synthetic tests.  A separate downstream gate is still required after PASS.
FURY_FIDELITY_THRESHOLDS = {
    "minimum_training_components": 20,
    "minimum_heldout_components": 20,
    "minimum_heldout_decisions": 1000,
    "minimum_known_action_coverage": 0.90,
    "minimum_top1_accuracy": 0.45,
    "minimum_top3_accuracy": 0.80,
    "minimum_contextual_log_loss_improvement": 0.0,
    "maximum_expected_calibration_error": 0.20,
}

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_FORBIDDEN_STATE_KEYS = frozenset(
    (
        "death_clock",
        "target_summaries",
        "wave_summary",
        "team_damage",
        "final_totals",
        "final_target_count",
        "future_events",
        "future_teammate_actions",
        "wave_end_ms",
        "wave_duration_ms",
        "remaining_wave_ms",
        "next_action",
        "next_event",
    )
)


class HistoricalWarriorPolicyError(RuntimeError):
    """Input evidence or generated policy violates the frozen v1 contract."""


@dataclass(frozen=True)
class Decision:
    lane: str
    component_id: str
    episode_id: str
    contamination_status: str
    context_keys: tuple[tuple[str, str], ...]
    action_key: str

    def contexts(self) -> dict[str, str]:
        return dict(self.context_keys)


@dataclass(frozen=True)
class HistoricalWarriorPolicyResult:
    model: Path
    content_addressed_model: Path
    evaluation: Path
    content_addressed_evaluation: Path
    comparison_status: str
    fury_decision_count: int
    arms_decision_count: int

    def as_dict(self) -> JSONMap:
        return {
            "status": "ok",
            "schema": SCHEMA,
            "model": str(self.model),
            "content_addressed_model": str(self.content_addressed_model),
            "evaluation": str(self.evaluation),
            "content_addressed_evaluation": str(
                self.content_addressed_evaluation
            ),
            "comparison_status": self.comparison_status,
            "fourth_baseline_ready": False,
            "fury_decision_count": self.fury_decision_count,
            "arms_decision_count": self.arms_decision_count,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise HistoricalWarriorPolicyError(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalWarriorPolicyError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalWarriorPolicyError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalWarriorPolicyError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalWarriorPolicyError(f"{label} must be nonempty text")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip()
    return rendered or None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalWarriorPolicyError(f"{label} must be an integer")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalWarriorPolicyError(f"{label} must be numeric")
    rendered = float(value)
    if not math.isfinite(rendered) or rendered < 0:
        raise HistoricalWarriorPolicyError(f"{label} must be finite and nonnegative")
    return rendered


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HistoricalWarriorPolicyError(f"{label} must be lowercase SHA-256")
    return value


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalWarriorPolicyError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise HistoricalWarriorPolicyError(f"{label} must be a JSON object")
    return value


def _verify_content_address(document: Mapping[str, Any], label: str) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    declared = _sha(address.get("sha256"), f"{label}.content_address.sha256")
    core = {key: value for key, value in document.items() if key != "content_address"}
    actual = _sha256_json(core)
    if actual != declared:
        raise HistoricalWarriorPolicyError(
            f"{label} content address mismatch: expected {declared}, got {actual}"
        )
    return actual


def _resolve_partition(manifest_path: Path, raw: Any, label: str) -> Path:
    relative = Path(_text(raw, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalWarriorPolicyError(f"{label} must be a safe relative path")
    base = manifest_path.parent.resolve()
    try:
        resolved = (base / relative).resolve(strict=True)
    except OSError as error:
        raise HistoricalWarriorPolicyError(f"cannot resolve {label}: {error}") from error
    if not resolved.is_relative_to(base) or not resolved.is_file() or resolved.is_symlink():
        raise HistoricalWarriorPolicyError(f"{label} escapes or is not a regular file")
    return resolved


def _assert_no_future_keys(value: Any, path: str = "state_before") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if (
                key in _FORBIDDEN_STATE_KEYS
                or lowered.startswith(("future_", "final_", "next_"))
                or "death_clock" in lowered
                or lowered.endswith("_after_wave")
            ):
                raise HistoricalWarriorPolicyError(
                    f"forbidden future/outcome feature {path}.{key}"
                )
            _assert_no_future_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_future_keys(child, f"{path}[{index}]")


def _list_of_text(value: Any, label: str, *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    values = _array(value, label)
    output: list[str] = []
    for index, raw in enumerate(values):
        output.append(_text(raw, f"{label}[{index}]"))
    return output


def _log_bucket(value: float) -> str:
    if value <= 0:
        return "ZERO"
    return f"LOG2_{min(30, int(math.log2(value + 1)))}"


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "ZERO"
    if value == 1:
        return "ONE"
    if value <= 3:
        return "TWO_TO_THREE"
    if value <= 7:
        return "FOUR_TO_SEVEN"
    return "EIGHT_PLUS"


def _elapsed_bucket(value: float) -> str:
    return f"TWO_SECOND_{min(60, int(value // 2000))}"


def _spell_key(spell: Mapping[str, Any]) -> str:
    spell_id = spell.get("id")
    if isinstance(spell_id, int) and not isinstance(spell_id, bool) and spell_id > 0:
        return f"id:{spell_id}"
    name = _optional_text(spell.get("name"))
    if name is None:
        raise HistoricalWarriorPolicyError("action spell needs a positive id or name")
    return f"name:{name.casefold()}"


def _target_role(state: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    guid = _optional_text(target.get("guid"))
    if guid is None:
        return "NO_TARGET"
    key = guid.casefold()
    dead = {value.casefold() for value in _list_of_text(
        state.get("observed_dead_target_guids"),
        "state_before.observed_dead_target_guids",
        required=False,
    )}
    alive = {value.casefold() for value in _list_of_text(
        state.get("alive_seen_target_guids"),
        "state_before.alive_seen_target_guids",
        required=False,
    )}
    seen = {value.casefold() for value in _list_of_text(
        state.get("seen_target_guids"),
        "state_before.seen_target_guids",
        required=False,
    )}
    last = _optional_text(state.get("actor_last_observed_target_guid"))
    if key in dead:
        return "OBSERVED_DEAD_TARGET"
    if last is not None and key == last.casefold():
        return "ACTOR_LAST_OBSERVED_TARGET"
    if key in alive:
        return "ALIVE_SEEN_TARGET"
    if key in seen:
        return "SEEN_NOT_CONFIRMED_ALIVE"
    return "UNSEEN_OR_EXTERNAL_TARGET"


def _action_key(state: Mapping[str, Any], action: Mapping[str, Any]) -> str:
    spell = _mapping(action.get("spell"), "action.spell")
    target_raw = action.get("target")
    target = {} if target_raw is None else _mapping(target_raw, "action.target")
    return _canonical_bytes(
        {"spell_key": _spell_key(spell), "target_role": _target_role(state, target)}
    ).decode("utf-8")


def _last_prefix_action(state: Mapping[str, Any]) -> tuple[str, str]:
    recent = state.get("actor_recent_observed_events")
    if not isinstance(recent, list):
        return "NONE", "NONE"
    for raw in reversed(recent):
        if not isinstance(raw, Mapping):
            continue
        event_type = _optional_text(raw.get("event_type"))
        spell = raw.get("spell")
        if event_type not in ("START", "CAST", "FAIL") or not isinstance(spell, Mapping):
            continue
        try:
            return event_type, _spell_key(spell)
        except HistoricalWarriorPolicyError:
            continue
    return "NONE", "NONE"


def _state_contexts(state: Mapping[str, Any]) -> dict[str, str]:
    _assert_no_future_keys(state)
    prefix_count_raw = state.get("prefix_event_count")
    prefix_count = (
        prefix_count_raw
        if isinstance(prefix_count_raw, int) and not isinstance(prefix_count_raw, bool)
        and prefix_count_raw >= 0
        else 0
    )
    elapsed_raw = state.get("wave_elapsed_ms")
    elapsed = (
        float(elapsed_raw)
        if isinstance(elapsed_raw, (int, float))
        and not isinstance(elapsed_raw, bool)
        and math.isfinite(float(elapsed_raw))
        and float(elapsed_raw) >= 0
        else 0.0
    )
    seen = _list_of_text(
        state.get("seen_target_guids"), "state_before.seen_target_guids", required=False
    )
    alive = _list_of_text(
        state.get("alive_seen_target_guids"),
        "state_before.alive_seen_target_guids",
        required=False,
    )
    dead = _list_of_text(
        state.get("observed_dead_target_guids"),
        "state_before.observed_dead_target_guids",
        required=False,
    )
    last_target = _optional_text(state.get("actor_last_observed_target_guid"))
    if last_target is None:
        last_target_role = "NONE"
    else:
        key = last_target.casefold()
        if key in {value.casefold() for value in dead}:
            last_target_role = "DEAD"
        elif key in {value.casefold() for value in alive}:
            last_target_role = "ALIVE"
        elif key in {value.casefold() for value in seen}:
            last_target_role = "SEEN"
        else:
            last_target_role = "EXTERNAL"
    background_raw = state.get("leave_one_player_out_background_before")
    background = background_raw if isinstance(background_raw, Mapping) else {}
    background_damage = background.get("included_background_damage", 0)
    background_dps = background.get("prefix_elapsed_average_background_dps", 0)
    unattributed = background.get("included_unattributed_damage", 0)
    background_damage_value = (
        float(background_damage)
        if isinstance(background_damage, (int, float))
        and not isinstance(background_damage, bool)
        and math.isfinite(float(background_damage))
        and float(background_damage) >= 0
        else 0.0
    )
    background_dps_value = (
        float(background_dps)
        if isinstance(background_dps, (int, float))
        and not isinstance(background_dps, bool)
        and math.isfinite(float(background_dps))
        and float(background_dps) >= 0
        else 0.0
    )
    unattributed_value = (
        float(unattributed)
        if isinstance(unattributed, (int, float))
        and not isinstance(unattributed, bool)
        and math.isfinite(float(unattributed))
        and float(unattributed) >= 0
        else 0.0
    )
    last_event_type, last_spell = _last_prefix_action(state)
    coarse = {
        "alive_count_bucket": _count_bucket(len(alive)),
        "dead_count_bucket": _count_bucket(len(dead)),
        "last_target_role": last_target_role,
    }
    tactical = {
        **coarse,
        "last_prefix_action_event_type": last_event_type,
        "last_prefix_action_spell": last_spell,
        "seen_count_bucket": _count_bucket(len(seen)),
    }
    full = {
        **tactical,
        "wave_elapsed_bucket": _elapsed_bucket(elapsed),
        "prefix_event_count_bucket": _count_bucket(prefix_count),
        "background_damage_bucket": _log_bucket(background_damage_value),
        "background_dps_bucket": _log_bucket(background_dps_value),
        "unattributed_damage_present": unattributed_value > 0,
    }
    return {
        "coarse": _canonical_bytes(coarse).decode("utf-8"),
        "tactical": _canonical_bytes(tactical).decode("utf-8"),
        "full": _canonical_bytes(full).decode("utf-8"),
    }


def _validate_training_transition(transition: Mapping[str, Any], label: str) -> None:
    if transition.get("feature_cutoff_is_strict_prefix") is not True:
        raise HistoricalWarriorPolicyError(f"{label} is not strict-prefix")
    if transition.get("future_outcomes_in_state_before") is not False:
        raise HistoricalWarriorPolicyError(f"{label} permits future outcomes")
    state = _mapping(transition.get("state_before"), f"{label}.state_before")
    observed = _mapping(transition.get("observed_event"), f"{label}.observed_event")
    cutoff = state.get("cutoff_exclusive_order_key")
    order = observed.get("order_key")
    if cutoff != order:
        raise HistoricalWarriorPolicyError(
            f"{label} cutoff_exclusive_order_key does not equal current event order_key"
        )
    if state.get("cutoff_semantics") != "strictly before current event order_key":
        raise HistoricalWarriorPolicyError(f"{label} cutoff semantics were weakened")
    _assert_no_future_keys(state, f"{label}.state_before")
    declared_sha = _sha(
        transition.get("transition_sha256"), f"{label}.transition_sha256"
    )
    core = {
        key: value for key, value in transition.items() if key != "transition_sha256"
    }
    if _sha256_json(core) != declared_sha:
        raise HistoricalWarriorPolicyError(f"{label} transition content hash mismatch")


def _component_index(manifest: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    split = _mapping(manifest.get("split_graph"), "manifest.split_graph")
    index: dict[str, str] = {}
    for row_index, raw in enumerate(
        _array(split.get("node_to_component"), "manifest.split_graph.node_to_component")
    ):
        row = _mapping(raw, f"manifest.split_graph.node_to_component[{row_index}]")
        node = _text(row.get("node_id"), f"node_to_component[{row_index}].node_id")
        component = _sha(
            row.get("component_id"), f"node_to_component[{row_index}].component_id"
        )
        if node in index and index[node] != component:
            raise HistoricalWarriorPolicyError(f"split node {node} has two components")
        index[node] = component
    components: list[str] = []
    for row_index, raw in enumerate(
        _array(split.get("connected_components"), "manifest.split_graph.connected_components")
    ):
        row = _mapping(raw, f"connected_components[{row_index}]")
        component = _sha(row.get("component_id"), f"components[{row_index}].component_id")
        nodes = _list_of_text(row.get("node_ids"), f"components[{row_index}].node_ids", required=True)
        if component in components:
            raise HistoricalWarriorPolicyError(f"duplicate component {component}")
        if any(index.get(node) != component for node in nodes):
            raise HistoricalWarriorPolicyError(f"component {component} node mapping disagrees")
        components.append(component)
    if not components or set(index.values()) != set(components):
        raise HistoricalWarriorPolicyError("split component index is incomplete")
    return index, sorted(components)


def _episode_component(
    episode: Mapping[str, Any], node_index: Mapping[str, str], label: str
) -> str:
    membership = _mapping(episode.get("component_membership"), f"{label}.component_membership")
    if membership.get("row_random_split_allowed") is not False:
        raise HistoricalWarriorPolicyError(f"{label} permits row-random splitting")
    required = _text(
        membership.get("required_split_unit"),
        f"{label}.component_membership.required_split_unit",
    )
    if "connected component" not in required:
        raise HistoricalWarriorPolicyError(f"{label} weakened component split unit")
    nodes = [
        _text(membership.get("instance_node_id"), f"{label}.instance_node_id"),
        _text(membership.get("player_node_id"), f"{label}.player_node_id"),
        *_list_of_text(
            membership.get("guild_node_ids"), f"{label}.guild_node_ids", required=True
        ),
    ]
    components = {node_index.get(node) for node in nodes}
    if None in components or len(components) != 1:
        raise HistoricalWarriorPolicyError(
            f"{label} instance/guild/player nodes do not share exactly one component"
        )
    return str(next(iter(components)))


def _action_identity(action: Mapping[str, Any]) -> tuple[str, str]:
    spell = _mapping(action.get("spell"), "action.spell")
    target_raw = action.get("target")
    target = target_raw if isinstance(target_raw, Mapping) else {}
    return _spell_key(spell), (_optional_text(target.get("guid")) or "").casefold()


def _episode_decisions(
    episode: Mapping[str, Any], *, lane: str, component_id: str, label: str
) -> tuple[list[Decision], Counter[str]]:
    episode_id = _text(episode.get("episode_id"), f"{label}.episode_id")
    eligibility = _mapping(episode.get("eligibility"), f"{label}.eligibility")
    contamination = _text(
        eligibility.get("contamination_status"), f"{label}.contamination_status"
    )
    transitions = _array(episode.get("prefix_transitions"), f"{label}.prefix_transitions")
    decisions: list[Decision] = []
    audit: Counter[str] = Counter()
    pending_starts: dict[tuple[str, str], int] = {}
    last_order: tuple[int, ...] | None = None
    for index, raw_transition in enumerate(transitions):
        transition = _mapping(raw_transition, f"{label}.prefix_transitions[{index}]")
        transition_label = f"{label}.prefix_transitions[{index}]"
        _validate_training_transition(transition, transition_label)
        observed = _mapping(transition.get("observed_event"), f"{transition_label}.observed_event")
        raw_order = _array(observed.get("order_key"), f"{transition_label}.order_key")
        order = tuple(_integer(value, f"{transition_label}.order_key") for value in raw_order)
        if last_order is not None and order <= last_order:
            raise HistoricalWarriorPolicyError(f"{label} transition order is not strictly increasing")
        last_order = order
        action_raw = transition.get("action_observation")
        if action_raw is None:
            continue
        action = _mapping(action_raw, f"{transition_label}.action_observation")
        event_type = _text(action.get("event_type"), f"{transition_label}.action.event_type")
        if event_type != observed.get("event_type"):
            raise HistoricalWarriorPolicyError(f"{transition_label} action/observed event disagree")
        if action.get("spell") != observed.get("spell") or action.get("target") != observed.get("target"):
            raise HistoricalWarriorPolicyError(f"{transition_label} action label payload disagrees")
        action_identity = _action_identity(action)
        wave_offset = int(
            _nonnegative_number(observed.get("wave_offset_ms"), f"{transition_label}.wave_offset_ms")
        )
        pending_starts = {
            key: start
            for key, start in pending_starts.items()
            if wave_offset - start <= START_CAST_PAIR_MAX_MS
        }
        if event_type == "FAIL":
            pending_starts.pop(action_identity, None)
            audit["failure_outcomes_not_action_labels"] += 1
            continue
        if event_type == "CAST" and action_identity in pending_starts:
            start = pending_starts.pop(action_identity)
            if 0 <= wave_offset - start <= START_CAST_PAIR_MAX_MS:
                audit["paired_casts_deduplicated_from_prior_start"] += 1
                continue
        if event_type not in ("START", "CAST"):
            raise HistoricalWarriorPolicyError(
                f"{transition_label} unsupported action event type {event_type}"
            )
        state = _mapping(transition.get("state_before"), f"{transition_label}.state_before")
        action_key = _action_key(state, action)
        if json.loads(action_key)["target_role"] == "OBSERVED_DEAD_TARGET":
            audit["labels_targeting_prefix_observed_dead_target_rejected"] += 1
            continue
        if event_type == "START":
            pending_starts[action_identity] = wave_offset
            audit["start_action_labels"] += 1
        else:
            audit["instant_or_unpaired_cast_action_labels"] += 1
        contexts = _state_contexts(state)
        decisions.append(
            Decision(
                lane=lane,
                component_id=component_id,
                episode_id=episode_id,
                contamination_status=contamination,
                context_keys=tuple(sorted(contexts.items())),
                action_key=action_key,
            )
        )
    audit["accepted_action_labels"] = len(decisions)
    return decisions, audit


def _lane_and_eligibility(episode: Mapping[str, Any], label: str) -> tuple[str | None, bool, str]:
    player = _mapping(episode.get("player"), f"{label}.player")
    if player.get("hero_class") != "WARRIOR":
        return None, False, "NOT_OBSERVED_WARRIOR"
    specialization = _mapping(player.get("specialization"), f"{label}.specialization")
    lane = specialization.get("partition_key")
    if lane not in LANES or specialization.get("status") != "OBSERVED_SINGLE":
        return None, False, "NOT_SINGLE_OBSERVED_FURY_OR_ARMS"
    eligibility = _mapping(episode.get("eligibility"), f"{label}.eligibility")
    contamination = _text(
        eligibility.get("contamination_status"), f"{label}.contamination_status"
    )
    if contamination not in ALL_CONTAMINATION_STATUSES:
        raise HistoricalWarriorPolicyError(f"{label} unsupported contamination status")
    if contamination not in TRAINING_CONTAMINATION_STATUSES:
        return str(lane), False, f"CONTAMINATION_{contamination}"
    if lane == FURY_LANE:
        eligible = eligibility.get("historical_fury_policy_training_eligible") is True
        return str(lane), eligible, "ELIGIBLE" if eligible else "UPSTREAM_FURY_INELIGIBLE"
    eligible = eligibility.get("team_behavior_training_eligible") is True
    return str(lane), eligible, "ELIGIBLE" if eligible else "UPSTREAM_ARMS_INELIGIBLE"


def _read_decisions(
    manifest_path: Path,
) -> tuple[JSONMap, dict[str, list[Decision]], JSONMap, list[str]]:
    manifest = _load_json(manifest_path, "team-wave model manifest")
    try:
        validate_team_wave_model_manifest(manifest)
    except Exception as error:
        raise HistoricalWarriorPolicyError(f"invalid team-wave model manifest: {error}") from error
    content_sha = _verify_content_address(manifest, "team-wave model manifest")
    node_index, components = _component_index(manifest)
    decisions: dict[str, list[Decision]] = {lane: [] for lane in LANES}
    accounting: JSONMap = {
        "episode_counts_by_lane": {lane: Counter() for lane in LANES},
        "excluded_episode_reasons_by_lane": {lane: Counter() for lane in LANES},
        "decision_counts_by_lane": {lane: Counter() for lane in LANES},
        "action_label_audit_by_lane": {lane: Counter() for lane in LANES},
        "ignored_episode_reasons": Counter(),
    }
    for partition_index, raw_partition in enumerate(
        _array(manifest.get("partitions"), "manifest.partitions")
    ):
        partition = _mapping(raw_partition, f"manifest.partitions[{partition_index}]")
        path = _resolve_partition(
            manifest_path, partition.get("partition"), f"partitions[{partition_index}].partition"
        )
        declared_size = _integer(
            partition.get("compressed_size_bytes"), f"partitions[{partition_index}].compressed_size_bytes"
        )
        if path.stat().st_size != declared_size:
            raise HistoricalWarriorPolicyError(f"partition size mismatch: {path}")
        declared_file_sha = _sha(
            partition.get("compressed_file_sha256"), f"partitions[{partition_index}].compressed_file_sha256"
        )
        if _sha256_file(path) != declared_file_sha:
            raise HistoricalWarriorPolicyError(f"partition compressed SHA-256 mismatch: {path}")
        logical_digest = hashlib.sha256()
        record_count = 0
        episode_count = 0
        try:
            with gzip.open(path, "rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    if not raw_line.strip():
                        continue
                    logical_digest.update(raw_line)
                    record_count += 1
                    try:
                        row = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise HistoricalWarriorPolicyError(
                            f"invalid model JSON {path}:{line_number}: {error}"
                        ) from error
                    if not isinstance(row, dict) or row.get("schema") != TEAM_MODEL_SCHEMA:
                        raise HistoricalWarriorPolicyError(f"invalid model row {path}:{line_number}")
                    if raw_line != _canonical_bytes(row) + b"\n":
                        raise HistoricalWarriorPolicyError(
                            f"noncanonical model row {path}:{line_number}"
                        )
                    if row.get("record_type") != "player_wave_episode":
                        continue
                    episode_count += 1
                    label = f"{path.name}:{line_number}"
                    component = _episode_component(row, node_index, label)
                    lane, eligible, reason = _lane_and_eligibility(row, label)
                    if lane is None:
                        accounting["ignored_episode_reasons"][reason] += 1
                        continue
                    eligibility = _mapping(row.get("eligibility"), f"{label}.eligibility")
                    contamination = _text(
                        eligibility.get("contamination_status"), f"{label}.contamination_status"
                    )
                    accounting["episode_counts_by_lane"][lane][contamination] += 1
                    if not eligible:
                        accounting["excluded_episode_reasons_by_lane"][lane][reason] += 1
                        continue
                    episode_decisions, audit = _episode_decisions(
                        row, lane=lane, component_id=component, label=label
                    )
                    decisions[lane].extend(episode_decisions)
                    accounting["decision_counts_by_lane"][lane][contamination] += len(
                        episode_decisions
                    )
                    accounting["action_label_audit_by_lane"][lane].update(audit)
        except HistoricalWarriorPolicyError:
            raise
        except OSError as error:
            raise HistoricalWarriorPolicyError(f"cannot read partition {path}: {error}") from error
        if record_count != _integer(
            partition.get("record_count"), f"partitions[{partition_index}].record_count"
        ):
            raise HistoricalWarriorPolicyError(f"partition record count mismatch: {path}")
        if episode_count != _integer(
            partition.get("player_wave_episode_count"),
            f"partitions[{partition_index}].player_wave_episode_count",
        ):
            raise HistoricalWarriorPolicyError(f"partition episode count mismatch: {path}")
        declared_logical = _sha(
            partition.get("logical_content_sha256"), f"partitions[{partition_index}].logical_content_sha256"
        )
        if logical_digest.hexdigest() != declared_logical:
            raise HistoricalWarriorPolicyError(f"partition logical SHA-256 mismatch: {path}")
    rendered_accounting = {
        outer: {
            lane: dict(sorted(counter.items()))
            for lane, counter in lane_counters.items()
        }
        if outer != "ignored_episode_reasons"
        else dict(sorted(lane_counters.items()))
        for outer, lane_counters in accounting.items()
    }
    input_identity = {
        "team_wave_model_manifest_path": _portable_path(manifest_path),
        "team_wave_model_manifest_file_sha256": _sha256_file(manifest_path),
        "team_wave_model_manifest_content_sha256": content_sha,
        "team_wave_model_schema": TEAM_MODEL_SCHEMA,
    }
    return input_identity, decisions, rendered_accounting, components


def _portable_path(path: Path) -> str:
    try:
        return "$PROJECT_ROOT/" + path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return "$EXTERNAL/" + path.name


def _sorted_counter(counter: Mapping[str, int]) -> JSONMap:
    return {key: int(counter[key]) for key in sorted(counter)}


def _fit_lane(
    decisions: Sequence[Decision], *, alpha: float, backoff_strength: float
) -> JSONMap:
    if alpha <= 0 or not math.isfinite(alpha):
        raise HistoricalWarriorPolicyError("smoothing alpha must be finite and positive")
    if backoff_strength <= 0 or not math.isfinite(backoff_strength):
        raise HistoricalWarriorPolicyError(
            "backoff strength must be finite and positive"
        )
    global_counts: Counter[str] = Counter()
    context_counts: dict[str, dict[str, Counter[str]]] = {
        level: defaultdict(Counter) for level in CONTEXT_LEVELS
    }
    for decision in decisions:
        global_counts[decision.action_key] += 1
        contexts = decision.contexts()
        for level in CONTEXT_LEVELS:
            context_counts[level][contexts[level]][decision.action_key] += 1
    return {
        "decision_count": len(decisions),
        "component_count": len({decision.component_id for decision in decisions}),
        "action_catalog": sorted(global_counts),
        "global_action_counts": _sorted_counter(global_counts),
        "context_action_counts": {
            level: {
                context: _sorted_counter(counts)
                for context, counts in sorted(context_counts[level].items())
            }
            for level in CONTEXT_LEVELS
        },
        "smoothing": {
            "method": "hierarchical_convex_backoff_with_laplace_local_posteriors",
            "alpha": alpha,
            "backoff_strength": backoff_strength,
            "level_order": list(CONTEXT_LEVELS),
        },
    }


def _normalized_distribution(values: Mapping[str, float]) -> dict[str, float]:
    total = sum(values.values())
    if not math.isfinite(total) or total <= 0:
        raise HistoricalWarriorPolicyError("cannot normalize an empty distribution")
    return {key: float(values[key] / total) for key in sorted(values)}


def _policy_distribution(
    fitted: Mapping[str, Any],
    contexts: Mapping[str, str],
    *,
    evaluation_unknown_bucket: bool,
) -> tuple[dict[str, float], list[str]]:
    catalog = _list_of_text(
        fitted.get("action_catalog"), "fitted.action_catalog", required=False
    )
    if evaluation_unknown_bucket and UNKNOWN_ACTION_KEY not in catalog:
        catalog.append(UNKNOWN_ACTION_KEY)
    catalog = sorted(set(catalog))
    if not catalog:
        raise HistoricalWarriorPolicyError("policy lane has no action catalog")
    smoothing = _mapping(fitted.get("smoothing"), "fitted.smoothing")
    alpha = _nonnegative_number(smoothing.get("alpha"), "smoothing.alpha")
    strength = _nonnegative_number(
        smoothing.get("backoff_strength"), "smoothing.backoff_strength"
    )
    if alpha <= 0 or strength <= 0:
        raise HistoricalWarriorPolicyError("policy smoothing values must be positive")
    global_raw = _mapping(
        fitted.get("global_action_counts"), "fitted.global_action_counts"
    )
    global_counts = {
        action: _nonnegative_number(global_raw.get(action, 0), "global action count")
        for action in catalog
    }
    global_total = sum(global_counts.values())
    probabilities = {
        action: (global_counts[action] + alpha)
        / (global_total + alpha * len(catalog))
        for action in catalog
    }
    matched: list[str] = []
    all_context_counts = _mapping(
        fitted.get("context_action_counts"), "fitted.context_action_counts"
    )
    for level in CONTEXT_LEVELS:
        level_tables = _mapping(all_context_counts.get(level), f"contexts.{level}")
        context = contexts.get(level)
        raw_counts = level_tables.get(context) if context is not None else None
        if not isinstance(raw_counts, Mapping):
            continue
        local_counts = {
            action: _nonnegative_number(raw_counts.get(action, 0), "context action count")
            for action in catalog
        }
        local_total = sum(local_counts.values())
        if local_total <= 0:
            continue
        local = {
            action: (local_counts[action] + alpha)
            / (local_total + alpha * len(catalog))
            for action in catalog
        }
        weight = local_total / (local_total + strength)
        probabilities = {
            action: weight * local[action] + (1.0 - weight) * probabilities[action]
            for action in catalog
        }
        matched.append(level)
    return _normalized_distribution(probabilities), matched


def _component_folds(
    components: Sequence[str], *, fold_count: int, split_seed: int
) -> tuple[int, dict[str, int]]:
    unique = sorted(set(components))
    if not unique:
        raise HistoricalWarriorPolicyError("team model has no connected components")
    if isinstance(fold_count, bool) or not isinstance(fold_count, int) or fold_count < 2:
        raise HistoricalWarriorPolicyError("fold_count must be an integer of at least two")
    effective = min(fold_count, len(unique))
    ranked = sorted(
        unique,
        key=lambda component: (
            hashlib.sha256(
                f"{split_seed}|{component}".encode("utf-8")
            ).hexdigest(),
            component,
        ),
    )
    return effective, {
        component: index % effective for index, component in enumerate(ranked)
    }


def _ece_bins(rows: Sequence[tuple[float, bool]], bin_count: int = 10) -> JSONMap:
    bins: list[JSONMap] = []
    total = len(rows)
    weighted_gap = 0.0
    for bin_index in range(bin_count):
        lower = bin_index / bin_count
        upper = (bin_index + 1) / bin_count
        selected = [
            (confidence, correct)
            for confidence, correct in rows
            if confidence >= lower
            and (confidence < upper or (bin_index == bin_count - 1 and confidence <= upper))
        ]
        count = len(selected)
        if count:
            mean_value = (
                sum(confidence for confidence, _ in selected) / count
            )
            accuracy_value = (
                sum(1 for _, correct in selected if correct) / count
            )
            mean_confidence: float | None = mean_value
            accuracy: float | None = accuracy_value
            gap: float | None = abs(mean_value - accuracy_value)
        else:
            mean_confidence = None
            accuracy = None
            gap = None
        if gap is not None and total:
            weighted_gap += count / total * gap
        bins.append(
            {
                "bin_index": bin_index,
                "lower_inclusive": lower,
                "upper_exclusive_except_last": upper,
                "count": count,
                "mean_confidence": mean_confidence,
                "accuracy": accuracy,
                "absolute_gap": gap,
            }
        )
    return {
        "method": "top_label_expected_calibration_error_10_equal_width_bins",
        "expected_calibration_error": weighted_gap if total else None,
        "bins": bins,
    }


def _empty_metrics() -> JSONMap:
    return {
        "decision_count": 0,
        "known_action_count": 0,
        "known_action_coverage": None,
        "top1_accuracy": None,
        "top3_accuracy": None,
        "contextual_log_loss": None,
        "marginal_log_loss": None,
        "contextual_log_loss_improvement": None,
        "context_match_counts": {
            "coarse": 0,
            "tactical": 0,
            "full": 0,
            "global_only": 0,
        },
        "calibration": _ece_bins([]),
    }


def _evaluate_lane(
    decisions: Sequence[Decision],
    *,
    components: Sequence[str],
    assignment: Mapping[str, int],
    effective_fold_count: int,
    alpha: float,
    backoff_strength: float,
) -> JSONMap:
    lane_components = sorted({decision.component_id for decision in decisions})
    totals: Counter[str] = Counter()
    log_loss = 0.0
    marginal_log_loss = 0.0
    calibration_rows: list[tuple[float, bool]] = []
    contamination: dict[str, Counter[str]] = defaultdict(Counter)
    folds: list[JSONMap] = []
    for fold_index in range(effective_fold_count):
        test_components = sorted(
            component for component in components if assignment[component] == fold_index
        )
        train_components = sorted(
            component for component in components if assignment[component] != fold_index
        )
        train = [
            decision
            for decision in decisions
            if assignment[decision.component_id] != fold_index
        ]
        test = [
            decision
            for decision in decisions
            if assignment[decision.component_id] == fold_index
        ]
        fold_record: JSONMap = {
            "fold_index": fold_index,
            "train_component_ids": train_components,
            "test_component_ids": test_components,
            "component_sets_disjoint": not bool(
                set(train_components).intersection(test_components)
            ),
            "training_decision_count": len(train),
            "heldout_decision_count": len(test),
            "evaluation_status": "EVALUATED",
        }
        if not train or not test:
            fold_record["evaluation_status"] = (
                "NO_TRAINING_DECISIONS" if not train else "NO_HELDOUT_DECISIONS"
            )
            folds.append(fold_record)
            continue
        fitted = _fit_lane(train, alpha=alpha, backoff_strength=backoff_strength)
        global_only = deepcopy(fitted)
        global_only["context_action_counts"] = {
            level: {} for level in CONTEXT_LEVELS
        }
        train_catalog = set(fitted["action_catalog"])
        fold_totals: Counter[str] = Counter()
        for decision in test:
            contexts = decision.contexts()
            probabilities, matched = _policy_distribution(
                fitted, contexts, evaluation_unknown_bucket=True
            )
            marginal, _ = _policy_distribution(
                global_only, contexts, evaluation_unknown_bucket=True
            )
            label = (
                decision.action_key
                if decision.action_key in train_catalog
                else UNKNOWN_ACTION_KEY
            )
            known = decision.action_key in train_catalog
            ranking = sorted(probabilities, key=lambda key: (-probabilities[key], key))
            predicted = ranking[0]
            confidence = probabilities[predicted]
            correct = predicted == label
            probability = max(probabilities[label], 1e-300)
            marginal_probability = max(marginal[label], 1e-300)
            current_log_loss = -math.log(probability)
            current_marginal_log_loss = -math.log(marginal_probability)
            totals["decision_count"] += 1
            fold_totals["decision_count"] += 1
            if known:
                totals["known_action_count"] += 1
                fold_totals["known_action_count"] += 1
            if correct:
                totals["top1_correct"] += 1
                fold_totals["top1_correct"] += 1
            if label in ranking[:3]:
                totals["top3_correct"] += 1
                fold_totals["top3_correct"] += 1
            deepest = matched[-1] if matched else "global_only"
            totals[f"context_{deepest}"] += 1
            fold_totals[f"context_{deepest}"] += 1
            log_loss += current_log_loss
            marginal_log_loss += current_marginal_log_loss
            calibration_rows.append((confidence, correct))
            contamination[decision.contamination_status]["decision_count"] += 1
            contamination[decision.contamination_status]["known_action_count"] += int(known)
            contamination[decision.contamination_status]["top1_correct"] += int(correct)
            contamination[decision.contamination_status]["top3_correct"] += int(
                label in ranking[:3]
            )
        fold_record["metrics"] = {
            "decision_count": fold_totals["decision_count"],
            "known_action_coverage": fold_totals["known_action_count"]
            / fold_totals["decision_count"],
            "top1_accuracy": fold_totals["top1_correct"]
            / fold_totals["decision_count"],
            "top3_accuracy": fold_totals["top3_correct"]
            / fold_totals["decision_count"],
        }
        folds.append(fold_record)
    count = totals["decision_count"]
    metrics = _empty_metrics()
    if count:
        metrics.update(
            {
                "decision_count": count,
                "known_action_count": totals["known_action_count"],
                "known_action_coverage": totals["known_action_count"] / count,
                "top1_accuracy": totals["top1_correct"] / count,
                "top3_accuracy": totals["top3_correct"] / count,
                "contextual_log_loss": log_loss / count,
                "marginal_log_loss": marginal_log_loss / count,
                "contextual_log_loss_improvement": (
                    marginal_log_loss - log_loss
                )
                / count,
                "context_match_counts": {
                    "coarse": totals["context_coarse"],
                    "tactical": totals["context_tactical"],
                    "full": totals["context_full"],
                    "global_only": totals["context_global_only"],
                },
                "calibration": _ece_bins(calibration_rows),
            }
        )
    by_status: JSONMap = {}
    for status, values in sorted(contamination.items()):
        status_count = values["decision_count"]
        by_status[status] = {
            "decision_count": status_count,
            "known_action_coverage": values["known_action_count"] / status_count,
            "top1_accuracy": values["top1_correct"] / status_count,
            "top3_accuracy": values["top3_correct"] / status_count,
        }
    return {
        "eligible_component_count": len(lane_components),
        "eligible_component_ids": lane_components,
        "heldout_component_count": len(
            {
                decision.component_id
                for decision in decisions
                if any(
                    fold.get("evaluation_status") == "EVALUATED"
                    and decision.component_id in fold["test_component_ids"]
                    for fold in folds
                )
            }
        ),
        "metrics": metrics,
        "metrics_by_contamination_status": by_status,
        "folds": folds,
    }


def _threshold_rejections(evaluation: Mapping[str, Any]) -> list[str]:
    metrics = _mapping(evaluation.get("metrics"), "lane evaluation metrics")
    calibration_value = _mapping(
        metrics.get("calibration"), "metrics.calibration"
    ).get("expected_calibration_error")
    checks = (
        (
            "INSUFFICIENT_TRAINING_COMPONENTS",
            float(evaluation.get("eligible_component_count", 0)),
            float(FURY_FIDELITY_THRESHOLDS["minimum_training_components"]),
            "minimum",
        ),
        (
            "INSUFFICIENT_HELDOUT_COMPONENTS",
            float(evaluation.get("heldout_component_count", 0)),
            float(FURY_FIDELITY_THRESHOLDS["minimum_heldout_components"]),
            "minimum",
        ),
        (
            "INSUFFICIENT_HELDOUT_DECISIONS",
            float(metrics.get("decision_count") or 0),
            float(FURY_FIDELITY_THRESHOLDS["minimum_heldout_decisions"]),
            "minimum",
        ),
        (
            "KNOWN_ACTION_COVERAGE_BELOW_THRESHOLD",
            float(metrics.get("known_action_coverage") or 0),
            float(FURY_FIDELITY_THRESHOLDS["minimum_known_action_coverage"]),
            "minimum",
        ),
        (
            "TOP1_ACCURACY_BELOW_THRESHOLD",
            float(metrics.get("top1_accuracy") or 0),
            float(FURY_FIDELITY_THRESHOLDS["minimum_top1_accuracy"]),
            "minimum",
        ),
        (
            "TOP3_ACCURACY_BELOW_THRESHOLD",
            float(metrics.get("top3_accuracy") or 0),
            float(FURY_FIDELITY_THRESHOLDS["minimum_top3_accuracy"]),
            "minimum",
        ),
        (
            "CONTEXTUAL_LOG_LOSS_NOT_BETTER_THAN_MARGINAL",
            float(metrics.get("contextual_log_loss_improvement") or 0),
            float(
                FURY_FIDELITY_THRESHOLDS[
                    "minimum_contextual_log_loss_improvement"
                ]
            ),
            "minimum",
        ),
        (
            "CALIBRATION_ERROR_ABOVE_THRESHOLD",
            float(calibration_value) if calibration_value is not None else 1.0,
            float(
                FURY_FIDELITY_THRESHOLDS[
                    "maximum_expected_calibration_error"
                ]
            ),
            "maximum",
        ),
    )
    rejected: list[str] = []
    for code, observed, threshold, direction in checks:
        failed = observed < threshold if direction == "minimum" else observed > threshold
        if failed:
            rejected.append(code)
    return rejected


def _with_content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        **deepcopy(dict(core)),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(core),
        },
    }


def _atomic_json_temporary(path: Path, value: Mapping[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_canonical_bytes(value) + b"\n")
    return temporary


def _commit_or_reuse(temporary: Path, final: Path) -> None:
    if final.exists():
        if not final.is_file() or _sha256_file(final) != _sha256_file(temporary):
            raise HistoricalWarriorPolicyError(
                f"refusing to overwrite non-identical addressed file {final}"
            )
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(final)


def validate_historical_warrior_policy_model(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != MODEL_KIND
    ):
        raise HistoricalWarriorPolicyError(f"policy model must use {SCHEMA}")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise HistoricalWarriorPolicyError(
            "policy model implementation revision is stale or unsupported"
        )
    _verify_content_address(document, "historical Warrior policy model")
    boundary = _mapping(document.get("claim_boundary"), "model.claim_boundary")
    if (
        boundary.get("comparison_status") != "NOT_COMPARISON_READY"
        or boundary.get("fourth_baseline_ready") is not False
        or boundary.get("simulator_runtime_admission_validated") is not False
        or boundary.get("arms_can_vote") is not False
        or boundary.get("exact_trace_replay_used") is not False
    ):
        raise HistoricalWarriorPolicyError("policy model claim boundary was widened")
    feature_contract = _mapping(
        document.get("feature_contract"), "model.feature_contract"
    )
    if (
        feature_contract.get("state_source") != "state_before only"
        or feature_contract.get("strict_prefix_required") is not True
        or feature_contract.get("future_outcome_features_allowed") is not False
        or feature_contract.get("named_player_feature_or_override_allowed") is not False
    ):
        raise HistoricalWarriorPolicyError("policy feature contract was weakened")
    lanes = _mapping(document.get("lanes"), "model.lanes")
    if set(lanes) != set(LANES):
        raise HistoricalWarriorPolicyError("policy model must keep Fury and Arms separate")
    if _mapping(lanes[FURY_LANE], "Fury lane").get("policy_role") != "VOTING_CANDIDATE_NONVOTING_UNTIL_ADMISSION":
        raise HistoricalWarriorPolicyError("Fury lane role is invalid")
    if _mapping(lanes[ARMS_LANE], "Arms lane").get("policy_role") != "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING":
        raise HistoricalWarriorPolicyError("Arms lane role is invalid")
    for lane in LANES:
        fitted = _mapping(_mapping(lanes[lane], lane).get("fitted_policy"), f"{lane}.fitted_policy")
        catalog = _list_of_text(fitted.get("action_catalog"), f"{lane}.action_catalog", required=False)
        if len(catalog) != len(set(catalog)):
            raise HistoricalWarriorPolicyError(f"{lane} action catalog has duplicates")


def validate_historical_warrior_policy_evaluation(
    document: Mapping[str, Any], *, model_content_sha256: str | None = None
) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != EVALUATION_KIND
    ):
        raise HistoricalWarriorPolicyError(f"policy evaluation must use {SCHEMA}")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise HistoricalWarriorPolicyError(
            "policy evaluation implementation revision is stale or unsupported"
        )
    _verify_content_address(document, "historical Warrior policy evaluation")
    declared_model = _sha(
        document.get("model_content_sha256"), "evaluation.model_content_sha256"
    )
    if model_content_sha256 is not None and declared_model != _sha(
        model_content_sha256, "expected model content SHA-256"
    ):
        raise HistoricalWarriorPolicyError("evaluation points to a different model")
    split = _mapping(document.get("split"), "evaluation.split")
    if (
        split.get("unit")
        != "upstream guild+player+instance connected component"
        or split.get("each_component_held_out_exactly_once") is not True
        or split.get("row_random_split") is not False
    ):
        raise HistoricalWarriorPolicyError("evaluation split contract was weakened")
    lanes = _mapping(document.get("lanes"), "evaluation.lanes")
    if set(lanes) != set(LANES):
        raise HistoricalWarriorPolicyError("evaluation must report both separate lanes")
    boundary = _mapping(document.get("claim_boundary"), "evaluation.claim_boundary")
    if (
        boundary.get("comparison_status") != "NOT_COMPARISON_READY"
        or boundary.get("fourth_baseline_ready") is not False
        or boundary.get("arms_lane")
        != "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING"
    ):
        raise HistoricalWarriorPolicyError("evaluation claim boundary was widened")


def build_chronicle_historical_warrior_policy(
    *,
    team_wave_model_manifest_path: str | Path = DEFAULT_INPUT_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    fold_count: int = DEFAULT_FOLD_COUNT,
    split_seed: int = DEFAULT_SPLIT_SEED,
    smoothing_alpha: float = DEFAULT_SMOOTHING_ALPHA,
    backoff_strength: float = DEFAULT_BACKOFF_STRENGTH,
) -> HistoricalWarriorPolicyResult:
    """Compile immutable, prefix-causal Fury and Arms action generators."""

    if isinstance(split_seed, bool) or not isinstance(split_seed, int):
        raise HistoricalWarriorPolicyError("split_seed must be an integer")
    manifest_path = Path(team_wave_model_manifest_path).expanduser().resolve()
    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_identity, decisions, accounting, components = _read_decisions(manifest_path)
    effective_folds, assignment = _component_folds(
        components, fold_count=fold_count, split_seed=split_seed
    )
    lane_evaluations = {
        lane: _evaluate_lane(
            decisions[lane],
            components=components,
            assignment=assignment,
            effective_fold_count=effective_folds,
            alpha=smoothing_alpha,
            backoff_strength=backoff_strength,
        )
        for lane in LANES
    }
    fury_rejections = _threshold_rejections(lane_evaluations[FURY_LANE])
    internal_status = (
        "INTERNAL_HELDOUT_FIDELITY_PASS"
        if not fury_rejections
        else "INTERNAL_HELDOUT_FIDELITY_FAIL"
    )
    comparison_rejections = list(fury_rejections)
    if fury_rejections:
        comparison_rejections.insert(0, "FULL_DATA_HELDOUT_FIDELITY_GATE_NOT_PASSED")
    comparison_rejections.append("DOWNSTREAM_SIMULATOR_RUNTIME_ADMISSION_NOT_VALIDATED")
    fitted_lanes: JSONMap = {}
    for lane in LANES:
        fitted = _fit_lane(
            decisions[lane],
            alpha=smoothing_alpha,
            backoff_strength=backoff_strength,
        )
        fitted_lanes[lane] = {
            "policy_role": (
                "VOTING_CANDIDATE_NONVOTING_UNTIL_ADMISSION"
                if lane == FURY_LANE
                else "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING"
            ),
            "can_vote_in_current_artifact": False,
            "fitted_policy": fitted,
        }
    model_core: JSONMap = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": MODEL_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "input": input_identity,
        "feature_contract": {
            "state_source": "state_before only",
            "strict_prefix_required": True,
            "current_event_is_label_not_feature": True,
            "future_outcome_features_allowed": False,
            "final_death_clock_or_wave_totals_allowed": False,
            "future_teammate_actions_allowed": False,
            "named_player_feature_or_override_allowed": False,
            "feature_levels": list(CONTEXT_LEVELS),
        },
        "action_contract": {
            "labels": "START plus instant or unpaired CAST; paired CAST deduplicated causally",
            "failed_casts_are_labels": False,
            "target_identity_feature": "prefix-relative target role only",
            "exact_trace_replay_used": False,
            "runtime_distribution_conditioned_on_caller_supplied_legal_actions": True,
            "novel_legal_action_floor": smoothing_alpha,
        },
        "training_contract": {
            "eligible_contamination_statuses": sorted(TRAINING_CONTAMINATION_STATUSES),
            "nonvoting_contamination_statuses": sorted(NONVOTING_CONTAMINATION_STATUSES),
            "fury_and_arms_counts_shared": False,
            "named_players_receive_permanent_weight_changes": False,
            "observed_date_spec_and_contamination_rule_only": True,
            "date_evidence_source": "upstream raid-level contamination status",
            "player_name_is_not_a_feature": True,
        },
        "split_contract": {
            "unit": "upstream guild+player+instance connected component",
            "assignment_method": "sha256(seed|component_id) rank then round-robin",
            "split_seed": split_seed,
            "requested_fold_count": fold_count,
            "effective_fold_count": effective_folds,
            "row_random_split": False,
            "same_player_guild_or_instance_can_cross_train_test": False,
        },
        "lanes": fitted_lanes,
        "internal_fidelity_gate": {
            "status": internal_status,
            "thresholds": deepcopy(FURY_FIDELITY_THRESHOLDS),
            "rejection_reasons": fury_rejections,
            "arms_metrics_affect_gate": False,
        },
        "claim_boundary": {
            "comparison_status": "NOT_COMPARISON_READY",
            "fourth_baseline_ready": False,
            "simulator_runtime_admission_validated": False,
            "arms_can_vote": False,
            "exact_trace_replay_used": False,
            "superiority_claim": False,
            "rejection_reasons": comparison_rejections,
        },
    }
    model = _with_content_address(model_core)
    validate_historical_warrior_policy_model(model)
    model_sha = _text(
        _mapping(model["content_address"], "model.content_address").get("sha256"),
        "model.content_address.sha256",
    )
    evaluation_core: JSONMap = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "input": input_identity,
        "model_content_sha256": model_sha,
        "split": {
            "unit": "upstream guild+player+instance connected component",
            "split_seed": split_seed,
            "requested_fold_count": fold_count,
            "effective_fold_count": effective_folds,
            "component_count": len(components),
            "component_to_fold": [
                {"component_id": component, "fold_index": assignment[component]}
                for component in sorted(assignment)
            ],
            "each_component_held_out_exactly_once": True,
            "row_random_split": False,
        },
        "data_accounting": accounting,
        "lanes": lane_evaluations,
        "fury_internal_fidelity_gate": {
            "status": internal_status,
            "thresholds": deepcopy(FURY_FIDELITY_THRESHOLDS),
            "rejection_reasons": fury_rejections,
        },
        "claim_boundary": {
            "comparison_status": "NOT_COMPARISON_READY",
            "fourth_baseline_ready": False,
            "arms_lane": "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
            "rejection_reasons": comparison_rejections,
        },
    }
    evaluation = _with_content_address(evaluation_core)
    validate_historical_warrior_policy_evaluation(
        evaluation, model_content_sha256=model_sha
    )
    model_path = output_dir / "model.json"
    evaluation_path = output_dir / "evaluation.json"
    addressed_model = output_dir / f"{MODEL_KIND}.{model_sha}.json"
    evaluation_sha = _text(
        _mapping(evaluation["content_address"], "evaluation.content_address").get("sha256"),
        "evaluation.content_address.sha256",
    )
    addressed_evaluation = output_dir / f"{EVALUATION_KIND}.{evaluation_sha}.json"
    temporaries: list[Path] = []
    try:
        addressed_model_temp = _atomic_json_temporary(addressed_model, model)
        temporaries.append(addressed_model_temp)
        addressed_evaluation_temp = _atomic_json_temporary(addressed_evaluation, evaluation)
        temporaries.append(addressed_evaluation_temp)
        stable_model_temp = _atomic_json_temporary(model_path, model)
        temporaries.append(stable_model_temp)
        stable_evaluation_temp = _atomic_json_temporary(evaluation_path, evaluation)
        temporaries.append(stable_evaluation_temp)
        _commit_or_reuse(addressed_model_temp, addressed_model)
        temporaries.remove(addressed_model_temp)
        _commit_or_reuse(addressed_evaluation_temp, addressed_evaluation)
        temporaries.remove(addressed_evaluation_temp)
        stable_model_temp.replace(model_path)
        temporaries.remove(stable_model_temp)
        # Evaluation is the mutable last-commit marker for the pair.
        stable_evaluation_temp.replace(evaluation_path)
        temporaries.remove(stable_evaluation_temp)
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
    return HistoricalWarriorPolicyResult(
        model=model_path,
        content_addressed_model=addressed_model,
        evaluation=evaluation_path,
        content_addressed_evaluation=addressed_evaluation,
        comparison_status="NOT_COMPARISON_READY",
        fury_decision_count=len(decisions[FURY_LANE]),
        arms_decision_count=len(decisions[ARMS_LANE]),
    )


def _load_policy_model(model_or_path: Mapping[str, Any] | str | Path) -> JSONMap:
    if isinstance(model_or_path, Mapping):
        model = deepcopy(dict(model_or_path))
    else:
        model = _load_json(Path(model_or_path).expanduser().resolve(), "policy model")
    validate_historical_warrior_policy_model(model)
    return model


def _runtime_candidates(
    state_before: Mapping[str, Any], legal_actions: Sequence[Mapping[str, Any]]
) -> list[JSONMap]:
    output: list[JSONMap] = []
    for index, raw in enumerate(legal_actions):
        candidate = _mapping(raw, f"legal_actions[{index}]")
        if candidate.get("legal") is False:
            continue
        action_raw = candidate.get("action", candidate)
        action = _mapping(action_raw, f"legal_actions[{index}].action")
        action_key = _action_key(state_before, action)
        if json.loads(action_key)["target_role"] == "OBSERVED_DEAD_TARGET":
            continue
        candidate_id = _optional_text(candidate.get("candidate_id"))
        if candidate_id is None:
            candidate_id = _sha256_json(
                {"candidate_index": index, "action": action}
            )
        output.append(
            {
                "candidate_index": index,
                "candidate_id": candidate_id,
                "action_key": action_key,
                "action": deepcopy(dict(action)),
            }
        )
    if not output:
        raise HistoricalWarriorPolicyError("no legal non-dead-target actions supplied")
    return output


def predict_action_distribution(
    model_or_path: Mapping[str, Any] | str | Path,
    *,
    state_before: Mapping[str, Any],
    legal_actions: Sequence[Mapping[str, Any]],
    lane: str = FURY_LANE,
) -> JSONMap:
    """Condition a learned lane distribution on the caller's legal actions."""

    if lane not in LANES:
        raise HistoricalWarriorPolicyError(f"unsupported Warrior lane {lane}")
    model = _load_policy_model(model_or_path)
    _assert_no_future_keys(state_before)
    contexts = _state_contexts(state_before)
    lane_model = _mapping(_mapping(model["lanes"], "model.lanes")[lane], lane)
    fitted = _mapping(lane_model.get("fitted_policy"), f"{lane}.fitted_policy")
    if _integer(fitted.get("decision_count"), f"{lane}.decision_count") <= 0:
        raise HistoricalWarriorPolicyError(f"{lane} has no fitted decisions")
    learned, matched = _policy_distribution(
        fitted, contexts, evaluation_unknown_bucket=False
    )
    candidates = _runtime_candidates(state_before, legal_actions)
    smoothing = _mapping(fitted.get("smoothing"), f"{lane}.smoothing")
    alpha = _nonnegative_number(smoothing.get("alpha"), f"{lane}.smoothing.alpha")
    total_decisions = _integer(fitted.get("decision_count"), f"{lane}.decision_count")
    novel_floor = alpha / (total_decisions + alpha * (len(learned) + 1))
    multiplicity = Counter(row["action_key"] for row in candidates)
    weights = [
        (learned.get(row["action_key"], novel_floor) / multiplicity[row["action_key"]])
        for row in candidates
    ]
    total_weight = sum(weights)
    if not math.isfinite(total_weight) or total_weight <= 0:
        raise HistoricalWarriorPolicyError("legal action conditioning has zero mass")
    rows = []
    for candidate, weight in zip(candidates, weights):
        rows.append(
            {
                **candidate,
                "probability": weight / total_weight,
            }
        )
    return {
        "schema": SCHEMA,
        "kind": "chronicle_historical_warrior_policy_action_distribution",
        "model_content_sha256": _mapping(
            model.get("content_address"), "model.content_address"
        )["sha256"],
        "lane": lane,
        "policy_role": lane_model.get("policy_role"),
        "comparison_status": "NOT_COMPARISON_READY",
        "context_match_levels": matched,
        "candidate_distribution": rows,
        "probability_sum": sum(row["probability"] for row in rows),
    }


def sample_legal_action(
    model_or_path: Mapping[str, Any] | str | Path,
    *,
    state_before: Mapping[str, Any],
    legal_actions: Sequence[Mapping[str, Any]],
    seed: Any,
    lane: str = FURY_LANE,
) -> JSONMap:
    """Draw one legal action with a reproducible hash-derived uniform variate."""

    distribution = predict_action_distribution(
        model_or_path,
        state_before=state_before,
        legal_actions=legal_actions,
        lane=lane,
    )
    seed_material = {
        "model_content_sha256": distribution["model_content_sha256"],
        "lane": lane,
        "seed": seed,
        "contexts": _state_contexts(state_before),
        "candidate_ids": [
            row["candidate_id"] for row in distribution["candidate_distribution"]
        ],
    }
    digest = hashlib.sha256(_canonical_bytes(seed_material)).digest()
    uniform = int.from_bytes(digest, "big") / float(1 << (8 * len(digest)))
    cumulative = 0.0
    selected = distribution["candidate_distribution"][-1]
    for row in distribution["candidate_distribution"]:
        cumulative += float(row["probability"])
        if uniform < cumulative:
            selected = row
            break
    return {
        "schema": SCHEMA,
        "kind": "chronicle_historical_warrior_policy_sample",
        "model_content_sha256": distribution["model_content_sha256"],
        "lane": lane,
        "comparison_status": "NOT_COMPARISON_READY",
        "seed_sha256": _sha256_json(seed_material),
        "candidate_id": selected["candidate_id"],
        "candidate_index": selected["candidate_index"],
        "probability": selected["probability"],
        "action": deepcopy(selected["action"]),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a prefix-causal historical Warrior policy baseline candidate."
    )
    parser.add_argument(
        "--team-wave-model-manifest",
        type=Path,
        default=DEFAULT_INPUT_MANIFEST,
    )
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--fold-count", type=int, default=DEFAULT_FOLD_COUNT)
    parser.add_argument("--split-seed", type=int, default=DEFAULT_SPLIT_SEED)
    parser.add_argument("--smoothing-alpha", type=float, default=DEFAULT_SMOOTHING_ALPHA)
    parser.add_argument("--backoff-strength", type=float, default=DEFAULT_BACKOFF_STRENGTH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    result = build_chronicle_historical_warrior_policy(
        team_wave_model_manifest_path=arguments.team_wave_model_manifest,
        output_directory=arguments.output_directory,
        fold_count=arguments.fold_count,
        split_seed=arguments.split_seed,
        smoothing_alpha=arguments.smoothing_alpha,
        backoff_strength=arguments.backoff_strength,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
