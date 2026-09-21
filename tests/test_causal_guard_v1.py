from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.causal_guard_v1 import (
    GuardObservationError,
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
    next_observable_guard_check_ms_v1,
    observable_causal_guard_from_dict_v1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction


ACTION = ActionRef(spell_id=23894)
AURA = ActionRef(spell_id=12328)
SUNDER = ActionRef(spell_id=11597)


def _available(
    *, ready_in_ms: int, legal: bool = True
) -> tuple[AvailableAction, ...]:
    return (
        AvailableAction(
            index=0,
            action=ACTION,
            label="Bloodthirst",
            legal=legal,
            ready_in_ms=ready_in_ms,
            triggers_gcd=True,
        ),
    )


def _state() -> dict[str, object]:
    return {
        "time_ms": 4_500,
        "precombat": {"pull_time_ms": 3_000, "relative_time_ms": 1_500},
        "power": {"type": "rage", "current": 62.0, "maximum": 100.0},
        "mh_swing_remaining_ms": 420,
        "swing_queue": {"kind": "HEROIC_STRIKE", "status": "PENDING"},
        "target_index": 0,
        "target_auras": [],
        "auras": [
            {
                "action": AURA.to_wire(),
                "label": "Death Wish",
                "remaining_ms": 850,
            }
        ],
        "dynamic_target_semantics": {
            "targets": [
                {
                    "target_index": 0,
                    "maximum_health": 1_000.0,
                    "current_health": 240.0,
                    "dead": False,
                    "attackable": True,
                },
                {
                    "target_index": 1,
                    "maximum_health": 500.0,
                    "current_health": 0.0,
                    "dead": True,
                    "attackable": False,
                },
            ]
        },
    }


def test_closed_contract_supports_every_requested_current_observation() -> None:
    guard = ObservableCausalGuardV1(
        pull_relative_time_gte_ms=1_000,
        rage_gte=60,
        rage_lte=65,
        target_index=0,
        target_hp_pct_lte=25,
        live_target_count_gte=1,
        live_target_count_lte=1,
        attackable_target_count_gte=1,
        attackable_target_count_lte=1,
        mh_swing_remaining_lte_ms=500,
        queue_status_is="PENDING",
        aura_action=AURA,
        aura_remaining_gte_ms=800,
        aura_remaining_lte_ms=900,
        action_ready=ACTION,
    )

    result = evaluate_observable_guard_v1(guard, _state(), _available(ready_in_ms=0))

    assert result.satisfied
    assert result.failed_predicates == ()
    assert result.observed == {
        "pull_relative_time_ms": 1_500,
        "rage": 62.0,
        "target_hp_pct": 24.0,
        "live_target_count": 1,
        "attackable_target_count": 1,
        "mh_swing_remaining_ms": 420,
        "queue_status": "PENDING",
        "aura_remaining_ms": 850,
        "action_ready_in_ms": 0,
        "action_legal": True,
    }


def test_each_predicate_reports_false_without_reading_a_future_suffix() -> None:
    state = _state()
    state["precombat"]["relative_time_ms"] = -2_000
    state["power"]["current"] = 10.0
    state["mh_swing_remaining_ms"] = 900
    state["swing_queue"]["status"] = "NONE"
    state["auras"][0]["remaining_ms"] = 1_500
    state["dynamic_target_semantics"]["targets"][0]["current_health"] = 800.0
    state["dynamic_target_semantics"]["targets"][1].update(
        {"current_health": 500.0, "dead": False, "attackable": True}
    )
    guard = ObservableCausalGuardV1(
        pull_relative_time_gte_ms=0,
        rage_gte=50,
        target_index=0,
        target_hp_pct_lte=20,
        live_target_count_lte=1,
        mh_swing_remaining_lte_ms=500,
        queue_status_is="ACTIVE",
        aura_action=AURA,
        aura_remaining_lte_ms=1_000,
        action_ready=ACTION,
    )

    result = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=700)
    )

    assert set(result.failed_predicates) == {
        "pull_relative_time_gte_ms",
        "rage_gte",
        "target_hp_pct_lte",
        "live_target_count_lte",
        "mh_swing_remaining_lte_ms",
        "queue_status_is",
        "aura_remaining_lte_ms",
        "action_ready",
    }


