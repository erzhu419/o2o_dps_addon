"""Causal policy view over a dynamic simulator state.

The dynamic-v3 bridge intentionally exposes its complete simulator registry so
the control plane can execute the encounter.  That raw state is not a causal
policy observation: target rows, HP hypotheses, config digests, schedule
totals, and the idle horizon can all identify a future suffix.  This module
constructs a separate policy-plane view containing only targets introduced by
the current decision time.  Target HP is reconstructed from an exact
prefix-observed current/max baseline and only damage already applied at
runtime; raw simulator current/max HP never enters the policy plane.
Target armor is omitted because neither integrated policy needs it and the raw
value is an environment hypothesis rather than a prefix observation.

The bridge state does not carry target introduction times or provenance that
makes its HP hypotheses prefix-observed.  Callers therefore must supply both
explicit prefix-bound contracts; absence, partial coverage, or an inconsistent
contract fails closed.  The returned target-index map is control-plane data and
must not be passed to the policy.

This projection is development infrastructure only.  It does not authorize
training, comparison, deployment, voting, or superiority claims.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any, Mapping


JSONMap = dict[str, Any]

TARGET_REGISTRY_SCHEMA_V1 = "o2o_policy_target_introduction_registry/v1"
TARGET_REGISTRY_SOURCE_SEMANTICS_V1 = (
    "WINDOW_ORIGIN_PRESENT_OR_PREFIX_FIRST_OBSERVED_ACTIVITY"
)
TARGET_HEALTH_REGISTRY_SCHEMA_V1 = "o2o_policy_target_health_prefix_baseline_registry/v1"
TARGET_HEALTH_REGISTRY_SOURCE_SEMANTICS_V1 = (
    "EXACT_PREFIX_OBSERVED_CURRENT_AND_MAXIMUM_WITH_RUNTIME_DAMAGE_BASELINES"
)
CAUSAL_TEAM_VIEW_SCHEMA_V1 = "o2o_policy_dynamic_team_prefix_view/v1"
CAUSAL_TARGET_VIEW_SCHEMA_V1 = "o2o_policy_dynamic_target_prefix_view/v1"
CAUSAL_DAMAGE_RATE_SCHEMA_V1 = "o2o_policy_prefix_damage_rate/v1"


class PolicyObservationCausalProjectionV1Error(RuntimeError):
    """A raw state cannot be projected without guessing prefix information."""


@dataclass(frozen=True)
class TargetIntroductionV1:
    """One simulator target's first prefix-observable time, in simulator ms."""

    simulator_target_index: int
    introduced_at_ms: int

    def __post_init__(self) -> None:
        _integer(
            self.simulator_target_index,
            "target introduction simulator_target_index",
        )
        _integer(self.introduced_at_ms, "target introduction introduced_at_ms")

    def to_wire(self) -> JSONMap:
        return {
            "simulator_target_index": self.simulator_target_index,
            "introduced_at_ms": self.introduced_at_ms,
        }


@dataclass(frozen=True)
class TargetIntroductionRegistryV1:
    """Control-plane registry required to hide not-yet-introduced targets."""

    targets: tuple[TargetIntroductionV1, ...]
    schema: str = TARGET_REGISTRY_SCHEMA_V1
    source_semantics: str = TARGET_REGISTRY_SOURCE_SEMANTICS_V1

    def __post_init__(self) -> None:
        if self.schema != TARGET_REGISTRY_SCHEMA_V1:
            raise PolicyObservationCausalProjectionV1Error(
                "target introduction registry schema is unsupported"
            )
        if self.source_semantics != TARGET_REGISTRY_SOURCE_SEMANTICS_V1:
            raise PolicyObservationCausalProjectionV1Error(
                "target introduction registry is not explicitly prefix-bound"
            )
        if not self.targets:
            raise PolicyObservationCausalProjectionV1Error(
                "target introduction registry must not be empty"
            )
        if any(not isinstance(row, TargetIntroductionV1) for row in self.targets):
            raise PolicyObservationCausalProjectionV1Error(
                "target introduction registry must contain TargetIntroductionV1 rows"
            )
        indexes = [row.simulator_target_index for row in self.targets]
        if len(set(indexes)) != len(indexes):
            raise PolicyObservationCausalProjectionV1Error(
                "target introduction registry repeats a simulator target index"
            )

    def to_wire(self) -> JSONMap:
        return {
            "schema": self.schema,
            "source_semantics": self.source_semantics,
            "targets": [row.to_wire() for row in self.targets],
        }


