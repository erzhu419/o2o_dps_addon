from __future__ import annotations

from copy import deepcopy
import json

import pytest

from o2o_dps.causal_action_program_v1 import ProgramDecisionV1
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceWaveV1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_cat_action_plan_distiller_v8 import (
    UpperKaraCatActionPlanDistillationV8Error,
    distill_upper_kara_cat_action_plan_teacher_v8,
    load_upper_kara_cat_action_plan_distillation_v8,
)
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import SCHEMA as TEACHER_SCHEMA


BATTLE_SHOUT = ActionRef(spell_id=25_289)
BLOODTHIRST = ActionRef(spell_id=23_894)
WHIRLWIND = ActionRef(spell_id=1_680)
RECKLESSNESS = ActionRef(spell_id=1_719)


def _waves() -> tuple[CatResidualSequenceWaveV1, CatResidualSequenceWaveV1]:
    return (
        CatResidualSequenceWaveV1("wave-1", (0, 1)),
        CatResidualSequenceWaveV1("wave-2", (2,)),
    )


def _decision(action: ActionRef, target: int) -> ProgramDecisionV1:
    return ProgramDecisionV1(
        target_index=target,
        start_attack=True,
        gcd_action=action,
    )


def _observation(target: int, *, wave: int, live_count: int) -> dict:
    if wave == 1:
        rows = [
            {"target_index": 0, "attackable": True, "dead": live_count < 1},
            {"target_index": 1, "attackable": True, "dead": live_count < 2},
            {"target_index": 2, "attackable": False, "dead": False},
        ]
    else:
        rows = [
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
        "dynamic_target_semantics": {"targets": rows},
    }


def _branch(
    *,
    decision_index: int,
    candidate_action: ActionRef,
    delta: float,
    wave: int = 1,
    live_count: int = 2,
    source_action: ActionRef = BATTLE_SHOUT,
    complete: bool = True,
) -> dict:
    target = 0 if wave == 1 else 2
    return {
        "decision_index": decision_index,
        "candidate_plan_index": 0,
        "wave_stratum": f"WAVE_{wave}",
        "live_target_count": live_count,
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
        "cat_decision": _decision(source_action, target).to_dict(),
        "candidate_decision": _decision(candidate_action, target).to_dict(),
        "strict_single_intervention_verified": True,
        "branch_terminal": {
            "status": "COMPLETED" if complete else "INVALID",
        },
        "paired_effective_damage_delta": delta if complete else None,
        "status": (
            "COMPLETE_BRANCH_TEACHER_LABEL"
            if complete
            else "INVALID_OR_INCOMPLETE_BRANCH"
        ),
    }


def _result(seed: int, branches: list[dict], *, complete: bool = True) -> dict:
    return {
        "schema": TEACHER_SCHEMA,
        "status": (
            "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING"
            if complete
            else "BASELINE_INCOMPLETE_NO_BRANCHES_SCORED"
        ),
        "master_seed": seed,
        "simulator_seed": seed + 1000,
        "build_id": "build-a",
        "loadout_id": "loadout-a",
        "baseline_terminal": {
            "status": "COMPLETED" if complete else "INVALID",
        },
        "branches": branches,
    }


def _distill(results: list[dict], **kwargs: object) -> dict:
    options = {
        "proposal_seeds": (11, 12),
        "exact_build_id": "build-a",
        "loadout_id": "loadout-a",
        "waves": _waves(),
        "policy_id_prefix": "v8-test",
        "max_nonzero_candidates": 4,
    }
    options.update(kwargs)
    return distill_upper_kara_cat_action_plan_teacher_v8(results, **options)


def test_clusters_by_cat_relative_semantics_not_seed_or_decision_index() -> None:
    bundle = _distill(
        [
            _result(11, [_branch(decision_index=3, candidate_action=WHIRLWIND, delta=10)]),
            _result(12, [_branch(decision_index=91, candidate_action=WHIRLWIND, delta=30)]),
        ],
        min_distinct_proposal_seeds=2,
    )

    assert bundle["semantic_cell_count"] == 1
    assert bundle["selected_nonzero_candidate_count"] == 1
    assert len(bundle["candidates"]) == 2
    proposal = bundle["candidates"][1]
    assert proposal["proposal_cell"]["distinct_proposal_seed_count"] == 2
    assert proposal["proposal_cell"]["mean_paired_effective_damage_delta"] == 20

    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    assert list(policies)[0] == "v8-test::exact-cat-zero"
    policy = policies["v8-test::proposal-001"]
    assert len(policy.steps) == 1
    step = policy.steps[0]
    assert step.wave_id == "wave-1"
    assert step.expected_cat_gcd_action == BATTLE_SHOUT
    assert step.decision.gcd_action == WHIRLWIND
    assert step.guard.live_target_count_gte == 2
    assert step.guard.live_target_count_lte == 2
    assert step.guard.target_index == 0
    assert step.guard.target_attackable_is is True
    assert step.guard.action_ready == WHIRLWIND

    # Teacher-only coordinates and current HP/time never enter the full policy
    # wire.  They may remain in the upstream teacher evidence, but the remote
    # runtime receives only this closed residual payload.
    wire_text = json.dumps(proposal["policy_wire"], sort_keys=True)
    assert "decision_index" not in wire_text
    assert "time_ms" not in wire_text
    assert "target_health" not in wire_text
    assert "seed" not in wire_text


