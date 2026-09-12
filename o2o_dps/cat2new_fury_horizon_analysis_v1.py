"""Validate and compact the frozen Cat2_new Fury horizon confirmation.

The command reuses the strict four-lane reducer, extracts only the paired
candidate-minus-Cat DPS vector, and applies the predeclared three-arm Holm
family.  Its output is a small simulator-only mechanism diagnostic; it does
not select or promote an addon policy.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
)
from .fury_multiseed_hpc_dispatch_v3 import validate_dispatch_plan_v3
from .fury_multiseed_hpc_reducer_v3 import reduce_dispatch_v3
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
SCHEMA = "cat2new_fury_horizon_analysis/v1"
MANIFEST_SCHEMA = "fury_cat2_horizon_confirmation_manifest/v1"
STATUS = "COMPLETE_SYNTHETIC_MECHANISM_DIAGNOSTIC_NONVOTING"
EXPECTED_PAIR_COUNT = len(CONFIRMATION_MASTER_SEEDS)
FAMILYWISE_ALPHA = 0.05


class Cat2NewFuryHorizonAnalysisV1Error(RuntimeError):
    """The frozen analysis contract or one complete arm is invalid."""


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewFuryHorizonAnalysisV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise Cat2NewFuryHorizonAnalysisV1Error(f"{label} must be an object")
    return value


def _read_rows(path: Path, label: str) -> list[JSONMap]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        values = [json.loads(line) for line in lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewFuryHorizonAnalysisV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not values or any(not isinstance(value, dict) for value in values):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            f"{label} must contain JSON object rows"
        )
    return values


def _linear_type7(values: Sequence[float], probability: float) -> float:
    if not values:
        raise Cat2NewFuryHorizonAnalysisV1Error("quantile vector is empty")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    maximum_iterations = 256
    epsilon = 3.0e-14
    floor = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < floor:
        d = floor
    d = 1.0 / d
    result = d
    for iteration in range(1, maximum_iterations + 1):
        even = 2 * iteration
        numerator = iteration * (b - iteration) * x / (
            (qam + even) * (a + even)
        )
        d = 1.0 + numerator * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + numerator / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        result *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / (
            (a + even) * (qap + even)
        )
        d = 1.0 + numerator * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + numerator / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) <= epsilon:
            return result
    raise Cat2NewFuryHorizonAnalysisV1Error(
        "paired t-test beta continued fraction did not converge"
    )


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if not (a > 0.0 and b > 0.0 and 0.0 <= x <= 1.0):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "invalid incomplete-beta arguments"
        )
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def _paired_t_test(values: Sequence[float]) -> tuple[float, float]:
    count = len(values)
    mean = math.fsum(values) / count
    squared = math.fsum((value - mean) ** 2 for value in values)
    if squared == 0.0:
        return (0.0, 1.0) if mean == 0.0 else (
            math.copysign(math.inf, mean),
            0.0,
        )
    sample_variance = squared / (count - 1)
    statistic = mean / math.sqrt(sample_variance / count)
    degrees_of_freedom = count - 1
    x = degrees_of_freedom / (degrees_of_freedom + statistic * statistic)
    probability = _regularized_incomplete_beta(
        degrees_of_freedom / 2.0, 0.5, x
    )
    return statistic, min(1.0, max(0.0, probability))


def _holm_adjust(raw: Mapping[str, float]) -> dict[str, JSONMap]:
    ordered = sorted(raw.items(), key=lambda row: (row[1], row[0]))
    running = 0.0
    result: dict[str, JSONMap] = {}
    family_size = len(ordered)
    for index, (arm_id, probability) in enumerate(ordered):
        adjusted = min(1.0, (family_size - index) * probability)
        running = max(running, adjusted)
        result[arm_id] = {
            "holm_rank": index + 1,
            "raw_two_sided_p": probability,
            "holm_adjusted_p": running,
            "holm_reject_familywise_0_05": running <= FAMILYWISE_ALPHA,
        }
    return result


def summarize_horizon_deltas_v1(
    deltas_by_arm: Mapping[str, Sequence[float]],
) -> JSONMap:
    """Summarize the exact frozen 3 x 256 paired DPS-delta family."""

    if set(deltas_by_arm) != set(CONFIRMATION_ARM_IDS):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "delta vectors must contain exactly the frozen three arm IDs"
        )
    values_by_arm: dict[str, list[float]] = {}
    raw_probabilities: dict[str, float] = {}
    base_rows: dict[str, JSONMap] = {}
    for arm_id in CONFIRMATION_ARM_IDS:
        raw_values = deltas_by_arm[arm_id]
        if isinstance(raw_values, (str, bytes)) or len(raw_values) != EXPECTED_PAIR_COUNT:
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"arm {arm_id} must contain exactly {EXPECTED_PAIR_COUNT} paired deltas"
            )
        values: list[float] = []
        for raw in raw_values:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise Cat2NewFuryHorizonAnalysisV1Error(
                    f"arm {arm_id} contains a nonnumeric paired delta"
                )
            value = float(raw)
            if not math.isfinite(value):
                raise Cat2NewFuryHorizonAnalysisV1Error(
                    f"arm {arm_id} contains a nonfinite paired delta"
                )
            values.append(value)
        values_by_arm[arm_id] = values
        mean = math.fsum(values) / EXPECTED_PAIR_COUNT
        sample_sd = math.sqrt(
            math.fsum((value - mean) ** 2 for value in values)
            / (EXPECTED_PAIR_COUNT - 1)
        )
        statistic, probability = _paired_t_test(values)
        raw_probabilities[arm_id] = probability
        base_rows[arm_id] = {
            "arm_id": arm_id,
            "paired_n": EXPECTED_PAIR_COUNT,
            "candidate_minus_cat_mean_dps": mean,
            "candidate_minus_cat_sample_sd_dps": sample_sd,
            "candidate_minus_cat_standard_error_dps": (
                sample_sd / math.sqrt(EXPECTED_PAIR_COUNT)
            ),
            "wins": sum(value > 0.0 for value in values),
            "ties": sum(value == 0.0 for value in values),
            "losses": sum(value < 0.0 for value in values),
            "p05_dps": _linear_type7(values, 0.05),
            "median_dps": _linear_type7(values, 0.5),
            "p95_dps": _linear_type7(values, 0.95),
            "paired_t_statistic": statistic,
            "paired_t_degrees_of_freedom": EXPECTED_PAIR_COUNT - 1,
        }
    adjusted = _holm_adjust(raw_probabilities)
    rows = [{**base_rows[arm_id], **adjusted[arm_id]} for arm_id in CONFIRMATION_ARM_IDS]
    ranking = sorted(
        rows,
        key=lambda row: (-row["candidate_minus_cat_mean_dps"], row["arm_id"]),
    )
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "arm_results": rows,
        "diagnostic_ranking": [
            {"rank": index, **row} for index, row in enumerate(ranking, 1)
        ],
        "analysis_contract": {
            "primary_metric": "paired_candidate_minus_cat_mean_dps",
            "paired_test": "two_sided_paired_student_t",
            "multiplicity_method": "HOLM",
            "multiplicity_family_size": len(CONFIRMATION_ARM_IDS),
            "familywise_alpha": FAMILYWISE_ALPHA,
            "quantile_method": "linear_hyndman_fan_type_7",
            "tie_rule": "candidate_minus_cat_dps_exactly_zero",
        },
        "simulator_only": True,
        "mechanism_diagnostic_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "selection_or_promotion_performed": False,
        "deployment_allowed": False,
    }


def _extract_pair_trace(
    plan: Mapping[str, Any], output_directory: Path
) -> list[JSONMap]:
    trace: list[JSONMap] = []
    for group in sorted(plan["contract"]["groups"], key=lambda row: row["group_id"]):
        group_id = str(group["group_id"])
        rows = _read_rows(
            output_directory / "groups" / f"{group_id}.jsonl",
            f"rollout group {group_id}",
        )
        by_policy = {
            row.get("policy_identity", {}).get("policy_id"): row
            for row in rows
            if isinstance(row.get("policy_identity"), Mapping)
        }
        if set(by_policy) != {
            CAT_POLICY_ID,
            CONTRA_DEPLOYED_POLICY_ID,
            CONTRA260817_POLICY_ID,
            CAT2NEW_POLICY_ID,
        }:
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"rollout group {group_id} differs from the validated four-lane set"
            )
        candidate = by_policy[CAT2NEW_POLICY_ID]["lane_result"]
        cat = by_policy[CAT_POLICY_ID]["lane_result"]
        if not (
            candidate.get("offline_score_eligible") is True
            and cat.get("offline_score_eligible") is True
        ):
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"rollout group {group_id} is not candidate/Cat score eligible"
            )
        candidate_dps = float(candidate["dps"])
        cat_dps = float(cat["dps"])
        trace.append(
            {
                "group_id": group_id,
                "master_seed": group["master_seed"],
                "simulator_seed": group["simulator_seed"],
                "candidate_dps": candidate_dps,
                "cat_dps": cat_dps,
                "candidate_minus_cat_dps": candidate_dps - cat_dps,
            }
        )
    return trace


def analyze_confirmation_run_v1(
    manifest: Mapping[str, Any],
    *,
    run_root: str | Path,
    arm_output_directories: Mapping[str, str | Path],
) -> JSONMap:
    """Validate all raw outputs, then return one compact three-arm result."""

    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("arm_count") != len(CONFIRMATION_ARM_IDS)
    ):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "confirmation manifest schema or arm count mismatch"
        )
    contract = manifest.get("analysis_contract")
    expected_contract = {
        "primary_baseline_policy_id": CAT_POLICY_ID,
        "primary_metric": "paired_candidate_minus_cat_mean_dps",
        "distribution_diagnostics": ["wins_ties_losses", "p05", "median", "p95"],
        "multiplicity_method": "HOLM",
        "multiplicity_family_size": len(CONFIRMATION_ARM_IDS),
        "paired_test": "two_sided_paired_student_t",
        "familywise_alpha": FAMILYWISE_ALPHA,
        "quantile_method": "linear_hyndman_fan_type_7",
        "tie_rule": "candidate_minus_cat_dps_exactly_zero",
        "required_complete_eligible_pairs_per_arm": EXPECTED_PAIR_COUNT,
        "no_arm_is_preapproved_for_promotion": True,
    }
    if contract != expected_contract:
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "confirmation manifest analysis contract differs from the frozen contract"
        )
    if set(arm_output_directories) != set(CONFIRMATION_ARM_IDS):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "arm output directories must contain exactly the frozen three arm IDs"
        )
    root = Path(run_root).expanduser().resolve()
    manifest_arms = manifest.get("arms")
    if not isinstance(manifest_arms, list) or [
        row.get("arm_spec", {}).get("arm_id")
        for row in manifest_arms
        if isinstance(row, Mapping)
    ] != list(CONFIRMATION_ARM_IDS):
        raise Cat2NewFuryHorizonAnalysisV1Error(
            "confirmation manifest arm order differs from the frozen registry"
        )
    reductions: dict[str, JSONMap] = {}
    traces: dict[str, list[JSONMap]] = {}
    for arm in manifest_arms:
        arm_id = str(arm["arm_spec"]["arm_id"])
        runner = validate_runner_plan(
            _read_json(root / arm["runner_plan_path"], f"{arm_id} runner plan")
        )
        dispatch = validate_dispatch_plan_v3(
            _read_json(root / arm["dispatch_plan_path"], f"{arm_id} dispatch plan"),
            runner,
        )
        if (
            runner["plan_sha256"] != arm["runner_plan_sha256"]
            or sha256_json(dispatch) != arm["dispatch_plan_sha256"]
        ):
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"{arm_id} runner or dispatch identity mismatch"
            )
        output = Path(arm_output_directories[arm_id]).expanduser().resolve()
        reduction = reduce_dispatch_v3(runner, dispatch, output_directory=output)
        pair = reduction["paired_sufficient_statistics"]["candidate_minus_cat"]
        candidate = reduction["policy_sufficient_statistics"][CAT2NEW_POLICY_ID]
        cat = reduction["policy_sufficient_statistics"][CAT_POLICY_ID]
        if not (
            reduction["paired_group_count"] == EXPECTED_PAIR_COUNT
            and pair["pair_count"] == EXPECTED_PAIR_COUNT
            and pair["both_offline_score_eligible_count"] == EXPECTED_PAIR_COUNT
            and candidate["completion_count"] == EXPECTED_PAIR_COUNT
            and candidate["offline_score_eligible_count"] == EXPECTED_PAIR_COUNT
            and cat["completion_count"] == EXPECTED_PAIR_COUNT
            and cat["offline_score_eligible_count"] == EXPECTED_PAIR_COUNT
            and reduction["dynamic_runtime_receipts_complete"] is True
        ):
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"{arm_id} is not the complete eligible 256-pair diagnostic"
            )
        trace = _extract_pair_trace(runner, output)
        delta_sum = math.fsum(row["candidate_minus_cat_dps"] for row in trace)
        squared_sum = math.fsum(
            row["candidate_minus_cat_dps"] ** 2 for row in trace
        )
        if not (
            math.isclose(delta_sum, pair["dps_delta_sum"], rel_tol=0.0, abs_tol=1e-9)
            and math.isclose(
                squared_sum,
                pair["dps_delta_squared_sum"],
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            raise Cat2NewFuryHorizonAnalysisV1Error(
                f"{arm_id} compact paired vector differs from its strict reduction"
            )
        reductions[arm_id] = reduction
        traces[arm_id] = trace
    summary = summarize_horizon_deltas_v1(
        {
            arm_id: [row["candidate_minus_cat_dps"] for row in traces[arm_id]]
            for arm_id in CONFIRMATION_ARM_IDS
        }
    )
    summary["confirmation_id"] = manifest.get("confirmation_id")
    summary["source_reductions"] = {
        arm_id: {
            "status": reductions[arm_id]["status"],
            "runner_plan_sha256": reductions[arm_id]["runner_plan_sha256"],
            "paired_group_count": reductions[arm_id]["paired_group_count"],
            "unique_rollout_count": reductions[arm_id]["unique_rollout_count"],
            "dynamic_runtime_receipts_complete": reductions[arm_id][
                "dynamic_runtime_receipts_complete"
            ],
            "formal_comparison_status": reductions[arm_id][
                "formal_comparison_status"
            ],
        }
        for arm_id in CONFIRMATION_ARM_IDS
    }
    summary["paired_trace"] = traces
    return summary


def _parse_arm_outputs(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        arm_id, separator, raw_path = value.partition("=")
        if not separator or not arm_id or not raw_path or arm_id in result:
            raise Cat2NewFuryHorizonAnalysisV1Error(
                "--arm-output values must be unique ARM_ID=PATH pairs"
            )
        result[arm_id] = Path(raw_path)
    return result


def _write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if path is None:
        print(payload, end="")
    else:
        destination = path.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--arm-output", action="append", default=[], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = analyze_confirmation_run_v1(
            _read_json(args.manifest, "confirmation manifest"),
            run_root=args.run_root,
            arm_output_directories=_parse_arm_outputs(args.arm_output),
        )
        _write_json(args.output, result)
        return 0
    except (Cat2NewFuryHorizonAnalysisV1Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "Cat2NewFuryHorizonAnalysisV1Error",
    "SCHEMA",
    "STATUS",
    "analyze_confirmation_run_v1",
    "summarize_horizon_deltas_v1",
)
