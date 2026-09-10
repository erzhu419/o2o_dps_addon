from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_watch import CalibrationWatcher
from o2o_dps.fury_timer_calibration_summary import CAMPAIGN_ID as TIMER_CAMPAIGN_ID
from o2o_dps.timer_live_debug import (
    COMBAT_LOG_PRELUDE_BYTES,
    SUPPORT_LOG_PRELUDE_BYTES,
    TERMINAL_LOG_SYNC_GRACE_SECONDS,
    TimerLiveDebugCollector,
    TimerLiveDebugError,
    derive_wow_root,
)


def _layout(root: Path) -> tuple[Path, Path, Path, Path]:
    wow = root / "WoW"
    source = (
        wow
        / "WTF"
        / "Account"
        / "ACCOUNT"
        / "Realm"
        / "Character"
        / "SavedVariables"
        / "BrainOfCat.lua"
    )
    source.parent.mkdir(parents=True)
    imports = wow / "Imports"
    logs = wow / "Logs"
    imports.mkdir()
    logs.mkdir()
    return wow, source, imports, logs


def _debug_document(
    run_id: str,
    revision: int,
    sequences: list[int],
    *,
    terminal: bool = False,
) -> dict[str, object]:
    return {
        "schema": "brainofcat_timer_live_debug/v1",
        "schemaVersion": 1,
        "campaignRunId": run_id,
        "revision": revision,
        "status": "awaiting_export_reload" if terminal else "running",
        "terminal": terminal,
        "stage": "C_HASTE",
        "substep": "observe",
        "snapshot": {"gate": "waiting_for_flurry_remove"},
        "trace": [
            {
                "sequence": sequence,
                "gameTime": 100 + sequence,
                "kind": "gate",
                "reason": f"reason-{sequence}",
            }
            for sequence in sequences
        ],
    }


def _timer_savedvariables(run_id: str) -> str:
    return f'''BrainOfCatCharacterDB = {{
    ["schemaVersion"] = 1,
    ["calibration"] = {{
        ["schemaVersion"] = 1,
        ["maxEntries"] = 15000,
        ["count"] = 1,
        ["nextIndex"] = 2,
        ["nextSequence"] = 2,
        ["entries"] = {{
            [1] = {{
                ["sequence"] = 1,
                ["time"] = 100.5,
                ["event"] = "CALIBRATION_CAMPAIGN_COMPLETED",
                ["state"] = {{}},
                ["marker"] = {{
                    ["campaignId"] = "{TIMER_CAMPAIGN_ID}",
                    ["campaignRunId"] = "{run_id}",
                }},
            }},
        }},
    }},
}}
'''