def test_future_team_and_terminal_fields_cannot_change_guard_result() -> None:
    guard = ObservableCausalGuardV1(
        rage_gte=60,
        target_index=0,
        target_hp_pct_lte=25,
        live_target_count_gte=1,
    )
    state = _state()
    polluted = deepcopy(state)
    polluted["future_team_events"] = [
        {"time_ms": 4_501, "target_index": 0, "damage": 999_999}
    ]
    polluted["terminal_death_time_ms"] = 4_501
    polluted["dynamic_target_semantics"]["targets"][0][
        "death_time_ms"
    ] = 4_501
    polluted["dynamic_team_background"] = {
        "background_events_total": 999,
        "future_events": [{"damage": 999_999}],
    }

    clean = evaluate_observable_guard_v1(guard, state, _available(ready_in_ms=0))
    with_future = evaluate_observable_guard_v1(
        guard, polluted, _available(ready_in_ms=0)
    )

    assert with_future == clean


def test_attackable_count_excludes_alive_future_wave_targets_and_round_trips() -> None:
    state = _state()
    state["dynamic_target_semantics"]["targets"][1].update(
        {"current_health": 500.0, "dead": False, "attackable": False}
    )
    guard = ObservableCausalGuardV1(
        live_target_count_gte=2,
        attackable_target_count_gte=1,
        attackable_target_count_lte=1,
        false_semantics=SKIP_PLAN,
    )

    result = evaluate_observable_guard_v1(guard, state, ())

    assert result.satisfied
    assert result.observed == {
        "live_target_count": 2,
        "attackable_target_count": 1,
    }
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard

    state["dynamic_target_semantics"]["targets"][1]["attackable"] = True
    two_attackable = evaluate_observable_guard_v1(guard, state, ())
    assert two_attackable.failed_predicates == (
        "attackable_target_count_lte",
    )


def test_missing_requested_observation_is_invalid_not_an_infinite_wait() -> None:
    with pytest.raises(GuardObservationError, match="power"):
        evaluate_observable_guard_v1(
            ObservableCausalGuardV1(rage_gte=20),
            {"time_ms": 0},
            (),
        )


def test_guard_wire_round_trip_and_closed_predicate_contract() -> None:
    guard = ObservableCausalGuardV1(
        rage_gte=30,
        rage_lte=70,
        target_index=0,
        target_hp_pct_lte=20,
        action_ready=ACTION,
        timeout_ms=2_000,
        check_interval_ms=50,
    )
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard
    invalid = guard.to_dict()
    invalid["all_of"]["future_death_time_lte_ms"] = 500
    with pytest.raises(ValueError, match="unsupported predicates"):
        observable_causal_guard_from_dict_v1(invalid)


def test_rage_upper_bound_is_symmetric_causal_and_round_trips() -> None:
    guard = ObservableCausalGuardV1(
        rage_gte=20,
        rage_lte=40,
        false_semantics=SKIP_PLAN,
    )
    state = _state()

    state["power"]["current"] = 19.0
    low = evaluate_observable_guard_v1(guard, state, ())
    assert low.failed_predicates == ("rage_gte",)

    state["power"]["current"] = 40.0
    assert evaluate_observable_guard_v1(guard, state, ()).satisfied

    state["power"]["current"] = 41.0
    high = evaluate_observable_guard_v1(guard, state, ())
    assert high.failed_predicates == ("rage_lte",)
    assert next_observable_guard_check_ms_v1(guard, high, ()) == 100
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard


def test_rage_window_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="rage_gte cannot exceed rage_lte"):
        ObservableCausalGuardV1(rage_gte=60, rage_lte=40)


