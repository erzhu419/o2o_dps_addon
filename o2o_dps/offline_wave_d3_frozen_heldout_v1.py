"""Blind held-out expansion contract for one already-frozen D3 winner.

This module cannot generate candidates or reopen selection.  It accepts the
successful D3 artifact as its immutable source, rejects every seed component
used by that artifact, and scores the frozen program against the four D2
controllers only on complete, fallback-free paired rows.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
import statistics
from typing import Any, Mapping, Sequence

from .offline_wave_d2_panel_v1 import CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D
from .offline_wave_d3_train_eval_v1 import (
    CAMPAIGN_SCHEMA,
    CANDIDATE_SCHEMA,
    COHORTS,
    SELECTION_SCHEMA,
)
from .offline_wave_searched_program_v1 import (
    searched_wave_behavior_key_v1,
    searched_wave_program_from_dict_v1,
)


JSONMap = dict[str, Any]
SOURCE_SCHEMA = "development_offline_wave_policy_d900_d3/v1"
SCHEMA = "offline_wave_d3_frozen_heldout/v1"
CONTRACT_SCHEMA = f"{SCHEMA}/contract"
ROW_SCHEMA = f"{SCHEMA}/row"
RECEIPT_SCHEMA = f"{SCHEMA}/receipt"
PI_STAR = "PI_STAR"
CONTROLLER_IDS = (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D, PI_STAR)
FALLBACK_CAPABLE_CONTROLLER_IDS = frozenset({PI_D, PI_STAR})


class OfflineWaveD3FrozenHeldoutV1Error(ValueError):
    """The frozen-winner blind held-out contract was violated."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _seed_pair(value: object, label: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if set(value) != {"simulator_seed", "teammate_seed"}:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                f"{label} must contain only simulator_seed and teammate_seed"
            )
        simulator = value["simulator_seed"]
        teammate = value["teammate_seed"]
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
    ):
        simulator, teammate = value
    else:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} is not a paired seed"
        )
    return (
        _nonnegative_int(simulator, f"{label}.simulator_seed"),
        _nonnegative_int(teammate, f"{label}.teammate_seed"),
    )


def _seed_pairs(values: object, label: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} must be a sequence"
        )
    pairs = tuple(
        _seed_pair(value, f"{label}[{index}]")
        for index, value in enumerate(values)
    )
    if not pairs:
        raise OfflineWaveD3FrozenHeldoutV1Error(f"{label} must not be empty")
    simulator_seeds = [row[0] for row in pairs]
    teammate_seeds = [row[1] for row in pairs]
    if len(simulator_seeds) != len(set(simulator_seeds)):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} reuses a simulator seed"
        )
    if len(teammate_seeds) != len(set(teammate_seeds)):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{label} reuses a teammate seed"
        )
    return pairs


def _source_seed_pairs(source: Mapping[str, Any]) -> tuple[tuple[int, int], ...]:
    campaign = source.get("campaign")
    if not isinstance(campaign, Mapping) or campaign.get("schema") != CAMPAIGN_SCHEMA:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen D3 source has an invalid campaign"
        )
    cohorts = campaign.get("paired_seed_cohorts")
    if not isinstance(cohorts, Mapping) or set(cohorts) != set(COHORTS):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen D3 source has invalid paired-seed cohorts"
        )
    return tuple(
        pair
        for cohort in COHORTS
        for pair in _seed_pairs(cohorts[cohort], f"source.{cohort}")
    )