@dataclass(frozen=True)
class TargetHealthPrefixBaselineV1:
    """Exact prefix HP plus causal damage counters at that observation."""

    simulator_target_index: int
    observed_at_ms: int
    current_health: float
    maximum_health: float
    simulated_damage_applied_at_observation: float
    background_damage_applied_at_observation: float

    def __post_init__(self) -> None:
        _integer(
            self.simulator_target_index,
            "target health baseline simulator_target_index",
        )
        _integer(self.observed_at_ms, "target health baseline observed_at_ms")
        current = _number(
            self.current_health, "target health baseline current_health"
        )
        maximum = _number(
            self.maximum_health,
            "target health baseline maximum_health",
            positive=True,
        )
        if current > maximum:
            raise PolicyObservationCausalProjectionV1Error(
                "target health baseline current_health exceeds maximum_health"
            )
        simulated = _number(
            self.simulated_damage_applied_at_observation,
            "target health baseline simulated damage",
        )
        background = _number(
            self.background_damage_applied_at_observation,
            "target health baseline background damage",
        )
        object.__setattr__(self, "current_health", current)
        object.__setattr__(self, "maximum_health", maximum)
        object.__setattr__(
            self,
            "simulated_damage_applied_at_observation",
            simulated,
        )
        object.__setattr__(
            self,
            "background_damage_applied_at_observation",
            background,
        )

    def to_wire(self) -> JSONMap:
        return {
            "simulator_target_index": self.simulator_target_index,
            "observed_at_ms": self.observed_at_ms,
            "current_health": self.current_health,
            "maximum_health": self.maximum_health,
            "simulated_damage_applied_at_observation": (
                self.simulated_damage_applied_at_observation
            ),
            "background_damage_applied_at_observation": (
                self.background_damage_applied_at_observation
            ),
        }


@dataclass(frozen=True)
class TargetHealthPrefixRegistryV1:
    """Control-plane HP baselines that are exact at a prefix observation."""

    targets: tuple[TargetHealthPrefixBaselineV1, ...]
    schema: str = TARGET_HEALTH_REGISTRY_SCHEMA_V1
    source_semantics: str = TARGET_HEALTH_REGISTRY_SOURCE_SEMANTICS_V1

    def __post_init__(self) -> None:
        if self.schema != TARGET_HEALTH_REGISTRY_SCHEMA_V1:
            raise PolicyObservationCausalProjectionV1Error(
                "target health prefix registry schema is unsupported"
            )
        if self.source_semantics != TARGET_HEALTH_REGISTRY_SOURCE_SEMANTICS_V1:
            raise PolicyObservationCausalProjectionV1Error(
                "target health registry is not explicitly prefix-observed"
            )
        if not self.targets:
            raise PolicyObservationCausalProjectionV1Error(
                "target health prefix registry must not be empty"
            )
        if any(
            not isinstance(row, TargetHealthPrefixBaselineV1)
            for row in self.targets
        ):
            raise PolicyObservationCausalProjectionV1Error(
                "target health prefix registry has an invalid row type"
            )
        indexes = [row.simulator_target_index for row in self.targets]
        if len(set(indexes)) != len(indexes):
            raise PolicyObservationCausalProjectionV1Error(
                "target health prefix registry repeats a simulator target index"
            )

    def to_wire(self) -> JSONMap:
        return {
            "schema": self.schema,
            "source_semantics": self.source_semantics,
            "targets": [row.to_wire() for row in self.targets],
        }


@dataclass(frozen=True)
class CausalLiveStateProjectionV1:
    """Policy state plus a private local-to-simulator target index map."""

    state: JSONMap
    policy_to_simulator_target_index: tuple[int, ...]
    visibility_cutoff_ms: int

    def simulator_target_index(self, policy_target_index: int) -> int:
        index = _integer(policy_target_index, "policy target index")
        if index >= len(self.policy_to_simulator_target_index):
            raise PolicyObservationCausalProjectionV1Error(
                "policy target index is outside the visible target registry"
            )
        return self.policy_to_simulator_target_index[index]


@dataclass(frozen=True)
class CausalPolicyInputProjectionV1:
    """Cat2-compatible policy input and its private target routing map."""

    policy_input: JSONMap
    policy_to_simulator_target_index: tuple[int, ...]
    visibility_cutoff_ms: int
    training_authorized: bool = False
    comparison_authorized: bool = False
    deployment_authorized: bool = False
    voting_eligible: bool = False
    superiority_claim_authorized: bool = False

    def simulator_target_index(self, policy_target_index: int) -> int:
        index = _integer(policy_target_index, "policy target index")
        if index >= len(self.policy_to_simulator_target_index):
            raise PolicyObservationCausalProjectionV1Error(
                "policy target index is outside the visible target registry"
            )
        return self.policy_to_simulator_target_index[index]


_PASSTHROUGH_ROOT_FIELDS = frozenset(
    {
        "armor_penetration",
        "auras",
        "autoattack_active",
        "current_cast",
        "damage_done",
        "equipment_slots",
        "finished",
        "gcd_remaining_ms",
        "health_current",
        "health_maximum",
        "melee_attack_power",
        "mh_swing_duration_ms",
        "mh_swing_remaining_ms",
        "moving",
        "needs_input",
        "oh_swing_remaining_ms",
        "power",
        "precombat",
        "queued_swing",
        "stance",
        "strength",
        "swing_queue",
        "target_auras",
        "time_ms",
    }
)

