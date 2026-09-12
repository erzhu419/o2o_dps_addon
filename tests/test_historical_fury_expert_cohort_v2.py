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
from o2o_dps import historical_fury_expert_cohort_v2 as cohort_v2


GUID_A = "0x00000000000000A1"
GUID_B = "0x00000000000000B2"
INSTANCE_A = "11111111-1111-1111-1111-111111111111"
INSTANCE_B = "22222222-2222-2222-2222-222222222222"
ENCOUNTER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _manifest(row_count: int) -> dict:
    return {
        "schema": dps_index_v1.SCHEMA,
        "implementation_revision": dps_index_v1.IMPLEMENTATION_REVISION,
        "content_address": {"sha256": "a" * 64},
        "partition": {
            "path": "derived/index/fixture.jsonl.zst",
            "record_count": row_count,
        },
        "summary": {
            "exact_ranking_record_count": row_count,
            "training_eligible_exact_membership_count": row_count,
            "censored_membership_count": 0,
        },
    }


def _row(
    *,
    guid: str = GUID_A,
    name: str = "玩家甲",
    instance_id: str = INSTANCE_A,
    encounter_id: str | None = ENCOUNTER_A,
    ranking_id: str = "00000000-0000-0000-0000-000000000001",
    started_at: str = "2026-09-03T20:00:00+08:00",
    uploaded_at: str = "2026-09-04T00:00:00Z",
    guild_name: str | None = "南北",
    guild_id: str | None = "guild-1",
    server: str | None = "Capybara",
    realm: str | None = "Basin of Stars",
    instance_name: str = cohort_v2.FROZEN_INSTANCE_NAME,
    spec: str = cohort_v2.FROZEN_SPEC,
    role: str = cohort_v2.FROZEN_ROLE,
    damage: float = 100_000.0,
    duration: float = 100.0,
) -> dict:
    return {
        "character_guid": guid,
        "character_name": name,
        "server": server,
        "realm": realm,
        "instance_id": instance_id,
        "instance_name": instance_name,
        "started_at": started_at,
        "uploaded_at": uploaded_at,
        "guild": (
            None if guild_name is None else {"id": guild_id, "name": guild_name}
        ),
        "contamination_label": ingest_v1.classify_range_bug(
            guild_name, started_at
        ),
        "encounter_id": encounter_id,
        "encounter_name": "Boss",
        "killed_at": "2026-09-03T20:02:00+08:00",
        "damage_done": damage,
        "duration_secs": duration,
        "dps": damage / duration,
        "spec": spec,
        "role": role,
        "ranking_record_id": ranking_id,
    }


def _build(rows: list[dict]) -> dict:
    return cohort_v2.build_cohort_document(
        _manifest(len(rows)), rows, source_manifest_path="derived/index/manifest.json"
    )


