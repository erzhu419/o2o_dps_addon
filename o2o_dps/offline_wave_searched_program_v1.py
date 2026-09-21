"""Independent searched complete-wave programs derived from offline guides.

This is deliberately a different schema from :mod:`offline_wave_policy_v1`.
An offline policy records source evidence; a searched program is a proposal.
Consequently, searched steps carry explicit execution windows and optional
causal guards, but no evidence status or source-event claim.

The edit grammar is intentionally small.  A normal candidate differs from its
parent by at most two non-conflicting edits.  Callers may explicitly raise the
limit to eight for a bounded joint opener repair; larger per-candidate rewrites
are outside this contract.  Programs are frozen before seeds are evaluated --
seed-indexed program shapes are rejected by the wire parsers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from typing import Any, Mapping, Sequence

from .causal_guard_v1 import (
    ObservableCausalGuardV1,
    observable_causal_guard_from_dict_v1,
)
from .offline_wave_policy_v1 import (
    LANE_GCD,
    LANE_OFF_GCD,
    LANE_QUEUE,
    TARGET_OTHER,
    TARGET_SELF,
    OfflineWaveFeedbackPolicyV1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError


JSONMap = dict[str, Any]
PROGRAM_SCHEMA = "offline_wave_searched_program/v1"
STEP_SCHEMA = "offline_wave_searched_program_step/v1"
EDIT_SCHEMA = "offline_wave_searched_program_edit/v1"
BEHAVIOR_SCHEMA = "offline_wave_searched_behavior/v1"

LANES = frozenset({LANE_GCD, LANE_OFF_GCD, LANE_QUEUE})
MAX_SUPPORTED_EDITS = 8

_PROGRAM_CONTRACT = {
    "searched_proposal_not_source_evidence": True,
    "execution_window_explicit_per_step": True,
    "future_events_visible": False,
    "program_frozen_before_seed_evaluation": True,
    "per_seed_program_shape_allowed": False,
}
_FORBIDDEN_SEED_FIELDS = frozenset(
    {
        "seed",
        "seed_id",
        "simulator_seed",
        "teammate_seed",
        "per_seed",
        "per_seed_program",
        "per_seed_programs",
        "seed_override",
        "seed_overrides",
        "seed_program",
        "seed_programs",
    }
)


class SearchedWaveProgramV1Error(ValueError):
    """A searched program or bounded edit is invalid."""


class SearchedWaveStepKindV1(str, Enum):
    ACTION = "ACTION"
    WAIT = "WAIT"


class SearchedWaveGapBehaviorV1(str, Enum):
    WAIT_UNTIL_STEP = "WAIT_UNTIL_STEP"
    TAIL_FILL_UNTIL_STEP = "TAIL_FILL_UNTIL_STEP"


class SearchedWaveTargetKindV1(str, Enum):
    CURRENT = "CURRENT"
    INDEX = "INDEX"
    SELF = "SELF"


class SearchedWaveEditKindV1(str, Enum):
    INSERT_BEFORE = "INSERT_BEFORE"
    DELETE = "DELETE"
    REPLACE = "REPLACE"
    REPEAT_AFTER = "REPEAT_AFTER"
    RETIME = "RETIME"
    RETARGET = "RETARGET"
    REPLACE_GUARD = "REPLACE_GUARD"
    TAIL_REORDER = "TAIL_REORDER"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchedWaveProgramV1Error(f"{label} must be nonempty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SearchedWaveProgramV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise SearchedWaveProgramV1Error(f"{label} must be positive")
    return result


def _exact_mapping(
    value: object,
    expected: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SearchedWaveProgramV1Error(f"{label} must be an object")
    if set(value) != expected:
        raise SearchedWaveProgramV1Error(
            f"{label} fields differ: expected {sorted(expected)}, "
            f"got {sorted(value)}"
        )
    return value


def _action(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise SearchedWaveProgramV1Error(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if (
        any(
            isinstance(field, bool) or not isinstance(field, int) or field < 0
            for field in (*identities, value.tag)
        )
        or sum(field > 0 for field in identities) != 1
    ):
        raise SearchedWaveProgramV1Error(
            f"{label} has an invalid exact action identity"
        )
    return value


def _action_from_wire(value: object, label: str) -> ActionRef:
    if not isinstance(value, Mapping):
        raise SearchedWaveProgramV1Error(f"{label} must be an action object")
    try:
        return _action(ActionRef.from_wire(value), label)
    except (TypeError, ValueError, SimBridgeProtocolError) as error:
        raise SearchedWaveProgramV1Error(f"invalid {label}: {error}") from error


def _enum(enum_type: type[Enum], value: object, label: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise SearchedWaveProgramV1Error(f"{label} is unsupported") from error


def _reject_per_seed_shape(value: object, label: str) -> None:
    """Reject seed-conditioned program syntax before discriminated parsing."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            if key in _FORBIDDEN_SEED_FIELDS:
                raise SearchedWaveProgramV1Error(
                    f"{label} contains forbidden per-seed field {raw_key!r}"
                )
            _reject_per_seed_shape(child, label)
    elif isinstance(value, list):
        for child in value:
            _reject_per_seed_shape(child, label)


