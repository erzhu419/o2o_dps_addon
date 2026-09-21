from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import o2o_dps.responsive_white6603_actor_trace_v1 as trace_module
from o2o_dps.responsive_white6603_actor_trace_v1 import White6603ActorTraceDrivenBridgeV1
from scripts import development_d900_white6603_actor_trace_runner_v1 as runner


def _emitted(*, actor, time_ms, sequence, status, target, spell_id=6603,
             attribution_kind="DIRECT_FRIENDLY_PLAYER", damage=100):
    return {
        "wire_receipt": {
            "actor_guid": actor, "time_ms": time_ms, "target_index": target,
            "event_type": "DMG", "status": status,
            "applied_damage": damage if status == "APPLIED" else 0,
        },
        "sampled_emission": {
            "spell_id": spell_id, "attribution_kind": attribution_kind,
        },
        "scheduler_order_key": [time_ms, actor, sequence],
        "local_runtime_transition": {
            "target_guid": None if target is None else f"target-{target}",
            "requested_damage": damage,
        },
        "target_selection_basis": "LEARNED_DIRECT_WHITE6603_PREFIX_TARGET_CHOICE",
        "target_choice_head": None,
    }


def test_early_direct_white_trace_keeps_actor_timing_outcomes_and_basis_only():
    driven = White6603ActorTraceDrivenBridgeV1(
        bridge=object(), case=object(), loaded_model=object(), teammate_seed=17,
    )
    events = [
        _emitted(actor="b", time_ms=9098, sequence=2,
                 status="CANCELED_TARGET_UNATTACKABLE", target=2),
        _emitted(actor="a", time_ms=100, sequence=0, status="APPLIED", target=0),
        _emitted(actor="a", time_ms=150, sequence=1, status="APPLIED", target=0,
                 damage=0),
        _emitted(actor="a", time_ms=200, sequence=2, status="APPLIED", target=1,
                 attribution_kind="INDIRECT_FRIENDLY_PLAYER"),
        _emitted(actor="a", time_ms=300, sequence=3, status="APPLIED", target=1,
                 spell_id=23894),
        _emitted(actor="c", time_ms=9099, sequence=0, status="APPLIED", target=0),
    ]
    for event in events:
        driven._record_target_event(event)
    trace = driven.target_event_diagnostic()["early_direct_white6603_actor_trace"]
    assert trace["cutoff_ms_inclusive"] == 9098
    assert [(row["actor_guid"], row["time_ms"], row["status"],
             row["target_index"], row["requested_damage"], row["applied_damage"])
            for row in trace["events"]] == [
                ("a", 100, "APPLIED", 0, 100, 100),
                ("a", 150, "APPLIED", 0, 0, 0),
                ("b", 9098, "CANCELED_TARGET_UNATTACKABLE", 2, 100, 0),
            ]
    assert all(row["target_selection_basis"] ==
               "LEARNED_DIRECT_WHITE6603_PREFIX_TARGET_CHOICE"
               for row in trace["events"])
    assert [row["sequence"] for row in trace["events"]] == [0, 1, 2]
    assert [row["target_guid"] for row in trace["events"]] == [
        "target-0", "target-0", "target-2",
    ]


def test_diagnostic_runner_only_returns_small_replay_trace(monkeypatch, capsys):
    original = runner.full_wave.IncantagosDevelopmentDrivenBridgeV1
    seen = []
    def fake_main():
        seen.append(runner.full_wave.IncantagosDevelopmentDrivenBridgeV1)
        print(json.dumps({
            "wave_id": "wave-1",
            "paired_replays": [{
                "source_policy_id": "cat", "seed": 1, "teammate_seed": 2,
                "status": "COMPLETE", "invalid_reason": None,
                "responsive_drive": {"target_event_diagnostic": {
                    "early_direct_white6603_actor_trace": {"events": [{"actor_guid": "a"}]},
                    "focus_actor_wake_marks": [],
                    "focus_actor_deadline_audit": [],
                }},
                "large_unrelated_field": ["not returned"],
            }],
        }))
        return 0
    monkeypatch.setattr(runner.full_wave, "main", fake_main)
    try:
        assert runner.main() == 0
        assert seen == [White6603ActorTraceDrivenBridgeV1]
        compact = json.loads(capsys.readouterr().out)
        assert compact["early_direct_white6603_actor_trace"]["events"] == [
            {"actor_guid": "a"},
        ]
        assert "large_unrelated_field" not in compact
    finally:
        monkeypatch.setattr(runner.full_wave, "IncantagosDevelopmentDrivenBridgeV1", original)


