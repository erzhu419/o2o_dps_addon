from __future__ import annotations

import unittest

from o2o_dps.upper_kara_target_prefix_validation_v1 import analyze_target_prefix_wave_v1


A = "0xF130A"
B = "0xF130B"
P = "0x00001"
Q = "0x00002"


def row(index: int, time: int, kind: str, target: str, actor: str | None = None,
        amount: int | None = None) -> dict:
    return {
        "trace_index": index,
        "trace_kind": "EXACT_PLAYER_EVENT" if actor else "DEATH_MARKER",
        "player_guid": actor,
        "anchor": {"offset_ms": time},
        "event": {
            "event_type": kind,
            "source": {"guid": actor, "lane": "FRIENDLY_PLAYER"} if actor else {},
            "target": {"guid": target, "lane": "HOSTILE_CREATURE"},
            "attribution": {"attribution_kind": "DIRECT_FRIENDLY_PLAYER", "player_guid": actor},
            "damage": {"amount": amount} if amount is not None else None,
        },
    }


def context(index: int, time: int, guid: str) -> dict:
    return {
        "trace_index": index,
        "trace_kind": "CLASSIFICATION_CONTEXT",
        "anchor": {"offset_ms": time},
        "classification": {"guid": guid, "lane": "HOSTILE_CREATURE"},
    }


class TargetPrefixValidationTests(unittest.TestCase):
    def test_current_start_does_not_make_its_own_target_visible(self) -> None:
        result = analyze_target_prefix_wave_v1({
            "wave": {"wave_id": "w"},
            "exact_trace": [
                context(0, 0, A),
                row(1, 100, "START", A, P),
                row(2, 100, "START", B, Q),
                row(3, 200, "DMG", B, Q, 13),
                row(4, 300, "START", B, P),
                row(5, 400, "DEAD", A),
                row(6, 500, "START", B, P),
            ],
        }, detail=True)
        labels = result["first_acquisition_and_retarget_labels"]
        self.assertEqual(["FIRST_ACQUISITION", "FIRST_ACQUISITION", "RETARGET"],
                         [x["choice_type"] for x in labels])
        self.assertEqual([A], [x["target_guid"] for x in labels[0]["prefix_context_candidates"]])
        self.assertFalse(labels[1]["label_context_visible"])
        self.assertEqual([A], [x["target_guid"] for x in labels[1]["prefix_context_candidates"]])
        self.assertEqual([A, B], [x["label_target_guid"] for x in
                               result["first_acquisition_unseen_activity_labels"]])
        self.assertTrue(labels[2]["label_activity_visible"])
        self.assertEqual(13, next(x for x in labels[2]["prefix_context_candidates"]
                                  if x["target_guid"] == B)["prefix_damage"])
        self.assertEqual(1, result["target_choice_metrics"]["RETARGET"]["ACTIVITY_ONLY"]["eligible_multi_choice"])
        self.assertFalse(result["source_boundary"]["attackability_known"])

    def test_future_events_do_not_change_earlier_prefix_and_damage_is_not_choice(self) -> None:
        prefix = [context(0, 0, A), context(1, 0, B), row(2, 100, "DMG", B, Q, 50),
                  row(3, 200, "START", A, P)]
        a = analyze_target_prefix_wave_v1({"wave": {"wave_id": "w"}, "exact_trace": prefix}, detail=True)
        b = analyze_target_prefix_wave_v1({
            "wave": {"wave_id": "w"},
            "exact_trace": prefix + [row(4, 201, "DMG", B, P, 9999), row(5, 1000, "DEAD", A)],
        }, detail=True)
        self.assertEqual(a["first_acquisition_and_retarget_labels"],
                         b["first_acquisition_and_retarget_labels"])
        self.assertEqual(1, a["target_choice_metrics"]["FIRST_ACQUISITION"]["CONTEXT_OR_ACTIVITY"]["labels"])
        self.assertEqual(0.5, a["target_choice_metrics"]["FIRST_ACQUISITION"]["CONTEXT_OR_ACTIVITY"]["uniform_expected_correct"])
        self.assertEqual(0, a["target_choice_metrics"]["RETARGET"]["CONTEXT_OR_ACTIVITY"]["labels"])


if __name__ == "__main__":
    unittest.main()
