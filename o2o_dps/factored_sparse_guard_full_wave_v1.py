"""Independent full-wave validation for route-local sparse Cat guards.

The sparse learner only creates a development shortlist.  This module runs
each shortlisted guard as its own one-intervention Cat-relative policy on
held-out full waves, authorizes it only from complete transfer evidence, and
then reduces another untouched phase.  Cat and an abstaining sparse policy
are executed once per case and shared by every guard on that exact mechanism
route.  Full press lanes are deliberately not retained in worker artifacts.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, fields
import json
import math
import os
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Iterable, Mapping

from .branch_teacher_v1 import BranchReplayMismatchV1, _run_fresh
from .cat_external_press_action_teacher_v1 import (
    DEFAULT_BRIDGE,
    _branch_action_accepted,
    _normalized_paired_delta,
    _run_press_lane,
    _same_press_prefix,
    _terminal_receipt,
    _wire,
)
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from .cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    CatSparseGuardPolicyV2,
    SparseGuardV2,
)
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from .factored_cat_branch_router_v1 import MechanismRouteV1, mechanism_route_v1
from .factored_external_press_matrix_v1 import (
    MATRIX_RANKS,
    MATRIX_STRATA,
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
    SPARSE_SHORTLIST_SCHEMA,
    _bridge_version_tag,
    _case_from_projection,
    _case_projection,
    _load_item_database,
    build_matrix_case_from_seed_v1,
)
from .factored_external_press_search_v1 import (
    _exact_active_fallback_identity,
    _exact_noop_identity,
    _semantic_receipt,
)
from .factored_sparse_guard_learner_v2 import SCHEMA as SPARSE_LEARNER_SCHEMA
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)


SCHEMA = "factored_sparse_guard_full_wave/v1"
CASE_SCHEMA = "factored_sparse_guard_full_wave_case/v1"
BATCH_SCHEMA = "factored_sparse_guard_full_wave_batch/v1"
AUTHORIZATION_SCHEMA = "factored_sparse_guard_full_wave_authorization/v1"
FRESH_SCHEMA = "factored_sparse_guard_full_wave_fresh/v1"
EXECUTION_CONTRACT_SCHEMA = "factored_sparse_guard_full_wave_execution_contract/v1"

TRANSFER_PHASE = "held_out_transfer"
FRESH_PHASE = "untouched_fresh"
PHASES = (TRANSFER_PHASE, FRESH_PHASE)
PHASE_SEED_BASES = {
    TRANSFER_PHASE: 264_000_000_000,
    FRESH_PHASE: 265_000_000_000,
}
PHASE_SEED_CAPACITY = 1_000_000
MAX_SAMPLE_INDEX = PHASE_SEED_CAPACITY - 1
DEFAULT_BRIDGE_CWD = Path(__file__).resolve().parents[2] / "wowsims-turtle"
_ROUTE_FIELDS = tuple(field.name for field in fields(MechanismRouteV1))
_GUARD_FIELDS = tuple(field.name for field in fields(SparseGuardV2))
_BATCH_CONTEXT: dict[str, Any] | None = None


def _route_key(route: MechanismRouteV1 | Mapping[str, Any]) -> tuple[Any, ...]:
    raw = asdict(route) if isinstance(route, MechanismRouteV1) else route
    return tuple(raw[name] for name in _ROUTE_FIELDS)


def _guard_key(guard: SparseGuardV2 | Mapping[str, Any]) -> tuple[Any, ...]:
    raw = asdict(guard) if isinstance(guard, SparseGuardV2) else guard
    return tuple(raw[name] for name in _GUARD_FIELDS)


def _finite_number(value: Any) -> bool:
    return bool(
        type(value) in (int, float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def sparse_full_wave_seed_v1(
    rank: int, stratum: str, phase: str, sample_index: int,
) -> int:
    """Return a phase-disjoint randomized-block seed shared by all 12 cells."""

    if rank not in MATRIX_RANKS:
        raise ValueError(f"rank must be one of {MATRIX_RANKS}")
    if stratum not in MATRIX_STRATA:
        raise ValueError(f"stratum must be one of {MATRIX_STRATA}")
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    if type(sample_index) is not int or not 0 <= sample_index <= MAX_SAMPLE_INDEX:
        raise ValueError(f"sample_index must be in [0, {MAX_SAMPLE_INDEX}]")
    return PHASE_SEED_BASES[phase] + sample_index


def _seed_in_phase(seed: Any, phase: str) -> bool:
    base = PHASE_SEED_BASES[phase]
    return type(seed) is int and base <= seed < base + PHASE_SEED_CAPACITY


def _route_from_wire(raw: Any) -> MechanismRouteV1:
    if not isinstance(raw, Mapping) or set(raw) != set(_ROUTE_FIELDS):
        raise ValueError("sparse shortlist mechanism route is malformed")
    return MechanismRouteV1(**{name: raw[name] for name in _ROUTE_FIELDS})


def _guard_from_wire(raw: Any) -> SparseGuardV2:
    if not isinstance(raw, Mapping) or set(raw) != set(_GUARD_FIELDS):
        raise ValueError("sparse shortlist guard is malformed")
    guard = SparseGuardV2(**{name: raw[name] for name in _GUARD_FIELDS})
    if guard.kind is None:
        raise ValueError("a shortlist may not contain the Cat abstention arm")
    return guard


def _route_relative_guard_key(
    route: MechanismRouteV1, guard: SparseGuardV2,
) -> tuple[Any, ...]:
    """Collapse predicates already fixed by an exact mechanism route."""

    predicates = [
        (guard.first_feature, guard.first_value),
        (guard.second_feature, guard.second_value),
    ]
    retained = []
    for feature, value in predicates:
        if feature is None:
            continue
        if feature == "weapon_mode":
            if value != route.weapon_mode:
                raise ValueError("sparse guard contradicts its exact route weapon mode")
            continue
        retained.append((feature, value))
    return (guard.kind, *retained)


def validate_sparse_shortlist_v5(
    artifact: Mapping[str, Any],
) -> dict[MechanismRouteV1, tuple[SparseGuardV2, ...]]:
    """Validate the current matrix shortlist and return exact route bindings.

    The schema constants are imported from their producers, so a coordinated
    producer schema bump is picked up without leaving a stale literal here.
    """

    if not isinstance(artifact, Mapping) or artifact.get("schema") != SPARSE_SHORTLIST_SCHEMA:
        raise ValueError("sparse shortlist wrapper schema differs")
    learner = artifact.get("learner")
    if not isinstance(learner, Mapping) or learner.get("schema") != SPARSE_LEARNER_SCHEMA:
        raise ValueError("sparse shortlist learner schema differs")
    if (
        artifact.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or artifact.get("raw_chronicle_rows_loaded") is not False
        or artifact.get("full_wave_candidate_policy_evaluated") is not False
        or artifact.get("voting_eligible") is not False
        or artifact.get("deployment_eligible") is not False
    ):
        raise ValueError("sparse shortlist wrapper exceeds its development scope")
    if (
        learner.get("feature_order") != list(FEATURE_ORDER)
        or learner.get("authorization_claimed") is not False
        or learner.get("full_wave_candidate_policy_evaluated") is not False
        or learner.get("voting_eligible") is not False
        or learner.get("deployment_eligible") is not False
        or (learner.get("cat_arm") or {}).get("role") != "PERMANENT_FALLBACK"
        or (learner.get("cat_arm") or {}).get("residual_effective_damage_delta") != 0.0
    ):
        raise ValueError("sparse learner contract is incomplete")
    routes = learner.get("routes")
    if not isinstance(routes, list) or learner.get("route_count") != len(routes):
        raise ValueError("sparse learner route inventory is incomplete")

    result: dict[MechanismRouteV1, tuple[SparseGuardV2, ...]] = {}
    total = 0
    for row in routes:
        if not isinstance(row, Mapping):
            raise ValueError("sparse learner route row is malformed")
        route = _route_from_wire(row.get("mechanism_route"))
        if route in result:
            raise ValueError("sparse learner contains a duplicate exact route")
        raw_shortlist = row.get("shortlist")
        if not isinstance(raw_shortlist, list) or row.get("shortlist_count") != len(raw_shortlist):
            raise ValueError("sparse learner shortlist count differs from its rows")
        guards = tuple(_guard_from_wire(raw) for raw in raw_shortlist)
        if len({_guard_key(guard) for guard in guards}) != len(guards):
            raise ValueError("sparse learner repeats a guard on one exact route")
        if len({_route_relative_guard_key(route, guard) for guard in guards}) != len(guards):
            raise ValueError("sparse learner contains duplicate route-relative policies")
        diagnostics = row.get("candidate_diagnostics")
        if not isinstance(diagnostics, list):
            raise ValueError("sparse learner lacks candidate diagnostics")
        selected = {
            _guard_key(_guard_from_wire(candidate.get("guard")))
            for candidate in diagnostics
            if isinstance(candidate, Mapping) and candidate.get("shortlisted") is True
        }
        if selected != {_guard_key(guard) for guard in guards}:
            raise ValueError("sparse learner shortlist differs from selected diagnostics")
        result[route] = guards
        total += len(guards)
    if learner.get("shortlisted_guard_count") != total:
        raise ValueError("sparse learner global shortlist count differs")
    if total and artifact.get("status") != "SPARSE_GUARD_SHORTLIST_READY_NONVOTING":
        raise ValueError("nonempty sparse shortlist is not marked ready")
    if not total and artifact.get("status") != "ABSTAIN_NO_STABLE_REACHABLE_SPARSE_GUARD":
        raise ValueError("empty sparse shortlist has an inconsistent status")
    training_seeds = artifact.get("training_seeds")
    if (
        not isinstance(training_seeds, list)
        or len(training_seeds) != len(set(training_seeds))
        or any(type(seed) is not int for seed in training_seeds)
        or artifact.get("training_distinct_seed_count") != len(training_seeds)
    ):
        raise ValueError("sparse shortlist training seed provenance is malformed")
    semantic = artifact.get("execution_semantic_contract")
    if (
        not isinstance(semantic, Mapping)
        or semantic.get("required_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("sparse shortlist lacks the atomic press-clock contract")
    return result


def _authorized_route_guards(
    artifact: Mapping[str, Any],
) -> dict[MechanismRouteV1, tuple[SparseGuardV2, ...]]:
    if (
        artifact.get("schema") != AUTHORIZATION_SCHEMA
        or artifact.get("status") != "TRANSFER_AUTHORIZATION_COMPLETE_NONVOTING"
        or artifact.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or artifact.get("source_shortlist_schema") != SPARSE_SHORTLIST_SCHEMA
        or artifact.get("source_learner_schema") != SPARSE_LEARNER_SCHEMA
        or artifact.get("raw_chronicle_rows_loaded") is not False
        or artifact.get("voting_eligible") is not False
        or artifact.get("deployment_eligible") is not False
    ):
        raise ValueError("sparse full-wave authorization schema differs")
    training = artifact.get("training_seeds")
    transfer = artifact.get("transfer_seeds")
    semantic = artifact.get("execution_semantic_contract")
    if (
        not isinstance(training, list) or len(training) != len(set(training))
        or any(type(seed) is not int for seed in training)
        or not isinstance(transfer, list) or len(transfer) != len(set(transfer))
        or any(not _seed_in_phase(seed, TRANSFER_PHASE) for seed in transfer)
        or set(training) & set(transfer)
        or not isinstance(semantic, Mapping)
        or semantic.get("required_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("authorization phase provenance is malformed")
    rows = artifact.get("authorized_routes")
    if not isinstance(rows, list):
        raise ValueError("authorization lacks exact authorized routes")
    result: dict[MechanismRouteV1, tuple[SparseGuardV2, ...]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("authorized route row is malformed")
        route = _route_from_wire(row.get("mechanism_route"))
        raw_guards = row.get("guards")
        if route in result or not isinstance(raw_guards, list):
            raise ValueError("authorized route inventory is malformed")
        guards = tuple(_guard_from_wire(raw) for raw in raw_guards)
        if not guards or len({_guard_key(guard) for guard in guards}) != len(guards):
            raise ValueError("authorized route guard inventory is malformed")
        if len({_route_relative_guard_key(route, guard) for guard in guards}) != len(guards):
            raise ValueError("authorization repeats a route-relative policy")
        result[route] = guards
    if artifact.get("authorized_guard_count") != sum(map(len, result.values())):
        raise ValueError("authorized guard count differs from exact routes")
    authorizations = artifact.get("guard_authorizations")
    authorized_keys = {
        (_route_key(route), _guard_key(guard))
        for route, guards in result.items() for guard in guards
    }
    recorded_rows = [
        row for row in authorizations or []
        if isinstance(row, Mapping) and row.get("authorized_for_fresh") is True
    ]
    recorded_keys = {
        (
            _route_key(_route_from_wire(row.get("mechanism_route"))),
            _guard_key(_guard_from_wire(row.get("guard"))),
        )
        for row in recorded_rows
    }
    if (
        not isinstance(authorizations, list)
        or len(recorded_rows) != len(recorded_keys)
        or recorded_keys != authorized_keys
    ):
        raise ValueError("authorized routes differ from transfer decisions")
    return result


def _case_tracking(case: DevelopmentWaveCaseV1) -> dict[str, Any]:
    return {
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
    }


def _lane(
    case: DevelopmentWaveCaseV1,
    adapter: Any,
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int,
    max_presses: int,
    simulator_inputs: CatFurySimulatorInputsV5 | None,
) -> dict[str, Any]:
    return _run_fresh(
        bridge_factory,
        lambda bridge: _run_press_lane(
            bridge,
            case,
            adapter,
            period_ms=period_ms,
            max_presses=max_presses,
            simulator_inputs=simulator_inputs,
        ),
    )


def _shared_baseline(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int,
    max_presses: int,
    simulator_inputs: CatFurySimulatorInputsV5 | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cat = _lane(
        case, CatFuryFullPolicyAdapterV4(), bridge_factory,
        period_ms=period_ms, max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    no_op_adapter = CatSparseGuardPolicyV2(SparseGuardV2())
    no_op = _lane(
        case, no_op_adapter, bridge_factory,
        period_ms=period_ms, max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    terminals = {
        "cat": _terminal_receipt(case, cat),
        "no_op": _terminal_receipt(case, no_op),
    }
    semantics = {
        "cat": _semantic_receipt(case, cat),
        "no_op": _semantic_receipt(case, no_op),
    }
    identity = _exact_noop_identity(
        cat, no_op, no_op_interventions=len(no_op_adapter.intervention_receipts),
    )
    ready = bool(
        identity["exact"]
        and all(row.get("status") == "COMPLETED" for row in terminals.values())
        and all(row.get("valid") is True for row in semantics.values())
        and cat.get("press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        and no_op.get("press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    )
    compact = {
        "status": (
            "COMPLETE_SHARED_CAT_NOOP_BASELINE"
            if ready else "INCOMPLETE_SHARED_CAT_NOOP_BASELINE"
        ),
        "cat_terminal": terminals["cat"],
        "no_op_terminal": terminals["no_op"],
        "semantic_receipts": semantics,
        "no_op_identity_gate": identity,
        "press_clock_configuration_modes": {
            "cat": cat.get("press_clock_configuration_mode"),
            "no_op": no_op.get("press_clock_configuration_mode"),
        },
        "technical_receipts_ready": ready,
    }
    return cat, no_op, compact


def _evaluate_guard(
    case: DevelopmentWaveCaseV1,
    route: MechanismRouteV1,
    guard: SparseGuardV2,
    cat: Mapping[str, Any],
    shared: Mapping[str, Any],
    bridge_factory: Callable[[], Any],
    *,
    phase: str,
    period_ms: int,
    max_presses: int,
    simulator_inputs: CatFurySimulatorInputsV5 | None,
) -> dict[str, Any]:
    adapter = CatSparseGuardPolicyV2(guard)
    candidate = _lane(
        case, adapter, bridge_factory,
        period_ms=period_ms, max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    candidate_terminal = _terminal_receipt(case, candidate)
    candidate_semantics = _semantic_receipt(case, candidate)
    interventions = adapter.intervention_receipts
    active_fallback = _exact_active_fallback_identity(
        cat, candidate, candidate_interventions=len(interventions),
    )
    prefix_verified = False
    prefix_press_count: int | None = None
    action_accepted = False
    action_receipts: dict[str, Any] | None = None
    intervention: Mapping[str, Any] | None = None
    receipt_bound = False
    if len(interventions) == 1:
        intervention = interventions[0]
        decision_index = intervention.get("decision_index")
        receipt_bound = bool(
            type(decision_index) is int
            and intervention.get("kind") == guard.kind
            and intervention.get("guard") == asdict(guard)
        )
        if receipt_bound:
            try:
                prefix_press_count = _same_press_prefix(cat, candidate, decision_index)
                cat_press = next(
                    row for row in cat.get("presses") or []
                    if row.get("decision_index") == decision_index
                )
                candidate_press = next(
                    row for row in candidate.get("presses") or []
                    if row.get("decision_index") == decision_index
                )
                action_accepted, action_receipts = _branch_action_accepted(
                    str(guard.kind), cat_press, candidate_press,
                )
                prefix_verified = True
            except (BranchReplayMismatchV1, StopIteration):
                prefix_verified = False
    strict = bool(
        len(interventions) == 1 and receipt_bound
        and prefix_verified and action_accepted
    )
    candidate_ready = bool(
        candidate_terminal.get("status") == "COMPLETED"
        and candidate_semantics.get("valid") is True
        and candidate.get("press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    )
    shared_ready = shared.get("technical_receipts_ready") is True
    triggered_ready = bool(shared_ready and candidate_ready and strict)
    fallback_ready = bool(
        shared_ready and candidate_ready and len(interventions) == 0
        and active_fallback["exact"]
    )
    delta = (
        _normalized_paired_delta(
            candidate_terminal["own_effective_damage"],
            shared["cat_terminal"]["own_effective_damage"],
        )
        if triggered_ready else 0.0 if fallback_ready else None
    )
    if triggered_ready:
        status = "COMPLETE_TRIGGERED_SPARSE_GUARD_PAIR"
        effect_class = "TRIGGERED_INTERVENTION"
    elif fallback_ready:
        status = "EXACT_CAT_NO_TRIGGER_FALLBACK"
        effect_class = "EXACT_CAT_FALLBACK_ZERO"
    else:
        status = "UNKNOWN_INCOMPLETE_SPARSE_GUARD_PAIR"
        effect_class = "UNKNOWN_NOT_IMPUTED"
    compact_intervention = None
    if isinstance(intervention, Mapping):
        compact_intervention = {
            "decision_index": intervention.get("decision_index"),
            "kind": intervention.get("kind"),
            "guard": deepcopy(intervention.get("guard")),
            "matched_features": deepcopy(intervention.get("matched_features")),
        }
    return {
        "schema": "factored_sparse_guard_full_wave_pair/v1",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        **_case_tracking(case),
        "phase": phase,
        "mechanism_route": asdict(route),
        "guard": asdict(guard),
        "status": status,
        "effect_class": effect_class,
        "candidate_intervention_count": len(interventions),
        "candidate_intervention": compact_intervention,
        "candidate_branch_action_accepted": action_accepted,
        "candidate_action_receipts": _wire(action_receipts),
        "accepted_prefix_presses_verified": (
            prefix_press_count if strict else None
        ),
        "strict_single_intervention_verified": strict,
        "active_cat_fallback_identity_gate": active_fallback,
        "active_cat_fallback_verified": fallback_ready,
        "candidate_terminal": candidate_terminal,
        "candidate_semantic_receipt": candidate_semantics,
        "candidate_press_clock_configuration_mode": candidate.get(
            "press_clock_configuration_mode"
        ),
        "paired_effective_damage_delta": delta,
        "technical_receipts_ready": triggered_ready or fallback_ready,
        "comparison_ready": triggered_ready or fallback_ready,
        "full_press_lane_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def evaluate_sparse_guard_case_v1(
    case: DevelopmentWaveCaseV1,
    route: MechanismRouteV1,
    guards: Iterable[SparseGuardV2],
    bridge_factory: Callable[[], Any],
    *,
    phase: str,
    period_ms: int = 100,
    max_presses: int = 400,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all guards for one case with one shared Cat/no-op baseline."""

    if not isinstance(case, DevelopmentWaveCaseV1):
        raise TypeError("case must be DevelopmentWaveCaseV1")
    if not isinstance(route, MechanismRouteV1):
        raise TypeError("route must be MechanismRouteV1")
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    if not _seed_in_phase(case.dynamic_load.seed, phase):
        raise ValueError("case seed is outside the sparse full-wave phase namespace")
    actual_route = mechanism_route_v1(
        case, item_database=item_database,
        talent_position_map=talent_position_map,
    )
    if actual_route != route:
        raise ValueError("case does not belong to the supplied exact mechanism route")
    frozen_guards = tuple(guards)
    if (
        not frozen_guards
        or any(not isinstance(guard, SparseGuardV2) or guard.kind is None
               for guard in frozen_guards)
        or len({_guard_key(guard) for guard in frozen_guards}) != len(frozen_guards)
    ):
        raise ValueError("case evaluation requires distinct active sparse guards")
    cat, _, shared = _shared_baseline(
        case, bridge_factory, period_ms=period_ms, max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    pairs = [
        _evaluate_guard(
            case, route, guard, cat, shared, bridge_factory,
            phase=phase, period_ms=period_ms, max_presses=max_presses,
            simulator_inputs=simulator_inputs,
        )
        for guard in frozen_guards
    ]
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        **_case_tracking(case),
        "phase": phase,
        "mechanism_route": asdict(route),
        "guard_count": len(frozen_guards),
        "shared_cat_no_op": shared,
        "guard_pairs": pairs,
        "status": (
            "COMPLETE_SPARSE_GUARD_CASE_COMPARISONS"
            if all(pair["technical_receipts_ready"] for pair in pairs)
            else "INCOMPLETE_SPARSE_GUARD_CASE_COMPARISONS"
        ),
        "all_guard_receipts_ready": all(
            pair["technical_receipts_ready"] for pair in pairs
        ),
        "cat_no_op_lane_count": 2,
        "candidate_lane_count": len(frozen_guards),
        "full_press_lanes_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _policy_routes(
    policy_artifact: Mapping[str, Any], phase: str,
) -> dict[MechanismRouteV1, tuple[SparseGuardV2, ...]]:
    if phase == TRANSFER_PHASE:
        return validate_sparse_shortlist_v5(policy_artifact)
    if policy_artifact.get("schema") == AUTHORIZATION_SCHEMA:
        return _authorized_route_guards(policy_artifact)
    # The companion refinement module depends on this runner.  Import lazily
    # so a refinement artifact can be an untouched-fresh policy source without
    # creating a module import cycle or pretending it passed transfer.
    from .factored_sparse_guard_post_transfer_refinement_v1 import (
        validate_post_transfer_refinement_v1,
    )
    return validate_post_transfer_refinement_v1(policy_artifact)


