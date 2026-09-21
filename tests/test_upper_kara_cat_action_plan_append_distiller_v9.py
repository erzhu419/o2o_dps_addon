from __future__ import annotations

from copy import deepcopy
import json

import pytest

from o2o_dps.causal_action_program_v1 import ProgramDecisionV1
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceStepV1,
    CatResidualSequenceWaveV1,
    freeze_two_wave_cat_residual_sequence_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_cat_action_plan_append_distiller_v9 import (
    TEACHER_SCHEMA,
    UpperKaraCatActionPlanAppendDistillationV9Error,
    distill_upper_kara_cat_action_plan_append_teacher_v9,
    load_upper_kara_cat_action_plan_append_distillation_v9,
)


BATTLE_SHOUT = ActionRef(spell_id=25_289)
BLOODTHIRST = ActionRef(spell_id=23_894)
WHIRLWIND = ActionRef(spell_id=1_680)
RECKLESSNESS = ActionRef(spell_id=1_719)


def _decision(action: ActionRef, target: int) -> ProgramDecisionV1:
    return ProgramDecisionV1(
        target_index=target,
        start_attack=True,
        gcd_action=action,
    )


def _parent_wire() -> dict:
    waves = (
        CatResidualSequenceWaveV1("wave-1", (0, 1)),
        CatResidualSequenceWaveV1("wave-2", (2,)),
    )
    steps = (
        CatResidualSequenceStepV1(
            step_id="parent-w1",
            wave_id="wave-1",
            guard=ObservableCausalGuardV1(
                target_index=0,
                target_attackable_is=True,
                live_target_count_gte=2,
                live_target_count_lte=2,
                action_ready=WHIRLWIND,
                false_semantics=SKIP_PLAN,
            ),
            decision=_decision(WHIRLWIND, 0),
            expected_cat_gcd_action=BATTLE_SHOUT,
        ),
        CatResidualSequenceStepV1(
            step_id="parent-w2",
            wave_id="wave-2",
            guard=ObservableCausalGuardV1(
                target_index=2,
                target_attackable_is=True,
                live_target_count_gte=1,
                live_target_count_lte=1,
                action_ready=BLOODTHIRST,
                false_semantics=SKIP_PLAN,
            ),
            decision=_decision(BLOODTHIRST, 2),
            expected_cat_gcd_action=WHIRLWIND,
        ),
    )
    policy = freeze_two_wave_cat_residual_sequence_v1(
        policy_id="v8-frozen-winner",
        exact_build_id="build-a",
        waves=waves,
        steps=steps,
    )
    return frozen_two_wave_cat_residual_sequence_wire_v1(policy)


def _observation(target: int, *, wave: int, live_count: int) -> dict:
    if wave == 1:
        targets = [
            {"target_index": 0, "attackable": True, "dead": live_count < 1},
            {"target_index": 1, "attackable": True, "dead": live_count < 2},
            {"target_index": 2, "attackable": False, "dead": False},
        ]
    else:
        targets = [
            {"target_index": 0, "attackable": False, "dead": True},
            {"target_index": 1, "attackable": False, "dead": True},
            {"target_index": 2, "attackable": True, "dead": False},
        ]
    return {
        "time_ms": 12_345,
        "pull_relative_time_ms": 8_765,
        "target_index": target,
        "target_health_pct": 42.0,
        "precombat": {"active": False},
        "dynamic_target_semantics": {"targets": targets},
    }


def _branch(
    *,
    candidate_action: ActionRef = BLOODTHIRST,
    delta: float = 10.0,
    wave: int = 1,
    live_count: int | None = None,
    complete: bool = True,
) -> dict:
    if live_count is None:
        live_count = 2 if wave == 1 else 1
    target = 0 if wave == 1 else 2
    wave_id = f"wave-{wave}"
    required = [[wave_id, f"parent-w{wave}"]]
    before = (
        [["wave-1", "parent-w1"]]
        if wave == 1
        else [["wave-1", "parent-w1"], ["wave-2", "parent-w2"]]
    )
    source = _decision(BATTLE_SHOUT, target)
    return {
        "decision_index": 91,
        "candidate_plan_index": 0,
        "wave_stratum": f"WAVE_{wave}",
        "live_target_count": live_count,
        "branch_key": "teacher-only-coordinate",
        "policy_observation": _observation(
            target, wave=wave, live_count=live_count
        ),
        "available_actions": [
            {
                "action": candidate_action.to_wire(),
                "legal": True,
                "ready_in_ms": 0,
            }
        ],
        "cat_decision": source.to_dict(),
        "parent_decision": source.to_dict(),
        "candidate_decision": _decision(candidate_action, target).to_dict(),
        "parent_executed_step_keys_before_branch": before,
        "parent_executed_step_keys_after_branch": before,
        "required_parent_wave_step_keys": required,
        "parent_execution_prefix_verified": True,
        "parent_decision_prefix_verified": True,
        "causal_observation_verified": True,
        "append_after_parent_steps_verified": True,
        "strict_single_intervention_verified": True,
        "execution_receipts": [],
        "branch_terminal": {
            "status": "COMPLETED" if complete else "INVALID",
        },
        "paired_effective_damage_delta": delta if complete else None,
        "status": (
            "COMPLETE_APPEND_BRANCH_TEACHER_LABEL"
            if complete
            else "INVALID_OR_INCOMPLETE_BRANCH"
        ),
    }


