"""Plan, inventory, or queue one phase of the continuous two-wave campaign.

The phase boundary is explicit: every training shard must terminate before the
single freeze reducer is queued; the frozen artifact must exist before the
held-out shards are queued; their terminals must exist before the summary
reducer is queued.  Planning is the default and performs no remote I/O.
``--submit`` queues only the selected phase and never dispatches it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CatActionPlanResidualSequenceSearchV8,
    FixedParentCatHpGuardedSparseRoutingSearchV1,
    FixedParentQueueGcdBlockSearchV1,
    HeterogeneousTwoWaveSequenceSearchV7,
    TERMINAL_STATUSES,
    assign_evaluation_shards_v1,
    assign_teacher_plan_shards_v8,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
)
from o2o_dps.upper_kara_causal_program_remote_compact_v2 import (
    CANDIDATE_MANIFEST_SCHEMA_V2,
    TRAIN_TERMINAL_COMMIT_SCHEMA_V2,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    DEFAULT_REPLAY_WORKERS,
    EVAL_TERMINAL_SCHEMA,
    FREEZE_TERMINAL_SCHEMA,
    TRAIN_TERMINAL_SCHEMA,
    _campaign_contract_v1,
    evaluation_terminal_name_v1,
    train_terminal_name_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v2 import (
    candidate_manifest_name_v2,
    train_lane_sidecar_name_v2,
)
from o2o_dps.upper_kara_cat_action_plan_distiller_v8 import (
    SCHEMA as CAT_ACTION_PLAN_DISTILLATION_SCHEMA_V8,
)
from o2o_dps.upper_kara_cat_action_plan_remote_worker_v8 import (
    residual_policy_bundle_name_v8,
    teacher_terminal_name_v8,
)
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import (
    SCHEMA as CAT_ACTION_PLAN_TEACHER_SCHEMA_V8,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1,
)
from o2o_dps.upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1,
)
from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    SCHEDULER_SKILL,
    _scheduler,
    _shared_home,
)
from scripts.factored_press_remote_submit_v1 import (
    check_scheduler_write_guards_v1,
    scheduler_escalation_inventory_v1,
)
from scripts.upper_kara_causal_program_remote_stage_v1 import (
    REMOTE_OFFLINE_GUIDE_NAME,
    REMOTE_RUNTIME_BINDING_NAME,
    REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH,
    RUN_FAMILY,
    _remote_layout,
    _valid_run_id,
)


JSONMap = dict[str, Any]
PHASES = ("train", "freeze", "eval", "summarize")
PHASES_V2 = ("candidate", "train", "freeze", "eval", "summarize")
PHASES_V8 = ("teacher", "distill", "train", "freeze", "eval", "summarize")
TASK_CONFLICT_STATUSES = frozenset(("queued", "launching", "running", "done"))
REMOTE_OUTPUT_PATH_BATCH_SIZE = 128
TERMINAL_PROJECTION_KEYS = (
    "schema",
    "terminal_status",
    "campaign_id",
    "build_id",
    "campaign_contract",
    "loadout_id",
    "seed_shard_index",
    "examples",
    "winner",
    "selected_loadout_id",
    "frozen_program_id",
    "frozen_program_key",
    "frozen_program_origin",
    "program_count",
    "lane_count",
    "status",
    "master_seed",
    "simulator_seed",
    "exact_build_id",
    "remote_teacher_task",
)
WORKER_MODULE = "o2o_dps.upper_kara_causal_program_remote_worker_v1"
WORKER_MODULE_V2 = "o2o_dps.upper_kara_causal_program_remote_worker_v2"
WORKER_MODULE_V8 = "o2o_dps.upper_kara_cat_action_plan_remote_worker_v8"
# Native bridge replays run in Python threads.  The v3 measurement saturated
# on the GIL well below 48 workers while one 48-worker task peaked near 2.4 GiB.
# Four workers per task expose parallel bridge work without pretending that a
# task can use 48 cores.  Four GiB also leaves room for the larger 5--6 seed v4
# shard terminal while 32 train tasks fit below a 188 GiB node's RAM.
TRAIN_CPU = 4
EVAL_CPU = 4
TRAIN_RAM_MB = 4_096
EVAL_RAM_MB = 4_096
TEACHER_CPU = 2
TEACHER_RAM_MB = 4_096
DISTILL_CPU = 1
DISTILL_RAM_MB = 4_096
IMPORTED_REFERENCE_COUNT = 3
OVERLAY_VARIANTS_PER_SOURCE_UPPER_BOUND = (
    1 + len(DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1)
)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _task_spec_v1(
    *,
    description: str,
    argv: list[str],
    cwd: PurePosixPath,
    signature: str,
    preferred_node: str,
    cpu: int,
    ram_mb: int,
    logical_items: int,
    success_marker: str,
) -> JSONMap:
    return {
        "description": description,
        "cmd": (
            f"{shlex.join(argv)} && printf '%s\\n' "
            f"{shlex.quote(success_marker)}"
        ),
        "cwd": str(cwd),
        "signature": signature,
        "project": "BrainOfCat",
        "resource_family": "BrainOfCat/upper-kara-causal-program-v1/cpu-bridge-replay",
        "vram": 0,
        "ram_mb": ram_mb,
        "cpu": cpu,
        "cpu_parallel_items": logical_items,
        "cpu_parallel_total_items": logical_items,
        "cpu_parallel_logical_items": logical_items,
        "cpu_parallel_item_multiplier": 1,
        "cpu_parallel_start": 0,
        "cpu_parallel_end": logical_items,
        "priority": "normal",
        "preferred_node": preferred_node,
        "allowed_nodes": list(NODE_NAMES),
        "skip_launch_staging": True,
        "reroute_on_node_down": True,
        "node_down_requeue_s": 300,
        "allow_cpu_training": True,
        "cpu_training_justification": (
            "Independent continuous two-wave native bridge replays are "
            "partitioned by loadout and disjoint seed shard."
        ),
        "allow_no_ckpt": True,
        "allow_no_resume": True,
        "env_spec": "none",
    }


def build_upper_kara_causal_program_remote_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    phase: str,
    campaign: str | Path,
    bridge_name: str,
    teacher_workers: int = TEACHER_CPU,
    train_workers: int = TRAIN_CPU,
    eval_workers: int = EVAL_CPU,
    scheduler_cli: str | None = None,
    compact_v2: bool = False,
) -> JSONMap:
    _valid_run_id(run_id)
    _positive_int(teacher_workers, "teacher_workers")
    _positive_int(train_workers, "train_workers")
    _positive_int(eval_workers, "eval_workers")
    if (
        not isinstance(bridge_name, str)
        or bridge_name != PurePosixPath(bridge_name).name
        or not bridge_name.endswith(".linux-amd64")
    ):
        raise ValueError("bridge_name must explicitly name one *.linux-amd64 file")
    parsed = load_continuous_two_wave_remote_campaign_v1(campaign)
    if isinstance(
        parsed.search_spec, CatActionPlanResidualSequenceSearchV8
    ):
        return _build_upper_kara_cat_action_plan_remote_plan_v8(
            shared_home=shared_home,
            run_id=run_id,
            phase=phase,
            parsed=parsed,
            bridge_name=bridge_name,
            teacher_workers=teacher_workers,
            train_workers=train_workers,
            eval_workers=eval_workers,
            scheduler_cli=scheduler_cli,
        )
    phase_order = PHASES_V2 if compact_v2 else PHASES
    if phase not in phase_order:
        raise ValueError(f"phase must be one of {phase_order!r}")
    run_root, project = _remote_layout(shared_home, run_id)
    python = (
        PurePosixPath(shared_home)
        / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    )
    remote_campaign = project / "inputs/campaign.json"
    bridge = project / "bin" / bridge_name
    bridge_cwd = run_root / "runtime/wowsims"
    runtime_binding = project / "runtime" / REMOTE_RUNTIME_BINDING_NAME
    offline_guide = project / "runtime/guides" / REMOTE_OFFLINE_GUIDE_NAME
    scenario_capsule = project / REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH
    candidate_root = project / "results/candidates"
    train_root = project / "results/train"
    frozen = project / "results/freeze/frozen.json"
    eval_root = project / "results/eval"
    summary = project / "results/summary.json"
    contra_source_root = run_root / "AddOns/BrainOfCat/Contra_new"
    contra_manifest = (
        project / "configs/experts/contra260817_source_manifest_91baa120.json"
    )
    signature_prefix = f"BrainOfCat/{RUN_FAMILY}-{run_id}/{phase}"
    worker_module = WORKER_MODULE_V2 if compact_v2 else WORKER_MODULE
    specs: list[JSONMap] = []
    outputs: list[str] = []
    campaign_contract = _campaign_contract_v1(parsed)
    fixed_parent_block_search = isinstance(
        parsed.search_spec, FixedParentQueueGcdBlockSearchV1
    )
    hp_guarded_sparse_routing_search = isinstance(
        parsed.search_spec,
        FixedParentCatHpGuardedSparseRoutingSearchV1,
    )
    fixed_parent_burst_search = (
        fixed_parent_block_search or hp_guarded_sparse_routing_search
    )
    heterogeneous_sequence_search = isinstance(
        parsed.search_spec, HeterogeneousTwoWaveSequenceSearchV7
    )
    if heterogeneous_sequence_search:
        searched_candidate_upper_bound = parsed.search_spec.candidate_count
    elif fixed_parent_block_search:
        searched_candidate_upper_bound = parsed.search_spec.max_block_programs
    elif hp_guarded_sparse_routing_search:
        searched_candidate_upper_bound = (
            HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1
        )
    else:
        searched_candidate_upper_bound = (
            1
            + parsed.generation_config.max_programs
            * OVERLAY_VARIANTS_PER_SOURCE_UPPER_BOUND
        )
    evaluation_policy_count = 7 if fixed_parent_burst_search else 4
    semantic_prerequisites: list[JSONMap] = []
    prerequisites: list[JSONMap] = [
        {"path": str(project / "o2o_dps"), "kind": "directory"},
        {"path": str(remote_campaign), "kind": "file"},
        {"path": str(python), "kind": "executable"},
    ]

    if phase == "candidate":
        if not compact_v2:
            raise AssertionError("candidate phase requires compact_v2")
        prerequisites.extend(
            (
                {"path": str(bridge), "kind": "executable"},
                {"path": str(runtime_binding), "kind": "file"},
                {"path": str(offline_guide), "kind": "file"},
                {"path": str(contra_source_root), "kind": "directory"},
                {"path": str(contra_manifest), "kind": "file"},
                {
                    "path": str(bridge_cwd / "assets/database/db.json"),
                    "kind": "file",
                },
            )
        )
        if heterogeneous_sequence_search:
            prerequisites.append(
                {"path": str(scenario_capsule), "kind": "file"}
            )
        proposal_examples, _ = split_training_examples_for_selection_v1(parsed)
        for loadout_index, loadout_id in enumerate(parsed.loadout_ids):
            output = candidate_root / candidate_manifest_name_v2(loadout_id)
            logical_items = max(1, len(proposal_examples))
            assigned_workers = min(train_workers, logical_items)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                worker_module,
                "candidate",
                "--campaign",
                str(remote_campaign),
                "--loadout-id",
                loadout_id,
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--offline-guide-artifact",
                str(offline_guide),
                "--replay-workers",
                str(assigned_workers),
                "--output",
                str(output),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"generate candidate family {loadout_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{loadout_id}",
                    preferred_node=NODE_NAMES[loadout_index % len(NODE_NAMES)],
                    cpu=assigned_workers,
                    ram_mb=TRAIN_RAM_MB,
                    logical_items=logical_items,
                    success_marker=f"DONE causal_program_candidate {loadout_id}",
                )
            )
            outputs.append(str(output))
    elif phase == "train":
        prerequisites.extend(
            (
                {"path": str(bridge), "kind": "executable"},
                {"path": str(runtime_binding), "kind": "file"},
                {
                    "path": str(contra_source_root),
                    "kind": "directory",
                },
                {"path": str(contra_manifest), "kind": "file"},
                {
                    "path": str(bridge_cwd / "assets/database/db.json"),
                    "kind": "file",
                },
            )
        )
        if heterogeneous_sequence_search:
            prerequisites.append(
                {"path": str(scenario_capsule), "kind": "file"}
            )
        if compact_v2:
            for loadout_id in parsed.loadout_ids:
                candidate_manifest = (
                    candidate_root / candidate_manifest_name_v2(loadout_id)
                )
                prerequisites.append(
                    {"path": str(candidate_manifest), "kind": "file"}
                )
                semantic_prerequisites.append(
                    {
                        "role": "CANDIDATE_MANIFEST_V2",
                        "path": str(candidate_manifest),
                        "schema": CANDIDATE_MANIFEST_SCHEMA_V2,
                        "expected_fields": {"loadout_id": loadout_id},
                    }
                )
        else:
            prerequisites.append({"path": str(offline_guide), "kind": "file"})
        source_candidate_upper_bound = parsed.generation_config.max_programs
        # Each source program can project once without an attackable-time
        # threshold and once per configured threshold.  Projection may merge
        # semantic duplicates, so only this upper bound is knowable before the
        # native training frontier and proposal guides are materialized.
        candidate_count = (
            searched_candidate_upper_bound + IMPORTED_REFERENCE_COUNT
        )
        for shard in assign_training_shards_v1(parsed):
            output = train_root / train_terminal_name_v1(shard)
            sidecar = train_root / train_lane_sidecar_name_v2(shard)
            candidate_manifest = (
                candidate_root / candidate_manifest_name_v2(shard.loadout_id)
            )
            node = NODE_NAMES[shard.seed_shard_index % len(NODE_NAMES)]
            logical_items = len(shard.examples) * candidate_count
            assigned_workers = min(train_workers, logical_items)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                worker_module,
                "train",
                "--campaign",
                str(remote_campaign),
                "--loadout-id",
                shard.loadout_id,
                "--seed-shard-index",
                str(shard.seed_shard_index),
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--replay-workers",
                str(assigned_workers),
                "--output",
                str(output),
            ]
            if compact_v2:
                argv.extend(
                    [
                        "--candidate-manifest",
                        str(candidate_manifest),
                        "--lane-sidecar",
                        str(sidecar),
                    ]
                )
            else:
                argv.extend(
                    ["--offline-guide-artifact", str(offline_guide)]
                )
            specs.append(
                _task_spec_v1(
                    description=f"continuous two-wave train {shard.work_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{shard.work_id}",
                    preferred_node=node,
                    cpu=assigned_workers,
                    ram_mb=TRAIN_RAM_MB,
                    logical_items=logical_items,
                    success_marker=f"DONE causal_program_train {shard.work_id}",
                )
            )
            outputs.append(str(output))
            if compact_v2:
                outputs.append(str(sidecar))
    elif phase == "freeze":
        if compact_v2:
            for loadout_id in parsed.loadout_ids:
                candidate_manifest = (
                    candidate_root / candidate_manifest_name_v2(loadout_id)
                )
                prerequisites.append(
                    {"path": str(candidate_manifest), "kind": "file"}
                )
                semantic_prerequisites.append(
                    {
                        "role": "CANDIDATE_MANIFEST_V2",
                        "path": str(candidate_manifest),
                        "schema": CANDIDATE_MANIFEST_SCHEMA_V2,
                        "expected_fields": {"loadout_id": loadout_id},
                    }
                )
        for shard in assign_training_shards_v1(parsed):
            terminal = str(train_root / train_terminal_name_v1(shard))
            prerequisites.append({"path": terminal, "kind": "file"})
            if compact_v2:
                sidecar = str(train_root / train_lane_sidecar_name_v2(shard))
                prerequisites.append({"path": sidecar, "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": (
                        "TRAIN_COMMIT_V2" if compact_v2 else "TRAIN_SHARD"
                    ),
                    "path": terminal,
                    "schema": (
                        TRAIN_TERMINAL_COMMIT_SCHEMA_V2
                        if compact_v2
                        else TRAIN_TERMINAL_SCHEMA
                    ),
                    "allowed_terminal_statuses": sorted(TERMINAL_STATUSES),
                    "expected_fields": {
                        "loadout_id": shard.loadout_id,
                        "seed_shard_index": shard.seed_shard_index,
                        "examples": [row.to_dict() for row in shard.examples],
                    },
                }
            )
        argv = [
            str(python),
            "-m",
            worker_module,
            "freeze",
            "--campaign",
            str(remote_campaign),
            "--training-root",
            str(train_root),
            "--output",
            str(frozen),
        ]
        if compact_v2:
            argv.extend(["--candidate-root", str(candidate_root)])
        specs.append(
            _task_spec_v1(
                description="freeze continuous two-wave training winner",
                argv=argv,
                cwd=project,
                signature=f"{signature_prefix}/winner",
                preferred_node=NODE_NAMES[0],
                cpu=1,
                ram_mb=4_096,
                logical_items=len(assign_training_shards_v1(parsed)),
                success_marker="DONE causal_program_freeze",
            )
        )
        outputs.append(str(frozen))
    elif phase == "eval":
        prerequisites.extend(
            (
                {"path": str(frozen), "kind": "file"},
                {"path": str(bridge), "kind": "executable"},
                {"path": str(runtime_binding), "kind": "file"},
                {
                    "path": str(contra_source_root),
                    "kind": "directory",
                },
                {"path": str(contra_manifest), "kind": "file"},
                {
                    "path": str(bridge_cwd / "assets/database/db.json"),
                    "kind": "file",
                },
            )
        )
        if heterogeneous_sequence_search:
            prerequisites.append(
                {"path": str(scenario_capsule), "kind": "file"}
            )
        semantic_prerequisites.append(
            {
                "role": "FROZEN_SEARCHED_WINNER",
                "path": str(frozen),
                "schema": FREEZE_TERMINAL_SCHEMA,
                "allowed_terminal_statuses": ["COMPLETE"],
                "expected_fields": {},
            }
        )
        for shard in assign_evaluation_shards_v1(parsed):
            output = eval_root / evaluation_terminal_name_v1(shard)
            node = NODE_NAMES[shard.seed_shard_index % len(NODE_NAMES)]
            logical_items = len(shard.examples) * evaluation_policy_count
            assigned_workers = min(eval_workers, logical_items)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                worker_module,
                "eval",
                "--campaign",
                str(remote_campaign),
                "--frozen",
                str(frozen),
                "--seed-shard-index",
                str(shard.seed_shard_index),
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--replay-workers",
                str(assigned_workers),
                "--output",
                str(output),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"held-out continuous two-wave {shard.work_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{shard.work_id}",
                    preferred_node=node,
                    cpu=assigned_workers,
                    ram_mb=EVAL_RAM_MB,
                    logical_items=logical_items,
                    success_marker=f"DONE causal_program_eval {shard.work_id}",
                )
            )
            outputs.append(str(output))
    else:
        prerequisites.append({"path": str(frozen), "kind": "file"})
        semantic_prerequisites.append(
            {
                "role": "FROZEN_SEARCHED_WINNER",
                "path": str(frozen),
                "schema": FREEZE_TERMINAL_SCHEMA,
                "allowed_terminal_statuses": ["COMPLETE"],
                "expected_fields": {},
            }
        )
        for shard in assign_evaluation_shards_v1(parsed):
            terminal = str(eval_root / evaluation_terminal_name_v1(shard))
            prerequisites.append({"path": terminal, "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": "EVAL_SHARD",
                    "path": terminal,
                    "schema": EVAL_TERMINAL_SCHEMA,
                    # A valid failed/invalid held-out shard must still reach
                    # the summary reducer so the conclusion remains null
                    # rather than silently disappearing.
                    "allowed_terminal_statuses": sorted(TERMINAL_STATUSES),
                    "expected_fields": {
                        "seed_shard_index": shard.seed_shard_index,
                        "examples": [row.to_dict() for row in shard.examples],
                    },
                }
            )
        argv = [
            str(python),
            "-m",
            worker_module,
            "summarize",
            "--campaign",
            str(remote_campaign),
            "--frozen",
            str(frozen),
            "--evaluation-root",
            str(eval_root),
            "--output",
            str(summary),
        ]
        specs.append(
            _task_spec_v1(
                description="summarize held-out continuous two-wave panel",
                argv=argv,
                cwd=project,
                signature=f"{signature_prefix}/summary",
                preferred_node=NODE_NAMES[0],
                cpu=1,
                ram_mb=4_096,
                logical_items=(
                    len(parsed.evaluation_examples) * evaluation_policy_count
                ),
                success_marker="DONE causal_program_summary",
            )
        )
        outputs.append(str(summary))

    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")
    return {
        "schema": (
            "upper_kara_causal_program_remote_plan/v2"
            if compact_v2
            else "upper_kara_causal_program_remote_plan/v1"
        ),
        "status": "PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED",
        "run_id": run_id,
        "phase": phase,
        "campaign_id": parsed.campaign_id,
        "build_id": parsed.build_id,
        "campaign_contract": campaign_contract,
        "run_root": str(run_root),
        "project_root": str(project),
        "remote_python": str(python),
        "task_count": len(specs),
        "task_specs": specs,
        "required_remote_paths": prerequisites,
        "semantic_prerequisites": semantic_prerequisites,
        "terminal_outputs": [
            path for path in outputs if not path.endswith(".lanes.json")
        ],
        "artifact_outputs": outputs,
        "scheduler_intent_label": f"BrainOfCat-{RUN_FAMILY}-{run_id}-{phase}",
        "signature_batch_prefix": f"BrainOfCat/{RUN_FAMILY}-{run_id}",
        "manual_small_fetch_candidates": outputs if phase in {"freeze", "summarize"} else [],
        "raw_offline_data_required": False,
        "derived_scenario_capsule_required": (
            heterogeneous_sequence_search
            and phase in {"candidate", "train", "eval"}
        ),
        "compact_offline_guide_required": phase == (
            "candidate" if compact_v2 else "train"
        ),
        "external_project_directory_required": False,
        "external_source_code_closure_required": phase in (
            {"candidate", "train", "eval"}
            if compact_v2
            else {"train", "eval"}
        ),
        "automatic_result_pull": False,
        "candidate_count_audit": {
            "source_program_upper_bound": (
                parsed.generation_config.max_programs
            ),
            "searched_cat_residual_upper_bound": (
                None
                if fixed_parent_burst_search or heterogeneous_sequence_search
                else searched_candidate_upper_bound
            ),
            "searched_heterogeneous_sequence_count": (
                searched_candidate_upper_bound
                if heterogeneous_sequence_search
                else None
            ),
            "searched_fixed_parent_block_upper_bound": (
                searched_candidate_upper_bound
                if fixed_parent_block_search
                else None
            ),
            "searched_hp_guarded_sparse_routing_count": (
                searched_candidate_upper_bound
                if hp_guarded_sparse_routing_search
                else None
            ),
            "zero_residual_count": 1,
            "projection_variants_per_source_upper_bound": (
                None
                if fixed_parent_block_search
                else OVERLAY_VARIANTS_PER_SOURCE_UPPER_BOUND
            ),
            "fixed_parent_burst_program_held_constant": (
                fixed_parent_burst_search
            ),
            "explicit_no_queue_mode_in_block_space": (
                fixed_parent_block_search
            ),
            "imported_reference_count": IMPORTED_REFERENCE_COUNT,
            "total_training_program_upper_bound": (
                searched_candidate_upper_bound + IMPORTED_REFERENCE_COUNT
            ),
            "exact_unique_count_known_at_plan_time": (
                hp_guarded_sparse_routing_search
                or heterogeneous_sequence_search
            ),
            "projection_may_deduplicate": (
                not (
                    hp_guarded_sparse_routing_search
                    or heterogeneous_sequence_search
                )
            ),
        },
        "resource_model": {
            "train_replay_workers_per_task_default": TRAIN_CPU,
            "evaluation_replay_workers_per_task_default": EVAL_CPU,
            "train_ram_mb_per_task": TRAIN_RAM_MB,
            "evaluation_ram_mb_per_task": EVAL_RAM_MB,
            "v3_observed_peak_ram_mb_per_48_worker_task_approx": 2_400,
            "python_thread_pool_is_gil_limited": True,
            "gomaxprocs_per_bridge": 1,
        },
        "phase_logical_item_count": sum(
            row["cpu_parallel_logical_items"] for row in specs
        ),
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl "
            "--file <TASK_SPECS.json> --trusted --json "
            f"--intent-label {shlex.quote(f'BrainOfCat-{RUN_FAMILY}-{run_id}-{phase}')}"
        ),
        "phase_contract": {
            "order": list(phase_order),
            "only_selected_phase_is_planned": True,
            "prerequisite_artifacts_required_before_queue_write": True,
            "training_partition_unit": (
                "LOADOUT_AND_SEED_SHARD_REPLAY_ONLY"
                if compact_v2
                else "LOADOUT_AND_SEED_SHARD"
            ),
            "candidate_generation_phase_task_count": (
                len(parsed.loadout_ids) if compact_v2 else None
            ),
            "candidate_generation_once_per_loadout": compact_v2,
            "training_shards_may_generate_candidates": False if compact_v2 else None,
            "compact_lane_sidecar_then_terminal_commit": compact_v2,
            "heldout_search_or_generation": False,
            "legacy_independent_48_cell_semantics": False,
            "submit_performed": False,
            "dispatch_performed": False,
        },
    }


def _build_upper_kara_cat_action_plan_remote_plan_v8(
    *,
    shared_home: str,
    run_id: str,
    phase: str,
    parsed: Any,
    bridge_name: str,
    teacher_workers: int,
    train_workers: int,
    eval_workers: int,
    scheduler_cli: str | None,
) -> JSONMap:
    """Plan exactly one phase of the frozen six-stage V8 workflow."""

    if phase not in PHASES_V8:
        raise ValueError(f"phase must be one of {PHASES_V8!r}")
    spec = parsed.search_spec
    if not isinstance(spec, CatActionPlanResidualSequenceSearchV8):
        raise TypeError("parsed campaign is not a V8 Cat action-plan campaign")
    run_root, project = _remote_layout(shared_home, run_id)
    python = (
        PurePosixPath(shared_home)
        / "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    )
    remote_campaign = project / "inputs/campaign.json"
    bridge = project / "bin" / bridge_name
    bridge_cwd = run_root / "runtime/wowsims"
    runtime_binding = project / "runtime" / REMOTE_RUNTIME_BINDING_NAME
    scenario_capsule = project / REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH
    teacher_root = project / "results/teacher"
    candidate_root = project / "results/candidates"
    policy_bundle_root = project / "results/policies"
    train_root = project / "results/train"
    frozen = project / "results/freeze/frozen.json"
    eval_root = project / "results/eval"
    summary = project / "results/summary.json"
    contra_source_root = run_root / "AddOns/BrainOfCat/Contra_new"
    contra_manifest = (
        project / "configs/experts/contra260817_source_manifest_91baa120.json"
    )
    signature_prefix = f"BrainOfCat/{RUN_FAMILY}-{run_id}/{phase}"
    campaign_contract = _campaign_contract_v1(parsed)
    teacher_shards = assign_teacher_plan_shards_v8(parsed)
    training_shards = assign_training_shards_v1(parsed)
    evaluation_shards = assign_evaluation_shards_v1(parsed)
    candidate_upper_bound = (
        IMPORTED_REFERENCE_COUNT + spec.max_distilled_candidates_per_loadout
    )
    specs: list[JSONMap] = []
    outputs: list[str] = []
    terminal_outputs: list[str] = []
    semantic_prerequisites: list[JSONMap] = []
    prerequisites: list[JSONMap] = [
        {"path": str(project / "o2o_dps"), "kind": "directory"},
        {"path": str(remote_campaign), "kind": "file"},
        {"path": str(python), "kind": "executable"},
    ]

    native_prerequisites = (
        {"path": str(bridge), "kind": "executable"},
        {"path": str(runtime_binding), "kind": "file"},
        {"path": str(bridge_cwd / "assets/database/db.json"), "kind": "file"},
        {"path": str(scenario_capsule), "kind": "file"},
        {"path": str(contra_source_root), "kind": "directory"},
        {"path": str(contra_manifest), "kind": "file"},
    )

    if phase == "teacher":
        prerequisites.extend(native_prerequisites)
        for task_index, shard in enumerate(teacher_shards):
            output = teacher_root / teacher_terminal_name_v8(shard)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                WORKER_MODULE_V8,
                "teacher",
                "--campaign",
                str(remote_campaign),
                "--loadout-id",
                shard.loadout_id,
                "--teacher-shard-index",
                str(shard.teacher_shard_index),
                "--teacher-workers",
                str(teacher_workers),
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--output",
                str(output),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"Cat action-plan teacher {shard.work_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{shard.work_id}",
                    preferred_node=NODE_NAMES[task_index % len(NODE_NAMES)],
                    cpu=teacher_workers,
                    ram_mb=TEACHER_RAM_MB,
                    logical_items=(
                        shard.teacher_max_states
                        * shard.teacher_plan_shard_size
                    ),
                    success_marker=f"DONE cat_action_plan_teacher {shard.work_id}",
                )
            )
            outputs.append(str(output))
            terminal_outputs.append(str(output))
    elif phase == "distill":
        for shard in teacher_shards:
            teacher_output = teacher_root / teacher_terminal_name_v8(shard)
            prerequisites.append({"path": str(teacher_output), "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": "V8_TEACHER_RESULT",
                    "path": str(teacher_output),
                    "schema": CAT_ACTION_PLAN_TEACHER_SCHEMA_V8,
                    "allowed_statuses": [
                        "BASELINE_INCOMPLETE_NO_BRANCHES_SCORED",
                        "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING",
                    ],
                    "expected_fields": {
                        "build_id": parsed.build_id,
                        "loadout_id": shard.loadout_id,
                        "master_seed": shard.example.seed,
                    },
                    "expected_nested_fields": {
                        "remote_teacher_task": {
                            "campaign_id": parsed.campaign_id,
                            "work_id": shard.work_id,
                            "teacher_shard_index": shard.teacher_shard_index,
                            "first_wave_arrival_ms": (
                                shard.example.first_wave_arrival_ms
                            ),
                            "proposal_cohort_only": True,
                            "selection_or_heldout_seed_used": False,
                        }
                    },
                }
            )
        for loadout_index, loadout_id in enumerate(parsed.loadout_ids):
            manifest = candidate_root / candidate_manifest_name_v2(loadout_id)
            bundle = policy_bundle_root / residual_policy_bundle_name_v8(loadout_id)
            argv = [
                str(python),
                "-m",
                WORKER_MODULE_V8,
                "candidate",
                "--campaign",
                str(remote_campaign),
                "--teacher-root",
                str(teacher_root),
                "--loadout-id",
                loadout_id,
                "--policy-bundle",
                str(bundle),
                "--output",
                str(manifest),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"distill Cat action-plan labels {loadout_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{loadout_id}",
                    preferred_node=NODE_NAMES[loadout_index % len(NODE_NAMES)],
                    cpu=DISTILL_CPU,
                    ram_mb=DISTILL_RAM_MB,
                    logical_items=len(teacher_shards) // len(parsed.loadout_ids),
                    success_marker=f"DONE cat_action_plan_distill {loadout_id}",
                )
            )
            outputs.extend((str(bundle), str(manifest)))
            # The manifest is written after the bundle and is the phase commit.
            terminal_outputs.append(str(manifest))
    elif phase == "train":
        prerequisites.extend(native_prerequisites)
        for loadout_id in parsed.loadout_ids:
            manifest = candidate_root / candidate_manifest_name_v2(loadout_id)
            bundle = policy_bundle_root / residual_policy_bundle_name_v8(loadout_id)
            prerequisites.extend(
                (
                    {"path": str(manifest), "kind": "file"},
                    {"path": str(bundle), "kind": "file"},
                )
            )
            semantic_prerequisites.extend(
                (
                    {
                        "role": "CANDIDATE_MANIFEST_V2",
                        "path": str(manifest),
                        "schema": CANDIDATE_MANIFEST_SCHEMA_V2,
                        "expected_fields": {"loadout_id": loadout_id},
                    },
                    {
                        "role": "V8_RESIDUAL_POLICY_BUNDLE",
                        "path": str(bundle),
                        "schema": CAT_ACTION_PLAN_DISTILLATION_SCHEMA_V8,
                        "allowed_statuses": [
                            "EXACT_CAT_AND_PROPOSAL_CANDIDATES_FROZEN_FOR_FRESH_TEST",
                            "EXACT_CAT_ONLY_NO_ELIGIBLE_PROPOSAL_CELL",
                        ],
                        "expected_fields": {
                            "loadout_id": loadout_id,
                            "exact_build_id": parsed.build_id,
                        },
                    },
                )
            )
        for task_index, shard in enumerate(training_shards):
            output = train_root / train_terminal_name_v1(shard)
            sidecar = train_root / train_lane_sidecar_name_v2(shard)
            manifest = candidate_root / candidate_manifest_name_v2(shard.loadout_id)
            bundle = policy_bundle_root / residual_policy_bundle_name_v8(
                shard.loadout_id
            )
            logical_items = len(shard.examples) * candidate_upper_bound
            assigned_workers = min(train_workers, logical_items)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                WORKER_MODULE_V8,
                "train",
                "--campaign",
                str(remote_campaign),
                "--candidate-manifest",
                str(manifest),
                "--policy-bundle",
                str(bundle),
                "--loadout-id",
                shard.loadout_id,
                "--seed-shard-index",
                str(shard.seed_shard_index),
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--lane-sidecar",
                str(sidecar),
                "--replay-workers",
                str(assigned_workers),
                "--output",
                str(output),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"fresh V8 train {shard.work_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{shard.work_id}",
                    preferred_node=NODE_NAMES[task_index % len(NODE_NAMES)],
                    cpu=assigned_workers,
                    ram_mb=TRAIN_RAM_MB,
                    logical_items=logical_items,
                    success_marker=f"DONE cat_action_plan_train {shard.work_id}",
                )
            )
            outputs.extend((str(output), str(sidecar)))
            terminal_outputs.append(str(output))
    elif phase == "freeze":
        for loadout_id in parsed.loadout_ids:
            manifest = candidate_root / candidate_manifest_name_v2(loadout_id)
            prerequisites.append({"path": str(manifest), "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": "CANDIDATE_MANIFEST_V2",
                    "path": str(manifest),
                    "schema": CANDIDATE_MANIFEST_SCHEMA_V2,
                    "expected_fields": {"loadout_id": loadout_id},
                }
            )
        for shard in training_shards:
            terminal = train_root / train_terminal_name_v1(shard)
            sidecar = train_root / train_lane_sidecar_name_v2(shard)
            prerequisites.extend(
                (
                    {"path": str(terminal), "kind": "file"},
                    {"path": str(sidecar), "kind": "file"},
                )
            )
            semantic_prerequisites.append(
                {
                    "role": "TRAIN_COMMIT_V2",
                    "path": str(terminal),
                    "schema": TRAIN_TERMINAL_COMMIT_SCHEMA_V2,
                    "allowed_terminal_statuses": sorted(TERMINAL_STATUSES),
                    "expected_fields": {
                        "loadout_id": shard.loadout_id,
                        "seed_shard_index": shard.seed_shard_index,
                        "examples": [row.to_dict() for row in shard.examples],
                    },
                }
            )
        argv = [
            str(python),
            "-m",
            WORKER_MODULE_V8,
            "freeze",
            "--campaign",
            str(remote_campaign),
            "--candidate-root",
            str(candidate_root),
            "--training-root",
            str(train_root),
            "--output",
            str(frozen),
        ]
        specs.append(
            _task_spec_v1(
                description="freeze fresh V8 selection winner",
                argv=argv,
                cwd=project,
                signature=f"{signature_prefix}/winner",
                preferred_node=NODE_NAMES[0],
                cpu=1,
                ram_mb=4_096,
                logical_items=len(training_shards),
                success_marker="DONE cat_action_plan_freeze",
            )
        )
        outputs.append(str(frozen))
        terminal_outputs.append(str(frozen))
    elif phase == "eval":
        prerequisites.extend(native_prerequisites)
        prerequisites.append({"path": str(frozen), "kind": "file"})
        semantic_prerequisites.append(
            {
                "role": "FROZEN_SEARCHED_WINNER",
                "path": str(frozen),
                "schema": FREEZE_TERMINAL_SCHEMA,
                "allowed_terminal_statuses": ["COMPLETE"],
                "expected_fields": {},
            }
        )
        for loadout_id in parsed.loadout_ids:
            bundle = policy_bundle_root / residual_policy_bundle_name_v8(loadout_id)
            prerequisites.append({"path": str(bundle), "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": "V8_RESIDUAL_POLICY_BUNDLE",
                    "path": str(bundle),
                    "schema": CAT_ACTION_PLAN_DISTILLATION_SCHEMA_V8,
                    "allowed_statuses": [
                        "EXACT_CAT_AND_PROPOSAL_CANDIDATES_FROZEN_FOR_FRESH_TEST",
                        "EXACT_CAT_ONLY_NO_ELIGIBLE_PROPOSAL_CELL",
                    ],
                    "expected_fields": {
                        "loadout_id": loadout_id,
                        "exact_build_id": parsed.build_id,
                    },
                }
            )
        for task_index, shard in enumerate(evaluation_shards):
            output = eval_root / evaluation_terminal_name_v1(shard)
            logical_items = len(shard.examples) * 4
            assigned_workers = min(eval_workers, logical_items)
            argv = [
                "env",
                "GOMAXPROCS=1",
                f"BOC_CONTRA260817_ROOT={contra_source_root}",
                f"BOC_CONTRA260817_MANIFEST={contra_manifest}",
                str(python),
                "-m",
                WORKER_MODULE_V8,
                "eval",
                "--campaign",
                str(remote_campaign),
                "--frozen",
                str(frozen),
                "--policy-bundle-root",
                str(policy_bundle_root),
                "--seed-shard-index",
                str(shard.seed_shard_index),
                "--bridge",
                str(bridge),
                "--bridge-cwd",
                str(bridge_cwd),
                "--runtime-binding",
                str(runtime_binding),
                "--replay-workers",
                str(assigned_workers),
                "--output",
                str(output),
            ]
            specs.append(
                _task_spec_v1(
                    description=f"held-out fresh V8 {shard.work_id}",
                    argv=argv,
                    cwd=project,
                    signature=f"{signature_prefix}/{shard.work_id}",
                    preferred_node=NODE_NAMES[task_index % len(NODE_NAMES)],
                    cpu=assigned_workers,
                    ram_mb=EVAL_RAM_MB,
                    logical_items=logical_items,
                    success_marker=f"DONE cat_action_plan_eval {shard.work_id}",
                )
            )
            outputs.append(str(output))
            terminal_outputs.append(str(output))
    else:
        prerequisites.append({"path": str(frozen), "kind": "file"})
        semantic_prerequisites.append(
            {
                "role": "FROZEN_SEARCHED_WINNER",
                "path": str(frozen),
                "schema": FREEZE_TERMINAL_SCHEMA,
                "allowed_terminal_statuses": ["COMPLETE"],
                "expected_fields": {},
            }
        )
        for shard in evaluation_shards:
            terminal = eval_root / evaluation_terminal_name_v1(shard)
            prerequisites.append({"path": str(terminal), "kind": "file"})
            semantic_prerequisites.append(
                {
                    "role": "EVAL_SHARD",
                    "path": str(terminal),
                    "schema": EVAL_TERMINAL_SCHEMA,
                    "allowed_terminal_statuses": sorted(TERMINAL_STATUSES),
                    "expected_fields": {
                        "seed_shard_index": shard.seed_shard_index,
                        "examples": [row.to_dict() for row in shard.examples],
                    },
                }
            )
        argv = [
            str(python),
            "-m",
            WORKER_MODULE_V8,
            "summarize",
            "--campaign",
            str(remote_campaign),
            "--frozen",
            str(frozen),
            "--evaluation-root",
            str(eval_root),
            "--output",
            str(summary),
        ]
        specs.append(
            _task_spec_v1(
                description="summarize held-out fresh V8 panel",
                argv=argv,
                cwd=project,
                signature=f"{signature_prefix}/summary",
                preferred_node=NODE_NAMES[0],
                cpu=1,
                ram_mb=4_096,
                logical_items=len(parsed.evaluation_examples) * 4,
                success_marker="DONE cat_action_plan_summary",
            )
        )
        outputs.append(str(summary))
        terminal_outputs.append(str(summary))

    scheduler_cli = scheduler_cli or str(SCHEDULER_SKILL / "scheduler.py")
    return {
        "schema": "upper_kara_cat_action_plan_remote_plan/v8",
        "status": "PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED",
        "run_id": run_id,
        "phase": phase,
        "campaign_id": parsed.campaign_id,
        "build_id": parsed.build_id,
        "campaign_contract": campaign_contract,
        "run_root": str(run_root),
        "project_root": str(project),
        "remote_python": str(python),
        "task_count": len(specs),
        "task_specs": specs,
        "required_remote_paths": prerequisites,
        "semantic_prerequisites": semantic_prerequisites,
        "terminal_outputs": terminal_outputs,
        "artifact_outputs": outputs,
        "scheduler_intent_label": f"BrainOfCat-{RUN_FAMILY}-{run_id}-{phase}",
        "signature_batch_prefix": f"BrainOfCat/{RUN_FAMILY}-{run_id}",
        "manual_small_fetch_candidates": (
            outputs if phase in {"freeze", "summarize"} else []
        ),
        "raw_offline_data_required": False,
        "derived_scenario_capsule_required": phase in {"teacher", "train", "eval"},
        "compact_offline_guide_required": False,
        "external_project_directory_required": False,
        "external_source_code_closure_required": phase in {"teacher", "train", "eval"},
        "automatic_result_pull": False,
        "candidate_count_audit": {
            "distilled_residual_upper_bound_per_loadout_including_zero": (
                spec.max_distilled_candidates_per_loadout
            ),
            "zero_residual_count": 1,
            "imported_reference_count": IMPORTED_REFERENCE_COUNT,
            "total_training_program_upper_bound": candidate_upper_bound,
            "exact_unique_count_known_at_plan_time": False,
            "teacher_labels_are_not_full_route_policy_outcomes": True,
        },
        "resource_model": {
            "teacher_branch_workers_per_task": teacher_workers,
            "teacher_ram_mb_per_task": TEACHER_RAM_MB,
            "train_replay_workers_per_task_default": train_workers,
            "evaluation_replay_workers_per_task_default": eval_workers,
            "train_ram_mb_per_task": TRAIN_RAM_MB,
            "evaluation_ram_mb_per_task": EVAL_RAM_MB,
            "gomaxprocs_per_bridge": 1,
        },
        "phase_logical_item_count": sum(
            row["cpu_parallel_logical_items"] for row in specs
        ),
        "bulk_submit_command": (
            f"python3 {shlex.quote(scheduler_cli)} submit-jsonl "
            "--file <TASK_SPECS.json> --trusted --json "
            f"--intent-label {shlex.quote(f'BrainOfCat-{RUN_FAMILY}-{run_id}-{phase}')}"
        ),
        "phase_contract": {
            "order": list(PHASES_V8),
            "only_selected_phase_is_planned": True,
            "prerequisite_artifacts_required_before_queue_write": True,
            "teacher_task_count": len(teacher_shards),
            "teacher_uses_proposal_cohort_only": True,
            "distill_task_count": len(parsed.loadout_ids),
            "distillation_once_per_loadout": True,
            "training_task_count": len(training_shards),
            "freeze_task_count": 1,
            "evaluation_task_count": len(evaluation_shards),
            "summary_task_count": 1,
            "training_shards_may_generate_candidates": False,
            "compact_lane_sidecar_then_terminal_commit": True,
            "heldout_search_or_generation": False,
            "submit_performed": False,
            "dispatch_performed": False,
        },
    }


def build_upper_kara_causal_program_remote_plan_v8(
    *,
    shared_home: str,
    run_id: str,
    phase: str,
    campaign: str | Path,
    bridge_name: str,
    teacher_workers: int = TEACHER_CPU,
    train_workers: int = TRAIN_CPU,
    eval_workers: int = EVAL_CPU,
    scheduler_cli: str | None = None,
) -> JSONMap:
    """Plan V8 explicitly while retaining automatic V8 dispatch in v1."""

    return build_upper_kara_causal_program_remote_plan_v1(
        shared_home=shared_home,
        run_id=run_id,
        phase=phase,
        campaign=campaign,
        bridge_name=bridge_name,
        teacher_workers=teacher_workers,
        train_workers=train_workers,
        eval_workers=eval_workers,
        scheduler_cli=scheduler_cli,
    )


def build_upper_kara_causal_program_remote_plan_v2(
    *,
    shared_home: str,
    run_id: str,
    phase: str,
    campaign: str | Path,
    bridge_name: str,
    train_workers: int = TRAIN_CPU,
    eval_workers: int = EVAL_CPU,
    scheduler_cli: str | None = None,
) -> JSONMap:
    """Plan the candidate-once compact workflow without altering v1 plans."""

    return build_upper_kara_causal_program_remote_plan_v1(
        shared_home=shared_home,
        run_id=run_id,
        phase=phase,
        campaign=campaign,
        bridge_name=bridge_name,
        train_workers=train_workers,
        eval_workers=eval_workers,
        scheduler_cli=scheduler_cli,
        compact_v2=True,
    )


def _test_remote_path(scheduler: Any, node: str, row: Mapping[str, Any]) -> bool:
    try:
        flag = {"file": "-f", "directory": "-d", "executable": "-x"}[
            row["kind"]
        ]
    except KeyError as error:
        raise ValueError(f"unsupported remote path kind: {row.get('kind')!r}") from error
    rc, _, _ = scheduler.run_on(
        node,
        f"test {flag} {shlex.quote(str(row['path']))}",
        timeout=20,
        check=False,
    )
    return rc == 0


def _remote_missing_required_paths_v1(
    scheduler: Any,
    node: str,
    *,
    remote_python: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    """Return missing prerequisite rows after bounded remote probes."""

    required = tuple(rows)
    if not required:
        return []
    probes: list[JSONMap] = []
    for index, row in enumerate(required):
        kind = row.get("kind")
        if kind not in {"file", "directory", "executable"}:
            raise ValueError(f"unsupported remote path kind: {kind!r}")
        probes.append(
            {
                "index": index,
                "path": str(row["path"]),
                "kind": kind,
            }
        )
    script = """# upper_kara_required_path_batch_v1
