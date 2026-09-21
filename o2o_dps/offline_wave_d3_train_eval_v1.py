"""Runtime-independent train/select/held-out contract for D3 wave policies.

The contract deliberately does not generate or execute policies.  It freezes
the experimental boundary around those runtime jobs: one program per
candidate, disjoint paired-seed cohorts, equal proposal budgets, complete-wave
selection with no zero imputation, and a held-out pass that cannot reselect the
winner.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import math
import statistics
from typing import Any, Mapping, Sequence

from .offline_wave_searched_program_v1 import (
    PROGRAM_SCHEMA as SEARCHED_PROGRAM_SCHEMA,
    SearchedWaveProgramV1Error,
    searched_wave_behavior_key_v1,
    searched_wave_program_from_dict_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "offline_wave_d3_train_eval/v1"
CAMPAIGN_SCHEMA = f"{SCHEMA}/campaign"
CANDIDATE_SCHEMA = f"{SCHEMA}/candidate"
ROW_SCHEMA = f"{SCHEMA}/full_wave_row"
SELECTION_SCHEMA = f"{SCHEMA}/selection"
HELDOUT_SCHEMA = f"{SCHEMA}/heldout"

PROPOSAL = "proposal"
SELECTION = "selection"
HELDOUT = "heldout"
COHORTS = (PROPOSAL, SELECTION, HELDOUT)

PARENT_RETENTION = "parent_retention"
PLUGIN_ONLY = "plugin_only"
OFFLINE_ONLY = "offline_only"
COMBINED = "combined"
SEARCH_ARMS = (PLUGIN_ONLY, OFFLINE_ONLY, COMBINED)
ALL_ARMS = (PARENT_RETENTION,) + SEARCH_ARMS

_FORBIDDEN_CANDIDATE_KEYS = frozenset(
    {"program_by_seed", "seed_action_sequence"}
)
_DPS_DENOMINATOR = "FULL_WAVE_REPLAY_ELAPSED_TO_ALL_TARGETS_DEAD"


class OfflineWaveD3ContractV1Error(ValueError):
    """A D3 experiment would violate the frozen comparison contract."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OfflineWaveD3ContractV1Error(f"{label} must be nonempty text")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveD3ContractV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _seed_pair(value: object, label: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if set(value) != {"simulator_seed", "teammate_seed"}:
            raise OfflineWaveD3ContractV1Error(
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
        raise OfflineWaveD3ContractV1Error(f"{label} is not a paired seed")
    return (
        _nonnegative_int(simulator, f"{label}.simulator_seed"),
        _nonnegative_int(teammate, f"{label}.teammate_seed"),
    )


def _seed_pairs(values: object, label: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise OfflineWaveD3ContractV1Error(f"{label} must be a sequence")
    pairs = tuple(_seed_pair(value, f"{label}[{index}]") for index, value in enumerate(values))
    if not pairs:
        raise OfflineWaveD3ContractV1Error(f"{label} must not be empty")
    if len(pairs) != len(set(pairs)):
        raise OfflineWaveD3ContractV1Error(f"{label} contains duplicate pairs")
    return pairs


def _pairs_to_rows(pairs: Sequence[tuple[int, int]]) -> list[JSONMap]:
    return [
        {"simulator_seed": simulator, "teammate_seed": teammate}
        for simulator, teammate in pairs
    ]


def _require_disjoint_cohorts(
    cohorts: Mapping[str, Sequence[tuple[int, int]]],
) -> None:
    for index, left_name in enumerate(COHORTS):
        left = cohorts[left_name]
        for right_name in COHORTS[index + 1 :]:
            right = cohorts[right_name]
            if set(left) & set(right):
                raise OfflineWaveD3ContractV1Error(
                    f"{left_name} and {right_name} paired seeds overlap"
                )
            if {row[0] for row in left} & {row[0] for row in right}:
                raise OfflineWaveD3ContractV1Error(
                    f"{left_name} and {right_name} simulator seeds overlap"
                )
            if {row[1] for row in left} & {row[1] for row in right}:
                raise OfflineWaveD3ContractV1Error(
                    f"{left_name} and {right_name} teammate seeds overlap"
                )


def build_d3_campaign_contract_v1(
    *,
    campaign_id: str,
    proposal_seed_pairs: Sequence[object],
    selection_seed_pairs: Sequence[object],
    heldout_seed_pairs: Sequence[object],
    proposal_budget_by_arm: Mapping[str, int],
) -> JSONMap:
    """Freeze disjoint paired seeds and equal non-parent proposal budgets."""

    cohorts = {
        PROPOSAL: _seed_pairs(proposal_seed_pairs, "proposal_seed_pairs"),
        SELECTION: _seed_pairs(selection_seed_pairs, "selection_seed_pairs"),
        HELDOUT: _seed_pairs(heldout_seed_pairs, "heldout_seed_pairs"),
    }
    _require_disjoint_cohorts(cohorts)

    if not isinstance(proposal_budget_by_arm, Mapping) or set(
        proposal_budget_by_arm
    ) != set(SEARCH_ARMS):
        raise OfflineWaveD3ContractV1Error(
            "proposal budgets must name plugin_only, offline_only, and combined"
        )
    budgets = {
        arm: _nonnegative_int(proposal_budget_by_arm[arm], f"budget.{arm}")
        for arm in SEARCH_ARMS
    }
    if not all(budgets.values()) or len(set(budgets.values())) != 1:
        raise OfflineWaveD3ContractV1Error(
            "plugin-only, offline-only, and combined proposal budgets must be equal and positive"
        )

    return {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": _text(campaign_id, "campaign_id"),
        "paired_seed_cohorts": {
            name: _pairs_to_rows(cohorts[name]) for name in COHORTS
        },
        "proposal_budget_by_arm": budgets,
        "equal_non_parent_proposal_budgets": True,
        "selection_primary_metric": "MEAN_PAIRED_FULL_WAVE_DPS",
        "selection_tie_break": "BEHAVIOR_KEY_ASC_THEN_CANDIDATE_ID_ASC",
        "failed_or_incomplete_row_imputed_as_zero": False,
        "heldout_can_affect_selection": False,
    }


def _campaign_parts(
    campaign: Mapping[str, Any],
) -> tuple[str, dict[str, tuple[tuple[int, int], ...]], dict[str, int]]:
    if not isinstance(campaign, Mapping) or campaign.get("schema") != CAMPAIGN_SCHEMA:
        raise OfflineWaveD3ContractV1Error("invalid D3 campaign contract")
    campaign_id = _text(campaign.get("campaign_id"), "campaign_id")
    raw_cohorts = campaign.get("paired_seed_cohorts")
    if not isinstance(raw_cohorts, Mapping) or set(raw_cohorts) != set(COHORTS):
        raise OfflineWaveD3ContractV1Error("campaign seed cohorts are invalid")
    cohorts = {
        name: _seed_pairs(raw_cohorts[name], f"campaign.{name}")
        for name in COHORTS
    }
    _require_disjoint_cohorts(cohorts)
    raw_budgets = campaign.get("proposal_budget_by_arm")
    if not isinstance(raw_budgets, Mapping) or set(raw_budgets) != set(SEARCH_ARMS):
        raise OfflineWaveD3ContractV1Error("campaign proposal budgets are invalid")
    budgets = {
        arm: _nonnegative_int(raw_budgets[arm], f"campaign.budget.{arm}")
        for arm in SEARCH_ARMS
    }
    if not all(budgets.values()) or len(set(budgets.values())) != 1:
        raise OfflineWaveD3ContractV1Error("campaign proposal budgets are not equal")
    return campaign_id, cohorts, budgets


def _forbidden_paths(value: object, prefix: str = "candidate") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if key in _FORBIDDEN_CANDIDATE_KEYS:
                found.append(path)
            found.extend(_forbidden_paths(child, path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{prefix}[{index}]"))
    return found


def validate_d3_candidate_manifests_v1(
    candidates: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    """Validate candidates as seed-invariant programs from all four arms."""

    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise OfflineWaveD3ContractV1Error("candidates must be a sequence")
    copied: list[JSONMap] = []
    for index, value in enumerate(candidates):
        if not isinstance(value, Mapping):
            raise OfflineWaveD3ContractV1Error(f"candidate[{index}] is not a mapping")
        row = deepcopy(dict(value))
        if row.get("schema") != CANDIDATE_SCHEMA:
            raise OfflineWaveD3ContractV1Error(
                f"candidate[{index}] has the wrong schema"
            )
        _text(row.get("candidate_id"), f"candidate[{index}].candidate_id")
        _text(row.get("behavior_key"), f"candidate[{index}].behavior_key")
        arm = row.get("proposal_arm")
        if arm not in ALL_ARMS:
            raise OfflineWaveD3ContractV1Error(
                f"candidate[{index}] has an unsupported proposal_arm"
            )
        if not isinstance(row.get("program"), Mapping):
            raise OfflineWaveD3ContractV1Error(
                f"candidate[{index}].program must be a mapping"
            )
        forbidden = _forbidden_paths(row)
        if forbidden:
            raise OfflineWaveD3ContractV1Error(
                "seed-specific candidate programs are forbidden: "
                + ", ".join(forbidden)
            )
        copied.append(row)

    ids = [row["candidate_id"] for row in copied]
    if len(ids) != len(set(ids)):
        raise OfflineWaveD3ContractV1Error("candidate_id values must be unique")
    arm_counts = {arm: sum(row["proposal_arm"] == arm for row in copied) for arm in ALL_ARMS}
    if arm_counts[PARENT_RETENTION] != 1:
        raise OfflineWaveD3ContractV1Error(
            "exactly one parent-retention candidate is required"
        )
    missing = [arm for arm in SEARCH_ARMS if arm_counts[arm] == 0]
    if missing:
        raise OfflineWaveD3ContractV1Error(
            "candidate panel is missing search arms: " + ", ".join(missing)
        )

    generic_program_by_behavior_key: dict[str, str] = {}
    behavior_key_by_generic_program: dict[str, str] = {}
    for row in copied:
        behavior_key = row["behavior_key"]
        if row["program"].get("schema") == SEARCHED_PROGRAM_SCHEMA:
            try:
                program = searched_wave_program_from_dict_v1(row["program"])
            except (SearchedWaveProgramV1Error, TypeError, ValueError) as error:
                raise OfflineWaveD3ContractV1Error(
                    f"candidate {row['candidate_id']!r} searched program is invalid"
                ) from error
            if behavior_key != searched_wave_behavior_key_v1(program):
                raise OfflineWaveD3ContractV1Error(
                    "searched-program behavior_key differs from its provenance-free behavior projection"
                )
            continue

        try:
            canonical_program = json.dumps(
                row["program"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise OfflineWaveD3ContractV1Error(
                f"candidate {row['candidate_id']!r} program is not JSON-serializable"
            ) from error
        prior_program = generic_program_by_behavior_key.setdefault(
            behavior_key, canonical_program
        )
        if prior_program != canonical_program:
            raise OfflineWaveD3ContractV1Error(
                "one behavior_key must not name different executable programs"
            )
        prior_key = behavior_key_by_generic_program.setdefault(
            canonical_program, behavior_key
        )
        if prior_key != behavior_key:
            raise OfflineWaveD3ContractV1Error(
                "identical executable programs must share one behavior_key"
            )
    return copied


def build_d3_full_wave_row_v1(
    *,
    candidate_id: str,
    cohort: str,
    simulator_seed: int,
    teammate_seed: int,
    replay_status: str,
    score_status: str,
    required_targets_dead: bool,
    effective_damage: float | None,
    dps: float | None,
    completion_time_ms: int | None,
    failure_reason: str | None = None,
) -> JSONMap:
    """Build one full-wave result while enforcing D2 null/zero discipline."""

    if cohort not in COHORTS:
        raise OfflineWaveD3ContractV1Error(f"unsupported cohort {cohort!r}")
    candidate_id = _text(candidate_id, "candidate_id")
    replay_status = _text(replay_status, "replay_status")
    score_status = _text(score_status, "score_status")
    simulator_seed = _nonnegative_int(simulator_seed, "simulator_seed")
    teammate_seed = _nonnegative_int(teammate_seed, "teammate_seed")
    if type(required_targets_dead) is not bool:
        raise OfflineWaveD3ContractV1Error(
            "required_targets_dead must be a boolean"
        )

    completed = score_status == "COMPLETED"
    metrics = (effective_damage, dps, completion_time_ms)
    if completed:
        if replay_status != "COMPLETE" or required_targets_dead is not True:
            raise OfflineWaveD3ContractV1Error(
                "a completed row requires COMPLETE replay and every target dead"
            )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in metrics
        ):
            raise OfflineWaveD3ContractV1Error(
                "a completed row requires numeric damage, DPS, and elapsed time"
            )
        if float(effective_damage) < 0 or float(dps) < 0:
            raise OfflineWaveD3ContractV1Error("completed metrics must be nonnegative")
        if type(completion_time_ms) is not int or completion_time_ms <= 0:
            raise OfflineWaveD3ContractV1Error(
                "completion_time_ms must be a positive integer"
            )
        expected_dps = float(effective_damage) * 1000.0 / completion_time_ms
        if not math.isclose(float(dps), expected_dps, rel_tol=1e-9, abs_tol=1e-9):
            raise OfflineWaveD3ContractV1Error(
                "DPS must equal effective damage over the full-wave elapsed time"
            )
        if failure_reason is not None:
            raise OfflineWaveD3ContractV1Error(
                "a completed row must not contain a failure reason"
            )
    else:
        if any(value is not None for value in metrics):
            raise OfflineWaveD3ContractV1Error(
                "failed or incomplete rows must keep damage, DPS, and time null"
            )
        _text(failure_reason, "failure_reason")

    return {
        "schema": ROW_SCHEMA,
        "candidate_id": candidate_id,
        "cohort": cohort,
        "simulator_seed": simulator_seed,
        "teammate_seed": teammate_seed,
        "replay_status": replay_status,
        "score_status": score_status,
        "failure_reason": failure_reason,
        "required_targets_dead": required_targets_dead,
        "effective_damage": float(effective_damage) if completed else None,
        "dps": float(dps) if completed else None,
        "completion_time_ms": completion_time_ms if completed else None,
        "dps_denominator": {
            "kind": _DPS_DENOMINATOR,
            "elapsed_ms": completion_time_ms if completed else None,
            "includes_wait_and_non_damage_actions": True,
        },
        "selection_eligible": completed,
        "failed_or_incomplete_row_imputed_as_zero": False,
    }


def _validate_row(row: Mapping[str, Any]) -> JSONMap:
    if not isinstance(row, Mapping) or row.get("schema") != ROW_SCHEMA:
        raise OfflineWaveD3ContractV1Error("evaluation row has the wrong schema")
    rebuilt = build_d3_full_wave_row_v1(
        candidate_id=row.get("candidate_id"),
        cohort=row.get("cohort"),
        simulator_seed=row.get("simulator_seed"),
        teammate_seed=row.get("teammate_seed"),
        replay_status=row.get("replay_status"),
        score_status=row.get("score_status"),
        required_targets_dead=row.get("required_targets_dead"),
        effective_damage=row.get("effective_damage"),
        dps=row.get("dps"),
        completion_time_ms=row.get("completion_time_ms"),
        failure_reason=row.get("failure_reason"),
    )
    for field in (
        "dps_denominator",
        "selection_eligible",
        "failed_or_incomplete_row_imputed_as_zero",
    ):
        if row.get(field) != rebuilt[field]:
            raise OfflineWaveD3ContractV1Error(
                f"evaluation row has invalid {field}"
            )
    return deepcopy(dict(row))


def _proposal_accounting(
    completed: Mapping[str, int], budgets: Mapping[str, int]
) -> dict[str, int]:
    if not isinstance(completed, Mapping) or set(completed) != set(SEARCH_ARMS):
        raise OfflineWaveD3ContractV1Error(
            "completed proposal trials must name all three search arms"
        )
    values = {
        arm: _nonnegative_int(completed[arm], f"completed_trials.{arm}")
        for arm in SEARCH_ARMS
    }
    if values != dict(budgets):
        raise OfflineWaveD3ContractV1Error(
            "selection is forbidden until every equal proposal budget is consumed"
        )
    return values


def _candidate_summaries(
    candidates: Sequence[JSONMap],
    rows: Sequence[JSONMap],
    required_pairs: Sequence[tuple[int, int]],
) -> list[JSONMap]:
    expected = set(required_pairs)
    by_candidate: dict[str, list[JSONMap]] = defaultdict(list)
    candidate_ids = {row["candidate_id"] for row in candidates}
    for row in rows:
        if row["candidate_id"] not in candidate_ids:
            raise OfflineWaveD3ContractV1Error(
                f"evaluation names unknown candidate {row['candidate_id']!r}"
            )
        pair = (row["simulator_seed"], row["teammate_seed"])
        if pair not in expected:
            raise OfflineWaveD3ContractV1Error(
                "evaluation row is outside the frozen cohort"
            )
        by_candidate[row["candidate_id"]].append(row)

    summaries: list[JSONMap] = []
    for candidate in candidates:
        candidate_rows = by_candidate[candidate["candidate_id"]]
        observed = [
            (row["simulator_seed"], row["teammate_seed"])
            for row in candidate_rows
        ]
        reasons: list[str] = []
        if len(observed) != len(set(observed)):
            reasons.append("DUPLICATE_SEED_PAIR")
        missing = sorted(expected - set(observed))
        if missing:
            reasons.append("MISSING_REQUIRED_SEED_PAIRS")
        ineligible = [
            row for row in candidate_rows if row["selection_eligible"] is not True
        ]
        if ineligible:
            reasons.append("FAILED_OR_INCOMPLETE_FULL_WAVE_ROW")
        eligible = not reasons and set(observed) == expected
        summaries.append(
            {
                "candidate_id": candidate["candidate_id"],
                "behavior_key": candidate["behavior_key"],
                "proposal_arm": candidate["proposal_arm"],
                "selection_eligible": eligible,
                "ineligible_reasons": reasons,
                "required_seed_pair_count": len(expected),
                "observed_row_count": len(candidate_rows),
                "mean_paired_dps": (
                    float(statistics.fmean(row["dps"] for row in candidate_rows))
                    if eligible
                    else None
                ),
                "mean_paired_effective_damage": (
                    float(
                        statistics.fmean(
                            row["effective_damage"] for row in candidate_rows
                        )
                    )
                    if eligible
                    else None
                ),
                "failed_or_incomplete_row_imputed_as_zero": False,
            }
        )
    return summaries


def _offline_contribution(
    winner: JSONMap, summaries: Sequence[JSONMap]
) -> JSONMap:
    nonoffline = [
        row
        for row in summaries
        if row["selection_eligible"]
        and row["proposal_arm"] in {PARENT_RETENTION, PLUGIN_ONLY}
    ]
    selected_behavior_rows = [
        row
        for row in summaries
        if row["selection_eligible"]
        and row["behavior_key"] == winner["behavior_key"]
    ]
    selected_offline = any(
        row["proposal_arm"] in {OFFLINE_ONLY, COMBINED}
        for row in selected_behavior_rows
    )
    duplicate_behavior = any(
        row["proposal_arm"] in {PARENT_RETENTION, PLUGIN_ONLY}
        for row in selected_behavior_rows
    )
    best_nonoffline = max(
        (float(row["mean_paired_dps"]) for row in nonoffline), default=None
    )
    strict_gain = (
        best_nonoffline is not None
        and float(winner["mean_paired_dps"]) > best_nonoffline
    )
    if not selected_offline:
        status = "NOT_COUNTED_SELECTED_NON_OFFLINE_ARM"
    elif duplicate_behavior:
        status = "NOT_COUNTED_PROVENANCE_ONLY_BEHAVIOR"
    elif not strict_gain:
        status = "NOT_COUNTED_NO_STRICT_SELECTION_GAIN"
    else:
        status = "COUNTED_DISTINCT_BEHAVIOR_WITH_STRICT_SELECTION_GAIN"
    return {
        "status": status,
        "counted": status.startswith("COUNTED_"),
        "selected_from_offline_enabled_arm": selected_offline,
        "selected_behavior_distinct_from_nonoffline_arms": not duplicate_behavior,
        "strict_mean_paired_dps_gain_vs_best_nonoffline": strict_gain,
        "best_nonoffline_mean_paired_dps": best_nonoffline,
        "provenance_alone_counts_as_contribution": False,
    }


def _deduplicate_eligible_behaviors(
    summaries: Sequence[JSONMap],
) -> tuple[list[JSONMap], list[JSONMap]]:
    """Return one deterministic representative per executable behavior."""

    arm_order = {
        PARENT_RETENTION: 0,
        PLUGIN_ONLY: 1,
        OFFLINE_ONLY: 2,
        COMBINED: 3,
    }
    grouped: dict[str, list[JSONMap]] = defaultdict(list)
    for row in summaries:
        if row["selection_eligible"]:
            grouped[row["behavior_key"]].append(row)

    representatives: list[JSONMap] = []
    groups: list[JSONMap] = []
    for behavior_key, members in sorted(grouped.items()):
        dps_values = [float(row["mean_paired_dps"]) for row in members]
        damage_values = [
            float(row["mean_paired_effective_damage"]) for row in members
        ]
        if not all(math.isclose(value, dps_values[0]) for value in dps_values[1:]):
            raise OfflineWaveD3ContractV1Error(
                "one behavior_key produced different paired DPS values"
            )
        if not all(
            math.isclose(value, damage_values[0]) for value in damage_values[1:]
        ):
            raise OfflineWaveD3ContractV1Error(
                "one behavior_key produced different paired damage values"
            )
        representative = min(
            members,
            key=lambda row: (arm_order[row["proposal_arm"]], row["candidate_id"]),
        )
        representative = deepcopy(representative)
        representative["equivalent_candidate_ids"] = sorted(
            row["candidate_id"] for row in members
        )
        representative["equivalent_proposal_arms"] = sorted(
            {row["proposal_arm"] for row in members}
        )
        representatives.append(representative)
        groups.append(
            {
                "behavior_key": behavior_key,
                "representative_candidate_id": representative["candidate_id"],
                "candidate_ids": representative["equivalent_candidate_ids"],
                "proposal_arms": representative["equivalent_proposal_arms"],
                "candidate_count": len(members),
                "ranking_entries_after_deduplication": 1,
            }
        )
    return representatives, groups


def select_d3_frozen_candidate_v1(
    *,
    campaign: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    selection_rows: Sequence[Mapping[str, Any]],
    completed_proposal_trials_by_arm: Mapping[str, int],
) -> JSONMap:
    """Select once on selection rows; held-out rows are not accepted here."""

    campaign_id, cohorts, budgets = _campaign_parts(campaign)
    manifests = validate_d3_candidate_manifests_v1(candidates)
    completed = _proposal_accounting(completed_proposal_trials_by_arm, budgets)
    rows = [_validate_row(row) for row in selection_rows]
    if any(row["cohort"] != SELECTION for row in rows):
        raise OfflineWaveD3ContractV1Error(
            "selection accepts selection-cohort rows only; held-out data cannot select"
        )
    summaries = _candidate_summaries(manifests, rows, cohorts[SELECTION])
    parent = next(
        row for row in summaries if row["proposal_arm"] == PARENT_RETENTION
    )
    eligible, behavior_groups = _deduplicate_eligible_behaviors(summaries)
    if not parent["selection_eligible"] or not eligible:
        return {
            "schema": SELECTION_SCHEMA,
            "campaign_id": campaign_id,
            "status": "NO_FROZEN_CANDIDATE_PARENT_RETENTION_INELIGIBLE",
            "frozen_candidate": None,
            "candidate_summaries": summaries,
            "behavior_groups": behavior_groups,
            "completed_proposal_trials_by_arm": completed,
            "heldout_rows_consumed": 0,
            "heldout_can_affect_selection": False,
            "failed_or_incomplete_row_imputed_as_zero": False,
        }

    ranked = sorted(
        eligible,
        key=lambda row: (
            -float(row["mean_paired_dps"]),
            row["behavior_key"],
            row["candidate_id"],
        ),
    )
    winner = ranked[0]
    manifest = next(
        row for row in manifests if row["candidate_id"] == winner["candidate_id"]
    )
    return {
        "schema": SELECTION_SCHEMA,
        "campaign_id": campaign_id,
        "status": "ONE_CANDIDATE_FROZEN_FOR_HELDOUT",
        "frozen_candidate": deepcopy(manifest),
        "selection_score": deepcopy(winner),
        "parent_retention_score": deepcopy(parent),
        "candidate_summaries": summaries,
        "behavior_groups": behavior_groups,
        "eligible_distinct_behavior_count": len(eligible),
        "ranking_rule": {
            "primary": "MEAN_PAIRED_FULL_WAVE_DPS_DESC",
            "tie_break": "BEHAVIOR_KEY_ASC_THEN_CANDIDATE_ID_ASC",
        },
        "completed_proposal_trials_by_arm": completed,
        "offline_contribution_on_selection": _offline_contribution(
            winner, summaries
        ),
        "heldout_rows_consumed": 0,
        "heldout_can_affect_selection": False,
        "failed_or_incomplete_row_imputed_as_zero": False,
    }


def evaluate_d3_frozen_candidate_heldout_v1(
    *,
    campaign: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    heldout_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Evaluate the already frozen winner without reopening selection."""

    campaign_id, cohorts, _ = _campaign_parts(campaign)
    if (
        not isinstance(selection_receipt, Mapping)
        or selection_receipt.get("schema") != SELECTION_SCHEMA
        or selection_receipt.get("campaign_id") != campaign_id
        or selection_receipt.get("status") != "ONE_CANDIDATE_FROZEN_FOR_HELDOUT"
    ):
        raise OfflineWaveD3ContractV1Error(
            "held-out evaluation requires a successful selection receipt"
        )
    frozen = selection_receipt.get("frozen_candidate")
    if not isinstance(frozen, Mapping):
        raise OfflineWaveD3ContractV1Error("selection receipt lacks a frozen candidate")
    rows = [_validate_row(row) for row in heldout_rows]
    if any(row["cohort"] != HELDOUT for row in rows):
        raise OfflineWaveD3ContractV1Error("held-out evaluation requires heldout rows")
    if any(row["candidate_id"] != frozen.get("candidate_id") for row in rows):
        raise OfflineWaveD3ContractV1Error(
            "held-out rows may evaluate only the already frozen candidate"
        )
    summary = _candidate_summaries(
        [deepcopy(dict(frozen))], rows, cohorts[HELDOUT]
    )[0]
    return {
        "schema": HELDOUT_SCHEMA,
        "campaign_id": campaign_id,
        "status": (
            "FROZEN_CANDIDATE_HELDOUT_COMPLETE"
            if summary["selection_eligible"]
            else "FROZEN_CANDIDATE_HELDOUT_INCOMPLETE"
        ),
        "frozen_candidate": deepcopy(dict(frozen)),
        "heldout_score": summary,
        "selection_reopened": False,
        "heldout_can_affect_selection": False,
        "failed_or_incomplete_row_imputed_as_zero": False,
    }


__all__ = (
    "ALL_ARMS",
    "CAMPAIGN_SCHEMA",
    "CANDIDATE_SCHEMA",
    "COMBINED",
    "HELDOUT",
    "HELDOUT_SCHEMA",
    "OFFLINE_ONLY",
    "OfflineWaveD3ContractV1Error",
    "PARENT_RETENTION",
    "PLUGIN_ONLY",
    "PROPOSAL",
    "ROW_SCHEMA",
    "SCHEMA",
    "SEARCH_ARMS",
    "SELECTION",
    "SELECTION_SCHEMA",
    "build_d3_campaign_contract_v1",
    "build_d3_full_wave_row_v1",
    "evaluate_d3_frozen_candidate_heldout_v1",
    "select_d3_frozen_candidate_v1",
    "validate_d3_candidate_manifests_v1",
)
