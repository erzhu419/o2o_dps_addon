"""Versioned dynamic-v3 load binding for diagnostic Fury rollouts.

The only new executable claim is that ``load_dynamic_v3`` owns idle advancement
when all living targets are temporarily unattackable.  Target health, armor,
attackability, and team damage remain ``SIMULATOR_HYPOTHESIS`` inputs.  This
module does not connect the load to a formal runner or a comparison gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping

from .fury_dynamic_target_semantics_v4 import (
    DynamicRolloutLoadV2,
    FuryDynamicTargetSemanticsV4Error,
    _mapping,
    _sha256,
    _signed_int64,
    _strict_int,
    _strict_json_copy,
    validate_dynamic_load_request_v2,
)
from .fury_paired_multiseed_runner_v2 import sha256_json
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_MODE_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicTargetSemanticsConfigV3,
    DynamicV3ConfigError,
    _v2_projection,
    _validate_request_horizon_v3,
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]
DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3 = "fury_full_policy_dynamic_load/v3"
DYNAMIC_LOAD_BINDING_SCHEMA_V3 = "fury_full_policy_dynamic_load_binding/v3"
IDLE_BINDING_SCHEMA_V5 = "fury_dynamic_idle_advance_binding/v5"
IDLE_BINDING_CONTENT_SCHEMA_V5 = "fury_dynamic_idle_advance_content/v5"
IMPLEMENTATION_REVISION = "v5.0_load_dynamic_v3_central_idle_hypothesis"
STATUS = "DYNAMIC_V3_CENTRAL_IDLE_SIMULATOR_HYPOTHESIS_NONVOTING"
_CLAIM_BOUNDARY = (
    "central idle mechanism and typed lifecycle receipts only",
    "target and team schedules remain SIMULATOR_HYPOTHESIS",
    "no policy comparison admission",
)


class FuryDynamicTargetSemanticsV5Error(RuntimeError):
    """A v5 dynamic-v3 load or content binding is malformed."""


@dataclass(frozen=True)
class DynamicRolloutLoadV3:
    request_sha256: str
    seed: int
    config: DynamicTargetSemanticsConfigV3
    contract_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _sha256(self.request_sha256, "request_sha256")
        _signed_int64(self.seed, "seed")
        if not isinstance(self.config, DynamicTargetSemanticsConfigV3):
            raise TypeError("config must be DynamicTargetSemanticsConfigV3")
        identity = {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config_digest": self.config.content_sha256,
            "idle_advance_mode": self.config.idle_advance_mode,
            "idle_advance_horizon_ms": self.config.idle_advance_horizon_ms,
        }
        object.__setattr__(self, "contract_sha256", sha256_json(identity))

    @classmethod
    def bind(
        cls,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV3,
    ) -> "DynamicRolloutLoadV3":
        request_copy = _strict_json_copy(request, "request")
        load = cls(
            request_sha256=sha256_json(request_copy),
            seed=_strict_int(seed, "seed"),
            config=config,
        )
        validate_dynamic_load_request_v3(load, request_copy)
        return load

    @classmethod
    def from_wire(
        cls, request: Mapping[str, Any], value: Mapping[str, Any]
    ) -> "DynamicRolloutLoadV3":
        raw = _mapping(value, "dynamic-v3 rollout load")
        if set(raw) != {
            "schema",
            "request_sha256",
            "seed",
            "config",
            "contract_sha256",
        }:
            raise FuryDynamicTargetSemanticsV5Error(
                "dynamic-v3 rollout load field set mismatch"
            )
        if raw.get("schema") != DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3:
            raise FuryDynamicTargetSemanticsV5Error(
                "dynamic-v3 rollout load schema is unsupported"
            )
        try:
            config = dynamic_target_semantics_config_from_wire_v3(
                _mapping(raw.get("config"), "config")
            )
        except (TypeError, ValueError, DynamicV3ConfigError) as error:
            raise FuryDynamicTargetSemanticsV5Error(
                f"invalid dynamic-v3 config: {error}"
            ) from error
        load = cls(
            request_sha256=_sha256(
                raw.get("request_sha256"), "request_sha256"
            ),
            seed=_signed_int64(raw.get("seed"), "seed"),
            config=config,
        )
        request_copy = _strict_json_copy(request, "request")
        validate_dynamic_load_request_v3(load, request_copy)
        if raw.get("contract_sha256") != load.contract_sha256:
            raise FuryDynamicTargetSemanticsV5Error(
                "dynamic-v3 rollout load contract SHA-256 mismatch"
            )
        return load

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config": self.config.to_wire(),
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class CompiledDynamicIdleBindingV5:
    artifact: JSONMap
    dynamic_load: DynamicRolloutLoadV3


def validate_dynamic_load_request_v3(
    dynamic_load: DynamicRolloutLoadV3, request: Mapping[str, Any]
) -> None:
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    request_copy = _strict_json_copy(request, "request")
    if dynamic_load.request_sha256 != sha256_json(request_copy):
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v3 load request SHA-256 differs from request"
        )
    projection = DynamicRolloutLoadV2(
        request_sha256=dynamic_load.request_sha256,
        seed=dynamic_load.seed,
        config=_v2_projection(dynamic_load.config),
    )
    try:
        validate_dynamic_load_request_v2(projection, request_copy)
        _validate_request_horizon_v3(request_copy, dynamic_load.config)
    except (TypeError, ValueError, FuryDynamicTargetSemanticsV4Error) as error:
        raise FuryDynamicTargetSemanticsV5Error(
            f"dynamic-v3 request/config binding failed: {error}"
        ) from error


def dynamic_rollout_load_from_adapter_wire_v3(
    value: Mapping[str, Any],
) -> tuple[JSONMap, DynamicRolloutLoadV3]:
    raw = _mapping(value, "dynamic-v3 adapter wire request")
    if set(raw) != {"command", "request", "seed", "dynamic"}:
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v3 adapter wire request field set mismatch"
        )
    if raw.get("command") != "load_dynamic_v3":
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v3 adapter command must be load_dynamic_v3"
        )
    request = _strict_json_copy(
        _mapping(raw.get("request"), "adapter request"), "adapter request"
    )
    try:
        config = dynamic_target_semantics_config_from_wire_v3(
            _mapping(raw.get("dynamic"), "adapter dynamic config")
        )
    except (TypeError, ValueError) as error:
        raise FuryDynamicTargetSemanticsV5Error(
            f"invalid adapter dynamic-v3 config: {error}"
        ) from error
    return request, DynamicRolloutLoadV3.bind(
        request, _signed_int64(raw.get("seed"), "seed"), config
    )


def upgrade_dynamic_rollout_load_v5(
    request: Mapping[str, Any], prior: DynamicRolloutLoadV2
) -> DynamicRolloutLoadV3:
    """Create a v3 load without changing any v2 hypothesis schedule bytes."""

    if not isinstance(prior, DynamicRolloutLoadV2):
        raise TypeError("prior must be DynamicRolloutLoadV2")
    request_copy = _strict_json_copy(request, "request")
    validate_dynamic_load_request_v2(prior, request_copy)
    encounter = _mapping(request_copy.get("encounter"), "request.encounter")
    duration = encounter.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise FuryDynamicTargetSemanticsV5Error(
            "request encounter.duration must be numeric"
        )
    horizon_float = float(duration) * 1000.0
    horizon_ms = int(horizon_float)
    if horizon_ms <= 0 or horizon_float != float(horizon_ms):
        raise FuryDynamicTargetSemanticsV5Error(
            "request encounter.duration must resolve to exact positive milliseconds"
        )
    old = prior.config
    config = DynamicTargetSemanticsConfigV3(
        target_health=old.target_health,
        idle_advance_horizon_ms=horizon_ms,
        background_damage_events=old.background_damage_events,
        attackability_events=old.attackability_events,
        effective_armor_events=old.effective_armor_events,
        retarget_mode=old.retarget_mode,
    )
    return DynamicRolloutLoadV3.bind(request_copy, prior.seed, config)


def compile_dynamic_idle_binding_v5(
    request: Mapping[str, Any], dynamic_load: DynamicRolloutLoadV3
) -> CompiledDynamicIdleBindingV5:
    """Emit a small content-addressed, permanently non-voting binding."""

    request_copy = _strict_json_copy(request, "request")
    validate_dynamic_load_request_v3(dynamic_load, request_copy)
    core: JSONMap = {
        "schema": IDLE_BINDING_SCHEMA_V5,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "required_bridge_command": "load_dynamic_v3",
        "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        "dynamic_rollout_load": dynamic_load.to_wire(),
        "request_sha256": dynamic_load.request_sha256,
        "idle_advance_mode": DYNAMIC_IDLE_ADVANCE_MODE_V3,
        "central_simulator_idle_advance": True,
        "python_policy_pause_emulation": False,
        "formal_runner_connected": False,
        "historical_truth": False,
        "comparison_eligible": False,
        "voting_eligible": False,
        "claim_boundary": list(_CLAIM_BOUNDARY),
    }
    artifact = json.loads(
        json.dumps(
            core,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    artifact["content_address"] = {
        "schema": IDLE_BINDING_CONTENT_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_dynamic_idle_binding_v5(artifact, request=request_copy)
    return CompiledDynamicIdleBindingV5(
        artifact=artifact, dynamic_load=dynamic_load
    )


def validate_dynamic_idle_binding_v5(
    value: Mapping[str, Any], *, request: Mapping[str, Any]
) -> JSONMap:
    raw = _strict_json_copy(value, "dynamic-v5 idle binding")
    expected_fields = {
        "schema",
        "implementation_revision",
        "status",
        "required_bridge_command",
        "dynamic_config_schema",
        "dynamic_rollout_load",
        "request_sha256",
        "idle_advance_mode",
        "central_simulator_idle_advance",
        "python_policy_pause_emulation",
        "formal_runner_connected",
        "historical_truth",
        "comparison_eligible",
        "voting_eligible",
        "claim_boundary",
        "content_address",
    }
    if set(raw) != expected_fields or any(
        raw.get(key) is not False
        for key in (
            "python_policy_pause_emulation",
            "formal_runner_connected",
            "historical_truth",
            "comparison_eligible",
            "voting_eligible",
        )
    ) or (
        raw.get("schema") != IDLE_BINDING_SCHEMA_V5
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("status") != STATUS
        or raw.get("required_bridge_command") != "load_dynamic_v3"
        or raw.get("dynamic_config_schema")
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        or raw.get("idle_advance_mode") != DYNAMIC_IDLE_ADVANCE_MODE_V3
        or raw.get("central_simulator_idle_advance") is not True
        or raw.get("claim_boundary") != list(_CLAIM_BOUNDARY)
    ):
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v5 identity or scientific boundary mismatch"
        )
    load = DynamicRolloutLoadV3.from_wire(
        request,
        _mapping(raw.get("dynamic_rollout_load"), "dynamic_rollout_load"),
    )
    if raw.get("request_sha256") != load.request_sha256:
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v5 request/load digest mismatch"
        )
    content = _mapping(raw.get("content_address"), "content_address")
    expected_content = {
        "schema": IDLE_BINDING_CONTENT_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if content != expected_content:
        raise FuryDynamicTargetSemanticsV5Error(
            "dynamic-v5 content address mismatch"
        )
    return raw


__all__ = (
    "CompiledDynamicIdleBindingV5",
    "DYNAMIC_LOAD_BINDING_SCHEMA_V3",
    "DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3",
    "DynamicRolloutLoadV3",
    "FuryDynamicTargetSemanticsV5Error",
    "compile_dynamic_idle_binding_v5",
    "dynamic_rollout_load_from_adapter_wire_v3",
    "upgrade_dynamic_rollout_load_v5",
    "validate_dynamic_idle_binding_v5",
    "validate_dynamic_load_request_v3",
)