import json
import os
import sys

rows = json.loads(sys.argv[1])
checks = {
    "file": os.path.isfile,
    "directory": os.path.isdir,
    "executable": lambda path: os.access(path, os.X_OK),
}
missing = [
    {"index": row["index"], "path": row["path"]}
    for row in rows
    if not checks[row["kind"]](row["path"])
]
print(json.dumps(missing, ensure_ascii=False, separators=(",", ":")))
"""
    missing_indices: list[int] = []
    for offset in range(0, len(probes), REMOTE_OUTPUT_PATH_BATCH_SIZE):
        batch = probes[offset : offset + REMOTE_OUTPUT_PATH_BATCH_SIZE]
        command = shlex.join(
            [
                remote_python,
                "-c",
                script,
                json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
            ]
        )
        rc, stdout, stderr = scheduler.run_on(
            node, command, timeout=60, check=False
        )
        batch_index = offset // REMOTE_OUTPUT_PATH_BATCH_SIZE
        if rc != 0:
            raise RuntimeError(
                "remote prerequisite inventory failed at batch "
                f"{batch_index}: {stderr[-300:]}"
            )
        try:
            missing = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "remote prerequisite inventory is not JSON at batch "
                f"{batch_index}"
            ) from error
        if not isinstance(missing, list):
            raise RuntimeError("remote prerequisite inventory is not a list")
        batch_indices = {row["index"] for row in batch}
        for result in missing:
            if not isinstance(result, Mapping):
                raise RuntimeError(
                    "remote prerequisite inventory returned malformed rows"
                )
            index = result.get("index")
            path = result.get("path")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index not in batch_indices
                or path != probes[index]["path"]
                or index in missing_indices
            ):
                raise RuntimeError(
                    "remote prerequisite inventory returned invalid rows"
                )
            missing_indices.append(index)
    return [dict(required[index]) for index in sorted(missing_indices)]


def _remote_existing_paths_v1(
    scheduler: Any,
    node: str,
    *,
    remote_python: str,
    paths: Sequence[str],
) -> list[str]:
    """Return planned output paths that already exist in one remote probe."""

    ordered = tuple(sorted(set(paths)))
    if not ordered:
        return []
    script = (
        "import json,os,sys;"
        "print(json.dumps([path for path in sys.argv[1:] "
        "if os.path.exists(path)],separators=(',',':')))"
    )
    existing: list[str] = []
    for offset in range(0, len(ordered), REMOTE_OUTPUT_PATH_BATCH_SIZE):
        batch = ordered[offset : offset + REMOTE_OUTPUT_PATH_BATCH_SIZE]
        rc, stdout, stderr = scheduler.run_on(
            node,
            shlex.join([remote_python, "-c", script, *batch]),
            timeout=60,
            check=False,
        )
        if rc != 0:
            raise RuntimeError(
                "remote output inventory failed at batch "
                f"{offset // REMOTE_OUTPUT_PATH_BATCH_SIZE}: {stderr[-300:]}"
            )
        try:
            found = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("remote output inventory is not JSON") from error
        if (
            not isinstance(found, list)
            or any(not isinstance(path, str) for path in found)
            or len(found) != len(set(found))
            or not set(found).issubset(batch)
        ):
            raise RuntimeError("remote output inventory returned invalid paths")
        existing.extend(found)
    return sorted(existing)


def _read_remote_terminal_projections_v1(
    scheduler: Any,
    node: str,
    *,
    remote_python: str,
    paths: Sequence[str],
) -> tuple[dict[str, JSONMap], dict[str, str]]:
    """Read terminal identity projections in bounded remote Python batches."""

    ordered = tuple(dict.fromkeys(str(path) for path in paths))
    if not ordered:
        return {}, {}
    script = f"""# upper_kara_semantic_projection_batch_v1
