"""Learn non-voting current-observation subsets of the latched HS policy.

This learner consumes the complete 64-seed formal latch run and a disjoint
192-seed development extension.  It revalidates every compact case through the
strict full-wave reducer, then screens only subsets of interventions that were
actually executed.  A non-matching decision always falls back to Cat.

The resulting shortlist is development evidence.  Reusing the formal run for
subset discovery means that this module cannot authorize voting, deployment,
or a policy-superiority claim.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import glob
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .cat_latched_first_opportunity_full_wave_v1 import TARGET_ROUTE
from .cat_latched_first_opportunity_policy_v1 import (
    POLICY_ID,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
)
from .cat_sparse_guard_policy_v2 import FEATURE_ORDER, _FEATURE_VALUES
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .factored_external_press_matrix_v1 import MATRIX_RANKS, MATRIX_STRATA
from .factored_latched_first_opportunity_full_wave_v1 import (
    FROZEN_POLICY_SCHEMA,
    PHASE,
    PHASE_SEED_BASE,
    SUMMARY_SCHEMA as SOURCE_SUMMARY_SCHEMA,
    _load_item_database,
    _validated_exact_effect_v1,
    reduce_latched_full_wave_v1,
    validate_frozen_latched_policy_v1,
)
from .factored_sparse_guard_full_wave_v1 import _effect_statistics


SCHEMA = "factored_latched_subset_learner/v1"
FORMAL_SAMPLE_INDICES = tuple(range(2, 66))
EXTENSION_SAMPLE_INDICES = tuple(range(66, 258))
ALL_SAMPLE_INDICES = FORMAL_SAMPLE_INDICES + EXTENSION_SAMPLE_INDICES
FOLD_COUNT = 4
MIN_TOTAL_SEEDS = 256
MIN_TRIGGERED_SEEDS = 16
FOLD_ASSIGNMENT_CONTRACT = "(SAMPLE_INDEX_MINUS_2)_MOD_4"
PARENT_DECISION_CONTRACT = (
    "FIRST_EXACT_BROAD_OPPORTUNITY_ONCE;SUBSET_ONLY_OF_EXECUTED_"
    "FLURRY_INACTIVE_INTERVENTIONS;NONMATCH_CAT_FALLBACK"
)

_ALLOWED_RESOLUTIONS = frozenset({
    RESOLUTION_INTERVENED,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
})


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _finite_effect(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _validated_features(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != set(FEATURE_ORDER):
        raise ValueError("intervention features must be the complete current-only schema")
    features: dict[str, str] = {}
    for name in FEATURE_ORDER:
        value = raw.get(name)
        if not isinstance(value, str) or value not in _FEATURE_VALUES[name]:
            raise ValueError(f"invalid current feature value for {name}")
        features[name] = value
    return features


def _fold_index(sample_index: int) -> int:
    return (sample_index - FORMAL_SAMPLE_INDICES[0]) % FOLD_COUNT


def _predicate_matches(
    features: Mapping[str, str], predicates: Sequence[Mapping[str, str]],
) -> bool:
    return all(features.get(row["feature"]) == row["value"] for row in predicates)


def _candidate_predicates(
    intervention_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[list[dict[str, str]]]]:
    values = {
        name: {row["current_features"][name] for row in intervention_rows}
        for name in FEATURE_ORDER
    }
    varying = [name for name in FEATURE_ORDER if len(values[name]) > 1]
    candidates: list[list[dict[str, str]]] = [[]]
    for name in varying:
        for value in sorted(values[name]):
            candidates.append([{"feature": name, "value": value}])
    for first_index, first in enumerate(varying):
        for second in varying[first_index + 1:]:
            observed_pairs = sorted({
                (
                    row["current_features"][first],
                    row["current_features"][second],
                )
                for row in intervention_rows
            })
            for first_value, second_value in observed_pairs:
                candidates.append([
                    {"feature": first, "value": first_value},
                    {"feature": second, "value": second_value},
                ])
    return varying, candidates


def _candidate_diagnostic(
    records: Sequence[Mapping[str, Any]],
    *,
    candidate_index: int,
    predicates: Sequence[Mapping[str, str]],
    min_triggered_seeds: int,
) -> dict[str, Any]:
    effects: dict[int, float] = {}
    triggered: set[int] = set()
    sample_by_seed: dict[int, int] = {}
    for row in records:
        seed = row["seed"]
        sample_by_seed[seed] = row["sample_index"]
        matches = bool(
            row["resolution"] == RESOLUTION_INTERVENED
            and _predicate_matches(row["current_features"], predicates)
        )
        if matches:
            triggered.add(seed)
            effects[seed] = row["paired_effective_damage_delta"]
        else:
            effects[seed] = 0.0

    full = _effect_statistics(effects.values())
    folds: list[dict[str, Any]] = []
    fold_train_positive = True
    fold_heldout_nonnegative = True
    for fold_index in range(FOLD_COUNT):
        heldout_seeds = [
            seed for seed in effects
            if _fold_index(sample_by_seed[seed]) == fold_index
        ]
        train_seeds = [seed for seed in effects if seed not in set(heldout_seeds)]
        train_stats = _effect_statistics(effects[seed] for seed in train_seeds)
        heldout_stats = _effect_statistics(effects[seed] for seed in heldout_seeds)
        train_lcb = train_stats["lower_95_normal_effective_damage_delta_bound"]
        heldout_mean = heldout_stats["mean_paired_effective_damage_delta"]
        train_pass = bool(train_lcb is not None and train_lcb > 0)
        heldout_pass = bool(heldout_mean is not None and heldout_mean >= 0)
        fold_train_positive = fold_train_positive and train_pass
        fold_heldout_nonnegative = fold_heldout_nonnegative and heldout_pass
        folds.append({
            "fold_index": fold_index,
            "train_seed_count": len(train_seeds),
            "heldout_seed_count": len(heldout_seeds),
            "train_triggered_seed_count": sum(seed in triggered for seed in train_seeds),
            "heldout_triggered_seed_count": sum(seed in triggered for seed in heldout_seeds),
            "train_effect_statistics": train_stats,
            "heldout_effect_statistics": heldout_stats,
            "train_lower_bound_positive": train_pass,
            "heldout_mean_nonnegative": heldout_pass,
        })

    full_lcb = full["lower_95_normal_effective_damage_delta_bound"]
    support_pass = len(triggered) >= min_triggered_seeds
    full_pass = bool(full_lcb is not None and full_lcb > 0)
    reason_codes = []
    if not support_pass:
        reason_codes.append("TRIGGERED_SUPPORT_BELOW_16")
    if not full_pass:
        reason_codes.append("FULL_256_LOWER_BOUND_NOT_POSITIVE")
    if not fold_train_positive:
        reason_codes.append("ONE_OR_MORE_FOLD_TRAIN_LOWER_BOUNDS_NOT_POSITIVE")
    if not fold_heldout_nonnegative:
        reason_codes.append("ONE_OR_MORE_FOLD_HELDOUT_MEANS_NEGATIVE")
    passed = bool(
        support_pass and full_pass
        and fold_train_positive and fold_heldout_nonnegative
    )
    return {
        "candidate_id": f"subset-{candidate_index:04d}",
        "guard": {
            "decision_contract": PARENT_DECISION_CONTRACT,
            "action": "ADD_HS_QUEUE",
            "predicate_count": len(predicates),
            "predicates": deepcopy(list(predicates)),
            "nonmatch_action": "EXACT_CAT_FALLBACK",
        },
        "total_seed_count": len(records),
        "triggered_seed_count": len(triggered),
        "full_effect_statistics": full,
        "folds": folds,
        "support_gate_passed": support_pass,
        "full_lower_bound_positive": full_pass,
        "every_fold_train_lower_bound_positive": fold_train_positive,
        "every_fold_heldout_mean_nonnegative": fold_heldout_nonnegative,
        "passed_nonvoting_subset_screen": passed,
        "gate_status": (
            "PASSED_NONVOTING_SUBSET_SCREEN"
            if passed else "REJECTED_NONVOTING_SUBSET_SCREEN"
        ),
        "reason_codes": reason_codes,
    }


def learn_latched_subset_guards_v1(
    records: Iterable[Mapping[str, Any]],
    *,
    min_total_seeds: int = MIN_TOTAL_SEEDS,
    min_triggered_seeds: int = MIN_TRIGGERED_SEEDS,
) -> dict[str, Any]:
    """Enumerate broad, one-, and two-predicate intervention subsets."""

    if type(min_total_seeds) is not int or min_total_seeds < MIN_TOTAL_SEEDS:
        raise ValueError("minimum total support may not be lower than 256 seeds")
    if type(min_triggered_seeds) is not int or min_triggered_seeds < MIN_TRIGGERED_SEEDS:
        raise ValueError("minimum triggered support may not be lower than 16 seeds")
    rows = []
    seen_seeds: set[int] = set()
    seen_samples: set[int] = set()
    for raw in records:
        if not isinstance(raw, Mapping):
            raise ValueError("learner records must be mappings")
        seed = raw.get("seed")
        sample = raw.get("sample_index")
        resolution = raw.get("resolution")
        if type(seed) is not int or type(sample) is not int:
            raise ValueError("learner seed and sample index must be integers")
        if seed in seen_seeds or sample in seen_samples:
            raise ValueError("learner records must contain one row per seed and sample")
        if resolution not in _ALLOWED_RESOLUTIONS:
            raise ValueError("learner record has unknown or unsupported resolution")
        effect = _finite_effect(
            raw.get("paired_effective_damage_delta"),
            "paired_effective_damage_delta",
        )
        features = None
        if resolution == RESOLUTION_INTERVENED:
            features = _validated_features(raw.get("current_features"))
        elif effect != 0.0:
            raise ValueError("Cat fallback rows must have an exact zero paired effect")
        seen_seeds.add(seed)
        seen_samples.add(sample)
        rows.append({
            "seed": seed,
            "sample_index": sample,
            "resolution": resolution,
            "current_features": features,
            "paired_effective_damage_delta": effect,
        })
    rows.sort(key=lambda row: row["sample_index"])
    if len(rows) < min_total_seeds:
        raise ValueError("latched subset learner requires at least 256 complete seeds")
    fold_sizes = [sum(_fold_index(row["sample_index"]) == fold for row in rows)
                  for fold in range(FOLD_COUNT)]
    if len(set(fold_sizes)) != 1 or 0 in fold_sizes:
        raise ValueError("four-fold sample assignment is not balanced")

    intervention_rows = [
        row for row in rows if row["resolution"] == RESOLUTION_INTERVENED
    ]
    varying, predicate_sets = _candidate_predicates(intervention_rows)
    diagnostics = [
        _candidate_diagnostic(
            rows,
            candidate_index=index,
            predicates=predicates,
            min_triggered_seeds=min_triggered_seeds,
        )
        for index, predicates in enumerate(predicate_sets)
    ]
    shortlist = [
        deepcopy(row) for row in diagnostics
        if row["passed_nonvoting_subset_screen"] is True
    ]
    shortlist.sort(key=lambda row: (
        -row["full_effect_statistics"][
            "lower_95_normal_effective_damage_delta_bound"
        ],
        -row["full_effect_statistics"]["mean_paired_effective_damage_delta"],
        row["guard"]["predicate_count"],
        tuple(
            (item["feature"], item["value"])
            for item in row["guard"]["predicates"]
        ),
    ))
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_NONVOTING_SUBSET_SHORTLIST"
            if shortlist else "COMPLETE_NO_SUBSET_PASSED_NONVOTING"
        ),
        "source_parent_policy_id": POLICY_ID,
        "parent_decision_contract": PARENT_DECISION_CONTRACT,
        "total_seed_count": len(rows),
        "source_intervention_seed_count": len(intervention_rows),
        "fold_count": FOLD_COUNT,
        "fold_assignment_contract": FOLD_ASSIGNMENT_CONTRACT,
        "fold_seed_counts": fold_sizes,
        "minimum_total_seed_count": min_total_seeds,
        "minimum_triggered_seed_count": min_triggered_seeds,
        "intervention_feature_schema": list(FEATURE_ORDER),
        "truly_varying_intervention_features": varying,
        "candidate_family": "BROAD_PLUS_ONE_OR_TWO_CURRENT_OBSERVATION_PREDICATES",
        "evaluated_candidate_count": len(diagnostics),
        "candidate_diagnostics": diagnostics,
        "shortlisted_guard_count": len(shortlist),
        "shortlist": shortlist,
        "nonmatching_decisions_use_exact_cat_fallback": True,
        "selection_reuses_formal_data": True,
        "unknown_effect_imputed": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "policy_superiority_claimed": False,
    }


def _summary_matches_reduction(
    supplied: Mapping[str, Any], expected: Mapping[str, Any], *, label: str,
) -> None:
    if not isinstance(supplied, Mapping):
        raise ValueError(f"{label} summary must be a mapping")
    for name, value in expected.items():
        if supplied.get(name) != value:
            raise ValueError(f"{label} summary differs from strict case reduction at {name}")


def _source_rows_by_index(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[int, list[Mapping[str, Any]]]:
    rows: dict[int, list[Mapping[str, Any]]] = {}
    for artifact in artifacts:
        matrix = artifact.get("matrix") if isinstance(artifact, Mapping) else None
        sample = matrix.get("sample_index") if isinstance(matrix, Mapping) else None
        if type(sample) is not int:
            raise ValueError("case artifact lacks an integer sample index")
        rows.setdefault(sample, []).append(artifact)
    return rows


def _validate_cell_balance(
    rows_by_index: Mapping[int, Sequence[Mapping[str, Any]]],
    expected_indices: Sequence[int], *, label: str,
) -> list[Mapping[str, Any]]:
    if set(rows_by_index) != set(expected_indices):
        raise ValueError(f"{label} sample indices must match the frozen range")
    expected_cells = {
        (rank, stratum) for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
    }
    flattened: list[Mapping[str, Any]] = []
    for sample in expected_indices:
        rows = list(rows_by_index[sample])
        cells = []
        for artifact in rows:
            matrix = artifact.get("matrix")
            cells.append((matrix.get("rank"), matrix.get("stratum")))
        if len(rows) != len(expected_cells) or set(cells) != expected_cells:
            raise ValueError(f"{label} does not contain one artifact for every matrix cell")
        flattened.extend(rows)
    return flattened


def _records_from_validated_rows(
    artifacts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    target_wire = asdict(TARGET_ROUTE)
    records = []
    seen_seeds: set[int] = set()
    for artifact in artifacts:
        if artifact.get("mechanism_route") != target_wire:
            continue
        matrix = artifact["matrix"]
        result = artifact["result"]
        effect = _validated_exact_effect_v1(result)
        if effect is None:
            raise ValueError("strict target-route effect is UNKNOWN")
        seed = matrix["seed"]
        if seed in seen_seeds:
            raise ValueError("subset learner requires exactly one exact-route case per seed")
        receipt = result.get("resolution_receipt")
        features = receipt.get("current_features") if isinstance(receipt, Mapping) else None
        records.append({
            "seed": seed,
            "sample_index": matrix["sample_index"],
            "resolution": result.get("resolution"),
            "current_features": features,
            "paired_effective_damage_delta": effect,
        })
        seen_seeds.add(seed)
    if len(records) != len(ALL_SAMPLE_INDICES):
        raise ValueError("subset learner requires one exact-route result for each of 256 seeds")
    return records


def validate_latched_subset_sources_v1(
    frozen_policy: Mapping[str, Any],
    formal_summary: Mapping[str, Any],
    extension_summary: Mapping[str, Any],
    case_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate the two disjoint source windows and return learner records."""

    validate_frozen_latched_policy_v1(frozen_policy)
    artifacts = list(case_artifacts)
    by_index = _source_rows_by_index(artifacts)
    if set(FORMAL_SAMPLE_INDICES) & set(EXTENSION_SAMPLE_INDICES):
        raise AssertionError("frozen source ranges overlap")
    if set(by_index) != set(ALL_SAMPLE_INDICES):
        raise ValueError("case artifacts must contain exactly sample indices 2..257")
    formal_rows = _validate_cell_balance(
        {index: by_index[index] for index in FORMAL_SAMPLE_INDICES},
        FORMAL_SAMPLE_INDICES,
        label="formal",
    )
    extension_rows = _validate_cell_balance(
        {index: by_index[index] for index in EXTENSION_SAMPLE_INDICES},
        EXTENSION_SAMPLE_INDICES,
        label="development extension",
    )
    formal_expected = reduce_latched_full_wave_v1(
        frozen_policy, formal_rows,
        item_database=item_database,
        min_distinct_seeds=64,
    )
    extension_expected = reduce_latched_full_wave_v1(
        frozen_policy, extension_rows,
        item_database=item_database,
        min_distinct_seeds=64,
    )
    _summary_matches_reduction(
        formal_summary, formal_expected, label="formal",
    )
    _summary_matches_reduction(
        extension_summary, extension_expected, label="development extension",
    )
    for label, summary, indices in (
        ("formal", formal_summary, FORMAL_SAMPLE_INDICES),
        ("development extension", extension_summary, EXTENSION_SAMPLE_INDICES),
    ):
        expected_seeds = [PHASE_SEED_BASE + index for index in indices]
        if (
            summary.get("schema") != SOURCE_SUMMARY_SCHEMA
            or summary.get("source_policy_schema") != FROZEN_POLICY_SCHEMA
            or summary.get("phase") != PHASE
            or summary.get("sample_indices") != list(indices)
            or summary.get("fresh_seeds") != expected_seeds
            or summary.get("assigned_seed_count") != len(indices)
            or summary.get("complete_seed_count") != len(indices)
            or summary.get("unknown_seed_count") != 0
            or summary.get("matrix_case_count")
            != len(indices) * len(MATRIX_RANKS) * len(MATRIX_STRATA)
            or summary.get("exact_route_case_count") != len(indices)
            or summary.get("all_semantic_terminal_clock_receipts_valid") is not True
            or summary.get("comparison_ready") is not True
            or summary.get("unknown_effect_imputed") is not False
            or summary.get("voting_eligible") is not False
            or summary.get("deployment_eligible") is not False
        ):
            raise ValueError(f"{label} summary source gate failed")

    records = _records_from_validated_rows(formal_rows + extension_rows)
    formal_seeds = set(formal_summary["fresh_seeds"])
    extension_seeds = set(extension_summary["fresh_seeds"])
    if formal_seeds & extension_seeds:
        raise ValueError("formal and development extension seeds overlap")
    return records, {
        "schema": "factored_latched_subset_source_receipt/v1",
        "frozen_policy_schema": frozen_policy["schema"],
        "formal_summary_schema": formal_summary["schema"],
        "extension_summary_schema": extension_summary["schema"],
        "formal_sample_indices": list(FORMAL_SAMPLE_INDICES),
        "extension_sample_indices": list(EXTENSION_SAMPLE_INDICES),
        "formal_seed_count": len(formal_seeds),
        "extension_seed_count": len(extension_seeds),
        "combined_seed_count": len(formal_seeds | extension_seeds),
        "formal_extension_seed_overlap_count": 0,
        "matrix_cell_count_per_seed": len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "all_12_cells_balanced": True,
        "unknown_seed_count": 0,
        "strict_case_effects_revalidated": True,
    }


