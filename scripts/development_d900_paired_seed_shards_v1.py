"""Plan, run, and reduce development-only d900 Cat v6/v7 paired seed shards.

Plan mode only writes job specs. A shard runs the existing full-wave runner twice
on its assigned CPU node and keeps only compact per-seed diagnostics there.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, build_plan_v1


SEED_BASE = 2026092001
TEAMMATE_SEED_BASE = 2026092002
SEED_TOTAL = 128
SHARD_SIZE = 3
NODES = tuple(f"node{index:03d}" for index in range(1, 7))
RUNTIMES = {
    "v6": f"{BASE}/runtime-src-20260921-causal-target-v6",
    "v7": f"{BASE}/runtime-src-20260921-white6603-v7",
}
DISPATCHES = {
    "v6": (
        f"{BASE}/runs/teammate-response-current/single_scan_causal_target_choice_v6/"
        "7484e06074ef5a7a02a2bf6e06dcbca252777081040f75bfb2142616321c211a/"
        "attempt1/dispatch/dispatch.json"
    ),
    "v7": (
        f"{BASE}/runs/teammate-response-current/single_scan_direct_white6603_target_choice_v7/"
        "53a85ece0dbe56b95d072a47226ce6d2b5a73e540e1e9f350c1fe1cc04010c02/"
        "attempt1/dispatch/dispatch.json"
    ),
}
V6_RESULT_SHA = "8fc29addf548011ab72471758ae4fe96444633fca8df6146e1c31d7f0764f125"
V6_MODEL_SHA = "a82aa8858dc8b2e7ad453ed5494cc39476d122a4c4eeb23149a8a93fb034fe09"
NONWHITE_SPELL_IDS = (12723, 10202, 22482, 1680, 25346, 45961)
CATEGORIES = ("6603", *(str(spell) for spell in NONWHITE_SPELL_IDS), "other_nonwhite")


def seed_shards() -> list[tuple[int, int]]:
    return [
        (offset, min(SHARD_SIZE, SEED_TOTAL - offset))
        for offset in range(0, SEED_TOTAL, SHARD_SIZE)
    ]


def _runner_argv(*, version: str, store: str, result_sha: str, model_sha: str,
                 seed_offset: int, seed_count: int) -> list[str]:
    static = RUNTIMES["v6"]
    plan = build_plan_v1(
        code_root=RUNTIMES[version],
        simulator_root=static,
        frozen_dispatch=DISPATCHES[version],
        runtime_store=store,
        result_sha=result_sha,
        model_sha=model_sha,
        exact_build=f"{static}/results/responsive-team-v4/v34-doomguard-exact-fury-build-request.json",
        deployed_binding=f"{static}/results/responsive-team-v4/deployed-contra-runtime-binding-v1.951b8faa.json",
        bridge=f"{static}/bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64",
    )
    argv = list(plan["full_wave"]["argv"])
    for flag, value in (
        ("--source", "cat"),
        ("--simulator-seed", SEED_BASE + seed_offset),
        ("--teammate-seed", TEAMMATE_SEED_BASE + seed_offset),
        ("--seed-count", seed_count),
    ):
        argv[argv.index(flag) + 1] = str(value)
    return argv


def build_specs(*, v6_store: str, v7_store: str, v7_result_sha: str,
                v7_model_sha: str) -> list[dict]:
    specs = []
    for index, (offset, count) in enumerate(seed_shards()):
        specs.append({
            "schema": "development_d900_paired_seed_shard_spec/v1",
            "status": "PLANNED_NOT_EXECUTED",
            "shard_index": index,
            "assigned_node": NODES[index % len(NODES)],
            "seed_offset": offset,
            "seed_count": count,
            "v6": {
                "runtime_root": RUNTIMES["v6"],
                "result_sha": V6_RESULT_SHA,
                "model_sha": V6_MODEL_SHA,
                "argv": _runner_argv(
                    version="v6", store=v6_store, result_sha=V6_RESULT_SHA,
                    model_sha=V6_MODEL_SHA, seed_offset=offset, seed_count=count,
                ),
            },
            "v7": {
                "runtime_root": RUNTIMES["v7"],
                "result_sha": v7_result_sha,
                "model_sha": v7_model_sha,
                "argv": _runner_argv(
                    version="v7", store=v7_store, result_sha=v7_result_sha,
                    model_sha=v7_model_sha, seed_offset=offset, seed_count=count,
                ),
            },
        })
    return specs


def _early_damage(diagnostic: dict) -> dict[str, dict[str, dict[str, float | int]]]:
    targets = {
        str(index): {category: {"emitted_dmg_event_count": 0, "hits": 0, "applied_damage": 0.0}
                     for category in CATEGORIES}
        for index in range(3)
    }
    for row in diagnostic["groups"]:
        target = row["target_index"]
        if row["time_bucket"] != "through_9098ms" or row["event_type"] != "DMG" or target not in (0, 1, 2):
            continue
        spell = row["spell_id"]
        category = "6603" if spell == 6603 else str(spell) if spell in NONWHITE_SPELL_IDS else "other_nonwhite"
        item = targets[str(target)][category]
        item["emitted_dmg_event_count"] += row["event_count"]
        item["hits"] += row["applied_hit_count"]
        item["applied_damage"] += row["applied_damage"]
    return targets


def summarize_runner_output(result: dict, *, offset: int, count: int) -> list[dict]:
    if (
        result["wave_id"] != "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
        or result["historical_build_segment_id"] != "segment-0042"
        or result["attackability_mode"] != "OBSERVED_ONSET_UNTIL_SIM_DEATH"
        or result["current_state_route_focus_applied"] is not True
        or result["source_policy_ids"] != ["cat.fury.profile1"]
    ):
        raise ValueError("full-wave runner setup differs from frozen Cat d900")
    rows = result["paired_replays"]
    if len(rows) != count:
        raise ValueError("runner seed count differs from shard")
    compact = []
    for index, row in enumerate(rows):
        if (row["seed"], row["teammate_seed"]) != (
            SEED_BASE + offset + index, TEAMMATE_SEED_BASE + offset + index
        ):
            raise ValueError("runner seed pair differs from shard")
        drive = row["responsive_drive"]
        diagnostic = drive["target_event_diagnostic"]
        if sum(group["event_count"] for group in diagnostic["groups"]) != drive["responsive_event_count"] or not math.isclose(
            sum(group["applied_damage"] for group in diagnostic["groups"]),
            drive["responsive_applied_damage"], abs_tol=1e-6,
        ):
            raise ValueError("diagnostic APPLIED event/damage conservation differs")
        dead = {str(target): row["first_observed_dead_ms"].get(str(target)) for target in range(3)}
        observed_times = sorted({time for time in dead.values() if time is not None})
        death_order_groups = [
            [int(target) for target, time in dead.items() if time == observed]
            for observed in observed_times
        ]
        compact.append({
            "seed": row["seed"], "teammate_seed": row["teammate_seed"],
            "valid_development_completion": row["valid_development_completion"],
            "status": row["status"], "elapsed_ms": row["elapsed_ms"],
            "candidate_effective_damage": row["effective_damage"],
            "team_applied_damage_total": drive["responsive_applied_damage"],
            "first_observed_dead_ms": dead,
            "death_order_groups": death_order_groups,
            "early_applied_by_target_category": _early_damage(diagnostic),
        })
    return compact


def run_spec(spec: dict) -> dict:
    if spec["schema"] != "development_d900_paired_seed_shard_spec/v1":
        raise ValueError("unknown shard spec")
    rows_by_version = {}
    for version in ("v6", "v7"):
        binding = spec[version]
        env = dict(os.environ, PYTHONPATH=binding["runtime_root"], PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(binding["argv"], env=env, text=True,
                                   capture_output=True, timeout=900, check=False)
        if completed.returncode:
            raise RuntimeError(f"{version} shard {spec['shard_index']} failed: {completed.stderr[-2500:]}")
        rows_by_version[version] = summarize_runner_output(
            json.loads(completed.stdout), offset=spec["seed_offset"], count=spec["seed_count"]
        )
    return {
        "schema": "development_d900_paired_seed_shard_result/v1",
        "status": "DEVELOPMENT_ONLY_NOT_DPS_COMPARISON",
        "shard_index": spec["shard_index"],
        "seed_offset": spec["seed_offset"],
        "seed_count": spec["seed_count"],
        "model_bindings": {
            version: {key: spec[version][key] for key in ("result_sha", "model_sha")}
            for version in ("v6", "v7")
        },
        "v6": rows_by_version["v6"],
        "v7": rows_by_version["v7"],
        "attribution_boundary": (
            "Simulator emitted-DMG receipts do not expose Chronicle attribution_kind; "
            "emitted events and APPLIED hits are distinct, and neither is a verified "
            "DIRECT_FRIENDLY_PLAYER historical row count"
        ),
    }


def aggregate_shards(shards: list[dict]) -> dict:
    if len(shards) != len(seed_shards()):
        raise ValueError("not all 43 shards are complete")
    model_bindings = shards[0]["model_bindings"]
    pairs = []
    for expected_index, ((expected_offset, expected_count), shard) in enumerate(zip(seed_shards(), sorted(shards, key=lambda row: row["shard_index"]))):
        if shard["model_bindings"] != model_bindings:
            raise ValueError("paired shards use different model bindings")
        if (shard["shard_index"], shard["seed_offset"], shard["seed_count"]) != (
            expected_index, expected_offset, expected_count
        ):
            raise ValueError("shard coverage differs")
        if len(shard["v6"]) != expected_count or len(shard["v7"]) != expected_count:
            raise ValueError("paired shard row count differs")
        for v6, v7 in zip(shard["v6"], shard["v7"]):
            if (v6["seed"], v6["teammate_seed"]) != (v7["seed"], v7["teammate_seed"]):
                raise ValueError("v6/v7 seed pairing differs")
            pairs.append((v6, v7))
    if len(pairs) != SEED_TOTAL:
        raise ValueError("paired seed count differs")
    valid = [(v6, v7) for v6, v7 in pairs if v6["valid_development_completion"] and v7["valid_development_completion"]]
    candidate_deltas = [v7["candidate_effective_damage"] - v6["candidate_effective_damage"] for v6, v7 in valid]
    candidate_delta_sign_counts = {
        "v7_higher": sum(delta > 0 for delta in candidate_deltas),
        "equal": sum(delta == 0 for delta in candidate_deltas),
        "v7_lower": sum(delta < 0 for delta in candidate_deltas),
    }
    def percentile(values: list[int], probability: float) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    white_6603_three_target_tail = {}
    historical_order_given_all_three_dead = {}
    for version, index in (("v6", 0), ("v7", 1)):
        rows = [pair[index] for pair in pairs]
        emitted = [sum(row["early_applied_by_target_category"][str(target)]["6603"]["emitted_dmg_event_count"] for target in range(3)) for row in rows]
        hits = [sum(row["early_applied_by_target_category"][str(target)]["6603"]["hits"] for target in range(3)) for row in rows]
        white_6603_three_target_tail[version] = {
            "emitted_dmg_event_count": {
                "mean": statistics.mean(emitted), "p05": percentile(emitted, 0.05),
                "p50": percentile(emitted, 0.50), "p95": percentile(emitted, 0.95),
                "at_least_historical_71_count": sum(value >= 71 for value in emitted),
            },
            "applied_positive_hit_count": {
                "mean": statistics.mean(hits), "p05": percentile(hits, 0.05),
                "p50": percentile(hits, 0.50), "p95": percentile(hits, 0.95),
                "at_least_historical_61_count": sum(value >= 61 for value in hits),
            },
            "both_at_least_historical_71_emitted_and_61_hits_count": sum(
                a >= 71 and b >= 61 for a, b in zip(emitted, hits)
            ),
        }
    all_three_dead = {
        version: sum(
            all(time is not None for time in pair[index]["first_observed_dead_ms"].values())
            for pair in pairs
        ) for version, index in (("v6", 0), ("v7", 1))
    }
    all_three_dead["both"] = sum(
        all(all(time is not None for time in row["first_observed_dead_ms"].values()) for row in pair)
        for pair in pairs
    )
    for version, index in (("v6", 0), ("v7", 1)):
        historical_count = sum(pair[index]["death_order_groups"] == [[0], [1], [2]] for pair in pairs)
        denominator = all_three_dead[version]
        historical_order_given_all_three_dead[version] = {
            "all_three_dead_denominator": denominator,
            "historical_order_0_1_2_count": historical_count,
            "historical_order_0_1_2_fraction": historical_count / denominator if denominator else None,
        }
    death_order = {
        version: dict(Counter(
            "/".join("+".join(map(str, group)) for group in row["death_order_groups"])
            for row in (pair[0 if version == "v6" else 1] for pair in pairs)
        )) for version in ("v6", "v7")
    }
    categories = {}
    death_times = {}
    for target in range(3):
        key = str(target)
        categories[key] = {
            category: {
                "v6_mean_emitted_dmg_event_count": statistics.mean(
                    v6["early_applied_by_target_category"][key][category]["emitted_dmg_event_count"]
                    for v6, _ in pairs
                ),
                "v7_mean_emitted_dmg_event_count": statistics.mean(
                    v7["early_applied_by_target_category"][key][category]["emitted_dmg_event_count"]
                    for _, v7 in pairs
                ),
                "v6_mean_hits": statistics.mean(v6["early_applied_by_target_category"][key][category]["hits"] for v6, _ in pairs),
                "v7_mean_hits": statistics.mean(v7["early_applied_by_target_category"][key][category]["hits"] for _, v7 in pairs),
                "v6_mean_applied_damage": statistics.mean(v6["early_applied_by_target_category"][key][category]["applied_damage"] for v6, _ in pairs),
                "v7_mean_applied_damage": statistics.mean(v7["early_applied_by_target_category"][key][category]["applied_damage"] for _, v7 in pairs),
            } for category in CATEGORIES
        }
        observed = {
            version: [row["first_observed_dead_ms"][key] for row in (
                pair[0 if version == "v6" else 1] for pair in pairs
            ) if row["first_observed_dead_ms"][key] is not None]
            for version in ("v6", "v7")
        }
        death_times[key] = {
            version: {"observed_count": len(times),
                      "mean_ms": statistics.mean(times) if times else None}
            for version, times in observed.items()
        }
    return {
        "schema": "development_d900_paired_seed_aggregate/v1",
        "status": "DEVELOPMENT_ONLY_NOT_DPS_COMPARISON",
        "model_bindings": model_bindings,
        "paired_seed_count": len(pairs),
        "both_valid_count": len(valid),
        "candidate_effective_damage_delta_sign_counts": candidate_delta_sign_counts,
        "all_three_targets_dead_count": all_three_dead,
        "historical_order_given_all_three_dead": historical_order_given_all_three_dead,
        "white_6603_three_target_through_9098ms_tail": white_6603_three_target_tail,
        "historical_single_trace_white_6603_reference": {
            "through_ms_inclusive": 9098,
            "emitted_events": 71,
            "positive_logged_hits": 61,
            "interpretation": "One observed raid trace; compare tail frequency, not equality of means or simulator APPLIED damage.",
        },
        "candidate_effective_damage_delta_v7_minus_v6_mean": statistics.mean(candidate_deltas) if valid else None,
        "candidate_effective_damage_delta_v7_minus_v6_median": statistics.median(candidate_deltas) if valid else None,
        "first_observed_dead_time_by_target": death_times,
        "death_order_groups_frequency": death_order,
        "through_9098_by_target_category": categories,
        "historical_damage_boundary": "Historical logged raw amount and simulated APPLIED HP damage are not identical measures",
        "attribution_boundary": "Simulator emitted-DMG receipts lack Chronicle attribution_kind",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    plan = command.add_parser("plan")
    plan.add_argument("--v6-store", required=True)
    plan.add_argument("--v7-store", required=True)
    plan.add_argument("--v7-result-sha", required=True)
    plan.add_argument("--v7-model-sha", required=True)
    plan.add_argument("--spec-dir", type=Path, required=True)
    run = command.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    aggregate = command.add_parser("aggregate")
    aggregate.add_argument("--input-dir", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan":
        specs = build_specs(
            v6_store=args.v6_store, v7_store=args.v7_store,
            v7_result_sha=args.v7_result_sha, v7_model_sha=args.v7_model_sha,
        )
        args.spec_dir.mkdir(parents=True, exist_ok=True)
        for spec in specs:
            (args.spec_dir / f"shard-{spec['shard_index']:03d}.json").write_text(
                json.dumps(spec, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        print(json.dumps({"planned_shards": len(specs), "paired_seeds": SEED_TOTAL,
                          "spec_dir": str(args.spec_dir), "executed": False}))
    elif args.command == "run":
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        result = run_spec(spec)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"shard_index": result["shard_index"], "paired_seeds": result["seed_count"],
                          "small_result": str(args.output)}))
    else:
        shards = [
            json.loads((args.input_dir / f"shard-{index:03d}.json").read_text(encoding="utf-8"))
            for index in range(len(seed_shards()))
        ]
        result = aggregate_shards(shards)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"paired_seed_count": result["paired_seed_count"],
                          "small_aggregate": str(args.output)}))


if __name__ == "__main__":
    main()