def _source_semantic(policy_artifact: Mapping[str, Any], phase: str) -> Mapping[str, Any]:
    raw = (
        policy_artifact.get("execution_semantic_contract")
        if phase == TRANSFER_PHASE
        else policy_artifact.get("execution_semantic_contract")
    )
    if not isinstance(raw, Mapping):
        raise ValueError("policy artifact lacks its execution semantic contract")
    return raw


def _execution_contract(
    *, phase: str, route: MechanismRouteV1,
    guards: tuple[SparseGuardV2, ...], policy_artifact: Mapping[str, Any],
    period_ms: int, max_presses: int, bridge_path: Path,
) -> dict[str, Any]:
    source = _source_semantic(policy_artifact, phase)
    semantic = {
        "period_ms": period_ms,
        "max_presses": max_presses,
        "required_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "bridge_version_tag": _bridge_version_tag(bridge_path),
        "shared_cat_no_op_once_per_case": True,
        "each_guard_independent_fresh_lane": True,
    }
    for name in (
        "period_ms", "max_presses", "required_press_clock_configuration_mode",
        "bridge_version_tag",
    ):
        if source.get(name) != semantic[name]:
            raise ValueError(f"full-wave {name} differs from preceding phase")
    return {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "semantic": semantic,
        "policy_lineage": {
            "source_schema": policy_artifact.get("schema"),
            "mechanism_route": asdict(route),
            "guards": [asdict(guard) for guard in guards],
        },
    }