_RECOMPUTED_ROOT_FIELDS = frozenset(
    {
        "encounter_damage_taken",
        "execute_phase_20",
        "execute_phase_25",
        "execute_phase_35",
        "num_targets",
        "target_health",
        "target_health_known",
        "target_health_max",
        "target_health_percent",
        "target_index",
        "total_target_count",
    }
)

_DROPPED_ROOT_FIELDS = frozenset(
    {
        "dynamic_config_sha256",
        "dynamic_idle_advance",
        "dynamic_team_response",
        "encounter_health_target",
        "environment_generation",
        "effective_target_armor",
        "remaining_ms",
        "target_armor",
        "wake_ready",
    }
)

_DYNAMIC_ROOT_FIELDS = frozenset(
    {"dynamic_team_background", "dynamic_target_semantics"}
)

_TEAM_INPUT_FIELDS = frozenset(
    {
        "schema",
        "config_digest",
        "environment_generation",
        "same_timestamp_order",
        "retarget_mode",
        "retarget_required",
        "simulated_damage_applied",
        "background_damage_applied",
        "combined_damage_applied",
        "background_events_processed",
        "background_events_total",
        "background_events_canceled",
        "background_damage_applications_processed",
        "candidate_events_processed",
        "candidate_events_canceled",
        "responsive_damage_applications_processed",
        "damage_applications_total",
        "targets",
    }
)

_LIFECYCLE_TARGET_FIELDS = frozenset(
    {
        "target_index",
        "initial_health",
        "current_health",
        "dead",
        "simulated_damage_applied",
        "background_damage_applied",
        "death_time_ms",
    }
)

_SEMANTICS_INPUT_FIELDS = frozenset(
    {
        "schema",
        "config_digest",
        "environment_generation",
        "same_timestamp_order",
        "attackability_events_processed",
        "attackability_events_total",
        "effective_armor_events_processed",
        "effective_armor_events_total",
        "targets",
    }
)

_SEMANTICS_TARGET_FIELDS = frozenset(
    {
        "target_index",
        "attackable",
        "effective_armor",
        "maximum_health",
        "current_health",
        "dead",
        "death_time_ms",
    }
)

_CAT2_POLICY_INPUT_FIELDS = frozenset(
    {
        "schema",
        "policy_id",
        "decision_index",
        "run_binding_content_sha256",
        "live_state",
        "available_actions",
        "pending_attempt_ids",
        "target_boundary",
        "optimizer_parameters",
        "historical_prior",
    }
)

_AVAILABLE_ACTION_REQUIRED_FIELDS = frozenset(
    {"index", "action", "label", "legal", "ready_in_ms", "triggers_gcd"}
)
_AVAILABLE_ACTION_FIELDS = _AVAILABLE_ACTION_REQUIRED_FIELDS | frozenset(
    {"cooldown_duration_ms", "result_bearing"}
)
_ACTION_REF_FIELDS = frozenset({"spell_id", "item_id", "other_id", "tag"})


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must be a finite number"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must be a {'positive' if positive else 'nonnegative'} finite number"
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must be an object"
        )
    return value


def _object_rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must be a nonempty object array"
        )
    if any(not isinstance(row, Mapping) for row in value):
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} must contain only objects"
        )
    return list(value)


def _require_known_fields(
    value: Mapping[str, Any], allowed: frozenset[str], label: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} contains unclassified fields: {names}"
        )


def _strict_json_copy(value: Any, label: str) -> Any:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PolicyObservationCausalProjectionV1Error(
            f"{label} is not strict JSON: {error}"
        ) from error