@dataclass(frozen=True)
class SearchedWaveTargetV1:
    kind: SearchedWaveTargetKindV1
    index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SearchedWaveTargetKindV1):
            raise TypeError("target kind must be SearchedWaveTargetKindV1")
        if self.kind is SearchedWaveTargetKindV1.INDEX:
            _nonnegative_int(self.index, "target index")
        elif self.index is not None:
            raise SearchedWaveProgramV1Error(
                "only INDEX targets may carry an index"
            )

    def to_dict(self) -> JSONMap:
        if self.kind is SearchedWaveTargetKindV1.INDEX:
            return {"kind": self.kind.value, "index": self.index}
        return {"kind": self.kind.value}


def searched_wave_target_from_dict_v1(value: object) -> SearchedWaveTargetV1:
    if not isinstance(value, Mapping):
        raise SearchedWaveProgramV1Error("target must be an object")
    kind = _enum(SearchedWaveTargetKindV1, value.get("kind"), "target kind")
    expected = {"kind", "index"} if kind is SearchedWaveTargetKindV1.INDEX else {"kind"}
    raw = _exact_mapping(value, expected, "target")
    return SearchedWaveTargetV1(kind=kind, index=raw.get("index"))


@dataclass(frozen=True)
class SearchedWaveStepV1:
    """One searched action or intentional wait with an explicit window."""

    step_id: str
    kind: SearchedWaveStepKindV1
    at_or_after_ms: int
    max_lateness_ms: int
    proposal_source: str
    action_key: str | None = None
    action_ref: ActionRef | None = None
    lane: str | None = None
    target: SearchedWaveTargetV1 | None = None
    guard: ObservableCausalGuardV1 | None = None
    wait_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "step_id"))
        if not isinstance(self.kind, SearchedWaveStepKindV1):
            raise TypeError("step kind must be SearchedWaveStepKindV1")
        _nonnegative_int(self.at_or_after_ms, "at_or_after_ms")
        _nonnegative_int(self.max_lateness_ms, "max_lateness_ms")
        object.__setattr__(
            self,
            "proposal_source",
            _text(self.proposal_source, "proposal_source"),
        )
        if self.kind is SearchedWaveStepKindV1.ACTION:
            object.__setattr__(
                self, "action_key", _text(self.action_key, "action_key")
            )
            _action(self.action_ref, "action_ref")
            if self.lane not in LANES:
                raise SearchedWaveProgramV1Error(
                    f"unsupported action lane {self.lane!r}"
                )
            if not isinstance(self.target, SearchedWaveTargetV1):
                raise TypeError("ACTION step target must be SearchedWaveTargetV1")
            if self.guard is not None and not isinstance(
                self.guard, ObservableCausalGuardV1
            ):
                raise TypeError("ACTION step guard must be ObservableCausalGuardV1")
            if self.wait_ms is not None:
                raise SearchedWaveProgramV1Error(
                    "ACTION step cannot carry wait_ms"
                )
        else:
            if any(
                value is not None
                for value in (
                    self.action_key,
                    self.action_ref,
                    self.lane,
                    self.target,
                    self.guard,
                )
            ):
                raise SearchedWaveProgramV1Error(
                    "WAIT step cannot carry action, target, lane, or guard"
                )
            _positive_int(self.wait_ms, "wait_ms")

    def to_dict(self) -> JSONMap:
        core: JSONMap = {
            "schema": STEP_SCHEMA,
            "step_id": self.step_id,
            "kind": self.kind.value,
            "at_or_after_ms": self.at_or_after_ms,
            "max_lateness_ms": self.max_lateness_ms,
            "proposal_source": self.proposal_source,
        }
        if self.kind is SearchedWaveStepKindV1.ACTION:
            core.update(
                {
                    "action_key": self.action_key,
                    "action_ref": self.action_ref.to_wire(),
                    "lane": self.lane,
                    "target": self.target.to_dict(),
                    "guard": self.guard.to_dict() if self.guard else None,
                }
            )
        else:
            core["wait_ms"] = self.wait_ms
        return core

    def behavior_dict(self) -> JSONMap:
        """Behavioral projection with identifiers and provenance removed."""

        wire = self.to_dict()
        wire.pop("schema")
        wire.pop("step_id")
        wire.pop("proposal_source")
        return wire


