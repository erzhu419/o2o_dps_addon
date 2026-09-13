"""Small observable-state branch teacher and Cat-relative guard pilot.

The teacher may inspect completed counterfactuals offline.  The controller
only receives the current Cat policy state; it never receives a seed, future
team damage schedule, or the teacher's outcome labels during a rollout.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Mapping, Sequence

from .branch_teacher_v1 import (
    DEFAULT_BRIDGE,
    PROJECT_ROOT,
    _observation_receipt,
    _run_fresh,
    _same_prefix,
    run_cat_relative_branch_teacher_v1,
)
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4
from .cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from .cat_residual_candidate_rollout_v1 import (
    CatQueueResidualV1,
    CatResidualCandidateV1,
    run_cat_residual_candidate_v1,
)
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
)
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


SCHEMA = "observable_search_teacher_guard/v1"
CONTROLLER_ID = "cat_observable_rage_guard/v1"


@dataclass(frozen=True)
class ObservableRageGuardV1:
    """One-threshold exploratory guard; Cat remains the fallback."""

    rage_at_least: float | None
    reserve_discount_rage: float
    training_label_count: int
    positive_label_count: int

    def allows(self, state: CatFuryFullPolicyStateV4) -> bool:
        return self.rage_at_least is not None and state.combat.rage >= self.rage_at_least

    def to_dict(self) -> dict[str, Any]:
        return {
            "controller_id": CONTROLLER_ID,
            "feature": "combat.rage",
            "operator": ">=",
            "rage_at_least": self.rage_at_least,
            "reserve_discount_rage": self.reserve_discount_rage,
            "training_label_count": self.training_label_count,
            "positive_label_count": self.positive_label_count,
            "enabled": self.rage_at_least is not None,
            "policy_inputs": "CURRENT_CAT_STATE_ONLY",
        }


def teacher_label_v1(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project a verified one-decision replay into a training label."""

    status = result.get("status")
    label: dict[str, Any] = {
        "seed": result.get("seed"),
        "status": status,
        "decision_index": result.get("decision_index"),
        "accepted_prefix_decisions_verified": result.get("accepted_prefix_decisions_verified"),
        "observation": None,
        "proposals": None,
        "values": result.get("value"),
        "uncertainty": {
            "paired_seed_count": 1,
            "hidden_rng_snapshot_verified": result.get("hidden_rng_snapshot_verified"),
            "causal_effect_established": result.get("causal_effect_established"),
        },
        "selected_action": None,
    }
    if status != "COMPLETE_BRANCH_SMOKE":
        return label
    receipt = result.get("policy_observation") or {}
    observation = receipt.get("observation")
    if not isinstance(observation, Mapping):
        raise ValueError("complete teacher branch lacks policy observation")
    _observation_receipt(observation)
    value = result.get("value") or {}
    delta = value.get("paired_delta")
    if type(delta) not in (int, float):
        raise ValueError("complete teacher branch lacks paired value")
    proposal = result.get("proposal") or {}
    label.update({
        "observation": dict(observation),
        "proposals": {"cat": proposal.get("cat"), "residual": proposal.get("branch")},
        "values": value,
        "selected_action": "RESIDUAL" if delta > 0 else "CAT",
    })
    return label


def distill_rage_guard_v1(
    labels: Sequence[Mapping[str, Any]], *, reserve_discount_rage: float,
) -> ObservableRageGuardV1:
    """Fit one mechanistic rage split; abstain if training labels overlap.

    This is intentionally a hypothesis generator, not a confidence gate.
    A held-out whole-wave evaluation decides whether the guard survives.
    """

    positive: list[float] = []
    nonpositive: list[float] = []
    for label in labels:
        if label.get("status") != "COMPLETE_BRANCH_SMOKE":
            continue
        observation = label.get("observation") or {}
        combat = observation.get("combat") if isinstance(observation, Mapping) else None
        rage = combat.get("rage") if isinstance(combat, Mapping) else None
        if type(rage) not in (int, float):
            raise ValueError("teacher label is missing observed rage")
        if label.get("selected_action") == "RESIDUAL":
            positive.append(float(rage))
        elif label.get("selected_action") == "CAT":
            nonpositive.append(float(rage))
        else:
            raise ValueError("complete teacher label has no selected action")
    threshold = None
    if positive and nonpositive and min(positive) > max(nonpositive):
        threshold = (min(positive) + max(nonpositive)) / 2.0
    return ObservableRageGuardV1(
        rage_at_least=threshold,
        reserve_discount_rage=reserve_discount_rage,
        training_label_count=len(positive) + len(nonpositive),
        positive_label_count=len(positive),
    )


def run_guarded_candidate_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    guard: ObservableRageGuardV1,
) -> dict[str, Any]:
    """Run the distilled controller over a complete native wave."""

    candidate = CatResidualCandidateV1(CatQueueResidualV1(0.0))
    original_propose = candidate.propose
    trace: list[dict[str, Any]] = []

    def guarded_propose(state: CatFuryFullPolicyStateV4) -> Any:
        receipt = _observation_receipt(asdict(state))
        allowed = guard.allows(state)
        candidate.residual = CatQueueResidualV1(
            guard.reserve_discount_rage if allowed else 0.0
        )
        previous = len(candidate.interventions)
        proposal = original_propose(state)
        changed = len(candidate.interventions) > previous
        trace.append({
            "decision_index": candidate.decision_count - 1,
            "observation": receipt["observation"],
            "guard_allowed": allowed,
            "selected_action": "RESIDUAL" if changed else "CAT",
            "proposal": proposal.to_dict(),
        })
        return proposal

    candidate.propose = guarded_propose  # Exact candidate type remains the native executor protocol.
    artifact = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_residual_candidate_v1(
            bridge, case.request, candidate, seed=case.dynamic_load.seed,
            target_contexts=case.target_contexts, dynamic_load=case.dynamic_load,
        ),
    )
    artifact["schema"] = SCHEMA
    artifact["expert_id"] = CONTROLLER_ID
    artifact["controller"] = guard.to_dict()
    artifact["controller_trace"] = trace
    artifact["residual"] = {"type": "OBSERVABLE_STATE_GUARD", **guard.to_dict()}
    artifact["cat_baseline_artifact"] = False
    artifact["comparison_ready"] = False
    return artifact


