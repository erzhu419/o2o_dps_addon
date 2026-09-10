from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_combatant_sidecar import (
    MANIFEST_SCHEMA as SIDECAR_MANIFEST_SCHEMA,
    RECORD_SCHEMA as SIDECAR_RECORD_SCHEMA,
)
from o2o_dps.combatant_context_join import (
    audit_combatant_context_coverage,
    build_context_index,
    iter_joined_observations,
    join_decision_record,
    load_context_index,
)
from o2o_dps.o2o_observation import OBSERVATION_FIELDS, validate_observation


def _missing() -> dict[str, object]:
    return {
        "kind": "MISSING",
        "event_index": None,
        "csv_line": None,
        "offset_ms": None,
        "note": "unavailable",
    }


def _decision(
    *,
    instance_ref: str = "instance-1",
    encounter_id: str = "encounter-1",
    player_guid: str = "Player-A",
    start_event_index: int = 10,
) -> dict[str, object]:
    return {
        "schema": "chronicle_fury_decision/v1",
        "schema_version": 1,
        "identity": {
            "source_instance_ref": instance_ref,
            "encounter_id": encounter_id,
            "player_guid": player_guid,
            "player_name": "not policy state",
            "board_spec": "Fury",
            "leaderboard_rows": [],
            "identity_status": "VERIFIED",
        },
        "source": {
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": start_event_index,
                "csv_line": start_event_index + 1,
                "offset_ms": start_event_index * 10,
                "note": "START",
            }
        },
        "action": {},
        "result": {},
        "eligibility": {},
        "state_before": {field: None for field in OBSERVATION_FIELDS},
        "state_mask": {field: False for field in OBSERVATION_FIELDS},
        "state_provenance": {field: _missing() for field in OBSERVATION_FIELDS},
        "window_until_next_start_candidate": {},
    }


def _info(
    event_index: int,
    *,
    instance_ref: str = "instance-1",
    encounter_id: str = "encounter-1",
    player_guid: str = "Player-A",
    message_ordinal: int | None = None,
    item_id: int = 19019,
    talent_marker: str = "1",
) -> dict[str, object]:
    ordinal = event_index if message_ordinal is None else message_ordinal
    return {
        "schema": SIDECAR_RECORD_SCHEMA,
        "instance_ref": instance_ref,
        "slug": "slug-1",
        "encounter_id": encounter_id,
        "first_timestamp_ms": 1_700_000_000_000,
        "frame_index": 0,
        "frame_message_index": ordinal,
        "message_ordinal": ordinal,
        "anchor": {
            "event_index": event_index,
            "offset_ms": event_index * 10,
            "timestamp_ms": 1_700_000_000_000 + event_index * 10,
            "is_synthetic": False,
        },
        "player": {
            "guid": player_guid,
            "name": "historical player",
            "hero_class": "WARRIOR",
            "race": "Orc",
            "gender": 2,
            "guild_name": "Historical Guild",
        },
        "gear": [
            {
                "slot_index": 0,
                "item_id": item_id,
                "enchant_id": 2564,
                "temporary_enchant_id": None,
                "gem_enchant_ids": [],
            },
            {
                "slot_index": 1,
                "item_id": 0,
                "enchant_id": None,
                "temporary_enchant_id": None,
                "gem_enchant_ids": [],
            },
        ],
        "talents": {
            "summary": [17, 34, 0],
            "trees": [talent_marker, f"{talent_marker}2", ""],
        },
    }


def _write_gzip_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with gzip.open(path, mode="wt", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row))
            handle.write("\n")


