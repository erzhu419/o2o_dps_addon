"""Frozen all-seed endpoints for the d900 searched-wave development panel.

The earlier D3 receipt scored only waves where every controller cleared every
target.  This contract retains technically valid terminal evidence for every
seed, including waves which reach the historical horizon with surviving
targets.  It therefore separates four quantities instead of replacing an
incomplete wave with zero DPS:

* clear probability by the fixed horizon;
* required-target residual health at the terminal boundary;
* focal-player effective damage by ``min(clear, horizon)``; and
* clear DPS only when both compared controllers clear.

The old 48-seed expansion informed this endpoint design.  It may be reused for
diagnosis, but a confirmatory contract must use 48 entirely new paired seeds.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
import statistics
from typing import Any, Mapping, Sequence

from .offline_wave_d3_frozen_heldout_v1 import (
    CONTROLLER_IDS,
    FALLBACK_CAPABLE_CONTROLLER_IDS,
    PI_STAR,
    build_frozen_heldout_expansion_contract_v1,
)
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]

SCHEMA = "offline_wave_d4_all_seed_endpoint/v1"
ENDPOINT_SPEC_SCHEMA = f"{SCHEMA}/endpoint_spec"
CONTRACT_SCHEMA = f"{SCHEMA}/contract"
TERMINAL_SCHEMA = f"{SCHEMA}/terminal"
ROW_SCHEMA = f"{SCHEMA}/row"
RECEIPT_SCHEMA = f"{SCHEMA}/receipt"

PRIOR_EXPANSION_SCHEMA = (
    "development_offline_wave_policy_d900_d3_frozen_blind_multinode/v1"
)
DIAGNOSTIC_REUSE_SEEN = "DIAGNOSTIC_REUSE_SEEN_SEEDS"
FRESH_CONFIRMATORY = "FRESH_CONFIRMATORY_SEEDS"
EVIDENCE_MODES = (DIAGNOSTIC_REUSE_SEEN, FRESH_CONFIRMATORY)
CONFIRMATORY_PAIR_COUNT = 48
IMPLEMENTATION_REVISION = "d4_all_seed_terminal_v1"


class OfflineWaveD4AllSeedEndpointV1Error(ValueError):
    """The frozen all-seed endpoint contract or terminal evidence is invalid."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be positive"
        )
    return result


def _number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be a finite number >= {minimum}"
        )
    return float(value)


def _same(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-7)


def _seed_pair(value: object, label: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        simulator = value.get("simulator_seed")
        teammate = value.get("teammate_seed")
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 2
    ):
        simulator, teammate = value
    else:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be a paired seed"
        )
    return (
        _nonnegative_int(simulator, f"{label}.simulator_seed"),
        _nonnegative_int(teammate, f"{label}.teammate_seed"),
    )


def _seed_pairs(values: object, label: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} must be a sequence"
        )
    pairs = tuple(
        _seed_pair(value, f"{label}[{index}]")
        for index, value in enumerate(values)
    )
    if not pairs:
        raise OfflineWaveD4AllSeedEndpointV1Error(f"{label} must not be empty")
    simulator = [row[0] for row in pairs]
    teammate = [row[1] for row in pairs]
    if len(simulator) != len(set(simulator)):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} repeats a simulator seed"
        )
    if len(teammate) != len(set(teammate)):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{label} repeats a teammate seed"
        )
    return pairs


def _endpoint_spec(horizon_ms: int) -> JSONMap:
    return {
        "schema": ENDPOINT_SPEC_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "fixed_horizon_ms": _positive_int(horizon_ms, "horizon_ms"),
        "terminal_boundary": "MIN_ALL_REQUIRED_TARGETS_DEAD_OR_FIXED_HORIZON",
        "all_seed_endpoints": [
            "cleared_by_horizon",
            "residual_required_health",
            "residual_required_health_fraction",
            "focal_effective_damage_by_horizon_or_clear",
        ],
        "conditional_endpoint": "focal_dps_if_cleared",
        "conditional_endpoint_role": "DIAGNOSTIC_NOT_DOMINANCE_GATE",
        "model_dominance_rule": {
            "completion_rate_delta": ">=0",
            "mean_residual_health_fraction_delta": "<=0",
            "mean_focal_effective_damage_delta": ">=0",
            "at_least_one_strict": True,
            "weighted_scalar_score": False,
        },
        "incomplete_damage_imputed_as_zero": False,
        "incomplete_dps_imputed_as_zero": False,
        "early_clear_damage_constant_through_horizon": True,
    }


