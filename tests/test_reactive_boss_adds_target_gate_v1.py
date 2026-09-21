from __future__ import annotations

from types import SimpleNamespace

import pytest

from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    ObservedTargetStateV1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    REACTIVE_ADDS_STAGE_ID_V1,
    REACTIVE_BOSS_STAGE_ID_V1,
    REQUIRED_RETARGET_MODE_V1,
    ReactiveBossAddsTargetGateV1,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    NativeDynamicV3ScheduleReplayV1,
    ReplayStatusV1,
)


def _observed(*, visible: bool, attackable: bool, dead: bool = False):
    return ObservedTargetStateV1(visible, attackable, dead)


def _state(
    observations: dict[int, ObservedTargetStateV1],
    *,
    selected: int,
) -> dict:
    return {
        "time_ms": 0,
        "damage_done": 0.0,
        "needs_input": True,
        "finished": False,
        "target_index": selected,
        "boss_add_observations": observations,
        "dynamic_target_semantics": {
            "targets": [
                {
                    "target_index": index,
                    "dead": observation.dead,
                    "attackable": observation.attackable,
                }
                for index, observation in observations.items()
            ]
        },
    }


def _gate() -> ReactiveBossAddsTargetGateV1:
    return ReactiveBossAddsTargetGateV1(
        boss_target_index=0,
        add_target_indexes=(1, 2, 3),
        observation_provider=lambda state: state["boss_add_observations"],
    )


def test_reactive_gate_returns_to_boss_then_reenters_a_second_add_wave() -> None:
    gate = _gate()
    observations = {
        0: _observed(visible=True, attackable=True),
        1: _observed(visible=False, attackable=False),
        2: _observed(visible=False, attackable=False),
        3: _observed(visible=False, attackable=False),
    }

    boss = gate.evaluate_state(_state(observations, selected=0))
    assert boss.stage_id == REACTIVE_BOSS_STAGE_ID_V1
    assert boss.direct_target_indexes == (0,)
    assert boss.collateral_target_indexes == (0, 1, 2, 3)

    # The boss remains attackable, but any active add preempts it for both
    # direct and collateral permission.
    observations[1] = _observed(visible=True, attackable=True)
    observations[2] = _observed(visible=True, attackable=True)
    first_wave = gate.evaluate_state(
        _state(observations, selected=0),
        previous_stage_id=boss.stage_id,
    )
    assert first_wave.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert first_wave.direct_target_indexes == (1, 2)
    assert first_wave.collateral_target_indexes == (1, 2, 3)
    assert 0 not in first_wave.collateral_target_indexes

    focused = gate.evaluate_state(
        _state(observations, selected=2),
        previous_stage_id=first_wave.stage_id,
    )
    assert focused.direct_target_indexes == (2,)
    assert focused.collateral_target_indexes == (1, 2, 3)

    observations[1] = _observed(visible=True, attackable=False, dead=True)
    observations[2] = _observed(visible=True, attackable=False, dead=True)
    back_to_boss = gate.evaluate_state(
        _state(observations, selected=2),
        previous_stage_id=focused.stage_id,
    )
    assert back_to_boss.stage_id == REACTIVE_BOSS_STAGE_ID_V1
    assert back_to_boss.direct_target_indexes == (0,)

    # The same declared slots may become a later wave.  No monotone stage
    # cursor or historical death observation prevents re-entry.
    observations[2] = _observed(visible=True, attackable=True)
    observations[3] = _observed(visible=True, attackable=True)
    second_wave = gate.evaluate_state(
        _state(observations, selected=0),
        previous_stage_id=back_to_boss.stage_id,
    )
    assert second_wave.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert second_wave.direct_target_indexes == (2, 3)
    assert second_wave.collateral_target_indexes == (2, 3)
    assert 0 not in second_wave.collateral_target_indexes


def test_reactive_gate_ignores_history_and_requires_complete_current_rows() -> None:
    gate = _gate()
    observations = {
        0: _observed(visible=True, attackable=True),
        1: _observed(visible=False, attackable=False),
        2: _observed(visible=True, attackable=True),
        3: _observed(visible=False, attackable=False),
    }
    state = _state(observations, selected=0)
    state["historical_activity_offsets"] = {
        "future_spawn_ms": -1,
        "observed_death_ms": -1,
    }
    assert gate.evaluate_state(state).direct_target_indexes == (2,)

    missing = dict(observations)
    missing.pop(3)
    with pytest.raises(ValueError, match="exactly the boss and every declared add"):
        gate.evaluate_state(_state(missing, selected=0))


def test_reactive_gate_keeps_a_selected_living_add_until_observed_dead() -> None:
    gate = _gate()
    observations = {
        0: _observed(visible=True, attackable=True),
        1: _observed(visible=True, attackable=True),
        2: _observed(visible=True, attackable=True),
        3: _observed(visible=False, attackable=False),
    }
    focused = gate.evaluate_state(_state(observations, selected=1))
    assert focused.direct_target_indexes == (1,)

    # Temporary untargetability does not silently authorize a switch to add 2
    # or back to the boss.  Only a current death observation releases focus.
    observations[1] = _observed(visible=True, attackable=False, dead=False)
    waiting = gate.evaluate_state(
        _state(observations, selected=1),
        previous_stage_id=focused.stage_id,
    )
    assert waiting.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert waiting.direct_target_indexes == ()
    assert waiting.collateral_target_indexes == (1, 2, 3)

    observations[1] = _observed(visible=True, attackable=False, dead=True)
    released = gate.evaluate_state(
        _state(observations, selected=1),
        previous_stage_id=waiting.stage_id,
    )
    assert released.direct_target_indexes == (2,)


