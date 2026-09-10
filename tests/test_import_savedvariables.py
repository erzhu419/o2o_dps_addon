from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.import_savedvariables import (
    SavedVariablesSchemaError,
    SavedVariablesSyntaxError,
    _Parser,
    _extract_records,
    import_savedvariables,
    main,
    publish_shadow_pairs,
)


FIXTURE = Path(__file__).parent / "fixtures" / "BrainOfCat.lua"


class SavedVariablesImportTests(unittest.TestCase):
    def test_compact_shadow_pair_journal_is_incremental_and_has_no_raw_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_root = root / "offline_data"
            source_a = root / "AccountA" / "BrainOfCat.lua"
            source_b = root / "AccountB" / "BrainOfCat.lua"
            source_a.parent.mkdir()
            source_b.parent.mkdir()
            fixture_text = FIXTURE.read_text(encoding="utf-8")
            source_a.write_text(fixture_text, encoding="utf-8")
            source_b.write_text(fixture_text, encoding="utf-8")
            assignments = _Parser(
                fixture_text, FIXTURE.name
            ).parse()
            decisions, _, _ = _extract_records(assignments)

            first = publish_shadow_pairs(
                decisions,
                source_lua=source_a,
                data_root=data_root,
                source_signature={"mtime_ns": 10, "size_bytes": 20},
            )
            second = publish_shadow_pairs(
                decisions,
                source_lua=source_a,
                data_root=data_root,
                source_signature={"mtime_ns": 11, "size_bytes": 20},
            )
            other_source = publish_shadow_pairs(
                decisions,
                source_lua=source_b,
                data_root=data_root,
                source_signature={"mtime_ns": 12, "size_bytes": 20},
            )

            self.assertEqual(first.source_pair_total, 1)
            self.assertEqual(first.new_pair_count, 1)
            self.assertEqual(first.journal_pair_total, 1)
            self.assertEqual(second.new_pair_count, 0)
            self.assertEqual(second.journal_pair_total, 1)
            self.assertEqual(other_source.new_pair_count, 1)
            self.assertEqual(other_source.journal_pair_total, 2)
            rows = [
                json.loads(line)
                for line in first.output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [row["decisionId"] for row in rows],
                ["boc-decision-17", "boc-decision-17"],
            )
            self.assertEqual(
                len({row["provenance"]["source_identity"] for row in rows}), 2
            )
            self.assertEqual(rows[0]["provenance"]["kind"], "shadow_decision_pair")
            self.assertEqual(rows[0]["shadowSample"]["status"], "confirmed")
            self.assertTrue(rows[0]["shadowSample"]["materialized"])
            self.assertTrue(rows[0]["shadowSample"]["counted"])
            self.assertNotIn("raw_file", rows[0]["provenance"])
            manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["journal_pair_total"], 2)
            self.assertEqual(len(manifest["identities"]), 2)
            self.assertEqual(
                {item["decision_id"] for item in manifest["identities"]},
                {"boc-decision-17"},
            )
            self.assertEqual(
                len({item["source"] for item in manifest["identities"]}), 2
            )
            self.assertEqual(manifest["last_observed"]["new_pair_count"], 1)
            self.assertFalse((data_root / "online_raw").exists())

    def test_shadow_journal_ignores_macro_only_failed_and_uncounted_rows(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        assignments = _Parser(fixture_text, FIXTURE.name).parse()
        decisions, _, _ = _extract_records(assignments)
        confirmed = next(item for item in decisions if item[1].get("schemaVersion") == 2)
        rows = [confirmed]
        for offset, status in enumerate(("pending", "failed", "expired"), start=1):
            record = copy.deepcopy(confirmed[1])
            record["decisionId"] = f"boc-decision-unconfirmed-{offset}"
            record["shadowSample"] = {
                "contractVersion": 2,
                "status": status,
                "armedAt": 103.0 + offset,
                "materialized": False,
                "counted": False,
                "coalescedMacroEvaluations": 10,
            }
            rows.append((confirmed[0] + offset, record))
        uncounted = copy.deepcopy(confirmed[1])
        uncounted["decisionId"] = "boc-decision-confirmed-not-counted"
        uncounted["shadowSample"]["counted"] = False
        uncounted["shadowSample"].pop("countedAt")
        rows.append((confirmed[0] + 4, uncounted))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(fixture_text, encoding="utf-8")
            result = publish_shadow_pairs(
                rows,
                source_lua=source,
                data_root=root / "offline_data",
            )
            self.assertEqual(result.source_pair_total, 1)
            self.assertEqual(result.new_pair_count, 1)
            published = [
                json.loads(line)
                for line in result.output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [row["decisionId"] for row in published], ["boc-decision-17"]
            )

    def test_shadow_contract_versions_two_through_four_are_imported_and_published(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        for contract_version in (2, 3, 4):
            with self.subTest(contract_version=contract_version):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    source = root / "BrainOfCat.lua"
                    source.write_text(
                        fixture_text.replace(
                            '["contractVersion"] = 2,',
                            f'["contractVersion"] = {contract_version},',
                            1,
                        ),
                        encoding="utf-8",
                    )

                    assignments = _Parser(
                        source.read_text(encoding="utf-8"), source.name
                    ).parse()
                    decisions, _, _ = _extract_records(assignments)
                    result = publish_shadow_pairs(
                        decisions,
                        source_lua=source,
                        data_root=root / "offline_data",
                    )
                    published = [
                        json.loads(line)
                        for line in result.output.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertEqual(len(published), 1)
                    self.assertEqual(
                        published[0]["shadowSample"]["contractVersion"],
                        contract_version,
                    )

    def test_unknown_shadow_contract_version_is_rejected(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                fixture_text.replace(
                    '["contractVersion"] = 2,',
                    '["contractVersion"] = 5,',
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SavedVariablesSchemaError) as raised:
                import_savedvariables(source, data_root=root / "offline_data")

            self.assertIn("shadowSample.contractVersion", str(raised.exception))
            self.assertIn("expected one of 2, 3, 4", str(raised.exception))
            self.assertFalse((root / "offline_data").exists())

    def test_import_preserves_raw_and_sorts_calibration_by_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            result = import_savedvariables(FIXTURE, data_root=data_root)

            self.assertEqual(result.decision_count, 3)
            self.assertEqual(result.calibration_count, 3)
            self.assertEqual(result.expert_trace_count, 3)
            self.assertEqual(result.raw_copy.read_bytes(), FIXTURE.read_bytes())

            decisions = [
                json.loads(line)
                for line in result.online_decisions.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [row["mode"] for row in decisions], ["shadow", "live", "live"]
            )
            self.assertEqual(decisions[0]["proposal"]["queue"], [])
            self.assertEqual(
                decisions[0]["proposal"]["gcd"],
                ["warrior_bloodthirst", "warrior_whirlwind"],
            )
            self.assertEqual(decisions[0]["state"]["targetName"], 'Training "Dummy"')
            self.assertEqual(decisions[0]["provenance"]["storage_index"], 1)
            self.assertEqual(
                decisions[0]["provenance"]["lua_path"],
                "BrainOfCatCharacterDB.entries",
            )
            paired = decisions[2]
            self.assertEqual(paired["schemaVersion"], 2)
            self.assertEqual(paired["decisionId"], "boc-decision-17")
            self.assertFalse(paired["candidateShadow"]["executed"])
            self.assertTrue(paired["expertActual"]["materialized"])
            self.assertEqual(paired["shadowSample"]["status"], "confirmed")
            self.assertEqual(
                paired["candidateShadow"]["proposal"]["gcd"],
                ["warrior_whirlwind"],
            )
            self.assertEqual(
                paired["expertActual"]["sinkActions"][0]["decisionId"],
                paired["decisionId"],
            )
            self.assertEqual(paired["expertActual"]["serverObservedActions"], [])
            self.assertTrue(paired["observedActualOutcome"]["observedOnly"])
            self.assertEqual(paired["observedActualOutcome"]["eventSequences"], [8, 9])
            self.assertEqual(
                paired["observedActualOutcome"]["compactEvents"][1]["amount"],
                812,
            )
            self.assertEqual(
                paired["observedActualOutcome"]["telemetry"]["source"],
                "always_on_typed",
            )

            calibration = [
                json.loads(line)
                for line in result.calibration.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["sequence"] for row in calibration], [7, 8, 9])
            self.assertEqual(
                [row["provenance"]["storage_index"] for row in calibration],
                [2, 3, 1],
            )
            self.assertEqual(calibration[1]["castDuration"], -1.5)
            self.assertEqual(calibration[2]["rawMessage"], "critical\nline")
            self.assertEqual(calibration[2]["actionDecisionId"], "boc-decision-17")

            expert_traces = [
                json.loads(line)
                for line in result.expert_traces.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual([row["sequence"] for row in expert_traces], [11, 12, 13])
            self.assertEqual(expert_traces[0]["expert"], "Cat")
            self.assertEqual(expert_traces[1]["actions"][0]["channel"], "queue")
            self.assertEqual(expert_traces[1]["actions"][1]["args"]["arg1"], "嗜血")
            self.assertEqual(expert_traces[1]["decisionId"], "boc-decision-17")
            self.assertEqual(expert_traces[1]["provenance"]["storage_index"], 1)
            self.assertTrue(expert_traces[2]["policyEntered"])
            self.assertEqual(expert_traces[2]["policyFunction"], "Contra_SCKBZ_A")

            metadata_text = result.metadata.read_text(encoding="utf-8")
            metadata = json.loads(metadata_text)
            self.assertEqual(metadata["decision_count"], 3)
            self.assertEqual(metadata["calibration_count"], 3)
            self.assertEqual(metadata["expert_trace_count"], 3)
            self.assertEqual(metadata["source"]["size_bytes"], FIXTURE.stat().st_size)
            self.assertNotIn("checksum", metadata_text.lower())
            self.assertNotIn("fingerprint", metadata_text.lower())
            self.assertNotIn('"hash', metadata_text.lower())

    def test_cli_uses_only_the_explicit_input_and_prints_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = main([str(FIXTURE), "--data-root", str(data_root)])

            self.assertEqual(return_code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "ok")
            self.assertEqual(receipt["decision_count"], 3)
            self.assertEqual(receipt["calibration_count"], 3)
            self.assertEqual(receipt["expert_trace_count"], 3)

    def test_syntax_error_reports_source_line_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                'BrainOfCatCharacterDB = { ["schemaVersion"] = 1, @ }',
                encoding="utf-8",
            )
            data_root = root / "offline_data"

            with self.assertRaises(SavedVariablesSyntaxError) as raised:
                import_savedvariables(source, data_root=data_root)

            message = str(raised.exception)
            self.assertIn("BrainOfCat.lua:1:", message)
            self.assertIn("unexpected character '@'", message)
            self.assertFalse(data_root.exists())

    def test_schema_error_reports_path_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                """
BrainOfCatCharacterDB = {
    ["schemaVersion"] = 1,
    ["entries"] = "not-a-table",
}
""".strip(),
                encoding="utf-8",
            )
            data_root = root / "offline_data"

            with self.assertRaises(SavedVariablesSchemaError) as raised:
                import_savedvariables(source, data_root=data_root)

            self.assertIn("BrainOfCatCharacterDB.entries", str(raised.exception))
            self.assertIn("expected table", str(raised.exception))
            self.assertFalse(data_root.exists())

    def test_schema_v2_rejects_an_executed_inactive_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            fixture_text = FIXTURE.read_text(encoding="utf-8")
            source.write_text(
                fixture_text.replace(
                    '["executed"] = false,', '["executed"] = true,', 1
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SavedVariablesSchemaError) as raised:
                import_savedvariables(source, data_root=root / "offline_data")

            self.assertIn("candidateShadow.executed", str(raised.exception))
            self.assertIn("must not be executed", str(raised.exception))

    def test_schema_v1_rejects_malformed_optional_off_gcd_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                """
BrainOfCatCharacterDB = {
    ["schemaVersion"] = 1,
    ["entries"] = {
        [1] = {
            ["schemaVersion"] = 1,
            ["capturedAt"] = 1,
            ["mode"] = "shadow",
            ["state"] = {},
            ["proposal"] = {
                ["off_gcd"] = "bad",
                ["queue"] = {},
                ["gcd"] = {},
            },
            ["attempts"] = {},
            ["stopped"] = false,
        },
    },
}
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(SavedVariablesSchemaError) as raised:
                import_savedvariables(source, data_root=root / "offline_data")

            self.assertIn("proposal.off_gcd", str(raised.exception))

    def test_calibration_snapshot_empty_arrays_remain_json_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                """
BrainOfCatCharacterDB = {
    ["schemaVersion"] = 1,
    ["calibration"] = {
        ["schemaVersion"] = 1,
        ["count"] = 1,
        ["entries"] = {
            [1] = {
                ["sequence"] = 1,
                ["time"] = 2.5,
                ["event"] = "CALIBRATION_TASK_STARTED",
                ["state"] = {
                    ["playerAuras"] = {},
                    ["targetAuras"] = {},
                    ["equipment"] = {},
                    ["talents"] = {},
                    ["spellbook"] = {},
                    ["actionBarSpells"] = {},
                    ["skillLines"] = {},
                    ["bagItems"] = {},
                    ["characterStats"] = {
                        ["attributes"] = {},
                    },
                },
            },
        },
    },
}
""".strip(),
                encoding="utf-8",
            )
            result = import_savedvariables(source, data_root=root / "offline_data")
            record = json.loads(result.calibration.read_text(encoding="utf-8"))
            for key in (
                "playerAuras",
                "targetAuras",
                "equipment",
                "talents",
                "spellbook",
                "actionBarSpells",
                "skillLines",
                "bagItems",
            ):
                self.assertEqual(record["state"][key], [])
            self.assertEqual(record["state"]["characterStats"]["attributes"], [])

    def test_sparse_campaign_task_index_maps_remain_json_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                """
BrainOfCatCharacterDB = {
    ["schemaVersion"] = 1,
    ["calibration"] = {
        ["schemaVersion"] = 1,
        ["count"] = 1,
        ["entries"] = {
            [1] = {
                ["sequence"] = 1,
                ["time"] = 2.5,
                ["event"] = "CALIBRATION_CAMPAIGN_INCOMPLETE",
                ["state"] = {},
                ["marker"] = {
                    ["completedTaskIds"] = {
                        [1] = "task_one",
                        [3] = "task_three",
                    },
                    ["incompleteTaskIds"] = {
                        [4] = "task_four",
                        [6] = "task_six",
                        [7] = "task_seven",
                    },
                    ["deferredTaskIds"] = {
                        [8] = "task_eight",
                    },
                },
            },
        },
    },
}
""".strip(),
                encoding="utf-8",
            )

            result = import_savedvariables(source, data_root=root / "offline_data")
            record = json.loads(result.calibration.read_text(encoding="utf-8"))
            marker = record["marker"]
            self.assertEqual(
                marker["completedTaskIds"],
                {"1": "task_one", "3": "task_three"},
            )
            self.assertEqual(
                marker["incompleteTaskIds"],
                {"4": "task_four", "6": "task_six", "7": "task_seven"},
            )
            self.assertEqual(marker["deferredTaskIds"], {"8": "task_eight"})

    def test_other_sparse_integer_tables_are_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                """
BrainOfCatCharacterDB = {
    ["schemaVersion"] = 1,
    ["calibration"] = {
        ["schemaVersion"] = 1,
        ["count"] = 1,
        ["entries"] = {
            [1] = {
                ["sequence"] = 1,
                ["time"] = 2.5,
                ["event"] = "CALIBRATION_TASK_STARTED",
                ["state"] = {
                    ["spellbook"] = {
                        [1] = "first",
                        [3] = "third",
                    },
                },
            },
        },
    },
}
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(SavedVariablesSchemaError) as raised:
                import_savedvariables(source, data_root=root / "offline_data")

            self.assertIn("array keys must be contiguous", str(raised.exception))
            self.assertFalse((root / "offline_data").exists())


if __name__ == "__main__":
    unittest.main()
