"""Low-overhead shard telemetry for the adaptive Cat-gap pipeline.

The scientific shard receipt is deliberately untouched.  This module emits a
separate measurement document that can be consumed by
``optimization_throughput_profile_v1``.  Collection is opt-in so an existing
worker invocation has no profiling syscalls in its task loop.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence


CAPTURE_SCHEMA = "fury_cat_gap_shard_throughput_capture/v1"
JOB_WINDOW_SCHEMA = "fury_cat_gap_job_window_throughput/v1"
_PHASES = ("ROLLOUT", "SERIALIZATION", "VALIDATION", "ANALYSIS", "MERGE")


class FuryCatGapThroughputCaptureV1Error(ValueError):
    """A capture cannot be represented without inventing measurements."""


@dataclass(frozen=True)
class _CpuSnapshot:
    self_user: float
    self_system: float
    child_user: float
    child_system: float
    peak_rss_bytes: int


@dataclass
class _Phase:
    user: float = 0.0
    system: float = 0.0
    wall: float = 0.0
    failures: int = 0


def _linux_process_metrics(pid: int) -> tuple[float, float, int]:
    """Return cumulative CPU and peak RSS for one live Linux process."""

    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "stat").read_text(encoding="ascii")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        ticks = float(os.sysconf("SC_CLK_TCK"))
        # fields starts at proc stat field 3, so utime/stime are offsets 11/12.
        user = float(fields[11]) / ticks
        system = float(fields[12]) / ticks
        peak = 0
        for line in (proc / "status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                peak = int(line.split()[1]) * 1024
                break
        return user, system, peak
    except (OSError, ValueError, IndexError):
        # A process may exit between the two proc reads. os.times() then accounts
        # for it through children_user/children_system at the next snapshot.
        return 0.0, 0.0, 0


def _windows_process_metrics(pid: int) -> tuple[float, float, int]:
    """Return cumulative CPU and peak working set using the Windows API."""

    if os.name != "nt":
        return 0.0, 0.0, 0
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = (
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return 0.0, 0.0, 0
        try:
            creation = FILETIME()
            exit_time = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return 0.0, 0.0, 0
            peak = 0
            if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                peak = int(counters.PeakWorkingSetSize)

            def seconds(value: FILETIME) -> float:
                return ((int(value.high) << 32) | int(value.low)) / 10_000_000.0

            return seconds(user), seconds(kernel), peak
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return 0.0, 0.0, 0


def _own_peak_rss_bytes() -> int:
    if os.name == "nt":
        return _windows_process_metrics(os.getpid())[2]
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux reports KiB; macOS reports bytes.  Cluster execution is Linux,
        # but retain the documented platform distinction for local smoke runs.
        return value * 1024 if sys_platform_linux() else value
    except (ImportError, OSError, ValueError):
        return 0


def sys_platform_linux() -> bool:
    return os.name == "posix" and Path("/proc/self/stat").is_file()


def _snapshot(child_pids: Sequence[int]) -> _CpuSnapshot:
    times = os.times()
    child_user = float(times.children_user)
    child_system = float(times.children_system)
    child_peak = 0
    if sys_platform_linux():
        for pid in child_pids:
            user, system, peak = _linux_process_metrics(pid)
            child_user += user
            child_system += system
            child_peak += peak
    elif os.name == "nt":
        for pid in child_pids:
            user, system, peak = _windows_process_metrics(pid)
            child_user += user
            child_system += system
            child_peak += peak
    return _CpuSnapshot(
        self_user=float(times.user),
        self_system=float(times.system),
        child_user=child_user,
        child_system=child_system,
        peak_rss_bytes=_own_peak_rss_bytes() + child_peak,
    )


def _counter_pair(artifact: Any) -> tuple[int | None, int | None]:
    """Extract exact retained-step counts when the native artifact exposes them."""

    if not isinstance(artifact, Mapping):
        return None, None
    steps = artifact.get("steps")
    if isinstance(steps, list):
        interactions = len(steps)
        events = 0
        for step in steps:
            if not isinstance(step, Mapping):
                return None, interactions
            execution = step.get("ordered_execution")
            sink_events = (
                execution.get("sink_events")
                if isinstance(execution, Mapping)
                else None
            )
            if not isinstance(sink_events, list):
                return None, interactions
            events += len(sink_events)
            events += int(execution.get("wait_event") is not None)
        return events, interactions
    decisions = artifact.get("decisions")
    if isinstance(decisions, list):
        # Candidate executor artifacts expose decisions but not a common event
        # ledger; report only the count that is actually present.
        return None, len(decisions)
    return None, None


class ShardThroughputCaptureV1:
    """Accumulate exclusive worker phases and one shard resource boundary."""

    def __init__(
        self,
        *,
        batch_id: str,
        node: str,
        shard_index: int,
        workload_class: str,
        child_pids: Sequence[int] = (),
        logical_cpu_capacity: int = 1,
        input_bytes: int = 0,
        trace_mode: str = "FULL",
        policy_id: str | None = None,
        policy_role: str | None = None,
        producer: str | None = None,
        workload_strata: Sequence[str] = (),
    ) -> None:
        if not batch_id or not node or not workload_class:
            raise FuryCatGapThroughputCaptureV1Error(
                "batch_id, node, and workload_class are required"
            )
        if shard_index < 0 or logical_cpu_capacity <= 0 or input_bytes < 0:
            raise FuryCatGapThroughputCaptureV1Error("invalid shard capture bounds")
        if trace_mode not in {"COMPACT", "FULL"}:
            raise FuryCatGapThroughputCaptureV1Error("invalid trace_mode")
        identity = (policy_id, policy_role, producer)
        if any(value is not None for value in identity) and not all(
            isinstance(value, str) and value.strip() for value in identity
        ):
            raise FuryCatGapThroughputCaptureV1Error(
                "homogeneous capture identity requires policy_id, policy_role, and producer"
            )
        tags = tuple(sorted({str(value).strip() for value in workload_strata}))
        if any(not value for value in tags) or len(tags) != len(workload_strata):
            raise FuryCatGapThroughputCaptureV1Error(
                "workload_strata must contain unique non-empty tags"
            )
        if all(value is not None for value in identity) != bool(tags):
            raise FuryCatGapThroughputCaptureV1Error(
                "homogeneous capture identity and workload_strata must be supplied together"
            )
        self.batch_id = batch_id
        self.node = node
        self.shard_index = shard_index
        self.workload_class = workload_class
        self.child_pids = tuple(int(pid) for pid in child_pids if int(pid) > 0)
        self.logical_cpu_capacity = logical_cpu_capacity
        self.input_bytes = input_bytes
        self.trace_mode = trace_mode
        self.policy_id = str(policy_id).strip() if policy_id is not None else None
        self.policy_role = str(policy_role).strip() if policy_role is not None else None
        self.producer = str(producer).strip() if producer is not None else None
        self.workload_strata = tags
        self._start_epoch_seconds = time.time()
        self._start_wall = time.perf_counter()
        self._start_cpu = _snapshot(self.child_pids)
        self._peak_rss_bytes = self._start_cpu.peak_rss_bytes
        self._phases = {name: _Phase() for name in _PHASES}
        self._phase_stack: list[list[float]] = []
        self._attempted = 0
        self._valid = 0
        self._incomplete = 0
        self._failed = 0
        self._active_attempt = False
        self._simulated_seconds = 0.0
        self._event_count = 0
        self._event_count_observed = True
        self._event_count_observed_rollouts = 0
        self._interaction_count = 0
        self._interaction_count_observed = True
        self._interaction_count_observed_rollouts = 0
        self._json_bytes = 0
        self._compressed_bytes = 0
        self._output_bytes = 0
        self._phase_bytes = {
            name: {"input": 0, "json": 0, "compressed": 0, "output": 0}
            for name in _PHASES
        }
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if name not in self._phases:
            raise FuryCatGapThroughputCaptureV1Error(f"unknown phase {name!r}")
        started_wall = time.perf_counter()
        started = os.times()
        frame = [0.0, 0.0, 0.0]
        self._phase_stack.append(frame)
        try:
            yield
        except BaseException:
            self._phases[name].failures += 1
            raise
        finally:
            ended = os.times()
            if not self._phase_stack or self._phase_stack[-1] is not frame:
                raise FuryCatGapThroughputCaptureV1Error(
                    "profiling phases must close in stack order"
                )
            self._phase_stack.pop()
            inclusive_user = max(0.0, float(ended.user - started.user))
            inclusive_system = max(0.0, float(ended.system - started.system))
            inclusive_wall = max(0.0, time.perf_counter() - started_wall)
            phase = self._phases[name]
            phase.user += max(0.0, inclusive_user - frame[0])
            phase.system += max(0.0, inclusive_system - frame[1])
            phase.wall += max(0.0, inclusive_wall - frame[2])
            if self._phase_stack:
                parent = self._phase_stack[-1]
                parent[0] += inclusive_user
                parent[1] += inclusive_system
                parent[2] += inclusive_wall

    def begin_rollout(self) -> None:
        if self._active_attempt:
            raise FuryCatGapThroughputCaptureV1Error("rollout attempt already active")
        self._attempted += 1
        self._active_attempt = True

    def finish_rollout(self, lane: Mapping[str, Any]) -> None:
        if not self._active_attempt:
            raise FuryCatGapThroughputCaptureV1Error("no rollout attempt is active")
        self._active_attempt = False
        fatal = lane.get("fatal_error_count")
        complete = lane.get("completion_criterion_met") is True
        eligible = lane.get("offline_score_eligible") is True
        if isinstance(fatal, int) and not isinstance(fatal, bool) and fatal > 0:
            self._failed += 1
        elif complete and eligible:
            self._valid += 1
            elapsed = lane.get("elapsed_ms")
            if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed > 0:
                self._simulated_seconds += elapsed / 1000.0
        else:
            self._incomplete += 1
        events, interactions = _counter_pair(lane.get("artifact"))
        if events is None:
            self._event_count_observed = False
        else:
            self._event_count += events
            self._event_count_observed_rollouts += 1
        if interactions is None:
            self._interaction_count_observed = False
        else:
            self._interaction_count += interactions
            self._interaction_count_observed_rollouts += 1

    def add_serialization_bytes(self, *, json_bytes: int = 0) -> None:
        if json_bytes < 0:
            raise FuryCatGapThroughputCaptureV1Error("byte counts cannot be negative")
        self._json_bytes += json_bytes
        self._phase_bytes["SERIALIZATION"]["json"] += json_bytes

    def set_output_bytes(self, *, compressed_bytes: int, output_bytes: int) -> None:
        if compressed_bytes < 0 or output_bytes < 0:
            raise FuryCatGapThroughputCaptureV1Error("byte counts cannot be negative")
        self._compressed_bytes = compressed_bytes
        self._output_bytes = output_bytes
        self._phase_bytes["SERIALIZATION"]["compressed"] = compressed_bytes
        self._phase_bytes["SERIALIZATION"]["output"] = output_bytes

    def mark_shard_failed(self) -> None:
        """Exclude every non-published rollout from the valid denominator."""

        self._active_attempt = False
        self._valid = 0
        self._incomplete = 0
        self._failed = self._attempted
        self._simulated_seconds = 0.0
        self._event_count_observed = False
        self._interaction_count_observed = False

    def add_phase_bytes(
        self,
        name: str,
        *,
        input_bytes: int = 0,
        json_bytes: int = 0,
        compressed_bytes: int = 0,
        output_bytes: int = 0,
    ) -> None:
        if name not in self._phase_bytes or any(
            value < 0
            for value in (input_bytes, json_bytes, compressed_bytes, output_bytes)
        ):
            raise FuryCatGapThroughputCaptureV1Error("invalid phase byte accounting")
        row = self._phase_bytes[name]
        row["input"] += input_bytes
        row["json"] += json_bytes
        row["compressed"] += compressed_bytes
        row["output"] += output_bytes

    def finish(self) -> dict[str, Any]:
        if self._finished:
            raise FuryCatGapThroughputCaptureV1Error("capture already finished")
        self._finished = True
        if self._active_attempt:
            self._active_attempt = False
            self._failed += 1
            self._event_count_observed = False
            self._interaction_count_observed = False
        ended = _snapshot(self.child_pids)
        self._peak_rss_bytes = max(self._peak_rss_bytes, ended.peak_rss_bytes)
        wall = max(time.perf_counter() - self._start_wall, 1e-9)
        child_user = max(0.0, ended.child_user - self._start_cpu.child_user)
        child_system = max(0.0, ended.child_system - self._start_cpu.child_system)
        if self._phase_stack:
            raise FuryCatGapThroughputCaptureV1Error(
                "cannot finish while a profiling phase is active"
            )
        tasks: list[dict[str, Any]] = []
        for name in _PHASES:
            phase = self._phases[name]
            if phase.wall <= 0.0 and not (name == "ROLLOUT" and self._attempted):
                continue
            if name == "ROLLOUT":
                if self._failed == self._attempted and self._attempted:
                    status = "FAILED"
                elif self._valid == self._attempted and self._attempted:
                    status = "COMPLETE_VALID"
                else:
                    status = "INCOMPLETE"
                attempted = self._attempted
                valid = self._valid
                incomplete = self._incomplete
                failed = self._failed
                simulated = self._simulated_seconds
                events = self._event_count if self._event_count_observed else None
                interactions = (
                    self._interaction_count
                    if self._interaction_count_observed
                    else None
                )
                phase_child_user = child_user
                phase_child_system = child_system
                policy_id = self.policy_id
                policy_role = self.policy_role or "mixed_shard"
                producer = self.producer
                workload_strata = list(self.workload_strata)
                cpu_attribution = (
                    "EXACT_HOMOGENEOUS_SHARD"
                    if self.policy_id is not None
                    else "UNAVAILABLE_IDENTITY_NOT_RECORDED"
                )
            else:
                status = "FAILED" if phase.failures else "COMPLETE_VALID"
                attempted = valid = incomplete = failed = 0
                simulated = 0.0
                events = interactions = None
                phase_child_user = phase_child_system = 0.0
                policy_id = producer = None
                policy_role = "pipeline"
                workload_strata = []
                cpu_attribution = "NOT_APPLICABLE"
            tasks.append(
                {
                    "task_id": f"{self.batch_id}:{name.lower()}",
                    "batch_id": self.batch_id,
                    "task_kind": name,
                    "workload_class": self.workload_class,
                    "policy_role": policy_role,
                    "policy_id": policy_id,
                    "producer": producer,
                    "workload_strata": workload_strata,
                    "rollout_cpu_attribution": cpu_attribution,
                    "rollout_breakdown": [],
                    "completion_status": status,
                    "trace_mode": self.trace_mode,
                    "attempted_rollout_count": attempted,
                    "complete_valid_rollout_count": valid,
                    "incomplete_rollout_count": incomplete,
                    "failed_rollout_count": failed,
                    "user_cpu_seconds": phase.user,
                    "system_cpu_seconds": phase.system,
                    "child_user_cpu_seconds": phase_child_user,
                    "child_system_cpu_seconds": phase_child_system,
                    "wall_seconds": max(phase.wall, 1e-9),
                    "peak_rss_bytes": self._peak_rss_bytes,
                    "simulated_combat_seconds": simulated,
                    "event_count": events,
                    "state_interaction_count": interactions,
                    "input_bytes": (
                        self._phase_bytes[name]["input"]
                        + (self.input_bytes if name == "ROLLOUT" else 0)
                    ),
                    "json_bytes": self._phase_bytes[name]["json"],
                    "compressed_bytes": self._phase_bytes[name]["compressed"],
                    "output_bytes": self._phase_bytes[name]["output"],
                }
            )
        return {
            "schema": CAPTURE_SCHEMA,
            "measurement_scope": "ONE_SHARD_PROCESS_WITH_PERSISTENT_BRIDGE",
            "worker": {
                "node": self.node,
                "shard_index": self.shard_index,
                "logical_cpu_capacity": self.logical_cpu_capacity,
            },
            "capture_window": {
                "started_at_epoch_seconds": self._start_epoch_seconds,
                "ended_at_epoch_seconds": time.time(),
            },
            "tasks": tasks,
            "batches": [
                {
                    "batch_id": self.batch_id,
                    "node": self.node,
                    "measurement_scope": "SHARD_PROCESS",
                    "wall_seconds": wall,
                    "logical_cpu_capacity": self.logical_cpu_capacity,
                    "peak_rss_bytes": self._peak_rss_bytes,
                }
            ],
            "counter_semantics": {
                "event_count": "retained ordered sink events plus explicit wait events",
                "state_interaction_count": "retained policy decision epochs",
                "null": "counter not exposed by that producer artifact",
            },
            "counter_coverage": {
                "completed_rollout_count": (
                    self._valid + self._incomplete + self._failed
                ),
                "event_count_observed_rollout_count": (
                    self._event_count_observed_rollouts
                ),
                "state_interaction_count_observed_rollout_count": (
                    self._interaction_count_observed_rollouts
                ),
            },
            "child_cpu_semantics": (
                "os.times terminated children plus explicit live Linux or Windows "
                "bridge PIDs; assigned to ROLLOUT"
            ),
            "rss_semantics": (
                "sum of process peak RSS and explicitly observed live child VmHWM; "
                "a conservative process-group peak on Linux and Windows"
            ),
            "receipt_protocol_modified": False,
        }


def write_capture_document_v1(path: str | Path, document: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def combine_capture_documents_v1(
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Combine small worker/reducer sidecars into profiler input without raw shards."""

    tasks: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        if document.get("schema") != CAPTURE_SCHEMA:
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} has an unsupported schema"
            )
        raw_tasks = document.get("tasks")
        raw_batches = document.get("batches")
        if not isinstance(raw_tasks, list) or not isinstance(raw_batches, list):
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} lacks task or batch rows"
            )
        if not all(isinstance(row, Mapping) for row in raw_tasks):
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} contains a non-object task"
            )
        if not all(isinstance(row, Mapping) for row in raw_batches):
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} contains a non-object batch"
            )
        tasks.extend(dict(row) for row in raw_tasks)
        batches.extend(dict(row) for row in raw_batches)
    task_ids = [str(row.get("task_id")) for row in tasks]
    batch_ids = [str(row.get("batch_id")) for row in batches]
    if len(task_ids) != len(set(task_ids)) or len(batch_ids) != len(set(batch_ids)):
        raise FuryCatGapThroughputCaptureV1Error(
            "combined capture task_id and batch_id values must be unique"
        )
    return {"tasks": tasks, "batches": batches}


