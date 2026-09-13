"""One actual development-only candidate -> rollout -> parameter update loop.

The four-policy whole-wave panel is the evaluator.  This module makes joint
queue-threshold/cancel-margin proposals, executes or consumes their paired
panels, and changes the incumbent only after every compared lane completed on
the same seeds.  A small local run is a wiring smoke, not a selection result.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import (
    PARAMETER_AXES,
    FuryCatGapPolicyParametersV1,
)
from .development_wave_panel_v1 import (
    ANCHOR_PARAMETERS,
    run_development_wave_panel_v1,
)
from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS


SCHEMA = "development_wave_iteration/v1"
MIN_UPDATE_SEEDS = 32
JOINT_AXES = ("heroic_strike_base_rage", "queue_cancel_margin_rage")
PANEL_COMPLETE = "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY"


def _validated_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    return asdict(FuryCatGapPolicyParametersV1.from_mapping(value))


def _nearby_values(name: str, center: int) -> list[int]:
    allowed = dict(PARAMETER_AXES)[name]
    return sorted((value for value in allowed if value != center), key=lambda value: (abs(value - center), value))[:2]


def propose_development_wave_candidates_v1(
    anchor_parameters: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return incumbent and distinct two-axis queue proposals (up to four)."""

    anchor = _validated_parameters(ANCHOR_PARAMETERS if anchor_parameters is None else anchor_parameters)
    incumbent = FuryCatGapPolicyParametersV1.from_mapping(anchor)
    proposals = [{
        "candidate_id": incumbent.candidate_id,
        "parameters": anchor,
        "changed_axes": [],
        "origin": "INCUMBENT",
    }]
    threshold_axis, margin_axis = JOINT_AXES
    for threshold in _nearby_values(threshold_axis, anchor[threshold_axis]):
        for margin in _nearby_values(margin_axis, anchor[margin_axis]):
            parameters = dict(anchor)
            parameters[threshold_axis] = threshold
            parameters[margin_axis] = margin
            candidate = FuryCatGapPolicyParametersV1.from_mapping(parameters)
            proposals.append({
                "candidate_id": candidate.candidate_id,
                "parameters": parameters,
                "changed_axes": list(JOINT_AXES),
                "origin": "JOINT_QUEUE_THRESHOLD_CANCEL_MARGIN",
            })
    return proposals


