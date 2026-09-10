import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps.fury_factorized_decision_epoch_v1 import (
    DEFAULT_OUTPUT,
    FuryFactorizedDecisionEpochError,
    audit_fury_factorized_decision_epochs,
    build_fury_factorized_decision_epoch_audit,
    factorize_start_group,
    iter_factorized_decision_epochs,
    validate_factorized_epoch,
)


_FORBIDDEN_FUTURE_KEYS = frozenset(
    (
        "result",
        "window_until_next_start_candidate",
        "eligibility",
        "state_before",
        "state_mask",
        "state_provenance",
    )
)


class _NoFutureRead(dict):
    def get(self, key, default=None):  # type: ignore[override]
        if key in _FORBIDDEN_FUTURE_KEYS:
            raise AssertionError(f"future/non-action field was read: {key}")
        return super().get(key, default)


def _record(
    event_index: int,
    *,
    offset_ms: int = 100,
    instance: str = "Instance-1",
    encounter: str = "Encounter-1",
    player: str = "Player-A",
    policy_key: str | None = "warrior.bloodthirst",
    spell_id: int = 23881,
    spell_name: str = "Bloodthirst",
    lane: str = "gcd",
    catalog_status: str = "MAPPED_ACTIVE",
    target_guid: str | None = "Target-1",
    target_name: str | None = "Target One",
    poison_future: bool = False,
) -> dict[str, object]:
    record_type = _NoFutureRead if poison_future else dict
    return record_type(
        {
            "schema": "chronicle_fury_decision/v1",
            "identity": {
                "source_instance_ref": instance,
                "encounter_id": encounter,
                "player_guid": player,
            },
            "source": {
                "start_anchor": {
                    "kind": "OBSERVED",
                    "event_index": event_index,
                    "csv_line": event_index + 1000,
                    "offset_ms": offset_ms,
                }
            },
            "action": {
                "decision_id": f"{instance}:{encounter}:{player}:{event_index}",
                "semantics": "server_observed_START_candidate",
                "policy_action_key": policy_key,
                "spell_id": spell_id,
                "spell_name": spell_name,
                "lane": lane,
                "catalog_status": catalog_status,
                "target_guid": target_guid,
                "target_name": target_name,
            },
            "result": {
                "association": "unique",
                "status": "succeeded",
                "anchor": {"event_index": event_index + 5000},
            },
            "window_until_next_start_candidate": {
                "next_start_anchor": {"event_index": event_index + 9000}
            },
            "eligibility": {"observable_behavior_label": True},
            "state_before": {"future_poison": True},
            "state_mask": {"future_poison": True},
            "state_provenance": {
                "future_poison": {"event_index": event_index + 10000}
            },
        }
    )


def _write_partition(path: Path, records: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for record in records:
            json.dump(record, handle, separators=(",", ":"))
            handle.write("\n")


def _manifest(root: Path, partition: Path, decision_count: int) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chronicle_fury_decision_dataset/v1",
                "output": {"decision_count": decision_count},
                "partitions": [{"partition": str(partition.resolve())}],
            }
        ),
        encoding="utf-8",
    )
    return path


