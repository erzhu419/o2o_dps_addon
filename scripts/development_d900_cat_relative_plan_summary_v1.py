"""Keep a small source-bound summary of one development d900 proposal plan."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(plan: dict, *, plan_path: Path, plan_bytes: int) -> dict:
    candidates = plan["candidates"]
    freeze = plan["freeze"]
    illegal = [
        row["candidate_id"] for row in candidates
        if row["replacement"].get("target_index") not in (
            None, row["guard"]["all_of"]["target_attackable_is"]["target_index"]
        )
        or row["guard"]["all_of"]["target_attackable_is"]["value"] is not True
        or row["replacement"] == row["cat_decision_is"]
    ]
    valid = (
        plan["status"] == "DEVELOPMENT_ONLY_PLAN_NOT_EVALUATED"
        and 0 < len(candidates) <= 256
        and len({row["candidate_id"] for row in candidates}) == len(candidates)
        and not illegal
    )
    return {
        "schema": "development_d900_cat_relative_plan_summary/v1",
        "status": "PROPOSAL_PLAN_VALID" if valid else "PROPOSAL_PLAN_INVALID",
        "comparison_authorized": False,
        "plan_path": str(plan_path),
        "plan_bytes": plan_bytes,
        "nonzero_candidate_count": len(candidates),
        "proposal_exact_cat": plan["proposal_exact_cat"],
        "prefix_candidate_counts": dict(sorted(Counter(
            str(row["decision_index_is"]) for row in candidates
        ).items(), key=lambda item: int(item[0]))),
        "source_focus_target_counts": dict(sorted(Counter(
            str(row["guard"]["all_of"]["target_attackable_is"]["target_index"])
            for row in candidates
        ).items())),
        "explicit_retarget_candidate_count": sum(
            row["replacement"].get("target_index") is not None for row in candidates
        ),
        "illegal_candidate_ids": illegal,
        "seed_cohort_counts": {
            name: len(rows) for name, rows in plan["seed_cohorts"].items()
        },
        "binding": {
            "wave_id": freeze["case_source"]["wave_id"],
            "native_target_guids": freeze["native_target_guids"],
            "attackability_mode": freeze["attackability_mode"],
            "dynamic_config_content_sha256": freeze["dynamic_config_content_sha256"],
            "cat_source_policy_id": freeze["cat_source_policy_id"],
            "model_result_sha": freeze["model_result_sha"],
            "model_sha": freeze["model_sha"],
            "discovery_seed": plan["discovery_seed"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(
        json.loads(args.plan.read_text(encoding="utf-8")),
        plan_path=args.plan,
        plan_bytes=args.plan.stat().st_size,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result["status"],
                      "nonzero_candidate_count": result["nonzero_candidate_count"]}))
    if result["status"] != "PROPOSAL_PLAN_VALID":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
