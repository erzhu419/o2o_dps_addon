from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.import_savedvariables import (
    _Parser,
    _extract_records,
    publish_shadow_pairs,
)
from o2o_dps.calibration_watch import CalibrationWatcher
from o2o_dps.shadow_live_export import (
    EXPORT_SCHEMA,
    EXPORT_SCHEMA_VERSION,
    ShadowLiveExportCollector,
)


FIXTURE = Path(__file__).parent / "fixtures" / "BrainOfCat.lua"
CHARACTER_KEY = "Account/Realm/Character"


def _confirmed_pair(export_session_id: str) -> dict[str, object]:
    fixture_text = FIXTURE.read_text(encoding="utf-8")
    assignments = _Parser(fixture_text, FIXTURE.name).parse()
    decisions, _, _ = _extract_records(assignments)
    record = copy.deepcopy(
        next(record for _, record in decisions if record.get("schemaVersion") == 2)
    )
    sample = record["shadowSample"]
    sample["exportSessionId"] = export_session_id
    sample["exportTransport"] = "nampower_customdata_jsonl"
    sample["exportedAt"] = 103.0
    return record


def _session_start(export_session_id: str) -> dict[str, object]:
    return {
        "schema": EXPORT_SCHEMA,
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "session_start",
        "exportSessionId": export_session_id,
        "characterKey": CHARACTER_KEY,
    }


def _pair_envelope(
    export_session_id: str,
    *,
    ordinal: int = 1,
) -> dict[str, object]:
    return {
        "schema": EXPORT_SCHEMA,
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "shadow_pair",
        "exportSessionId": export_session_id,
        "characterKey": CHARACTER_KEY,
        "ordinal": ordinal,
        "target": 60,
        "completed": False,
        "record": _confirmed_pair(export_session_id),
    }