def build_all_seed_endpoint_contract_v1(
    *,
    frozen_d3_source: Mapping[str, Any],
    prior_expansion_source: Mapping[str, Any],
    seed_pairs: Sequence[object],
    campaign_id: str,
    horizon_ms: int,
    evidence_mode: str,
) -> JSONMap:
    """Freeze endpoints and reject leakage into a fresh confirmation cohort."""

    if evidence_mode not in EVIDENCE_MODES:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"unsupported evidence_mode {evidence_mode!r}"
        )
    pairs = _seed_pairs(seed_pairs, "seed_pairs")
    # This validates the original D3 artifact and excludes its proposal,
    # selection, and first held-out seed components.
    base = build_frozen_heldout_expansion_contract_v1(
        source=frozen_d3_source,
        heldout_seed_pairs=pairs,
        expansion_id=_text(campaign_id, "campaign_id"),
    )
    if (
        not isinstance(prior_expansion_source, Mapping)
        or prior_expansion_source.get("schema") != PRIOR_EXPANSION_SCHEMA
    ):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "prior expansion source has the wrong schema"
        )
    prior_contract = prior_expansion_source.get("contract")
    if not isinstance(prior_contract, Mapping):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "prior expansion source lacks its frozen contract"
        )
    if prior_contract.get("frozen_candidate") != base["frozen_candidate"]:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "prior expansion and original D3 source freeze different candidates"
        )
    prior_pairs = _seed_pairs(
        prior_contract.get("heldout_seed_pairs"),
        "prior_expansion.heldout_seed_pairs",
    )
    prior_pair_set = set(prior_pairs)
    if evidence_mode == DIAGNOSTIC_REUSE_SEEN:
        unseen = sorted(set(pairs) - prior_pair_set)
        if unseen:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "diagnostic mode may use only the already-seen expansion pairs"
            )
    else:
        if len(pairs) != CONFIRMATORY_PAIR_COUNT:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                f"confirmatory mode requires exactly {CONFIRMATORY_PAIR_COUNT} pairs"
            )
        prior_simulator = {row[0] for row in prior_pairs}
        prior_teammate = {row[1] for row in prior_pairs}
        reused_simulator = sorted(row[0] for row in pairs if row[0] in prior_simulator)
        reused_teammate = sorted(row[1] for row in pairs if row[1] in prior_teammate)
        if reused_simulator or reused_teammate:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "confirmatory cohort reuses a seed component from the seen expansion"
            )

    return {
        "schema": CONTRACT_SCHEMA,
        "campaign_id": _text(campaign_id, "campaign_id"),
        "evidence_mode": evidence_mode,
        "frozen_candidate": deepcopy(base["frozen_candidate"]),
        "seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in pairs
        ],
        "excluded_original_d3_seed_pairs": deepcopy(
            base["excluded_prior_seed_pairs"]
        ),
        "excluded_seen_expansion_seed_pairs": [
            {"simulator_seed": simulator, "teammate_seed": teammate}
            for simulator, teammate in prior_pairs
        ],
        "required_controller_ids": list(CONTROLLER_IDS),
        "endpoint_spec": _endpoint_spec(horizon_ms),
        "endpoints_frozen_before_execution": True,
        "candidate_regenerated": False,
        "selection_reopened": False,
        "real_environment_comparison_authorized": False,
        "deployment_authorized": False,
    }


def _invalid_terminal(reason: str, *, horizon_ms: int) -> JSONMap:
    return {
        "schema": TERMINAL_SCHEMA,
        "technical_status": "INVALID_TERMINAL_EVIDENCE",
        "failure_reason": _text(reason, "failure_reason"),
        "fixed_horizon_ms": horizon_ms,
        "terminal_mode": None,
        "terminal_elapsed_ms": None,
        "cleared_by_horizon": None,
        "initial_required_health": None,
        "residual_required_health": None,
        "residual_required_health_fraction": None,
        "focal_effective_damage_by_horizon_or_clear": None,
        "background_effective_damage_by_horizon_or_clear": None,
        "combined_effective_damage_by_horizon_or_clear": None,
        "focal_dps_if_cleared": None,
        "target_outcomes": None,
    }