def _paired_whole_wave(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    guard: ObservableRageGuardV1,
) -> dict[str, Any]:
    arguments = dict(seed=case.dynamic_load.seed, target_contexts=case.target_contexts,
                     dynamic_load=case.dynamic_load)
    cat = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_fury_full_policy_rollout_v6(
            bridge, case.request, CatFuryFullPolicyAdapterV4(), **arguments,
        ),
    )
    student = run_guarded_candidate_v1(case, bridge_factory, guard)
    steps = student.get("steps") or []
    changed = student.get("interventions") or []
    if changed:
        _same_prefix(cat, student, changed[0]["decision_index"])
    elif steps and len(steps) == len(cat.get("steps") or []):
        _same_prefix(cat, student, len(steps) - 1)
    cat_terminal = adjudicate_development_wave_completion_v1(case, cat)
    student_terminal = adjudicate_development_wave_completion_v1(case, student)
    complete = cat_terminal["status"] == student_terminal["status"] == "COMPLETED"
    return {
        "seed": case.dynamic_load.seed,
        "status": "PAIRED_COMPLETE" if complete else "INCOMPLETE",
        "cat_terminal": cat_terminal,
        "student_terminal": student_terminal,
        "student_intervention_count": len(changed),
        "student_visited_state_count": len(student["controller_trace"]),
        "paired_effective_damage_delta": (
            student_terminal["own_effective_damage"] - cat_terminal["own_effective_damage"]
            if complete else None
        ),
    }


def run_observable_search_teacher_pilot_v1(
    *, training_seeds: Sequence[int], heldout_seeds: Sequence[int],
    bridge_factory: Callable[[], Any], reserve_discount_rage: float = 25.0,
) -> dict[str, Any]:
    if not training_seeds or not heldout_seeds or set(training_seeds) & set(heldout_seeds):
        raise ValueError("training and held-out seeds must be nonempty and disjoint")
    labels = [teacher_label_v1(run_cat_relative_branch_teacher_v1(
        build_development_wave_case_v1(seed), bridge_factory,
        reserve_discount_rage=reserve_discount_rage,
    )) for seed in training_seeds]
    guard = distill_rage_guard_v1(labels, reserve_discount_rage=reserve_discount_rage)
    heldout = [_paired_whole_wave(build_development_wave_case_v1(seed), bridge_factory, guard)
               for seed in heldout_seeds]
    deltas = [row["paired_effective_damage_delta"] for row in heldout
              if row["status"] == "PAIRED_COMPLETE"]
    complete = len(deltas) == len(heldout)
    delta_mean = mean(deltas) if complete else None
    delta_se = stdev(deltas) / len(deltas) ** 0.5 if complete and len(deltas) > 1 else None
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "training_seeds": list(training_seeds),
        "heldout_seeds": list(heldout_seeds),
        "teacher_labels": labels,
        "teacher_label_count": sum(row["status"] == "COMPLETE_BRANCH_SMOKE" for row in labels),
        "searched_candidate_structure_count": 1,
        "searched_proposals_per_label": ["CAT", "TWO_HAND_HS_RESERVE_DISCOUNT"],
        "guard": guard.to_dict(),
        "heldout_whole_wave": heldout,
        "heldout_student_visited_state_count": sum(
            row["student_visited_state_count"] for row in heldout
        ),
        "heldout_mean_paired_effective_damage_delta": delta_mean,
        "heldout_paired_delta_se": delta_se,
        "status": "HELDOUT_COMPLETE_EXPLORATORY" if complete else "HELDOUT_INCOMPLETE",
        "adoption_decision": (
            "REJECT_NEGATIVE_HELDOUT" if complete and delta_mean < 0
            else "NOT_ELIGIBLE_FOR_ADOPTION"
        ),
        "updated": False,
        "deployment_authorized": False,
        "limitations": [
            "One-decision same-seed replay does not restore hidden simulator RNG state.",
            "One model-defined wave and fewer than 32 held-out seeds do not establish superiority.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--train-seed-start", type=int, default=20260913)
    parser.add_argument("--train-seed-count", type=int, default=6)
    parser.add_argument("--heldout-seed-start", type=int, default=20260919)
    parser.add_argument("--heldout-seed-count", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_observable_search_teacher_pilot_v1(
        training_seeds=list(range(args.train_seed_start, args.train_seed_start + args.train_seed_count)),
        heldout_seeds=list(range(args.heldout_seed_start, args.heldout_seed_start + args.heldout_seed_count)),
        bridge_factory=lambda: SimulatorBridgeDynamicV3(
            args.bridge, cwd=PROJECT_ROOT.parent / "wowsims-turtle",
        ),
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