def _write_export(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class ShadowLiveExportTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        wow_root = root / "WoW"
        savedvariables = (
            wow_root
            / "WTF"
            / "Account"
            / "Realm"
            / "Character"
            / "SavedVariables"
            / "BrainOfCat.lua"
        )
        savedvariables.parent.mkdir(parents=True)
        savedvariables.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        export = wow_root / "CustomData" / "BrainOfCatShadowPairs.jsonl"
        data_root = root / "offline_data"
        return savedvariables, export, data_root

    def test_customdata_import_does_not_require_savedvariables_change_and_repolls_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, export, data_root = self._paths(
                Path(temporary_directory)
            )
            before_stat = savedvariables.stat()
            before_bytes = savedvariables.read_bytes()
            session_id = "shadow-session-one"
            _write_export(
                export,
                [_session_start(session_id), _pair_envelope(session_id)],
            )
            collector = ShadowLiveExportCollector(
                savedvariables,
                data_root=data_root,
            )

            first = collector.poll()
            second = collector.poll()

            after_stat = savedvariables.stat()
            self.assertEqual(savedvariables.read_bytes(), before_bytes)
            self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(after_stat.st_size, before_stat.st_size)
            self.assertEqual(first["transport"], "nampower_customdata_jsonl")
            self.assertTrue(first["changed"])
            self.assertEqual(first["valid_pair_count"], 1)
            self.assertEqual(first["source_pair_total"], 1)
            self.assertEqual(first["new_pair_count"], 1)
            self.assertEqual(first["journal_pair_total"], 1)
            self.assertFalse(second["changed"])
            self.assertEqual(second["new_pair_count"], 0)
            self.assertEqual(second["journal_pair_total"], 1)
            self.assertFalse((data_root / "online_raw").exists())

            journal = Path(first["output"])
            rows = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["provenance"]["source_identity"],
                f"brainofcat-shadow-session:{session_id}",
            )

    def test_new_session_reuses_decision_id_and_same_session_savedvariables_deduplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, export, data_root = self._paths(
                Path(temporary_directory)
            )
            first_session = "shadow-session-one"
            second_session = "shadow-session-two-longer"
            collector = ShadowLiveExportCollector(
                savedvariables,
                data_root=data_root,
            )
            _write_export(
                export,
                [
                    _session_start(first_session),
                    _pair_envelope(first_session),
                ],
            )
            first = collector.poll()
            _write_export(
                export,
                [
                    _session_start(first_session),
                    _pair_envelope(first_session),
                    _session_start(second_session),
                    _pair_envelope(second_session),
                ],
            )

            second = collector.poll()

            self.assertEqual(first["new_pair_count"], 1)
            self.assertEqual(second["session_count"], 2)
            self.assertEqual(second["source_pair_total"], 2)
            self.assertEqual(second["new_pair_count"], 1)
            self.assertEqual(second["journal_pair_total"], 2)

            fixture_text = FIXTURE.read_text(encoding="utf-8")
            insertion = '                ["coalescedMacroEvaluations"] = 4,\n'
            self.assertEqual(fixture_text.count(insertion), 1)
            savedvariables.write_text(
                fixture_text.replace(
                    insertion,
                    insertion
                    + f'                ["exportSessionId"] = "{second_session}",\n'
                    + '                ["exportTransport"] = "nampower_customdata_jsonl",\n'
                    + '                ["exportedAt"] = 103,\n',
                ),
                encoding="utf-8",
            )
            assignments = _Parser(
                savedvariables.read_text(encoding="utf-8"),
                savedvariables.name,
            ).parse()
            savedvariable_decisions, _, _ = _extract_records(assignments)

            later_savedvariables = publish_shadow_pairs(
                savedvariable_decisions,
                source_lua=savedvariables,
                data_root=data_root,
            )

            self.assertEqual(later_savedvariables.source_pair_total, 1)
            self.assertEqual(later_savedvariables.new_pair_count, 0)
            self.assertEqual(later_savedvariables.journal_pair_total, 2)
            rows = [
                json.loads(line)
                for line in later_savedvariables.output.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [row["decisionId"] for row in rows],
                ["boc-decision-17", "boc-decision-17"],
            )
            self.assertEqual(
                {row["shadowSample"]["exportSessionId"] for row in rows},
                {first_session, second_session},
            )
            self.assertEqual(
                {row["provenance"]["source_identity"] for row in rows},
                {
                    f"brainofcat-shadow-session:{first_session}",
                    f"brainofcat-shadow-session:{second_session}",
                },
            )
            self.assertFalse((data_root / "online_raw").exists())

    def test_latest_session_diagnostics_ignore_an_older_completed_larger_target(
        self,
    ) -> None:
        """A retained 60/60 session must not complete a new 1/12 session."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, export, data_root = self._paths(
                Path(temporary_directory)
            )
            old_session = "shadow-old-60"
            new_session = "shadow-new-12"
            old_pair = _pair_envelope(old_session, ordinal=60)
            old_pair["target"] = 60
            old_pair["completed"] = True
            new_pair = _pair_envelope(new_session, ordinal=1)
            new_pair["target"] = 12
            new_pair["completed"] = False
            _write_export(
                export,
                [
                    _session_start(old_session),
                    old_pair,
                    _session_start(new_session),
                    new_pair,
                ],
            )

            result = ShadowLiveExportCollector(
                savedvariables,
                data_root=data_root,
            ).poll()

            self.assertEqual(result["session_count"], 2)
            self.assertEqual(result["latest_export_session_id"], new_session)
            self.assertEqual(result["latest_ordinal"], 1)
            self.assertEqual(result["latest_target"], 12)
            self.assertFalse(result["completed"])
            self.assertEqual(result["valid_pair_count"], 2)
            self.assertEqual(result["journal_pair_total"], 2)

    def test_watcher_polls_customdata_even_when_savedvariables_is_unchanged(
        self,
    ) -> None:
        class FakeTimerCollector:
            @staticmethod
            def poll() -> dict[str, object]:
                return {"status": "idle"}

        class FakeShadowCollector:
            def __init__(self) -> None:
                self.calls = 0

            def poll(self) -> dict[str, object]:
                self.calls += 1
                return {
                    "status": "imported",
                    "transport": "nampower_customdata_jsonl",
                    "source_pair_total": 1,
                    "new_pair_count": 1,
                    "journal_pair_total": 1,
                    "changed": True,
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, _, data_root = self._paths(Path(temporary_directory))
            shadow = FakeShadowCollector()
            watcher = CalibrationWatcher(
                savedvariables,
                data_root=data_root,
                timer_live_debug_collector=FakeTimerCollector(),
                shadow_live_export_collector=shadow,
            )

            with patch(
                "o2o_dps.calibration_watch.time.sleep",
                side_effect=KeyboardInterrupt,
            ):
                watcher.run_forever(poll_seconds=1.0, stable_seconds=1.5)

            self.assertEqual(shadow.calls, 1)
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "stopped")
            self.assertEqual(status["shadow_pairs"]["journal_pair_total"], 1)

    def test_completed_live_export_runs_acceptance_audit_with_session_target(
        self,
    ) -> None:
        class FakeShadowCollector:
            @staticmethod
            def poll() -> dict[str, object]:
                return {
                    "status": "imported",
                    "transport": "nampower_customdata_jsonl",
                    "source_pair_total": 12,
                    "new_pair_count": 1,
                    "journal_pair_total": 72,
                    "invalid_line_count": 0,
                    "changed": True,
                    "completed": True,
                    "latest_target": 12,
                    "latest_export_session_id": "shadow-session-two",
                    "output": "journal.jsonl",
                    "manifest": "manifest.json",
                }

        report = {
            "status": "PASS",
            "input": {"selected_export_session_id": "shadow-session-two"},
            "transport_action_gate": {"status": "PASS"},
            "causal_state_action_gate": {"status": "PASS"},
            "outcome_reward_gate": {"status": "PASS"},
            "deployment_allowed": False,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, _, data_root = self._paths(Path(temporary_directory))
            watcher = CalibrationWatcher(
                savedvariables,
                data_root=data_root,
                shadow_live_export_collector=FakeShadowCollector(),
            )
            with patch(
                "o2o_dps.calibration_watch.audit_shadow_pairs",
                return_value=(report, watcher.shadow_acceptance_output),
            ) as audit:
                result = watcher._poll_shadow_live_export()

            self.assertTrue(result["completed"])
            audit.assert_called_once_with(
                "journal.jsonl",
                "manifest.json",
                output_path=watcher.shadow_acceptance_output,
                expected_pairs=12,
                export_session_id="shadow-session-two",
            )
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "shadow_pairs_imported_live")
            self.assertEqual(status["shadow_acceptance"]["status"], "PASS")
            self.assertEqual(
                status["shadow_acceptance"]["causal_state_action_gate"],
                "PASS",
            )
            self.assertFalse(status["shadow_acceptance"]["deployment_allowed"])

    def test_completed_deduplicated_export_refreshes_acceptance_after_restart(
        self,
    ) -> None:
        class FakeShadowCollector:
            @staticmethod
            def poll() -> dict[str, object]:
                return {
                    "status": "imported",
                    "transport": "nampower_customdata_jsonl",
                    "source_pair_total": 72,
                    "new_pair_count": 0,
                    "journal_pair_total": 72,
                    "invalid_line_count": 0,
                    "changed": True,
                    "completed": True,
                    "latest_target": 12,
                    "latest_export_session_id": "shadow-session-two",
                    "output": "journal.jsonl",
                    "manifest": "manifest.json",
                }

        report = {
            "status": "FAIL",
            "input": {"selected_export_session_id": "shadow-session-two"},
            "transport_action_gate": {"status": "PASS"},
            "causal_state_action_gate": {"status": "FAIL"},
            "outcome_reward_gate": {"status": "FAIL"},
            "deployment_allowed": False,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            savedvariables, _, data_root = self._paths(Path(temporary_directory))
            watcher = CalibrationWatcher(
                savedvariables,
                data_root=data_root,
                shadow_live_export_collector=FakeShadowCollector(),
            )
            with patch(
                "o2o_dps.calibration_watch.audit_shadow_pairs",
                return_value=(report, watcher.shadow_acceptance_output),
            ) as audit:
                result = watcher._poll_shadow_live_export()

            self.assertEqual(result["new_pair_count"], 0)
            audit.assert_called_once_with(
                "journal.jsonl",
                "manifest.json",
                output_path=watcher.shadow_acceptance_output,
                expected_pairs=12,
                export_session_id="shadow-session-two",
            )
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "shadow_acceptance_refreshed")
            self.assertEqual(status["shadow_acceptance"]["status"], "FAIL")
            self.assertEqual(
                status["shadow_acceptance"]["export_session_id"],
                "shadow-session-two",
            )


if __name__ == "__main__":
    unittest.main()