def extract_terminal_endpoint_v1(
    outcome: ScheduleReplayOutcomeV1,
    *,
    required_target_indexes: Sequence[int],
    horizon_ms: int,
) -> JSONMap:
    """Extract compact conserved terminal evidence from one replay outcome."""

    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("outcome must be ScheduleReplayOutcomeV1")
    horizon_ms = _positive_int(horizon_ms, "horizon_ms")
    indexes = tuple(
        _nonnegative_int(value, f"required_target_indexes[{index}]")
        for index, value in enumerate(required_target_indexes)
    )
    if not indexes or len(indexes) != len(set(indexes)) or indexes != tuple(sorted(indexes)):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "required_target_indexes must be nonempty, unique, and sorted"
        )
    if outcome.status is not ReplayStatusV1.COMPLETE:
        return _invalid_terminal(
            outcome.invalid_reason or f"replay status is {outcome.status.value}",
            horizon_ms=horizon_ms,
        )

    state = outcome.state
    if state.get("finished") is not True:
        return _invalid_terminal(
            "complete replay lacks finished=true terminal state",
            horizon_ms=horizon_ms,
        )
    try:
        elapsed_ms = _nonnegative_int(state.get("time_ms"), "terminal time_ms")
        if elapsed_ms > horizon_ms:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "terminal time exceeds the frozen horizon"
            )
        team = state.get("dynamic_team_background")
        semantics = state.get("dynamic_target_semantics")
        if not isinstance(team, Mapping) or not isinstance(semantics, Mapping):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "terminal state lacks dynamic team or target semantics"
            )
        team_targets = team.get("targets")
        semantic_targets = semantics.get("targets")
        if not isinstance(team_targets, list) or not isinstance(semantic_targets, list):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "terminal target registries must be lists"
            )
        if len(team_targets) != len(semantic_targets):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "team and semantic terminal target counts differ"
            )
        if indexes != tuple(range(len(team_targets))):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "required target registry must cover every modeled target"
            )

        compact_targets: list[JSONMap] = []
        for index in indexes:
            team_row = team_targets[index]
            semantic_row = semantic_targets[index]
            if not isinstance(team_row, Mapping) or not isinstance(
                semantic_row, Mapping
            ):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} terminal rows must be objects"
                )
            if (
                team_row.get("target_index") != index
                or semantic_row.get("target_index") != index
            ):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} registry identity drifted"
                )
            initial = _number(team_row.get("initial_health"), f"target {index} initial_health", minimum=1e-12)
            current = _number(team_row.get("current_health"), f"target {index} current_health")
            semantic_current = _number(
                semantic_row.get("current_health"),
                f"target {index} semantic current_health",
            )
            maximum = _number(
                semantic_row.get("maximum_health"),
                f"target {index} maximum_health",
                minimum=1e-12,
            )
            focal = _number(
                team_row.get("simulated_damage_applied"),
                f"target {index} focal damage",
            )
            background = _number(
                team_row.get("background_damage_applied"),
                f"target {index} background damage",
            )
            dead = team_row.get("dead")
            semantic_dead = semantic_row.get("dead")
            if type(dead) is not bool or semantic_dead is not dead:
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} dead state differs across terminal surfaces"
                )
            if not _same(current, semantic_current):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} current health differs across terminal surfaces"
                )
            if current > initial or initial > maximum:
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} terminal health ordering is invalid"
                )
            if dead != _same(current, 0.0):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} dead flag differs from zero health"
                )
            if not _same(initial, current + focal + background):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} damage does not conserve initial health"
                )
            death_time = team_row.get("death_time_ms")
            if death_time is not None:
                death_time = _nonnegative_int(
                    death_time, f"target {index} death_time_ms"
                )
                if death_time > elapsed_ms:
                    raise OfflineWaveD4AllSeedEndpointV1Error(
                        f"target {index} death occurs after terminal time"
                    )
            if dead != (death_time is not None):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"target {index} death time and dead flag differ"
                )
            compact_targets.append(
                {
                    "target_index": index,
                    "maximum_health": maximum,
                    "initial_health": initial,
                    "current_health": current,
                    "simulated_damage_applied": focal,
                    "background_damage_applied": background,
                    "dead": dead,
                    "death_time_ms": death_time,
                }
            )

        initial_total = float(
            math.fsum(row["initial_health"] for row in compact_targets)
        )
        residual = float(
            math.fsum(row["current_health"] for row in compact_targets)
        )
        focal_damage = float(
            math.fsum(row["simulated_damage_applied"] for row in compact_targets)
        )
        background_damage = float(
            math.fsum(row["background_damage_applied"] for row in compact_targets)
        )
        combined_damage = focal_damage + background_damage
        if not _same(initial_total, residual + combined_damage):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "aggregate terminal health and damage do not conserve"
            )
        for value, label in (
            (team.get("simulated_damage_applied"), "team focal damage"),
            (team.get("background_damage_applied"), "team background damage"),
            (team.get("combined_damage_applied"), "team combined damage"),
        ):
            _number(value, label)
        if not (
            _same(focal_damage, float(team["simulated_damage_applied"]))
            and _same(background_damage, float(team["background_damage_applied"]))
            and _same(combined_damage, float(team["combined_damage_applied"]))
            and _same(focal_damage, outcome.effective_damage)
        ):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "aggregate terminal damage differs from replay/team surfaces"
            )
        cleared = all(row["dead"] for row in compact_targets)
        if not cleared and elapsed_ms != horizon_ms:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "surviving targets require an exact fixed-horizon terminal"
            )
        if cleared and elapsed_ms <= 0:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "a cleared wave requires positive elapsed time"
            )
        return {
            "schema": TERMINAL_SCHEMA,
            "technical_status": "VALID_TERMINAL_EVIDENCE",
            "failure_reason": None,
            "fixed_horizon_ms": horizon_ms,
            "terminal_mode": (
                "ALL_REQUIRED_TARGETS_DEAD"
                if cleared
                else "FIXED_HORIZON_REACHED_WITH_SURVIVORS"
            ),
            "terminal_elapsed_ms": elapsed_ms,
            "cleared_by_horizon": cleared,
            "initial_required_health": initial_total,
            "residual_required_health": residual,
            "residual_required_health_fraction": residual / initial_total,
            "focal_effective_damage_by_horizon_or_clear": focal_damage,
            "background_effective_damage_by_horizon_or_clear": background_damage,
            "combined_effective_damage_by_horizon_or_clear": combined_damage,
            "focal_dps_if_cleared": (
                focal_damage * 1000.0 / elapsed_ms if cleared else None
            ),
            "target_outcomes": compact_targets,
        }
    except (KeyError, TypeError, ValueError) as error:
        return _invalid_terminal(
            f"{type(error).__name__}: {error}", horizon_ms=horizon_ms
        )


