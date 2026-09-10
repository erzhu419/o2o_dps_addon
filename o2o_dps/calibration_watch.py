"""Watch one Windows WoW SavedVariables file for a completed campaign.

WoW only writes SavedVariables on logout or reload.  This process waits for a
stable write of the explicitly selected ``BrainOfCat.lua``, finds a new
``CALIBRATION_CAMPAIGN_COMPLETED`` marker, then runs the existing importer and
calibration summarizer.  It never scans account directories and never writes
to the WoW installation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Sequence

from .bloodthirst_armor_matched_replay import (
    DEFAULT_BRIDGE as DEFAULT_REPLAY_BRIDGE,
    EXPECTED_ANALYZER as PHASE5_ANALYZER,
    run_from_paths as run_phase5_matched_replay,
)
from .calibration_summary import CalibrationSummaryError, summarize_calibration
from .import_savedvariables import (
    DEFAULT_DATA_ROOT,
    SavedVariablesImportError,
    _Parser,
    _extract_records,
    import_savedvariables,
    publish_shadow_pairs,
)
from .wowsims_profile import (
    DEFAULT_OUTPUT as DEFAULT_WOWSIMS_PROFILE_OUTPUT,
    WowsimsProfileError,
    generate_wowsims_profile,
)
from .sim_bridge import SimulatorBridge
from .white_rage_formula_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE6_RAGE_AUDIT_OUTPUT,
    EXPECTED_ANALYZER as PHASE6_RAGE_ANALYZER,
    WHITE_RAGE_ARMOR_TASK as PHASE6_RAGE_TASK,
    run_from_path as run_phase6_rage_audit,
)
from .white_rage_phase7_formula_audit import (
    AUDIT_SCHEMA_VERSION as PHASE7_AUDIT_SCHEMA_VERSION,
    DEFAULT_OUTPUT as DEFAULT_PHASE7_JOINT_AUDIT_OUTPUT,
    PHASE7_ANALYZER,
    PHASE7_TASK,
    run_from_paths as run_phase7_joint_audit,
)
from .white_rage_phase8_formula_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE8_JOINT_AUDIT_OUTPUT,
    PHASE8_ANALYZER,
    PHASE8_TASK,
    run_from_paths as run_phase8_joint_audit,
)
from .white_rage_phase9_formula_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE9_FORMULA_AUDIT_OUTPUT,
    PHASE9_ANALYZER,
    PHASE9_TASK,
    run_from_path as run_phase9_formula_audit,
)
from .white_rage_phase10_identification_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE10_IDENTIFICATION_AUDIT_OUTPUT,
    PHASE10_ANALYZER,
    PHASE10_TASK,
    run_from_path as run_phase10_identification_audit,
)
from .white_rage_phase11_external_holdout_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE11_EXTERNAL_HOLDOUT_AUDIT_OUTPUT,
    PHASE11_ANALYZER,
    PHASE11_TASK,
    run_from_path as run_phase11_external_holdout_audit,
)
from .fury_current_build_phase12_audit import (
    DEFAULT_OUTPUT as DEFAULT_PHASE12_AUDIT_OUTPUT,
    DEFAULT_PREREGISTRATION as DEFAULT_PHASE12_PREREGISTRATION,
    EXTERNAL_MECHANISM_BOUNDARY_NAMES,
    MECHANISM_GATE_NAMES,
    PHASE12_ANALYZER,
    PHASE12_CAMPAIGN_ID,
    PHASE12_REPAIR_CAMPAIGN_ID,
    PHASE12_REPAIR_TASK_ORDER,
    PHASE12_TASK_ORDER,
    run_from_path as run_phase12_audit,
)
from .fury_timer_calibration_summary import (
    CAMPAIGN_ID as TIMER_CAMPAIGN_ID,
    COMPLETION_EVENT as TIMER_COMPLETION_EVENT,
    FuryTimerCalibrationSummaryError,
    summarize_fury_timer_calibration,
)
from .timer_live_debug import TimerLiveDebugCollector
from .shadow_live_export import ShadowLiveExportCollector
from .shadow_pair_acceptance_v1 import (
    ShadowPairAcceptanceError,
    audit_shadow_pairs,
)
from .fury_shadow_transition_dataset_v1 import (
    FuryShadowTransitionDatasetError,
    materialize_fury_shadow_transition_dataset,
)


SCHEMA_VERSION = 1
TIMER_RECOVERY_COMPLETION_EVENT = "CALIBRATION_STAGE_D_RECOVERY_COMPLETED"
CAMPAIGN_TERMINAL_EVENTS = frozenset(
    {
        "CALIBRATION_CAMPAIGN_COMPLETED",
        "CALIBRATION_CAMPAIGN_INCOMPLETE",
        TIMER_RECOVERY_COMPLETION_EVENT,
    }
)
STATIC_PROFILE_EVENT = "STATIC_PROFILE_CAPTURED"
DEFAULT_RUNTIME_DIRECTORY = DEFAULT_DATA_ROOT / "calibration_watch"
DEFAULT_PHASE5_REPLAY_NAME = "bloodthirst_phase5_armor_matched_replay.json"


class CalibrationWatchError(RuntimeError):
    """The watcher cannot inspect or publish the selected campaign."""


class UnstableSavedVariablesError(CalibrationWatchError):
    """The source changed while it was being read."""


@dataclass(frozen=True)
class SourceSignature:
    mtime_ns: int
    size_bytes: int

    def as_dict(self) -> dict[str, int]:
        return {
            "mtime_ns": self.mtime_ns,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class CampaignCompletion:
    campaign_run_id: str
    sequence: int
    terminal_event: str
    campaign_id: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _source_signature(path: Path) -> SourceSignature:
    try:
        stat = path.stat()
    except OSError as error:
        raise CalibrationWatchError(f"cannot stat SavedVariables source {path}: {error}") from error
    if not path.is_file():
        raise CalibrationWatchError(f"SavedVariables source is not a file: {path}")
    return SourceSignature(mtime_ns=stat.st_mtime_ns, size_bytes=stat.st_size)


def _inspect_savedvariables(
    source_lua: str | Path,
) -> tuple[
    SourceSignature,
    list[CampaignCompletion],
    list[tuple[int, dict[str, Any]]],
]:
    """Parse one stable file once for campaign markers and Shadow pairs."""

    source = Path(source_lua).expanduser().resolve()
    before = _source_signature(source)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise CalibrationWatchError(f"cannot read SavedVariables source {source}: {error}") from error
    after = _source_signature(source)
    if before != after:
        raise UnstableSavedVariablesError(
            f"SavedVariables source changed while being read: {source}"
        )

    try:
        assignments = _Parser(text, source.name).parse()
        decisions, calibration, _ = _extract_records(assignments)
    except SavedVariablesImportError as error:
        raise CalibrationWatchError(f"cannot decode SavedVariables source {source}: {error}") from error

    latest_by_run: dict[str, CampaignCompletion] = {}
    invalid_sequences: list[int] = []
    for _, record in calibration:
        terminal_event = record.get("event")
        if terminal_event not in CAMPAIGN_TERMINAL_EVENTS:
            continue
        sequence = record["sequence"]
        marker = record.get("marker")
        campaign_run_id = marker.get("campaignRunId") if isinstance(marker, dict) else None
        campaign_id = marker.get("campaignId") if isinstance(marker, dict) else None
        if not isinstance(campaign_run_id, str) or not campaign_run_id.strip():
            invalid_sequences.append(sequence)
            continue
        completion = CampaignCompletion(
            campaign_run_id.strip(),
            sequence,
            terminal_event,
            campaign_id.strip()
            if isinstance(campaign_id, str) and campaign_id.strip()
            else None,
        )
        previous = latest_by_run.get(completion.campaign_run_id)
        if previous is None or completion.sequence > previous.sequence:
            latest_by_run[completion.campaign_run_id] = completion

    if invalid_sequences:
        joined = ", ".join(str(value) for value in invalid_sequences)
        raise CalibrationWatchError(
            f"campaign terminal marker(s) at sequence {joined} lack marker.campaignRunId"
        )
    return (
        after,
        sorted(latest_by_run.values(), key=lambda item: item.sequence),
        decisions,
    )


def inspect_campaign_completions(
    source_lua: str | Path,
) -> tuple[SourceSignature, list[CampaignCompletion]]:
    """Read one stable file and return campaign terminal markers in sequence order."""

    signature, completions, _ = _inspect_savedvariables(source_lua)
    return signature, completions


def _contains_static_profile(calibration_jsonl: str | Path) -> bool:
    """Return whether this import contains an explicit static-profile capture."""

    path = Path(calibration_jsonl)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise WowsimsProfileError(
                    f"invalid calibration JSONL at {path}:{line_number}: {error.msg}"
                ) from error
            if isinstance(record, dict) and record.get("event") == STATIC_PROFILE_EVENT:
                return True
    return False


def _terminal_record(
    calibration_jsonl: str | Path,
    completion: CampaignCompletion,
) -> dict[str, Any] | None:
    """Return the imported terminal row that established ``completion``."""

    path = Path(calibration_jsonl)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise CalibrationWatchError(
                    f"invalid calibration JSONL at {path}:{line_number}: {error.msg}"
                ) from error
            if not isinstance(record, dict):
                continue
            marker = record.get("marker")
            marker_run_id = (
                marker.get("campaignRunId") if isinstance(marker, dict) else None
            )
            if (
                record.get("sequence") == completion.sequence
                and record.get("event") == completion.terminal_event
                and marker_run_id == completion.campaign_run_id
            ):
                return record
    return None


def _summary_contains_phase5(summary_output: Any) -> bool:
    """Return whether a successful summary contains the Phase-5 analyzer."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict) and run.get("analyzer") == PHASE5_ANALYZER
        for run in runs
    )


def _summary_contains_phase6_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the Phase-6 rage analyzer."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE6_RAGE_TASK
        and run.get("analyzer") == PHASE6_RAGE_ANALYZER
        for run in runs
    )


def _summary_phase6_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason when a completed Phase-6 task could not be analyzed."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if (
            isinstance(task, dict)
            and task.get("task_id") == PHASE6_RAGE_TASK
            and task.get("analysis_status") == "incomplete_evidence"
        ):
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-6 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_phase7_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the strict Phase-7 run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE7_TASK
        and run.get("analyzer") == PHASE7_ANALYZER
        for run in runs
    )


def _summary_phase7_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason for a completed but unanalyzable Phase-7 task."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if isinstance(task, dict) and task.get("task_id") == PHASE7_TASK:
            if task.get("analysis_status") != "incomplete_evidence":
                continue
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-7 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_phase8_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the strict Phase-8 run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE8_TASK
        and run.get("analyzer") == PHASE8_ANALYZER
        for run in runs
    )


def _summary_phase8_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason for a completed but unanalyzable Phase-8 task."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if isinstance(task, dict) and task.get("task_id") == PHASE8_TASK:
            if task.get("analysis_status") != "incomplete_evidence":
                continue
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-8 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_phase9_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the strict Phase-9 run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE9_TASK
        and run.get("analyzer") == PHASE9_ANALYZER
        for run in runs
    )


def _summary_phase9_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason for a completed but unanalyzable Phase-9 task."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if isinstance(task, dict) and task.get("task_id") == PHASE9_TASK:
            if task.get("analysis_status") != "incomplete_evidence":
                continue
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-9 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_phase10_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the strict Phase-10 run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE10_TASK
        and run.get("analyzer") == PHASE10_ANALYZER
        for run in runs
    )


def _summary_phase10_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason for a completed but unanalyzable Phase-10 task."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if isinstance(task, dict) and task.get("task_id") == PHASE10_TASK:
            if task.get("analysis_status") != "incomplete_evidence":
                continue
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-10 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_phase11_rage(summary_output: Any) -> bool:
    """Return whether a successful summary contains the strict Phase-11 run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE11_TASK
        and run.get("analyzer") == PHASE11_ANALYZER
        for run in runs
    )


def _summary_phase11_incomplete_reason(summary_output: Any) -> str | None:
    """Return the retained reason for a completed but unanalyzable Phase-11 task."""

    if not isinstance(summary_output, str) or not summary_output:
        return None
    path = Path(summary_output)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return None
    tasks = document.get("task_completions")
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if isinstance(task, dict) and task.get("task_id") == PHASE11_TASK:
            if task.get("analysis_status") != "incomplete_evidence":
                continue
            reason = task.get("analysis_reason")
            return (
                reason
                if isinstance(reason, str) and reason
                else "completed Phase-11 task has incomplete analysis evidence"
            )
    return None


def _summary_contains_terminal_phase12(summary_output: Any) -> bool:
    """Return whether the summary has a structurally complete Phase-12 terminal run."""

    if not isinstance(summary_output, str) or not summary_output:
        return False
    path = Path(summary_output)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_campaigns")
    if not isinstance(runs, list):
        return False
    repair_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("campaign_id") == PHASE12_REPAIR_CAMPAIGN_ID
    ]
    candidate_runs = repair_runs if repair_runs else runs
    for run in candidate_runs:
        if not isinstance(run, dict):
            continue
        tasks = run.get("tasks")
        observed_order = tuple(
            task.get("task_id")
            for task in tasks
            if isinstance(task, dict) and isinstance(task.get("task_id"), str)
        ) if isinstance(tasks, list) else ()
        campaign_id = run.get("campaign_id")
        expected_order = (
            PHASE12_REPAIR_TASK_ORDER
            if campaign_id == PHASE12_REPAIR_CAMPAIGN_ID
            else PHASE12_TASK_ORDER
        )
        if (
            campaign_id in {PHASE12_CAMPAIGN_ID, PHASE12_REPAIR_CAMPAIGN_ID}
            and run.get("analyzer") == PHASE12_ANALYZER
            and (
                run.get("terminal_confirmed") is True
                or run.get("completion_confirmed") is True
            )
            and run.get("task_count_consistent") is True
            and run.get("declared_task_count") == len(expected_order)
            and run.get("observed_task_count") == len(expected_order)
            and observed_order == expected_order
        ):
            return True
    return False


def _is_completed_phase6_summary(path: Path) -> bool:
    """Return whether ``path`` is a completed strict Phase-6 summary."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE6_RAGE_TASK
        and run.get("analyzer") == PHASE6_RAGE_ANALYZER
        and run.get("completion_confirmed") is True
        for run in runs
    )


