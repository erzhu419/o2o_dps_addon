from __future__ import annotations

import unittest

from scripts.development_d900_white_swing_acquisition_probe_v1 import summarize_wave_rows_v1


def row(index: int, actor: str, event_type: str, target: str,
        *, spell: int = 123, hostile: bool = True, candidates: tuple[str, ...] = ("A", "B")) -> dict:
    return {
        "trace_index": index,
        "actor": {"player_guid": actor},
        "emission_state_before_current_event": {
            "wave_elapsed_ms": index * 100,
            "actor_last_target_guid": None,
            "target_state": {"target_choice_candidates": [
                {"target_guid": guid} for guid in candidates
            ]},
        },
        "label": {
            "event_type": event_type,
            "spell_id": spell,
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "source_lane": "FRIENDLY_PLAYER",
            "target_lane": "HOSTILE_CREATURE" if hostile else "FRIENDLY_PLAYER",
            "target_guid": target,
            "target_mode": "SWITCH_ALIVE",
        },
    }


class WhiteSwingAcquisitionTests(unittest.TestCase):
    def test_first_white_is_distinct_from_start_and_buff_predecessor(self) -> None:
        result = summarize_wave_rows_v1([
            row(0, "p", "DMG", "B", spell=6603),
            row(1, "p", "START", "B"),
            row(2, "q", "START", "q", hostile=False),
            row(3, "q", "DMG", "A", spell=6603),
            row(4, "r", "START", "A"),
            row(5, "r", "DMG", "B", spell=6603),
        ])
        self.assertEqual({"p": "WHITE_6603_DMG", "q": "WHITE_6603_DMG", "r": "DIRECT_START"},
                         result["first_hostile_intent_by_actor"])
        self.assertEqual([False, True], [x["prior_direct_start_any"] for x in result["white_first"]])
        self.assertTrue(all(x["strict_prefix_multi_choice"] for x in result["white_first"]))
        self.assertEqual({"p"}, set(result["first_later_direct_hostile_start_after_white_by_actor"]))
        self.assertEqual("B", result["first_later_direct_hostile_start_after_white_by_actor"]["p"]["target_guid"])

    def test_current_white_target_is_not_retroactively_visible(self) -> None:
        result = summarize_wave_rows_v1([
            row(0, "p", "DMG", "B", spell=6603, candidates=("A",)),
        ])
        white = result["white_first"][0]
        self.assertFalse(white["label_prefix_candidate"])
        self.assertFalse(white["strict_prefix_multi_choice"])


if __name__ == "__main__":
    unittest.main()
