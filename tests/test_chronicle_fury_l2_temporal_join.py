from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_fury_l2_temporal_join import (
    L2TemporalJoinError,
    _feature_index,
    build_l2_temporal_join,
    join_decision_record,
)


INSTANCE = "instance-1"
ENCOUNTER = "encounter-1"
PLAYER = "0x0000000000000001"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"
TARGET_C = "0xF130000003000003"


def _anchor(offset_ms: int, event_index: int, event_type: str = "DMG") -> dict[str, object]:
    return {
        "encounter": ENCOUNTER,
        "offset_ms": offset_ms,
        "event_index": event_index,
        "type": event_type,
        "csv_line": event_index + 2,
    }


def _target(
    guid: str,
    *,
    first_offset: int,
    first_index: int,
    death_offset: int | None = None,
    death_index: int | None = None,
) -> dict[str, object]:
    death_anchor = (
        _anchor(death_offset, death_index, "DEAD")
        if death_offset is not None and death_index is not None
        else None
    )
    return {
        "target_guid": guid,
        "activity_interval": {
            "first_offset_ms": first_offset,
            "last_offset_ms": death_offset if death_offset is not None else first_offset,
            "status": "OBSERVED",
            "first_anchor": _anchor(first_offset, first_index),
            "last_anchor": death_anchor or _anchor(first_offset, first_index),
        },
        "kill_budget_proxy": (
            {
                "value": 999999,
                "status": "OBSERVED",
                "death_anchor": death_anchor,
            }
            if death_anchor is not None
            else {"value": None, "status": "MISSING"}
        ),
    }


def _wave(
    ordinal: int,
    *,
    start_offset: int,
    start_index: int,
    end_offset: int,
    end_index: int,
    targets: list[dict[str, object]],
    groups: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "wave_id": f"{ENCOUNTER}:wave:{ordinal}",
        "ordinal": ordinal,
        "start_offset_ms": start_offset,
        "end_offset_ms": end_offset,
        "duration_ms": end_offset - start_offset,
        "target_count": 99,
        "first_anchor": _anchor(start_offset, start_index),
        "last_anchor": _anchor(end_offset, end_index),
        "targets": targets,
        "same_batch_damage_groups": groups or [],
        "target_groups": [
            {
                "group_id": "future-final-group",
                "target_guids": [TARGET_A, TARGET_B, TARGET_C],
            }
        ],
    }


def _feature_report() -> dict[str, object]:
    wave_one = _wave(
        1,
        start_offset=0,
        start_index=1,
        end_offset=1_000,
        end_index=50,
        targets=[
            _target(
                TARGET_A,
                first_offset=0,
                first_index=2,
                death_offset=900,
                death_index=45,
            ),
            _target(TARGET_B, first_offset=400, first_index=20),
            _target(TARGET_C, first_offset=800, first_index=40),
        ],
        groups=[
            {
                "offset_ms": 500,
                "source_guid": PLAYER,
                "spell_id": 1680,
                "spell_name": "Whirlwind",
                "target_guids": [TARGET_A, TARGET_B],
                "status": "OBSERVED",
                "relation": "MELEE_REACH_COHIT",
                # Only the first row anchor survives in the compact report.
                "first_anchor": _anchor(500, 25),
            },
            {
                "offset_ms": 700,
                "source_guid": PLAYER,
                "spell_id": 11605,
                "spell_name": "Slam",
                "target_guids": [TARGET_A, TARGET_B],
                "status": "OBSERVED",
                "relation": "TEMPORAL_COHIT_ONLY",
                "first_anchor": _anchor(700, 35),
            },
        ],
    )
    wave_two = _wave(
        2,
        start_offset=10_000,
        start_index=100,
        end_offset=12_000,
        end_index=130,
        targets=[_target(TARGET_C, first_offset=10_000, first_index=100)],
    )
    return {
        "schema_version": 1,
        "kind": "chronicle_encounter_reconstruction_v1",
        "source": {"normalized_file": "unused.jsonl"},
        "encounters": [
            {
                "instance": INSTANCE,
                "encounter": ENCOUNTER,
                "waves": [wave_one, wave_two],
            }
        ],
    }


