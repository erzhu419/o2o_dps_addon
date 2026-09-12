"""Rank-preserving calibration for the frozen historical Fury V2 policy.

V5 changes confidence only.  It applies one positive inverse temperature to
the V2 action distribution, fitted from component-level inner out-of-fold
predictions inside each outer training fold.  Positive temperature scaling is
strictly monotone in every action probability, so the V2 action ordering and
therefore top-1/top-3 decisions are unchanged.

The final published V2 model is not, by itself, sufficient for leakage-free
calibration: it does not retain component-separated decision cells.  The V4
Arm-A worker aggregates do retain exactly those sufficient statistics and are
the intended input to this module; no Stage5 partition needs to be reopened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v4 as policy_v4


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy_v5_rank_calibration/v1"
PLAN_SCHEMA = "chronicle_external_historical_fury_policy_v5_rank_calibration_plan/v1"
NEGATIVE_RESULT_SCHEMA = (
    "chronicle_external_historical_fury_policy_v4_negative_result/v1"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = (
    PROJECT_ROOT
    / "configs"
    / "evaluation"
    / "chronicle_external_historical_fury_policy_v5_rank_calibration.json"
)
DEFAULT_V4_NEGATIVE_RESULT = (
    PROJECT_ROOT
    / "configs"
    / "evaluation"
    / "chronicle_external_historical_fury_policy_v4_negative_result.json"
)

DEFAULT_BETA_LOWER = 0.05
DEFAULT_BETA_UPPER = 20.0
DEFAULT_BISECTION_ITERATIONS = 80
ECE_BIN_COUNT = 10
FIXED_COMPONENT_COUNT = 3
FIXED_REQUESTED_FOLD_COUNT = 5
FIXED_EFFECTIVE_FOLD_COUNT = 3
FIXED_SPLIT_SEED = 20260911


class HistoricalFuryPolicyV5Error(ValueError):
    """The frozen V5 calibration contract or its sufficient statistics are invalid."""


@dataclass(frozen=True)
class LogProbabilityRow:
    """One weighted calibration target, including V2's held-out unknown bucket."""

    log_probabilities: tuple[float, ...]
    true_index: int
    weight: int


@dataclass(frozen=True)
class CalibratedDistribution:
    """A normalized distribution kept in log space to preserve tiny-probability ranks."""

    log_probabilities: Mapping[str, float]
    unknown_log_probability: float
    ranked_actions: tuple[str, ...]

    @property
    def probabilities(self) -> dict[str, float]:
        return {
            action: math.exp(log_probability)
            for action, log_probability in self.log_probabilities.items()
        }

    @property
    def unknown_probability(self) -> float:
        return math.exp(self.unknown_log_probability)


