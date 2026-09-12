"""Freeze the next development-only search for the Cat Fury gap.

The prior Horizon-v2 result is admitted only as a negative diagnostic.  It
does not seed this design, nominate ``ww_wait``, or authorize extending the
513--768 seed family.  Search ranking uses the generic lane whose GUID/identity
discovery is outcome-free.  Its simulator-health branch availability is still
read from historical same-wave kill-budget proxy capsules; it never uses
candidate/simulator outcomes or future policy input and remains development/
training-only, non-heldout, and comparison-ineligible.

This module builds and validates plans.  It never starts a simulator, stages a
remote job, selects a live policy, or changes Cat2.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import cat2_capability_manifest_v1 as cat2_manifest_v1
from . import cat2new_fury_cat_gap_policy_v1 as cat_gap_policy_v1
from . import cat_deployed_source_manifest_v1 as cat_manifest_v1
from . import chronicle_old50_exact_fury_dynamic_v3_adapter_v1 as adapter_v1
from . import chronicle_old50_warrior_slot_substitution_v2 as selector_v2
from . import contra260817_source_manifest_v1 as contra260817_manifest_v1
from .fury_multiseed_evaluation_v2 import derive_seed_set
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "fury_cat_gap_search_plan/v1"
REVISION = "v1_successive_halving_generic_old50_dual_baseline"
STATUS_BLOCKED = "BLOCKED_REAL_470_WAVE_ADAPTER_AND_POLICY_EXECUTOR_NOT_VALIDATED"
STATUS_ENVIRONMENT_BOUND = (
    "PREMATERIALIZATION_ENVIRONMENT_BOUND_REAL_ADAPTER_NOT_VALIDATED"
)
STATUS_ADAPTER_READY = "ADAPTER_VALIDATED_PLAN_ONLY_POLICY_EXECUTOR_PENDING"
STATUS_MECHANICS_READY = (
    "VARIABLE_LANE_EXECUTOR_MECHANICS_VALIDATED_HEAVY_ADMISSION_PENDING"
)
STATUS_HEAVY_PREPARED = (
    "FORMAL_343_RUNTIME_CLOSED_READY_FOR_CAPACITY_PILOT_ONLY"
)
STATUS_HEAVY_READY = "FORMAL_343_RUNTIME_AND_SIX_NODE_HEAVY_ADMISSION_READY"
ENVIRONMENT_BINDING_SCHEMA = "fury_cat_gap_prematerialization_environment/v1"
ENVIRONMENT_BINDING_STATUS = "FROZEN_MATERIALIZATION_INPUTS_ADAPTER_PENDING"
EXECUTION_SURFACE_ADMISSION_SCHEMA = (
    "fury_cat_gap_variable_lane_execution_surface_admission/v1"
)
ADAPTER_ADMISSION_RECEIPT_FIELDS = frozenset(
    {
        "real_artifact_validated",
        "validation_method",
        "validated_total_wave_count",
        "validated_generic_wave_count",
        "validated_pdf_wave_count",
        "validated_generic_instance_count",
        "validated_pdf_instance_count",
        "fixed_player_semantic_sha256",
        "materialized_manifest_content_sha256",
        "validated_source_bindings",
        "overlay_manifest_file_sha256",
        "materialization_parameters_sha256",
        "health_branch_selection_counts",
        "pre_materialization_environment_binding_sha256",
    }
)

EXPECTED_HORIZON_CONFIRMATION_ID = (
    "1430f11be4b5a4647aac7818b6e2012bfc830c15ec372328ea00d782261d1ded"
)
EXPECTED_HORIZON_ANALYSIS_CONTRACT_SHA256 = (
    "dde1d2af7f7b51f394dc8346f06de5b77ef4712b74f2f75ca3c61132dcba2bc0"
)
EXPECTED_HORIZON_COMPACT_SHA256 = (
    "d34ada4e487998147c098cba4b9f2f72537d9605c54ac6e758c16905dc872236"
)
EXPECTED_HORIZON_ARMS = (
    "ww_hamstring_cat_timing",
    "ww_wait_cat_timing",
    "bt_hamstring_cat_timing",
)
FORBIDDEN_HORIZON_SEEDS = frozenset(range(513, 769))

EXPECTED_TOTAL_WAVES = 470
EXPECTED_GENERIC_WAVES = 343
EXPECTED_PDF_WAVES = 127
EXPECTED_GENERIC_INSTANCES = 14
EXPECTED_PDF_INSTANCES = 6
EXPECTED_OVERLAY_MANIFEST_SHA256 = (
    adapter_v1.FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256
)
EXPECTED_OVERLAY_MANIFEST_FILE_SHA256 = (
    adapter_v1.FORMAL_OVERLAY_MANIFEST_FILE_SHA256
)

FIXED_TALENTS = "30205020332-05050005025010051"
FIXED_MAIN_HAND_ITEM_ID = 17076
FIXED_MAIN_HAND_ENCHANT_ID = 1900
FIXED_WEAPON_MODE = "TWO_HAND"
FIXED_ARMOR_MAGNITUDE_BY_DEBUFF = {"expose_armor": 1700}

APPROVED_CAT_GAP_POLICY_MODULE = "o2o_dps.cat2new_fury_cat_gap_policy_v1"
APPROVED_CAT_GAP_POLICY_SOURCE_PATH = (
    "o2o_dps/cat2new_fury_cat_gap_policy_v1.py"
)
APPROVED_CAT_GAP_POLICY_TEST_PATH = "tests/test_cat2new_fury_cat_gap_policy_v1.py"
APPROVED_CAT_GAP_POLICY_SOURCE_SHA256 = (
    "b2fd40fd7bdb9352915ea8cc34c02166293316e44bb68019845287c214847b6c"
)
APPROVED_CAT_GAP_POLICY_TEST_SHA256 = (
    "6f880db262c0c8cef1f0c01dc62d5b7ae4a67e52fb0ae4a52eb2480c8d0fa366"
)

BASELINE_POLICY_IDS = (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
SEED_NAMESPACE = "brainofcat.fury.cat-gap-search.v1.2026-09-12"

DEFAULT_HORIZON_OBSERVATION = (
    PROJECT_ROOT
    / ".hpc-local"
    / "horizon-remote-attempts"
    / EXPECTED_HORIZON_CONFIRMATION_ID
    / "attempt-002"
    / "v2-diagnostic-negative-result-observation.json"
)


class FuryCatGapSearchPlanV1Error(RuntimeError):
    """A search plan would reuse evidence or widen the development boundary."""


PARAMETER_AXES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("heroic_strike_base_rage", tuple(range(35, 76, 5))),
    ("cleave_base_rage", tuple(range(35, 76, 5))),
    ("queue_cancel_margin_rage", tuple(range(4, 17, 2))),
    ("primary_cooldown_reserve_window_ms", tuple(range(800, 1801, 200))),
    ("bloodrage_trigger_below_rage", tuple(range(15, 36, 5))),
    ("single_target_priority", ("BLOODTHIRST_FIRST", "WHIRLWIND_FIRST")),
    ("multi_target_priority", ("BLOODTHIRST_FIRST", "WHIRLWIND_FIRST")),
    ("hamstring_min_rage", tuple(range(10, 71, 10))),
    ("hamstring_min_primary_gap_ms", tuple(range(1000, 2001, 200))),
    ("slam_min_swing_remaining_ms", tuple(range(1600, 2601, 200))),
    ("slam_min_primary_gap_ms", tuple(range(800, 1801, 200))),
    ("execute_reserve_rage", tuple(range(0, 41, 10))),
    ("wait_ms", (50, 75, 100, 125, 150)),
)

_HALTON_BASES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)

_SOURCE_NEIGHBORHOOD_ANCHORS: tuple[tuple[str, JSONMap], ...] = (
    (
        "cat_source_neighborhood_not_exact_cat",
        {
            "heroic_strike_base_rage": 35,
            "cleave_base_rage": 35,
            "queue_cancel_margin_rage": 8,
            "primary_cooldown_reserve_window_ms": 1400,
            "bloodrage_trigger_below_rage": 30,
            "single_target_priority": "BLOODTHIRST_FIRST",
            "multi_target_priority": "WHIRLWIND_FIRST",
            "hamstring_min_rage": 10,
            "hamstring_min_primary_gap_ms": 1400,
            "slam_min_swing_remaining_ms": 2000,
            "slam_min_primary_gap_ms": 1400,
            "execute_reserve_rage": 30,
            "wait_ms": 100,
        },
    ),
    (
        "contra260817_source_neighborhood_not_exact_contra",
        {
            "heroic_strike_base_rage": 60,
            "cleave_base_rage": 60,
            "queue_cancel_margin_rage": 8,
            "primary_cooldown_reserve_window_ms": 1400,
            "bloodrage_trigger_below_rage": 25,
            "single_target_priority": "BLOODTHIRST_FIRST",
            "multi_target_priority": "WHIRLWIND_FIRST",
            "hamstring_min_rage": 70,
            "hamstring_min_primary_gap_ms": 1400,
            "slam_min_swing_remaining_ms": 1800,
            "slam_min_primary_gap_ms": 1000,
            "execute_reserve_rage": 30,
            "wait_ms": 100,
        },
    ),
)

_STAGE_SPECS = (
    ("successive_halving_1", 64, 16, 42, 16, 0),
    ("successive_halving_2", 16, 4, 112, 32, 10_000),
    ("successive_halving_3", 4, 2, 343, 64, 20_000),
)
_SELECTION_SPEC = ("selection_validation", 2, 1, 343, 256, 30_000)
_CONFIRMATION_SPEC = ("future_confirmation_reserved", 1, 1, None, 1_000, 40_000)

SCIENTIFIC_BOUNDARY = {
    "simulator_only": True,
    "development_only": True,
    "old50_is_development_training_only": True,
    "old50_heldout_performance_evidence_eligible": False,
    "old50_comparison_outcome_eligible": False,
    "historical_policy_voting_eligible": False,
    "historical_health_proxy_availability_used": True,
    "candidate_or_simulator_outcome_used_for_health_selection": False,
    "future_information_used_for_health_selection": False,
    "health_hypothesis_eligible_for_comparison": False,
    "live_fidelity": False,
    "scientific_result_available": False,
    "policy_selected": False,
    "policy_promoted": False,
    "deployment_allowed": False,
    "heavy_execution_started": False,
}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapSearchPlanV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryCatGapSearchPlanV1Error(f"{label} must be an array")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _seal(core: Mapping[str, Any]) -> JSONMap:
    result = deepcopy(dict(core))
    result["plan_sha256"] = sha256_json(core)
    return result


def _sha256_file_bytes(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise FuryCatGapSearchPlanV1Error(
            f"could not read formal overlay manifest: {error}"
        ) from error


def _read_json_source_path(path: str | Path, label: str) -> tuple[JSONMap, str]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = resolved.read_bytes()
        logical = gzip.decompress(payload) if resolved.suffix == ".gz" else payload
        value = json.loads(logical.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, gzip.BadGzipFile) as error:
        raise FuryCatGapSearchPlanV1Error(
            f"could not read {label} {resolved}: {error}"
        ) from error
    return dict(_mapping(value, label)), hashlib.sha256(payload).hexdigest()


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    result = deepcopy(dict(core))
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    return result


def _radical_inverse(index: int, base: int) -> float:
    inverse = 1.0 / base
    factor = inverse
    value = 0.0
    while index:
        index, digit = divmod(index, base)
        value += digit * factor
        factor *= inverse
    return value


def _validate_parameters(parameters: Mapping[str, Any]) -> JSONMap:
    expected = {name for name, _ in PARAMETER_AXES}
    if set(parameters) != expected:
        raise FuryCatGapSearchPlanV1Error("candidate parameter field set differs")
    result: JSONMap = {}
    for name, values in PARAMETER_AXES:
        value = parameters[name]
        if type(value) is not type(values[0]) or value not in values:
            raise FuryCatGapSearchPlanV1Error(
                f"candidate parameter {name} is outside the frozen axis"
            )
        result[name] = value
    return result


def _approved_cat_gap_policy_binding_v1() -> JSONMap:
    """Bind the independently accepted executable 13-axis policy pair."""

    if (
        cat_gap_policy_v1.PARAMETER_SCHEMA
        != "fury_cat_gap_policy_parameters/v1"
        or cat_gap_policy_v1.PARAMETER_AXES != PARAMETER_AXES
    ):
        raise FuryCatGapSearchPlanV1Error(
            "approved Cat-gap policy schema or axes differ from the search plan"
        )
    source_path = PROJECT_ROOT / APPROVED_CAT_GAP_POLICY_SOURCE_PATH
    test_path = PROJECT_ROOT / APPROVED_CAT_GAP_POLICY_TEST_PATH
    if (
        hashlib.sha256(source_path.read_bytes()).hexdigest()
        != APPROVED_CAT_GAP_POLICY_SOURCE_SHA256
        or hashlib.sha256(test_path.read_bytes()).hexdigest()
        != APPROVED_CAT_GAP_POLICY_TEST_SHA256
    ):
        raise FuryCatGapSearchPlanV1Error(
            "approved Cat-gap policy source/test pair differs from independent review"
        )
    return {
        "module": APPROVED_CAT_GAP_POLICY_MODULE,
        "implementation_revision": cat_gap_policy_v1.IMPLEMENTATION_REVISION,
        "parameter_schema": cat_gap_policy_v1.PARAMETER_SCHEMA,
        "source_path": APPROVED_CAT_GAP_POLICY_SOURCE_PATH,
        "source_sha256": APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
        "test_path": APPROVED_CAT_GAP_POLICY_TEST_PATH,
        "test_sha256": APPROVED_CAT_GAP_POLICY_TEST_SHA256,
        "independent_review_status": "PASS_13_OF_13_AXES_RUNTIME_REACHABLE",
    }


def build_candidate_design_v1(count: int = 64) -> tuple[JSONMap, ...]:
    """Return a deterministic source-anchored, low-discrepancy design."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValueError("candidate count must be an integer >= 2")
    rows: list[JSONMap] = []
    seen: set[str] = set()

    def append(parameters: Mapping[str, Any], origin: str) -> None:
        normalized = _validate_parameters(parameters)
        identity = sha256_json(normalized)
        if identity in seen:
            return
        seen.add(identity)
        rows.append(
            {
                "candidate_id": f"cat-gap-v1-{identity[:16]}",
                "parameter_sha256": identity,
                "origin": origin,
                "parameters": normalized,
                "legacy_horizon_arm_id": None,
                "horizon_v2_ranking_used": False,
            }
        )

    for label, parameters in _SOURCE_NEIGHBORHOOD_ANCHORS:
        append(parameters, label)
    index = 1
    while len(rows) < count:
        parameters = {
            name: values[
                min(
                    len(values) - 1,
                    int(_radical_inverse(index, base) * len(values)),
                )
            ]
            for (name, values), base in zip(
                PARAMETER_AXES, _HALTON_BASES, strict=True
            )
        }
        append(parameters, "predeclared_halton_design_no_outcome_feedback")
        index += 1
        if index > count * 100:
            raise FuryCatGapSearchPlanV1Error(
                "could not construct the unique candidate design"
            )
    return tuple(rows)