def build_latched_subset_shortlist_v1(
    frozen_policy: Mapping[str, Any],
    formal_summary: Mapping[str, Any],
    extension_summary: Mapping[str, Any],
    case_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
) -> dict[str, Any]:
    records, source_receipt = validate_latched_subset_sources_v1(
        frozen_policy,
        formal_summary,
        extension_summary,
        case_artifacts,
        item_database=item_database,
    )
    result = learn_latched_subset_guards_v1(records)
    result["source_receipt"] = source_receipt
    result["source_frozen_policy_schema"] = FROZEN_POLICY_SCHEMA
    result["source_formal_summary_schema"] = SOURCE_SUMMARY_SCHEMA
    result["source_extension_summary_schema"] = SOURCE_SUMMARY_SCHEMA
    return result


def _expand_case_globs(pattern_groups: Sequence[Sequence[str]]) -> list[Path]:
    patterns = [pattern for group in pattern_groups for pattern in group]
    matched: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        candidates = [Path(path) for path in glob.glob(pattern, recursive=True)]
        if not candidates and Path(pattern).is_file():
            candidates = [Path(pattern)]
        if not candidates:
            raise ValueError(f"case glob matched no files: {pattern}")
        for path in sorted(candidates, key=lambda item: str(item)):
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                raise ValueError(f"case file was supplied more than once: {path}")
            seen.add(key)
            matched.append(path)
    if not matched:
        raise ValueError("case globs matched no files")
    return matched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frozen-policy", "--policy", dest="frozen_policy",
        type=Path, required=True,
    )
    parser.add_argument("--formal-summary", type=Path, required=True)
    parser.add_argument("--extension-summary", type=Path, required=True)
    parser.add_argument(
        "--case-glob", "--case-globs", dest="case_globs",
        action="append", nargs="+", required=True,
    )
    parser.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    case_paths = _expand_case_globs(args.case_globs)
    result = build_latched_subset_shortlist_v1(
        _read_json(args.frozen_policy),
        _read_json(args.formal_summary),
        _read_json(args.extension_summary),
        [_read_json(path) for path in case_paths],
        item_database=_load_item_database(args.item_db),
    )
    _write_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "status": result["status"],
        "evaluated_candidate_count": result["evaluated_candidate_count"],
        "shortlisted_guard_count": result["shortlisted_guard_count"],
        "output": str(args.output),
    }, ensure_ascii=False, separators=(",", ":")))


__all__ = (
    "SCHEMA",
    "FORMAL_SAMPLE_INDICES",
    "EXTENSION_SAMPLE_INDICES",
    "learn_latched_subset_guards_v1",
    "validate_latched_subset_sources_v1",
    "build_latched_subset_shortlist_v1",
)


if __name__ == "__main__":
    main()
