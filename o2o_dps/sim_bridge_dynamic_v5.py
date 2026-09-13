"""Pre-event Warrior player checkpoint on top of dynamic-v4 targets.

The native v15 bridge restores only fields it can actually make causal:
rage, stance, GCD, named spell CDs and MH/OH deadlines. A nonempty queue,
self aura/proc or candidate-owned debuff is rejected by both clients.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .brainofcat_shadow_checkpoint_v1 import validate_record
from .sim_bridge import SimBridgeProtocolError
from . import sim_bridge_dynamic_v2 as _v2
from . import sim_bridge_dynamic_v4 as _v4


PLAYER_CHECKPOINT_SCHEMA_V1 = "o2o_player_state_checkpoint/v1"


class PlayerCheckpointError(ValueError):
    """A player checkpoint is incomplete or cannot be restored exactly."""


def _duration(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 86_400_000:
        raise PlayerCheckpointError(f"{label} must be integer milliseconds in [0, 86400000]")
    return value


@dataclass(frozen=True)
class CooldownCheckpointV1:
    spell_id: int
    remaining_ms: int
    tag: int = 0

    def __post_init__(self) -> None:
        if type(self.spell_id) is not int or self.spell_id <= 0:
            raise PlayerCheckpointError("cooldown spell_id must be positive")
        if type(self.tag) is not int or self.tag < 0:
            raise PlayerCheckpointError("cooldown tag must be nonnegative")
        _duration(self.remaining_ms, "cooldown remaining_ms")

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": {"spell_id": self.spell_id, "tag": self.tag},
            "remaining_ms": self.remaining_ms,
        }


@dataclass(frozen=True)
class PlayerStateCheckpointV1:
    rage_current: float
    stance: str
    gcd_remaining_ms: int
    cooldowns: tuple[CooldownCheckpointV1, ...]
    mh_swing_remaining_ms: int
    oh_swing_remaining_ms: int | None
    queued_next_swing: str
    self_auras_and_procs: tuple[()] = ()
    candidate_owned_debuffs: tuple[()] = ()

    def __post_init__(self) -> None:
        if (type(self.rage_current) not in (float, int)
                or not math.isfinite(self.rage_current)
                or not 0 <= self.rage_current <= 100):
            raise PlayerCheckpointError("rage_current must be finite in [0,100]")
        if self.stance not in {"BATTLE", "DEFENSIVE", "BERSERKER"}:
            raise PlayerCheckpointError("stance is unsupported")
        _duration(self.gcd_remaining_ms, "gcd_remaining_ms")
        _duration(self.mh_swing_remaining_ms, "mh_swing_remaining_ms")
        if self.oh_swing_remaining_ms is not None:
            _duration(self.oh_swing_remaining_ms, "oh_swing_remaining_ms")
        if type(self.cooldowns) is not tuple or any(
            not isinstance(row, CooldownCheckpointV1) for row in self.cooldowns
        ):
            raise PlayerCheckpointError("cooldowns must be typed and complete")
        if len({(row.spell_id, row.tag) for row in self.cooldowns}) != len(self.cooldowns):
            raise PlayerCheckpointError("cooldowns repeat a spell action")
        if self.queued_next_swing != "NONE":
            raise PlayerCheckpointError("queued next-swing state is not restorable in v1")
        if self.self_auras_and_procs or self.candidate_owned_debuffs:
            raise PlayerCheckpointError("active aura/proc or owned debuff is not restorable in v1")

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": PLAYER_CHECKPOINT_SCHEMA_V1,
            "rage_current": float(self.rage_current),
            "stance": self.stance,
            "gcd_remaining_ms": self.gcd_remaining_ms,
            "cooldowns": [row.to_wire() for row in self.cooldowns],
            "mh_swing_remaining_ms": self.mh_swing_remaining_ms,
            "oh_swing_remaining_ms": self.oh_swing_remaining_ms,
            "queued_next_swing": self.queued_next_swing,
            "self_auras_and_procs": [],
            "candidate_owned_debuffs": [],
        }


def player_checkpoint_from_shadow_v1(record: Mapping[str, Any]) -> PlayerStateCheckpointV1:
    """Reject current visible-target-only Shadow rows for exact replay."""
    validated = validate_record(record)
    if validated["kind"] != "checkpoint":
        raise PlayerCheckpointError("Shadow row is not a checkpoint")
    raise PlayerCheckpointError(
        "Shadow checkpoint is not exact-ready: "
        + ", ".join(validated["exactCheckpointBlockers"])
    )


class SimulatorBridgeDynamicV5(_v4.SimulatorBridgeDynamicV4):
    """Load a v4 target environment with a pre-event Warrior checkpoint."""

    def load_dynamic_v5(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: _v4.DynamicTargetSemanticsConfigV4,
        player_state: PlayerStateCheckpointV1,
    ) -> _v4.DynamicLoadResultV4:
        if not isinstance(config, _v4.DynamicTargetSemanticsConfigV4):
            raise TypeError("config must be DynamicTargetSemanticsConfigV4")
        if not isinstance(player_state, PlayerStateCheckpointV1):
            raise TypeError("player_state must be PlayerStateCheckpointV1")
        self._dynamic_binding = None
        request_copy = _v2._strict_json_object_v2(request, "RaidSimRequest")
        _v4._validate_request_binding_v4(request_copy, config)
        response = self._request(
            "load_dynamic_v5",
            request=request_copy,
            seed=_v2._signed_int64(seed, "seed"),
            dynamic=config.to_wire(),
            player_state=player_state.to_wire(),
        )
        raw_receipt = response.get("dynamic_load")
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError("load_dynamic_v5 lacks dynamic_load receipt")
        receipt = _v4._dynamic_load_receipt_v4(raw_receipt, config)
        if _v2._positive_int_field(response, "environment_generation") != receipt.environment_generation:
            raise SimBridgeProtocolError("load_dynamic_v5 generation differs from receipt")
        state = _v2._state_field(response, "load_dynamic_v5")
        _v4._validate_dynamic_state_binding_v4(
            state, generation=receipt.environment_generation, config=config
        )
        _validate_player_state_at_load(state, player_state)
        self._dynamic_binding = (
            receipt.environment_generation, receipt.config_digest, config
        )
        actions = {
            (row.action.spell_id, row.action.tag): row.ready_in_ms
            for row in self.actions()
        }
        for cooldown in player_state.cooldowns:
            observed = actions.get((cooldown.spell_id, cooldown.tag))
            if observed != cooldown.remaining_ms:
                self._dynamic_binding = None
                raise SimBridgeProtocolError(
                    f"restored cooldown {(cooldown.spell_id, cooldown.tag)} "
                    f"is {observed!r} ms, expected {cooldown.remaining_ms} ms"
                )
        return _v4.DynamicLoadResultV4(receipt=receipt, state=state)


def _validate_player_state_at_load(
    state: Mapping[str, Any], checkpoint: PlayerStateCheckpointV1
) -> None:
    if state.get("time_ms") != 0:
        raise SimBridgeProtocolError("player checkpoint load advanced past time zero")
    power = state.get("power")
    if not isinstance(power, Mapping) or power.get("type") != "rage" or power.get("current") != checkpoint.rage_current:
        raise SimBridgeProtocolError("restored rage differs from checkpoint")
    for name, expected in (
        ("gcd_remaining_ms", checkpoint.gcd_remaining_ms),
        ("mh_swing_remaining_ms", checkpoint.mh_swing_remaining_ms),
        ("oh_swing_remaining_ms", checkpoint.oh_swing_remaining_ms),
    ):
        if state.get(name) != expected:
            raise SimBridgeProtocolError(f"restored {name} differs from checkpoint")
    stance_id = {"BATTLE": 2457, "DEFENSIVE": 71, "BERSERKER": 2458}[checkpoint.stance]
    auras = state.get("auras")
    if not isinstance(auras, list) or not any(
        isinstance(row, Mapping)
        and isinstance(row.get("action"), Mapping)
        and row["action"].get("spell_id") == stance_id
        for row in auras
    ):
        raise SimBridgeProtocolError("restored stance aura differs from checkpoint")


__all__ = [
    "CooldownCheckpointV1",
    "PLAYER_CHECKPOINT_SCHEMA_V1",
    "PlayerCheckpointError",
    "PlayerStateCheckpointV1",
    "SimulatorBridgeDynamicV5",
    "player_checkpoint_from_shadow_v1",
]
