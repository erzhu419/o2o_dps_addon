"""Strict dynamic-v4 bridge client with source-window HP checkpoints.

Dynamic v4 keeps the immutable maximum-health denominator in the simulator
request while restoring a distinct current-health checkpoint at the start of a
historical window.  It also validates the optional responsive-teammate state
that shares the authoritative Go target-damage lifecycle.
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
from . import sim_bridge_dynamic_v3 as _v3
from .sim_bridge_dynamic_v2 import (
    DynamicArmorTransitionReceiptV2,
    DynamicAttackabilityEventV2,
    DynamicAttackabilityTransitionReceiptV2,
    DynamicCandidateDamageReceiptV2,
    DynamicEffectiveArmorEventV2,
)


JSONMap = dict[str, Any]

DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4 = "o2o_dynamic_target_semantics/v4"
DYNAMIC_TEAM_RESPONSE_STATE_SCHEMA_V1 = (
    "o2o_dynamic_team_response_receipts/v2"
)
DYNAMIC_TEAM_WAKE_SCHEMA_V1 = "o2o_dynamic_team_wake/v1"
DYNAMIC_SAME_TIMESTAMP_ORDER_V4 = (
    "TARGET_SEMANTICS_BEFORE_FIXED_BACKGROUND_BEFORE_RESPONSIVE_TEAM_WAKE_"
    "BEFORE_ENVIRONMENT_WAKE_BEFORE_CANDIDATE"
)


class DynamicV4ConfigError(ValueError):
    """A dynamic-v4 config or its RaidSimRequest binding is malformed."""


@dataclass(frozen=True)
class DynamicTargetHealthV4:
    target_index: int
    maximum_health: float
    current_health: float

    def __post_init__(self) -> None:
        try:
            target_index = _v2._nonnegative_int(
                self.target_index, "target_index"
            )
            maximum = _positive_finite_number(
                self.maximum_health, "maximum_health"
            )
            current = _positive_finite_number(
                self.current_health, "current_health"
            )
        except (TypeError, ValueError) as error:
            raise DynamicV4ConfigError(str(error)) from error
        if current > maximum:
            raise DynamicV4ConfigError(
                "current_health must be no greater than maximum_health"
            )
        object.__setattr__(self, "target_index", target_index)
        object.__setattr__(self, "maximum_health", maximum)
        object.__setattr__(self, "current_health", current)

    def to_wire(self) -> JSONMap:
        return {
            "target_index": self.target_index,
            "maximum_health": self.maximum_health,
            "current_health": self.current_health,
        }


@dataclass(frozen=True)
class DynamicTargetSemanticsConfigV4:
    target_health: tuple[DynamicTargetHealthV4, ...]
    idle_advance_horizon_ms: int
    background_damage_events: tuple[BackgroundDamageEventV1, ...] = ()
    attackability_events: tuple[DynamicAttackabilityEventV2, ...] = ()
    effective_armor_events: tuple[DynamicEffectiveArmorEventV2, ...] = ()
    idle_advance_mode: str = _v3.DYNAMIC_IDLE_ADVANCE_MODE_V3
    same_timestamp_order: str = DYNAMIC_SAME_TIMESTAMP_ORDER_V4
    retarget_mode: str = "NEXT_ALIVE_CYCLIC"
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target_health, tuple) or any(
            not isinstance(row, DynamicTargetHealthV4)
            for row in self.target_health
        ):
            raise TypeError(
                "target_health must be a tuple of DynamicTargetHealthV4"
            )
        if not self.target_health or tuple(
            row.target_index for row in self.target_health
        ) != tuple(range(len(self.target_health))):
            raise DynamicV4ConfigError(
                "target_health must cover target indexes 0..N-1 in order"
            )
        if self.same_timestamp_order != DYNAMIC_SAME_TIMESTAMP_ORDER_V4:
            raise DynamicV4ConfigError(
                "same_timestamp_order must be "
                f"{DYNAMIC_SAME_TIMESTAMP_ORDER_V4!r}"
            )
        try:
            _v3_projection(self)
        except (TypeError, ValueError) as error:
            raise DynamicV4ConfigError(str(error)) from error
        object.__setattr__(
            self, "content_sha256", dynamic_target_semantics_digest_v4(self)
        )

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
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


def _v3_projection(
    config: DynamicTargetSemanticsConfigV4,
) -> _v3.DynamicTargetSemanticsConfigV3:
    return _v3.DynamicTargetSemanticsConfigV3(
        target_health=tuple(
            DynamicTargetHealthV1(row.target_index, row.maximum_health)
            for row in config.target_health
        ),
        idle_advance_horizon_ms=config.idle_advance_horizon_ms,
        background_damage_events=config.background_damage_events,
        attackability_events=config.attackability_events,
        effective_armor_events=config.effective_armor_events,
        idle_advance_mode=config.idle_advance_mode,
        same_timestamp_order=_v3.DYNAMIC_SAME_TIMESTAMP_ORDER_V3,
        retarget_mode=config.retarget_mode,
    )


def _v2_projection(
    config: DynamicTargetSemanticsConfigV4,
) -> _v2.DynamicTargetSemanticsConfigV2:
    return _v3._v2_projection(_v3_projection(config))


def dynamic_target_semantics_digest_v4(
    config: DynamicTargetSemanticsConfigV4,
) -> str:
    if not isinstance(config, DynamicTargetSemanticsConfigV4):
        raise TypeError("config must be DynamicTargetSemanticsConfigV4")
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
        "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
        "target_health": [
            {
                "current_health_ieee754": struct.pack(
                    ">d", row.current_health
                ).hex(),
                "maximum_health_ieee754": struct.pack(
                    ">d", row.maximum_health
                ).hex(),
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


def dynamic_target_semantics_config_from_wire_v4(
    value: Mapping[str, Any],
) -> DynamicTargetSemanticsConfigV4:
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
            "dynamic-v4 config",
        )
        if raw["schema"] != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4:
            raise DynamicV4ConfigError(
                "dynamic-v4 config schema is unsupported"
            )
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
        config = DynamicTargetSemanticsConfigV4(
            target_health=tuple(
                DynamicTargetHealthV4(
                    target_index=_v2._strict_int_field(
                        _v2._exact_mapping(
                            row,
                            {
                                "target_index",
                                "maximum_health",
                                "current_health",
                            },
                            f"target_health[{index}]",
                        ),
                        "target_index",
                    ),
                    maximum_health=_v2._number_field(
                        row, "maximum_health"
                    ),
                    current_health=_v2._number_field(row, "current_health"),
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
        digest = _v2._sha256_field(raw, "content_sha256")
    except (TypeError, ValueError) as error:
        if isinstance(error, DynamicV4ConfigError):
            raise
        raise DynamicV4ConfigError(str(error)) from error
    if digest != config.content_sha256:
        raise DynamicV4ConfigError(
            "dynamic-v4 config content SHA-256 mismatch"
        )
    return config


@dataclass(frozen=True)
class DynamicLoadReceiptV4:
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
class DynamicLoadResultV4:
    receipt: DynamicLoadReceiptV4
    state: JSONMap


@dataclass(frozen=True)
class DynamicLifecycleTargetStateV4:
    target_index: int
    maximum_health: float
    initial_health: float
    current_health: float
    missing_health_at_checkpoint: float
    dead: bool
    simulated_damage_applied: float
    background_damage_applied: float
    death_time_ms: int | None = None


@dataclass(frozen=True)
class DynamicTeamLifecycleStateV4:
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
    background_damage_applications_processed: int
    candidate_events_processed: int
    candidate_events_canceled: int
    responsive_damage_applications_processed: int
    damage_applications_total: int
    targets: tuple[DynamicLifecycleTargetStateV4, ...]


@dataclass(frozen=True)
class DynamicTeamResponseStateV1:
    schema: str
    model_content_sha256: str
    environment_generation: int
    receipts_processed: int
    damage_applications_processed: int
    stream_closed: bool
    pending_wake_id: str | None = None
    pending_wake_time_ms: int | None = None


@dataclass(frozen=True)
class DynamicTeamWakeReadyV1:
    schema: str
    model_content_sha256: str
    wake_id: str
    time_ms: int


@dataclass(frozen=True)
class DynamicTargetRuntimeStateV4:
    target_index: int
    attackable: bool
    effective_armor: float
    maximum_health: float
    current_health: float
    dead: bool
    death_time_ms: int | None = None


@dataclass(frozen=True)
class DynamicTargetSemanticsStateV4:
    schema: str
    config_digest: str
    environment_generation: int
    same_timestamp_order: str
    attackability_events_processed: int
    attackability_events_total: int
    effective_armor_events_processed: int
    effective_armor_events_total: int
    targets: tuple[DynamicTargetRuntimeStateV4, ...]


@dataclass(frozen=True)
class ParsedDynamicStateV4:
    team: DynamicTeamLifecycleStateV4
    target_semantics: DynamicTargetSemanticsStateV4
    idle_advance: _v3.DynamicIdleAdvanceStateV3
    team_response: DynamicTeamResponseStateV1 | None
    wake_ready: DynamicTeamWakeReadyV1 | None


@dataclass(frozen=True)
class DynamicAttackabilityReceiptBatchV4:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicAttackabilityTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicArmorReceiptBatchV4:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicArmorTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicDamageReceiptV4:
    damage_ordinal: int | None
    schedule_index: int
    event_id: str
    time_ms: int
    target_index: int
    requested_damage: float
    applied_damage: float
    overkill_damage: float
    killed: bool
    status: str
    retargeted_to: int | None = None


@dataclass(frozen=True)
class DynamicDamageReceiptBatchV4:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicDamageReceiptV4, ...]


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptBatchV4:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    receipts: tuple[DynamicCandidateDamageReceiptV2, ...]


@dataclass(frozen=True)
class DynamicIdleAdvanceReceiptBatchV4:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    stream_closed: bool
    active: bool
    receipts: tuple[_v3.DynamicIdleAdvanceReceiptV3, ...]


class SimulatorBridgeDynamicV4(_v3.SimulatorBridgeDynamicV3):
    """Persistent typed bridge for dynamic-v4 checkpointed targets."""

    def load_dynamic_v4(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV4,
    ) -> DynamicLoadResultV4:
        if not isinstance(request, Mapping):
            raise TypeError("RaidSimRequest must be a mapping")
        if not isinstance(config, DynamicTargetSemanticsConfigV4):
            raise TypeError("config must be DynamicTargetSemanticsConfigV4")
        self._dynamic_binding = None
        request_copy = _v2._strict_json_object_v2(
            request, "RaidSimRequest"
        )
        _validate_request_binding_v4(request_copy, config)
        response = self._request(
            "load_dynamic_v4",
            request=request_copy,
            seed=_v2._signed_int64(seed, "seed"),
            dynamic=config.to_wire(),
        )
        raw_receipt = response.get("dynamic_load")
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError(
                "load_dynamic_v4 response is missing dynamic_load receipt"
            )
        receipt = _dynamic_load_receipt_v4(raw_receipt, config)
        response_generation = _v2._positive_int_field(
            response, "environment_generation"
        )
        if response_generation != receipt.environment_generation:
            raise SimBridgeProtocolError(
                "load_dynamic_v4 response generation differs from its receipt"
            )
        state = _v2._state_field(response, "load_dynamic_v4")
        _validate_dynamic_state_binding_v4(
            state,
            generation=receipt.environment_generation,
            config=config,
        )
        self._dynamic_binding = (
            receipt.environment_generation,
            receipt.config_digest,
            config,
        )
        return DynamicLoadResultV4(receipt=receipt, state=state)

    def _validate_bound_state(self, state: Mapping[str, Any]) -> None:
        if self._is_v4_bound():
            config, generation = self._require_v4_binding("state")
            _validate_dynamic_state_binding_v4(
                state, generation=generation, config=config
            )
            return
        super()._validate_bound_state(state)

    def parsed_dynamic_state(
        self, state: Mapping[str, Any] | None = None
    ) -> ParsedDynamicStateV4 | Any:
        if not self._is_v4_bound():
            return super().parsed_dynamic_state(state)
        config, generation = self._require_v4_binding(
            "parsed_dynamic_state"
        )
        current = self.state() if state is None else dict(state)
        return _validate_dynamic_state_binding_v4(
            current, generation=generation, config=config
        )

    def dynamic_attackability_receipts(
        self, *, cursor: int = 0
    ) -> DynamicAttackabilityReceiptBatchV4 | Any:
        if not self._is_v4_bound():
            return super().dynamic_attackability_receipts(cursor=cursor)
        config, generation = self._require_v4_binding(
            "dynamic_attackability_receipts"
        )
        normalized = _v3._strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload(
            "dynamic_attackability_receipts", normalized
        )
        parsed = _v2._attackability_batch_v2(
            _project_envelope_v4_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicAttackabilityReceiptBatchV4(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            schedule_complete=parsed.schedule_complete,
            receipts=parsed.receipts,
        )

    def dynamic_armor_receipts(
        self, *, cursor: int = 0
    ) -> DynamicArmorReceiptBatchV4 | Any:
        if not self._is_v4_bound():
            return super().dynamic_armor_receipts(cursor=cursor)
        config, generation = self._require_v4_binding(
            "dynamic_armor_receipts"
        )
        normalized = _v3._strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload("dynamic_armor_receipts", normalized)
        parsed = _v2._armor_batch_v2(
            _project_envelope_v4_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicArmorReceiptBatchV4(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            schedule_complete=parsed.schedule_complete,
            receipts=parsed.receipts,
        )

    def dynamic_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicDamageReceiptBatchV4 | Any:
        if not self._is_v4_bound():
            return super().dynamic_damage_receipts(cursor=cursor)
        config, generation = self._require_v4_binding(
            "dynamic_damage_receipts"
        )
        normalized = _v3._strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload("dynamic_damage_receipts", normalized)
        parsed = _background_batch_v4(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )
        return parsed

    def dynamic_candidate_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicCandidateDamageReceiptBatchV4 | Any:
        if not self._is_v4_bound():
            return super().dynamic_candidate_damage_receipts(cursor=cursor)
        config, generation = self._require_v4_binding(
            "dynamic_candidate_damage_receipts"
        )
        normalized = _v3._strict_nonnegative_int(cursor, "cursor")
        raw = self._receipt_payload(
            "dynamic_candidate_damage_receipts", normalized
        )
        parsed = _v2._candidate_batch_v2(
            _project_envelope_v4_to_v2(raw, config),
            requested_cursor=normalized,
            generation=generation,
            config=_v2_projection(config),
        )
        return DynamicCandidateDamageReceiptBatchV4(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            receipts=parsed.receipts,
        )

    def dynamic_idle_advance_receipts(
        self, *, cursor: int = 0
    ) -> DynamicIdleAdvanceReceiptBatchV4 | Any:
        if not self._is_v4_bound():
            return super().dynamic_idle_advance_receipts(cursor=cursor)
        config, generation = self._require_v4_binding(
            "dynamic_idle_advance_receipts"
        )
        normalized = _v3._strict_nonnegative_int(cursor, "cursor")
        raw = dict(
            self._receipt_payload(
                "dynamic_idle_advance_receipts", normalized
            )
        )
        projected_config = _v3_projection(config)
        raw["config_digest"] = projected_config.content_sha256
        parsed = _v3._idle_receipt_batch_v3(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=projected_config,
        )
        return DynamicIdleAdvanceReceiptBatchV4(
            schema=parsed.schema,
            config_digest=config.content_sha256,
            environment_generation=generation,
            cursor=parsed.cursor,
            next_cursor=parsed.next_cursor,
            stream_closed=parsed.stream_closed,
            active=parsed.active,
            receipts=parsed.receipts,
        )

    def _is_v4_bound(self) -> bool:
        return self._dynamic_binding is not None and isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV4
        )

    def _require_v4_binding(
        self, command: str
    ) -> tuple[DynamicTargetSemanticsConfigV4, int]:
        if not self._is_v4_bound():
            raise SimBridgeProtocolError(
                f"{command} requires a successful load_dynamic_v4"
            )
        generation, _, config = self._dynamic_binding
        return config, generation


def _validate_request_binding_v4(
    request: Mapping[str, Any], config: DynamicTargetSemanticsConfigV4
) -> None:
    try:
        _v3._validate_request_horizon_v3(request, _v3_projection(config))
    except (TypeError, ValueError) as error:
        raise DynamicV4ConfigError(str(error)) from error
    encounter = request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise DynamicV4ConfigError("RaidSimRequest encounter must be an object")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or any(
        not isinstance(row, Mapping) for row in targets
    ):
        raise DynamicV4ConfigError(
            "RaidSimRequest encounter.targets must be an array of objects"
        )
    if len(targets) != len(config.target_health):
        raise DynamicV4ConfigError(
            "target_health count differs from RaidSimRequest target count"
        )
    for index, (target, checkpoint) in enumerate(
        zip(targets, config.target_health)
    ):
        stats = target.get("stats")
        if not isinstance(stats, list) or len(stats) <= 34:
            raise DynamicV4ConfigError(
                f"RaidSimRequest target {index} has no explicit stats[34] health"
            )
        request_health = stats[34]
        if isinstance(request_health, bool) or not isinstance(
            request_health, (int, float)
        ):
            raise DynamicV4ConfigError(
                f"RaidSimRequest target {index} stats[34] must be numeric"
            )
        request_health = float(request_health)
        if not math.isfinite(request_health) or not _v2._same_float(
            request_health, checkpoint.maximum_health
        ):
            raise DynamicV4ConfigError(
                f"RaidSimRequest target {index} stats[34] must exactly equal "
                "maximum_health"
            )


def _dynamic_load_receipt_v4(
    value: Mapping[str, Any], config: DynamicTargetSemanticsConfigV4
) -> DynamicLoadReceiptV4:
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
        "dynamic-v4 load receipt",
        protocol=True,
    )
    receipt = DynamicLoadReceiptV4(
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
        retarget_mode=_v2._text_field(raw, "retarget_mode", protocol=True),
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
        DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        len(config.attackability_events),
        len(config.effective_armor_events),
        config.same_timestamp_order,
        config.retarget_mode,
        config.idle_advance_mode,
        config.idle_advance_horizon_ms,
        _v3.DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
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
            "dynamic-v4 load receipt differs from the requested config"
        )
    return receipt


def _validate_dynamic_state_binding_v4(
    state: Mapping[str, Any],
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV4,
) -> ParsedDynamicStateV4:
    if not isinstance(state, Mapping):
        raise SimBridgeProtocolError("simulator state must be an object")
    team_response, wake_ready = _parse_team_response_state_v4(
        state,
        generation=generation,
        horizon_ms=config.idle_advance_horizon_ms,
    )
    responsive_count = (
        0
        if team_response is None
        else team_response.damage_applications_processed
    )
    team = _parse_team_state_v4(
        state.get("dynamic_team_background"),
        generation=generation,
        config=config,
        responsive_damage_applications=responsive_count,
    )
    semantics = _parse_semantics_state_v4(
        state.get("dynamic_target_semantics"),
        generation=generation,
        config=config,
    )
    if len(team.targets) != len(semantics.targets):
        raise SimBridgeProtocolError(
            "dynamic-v4 state target blocks differ in length"
        )
    for lifecycle, target in zip(team.targets, semantics.targets):
        if (
            lifecycle.target_index != target.target_index
            or not _v2._same_float(
                lifecycle.maximum_health, target.maximum_health
            )
            or not _v2._same_float(
                lifecycle.current_health, target.current_health
            )
            or lifecycle.dead != target.dead
            or lifecycle.death_time_ms != target.death_time_ms
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 lifecycle and semantics target rows disagree"
            )
    total_count = _v2._nonnegative_int_field(state, "total_target_count")
    active_count = _v2._nonnegative_int_field(state, "num_targets")
    if total_count != len(config.target_health):
        raise SimBridgeProtocolError(
            "state total_target_count differs from dynamic-v4 config"
        )
    expected_active = sum(
        1 for target in semantics.targets if target.attackable and not target.dead
    )
    if active_count != expected_active:
        raise SimBridgeProtocolError(
            "state num_targets differs from live attackable target rows"
        )
    target_index = _v2._nonnegative_int_field(state, "target_index")
    if target_index >= total_count:
        raise SimBridgeProtocolError("state target_index is out of range")
    selected = semantics.targets[target_index]
    if state.get("target_health_known") is not True:
        raise SimBridgeProtocolError(
            "dynamic-v4 state must expose target_health_known=true"
        )
    current = _v2._nonnegative_number_field(state, "target_health")
    maximum = _v2._positive_number_field(state, "target_health_max")
    percent = _v2._nonnegative_number_field(
        state, "target_health_percent"
    )
    if (
        not _v2._same_float(current, selected.current_health)
        or not _v2._same_float(maximum, selected.maximum_health)
        or not _v2._numbers_conserve(
            percent, selected.current_health / selected.maximum_health * 100.0
        )
        or not _v2._same_float(
            _v2._nonnegative_number_field(state, "target_armor"),
            selected.effective_armor,
        )
    ):
        raise SimBridgeProtocolError(
            "selected target state differs from dynamic-v4 target semantics"
        )
    if not _v2._numbers_conserve(
        _v2._positive_number_field(state, "encounter_health_target"),
        sum(row.current_health for row in config.target_health),
    ):
        raise SimBridgeProtocolError(
            "encounter health target differs from the v4 current-HP checkpoint"
        )

    idle_raw = state.get("dynamic_idle_advance")
    if not isinstance(idle_raw, Mapping):
        raise SimBridgeProtocolError("state lacks dynamic_idle_advance")
    idle = _parse_idle_state_v4(
        idle_raw,
        state=state,
        generation=generation,
        config=config,
        wake_ready=wake_ready,
    )
    return ParsedDynamicStateV4(
        team=team,
        target_semantics=semantics,
        idle_advance=idle,
        team_response=team_response,
        wake_ready=wake_ready,
    )


def _parse_team_response_state_v4(
    state: Mapping[str, Any],
    *,
    generation: int,
    horizon_ms: int,
) -> tuple[DynamicTeamResponseStateV1 | None, DynamicTeamWakeReadyV1 | None]:
    """Validate the optional responsive-team lifecycle and exact wake."""

    value = state.get("dynamic_team_response")
    wake_value = state.get("wake_ready")
    if value is None:
        if wake_value is not None:
            raise SimBridgeProtocolError(
                "wake_ready requires dynamic_team_response state"
            )
        return None, None
    raw = _v2._mapping_with_optional(
        value,
        {
            "schema",
            "model_content_sha256",
            "environment_generation",
            "receipts_processed",
            "damage_applications_processed",
            "stream_closed",
        },
        {"pending_wake_id", "pending_wake_time_ms"},
        "dynamic-v4 team response state",
    )
    pending_fields = {"pending_wake_id", "pending_wake_time_ms"}.intersection(
        raw
    )
    if pending_fields and pending_fields != {
        "pending_wake_id",
        "pending_wake_time_ms",
    }:
        raise SimBridgeProtocolError(
            "dynamic-v4 team response pending wake fields are incomplete"
        )
    response = DynamicTeamResponseStateV1(
        schema=_v2._text_field(raw, "schema", protocol=True),
        model_content_sha256=_v2._sha256_field(
            raw, "model_content_sha256", protocol=True
        ),
        environment_generation=_v2._positive_int_field(
            raw, "environment_generation"
        ),
        receipts_processed=_v2._nonnegative_int_field(
            raw, "receipts_processed"
        ),
        damage_applications_processed=_v2._nonnegative_int_field(
            raw, "damage_applications_processed"
        ),
        stream_closed=_v2._strict_bool_field(raw, "stream_closed"),
        pending_wake_id=(
            _v2._text_field(raw, "pending_wake_id", protocol=True)
            if "pending_wake_id" in raw
            else None
        ),
        pending_wake_time_ms=(
            _v2._nonnegative_int_field(raw, "pending_wake_time_ms")
            if "pending_wake_time_ms" in raw
            else None
        ),
    )
    finished = _v2._strict_bool_field(state, "finished")
    now = _v2._nonnegative_int_field(state, "time_ms")
    if (
        response.schema != DYNAMIC_TEAM_RESPONSE_STATE_SCHEMA_V1
        or response.environment_generation != generation
        or response.damage_applications_processed
        > response.receipts_processed
        or response.stream_closed != finished
        or response.pending_wake_time_ms is not None
        and (
            response.stream_closed
            or response.pending_wake_time_ms < now
            or response.pending_wake_time_ms >= horizon_ms
        )
    ):
        raise SimBridgeProtocolError(
            "dynamic-v4 team response state violates its lifecycle binding"
        )

    wake: DynamicTeamWakeReadyV1 | None = None
    if wake_value is not None:
        wake_raw = _v2._exact_mapping(
            wake_value,
            {"schema", "model_content_sha256", "wake_id", "time_ms"},
            "dynamic-v4 wake_ready",
            protocol=True,
        )
        wake = DynamicTeamWakeReadyV1(
            schema=_v2._text_field(wake_raw, "schema", protocol=True),
            model_content_sha256=_v2._sha256_field(
                wake_raw, "model_content_sha256", protocol=True
            ),
            wake_id=_v2._text_field(wake_raw, "wake_id", protocol=True),
            time_ms=_v2._nonnegative_int_field(wake_raw, "time_ms"),
        )
        if (
            wake.schema != DYNAMIC_TEAM_WAKE_SCHEMA_V1
            or wake.model_content_sha256 != response.model_content_sha256
            or wake.time_ms != now
            or wake.time_ms >= horizon_ms
            or response.stream_closed
            or response.pending_wake_id is not None
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 wake_ready violates its model/time/lifecycle binding"
            )
    return response, wake


def _parse_idle_state_v4(
    value: Any,
    *,
    state: Mapping[str, Any],
    generation: int,
    config: DynamicTargetSemanticsConfigV4,
    wake_ready: DynamicTeamWakeReadyV1 | None,
) -> _v3.DynamicIdleAdvanceStateV3:
    """Validate v3 idle receipts with the v4 responsive-wake boundary."""

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
        "dynamic-v4 idle state",
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
            "dynamic-v4 idle active fields disagree with active flag"
        )
    result = _v3.DynamicIdleAdvanceStateV3(
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
    violations = []
    if result.schema != _v3.DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3:
        violations.append("schema")
    if result.config_digest != config.content_sha256:
        violations.append("config_digest")
    if result.environment_generation != generation:
        violations.append("environment_generation")
    if result.mode != config.idle_advance_mode:
        violations.append("mode")
    if result.horizon_ms != config.idle_advance_horizon_ms:
        violations.append("horizon_ms")
    if result.stream_closed != finished:
        violations.append(f"stream_closed={result.stream_closed},finished={finished}")
    if time_ms > result.horizon_ms:
        violations.append(f"time_ms={time_ms}>horizon_ms={result.horizon_ms}")
    if result.total_auto_advanced_ms > time_ms:
        violations.append("total_auto_advanced_ms>time_ms")
    if result.active and (
        needs_input and wake_ready is None
        or num_targets != 0
        or result.planned_wake_source not in _v3.DYNAMIC_IDLE_WAKE_SOURCES_V3
        or result.active_start_time_ms is None
        or result.planned_wake_time_ms is None
        or result.active_start_time_ms > time_ms
        or result.planned_wake_time_ms < time_ms
        or result.planned_wake_time_ms > result.horizon_ms
    ):
        violations.append(
            f"active_wake(finished={finished},needs_input={needs_input},"
            f"num_targets={num_targets},wake_ready={wake_ready is not None},"
            f"source={result.planned_wake_source},time_ms={time_ms},"
            f"start_ms={result.active_start_time_ms},wake_ms={result.planned_wake_time_ms})"
        )
    if needs_input and num_targets == 0 and wake_ready is None:
        violations.append("empty_target_input_without_ready_wake")
    if violations:
        raise SimBridgeProtocolError(
            "dynamic-v4 idle state violates its lifecycle/horizon binding: "
            + "; ".join(violations)
        )
    return result


def _parse_team_state_v4(
    value: Any,
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV4,
    responsive_damage_applications: int,
) -> DynamicTeamLifecycleStateV4:
    raw = _v2._exact_mapping(
        value,
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
            "damage_applications_total",
            "targets",
        },
        "dynamic-v4 team lifecycle state",
        protocol=True,
    )
    target_rows = _v2._object_array(
        raw["targets"], "dynamic-v4 lifecycle targets", protocol=True
    )
    if len(target_rows) != len(config.target_health):
        raise SimBridgeProtocolError(
            "dynamic-v4 lifecycle target count differs from config"
        )
    targets: list[DynamicLifecycleTargetStateV4] = []
    for index, value_row in enumerate(target_rows):
        row = _v2._mapping_with_optional(
            value_row,
            {
                "target_index",
                "initial_health",
                "current_health",
                "dead",
                "simulated_damage_applied",
                "background_damage_applied",
            },
            {"death_time_ms"},
            f"dynamic-v4 lifecycle target[{index}]",
        )
        checkpoint = config.target_health[index]
        initial = _v2._positive_number_field(row, "initial_health")
        current = _v2._nonnegative_number_field(row, "current_health")
        simulated = _v2._nonnegative_number_field(
            row, "simulated_damage_applied"
        )
        background = _v2._nonnegative_number_field(
            row, "background_damage_applied"
        )
        target = DynamicLifecycleTargetStateV4(
            target_index=_v2._nonnegative_int_field(row, "target_index"),
            maximum_health=checkpoint.maximum_health,
            initial_health=initial,
            current_health=current,
            missing_health_at_checkpoint=(
                checkpoint.maximum_health - checkpoint.current_health
            ),
            dead=_v2._strict_bool_field(row, "dead"),
            simulated_damage_applied=simulated,
            background_damage_applied=background,
            death_time_ms=(
                _v2._nonnegative_int_field(row, "death_time_ms")
                if "death_time_ms" in row
                else None
            ),
        )
        if (
            target.target_index != index
            or not _v2._same_float(initial, checkpoint.current_health)
            or current > initial
            or target.dead != (current <= 0)
            or target.dead != (target.death_time_ms is not None)
            or not _v2._numbers_conserve(
                initial, current + simulated + background
            )
            or not _v2._numbers_conserve(
                target.maximum_health,
                target.missing_health_at_checkpoint
                + current
                + simulated
                + background,
            )
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 target violates max/current checkpoint conservation"
            )
        targets.append(target)

    result = DynamicTeamLifecycleStateV4(
        schema=_v2._text_field(raw, "schema", protocol=True),
        config_digest=_v2._sha256_field(
            raw, "config_digest", protocol=True
        ),
        environment_generation=_v2._positive_int_field(
            raw, "environment_generation"
        ),
        same_timestamp_order=_v2._text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        retarget_mode=_v2._text_field(raw, "retarget_mode", protocol=True),
        retarget_required=_v2._strict_bool_field(raw, "retarget_required"),
        simulated_damage_applied=_v2._nonnegative_number_field(
            raw, "simulated_damage_applied"
        ),
        background_damage_applied=_v2._nonnegative_number_field(
            raw, "background_damage_applied"
        ),
        combined_damage_applied=_v2._nonnegative_number_field(
            raw, "combined_damage_applied"
        ),
        background_events_processed=_v2._nonnegative_int_field(
            raw, "background_events_processed"
        ),
        background_events_total=_v2._nonnegative_int_field(
            raw, "background_events_total"
        ),
        background_events_canceled=_v2._nonnegative_int_field(
            raw, "background_events_canceled"
        ),
        background_damage_applications_processed=(
            _v2._nonnegative_int_field(
                raw, "background_damage_applications_processed"
            )
        ),
        candidate_events_processed=_v2._nonnegative_int_field(
            raw, "candidate_events_processed"
        ),
        candidate_events_canceled=_v2._nonnegative_int_field(
            raw, "candidate_events_canceled"
        ),
        responsive_damage_applications_processed=(
            responsive_damage_applications
        ),
        damage_applications_total=_v2._nonnegative_int_field(
            raw, "damage_applications_total"
        ),
        targets=tuple(targets),
    )
    if (
        result.schema != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4
        or result.config_digest != config.content_sha256
        or result.environment_generation != generation
        or result.same_timestamp_order != config.same_timestamp_order
        or result.retarget_mode != config.retarget_mode
        or result.background_events_total
        != len(config.background_damage_events)
        or result.background_events_processed > result.background_events_total
        or result.background_events_canceled
        > result.background_events_processed
        or result.background_damage_applications_processed
        > result.background_events_processed
        or result.candidate_events_canceled
        > result.candidate_events_processed
        or result.damage_applications_total
        != result.background_damage_applications_processed
        + result.candidate_events_processed
        + responsive_damage_applications
    ):
        raise SimBridgeProtocolError(
            "dynamic-v4 team lifecycle violates config/counter binding"
        )
    simulated = sum(row.simulated_damage_applied for row in targets)
    background = sum(row.background_damage_applied for row in targets)
    if not (
        _v2._numbers_conserve(result.simulated_damage_applied, simulated)
        and _v2._numbers_conserve(result.background_damage_applied, background)
        and _v2._numbers_conserve(
            result.combined_damage_applied, simulated + background
        )
    ):
        raise SimBridgeProtocolError(
            "dynamic-v4 aggregate damage does not conserve"
        )
    return result


def _parse_semantics_state_v4(
    value: Any,
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV4,
) -> DynamicTargetSemanticsStateV4:
    raw = _v2._exact_mapping(
        value,
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
        },
        "dynamic-v4 target semantics state",
        protocol=True,
    )
    target_rows = _v2._object_array(
        raw["targets"], "dynamic-v4 semantics targets", protocol=True
    )
    if len(target_rows) != len(config.target_health):
        raise SimBridgeProtocolError(
            "dynamic-v4 semantics target count differs from config"
        )
    targets: list[DynamicTargetRuntimeStateV4] = []
    for index, value_row in enumerate(target_rows):
        row = _v2._mapping_with_optional(
            value_row,
            {
                "target_index",
                "attackable",
                "effective_armor",
                "maximum_health",
                "current_health",
                "dead",
            },
            {"death_time_ms"},
            f"dynamic-v4 semantics target[{index}]",
        )
        target = DynamicTargetRuntimeStateV4(
            target_index=_v2._nonnegative_int_field(row, "target_index"),
            attackable=_v2._strict_bool_field(row, "attackable"),
            effective_armor=_v2._nonnegative_number_field(
                row, "effective_armor"
            ),
            maximum_health=_v2._positive_number_field(
                row, "maximum_health"
            ),
            current_health=_v2._nonnegative_number_field(
                row, "current_health"
            ),
            dead=_v2._strict_bool_field(row, "dead"),
            death_time_ms=(
                _v2._nonnegative_int_field(row, "death_time_ms")
                if "death_time_ms" in row
                else None
            ),
        )
        checkpoint = config.target_health[index]
        if (
            target.target_index != index
            or not _v2._same_float(
                target.maximum_health, checkpoint.maximum_health
            )
            or target.current_health > target.maximum_health
            or target.dead != (target.current_health <= 0)
            or target.dead != (target.death_time_ms is not None)
            or target.dead
            and target.attackable
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 semantics target violates its maximum/lifecycle binding"
            )
        targets.append(target)
    result = DynamicTargetSemanticsStateV4(
        schema=_v2._text_field(raw, "schema", protocol=True),
        config_digest=_v2._sha256_field(
            raw, "config_digest", protocol=True
        ),
        environment_generation=_v2._positive_int_field(
            raw, "environment_generation"
        ),
        same_timestamp_order=_v2._text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        attackability_events_processed=_v2._nonnegative_int_field(
            raw, "attackability_events_processed"
        ),
        attackability_events_total=_v2._nonnegative_int_field(
            raw, "attackability_events_total"
        ),
        effective_armor_events_processed=_v2._nonnegative_int_field(
            raw, "effective_armor_events_processed"
        ),
        effective_armor_events_total=_v2._nonnegative_int_field(
            raw, "effective_armor_events_total"
        ),
        targets=tuple(targets),
    )
    if (
        result.schema != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4
        or result.config_digest != config.content_sha256
        or result.environment_generation != generation
        or result.same_timestamp_order != config.same_timestamp_order
        or result.attackability_events_total
        != len(config.attackability_events)
        or result.effective_armor_events_total
        != len(config.effective_armor_events)
        or result.attackability_events_processed
        > result.attackability_events_total
        or result.effective_armor_events_processed
        > result.effective_armor_events_total
    ):
        raise SimBridgeProtocolError(
            "dynamic-v4 target semantics violates its config binding"
        )
    return result


def _background_batch_v4(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV4,
) -> DynamicDamageReceiptBatchV4:
    """Parse v4 fixed-background receipts, including terminal no-op rows."""

    raw = _v2._exact_mapping(
        value,
        {
            "schema",
            "config_digest",
            "environment_generation",
            "cursor",
            "next_cursor",
            "schedule_complete",
            "receipts",
        },
        "dynamic-v4 background receipt batch",
        protocol=True,
    )
    rows = _v2._object_array(
        raw["receipts"], "dynamic-v4 background receipts", protocol=True
    )
    cursor = _v2._nonnegative_int_field(raw, "cursor")
    next_cursor = _v2._nonnegative_int_field(raw, "next_cursor")
    schedule_complete = _v2._strict_bool_field(raw, "schedule_complete")
    if (
        _v2._text_field(raw, "schema", protocol=True)
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4
        or _v2._sha256_field(raw, "config_digest", protocol=True)
        != config.content_sha256
        or _v2._positive_int_field(raw, "environment_generation")
        != generation
        or cursor != requested_cursor
        or next_cursor != cursor + len(rows)
        or next_cursor > len(config.background_damage_events)
        or schedule_complete
        != (next_cursor == len(config.background_damage_events))
    ):
        raise SimBridgeProtocolError(
            "dynamic-v4 background receipt batch violates its cursor/content binding"
        )

    receipts: list[DynamicDamageReceiptV4] = []
    for offset, value_row in enumerate(rows):
        event = config.background_damage_events[cursor + offset]
        row = _v2._mapping_with_optional(
            value_row,
            {
                "schedule_index",
                "event_id",
                "time_ms",
                "target_index",
                "requested_damage",
                "applied_damage",
                "overkill_damage",
                "killed",
                "status",
            },
            {"damage_ordinal", "retargeted_to"},
            "dynamic-v4 background receipt",
        )
        receipt = DynamicDamageReceiptV4(
            damage_ordinal=(
                _v2._positive_int_field(row, "damage_ordinal")
                if "damage_ordinal" in row
                else None
            ),
            schedule_index=_v2._nonnegative_int_field(
                row, "schedule_index"
            ),
            event_id=_v2._text_field(row, "event_id", protocol=True),
            time_ms=_v2._nonnegative_int_field(row, "time_ms"),
            target_index=_v2._nonnegative_int_field(row, "target_index"),
            requested_damage=_v2._nonnegative_number_field(
                row, "requested_damage"
            ),
            applied_damage=_v2._nonnegative_number_field(
                row, "applied_damage"
            ),
            overkill_damage=_v2._nonnegative_number_field(
                row, "overkill_damage"
            ),
            killed=_v2._strict_bool_field(row, "killed"),
            status=_v2._text_field(row, "status", protocol=True),
            retargeted_to=(
                _v2._nonnegative_int_field(row, "retargeted_to")
                if "retargeted_to" in row
                else None
            ),
        )
        if (
            receipt.schedule_index != event.schedule_index
            or receipt.event_id != event.event_id
            or receipt.time_ms != event.time_ms
            or receipt.target_index != event.target_index
            or not _v2._same_float(receipt.requested_damage, event.damage)
            or receipt.status not in _v2.DYNAMIC_DAMAGE_STATUSES_V2
            or not _v2._numbers_conserve(
                receipt.requested_damage,
                receipt.applied_damage + receipt.overkill_damage,
            )
            or receipt.status != "APPLIED"
            and (receipt.applied_damage != 0 or receipt.killed)
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 background receipt violates its scheduled damage contract"
            )
        if receipt.retargeted_to is not None and (
            receipt.retargeted_to >= len(config.target_health)
            or not receipt.killed
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 background receipt retarget is invalid"
            )
        if (
            receipt.damage_ordinal is None
            and receipt.status != "CANCELED_TARGET_DEAD"
        ):
            raise SimBridgeProtocolError(
                "dynamic-v4 background receipt without a damage ordinal must "
                "be a terminal target-dead cancellation"
            )
        receipts.append(receipt)
    _strictly_increasing_ordinals_v4(receipts)
    return DynamicDamageReceiptBatchV4(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        schedule_complete=schedule_complete,
        receipts=tuple(receipts),
    )


def _strictly_increasing_ordinals_v4(
    rows: list[DynamicDamageReceiptV4],
) -> None:
    previous = 0
    terminal_without_application = False
    for row in rows:
        if row.damage_ordinal is None:
            terminal_without_application = True
            continue
        if terminal_without_application:
            raise SimBridgeProtocolError(
                "dynamic-v4 background receipts resume damage ordinals after "
                "a terminal cancellation"
            )
        if row.damage_ordinal <= previous:
            raise SimBridgeProtocolError(
                "dynamic-v4 background receipt damage ordinals are not increasing"
            )
        previous = row.damage_ordinal


def _project_envelope_v4_to_v2(
    value: Mapping[str, Any], config: DynamicTargetSemanticsConfigV4
) -> JSONMap:
    projected = dict(value)
    projected_config = _v2_projection(config)
    projected["schema"] = _v2.DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
    projected["config_digest"] = projected_config.content_sha256
    return projected


def _positive_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise DynamicV4ConfigError(f"{label} must be finite and positive")
    return parsed


__all__ = (
    "DYNAMIC_SAME_TIMESTAMP_ORDER_V4",
    "DYNAMIC_TEAM_RESPONSE_STATE_SCHEMA_V1",
    "DYNAMIC_TEAM_WAKE_SCHEMA_V1",
    "DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4",
    "DynamicArmorReceiptBatchV4",
    "DynamicAttackabilityReceiptBatchV4",
    "DynamicCandidateDamageReceiptBatchV4",
    "DynamicDamageReceiptV4",
    "DynamicDamageReceiptBatchV4",
    "DynamicIdleAdvanceReceiptBatchV4",
    "DynamicLifecycleTargetStateV4",
    "DynamicLoadReceiptV4",
    "DynamicLoadResultV4",
    "DynamicTargetHealthV4",
    "DynamicTargetRuntimeStateV4",
    "DynamicTargetSemanticsConfigV4",
    "DynamicTargetSemanticsStateV4",
    "DynamicTeamLifecycleStateV4",
    "DynamicTeamResponseStateV1",
    "DynamicTeamWakeReadyV1",
    "DynamicV4ConfigError",
    "ParsedDynamicStateV4",
    "SimulatorBridgeDynamicV4",
    "dynamic_target_semantics_config_from_wire_v4",
    "dynamic_target_semantics_digest_v4",
)
