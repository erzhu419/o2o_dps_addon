"""Runtime target gate for one Upper Karazhan local-search unit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .upper_kara_wave_local_search_contract_v1 import (
    ObservedTargetStateV1,
    UpperKaraWaveLocalSearchContractV1,
)


JSONMap = dict[str, Any]

REQUIRED_RETARGET_MODE_V1 = "REQUIRE_EXPLICIT"
REACTIVE_BOSS_STAGE_ID_V1 = "reactive-boss"
REACTIVE_ADDS_STAGE_ID_V1 = "reactive-adds"


@dataclass(frozen=True)
class WaveTargetGateDecisionV1:
    """Stage-local target sets resolved from one current observation."""

    stage_id: str
    direct_target_indexes: tuple[int, ...]
    collateral_target_indexes: tuple[int, ...]

    def to_dict(self) -> JSONMap:
        return {
            "stage_id": self.stage_id,
            "direct_target_indexes": list(self.direct_target_indexes),
            "collateral_target_indexes": list(self.collateral_target_indexes),
            "retarget_mode": REQUIRED_RETARGET_MODE_V1,
        }


@runtime_checkable
class RuntimeTargetGateV1(Protocol):
    """Structural interface shared by staged and reactive runtime gates."""

    def validate_case(self, case: object) -> None: ...

    def evaluate_state(
        self,
        state: Mapping[str, Any],
        *,
        previous_stage_id: str | None = None,
    ) -> WaveTargetGateDecisionV1: ...


class UpperKaraWaveTargetGateV1:
    """Bind the frozen wave target contract to native dynamic-v3 state.

    Runtime target rows remain untouched.  The direct-target allowlist used by
    schedule enumeration and the collateral/AOE allowlist are resolved and
    recorded independently.
    """

    def __init__(
        self,
        contract: UpperKaraWaveLocalSearchContractV1,
        observation_provider: Callable[
            [Mapping[str, Any]], Mapping[str, ObservedTargetStateV1]
        ],
    ) -> None:
        if not isinstance(contract, UpperKaraWaveLocalSearchContractV1):
            raise TypeError(
                "contract must be UpperKaraWaveLocalSearchContractV1"
            )
        self.contract = contract
        if not callable(observation_provider):
            raise TypeError("observation_provider must be callable")
        self._observation_provider = observation_provider
        self._stage_positions = {
            stage.stage_id: index
            for index, stage in enumerate(contract.target_stages)
        }

    def validate_case(self, case: object) -> None:
        """Reject a run case whose engine may auto-retarget after a death."""

        dynamic_load = getattr(case, "dynamic_load", None)
        config = getattr(dynamic_load, "config", None)
        mode = getattr(config, "retarget_mode", None)
        if mode != REQUIRED_RETARGET_MODE_V1:
            raise ValueError(
                "wave target-gated replay requires dynamic_load.config."
                f"retarget_mode={REQUIRED_RETARGET_MODE_V1!r}"
            )

    def evaluate_state(
        self,
        state: Mapping[str, Any],
        *,
        previous_stage_id: str | None = None,
    ) -> WaveTargetGateDecisionV1:
        """Resolve the current stage and legal sets from current observations."""

        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        observations = self._observations(state)
        if previous_stage_id is None:
            stage_position = 0
        else:
            try:
                stage_position = self._stage_positions[previous_stage_id]
            except KeyError as error:
                raise ValueError(
                    f"unknown previous_stage_id: {previous_stage_id}"
                ) from error

        stages = self.contract.target_stages
        while stage_position + 1 < len(stages):
            stage = stages[stage_position]
            if not self.contract.transition_satisfied(stage.stage_id, observations):
                break
            stage_position += 1

        stage_id = stages[stage_position].stage_id
        direct_target_indexes = self.contract.legal_direct_target_indexes(
            stage_id, observations
        )
        if stages[stage_position].direct_target_mode == "FOCUS_ONE_UNTIL_DEAD":
            direct_target_indexes = self._focus_locked_direct_targets(
                state,
                stages[stage_position].direct_target_occurrence_ids,
                observations,
                direct_target_indexes,
            )
        return WaveTargetGateDecisionV1(
            stage_id=stage_id,
            direct_target_indexes=direct_target_indexes,
            collateral_target_indexes=self.contract.legal_collateral_target_indexes(
                stage_id, observations
            ),
        )

    def _focus_locked_direct_targets(
        self,
        state: Mapping[str, Any],
        stage_target_ids: tuple[str, ...],
        observations: Mapping[str, ObservedTargetStateV1],
        currently_legal: tuple[int, ...],
    ) -> tuple[int, ...]:
        """Keep a selected stage target until its death is currently observed.

        The first target remains a search decision: when the selected simulator
        target is outside this stage (or the previous focused target is dead),
        every currently legal member is exposed.  Once a living stage member is
        selected, no different member becomes legal merely because it is also
        visible.  A temporarily unattackable focused target yields no direct
        target rather than silently authorizing a switch.
        """

        selected = state.get("target_index")
        if isinstance(selected, bool) or not isinstance(selected, int):
            raise ValueError(
                "FOCUS_ONE_UNTIL_DEAD requires an integer state.target_index"
            )
        if selected < 0 or selected >= len(self.contract.targets):
            raise ValueError("state.target_index is outside the contract targets")
        target = self.contract.targets[selected]
        if target.occurrence_id not in set(stage_target_ids):
            return currently_legal
        observed = observations[target.occurrence_id]
        if observed.dead:
            return currently_legal
        if observed.visible and observed.attackable:
            return (selected,)
        return ()

    def _observations(
        self, state: Mapping[str, Any]
    ) -> dict[str, ObservedTargetStateV1]:
        raw = self._observation_provider(state)
        if not isinstance(raw, Mapping):
            raise TypeError("observation_provider must return a mapping")
        result = dict(raw)
        expected = {target.occurrence_id for target in self.contract.targets}
        if set(result) != expected or any(
            not isinstance(value, ObservedTargetStateV1)
            for value in result.values()
        ):
            raise ValueError(
                "observation_provider must return current visible/attackable/dead "
                "state for every contract occurrence"
            )
        return result


class ReactiveBossAddsTargetGateV1:
    """Causally gate one boss, priority adds, and nonblocking add roles.

    Unlike a monotone stage graph, this gate has no concept of a first or last
    add wave.  Every decision is recomputed solely from the current runtime
    observations supplied for the boss and the complete declared target universe.
    Historical activity windows, observed death offsets, and future spawn
    schedules are therefore neither accepted nor consulted.

    Selecting an active add establishes the same ``FOCUS_ONE_UNTIL_DEAD``
    behavior as the staged gate.  A focused add that is temporarily not
    attackable yields no direct target until its death is observed; it does not
    silently authorize another add or the boss.  Without an established focus,
    all active adds are exposed so the search can choose one.  The boss is
    legal only when no active priority add or living focused priority add blocks
    it.  Optional-actionable adds may be selected directly but do not block the
    boss.  Collateral-only adds are never direct targets and likewise do not
    block the boss.  The collateral allowlist retains every living declared
    add because delayed Cleave/Sweeping effects can land after a currently
    hidden or unattackable target becomes active; immediate effects still see
    only the runtime's currently attackable set.
    """

    def __init__(
        self,
        boss_target_index: int,
        add_target_indexes: Sequence[int],
        observation_provider: Callable[
            [Mapping[str, Any]], Mapping[int, ObservedTargetStateV1]
        ],
        *,
        collateral_only_target_indexes: Sequence[int] = (),
        optional_actionable_target_indexes: Sequence[int] = (),
        boss_collateral_during_priority_adds: bool = False,
    ) -> None:
        if (
            isinstance(boss_target_index, bool)
            or not isinstance(boss_target_index, int)
            or boss_target_index < 0
        ):
            raise ValueError("boss_target_index must be a nonnegative integer")
        if isinstance(add_target_indexes, (str, bytes)) or not isinstance(
            add_target_indexes, Sequence
        ):
            raise TypeError("add_target_indexes must be a sequence")
        adds = tuple(add_target_indexes)
        if not adds:
            raise ValueError("add_target_indexes must not be empty")
        if any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in adds
        ):
            raise ValueError(
                "add_target_indexes must contain nonnegative integers"
            )
        if len(adds) != len(set(adds)):
            raise ValueError("add_target_indexes must contain unique indexes")
        if boss_target_index in adds:
            raise ValueError("boss target cannot also be an add target")
        if not callable(observation_provider):
            raise TypeError("observation_provider must be callable")

        def optional_indexes(value: Sequence[int], label: str) -> tuple[int, ...]:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"{label} must be a sequence")
            result = tuple(value)
            if any(
                isinstance(index, bool) or not isinstance(index, int) or index < 0
                for index in result
            ):
                raise ValueError(f"{label} must contain nonnegative integers")
            if len(result) != len(set(result)):
                raise ValueError(f"{label} must contain unique indexes")
            return result

        collateral_only = optional_indexes(
            collateral_only_target_indexes, "collateral_only_target_indexes"
        )
        optional_actionable = optional_indexes(
            optional_actionable_target_indexes,
            "optional_actionable_target_indexes",
        )
        declared_groups = (adds, collateral_only, optional_actionable)
        declared_flat = tuple(index for group in declared_groups for index in group)
        if boss_target_index in declared_flat:
            raise ValueError("boss target cannot also be a declared add target")
        if len(declared_flat) != len(set(declared_flat)):
            raise ValueError("declared add target roles must be disjoint")
        if not isinstance(boss_collateral_during_priority_adds, bool):
            raise TypeError("boss_collateral_during_priority_adds must be boolean")

        self.boss_target_index = boss_target_index
        self.add_target_indexes = adds
        self.collateral_only_target_indexes = collateral_only
        self.optional_actionable_target_indexes = optional_actionable
        self.boss_collateral_during_priority_adds = (
            boss_collateral_during_priority_adds
        )
        self._observation_provider = observation_provider

    def validate_case(self, case: object) -> None:
        """Reject a run case whose engine may auto-retarget after a death."""

        dynamic_load = getattr(case, "dynamic_load", None)
        config = getattr(dynamic_load, "config", None)
        mode = getattr(config, "retarget_mode", None)
        if mode != REQUIRED_RETARGET_MODE_V1:
            raise ValueError(
                "reactive boss/add target-gated replay requires "
                "dynamic_load.config."
                f"retarget_mode={REQUIRED_RETARGET_MODE_V1!r}"
            )

    def evaluate_state(
        self,
        state: Mapping[str, Any],
        *,
        previous_stage_id: str | None = None,
    ) -> WaveTargetGateDecisionV1:
        """Resolve boss/add legality from only the current observation."""

        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        if previous_stage_id not in {
            None,
            REACTIVE_BOSS_STAGE_ID_V1,
            REACTIVE_ADDS_STAGE_ID_V1,
        }:
            raise ValueError(f"unknown previous_stage_id: {previous_stage_id}")

        observations = self._observations(state)
        active_adds = tuple(
            index
            for index in self.add_target_indexes
            if self._is_active(observations[index])
        )
        active_optional_actionable = tuple(
            index
            for index in self.optional_actionable_target_indexes
            if self._is_active(observations[index])
        )
        living_add_collateral = tuple(
            index
            for index in (
                *self.add_target_indexes,
                *self.optional_actionable_target_indexes,
                *self.collateral_only_target_indexes,
            )
            if self._is_living(observations[index])
        )
        selected = state.get("target_index")
        selected_add = (
            selected
            if type(selected) is int and selected in self.add_target_indexes
            else None
        )
        selected_add_is_alive = bool(
            selected_add is not None and not observations[selected_add].dead
        )
        focus_is_established = bool(
            selected_add_is_alive
            and (
                previous_stage_id == REACTIVE_ADDS_STAGE_ID_V1
                or self._is_active(observations[selected_add])
            )
        )
        if active_adds or focus_is_established:
            if focus_is_established:
                direct = (
                    (selected_add,)
                    if self._is_active(observations[selected_add])
                    else ()
                )
            else:
                direct = active_adds
            boss_collateral = (
                (self.boss_target_index,)
                if self.boss_collateral_during_priority_adds
                and self._is_living(observations[self.boss_target_index])
                else ()
            )
            return WaveTargetGateDecisionV1(
                stage_id=REACTIVE_ADDS_STAGE_ID_V1,
                direct_target_indexes=direct,
                collateral_target_indexes=(*living_add_collateral, *boss_collateral),
            )

        boss_active = self._is_active(observations[self.boss_target_index])
        boss_targets = (self.boss_target_index,) if boss_active else ()
        direct_targets = (*boss_targets, *active_optional_actionable)
        living_boss = (
            (self.boss_target_index,)
            if self._is_living(observations[self.boss_target_index])
            else ()
        )
        return WaveTargetGateDecisionV1(
            stage_id=REACTIVE_BOSS_STAGE_ID_V1,
            direct_target_indexes=direct_targets,
            collateral_target_indexes=(*living_boss, *living_add_collateral),
        )

    @staticmethod
    def _is_active(observation: ObservedTargetStateV1) -> bool:
        return (
            observation.visible
            and observation.attackable
            and not observation.dead
        )

    @staticmethod
    def _is_living(observation: ObservedTargetStateV1) -> bool:
        return not observation.dead

    def _observations(
        self, state: Mapping[str, Any]
    ) -> dict[int, ObservedTargetStateV1]:
        raw = self._observation_provider(state)
        if not isinstance(raw, Mapping):
            raise TypeError("observation_provider must return a mapping")
        result = dict(raw)
        expected = {
            self.boss_target_index,
            *self.add_target_indexes,
            *self.optional_actionable_target_indexes,
            *self.collateral_only_target_indexes,
        }
        if set(result) != expected or any(
            not isinstance(value, ObservedTargetStateV1)
            for value in result.values()
        ):
            raise ValueError(
                "observation_provider must return current visible/attackable/dead "
                "state for exactly the boss and every declared add target index"
            )
        return result


__all__ = (
    "REACTIVE_ADDS_STAGE_ID_V1",
    "REACTIVE_BOSS_STAGE_ID_V1",
    "REQUIRED_RETARGET_MODE_V1",
    "ReactiveBossAddsTargetGateV1",
    "RuntimeTargetGateV1",
    "UpperKaraWaveTargetGateV1",
    "WaveTargetGateDecisionV1",
)