def validate_horizon_negative_observation_v1(value: Mapping[str, Any]) -> JSONMap:
    raw = deepcopy(dict(_mapping(value, "Horizon-v2 observation")))
    selection = _mapping(raw.get("selection_gate"), "selection_gate")
    identity = _mapping(raw.get("analysis_identity"), "analysis_identity")
    remote = _mapping(raw.get("remote_results"), "remote_results")
    interpretation = _mapping(raw.get("interpretation"), "interpretation")
    counts = _mapping(raw.get("receipt_counts"), "receipt_counts")
    contrasts = [
        _mapping(row, "contrast") for row in _array(raw.get("contrasts"), "contrasts")
    ]
    contrast_cells = {
        (row.get("arm_id"), row.get("baseline_policy_id")) for row in contrasts
    }
    expected_cells = {
        (arm_id, baseline_id)
        for arm_id in EXPECTED_HORIZON_ARMS
        for baseline_id in BASELINE_POLICY_IDS
    }
    if (
        raw.get("schema")
        != "fury_cat2_horizon_v2_diagnostic_result_observation/v1"
        or raw.get("confirmation_id") != EXPECTED_HORIZON_CONFIRMATION_ID
        or raw.get("status") != "COMPLETE_NO_SELECTION_NEGATIVE_RESULT_PRESERVED"
        or identity.get("analysis_contract_sha256")
        != EXPECTED_HORIZON_ANALYSIS_CONTRACT_SHA256
        or remote.get("compact_sha256") != EXPECTED_HORIZON_COMPACT_SHA256
        or selection.get("status") != "NO_SELECTION"
        or selection.get("selected_arm_id") is not None
        or selection.get("passing_arm_ids") != []
        or contrast_cells != expected_cells
        or len(contrasts) != len(expected_cells)
        or set(counts) != set(EXPECTED_HORIZON_ARMS)
        or any(counts.get(arm_id) != 256 for arm_id in EXPECTED_HORIZON_ARMS)
        or interpretation.get("candidate_beats_both_baselines") is not False
        or interpretation.get("same_seed_extension_or_retuning_allowed") is not False
        or interpretation.get("policy_promotion_performed") is not False
        or interpretation.get("deployment_allowed") is not False
    ):
        raise FuryCatGapSearchPlanV1Error(
            "Horizon-v2 input is not the frozen dual-baseline NO_SELECTION result"
        )
    return raw


def _canonical_horizon_negative_observation_v1() -> JSONMap:
    return {
        "schema": "fury_cat2_horizon_v2_diagnostic_result_observation/v1",
        "confirmation_id": EXPECTED_HORIZON_CONFIRMATION_ID,
        "status": "COMPLETE_NO_SELECTION_NEGATIVE_RESULT_PRESERVED",
        "receipt_counts": {
            arm_id: 256 for arm_id in EXPECTED_HORIZON_ARMS
        },
        "analysis_identity": {
            "analysis_contract_sha256": (
                EXPECTED_HORIZON_ANALYSIS_CONTRACT_SHA256
            )
        },
        "remote_results": {"compact_sha256": EXPECTED_HORIZON_COMPACT_SHA256},
        "selection_gate": {
            "status": "NO_SELECTION",
            "selected_arm_id": None,
            "passing_arm_ids": [],
        },
        "contrasts": [
            {"arm_id": arm_id, "baseline_policy_id": baseline_id}
            for arm_id in EXPECTED_HORIZON_ARMS
            for baseline_id in BASELINE_POLICY_IDS
        ],
        "interpretation": {
            "candidate_beats_both_baselines": False,
            "same_seed_extension_or_retuning_allowed": False,
            "policy_promotion_performed": False,
            "deployment_allowed": False,
        },
    }


def _seed_contract() -> JSONMap:
    specs = (*_STAGE_SPECS, _SELECTION_SPEC, _CONFIRMATION_SPEC)
    phases: JSONMap = {}
    occupied: set[int] = set()
    for stage_id, _, _, _, count, counter_start in specs:
        seeds = derive_seed_set(
            SEED_NAMESPACE,
            stage_id,
            count,
            counter_start=counter_start,
        )
        if occupied.intersection(seeds) or FORBIDDEN_HORIZON_SEEDS.intersection(seeds):
            raise FuryCatGapSearchPlanV1Error("fresh seed families are not disjoint")
        occupied.update(seeds)
        phases[stage_id] = {
            "count": count,
            "counter_start": counter_start,
            "master_seeds": list(seeds),
            "seed_list_sha256": sha256_json(list(seeds)),
            "fixed_sample_no_optional_stopping": True,
        }
    return {
        "algorithm": "sha256_namespace_phase_counter_u63_v1",
        "namespace": SEED_NAMESPACE,
        "forbidden_prior_seed_interval": [513, 768],
        "all_phase_seed_sets_pairwise_disjoint": True,
        "phases": phases,
    }