def searched_wave_step_from_dict_v1(value: object) -> SearchedWaveStepV1:
    _reject_per_seed_shape(value, "searched step")
    if not isinstance(value, Mapping):
        raise SearchedWaveProgramV1Error("searched step must be an object")
    kind = _enum(SearchedWaveStepKindV1, value.get("kind"), "step kind")
    common = {
        "schema",
        "step_id",
        "kind",
        "at_or_after_ms",
        "max_lateness_ms",
        "proposal_source",
    }
    if kind is SearchedWaveStepKindV1.ACTION:
        raw = _exact_mapping(
            value,
            common | {"action_key", "action_ref", "lane", "target", "guard"},
            "ACTION step",
        )
        guard_raw = raw["guard"]
        guard = (
            None
            if guard_raw is None
            else observable_causal_guard_from_dict_v1(
                guard_raw, label="searched step guard"
            )
        )
        kwargs: JSONMap = {
            "action_key": raw["action_key"],
            "action_ref": _action_from_wire(raw["action_ref"], "action_ref"),
            "lane": raw["lane"],
            "target": searched_wave_target_from_dict_v1(raw["target"]),
            "guard": guard,
        }
    else:
        raw = _exact_mapping(value, common | {"wait_ms"}, "WAIT step")
        kwargs = {"wait_ms": raw["wait_ms"]}
    if raw["schema"] != STEP_SCHEMA:
        raise SearchedWaveProgramV1Error("searched step schema is unsupported")
    return SearchedWaveStepV1(
        step_id=raw["step_id"],
        kind=kind,
        at_or_after_ms=raw["at_or_after_ms"],
        max_lateness_ms=raw["max_lateness_ms"],
        proposal_source=raw["proposal_source"],
        **kwargs,
    )