def _decision(offset_ms: int, event_index: int, *, encounter: str = ENCOUNTER) -> dict[str, object]:
    return {
        "schema_version": 1,
        "schema": "chronicle_fury_decision/v1",
        "identity": {
            "source_instance_ref": INSTANCE,
            "encounter_id": encounter,
            "player_guid": PLAYER,
        },
        "source": {"start_anchor": _anchor(offset_ms, event_index, "START")},
        "action": {
            "decision_id": f"{encounter}:{PLAYER}:{event_index}",
            "spell_id": 23881,
            "spell_name": "Bloodthirst",
        },
        "state_before": {
            "recent_uniquely_linked_server_actions": [
                {
                    "spell_id": 1680,
                    "spell_name": "Whirlwind",
                    "result_event_index": event_index - 1,
                }
            ],
            "known_player_aura_event_ledger": {
                "initial_state_complete": False,
                "duration_complete": False,
                "entries": [
                    {
                        "spell_id": 12970,
                        "spell_name": "Flurry",
                        "event_index": event_index - 1,
                    }
                ],
            },
            "known_outgoing_target_aura_event_ledger": {
                "initial_state_complete": False,
                "duration_complete": False,
                "entries": [],
            },
        },
        "state_mask": {
            "recent_uniquely_linked_server_actions": True,
            "known_player_aura_event_ledger": True,
            "known_outgoing_target_aura_event_ledger": True,
        },
        "state_provenance": {
            "recent_uniquely_linked_server_actions": {
                "kind": "RECONSTRUCTED",
                "event_index": event_index - 1,
                "offset_ms": max(0, offset_ms - 1),
            },
            "known_player_aura_event_ledger": {
                "kind": "RECONSTRUCTED",
                "event_index": event_index - 1,
                "offset_ms": max(0, offset_ms - 1),
            },
            "known_outgoing_target_aura_event_ledger": {
                "kind": "RECONSTRUCTED",
                "event_index": event_index,
                "offset_ms": offset_ms,
            },
        },
    }