def _stage_row(
    spec: tuple[str, int, int, int | None, int, int], seed_contract: Mapping[str, Any]
) -> JSONMap:
    stage_id, candidates, retained, scenarios, seed_count, _ = spec
    baselines = len(BASELINE_POLICY_IDS)
    if scenarios is None:
        return {
            "stage_id": stage_id,
            "candidate_count": candidates,
            "retained_candidate_count": retained,
            "scenario_count": None,
            "master_seed_count": seed_count,
            "seed_list_sha256": _mapping(
                _mapping(seed_contract.get("phases"), "seed phases").get(stage_id),
                stage_id,
            )["seed_list_sha256"],
            "policy_rollout_cost": {
                "formula": "future_new_scenario_count * 1000 * (1 candidate + 2 baselines)",
                "coefficient_per_future_scenario": 3_000,
            },
        }
    candidate_rollouts = candidates * scenarios * seed_count
    baseline_rollouts = baselines * scenarios * seed_count
    return {
        "stage_id": stage_id,
        "candidate_count": candidates,
        "retained_candidate_count": retained,
        "scenario_count": scenarios,
        "scenario_selection": (
            "ALL_343_GENERIC_WAVES"
            if scenarios == EXPECTED_GENERIC_WAVES
            else "ROUND_ROBIN_BY_INSTANCE_THEN_SCENARIO_ID_NO_OUTCOME"
        ),
        "master_seed_count": seed_count,
        "seed_list_sha256": _mapping(
            _mapping(seed_contract.get("phases"), "seed phases").get(stage_id),
            stage_id,
        )["seed_list_sha256"],
        "candidate_policy_rollouts": candidate_rollouts,
        "cached_dual_baseline_rollouts": baseline_rollouts,
        "total_policy_rollouts": candidate_rollouts + baseline_rollouts,
        "optional_stopping_allowed": False,
    }


def build_cat_gap_search_blueprint_v1(
    horizon_observation: Mapping[str, Any],
) -> JSONMap:
    negative = validate_horizon_negative_observation_v1(horizon_observation)
    candidates = build_candidate_design_v1()
    approved_policy = _approved_cat_gap_policy_binding_v1()
    seeds = _seed_contract()
    stages = [_stage_row(spec, seeds) for spec in _STAGE_SPECS]
    selection = _stage_row(_SELECTION_SPEC, seeds)
    confirmation = _stage_row(_CONFIRMATION_SPEC, seeds)
    lattice_count = math.prod(len(values) for _, values in PARAMETER_AXES)
    development_rollouts = sum(row["total_policy_rollouts"] for row in stages)
    through_selection = development_rollouts + selection["total_policy_rollouts"]
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": STATUS_BLOCKED,
        "negative_diagnostic_binding": {
            "confirmation_id": negative["confirmation_id"],
            "analysis_contract_sha256": negative["analysis_identity"][
                "analysis_contract_sha256"
            ],
            "compact_analysis_sha256": negative["remote_results"]["compact_sha256"],
            "result": "NO_SELECTION",
            "use": "NEGATIVE_DIAGNOSTIC_ONLY",
            "arm_ranking_used_for_initialization": False,
            "ww_wait_selected": False,
            "same_seed_extension_allowed": False,
        },
        "adapter_admission": {
            "schema": adapter_v1.SCHEMA,
            "required_total_wave_count": EXPECTED_TOTAL_WAVES,
            "required_generic_wave_count": EXPECTED_GENERIC_WAVES,
            "required_pdf_wave_count": EXPECTED_PDF_WAVES,
            "required_generic_instance_count": EXPECTED_GENERIC_INSTANCES,
            "required_pdf_instance_count": EXPECTED_PDF_INSTANCES,
            "required_overlay_manifest_content_sha256": (
                EXPECTED_OVERLAY_MANIFEST_SHA256
            ),
            "required_overlay_manifest_file_sha256": (
                EXPECTED_OVERLAY_MANIFEST_FILE_SHA256
            ),
            "generic_lane": selector_v2.GENERIC_LANE,
            "pdf_lane": selector_v2.PDF_LANE,
            "search_uses_generic_lane_only": True,
            "pdf_lane_used_for_performance_ranking": False,
            "generic_lane_identity_discovery_outcome_free": True,
            "health_branch_selection_policy_receipt": deepcopy(
                adapter_v1.HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1
            ),
            "real_artifact_validated": False,
            "materialized_manifest_content_sha256": None,
        },
        "fixed_player_domain": {
            "talents_string": FIXED_TALENTS,
            "main_hand_item_id": FIXED_MAIN_HAND_ITEM_ID,
            "main_hand_enchant_id": FIXED_MAIN_HAND_ENCHANT_ID,
            "off_hand_empty": True,
            "weapon_mode": FIXED_WEAPON_MODE,
            "loadout_or_talent_search_allowed": False,
            "bonereaver_proc_is_part_of_the_same_fixed_simulator_loadout": True,
        },
        "baseline_contract": {
            "policy_ids": list(BASELINE_POLICY_IDS),
            "source_authorities": {
                CAT_POLICY_ID: {
                    "manifest_id": cat_manifest_v1.MANIFEST_ID,
                    "manifest_sha256": cat_manifest_v1.EXPECTED_MANIFEST_SHA256,
                },
                CONTRA260817_POLICY_ID: {
                    "manifest_id": contra260817_manifest_v1.MANIFEST_ID,
                    "manifest_sha256": (
                        contra260817_manifest_v1.EXPECTED_MANIFEST_SHA256
                    ),
                    "code_manifest_sha256": (
                        contra260817_manifest_v1.CODE_MANIFEST_SHA256
                    ),
                },
            },
            "same_request_and_master_seed_as_candidate": True,
            "baseline_rows_cached_once_per_scenario_seed": True,
            "baseline_removal_after_results_allowed": False,
            "primary_development_metric": (
                "min over baselines of equal-instance-weighted paired mean "
                "candidate-minus-baseline DPS"
            ),
            "strongest_baseline_is_not_predeclared": True,
        },
        "parameter_design": {
            "dimension_count": len(PARAMETER_AXES),
            "admissible_lattice_point_count": lattice_count,
            "predeclared_candidate_count": len(candidates),
            "construction": (
                "two source-neighborhood anchors plus deterministic Halton points; "
                "no Horizon-v2 outcome feedback"
            ),
            "axes": [
                {"name": name, "values": list(values)}
                for name, values in PARAMETER_AXES
            ],
            "candidate_design_sha256": sha256_json(list(candidates)),
            "candidates": list(candidates),
            "legacy_horizon_arm_ids_forbidden": list(EXPECTED_HORIZON_ARMS),
            "death_wish": "FIXED_DISABLED_UNTIL_OFF_GCD_CONTRACT_IS_ADJUDICATED",
            "candidate_capability_source": {
                "manifest_id": cat2_manifest_v1.MANIFEST_ID,
                "manifest_sha256": cat2_manifest_v1.EXPECTED_MANIFEST_SHA256,
                "use": "DEVELOPMENT_EXECUTOR_CAPABILITY_ONLY_NOT_A_BASELINE",
            },
        },
        "candidate_executor_contract": {
            "status": "POLICY_READY_VARIABLE_LANE_WORKER_PENDING",
            "approved_policy": approved_policy,
            "implemented_runtime_axes": [name for name, _ in PARAMETER_AXES],
            "missing_runtime_axes": [],
            "current_worker_module": "o2o_dps.fury_multiseed_hpc_worker_v3",
            "current_worker_blockers": [
                "REQUIRES_EXACT_CAT2NEW_FURY_PARAMETRIC_POLICY_V1_TYPE",
                "REQUIRES_FIXED_FOUR_LANE_REGISTRY",
                "DOES_NOT_ACCEPT_CANDIDATE_ID_TO_13D_PARAMETER_BINDING",
            ],
            "required_factory": (
                "build_cat_gap_policy_v1(candidate_id, exact_13d_parameters)"
            ),
            "required_paired_lanes": [
                CAT_POLICY_ID,
                CONTRA260817_POLICY_ID,
                "candidate_id_from_frozen_design",
            ],
            "each_axis_has_at_least_one_predeclared_reachable_policy_family_witness": True,
            "local_parameter_interaction_inactivity_allowed": True,
            "complete_behavior_duplicate_candidates_allowed": False,
            "silent_axis_drop_allowed": False,
            "policy_ready": True,
            "variable_lane_worker_ready": False,
            "ready": False,
        },
        "seed_contract": seeds,
        "successive_halving": {
            "stages": stages,
            "retention_rule": (
                "rank complete eligible candidates by dual-baseline maximin paired "
                "mean; ties by candidate_id ascending; retain exact declared count"
            ),
            "incomplete_or_noneligible_stage_result": "STAGE_FAILED_NO_RETENTION",
            "right_censored_active_hardcast_at_exact_horizon_allowed": True,
            "other_pending_or_omitted_action_allowed": False,
            "optional_stopping_allowed": False,
        },
        "selection_validation": {
            **selection,
            "contrast_family_size": 4,
            "multiplicity": "HOLM_FAMILYWISE_ALPHA_0.05",
            "pass_rule": (
                "both baseline paired means positive and both Holm-adjusted tests reject"
            ),
            "no_passing_candidate_result": "NO_SELECTION",
            "old50_result_is_scientific_confirmation": False,
        },
        "future_confirmation_reservation": {
            **confirmation,
            "corpus": "FUTURE_NEW_INSTANCE_DISJOINT_NOT_OLD50",
            "candidate_must_be_frozen_before_first_instance_ingest": True,
            "can_inform_policy_changes": False,
            "run_now": False,
        },
        "cost": {
            "successive_halving_policy_rollouts": development_rollouts,
            "selection_validation_policy_rollouts": selection[
                "total_policy_rollouts"
            ],
            "total_through_old50_selection_validation": through_selection,
            "future_confirmation_policy_rollout_formula": "3000 * future_new_scenario_count",
            "baseline_cache_counted_once_per_stage_scenario_seed": True,
        },
        "scenario_sampling_contract": {
            "unit": "exact generic instance x encounter x wave",
            "stage_counts": [42, 112, 343, 343],
            "round_robin_key": "instance_id ascending then scenario_id ascending",
            "outcome_fields_used_for_sampling": False,
            "guild_player_component_split_required_for_later_heldout_work": True,
            "materialized_manifest_content_sha256": None,
            "scenario_id_binding_status": "PENDING_STRICT_470_WAVE_ADAPTER_VALIDATION",
        },
        "execution_gate": {
            "ready_for_heavy_execution": False,
            "required_before_staging": [
                "REAL_470_WAVE_ADAPTER_ARTIFACT_VALIDATES",
                "ALL_343_GENERIC_SCENARIOS_VALIDATE_AND_SHARE_FIXED_PLAYER_DOMAIN",
                "CAT_GAP_13D_POLICY_EXECUTOR_AND_WORKER_CONFIG_CONTRACT_PASS",
                "ONE_PROCESS_LOCAL_SMOKE_PASS",
            ],
            "local_process_limit": 1,
            "heavy_compute_nodes": [f"node{index:03d}" for index in range(1, 7)],
        },
        "scientific_boundary": deepcopy(SCIENTIFIC_BOUNDARY),
    }
    return _seal(core)