def _profile_overrides(
    *, representatives_path: Path | None,
    catalog_manifest_path: Path | None,
    catalog_data_path: Path | None,
) -> dict[str, Path] | None:
    rows = {
        "representatives": representatives_path,
        "catalog_manifest": catalog_manifest_path,
        "catalog_data": catalog_data_path,
    }
    values = {name: path for name, path in rows.items() if path is not None}
    return values or None


def _build_case(
    rank: int, stratum: str, phase: str, sample_index: int, *,
    item_database_path: Path,
    selector_manifest_path: Path,
    representatives_path: Path | None,
    catalog_manifest_path: Path | None,
    catalog_data_path: Path | None,
) -> DevelopmentWaveCaseV1:
    return build_matrix_case_from_seed_v1(
        rank, stratum, sparse_full_wave_seed_v1(rank, stratum, phase, sample_index),
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path,
    )


def execute_sparse_full_wave_matrix_case_v1(
    case: DevelopmentWaveCaseV1,
    *,
    rank: int,
    stratum: str,
    phase: str,
    sample_index: int,
    policy_artifact: Mapping[str, Any],
    bridge_factory: Callable[[], Any],
    item_database: Mapping[str, Any],
    period_ms: int = 100,
    max_presses: int = 400,
    bridge_path: Path = DEFAULT_BRIDGE,
) -> dict[str, Any]:
    expected_seed = sparse_full_wave_seed_v1(rank, stratum, phase, sample_index)
    if case.dynamic_load.seed != expected_seed:
        raise ValueError("case seed differs from sparse full-wave namespace")
    routes = _policy_routes(policy_artifact, phase)
    route = mechanism_route_v1(case, item_database=item_database)
    guards = routes.get(route, ())
    contract = _execution_contract(
        phase=phase, route=route, guards=guards,
        policy_artifact=policy_artifact, period_ms=period_ms,
        max_presses=max_presses, bridge_path=bridge_path,
    )
    if guards:
        result = evaluate_sparse_guard_case_v1(
            case, route, guards, bridge_factory, phase=phase,
            period_ms=period_ms, max_presses=max_presses,
            item_database=item_database,
        )
    else:
        result = {
            "schema": SCHEMA,
            "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            **_case_tracking(case),
            "phase": phase,
            "mechanism_route": asdict(route),
            "guard_count": 0,
            "status": "NO_GUARD_FOR_EXACT_MECHANISM_ROUTE",
            "shared_cat_no_op": None,
            "guard_pairs": [],
            "all_guard_receipts_ready": True,
            "cat_no_op_lane_count": 0,
            "candidate_lane_count": 0,
            "full_press_lanes_retained": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }
    return {
        "schema": CASE_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_SPARSE_FULL_WAVE_CASE_ARTIFACT_NONVOTING",
        "matrix": {
            "rank": rank,
            "stratum": stratum,
            "phase": phase,
            "sample_index": sample_index,
            "seed": expected_seed,
            "seed_contract": "SPARSE_PHASE_DISJOINT_CROSS_CELL_RANDOMIZED_BLOCK_V1",
        },
        "case_projection": _case_projection(case, rank=rank, stratum=stratum),
        "mechanism_route": asdict(route),
        "execution_contract": contract,
        "result": result,
        "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def sparse_full_wave_case_filename_v1(
    rank: int, stratum: str, phase: str, sample_index: int,
) -> str:
    sparse_full_wave_seed_v1(rank, stratum, phase, sample_index)
    return f"rank{rank}-{stratum}-{phase}-s{sample_index:05d}.json"


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


