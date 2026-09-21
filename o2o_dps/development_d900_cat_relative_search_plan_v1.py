"""Development-only d900 one-wave Cat-relative search plan.

Proposal observations are current Cat prefixes, never historical future events.
All lanes use the same frozen case and paired simulator/teammate seeds.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import ImportedReactiveProgramBindingV1, ProgramDecisionV1
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .development_d900_cat_relative_v1 import (
    D900CatRelativeCandidateV1,
    D900CatRelativeError,
    enumerate_d900_cat_relative_action_plans_v1,
)
from .sim_bridge import AvailableAction
from .upper_kara_cat_action_plan_teacher_v8 import CatDecisionPointV8
from .upper_kara_development_route_focus_v1 import ORDERED_GUIDS


SCHEMA = "development_d900_cat_relative_search_plan/v1"
PROPOSAL_COUNT = 128
SELECTION_COUNT = 128
HELDOUT_COUNT = 256
MAX_NONZERO_CANDIDATES = 256


def paired_seed_cohorts_v1(
    simulator_seed_start: int, teammate_seed_start: int
) -> dict[str, tuple[tuple[int, int], ...]]:
    """Disjoint, predeclared pairs; every candidate and exact Cat share each pair."""

    for label, value in (("simulator_seed_start", simulator_seed_start),
                         ("teammate_seed_start", teammate_seed_start)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a nonnegative integer")
    counts = (("PROPOSAL", PROPOSAL_COUNT), ("SELECTION", SELECTION_COUNT),
              ("HELDOUT", HELDOUT_COUNT))
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    offset = 0
    for name, count in counts:
        result[name] = tuple(
            (simulator_seed_start + offset + index,
             teammate_seed_start + offset + index)
            for index in range(count)
        )
        offset += count
    return result


def freeze_d900_search_case_v1(
    case: Any,
    cat_binding: ImportedReactiveProgramBindingV1,
    *,
    model_result_sha: str,
    model_sha: str,
    exact_build_source: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep the exact build/items, initial drug/CD state and route hypotheses fixed."""

    if tuple(case.native_target_guids) != ORDERED_GUIDS:
        raise D900CatRelativeError("search case differs from exact d900 registry")
    mode = case.receipt.get("fixed_attackability_mode")
    if not isinstance(mode, str) or not mode:
        raise D900CatRelativeError("d900 search needs an explicit fixed attackability mode")
    if not model_result_sha or not model_sha:
        raise ValueError("responsive model result and model IDs are required")
    return {
        "request": deepcopy(case.request),
        "dynamic_config_content_sha256": case.dynamic_config.content_sha256,
        "native_target_guids": list(case.native_target_guids),
        "attackability_mode": mode,
        "case_source": deepcopy(case.receipt["source"]),
        "exact_build_source": deepcopy(dict(exact_build_source)),
        "cat_source_policy_id": cat_binding.source_policy_id,
        "model_result_sha": model_result_sha,
        "model_sha": model_sha,
        "fixed_long_cd_and_consumable_availability": "UNCHANGED_CASE_REQUEST_AND_NATIVE_READY_SNAPSHOT",
    }


def verify_d900_search_case_v1(
    case: Any, cat_binding: ImportedReactiveProgramBindingV1, freeze: Mapping[str, Any]
) -> None:
    """Reject source/loadout/target/attackability drift before any paired lane."""

    if (
        case.request != freeze["request"]
        or case.dynamic_config.content_sha256 != freeze["dynamic_config_content_sha256"]
        or list(case.native_target_guids) != freeze["native_target_guids"]
        or case.receipt.get("fixed_attackability_mode") != freeze["attackability_mode"]
        or case.receipt.get("source") != freeze["case_source"]
        or cat_binding.source_policy_id != freeze["cat_source_policy_id"]
    ):
        raise D900CatRelativeError("d900 paired lane changes the frozen case or Cat binding")


def record_d900_cat_prefix_v1(
    cat_binding: ImportedReactiveProgramBindingV1,
    points: list[CatDecisionPointV8],
) -> ImportedReactiveProgramBindingV1:
    """Capture only the current decision observation during one exact Cat replay."""

    def open_session():
        native_cat = cat_binding.open_session()

        def decide(observation, available: tuple[AvailableAction, ...]) -> ProgramDecisionV1:
            decision = native_cat(observation, available)
            points.append(CatDecisionPointV8(
                len(points), deepcopy(observation), tuple(available), decision
            ))
            return decision

        return decide

    return ImportedReactiveProgramBindingV1(
        cat_binding.binding_id,
        cat_binding.source_policy_id,
        cat_binding.observation_contract_id,
        open_session,
    )


def _candidate_at_point(
    point: CatDecisionPointV8, option_index: int, replacement: ProgramDecisionV1
) -> D900CatRelativeCandidateV1:
    focus = point.observation.state["target_index"]
    ready_action = (
        replacement.gcd_action if replacement.gcd_action != point.cat_decision.gcd_action
        else replacement.queue_action if replacement.queue_action != point.cat_decision.queue_action
        else None
    )
    return D900CatRelativeCandidateV1(
        candidate_id=f"d900-p{point.decision_index:03d}-a{option_index:03d}",
        guard=ObservableCausalGuardV1(
            target_index=focus,
            target_attackable_is=True,
            live_target_count_gte=1,
            attackable_target_count_gte=1,
            action_ready=ready_action,
            false_semantics=SKIP_PLAN,
        ),
        replacement=replacement,
        decision_index_is=point.decision_index,
        cat_decision_is=point.cat_decision,
    )


