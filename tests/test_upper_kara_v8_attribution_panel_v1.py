from __future__ import annotations

import pytest

from o2o_dps.causal_action_program_v1 import (
    ProgramDecisionV1,
    ProgramPrefixOperationKindV1,
)
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    SEED_DERIVATION_ALGORITHM,
    derive_simulator_seed,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_v8_attribution_panel_v1 import (
    A0_EXACT_CAT,
    A1_DEATH_WISH_ONLY,
    A2_CLEAVE_ONLY,
    A3_FROZEN_V8,
    ARM_IDS,
    CLEAVE,
    DEATH_WISH,
    compose_v8_attribution_decision_v1,
    evaluate_v8_attribution_seed_v1,
    paired_interaction_v1,
    summarize_v8_attribution_rows_v1,
    v8_attribution_contract_from_dict_v1,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


BATTLE_SHOUT = ActionRef(spell_id=25_289)
HEROIC_STRIKE = ActionRef(spell_id=25_286, tag=1)


def _frozen_v8() -> ProgramDecisionV1:
    return ProgramDecisionV1(
        queue_op=QueueLaneOp.SET,
        queue_action=CLEAVE,
        gcd_action=DEATH_WISH,
    )


@pytest.mark.parametrize("source_queue", (QueueLaneOp.SET, QueueLaneOp.CANCEL))
def test_a1_replaces_only_gcd_and_keeps_nontrivial_cat_queue(source_queue) -> None:
    queue_action = HEROIC_STRIKE if source_queue is QueueLaneOp.SET else None
    cat = ProgramDecisionV1(
        target_index=1,
        start_attack=True,
        queue_op=source_queue,
        queue_action=queue_action,
        gcd_action=BATTLE_SHOUT,
    )

    arm = compose_v8_attribution_decision_v1(
        A1_DEATH_WISH_ONLY, cat, _frozen_v8()
    )

    assert arm.gcd_action == DEATH_WISH
    assert arm.queue_op is source_queue
    assert arm.queue_action == queue_action
    assert arm.target_index == cat.target_index
    assert arm.start_attack == cat.start_attack
    assert arm.prefix_order == cat.prefix_order


def test_a2_replaces_only_queue_and_keeps_cat_terminal_and_prefix_order() -> None:
    cat = ProgramDecisionV1(
        target_index=1,
        start_attack=True,
        queue_op=QueueLaneOp.CANCEL,
        gcd_action=BATTLE_SHOUT,
        prefix_order=(
            ProgramPrefixOperationKindV1.START_ATTACK,
            ProgramPrefixOperationKindV1.QUEUE_CANCEL,
            ProgramPrefixOperationKindV1.SET_TARGET,
        ),
    )

    arm = compose_v8_attribution_decision_v1(
        A2_CLEAVE_ONLY, cat, _frozen_v8()
    )

    assert arm.gcd_action == BATTLE_SHOUT
    assert arm.queue_op is QueueLaneOp.SET
    assert arm.queue_action == CLEAVE
    assert arm.target_index == cat.target_index
    assert arm.start_attack == cat.start_attack
    assert arm.prefix_order == (
        ProgramPrefixOperationKindV1.START_ATTACK,
        ProgramPrefixOperationKindV1.QUEUE_SET,
        ProgramPrefixOperationKindV1.SET_TARGET,
    )


def test_a0_is_exact_object_and_a3_is_frozen_exact_object() -> None:
    cat = ProgramDecisionV1(gcd_action=BATTLE_SHOUT)
    frozen = _frozen_v8()

    assert compose_v8_attribution_decision_v1(A0_EXACT_CAT, cat, frozen) is cat
    assert compose_v8_attribution_decision_v1(A3_FROZEN_V8, cat, frozen) is frozen


def test_rejects_a_static_body_that_is_not_the_frozen_v8_joint_mutation() -> None:
    cat = ProgramDecisionV1(gcd_action=BATTLE_SHOUT)
    wrong = ProgramDecisionV1(gcd_action=DEATH_WISH)

    with pytest.raises(ValueError, match="tagged Cleave"):
        compose_v8_attribution_decision_v1(A1_DEATH_WISH_ONLY, cat, wrong)


def test_paired_interaction_uses_within_seed_arm_values() -> None:
    assert paired_interaction_v1(a0=10, a1=14, a2=13, a3=20) == 3.0


def test_summary_uses_complete_paired_blocks_and_reports_interaction() -> None:
    rows = []
    for seed, values in ((11, (10, 14, 13, 20)), (12, (20, 22, 25, 30))):
        rows.append(
            {
                "seed": seed,
                "first_wave_arrival_ms": 0,
                "arms": {
                    arm_id: {
                        "status": "COMPLETED",
                        "own_effective_damage": damage,
                        "elapsed_ms": 1_000 + offset,
                        "intervention_executed": arm_id != A0_EXACT_CAT,
                    }
                    for offset, (arm_id, damage) in enumerate(
                        zip(
                            (
                                A0_EXACT_CAT,
                                A1_DEATH_WISH_ONLY,
                                A2_CLEAVE_ONLY,
                                A3_FROZEN_V8,
                            ),
                            values,
                            strict=True,
                        )
                    )
                },
            }
        )

    summary = summarize_v8_attribution_rows_v1(rows)

    assert summary["status"] == "COMPLETE"
    assert summary["arms"][A3_FROZEN_V8]["mean_own_effective_damage"] == 25.0
    assert summary["arms"][A3_FROZEN_V8][
        "paired_own_effective_damage_minus_a0"
    ]["mean"] == 10.0
    assert summary[
        "paired_interaction_a3_minus_a1_minus_a2_plus_a0"
    ]["mean"] == 3.0


def test_summary_refuses_to_turn_an_incomplete_lane_into_zero() -> None:
    arms = {
        arm_id: {
            "status": "COMPLETED",
            "own_effective_damage": 1.0,
            "elapsed_ms": 1.0,
            "intervention_executed": False,
        }
        for arm_id in (
            A0_EXACT_CAT,
            A1_DEATH_WISH_ONLY,
            A2_CLEAVE_ONLY,
            A3_FROZEN_V8,
        )
    }
    arms[A2_CLEAVE_ONLY]["status"] = "INVALID"

    with pytest.raises(ValueError, match="not a complete lane"):
        summarize_v8_attribution_rows_v1(
            [{"seed": 1, "first_wave_arrival_ms": 0, "arms": arms}]
        )


def test_frozen_contract_roundtrip_and_seed_schedule() -> None:
    import json
    from pathlib import Path

    path = (
        Path(__file__).parents[1]
        / "configs"
        / "evaluation"
        / "upper_kara_v8_attribution_fresh_v1.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    contract = v8_attribution_contract_from_dict_v1(raw)

    assert contract.to_dict() == raw
    assert contract.examples()[:7] == (
        (1_620_001, 0),
        (1_620_002, 1_000),
        (1_620_003, 3_000),
        (1_620_004, 5_000),
        (1_620_005, 7_000),
        (1_620_006, 9_000),
        (1_620_007, 0),
    )
    assert tuple(row["arm_id"] for row in raw["arms"]) == ARM_IDS


def test_seed_evaluator_dynamically_composes_three_arms_and_checks_cat_identity() -> None:
    from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
    from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
        CatResidualSequenceStepV1,
        CatResidualSequenceWaveV1,
        DevelopmentTwoWaveCatResidualSequenceV1,
    )

    policy = DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id="frozen-v8",
        exact_build_id="live_bonereaver",
        waves=(
            CatResidualSequenceWaveV1("multi_two", (0, 1)),
            CatResidualSequenceWaveV1("single_long", (2,)),
        ),
        steps=(
            CatResidualSequenceStepV1(
                "step",
                "multi_two",
                ObservableCausalGuardV1(
                    action_ready=DEATH_WISH,
                    false_semantics=SKIP_PLAN,
                ),
                _frozen_v8(),
                BATTLE_SHOUT,
            ),
        ),
    )
    seen = []

    def fake_pair(policy, *, selected_decision_transform, **kwargs):
        del policy
        cat = ProgramDecisionV1(
            queue_op=QueueLaneOp.SET,
            queue_action=HEROIC_STRIKE,
            gcd_action=BATTLE_SHOUT,
        )
        selected = (
            _frozen_v8()
            if selected_decision_transform is None
            else selected_decision_transform(cat, _frozen_v8())
        )
        seen.append(selected)
        damage = {
            (DEATH_WISH, HEROIC_STRIKE): 12.0,
            (BATTLE_SHOUT, CLEAVE): 13.0,
            (DEATH_WISH, CLEAVE): 16.0,
        }[(selected.gcd_action, selected.queue_action)]
        return {
            "seed": kwargs["seed"],
            "master_seed": kwargs["seed"],
            "simulator_seed": derive_simulator_seed(
                kwargs["seed"], "a" * 64, namespace=PROTOCOL_ID
            ),
            "request_sha256": "a" * 64,
            "simulator_seed_namespace": kwargs["simulator_seed_namespace"],
            "simulator_seed_derivation_algorithm": SEED_DERIVATION_ALGORITHM,
            "exact_cat_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": 10.0,
                "elapsed_ms": 1_000,
            },
            "residual_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": damage,
                "elapsed_ms": 900,
            },
            "paired_residual_minus_cat_own_effective_damage": damage - 10.0,
            "paired_comparison_valid": True,
            "step_audit": {
                "executed_step_keys": [["multi_two", "step"]],
                "runtime_events": [
                    {
                        "kind": "CAT_RELATIVE_RESIDUAL_EXECUTION_CONFIRMED",
                        "state_time_ms": 123,
                    }
                ],
            },
        }

    result = evaluate_v8_attribution_seed_v1(
        policy,
        seed=1,
        build_id="live_bonereaver",
        loadout_id="rage",
        first_wave_arrival_ms=0,
        arm_workers=1,
        simulator_seed_namespace=PROTOCOL_ID,
        paired_evaluator=fake_pair,
    )

    assert result["status"] == "COMPLETED_ATTRIBUTION_BLOCK"
    assert result["arms"][A0_EXACT_CAT]["own_effective_damage"] == 10.0
    assert result["arms"][A1_DEATH_WISH_ONLY]["own_effective_damage"] == 12.0
    assert result["arms"][A2_CLEAVE_ONLY]["own_effective_damage"] == 13.0
    assert result["arms"][A3_FROZEN_V8]["own_effective_damage"] == 16.0
    assert result["arms"][A1_DEATH_WISH_ONLY]["intervention_time_ms"] == 123
    assert seen[0].queue_action == HEROIC_STRIKE
    assert result["master_seed"] == 1
    assert result["simulator_seed"] == derive_simulator_seed(
        1, "a" * 64, namespace=PROTOCOL_ID
    )
    assert result["request_sha256"] == "a" * 64


