"""Plan or queue one six-shard sparse-guard full-wave phase.

The development shortlist and every case artifact remain on the shared server
filesystem.  Planning performs no scheduler write.  ``--submit`` rechecks the
source gate, scheduler identities, remote artifact paths, related escalations,
and scheduler doctor before atomically queuing exactly six recoverable tasks;
it never dispatches them and never pulls Chronicle rows or checkpoints.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.factored_external_press_matrix_v1 import MATRIX_RANKS, MATRIX_STRATA
from o2o_dps.factored_sparse_guard_full_wave_v1 import (
    FRESH_PHASE,
    MAX_SAMPLE_INDEX,
    PHASE_SEED_BASES,
    TRANSFER_PHASE,
)
from o2o_dps.factored_latched_first_opportunity_full_wave_v1 import (
    PHASE as LATCHED_MODULE_PHASE,
    PHASE_SEED_BASE as LATCHED_PHASE_SEED_BASE,
)
from o2o_dps.factored_latched_subset_actual_full_wave_v1 import (
    PHASE as LATCHED_SUBSET_ACTUAL_MODULE_PHASE,
    PHASE_SEED_BASE as LATCHED_SUBSET_ACTUAL_PHASE_SEED_BASE,
)
from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    SCHEDULER_SKILL,
    _scheduler,
    _shared_home,
)
from scripts.factored_press_remote_submit_v1 import (
    CPU_TRAINING_JUSTIFICATION,
    check_scheduler_write_guards_v1,
    scheduler_escalation_inventory_v1,
    submit_plan_atomically_v1,
)


DEFAULT_V19_BRIDGE = PROJECT_ROOT / "bin/o2obridge.press-v19.linux-amd64"
PLAN_TRANSFER_PHASE = "sparse_held_out_transfer"
PLAN_FRESH_PHASE = "sparse_untouched_fresh"
PLAN_REFINEMENT_FRESH_PHASE = "sparse_refinement_fresh"
PLAN_LATCHED_FRESH_PHASE = "sparse_latched_fresh"
PLAN_LATCHED_DEVELOPMENT_PHASE = "sparse_latched_development_extension"
PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE = "sparse_latched_subset_actual_fresh"
LATCHED_PLAN_PHASES = (
    PLAN_LATCHED_FRESH_PHASE,
    PLAN_LATCHED_DEVELOPMENT_PHASE,
)
LATCHED_SUBSET_ACTUAL_PLAN_PHASES = (PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,)
NO_MODULE_PHASE_ARG_PLAN_PHASES = (
    *LATCHED_PLAN_PHASES,
    *LATCHED_SUBSET_ACTUAL_PLAN_PHASES,
)
PHASES = (
    PLAN_TRANSFER_PHASE,
    PLAN_FRESH_PHASE,
    PLAN_REFINEMENT_FRESH_PHASE,
    PLAN_LATCHED_FRESH_PHASE,
    PLAN_LATCHED_DEVELOPMENT_PHASE,
    PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,
)
MODULE_PHASE = {
    PLAN_TRANSFER_PHASE: TRANSFER_PHASE,
    PLAN_FRESH_PHASE: FRESH_PHASE,
    PLAN_REFINEMENT_FRESH_PHASE: FRESH_PHASE,
    PLAN_LATCHED_FRESH_PHASE: LATCHED_MODULE_PHASE,
    PLAN_LATCHED_DEVELOPMENT_PHASE: LATCHED_MODULE_PHASE,
    PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE: LATCHED_SUBSET_ACTUAL_MODULE_PHASE,
}
PHASE_DEFAULT_SAMPLES = {
    PLAN_TRANSFER_PHASE: 32,
    PLAN_FRESH_PHASE: 64,
    PLAN_REFINEMENT_FRESH_PHASE: 64,
    PLAN_LATCHED_FRESH_PHASE: 64,
    PLAN_LATCHED_DEVELOPMENT_PHASE: 192,
    PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE: 256,
}
PHASE_SOURCE_RELATIVE = {
    PLAN_TRANSFER_PHASE: "results/training/sparse-guard-shortlist-frozen-v5.json",
    PLAN_FRESH_PHASE: "results/sparse_held_out_transfer/authorization.json",
    PLAN_REFINEMENT_FRESH_PHASE: "results/sparse_refinement/refinement.json",
    PLAN_LATCHED_FRESH_PHASE: "results/sparse_latched/latched-policy.json",
    PLAN_LATCHED_DEVELOPMENT_PHASE: "results/sparse_latched/latched-policy.json",
    PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE: (
        "results/sparse_latched_subset/subset-policy.json"
    ),
}
SHARD_COUNT = 6
DEFAULT_WORKERS = 64
DEFAULT_RAM_MB = 65_536
DEFAULT_PERIOD_MS = 100
DEFAULT_MAX_PRESSES = 400
ACTIVE_STATUSES = frozenset(("queued", "launching", "running"))


def _valid_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    return run_id


def _result_root(project: PurePosixPath, phase: str) -> PurePosixPath:
    return project / f"results/{phase}"


def _reducer_path(project: PurePosixPath, phase: str) -> PurePosixPath:
    filename = "authorization.json" if phase == PLAN_TRANSFER_PHASE else "fresh-summary.json"
    return _result_root(project, phase) / filename


def _assigned_item_count(samples_per_cell: int, shard_index: int) -> int:
    total = len(MATRIX_RANKS) * len(MATRIX_STRATA) * samples_per_cell
    return len(range(shard_index, total, SHARD_COUNT))


def _reducer_command(
    *, python: PurePosixPath, project: PurePosixPath, phase: str,
    policy: PurePosixPath, item_database: PurePosixPath,
) -> str:
    module = "o2o_dps.factored_sparse_guard_full_wave_v1"
    inputs = " ".join(
        str(_result_root(project, phase) / f"shard-{index:02d}/cases/*.json")
        for index in range(SHARD_COUNT)
    )
    if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES:
        prefix = [
            str(python), "-m",
            "o2o_dps.factored_latched_subset_actual_full_wave_v1",
            "reduce", "--policy", str(policy), "--input",
        ]
    elif phase in LATCHED_PLAN_PHASES:
        prefix = [
            str(python), "-m",
            "o2o_dps.factored_latched_first_opportunity_full_wave_v1",
            "reduce", "--policy", str(policy), "--input",
        ]
    elif phase == PLAN_TRANSFER_PHASE:
        prefix = [
            str(python), "-m", module, "authorize", "--shortlist", str(policy),
            "--input",
        ]
    elif phase == PLAN_FRESH_PHASE:
        prefix = [
            str(python), "-m", module, "fresh", "--authorization", str(policy),
            "--input",
        ]
    else:
        prefix = [
            str(python), "-m",
            "o2o_dps.factored_sparse_guard_post_transfer_refinement_v1",
            "fresh", "--refinement", str(policy), "--input",
        ]
    suffix = []
    if phase not in LATCHED_SUBSET_ACTUAL_PLAN_PHASES:
        suffix.extend((
            "--min-distinct-seeds",
            "64" if phase in {
                PLAN_REFINEMENT_FRESH_PHASE, *LATCHED_PLAN_PHASES,
            } else "8",
        ))
    suffix.extend((
        "--item-db", str(item_database),
        "--output", str(_reducer_path(project, phase)),
    ))
    # Only the six fixed case globs remain unquoted for remote shell expansion.
    return f"{shlex.join(prefix)} {inputs} {shlex.join(suffix)}"


def build_factored_sparse_guard_phase_plan_v1(
    *, shared_home: str, run_id: str, phase: str = PLAN_TRANSFER_PHASE,
    samples_per_cell: int | None = None, workers: int = DEFAULT_WORKERS,
    ram_mb: int = DEFAULT_RAM_MB, sample_start: int = 0,
    period_ms: int = DEFAULT_PERIOD_MS, max_presses: int = DEFAULT_MAX_PRESSES,
    scheduler_cli: str | None = None,
) -> dict[str, Any]:
    """Build exact scheduler specs for one phase, without doing any I/O."""

    _valid_run_id(run_id)
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    if not shared_home.startswith("/home/") or "\n" in shared_home:
        raise ValueError("shared_home must be one absolute remote home")
    samples = PHASE_DEFAULT_SAMPLES[phase] if samples_per_cell is None else samples_per_cell
    if type(samples) is not int or samples < 1:
        raise ValueError("samples_per_cell must be positive")
    if phase in {PLAN_REFINEMENT_FRESH_PHASE, *LATCHED_PLAN_PHASES} and samples < 64:
        raise ValueError(f"{phase} requires at least 64 samples per cell")
    if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES and samples != 256:
        raise ValueError(f"{phase} requires exactly 256 samples per cell")
    if type(sample_start) is not int or sample_start < 0:
        raise ValueError("sample_start must be a nonnegative integer")
    if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES and sample_start != 0:
        raise ValueError(f"{phase} requires sample_start 0")
    if sample_start + samples - 1 > MAX_SAMPLE_INDEX:
        raise ValueError("sample range is outside the sparse full-wave seed namespace")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    if type(ram_mb) is not int or ram_mb < 1:
        raise ValueError("ram_mb must be positive")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")

    home = PurePosixPath(shared_home)
    run_root = home / "scheduleurm_work/o2o-dps-hpc/runs/factored-press-v1" / run_id
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    wowsims = run_root / "AddOns/BrainOfCat/wowsims-turtle"
    python = home / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    bridge = project / "bin/o2obridge.press-v19.linux-amd64"
    capsule = (
        project / "offline_data/derived/fury_offline_scenario_capsules/v2"
        / "fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
    )
    selector = project / "offline_data/derived/historical_representative_build_selector/v1"
    catalog = project / "offline_data/derived/historical_build_catalog/v1"
    item_database = wowsims / "assets/database/db.json"
    policy = project / PHASE_SOURCE_RELATIVE[phase]
    module_phase = MODULE_PHASE[phase]
    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")
    result_root = _result_root(project, phase)
    signature_prefix = f"BrainOfCat/factored-sparse-full-wave-v1-{run_id}-{phase}"
    intent_label = f"BrainOfCat-factored-sparse-full-wave-{run_id}-{phase}"

    specs: list[dict[str, Any]] = []
    submit_argv: list[list[str]] = []
    shards: list[dict[str, Any]] = []
    for shard_index, preferred_node in enumerate(NODE_NAMES):
        result_dir = result_root / f"shard-{shard_index:02d}"
        cases = result_dir / "cases"
        summary = result_dir / "summary.json"
        assigned = _assigned_item_count(samples, shard_index)
        declared_workers = min(workers, assigned)
        if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES:
            worker_module = "o2o_dps.factored_latched_subset_actual_full_wave_v1"
        elif phase in LATCHED_PLAN_PHASES:
            worker_module = "o2o_dps.factored_latched_first_opportunity_full_wave_v1"
        else:
            worker_module = "o2o_dps.factored_sparse_guard_full_wave_v1"
        worker_argv = [
            str(python), "-m", worker_module, "batch",
        ]
        if phase not in NO_MODULE_PHASE_ARG_PLAN_PHASES:
            worker_argv.extend(("--phase", module_phase))
        worker_argv.extend([
            "--policy", str(policy),
            "--sample-start", str(sample_start), "--samples-per-cell", str(samples),
            "--shard-index", str(shard_index), "--shard-count", str(SHARD_COUNT),
            "--workers", str(declared_workers), "--bridge", str(bridge),
            "--bridge-cwd", str(wowsims), "--item-db", str(item_database),
            "--selector-manifest", str(selector / "manifest.json"),
            "--representatives", str(selector / "representatives.jsonl"),
            "--catalog-manifest", str(catalog / "manifest.json"),
            "--catalog-data", str(catalog / "catalog.jsonl.gz"),
            "--period-ms", str(period_ms), "--max-presses", str(max_presses),
            "--output-dir", str(cases), "--summary", str(summary),
        ])
        signature = f"{signature_prefix}/shard-{shard_index:02d}"
        description = (
            f"BrainOfCat sparse full wave {phase} shard "
            f"{shard_index + 1}/{SHARD_COUNT}"
        )
        marker = (
            f"DONE factored_sparse_guard_full_wave_batch phase={phase} "
            f"sample_start={sample_start} shard={shard_index:02d}"
        )
        command = f"{shlex.join(worker_argv)} && printf '%s\\n' {shlex.quote(marker)}"
        spec = {
            "description": description,
            "cmd": command,
            "cwd": str(project),
            "signature": signature,
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/factored-sparse-full-wave-v1/cpu-branch",
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
                "last_wave_items": assigned % declared_workers or declared_workers,
            },
            "priority": "normal",
            "preferred_node": preferred_node,
            "allowed_nodes": list(NODE_NAMES),
            "skip_launch_staging": True,
            "reroute_on_node_down": True,
            "node_down_requeue_s": 300,
            "allow_cpu_training": True,
            "cpu_training_justification": CPU_TRAINING_JUSTIFICATION,
            "allow_no_ckpt": True,
            "allow_no_resume": True,
            "env_spec": "none",
        }
        specs.append(spec)
        ordinary = [
            "python3", scheduler_cli, "submit", "--description", description,
            "--cmd", command, "--cwd", str(project), "--signature", signature,
            "--project", "BrainOfCat", "--resource-family", spec["resource_family"],
            "--vram", "0", "--ram-mb", str(ram_mb), "--cpu", str(declared_workers),
            "--cpu-parallel-items", str(assigned), "--priority", "normal",
            "--preferred-node", preferred_node,
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
        shards.append({
            "shard": shard_index,
            "result_dir": str(result_dir),
            "cases": str(cases),
            "summary": str(summary),
            "assigned_case_count": assigned,
            "declared_workers": declared_workers,
        })

    required = [
        {"path": str(project), "kind": "directory"},
        {"path": str(python), "kind": "executable"},
        {"path": str(bridge), "kind": "executable"},
        {"path": str(capsule), "kind": "file"},
        {"path": str(selector / "manifest.json"), "kind": "file"},
        {"path": str(selector / "representatives.jsonl"), "kind": "file"},
        {"path": str(catalog / "manifest.json"), "kind": "file"},
        {"path": str(catalog / "catalog.jsonl.gz"), "kind": "file"},
        {"path": str(item_database), "kind": "file"},
        {"path": str(policy), "kind": "file"},
    ]
    reducer = _reducer_path(project, phase)
    return {
        "schema": "factored_sparse_guard_remote_phase_plan/v1",
        "status": "PLAN_ONLY_NOT_SUBMITTED_NOT_DISPATCHED",
        "run_id": run_id,
        "phase": phase,
        "module_phase": module_phase,
        "shared_home": shared_home,
        "run_root": str(run_root),
        "project_root": str(project),
        "remote_python": str(python),
        "policy_source": str(policy),
        "sample_start": sample_start,
        "samples_per_cell": samples,
        "matrix_cell_count": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "global_case_count": len(MATRIX_RANKS) * len(MATRIX_STRATA) * samples,
        "seed_namespace_base": (
            LATCHED_SUBSET_ACTUAL_PHASE_SEED_BASE
            if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES
            else LATCHED_PHASE_SEED_BASE
            if phase in LATCHED_PLAN_PHASES
            else PHASE_SEED_BASES[module_phase]
        ),
        "seed_namespace_first": (
            LATCHED_SUBSET_ACTUAL_PHASE_SEED_BASE
            if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES
            else LATCHED_PHASE_SEED_BASE
            if phase in LATCHED_PLAN_PHASES
            else PHASE_SEED_BASES[module_phase]
        ) + sample_start,
        "seed_namespace_last": (
            LATCHED_SUBSET_ACTUAL_PHASE_SEED_BASE
            if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES
            else LATCHED_PHASE_SEED_BASE
            if phase in LATCHED_PLAN_PHASES
            else PHASE_SEED_BASES[module_phase]
        ) + sample_start + samples - 1,
        "shard_count": SHARD_COUNT,
        "worker_cap_per_shard": workers,
        "workers_per_shard": [row["declared_workers"] for row in shards],
        "cpu_per_shard": [row["declared_workers"] for row in shards],
        "ram_mb_per_shard": ram_mb,
        "period_ms": period_ms,
        "max_presses": max_presses,
        "bridge_generation": "press-v19",
        "local_bridge_source": str(DEFAULT_V19_BRIDGE),
        "remote_bridge": str(bridge),
        "signature_batch_prefix": signature_prefix,
        "scheduler_intent_label": intent_label,
        "required_remote_paths": required,
        "shards": shards,
        "task_specs": specs,
        "ordinary_submit_argv": submit_argv,
        "ordinary_submit_commands": [shlex.join(row) for row in submit_argv],
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl --file <PLAN.json> "
            f"--trusted --json --intent-label {shlex.quote(intent_label)}"
        ),
        "post_phase_reducer_command": _reducer_command(
            python=python, project=project, phase=phase, policy=policy,
            item_database=item_database,
        ),
        "phase_reducer_artifact": str(reducer),
        "automatic_result_pull": False,
        "manual_small_fetch_candidates": [
            *[row["summary"] for row in shards], str(reducer),
        ],
        "server_resident_case_artifacts": True,
        "full_press_lanes_retained": False,
        "raw_chronicle_csv_required": False,
        "checkpoint_required": False,
        "scheduler_guards_required": [
            "source artifact passes its current schema and nonempty phase gate",
            "no scheduler task already has an exact planned signature",
            "no active scheduler task writes a planned result directory",
            "no planned result directory or reducer artifact already exists",
            "this run and phase has zero pending scheduler escalations",
            "scheduler doctor reports ok before the write",
        ],
    }


def _test_remote_path(scheduler: Any, node: str, path: str, kind: str) -> bool:
    flag = {"file": "-f", "directory": "-d", "executable": "-x"}[kind]
    rc, _, _ = scheduler.run_on(
        node, f"test {flag} {shlex.quote(path)}", timeout=20, check=False,
    )
    return rc == 0


def _remote_source_gate_code(phase: str) -> str:
    """Return a remote-only validator that prints one compact gate receipt."""

    common = (
        "import json,sys;from pathlib import Path;"
        "a=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));"
    )
    if phase == PLAN_TRANSFER_PHASE:
        return common + (
            "from o2o_dps.factored_external_press_matrix_v1 import "
            "SPARSE_SHORTLIST_SCHEMA;"
            "from o2o_dps.factored_sparse_guard_learner_v2 import SCHEMA as LS;"
            "from o2o_dps.factored_sparse_guard_full_wave_v1 import "
            "validate_sparse_shortlist_v5;"
            "r=validate_sparse_shortlist_v5(a);n=sum(len(v) for v in r.values());"
            "assert n>0,'sparse shortlist has no guard worth full-wave transfer';"
            "print(json.dumps({'status':'SOURCE_GATE_CLEAR','phase':"
            f"'{PLAN_TRANSFER_PHASE}'"
            ",'schema':SPARSE_SHORTLIST_SCHEMA,'learner_schema':LS,"
            "'training_distinct_seed_count':a.get('training_distinct_seed_count'),"
            "'shortlisted_guard_count':n},separators=(',',':')))"
        )
    if phase == PLAN_FRESH_PHASE:
        return common + (
        "from o2o_dps.factored_sparse_guard_full_wave_v1 import "
        "AUTHORIZATION_SCHEMA,_authorized_route_guards;"
        "assert a.get('schema')==AUTHORIZATION_SCHEMA,'authorization schema differs';"
        "assert a.get('scope')=='MODEL_DEFINED_DEVELOPMENT_ONLY','scope differs';"
        "assert a.get('status')=='TRANSFER_AUTHORIZATION_COMPLETE_NONVOTING','status differs';"
        "assert a.get('raw_chronicle_rows_loaded') is False,'raw-data flag differs';"
        "assert a.get('voting_eligible') is False,'voting flag differs';"
        "assert a.get('deployment_eligible') is False,'deployment flag differs';"
        "assert a.get('authorization_gate')=='COMPLETE_BALANCED_SAME_SEED_EXPECTED_PAIRED_EFFECT;POSITIVE_LOWER_95_NORMAL_BOUND;POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY','effect gate differs';"
        "r=_authorized_route_guards(a);n=sum(len(v) for v in r.values());"
        "assert n>0,'authorization has no guard worth untouched fresh';"
        "print(json.dumps({'status':'SOURCE_GATE_CLEAR','phase':"
        f"'{PLAN_FRESH_PHASE}'"
        ",'schema':AUTHORIZATION_SCHEMA,'authorized_route_count':len(r),"
        "'authorized_guard_count':n},separators=(',',':')))"
        )
    if phase in LATCHED_SUBSET_ACTUAL_PLAN_PHASES:
        return common + (
            "from o2o_dps.factored_latched_subset_actual_full_wave_v1 import "
            "FROZEN_POLICY_SCHEMA,validate_frozen_latched_subset_actual_policy_v1;"
            "validate_frozen_latched_subset_actual_policy_v1(a);"
            "assert a.get('latched_subset_actual_full_wave_evaluated') is False,'actual evaluation flag differs';"
            "print(json.dumps({'status':'SOURCE_GATE_CLEAR','phase':"
            f"'{phase}'"
            ",'schema':FROZEN_POLICY_SCHEMA,'frozen_subset_policy_count':1,"
            "'latched_subset_actual_full_wave_evaluated':a.get('latched_subset_actual_full_wave_evaluated')},separators=(',',':')))"
        )
    if phase in LATCHED_PLAN_PHASES:
        return common + (
            "from o2o_dps.factored_latched_first_opportunity_full_wave_v1 import "
            "FROZEN_POLICY_SCHEMA,validate_frozen_latched_policy_v1;"
            "validate_frozen_latched_policy_v1(a);"
            "print(json.dumps({'status':'SOURCE_GATE_CLEAR','phase':"
            f"'{phase}'"
            ",'schema':FROZEN_POLICY_SCHEMA,'frozen_policy_count':1,"
            "'latched_policy_actual_full_wave_evaluated':a.get('latched_policy_actual_full_wave_evaluated')},separators=(',',':')))"
        )
    return common + (
        "from o2o_dps.factored_sparse_guard_post_transfer_refinement_v1 import "
        "SCHEMA,validate_post_transfer_refinement_v1;"
        "r=validate_post_transfer_refinement_v1(a);n=sum(len(v) for v in r.values());"
        "assert n==1,'refinement must freeze exactly one guard';"
        "assert a.get('status')=='DEVELOPMENT_REFINEMENT_FROZEN_FOR_UNTOUCHED_FRESH_NONVOTING','refinement status differs';"
        "assert a.get('scope')=='MODEL_DEFINED_DEVELOPMENT_ONLY','scope differs';"
        "assert a.get('voting_eligible') is False,'voting flag differs';"
        "assert a.get('deployment_eligible') is False,'deployment flag differs';"
        "print(json.dumps({'status':'SOURCE_GATE_CLEAR','phase':"
        f"'{PLAN_REFINEMENT_FRESH_PHASE}'"
        ",'schema':SCHEMA,'frozen_route_count':len(r),"
        "'frozen_guard_count':n},separators=(',',':')))"
    )


def _probe_remote_source_gate(
    scheduler: Any, plan: Mapping[str, Any], *, probe_node: str,
) -> dict[str, Any]:
    validator = shlex.join([
        str(plan["remote_python"]), "-c", _remote_source_gate_code(plan["phase"]),
        str(plan["policy_source"]),
    ])
    command = f"cd {shlex.quote(str(plan['project_root']))} && {validator}"
    rc, out, err = scheduler.run_on(
        probe_node, command, timeout=120, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote sparse source gate failed: {str(err or out)[-500:]}")
    try:
        receipt = json.loads(out)
    except json.JSONDecodeError as error:
        raise RuntimeError("remote sparse source gate did not return compact JSON") from error
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "SOURCE_GATE_CLEAR"
        or receipt.get("phase") != plan["phase"]
    ):
        raise RuntimeError("remote sparse source gate receipt differs")
    return receipt


def inventory_factored_sparse_guard_phase_v1(
    scheduler: Any, plan: Mapping[str, Any], *, probe_node: str = "node001",
) -> dict[str, Any]:
    """Refuse duplicate tasks/artifacts and validate the server source gate."""

    signatures = {row["signature"] for row in plan["task_specs"]}
    result_dirs = {row["result_dir"] for row in plan["shards"]}
    exact_task_conflicts = []
    active_output_conflicts = []
    with scheduler.state_lock(shared=True, purpose="sparse-full-wave-inventory"):
        state = scheduler.load_state()
        for task in state.get("tasks", []):
            signature = str(task.get("signature") or "")
            status = str(task.get("status") or "")
            command = str(task.get("cmd") or "")
            if signature in signatures:
                exact_task_conflicts.append({
                    "id": task.get("id"), "status": status, "signature": signature,
                })
            elif status in ACTIVE_STATUSES and any(path in command for path in result_dirs):
                active_output_conflicts.append({
                    "id": task.get("id"), "status": status, "signature": signature,
                })
    if exact_task_conflicts or active_output_conflicts:
        raise RuntimeError(
            "planned sparse task or output already exists: "
            + json.dumps({
                "exact_task_conflicts": exact_task_conflicts,
                "active_output_conflicts": active_output_conflicts,
            }, ensure_ascii=False)
        )

    missing = [
        row for row in plan["required_remote_paths"]
        if not _test_remote_path(scheduler, probe_node, row["path"], row["kind"])
    ]
    if missing:
        raise RuntimeError("staged compact closure is incomplete: " + json.dumps(missing))
    # The policy can be tens of MB.  Parse and validate it on shared storage;
    # only the compact schema/count receipt crosses the scheduler connection.
    source_gate = _probe_remote_source_gate(
        scheduler, plan, probe_node=probe_node,
    )

    artifact_conflicts = []
    for path, kind in [
        *[(row["result_dir"], "directory") for row in plan["shards"]],
        (plan["phase_reducer_artifact"], "file"),
    ]:
        if _test_remote_path(scheduler, probe_node, path, kind):
            artifact_conflicts.append(path)
    if artifact_conflicts:
        raise RuntimeError(
            "planned sparse phase already has remote artifacts: "
            + json.dumps(artifact_conflicts)
        )
    return {
        "schema": "factored_sparse_guard_remote_phase_inventory/v1",
        "status": "CLEAR_TO_SUBMIT_NOT_DISPATCHED",
        "exact_task_conflicts": [],
        "active_output_conflicts": [],
        "remote_artifact_conflicts": [],
        "required_path_count": len(plan["required_remote_paths"]),
        "source_gate": source_gate,
        "seed_namespace": {
            "module_phase": plan["module_phase"],
            "first": plan["seed_namespace_first"],
            "last": plan["seed_namespace_last"],
        },
        "shared_data_policy": "COMPACT_DERIVED_INPUTS_ONLY_NO_RAW_CSV_NO_CKPT",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=PHASES, default=PLAN_TRANSFER_PHASE)
    parser.add_argument("--sample-start", type=int, default=0)
    parser.add_argument("--samples-per-cell", type=int)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--ram-mb", type=int, default=DEFAULT_RAM_MB)
    parser.add_argument("--period-ms", type=int, default=DEFAULT_PERIOD_MS)
    parser.add_argument("--max-presses", type=int, default=DEFAULT_MAX_PRESSES)
    parser.add_argument("--probe-node", choices=NODE_NAMES, default="node001")
    parser.add_argument("--bridge-source", type=Path, default=DEFAULT_V19_BRIDGE)
    parser.add_argument(
        "--submit", action="store_true",
        help="after all guards pass, atomically queue six tasks; never dispatch",
    )
    args = parser.parse_args()

    bridge_source = args.bridge_source.expanduser().resolve(strict=True)
    if bridge_source.name != DEFAULT_V19_BRIDGE.name:
        parser.error("--bridge-source must name o2obridge.press-v19.linux-amd64")
    scheduler = _scheduler()
    shared_home, receipts = _shared_home(scheduler)
    plan = build_factored_sparse_guard_phase_plan_v1(
        shared_home=shared_home, run_id=args.run_id, phase=args.phase,
        sample_start=args.sample_start, samples_per_cell=args.samples_per_cell,
        workers=args.workers, ram_mb=args.ram_mb, period_ms=args.period_ms,
        max_presses=args.max_presses,
    )
    plan["shared_home_receipts"] = receipts
    plan["inventory"] = inventory_factored_sparse_guard_phase_v1(
        scheduler, plan, probe_node=args.probe_node,
    )
    plan["scheduler_escalations"] = scheduler_escalation_inventory_v1(plan)
    if args.submit:
        plan["scheduler_write_guards"] = check_scheduler_write_guards_v1(plan)
        plan["pre_submit_inventory"] = inventory_factored_sparse_guard_phase_v1(
            scheduler, plan, probe_node=args.probe_node,
        )
        plan["submission"] = submit_plan_atomically_v1(plan)
        plan["status"] = "SIX_TASKS_QUEUED_NOT_DISPATCHED"
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