def _priority(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise SearchedWaveProgramV1Error(f"{label} must be an array")
    result = tuple(_text(value, f"{label} action") for value in values)
    if len(set(result)) != len(result):
        raise SearchedWaveProgramV1Error(f"{label} must be unique")
    return result


@dataclass(frozen=True)
class SearchedWaveProgramV1:
    program_id: str
    parent_program_id: str | None
    source_refs: tuple[str, ...]
    applied_edit_ids: tuple[str, ...]
    gap_behavior: SearchedWaveGapBehaviorV1
    steps: tuple[SearchedWaveStepV1, ...]
    tail_gcd_priority: tuple[str, ...]
    tail_queue_priority: tuple[str, ...]
    tail_off_gcd_once: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "program_id", _text(self.program_id, "program_id"))
        if self.parent_program_id is not None:
            object.__setattr__(
                self,
                "parent_program_id",
                _text(self.parent_program_id, "parent_program_id"),
            )
            if self.parent_program_id == self.program_id:
                raise SearchedWaveProgramV1Error(
                    "parent_program_id must differ from program_id"
                )
        for label, values in (
            ("source_refs", self.source_refs),
            ("applied_edit_ids", self.applied_edit_ids),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"{label} must be a tuple")
            normalized = tuple(_text(value, label) for value in values)
            if len(set(normalized)) != len(normalized):
                raise SearchedWaveProgramV1Error(f"{label} must be unique")
            object.__setattr__(self, label, normalized)
        if len(self.applied_edit_ids) > MAX_SUPPORTED_EDITS:
            raise SearchedWaveProgramV1Error(
                f"applied_edit_ids cannot exceed {MAX_SUPPORTED_EDITS}"
            )
        if not isinstance(self.gap_behavior, SearchedWaveGapBehaviorV1):
            raise TypeError("gap_behavior must be SearchedWaveGapBehaviorV1")
        if (
            not isinstance(self.steps, tuple)
            or not self.steps
            or any(not isinstance(step, SearchedWaveStepV1) for step in self.steps)
        ):
            raise TypeError("steps must be a nonempty tuple of searched steps")
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise SearchedWaveProgramV1Error("step IDs must be unique")
        times = [step.at_or_after_ms for step in self.steps]
        if times != sorted(times):
            raise SearchedWaveProgramV1Error(
                "steps must be ordered by nondecreasing at_or_after_ms"
            )
        if not any(step.kind is SearchedWaveStepKindV1.ACTION for step in self.steps):
            raise SearchedWaveProgramV1Error(
                "a complete-wave searched program needs an ACTION step"
            )
        for label in (
            "tail_gcd_priority",
            "tail_queue_priority",
            "tail_off_gcd_once",
        ):
            object.__setattr__(
                self, label, _priority(getattr(self, label), label)
            )
        if not self.tail_gcd_priority:
            raise SearchedWaveProgramV1Error(
                "a complete-wave searched program needs a GCD tail"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": PROGRAM_SCHEMA,
            "program_id": self.program_id,
            "parent_program_id": self.parent_program_id,
            "source_refs": list(self.source_refs),
            "applied_edit_ids": list(self.applied_edit_ids),
            "gap_behavior": self.gap_behavior.value,
            "steps": [step.to_dict() for step in self.steps],
            "tail_priorities": {
                "gcd": list(self.tail_gcd_priority),
                "queue": list(self.tail_queue_priority),
                "off_gcd_once": list(self.tail_off_gcd_once),
            },
            "contract": dict(_PROGRAM_CONTRACT),
        }

    def behavior_dict(self) -> JSONMap:
        return {
            "schema": BEHAVIOR_SCHEMA,
            "gap_behavior": self.gap_behavior.value,
            "steps": [step.behavior_dict() for step in self.steps],
            "tail_priorities": {
                "gcd": list(self.tail_gcd_priority),
                "queue": list(self.tail_queue_priority),
                "off_gcd_once": list(self.tail_off_gcd_once),
            },
        }

    def behavior_key(self) -> str:
        """Canonical behavior only; IDs and proposal provenance are absent."""

        return json.dumps(
            self.behavior_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def searched_wave_behavior_key_v1(program: SearchedWaveProgramV1) -> str:
    if not isinstance(program, SearchedWaveProgramV1):
        raise TypeError("program must be SearchedWaveProgramV1")
    return program.behavior_key()


def searched_wave_program_from_dict_v1(value: object) -> SearchedWaveProgramV1:
    _reject_per_seed_shape(value, "searched program")
    raw = _exact_mapping(
        value,
        {
            "schema",
            "program_id",
            "parent_program_id",
            "source_refs",
            "applied_edit_ids",
            "gap_behavior",
            "steps",
            "tail_priorities",
            "contract",
        },
        "searched program",
    )
    if raw["schema"] != PROGRAM_SCHEMA:
        raise SearchedWaveProgramV1Error("searched program schema is unsupported")
    if raw["contract"] != _PROGRAM_CONTRACT:
        raise SearchedWaveProgramV1Error(
            "searched program contract differs from v1"
        )
    source_refs = raw["source_refs"]
    edit_ids = raw["applied_edit_ids"]
    steps = raw["steps"]
    if not isinstance(source_refs, list):
        raise SearchedWaveProgramV1Error("source_refs must be an array")
    if not isinstance(edit_ids, list):
        raise SearchedWaveProgramV1Error("applied_edit_ids must be an array")
    if not isinstance(steps, list):
        raise SearchedWaveProgramV1Error("steps must be an array")
    tails = _exact_mapping(
        raw["tail_priorities"], {"gcd", "queue", "off_gcd_once"}, "tail priorities"
    )
    return SearchedWaveProgramV1(
        program_id=raw["program_id"],
        parent_program_id=raw["parent_program_id"],
        source_refs=tuple(source_refs),
        applied_edit_ids=tuple(edit_ids),
        gap_behavior=_enum(
            SearchedWaveGapBehaviorV1, raw["gap_behavior"], "gap_behavior"
        ),
        steps=tuple(searched_wave_step_from_dict_v1(step) for step in steps),
        tail_gcd_priority=_priority(tails["gcd"], "tail gcd priority"),
        tail_queue_priority=_priority(tails["queue"], "tail queue priority"),
        tail_off_gcd_once=_priority(
            tails["off_gcd_once"], "tail off-GCD priority"
        ),
    )


@dataclass(frozen=True)
class SearchedWaveEditV1:
    edit_id: str
    kind: SearchedWaveEditKindV1
    proposal_source: str
    target_step_id: str | None = None
    step: SearchedWaveStepV1 | None = None
    new_step_id: str | None = None
    at_or_after_ms: int | None = None
    max_lateness_ms: int | None = None
    target: SearchedWaveTargetV1 | None = None
    guard: ObservableCausalGuardV1 | None = None
    tail_lane: str | None = None
    tail_priority: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edit_id", _text(self.edit_id, "edit_id"))
        if not isinstance(self.kind, SearchedWaveEditKindV1):
            raise TypeError("edit kind must be SearchedWaveEditKindV1")
        object.__setattr__(
            self,
            "proposal_source",
            _text(self.proposal_source, "proposal_source"),
        )
        if self.target_step_id is not None:
            object.__setattr__(
                self,
                "target_step_id",
                _text(self.target_step_id, "target_step_id"),
            )
        if self.new_step_id is not None:
            object.__setattr__(
                self, "new_step_id", _text(self.new_step_id, "new_step_id")
            )
        if self.at_or_after_ms is not None:
            _nonnegative_int(self.at_or_after_ms, "edit at_or_after_ms")
        if self.max_lateness_ms is not None:
            _nonnegative_int(self.max_lateness_ms, "edit max_lateness_ms")
        if self.step is not None and not isinstance(self.step, SearchedWaveStepV1):
            raise TypeError("edit step must be SearchedWaveStepV1")
        if self.target is not None and not isinstance(
            self.target, SearchedWaveTargetV1
        ):
            raise TypeError("edit target must be SearchedWaveTargetV1")
        if self.guard is not None and not isinstance(
            self.guard, ObservableCausalGuardV1
        ):
            raise TypeError("edit guard must be ObservableCausalGuardV1")
        if (
            self.kind is not SearchedWaveEditKindV1.REPLACE_GUARD
            and self.guard is not None
        ):
            raise SearchedWaveProgramV1Error(
                f"{self.kind.value} cannot carry guard"
            )

        target_kinds = {
            SearchedWaveEditKindV1.INSERT_BEFORE,
            SearchedWaveEditKindV1.DELETE,
            SearchedWaveEditKindV1.REPLACE,
            SearchedWaveEditKindV1.REPEAT_AFTER,
            SearchedWaveEditKindV1.RETIME,
            SearchedWaveEditKindV1.RETARGET,
            SearchedWaveEditKindV1.REPLACE_GUARD,
        }
        if (self.kind in target_kinds) != (self.target_step_id is not None):
            raise SearchedWaveProgramV1Error(
                "target_step_id is required exactly for step edits"
            )
        if self.kind in {
            SearchedWaveEditKindV1.INSERT_BEFORE,
            SearchedWaveEditKindV1.REPLACE,
        }:
            if self.step is None:
                raise SearchedWaveProgramV1Error(
                    f"{self.kind.value} requires step"
                )
        elif self.step is not None:
            raise SearchedWaveProgramV1Error(
                f"{self.kind.value} cannot carry step"
            )
        if self.kind is SearchedWaveEditKindV1.REPEAT_AFTER:
            if self.new_step_id is None:
                raise SearchedWaveProgramV1Error(
                    "REPEAT_AFTER requires new_step_id"
                )
        elif self.new_step_id is not None:
            raise SearchedWaveProgramV1Error(
                f"{self.kind.value} cannot carry new_step_id"
            )
        timing_present = (
            self.at_or_after_ms is not None or self.max_lateness_ms is not None
        )
        if self.kind is SearchedWaveEditKindV1.RETIME:
            if self.at_or_after_ms is None or self.max_lateness_ms is None:
                raise SearchedWaveProgramV1Error(
                    "RETIME requires a complete execution window"
                )
        elif timing_present:
            raise SearchedWaveProgramV1Error(
                f"{self.kind.value} cannot carry timing fields"
            )
        if (self.kind is SearchedWaveEditKindV1.RETARGET) != (
            self.target is not None
        ):
            raise SearchedWaveProgramV1Error(
                "target is required exactly for RETARGET"
            )
        if self.kind is SearchedWaveEditKindV1.TAIL_REORDER:
            if self.tail_lane not in LANES:
                raise SearchedWaveProgramV1Error(
                    "TAIL_REORDER requires a supported tail_lane"
                )
            object.__setattr__(
                self,
                "tail_priority",
                _priority(self.tail_priority, "tail_priority"),
            )
        elif self.tail_lane is not None or self.tail_priority is not None:
            raise SearchedWaveProgramV1Error(
                f"{self.kind.value} cannot carry tail fields"
            )

    def to_dict(self) -> JSONMap:
        result: JSONMap = {
            "schema": EDIT_SCHEMA,
            "edit_id": self.edit_id,
            "kind": self.kind.value,
            "proposal_source": self.proposal_source,
        }
        if self.target_step_id is not None:
            result["target_step_id"] = self.target_step_id
        if self.step is not None:
            result["step"] = self.step.to_dict()
        if self.new_step_id is not None:
            result["new_step_id"] = self.new_step_id
        if self.kind is SearchedWaveEditKindV1.RETIME:
            result["at_or_after_ms"] = self.at_or_after_ms
            result["max_lateness_ms"] = self.max_lateness_ms
        if self.target is not None:
            result["target"] = self.target.to_dict()
        if self.kind is SearchedWaveEditKindV1.REPLACE_GUARD:
            result["guard"] = self.guard.to_dict() if self.guard else None
        if self.kind is SearchedWaveEditKindV1.TAIL_REORDER:
            result["tail_lane"] = self.tail_lane
            result["tail_priority"] = list(self.tail_priority)
        return result


def searched_wave_edit_from_dict_v1(value: object) -> SearchedWaveEditV1:
    _reject_per_seed_shape(value, "searched edit")
    if not isinstance(value, Mapping):
        raise SearchedWaveProgramV1Error("searched edit must be an object")
    kind = _enum(SearchedWaveEditKindV1, value.get("kind"), "edit kind")
    fields = {"schema", "edit_id", "kind", "proposal_source"}
    if kind is SearchedWaveEditKindV1.TAIL_REORDER:
        fields |= {"tail_lane", "tail_priority"}
    else:
        fields.add("target_step_id")
        if kind in {
            SearchedWaveEditKindV1.INSERT_BEFORE,
            SearchedWaveEditKindV1.REPLACE,
        }:
            fields.add("step")
        elif kind is SearchedWaveEditKindV1.REPEAT_AFTER:
            fields.add("new_step_id")
        elif kind is SearchedWaveEditKindV1.RETIME:
            fields |= {"at_or_after_ms", "max_lateness_ms"}
        elif kind is SearchedWaveEditKindV1.RETARGET:
            fields.add("target")
        elif kind is SearchedWaveEditKindV1.REPLACE_GUARD:
            fields.add("guard")
    raw = _exact_mapping(value, fields, "searched edit")
    if raw["schema"] != EDIT_SCHEMA:
        raise SearchedWaveProgramV1Error("searched edit schema is unsupported")
    kwargs: JSONMap = {}
    if "target_step_id" in raw:
        kwargs["target_step_id"] = raw["target_step_id"]
    if "step" in raw:
        kwargs["step"] = searched_wave_step_from_dict_v1(raw["step"])
    if "new_step_id" in raw:
        kwargs["new_step_id"] = raw["new_step_id"]
    if kind is SearchedWaveEditKindV1.RETIME:
        kwargs["at_or_after_ms"] = raw["at_or_after_ms"]
        kwargs["max_lateness_ms"] = raw["max_lateness_ms"]
    if "target" in raw:
        kwargs["target"] = searched_wave_target_from_dict_v1(raw["target"])
    if kind is SearchedWaveEditKindV1.REPLACE_GUARD:
        kwargs["guard"] = (
            None
            if raw["guard"] is None
            else observable_causal_guard_from_dict_v1(
                raw["guard"], label="replacement guard"
            )
        )
    if kind is SearchedWaveEditKindV1.TAIL_REORDER:
        kwargs["tail_lane"] = raw["tail_lane"]
        kwargs["tail_priority"] = _priority(
            raw["tail_priority"], "tail_priority"
        )
    return SearchedWaveEditV1(
        edit_id=raw["edit_id"],
        kind=kind,
        proposal_source=raw["proposal_source"],
        **kwargs,
    )


def _target_for_offline_action(action: Any) -> SearchedWaveTargetV1:
    if action.target_role == TARGET_SELF:
        return SearchedWaveTargetV1(SearchedWaveTargetKindV1.SELF)
    if action.target_role == TARGET_OTHER and action.source_target_ordinal is not None:
        return SearchedWaveTargetV1(
            SearchedWaveTargetKindV1.INDEX, action.source_target_ordinal
        )
    return SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT)


