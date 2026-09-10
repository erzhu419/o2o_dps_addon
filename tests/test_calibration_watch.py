from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_watch import (
    CalibrationWatchError,
    CalibrationWatcher,
    inspect_campaign_completions,
)
from o2o_dps.fury_timer_calibration_summary import CAMPAIGN_ID as TIMER_CAMPAIGN_ID

SHADOW_FIXTURE = Path(__file__).parent / "fixtures" / "BrainOfCat.lua"


def _savedvariables(
    campaign_run_id: str | None,
    *,
    campaign_id: str | None = None,
    top_level_only: bool = False,
    include_static_profile: bool = False,
    static_race: str | None = "GNOME",
    terminal_event: str = "CALIBRATION_CAMPAIGN_COMPLETED",
) -> str:
    marker_field = (
        f'["campaignRunId"] = "{campaign_run_id}",'
        if campaign_run_id is not None
        else ""
    )
    if campaign_id is not None:
        marker_field += f'["campaignId"] = "{campaign_id}",'
    marker = (
        f'["campaignRunId"] = "{campaign_run_id}",'
        if top_level_only and campaign_run_id is not None
        else f'["marker"] = {{ {marker_field} }},'
    )
    completion_sequence = 2 if include_static_profile else 1
    completion_event = (
        f"""
            [{completion_sequence}] = {{
                [\"sequence\"] = {completion_sequence},
                [\"time\"] = 100.5,
                [\"event\"] = \"{terminal_event}\",
                [\"state\"] = {{}},
                {marker}
            }},
        """
        if campaign_run_id != ""
        else ""
    )
    race_field = f'["raceFile"] = "{static_race}",' if static_race is not None else ""
    static_event = (
        f"""
            [1] = {{
                ["sequence"] = 1,
                ["time"] = 99.5,
                ["event"] = "STATIC_PROFILE_CAPTURED",
                ["state"] = {{
                    {race_field}
                    ["classFile"] = "WARRIOR",
                }},
            }},
        """
        if include_static_profile
        else ""
    )
    events = static_event + completion_event
    count = (1 if include_static_profile else 0) + (1 if completion_event else 0)
    return f"""BrainOfCatCharacterDB = {{
    [\"schemaVersion\"] = 1,
    [\"entries\"] = {{}},
    [\"calibration\"] = {{
        [\"schemaVersion\"] = 1,
        [\"maxEntries\"] = 15000,
        [\"count\"] = {count},
        [\"nextIndex\"] = {count + 1},
        [\"nextSequence\"] = {count + 1},
        [\"entries\"] = {{{events}
        }},
    }},
}}
"""