def test_pull_relative_time_window_is_causal_strict_and_round_trips() -> None:
    guard = ObservableCausalGuardV1(
        pull_relative_time_gte_ms=-2_500,
        pull_relative_time_lte_ms=-1,
        false_semantics=SKIP_PLAN,
    )
    state = _state()

    state["precombat"]["relative_time_ms"] = -2_600
    early = evaluate_observable_guard_v1(guard, state, ())
    assert early.failed_predicates == ("pull_relative_time_gte_ms",)
    assert next_observable_guard_check_ms_v1(guard, early, ()) == 100

    state["precombat"]["relative_time_ms"] = -2_500
    assert evaluate_observable_guard_v1(guard, state, ()).satisfied

    state["precombat"]["relative_time_ms"] = -1
    assert evaluate_observable_guard_v1(guard, state, ()).satisfied

    state["precombat"]["relative_time_ms"] = 0
    missed = evaluate_observable_guard_v1(guard, state, ())
    assert missed.failed_predicates == ("pull_relative_time_lte_ms",)
    assert next_observable_guard_check_ms_v1(guard, missed, ()) == 100
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard


def test_pull_relative_time_window_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        ObservableCausalGuardV1(
            pull_relative_time_gte_ms=-1,
            pull_relative_time_lte_ms=-2_500,
        )


def test_arrival_reschedule_guard_uses_only_current_hp_and_attackability() -> None:
    guard = ObservableCausalGuardV1(
        target_index=0,
        target_hp_pct_gte=35,
        target_attackable_is=True,
        action_ready=ACTION,
        false_semantics=SKIP_PLAN,
    )
    healthy = evaluate_observable_guard_v1(
        guard, _state(), _available(ready_in_ms=0)
    )
    assert not healthy.satisfied
    assert healthy.failed_predicates == ("target_hp_pct_gte",)
    assert healthy.observed["target_hp_pct"] == 24.0
    assert healthy.observed["target_attackable"] is True
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard


def test_late_arrival_remaining_time_uses_current_hp_and_prefix_damage_only() -> None:
    state = _state()
    state["dynamic_team_background"] = {
        "schema": "o2o_policy_dynamic_team_prefix_view/v1",
        "prefix_damage_rate": {
            "schema": "o2o_policy_prefix_damage_rate/v1",
            "combined_damage_per_second": 2_000.0,
        },
        "targets": [
            {
                "target_index": 0,
                "current_health": 6_000.0,
                "dead": False,
            },
            {
                "target_index": 1,
                "current_health": 50_000.0,
                "dead": False,
            },
        ],
    }
    state["dynamic_target_semantics"]["targets"] = [
        {
            "target_index": 0,
            "maximum_health": 10_000.0,
            "current_health": 6_000.0,
            "dead": False,
            "attackable": True,
        },
        {
            "target_index": 1,
            "maximum_health": 50_000.0,
            "current_health": 50_000.0,
            "dead": False,
            "attackable": False,
        },
    ]
    guard = ObservableCausalGuardV1(
        target_index=0,
        target_hp_pct_gte=35,
        target_attackable_is=True,
        estimated_remaining_attackable_gte_ms=2_500,
        action_ready=ACTION,
        false_semantics=SKIP_PLAN,
    )

    enough = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    )
    assert enough.satisfied
    assert enough.observed["current_attackable_health"] == 6_000.0
    assert enough.observed["estimated_remaining_attackable_ms"] == 3_000.0
    polluted = deepcopy(state)
    polluted["future_team_events"] = [
        {"time_ms": 4_501, "damage": 999_999.0}
    ]
    polluted["terminal_death_time_ms"] = 4_502
    assert evaluate_observable_guard_v1(
        guard, polluted, _available(ready_in_ms=0)
    ) == enough

    state["dynamic_team_background"]["targets"][0]["current_health"] = 4_000.0
    state["dynamic_target_semantics"]["targets"][0]["current_health"] = 4_000.0
    too_late = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    )
    assert too_late.failed_predicates == (
        "estimated_remaining_attackable_gte_ms",
    )
    assert too_late.observed["estimated_remaining_attackable_ms"] == 2_000.0
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard


def test_remaining_attackable_time_defers_until_prefix_rate_exists() -> None:
    state = _state()
    state["dynamic_team_background"] = {
        "schema": "o2o_policy_dynamic_team_prefix_view/v1",
        "prefix_damage_rate": {
            "schema": "o2o_policy_prefix_damage_rate/v1",
            "combined_damage_per_second": None,
        },
        "targets": [
            {"target_index": 0, "current_health": 240.0, "dead": False}
        ],
    }
    guard = ObservableCausalGuardV1(
        estimated_remaining_attackable_gte_ms=1_500,
        false_semantics=SKIP_PLAN,
    )

    result = evaluate_observable_guard_v1(guard, state, ())

    assert result.failed_predicates == (
        "estimated_remaining_attackable_gte_ms",
    )
    assert result.observed["prefix_combined_damage_per_second"] is None
    assert result.observed["estimated_remaining_attackable_ms"] is None
    assert next_observable_guard_check_ms_v1(guard, result, ()) == 100


def test_action_ready_requires_current_legality_even_when_cooldown_is_zero() -> None:
    guard = ObservableCausalGuardV1(
        action_ready=ACTION,
        false_semantics=SKIP_PLAN,
    )
    result = evaluate_observable_guard_v1(
        guard,
        _state(),
        _available(ready_in_ms=0, legal=False),
    )
    assert not result.satisfied
    assert result.failed_predicates == ("action_ready",)
    assert result.observed == {
        "action_ready_in_ms": 0,
        "action_legal": False,
    }


def test_target_aura_stack_cap_is_current_selected_target_only_and_round_trips() -> None:
    guard = ObservableCausalGuardV1(
        target_index=0,
        target_attackable_is=True,
        target_aura_action=SUNDER,
        target_aura_stacks_lte=4,
        action_ready=ACTION,
        false_semantics=SKIP_PLAN,
    )
    state = _state()

    absent = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    )
    assert absent.satisfied
    assert absent.observed["target_aura_stacks"] == 0

    state["target_auras"] = [
        {
            "action": SUNDER.to_wire(),
            "label": "Sunder Armor",
            "stacks": 4,
            "remaining_ms": 20_000,
        }
    ]
    assert evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    ).satisfied

    state["target_auras"][0]["stacks"] = 5
    capped = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    )
    assert capped.failed_predicates == ("target_aura_stacks_lte",)
    assert capped.observed["target_aura_stacks"] == 5

    state["target_index"] = 1
    other_target = evaluate_observable_guard_v1(
        guard, state, _available(ready_in_ms=0)
    )
    assert other_target.failed_predicates == (
        "target_aura_target_is_selected",
    )
    assert other_target.observed["target_aura_stacks"] is None
    assert observable_causal_guard_from_dict_v1(guard.to_dict()) == guard


def test_native_split_target_rows_fall_through_to_complete_team_health() -> None:
    state = _state()
    state["dynamic_target_semantics"]["targets"][0].pop("maximum_health")
    state["dynamic_team_background"] = {
        "targets": [
            {
                "target_index": 0,
                "initial_health": 1_000.0,
                "current_health": 240.0,
                "dead": False,
            }
        ]
    }
    guard = ObservableCausalGuardV1(
        target_index=0,
        target_hp_pct_gte=20,
        target_hp_pct_lte=25,
    )

    result = evaluate_observable_guard_v1(guard, state, ())

    assert result.satisfied
    assert result.observed["target_hp_pct"] == 24.0


def test_target_hp_interval_and_target_identity_are_validated() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        ObservableCausalGuardV1(
            target_index=0,
            target_hp_pct_gte=60,
            target_hp_pct_lte=40,
        )
    wire = ObservableCausalGuardV1(
        target_index=0,
        target_hp_pct_gte=20,
        target_attackable_is=True,
    ).to_dict()
    wire["all_of"]["target_attackable_is"]["target_index"] = 1
    with pytest.raises(ValueError, match="disagree"):
        observable_causal_guard_from_dict_v1(wire)