def _player_from_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
    raid = _mapping(request.get("raid"), "scenario request.raid")
    parties = _array(raid.get("parties"), "scenario request.raid.parties")
    if not parties:
        raise FuryCatGapSearchPlanV1Error("fixed-player request has no party")
    player_rows = [
        _array(_mapping(party, "party").get("players"), "party.players")
        for party in parties
    ]
    if (
        len(player_rows[0]) != 1
        or not isinstance(player_rows[0][0], Mapping)
        or any(rows for rows in player_rows[1:])
    ):
        raise FuryCatGapSearchPlanV1Error(
            "fixed-player search requires the only player at party 0/player 0"
        )
    return player_rows[0][0]


def _fixed_player_identity(request: Mapping[str, Any]) -> JSONMap:
    player = _player_from_request(request)
    equipment = _mapping(player.get("equipment"), "player.equipment")
    items = _array(equipment.get("items"), "player.equipment.items")
    if len(items) <= 15:
        raise FuryCatGapSearchPlanV1Error("player equipment lacks weapon slots")
    main = _mapping(items[14], "main-hand item")
    off = _mapping(items[15], "off-hand item")
    if (
        player.get("talentsString") != FIXED_TALENTS
        or main.get("id") != FIXED_MAIN_HAND_ITEM_ID
        or main.get("enchant") != FIXED_MAIN_HAND_ENCHANT_ID
        or off.get("id") not in (None, 0)
    ):
        raise FuryCatGapSearchPlanV1Error(
            "adapter scenario differs from the frozen live two-hand player domain"
        )
    identity = deepcopy(dict(player))
    return {"semantic": identity, "semantic_sha256": sha256_json(identity)}


def _materialization_parameters_v1(
    *,
    target_level: int,
    initial_base_armor: int | float,
    health_branch_selection_policy: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None,
) -> JSONMap:
    if isinstance(target_level, bool) or not isinstance(target_level, int) or target_level <= 0:
        raise FuryCatGapSearchPlanV1Error("target_level must be a positive integer")
    if (
        isinstance(initial_base_armor, bool)
        or not isinstance(initial_base_armor, (int, float))
        or not math.isfinite(float(initial_base_armor))
        or float(initial_base_armor) <= 0
    ):
        raise FuryCatGapSearchPlanV1Error("initial_base_armor must be positive")
    if health_branch_selection_policy != adapter_v1.HEALTH_BRANCH_FALLBACK_POLICY_V1:
        raise FuryCatGapSearchPlanV1Error(
            "health fallback policy differs from the versioned adapter authority"
        )
    if not isinstance(attackability_branch_id, str) or not attackability_branch_id:
        raise FuryCatGapSearchPlanV1Error("attackability_branch_id must be nonempty")
    if not isinstance(target_classification, str) or not target_classification:
        raise FuryCatGapSearchPlanV1Error("target_classification must be nonempty")
    armor = deepcopy(dict(armor_magnitude_by_debuff or {}))
    if any(
        not isinstance(name, str)
        or not name
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for name, value in armor.items()
    ):
        raise FuryCatGapSearchPlanV1Error(
            "armor_magnitude_by_debuff must contain finite numeric choices"
        )
    if armor != FIXED_ARMOR_MAGNITUDE_BY_DEBUFF:
        raise FuryCatGapSearchPlanV1Error(
            "materialization must freeze expose_armor at 1700"
        )
    return {
        "target_level": target_level,
        "initial_base_armor": float(initial_base_armor),
        "health_branch_selection_policy": health_branch_selection_policy,
        "attackability_branch_id": attackability_branch_id,
        "target_classification": target_classification,
        "armor_magnitude_by_debuff": armor,
        "loadout_contract": "CALLER_PINNED_STATIC_REQUEST_AND_EQUIPPED_NAMES",
    }


def _validate_environment_binding_v1(value: Mapping[str, Any]) -> JSONMap:
    raw = deepcopy(dict(_mapping(value, "pre-materialization environment binding")))
    address = _mapping(raw.get("content_address"), "environment content address")
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if set(core) != {
        "schema",
        "status",
        "source_inputs",
        "canonical_base_request",
        "complete_player_semantic_identity",
        "materialization_parameters",
        "health_fallback_contract",
        "adapter_contract",
        "strict_manifest_expected_source_bindings",
    } or (
        core.get("schema") != ENVIRONMENT_BINDING_SCHEMA
        or core.get("status") != ENVIRONMENT_BINDING_STATUS
        or address
        != {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        }
    ):
        raise FuryCatGapSearchPlanV1Error(
            "pre-materialization environment binding schema or address differs"
        )
    sources = _mapping(core.get("source_inputs"), "environment source inputs")
    if set(sources) != {
        "overlay_manifest",
        "capsule_bundle",
        "base_request",
        "equipped_names_receipt",
    }:
        raise FuryCatGapSearchPlanV1Error("environment source input set differs")
    overlay = _mapping(sources.get("overlay_manifest"), "overlay identity")
    capsule = _mapping(sources.get("capsule_bundle"), "capsule identity")
    request_identity = _mapping(sources.get("base_request"), "request identity")
    equipped = _mapping(
        sources.get("equipped_names_receipt"), "equipped receipt identity"
    )
    request = _mapping(core.get("canonical_base_request"), "canonical base request")
    player = _fixed_player_identity(request)
    player_binding = _mapping(
        core.get("complete_player_semantic_identity"), "complete player identity"
    )
    parameters = _mapping(
        core.get("materialization_parameters"), "materialization parameters"
    )
    expected_parameters = _materialization_parameters_v1(
        target_level=parameters.get("target_level"),
        initial_base_armor=parameters.get("initial_base_armor"),
        health_branch_selection_policy=parameters.get(
            "health_branch_selection_policy"
        ),
        attackability_branch_id=parameters.get("attackability_branch_id"),
        target_classification=parameters.get("target_classification"),
        armor_magnitude_by_debuff=_mapping(
            parameters.get("armor_magnitude_by_debuff"), "armor choices"
        ),
    )
    expected_sources = {
        "overlay_manifest_content_sha256": EXPECTED_OVERLAY_MANIFEST_SHA256,
        "capsule_bundle_content_sha256": capsule.get("bundle_content_sha256"),
        "base_request_sha256": sha256_json(request),
        "equipped_names_receipt_sha256": equipped.get(
            "canonical_document_sha256"
        ),
    }
    health_contract = {
        "policy_receipt": deepcopy(
            adapter_v1.HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1
        ),
        "formal_selection_counts": deepcopy(
            adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
        ),
        "per_target_available_branch_provenance": {
            "artifact_field": (
                "hypothesis_selection.health_by_target[]."
                "available_branch_provenance[]"
            ),
            "authority": (
                "validate_materialized_exact_fury_dynamic_v3_corpus_v1"
            ),
            "validation": "EXACT_470_ROW_DETERMINISTIC_SOURCE_RECOMPILE",
            "target_count": sum(
                adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1.values()
            ),
            "schema_duplicated_in_planner": False,
        },
    }
    adapter_contract = {
        "record_schema": adapter_v1.SCHEMA,
        "revision": adapter_v1.REVISION,
        "materialized_manifest_schema": adapter_v1.MATERIALIZED_MANIFEST_SCHEMA,
        "materialized_manifest_status": adapter_v1.MATERIALIZED_MANIFEST_STATUS,
    }
    if (
        overlay
        != {
            "content_sha256": EXPECTED_OVERLAY_MANIFEST_SHA256,
            "file_sha256": EXPECTED_OVERLAY_MANIFEST_FILE_SHA256,
        }
        or set(capsule)
        != {
            "bundle_content_sha256",
            "canonical_document_sha256",
            "file_sha256",
        }
        or set(request_identity) != {"canonical_document_sha256", "file_sha256"}
        or set(equipped) != {"canonical_document_sha256", "file_sha256"}
        or any(
            not _is_sha256(value)
            for identity in (capsule, request_identity, equipped)
            for value in identity.values()
        )
        or request_identity.get("canonical_document_sha256") != sha256_json(request)
        or player_binding
        != {
            "locator": {"party_index": 0, "player_index": 0},
            "semantic": player["semantic"],
            "semantic_sha256": player["semantic_sha256"],
        }
        or dict(parameters) != expected_parameters
        or core.get("health_fallback_contract") != health_contract
        or core.get("adapter_contract") != adapter_contract
        or core.get("strict_manifest_expected_source_bindings") != expected_sources
    ):
        raise FuryCatGapSearchPlanV1Error(
            "pre-materialization environment binding differs from its sources"
        )
    return raw


def _apply_environment_binding_v1(
    blueprint: Mapping[str, Any], binding: Mapping[str, Any]
) -> JSONMap:
    core = deepcopy(dict(blueprint))
    core.pop("plan_sha256", None)
    core["status"] = STATUS_ENVIRONMENT_BOUND
    core["pre_materialization_environment_binding"] = deepcopy(dict(binding))
    return _seal(core)


