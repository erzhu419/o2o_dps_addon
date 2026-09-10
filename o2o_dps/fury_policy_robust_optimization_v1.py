"""Worst-branch Fury search across explicit encounter hypotheses.

Armor and target level branches are sensitivity alternatives, not additional
observations.  Candidate selection therefore maximizes the worst training
margin against the strongest source-derived baseline instead of averaging the
branches into a made-up probability distribution.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_policy_optimization_v1 import (
    DEFAULT_BRIDGE,
    FuryPolicyOptimizationError,
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    PolicyScenario,
    _evaluate_adapters,
    _paired_comparison,
    _seed_tuple,
    _write_json,
    default_parameter_grid,
    scenarios_from_catalog,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_robust_optimization_v1.json"
)


def _ranking_map(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row["expert_id"]): row
        for row in result["ranking"]
        if isinstance(row, Mapping)
    }


def optimize_fury_policy_robust(
    bridge: Any,
    scenario_groups: Mapping[str, Sequence[PolicyScenario]],
    *,
    training_seeds: Iterable[int],
    validation_seeds: Iterable[int],
    candidates: Iterable[FuryPolicyParameters] | None = None,
) -> JSONMap:
    """Select one fixed policy by its weakest explicit hypothesis branch."""

    if not scenario_groups:
        raise ValueError("scenario_groups must not be empty")
    groups: dict[str, tuple[PolicyScenario, ...]] = {}
    all_scenario_ids: set[str] = set()
    for branch_id, scenarios in scenario_groups.items():
        if not str(branch_id).strip():
            raise ValueError("branch ids must be nonempty")
        values = tuple(scenarios)
        if not values:
            raise ValueError(f"scenario branch {branch_id!r} is empty")
        for scenario in values:
            if scenario.scenario_id in all_scenario_ids:
                raise ValueError(f"duplicate scenario id: {scenario.scenario_id}")
            all_scenario_ids.add(scenario.scenario_id)
        groups[str(branch_id)] = values

    train = _seed_tuple(training_seeds, "training_seeds")
    validation = _seed_tuple(validation_seeds, "validation_seeds")
    overlap = sorted(set(train).intersection(validation))
    if overlap:
        raise ValueError(f"training and validation seeds overlap: {overlap}")

    parameter_values = tuple(candidates or default_parameter_grid())
    if not parameter_values:
        raise ValueError("candidates must not be empty")
    if len({value.policy_id for value in parameter_values}) != len(parameter_values):
        raise ValueError("candidate policy IDs must be unique")

    baselines = (CatFurySourceAdapter(), ContraDeployedSourceAdapter())
    candidate_adapters = tuple(FuryTunedPolicyAdapter(value) for value in parameter_values)
    training_branches: list[JSONMap] = []
    score_rows: dict[str, list[tuple[float, float]]] = {
        adapter.expert_id: [] for adapter in candidate_adapters
    }
    eligible = set(score_rows)

    for branch_id in sorted(groups):
        result = _evaluate_adapters(
            bridge, groups[branch_id], (*baselines, *candidate_adapters), train
        )
        ranking = _ranking_map(result)
        strongest = max(
            (ranking[adapter.expert_id] for adapter in baselines),
            key=lambda row: (float(row["weighted_mean_dps"]), str(row["expert_id"])),
        )
        baseline_dps = float(strongest["weighted_mean_dps"])
        for adapter in candidate_adapters:
            row = ranking[adapter.expert_id]
            if not bool(row["candidate_faithful"]):
                eligible.discard(adapter.expert_id)
                continue
            candidate_dps = float(row["weighted_mean_dps"])
            score_rows[adapter.expert_id].append(
                (candidate_dps - baseline_dps, candidate_dps)
            )
        training_branches.append(
            {
                "branch_id": branch_id,
                "scenario_ids": [value.scenario_id for value in groups[branch_id]],
                "strongest_baseline_id": strongest["expert_id"],
                "ranking": result["ranking"],
                "rollout_count": len(result["rollouts"]),
            }
        )

    eligible = {
        policy_id
        for policy_id in eligible
        if len(score_rows[policy_id]) == len(groups)
    }
    if not eligible:
        raise FuryPolicyOptimizationError(
            "no candidate completed every training branch without lane omissions"
        )

    robust_ranking: list[JSONMap] = []
    for policy_id in eligible:
        values = score_rows[policy_id]
        margins = [value[0] for value in values]
        dps_values = [value[1] for value in values]
        parameters = next(
            asdict(adapter.parameters)
            for adapter in candidate_adapters
            if adapter.expert_id == policy_id
        )
        robust_ranking.append(
            {
                "policy_id": policy_id,
                "minimum_branch_margin_dps": min(margins),
                "mean_branch_margin_dps": fmean(margins),
                "mean_branch_candidate_dps": fmean(dps_values),
                "parameters": parameters,
            }
        )
    robust_ranking.sort(
        key=lambda row: (
            -float(row["minimum_branch_margin_dps"]),
            -float(row["mean_branch_margin_dps"]),
            -float(row["mean_branch_candidate_dps"]),
            str(row["policy_id"]),
        )
    )
    selected_id = str(robust_ranking[0]["policy_id"])
    selected = next(
        adapter for adapter in candidate_adapters if adapter.expert_id == selected_id
    )

    validation_branches: list[JSONMap] = []
    for branch_id in sorted(groups):
        result = _evaluate_adapters(
            bridge, groups[branch_id], (*baselines, selected), validation
        )
        ranking = _ranking_map(result)
        candidate_row = ranking[selected_id]
        strongest = max(
            (ranking[adapter.expert_id] for adapter in baselines),
            key=lambda row: (float(row["weighted_mean_dps"]), str(row["expert_id"])),
        )
        paired = _paired_comparison(
            result["rollouts"], selected_id, str(strongest["expert_id"])
        )
        gate = (
            bool(candidate_row["candidate_faithful"])
            and float(candidate_row["weighted_mean_dps"])
            > float(strongest["weighted_mean_dps"])
            and int(paired["wins"]) > int(paired["losses"])
        )
        validation_branches.append(
            {
                "branch_id": branch_id,
                "scenario_ids": [value.scenario_id for value in groups[branch_id]],
                "strongest_baseline_id": strongest["expert_id"],
                "ranking": result["ranking"],
                "paired_selected_vs_strongest_baseline": paired,
                "branch_gate_passed": gate,
                "rollout_count": len(result["rollouts"]),
            }
        )

    robust_gate = all(row["branch_gate_passed"] for row in validation_branches)
    return {
        "schema_version": 1,
        "kind": "fury_policy_robust_optimization_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection_contract": {
            "training_seeds": list(train),
            "validation_seeds": list(validation),
            "disjoint": True,
            "branch_semantics": "explicit sensitivity alternatives, not IID samples",
            "selection_metric": (
                "maximize minimum training DPS margin versus each branch's strongest baseline; "
                "then mean margin"
            ),
            "validation_gate": (
                "every branch has zero candidate lane omissions, higher weighted mean DPS, "
                "and more paired wins than losses"
            ),
        },
        "branch_count": len(groups),
        "candidate_count": len(candidate_adapters),
        "eligible_candidate_count": len(eligible),
        "selected_policy_id": selected_id,
        "selected_parameters": asdict(selected.parameters),
        "training_robust_ranking": robust_ranking,
        "training_branches": training_branches,
        "validation_branches": validation_branches,
        "passed_validation_branch_count": sum(
            bool(row["branch_gate_passed"]) for row in validation_branches
        ),
        "simulator_robust_gate_passed": robust_gate,
        "deployment_allowed": False,
        "next_gate": (
            "held-out Chronicle encounter families and calibrated real-game shadow evaluation"
        ),
        "claims_excluded": [
            "numeric armor or level identified by Chronicle",
            "exact Cat or Contra Lua execution",
            "real-game DPS superiority",
            "independent evidence from repeated simulator seeds",
        ],
    }


def _load_candidates(path: Path | None) -> tuple[FuryPolicyParameters, ...]:
    if path is None:
        return default_parameter_grid()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or value.get("kind") != "fury_policy_candidate_grid_v1":
        raise FuryPolicyOptimizationError("invalid Fury candidate config kind")
    rows = value.get("candidates")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
        raise FuryPolicyOptimizationError("candidate config must contain object candidates")
    return tuple(FuryPolicyParameters(**dict(row)) for row in rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-catalog", type=Path, required=True)
    parser.add_argument("--armor-hypothesis", type=float, action="append", required=True)
    parser.add_argument("--level-hypothesis", type=int, action="append", required=True)
    parser.add_argument(
        "--layout-side", choices=("upper", "lower", "evidence_bounded"), default="evidence_bounded"
    )
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--train-seed", type=int, action="append", dest="train_seeds")
    parser.add_argument("--validation-seed", type=int, action="append", dest="validation_seeds")
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--candidate-config", type=Path)
    args = parser.parse_args(argv)

    catalog = json.loads(args.scenario_catalog.read_text(encoding="utf-8"))
    groups: dict[str, tuple[PolicyScenario, ...]] = {}
    for armor in args.armor_hypothesis:
        for level in args.level_hypothesis:
            branch_id = f"armor-{armor:g}__level-{level}"
            groups[branch_id] = scenarios_from_catalog(
                catalog,
                armor_hypothesis=armor,
                level_hypothesis=level,
                layout_side=args.layout_side,
            )
    candidates = _load_candidates(args.candidate_config)
    if args.max_candidates is not None:
        if args.max_candidates <= 0:
            raise ValueError("max-candidates must be positive")
        candidates = candidates[: args.max_candidates]
    train = tuple(args.train_seeds or (2026090811, 2026090812, 2026090813, 2026090814))
    validation = tuple(
        args.validation_seeds or (2026090821, 2026090822, 2026090823, 2026090824)
    )
    with SimulatorBridge(args.bridge) as bridge:
        artifact = optimize_fury_policy_robust(
            bridge,
            groups,
            training_seeds=train,
            validation_seeds=validation,
            candidates=candidates,
        )
    _write_json(args.output, artifact)
    print(
        json.dumps(
            {
                "selected_policy_id": artifact["selected_policy_id"],
                "branch_count": artifact["branch_count"],
                "passed_validation_branch_count": artifact[
                    "passed_validation_branch_count"
                ],
                "simulator_robust_gate_passed": artifact[
                    "simulator_robust_gate_passed"
                ],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
