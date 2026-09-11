from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_character_dps_index_v1 as index_v1
from o2o_dps import chronicle_external_character_history_v1 as history_v1


GUID_A = "0x00000000000000A1"
GUID_B = "0x00000000000000B2"
GUID_C = "0x00000000000000C3"
INSTANCE_A = "11111111-1111-1111-1111-111111111111"
INSTANCE_B = "22222222-2222-2222-2222-222222222222"


def _ranking(record_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {
        "ranking_record_id": record_id,
        "encounter_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "encounter_name": "King",
        "player_spec": "Fury",
        "player_role": "dps",
        "killed_at": "2026-08-31T16:05:00Z",
        "damage_done": 120000,
        "duration_secs": 100,
        "dps": 1200,
    }


def _coverage(
    status: str,
    *,
    records: list[dict] | None = None,
    name_match_guids: list[str] | None = None,
    legacy: bool = False,
    training_eligible: bool = False,
) -> dict:
    records = list(records or [])
    exact = {
        "status": status,
        "identity_match": "EXACT_PLAYER_GUID_ONLY",
        "record_count": len(records),
        "records": records,
    }
    if not legacy:
        captured = status != index_v1.RANKING_MISSING
        available = status == index_v1.EXACT_AVAILABLE
        conflict = status == index_v1.CAPTURED_GUID_ABSENT_NAME_CONFLICT
        guids = list(name_match_guids or [])
        exact.update(
            {
                "exact_dps_available": available,
                "usable_for_exact_dps_training": (
                    available and training_eligible
                ),
                "raid_contamination_training_eligible": training_eligible,
                "ranking_endpoint_captured": captured,
                "ranking_endpoint_capture_count": int(captured),
                "expected_name_exact_match_record_count": (
                    1 if available or conflict else 0
                ),
                "expected_name_match_guids": guids,
                "ranking_fetch_required": not captured,
                "negative_evidence_contract": "fixture",
            }
        )
    return {"ranking_exact_dps": exact}


def _membership(
    guid: str,
    instance_id: str,
    *,
    name: str = "同名战士",
    coverage: dict | None = None,
    guild_name: str | None = "南北",
    started_at: str = "2026-08-31T15:59:59Z",
    uploaded_at: str = "2026-09-02T00:00:00Z",
    query: bool = True,
) -> dict:
    guild = None if guild_name is None else {"id": "guild-1", "name": guild_name}
    label = ingest_v1.classify_range_bug(guild_name, started_at)
    row = {
        "character_guid": guid,
        "character_name": name,
        "instance_id": instance_id,
        "name": "Upper Tower of Karazhan",
        "started_at": started_at,
        "uploaded_at": uploaded_at,
        "guild": guild,
        "contamination_label": label,
        "performance": [
            {"encounter_name": "King", "dps_parse": 100, "hps_parse": 1}
        ],
        "coverage": coverage
        or _coverage(
            index_v1.EXACT_AVAILABLE,
            records=[_ranking()],
            training_eligible=(
                label in history_v1.TRAINING_CANDIDATE_LABELS
            ),
        ),
    }
    if query:
        row["history_version_audit"] = {
            "versions": [
                {
                    "selected": True,
                    "query": {
                        "server": "Capybara",
                        "realm": "Basin of Stars",
                        "character": name,
                    },
                }
            ]
        }
    return row


def _inventory(rows: list[dict], *, revision: str | None = None) -> dict:
    return {
        "schema": history_v1.INVENTORY_SCHEMA,
        "implementation_revision": (
            revision or history_v1.INVENTORY_IMPLEMENTATION_REVISION
        ),
        "instances": rows,
    }


class CharacterDpsIndexProjectionTests(unittest.TestCase):
    def test_exact_projection_preserves_fields_and_uses_started_at_for_label(self) -> None:
        inventory = _inventory([_membership(GUID_A, INSTANCE_A)])
        rows = list(index_v1.iter_character_dps_rows(inventory))
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(GUID_A, row["character_guid"])
        self.assertEqual("同名战士", row["character_name"])
        self.assertEqual("Capybara", row["server"])
        self.assertEqual("Basin of Stars", row["realm"])
        self.assertEqual(INSTANCE_A, row["instance_id"])
        self.assertEqual("Upper Tower of Karazhan", row["instance_name"])
        self.assertEqual("2026-08-31T15:59:59Z", row["started_at"])
        self.assertEqual("2026-09-02T00:00:00Z", row["uploaded_at"])
        # Upload is after the cutoff, but raid start is before it.
        self.assertEqual(ingest_v1.SUSPECT_36YD_RANGE_BUG, row["contamination_label"])
        exact = inventory["instances"][0]["coverage"]["ranking_exact_dps"]
        self.assertTrue(exact["exact_dps_available"])
        self.assertFalse(exact["raid_contamination_training_eligible"])
        self.assertFalse(exact["usable_for_exact_dps_training"])
        self.assertEqual("King", row["encounter_name"])
        self.assertEqual(120000.0, row["damage_done"])
        self.assertEqual(100.0, row["duration_secs"])
        self.assertEqual(1200.0, row["dps"])
        self.assertEqual("Fury", row["spec"])
        self.assertEqual("dps", row["role"])
        # The parse percentile exists but was neither emitted nor converted.
        self.assertNotIn("dps_parse", row)

    def test_fix_morning_boundary_is_explicit_nontraining_and_old_inventory_rejected(
        self,
    ) -> None:
        boundary = _membership(
            GUID_A,
            INSTANCE_A,
            name="托尼牛",
            started_at="2026-09-03T01:00:00Z",
        )
        rows = list(index_v1.iter_character_dps_rows(_inventory([boundary])))
        self.assertEqual(1, len(rows))
        self.assertEqual(
            ingest_v1.RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
            rows[0]["contamination_label"],
        )
        exact = boundary["coverage"]["ranking_exact_dps"]
        self.assertTrue(exact["exact_dps_available"])
        self.assertFalse(exact["raid_contamination_training_eligible"])
        self.assertFalse(exact["usable_for_exact_dps_training"])
        with self.assertRaisesRegex(
            index_v1.CharacterDpsIndexError,
            "current boundary-aware revision",
        ):
            index_v1.analyze_inventory(
                _inventory(
                    [boundary],
                    revision=(
                        history_v1.STREAM_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION
                    ),
                )
            )

    def test_three_explicit_missing_states_and_legacy_unknown_emit_no_rows(self) -> None:
        uncaptured = _membership(
            GUID_A,
            INSTANCE_A,
            coverage=_coverage(index_v1.RANKING_MISSING),
        )
        captured_absent = _membership(
            GUID_B,
            INSTANCE_A,
            coverage=_coverage(index_v1.CAPTURED_GUID_ABSENT),
        )
        name_conflict = _membership(
            GUID_C,
            INSTANCE_A,
            coverage=_coverage(
                index_v1.CAPTURED_GUID_ABSENT_NAME_CONFLICT,
                name_match_guids=[GUID_A],
            ),
        )
        legacy = _membership(
            "0x00000000000000D4",
            INSTANCE_A,
            coverage=_coverage(index_v1.RANKING_MISSING, legacy=True),
        )
        inventory = _inventory([uncaptured, captured_absent, name_conflict, legacy])
        analysis = index_v1.analyze_inventory(inventory)
        self.assertEqual(4, analysis.summary["missing_membership_count"])
        self.assertEqual(2, analysis.summary["censored_membership_count"])
        self.assertEqual(1, analysis.summary["uncaptured_membership_count"])
        self.assertEqual(
            1, analysis.summary["legacy_endpoint_state_unknown_membership_count"]
        )
        self.assertEqual(
            1, analysis.summary["name_conflict_censored_membership_count"]
        )
        self.assertEqual([], list(index_v1.iter_character_dps_rows(inventory)))
        self.assertEqual(
            {
                index_v1.RANKING_MISSING,
                index_v1.CAPTURED_GUID_ABSENT,
                index_v1.CAPTURED_GUID_ABSENT_NAME_CONFLICT,
                index_v1.LEGACY_EXPLICIT_STATE_UNKNOWN,
            },
            {row["missing_status"] for row in analysis.missing_memberships},
        )
        self.assertTrue(
            all("dps" not in row for row in analysis.missing_memberships)
        )

    def test_same_name_never_substitutes_for_exact_guid(self) -> None:
        exact = _membership(GUID_A, INSTANCE_A, name="重复名字")
        absent = _membership(
            GUID_B,
            INSTANCE_A,
            name="重复名字",
            coverage=_coverage(
                index_v1.CAPTURED_GUID_ABSENT_NAME_CONFLICT,
                name_match_guids=[GUID_A],
            ),
        )
        rows = list(index_v1.iter_character_dps_rows(_inventory([absent, exact])))
        self.assertEqual(1, len(rows))
        self.assertEqual(GUID_A, rows[0]["character_guid"])

    def test_duplicate_unique_key_is_rejected(self) -> None:
        duplicate = _ranking()
        membership = _membership(
            GUID_A,
            INSTANCE_A,
            coverage=_coverage(
                index_v1.EXACT_AVAILABLE,
                records=[duplicate, deepcopy(duplicate)],
            ),
        )
        with self.assertRaisesRegex(index_v1.CharacterDpsIndexError, "duplicate"):
            list(index_v1.iter_character_dps_rows(_inventory([membership])))

    def test_duplicate_membership_and_wrong_temporal_label_are_rejected(self) -> None:
        membership = _membership(GUID_A, INSTANCE_A)
        with self.assertRaisesRegex(index_v1.CharacterDpsIndexError, "duplicate"):
            index_v1.analyze_inventory(
                _inventory([membership, deepcopy(membership)])
            )
        poisoned = deepcopy(membership)
        poisoned["contamination_label"] = ingest_v1.POSTFIX_KNOWN_CLEAN
        with self.assertRaisesRegex(
            index_v1.CharacterDpsIndexError, r"guild \+ started_at"
        ):
            index_v1.analyze_inventory(_inventory([poisoned]))

    def test_partial_endpoint_contract_fails_closed(self) -> None:
        membership = _membership(
            GUID_A,
            INSTANCE_A,
            coverage=_coverage(index_v1.CAPTURED_GUID_ABSENT),
        )
        del membership["coverage"]["ranking_exact_dps"]["ranking_fetch_required"]
        with self.assertRaisesRegex(index_v1.CharacterDpsIndexError, "partial"):
            index_v1.analyze_inventory(_inventory([membership]))


@unittest.skipUnless(shutil.which("zstd"), "zstd executable is unavailable")
class CharacterDpsIndexPublicationTests(unittest.TestCase):
    def _source_inventory(self, root: Path) -> tuple[dict, Path]:
        inventory = history_v1._content_addressed(
            _inventory([_membership(GUID_A, INSTANCE_A)])
        )
        digest = history_v1._verify_content_address(
            inventory, label="fixture inventory"
        )
        directory = root / Path(history_v1.INVENTORY_MANIFEST_DIRECTORY)
        directory.mkdir(parents=True)
        path = directory / (
            f"{history_v1.INVENTORY_MANIFEST_PREFIX}.{digest}.manifest.json"
        )
        path.write_bytes(history_v1._canonical_document_bytes(inventory))
        return inventory, path.resolve()

    def test_publish_and_load_perform_exact_streaming_source_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "offline_data"
            inventory, inventory_path = self._source_inventory(root)

            def load_inventory(path, *, data_root):
                self.assertEqual(inventory_path, Path(path).resolve())
                self.assertEqual(root.resolve(), Path(data_root).resolve())
                return deepcopy(inventory), inventory_path

            with mock.patch.object(
                history_v1,
                "load_character_instance_inventory_manifest",
                side_effect=load_inventory,
            ):
                published = index_v1.publish_character_dps_index(
                    inventory_path, data_root=root
                )
                manifest, resolved = index_v1.load_character_dps_index_manifest(
                    published["manifest_path"], data_root=root
                )
                audited = index_v1.audit_character_dps_index(
                    published["manifest_path"], data_root=root
                )
            self.assertEqual(Path(published["manifest_path"]), resolved)
            self.assertEqual(1, manifest["partition"]["record_count"])
            self.assertTrue(str(published["partition_path"]).endswith(".jsonl.zst"))
            self.assertEqual(
                "PASS_STRICT_HASH_AND_SOURCE_REPLAY", audited["status"]
            )
            self.assertEqual(0, manifest["evidence_contract"]["synthetic_zero_dps_rows"])
            self.assertFalse(
                manifest["temporal_contract"]["uploaded_at_used_for_date_split"]
            )

            partition = Path(published["partition_path"])
            payload = bytearray(partition.read_bytes())
            payload[len(payload) // 2] ^= 1
            partition.write_bytes(payload)
            with mock.patch.object(
                history_v1,
                "load_character_instance_inventory_manifest",
                side_effect=load_inventory,
            ):
                with self.assertRaisesRegex(
                    index_v1.CharacterDpsIndexError, "compressed envelope"
                ):
                    index_v1.load_character_dps_index_manifest(
                        published["manifest_path"], data_root=root
                    )

    def test_changed_source_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "offline_data"
            inventory, inventory_path = self._source_inventory(root)

            def original(path, *, data_root):
                return deepcopy(inventory), inventory_path

            with mock.patch.object(
                history_v1,
                "load_character_instance_inventory_manifest",
                side_effect=original,
            ):
                published = index_v1.publish_character_dps_index(
                    inventory_path, data_root=root
                )

            changed = deepcopy(inventory)
            changed["instances"][0]["coverage"]["ranking_exact_dps"]["records"][0][
                "dps"
            ] = 9999

            def poisoned(path, *, data_root):
                return deepcopy(changed), inventory_path

            with mock.patch.object(
                history_v1,
                "load_character_instance_inventory_manifest",
                side_effect=poisoned,
            ):
                with self.assertRaisesRegex(
                    index_v1.CharacterDpsIndexError,
                    "source inventory",
                ):
                    index_v1.load_character_dps_index_manifest(
                        published["manifest_path"], data_root=root
                    )


if __name__ == "__main__":
    unittest.main()