class CalibrationWatcherTests(unittest.TestCase):
    _SHADOW_TRANSITION_CLOSED_FLAGS = (
        "scalar_reward_available",
        "recorded_active_policy_counterfactual_reward_available",
        "candidate_counterfactual_reward_available",
        "full_next_state_available",
        "td_transition_eligible",
        "offline_rl_episode_eligible",
        "deployment_allowed",
    )

    def _run_failed_shadow_transition_case(
        self,
        *,
        mutate_receipt: object | None = None,
        builder_error: Exception | None = None,
    ) -> tuple[dict[str, object], mock.Mock]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables(""), encoding="utf-8")
            data_root = root / "offline_data"
            export_session_id = "shadow-session-complete"
            live_result = {
                "status": "unchanged",
                "transport": "nampower_customdata_jsonl",
                "changed": False,
                "completed": True,
                "invalid_line_count": 0,
                "journal_pair_total": 12,
                "latest_target": 12,
                "latest_export_session_id": export_session_id,
                "output": str(data_root / "online_decisions" / "shadow.jsonl"),
                "manifest": str(
                    data_root / "online_decisions" / "shadow.manifest.json"
                ),
            }
            collector = SimpleNamespace(poll=lambda: dict(live_result))
            acceptance_report = {
                "status": "PASS",
                "input": {"selected_export_session_id": export_session_id},
                "transport_action_gate": {"status": "PASS", "passed": True},
                "causal_state_action_gate": {"status": "PASS", "passed": True},
                "outcome_reward_gate": {"status": "PASS", "passed": True},
                "deployment_allowed": False,
            }
            acceptance_output = data_root / "reports" / "acceptance.json"
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                shadow_live_export_collector=collector,
                shadow_transition_builder=mock.Mock(),
            )
            transition_receipt: dict[str, object] = {
                "status": "ok",
                "export_session_id": export_session_id,
                "row_count": 12,
                "dataset": str(watcher.shadow_transition_output),
                "manifest": str(watcher.shadow_transition_manifest),
                "report": str(watcher.shadow_transition_report),
                "dataset_sha256": "a" * 64,
                **{
                    field: False
                    for field in self._SHADOW_TRANSITION_CLOSED_FLAGS
                },
            }
            if callable(mutate_receipt):
                mutate_receipt(transition_receipt, watcher)
            transition_builder = mock.Mock(
                return_value=transition_receipt,
                side_effect=builder_error,
            )
            watcher.shadow_transition_builder = transition_builder

            with mock.patch(
                "o2o_dps.calibration_watch.audit_shadow_pairs",
                return_value=(acceptance_report, acceptance_output),
            ) as auditor:
                first = watcher._poll_shadow_live_export()
                second = watcher._poll_shadow_live_export()

            self.assertFalse(first["changed"])
            self.assertFalse(second["changed"])
            auditor.assert_called_once()
            transition_builder.assert_called_once()
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            transition_status = status["shadow_transition_dataset"]
            self.assertEqual(transition_status["status"], "materialize_error")
            self.assertEqual(
                transition_status["export_session_id"], export_session_id
            )
            self.assertEqual(transition_status["row_count"], 12)
            for field in self._SHADOW_TRANSITION_CLOSED_FLAGS:
                self.assertIs(transition_status[field], False)
            self.assertGreater(watcher._shadow_transition_retry_after, 0)
            return transition_status, transition_builder

    def test_passed_shadow_acceptance_materializes_bounded_transition_dataset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables(""), encoding="utf-8")
            data_root = root / "offline_data"
            live_result = {
                "status": "unchanged",
                "transport": "nampower_customdata_jsonl",
                "changed": False,
                "completed": True,
                "invalid_line_count": 0,
                "journal_pair_total": 12,
                "latest_target": 12,
                "latest_export_session_id": "shadow-session-complete",
                "output": str(data_root / "online_decisions" / "shadow.jsonl"),
                "manifest": str(
                    data_root / "online_decisions" / "shadow.manifest.json"
                ),
            }
            collector = SimpleNamespace(poll=lambda: dict(live_result))
            acceptance_report = {
                "status": "PASS",
                "input": {
                    "selected_export_session_id": "shadow-session-complete"
                },
                "transport_action_gate": {"status": "PASS", "passed": True},
                "causal_state_action_gate": {"status": "PASS", "passed": True},
                "outcome_reward_gate": {"status": "PASS", "passed": True},
                "deployment_allowed": False,
            }
            acceptance_output = data_root / "reports" / "acceptance.json"
            transition_receipt = {
                "status": "ok",
                "export_session_id": "shadow-session-complete",
                "row_count": 12,
                "dataset": str(
                    data_root / "online_training" / "fury_shadow_transitions_v1.jsonl"
                ),
                "manifest": str(
                    data_root
                    / "online_training"
                    / "fury_shadow_transitions_v1.manifest.json"
                ),
                "report": str(
                    data_root / "reports" / "fury_shadow_transition_dataset_v1.json"
                ),
                "dataset_sha256": "a" * 64,
                "scalar_reward_available": False,
                "recorded_active_policy_counterfactual_reward_available": False,
                "candidate_counterfactual_reward_available": False,
                "full_next_state_available": False,
                "td_transition_eligible": False,
                "offline_rl_episode_eligible": False,
                "deployment_allowed": False,
            }
            transition_builder = mock.Mock(return_value=transition_receipt)
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                shadow_live_export_collector=collector,
                shadow_transition_builder=transition_builder,
            )

            with mock.patch(
                "o2o_dps.calibration_watch.audit_shadow_pairs",
                return_value=(acceptance_report, acceptance_output),
            ):
                watcher._poll_shadow_live_export()
                watcher._poll_shadow_live_export()

            transition_builder.assert_called_once_with(
                live_result["output"],
                live_result["manifest"],
                acceptance_output,
                session_id="shadow-session-complete",
                output_path=watcher.shadow_transition_output,
                output_manifest_path=watcher.shadow_transition_manifest,
                report_path=watcher.shadow_transition_report,
            )
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["shadow_transition_dataset"], transition_receipt)

    def test_invalid_shadow_transition_identity_and_artifacts_are_isolated(
        self,
    ) -> None:
        cases = {
            "wrong_session": (
                lambda receipt, _watcher: receipt.__setitem__(
                    "export_session_id", "another-session"
                ),
                "session mismatch",
            ),
            "wrong_row_count": (
                lambda receipt, _watcher: receipt.__setitem__("row_count", 11),
                "row count mismatch",
            ),
            "wrong_dataset_path": (
                lambda receipt, watcher: receipt.__setitem__(
                    "dataset", str(watcher.shadow_transition_output.with_name("other.jsonl"))
                ),
                "dataset path mismatch",
            ),
            "wrong_manifest_path": (
                lambda receipt, watcher: receipt.__setitem__(
                    "manifest",
                    str(watcher.shadow_transition_manifest.with_name("other.json")),
                ),
                "manifest path mismatch",
            ),
            "wrong_report_path": (
                lambda receipt, watcher: receipt.__setitem__(
                    "report",
                    str(watcher.shadow_transition_report.with_name("other.json")),
                ),
                "report path mismatch",
            ),
            "invalid_dataset_hash": (
                lambda receipt, _watcher: receipt.__setitem__(
                    "dataset_sha256", "A" * 64
                ),
                "lowercase SHA-256",
            ),
        }
        for name, (mutate_receipt, expected_error) in cases.items():
            with self.subTest(case=name):
                status, _builder = self._run_failed_shadow_transition_case(
                    mutate_receipt=mutate_receipt
                )
                self.assertIn(expected_error, status["error"])

    def test_shadow_transition_closed_flags_must_be_explicit_false(self) -> None:
        for field in self._SHADOW_TRANSITION_CLOSED_FLAGS:
            for mutation in ("true", "missing"):
                with self.subTest(field=field, mutation=mutation):
                    def mutate_receipt(
                        receipt: dict[str, object],
                        _watcher: CalibrationWatcher,
                        *,
                        selected_field: str = field,
                        selected_mutation: str = mutation,
                    ) -> None:
                        if selected_mutation == "true":
                            receipt[selected_field] = True
                        else:
                            receipt.pop(selected_field)

                    status, _builder = self._run_failed_shadow_transition_case(
                        mutate_receipt=mutate_receipt
                    )
                    self.assertIn(field, status["error"])
                    self.assertIn("=false", status["error"])

    def test_unexpected_shadow_transition_builder_error_is_isolated(self) -> None:
        status, builder = self._run_failed_shadow_transition_case(
            builder_error=RuntimeError("unexpected transition builder failure")
        )

        self.assertIn("unexpected transition builder failure", status["error"])
        builder.assert_called_once()

    def test_refreshes_completed_shadow_acceptance_once_when_live_export_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables(""), encoding="utf-8")
            data_root = root / "offline_data"
            live_result = {
                "status": "unchanged",
                "transport": "nampower_customdata_jsonl",
                "changed": False,
                "completed": True,
                "invalid_line_count": 0,
                "journal_pair_total": 12,
                "latest_target": 12,
                "latest_export_session_id": "shadow-session-complete",
                "output": str(data_root / "online_decisions" / "shadow.jsonl"),
                "manifest": str(
                    data_root / "online_decisions" / "shadow.manifest.json"
                ),
            }
            collector = SimpleNamespace(poll=lambda: dict(live_result))
            acceptance_report = {
                "status": "accepted",
                "input": {
                    "selected_export_session_id": "shadow-session-complete"
                },
                "transport_action_gate": {"status": "pass"},
                "causal_state_action_gate": {"status": "pass"},
                "outcome_reward_gate": {"status": "pass"},
                "deployment_allowed": False,
            }
            acceptance_output = data_root / "reports" / "acceptance.json"
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                shadow_live_export_collector=collector,
            )

            with mock.patch(
                "o2o_dps.calibration_watch.audit_shadow_pairs",
                return_value=(acceptance_report, acceptance_output),
            ) as auditor:
                first = watcher._poll_shadow_live_export()
                second = watcher._poll_shadow_live_export()

            self.assertFalse(first["changed"])
            self.assertFalse(second["changed"])
            auditor.assert_called_once_with(
                live_result["output"],
                live_result["manifest"],
                output_path=watcher.shadow_acceptance_output,
                expected_pairs=12,
                export_session_id="shadow-session-complete",
            )
            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "shadow_acceptance_refreshed")
            self.assertEqual(
                status["shadow_acceptance"]["export_session_id"],
                "shadow-session-complete",
            )

    def test_shadow_pairs_import_without_a_terminal_campaign_and_deduplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                SHADOW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            watcher = CalibrationWatcher(source, data_root=data_root)

            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(first["status"], "waiting_for_campaign_completion")
            self.assertEqual(first["shadow_pairs"]["source_pair_total"], 1)
            self.assertEqual(first["shadow_pairs"]["new_pair_count"], 1)
            self.assertEqual(first["shadow_pairs"]["journal_pair_total"], 1)
            self.assertEqual(second["shadow_pairs"]["new_pair_count"], 0)
            self.assertEqual(second["shadow_pairs"]["journal_pair_total"], 1)
            self.assertTrue(Path(first["shadow_pairs"]["output"]).is_file())
            self.assertTrue(Path(first["shadow_pairs"]["manifest"]).is_file())
            self.assertFalse((data_root / "online_raw").exists())

    def test_compacted_calibration_ring_retains_latest_receipt_and_processes_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            data_root = root / "offline_data"
            watcher = CalibrationWatcher(source, data_root=data_root)
            watcher.runtime_directory.mkdir(parents=True, exist_ok=True)

            retained_keys = (
                "summary",
                "profile",
                "phase5_replay",
                "phase6_rage_audit",
                "phase7_joint_audit",
                "phase8_joint_audit",
                "phase9_formula_audit",
                "phase10_identification_audit",
                "phase11_external_holdout_audit",
                "phase12_audit",
                "timer_summary",
                "timer_live_debug",
            )
            old_receipt = {
                "source": str(watcher.source),
                "campaign_run_id": "campaign-old",
                "terminal_event": "CALIBRATION_CAMPAIGN_COMPLETED",
                **{key: {"status": f"old-{key}"} for key in retained_keys},
            }
            latest_receipt = {
                "source": str(watcher.source),
                "campaign_run_id": "campaign-latest",
                "terminal_event": "CALIBRATION_CAMPAIGN_INCOMPLETE",
                **{key: {"status": f"latest-{key}"} for key in retained_keys},
            }
            watcher.state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "processed_campaigns": [old_receipt, latest_receipt],
                    }
                ),
                encoding="utf-8",
            )
            source.write_text(
                SHADOW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_history_retained_shadow_processed"
            )
            self.assertIn("latest processed campaign receipt", result["message"])
            self.assertEqual(result["campaign_run_ids"], ["campaign-latest"])
            self.assertEqual(
                result["campaign_terminal_events"],
                ["CALIBRATION_CAMPAIGN_INCOMPLETE"],
            )
            for key in retained_keys:
                self.assertEqual(result[key], latest_receipt[key])
            self.assertEqual(result["shadow_pairs"]["source_pair_total"], 1)
            self.assertEqual(result["shadow_pairs"]["new_pair_count"], 1)
            self.assertEqual(result["shadow_pairs"]["journal_pair_total"], 1)
            self.assertFalse((data_root / "online_raw").exists())

    def test_imports_and_summarizes_new_campaign_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-one"), encoding="utf-8")
            data_root = root / "offline_data"
            summary_calls: list[Path] = []

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                calibration_path = Path(calibration)
                summary_calls.append(calibration_path)
                output = data_root / "calibration_summaries" / f"{calibration_path.stem}.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "run_count": 1,
                        "trial_count": 1,
                        "output": str(output),
                    }
                )

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=fake_summarizer,
            )
            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(first["status"], "campaign_imported")
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(second["profile"]["status"], "pending")
            self.assertEqual(first["campaign_run_ids"], ["campaign-one"])
            self.assertEqual(first["profile"]["status"], "pending")
            self.assertEqual(
                first["profile"]["reason"], "static_profile_not_captured"
            )
            self.assertEqual(len(summary_calls), 1)
            self.assertTrue(Path(first["import_result"]["raw_copy"]).is_file())
            self.assertTrue(Path(first["import_result"]["calibration"]).is_file())
            raw_imports = list((data_root / "online_raw").glob("*/BrainOfCat.lua"))
            self.assertEqual(len(raw_imports), 1)

            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["campaign_run_id"] for item in state["processed_campaigns"]],
                ["campaign-one"],
            )
            self.assertIn("mtime_ns", state["processed_campaigns"][0]["source_signature"])
            self.assertIn("size_bytes", state["processed_campaigns"][0]["source_signature"])
            self.assertEqual(
                state["processed_campaigns"][0]["profile"]["status"], "pending"
            )

    def test_timer_campaign_is_decoded_once_and_retained_when_already_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables(
                    "timer-campaign-run-one", campaign_id=TIMER_CAMPAIGN_ID
                ),
                encoding="utf-8",
            )
            data_root = root / "offline_data"
            timer_calls: list[tuple[Path, str, Path]] = []

            def fake_timer_summarizer(
                calibration: str | Path,
                *,
                campaign_run_id: str,
                output_path: str | Path,
            ) -> SimpleNamespace:
                output = Path(output_path)
                timer_calls.append((Path(calibration), campaign_run_id, output))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "complete",
                        "output": str(output),
                        "kind": "fury_timer_calibration_summary",
                        "campaign_id": TIMER_CAMPAIGN_ID,
                        "campaign_run_id": campaign_run_id,
                        "evidence_complete": True,
                        "blocker_count": 0,
                    }
                )

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "failed",
                        "error": "generic summary has no timer campaign analyzer",
                    }
                ),
                timer_summarizer=fake_timer_summarizer,
            )
            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(first["status"], "campaign_imported_timer_complete")
            self.assertEqual(first["summary"]["status"], "failed")
            self.assertEqual(first["timer_summary"]["status"], "complete")
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(second["timer_summary"], first["timer_summary"])
            self.assertEqual(len(timer_calls), 1)
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["timer_summary"]["status"],
                "complete",
            )

    def test_stage_d_recovery_terminal_is_imported_finalized_and_composited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            recovery_run_id = "timer-recovery-run-one"
            source.write_text(
                _savedvariables(
                    recovery_run_id,
                    campaign_id=TIMER_CAMPAIGN_ID,
                    terminal_event="CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
                ),
                encoding="utf-8",
            )
            composite_output = root / "composite_evidence.json"
            finalize_records: list[dict[str, object]] = []

            class FakeCollector:
                def poll(self) -> dict[str, object]:
                    return {"status": "ready_idle"}

                def finalize(
                    self,
                    campaign_run_id: str,
                    terminal_record: dict[str, object] | None = None,
                ) -> dict[str, object]:
                    self.assertions(campaign_run_id, terminal_record)
                    composite_output.write_text("{}\n", encoding="utf-8")
                    return {
                        "status": "terminal_bundle_ready",
                        "campaign_run_id": campaign_run_id,
                        "bundle": str(root / "debug-bundle"),
                        "manifest": str(root / "debug-bundle" / "manifest.json"),
                        "recovery_composite": {
                            "status": "complete",
                            "output": str(composite_output),
                            "checks": {"source_and_recovery_linked": True},
                        },
                    }

                @staticmethod
                def assertions(
                    campaign_run_id: str,
                    terminal_record: dict[str, object] | None,
                ) -> None:
                    if campaign_run_id != recovery_run_id:
                        raise AssertionError("wrong recovery campaign run ID")
                    if not isinstance(terminal_record, dict):
                        raise AssertionError("missing imported recovery terminal record")
                    finalize_records.append(terminal_record)

            def unexpected_timer_summarizer(*args: object, **kwargs: object) -> object:
                raise AssertionError("Stage-D recovery must use the source/recovery composite")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "failed", "error": "recovery-only import"}
                ),
                timer_summarizer=unexpected_timer_summarizer,
                timer_live_debug_collector=FakeCollector(),
            )

            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported_timer_complete")
            self.assertEqual(result["campaign_run_ids"], [recovery_run_id])
            self.assertEqual(
                result["campaign_terminal_events"],
                ["CALIBRATION_STAGE_D_RECOVERY_COMPLETED"],
            )
            self.assertEqual(result["timer_summary"]["status"], "complete")
            self.assertTrue(result["timer_summary"]["evidence_complete"])
            self.assertEqual(len(finalize_records), 1)
            self.assertEqual(
                finalize_records[0]["event"],
                "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            stored = state["processed_campaigns"][0]
            self.assertEqual(stored["terminal_event"], finalize_records[0]["event"])
            self.assertEqual(stored["timer_summary"]["status"], "complete")

    def test_timer_decoder_failure_does_not_roll_back_successful_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables(
                    "timer-campaign-run-failure", campaign_id=TIMER_CAMPAIGN_ID
                ),
                encoding="utf-8",
            )

            def failing_timer_summarizer(*args: object, **kwargs: object) -> object:
                raise ValueError("frozen timer gate could not be decoded")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok"}
                ),
                timer_summarizer=failing_timer_summarizer,
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_timer_summary_failed"
            )
            self.assertEqual(result["import_result"]["status"], "ok")
            self.assertTrue(Path(result["import_result"]["calibration"]).is_file())
            self.assertEqual(result["timer_summary"]["status"], "summary_error")
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["processed_campaigns"]), 1)
            self.assertEqual(
                state["processed_campaigns"][0]["timer_summary"]["status"],
                "summary_error",
            )

    def test_non_timer_campaign_has_explicit_not_applicable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("ordinary-run", campaign_id="ordinary_campaign"),
                encoding="utf-8",
            )

            def unexpected_timer_summarizer(*args: object, **kwargs: object) -> object:
                raise AssertionError("non-timer campaign must not enter timer decoder")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok"}
                ),
                timer_summarizer=unexpected_timer_summarizer,
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["timer_summary"]["status"], "not_applicable")
            self.assertEqual(
                result["timer_summary"]["reason"],
                "completed_timer_campaign_not_present",
            )

    def test_static_profile_generates_default_request_and_metadata_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-profile", include_static_profile=True),
                encoding="utf-8",
            )
            data_root = root / "offline_data"
            output = root / "configs" / "wowsims" / "fury_warrior_live.json"
            metadata = output.with_suffix(".metadata.json")
            profile_calls: list[tuple[list[Path], Path, Path]] = []

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "run_count": 1,
                        "trial_count": 1,
                        "output": str(root / "summary.json"),
                    }
                )

            def fake_profile_generator(
                calibration_paths: list[Path],
                *,
                output_path: Path,
                metadata_path: Path,
            ) -> SimpleNamespace:
                paths = list(calibration_paths)
                profile_calls.append((paths, output_path, metadata_path))
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text('{"raid": {}}\n', encoding="utf-8")
                metadata_path.write_text('{"race": "GNOME"}\n', encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "request": str(output_path),
                        "metadata": str(metadata_path),
                        "source": str(paths[0]),
                        "source_event": "STATIC_PROFILE_CAPTURED",
                        "talents_string": "30205020332-05050005025010051",
                    }
                )

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=fake_summarizer,
                profile_generator=fake_profile_generator,
                profile_output=output,
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["profile"]["status"], "ok")
            self.assertEqual(result["profile"]["source_event"], "STATIC_PROFILE_CAPTURED")
            self.assertEqual(len(profile_calls), 1)
            self.assertEqual(profile_calls[0][0], [Path(result["import_result"]["calibration"])])
            self.assertEqual(profile_calls[0][1:], (output.resolve(), metadata.resolve()))
            self.assertTrue(output.is_file())
            self.assertTrue(metadata.is_file())

            status = json.loads(watcher.status_path.read_text(encoding="utf-8"))
            log = [
                json.loads(line)
                for line in watcher.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(status["profile"]["status"], "ok")
            self.assertEqual(log[-1]["profile"]["status"], "ok")

    def test_old_import_without_static_profile_stays_pending_and_is_not_generated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-old"), encoding="utf-8")

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(as_dict=lambda: {"status": "ok"})

            def unexpected_profile_generator(*args: object, **kwargs: object) -> object:
                raise AssertionError("old imports must not enter profile fallback")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=fake_summarizer,
                profile_generator=unexpected_profile_generator,
                profile_output=root / "live.json",
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["profile"]["status"], "pending")
            self.assertEqual(
                result["profile"]["reason"], "static_profile_not_captured"
            )
            self.assertFalse((root / "live.json").exists())

    def test_profile_error_does_not_turn_successful_import_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-profile-error", include_static_profile=True),
                encoding="utf-8",
            )

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(as_dict=lambda: {"status": "ok"})

            def failing_profile_generator(*args: object, **kwargs: object) -> object:
                raise ValueError("live profile race was not observed")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=fake_summarizer,
                profile_generator=failing_profile_generator,
                profile_output=root / "live.json",
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["import_result"]["status"], "ok")
            self.assertEqual(result["summary"]["status"], "ok")
            self.assertEqual(result["profile"]["status"], "error")
            self.assertIn("race was not observed", result["profile"]["error"])

            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["profile"]["status"], "error"
            )
            log_record = json.loads(
                watcher.log_path.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(log_record["event"], "campaign_imported")
            self.assertEqual(log_record["profile"]["status"], "error")

    def test_static_capture_without_race_does_not_publish_template_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables(
                    "campaign-no-race",
                    include_static_profile=True,
                    static_race=None,
                ),
                encoding="utf-8",
            )
            output = root / "live.json"
            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok"}
                ),
                profile_output=output,
            )

            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["profile"]["status"], "error")
            self.assertIn("race was not observed", result["profile"]["error"])
            self.assertFalse(output.exists())

    def test_summary_failure_leaves_profile_pending_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-summary-error", include_static_profile=True),
                encoding="utf-8",
            )

            def failing_summarizer(calibration: str | Path) -> object:
                raise ValueError("summary did not converge")

            def unexpected_profile_generator(*args: object, **kwargs: object) -> object:
                raise AssertionError("profile generation must follow a successful summary")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=failing_summarizer,
                profile_generator=unexpected_profile_generator,
                profile_output=root / "live.json",
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported_summary_failed")
            self.assertEqual(result["import_result"]["status"], "ok")
            self.assertEqual(result["profile"]["status"], "pending")
            self.assertEqual(result["profile"]["reason"], "summary_failed")

    def test_phase5_summary_runs_matched_replay_and_publishes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase5", include_static_profile=True),
                encoding="utf-8",
            )
            summary_path = root / "phase5-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "analyzer": "bloodthirst_armor_strata_damage_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "live-profile.json"
            metadata_path = root / "live-profile.metadata.json"
            replay_output = root / "phase5-replay.json"
            replay_calls: list[tuple[Path, Path]] = []

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                )

            def fake_profile_generator(
                calibration_paths: list[Path],
                *,
                output_path: Path,
                metadata_path: Path,
            ) -> SimpleNamespace:
                output_path.write_text('{"raid": {}}\n', encoding="utf-8")
                metadata_path.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "request": str(output_path),
                        "metadata": str(metadata_path),
                    }
                )

            def fake_replayer(summary: Path, profile: Path) -> dict[str, object]:
                replay_calls.append((summary, profile))
                return {
                    "kind": "bloodthirst_phase5_armor_matched_replay",
                    "validation_status": "matched",
                    "task_run_id": "phase5-task-run",
                }

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=fake_summarizer,
                profile_generator=fake_profile_generator,
                profile_output=profile_path,
                profile_metadata=metadata_path,
                phase5_replayer=fake_replayer,
                phase5_replay_output=replay_output,
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(result["phase5_replay"]["status"], "matched")
            self.assertEqual(result["phase5_replay"]["validation_status"], "matched")
            self.assertEqual(replay_calls, [(summary_path, profile_path.resolve())])
            self.assertEqual(
                json.loads(replay_output.read_text(encoding="utf-8"))[
                    "validation_status"
                ],
                "matched",
            )

            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["phase5_replay"]["status"],
                "matched",
            )
            log = json.loads(
                watcher.log_path.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(log["phase5_replay"]["status"], "matched")

    def test_phase5_replay_error_preserves_successful_import_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables(
                    "campaign-phase5-error", include_static_profile=True
                ),
                encoding="utf-8",
            )
            summary_path = root / "phase5-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "analyzer": "bloodthirst_armor_strata_damage_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "live.json"

            def fake_profile_generator(
                calibration_paths: list[Path],
                *,
                output_path: Path,
                metadata_path: Path,
            ) -> SimpleNamespace:
                output_path.write_text("{}\n", encoding="utf-8")
                metadata_path.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "request": str(output_path),
                        "metadata": str(metadata_path),
                    }
                )

            def failing_replayer(summary: Path, profile: Path) -> dict[str, object]:
                raise RuntimeError("bridge exited during Phase-5 replay")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                profile_generator=fake_profile_generator,
                profile_output=profile_path,
                phase5_replayer=failing_replayer,
                phase5_replay_output=root / "replay.json",
            )
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported_replay_error")
            self.assertEqual(result["import_result"]["status"], "ok")
            self.assertEqual(result["summary"]["status"], "ok")
            self.assertEqual(result["phase5_replay"]["status"], "replay_error")
            self.assertIn("bridge exited", result["phase5_replay"]["error"])

            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            stored = state["processed_campaigns"][0]
            self.assertEqual(stored["import"]["status"], "ok")
            self.assertEqual(stored["summary"]["status"], "ok")
            self.assertEqual(stored["phase5_replay"]["status"], "replay_error")
            second = watcher.process_once()
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(second["phase5_replay"]["status"], "replay_error")

    def test_phase5_not_matched_has_distinct_non_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase5-gate", include_static_profile=True),
                encoding="utf-8",
            )
            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {"analyzer": "bloodthirst_armor_strata_damage_v1"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"

            def fake_profile_generator(
                calibration_paths: list[Path],
                *,
                output_path: Path,
                metadata_path: Path,
            ) -> SimpleNamespace:
                output_path.write_text("{}\n", encoding="utf-8")
                metadata_path.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "request": str(output_path),
                        "metadata": str(metadata_path),
                    }
                )

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                profile_generator=fake_profile_generator,
                profile_output=profile_path,
                phase5_replayer=lambda summary, profile: {
                    "kind": "bloodthirst_phase5_armor_matched_replay",
                    "validation_status": "not_matched",
                },
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_replay_not_matched"
            )
            self.assertEqual(result["phase5_replay"]["status"], "not_matched")

    def test_phase6_summary_runs_rage_audit_without_live_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase6"),
                encoding="utf-8",
            )
            summary_path = root / "phase6-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analyzer": "white_swing_rage_armor_strata_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            audit_output = root / "phase6-rage-audit.json"
            audit_calls: list[Path] = []

            def fake_auditor(summary: Path) -> dict[str, object]:
                audit_calls.append(summary)
                return {
                    "kind": "white_rage_phase6_formula_audit",
                    "validation_status": "not_matched",
                    "task_run_id": "phase6-task-run",
                }

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                phase6_rage_auditor=fake_auditor,
                phase6_rage_audit_output=audit_output,
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase6_rage_not_matched"
            )
            self.assertEqual(result["profile"]["status"], "pending")
            self.assertEqual(result["phase6_rage_audit"]["status"], "not_matched")
            self.assertEqual(audit_calls, [summary_path])
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))[
                    "validation_status"
                ],
                "not_matched",
            )

            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["phase6_rage_audit"]["status"],
                "not_matched",
            )
            second = watcher.process_once()
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(
                second["phase6_rage_audit"]["status"], "not_matched"
            )

    def test_phase6_incomplete_summary_is_not_reported_as_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase6-incomplete"), encoding="utf-8")
            summary_path = root / "phase6-summary.json"
            reason = "white-swing armor trial 1 swing target armor drifted"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                        "deferred_analysis": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analysis_reason": reason,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def unexpected_auditor(summary: Path) -> dict[str, object]:
                raise AssertionError("incomplete Phase-6 summary must not be audited")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                phase6_rage_auditor=unexpected_auditor,
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase6_summary_incomplete"
            )
            self.assertEqual(
                result["phase6_rage_audit"],
                {"status": "summary_incomplete", "reason": reason},
            )

    def test_corrected_phase6_summary_reconciles_an_already_imported_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase6-reconcile"), encoding="utf-8")
            summary_path = root / "phase6-summary.json"
            reason = "white-swing armor trial 1 swing target armor drifted"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                        "deferred_analysis": [],
                    }
                ),
                encoding="utf-8",
            )
            audit_calls: list[Path] = []

            def fake_auditor(summary: Path) -> dict[str, object]:
                audit_calls.append(summary)
                return {
                    "kind": "white_rage_phase6_formula_audit",
                    "validation_status": "not_matched",
                    "task_run_id": "phase6-task-run",
                }

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "specialized_run_count": 0,
                        "deferred_analysis_count": 1,
                        "output": str(summary_path),
                    }
                ),
                phase6_rage_auditor=fake_auditor,
            )
            first = watcher.process_once()
            self.assertEqual(
                first["status"], "campaign_imported_phase6_summary_incomplete"
            )
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analyzer": "white_swing_rage_armor_strata_v1",
                            }
                        ],
                        "task_completions": [],
                        "deferred_analysis": [],
                    }
                ),
                encoding="utf-8",
            )

            second = watcher.process_once()

            self.assertEqual(
                second["status"],
                "campaign_reconciled_phase6_rage_not_matched",
            )
            self.assertEqual(second["phase6_rage_audit"]["status"], "not_matched")
            self.assertEqual(audit_calls, [summary_path])
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            stored = state["processed_campaigns"][0]
            self.assertEqual(stored["summary"]["specialized_run_count"], 1)
            self.assertEqual(stored["summary"]["deferred_analysis_count"], 0)
            self.assertEqual(stored["phase6_rage_audit"]["status"], "not_matched")

    def test_phase6_audit_error_preserves_successful_import_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase6-error"),
                encoding="utf-8",
            )
            summary_path = root / "phase6-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analyzer": "white_swing_rage_armor_strata_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def failing_auditor(summary: Path) -> dict[str, object]:
                raise RuntimeError("Phase-6 summary fields are incomplete")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                phase6_rage_auditor=failing_auditor,
                phase6_rage_audit_output=root / "audit.json",
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase6_rage_audit_error"
            )
            self.assertEqual(result["import_result"]["status"], "ok")
            self.assertEqual(result["summary"]["status"], "ok")
            self.assertEqual(result["phase6_rage_audit"]["status"], "audit_error")
            self.assertIn(
                "fields are incomplete", result["phase6_rage_audit"]["error"]
            )

    def test_phase7_uses_latest_completed_phase6_summary_and_publishes_joint_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase7"), encoding="utf-8")
            data_root = root / "offline_data"
            summaries = data_root / "calibration_summaries"
            summaries.mkdir(parents=True)

            def phase6_document(task_run_id: str) -> dict[str, object]:
                return {
                    "kind": "brainofcat_calibration_summary",
                    "specialized_runs": [
                        {
                            "task_id": "warrior_white_swing_rage_armor_strata",
                            "task_run_id": task_run_id,
                            "analyzer": "white_swing_rage_armor_strata_v1",
                            "completion_confirmed": True,
                        }
                    ],
                }

            older = summaries / "BrainOfCat__phase6_older.json"
            newer = summaries / "BrainOfCat__phase6_newer.json"
            older.write_text(json.dumps(phase6_document("older")), encoding="utf-8")
            newer.write_text(json.dumps(phase6_document("newer")), encoding="utf-8")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

            phase7_summary = root / "phase7-summary.json"
            phase7_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_weapon_speed",
                                "analyzer": "white_swing_rage_weapon_speed_v1",
                                "completion_confirmed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            audit_output = root / "phase7-joint-audit.json"
            audit_calls: list[tuple[Path, Path]] = []

            def fake_joint_auditor(
                phase6_path: Path, phase7_path: Path
            ) -> dict[str, object]:
                audit_calls.append((phase6_path, phase7_path))
                return {
                    "kind": "white_rage_phase7_joint_formula_audit",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "replacement_formula_identified": False,
                        "simulator_patch_allowed": False,
                    },
                    "speed_source_assessment": {
                        "result": "base_speed_candidates_only"
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(phase7_summary),
                    }
                ),
                phase7_joint_auditor=fake_joint_auditor,
                phase7_joint_audit_output=audit_output,
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"],
                "campaign_imported_phase7_formula_not_identified",
            )
            self.assertEqual(audit_calls, [(newer.resolve(), phase7_summary)])
            self.assertEqual(
                result["phase7_joint_audit"]["phase6_summary"], str(newer.resolve())
            )
            self.assertEqual(
                result["phase7_joint_audit"]["speed_source_result"],
                "base_speed_candidates_only",
            )
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))["kind"],
                "white_rage_phase7_joint_formula_audit",
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["phase7_joint_audit"]["status"],
                "formula_not_identified",
            )
            second = watcher.process_once()
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(
                second["phase7_joint_audit"]["status"], "formula_not_identified"
            )

    def test_phase7_explicit_phase6_summary_overrides_newest_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase7-explicit"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            summaries = data_root / "calibration_summaries"
            summaries.mkdir(parents=True)
            phase6_shape = {
                "kind": "brainofcat_calibration_summary",
                "specialized_runs": [
                    {
                        "task_id": "warrior_white_swing_rage_armor_strata",
                        "analyzer": "white_swing_rage_armor_strata_v1",
                        "completion_confirmed": True,
                    }
                ],
            }
            specified = summaries / "specified.json"
            newest = summaries / "newest.json"
            specified.write_text(json.dumps(phase6_shape), encoding="utf-8")
            newest.write_text(json.dumps(phase6_shape), encoding="utf-8")
            os.utime(specified, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newest, ns=(2_000_000_000, 2_000_000_000))
            phase7_summary = root / "phase7-summary.json"
            phase7_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_weapon_speed",
                                "analyzer": "white_swing_rage_weapon_speed_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            audit_calls: list[tuple[Path, Path]] = []

            def fake_joint_auditor(
                phase6_path: Path, phase7_path: Path
            ) -> dict[str, object]:
                audit_calls.append((phase6_path, phase7_path))
                return {
                    "kind": "white_rage_phase7_joint_formula_audit",
                    "evidence_gate": {"sufficient": False, "reasons": ["quota"]},
                    "conclusion_gate": {
                        "replacement_formula_identified": False,
                        "simulator_patch_allowed": False,
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(phase7_summary)}
                ),
                phase6_summary=specified,
                phase7_joint_auditor=fake_joint_auditor,
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase7_insufficient_evidence"
            )
            self.assertEqual(audit_calls, [(specified.resolve(), phase7_summary)])

    def test_phase7_incomplete_summary_fails_closed_with_retained_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase7-incomplete"), encoding="utf-8"
            )
            phase7_summary = root / "phase7-summary.json"
            reason = "Phase-7 quota noncritical_no_flurry has only 3 accepted samples"
            phase7_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_weapon_speed",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def unexpected_joint_auditor(*args: object) -> dict[str, object]:
                raise AssertionError("incomplete Phase-7 summary must not be audited")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(phase7_summary)}
                ),
                phase7_joint_auditor=unexpected_joint_auditor,
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase7_summary_incomplete"
            )
            self.assertEqual(
                result["phase7_joint_audit"],
                {"status": "summary_incomplete", "reason": reason},
            )

    def test_phase7_without_completed_phase6_summary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase7-no-phase6"), encoding="utf-8"
            )
            phase7_summary = root / "phase7-summary.json"
            phase7_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_weapon_speed",
                                "analyzer": "white_swing_rage_weapon_speed_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def unexpected_joint_auditor(*args: object) -> dict[str, object]:
                raise AssertionError("joint audit must wait for a Phase-6 baseline")

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(phase7_summary)}
                ),
                phase7_joint_auditor=unexpected_joint_auditor,
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"],
                "campaign_imported_phase7_phase6_summary_unavailable",
            )
            self.assertEqual(
                result["phase7_joint_audit"]["status"],
                "phase6_summary_unavailable",
            )
            self.assertIn(
                "no completed strict Phase-6",
                result["phase7_joint_audit"]["reason"],
            )

    def test_phase7_reconciles_after_phase6_summary_becomes_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase7-reconcile"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            phase7_summary = root / "phase7-summary.json"
            phase7_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_weapon_speed",
                                "analyzer": "white_swing_rage_weapon_speed_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            audit_calls: list[tuple[Path, Path]] = []

            def fake_joint_auditor(
                phase6_path: Path, phase7_path: Path
            ) -> dict[str, object]:
                audit_calls.append((phase6_path, phase7_path))
                return {
                    "kind": "white_rage_phase7_joint_formula_audit",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "replacement_formula_identified": False,
                        "simulator_patch_allowed": False,
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(phase7_summary)}
                ),
                phase7_joint_auditor=fake_joint_auditor,
            )
            first = watcher.process_once()
            self.assertEqual(
                first["status"],
                "campaign_imported_phase7_phase6_summary_unavailable",
            )

            summary_directory = data_root / "calibration_summaries"
            summary_directory.mkdir(parents=True)
            phase6_summary = summary_directory / "phase6.json"
            phase6_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analyzer": "white_swing_rage_armor_strata_v1",
                                "completion_confirmed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            second = watcher.process_once()

            self.assertEqual(
                second["status"],
                "campaign_reconciled_phase7_formula_not_identified",
            )
            self.assertEqual(
                audit_calls, [(phase6_summary.resolve(), phase7_summary)]
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["phase7_joint_audit"]["status"],
                "formula_not_identified",
            )

    def test_phase8_selects_latest_phase6_and_phase7_and_publishes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase8"), encoding="utf-8")
            data_root = root / "offline_data"
            summaries = data_root / "calibration_summaries"
            summaries.mkdir(parents=True)

            def strict_summary(task_id: str, analyzer: str) -> dict[str, object]:
                return {
                    "kind": "brainofcat_calibration_summary",
                    "specialized_runs": [
                        {
                            "task_id": task_id,
                            "analyzer": analyzer,
                            "completion_confirmed": True,
                        }
                    ],
                }

            phase6 = summaries / "phase6.json"
            phase7_old = summaries / "phase7-old.json"
            phase7_new = summaries / "phase7-new.json"
            phase6.write_text(
                json.dumps(
                    strict_summary(
                        "warrior_white_swing_rage_armor_strata",
                        "white_swing_rage_armor_strata_v1",
                    )
                ),
                encoding="utf-8",
            )
            phase7_document = strict_summary(
                "warrior_white_swing_rage_weapon_speed",
                "white_swing_rage_weapon_speed_v1",
            )
            phase7_old.write_text(json.dumps(phase7_document), encoding="utf-8")
            phase7_new.write_text(json.dumps(phase7_document), encoding="utf-8")
            os.utime(phase7_old, ns=(1_000_000_000, 1_000_000_000))
            os.utime(phase7_new, ns=(2_000_000_000, 2_000_000_000))

            phase8_summary = root / "phase8-summary.json"
            phase8_summary.write_text(
                json.dumps(
                    strict_summary(
                        "warrior_white_swing_rage_low_damage_speed",
                        "white_swing_rage_low_damage_speed_v1",
                    )
                ),
                encoding="utf-8",
            )
            calls: list[tuple[Path, Path, Path]] = []

            def fake_auditor(
                phase6_path: Path, phase7_path: Path, phase8_path: Path
            ) -> dict[str, object]:
                calls.append((phase6_path, phase7_path, phase8_path))
                return {
                    "kind": "white_rage_phase8_joint_formula_audit",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "replacement_formula_identified": True,
                        "simulator_patch_allowed": True,
                    },
                }

            audit_output = root / "phase8-audit.json"
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(phase8_summary),
                    }
                ),
                phase8_joint_auditor=fake_auditor,
                phase8_joint_audit_output=audit_output,
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"], "campaign_imported_phase8_formula_identified"
            )
            self.assertEqual(
                calls,
                [(phase6.resolve(), phase7_new.resolve(), phase8_summary)],
            )
            self.assertTrue(
                result["phase8_joint_audit"]["simulator_patch_allowed"]
            )
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))["kind"],
                "white_rage_phase8_joint_formula_audit",
            )
            second = watcher.process_once()
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(
                second["phase8_joint_audit"]["status"], "formula_identified"
            )

    def test_phase8_without_phase7_summary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase8-no-phase7"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            summaries = data_root / "calibration_summaries"
            summaries.mkdir(parents=True)
            phase6 = summaries / "phase6.json"
            phase6.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_armor_strata",
                                "analyzer": "white_swing_rage_armor_strata_v1",
                                "completion_confirmed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            phase8_summary = root / "phase8-summary.json"
            phase8_summary.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_low_damage_speed",
                                "analyzer": "white_swing_rage_low_damage_speed_v1",
                                "completion_confirmed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def unexpected_auditor(*args: object) -> dict[str, object]:
                raise AssertionError("Phase-8 audit must wait for Phase-7")

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(phase8_summary),
                    }
                ),
                phase8_joint_auditor=unexpected_auditor,
            )

            result = watcher.process_once()

            self.assertEqual(
                result["status"],
                "campaign_imported_phase8_phase7_summary_unavailable",
            )
            self.assertEqual(
                result["phase8_joint_audit"]["status"],
                "phase7_summary_unavailable",
            )

    def test_corrected_phase8_summary_reconciliation_refreshes_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase8-reconcile"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            summaries = data_root / "calibration_summaries"
            summaries.mkdir(parents=True)

            def strict_summary(task_id: str, analyzer: str) -> dict[str, object]:
                return {
                    "kind": "brainofcat_calibration_summary",
                    "specialized_runs": [
                        {
                            "task_id": task_id,
                            "analyzer": analyzer,
                            "completion_confirmed": True,
                        }
                    ],
                }

            (summaries / "phase6.json").write_text(
                json.dumps(
                    strict_summary(
                        "warrior_white_swing_rage_armor_strata",
                        "white_swing_rage_armor_strata_v1",
                    )
                ),
                encoding="utf-8",
            )
            (summaries / "phase7.json").write_text(
                json.dumps(
                    strict_summary(
                        "warrior_white_swing_rage_weapon_speed",
                        "white_swing_rage_weapon_speed_v1",
                    )
                ),
                encoding="utf-8",
            )
            summary_path = root / "phase8-summary.json"
            reason = "selected weapon has a Crusader chance-on-hit enchant"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_low_damage_speed",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                        "deferred_analysis": [{"analysis_reason": reason}],
                    }
                ),
                encoding="utf-8",
            )

            def fake_auditor(*paths: Path) -> dict[str, object]:
                return {
                    "kind": "white_rage_phase8_joint_formula_audit",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "replacement_formula_identified": False,
                        "simulator_patch_allowed": False,
                    },
                }

            phase8_audit_output = (
                data_root
                / "sim_validation"
                / "white_rage_phase8_joint_formula_audit.json"
            )
            phase8_audit_output.parent.mkdir(parents=True, exist_ok=True)
            phase8_audit_output.write_text(
                json.dumps(
                    {
                        "kind": "white_rage_phase8_joint_formula_audit",
                        "status": "formula_identified",
                        "conclusion_gate": {"simulator_patch_allowed": True},
                    }
                ),
                encoding="utf-8",
            )

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "specialized_run_count": 0,
                        "deferred_analysis_count": 1,
                        "output": str(summary_path),
                    }
                ),
                phase8_joint_auditor=fake_auditor,
            )
            first = watcher.process_once()
            self.assertEqual(
                first["status"], "campaign_imported_phase8_summary_incomplete"
            )
            superseded = json.loads(
                phase8_audit_output.read_text(encoding="utf-8")
            )
            self.assertEqual(superseded["status"], "insufficient_evidence")
            self.assertTrue(superseded["superseded"])
            self.assertFalse(
                superseded["conclusion_gate"]["simulator_patch_allowed"]
            )

            summary_path.write_text(
                json.dumps(
                    strict_summary(
                        "warrior_white_swing_rage_low_damage_speed",
                        "white_swing_rage_low_damage_speed_v1",
                    )
                    | {"deferred_analysis": []}
                ),
                encoding="utf-8",
            )

            second = watcher.process_once()

            self.assertEqual(
                second["status"],
                "campaign_reconciled_phase8_formula_not_identified",
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            stored = state["processed_campaigns"][0]["summary"]
            self.assertEqual(stored["specialized_run_count"], 1)
            self.assertEqual(stored["deferred_analysis_count"], 0)

            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_low_damage_speed",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                        "deferred_analysis": [{"analysis_reason": reason}],
                    }
                ),
                encoding="utf-8",
            )

            third = watcher.process_once()

            self.assertEqual(
                third["status"], "campaign_reconciled_phase8_summary_incomplete"
            )
            self.assertEqual(third["phase8_joint_audit"]["reason"], reason)
            superseded_again = json.loads(
                phase8_audit_output.read_text(encoding="utf-8")
            )
            self.assertEqual(superseded_again["status"], "insufficient_evidence")
            self.assertTrue(superseded_again["superseded"])
            self.assertFalse(
                superseded_again["conclusion_gate"]["simulator_patch_allowed"]
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            stored = state["processed_campaigns"][0]["summary"]
            self.assertEqual(stored["specialized_run_count"], 0)
            self.assertEqual(stored["deferred_analysis_count"], 1)
            superseded_mtime = phase8_audit_output.stat().st_mtime_ns

            fourth = watcher.process_once()

            self.assertEqual(fourth["status"], "campaign_already_imported")
            self.assertEqual(
                phase8_audit_output.stat().st_mtime_ns, superseded_mtime
            )

    def test_phase9_publishes_preregistered_formula_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase9"), encoding="utf-8")
            data_root = root / "offline_data"
            summary_path = root / "phase9-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_formula_holdout",
                                "analyzer": "white_swing_rage_formula_holdout_v1",
                                "completion_confirmed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            calls: list[Path] = []

            def fake_auditor(path: Path) -> dict[str, object]:
                calls.append(path)
                return {
                    "schema_version": 1,
                    "kind": "white_rage_phase9_formula_audit",
                    "status": "formula_identified",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "selected_candidate_id": "M2_10_9D_plus_14_9s_noncrit_10_3s_crit",
                        "replacement_formula_identified": True,
                        "simulator_patch_allowed": True,
                    },
                }

            audit_output = root / "phase9-audit.json"
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "output": str(summary_path),
                    }
                ),
                phase9_formula_auditor=fake_auditor,
                phase9_formula_audit_output=audit_output,
            )

            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(
                first["status"], "campaign_imported_phase9_formula_identified"
            )
            self.assertEqual(calls, [summary_path])
            self.assertTrue(
                first["phase9_formula_audit"]["simulator_patch_allowed"]
            )
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))["status"],
                "formula_identified",
            )
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertEqual(
                second["phase9_formula_audit"]["selected_candidate_id"],
                "M2_10_9D_plus_14_9s_noncrit_10_3s_crit",
            )

    def test_phase9_corrected_summary_reconciles_fail_closed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("campaign-phase9-reconcile"), encoding="utf-8"
            )
            data_root = root / "offline_data"
            summary_path = root / "phase9-summary.json"
            reason = "Phase-9 raw-tenths evidence is incomplete"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [],
                        "task_completions": [
                            {
                                "task_id": "warrior_white_swing_rage_formula_holdout",
                                "analysis_status": "incomplete_evidence",
                                "analysis_reason": reason,
                            }
                        ],
                        "deferred_analysis": [{"analysis_reason": reason}],
                    }
                ),
                encoding="utf-8",
            )
            calls: list[Path] = []

            def fake_auditor(path: Path) -> dict[str, object]:
                calls.append(path)
                return {
                    "schema_version": 1,
                    "kind": "white_rage_phase9_formula_audit",
                    "status": "formula_not_identified",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "selected_candidate_id": None,
                        "replacement_formula_identified": False,
                        "simulator_patch_allowed": False,
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "specialized_run_count": 0,
                        "deferred_analysis_count": 1,
                        "output": str(summary_path),
                    }
                ),
                phase9_formula_auditor=fake_auditor,
            )
            first = watcher.process_once()
            self.assertEqual(
                first["status"], "campaign_imported_phase9_summary_incomplete"
            )
            self.assertFalse(
                first["phase9_formula_audit"]["simulator_patch_allowed"]
            )

            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_runs": [
                            {
                                "task_id": "warrior_white_swing_rage_formula_holdout",
                                "analyzer": "white_swing_rage_formula_holdout_v1",
                                "completion_confirmed": True,
                            }
                        ],
                        "deferred_analysis": [],
                    }
                ),
                encoding="utf-8",
            )

            second = watcher.process_once()

            self.assertEqual(
                second["status"],
                "campaign_reconciled_phase9_formula_not_identified",
            )
            self.assertEqual(calls, [summary_path])
            self.assertFalse(
                second["phase9_formula_audit"]["simulator_patch_allowed"]
            )

    def test_new_campaign_id_in_later_write_is_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            data_root = root / "offline_data"

            def fake_summarizer(calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "run_count": 1,
                        "trial_count": 1,
                        "output": str(root / "summary.json"),
                    }
                )

            watcher = CalibrationWatcher(source, data_root=data_root, summarizer=fake_summarizer)
            source.write_text(_savedvariables("campaign-one"), encoding="utf-8")
            watcher.process_once()
            source.write_text(_savedvariables("campaign-two-longer"), encoding="utf-8")
            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["campaign_run_id"] for item in state["processed_campaigns"]],
                ["campaign-one", "campaign-two-longer"],
            )

    def test_waits_when_source_has_no_campaign_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables(""), encoding="utf-8")
            watcher = CalibrationWatcher(source, data_root=root / "offline_data")

            result = watcher.process_once()

            self.assertEqual(result["status"], "waiting_for_campaign_completion")
            self.assertFalse((root / "offline_data" / "online_raw").exists())

    def test_imports_incomplete_campaign_as_a_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(
                _savedvariables(
                    "campaign-partial",
                    terminal_event="CALIBRATION_CAMPAIGN_INCOMPLETE",
                ),
                encoding="utf-8",
            )
            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok"}
                ),
            )

            result = watcher.process_once()

            self.assertEqual(result["status"], "campaign_imported")
            self.assertEqual(
                result["campaign_terminal_events"],
                ["CALIBRATION_CAMPAIGN_INCOMPLETE"],
            )
            self.assertTrue(result["message"].startswith("Incomplete terminal"))
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["terminal_event"],
                "CALIBRATION_CAMPAIGN_INCOMPLETE",
            )

    def test_rejects_completion_marker_without_campaign_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "BrainOfCat.lua"
            source.write_text(_savedvariables(None), encoding="utf-8")

            with self.assertRaises(CalibrationWatchError) as raised:
                inspect_campaign_completions(source)

            self.assertIn("lack marker.campaignRunId", str(raised.exception))

    def test_top_level_campaign_run_id_is_not_accepted_as_completion_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "BrainOfCat.lua"
            source.write_text(
                _savedvariables("top-level-is-wrong", top_level_only=True),
                encoding="utf-8",
            )

            with self.assertRaises(CalibrationWatchError) as raised:
                inspect_campaign_completions(source)

            self.assertIn("lack marker.campaignRunId", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
