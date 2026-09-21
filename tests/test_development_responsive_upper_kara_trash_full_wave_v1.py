from scripts.development_responsive_upper_kara_trash_full_wave_v1 import (
    _DiagnosticFirstDelayModel,
    _SparseTargetTimelineBridge,
    _historical_first_actor_delays,
)


def test_sparse_target_timeline_records_reached_state_not_future_anchor():
    probe = _SparseTargetTimelineBridge(object())
    probe._observe({
        "time_ms": 0,
        "finished": False,
        "dynamic_target_semantics": {"targets": [
            {"target_index": 0, "current_health": 100, "dead": False, "attackable": True}
        ]},
    })
    assert [row["requested_at_or_after_ms"] for row in probe.snapshots] == [0]

    probe._observe({
        "time_ms": 9094,
        "finished": False,
        "dynamic_target_semantics": {"targets": [
            {"target_index": 0, "current_health": 0, "dead": True, "attackable": False}
        ]},
    })
    assert [row["requested_at_or_after_ms"] for row in probe.snapshots] == [
        0, 3000, 6000, 9093,
    ]
    assert probe.snapshots[-1]["observed_time_ms"] == 9094
    assert probe.first_observed_dead_ms == {0: 9094}


def test_same_wave_first_delay_override_applies_once_per_actor():
    wave = {
        "descriptive_outcome": {"reconstruction_binding": {"window": {"start_offset_ms": 1000}}},
        "exact_trace": [
            {"trace_kind": "EXACT_PLAYER_EVENT", "anchor": {"offset_ms": 1250}},
            {"trace_kind": "EXACT_PLAYER_EVENT", "anchor": {"offset_ms": 1450}},
            {"trace_kind": "EXACT_PLAYER_EVENT", "anchor": {"offset_ms": 1310}},
        ],
        "players": [
            {"player": {"guid": "teammate-a"}, "exact_trace_indices": [1, 0]},
            {"player": {"guid": "teammate-b"}, "exact_trace_indices": [2]},
        ],
    }
    first = _historical_first_actor_delays(wave, ("teammate-a", "teammate-b"))
    assert first == {"teammate-a": 250, "teammate-b": 310}

    class Base:
        calls = 0

        def sample_delay(self, *, actor, timing_state, rng):
            self.calls += 1
            return {"delay_ms": 50, "delay_bucket": 0, "context_level": "BASE", "support": 10}

    base = Base()
    model = _DiagnosticFirstDelayModel(base, first)
    draw = lambda guid: model.sample_delay(actor={"player_guid": guid}, timing_state={}, rng=None)
    assert draw("teammate-a")["delay_ms"] == 250
    assert draw("teammate-a")["delay_ms"] == 50
    assert draw("teammate-b")["delay_ms"] == 310
    assert draw("other")["delay_ms"] == 50
    assert model.applied == first
    assert base.calls == 4
