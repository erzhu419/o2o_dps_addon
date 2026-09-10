from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_historical_state_catalog_v1 import (
    DECLARED_ARMOR_SOURCE_WITHOUT_TARGET,
    FuryHistoricalStateCatalogError,
    MISSING_REASON,
    RECORD_SCHEMA,
    SCOPE,
    USE_CLASS,
    materialize_fury_historical_state_catalog,
    project_capture_row,
)


def _state(*, rich: bool = True, captured_at: float = 100.25) -> dict[str, object]:
    state: dict[str, object] = {
        "capturedAt": captured_at,
        "playerGUID": "0xPLAYER",
        "targetGUID": "0xTARGET",
        "classFile": "WARRIOR",
        "playerLevel": 60,
        "health": 4000,
        "maximumHealth": 5000,
        "power": 41,
        "rage": 41,
        "rageRaw": 418,
        "attackPower": {
            "base": 900,
            "positive": 300,
            "negative": 0,
            "effective": 1200,
        },
        "targetName": "Training Dummy",
        "targetClassification": "worldboss",
        "targetLevel": -1,
        "targetHealth": 100000,
        "targetMaximumHealth": 200000,
        "mainHandSpeed": 3.4,
        "cat2MainHandRemaining": 1.2,
        "gcd": 0,
        "cooldowns": {"bloodthirst": 0.0},
        "inCombat": True,
        "moving": False,
        "targetMeleeDistance": 0,
        "fieldProvenance": {
            "mainHandSpeed": "OBSERVED_GAME_API",
            "offHandSpeed": "OBSERVED_GAME_API",
            "playerLevel": "OBSERVED_GAME_API",
            "attackPower": "OBSERVED_GAME_API",
        },
    }
    if rich:
        state.update(
            {
                "characterIdentity": {
                    "name": "Warrior",
                    "classFile": "WARRIOR",
                    "level": 60,
                    "sex": 3,
                },
                "equipment": [
                    {
                        "slot": 16,
                        "link": "|cffa335ee|Hitem:17076:1900:0:0|h[Blade]|h|r",
                    }
                ],
                "talents": [
                    {
                        "tab": 2,
                        "index": 15,
                        "rank": 5,
                        "maxRank": 5,
                        "tier": 6,
                        "column": 2,
                        "name": "Flurry",
                    }
                ],
                "skillLines": [
                    {
                        "index": 1,
                        "rank": 308,
                        "maximum": 308,
                        "modifier": 0,
                        "temporary": 0,
                        "isHeader": False,
                        "isExpanded": False,
                        "name": "Two-Handed Swords",
                    }
                ],
                "spellbook": [
                    {
                        "spellbookIndex": 21,
                        "name": "Slam",
                        # Empty rank is a legitimate WoW spellbook value for
                        # unranked abilities and must not be treated as absent.
                        "rank": "",
                        "texture": "Interface\\Icons\\Ability",
                        "tooltipText": "Slam text",
                    }
                ],
                "playerAuras": ["Flurry"],
                "targetAuras": ["Sunder Armor"],
                "targetArmor": {
                    "base": 4211,
                    "positive": 0,
                    "negative": 2250,
                    "armor": 1961,
                    "effective": 1961,
                },
            }
        )
        state["fieldProvenance"].update(
            {
                "characterIdentity": "OBSERVED_UNIT_API",
                "equipment": "OBSERVED_INVENTORY_LINK",
                "talents": "OBSERVED_TALENT_API",
                "skillLines": "OBSERVED_SKILL_API",
                "spellbook": "OBSERVED_SPELLBOOK_API_TOOLTIP",
                "playerAuras": "OBSERVED_AURA_SCAN",
                "targetAuras": "OBSERVED_AURA_SCAN",
                "targetArmor": "OBSERVED_GAME_API_WHEN_AVAILABLE",
            }
        )
    return state