def build_all_seed_endpoint_row_v1(
    *,
    candidate_id: str,
    controller_id: str,
    simulator_seed: int,
    teammate_seed: int,
    replay_status: str,
    terminal_endpoint: Mapping[str, Any],
    domain_fallback_calls: int | None,
) -> JSONMap:
    """Bind terminal evidence to one controller/seed row without imputation."""

    candidate_id = _text(candidate_id, "candidate_id")
    if controller_id not in CONTROLLER_IDS:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"unsupported controller_id {controller_id!r}"
        )
    simulator_seed = _nonnegative_int(simulator_seed, "simulator_seed")
    teammate_seed = _nonnegative_int(teammate_seed, "teammate_seed")
    replay_status = _text(replay_status, "replay_status")
    if not isinstance(terminal_endpoint, Mapping) or terminal_endpoint.get(
        "schema"
    ) != TERMINAL_SCHEMA:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "terminal_endpoint has the wrong schema"
        )
    terminal = deepcopy(dict(terminal_endpoint))
    fallback_valid = True
    if controller_id in FALLBACK_CAPABLE_CONTROLLER_IDS:
        if type(domain_fallback_calls) is not int or domain_fallback_calls < 0:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                f"{controller_id} must report a nonnegative fallback count"
            )
        fallback_valid = domain_fallback_calls == 0
    elif domain_fallback_calls is not None:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            f"{controller_id} must not report a domain fallback count"
        )
    terminal_valid = (
        replay_status == ReplayStatusV1.COMPLETE.value
        and terminal.get("technical_status") == "VALID_TERMINAL_EVIDENCE"
    )
    eligible = terminal_valid and fallback_valid
    failure_reason = terminal.get("failure_reason")
    if terminal_valid and not fallback_valid:
        failure_reason = f"domain fallback calls={domain_fallback_calls}"
    return {
        "schema": ROW_SCHEMA,
        "candidate_id": candidate_id,
        "controller_id": controller_id,
        "simulator_seed": simulator_seed,
        "teammate_seed": teammate_seed,
        "replay_status": replay_status,
        "technical_status": (
            "ELIGIBLE_ALL_SEED_ENDPOINT"
            if eligible
            else "INELIGIBLE_ALL_SEED_ENDPOINT"
        ),
        "failure_reason": failure_reason,
        "domain_fallback_calls": domain_fallback_calls,
        "all_seed_endpoint_eligible": eligible,
        "terminal_endpoint": terminal,
        "incomplete_damage_imputed_as_zero": False,
        "incomplete_dps_imputed_as_zero": False,
    }