def _is_completed_phase7_summary(path: Path) -> bool:
    """Return whether ``path`` is a completed strict Phase-7 summary."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("kind") != "brainofcat_calibration_summary":
        return False
    runs = document.get("specialized_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == PHASE7_TASK
        and run.get("analyzer") == PHASE7_ANALYZER
        and run.get("completion_confirmed") is True
        for run in runs
    )


class CalibrationWatcher:
    """Stateful importer for one explicit character SavedVariables file."""

    def __init__(
        self,
        source_lua: str | Path,
        *,
        data_root: str | Path = DEFAULT_DATA_ROOT,
        runtime_directory: str | Path | None = None,
        importer: Callable[..., Any] = import_savedvariables,
        summarizer: Callable[..., Any] = summarize_calibration,
        profile_generator: Callable[..., Any] = generate_wowsims_profile,
        profile_output: str | Path = DEFAULT_WOWSIMS_PROFILE_OUTPUT,
        profile_metadata: str | Path | None = None,
        phase5_replayer: Callable[[Path, Path], dict[str, Any]] | None = None,
        phase5_replay_bridge: str | Path = DEFAULT_REPLAY_BRIDGE,
        phase5_replay_output: str | Path | None = None,
        phase6_rage_auditor: Callable[[Path], dict[str, Any]] | None = None,
        phase6_rage_audit_output: str | Path | None = None,
        phase6_summary: str | Path | None = None,
        phase7_joint_auditor: Callable[[Path, Path], dict[str, Any]] | None = None,
        phase7_joint_audit_output: str | Path | None = None,
        phase7_summary: str | Path | None = None,
        phase8_joint_auditor: (
            Callable[[Path, Path, Path], dict[str, Any]] | None
        ) = None,
        phase8_joint_audit_output: str | Path | None = None,
        phase9_formula_auditor: Callable[[Path], dict[str, Any]] | None = None,
        phase9_formula_audit_output: str | Path | None = None,
        phase10_identification_auditor: Callable[[Path], dict[str, Any]] | None = None,
        phase10_identification_audit_output: str | Path | None = None,
        phase11_external_holdout_auditor: Callable[[Path], dict[str, Any]] | None = None,
        phase11_external_holdout_audit_output: str | Path | None = None,
        phase12_auditor: Callable[[Path], dict[str, Any]] | None = None,
        phase12_audit_output: str | Path | None = None,
        phase12_preregistration: str | Path = DEFAULT_PHASE12_PREREGISTRATION,
        timer_summarizer: Callable[..., Any] | None = None,
        timer_summary_directory: str | Path | None = None,
        timer_live_debug_collector: Any | None = None,
        shadow_pair_publisher: Callable[..., Any] = publish_shadow_pairs,
        shadow_live_export_collector: Any | None = None,
        shadow_transition_builder: Callable[..., Any] = (
            materialize_fury_shadow_transition_dataset
        ),
    ) -> None:
        self.source = Path(source_lua).expanduser().resolve()
        self.data_root = Path(data_root).expanduser().resolve()
        self.runtime_directory = (
            Path(runtime_directory).expanduser().resolve()
            if runtime_directory is not None
            else (self.data_root / "calibration_watch").resolve()
        )
        self.state_path = self.runtime_directory / "state.json"
        self.status_path = self.runtime_directory / "status.json"
        self.log_path = self.runtime_directory / "watch.log"
        self.pid_path = self.runtime_directory / "watcher.pid"
        self.importer = importer
        self.summarizer = summarizer
        self.profile_generator = profile_generator
        self.profile_output = Path(profile_output).expanduser().resolve()
        self.profile_metadata = (
            Path(profile_metadata).expanduser().resolve()
            if profile_metadata is not None
            else self.profile_output.with_suffix(".metadata.json")
        )
        self.phase5_replayer = phase5_replayer
        self.phase5_replay_bridge = Path(phase5_replay_bridge).expanduser().resolve()
        self.phase5_replay_output = (
            Path(phase5_replay_output).expanduser().resolve()
            if phase5_replay_output is not None
            else (self.data_root / "sim_validation" / DEFAULT_PHASE5_REPLAY_NAME)
        )
        self.phase6_rage_auditor = phase6_rage_auditor
        self.phase6_rage_audit_output = (
            Path(phase6_rage_audit_output).expanduser().resolve()
            if phase6_rage_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE6_RAGE_AUDIT_OUTPUT.name
            )
        )
        self.phase6_summary = (
            Path(phase6_summary).expanduser().resolve()
            if phase6_summary is not None
            else None
        )
        self.phase7_joint_auditor = phase7_joint_auditor
        self.phase7_joint_audit_output = (
            Path(phase7_joint_audit_output).expanduser().resolve()
            if phase7_joint_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE7_JOINT_AUDIT_OUTPUT.name
            )
        )
        self.phase7_summary = (
            Path(phase7_summary).expanduser().resolve()
            if phase7_summary is not None
            else None
        )
        self.phase8_joint_auditor = phase8_joint_auditor
        self.phase8_joint_audit_output = (
            Path(phase8_joint_audit_output).expanduser().resolve()
            if phase8_joint_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE8_JOINT_AUDIT_OUTPUT.name
            )
        )
        self.phase9_formula_auditor = phase9_formula_auditor
        self.phase9_formula_audit_output = (
            Path(phase9_formula_audit_output).expanduser().resolve()
            if phase9_formula_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE9_FORMULA_AUDIT_OUTPUT.name
            )
        )
        self.phase10_identification_auditor = phase10_identification_auditor
        self.phase10_identification_audit_output = (
            Path(phase10_identification_audit_output).expanduser().resolve()
            if phase10_identification_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE10_IDENTIFICATION_AUDIT_OUTPUT.name
            )
        )
        self.phase11_external_holdout_auditor = phase11_external_holdout_auditor
        self.phase11_external_holdout_audit_output = (
            Path(phase11_external_holdout_audit_output).expanduser().resolve()
            if phase11_external_holdout_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE11_EXTERNAL_HOLDOUT_AUDIT_OUTPUT.name
            )
        )
        self.phase12_auditor = phase12_auditor
        self.phase12_audit_output = (
            Path(phase12_audit_output).expanduser().resolve()
            if phase12_audit_output is not None
            else (
                self.data_root
                / "sim_validation"
                / DEFAULT_PHASE12_AUDIT_OUTPUT.name
            )
        )
        self.phase12_preregistration = (
            Path(phase12_preregistration).expanduser().resolve()
        )
        self.timer_summarizer = timer_summarizer
        self.timer_summary_directory = (
            Path(timer_summary_directory).expanduser().resolve()
            if timer_summary_directory is not None
            else (self.data_root / "timer_calibration_summaries").resolve()
        )
        self.timer_live_debug = (
            timer_live_debug_collector
            if timer_live_debug_collector is not None
            else TimerLiveDebugCollector(
                self.source,
                data_root=self.data_root,
                runtime_directory=self.runtime_directory,
            )
        )
        self.shadow_pair_publisher = shadow_pair_publisher
        self.shadow_live_export = (
            shadow_live_export_collector
            if shadow_live_export_collector is not None
            else ShadowLiveExportCollector(
                self.source,
                data_root=self.data_root,
                publisher=self.shadow_pair_publisher,
            )
        )
        self._shadow_pairs_status: dict[str, Any] | None = None
        self._shadow_acceptance_status: dict[str, Any] | None = None
        self._shadow_transition_status: dict[str, Any] | None = None
        self._shadow_transition_retry_after = 0.0
        self.shadow_acceptance_output = (
            self.data_root
            / "reports"
            / "fury_shadow_pair_acceptance_v1.json"
        ).resolve()
        self.shadow_transition_builder = shadow_transition_builder
        self.shadow_transition_output = (
            self.data_root
            / "online_training"
            / "fury_shadow_transitions_v1.jsonl"
        ).resolve()
        self.shadow_transition_manifest = self.shadow_transition_output.with_suffix(
            ".manifest.json"
        )
        self.shadow_transition_report = (
            self.data_root
            / "reports"
            / "fury_shadow_transition_dataset_v1.json"
        ).resolve()

    def _timer_summary_document(
        self, calibration_path: Path, completion: CampaignCompletion
    ) -> dict[str, Any]:
        if completion.campaign_id != TIMER_CAMPAIGN_ID:
            return {
                "status": "not_applicable",
                "reason": "completed_timer_campaign_not_present",
                "campaign_run_id": completion.campaign_run_id,
            }
        if completion.terminal_event == TIMER_RECOVERY_COMPLETION_EVENT:
            return {
                "status": "awaiting_recovery_composite",
                "reason": "stage_d_recovery_requires_frozen_source_composite",
                "campaign_id": TIMER_CAMPAIGN_ID,
                "campaign_run_id": completion.campaign_run_id,
            }
        if completion.terminal_event != TIMER_COMPLETION_EVENT:
            return {
                "status": "not_applicable",
                "reason": "timer_campaign_requires_completed_terminal_event",
                "campaign_run_id": completion.campaign_run_id,
            }
        output = self.timer_summary_directory / (
            f"{calibration_path.stem}__seq{completion.sequence}.json"
        )
        try:
            if self.timer_summarizer is None:
                result = summarize_fury_timer_calibration(
                    calibration_path,
                    campaign_run_id=completion.campaign_run_id,
                    output_path=output,
                )
            else:
                result = self.timer_summarizer(
                    calibration_path,
                    campaign_run_id=completion.campaign_run_id,
                    output_path=output,
                )
            document = result.as_dict() if hasattr(result, "as_dict") else result
            if not isinstance(document, dict):
                raise TypeError("timer summarizer must return a result object or mapping")
            if document.get("status") not in {"complete", "incomplete_evidence"}:
                raise ValueError(
                    "timer summary status must be complete or incomplete_evidence"
                )
            if document.get("campaign_run_id") != completion.campaign_run_id:
                raise ValueError("timer summary campaign_run_id does not match terminal")
            return dict(document)
        except (
            FuryTimerCalibrationSummaryError,
            OSError,
            TypeError,
            ValueError,
            AttributeError,
        ) as error:
            return {
                "status": "summary_error",
                "error": str(error),
                "output": str(output),
                "campaign_id": TIMER_CAMPAIGN_ID,
                "campaign_run_id": completion.campaign_run_id,
            }

    def _run_phase5_replay(
        self, summary_path: Path, profile_path: Path
    ) -> dict[str, Any]:
        if self.phase5_replayer is not None:
            return self.phase5_replayer(summary_path, profile_path)
        with SimulatorBridge(self.phase5_replay_bridge) as bridge:
            return run_phase5_matched_replay(summary_path, profile_path, bridge)

    def _run_phase6_rage_audit(self, summary_path: Path) -> dict[str, Any]:
        if self.phase6_rage_auditor is not None:
            return self.phase6_rage_auditor(summary_path)
        return run_phase6_rage_audit(summary_path)

    def _phase6_audit_document(self, summary_path: Path) -> dict[str, Any]:
        try:
            audit_result = self._run_phase6_rage_audit(summary_path)
            if not isinstance(audit_result, dict):
                raise TypeError("Phase-6 rage auditor must return a JSON object")
            validation_status = audit_result.get("validation_status")
            if validation_status not in {
                "matched",
                "not_matched",
                "insufficient_evidence",
            }:
                raise ValueError(
                    "Phase-6 rage audit validation_status must be matched, "
                    "not_matched, or insufficient_evidence"
                )
            self._write_json(self.phase6_rage_audit_output, audit_result)
            return {
                "status": validation_status,
                "validation_status": validation_status,
                "output": str(self.phase6_rage_audit_output),
                "kind": audit_result.get("kind"),
                "task_run_id": audit_result.get("task_run_id"),
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase6_rage_audit_output),
            }

    def _select_phase6_summary(self, phase7_summary_path: Path) -> Path:
        if self.phase6_summary is not None:
            if not self.phase6_summary.is_file():
                raise FileNotFoundError(
                    f"specified Phase-6 summary is not a file: {self.phase6_summary}"
                )
            if not _is_completed_phase6_summary(self.phase6_summary):
                raise ValueError(
                    "specified Phase-6 summary does not contain a completed strict "
                    f"{PHASE6_RAGE_TASK} run: {self.phase6_summary}"
                )
            return self.phase6_summary

        summary_directory = self.data_root / "calibration_summaries"
        if summary_directory.is_dir():
            candidates = sorted(
                (
                    path.resolve()
                    for path in summary_directory.glob("*.json")
                    if path.resolve() != phase7_summary_path.resolve()
                    and _is_completed_phase6_summary(path)
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                reverse=True,
            )
            if candidates:
                return candidates[0]
        raise FileNotFoundError(
            "no completed strict Phase-6 white-rage summary was found under "
            f"{summary_directory}"
        )

    def _run_phase7_joint_audit(
        self, phase6_summary_path: Path, phase7_summary_path: Path
    ) -> dict[str, Any]:
        if self.phase7_joint_auditor is not None:
            return self.phase7_joint_auditor(
                phase6_summary_path, phase7_summary_path
            )
        return run_phase7_joint_audit(phase6_summary_path, phase7_summary_path)

    def _phase7_joint_audit_document(
        self, phase7_summary_path: Path
    ) -> dict[str, Any]:
        try:
            phase6_summary_path = self._select_phase6_summary(phase7_summary_path)
        except (OSError, ValueError) as error:
            return {
                "status": "phase6_summary_unavailable",
                "reason": str(error),
                "output": str(self.phase7_joint_audit_output),
            }

        try:
            audit_result = self._run_phase7_joint_audit(
                phase6_summary_path, phase7_summary_path
            )
            if not isinstance(audit_result, dict):
                raise TypeError("Phase-7 joint auditor must return a JSON object")
            if audit_result.get("kind") != "white_rage_phase7_joint_formula_audit":
                raise ValueError(
                    "Phase-7 joint audit kind must be "
                    "white_rage_phase7_joint_formula_audit"
                )
            evidence_gate = audit_result.get("evidence_gate")
            conclusion_gate = audit_result.get("conclusion_gate")
            if not isinstance(evidence_gate, dict) or type(
                evidence_gate.get("sufficient")
            ) is not bool:
                raise ValueError(
                    "Phase-7 joint audit must expose evidence_gate.sufficient"
                )
            if not isinstance(conclusion_gate, dict) or type(
                conclusion_gate.get("replacement_formula_identified")
            ) is not bool:
                raise ValueError(
                    "Phase-7 joint audit must expose "
                    "conclusion_gate.replacement_formula_identified"
                )
            if not evidence_gate["sufficient"]:
                status = "insufficient_evidence"
            elif conclusion_gate["replacement_formula_identified"]:
                status = "formula_identified"
            else:
                status = "formula_not_identified"
            self._write_json(self.phase7_joint_audit_output, audit_result)
            speed_assessment = audit_result.get("speed_source_assessment")
            return {
                "status": status,
                "audit_schema_version": audit_result.get("schema_version"),
                "output": str(self.phase7_joint_audit_output),
                "kind": audit_result.get("kind"),
                "phase6_summary": str(phase6_summary_path),
                "phase7_summary": str(phase7_summary_path),
                "evidence_sufficient": evidence_gate["sufficient"],
                "evidence_reasons": evidence_gate.get("reasons", []),
                "replacement_formula_identified": conclusion_gate[
                    "replacement_formula_identified"
                ],
                "simulator_patch_allowed": conclusion_gate.get(
                    "simulator_patch_allowed"
                ),
                "speed_source_result": (
                    speed_assessment.get("result")
                    if isinstance(speed_assessment, dict)
                    else None
                ),
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase7_joint_audit_output),
                "phase6_summary": str(phase6_summary_path),
                "phase7_summary": str(phase7_summary_path),
            }

    def _select_phase7_summary(self, phase8_summary_path: Path) -> Path:
        if self.phase7_summary is not None:
            if not self.phase7_summary.is_file():
                raise FileNotFoundError(
                    f"specified Phase-7 summary is not a file: {self.phase7_summary}"
                )
            if not _is_completed_phase7_summary(self.phase7_summary):
                raise ValueError(
                    "specified Phase-7 summary does not contain a completed strict "
                    f"{PHASE7_TASK} run: {self.phase7_summary}"
                )
            return self.phase7_summary

        summary_directory = self.data_root / "calibration_summaries"
        if summary_directory.is_dir():
            candidates = sorted(
                (
                    path.resolve()
                    for path in summary_directory.glob("*.json")
                    if path.resolve() != phase8_summary_path.resolve()
                    and _is_completed_phase7_summary(path)
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                reverse=True,
            )
            if candidates:
                return candidates[0]
        raise FileNotFoundError(
            "no completed strict Phase-7 white-rage summary was found under "
            f"{summary_directory}"
        )

    def _run_phase8_joint_audit(
        self,
        phase6_summary_path: Path,
        phase7_summary_path: Path,
        phase8_summary_path: Path,
    ) -> dict[str, Any]:
        if self.phase8_joint_auditor is not None:
            return self.phase8_joint_auditor(
                phase6_summary_path, phase7_summary_path, phase8_summary_path
            )
        return run_phase8_joint_audit(
            phase6_summary_path, phase7_summary_path, phase8_summary_path
        )

    def _phase8_joint_audit_document(
        self, phase8_summary_path: Path
    ) -> dict[str, Any]:
        try:
            phase6_summary_path = self._select_phase6_summary(phase8_summary_path)
        except (OSError, ValueError) as error:
            return {
                "status": "phase6_summary_unavailable",
                "reason": str(error),
                "output": str(self.phase8_joint_audit_output),
            }
        try:
            phase7_summary_path = self._select_phase7_summary(phase8_summary_path)
        except (OSError, ValueError) as error:
            return {
                "status": "phase7_summary_unavailable",
                "reason": str(error),
                "output": str(self.phase8_joint_audit_output),
                "phase6_summary": str(phase6_summary_path),
            }

        try:
            audit_result = self._run_phase8_joint_audit(
                phase6_summary_path, phase7_summary_path, phase8_summary_path
            )
            if not isinstance(audit_result, dict):
                raise TypeError("Phase-8 joint auditor must return a JSON object")
            if audit_result.get("kind") != "white_rage_phase8_joint_formula_audit":
                raise ValueError(
                    "Phase-8 joint audit kind must be "
                    "white_rage_phase8_joint_formula_audit"
                )
            evidence_gate = audit_result.get("evidence_gate")
            conclusion_gate = audit_result.get("conclusion_gate")
            if not isinstance(evidence_gate, dict) or type(
                evidence_gate.get("sufficient")
            ) is not bool:
                raise ValueError(
                    "Phase-8 joint audit must expose evidence_gate.sufficient"
                )
            if not isinstance(conclusion_gate, dict) or type(
                conclusion_gate.get("replacement_formula_identified")
            ) is not bool:
                raise ValueError(
                    "Phase-8 joint audit must expose "
                    "conclusion_gate.replacement_formula_identified"
                )
            if not evidence_gate["sufficient"]:
                status = "insufficient_evidence"
            elif conclusion_gate["replacement_formula_identified"]:
                status = "formula_identified"
            else:
                status = "formula_not_identified"
            self._write_json(self.phase8_joint_audit_output, audit_result)
            return {
                "status": status,
                "output": str(self.phase8_joint_audit_output),
                "kind": audit_result.get("kind"),
                "phase6_summary": str(phase6_summary_path),
                "phase7_summary": str(phase7_summary_path),
                "phase8_summary": str(phase8_summary_path),
                "evidence_sufficient": evidence_gate["sufficient"],
                "evidence_reasons": evidence_gate.get("reasons", []),
                "replacement_formula_identified": conclusion_gate[
                    "replacement_formula_identified"
                ],
                "simulator_patch_allowed": conclusion_gate.get(
                    "simulator_patch_allowed"
                ),
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase8_joint_audit_output),
                "phase6_summary": str(phase6_summary_path),
                "phase7_summary": str(phase7_summary_path),
                "phase8_summary": str(phase8_summary_path),
            }

    def _supersede_phase8_joint_audit(
        self, reason: str, phase8_summary_path: Path | None = None
    ) -> dict[str, Any]:
        """Replace any stale Phase-8 success with an explicit fail-closed artifact."""

        audit_result = {
            "schema_version": 1,
            "kind": "white_rage_phase8_joint_formula_audit",
            "status": "insufficient_evidence",
            "superseded": True,
            "created_at": _utc_now(),
            "evidence_gate": {
                "sufficient": False,
                "reasons": ["phase8_summary_incomplete", reason],
            },
            "conclusion_gate": {
                "replacement_formula_identified": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
                "reason": "phase8_summary_incomplete",
            },
            "simulator_overrides": [],
            **(
                {"sources": {"phase8_calibration_summary": str(phase8_summary_path)}}
                if phase8_summary_path is not None
                else {}
            ),
        }
        self._write_json(self.phase8_joint_audit_output, audit_result)
        return {
            "status": "summary_incomplete",
            "reason": reason,
            "output": str(self.phase8_joint_audit_output),
            "superseded": True,
            "simulator_patch_allowed": False,
        }

    def _run_phase9_formula_audit(
        self, phase9_summary_path: Path
    ) -> dict[str, Any]:
        if self.phase9_formula_auditor is not None:
            return self.phase9_formula_auditor(phase9_summary_path)
        return run_phase9_formula_audit(phase9_summary_path)

    def _phase9_formula_audit_document(
        self, phase9_summary_path: Path
    ) -> dict[str, Any]:
        try:
            audit_result = self._run_phase9_formula_audit(phase9_summary_path)
            if not isinstance(audit_result, dict):
                raise TypeError("Phase-9 formula auditor must return a JSON object")
            if audit_result.get("kind") != "white_rage_phase9_formula_audit":
                raise ValueError(
                    "Phase-9 audit kind must be white_rage_phase9_formula_audit"
                )
            evidence_gate = audit_result.get("evidence_gate")
            conclusion_gate = audit_result.get("conclusion_gate")
            if not isinstance(evidence_gate, dict) or type(
                evidence_gate.get("sufficient")
            ) is not bool:
                raise ValueError(
                    "Phase-9 audit must expose evidence_gate.sufficient"
                )
            if (
                not isinstance(conclusion_gate, dict)
                or type(conclusion_gate.get("replacement_formula_identified"))
                is not bool
                or type(conclusion_gate.get("simulator_patch_allowed")) is not bool
            ):
                raise ValueError(
                    "Phase-9 audit must expose boolean replacement and patch gates"
                )
            identified = conclusion_gate["replacement_formula_identified"]
            patch_allowed = conclusion_gate["simulator_patch_allowed"]
            if patch_allowed != identified:
                raise ValueError(
                    "Phase-9 replacement and simulator patch gates must agree"
                )
            if not evidence_gate["sufficient"]:
                status = "insufficient_evidence"
            elif identified:
                status = "formula_identified"
            else:
                status = "formula_not_identified"
            if audit_result.get("status") != status:
                raise ValueError("Phase-9 audit status is inconsistent with its gates")
            self._write_json(self.phase9_formula_audit_output, audit_result)
            return {
                "status": status,
                "output": str(self.phase9_formula_audit_output),
                "kind": audit_result.get("kind"),
                "phase9_summary": str(phase9_summary_path),
                "evidence_sufficient": evidence_gate["sufficient"],
                "evidence_reasons": evidence_gate.get("reasons", []),
                "replacement_formula_identified": identified,
                "simulator_patch_allowed": patch_allowed,
                "selected_candidate_id": conclusion_gate.get(
                    "selected_candidate_id"
                ),
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase9_formula_audit_output),
                "phase9_summary": str(phase9_summary_path),
                "simulator_patch_allowed": False,
            }

    def _run_phase10_identification_audit(
        self, phase10_summary_path: Path
    ) -> dict[str, Any]:
        if self.phase10_identification_auditor is not None:
            return self.phase10_identification_auditor(phase10_summary_path)
        return run_phase10_identification_audit(phase10_summary_path)

    def _phase10_identification_audit_document(
        self, phase10_summary_path: Path
    ) -> dict[str, Any]:
        try:
            audit_result = self._run_phase10_identification_audit(
                phase10_summary_path
            )
            if not isinstance(audit_result, dict):
                raise TypeError(
                    "Phase-10 identification auditor must return a JSON object"
                )
            if audit_result.get("kind") != "white_rage_phase10_identification_audit":
                raise ValueError(
                    "Phase-10 audit kind must be "
                    "white_rage_phase10_identification_audit"
                )
            evidence_gate = audit_result.get("evidence_gate")
            conclusion_gate = audit_result.get("conclusion_gate")
            if not isinstance(evidence_gate, dict) or type(
                evidence_gate.get("sufficient")
            ) is not bool:
                raise ValueError(
                    "Phase-10 audit must expose evidence_gate.sufficient"
                )
            if (
                not isinstance(conclusion_gate, dict)
                or type(conclusion_gate.get("phase11_candidate_ready")) is not bool
                or conclusion_gate.get("simulator_patch_allowed") is not False
            ):
                raise ValueError(
                    "Phase-10 audit must expose a Phase-11 candidate gate and "
                    "must never authorize a simulator patch"
                )
            status = audit_result.get("status")
            if status not in {
                "insufficient_evidence",
                "identification_failed",
                "holdout_failed",
                "phase11_candidate_ready",
            }:
                raise ValueError("Phase-10 audit returned an unsupported status")
            candidate_ready = conclusion_gate["phase11_candidate_ready"]
            if candidate_ready != (status == "phase11_candidate_ready"):
                raise ValueError(
                    "Phase-10 status is inconsistent with its Phase-11 candidate gate"
                )
            self._write_json(self.phase10_identification_audit_output, audit_result)
            return {
                "status": status,
                "output": str(self.phase10_identification_audit_output),
                "kind": audit_result.get("kind"),
                "phase10_summary": str(phase10_summary_path),
                "evidence_sufficient": evidence_gate["sufficient"],
                "evidence_reasons": evidence_gate.get("reasons", []),
                "phase11_candidate_ready": candidate_ready,
                "phase11_candidate": conclusion_gate.get("phase11_candidate"),
                "simulator_patch_allowed": False,
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase10_identification_audit_output),
                "phase10_summary": str(phase10_summary_path),
                "phase11_candidate_ready": False,
                "simulator_patch_allowed": False,
            }

    def _run_phase11_external_holdout_audit(
        self, phase11_summary_path: Path
    ) -> dict[str, Any]:
        if self.phase11_external_holdout_auditor is not None:
            return self.phase11_external_holdout_auditor(phase11_summary_path)
        return run_phase11_external_holdout_audit(
            phase11_summary_path, self.phase10_identification_audit_output
        )

    def _phase11_external_holdout_audit_document(
        self, phase11_summary_path: Path
    ) -> dict[str, Any]:
        try:
            audit_result = self._run_phase11_external_holdout_audit(
                phase11_summary_path
            )
            if not isinstance(audit_result, dict):
                raise TypeError(
                    "Phase-11 external-holdout auditor must return a JSON object"
                )
            if audit_result.get("kind") != "white_rage_phase11_external_holdout_audit":
                raise ValueError(
                    "Phase-11 audit kind must be "
                    "white_rage_phase11_external_holdout_audit"
                )
            evidence_gate = audit_result.get("evidence_gate")
            conclusion_gate = audit_result.get("conclusion_gate")
            if not isinstance(evidence_gate, dict) or type(
                evidence_gate.get("sufficient")
            ) is not bool:
                raise ValueError(
                    "Phase-11 audit must expose evidence_gate.sufficient"
                )
            if (
                not isinstance(conclusion_gate, dict)
                or type(conclusion_gate.get("external_holdout_passed")) is not bool
                or type(conclusion_gate.get("candidate_external_validated")) is not bool
                or type(conclusion_gate.get("simulator_patch_allowed")) is not bool
                or conclusion_gate.get("unconditional_global_rage_go_patch_allowed")
                is not False
            ):
                raise ValueError(
                    "Phase-11 audit must expose its external holdout and scoped patch gates"
                )
            status = audit_result.get("status")
            if status not in {
                "insufficient_evidence",
                "holdout_failed",
                "external_holdout_validated",
            }:
                raise ValueError("Phase-11 audit returned an unsupported status")
            passed = conclusion_gate["external_holdout_passed"]
            validated = conclusion_gate["candidate_external_validated"]
            patch_allowed = conclusion_gate["simulator_patch_allowed"]
            expected = status == "external_holdout_validated"
            if passed != expected or validated != expected or patch_allowed != expected:
                raise ValueError(
                    "Phase-11 status is inconsistent with its external validation gates"
                )
            if expected and not isinstance(conclusion_gate.get("simulator_patch"), dict):
                raise ValueError(
                    "Validated Phase-11 audit must publish a scoped registry patch"
                )
            if not expected and conclusion_gate.get("simulator_patch") is not None:
                raise ValueError(
                    "Failed Phase-11 audit must not publish a simulator patch"
                )
            self._write_json(self.phase11_external_holdout_audit_output, audit_result)
            return {
                "status": status,
                "output": str(self.phase11_external_holdout_audit_output),
                "kind": audit_result.get("kind"),
                "phase11_summary": str(phase11_summary_path),
                "evidence_sufficient": evidence_gate["sufficient"],
                "evidence_reasons": evidence_gate.get("reasons", []),
                "external_holdout_passed": passed,
                "candidate_external_validated": validated,
                "simulator_patch_allowed": patch_allowed,
                "simulator_patch": conclusion_gate.get("simulator_patch"),
                "unconditional_global_rage_go_patch_allowed": False,
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase11_external_holdout_audit_output),
                "phase11_summary": str(phase11_summary_path),
                "external_holdout_passed": False,
                "candidate_external_validated": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
                "unconditional_global_rage_go_patch_allowed": False,
            }

    def _run_phase12_audit(self, summary_path: Path) -> dict[str, Any]:
        if self.phase12_auditor is not None:
            return self.phase12_auditor(summary_path)
        return run_phase12_audit(
            summary_path,
            self.phase12_preregistration,
            history_directory=self.data_root / "calibration_summaries",
        )

    def _phase12_audit_document(self, summary_path: Path) -> dict[str, Any]:
        try:
            audit_result = self._run_phase12_audit(summary_path)
            if not isinstance(audit_result, dict):
                raise TypeError("Phase-12 auditor must return a JSON object")
            if audit_result.get("kind") != "fury_current_build_phase12_audit":
                raise ValueError(
                    "Phase-12 audit kind must be fury_current_build_phase12_audit"
                )
            status = audit_result.get("status")
            if status not in {"partial", "complete", "complete_with_deferred"}:
                raise ValueError("Phase-12 audit returned an unsupported status")
            gates = audit_result.get("mechanism_gates")
            external_boundaries = audit_result.get(
                "external_mechanism_boundaries"
            )
            conclusion = audit_result.get("conclusion_gate")
            if not isinstance(gates, dict) or set(gates) != set(
                MECHANISM_GATE_NAMES
            ):
                raise ValueError("Phase-12 audit mechanism gates are incomplete")
            if (
                not isinstance(external_boundaries, dict)
                or tuple(external_boundaries) != EXTERNAL_MECHANISM_BOUNDARY_NAMES
                or any(
                    not isinstance(boundary, dict)
                    or boundary.get("collection_complete_contribution") is not False
                    or boundary.get("simulator_patch_allowed") is not False
                    for boundary in external_boundaries.values()
                )
            ):
                raise ValueError(
                    "Phase-12 external boundaries must remain outside collection and patch gates"
                )
            if (
                not isinstance(conclusion, dict)
                or conclusion.get("replacement_formula_identified") is not False
                or conclusion.get("holdout_refit_permitted") is not False
                or conclusion.get("simulator_patch_allowed") is not False
                or conclusion.get("simulator_patch") is not None
            ):
                raise ValueError(
                    "Phase-12 collection audit must never fit or authorize a patch"
                )
            gate_statuses = {
                name: gate.get("status")
                for name, gate in gates.items()
                if isinstance(gate, dict)
            }
            if len(gate_statuses) != len(gates):
                raise ValueError("Phase-12 audit has a malformed mechanism gate")
            if status == "complete":
                if (
                    any(value not in {"COMPLETE", "REUSED"} for value in gate_statuses.values())
                    or conclusion.get("collection_complete") is not True
                    or conclusion.get("fully_observed") is not True
                ):
                    raise ValueError(
                        "Phase-12 complete status requires fully observed gates"
                    )
            elif status == "complete_with_deferred":
                if (
                    "DEFERRED" not in gate_statuses.values()
                    or any(
                        value not in {"COMPLETE", "REUSED", "DEFERRED"}
                        for value in gate_statuses.values()
                    )
                    or conclusion.get("collection_complete") is not False
                    or conclusion.get("fully_observed") is not False
                ):
                    raise ValueError(
                        "Phase-12 deferred status cannot be reported as calibrated collection"
                    )
            elif (
                conclusion.get("collection_complete") is not False
                or conclusion.get("fully_observed") is not False
            ):
                raise ValueError(
                    "Phase-12 partial status cannot be reported as complete"
                )
            self._write_json(self.phase12_audit_output, audit_result)
            return {
                "status": status,
                "output": str(self.phase12_audit_output),
                "kind": audit_result.get("kind"),
                "phase12_summary": str(summary_path),
                "campaign_run_id": audit_result.get("campaign_run_id"),
                "mechanism_gate_statuses": gate_statuses,
                "external_boundary_statuses": {
                    name: boundary.get("status")
                    for name, boundary in external_boundaries.items()
                },
                "collection_complete": conclusion.get("collection_complete"),
                "fully_observed": conclusion.get("fully_observed"),
                "replacement_formula_identified": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
            }
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            return {
                "status": "audit_error",
                "error": str(error),
                "output": str(self.phase12_audit_output),
                "phase12_summary": str(summary_path),
                "collection_complete": False,
                "fully_observed": False,
                "replacement_formula_identified": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
            }

    def _write_json(self, path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _log(self, event: str, **fields: Any) -> None:
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        record = {"time": _utc_now(), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")

    def _write_status(self, status: str, message: str, **fields: Any) -> dict[str, Any]:
        if self._shadow_pairs_status is not None and "shadow_pairs" not in fields:
            fields["shadow_pairs"] = self._shadow_pairs_status
        if (
            self._shadow_acceptance_status is not None
            and "shadow_acceptance" not in fields
        ):
            fields["shadow_acceptance"] = self._shadow_acceptance_status
        if (
            self._shadow_transition_status is not None
            and "shadow_transition_dataset" not in fields
        ):
            fields["shadow_transition_dataset"] = self._shadow_transition_status
        document = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "message": message,
            "source": str(self.source),
            "updated_at": _utc_now(),
            **fields,
        }
        self._write_json(self.status_path, document)
        return document

    def _poll_timer_live_debug(self) -> dict[str, Any]:
        """Poll optional live diagnostics without affecting the main watcher."""

        try:
            result = self.timer_live_debug.poll()
            if not isinstance(result, dict):
                raise TypeError("timer live debug poll must return a mapping")
            return result
        except Exception as error:
            try:
                self._log("timer_live_debug_poll_error", error=str(error))
            except OSError:
                pass
            try:
                result = self.timer_live_debug.write_error_status(error)
                if isinstance(result, dict):
                    return result
            except Exception as status_error:
                try:
                    self._log(
                        "timer_live_debug_status_error",
                        error=str(status_error),
                        poll_error=str(error),
                    )
                except OSError:
                    pass
            return {
                "status": "poll_error",
                "error": str(error),
                "message": (
                    "Timer live diagnostics failed on this poll; the main "
                    "calibration watcher continues."
                ),
            }

    def _remember_shadow_pairs_status(self, document: dict[str, Any]) -> None:
        """Keep the most informative pair status across direct and SV imports."""

        current = self._shadow_pairs_status
        if current is None:
            self._shadow_pairs_status = document
            return
        current_rank = (
            int(current.get("journal_pair_total", 0) or 0),
            int(current.get("source_pair_total", 0) or 0),
        )
        document_rank = (
            int(document.get("journal_pair_total", 0) or 0),
            int(document.get("source_pair_total", 0) or 0),
        )
        if document_rank > current_rank or (
            document_rank == current_rank
            and (
                document.get("transport") == current.get("transport")
                or (
                    document.get("transport") == "nampower_customdata_jsonl"
                    and current.get("transport") is None
                )
            )
        ):
            self._shadow_pairs_status = document

    def _validate_shadow_transition_receipt(
        self,
        transition: dict[str, Any],
        *,
        export_session_id: str,
        expected_pairs: int,
    ) -> dict[str, Any]:
        if transition.get("status") != "ok":
            raise ValueError("Shadow transition receipt status must equal ok")
        if transition.get("export_session_id") != export_session_id:
            raise ValueError("Shadow transition receipt session mismatch")
        if transition.get("row_count") != expected_pairs:
            raise ValueError("Shadow transition receipt row count mismatch")
        expected_paths = {
            "dataset": self.shadow_transition_output,
            "manifest": self.shadow_transition_manifest,
            "report": self.shadow_transition_report,
        }
        for field, expected_path in expected_paths.items():
            supplied = transition.get(field)
            if not isinstance(supplied, str) or Path(supplied).resolve() != expected_path:
                raise ValueError(f"Shadow transition receipt {field} path mismatch")
        digest = transition.get("dataset_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Shadow transition receipt lacks a lowercase SHA-256")
        closed_flags = (
            "scalar_reward_available",
            "recorded_active_policy_counterfactual_reward_available",
            "candidate_counterfactual_reward_available",
            "full_next_state_available",
            "td_transition_eligible",
            "offline_rl_episode_eligible",
            "deployment_allowed",
        )
        for field in closed_flags:
            if transition.get(field) is not False:
                raise ValueError(
                    f"Shadow transition receipt must explicitly keep {field}=false"
                )
        return dict(transition)

    def _audit_completed_shadow_live_export(
        self, result: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            expected_pairs = int(result.get("latest_target", 0) or 0)
            if expected_pairs <= 0:
                raise ShadowPairAcceptanceError(
                    "completed Shadow export has no positive latest_target"
                )
            export_session_id = result.get("latest_export_session_id")
            if not isinstance(export_session_id, str) or not export_session_id.strip():
                raise ShadowPairAcceptanceError(
                    "completed Shadow export has no latest_export_session_id"
                )
            report, output = audit_shadow_pairs(
                result.get("output", ""),
                result.get("manifest", ""),
                output_path=self.shadow_acceptance_output,
                expected_pairs=expected_pairs,
                export_session_id=export_session_id,
            )
            summary = {
                "status": report.get("status"),
                "output": str(output),
                "export_session_id": report.get("input", {}).get(
                    "selected_export_session_id"
                ),
                "transport_action_gate": report.get(
                    "transport_action_gate", {}
                ).get("status"),
                "causal_state_action_gate": report.get(
                    "causal_state_action_gate", {}
                ).get("status"),
                "outcome_reward_gate": report.get(
                    "outcome_reward_gate", {}
                ).get("status"),
                "deployment_allowed": report.get("deployment_allowed") is True,
            }
            self._shadow_acceptance_status = summary
            self._log("shadow_pair_acceptance_completed", **summary)
            if (
                report.get("status") == "PASS"
                and report.get("deployment_allowed") is False
                and all(
                    report.get(gate_name, {}).get("status") == "PASS"
                    and report.get(gate_name, {}).get("passed") is True
                    for gate_name in (
                        "transport_action_gate",
                        "causal_state_action_gate",
                        "outcome_reward_gate",
                    )
                )
            ):
                try:
                    transition = self.shadow_transition_builder(
                        result.get("output", ""),
                        result.get("manifest", ""),
                        output,
                        session_id=export_session_id,
                        output_path=self.shadow_transition_output,
                        output_manifest_path=self.shadow_transition_manifest,
                        report_path=self.shadow_transition_report,
                    )
                    if not isinstance(transition, dict):
                        raise TypeError(
                            "Shadow transition builder must return a mapping"
                        )
                    self._shadow_transition_status = (
                        self._validate_shadow_transition_receipt(
                            transition,
                            export_session_id=export_session_id,
                            expected_pairs=expected_pairs,
                        )
                    )
                    self._shadow_transition_retry_after = 0.0
                    self._log(
                        "shadow_transition_dataset_materialized",
                        **self._shadow_transition_status,
                    )
                except Exception as error:
                    self._shadow_transition_retry_after = time.monotonic() + 30.0
                    self._shadow_transition_status = {
                        "status": "materialize_error",
                        "export_session_id": export_session_id,
                        "row_count": expected_pairs,
                        "dataset": str(self.shadow_transition_output),
                        "manifest": str(self.shadow_transition_manifest),
                        "report": str(self.shadow_transition_report),
                        "error": str(error),
                        "scalar_reward_available": False,
                        "recorded_active_policy_counterfactual_reward_available": False,
                        "candidate_counterfactual_reward_available": False,
                        "full_next_state_available": False,
                        "td_transition_eligible": False,
                        "offline_rl_episode_eligible": False,
                        "deployment_allowed": False,
                    }
                    self._log(
                        "shadow_transition_dataset_error",
                        **self._shadow_transition_status,
                    )
            else:
                self._shadow_transition_status = {
                    "status": "blocked_by_acceptance",
                    "export_session_id": export_session_id,
                    "row_count": expected_pairs,
                    "acceptance_status": report.get("status"),
                    "scalar_reward_available": False,
                    "recorded_active_policy_counterfactual_reward_available": False,
                    "candidate_counterfactual_reward_available": False,
                    "full_next_state_available": False,
                    "td_transition_eligible": False,
                    "offline_rl_episode_eligible": False,
                    "deployment_allowed": False,
                }
            return summary
        except Exception as error:
            summary = {
                "status": "audit_error",
                "output": str(self.shadow_acceptance_output),
                "error": str(error),
                "deployment_allowed": False,
            }
            self._shadow_acceptance_status = summary
            self._shadow_transition_status = {
                "status": "blocked_by_acceptance_error",
                "export_session_id": result.get("latest_export_session_id"),
                "error": str(error),
                "scalar_reward_available": False,
                "recorded_active_policy_counterfactual_reward_available": False,
                "candidate_counterfactual_reward_available": False,
                "full_next_state_available": False,
                "td_transition_eligible": False,
                "offline_rl_episode_eligible": False,
                "deployment_allowed": False,
            }
            self._log("shadow_pair_acceptance_error", **summary)
            return summary

    def _poll_shadow_live_export(self) -> dict[str, Any]:
        """Import new Nampower CustomData pairs independently of SV writes."""

        try:
            result = self.shadow_live_export.poll()
            if not isinstance(result, dict):
                raise TypeError("Shadow live export poll must return a mapping")
        except Exception as error:
            result = {
                "status": "poll_error",
                "transport": "nampower_customdata_jsonl",
                "changed": False,
                "error": str(error),
            }
            try:
                self._log("shadow_pairs_live_poll_error", error=str(error))
            except OSError:
                pass
            return result

        if "journal_pair_total" in result:
            self._remember_shadow_pairs_status(result)
        if (
            not bool(result.get("changed"))
            and (
                self._shadow_acceptance_status is None
                or (
                    isinstance(self._shadow_transition_status, dict)
                    and self._shadow_transition_status.get("status")
                    == "materialize_error"
                    and time.monotonic() >= self._shadow_transition_retry_after
                )
            )
            and int(result.get("invalid_line_count", 0) or 0) == 0
            and result.get("completed") is True
        ):
            acceptance = self._audit_completed_shadow_live_export(result)
            self._write_status(
                "shadow_acceptance_refreshed",
                "Rebuilt the acceptance report for an already imported completed Shadow session.",
                pid=os.getpid(),
                shadow_live_export=result,
                shadow_acceptance=acceptance,
            )
        if result.get("changed"):
            self._log("shadow_pairs_live_observed", **result)
            new_pair_count = int(result.get("new_pair_count", 0) or 0)
            invalid_line_count = int(result.get("invalid_line_count", 0) or 0)
            acceptance = None
            if (
                invalid_line_count == 0
                and result.get("completed") is True
            ):
                acceptance = self._audit_completed_shadow_live_export(result)
            if invalid_line_count > 0:
                self._write_status(
                    "shadow_export_partial",
                    "The Shadow CustomData export contains invalid rows; valid rows were retained.",
                    pid=os.getpid(),
                    shadow_live_export=result,
                )
            elif new_pair_count > 0:
                self._write_status(
                    "shadow_pairs_imported_live",
                    "Imported event-confirmed Shadow pairs from Nampower CustomData.",
                    pid=os.getpid(),
                    shadow_live_export=result,
                    **(
                        {"shadow_acceptance": acceptance}
                        if acceptance is not None
                        else {}
                    ),
                )
            elif acceptance is not None:
                self._write_status(
                    "shadow_acceptance_refreshed",
                    "Rebuilt the acceptance report for an already imported completed Shadow session.",
                    pid=os.getpid(),
                    shadow_live_export=result,
                    shadow_acceptance=acceptance,
                )
            elif int(result.get("session_start_count", 0) or 0) > 0:
                self._write_status(
                    "shadow_export_ready",
                    "Observed the addon Shadow export handshake in Nampower CustomData.",
                    pid=os.getpid(),
                    shadow_live_export=result,
                )
        return result

    def _finalize_timer_live_debug(
        self,
        campaign_run_ids: Sequence[str],
        *,
        terminal_records: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for campaign_run_id in campaign_run_ids:
            try:
                terminal_record = (
                    terminal_records.get(campaign_run_id)
                    if terminal_records is not None
                    else None
                )
                if terminal_record is None:
                    result = self.timer_live_debug.finalize(campaign_run_id)
                else:
                    result = self.timer_live_debug.finalize(
                        campaign_run_id,
                        terminal_record=terminal_record,
                    )
                if not isinstance(result, dict):
                    raise TypeError("timer live debug finalize must return a mapping")
                results[campaign_run_id] = result
            except Exception as error:
                try:
                    self._log(
                        "timer_live_debug_finalize_error",
                        campaign_run_id=campaign_run_id,
                        error=str(error),
                    )
                except OSError:
                    pass
                results[campaign_run_id] = {
                    "status": "finalize_error",
                    "campaign_run_id": campaign_run_id,
                    "error": str(error),
                }
        return results

    def _attach_timer_debug_bundle(
        self,
        timer_summary: dict[str, Any],
        live_debug: dict[str, Any],
    ) -> None:
        timer_summary["live_debug_status"] = live_debug.get("status")
        if live_debug.get("bundle"):
            timer_summary["debug_bundle"] = live_debug["bundle"]
        if live_debug.get("manifest"):
            timer_summary["debug_manifest"] = live_debug["manifest"]
        recovery_composite = live_debug.get("recovery_composite")
        if isinstance(recovery_composite, dict):
            timer_summary["recovery_composite"] = recovery_composite
            if timer_summary.get("status") == "awaiting_recovery_composite":
                composite_status = recovery_composite.get("status")
                timer_summary["status"] = (
                    "complete"
                    if composite_status == "complete"
                    else "incomplete_evidence"
                )
                timer_summary["kind"] = "fury_timer_source_recovery_composite"
                timer_summary["evidence_complete"] = composite_status == "complete"
                timer_summary["blocker_count"] = sum(
                    1
                    for passed in recovery_composite.get("checks", {}).values()
                    if passed is not True
                ) if isinstance(recovery_composite.get("checks"), dict) else 1
                if isinstance(recovery_composite.get("output"), str):
                    timer_summary["output"] = recovery_composite["output"]
        output = timer_summary.get("output")
        if not isinstance(output, str) or not output:
            return
        output_path = Path(output)
        if not output_path.is_file():
            return
        try:
            document = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(document, dict):
            return
        document["live_debug_status"] = live_debug.get("status")
        if live_debug.get("bundle"):
            document["debug_bundle"] = live_debug["bundle"]
        if live_debug.get("manifest"):
            document["debug_manifest"] = live_debug["manifest"]
        try:
            self._write_json(output_path, document)
        except OSError as error:
            timer_summary["live_debug_summary_attachment_error"] = str(error)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "processed_campaigns": []}
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CalibrationWatchError(f"cannot read watcher state {self.state_path}: {error}") from error
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise CalibrationWatchError(f"unsupported watcher state in {self.state_path}")
        campaigns = document.get("processed_campaigns")
        if not isinstance(campaigns, list):
            raise CalibrationWatchError(f"watcher state has no processed_campaigns list: {self.state_path}")
        return document

    def process_once(self) -> dict[str, Any]:
        """Inspect the current stable file and import all newly completed campaign IDs once."""

        self._poll_timer_live_debug()
        self._poll_shadow_live_export()
        signature, completions, decisions = _inspect_savedvariables(self.source)
        try:
            shadow_result = self.shadow_pair_publisher(
                decisions,
                source_lua=self.source,
                data_root=self.data_root,
                source_signature=signature.as_dict(),
            )
            shadow_document = shadow_result.as_dict()
        except SavedVariablesImportError as error:
            raise CalibrationWatchError(
                f"automatic Shadow pair import failed: {error}"
            ) from error
        self._remember_shadow_pairs_status(shadow_document)
        if shadow_document.get("new_pair_count", 0):
            self._log("shadow_pairs_imported", **shadow_document)
        state = self._load_state()
        source_text = str(self.source)
        processed = {
            item.get("campaign_run_id")
            for item in state["processed_campaigns"]
            if isinstance(item, dict) and item.get("source") == source_text
        }
        new_completions = [
            completion
            for completion in completions
            if completion.campaign_run_id not in processed
        ]

        signature_document = signature.as_dict()
        if not completions:
            latest_processed = next(
                (
                    item
                    for item in reversed(state["processed_campaigns"])
                    if isinstance(item, dict) and item.get("source") == source_text
                ),
                None,
            )
            if latest_processed is not None:
                retained_fields = {
                    key: latest_processed[key]
                    for key in (
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
                    if isinstance(latest_processed.get(key), dict)
                }
                terminal_event = latest_processed.get("terminal_event")
                terminal_events = (
                    [terminal_event] if isinstance(terminal_event, str) else []
                )
                return self._write_status(
                    "campaign_history_retained_shadow_processed",
                    "The current SavedVariables calibration ring has no terminal "
                    "marker after compaction; the latest processed campaign receipt "
                    "was retained and the current Shadow pairs were imported or "
                    "deduplicated.",
                    source_signature=signature_document,
                    campaign_run_ids=[latest_processed.get("campaign_run_id")],
                    campaign_terminal_events=terminal_events,
                    **retained_fields,
                )
            shadow_message = (
                f" Imported {shadow_document['new_pair_count']} new Shadow pair(s)."
                if shadow_document.get("new_pair_count", 0)
                else ""
            )
            return self._write_status(
                "waiting_for_campaign_completion",
                "SavedVariables is readable; no terminal calibration campaign is on disk yet."
                + shadow_message,
                source_signature=signature_document,
            )
        if not new_completions:
            latest_processed = next(
                (
                    item
                    for item in reversed(state["processed_campaigns"])
                    if isinstance(item, dict)
                    and item.get("source") == source_text
                    and item.get("campaign_run_id")
                    in {completion.campaign_run_id for completion in completions}
                ),
                None,
            )
            previous_profile = (
                latest_processed.get("profile")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("profile"), dict)
                else None
            )
            previous_replay = (
                latest_processed.get("phase5_replay")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase5_replay"), dict)
                else None
            )
            previous_phase6_audit = (
                latest_processed.get("phase6_rage_audit")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase6_rage_audit"), dict)
                else None
            )
            previous_phase7_audit = (
                latest_processed.get("phase7_joint_audit")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase7_joint_audit"), dict)
                else None
            )
            previous_phase8_audit = (
                latest_processed.get("phase8_joint_audit")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase8_joint_audit"), dict)
                else None
            )
            previous_phase9_audit = (
                latest_processed.get("phase9_formula_audit")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase9_formula_audit"), dict)
                else None
            )
            previous_phase10_audit = (
                latest_processed.get("phase10_identification_audit")
                if isinstance(latest_processed, dict)
                and isinstance(
                    latest_processed.get("phase10_identification_audit"), dict
                )
                else None
            )
            previous_phase11_audit = (
                latest_processed.get("phase11_external_holdout_audit")
                if isinstance(latest_processed, dict)
                and isinstance(
                    latest_processed.get("phase11_external_holdout_audit"), dict
                )
                else None
            )
            previous_phase12_audit = (
                latest_processed.get("phase12_audit")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("phase12_audit"), dict)
                else None
            )
            previous_timer_summary = (
                latest_processed.get("timer_summary")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("timer_summary"), dict)
                else None
            )
            previous_timer_live_debug = (
                latest_processed.get("timer_live_debug")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("timer_live_debug"), dict)
                else None
            )
            matching_timer_completion = next(
                (
                    completion
                    for completion in reversed(completions)
                    if completion.campaign_id == TIMER_CAMPAIGN_ID
                    and isinstance(latest_processed, dict)
                    and completion.campaign_run_id
                    == latest_processed.get("campaign_run_id")
                ),
                None,
            )
            if matching_timer_completion is not None:
                refreshed_live_debug = self._finalize_timer_live_debug(
                    [matching_timer_completion.campaign_run_id]
                )[matching_timer_completion.campaign_run_id]
                previous_timer_live_debug = refreshed_live_debug
                latest_processed["timer_live_debug"] = refreshed_live_debug
                if previous_timer_summary is not None:
                    self._attach_timer_debug_bundle(
                        previous_timer_summary, refreshed_live_debug
                    )
                    latest_processed["timer_summary"] = previous_timer_summary
                state["updated_at"] = _utc_now()
                self._write_json(self.state_path, state)
            stored_summary = (
                latest_processed.get("summary")
                if isinstance(latest_processed, dict)
                and isinstance(latest_processed.get("summary"), dict)
                else None
            )
            stored_summary_output = (
                stored_summary.get("output")
                if isinstance(stored_summary, dict)
                else None
            )
            phase9_incomplete_reason = _summary_phase9_incomplete_reason(
                stored_summary_output
            )
            refreshed_phase9_audit = None
            if phase9_incomplete_reason is not None:
                refreshed_phase9_audit = {
                    "status": "summary_incomplete",
                    "reason": phase9_incomplete_reason,
                    "output": str(self.phase9_formula_audit_output),
                    "simulator_patch_allowed": False,
                }
            elif _summary_contains_phase9_rage(stored_summary_output) and (
                not isinstance(previous_phase9_audit, dict)
                or previous_phase9_audit.get("status")
                in {"not_applicable", "summary_incomplete", "audit_error"}
            ):
                refreshed_phase9_audit = self._phase9_formula_audit_document(
                    Path(stored_summary_output)
                )
            if (
                refreshed_phase9_audit is not None
                and refreshed_phase9_audit != previous_phase9_audit
            ):
                latest_processed["phase9_formula_audit"] = refreshed_phase9_audit
                try:
                    corrected_summary = json.loads(
                        Path(stored_summary_output).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    corrected_summary = None
                if isinstance(corrected_summary, dict):
                    specialized_runs = corrected_summary.get("specialized_runs")
                    deferred = corrected_summary.get("deferred_analysis")
                    if isinstance(specialized_runs, list):
                        stored_summary["specialized_run_count"] = len(specialized_runs)
                    if isinstance(deferred, list):
                        stored_summary["deferred_analysis_count"] = len(deferred)
                state["updated_at"] = _utc_now()
                self._write_json(self.state_path, state)
                refreshed_status = refreshed_phase9_audit.get("status")
                status = f"campaign_reconciled_phase9_{refreshed_status}"
                message = (
                    "The already imported Phase-9 campaign was reconciled and its "
                    "preregistered white-rage formula audit was refreshed."
                )
                fields = {
                    "source_signature": signature_document,
                    "campaign_run_ids": [latest_processed.get("campaign_run_id")],
                    "summary": stored_summary,
                    "phase9_formula_audit": refreshed_phase9_audit,
                    **({"profile": previous_profile} if previous_profile is not None else {}),
                    **({"phase5_replay": previous_replay} if previous_replay is not None else {}),
                    **({"phase6_rage_audit": previous_phase6_audit} if previous_phase6_audit is not None else {}),
                    **({"phase7_joint_audit": previous_phase7_audit} if previous_phase7_audit is not None else {}),
                    **({"phase8_joint_audit": previous_phase8_audit} if previous_phase8_audit is not None else {}),
                }
                self._log(status, **fields)
                return self._write_status(status, message, **fields)
            refreshed_phase8_audit = None
            phase8_incomplete_reason = _summary_phase8_incomplete_reason(
                stored_summary_output
            )
            if phase8_incomplete_reason is not None:
                phase8_incomplete_metadata_current = (
                    isinstance(previous_phase8_audit, dict)
                    and previous_phase8_audit.get("status") == "summary_incomplete"
                    and previous_phase8_audit.get("reason")
                    == phase8_incomplete_reason
                    and previous_phase8_audit.get("output")
                    == str(self.phase8_joint_audit_output)
                    and previous_phase8_audit.get("superseded") is True
                    and previous_phase8_audit.get("simulator_patch_allowed") is False
                )
                if (
                    not phase8_incomplete_metadata_current
                    or not self.phase8_joint_audit_output.is_file()
                ):
                    refreshed_phase8_audit = self._supersede_phase8_joint_audit(
                        phase8_incomplete_reason,
                        Path(stored_summary_output),
                    )
            elif isinstance(previous_phase8_audit, dict):
                if (
                    previous_phase8_audit.get("status")
                    in {
                        "not_applicable",
                        "summary_incomplete",
                        "phase6_summary_unavailable",
                        "phase7_summary_unavailable",
                    }
                    and _summary_contains_phase8_rage(stored_summary_output)
                ):
                    refreshed_phase8_audit = self._phase8_joint_audit_document(
                        Path(stored_summary_output)
                    )
            if refreshed_phase8_audit is not None:
                if refreshed_phase8_audit != previous_phase8_audit:
                    latest_processed["phase8_joint_audit"] = refreshed_phase8_audit
                    try:
                        corrected_summary = json.loads(
                            Path(stored_summary_output).read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        corrected_summary = None
                    if isinstance(corrected_summary, dict):
                        specialized_runs = corrected_summary.get("specialized_runs")
                        deferred = corrected_summary.get("deferred_analysis")
                        if isinstance(specialized_runs, list):
                            stored_summary["specialized_run_count"] = len(
                                specialized_runs
                            )
                        if isinstance(deferred, list):
                            stored_summary["deferred_analysis_count"] = len(deferred)
                    state["updated_at"] = _utc_now()
                    self._write_json(self.state_path, state)
                    refreshed_status = refreshed_phase8_audit.get("status")
                    status = f"campaign_reconciled_phase8_{refreshed_status}"
                    message = (
                        "The already imported Phase-8 campaign was reconciled and "
                        "its three-phase white-rage formula audit was refreshed."
                    )
                    fields = {
                        "source_signature": signature_document,
                        "campaign_run_ids": [latest_processed.get("campaign_run_id")],
                        "summary": stored_summary,
                        "phase8_joint_audit": refreshed_phase8_audit,
                        **(
                            {"profile": previous_profile}
                            if previous_profile is not None
                            else {}
                        ),
                        **(
                            {"phase5_replay": previous_replay}
                            if previous_replay is not None
                            else {}
                        ),
                        **(
                            {"phase6_rage_audit": previous_phase6_audit}
                            if previous_phase6_audit is not None
                            else {}
                        ),
                        **(
                            {"phase7_joint_audit": previous_phase7_audit}
                            if previous_phase7_audit is not None
                            else {}
                        ),
                    }
                    self._log(status, **fields)
                    return self._write_status(status, message, **fields)
            if (
                isinstance(previous_phase7_audit, dict)
                and (
                    previous_phase7_audit.get("status")
                    in {
                        "not_applicable",
                        "summary_incomplete",
                        "phase6_summary_unavailable",
                    }
                    or previous_phase7_audit.get("audit_schema_version")
                    != PHASE7_AUDIT_SCHEMA_VERSION
                )
                and _summary_contains_phase7_rage(stored_summary_output)
            ):
                refreshed_phase7_audit = self._phase7_joint_audit_document(
                    Path(stored_summary_output)
                )
                if refreshed_phase7_audit != previous_phase7_audit:
                    latest_processed["phase7_joint_audit"] = refreshed_phase7_audit
                    try:
                        corrected_summary = json.loads(
                            Path(stored_summary_output).read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        corrected_summary = None
                    if isinstance(corrected_summary, dict):
                        specialized_runs = corrected_summary.get("specialized_runs")
                        deferred = corrected_summary.get("deferred_analysis")
                        if isinstance(specialized_runs, list):
                            stored_summary["specialized_run_count"] = len(
                                specialized_runs
                            )
                        if isinstance(deferred, list):
                            stored_summary["deferred_analysis_count"] = len(deferred)
                    state["updated_at"] = _utc_now()
                    self._write_json(self.state_path, state)
                    refreshed_status = refreshed_phase7_audit.get("status")
                    status = f"campaign_reconciled_phase7_{refreshed_status}"
                    message = (
                        "The already imported Phase-7 campaign was reconciled and "
                        "its joint white-rage formula audit was refreshed."
                    )
                    fields = {
                        "source_signature": signature_document,
                        "campaign_run_ids": [latest_processed.get("campaign_run_id")],
                        "summary": stored_summary,
                        "phase7_joint_audit": refreshed_phase7_audit,
                        **(
                            {"profile": previous_profile}
                            if previous_profile is not None
                            else {}
                        ),
                        **(
                            {"phase5_replay": previous_replay}
                            if previous_replay is not None
                            else {}
                        ),
                        **(
                            {"phase6_rage_audit": previous_phase6_audit}
                            if previous_phase6_audit is not None
                            else {}
                        ),
                    }
                    self._log(status, **fields)
                    return self._write_status(status, message, **fields)
            if (
                isinstance(previous_phase6_audit, dict)
                and previous_phase6_audit.get("status")
                in {"not_applicable", "summary_incomplete"}
                and _summary_contains_phase6_rage(stored_summary_output)
            ):
                refreshed_phase6_audit = self._phase6_audit_document(
                    Path(stored_summary_output)
                )
                latest_processed["phase6_rage_audit"] = refreshed_phase6_audit
                try:
                    corrected_summary = json.loads(
                        Path(stored_summary_output).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    corrected_summary = None
                if isinstance(corrected_summary, dict):
                    specialized_runs = corrected_summary.get("specialized_runs")
                    deferred = corrected_summary.get("deferred_analysis")
                    if isinstance(specialized_runs, list):
                        stored_summary["specialized_run_count"] = len(specialized_runs)
                    if isinstance(deferred, list):
                        stored_summary["deferred_analysis_count"] = len(deferred)
                state["updated_at"] = _utc_now()
                self._write_json(self.state_path, state)
                refreshed_status = refreshed_phase6_audit.get("status")
                status = f"campaign_reconciled_phase6_rage_{refreshed_status}"
                message = (
                    "The already imported Phase-6 campaign was reconciled from its "
                    "corrected strict summary and the rage-formula audit was refreshed."
                )
                fields = {
                    "source_signature": signature_document,
                    "campaign_run_ids": [latest_processed.get("campaign_run_id")],
                    "summary": stored_summary,
                    "phase6_rage_audit": refreshed_phase6_audit,
                    **({"profile": previous_profile} if previous_profile is not None else {}),
                    **(
                        {"phase5_replay": previous_replay}
                        if previous_replay is not None
                        else {}
                    ),
                }
                self._log(status, **fields)
                return self._write_status(status, message, **fields)
            return self._write_status(
                "campaign_already_imported",
                "The terminal campaign in this SavedVariables write was already imported.",
                source_signature=signature_document,
                campaign_run_ids=[item.campaign_run_id for item in completions],
                **({"profile": previous_profile} if previous_profile is not None else {}),
                **(
                    {"phase5_replay": previous_replay}
                    if previous_replay is not None
                    else {}
                ),
                **(
                    {"phase6_rage_audit": previous_phase6_audit}
                    if previous_phase6_audit is not None
                    else {}
                ),
                **(
                    {"phase7_joint_audit": previous_phase7_audit}
                    if previous_phase7_audit is not None
                    else {}
                ),
                **(
                    {"phase8_joint_audit": previous_phase8_audit}
                    if previous_phase8_audit is not None
                    else {}
                ),
                **(
                    {"phase9_formula_audit": previous_phase9_audit}
                    if previous_phase9_audit is not None
                    else {}
                ),
                **(
                    {"phase10_identification_audit": previous_phase10_audit}
                    if previous_phase10_audit is not None
                    else {}
                ),
                **(
                    {"phase11_external_holdout_audit": previous_phase11_audit}
                    if previous_phase11_audit is not None
                    else {}
                ),
                **(
                    {"phase12_audit": previous_phase12_audit}
                    if previous_phase12_audit is not None
                    else {}
                ),
                **(
                    {"timer_summary": previous_timer_summary}
                    if previous_timer_summary is not None
                    else {}
                ),
                **(
                    {"timer_live_debug": previous_timer_live_debug}
                    if previous_timer_live_debug is not None
                    else {}
                ),
            )

        try:
            import_result = self.importer(self.source, data_root=self.data_root)
        except SavedVariablesImportError as error:
            raise CalibrationWatchError(f"automatic SavedVariables import failed: {error}") from error

        summary_document: dict[str, Any]
        try:
            summary_result = self.summarizer(import_result.calibration)
            summary_document = summary_result.as_dict()
        except CalibrationSummaryError as error:
            summary_document = {"status": "failed", "error": str(error)}
        except (OSError, TypeError, ValueError) as error:
            summary_document = {"status": "failed", "error": str(error)}

        summary_ok = summary_document.get("status") == "ok"
        timer_summary_documents = {
            completion.campaign_run_id: self._timer_summary_document(
                Path(import_result.calibration), completion
            )
            for completion in new_completions
        }
        timer_run_ids = [
            completion.campaign_run_id
            for completion in new_completions
            if completion.campaign_id == TIMER_CAMPAIGN_ID
        ]
        terminal_records: dict[str, dict[str, Any]] = {}
        for completion in new_completions:
            if (
                completion.campaign_id != TIMER_CAMPAIGN_ID
                or completion.terminal_event != TIMER_RECOVERY_COMPLETION_EVENT
            ):
                continue
            terminal_record = _terminal_record(
                Path(import_result.calibration), completion
            )
            if terminal_record is not None:
                terminal_records[completion.campaign_run_id] = terminal_record
        timer_live_debug_documents = self._finalize_timer_live_debug(
            timer_run_ids,
            terminal_records=terminal_records,
        )
        for completion in new_completions:
            timer_live_debug_documents.setdefault(
                completion.campaign_run_id,
                {
                    "status": "not_applicable",
                    "campaign_run_id": completion.campaign_run_id,
                    "reason": "not_a_timer_calibration_campaign",
                },
            )
        for completion in new_completions:
            self._attach_timer_debug_bundle(
                timer_summary_documents[completion.campaign_run_id],
                timer_live_debug_documents[completion.campaign_run_id],
            )
        timer_summary_document = timer_summary_documents[
            new_completions[-1].campaign_run_id
        ]
        timer_live_debug_document = timer_live_debug_documents[
            new_completions[-1].campaign_run_id
        ]
        profile_document: dict[str, Any]
        if not summary_ok:
            profile_document = {
                "status": "pending",
                "reason": "summary_failed",
                "request": str(self.profile_output),
                "metadata": str(self.profile_metadata),
            }
        else:
            try:
                has_static_profile = _contains_static_profile(import_result.calibration)
            except (OSError, UnicodeError, WowsimsProfileError) as error:
                profile_document = {
                    "status": "error",
                    "error": str(error),
                    "request": str(self.profile_output),
                    "metadata": str(self.profile_metadata),
                }
            else:
                if not has_static_profile:
                    profile_document = {
                        "status": "pending",
                        "reason": "static_profile_not_captured",
                        "request": str(self.profile_output),
                        "metadata": str(self.profile_metadata),
                    }
                else:
                    try:
                        profile_result = self.profile_generator(
                            [Path(import_result.calibration)],
                            output_path=self.profile_output,
                            metadata_path=self.profile_metadata,
                        )
                        profile_document = profile_result.as_dict()
                    except (OSError, TypeError, ValueError, AttributeError) as error:
                        profile_document = {
                            "status": "error",
                            "error": str(error),
                            "request": str(self.profile_output),
                            "metadata": str(self.profile_metadata),
                        }

        phase5_present = summary_ok and _summary_contains_phase5(
            summary_document.get("output")
        )
        replay_document: dict[str, Any]
        if not phase5_present:
            replay_document = {
                "status": "not_applicable",
                "reason": "phase5_specialized_run_not_present",
            }
        elif profile_document.get("status") != "ok":
            replay_document = {
                "status": "pending",
                "reason": "live_profile_not_ready",
                "output": str(self.phase5_replay_output),
            }
        else:
            try:
                replay_result = self._run_phase5_replay(
                    Path(summary_document["output"]),
                    Path(profile_document["request"]),
                )
                if not isinstance(replay_result, dict):
                    raise TypeError("Phase-5 replayer must return a JSON object")
                validation_status = replay_result.get("validation_status")
                if validation_status not in {"matched", "not_matched"}:
                    raise ValueError(
                        "Phase-5 replay validation_status must be matched or not_matched"
                    )
                self._write_json(self.phase5_replay_output, replay_result)
                replay_document = {
                    "status": (
                        "matched"
                        if validation_status == "matched"
                        else "not_matched"
                    ),
                    "validation_status": validation_status,
                    "output": str(self.phase5_replay_output),
                    "kind": replay_result.get("kind"),
                    "task_run_id": replay_result.get("task_run_id"),
                }
            except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
                replay_document = {
                    "status": "replay_error",
                    "error": str(error),
                    "output": str(self.phase5_replay_output),
                }

        phase6_present = summary_ok and _summary_contains_phase6_rage(
            summary_document.get("output")
        )
        phase6_incomplete_reason = (
            _summary_phase6_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase6_present
            else None
        )
        phase6_audit_document: dict[str, Any]
        if phase6_incomplete_reason is not None:
            phase6_audit_document = {
                "status": "summary_incomplete",
                "reason": phase6_incomplete_reason,
            }
        elif not phase6_present:
            phase6_audit_document = {
                "status": "not_applicable",
                "reason": "phase6_rage_specialized_run_not_present",
            }
        else:
            phase6_audit_document = self._phase6_audit_document(
                Path(summary_document["output"])
            )

        phase7_present = summary_ok and _summary_contains_phase7_rage(
            summary_document.get("output")
        )
        phase7_incomplete_reason = (
            _summary_phase7_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase7_present
            else None
        )
        phase7_joint_audit_document: dict[str, Any]
        if phase7_incomplete_reason is not None:
            phase7_joint_audit_document = {
                "status": "summary_incomplete",
                "reason": phase7_incomplete_reason,
            }
        elif not phase7_present:
            phase7_joint_audit_document = {
                "status": "not_applicable",
                "reason": "phase7_rage_specialized_run_not_present",
            }
        else:
            phase7_joint_audit_document = self._phase7_joint_audit_document(
                Path(summary_document["output"])
            )

        phase8_present = summary_ok and _summary_contains_phase8_rage(
            summary_document.get("output")
        )
        phase8_incomplete_reason = (
            _summary_phase8_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase8_present
            else None
        )
        phase8_joint_audit_document: dict[str, Any]
        if phase8_incomplete_reason is not None:
            phase8_joint_audit_document = self._supersede_phase8_joint_audit(
                phase8_incomplete_reason,
                Path(summary_document["output"]),
            )
        elif not phase8_present:
            phase8_joint_audit_document = {
                "status": "not_applicable",
                "reason": "phase8_rage_specialized_run_not_present",
            }
        else:
            phase8_joint_audit_document = self._phase8_joint_audit_document(
                Path(summary_document["output"])
            )

        phase9_present = summary_ok and _summary_contains_phase9_rage(
            summary_document.get("output")
        )
        phase9_incomplete_reason = (
            _summary_phase9_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase9_present
            else None
        )
        phase9_formula_audit_document: dict[str, Any]
        if phase9_incomplete_reason is not None:
            phase9_formula_audit_document = {
                "status": "summary_incomplete",
                "reason": phase9_incomplete_reason,
                "output": str(self.phase9_formula_audit_output),
                "simulator_patch_allowed": False,
            }
        elif not phase9_present:
            phase9_formula_audit_document = {
                "status": "not_applicable",
                "reason": "phase9_rage_specialized_run_not_present",
            }
        else:
            phase9_formula_audit_document = self._phase9_formula_audit_document(
                Path(summary_document["output"])
            )

        phase10_present = summary_ok and _summary_contains_phase10_rage(
            summary_document.get("output")
        )
        phase10_incomplete_reason = (
            _summary_phase10_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase10_present
            else None
        )
        phase10_identification_audit_document: dict[str, Any]
        if phase10_incomplete_reason is not None:
            phase10_identification_audit_document = {
                "status": "summary_incomplete",
                "reason": phase10_incomplete_reason,
                "output": str(self.phase10_identification_audit_output),
                "phase11_candidate_ready": False,
                "simulator_patch_allowed": False,
            }
        elif not phase10_present:
            phase10_identification_audit_document = {
                "status": "not_applicable",
                "reason": "phase10_rage_specialized_run_not_present",
            }
        else:
            phase10_identification_audit_document = (
                self._phase10_identification_audit_document(
                    Path(summary_document["output"])
                )
            )

        phase11_present = summary_ok and _summary_contains_phase11_rage(
            summary_document.get("output")
        )
        phase11_incomplete_reason = (
            _summary_phase11_incomplete_reason(summary_document.get("output"))
            if summary_ok and not phase11_present
            else None
        )
        phase11_external_holdout_audit_document: dict[str, Any]
        if phase11_incomplete_reason is not None:
            phase11_external_holdout_audit_document = {
                "status": "summary_incomplete",
                "reason": phase11_incomplete_reason,
                "output": str(self.phase11_external_holdout_audit_output),
                "external_holdout_passed": False,
                "candidate_external_validated": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
                "unconditional_global_rage_go_patch_allowed": False,
            }
        elif not phase11_present:
            phase11_external_holdout_audit_document = {
                "status": "not_applicable",
                "reason": "phase11_rage_specialized_run_not_present",
            }
        else:
            phase11_external_holdout_audit_document = (
                self._phase11_external_holdout_audit_document(
                    Path(summary_document["output"])
                )
            )

        phase12_present = summary_ok and _summary_contains_terminal_phase12(
            summary_document.get("output")
        )
        phase12_audit_document: dict[str, Any]
        if not phase12_present:
            phase12_audit_document = {
                "status": "not_applicable",
                "reason": "completed_phase12_specialized_campaign_not_present",
                "simulator_patch_allowed": False,
                "simulator_patch": None,
            }
        else:
            phase12_audit_document = self._phase12_audit_document(
                Path(summary_document["output"])
            )

        imported_at = _utc_now()
        import_document = import_result.as_dict()
        for completion in new_completions:
            state["processed_campaigns"].append(
                {
                    "source": source_text,
                    "campaign_run_id": completion.campaign_run_id,
                    "marker_sequence": completion.sequence,
                    "terminal_event": completion.terminal_event,
                    "source_signature": signature_document,
                    "imported_at": imported_at,
                    "import": import_document,
                    "summary": summary_document,
                    "profile": profile_document,
                    "phase5_replay": replay_document,
                    "phase6_rage_audit": phase6_audit_document,
                    "phase7_joint_audit": phase7_joint_audit_document,
                    "phase8_joint_audit": phase8_joint_audit_document,
                    "phase9_formula_audit": phase9_formula_audit_document,
                    "phase10_identification_audit": (
                        phase10_identification_audit_document
                    ),
                    "phase11_external_holdout_audit": (
                        phase11_external_holdout_audit_document
                    ),
                    "phase12_audit": phase12_audit_document,
                    "timer_summary": timer_summary_documents[
                        completion.campaign_run_id
                    ],
                    "timer_live_debug": timer_live_debug_documents[
                        completion.campaign_run_id
                    ],
                }
            )
        state["updated_at"] = imported_at
        self._write_json(self.state_path, state)

        if timer_summary_document.get("status") == "summary_error":
            status = "campaign_imported_timer_summary_failed"
        elif timer_summary_document.get("status") == "incomplete_evidence":
            status = "campaign_imported_timer_incomplete_evidence"
        elif timer_summary_document.get("status") == "complete":
            status = "campaign_imported_timer_complete"
        elif not summary_ok:
            status = "campaign_imported_summary_failed"
        elif phase12_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase12_audit_error"
        elif phase12_audit_document.get("status") == "partial":
            status = "campaign_imported_phase12_partial"
        elif phase12_audit_document.get("status") == "complete_with_deferred":
            status = "campaign_imported_phase12_complete_with_deferred"
        elif phase12_audit_document.get("status") == "complete":
            status = "campaign_imported_phase12_complete"
        elif phase11_external_holdout_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase11_summary_incomplete"
        elif phase11_external_holdout_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase11_external_holdout_audit_error"
        elif phase11_external_holdout_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase11_insufficient_evidence"
        elif phase11_external_holdout_audit_document.get("status") == "holdout_failed":
            status = "campaign_imported_phase11_holdout_failed"
        elif phase11_external_holdout_audit_document.get("status") == "external_holdout_validated":
            status = "campaign_imported_phase11_external_holdout_validated"
        elif phase10_identification_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase10_summary_incomplete"
        elif phase10_identification_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase10_identification_audit_error"
        elif phase10_identification_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase10_insufficient_evidence"
        elif phase10_identification_audit_document.get("status") == "identification_failed":
            status = "campaign_imported_phase10_identification_failed"
        elif phase10_identification_audit_document.get("status") == "holdout_failed":
            status = "campaign_imported_phase10_holdout_failed"
        elif phase10_identification_audit_document.get("status") == "phase11_candidate_ready":
            status = "campaign_imported_phase10_phase11_candidate_ready"
        elif phase9_formula_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase9_summary_incomplete"
        elif phase9_formula_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase9_formula_audit_error"
        elif phase9_formula_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase9_insufficient_evidence"
        elif phase9_formula_audit_document.get("status") == "formula_not_identified":
            status = "campaign_imported_phase9_formula_not_identified"
        elif phase9_formula_audit_document.get("status") == "formula_identified":
            status = "campaign_imported_phase9_formula_identified"
        elif phase8_joint_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase8_summary_incomplete"
        elif phase8_joint_audit_document.get("status") == "phase6_summary_unavailable":
            status = "campaign_imported_phase8_phase6_summary_unavailable"
        elif phase8_joint_audit_document.get("status") == "phase7_summary_unavailable":
            status = "campaign_imported_phase8_phase7_summary_unavailable"
        elif phase8_joint_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase8_joint_audit_error"
        elif phase8_joint_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase8_insufficient_evidence"
        elif phase8_joint_audit_document.get("status") == "formula_not_identified":
            status = "campaign_imported_phase8_formula_not_identified"
        elif phase8_joint_audit_document.get("status") == "formula_identified":
            status = "campaign_imported_phase8_formula_identified"
        elif phase7_joint_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase7_summary_incomplete"
        elif phase7_joint_audit_document.get("status") == "phase6_summary_unavailable":
            status = "campaign_imported_phase7_phase6_summary_unavailable"
        elif phase7_joint_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase7_joint_audit_error"
        elif phase7_joint_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase7_insufficient_evidence"
        elif phase7_joint_audit_document.get("status") == "formula_not_identified":
            status = "campaign_imported_phase7_formula_not_identified"
        elif phase7_joint_audit_document.get("status") == "formula_identified":
            status = "campaign_imported_phase7_formula_identified"
        elif phase6_audit_document.get("status") == "summary_incomplete":
            status = "campaign_imported_phase6_summary_incomplete"
        elif replay_document.get("status") == "replay_error":
            status = "campaign_imported_replay_error"
        elif phase6_audit_document.get("status") == "audit_error":
            status = "campaign_imported_phase6_rage_audit_error"
        elif phase6_audit_document.get("status") == "not_matched":
            status = "campaign_imported_phase6_rage_not_matched"
        elif phase6_audit_document.get("status") == "insufficient_evidence":
            status = "campaign_imported_phase6_rage_insufficient_evidence"
        elif replay_document.get("status") == "not_matched":
            status = "campaign_imported_replay_not_matched"
        elif phase5_present and replay_document.get("status") == "pending":
            status = "campaign_imported_replay_pending"
        else:
            status = "campaign_imported"
        if timer_summary_document.get("status") == "summary_error":
            message = (
                "Completed timer campaign was imported, but its independent timer "
                "decoder failed; the imported JSONL remains saved."
            )
        elif timer_summary_document.get("status") == "incomplete_evidence":
            message = (
                "Completed timer campaign was imported and decoded, but one or more "
                "frozen timer evidence gates are incomplete."
            )
        elif timer_summary_document.get("status") == "complete":
            message = (
                "Completed timer campaign was imported and its four-stage timer "
                "evidence was decoded; simulator comparison remains separate."
            )
        elif not summary_ok:
            message = (
                "Completed calibration campaign was imported, but automatic summary failed; "
                "the live wowsims profile remains pending."
            )
        elif phase12_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-12 campaign was imported and summarized, but its "
                "collection audit failed; no simulator patch was authorized."
            )
        elif phase12_audit_document.get("status") == "partial":
            message = (
                "Completed Phase-12 campaign was imported and audited; at least one "
                "mechanism gate is partial or incomplete, and no simulator patch was authorized."
            )
        elif phase12_audit_document.get("status") == "complete_with_deferred":
            message = (
                "Completed Phase-12 campaign was imported and audited; available-environment "
                "gates completed while special-environment tasks remain explicitly DEFERRED."
            )
        elif phase12_audit_document.get("status") == "complete":
            message = (
                "Completed Phase-12 campaign was imported and its collection gates are "
                "fully observed; a separate evidence review is still required before any patch."
            )
        elif phase11_external_holdout_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-11 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no patch was authorized."
            )
        elif phase11_external_holdout_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-11 campaign was imported and summarized, but its "
                "external-holdout audit failed; evidence remains saved."
            )
        elif phase11_external_holdout_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-11 campaign was imported and audited, but its strict "
                "combat and eight-sample external evidence gate did not pass."
            )
        elif phase11_external_holdout_audit_document.get("status") == "holdout_failed":
            message = (
                "Completed Phase-11 campaign was imported and audited; at least one "
                "external holdout contradicted the frozen candidate, so no patch was authorized."
            )
        elif phase11_external_holdout_audit_document.get("status") == "external_holdout_validated":
            message = (
                "Completed Phase-11 campaign was imported and audited; all eight external "
                "holdouts matched and a scoped Turtle registry patch was authorized."
            )
        elif phase10_identification_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-10 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no candidate was published."
            )
        elif phase10_identification_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-10 campaign was imported and summarized, but its "
                "two-hand rage identification audit failed; evidence remains saved."
            )
        elif phase10_identification_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-10 campaign was imported and audited, but its strict "
                "combat and twelve-sample evidence gate did not pass."
            )
        elif phase10_identification_audit_document.get("status") == "identification_failed":
            message = (
                "Completed Phase-10 campaign was imported and audited; the "
                "outcome-specific identification fits did not pass."
            )
        elif phase10_identification_audit_document.get("status") == "holdout_failed":
            message = (
                "Completed Phase-10 campaign was imported and audited; its fitted "
                "candidate failed the four-sample internal holdout."
            )
        elif phase10_identification_audit_document.get("status") == "phase11_candidate_ready":
            message = (
                "Completed Phase-10 campaign was imported and audited; the fitted "
                "candidate passed its internal holdout and is ready for Phase 11."
            )
        elif phase9_formula_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-9 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no formula patch was authorized."
            )
        elif phase9_formula_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-9 campaign was imported and summarized, but its "
                "preregistered white-rage audit failed; evidence remains saved."
            )
        elif phase9_formula_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-9 campaign was imported and audited, but its strict "
                "eight-sample evidence gate remains incomplete."
            )
        elif phase9_formula_audit_document.get("status") == "formula_not_identified":
            message = (
                "Completed Phase-9 campaign was imported and audited; the held-out "
                "samples do not uniquely identify M1 or M2."
            )
        elif phase9_formula_audit_document.get("status") == "formula_identified":
            message = (
                "Completed Phase-9 campaign was imported and audited; exactly one "
                "preregistered replacement formula is identified at both armor endpoints."
            )
        elif phase8_joint_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-8 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no three-phase audit was run."
            )
        elif phase8_joint_audit_document.get("status") == "phase6_summary_unavailable":
            message = (
                "Completed Phase-8 campaign was imported and summarized, but no "
                "completed strict Phase-6 baseline summary was available."
            )
        elif phase8_joint_audit_document.get("status") == "phase7_summary_unavailable":
            message = (
                "Completed Phase-8 campaign was imported and summarized, but no "
                "completed strict Phase-7 weapon-speed summary was available."
            )
        elif phase8_joint_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-8 campaign was imported and summarized, but its "
                "three-phase white-rage audit failed; evidence remains saved."
            )
        elif phase8_joint_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-8 campaign was imported and audited with Phases 6 "
                "and 7, but the strict evidence gate remains incomplete."
            )
        elif phase8_joint_audit_document.get("status") == "formula_not_identified":
            message = (
                "Completed Phase-8 campaign was imported and audited with Phases 6 "
                "and 7; the held-out samples did not identify the predeclared model."
            )
        elif phase8_joint_audit_document.get("status") == "formula_identified":
            message = (
                "Completed Phase-8 campaign was imported and audited with Phases 6 "
                "and 7; the held-out samples identify the predeclared model set."
            )
        elif phase7_joint_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-7 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no joint formula audit was run."
            )
        elif phase7_joint_audit_document.get("status") == "phase6_summary_unavailable":
            message = (
                "Completed Phase-7 campaign was imported and summarized, but no "
                "completed strict Phase-6 baseline summary was available for joint audit."
            )
        elif phase7_joint_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-7 campaign was imported and summarized, but its joint "
                "white-rage formula audit failed; imported evidence remains saved."
            )
        elif phase7_joint_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-7 campaign was imported and jointly audited with "
                "Phase 6, but the strict evidence gate remains incomplete."
            )
        elif phase7_joint_audit_document.get("status") == "formula_not_identified":
            message = (
                "Completed Phase-7 campaign was imported and jointly audited with "
                "Phase 6; the evidence does not uniquely identify a replacement formula."
            )
        elif phase7_joint_audit_document.get("status") == "formula_identified":
            message = (
                "Completed Phase-7 campaign was imported and jointly audited with "
                "Phase 6; the audit identifies a replacement formula."
            )
        elif phase6_audit_document.get("status") == "summary_incomplete":
            message = (
                "Completed Phase-6 campaign was imported, but its strict specialized "
                "summary retained incomplete evidence; no rage-formula audit was run."
            )
        elif replay_document.get("status") == "replay_error":
            message = (
                "Completed Phase-5 campaign was imported and summarized, but the "
                "automatic matched replay failed; imported evidence remains saved."
            )
        elif phase6_audit_document.get("status") == "audit_error":
            message = (
                "Completed Phase-6 campaign was imported and summarized, but the "
                "automatic white-rage formula audit failed; imported evidence remains saved."
            )
        elif phase6_audit_document.get("status") == "not_matched":
            message = (
                "Completed Phase-6 campaign was imported and summarized; observed "
                "white-swing rage does not match the current wowsims formula."
            )
        elif phase6_audit_document.get("status") == "insufficient_evidence":
            message = (
                "Completed Phase-6 campaign was imported and summarized; the white-rage "
                "formula audit needs eligible evidence in all four armor strata."
            )
        elif replay_document.get("status") == "not_matched":
            message = (
                "Completed Phase-5 campaign was imported and summarized; the matched "
                "replay ran but its environment gate did not match."
            )
        elif phase5_present and replay_document.get("status") == "pending":
            message = (
                "Completed Phase-5 campaign was imported and summarized; matched "
                "replay is pending a generated live profile."
            )
        elif phase6_audit_document.get("status") == "matched":
            message = (
                "Completed Phase-6 campaign was imported and summarized; observed "
                "white-swing rage matches the current wowsims formula."
            )
        elif replay_document.get("status") == "matched":
            message = (
                "Completed Phase-5 campaign was imported, summarized, and matched "
                "against the simulator automatically."
            )
        elif profile_document.get("status") == "ok":
            message = (
                "Completed calibration campaign was imported and summarized automatically; "
                "the live wowsims profile was generated."
            )
        elif profile_document.get("status") == "error":
            message = (
                "Completed calibration campaign was imported and summarized automatically; "
                "live wowsims profile generation failed."
            )
        else:
            message = (
                "Completed calibration campaign was imported and summarized automatically; "
                "the live wowsims profile is pending an explicit static-profile capture."
            )
        terminal_events = [
            item.terminal_event for item in new_completions
        ]
        if "CALIBRATION_CAMPAIGN_INCOMPLETE" in terminal_events:
            message = message.replace("Completed ", "Incomplete terminal ", 1)
        self._log(
            status,
            campaign_run_ids=[item.campaign_run_id for item in new_completions],
            campaign_terminal_events=terminal_events,
            source_signature=signature_document,
            calibration=import_document.get("calibration"),
            summary=summary_document,
            profile=profile_document,
            phase5_replay=replay_document,
            phase6_rage_audit=phase6_audit_document,
            phase7_joint_audit=phase7_joint_audit_document,
            phase8_joint_audit=phase8_joint_audit_document,
            phase9_formula_audit=phase9_formula_audit_document,
            phase10_identification_audit=phase10_identification_audit_document,
            phase11_external_holdout_audit=(
                phase11_external_holdout_audit_document
            ),
            phase12_audit=phase12_audit_document,
            timer_summary=timer_summary_document,
            timer_live_debug=timer_live_debug_document,
        )
        return self._write_status(
            status,
            message,
            source_signature=signature_document,
            campaign_run_ids=[item.campaign_run_id for item in new_completions],
            campaign_terminal_events=terminal_events,
            import_result=import_document,
            summary=summary_document,
            profile=profile_document,
            phase5_replay=replay_document,
            phase6_rage_audit=phase6_audit_document,
            phase7_joint_audit=phase7_joint_audit_document,
            phase8_joint_audit=phase8_joint_audit_document,
            phase9_formula_audit=phase9_formula_audit_document,
            phase10_identification_audit=phase10_identification_audit_document,
            phase11_external_holdout_audit=(
                phase11_external_holdout_audit_document
            ),
            phase12_audit=phase12_audit_document,
            timer_summary=timer_summary_document,
            timer_live_debug=timer_live_debug_document,
        )

    def run_forever(self, *, poll_seconds: float, stable_seconds: float) -> None:
        """Poll until interrupted, processing each source signature at most once."""

        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        self._log("watcher_started", pid=os.getpid(), source=str(self.source))
        self._write_status(
            "watching",
            "Waiting for WoW to write a terminal calibration campaign on reload or logout.",
            pid=os.getpid(),
        )

        pending_signature: SourceSignature | None = None
        pending_since = 0.0
        handled_signature: SourceSignature | None = None
        try:
            while True:
                self._poll_timer_live_debug()
                self._poll_shadow_live_export()
                try:
                    current = _source_signature(self.source)
                except CalibrationWatchError as error:
                    if pending_signature is not None:
                        pending_signature = None
                    self._write_status("waiting_for_source", str(error), pid=os.getpid())
                    time.sleep(poll_seconds)
                    continue

                now = time.monotonic()
                if current != pending_signature:
                    pending_signature = current
                    pending_since = now
                    self._write_status(
                        "waiting_for_stable_write",
                        "Observed a SavedVariables change; waiting for the write to settle.",
                        pid=os.getpid(),
                        source_signature=current.as_dict(),
                    )
                elif current != handled_signature and now - pending_since >= stable_seconds:
                    try:
                        self.process_once()
                    except CalibrationWatchError as error:
                        self._log("watch_error", error=str(error))
                        self._write_status(
                            "error",
                            str(error),
                            pid=os.getpid(),
                            source_signature=current.as_dict(),
                        )
                    handled_signature = current
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            self._log("watcher_stopped", pid=os.getpid())
            self._write_status("stopped", "Calibration watcher stopped.", pid=os.getpid())
        finally:
            try:
                if self.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self.pid_path.unlink()
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calibration_watch",
        description=(
            "Watch one explicit BrainOfCat.lua for a terminal calibration "
            "campaign, then import and summarize it automatically."
        ),
    )
    parser.add_argument("--source", type=Path, required=True, help="exact BrainOfCat.lua path")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"offline data root (default: {DEFAULT_DATA_ROOT})",
    )
    parser.add_argument("--runtime-directory", type=Path)
    parser.add_argument(
        "--phase6-summary",
        type=Path,
        help=(
            "completed strict Phase-6 summary for the Phase-7 joint audit; "
            "otherwise the newest eligible summary under data-root is used"
        ),
    )
    parser.add_argument("--phase7-joint-audit-output", type=Path)
    parser.add_argument(
        "--phase7-summary",
        type=Path,
        help=(
            "completed strict Phase-7 summary for the Phase-8 joint audit; "
            "otherwise the newest eligible summary under data-root is used"
        ),
    )
    parser.add_argument("--phase8-joint-audit-output", type=Path)
    parser.add_argument("--phase9-formula-audit-output", type=Path)
    parser.add_argument("--phase10-identification-audit-output", type=Path)
    parser.add_argument("--phase11-external-holdout-audit-output", type=Path)
    parser.add_argument("--phase12-audit-output", type=Path)
    parser.add_argument(
        "--phase12-preregistration",
        type=Path,
        default=DEFAULT_PHASE12_PREREGISTRATION,
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--stable-seconds", type=float, default=1.5)
    parser.add_argument(
        "--once",
        action="store_true",
        help="inspect the current stable file once and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.poll_seconds <= 0 or args.stable_seconds < 0:
        print("poll-seconds must be positive and stable-seconds cannot be negative", file=sys.stderr)
        return 2
    watcher = CalibrationWatcher(
        args.source,
        data_root=args.data_root,
        runtime_directory=args.runtime_directory,
        phase6_summary=args.phase6_summary,
        phase7_joint_audit_output=args.phase7_joint_audit_output,
        phase7_summary=args.phase7_summary,
        phase8_joint_audit_output=args.phase8_joint_audit_output,
        phase9_formula_audit_output=args.phase9_formula_audit_output,
        phase10_identification_audit_output=(
            args.phase10_identification_audit_output
        ),
        phase11_external_holdout_audit_output=(
            args.phase11_external_holdout_audit_output
        ),
        phase12_audit_output=args.phase12_audit_output,
        phase12_preregistration=args.phase12_preregistration,
    )
    try:
        if args.once:
            result = watcher.process_once()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        watcher.run_forever(
            poll_seconds=args.poll_seconds,
            stable_seconds=args.stable_seconds,
        )
    except CalibrationWatchError as error:
        watcher._log("watch_error", error=str(error))
        watcher._write_status("error", str(error))
        print(f"Calibration watcher failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
