"""Stage and submit eval-only alive0/formal-v7 paired Cat shards on CPU nodes."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_alive0_projection_paired_shards_v1 import (
    BASE, LANES, MODEL_SHA, NODES, RESULT_SHA, seed_shards,
)

REMOTE = f"{BASE}/runs/development-d900-formal-v7-vs-alive0-projection-paired-128"
REMOTE_PYTHON = "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10"
SCHEDULER = Path.home() / "mine_code/scheduleurm/skill/scheduler.py"


def verify_prior_case_identity() -> dict:
    directory = ROOT / "results/responsive-team-v4"
    formal = json.loads((directory / "v69-d900-zero-time-boundary-formal-seed1.json").read_text(encoding="utf-8"))
    projected = json.loads((directory / "v69-d900-zero-time-boundary-projected-seed1.json").read_text(encoding="utf-8"))
    fields = (
        "simulator_seed", "teammate_seed", "wave_id", "model_result_sha", "model_sha",
        "dynamic_config_content_sha256", "case_request_sha256", "metadata_artifact",
        "exact_build_artifact", "attackability_mode", "attackability_events",
        "effective_armor_events", "case_initial_health", "load_state_targets_before_zero_wakes",
    )
    if any(formal[field] != projected[field] for field in fields):
        raise ValueError("v69 formal/projected seed0 initial case inputs differ")
    if (formal["model_result_sha"], formal["model_sha"]) != (RESULT_SHA, MODEL_SHA):
        raise ValueError("v69 seed0 model binding differs")
    return {field: formal[field] for field in fields}


def task_specs(spec_dir: Path, *, subset: str) -> list[dict]:
    tasks = []
    for index, (offset, count) in enumerate(seed_shards()):
        if (subset == "smoke") != (index == 0):
            continue
        spec = json.loads((spec_dir / f"shard-{index:03d}.json").read_text(encoding="utf-8"))
        node = NODES[index % len(NODES)]
        if (spec["shard_index"], spec["assigned_node"], spec["seed_offset"], spec["seed_count"]) != (index, node, offset, count):
            raise ValueError(f"shard {index} assignment differs")
        command = shlex.join([
            REMOTE_PYTHON, "-B", f"{REMOTE}/scripts/development_d900_alive0_projection_paired_shards_v1.py",
            "run", "--spec", f"{REMOTE}/specs/shard-{index:03d}.json",
            "--output", f"{REMOTE}/shards/shard-{index:03d}.json",
        ])
        tasks.append({
            "description": f"BrainOfCat d900 formal-v7/alive0 Cat paired seeds {offset}-{offset + count - 1}",
            "cmd": command,
            "cwd": REMOTE,
            "signature": f"BrainOfCat/d900-formal-v7-alive0-paired-128/shard-{index:03d}",
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/d900-formal-v7-alive0-paired-128",
            "vram": 0, "ram_mb": 4096, "cpu": 2, "priority": "normal", "require_node": node,
            "skip_launch_staging": True,
            "allow_cpu_training": True,
            "cpu_training_justification": "Independent development-only paired Cat simulator diagnostics on CPU nodes.",
            "allow_no_ckpt": True, "allow_no_resume": True, "env_spec": "none",
        })
    return tasks


def stage(spec_dir: Path) -> None:
    sys.path.insert(0, str(SCHEDULER.parent))
    import scheduler

    node = "node004"
    rc, _, stderr = scheduler.run_on(
        node, shlex.join(["mkdir", "-p", f"{REMOTE}/scripts", f"{REMOTE}/specs", f"{REMOTE}/shards"]),
        timeout=60, check=False,
    )
    if rc:
        raise RuntimeError(f"remote stage directory failed: {stderr[-1000:]}")
    for source, destination in (
        (ROOT / "scripts/development_d900_alive0_projection_paired_shards_v1.py", f"{REMOTE}/scripts/"),
        (ROOT / "scripts/development_d900_paired_seed_shards_v1.py", f"{REMOTE}/scripts/"),
        (ROOT / "scripts/development_d900_v6_followup_plan_v1.py", f"{REMOTE}/scripts/"),
        (spec_dir, f"{REMOTE}/specs/"),
    ):
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", scheduler._ssh_rsync_shell_for_node(node),
            str(source) + ("/" if source.is_dir() else ""),
            f"{scheduler._ssh_target_for_node(node)}:{destination}",
        ], check=True, timeout=120)


def probe_smoke() -> dict:
    case_identity = verify_prior_case_identity()
    sys.path.insert(0, str(SCHEDULER.parent))
    import scheduler

    code = (
        "import json,os,sys; p=sys.argv[1]; x=json.load(open(p)); "
        "lanes=('formal_v7','projected_alive0_v7'); "
        "print(json.dumps({'bytes':os.path.getsize(p),'seed_count':x['seed_count'],"
        "'runtime_source_identity':x['runtime_source_identity'],"
        "'result_sha':x['statistical_model_result_sha'],'model_sha':x['statistical_model_sha'],"
        "'seed0':{lane:{k:x[lane][0][k] for k in "
        "('seed','teammate_seed','candidate_effective_damage','elapsed_ms','valid_development_completion',"
        "'first_observed_dead_ms','zero_time_post_drain_target_snapshot','first_candidate_decisions')} "
        "for lane in lanes}}))"
    )
    command = shlex.join([REMOTE_PYTHON, "-c", code, f"{REMOTE}/shards/shard-000.json"])
    rc, stdout, stderr = scheduler.run_on("node001", command, timeout=60, check=False)
    if rc:
        raise RuntimeError(f"smoke result unavailable: {stderr[-1000:]}")
    observed = json.loads(stdout)
    if (observed["result_sha"], observed["model_sha"], observed["runtime_source_identity"]) != (RESULT_SHA, MODEL_SHA, LANES):
        raise ValueError("smoke formal model or eval source identity differs")
    formal = observed["seed0"]["formal_v7"]
    projected = observed["seed0"]["projected_alive0_v7"]
    if any((row["seed"], row["teammate_seed"]) != (2026092001, 2026092002) for row in (formal, projected)):
        raise ValueError("smoke seed pair differs")
    old = json.loads((ROOT / "results/responsive-team-v4/v62-d900-v7-formal-d-cat-target-diagnostic-seed1.json").read_text(encoding="utf-8"))
    new = json.loads((ROOT / "results/responsive-team-v4/v68-d900-v7-initial-delay-cat-small-seed1.json").read_text(encoding="utf-8"))
    for lane, reference in ((formal, old), (projected, new)):
        if (not math.isclose(lane["candidate_effective_damage"], reference.get("effective_damage", lane["candidate_effective_damage"]), abs_tol=1e-6)
                or lane["elapsed_ms"] != reference["elapsed_ms"]
                or lane["first_observed_dead_ms"] != reference["first_observed_dead_ms"]
                or lane["zero_time_post_drain_target_snapshot"] != reference["target_timeline_snapshots"][0]["targets"]):
            raise ValueError("smoke seed0 differs from the corresponding previous lane")
    if (formal["first_candidate_decisions"] != new["first_decisions"]
            or projected["first_candidate_decisions"] != new["first_decisions"]):
        raise ValueError("smoke Cat first six actions differ from the matched seed0 case")
    if observed["seed_count"] != 3 or observed["bytes"] > 150_000:
        raise ValueError("smoke result count or compact size differs")
    observed["status"] = "SMOKE_PASS_FORMAL_V62_PROJECTED_V68_MATCH"
    observed["matched_initial_case_identity"] = {
        key: case_identity[key] for key in (
            "dynamic_config_content_sha256", "case_request_sha256", "case_initial_health",
            "effective_armor_events", "attackability_events", "metadata_artifact", "exact_build_artifact",
        )
    }
    return observed


def fetch_aggregate(destination: Path) -> dict:
    sys.path.insert(0, str(SCHEDULER.parent))
    import scheduler

    node = "node004"
    source = f"{REMOTE}/aggregate-formal-bound.json"
    rc, stdout, stderr = scheduler.run_on(node, shlex.join(["stat", "-c", "%s", source]), timeout=30, check=False)
    if rc:
        raise RuntimeError(f"remote aggregate unavailable: {stderr[-1000:]}")
    size = int(stdout.strip())
    if size > 100_000:
        raise ValueError("remote aggregate exceeds small-pull boundary")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
        "-e", scheduler._ssh_rsync_shell_for_node(node),
        f"{scheduler._ssh_target_for_node(node)}:{source}", str(destination),
    ], check=True, timeout=120)
    result = json.loads(destination.read_text(encoding="utf-8"))
    if (result["paired_seed_count"], result["statistical_model_result_sha"],
            result["statistical_model_sha"], result["runtime_source_identity"]) != (128, RESULT_SHA, MODEL_SHA, LANES):
        raise ValueError("aggregate count/model/source identity differs")
    return {"status": "SMALL_AGGREGATE_FETCHED_AND_BOUND", "bytes": size, "output": str(destination)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "probe-smoke", "emit-smoke", "emit-rest", "fetch-aggregate"))
    parser.add_argument("--spec-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operation == "stage":
        if args.spec_dir is None:
            parser.error("--spec-dir is required")
        stage(args.spec_dir)
        print(json.dumps({"status": "STAGED_ONLY", "remote": REMOTE, "specs": len(seed_shards())}))
    elif args.operation == "probe-smoke":
        print(json.dumps(probe_smoke(), ensure_ascii=False))
    elif args.operation == "fetch-aggregate":
        if args.output is None:
            parser.error("--output is required")
        print(json.dumps(fetch_aggregate(args.output), ensure_ascii=False))
    else:
        if args.spec_dir is None or args.output is None:
            parser.error("--spec-dir and --output are required")
        if args.operation == "emit-rest":
            probe_smoke()
        tasks = task_specs(args.spec_dir, subset="smoke" if args.operation == "emit-smoke" else "rest")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("".join(json.dumps(task, ensure_ascii=False) + "\n" for task in tasks), encoding="utf-8")
        print(json.dumps({"status": "SPECS_ONLY_NOT_SUBMITTED", "tasks": len(tasks), "output": str(args.output)}))


if __name__ == "__main__":
    main()