def _contract_parts(
    contract: Mapping[str, Any],
) -> tuple[str, str, tuple[tuple[int, int], ...], int]:
    if not isinstance(contract, Mapping) or contract.get("schema") != CONTRACT_SCHEMA:
        raise OfflineWaveD4AllSeedEndpointV1Error("invalid all-seed contract")
    frozen = contract.get("frozen_candidate")
    spec = contract.get("endpoint_spec")
    if not isinstance(frozen, Mapping) or not isinstance(spec, Mapping):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed contract lacks frozen candidate or endpoint spec"
        )
    if spec != _endpoint_spec(spec.get("fixed_horizon_ms")):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed endpoint specification drifted"
        )
    evidence_mode = contract.get("evidence_mode")
    if evidence_mode not in EVIDENCE_MODES:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed evidence mode drifted"
        )
    if contract.get("required_controller_ids") != list(CONTROLLER_IDS):
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed controller panel drifted"
        )
    for field, expected in (
        ("endpoints_frozen_before_execution", True),
        ("candidate_regenerated", False),
        ("selection_reopened", False),
        ("real_environment_comparison_authorized", False),
        ("deployment_authorized", False),
    ):
        if contract.get(field) is not expected:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                f"all-seed contract has invalid {field}"
            )
    return (
        _text(frozen.get("candidate_id"), "frozen candidate_id"),
        evidence_mode,
        _seed_pairs(contract.get("seed_pairs"), "contract.seed_pairs"),
        _positive_int(spec.get("fixed_horizon_ms"), "fixed_horizon_ms"),
    )


