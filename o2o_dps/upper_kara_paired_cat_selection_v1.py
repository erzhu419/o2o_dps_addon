"""Conservative Cat-relative selection for causal residual programs.

Training ranks every residual by paired own effective-damage delta against the
exact zero-residual Cat program on the same loadout and simulator seed.  A
separate validation cohort then admits the selected nonzero residual only when
the lower endpoint of its paired confidence interval is strictly positive.
Otherwise the result is the exact zero-residual program for that loadout.

This reducer consumes terminal damage only.  It does not inspect future target
death times, action traces, or any held-out policy observation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_paired_cat_selection/v1"
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_MINIMUM_VALIDATION_PAIRS = 32


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class PairedCatDamageV1:
    cohort: str
    loadout_id: str
    candidate_ref: str
    zero_residual_ref: str
    seed: int
    candidate_damage: float
    zero_residual_damage: float

    def __post_init__(self) -> None:
        if self.cohort not in {"TRAIN", "VALIDATION"}:
            raise ValueError("cohort must be TRAIN or VALIDATION")
        for field in ("loadout_id", "candidate_ref", "zero_residual_ref"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        object.__setattr__(
            self,
            "candidate_damage",
            _finite(self.candidate_damage, "candidate_damage"),
        )
        object.__setattr__(
            self,
            "zero_residual_damage",
            _finite(self.zero_residual_damage, "zero_residual_damage"),
        )

    @property
    def damage_delta(self) -> float:
        return self.candidate_damage - self.zero_residual_damage

    def to_dict(self) -> JSONMap:
        return {
            "cohort": self.cohort,
            "loadout_id": self.loadout_id,
            "candidate_ref": self.candidate_ref,
            "zero_residual_ref": self.zero_residual_ref,
            "seed": self.seed,
            "candidate_damage": self.candidate_damage,
            "zero_residual_damage": self.zero_residual_damage,
        }


def paired_cat_damage_from_dict_v1(value: object) -> PairedCatDamageV1:
    if not isinstance(value, Mapping):
        raise ValueError("paired damage row must be an object")
    expected = {
        "cohort",
        "loadout_id",
        "candidate_ref",
        "zero_residual_ref",
        "seed",
        "candidate_damage",
        "zero_residual_damage",
    }
    if set(value) != expected:
        raise ValueError("paired damage row fields differ from v1 contract")
    return PairedCatDamageV1(**dict(value))


def exact_paired_zero_program_ref_v1(
    expected: CausalActionProgramV1,
    program_receipts: Sequence[Mapping[str, Any]],
    *,
    label: str = "paired zero program",
) -> str:
    """Resolve any paired zero arm by full serialized program identity."""

    if not isinstance(expected, CausalActionProgramV1):
        raise TypeError("expected must be CausalActionProgramV1")
    matches: list[str] = []
    for receipt in program_receipts:
        if not isinstance(receipt, Mapping):
            raise ValueError("program receipt must be an object")
        program = causal_action_program_from_dict_v1(receipt.get("program"))
        if (
            program == expected
            and program.origin is ProgramOriginV1.SEARCHED
            and receipt.get("program_ref") == program.program_id
            and receipt.get("program_key") == program.program_key()
        ):
            matches.append(program.program_id)
    if len(matches) != 1:
        raise ValueError(
            f"{label} must occur exactly once by full program identity; "
            f"found {len(matches)}"
        )
    return matches[0]


def exact_cat_zero_residual_ref_v1(
    loadout_id: str, program_receipts: Sequence[Mapping[str, Any]]
) -> str:
    """Resolve the exact-Cat zero arm by full program identity."""

    return exact_paired_zero_program_ref_v1(
        cat_zero_residual_program_v1(loadout_id),
        program_receipts,
        label=f"{loadout_id} structurally exact Cat zero residual",
    )


def paired_cat_damage_rows_from_remote_lanes_v1(
    *,
    cohort: str,
    loadout_id: str,
    candidate_refs: Sequence[str],
    zero_residual_ref: str,
    lanes: Sequence[Mapping[str, Any]],
) -> tuple[PairedCatDamageV1, ...]:
    """Project complete remote lanes into exact same-seed Cat pairs."""

    refs = tuple(_text(value, "candidate_ref") for value in candidate_refs)
    if len(refs) != len(set(refs)) or zero_residual_ref not in refs:
        raise ValueError(
            "candidate_refs must be unique and include zero_residual_ref"
        )
    by_ref_seed: dict[tuple[str, int], Mapping[str, Any]] = {}
    seed_sets: dict[str, set[int]] = {ref: set() for ref in refs}
    for lane in lanes:
        if not isinstance(lane, Mapping):
            raise ValueError("remote lane must be an object")
        ref = lane.get("program_ref")
        if ref not in seed_sets:
            continue
        seed = lane.get("master_seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("remote lane master_seed must be an integer")
        key = (ref, seed)
        if key in by_ref_seed:
            raise ValueError("remote lanes contain a duplicate program/seed")
        if lane.get("status") != "COMPLETE":
            raise ValueError("paired candidate lane is not COMPLETE")
        _finite(lane.get("own_effective_damage"), "own_effective_damage")
        by_ref_seed[key] = lane
        seed_sets[ref].add(seed)
    zero_seeds = seed_sets[zero_residual_ref]
    if not zero_seeds or any(seeds != zero_seeds for seeds in seed_sets.values()):
        raise ValueError("remote candidates do not share exact zero-lane seed coverage")

    rows: list[PairedCatDamageV1] = []
    for ref in refs:
        for seed in sorted(zero_seeds):
            candidate = by_ref_seed[(ref, seed)]
            zero = by_ref_seed[(zero_residual_ref, seed)]
            if candidate.get("simulator_seed") != zero.get("simulator_seed"):
                raise ValueError("paired remote lanes have different simulator seeds")
            rows.append(
                PairedCatDamageV1(
                    cohort=cohort,
                    loadout_id=loadout_id,
                    candidate_ref=ref,
                    zero_residual_ref=zero_residual_ref,
                    seed=seed,
                    candidate_damage=candidate["own_effective_damage"],
                    zero_residual_damage=zero["own_effective_damage"],
                )
            )
    return tuple(rows)


def _normal_paired_interval_v1(
    values: Sequence[float], confidence_level: float
) -> JSONMap:
    if not 0.5 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0.5 and 1")
    if len(values) < 2:
        raise ValueError("a paired confidence interval requires at least two pairs")
    center = mean(values)
    standard_deviation = stdev(values)
    standard_error = standard_deviation / math.sqrt(len(values))
    critical = NormalDist().inv_cdf(0.5 + confidence_level / 2.0)
    half_width = critical * standard_error
    return {
        "kind": "PAIRED_MEAN_NORMAL_APPROXIMATION_TWO_SIDED",
        "confidence_level": confidence_level,
        "pair_count": len(values),
        "mean_damage_delta": center,
        "sample_standard_deviation": standard_deviation,
        "standard_error": standard_error,
        "critical_value": critical,
        "lower_bound": center - half_width,
        "upper_bound": center + half_width,
    }


def _index_cohort_v1(
    rows: Sequence[PairedCatDamageV1], cohort: str
) -> dict[tuple[str, str], tuple[PairedCatDamageV1, ...]]:
    if not rows:
        raise ValueError(f"{cohort.lower()} rows must be nonempty")
    grouped: dict[tuple[str, str], list[PairedCatDamageV1]] = {}
    observed: set[tuple[str, str, int]] = set()
    for row in rows:
        if not isinstance(row, PairedCatDamageV1):
            raise TypeError("rows must contain PairedCatDamageV1")
        if row.cohort != cohort:
            raise ValueError(f"{cohort.lower()} rows contain another cohort")
        key = (row.loadout_id, row.candidate_ref, row.seed)
        if key in observed:
            raise ValueError("paired cohort contains a duplicate candidate/seed")
        observed.add(key)
        grouped.setdefault((row.loadout_id, row.candidate_ref), []).append(row)
    return {
        key: tuple(sorted(value, key=lambda row: row.seed))
        for key, value in grouped.items()
    }


def _validate_zero_and_coverage_v1(
    grouped: Mapping[tuple[str, str], tuple[PairedCatDamageV1, ...]],
    *,
    cohort: str,
) -> tuple[dict[str, str], dict[str, frozenset[int]]]:
    zero_by_loadout: dict[str, str] = {}
    seed_set_by_loadout: dict[str, frozenset[int]] = {}
    for (loadout_id, candidate_ref), rows in grouped.items():
        zero_refs = {row.zero_residual_ref for row in rows}
        if len(zero_refs) != 1:
            raise ValueError(f"{cohort} candidate changes zero-residual reference")
        zero_ref = next(iter(zero_refs))
        previous = zero_by_loadout.setdefault(loadout_id, zero_ref)
        if previous != zero_ref:
            raise ValueError(f"{cohort} loadout has multiple zero-residual programs")
        seeds = frozenset(row.seed for row in rows)
        prior_seeds = seed_set_by_loadout.setdefault(loadout_id, seeds)
        if prior_seeds != seeds:
            raise ValueError(f"{cohort} candidates are not paired on identical seeds")
        if candidate_ref == zero_ref and any(row.damage_delta != 0.0 for row in rows):
            raise ValueError("zero-residual Cat row must have exact delta=0")
    for loadout_id, zero_ref in zero_by_loadout.items():
        if (loadout_id, zero_ref) not in grouped:
            raise ValueError(f"{cohort} loadout lacks explicit zero-residual rows")
    return zero_by_loadout, seed_set_by_loadout


def select_paired_cat_residual_v1(
    *,
    training_rows: Sequence[PairedCatDamageV1],
    validation_rows: Sequence[PairedCatDamageV1],
    minimum_validation_pairs: int = DEFAULT_MINIMUM_VALIDATION_PAIRS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> JSONMap:
    """Rank on TRAIN paired deltas, then gate once on independent VALIDATION."""

    if (
        isinstance(minimum_validation_pairs, bool)
        or not isinstance(minimum_validation_pairs, int)
        or minimum_validation_pairs < 2
    ):
        raise ValueError("minimum_validation_pairs must be an integer >= 2")
    train = _index_cohort_v1(training_rows, "TRAIN")
    validation = _index_cohort_v1(validation_rows, "VALIDATION")
    train_zero, train_seeds = _validate_zero_and_coverage_v1(
        train, cohort="TRAIN"
    )
    validation_zero, validation_seeds = _validate_zero_and_coverage_v1(
        validation, cohort="VALIDATION"
    )
    if set(validation_zero) != set(train_zero):
        raise ValueError("validation must contain every training loadout")
    for loadout_id, zero_ref in validation_zero.items():
        if train_zero[loadout_id] != zero_ref:
            raise ValueError("training and validation zero-residual identities differ")

    training_summary: list[JSONMap] = []
    ranked: list[tuple[float, str, str]] = []
    for (loadout_id, candidate_ref), rows in sorted(train.items()):
        deltas = [row.damage_delta for row in rows]
        delta_mean = mean(deltas)
        zero_ref = train_zero[loadout_id]
        is_zero = candidate_ref == zero_ref
        training_summary.append(
            {
                "loadout_id": loadout_id,
                "candidate_ref": candidate_ref,
                "zero_residual_ref": zero_ref,
                "is_zero_residual": is_zero,
                "pair_count": len(rows),
                "mean_candidate_damage": mean(row.candidate_damage for row in rows),
                "mean_zero_residual_damage": mean(
                    row.zero_residual_damage for row in rows
                ),
                "mean_paired_damage_delta": delta_mean,
            }
        )
        # A deterministic candidate-ref/loadout tie-break leaves exact ties on
        # zero residual rather than manufacturing a positive update.
        ranked.append((delta_mean, loadout_id, candidate_ref))
    per_loadout_admission: list[JSONMap] = []
    for loadout_id in sorted(train_zero):
        loadout_ranked = [row for row in ranked if row[1] == loadout_id]
        selected_delta, _, selected_ref = sorted(
            loadout_ranked,
            key=lambda row: (
                -row[0],
                row[2] != train_zero[row[1]],
                row[2],
            ),
        )[0]
        zero_ref = train_zero[loadout_id]
        overlap = train_seeds[loadout_id] & validation_seeds[loadout_id]
        if overlap:
            raise ValueError(
                f"training and validation seeds overlap for {loadout_id}: "
                f"{sorted(overlap)}"
            )
        zero_validation = validation[(loadout_id, zero_ref)]

        if selected_ref == zero_ref:
            validation_interval = None
            accepted_ref = zero_ref
            gate_status = "ZERO_RESIDUAL_SELECTED_ON_TRAIN"
            accepted_validation = zero_validation
        else:
            validation_key = (loadout_id, selected_ref)
            if validation_key not in validation:
                raise ValueError(
                    "validation lacks a selected per-loadout training candidate"
                )
            selected_validation = validation[validation_key]
            if len(selected_validation) < minimum_validation_pairs:
                validation_interval = None
                accepted_ref = zero_ref
                gate_status = (
                    "FALLBACK_ZERO_RESIDUAL_INSUFFICIENT_VALIDATION_PAIRS"
                )
                accepted_validation = zero_validation
            else:
                validation_interval = _normal_paired_interval_v1(
                    [row.damage_delta for row in selected_validation],
                    confidence_level,
                )
                if validation_interval["lower_bound"] > 0.0:
                    accepted_ref = selected_ref
                    gate_status = "NONZERO_RESIDUAL_ACCEPTED"
                    accepted_validation = selected_validation
                else:
                    accepted_ref = zero_ref
                    gate_status = "FALLBACK_ZERO_RESIDUAL_LCB_NOT_POSITIVE"
                    accepted_validation = zero_validation
        per_loadout_admission.append(
            {
                "loadout_id": loadout_id,
                "selected_training_candidate_ref": selected_ref,
                "zero_residual_ref": zero_ref,
                "training_mean_paired_damage_delta": selected_delta,
                "status": gate_status,
                "accepted_program_ref": accepted_ref,
                "accepted_is_zero_residual": accepted_ref == zero_ref,
                "validation_interval": validation_interval,
                "accepted_validation_pair_count": len(accepted_validation),
                "accepted_validation_mean_damage": mean(
                    row.candidate_damage for row in accepted_validation
                ),
            }
        )

    final = sorted(
        per_loadout_admission,
        key=lambda row: (
            -row["accepted_validation_mean_damage"],
            row["loadout_id"],
            row["accepted_program_ref"],
        ),
    )[0]
    selected_loadout = final["loadout_id"]
    selected_ref = final["selected_training_candidate_ref"]
    zero_ref = final["zero_residual_ref"]
    selected_delta = final["training_mean_paired_damage_delta"]
    accepted_ref = final["accepted_program_ref"]
    gate_status = final["status"]
    validation_interval = final["validation_interval"]

    return {
        "schema": SCHEMA,
        "status": gate_status,
        "selected_training_candidate": {
            "loadout_id": selected_loadout,
            "candidate_ref": selected_ref,
            "zero_residual_ref": zero_ref,
            "mean_paired_damage_delta": selected_delta,
        },
        "accepted_program": {
            "loadout_id": selected_loadout,
            "program_ref": accepted_ref,
            "is_zero_residual": accepted_ref == zero_ref,
        },
        "validation_interval": validation_interval,
        "per_loadout_admission": per_loadout_admission,
        "training_ranking": training_summary,
        "contract": {
            "training_objective": "PAIRED_DAMAGE_DELTA_VS_EXACT_CAT_ZERO_RESIDUAL",
            "same_loadout_and_seed_pairing_required": True,
            "explicit_zero_residual_required": True,
            "zero_residual_delta": 0.0,
            "training_and_validation_seeds_disjoint": True,
            "nonzero_acceptance_rule": "VALIDATION_PAIRED_LOWER_BOUND_STRICTLY_GT_ZERO",
            "final_loadout_selection_rule": (
                "MAX_ACCEPTED_VALIDATION_MEAN_ABSOLUTE_DAMAGE"
            ),
            "minimum_validation_pairs": minimum_validation_pairs,
            "confidence_level": confidence_level,
            "future_kill_time_observed": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--minimum-validation-pairs",
        type=int,
        default=DEFAULT_MINIMUM_VALIDATION_PAIRS,
    )
    parser.add_argument(
        "--confidence-level", type=float, default=DEFAULT_CONFIDENCE_LEVEL
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, Mapping) or set(raw) != {
        "training_rows",
        "validation_rows",
    }:
        parser.error("input must contain exactly training_rows and validation_rows")
    result = select_paired_cat_residual_v1(
        training_rows=tuple(
            paired_cat_damage_from_dict_v1(row) for row in raw["training_rows"]
        ),
        validation_rows=tuple(
            paired_cat_damage_from_dict_v1(row) for row in raw["validation_rows"]
        ),
        minimum_validation_pairs=args.minimum_validation_pairs,
        confidence_level=args.confidence_level,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"], **result["accepted_program"]}))


if __name__ == "__main__":
    main()


__all__ = (
    "DEFAULT_CONFIDENCE_LEVEL",
    "DEFAULT_MINIMUM_VALIDATION_PAIRS",
    "PairedCatDamageV1",
    "SCHEMA",
    "paired_cat_damage_from_dict_v1",
    "paired_cat_damage_rows_from_remote_lanes_v1",
    "exact_cat_zero_residual_ref_v1",
    "exact_paired_zero_program_ref_v1",
    "select_paired_cat_residual_v1",
)
