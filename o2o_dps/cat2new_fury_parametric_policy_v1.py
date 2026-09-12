"""Executable Cat2_new feedback policy backed by the bounded Fury controller.

The policy consumes only the immutable state and action table supplied at the
current simulator decision boundary.  It translates the existing compact
``FuryTunedPolicyAdapter`` into Cat2_new v4 operations, while masking every
spell/queue request against the bridge's current legal action surface.

This is a development candidate factory.  Its fixed-melee assumption and
simulator action legality are not WoW-client fidelity evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Mapping

from .cat2new_candidate_feedback_loop_v6 import (
    POLICY_INPUT_SCHEMA_V6,
    Cat2NewPolicyIntentV6,
)
from .expert_policy import StanceOp, SwingQueueOp, WAIT_ACTION
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .fury_expert_adapters import FuryExpertState, WeaponMode
from .fury_paired_multiseed_runner_v4 import CAT2NEW_POLICY_ID
from .fury_policy_optimization_v1 import FuryPolicyParameters, FuryTunedPolicyAdapter


JSONMap = dict[str, Any]
IMPLEMENTATION_REVISION = "v1.2_live_flurry_proc_aura_and_screening_grid"


class Cat2NewFuryParametricPolicyV1Error(RuntimeError):
    """A decision input cannot be mapped without inventing runtime state."""


@dataclass(frozen=True)
class Cat2NewFuryParametricPolicyConfigV1:
    parameters: FuryPolicyParameters = field(
        default_factory=lambda: FuryPolicyParameters(use_death_wish=False)
    )
    execute_cost: float = 15.0
    heroic_strike_cost: float = 15.0
    cleave_cost: float = 20.0
    whirlwind_cost: float = 25.0
    fixed_melee_environment: bool = True

    def __post_init__(self) -> None:
        if self.parameters.use_death_wish:
            raise ValueError(
                "Death Wish is disabled until its Cat2 off-GCD lane agrees with "
                "the simulator GCD contract"
            )
        for name in (
            "execute_cost",
            "heroic_strike_cost",
            "cleave_cost",
            "whirlwind_cost",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.fixed_melee_environment is not True:
            raise ValueError(
                "the current dynamic simulator policy requires a fixed-melee environment"
            )


@dataclass(frozen=True)
class _ActionAvailability:
    legal: bool
    ready_in_ms: int
    triggers_gcd: bool


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Cat2NewFuryParametricPolicyV1Error(f"{label} must be an object")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Cat2NewFuryParametricPolicyV1Error(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise Cat2NewFuryParametricPolicyV1Error(f"{label} must be finite")
    return parsed


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Cat2NewFuryParametricPolicyV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _power(state: Mapping[str, Any]) -> float:
    value = state.get("power")
    if isinstance(value, Mapping):
        if value.get("type") not in (None, "rage"):
            raise Cat2NewFuryParametricPolicyV1Error(
                "live_state.power is not rage"
            )
        value = value.get("current")
    return _number(value, "live_state.power.current")


def _action_key(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
    action = _mapping(row.get("action"), "available action.action")
    values = []
    for field_name in ("spell_id", "item_id", "other_id", "tag"):
        raw = action.get(field_name, 0)
        values.append(_integer(raw, f"available action.action.{field_name}"))
    return tuple(values)  # type: ignore[return-value]


def _action_table(value: Any) -> dict[tuple[int, int, int, int], _ActionAvailability]:
    if not isinstance(value, list) or not value:
        raise Cat2NewFuryParametricPolicyV1Error(
            "available_actions must be a nonempty array from bridge.actions"
        )
    result: dict[tuple[int, int, int, int], _ActionAvailability] = {}
    for index, raw in enumerate(value):
        row = _mapping(raw, f"available_actions[{index}]")
        key = _action_key(row)
        if key in result:
            raise Cat2NewFuryParametricPolicyV1Error(
                "available_actions contains a duplicate ActionRef"
            )
        legal = row.get("legal")
        triggers_gcd = row.get("triggers_gcd")
        if not isinstance(legal, bool) or not isinstance(triggers_gcd, bool):
            raise Cat2NewFuryParametricPolicyV1Error(
                "available action legal/triggers_gcd must be boolean"
            )
        result[key] = _ActionAvailability(
            legal=legal,
            ready_in_ms=_integer(
                row.get("ready_in_ms"),
                f"available_actions[{index}].ready_in_ms",
            ),
            triggers_gcd=triggers_gcd,
        )
    return result


def _ref_key(ref: Any) -> tuple[int, int, int, int]:
    return (ref.spell_id, ref.item_id, ref.other_id, ref.tag)


def _aura_rows(state: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = state.get("auras", [])
    if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
        raise Cat2NewFuryParametricPolicyV1Error("live_state.auras must be an array")
    return tuple(raw)


def _aura_active(auras: tuple[Mapping[str, Any], ...], label: str) -> bool:
    wanted = label.casefold()
    for row in auras:
        if str(row.get("label", "")).casefold() != wanted:
            continue
        remaining = row.get("remaining_ms")
        return isinstance(remaining, (int, float)) and not isinstance(
            remaining, bool
        ) and float(remaining) != 0.0
    return False


_WARRIOR_FLURRY_PROC_SPELL_IDS = frozenset(range(12966, 12971))


def _warrior_flurry_active(auras: tuple[Mapping[str, Any], ...]) -> bool:
    """Recognize the live wowsims Warrior Flurry proc aura.

    The bridge exposes the active rank as ``Flurry Proc (<spell id>)`` rather
    than the exclusive-effect category name ``Flurry``.  Bind to the five
    Warrior proc-aura IDs so inactive trigger auras do not look active.
    """

    for row in auras:
        action = row.get("action")
        spell_id = action.get("spell_id") if isinstance(action, Mapping) else None
        if spell_id not in _WARRIOR_FLURRY_PROC_SPELL_IDS:
            continue
        remaining = row.get("remaining_ms")
        if (
            isinstance(remaining, (int, float))
            and not isinstance(remaining, bool)
            and float(remaining) != 0.0
        ):
            return True
    return False


def _stance(auras: tuple[Mapping[str, Any], ...]) -> StanceOp:
    for label, stance in (
        ("Berserker Stance", StanceOp.BERSERKER),
        ("Defensive Stance", StanceOp.DEFENSIVE),
        ("Battle Stance", StanceOp.BATTLE),
    ):
        if _aura_active(auras, label):
            return stance
    raise Cat2NewFuryParametricPolicyV1Error(
        "live_state does not expose an active Warrior stance"
    )


def _target_rows(state: Mapping[str, Any]) -> tuple[int, Mapping[str, Any], int]:
    index = _integer(state.get("target_index"), "live_state.target_index")
    semantics = _mapping(
        state.get("dynamic_target_semantics"),
        "live_state.dynamic_target_semantics",
    )
    raw_rows = semantics.get("targets")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise Cat2NewFuryParametricPolicyV1Error(
            "dynamic target semantics has no targets"
        )
    if index >= len(raw_rows) or not isinstance(raw_rows[index], Mapping):
        raise Cat2NewFuryParametricPolicyV1Error(
            "target_index is outside dynamic target semantics"
        )
    eligible = sum(
        1
        for row in raw_rows
        if isinstance(row, Mapping)
        and row.get("dead") is not True
        and row.get("attackable") is True
    )
    return index, raw_rows[index], eligible


def _queued_swing(value: Any) -> SwingQueueOp:
    if not isinstance(value, str):
        return SwingQueueOp.KEEP
    normalized = value.strip().upper().replace(" ", "_")
    if normalized in {"HEROIC_STRIKE", "CLEAVE"}:
        return SwingQueueOp(normalized)
    return SwingQueueOp.KEEP


def _weapon_mode(state: Mapping[str, Any]) -> WeaponMode:
    if "oh_swing_remaining_ms" not in state:
        raise Cat2NewFuryParametricPolicyV1Error(
            "live_state does not expose off-hand swing state"
        )
    remaining = state.get("oh_swing_remaining_ms")
    if remaining is None:
        return WeaponMode.TWO_HAND
    _integer(remaining, "live_state.oh_swing_remaining_ms")
    return WeaponMode.DUAL_WIELD


def _operation(
    operation_id: str,
    lane: str,
    intent: str,
    arguments: Mapping[str, Any] | None = None,
) -> JSONMap:
    return {
        "operation_id": operation_id,
        "lane": lane,
        "intent": intent,
        "arguments": dict(arguments or {}),
    }


class Cat2NewFuryParametricPolicyV1:
    """Mask one compact Fury controller against live simulator actions."""

    policy_id = CAT2NEW_POLICY_ID

    def __init__(
        self, config: Cat2NewFuryParametricPolicyConfigV1 | None = None
    ) -> None:
        self.config = config or Cat2NewFuryParametricPolicyConfigV1()
        self._controller = FuryTunedPolicyAdapter(self.config.parameters)

    def decide(self, policy_input: Mapping[str, Any]) -> Cat2NewPolicyIntentV6:
        root = _mapping(policy_input, "policy_input")
        if root.get("schema") != POLICY_INPUT_SCHEMA_V6:
            raise Cat2NewFuryParametricPolicyV1Error("policy input schema mismatch")
        if root.get("policy_id") != self.policy_id:
            raise Cat2NewFuryParametricPolicyV1Error("policy input identity mismatch")
        state = _mapping(root.get("live_state"), "policy_input.live_state")
        actions = _action_table(root.get("available_actions"))
        auras = _aura_rows(state)
        _, selected_target, eligible_targets = _target_rows(state)
        rage = _power(state)

        def availability(action_key: str) -> _ActionAvailability | None:
            ref = ACTION_KEY_TO_REF[action_key]
            return actions.get(_ref_key(ref))

        def ready_in_s(action_key: str) -> float:
            row = availability(action_key)
            return float("inf") if row is None else row.ready_in_ms / 1000.0

        target_dead = selected_target.get("dead") is True
        target_attackable = selected_target.get("attackable") is True
        health_pct = _number(
            state.get("target_health_percent"),
            "live_state.target_health_percent",
        )
        expert_state = FuryExpertState(
            rage=rage,
            target_health_pct=health_pct,
            weapon_mode=_weapon_mode(state),
            target_exists=not target_dead and target_attackable,
            target_is_boss=False,
            in_melee_range=self.config.fixed_melee_environment,
            nearby_enemies=max(1, eligible_targets),
            gcd_ready=_integer(
                state.get("gcd_remaining_ms"),
                "live_state.gcd_remaining_ms",
            )
            == 0,
            current_stance=_stance(auras),
            has_battle_shout=_aura_active(auras, "Battle Shout"),
            battle_shout_remaining_s=next(
                (
                    float(row.get("remaining_ms")) / 1000.0
                    for row in auras
                    if str(row.get("label", "")).casefold() == "battle shout"
                    and isinstance(row.get("remaining_ms"), (int, float))
                    and not isinstance(row.get("remaining_ms"), bool)
                ),
                0.0,
            ),
            flurry_talent=any(
                "flurry" in str(row.get("label", "")).casefold() for row in auras
            ),
            flurry_active=_warrior_flurry_active(auras),
            bloodthirst_known=availability("warrior.bloodthirst") is not None,
            bloodthirst_ready_in_s=ready_in_s("warrior.bloodthirst"),
            whirlwind_ready_in_s=ready_in_s("warrior.whirlwind"),
            bloodrage_ready=ready_in_s("warrior.bloodrage") <= 0.0,
            death_wish_ready=False,
            mainhand_swing_remaining_s=_integer(
                state.get("mh_swing_remaining_ms"),
                "live_state.mh_swing_remaining_ms",
            )
            / 1000.0,
            mainhand_swing_duration_s=_integer(
                state.get("mh_swing_duration_ms"),
                "live_state.mh_swing_duration_ms",
                minimum=1,
            )
            / 1000.0,
            slam_remaining_s=ready_in_s("warrior.slam"),
            execute_cost=self.config.execute_cost,
            heroic_strike_cost=self.config.heroic_strike_cost,
            cleave_cost=self.config.cleave_cost,
            whirlwind_cost=self.config.whirlwind_cost,
            queued_swing=_queued_swing(state.get("queued_swing")),
        )
        proposal = self._controller.propose(expert_state)

        operations: list[JSONMap] = []
        masked: list[str] = []
        queued = proposal.swing_queue
        if queued in (SwingQueueOp.HEROIC_STRIKE, SwingQueueOp.CLEAVE):
            row = actions.get(_ref_key(QUEUE_REFS[queued]))
            if row is not None and row.legal and not row.triggers_gcd:
                operations.append(
                    _operation(
                        f"d{root.get('decision_index')}.queue",
                        "swing_queue",
                        queued.value,
                        {"options": {}},
                    )
                )
            else:
                masked.append(f"swing_queue:{queued.value}")
        elif queued is SwingQueueOp.CANCEL:
            if _queued_swing(state.get("queued_swing")) is not SwingQueueOp.KEEP:
                operations.append(
                    _operation(
                        f"d{root.get('decision_index')}.queue_cancel",
                        "swing_queue",
                        "CANCEL",
                    )
                )

        for ordinal, action_key in enumerate(proposal.off_gcd):
            row = availability(action_key)
            if row is not None and row.legal and not row.triggers_gcd:
                operations.append(
                    _operation(
                        f"d{root.get('decision_index')}.off_gcd.{ordinal}",
                        "off_gcd",
                        "CAST_ACTION",
                        {"action_key": action_key, "options": {}},
                    )
                )
            else:
                masked.append(f"off_gcd:{action_key}")

        if proposal.gcd != WAIT_ACTION:
            row = availability(proposal.gcd)
            if row is not None and row.legal and row.triggers_gcd:
                operations.append(
                    _operation(
                        f"d{root.get('decision_index')}.gcd",
                        "gcd",
                        "CAST_ACTION",
                        {"action_key": proposal.gcd, "options": {}},
                    )
                )
            else:
                masked.append(f"gcd:{proposal.gcd}")

        if proposal.gcd == WAIT_ACTION or not any(
            row["lane"] == "gcd" for row in operations
        ):
            operations.append(
                _operation(
                    f"d{root.get('decision_index')}.wait",
                    "wait",
                    "WAIT",
                    {"wait_ms": proposal.wait_ms or self.config.parameters.wait_ms},
                )
            )

        return Cat2NewPolicyIntentV6(
            operations=tuple(operations),
            policy_metadata={
                "implementation_revision": IMPLEMENTATION_REVISION,
                "controller_policy_id": self._controller.expert_id,
                "parameters": asdict(self.config.parameters),
                "masked_illegal_intents": masked,
                "fixed_melee_environment": self.config.fixed_melee_environment,
                "deployment_allowed": False,
            },
        )


def build_policy() -> Cat2NewFuryParametricPolicyV1:
    """Zero-argument HPC worker factory for the current bounded candidate."""

    return Cat2NewFuryParametricPolicyV1()


def _screening_policy(
    *,
    single_target_priority: str,
    filler: str,
    two_hand_slam_mode: str,
) -> Cat2NewFuryParametricPolicyV1:
    return Cat2NewFuryParametricPolicyV1(
        Cat2NewFuryParametricPolicyConfigV1(
            parameters=FuryPolicyParameters(
                single_target_priority=single_target_priority,
                filler=filler,
                two_hand_slam_mode=two_hand_slam_mode,
                use_death_wish=False,
            )
        )
    )


def build_policy_bt_wait_no_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="BLOODTHIRST_FIRST",
        filler="WAIT",
        two_hand_slam_mode="DISABLED",
    )


def build_policy_bt_hamstring_no_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="BLOODTHIRST_FIRST",
        filler="HAMSTRING",
        two_hand_slam_mode="DISABLED",
    )


def build_policy_ww_wait_no_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="WHIRLWIND_FIRST",
        filler="WAIT",
        two_hand_slam_mode="DISABLED",
    )


def build_policy_ww_hamstring_no_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="WHIRLWIND_FIRST",
        filler="HAMSTRING",
        two_hand_slam_mode="DISABLED",
    )


def build_policy_bt_wait_cat_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="BLOODTHIRST_FIRST",
        filler="WAIT",
        two_hand_slam_mode="CAT_TIMING",
    )


def build_policy_bt_hamstring_cat_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="BLOODTHIRST_FIRST",
        filler="HAMSTRING",
        two_hand_slam_mode="CAT_TIMING",
    )


def build_policy_ww_wait_cat_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="WHIRLWIND_FIRST",
        filler="WAIT",
        two_hand_slam_mode="CAT_TIMING",
    )


def build_policy_ww_hamstring_cat_slam() -> Cat2NewFuryParametricPolicyV1:
    return _screening_policy(
        single_target_priority="WHIRLWIND_FIRST",
        filler="HAMSTRING",
        two_hand_slam_mode="CAT_TIMING",
    )


__all__ = (
    "Cat2NewFuryParametricPolicyConfigV1",
    "Cat2NewFuryParametricPolicyV1",
    "Cat2NewFuryParametricPolicyV1Error",
    "IMPLEMENTATION_REVISION",
    "build_policy",
    "build_policy_bt_hamstring_cat_slam",
    "build_policy_bt_hamstring_no_slam",
    "build_policy_bt_wait_cat_slam",
    "build_policy_bt_wait_no_slam",
    "build_policy_ww_hamstring_cat_slam",
    "build_policy_ww_hamstring_no_slam",
    "build_policy_ww_wait_cat_slam",
    "build_policy_ww_wait_no_slam",
)