def _project_available_actions(value: Any) -> list[JSONMap]:
    """Retain only the typed, current-decision action surface.

    ``available_actions`` is an observation of legality/readiness *now*.  It is
    therefore policy-visible, but arbitrary caller metadata is not.  Rebuilding
    every row here prevents a future schedule or target registry from being
    smuggled through an otherwise opaque action object.
    """

    if not isinstance(value, list):
        raise PolicyObservationCausalProjectionV1Error(
            "Cat2 available_actions must be an array"
        )
    projected: list[JSONMap] = []
    indexes: set[int] = set()
    for ordinal, raw_row in enumerate(value):
        row = _mapping(raw_row, f"Cat2 available action {ordinal}")
        _require_known_fields(
            row, _AVAILABLE_ACTION_FIELDS, f"Cat2 available action {ordinal}"
        )
        if not _AVAILABLE_ACTION_REQUIRED_FIELDS.issubset(row):
            missing = ", ".join(
                sorted(_AVAILABLE_ACTION_REQUIRED_FIELDS - set(row))
            )
            raise PolicyObservationCausalProjectionV1Error(
                f"Cat2 available action {ordinal} lacks fields: {missing}"
            )
        index = _integer(row["index"], f"Cat2 available action {ordinal} index")
        if index in indexes:
            raise PolicyObservationCausalProjectionV1Error(
                "Cat2 available_actions repeats an index"
            )
        indexes.add(index)
        action = _mapping(
            row["action"], f"Cat2 available action {ordinal} action"
        )
        _require_known_fields(
            action, _ACTION_REF_FIELDS, f"Cat2 available action {ordinal} action"
        )
        if not action:
            raise PolicyObservationCausalProjectionV1Error(
                f"Cat2 available action {ordinal} lacks an action identity"
            )
        projected_action: JSONMap = {}
        for key in ("spell_id", "item_id", "other_id", "tag"):
            if key in action:
                projected_action[key] = _integer(
                    action[key], f"Cat2 available action {ordinal} action {key}"
                )
        label = row["label"]
        if not isinstance(label, str):
            raise PolicyObservationCausalProjectionV1Error(
                f"Cat2 available action {ordinal} label must be text"
            )
        legal = row["legal"]
        triggers_gcd = row["triggers_gcd"]
        result_bearing = row.get("result_bearing", False)
        if (
            not isinstance(legal, bool)
            or not isinstance(triggers_gcd, bool)
            or not isinstance(result_bearing, bool)
        ):
            raise PolicyObservationCausalProjectionV1Error(
                f"Cat2 available action {ordinal} flags must be boolean"
            )
        projected.append(
            {
                "index": index,
                "action": projected_action,
                "label": label,
                "legal": legal,
                "ready_in_ms": _integer(
                    row["ready_in_ms"],
                    f"Cat2 available action {ordinal} ready_in_ms",
                ),
                "cooldown_duration_ms": _integer(
                    row.get("cooldown_duration_ms", 0),
                    (
                        f"Cat2 available action {ordinal} "
                        "cooldown_duration_ms"
                    ),
                ),
                "triggers_gcd": triggers_gcd,
                "result_bearing": result_bearing,
            }
        )
    return projected