def test_focus_actor_all_ready_wakes_and_marks_are_observed_without_sampling():
    actor = "0x0000000000757D23"
    case = SimpleNamespace(
        actors=({"player_guid": actor, "class": "WARRIOR", "spec_key": "FURY"},),
        teammate_player_guids=(actor,),
    )
    driven = White6603ActorTraceDrivenBridgeV1(
        bridge=object(), case=case, loaded_model=object(), teammate_seed=17,
    )
    first = _emitted(actor=actor, time_ms=300, sequence=0, status="APPLIED",
                     target=0, spell_id=23894)
    first["bridge_attackable_alive_target_indices_before_emission"] = [0, 1]
    second = _emitted(actor=actor, time_ms=500, sequence=1, status="APPLIED",
                      target=0)
    second["bridge_attackable_alive_target_indices_before_emission"] = [0]
    later = _emitted(actor=actor, time_ms=9099, sequence=2, status="APPLIED",
                     target=0)
    for event in (first, second, later):
        driven._record_target_event(event)
    focused = driven.target_event_diagnostic()["focus_actor_wake_marks"][0]
    assert focused["actor_guid"] == actor
    assert focused["runtime_roster"] == {
        "class": "WARRIOR", "spec_key": "FURY", "teammate_runtime_actor": True,
    }
    assert focused["drained_wake_count"] == 2
    assert focused["first_wake_ms"] == 300
    assert focused["last_wake_ms"] == 500
    assert focused["white6603_mark_count"] == 1
    assert focused["direct_white6603_mark_count"] == 1
    assert focused["wakes_with_attackable_target_count"] == 2
    assert [row["spell_id"] for row in focused["marks"]] == [23894, 6603]


def test_initial_deadlines_are_copied_before_first_ready_drain(monkeypatch):
    actors = trace_module.FOCUS_ACTORS
    adapter = SimpleNamespace(
        _deadline_by_actor={
            actor: {"actor_guid": actor, "time_ms": time_ms, "event_sequence": 0}
            for actor, time_ms in zip(actors, (12000, 1200, 7000))
        },
        _horizon_discard_by_actor={},
        _active_actor=actors[1],
        _armed_by_actor={actors[1]: {"actor_guid": actors[1], "time_ms": 1200}},
        _sequence_by_actor={actor: 0 for actor in actors},
    )

    @dataclass(frozen=True)
    class Load:
        state: dict

    session = SimpleNamespace(
        adapter=adapter, load_result=Load({"time_ms": 0}),
    )
    monkeypatch.setattr(
        trace_module, "bind_responsive_incantagos_development_session_v1",
        lambda **kwargs: session,
    )
    case = SimpleNamespace(
        request={"one": 1}, dynamic_config="config", actors=(),
        teammate_player_guids=actors,
    )
    driven = White6603ActorTraceDrivenBridgeV1(
        bridge=SimpleNamespace(state=lambda: {"time_ms": 0}),
        case=case, loaded_model=object(), teammate_seed=17,
    )
    monkeypatch.setattr(driven, "_drain_ready", lambda state: state)
    assert driven.load_dynamic_v4({"one": 1}, 99, "config").state == {"time_ms": 0}
    adapter._deadline_by_actor[actors[0]]["time_ms"] = 13000
    audit = driven.target_event_diagnostic()["focus_actor_deadline_audit"]
    assert audit[0]["initial"]["planned_deadline"]["time_ms"] == 12000
    assert audit[0]["final_planned_deadline"]["time_ms"] == 13000
    assert audit[1]["initial"]["active_armed"] is True
    assert audit[0]["final_event_sequence"] == 0
