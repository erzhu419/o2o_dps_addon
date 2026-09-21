"""Stage and submit small d900 paired-seed shards to scheduleurm CPU nodes."""

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
from scripts.development_d900_paired_seed_shards_v1 import BASE, NODES, seed_shards

REMOTE = f"{BASE}/runs/development-d900-v6v7-cat-paired-128"
REMOTE_PYTHON = "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10"
SCHEDULER = Path.home() / "mine_code/scheduleurm/skill/scheduler.py"


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
            REMOTE_PYTHON, "-B", f"{REMOTE}/scripts/development_d900_paired_seed_shards_v1.py",
            "run", "--spec", f"{REMOTE}/specs/shard-{index:03d}.json",
            "--output", f"{REMOTE}/shards/shard-{index:03d}.json",
        ])
        tasks.append({
            "description": f"BrainOfCat d900 v6/v7 Cat paired seeds {offset}-{offset + count - 1}",
            "cmd": command,
            "cwd": REMOTE,
            "signature": f"BrainOfCat/d900-v6v7-cat-paired-128/shard-{index:03d}",
            "project": "BrainOfCat",
            "resource_family": "BrainOfCat/d900-v6v7-cat-paired-128",
            "vram": 0,
            "ram_mb": 4096,
            "cpu": 2,
            "priority": "normal",
            "require_node": node,
            "skip_launch_staging": True,
            "allow_cpu_training": True,
            "cpu_training_justification": "Independent paired-seed Cat simulator diagnostics on CPU nodes.",
            "allow_no_ckpt": True,
            "allow_no_resume": True,
            "env_spec": "none",
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
        (ROOT / "scripts/development_d900_paired_seed_shards_v1.py", f"{REMOTE}/scripts/"),
        (ROOT / "scripts/development_d900_v6_followup_plan_v1.py", f"{REMOTE}/scripts/"),
        (spec_dir, f"{REMOTE}/specs/"),
    ):
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", scheduler._ssh_rsync_shell_for_node(node), str(source) + ("/" if source.is_dir() else ""),
            f"{scheduler._ssh_target_for_node(node)}:{destination}",
        ], check=True, timeout=120)


def probe_smoke(spec_dir: Path) -> dict:
    sys.path.insert(0, str(SCHEDULER.parent))
    import scheduler

    code = (
        "import json,os,sys; p=sys.argv[1]; x=json.load(open(p)); "
        "print(json.dumps({'bytes':os.path.getsize(p),'seed_count':x['seed_count'],"
        "'model_bindings':x['model_bindings'],"
        "'seed0':{v:{k:x[v][0][k] for k in "
        "('seed','teammate_seed','candidate_effective_damage','valid_development_completion','first_observed_dead_ms')} "
        "for v in ('v6','v7')}}))"
    )
    command = shlex.join([REMOTE_PYTHON, "-c", code, f"{REMOTE}/shards/shard-000.json"])
    rc, stdout, stderr = scheduler.run_on("node001", command, timeout=60, check=False)
    if rc:
        raise RuntimeError(f"smoke result unavailable: {stderr[-1000:]}")
    observed = json.loads(stdout)
    spec = json.loads((spec_dir / "shard-000.json").read_text(encoding="utf-8"))
    for version in ("v6", "v7"):
        expected_binding = {key: spec[version][key] for key in ("result_sha", "model_sha")}
        if observed["model_bindings"][version] != expected_binding:
            raise ValueError(f"{version} model identity differs")
        seed0 = observed["seed0"][version]
        if (seed0["seed"], seed0["teammate_seed"]) != (2026092001, 2026092002):
            raise ValueError(f"{version} first seed pair differs")
        reference_path = ROOT / "results/responsive-team-v4" / (
            "v56-d900-v6-cat-target-diagnostic-seed1.json" if version == "v6"
            else "v62-d900-v7-formal-d-cat-target-diagnostic-seed1.json"
        )
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        reference_row = reference["paired_replays"][0] if version == "v6" else reference
        if not math.isclose(seed0["candidate_effective_damage"], reference_row["effective_damage"], abs_tol=1e-6):
            raise ValueError(f"{version} seed0 candidate damage differs")
        if seed0["first_observed_dead_ms"] != reference_row["first_observed_dead_ms"]:
            raise ValueError(f"{version} seed0 death times differ")
    if observed["seed_count"] != 3 or observed["bytes"] > 100_000:
        raise ValueError("smoke result count/size differs")
    observed["status"] = "SMOKE_PASS_SEED0_MATCHES_V56_V62"
    return observed


def fetch_aggregate(spec_dir: Path, destination: Path) -> dict:
    sys.path.insert(0, str(SCHEDULER.parent))
    import scheduler

    node = "node004"
    source = f"{REMOTE}/aggregate-formal-bound.json"
    rc, stdout, stderr = scheduler.run_on(
        node, shlex.join(["stat", "-c", "%s", source]), timeout=30, check=False,
    )
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
    aggregate = json.loads(destination.read_text(encoding="utf-8"))
    spec = json.loads((spec_dir / "shard-000.json").read_text(encoding="utf-8"))
    expected = {version: {key: spec[version][key] for key in ("result_sha", "model_sha")}
                for version in ("v6", "v7")}
    if aggregate["paired_seed_count"] != 128 or aggregate["model_bindings"] != expected:
        raise ValueError("aggregate seed count or model identity differs")
    return {"status": "SMALL_AGGREGATE_FETCHED_AND_BOUND", "bytes": size,
            "output": str(destination), "paired_seed_count": aggregate["paired_seed_count"],
            "both_valid_count": aggregate["both_valid_count"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "probe-smoke", "emit-smoke", "emit-rest", "fetch-aggregate"))
    parser.add_argument("--spec-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operation == "stage":
        stage(args.spec_dir)
        print(json.dumps({"status": "STAGED_ONLY", "remote": REMOTE, "specs": len(seed_shards())}))
        return
    if args.operation == "probe-smoke":
        print(json.dumps(probe_smoke(args.spec_dir), ensure_ascii=False))
        return
    if args.operation == "fetch-aggregate":
        if args.output is None:
            parser.error("--output is required for fetch-aggregate")
        print(json.dumps(fetch_aggregate(args.spec_dir, args.output), ensure_ascii=False))
        return
    if args.output is None:
        parser.error("--output is required for emit operations")
    tasks = task_specs(args.spec_dir, subset="smoke" if args.operation == "emit-smoke" else "rest")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(task, ensure_ascii=False) + "\n" for task in tasks), encoding="utf-8")
    print(json.dumps({"status": "SPECS_ONLY_NOT_SUBMITTED", "tasks": len(tasks), "output": str(args.output)}))


if __name__ == "__main__":
    main()