def canonical_policy_observation_bytes_v1(value: Mapping[str, Any]) -> bytes:
    """Serialize the policy-visible mapping for paired-prefix assertions."""

    copy = _strict_json_copy(value, "policy observation")
    if not isinstance(copy, dict):
        raise PolicyObservationCausalProjectionV1Error(
            "policy observation must remain an object"
        )
    return json.dumps(
        copy,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def target_introduction_registry_from_wire_v1(
    value: Mapping[str, Any],
) -> TargetIntroductionRegistryV1:
    """Parse an exact serialized prefix-registry contract."""

    raw = _mapping(
        _strict_json_copy(value, "target introduction registry"),
        "target introduction registry",
    )
    expected = {"schema", "source_semantics", "targets"}
    if set(raw) != expected:
        raise PolicyObservationCausalProjectionV1Error(
            "target introduction registry fields are not exact"
        )
    rows = raw["targets"]
    if not isinstance(rows, list) or not rows:
        raise PolicyObservationCausalProjectionV1Error(
            "target introduction registry targets must be a nonempty array"
        )
    parsed: list[TargetIntroductionV1] = []
    for index, value_row in enumerate(rows):
        row = _mapping(value_row, f"target introduction row {index}")
        if set(row) != {"simulator_target_index", "introduced_at_ms"}:
            raise PolicyObservationCausalProjectionV1Error(
                f"target introduction row {index} fields are not exact"
            )
        parsed.append(
            TargetIntroductionV1(
                simulator_target_index=_integer(
                    row["simulator_target_index"],
                    f"target introduction row {index} simulator_target_index",
                ),
                introduced_at_ms=_integer(
                    row["introduced_at_ms"],
                    f"target introduction row {index} introduced_at_ms",
                ),
            )
        )
    return TargetIntroductionRegistryV1(
        targets=tuple(parsed),
        schema=str(raw["schema"]),
        source_semantics=str(raw["source_semantics"]),
    )


def target_health_prefix_registry_from_wire_v1(
    value: Mapping[str, Any],
) -> TargetHealthPrefixRegistryV1:
    """Parse an exact serialized prefix-observed HP contract."""

    raw = _mapping(
        _strict_json_copy(value, "target health prefix registry"),
        "target health prefix registry",
    )
    expected = {"schema", "source_semantics", "targets"}
    if set(raw) != expected:
        raise PolicyObservationCausalProjectionV1Error(
            "target health prefix registry fields are not exact"
        )
    rows = raw["targets"]
    if not isinstance(rows, list) or not rows:
        raise PolicyObservationCausalProjectionV1Error(
            "target health prefix registry targets must be a nonempty array"
        )
    fields = {
        "simulator_target_index",
        "observed_at_ms",
        "current_health",
        "maximum_health",
        "simulated_damage_applied_at_observation",
        "background_damage_applied_at_observation",
    }
    parsed: list[TargetHealthPrefixBaselineV1] = []
    for index, value_row in enumerate(rows):
        row = _mapping(value_row, f"target health prefix row {index}")
        if set(row) != fields:
            raise PolicyObservationCausalProjectionV1Error(
                f"target health prefix row {index} fields are not exact"
            )
        parsed.append(
            TargetHealthPrefixBaselineV1(
                simulator_target_index=_integer(
                    row["simulator_target_index"],
                    f"target health prefix row {index} simulator_target_index",
                ),
                observed_at_ms=_integer(
                    row["observed_at_ms"],
                    f"target health prefix row {index} observed_at_ms",
                ),
                current_health=_number(
                    row["current_health"],
                    f"target health prefix row {index} current_health",
                ),
                maximum_health=_number(
                    row["maximum_health"],
                    f"target health prefix row {index} maximum_health",
                    positive=True,
                ),
                simulated_damage_applied_at_observation=_number(
                    row["simulated_damage_applied_at_observation"],
                    f"target health prefix row {index} simulated damage",
                ),
                background_damage_applied_at_observation=_number(
                    row["background_damage_applied_at_observation"],
                    f"target health prefix row {index} background damage",
                ),
            )
        )
    return TargetHealthPrefixRegistryV1(
        targets=tuple(parsed),
        schema=str(raw["schema"]),
        source_semantics=str(raw["source_semantics"]),
    )


def _validate_target_rows(
    team: Mapping[str, Any], semantics: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    _require_known_fields(team, _TEAM_INPUT_FIELDS, "dynamic team state")
    _require_known_fields(
        semantics, _SEMANTICS_INPUT_FIELDS, "dynamic target-semantics state"
    )
    life_rows = _object_rows(team.get("targets"), "dynamic team targets")
    semantic_rows = _object_rows(
        semantics.get("targets"), "dynamic target-semantics targets"
    )
    if len(life_rows) != len(semantic_rows):
        raise PolicyObservationCausalProjectionV1Error(
            "dynamic target blocks differ in length"
        )
    for index, (life, semantic) in enumerate(
        zip(life_rows, semantic_rows, strict=True)
    ):
        _require_known_fields(
            life, _LIFECYCLE_TARGET_FIELDS, f"lifecycle target {index}"
        )
        _require_known_fields(
            semantic, _SEMANTICS_TARGET_FIELDS, f"semantics target {index}"
        )
        if life.get("target_index") != index or semantic.get("target_index") != index:
            raise PolicyObservationCausalProjectionV1Error(
                "raw simulator target indexes must be contiguous and position-bound"
            )
        life_health = _number(
            life.get("current_health"), f"lifecycle target {index} current_health"
        )
        semantic_health = _number(
            semantic.get("current_health"),
            f"semantics target {index} current_health",
        )
        life_dead = life.get("dead")
        semantic_dead = semantic.get("dead")
        if (
            not isinstance(life_dead, bool)
            or not isinstance(semantic_dead, bool)
            or life_dead != semantic_dead
            or life_health != semantic_health
        ):
            raise PolicyObservationCausalProjectionV1Error(
                "dynamic target blocks disagree on current health or death"
            )
    return life_rows, semantic_rows


def _visible_indexes(
    registry: TargetIntroductionRegistryV1,
    *,
    target_count: int,
    now_ms: int,
) -> tuple[int, ...]:
    by_index = {row.simulator_target_index: row for row in registry.targets}
    expected = set(range(target_count))
    if set(by_index) != expected:
        raise PolicyObservationCausalProjectionV1Error(
            "target introduction registry does not exactly cover raw target indexes"
        )
    return tuple(
        index
        for index in range(target_count)
        if by_index[index].introduced_at_ms <= now_ms
    )


def _health_baselines(
    registry: TargetHealthPrefixRegistryV1 | None,
    introductions: TargetIntroductionRegistryV1,
    *,
    target_count: int,
    now_ms: int,
    visible_indexes: tuple[int, ...],
) -> dict[int, TargetHealthPrefixBaselineV1]:
    if registry is None or not isinstance(registry, TargetHealthPrefixRegistryV1):
        raise PolicyObservationCausalProjectionV1Error(
            "an explicit TargetHealthPrefixRegistryV1 is required"
        )
    health_by_index = {
        row.simulator_target_index: row for row in registry.targets
    }
    introduction_by_index = {
        row.simulator_target_index: row for row in introductions.targets
    }
    expected = set(range(target_count))
    if set(health_by_index) != expected:
        raise PolicyObservationCausalProjectionV1Error(
            "target health prefix registry does not exactly cover raw target indexes"
        )
    for index, baseline in health_by_index.items():
        introduced_at = introduction_by_index[index].introduced_at_ms
        if baseline.observed_at_ms < introduced_at:
            raise PolicyObservationCausalProjectionV1Error(
                "target health baseline precedes target introduction"
            )
    for index in visible_indexes:
        if health_by_index[index].observed_at_ms > now_ms:
            raise PolicyObservationCausalProjectionV1Error(
                "a visible target lacks a current-prefix health baseline"
            )
    return health_by_index


def _project_lifecycle_target(
    row: Mapping[str, Any],
    *,
    policy_index: int,
    health_baseline: TargetHealthPrefixBaselineV1,
) -> JSONMap:
    simulated_total = _number(
        row.get("simulated_damage_applied"),
        "lifecycle target simulated_damage_applied",
    )
    background_total = _number(
        row.get("background_damage_applied"),
        "lifecycle target background_damage_applied",
    )
    simulated = (
        simulated_total
        - health_baseline.simulated_damage_applied_at_observation
    )
    background = (
        background_total
        - health_baseline.background_damage_applied_at_observation
    )
    tolerance = 1e-7
    if simulated < -tolerance or background < -tolerance:
        raise PolicyObservationCausalProjectionV1Error(
            "runtime damage counters precede the prefix health baseline"
        )
    simulated = max(0.0, simulated)
    background = max(0.0, background)
    current_health = health_baseline.current_health - simulated - background
    if current_health < -tolerance:
        raise PolicyObservationCausalProjectionV1Error(
            "runtime prefix damage exceeds the observed target health"
        )
    # The bridge may accumulate decimal background damage in a different
    # floating-point order than this prefix reconstruction.  Treat the same
    # tolerance already accepted above as exact zero so a killed target cannot
    # reappear as a live ~1e-11 HP row.
    current_health = 0.0 if current_health <= tolerance else current_health
    dead = current_health <= 0.0
    result: JSONMap = {
        "target_index": policy_index,
        "initial_health": health_baseline.current_health,
        "current_health": current_health,
        "dead": dead,
        "simulated_damage_applied": simulated,
        "background_damage_applied": background,
    }
    return result


def _project_semantics_target(
    row: Mapping[str, Any],
    *,
    policy_index: int,
    health_baseline: TargetHealthPrefixBaselineV1,
    lifecycle: Mapping[str, Any],
) -> JSONMap:
    attackable = row.get("attackable")
    if not isinstance(attackable, bool):
        raise PolicyObservationCausalProjectionV1Error(
            "semantics target attackable must be boolean"
        )
    dead = lifecycle.get("dead") is True
    result: JSONMap = {
        "target_index": policy_index,
        "attackable": attackable and not dead,
        "maximum_health": health_baseline.maximum_health,
        "current_health": float(lifecycle["current_health"]),
        "dead": dead,
    }
    return result


def project_live_state_for_policy_v1(
    live_state: Mapping[str, Any],
    registry: TargetIntroductionRegistryV1 | None,
    health_registry: TargetHealthPrefixRegistryV1 | None,
) -> CausalLiveStateProjectionV1:
    """Project one raw bridge state onto its observed target prefix.

    Target-local indexes are compact in the policy plane.  Use the returned
    mapping to translate a target-setting intent back to the simulator plane.
    """

    if registry is None or not isinstance(registry, TargetIntroductionRegistryV1):
        raise PolicyObservationCausalProjectionV1Error(
            "an explicit TargetIntroductionRegistryV1 is required"
        )
    state = _mapping(_strict_json_copy(live_state, "live state"), "live state")
    allowed_root = (
        _PASSTHROUGH_ROOT_FIELDS
        | _RECOMPUTED_ROOT_FIELDS
        | _DROPPED_ROOT_FIELDS
        | _DYNAMIC_ROOT_FIELDS
    )
    _require_known_fields(state, allowed_root, "live state")

    now_ms = _integer(state.get("time_ms"), "live state time_ms")
    team = _mapping(
        state.get("dynamic_team_background"), "dynamic team background"
    )
    semantics = _mapping(
        state.get("dynamic_target_semantics"), "dynamic target semantics"
    )
    life_rows, semantic_rows = _validate_target_rows(team, semantics)
    visible_simulator_indexes = _visible_indexes(
        registry, target_count=len(life_rows), now_ms=now_ms
    )
    if not visible_simulator_indexes:
        raise PolicyObservationCausalProjectionV1Error(
            "no target is prefix-visible at the current decision time"
        )
    health_by_simulator_index = _health_baselines(
        health_registry,
        registry,
        target_count=len(life_rows),
        now_ms=now_ms,
        visible_indexes=visible_simulator_indexes,
    )

    selected_simulator_index = _integer(
        state.get("target_index"), "live state target_index"
    )
    if selected_simulator_index not in visible_simulator_indexes:
        raise PolicyObservationCausalProjectionV1Error(
            "the selected simulator target is not prefix-visible"
        )
    visible_simulator_index_set = set(visible_simulator_indexes)
    for simulator_index, semantic in enumerate(semantic_rows):
        if (
            simulator_index not in visible_simulator_index_set
            and semantic.get("attackable") is not False
        ):
            raise PolicyObservationCausalProjectionV1Error(
                "a not-yet-introduced target must be raw-unattackable"
            )
    simulator_to_policy = {
        simulator_index: policy_index
        for policy_index, simulator_index in enumerate(visible_simulator_indexes)
    }
    selected_policy_index = simulator_to_policy[selected_simulator_index]

    projected_life = [
        _project_lifecycle_target(
            life_rows[simulator_index],
            policy_index=policy_index,
            health_baseline=health_by_simulator_index[simulator_index],
        )
        for policy_index, simulator_index in enumerate(visible_simulator_indexes)
    ]
    projected_semantics = [
        _project_semantics_target(
            semantic_rows[simulator_index],
            policy_index=policy_index,
            health_baseline=health_by_simulator_index[simulator_index],
            lifecycle=projected_life[policy_index],
        )
        for policy_index, simulator_index in enumerate(visible_simulator_indexes)
    ]

    # Estimate only from damage already observed after the earliest visible
    # prefix baseline.  In particular, do not copy the simulator's responsive
    # team model, future event count, configured DPS, or eventual death time.
    # The estimate is intentionally unavailable at a zero-length prefix; a
    # time-gated policy can then defer and reconsider at the next observation.
    earliest_health_observation_ms = min(
        health_by_simulator_index[index].observed_at_ms
        for index in visible_simulator_indexes
    )
    prefix_elapsed_ms = now_ms - earliest_health_observation_ms
    prefix_simulated_damage = sum(
        float(row["simulated_damage_applied"]) for row in projected_life
    )
    prefix_background_damage = sum(
        float(row["background_damage_applied"]) for row in projected_life
    )
    prefix_combined_damage = (
        prefix_simulated_damage + prefix_background_damage
    )
    prefix_combined_dps = (
        prefix_combined_damage * 1000.0 / prefix_elapsed_ms
        if prefix_elapsed_ms > 0 and prefix_combined_damage > 0
        else None
    )

    projected: JSONMap = {
        key: deepcopy(state[key])
        for key in _PASSTHROUGH_ROOT_FIELDS
        if key in state
    }
    selected_life = projected_life[selected_policy_index]
    selected_semantics = projected_semantics[selected_policy_index]
    current_health = float(selected_semantics["current_health"])
    maximum_health = float(selected_semantics["maximum_health"])
    projected.update(
        {
            "target_index": selected_policy_index,
            "num_targets": sum(
                row["attackable"] is True and row["dead"] is False
                for row in projected_semantics
            ),
            "total_target_count": len(projected_semantics),
            "target_health_known": True,
            "target_health": current_health,
            "target_health_max": maximum_health,
            "target_health_percent": 100.0 * current_health / maximum_health,
            "execute_phase_20": current_health <= maximum_health * 0.20,
            "execute_phase_25": current_health <= maximum_health * 0.25,
            "execute_phase_35": current_health <= maximum_health * 0.35,
            "encounter_damage_taken": sum(
                float(row["simulated_damage_applied"])
                + float(row["background_damage_applied"])
                for row in projected_life
            ),
            "dynamic_team_background": {
                "schema": CAUSAL_TEAM_VIEW_SCHEMA_V1,
                "retarget_required": selected_life["dead"] is True,
                "simulated_damage_applied": sum(
                    float(row["simulated_damage_applied"])
                    for row in projected_life
                ),
                "background_damage_applied": sum(
                    float(row["background_damage_applied"])
                    for row in projected_life
                ),
                "combined_damage_applied": sum(
                    float(row["simulated_damage_applied"])
                    + float(row["background_damage_applied"])
                    for row in projected_life
                ),
                "prefix_damage_rate": {
                    "schema": CAUSAL_DAMAGE_RATE_SCHEMA_V1,
                    "observation_start_ms": earliest_health_observation_ms,
                    "observation_end_ms": now_ms,
                    "elapsed_ms": prefix_elapsed_ms,
                    "simulated_damage": prefix_simulated_damage,
                    "background_damage": prefix_background_damage,
                    "combined_damage": prefix_combined_damage,
                    "combined_damage_per_second": prefix_combined_dps,
                    "source_semantics": (
                        "CURRENT_PREFIX_DAMAGE_DELTAS_ONLY"
                    ),
                },
                "targets": projected_life,
            },
            "dynamic_target_semantics": {
                "schema": CAUSAL_TARGET_VIEW_SCHEMA_V1,
                "targets": projected_semantics,
            },
        }
    )
    projected_copy = _strict_json_copy(projected, "projected live state")
    if not isinstance(projected_copy, dict):
        raise AssertionError("projected live state did not remain an object")
    return CausalLiveStateProjectionV1(
        state=projected_copy,
        policy_to_simulator_target_index=visible_simulator_indexes,
        visibility_cutoff_ms=now_ms,
    )


def _target_boundary(state: Mapping[str, Any]) -> JSONMap:
    semantics = _mapping(
        state.get("dynamic_target_semantics"), "projected target semantics"
    )
    lifecycle = _mapping(
        state.get("dynamic_team_background"), "projected target lifecycle"
    )
    target_rows = _object_rows(
        semantics.get("targets"), "projected target-semantics rows"
    )
    lifecycle_rows = _object_rows(
        lifecycle.get("targets"), "projected target-lifecycle rows"
    )
    index = _integer(state.get("target_index"), "projected target index")
    if index >= len(target_rows) or index >= len(lifecycle_rows):
        raise PolicyObservationCausalProjectionV1Error(
            "projected target index is outside the visible registry"
        )
    return {
        "target_index": index,
        "retarget_required": lifecycle.get("retarget_required") is True,
        "selected_target_semantics": deepcopy(target_rows[index]),
        "selected_target_lifecycle": deepcopy(lifecycle_rows[index]),
        "target_semantics_rows": deepcopy(target_rows),
        "target_lifecycle_rows": deepcopy(lifecycle_rows),
    }


def project_cat2_policy_input_v1(
    policy_input: Mapping[str, Any],
    registry: TargetIntroductionRegistryV1 | None,
    health_registry: TargetHealthPrefixRegistryV1 | None,
) -> CausalPolicyInputProjectionV1:
    """Replace Cat2's raw live state and target boundary with a causal view.

    The suffix-bound run binding is deliberately omitted from the policy plane;
    it remains available to the executor as control-plane data.
    """

    raw = _mapping(
        _strict_json_copy(policy_input, "Cat2 policy input"),
        "Cat2 policy input",
    )
    _require_known_fields(raw, _CAT2_POLICY_INPUT_FIELDS, "Cat2 policy input")
    for key in (
        "schema",
        "policy_id",
        "decision_index",
        "live_state",
        "available_actions",
        "pending_attempt_ids",
        "target_boundary",
        "optimizer_parameters",
        "historical_prior",
    ):
        if key not in raw:
            raise PolicyObservationCausalProjectionV1Error(
                f"Cat2 policy input lacks {key}"
            )
    if not isinstance(raw["schema"], str) or not raw["schema"]:
        raise PolicyObservationCausalProjectionV1Error(
            "Cat2 policy input schema must be nonempty text"
        )
    if not isinstance(raw["policy_id"], str) or not raw["policy_id"]:
        raise PolicyObservationCausalProjectionV1Error(
            "Cat2 policy input policy_id must be nonempty text"
        )
    _integer(raw["decision_index"], "Cat2 policy decision_index")
    available_actions = _project_available_actions(raw["available_actions"])
    if not isinstance(raw["pending_attempt_ids"], list) or any(
        not isinstance(value, str) or not value
        for value in raw["pending_attempt_ids"]
    ):
        raise PolicyObservationCausalProjectionV1Error(
            "Cat2 pending_attempt_ids must contain nonempty text"
        )
    _mapping(raw["target_boundary"], "Cat2 raw target boundary")
    _mapping(raw["optimizer_parameters"], "Cat2 optimizer parameters")
    _mapping(raw["historical_prior"], "Cat2 historical prior")

    live = project_live_state_for_policy_v1(
        _mapping(raw["live_state"], "Cat2 live state"),
        registry,
        health_registry,
    )
    safe = {
        "schema": raw["schema"],
        "policy_id": raw["policy_id"],
        "decision_index": raw["decision_index"],
        "live_state": live.state,
        "available_actions": available_actions,
        "pending_attempt_ids": deepcopy(raw["pending_attempt_ids"]),
        "target_boundary": _target_boundary(live.state),
        # These mappings belong to the control-plane run configuration.  The
        # current policy carries its parameters in its own immutable object and
        # does not consume either mapping.  Empty typed placeholders preserve
        # the frozen v6 policy-input shape without allowing a suffix-derived
        # prior or optimizer payload to influence a decision.
        "optimizer_parameters": {},
        "historical_prior": {},
    }
    safe_copy = _strict_json_copy(safe, "causal Cat2 policy input")
    if not isinstance(safe_copy, dict):
        raise AssertionError("causal Cat2 policy input did not remain an object")
    return CausalPolicyInputProjectionV1(
        policy_input=safe_copy,
        policy_to_simulator_target_index=(
            live.policy_to_simulator_target_index
        ),
        visibility_cutoff_ms=live.visibility_cutoff_ms,
    )


__all__ = [
    "CAUSAL_TARGET_VIEW_SCHEMA_V1",
    "CAUSAL_TEAM_VIEW_SCHEMA_V1",
    "CausalLiveStateProjectionV1",
    "CausalPolicyInputProjectionV1",
    "PolicyObservationCausalProjectionV1Error",
    "TARGET_REGISTRY_SCHEMA_V1",
    "TARGET_REGISTRY_SOURCE_SEMANTICS_V1",
    "TARGET_HEALTH_REGISTRY_SCHEMA_V1",
    "TARGET_HEALTH_REGISTRY_SOURCE_SEMANTICS_V1",
    "TargetHealthPrefixBaselineV1",
    "TargetHealthPrefixRegistryV1",
    "TargetIntroductionRegistryV1",
    "TargetIntroductionV1",
    "canonical_policy_observation_bytes_v1",
    "project_cat2_policy_input_v1",
    "project_live_state_for_policy_v1",
    "target_health_prefix_registry_from_wire_v1",
    "target_introduction_registry_from_wire_v1",
]