def _result(seed: int, branches: list[dict], *, parent_wire: dict) -> dict:
    return {
        "schema": TEACHER_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_CAT_ACTION_PLAN_APPEND_TEACHER_NONVOTING",
        "master_seed": seed,
        "simulator_seed": seed + 10_000,
        "build_id": "build-a",
        "loadout_id": "loadout-a",
        "parent_policy_id": "v8-frozen-winner",
        "parent_policy_wire": deepcopy(parent_wire),
        "parent_step_count": 2,
        "baseline_executed_step_keys": [
            ["wave-1", "parent-w1"],
            ["wave-2", "parent-w2"],
        ],
        "baseline_terminal": {"status": "COMPLETED"},
        "branches": branches,
    }


def _distill(
    results: list[dict], parent_wire: dict, **kwargs: object
) -> dict:
    options = {
        "parent_policy_wire": parent_wire,
        "proposal_seeds": tuple(range(11, 19)),
        "loadout_id": "loadout-a",
        "policy_id_prefix": "v9-test",
    }
    options.update(kwargs)
    return distill_upper_kara_cat_action_plan_append_teacher_v9(
        results, **options
    )


def test_candidate_zero_is_parent_and_nonzero_is_exactly_one_append() -> None:
    parent_wire = _parent_wire()
    results = [
        _result(seed, [_branch(delta=float(seed))], parent_wire=parent_wire)
        for seed in range(11, 19)
    ]

    bundle = _distill(results, parent_wire)

    assert bundle["selected_append_candidate_count"] == 1
    assert bundle["candidates"][0]["kind"] == "FROZEN_V8_PARENT_UNCHANGED"
    assert bundle["candidates"][0]["policy_id"] == "v8-frozen-winner"
    assert bundle["candidates"][0]["parent_policy_id"] == "v8-frozen-winner"
    assert bundle["candidates"][0]["policy_wire"] == parent_wire
    assert bundle["candidates"][0]["policy_wire"] is not parent_wire

    append_wire = bundle["candidates"][1]["policy_wire"]
    assert bundle["candidates"][1]["kind"] == "PARENT_PLUS_ONE_APPEND"
    assert bundle["candidates"][1]["policy_id"] == "v9-test::append-001"
    assert bundle["candidates"][1]["parent_policy_id"] == "v8-frozen-winner"
    assert append_wire["steps"][:-1] == parent_wire["steps"]
    assert len(append_wire["steps"]) == len(parent_wire["steps"]) + 1
    assert append_wire["steps"][-1]["wave_id"] == "wave-1"
    assert append_wire["steps"][-1]["expected_cat_gcd_action"] == (
        BATTLE_SHOUT.to_wire()
    )
    assert append_wire["steps"][-1]["decision"]["terminal"][
        "gcd_action"
    ] == BLOODTHIRST.to_wire()

    restored = load_upper_kara_cat_action_plan_append_distillation_v9(
        json.loads(json.dumps(bundle))
    )
    assert list(restored) == ["v8-frozen-winner", "v9-test::append-001"]
    assert [step.step_id for step in restored["v9-test::append-001"].steps] == [
        "parent-w1",
        "parent-w2",
        "append-v9-001",
    ]

    # Proposal-only coordinates and observations do not enter runtime wires.
    text = json.dumps(append_wire, sort_keys=True)
    assert "decision_index" not in text
    assert "branch_key" not in text
    assert "time_ms" not in text
    assert "target_health" not in text
    assert "seed" not in text