def bind_prematerialization_environment_v1(
    blueprint: Mapping[str, Any],
    *,
    overlay_manifest_path: str | Path,
    capsule_path: str | Path,
    base_request_path: str | Path,
    equipped_names_receipt_path: str | Path,
    target_level: int,
    initial_base_armor: int | float,
    health_branch_selection_policy: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None = None,
) -> JSONMap:
    plan = validate_cat_gap_search_plan_v1(blueprint)
    if plan.get("status") != STATUS_BLOCKED:
        raise FuryCatGapSearchPlanV1Error(
            "environment binding requires the canonical blocked blueprint"
        )
    overlay_path = Path(overlay_manifest_path).expanduser().resolve()
    overlay_file_sha256 = _sha256_file_bytes(overlay_path)
    if overlay_file_sha256 != EXPECTED_OVERLAY_MANIFEST_FILE_SHA256:
        raise FuryCatGapSearchPlanV1Error("formal overlay manifest file SHA-256 differs")
    try:
        corpus = adapter_v1.load_formal_exact_fury_overlay_corpus_v1(overlay_path)
    except adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError as error:
        raise FuryCatGapSearchPlanV1Error(
            f"formal overlay validation failed: {error}"
        ) from error
    if (
        corpus.manifest["content_address"]["sha256"]
        != EXPECTED_OVERLAY_MANIFEST_SHA256
    ):
        raise FuryCatGapSearchPlanV1Error("formal overlay content SHA-256 differs")
    capsule, capsule_file_sha256 = _read_json_source_path(
        capsule_path, "old-50 capsule bundle"
    )
    request, request_file_sha256 = _read_json_source_path(
        base_request_path, "base simulator request"
    )
    equipped, equipped_file_sha256 = _read_json_source_path(
        equipped_names_receipt_path, "equipped names receipt"
    )
    try:
        capsule_index = adapter_v1.build_validated_old50_capsule_index_v1(capsule)
    except adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError as error:
        raise FuryCatGapSearchPlanV1Error(
            f"old-50 capsule validation failed: {error}"
        ) from error
    player = _fixed_player_identity(request)
    parameters = _materialization_parameters_v1(
        target_level=target_level,
        initial_base_armor=initial_base_armor,
        health_branch_selection_policy=health_branch_selection_policy,
        attackability_branch_id=attackability_branch_id,
        target_classification=target_classification,
        armor_magnitude_by_debuff=armor_magnitude_by_debuff,
    )
    source_bindings = {
        "overlay_manifest_content_sha256": EXPECTED_OVERLAY_MANIFEST_SHA256,
        "capsule_bundle_content_sha256": capsule_index.bundle_content_sha256,
        "base_request_sha256": sha256_json(request),
        "equipped_names_receipt_sha256": sha256_json(equipped),
    }
    binding = _content_address(
        {
            "schema": ENVIRONMENT_BINDING_SCHEMA,
            "status": ENVIRONMENT_BINDING_STATUS,
            "source_inputs": {
                "overlay_manifest": {
                    "content_sha256": EXPECTED_OVERLAY_MANIFEST_SHA256,
                    "file_sha256": overlay_file_sha256,
                },
                "capsule_bundle": {
                    "bundle_content_sha256": capsule_index.bundle_content_sha256,
                    "canonical_document_sha256": sha256_json(capsule),
                    "file_sha256": capsule_file_sha256,
                },
                "base_request": {
                    "canonical_document_sha256": sha256_json(request),
                    "file_sha256": request_file_sha256,
                },
                "equipped_names_receipt": {
                    "canonical_document_sha256": sha256_json(equipped),
                    "file_sha256": equipped_file_sha256,
                },
            },
            "canonical_base_request": deepcopy(request),
            "complete_player_semantic_identity": {
                "locator": {"party_index": 0, "player_index": 0},
                "semantic": player["semantic"],
                "semantic_sha256": player["semantic_sha256"],
            },
            "materialization_parameters": parameters,
            "health_fallback_contract": {
                "policy_receipt": deepcopy(
                    adapter_v1.HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1
                ),
                "formal_selection_counts": deepcopy(
                    adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
                ),
                "per_target_available_branch_provenance": {
                    "artifact_field": (
                        "hypothesis_selection.health_by_target[]."
                        "available_branch_provenance[]"
                    ),
                    "authority": (
                        "validate_materialized_exact_fury_dynamic_v3_corpus_v1"
                    ),
                    "validation": "EXACT_470_ROW_DETERMINISTIC_SOURCE_RECOMPILE",
                    "target_count": sum(
                        adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1.values()
                    ),
                    "schema_duplicated_in_planner": False,
                },
            },
            "adapter_contract": {
                "record_schema": adapter_v1.SCHEMA,
                "revision": adapter_v1.REVISION,
                "materialized_manifest_schema": (
                    adapter_v1.MATERIALIZED_MANIFEST_SCHEMA
                ),
                "materialized_manifest_status": (
                    adapter_v1.MATERIALIZED_MANIFEST_STATUS
                ),
            },
            "strict_manifest_expected_source_bindings": source_bindings,
        }
    )
    validated_binding = _validate_environment_binding_v1(binding)
    return _apply_environment_binding_v1(plan, validated_binding)


def _apply_adapter_admission_v1(
    environment_plan: Mapping[str, Any], admission_receipt: Mapping[str, Any]
) -> JSONMap:
    core = deepcopy(dict(environment_plan))
    core.pop("plan_sha256", None)
    core["status"] = STATUS_ADAPTER_READY
    core["adapter_admission"].update(deepcopy(dict(admission_receipt)))
    manifest_sha256 = admission_receipt["materialized_manifest_content_sha256"]
    core["scenario_sampling_contract"]["materialized_manifest_content_sha256"] = (
        manifest_sha256
    )
    core["scenario_sampling_contract"]["scenario_id_binding_status"] = (
        "PENDING_STRICT_POLICY_RUNNER_PREPARATION_FROM_VALIDATED_PARTITIONS"
    )
    core["execution_gate"]["required_before_staging"] = [
        "STRICT_POLICY_RUNNER_BINDS_ALL_343_GENERIC_SCENARIOS_FROM_VALIDATED_PARTITIONS",
        "CAT_GAP_13D_POLICY_EXECUTOR_AND_WORKER_CONFIG_CONTRACT_PASS",
        "ONE_PROCESS_LOCAL_SMOKE_PASS",
    ]
    return _seal(core)


def admit_real_adapter_artifacts_v1(
    environment_plan: Mapping[str, Any],
    *,
    materialized_manifest_path: str | Path,
    overlay_manifest_path: str | Path,
    capsule_path: str | Path,
    base_request_path: str | Path,
    equipped_names_receipt_path: str | Path,
) -> JSONMap:
    """Replay and admit a materialization that exactly matches its prebinding."""

    plan = validate_cat_gap_search_plan_v1(environment_plan)
    if plan.get("status") != STATUS_ENVIRONMENT_BOUND:
        raise FuryCatGapSearchPlanV1Error(
            "adapter admission requires a pre-materialization environment binding"
        )
    binding = _validate_environment_binding_v1(
        _mapping(
            plan.get("pre_materialization_environment_binding"),
            "pre-materialization environment binding",
        )
    )
    bound_sources = _mapping(binding["source_inputs"], "bound source inputs")
    overlay_path = Path(overlay_manifest_path).expanduser().resolve()
    overlay_file_sha256 = _sha256_file_bytes(overlay_path)
    capsule, capsule_file_sha256 = _read_json_source_path(
        capsule_path, "old-50 capsule bundle"
    )
    request, request_file_sha256 = _read_json_source_path(
        base_request_path, "base simulator request"
    )
    equipped, equipped_file_sha256 = _read_json_source_path(
        equipped_names_receipt_path, "equipped names receipt"
    )
    try:
        capsule_index = adapter_v1.build_validated_old50_capsule_index_v1(capsule)
    except adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError as error:
        raise FuryCatGapSearchPlanV1Error(
            f"old-50 capsule validation failed: {error}"
        ) from error
    actual_source_inputs = {
        "overlay_manifest": {
            "content_sha256": EXPECTED_OVERLAY_MANIFEST_SHA256,
            "file_sha256": overlay_file_sha256,
        },
        "capsule_bundle": {
            "bundle_content_sha256": capsule_index.bundle_content_sha256,
            "canonical_document_sha256": sha256_json(capsule),
            "file_sha256": capsule_file_sha256,
        },
        "base_request": {
            "canonical_document_sha256": sha256_json(request),
            "file_sha256": request_file_sha256,
        },
        "equipped_names_receipt": {
            "canonical_document_sha256": sha256_json(equipped),
            "file_sha256": equipped_file_sha256,
        },
    }
    if (
        actual_source_inputs != bound_sources
        or request != binding["canonical_base_request"]
        or _fixed_player_identity(request)["semantic_sha256"]
        != binding["complete_player_semantic_identity"]["semantic_sha256"]
    ):
        raise FuryCatGapSearchPlanV1Error(
            "materialization source files differ from the pre-bound environment"
        )
    try:
        manifest = adapter_v1.validate_materialized_exact_fury_dynamic_v3_corpus_v1(
            manifest_path=materialized_manifest_path,
            overlay_manifest_path=overlay_path,
            capsule_bundle=capsule,
            base_request=request,
            equipped_names_receipt=equipped,
        )
    except adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError as error:
        raise FuryCatGapSearchPlanV1Error(
            f"strict materialized adapter validation failed: {error}"
        ) from error
    sources = _mapping(manifest.get("source_bindings"), "manifest source bindings")
    parameters = _mapping(
        manifest.get("materialization_parameters"), "manifest materialization parameters"
    )
    summary = _mapping(manifest.get("summary"), "materialized summary")
    instances = [
        _mapping(value, "materialized instance")
        for value in _array(manifest.get("instances"), "materialized instances")
    ]
    lane_instance_counts = {
        lane: sum(entry.get("selection_lane") == lane for entry in instances)
        for lane in selector_v2.SELECTION_LANES
    }
    if (
        manifest.get("schema") != adapter_v1.MATERIALIZED_MANIFEST_SCHEMA
        or manifest.get("revision") != adapter_v1.REVISION
        or manifest.get("status") != adapter_v1.MATERIALIZED_MANIFEST_STATUS
        or sources != binding["strict_manifest_expected_source_bindings"]
        or parameters != binding["materialization_parameters"]
        or summary.get("instance_count")
        != EXPECTED_GENERIC_INSTANCES + EXPECTED_PDF_INSTANCES
        or summary.get("compiled_wave_count") != EXPECTED_TOTAL_WAVES
        or summary.get("generic_outcome_free_wave_count") != EXPECTED_GENERIC_WAVES
        or summary.get("pdf_outcome_conditioned_wave_count") != EXPECTED_PDF_WAVES
        or summary.get("health_branch_selection_counts")
        != adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
        or lane_instance_counts.get(selector_v2.GENERIC_LANE)
        != EXPECTED_GENERIC_INSTANCES
        or lane_instance_counts.get(selector_v2.PDF_LANE) != EXPECTED_PDF_INSTANCES
    ):
        raise FuryCatGapSearchPlanV1Error(
            "strict materialized adapter differs from the pre-bound contract"
        )
    manifest_sha256 = _mapping(
        manifest.get("content_address"), "manifest content address"
    ).get("sha256")
    if not _is_sha256(manifest_sha256):
        raise FuryCatGapSearchPlanV1Error(
            "strict materialized adapter manifest SHA-256 is missing"
        )
    receipt = {
        "real_artifact_validated": True,
        "validation_method": "STRICT_FULL_SOURCE_REPLAY_VIA_MATERIALIZED_MANIFEST",
        "validated_total_wave_count": summary["compiled_wave_count"],
        "validated_generic_wave_count": summary["generic_outcome_free_wave_count"],
        "validated_pdf_wave_count": summary["pdf_outcome_conditioned_wave_count"],
        "validated_generic_instance_count": lane_instance_counts[
            selector_v2.GENERIC_LANE
        ],
        "validated_pdf_instance_count": lane_instance_counts[selector_v2.PDF_LANE],
        "fixed_player_semantic_sha256": binding[
            "complete_player_semantic_identity"
        ]["semantic_sha256"],
        "materialized_manifest_content_sha256": manifest_sha256,
        "validated_source_bindings": deepcopy(dict(sources)),
        "overlay_manifest_file_sha256": overlay_file_sha256,
        "materialization_parameters_sha256": sha256_json(parameters),
        "health_branch_selection_counts": deepcopy(
            summary["health_branch_selection_counts"]
        ),
        "pre_materialization_environment_binding_sha256": binding[
            "content_address"
        ]["sha256"],
    }
    return _apply_adapter_admission_v1(plan, receipt)


