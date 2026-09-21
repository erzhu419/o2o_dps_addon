"""Compare formal v7 and eval-only alive0 projection with paired Cat seeds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, build_plan_v1
from scripts.development_d900_paired_seed_shards_v1 import (
    NODES, SEED_BASE, TEAMMATE_SEED_BASE, aggregate_shards,
    seed_shards, summarize_runner_output,
)

FORMAL = f"{BASE}/runtime-src-20260921-white6603-v7"
PROJECTED = f"{BASE}/runtime-eval-initial-delay-projection-v1-node006"
STATIC = f"{BASE}/runtime-src-20260921-causal-target-v6"
DISPATCH = (
    f"{BASE}/runs/teammate-response-current/single_scan_direct_white6603_target_choice_v7/"
    "53a85ece0dbe56b95d072a47226ce6d2b5a73e540e1e9f350c1fe1cc04010c02/"
    "attempt1/dispatch/dispatch.json"
)
STORE = f"{FORMAL}/results/formal-d-white6603-v7.sqlite3"
RESULT_SHA = "fd8130722cf3b1e69795a3d894757f821a79805950cffa16120e366a564ae6ea"
MODEL_SHA = "62407cf5e8ca19aa075088182ba117cde6e44977b918da46e144eee63b25503d"
HISTORICAL_DEATH_MS = {"0": 9093, "1": 12796, "2": 15531}
LANES = {
    "formal_v7": {
        "runtime_root": FORMAL,
        "model_source_file_sha256": "42b60a5296197e7ef4a96c4279e52060e81418e9081e2de9b19a762659e3614b",
        "source_identity": "FROZEN_FORMAL_V7_RUNTIME",
    },
    "projected_alive0_v7": {
        "runtime_root": PROJECTED,
        "model_source_file_sha256": "5f0629712afca246396478bce30ee64ea5a75d84bd534d66b4be1d0b5b3d85b3",
        "source_identity": "EVAL_ONLY_ALIVE0_SOURCE_NOT_FORMAL_V7_CLOSURE",
    },
}


def build_specs() -> list[dict]:
    specs = []
    for index, (offset, count) in enumerate(seed_shards()):
        lanes = {}
        for name, identity in LANES.items():
            root = identity["runtime_root"]
            plan = build_plan_v1(
                code_root=root, simulator_root=STATIC, frozen_dispatch=DISPATCH,
                runtime_store=STORE, result_sha=RESULT_SHA, model_sha=MODEL_SHA,
                exact_build=f"{STATIC}/results/responsive-team-v4/v34-doomguard-exact-fury-build-request.json",
                deployed_binding=f"{STATIC}/results/responsive-team-v4/deployed-contra-runtime-binding-v1.951b8faa.json",
                bridge=f"{STATIC}/bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64",
            )
            argv = list(plan["full_wave"]["argv"])
            for flag, value in (
                ("--source", "cat"), ("--simulator-seed", SEED_BASE + offset),
                ("--teammate-seed", TEAMMATE_SEED_BASE + offset), ("--seed-count", count),
            ):
                argv[argv.index(flag) + 1] = str(value)
            lanes[name] = {**identity, "argv": argv}
        if lanes["formal_v7"]["argv"][2:] != lanes["projected_alive0_v7"]["argv"][2:]:
            raise ValueError("paired lanes differ beyond their runtime script path")
        specs.append({
            "schema": "development_d900_alive0_projection_paired_spec/v1",
            "status": "PLANNED_NOT_EXECUTED",
            "shard_index": index, "assigned_node": NODES[index % len(NODES)],
            "seed_offset": offset, "seed_count": count,
            "statistical_model_result_sha": RESULT_SHA,
            "statistical_model_sha": MODEL_SHA,
            "lanes": lanes,
        })
    return specs


def run_spec(spec: dict) -> dict:
    if spec["schema"] != "development_d900_alive0_projection_paired_spec/v1":
        raise ValueError("unknown paired projection spec")
    rows = {}
    for name, identity in LANES.items():
        lane = spec["lanes"][name]
        if any(lane[key] != identity[key] for key in identity):
            raise ValueError(f"{name} runtime identity differs")
        source_file = Path(identity["runtime_root"]) / "o2o_dps/chronicle_external_teammate_response_model_v1.py"
        if hashlib.sha256(source_file.read_bytes()).hexdigest() != identity["model_source_file_sha256"]:
            raise ValueError(f"{name} source module differs")
        env = dict(os.environ, PYTHONPATH=identity["runtime_root"], PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(lane["argv"], env=env, text=True,
                                   capture_output=True, timeout=900, check=False)
        if completed.returncode:
            raise RuntimeError(f"{name} shard {spec['shard_index']} failed: {completed.stderr[-2500:]}")
        full = json.loads(completed.stdout)
        compact = summarize_runner_output(
            full, offset=spec["seed_offset"], count=spec["seed_count"]
        )
        for item, replay in zip(compact, full["paired_replays"]):
            item["first_candidate_decisions"] = replay["first_decisions"]
            item["zero_time_post_drain_target_snapshot"] = next(
                (snapshot["targets"] for snapshot in replay["target_timeline_snapshots"]
                 if snapshot["observed_time_ms"] == 0), None
            )
        rows[name] = compact
    return {
        "schema": "development_d900_alive0_projection_paired_shard/v1",
        "status": "DEVELOPMENT_ONLY_RUNTIME_FIX_NOT_FORMAL_MODEL_RETRAIN",
        "shard_index": spec["shard_index"], "seed_offset": spec["seed_offset"],
        "seed_count": spec["seed_count"],
        "statistical_model_result_sha": RESULT_SHA,
        "statistical_model_sha": MODEL_SHA,
        "runtime_source_identity": LANES,
        **rows,
    }


def aggregate(results: list[dict]) -> dict:
    if len(results) != len(seed_shards()):
        raise ValueError("not all 43 shards are complete")
    for row in results:
        if (row["runtime_source_identity"] != LANES
                or row["statistical_model_result_sha"] != RESULT_SHA
                or row["statistical_model_sha"] != MODEL_SHA):
            raise ValueError("paired projection source or model identity differs")
    translated = [
        {
            "shard_index": row["shard_index"], "seed_offset": row["seed_offset"],
            "seed_count": row["seed_count"], "v6": row["formal_v7"],
            "v7": row["projected_alive0_v7"],
            "model_bindings": {
                lane: {"result_sha": RESULT_SHA, "model_sha": MODEL_SHA}
                for lane in ("v6", "v7")
            },
        } for row in results
    ]
    base = aggregate_shards(translated)

    def relabel_key(key: str) -> str:
        return (key.replace("v7_minus_v6", "@DELTA@")
                .replace("v6", "@FORMAL@")
                .replace("v7", "@PROJECTED@")
                .replace("@DELTA@", "projected_alive0_minus_formal")
                .replace("@FORMAL@", "formal_v7")
                .replace("@PROJECTED@", "projected_alive0_v7"))

    def relabel(value):
        if isinstance(value, dict):
            return {relabel_key(key): relabel(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relabel(item) for item in value]
        return value

    summary = relabel(base)
    summary["schema"] = "development_d900_alive0_projection_paired_aggregate/v1"
    summary["status"] = "DEVELOPMENT_ONLY_RUNTIME_FIX_NOT_FORMAL_MODEL_RETRAIN"
    summary["runtime_source_identity"] = LANES
    summary["statistical_model_result_sha"] = RESULT_SHA
    summary["statistical_model_sha"] = MODEL_SHA
    paired_death = {}
    for target, historical_ms in HISTORICAL_DEATH_MS.items():
        observed = [
            (formal["first_observed_dead_ms"][target], projected["first_observed_dead_ms"][target])
            for shard in results
            for formal, projected in zip(shard["formal_v7"], shard["projected_alive0_v7"])
            if (formal["first_observed_dead_ms"][target] is not None
                and projected["first_observed_dead_ms"][target] is not None)
        ]
        time_delta = [projected - formal for formal, projected in observed]
        error_delta = [abs(projected - historical_ms) - abs(formal - historical_ms)
                       for formal, projected in observed]
        paired_death[target] = {
            "historical_death_ms": historical_ms,
            "both_observed_count": len(observed),
            "projected_minus_formal_death_ms": {
                "mean": statistics.mean(time_delta) if time_delta else None,
                "median": statistics.median(time_delta) if time_delta else None,
                "earlier_count": sum(delta < 0 for delta in time_delta),
                "same_count": sum(delta == 0 for delta in time_delta),
                "later_count": sum(delta > 0 for delta in time_delta),
            },
            "projected_minus_formal_absolute_error_ms": {
                "mean": statistics.mean(error_delta) if error_delta else None,
                "median": statistics.median(error_delta) if error_delta else None,
                "closer_count": sum(delta < 0 for delta in error_delta),
                "same_count": sum(delta == 0 for delta in error_delta),
                "farther_count": sum(delta > 0 for delta in error_delta),
            },
        }
    summary["paired_death_time_and_historical_error_by_target"] = paired_death
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--spec-dir", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    reduce = commands.add_parser("aggregate")
    reduce.add_argument("--input-dir", type=Path, required=True)
    reduce.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan":
        args.spec_dir.mkdir(parents=True, exist_ok=True)
        specs = build_specs()
        for spec in specs:
            (args.spec_dir / f"shard-{spec['shard_index']:03d}.json").write_text(
                json.dumps(spec, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"planned_shards": len(specs), "paired_seeds": 128,
                          "executed": False}))
    elif args.command == "run":
        result = run_spec(json.loads(args.spec.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"shard_index": result["shard_index"], "paired_seeds": result["seed_count"]}))
    else:
        result = aggregate([
            json.loads((args.input_dir / f"shard-{index:03d}.json").read_text(encoding="utf-8"))
            for index in range(len(seed_shards()))
        ])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"paired_seed_count": result["paired_seed_count"],
                          "small_aggregate": str(args.output)}))


if __name__ == "__main__":
    main()
