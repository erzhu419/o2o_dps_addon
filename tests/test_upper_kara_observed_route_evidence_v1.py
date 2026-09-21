from __future__ import annotations

import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from o2o_dps.upper_kara_observed_route_evidence_v1 import (
    extract_observed_route_evidence_v1,
    extract_selected_wave_gzip_v1,
)


A = "0xF130A"
B = "0xF130B"
FOCAL = "0x00001"
OTHER = "0x00002"


def event(offset: int, index: int, kind: str, target: str, actor: str | None,
          amount: int | None = None) -> dict:
    return {
        "trace_kind": "EXACT_PLAYER_EVENT" if actor else "DEATH_MARKER",
        "player_guid": actor,
        "trace_index": index,
        "anchor": {"offset_ms": offset},
        "event": {
            "event_type": kind,
            "source": {"guid": actor, "lane": "FRIENDLY_PLAYER"} if actor else {},
            "target": {"guid": target, "lane": "HOSTILE_CREATURE"},
            "attribution": {"attribution_kind": "DIRECT_FRIENDLY_PLAYER", "player_guid": actor},
            "damage": {"amount": amount} if amount is not None else None,
            "spell": {"id": 123, "name": "Strike"},
        },
    }


class ObservedRouteEvidenceTests(unittest.TestCase):
    def test_split_damage_and_focal_switch_are_descriptive(self) -> None:
        record = {
            "wave": {"instance_id": "i", "encounter_id": "e", "wave_id": "w"},
            "exact_trace": [
                event(100, 0, "START", A, FOCAL),
                event(200, 1, "DMG", A, FOCAL, 100),
                event(250, 2, "DMG", B, OTHER, 200),
                event(1200, 3, "START", B, FOCAL),
                event(1300, 4, "DMG", B, FOCAL, 300),
                event(1500, 5, "DEAD", A, None),
                event(1600, 6, "DMG", A, OTHER, 99),
                event(2100, 7, "DEAD", B, None),
            ],
        }
        result = extract_observed_route_evidence_v1(
            record, target_guids=(A, B), focal_guid=FOCAL
        )
        self.assertTrue(result["damage_timeline"][0]["more_than_one_target_damaged"])
        self.assertEqual([A, B], [row["target_guid"] for row in result["focal_event_target_changes"]])
        self.assertEqual([A, B], [row["target_guid"] for row in result["focal_targeted_action_events"]])
        self.assertEqual([A, B], [row["target_guid"] for row in result["focal_start_target_transitions"]])
        self.assertEqual(99, result["targets"][0]["post_death_positive_damage"])
        self.assertEqual(100, result["targets"][0]["positive_damage"])
        self.assertFalse(result["interpretation"]["raid_leader_mandated_order_known"])

    def test_selects_exact_wave_without_returning_trace(self) -> None:
        records = [
            {"wave": {"instance_id": "i", "encounter_id": "e", "wave_id": "other"}, "exact_trace": []},
            {"wave": {"instance_id": "i", "encounter_id": "e", "wave_id": "w"},
             "exact_trace": [event(10, 0, "DMG", A, FOCAL, 7)]},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "waves.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            result = extract_selected_wave_gzip_v1(
                path, instance_id="i", encounter_id="e", wave_id="w",
                target_guids=(A,), focal_guid=FOCAL,
            )
        self.assertEqual(2, result["source"]["jsonl_line"])
        self.assertNotIn("exact_trace", result)


if __name__ == "__main__":
    unittest.main()