def _validated_row(row: Mapping[str, Any], *, horizon_ms: int) -> JSONMap:
    if not isinstance(row, Mapping) or row.get("schema") != ROW_SCHEMA:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed row has the wrong schema"
        )
    terminal = row.get("terminal_endpoint")
    if not isinstance(terminal, Mapping) or terminal.get("schema") != TERMINAL_SCHEMA:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed row lacks terminal evidence"
        )
    if terminal.get("fixed_horizon_ms") != horizon_ms:
        raise OfflineWaveD4AllSeedEndpointV1Error(
            "all-seed row used a different horizon"
        )
    rebuilt = build_all_seed_endpoint_row_v1(
        candidate_id=row.get("candidate_id"),
        controller_id=row.get("controller_id"),
        simulator_seed=row.get("simulator_seed"),
        teammate_seed=row.get("teammate_seed"),
        replay_status=row.get("replay_status"),
        terminal_endpoint=terminal,
        domain_fallback_calls=row.get("domain_fallback_calls"),
    )
    for field in (
        "technical_status",
        "failure_reason",
        "all_seed_endpoint_eligible",
        "incomplete_damage_imputed_as_zero",
        "incomplete_dps_imputed_as_zero",
    ):
        if row.get(field) != rebuilt[field]:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                f"all-seed row has invalid {field}"
            )
    if terminal.get("technical_status") == "VALID_TERMINAL_EVIDENCE":
        elapsed = _nonnegative_int(
            terminal.get("terminal_elapsed_ms"),
            "terminal_endpoint.terminal_elapsed_ms",
        )
        if elapsed > horizon_ms:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "terminal endpoint exceeds the frozen horizon"
            )
        targets = terminal.get("target_outcomes")
        if not isinstance(targets, list) or not targets:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "valid terminal endpoint lacks target outcomes"
            )
        initial_values: list[float] = []
        residual_values: list[float] = []
        focal_values: list[float] = []
        background_values: list[float] = []
        dead_values: list[bool] = []
        for index, target in enumerate(targets):
            if not isinstance(target, Mapping) or target.get("target_index") != index:
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    "terminal target registry must be contiguous and ordered"
                )
            maximum = _number(
                target.get("maximum_health"),
                f"terminal target[{index}].maximum_health",
                minimum=1e-12,
            )
            initial = _number(
                target.get("initial_health"),
                f"terminal target[{index}].initial_health",
                minimum=1e-12,
            )
            current = _number(
                target.get("current_health"),
                f"terminal target[{index}].current_health",
            )
            focal = _number(
                target.get("simulated_damage_applied"),
                f"terminal target[{index}].simulated_damage_applied",
            )
            background = _number(
                target.get("background_damage_applied"),
                f"terminal target[{index}].background_damage_applied",
            )
            dead = target.get("dead")
            if type(dead) is not bool:
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"terminal target[{index}].dead must be boolean"
                )
            death_time = target.get("death_time_ms")
            if death_time is not None:
                death_time = _nonnegative_int(
                    death_time, f"terminal target[{index}].death_time_ms"
                )
                if death_time > elapsed:
                    raise OfflineWaveD4AllSeedEndpointV1Error(
                        "terminal target death occurs after terminal time"
                    )
            if (
                initial > maximum
                or current > initial
                or dead != _same(current, 0.0)
                or dead != (death_time is not None)
                or not _same(initial, current + focal + background)
            ):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"terminal target[{index}] violates health conservation"
                )
            initial_values.append(initial)
            residual_values.append(current)
            focal_values.append(focal)
            background_values.append(background)
            dead_values.append(dead)

        initial_total = float(math.fsum(initial_values))
        residual_total = float(math.fsum(residual_values))
        focal_total = float(math.fsum(focal_values))
        background_total = float(math.fsum(background_values))
        combined_total = focal_total + background_total
        expected = {
            "initial_required_health": initial_total,
            "residual_required_health": residual_total,
            "residual_required_health_fraction": residual_total / initial_total,
            "focal_effective_damage_by_horizon_or_clear": focal_total,
            "background_effective_damage_by_horizon_or_clear": background_total,
            "combined_effective_damage_by_horizon_or_clear": combined_total,
        }
        for field, expected_value in expected.items():
            actual = _number(terminal.get(field), f"terminal_endpoint.{field}")
            if not _same(actual, expected_value):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    f"terminal endpoint {field} differs from target outcomes"
                )
        cleared = terminal.get("cleared_by_horizon")
        if type(cleared) is not bool or cleared != all(dead_values):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "cleared_by_horizon differs from terminal target outcomes"
            )
        expected_mode = (
            "ALL_REQUIRED_TARGETS_DEAD"
            if cleared
            else "FIXED_HORIZON_REACHED_WITH_SURVIVORS"
        )
        if terminal.get("terminal_mode") != expected_mode:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "terminal mode differs from clear state"
            )
        dps = terminal.get("focal_dps_if_cleared")
        if cleared:
            if elapsed <= 0:
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    "cleared endpoint requires positive elapsed time"
                )
            observed_dps = _number(dps, "terminal_endpoint.focal_dps_if_cleared")
            if not _same(observed_dps, focal_total * 1000.0 / elapsed):
                raise OfflineWaveD4AllSeedEndpointV1Error(
                    "conditional clear DPS differs from focal damage/time"
                )
        elif dps is not None or elapsed != horizon_ms:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "non-cleared endpoint must end at horizon with null clear DPS"
            )
    else:
        metric_fields = (
            "terminal_mode",
            "terminal_elapsed_ms",
            "cleared_by_horizon",
            "initial_required_health",
            "residual_required_health",
            "residual_required_health_fraction",
            "focal_effective_damage_by_horizon_or_clear",
            "background_effective_damage_by_horizon_or_clear",
            "combined_effective_damage_by_horizon_or_clear",
            "focal_dps_if_cleared",
            "target_outcomes",
        )
        if any(terminal.get(field) is not None for field in metric_fields):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "invalid terminal endpoint must keep metrics null"
            )
        _text(terminal.get("failure_reason"), "terminal failure_reason")
    return deepcopy(dict(row))


