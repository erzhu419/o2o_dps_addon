from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_character_dps_index_v1 as dps_index_v1
from o2o_dps import historical_warrior_reference_cohort_v1 as reference_v1


GUID_A = "0x000000000056DEB3"
GUID_B = "0x00000000007395BF"


def _config() -> dict:
    return {
        "schema": reference_v1.CONFIG_SCHEMA,
        "implementation_revision": reference_v1.CONFIG_REVISION,
        "instance_name": "Upper Tower of Karazhan",
        "selection": {
            "identity_seed": "exact_character_name_then_pinned_exact_character_guid",
            "identity_after_resolution": "exact_character_guid_only",
            "raid_date_field": "started_at",
            "started_at_not_before_local": ingest_v1.range_bug_boundary_contract()[
                "postfix_known_clean_at_or_after_local"
            ],
            "accepted_contamination_labels": [ingest_v1.POSTFIX_KNOWN_CLEAN],
            "pre_fix_rows": "COUNT_AS_EXCLUDED_EVIDENCE_NEVER_SELECT",
        },
        "players": [
            {
                "requested_name": "托尼牛",
                "expected_character_guid": GUID_A,
                "purpose": "reference_a",
            }
        ],
        "use_boundaries": {
            "identity_and_raid_reference": True,
            "fury_policy_baseline": False,
            "action_trace_available": False,
            "queue_intent_available": False,
            "direct_same_spec_dps_win_loss": False,
        },
    }


def _manifest(count: int) -> dict:
    return {
        "schema": dps_index_v1.SCHEMA,
        "implementation_revision": dps_index_v1.IMPLEMENTATION_REVISION,
        "content_address": {"sha256": "a" * 64},
        "partition": {"record_count": count},
        "summary": {"training_eligible_exact_membership_count": 1},
    }


def _row(
    *,
    guid: str = GUID_A,
    name: str = "托尼牛",
    instance_id: str = "11111111-1111-1111-1111-111111111111",
    ranking_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    started_at: str = "2026-09-09T13:37:19.228Z",
    spec: str = "Arms",
    role: str = "dps",
    damage: float = 1000.0,
    duration: float = 2.0,
) -> dict:
    guild = {"id": "guild-1", "name": "南北"}
    return {
        "character_guid": guid,
        "character_name": name,
        "server": "Capybara",
        "realm": "Basin of Stars",
        "instance_id": instance_id,
        "instance_name": "Upper Tower of Karazhan",
        "started_at": started_at,
        "uploaded_at": "2026-09-09T14:47:39.154657Z",
        "guild": guild,
        "contamination_label": ingest_v1.classify_range_bug(
            guild["name"], started_at
        ),
        "encounter_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "encounter_name": "Boss",
        "killed_at": "2026-09-09T13:47:59.021Z",
        "damage_done": damage,
        "duration_secs": duration,
        "dps": damage / duration,
        "spec": spec,
        "role": role,
        "ranking_record_id": ranking_id,
    }


def _build(rows: list[dict], config: dict | None = None) -> dict:
    return reference_v1.build_reference_document(
        _manifest(len(rows)),
        rows,
        config or _config(),
        source_manifest_path="derived/index/manifest.json",
        config_path="configs/experts/reference.json",
    )