def build_frozen_heldout_expansion_contract_v1(
    *,
    source: Mapping[str, Any],
    heldout_seed_pairs: Sequence[object],
    expansion_id: str,
) -> JSONMap:
    """Freeze a blind seed expansion without regenerating or selecting policy."""

    if not isinstance(source, Mapping) or source.get("schema") != SOURCE_SCHEMA:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "input is not a D3 development result"
        )
    if source.get("status") != "D3_FROZEN_WINNER_HELDOUT_COMPLETE":
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "D3 source did not freeze and complete a held-out winner"
        )
    selection = source.get("selection_receipt")
    if (
        not isinstance(selection, Mapping)
        or selection.get("schema") != SELECTION_SCHEMA
        or selection.get("status") != "ONE_CANDIDATE_FROZEN_FOR_HELDOUT"
        or selection.get("heldout_rows_consumed") != 0
    ):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "D3 source has no valid pre-heldout selection receipt"
        )
    frozen = selection.get("frozen_candidate")
    if (
        not isinstance(frozen, Mapping)
        or frozen.get("schema") != CANDIDATE_SCHEMA
        or not isinstance(frozen.get("program"), Mapping)
    ):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "D3 source lacks a frozen searched candidate"
        )
    try:
        program = searched_wave_program_from_dict_v1(frozen["program"])
    except (TypeError, ValueError) as error:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "D3 frozen searched program is invalid"
        ) from error
    if frozen.get("candidate_id") != program.program_id:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen candidate and searched program identities differ"
        )
    if frozen.get("behavior_key") != searched_wave_behavior_key_v1(program):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen candidate behavior key does not match its program"
        )

    prior_pairs = _source_seed_pairs(source)
    new_pairs = _seed_pairs(heldout_seed_pairs, "heldout_seed_pairs")
    prior_simulator_seeds = {row[0] for row in prior_pairs}
    prior_teammate_seeds = {row[1] for row in prior_pairs}
    reused_simulator = sorted(
        row[0] for row in new_pairs if row[0] in prior_simulator_seeds
    )
    reused_teammate = sorted(
        row[1] for row in new_pairs if row[1] in prior_teammate_seeds
    )
    if reused_simulator:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "blind held-out reuses prior simulator seeds: "
            + ", ".join(map(str, reused_simulator))
        )
    if reused_teammate:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "blind held-out reuses prior teammate seeds: "
            + ", ".join(map(str, reused_teammate))
        )

    source_campaign = source["campaign"]
    return {
        "schema": CONTRACT_SCHEMA,
        "expansion_id": _text(expansion_id, "expansion_id"),
        "source_campaign_id": _text(
            source_campaign.get("campaign_id"), "source campaign_id"
        ),
        "frozen_candidate": deepcopy(dict(frozen)),
        "heldout_seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in new_pairs
        ],
        "excluded_prior_seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in prior_pairs
        ],
        "required_controller_ids": list(CONTROLLER_IDS),
        "selection_reopened": False,
        "candidate_regenerated": False,
        "incomplete_imputed_as_zero": False,
        "fallback_required_zero": True,
    }


def _contract_parts(
    contract: Mapping[str, Any],
) -> tuple[str, tuple[tuple[int, int], ...]]:
    if not isinstance(contract, Mapping) or contract.get("schema") != CONTRACT_SCHEMA:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "invalid frozen held-out expansion contract"
        )
    frozen = contract.get("frozen_candidate")
    if not isinstance(frozen, Mapping):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "expansion contract lacks its frozen candidate"
        )
    candidate_id = _text(frozen.get("candidate_id"), "frozen candidate_id")
    pairs = _seed_pairs(contract.get("heldout_seed_pairs"), "heldout_seed_pairs")
    if contract.get("required_controller_ids") != list(CONTROLLER_IDS):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "required controller identities drifted"
        )
    if (
        contract.get("selection_reopened") is not False
        or contract.get("candidate_regenerated") is not False
        or contract.get("incomplete_imputed_as_zero") is not False
        or contract.get("fallback_required_zero") is not True
    ):
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen held-out boundary flags drifted"
        )
    return candidate_id, pairs