@dataclass(frozen=True)
class TemperatureFit:
    beta: float
    temperature: float
    boundary_status: str
    calibration_weight: int
    baseline_log_loss: float
    calibrated_log_loss: float

    def as_dict(self) -> JSONMap:
        return {
            "inverse_temperature_beta": self.beta,
            "temperature": self.temperature,
            "boundary_status": self.boundary_status,
            "calibration_weight": self.calibration_weight,
            "baseline_log_loss": self.baseline_log_loss,
            "calibrated_log_loss": self.calibrated_log_loss,
        }


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFuryPolicyV5Error(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise HistoricalFuryPolicyV5Error(f"{label} must be positive and finite")
    return result


def _logsumexp(values: Sequence[float]) -> float:
    if not values:
        raise HistoricalFuryPolicyV5Error("a probability vector cannot be empty")
    if any(not math.isfinite(value) for value in values):
        raise HistoricalFuryPolicyV5Error("log probabilities must be finite")
    maximum = max(values)
    return maximum + math.log(sum(math.exp(value - maximum) for value in values))


def _base_logs(
    probabilities: Mapping[str, float], unknown_probability: float
) -> tuple[dict[str, float], float]:
    unknown = _positive_finite(unknown_probability, "unknown_probability")
    logs: dict[str, float] = {}
    for action, probability in probabilities.items():
        if not isinstance(action, str) or not action:
            raise HistoricalFuryPolicyV5Error("action keys must be nonempty strings")
        logs[action] = math.log(_positive_finite(probability, f"probability[{action!r}]"))
    normalizer = _logsumexp((*logs.values(), math.log(unknown)))
    return (
        {action: value - normalizer for action, value in logs.items()},
        math.log(unknown) - normalizer,
    )


def calibrate_distribution(
    probabilities: Mapping[str, float],
    unknown_probability: float,
    *,
    beta: float,
) -> CalibratedDistribution:
    """Apply one positive inverse temperature without changing known-action rank.

    Ranking is computed from normalized log probabilities, rather than
    exponentiated floats, so even probabilities that underflow when rendered
    remain strictly ordered in the implementation.
    """

    inverse_temperature = _positive_finite(beta, "beta")
    base, base_unknown = _base_logs(probabilities, unknown_probability)
    scaled = {action: inverse_temperature * value for action, value in base.items()}
    scaled_unknown = inverse_temperature * base_unknown
    normalizer = _logsumexp((*scaled.values(), scaled_unknown))
    calibrated = {action: value - normalizer for action, value in scaled.items()}
    calibrated_unknown = scaled_unknown - normalizer
    before = tuple(sorted(base, key=lambda key: (-base[key], key)))
    after = tuple(
        sorted(calibrated, key=lambda key: (-calibrated[key], key))
    )
    if before != after:
        raise HistoricalFuryPolicyV5Error(
            "positive temperature unexpectedly changed the known-action ranking"
        )
    return CalibratedDistribution(
        log_probabilities=calibrated,
        unknown_log_probability=calibrated_unknown,
        ranked_actions=after,
    )


def _validate_rows(rows: Sequence[LogProbabilityRow]) -> int:
    total_weight = 0
    for row in rows:
        if not row.log_probabilities:
            raise HistoricalFuryPolicyV5Error(
                "calibration rows must contain at least one outcome"
            )
        if any(not math.isfinite(value) for value in row.log_probabilities):
            raise HistoricalFuryPolicyV5Error(
                "calibration row log probabilities must be finite"
            )
        if (
            isinstance(row.true_index, bool)
            or not isinstance(row.true_index, int)
            or not 0 <= row.true_index < len(row.log_probabilities)
        ):
            raise HistoricalFuryPolicyV5Error("calibration true_index is invalid")
        if isinstance(row.weight, bool) or not isinstance(row.weight, int) or row.weight <= 0:
            raise HistoricalFuryPolicyV5Error(
                "calibration row weight must be a positive integer"
            )
        total_weight += row.weight
    if total_weight <= 0:
        raise HistoricalFuryPolicyV5Error("at least one calibration row is required")
    return total_weight


def _mean_negative_log_likelihood(
    rows: Sequence[LogProbabilityRow], beta: float, total_weight: int
) -> float:
    loss = 0.0
    for row in rows:
        scaled = tuple(beta * value for value in row.log_probabilities)
        log_normalizer = _logsumexp(scaled)
        loss += row.weight * (
            log_normalizer - scaled[row.true_index]
        )
    return loss / total_weight


def _nll_derivative(rows: Sequence[LogProbabilityRow], beta: float) -> float:
    derivative = 0.0
    for row in rows:
        scaled = tuple(beta * value for value in row.log_probabilities)
        log_normalizer = _logsumexp(scaled)
        expectation = sum(
            math.exp(value - log_normalizer) * base
            for value, base in zip(scaled, row.log_probabilities, strict=True)
        )
        derivative += row.weight * (
            expectation - row.log_probabilities[row.true_index]
        )
    return derivative


def fit_inverse_temperature(
    rows: Sequence[LogProbabilityRow],
    *,
    lower: float = DEFAULT_BETA_LOWER,
    upper: float = DEFAULT_BETA_UPPER,
    iterations: int = DEFAULT_BISECTION_ITERATIONS,
) -> TemperatureFit:
    """Fit the sole preregistered parameter by convex derivative bisection."""

    low = _positive_finite(lower, "lower beta bound")
    high = _positive_finite(upper, "upper beta bound")
    if low >= high:
        raise HistoricalFuryPolicyV5Error("lower beta bound must be below upper")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
        raise HistoricalFuryPolicyV5Error("iterations must be a positive integer")
    total_weight = _validate_rows(rows)
    derivative_at_one = _nll_derivative(rows, 1.0)
    derivative_scale = max(1.0, float(total_weight))
    if low <= 1.0 <= high and abs(derivative_at_one) <= 1e-14 * derivative_scale:
        beta = 1.0
        boundary = "INTERIOR_BASELINE_OPTIMUM"
    elif _nll_derivative(rows, low) >= 0:
        beta = low
        boundary = "LOWER_BOUND"
    elif _nll_derivative(rows, high) <= 0:
        beta = high
        boundary = "UPPER_BOUND"
    else:
        for _ in range(iterations):
            midpoint = (low + high) / 2.0
            if _nll_derivative(rows, midpoint) > 0:
                high = midpoint
            else:
                low = midpoint
        beta = (low + high) / 2.0
        boundary = "INTERIOR"
    return TemperatureFit(
        beta=beta,
        temperature=1.0 / beta,
        boundary_status=boundary,
        calibration_weight=total_weight,
        baseline_log_loss=_mean_negative_log_likelihood(rows, 1.0, total_weight),
        calibrated_log_loss=_mean_negative_log_likelihood(rows, beta, total_weight),
    )


def _merged(
    values: Sequence[policy_v4.AdditiveAggregate],
) -> policy_v4.AdditiveAggregate:
    result = policy_v4.AdditiveAggregate()
    for value in values:
        result.merge(value)
    return result


def _calibration_rows(
    train: policy_v4.AdditiveAggregate,
    held_out: policy_v4.AdditiveAggregate,
    *,
    alpha: float,
    backoff_strength: float,
) -> list[LogProbabilityRow]:
    catalog = sorted(train.action_counts)
    action_index = {action: index for index, action in enumerate(catalog)}
    result: list[LogProbabilityRow] = []
    for (context_pairs, action), count in sorted(held_out.decision_cells.items()):
        atoms = tuple(policy_v4.FeatureAtom(*pair) for pair in context_pairs)
        probabilities, _matched, unknown = policy_v4.distribution_from_atoms(
            train,
            atoms,
            alpha=alpha,
            strength=backoff_strength,
        )
        base, base_unknown = _base_logs(probabilities, unknown)
        logs = tuple(base[key] for key in catalog) + (base_unknown,)
        result.append(
            LogProbabilityRow(
                log_probabilities=logs,
                true_index=action_index.get(action, len(catalog)),
                weight=count,
            )
        )
    return result


def _empty_metrics() -> JSONMap:
    return {
        "heldout_decision_count": 0,
        "known_action_count": 0,
        "known_action_coverage": None,
        "top1_accuracy": None,
        "top3_accuracy": None,
        "contextual_log_loss": None,
        "expected_calibration_error": None,
    }


def _summarize_metrics(
    *,
    total: int,
    known: int,
    top1: int,
    top3: int,
    loss: float,
    calibration_rows: Sequence[tuple[float, bool, int]],
) -> JSONMap:
    if total == 0:
        return _empty_metrics()
    ece = 0.0
    for bin_index in range(ECE_BIN_COUNT):
        low = bin_index / ECE_BIN_COUNT
        high = (bin_index + 1) / ECE_BIN_COUNT
        rows = [
            row
            for row in calibration_rows
            if row[0] >= low
            and (row[0] < high or bin_index == ECE_BIN_COUNT - 1)
        ]
        weight = sum(row[2] for row in rows)
        if weight:
            confidence = sum(row[0] * row[2] for row in rows) / weight
            accuracy = sum(bool(row[1]) * row[2] for row in rows) / weight
            ece += (weight / total) * abs(confidence - accuracy)
    return {
        "heldout_decision_count": total,
        "known_action_count": known,
        "known_action_coverage": known / total,
        "top1_accuracy": top1 / total,
        "top3_accuracy": top3 / total,
        "contextual_log_loss": loss / total,
        "expected_calibration_error": ece,
    }


def evaluate_rank_calibration(
    components: Mapping[str, policy_v4.AdditiveAggregate],
    *,
    fold_count: int,
    split_seed: int,
    alpha: float = policy_v2.DEFAULT_SMOOTHING_ALPHA,
    backoff_strength: float = policy_v2.DEFAULT_BACKOFF_STRENGTH,
    beta_lower: float = DEFAULT_BETA_LOWER,
    beta_upper: float = DEFAULT_BETA_UPPER,
    bisection_iterations: int = DEFAULT_BISECTION_ITERATIONS,
) -> JSONMap:
    """Evaluate V2 and calibrated V2 on identical outer component folds."""

    ids = sorted(components)
    if len(ids) != FIXED_COMPONENT_COUNT:
        raise HistoricalFuryPolicyV5Error(
            f"V5 is frozen to {FIXED_COMPONENT_COUNT} independent components"
        )
    if fold_count != FIXED_REQUESTED_FOLD_COUNT or split_seed != FIXED_SPLIT_SEED:
        raise HistoricalFuryPolicyV5Error(
            "V5 fold_count or split_seed differs from the preregistration"
        )
    if alpha != policy_v2.DEFAULT_SMOOTHING_ALPHA:
        raise HistoricalFuryPolicyV5Error("V5 smoothing alpha is frozen at 0.5")
    if backoff_strength != policy_v2.DEFAULT_BACKOFF_STRENGTH:
        raise HistoricalFuryPolicyV5Error("V5 backoff strength is frozen at 8.0")
    if (
        beta_lower != DEFAULT_BETA_LOWER
        or beta_upper != DEFAULT_BETA_UPPER
        or bisection_iterations != DEFAULT_BISECTION_ITERATIONS
    ):
        raise HistoricalFuryPolicyV5Error(
            "V5 temperature bounds and iteration budget are frozen"
        )
    effective, assignment = policy_v2._fold_assignment(
        ids, fold_count=fold_count, split_seed=split_seed
    )
    if effective < 3:
        raise HistoricalFuryPolicyV5Error(
            "rank calibration requires at least three independent components"
        )
    total = known = top1 = top3 = 0
    base_loss = calibrated_loss = 0.0
    base_calibration: list[tuple[float, bool, int]] = []
    calibrated_calibration: list[tuple[float, bool, int]] = []
    folds: list[JSONMap] = []
    for fold_index in range(effective):
        outer_test_ids = [key for key in ids if assignment[key] == fold_index]
        outer_train_ids = [key for key in ids if assignment[key] != fold_index]
        if len(outer_train_ids) < 2:
            raise HistoricalFuryPolicyV5Error(
                "each outer training fold needs two components for inner OOF fitting"
            )
        inner_rows: list[LogProbabilityRow] = []
        inner_fits: list[JSONMap] = []
        for inner_test_id in outer_train_ids:
            inner_train_ids = [
                key for key in outer_train_ids if key != inner_test_id
            ]
            inner_train = _merged([components[key] for key in inner_train_ids])
            rows = _calibration_rows(
                inner_train,
                components[inner_test_id],
                alpha=alpha,
                backoff_strength=backoff_strength,
            )
            inner_rows.extend(rows)
            inner_fits.append(
                {
                    "inner_train_component_ids": inner_train_ids,
                    "inner_test_component_id": inner_test_id,
                    "outer_test_overlap_count": len(
                        set(inner_train_ids + [inner_test_id]) & set(outer_test_ids)
                    ),
                    "calibration_weight": sum(row.weight for row in rows),
                }
            )
        fit = fit_inverse_temperature(
            inner_rows,
            lower=beta_lower,
            upper=beta_upper,
            iterations=bisection_iterations,
        )
        outer_train = _merged([components[key] for key in outer_train_ids])
        outer_test = _merged([components[key] for key in outer_test_ids])
        fold_total = 0
        for (context_pairs, action), count in sorted(outer_test.decision_cells.items()):
            atoms = tuple(policy_v4.FeatureAtom(*pair) for pair in context_pairs)
            probabilities, _matched, unknown_probability = (
                policy_v4.distribution_from_atoms(
                    outer_train,
                    atoms,
                    alpha=alpha,
                    strength=backoff_strength,
                )
            )
            base_logs, _base_unknown_log = _base_logs(
                probabilities, unknown_probability
            )
            base_ranked = tuple(
                sorted(base_logs, key=lambda key: (-base_logs[key], key))
            )
            calibrated = calibrate_distribution(
                probabilities, unknown_probability, beta=fit.beta
            )
            if calibrated.ranked_actions != base_ranked:
                raise HistoricalFuryPolicyV5Error(
                    "calibration changed an outer-test known-action ranking"
                )
            is_known = action in base_logs
            predicted = (
                base_ranked[0] if base_ranked else policy_v2.UNKNOWN_ACTION_KEY
            )
            correct = predicted == action
            in_top3 = action in base_ranked[:3]
            base_probability = probabilities.get(action, unknown_probability)
            base_true_log = math.log(base_probability)
            calibrated_true_log = calibrated.log_probabilities.get(
                action, calibrated.unknown_log_probability
            )
            base_confidence = (
                probabilities[predicted]
                if predicted in probabilities
                else unknown_probability
            )
            calibrated_confidence = (
                math.exp(calibrated.log_probabilities[predicted])
                if predicted in calibrated.log_probabilities
                else calibrated.unknown_probability
            )
            total += count
            fold_total += count
            known += count if is_known else 0
            top1 += count if correct else 0
            top3 += count if in_top3 else 0
            base_loss -= count * base_true_log
            calibrated_loss -= count * calibrated_true_log
            base_calibration.append((base_confidence, correct, count))
            calibrated_calibration.append(
                (calibrated_confidence, correct, count)
            )
        folds.append(
            {
                "fold_index": fold_index,
                "outer_train_component_ids": outer_train_ids,
                "outer_test_component_ids": outer_test_ids,
                "outer_train_test_overlap_count": 0,
                "heldout_decision_count": fold_total,
                "inner_component_fits": inner_fits,
                "temperature_fit": fit.as_dict(),
            }
        )
    base_metrics = _summarize_metrics(
        total=total,
        known=known,
        top1=top1,
        top3=top3,
        loss=base_loss,
        calibration_rows=base_calibration,
    )
    calibrated_metrics = _summarize_metrics(
        total=total,
        known=known,
        top1=top1,
        top3=top3,
        loss=calibrated_loss,
        calibration_rows=calibrated_calibration,
    )
    top1_equal = (
        base_metrics["top1_accuracy"] == calibrated_metrics["top1_accuracy"]
    )
    top3_equal = (
        base_metrics["top3_accuracy"] == calibrated_metrics["top3_accuracy"]
    )
    log_loss_improved = (
        calibrated_metrics["contextual_log_loss"]
        < base_metrics["contextual_log_loss"]
    )
    ece_improved = (
        calibrated_metrics["expected_calibration_error"]
        < base_metrics["expected_calibration_error"]
    )
    return {
        "schema": SCHEMA + "/paired_evaluation",
        "component_count": len(ids),
        "effective_fold_count": effective,
        "component_to_fold": [
            {"component_id": key, "fold_index": assignment[key]}
            for key in sorted(assignment)
        ],
        "same_labels_components_and_folds": True,
        "outer_test_used_for_temperature_fit": False,
        "rank_preserving_by_construction": True,
        "folds": folds,
        "baseline_v2": base_metrics,
        "calibrated_v5": calibrated_metrics,
        "gate": {
            "top1_exactly_preserved": top1_equal,
            "top3_exactly_preserved": top3_equal,
            "contextual_log_loss_strictly_improved": log_loss_improved,
            "expected_calibration_error_strictly_improved": ece_improved,
            "joint_calibration_improvement": (
                top1_equal and top3_equal and log_loss_improved and ece_improved
            ),
            "runner_or_deployment_authorized": False,
        },
    }


def load_preregistered_plan(path: Path = DEFAULT_PLAN) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalFuryPolicyV5Error(
            f"cannot read V5 preregistration {path}: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("schema") != PLAN_SCHEMA:
        raise HistoricalFuryPolicyV5Error("unexpected V5 preregistration schema")
    if value.get("status") != "PREREGISTERED_NOT_EXECUTED":
        raise HistoricalFuryPolicyV5Error("V5 plan must remain unexecuted")
    calibrator = value.get("calibrator")
    budget = value.get("budget")
    if not isinstance(calibrator, dict) or not isinstance(budget, dict):
        raise HistoricalFuryPolicyV5Error("V5 calibrator or budget is missing")
    if calibrator.get("parameter_count") != 1:
        raise HistoricalFuryPolicyV5Error("V5 must have exactly one parameter")
    if (
        calibrator.get("grid_search") is not False
        or calibrator.get("model_selection") is not False
    ):
        raise HistoricalFuryPolicyV5Error("V5 forbids grid or model selection")
    if (
        calibrator.get("fixed_lower_bound") != DEFAULT_BETA_LOWER
        or calibrator.get("fixed_upper_bound") != DEFAULT_BETA_UPPER
        or calibrator.get("bisection_iterations")
        != DEFAULT_BISECTION_ITERATIONS
    ):
        raise HistoricalFuryPolicyV5Error(
            "V5 temperature optimizer differs from the preregistration"
        )
    population = value.get("population")
    baseline = value.get("baseline")
    source = value.get("source")
    if (
        not isinstance(population, dict)
        or population.get("strict_controllable_labels") != 182481
        or population.get("outer_component_count") != FIXED_COMPONENT_COUNT
        or population.get("requested_fold_count") != FIXED_REQUESTED_FOLD_COUNT
        or population.get("effective_fold_count") != FIXED_EFFECTIVE_FOLD_COUNT
    ):
        raise HistoricalFuryPolicyV5Error("V5 population is not the frozen V4 universe")
    if (
        not isinstance(baseline, dict)
        or baseline.get("smoothing_alpha") != policy_v2.DEFAULT_SMOOTHING_ALPHA
        or baseline.get("backoff_strength") != policy_v2.DEFAULT_BACKOFF_STRENGTH
    ):
        raise HistoricalFuryPolicyV5Error("V5 learner differs from frozen V2 Arm A")
    if (
        not isinstance(source, dict)
        or source.get("stage5_partitions_reopened") is not False
        or source.get("final_v2_model_alone_is_sufficient") is not False
    ):
        raise HistoricalFuryPolicyV5Error("V5 sufficient-statistic source is inconsistent")
    if budget.get("execution_authorized_now") is not False:
        raise HistoricalFuryPolicyV5Error("V5 execution is not authorized")
    if budget.get("hyperparameter_trials") != 1:
        raise HistoricalFuryPolicyV5Error("V5 budget permits one fixed fit only")
    return value


def load_v4_negative_result(path: Path = DEFAULT_V4_NEGATIVE_RESULT) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalFuryPolicyV5Error(
            f"cannot read V4 negative-result registry {path}: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("schema") != NEGATIVE_RESULT_SCHEMA:
        raise HistoricalFuryPolicyV5Error("unexpected V4 negative-result schema")
    if value.get("status") != "RETAIN_NEGATIVE_NO_NEXT_HPC":
        raise HistoricalFuryPolicyV5Error("V4 negative result was not retained")
    decision = value.get("decision")
    if (
        not isinstance(decision, dict)
        or decision.get("low_cardinality_replacement_adopted") is not False
    ):
        raise HistoricalFuryPolicyV5Error("V4 replacement decision is inconsistent")
    return value


def preregistration_summary() -> JSONMap:
    """Return the fixed local contract without opening data or launching work."""

    plan = load_preregistered_plan()
    negative = load_v4_negative_result()
    source = plan["source"]
    if source.get("v4_plan_id") != negative.get("plan_id"):
        raise HistoricalFuryPolicyV5Error(
            "V5 source plan does not match the retained V4 result"
        )
    return {
        "schema": SCHEMA + "/preregistration_summary",
        "status": plan["status"],
        "v4_negative_result_retained": True,
        "rank_preserving": True,
        "parameter_count": 1,
        "execution_authorized_now": False,
        "final_v2_model_alone_is_sufficient": False,
        "component_separated_v4_arm_a_aggregates_are_sufficient": True,
        "stage5_partitions_required": False,
        "budget": plan["budget"],
    }