def _validate_adapter_admission_receipt_v1(
    value: Mapping[str, Any], environment_binding: Mapping[str, Any]
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "strict adapter admission receipt")))
    binding = _validate_environment_binding_v1(environment_binding)
    if (
        set(receipt) != ADAPTER_ADMISSION_RECEIPT_FIELDS
        or receipt.get("real_artifact_validated") is not True
        or receipt.get("validation_method")
        != "STRICT_FULL_SOURCE_REPLAY_VIA_MATERIALIZED_MANIFEST"
        or receipt.get("validated_total_wave_count") != EXPECTED_TOTAL_WAVES
        or receipt.get("validated_generic_wave_count") != EXPECTED_GENERIC_WAVES
        or receipt.get("validated_pdf_wave_count") != EXPECTED_PDF_WAVES
        or receipt.get("validated_generic_instance_count")
        != EXPECTED_GENERIC_INSTANCES
        or receipt.get("validated_pdf_instance_count") != EXPECTED_PDF_INSTANCES
        or receipt.get("fixed_player_semantic_sha256")
        != binding["complete_player_semantic_identity"]["semantic_sha256"]
        or not _is_sha256(receipt.get("materialized_manifest_content_sha256"))
        or receipt.get("validated_source_bindings")
        != binding["strict_manifest_expected_source_bindings"]
        or receipt.get("overlay_manifest_file_sha256")
        != binding["source_inputs"]["overlay_manifest"]["file_sha256"]
        or receipt.get("materialization_parameters_sha256")
        != sha256_json(binding["materialization_parameters"])
        or receipt.get("health_branch_selection_counts")
        != adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
        or receipt.get("pre_materialization_environment_binding_sha256")
        != binding["content_address"]["sha256"]
    ):
        raise FuryCatGapSearchPlanV1Error(
            "strict adapter admission receipt differs from its environment binding"
        )
    return receipt


def _execution_surface_source_binding_v1() -> JSONMap:
    from . import fury_cat_gap_hpc_plan_v1 as plan_v1
    from . import fury_cat_gap_hpc_reducer_v1 as reducer_v1
    from . import fury_cat_gap_hpc_worker_v1 as worker_v1
    from . import hpc_dynamic_environment_v5 as environment_v5
    from .fury_execution_source_identity_v2 import (
        build_fury_execution_source_identity_v2,
    )

    module_specs = (
        (
            "o2o_dps.fury_cat_gap_hpc_plan_v1",
            plan_v1,
            [plan_v1.EXECUTION_PLAN_SCHEMA_V1, plan_v1.DISPATCH_SCHEMA_V1],
        ),
        (
            "o2o_dps.fury_cat_gap_hpc_worker_v1",
            worker_v1,
            [
                worker_v1.ROLLOUT_ROW_SCHEMA_V1,
                worker_v1.SHARD_PARTIAL_SCHEMA_V1,
                worker_v1.SHARD_RECEIPT_SCHEMA_V1,
            ],
        ),
        (
            "o2o_dps.fury_cat_gap_hpc_reducer_v1",
            reducer_v1,
            [reducer_v1.REDUCTION_SCHEMA_V1, reducer_v1.LOCAL_SMOKE_EVIDENCE_SCHEMA_V1],
        ),
    )
    modules = []
    for module_name, module, schemas in module_specs:
        source = Path(module.__file__).resolve()
        try:
            relative = source.relative_to(PROJECT_ROOT).as_posix()
        except ValueError as error:
            raise FuryCatGapSearchPlanV1Error(
                f"execution module is outside the project root: {module_name}"
            ) from error
        modules.append(
            {
                "module": module_name,
                "source_path": relative,
                "source_sha256": _sha256_file_bytes(source),
                "schemas": list(schemas),
            }
        )
    test_path = PROJECT_ROOT / "tests/test_fury_cat_gap_hpc_v1.py"
    python_closure = build_fury_execution_source_identity_v2(
        project_root=PROJECT_ROOT,
        required_relative_paths=tuple(row["source_path"] for row in modules),
    )
    build_script = PROJECT_ROOT / "scripts/build_simulator_linux.ps1"
    dynamic_contract = PROJECT_ROOT / "configs/hpc/dynamic_v5.example.json"
    environment_source = Path(environment_v5.__file__).resolve()
    closure = {
        "schema": "fury_cat_gap_variable_lane_source_closure/v1",
        "modules": modules,
        "python_dependency_closure": python_closure,
        "go_bridge_build_contract": {
            "build_script_path": "scripts/build_simulator_linux.ps1",
            "build_script_sha256": _sha256_file_bytes(build_script),
            "build_parameters": (
                "GOOS=linux;GOARCH=amd64;GOAMD64=v1;CGO_ENABLED=0;"
                "GOFLAGS=empty;buildvcs=false;trimpath=true;tags=with_db;"
                "buildid=empty"
            ),
            "dynamic_environment_module_path": (
                environment_source.relative_to(PROJECT_ROOT).as_posix()
            ),
            "dynamic_environment_module_sha256": _sha256_file_bytes(
                environment_source
            ),
            "dynamic_environment_contract_path": (
                "configs/hpc/dynamic_v5.example.json"
            ),
            "dynamic_environment_contract_sha256": _sha256_file_bytes(
                dynamic_contract
            ),
            "linux_bridge_filename": environment_v5.EXPECTED_BRIDGE_FILENAME,
            "linux_bridge_sha256": environment_v5.EXPECTED_BRIDGE_SHA256,
        },
        "test": {
            "source_path": "tests/test_fury_cat_gap_hpc_v1.py",
            "source_sha256": _sha256_file_bytes(test_path),
        },
    }
    return {**closure, "source_closure_sha256": sha256_json(closure)}


def _validate_smoke_evidence_v1(value: Mapping[str, Any]) -> JSONMap:
    from .fury_cat_gap_hpc_plan_v1 import (
        LOCAL_SMOKE_MASTER_SEED_V1,
        LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V1,
    )
    from .fury_cat_gap_hpc_reducer_v1 import LOCAL_SMOKE_EVIDENCE_SCHEMA_V1

    evidence = deepcopy(dict(_mapping(value, "real one-process smoke evidence")))
    core = deepcopy(evidence)
    address = _mapping(core.pop("content_address", None), "smoke evidence address")
    expected_fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "reduction_sha256", "shard_receipt_sha256", "raw_logical_sha256",
        "raw_compressed_sha256", "partial_sha256", "bridge_sha256",
        "master_seed", "simulator_seed", "simulator_seed_namespace",
        "candidate_id", "policy_ids", "validated_full_rollout_artifact_count",
        "task_count", "bridge_process_count", "gomaxprocs",
        "heavy_execution_started", "retention_allowed", "simulator_only",
        "deployment_allowed",
    }
    sha_fields = {
        "execution_plan_sha256", "dispatch_plan_sha256", "reduction_sha256",
        "shard_receipt_sha256", "raw_logical_sha256", "raw_compressed_sha256",
        "partial_sha256", "bridge_sha256",
    }
    candidate_id = evidence.get("candidate_id")
    canonical_first = build_candidate_design_v1()[0]["candidate_id"]
    if (
        set(core) != expected_fields
        or evidence.get("schema") != LOCAL_SMOKE_EVIDENCE_SCHEMA_V1
        or evidence.get("status") != "PASS_REAL_BRIDGE_ONE_PROCESS_THREE_LANE_SMOKE"
        or address.get("sha256") != sha256_json(core)
        or any(not _is_sha256(evidence.get(field)) for field in sha_fields)
        or candidate_id != canonical_first
        or evidence.get("policy_ids")
        != [CAT_POLICY_ID, CONTRA260817_POLICY_ID, canonical_first]
        or evidence.get("master_seed") != LOCAL_SMOKE_MASTER_SEED_V1
        or not isinstance(evidence.get("simulator_seed"), int)
        or isinstance(evidence.get("simulator_seed"), bool)
        or evidence.get("simulator_seed") < 0
        or evidence.get("simulator_seed_namespace")
        != LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V1
        or evidence.get("validated_full_rollout_artifact_count") != 3
        or evidence.get("task_count") != 3
        or evidence.get("bridge_process_count") != 1
        or evidence.get("gomaxprocs") != 1
        or evidence.get("heavy_execution_started") is not False
        or evidence.get("retention_allowed") is not False
        or evidence.get("simulator_only") is not True
        or evidence.get("deployment_allowed") is not False
    ):
        raise FuryCatGapSearchPlanV1Error(
            "real one-process smoke evidence differs from the execution contract"
        )
    return evidence


