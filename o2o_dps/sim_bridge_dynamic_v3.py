"""Strict client for central dynamic-target idle advancement (wire v3).

This is a version-isolated overlay on :mod:`sim_bridge_dynamic_v2`.  It sends
the real ``load_dynamic_v3`` command and validates the simulator's live target,
team-lifecycle, and idle-lifecycle blocks.  No Python wait loop emulates an
unattackable interval.  All configured values remain simulator hypotheses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import struct
from typing import Any, Mapping

from .sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    SimBridgeProtocolError,
)
from . import sim_bridge_dynamic_v2 as _v2
from .sim_bridge_dynamic_v2 import (
    DynamicArmorTransitionReceiptV2,
    DynamicAttackabilityEventV2,
    DynamicAttackabilityTransitionReceiptV2,
    DynamicCandidateDamageReceiptV2,
    DynamicDamageReceiptV2,
    DynamicEffectiveArmorEventV2,
    DynamicLifecycleTargetStateV2,
    DynamicTargetRuntimeStateV2,
    DynamicTargetSemanticsConfigV2,
    SimulatorBridgeDynamicV2,
)


JSONMap = dict[str, Any]

DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3 = "o2o_dynamic_target_semantics/v3"
DYNAMIC_SAME_TIMESTAMP_ORDER_V3 = (
    "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_IDLE_WAKE_BEFORE_CANDIDATE"
)
DYNAMIC_IDLE_ADVANCE_MODE_V3 = "CENTRAL_TO_NEXT_ATTACKABLE_OR_HORIZON"
DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3 = (
    "o2o_dynamic_idle_advance_receipts/v3"
)
DYNAMIC_IDLE_ADVANCE_STATUSES_V3 = frozenset(
    {
        "ATTACKABILITY_RESTORED",
        "ALL_TARGETS_DEAD",
        "SCENARIO_HORIZON_REACHED",
        "SIMULATOR_TERMINAL",
    }
)
DYNAMIC_IDLE_WAKE_SOURCES_V3 = frozenset(
    {"NEXT_ATTACKABILITY_TRUE", "SCENARIO_HORIZON"}
)
_MAX_DYNAMIC_TIME_MS_V3 = ((1 << 63) - 1) // 1_000_000


class DynamicV3ConfigError(ValueError):
    """A dynamic-v3 config is malformed or not content bound."""


@dataclass(frozen=True)
class DynamicTargetSemanticsConfigV3:
    target_health: tuple[DynamicTargetHealthV1, ...]
    idle_advance_horizon_ms: int
    background_damage_events: tuple[BackgroundDamageEventV1, ...] = ()
    attackability_events: tuple[DynamicAttackabilityEventV2, ...] = ()
    effective_armor_events: tuple[DynamicEffectiveArmorEventV2, ...] = ()
    idle_advance_mode: str = DYNAMIC_IDLE_ADVANCE_MODE_V3
    same_timestamp_order: str = DYNAMIC_SAME_TIMESTAMP_ORDER_V3
    retarget_mode: str = "NEXT_ALIVE_CYCLIC"
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            _v2_projection(self)
        except (TypeError, ValueError) as error:
            raise DynamicV3ConfigError(str(error)) from error
        if self.idle_advance_mode != DYNAMIC_IDLE_ADVANCE_MODE_V3:
            raise DynamicV3ConfigError(
                "idle_advance_mode must be "
                f"{DYNAMIC_IDLE_ADVANCE_MODE_V3!r}"
            )
        horizon = _strict_nonnegative_int(
            self.idle_advance_horizon_ms, "idle_advance_horizon_ms"
        )
        if horizon <= 0 or horizon > _MAX_DYNAMIC_TIME_MS_V3:
            raise DynamicV3ConfigError(
                "idle_advance_horizon_ms is outside the supported positive "
                "duration range"
            )
        if self.same_timestamp_order != DYNAMIC_SAME_TIMESTAMP_ORDER_V3:
            raise DynamicV3ConfigError(
                "same_timestamp_order must be "
                f"{DYNAMIC_SAME_TIMESTAMP_ORDER_V3!r}"
            )
        for label, rows in (
            ("background_damage_events", self.background_damage_events),
            ("attackability_events", self.attackability_events),
            ("effective_armor_events", self.effective_armor_events),
        ):
            for index, row in enumerate(rows):
                if row.time_ms > horizon:
                    raise DynamicV3ConfigError(
                        f"{label}[{index}].time_ms exceeds "
                        "idle_advance_horizon_ms"
                    )
        object.__setattr__(
            self, "content_sha256", dynamic_target_semantics_digest_v3(self)
        )

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            "content_sha256": self.content_sha256,
            "target_health": [row.to_wire() for row in self.target_health],
            "background_damage_events": [
                row.to_wire() for row in self.background_damage_events
            ],
            "attackability_events": [
                row.to_wire() for row in self.attackability_events
            ],
            "effective_armor_events": [
                row.to_wire() for row in self.effective_armor_events
            ],
            "idle_advance_mode": self.idle_advance_mode,
            "idle_advance_horizon_ms": self.idle_advance_horizon_ms,
            "same_timestamp_order": self.same_timestamp_order,
            "retarget_mode": self.retarget_mode,
        }


def _v2_projection(
    config: DynamicTargetSemanticsConfigV3,
) -> DynamicTargetSemanticsConfigV2:
    return DynamicTargetSemanticsConfigV2(
        target_health=config.target_health,
        background_damage_events=config.background_damage_events,
        attackability_events=config.attackability_events,
        effective_armor_events=config.effective_armor_events,
        same_timestamp_order=_v2.DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
        retarget_mode=config.retarget_mode,
    )


def dynamic_target_semantics_digest_v3(
    config: DynamicTargetSemanticsConfigV3,
) -> str:
    if not isinstance(config, DynamicTargetSemanticsConfigV3):
        raise TypeError("config must be DynamicTargetSemanticsConfigV3")
    document = {
        "attackability_events": [
            row.to_wire() for row in config.attackability_events
        ],
        "background_damage_events": [
            {
                "damage_ieee754": struct.pack(">d", row.damage).hex(),
                "event_id": row.event_id,
                "schedule_index": row.schedule_index,
                "target_index": row.target_index,
                "time_ms": row.time_ms,
            }
            for row in config.background_damage_events
        ],
        "effective_armor_events": [
            {
                "effective_armor_ieee754": struct.pack(
                    ">d", row.effective_armor
                ).hex(),
                "schedule_index": row.schedule_index,
                "target_index": row.target_index,
                "time_ms": row.time_ms,
            }
            for row in config.effective_armor_events
        ],
        "idle_advance_horizon_ms": config.idle_advance_horizon_ms,
        "idle_advance_mode": config.idle_advance_mode,
        "retarget_mode": config.retarget_mode,
        "same_timestamp_order": config.same_timestamp_order,
        "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        "target_health": [
            {
                "health_ieee754": struct.pack(">d", row.health).hex(),
                "target_index": row.target_index,
            }
            for row in config.target_health
        ],
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dynamic_target_semantics_config_from_wire_v3(
    value: Mapping[str, Any],
) -> DynamicTargetSemanticsConfigV3:
    try:
        raw = _v2._exact_mapping(
            value,
            {
                "schema",
                "content_sha256",
                "target_health",
                "background_damage_events",
                "attackability_events",
                "effective_armor_events",
                "idle_advance_mode",
                "idle_advance_horizon_ms",
                "same_timestamp_order",
                "retarget_mode",
            },
            "dynamic-v3 config",
        )
    except _v2.DynamicV2ConfigError as error:
        raise DynamicV3ConfigError(str(error)) from error
    if raw["schema"] != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3:
        raise DynamicV3ConfigError("dynamic-v3 config schema is unsupported")
    try:
        target_rows = _v2._object_array(raw["target_health"], "target_health")
        background_rows = _v2._object_array(
            raw["background_damage_events"], "background_damage_events"
        )
        attack_rows = _v2._object_array(
            raw["attackability_events"], "attackability_events"
        )
        armor_rows = _v2._object_array(
            raw["effective_armor_events"], "effective_armor_events"
        )
    except _v2.DynamicV2ConfigError as error:
        raise DynamicV3ConfigError(str(error)) from error
    try:
        config = DynamicTargetSemanticsConfigV3(
            target_health=tuple(
                DynamicTargetHealthV1(
                    target_index=_v2._strict_int_field(
                        _v2._exact_mapping(
                            row,
                            {"target_index", "health"},
                            f"target_health[{index}]",
                        ),
                        "target_index",
                    ),
                    health=_v2._number_field(row, "health"),
                )
                for index, row in enumerate(target_rows)
            ),
            background_damage_events=tuple(
                BackgroundDamageEventV1(
                    schedule_index=_v2._strict_int_field(
                        _v2._exact_mapping(
                            row,
                            {
                                "schedule_index",
                                "time_ms",
                                "target_index",
                                "event_id",
                                "damage",
                            },
                            f"background_damage_events[{index}]",
                        ),
                        "schedule_index",
                    ),
                    time_ms=_v2._strict_int_field(row, "time_ms"),
                    target_index=_v2._strict_int_field(row, "target_index"),
                    event_id=_v2._text_field(row, "event_id"),
                    damage=_v2._number_field(row, "damage"),
                )
                for index, row in enumerate(background_rows)
            ),
            attackability_events=tuple(
                DynamicAttackabilityEventV2(
                    schedule_index=_v2._strict_int_field(
                        _v2._exact_mapping(
                            row,
                            {
                                "schedule_index",
                                "time_ms",
                                "target_index",
                                "attackable",
                            },
                            f"attackability_events[{index}]",
                        ),
                        "schedule_index",
                    ),
                    time_ms=_v2._strict_int_field(row, "time_ms"),
                    target_index=_v2._strict_int_field(row, "target_index"),
                    attackable=_v2._strict_bool_field(row, "attackable"),
                )
                for index, row in enumerate(attack_rows)
            ),
            effective_armor_events=tuple(
                DynamicEffectiveArmorEventV2(
                    schedule_index=_v2._strict_int_field(
                        _v2._exact_mapping(
                            row,
                            {
                                "schedule_index",
                                "time_ms",
                                "target_index",
                                "effective_armor",
                            },
                            f"effective_armor_events[{index}]",
                        ),
                        "schedule_index",
                    ),
                    time_ms=_v2._strict_int_field(row, "time_ms"),
                    target_index=_v2._strict_int_field(row, "target_index"),
                    effective_armor=_v2._number_field(
                        row, "effective_armor"
                    ),
                )
                for index, row in enumerate(armor_rows)
            ),
            idle_advance_mode=_v2._text_field(raw, "idle_advance_mode"),
            idle_advance_horizon_ms=_v2._strict_int_field(
                raw, "idle_advance_horizon_ms"
            ),
            same_timestamp_order=_v2._text_field(
                raw, "same_timestamp_order"
            ),
            retarget_mode=_v2._text_field(raw, "retarget_mode"),
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, DynamicV3ConfigError):
            raise
        raise DynamicV3ConfigError(str(error)) from error
    digest = _v2._sha256_field(raw, "content_sha256")
    if digest != config.content_sha256:
        raise DynamicV3ConfigError("dynamic-v3 config content SHA-256 mismatch")
    return config


@dataclass(frozen=True)
class DynamicLoadReceiptV3:
    schema: str
    config_digest: str
    environment_generation: int
    target_count: int
    background_event_count: int
    attackability_event_count: int
    effective_armor_event_count: int
    same_timestamp_order: str
    retarget_mode: str
    idle_advance_mode: str
    idle_advance_horizon_ms: int
    idle_advance_receipt_schema: str


@dataclass(frozen=True)
class DynamicLoadResultV3:
    receipt: DynamicLoadReceiptV3
    state: JSONMap


@dataclass(frozen=True)
class DynamicTeamLifecycleStateV3:
    schema: str
    config_digest: str
    environment_generation: int
    same_timestamp_order: str
    retarget_mode: str
    retarget_required: bool
    simulated_damage_applied: float
    background_damage_applied: float
    combined_damage_applied: float
    background_events_processed: int
    background_events_total: int
    background_events_canceled: int
    candidate_events_processed: int
    candidate_events_canceled: int
    damage_applications_total: int
    targets: tuple[DynamicLifecycleTargetStateV2, ...]


@dataclass(frozen=True)
class DynamicTargetSemanticsStateV3:
    schema: str
    config_digest: str
    environment_generation: int
    same_timestamp_order: str
    attackability_events_processed: int
    attackability_events_total: int
    effective_armor_events_processed: int
    effective_armor_events_total: int
    targets: tuple[DynamicTargetRuntimeStateV2, ...]


@dataclass(frozen=True)
class DynamicIdleAdvanceStateV3:
    schema: str
    config_digest: str
    environment_generation: int
    mode: str
    horizon_ms: int
    active: bool
    receipts_processed: int
    total_auto_advanced_ms: int
    stream_closed: bool
    active_start_time_ms: int | None = None
    planned_wake_time_ms: int | None = None
    planned_wake_source: str | None = None


@dataclass(frozen=True)
class ParsedDynamicStateV3:
    team: DynamicTeamLifecycleStateV3
    target_semantics: DynamicTargetSemanticsStateV3
    idle_advance: DynamicIdleAdvanceStateV3


@dataclass(frozen=True)
class DynamicAttackabilityReceiptBatchV3:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicAttackabilityTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicArmorReceiptBatchV3:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicArmorTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicDamageReceiptBatchV3:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicDamageReceiptV2, ...]


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptBatchV3:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    receipts: tuple[DynamicCandidateDamageReceiptV2, ...]


@dataclass(frozen=True)
class DynamicIdleAdvanceReceiptV3:
    idle_advance_ordinal: int
    start_time_ms: int
    end_time_ms: int
    planned_wake_time_ms: int
    wake_source: str
    wake_attempt_count: int
    status: str
    attackable_targets_before: int
    attackable_targets_after: int
    attackability_cursor_start: int
    attackability_cursor_end: int
    armor_cursor_start: int
    armor_cursor_end: int
    background_cursor_start: int
    background_cursor_end: int
    candidate_cursor_start: int
    candidate_cursor_end: int
    policy_actions_consumed: int
    policy_target_selections_consumed: int
    scheduler_random_draws: int
    environment_retargeted: bool
    auto_advanced_duration_ms: int
    start_target_index: int | None = None
    end_target_index: int | None = None


@dataclass(frozen=True)
class DynamicIdleAdvanceReceiptBatchV3:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    stream_closed: bool
    active: bool
    receipts: tuple[DynamicIdleAdvanceReceiptV3, ...]


class SimulatorBridgeDynamicV3(SimulatorBridgeDynamicV2):
    """Persistent bridge with central, simulator-owned idle advancement."""

    def load_dynamic_v3(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV3,
    ) -> DynamicLoadResultV3:
        if not isinstance(request, Mapping):
            raise TypeError("RaidSimRequest must be a mapping")
        if not isinstance(config, DynamicTargetSemanticsConfigV3):
            raise TypeError("config must be DynamicTargetSemanticsConfigV3")
        self._dynamic_binding = None
        request_copy = _v2._strict_json_object_v2(request, "RaidSimRequest")
        _validate_request_horizon_v3(request_copy, config)
        response = self._request(
            "load_dynamic_v3",
            request=request_copy,
            seed=_v2._signed_int64(seed, "seed"),
            dynamic=config.to_wire(),
        )
        raw_receipt = response.get("dynamic_load")
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError(
                "load_dynamic_v3 response is missing dynamic_load receipt"
            )
        receipt = _dynamic_load_receipt_v3(raw_receipt, config)
        response_generation = _v2._positive_int_field(
            response, "environment_generation"
        )
        if response_generation != receipt.environment_generation:
            raise SimBridgeProtocolError(
                "load_dynamic_v3 response generation differs from its receipt"
            )
        state = _v2._state_field(response, "load_dynamic_v3")
        _validate_dynamic_state_binding_v3(
            state, generation=receipt.environment_generation, config=config
        )
        self._dynamic_binding = (
            receipt.environment_generation,
            receipt.config_digest,
            config,
        )
        return DynamicLoadResultV3(receipt=receipt, state=state)

    def _validate_bound_state(self, state: Mapping[str, Any]) -> None:
        if self._dynamic_binding is not None and isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV3
        ):
            generation, _, config = self._dynamic_binding
            _validate_dynamic_state_binding_v3(
                state, generation=generation, config=config
            )
            return
        super()._validate_bound_state(state)

    def parsed_dynamic_state(
        self, state: Mapping[str, Any] | None = None
    ) -> ParsedDynamicStateV3 | Any:
        if self._dynamic_binding is None or not isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV3
        ):
            return super().parsed_dynamic_state(state)
        config, generation = self._require_v3_binding("parsed_dynamic_state")
        current = self.state() if state is None else dict(state)
        return _validate_dynamic_state_binding_v3(
            current, generation=generation, config=config
        )

    def dynamic_attackability_receipts(
        self, *, cursor: int = 0
    ) -> DynamicAttackabilityReceiptBatchV3 | Any:
        if not self._is_v3_bound():
            return super().dynamic_attackability_receipts(cursor=cursor)
        config, generation = self._require_v3_binding(
            "dynamic_attackability_receipts"
        )
        normalized = _strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload(
            "dynamic_attackability_receipts", normalized
        )
        projected = _project_envelope_v3_to_v2(raw, config)
        parsed = _v2._attackability_batch_v2(
            projected,
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicAttackabilityReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            schedule_complete=parsed.schedule_complete,
            receipts=parsed.receipts,
        )

    def dynamic_armor_receipts(
        self, *, cursor: int = 0
    ) -> DynamicArmorReceiptBatchV3 | Any:
        if not self._is_v3_bound():
            return super().dynamic_armor_receipts(cursor=cursor)
        config, generation = self._require_v3_binding(
            "dynamic_armor_receipts"
        )
        normalized = _strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload("dynamic_armor_receipts", normalized)
        parsed = _v2._armor_batch_v2(
            _project_envelope_v3_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicArmorReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            schedule_complete=parsed.schedule_complete,
            receipts=parsed.receipts,
        )

    def dynamic_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicDamageReceiptBatchV3 | Any:
        if not self._is_v3_bound():
            return super().dynamic_damage_receipts(cursor=cursor)
        config, generation = self._require_v3_binding(
            "dynamic_damage_receipts"
        )
        normalized = _strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload("dynamic_damage_receipts", normalized)
        parsed = _v2._background_batch_v2(
            _project_envelope_v3_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicDamageReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            schedule_complete=parsed.schedule_complete,
            receipts=parsed.receipts,
        )

    def dynamic_candidate_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicCandidateDamageReceiptBatchV3 | Any:
        if not self._is_v3_bound():
            return super().dynamic_candidate_damage_receipts(cursor=cursor)
        config, generation = self._require_v3_binding(
            "dynamic_candidate_damage_receipts"
        )
        normalized = _strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload(
            "dynamic_candidate_damage_receipts", normalized
        )
        parsed = _v2._candidate_batch_v2(
            _project_envelope_v3_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicCandidateDamageReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            receipts=parsed.receipts,
        )

    def dynamic_idle_advance_receipts(
        self, *, cursor: int = 0
    ) -> DynamicIdleAdvanceReceiptBatchV3:
        config, generation = self._require_v3_binding(
            "dynamic_idle_advance_receipts"
        )
        normalized = _strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload(
            "dynamic_idle_advance_receipts", normalized
        )
        return _idle_receipt_batch_v3(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )

    def _receipt_payload(self, command: str, cursor: int) -> Mapping[str, Any]:
        response = self._request(command, cursor=cursor)
        raw = response.get(command)
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(f"{command} response lacks its batch")
        return raw

    def _is_v3_bound(self) -> bool:
        return self._dynamic_binding is not None and isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV3
        )

    def _require_v3_binding(
        self, command: str
    ) -> tuple[DynamicTargetSemanticsConfigV3, int]:
        if not self._is_v3_bound():
            raise SimBridgeProtocolError(
                f"{command} requires a successful load_dynamic_v3"
            )
        generation, _, config = self._dynamic_binding
        return config, generation


def _validate_request_horizon_v3(
    request: Mapping[str, Any], config: DynamicTargetSemanticsConfigV3
) -> None:
    encounter = request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise DynamicV3ConfigError("RaidSimRequest encounter must be an object")
    duration = encounter.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise DynamicV3ConfigError("RaidSimRequest encounter.duration must be numeric")
    numeric = float(duration)
    if not math.isfinite(numeric) or numeric <= 0:
        raise DynamicV3ConfigError(
            "RaidSimRequest encounter.duration must be finite and positive"
        )
    # RaidSimRequest stores seconds as float64 while dynamic-v3 owns an integer
    # millisecond horizon.  Round the float product to the nearest nanosecond,
    # matching Go's math.Round conversion, so an exact integer-millisecond
    # value is not rejected after binary float encoding (for example 8.059s).
    request_ns = math.floor(numeric * 1_000_000_000 + 0.5)
    if request_ns != config.idle_advance_horizon_ms * 1_000_000:
        raise DynamicV3ConfigError(
            "idle_advance_horizon_ms does not exactly match "
            "RaidSimRequest encounter.duration"
        )


def _dynamic_load_receipt_v3(
    value: Mapping[str, Any], config: DynamicTargetSemanticsConfigV3
) -> DynamicLoadReceiptV3:
    raw = _v2._exact_mapping(
        value,
        {
            "schema",
            "config_digest",
            "environment_generation",
            "target_count",
            "background_event_count",
            "attackability_event_count",
            "effective_armor_event_count",
            "same_timestamp_order",
            "retarget_mode",
            "idle_advance_mode",
            "idle_advance_horizon_ms",
            "idle_advance_receipt_schema",
        },
        "dynamic-v3 load receipt",
        protocol=True,
    )
    receipt = DynamicLoadReceiptV3(
        schema=_v2._text_field(raw, "schema", protocol=True),
        config_digest=_v2._sha256_field(
            raw, "config_digest", protocol=True
        ),
        environment_generation=_v2._positive_int_field(
            raw, "environment_generation"
        ),
        target_count=_v2._nonnegative_int_field(raw, "target_count"),
        background_event_count=_v2._nonnegative_int_field(
            raw, "background_event_count"
        ),
        attackability_event_count=_v2._nonnegative_int_field(
            raw, "attackability_event_count"
        ),
        effective_armor_event_count=_v2._nonnegative_int_field(
            raw, "effective_armor_event_count"
        ),
        same_timestamp_order=_v2._text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        retarget_mode=_v2._text_field(
            raw, "retarget_mode", protocol=True
        ),
        idle_advance_mode=_v2._text_field(
            raw, "idle_advance_mode", protocol=True
        ),
        idle_advance_horizon_ms=_v2._positive_int_field(
            raw, "idle_advance_horizon_ms"
        ),
        idle_advance_receipt_schema=_v2._text_field(
            raw, "idle_advance_receipt_schema", protocol=True
        ),
    )
    expected = (
        DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        len(config.attackability_events),
        len(config.effective_armor_events),
        config.same_timestamp_order,
        config.retarget_mode,
        config.idle_advance_mode,
        config.idle_advance_horizon_ms,
        DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    )
    observed = (
        receipt.schema,
        receipt.config_digest,
        receipt.target_count,
        receipt.background_event_count,
        receipt.attackability_event_count,
        receipt.effective_armor_event_count,
        receipt.same_timestamp_order,
        receipt.retarget_mode,
        receipt.idle_advance_mode,
        receipt.idle_advance_horizon_ms,
        receipt.idle_advance_receipt_schema,
    )
    if observed != expected:
        raise SimBridgeProtocolError(
            "dynamic-v3 load receipt differs from the requested config"
        )
    return receipt


def _project_envelope_v3_to_v2(
    value: Mapping[str, Any], config: DynamicTargetSemanticsConfigV3
) -> JSONMap:
    projected = dict(value)
    v2_config = _v2_projection(config)
    projected["schema"] = _v2.DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
    projected["config_digest"] = v2_config.content_sha256
    return projected


def _validate_dynamic_state_binding_v3(
    state: Mapping[str, Any],
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV3,
) -> ParsedDynamicStateV3:
    if not isinstance(state, Mapping):
        raise SimBridgeProtocolError("simulator state must be an object")
    v2_config = _v2_projection(config)
    projected = dict(state)
    for key in ("dynamic_team_background", "dynamic_target_semantics"):
        raw = state.get(key)
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(f"state lacks {key}")
        block = dict(raw)
        block["schema"] = _v2.DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        block["config_digest"] = v2_config.content_sha256
        block["same_timestamp_order"] = (
            _v2.DYNAMIC_SAME_TIMESTAMP_ORDER_V2
        )
        projected[key] = block
    team_v2, semantics_v2 = _v2._validate_dynamic_state_binding_v2(
        projected, generation=generation, config=v2_config
    )
    team = DynamicTeamLifecycleStateV3(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        config_digest=config.content_sha256,
        environment_generation=team_v2.environment_generation,
        same_timestamp_order=config.same_timestamp_order,
        retarget_mode=team_v2.retarget_mode,
        retarget_required=team_v2.retarget_required,
        simulated_damage_applied=team_v2.simulated_damage_applied,
        background_damage_applied=team_v2.background_damage_applied,
        combined_damage_applied=team_v2.combined_damage_applied,
        background_events_processed=team_v2.background_events_processed,
        background_events_total=team_v2.background_events_total,
        background_events_canceled=team_v2.background_events_canceled,
        candidate_events_processed=team_v2.candidate_events_processed,
        candidate_events_canceled=team_v2.candidate_events_canceled,
        damage_applications_total=team_v2.damage_applications_total,
        targets=team_v2.targets,
    )
    semantics = DynamicTargetSemanticsStateV3(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        config_digest=config.content_sha256,
        environment_generation=semantics_v2.environment_generation,
        same_timestamp_order=config.same_timestamp_order,
        attackability_events_processed=(
            semantics_v2.attackability_events_processed
        ),
        attackability_events_total=semantics_v2.attackability_events_total,
        effective_armor_events_processed=(
            semantics_v2.effective_armor_events_processed
        ),
        effective_armor_events_total=(
            semantics_v2.effective_armor_events_total
        ),
        targets=semantics_v2.targets,
    )
    idle = _parse_idle_state_v3(
        state.get("dynamic_idle_advance"),
        state=state,
        generation=generation,
        config=config,
    )
    return ParsedDynamicStateV3(
        team=team, target_semantics=semantics, idle_advance=idle
    )


def _parse_idle_state_v3(
    value: Any,
    *,
    state: Mapping[str, Any],
    generation: int,
    config: DynamicTargetSemanticsConfigV3,
) -> DynamicIdleAdvanceStateV3:
    raw = _v2._mapping_with_optional(
        value,
        {
            "schema",
            "config_digest",
            "environment_generation",
            "mode",
            "horizon_ms",
            "active",
            "receipts_processed",
            "total_auto_advanced_ms",
            "stream_closed",
        },
        {
            "active_start_time_ms",
            "planned_wake_time_ms",
            "planned_wake_source",
        },
        "dynamic idle state",
    )
    active = _v2._strict_bool_field(raw, "active")
    optional = {
        "active_start_time_ms",
        "planned_wake_time_ms",
        "planned_wake_source",
    }
    present = optional.intersection(raw)
    if (active and present != optional) or (not active and present):
        raise SimBridgeProtocolError(
            "dynamic idle active fields disagree with active flag"
        )
    result = DynamicIdleAdvanceStateV3(
        schema=_v2._text_field(raw, "schema", protocol=True),
        config_digest=_v2._sha256_field(
            raw, "config_digest", protocol=True
        ),
        environment_generation=_v2._positive_int_field(
            raw, "environment_generation"
        ),
        mode=_v2._text_field(raw, "mode", protocol=True),
        horizon_ms=_v2._positive_int_field(raw, "horizon_ms"),
        active=active,
        receipts_processed=_v2._nonnegative_int_field(
            raw, "receipts_processed"
        ),
        total_auto_advanced_ms=_v2._nonnegative_int_field(
            raw, "total_auto_advanced_ms"
        ),
        stream_closed=_v2._strict_bool_field(raw, "stream_closed"),
        active_start_time_ms=(
            _v2._nonnegative_int_field(raw, "active_start_time_ms")
            if "active_start_time_ms" in raw
            else None
        ),
        planned_wake_time_ms=(
            _v2._nonnegative_int_field(raw, "planned_wake_time_ms")
            if "planned_wake_time_ms" in raw
            else None
        ),
        planned_wake_source=(
            _v2._text_field(raw, "planned_wake_source", protocol=True)
            if "planned_wake_source" in raw
            else None
        ),
    )
    finished = _v2._strict_bool_field(state, "finished")
    needs_input = _v2._strict_bool_field(state, "needs_input")
    time_ms = _v2._nonnegative_int_field(state, "time_ms")
    num_targets = _v2._nonnegative_int_field(state, "num_targets")
    if (
        result.schema != DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
        or result.config_digest != config.content_sha256
        or result.environment_generation != generation
        or result.mode != config.idle_advance_mode
        or result.horizon_ms != config.idle_advance_horizon_ms
        or result.stream_closed != finished
        or time_ms > result.horizon_ms
        or result.total_auto_advanced_ms > time_ms
        or result.active
        and (
            needs_input
            or num_targets != 0
            or result.planned_wake_source
            not in DYNAMIC_IDLE_WAKE_SOURCES_V3
            or result.active_start_time_ms is None
            or result.planned_wake_time_ms is None
            or result.active_start_time_ms > time_ms
            or result.planned_wake_time_ms < time_ms
            or result.planned_wake_time_ms > result.horizon_ms
        )
        or needs_input
        and num_targets == 0
    ):
        raise SimBridgeProtocolError(
            "dynamic idle state violates its lifecycle/horizon binding"
        )
    return result


def _idle_receipt_batch_v3(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV3,
) -> DynamicIdleAdvanceReceiptBatchV3:
    raw = _v2._exact_mapping(
        value,
        {
            "schema",
            "config_digest",
            "environment_generation",
            "cursor",
            "next_cursor",
            "stream_closed",
            "active",
            "receipts",
        },
        "dynamic idle receipt batch",
        protocol=True,
    )
    rows = _v2._object_array(
        raw["receipts"], "dynamic idle receipts", protocol=True
    )
    cursor = _v2._nonnegative_int_field(raw, "cursor")
    next_cursor = _v2._nonnegative_int_field(raw, "next_cursor")
    stream_closed = _v2._strict_bool_field(raw, "stream_closed")
    active = _v2._strict_bool_field(raw, "active")
    if (
        _v2._text_field(raw, "schema", protocol=True)
        != DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
        or _v2._sha256_field(raw, "config_digest", protocol=True)
        != config.content_sha256
        or _v2._positive_int_field(raw, "environment_generation")
        != generation
        or cursor != requested_cursor
        or next_cursor != cursor + len(rows)
        or stream_closed
        and active
    ):
        raise SimBridgeProtocolError(
            "dynamic idle receipt batch violates cursor/content lifecycle"
        )
    receipts: list[DynamicIdleAdvanceReceiptV3] = []
    for offset, value_row in enumerate(rows):
        row = _v2._mapping_with_optional(
            value_row,
            {
                "idle_advance_ordinal",
                "start_time_ms",
                "end_time_ms",
                "planned_wake_time_ms",
                "wake_source",
                "wake_attempt_count",
                "status",
                "attackable_targets_before",
                "attackable_targets_after",
                "attackability_cursor_start",
                "attackability_cursor_end",
                "armor_cursor_start",
                "armor_cursor_end",
                "background_cursor_start",
                "background_cursor_end",
                "candidate_cursor_start",
                "candidate_cursor_end",
                "policy_actions_consumed",
                "policy_target_selections_consumed",
                "scheduler_random_draws",
                "environment_retargeted",
                "auto_advanced_duration_ms",
            },
            {"start_target_index", "end_target_index"},
            "dynamic idle receipt",
        )
        receipt = DynamicIdleAdvanceReceiptV3(
            idle_advance_ordinal=_v2._nonnegative_int_field(
                row, "idle_advance_ordinal"
            ),
            start_time_ms=_v2._nonnegative_int_field(row, "start_time_ms"),
            end_time_ms=_v2._nonnegative_int_field(row, "end_time_ms"),
            planned_wake_time_ms=_v2._nonnegative_int_field(
                row, "planned_wake_time_ms"
            ),
            wake_source=_v2._text_field(
                row, "wake_source", protocol=True
            ),
            wake_attempt_count=_v2._positive_int_field(
                row, "wake_attempt_count"
            ),
            status=_v2._text_field(row, "status", protocol=True),
            attackable_targets_before=_v2._nonnegative_int_field(
                row, "attackable_targets_before"
            ),
            attackable_targets_after=_v2._nonnegative_int_field(
                row, "attackable_targets_after"
            ),
            attackability_cursor_start=_v2._nonnegative_int_field(
                row, "attackability_cursor_start"
            ),
            attackability_cursor_end=_v2._nonnegative_int_field(
                row, "attackability_cursor_end"
            ),
            armor_cursor_start=_v2._nonnegative_int_field(
                row, "armor_cursor_start"
            ),
            armor_cursor_end=_v2._nonnegative_int_field(
                row, "armor_cursor_end"
            ),
            background_cursor_start=_v2._nonnegative_int_field(
                row, "background_cursor_start"
            ),
            background_cursor_end=_v2._nonnegative_int_field(
                row, "background_cursor_end"
            ),
            candidate_cursor_start=_v2._nonnegative_int_field(
                row, "candidate_cursor_start"
            ),
            candidate_cursor_end=_v2._nonnegative_int_field(
                row, "candidate_cursor_end"
            ),
            policy_actions_consumed=_v2._nonnegative_int_field(
                row, "policy_actions_consumed"
            ),
            policy_target_selections_consumed=_v2._nonnegative_int_field(
                row, "policy_target_selections_consumed"
            ),
            scheduler_random_draws=_v2._nonnegative_int_field(
                row, "scheduler_random_draws"
            ),
            environment_retargeted=_v2._strict_bool_field(
                row, "environment_retargeted"
            ),
            auto_advanced_duration_ms=_v2._nonnegative_int_field(
                row, "auto_advanced_duration_ms"
            ),
            start_target_index=(
                _v2._nonnegative_int_field(row, "start_target_index")
                if "start_target_index" in row
                else None
            ),
            end_target_index=(
                _v2._nonnegative_int_field(row, "end_target_index")
                if "end_target_index" in row
                else None
            ),
        )
        if not _valid_idle_receipt_v3(
            receipt,
            expected_ordinal=cursor + offset,
            config=config,
        ):
            raise SimBridgeProtocolError(
                "dynamic idle receipt violates scheduler lifecycle invariants"
            )
        receipts.append(receipt)
    if receipts and receipts[-1].status in {
        "ALL_TARGETS_DEAD",
        "SCENARIO_HORIZON_REACHED",
        "SIMULATOR_TERMINAL",
    } and not stream_closed:
        raise SimBridgeProtocolError(
            "terminal dynamic idle receipt did not close its stream"
        )
    return DynamicIdleAdvanceReceiptBatchV3(
        schema=DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        stream_closed=stream_closed,
        active=active,
        receipts=tuple(receipts),
    )


def _valid_idle_receipt_v3(
    receipt: DynamicIdleAdvanceReceiptV3,
    *,
    expected_ordinal: int,
    config: DynamicTargetSemanticsConfigV3,
) -> bool:
    indexes = (receipt.start_target_index, receipt.end_target_index)
    cursor_pairs = (
        (receipt.attackability_cursor_start, receipt.attackability_cursor_end),
        (receipt.armor_cursor_start, receipt.armor_cursor_end),
        (receipt.background_cursor_start, receipt.background_cursor_end),
        (receipt.candidate_cursor_start, receipt.candidate_cursor_end),
    )
    if (
        receipt.idle_advance_ordinal != expected_ordinal
        or receipt.status not in DYNAMIC_IDLE_ADVANCE_STATUSES_V3
        or receipt.wake_source not in DYNAMIC_IDLE_WAKE_SOURCES_V3
        or receipt.start_time_ms > receipt.end_time_ms
        or receipt.end_time_ms > config.idle_advance_horizon_ms
        or receipt.planned_wake_time_ms < receipt.end_time_ms
        or receipt.planned_wake_time_ms > config.idle_advance_horizon_ms
        or receipt.auto_advanced_duration_ms
        != receipt.end_time_ms - receipt.start_time_ms
        or receipt.attackable_targets_before != 0
        or receipt.attackable_targets_after > len(config.target_health)
        or receipt.policy_actions_consumed != 0
        or receipt.policy_target_selections_consumed != 0
        or receipt.scheduler_random_draws != 0
        or any(start > end for start, end in cursor_pairs)
        or receipt.attackability_cursor_end
        > len(config.attackability_events)
        or receipt.armor_cursor_end > len(config.effective_armor_events)
        or receipt.background_cursor_end
        > len(config.background_damage_events)
        or any(
            index is not None and index >= len(config.target_health)
            for index in indexes
        )
    ):
        return False
    if receipt.status == "ATTACKABILITY_RESTORED":
        return (
            receipt.attackable_targets_after > 0
            and receipt.wake_source == "NEXT_ATTACKABILITY_TRUE"
            and receipt.planned_wake_time_ms == receipt.end_time_ms
        )
    if receipt.status == "SCENARIO_HORIZON_REACHED":
        return (
            receipt.end_time_ms == config.idle_advance_horizon_ms
            and receipt.wake_source == "SCENARIO_HORIZON"
            and receipt.planned_wake_time_ms == receipt.end_time_ms
        )
    if receipt.status == "ALL_TARGETS_DEAD":
        return receipt.attackable_targets_after == 0
    return True


def _strict_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DynamicV3ConfigError(f"{label} must be a non-negative integer")
    return value


__all__ = (
    "DYNAMIC_IDLE_ADVANCE_MODE_V3",
    "DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3",
    "DYNAMIC_SAME_TIMESTAMP_ORDER_V3",
    "DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3",
    "DynamicArmorReceiptBatchV3",
    "DynamicAttackabilityReceiptBatchV3",
    "DynamicCandidateDamageReceiptBatchV3",
    "DynamicDamageReceiptBatchV3",
    "DynamicIdleAdvanceReceiptBatchV3",
    "DynamicIdleAdvanceReceiptV3",
    "DynamicIdleAdvanceStateV3",
    "DynamicLoadReceiptV3",
    "DynamicLoadResultV3",
    "DynamicTargetSemanticsConfigV3",
    "DynamicTargetSemanticsStateV3",
    "DynamicTeamLifecycleStateV3",
    "DynamicV3ConfigError",
    "ParsedDynamicStateV3",
    "SimulatorBridgeDynamicV3",
    "dynamic_target_semantics_config_from_wire_v3",
    "dynamic_target_semantics_digest_v3",
)