class CombatantContextJoinTests(unittest.TestCase):
    def test_late_info_is_rejected_and_fields_remain_missing(self) -> None:
        index = build_context_index(
            [_info(11)], instance_ref="instance-1", source_artifact="sidecar.jsonl.gz"
        )
        joined = join_decision_record(_decision(start_event_index=10), index)

        self.assertFalse(joined.causal_info_matched)
        self.assertTrue(joined.late_info_rejected)
        self.assertEqual(joined.join_outcome, "late_info_only")
        self.assertEqual(joined.observation["fields"]["gear_item_ids"]["status"], "MISSING")
        self.assertEqual(joined.observation["fields"]["exact_talent_ranks"]["status"], "MISSING")

    def test_guid_mismatch_does_not_join(self) -> None:
        index = build_context_index(
            [_info(5, player_guid="Player-B")],
            instance_ref="instance-1",
            source_artifact="sidecar.jsonl.gz",
        )
        joined = join_decision_record(_decision(player_guid="Player-A"), index)

        self.assertFalse(joined.causal_info_matched)
        self.assertFalse(joined.late_info_rejected)
        self.assertEqual(joined.join_outcome, "guid_mismatch")
        self.assertEqual(joined.observation["fields"]["gear_item_ids"]["status"], "MISSING")

    def test_duplicate_event_index_selects_latest_stream_ordinal(self) -> None:
        records = [
            _info(5, message_ordinal=0, item_id=100, talent_marker="a"),
            _info(9, message_ordinal=1, item_id=200, talent_marker="b"),
            _info(9, message_ordinal=2, item_id=300, talent_marker="c"),
        ]
        index = build_context_index(
            records, instance_ref="instance-1", source_artifact="sidecar.jsonl.gz"
        )
        joined = join_decision_record(_decision(start_event_index=9), index)

        self.assertTrue(joined.causal_info_matched)
        self.assertEqual(joined.join_outcome, "matched")
        gear = joined.observation["fields"]["gear_item_ids"]
        talents = joined.observation["fields"]["exact_talent_ranks"]
        self.assertEqual(gear["value"], [300, 0])
        self.assertEqual(talents["value"], ["c", "c2", ""])
        self.assertEqual(gear["provenance"]["event_index"], 9)
        self.assertEqual(gear["provenance"]["message_ordinal"], 2)
        validate_observation(joined.observation)

    def test_join_does_not_mutate_input_record(self) -> None:
        record = _decision()
        before = copy.deepcopy(record)
        index = build_context_index(
            [_info(5)], instance_ref="instance-1", source_artifact="sidecar.jsonl.gz"
        )

        joined = join_decision_record(record, index)

        self.assertEqual(record, before)
        self.assertEqual(joined.observation["fields"]["gear_item_ids"]["status"], "OBSERVED")
        self.assertEqual(joined.observation["fields"]["exact_talent_ranks"]["status"], "OBSERVED")

    def test_encounter_missing_and_unanchored_info_are_distinct(self) -> None:
        unanchored = _info(5)
        unanchored["anchor"] = None
        index = build_context_index(
            [unanchored],
            instance_ref="instance-1",
            source_artifact="sidecar.jsonl.gz",
        )

        exact_identity = join_decision_record(_decision(), index)
        missing_encounter = join_decision_record(
            _decision(encounter_id="encounter-2"), index
        )

        self.assertEqual(exact_identity.join_outcome, "unanchored_info_only")
        self.assertEqual(missing_encounter.join_outcome, "encounter_missing")
        self.assertEqual(
            exact_identity.observation["fields"]["gear_item_ids"]["status"],
            "MISSING",
        )

    def test_context_loader_reads_only_gzip_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "context.jsonl.gz"
            _write_gzip_jsonl(path, [_info(5)])

            index = load_context_index(path, instance_ref="instance-1")
            joined = join_decision_record(_decision(), index)

            self.assertTrue(joined.causal_info_matched)
            self.assertEqual(
                joined.observation["fields"]["gear_item_ids"]["value"], [19019, 0]
            )

    def test_streaming_audit_counts_coverage_and_404(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "instance-1.combatant_info.jsonl.gz"
            _write_gzip_jsonl(
                sidecar,
                [
                    _info(5, message_ordinal=0, item_id=100),
                    _info(9, message_ordinal=1, item_id=200),
                ],
            )
            sidecar_manifest = root / "sidecar-manifest.json"
            sidecar_manifest.write_text(
                json.dumps(
                    {
                        "schema": SIDECAR_MANIFEST_SCHEMA,
                        "sidecar_compressed_bytes_total": sidecar.stat().st_size,
                        "instances": [
                            {
                                "instance_ref": "instance-1",
                                "availability": "available",
                                "http_status": None,
                                "sidecar_path": str(sidecar),
                            },
                            {
                                "instance_ref": "instance-2",
                                "availability": "unavailable",
                                "http_status": 404,
                                "sidecar_path": None,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            partition_one = root / "instance-1.jsonl.gz"
            _write_gzip_jsonl(
                partition_one,
                [
                    _decision(start_event_index=10),
                    _decision(start_event_index=4),
                    _decision(player_guid="Player-B", start_event_index=10),
                    _decision(start_event_index=9),
                ],
            )
            partition_two = root / "instance-2.jsonl.gz"
            _write_gzip_jsonl(
                partition_two,
                [_decision(instance_ref="instance-2", start_event_index=10)],
            )
            dataset_manifest = root / "dataset-manifest.json"
            dataset_manifest.write_text(
                json.dumps(
                    {
                        "schema": "chronicle_fury_decision_dataset/v1",
                        "output": {"decision_count": 5},
                        "partitions": [
                            {
                                "partition": str(partition_one),
                                "source_instance_refs": ["instance-1"],
                            },
                            {
                                "partition": str(partition_two),
                                "source_instance_refs": ["instance-2"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "coverage.json"

            outcomes = list(
                iter_joined_observations(
                    dataset_manifest=dataset_manifest,
                    sidecar_manifest=sidecar_manifest,
                )
            )
            report = audit_combatant_context_coverage(
                dataset_manifest=dataset_manifest,
                sidecar_manifest=sidecar_manifest,
                output=report_path,
            )

            self.assertEqual(len(outcomes), 5)
            self.assertEqual(report["coverage"]["decision_rows"], 5)
            self.assertEqual(report["coverage"]["causal_info_matched"], 2)
            self.assertEqual(report["coverage"]["gear_item_ids_available"], 2)
            self.assertEqual(report["coverage"]["exact_talent_ranks_available"], 2)
            self.assertEqual(report["coverage"]["late_info_rejected"], 1)
            self.assertEqual(
                report["coverage"]["join_outcomes"],
                {
                    "instance_unavailable": 1,
                    "encounter_missing": 0,
                    "guid_mismatch": 1,
                    "late_info_only": 1,
                    "unanchored_info_only": 0,
                    "matched": 2,
                },
            )
            self.assertEqual(report["instances"], {
                "referenced": 2,
                "available": 1,
                "unavailable_404": 1,
                "other_missing_or_unavailable": 0,
            })
            self.assertEqual(
                report["quality"],
                {
                    "declared_decision_rows": 5,
                    "streamed_decision_rows": 5,
                    "row_count_matches_manifest": True,
                    "join_outcome_rows": 5,
                    "join_outcome_sum_matches_decision_rows": True,
                    "future_cutoff_failures": 0,
                    "line_level_output_materialized": False,
                },
            )
            self.assertTrue(report_path.is_file())
            self.assertEqual(
                outcomes[-1].observation["fields"]["gear_item_ids"]["status"],
                "MISSING",
            )
            self.assertEqual(outcomes[-1].join_outcome, "instance_unavailable")


if __name__ == "__main__":
    unittest.main()