def _valid_score(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _panel_scores(
    panel: Mapping[str, Any], *, seed: int, proposal: Mapping[str, Any],
) -> tuple[dict[str, float], str | None]:
    if (
        panel.get("schema") != "development_wave_four_policy_panel/v1"
        or panel.get("candidate_kind") != "anchor_13d"
        or panel.get("master_seed") != seed
        or panel.get("anchor_parameters") != proposal["parameters"]
        or panel.get("status") != PANEL_COMPLETE
        or panel.get("four_way_complete") is not True
        or panel.get("completed_count") != 4
    ):
        return {}, "PANEL_NOT_MATCHED_FOUR_WAY_COMPLETE"
    rows = panel.get("rows")
    if not isinstance(rows, list) or len(rows) != 4:
        return {}, "PANEL_ROW_SET_INVALID"
    expected = set(BASELINE_IDS) | {proposal["candidate_id"]}
    actual = [row.get("policy_id") for row in rows if isinstance(row, Mapping)]
    if len(actual) != 4 or set(actual) != expected or len(set(actual)) != 4:
        return {}, "PANEL_POLICY_IDENTITIES_INVALID"
    scores = {}
    for row in rows:
        policy_id = row["policy_id"]
        if (
            row.get("status") != "COMPLETED"
            or row.get("role") != ("CANDIDATE" if policy_id == proposal["candidate_id"] else "BASELINE")
            or not _valid_score(row.get("own_effective_damage"))
            or not _valid_score(row.get("ttk_ms"))
            or row["ttk_ms"] == 0
        ):
            return {}, "PANEL_LANE_INCOMPLETE_OR_SCORE_INVALID"
        scores[policy_id] = float(row["own_effective_damage"])
    return scores, None


def reduce_development_wave_iteration_v1(
    *, proposals: Sequence[Mapping[str, Any]], master_seeds: Sequence[int],
    panels_by_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
    min_update_seeds: int = MIN_UPDATE_SEEDS,
) -> dict[str, Any]:
    """Select a new vector only from complete matched whole-wave evidence."""

    if not proposals or proposals[0].get("origin") != "INCUMBENT":
        raise ValueError("first proposal must be the incumbent")
    if not master_seeds or len(set(master_seeds)) != len(master_seeds):
        raise ValueError("master_seeds must be nonempty and unique")
    if min_update_seeds < MIN_UPDATE_SEEDS:
        raise ValueError(f"min_update_seeds must be at least {MIN_UPDATE_SEEDS}")
    incumbent = proposals[0]
    proposal_ids = [proposal["candidate_id"] for proposal in proposals]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("candidate proposals must be distinct")
    evidence: dict[str, dict[int, dict[str, float]]] = {}
    outcome_profiles: dict[str, dict[int, tuple[float, float]]] = {}
    failures: list[dict[str, Any]] = []
    reference_case: dict[int, Any] = {}
    reference_sim_seed: dict[int, Any] = {}
    reference_baselines: dict[int, dict[str, float]] = {}
    for proposal in proposals:
        candidate_id = proposal["candidate_id"]
        _validated_parameters(proposal["parameters"])
        panels = panels_by_candidate.get(candidate_id, [])
        by_seed = {panel.get("master_seed"): panel for panel in panels if isinstance(panel, Mapping)}
        if len(by_seed) != len(panels):
            failures.append({"candidate_id": candidate_id, "reason": "DUPLICATE_OR_MALFORMED_SEED_PANEL"})
        evidence[candidate_id] = {}
        outcome_profiles[candidate_id] = {}
        for seed in master_seeds:
            panel = by_seed.get(seed)
            if panel is None:
                failures.append({"candidate_id": candidate_id, "master_seed": seed, "reason": "PANEL_NOT_RUN"})
                continue
            scores, reason = _panel_scores(panel, seed=seed, proposal=proposal)
            if reason is not None:
                failures.append({
                    "candidate_id": candidate_id, "master_seed": seed,
                    "reason": reason, "detail": panel.get("error"),
                })
                continue
            if seed not in reference_case:
                reference_case[seed] = panel.get("case")
                reference_sim_seed[seed] = panel.get("simulator_seed")
                reference_baselines[seed] = {policy_id: scores[policy_id] for policy_id in BASELINE_IDS}
            elif (
                panel.get("case") != reference_case[seed]
                or panel.get("simulator_seed") != reference_sim_seed[seed]
                or any(scores[policy_id] != reference_baselines[seed][policy_id] for policy_id in BASELINE_IDS)
            ):
                failures.append({"candidate_id": candidate_id, "master_seed": seed, "reason": "PAIRED_CASE_SEED_OR_BASELINE_MISMATCH"})
                continue
            evidence[candidate_id][seed] = scores
            candidate_row = next(
                row for row in panel["rows"] if row["policy_id"] == candidate_id
            )
            outcome_profiles[candidate_id][seed] = (
                scores[candidate_id], float(candidate_row["ttk_ms"])
            )

    summary = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_UPPER_KARA_DEVELOPMENT_ONLY",
        "master_seeds": list(master_seeds),
        "candidate_count": len(proposals),
        "joint_proposal_count": sum(len(proposal["changed_axes"]) == 2 for proposal in proposals),
        "distinct_outcome_profile_count": None,
        "outcome_equivalence_groups": [],
        "incumbent_candidate_id": incumbent["candidate_id"],
        "incumbent_parameters": dict(incumbent["parameters"]),
        "next_candidate_id": incumbent["candidate_id"],
        "next_parameters": dict(incumbent["parameters"]),
        "updated": False,
        "comparison_ready": False,
        "live_fidelity": False,
        "failures": failures,
        "candidate_results": [],
    }
    if failures:
        summary["status"] = "EVIDENCE_INCOMPLETE_NO_UPDATE"
        return summary

    equivalent: dict[tuple[tuple[float, float], ...], list[str]] = {}
    for proposal in proposals:
        candidate_id = proposal["candidate_id"]
        profile = tuple(outcome_profiles[candidate_id][seed] for seed in master_seeds)
        equivalent.setdefault(profile, []).append(candidate_id)
    summary["distinct_outcome_profile_count"] = len(equivalent)
    summary["outcome_equivalence_groups"] = list(equivalent.values())

    incumbent_scores = evidence[incumbent["candidate_id"]]
    for proposal in proposals:
        candidate_id = proposal["candidate_id"]
        deltas = [
            evidence[candidate_id][seed][candidate_id]
            - incumbent_scores[seed][incumbent["candidate_id"]]
            for seed in master_seeds
        ]
        baseline_deltas = {
            baseline_id: sum(
                evidence[candidate_id][seed][candidate_id]
                - evidence[candidate_id][seed][baseline_id]
                for seed in master_seeds
            ) / len(master_seeds)
            for baseline_id in BASELINE_IDS
        }
        summary["candidate_results"].append({
            "candidate_id": candidate_id,
            "changed_axes": list(proposal["changed_axes"]),
            "mean_own_effective_damage": sum(
                evidence[candidate_id][seed][candidate_id] for seed in master_seeds
            ) / len(master_seeds),
            "mean_paired_damage_vs_incumbent": sum(deltas) / len(deltas),
            "mean_paired_damage_vs_baselines": baseline_deltas,
        })
    if len(master_seeds) < min_update_seeds:
        summary["status"] = "SMOKE_ONLY_NO_UPDATE"
        return summary
    best = max(
        enumerate(summary["candidate_results"]),
        key=lambda indexed: (indexed[1]["mean_paired_damage_vs_incumbent"], -indexed[0]),
    )[1]
    if best["candidate_id"] == incumbent["candidate_id"] or best["mean_paired_damage_vs_incumbent"] <= 0:
        summary["status"] = "NO_POSITIVE_UPDATE_DEVELOPMENT_ONLY"
        return summary
    selected = next(proposal for proposal in proposals if proposal["candidate_id"] == best["candidate_id"])
    summary.update({
        "status": "UPDATED_DEVELOPMENT_ONLY",
        "updated": True,
        "next_candidate_id": selected["candidate_id"],
        "next_parameters": dict(selected["parameters"]),
        "selected_mean_paired_damage_vs_incumbent": best["mean_paired_damage_vs_incumbent"],
    })
    return summary