class FuryFactorizedDecisionEpochV1Tests(unittest.TestCase):
    def test_exact_time_group_projects_lanes_without_reading_future_fields(self) -> None:
        records = [
            _record(10, poison_future=True),
            _record(
                11,
                policy_key="warrior.death_wish",
                spell_id=12328,
                spell_name="Death Wish",
                lane="off_gcd",
                target_guid="Player-A",
                target_name="Player A",
                poison_future=True,
            ),
            _record(
                12,
                policy_key="warrior.heroic_strike",
                spell_id=25286,
                spell_name="Heroic Strike",
                lane="on_swing_unknown_intent",
                poison_future=True,
            ),
        ]

        epoch = factorize_start_group(records)

        self.assertEqual(epoch["grouping"]["source_row_count"], 3)
        self.assertEqual(epoch["causal_cutoff"]["event_index"], 12)
        self.assertEqual(epoch["action"]["gcd"]["status"], "OBSERVED")
        self.assertEqual(epoch["action"]["off_gcd"]["status"], "OBSERVED")
        self.assertEqual(epoch["action"]["swing_queue"]["status"], "MISSING")
        self.assertIsNone(epoch["action"]["swing_queue"]["value"])
        self.assertEqual(epoch["action"]["target"]["status"], "OBSERVED")
        self.assertEqual(
            epoch["action"]["target"]["value"]["semantics"],
            "resolved_server_target_not_target_switch_intent",
        )
        self.assertTrue(
            any(
                item["reason"]
                == "server_on_swing_START_is_execution_evidence_not_queue_intent"
                for item in epoch["exclusions"]
            )
        )

    def test_stance_action_is_not_mislabelled_as_generic_off_gcd(self) -> None:
        epoch = factorize_start_group(
            [
                _record(
                    10,
                    policy_key="warrior.berserker_stance",
                    spell_id=2458,
                    spell_name="Berserker Stance",
                    lane="off_gcd",
                    target_guid="Player-A",
                    target_name="Player A",
                )
            ]
        )

        self.assertEqual(epoch["action"]["stance"]["status"], "OBSERVED")
        self.assertEqual(
            epoch["action"]["stance"]["value"]["policy_action_key"],
            "warrior.berserker_stance",
        )
        self.assertEqual(epoch["action"]["off_gcd"]["status"], "MISSING")
        self.assertEqual(
            epoch["source_row_dispositions"][0]["disposition"],
            "stance_start_candidate",
        )

    def test_interleaving_never_crosses_identity_and_nonzero_gap_is_not_grouped(self) -> None:
        records = [
            _record(10, player="Player-A", offset_ms=100),
            _record(11, player="Player-B", offset_ms=100),
            _record(
                12,
                player="Player-A",
                offset_ms=100,
                policy_key="warrior.death_wish",
                spell_id=12328,
                spell_name="Death Wish",
                lane="off_gcd",
            ),
            _record(13, player="Player-A", offset_ms=101),
            _record(14, encounter="Encounter-2", player="Player-A", offset_ms=100),
        ]

        epochs = list(iter_factorized_decision_epochs(records))

        self.assertEqual(sum(item["grouping"]["source_row_count"] for item in epochs), 5)
        grouped = [item for item in epochs if item["grouping"]["source_row_count"] == 2]
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["identity"]["player_guid"], "player-a")
        self.assertEqual(grouped[0]["grouping"]["offset_ms"], 100)
        self.assertEqual(len(epochs), 4)

    def test_mixed_identity_or_offset_group_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            FuryFactorizedDecisionEpochError, "crosses instance, encounter, or player"
        ):
            factorize_start_group([_record(10), _record(11, player="Player-B")])
        with self.assertRaisesRegex(FuryFactorizedDecisionEpochError, "exact equal"):
            factorize_start_group([_record(10), _record(11, offset_ms=101)])

    def test_duplicate_lane_and_unmapped_start_remain_unknown(self) -> None:
        epoch = factorize_start_group(
            [
                _record(10),
                _record(
                    11,
                    policy_key="warrior.whirlwind",
                    spell_id=1680,
                    spell_name="Whirlwind",
                    lane="gcd",
                ),
                _record(
                    12,
                    policy_key=None,
                    spell_id=999999,
                    spell_name="Unknown",
                    lane="unknown",
                    catalog_status="UNMAPPED",
                ),
            ]
        )

        self.assertEqual(epoch["action"]["gcd"]["status"], "UNKNOWN")
        self.assertIsNone(epoch["action"]["gcd"]["value"])
        self.assertEqual(epoch["action"]["off_gcd"]["status"], "UNKNOWN")
        self.assertEqual(epoch["action"]["stance"]["status"], "UNKNOWN")
        self.assertEqual(epoch["action"]["swing_queue"]["status"], "MISSING")
        self.assertIn(
            "unmapped_start_candidate",
            [item["disposition"] for item in epoch["source_row_dispositions"]],
        )

    def test_conflicting_resolved_targets_are_unknown(self) -> None:
        epoch = factorize_start_group(
            [
                _record(10, target_guid="Target-1", target_name="One"),
                _record(
                    11,
                    policy_key="warrior.death_wish",
                    spell_id=12328,
                    spell_name="Death Wish",
                    lane="off_gcd",
                    target_guid="Target-2",
                    target_name="Two",
                ),
            ]
        )

        self.assertEqual(epoch["action"]["target"]["status"], "UNKNOWN")
        self.assertEqual(
            epoch["action"]["target"]["reason"], "conflicting_resolved_target_GUIDs"
        )

    def test_validator_rejects_nested_future_and_cross_identity_provenance(self) -> None:
        epoch = factorize_start_group([_record(10), _record(11)])
        future = copy.deepcopy(epoch)
        future["action"]["gcd"]["provenance"][0]["event_index"] = 99
        with self.assertRaisesRegex(
            FuryFactorizedDecisionEpochError, "after its causal cutoff"
        ):
            validate_factorized_epoch(future)

        crossed = copy.deepcopy(epoch)
        crossed["source_starts"][0]["encounter_id"] = "Encounter-Other"
        with self.assertRaisesRegex(
            FuryFactorizedDecisionEpochError, "crosses instance, encounter, or player"
        ):
            validate_factorized_epoch(crossed)

    def test_audit_streams_once_conserves_rows_and_writes_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "decisions.jsonl.gz"
            records = [
                _record(10, offset_ms=100),
                _record(
                    11,
                    offset_ms=100,
                    policy_key="warrior.death_wish",
                    spell_id=12328,
                    spell_name="Death Wish",
                    lane="off_gcd",
                ),
                _record(
                    12,
                    offset_ms=200,
                    policy_key="warrior.heroic_strike",
                    spell_id=25286,
                    spell_name="Heroic Strike",
                    lane="on_swing_unknown_intent",
                ),
            ]
            _write_partition(partition, records)
            original = partition.read_bytes()
            manifest = _manifest(root, partition, decision_count=3)
            output = root / "reports" / "factorized.json"
            real_gzip_open = gzip.open

            with mock.patch(
                "o2o_dps.fury_factorized_decision_epoch_v1.gzip.open",
                side_effect=real_gzip_open,
            ) as opened:
                report, written = audit_fury_factorized_decision_epochs(
                    manifest, output_path=output
                )

            self.assertEqual(opened.call_count, 1)
            self.assertEqual(written, output.resolve())
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["coverage"]["decision_rows"], 3)
            self.assertEqual(report["coverage"]["factorized_epochs"], 2)
            self.assertEqual(report["coverage"]["multi_start_epochs"], 1)
            self.assertEqual(report["coverage"]["parallel_action_lane_epochs"], 1)
            self.assertTrue(report["quality"]["source_row_conservation"])
            self.assertEqual(report["quality"]["future_cutoff_failures"], 0)
            self.assertFalse(report["quality"]["line_level_output_materialized"])
            self.assertFalse(report["offline_rl_gate"]["offline_rl_ready"])
            self.assertEqual(partition.read_bytes(), original)
            self.assertEqual([item.name for item in output.parent.iterdir()], ["factorized.json"])
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["coverage"],
                report["coverage"],
            )
            self.assertEqual(DEFAULT_OUTPUT.name, "fury_factorized_decision_epoch_v1.json")

    def test_manifest_row_mismatch_fails_audit_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "decisions.jsonl.gz"
            _write_partition(partition, [_record(10)])
            manifest = _manifest(root, partition, decision_count=2)

            report = build_fury_factorized_decision_epoch_audit(manifest)

            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["quality"]["row_count_matches_manifest"])
            self.assertTrue(report["quality"]["source_row_conservation"])


if __name__ == "__main__":
    unittest.main()