def searched_wave_program_from_offline_policy_v1(
    policy: OfflineWaveFeedbackPolicyV1,
    *,
    program_id: str,
    gap_behavior: SearchedWaveGapBehaviorV1 = (
        SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
    ),
    action_max_lateness_ms: int = 500,
) -> SearchedWaveProgramV1:
    """Create a searched guide without changing the observed-policy object.

    Source timestamps initialize proposal windows.  They are not copied as
    evidence labels, and inter-step gaps default to tail fill rather than being
    interpreted as intentional idle decisions.
    """

    if not isinstance(policy, OfflineWaveFeedbackPolicyV1):
        raise TypeError("policy must be OfflineWaveFeedbackPolicyV1")
    if not isinstance(gap_behavior, SearchedWaveGapBehaviorV1):
        raise TypeError("gap_behavior must be SearchedWaveGapBehaviorV1")
    _nonnegative_int(action_max_lateness_ms, "action_max_lateness_ms")
    steps = tuple(
        SearchedWaveStepV1(
            step_id=f"step-{ordinal:04d}",
            kind=SearchedWaveStepKindV1.ACTION,
            at_or_after_ms=action.at_or_after_ms,
            max_lateness_ms=action_max_lateness_ms,
            proposal_source="OFFLINE_POLICY_ACTION_GUIDE",
            action_key=action.action_key,
            action_ref=action.action_ref,
            lane=action.lane,
            target=_target_for_offline_action(action),
        )
        for ordinal, action in enumerate(policy.actions)
    )
    return SearchedWaveProgramV1(
        program_id=program_id,
        parent_program_id=None,
        source_refs=(policy.policy_id,),
        applied_edit_ids=(),
        gap_behavior=gap_behavior,
        steps=steps,
        tail_gcd_priority=policy.tail_gcd_priority,
        tail_queue_priority=policy.tail_queue_priority,
        tail_off_gcd_once=policy.tail_off_gcd_once,
    )