def _mean(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _delta_counts(values: Sequence[float], *, positive_better: bool) -> JSONMap:
    wins = losses = ties = 0
    for value in values:
        if math.isclose(value, 0.0, rel_tol=1e-12, abs_tol=1e-9):
            ties += 1
        elif (value > 0) is positive_better:
            wins += 1
        else:
            losses += 1
    return {
        "pi_star_win_count": wins,
        "pi_star_loss_count": losses,
        "tie_count": ties,
    }


def adjudicate_all_seed_endpoint_v1(
    *, contract: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> JSONMap:
    """Aggregate all technically valid seeds without complete-case deletion."""

    candidate_id, evidence_mode, pairs, horizon_ms = _contract_parts(contract)
    expected_pairs = set(pairs)
    validated = [_validated_row(row, horizon_ms=horizon_ms) for row in rows]
    grouped: dict[tuple[int, int], list[JSONMap]] = defaultdict(list)
    for row in validated:
        pair = (row["simulator_seed"], row["teammate_seed"])
        if pair not in expected_pairs:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "all-seed row is outside the frozen cohort"
            )
        if row["candidate_id"] != candidate_id:
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "all-seed row names a different frozen candidate"
            )
        grouped[pair].append(row)

    pair_index: dict[tuple[int, int], dict[str, JSONMap]] = {}
    pair_receipts: list[JSONMap] = []
    for pair in pairs:
        members = grouped[pair]
        by_controller = {row["controller_id"]: row for row in members}
        if len(by_controller) != len(members):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "all-seed pair repeats a controller"
            )
        if set(by_controller) != set(CONTROLLER_IDS):
            raise OfflineWaveD4AllSeedEndpointV1Error(
                "all-seed pair lacks the exact five-controller panel"
            )
        pair_index[pair] = by_controller
        eligible = all(row["all_seed_endpoint_eligible"] for row in members)
        pair_receipts.append(
            {
                "simulator_seed": pair[0],
                "teammate_seed": pair[1],
                "all_five_controllers_eligible": eligible,
                "ineligible_controller_ids": [
                    controller_id
                    for controller_id in CONTROLLER_IDS
                    if not by_controller[controller_id]["all_seed_endpoint_eligible"]
                ],
            }
        )

    aggregates: JSONMap = {}
    for controller_id in CONTROLLER_IDS:
        controller_rows = [pair_index[pair][controller_id] for pair in pairs]
        eligible_rows = [
            row for row in controller_rows if row["all_seed_endpoint_eligible"]
        ]
        terminals = [row["terminal_endpoint"] for row in eligible_rows]
        cleared = [row for row in terminals if row["cleared_by_horizon"] is True]
        aggregates[controller_id] = {
            "requested_seed_count": len(pairs),
            "technically_valid_seed_count": len(eligible_rows),
            "invalid_seed_count": len(pairs) - len(eligible_rows),
            "clear_count": len(cleared),
            "completion_rate": (
                len(cleared) / len(eligible_rows) if eligible_rows else None
            ),
            "mean_residual_required_health": _mean(
                [row["residual_required_health"] for row in terminals]
            ),
            "mean_residual_required_health_fraction": _mean(
                [row["residual_required_health_fraction"] for row in terminals]
            ),
            "mean_focal_effective_damage_by_horizon_or_clear": _mean(
                [row["focal_effective_damage_by_horizon_or_clear"] for row in terminals]
            ),
            "mean_background_effective_damage_by_horizon_or_clear": _mean(
                [row["background_effective_damage_by_horizon_or_clear"] for row in terminals]
            ),
            "conditional_clear_seed_count": len(cleared),
            "mean_focal_dps_if_cleared": _mean(
                [row["focal_dps_if_cleared"] for row in cleared]
            ),
            "failed_or_incomplete_imputed_as_zero": False,
        }

    pairwise: JSONMap = {}
    for baseline_id in CONTROLLER_IDS:
        if baseline_id == PI_STAR:
            continue
        valid_pairs = [
            pair
            for pair in pairs
            if pair_index[pair][PI_STAR]["all_seed_endpoint_eligible"]
            and pair_index[pair][baseline_id]["all_seed_endpoint_eligible"]
        ]
        pi_only_clear = baseline_only_clear = both_clear = neither_clear = 0
        clear_deltas: list[float] = []
        residual_deltas: list[float] = []
        focal_deltas: list[float] = []
        conditional_dps_deltas: list[float] = []
        for pair in valid_pairs:
            pi_terminal = pair_index[pair][PI_STAR]["terminal_endpoint"]
            baseline_terminal = pair_index[pair][baseline_id]["terminal_endpoint"]
            pi_clear = bool(pi_terminal["cleared_by_horizon"])
            baseline_clear = bool(baseline_terminal["cleared_by_horizon"])
            clear_deltas.append(float(pi_clear) - float(baseline_clear))
            if pi_clear and baseline_clear:
                both_clear += 1
                conditional_dps_deltas.append(
                    pi_terminal["focal_dps_if_cleared"]
                    - baseline_terminal["focal_dps_if_cleared"]
                )
            elif pi_clear:
                pi_only_clear += 1
            elif baseline_clear:
                baseline_only_clear += 1
            else:
                neither_clear += 1
            residual_deltas.append(
                pi_terminal["residual_required_health_fraction"]
                - baseline_terminal["residual_required_health_fraction"]
            )
            focal_deltas.append(
                pi_terminal["focal_effective_damage_by_horizon_or_clear"]
                - baseline_terminal["focal_effective_damage_by_horizon_or_clear"]
            )

        completion_delta = _mean(clear_deltas)
        residual_delta = _mean(residual_deltas)
        focal_delta = _mean(focal_deltas)
        eps = 1e-12
        pi_dominates = (
            completion_delta is not None
            and residual_delta is not None
            and focal_delta is not None
            and completion_delta >= -eps
            and residual_delta <= eps
            and focal_delta >= -eps
            and (
                completion_delta > eps
                or residual_delta < -eps
                or focal_delta > eps
            )
        )
        baseline_dominates = (
            completion_delta is not None
            and residual_delta is not None
            and focal_delta is not None
            and completion_delta <= eps
            and residual_delta >= -eps
            and focal_delta <= eps
            and (
                completion_delta < -eps
                or residual_delta > eps
                or focal_delta < -eps
            )
        )
        dominance = (
            "PI_STAR_MODEL_DOMINATES"
            if pi_dominates
            else "BASELINE_MODEL_DOMINATES"
            if baseline_dominates
            else "MIXED_OR_TIED"
        )
        pairwise[baseline_id] = {
            "technically_valid_paired_seed_count": len(valid_pairs),
            "both_clear_count": both_clear,
            "pi_star_only_clear_count": pi_only_clear,
            "baseline_only_clear_count": baseline_only_clear,
            "neither_clear_count": neither_clear,
            "completion_rate_delta": completion_delta,
            "mean_residual_health_fraction_delta": residual_delta,
            "median_residual_health_fraction_delta": _median(residual_deltas),
            "mean_focal_effective_damage_delta": focal_delta,
            "median_focal_effective_damage_delta": _median(focal_deltas),
            "mean_conditional_clear_dps_delta": _mean(conditional_dps_deltas),
            "median_conditional_clear_dps_delta": _median(conditional_dps_deltas),
            "residual_health_pair_counts": _delta_counts(
                residual_deltas, positive_better=False
            ),
            "focal_damage_pair_counts": _delta_counts(
                focal_deltas, positive_better=True
            ),
            "conditional_clear_dps_pair_counts": _delta_counts(
                conditional_dps_deltas, positive_better=True
            ),
            "model_dominance": dominance,
            "conditional_clear_dps_is_diagnostic_only": True,
        }

    all_pairs_valid = all(
        row["all_five_controllers_eligible"] for row in pair_receipts
    )
    confirmatory_complete = (
        evidence_mode == FRESH_CONFIRMATORY
        and len(pairs) == CONFIRMATORY_PAIR_COUNT
        and all_pairs_valid
    )
    dominates_all = confirmatory_complete and all(
        row["model_dominance"] == "PI_STAR_MODEL_DOMINATES"
        for row in pairwise.values()
    )
    if not all_pairs_valid:
        status = "ALL_SEED_ENDPOINT_PANEL_INCOMPLETE"
    elif evidence_mode == DIAGNOSTIC_REUSE_SEEN:
        status = "SEEN_SEED_ENDPOINT_DIAGNOSTIC_COMPLETE"
    else:
        status = "FRESH_ALL_SEED_ENDPOINT_CONFIRMATION_COMPLETE"
    return {
        "schema": RECEIPT_SCHEMA,
        "campaign_id": contract["campaign_id"],
        "status": status,
        "evidence_mode": evidence_mode,
        "requested_seed_count": len(pairs),
        "all_five_controllers_valid_seed_count": sum(
            row["all_five_controllers_eligible"] for row in pair_receipts
        ),
        "pair_receipts": pair_receipts,
        "per_controller": aggregates,
        "pi_star_pairwise_by_baseline": pairwise,
        "confirmatory_endpoint_complete": confirmatory_complete,
        "development_model_superiority_status": (
            "PI_STAR_PARETO_DOMINATES_ALL_BASELINES"
            if dominates_all
            else "NOT_ESTABLISHED"
        ),
        "real_environment_superiority_authorized": False,
        "deployment_authorized": False,
        "incomplete_damage_imputed_as_zero": False,
        "incomplete_dps_imputed_as_zero": False,
    }


__all__ = (
    "CONFIRMATORY_PAIR_COUNT",
    "CONTRACT_SCHEMA",
    "DIAGNOSTIC_REUSE_SEEN",
    "EVIDENCE_MODES",
    "FRESH_CONFIRMATORY",
    "IMPLEMENTATION_REVISION",
    "OfflineWaveD4AllSeedEndpointV1Error",
    "RECEIPT_SCHEMA",
    "ROW_SCHEMA",
    "SCHEMA",
    "TERMINAL_SCHEMA",
    "adjudicate_all_seed_endpoint_v1",
    "build_all_seed_endpoint_contract_v1",
    "build_all_seed_endpoint_row_v1",
    "extract_terminal_endpoint_v1",
)