import json
import sys

keys = {TERMINAL_PROJECTION_KEYS!r}
rows = []
for path in sys.argv[1:]:
    try:
        with open(path, encoding="utf-8-sig") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("terminal is not a JSON object")
        rows.append({{
            "path": path,
            "projection": {{key: value.get(key) for key in keys}},
        }})
    except Exception as error:
        rows.append({{
            "path": path,
            "error": f"{{type(error).__name__}}:{{error}}",
        }})
print(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
"""
    projections: dict[str, JSONMap] = {}
    failures: dict[str, str] = {}
    for offset in range(0, len(ordered), REMOTE_OUTPUT_PATH_BATCH_SIZE):
        batch = ordered[offset : offset + REMOTE_OUTPUT_PATH_BATCH_SIZE]
        command = shlex.join([remote_python, "-c", script, *batch])
        rc, stdout, stderr = scheduler.run_on(
            node, command, timeout=600, check=False
        )
        batch_index = offset // REMOTE_OUTPUT_PATH_BATCH_SIZE
        if rc != 0:
            raise ValueError(
                "remote terminal projection batch failed at batch "
                f"{batch_index}: {stderr[-300:]}"
            )
        try:
            rows = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ValueError(
                "remote terminal projection batch is not JSON at batch "
                f"{batch_index}"
            ) from error
        if not isinstance(rows, list):
            raise ValueError("remote terminal projection batch is not a list")
        allowed = set(batch)
        returned: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
                raise ValueError(
                    "remote terminal projection batch row is malformed"
                )
            path = row["path"]
            if path not in allowed or path in returned:
                raise ValueError(
                    "remote terminal projection batch returned invalid paths"
                )
            returned.add(path)
            projection = row.get("projection")
            error = row.get("error")
            if isinstance(projection, Mapping) and error is None:
                projections[path] = dict(projection)
            elif isinstance(error, str) and error and projection is None:
                failures[path] = error
            else:
                raise ValueError(
                    "remote terminal projection batch row lacks one result"
                )
        if returned != allowed:
            raise ValueError("remote terminal projection batch omitted paths")
    return projections, failures


def _semantic_prerequisite_errors_v1(
    plan: Mapping[str, Any],
    spec: Mapping[str, Any],
    projection: Mapping[str, Any],
    *,
    frozen_projection: Mapping[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    role = spec.get("role")
    if projection.get("schema") != spec.get("schema"):
        errors.append("SCHEMA_MISMATCH")
    if role == "V8_TEACHER_RESULT":
        if projection.get("status") not in spec.get("allowed_statuses", ()):
            errors.append("STATUS_NOT_ALLOWED")
        simulator_seed = projection.get("simulator_seed")
        if (
            isinstance(simulator_seed, bool)
            or not isinstance(simulator_seed, int)
            or simulator_seed < 0
        ):
            errors.append("SIMULATOR_SEED_INVALID")
    elif role == "V8_RESIDUAL_POLICY_BUNDLE":
        if projection.get("status") not in spec.get("allowed_statuses", ()):
            errors.append("STATUS_NOT_ALLOWED")
        if projection.get("campaign_id") != plan.get("campaign_id"):
            errors.append("CAMPAIGN_ID_MISMATCH")
        if projection.get("campaign_contract") != plan.get("campaign_contract"):
            errors.append("CAMPAIGN_CONTRACT_MISMATCH")
        if projection.get("exact_build_id") != plan.get("build_id"):
            errors.append("BUILD_ID_MISMATCH")
    else:
        if role != "CANDIDATE_MANIFEST_V2" and projection.get(
            "terminal_status"
        ) not in spec.get("allowed_terminal_statuses", ()):
            errors.append("TERMINAL_STATUS_NOT_ALLOWED")
        if projection.get("campaign_id") != plan.get("campaign_id"):
            errors.append("CAMPAIGN_ID_MISMATCH")
        if projection.get("build_id") != plan.get("build_id"):
            errors.append("BUILD_ID_MISMATCH")
        if (
            role != "TRAIN_COMMIT_V2"
            and projection.get("campaign_contract")
            != plan.get("campaign_contract")
        ):
            errors.append("CAMPAIGN_CONTRACT_MISMATCH")
    expected_fields = spec.get("expected_fields")
    if not isinstance(expected_fields, Mapping):
        errors.append("EXPECTED_FIELDS_MALFORMED")
    else:
        errors.extend(
            f"FIELD_MISMATCH:{name}"
            for name, expected in expected_fields.items()
            if projection.get(name) != expected
        )
    expected_nested_fields = spec.get("expected_nested_fields", {})
    if not isinstance(expected_nested_fields, Mapping):
        errors.append("EXPECTED_NESTED_FIELDS_MALFORMED")
    else:
        for parent, expected_rows in expected_nested_fields.items():
            actual_rows = projection.get(parent)
            if not isinstance(expected_rows, Mapping) or not isinstance(
                actual_rows, Mapping
            ):
                errors.append(f"NESTED_FIELD_MISMATCH:{parent}")
                continue
            errors.extend(
                f"NESTED_FIELD_MISMATCH:{parent}.{name}"
                for name, expected in expected_rows.items()
                if actual_rows.get(name) != expected
            )
    if role == "FROZEN_SEARCHED_WINNER":
        winner = projection.get("winner")
        if not isinstance(winner, Mapping):
            errors.append("FROZEN_WINNER_MISSING")
        elif (
            winner.get("program_origin")
            not in {"SEARCHED", "SEARCHED_REACTIVE"}
            or winner.get("program_ref") != winner.get("program_id")
            or not isinstance(winner.get("program_key"), str)
            or not winner.get("program_key")
        ):
            errors.append("FROZEN_WINNER_NOT_SEARCHED_OR_INCONSISTENT")
    elif role == "EVAL_SHARD":
        if projection.get("frozen_program_origin") not in {
            "SEARCHED",
            "SEARCHED_REACTIVE",
        }:
            errors.append("EVAL_FROZEN_PROGRAM_NOT_SEARCHED")
        winner = (
            frozen_projection.get("winner")
            if isinstance(frozen_projection, Mapping)
            else None
        )
        if not isinstance(winner, Mapping):
            errors.append("FROZEN_WINNER_UNAVAILABLE_FOR_EVAL_IDENTITY")
        else:
            comparisons = {
                "selected_loadout_id": winner.get("loadout_id"),
                "frozen_program_id": winner.get("program_id"),
                "frozen_program_key": winner.get("program_key"),
                "frozen_program_origin": winner.get("program_origin"),
            }
            errors.extend(
                f"FROZEN_IDENTITY_MISMATCH:{name}"
                for name, expected in comparisons.items()
                if projection.get(name) != expected
            )
    return errors


def inventory_upper_kara_causal_program_remote_v1(
    scheduler: Any, plan: Mapping[str, Any], *, probe_node: str = "node001"
) -> JSONMap:
    if probe_node not in NODE_NAMES:
        raise ValueError("probe_node must be node001--node006")
    signatures = {row["signature"] for row in plan["task_specs"]}
    outputs = set(plan.get("artifact_outputs", plan["terminal_outputs"]))
    signature_statuses: dict[str, list[JSONMap]] = {
        signature: [] for signature in sorted(signatures)
    }
    writer_conflicts: list[JSONMap] = []
    with scheduler.state_lock(
        shared=True, purpose="upper-kara-causal-program-phase-inventory"
    ):
        state = scheduler.load_state()
        for task in state.get("tasks", []):
            status = task.get("status")
            signature = task.get("signature")
            command = str(task.get("cmd") or "")
            same_signature = signature in signatures
            same_output = any(path in command for path in outputs)
            if same_signature:
                signature_statuses[str(signature)].append(
                    {"id": task.get("id"), "status": status}
                )
            if status in TASK_CONFLICT_STATUSES and (same_signature or same_output):
                writer_conflicts.append(
                    {
                        "id": task.get("id"),
                        "status": status,
                        "signature": signature,
                        "conflict": (
                            "PLANNED_SIGNATURE"
                            if same_signature
                            else "PLANNED_TERMINAL_WRITER"
                        ),
                    }
                )
    missing = _remote_missing_required_paths_v1(
        scheduler,
        probe_node,
        remote_python=str(plan["remote_python"]),
        rows=plan["required_remote_paths"],
    )
    artifact_conflicts = _remote_existing_paths_v1(
        scheduler,
        probe_node,
        remote_python=str(plan["remote_python"]),
        paths=tuple(outputs),
    )
    missing_paths = {row["path"] for row in missing}
    semantic_specs = tuple(plan.get("semantic_prerequisites", ()))
    semantic_paths = tuple(
        str(spec["path"])
        for spec in semantic_specs
        if str(spec["path"]) not in missing_paths
    )
    projections: dict[str, JSONMap] = {}
    projection_failures: list[JSONMap] = []
    try:
        projections, batch_failures = _read_remote_terminal_projections_v1(
            scheduler,
            probe_node,
            remote_python=str(plan["remote_python"]),
            paths=semantic_paths,
        )
    except (TypeError, ValueError) as error:
        batch_failures = {path: str(error) for path in semantic_paths}
    spec_by_path = {str(spec["path"]): spec for spec in semantic_specs}
    for path, error in batch_failures.items():
        spec = spec_by_path[path]
        projection_failures.append(
            {
                "path": path,
                "role": spec.get("role"),
                "errors": [f"PROJECTION_FAILED:{error}"],
            }
        )
    frozen_projection = next(
        (
            projections.get(str(spec["path"]))
            for spec in semantic_specs
            if spec.get("role") == "FROZEN_SEARCHED_WINNER"
        ),
        None,
    )
    semantic_conflicts = list(projection_failures)
    failed_projection_paths = {row["path"] for row in projection_failures}
    for spec in semantic_specs:
        path = str(spec["path"])
        if path in missing_paths or path in failed_projection_paths:
            continue
        errors = _semantic_prerequisite_errors_v1(
            plan,
            spec,
            projections[path],
            frozen_projection=frozen_projection,
        )
        if errors:
            semantic_conflicts.append(
                {"path": path, "role": spec.get("role"), "errors": errors}
            )
    ready = (
        not missing
        and not writer_conflicts
        and not artifact_conflicts
        and not semantic_conflicts
    )
    return {
        "schema": "upper_kara_causal_program_remote_inventory/v1",
        "status": "READY_TO_QUEUE" if ready else "NOT_READY_TO_QUEUE",
        "probe_node": probe_node,
        "missing_required_paths": missing,
        "signature_statuses": signature_statuses,
        "scheduler_task_conflicts": writer_conflicts,
        "remote_terminal_conflicts": artifact_conflicts,
        "semantic_prerequisite_conflicts": semantic_conflicts,
        "semantic_terminal_projection_count": len(projections),
        "task_count": len(plan["task_specs"]),
    }


def submit_upper_kara_causal_program_phase_v1(
    plan: Mapping[str, Any], *, scheduler_cli: Path | None = None
) -> JSONMap:
    specs = list(plan.get("task_specs") or [])
    if not specs:
        raise ValueError("phase plan has no task specs")
    scheduler_cli = scheduler_cli or SCHEDULER_SKILL / "scheduler.py"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(specs, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(scheduler_cli),
                "submit-jsonl",
                "--file",
                str(temporary),
                "--trusted",
                "--json",
                "--intent-label",
                str(plan["scheduler_intent_label"]),
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
    if result.get("count") != len(specs):
        raise RuntimeError("scheduler accepted a different task count")
    return {
        "status": "PHASE_QUEUED_NOT_DISPATCHED",
        "task_count": len(specs),
        "scheduler_result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--phase", choices=tuple(dict.fromkeys((*PHASES_V2, *PHASES_V8))), required=True
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--bridge-name", required=True)
    parser.add_argument("--shared-home", default="/home/erzhu419")
    parser.add_argument("--teacher-workers", type=int, default=TEACHER_CPU)
    parser.add_argument("--train-workers", type=int, default=TRAIN_CPU)
    parser.add_argument("--eval-workers", type=int, default=EVAL_CPU)
    parser.add_argument("--probe-node", choices=NODE_NAMES, default="node001")
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--compact-v2",
        action="store_true",
        help="use candidate-once manifests and compact training shards",
    )
    args = parser.parse_args()
    plan = build_upper_kara_causal_program_remote_plan_v1(
        shared_home=args.shared_home,
        run_id=args.run_id,
        phase=args.phase,
        campaign=args.campaign,
        bridge_name=args.bridge_name,
        teacher_workers=args.teacher_workers,
        train_workers=args.train_workers,
        eval_workers=args.eval_workers,
        compact_v2=args.compact_v2,
    )
    if args.inventory or args.submit:
        scheduler = _scheduler()
        shared_home, receipts = _shared_home(scheduler)
        if shared_home != args.shared_home:
            raise RuntimeError("planned shared_home differs from scheduler shared home")
        inventory = inventory_upper_kara_causal_program_remote_v1(
            scheduler, plan, probe_node=args.probe_node
        )
        plan["shared_home_receipts"] = receipts
        plan["inventory"] = inventory
        plan["scheduler_escalations"] = scheduler_escalation_inventory_v1(plan)
        if args.submit:
            if inventory["status"] != "READY_TO_QUEUE":
                raise RuntimeError(
                    "phase is not ready: missing input or duplicate task/artifact"
                )
            plan["scheduler_write_guards"] = check_scheduler_write_guards_v1(plan)
            plan["pre_submit_inventory"] = (
                inventory_upper_kara_causal_program_remote_v1(
                    scheduler, plan, probe_node=args.probe_node
                )
            )
            if plan["pre_submit_inventory"]["status"] != "READY_TO_QUEUE":
                raise RuntimeError(
                    "phase became non-ready before the queue transaction"
                )
            plan["submission"] = submit_upper_kara_causal_program_phase_v1(plan)
            plan["status"] = "PHASE_QUEUED_NOT_DISPATCHED"
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = (
    "PHASES",
    "PHASES_V2",
    "PHASES_V8",
    "TASK_CONFLICT_STATUSES",
    "WORKER_MODULE",
    "WORKER_MODULE_V2",
    "WORKER_MODULE_V8",
    "build_upper_kara_causal_program_remote_plan_v1",
    "build_upper_kara_causal_program_remote_plan_v2",
    "build_upper_kara_causal_program_remote_plan_v8",
    "inventory_upper_kara_causal_program_remote_v1",
    "submit_upper_kara_causal_program_phase_v1",
)