def test_reactive_gate_separates_priority_collateral_only_and_optional_adds() -> None:
    gate = ReactiveBossAddsTargetGateV1(
        boss_target_index=0,
        add_target_indexes=(1,),
        observation_provider=lambda state: state["boss_add_observations"],
        collateral_only_target_indexes=(2,),
        optional_actionable_target_indexes=(3,),
    )
    observations = {
        0: _observed(visible=True, attackable=True),
        1: _observed(visible=False, attackable=False, dead=True),
        2: _observed(visible=True, attackable=True),
        3: _observed(visible=False, attackable=False, dead=True),
    }

    # A whelp may receive collateral, but it is never a direct target and does
    # not prevent direct boss damage.
    boss_and_whelp = gate.evaluate_state(_state(observations, selected=0))
    assert boss_and_whelp.stage_id == REACTIVE_BOSS_STAGE_ID_V1
    assert boss_and_whelp.direct_target_indexes == (0,)
    assert boss_and_whelp.collateral_target_indexes == (0, 2)

    # A not-yet-attackable but living declared add remains in the potential
    # collateral mask needed by delayed Cleave/Sweeping effects.  It still does
    # not block direct boss damage until it is currently active.
    observations[1] = _observed(visible=False, attackable=False, dead=False)
    future_seeker = gate.evaluate_state(_state(observations, selected=0))
    assert future_seeker.direct_target_indexes == (0,)
    assert future_seeker.collateral_target_indexes == (0, 1, 2)

    # A Ley-Seeker is priority.  It removes the boss from both permission sets,
    # while the active whelp remains collateral only.
    observations[1] = _observed(visible=True, attackable=True)
    seeker_and_whelp = gate.evaluate_state(
        _state(observations, selected=0),
        previous_stage_id=boss_and_whelp.stage_id,
    )
    assert seeker_and_whelp.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert seeker_and_whelp.direct_target_indexes == (1,)
    assert seeker_and_whelp.collateral_target_indexes == (1, 2)
    assert 0 not in seeker_and_whelp.collateral_target_indexes

    # A focused but temporarily untargetable living seeker continues to block
    # boss/direct switching.  The whelp remains a legal collateral recipient.
    observations[1] = _observed(visible=True, attackable=False, dead=False)
    waiting = gate.evaluate_state(
        _state(observations, selected=1),
        previous_stage_id=seeker_and_whelp.stage_id,
    )
    assert waiting.direct_target_indexes == ()
    assert waiting.collateral_target_indexes == (1, 2)

    # A school-compatible Affinity is directly actionable without becoming a
    # boss blocker; if a priority add appears, it remains collateral only for
    # that priority phase.
    observations[1] = _observed(visible=False, attackable=False, dead=True)
    observations[3] = _observed(visible=True, attackable=True)
    affinity_phase = gate.evaluate_state(
        _state(observations, selected=0),
        previous_stage_id=waiting.stage_id,
    )
    assert affinity_phase.stage_id == REACTIVE_BOSS_STAGE_ID_V1
    assert affinity_phase.direct_target_indexes == (0, 3)
    assert affinity_phase.collateral_target_indexes == (0, 3, 2)

    observations[1] = _observed(visible=True, attackable=True, dead=False)
    priority_again = gate.evaluate_state(
        _state(observations, selected=0),
        previous_stage_id=affinity_phase.stage_id,
    )
    assert priority_again.direct_target_indexes == (1,)
    assert priority_again.collateral_target_indexes == (1, 3, 2)

    boss_collateral_gate = ReactiveBossAddsTargetGateV1(
        boss_target_index=0,
        add_target_indexes=(1,),
        observation_provider=lambda state: state["boss_add_observations"],
        collateral_only_target_indexes=(2,),
        optional_actionable_target_indexes=(3,),
        boss_collateral_during_priority_adds=True,
    )
    with_boss_collateral = boss_collateral_gate.evaluate_state(
        _state(observations, selected=0)
    )
    assert with_boss_collateral.direct_target_indexes == (1,)
    assert with_boss_collateral.collateral_target_indexes == (1, 3, 2, 0)


def test_native_replay_accepts_reactive_gate_and_still_requires_explicit_retarget() -> None:
    gate = _gate()
    observations = {
        0: _observed(visible=True, attackable=True),
        1: _observed(visible=True, attackable=True),
        2: _observed(visible=False, attackable=False),
        3: _observed(visible=False, attackable=False),
    }
    state = _state(observations, selected=0)
    action = ActionRef(spell_id=1001)

    class LoadOnlyBridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def load_dynamic_v3(self, request, seed, config):
            return SimpleNamespace(state=state)

        def actions(self):
            return (AvailableAction(0, action, "hit", True, 0, True),)

    def case(mode: str):
        return SimpleNamespace(
            request={},
            dynamic_load=SimpleNamespace(config=SimpleNamespace(retarget_mode=mode)),
        )

    rejected = NativeDynamicV3ScheduleReplayV1(
        LoadOnlyBridge,
        lambda seed: case("NEXT_ALIVE_CYCLIC"),
        target_gate=gate,
    ).replay(1, ())
    assert rejected.status is ReplayStatusV1.INVALID
    assert REQUIRED_RETARGET_MODE_V1 in rejected.invalid_reason

    accepted = NativeDynamicV3ScheduleReplayV1(
        LoadOnlyBridge,
        lambda seed: case(REQUIRED_RETARGET_MODE_V1),
        target_gate=gate,
    ).replay(1, ())
    assert accepted.status is ReplayStatusV1.FRONTIER
    assert accepted.target_gate is not None
    assert accepted.target_gate.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert accepted.target_gate.direct_target_indexes == (1,)
    assert accepted.target_gate.collateral_target_indexes == (1, 2, 3)
