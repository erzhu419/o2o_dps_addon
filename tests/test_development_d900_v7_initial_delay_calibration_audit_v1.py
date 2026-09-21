from __future__ import annotations

from scripts.development_d900_v7_initial_delay_calibration_audit_v1 import (
    _model_distribution, _wave_summary,
)


def test_wave_summary_keeps_first_any_white_and_hostile_start_separate():
    def event(actor, offset, kind, target=None, spell=None, direct=True):
        return {
            "trace_kind": "EXACT_PLAYER_EVENT", "player_guid": actor,
            "anchor": {"offset_ms": offset},
            "event": {
                "event_type": kind,
                "attribution": {
                    "attribution_kind": "DIRECT_FRIENDLY_PLAYER" if direct else "OWNER_FRIENDLY_PLAYER",
                    "player_guid": actor,
                },
                "target": {"lane": "HOSTILE_CREATURE", "guid": target}
                if target else {"lane": "NO_TARGET"},
                "spell": {"id": spell},
            },
        }
    record = {
        "wave": {"wave_id": "wave-1"},
        "raid_provenance": {"contamination": {"candidate_filter_passed": True}},
        "descriptive_outcome": {"reconstruction_binding": {"window": {
            "start_offset_ms": 100, "context_duration_ms": 19900,
        }}},
        "players": [
            {"player": {"guid": "a", "class": "ROGUE"}, "warrior_spec_lane": {}, "exact_trace_indices": [0, 1, 2]},
            {"player": {"guid": "b", "class": "PALADIN"}, "warrior_spec_lane": {}, "exact_trace_indices": [3]},
        ],
        "exact_trace": [
            event("a", 100, "GO", spell=1, direct=False),
            event("a", 300, "DMG", target="enemy-1", spell=6603),
            event("a", 400, "START", target="enemy-1", spell=10),
            event("b", 500, "START", target="enemy-2", spell=20),
        ],
    }
    wave = _wave_summary(record)
    assert wave["duration_ms"] == 19900
    assert wave["first_observed_direct_hostile_start_ms"] == 300
    assert wave["direct_start_target_count"] == 2
    assert wave["actors"][0]["first_any_ms"] == 0
    assert wave["actors"][0]["first_direct_white6603_ms"] == 200
    assert wave["actors"][0]["first_direct_hostile_start_ms"] == 300


def test_global_delay_tail_uses_uniform_within_bucket():
    model = _model_distribution((10, ((0, 5), (8, 5))))
    assert model["support"] == 10
    assert model["p_delay_ge_9098"] > model["p_delay_ge_14997"] > 0
    assert model["buckets"][1]["lower_ms"] == 8001
    assert model["buckets"][1]["upper_ms"] == 16000
