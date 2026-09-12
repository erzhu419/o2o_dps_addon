"""Executable 13-axis Cat-gap Fury candidate for the dynamic simulator.

The search plan owns the finite candidate design.  This module owns the exact
runtime meaning of every parameter in that design and rejects any silent axis
drop.  It remains a development-only simulator policy: the fixed-melee state
mapping and a successful offline rollout do not establish Cat2 client fidelity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .cat2new_candidate_feedback_loop_v6 import Cat2NewPolicyIntentV6
from .cat2new_fury_parametric_policy_v1 import (
    Cat2NewFuryParametricPolicyConfigV1,
    Cat2NewFuryParametricPolicyV1,
)
from .expert_policy import (
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    SwingQueueOp,
    WAIT_ACTION,
)
from .fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    SLAM,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
)
from .fury_paired_multiseed_runner_v4 import sha256_json
from .fury_policy_optimization_v1 import FuryTunedPolicyAdapter


JSONMap = dict[str, Any]
IMPLEMENTATION_REVISION = "v1_exact_13_axis_cat_gap_development_policy"
PARAMETER_SCHEMA = "fury_cat_gap_policy_parameters/v1"

PARAMETER_AXES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("heroic_strike_base_rage", tuple(range(35, 76, 5))),
    ("cleave_base_rage", tuple(range(35, 76, 5))),
    ("queue_cancel_margin_rage", tuple(range(4, 17, 2))),
    ("primary_cooldown_reserve_window_ms", tuple(range(800, 1801, 200))),
    ("bloodrage_trigger_below_rage", tuple(range(15, 36, 5))),
    ("single_target_priority", ("BLOODTHIRST_FIRST", "WHIRLWIND_FIRST")),
    ("multi_target_priority", ("BLOODTHIRST_FIRST", "WHIRLWIND_FIRST")),
    ("hamstring_min_rage", tuple(range(10, 71, 10))),
    ("hamstring_min_primary_gap_ms", tuple(range(1000, 2001, 200))),
    ("slam_min_swing_remaining_ms", tuple(range(1600, 2601, 200))),
    ("slam_min_primary_gap_ms", tuple(range(800, 1801, 200))),
    ("execute_reserve_rage", tuple(range(0, 41, 10))),
    ("wait_ms", (50, 75, 100, 125, 150)),
)


class Cat2NewFuryCatGapPolicyV1Error(RuntimeError):
    """A candidate identity or one of its 13 runtime axes is invalid."""


def _validate_axis_value(name: str, value: Any, allowed: tuple[Any, ...]) -> Any:
    expected_type = type(allowed[0])
    if type(value) is not expected_type or value not in allowed:
        raise Cat2NewFuryCatGapPolicyV1Error(
            f"candidate parameter {name} is outside the frozen axis"
        )
    return value


@dataclass(frozen=True)
class FuryCatGapPolicyParametersV1:
    heroic_strike_base_rage: int
    cleave_base_rage: int
    queue_cancel_margin_rage: int
    primary_cooldown_reserve_window_ms: int
    bloodrage_trigger_below_rage: int
    single_target_priority: str
    multi_target_priority: str
    hamstring_min_rage: int
    hamstring_min_primary_gap_ms: int
    slam_min_swing_remaining_ms: int
    slam_min_primary_gap_ms: int
    execute_reserve_rage: int
    wait_ms: int

    def __post_init__(self) -> None:
        for name, allowed in PARAMETER_AXES:
            _validate_axis_value(name, getattr(self, name), allowed)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any]
    ) -> "FuryCatGapPolicyParametersV1":
        if not isinstance(value, Mapping):
            raise Cat2NewFuryCatGapPolicyV1Error(
                "candidate parameters must be an object"
            )
        expected = {name for name, _ in PARAMETER_AXES}
        if set(value) != expected:
            raise Cat2NewFuryCatGapPolicyV1Error(
                "candidate parameter field set differs from the exact 13-axis contract"
            )
        return cls(**{name: value[name] for name, _ in PARAMETER_AXES})

    @property
    def parameter_sha256(self) -> str:
        return sha256_json(asdict(self))

    @property
    def candidate_id(self) -> str:
        return f"cat-gap-v1-{self.parameter_sha256[:16]}"

    @property
    def policy_id(self) -> str:
        return self.candidate_id

    # Compatibility properties used only by the inherited proposal envelope.
    @property
    def bloodrage_below(self) -> float:
        return float(self.bloodrage_trigger_below_rage)

    @property
    def use_death_wish(self) -> bool:
        return False


class FuryCatGapControllerV1(FuryTunedPolicyAdapter):
    """Give each frozen search axis an explicit, reachable decision guard."""

    def __init__(self, parameters: FuryCatGapPolicyParametersV1) -> None:
        self.parameters = parameters
        self.expert_id = parameters.candidate_id

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(str(Path(__file__).resolve()),),
            source_refs=(
                "FuryCatGapControllerV1._queue_operation",
                "FuryCatGapControllerV1._gcd_action",
            ),
        )

    def _queue_operation(self, state: FuryExpertState) -> SwingQueueOp:
        params = self.parameters
        desired = (
            SwingQueueOp.CLEAVE
            if state.nearby_enemies > 1
            else SwingQueueOp.HEROIC_STRIKE
        )
        base_threshold = float(
            params.cleave_base_rage
            if desired is SwingQueueOp.CLEAVE
            else params.heroic_strike_base_rage
        )
        reserve = base_threshold
        reserve_window_s = params.primary_cooldown_reserve_window_ms / 1000.0
        if state.bloodthirst_ready_in_s <= reserve_window_s:
            reserve = max(reserve, 30.0 + self._queue_cost(state, desired))
        if state.whirlwind_ready_in_s <= reserve_window_s:
            reserve = max(
                reserve,
                state.whirlwind_cost + self._queue_cost(state, desired),
            )

        cancel_threshold = max(
            self._queue_cost(state, desired),
            reserve - float(params.queue_cancel_margin_rage),
        )
        if state.queued_swing is not SwingQueueOp.KEEP:
            if state.queued_swing is not desired:
                return SwingQueueOp.CANCEL
            if state.target_health_pct < 20.0 or state.rage < cancel_threshold:
                return SwingQueueOp.CANCEL
            return SwingQueueOp.KEEP
        if state.target_health_pct < 20.0:
            return SwingQueueOp.KEEP
        if state.rage >= reserve:
            return desired
        return SwingQueueOp.KEEP

    def _gcd_action(self, state: FuryExpertState) -> str:
        params = self.parameters
        bt_ready = (
            state.bloodthirst_known
            and state.bloodthirst_ready_in_s <= 0.0
            and state.rage >= 30.0
        )
        ww_ready = (
            state.whirlwind_ready_in_s <= 0.0
            and state.rage >= state.whirlwind_cost
            and state.current_stance.value == "BERSERKER"
        )

        if state.target_health_pct < 20.0:
            if (
                bt_ready
                and state.rage
                >= 30.0 + float(params.execute_reserve_rage)
            ):
                return BLOODTHIRST
            if state.rage >= state.execute_cost:
                return EXECUTE
            if bt_ready:
                return BLOODTHIRST
            return WAIT_ACTION

        slam_primary_gap_s = params.slam_min_primary_gap_ms / 1000.0
        if (
            state.weapon_mode is WeaponMode.TWO_HAND
            and (not state.flurry_talent or state.flurry_active)
            and state.mainhand_swing_remaining_s
            >= params.slam_min_swing_remaining_ms / 1000.0
            and state.bloodthirst_ready_in_s > slam_primary_gap_s
            and state.whirlwind_ready_in_s > slam_primary_gap_s
            and state.slam_remaining_s <= 0.0
            and state.rage >= 15.0
        ):
            return SLAM

        priority = (
            params.multi_target_priority
            if state.nearby_enemies > 1
            else params.single_target_priority
        )
        if priority == "WHIRLWIND_FIRST":
            if ww_ready:
                return WHIRLWIND
            if bt_ready:
                return BLOODTHIRST
        else:
            if bt_ready:
                return BLOODTHIRST
            if ww_ready:
                return WHIRLWIND

        filler_gap_s = params.hamstring_min_primary_gap_ms / 1000.0
        if (
            state.rage >= float(params.hamstring_min_rage)
            and state.bloodthirst_ready_in_s > filler_gap_s
            and state.whirlwind_ready_in_s > filler_gap_s
        ):
            return HAMSTRING
        return WAIT_ACTION


class Cat2NewFuryCatGapPolicyV1(Cat2NewFuryParametricPolicyV1):
    """Translate the exact Cat-gap controller through the existing Cat2 lanes."""

    def __init__(
        self,
        candidate_id: str,
        parameters: FuryCatGapPolicyParametersV1,
    ) -> None:
        if candidate_id != parameters.candidate_id:
            raise Cat2NewFuryCatGapPolicyV1Error(
                "candidate_id differs from the canonical 13-axis parameter identity"
            )
        super().__init__(
            Cat2NewFuryParametricPolicyConfigV1(parameters=parameters)  # type: ignore[arg-type]
        )
        self.candidate_id = candidate_id
        self.policy_id = candidate_id
        self.parameter_sha256 = parameters.parameter_sha256
        self._controller = FuryCatGapControllerV1(parameters)

    def decide(self, policy_input: Mapping[str, Any]) -> Cat2NewPolicyIntentV6:
        expected_profile = asdict(self.config)
        observed_profile = policy_input.get("optimizer_parameters")
        if (
            not isinstance(observed_profile, Mapping)
            or sha256_json(dict(observed_profile)) != sha256_json(expected_profile)
        ):
            raise Cat2NewFuryCatGapPolicyV1Error(
                "policy_input optimizer_parameters differ from the executing "
                "13-axis candidate profile"
            )
        inherited = super().decide(policy_input)
        metadata = dict(inherited.policy_metadata)
        metadata.update(
            {
                "implementation_revision": IMPLEMENTATION_REVISION,
                "candidate_id": self.candidate_id,
                "parameter_schema": PARAMETER_SCHEMA,
                "parameter_sha256": self.parameter_sha256,
                "all_13_axes_executed_by": "FuryCatGapControllerV1",
                "simulator_only": True,
                "development_only": True,
                "deployment_allowed": False,
            }
        )
        return Cat2NewPolicyIntentV6(
            operations=inherited.operations,
            policy_metadata=metadata,
        )


def build_cat_gap_policy_v1(
    candidate_id: str,
    exact_13d_parameters: Mapping[str, Any],
) -> Cat2NewFuryCatGapPolicyV1:
    """Build one source-bound search candidate; no environment lookup is used."""

    parameters = FuryCatGapPolicyParametersV1.from_mapping(exact_13d_parameters)
    return Cat2NewFuryCatGapPolicyV1(candidate_id, parameters)


__all__ = (
    "Cat2NewFuryCatGapPolicyV1",
    "Cat2NewFuryCatGapPolicyV1Error",
    "FuryCatGapControllerV1",
    "FuryCatGapPolicyParametersV1",
    "IMPLEMENTATION_REVISION",
    "PARAMETER_AXES",
    "PARAMETER_SCHEMA",
    "build_cat_gap_policy_v1",
)