def test_requires_eight_distinct_predeclared_proposal_seeds() -> None:
    parent_wire = _parent_wire()
    # Seven declared seeds plus a very favorable undeclared seed are still
    # below the fixed support floor.
    results = [
        _result(seed, [_branch(delta=10)], parent_wire=parent_wire)
        for seed in range(11, 18)
    ]
    results.append(
        _result(99, [_branch(delta=999_999)], parent_wire=parent_wire)
    )
    bundle = _distill(
        results,
        parent_wire,
        proposal_seeds=tuple(range(11, 18)),
    )

    assert bundle["ignored_nonproposal_teacher_result_count"] == 1
    assert bundle["semantic_cell_count"] == 1
    assert bundle["semantic_cells"][0]["distinct_proposal_seed_count"] == 7
    assert bundle["selected_append_candidate_count"] == 0
    assert bundle["candidates"][0]["policy_wire"] == parent_wire

    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="at least 8",
    ):
        _distill(
            results,
            parent_wire,
            min_distinct_proposal_seeds=7,
        )


def test_duplicate_labels_cannot_inflate_seed_support() -> None:
    parent_wire = _parent_wire()
    results = [
        _result(
            11,
            [_branch(delta=100), _branch(delta=-100)] * 20,
            parent_wire=parent_wire,
        )
    ]
    results.extend(
        _result(seed, [_branch(delta=10)], parent_wire=parent_wire)
        for seed in range(12, 18)
    )
    bundle = _distill(results, parent_wire)

    assert bundle["semantic_cells"][0]["distinct_proposal_seed_count"] == 7
    assert bundle["selected_append_candidate_count"] == 0


def test_recklessness_and_unverified_parent_prefix_are_excluded() -> None:
    parent_wire = _parent_wire()
    results = []
    for seed in range(11, 19):
        invalid_prefix = _branch(candidate_action=BLOODTHIRST, delta=100)
        invalid_prefix["append_after_parent_steps_verified"] = False
        results.append(
            _result(
                seed,
                [
                    _branch(candidate_action=RECKLESSNESS, delta=50_000),
                    invalid_prefix,
                ],
                parent_wire=parent_wire,
            )
        )
    bundle = _distill(results, parent_wire)

    assert bundle["selected_append_candidate_count"] == 0
    assert bundle["rejected_label_counts"] == {
        "COMPARISON_OR_PARENT_PREFIX_INCOMPLETE": 8,
        "MECHANICS_EXCLUDED_ACTION_1719": 8,
    }


def test_wave_two_append_follows_parent_prefix_without_reordering_it() -> None:
    parent_wire = _parent_wire()
    results = [
        _result(
            seed,
            [_branch(candidate_action=WHIRLWIND, delta=20, wave=2)],
            parent_wire=parent_wire,
        )
        for seed in range(11, 19)
    ]
    bundle = _distill(results, parent_wire)
    append_wire = bundle["candidates"][1]["policy_wire"]

    assert append_wire["steps"][:2] == parent_wire["steps"]
    assert [row["wave_id"] for row in append_wire["steps"]] == [
        "wave-1",
        "wave-2",
        "wave-2",
    ]


def test_loader_rejects_parent_mutation_and_more_than_one_append() -> None:
    parent_wire = _parent_wire()
    bundle = _distill(
        [
            _result(seed, [_branch(delta=10)], parent_wire=parent_wire)
            for seed in range(11, 19)
        ],
        parent_wire,
    )

    changed_zero = deepcopy(bundle)
    changed_zero["candidates"][0]["policy_wire"]["policy_id"] = "not-parent"
    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="candidate zero",
    ):
        load_upper_kara_cat_action_plan_append_distillation_v9(changed_zero)

    changed_prefix = deepcopy(bundle)
    changed_prefix["candidates"][1]["policy_wire"]["steps"][0][
        "step_id"
    ] = "mutated-parent"
    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="preserve parent.steps",
    ):
        load_upper_kara_cat_action_plan_append_distillation_v9(changed_prefix)

    weak_support = deepcopy(bundle)
    weak_support["candidates"][1]["proposal_cell"][
        "distinct_proposal_seed_count"
    ] = 7
    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="proposal-seed support",
    ):
        load_upper_kara_cat_action_plan_append_distillation_v9(weak_support)

    two_appends = deepcopy(bundle)
    two_appends["candidates"][1]["policy_wire"]["steps"].append(
        deepcopy(two_appends["candidates"][1]["policy_wire"]["steps"][-1])
    )
    two_appends["candidates"][1]["policy_wire"]["steps"][-1][
        "step_id"
    ] = "second-append"
    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="exactly one",
    ):
        load_upper_kara_cat_action_plan_append_distillation_v9(two_appends)


def test_append_count_is_hard_capped_at_63() -> None:
    parent_wire = _parent_wire()
    with pytest.raises(
        UpperKaraCatActionPlanAppendDistillationV9Error,
        match="between 0 and 63",
    ):
        _distill([], parent_wire, max_append_candidates=64)
