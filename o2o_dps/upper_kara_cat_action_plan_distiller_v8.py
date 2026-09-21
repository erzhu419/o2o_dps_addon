"""Distill proposal-cohort v8 Cat branch labels into frozen residuals.

The action-plan teacher evaluates one counterfactual decision at a recorded
Cat opportunity.  Those rows are labels, not deployable policies.  This
module groups complete labels by the *semantic change relative to Cat* and
emits a deliberately small candidate set for a later fresh full-route test.

The first version is intentionally narrow: every nonzero candidate contains
one wave-local step, requires the Cat GCD observed by the teacher, and uses
only current live-target count, current target attackability, and native
action readiness.  Decision indexes, absolute time, HP, simulator seeds, and
future suffixes never enter a frozen policy.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .causal_action_program_v1 import (
    ProgramDecisionV1,
    program_decision_from_dict_v1,
)
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceStepV1,
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    freeze_two_wave_cat_residual_sequence_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
    two_wave_cat_residual_sequence_from_dict_v1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError
from .upper_kara_cat_action_plan_teacher_v8 import RECKLESSNESS, SCHEMA as TEACHER_SCHEMA


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_distillation/v8"
SCOPE = "DEVELOPMENT_PROPOSAL_LABELS_TO_FRESH_TEST_CANDIDATES"

_COMPLETE_TEACHER_STATUS = "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING"
_COMPLETE_LABEL_STATUS = "COMPLETE_BRANCH_TEACHER_LABEL"
_COMPLETE_TERMINAL_STATUS = "COMPLETED"
_WAVE_POSITION = {"WAVE_1": 0, "WAVE_2": 1}
_DECISION_FIELDS = (
    "target_index",
    "controls",
    "optional_off_gcd_prefixes",
    "queue",
    "terminal",
    "prefix_order",
)


class UpperKaraCatActionPlanDistillationV8Error(ValueError):
    """The proposal-label input or requested frozen-candidate contract is invalid."""


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraCatActionPlanDistillationV8Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _seed_set(values: Iterable[int]) -> frozenset[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UpperKaraCatActionPlanDistillationV8Error(
                "proposal_seeds must contain nonnegative integers"
            )
        result.add(value)
    if not result:
        raise UpperKaraCatActionPlanDistillationV8Error(
            "proposal_seeds must be nonempty"
        )
    return frozenset(result)


def _decision_actions(decision: ProgramDecisionV1) -> tuple[ActionRef, ...]:
    actions = [row.action for row in decision.optional_off_gcd_prefixes]
    if decision.queue_action is not None:
        actions.append(decision.queue_action)
    if decision.gcd_action is not None:
        actions.append(decision.gcd_action)
    return tuple(actions)


def _cat_relative_mutation(
    cat: ProgramDecisionV1,
    candidate: ProgramDecisionV1,
) -> JSONMap:
    source = cat.to_dict()
    replacement = candidate.to_dict()
    changed: JSONMap = {}
    for field in _DECISION_FIELDS:
        if source[field] != replacement[field]:
            changed[field] = {
                "cat": source[field],
                "candidate": replacement[field],
            }
    return {
        "schema": "cat_relative_action_plan_mutation/v8",
        "changed_fields": changed,
    }


def _readiness_action(
    cat: ProgramDecisionV1,
    candidate: ProgramDecisionV1,
) -> ActionRef | None:
    """Choose the current-ready action that makes this mutation actionable."""

    if candidate.gcd_action is not None and candidate.gcd_action != cat.gcd_action:
        return candidate.gcd_action
    if (
        candidate.queue_action is not None
        and (candidate.queue_op, candidate.queue_action)
        != (cat.queue_op, cat.queue_action)
    ):
        return candidate.queue_action
    cat_prefixes = tuple(row.action for row in cat.optional_off_gcd_prefixes)
    candidate_prefixes = tuple(
        row.action for row in candidate.optional_off_gcd_prefixes
    )
    added = tuple(action for action in candidate_prefixes if action not in cat_prefixes)
    if len(added) == 1:
        return added[0]
    # A target/control mutation deliberately ends in a 1 ms WAIT so the next
    # epoch observes the newly selected target.  Bind it to the ready Cat GCD
    # that exposed this opportunity; the residual itself still executes only
    # the target/control change and does not cast that old-target action.
    return candidate.gcd_action or cat.gcd_action


def _available_now(branch: Mapping[str, Any], action: ActionRef) -> bool:
    rows = branch.get("available_actions")
    if not isinstance(rows, list):
        return False
    matches = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            row_action = ActionRef.from_wire(row.get("action"))
        except (TypeError, ValueError, SimBridgeProtocolError):
            continue
        if row_action == action:
            matches.append(row)
    return bool(
        len(matches) == 1
        and matches[0].get("legal") is True
        and matches[0].get("ready_in_ms") == 0
    )


def _current_attackable_target(
    observation: Mapping[str, Any],
) -> int | None:
    precombat = observation.get("precombat")
    if isinstance(precombat, Mapping) and precombat.get("active") is True:
        return None
    target_index = observation.get("target_index")
    if (
        isinstance(target_index, bool)
        or not isinstance(target_index, int)
        or target_index < 0
    ):
        return None
    semantics = observation.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("target_index") == target_index
    ]
    if (
        len(matches) != 1
        or matches[0].get("attackable") is not True
        or matches[0].get("dead") is True
    ):
        return None
    return target_index


def _visible_live_target_count(observation: Mapping[str, Any]) -> int | None:
    semantics = observation.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return None
    return sum(
        row.get("attackable") is True and row.get("dead") is not True
        for row in rows
    )


@dataclass(frozen=True)
class _EligibleLabel:
    seed: int
    wave_stratum: str
    live_target_count: int
    guard_target_index: int
    readiness_action: ActionRef
    expected_cat_gcd_action: ActionRef
    mutation: JSONMap
    candidate_decision: ProgramDecisionV1
    paired_delta: float

    @property
    def semantic_key(self) -> str:
        return _canonical(
            {
                "wave_stratum": self.wave_stratum,
                "live_target_count": self.live_target_count,
                "guard_target_index": self.guard_target_index,
                "action_ready": self.readiness_action.to_wire(),
                "expected_cat_gcd_action": self.expected_cat_gcd_action.to_wire(),
                "mutation": self.mutation,
            }
        )


def _eligible_label(
    result: Mapping[str, Any],
    branch: object,
) -> tuple[_EligibleLabel | None, str | None]:
    if not isinstance(branch, Mapping):
        return None, "MALFORMED_BRANCH"
    terminal = branch.get("branch_terminal")
    delta = branch.get("paired_effective_damage_delta")
    if (
        branch.get("status") != _COMPLETE_LABEL_STATUS
        or not isinstance(terminal, Mapping)
        or terminal.get("status") != _COMPLETE_TERMINAL_STATUS
        or branch.get("strict_single_intervention_verified") is not True
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(float(delta))
    ):
        return None, "COMPARISON_INCOMPLETE"

    try:
        cat = program_decision_from_dict_v1(branch.get("cat_decision"))
        candidate = program_decision_from_dict_v1(
            branch.get("candidate_decision")
        )
    except (TypeError, ValueError, SimBridgeProtocolError):
        return None, "MALFORMED_DECISION"
    if cat.gcd_action is None:
        return None, "CAT_SOURCE_GCD_ABSENT"
    if RECKLESSNESS in _decision_actions(candidate):
        return None, "MECHANICS_EXCLUDED_ACTION_1719"

    mutation = _cat_relative_mutation(cat, candidate)
    if not mutation["changed_fields"]:
        return None, "NO_CAT_RELATIVE_MUTATION"
    readiness_action = _readiness_action(cat, candidate)
    if readiness_action is None or not _available_now(branch, readiness_action):
        return None, "NO_CURRENT_READY_MUTATION_ACTION"

    wave = branch.get("wave_stratum")
    live_count = branch.get("live_target_count")
    if wave not in _WAVE_POSITION:
        return None, "UNSUPPORTED_WAVE_STRATUM"
    if (
        isinstance(live_count, bool)
        or not isinstance(live_count, int)
        or live_count < 1
    ):
        return None, "INVALID_LIVE_TARGET_COUNT"
    observation = branch.get("policy_observation")
    if not isinstance(observation, Mapping):
        return None, "MALFORMED_POLICY_OBSERVATION"
    target_index = _current_attackable_target(observation)
    if target_index is None:
        return None, "PRECOMBAT_OR_TARGET_NOT_ATTACKABLE"
    if _visible_live_target_count(observation) != live_count:
        return None, "LIVE_TARGET_COUNT_MISMATCH"

    seed = result.get("master_seed")
    assert isinstance(seed, int) and not isinstance(seed, bool)
    return (
        _EligibleLabel(
            seed=seed,
            wave_stratum=wave,
            live_target_count=live_count,
            guard_target_index=target_index,
            readiness_action=readiness_action,
            expected_cat_gcd_action=cat.gcd_action,
            mutation=mutation,
            candidate_decision=candidate,
            paired_delta=float(delta),
        ),
        None,
    )


def _cell_summary(labels: Sequence[_EligibleLabel]) -> JSONMap:
    first = labels[0]
    per_seed_values: dict[int, list[float]] = {}
    bodies: dict[str, ProgramDecisionV1] = {}
    for label in labels:
        per_seed_values.setdefault(label.seed, []).append(label.paired_delta)
        body_key = _canonical(label.candidate_decision.to_dict())
        bodies[body_key] = label.candidate_decision
    seed_means = [
        mean(values) for _, values in sorted(per_seed_values.items())
    ]
    return {
        "semantic_key": first.semantic_key,
        "wave_stratum": first.wave_stratum,
        "live_target_count": first.live_target_count,
        "guard_target_index": first.guard_target_index,
        "action_ready": first.readiness_action.to_wire(),
        "expected_cat_gcd_action": first.expected_cat_gcd_action.to_wire(),
        "mutation": first.mutation,
        "complete_label_count": len(labels),
        "distinct_proposal_seed_count": len(seed_means),
        "mean_paired_effective_damage_delta": mean(seed_means),
        "positive_proposal_seed_fraction": (
            sum(value > 0 for value in seed_means) / len(seed_means)
        ),
        "candidate_decision_body_count": len(bodies),
        "candidate_decision": (
            next(iter(bodies.values())).to_dict() if len(bodies) == 1 else None
        ),
    }


def _rank_key(cell: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -float(cell["mean_paired_effective_damage_delta"]),
        -float(cell["positive_proposal_seed_fraction"]),
        -int(cell["distinct_proposal_seed_count"]),
        str(cell["semantic_key"]),
    )


def _validate_distilled_step(step: CatResidualSequenceStepV1) -> None:
    guard = step.guard
    disallowed = (
        guard.pull_relative_time_gte_ms,
        guard.pull_relative_time_lte_ms,
        guard.rage_gte,
        guard.rage_lte,
        guard.target_hp_pct_gte,
        guard.target_hp_pct_lte,
        guard.target_aura_action,
        guard.estimated_remaining_attackable_gte_ms,
        guard.attackable_target_count_gte,
        guard.attackable_target_count_lte,
        guard.mh_swing_remaining_lte_ms,
        guard.queue_status_is,
        guard.aura_action,
    )
    if any(value is not None for value in disallowed):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distilled step guard exceeds live-count/readiness/nonprecombat v8 contract"
        )
    if (
        guard.target_index is None
        or guard.target_attackable_is is not True
        or guard.live_target_count_gte is None
        or guard.live_target_count_lte != guard.live_target_count_gte
        or guard.action_ready is None
        or guard.false_semantics != SKIP_PLAN
        or step.expected_cat_gcd_action is None
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distilled step lacks its exact observable v8 guard contract"
        )
    if RECKLESSNESS in _decision_actions(step.decision):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distilled step contains mechanics-excluded action 1719"
        )


def load_upper_kara_cat_action_plan_distillation_v8(
    value: object,
) -> dict[str, DevelopmentTwoWaveCatResidualSequenceV1]:
    """Validate a portable bundle and restore policies keyed by policy ID.

    The returned key is the runtime program/binding identity source.  The
    caller can therefore rebuild one imported-reactive binding per full
    residual wire without depending on candidate list position.
    """

    if not isinstance(value, Mapping):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distillation bundle must be an object"
        )
    if value.get("schema") != SCHEMA or value.get("scope") != SCOPE:
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distillation bundle schema or scope differs"
        )
    exact_build_id = value.get("exact_build_id")
    if not isinstance(exact_build_id, str) or not exact_build_id.strip():
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distillation bundle lacks exact_build_id"
        )
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distillation bundle candidates must be nonempty"
        )
    if (
        not isinstance(candidates[0], Mapping)
        or candidates[0].get("kind") != "EXACT_CAT_ZERO_RESIDUAL"
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "the first candidate must be the empty exact-Cat residual"
        )
    restored: dict[str, DevelopmentTwoWaveCatResidualSequenceV1] = {}
    candidate_ids: set[str] = set()
    zero_count = 0
    nonzero_count = 0
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            raise UpperKaraCatActionPlanDistillationV8Error(
                f"candidates[{index}] must be an object"
            )
        candidate_id = raw.get("candidate_id")
        kind = raw.get("kind")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in candidate_ids
        ):
            raise UpperKaraCatActionPlanDistillationV8Error(
                "candidate IDs must be nonempty and unique"
            )
        candidate_ids.add(candidate_id)
        try:
            policy = two_wave_cat_residual_sequence_from_dict_v1(
                raw.get("policy_wire")
            )
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise UpperKaraCatActionPlanDistillationV8Error(
                f"candidate {candidate_id!r} has an invalid residual wire: {error}"
            ) from error
        if policy.exact_build_id != exact_build_id:
            raise UpperKaraCatActionPlanDistillationV8Error(
                "candidate residual exact build differs from bundle"
            )
        if policy.policy_id in restored:
            raise UpperKaraCatActionPlanDistillationV8Error(
                "candidate residual policy IDs must be unique"
            )
        if kind == "EXACT_CAT_ZERO_RESIDUAL":
            zero_count += 1
            if index != 0 or policy.steps:
                raise UpperKaraCatActionPlanDistillationV8Error(
                    "the first candidate must be the empty exact-Cat residual"
                )
        elif kind == "SINGLE_STEP_CAT_RELATIVE_RESIDUAL":
            nonzero_count += 1
            if len(policy.steps) != 1:
                raise UpperKaraCatActionPlanDistillationV8Error(
                    "nonzero v8 candidates must contain exactly one step"
                )
            _validate_distilled_step(policy.steps[0])
        else:
            raise UpperKaraCatActionPlanDistillationV8Error(
                f"candidate {candidate_id!r} has an unsupported kind"
            )
        restored[policy.policy_id] = policy
    if zero_count != 1:
        raise UpperKaraCatActionPlanDistillationV8Error(
            "distillation bundle must contain exactly one exact-Cat residual"
        )
    if value.get("selected_nonzero_candidate_count") != nonzero_count:
        raise UpperKaraCatActionPlanDistillationV8Error(
            "selected_nonzero_candidate_count differs from candidate wires"
        )
    maximum = value.get("max_nonzero_candidates")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < nonzero_count
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "candidate bundle exceeds max_nonzero_candidates"
        )
    return restored


def distill_upper_kara_cat_action_plan_teacher_v8(
    teacher_results: Sequence[Mapping[str, Any]],
    *,
    proposal_seeds: Iterable[int],
    exact_build_id: str,
    loadout_id: str,
    waves: tuple[CatResidualSequenceWaveV1, CatResidualSequenceWaveV1],
    policy_id_prefix: str,
    max_nonzero_candidates: int,
    min_distinct_proposal_seeds: int = 1,
    minimum_mean_paired_effective_damage_delta: float = 0.0,
) -> JSONMap:
    """Return exact Cat plus bounded one-step candidates for fresh testing.

    ``proposal_seeds`` is the caller's predeclared cohort authority.  Results
    for every other seed are ignored.  Missing or duplicate shard detection is
    intentionally left to the remote campaign combiner; repeated labels here
    are averaged first within seed and therefore cannot overweight a seed.
    """

    if not isinstance(teacher_results, Sequence) or isinstance(
        teacher_results, (str, bytes, bytearray)
    ):
        raise TypeError("teacher_results must be a sequence of mappings")
    if any(not isinstance(row, Mapping) for row in teacher_results):
        raise TypeError("teacher_results must contain mappings")
    proposal = _seed_set(proposal_seeds)
    exact_build = _text(exact_build_id, "exact_build_id")
    requested_loadout = _text(loadout_id, "loadout_id")
    prefix = _text(policy_id_prefix, "policy_id_prefix")
    if (
        isinstance(max_nonzero_candidates, bool)
        or not isinstance(max_nonzero_candidates, int)
        or max_nonzero_candidates < 0
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "max_nonzero_candidates must be a nonnegative integer"
        )
    if (
        isinstance(min_distinct_proposal_seeds, bool)
        or not isinstance(min_distinct_proposal_seeds, int)
        or min_distinct_proposal_seeds < 1
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "min_distinct_proposal_seeds must be a positive integer"
        )
    if (
        isinstance(minimum_mean_paired_effective_damage_delta, bool)
        or not isinstance(
            minimum_mean_paired_effective_damage_delta, (int, float)
        )
        or not math.isfinite(float(minimum_mean_paired_effective_damage_delta))
    ):
        raise UpperKaraCatActionPlanDistillationV8Error(
            "minimum_mean_paired_effective_damage_delta must be finite"
        )
    if (
        not isinstance(waves, tuple)
        or len(waves) != 2
        or any(not isinstance(row, CatResidualSequenceWaveV1) for row in waves)
    ):
        raise TypeError("waves must contain exactly two residual wave rows")

    rejected_results: Counter[str] = Counter()
    rejected_labels: Counter[str] = Counter()
    ignored_nonproposal_result_count = 0
    accepted_results = 0
    labels: list[_EligibleLabel] = []
    for result in teacher_results:
        seed = result.get("master_seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            rejected_results["INVALID_MASTER_SEED"] += 1
            continue
        simulator_seed = result.get("simulator_seed")
        if (
            isinstance(simulator_seed, bool)
            or not isinstance(simulator_seed, int)
            or simulator_seed < 0
        ):
            rejected_results["INVALID_SIMULATOR_SEED"] += 1
            continue
        if seed not in proposal:
            ignored_nonproposal_result_count += 1
            continue
        if result.get("schema") != TEACHER_SCHEMA:
            rejected_results["WRONG_TEACHER_SCHEMA"] += 1
            continue
        if result.get("build_id") != exact_build:
            rejected_results["WRONG_BUILD"] += 1
            continue
        if result.get("loadout_id") != requested_loadout:
            rejected_results["WRONG_LOADOUT"] += 1
            continue
        baseline = result.get("baseline_terminal")
        if (
            result.get("status") != _COMPLETE_TEACHER_STATUS
            or not isinstance(baseline, Mapping)
            or baseline.get("status") != _COMPLETE_TERMINAL_STATUS
        ):
            rejected_results["BASELINE_OR_TEACHER_INCOMPLETE"] += 1
            continue
        branches = result.get("branches")
        if not isinstance(branches, list):
            rejected_results["MALFORMED_BRANCH_ARRAY"] += 1
            continue
        accepted_results += 1
        for branch in branches:
            label, reason = _eligible_label(result, branch)
            if label is None:
                assert reason is not None
                rejected_labels[reason] += 1
            else:
                labels.append(label)

    grouped: dict[str, list[_EligibleLabel]] = {}
    for label in labels:
        grouped.setdefault(label.semantic_key, []).append(label)
    cells = [_cell_summary(rows) for _, rows in sorted(grouped.items())]
    eligible_cells = [
        cell
        for cell in cells
        if cell["candidate_decision_body_count"] == 1
        and cell["distinct_proposal_seed_count"]
        >= min_distinct_proposal_seeds
        and cell["mean_paired_effective_damage_delta"]
        > float(minimum_mean_paired_effective_damage_delta)
    ]
    selected_cells = sorted(eligible_cells, key=_rank_key)[
        :max_nonzero_candidates
    ]

    zero = freeze_two_wave_cat_residual_sequence_v1(
        policy_id=f"{prefix}::exact-cat-zero",
        exact_build_id=exact_build,
        waves=waves,
        steps=(),
    )
    candidates: list[JSONMap] = [
        {
            "candidate_id": "exact-cat-zero",
            "kind": "EXACT_CAT_ZERO_RESIDUAL",
            "proposal_cell": None,
            "policy_wire": frozen_two_wave_cat_residual_sequence_wire_v1(zero),
        }
    ]
    for rank, cell in enumerate(selected_cells, start=1):
        wave_position = _WAVE_POSITION[str(cell["wave_stratum"])]
        wave = waves[wave_position]
        guard = ObservableCausalGuardV1(
            target_index=int(cell["guard_target_index"]),
            target_attackable_is=True,
            live_target_count_gte=int(cell["live_target_count"]),
            live_target_count_lte=int(cell["live_target_count"]),
            action_ready=ActionRef.from_wire(cell["action_ready"]),
            false_semantics=SKIP_PLAN,
        )
        decision = program_decision_from_dict_v1(cell["candidate_decision"])
        step = CatResidualSequenceStepV1(
            step_id=f"proposal-step-{rank:03d}",
            wave_id=wave.wave_id,
            guard=guard,
            decision=decision,
            expected_cat_gcd_action=ActionRef.from_wire(
                cell["expected_cat_gcd_action"]
            ),
        )
        try:
            policy = freeze_two_wave_cat_residual_sequence_v1(
                policy_id=f"{prefix}::proposal-{rank:03d}",
                exact_build_id=exact_build,
                waves=waves,
                steps=(step,),
            )
        except (TypeError, ValueError) as error:
            raise UpperKaraCatActionPlanDistillationV8Error(
                "selected teacher cell cannot form a valid wave-local residual: "
                f"{error}"
            ) from error
        candidates.append(
            {
                "candidate_id": f"proposal-{rank:03d}",
                "kind": "SINGLE_STEP_CAT_RELATIVE_RESIDUAL",
                "proposal_cell": {
                    key: value
                    for key, value in cell.items()
                    if key not in {"semantic_key", "candidate_decision"}
                },
                "policy_wire": frozen_two_wave_cat_residual_sequence_wire_v1(
                    policy
                ),
            }
        )

    return {
        "schema": SCHEMA,
        "scope": SCOPE,
        "status": (
            "EXACT_CAT_AND_PROPOSAL_CANDIDATES_FROZEN_FOR_FRESH_TEST"
            if len(candidates) > 1
            else "EXACT_CAT_ONLY_NO_ELIGIBLE_PROPOSAL_CELL"
        ),
        "exact_build_id": exact_build,
        "loadout_id": requested_loadout,
        "input_teacher_result_count": len(teacher_results),
        "accepted_proposal_teacher_result_count": accepted_results,
        "ignored_nonproposal_teacher_result_count": (
            ignored_nonproposal_result_count
        ),
        "complete_mechanics_eligible_label_count": len(labels),
        "rejected_result_counts": dict(sorted(rejected_results.items())),
        "rejected_label_counts": dict(sorted(rejected_labels.items())),
        "semantic_cell_count": len(cells),
        "eligible_semantic_cell_count": len(eligible_cells),
        "selected_nonzero_candidate_count": len(candidates) - 1,
        "max_nonzero_candidates": max_nonzero_candidates,
        "selection_contract": {
            "cohort": "PREDECLARED_PROPOSAL_SEEDS_ONLY",
            "comparison": "BOTH_CAT_AND_BRANCH_TERMINALS_COMPLETED",
            "mechanics_excluded_actions": [RECKLESSNESS.to_wire()],
            "grouping": "CAT_RELATIVE_SEMANTIC_MUTATION_AND_OBSERVABLE_WAVE_CELL",
            "repeat_weighting": "MEAN_WITHIN_SEED_THEN_MEAN_ACROSS_SEEDS",
            "minimum_distinct_proposal_seeds": min_distinct_proposal_seeds,
            "minimum_mean_paired_effective_damage_delta_exclusive": float(
                minimum_mean_paired_effective_damage_delta
            ),
            "ranking": "MEAN_DELTA_THEN_POSITIVE_FRACTION_THEN_SUPPORT",
            "single_step_only": True,
            "fresh_full_route_test_required": True,
            "teacher_label_is_not_policy_outcome": True,
        },
        "portable_bundle_contract": {
            "policy_payload": "FULL_DEVELOPMENT_TWO_WAVE_CAT_RESIDUAL_SEQUENCE_V1_WIRE",
            "runtime_identity": "POLICY_WIRE_POLICY_ID",
            "exact_cat_zero_included": True,
            "file_io_performed": False,
        },
        "semantic_cells": [
            {
                key: value
                for key, value in cell.items()
                if key not in {"semantic_key", "candidate_decision"}
            }
            for cell in sorted(cells, key=_rank_key)
        ],
        "candidates": candidates,
        "deployment_authorized": False,
        "scientific_comparison_complete": False,
    }


__all__ = (
    "SCHEMA",
    "SCOPE",
    "UpperKaraCatActionPlanDistillationV8Error",
    "distill_upper_kara_cat_action_plan_teacher_v8",
    "load_upper_kara_cat_action_plan_distillation_v8",
)
