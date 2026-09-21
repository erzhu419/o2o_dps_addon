"""Stage and queue one six-node V8 attribution or reproduction panel.

Planning is local and read-only.  ``--stage`` copies only Python source, the
frozen contract/policy bundle, two character config files, one compact derived
scenario capsule, the runtime binding, and one Linux bridge into a fresh shared
run directory.  ``--submit`` atomically queues six scheduleurm tasks but never
dispatches them.  Seed artifacts stay server-side; only shard/final summaries
are named as manual fetch candidates.  No raw Chronicle CSV or checkpoint is
part of the closure.  The published E0 reproduction protocol additionally
stages its small authoritative summary and keeps all outputs outside the fresh
attribution result root.
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

from o2o_dps.development_wave_coverage_v1 import (
    DEFAULT_MANIFEST as DEFAULT_SCENARIO_CAPSULE,
)
from o2o_dps.upper_kara_v8_attribution_panel_v1 import (
    EXECUTION_MODES,
    load_v8_attribution_contract_v1,
)
from o2o_dps.upper_kara_v8_attribution_remote_worker_v1 import (
    validate_v8_attribution_inputs_v1,
)
from o2o_dps.upper_kara_v8_e0_reproduction_gate_v1 import (
    EXECUTION_MODE as REPRODUCTION_EXECUTION_MODE,
    V27_BRIDGE_FILENAME,
    load_v8_e0_reproduction_contract_v1,
    validate_published_summary_reference_v1,
    validate_v8_e0_reproduction_inputs_v1,
)
from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    SCHEDULER_SKILL,
    _scheduler,
    _shared_home,
)
from scripts.factored_press_remote_submit_v1 import check_scheduler_write_guards_v1


JSONMap = dict[str, Any]
RUN_FAMILY = "upper-kara-v8-attribution-v1"
REPRODUCTION_RUN_FAMILY = "upper-kara-v8-e0-reproduction-gate-v1"
SHARD_COUNT = 6
DEFAULT_SEED_WORKERS = 32
DEFAULT_REPRODUCTION_SEED_WORKERS = 43
DEFAULT_ARM_WORKERS = 3
NATIVE_LANES_PER_PAIRED_ARM = 2
DEFAULT_RAM_MB = 65_536
DEFAULT_CONTRACT = (
    PROJECT_ROOT / "configs/evaluation/upper_kara_v8_attribution_fresh_v1.json"
)
DEFAULT_REPRODUCTION_CONTRACT = (
    PROJECT_ROOT
    / "configs/evaluation/upper_kara_v8_e0_reproduction_gate_v1.json"
)
DEFAULT_POLICY_BUNDLE = (
    PROJECT_ROOT
    / "results/upper_kara_cat_action_plan_residual_remote_v8_256x256_v1"
    / "cat-action-plan-residual-20260920-v8"
    / "residual-bundle--contra_turtle_burst__rage.json"
)
DEFAULT_RUNTIME_BINDING = (
    PROJECT_ROOT
    / "results/responsive-team-v4"
    / "deployed-contra-runtime-binding-v1.951b8faa.json"
)
DEFAULT_PUBLISHED_SUMMARY = (
    PROJECT_ROOT
    / "results/upper_kara_cat_action_plan_residual_remote_v8_256x256_v1"
    / "cat-action-plan-residual-20260920-v8"
    / "summary.json"
)
DEFAULT_BRIDGE = (
    PROJECT_ROOT
    / "bin/o2obridge.seedfix-v27.precombat-press.withdb.goamd64v1.linux-amd64"
)
DEFAULT_LIVE_PROFILE = PROJECT_ROOT / "configs/wowsims/fury_warrior_live.json"
DEFAULT_EQUIPPED_NAMES = (
    PROJECT_ROOT / "configs/wowsims/fury_warrior_live.equipped_names.json"
)
CPU_JUSTIFICATION = (
    "Each seed runs three attribution arms concurrently and each paired arm "
    "runs exact-Cat and residual native bridges concurrently; GOMAXPROCS=1 "
    "limits every bridge to one CPU. Immutable per-seed JSON supports restart "
    "without a checkpoint."
)
REPRODUCTION_CPU_JUSTIFICATION = (
    "Each seed runs exact-Cat and frozen-V8 native bridges concurrently; "
    "GOMAXPROCS=1 limits every bridge to one CPU. Immutable per-seed JSON "
    "supports restart without a checkpoint."
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _file(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser().resolve(strict=True)
    if not value.is_file():
        raise ValueError(f"{label} must be a file")
    return value


def _mode_slug(execution_mode: str) -> str:
    if execution_mode == "E0_EVENT_DRIVEN":
        return "e0-event-driven"
    if execution_mode == "E1_EXTERNAL_PRESS_CLOCK":
        return "e1-external-press-clock"
    raise ValueError("execution mode is outside the frozen contract")


def build_v8_attribution_remote_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    execution_mode: str,
    contract: str | Path = DEFAULT_CONTRACT,
    policy_bundle: str | Path = DEFAULT_POLICY_BUNDLE,
    runtime_binding: str | Path = DEFAULT_RUNTIME_BINDING,
    bridge: str | Path = DEFAULT_BRIDGE,
    scenario_capsule: str | Path = DEFAULT_SCENARIO_CAPSULE,
    live_profile: str | Path = DEFAULT_LIVE_PROFILE,
    equipped_names: str | Path = DEFAULT_EQUIPPED_NAMES,
    seed_workers: int = DEFAULT_SEED_WORKERS,
    arm_workers: int = DEFAULT_ARM_WORKERS,
    ram_mb: int = DEFAULT_RAM_MB,
    scheduler_cli: str | None = None,
) -> JSONMap:
    """Build an exact stage/queue plan without remote I/O."""

    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run ID")
    if not isinstance(shared_home, str) or not shared_home.startswith("/home/"):
        raise ValueError("shared_home must be one absolute remote home")
    if "\n" in shared_home:
        raise ValueError("shared_home must be one absolute remote home")
    if type(seed_workers) is not int or seed_workers < 1:
        raise ValueError("seed_workers must be positive")
    if type(arm_workers) is not int or arm_workers != 3:
        raise ValueError("the frozen A0--A3 panel requires exactly 3 arm workers")
    if type(ram_mb) is not int or ram_mb < 1:
        raise ValueError("ram_mb must be positive")

    contract_path = _file(contract, "contract")
    parsed = load_v8_attribution_contract_v1(contract_path)
    if execution_mode not in parsed.execution_modes:
        raise ValueError("execution mode is outside the frozen contract")
    bundle_path = _file(policy_bundle, "policy_bundle")
    binding_path = _file(runtime_binding, "runtime_binding")
    bridge_path = _file(bridge, "bridge")
    if not bridge_path.name.endswith(".linux-amd64"):
        raise ValueError("bridge must explicitly name a Linux amd64 binary")
    capsule_path = _file(scenario_capsule, "scenario_capsule")
    profile_path = _file(live_profile, "live_profile")
    names_path = _file(equipped_names, "equipped_names")
    frozen_input_identity = validate_v8_attribution_inputs_v1(
        parsed,
        policy_bundle_path=bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=binding_path,
    )

    home = PurePosixPath(shared_home)
    run_root = home / "scheduleurm_work/o2o-dps-hpc/runs" / RUN_FAMILY / run_id
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    runtime_root = run_root / "runtime/wowsims"
    python = home / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    remote_contract = project / "inputs/attribution-contract.json"
    remote_bundle = project / "inputs/v8-parent-policy-bundle.json"
    remote_binding = project / "inputs/deployed-runtime-binding.json"
    remote_bridge = project / "bin" / bridge_path.name
    remote_capsule = (
        project
        / "offline_data/derived/fury_offline_scenario_capsules/v2"
        / capsule_path.name
    )
    remote_profile = project / "configs/wowsims/fury_warrior_live.json"
    remote_names = (
        project / "configs/wowsims/fury_warrior_live.equipped_names.json"
    )
    mode_slug = _mode_slug(execution_mode)
    result_root = project / "results/attribution" / mode_slug
    seed_root = result_root / "seeds"
    final_summary = result_root / "summary.json"
    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")

    copy_specs = [
        {
            "source": str((PROJECT_ROOT / "o2o_dps").resolve()),
            "destination": str(project / "o2o_dps"),
            "kind": "directory",
            "python_source_only": True,
        },
        *(
            {
                "source": str(source),
                "destination": str(destination),
                "kind": "file",
                "python_source_only": False,
            }
            for source, destination in (
                (contract_path, remote_contract),
                (bundle_path, remote_bundle),
                (binding_path, remote_binding),
                (bridge_path, remote_bridge),
                (capsule_path, remote_capsule),
                (profile_path, remote_profile),
                (names_path, remote_names),
            )
        ),
    ]

    task_specs: list[JSONMap] = []
    shards: list[JSONMap] = []
    signature_prefix = f"BrainOfCat/v8-attribution-{run_id}-{mode_slug}"
    intent_label = f"BrainOfCat-v8-attribution-{run_id}-{mode_slug}"
    for shard_index, preferred_node in enumerate(NODE_NAMES):
        assigned = len(range(shard_index, parsed.seed_count, SHARD_COUNT))
        workers = min(seed_workers, assigned)
        parallel_lanes_per_seed = arm_workers * NATIVE_LANES_PER_PAIRED_ARM
        cpu = workers * parallel_lanes_per_seed
        shard_summary = result_root / f"shard-{shard_index:02d}-summary.json"
        argv = [
            "env", "GOMAXPROCS=1",
            str(python), "-m",
            "o2o_dps.upper_kara_v8_attribution_remote_worker_v1",
            "run-shard",
            "--contract", str(remote_contract),
            "--policy-bundle", str(remote_bundle),
            "--shard-index", str(shard_index),
            "--shard-count", str(SHARD_COUNT),
            "--execution-mode", execution_mode,
            "--output-dir", str(seed_root),
            "--summary", str(shard_summary),
            "--bridge", str(remote_bridge),
            "--bridge-cwd", str(runtime_root),
            "--runtime-binding", str(remote_binding),
            "--seed-workers", str(workers),
            "--arm-workers", str(arm_workers),
        ]
        marker = f"DONE v8_attribution {mode_slug} shard={shard_index:02d}"
        spec: JSONMap = {
            "description": (
                f"BrainOfCat V8 attribution {mode_slug} "
                f"shard {shard_index + 1}/{SHARD_COUNT}"
            ),
            "cmd": f"{shlex.join(argv)} && printf '%s\\n' {shlex.quote(marker)}",
            "cwd": str(project),
            "signature": f"{signature_prefix}/shard-{shard_index:02d}",
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/v8-attribution/cpu-native-sim",
            "vram": 0,
            "ram_mb": ram_mb,
            "cpu": cpu,
            "cpu_parallel_items": assigned,
            "cpu_parallel_total_items": assigned,
            "cpu_parallel_logical_items": assigned,
            "cpu_parallel_item_multiplier": parallel_lanes_per_seed,
            "cpu_parallel_start": 0,
            "cpu_parallel_end": assigned,
            "cpu_parallel_shard_index": shard_index,
            "cpu_parallel_num_shards": SHARD_COUNT,
            "cpu_batch_plan": {
                "seed_workers": workers,
                "arm_workers_per_seed": arm_workers,
                "native_lanes_per_paired_arm": NATIVE_LANES_PER_PAIRED_ARM,
                "maximum_concurrent_arm_lanes": cpu,
                "assigned_seed_blocks": assigned,
                "waves": (assigned + workers - 1) // workers,
            },
            "priority": "normal",
            "preferred_node": preferred_node,
            "allowed_nodes": list(NODE_NAMES),
            "skip_launch_staging": True,
            "reroute_on_node_down": True,
            "node_down_requeue_s": 300,
            "allow_cpu_training": True,
            "cpu_training_justification": CPU_JUSTIFICATION,
            "allow_no_ckpt": True,
            "allow_no_resume": True,
            "env_spec": "none",
        }
        task_specs.append(spec)
        shards.append(
            {
                "shard_index": shard_index,
                "assigned_seed_count": assigned,
                "seed_workers": workers,
                "arm_workers_per_seed": arm_workers,
                "declared_cpu": cpu,
                "summary": str(shard_summary),
            }
        )

    reducer_argv = [
        str(python), "-m", "o2o_dps.upper_kara_v8_attribution_remote_worker_v1",
        "summarize", "--contract", str(remote_contract),
        "--input-root", str(seed_root), "--execution-mode", execution_mode,
        "--output", str(final_summary),
    ]
    required = [
        {"path": str(project / "o2o_dps"), "kind": "directory"},
        {"path": str(python), "kind": "executable"},
        {"path": str(remote_contract), "kind": "file"},
        {"path": str(remote_bundle), "kind": "file"},
        {"path": str(remote_binding), "kind": "file"},
        {"path": str(remote_bridge), "kind": "executable"},
        {"path": str(remote_capsule), "kind": "file"},
        {"path": str(remote_profile), "kind": "file"},
        {"path": str(remote_names), "kind": "file"},
        {"path": str(runtime_root), "kind": "directory"},
    ]
    return {
        "schema": "upper_kara_v8_attribution_remote_plan/v1",
        "status": "PLAN_ONLY_NOT_STAGED_NOT_SUBMITTED_NOT_DISPATCHED",
        "run_id": run_id,
        "execution_mode": execution_mode,
        "experiment_id": parsed.experiment_id,
        "frozen_input_identity": frozen_input_identity,
        "seed_count": parsed.seed_count,
        "shard_count": SHARD_COUNT,
        "run_root": str(run_root),
        "project_root": str(project),
        "result_root": str(result_root),
        "seed_output_root": str(seed_root),
        "final_summary": str(final_summary),
        "signature_batch_prefix": signature_prefix,
        "scheduler_intent_label": intent_label,
        "copy_specs": copy_specs,
        "required_remote_paths": required,
        "shards": shards,
        "task_specs": task_specs,
        "post_panel_summary_command": shlex.join(reducer_argv),
        "automatic_result_pull": False,
        "manual_small_fetch_candidates": [
            *(row["summary"] for row in shards),
            str(final_summary),
        ],
        "server_resident_seed_artifacts": True,
        "raw_chronicle_csv_staged": False,
        "checkpoint_staged": False,
        "third_party_project_tree_staged": False,
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl --file <PLAN.json> "
            f"--trusted --json --intent-label {shlex.quote(intent_label)}"
        ),
    }


def build_v8_e0_reproduction_remote_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    contract: str | Path = DEFAULT_REPRODUCTION_CONTRACT,
    published_summary: str | Path = DEFAULT_PUBLISHED_SUMMARY,
    policy_bundle: str | Path = DEFAULT_POLICY_BUNDLE,
    runtime_binding: str | Path = DEFAULT_RUNTIME_BINDING,
    bridge: str | Path = DEFAULT_BRIDGE,
    scenario_capsule: str | Path = DEFAULT_SCENARIO_CAPSULE,
    live_profile: str | Path = DEFAULT_LIVE_PROFILE,
    equipped_names: str | Path = DEFAULT_EQUIPPED_NAMES,
    seed_workers: int = DEFAULT_REPRODUCTION_SEED_WORKERS,
    ram_mb: int = DEFAULT_RAM_MB,
    scheduler_cli: str | None = None,
) -> JSONMap:
    """Build the six-node old-heldout E0 compatibility gate plan."""

    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run ID")
    if (
        not isinstance(shared_home, str)
        or not shared_home.startswith("/home/")
        or "\n" in shared_home
    ):
        raise ValueError("shared_home must be one absolute remote home")
    if type(seed_workers) is not int or seed_workers < 1:
        raise ValueError("seed_workers must be positive")
    if type(ram_mb) is not int or ram_mb < 1:
        raise ValueError("ram_mb must be positive")

    contract_path = _file(contract, "reproduction contract")
    parsed = load_v8_e0_reproduction_contract_v1(contract_path)
    summary_path = _file(published_summary, "published V8 summary")
    reference_receipt = validate_published_summary_reference_v1(
        parsed, summary_path
    )
    bundle_path = _file(policy_bundle, "policy_bundle")
    binding_path = _file(runtime_binding, "runtime_binding")
    bridge_path = _file(bridge, "bridge")
    if bridge_path.name != V27_BRIDGE_FILENAME:
        raise ValueError("published E0 reproduction requires the named v27 bridge")
    capsule_path = _file(scenario_capsule, "scenario_capsule")
    profile_path = _file(live_profile, "live_profile")
    names_path = _file(equipped_names, "equipped_names")
    frozen_input_identity = validate_v8_e0_reproduction_inputs_v1(
        parsed,
        policy_bundle_path=bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=binding_path,
    )

    home = PurePosixPath(shared_home)
    run_root = (
        home
        / "scheduleurm_work/o2o-dps-hpc/runs"
        / REPRODUCTION_RUN_FAMILY
        / run_id
    )
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    runtime_root = run_root / "runtime/wowsims"
    python = home / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    remote_contract = project / "inputs/e0-reproduction-contract.json"
    remote_published_summary = project / "inputs/published-v8-summary.json"
    remote_bundle = project / "inputs/v8-parent-policy-bundle.json"
    remote_binding = project / "inputs/deployed-runtime-binding.json"
    remote_bridge = project / "bin" / bridge_path.name
    remote_capsule = (
        project
        / "offline_data/derived/fury_offline_scenario_capsules/v2"
        / capsule_path.name
    )
    remote_profile = project / "configs/wowsims/fury_warrior_live.json"
    remote_names = (
        project / "configs/wowsims/fury_warrior_live.equipped_names.json"
    )
    result_root = project / "results/reproduction-gate/e0-published-heldout"
    seed_root = result_root / "seeds"
    final_summary = result_root / "summary.json"
    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")

    copy_specs = [
        {
            "source": str((PROJECT_ROOT / "o2o_dps").resolve()),
            "destination": str(project / "o2o_dps"),
            "kind": "directory",
            "python_source_only": True,
        },
        *(
            {
                "source": str(source),
                "destination": str(destination),
                "kind": "file",
                "python_source_only": False,
            }
            for source, destination in (
                (contract_path, remote_contract),
                (summary_path, remote_published_summary),
                (bundle_path, remote_bundle),
                (binding_path, remote_binding),
                (bridge_path, remote_bridge),
                (capsule_path, remote_capsule),
                (profile_path, remote_profile),
                (names_path, remote_names),
            )
        ),
    ]
    signature_prefix = f"BrainOfCat/v8-e0-reproduction-{run_id}"
    intent_label = f"BrainOfCat-v8-e0-reproduction-{run_id}"
    task_specs: list[JSONMap] = []
    shards: list[JSONMap] = []
    for shard_index, preferred_node in enumerate(NODE_NAMES):
        assigned = len(range(shard_index, parsed.seed_count, SHARD_COUNT))
        workers = min(seed_workers, assigned)
        cpu = workers * NATIVE_LANES_PER_PAIRED_ARM
        shard_summary = result_root / f"shard-{shard_index:02d}-summary.json"
        argv = [
            "env", "GOMAXPROCS=1",
            str(python), "-m", "o2o_dps.upper_kara_v8_e0_reproduction_gate_v1",
            "run-shard", "--contract", str(remote_contract),
            "--policy-bundle", str(remote_bundle),
            "--shard-index", str(shard_index),
            "--shard-count", str(SHARD_COUNT),
            "--output-dir", str(seed_root),
            "--summary", str(shard_summary),
            "--bridge", str(remote_bridge),
            "--bridge-cwd", str(runtime_root),
            "--runtime-binding", str(remote_binding),
            "--seed-workers", str(workers),
        ]
        marker = f"DONE v8_e0_reproduction shard={shard_index:02d}"
        spec: JSONMap = {
            "description": (
                "BrainOfCat published V8 E0 reproduction-only shard "
                f"{shard_index + 1}/{SHARD_COUNT}"
            ),
            "cmd": f"{shlex.join(argv)} && printf '%s\\n' {shlex.quote(marker)}",
            "cwd": str(project),
            "signature": f"{signature_prefix}/shard-{shard_index:02d}",
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/v8-e0-reproduction/cpu-native-sim",
            "vram": 0,
            "ram_mb": ram_mb,
            "cpu": cpu,
            "cpu_parallel_items": assigned,
            "cpu_parallel_total_items": assigned,
            "cpu_parallel_logical_items": assigned,
            "cpu_parallel_item_multiplier": NATIVE_LANES_PER_PAIRED_ARM,
            "cpu_parallel_start": 0,
            "cpu_parallel_end": assigned,
            "cpu_parallel_shard_index": shard_index,
            "cpu_parallel_num_shards": SHARD_COUNT,
            "cpu_batch_plan": {
                "seed_workers": workers,
                "paired_lanes_per_seed": 2,
                "paired_lanes_execute_concurrently": True,
                "maximum_concurrent_native_bridges": cpu,
                "assigned_seed_pairs": assigned,
                "waves": (assigned + workers - 1) // workers,
            },
            "priority": "normal",
            "preferred_node": preferred_node,
            "allowed_nodes": list(NODE_NAMES),
            "skip_launch_staging": True,
            "reroute_on_node_down": True,
            "node_down_requeue_s": 300,
            "allow_cpu_training": True,
            "cpu_training_justification": REPRODUCTION_CPU_JUSTIFICATION,
            "allow_no_ckpt": True,
            "allow_no_resume": True,
            "env_spec": "none",
        }
        task_specs.append(spec)
        shards.append({
            "shard_index": shard_index,
            "assigned_seed_count": assigned,
            "seed_workers": workers,
            "declared_cpu": cpu,
            "summary": str(shard_summary),
        })

    reducer = shlex.join([
        str(python), "-m", "o2o_dps.upper_kara_v8_e0_reproduction_gate_v1",
        "summarize", "--contract", str(remote_contract),
        "--published-summary", str(remote_published_summary),
        "--input-root", str(seed_root), "--output", str(final_summary),
    ])
    required = [
        {"path": str(project / "o2o_dps"), "kind": "directory"},
        {"path": str(python), "kind": "executable"},
        {"path": str(remote_contract), "kind": "file"},
        {"path": str(remote_published_summary), "kind": "file"},
        {"path": str(remote_bundle), "kind": "file"},
        {"path": str(remote_binding), "kind": "file"},
        {"path": str(remote_bridge), "kind": "executable"},
        {"path": str(remote_capsule), "kind": "file"},
        {"path": str(remote_profile), "kind": "file"},
        {"path": str(remote_names), "kind": "file"},
        {"path": str(runtime_root), "kind": "directory"},
    ]
    return {
        "schema": "upper_kara_v8_e0_reproduction_remote_plan/v1",
        "status": "PLAN_ONLY_NOT_STAGED_NOT_SUBMITTED_NOT_DISPATCHED",
        "scope": "REPRODUCTION_ONLY_NOT_FRESH",
        "fresh_evidence": False,
        "published_heldout_reused": True,
        "execution_mode": REPRODUCTION_EXECUTION_MODE,
        "bridge_generation": parsed.bridge_generation,
        "run_id": run_id,
        "experiment_id": parsed.experiment_id,
        "seed_count": parsed.seed_count,
        "shard_count": SHARD_COUNT,
        "run_root": str(run_root),
        "project_root": str(project),
        "result_root": str(result_root),
        "seed_output_root": str(seed_root),
        "final_summary": str(final_summary),
        "published_reference_validation": reference_receipt,
        "frozen_input_identity": frozen_input_identity,
        "remote_published_summary": str(remote_published_summary),
        "signature_batch_prefix": signature_prefix,
        "scheduler_intent_label": intent_label,
        "copy_specs": copy_specs,
        "required_remote_paths": required,
        "shards": shards,
        "task_specs": task_specs,
        "post_panel_summary_command": reducer,
        "automatic_result_pull": False,
        "manual_small_fetch_candidates": [
            *(row["summary"] for row in shards), str(final_summary)
        ],
        "server_resident_seed_artifacts": True,
        "raw_chronicle_csv_staged": False,
        "checkpoint_staged": False,
        "third_party_project_tree_staged": False,
        "results_mixed_with_fresh_attribution": False,
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl --file <PLAN.json> "
            f"--trusted --json --intent-label {shlex.quote(intent_label)}"
        ),
    }


def stage_v8_attribution_remote_plan_v1(
    scheduler: Any,
    plan: Mapping[str, Any],
    *,
    staging_node: str = "node001",
) -> JSONMap:
    """Materialize the compact closure into a fresh shared run directory."""

    if staging_node not in NODE_NAMES:
        raise ValueError("staging_node must be node001--node006")
    rc, _, error = scheduler.run_on(
        staging_node,
        f"test ! -e {shlex.quote(str(plan['run_root']))}",
        timeout=20,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote run already exists: {str(error)[-500:]}")
    directories = {
        str(PurePosixPath(str(plan["run_root"])) / "runtime/wowsims")
    }
    for spec in plan["copy_specs"]:
        destination = PurePosixPath(str(spec["destination"]))
        directories.add(
            str(destination if spec["kind"] == "directory" else destination.parent)
        )
    rc, _, error = scheduler.run_on(
        staging_node,
        "mkdir -p " + " ".join(shlex.quote(row) for row in sorted(directories)),
        timeout=30,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote directory creation failed: {str(error)[-500:]}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(staging_node)
    ssh_target = scheduler._ssh_target_for_node(staging_node)
    for spec in plan["copy_specs"]:
        command = ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms"]
        if spec.get("python_source_only"):
            command.extend(("--include=*/", "--include=*.py", "--exclude=*"))
        source = str(spec["source"]) + ("/" if spec["kind"] == "directory" else "")
        destination = str(spec["destination"]) + (
            "/" if spec["kind"] == "directory" else ""
        )
        command.extend(("-e", ssh_shell, source, f"{ssh_target}:{destination}"))
        subprocess.run(command, check=True, timeout=240)
    bridge = next(
        row["path"] for row in plan["required_remote_paths"]
        if row["kind"] == "executable" and "o2obridge" in row["path"]
    )
    rc, _, error = scheduler.run_on(
        staging_node, f"chmod u+x {shlex.quote(bridge)}", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge chmod failed: {str(error)[-500:]}")
    return {"status": "STAGED_NO_JOB_SUBMITTED", "staging_node": staging_node}


def submit_v8_attribution_plan_atomically_v1(
    plan: Mapping[str, Any], *, scheduler_cli: Path | None = None,
) -> JSONMap:
    """Queue exactly six trusted shard tasks; deliberately do not dispatch."""

    specs = list(plan.get("task_specs") or ())
    if len(specs) != SHARD_COUNT:
        raise ValueError("attribution submit must contain exactly six tasks")
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
                "--intent-label", str(plan["scheduler_intent_label"]),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(f"atomic scheduler submit failed: {completed.stderr[-1000:]}")
    result = json.loads(completed.stdout)
    if result.get("count") != SHARD_COUNT:
        raise RuntimeError(f"scheduler did not accept exactly six tasks: {result}")
    return {"status": "SIX_TASKS_QUEUED_NOT_DISPATCHED", "scheduler_result": result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--protocol",
        choices=("fresh-attribution", "published-e0-reproduction"),
        default="fresh-attribution",
    )
    parser.add_argument("--execution-mode", choices=EXECUTION_MODES)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--published-summary", type=Path, default=DEFAULT_PUBLISHED_SUMMARY)
    parser.add_argument("--policy-bundle", type=Path, default=DEFAULT_POLICY_BUNDLE)
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_RUNTIME_BINDING)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--seed-workers", type=int)
    parser.add_argument("--ram-mb", type=int, default=DEFAULT_RAM_MB)
    parser.add_argument("--staging-node", choices=NODE_NAMES, default="node001")
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if args.submit and not args.stage:
        parser.error("--submit requires --stage so the queued closure is fresh")

    scheduler = _scheduler()
    shared_home, receipts = _shared_home(scheduler)
    if args.protocol == "published-e0-reproduction":
        if args.execution_mode not in (None, REPRODUCTION_EXECUTION_MODE):
            parser.error("published reproduction is frozen to E0_EVENT_DRIVEN")
        plan = build_v8_e0_reproduction_remote_plan_v1(
            shared_home=shared_home,
            run_id=args.run_id,
            contract=args.contract or DEFAULT_REPRODUCTION_CONTRACT,
            published_summary=args.published_summary,
            policy_bundle=args.policy_bundle,
            runtime_binding=args.runtime_binding,
            bridge=args.bridge,
            seed_workers=(
                args.seed_workers
                if args.seed_workers is not None
                else DEFAULT_REPRODUCTION_SEED_WORKERS
            ),
            ram_mb=args.ram_mb,
        )
    else:
        if args.execution_mode is None:
            parser.error("fresh attribution requires --execution-mode")
        plan = build_v8_attribution_remote_plan_v1(
            shared_home=shared_home,
            run_id=args.run_id,
            execution_mode=args.execution_mode,
            contract=args.contract or DEFAULT_CONTRACT,
            policy_bundle=args.policy_bundle,
            runtime_binding=args.runtime_binding,
            bridge=args.bridge,
            seed_workers=(
                args.seed_workers
                if args.seed_workers is not None
                else DEFAULT_SEED_WORKERS
            ),
            ram_mb=args.ram_mb,
        )
    plan["shared_home_receipts"] = receipts
    if args.stage:
        plan["staging"] = stage_v8_attribution_remote_plan_v1(
            scheduler, plan, staging_node=args.staging_node,
        )
        plan["status"] = "STAGED_NO_JOB_SUBMITTED"
    if args.submit:
        plan["scheduler_write_guards"] = check_scheduler_write_guards_v1(plan)
        plan["submission"] = submit_v8_attribution_plan_atomically_v1(plan)
        plan["status"] = "SIX_TASKS_QUEUED_NOT_DISPATCHED"
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = (
    "CPU_JUSTIFICATION",
    "NODE_NAMES",
    "build_v8_e0_reproduction_remote_plan_v1",
    "build_v8_attribution_remote_plan_v1",
    "stage_v8_attribution_remote_plan_v1",
    "submit_v8_attribution_plan_atomically_v1",
)