class HistoricalFuryExpertCohortV2Tests(unittest.TestCase):
    def test_freezes_only_postfix_exact_fury_dps_rows_by_started_at(self) -> None:
        rows = [
            _row(),
            _row(
                guid=GUID_B,
                name="上传晚但开打早",
                ranking_id="00000000-0000-0000-0000-000000000002",
                started_at="2026-09-02T20:00:00+08:00",
                uploaded_at="2026-09-10T00:00:00Z",
                guild_name="其他公会",
            ),
            _row(
                guid=GUID_B,
                name="武器战",
                ranking_id="00000000-0000-0000-0000-000000000003",
                spec="Arms",
            ),
            _row(
                guid=GUID_B,
                name="治疗",
                ranking_id="00000000-0000-0000-0000-000000000004",
                role="healer",
            ),
            _row(
                guid=GUID_B,
                name="其他副本",
                ranking_id="00000000-0000-0000-0000-000000000005",
                instance_name="Molten Core",
            ),
        ]

        document = _build(rows)

        self.assertEqual(1, document["summary"]["unique_player_candidate_count"])
        self.assertEqual(
            {
                "before_frozen_postfix_cutoff": 1,
                "other_instance": 1,
                "other_role": 1,
                "other_spec": 1,
            },
            document["summary"]["filtered_out_row_counts"],
        )
        contract = document["frozen_cohort_contract"]
        self.assertEqual("started_at", contract["raid_date_field"])
        self.assertFalse(contract["uploaded_at_used_for_date_split"])
        self.assertEqual(
            ingest_v1.range_bug_boundary_contract()[
                "postfix_known_clean_at_or_after_local"
            ],
            contract["started_at_not_before_local"],
        )

    def test_exact_guid_is_the_player_key_and_raids_get_equal_summary_weight(self) -> None:
        rows = [
            _row(damage=1_000.0, duration=1.0),
            _row(
                guid=GUID_B,
                name="玩家乙",
                ranking_id="00000000-0000-0000-0000-000000000002",
                damage=500.0,
                duration=1.0,
            ),
            _row(
                guid=GUID_A,
                name="玩家甲改名",
                instance_id=INSTANCE_B,
                encounter_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                ranking_id="00000000-0000-0000-0000-000000000003",
                started_at="2026-09-09T20:00:00+08:00",
                damage=900.0,
                duration=1.0,
            ),
            _row(
                guid=GUID_B,
                name="玩家乙",
                instance_id=INSTANCE_B,
                encounter_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                ranking_id="00000000-0000-0000-0000-000000000004",
                started_at="2026-09-09T20:00:00+08:00",
                damage=900.0,
                duration=1.0,
            ),
        ]

        document = _build(rows)
        candidates = {
            candidate["character_guid"].lower(): candidate
            for candidate in document["player_candidates"]
        }
        candidate = candidates[GUID_A.lower()]

        self.assertEqual(f"player:{GUID_A.lower()}", candidate["candidate_id"])
        self.assertEqual(["玩家甲", "玩家甲改名"], candidate["observed_character_names"])
        self.assertEqual(2, candidate["raid_count"])
        # Raid A: 1000 / median(1000, 500) = 4/3. Raid B: 900/900 = 1.
        # The player statistic is the median of the two raid-level values, so
        # the two raids get equal weight despite their different DPS scale.
        self.assertAlmostEqual(
            7.0 / 6.0,
            candidate["median_equal_raid_local_fury_dps_ratio"],
        )
        self.assertEqual(["character_guid"], document[
            "deduplication_and_weighting_contract"
        ]["player_identity"])
        self.assertEqual(
            ["character_guid", "instance_id", "ranking_record_id"],
            document["deduplication_and_weighting_contract"][
                "source_observation_unique_key"
            ],
        )

    def test_same_guid_with_conflicting_server_or_realm_is_rejected(self) -> None:
        rows = [
            _row(),
            _row(
                name="玩家甲改名",
                instance_id=INSTANCE_B,
                encounter_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                ranking_id="00000000-0000-0000-0000-000000000002",
                started_at="2026-09-09T20:00:00+08:00",
                realm="Conflicting Realm",
            ),
        ]

        with self.assertRaisesRegex(
            cohort_v2.HistoricalFuryExpertCohortError,
            "one exact character GUID has conflicting server or realm evidence",
        ):
            _build(rows)

    def test_same_evidence_duplicates_collapse_but_conflicts_fail(self) -> None:
        first = _row()
        duplicate = deepcopy(first)
        document = _build([first, duplicate])

        self.assertEqual(1, document["summary"]["same_evidence_duplicate_count_collapsed"])
        self.assertEqual(
            1,
            document["summary"]["selected_exact_fury_encounter_observation_count"],
        )

        conflict = deepcopy(duplicate)
        conflict["damage_done"] = 200_000.0
        conflict["dps"] = 2_000.0
        with self.assertRaisesRegex(
            cohort_v2.HistoricalFuryExpertCohortError,
            "conflicting rows share the exact DPS source unique key",
        ):
            _build([first, conflict])

    def test_null_trash_encounter_ids_are_preserved_as_distinct_windows(self) -> None:
        rows = [
            _row(encounter_id=None, damage=1_000.0, duration=1.0),
            _row(
                encounter_id=None,
                ranking_id="00000000-0000-0000-0000-000000000002",
                damage=600.0,
                duration=1.0,
            ),
            _row(
                guid=GUID_B,
                name="无公会证据",
                encounter_id=None,
                ranking_id="00000000-0000-0000-0000-000000000003",
                guild_name=None,
            ),
        ]
        rows[0]["killed_at"] = "2026-09-03T20:02:00+08:00"
        rows[1]["killed_at"] = "2026-09-03T20:04:00+08:00"

        document = _build(rows)

        candidate = document["player_candidates"][0]
        self.assertEqual(2, candidate["encounter_observation_count"])
        self.assertEqual(0, candidate["raids"][0]["local_peer_comparable_encounter_count"])
        self.assertEqual(
            {"contamination_nontraining": 1},
            document["summary"]["filtered_out_row_counts"],
        )
        self.assertEqual(
            "INSTANCE_PLUS_ENCOUNTER_ID_OR_NULL_ID_TRASH_NAME_AND_KILLED_AT",
            document["deduplication_and_weighting_contract"][
                "peer_comparison_stratum"
            ],
        )

    def test_dps_index_never_becomes_action_queue_or_complete_expert_evidence(self) -> None:
        document = _build([_row()])
        prototype = document["behavior_prototype_candidates"][0]
        boundaries = document["uncertainty_and_use_boundaries"]

        self.assertEqual(
            cohort_v2.ACTION_TRACE_MISSING,
            prototype["controllable_action_trace_status"],
        )
        self.assertEqual(cohort_v2.QUEUE_UNKNOWN, prototype["queue_intent_status"])
        self.assertEqual(
            cohort_v2.QUEUE_UNKNOWN, prototype["target_switch_intent_status"]
        )
        self.assertFalse(prototype["closed_loop_policy_training_eligible"])
        self.assertEqual(
            cohort_v2.COMPLETE_STRATEGY_REFUSED,
            prototype["complete_expert_strategy_claim"],
        )
        self.assertFalse(boundaries["pooled_behavior_cloning_policy_authorized"])
        self.assertFalse(boundaries["top_one_dps_as_complete_strategy_authorized"])
        self.assertFalse(boundaries["closed_loop_baseline_or_comparison_authorized"])
        self.assertFalse(boundaries["training_or_superiority_claim_authorized"])

    def test_invalid_dps_or_partition_count_fails_closed(self) -> None:
        bad_dps = _row()
        bad_dps["dps"] = 999.0
        with self.assertRaisesRegex(
            cohort_v2.HistoricalFuryExpertCohortError,
            "row.dps disagrees",
        ):
            _build([bad_dps])

        with self.assertRaisesRegex(
            cohort_v2.HistoricalFuryExpertCohortError,
            "streamed DPS row count disagrees",
        ):
            cohort_v2.build_cohort_document(
                _manifest(2),
                [_row()],
                source_manifest_path="derived/index/manifest.json",
            )

    def test_validator_rejects_policy_claim_tampering(self) -> None:
        document = _build([_row()])
        document["behavior_prototype_candidates"][0][
            "closed_loop_policy_training_eligible"
        ] = True
        with self.assertRaisesRegex(
            cohort_v2.HistoricalFuryExpertCohortError,
            "cannot train a closed-loop policy",
        ):
            cohort_v2.validate_cohort_document(document)

    def test_main_build_emits_small_receipt_without_embedding_the_cohort(self) -> None:
        document = _build([_row()])
        with tempfile.TemporaryDirectory(prefix="offline_data_cohort_v2_") as directory:
            root = Path(directory) / "offline_data"
            root.mkdir()
            output = root / "derived" / "cohort.json"
            with mock.patch.object(
                cohort_v2,
                "build_cohort_from_index",
                return_value=document,
            ):
                stdout = io.StringIO()
                result = cohort_v2.main(
                    [
                        "build",
                        "--dps-index-manifest",
                        str(root / "source.json"),
                        "--data-root",
                        str(root),
                        "--output",
                        str(output),
                    ],
                    stdout=stdout,
                )
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(
                "PUBLISHED_IDENTITY_AND_PERFORMANCE_COHORT_NOT_POLICY",
                receipt["status"],
            )
            self.assertNotIn("player_candidates", receipt)
            self.assertEqual(document, json.loads(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