def _validate_execution_surface_admission_v1(
    value: Mapping[str, Any], *, adapter_ready_plan_sha256: str
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "execution surface admission")))
    expected_fields = {
        "schema", "status", "input_adapter_ready_plan_sha256",
        "execution_surface_source_binding", "smoke_evidence",
        "simulator_only", "development_only", "scientific_result_available",
        "deployment_allowed", "content_address",
    }
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "executor admission address")
    source_binding = _mapping(
        receipt.get("execution_surface_source_binding"), "execution source binding"
    )
    smoke = _validate_smoke_evidence_v1(
        _mapping(receipt.get("smoke_evidence"), "smoke evidence")
    )
    if (
        set(receipt) != expected_fields
        or receipt.get("schema") != EXECUTION_SURFACE_ADMISSION_SCHEMA
        or receipt.get("status")
        != "PASS_EXACT_SOURCE_TEST_AND_REAL_ONE_PROCESS_SMOKE"
        or receipt.get("input_adapter_ready_plan_sha256")
        != adapter_ready_plan_sha256
        or dict(source_binding) != _execution_surface_source_binding_v1()
        or address.get("sha256") != sha256_json(core)
        or smoke != receipt.get("smoke_evidence")
        or receipt.get("simulator_only") is not True
        or receipt.get("development_only") is not True
        or receipt.get("scientific_result_available") is not False
        or receipt.get("deployment_allowed") is not False
    ):
        raise FuryCatGapSearchPlanV1Error(
            "execution surface admission differs from current source/test/runtime closure"
        )
    return receipt


def _apply_execution_surface_admission_v1(
    adapter_plan: Mapping[str, Any], admission_receipt: Mapping[str, Any]
) -> JSONMap:
    core = deepcopy(dict(adapter_plan))
    core.pop("plan_sha256", None)
    core["status"] = STATUS_MECHANICS_READY
    executor = core["candidate_executor_contract"]
    executor.update(
        {
            "status": "POLICY_AND_VARIABLE_LANE_MECHANICS_READY_HEAVY_PENDING",
            "current_worker_module": "o2o_dps.fury_cat_gap_hpc_worker_v1",
            "current_worker_blockers": [],
            "execution_surface_admission": deepcopy(dict(admission_receipt)),
            "variable_lane_worker_ready": True,
            "ready": False,
        }
    )
    core["execution_gate"].update(
        {
            "ready_for_heavy_execution": False,
            "required_before_staging": [
                "STRICT_343_GENERIC_TEMPLATE_PROOF",
                "SIX_NODE_V11_ACTIVATION_RECEIPT",
                "CAT2_RUNTIME_SNAPSHOT_AND_REMOTE_ENVIRONMENT_CAPSULE",
            ],
            "execution_surface_admission_sha256": admission_receipt[
                "content_address"
            ]["sha256"],
        }
    )
    return _seal(core)


def _apply_heavy_preparation_v1(
    mechanics_plan: Mapping[str, Any], preparation_receipt: Mapping[str, Any]
) -> JSONMap:
    """Close formal/runtime inputs while leaving the scientific stage blocked."""

    source_closure_sha256 = _mapping(
        _mapping(
            _mapping(
                _mapping(
                    mechanics_plan.get("candidate_executor_contract"),
                    "candidate executor contract",
                ).get("execution_surface_admission"),
                "execution surface admission",
            ).get("execution_surface_source_binding"),
            "execution source binding",
        ).get("python_dependency_closure"),
        "Python dependency closure",
    ).get("canonical_bundle")
    source_closure_sha256 = _mapping(
        source_closure_sha256, "Python dependency closure address"
    ).get("sha256")
    if preparation_receipt.get("execution_source_closure_sha256") != source_closure_sha256:
        raise FuryCatGapSearchPlanV1Error(
            "heavy preparation execution source differs from the mechanics-ready plan"
        )

    core = deepcopy(dict(mechanics_plan))
    core.pop("plan_sha256", None)
    core["status"] = STATUS_HEAVY_PREPARED
    core["candidate_executor_contract"].update(
        {
            "status": "FORMAL_RUNTIME_PREPARED_CAPACITY_PILOT_PENDING",
            "heavy_preparation_receipt": deepcopy(dict(preparation_receipt)),
            "ready": False,
        }
    )
    core["execution_gate"].update(
        {
            "ready_for_capacity_pilot": True,
            "ready_for_heavy_execution": False,
            "required_before_staging": [
                "CAPACITY_PILOT_960_OF_960_PASS",
                "PARALLEL_EFFICIENCY_AT_LEAST_0_70",
                "EXTRAPOLATED_PEAK_RSS_AT_MOST_0_80_INITIAL_MEMAVAILABLE",
            ],
            "heavy_preparation_receipt_sha256": preparation_receipt[
                "content_address"
            ]["sha256"],
        }
    )
    return _seal(core)


def _apply_capacity_pilot_admission_v1(
    heavy_prepared_plan: Mapping[str, Any],
    *,
    pilot_plan_sha256: str,
    pilot_admission: Mapping[str, Any],
) -> JSONMap:
    """Open SH1 staging only after the no-retention capacity gate passes."""

    core = deepcopy(dict(heavy_prepared_plan))
    core.pop("plan_sha256", None)
    core["status"] = STATUS_HEAVY_READY
    core["candidate_executor_contract"].update(
        {
            "status": "FORMAL_RUNTIME_AND_CAPACITY_ADMITTED_SEARCH_NOT_STARTED",
            "capacity_pilot_plan_sha256": pilot_plan_sha256,
            "capacity_pilot_admission": deepcopy(dict(pilot_admission)),
            "ready": True,
        }
    )
    core["execution_gate"].update(
        {
            "ready_for_capacity_pilot": False,
            "ready_for_heavy_execution": True,
            "ready_for_scientific_claim": False,
            "required_before_staging": [],
            "capacity_pilot_plan_sha256": pilot_plan_sha256,
            "capacity_pilot_admission_sha256": pilot_admission[
                "content_address"
            ]["sha256"],
        }
    )
    return _seal(core)


def admit_variable_lane_execution_surface_v1(
    adapter_plan: Mapping[str, Any],
    *,
    smoke_execution_plan: Mapping[str, Any],
    smoke_dispatch_plan: Mapping[str, Any],
    smoke_output_directory: str | Path,
) -> JSONMap:
    """Promote only after exact sources and a real three-lane bridge smoke pass."""

    base = validate_cat_gap_search_plan_v1(adapter_plan)
    if base.get("status") != STATUS_ADAPTER_READY:
        raise FuryCatGapSearchPlanV1Error(
            "execution surface admission requires the strict adapter-ready plan"
        )
    from .fury_cat_gap_hpc_plan_v1 import (
        validate_dispatch_plan_v1,
        validate_execution_plan_v1,
    )
    from .fury_cat_gap_hpc_reducer_v1 import validate_local_smoke_evidence_v1

    smoke = validate_execution_plan_v1(smoke_execution_plan)
    if (
        smoke.get("search_plan") != base
        or smoke.get("candidate_ids")
        != [build_candidate_design_v1()[0]["candidate_id"]]
    ):
        raise FuryCatGapSearchPlanV1Error(
            "smoke does not bind the adapter-ready plan and canonical first candidate"
        )
    dispatch = validate_dispatch_plan_v1(smoke_dispatch_plan, smoke)
    evidence = validate_local_smoke_evidence_v1(
        smoke,
        dispatch,
        output_directory=smoke_output_directory,
    )
    source_binding = _execution_surface_source_binding_v1()
    receipt_core = {
        "schema": EXECUTION_SURFACE_ADMISSION_SCHEMA,
        "status": "PASS_EXACT_SOURCE_TEST_AND_REAL_ONE_PROCESS_SMOKE",
        "input_adapter_ready_plan_sha256": base["plan_sha256"],
        "execution_surface_source_binding": source_binding,
        "smoke_evidence": evidence,
        "simulator_only": True,
        "development_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    receipt = {
        **receipt_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(receipt_core),
        },
    }
    checked_receipt = _validate_execution_surface_admission_v1(
        receipt, adapter_ready_plan_sha256=base["plan_sha256"]
    )
    return _apply_execution_surface_admission_v1(base, checked_receipt)


def admit_heavy_preparation_v1(
    mechanics_ready_plan: Mapping[str, Any],
    *,
    adapter_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
    materialized_manifest_path: str | Path,
    activation_receipt: Mapping[str, Any],
    runtime_closure: Mapping[str, Any],
    windows_build_receipt: Mapping[str, Any],
) -> JSONMap:
    """Admit formal inputs for the capacity pilot, never a search stage."""

    base = validate_cat_gap_search_plan_v1(mechanics_ready_plan)
    if base.get("status") != STATUS_MECHANICS_READY:
        raise FuryCatGapSearchPlanV1Error(
            "heavy preparation requires the mechanics-ready plan"
        )
    from .fury_cat_gap_formal_preparation_v1 import (
        build_heavy_preparation_receipt_v1,
        validate_heavy_preparation_receipt_v1,
    )

    try:
        receipt = build_heavy_preparation_receipt_v1(
            base,
            adapter_ready_plan=adapter_ready_plan,
            generic_template=generic_template,
            materialized_manifest_path=materialized_manifest_path,
            activation_receipt=activation_receipt,
            runtime_closure=runtime_closure,
            windows_build_receipt=windows_build_receipt,
        )
        checked = validate_heavy_preparation_receipt_v1(
            receipt,
            mechanics_ready_plan_sha256=base["plan_sha256"],
        )
    except Exception as error:
        if isinstance(error, FuryCatGapSearchPlanV1Error):
            raise
        raise FuryCatGapSearchPlanV1Error(
            f"formal heavy preparation admission failed: {error}"
        ) from error
    return _apply_heavy_preparation_v1(base, checked)


