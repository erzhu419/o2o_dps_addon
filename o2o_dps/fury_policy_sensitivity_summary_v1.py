"""Compact robustness summary for Fury policy sensitivity branches.

The optimizer intentionally evaluates one explicit armor/level hypothesis at a
time.  This module combines those small artifacts without treating the
hypotheses as observations or averaging them into a fictitious target model.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence


class SensitivitySummaryError(ValueError):
    """Raised when optimizer artifacts do not share the required contract."""


def _hypothesis(artifact: Mapping[str, Any]) -> tuple[float, int]:
    provenance = artifact.get("scenario_contract", {}).get("provenance", [])
    if not isinstance(provenance, list) or not provenance:
        raise SensitivitySummaryError("scenario provenance is missing")
    observed: set[tuple[float, int]] = set()
    for row in provenance:
        hypotheses = row.get("provenance", {}).get("target_hypotheses", {})
        try:
            armor = float(hypotheses["armor"]["value"])
            level = int(hypotheses["level"]["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SensitivitySummaryError(
                "scenario target hypotheses are malformed"
            ) from exc
        observed.add((armor, level))
    if len(observed) != 1:
        raise SensitivitySummaryError(
            "one optimizer artifact must contain exactly one armor/level branch"
        )
    return next(iter(observed))


def _ranking(artifact: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = artifact.get("validation", {}).get("ranking", [])
    if not isinstance(rows, list):
        raise SensitivitySummaryError("validation ranking is missing")
    return {
        str(row["expert_id"]): row
        for row in rows
        if isinstance(row, Mapping) and row.get("expert_id")
    }


def summarize_sensitivity_artifacts(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not artifacts:
        raise SensitivitySummaryError("at least one optimizer artifact is required")
    if sources is not None and len(sources) != len(artifacts):
        raise SensitivitySummaryError("sources must align with artifacts")

    branches: list[dict[str, Any]] = []
    seen: set[tuple[float, int]] = set()
    policy_ids: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if artifact.get("kind") != "fury_policy_optimization_v1":
            raise SensitivitySummaryError("unexpected optimizer artifact kind")
        armor, level = _hypothesis(artifact)
        key = (armor, level)
        if key in seen:
            raise SensitivitySummaryError(f"duplicate sensitivity branch: {key}")
        seen.add(key)

        candidate_id = str(artifact.get("selected_policy_id", ""))
        baseline_id = str(artifact.get("strongest_validation_baseline_id", ""))
        if not candidate_id or not baseline_id:
            raise SensitivitySummaryError("candidate or baseline id is missing")
        policy_ids.add(candidate_id)
        ranking = _ranking(artifact)
        if candidate_id not in ranking or baseline_id not in ranking:
            raise SensitivitySummaryError("selected policy is absent from validation ranking")
        candidate = ranking[candidate_id]
        baseline = ranking[baseline_id]
        contra = ranking.get("contra.deployed.fury.raid_a")
        paired = artifact.get("paired_selected_vs_strongest_baseline", {})
        mean_delta = float(paired.get("mean_paired_dps", math.nan))
        if not math.isfinite(mean_delta):
            raise SensitivitySummaryError("paired mean DPS is not finite")

        branches.append(
            {
                "armor_hypothesis": armor,
                "level_hypothesis": level,
                "hypothesis_status": "INFERRED",
                "candidate_id": candidate_id,
                "strongest_baseline_id": baseline_id,
                "candidate_weighted_mean_dps": float(candidate["weighted_mean_dps"]),
                "baseline_weighted_mean_dps": float(baseline["weighted_mean_dps"]),
                "contra_weighted_mean_dps": (
                    float(contra["weighted_mean_dps"]) if contra is not None else None
                ),
                "mean_paired_dps": mean_delta,
                "pair_count": int(paired.get("pair_count", 0)),
                "wins": int(paired.get("wins", 0)),
                "ties": int(paired.get("ties", 0)),
                "losses": int(paired.get("losses", 0)),
                "candidate_omitted_lane_count": int(
                    candidate.get("omitted_lane_count", 0)
                ),
                "branch_gate_passed": bool(
                    artifact.get("simulator_improvement_gate_passed", False)
                ),
                "source": sources[index] if sources is not None else None,
            }
        )

    branches.sort(key=lambda row: (row["armor_hypothesis"], row["level_hypothesis"]))
    deltas = [row["mean_paired_dps"] for row in branches]
    failed = [
        {
            "armor_hypothesis": row["armor_hypothesis"],
            "level_hypothesis": row["level_hypothesis"],
            "mean_paired_dps": row["mean_paired_dps"],
            "wins": row["wins"],
            "losses": row["losses"],
        }
        for row in branches
        if not row["branch_gate_passed"]
    ]
    candidate_consistent = len(policy_ids) == 1
    robust_gate = (
        candidate_consistent
        and not failed
        and all(row["candidate_omitted_lane_count"] == 0 for row in branches)
    )
    return {
        "schema_version": 1,
        "kind": "fury_policy_sensitivity_summary_v1",
        "contract": {
            "armor_and_level_are_explicit_hypotheses": True,
            "chronicle_does_not_identify_numeric_armor_or_level_here": True,
            "branches_are_not_averaged_into_a_probability_distribution": True,
            "same_validation_seed_set_across_branches_is_correlated": True,
        },
        "branch_count": len(branches),
        "candidate_policy_ids": sorted(policy_ids),
        "candidate_consistent": candidate_consistent,
        "gate_passed_branch_count": len(branches) - len(failed),
        "gate_failed_branch_count": len(failed),
        "simulator_sensitivity_gate_passed": robust_gate,
        "deployment_allowed": False,
        "paired_delta_dps_diagnostic": {
            "minimum_branch_mean": min(deltas),
            "median_branch_mean": statistics.median(deltas),
            "maximum_branch_mean": max(deltas),
            "negative_mean_branch_count": sum(value < 0 for value in deltas),
            "aggregate_wins": sum(row["wins"] for row in branches),
            "aggregate_ties": sum(row["ties"] for row in branches),
            "aggregate_losses": sum(row["losses"] for row in branches),
            "not_an_independent_pooled_test": True,
        },
        "failed_branches": failed,
        "branches": branches,
        "next_gate": (
            "increase held-out seeds and optimize a level-aware or robust candidate; "
            "then repeat after the Contra adapter has zero avoidable legality omissions"
        ),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--pattern", default="fury_policy_sensitivity_v1_a*_l*.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    paths = sorted(args.input_dir.glob(args.pattern))
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    summary = summarize_sensitivity_artifacts(
        artifacts, sources=[str(path) for path in paths]
    )
    _write_json(args.output, summary)
    print(
        json.dumps(
            {
                "branch_count": summary["branch_count"],
                "gate_passed_branch_count": summary["gate_passed_branch_count"],
                "gate_failed_branch_count": summary["gate_failed_branch_count"],
                "simulator_sensitivity_gate_passed": summary[
                    "simulator_sensitivity_gate_passed"
                ],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
