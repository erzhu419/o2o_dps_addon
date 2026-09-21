"""Plan, inventory, or queue one six-shard finite action-schedule search.

Planning is local-only and is the default.  ``--inventory`` performs read-only
remote checks.  ``--submit`` is the sole queue-writing mode; it atomically
adds six tasks and never dispatches them.  Exact cells, rather than seeds from
one adaptive search, are partitioned across the six shards.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    SCHEDULER_SKILL,
    _scheduler,
    _shared_home,
)
from scripts.factored_press_remote_submit_v1 import (
    check_scheduler_write_guards_v1,
    scheduler_escalation_inventory_v1,
    submit_plan_atomically_v1,
)
from o2o_dps.wave_action_sequence_remote_contract_v1 import (
    SHARD_COUNT,
    assign_exact_cell_shards_v1,
    load_exact_cell_manifest_v1,
)
from o2o_dps.causal_guard_v1 import observable_causal_guard_from_dict_v1
from scripts.wave_action_sequence_remote_stage_v1 import (
    DEVELOPMENT_CASE_BUILDER,
    OFFLINE_GUIDE_ARTIFACT,
    RUN_FAMILY,
    UPPER_KARA_CASE_BUILDER,
    compact_inputs_for_manifest_v1,
    remote_wave_action_sequence_input_paths_v1,
)


JSONMap = dict[str, Any]
ACTIVE_STATUSES = frozenset(("queued", "launching", "running"))
REPLAY_WORKERS = 64
RAM_MB = 65_536
DEFAULT_CONTINUATION_MAX_STEPS = 32
DEFAULT_MAX_STEPS = 16
DEFAULT_BEAM_WIDTH = 16
DEFAULT_MAX_EXPANSIONS_PER_NODE = 64
DEFAULT_MAX_OFF_GCD_ACTIONS = 3
DEFAULT_MAX_PREFIX_PERMUTATIONS = 8
DEFAULT_GUARD_GRID = "default"
GUARD_GRIDS = ("default", "none")
GOMAXPROCS_PER_BRIDGE = 1
WORKER_MODULE = "o2o_dps.wave_action_sequence_remote_worker_v1"
CPU_TRAINING_JUSTIFICATION = (
    "Independent fixed-seed simulator replays execute in isolated Go bridge "
    "subprocesses; exact cells are disjoint across six recoverable shards."
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PYTHON_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _valid_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run ID")
    return run_id


def _bridge_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != PurePosixPath(value).name
        or not value.endswith(".linux-amd64")
    ):
        raise ValueError("bridge_name must explicitly name one *.linux-amd64 file")
    return value


def _worker_module(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _PYTHON_MODULE.fullmatch(value)
        or not value.startswith("o2o_dps.")
    ):
        raise ValueError("worker_module must be a staged o2o_dps dotted module")
    return value


def _guard_grid(value: str) -> str:
    if not isinstance(value, str) or value not in GUARD_GRIDS:
        raise ValueError("guard_grid must be 'default' or 'none'")
    return value


def _guard_option_jsons(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ValueError(f"guard_option_json[{index}] must be JSON text")
        try:
            parsed = json.loads(value)
            guard = observable_causal_guard_from_dict_v1(
                parsed, label=f"guard_option_json[{index}]"
            )
            canonical = json.dumps(
                guard.to_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(
                f"guard_option_json[{index}] is invalid: {error}"
            ) from error
        if canonical in seen:
            raise ValueError("guard_option_json entries must be unique")
        seen.add(canonical)
        result.append(canonical)
    return tuple(result)


def _remote_layout(shared_home: str, run_id: str) -> tuple[PurePosixPath, PurePosixPath]:
    if not isinstance(shared_home, str) or not shared_home.startswith("/home/") or "\n" in shared_home:
        raise ValueError("shared_home must be one absolute remote home")
    run_root = PurePosixPath(shared_home) / "scheduleurm_work/o2o-dps-hpc/runs" / RUN_FAMILY / run_id
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    return run_root, project


def _coverage_receipt(shards: tuple[tuple[JSONMap, ...], ...]) -> JSONMap:
    ids_by_shard = [[row["cell_id"] for row in shard] for shard in shards]
    flattened = [cell_id for shard in ids_by_shard for cell_id in shard]
    counts = Counter(flattened)
    duplicates = sorted(cell_id for cell_id, count in counts.items() if count > 1)
    return {
        "schema": "wave_action_sequence_exact_cell_shard_coverage/v1",
        "shard_count": len(shards),
        "cell_count": len(flattened),
        "unique_cell_count": len(set(flattened)),
        "cell_ids_by_shard": ids_by_shard,
        "duplicate_cell_ids": duplicates,
        "all_shards_nonempty": all(bool(shard) for shard in shards),
        "disjoint": not duplicates,
        "full_coverage": len(flattened) == len(set(flattened)),
    }


def build_wave_action_sequence_remote_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    bridge_name: str,
    cell_manifest: str | Path,
    worker_module: str = WORKER_MODULE,
    continuation_max_steps: int = DEFAULT_CONTINUATION_MAX_STEPS,
    max_steps: int = DEFAULT_MAX_STEPS,
    beam_width: int = DEFAULT_BEAM_WIDTH,
    max_expansions_per_node: int = DEFAULT_MAX_EXPANSIONS_PER_NODE,
    max_off_gcd_actions: int = DEFAULT_MAX_OFF_GCD_ACTIONS,
    max_prefix_permutations: int = DEFAULT_MAX_PREFIX_PERMUTATIONS,
    guard_grid: str = DEFAULT_GUARD_GRID,
    guard_option_json: Sequence[str] = (),
    compact_inputs: tuple[str | Path, ...] | None = None,
    scheduler_cli: str | None = None,
) -> JSONMap:
    """Build six immutable task specs without scheduler or remote I/O."""

    _valid_run_id(run_id)
    bridge_name = _bridge_name(bridge_name)
    worker_module = _worker_module(worker_module)
    guard_grid = _guard_grid(guard_grid)
    normalized_guard_options = _guard_option_jsons(guard_option_json)
    if (
        isinstance(continuation_max_steps, bool)
        or not isinstance(continuation_max_steps, int)
        or continuation_max_steps < 0
    ):
        raise ValueError("continuation_max_steps must be a nonnegative integer")
    positive_search_values = {
        "max_steps": max_steps,
        "beam_width": beam_width,
        "max_expansions_per_node": max_expansions_per_node,
        "max_prefix_permutations": max_prefix_permutations,
    }
    for label, value in positive_search_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
    if (
        isinstance(max_off_gcd_actions, bool)
        or not isinstance(max_off_gcd_actions, int)
        or max_off_gcd_actions < 0
    ):
        raise ValueError("max_off_gcd_actions must be a nonnegative integer")
    manifest = load_exact_cell_manifest_v1(cell_manifest)
    compact = compact_inputs_for_manifest_v1(manifest, compact_inputs)
    case_builders = frozenset(
        str(row.get("case_builder") or DEVELOPMENT_CASE_BUILDER)
        for row in manifest["cells"]
    )
    shards = assign_exact_cell_shards_v1(manifest)
    coverage = _coverage_receipt(shards)
    if not (
        coverage["shard_count"] == SHARD_COUNT
        and coverage["all_shards_nonempty"]
        and coverage["disjoint"]
        and coverage["full_coverage"]
        and coverage["cell_count"] == len(manifest["cells"])
    ):
        raise AssertionError("exact-cell shard coverage contract failed")

    run_root, project = _remote_layout(shared_home, run_id)
    wowsims = run_root / "AddOns/BrainOfCat/wowsims-turtle"
    python = PurePosixPath(shared_home) / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    bridge = project / "bin" / bridge_name
    remote_manifest = project / "inputs/exact-cells.json"
    item_database = wowsims / "assets/database/db.json"
    runtime_inputs = remote_wave_action_sequence_input_paths_v1(project)
    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")
    signature_prefix = f"BrainOfCat/{RUN_FAMILY}-{run_id}"
    intent_label = f"BrainOfCat-{RUN_FAMILY}-{run_id}"

    specs: list[JSONMap] = []
    shard_rows: list[JSONMap] = []
    for shard_index, (preferred_node, cells) in enumerate(zip(NODE_NAMES, shards)):
        result_dir = project / f"results/search/shard-{shard_index:02d}"
        cell_output = result_dir / "cells"
        terminal = result_dir / "summary.json"
        training_seed_items = sum(len(row["seeds"]) for row in cells)
        evaluation_seed_items = sum(
            len(row.get("evaluation_seeds", ())) for row in cells
        )
        paired_seed_items = training_seed_items + evaluation_seed_items
        worker_argv = [
            "env",
            f"GOMAXPROCS={GOMAXPROCS_PER_BRIDGE}",
            str(python),
            "-m",
            worker_module,
            "--cell-manifest",
            str(remote_manifest),
            "--shard-index",
            str(shard_index),
            "--shard-count",
            str(SHARD_COUNT),
            "--bridge",
            str(bridge),
            "--bridge-cwd",
            str(wowsims),
            "--replay-workers",
            str(REPLAY_WORKERS),
            "--continuation-max-steps",
            str(continuation_max_steps),
            "--max-steps",
            str(max_steps),
            "--beam-width",
            str(beam_width),
            "--max-expansions-per-node",
            str(max_expansions_per_node),
            "--max-off-gcd-actions",
            str(max_off_gcd_actions),
            "--max-prefix-permutations",
            str(max_prefix_permutations),
            "--expert-guides",
            "all",
            "--guard-grid",
            guard_grid,
            "--runtime-binding",
            str(
                project
                / ".hpc-local/smokes/cat-gap-three-baseline-v1"
                / "deployed-contra-runtime-binding-v1.951b8faa.json"
            ),
            "--offline-guide-artifact",
            runtime_inputs["offline_guide_artifact"],
            "--output-dir",
            str(cell_output),
            "--summary",
            str(terminal),
        ]
        for guard_option in normalized_guard_options:
            worker_argv.extend(("--guard-option-json", guard_option))
        if UPPER_KARA_CASE_BUILDER in case_builders:
            worker_argv.extend(
                (
                    "--item-database",
                    str(item_database),
                    "--selector-manifest",
                    runtime_inputs["selector_manifest"],
                    "--selector-representatives",
                    runtime_inputs["selector_representatives"],
                    "--catalog-manifest",
                    runtime_inputs["catalog_manifest"],
                    "--catalog-data",
                    runtime_inputs["catalog_data"],
                )
            )
        batch_command = shlex.join(worker_argv)
        success_marker = (
            f"DONE wave_action_sequence_batch run_id={run_id} "
            f"shard={shard_index:02d}"
        )
        signature = f"{signature_prefix}/shard-{shard_index:02d}"
        description = (
            f"BrainOfCat finite action schedule shard "
            f"{shard_index + 1}/{SHARD_COUNT}"
        )
        spec = {
            "description": description,
            "cmd": (
                f"{batch_command} && printf '%s\\n' "
                f"{shlex.quote(success_marker)}"
            ),
            "cwd": str(project),
            "signature": signature,
            "project": "BrainOfCat",
            "resource_family": (
                "BrainOfCat/wave-action-sequence-v1/cpu-bridge-replay"
            ),
            "vram": 0,
            "ram_mb": RAM_MB,
            "cpu": REPLAY_WORKERS,
            "cpu_parallel_items": paired_seed_items,
            "cpu_parallel_total_items": paired_seed_items,
            "cpu_parallel_logical_items": paired_seed_items,
            "cpu_parallel_item_multiplier": 1,
            "cpu_parallel_start": 0,
            "cpu_parallel_end": paired_seed_items,
            "cpu_parallel_shard_index": shard_index,
            "cpu_parallel_num_shards": SHARD_COUNT,
            "cpu_batch_plan": {
                "workers": REPLAY_WORKERS,
                "physical_cores": REPLAY_WORKERS,
                "exact_cell_count": len(cells),
                "training_seed_items": training_seed_items,
                "held_out_evaluation_seed_items": evaluation_seed_items,
                "declared_seed_items": paired_seed_items,
                "gomaxprocs_per_bridge": GOMAXPROCS_PER_BRIDGE,
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
        shard_rows.append(
            {
                "shard_index": shard_index,
                "preferred_node": preferred_node,
                "cell_ids": [row["cell_id"] for row in cells],
                "cell_count": len(cells),
                "training_seed_items": training_seed_items,
                "held_out_evaluation_seed_items": evaluation_seed_items,
                "declared_seed_items": paired_seed_items,
                "result_dir": str(result_dir),
                "cell_output_dir": str(cell_output),
                "terminal_summary": str(terminal),
                "signature": signature,
            }
        )

    required = [
        {"path": str(project / "o2o_dps"), "kind": "directory"},
        {"path": str(project / "configs"), "kind": "directory"},
        {"path": str(remote_manifest), "kind": "file"},
        {"path": str(bridge), "kind": "file"},
        {"path": str(item_database), "kind": "file"},
        {
            "path": str(
                project
                / ".hpc-local/smokes/cat-gap-three-baseline-v1"
                / "deployed-contra-runtime-binding-v1.951b8faa.json"
            ),
            "kind": "file",
        },
        {
            "path": str(run_root / "AddOns/BrainOfCat/Contra_new"),
            "kind": "directory",
        },
        {
            "path": str(
                project
                / PurePosixPath(worker_module.replace(".", "/") + ".py")
            ),
            "kind": "file",
        },
    ]
    for source in compact:
        relative = source.resolve().relative_to(PROJECT_ROOT)
        required.append(
            {
                "path": str(project / PurePosixPath(relative.as_posix())),
                "kind": "file" if source.is_file() else "directory",
            }
        )
    if UPPER_KARA_CASE_BUILDER in case_builders:
        required.extend(
            {"path": runtime_inputs[key], "kind": "file"}
            for key in (
                "selector_manifest",
                "selector_representatives",
                "catalog_manifest",
                "catalog_data",
            )
        )

    return {
        "schema": "wave_action_sequence_remote_plan/v1",
        "status": "PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED",
        "run_id": run_id,
        "run_root": str(run_root),
        "project_root": str(project),
        "bridge": str(bridge),
        "bridge_generation": bridge_name,
        "worker_module": worker_module,
        "case_builders": sorted(case_builders),
        "compact_inputs": [str(path) for path in compact],
        "offline_guide_artifact": {
            "local": str(OFFLINE_GUIDE_ARTIFACT.resolve()),
            "remote": runtime_inputs["offline_guide_artifact"],
        },
        "upper_kara_compact_closure": (
            runtime_inputs
            if UPPER_KARA_CASE_BUILDER in case_builders
            else None
        ),
        "cell_manifest": str(remote_manifest),
        "local_cell_manifest": manifest["source_path"],
        "cell_count": len(manifest["cells"]),
        "shard_count": SHARD_COUNT,
        "replay_workers_per_shard": [REPLAY_WORKERS] * SHARD_COUNT,
        "cpu_per_shard": [REPLAY_WORKERS] * SHARD_COUNT,
        "ram_mb_per_shard": [RAM_MB] * SHARD_COUNT,
        "gomaxprocs_per_bridge": GOMAXPROCS_PER_BRIDGE,
        "continuation_max_steps": continuation_max_steps,
        "search_budget": {
            "max_steps": max_steps,
            "beam_width": beam_width,
            "max_expansions_per_node": max_expansions_per_node,
            "max_off_gcd_actions": max_off_gcd_actions,
            "max_prefix_permutations": max_prefix_permutations,
        },
        "causal_guard_configuration": {
            "guard_grid": guard_grid,
            "guard_option_json": [
                json.loads(row) for row in normalized_guard_options
            ],
            "resolution": (
                "EXPLICIT_OPTIONS_REPLACE_NAMED_GRID"
                if normalized_guard_options
                else f"NAMED_GRID_{guard_grid.upper()}"
            ),
            "manifest_cell_guard_options_may_override": True,
        },
        "shard_coverage": coverage,
        "shards": shard_rows,
        "task_specs": specs,
        "signature_batch_prefix": signature_prefix,
        "scheduler_intent_label": intent_label,
        "required_remote_paths": required,
        "manual_small_fetch_candidates": [
            row["terminal_summary"] for row in shard_rows
        ],
        "automatic_result_pull": False,
        "server_resident_cell_artifacts": True,
        "raw_offline_data_required": False,
        "checkpoint_required": False,
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl "
            "--file <PLAN.json> --trusted --json "
            f"--intent-label {shlex.quote(intent_label)}"
        ),
        "claim_boundary": {
            "adaptive_search_partition_unit": "EXACT_CELL_NOT_SEED_SUBSET",
            "same_cell_seed_sharding_for_search": False,
            "six_shards_disjoint_and_complete": True,
            "submit_performed": False,
            "dispatch_performed": False,
        },
    }


def _test_remote_path(
    scheduler: Any, node: str, path: str, kind: str
) -> bool:
    flag = "-f" if kind == "file" else "-d"
    rc, _, _ = scheduler.run_on(
        node,
        f"test {flag} {shlex.quote(path)}",
        timeout=20,
        check=False,
    )
    return rc == 0


def inventory_wave_action_sequence_remote_v1(
    scheduler: Any,
    plan: Mapping[str, Any],
    *,
    probe_node: str = "node001",
) -> JSONMap:
    """Refuse active writers, incomplete staging, and planned artifacts."""

    signatures = {row["signature"] for row in plan["task_specs"]}
    result_dirs = {row["result_dir"] for row in plan["shards"]}
    terminals = {row["terminal_summary"] for row in plan["shards"]}
    with scheduler.state_lock(
        shared=True, purpose="wave-action-sequence-active-inventory"
    ):
        state = scheduler.load_state()
        active_conflicts = []
        for task in state.get("tasks", []):
            if task.get("status") not in ACTIVE_STATUSES:
                continue
            command = str(task.get("cmd") or "")
            same_signature = task.get("signature") in signatures
            same_output = any(
                path in command for path in result_dirs | terminals
            )
            if same_signature or same_output:
                active_conflicts.append(
                    {
                        "id": task.get("id"),
                        "status": task.get("status"),
                        "signature": task.get("signature"),
                        "conflict": (
                            "planned_signature"
                            if same_signature
                            else "planned_remote_terminal_or_result"
                        ),
                    }
                )
    if active_conflicts:
        raise RuntimeError(
            "planned scheduler task or terminal writer already active: "
            + json.dumps(active_conflicts, ensure_ascii=False)
        )

    missing = [
        row
        for row in plan["required_remote_paths"]
        if not _test_remote_path(
            scheduler, probe_node, row["path"], row["kind"]
        )
    ]
    if missing:
        raise RuntimeError(
            "staged finite-schedule closure is incomplete: "
            + json.dumps(missing, ensure_ascii=False)
        )

    artifact_conflicts = []
    for path in sorted(result_dirs | terminals):
        if _test_remote_path(scheduler, probe_node, path, "directory") or _test_remote_path(
            scheduler, probe_node, path, "file"
        ):
            artifact_conflicts.append(path)
    if artifact_conflicts:
        raise RuntimeError(
            "planned finite-schedule terminal artifacts already exist: "
            + json.dumps(artifact_conflicts, ensure_ascii=False)
        )
    return {
        "schema": "wave_action_sequence_remote_inventory/v1",
        "status": "CLEAR_TO_SUBMIT_NOT_DISPATCHED",
        "active_signature_conflicts": [],
        "remote_artifact_conflicts": [],
        "required_path_count": len(plan["required_remote_paths"]),
        "remote_bridge": plan["bridge"],
        "cell_count": plan["cell_count"],
        "shard_coverage": plan["shard_coverage"],
        "shared_data_policy": "COMPACT_DERIVED_INPUTS_ONLY_NO_RAW_CSV_NO_CKPT",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bridge-name", required=True)
    parser.add_argument("--cell-manifest", type=Path, required=True)
    parser.add_argument("--worker-module", default=WORKER_MODULE)
    parser.add_argument("--compact-input", type=Path, action="append")
    parser.add_argument(
        "--continuation-max-steps",
        type=int,
        default=DEFAULT_CONTINUATION_MAX_STEPS,
        help="guide-continuation rollout depth; use 0 only for a local dry-run",
    )
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--beam-width", type=int, default=DEFAULT_BEAM_WIDTH)
    parser.add_argument(
        "--max-expansions-per-node",
        type=int,
        default=DEFAULT_MAX_EXPANSIONS_PER_NODE,
    )
    parser.add_argument(
        "--max-off-gcd-actions", type=int, default=DEFAULT_MAX_OFF_GCD_ACTIONS
    )
    parser.add_argument(
        "--max-prefix-permutations",
        type=int,
        default=DEFAULT_MAX_PREFIX_PERMUTATIONS,
    )
    parser.add_argument(
        "--guard-grid",
        choices=GUARD_GRIDS,
        default=DEFAULT_GUARD_GRID,
        help="named causal guard grid; explicit JSON options replace it",
    )
    parser.add_argument(
        "--guard-option-json",
        action="append",
        default=[],
        help=(
            "repeatable observable_causal_guard/v1 JSON object; when present, "
            "these options replace the named grid"
        ),
    )
    parser.add_argument("--shared-home", default="/home/zhengliang01")
    parser.add_argument("--probe-node", choices=NODE_NAMES, default="node001")
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="perform read-only staged-path and duplicate checks",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="after guards and a second inventory, atomically queue six tasks",
    )
    args = parser.parse_args()

    shared_home = args.shared_home
    scheduler = None
    receipts = None
    if args.inventory or args.submit:
        scheduler = _scheduler()
        discovered, receipts = _shared_home(scheduler)
        if discovered != shared_home:
            parser.error(
                f"--shared-home {shared_home!r} differs from discovered {discovered!r}"
            )
        shared_home = discovered
    plan = build_wave_action_sequence_remote_plan_v1(
        shared_home=shared_home,
        run_id=args.run_id,
        bridge_name=args.bridge_name,
        cell_manifest=args.cell_manifest,
        worker_module=args.worker_module,
        continuation_max_steps=args.continuation_max_steps,
        max_steps=args.max_steps,
        beam_width=args.beam_width,
        max_expansions_per_node=args.max_expansions_per_node,
        max_off_gcd_actions=args.max_off_gcd_actions,
        max_prefix_permutations=args.max_prefix_permutations,
        guard_grid=args.guard_grid,
        guard_option_json=tuple(args.guard_option_json),
        compact_inputs=(
            None
            if args.compact_input is None
            else tuple(args.compact_input)
        ),
    )
    if receipts is not None:
        plan["shared_home_receipts"] = receipts
    if scheduler is not None:
        plan["inventory"] = inventory_wave_action_sequence_remote_v1(
            scheduler, plan, probe_node=args.probe_node
        )
    if args.submit:
        plan["scheduler_escalations"] = scheduler_escalation_inventory_v1(plan)
        plan["scheduler_write_guards"] = check_scheduler_write_guards_v1(plan)
        plan["pre_submit_inventory"] = inventory_wave_action_sequence_remote_v1(
            scheduler, plan, probe_node=args.probe_node
        )
        plan["submission"] = submit_plan_atomically_v1(plan)
        plan["status"] = "SIX_TASKS_QUEUED_NOT_DISPATCHED"
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
