import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps.fury_observed_interval_reward_v1 import (
    DEFAULT_OUTPUT,
    audit_fury_observed_interval_reward,
    build_fury_observed_interval_reward_audit,
)


def _anchor(event_index: int, offset_ms: int | None = None) -> dict[str, object]:
    return {
        "kind": "OBSERVED",
        "event_index": event_index,
        "csv_line": event_index + 100,
        "offset_ms": event_index * 10 if offset_ms is None else offset_ms,
    }


def _record(
    event_index: int,
    *,
    reward: float = 0.0,
    catalog_status: str = "MAPPED_ACTIVE",
    lane: str = "gcd",
    association: str = "unique",
    status: str = "succeeded",
    player_guid: str = "Player-A",
    encounter_id: str = "Encounter-1",
    instance: str = "Instance-1",
    offset_ms: int | None = None,
) -> dict[str, object]:
    return {
        "schema": "chronicle_fury_decision/v1",
        "identity": {
            "source_instance_ref": instance,
            "encounter_id": encounter_id,
            "player_guid": player_guid,
        },
        "source": {"start_anchor": _anchor(event_index, offset_ms)},
        "action": {
            "decision_id": f"{instance}:{encounter_id}:{player_guid}:{event_index}",
            "catalog_status": catalog_status,
            "lane": lane,
            "policy_action_key": (
                "warrior.bloodthirst" if catalog_status == "MAPPED_ACTIVE" else None
            ),
        },
        "result": {
            "association": association,
            "status": status,
        },
        "state_before": {},
        "state_mask": {},
        "state_provenance": {},
        "window_until_next_start_candidate": {
            "outgoing_damage_observed": reward,
            "next_start_anchor": None,
            "causal_reward": None,
            "causal_attribution": "MISSING",
        },
    }


def _link(records: list[dict[str, object]]) -> None:
    for previous, current in zip(records, records[1:]):
        previous["window_until_next_start_candidate"]["next_start_anchor"] = dict(
            current["source"]["start_anchor"]
        )