def _existing_artifact_valid(
    path: Path, *, case: DevelopmentWaveCaseV1, rank: int, stratum: str,
    phase: str, sample_index: int, expected_route: MechanismRouteV1,
    expected_guards: tuple[SparseGuardV2, ...],
    expected_contract: Mapping[str, Any],
) -> bool:
    if not path.is_file():
        return False
    try:
        artifact = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    matrix = artifact.get("matrix")
    projection = artifact.get("case_projection")
    result = artifact.get("result")
    if not all(isinstance(row, Mapping) for row in (matrix, projection, result)):
        return False
    expected_seed = sparse_full_wave_seed_v1(rank, stratum, phase, sample_index)
    return bool(
        artifact.get("schema") == CASE_SCHEMA
        and artifact.get("status")
        == "COMPLETE_SPARSE_FULL_WAVE_CASE_ARTIFACT_NONVOTING"
        and artifact.get("full_press_lanes_retained") is False
        and matrix.get("rank") == rank
        and matrix.get("stratum") == stratum
        and matrix.get("phase") == phase
        and matrix.get("sample_index") == sample_index
        and matrix.get("seed") == expected_seed
        and projection.get("seed") == expected_seed
        and projection.get("request_sha256") == case.dynamic_load.request_sha256
        and projection.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and artifact.get("mechanism_route") == asdict(expected_route)
        and artifact.get("execution_contract") == expected_contract
        and result.get("seed") == expected_seed
        and result.get("request_sha256") == case.dynamic_load.request_sha256
        and result.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and result.get("guard_count") == len(expected_guards)
        and [pair.get("guard") for pair in result.get("guard_pairs") or []]
        == [asdict(guard) for guard in expected_guards]
    )