@dataclass(frozen=True)
class D900CatRelativeSearchPlanV1:
    freeze: dict[str, Any]
    seed_cohorts: dict[str, tuple[tuple[int, int], ...]]
    discovery_seed: tuple[int, int]
    candidates: tuple[D900CatRelativeCandidateV1, ...]

    def paired_row(
        self, *, cohort: str, candidate_id: str, seed: int, teammate_seed: int,
        candidate_outcome: Any, cat_outcome: Any, opened_sessions: Sequence[Any],
    ) -> dict[str, Any]:
        if (seed, teammate_seed) not in self.seed_cohorts[cohort]:
            raise ValueError("paired result seed is outside its frozen cohort")
        if candidate_id not in {candidate.candidate_id for candidate in self.candidates}:
            raise ValueError("paired result candidate is outside the frozen plan")
        if len(opened_sessions) != 1:
            raise ValueError("one paired candidate replay must open exactly one Cat session")
        session = opened_sessions[0]
        intervention_count = len(session.interventions)
        if intervention_count > 1:
            raise ValueError("candidate lane intervened more than once")
        candidate_status = candidate_outcome.status.value
        cat_status = cat_outcome.status.value
        complete = candidate_status == cat_status == "COMPLETE"
        return {
            "cohort": cohort,
            "candidate_id": candidate_id,
            "seed": seed,
            "teammate_seed": teammate_seed,
            "candidate_status": candidate_status,
            "cat_status": cat_status,
            "candidate_effective_damage": candidate_outcome.effective_damage if complete else None,
            "cat_effective_damage": cat_outcome.effective_damage if complete else None,
            "paired_delta": (
                candidate_outcome.effective_damage - cat_outcome.effective_damage
                if complete else None
            ),
            "intervention_count": intervention_count,
            "matched_baseline_count": session.matched_baseline_count,
            "ineligible_count": session.ineligible_count,
            "invalid_reason": candidate_outcome.invalid_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": "DEVELOPMENT_ONLY_PLAN_NOT_EVALUATED",
            "comparison_authorized": False,
            "freeze": deepcopy(self.freeze),
            "seed_cohorts": {
                name: [{"seed": seed, "teammate_seed": teammate_seed}
                       for seed, teammate_seed in pairs]
                for name, pairs in self.seed_cohorts.items()
            },
            "discovery_seed": {
                "seed": self.discovery_seed[0],
                "teammate_seed": self.discovery_seed[1],
            },
            "baseline": "EXACT_CAT_SAME_SESSION_IMPORT_AND_SAME_PAIRED_SEEDS",
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selection_contract": {
                "proposal_rank": "MEAN_PAIRED_EFFECTIVE_DAMAGE_DELTA",
                "selection_gate": "SELECTED_NONZERO_PAIRED_95_PERCENT_LCB_GT_ZERO_ELSE_EXACT_CAT",
                "minimum_complete_selection_pairs": SELECTION_COUNT,
                "minimum_activated_selection_pairs": 1,
                "heldout_use": "ACCEPTED_LANE_REPORT_ONLY_NO_RESELECTION",
                "paired_row_fields": [
                    "cohort", "candidate_id", "seed", "teammate_seed",
                    "candidate_status", "cat_status", "candidate_effective_damage",
                    "cat_effective_damage", "paired_delta", "intervention_count",
                    "matched_baseline_count", "ineligible_count", "invalid_reason",
                ],
            },
        }


def build_d900_cat_relative_search_plan_v1(
    *,
    proposal_points: Sequence[CatDecisionPointV8],
    freeze: Mapping[str, Any],
    simulator_seed_start: int,
    teammate_seed_start: int,
) -> D900CatRelativeSearchPlanV1:
    """Round-robin current-prefix alternatives across the one-wave Cat trace."""

    if not proposal_points:
        raise ValueError("proposal Cat replay has no causal decision points")
    if [point.decision_index for point in proposal_points] != list(range(len(proposal_points))):
        raise ValueError("proposal Cat points must be one ordered session")
    options = [enumerate_d900_cat_relative_action_plans_v1(point)
               for point in proposal_points]
    candidates: list[D900CatRelativeCandidateV1] = []
    depth = 0
    while len(candidates) < MAX_NONZERO_CANDIDATES and any(depth < len(rows) for rows in options):
        for point, rows in zip(proposal_points, options):
            if depth < len(rows):
                candidates.append(_candidate_at_point(point, depth, rows[depth]))
                if len(candidates) == MAX_NONZERO_CANDIDATES:
                    break
        depth += 1
    if not candidates:
        raise D900CatRelativeError("proposal Cat prefixes expose no route-legal nonzero ActionPlan")
    seeds = paired_seed_cohorts_v1(simulator_seed_start, teammate_seed_start)
    return D900CatRelativeSearchPlanV1(
        freeze=deepcopy(dict(freeze)),
        seed_cohorts=seeds,
        discovery_seed=seeds["PROPOSAL"][0],
        candidates=tuple(candidates),
    )


__all__ = [
    "D900CatRelativeSearchPlanV1", "build_d900_cat_relative_search_plan_v1",
    "freeze_d900_search_case_v1", "paired_seed_cohorts_v1",
    "record_d900_cat_prefix_v1", "verify_d900_search_case_v1",
]