class ChronicleFuryL2TemporalJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = _feature_index(_feature_report())

    def join(self, offset_ms: int, event_index: int) -> dict[str, object]:
        return join_decision_record(
            _decision(offset_ms, event_index),
            self.index,
            feature_name="fixture.reconstruction.json.gz",
        )

    def test_prefix_target_and_alive_proxies_do_not_copy_final_counts(self) -> None:
        early = self.join(300, 10)
        fields = early["fields"]
        self.assertEqual(fields["target_count"]["value"], 1)
        self.assertEqual(fields["alive_target_proxy"]["value"]["count"], 1)
        self.assertEqual(fields["wave_elapsed_ms"]["value"], 300)
        self.assertEqual(fields["current_wave_final_duration_ms"]["mask"], "MISSING")
        self.assertEqual(fields["previous_wave_duration_ms"]["mask"], "MISSING")
        self.assertEqual(fields["pile_cohit_components"]["mask"], "MISSING")

        after_death = self.join(950, 46)
        fields = after_death["fields"]
        self.assertEqual(fields["target_count"]["value"], 3)
        self.assertEqual(fields["alive_target_proxy"]["value"]["count"], 2)
        self.assertEqual(
            fields["alive_target_proxy"]["value"]["target_guids"],
            [TARGET_B, TARGET_C],
        )
        self.assertNotEqual(fields["target_count"]["value"], 99)

    def test_same_offset_cohit_is_withheld_until_a_later_decision(self) -> None:
        at_same_offset = self.join(500, 30)
        self.assertEqual(
            at_same_offset["fields"]["pile_cohit_components"]["mask"],
            "MISSING",
        )

        later = self.join(501, 31)
        pile = later["fields"]["pile_cohit_components"]
        self.assertEqual(pile["mask"], "RECONSTRUCTED")
        self.assertEqual(pile["value"]["components"], [[TARGET_A, TARGET_B]])
        self.assertNotIn(TARGET_C, pile["value"]["components"][0])

    def test_previous_duration_appears_only_after_next_wave_start(self) -> None:
        before_next_wave = self.join(9_999, 99)
        self.assertEqual(
            before_next_wave["fields"]["previous_wave_duration_ms"]["mask"],
            "MISSING",
        )
        self.assertEqual(
            before_next_wave["fields"]["current_wave_final_duration_ms"]["mask"],
            "MISSING",
        )

        next_wave = self.join(10_000, 101)
        self.assertEqual(next_wave["fields"]["wave"]["value"]["ordinal"], 2)
        self.assertEqual(
            next_wave["fields"]["previous_wave_duration_ms"]["value"], 1_000
        )
        self.assertEqual(
            next_wave["fields"]["current_wave_final_duration_ms"]["mask"],
            "MISSING",
        )

    def test_histories_are_prefix_only_and_protected_fields_stay_missing(self) -> None:
        row = self.join(501, 31)
        fields = row["fields"]
        self.assertEqual(fields["recent_action_history"]["mask"], "RECONSTRUCTED")
        self.assertEqual(fields["player_aura_history"]["mask"], "RECONSTRUCTED")
        for name in (
            "absolute_rage",
            "queue_intent",
            "gcd_remaining_ms",
            "cooldown_remaining_ms",
            "mainhand_swing_remaining_ms",
            "offhand_swing_remaining_ms",
            "target_hp",
            "player_hp",
        ):
            self.assertEqual(fields[name]["mask"], "MISSING")
            self.assertIsNone(fields[name]["value"])

        future = _decision(501, 31)
        future["state_before"]["known_player_aura_event_ledger"]["entries"][0][
            "event_index"
        ] = 32
        with self.assertRaisesRegex(Exception, "future state evidence"):
            join_decision_record(
                future,
                self.index,
                feature_name="fixture.reconstruction.json.gz",
            )

    def test_unmatched_encounter_keeps_histories_but_masks_structure(self) -> None:
        joined = join_decision_record(
            _decision(500, 30, encounter="missing-encounter"),
            self.index,
            feature_name="fixture.reconstruction.json.gz",
        )
        self.assertEqual(joined["join"]["status"], "MISSING_ENCOUNTER")
        for name in (
            "wave",
            "wave_elapsed_ms",
            "previous_wave_duration_ms",
            "current_wave_final_duration_ms",
            "target_count",
            "alive_target_proxy",
            "pile_cohit_components",
        ):
            self.assertEqual(joined["fields"][name]["mask"], "MISSING")
        self.assertEqual(
            joined["fields"]["recent_action_history"]["mask"], "RECONSTRUCTED"
        )

    def test_bounded_batch_streams_compact_sidecar_without_normalized_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            decision_partition = root / "source.jsonl.gz"
            with gzip.open(decision_partition, "wt", encoding="utf-8") as handle:
                for offset, event in ((300, 10), (501, 31), (950, 46)):
                    handle.write(json.dumps(_decision(offset, event)) + "\n")
            feature_report = root / "features" / "source.reconstruction.json.gz"
            feature_report.parent.mkdir()
            with gzip.open(feature_report, "wt", encoding="utf-8") as handle:
                json.dump(_feature_report(), handle)

            # This normalized path intentionally does not exist.  The join must
            # use only the compact decision and feature artifacts.
            normalized_file = root / "source.jsonl"
            decision_manifest = root / "decision_manifest.json"
            decision_manifest.write_text(
                json.dumps(
                    {
                        "schema": "chronicle_fury_decision_dataset/v1",
                        "partitions": [
                            {
                                "normalized_file": str(normalized_file),
                                "partition": str(decision_partition),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            feature_manifest = root / "feature_manifest.json"
            feature_manifest.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "source": {
                                    "name": normalized_file.name,
                                    "path": str(normalized_file),
                                },
                                "processing_status": "COMPLETED",
                                "outputs": {
                                    "feature_report": {
                                        "path": str(feature_report.relative_to(root))
                                    }
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            result = build_l2_temporal_join(
                decision_manifest,
                feature_manifest,
                output_dir,
                max_partitions=1,
                max_records_per_partition=2,
            )
            self.assertEqual(result["summary"]["record_count"], 2)
            self.assertEqual(result["inputs"]["normalized_or_raw_rows_read"], 0)
            self.assertEqual(result["summary"]["join_status_counts"], {"MATCHED": 2})
            output_partition = Path(
                result["partitions"][0]["output_partition"]
            )
            self.assertLess(output_partition.stat().st_size, 20_000)
            with gzip.open(output_partition, "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(len(rows), 2)
            self.assertTrue((output_dir / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