def build_frozen_heldout_row_v1(
    *,
    candidate_id: str,
    controller_id: str,
    simulator_seed: int,
    teammate_seed: int,
    replay_status: str,
    score_status: str,
    required_targets_dead: bool,
    effective_damage: float | None,
    dps: float | None,
    completion_time_ms: int | None,
    domain_fallback_calls: int | None,
    failure_reason: str | None = None,
) -> JSONMap:
    """Build one comparison row with strict null and fallback accounting."""

    candidate_id = _text(candidate_id, "candidate_id")
    if controller_id not in CONTROLLER_IDS:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"unsupported controller_id {controller_id!r}"
        )
    simulator_seed = _nonnegative_int(simulator_seed, "simulator_seed")
    teammate_seed = _nonnegative_int(teammate_seed, "teammate_seed")
    replay_status = _text(replay_status, "replay_status")
    score_status = _text(score_status, "score_status")
    if type(required_targets_dead) is not bool:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "required_targets_dead must be a boolean"
        )
    if domain_fallback_calls is not None:
        domain_fallback_calls = _nonnegative_int(
            domain_fallback_calls, "domain_fallback_calls"
        )
    if controller_id in FALLBACK_CAPABLE_CONTROLLER_IDS and domain_fallback_calls is None:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            f"{controller_id} must report domain_fallback_calls"
        )

    completed = score_status == "COMPLETED"
    metrics = (effective_damage, dps, completion_time_ms)
    if completed:
        if replay_status != "COMPLETE" or required_targets_dead is not True:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a completed row requires COMPLETE replay and every target dead"
            )
        if controller_id in FALLBACK_CAPABLE_CONTROLLER_IDS and domain_fallback_calls != 0:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a completed fallback-capable row requires exactly zero fallback calls"
            )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in metrics
        ):
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a completed row requires numeric damage, DPS, and elapsed time"
            )
        if float(effective_damage) < 0 or float(dps) < 0:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "completed metrics must be nonnegative"
            )
        if type(completion_time_ms) is not int or completion_time_ms <= 0:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "completion_time_ms must be a positive integer"
            )
        expected_dps = float(effective_damage) * 1000.0 / completion_time_ms
        if not math.isclose(float(dps), expected_dps, rel_tol=1e-9, abs_tol=1e-9):
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "DPS must equal effective damage over full-wave elapsed time"
            )
        if failure_reason is not None:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a completed row must not contain a failure reason"
            )
    else:
        if any(value is not None for value in metrics):
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "failed or incomplete rows must keep damage, DPS, and time null"
            )
        _text(failure_reason, "failure_reason")

    return {
        "schema": ROW_SCHEMA,
        "candidate_id": candidate_id,
        "controller_id": controller_id,
        "simulator_seed": simulator_seed,
        "teammate_seed": teammate_seed,
        "replay_status": replay_status,
        "score_status": score_status,
        "failure_reason": failure_reason,
        "required_targets_dead": required_targets_dead,
        "effective_damage": float(effective_damage) if completed else None,
        "dps": float(dps) if completed else None,
        "completion_time_ms": completion_time_ms if completed else None,
        "domain_fallback_calls": domain_fallback_calls,
        "paired_score_eligible": completed,
        "failed_or_incomplete_row_imputed_as_zero": False,
    }


def _validate_row(row: Mapping[str, Any]) -> JSONMap:
    if not isinstance(row, Mapping) or row.get("schema") != ROW_SCHEMA:
        raise OfflineWaveD3FrozenHeldoutV1Error(
            "frozen held-out row has the wrong schema"
        )
    rebuilt = build_frozen_heldout_row_v1(
        candidate_id=row.get("candidate_id"),
        controller_id=row.get("controller_id"),
        simulator_seed=row.get("simulator_seed"),
        teammate_seed=row.get("teammate_seed"),
        replay_status=row.get("replay_status"),
        score_status=row.get("score_status"),
        required_targets_dead=row.get("required_targets_dead"),
        effective_damage=row.get("effective_damage"),
        dps=row.get("dps"),
        completion_time_ms=row.get("completion_time_ms"),
        domain_fallback_calls=row.get("domain_fallback_calls"),
        failure_reason=row.get("failure_reason"),
    )
    for field in (
        "paired_score_eligible",
        "failed_or_incomplete_row_imputed_as_zero",
    ):
        if row.get(field) != rebuilt[field]:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                f"frozen held-out row has invalid {field}"
            )
    return deepcopy(dict(row))


