"""Parent-relative proposal ranking and independent selection gate for V9.

Every row compares one append candidate with the frozen V8 parent on the same
loadout and simulator seed.  Proposal rows choose at most one challenger.
Independent selection rows admit that challenger only when the paired damage
confidence interval has a strictly positive lower endpoint.  Otherwise the
frozen parent remains the selected program.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import NormalDist, mean, stdev
from typing import Any, Mapping, Sequence

from .upper_kara_cat_action_plan_append_contract_v9 import (
    FrozenV8ParentRefV9,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_append_selection/v9"
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_MINIMUM_SELECTION_PAIRS = 32


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be finite numeric data")
    return float(value)


@dataclass(frozen=True)
class PairedParentDamageV9:
    cohort: str
    loadout_id: str
    candidate_ref: str
    paired_parent_ref: str
    seed: int
    simulator_seed: int
    candidate_damage: float
    parent_damage: float

    def __post_init__(self) -> None:
        if self.cohort not in {"PROPOSAL", "SELECTION"}:
            raise ValueError("cohort must be PROPOSAL or SELECTION")
        for field in ("loadout_id", "candidate_ref", "paired_parent_ref"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(self, "seed", _integer(self.seed, "seed"))
        object.__setattr__(
            self,
            "simulator_seed",
            _integer(self.simulator_seed, "simulator_seed"),
        )
        object.__setattr__(
            self,
            "candidate_damage",
            _finite(self.candidate_damage, "candidate_damage"),
        )
        object.__setattr__(
            self,
            "parent_damage",
            _finite(self.parent_damage, "parent_damage"),
        )
        if (
            self.candidate_ref == self.paired_parent_ref
            and self.candidate_damage != self.parent_damage
        ):
            raise ValueError("the explicit parent row must have exact delta=0")

    @property
    def damage_delta(self) -> float:
        return self.candidate_damage - self.parent_damage

    def to_dict(self) -> JSONMap:
        return {
            "cohort": self.cohort,
            "loadout_id": self.loadout_id,
            "candidate_ref": self.candidate_ref,
            "paired_parent_ref": self.paired_parent_ref,
            "seed": self.seed,
            "simulator_seed": self.simulator_seed,
            "candidate_damage": self.candidate_damage,
            "parent_damage": self.parent_damage,
        }


def paired_parent_damage_from_dict_v9(value: object) -> PairedParentDamageV9:
    if not isinstance(value, Mapping):
        raise ValueError("paired parent damage row must be an object")
    fields = {
        "cohort",
        "loadout_id",
        "candidate_ref",
        "paired_parent_ref",
        "seed",
        "simulator_seed",
        "candidate_damage",
        "parent_damage",
    }
    if set(value) != fields:
        raise ValueError("paired parent damage row fields differ from V9")
    return PairedParentDamageV9(**dict(value))


def paired_parent_damage_rows_from_remote_lanes_v9(
    *,
    cohort: str,
    parent: FrozenV8ParentRefV9,
    candidate_refs: Sequence[str],
    lanes: Sequence[Mapping[str, Any]],
) -> tuple[PairedParentDamageV9, ...]:
    """Project complete remote lanes into same-seed candidate-parent pairs."""

    if not isinstance(parent, FrozenV8ParentRefV9):
        raise TypeError("parent must be FrozenV8ParentRefV9")
    refs = tuple(_text(value, "candidate_ref") for value in candidate_refs)
    if len(refs) != len(set(refs)) or parent.program_ref not in refs:
        raise ValueError(
            "candidate_refs must be unique and include the paired parent"
        )
    by_ref_seed: dict[tuple[str, int], Mapping[str, Any]] = {}
    seed_sets: dict[str, set[int]] = {ref: set() for ref in refs}
    for lane in lanes:
        if not isinstance(lane, Mapping):
            raise ValueError("remote lane must be an object")
        ref = lane.get("program_ref")
        if ref not in seed_sets:
            continue
        seed = _integer(lane.get("master_seed"), "remote lane master_seed")
        simulator_seed = _integer(
            lane.get("simulator_seed"), "remote lane simulator_seed"
        )
        key = (ref, seed)
        if key in by_ref_seed:
            raise ValueError("remote lanes contain a duplicate program/seed")
        if lane.get("status") != "COMPLETE":
            raise ValueError("paired candidate lane is not COMPLETE")
        _finite(lane.get("own_effective_damage"), "own_effective_damage")
        by_ref_seed[key] = lane
        seed_sets[ref].add(seed)
    parent_seeds = seed_sets[parent.program_ref]
    if not parent_seeds or any(seeds != parent_seeds for seeds in seed_sets.values()):
        raise ValueError("remote candidates do not share parent seed coverage")

    rows: list[PairedParentDamageV9] = []
    for ref in refs:
        for seed in sorted(parent_seeds):
            candidate = by_ref_seed[(ref, seed)]
            parent_lane = by_ref_seed[(parent.program_ref, seed)]
            if candidate["simulator_seed"] != parent_lane["simulator_seed"]:
                raise ValueError("candidate and parent use different simulator seeds")
            rows.append(
                PairedParentDamageV9(
                    cohort=cohort,
                    loadout_id=parent.loadout_id,
                    candidate_ref=ref,
                    paired_parent_ref=parent.program_ref,
                    seed=seed,
                    simulator_seed=candidate["simulator_seed"],
                    candidate_damage=candidate["own_effective_damage"],
                    parent_damage=parent_lane["own_effective_damage"],
                )
            )
    return tuple(rows)


def _index_cohort_v9(
    rows: Sequence[PairedParentDamageV9], cohort: str
) -> dict[str, tuple[PairedParentDamageV9, ...]]:
    if not rows:
        raise ValueError(f"{cohort.lower()} rows must be nonempty")
    grouped: dict[str, list[PairedParentDamageV9]] = {}
    observed: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, PairedParentDamageV9):
            raise TypeError("rows must contain PairedParentDamageV9")
        if row.cohort != cohort:
            raise ValueError(f"{cohort.lower()} rows contain another cohort")
        key = (row.candidate_ref, row.seed)
        if key in observed:
            raise ValueError("paired cohort contains a duplicate candidate/seed")
        observed.add(key)
        grouped.setdefault(row.candidate_ref, []).append(row)
    return {
        ref: tuple(sorted(values, key=lambda row: row.seed))
        for ref, values in grouped.items()
    }


def _validate_cohort_v9(
    grouped: Mapping[str, tuple[PairedParentDamageV9, ...]],
    *,
    cohort: str,
) -> tuple[str, str, frozenset[int]]:
    rows = tuple(row for values in grouped.values() for row in values)
    loadouts = {row.loadout_id for row in rows}
    parent_refs = {row.paired_parent_ref for row in rows}
    if len(loadouts) != 1 or len(parent_refs) != 1:
        raise ValueError(f"{cohort} must use exactly one loadout and parent")
    loadout_id = next(iter(loadouts))
    parent_ref = next(iter(parent_refs))
    if parent_ref not in grouped:
        raise ValueError(f"{cohort} lacks explicit parent rows")
    seed_sets = {frozenset(row.seed for row in values) for values in grouped.values()}
    if len(seed_sets) != 1:
        raise ValueError(f"{cohort} candidates are not paired on identical seeds")
    seed_set = next(iter(seed_sets))
    simulator_by_seed: dict[int, int] = {}
    for row in rows:
        previous = simulator_by_seed.setdefault(row.seed, row.simulator_seed)
        if previous != row.simulator_seed:
            raise ValueError(f"{cohort} pair uses different simulator seeds")
    parent_rows = grouped[parent_ref]
    if any(row.damage_delta != 0.0 for row in parent_rows):
        raise ValueError(f"{cohort} parent rows must have exact delta=0")
    parent_damage_by_seed = {
        row.seed: row.candidate_damage for row in parent_rows
    }
    if any(
        row.parent_damage != parent_damage_by_seed[row.seed]
        for row in rows
    ):
        raise ValueError(
            f"{cohort} candidate rows do not share the explicit parent damage"
        )
    return loadout_id, parent_ref, seed_set


def _normal_paired_interval_v9(
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
        "mean_candidate_minus_parent_damage": center,
        "sample_standard_deviation": standard_deviation,
        "standard_error": standard_error,
        "critical_value": critical,
        "lower_bound": center - half_width,
        "upper_bound": center + half_width,
    }


def select_paired_parent_append_v9(
    *,
    proposal_rows: Sequence[PairedParentDamageV9],
    selection_rows: Sequence[PairedParentDamageV9],
    minimum_selection_pairs: int = DEFAULT_MINIMUM_SELECTION_PAIRS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> JSONMap:
    """Rank on proposal deltas and gate once on disjoint selection pairs."""

    if (
        isinstance(minimum_selection_pairs, bool)
        or not isinstance(minimum_selection_pairs, int)
        or minimum_selection_pairs < 2
    ):
        raise ValueError("minimum_selection_pairs must be an integer >= 2")
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not math.isfinite(float(confidence_level))
        or not 0.5 < float(confidence_level) < 1.0
    ):
        raise ValueError("confidence_level must be between 0.5 and 1")
    confidence_level = float(confidence_level)
    proposal = _index_cohort_v9(proposal_rows, "PROPOSAL")
    selection = _index_cohort_v9(selection_rows, "SELECTION")
    proposal_loadout, proposal_parent, proposal_seeds = _validate_cohort_v9(
        proposal, cohort="PROPOSAL"
    )
    selection_loadout, selection_parent, selection_seeds = _validate_cohort_v9(
        selection, cohort="SELECTION"
    )
    if (
        proposal_loadout != selection_loadout
        or proposal_parent != selection_parent
    ):
        raise ValueError("proposal and selection parent identity differs")
    overlap = proposal_seeds & selection_seeds
    if overlap:
        raise ValueError(
            "proposal and selection seeds overlap: " + str(sorted(overlap))
        )
    if not set(selection).issubset(proposal):
        raise ValueError("selection contains a candidate absent from proposal")

    ranking: list[JSONMap] = []
    for candidate_ref, rows in sorted(proposal.items()):
        ranking.append(
            {
                "loadout_id": proposal_loadout,
                "candidate_ref": candidate_ref,
                "paired_parent_ref": proposal_parent,
                "is_parent_unchanged": candidate_ref == proposal_parent,
                "pair_count": len(rows),
                "mean_candidate_damage": mean(
                    row.candidate_damage for row in rows
                ),
                "mean_parent_damage": mean(row.parent_damage for row in rows),
                "mean_candidate_minus_parent_damage": mean(
                    row.damage_delta for row in rows
                ),
            }
        )
    selected = sorted(
        ranking,
        key=lambda row: (
            -row["mean_candidate_minus_parent_damage"],
            row["candidate_ref"] != proposal_parent,
            row["candidate_ref"],
        ),
    )[0]
    selected_ref = selected["candidate_ref"]

    if selected_ref == proposal_parent:
        interval = None
        accepted_ref = proposal_parent
        status = "PARENT_RETAINED_ON_PROPOSAL_RANKING"
        accepted_selection_rows = selection[proposal_parent]
    else:
        if selected_ref not in selection:
            raise ValueError("selection lacks the proposal-ranked challenger")
        challenger_rows = selection[selected_ref]
        if len(challenger_rows) < minimum_selection_pairs:
            interval = None
            accepted_ref = proposal_parent
            status = "PARENT_RETAINED_INSUFFICIENT_SELECTION_PAIRS"
            accepted_selection_rows = selection[proposal_parent]
        else:
            interval = _normal_paired_interval_v9(
                [row.damage_delta for row in challenger_rows],
                confidence_level,
            )
            if interval["lower_bound"] > 0.0:
                accepted_ref = selected_ref
                status = "APPEND_ACCEPTED_SELECTION_LCB_POSITIVE"
                accepted_selection_rows = challenger_rows
            else:
                accepted_ref = proposal_parent
                status = "PARENT_RETAINED_SELECTION_LCB_NOT_POSITIVE"
                accepted_selection_rows = selection[proposal_parent]

    return {
        "schema": SCHEMA,
        "status": status,
        "loadout_id": proposal_loadout,
        "paired_parent_ref": proposal_parent,
        "proposal_ranked_candidate": {
            "candidate_ref": selected_ref,
            "is_parent_unchanged": selected_ref == proposal_parent,
            "pair_count": selected["pair_count"],
            "mean_candidate_minus_parent_damage": selected[
                "mean_candidate_minus_parent_damage"
            ],
        },
        "accepted_program": {
            "program_ref": accepted_ref,
            "is_parent_unchanged": accepted_ref == proposal_parent,
        },
        "selection_interval": interval,
        "accepted_selection_pair_count": len(accepted_selection_rows),
        "accepted_selection_mean_damage": mean(
            row.candidate_damage for row in accepted_selection_rows
        ),
        "proposal_ranking": ranking,
        "contract": {
            "proposal_objective": "PAIRED_CANDIDATE_MINUS_PARENT_DAMAGE",
            "selection_objective": "PAIRED_CANDIDATE_MINUS_PARENT_DAMAGE",
            "same_loadout_master_seed_and_simulator_seed_required": True,
            "explicit_parent_rows_required": True,
            "parent_delta": 0.0,
            "proposal_and_selection_seeds_disjoint": True,
            "challenger_acceptance_rule": (
                "SELECTION_PAIRED_LOWER_BOUND_STRICTLY_GT_ZERO"
            ),
            "failed_gate_action": "RETAIN_FROZEN_PARENT",
            "minimum_selection_pairs": minimum_selection_pairs,
            "confidence_level": confidence_level,
            "heldout_outcomes_observed": False,
        },
    }


__all__ = (
    "DEFAULT_CONFIDENCE_LEVEL",
    "DEFAULT_MINIMUM_SELECTION_PAIRS",
    "PairedParentDamageV9",
    "SCHEMA",
    "paired_parent_damage_from_dict_v9",
    "paired_parent_damage_rows_from_remote_lanes_v9",
    "select_paired_parent_append_v9",
)