def _validate_edit_batch(
    base: SearchedWaveProgramV1,
    edits: tuple[SearchedWaveEditV1, ...],
) -> None:
    edit_ids = [edit.edit_id for edit in edits]
    if len(set(edit_ids)) != len(edit_ids):
        raise SearchedWaveProgramV1Error("edit IDs must be unique")
    base_ids = {step.step_id for step in base.steps}
    targeted: set[str] = set()
    created: set[str] = set()
    tail_lanes: set[str] = set()
    for edit in edits:
        if edit.target_step_id is not None:
            if edit.target_step_id not in base_ids:
                raise SearchedWaveProgramV1Error(
                    f"edit target {edit.target_step_id!r} is absent from parent"
                )
            if edit.target_step_id in targeted:
                raise SearchedWaveProgramV1Error(
                    f"conflicting edits target {edit.target_step_id!r}"
                )
            targeted.add(edit.target_step_id)
        if edit.kind is SearchedWaveEditKindV1.INSERT_BEFORE:
            created_id = edit.step.step_id
        elif edit.kind is SearchedWaveEditKindV1.REPEAT_AFTER:
            created_id = edit.new_step_id
        else:
            created_id = None
        if created_id is not None:
            if created_id in base_ids or created_id in created:
                raise SearchedWaveProgramV1Error(
                    f"created step ID {created_id!r} is not new and unique"
                )
            created.add(created_id)
        if edit.kind is SearchedWaveEditKindV1.REPLACE:
            if edit.step.step_id != edit.target_step_id:
                raise SearchedWaveProgramV1Error(
                    "REPLACE must preserve the stable target step ID"
                )
        if edit.kind is SearchedWaveEditKindV1.TAIL_REORDER:
            if edit.tail_lane in tail_lanes:
                raise SearchedWaveProgramV1Error(
                    f"conflicting tail edits target lane {edit.tail_lane!r}"
                )
            tail_lanes.add(edit.tail_lane)


