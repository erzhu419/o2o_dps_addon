from __future__ import annotations

import unittest

from scripts.development_d900_team_spill_probe_v1 import FOCAL, TARGETS, WAVE, analyze


def _row(offset: int, actor: str, target: str, amount: int) -> dict:
    return {
        "trace_kind": "EXACT_PLAYER_EVENT",
        "player_guid": actor,
        "anchor": {"offset_ms": offset},
        "event": {
            "event_type": "DMG",
            "source": {"lane": "FRIENDLY_PLAYER"},
            "target": {"guid": target},
            "attribution": {"attribution_kind": "DIRECT_FRIENDLY_PLAYER"},
            "spell": {"id": 1680, "name": "Whirlwind"},
            "damage": {"amount": amount},
        },
    }


class TeamSpillProbeTests(unittest.TestCase):
    def test_first_teammate_events_and_cutoff_damage(self) -> None:
        result = analyze({
            "wave": {"wave_id": WAVE},
            "exact_trace": [
                _row(0, "teammate-a", TARGETS[0], 10),
                _row(2000, "teammate-a", TARGETS[1], 20),
                _row(3000, "teammate-b", TARGETS[2], 30),
                _row(4000, FOCAL, TARGETS[0], 40),
            ],
        })
        self.assertEqual([0, 3000], result["first_teammate_actor_event_offsets_ms"])
        self.assertEqual(1, result["first_teammate_actor_event_count_before_3000_ms"])
        self.assertEqual(2, result["teammate_exact_player_event_count_before_3000_ms"])
        self.assertEqual(20, result["damage_by_target_at_cutoff"]["3000"][TARGETS[1]]["other_player"])
        self.assertEqual(40, result["damage_by_target_at_cutoff"]["9093"][TARGETS[0]]["focal"])


if __name__ == "__main__":
    unittest.main()
