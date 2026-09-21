from __future__ import annotations

import unittest

from scripts.development_d900_historical_spell_target_audit_v1 import FOCAL, TARGETS, WAVE, analyze


def _row(offset: int, actor: str | None, target: str, spell: int, amount: int,
         overkill: int = 0, trace_kind: str = "EXACT_PLAYER_EVENT") -> dict:
    return {
        "trace_kind": trace_kind,
        "player_guid": actor,
        "anchor": {"offset_ms": offset},
        "event": {
            "event_type": "DMG",
            "source": {"lane": "FRIENDLY_PLAYER"},
            "target": {"guid": target},
            "attribution": {"attribution_kind": "DIRECT_FRIENDLY_PLAYER"},
            "spell": {"id": spell},
            "damage": {"amount": amount, "overkill": overkill},
        },
    }


class HistoricalSpellTargetAuditTests(unittest.TestCase):
    def test_owner_cutoff_and_logged_overkill_remain_separate(self) -> None:
        result = analyze({"wave": {"wave_id": WAVE}, "exact_trace": [
            _row(9093, "teammate-a", TARGETS[0], 6603, 100, 30),
            _row(9098, FOCAL, TARGETS[1], 23881, 20),
            _row(9098, None, TARGETS[2], 1680, 50,
                 trace_kind="UNATTRIBUTED_EVENT"),
            _row(9099, "teammate-a", TARGETS[0], 6603, 200),
        ]})
        rows = {(row["cutoff_ms_inclusive"], row["owner"], row["target_guid"],
                 row["spell_id"]): row for row in result["per_spell"]}
        teammate = rows[(9093, "teammate_direct", TARGETS[0], 6603)]
        self.assertEqual((1, 100, 30, 70), (
            teammate["hit_count"], teammate["raw_damage"],
            teammate["declared_overkill"], teammate["amount_minus_overkill_proxy"],
        ))
        self.assertNotIn((9093, "focal_direct", TARGETS[1], 23881), rows)
        self.assertEqual(20, rows[(9098, "focal_direct", TARGETS[1], 23881)]["raw_damage"])
        self.assertEqual(50, rows[(9098, "unattributed_fixed_background", TARGETS[2], 1680)]["raw_damage"])
        self.assertEqual(4, len(rows))


if __name__ == "__main__":
    unittest.main()
