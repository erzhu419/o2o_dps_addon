"""Fit one observable Cat-relative branch and evaluate it on fresh full waves.

Teacher rows are labels, not a policy evaluation.  The deployed-in-simulator
candidate receives only a frozen current-state rule and retains Cat as its
source-policy fallback; at most one ActionPlan is changed per wave.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Iterable, Mapping

from . import cat_action_branch_search_v1 as _branch
from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .branch_teacher_v1 import BranchReplayMismatchV1, _run_fresh, _same_prefix, _seed_receipt
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4, validate_source_decision_v4,
)
from .cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1, adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
)
from .expert_policy import ExpertDecision
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3, validate_dynamic_load_request_v3
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


SCHEMA = "conditional_cat_action_branch/v1"
POLICY_ID = "conditional_cat_action_branch_candidate/v1"
_QUEUE_KINDS = {"SUPPRESS_QUEUE", "ADD_HS_QUEUE"}


@dataclass(frozen=True)
class FrozenRuleV1:
    """Only current-state bins enter the candidate.  None means exact Cat."""

    kind: str | None = None
    rage_band: str | None = None
    swing_band: str | None = None
    target_phase: str | None = None
    target_count: str | None = None
    weapon_mode: str | None = None

    def __post_init__(self) -> None:
        if self.kind is None:
            if any(getattr(self, key) is not None for key in (
                "rage_band", "swing_band", "target_phase", "target_count", "weapon_mode",
            )):
                raise ValueError("abstaining rule must not constrain observations")
        elif self.kind not in _branch.BRANCH_KINDS:
            raise ValueError("unknown ActionPlan kind")


def _signature(combat: Mapping[str, Any], kind: str) -> tuple[str, ...]:
    rage = float(combat["rage"])
    swing = float(combat["mainhand_swing_remaining_s"])
    target_hp = float(combat["target_health_pct"])
    enemies = int(combat["nearby_enemies"])
    return (
        "low" if rage < 40 else "mid" if rage < 70 else "high",
        ("imminent" if swing <= 0.8 else "later") if kind in _QUEUE_KINDS else "any",
        "execute" if target_hp < 20 else "normal",
        "single" if enemies == 1 else "multiple",
        str(getattr(combat["weapon_mode"], "value", combat["weapon_mode"])),
    )


def _rule_for_signature(kind: str, signature: tuple[str, ...]) -> FrozenRuleV1:
    return FrozenRuleV1(kind, *signature)


def _training_row(row: Mapping[str, Any]) -> tuple[int, str, tuple[str, ...], float] | None:
    if row.get("baseline_status") != "COMPLETED" or row.get("status") != "COMPLETE_BRANCH_SMOKE":
        return None
    if row.get("branch_action_accepted") is not True:
        return None
    kind = row.get("kind")
    if kind not in _branch.BRANCH_KINDS:
        return None
    delta = row.get("paired_effective_damage_delta")
    if type(delta) not in (int, float) or not math.isfinite(delta):
        return None
    combat = row.get("combat")
    if not isinstance(combat, Mapping):
        return None
    return int(row["seed"]), kind, _signature(combat, kind), float(delta)


def fit_conditional_cat_branch_v1(
    rows: Iterable[Mapping[str, Any]], *, training_seeds: Iterable[int],
    min_distinct_seeds: int = 6,
) -> dict[str, Any]:
    """Use complete, accepted labels; aggregate repeated states by seed."""
    seeds = frozenset(int(seed) for seed in training_seeds)
    if not seeds or min_distinct_seeds < 2:
        raise ValueError("training seeds and minimum support are required")
    by_cell: dict[tuple[str, tuple[str, ...]], dict[int, list[float]]] = {}
    label_count = 0
    for row in rows:
        seed = int(row["seed"])
        if seed not in seeds:
            continue
        label = _training_row(row)
        if label is None:
            continue
        _, kind, signature, delta = label
        by_cell.setdefault((kind, signature), {}).setdefault(seed, []).append(delta)
        label_count += 1
    cells: list[dict[str, Any]] = []
    for (kind, signature), seed_values in sorted(by_cell.items()):
        deltas = [mean(values) for _, values in sorted(seed_values.items())]
        n = len(deltas)
        avg = mean(deltas)
        sem = stdev(deltas) / math.sqrt(n) if n >= 2 else math.inf
        lower = avg - 1.96 * sem
        fraction_positive = sum(value > 0 for value in deltas) / n
        eligible = n >= min_distinct_seeds and fraction_positive >= 0.75 and lower > 0
        cells.append({
            "rule": asdict(_rule_for_signature(kind, signature)),
            "distinct_seed_count": n, "mean_paired_label_delta": avg,
            "lower_95_normal_label_bound": lower if math.isfinite(lower) else None,
            "positive_seed_fraction": fraction_positive,
            "eligible_for_fresh_test": eligible,
        })
    eligible_cells = [cell for cell in cells if cell["eligible_for_fresh_test"]]
    selected = max(eligible_cells, key=lambda cell: cell["lower_95_normal_label_bound"]) if eligible_cells else None
    return {
        "schema": SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "FROZEN_RULE_FOR_FRESH_TEST" if selected else "ABSTAIN_INSUFFICIENT_CONDITIONAL_EVIDENCE",
        "training_seeds": sorted(seeds), "usable_teacher_label_count": label_count,
        "candidate_cell_count": len(cells), "min_distinct_seeds": min_distinct_seeds,
        "policy": selected["rule"] if selected else asdict(FrozenRuleV1()),
        "selected_training_cell": selected,
        "cells": cells,
        "teacher_labels_are_policy_outcome": False,
        "scientific_run_launched": False,
    }


def diagnose_teacher_cells_v1(
    rows: Iterable[Mapping[str, Any]], model: Mapping[str, Any], *, diagnostic_seeds: Iterable[int],
) -> dict[str, Any]:
    """Read unused teacher labels for instability; this is not a policy rollout."""
    seeds = frozenset(int(seed) for seed in diagnostic_seeds)
    if seeds.intersection(model["training_seeds"]):
        raise ValueError("diagnostic teacher seeds overlap training seeds")
    cells = []
    for training_cell in model["cells"]:
        rule = training_cell["rule"]
        key = (rule["kind"], (
            rule["rage_band"], rule["swing_band"], rule["target_phase"],
            rule["target_count"], rule["weapon_mode"],
        ))
        per_seed: dict[int, list[float]] = {}
        for row in rows:
            if int(row["seed"]) not in seeds:
                continue
            label = _training_row(row)
            if label is not None and (label[1], label[2]) == key:
                per_seed.setdefault(label[0], []).append(label[3])
        deltas = [mean(values) for _, values in sorted(per_seed.items())]
        cells.append({
            "rule": rule, "distinct_seed_count": len(deltas),
            "mean_paired_teacher_label_delta": mean(deltas) if deltas else None,
            "positive_seed_count": sum(delta > 0 for delta in deltas),
        })
    return {
        "status": "TEACHER_LABEL_DIAGNOSTIC_ONLY",
        "diagnostic_seeds": sorted(seeds), "cells": cells,
        "full_wave_candidate_policy_evaluated": False,
    }


def _matches(rule: FrozenRuleV1, state: CatFuryFullPolicyStateV4) -> bool:
    if rule.kind is None:
        return False
    return _signature(asdict(state.combat), rule.kind) == (
        rule.rage_band, rule.swing_band, rule.target_phase,
        rule.target_count, rule.weapon_mode,
    )


class ConditionalCatBranchCandidateV1:
    expert_id = POLICY_ID

    def __init__(self, rule: FrozenRuleV1) -> None:
        self.rule = rule
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.interventions: list[dict[str, Any]] = []

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        index = self.decision_count
        self.decision_count += 1
        base = validate_source_decision_v4(self.cat.propose(state))
        kind = self.rule.kind
        if self.interventions or not _matches(self.rule, state) or kind not in _branch.available_branches_v1(state, base):
            return base
        changed = _branch.apply_action_branch_v1(state, base, kind)
        self.interventions.append({
            "decision_index": index, "kind": kind,
            "policy_observation": asdict(state),
            "cat_proposal": base.to_dict(), "candidate_proposal": changed.to_dict(),
        })
        return changed


def run_conditional_cat_branch_candidate_v1(
    bridge: Any, raid_sim_request: Mapping[str, Any], candidate: ConditionalCatBranchCandidateV1,
    *, seed: int, target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: _cat_v5.CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000, max_advances: int = 100_000,
) -> dict[str, Any]:
    if type(candidate) is not ConditionalCatBranchCandidateV1:
        raise TypeError("candidate must be exact ConditionalCatBranchCandidateV1")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or _cat_v5.CatFurySimulatorInputsV5()
    candidate.interventions.clear()
    candidate.decision_count = 0
    controls = CatSimulatorControlFacadeV5(
        bridge, item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_cvars={
            "NP_QueueCastTimeSpells": inputs.initial_np_queue_cast_time_spells,
            "NP_QueueInstantSpells": inputs.initial_np_queue_instant_spells,
        },
    )
    controls.reset_sidecar()
    facade = _dynamic_v5._DynamicV3ExecutorBridgeFacade(controls)
    core = _branch._candidate_core(controls, inputs)
    # _candidate_core returns a FunctionType with its own copied globals.
    core.__globals__["_supported_adapter"] = lambda adapter: type(adapter) is ConditionalCatBranchCandidateV1
    result = core(
        facade, raid_sim_request, candidate, seed=seed,
        target_contexts=target_contexts, dynamic_load=dynamic_load,
        max_decisions=max_decisions, max_advances=max_advances, retain_steps=True,
    )
    _branch._cat_v6._bind_source_reentry_next_epochs_v6(result)
    _dynamic_v5._rewrite_v5_identity(result, dynamic_load, facade.last_load_result)
    changed = {row["decision_index"]: row for row in candidate.interventions}
    for step in result.get("steps") or []:
        intervention = changed.get(step.get("decision_index"))
        step["policy_proposal_origin"] = "CONDITIONAL_BRANCH" if intervention else "CAT_UNCHANGED"
    result.update({
        "schema": "conditional_cat_action_branch_rollout/v1",
        "expert_id": POLICY_ID, "policy_family": "CAT_RELATIVE_CONDITIONAL_BRANCH",
        "rule": asdict(candidate.rule), "interventions": list(candidate.interventions),
        "intervention_count": len(candidate.interventions),
        "cat_baseline_artifact": False, "cat_source_order_claim": not candidate.interventions,
        "dynamic_v3_runtime_receipt_closure": _dynamic_v5._collect_runtime_receipts_v5(
            bridge, dynamic_load, facade.last_load_result, result.get("final_state")
        ),
        "historical_truth": False, "live_fidelity": False, "voting_eligible": False,
        "comparison_ready": False, "scientific_run_launched": False,
    })
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_CANDIDATE_SIMULATOR_ONLY"
    return result


def _action_receipts(step: Mapping[str, Any]) -> list[dict[str, Any]]:
    execution = step.get("ordered_execution") or {}
    return [
        {"source_sink": event.get("source_sink"), "simulator_acceptance": event.get("simulator_acceptance")}
        for event in execution.get("sink_events") or []
        if isinstance(event, Mapping) and isinstance(event.get("source_sink"), Mapping)
        and event["source_sink"].get("channel") in {"gcd", "swing_queue"}
    ]


def evaluate_conditional_cat_branch_v1(
    case: DevelopmentWaveCaseV1, bridge_factory: Callable[[], Any],
    rule: FrozenRuleV1, *, training_seeds: Iterable[int],
) -> dict[str, Any]:
    """Evaluate an already-frozen rule, never a teacher seed, on a complete wave."""
    seed = case.dynamic_load.seed
    if seed in set(training_seeds):
        raise ValueError("fresh evaluation seed overlaps teacher training seeds")
    arguments = dict(seed=seed, target_contexts=case.target_contexts, dynamic_load=case.dynamic_load)
    baseline = _run_fresh(bridge_factory, lambda bridge: run_cat_fury_full_policy_rollout_v6(
        bridge, case.request, CatFuryFullPolicyAdapterV4(), **arguments,
    ))
    alternative = _run_fresh(bridge_factory, lambda bridge: run_conditional_cat_branch_candidate_v1(
        bridge, case.request, ConditionalCatBranchCandidateV1(rule), **arguments,
    ))
    _seed_receipt(baseline, seed)
    _seed_receipt(alternative, seed)
    interventions = alternative.get("interventions") or []
    if len(interventions) > 1:
        raise BranchReplayMismatchV1("conditional candidate changed more than one decision")
    if interventions:
        intervention = interventions[0]
        index = intervention["decision_index"]
        _same_prefix(baseline, alternative, index)
        if intervention["cat_proposal"] != baseline["steps"][index]["proposal"]:
            raise BranchReplayMismatchV1("conditional Cat proposal drifted at branch")
        if any(step.get("policy_proposal_origin") != "CAT_UNCHANGED"
               for step in alternative["steps"][index + 1:]):
            raise BranchReplayMismatchV1("conditional branch did not resume Cat")
        kind = intervention["kind"]
        source = _action_receipts(baseline["steps"][index])
        changed = _action_receipts(alternative["steps"][index])
        if kind in {"ADD_HS_QUEUE", "BT_TO_WW", "WW_TO_BT"}:
            accepted = any(
                (event.get("source_sink") or {}).get("source_ref", "").startswith(_branch.POLICY_ID)
                and (event.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
                for event in changed
            )
        else:
            channel = "swing_queue" if kind == "SUPPRESS_QUEUE" else "gcd"
            accepted = any(
                (event.get("source_sink") or {}).get("channel") == channel
                and (event.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
                for event in source
            )
    else:
        index, kind, accepted = None, None, False
        if len(baseline.get("steps") or []) != len(alternative.get("steps") or []):
            raise BranchReplayMismatchV1("abstaining candidate changed Cat step count")
        for left, right in zip(baseline["steps"], alternative["steps"]):
            if left.get("proposal") != right.get("proposal") or left.get("simulator_state_next_epoch") != right.get("simulator_state_next_epoch"):
                raise BranchReplayMismatchV1("abstaining candidate changed Cat execution")
    baseline_terminal = adjudicate_development_wave_completion_v1(case, baseline)
    candidate_terminal = adjudicate_development_wave_completion_v1(case, alternative)
    complete = baseline_terminal["status"] == candidate_terminal["status"] == "COMPLETED"
    return {
        "schema": "conditional_cat_action_branch_fresh_pair/v1",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY", "seed": seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "rule": asdict(rule), "intervention_index": index, "intervention_kind": kind,
        "intervention_accepted": accepted,
        "baseline_terminal": baseline_terminal, "candidate_terminal": candidate_terminal,
        "paired_effective_damage_delta": (
            candidate_terminal["own_effective_damage"] - baseline_terminal["own_effective_damage"]
            if complete else None
        ),
        "status": "COMPLETE_FRESH_PAIR" if complete else "CENSORED_OR_FAILED_PAIR",
        "hidden_rng_snapshot_verified": False, "causal_effect_established": False,
        "live_fidelity": False, "deployment_eligible": False,
    }


def read_teacher_projection_v1(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("teacher projection must have one object per line")
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teacher-jsonl", type=Path, required=True)
    parser.add_argument("--train-seed-count", type=int, default=16)
    parser.add_argument("--fresh-seed", type=int)
    parser.add_argument("--fresh-seed-count", type=int, default=1)
    parser.add_argument("--stratum", choices=("controlled", "single_short", "single_medium", "single_long", "multi_two"), default="single_short")
    parser.add_argument("--bridge", type=Path, default=_branch.DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=_branch.PROJECT_ROOT.parent / "wowsims-turtle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = read_teacher_projection_v1(args.teacher_jsonl)
    all_seeds = sorted({int(row["seed"]) for row in rows})
    if args.train_seed_count < 1 or args.train_seed_count > len(all_seeds):
        parser.error("--train-seed-count must fit available teacher seeds")
    model = fit_conditional_cat_branch_v1(rows, training_seeds=all_seeds[:args.train_seed_count])
    result: dict[str, Any] = {
        "model": model,
        "teacher_diagnostic": diagnose_teacher_cells_v1(
            rows, model, diagnostic_seeds=all_seeds[args.train_seed_count:],
        ),
        "fresh_pairs": [],
    }
    if args.fresh_seed is not None:
        if args.fresh_seed_count < 1:
            parser.error("--fresh-seed-count must be positive")
        rule = FrozenRuleV1(**model["policy"])
        for seed in range(args.fresh_seed, args.fresh_seed + args.fresh_seed_count):
            if args.stratum == "controlled":
                case = build_development_wave_case_v1(seed)
            else:
                from .development_wave_stratified_v1 import build_stratified_wave_case_v1
                case, _ = build_stratified_wave_case_v1(seed, args.stratum)
            result["fresh_pairs"].append(evaluate_conditional_cat_branch_v1(
                case, lambda: SimulatorBridgeDynamicV3(args.bridge, cwd=args.bridge_cwd),
                rule, training_seeds=model["training_seeds"],
            ))
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