def build_job_window_profile_input_v1(
    documents: Sequence[Mapping[str, Any]],
    job_window: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind complete shard sidecars to one explicit node/cluster time window.

    Shard CPU and IO remain present in ``per_shard``.  Tasks are rebound to one
    node batch so the throughput profiler can divide their CPU only by the
    common job wall time, never by a one-worker shard lifetime.
    """

    job_id = job_window.get("job_id")
    started = job_window.get("started_at_epoch_seconds")
    ended = job_window.get("ended_at_epoch_seconds")
    nodes = job_window.get("nodes")
    representative = job_window.get("representative_workload_strata", [])
    if (
        not isinstance(job_id, str)
        or not job_id.strip()
        or isinstance(started, bool)
        or not isinstance(started, (int, float))
        or isinstance(ended, bool)
        or not isinstance(ended, (int, float))
        or float(ended) <= float(started)
        or not isinstance(nodes, list)
        or not nodes
        or not isinstance(representative, list)
    ):
        raise FuryCatGapThroughputCaptureV1Error("invalid explicit job window")
    representative_rows: list[list[str]] = []
    for row in representative:
        if not isinstance(row, list):
            raise FuryCatGapThroughputCaptureV1Error(
                "representative workload strata must be arrays"
            )
        tags = sorted({str(value).strip() for value in row})
        if not tags or any(not value for value in tags) or len(tags) != len(row):
            raise FuryCatGapThroughputCaptureV1Error(
                "representative workload strata must be non-empty and unique"
            )
        representative_rows.append(tags)
    if len({tuple(row) for row in representative_rows}) != len(representative_rows):
        raise FuryCatGapThroughputCaptureV1Error(
            "representative workload strata must be unique"
        )
    representative_rows.sort()

    captures: dict[tuple[str, int], Mapping[str, Any]] = {}
    for index, document in enumerate(documents):
        if document.get("schema") != CAPTURE_SCHEMA:
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} has an unsupported schema"
            )
        worker = document.get("worker")
        window = document.get("capture_window")
        if not isinstance(worker, Mapping) or not isinstance(window, Mapping):
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} lacks worker or capture-window metadata"
            )
        node = worker.get("node")
        shard = worker.get("shard_index")
        capture_start = window.get("started_at_epoch_seconds")
        capture_end = window.get("ended_at_epoch_seconds")
        if (
            not isinstance(node, str)
            or not node
            or isinstance(shard, bool)
            or not isinstance(shard, int)
            or shard < 0
            or isinstance(capture_start, bool)
            or not isinstance(capture_start, (int, float))
            or isinstance(capture_end, bool)
            or not isinstance(capture_end, (int, float))
            or float(capture_start) < float(started)
            or float(capture_end) > float(ended)
            or float(capture_end) < float(capture_start)
        ):
            raise FuryCatGapThroughputCaptureV1Error(
                f"capture {index} is outside the explicit job window"
            )
        key = (node, shard)
        if key in captures:
            raise FuryCatGapThroughputCaptureV1Error(
                f"duplicate shard capture for {node} shard {shard}"
            )
        captures[key] = document

    tasks: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    per_shard: list[dict[str, Any]] = []
    expected: set[tuple[str, int]] = set()
    seen_nodes: set[str] = set()
    for node_row in nodes:
        if not isinstance(node_row, Mapping):
            raise FuryCatGapThroughputCaptureV1Error("job node must be an object")
        node = node_row.get("node")
        capacity = node_row.get("logical_cpu_capacity")
        worker_count = node_row.get("worker_count")
        shard_indices = node_row.get("shard_indices")
        if (
            not isinstance(node, str)
            or not node
            or node in seen_nodes
            or isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity <= 0
            or isinstance(worker_count, bool)
            or not isinstance(worker_count, int)
            or worker_count <= 0
            or not isinstance(shard_indices, list)
            or not shard_indices
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in shard_indices
            )
            or len(set(shard_indices)) != len(shard_indices)
        ):
            raise FuryCatGapThroughputCaptureV1Error("invalid job node specification")
        seen_nodes.add(node)
        node_keys = {(node, value) for value in shard_indices}
        expected.update(node_keys)
        batch_id = f"{job_id.strip()}:{node}"
        node_peak = 0
        for key in sorted(node_keys):
            document = captures.get(key)
            if document is None:
                raise FuryCatGapThroughputCaptureV1Error(
                    f"missing shard capture for {key[0]} shard {key[1]}"
                )
            raw_tasks = document.get("tasks")
            raw_batches = document.get("batches")
            if not isinstance(raw_tasks, list) or not isinstance(raw_batches, list) or len(raw_batches) != 1:
                raise FuryCatGapThroughputCaptureV1Error(
                    f"invalid shard capture payload for {key[0]} shard {key[1]}"
                )
            shard_batch = raw_batches[0]
            if not isinstance(shard_batch, Mapping):
                raise FuryCatGapThroughputCaptureV1Error("shard batch must be an object")
            node_peak = max(node_peak, int(shard_batch.get("peak_rss_bytes", 0)))
            for raw_task in raw_tasks:
                if not isinstance(raw_task, Mapping):
                    raise FuryCatGapThroughputCaptureV1Error("shard task must be an object")
                row = dict(raw_task)
                source_batch_id = str(row.get("batch_id"))
                row["task_id"] = f"{job_id.strip()}:{node}:{row.get('task_id')}"
                row["source_shard_batch_id"] = source_batch_id
                row["batch_id"] = batch_id
                tasks.append(row)
            per_shard.append(
                {
                    "node": node,
                    "shard_index": key[1],
                    "capture_window": dict(document["capture_window"]),
                    "tasks": [dict(row) for row in raw_tasks],
                    "batch": dict(shard_batch),
                }
            )
        batches.append(
            {
                "batch_id": batch_id,
                "node": node,
                "measurement_scope": "NODE_JOB_WINDOW",
                "wall_seconds": float(ended) - float(started),
                "logical_cpu_capacity": capacity,
                "peak_rss_bytes": node_peak,
                "job_window_id": job_id.strip(),
                "window_started_at_epoch_seconds": float(started),
                "window_ended_at_epoch_seconds": float(ended),
                "worker_count": worker_count,
                "shard_count": len(shard_indices),
                "representative_workload_strata": representative_rows,
            }
        )
    if set(captures) != expected:
        raise FuryCatGapThroughputCaptureV1Error(
            "capture set contains shards outside the explicit job node specification"
        )
    return {
        "schema": JOB_WINDOW_SCHEMA,
        "job_window": {
            "job_id": job_id.strip(),
            "started_at_epoch_seconds": float(started),
            "ended_at_epoch_seconds": float(ended),
            "representative_workload_strata": representative_rows,
        },
        "tasks": tasks,
        "batches": batches,
        "per_shard": sorted(per_shard, key=lambda row: (row["node"], row["shard_index"])),
        "rss_semantics": "node batch stores max per-shard peak, not concurrent node RSS",
    }


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapThroughputCaptureV1Error(
            f"cannot read {path}: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise FuryCatGapThroughputCaptureV1Error(f"{path} must contain an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind shard throughput sidecars to an explicit shared job window"
    )
    parser.add_argument("--capture", type=Path, action="append", required=True)
    parser.add_argument("--job-window", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = build_job_window_profile_input_v1(
        [_read_json(path) for path in args.capture],
        _read_json(args.job_window),
    )
    write_capture_document_v1(args.output, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "CAPTURE_SCHEMA",
    "JOB_WINDOW_SCHEMA",
    "FuryCatGapThroughputCaptureV1Error",
    "ShardThroughputCaptureV1",
    "build_job_window_profile_input_v1",
    "combine_capture_documents_v1",
    "main",
    "write_capture_document_v1",
)