def test_only_predeclared_proposal_seeds_contribute() -> None:
    bundle = _distill(
        [
            _result(11, [_branch(decision_index=1, candidate_action=WHIRLWIND, delta=4)]),
            _result(99, [_branch(decision_index=2, candidate_action=BLOODTHIRST, delta=9_999)]),
        ],
        proposal_seeds=(11,),
    )

    assert bundle["ignored_nonproposal_teacher_result_count"] == 1
    assert bundle["semantic_cell_count"] == 1
    assert bundle["candidates"][1]["policy_wire"]["steps"][0]["decision"][
        "terminal"
    ]["gcd_action"] == WHIRLWIND.to_wire()


def test_incomplete_comparison_and_recklessness_are_not_distilled() -> None:
    bundle = _distill(
        [
            _result(
                11,
                [
                    _branch(
                        decision_index=1,
                        candidate_action=RECKLESSNESS,
                        delta=50_000,
                    ),
                    _branch(
                        decision_index=2,
                        candidate_action=WHIRLWIND,
                        delta=1,
                        complete=False,
                    ),
                ],
            ),
            _result(12, [], complete=False),
        ]
    )

    assert bundle["status"] == "EXACT_CAT_ONLY_NO_ELIGIBLE_PROPOSAL_CELL"
    assert len(bundle["candidates"]) == 1
    assert bundle["candidates"][0]["policy_wire"]["steps"] == []
    assert bundle["rejected_label_counts"] == {
        "COMPARISON_INCOMPLETE": 1,
        "MECHANICS_EXCLUDED_ACTION_1719": 1,
    }
    assert bundle["rejected_result_counts"] == {
        "BASELINE_OR_TEACHER_INCOMPLETE": 1,
    }


def test_maximum_is_nonzero_candidate_bound_and_ranking_uses_seed_means() -> None:
    # WW is repeated twice for seed 11; within-seed averaging prevents that
    # seed from receiving twice the weight of seed 12.
    bundle = _distill(
        [
            _result(
                11,
                [
                    _branch(decision_index=1, candidate_action=WHIRLWIND, delta=100),
                    _branch(decision_index=2, candidate_action=WHIRLWIND, delta=-100),
                    _branch(decision_index=3, candidate_action=BLOODTHIRST, delta=20),
                ],
            ),
            _result(
                12,
                [
                    _branch(decision_index=4, candidate_action=WHIRLWIND, delta=10),
                    _branch(decision_index=5, candidate_action=BLOODTHIRST, delta=20),
                ],
            ),
        ],
        max_nonzero_candidates=1,
    )

    assert len(bundle["candidates"]) == 2
    selected = bundle["candidates"][1]
    assert selected["proposal_cell"]["action_ready"] == BLOODTHIRST.to_wire()
    assert selected["proposal_cell"]["mean_paired_effective_damage_delta"] == 20


def test_same_mutation_in_different_wave_strata_freezes_separate_steps() -> None:
    bundle = _distill(
        [
            _result(
                11,
                [
                    _branch(decision_index=1, candidate_action=WHIRLWIND, delta=9),
                    _branch(
                        decision_index=2,
                        candidate_action=WHIRLWIND,
                        delta=8,
                        wave=2,
                        live_count=1,
                    ),
                ],
            )
        ]
    )

    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    nonzero = [policy for policy in policies.values() if policy.steps]
    assert len(nonzero) == 2
    assert {policy.steps[0].wave_id for policy in nonzero} == {"wave-1", "wave-2"}


def test_target_only_wait_uses_ready_source_cat_gcd_as_guard() -> None:
    branch = _branch(
        decision_index=7,
        candidate_action=BATTLE_SHOUT,
        delta=12,
    )
    branch["candidate_decision"] = ProgramDecisionV1(
        target_index=1,
        start_attack=True,
        wait_ms=1,
    ).to_dict()
    bundle = _distill([_result(11, [branch])])

    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    step = policies["v8-test::proposal-001"].steps[0]
    assert step.expected_cat_gcd_action == BATTLE_SHOUT
    assert step.guard.action_ready == BATTLE_SHOUT
    assert step.decision.target_index == 1
    assert step.decision.gcd_action is None
    assert step.decision.wait_ms == 1


def test_loader_rejects_missing_zero_or_non_single_step_wire() -> None:
    bundle = _distill(
        [_result(11, [_branch(decision_index=1, candidate_action=WHIRLWIND, delta=9)])]
    )

    no_zero = deepcopy(bundle)
    no_zero["candidates"] = no_zero["candidates"][1:]
    with pytest.raises(UpperKaraCatActionPlanDistillationV8Error, match="first candidate"):
        load_upper_kara_cat_action_plan_distillation_v8(no_zero)

    bad_guard = deepcopy(bundle)
    predicates = bad_guard["candidates"][1]["policy_wire"]["steps"][0]["guard"][
        "all_of"
    ]
    predicates["pull_relative_time_gte_ms"] = 10
    with pytest.raises(
        UpperKaraCatActionPlanDistillationV8Error,
        match="exceeds live-count/readiness/nonprecombat",
    ):
        load_upper_kara_cat_action_plan_distillation_v8(bad_guard)


def test_precombat_label_cannot_create_a_candidate() -> None:
    branch = _branch(decision_index=1, candidate_action=WHIRLWIND, delta=9)
    branch["policy_observation"]["precombat"]["active"] = True
    bundle = _distill([_result(11, [branch])])

    assert bundle["selected_nonzero_candidate_count"] == 0
    assert bundle["rejected_label_counts"] == {
        "PRECOMBAT_OR_TARGET_NOT_ATTACKABLE": 1
    }
