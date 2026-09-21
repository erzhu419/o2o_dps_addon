from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.development_responsive_incantagos_smoke_v1 import (
    _drive_bounded_responsive_events,
    _reach_initial_wake,
    run,
)


def test_validation_smoke_requires_formal_model_and_frozen_dispatch() -> None:
    with pytest.raises(ValueError, match="formal runtime-store model"):
        run(simulator_seed=1, teammate_seed=2, source_split="VALIDATION")
    with pytest.raises(ValueError, match="frozen dispatch"):
        run(
            simulator_seed=1,
            teammate_seed=2,
            source_split="VALIDATION",
            loaded_model=object(),
        )


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def state(self):
        self.calls.append("state")
        return {"time_ms": 0}

    def wait(self, wait_ms: int):
        assert wait_ms > 0
        self.calls.append(("wait", wait_ms))

    def advance(self):
        self.calls.append("advance")
        return {"time_ms": 125}


def test_time_zero_empirical_wake_does_not_call_zero_duration_wait() -> None:
    bridge = _Bridge()

    state = _reach_initial_wake(bridge, {"time_ms": 0})

    assert state == {"time_ms": 0}
    assert bridge.calls == ["state"]


def test_positive_initial_wake_uses_existing_wait_then_advance_path() -> None:
    bridge = _Bridge()

    state = _reach_initial_wake(bridge, {"time_ms": 125})

    assert state == {"time_ms": 125}
    assert bridge.calls == ["state", ("wait", 125), "advance"]


class _ContinuousBridge:
    def __init__(self) -> None:
        self.time_ms = 0
        self.wake_ready = {"wake_id": "wake-1"}
        self.armed_wake = None
        self.wait_ms = None
        self.calls: list[object] = []

    def state(self):
        return {
            "time_ms": self.time_ms,
            "finished": False,
            "wake_ready": self.wake_ready,
            "dynamic_team_background": {
                "targets": [
                    {"target_index": 0, "dead": False},
                    {"target_index": 1, "dead": True},
                ]
            },
        }

    def wait(self, wait_ms: int):
        self.calls.append(("wait", wait_ms))
        self.wait_ms = wait_ms
        return self.state()

    def advance(self):
        assert self.wait_ms is not None
        self.calls.append("advance")
        self.time_ms += self.wait_ms
        self.wait_ms = None
        self.wake_ready = {"wake_id": self.armed_wake["wake_id"]}
        return self.state()


def _emitted(
    *,
    sequence: int,
    time_ms: int,
    target_index: int | None,
    observed_damage: float | None = None,
    requested_damage: float,
    applied_damage: float,
    damage_ordinal: int,
):
    observed_damage = (
        requested_damage if observed_damage is None else observed_damage
    )
    return {
        "sampled_emission": {
            "event_type": "DMG",
            "spell_id": 11722,
            "spell_name": "fixture",
            "target_mode": "NO_TARGET" if target_index is None else "STAY_ALIVE",
            "damage_bucket": 0 if observed_damage == 0 else 1,
            "sampled_damage": observed_damage,
        },
        "wire_event": {
            "target_index": target_index,
            "observed_damage": observed_damage,
            "requested_damage": requested_damage,
        },
        "wire_receipt": {
            "time_ms": time_ms,
            "actor_guid": "actor",
            "status": (
                "OBSERVED_NON_HOSTILE_DAMAGE"
                if target_index is None and observed_damage > 0
                else "APPLIED"
                if applied_damage > 0
                else "OBSERVED_NO_DAMAGE"
            ),
            "applied_damage": applied_damage,
            "damage_ordinal": damage_ordinal,
            "current_health": 100.0 if target_index is not None else None,
            "killed": False,
        },
        "scheduler_order_key": [time_ms, "actor", sequence],
        "target_selection_basis": (
            "MODEL_EXPLICIT_OBSERVED_NON_HOSTILE_DAMAGE"
            if target_index is None and observed_damage > 0
            else "MODEL_EXPLICIT_UNTARGETED_ZERO_DAMAGE"
            if target_index is None
            else "ATTACKABLE_ALIVE_REGISTRY"
        ),
        "model_input_live_prefix": {
            "target_state": {"alive_target_guids": ["target"]}
        },
    }


class _ContinuousAdapter:
    def __init__(self, bridge: _ContinuousBridge) -> None:
        self.bridge = bridge
        self.candidate_cursor = 4
        self.rows = [
            (
                _emitted(
                    sequence=0,
                    time_ms=0,
                    target_index=0,
                    requested_damage=0,
                    applied_damage=0,
                    damage_ordinal=1,
                ),
                {"wake_id": "wake-2", "time_ms": 0},
            ),
            (
                _emitted(
                    sequence=1,
                    time_ms=0,
                    target_index=None,
                    requested_damage=0,
                    applied_damage=0,
                    damage_ordinal=0,
                ),
                {"wake_id": "wake-3", "time_ms": 100},
            ),
            (
                _emitted(
                    sequence=2,
                    time_ms=100,
                    target_index=None,
                    observed_damage=5,
                    requested_damage=0,
                    applied_damage=0,
                    damage_ordinal=0,
                ),
                {"wake_id": "wake-4", "time_ms": 200},
            ),
            (
                _emitted(
                    sequence=3,
                    time_ms=200,
                    target_index=0,
                    requested_damage=7,
                    applied_damage=7,
                    damage_ordinal=2,
                ),
                None,
            ),
        ]

    def emit_global_ready_and_rearm(self):
        emitted, next_wake = self.rows.pop(0)
        self.bridge.armed_wake = next_wake
        self.bridge.wake_ready = (
            {"wake_id": next_wake["wake_id"]}
            if next_wake is not None
            and next_wake["time_ms"] == self.bridge.time_ms
            else None
        )
        return {
            "status": (
                "EMITTED_AND_NEXT_GLOBAL_WAKE_ARMED"
                if next_wake is not None
                else "EMITTED_NO_NEXT_WAKE_BEFORE_EXCLUSIVE_HORIZON"
            ),
            "emitted": emitted,
            "next_wake": next_wake,
        }

    def sync_authoritative_damage_prefix(self):
        return ({"source_kind": "CANDIDATE"},)


def test_bounded_driver_handles_same_time_future_and_untargeted_zero_events() -> None:
    bridge = _ContinuousBridge()
    adapter = _ContinuousAdapter(bridge)
    session = SimpleNamespace(
        initial_wake={"wake_id": "wake-1", "time_ms": 0},
        adapter=adapter,
    )

    result = _drive_bounded_responsive_events(
        bridge=bridge,
        session=session,
        max_responsive_events=4,
    )

    assert result["responsive_event_count"] == 4
    assert result["termination_reason"] == (
        "NO_RESPONSIVE_WAKE_BEFORE_EXCLUSIVE_HORIZON"
    )
    assert result["targeted_zero_damage_dmg_count"] == 1
    assert result["untargeted_zero_observed_damage_dmg_count"] == 1
    assert result["untargeted_zero_damage_has_zero_ordinal"] is True
    assert result["positive_non_hostile_observed_damage_count"] == 1
    assert result["positive_non_hostile_damage_has_zero_ordinal"] is True
    assert result["responsive_observed_damage_total"] == 12
    assert result["responsive_requested_damage_total"] == 7
    assert result["responsive_applied_damage_total"] == 7
    assert result["candidate_damage_receipt_cursor"] == 4
    assert result["final_prefix_sync_count"] == 1
    assert result["alive_target_count"] == 1
    assert result["dead_target_count"] == 1
    assert bridge.calls == [("wait", 100), "advance", ("wait", 100), "advance"]
