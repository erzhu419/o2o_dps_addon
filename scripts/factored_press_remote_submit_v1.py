"""Plan or queue one six-shard factored external-press matrix phase.

This entrypoint is intentionally phase-at-a-time.  It assumes
``factored_press_remote_stage_v1.py`` has already provisioned one shared home
inode on node001--node006 with the exact v19 Linux bridge and compact derived
inputs.  Planning is read-only.  ``--submit`` atomically adds exactly six
tasks to scheduleurm; it never dispatches them.

The worker closure contains no Chronicle CSV and no checkpoint.  Completed
case artifacts stay on the shared server filesystem.  Only the six small
batch summaries and the phase reducer artifact are named as manual fetch
candidates; scheduler result auto-pull is not enabled.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    SCHEDULER_SKILL,
    _scheduler,
    _shared_home,
)
from o2o_dps.factored_external_press_matrix_v1 import MAX_SAMPLE_INDEX

DEFAULT_V19_BRIDGE = PROJECT_ROOT / "bin/o2obridge.press-v19.linux-amd64"
ACTIVE_STATUSES = frozenset(("queued", "launching", "running"))
PHASES = ("training", "held_out_transfer", "untouched_fresh")
PHASE_DEFAULT_SAMPLES = {
    "training": 24,
    "held_out_transfer": 8,
    "untouched_fresh": 32,
}
SHARD_COUNT = 6
PHASE_DEFAULT_WORKERS = {
    "training": 48,
    "held_out_transfer": 16,
    "untouched_fresh": 64,
}
DEFAULT_RAM_MB = 65_536
DEFAULT_MAX_STATES = 1
CPU_TRAINING_JUSTIFICATION = (
    "Independent CPU simulator branches use no GPU kernels and recover from "
    "atomic per-case JSON artifacts after interruption."
)


def _valid_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    return run_id


def _phase_router_path(project: PurePosixPath, phase: str) -> PurePosixPath | None:
    if phase == "training":
        return None
    if phase == "held_out_transfer":
        return project / "results/training/proposal.json"
    return project / "results/held_out_transfer/authorization.json"


def _phase_result_root(
    project: PurePosixPath, phase: str, sample_start: int = 0,
) -> PurePosixPath:
    root = project / f"results/{phase}"
    return root if sample_start == 0 else root / f"sample-start-{sample_start:06d}"


def _phase_reducer_path(
    project: PurePosixPath, phase: str, sample_start: int = 0,
) -> PurePosixPath:
    root = _phase_result_root(project, phase, sample_start)
    if phase == "training":
        return root / "proposal.json"
    if phase == "held_out_transfer":
        return root / "authorization.json"
    return root / "fresh-summary.json"


def _training_sparse_shortlist_path(
    project: PurePosixPath, sample_start: int = 0,
) -> PurePosixPath:
    return _phase_result_root(
        project, "training", sample_start,
    ) / "sparse-guard-shortlist.json"


def _assigned_item_count(samples_per_cell: int, shard_index: int) -> int:
    total = 3 * 4 * samples_per_cell
    return len(range(shard_index, total, SHARD_COUNT))


def _reducer_command(
    *, python: PurePosixPath, project: PurePosixPath, phase: str,
    item_database: PurePosixPath, sample_start: int = 0,
) -> str:
    module = "o2o_dps.factored_external_press_matrix_v1"
    result_root = _phase_result_root(project, phase, sample_start)
    inputs = " ".join(
        str(result_root / f"shard-{index:02d}/cases/*.json")
        for index in range(SHARD_COUNT)
    )
    if phase == "training":
        prefix = [str(python), "-m", module, "fit", "--input"]
        suffix = [
            "--min-distinct-seeds", "6", "--item-db", str(item_database),
            "--output", str(_phase_reducer_path(project, phase, sample_start)),
        ]
        exact_command = f"{shlex.join(prefix)} {inputs} {shlex.join(suffix)}"
        sparse_prefix = [str(python), "-m", module, "fit-sparse", "--input"]
        sparse_suffix = [
            "--min-distinct-seeds", "6",
            "--min-opportunity-seeds", "6",
            "--item-db", str(item_database),
            "--output", str(_training_sparse_shortlist_path(
                project, sample_start,
            )),
        ]
        sparse_command = (
            f"{shlex.join(sparse_prefix)} {inputs} {shlex.join(sparse_suffix)}"
        )
        # Both reducers consume the same immutable case artifacts.  Keep the
        # exact-rule result for negative-evidence continuity, then derive the
        # independent sparse development shortlist in the same server-side
        # reducer step.
        return f"{exact_command} && {sparse_command}"
    elif phase == "held_out_transfer":
        prefix = [
            str(python), "-m", module, "authorize", "--proposal",
            str(_phase_router_path(project, phase)), "--input",
        ]
        suffix = [
            "--min-distinct-seeds", "8", "--item-db", str(item_database),
            "--output", str(_phase_reducer_path(project, phase, sample_start)),
        ]
    else:
        prefix = [
            str(python), "-m", module, "fresh", "--authorization",
            str(_phase_router_path(project, phase)), "--input",
        ]
        suffix = [
            "--item-db", str(item_database), "--output",
            str(_phase_reducer_path(project, phase, sample_start)),
        ]
    # The six fixed globs must remain unquoted so the remote shell expands the
    # complete case set.  All other arguments are shell-quoted.
    return f"{shlex.join(prefix)} {inputs} {shlex.join(suffix)}"


def build_factored_press_phase_plan_v1(
    *, shared_home: str, run_id: str, phase: str = "training",
    samples_per_cell: int | None = None, workers: int | None = None,
    ram_mb: int = DEFAULT_RAM_MB, max_states: int = DEFAULT_MAX_STATES,
    sample_start: int = 0, scheduler_cli: str | None = None,
) -> dict[str, Any]:
    """Build the exact remote commands and scheduler task specs, without I/O."""

    _valid_run_id(run_id)
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    if not shared_home.startswith("/home/") or "\n" in shared_home:
        raise ValueError("shared_home must be one absolute remote home")
    samples = PHASE_DEFAULT_SAMPLES[phase] if samples_per_cell is None else samples_per_cell
    workers = PHASE_DEFAULT_WORKERS[phase] if workers is None else workers
    if type(samples) is not int or samples < 1:
        raise ValueError("samples_per_cell must be positive")
    if type(sample_start) is not int or sample_start < 0:
        raise ValueError("sample_start must be a nonnegative integer")
    if sample_start + samples - 1 > MAX_SAMPLE_INDEX:
        raise ValueError("sample range is outside the matrix seed namespace")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    if type(ram_mb) is not int or ram_mb < 1:
        raise ValueError("ram_mb must be positive")
    if type(max_states) is not int or max_states < 1:
        raise ValueError("max_states must be positive")
    home = PurePosixPath(shared_home)
    run_root = (
        home / "scheduleurm_work/o2o-dps-hpc/runs/factored-press-v1" / run_id
    )
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    wowsims = run_root / "AddOns/BrainOfCat/wowsims-turtle"
    python = home / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    bridge = project / "bin/o2obridge.press-v19.linux-amd64"
    capsule = (
        project
        / "offline_data/derived/fury_offline_scenario_capsules/v2"
        / (
            "fury_offline_scenario_capsules_v2."
            "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
        )
    )
    selector = project / "offline_data/derived/historical_representative_build_selector/v1"
    catalog = project / "offline_data/derived/historical_build_catalog/v1"
    item_database = wowsims / "assets/database/db.json"
    router = _phase_router_path(project, phase)
    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")

    specs: list[dict[str, Any]] = []
    submit_argv: list[list[str]] = []
    shard_paths: list[dict[str, str]] = []
    cohort_suffix = (
        "" if sample_start == 0 else f"-sample-start-{sample_start:06d}"
    )
    signature_batch_prefix = (
        f"BrainOfCat/factored-press-v1-{run_id}-{phase}{cohort_suffix}"
    )
    scheduler_intent_label = (
        f"BrainOfCat-factored-press-{run_id}-{phase}{cohort_suffix}"
    )
    phase_result_root = _phase_result_root(project, phase, sample_start)
    for shard_index, preferred_node in enumerate(NODE_NAMES):
        result_dir = phase_result_root / f"shard-{shard_index:02d}"
        cases = result_dir / "cases"
        summary = result_dir / "summary.json"
        assigned = _assigned_item_count(samples, shard_index)
        declared_workers = min(workers, assigned)
        worker_argv = [
            str(python), "-m", "o2o_dps.factored_external_press_matrix_v1",
            "batch", "--phase", phase,
            "--sample-start", str(sample_start),
            "--samples-per-cell", str(samples),
            "--shard-index", str(shard_index),
            "--shard-count", str(SHARD_COUNT),
            "--workers", str(declared_workers),
            "--max-states", str(max_states),
            "--bridge", str(bridge),
            "--bridge-cwd", str(wowsims),
            "--item-db", str(item_database),
            "--selector-manifest", str(selector / "manifest.json"),
            "--representatives", str(selector / "representatives.jsonl"),
            "--catalog-manifest", str(catalog / "manifest.json"),
            "--catalog-data", str(catalog / "catalog.jsonl.gz"),
            "--output-dir", str(cases),
            "--summary", str(summary),
        ]
        if router is not None:
            worker_argv.extend(("--router", str(router)))
        signature = f"{signature_batch_prefix}/shard-{shard_index:02d}"
        description = (
            f"BrainOfCat factored press {phase} shard "
            f"{shard_index + 1}/{SHARD_COUNT}"
        )
        batch_command = shlex.join(worker_argv)
        success_marker = (
            f"DONE factored_press_batch phase={phase} sample_start={sample_start} "
            f"shard={shard_index:02d}"
        )
        spec = {
            "description": description,
            # scheduleurm currently treats a quiet rc=0 local-backend process
            # as failed unless its log contains a success word.  Keep this
            # explicit marker until the scheduler's exit-sentinel handling is
            # corrected; the batch still exits nonzero on any failed case.
            "cmd": (
                f"{batch_command} && printf '%s\\n' "
                f"{shlex.quote(success_marker)}"
            ),
            "cwd": str(project),
            "signature": signature,
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/factored-press-v1/cpu-branch",
            "vram": 0,
            "ram_mb": ram_mb,
            "cpu": declared_workers,
            "cpu_parallel_items": assigned,
            "cpu_parallel_total_items": assigned,
            "cpu_parallel_logical_items": assigned,
            "cpu_parallel_item_multiplier": 1,
            "cpu_parallel_start": 0,
            "cpu_parallel_end": assigned,
            "cpu_parallel_shard_index": shard_index,
            "cpu_parallel_num_shards": SHARD_COUNT,
            "cpu_batch_plan": {
                "workers": declared_workers,
                "waves": (assigned + declared_workers - 1) // declared_workers,
                "physical_cores": declared_workers,
                "total_physical_cores": declared_workers * SHARD_COUNT,
                "last_wave_items": (
                    assigned % declared_workers or declared_workers
                ),
            },
            "priority": "normal",
            "preferred_node": preferred_node,
            "allowed_nodes": list(NODE_NAMES),
            "skip_launch_staging": True,
            "reroute_on_node_down": True,
            "node_down_requeue_s": 300,
            "allow_cpu_training": True,
            "cpu_training_justification": CPU_TRAINING_JUSTIFICATION,
            # submit-jsonl is a trusted path.  Retain the two ordinary-submit
            # acknowledgements in the plan so the safety decision is explicit.
            "allow_no_ckpt": True,
            "allow_no_resume": True,
            "env_spec": "none",
        }
        specs.append(spec)
        ordinary = [
            "python3", scheduler_cli, "submit",
            "--description", description,
            "--cmd", spec["cmd"], "--cwd", str(project),
            "--signature", signature, "--project", "BrainOfCat",
            "--resource-family", spec["resource_family"],
            "--vram", "0", "--ram-mb", str(ram_mb),
            "--cpu", str(declared_workers), "--cpu-parallel-items", str(assigned),
            "--priority", "normal", "--preferred-node", preferred_node,
        ]
        for node in NODE_NAMES:
            ordinary.extend(("--allowed-node", node))
        ordinary.extend((
            "--skip-launch-staging", "--reroute-on-node-down",
            "--node-down-requeue-s", "300", "--allow-cpu-training",
            "--cpu-training-justification", CPU_TRAINING_JUSTIFICATION,
            "--allow-no-ckpt", "--allow-no-resume", "--env-spec", "none",
        ))
        submit_argv.append(ordinary)
        shard_paths.append({
            "shard": str(shard_index), "result_dir": str(result_dir),
            "cases": str(cases), "summary": str(summary),
            "assigned_case_count": str(assigned),
            "declared_workers": str(declared_workers),
        })

    pull_allowlist = [row["summary"] for row in shard_paths]
    pull_allowlist.append(str(_phase_reducer_path(project, phase, sample_start)))
    if phase == "training":
        pull_allowlist.append(str(_training_sparse_shortlist_path(
            project, sample_start,
        )))
    required_paths = [
        {"path": str(project), "kind": "directory"},
        {"path": str(python), "kind": "executable"},
        {"path": str(bridge), "kind": "executable"},
        {"path": str(capsule), "kind": "file"},
        {"path": str(selector / "manifest.json"), "kind": "file"},
        {"path": str(selector / "representatives.jsonl"), "kind": "file"},
        {"path": str(catalog / "manifest.json"), "kind": "file"},
        {"path": str(catalog / "catalog.jsonl.gz"), "kind": "file"},
        {"path": str(item_database), "kind": "file"},
    ]
    if router is not None:
        required_paths.append({"path": str(router), "kind": "file"})
    return {
        "schema": "factored_press_remote_phase_plan/v1",
        "status": "PLAN_ONLY_NOT_SUBMITTED_NOT_DISPATCHED",
        "run_id": run_id,
        "phase": phase,
        "shared_home": shared_home,
        "run_root": str(run_root),
        "project_root": str(project),
        "sample_start": sample_start,
        "samples_per_cell": samples,
        "matrix_cell_count": 12,
        "global_case_count": 12 * samples,
        "shard_count": SHARD_COUNT,
        "worker_cap_per_shard": workers,
        "workers_per_shard": [
            int(row["declared_workers"]) for row in shard_paths
        ],
        "cpu_per_shard": [int(row["declared_workers"]) for row in shard_paths],
        "ram_mb_per_shard": ram_mb,
        "max_states": max_states,
        "bridge_generation": "press-v19",
        "local_bridge_source": str(DEFAULT_V19_BRIDGE),
        "remote_bridge": str(bridge),
        "router": str(router) if router else None,
        "signature_batch_prefix": signature_batch_prefix,
        "scheduler_intent_label": scheduler_intent_label,
        "required_remote_paths": required_paths,
        "shards": shard_paths,
        "task_specs": specs,
        "ordinary_submit_argv": submit_argv,
        "ordinary_submit_commands": [shlex.join(row) for row in submit_argv],
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl --file <PLAN.json> "
            "--trusted --json --intent-label "
            f"{shlex.quote(scheduler_intent_label)}"
        ),
        "post_phase_reducer_command": _reducer_command(
            python=python, project=project, phase=phase,
            item_database=item_database, sample_start=sample_start,
        ),
        "training_sparse_shortlist": (
            str(_training_sparse_shortlist_path(project, sample_start))
            if phase == "training" else None
        ),
        "automatic_result_pull": False,
        "manual_small_fetch_candidates": pull_allowlist,
        "server_resident_case_artifacts": True,
        "raw_chronicle_csv_required": False,
        "checkpoint_required": False,
        "scheduler_guards_required": [
            "HEAL_NEEDS_CLAUDE and HEAL_NEEDS_USER are empty",
            "this run/phase has zero pending scheduler escalations",
            "scheduler doctor reports ok before the write",
            "no queued, launching, or running task uses a planned signature",
            "no result artifact exists for the planned run and phase",
            "all six nodes expose the same shared home inode",
            "the independently named remote press-v19 bridge is executable",
            "CPU-training/no-checkpoint overrides are explicit because the batch is CPU-only and per-case recoverable",
        ],
    }


def _test_remote_path(scheduler: Any, node: str, path: str, kind: str) -> bool:
    flag = {"file": "-f", "directory": "-d", "executable": "-x"}[kind]
    rc, _, _ = scheduler.run_on(
        node, f"test {flag} {shlex.quote(path)}", timeout=20, check=False,
    )
    return rc == 0


def _read_remote_json(scheduler: Any, node: str, path: str) -> dict[str, Any]:
    rc, out, err = scheduler.run_on(
        node, f"cat {shlex.quote(path)}", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(
            f"cannot read prior phase artifact {path}: {str(err or '')[-300:]}"
        )
    try:
        value = json.loads(out)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"prior phase artifact is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"prior phase artifact is not a JSON object: {path}")
    return value


def inventory_factored_press_phase_v1(
    scheduler: Any, plan: Mapping[str, Any], *, probe_node: str = "node001",
) -> dict[str, Any]:
    """Read scheduler state and shared artifacts, refusing any duplicate run."""

    signatures = {row["signature"] for row in plan["task_specs"]}
    planned_result_dirs = {row["result_dir"] for row in plan["shards"]}
    with scheduler.state_lock(
        shared=True, purpose="factored-press-active-signature-inventory",
    ):
        state = scheduler.load_state()
        active_conflicts = []
        for task in state.get("tasks", []):
            if task.get("status") not in ACTIVE_STATUSES:
                continue
            command = str(task.get("cmd") or "")
            same_signature = task.get("signature") in signatures
            same_output = any(path in command for path in planned_result_dirs)
            if not same_signature and not same_output:
                continue
            active_conflicts.append({
                "id": task.get("id"), "status": task.get("status"),
                "signature": task.get("signature"),
                "conflict": (
                    "planned_signature" if same_signature
                    else "planned_remote_result_path"
                ),
            })
    if active_conflicts:
        raise RuntimeError(
            "planned scheduler task or output already active: "
            + json.dumps(active_conflicts, ensure_ascii=False)
        )

    missing = [
        row for row in plan["required_remote_paths"]
        if not _test_remote_path(scheduler, probe_node, row["path"], row["kind"])
    ]
    if missing:
        raise RuntimeError(
            "staged compact closure is incomplete: "
            + json.dumps(missing, ensure_ascii=False)
        )

    prior_phase_gate = None
    router_path = plan.get("router")
    if plan["phase"] == "held_out_transfer":
        proposal = _read_remote_json(scheduler, probe_node, str(router_path))
        if proposal.get("schema") != "factored_external_press_matrix_proposal/v1":
            raise RuntimeError("training proposal schema differs")
        semantic = proposal.get("execution_semantic_contract") or {}
        if semantic.get("max_states") != plan["max_states"]:
            raise RuntimeError("training proposal max_states differs from phase plan")
        proposed = (proposal.get("router") or {}).get(
            "proposed_transfer_route_count"
        )
        if type(proposed) is not int or proposed < 1:
            raise RuntimeError(
                "training proposal has no route worth held-out transfer"
            )
        prior_phase_gate = {
            "schema": proposal.get("schema"),
            "proposed_transfer_route_count": proposed,
        }
    elif plan["phase"] == "untouched_fresh":
        authorization = _read_remote_json(
            scheduler, probe_node, str(router_path),
        )
        if authorization.get("schema") != (
            "factored_external_press_matrix_authorization/v1"
        ):
            raise RuntimeError("transfer authorization schema differs")
        semantic = authorization.get("execution_semantic_contract") or {}
        if semantic.get("max_states") != plan["max_states"]:
            raise RuntimeError(
                "transfer authorization max_states differs from phase plan"
            )
        eligible = (authorization.get("router") or {}).get(
            "eligible_fresh_test_route_count"
        )
        if type(eligible) is not int or eligible < 1:
            raise RuntimeError(
                "transfer authorization has no route worth untouched fresh evaluation"
            )
        prior_phase_gate = {
            "schema": authorization.get("schema"),
            "eligible_fresh_test_route_count": eligible,
        }

    artifact_conflicts = []
    candidates = [row["result_dir"] for row in plan["shards"]]
    candidates.append(str(_phase_reducer_path(
        PurePosixPath(plan["project_root"]), plan["phase"],
        int(plan.get("sample_start", 0)),
    )))
    sparse_shortlist = plan.get("training_sparse_shortlist")
    if sparse_shortlist:
        candidates.append(str(sparse_shortlist))
    for path in candidates:
        if _test_remote_path(scheduler, probe_node, path, "directory") or _test_remote_path(
            scheduler, probe_node, path, "file",
        ):
            artifact_conflicts.append(path)
    if artifact_conflicts:
        raise RuntimeError(
            "planned phase already has remote artifacts: "
            + json.dumps(artifact_conflicts, ensure_ascii=False)
        )
    return {
        "schema": "factored_press_remote_phase_inventory/v1",
        "status": "CLEAR_TO_SUBMIT_NOT_DISPATCHED",
        "active_signature_conflicts": [],
        "remote_artifact_conflicts": [],
        "required_path_count": len(plan["required_remote_paths"]),
        "remote_bridge": plan["remote_bridge"],
        "prior_phase_gate": prior_phase_gate,
        "shared_data_policy": "COMPACT_DERIVED_INPUTS_ONLY_NO_RAW_CSV_NO_CKPT",
    }


def _pending_escalations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        task_id = row.get("task_id")
        if task_id:
            latest[str(task_id)] = row
    return [row for row in latest.values() if row.get("status") == "pending"]


def scheduler_escalation_inventory_v1(
    plan: Mapping[str, Any], *, state_directory: Path | None = None,
) -> dict[str, Any]:
    """Separate project-related escalations from unrelated scheduler history."""

    state_directory = state_directory or Path.home() / ".claude/scheduler"
    pending = _pending_escalations(state_directory / "escalations.jsonl")
    signatures = {row["signature"] for row in plan["task_specs"]}
    run_prefix = str(plan["signature_batch_prefix"]) + "/"
    related = [
        row for row in pending
        if str(row.get("signature") or "") in signatures
        or str(row.get("signature") or "").startswith(run_prefix)
    ]
    return {
        "pending_escalation_count_total": len(pending),
        "pending_escalation_count_related": len(related),
        "related_task_ids": [row.get("task_id") for row in related],
        "unrelated_pending_escalations_require_external_scheduler_heal": (
            len(pending) > len(related)
        ),
    }


def check_scheduler_write_guards_v1(
    plan: Mapping[str, Any], *, scheduler_cli: Path | None = None,
    state_directory: Path | None = None,
) -> dict[str, Any]:
    """Fail closed when scheduleurm requires healing before any queue write."""

    state_directory = state_directory or Path.home() / ".claude/scheduler"
    inboxes = [
        path for path in (
            state_directory / "HEAL_NEEDS_CLAUDE.md",
            state_directory / "HEAL_NEEDS_USER.md",
        )
        if path.is_file() and path.stat().st_size > 0
    ]
    escalation_inventory = scheduler_escalation_inventory_v1(
        plan, state_directory=state_directory,
    )
    if inboxes or escalation_inventory["pending_escalation_count_related"]:
        raise RuntimeError(
            "scheduler healing is required before submit: "
            f"inboxes={[str(path) for path in inboxes]} "
            "related_pending_escalations="
            f"{escalation_inventory['pending_escalation_count_related']}"
        )
    scheduler_cli = scheduler_cli or SCHEDULER_SKILL / "scheduler.py"
    completed = subprocess.run(
        [sys.executable, str(scheduler_cli), "doctor", "--json"],
        check=False, capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"scheduler doctor failed: {completed.stderr[-500:]}")
    doctor = json.loads(completed.stdout)
    if doctor.get("ok") is not True or doctor.get("issues"):
        raise RuntimeError(
            "scheduler doctor requires repair before submit: "
            + json.dumps(doctor, ensure_ascii=False)
        )
    return {
        "status": "SCHEDULER_WRITE_GUARDS_CLEAR",
        **escalation_inventory,
        "doctor": doctor,
    }


def submit_plan_atomically_v1(
    plan: Mapping[str, Any], *, scheduler_cli: Path | None = None,
) -> dict[str, Any]:
    """Queue the six trusted specs atomically; deliberately do not dispatch."""

    specs = list(plan.get("task_specs") or [])
    if len(specs) != SHARD_COUNT:
        raise ValueError("a remote phase submit must contain exactly six specs")
    scheduler_cli = scheduler_cli or SCHEDULER_SKILL / "scheduler.py"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False,
    ) as handle:
        json.dump(specs, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                sys.executable, str(scheduler_cli), "submit-jsonl",
                "--file", str(temporary), "--trusted", "--json",
                "--intent-label",
                str(plan["scheduler_intent_label"]),
            ],
            check=False, capture_output=True, text=True, timeout=120,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(f"atomic scheduler submit failed: {completed.stderr[-1000:]}")
    result = json.loads(completed.stdout)
    if result.get("count") != SHARD_COUNT:
        raise RuntimeError(f"scheduler did not accept exactly six tasks: {result}")
    return {
        "status": "SIX_TASKS_QUEUED_NOT_DISPATCHED",
        "scheduler_result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=PHASES, default="training")
    parser.add_argument("--sample-start", type=int, default=0)
    parser.add_argument("--samples-per-cell", type=int)
    parser.add_argument(
        "--workers", type=int,
        help="worker/cpu cap; defaults are training=48, transfer=16, fresh=64",
    )
    parser.add_argument("--ram-mb", type=int, default=DEFAULT_RAM_MB)
    parser.add_argument(
        "--max-states", type=int, default=DEFAULT_MAX_STATES,
        help="teacher states per training case; repeat the same value in later phases",
    )
    parser.add_argument("--probe-node", choices=NODE_NAMES, default="node001")
    parser.add_argument(
        "--bridge-source", type=Path, default=DEFAULT_V19_BRIDGE,
        help="local frozen v19 Linux bridge used to verify the staged binary",
    )
    parser.add_argument(
        "--submit", action="store_true",
        help="after all guards pass, atomically queue six tasks; never dispatch",
    )
    args = parser.parse_args()

    bridge_source = args.bridge_source.expanduser().resolve(strict=True)
    if bridge_source.name != DEFAULT_V19_BRIDGE.name:
        parser.error(
            "--bridge-source must be the independently named "
            "o2obridge.press-v19.linux-amd64"
        )
    scheduler = _scheduler()
    shared_home, home_receipts = _shared_home(scheduler)
    plan = build_factored_press_phase_plan_v1(
        shared_home=shared_home, run_id=args.run_id, phase=args.phase,
        sample_start=args.sample_start, samples_per_cell=args.samples_per_cell,
        workers=args.workers,
        ram_mb=args.ram_mb, max_states=args.max_states,
    )
    plan["shared_home_receipts"] = home_receipts
    plan["inventory"] = inventory_factored_press_phase_v1(
        scheduler, plan, probe_node=args.probe_node,
    )
    plan["scheduler_escalations"] = scheduler_escalation_inventory_v1(plan)
    if args.submit:
        plan["scheduler_write_guards"] = check_scheduler_write_guards_v1(plan)
        # Re-read both active identities and artifacts immediately before the
        # atomic queue transaction, narrowing the plan/submit race.
        plan["pre_submit_inventory"] = inventory_factored_press_phase_v1(
            scheduler, plan, probe_node=args.probe_node,
        )
        plan["submission"] = submit_plan_atomically_v1(plan)
        plan["status"] = "SIX_TASKS_QUEUED_NOT_DISPATCHED"
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