def run_development_wave_iteration_v1(
    *, master_seeds: Sequence[int],
    anchor_parameters: Mapping[str, Any] | None = None,
    panel_runner: Callable[..., Mapping[str, Any]] = run_development_wave_panel_v1,
    min_update_seeds: int = MIN_UPDATE_SEEDS,
    **panel_kwargs: Any,
) -> dict[str, Any]:
    """Execute all candidate/seed panels and immediately reduce their results."""

    proposals = propose_development_wave_candidates_v1(anchor_parameters)
    panels_by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for proposal in proposals:
        panels_by_candidate[proposal["candidate_id"]] = []
        for seed in master_seeds:
            try:
                panel = panel_runner(
                    master_seed=seed,
                    candidate_kind="anchor_13d",
                    anchor_parameters=proposal["parameters"],
                    **panel_kwargs,
                )
            except Exception as error:
                panel = {
                    "schema": "development_wave_four_policy_panel/v1",
                    "candidate_kind": "anchor_13d",
                    "anchor_parameters": proposal["parameters"],
                    "master_seed": seed,
                    "status": "FAILED",
                    "error": f"{type(error).__name__}: {error}",
                }
            panels_by_candidate[proposal["candidate_id"]].append(panel)
    return reduce_development_wave_iteration_v1(
        proposals=proposals,
        master_seeds=master_seeds,
        panels_by_candidate=panels_by_candidate,
        min_update_seeds=min_update_seeds,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seeds", required=True, help="comma-separated unique integer seeds")
    parser.add_argument("--anchor-parameters-json", type=Path)
    parser.add_argument("--panels-json", type=Path, help="existing candidate-id -> panel-list JSON")
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--runtime-binding", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    seeds = [int(part) for part in args.master_seeds.split(",")]
    anchor = (
        json.loads(args.anchor_parameters_json.read_text(encoding="utf-8"))
        if args.anchor_parameters_json else None
    )
    if args.panels_json:
        proposals = propose_development_wave_candidates_v1(anchor)
        panels = json.loads(args.panels_json.read_text(encoding="utf-8"))
        result = reduce_development_wave_iteration_v1(
            proposals=proposals, master_seeds=seeds, panels_by_candidate=panels,
        )
    else:
        kwargs = {}
        if args.bridge:
            kwargs["bridge_path"] = args.bridge
        if args.runtime_binding:
            kwargs["runtime_binding_path"] = args.runtime_binding
        result = run_development_wave_iteration_v1(
            master_seeds=seeds, anchor_parameters=anchor, **kwargs,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "updated", "next_candidate_id")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