def admit_capacity_pilot_v1(
    heavy_prepared_plan: Mapping[str, Any],
    *,
    mechanics_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
    pilot_plan: Mapping[str, Any],
    pilot_output_directory: str | Path,
) -> JSONMap:
    """Promote to heavy-ready only from all 960 no-retention pilot receipts."""

    prepared = validate_cat_gap_search_plan_v1(heavy_prepared_plan)
    if prepared.get("status") != STATUS_HEAVY_PREPARED:
        raise FuryCatGapSearchPlanV1Error(
            "capacity admission requires the heavy-prepared pre-pilot plan"
        )
    mechanics = validate_cat_gap_search_plan_v1(mechanics_ready_plan)
    if mechanics.get("status") != STATUS_MECHANICS_READY:
        raise FuryCatGapSearchPlanV1Error(
            "capacity admission requires the exact mechanics-ready ancestor"
        )
    heavy_receipt = _mapping(
        _mapping(
            prepared.get("candidate_executor_contract"), "candidate executor contract"
        ).get("heavy_preparation_receipt"),
        "heavy preparation receipt",
    )
    from .fury_cat_gap_formal_preparation_v1 import (
        build_capacity_pilot_admission_v1,
        validate_capacity_pilot_admission_v1,
        validate_capacity_pilot_plan_v1,
        validate_heavy_preparation_receipt_v1,
    )

    try:
        checked_heavy = validate_heavy_preparation_receipt_v1(
            heavy_receipt,
            mechanics_ready_plan_sha256=mechanics["plan_sha256"],
        )
        checked_plan = validate_capacity_pilot_plan_v1(
            pilot_plan,
            checked_heavy,
            mechanics_ready_plan=mechanics,
            generic_template=generic_template,
        )
        admission = build_capacity_pilot_admission_v1(
            checked_plan,
            output_directory=pilot_output_directory,
        )
        checked_admission = validate_capacity_pilot_admission_v1(
            admission,
            pilot_plan_sha256=checked_plan["content_address"]["sha256"],
            heavy_preparation_receipt_sha256=checked_heavy["content_address"][
                "sha256"
            ],
        )
    except Exception as error:
        if isinstance(error, FuryCatGapSearchPlanV1Error):
            raise
        raise FuryCatGapSearchPlanV1Error(
            f"capacity pilot admission failed: {error}"
        ) from error
    return _apply_capacity_pilot_admission_v1(
        prepared,
        pilot_plan_sha256=checked_plan["content_address"]["sha256"],
        pilot_admission=checked_admission,
    )


def _canonical_blueprint_v1() -> JSONMap:
    return build_cat_gap_search_blueprint_v1(
        _canonical_horizon_negative_observation_v1()
    )


def validate_cat_gap_search_plan_v1(value: Mapping[str, Any]) -> JSONMap:
    """Reconstruct the applicable canonical plan and require exact equality."""

    document = deepcopy(dict(_mapping(value, "Cat-gap search plan")))
    raw = deepcopy(document)
    digest = raw.pop("plan_sha256", None)
    if digest != sha256_json(raw):
        raise FuryCatGapSearchPlanV1Error("search plan content address differs")
    status = raw.get("status")
    canonical = _canonical_blueprint_v1()
    if status == STATUS_BLOCKED:
        expected = deepcopy(canonical)
    elif status in {
        STATUS_ENVIRONMENT_BOUND,
        STATUS_ADAPTER_READY,
        STATUS_MECHANICS_READY,
        STATUS_HEAVY_PREPARED,
        STATUS_HEAVY_READY,
    }:
        binding = _validate_environment_binding_v1(
            _mapping(
                raw.get("pre_materialization_environment_binding"),
                "pre-materialization environment binding",
            )
        )
        expected = _apply_environment_binding_v1(canonical, binding)
        if status in {
            STATUS_ADAPTER_READY,
            STATUS_MECHANICS_READY,
            STATUS_HEAVY_PREPARED,
            STATUS_HEAVY_READY,
        }:
            observed_admission = _mapping(
                raw.get("adapter_admission"), "adapter admission"
            )
            if not ADAPTER_ADMISSION_RECEIPT_FIELDS.issubset(observed_admission):
                raise FuryCatGapSearchPlanV1Error(
                    "strict adapter admission receipt is incomplete"
                )
            receipt = _validate_adapter_admission_receipt_v1(
                {
                    field: observed_admission[field]
                    for field in ADAPTER_ADMISSION_RECEIPT_FIELDS
                },
                binding,
            )
            expected = _apply_adapter_admission_v1(expected, receipt)
        if status in {
            STATUS_MECHANICS_READY,
            STATUS_HEAVY_PREPARED,
            STATUS_HEAVY_READY,
        }:
            executor = _mapping(
                raw.get("candidate_executor_contract"), "candidate executor contract"
            )
            execution_receipt = _validate_execution_surface_admission_v1(
                _mapping(
                    executor.get("execution_surface_admission"),
                    "execution surface admission",
                ),
                adapter_ready_plan_sha256=expected["plan_sha256"],
            )
            expected = _apply_execution_surface_admission_v1(
                expected, execution_receipt
            )
        if status in {STATUS_HEAVY_PREPARED, STATUS_HEAVY_READY}:
            from .fury_cat_gap_formal_preparation_v1 import (
                validate_heavy_preparation_receipt_v1,
            )

            executor = _mapping(
                raw.get("candidate_executor_contract"), "candidate executor contract"
            )
            try:
                preparation = validate_heavy_preparation_receipt_v1(
                    _mapping(
                        executor.get("heavy_preparation_receipt"),
                        "heavy preparation receipt",
                    ),
                    mechanics_ready_plan_sha256=expected["plan_sha256"],
                )
            except Exception as error:
                raise FuryCatGapSearchPlanV1Error(
                    f"heavy preparation receipt validation failed: {error}"
                ) from error
            expected = _apply_heavy_preparation_v1(expected, preparation)
            if status == STATUS_HEAVY_READY:
                from .fury_cat_gap_formal_preparation_v1 import (
                    validate_capacity_pilot_admission_v1,
                )

                pilot_plan_sha256 = executor.get("capacity_pilot_plan_sha256")
                try:
                    pilot_admission = validate_capacity_pilot_admission_v1(
                        _mapping(
                            executor.get("capacity_pilot_admission"),
                            "capacity pilot admission",
                        ),
                        pilot_plan_sha256=pilot_plan_sha256,
                        heavy_preparation_receipt_sha256=preparation[
                            "content_address"
                        ]["sha256"],
                    )
                except Exception as error:
                    raise FuryCatGapSearchPlanV1Error(
                        f"capacity pilot admission validation failed: {error}"
                    ) from error
                expected = _apply_capacity_pilot_admission_v1(
                    expected,
                    pilot_plan_sha256=pilot_plan_sha256,
                    pilot_admission=pilot_admission,
                )
    else:
        raise FuryCatGapSearchPlanV1Error("search plan status differs")
    expected_core = deepcopy(expected)
    expected_core.pop("plan_sha256", None)
    if raw != expected_core:
        raise FuryCatGapSearchPlanV1Error(
            "search plan differs from its reconstructed canonical contract"
        )
    return document


def _read_json(path: Path) -> JSONMap:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapSearchPlanV1Error(f"could not read {path}: {error}") from error
    return dict(_mapping(value, str(path)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--horizon-observation", type=Path, default=DEFAULT_HORIZON_OBSERVATION
    )
    parser.add_argument("--materialized-manifest", type=Path)
    parser.add_argument("--overlay-manifest", type=Path)
    parser.add_argument("--capsule", type=Path)
    parser.add_argument("--base-request", type=Path)
    parser.add_argument("--equipped-names", type=Path)
    parser.add_argument("--target-level", type=int)
    parser.add_argument("--initial-base-armor", type=float)
    parser.add_argument("--health-branch-selection-policy")
    parser.add_argument("--attackability-branch-id")
    parser.add_argument("--target-classification")
    args = parser.parse_args(argv)
    try:
        plan = build_cat_gap_search_blueprint_v1(
            _read_json(args.horizon_observation)
        )
        environment_values = (
            args.overlay_manifest,
            args.capsule,
            args.base_request,
            args.equipped_names,
            args.target_level,
            args.initial_base_armor,
            args.health_branch_selection_policy,
            args.attackability_branch_id,
            args.target_classification,
        )
        if any(value is not None for value in environment_values):
            if not all(value is not None for value in environment_values):
                raise FuryCatGapSearchPlanV1Error(
                    "environment binding requires all source paths and materialization "
                    "parameters together"
                )
            plan = bind_prematerialization_environment_v1(
                plan,
                overlay_manifest_path=args.overlay_manifest,
                capsule_path=args.capsule,
                base_request_path=args.base_request,
                equipped_names_receipt_path=args.equipped_names,
                target_level=args.target_level,
                initial_base_armor=args.initial_base_armor,
                health_branch_selection_policy=args.health_branch_selection_policy,
                attackability_branch_id=args.attackability_branch_id,
                target_classification=args.target_classification,
                armor_magnitude_by_debuff=FIXED_ARMOR_MAGNITUDE_BY_DEBUFF,
            )
        if args.materialized_manifest is not None:
            if not all(value is not None for value in environment_values):
                raise FuryCatGapSearchPlanV1Error(
                    "adapter admission requires the complete pre-bound environment"
                )
            plan = admit_real_adapter_artifacts_v1(
                plan,
                materialized_manifest_path=args.materialized_manifest,
                overlay_manifest_path=args.overlay_manifest,
                capsule_path=args.capsule,
                base_request_path=args.base_request,
                equipped_names_receipt_path=args.equipped_names,
            )
    except FuryCatGapSearchPlanV1Error as error:
        print(f"BLOCKED: {error}", flush=True)
        return 2
    print(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BASELINE_POLICY_IDS",
    "EXPECTED_GENERIC_WAVES",
    "EXPECTED_HORIZON_ARMS",
    "EXPECTED_OVERLAY_MANIFEST_SHA256",
    "EXPECTED_PDF_WAVES",
    "EXPECTED_TOTAL_WAVES",
    "FIXED_MAIN_HAND_ENCHANT_ID",
    "FIXED_MAIN_HAND_ITEM_ID",
    "FIXED_TALENTS",
    "FuryCatGapSearchPlanV1Error",
    "PARAMETER_AXES",
    "REVISION",
    "SCHEMA",
    "SCIENTIFIC_BOUNDARY",
    "SEED_NAMESPACE",
    "STATUS_ADAPTER_READY",
    "STATUS_BLOCKED",
    "STATUS_ENVIRONMENT_BOUND",
    "STATUS_HEAVY_PREPARED",
    "STATUS_HEAVY_READY",
    "STATUS_MECHANICS_READY",
    "admit_capacity_pilot_v1",
    "admit_variable_lane_execution_surface_v1",
    "admit_heavy_preparation_v1",
    "admit_real_adapter_artifacts_v1",
    "bind_prematerialization_environment_v1",
    "build_candidate_design_v1",
    "build_cat_gap_search_blueprint_v1",
    "validate_cat_gap_search_plan_v1",
    "validate_horizon_negative_observation_v1",
)
