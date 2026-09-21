from __future__ import annotations

import unittest

from scripts.development_d900_white_target_head_gate_v1 import summarize_wave_rows_v1


def _row(actor: str, selected: str, previous: str | None,
         candidates: tuple[str, ...], *, spell: int = 6603) -> dict:
    return {
        "actor": {"player_guid": actor},
        "label": {
            "event_type": "DMG", "spell_id": spell,
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "source_lane": "FRIENDLY_PLAYER", "target_lane": "HOSTILE_CREATURE",
            "target_guid": selected,
            "target_mode": "SWITCH_ALIVE" if selected in candidates else "UNSEEN_HOSTILE_CURRENT_LABEL",
        },
        "emission_state_before_current_event": {
            "actor_last_target_guid": previous,
            "target_state": {"target_choice_candidates": [
                {"target_guid": guid} for guid in candidates
            ]},
        },
    }


class WhiteTargetHeadGateTests(unittest.TestCase):
    def test_strict_prefix_first_retarget_and_unseen_first_white(self) -> None:
        result = summarize_wave_rows_v1([
            _row("a", "A", None, ("A", "B")),
            _row("a", "B", "A", ("A", "B", "C")),
            _row("a", "B", "B", ("A", "B", "C")),
            _row("b", "U", None, ("A", "B")),
            _row("c", "C", "A", ("A", "C")),
            _row("d", "A", None, ("A", "B"), spell=1680),
        ])
        counts = result["counts"]
        self.assertEqual(5, counts["direct_hostile_white_6603"])
        self.assertEqual(3, counts["first_white_actor_wave"])
        self.assertEqual(1, counts["first_white_target_not_prefix_visible"])
        self.assertEqual(1, counts["stay_deterministic"])
        self.assertEqual(1, counts["FIRST_ACQUISITION_strict_prefix_multi_choice"])
        self.assertEqual(1, counts["FIRST_ACQUISITION_target_not_prefix_visible"])
        self.assertEqual(1, counts["RETARGET_strict_prefix_multi_choice"])
        self.assertEqual(1, counts["RETARGET_singleton_or_zero"])
        self.assertEqual(0.5, result["uniform_expected_hit_sum"]["FIRST_ACQUISITION"])
        self.assertEqual(0.5, result["uniform_expected_hit_sum"]["RETARGET"])


if __name__ == "__main__":
    unittest.main()