def _batch_initializer(config: Mapping[str, Any]) -> None:
    global _BATCH_CONTEXT
    policy = _read_json(Path(config["policy_path"]))
    _BATCH_CONTEXT = {
        **dict(config),
        "policy_artifact": policy,
        "routes": _policy_routes(policy, config["phase"]),
        "item_database": _load_item_database(Path(config["item_database_path"])),
    }


def _batch_execute_one(item: tuple[int, str, int, str]) -> dict[str, Any]:
    if _BATCH_CONTEXT is None:
        raise RuntimeError("sparse full-wave batch worker was not initialized")
    rank, stratum, sample_index, output_text = item
    config = _BATCH_CONTEXT
    phase = config["phase"]
    case = _build_case(
        rank, stratum, phase, sample_index,
        item_database_path=Path(config["item_database_path"]),
        selector_manifest_path=Path(config["selector_manifest_path"]),
        representatives_path=(
            Path(config["representatives_path"])
            if config.get("representatives_path") else None
        ),
        catalog_manifest_path=(
            Path(config["catalog_manifest_path"])
            if config.get("catalog_manifest_path") else None
        ),
        catalog_data_path=(
            Path(config["catalog_data_path"])
            if config.get("catalog_data_path") else None
        ),
    )
    artifact = execute_sparse_full_wave_matrix_case_v1(
        case, rank=rank, stratum=stratum, phase=phase,
        sample_index=sample_index, policy_artifact=config["policy_artifact"],
        bridge_factory=lambda: V14ProjectedDynamicV3Bridge(
            Path(config["bridge_path"]), cwd=Path(config["bridge_cwd"]),
        ),
        item_database=config["item_database"], period_ms=config["period_ms"],
        max_presses=config["max_presses"], bridge_path=Path(config["bridge_path"]),
    )
    path = Path(output_text)
    _write_json(path, artifact)
    return {
        "rank": rank,
        "stratum": stratum,
        "sample_index": sample_index,
        "seed": case.dynamic_load.seed,
        "output": str(path),
        "guard_count": artifact["result"]["guard_count"],
        "all_guard_receipts_ready": artifact["result"][
            "all_guard_receipts_ready"
        ],
    }