@pytest.mark.parametrize(
    ("execution_mode", "expected_period", "expected_phase"),
    (
        ("E0_EVENT_DRIVEN", None, 0),
        ("E1_EXTERNAL_PRESS_CLOCK", 125, 25),
    ),
)
def test_seed_evaluator_wires_the_frozen_execution_mode(
    execution_mode, expected_period, expected_phase
) -> None:
    from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
    from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
        CatResidualSequenceStepV1,
        CatResidualSequenceWaveV1,
        DevelopmentTwoWaveCatResidualSequenceV1,
    )

    policy = DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id="frozen-v8",
        exact_build_id="live_bonereaver",
        waves=(
            CatResidualSequenceWaveV1("multi_two", (0, 1)),
            CatResidualSequenceWaveV1("single_long", (2,)),
        ),
        steps=(
            CatResidualSequenceStepV1(
                "step",
                "multi_two",
                ObservableCausalGuardV1(
                    action_ready=DEATH_WISH,
                    false_semantics=SKIP_PLAN,
                ),
                _frozen_v8(),
                BATTLE_SHOUT,
            ),
        ),
    )
    clocks = []

    def fake_pair(policy, *, selected_decision_transform, **kwargs):
        del policy, selected_decision_transform
        clocks.append(
            (
                kwargs["external_press_period_ms"],
                kwargs["external_press_phase_ms"],
            )
        )
        return {
            "exact_cat_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": 10.0,
                "elapsed_ms": 1_000,
            },
            "residual_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": 11.0,
                "elapsed_ms": 1_000,
            },
            "paired_residual_minus_cat_own_effective_damage": 1.0,
            "paired_comparison_valid": True,
            "step_audit": {
                "executed_step_keys": [["multi_two", "step"]],
                "runtime_events": [],
            },
        }

    result = evaluate_v8_attribution_seed_v1(
        policy,
        seed=1,
        build_id="live_bonereaver",
        loadout_id="rage",
        first_wave_arrival_ms=0,
        execution_mode=execution_mode,
        press_period_ms=125,
        press_phase_ms=25,
        arm_workers=1,
        paired_evaluator=fake_pair,
    )

    assert result["execution_mode"] == execution_mode
    assert clocks == [(expected_period, expected_phase)] * 3