def _row(
    sequence: int,
    *,
    rich: bool = True,
    captured_at: float | None = None,
    task_run_id: str = "task-run-1",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "event": "CALIBRATION_TRIAL_COMPLETED",
        "state": _state(
            rich=rich,
            captured_at=captured_at if captured_at is not None else 100.0 + sequence,
        ),
        "task": {
            "taskRunId": task_run_id,
            "taskId": "warrior_test",
            "campaignRunId": "campaign-run-1",
            "campaignId": "campaign-1",
            "trial": 1,
        },
        "marker": {
            "taskRunId": task_run_id,
            "taskId": "warrior_test",
            "campaignRunId": "campaign-run-1",
            "campaignId": "campaign-1",
            "trial": 1,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_fixture(
    root: Path,
    rows: list[dict[str, object]],
    *,
    completion_range: tuple[int, int] = (1, 99),
) -> tuple[Path, Path, Path, Path, Path]:
    calibration = root / "calibration"
    summaries = root / "summaries"
    calibration.mkdir()
    summaries.mkdir()
    source = calibration / "BrainOfCat__fixture.jsonl"
    source.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    summary = {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "source": {"calibration_jsonl": str(source)},
        "task_completions": [
            {
                "task_run_id": "task-run-1",
                "status": "completed",
                "completion_source": "automatic_typed_event",
                "sequence_range": {
                    "start": completion_range[0],
                    "end": completion_range[1],
                },
            }
        ],
    }
    _write_json(summaries / "BrainOfCat__fixture.json", summary)
    output = root / "out" / "states.jsonl"
    manifest = root / "out" / "manifest.json"
    report = root / "reports" / "report.json"
    return calibration, summaries, output, manifest, report


class FuryHistoricalStateCatalogTests(unittest.TestCase):
    def test_projection_is_capture_bound_and_parses_enchant_identity(self) -> None:
        projected = project_capture_row(
            _row(7),
            source_name="BrainOfCat__fixture.jsonl",
            source_sha256="a" * 64,
            source_line=1,
        )
        self.assertEqual(projected["schema"], RECORD_SCHEMA)
        self.assertEqual(projected["evidence_scope"], SCOPE)
        self.assertEqual(projected["use_class"], USE_CLASS)
        equipment = projected["state"]["equipment"]
        self.assertEqual(equipment[0]["item_id"], 17076)
        self.assertEqual(equipment[0]["enchant_id"], 1900)
        self.assertEqual(projected["state"]["talents"][0]["rank"], 5)
        self.assertEqual(projected["state"]["skills"]["spellbook"][0]["rank"], "")
        self.assertEqual(projected["state"]["combat"]["player_aura_names"], ["Flurry"])
        self.assertFalse(projected["gates"]["training_eligible"])
        self.assertFalse(projected["gates"]["expert_vote_eligible"])
        self.assertFalse(projected["gates"]["deployment_eligible"])

    def test_missing_rich_fields_are_null_and_never_carried_from_previous_row(self) -> None:
        rich = project_capture_row(
            _row(1, rich=True),
            source_name="BrainOfCat__fixture.jsonl",
            source_sha256="b" * 64,
            source_line=1,
        )
        sparse = project_capture_row(
            _row(2, rich=False),
            source_name="BrainOfCat__fixture.jsonl",
            source_sha256="b" * 64,
            source_line=2,
        )
        self.assertIsNotNone(rich["state"]["equipment"])
        self.assertIsNone(sparse["state"]["equipment"])
        self.assertIsNone(sparse["state"]["talents"])
        self.assertIsNone(sparse["state"]["combat"]["player_aura_names"])
        self.assertIn("equipment", sparse["missing_mask"]["missing_fields"])
        self.assertEqual(sparse["missing_mask"]["reason"], MISSING_REASON)

    def test_declared_conditional_armor_source_without_target_is_not_observed(self) -> None:
        row = _row(2, rich=False)
        row["state"]["targetExists"] = False
        row["state"]["fieldProvenance"][
            "targetArmor"
        ] = "OBSERVED_GAME_API_WHEN_AVAILABLE"
        projected = project_capture_row(
            row,
            source_name="BrainOfCat__fixture.jsonl",
            source_sha256="b" * 64,
            source_line=2,
        )
        self.assertIsNone(projected["state"]["target"]["armor"])
        self.assertIn("target.armor", projected["missing_mask"]["missing_fields"])
        self.assertEqual(
            projected["field_provenance"]["target.armor"],
            DECLARED_ARMOR_SOURCE_WITHOUT_TARGET,
        )
        self.assertNotEqual(
            projected["field_provenance"]["target.armor"],
            "OBSERVED_GAME_API_WHEN_AVAILABLE",
        )

    def test_conflicting_task_and_marker_identity_is_rejected(self) -> None:
        row = _row(1)
        row["marker"]["taskRunId"] = "different-run"
        with self.assertRaisesRegex(FuryHistoricalStateCatalogError, "conflicting"):
            project_capture_row(
                row,
                source_name="BrainOfCat__fixture.jsonl",
                source_sha256="c" * 64,
                source_line=1,
            )

    def test_summary_binds_only_by_source_task_and_sequence_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calibration, summaries, output, manifest, report = _write_fixture(
                root, [_row(1), _row(2)], completion_range=(1, 1)
            )
            materialize_fury_historical_state_catalog(
                calibration,
                summaries,
                output_path=output,
                manifest_path=manifest,
                report_path=report,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                rows[0]["binding"]["summary_binding_status"],
                "EXACT_SOURCE_TASK_RUN_AND_SEQUENCE_RANGE",
            )
            self.assertEqual(
                rows[0]["binding"]["summary_completion"]["status"],
                "completed",
            )
            self.assertEqual(
                rows[1]["binding"]["summary_binding_status"],
                "SEQUENCE_OUTSIDE_TASK_COMPLETION_RANGE",
            )
            self.assertIsNone(rows[1]["binding"]["summary_completion"])

    def test_materialization_is_deterministic_and_manifest_is_written_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calibration, summaries, output, manifest, report = _write_fixture(
                root, [_row(1), _row(2, rich=False)]
            )
            first = materialize_fury_historical_state_catalog(
                calibration,
                summaries,
                output_path=output,
                manifest_path=manifest,
                report_path=report,
            )
            first_bytes = (output.read_bytes(), manifest.read_bytes(), report.read_bytes())
            second = materialize_fury_historical_state_catalog(
                calibration,
                summaries,
                output_path=output,
                manifest_path=manifest,
                report_path=report,
            )
            self.assertEqual(first_bytes, (output.read_bytes(), manifest.read_bytes(), report.read_bytes()))
            self.assertEqual(first["dataset_sha256"], second["dataset_sha256"])
            manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
            report_doc = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(manifest_doc["commit"]["manifest_written_last"])
            self.assertTrue(manifest_doc["commit"]["content_addressed"])
            self.assertEqual(
                manifest_doc["dataset"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest()
            )
            self.assertEqual(
                manifest_doc["report"]["sha256"], hashlib.sha256(report.read_bytes()).hexdigest()
            )
            self.assertEqual(manifest_doc["dataset"]["path_base"], "manifest_parent")
            self.assertEqual(manifest_doc["report"]["path_base"], "manifest_parent")
            self.assertEqual(
                (manifest.parent / manifest_doc["dataset"]["path"]).resolve(),
                output.resolve(),
            )
            self.assertEqual(
                (manifest.parent / manifest_doc["report"]["path"]).resolve(),
                report.resolve(),
            )
            calibration_root = (
                manifest.parent / manifest_doc["inputs"]["calibration_jsonl_root"]
            ).resolve()
            summary_root = (
                manifest.parent
                / manifest_doc["inputs"]["calibration_summaries_root"]
            ).resolve()
            self.assertEqual(
                manifest_doc["inputs"]["calibration_jsonl_path_base"],
                "manifest_parent",
            )
            self.assertEqual(
                manifest_doc["inputs"]["calibration_summaries_path_base"],
                "manifest_parent",
            )
            self.assertEqual(calibration_root, calibration.resolve())
            self.assertEqual(summary_root, summaries.resolve())
            self.assertEqual(
                (calibration_root / manifest_doc["inputs"]["calibration_jsonl"][0]["path"]).resolve(),
                next(calibration.glob("*.jsonl")).resolve(),
            )
            self.assertEqual(
                (summary_root / manifest_doc["inputs"]["calibration_summaries"][0]["path"]).resolve(),
                next(summaries.glob("*.json")).resolve(),
            )
            self.assertFalse(report_doc["binding_contract"]["shadow_transition_join"])
            self.assertEqual(report_doc["shadow_exclusion"]["shadow_rows_backfilled"], 0)
            self.assertFalse(report_doc["eligibility"]["training_eligible"])
            self.assertEqual(
                report_doc["coverage"]["groups"]["requested_core_same_capture"], 1
            )
            self.assertEqual(
                report_doc["coverage"]["groups"][
                    "requested_core_plus_skills_same_capture"
                ],
                1,
            )

    def test_source_mutation_changes_bundle_and_capture_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calibration, summaries, output, manifest, report = _write_fixture(root, [_row(1)])
            first = materialize_fury_historical_state_catalog(
                calibration,
                summaries,
                output_path=output,
                manifest_path=manifest,
                report_path=report,
            )
            first_capture = json.loads(output.read_text(encoding="utf-8"))["capture_id"]
            source = calibration / "BrainOfCat__fixture.jsonl"
            changed = _row(1)
            changed["state"]["power"] = 42
            source.write_text(
                json.dumps(changed, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            second = materialize_fury_historical_state_catalog(
                calibration,
                summaries,
                output_path=output,
                manifest_path=manifest,
                report_path=report,
            )
            second_capture = json.loads(output.read_text(encoding="utf-8"))["capture_id"]
            self.assertNotEqual(first["input_bundle_sha256"], second["input_bundle_sha256"])
            self.assertNotEqual(first_capture, second_capture)

    def test_duplicate_json_key_and_nonfinite_number_are_rejected(self) -> None:
        for invalid_line, pattern in (
            ('{"sequence":1,"sequence":2,"event":"x","state":{}}\n', "duplicate"),
            ('{"sequence":1,"event":"x","state":{"capturedAt":NaN}}\n', "non-finite"),
        ):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calibration = root / "calibration"
                summaries = root / "summaries"
                calibration.mkdir()
                summaries.mkdir()
                (calibration / "BrainOfCat__bad.jsonl").write_text(
                    invalid_line, encoding="utf-8", newline="\n"
                )
                with self.assertRaisesRegex(FuryHistoricalStateCatalogError, pattern):
                    materialize_fury_historical_state_catalog(
                        calibration,
                        summaries,
                        output_path=root / "out.jsonl",
                        manifest_path=root / "manifest.json",
                        report_path=root / "report.json",
                    )

    def test_outputs_cannot_alias_each_other_or_an_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calibration, summaries, output, _manifest, report = _write_fixture(root, [_row(1)])
            with self.assertRaisesRegex(FuryHistoricalStateCatalogError, "must differ"):
                materialize_fury_historical_state_catalog(
                    calibration,
                    summaries,
                    output_path=output,
                    manifest_path=output,
                    report_path=report,
                )


if __name__ == "__main__":
    unittest.main()