def _write_partition(path: Path, records: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for record in records:
            json.dump(record, handle, separators=(",", ":"))
            handle.write("\n")


def _manifest(
    root: Path,
    partitions: list[tuple[Path, bool]],
    *,
    decision_count: int,
    damage_rows: int | None = 1,
) -> Path:
    entries: list[dict[str, object]] = []
    for partition, relative in partitions:
        entry: dict[str, object] = {
            "partition": partition.name if relative else str(partition.resolve())
        }
        if damage_rows is not None:
            entry["selected_event_type_counts"] = {"DMG": damage_rows}
        entries.append(entry)
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chronicle_fury_decision_dataset/v1",
                "output": {"decision_count": decision_count},
                "partitions": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


class FuryObservedIntervalRewardV1Tests(unittest.TestCase):
    def test_classifies_every_row_once_and_keeps_reward_noncausal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "decisions.jsonl.gz"
            records = [
                _record(10, reward=100, offset_ms=100),
                _record(
                    20,
                    lane="off_gcd",
                    association="unique",
                    status="failed",
                    offset_ms=150,
                ),
                _record(30, association="ambiguous", offset_ms=350),
                _record(40, association="unlinked", status="missing", offset_ms=360),
                _record(50, lane="on_swing_unknown_intent", offset_ms=500),
                _record(
                    60,
                    catalog_status="UNMAPPED",
                    lane="unknown",
                    offset_ms=900,
                ),
                _record(70, lane="utility", offset_ms=901),
                _record(80, reward=25, offset_ms=1100),
            ]
            _link(records)
            _write_partition(partition, records)
            manifest = _manifest(root, [(partition, False)], decision_count=8)

            report = build_fury_observed_interval_reward_audit(manifest)

            self.assertEqual(report["status"], "ok")
            coverage = report["coverage"]
            self.assertEqual(coverage["decision_rows"], 8)
            self.assertEqual(coverage["observed_interval_rows"], 7)
            self.assertEqual(coverage["reward_eligible_success_rows"], 1)
            self.assertEqual(coverage["eligible_reward_sum"], 100)
            self.assertEqual(
                coverage["observed_interval_delta_t_ms"],
                {
                    "count": 7,
                    "sum_ms": 1000,
                    "min_ms": 1,
                    "max_ms": 400,
                    "mean_ms": 1000 / 7,
                },
            )
            self.assertEqual(
                coverage["eligible_interval_delta_t_ms"],
                {
                    "count": 1,
                    "sum_ms": 50,
                    "min_ms": 50,
                    "max_ms": 50,
                    "mean_ms": 50,
                },
            )
            self.assertEqual(coverage["excluded_rows_total"], 7)
            self.assertTrue(coverage["classification_closes"])
            self.assertEqual(
                coverage["excluded_rows"],
                {
                    "no_next_start": 1,
                    "invalid_boundary": 0,
                    "observation_missing": 0,
                    "damage_stream_unverified": 0,
                    "unmapped_action": 1,
                    "on_swing_unknown_intent": 1,
                    "unsupported_lane": 1,
                    "result_failed_unique": 1,
                    "result_ambiguous": 1,
                    "result_missing": 1,
                    "result_other": 0,
                },
            )
            self.assertEqual(report["contract"]["causal_reward_rows"], 0)
            self.assertEqual(
                report["contract"]["direct_causal_attribution"], "MISSING"
            )
            self.assertFalse(report["offline_rl_gate"]["offline_rl_ready"])
            self.assertTrue(
                any(
                    "factorized multi-lane decision epoch" in blocker
                    for blocker in report["offline_rl_gate"]["blockers"]
                )
            )
            self.assertFalse(report["quality"]["line_level_output_materialized"])

    def test_actual_next_start_must_match_declared_anchor_and_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "bad-boundary.jsonl.gz"
            records = [_record(10, offset_ms=100), _record(20, offset_ms=90)]
            _link(records)
            _write_partition(partition, records)
            manifest = _manifest(root, [(partition, False)], decision_count=2)

            report = build_fury_observed_interval_reward_audit(manifest)

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["coverage"]["excluded_rows"]["invalid_boundary"], 1)
            self.assertEqual(report["coverage"]["excluded_rows"]["no_next_start"], 1)
            self.assertTrue(report["coverage"]["classification_closes"])

    def test_missing_damage_declaration_and_next_observation_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "missing-evidence.jsonl.gz"
            records = [_record(10), _record(20), _record(30)]
            _link(records)
            records[2].pop("state_before")
            _write_partition(partition, records)
            manifest = _manifest(
                root,
                [(partition, False)],
                decision_count=3,
                damage_rows=None,
            )

            report = build_fury_observed_interval_reward_audit(manifest)

            excluded = report["coverage"]["excluded_rows"]
            self.assertEqual(excluded["damage_stream_unverified"], 1)
            self.assertEqual(excluded["observation_missing"], 1)
            self.assertEqual(excluded["no_next_start"], 1)
            self.assertTrue(report["coverage"]["classification_closes"])
            self.assertFalse(
                report["quality"]["reward_contract_usable_for_eligible_intervals"]
            )

    def test_streams_each_partition_once_and_writes_only_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.jsonl.gz"
            second = root / "second.jsonl.gz"
            first_records = [_record(10), _record(20)]
            second_records = [
                _record(5, player_guid="Player-B", instance="Instance-2"),
                _record(15, player_guid="Player-B", instance="Instance-2"),
            ]
            _link(first_records)
            _link(second_records)
            _write_partition(first, first_records)
            _write_partition(second, second_records)
            manifest = _manifest(
                root,
                [(first, False), (second, True)],
                decision_count=4,
            )
            real_gzip_open = gzip.open
            output = root / "reports" / "reward.json"

            with mock.patch(
                "o2o_dps.fury_observed_interval_reward_v1.gzip.open",
                side_effect=real_gzip_open,
            ) as opened:
                report, written = audit_fury_observed_interval_reward(
                    manifest, output_path=output
                )

            self.assertEqual(opened.call_count, 2)
            self.assertEqual(written, output.resolve())
            self.assertEqual(report["coverage"]["reward_eligible_success_rows"], 2)
            self.assertEqual(report["coverage"]["excluded_rows"]["no_next_start"], 2)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["coverage"],
                report["coverage"],
            )
            self.assertEqual([path.name for path in output.parent.iterdir()], ["reward.json"])
            self.assertEqual(DEFAULT_OUTPUT.name, "fury_observed_interval_reward_v1.json")


if __name__ == "__main__":
    unittest.main()
