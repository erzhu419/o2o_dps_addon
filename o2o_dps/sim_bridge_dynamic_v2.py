"""Strict client extension for ``o2obridge`` dynamic target semantics v2.

This module is deliberately version-isolated from :mod:`o2o_dps.sim_bridge`.
It reuses the proven synchronous process transport but adds only the typed
``load_dynamic_v2`` surface and its content/generation-bound receipts.  The
configuration is an executable simulator hypothesis; none of its health,
armor, or attackability values become historical truth by being executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import struct
from typing import Any, Mapping

from .sim_bridge import (
    ActionRef,
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
    SimBridgeProtocolError,
    SimulatorBridge,
)


JSONMap = dict[str, Any]

DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2 = "o2o_dynamic_target_semantics/v2"
DYNAMIC_SAME_TIMESTAMP_ORDER_V2 = (
    "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE"
)
DYNAMIC_RETARGET_MODES_V2 = frozenset(
    {"NEXT_ALIVE_CYCLIC", "REQUIRE_EXPLICIT"}
)
DYNAMIC_TRANSITION_STATUSES_V2 = frozenset(
    {
        "APPLIED",
        "NO_CHANGE",
        "CANCELED_TARGET_DEAD",
        "CANCELED_ENCOUNTER_FINISHED",
    }
)
DYNAMIC_DAMAGE_STATUSES_V2 = frozenset(
    {"APPLIED", "CANCELED_TARGET_DEAD", "CANCELED_TARGET_UNATTACKABLE"}
)
DYNAMIC_CANDIDATE_DAMAGE_STATUSES_V2 = frozenset(
    {
        "APPLIED",
        "NO_DAMAGE",
        "CANCELED_TARGET_DEAD",
        "CANCELED_TARGET_UNATTACKABLE",
    }
)
DYNAMIC_CANDIDATE_OUTCOMES_V2 = frozenset(
    {
        "HIT",
        "CRIT",
        "MISS",
        "DODGE",
        "PARRY",
        "BLOCK",
        "BLOCK_CRIT",
        "GLANCE",
        "CRUSH",
        "MIXED",
        "CANCELED",
        "EMPTY",
    }
)
DYNAMIC_CANDIDATE_RESOLUTION_PHASES_V2 = frozenset(
    {
        "APPLIED_AFTER_OUTCOME",
        "NO_DAMAGE_AFTER_OUTCOME",
        "TARGET_DEAD_BEFORE_OUTCOME",
        "TARGET_DIED_AFTER_OUTCOME",
        "TARGET_UNATTACKABLE_AFTER_OUTCOME",
    }
)
_MAX_DYNAMIC_TIME_MS_V2 = ((1 << 63) - 1) // 1_000_000
_HEX = frozenset("0123456789abcdef")


class DynamicV2ConfigError(ValueError):
    """A local dynamic-v2 configuration is malformed or unbound."""


@dataclass(frozen=True)
class DynamicAttackabilityEventV2:
    schedule_index: int
    time_ms: int
    target_index: int
    attackable: bool

    def __post_init__(self) -> None:
        _nonnegative_int(self.schedule_index, "schedule_index")
        time_ms = _nonnegative_int(self.time_ms, "time_ms")
        if time_ms > _MAX_DYNAMIC_TIME_MS_V2:
            raise DynamicV2ConfigError("time_ms exceeds the Go duration range")
        _nonnegative_int(self.target_index, "target_index")
        _strict_bool(self.attackable, "attackable")

    def to_wire(self) -> JSONMap:
        return {
            "schedule_index": self.schedule_index,
            "time_ms": self.time_ms,
            "target_index": self.target_index,
            "attackable": self.attackable,
        }


@dataclass(frozen=True)
class DynamicEffectiveArmorEventV2:
    schedule_index: int
    time_ms: int
    target_index: int
    effective_armor: float

    def __post_init__(self) -> None:
        _nonnegative_int(self.schedule_index, "schedule_index")
        time_ms = _nonnegative_int(self.time_ms, "time_ms")
        if time_ms > _MAX_DYNAMIC_TIME_MS_V2:
            raise DynamicV2ConfigError("time_ms exceeds the Go duration range")
        _nonnegative_int(self.target_index, "target_index")
        armor = _nonnegative_finite_number(
            self.effective_armor, "effective_armor"
        )
        object.__setattr__(self, "effective_armor", armor)

    def to_wire(self) -> JSONMap:
        return {
            "schedule_index": self.schedule_index,
            "time_ms": self.time_ms,
            "target_index": self.target_index,
            "effective_armor": self.effective_armor,
        }


@dataclass(frozen=True)
class DynamicTargetSemanticsConfigV2:
    """Immutable full dynamic target configuration for ``load_dynamic_v2``."""

    target_health: tuple[DynamicTargetHealthV1, ...]
    background_damage_events: tuple[BackgroundDamageEventV1, ...] = ()
    attackability_events: tuple[DynamicAttackabilityEventV2, ...] = ()
    effective_armor_events: tuple[DynamicEffectiveArmorEventV2, ...] = ()
    same_timestamp_order: str = DYNAMIC_SAME_TIMESTAMP_ORDER_V2
    retarget_mode: str = "NEXT_ALIVE_CYCLIC"
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target_health, tuple) or any(
            not isinstance(row, DynamicTargetHealthV1)
            for row in self.target_health
        ):
            raise TypeError(
                "target_health must be a tuple of DynamicTargetHealthV1"
            )
        if not self.target_health or tuple(
            row.target_index for row in self.target_health
        ) != tuple(range(len(self.target_health))):
            raise DynamicV2ConfigError(
                "target_health must cover target indexes 0..N-1 exactly"
            )
        _typed_tuple(
            self.background_damage_events,
            BackgroundDamageEventV1,
            "background_damage_events",
        )
        _typed_tuple(
            self.attackability_events,
            DynamicAttackabilityEventV2,
            "attackability_events",
        )
        _typed_tuple(
            self.effective_armor_events,
            DynamicEffectiveArmorEventV2,
            "effective_armor_events",
        )
        if self.same_timestamp_order != DYNAMIC_SAME_TIMESTAMP_ORDER_V2:
            raise DynamicV2ConfigError(
                "same_timestamp_order must be "
                f"{DYNAMIC_SAME_TIMESTAMP_ORDER_V2!r}"
            )
        if self.retarget_mode not in DYNAMIC_RETARGET_MODES_V2:
            raise DynamicV2ConfigError(
                f"unsupported retarget_mode {self.retarget_mode!r}"
            )
        _validate_background_events(self)
        _validate_transition_events(
            self.attackability_events,
            target_count=len(self.target_health),
            label="attackability_events",
        )
        _validate_transition_events(
            self.effective_armor_events,
            target_count=len(self.target_health),
            label="effective_armor_events",
        )
        object.__setattr__(
            self, "content_sha256", dynamic_target_semantics_digest_v2(self)
        )

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
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
            "same_timestamp_order": self.same_timestamp_order,
            "retarget_mode": self.retarget_mode,
        }


def dynamic_target_semantics_digest_v2(
    config: DynamicTargetSemanticsConfigV2,
) -> str:
    """Return the Go-compatible IEEE-754 canonical config digest."""

    if not isinstance(config, DynamicTargetSemanticsConfigV2):
        raise TypeError("config must be DynamicTargetSemanticsConfigV2")
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
        "retarget_mode": config.retarget_mode,
        "same_timestamp_order": config.same_timestamp_order,
        "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
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


def dynamic_target_semantics_config_from_wire_v2(
    value: Mapping[str, Any],
) -> DynamicTargetSemanticsConfigV2:
    """Strictly reconstruct a typed config and verify its content address."""

    raw = _exact_mapping(
        value,
        {
            "schema",
            "content_sha256",
            "target_health",
            "background_damage_events",
            "attackability_events",
            "effective_armor_events",
            "same_timestamp_order",
            "retarget_mode",
        },
        "dynamic-v2 config",
    )
    if raw["schema"] != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2:
        raise DynamicV2ConfigError("dynamic-v2 config schema is unsupported")
    target_rows = _object_array(raw["target_health"], "target_health")
    background_rows = _object_array(
        raw["background_damage_events"], "background_damage_events"
    )
    attack_rows = _object_array(
        raw["attackability_events"], "attackability_events"
    )
    armor_rows = _object_array(
        raw["effective_armor_events"], "effective_armor_events"
    )
    try:
        config = DynamicTargetSemanticsConfigV2(
            target_health=tuple(
                DynamicTargetHealthV1(
                    target_index=_strict_int_field(
                        _exact_mapping(
                            row,
                            {"target_index", "health"},
                            f"target_health[{index}]",
                        ),
                        "target_index",
                    ),
                    health=_number_field(row, "health"),
                )
                for index, row in enumerate(target_rows)
            ),
            background_damage_events=tuple(
                BackgroundDamageEventV1(
                    schedule_index=_strict_int_field(
                        _exact_mapping(
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
                    time_ms=_strict_int_field(row, "time_ms"),
                    target_index=_strict_int_field(row, "target_index"),
                    event_id=_text_field(row, "event_id"),
                    damage=_number_field(row, "damage"),
                )
                for index, row in enumerate(background_rows)
            ),
            attackability_events=tuple(
                DynamicAttackabilityEventV2(
                    schedule_index=_strict_int_field(
                        _exact_mapping(
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
                    time_ms=_strict_int_field(row, "time_ms"),
                    target_index=_strict_int_field(row, "target_index"),
                    attackable=_strict_bool_field(row, "attackable"),
                )
                for index, row in enumerate(attack_rows)
            ),
            effective_armor_events=tuple(
                DynamicEffectiveArmorEventV2(
                    schedule_index=_strict_int_field(
                        _exact_mapping(
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
                    time_ms=_strict_int_field(row, "time_ms"),
                    target_index=_strict_int_field(row, "target_index"),
                    effective_armor=_number_field(row, "effective_armor"),
                )
                for index, row in enumerate(armor_rows)
            ),
            same_timestamp_order=_text_field(raw, "same_timestamp_order"),
            retarget_mode=_text_field(raw, "retarget_mode"),
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, DynamicV2ConfigError):
            raise
        raise DynamicV2ConfigError(str(error)) from error
    digest = _sha256_field(raw, "content_sha256")
    if digest != config.content_sha256:
        raise DynamicV2ConfigError("dynamic-v2 config content SHA-256 mismatch")
    return config


@dataclass(frozen=True)
class DynamicLoadReceiptV2:
    schema: str
    config_digest: str
    environment_generation: int
    target_count: int
    background_event_count: int
    attackability_event_count: int
    effective_armor_event_count: int
    same_timestamp_order: str
    retarget_mode: str


@dataclass(frozen=True)
class DynamicLoadResultV2:
    receipt: DynamicLoadReceiptV2
    state: JSONMap


@dataclass(frozen=True)
class DynamicAttackabilityTransitionReceiptV2:
    schedule_index: int
    time_ms: int
    target_index: int
    requested_attackable: bool
    previous_attackable: bool
    resulting_attackable: bool
    status: str
    retargeted_to: int | None = None


@dataclass(frozen=True)
class DynamicArmorTransitionReceiptV2:
    schedule_index: int
    time_ms: int
    target_index: int
    requested_effective_armor: float
    previous_target_armor: float
    resulting_target_armor: float
    status: str


@dataclass(frozen=True)
class DynamicAttackabilityReceiptBatchV2:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicAttackabilityTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicArmorReceiptBatchV2:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicArmorTransitionReceiptV2, ...]


@dataclass(frozen=True)
class DynamicDamageReceiptV2:
    damage_ordinal: int
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
class DynamicDamageReceiptBatchV2:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicDamageReceiptV2, ...]


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptV2:
    damage_ordinal: int
    time_ms: int
    target_index: int
    requested_damage: float
    applied_damage: float
    overkill_damage: float
    killed: bool
    status: str
    action: ActionRef
    outcome: str
    execution_id: int
    execution_index: int
    landed_execution_index: int
    resolution_phase: str
    outcome_computed: bool
    random_stream_rewound: bool
    attempt_id: str | None = None
    retargeted_to: int | None = None


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptBatchV2:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    receipts: tuple[DynamicCandidateDamageReceiptV2, ...]


@dataclass(frozen=True)
class DynamicLifecycleTargetStateV2:
    target_index: int
    initial_health: float
    current_health: float
    dead: bool
    simulated_damage_applied: float
    background_damage_applied: float
    death_time_ms: int | None = None


@dataclass(frozen=True)
class DynamicTeamLifecycleStateV2:
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
class DynamicTargetRuntimeStateV2:
    target_index: int
    attackable: bool
    effective_armor: float
    current_health: float
    dead: bool
    death_time_ms: int | None = None


@dataclass(frozen=True)
class DynamicTargetSemanticsStateV2:
    schema: str
    config_digest: str
    environment_generation: int
    same_timestamp_order: str
    attackability_events_processed: int
    attackability_events_total: int
    effective_armor_events_processed: int
    effective_armor_events_total: int
    targets: tuple[DynamicTargetRuntimeStateV2, ...]


class SimulatorBridgeDynamicV2(SimulatorBridge):
    """Persistent bridge with strict, typed dynamic-v2 bindings."""

    def _request(self, command: str, **payload: Any) -> JSONMap:
        response = super()._request(command, **payload)
        _strict_json_object_v2(response, f"{command} response")
        return response

    def load_dynamic_v2(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV2,
    ) -> DynamicLoadResultV2:
        if not isinstance(request, Mapping):
            raise TypeError("RaidSimRequest must be a mapping")
        if not isinstance(config, DynamicTargetSemanticsConfigV2):
            raise TypeError("config must be DynamicTargetSemanticsConfigV2")
        self._dynamic_binding = None
        request_copy = _strict_json_object_v2(request, "RaidSimRequest")
        response = self._request(
            "load_dynamic_v2",
            request=request_copy,
            seed=_signed_int64(seed, "seed"),
            dynamic=config.to_wire(),
        )
        raw_receipt = response.get("dynamic_load")
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError(
                "load_dynamic_v2 response is missing dynamic_load receipt"
            )
        receipt = _dynamic_load_receipt_v2(raw_receipt, config)
        response_generation = _positive_int_field(
            response, "environment_generation"
        )
        if response_generation != receipt.environment_generation:
            raise SimBridgeProtocolError(
                "load_dynamic_v2 response generation differs from its receipt"
            )
        state = _state_field(response, "load_dynamic_v2")
        _validate_dynamic_state_binding_v2(
            state, generation=receipt.environment_generation, config=config
        )
        self._dynamic_binding = (
            receipt.environment_generation,
            receipt.config_digest,
            config,
        )
        return DynamicLoadResultV2(receipt=receipt, state=state)

    def _validate_bound_state(self, state: Mapping[str, Any]) -> None:
        if self._dynamic_binding is None:
            return
        generation, _, config = self._dynamic_binding
        if isinstance(config, DynamicTargetSemanticsConfigV2):
            _validate_dynamic_state_binding_v2(
                state, generation=generation, config=config
            )
            return
        super()._validate_bound_state(state)

    def dynamic_attackability_receipts(
        self, *, cursor: int = 0
    ) -> DynamicAttackabilityReceiptBatchV2:
        config, generation = self._require_v2_binding(
            "dynamic_attackability_receipts"
        )
        normalized = _nonnegative_int(cursor, "cursor")
        response = self._request(
            "dynamic_attackability_receipts", cursor=normalized
        )
        raw = response.get("dynamic_attackability_receipts")
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_attackability_receipts response lacks its batch"
            )
        return _attackability_batch_v2(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )

    def dynamic_armor_receipts(
        self, *, cursor: int = 0
    ) -> DynamicArmorReceiptBatchV2:
        config, generation = self._require_v2_binding("dynamic_armor_receipts")
        normalized = _nonnegative_int(cursor, "cursor")
        response = self._request("dynamic_armor_receipts", cursor=normalized)
        raw = response.get("dynamic_armor_receipts")
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_armor_receipts response lacks its batch"
            )
        return _armor_batch_v2(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )

    def dynamic_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicDamageReceiptBatchV2 | Any:
        if self._dynamic_binding is None or not isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV2
        ):
            return super().dynamic_damage_receipts(cursor=cursor)
        config, generation = self._require_v2_binding(
            "dynamic_damage_receipts"
        )
        normalized = _nonnegative_int(cursor, "cursor")
        response = self._request("dynamic_damage_receipts", cursor=normalized)
        raw = response.get("dynamic_damage_receipts")
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_damage_receipts response lacks its batch"
            )
        return _background_batch_v2(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )

    def dynamic_candidate_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicCandidateDamageReceiptBatchV2 | Any:
        if self._dynamic_binding is None or not isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV2
        ):
            return super().dynamic_candidate_damage_receipts(cursor=cursor)
        config, generation = self._require_v2_binding(
            "dynamic_candidate_damage_receipts"
        )
        normalized = _nonnegative_int(cursor, "cursor")
        response = self._request(
            "dynamic_candidate_damage_receipts", cursor=normalized
        )
        raw = response.get("dynamic_candidate_damage_receipts")
        if not isinstance(raw, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_candidate_damage_receipts response lacks its batch"
            )
        return _candidate_batch_v2(
            raw,
            requested_cursor=normalized,
            generation=generation,
            config=config,
        )

    def parsed_dynamic_state(
        self, state: Mapping[str, Any] | None = None
    ) -> tuple[DynamicTeamLifecycleStateV2, DynamicTargetSemanticsStateV2]:
        """Return both typed live blocks after full binding validation."""

        config, generation = self._require_v2_binding("parsed_dynamic_state")
        current = self.state() if state is None else dict(state)
        return _validate_dynamic_state_binding_v2(
            current, generation=generation, config=config
        )

    def _require_v2_binding(
        self, command: str
    ) -> tuple[DynamicTargetSemanticsConfigV2, int]:
        if self._dynamic_binding is None or not isinstance(
            self._dynamic_binding[2], DynamicTargetSemanticsConfigV2
        ):
            raise SimBridgeProtocolError(
                f"{command} requires a successful load_dynamic_v2"
            )
        generation, _, config = self._dynamic_binding
        return config, generation


def _dynamic_load_receipt_v2(
    value: Mapping[str, Any], config: DynamicTargetSemanticsConfigV2
) -> DynamicLoadReceiptV2:
    raw = _exact_mapping(
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
        },
        "dynamic-v2 load receipt",
        protocol=True,
    )
    receipt = DynamicLoadReceiptV2(
        schema=_text_field(raw, "schema", protocol=True),
        config_digest=_sha256_field(raw, "config_digest", protocol=True),
        environment_generation=_positive_int_field(
            raw, "environment_generation"
        ),
        target_count=_nonnegative_int_field(raw, "target_count"),
        background_event_count=_nonnegative_int_field(
            raw, "background_event_count"
        ),
        attackability_event_count=_nonnegative_int_field(
            raw, "attackability_event_count"
        ),
        effective_armor_event_count=_nonnegative_int_field(
            raw, "effective_armor_event_count"
        ),
        same_timestamp_order=_text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        retarget_mode=_text_field(raw, "retarget_mode", protocol=True),
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
    )
    expected = (
        DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        len(config.attackability_events),
        len(config.effective_armor_events),
        config.same_timestamp_order,
        config.retarget_mode,
    )
    if observed != expected:
        raise SimBridgeProtocolError(
            "dynamic-v2 load receipt differs from the requested config"
        )
    return receipt


def _validate_dynamic_state_binding_v2(
    state: Mapping[str, Any],
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> tuple[DynamicTeamLifecycleStateV2, DynamicTargetSemanticsStateV2]:
    if not isinstance(state, Mapping):
        raise SimBridgeProtocolError("simulator state must be an object")
    team = _parse_team_state_v2(
        state.get("dynamic_team_background"), generation=generation, config=config
    )
    semantics = _parse_semantics_state_v2(
        state.get("dynamic_target_semantics"),
        generation=generation,
        config=config,
    )
    if len(team.targets) != len(semantics.targets):
        raise SimBridgeProtocolError("dynamic state target blocks differ in length")
    for lifecycle, target in zip(team.targets, semantics.targets):
        if (
            lifecycle.target_index != target.target_index
            or not _same_float(lifecycle.current_health, target.current_health)
            or lifecycle.dead != target.dead
            or lifecycle.death_time_ms != target.death_time_ms
        ):
            raise SimBridgeProtocolError(
                "dynamic state target lifecycle and semantics rows disagree"
            )
    total_count = _nonnegative_int_field(state, "total_target_count")
    active_count = _nonnegative_int_field(state, "num_targets")
    if total_count != len(config.target_health):
        raise SimBridgeProtocolError(
            "state total_target_count differs from dynamic-v2 config"
        )
    expected_active = sum(
        1 for target in semantics.targets if target.attackable and not target.dead
    )
    if active_count != expected_active:
        raise SimBridgeProtocolError(
            "state num_targets differs from live attackable target rows"
        )
    target_index = _nonnegative_int_field(state, "target_index")
    if target_index >= total_count:
        raise SimBridgeProtocolError("state target_index is out of range")
    current = semantics.targets[target_index]
    if state.get("target_health_known") is not True:
        raise SimBridgeProtocolError(
            "dynamic-v2 state must expose target_health_known=true"
        )
    if not _same_float(
        _nonnegative_number_field(state, "target_health"), current.current_health
    ):
        raise SimBridgeProtocolError(
            "state target_health differs from selected dynamic target"
        )
    if not _same_float(
        _nonnegative_number_field(state, "target_health_max"),
        team.targets[target_index].initial_health,
    ):
        raise SimBridgeProtocolError(
            "state target_health_max differs from selected dynamic target"
        )
    if not _same_float(
        _nonnegative_number_field(state, "target_armor"), current.effective_armor
    ):
        raise SimBridgeProtocolError(
            "state target_armor differs from selected dynamic target semantics"
        )
    return team, semantics


def _parse_team_state_v2(
    value: Any,
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicTeamLifecycleStateV2:
    raw = _exact_mapping(
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
            "candidate_events_processed",
            "candidate_events_canceled",
            "damage_applications_total",
            "targets",
        },
        "dynamic_team_background state",
        protocol=True,
    )
    target_rows = _object_array(
        raw["targets"], "dynamic_team_background.targets", protocol=True
    )
    targets: list[DynamicLifecycleTargetStateV2] = []
    for index, value_row in enumerate(target_rows):
        required = {
            "target_index",
            "initial_health",
            "current_health",
            "dead",
            "simulated_damage_applied",
            "background_damage_applied",
        }
        row = _mapping_with_optional(
            value_row,
            required,
            {"death_time_ms"},
            f"dynamic lifecycle target[{index}]",
        )
        target = DynamicLifecycleTargetStateV2(
            target_index=_nonnegative_int_field(row, "target_index"),
            initial_health=_positive_number_field(row, "initial_health"),
            current_health=_nonnegative_number_field(row, "current_health"),
            dead=_strict_bool_field(row, "dead"),
            simulated_damage_applied=_nonnegative_number_field(
                row, "simulated_damage_applied"
            ),
            background_damage_applied=_nonnegative_number_field(
                row, "background_damage_applied"
            ),
            death_time_ms=(
                _nonnegative_int_field(row, "death_time_ms")
                if "death_time_ms" in row
                else None
            ),
        )
        if target.target_index != index:
            raise SimBridgeProtocolError(
                "dynamic lifecycle target indexes are not contiguous"
            )
        if not _same_float(
            target.initial_health, config.target_health[index].health
        ):
            raise SimBridgeProtocolError(
                "dynamic lifecycle initial health differs from config"
            )
        if target.dead != (target.current_health <= 0):
            raise SimBridgeProtocolError(
                "dynamic lifecycle dead flag disagrees with current health"
            )
        if target.dead != (target.death_time_ms is not None):
            raise SimBridgeProtocolError(
                "dynamic lifecycle death time presence disagrees with dead flag"
            )
        if not _numbers_conserve(
            target.initial_health,
            target.current_health
            + target.simulated_damage_applied
            + target.background_damage_applied,
        ):
            raise SimBridgeProtocolError(
                "dynamic lifecycle target damage does not conserve health"
            )
        targets.append(target)
    result = DynamicTeamLifecycleStateV2(
        schema=_text_field(raw, "schema", protocol=True),
        config_digest=_sha256_field(raw, "config_digest", protocol=True),
        environment_generation=_positive_int_field(
            raw, "environment_generation"
        ),
        same_timestamp_order=_text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        retarget_mode=_text_field(raw, "retarget_mode", protocol=True),
        retarget_required=_strict_bool_field(raw, "retarget_required"),
        simulated_damage_applied=_nonnegative_number_field(
            raw, "simulated_damage_applied"
        ),
        background_damage_applied=_nonnegative_number_field(
            raw, "background_damage_applied"
        ),
        combined_damage_applied=_nonnegative_number_field(
            raw, "combined_damage_applied"
        ),
        background_events_processed=_nonnegative_int_field(
            raw, "background_events_processed"
        ),
        background_events_total=_nonnegative_int_field(
            raw, "background_events_total"
        ),
        background_events_canceled=_nonnegative_int_field(
            raw, "background_events_canceled"
        ),
        candidate_events_processed=_nonnegative_int_field(
            raw, "candidate_events_processed"
        ),
        candidate_events_canceled=_nonnegative_int_field(
            raw, "candidate_events_canceled"
        ),
        damage_applications_total=_nonnegative_int_field(
            raw, "damage_applications_total"
        ),
        targets=tuple(targets),
    )
    if (
        result.schema != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or result.config_digest != config.content_sha256
        or result.environment_generation != generation
        or result.same_timestamp_order != config.same_timestamp_order
        or result.retarget_mode != config.retarget_mode
        or result.background_events_total != len(config.background_damage_events)
        or result.background_events_processed > result.background_events_total
        or result.background_events_canceled > result.background_events_processed
        or result.candidate_events_canceled > result.candidate_events_processed
        # The simulator writes cancellation receipts for pending background
        # events when the last target dies. Those events count as processed,
        # but never enter ApplyTargetDamage and have no damage ordinal.
        or result.damage_applications_total
        > result.background_events_processed + result.candidate_events_processed
        or (
            any(not target.dead for target in targets)
            and result.damage_applications_total
            != result.background_events_processed + result.candidate_events_processed
        )
        or result.damage_applications_total
        < result.background_events_processed
        - result.background_events_canceled
        + result.candidate_events_processed
    ):
        raise SimBridgeProtocolError(
            "dynamic team lifecycle state violates its config/counter binding"
        )
    simulated = sum(row.simulated_damage_applied for row in targets)
    background = sum(row.background_damage_applied for row in targets)
    if not (
        _numbers_conserve(result.simulated_damage_applied, simulated)
        and _numbers_conserve(result.background_damage_applied, background)
        and _numbers_conserve(
            result.combined_damage_applied, simulated + background
        )
    ):
        raise SimBridgeProtocolError(
            "dynamic team lifecycle aggregate damage does not conserve"
        )
    return result


def _parse_semantics_state_v2(
    value: Any,
    *,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicTargetSemanticsStateV2:
    raw = _exact_mapping(
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
        "dynamic_target_semantics state",
        protocol=True,
    )
    target_rows = _object_array(
        raw["targets"], "dynamic_target_semantics.targets", protocol=True
    )
    targets: list[DynamicTargetRuntimeStateV2] = []
    for index, value_row in enumerate(target_rows):
        row = _mapping_with_optional(
            value_row,
            {
                "target_index",
                "attackable",
                "effective_armor",
                "current_health",
                "dead",
            },
            {"death_time_ms"},
            f"dynamic semantics target[{index}]",
        )
        target = DynamicTargetRuntimeStateV2(
            target_index=_nonnegative_int_field(row, "target_index"),
            attackable=_strict_bool_field(row, "attackable"),
            effective_armor=_nonnegative_number_field(
                row, "effective_armor"
            ),
            current_health=_nonnegative_number_field(row, "current_health"),
            dead=_strict_bool_field(row, "dead"),
            death_time_ms=(
                _nonnegative_int_field(row, "death_time_ms")
                if "death_time_ms" in row
                else None
            ),
        )
        if target.target_index != index:
            raise SimBridgeProtocolError(
                "dynamic semantics target indexes are not contiguous"
            )
        if target.dead != (target.current_health <= 0):
            raise SimBridgeProtocolError(
                "dynamic semantics dead flag disagrees with current health"
            )
        if target.dead != (target.death_time_ms is not None):
            raise SimBridgeProtocolError(
                "dynamic semantics death time presence disagrees with dead flag"
            )
        if target.dead and target.attackable:
            raise SimBridgeProtocolError("dead dynamic target cannot be attackable")
        targets.append(target)
    result = DynamicTargetSemanticsStateV2(
        schema=_text_field(raw, "schema", protocol=True),
        config_digest=_sha256_field(raw, "config_digest", protocol=True),
        environment_generation=_positive_int_field(
            raw, "environment_generation"
        ),
        same_timestamp_order=_text_field(
            raw, "same_timestamp_order", protocol=True
        ),
        attackability_events_processed=_nonnegative_int_field(
            raw, "attackability_events_processed"
        ),
        attackability_events_total=_nonnegative_int_field(
            raw, "attackability_events_total"
        ),
        effective_armor_events_processed=_nonnegative_int_field(
            raw, "effective_armor_events_processed"
        ),
        effective_armor_events_total=_nonnegative_int_field(
            raw, "effective_armor_events_total"
        ),
        targets=tuple(targets),
    )
    if (
        result.schema != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or result.config_digest != config.content_sha256
        or result.environment_generation != generation
        or result.same_timestamp_order != config.same_timestamp_order
        or result.attackability_events_total != len(config.attackability_events)
        or result.effective_armor_events_total
        != len(config.effective_armor_events)
        or result.attackability_events_processed
        > result.attackability_events_total
        or result.effective_armor_events_processed
        > result.effective_armor_events_total
        or len(targets) != len(config.target_health)
    ):
        raise SimBridgeProtocolError(
            "dynamic target semantics state violates its config binding"
        )
    return result


def _attackability_batch_v2(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicAttackabilityReceiptBatchV2:
    raw, rows = _transition_batch_envelope(
        value,
        requested_cursor=requested_cursor,
        generation=generation,
        config=config,
        total=len(config.attackability_events),
        label="attackability receipt batch",
    )
    receipts: list[DynamicAttackabilityTransitionReceiptV2] = []
    for offset, value_row in enumerate(rows):
        event = config.attackability_events[requested_cursor + offset]
        row = _mapping_with_optional(
            value_row,
            {
                "schedule_index",
                "time_ms",
                "target_index",
                "requested_attackable",
                "previous_attackable",
                "resulting_attackable",
                "status",
            },
            {"retargeted_to"},
            "attackability receipt",
        )
        receipt = DynamicAttackabilityTransitionReceiptV2(
            schedule_index=_nonnegative_int_field(row, "schedule_index"),
            time_ms=_nonnegative_int_field(row, "time_ms"),
            target_index=_nonnegative_int_field(row, "target_index"),
            requested_attackable=_strict_bool_field(
                row, "requested_attackable"
            ),
            previous_attackable=_strict_bool_field(
                row, "previous_attackable"
            ),
            resulting_attackable=_strict_bool_field(
                row, "resulting_attackable"
            ),
            status=_text_field(row, "status", protocol=True),
            retargeted_to=(
                _nonnegative_int_field(row, "retargeted_to")
                if "retargeted_to" in row
                else None
            ),
        )
        if (
            receipt.schedule_index != event.schedule_index
            or receipt.time_ms != event.time_ms
            or receipt.target_index != event.target_index
            or receipt.requested_attackable != event.attackable
            or receipt.status not in DYNAMIC_TRANSITION_STATUSES_V2
        ):
            raise SimBridgeProtocolError(
                "attackability receipt differs from its scheduled event"
            )
        _validate_attackability_transition(receipt, len(config.target_health))
        receipts.append(receipt)
    return DynamicAttackabilityReceiptBatchV2(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=requested_cursor,
        next_cursor=_nonnegative_int_field(raw, "next_cursor"),
        schedule_complete=_strict_bool_field(raw, "schedule_complete"),
        receipts=tuple(receipts),
    )


def _armor_batch_v2(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicArmorReceiptBatchV2:
    raw, rows = _transition_batch_envelope(
        value,
        requested_cursor=requested_cursor,
        generation=generation,
        config=config,
        total=len(config.effective_armor_events),
        label="armor receipt batch",
    )
    receipts: list[DynamicArmorTransitionReceiptV2] = []
    for offset, value_row in enumerate(rows):
        event = config.effective_armor_events[requested_cursor + offset]
        row = _exact_mapping(
            value_row,
            {
                "schedule_index",
                "time_ms",
                "target_index",
                "requested_effective_armor",
                "previous_target_armor",
                "resulting_target_armor",
                "status",
            },
            "armor receipt",
            protocol=True,
        )
        receipt = DynamicArmorTransitionReceiptV2(
            schedule_index=_nonnegative_int_field(row, "schedule_index"),
            time_ms=_nonnegative_int_field(row, "time_ms"),
            target_index=_nonnegative_int_field(row, "target_index"),
            requested_effective_armor=_nonnegative_number_field(
                row, "requested_effective_armor"
            ),
            previous_target_armor=_nonnegative_number_field(
                row, "previous_target_armor"
            ),
            resulting_target_armor=_nonnegative_number_field(
                row, "resulting_target_armor"
            ),
            status=_text_field(row, "status", protocol=True),
        )
        if (
            receipt.schedule_index != event.schedule_index
            or receipt.time_ms != event.time_ms
            or receipt.target_index != event.target_index
            or not _same_float(
                receipt.requested_effective_armor, event.effective_armor
            )
            or receipt.status not in DYNAMIC_TRANSITION_STATUSES_V2
        ):
            raise SimBridgeProtocolError(
                "armor receipt differs from its scheduled event"
            )
        if receipt.status in {"APPLIED", "NO_CHANGE"} and not _same_float(
            receipt.resulting_target_armor, receipt.requested_effective_armor
        ):
            raise SimBridgeProtocolError(
                "applied armor receipt did not reach its requested value"
            )
        if receipt.status.startswith("CANCELED_") and not _same_float(
            receipt.previous_target_armor, receipt.resulting_target_armor
        ):
            raise SimBridgeProtocolError(
                "canceled armor receipt changed target armor"
            )
        receipts.append(receipt)
    return DynamicArmorReceiptBatchV2(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=requested_cursor,
        next_cursor=_nonnegative_int_field(raw, "next_cursor"),
        schedule_complete=_strict_bool_field(raw, "schedule_complete"),
        receipts=tuple(receipts),
    )


def _transition_batch_envelope(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
    total: int,
    label: str,
) -> tuple[JSONMap, list[JSONMap]]:
    raw = _exact_mapping(
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
        label,
        protocol=True,
    )
    cursor = _nonnegative_int_field(raw, "cursor")
    next_cursor = _nonnegative_int_field(raw, "next_cursor")
    rows = _object_array(raw["receipts"], f"{label}.receipts", protocol=True)
    if (
        _text_field(raw, "schema", protocol=True)
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or _sha256_field(raw, "config_digest", protocol=True)
        != config.content_sha256
        or _positive_int_field(raw, "environment_generation") != generation
        or cursor != requested_cursor
        or next_cursor != cursor + len(rows)
        or next_cursor > total
        or _strict_bool_field(raw, "schedule_complete") != (next_cursor == total)
    ):
        raise SimBridgeProtocolError(f"{label} violates its cursor/content binding")
    return raw, rows


def _background_batch_v2(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicDamageReceiptBatchV2:
    raw = _exact_mapping(
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
        "background receipt batch",
        protocol=True,
    )
    rows = _object_array(raw["receipts"], "background receipts", protocol=True)
    cursor = _nonnegative_int_field(raw, "cursor")
    next_cursor = _nonnegative_int_field(raw, "next_cursor")
    if (
        _text_field(raw, "schema", protocol=True)
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or _sha256_field(raw, "config_digest", protocol=True)
        != config.content_sha256
        or _positive_int_field(raw, "environment_generation") != generation
        or cursor != requested_cursor
        or next_cursor != cursor + len(rows)
        or next_cursor > len(config.background_damage_events)
        or _strict_bool_field(raw, "schedule_complete")
        != (next_cursor == len(config.background_damage_events))
    ):
        raise SimBridgeProtocolError(
            "background receipt batch violates its cursor/content binding"
        )
    receipts: list[DynamicDamageReceiptV2] = []
    for offset, value_row in enumerate(rows):
        event = config.background_damage_events[cursor + offset]
        row = _mapping_with_optional(
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
            "background receipt",
        )
        receipt = DynamicDamageReceiptV2(
            damage_ordinal=(
                _positive_int_field(row, "damage_ordinal")
                if "damage_ordinal" in row
                else 0
            ),
            schedule_index=_nonnegative_int_field(row, "schedule_index"),
            event_id=_text_field(row, "event_id", protocol=True),
            time_ms=_nonnegative_int_field(row, "time_ms"),
            target_index=_nonnegative_int_field(row, "target_index"),
            requested_damage=_nonnegative_number_field(
                row, "requested_damage"
            ),
            applied_damage=_nonnegative_number_field(row, "applied_damage"),
            overkill_damage=_nonnegative_number_field(
                row, "overkill_damage"
            ),
            killed=_strict_bool_field(row, "killed"),
            status=_text_field(row, "status", protocol=True),
            retargeted_to=(
                _nonnegative_int_field(row, "retargeted_to")
                if "retargeted_to" in row
                else None
            ),
        )
        if (
            receipt.schedule_index != event.schedule_index
            or receipt.event_id != event.event_id
            or receipt.time_ms != event.time_ms
            or receipt.target_index != event.target_index
            or not _same_float(receipt.requested_damage, event.damage)
            or receipt.status not in DYNAMIC_DAMAGE_STATUSES_V2
            or (receipt.damage_ordinal == 0 and receipt.status != "CANCELED_TARGET_DEAD")
            or not _numbers_conserve(
                receipt.requested_damage,
                receipt.applied_damage + receipt.overkill_damage,
            )
            or (
                receipt.status != "APPLIED"
                and (receipt.applied_damage != 0 or receipt.killed)
            )
        ):
            raise SimBridgeProtocolError(
                "background receipt violates its scheduled damage contract"
            )
        if receipt.retargeted_to is not None and (
            receipt.retargeted_to >= len(config.target_health)
            or not receipt.killed
        ):
            raise SimBridgeProtocolError("background receipt retarget is invalid")
        receipts.append(receipt)
    positive_receipts = [row for row in receipts if row.damage_ordinal > 0]
    if len(positive_receipts) != len(receipts) and any(
        row.damage_ordinal > 0
        for row in receipts[len(positive_receipts):]
    ):
        raise SimBridgeProtocolError(
            "background receipts without damage ordinals must be a terminal suffix"
        )
    _strictly_increasing_ordinals(positive_receipts, "background receipts")
    return DynamicDamageReceiptBatchV2(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        schedule_complete=_strict_bool_field(raw, "schedule_complete"),
        receipts=tuple(receipts),
    )


def _candidate_batch_v2(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config: DynamicTargetSemanticsConfigV2,
) -> DynamicCandidateDamageReceiptBatchV2:
    raw = _exact_mapping(
        value,
        {
            "schema",
            "config_digest",
            "environment_generation",
            "cursor",
            "next_cursor",
            "receipts",
        },
        "candidate receipt batch",
        protocol=True,
    )
    rows = _object_array(raw["receipts"], "candidate receipts", protocol=True)
    cursor = _nonnegative_int_field(raw, "cursor")
    next_cursor = _nonnegative_int_field(raw, "next_cursor")
    if (
        _text_field(raw, "schema", protocol=True)
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or _sha256_field(raw, "config_digest", protocol=True)
        != config.content_sha256
        or _positive_int_field(raw, "environment_generation") != generation
        or cursor != requested_cursor
        or next_cursor != cursor + len(rows)
    ):
        raise SimBridgeProtocolError(
            "candidate receipt batch violates its cursor/content binding"
        )
    receipts: list[DynamicCandidateDamageReceiptV2] = []
    for value_row in rows:
        row = _mapping_with_optional(
            value_row,
            {
                "damage_ordinal",
                "time_ms",
                "target_index",
                "requested_damage",
                "applied_damage",
                "overkill_damage",
                "killed",
                "status",
                "action",
                "outcome",
                "execution_id",
                "execution_index",
                "landed_execution_index",
                "resolution_phase",
                "outcome_computed",
                "random_stream_rewound",
            },
            {"attempt_id", "retargeted_to"},
            "candidate receipt",
        )
        action_raw = row["action"]
        if not isinstance(action_raw, Mapping):
            raise SimBridgeProtocolError("candidate receipt action must be an object")
        receipt = DynamicCandidateDamageReceiptV2(
            damage_ordinal=_positive_int_field(row, "damage_ordinal"),
            time_ms=_nonnegative_int_field(row, "time_ms"),
            target_index=_nonnegative_int_field(row, "target_index"),
            requested_damage=_nonnegative_number_field(
                row, "requested_damage"
            ),
            applied_damage=_nonnegative_number_field(row, "applied_damage"),
            overkill_damage=_nonnegative_number_field(
                row, "overkill_damage"
            ),
            killed=_strict_bool_field(row, "killed"),
            status=_text_field(row, "status", protocol=True),
            action=_strict_action_ref_v2(action_raw),
            outcome=_text_field(row, "outcome", protocol=True),
            execution_id=_nonnegative_int_field(row, "execution_id"),
            execution_index=_nonnegative_int_field(row, "execution_index"),
            landed_execution_index=_nonnegative_int_field(
                row, "landed_execution_index"
            ),
            resolution_phase=_text_field(
                row, "resolution_phase", protocol=True
            ),
            outcome_computed=_strict_bool_field(row, "outcome_computed"),
            random_stream_rewound=_strict_bool_field(
                row, "random_stream_rewound"
            ),
            attempt_id=(
                _text_field(row, "attempt_id", protocol=True)
                if "attempt_id" in row
                else None
            ),
            retargeted_to=(
                _nonnegative_int_field(row, "retargeted_to")
                if "retargeted_to" in row
                else None
            ),
        )
        _validate_candidate_receipt(receipt, len(config.target_health))
        receipts.append(receipt)
    _strictly_increasing_ordinals(receipts, "candidate receipts")
    return DynamicCandidateDamageReceiptBatchV2(
        schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config_digest=config.content_sha256,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        receipts=tuple(receipts),
    )


def _validate_attackability_transition(
    receipt: DynamicAttackabilityTransitionReceiptV2, target_count: int
) -> None:
    if receipt.status == "APPLIED":
        valid = (
            receipt.previous_attackable != receipt.requested_attackable
            and receipt.resulting_attackable == receipt.requested_attackable
        )
    elif receipt.status == "NO_CHANGE":
        valid = (
            receipt.previous_attackable
            == receipt.requested_attackable
            == receipt.resulting_attackable
        )
    elif receipt.status == "CANCELED_TARGET_DEAD":
        valid = not receipt.previous_attackable and not receipt.resulting_attackable
    else:
        valid = receipt.previous_attackable == receipt.resulting_attackable
    if not valid:
        raise SimBridgeProtocolError(
            "attackability receipt status/state transition is inconsistent"
        )
    if receipt.retargeted_to is not None and (
        receipt.retargeted_to >= target_count
        or receipt.status.startswith("CANCELED_")
    ):
        raise SimBridgeProtocolError(
            "attackability receipt retarget index is invalid"
        )


def _validate_candidate_receipt(
    receipt: DynamicCandidateDamageReceiptV2, target_count: int
) -> None:
    if (
        receipt.target_index >= target_count
        or receipt.status not in DYNAMIC_CANDIDATE_DAMAGE_STATUSES_V2
        or receipt.outcome not in DYNAMIC_CANDIDATE_OUTCOMES_V2
        or receipt.resolution_phase
        not in DYNAMIC_CANDIDATE_RESOLUTION_PHASES_V2
        or receipt.random_stream_rewound
        or not _numbers_conserve(
            receipt.requested_damage,
            receipt.applied_damage + receipt.overkill_damage,
        )
    ):
        raise SimBridgeProtocolError("candidate receipt violates base invariants")
    if receipt.retargeted_to is not None and (
        receipt.retargeted_to >= target_count or not receipt.killed
    ):
        raise SimBridgeProtocolError("candidate receipt retarget is invalid")
    if receipt.status == "CANCELED_TARGET_UNATTACKABLE":
        if (
            receipt.resolution_phase != "TARGET_UNATTACKABLE_AFTER_OUTCOME"
            or receipt.outcome != "CANCELED"
            or receipt.applied_damage != 0
            or receipt.killed
            or not receipt.outcome_computed
        ):
            raise SimBridgeProtocolError(
                "unattackable candidate cancellation is inconsistent"
            )
    elif receipt.status == "CANCELED_TARGET_DEAD":
        if (
            receipt.resolution_phase
            not in {"TARGET_DEAD_BEFORE_OUTCOME", "TARGET_DIED_AFTER_OUTCOME"}
            or receipt.outcome != "CANCELED"
            or receipt.applied_damage != 0
            or receipt.killed
        ):
            raise SimBridgeProtocolError("dead candidate cancellation is inconsistent")
    elif receipt.status == "NO_DAMAGE":
        if (
            receipt.resolution_phase != "NO_DAMAGE_AFTER_OUTCOME"
            or receipt.applied_damage != 0
            or receipt.killed
        ):
            raise SimBridgeProtocolError(
                "zero-damage candidate receipt is inconsistent"
            )
    elif receipt.resolution_phase != "APPLIED_AFTER_OUTCOME":
        raise SimBridgeProtocolError("applied candidate resolution phase is invalid")


def _strict_action_ref_v2(value: Mapping[str, Any]) -> ActionRef:
    fields = {"spell_id", "item_id", "other_id", "tag"}
    if not value or not set(value).issubset(fields):
        raise SimBridgeProtocolError(
            "candidate receipt action field set is invalid"
        )
    action = ActionRef.from_wire(value)
    identity_count = sum(
        bool(item) for item in (action.spell_id, action.item_id, action.other_id)
    )
    if identity_count != 1:
        raise SimBridgeProtocolError(
            "candidate receipt action must have exactly one nonzero identity"
        )
    return action


def _validate_background_events(config: DynamicTargetSemanticsConfigV2) -> None:
    seen_ids: set[str] = set()
    prior_time = -1
    for index, event in enumerate(config.background_damage_events):
        if event.schedule_index != index:
            raise DynamicV2ConfigError(
                "background_damage_events schedule_index must cover 0..N-1"
            )
        if event.time_ms < prior_time:
            raise DynamicV2ConfigError(
                "background_damage_events must be sorted by time_ms"
            )
        prior_time = event.time_ms
        if event.target_index >= len(config.target_health):
            raise DynamicV2ConfigError(
                f"background_damage_events[{index}].target_index out of range"
            )
        if event.event_id in seen_ids:
            raise DynamicV2ConfigError(
                f"duplicate background event_id {event.event_id!r}"
            )
        seen_ids.add(event.event_id)


def _validate_transition_events(
    events: tuple[Any, ...], *, target_count: int, label: str
) -> None:
    prior_time = -1
    seen: set[tuple[int, int]] = set()
    for index, event in enumerate(events):
        if event.schedule_index != index:
            raise DynamicV2ConfigError(
                f"{label} schedule_index must cover 0..N-1 exactly"
            )
        if event.time_ms < prior_time:
            raise DynamicV2ConfigError(f"{label} must be sorted by time_ms")
        prior_time = event.time_ms
        if event.target_index >= target_count:
            raise DynamicV2ConfigError(
                f"{label}[{index}].target_index out of range"
            )
        key = (event.time_ms, event.target_index)
        if key in seen:
            raise DynamicV2ConfigError(
                f"duplicate {label} event for target/time"
            )
        seen.add(key)


def _typed_tuple(value: Any, row_type: type, label: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(row, row_type) for row in value
    ):
        raise TypeError(f"{label} must be a tuple of {row_type.__name__}")


def _state_field(value: Mapping[str, Any], command: str) -> JSONMap:
    state = value.get("state")
    if not isinstance(state, dict):
        raise SimBridgeProtocolError(f"{command} response is missing state")
    return dict(state)


def _exact_mapping(
    value: Any,
    fields: set[str],
    label: str,
    *,
    protocol: bool = False,
) -> JSONMap:
    if not isinstance(value, Mapping):
        _raise(label + " must be an object", protocol)
    raw = dict(value)
    if set(raw) != fields:
        _raise(label + " field set mismatch", protocol)
    return raw


def _mapping_with_optional(
    value: Any, required: set[str], optional: set[str], label: str
) -> JSONMap:
    if not isinstance(value, Mapping):
        raise SimBridgeProtocolError(f"{label} must be an object")
    raw = dict(value)
    if not required <= set(raw) or not set(raw) <= required | optional:
        raise SimBridgeProtocolError(f"{label} field set mismatch")
    return raw


def _object_array(
    value: Any, label: str, *, protocol: bool = False
) -> list[JSONMap]:
    if not isinstance(value, list) or any(
        not isinstance(row, Mapping) for row in value
    ):
        _raise(label + " must be an array of objects", protocol)
    return [dict(row) for row in value]


def _raise(message: str, protocol: bool) -> None:
    if protocol:
        raise SimBridgeProtocolError(message)
    raise DynamicV2ConfigError(message)


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _signed_int64(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed < -(1 << 63) or parsed > (1 << 63) - 1:
        raise DynamicV2ConfigError(f"{label} exceeds the signed int64 range")
    return parsed


def _strict_json_object_v2(value: Any, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise SimBridgeProtocolError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SimBridgeProtocolError(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise SimBridgeProtocolError(f"{label} must be an object")
    return decoded


def _nonnegative_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be boolean")
    return value


def _strict_int_field(value: Mapping[str, Any], field: str) -> int:
    return _strict_int(value.get(field), field)


def _nonnegative_int_field(value: Mapping[str, Any], field: str) -> int:
    try:
        return _nonnegative_int(value.get(field), field)
    except (TypeError, ValueError) as error:
        raise SimBridgeProtocolError(str(error)) from error


def _positive_int_field(value: Mapping[str, Any], field: str) -> int:
    result = _nonnegative_int_field(value, field)
    if result <= 0:
        raise SimBridgeProtocolError(f"{field} must be positive")
    return result


def _strict_bool_field(value: Mapping[str, Any], field: str) -> bool:
    try:
        return _strict_bool(value.get(field), field)
    except TypeError as error:
        raise SimBridgeProtocolError(str(error)) from error


def _number_field(value: Mapping[str, Any], field: str) -> float:
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{field} must be numeric")
    return float(raw)


def _nonnegative_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise DynamicV2ConfigError(f"{label} must be finite and non-negative")
    return parsed


def _nonnegative_number_field(value: Mapping[str, Any], field: str) -> float:
    try:
        return _nonnegative_finite_number(value.get(field), field)
    except (TypeError, ValueError) as error:
        raise SimBridgeProtocolError(str(error)) from error


def _positive_number_field(value: Mapping[str, Any], field: str) -> float:
    result = _nonnegative_number_field(value, field)
    if result <= 0:
        raise SimBridgeProtocolError(f"{field} must be positive")
    return result


def _text_field(
    value: Mapping[str, Any], field: str, *, protocol: bool = False
) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        _raise(f"{field} must be nonempty text", protocol)
    return raw


def _sha256_field(
    value: Mapping[str, Any], field: str, *, protocol: bool = False
) -> str:
    raw = value.get(field)
    if (
        not isinstance(raw, str)
        or len(raw) != 64
        or any(character not in _HEX for character in raw)
    ):
        _raise(f"{field} must be lowercase SHA-256", protocol)
    return raw


def _same_float(left: float, right: float) -> bool:
    return struct.pack(">d", float(left)) == struct.pack(">d", float(right))


def _numbers_conserve(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)


def _strictly_increasing_ordinals(rows: list[Any], label: str) -> None:
    ordinals = [row.damage_ordinal for row in rows]
    if any(left >= right for left, right in zip(ordinals, ordinals[1:])):
        raise SimBridgeProtocolError(f"{label} damage ordinals are not increasing")


__all__ = (
    "DYNAMIC_SAME_TIMESTAMP_ORDER_V2",
    "DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2",
    "DynamicArmorReceiptBatchV2",
    "DynamicArmorTransitionReceiptV2",
    "DynamicAttackabilityEventV2",
    "DynamicAttackabilityReceiptBatchV2",
    "DynamicAttackabilityTransitionReceiptV2",
    "DynamicCandidateDamageReceiptBatchV2",
    "DynamicCandidateDamageReceiptV2",
    "DynamicDamageReceiptBatchV2",
    "DynamicDamageReceiptV2",
    "DynamicEffectiveArmorEventV2",
    "DynamicLifecycleTargetStateV2",
    "DynamicLoadReceiptV2",
    "DynamicLoadResultV2",
    "DynamicTargetRuntimeStateV2",
    "DynamicTargetSemanticsConfigV2",
    "DynamicTargetSemanticsStateV2",
    "DynamicTeamLifecycleStateV2",
    "DynamicV2ConfigError",
    "SimulatorBridgeDynamicV2",
    "dynamic_target_semantics_config_from_wire_v2",
    "dynamic_target_semantics_digest_v2",
)
