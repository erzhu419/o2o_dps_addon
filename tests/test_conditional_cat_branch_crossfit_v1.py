from dataclasses import asdict

from o2o_dps.conditional_cat_branch_v1 import (
    FrozenRuleV1,
    fit_conditional_cat_branch_v1,
)


def _row(seed: int, delta: float, *, rage: float = 55.0) -> dict:
    return {
        "seed": seed,
        "baseline_status": "COMPLETED",
        "status": "COMPLETE_BRANCH_SMOKE",
        "kind": "WW_TO_BT",
        "branch_action_accepted": True,
        "paired_effective_damage_delta": delta,
        "combat": {
            "rage": rage,
            "mainhand_swing_remaining_s": 1.5,
            "target_health_pct": 70.0,
            "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
            "bloodthirst_ready_in_s": 0.0,
            "whirlwind_ready_in_s": 0.0,
            "queued_swing": "KEEP",
            "flurry_active": False,
            "gcd_ready": True,
        },
    }


def test_three_fold_stable_rule_passes_and_reports_oof_seed_values() -> None:
    rows = [_row(seed, 10.0) for seed in range(1, 13)]

    model = fit_conditional_cat_branch_v1(
        rows, training_seeds=range(1, 13), min_distinct_seeds=6,
    )

    assert model["status"] == "FROZEN_RULE_FOR_FRESH_TEST"
    assert model["stability_gate_status"] == "PASSED"
    assert model["crossfit_fold_count"] == 3
    assert model["stable_selection_fold_count"] == 3
    assert model["crossfit_oof_distinct_seed_count"] == 12
    assert model["crossfit_oof_mean_paired_label_delta"] == 10.0
    assert model["crossfit_oof_lower_95_normal_label_bound"] == 10.0
    assert model["stable_rule"] == model["policy"]
    assert all(row["selected_rule"] == model["policy"] for row in model["crossfit_folds"])


def test_crossfit_aggregates_repeated_labels_once_per_seed() -> None:
    rows = []
    for seed in range(1, 10):
        rows.extend((_row(seed, 8.0), _row(seed, 12.0)))

    model = fit_conditional_cat_branch_v1(
        rows, training_seeds=range(1, 10), min_distinct_seeds=6,
    )

    assert model["stability_gate_status"] == "PASSED"
    assert model["usable_teacher_label_count"] == 18
    assert model["selected_training_cell"]["distinct_seed_count"] == 9
    assert model["crossfit_oof_distinct_seed_count"] == 9
    assert model["crossfit_oof_mean_paired_label_delta"] == 10.0


def test_positive_full_fit_abstains_when_fold_selection_is_unstable() -> None:
    rows = []
    fold_zero = {1, 4}
    fold_one = {2, 5}
    for seed in range(1, 7):
        rows.append(_row(seed, 100.0 if seed in fold_zero else 10.0, rage=55.0))
        rows.append(_row(seed, 100.0 if seed in fold_one else 10.0, rage=75.0))

    model = fit_conditional_cat_branch_v1(
        rows, training_seeds=range(1, 7), min_distinct_seeds=2,
    )

    assert model["full_fit_selected_cell"] is not None
    assert model["full_fit_selected_cell"]["eligible_for_fresh_test"] is True
    assert model["status"] == "ABSTAIN_INSUFFICIENT_CONDITIONAL_EVIDENCE"
    assert model["stability_gate_status"] == "UNSTABLE_RULE_ACROSS_FOLDS"
    assert model["policy"] == asdict(FrozenRuleV1())
    assert model["selected_training_cell"] is None
    assert len({
        tuple(sorted(row["selected_rule"].items()))
        for row in model["crossfit_folds"] if row["selected_rule"] is not None
    }) > 1


def test_fold_selection_does_not_read_its_heldout_value() -> None:
    base = [_row(seed, 10.0) for seed in range(1, 10)]
    changed = [
        _row(row["seed"], -1000.0 if row["seed"] in {1, 4, 7} else 10.0)
        for row in base
    ]

    before = fit_conditional_cat_branch_v1(
        base, training_seeds=range(1, 10), min_distinct_seeds=6,
    )
    after = fit_conditional_cat_branch_v1(
        changed, training_seeds=range(1, 10), min_distinct_seeds=6,
    )

    assert before["crossfit_folds"][0]["validation_seeds"] == [1, 4, 7]
    assert (
        before["crossfit_folds"][0]["selected_rule"]
        == after["crossfit_folds"][0]["selected_rule"]
    )
    assert (
        before["crossfit_folds"][0]["heldout_mean_paired_label_delta"]
        != after["crossfit_folds"][0]["heldout_mean_paired_label_delta"]
    )