class HistoricalWarriorReferenceCohortV1Tests(unittest.TestCase):
    def test_exact_guid_resolution_preserves_arms_and_excludes_prefix_rows(self) -> None:
        clean = _row()
        clean_tank = _row(
            ranking_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            role="tank",
            damage=600.0,
        )
        suspect = _row(
            instance_id="22222222-2222-2222-2222-222222222222",
            ranking_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            started_at="2026-09-02T13:40:30.768Z",
            damage=2000.0,
        )
        document = _build([clean, clean_tank, suspect])
        player = document["players"][0]

        self.assertEqual(GUID_A, player["character_guid"])
        self.assertEqual(["Arms"], player["selected_specs"])
        self.assertEqual(["dps", "tank"], player["selected_roles"])
        self.assertEqual(2, player["selected_postfix_known_clean_observation_count"])
        self.assertEqual(
            {ingest_v1.SUSPECT_36YD_RANGE_BUG: 1},
            player["excluded_observations_by_contamination_label"],
        )
        self.assertEqual(1, document["summary"]["excluded_suspect_observation_count"])
        self.assertEqual("started_at", document["selection_contract"]["raid_date_field"])
        self.assertFalse(
            document["selection_contract"]["uploaded_at_used_for_date_split"]
        )

    def test_name_is_only_seed_and_later_alias_is_joined_by_exact_guid(self) -> None:
        alias = _row(
            name="托尼牛改名",
            instance_id="22222222-2222-2222-2222-222222222222",
            ranking_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        )
        document = _build([_row(), alias])
        player = document["players"][0]

        self.assertEqual(["托尼牛", "托尼牛改名"], player["identity"]["observed_character_names"])
        self.assertEqual(2, player["selected_postfix_known_clean_raid_count"])
        self.assertEqual(
            "EXACT_CONFIGURED_NAME_TO_ONE_PINNED_GUID_THEN_GUID_ONLY",
            player["identity"]["resolution"],
        )

    def test_same_name_on_two_guids_fails_instead_of_name_joining(self) -> None:
        collision = _row(
            guid=GUID_B,
            ranking_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        )
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "does not resolve to exactly pinned GUID",
        ):
            _build([_row(), collision])

    def test_tampered_contamination_label_is_rejected(self) -> None:
        row = _row()
        row["contamination_label"] = ingest_v1.SUSPECT_36YD_RANGE_BUG
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "contamination_label disagrees",
        ):
            _build([row])

    def test_no_postfix_clean_row_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "no post-fix clean reference rows",
        ):
            _build([_row(started_at="2026-09-02T13:40:30.768Z")])

    def test_config_cannot_relabel_prefix_rows_as_selectable(self) -> None:
        config = _config()
        config["selection"]["pre_fix_rows"] = "SELECT"
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "pre-fix exclusion was weakened",
        ):
            _build([_row()], config)

    def test_reference_never_becomes_fury_policy_or_direct_dps_vote(self) -> None:
        document = _build([_row()])
        boundaries = document["scientific_use_boundaries"]
        player_boundaries = document["players"][0]["reference_boundaries"]

        self.assertTrue(boundaries["historical_identity_performance_and_raid_reference"])
        self.assertFalse(boundaries["fury_policy_baseline"])
        self.assertFalse(boundaries["direct_same_spec_dps_win_loss_eligible"])
        self.assertFalse(boundaries["training_eligible"])
        self.assertEqual(
            reference_v1.ACTION_TRACE_STATUS,
            player_boundaries["controllable_action_trace_status"],
        )
        self.assertEqual(
            reference_v1.QUEUE_INTENT_STATUS,
            player_boundaries["next_swing_queue_intent_status"],
        )
        self.assertEqual(
            reference_v1.TARGET_INTENT_STATUS,
            player_boundaries["target_switch_intent_status"],
        )
        self.assertIn("External-V2 timeline", player_boundaries["reason"])
        self.assertIn("START/GO/FAIL", player_boundaries["reason"])
        self.assertIn("server-observed", player_boundaries["reason"])
        self.assertIn("do not expose those client intents", player_boundaries["reason"])

        document["scientific_use_boundaries"]["fury_policy_baseline"] = True
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "fury_policy_baseline must be false",
        ):
            reference_v1.validate_reference_document(document)

    def test_main_publishes_small_receipt_under_offline_data(self) -> None:
        document = _build([_row()])
        with tempfile.TemporaryDirectory(prefix="historical_warrior_reference_") as tmp:
            root = Path(tmp) / "offline_data"
            root.mkdir()
            output = root / "derived" / "reference.json"
            with mock.patch.object(
                reference_v1, "build_reference_from_index", return_value=document
            ):
                stdout = io.StringIO()
                result = reference_v1.main(
                    [
                        "--dps-index-manifest",
                        str(root / "index.json"),
                        "--config",
                        "configs/experts/reference.json",
                        "--data-root",
                        str(root),
                        "--output",
                        str(output),
                    ],
                    stdout=stdout,
                )
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(0, result)
            self.assertEqual(
                "PUBLISHED_REFERENCE_ONLY_NOT_POLICY_OR_DPS_COMPARISON",
                receipt["status"],
            )
            self.assertNotIn("players", receipt)
            self.assertEqual(document, json.loads(output.read_text(encoding="utf-8")))

    def test_duplicate_exact_dps_key_is_rejected(self) -> None:
        row = _row()
        with self.assertRaisesRegex(
            reference_v1.HistoricalWarriorReferenceError,
            "unique key is duplicated",
        ):
            _build([row, deepcopy(row)])


if __name__ == "__main__":
    unittest.main()
