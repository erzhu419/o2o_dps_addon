"""Read-only d900 first-deadline samples from the frozen teammate SQLite store."""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    ABLATION_D, DynamicTeamRuntimeV1, _actor_metadata,
)
from o2o_dps.responsive_team_bridge_adapter_v1 import _substream_seed
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1


WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
FOCAL = "0x0000000000576754"
HEALTH = {
    "0xF13000F240276CB6": 109896,
    "0xF13000F244276CB4": 96047,
    "0xF13000F245276CB3": 96757,
}
INTRODUCED = {
    "0xF13000F240276CB6": 0,
    "0xF13000F244276CB4": 1000,
    "0xF13000F245276CB3": 1000,
}
STAGE5_SHA = "75fe0c215d09b6b0ca5e3a2c46b5397f70b4b5de60f8740dceae1b2465576bc0"
RESULT_SHA = "1389ce06506b89d7c2040befdf24320bb0eb557d798dded4b0edb9dfa935bfe4"
MODEL_SHA = "dc4022d86393823d8df67f890805348438126735e73c5d40234cd915e45da7f2"
COMPONENT = "114175c6c5af0b03978551442d51dcbbf325f8ba2af8b7e3bef468bc7da8fa68"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--runtime-store", type=Path, required=True)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--expected-model-sha", default=MODEL_SHA)
    parser.add_argument("--seed-count", type=int, default=1)
    args = parser.parse_args()
    with gzip.open(args.stage5, "rt", encoding="utf-8") as handle:
        for line in handle:
            wave = json.loads(line)
            if wave.get("wave", {}).get("wave_id") == WAVE:
                break
        else:
            raise ValueError("selected Stage5 wave not found")
    actors = tuple(
        _actor_metadata(row) for row in wave["players"]
        if row.get("exact_trace_indices")
    )
    runtime = DynamicTeamRuntimeV1(
        actors=actors, target_health_by_guid=HEALTH,
        target_introduced_at_ms_by_guid=INTRODUCED,
    )
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=RESULT_SHA,
        expected_model_content_sha256=args.expected_model_sha,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=COMPONENT,
            declared_held_out=True,
        ),
    )
    rows = []
    per_seed = []
    try:
        for seed_index in range(args.seed_count):
            seed = args.teammate_seed + seed_index
            seed_rows = []
            for actor in sorted(runtime.actors):
                if actor == FOCAL:
                    continue
                metadata = runtime.actors[actor].metadata
                state = runtime.snapshot_for_actor(actor)
                sampled = loaded.model.sample_delay(
                    actor=metadata,
                    timing_state=state,
                    rng=random.Random(_substream_seed(seed, actor, 0, "delay")),
                )
                seed_rows.append({
                    "actor_guid": actor,
                    "class": metadata["class"],
                    "context_level": sampled["context_level"],
                    "context": sampled["context"],
                    "support": sampled["support"],
                    "sampled_first_delay_ms": sampled["delay_ms"],
                    "delay_bucket": sampled["delay_bucket"],
                })
            if seed_index == 0:
                rows = seed_rows
            delays = [row["sampled_first_delay_ms"] for row in seed_rows]
            per_seed.append({
                "seed": seed,
                "zero_count": sum(value == 0 for value in delays),
                "before_3000_count": sum(value < 3000 for value in delays),
            })
    finally:
        loaded.model.close()
    delays = [row["sampled_first_delay_ms"] for row in rows]
    print(json.dumps({
        "schema": "development_d900_initial_delay_probe/v1",
        "status": "MODEL_INITIAL_TIMING_DIAGNOSTIC_NOT_POLICY_INPUT",
        "wave_id": WAVE,
        "teammate_seed": args.teammate_seed,
        "seed_count": args.seed_count,
        "model_content_sha256": args.expected_model_sha,
        "teammate_count": len(rows),
        "sampled_first_delay_zero_count": sum(value == 0 for value in delays),
        "sampled_first_delay_before_3000_count": sum(value < 3000 for value in delays),
        "context_level_counts": dict(Counter(row["context_level"] for row in rows)),
        "before_3000_across_seeds": {
            "mean": statistics.mean(row["before_3000_count"] for row in per_seed),
            "minimum": min(row["before_3000_count"] for row in per_seed),
            "maximum": max(row["before_3000_count"] for row in per_seed),
            "historical_d900_count": 17,
        },
        "per_seed": per_seed,
        "rows": rows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