class TimerLiveDebugTests(unittest.TestCase):
    def test_derives_wow_root_from_explicit_savedvariables_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            wow, source, _, _ = _layout(Path(temporary_directory))
            self.assertEqual(derive_wow_root(source), wow.resolve())

    def test_terminal_stage_d_recovery_writes_validated_composite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, _ = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            source_run_id = "timer-campaign-source-1"
            source_document = _debug_document(source_run_id, 1, [1, 2, 3, 4])
            source_document["trace"] = [
                {"sequence": index, "kind": "MARKER", "name": name}
                for index, name in enumerate(
                    (
                        "CALIBRATION_TIMER_STAGE_COMPLETED",
                        "CALIBRATION_HASTE_STAGE_STARTED",
                        "CALIBRATION_HASTE_STAGE_COMPLETED",
                        "CALIBRATION_QUEUE_STAGE_STARTED",
                    ),
                    start=1,
                )
            ]
            debug_path.write_text(json.dumps(source_document), encoding="utf-8")
            data_root = root / "offline_data"
            collector = TimerLiveDebugCollector(
                source,
                data_root=data_root,
                runtime_directory=data_root / "calibration_watch",
            )
            collector.poll()

            recovery_document = _debug_document(
                "timer-recovery-1", 1, [1, 2, 3], terminal=True
            )
            recovery_document.update(
                {
                    "campaignMode": "stage_d_recovery",
                    "sourceCampaignRunId": source_run_id,
                    "terminalEvent": "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
                    "snapshot": {
                        "lastMarker": {
                            "details": {
                                "loadoutRestored": True,
                                "recoveryLoadoutEvidence": (
                                    "operator_supplied_explicit_item_ids"
                                ),
                                "heroicStrike": {
                                    "cancelCompleted": True,
                                    "strongCancelSupport": True,
                                    "nextMainHandWasWhite": True,
                                    "offHandContinued": True,
                                    "targetSwitchStatus": "EXTERNAL_HOLD",
                                },
                            }
                        }
                    },
                    "trace": [
                        {
                            "sequence": 1,
                            "kind": "MARKER",
                            "name": "CALIBRATION_STAGE_D_RECOVERY_STARTED",
                        },
                        {
                            "sequence": 2,
                            "kind": "MARKER",
                            "name": "CALIBRATION_HS_CANCEL_COMPLETED",
                        },
                        {
                            "sequence": 3,
                            "kind": "MARKER",
                            "name": "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
                        },
                    ],
                }
            )
            debug_path.write_text(json.dumps(recovery_document), encoding="utf-8")

            result = collector.poll()

            composite_status = result["recovery_composite"]
            self.assertEqual(composite_status["status"], "complete")
            composite = json.loads(
                Path(composite_status["output"]).read_text(encoding="utf-8")
            )
            self.assertEqual(composite["source_campaign_run_id"], source_run_id)
            self.assertTrue(all(composite["checks"].values()))

    def test_finalize_recovers_stage_d_terminal_from_savedvariables_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, _ = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            source_run_id = "timer-campaign-source-finalize"
            source_document = _debug_document(source_run_id, 1, [1, 2, 3, 4])
            source_document["trace"] = [
                {"sequence": index, "kind": "MARKER", "name": name}
                for index, name in enumerate(
                    (
                        "CALIBRATION_TIMER_STAGE_COMPLETED",
                        "CALIBRATION_HASTE_STAGE_STARTED",
                        "CALIBRATION_HASTE_STAGE_COMPLETED",
                        "CALIBRATION_QUEUE_STAGE_STARTED",
                    ),
                    start=1,
                )
            ]
            debug_path.write_text(json.dumps(source_document), encoding="utf-8")
            data_root = root / "offline_data"
            collector = TimerLiveDebugCollector(
                source,
                data_root=data_root,
                runtime_directory=data_root / "calibration_watch",
            )
            collector.poll()

            recovery_run_id = "timer-recovery-finalize"
            recovery_document = _debug_document(recovery_run_id, 1, [1, 2])
            recovery_document.update(
                {
                    "campaignMode": "stage_d_recovery",
                    "sourceCampaignRunId": source_run_id,
                    "trace": [
                        {
                            "sequence": 1,
                            "kind": "MARKER",
                            "name": "CALIBRATION_STAGE_D_RECOVERY_STARTED",
                        },
                        {
                            "sequence": 2,
                            "kind": "MARKER",
                            "name": "CALIBRATION_HS_CANCEL_COMPLETED",
                        },
                    ],
                }
            )
            debug_path.write_text(json.dumps(recovery_document), encoding="utf-8")
            capturing = collector.poll()

            terminal_record = {
                "sequence": 900,
                "time": 123.5,
                "event": "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
                "marker": {
                    "campaignRunId": recovery_run_id,
                    "campaignMode": "stage_d_recovery",
                    "sourceCampaignRunId": source_run_id,
                    "status": "awaiting_export_reload",
                    "loadoutRestored": True,
                    "recoveryLoadoutEvidence": (
                        "operator_supplied_explicit_item_ids"
                    ),
                    "heroicStrike": {
                        "cancelCompleted": True,
                        "strongCancelSupport": True,
                        "nextMainHandWasWhite": True,
                        "offHandContinued": True,
                        "targetSwitchStatus": "EXTERNAL_HOLD",
                    },
                },
            }
            idle_document = {
                "schema": "brainofcat_timer_live_debug/v1",
                "schemaVersion": 1,
                "revision": 2,
                "status": "idle",
                "ready": True,
                "trace": [],
            }
            debug_path.write_text(json.dumps(idle_document), encoding="utf-8")

            finalized = collector.finalize(recovery_run_id, terminal_record)

            self.assertEqual(finalized["recovery_composite"]["status"], "complete")
            composite_path = Path(finalized["recovery_composite"]["output"])
            composite = json.loads(composite_path.read_text(encoding="utf-8"))
            self.assertTrue(all(composite["checks"].values()))
            snapshot = json.loads(
                (Path(capturing["bundle"]) / "latest_snapshot.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(snapshot["terminal"])
            self.assertEqual(
                snapshot["terminalEvent"],
                "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
            )
            manifest = json.loads(
                Path(finalized["manifest"]).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["campaign_mode"], "stage_d_recovery")
            self.assertEqual(manifest["source_campaign_run_id"], source_run_id)

    def test_deduplicates_trace_and_copies_only_run_log_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wow, source, imports, logs = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            combat_prefix = b"C" * (COMBAT_LOG_PRELUDE_BYTES + 8192)
            frame_prefix = b"F" * (SUPPORT_LOG_PRELUDE_BYTES + 4096)
            nampower_prefix = b"N" * (SUPPORT_LOG_PRELUDE_BYTES + 2048)
            (logs / "WoWCombatLog.txt").write_bytes(combat_prefix)
            (logs / "FrameXML.log").write_bytes(frame_prefix)
            (logs / "nampower_debug.log").write_bytes(nampower_prefix)
            for index in range(1, 4):
                (logs / f"nampower_debug.log.{index}").write_bytes(
                    bytes([64 + index]) * (SUPPORT_LOG_PRELUDE_BYTES + index)
                )
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-1", 1, [1, 2])),
                encoding="utf-8",
            )
            data_root = root / "offline_data"
            collector = TimerLiveDebugCollector(
                source,
                data_root=data_root,
                runtime_directory=data_root / "calibration_watch",
            )

            first = collector.poll()

            self.assertEqual(first["status"], "capturing")
            bundle = Path(first["bundle"])
            self.assertEqual(
                (bundle / "WoWCombatLog.part000.txt").read_bytes(),
                combat_prefix[-COMBAT_LOG_PRELUDE_BYTES:],
            )
            self.assertEqual(
                (bundle / "FrameXML.part000.log").read_bytes(),
                frame_prefix[-SUPPORT_LOG_PRELUDE_BYTES:],
            )
            self.assertEqual(
                (bundle / "nampower_debug.part000.log").read_bytes(),
                nampower_prefix[-SUPPORT_LOG_PRELUDE_BYTES:],
            )
            for index in range(1, 4):
                self.assertEqual(
                    (bundle / f"nampower_debug_{index}.part000.log").stat().st_size,
                    SUPPORT_LOG_PRELUDE_BYTES,
                )

            with (logs / "WoWCombatLog.txt").open("ab") as handle:
                handle.write(b"combat-new")
            with (logs / "FrameXML.log").open("ab") as handle:
                handle.write(b"frame-new")
            with (logs / "nampower_debug.log").open("ab") as handle:
                handle.write(b"nampower-new")
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-1", 2, [1, 2, 3])),
                encoding="utf-8",
            )

            second = collector.poll()

            self.assertEqual(second["appended_trace_entries"], 1)
            trace = [
                json.loads(line)
                for line in (bundle / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([item["debugSequence"] for item in trace], [1, 2, 3])
            self.assertTrue(
                (bundle / "WoWCombatLog.part000.txt").read_bytes().endswith(b"combat-new")
            )
            self.assertTrue((bundle / "FrameXML.part000.log").read_bytes().endswith(b"frame-new"))
            self.assertTrue(
                (bundle / "nampower_debug.part000.log").read_bytes().endswith(b"nampower-new")
            )
            live_status = json.loads(collector.status_path.read_text(encoding="utf-8"))
            self.assertEqual(live_status["bundle"], str(bundle))
            for source_status in live_status["log_capture"].values():
                self.assertIn("offset", source_status)
                self.assertIn("source_size", source_status)
                self.assertIn("source_mtime_ns", source_status)
                self.assertIn("archive_part", source_status)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                len(manifest["files"]["log_parts"]["nampower_debug_1"]), 1
            )
            self.assertEqual(derive_wow_root(source), wow.resolve())

    def test_terminal_grace_collects_delayed_log_flush_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, logs = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            combat_log = logs / "WoWCombatLog.txt"
            combat_log.write_bytes(b"before-terminal")
            (logs / "FrameXML.log").write_bytes(b"frame")
            (logs / "nampower_debug.log").write_bytes(b"nampower")
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            run_id = "timer-run-terminal-grace"
            debug_path.write_text(
                json.dumps(_debug_document(run_id, 1, [1], terminal=True)),
                encoding="utf-8",
            )
            data_root = root / "offline_data"
            collector = TimerLiveDebugCollector(
                source,
                data_root=data_root,
                runtime_directory=data_root / "calibration_watch",
            )

            with patch("o2o_dps.timer_live_debug._utc_epoch", return_value=100.0):
                terminal = collector.poll()

            self.assertEqual(terminal["terminal_log_sync"]["status"], "pending")
            self.assertEqual(
                terminal["terminal_log_sync"]["terminal_pending_log_sync_until"],
                100.0 + TERMINAL_LOG_SYNC_GRACE_SECONDS,
            )
            bundle = Path(terminal["bundle"])
            archived_log = bundle / "WoWCombatLog.part000.txt"

            with combat_log.open("ab") as handle:
                handle.write(b"-flush-during-grace")
            debug_path.write_text(
                json.dumps(
                    {
                        "schema": "brainofcat_timer_live_debug/v1",
                        "schemaVersion": 1,
                        "revision": 2,
                        "status": "idle",
                        "ready": True,
                        "trace": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch("o2o_dps.timer_live_debug._utc_epoch", return_value=105.0):
                pending = collector.poll()

            self.assertEqual(pending["terminal_log_sync"]["status"], "pending")
            self.assertTrue(archived_log.read_bytes().endswith(b"-flush-during-grace"))

            with combat_log.open("ab") as handle:
                handle.write(b"-flush-at-close")
            with patch("o2o_dps.timer_live_debug._utc_epoch", return_value=111.0):
                complete = collector.poll()

            self.assertEqual(complete["terminal_log_sync"]["status"], "complete")
            self.assertEqual(complete["terminal_log_sync"]["completed_runs"], [run_id])
            closed_bytes = archived_log.read_bytes()
            self.assertTrue(closed_bytes.endswith(b"-flush-at-close"))
            manifest = json.loads(
                (bundle / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["terminal_log_sync"]["status"], "complete")

            with combat_log.open("ab") as handle:
                handle.write(b"-after-grace")
            with patch("o2o_dps.timer_live_debug._utc_epoch", return_value=120.0):
                collector.poll()

            self.assertEqual(archived_log.read_bytes(), closed_bytes)
            with patch("o2o_dps.timer_live_debug._utc_epoch", return_value=121.0):
                refreshed = collector.refresh_terminal_logs(run_id)
            self.assertEqual(refreshed["terminal_log_sync"]["status"], "complete")
            self.assertTrue(archived_log.read_bytes().endswith(b"-after-grace"))

    def test_log_truncation_starts_a_new_part(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, logs = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            (logs / "WoWCombatLog.txt").write_bytes(b"old" * 30000)
            (logs / "FrameXML.log").write_bytes(b"frame")
            (logs / "nampower_debug.log").write_bytes(b"nampower")
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-truncate", 1, [1])),
                encoding="utf-8",
            )
            collector = TimerLiveDebugCollector(
                source,
                data_root=root / "offline_data",
                runtime_directory=root / "offline_data" / "calibration_watch",
            )
            first = collector.poll()
            bundle = Path(first["bundle"])

            (logs / "WoWCombatLog.txt").write_bytes(b"new-generation")
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-truncate", 2, [1, 2])),
                encoding="utf-8",
            )
            second = collector.poll()

            self.assertEqual(
                (bundle / "WoWCombatLog.part001.txt").read_bytes(), b"new-generation"
            )
            self.assertEqual(second["log_capture"]["wow_combat_log"]["truncations"], 1)

    def test_nampower_rotation_keeps_tail_written_between_polls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, logs = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            (logs / "WoWCombatLog.txt").write_bytes(b"combat")
            (logs / "FrameXML.log").write_bytes(b"frame")
            active = logs / "nampower_debug.log"
            rotated = logs / "nampower_debug.log.1"
            active.write_bytes(b"A" * (SUPPORT_LOG_PRELUDE_BYTES + 100))
            rotated.write_bytes(b"older")
            debug_path = imports / "BrainOfCatTimerDebug.txt"
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-rotate", 1, [1])),
                encoding="utf-8",
            )
            collector = TimerLiveDebugCollector(
                source,
                data_root=root / "offline_data",
                runtime_directory=root / "offline_data" / "calibration_watch",
            )
            first = collector.poll()
            bundle = Path(first["bundle"])

            with active.open("ab") as handle:
                handle.write(b"missed-before-rotation")
            active.replace(rotated)
            active.write_bytes(b"new-active")
            debug_path.write_text(
                json.dumps(_debug_document("timer-run-rotate", 2, [1, 2])),
                encoding="utf-8",
            )

            second = collector.poll()

            self.assertEqual(
                (bundle / "nampower_debug_1.part001.log").read_bytes(),
                b"missed-before-rotation",
            )
            self.assertEqual(
                (bundle / "nampower_debug.part001.log").read_bytes(),
                b"new-active",
            )
            self.assertEqual(
                second["log_capture"]["nampower_debug_1"]["truncations"], 1
            )

    def test_incomplete_json_raises_a_poll_error_without_touching_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, _ = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            (imports / "BrainOfCatTimerDebug.txt").write_text("{", encoding="utf-8")
            collector = TimerLiveDebugCollector(
                source,
                data_root=root / "offline_data",
                runtime_directory=root / "offline_data" / "calibration_watch",
            )

            with self.assertRaises(TimerLiveDebugError):
                collector.poll()

            self.assertFalse(collector.bundle_root.exists())

    def test_idle_handshake_reports_readiness_without_creating_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, source, imports, _ = _layout(root)
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            (imports / "BrainOfCatTimerDebug.txt").write_text(
                json.dumps(
                    {
                        "schema": "brainofcat_timer_live_debug/v1",
                        "schemaVersion": 1,
                        "revision": 4,
                        "status": "idle",
                        "readiness": {"ready": True},
                        "campaignRunId": None,
                        "diagnosticSessionId": "login-4",
                        "snapshot": {
                            "readiness": "ready",
                            "wallClock": "2026-09-01T12:00:00+08:00",
                            "gameTime": 12.5,
                            "lastEvent": {
                                "event": "AUTO_ATTACK_SELF",
                                "sequence": 91,
                            },
                            "gate": {
                                "waitingFor": "next_main_hand_white",
                                "accepted": False,
                            },
                        },
                        "trace": [
                            {
                                "sequence": 1,
                                "kind": "PRESS_ENTER",
                                "gameTime": 11.5,
                            },
                            {
                                "sequence": 2,
                                "kind": "EVENT",
                                "gameTime": 12.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            collector = TimerLiveDebugCollector(
                source,
                data_root=root / "offline_data",
                runtime_directory=root / "offline_data" / "calibration_watch",
            )

            result = collector.poll()

            self.assertEqual(result["status"], "ready_idle")
            self.assertTrue(result["ready"])
            self.assertEqual(result["diagnostic_session_id"], "login-4")
            self.assertEqual(result["readiness"], {"ready": True})
            self.assertEqual(result["campaignStatus"], "idle")
            self.assertEqual(result["waitingFor"], "next_main_hand_white")
            self.assertEqual(result["lastObserved"]["event"], "AUTO_ATTACK_SELF")
            self.assertEqual(result["lastPress"]["kind"], "PRESS_ENTER")
            self.assertFalse(collector.bundle_root.exists())

    def test_main_watcher_survives_live_poll_failure(self) -> None:
        class BrokenCollector:
            def poll(self) -> dict[str, object]:
                raise RuntimeError("debug source is temporarily partial")

            def write_error_status(self, error: BaseException) -> dict[str, object]:
                return {"status": "poll_error", "error": str(error)}

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text("BrainOfCatCharacterDB = {}\n", encoding="utf-8")
            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                timer_live_debug_collector=BrokenCollector(),
            )

            result = watcher._poll_timer_live_debug()

            self.assertEqual(result["status"], "poll_error")
            self.assertIn("temporarily partial", result["error"])
            self.assertTrue(watcher.log_path.is_file())
            self.assertFalse(watcher.status_path.exists())

    def test_terminal_timer_summary_contains_debug_bundle(self) -> None:
        class FakeCollector:
            def poll(self) -> dict[str, object]:
                return {"status": "capturing"}

            def finalize(self, campaign_run_id: str) -> dict[str, object]:
                return {
                    "status": "terminal_bundle_ready",
                    "campaign_run_id": campaign_run_id,
                    "bundle": str(root / "debug-bundle"),
                    "manifest": str(root / "debug-bundle" / "manifest.json"),
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_timer_savedvariables("timer-run-terminal"), encoding="utf-8")
            data_root = root / "offline_data"
            timer_output = root / "timer-summary.json"

            def fake_summary(calibration: str | Path) -> SimpleNamespace:
                output = root / "summary.json"
                output.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(output)}
                )

            def fake_timer_summary(
                calibration: str | Path,
                *,
                campaign_run_id: str,
                output_path: str | Path,
            ) -> SimpleNamespace:
                timer_output.write_text(
                    json.dumps(
                        {
                            "status": "complete",
                            "campaign_run_id": campaign_run_id,
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "complete",
                        "campaign_run_id": campaign_run_id,
                        "output": str(timer_output),
                    }
                )

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=fake_summary,
                timer_summarizer=fake_timer_summary,
                timer_live_debug_collector=FakeCollector(),
            )

            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported_timer_complete")
            self.assertEqual(result["timer_summary"]["debug_bundle"], str(root / "debug-bundle"))
            self.assertEqual(result["timer_live_debug"]["status"], "terminal_bundle_ready")
            written_summary = json.loads(timer_output.read_text(encoding="utf-8"))
            self.assertEqual(written_summary["debug_bundle"], str(root / "debug-bundle"))


if __name__ == "__main__":
    unittest.main()