def materialize_searched_wave_program_v1(
    base: SearchedWaveProgramV1,
    edits: Sequence[SearchedWaveEditV1],
    *,
    program_id: str,
    max_edits: int = 2,
    source_refs: Sequence[str] = (),
) -> SearchedWaveProgramV1:
    """Apply one frozen, conflict-free bounded edit batch to ``base``."""

    if not isinstance(base, SearchedWaveProgramV1):
        raise TypeError("base must be SearchedWaveProgramV1")
    _positive_int(max_edits, "max_edits")
    if max_edits > MAX_SUPPORTED_EDITS:
        raise SearchedWaveProgramV1Error(
            f"max_edits cannot exceed {MAX_SUPPORTED_EDITS}"
        )
    if isinstance(edits, (str, bytes, bytearray)) or not isinstance(
        edits, Sequence
    ):
        raise TypeError("edits must be a sequence")
    frozen_edits = tuple(edits)
    if any(not isinstance(edit, SearchedWaveEditV1) for edit in frozen_edits):
        raise TypeError("edits must contain SearchedWaveEditV1 values")
    if len(frozen_edits) > max_edits:
        raise SearchedWaveProgramV1Error(
            f"candidate has {len(frozen_edits)} edits; limit is {max_edits}"
        )
    _validate_edit_batch(base, frozen_edits)

    steps = list(base.steps)
    tails = {
        LANE_GCD: base.tail_gcd_priority,
        LANE_QUEUE: base.tail_queue_priority,
        LANE_OFF_GCD: base.tail_off_gcd_once,
    }
    for edit in frozen_edits:
        if edit.kind is SearchedWaveEditKindV1.TAIL_REORDER:
            existing = tails[edit.tail_lane]
            if len(edit.tail_priority) != len(existing) or set(
                edit.tail_priority
            ) != set(existing):
                raise SearchedWaveProgramV1Error(
                    "TAIL_REORDER must be an exact permutation of its parent tail"
                )
            tails[edit.tail_lane] = edit.tail_priority
            continue
        index = next(
            index
            for index, step in enumerate(steps)
            if step.step_id == edit.target_step_id
        )
        current = steps[index]
        if edit.kind is SearchedWaveEditKindV1.INSERT_BEFORE:
            steps.insert(index, edit.step)
        elif edit.kind is SearchedWaveEditKindV1.DELETE:
            steps.pop(index)
        elif edit.kind is SearchedWaveEditKindV1.REPLACE:
            steps[index] = edit.step
        elif edit.kind is SearchedWaveEditKindV1.REPEAT_AFTER:
            steps.insert(
                index + 1,
                replace(
                    current,
                    step_id=edit.new_step_id,
                    proposal_source=edit.proposal_source,
                ),
            )
        elif edit.kind is SearchedWaveEditKindV1.RETIME:
            steps[index] = replace(
                current,
                at_or_after_ms=edit.at_or_after_ms,
                max_lateness_ms=edit.max_lateness_ms,
                proposal_source=edit.proposal_source,
            )
        elif edit.kind is SearchedWaveEditKindV1.RETARGET:
            if current.kind is not SearchedWaveStepKindV1.ACTION:
                raise SearchedWaveProgramV1Error(
                    "RETARGET requires an ACTION step"
                )
            steps[index] = replace(
                current,
                target=edit.target,
                proposal_source=edit.proposal_source,
            )
        elif edit.kind is SearchedWaveEditKindV1.REPLACE_GUARD:
            if current.kind is not SearchedWaveStepKindV1.ACTION:
                raise SearchedWaveProgramV1Error(
                    "REPLACE_GUARD requires an ACTION step"
                )
            steps[index] = replace(
                current,
                guard=edit.guard,
                proposal_source=edit.proposal_source,
            )
        else:  # pragma: no cover - enum exhaustiveness
            raise AssertionError(f"unhandled edit kind {edit.kind}")

    if isinstance(source_refs, (str, bytes, bytearray)) or not isinstance(
        source_refs, Sequence
    ):
        raise TypeError("source_refs must be a sequence")
    extra_refs = tuple(_text(value, "source_ref") for value in source_refs)
    combined_refs = tuple(dict.fromkeys((*base.source_refs, *extra_refs)))
    return SearchedWaveProgramV1(
        program_id=program_id,
        parent_program_id=base.program_id,
        source_refs=combined_refs,
        applied_edit_ids=tuple(edit.edit_id for edit in frozen_edits),
        gap_behavior=base.gap_behavior,
        steps=tuple(steps),
        tail_gcd_priority=tails[LANE_GCD],
        tail_queue_priority=tails[LANE_QUEUE],
        tail_off_gcd_once=tails[LANE_OFF_GCD],
    )


__all__ = (
    "BEHAVIOR_SCHEMA",
    "EDIT_SCHEMA",
    "MAX_SUPPORTED_EDITS",
    "PROGRAM_SCHEMA",
    "STEP_SCHEMA",
    "SearchedWaveEditKindV1",
    "SearchedWaveEditV1",
    "SearchedWaveGapBehaviorV1",
    "SearchedWaveProgramV1",
    "SearchedWaveProgramV1Error",
    "SearchedWaveStepKindV1",
    "SearchedWaveStepV1",
    "SearchedWaveTargetKindV1",
    "SearchedWaveTargetV1",
    "materialize_searched_wave_program_v1",
    "searched_wave_behavior_key_v1",
    "searched_wave_edit_from_dict_v1",
    "searched_wave_program_from_dict_v1",
    "searched_wave_program_from_offline_policy_v1",
    "searched_wave_step_from_dict_v1",
    "searched_wave_target_from_dict_v1",
)