def adjudicate_frozen_heldout_expansion_v1(
    *, contract: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> JSONMap:
    """Summarize only fully paired blind seeds; incomplete rows remain null."""

    candidate_id, pairs = _contract_parts(contract)
    expected_pairs = set(pairs)
    validated = [_validate_row(row) for row in rows]
    by_pair: dict[tuple[int, int], list[JSONMap]] = defaultdict(list)
    for row in validated:
        pair = (row["simulator_seed"], row["teammate_seed"])
        if pair not in expected_pairs:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "runtime row is outside the blind held-out cohort"
            )
        if row["candidate_id"] != candidate_id:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "runtime row does not name the frozen candidate"
            )
        by_pair[pair].append(row)

    pair_receipts: list[JSONMap] = []
    valid_pair_rows: list[list[JSONMap]] = []
    for pair in pairs:
        members = by_pair[pair]
        controller_ids = [row["controller_id"] for row in members]
        if len(controller_ids) != len(set(controller_ids)):
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a blind seed pair contains duplicate controller rows"
            )
        missing = sorted(set(CONTROLLER_IDS) - set(controller_ids))
        unexpected = sorted(set(controller_ids) - set(CONTROLLER_IDS))
        if missing or unexpected:
            raise OfflineWaveD3FrozenHeldoutV1Error(
                "a blind seed pair lacks the exact five-controller panel"
            )
        complete = all(row["paired_score_eligible"] for row in members)
        fallback_free = all(
            row["controller_id"] not in FALLBACK_CAPABLE_CONTROLLER_IDS
            or row["domain_fallback_calls"] == 0
            for row in members
        )
        eligible = complete and fallback_free
        indexed = {row["controller_id"]: row for row in members}
        pair_receipts.append(
            {
                "simulator_seed": pair[0],
                "teammate_seed": pair[1],
                "paired_score_eligible": eligible,
                "all_controllers_completed": complete,
                "fallback_free": fallback_free,
                "pi_star_minus_cat_dps": (
                    float(indexed[PI_STAR]["dps"] - indexed[CAT]["dps"])
                    if eligible
                    else None
                ),
                "pi_star_beats_cat": (
                    bool(indexed[PI_STAR]["dps"] > indexed[CAT]["dps"])
                    if eligible
                    else None
                ),
            }
        )
        if eligible:
            valid_pair_rows.append(members)

    aggregates: JSONMap = {}
    for controller_id in CONTROLLER_IDS:
        controller_rows = [
            row
            for members in valid_pair_rows
            for row in members
            if row["controller_id"] == controller_id
        ]
        aggregates[controller_id] = {
            "valid_paired_seed_count": len(controller_rows),
            "mean_dps": (
                float(statistics.fmean(row["dps"] for row in controller_rows))
                if controller_rows
                else None
            ),
            "mean_effective_damage": (
                float(
                    statistics.fmean(
                        row["effective_damage"] for row in controller_rows
                    )
                )
                if controller_rows
                else None
            ),
            "mean_completion_time_ms": (
                float(
                    statistics.fmean(
                        row["completion_time_ms"] for row in controller_rows
                    )
                )
                if controller_rows
                else None
            ),
        }

    pairwise_pi_star_by_baseline: JSONMap = {}
    for baseline_id in CONTROLLER_IDS:
        if baseline_id == PI_STAR:
            continue
        common_deltas: list[float] = []
        pi_star_dps: list[float] = []
        baseline_dps: list[float] = []
        wins = 0
        losses = 0
        ties = 0
        pi_star_only = 0
        baseline_only = 0
        neither = 0
        for pair in pairs:
            indexed = {
                row["controller_id"]: row
                for row in by_pair[pair]
            }
            pi_star_complete = bool(indexed[PI_STAR]["paired_score_eligible"])
            baseline_complete = bool(
                indexed[baseline_id]["paired_score_eligible"]
            )
            if pi_star_complete and baseline_complete:
                pi_value = float(indexed[PI_STAR]["dps"])
                baseline_value = float(indexed[baseline_id]["dps"])
                delta = pi_value - baseline_value
                pi_star_dps.append(pi_value)
                baseline_dps.append(baseline_value)
                common_deltas.append(delta)
                if delta > 0:
                    wins += 1
                elif delta < 0:
                    losses += 1
                else:
                    ties += 1
            elif pi_star_complete:
                pi_star_only += 1
            elif baseline_complete:
                baseline_only += 1
            else:
                neither += 1

        common_count = len(common_deltas)
        mean_pi_star = (
            float(statistics.fmean(pi_star_dps)) if pi_star_dps else None
        )
        mean_baseline = (
            float(statistics.fmean(baseline_dps)) if baseline_dps else None
        )
        pairwise_pi_star_by_baseline[baseline_id] = {
            "requested_pair_count": len(pairs),
            "common_completed_pair_count": common_count,
            "pi_star_only_completed_pair_count": pi_star_only,
            "baseline_only_completed_pair_count": baseline_only,
            "neither_completed_pair_count": neither,
            "pi_star_win_count_on_common_completed": wins,
            "pi_star_loss_count_on_common_completed": losses,
            "tie_count_on_common_completed": ties,
            "mean_pi_star_dps_on_common_completed": mean_pi_star,
            "mean_baseline_dps_on_common_completed": mean_baseline,
            "mean_delta_dps_on_common_completed": (
                float(statistics.fmean(common_deltas))
                if common_deltas
                else None
            ),
            "median_delta_dps_on_common_completed": (
                float(statistics.median(common_deltas))
                if common_deltas
                else None
            ),
            "aggregate_mean_uplift_fraction_on_common_completed": (
                float(mean_pi_star / mean_baseline - 1.0)
                if mean_pi_star is not None
                and mean_baseline is not None
                and mean_baseline != 0.0
                else None
            ),
            "conditions_on_both_controllers_completing": True,
            "confirmatory_interpretation_authorized": False,
            "interpretation_scope": "DESCRIPTIVE_POSTHOC",
        }

    valid_count = len(valid_pair_rows)
    if valid_count == len(pairs):
        status = "FROZEN_WINNER_BLIND_HELDOUT_COMPLETE"
    elif valid_count:
        status = "FROZEN_WINNER_BLIND_HELDOUT_PARTIAL"
    else:
        status = "FROZEN_WINNER_BLIND_HELDOUT_NO_VALID_PAIRS"
    return {
        "schema": RECEIPT_SCHEMA,
        "expansion_id": contract["expansion_id"],
        "status": status,
        "frozen_candidate_id": candidate_id,
        "requested_pair_count": len(pairs),
        "valid_paired_seed_count": valid_count,
        "pair_receipts": pair_receipts,
        "aggregate_on_valid_pairs_only": aggregates,
        "pairwise_pi_star_by_baseline": pairwise_pi_star_by_baseline,
        "pairwise_interpretation_scope": "DESCRIPTIVE_POSTHOC",
        "selection_reopened": False,
        "candidate_regenerated": False,
        "failed_or_incomplete_row_imputed_as_zero": False,
        "fallback_required_zero": True,
    }


__all__ = (
    "CONTROLLER_IDS",
    "CONTRACT_SCHEMA",
    "FALLBACK_CAPABLE_CONTROLLER_IDS",
    "OfflineWaveD3FrozenHeldoutV1Error",
    "PI_STAR",
    "RECEIPT_SCHEMA",
    "ROW_SCHEMA",
    "SCHEMA",
    "adjudicate_frozen_heldout_expansion_v1",
    "build_frozen_heldout_expansion_contract_v1",
    "build_frozen_heldout_row_v1",
)
