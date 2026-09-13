"""Compile weighted Fury observation prototypes into distinct clone models.

V2 consumes the five partitions prepared by
``historical_fury_behavior_prototypes_v1``.  Recognized controllable START
proxies alone contribute marks, targets, and completed inter-decision delays.
Usable terminal tails contribute genuine right-censored durations to a
weighted discrete-time product-limit representation.  Left-truncated first
STARTs and tails with no authoritative origin are retained in accounting but
never converted into completed intervals, censors, or pseudo-actions.

All source weights are parsed as exact ``Fraction`` values.  The output models
remain development-only artifacts.  An additive typed simulator adapter may
consume their bounded-float projection, but that does not authorize a
comparison or a claim about the original player's full client policy.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence, TextIO

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import historical_behavior_clone_v1 as clone_v1
from . import historical_fury_behavior_prototypes_v1 as prototypes_v1


JSONMap = dict[str, Any]
MODEL_SCHEMA = "historical_behavior_clone/v2"
MANIFEST_SCHEMA = "historical_behavior_clone_model_manifest/v2"
IMPLEMENTATION_REVISION = (
    "v2.2_exact_revision_and_product_limit_closure"
)
STATUS = "DEVELOPMENT_ONLY_WEIGHTED_PROTOTYPE_NOT_COMPARISON_AUTHORIZED"
RUNTIME_PROJECTION_SCHEMA = "historical_behavior_clone_runtime_projection/v1"
START_ACTION = clone_v1.START_ACTION
ACTION_KEYS = clone_v1.ACTION_KEYS
ACTION_SPEC_BY_KEY = clone_v1.ACTION_SPEC_BY_KEY
DELAY_UPPER_BOUNDS_MS = clone_v1.DELAY_UPPER_BOUNDS_MS
TARGET_ROLES = clone_v1.TARGET_ROLES + ("UNRESOLVED_NONVOTING_TARGET",)
QUEUE_INTENT_STATUS = prototypes_v1.QUEUE_INTENT_STATUS
DEFAULT_BACKOFF_STRENGTH = Fraction(1, 4)
DELAY_WEIGHT_CONTRACT = (
    "PLAYER_EQUAL_THEN_RAID_EQUAL_WITHIN_PLAYER_THEN_USABLE_INTER_DECISION_"
    "INTERVAL_EQUAL_WITHIN_PLAYER_RAID"
)
COMPLETED_DELAY = "COMPLETED"
LEFT_TRUNCATED_DELAY = "LEFT_TRUNCATED_NOT_COMPLETED"
RIGHT_CENSORED_DELAY = "RIGHT_CENSORED"
UNUSABLE_TRUNCATED_TAIL = "LEFT_AND_RIGHT_TRUNCATED_NOT_USABLE"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_PROTOTYPE_MANIFEST = prototypes_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "historical_behavior_clone_models" / "v2"
)


class HistoricalBehaviorCloneV2Error(RuntimeError):
    """A weighted prototype, censoring record, or model contract is invalid."""


@dataclass(frozen=True)
class CloneV2BuildResult:
    manifest: Path
    content_addressed_manifest: Path
    model_count: int
    weighted_start_observation_count: int
    completed_delay_observation_count: int
    usable_right_censor_count: int
    left_truncated_first_start_count: int
    unusable_left_right_truncated_tail_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": MANIFEST_SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "model_count": self.model_count,
            "weighted_start_observation_count": (
                self.weighted_start_observation_count
            ),
            "completed_delay_observation_count": (
                self.completed_delay_observation_count
            ),
            "usable_right_censor_count": self.usable_right_censor_count,
            "left_truncated_first_start_count": (
                self.left_truncated_first_start_count
            ),
            "unusable_left_right_truncated_tail_count": (
                self.unusable_left_right_truncated_tail_count
            ),
        }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalBehaviorCloneV2Error(f"{label} must be nonempty text")
    return value.strip()


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise HistoricalBehaviorCloneV2Error(f"{label} must be at least {minimum}")
    return value


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalBehaviorCloneV2Error(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalBehaviorCloneV2Error(f"cannot read {label}: {error}") from error
    return deepcopy(dict(_mapping(value, label)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalBehaviorCloneV2Error(f"cannot read {path}: {error}") from error
    return digest.hexdigest()


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _under_offline_data(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise HistoricalBehaviorCloneV2Error(
            "generated clone output must stay under ignored offline_data"
        )
    return resolved


def _fraction_wire(value: Fraction) -> JSONMap:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "value": float(value),
    }


def _fraction_from_wire(value: Any, label: str) -> Fraction:
    row = _mapping(value, label)
    numerator = _integer(row.get("numerator"), f"{label}.numerator", minimum=0)
    denominator = _integer(
        row.get("denominator"), f"{label}.denominator", minimum=1
    )
    result = Fraction(numerator, denominator)
    raw_float = row.get("value")
    if isinstance(raw_float, bool) or not isinstance(raw_float, (int, float)):
        raise HistoricalBehaviorCloneV2Error(f"{label}.value must be numeric")
    if not math.isfinite(float(raw_float)) or not math.isclose(
        float(raw_float), float(result), rel_tol=1e-12, abs_tol=1e-15
    ):
        raise HistoricalBehaviorCloneV2Error(
            f"{label}.value differs from its exact rational"
        )
    return result


def _fraction_counter_wire(
    counts: Mapping[str, Fraction], keys: Sequence[str]
) -> JSONMap:
    return {key: _fraction_wire(counts.get(key, Fraction())) for key in keys}


def _bounded_float(value: Fraction, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise HistoricalBehaviorCloneV2Error(
            f"{label} cannot be represented as a finite binary64 value"
        )
    return result


def _float_counter(
    counts: Mapping[str, Fraction], keys: Sequence[str], label: str
) -> JSONMap:
    return {
        key: _bounded_float(counts.get(key, Fraction()), f"{label}[{key}]")
        for key in keys
    }


def _float_survival_projection(cell: Mapping[str, Any]) -> JSONMap:
    rows = []
    for raw in _array(cell.get("buckets"), "delay survival buckets"):
        row = _mapping(raw, "delay survival bucket")
        rows.append(
            {
                "bucket": row.get("bucket"),
                "lower_bound_ms": row.get("lower_bound_ms"),
                "upper_bound_ms": row.get("upper_bound_ms"),
                "at_risk_weight": float(
                    _mapping(row.get("at_risk_weight"), "at_risk_weight")["value"]
                ),
                "event_weight": float(
                    _mapping(row.get("event_weight"), "event_weight")["value"]
                ),
                "censor_weight": float(
                    _mapping(row.get("censor_weight"), "censor_weight")["value"]
                ),
                "event_hazard": float(
                    _mapping(row.get("event_hazard"), "event_hazard")["value"]
                ),
                "survival_before": float(
                    _mapping(row.get("survival_before"), "survival_before")["value"]
                ),
                "event_probability_mass": float(
                    _mapping(
                        row.get("event_probability_mass"),
                        "event_probability_mass",
                    )["value"]
                ),
                "survival_after": float(
                    _mapping(row.get("survival_after"), "survival_after")["value"]
                ),
                "event_representative_ms": row.get("event_representative_ms"),
            }
        )
    return {
        "event_weight": float(
            _mapping(cell.get("event_weight"), "delay event_weight")["value"]
        ),
        "censor_weight": float(
            _mapping(cell.get("censor_weight"), "delay censor_weight")["value"]
        ),
        "residual_survival_after_last_bucket": float(
            _mapping(
                cell.get("residual_survival_after_last_bucket"),
                "delay residual survival",
            )["value"]
        ),
        "buckets": rows,
    }


def _delay_bucket_index(duration_ms: int) -> int:
    value = _integer(duration_ms, "delay duration_ms", minimum=0)
    for index, upper in enumerate(DELAY_UPPER_BOUNDS_MS):
        if value <= upper:
            return index
    return len(DELAY_UPPER_BOUNDS_MS)


def _delay_bucket_rows() -> list[tuple[str, int, int | None]]:
    result = []
    lower = 0
    for upper in DELAY_UPPER_BOUNDS_MS:
        result.append((f"LE_{upper}", lower, upper))
        lower = upper + 1
    result.append((f"GT_{DELAY_UPPER_BOUNDS_MS[-1]}", lower, None))
    return result


@dataclass
class _SurvivalCell:
    at_risk: list[Fraction] = field(
        default_factory=lambda: [Fraction() for _ in _delay_bucket_rows()]
    )
    events: list[Fraction] = field(
        default_factory=lambda: [Fraction() for _ in _delay_bucket_rows()]
    )
    censors: list[Fraction] = field(
        default_factory=lambda: [Fraction() for _ in _delay_bucket_rows()]
    )
    event_duration_sums: list[Fraction] = field(
        default_factory=lambda: [Fraction() for _ in _delay_bucket_rows()]
    )
    event_observation_count: int = 0
    censor_observation_count: int = 0

    def _risk(self, terminal: int, weight: Fraction) -> None:
        if weight <= 0:
            raise HistoricalBehaviorCloneV2Error(
                "delay observation weight must be positive"
            )
        for index in range(terminal + 1):
            self.at_risk[index] += weight

    def add_event(self, duration_ms: int, weight: Fraction) -> None:
        terminal = _delay_bucket_index(duration_ms)
        self._risk(terminal, weight)
        self.events[terminal] += weight
        self.event_duration_sums[terminal] += weight * duration_ms
        self.event_observation_count += 1

    def add_censor(self, duration_ms: int, weight: Fraction) -> None:
        terminal = _delay_bucket_index(duration_ms)
        self._risk(terminal, weight)
        self.censors[terminal] += weight
        self.censor_observation_count += 1

    @property
    def event_weight(self) -> Fraction:
        return sum(self.events, Fraction())

    @property
    def censor_weight(self) -> Fraction:
        return sum(self.censors, Fraction())

    def wire(self) -> JSONMap:
        survival = Fraction(1, 1)
        rows = []
        for index, (bucket, lower, upper) in enumerate(_delay_bucket_rows()):
            risk = self.at_risk[index]
            event = self.events[index]
            censor = self.censors[index]
            if event + censor > risk:
                raise HistoricalBehaviorCloneV2Error(
                    "terminal event/censor weight exceeds bucket risk set"
                )
            hazard = Fraction() if risk == 0 else event / risk
            survival_before = survival
            event_mass = survival_before * hazard
            survival *= 1 - hazard
            representative = None
            representative_exact = None
            if event:
                representative_exact = self.event_duration_sums[index] / event
                representative = int(round(representative_exact))
            rows.append(
                {
                    "bucket": bucket,
                    "lower_bound_ms": lower,
                    "upper_bound_ms": upper,
                    "at_risk_weight": _fraction_wire(risk),
                    "event_weight": _fraction_wire(event),
                    "censor_weight": _fraction_wire(censor),
                    "event_hazard": _fraction_wire(hazard),
                    "survival_before": _fraction_wire(survival_before),
                    "event_probability_mass": _fraction_wire(event_mass),
                    "survival_after": _fraction_wire(survival),
                    "event_representative_ms": representative,
                    "event_representative_ms_exact": (
                        None
                        if representative_exact is None
                        else _fraction_wire(representative_exact)
                    ),
                }
            )
        return {
            "event_observation_count": self.event_observation_count,
            "censor_observation_count": self.censor_observation_count,
            "event_weight": _fraction_wire(self.event_weight),
            "censor_weight": _fraction_wire(self.censor_weight),
            "buckets": rows,
            "residual_survival_after_last_bucket": _fraction_wire(survival),
        }


@dataclass(frozen=True)
class _Support:
    candidate_id: str
    player_count: int
    raid_count: int
    recognized_start_count: int
    wave_count: int
    completed_delay_count: int
    left_truncated_first_start_count: int
    usable_right_censor_count: int
    unusable_left_right_truncated_tail_count: int

    @property
    def start_weight(self) -> Fraction:
        return Fraction(
            1, self.player_count * self.raid_count * self.recognized_start_count
        )

    @property
    def delay_weight(self) -> Fraction:
        interval_count = self.completed_delay_count + self.usable_right_censor_count
        return Fraction(1, self.player_count * self.raid_count * interval_count)


def _support_index(prototype: Mapping[str, Any]) -> dict[tuple[str, str], _Support]:
    members = _array(prototype.get("members"), "prototype.members")
    support_rows = _array(
        prototype.get("observation_support"), "prototype.observation_support"
    )
    player_count = _integer(
        prototype.get("member_count"), "prototype.member_count", minimum=1
    )
    if player_count != len(members) or len(support_rows) != player_count:
        raise HistoricalBehaviorCloneV2Error(
            "prototype member/support cardinality differs"
        )
    member_by_guid = {}
    for raw_member in members:
        member = _mapping(raw_member, "prototype member")
        guid = _text(member.get("character_guid"), "member.character_guid").casefold()
        if guid in member_by_guid:
            raise HistoricalBehaviorCloneV2Error("duplicate prototype member GUID")
        member_by_guid[guid] = member

    result = {}
    for raw_player in support_rows:
        player = _mapping(raw_player, "observation support player")
        guid = _text(player.get("character_guid"), "support.character_guid").casefold()
        candidate_id = _text(player.get("candidate_id"), "support.candidate_id")
        member = _mapping(member_by_guid.get(guid), "matching prototype member")
        if member.get("candidate_id") != candidate_id:
            raise HistoricalBehaviorCloneV2Error(
                "observation support candidate differs from membership"
            )
        raid_count = _integer(member.get("raid_count"), "member.raid_count", minimum=1)
        raids = _array(player.get("raids"), "support.raids")
        if len(raids) != raid_count:
            raise HistoricalBehaviorCloneV2Error(
                "observation support raid count differs from membership"
            )
        declared_raid_ids = {
            _text(value, "member.raid_ids[]")
            for value in _array(member.get("raid_ids"), "member.raid_ids")
        }
        support_raid_ids = {
            _text(_mapping(value, "support raid").get("instance_id"), "support.instance_id")
            for value in raids
        }
        if len(declared_raid_ids) != raid_count or declared_raid_ids != support_raid_ids:
            raise HistoricalBehaviorCloneV2Error(
                "observation support raid identities differ from membership"
            )
        for raw_raid in raids:
            raid = _mapping(raw_raid, "support raid")
            instance_id = _text(raid.get("instance_id"), "support.instance_id")
            key = (guid, instance_id)
            if key in result:
                raise HistoricalBehaviorCloneV2Error(
                    "duplicate prototype player-raid support"
                )
            recognized_start_count = _integer(
                raid.get("recognized_start_count"),
                "support.recognized_start_count",
                minimum=1,
            )
            wave_count = _integer(
                raid.get("wave_count"), "support.wave_count", minimum=1
            )
            completed_delay_count = _integer(
                raid.get("completed_delay_count"),
                "support.completed_delay_count",
                minimum=0,
            )
            left_truncated_count = _integer(
                raid.get("left_truncated_first_start_count"),
                "support.left_truncated_first_start_count",
                minimum=0,
            )
            usable_censor_count = _integer(
                raid.get("usable_right_censor_count"),
                "support.usable_right_censor_count",
                minimum=0,
            )
            unusable_tail_count = _integer(
                raid.get("unusable_left_right_truncated_tail_count"),
                "support.unusable_left_right_truncated_tail_count",
                minimum=0,
            )
            if completed_delay_count + left_truncated_count != recognized_start_count:
                raise HistoricalBehaviorCloneV2Error(
                    "completed and left-truncated START counts do not close"
                )
            if usable_censor_count + unusable_tail_count != wave_count:
                raise HistoricalBehaviorCloneV2Error(
                    "usable and unusable tail counts do not close"
                )
            if completed_delay_count + usable_censor_count <= 0:
                raise HistoricalBehaviorCloneV2Error(
                    "player-raid has no usable inter-decision delay observation"
                )
            result[key] = _Support(
                candidate_id=candidate_id,
                player_count=player_count,
                raid_count=raid_count,
                recognized_start_count=recognized_start_count,
                wave_count=wave_count,
                completed_delay_count=completed_delay_count,
                left_truncated_first_start_count=left_truncated_count,
                usable_right_censor_count=usable_censor_count,
                unusable_left_right_truncated_tail_count=unusable_tail_count,
            )
    if set(member_by_guid) != {guid for guid, _ in result}:
        raise HistoricalBehaviorCloneV2Error(
            "one or more prototype members have no observation support"
        )
    return result


def _source_key(source: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(source.get("episode_id"), "source.episode_id"),
        _text(source.get("wave_id"), "source.wave_id"),
        _text(source.get("player_guid"), "source.player_guid").casefold(),
        _text(source.get("instance_id"), "source.instance_id"),
    )


def _validate_common_record(
    row: Mapping[str, Any], prototype_id: str
) -> tuple[Mapping[str, Any], tuple[str, str, str, str]]:
    if row.get("schema") != prototypes_v1.RECORD_SCHEMA:
        raise HistoricalBehaviorCloneV2Error("prototype record schema differs")
    if row.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneV2Error(
            "prototype record implementation_revision differs"
        )
    if row.get("prototype_id") != prototype_id:
        raise HistoricalBehaviorCloneV2Error(
            "prototype record identity crosses model partitions"
        )
    source = _mapping(row.get("source"), "record.source")
    _mapping(source.get("episode_window_coverage"), "episode_window_coverage")
    boundaries = _mapping(row.get("observation_boundaries"), "observation_boundaries")
    if (
        boundaries.get("go_fail_are_labels") is not False
        or boundaries.get("client_action_request_observed") is not False
        or boundaries.get("client_next_swing_queue_intent") != QUEUE_INTENT_STATUS
        or boundaries.get("target_switch_intent") != QUEUE_INTENT_STATUS
    ):
        raise HistoricalBehaviorCloneV2Error(
            "client queue/target-switch uncertainty was weakened"
        )
    return source, _source_key(source)


def _recent_recognized_actions(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    recent = _array(
        state.get("recent_direct_action_observations"),
        "state.recent_direct_action_observations",
    )
    result = []
    for raw in recent:
        action = _mapping(raw, "recent action")
        if (
            str(action.get("phase") or "").upper() == "START"
            and action.get("action_key") in ACTION_SPEC_BY_KEY
        ):
            result.append(action)
    return result


def _feature_contexts(
    state: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    left_truncated_prefix: bool,
) -> tuple[tuple[str, str], ...]:
    elapsed = _integer(state.get("wave_elapsed_ms"), "state.wave_elapsed_ms", minimum=0)
    seen = _array(
        state.get("seen_hostile_target_guids"), "state.seen_hostile_target_guids"
    )
    dead = _array(
        state.get("observed_dead_target_guids"), "state.observed_dead_target_guids"
    )
    if len(dead) > len(seen):
        raise HistoricalBehaviorCloneV2Error("dead targets exceed seen targets")
    coarse = {
        "wave_elapsed_bucket": policy_v2._elapsed_bucket(elapsed),
        "observed_target_count_bucket": policy_v2._count_bucket(len(seen)),
        "observed_dead_target_count_bucket": policy_v2._count_bucket(len(dead)),
    }
    history = _recent_recognized_actions(state)
    if left_truncated_prefix:
        last_action = "UNKNOWN_LEFT_TRUNCATED"
        last_lane = "UNKNOWN_LEFT_TRUNCATED"
    else:
        last = history[-1] if history else None
        last_action = str(last["action_key"]) if last is not None else "NONE"
        last_lane = (
            ACTION_SPEC_BY_KEY[last_action].lane if last_action != "NONE" else "NONE"
        )
    last_gcd = next(
        (
            action
            for action in reversed(history)
            if ACTION_SPEC_BY_KEY[str(action["action_key"])].lane == "gcd"
        ),
        None,
    )
    current_anchor = _mapping(observed.get("anchor"), "observed_start.anchor")
    current_ms = _integer(
        current_anchor.get("timestamp_ms"), "observed_start.timestamp_ms", minimum=0
    )
    if left_truncated_prefix:
        age_bucket = "UNKNOWN_LEFT_TRUNCATED"
    elif last_gcd is None:
        age_bucket = "NEVER_OBSERVED"
    else:
        order = _array(last_gcd.get("order_key"), "recent action order_key")
        previous_ms = _integer(order[0], "recent action timestamp_ms", minimum=0)
        if previous_ms > current_ms:
            raise HistoricalBehaviorCloneV2Error(
                "recent prefix action occurs after the current START"
            )
        age_bucket = policy_v2._elapsed_bucket(current_ms - previous_ms)
    if left_truncated_prefix:
        stance = "UNKNOWN_LEFT_TRUNCATED"
    else:
        stance = next(
            (
                str(action["action_key"]).removeprefix("warrior.")
                for action in reversed(history)
                if action.get("action_key") in clone_v1.STANCE_ACTION_KEYS
            ),
            "UNKNOWN_BEFORE_OBSERVED_STANCE_ACTION",
        )
    return (
        (
            "base.wave_coarse",
            json.dumps(coarse, sort_keys=True, separators=(",", ":")),
        ),
        (
            "target.has_last_observed_target",
            "YES" if state.get("last_observed_hostile_target_guid") else "NO",
        ),
        (
            "coverage.prefix_observation_status",
            "LEFT_TRUNCATED" if left_truncated_prefix else "OBSERVED_FROM_WAVE_ANCHOR",
        ),
        ("sequence.last_controllable_action", last_action),
        ("sequence.last_controllable_lane", last_lane),
        ("sequence.last_gcd_action_age", age_bucket),
        ("sequence.observed_stance", stance),
    )


def _target_role(
    observed: Mapping[str, Any], state: Mapping[str, Any], focal_guid: str
) -> str:
    target = _mapping(observed.get("exact_target"), "observed_start.exact_target")
    guid = target.get("guid")
    lane = target.get("lane")
    if guid is None:
        return "NO_EXPLICIT_TARGET"
    if isinstance(guid, str) and guid.casefold() == focal_guid.casefold():
        return "SELF"
    if target.get("voting_enemy_target") is True and lane == "HOSTILE_CREATURE":
        previous = state.get("last_observed_hostile_target_guid")
        if isinstance(previous, str) and previous.casefold() == str(guid).casefold():
            return "CURRENT_ENEMY"
        return "OTHER_OR_NEW_ENEMY"
    if lane == "FRIENDLY_PLAYER":
        return "OTHER_FRIENDLY"
    return "UNRESOLVED_NONVOTING_TARGET"


def _left_truncated_wave(source: Mapping[str, Any]) -> bool:
    coverage = _mapping(
        source.get("episode_window_coverage"), "source.episode_window_coverage"
    )
    left_unobserved_ms = _integer(
        coverage.get("left_unobserved_ms"),
        "episode_window_coverage.left_unobserved_ms",
        minimum=0,
    )
    wave_ordinal = _integer(source.get("wave_ordinal"), "source.wave_ordinal", minimum=1)
    return left_unobserved_ms > 0 or wave_ordinal > 1


@dataclass
class _Compiler:
    prototype: Mapping[str, Any]
    source_binding: Mapping[str, Any]
    support: dict[tuple[str, str], _Support] = field(init=False)
    prototype_id: str = field(init=False)
    mark_global: Counter[str] = field(default_factory=Counter)
    mark_context: dict[str, dict[str, Counter[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Counter))
    )
    target_by_action: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    delay_global: _SurvivalCell = field(default_factory=_SurvivalCell)
    delay_by_previous: dict[str, _SurvivalCell] = field(
        default_factory=lambda: defaultdict(_SurvivalCell)
    )
    seen_starts: Counter[tuple[str, str]] = field(default_factory=Counter)
    seen_tails: Counter[tuple[str, str]] = field(default_factory=Counter)
    seen_completed_delays: Counter[tuple[str, str]] = field(default_factory=Counter)
    seen_left_truncated_starts: Counter[tuple[str, str]] = field(
        default_factory=Counter
    )
    seen_usable_censors: Counter[tuple[str, str]] = field(default_factory=Counter)
    seen_unusable_tails: Counter[tuple[str, str]] = field(default_factory=Counter)
    active_wave: tuple[str, str, str, str] | None = None
    last_elapsed_ms: int | None = None
    last_timestamp_ms: int | None = None
    last_action_key: str | None = None
    record_count: int = 0
    start_count: int = 0
    tail_count: int = 0
    completed_delay_count: int = 0
    left_truncated_start_count: int = 0
    usable_censor_count: int = 0
    unusable_tail_count: int = 0

    def __post_init__(self) -> None:
        self.prototype_id = _text(
            self.prototype.get("prototype_id"), "prototype.prototype_id"
        )
        self.support = _support_index(self.prototype)

    def _support_for(self, key: tuple[str, str, str, str]) -> _Support:
        support = self.support.get((key[2], key[3]))
        if support is None:
            raise HistoricalBehaviorCloneV2Error(
                "prototype record is outside declared player-raid support"
            )
        return support

    def _begin_or_match_wave(self, key: tuple[str, str, str, str]) -> None:
        if self.active_wave is None:
            self.active_wave = key
            return
        if self.active_wave != key:
            raise HistoricalBehaviorCloneV2Error(
                "a source wave changed before its explicit censor tail"
            )

    def process(self, raw: Mapping[str, Any]) -> None:
        row = _mapping(raw, "prototype observation record")
        source, key = _validate_common_record(row, self.prototype_id)
        record_type = _text(row.get("record_type"), "record_type")
        self.record_count += 1
        if record_type == "weighted_server_observed_start":
            self._process_start(row, source, key)
        elif record_type == "right_censored_wave_tail":
            self._process_tail(row, source, key)
        else:
            raise HistoricalBehaviorCloneV2Error(
                f"unsupported prototype record_type {record_type!r}"
            )

    def _process_start(
        self,
        row: Mapping[str, Any],
        source: Mapping[str, Any],
        key: tuple[str, str, str, str],
    ) -> None:
        self._begin_or_match_wave(key)
        support = self._support_for(key)
        observed = _mapping(row.get("observed_start"), "observed_start")
        if (
            observed.get("phase") != "START"
            or observed.get("policy_decision_label") is not True
            or observed.get("server_observed_start_proxy") is not True
            or observed.get("client_action_request_observed") is not False
            or observed.get("client_next_swing_queue_intent_observed") is not False
        ):
            raise HistoricalBehaviorCloneV2Error(
                "mark/delay/target heads accept recognized START proxies only"
            )
        action_key = _text(observed.get("action_key"), "observed_start.action_key")
        if action_key not in ACTION_SPEC_BY_KEY:
            raise HistoricalBehaviorCloneV2Error(
                "recognized START action is outside the V1/V3 ontology"
            )
        weight_row = _mapping(
            row.get("prepared_training_weight"), "prepared_training_weight"
        )
        if (
            weight_row.get("contract") != prototypes_v1.WEIGHT_CONTRACT
            or weight_row.get("player_denominator") != support.player_count
            or weight_row.get("raid_denominator_within_player")
            != support.raid_count
            or weight_row.get("recognized_start_denominator_within_player_raid")
            != support.recognized_start_count
            or weight_row.get("training_application_authorized") is not False
        ):
            raise HistoricalBehaviorCloneV2Error(
                "START weight metadata differs from the prototype hierarchy"
            )
        weight = _fraction_from_wire(weight_row, "prepared_training_weight")
        if weight != support.start_weight:
            raise HistoricalBehaviorCloneV2Error(
                "START weight differs from exact player/raid/START contract"
            )
        state = _mapping(row.get("strict_prefix_state"), "strict_prefix_state")
        elapsed = _integer(
            state.get("wave_elapsed_ms"), "strict_prefix_state.wave_elapsed_ms", minimum=0
        )
        anchor = _mapping(observed.get("anchor"), "observed_start.anchor")
        timestamp = _integer(
            anchor.get("timestamp_ms"), "observed_start.timestamp_ms", minimum=0
        )
        delay_status = _text(row.get("delay_observation_status"), "delay_observation_status")
        first_start = self.last_action_key is None
        expected_left_truncated = first_start and _left_truncated_wave(source)
        if expected_left_truncated:
            if delay_status != LEFT_TRUNCATED_DELAY:
                raise HistoricalBehaviorCloneV2Error(
                    "first START in a left-truncated wave was treated as completed"
                )
        elif delay_status != COMPLETED_DELAY:
            raise HistoricalBehaviorCloneV2Error(
                "completed START interval was mislabeled as left-truncated"
            )
        if self.last_elapsed_ms is not None and elapsed < self.last_elapsed_ms:
            raise HistoricalBehaviorCloneV2Error(
                "recognized START elapsed time is nonmonotone within a wave"
            )
        if delay_status == COMPLETED_DELAY:
            delay = elapsed if self.last_elapsed_ms is None else elapsed - self.last_elapsed_ms
            previous = START_ACTION if self.last_action_key is None else self.last_action_key
            delay_weight = support.delay_weight
            self.delay_global.add_event(delay, delay_weight)
            self.delay_by_previous[previous].add_event(delay, delay_weight)
            self.seen_completed_delays[(key[2], key[3])] += 1
            self.completed_delay_count += 1
        else:
            self.seen_left_truncated_starts[(key[2], key[3])] += 1
            self.left_truncated_start_count += 1
        self.mark_global[action_key] += weight
        for family, value in _feature_contexts(
            state,
            observed,
            left_truncated_prefix=delay_status == LEFT_TRUNCATED_DELAY,
        ):
            self.mark_context[family][value][action_key] += weight
        role = _target_role(observed, state, key[2])
        self.target_by_action[action_key][role] += weight
        self.seen_starts[(key[2], key[3])] += 1
        self.start_count += 1
        self.last_elapsed_ms = elapsed
        self.last_timestamp_ms = timestamp
        self.last_action_key = action_key

    def _process_tail(
        self,
        row: Mapping[str, Any],
        source: Mapping[str, Any],
        key: tuple[str, str, str, str],
    ) -> None:
        self._begin_or_match_wave(key)
        support = self._support_for(key)
        if row.get("prepared_training_weight") is not None:
            raise HistoricalBehaviorCloneV2Error(
                "right-censored tail cannot carry a START training weight"
            )
        if row.get("tail_weight_status") != "UNASSIGNED_PENDING_CENSOR_AWARE_COMPILER":
            raise HistoricalBehaviorCloneV2Error(
                "tail weight was not explicitly delegated to the censor-aware compiler"
            )
        tail = _mapping(row.get("right_censored_tail"), "right_censored_tail")
        if (
            tail.get("right_censored") is not True
            or tail.get("next_action_policy_label_observed") is not False
            or tail.get("next_recognized_controllable_start_before_window_end")
            is not False
            or tail.get("later_unmapped_or_passive_starts_shorten_tail") is not False
        ):
            raise HistoricalBehaviorCloneV2Error(
                "terminal inter-decision censoring contract was weakened"
            )
        preceding = tail.get("preceding_recognized_controllable_start")
        if self.last_action_key is None:
            if preceding is not None or tail.get("origin") != "WAVE_FIRST_ANCHOR":
                raise HistoricalBehaviorCloneV2Error(
                    "no-decision wave tail does not begin at wave start"
                )
            previous = START_ACTION
        else:
            prior = _mapping(preceding, "preceding recognized controllable START")
            if (
                prior.get("policy_decision_label") is not True
                or prior.get("action_key") != self.last_action_key
                or tail.get("origin") != "LAST_RECOGNIZED_CONTROLLABLE_START"
                or _integer(
                    tail.get("origin_timestamp_ms"), "tail.origin_timestamp_ms", minimum=0
                )
                != self.last_timestamp_ms
            ):
                raise HistoricalBehaviorCloneV2Error(
                    "tail is not bound to the last recognized controllable START"
                )
            previous = self.last_action_key
        origin_timestamp_ms = _integer(
            tail.get("origin_timestamp_ms"), "tail.origin_timestamp_ms", minimum=0
        )
        window_end_timestamp_ms = _integer(
            tail.get("window_end_timestamp_ms"),
            "tail.window_end_timestamp_ms",
            minimum=0,
        )
        duration = _integer(tail.get("duration_ms"), "tail.duration_ms", minimum=0)
        if window_end_timestamp_ms - origin_timestamp_ms != duration:
            raise HistoricalBehaviorCloneV2Error(
                "tail duration differs from its source timestamp envelope"
            )
        delay_status = _text(row.get("delay_observation_status"), "delay_observation_status")
        if delay_status == RIGHT_CENSORED_DELAY:
            if self.last_action_key is None and _left_truncated_wave(source):
                raise HistoricalBehaviorCloneV2Error(
                    "no-START left-truncated tail is not a usable right censor"
                )
            censor_weight = support.delay_weight
            self.delay_global.add_censor(duration, censor_weight)
            self.delay_by_previous[previous].add_censor(duration, censor_weight)
            self.seen_usable_censors[(key[2], key[3])] += 1
            self.usable_censor_count += 1
        elif delay_status == UNUSABLE_TRUNCATED_TAIL:
            if self.last_action_key is not None or not _left_truncated_wave(source):
                raise HistoricalBehaviorCloneV2Error(
                    "an unusable tail must be a no-START left-truncated wave"
                )
            self.seen_unusable_tails[(key[2], key[3])] += 1
            self.unusable_tail_count += 1
        else:
            raise HistoricalBehaviorCloneV2Error(
                "tail delay_observation_status is unsupported"
            )
        self.seen_tails[(key[2], key[3])] += 1
        self.tail_count += 1
        self.active_wave = None
        self.last_elapsed_ms = None
        self.last_timestamp_ms = None
        self.last_action_key = None

    def finish(self) -> JSONMap:
        if self.active_wave is not None:
            raise HistoricalBehaviorCloneV2Error(
                "prototype partition ended without an explicit wave censor tail"
            )
        for key, support in sorted(self.support.items()):
            if self.seen_starts[key] != support.recognized_start_count:
                raise HistoricalBehaviorCloneV2Error(
                    f"recognized START count differs for player-raid {key}"
                )
            if self.seen_tails[key] != support.wave_count:
                raise HistoricalBehaviorCloneV2Error(
                    f"right-censored tail count differs for player-raid {key}"
                )
            if self.seen_completed_delays[key] != support.completed_delay_count:
                raise HistoricalBehaviorCloneV2Error(
                    f"completed delay count differs for player-raid {key}"
                )
            if (
                self.seen_left_truncated_starts[key]
                != support.left_truncated_first_start_count
            ):
                raise HistoricalBehaviorCloneV2Error(
                    f"left-truncated START count differs for player-raid {key}"
                )
            if self.seen_usable_censors[key] != support.usable_right_censor_count:
                raise HistoricalBehaviorCloneV2Error(
                    f"usable right-censor count differs for player-raid {key}"
                )
            if (
                self.seen_unusable_tails[key]
                != support.unusable_left_right_truncated_tail_count
            ):
                raise HistoricalBehaviorCloneV2Error(
                    f"unusable truncated-tail count differs for player-raid {key}"
                )
        event_mass = sum(self.mark_global.values(), Fraction())
        target_mass = sum(
            (sum(counts.values(), Fraction()) for counts in self.target_by_action.values()),
            Fraction(),
        )
        if (
            event_mass != Fraction(1, 1)
            or target_mass != event_mass
            or self.delay_global.event_weight + self.delay_global.censor_weight
            != Fraction(1, 1)
        ):
            raise HistoricalBehaviorCloneV2Error(
                "prototype mark/target or inter-decision interval masses do not close"
            )
        delay_global_wire = self.delay_global.wire()
        delay_by_previous_wire = {
            action: cell.wire()
            for action, cell in sorted(self.delay_by_previous.items())
        }
        runtime_projection = {
            "schema": RUNTIME_PROJECTION_SCHEMA,
            "numeric_type": "IEEE_754_BINARY64",
            "finite_values_only": True,
            "exact_fraction_numerators_and_denominators_runtime_decode_forbidden": True,
            "mark_global_weights": _float_counter(
                self.mark_global, ACTION_KEYS, "runtime mark"
            ),
            "mark_context_weights": {
                family: {
                    value: _float_counter(
                        counts, ACTION_KEYS, f"runtime mark context {family}:{value}"
                    )
                    for value, counts in sorted(values.items())
                }
                for family, values in sorted(self.mark_context.items())
            },
            "target_by_action_weights": {
                action: _float_counter(
                    self.target_by_action[action],
                    TARGET_ROLES,
                    f"runtime target {action}",
                )
                for action in ACTION_KEYS
            },
            "delay_product_limit": {
                "global": _float_survival_projection(delay_global_wire),
                "by_previous_action": {
                    action: _float_survival_projection(cell)
                    for action, cell in sorted(delay_by_previous_wire.items())
                },
            },
        }
        model_core = {
            "schema": MODEL_SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "prototype_identity": {
                "prototype_id": self.prototype_id,
                "prototype_family": self.prototype.get("prototype_family"),
                "selection_rule": self.prototype.get("selection_rule"),
                "member_count": self.prototype.get("member_count"),
                "members": deepcopy(self.prototype.get("members")),
            },
            "source_binding": deepcopy(dict(self.source_binding)),
            "action_ontology": [
                {
                    "action_key": spec.action_key,
                    "lane": spec.lane,
                    "unpaired_go_is_decision": False,
                    "v1_unpaired_go_semantics_reused": False,
                    "v1_ontology_unpaired_go_value": spec.unpaired_go_is_decision,
                    "v2_policy_decision_phase": "RECOGNIZED_START_ONLY",
                }
                for spec in policy_v3.ACTION_ONTOLOGY
            ],
            "feature_contract": {
                "strict_prefix_only": True,
                "left_truncated_first_start_uses_explicit_unknown_history": True,
                "families": sorted(self.mark_context),
                "team_background_dps_available": False,
                "client_queue_context_available": False,
                "target_switch_intent_available": False,
                "recent_action_history_is_source_bounded_to_last_eight": True,
            },
            "mark_head": {
                "kind": "EXACT_FRACTION_WEIGHTED_STRICT_PREFIX_COUNTS",
                "recognized_start_only": True,
                "global_counts": _fraction_counter_wire(
                    self.mark_global, ACTION_KEYS
                ),
                "context_counts": {
                    family: {
                        value: _fraction_counter_wire(counts, ACTION_KEYS)
                        for value, counts in sorted(values.items())
                    }
                    for family, values in sorted(self.mark_context.items())
                },
                "future_runtime_contract": {
                    "legal_mask_applied_before_sampling": True,
                    "smoothing_and_context_backoff_must_remain_explicit": True,
                },
            },
            "target_head": {
                "kind": "EXACT_FRACTION_ACTION_CONDITIONAL_TARGET_COUNTS",
                "recognized_start_only": True,
                "target_roles": list(TARGET_ROLES),
                "by_action": {
                    action: _fraction_counter_wire(
                        self.target_by_action[action], TARGET_ROLES
                    )
                    for action in ACTION_KEYS
                },
                "unresolved_nonvoting_target_runtime_mapping": (
                    "REFUSED_PENDING_TYPED_ADAPTER; never silently remapped"
                ),
            },
            "delay_head": {
                "kind": (
                    "EXACT_FRACTION_WEIGHTED_DISCRETE_PRODUCT_LIMIT_WITH_"
                    "RIGHT_CENSORING"
                ),
                "bucket_upper_bounds_ms": list(DELAY_UPPER_BOUNDS_MS),
                "event_definition": (
                    "next recognized controllable server-observed START proxy"
                ),
                "censor_definition": (
                    "source wave with an authoritative interval origin ended before "
                    "another recognized controllable START"
                ),
                "completed_interval_weight": DELAY_WEIGHT_CONTRACT,
                "terminal_censor_weight": (
                    "SAME_PLAYER_RAID_INTER_DECISION_INTERVAL_WEIGHT_AS_COMPLETED_"
                    "INTERVALS"
                ),
                "per_interval_weight": (
                    "1 / prototype_player_count / player_raid_count / "
                    "(completed_delay_count + usable_right_censor_count)"
                ),
                "all_source_tail_records_accounted": True,
                "usable_right_censored_tails_consumed": True,
                "terminal_tails_converted_to_pseudo_actions": False,
                "left_truncated_first_starts_converted_to_completed_intervals": False,
                "unusable_left_right_truncated_tails_consumed_as_censors": False,
                "ties": "events and censors are both at risk in their terminal bucket",
                "global": delay_global_wire,
                "by_previous_action": delay_by_previous_wire,
                "runtime_projection": {
                    "backoff_strength": _fraction_wire(DEFAULT_BACKOFF_STRENGTH),
                    "conditional_cell_hazard_backoff": (
                        "local weighted event/risk plus exact global-hazard prior"
                    ),
                    "eventually_acting_projection": (
                        "condition product-limit event mass on an event by the last "
                        "modeled bucket; retain residual survival separately"
                    ),
                    "residual_survival_becomes_action_or_finite_delay": False,
                    "typed_adapter_required": True,
                },
            },
            "compiler_summary": {
                "source_record_count": self.record_count,
                "weighted_start_observation_count": self.start_count,
                "source_tail_record_count": self.tail_count,
                "completed_delay_observation_count": self.completed_delay_count,
                "left_truncated_first_start_count": self.left_truncated_start_count,
                "usable_right_censor_count": self.usable_censor_count,
                "unusable_left_right_truncated_tail_count": self.unusable_tail_count,
                "weighted_mark_mass": _fraction_wire(event_mass),
                "completed_delay_mass": _fraction_wire(
                    self.delay_global.event_weight
                ),
                "usable_right_censor_mass": _fraction_wire(
                    self.delay_global.censor_weight
                ),
                "usable_delay_observation_mass": _fraction_wire(
                    self.delay_global.event_weight + self.delay_global.censor_weight
                ),
                "go_or_fail_policy_label_count": 0,
            },
            "runtime_bounded_float_projection": runtime_projection,
            "uncertainty_contract": {
                "client_action_request": "NOT_OBSERVED",
                "client_next_swing_queue_intent": QUEUE_INTENT_STATUS,
                "target_switch_intent": QUEUE_INTENT_STATUS,
                "go_fail": "OUTCOMES_ONLY_NOT_LABELS",
                "actions_outside_reconstruction_waves": "MISSING_NOT_INFERRED",
            },
            "claim_boundary": {
                "development_model_materialized": True,
                "typed_simulator_adapter_complete": False,
                "full_rollout_complete": False,
                "comparison_authorized": False,
                "same_equipment_matched_seed_comparison_authorized": False,
                "closed_loop_baseline_authorized": False,
                "deployment_authorized": False,
                "full_expert_policy_claim": False,
                "superiority_claim_authorized": False,
            },
        }
        model = _content_addressed(model_core)
        validate_model_v2(model)
        return model


def compile_weighted_prototype_v2(
    prototype: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    source_binding: Mapping[str, Any],
) -> JSONMap:
    """Compile one weighted observation partition without retaining its rows."""

    compiler = _Compiler(prototype, source_binding)
    iterator = iter(records)
    try:
        for record in iterator:
            compiler.process(record)
        return compiler.finish()
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def _model_fraction(value: Any, label: str) -> Fraction:
    return _fraction_from_wire(value, label)


def _sha256_text(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be a sha256 hex digest")
    return result


def _validate_source_binding(
    binding_value: Any, *, prototype_id: str
) -> Mapping[str, Any]:
    binding = _mapping(binding_value, "source_binding")
    manifest = _mapping(binding.get("prototype_manifest"), "source prototype_manifest")
    if manifest.get("schema") != prototypes_v1.MANIFEST_SCHEMA:
        raise HistoricalBehaviorCloneV2Error("source prototype manifest schema differs")
    if manifest.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneV2Error(
            "source prototype manifest implementation_revision differs"
        )
    _sha256_text(manifest.get("file_sha256"), "source manifest file_sha256")
    _sha256_text(manifest.get("content_sha256"), "source manifest content_sha256")
    partition = _mapping(
        binding.get("prototype_partition"), "source prototype_partition"
    )
    if partition.get("record_schema") != prototypes_v1.RECORD_SCHEMA:
        raise HistoricalBehaviorCloneV2Error("source partition schema differs")
    if partition.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneV2Error(
            "source prototype partition implementation_revision differs"
        )
    _integer(partition.get("record_count"), "source partition record_count", minimum=0)
    _integer(
        partition.get("logical_size_bytes"),
        "source partition logical_size_bytes",
        minimum=0,
    )
    _sha256_text(
        partition.get("logical_content_sha256"),
        "source partition logical_content_sha256",
    )
    if binding.get("prototype_id") != prototype_id:
        raise HistoricalBehaviorCloneV2Error(
            "source binding prototype_id differs from model identity"
        )
    if binding.get("network_request_count") != 0:
        raise HistoricalBehaviorCloneV2Error("source binding network count differs")
    if "path" in manifest or "path" in partition:
        raise HistoricalBehaviorCloneV2Error(
            "source binding paths are location-dependent and must not enter model identity"
        )
    return binding


def _projection_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise HistoricalBehaviorCloneV2Error(f"{label} must be finite")
    return result


def _match_projection_fraction(actual: Any, exact: Any, label: str) -> None:
    projected = _projection_number(actual, label)
    expected = float(_model_fraction(exact, f"{label} exact"))
    if projected != expected:
        raise HistoricalBehaviorCloneV2Error(
            f"{label} differs from the exact offline rational"
        )


def _validate_projected_counter(
    projected_value: Any,
    exact_value: Any,
    keys: Sequence[str],
    label: str,
) -> None:
    projected = _mapping(projected_value, f"{label} projection")
    exact = _mapping(exact_value, f"{label} exact")
    if set(projected) != set(keys) or set(exact) != set(keys):
        raise HistoricalBehaviorCloneV2Error(f"{label} keys differ")
    for key in keys:
        _match_projection_fraction(projected[key], exact[key], f"{label}[{key}]")


def _validate_projected_survival(
    projected_value: Any, exact_value: Any, label: str
) -> None:
    projected = _mapping(projected_value, f"{label} projection")
    exact = _mapping(exact_value, f"{label} exact")
    for key in (
        "event_weight",
        "censor_weight",
        "residual_survival_after_last_bucket",
    ):
        _match_projection_fraction(projected.get(key), exact.get(key), f"{label}.{key}")
    projected_rows = _array(projected.get("buckets"), f"{label} projected buckets")
    exact_rows = _array(exact.get("buckets"), f"{label} exact buckets")
    if len(projected_rows) != len(exact_rows):
        raise HistoricalBehaviorCloneV2Error(f"{label} bucket count differs")
    fraction_fields = (
        "at_risk_weight",
        "event_weight",
        "censor_weight",
        "event_hazard",
        "survival_before",
        "event_probability_mass",
        "survival_after",
    )
    for index, (projected_raw, exact_raw) in enumerate(
        zip(projected_rows, exact_rows, strict=True)
    ):
        projected_row = _mapping(projected_raw, f"{label} projected bucket[{index}]")
        exact_row = _mapping(exact_raw, f"{label} exact bucket[{index}]")
        for key in ("bucket", "lower_bound_ms", "upper_bound_ms"):
            if projected_row.get(key) != exact_row.get(key):
                raise HistoricalBehaviorCloneV2Error(
                    f"{label} projected bucket identity differs"
                )
        if projected_row.get("event_representative_ms") != exact_row.get(
            "event_representative_ms"
        ):
            raise HistoricalBehaviorCloneV2Error(
                f"{label} event representative differs"
            )
        for key in fraction_fields:
            _match_projection_fraction(
                projected_row.get(key),
                exact_row.get(key),
                f"{label}.bucket[{index}].{key}",
            )


def _validate_exact_survival(value: Any, label: str) -> None:
    cell = _mapping(value, label)
    rows = _array(cell.get("buckets"), f"{label}.buckets")
    expected_buckets = _delay_bucket_rows()
    if len(rows) != len(expected_buckets):
        raise HistoricalBehaviorCloneV2Error(f"{label} bucket count differs")

    parsed = []
    for index, (raw, (bucket, lower, upper)) in enumerate(
        zip(rows, expected_buckets, strict=True)
    ):
        row = _mapping(raw, f"{label}.bucket[{index}]")
        if (
            row.get("bucket") != bucket
            or row.get("lower_bound_ms") != lower
            or row.get("upper_bound_ms") != upper
        ):
            raise HistoricalBehaviorCloneV2Error(
                f"{label} bucket identity differs"
            )
        parsed.append(
            (
                row,
                _model_fraction(
                    row.get("at_risk_weight"),
                    f"{label}.bucket[{index}].at_risk_weight",
                ),
                _model_fraction(
                    row.get("event_weight"),
                    f"{label}.bucket[{index}].event_weight",
                ),
                _model_fraction(
                    row.get("censor_weight"),
                    f"{label}.bucket[{index}].censor_weight",
                ),
            )
        )

    event_total = sum((event for _, _, event, _ in parsed), Fraction())
    censor_total = sum((censor for _, _, _, censor in parsed), Fraction())
    if _model_fraction(cell.get("event_weight"), f"{label}.event_weight") != event_total:
        raise HistoricalBehaviorCloneV2Error(
            f"{label} event_weight differs from bucket total"
        )
    if _model_fraction(cell.get("censor_weight"), f"{label}.censor_weight") != censor_total:
        raise HistoricalBehaviorCloneV2Error(
            f"{label} censor_weight differs from bucket total"
        )

    terminal_mass_after = event_total + censor_total
    survival = Fraction(1, 1)
    for index, (row, risk, event, censor) in enumerate(parsed):
        if risk != terminal_mass_after:
            raise HistoricalBehaviorCloneV2Error(
                f"{label}.bucket[{index}].at_risk_weight differs from terminal masses"
            )
        if event + censor > risk:
            raise HistoricalBehaviorCloneV2Error(
                f"{label}.bucket[{index}] terminal mass exceeds risk"
            )
        hazard = Fraction() if risk == 0 else event / risk
        event_mass = survival * hazard
        survival_after = survival * (1 - hazard)
        identities = {
            "event_hazard": hazard,
            "survival_before": survival,
            "event_probability_mass": event_mass,
            "survival_after": survival_after,
        }
        for field, expected in identities.items():
            if _model_fraction(
                row.get(field), f"{label}.bucket[{index}].{field}"
            ) != expected:
                raise HistoricalBehaviorCloneV2Error(
                    f"{label}.bucket[{index}].{field} differs from product-limit identity"
                )
        terminal_mass_after -= event + censor
        survival = survival_after
    if terminal_mass_after != 0:
        raise HistoricalBehaviorCloneV2Error(
            f"{label} terminal mass does not close"
        )
    if _model_fraction(
        cell.get("residual_survival_after_last_bucket"),
        f"{label}.residual_survival_after_last_bucket",
    ) != survival:
        raise HistoricalBehaviorCloneV2Error(
            f"{label} residual survival differs from product-limit identity"
        )


def _validate_runtime_projection(
    projection_value: Any,
    *,
    mark: Mapping[str, Any],
    target: Mapping[str, Any],
    delay: Mapping[str, Any],
) -> None:
    projection = _mapping(projection_value, "runtime_bounded_float_projection")
    if (
        projection.get("schema") != RUNTIME_PROJECTION_SCHEMA
        or projection.get("numeric_type") != "IEEE_754_BINARY64"
        or projection.get("finite_values_only") is not True
        or projection.get(
            "exact_fraction_numerators_and_denominators_runtime_decode_forbidden"
        )
        is not True
    ):
        raise HistoricalBehaviorCloneV2Error("runtime numeric contract differs")
    _validate_projected_counter(
        projection.get("mark_global_weights"),
        mark.get("global_counts"),
        ACTION_KEYS,
        "runtime mark global",
    )
    projected_context = _mapping(
        projection.get("mark_context_weights"), "runtime mark contexts"
    )
    exact_context = _mapping(mark.get("context_counts"), "exact mark contexts")
    if set(projected_context) != set(exact_context):
        raise HistoricalBehaviorCloneV2Error("runtime mark context families differ")
    for family, exact_values_raw in exact_context.items():
        projected_values = _mapping(
            projected_context.get(family), f"runtime mark context {family}"
        )
        exact_values = _mapping(exact_values_raw, f"exact mark context {family}")
        if set(projected_values) != set(exact_values):
            raise HistoricalBehaviorCloneV2Error(
                f"runtime mark context values differ for {family}"
            )
        for value, exact_counts in exact_values.items():
            _validate_projected_counter(
                projected_values[value],
                exact_counts,
                ACTION_KEYS,
                f"runtime mark context {family}:{value}",
            )
    projected_targets = _mapping(
        projection.get("target_by_action_weights"), "runtime targets"
    )
    exact_targets = _mapping(target.get("by_action"), "exact targets")
    if set(projected_targets) != set(ACTION_KEYS):
        raise HistoricalBehaviorCloneV2Error("runtime target actions differ")
    for action in ACTION_KEYS:
        _validate_projected_counter(
            projected_targets[action],
            exact_targets[action],
            TARGET_ROLES,
            f"runtime target {action}",
        )
    projected_delay = _mapping(
        projection.get("delay_product_limit"), "runtime delay projection"
    )
    _validate_projected_survival(
        projected_delay.get("global"), delay.get("global"), "runtime delay global"
    )
    projected_previous = _mapping(
        projected_delay.get("by_previous_action"), "runtime delay by previous"
    )
    exact_previous = _mapping(
        delay.get("by_previous_action"), "exact delay by previous"
    )
    if set(projected_previous) != set(exact_previous):
        raise HistoricalBehaviorCloneV2Error(
            "runtime delay previous-action cells differ"
        )
    for action, exact_cell in exact_previous.items():
        _validate_projected_survival(
            projected_previous[action],
            exact_cell,
            f"runtime delay previous {action}",
        )


def validate_model_v2(model: Mapping[str, Any]) -> None:
    if model.get("schema") != MODEL_SCHEMA or model.get("status") != STATUS:
        raise HistoricalBehaviorCloneV2Error("unsupported behavior clone v2 model")
    if model.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneV2Error(
            "behavior clone model implementation_revision differs"
        )
    identity = _mapping(model.get("prototype_identity"), "prototype_identity")
    prototype_id = _text(
        identity.get("prototype_id"), "prototype_identity.prototype_id"
    )
    _validate_source_binding(model.get("source_binding"), prototype_id=prototype_id)
    mark = _mapping(model.get("mark_head"), "mark_head")
    target = _mapping(model.get("target_head"), "target_head")
    delay = _mapping(model.get("delay_head"), "delay_head")
    if mark.get("recognized_start_only") is not True or target.get(
        "recognized_start_only"
    ) is not True:
        raise HistoricalBehaviorCloneV2Error("non-START observations entered a head")
    mark_counts = _mapping(mark.get("global_counts"), "mark.global_counts")
    if set(mark_counts) != set(ACTION_KEYS):
        raise HistoricalBehaviorCloneV2Error("mark ontology differs from V1/V3")
    if sum(
        (_model_fraction(mark_counts[action], f"mark[{action}]") for action in ACTION_KEYS),
        Fraction(),
    ) != Fraction(1, 1):
        raise HistoricalBehaviorCloneV2Error("weighted mark mass does not equal one")
    global_delay = _mapping(delay.get("global"), "delay.global")
    _validate_exact_survival(global_delay, "delay.global")
    previous_delay = _mapping(
        delay.get("by_previous_action"), "delay.by_previous_action"
    )
    for action, cell in previous_delay.items():
        _validate_exact_survival(cell, f"delay.by_previous_action[{action}]")
    delay_event_mass = _model_fraction(
        global_delay.get("event_weight"), "delay.event_weight"
    )
    delay_censor_mass = _model_fraction(
        global_delay.get("censor_weight"), "delay.censor_weight"
    )
    if (
        delay_event_mass + delay_censor_mass != Fraction(1, 1)
        or delay.get("all_source_tail_records_accounted") is not True
        or delay.get("usable_right_censored_tails_consumed") is not True
        or delay.get("terminal_tails_converted_to_pseudo_actions") is not False
        or delay.get("left_truncated_first_starts_converted_to_completed_intervals")
        is not False
        or delay.get("unusable_left_right_truncated_tails_consumed_as_censors")
        is not False
    ):
        raise HistoricalBehaviorCloneV2Error(
            "delay event/censor mass or tail contract differs"
        )
    roles = target.get("target_roles")
    if roles != list(TARGET_ROLES):
        raise HistoricalBehaviorCloneV2Error("target roles differ")
    uncertainty = _mapping(model.get("uncertainty_contract"), "uncertainty_contract")
    if (
        uncertainty.get("client_next_swing_queue_intent") != QUEUE_INTENT_STATUS
        or uncertainty.get("target_switch_intent") != QUEUE_INTENT_STATUS
    ):
        raise HistoricalBehaviorCloneV2Error("source intent uncertainty was weakened")
    _validate_runtime_projection(
        model.get("runtime_bounded_float_projection"),
        mark=mark,
        target=target,
        delay=delay,
    )
    claims = _mapping(model.get("claim_boundary"), "claim_boundary")
    for key in (
        "typed_simulator_adapter_complete",
        "full_rollout_complete",
        "comparison_authorized",
        "same_equipment_matched_seed_comparison_authorized",
        "closed_loop_baseline_authorized",
        "deployment_authorized",
        "full_expert_policy_claim",
        "superiority_claim_authorized",
    ):
        if claims.get(key) is not False:
            raise HistoricalBehaviorCloneV2Error(f"claim boundary {key} must be false")
    content = _mapping(model.get("content_address"), "content_address")
    core = deepcopy(dict(model))
    core.pop("content_address", None)
    if content.get("sha256") != hashlib.sha256(_canonical_bytes(core)).hexdigest():
        raise HistoricalBehaviorCloneV2Error("model content address differs")


def _survival_rows(cell: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = _array(cell.get("buckets"), "survival buckets")
    result = {}
    for row in rows:
        value = _mapping(row, "survival bucket")
        bucket = _text(value.get("bucket"), "survival bucket name")
        result[bucket] = value
    expected = [name for name, _, _ in _delay_bucket_rows()]
    if list(result) != expected:
        raise HistoricalBehaviorCloneV2Error("survival bucket order differs")
    return result


def predict_delay_distribution_v2(
    model: Mapping[str, Any], previous_action_key: str | None
) -> JSONMap:
    """Project censor-aware hazards to a conditional eventual-action delay CDF.

    Residual survival is returned separately.  Conditioning it away for this
    projection does not convert it into an action or a finite-delay sample.
    """

    validate_model_v2(model)
    previous = START_ACTION if previous_action_key is None else previous_action_key
    if previous != START_ACTION and previous not in ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneV2Error("previous action is outside the ontology")
    head = _mapping(model.get("delay_head"), "delay_head")
    global_cell = _mapping(head.get("global"), "delay.global")
    global_rows = _survival_rows(global_cell)
    local_wire = _mapping(head.get("by_previous_action"), "by_previous_action").get(
        previous
    )
    local_rows = (
        _survival_rows(_mapping(local_wire, "local delay cell"))
        if isinstance(local_wire, Mapping)
        else None
    )
    runtime = _mapping(head.get("runtime_projection"), "runtime_projection")
    strength = _model_fraction(
        runtime.get("backoff_strength"), "delay backoff_strength"
    )
    survival = Fraction(1, 1)
    projected = []
    for bucket, lower, upper in _delay_bucket_rows():
        global_row = global_rows[bucket]
        global_risk = _model_fraction(
            global_row.get("at_risk_weight"), f"global {bucket} risk"
        )
        global_event = _model_fraction(
            global_row.get("event_weight"), f"global {bucket} event"
        )
        global_hazard = (
            Fraction() if global_risk == 0 else global_event / global_risk
        )
        representative = global_row.get("event_representative_ms")
        if local_rows is None:
            hazard = global_hazard
        else:
            local_row = local_rows[bucket]
            local_risk = _model_fraction(
                local_row.get("at_risk_weight"), f"local {bucket} risk"
            )
            local_event = _model_fraction(
                local_row.get("event_weight"), f"local {bucket} event"
            )
            hazard = (
                local_event + strength * global_hazard
            ) / (local_risk + strength)
            if local_event:
                representative = local_row.get("event_representative_ms")
        mass = survival * hazard
        survival *= 1 - hazard
        projected.append(
            {
                "bucket": bucket,
                "lower_bound_ms": lower,
                "upper_bound_ms": upper,
                "hazard": _fraction_wire(hazard),
                "unconditional_event_mass": _fraction_wire(mass),
                "representative_ms": representative,
            }
        )
    total_event_mass = sum(
        (
            _model_fraction(row["unconditional_event_mass"], "event mass")
            for row in projected
        ),
        Fraction(),
    )
    if total_event_mass <= 0:
        raise HistoricalBehaviorCloneV2Error(
            "censor-aware delay head has no eventual-action event mass"
        )
    for row in projected:
        mass = _model_fraction(row["unconditional_event_mass"], "event mass")
        row["conditional_event_probability"] = _fraction_wire(
            mass / total_event_mass
        )
    return {
        "previous_action_key": previous,
        "buckets": projected,
        "unconditional_event_mass": _fraction_wire(total_event_mass),
        "residual_survival_mass": _fraction_wire(survival),
        "conditional_on_event_by_last_modeled_bucket": True,
        "residual_survival_converted_to_action_or_finite_delay": False,
        "typed_adapter_required": True,
    }


def _partition_path(manifest_path: Path, partition: Mapping[str, Any]) -> Path:
    raw = Path(_text(partition.get("path"), "partition.path"))
    return raw.resolve() if raw.is_absolute() else (manifest_path.parent / raw).resolve()


def _verified_partition_records(
    path: Path, partition: Mapping[str, Any]
) -> Iterable[Mapping[str, Any]]:
    logical = hashlib.sha256()
    logical_size = 0
    count = 0
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                logical.update(line)
                logical_size += len(line)
                count += 1
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise HistoricalBehaviorCloneV2Error(
                        f"cannot decode {path}:{line_number}: {error}"
                    ) from error
                yield _mapping(value, f"{path}:{line_number}")
    except (OSError, EOFError) as error:
        raise HistoricalBehaviorCloneV2Error(
            f"cannot read prototype partition {path}: {error}"
        ) from error
    if count != _integer(partition.get("record_count"), "partition.record_count", minimum=0):
        raise HistoricalBehaviorCloneV2Error("prototype partition record_count differs")
    if logical_size != _integer(
        partition.get("logical_size_bytes"), "partition.logical_size_bytes", minimum=0
    ):
        raise HistoricalBehaviorCloneV2Error("prototype partition logical size differs")
    if logical.hexdigest() != _text(
        partition.get("logical_content_sha256"), "partition.logical_content_sha256"
    ):
        raise HistoricalBehaviorCloneV2Error("prototype partition logical digest differs")


def build_historical_behavior_clones_v2(
    *,
    prototype_manifest_path: str | Path = DEFAULT_PROTOTYPE_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> CloneV2BuildResult:
    """Compile every prepared prototype into a separately bound V2 model."""

    source_file = Path(prototype_manifest_path).expanduser().resolve()
    destination = _under_offline_data(Path(output_directory))
    destination.mkdir(parents=True, exist_ok=True)
    source = _load_json(source_file, "weighted prototype manifest")
    if source.get("schema") != prototypes_v1.MANIFEST_SCHEMA:
        raise HistoricalBehaviorCloneV2Error("prototype manifest schema differs")
    if source.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneV2Error(
            "prototype manifest implementation_revision differs"
        )
    source_content = _mapping(
        source.get("content_address"), "prototype manifest content_address"
    )
    source_content_sha = _sha256_text(
        source_content.get("sha256"), "prototype manifest content sha256"
    )
    source_core = deepcopy(source)
    source_core.pop("content_address", None)
    if hashlib.sha256(_canonical_bytes(source_core)).hexdigest() != source_content_sha:
        raise HistoricalBehaviorCloneV2Error(
            "prototype manifest content address differs"
        )
    boundaries = _mapping(source.get("scientific_boundaries"), "scientific_boundaries")
    for key in (
        "training_authorized",
        "comparison_authorized",
        "same_equipment_matched_seed_comparison_authorized",
        "closed_loop_baseline_authorized",
        "deployment_authorized",
        "superiority_claim_authorized",
    ):
        if boundaries.get(key) is not False:
            raise HistoricalBehaviorCloneV2Error(
                f"prototype source boundary {key} must remain false"
            )
    prototypes = [
        _mapping(value, "prototype")
        for value in _array(source.get("prototypes"), "prototypes")
    ]
    count_contract = _mapping(
        source.get("prototype_count_contract"), "prototype_count_contract"
    )
    if (
        len(prototypes) != 5
        or count_contract.get("new_materialized_prototype_count") != len(prototypes)
    ):
        raise HistoricalBehaviorCloneV2Error(
            "clone v2 requires the exact five-prototype source artifact"
        )
    source_file_sha = _sha256_file(source_file)
    models = []
    seen_ids = set()
    for prototype in sorted(prototypes, key=lambda row: str(row.get("prototype_id"))):
        prototype_id = _text(prototype.get("prototype_id"), "prototype_id")
        if prototype_id in seen_ids:
            raise HistoricalBehaviorCloneV2Error("duplicate prototype_id")
        seen_ids.add(prototype_id)
        partition = _mapping(prototype.get("partition"), "prototype.partition")
        if partition.get("record_schema") != prototypes_v1.RECORD_SCHEMA:
            raise HistoricalBehaviorCloneV2Error("prototype partition schema differs")
        path = _partition_path(source_file, partition)
        source_binding = {
            "prototype_manifest": {
                "schema": source.get("schema"),
                "implementation_revision": source.get("implementation_revision"),
                "file_sha256": source_file_sha,
                "content_sha256": source_content_sha,
            },
            "prototype_partition": {
                "record_schema": partition.get("record_schema"),
                "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
                "record_count": partition.get("record_count"),
                "logical_size_bytes": partition.get("logical_size_bytes"),
                "logical_content_sha256": partition.get("logical_content_sha256"),
            },
            "prototype_id": prototype_id,
            "network_request_count": 0,
        }
        model = compile_weighted_prototype_v2(
            prototype,
            _verified_partition_records(path, partition),
            source_binding=source_binding,
        )
        address = _text(
            _mapping(model.get("content_address"), "model.content_address").get(
                "sha256"
            ),
            "model content sha256",
        )
        model_path = destination / f"{prototype_id}.{address}.model.json"
        payload = _canonical_bytes(model, newline=True)
        _atomic_write(model_path, payload)
        summary = _mapping(model.get("compiler_summary"), "compiler_summary")
        models.append(
            {
                "prototype_id": prototype_id,
                "prototype_family": prototype.get("prototype_family"),
                "path": model_path.name,
                "schema": MODEL_SCHEMA,
                "content_sha256": address,
                "file_sha256": _sha256_file(model_path),
                "file_size_bytes": model_path.stat().st_size,
                "weighted_start_observation_count": summary.get(
                    "weighted_start_observation_count"
                ),
                "completed_delay_observation_count": summary.get(
                    "completed_delay_observation_count"
                ),
                "usable_right_censor_count": summary.get(
                    "usable_right_censor_count"
                ),
                "left_truncated_first_start_count": summary.get(
                    "left_truncated_first_start_count"
                ),
                "unusable_left_right_truncated_tail_count": summary.get(
                    "unusable_left_right_truncated_tail_count"
                ),
                "comparison_authorized": False,
            }
        )
    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "prototype_manifest": {
                "schema": source.get("schema"),
                "implementation_revision": source.get("implementation_revision"),
                "file_sha256": source_file_sha,
                "content_sha256": source_content_sha,
            },
            "network_request_count": 0,
            "prototype_partitions_streamed_one_at_a_time": True,
            "source_records_retained_in_memory": 0,
        },
        "models": models,
        "summary": {
            "model_count": len(models),
            "weighted_start_observation_count": sum(
                int(row["weighted_start_observation_count"]) for row in models
            ),
            "completed_delay_observation_count": sum(
                int(row["completed_delay_observation_count"]) for row in models
            ),
            "usable_right_censor_count": sum(
                int(row["usable_right_censor_count"]) for row in models
            ),
            "left_truncated_first_start_count": sum(
                int(row["left_truncated_first_start_count"]) for row in models
            ),
            "unusable_left_right_truncated_tail_count": sum(
                int(row["unusable_left_right_truncated_tail_count"])
                for row in models
            ),
        },
        "scientific_boundaries": {
            "distinct_development_models_materialized": True,
            "typed_simulator_adapter_complete": False,
            "full_rollout_complete": False,
            "comparison_authorized": False,
            "same_equipment_matched_seed_comparison_authorized": False,
            "closed_loop_baseline_authorized": False,
            "deployment_authorized": False,
            "full_expert_policy_claim": False,
            "superiority_claim_authorized": False,
        },
    }
    manifest = _content_addressed(manifest_core)
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    address = _text(
        _mapping(manifest.get("content_address"), "manifest.content_address").get(
            "sha256"
        ),
        "manifest content sha256",
    )
    addressed = destination / f"historical_behavior_clone_v2.{address}.manifest.json"
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return CloneV2BuildResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        model_count=len(models),
        weighted_start_observation_count=manifest_core["summary"][
            "weighted_start_observation_count"
        ],
        completed_delay_observation_count=manifest_core["summary"][
            "completed_delay_observation_count"
        ],
        usable_right_censor_count=manifest_core["summary"][
            "usable_right_censor_count"
        ],
        left_truncated_first_start_count=manifest_core["summary"][
            "left_truncated_first_start_count"
        ],
        unusable_left_right_truncated_tail_count=manifest_core["summary"][
            "unusable_left_right_truncated_tail_count"
        ],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile five weighted Fury prototypes into censor-aware clone v2 models"
    )
    parser.add_argument(
        "--prototype-manifest", type=Path, default=DEFAULT_PROTOTYPE_MANIFEST
    )
    parser.add_argument(
        "--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_behavior_clones_v2(
        prototype_manifest_path=args.prototype_manifest,
        output_directory=args.output_directory,
    )
    rendered = _canonical_bytes(result.as_dict(), newline=True).decode("utf-8")
    if stdout is None:
        print(rendered, end="")
    else:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalBehaviorCloneV2Error as error:
        print(str(error), file=os.sys.stderr)
        raise SystemExit(2)


__all__ = [
    "ACTION_KEYS",
    "CloneV2BuildResult",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_PROTOTYPE_MANIFEST",
    "HistoricalBehaviorCloneV2Error",
    "IMPLEMENTATION_REVISION",
    "MANIFEST_SCHEMA",
    "MODEL_SCHEMA",
    "QUEUE_INTENT_STATUS",
    "STATUS",
    "TARGET_ROLES",
    "build_historical_behavior_clones_v2",
    "compile_weighted_prototype_v2",
    "main",
    "predict_delay_distribution_v2",
    "validate_model_v2",
]