def run_sparse_full_wave_batch_v1(
    *,
    phase: str,
    policy_path: Path,
    samples_per_cell: int,
    shard_index: int,
    shard_count: int,
    workers: int,
    output_directory: Path,
    sample_start: int = 0,
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
    period_ms: int = 100,
    max_presses: int = 400,
) -> dict[str, Any]:
    """Run one recoverable modulo shard of compact full-wave case artifacts."""

    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    if type(samples_per_cell) is not int or samples_per_cell < 1:
        raise ValueError("samples_per_cell must be positive")
    if (
        type(sample_start) is not int or sample_start < 0
        or sample_start + samples_per_cell - 1 > MAX_SAMPLE_INDEX
    ):
        raise ValueError("sample range is outside its phase seed namespace")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be positive")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    policy_artifact = _read_json(policy_path)
    routes = _policy_routes(policy_artifact, phase)
    item_database = _load_item_database(item_database_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    config = {
        "phase": phase,
        "policy_path": str(policy_path),
        "bridge_path": str(bridge_path),
        "bridge_cwd": str(bridge_cwd),
        "item_database_path": str(item_database_path),
        "selector_manifest_path": str(selector_manifest_path),
        "representatives_path": str(representatives_path) if representatives_path else None,
        "catalog_manifest_path": (
            str(catalog_manifest_path) if catalog_manifest_path else None
        ),
        "catalog_data_path": str(catalog_data_path) if catalog_data_path else None,
        "period_ms": period_ms,
        "max_presses": max_presses,
    }
    global_items = [
        (rank, stratum, sample_index)
        for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
        for sample_index in range(sample_start, sample_start + samples_per_cell)
    ]
    assigned = [
        item for ordinal, item in enumerate(global_items)
        if ordinal % shard_count == shard_index
    ]
    pending: list[tuple[int, str, int, str]] = []
    skipped = 0
    for rank, stratum, sample_index in assigned:
        path = output_directory / sparse_full_wave_case_filename_v1(
            rank, stratum, phase, sample_index,
        )
        case = _build_case(
            rank, stratum, phase, sample_index,
            item_database_path=item_database_path,
            selector_manifest_path=selector_manifest_path,
            representatives_path=representatives_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_data_path=catalog_data_path,
        )
        route = mechanism_route_v1(case, item_database=item_database)
        guards = routes.get(route, ())
        contract = _execution_contract(
            phase=phase, route=route, guards=guards,
            policy_artifact=policy_artifact, period_ms=period_ms,
            max_presses=max_presses, bridge_path=bridge_path,
        )
        if _existing_artifact_valid(
            path, case=case, rank=rank, stratum=stratum, phase=phase,
            sample_index=sample_index, expected_route=route,
            expected_guards=guards, expected_contract=contract,
        ):
            skipped += 1
        else:
            pending.append((rank, stratum, sample_index, str(path)))
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if pending:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(pending)),
            initializer=_batch_initializer,
            initargs=(config,),
        ) as executor:
            future_rows = {
                executor.submit(_batch_execute_one, item): item for item in pending
            }
            for future in as_completed(future_rows):
                rank, stratum, sample_index, output = future_rows[future]
                try:
                    completed.append(future.result())
                except Exception as error:
                    failures.append({
                        "rank": rank,
                        "stratum": stratum,
                        "sample_index": sample_index,
                        "seed": sparse_full_wave_seed_v1(
                            rank, stratum, phase, sample_index,
                        ),
                        "output": output,
                        "error": f"{type(error).__name__}: {error}",
                    })
    order = lambda row: (
        MATRIX_RANKS.index(row["rank"]), MATRIX_STRATA.index(row["stratum"]),
        row["sample_index"],
    )
    completed.sort(key=order)
    failures.sort(key=order)
    return {
        "schema": BATCH_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_BATCH_SHARD" if not failures else "INCOMPLETE_BATCH_SHARD",
        "phase": phase,
        "sample_start": sample_start,
        "samples_per_cell": samples_per_cell,
        "global_item_count": len(global_items),
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_item_count": len(assigned),
        "skipped_existing_count": skipped,
        "executed_item_count": len(completed),
        "failed_item_count": len(failures),
        "workers": min(workers, len(pending)) if pending else 0,
        "assignment_contract": "GLOBAL_ITEM_ORDINAL_MOD_SHARD_COUNT",
        "completed": completed,
        "failures": failures,
        "full_press_lanes_retained": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _validated_phase_artifacts(
    artifacts: Iterable[Mapping[str, Any]], *, phase: str,
    policy_artifact: Mapping[str, Any], item_database: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    rows = list(artifacts)
    if not rows:
        raise ValueError(f"{phase} reducer requires case artifacts")
    routes = _policy_routes(policy_artifact, phase)
    seen: set[tuple[int, str, int]] = set()
    sample_sets = {
        (rank, stratum): set() for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
    }
    semantic: dict[str, Any] | None = None
    route_cells_by_seed: dict[tuple[Any, ...], dict[int, set[tuple[int, str]]]] = {}
    for artifact in rows:
        if (
            artifact.get("schema") != CASE_SCHEMA
            or artifact.get("status")
            != "COMPLETE_SPARSE_FULL_WAVE_CASE_ARTIFACT_NONVOTING"
            or artifact.get("full_press_lanes_retained") is not False
            or artifact.get("raw_chronicle_rows_loaded") is not False
            or artifact.get("voting_eligible") is not False
            or artifact.get("deployment_eligible") is not False
        ):
            raise ValueError("sparse full-wave case artifact schema or status differs")
        matrix = artifact.get("matrix")
        projection = artifact.get("case_projection")
        result = artifact.get("result")
        contract = artifact.get("execution_contract")
        if not all(isinstance(row, Mapping) for row in (
            matrix, projection, result, contract,
        )):
            raise ValueError("sparse full-wave artifact is incomplete")
        rank, stratum = matrix.get("rank"), matrix.get("stratum")
        sample_index, seed = matrix.get("sample_index"), matrix.get("seed")
        if matrix.get("phase") != phase or seed != sparse_full_wave_seed_v1(
            rank, stratum, phase, sample_index,
        ):
            raise ValueError("sparse full-wave artifact violates its phase namespace")
        key = (rank, stratum, sample_index)
        if key in seen:
            raise ValueError("duplicate sparse full-wave matrix case artifact")
        seen.add(key)
        sample_sets[(rank, stratum)].add(sample_index)
        case = _case_from_projection(projection)
        route = mechanism_route_v1(case, item_database=item_database)
        guards = routes.get(route, ())
        if (
            asdict(route) != artifact.get("mechanism_route")
            or result.get("mechanism_route") != asdict(route)
            or result.get("phase") != phase
            or result.get("seed") != seed
            or result.get("request_sha256") != projection.get("request_sha256")
            or result.get("dynamic_load_contract_sha256")
            != projection.get("dynamic_load_contract_sha256")
            or result.get("guard_count") != len(guards)
            or [pair.get("guard") for pair in result.get("guard_pairs") or []]
            != [asdict(guard) for guard in guards]
        ):
            raise ValueError("sparse full-wave result differs from its exact route")
        for pair, guard in zip(result.get("guard_pairs") or [], guards):
            if (
                not isinstance(pair, Mapping)
                or pair.get("schema") != "factored_sparse_guard_full_wave_pair/v1"
                or pair.get("phase") != phase
                or pair.get("seed") != seed
                or pair.get("mechanism_route") != asdict(route)
                or pair.get("guard") != asdict(guard)
                or pair.get("request_sha256") != projection.get("request_sha256")
                or pair.get("dynamic_load_contract_sha256")
                != projection.get("dynamic_load_contract_sha256")
                or pair.get("full_press_lane_retained") is not False
            ):
                raise ValueError("sparse guard pair identity or compactness differs")
        lineage = contract.get("policy_lineage")
        current_semantic = contract.get("semantic")
        if (
            contract.get("schema") != EXECUTION_CONTRACT_SCHEMA
            or not isinstance(lineage, Mapping)
            or not isinstance(current_semantic, Mapping)
            or lineage.get("source_schema") != policy_artifact.get("schema")
            or lineage.get("mechanism_route") != asdict(route)
            or lineage.get("guards") != [asdict(guard) for guard in guards]
        ):
            raise ValueError("sparse full-wave execution lineage differs")
        if semantic is None:
            semantic = dict(current_semantic)
        elif semantic != dict(current_semantic):
            raise ValueError("sparse full-wave phase mixes execution semantics")
        source_semantic = _source_semantic(policy_artifact, phase)
        for name in (
            "period_ms", "max_presses", "required_press_clock_configuration_mode",
            "bridge_version_tag",
        ):
            if current_semantic.get(name) != source_semantic.get(name):
                raise ValueError("sparse full-wave semantics differ from preceding phase")
        route_cells_by_seed.setdefault(_route_key(route), {}).setdefault(
            seed, set(),
        ).add((rank, stratum))
    balanced = {tuple(sorted(values)) for values in sample_sets.values()}
    if len(balanced) != 1 or next(iter(balanced), ()) == ():
        raise ValueError("sparse full-wave phase is incomplete across 12 cells")
    for per_seed in route_cells_by_seed.values():
        cell_sets = {tuple(sorted(cells)) for cells in per_seed.values()}
        if len(cell_sets) != 1:
            raise ValueError("exact route case composition is not balanced across seeds")
    return sorted(rows, key=lambda row: (
        MATRIX_RANKS.index(row["matrix"]["rank"]),
        MATRIX_STRATA.index(row["matrix"]["stratum"]),
        row["matrix"]["sample_index"],
    ))


def _effect_statistics(values: Iterable[float]) -> dict[str, Any]:
    rows = list(values)
    if not rows:
        return {
            "distinct_seed_count": 0,
            "mean_paired_effective_damage_delta": None,
            "standard_error_paired_effective_damage_delta": None,
            "lower_95_normal_effective_damage_delta_bound": None,
            "positive_seed_fraction": None,
        }
    average = mean(rows)
    standard_error = stdev(rows) / math.sqrt(len(rows)) if len(rows) >= 2 else None
    return {
        "distinct_seed_count": len(rows),
        "mean_paired_effective_damage_delta": average,
        "standard_error_paired_effective_damage_delta": standard_error,
        "lower_95_normal_effective_damage_delta_bound": (
            average - 1.96 * standard_error if standard_error is not None else None
        ),
        "positive_seed_fraction": sum(value > 0 for value in rows) / len(rows),
    }


def _shared_receipts_valid(result: Mapping[str, Any]) -> bool:
    shared = result.get("shared_cat_no_op")
    if not isinstance(shared, Mapping):
        return False
    terminals = (shared.get("cat_terminal"), shared.get("no_op_terminal"))
    semantics = shared.get("semantic_receipts")
    modes = shared.get("press_clock_configuration_modes")
    return bool(
        shared.get("status") == "COMPLETE_SHARED_CAT_NOOP_BASELINE"
        and shared.get("technical_receipts_ready") is True
        and all(
            isinstance(row, Mapping) and row.get("status") == "COMPLETED"
            for row in terminals
        )
        and isinstance(semantics, Mapping)
        and all(
            isinstance(semantics.get(name), Mapping)
            and semantics[name].get("valid") is True
            for name in ("cat", "no_op")
        )
        and (shared.get("no_op_identity_gate") or {}).get("exact") is True
        and isinstance(modes, Mapping)
        and all(
            modes.get(name) == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
            for name in ("cat", "no_op")
        )
    )


def _pair_valid_effect(
    pair: Mapping[str, Any], *, shared_receipts_valid: bool,
) -> float | None:
    delta = pair.get("paired_effective_damage_delta")
    terminal = pair.get("candidate_terminal")
    semantic = pair.get("candidate_semantic_receipt")
    candidate_receipts = bool(
        shared_receipts_valid
        and isinstance(terminal, Mapping) and terminal.get("status") == "COMPLETED"
        and isinstance(semantic, Mapping) and semantic.get("valid") is True
        and pair.get("candidate_press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    )
    intervention = pair.get("candidate_intervention")
    triggered = bool(
        pair.get("status") == "COMPLETE_TRIGGERED_SPARSE_GUARD_PAIR"
        and pair.get("effect_class") == "TRIGGERED_INTERVENTION"
        and pair.get("candidate_intervention_count") == 1
        and isinstance(intervention, Mapping)
        and type(intervention.get("decision_index")) is int
        and intervention.get("kind") == (pair.get("guard") or {}).get("kind")
        and intervention.get("guard") == pair.get("guard")
        and type(pair.get("accepted_prefix_presses_verified")) is int
        and pair.get("strict_single_intervention_verified") is True
        and pair.get("candidate_branch_action_accepted") is True
        and pair.get("active_cat_fallback_verified") is False
    )
    fallback = bool(
        pair.get("status") == "EXACT_CAT_NO_TRIGGER_FALLBACK"
        and pair.get("effect_class") == "EXACT_CAT_FALLBACK_ZERO"
        and pair.get("candidate_intervention_count") == 0
        and pair.get("strict_single_intervention_verified") is False
        and pair.get("active_cat_fallback_verified") is True
        and (pair.get("active_cat_fallback_identity_gate") or {}).get("exact") is True
        and delta == 0.0
    )
    if (
        candidate_receipts
        and pair.get("technical_receipts_ready") is True
        and pair.get("comparison_ready") is True
        and _finite_number(delta)
        and (triggered or fallback)
    ):
        return float(delta)
    return None


def _guard_reduction(
    rows: Iterable[Mapping[str, Any]],
    route: MechanismRouteV1,
    guard: SparseGuardV2,
) -> dict[str, Any]:
    per_seed_cases: dict[int, list[Mapping[str, Any]]] = {}
    for artifact in rows:
        if artifact.get("mechanism_route") != asdict(route):
            continue
        pair = next(
            candidate for candidate in artifact["result"]["guard_pairs"]
            if candidate.get("guard") == asdict(guard)
        )
        per_seed_cases.setdefault(artifact["matrix"]["seed"], []).append({
            "pair": pair,
            "shared_receipts_valid": _shared_receipts_valid(artifact["result"]),
        })
    per_seed = []
    complete: dict[int, float] = {}
    for seed, case_rows in sorted(per_seed_cases.items()):
        pairs = [row["pair"] for row in case_rows]
        effects = [
            _pair_valid_effect(
                row["pair"], shared_receipts_valid=row["shared_receipts_valid"],
            )
            for row in case_rows
        ]
        unknown = sum(value is None for value in effects)
        triggered = sum(
            pair.get("effect_class") == "TRIGGERED_INTERVENTION" for pair in pairs
        )
        fallback = sum(
            pair.get("effect_class") == "EXACT_CAT_FALLBACK_ZERO" for pair in pairs
        )
        value = mean(value for value in effects if value is not None) if not unknown else None
        if value is not None:
            complete[seed] = value
        per_seed.append({
            "seed": seed,
            "status": "COMPLETE_BALANCED_SEED" if value is not None else "UNKNOWN_SEED_NOT_IMPUTED",
            "assigned_case_count": len(pairs),
            "triggered_case_count": triggered,
            "exact_cat_fallback_case_count": fallback,
            "unknown_case_count": unknown,
            "paired_effective_damage_delta": value,
        })
    return {
        "mechanism_route": asdict(route),
        "guard": asdict(guard),
        "assigned_case_count": sum(len(pairs) for pairs in per_seed_cases.values()),
        "assigned_seed_count": len(per_seed_cases),
        "complete_seed_count": len(complete),
        "triggered_seed_count": sum(
            row["triggered_case_count"] > 0 for row in per_seed
        ),
        "exact_cat_fallback_only_seed_count": sum(
            row["exact_cat_fallback_case_count"] == row["assigned_case_count"]
            for row in per_seed
        ),
        "unknown_seed_count": sum(row["unknown_case_count"] > 0 for row in per_seed),
        "per_seed_effects": per_seed,
        "expected_route_policy_effect_statistics": _effect_statistics(
            complete.values()
        ),
        "aggregation_contract": (
            "ALL_EXACT_ROUTE_CASES_MEAN_ONCE_PER_SHARED_SEED;"
            "ANY_INVALID_CASE_MAKES_SEED_UNKNOWN;NO_UNKNOWN_ZERO_IMPUTATION"
        ),
        "positive_seed_fraction_role": "DIAGNOSTIC_ONLY_NOT_AN_AUTHORIZATION_GATE",
    }


def _phase_reductions(
    rows: Iterable[Mapping[str, Any]],
    routes: Mapping[MechanismRouteV1, tuple[SparseGuardV2, ...]],
) -> list[dict[str, Any]]:
    materialized = list(rows)
    return [
        _guard_reduction(materialized, route, guard)
        for route in sorted(routes, key=_route_key)
        for guard in routes[route]
    ]


def _passes_expected_effect_gate(row: Mapping[str, Any], minimum: int) -> bool:
    stats = row["expected_route_policy_effect_statistics"]
    lower = stats["lower_95_normal_effective_damage_delta_bound"]
    return bool(
        row["assigned_seed_count"] >= minimum
        and row["complete_seed_count"] == row["assigned_seed_count"]
        and row["unknown_seed_count"] == 0
        and lower is not None and lower > 0
    )


def authorize_sparse_guard_transfer_v1(
    shortlist_artifact: Mapping[str, Any],
    transfer_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
    min_distinct_seeds: int = 8,
) -> dict[str, Any]:
    """Authorize each exact-route guard independently on transfer waves."""

    if min_distinct_seeds < 8:
        raise ValueError("transfer support may not be lower than eight seeds")
    routes = validate_sparse_shortlist_v5(shortlist_artifact)
    if not any(routes.values()):
        raise ValueError("transfer evaluation requires a nonempty sparse shortlist")
    rows = _validated_phase_artifacts(
        transfer_artifacts, phase=TRANSFER_PHASE,
        policy_artifact=shortlist_artifact, item_database=item_database,
    )
    transfer_seeds = sorted({row["matrix"]["seed"] for row in rows})
    training_seeds = list(shortlist_artifact.get("training_seeds") or [])
    if set(training_seeds) & set(transfer_seeds):
        raise ValueError("training and sparse transfer seeds overlap")
    decisions = _phase_reductions(rows, routes)
    for row in decisions:
        stats = row["expected_route_policy_effect_statistics"]
        if row["unknown_seed_count"]:
            status = "REJECTED_UNKNOWN_TRANSFER_EFFECTS_NOT_IMPUTED"
        elif row["assigned_seed_count"] < min_distinct_seeds:
            status = "REJECTED_INSUFFICIENT_TRANSFER_SEEDS"
        elif stats["lower_95_normal_effective_damage_delta_bound"] is None:
            status = "REJECTED_TRANSFER_LOWER_BOUND_UNDEFINED"
        elif stats["lower_95_normal_effective_damage_delta_bound"] <= 0:
            status = "REJECTED_TRANSFER_EXPECTED_EFFECT_LOWER_BOUND_NOT_POSITIVE"
        else:
            status = "AUTHORIZED_FOR_UNTOUCHED_FRESH_NONVOTING"
        row["authorization_status"] = status
        row["authorized_for_fresh"] = _passes_expected_effect_gate(
            row, min_distinct_seeds,
        )
    authorized_routes = []
    for route in sorted(routes, key=_route_key):
        guards = [
            decision["guard"] for decision in decisions
            if decision["mechanism_route"] == asdict(route)
            and decision["authorized_for_fresh"]
        ]
        if guards:
            authorized_routes.append({
                "mechanism_route": asdict(route),
                "guards": guards,
            })
    semantic = deepcopy(rows[0]["execution_contract"]["semantic"])
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "TRANSFER_AUTHORIZATION_COMPLETE_NONVOTING",
        "source_shortlist_schema": shortlist_artifact.get("schema"),
        "source_learner_schema": (shortlist_artifact.get("learner") or {}).get("schema"),
        "training_seeds": sorted(training_seeds),
        "transfer_seeds": transfer_seeds,
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "transfer_case_count": len(rows),
        "execution_semantic_contract": semantic,
        "guard_authorizations": decisions,
        "evaluated_guard_count": len(decisions),
        "authorized_routes": authorized_routes,
        "authorized_guard_count": sum(len(row["guards"]) for row in authorized_routes),
        "authorization_gate": (
            "COMPLETE_BALANCED_SAME_SEED_EXPECTED_PAIRED_EFFECT;"
            "POSITIVE_LOWER_95_NORMAL_BOUND;POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY"
        ),
        "full_wave_candidate_policy_evaluated": True,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def reduce_sparse_guard_fresh_v1(
    authorization_artifact: Mapping[str, Any],
    fresh_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
    min_distinct_seeds: int = 8,
) -> dict[str, Any]:
    """Reduce untouched full waves for guards frozen by transfer authorization."""

    if min_distinct_seeds < 8:
        raise ValueError("fresh support may not be lower than eight seeds")
    routes = _authorized_route_guards(authorization_artifact)
    if not routes:
        raise ValueError("fresh evaluation requires at least one authorized guard")
    rows = _validated_phase_artifacts(
        fresh_artifacts, phase=FRESH_PHASE,
        policy_artifact=authorization_artifact, item_database=item_database,
    )
    fresh_seeds = sorted({row["matrix"]["seed"] for row in rows})
    prior = set(authorization_artifact.get("training_seeds") or []) | set(
        authorization_artifact.get("transfer_seeds") or []
    )
    if prior & set(fresh_seeds):
        raise ValueError("untouched fresh seeds overlap an earlier phase")
    reductions = _phase_reductions(rows, routes)
    for row in reductions:
        stats = row["expected_route_policy_effect_statistics"]
        if row["unknown_seed_count"]:
            status = "UNKNOWN_FRESH_EFFECTS_NOT_IMPUTED"
        elif row["assigned_seed_count"] < min_distinct_seeds:
            status = "INSUFFICIENT_UNTOUCHED_FRESH_SEEDS"
        elif stats["lower_95_normal_effective_damage_delta_bound"] is None:
            status = "UNTOUCHED_FRESH_LOWER_BOUND_UNDEFINED"
        elif stats["lower_95_normal_effective_damage_delta_bound"] <= 0:
            status = "UNTOUCHED_FRESH_EXPECTED_EFFECT_LOWER_BOUND_NOT_POSITIVE"
        else:
            status = "PASSED_UNTOUCHED_FRESH_EXPECTED_EFFECT_GATE_NONVOTING"
        row["fresh_gate_status"] = status
        row["passed_untouched_fresh_gate"] = _passes_expected_effect_gate(
            row, min_distinct_seeds,
        )
    all_complete = all(row["unknown_seed_count"] == 0 for row in reductions)
    return {
        "schema": FRESH_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_UNTOUCHED_SPARSE_FULL_WAVE_NONVOTING"
            if all_complete else "INCOMPLETE_UNTOUCHED_SPARSE_FULL_WAVE_NONVOTING"
        ),
        "training_seeds": sorted(authorization_artifact.get("training_seeds") or []),
        "transfer_seeds": sorted(authorization_artifact.get("transfer_seeds") or []),
        "fresh_seeds": fresh_seeds,
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "fresh_case_count": len(rows),
        "execution_semantic_contract": deepcopy(
            rows[0]["execution_contract"]["semantic"]
        ),
        "guard_results": reductions,
        "evaluated_guard_count": len(reductions),
        "passed_fresh_guard_count": sum(
            row["passed_untouched_fresh_gate"] for row in reductions
        ),
        "all_semantic_terminal_clock_receipts_valid": all_complete,
        "comparison_ready": bool(reductions and all_complete),
        "fresh_gate": (
            "COMPLETE_BALANCED_SAME_SEED_EXPECTED_PAIRED_EFFECT;"
            "POSITIVE_LOWER_95_NORMAL_BOUND;POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY"
        ),
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "scientific_run_launched": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    batch = commands.add_parser(
        "batch", help="run one recoverable modulo-assigned compact scheduler shard"
    )
    batch.add_argument("--phase", choices=PHASES, required=True)
    batch.add_argument("--policy", type=Path, required=True)
    batch.add_argument("--sample-start", type=int, default=0)
    batch.add_argument("--samples-per-cell", type=int, required=True)
    batch.add_argument("--shard-index", type=int, required=True)
    batch.add_argument("--shard-count", type=int, required=True)
    batch.add_argument("--workers", type=int, required=True)
    batch.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    batch.add_argument("--bridge-cwd", type=Path, default=DEFAULT_BRIDGE_CWD)
    batch.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    batch.add_argument("--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST)
    batch.add_argument("--representatives", type=Path)
    batch.add_argument("--catalog-manifest", type=Path)
    batch.add_argument("--catalog-data", type=Path)
    batch.add_argument("--period-ms", type=int, default=100)
    batch.add_argument("--max-presses", type=int, default=400)
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--summary", type=Path, required=True)

    authorize = commands.add_parser(
        "authorize", help="reduce transfer artifacts and freeze authorized guards"
    )
    authorize.add_argument("--shortlist", type=Path, required=True)
    authorize.add_argument("--input", type=Path, nargs="+", required=True)
    authorize.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    authorize.add_argument("--min-distinct-seeds", type=int, default=8)
    authorize.add_argument("--output", type=Path, required=True)

    fresh = commands.add_parser(
        "fresh", help="reduce untouched fresh artifacts for frozen guards"
    )
    fresh.add_argument("--authorization", type=Path, required=True)
    fresh.add_argument("--input", type=Path, nargs="+", required=True)
    fresh.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    fresh.add_argument("--min-distinct-seeds", type=int, default=8)
    fresh.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "batch":
        result = run_sparse_full_wave_batch_v1(
            phase=args.phase, policy_path=args.policy,
            samples_per_cell=args.samples_per_cell,
            sample_start=args.sample_start, shard_index=args.shard_index,
            shard_count=args.shard_count, workers=args.workers,
            output_directory=args.output_dir, bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd, item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data, period_ms=args.period_ms,
            max_presses=args.max_presses,
        )
        _write_json(args.summary, result)
        if result["failed_item_count"]:
            raise SystemExit(1)
        return
    item_database = _load_item_database(args.item_db)
    if args.command == "authorize":
        result = authorize_sparse_guard_transfer_v1(
            _read_json(args.shortlist),
            [_read_json(path) for path in args.input],
            item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
        )
    else:
        result = reduce_sparse_guard_fresh_v1(
            _read_json(args.authorization),
            [_read_json(path) for path in args.input],
            item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
        )
    _write_json(args.output, result)


if __name__ == "__main__":
    main()


__all__ = (
    "SCHEMA", "CASE_SCHEMA", "BATCH_SCHEMA", "AUTHORIZATION_SCHEMA",
    "FRESH_SCHEMA", "TRANSFER_PHASE", "FRESH_PHASE", "PHASE_SEED_BASES",
    "sparse_full_wave_seed_v1", "validate_sparse_shortlist_v5",
    "evaluate_sparse_guard_case_v1", "execute_sparse_full_wave_matrix_case_v1",
    "sparse_full_wave_case_filename_v1", "run_sparse_full_wave_batch_v1",
    "authorize_sparse_guard_transfer_v1", "reduce_sparse_guard_fresh_v1",
)
