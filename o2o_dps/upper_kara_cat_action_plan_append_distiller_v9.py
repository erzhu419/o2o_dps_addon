"""Distill parent-relative v9 labels into one-step append candidates.

The v9 teacher evaluates a single intervention only after every existing
step for the current wave has executed.  This module keeps the frozen v8
parent as candidate zero and turns a supported semantic label into a full
portable policy containing the unchanged parent prefix plus one new step.
Teacher coordinates, simulator seeds, time, HP, and future route state never
enter the policy wire.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
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
    DevelopmentTwoWaveCatResidualSequenceV1,
    freeze_two_wave_cat_residual_sequence_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
    two_wave_cat_residual_sequence_from_dict_v1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError
from .upper_kara_cat_action_plan_distiller_v8 import (
    _available_now,
    _current_attackable_target,
    _decision_actions,
    _readiness_action,
    _validate_distilled_step,
    _visible_live_target_count,
)
from .upper_kara_cat_action_plan_append_teacher_v9 import (
    COMPLETE_BRANCH_STATUS as _COMPLETE_LABEL_STATUS,
    COMPLETE_STATUS as _COMPLETE_TEACHER_STATUS,
    SCHEMA as TEACHER_SCHEMA,
    SCOPE as TEACHER_SCOPE,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_append_distillation/v9"
SCOPE = "DEVELOPMENT_PARENT_RELATIVE_APPEND_LABELS_TO_FRESH_TEST_CANDIDATES"
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
_RECKLESSNESS = ActionRef(spell_id=1_719)
_MINIMUM_SUPPORT = 8
_MAXIMUM_APPEND_CANDIDATES = 63


class UpperKaraCatActionPlanAppendDistillationV9Error(ValueError):
    """The frozen parent, teacher evidence, or append bundle is invalid."""


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
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _seed_set(values: Iterable[int]) -> frozenset[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "proposal_seeds must contain nonnegative integers"
            )
        result.add(value)
    if not result:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "proposal_seeds must be nonempty"
        )
    return frozenset(result)


def _parent_relative_mutation(
    parent: ProgramDecisionV1,
    candidate: ProgramDecisionV1,
) -> JSONMap:
    source = parent.to_dict()
    replacement = candidate.to_dict()
    changed: JSONMap = {}
    for field in _DECISION_FIELDS:
        if source[field] != replacement[field]:
            changed[field] = {
                "parent": source[field],
                "candidate": replacement[field],
            }
    return {
        "schema": "parent_relative_action_plan_mutation/v9",
        "changed_fields": changed,
    }


def _step_key_rows(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, list):
        return None
    parsed: list[tuple[str, str]] = []
    for row in value:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or any(not isinstance(item, str) or not item for item in row)
        ):
            return None
        parsed.append((row[0], row[1]))
    return tuple(parsed)


def _ordered_subsequence(
    required: tuple[tuple[str, str], ...],
    observed: tuple[tuple[str, str], ...],
) -> bool:
    cursor = 0
    for value in observed:
        if cursor < len(required) and value == required[cursor]:
            cursor += 1
    return cursor == len(required)


def _parent_wave_step_keys(
    parent: DevelopmentTwoWaveCatResidualSequenceV1,
    wave_id: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (step.wave_id, step.step_id)
        for step in parent.steps
        if step.wave_id == wave_id
    )


@dataclass(frozen=True)
class _EligibleAppendLabel:
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
                "expected_cat_gcd_action": (
                    self.expected_cat_gcd_action.to_wire()
                ),
                "mutation": self.mutation,
            }
        )


def _eligible_append_label(
    result: Mapping[str, Any],
    branch: object,
    parent: DevelopmentTwoWaveCatResidualSequenceV1,
) -> tuple[_EligibleAppendLabel | None, str | None]:
    if not isinstance(branch, Mapping):
        return None, "MALFORMED_BRANCH"
    terminal = branch.get("branch_terminal")
    delta = branch.get("paired_effective_damage_delta")
    required_flags = (
        "strict_single_intervention_verified",
        "parent_execution_prefix_verified",
        "parent_decision_prefix_verified",
        "causal_observation_verified",
        "append_after_parent_steps_verified",
    )
    if (
        branch.get("status") != _COMPLETE_LABEL_STATUS
        or not isinstance(terminal, Mapping)
        or terminal.get("status") != _COMPLETE_TERMINAL_STATUS
        or any(branch.get(field) is not True for field in required_flags)
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(float(delta))
    ):
        return None, "COMPARISON_OR_PARENT_PREFIX_INCOMPLETE"

    try:
        cat = program_decision_from_dict_v1(branch.get("cat_decision"))
        parent_decision = program_decision_from_dict_v1(
            branch.get("parent_decision")
        )
        candidate = program_decision_from_dict_v1(
            branch.get("candidate_decision")
        )
    except (TypeError, ValueError, SimBridgeProtocolError):
        return None, "MALFORMED_DECISION"
    if cat.gcd_action is None:
        return None, "CAT_SOURCE_GCD_ABSENT"
    # Once every parent step for this wave has executed, the frozen parent
    # must have fallen back to the exact Cat decision.  Otherwise appending a
    # runtime step cannot reproduce the teacher's source epoch.
    if parent_decision != cat:
        return None, "PARENT_DECISION_DIFFERS_FROM_CAT_AFTER_PREFIX"
    if _RECKLESSNESS in _decision_actions(candidate):
        return None, "MECHANICS_EXCLUDED_ACTION_1719"

    mutation = _parent_relative_mutation(parent_decision, candidate)
    if not mutation["changed_fields"]:
        return None, "NO_PARENT_RELATIVE_MUTATION"
    readiness_action = _readiness_action(parent_decision, candidate)
    if readiness_action is None or not _available_now(branch, readiness_action):
        return None, "NO_CURRENT_READY_MUTATION_ACTION"

    wave_stratum = branch.get("wave_stratum")
    live_count = branch.get("live_target_count")
    if wave_stratum not in _WAVE_POSITION:
        return None, "UNSUPPORTED_WAVE_STRATUM"
    if (
        isinstance(live_count, bool)
        or not isinstance(live_count, int)
        or live_count < 1
    ):
        return None, "INVALID_LIVE_TARGET_COUNT"
    wave = parent.waves[_WAVE_POSITION[wave_stratum]]
    required = _step_key_rows(branch.get("required_parent_wave_step_keys"))
    before = _step_key_rows(
        branch.get("parent_executed_step_keys_before_branch")
    )
    after = _step_key_rows(
        branch.get("parent_executed_step_keys_after_branch")
    )
    expected = _parent_wave_step_keys(parent, wave.wave_id)
    if required is None or before is None or after is None:
        return None, "MALFORMED_PARENT_STEP_KEYS"
    if required != expected:
        return None, "REQUIRED_PARENT_WAVE_PREFIX_MISMATCH"
    if not _ordered_subsequence(expected, before) or not _ordered_subsequence(
        expected, after
    ):
        return None, "PARENT_WAVE_PREFIX_NOT_EXECUTED_IN_ORDER"

    observation = branch.get("policy_observation")
    if not isinstance(observation, Mapping):
        return None, "MALFORMED_POLICY_OBSERVATION"
    target_index = _current_attackable_target(observation)
    if target_index is None:
        return None, "PRECOMBAT_OR_TARGET_NOT_ATTACKABLE"
    if target_index not in wave.target_indexes:
        return None, "TARGET_OUTSIDE_DECLARED_WAVE"
    if _visible_live_target_count(observation) != live_count:
        return None, "LIVE_TARGET_COUNT_MISMATCH"

    seed = result.get("master_seed")
    assert isinstance(seed, int) and not isinstance(seed, bool)
    return (
        _EligibleAppendLabel(
            seed=seed,
            wave_stratum=wave_stratum,
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


def _cell_summary(labels: Sequence[_EligibleAppendLabel]) -> JSONMap:
    first = labels[0]
    per_seed_values: dict[int, list[float]] = {}
    bodies: dict[str, ProgramDecisionV1] = {}
    for label in labels:
        per_seed_values.setdefault(label.seed, []).append(label.paired_delta)
        bodies[_canonical(label.candidate_decision.to_dict())] = (
            label.candidate_decision
        )
    seed_means = [mean(values) for _, values in sorted(per_seed_values.items())]
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


def _contains_recklessness(step: CatResidualSequenceStepV1) -> bool:
    return _RECKLESSNESS in _decision_actions(step.decision)


def _restore_parent(value: object) -> DevelopmentTwoWaveCatResidualSequenceV1:
    try:
        parent = two_wave_cat_residual_sequence_from_dict_v1(value)
    except (TypeError, ValueError, SimBridgeProtocolError) as error:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            f"parent policy wire is invalid: {error}"
        ) from error
    if any(_contains_recklessness(step) for step in parent.steps):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "parent policy contains mechanics-excluded action 1719"
        )
    return parent


def load_upper_kara_cat_action_plan_append_distillation_v9(
    value: object,
) -> dict[str, DevelopmentTwoWaveCatResidualSequenceV1]:
    """Strictly restore a portable parent-plus-one candidate bundle."""

    if not isinstance(value, Mapping):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "append distillation bundle must be an object"
        )
    if value.get("schema") != SCHEMA or value.get("scope") != SCOPE:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "append distillation bundle schema or scope differs"
        )
    parent_wire = value.get("parent_policy_wire")
    parent = _restore_parent(parent_wire)
    if value.get("parent_policy_id") != parent.policy_id:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "bundle parent policy identity differs from its wire"
        )
    if value.get("exact_build_id") != parent.exact_build_id:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "bundle exact build differs from parent wire"
        )
    if value.get("parent_step_count") != len(parent.steps):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "bundle parent step count differs from parent wire"
        )
    _text(value.get("loadout_id"), "loadout_id")
    minimum_support = value.get("minimum_distinct_proposal_seeds")
    if (
        isinstance(minimum_support, bool)
        or not isinstance(minimum_support, int)
        or minimum_support < _MINIMUM_SUPPORT
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "append bundle requires at least eight distinct proposal seeds"
        )
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "append candidates must be a nonempty array"
        )
    if (
        not isinstance(candidates[0], Mapping)
        or candidates[0].get("kind") != "FROZEN_V8_PARENT_UNCHANGED"
        or candidates[0].get("policy_wire") != parent_wire
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "candidate zero must be the frozen parent unchanged"
        )

    restored: dict[str, DevelopmentTwoWaveCatResidualSequenceV1] = {}
    candidate_ids: set[str] = set()
    append_count = 0
    parent_steps = [step.to_dict() for step in parent.steps]
    parent_waves = [wave.to_dict() for wave in parent.waves]
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                f"candidates[{index}] must be an object"
            )
        candidate_id = raw.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in candidate_ids
        ):
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate IDs must be nonempty and unique"
            )
        candidate_ids.add(candidate_id)
        try:
            policy = two_wave_cat_residual_sequence_from_dict_v1(
                raw.get("policy_wire")
            )
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                f"candidate {candidate_id!r} has an invalid policy wire: {error}"
            ) from error
        if policy.policy_id in restored:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate policy IDs must be unique"
            )
        if raw.get("policy_id") != policy.policy_id:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate policy_id differs from its policy wire"
            )
        if raw.get("parent_policy_id") != parent.policy_id:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate parent_policy_id differs from frozen parent"
            )
        if policy.exact_build_id != parent.exact_build_id:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate exact build differs from parent"
            )
        if [wave.to_dict() for wave in policy.waves] != parent_waves:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "candidate wave registry differs from parent"
            )
        kind = raw.get("kind")
        if kind == "FROZEN_V8_PARENT_UNCHANGED":
            if (
                index != 0
                or raw.get("policy_wire") != parent_wire
                or raw.get("proposal_cell") is not None
            ):
                raise UpperKaraCatActionPlanAppendDistillationV9Error(
                    "candidate zero must be the frozen parent unchanged"
                )
        elif kind == "PARENT_PLUS_ONE_APPEND":
            append_count += 1
            proposal_cell = raw.get("proposal_cell")
            if (
                not isinstance(proposal_cell, Mapping)
                or isinstance(
                    proposal_cell.get("distinct_proposal_seed_count"), bool
                )
                or not isinstance(
                    proposal_cell.get("distinct_proposal_seed_count"), int
                )
                or proposal_cell.get("distinct_proposal_seed_count")
                < minimum_support
            ):
                raise UpperKaraCatActionPlanAppendDistillationV9Error(
                    "append candidate lacks its minimum proposal-seed support"
                )
            steps = [step.to_dict() for step in policy.steps]
            if (
                len(steps) != len(parent_steps) + 1
                or steps[:-1] != parent_steps
            ):
                raise UpperKaraCatActionPlanAppendDistillationV9Error(
                    "append candidate must preserve parent.steps and add exactly one"
                )
            try:
                _validate_distilled_step(policy.steps[-1])
            except ValueError as error:
                raise UpperKaraCatActionPlanAppendDistillationV9Error(
                    f"appended step violates the observable guard contract: {error}"
                ) from error
            if _contains_recklessness(policy.steps[-1]):
                raise UpperKaraCatActionPlanAppendDistillationV9Error(
                    "append candidate contains mechanics-excluded action 1719"
                )
        else:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                f"candidate {candidate_id!r} has an unsupported kind"
            )
        restored[policy.policy_id] = policy

    if append_count != value.get("selected_append_candidate_count"):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "selected_append_candidate_count differs from candidate wires"
        )
    maximum = value.get("max_append_candidates")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < append_count
        or maximum > _MAXIMUM_APPEND_CANDIDATES
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "max_append_candidates is invalid or exceeded"
        )
    return restored


def distill_upper_kara_cat_action_plan_append_teacher_v9(
    teacher_results: Sequence[Mapping[str, Any]],
    *,
    parent_policy_wire: Mapping[str, Any],
    proposal_seeds: Iterable[int],
    loadout_id: str,
    policy_id_prefix: str,
    max_append_candidates: int = _MAXIMUM_APPEND_CANDIDATES,
    min_distinct_proposal_seeds: int = _MINIMUM_SUPPORT,
    minimum_mean_paired_effective_damage_delta: float = 0.0,
) -> JSONMap:
    """Return the unchanged frozen parent plus supported one-step appends."""

    if not isinstance(teacher_results, Sequence) or isinstance(
        teacher_results, (str, bytes, bytearray)
    ):
        raise TypeError("teacher_results must be a sequence of mappings")
    if any(not isinstance(row, Mapping) for row in teacher_results):
        raise TypeError("teacher_results must contain mappings")
    if not isinstance(parent_policy_wire, Mapping):
        raise TypeError("parent_policy_wire must be a mapping")
    # Keep this exact structure for candidate zero; restoration separately
    # proves that it is a valid closed runtime wire.
    parent_wire: JSONMap = deepcopy(dict(parent_policy_wire))
    parent = _restore_parent(parent_wire)
    proposal = _seed_set(proposal_seeds)
    requested_loadout = _text(loadout_id, "loadout_id")
    prefix = _text(policy_id_prefix, "policy_id_prefix")
    if (
        isinstance(max_append_candidates, bool)
        or not isinstance(max_append_candidates, int)
        or max_append_candidates < 0
        or max_append_candidates > _MAXIMUM_APPEND_CANDIDATES
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "max_append_candidates must be between 0 and 63"
        )
    if (
        isinstance(min_distinct_proposal_seeds, bool)
        or not isinstance(min_distinct_proposal_seeds, int)
        or min_distinct_proposal_seeds < _MINIMUM_SUPPORT
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "min_distinct_proposal_seeds must be at least 8"
        )
    if (
        isinstance(minimum_mean_paired_effective_damage_delta, bool)
        or not isinstance(
            minimum_mean_paired_effective_damage_delta, (int, float)
        )
        or not math.isfinite(float(minimum_mean_paired_effective_damage_delta))
    ):
        raise UpperKaraCatActionPlanAppendDistillationV9Error(
            "minimum_mean_paired_effective_damage_delta must be finite"
        )

    rejected_results: Counter[str] = Counter()
    rejected_labels: Counter[str] = Counter()
    ignored_nonproposal = 0
    accepted_results = 0
    labels: list[_EligibleAppendLabel] = []
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
            ignored_nonproposal += 1
            continue
        if result.get("schema") != TEACHER_SCHEMA:
            rejected_results["WRONG_TEACHER_SCHEMA"] += 1
            continue
        if result.get("scope") != TEACHER_SCOPE:
            rejected_results["WRONG_TEACHER_SCOPE"] += 1
            continue
        if result.get("build_id") != parent.exact_build_id:
            rejected_results["WRONG_BUILD"] += 1
            continue
        if result.get("loadout_id") != requested_loadout:
            rejected_results["WRONG_LOADOUT"] += 1
            continue
        if (
            result.get("parent_policy_id") != parent.policy_id
            or result.get("parent_step_count") != len(parent.steps)
            or result.get("parent_policy_wire") != parent_wire
        ):
            rejected_results["WRONG_PARENT_POLICY"] += 1
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
            label, reason = _eligible_append_label(result, branch, parent)
            if label is None:
                assert reason is not None
                rejected_labels[reason] += 1
            else:
                labels.append(label)

    grouped: dict[str, list[_EligibleAppendLabel]] = {}
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
        :max_append_candidates
    ]

    candidates: list[JSONMap] = [
        {
            "candidate_id": "frozen-v8-parent",
            "kind": "FROZEN_V8_PARENT_UNCHANGED",
            "policy_id": parent.policy_id,
            "parent_policy_id": parent.policy_id,
            "proposal_cell": None,
            "policy_wire": deepcopy(parent_wire),
        }
    ]
    parent_step_ids = {(step.wave_id, step.step_id) for step in parent.steps}
    for rank, cell in enumerate(selected_cells, start=1):
        wave = parent.waves[_WAVE_POSITION[str(cell["wave_stratum"])]]
        step_id = f"append-v9-{rank:03d}"
        collision = 0
        while (wave.wave_id, step_id) in parent_step_ids:
            collision += 1
            step_id = f"append-v9-{rank:03d}-{collision}"
        step = CatResidualSequenceStepV1(
            step_id=step_id,
            wave_id=wave.wave_id,
            guard=ObservableCausalGuardV1(
                target_index=int(cell["guard_target_index"]),
                target_attackable_is=True,
                live_target_count_gte=int(cell["live_target_count"]),
                live_target_count_lte=int(cell["live_target_count"]),
                action_ready=ActionRef.from_wire(cell["action_ready"]),
                false_semantics=SKIP_PLAN,
            ),
            decision=program_decision_from_dict_v1(cell["candidate_decision"]),
            expected_cat_gcd_action=ActionRef.from_wire(
                cell["expected_cat_gcd_action"]
            ),
        )
        try:
            policy = freeze_two_wave_cat_residual_sequence_v1(
                policy_id=f"{prefix}::append-{rank:03d}",
                exact_build_id=parent.exact_build_id,
                waves=parent.waves,
                steps=(*parent.steps, step),
            )
        except (TypeError, ValueError) as error:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "selected teacher cell cannot form a valid parent append: "
                f"{error}"
            ) from error
        policy_wire = frozen_two_wave_cat_residual_sequence_wire_v1(policy)
        if policy_wire["steps"][:-1] != parent_wire["steps"]:
            raise UpperKaraCatActionPlanAppendDistillationV9Error(
                "freezing an append changed the frozen parent step prefix"
            )
        candidates.append(
            {
                "candidate_id": f"append-{rank:03d}",
                "kind": "PARENT_PLUS_ONE_APPEND",
                "policy_id": policy.policy_id,
                "parent_policy_id": parent.policy_id,
                "proposal_cell": {
                    key: value
                    for key, value in cell.items()
                    if key not in {"semantic_key", "candidate_decision"}
                },
                "policy_wire": policy_wire,
            }
        )

    bundle: JSONMap = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "status": (
            "PARENT_AND_APPEND_CANDIDATES_FROZEN_FOR_FRESH_TEST"
            if len(candidates) > 1
            else "PARENT_ONLY_NO_ELIGIBLE_APPEND_CELL"
        ),
        "exact_build_id": parent.exact_build_id,
        "loadout_id": requested_loadout,
        "parent_policy_id": parent.policy_id,
        "parent_policy_wire": deepcopy(parent_wire),
        "parent_step_count": len(parent.steps),
        "input_teacher_result_count": len(teacher_results),
        "accepted_proposal_teacher_result_count": accepted_results,
        "ignored_nonproposal_teacher_result_count": ignored_nonproposal,
        "complete_mechanics_eligible_label_count": len(labels),
        "rejected_result_counts": dict(sorted(rejected_results.items())),
        "rejected_label_counts": dict(sorted(rejected_labels.items())),
        "semantic_cell_count": len(cells),
        "eligible_semantic_cell_count": len(eligible_cells),
        "selected_append_candidate_count": len(candidates) - 1,
        "max_append_candidates": max_append_candidates,
        "minimum_distinct_proposal_seeds": min_distinct_proposal_seeds,
        "selection_contract": {
            "cohort": "PREDECLARED_PROPOSAL_SEEDS_ONLY",
            "baseline": "FROZEN_V8_PARENT",
            "mechanics_excluded_actions": [_RECKLESSNESS.to_wire()],
            "grouping": "PARENT_RELATIVE_MUTATION_AND_OBSERVABLE_WAVE_CELL",
            "repeat_weighting": "MEAN_WITHIN_SEED_THEN_MEAN_ACROSS_SEEDS",
            "minimum_distinct_proposal_seeds": min_distinct_proposal_seeds,
            "minimum_mean_paired_effective_damage_delta_exclusive": float(
                minimum_mean_paired_effective_damage_delta
            ),
            "ranking": "MEAN_DELTA_THEN_POSITIVE_FRACTION_THEN_SUPPORT",
            "append_only": True,
            "fresh_full_route_test_required": True,
            "teacher_label_is_not_policy_outcome": True,
        },
        "portable_bundle_contract": {
            "policy_payload": (
                "FULL_DEVELOPMENT_TWO_WAVE_CAT_RESIDUAL_SEQUENCE_V1_WIRE"
            ),
            "candidate_zero": "FROZEN_V8_PARENT_UNCHANGED",
            "nonzero_candidate": "PARENT_STEPS_PLUS_EXACTLY_ONE_APPEND",
            "runtime_identity": "POLICY_WIRE_POLICY_ID",
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
    # Exercise the same strict loader used remotely before returning a bundle.
    load_upper_kara_cat_action_plan_append_distillation_v9(bundle)
    return bundle


__all__ = (
    "SCHEMA",
    "SCOPE",
    "TEACHER_SCHEMA",
    "UpperKaraCatActionPlanAppendDistillationV9Error",
    "distill_upper_kara_cat_action_plan_append_teacher_v9",
    "load_upper_kara_cat_action_plan_append_distillation_v9",
)
