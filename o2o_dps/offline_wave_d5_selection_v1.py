"""All-seed D5 candidate selection and frozen held-out adjudication.

D5 keeps one executable program per candidate across every paired seed.  The
selection cohort first gates candidates against the frozen D4 retention on
clear probability and residual required-target health, then maximizes focal
effective damage.  Clear and fixed-horizon outcomes are both retained; an
invalid/missing row invalidates the panel instead of being deleted or imputed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import math
import statistics
from typing import Any, Mapping, Sequence

from .offline_wave_d3_action_attribution_v1 import (
    CONTROLLER_WAIT,
    EXPLICIT_ACTION,
    EXPLICIT_WAIT,
    OTHER,
    SCHEMA as ATTRIBUTION_SCHEMA,
    TAIL_FILL,
)
from .offline_wave_d2_panel_v1 import CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D
from .offline_wave_d3_frozen_heldout_v1 import PI_STAR
from .offline_wave_d4_all_seed_endpoint_v1 import (
    OfflineWaveD4AllSeedEndpointV1Error,
    _validated_row as _validated_d4_row,
)
from .offline_wave_d5_candidate_panel_v1 import (
    CANDIDATE_SCHEMA,
    RETENTION,
    SEARCHED,
    TAIL_ONLY,
)
from .offline_wave_searched_program_v1 import (
    SearchedWaveProgramV1,
    SearchedWaveProgramV1Error,
    SearchedWaveStepKindV1,
    searched_wave_behavior_key_v1,
    searched_wave_program_from_dict_v1,
)


JSONMap = dict[str, Any]

SCHEMA = "offline_wave_d5_selection/v1"
CONTRACT_SCHEMA = f"{SCHEMA}/contract"
ROW_SCHEMA = f"{SCHEMA}/row"
SELECTION_SCHEMA = f"{SCHEMA}/selection"
HELDOUT_SCHEMA = f"{SCHEMA}/heldout"
CONFIRMATION_ROW_SCHEMA = f"{SCHEMA}/confirmation_row"
CONFIRMATION_SCHEMA = f"{SCHEMA}/confirmation"
SELECTION = "selection"
HELDOUT = "heldout"
COHORTS = (SELECTION, HELDOUT)

CANDIDATE_ROLES = (RETENTION, TAIL_ONLY, SEARCHED)

D4_RETENTION = "D4_RETENTION"
D5_TAIL_ONLY = "D5_TAIL_ONLY"
CONFIRMATION_CONTROLLER_IDS = (
    PI_STAR,
    D4_RETENTION,
    D5_TAIL_ONLY,
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    PI_D,
)
_SEARCHED_CONFIRMATION_CONTROLLER_IDS = (PI_STAR, D4_RETENTION, D5_TAIL_ONLY)
CONFIRMATION_PAIR_COUNT = 48

_ATTRIBUTION_SOURCES = (
    EXPLICIT_ACTION,
    TAIL_FILL,
    EXPLICIT_WAIT,
    CONTROLLER_WAIT,
    OTHER,
)
_TERMINAL_STEP_OUTCOMES = (
    "ACCEPTED",
    "MISSED",
    "SKIPPED",
    "SELECTED_BUT_UNACCEPTED",
    "NEVER_SELECTED",
)
_FORBIDDEN_PROGRAM_KEYS = frozenset({"program_by_seed", "seed_action_sequence"})


class OfflineWaveD5SelectionV1Error(ValueError):
    """The D5 selection or held-out contract is not auditable."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWaveD5SelectionV1Error(f"{label} must be nonempty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveD5SelectionV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise OfflineWaveD5SelectionV1Error(f"{label} must be positive")
    return result


def _number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise OfflineWaveD5SelectionV1Error(f"{label} must be a finite number")
    return float(value)


def _seed_pair(value: object, label: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if set(value) != {"simulator_seed", "teammate_seed"}:
            raise OfflineWaveD5SelectionV1Error(
                f"{label} must contain exactly the paired seed fields"
            )
        simulator = value["simulator_seed"]
        teammate = value["teammate_seed"]
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 2
    ):
        simulator, teammate = value
    else:
        raise OfflineWaveD5SelectionV1Error(f"{label} must be a paired seed")
    return (
        _nonnegative_int(simulator, f"{label}.simulator_seed"),
        _nonnegative_int(teammate, f"{label}.teammate_seed"),
    )


def _seed_pairs(value: object, label: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise OfflineWaveD5SelectionV1Error(f"{label} must be a sequence")
    pairs = tuple(
        _seed_pair(row, f"{label}[{index}]") for index, row in enumerate(value)
    )
    if not pairs or len(pairs) != len(set(pairs)):
        raise OfflineWaveD5SelectionV1Error(
            f"{label} must be nonempty and contain unique pairs"
        )
    simulator = [row[0] for row in pairs]
    teammate = [row[1] for row in pairs]
    if len(simulator) != len(set(simulator)) or len(teammate) != len(set(teammate)):
        raise OfflineWaveD5SelectionV1Error(
            f"{label} must not repeat either seed component"
        )
    return pairs


def _pairs_to_rows(pairs: Sequence[tuple[int, int]]) -> list[JSONMap]:
    return [
        {"simulator_seed": simulator, "teammate_seed": teammate}
        for simulator, teammate in pairs
    ]


def _require_component_disjoint(
    left: Sequence[tuple[int, int]],
    right: Sequence[tuple[int, int]],
    label: str,
) -> None:
    if set(left) & set(right):
        raise OfflineWaveD5SelectionV1Error(f"{label} repeats paired seeds")
    if {row[0] for row in left} & {row[0] for row in right}:
        raise OfflineWaveD5SelectionV1Error(f"{label} repeats simulator seeds")
    if {row[1] for row in left} & {row[1] for row in right}:
        raise OfflineWaveD5SelectionV1Error(f"{label} repeats teammate seeds")


def _forbidden_paths(value: object, prefix: str = "candidate") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if key in _FORBIDDEN_PROGRAM_KEYS:
                found.append(path)
            found.extend(_forbidden_paths(child, path))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{prefix}[{index}]") )
    return found


def validate_d5_candidate_manifests_v1(
    candidates: Sequence[Mapping[str, Any]],
    *,
    retention_candidate_id: str,
    tail_only_candidate_id: str,
) -> list[JSONMap]:
    """Validate one seed-invariant executable program per D5 candidate."""

    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        raise OfflineWaveD5SelectionV1Error("candidates must be a sequence")
    retention_candidate_id = _text(
        retention_candidate_id, "retention_candidate_id"
    )
    tail_only_candidate_id = _text(tail_only_candidate_id, "tail_only_candidate_id")
    if retention_candidate_id == tail_only_candidate_id:
        raise OfflineWaveD5SelectionV1Error(
            "retention and tail-only candidates must differ"
        )

    copied: list[JSONMap] = []
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping) or raw.get("schema") != CANDIDATE_SCHEMA:
            raise OfflineWaveD5SelectionV1Error(
                f"candidate[{index}] has the wrong schema"
            )
        row = deepcopy(dict(raw))
        candidate_id = _text(row.get("candidate_id"), f"candidate[{index}].candidate_id")
        behavior_key = _text(row.get("behavior_key"), f"candidate[{index}].behavior_key")
        role = row.get("role")
        if role not in CANDIDATE_ROLES or row.get("proposal_arm") != role:
            raise OfflineWaveD5SelectionV1Error(
                f"candidate {candidate_id!r} has an invalid role/proposal_arm"
            )
        forbidden = _forbidden_paths(row)
        if forbidden:
            raise OfflineWaveD5SelectionV1Error(
                "seed-specific candidate programs are forbidden: "
                + ", ".join(forbidden)
            )
        try:
            program = searched_wave_program_from_dict_v1(row.get("program"))
        except (SearchedWaveProgramV1Error, TypeError, ValueError) as error:
            raise OfflineWaveD5SelectionV1Error(
                f"candidate {candidate_id!r} has an invalid searched program"
            ) from error
        if searched_wave_behavior_key_v1(program) != behavior_key:
            raise OfflineWaveD5SelectionV1Error(
                f"candidate {candidate_id!r} behavior_key differs from its program"
            )
        copied.append(row)

    ids = [row["candidate_id"] for row in copied]
    if len(ids) != len(set(ids)):
        raise OfflineWaveD5SelectionV1Error("candidate_id values must be unique")
    by_id = {row["candidate_id"]: row for row in copied}
    if retention_candidate_id not in by_id or tail_only_candidate_id not in by_id:
        raise OfflineWaveD5SelectionV1Error(
            "candidate panel lacks retention or tail-only ablation"
        )
    if by_id[retention_candidate_id]["role"] != RETENTION:
        raise OfflineWaveD5SelectionV1Error(
            "retention_candidate_id does not name the retention role"
        )
    if by_id[tail_only_candidate_id]["role"] != TAIL_ONLY:
        raise OfflineWaveD5SelectionV1Error(
            "tail_only_candidate_id does not name the tail-only role"
        )
    if sum(row["role"] == RETENTION for row in copied) != 1:
        raise OfflineWaveD5SelectionV1Error("exactly one retention candidate is required")
    if sum(row["role"] == TAIL_ONLY for row in copied) != 1:
        raise OfflineWaveD5SelectionV1Error("exactly one tail-only ablation is required")
    if not any(row["role"] == SEARCHED for row in copied):
        raise OfflineWaveD5SelectionV1Error("at least one searched candidate is required")
    return copied


def build_d5_selection_contract_v1(
    *,
    campaign_id: str,
    candidates: Sequence[Mapping[str, Any]],
    retention_candidate_id: str,
    tail_only_candidate_id: str,
    selection_seed_pairs: Sequence[object],
    heldout_seed_pairs: Sequence[object],
    d4_confirmation_seed_pairs: Sequence[object],
    horizon_ms: int,
) -> JSONMap:
    """Freeze D5 programs and seed cohorts before any selection replay."""

    manifests = validate_d5_candidate_manifests_v1(
        candidates,
        retention_candidate_id=retention_candidate_id,
        tail_only_candidate_id=tail_only_candidate_id,
    )
    selection_pairs = _seed_pairs(selection_seed_pairs, "selection_seed_pairs")
    heldout_pairs = _seed_pairs(heldout_seed_pairs, "heldout_seed_pairs")
    d4_pairs = _seed_pairs(d4_confirmation_seed_pairs, "d4_confirmation_seed_pairs")
    _require_component_disjoint(
        selection_pairs, heldout_pairs, "selection and heldout cohorts"
    )
    _require_component_disjoint(
        selection_pairs, d4_pairs, "selection and D4 confirmation cohorts"
    )
    _require_component_disjoint(
        heldout_pairs, d4_pairs, "heldout and D4 confirmation cohorts"
    )
    return {
        "schema": CONTRACT_SCHEMA,
        "campaign_id": _text(campaign_id, "campaign_id"),
        "candidate_manifests": manifests,
        "retention_candidate_id": _text(
            retention_candidate_id, "retention_candidate_id"
        ),
        "tail_only_candidate_id": _text(
            tail_only_candidate_id, "tail_only_candidate_id"
        ),
        "paired_seed_cohorts": {
            SELECTION: _pairs_to_rows(selection_pairs),
            HELDOUT: _pairs_to_rows(heldout_pairs),
        },
        "excluded_d4_confirmation_seed_pairs": _pairs_to_rows(d4_pairs),
        "fixed_horizon_ms": _positive_int(horizon_ms, "horizon_ms"),
        "selection_rule": {
            "feasibility_gate_1": "COMPLETION_RATE_DELTA_VS_RETENTION_GTE_ZERO",
            "feasibility_gate_2": (
                "MEAN_RESIDUAL_REQUIRED_HEALTH_FRACTION_DELTA_VS_RETENTION_LTE_ZERO"
            ),
            "objective_after_feasibility": (
                "MEAN_FOCAL_EFFECTIVE_DAMAGE_BY_HORIZON_OR_CLEAR_DESC"
            ),
            "tie_break": "BEHAVIOR_KEY_ASC_THEN_CANDIDATE_ID_ASC",
        },
        "one_fixed_program_per_candidate_across_seeds": True,
        "tail_only_ablation_required": True,
        "exact_paired_seed_coverage_required": True,
        "complete_case_deletion": False,
        "failed_or_incomplete_imputed_as_zero": False,
        "heldout_can_reselect": False,
    }


def _program_by_candidate(manifests: Sequence[JSONMap]) -> dict[str, SearchedWaveProgramV1]:
    return {
        row["candidate_id"]: searched_wave_program_from_dict_v1(row["program"])
        for row in manifests
    }


def _id_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise OfflineWaveD5SelectionV1Error(f"{label} must be a list")
    result = [_text(row, f"{label}[{index}]") for index, row in enumerate(value)]
    if len(result) != len(set(result)):
        raise OfflineWaveD5SelectionV1Error(f"{label} repeats a step")
    return result


def _validated_attribution(
    value: Mapping[str, Any], *, program: SearchedWaveProgramV1
) -> JSONMap:
    if not isinstance(value, Mapping) or value.get("schema") != ATTRIBUTION_SCHEMA:
        raise OfflineWaveD5SelectionV1Error("row has invalid action attribution")
    row = deepcopy(dict(value))
    action_counts = row.get("accepted_action_count_by_source")
    wait_counts = row.get("accepted_wait_count_by_source")
    if not isinstance(action_counts, Mapping) or set(action_counts) != set(
        _ATTRIBUTION_SOURCES
    ):
        raise OfflineWaveD5SelectionV1Error(
            "attribution action sources are incomplete"
        )
    if not isinstance(wait_counts, Mapping) or set(wait_counts) != set(
        _ATTRIBUTION_SOURCES
    ):
        raise OfflineWaveD5SelectionV1Error(
            "attribution wait sources are incomplete"
        )
    action_counts = {
        source: _nonnegative_int(action_counts[source], f"action count {source}")
        for source in _ATTRIBUTION_SOURCES
    }
    wait_counts = {
        source: _nonnegative_int(wait_counts[source], f"wait count {source}")
        for source in _ATTRIBUTION_SOURCES
    }
    if _nonnegative_int(row.get("accepted_action_count"), "accepted_action_count") != sum(
        action_counts.values()
    ):
        raise OfflineWaveD5SelectionV1Error("accepted action counts do not sum")
    if _nonnegative_int(row.get("accepted_wait_count"), "accepted_wait_count") != sum(
        wait_counts.values()
    ):
        raise OfflineWaveD5SelectionV1Error("accepted wait counts do not sum")

    step_kind = {step.step_id: step.kind for step in program.steps}
    action_steps = {
        step_id for step_id, kind in step_kind.items() if kind is SearchedWaveStepKindV1.ACTION
    }
    wait_steps = set(step_kind) - action_steps
    if _nonnegative_int(
        row.get("explicit_action_step_count"), "explicit_action_step_count"
    ) != len(action_steps):
        raise OfflineWaveD5SelectionV1Error(
            "attribution explicit action count differs from fixed program"
        )
    if _nonnegative_int(
        row.get("explicit_wait_step_count"), "explicit_wait_step_count"
    ) != len(wait_steps):
        raise OfflineWaveD5SelectionV1Error(
            "attribution explicit wait count differs from fixed program"
        )
    accepted_action = _id_list(
        row.get("accepted_explicit_action_step_ids"),
        "accepted_explicit_action_step_ids",
    )
    accepted_wait = _id_list(
        row.get("accepted_explicit_wait_step_ids"),
        "accepted_explicit_wait_step_ids",
    )
    categories = {
        "ACCEPTED": set(accepted_action) | set(accepted_wait),
        "MISSED": set(_id_list(row.get("missed_explicit_step_ids"), "missed_explicit_step_ids")),
        "SKIPPED": set(_id_list(row.get("skipped_explicit_step_ids"), "skipped_explicit_step_ids")),
        "SELECTED_BUT_UNACCEPTED": set(
            _id_list(
                row.get("selected_but_unaccepted_explicit_step_ids"),
                "selected_but_unaccepted_explicit_step_ids",
            )
        ),
        "NEVER_SELECTED": set(
            _id_list(
                row.get("never_selected_explicit_step_ids"),
                "never_selected_explicit_step_ids",
            )
        ),
    }
    if not set(accepted_action) <= action_steps or not set(accepted_wait) <= wait_steps:
        raise OfflineWaveD5SelectionV1Error(
            "accepted explicit steps disagree with fixed program kinds"
        )
    seen: set[str] = set()
    for outcome in _TERMINAL_STEP_OUTCOMES:
        overlap = seen & categories[outcome]
        if overlap:
            raise OfflineWaveD5SelectionV1Error(
                f"explicit steps have multiple terminal outcomes: {sorted(overlap)}"
            )
        seen |= categories[outcome]
    if seen != set(step_kind):
        raise OfflineWaveD5SelectionV1Error(
            "attribution does not cover every fixed-program step exactly once"
        )
    if _nonnegative_int(
        row.get("accepted_explicit_action_step_count"),
        "accepted_explicit_action_step_count",
    ) != len(accepted_action):
        raise OfflineWaveD5SelectionV1Error(
            "accepted explicit action step count differs from IDs"
        )
    if _nonnegative_int(
        row.get("accepted_explicit_wait_step_count"),
        "accepted_explicit_wait_step_count",
    ) != len(accepted_wait):
        raise OfflineWaveD5SelectionV1Error(
            "accepted explicit wait step count differs from IDs"
        )
    if action_counts[EXPLICIT_ACTION] != len(accepted_action):
        raise OfflineWaveD5SelectionV1Error(
            "explicit accepted actions differ from accepted explicit steps"
        )
    if wait_counts[EXPLICIT_WAIT] != len(accepted_wait):
        raise OfflineWaveD5SelectionV1Error(
            "explicit accepted waits differ from accepted explicit wait steps"
        )
    return row


def build_d5_selection_row_v1(
    *,
    cohort: str,
    endpoint_row: Mapping[str, Any],
    action_attribution: Mapping[str, Any],
) -> JSONMap:
    """Attach D3 action attribution to one deeply validated D4 endpoint row."""

    if cohort not in COHORTS:
        raise OfflineWaveD5SelectionV1Error(f"unsupported cohort {cohort!r}")
    if not isinstance(endpoint_row, Mapping):
        raise OfflineWaveD5SelectionV1Error("endpoint_row must be a mapping")
    terminal = endpoint_row.get("terminal_endpoint")
    if not isinstance(terminal, Mapping):
        raise OfflineWaveD5SelectionV1Error("endpoint_row lacks terminal evidence")
    horizon_ms = _positive_int(terminal.get("fixed_horizon_ms"), "fixed_horizon_ms")
    try:
        validated = _validated_d4_row(endpoint_row, horizon_ms=horizon_ms)
    except (OfflineWaveD4AllSeedEndpointV1Error, TypeError, ValueError) as error:
        raise OfflineWaveD5SelectionV1Error("endpoint_row is not valid D4 evidence") from error
    if validated["controller_id"] != PI_STAR:
        raise OfflineWaveD5SelectionV1Error(
            "D5 candidate rows require the searched PI_STAR controller"
        )
    if validated["all_seed_endpoint_eligible"] is not True:
        raise OfflineWaveD5SelectionV1Error(
            "D5 candidate rows require technically valid clear or horizon evidence"
        )
    return {
        "schema": ROW_SCHEMA,
        "cohort": cohort,
        "endpoint_row": validated,
        "action_attribution": deepcopy(dict(action_attribution)),
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }


def build_d5_confirmation_endpoint_row_v1(
    *,
    controller_id: str,
    endpoint_row: Mapping[str, Any],
) -> JSONMap:
    """Label one raw D4 endpoint row for the seven-lane D5 confirmation."""

    if controller_id not in CONFIRMATION_CONTROLLER_IDS:
        raise OfflineWaveD5SelectionV1Error(
            f"unsupported confirmation controller_id {controller_id!r}"
        )
    if not isinstance(endpoint_row, Mapping):
        raise OfflineWaveD5SelectionV1Error("endpoint_row must be a mapping")
    terminal = endpoint_row.get("terminal_endpoint")
    if not isinstance(terminal, Mapping):
        raise OfflineWaveD5SelectionV1Error("endpoint_row lacks terminal evidence")
    horizon_ms = _positive_int(terminal.get("fixed_horizon_ms"), "fixed_horizon_ms")
    try:
        validated = _validated_d4_row(endpoint_row, horizon_ms=horizon_ms)
    except (OfflineWaveD4AllSeedEndpointV1Error, TypeError, ValueError) as error:
        raise OfflineWaveD5SelectionV1Error("endpoint_row is not valid D4 evidence") from error
    expected_inner_controller = (
        PI_STAR
        if controller_id in _SEARCHED_CONFIRMATION_CONTROLLER_IDS
        else controller_id
    )
    if validated["controller_id"] != expected_inner_controller:
        raise OfflineWaveD5SelectionV1Error(
            "confirmation controller differs from its raw D4 endpoint controller"
        )
    if validated["all_seed_endpoint_eligible"] is not True:
        raise OfflineWaveD5SelectionV1Error(
            "confirmation requires technically valid clear or horizon evidence"
        )
    return {
        "schema": CONFIRMATION_ROW_SCHEMA,
        "controller_id": controller_id,
        "endpoint_row": validated,
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }


def _contract_parts(
    contract: Mapping[str, Any],
) -> tuple[str, list[JSONMap], dict[str, tuple[tuple[int, int], ...]], int, str, str]:
    if not isinstance(contract, Mapping) or contract.get("schema") != CONTRACT_SCHEMA:
        raise OfflineWaveD5SelectionV1Error("invalid D5 selection contract")
    retention = _text(contract.get("retention_candidate_id"), "retention_candidate_id")
    tail_only = _text(contract.get("tail_only_candidate_id"), "tail_only_candidate_id")
    manifests = validate_d5_candidate_manifests_v1(
        contract.get("candidate_manifests"),
        retention_candidate_id=retention,
        tail_only_candidate_id=tail_only,
    )
    raw_cohorts = contract.get("paired_seed_cohorts")
    if not isinstance(raw_cohorts, Mapping) or set(raw_cohorts) != set(COHORTS):
        raise OfflineWaveD5SelectionV1Error("contract has invalid paired seed cohorts")
    cohorts = {
        cohort: _seed_pairs(raw_cohorts[cohort], f"contract.{cohort}")
        for cohort in COHORTS
    }
    d4_pairs = _seed_pairs(
        contract.get("excluded_d4_confirmation_seed_pairs"),
        "contract.excluded_d4_confirmation_seed_pairs",
    )
    _require_component_disjoint(
        cohorts[SELECTION], cohorts[HELDOUT], "selection and heldout cohorts"
    )
    _require_component_disjoint(
        cohorts[SELECTION], d4_pairs, "selection and D4 confirmation cohorts"
    )
    _require_component_disjoint(
        cohorts[HELDOUT], d4_pairs, "heldout and D4 confirmation cohorts"
    )
    expected_flags = {
        "one_fixed_program_per_candidate_across_seeds": True,
        "tail_only_ablation_required": True,
        "exact_paired_seed_coverage_required": True,
        "complete_case_deletion": False,
        "failed_or_incomplete_imputed_as_zero": False,
        "heldout_can_reselect": False,
    }
    for field, expected in expected_flags.items():
        if contract.get(field) is not expected:
            raise OfflineWaveD5SelectionV1Error(f"contract has invalid {field}")
    expected_rule = {
        "feasibility_gate_1": "COMPLETION_RATE_DELTA_VS_RETENTION_GTE_ZERO",
        "feasibility_gate_2": (
            "MEAN_RESIDUAL_REQUIRED_HEALTH_FRACTION_DELTA_VS_RETENTION_LTE_ZERO"
        ),
        "objective_after_feasibility": (
            "MEAN_FOCAL_EFFECTIVE_DAMAGE_BY_HORIZON_OR_CLEAR_DESC"
        ),
        "tie_break": "BEHAVIOR_KEY_ASC_THEN_CANDIDATE_ID_ASC",
    }
    if contract.get("selection_rule") != expected_rule:
        raise OfflineWaveD5SelectionV1Error("contract selection rule drifted")
    return (
        _text(contract.get("campaign_id"), "campaign_id"),
        manifests,
        cohorts,
        _positive_int(contract.get("fixed_horizon_ms"), "fixed_horizon_ms"),
        retention,
        tail_only,
    )


def _validated_d5_row(
    row: Mapping[str, Any],
    *,
    horizon_ms: int,
    programs: Mapping[str, SearchedWaveProgramV1],
) -> JSONMap:
    if not isinstance(row, Mapping) or row.get("schema") != ROW_SCHEMA:
        raise OfflineWaveD5SelectionV1Error("D5 evaluation row has the wrong schema")
    if row.get("cohort") not in COHORTS:
        raise OfflineWaveD5SelectionV1Error("D5 evaluation row has an invalid cohort")
    if row.get("complete_case_deleted") is not False or row.get(
        "failed_or_incomplete_imputed_as_zero"
    ) is not False:
        raise OfflineWaveD5SelectionV1Error(
            "D5 rows must not delete or zero-impute terminal evidence"
        )
    endpoint = row.get("endpoint_row")
    try:
        endpoint = _validated_d4_row(endpoint, horizon_ms=horizon_ms)
    except (OfflineWaveD4AllSeedEndpointV1Error, TypeError, ValueError) as error:
        raise OfflineWaveD5SelectionV1Error("D5 row has invalid D4 endpoint evidence") from error
    candidate_id = endpoint["candidate_id"]
    if candidate_id not in programs:
        raise OfflineWaveD5SelectionV1Error(
            f"D5 row names unknown candidate {candidate_id!r}"
        )
    if endpoint["controller_id"] != PI_STAR:
        raise OfflineWaveD5SelectionV1Error(
            "D5 candidate rows require the searched PI_STAR controller"
        )
    if endpoint["all_seed_endpoint_eligible"] is not True:
        raise OfflineWaveD5SelectionV1Error(
            "D5 exact panels cannot delete an invalid endpoint row"
        )
    attribution = _validated_attribution(
        row.get("action_attribution"), program=programs[candidate_id]
    )
    copied = deepcopy(dict(row))
    copied["endpoint_row"] = endpoint
    copied["action_attribution"] = attribution
    return copied


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise OfflineWaveD5SelectionV1Error("cannot aggregate an empty exact panel")
    return float(statistics.fmean(values))


def _optional_mean(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def _optional_median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _winner_delta_counts(
    values: Sequence[float], *, positive_better: bool
) -> JSONMap:
    wins = losses = ties = 0
    for value in values:
        if math.isclose(value, 0.0, rel_tol=1e-12, abs_tol=1e-9):
            ties += 1
        elif (value > 0) is positive_better:
            wins += 1
        else:
            losses += 1
    return {
        "winner_win_count": wins,
        "winner_loss_count": losses,
        "tie_count": ties,
    }


def _candidate_summaries(
    *,
    manifests: Sequence[JSONMap],
    rows: Sequence[JSONMap],
    required_pairs: Sequence[tuple[int, int]],
) -> list[JSONMap]:
    expected_pairs = set(required_pairs)
    manifest_by_id = {row["candidate_id"]: row for row in manifests}
    programs = _program_by_candidate(manifests)
    grouped: dict[str, list[JSONMap]] = defaultdict(list)
    for row in rows:
        endpoint = row["endpoint_row"]
        candidate_id = endpoint["candidate_id"]
        pair = (endpoint["simulator_seed"], endpoint["teammate_seed"])
        if pair not in expected_pairs:
            raise OfflineWaveD5SelectionV1Error(
                "D5 evaluation row is outside the frozen cohort"
            )
        grouped[candidate_id].append(row)

    summaries: list[JSONMap] = []
    for candidate_id in manifest_by_id:
        candidate_rows = grouped[candidate_id]
        observed = [
            (
                row["endpoint_row"]["simulator_seed"],
                row["endpoint_row"]["teammate_seed"],
            )
            for row in candidate_rows
        ]
        if len(observed) != len(set(observed)):
            raise OfflineWaveD5SelectionV1Error(
                f"candidate {candidate_id!r} repeats a paired seed"
            )
        missing = sorted(expected_pairs - set(observed))
        if missing or len(observed) != len(required_pairs):
            raise OfflineWaveD5SelectionV1Error(
                f"candidate {candidate_id!r} lacks exact paired seed coverage"
            )

        terminals = [row["endpoint_row"]["terminal_endpoint"] for row in candidate_rows]
        clear_count = sum(row["cleared_by_horizon"] is True for row in terminals)
        source_totals: Counter[str] = Counter({source: 0 for source in _ATTRIBUTION_SOURCES})
        per_seed_attribution: list[JSONMap] = []
        program = programs[candidate_id]
        per_step: dict[str, Counter[str]] = {
            step.step_id: Counter({outcome: 0 for outcome in _TERMINAL_STEP_OUTCOMES})
            for step in program.steps
        }
        step_kind = {step.step_id: step.kind.value for step in program.steps}
        for row in candidate_rows:
            endpoint = row["endpoint_row"]
            attribution = row["action_attribution"]
            counts = attribution["accepted_action_count_by_source"]
            source_totals.update(counts)
            categories = {
                "ACCEPTED": (
                    attribution["accepted_explicit_action_step_ids"]
                    + attribution["accepted_explicit_wait_step_ids"]
                ),
                "MISSED": attribution["missed_explicit_step_ids"],
                "SKIPPED": attribution["skipped_explicit_step_ids"],
                "SELECTED_BUT_UNACCEPTED": attribution[
                    "selected_but_unaccepted_explicit_step_ids"
                ],
                "NEVER_SELECTED": attribution["never_selected_explicit_step_ids"],
            }
            for outcome, step_ids in categories.items():
                for step_id in step_ids:
                    per_step[step_id][outcome] += 1
            per_seed_attribution.append(
                {
                    "simulator_seed": endpoint["simulator_seed"],
                    "teammate_seed": endpoint["teammate_seed"],
                    "accepted_explicit_action_count": counts[EXPLICIT_ACTION],
                    "accepted_tail_action_count": counts[TAIL_FILL],
                    "accepted_other_action_count": (
                        counts[CONTROLLER_WAIT] + counts[EXPLICIT_WAIT] + counts[OTHER]
                    ),
                    "missed_explicit_step_ids": deepcopy(
                        attribution["missed_explicit_step_ids"]
                    ),
                }
            )
        per_seed_attribution.sort(
            key=lambda row: (row["simulator_seed"], row["teammate_seed"])
        )
        summaries.append(
            {
                "candidate_id": candidate_id,
                "behavior_key": manifest_by_id[candidate_id]["behavior_key"],
                "role": manifest_by_id[candidate_id]["role"],
                "required_seed_pair_count": len(required_pairs),
                "technically_valid_seed_count": len(candidate_rows),
                "clear_count": clear_count,
                "completion_rate": clear_count / len(candidate_rows),
                "mean_residual_required_health_fraction": _mean(
                    [row["residual_required_health_fraction"] for row in terminals]
                ),
                "mean_focal_effective_damage_by_horizon_or_clear": _mean(
                    [row["focal_effective_damage_by_horizon_or_clear"] for row in terminals]
                ),
                "action_attribution": {
                    "accepted_action_count_by_source": dict(source_totals),
                    "accepted_explicit_action_count": source_totals[EXPLICIT_ACTION],
                    "accepted_tail_action_count": source_totals[TAIL_FILL],
                    "per_seed": per_seed_attribution,
                    "per_explicit_step": [
                        {
                            "step_id": step.step_id,
                            "step_kind": step_kind[step.step_id],
                            "terminal_outcome_seed_counts": dict(per_step[step.step_id]),
                        }
                        for step in program.steps
                    ],
                },
                "complete_case_deleted": False,
                "failed_or_incomplete_imputed_as_zero": False,
            }
        )
    return summaries


def _comparison(candidate: JSONMap, retention: JSONMap) -> JSONMap:
    completion_delta = candidate["completion_rate"] - retention["completion_rate"]
    residual_delta = (
        candidate["mean_residual_required_health_fraction"]
        - retention["mean_residual_required_health_fraction"]
    )
    focal_delta = (
        candidate["mean_focal_effective_damage_by_horizon_or_clear"]
        - retention["mean_focal_effective_damage_by_horizon_or_clear"]
    )
    eps = 1e-12
    feasible = completion_delta >= -eps and residual_delta <= eps
    return {
        "completion_rate_delta_vs_retention": completion_delta,
        "mean_residual_health_fraction_delta_vs_retention": residual_delta,
        "mean_focal_effective_damage_delta_vs_retention": focal_delta,
        "completion_gate_passed": completion_delta >= -eps,
        "residual_health_gate_passed": residual_delta <= eps,
        "feasible_vs_retention": feasible,
    }


def _validated_rows_for_cohort(
    *,
    raw_rows: Sequence[Mapping[str, Any]],
    cohort: str,
    horizon_ms: int,
    manifests: Sequence[JSONMap],
) -> list[JSONMap]:
    if not isinstance(raw_rows, Sequence) or isinstance(
        raw_rows, (str, bytes, bytearray)
    ):
        raise OfflineWaveD5SelectionV1Error("evaluation rows must be a sequence")
    programs = _program_by_candidate(manifests)
    rows = [
        _validated_d5_row(row, horizon_ms=horizon_ms, programs=programs)
        for row in raw_rows
    ]
    if any(row["cohort"] != cohort for row in rows):
        raise OfflineWaveD5SelectionV1Error(
            f"{cohort} adjudication accepts {cohort} rows only"
        )
    return rows


def select_d5_candidate_v1(
    *,
    contract: Mapping[str, Any],
    selection_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Freeze one D5 candidate using all selection seeds and no held-out data."""

    campaign_id, manifests, cohorts, horizon_ms, retention_id, tail_only_id = (
        _contract_parts(contract)
    )
    rows = _validated_rows_for_cohort(
        raw_rows=selection_rows,
        cohort=SELECTION,
        horizon_ms=horizon_ms,
        manifests=manifests,
    )
    summaries = _candidate_summaries(
        manifests=manifests, rows=rows, required_pairs=cohorts[SELECTION]
    )
    by_id = {row["candidate_id"]: row for row in summaries}
    retention = by_id[retention_id]
    compared: list[JSONMap] = []
    for summary in summaries:
        enriched = deepcopy(summary)
        enriched["comparison_vs_retention"] = _comparison(summary, retention)
        compared.append(enriched)
    feasible = [
        row for row in compared if row["comparison_vs_retention"]["feasible_vs_retention"]
    ]
    if not feasible:
        raise OfflineWaveD5SelectionV1Error(
            "retention candidate unexpectedly failed its own feasibility gates"
        )
    ranked = sorted(
        feasible,
        key=lambda row: (
            -row["mean_focal_effective_damage_by_horizon_or_clear"],
            row["behavior_key"],
            row["candidate_id"],
        ),
    )
    winner = ranked[0]
    manifest_by_id = {row["candidate_id"]: row for row in manifests}
    return {
        "schema": SELECTION_SCHEMA,
        "campaign_id": campaign_id,
        "status": "ONE_D5_CANDIDATE_FROZEN_FOR_HELDOUT",
        "frozen_candidate": deepcopy(manifest_by_id[winner["candidate_id"]]),
        "selection_score": deepcopy(winner),
        "retention_score": deepcopy(
            next(row for row in compared if row["candidate_id"] == retention_id)
        ),
        "tail_only_ablation_score": deepcopy(
            next(row for row in compared if row["candidate_id"] == tail_only_id)
        ),
        "candidate_summaries": compared,
        "feasible_candidate_ids": [row["candidate_id"] for row in ranked],
        "ranking_rule": deepcopy(contract["selection_rule"]),
        "selection_seed_pair_count": len(cohorts[SELECTION]),
        "heldout_rows_consumed": 0,
        "heldout_can_reselect": False,
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }


def evaluate_d5_frozen_candidate_heldout_v1(
    *,
    contract: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    heldout_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Confirm only the frozen winner versus retention; never re-rank candidates."""

    campaign_id, manifests, cohorts, horizon_ms, retention_id, _ = _contract_parts(
        contract
    )
    if (
        not isinstance(selection_receipt, Mapping)
        or selection_receipt.get("schema") != SELECTION_SCHEMA
        or selection_receipt.get("campaign_id") != campaign_id
        or selection_receipt.get("status")
        != "ONE_D5_CANDIDATE_FROZEN_FOR_HELDOUT"
        or selection_receipt.get("heldout_rows_consumed") != 0
        or selection_receipt.get("heldout_can_reselect") is not False
    ):
        raise OfflineWaveD5SelectionV1Error(
            "held-out adjudication requires an untouched D5 selection receipt"
        )
    frozen = selection_receipt.get("frozen_candidate")
    if not isinstance(frozen, Mapping):
        raise OfflineWaveD5SelectionV1Error("selection receipt lacks frozen candidate")
    manifest_by_id = {row["candidate_id"]: row for row in manifests}
    frozen_id = frozen.get("candidate_id")
    if frozen_id not in manifest_by_id or frozen != manifest_by_id[frozen_id]:
        raise OfflineWaveD5SelectionV1Error(
            "selection receipt frozen candidate differs from the contract"
        )
    required_ids = {retention_id, frozen_id}
    required_manifests = [
        row for row in manifests if row["candidate_id"] in required_ids
    ]
    rows = _validated_rows_for_cohort(
        raw_rows=heldout_rows,
        cohort=HELDOUT,
        horizon_ms=horizon_ms,
        manifests=required_manifests,
    )
    summaries = _candidate_summaries(
        manifests=required_manifests,
        rows=rows,
        required_pairs=cohorts[HELDOUT],
    )
    by_id = {row["candidate_id"]: row for row in summaries}
    retention = by_id[retention_id]
    frozen_score = by_id[frozen_id]
    comparison = _comparison(frozen_score, retention)
    eps = 1e-12
    focal_noninferior = (
        comparison["mean_focal_effective_damage_delta_vs_retention"] >= -eps
    )
    strict = (
        comparison["completion_rate_delta_vs_retention"] > eps
        or comparison["mean_residual_health_fraction_delta_vs_retention"] < -eps
        or comparison["mean_focal_effective_damage_delta_vs_retention"] > eps
    )
    return {
        "schema": HELDOUT_SCHEMA,
        "campaign_id": campaign_id,
        "status": "FROZEN_D5_CANDIDATE_HELDOUT_CONFIRMATION_COMPLETE",
        "frozen_candidate": deepcopy(dict(frozen)),
        "frozen_candidate_score": frozen_score,
        "retention_score": retention,
        "comparison_vs_retention": comparison,
        "frozen_candidate_model_dominates_retention": (
            comparison["feasible_vs_retention"] and focal_noninferior and strict
        ),
        "selection_reopened": False,
        "heldout_can_reselect": False,
        "heldout_candidate_ids_evaluated": sorted(required_ids),
        "heldout_seed_pair_count": len(cohorts[HELDOUT]),
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }


def _confirmation_candidate_by_controller(
    *,
    winner_candidate_id: str,
    retention_candidate_id: str,
    tail_only_candidate_id: str,
) -> dict[str, str]:
    return {
        PI_STAR: winner_candidate_id,
        D4_RETENTION: retention_candidate_id,
        D5_TAIL_ONLY: tail_only_candidate_id,
        CAT: winner_candidate_id,
        CONTRA_DEPLOYED: winner_candidate_id,
        CONTRA_NEW: winner_candidate_id,
        PI_D: winner_candidate_id,
    }


def _validated_confirmation_rows(
    *,
    raw_rows: Sequence[Mapping[str, Any]],
    horizon_ms: int,
    required_pairs: Sequence[tuple[int, int]],
    candidate_by_controller: Mapping[str, str],
) -> dict[tuple[int, int], dict[str, JSONMap]]:
    if not isinstance(raw_rows, Sequence) or isinstance(
        raw_rows, (str, bytes, bytearray)
    ):
        raise OfflineWaveD5SelectionV1Error(
            "confirmation_endpoint_rows must be a sequence"
        )
    required_pair_set = set(required_pairs)
    grouped: dict[tuple[int, int], dict[str, JSONMap]] = defaultdict(dict)
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping) or raw.get("schema") != CONFIRMATION_ROW_SCHEMA:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation endpoint row {index} has the wrong schema"
            )
        if raw.get("complete_case_deleted") is not False or raw.get(
            "failed_or_incomplete_imputed_as_zero"
        ) is not False:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation rows must not delete or zero-impute terminal evidence"
            )
        controller_id = raw.get("controller_id")
        if controller_id not in CONFIRMATION_CONTROLLER_IDS:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation row {index} has an unsupported controller"
            )
        try:
            endpoint = _validated_d4_row(
                raw.get("endpoint_row"), horizon_ms=horizon_ms
            )
        except (OfflineWaveD4AllSeedEndpointV1Error, TypeError, ValueError) as error:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation row {index} has invalid raw D4 endpoint evidence"
            ) from error
        expected_inner = (
            PI_STAR
            if controller_id in _SEARCHED_CONFIRMATION_CONTROLLER_IDS
            else controller_id
        )
        if endpoint["controller_id"] != expected_inner:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation controller differs from raw D4 endpoint controller"
            )
        if endpoint["candidate_id"] != candidate_by_controller[controller_id]:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation controller {controller_id!r} names the wrong candidate"
            )
        if endpoint["all_seed_endpoint_eligible"] is not True:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation cannot delete an invalid endpoint row"
            )
        pair = (endpoint["simulator_seed"], endpoint["teammate_seed"])
        if pair not in required_pair_set:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation endpoint row is outside the heldout cohort"
            )
        if controller_id in grouped[pair]:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation repeats one controller within a paired seed"
            )
        grouped[pair][controller_id] = endpoint

    expected_row_count = len(required_pairs) * len(CONFIRMATION_CONTROLLER_IDS)
    if len(raw_rows) != expected_row_count:
        raise OfflineWaveD5SelectionV1Error(
            "confirmation lacks exact 48-seed seven-controller coverage"
        )
    for pair in required_pairs:
        if set(grouped[pair]) != set(CONFIRMATION_CONTROLLER_IDS):
            raise OfflineWaveD5SelectionV1Error(
                "confirmation lacks the exact seven-controller panel"
            )
    return dict(grouped)


def _confirmation_controller_summary(
    endpoints: Sequence[Mapping[str, Any]],
) -> JSONMap:
    terminals = [row["terminal_endpoint"] for row in endpoints]
    cleared = [row for row in terminals if row["cleared_by_horizon"] is True]
    return {
        "requested_seed_count": len(endpoints),
        "technically_valid_seed_count": len(endpoints),
        "invalid_seed_count": 0,
        "clear_count": len(cleared),
        "completion_rate": len(cleared) / len(endpoints),
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
            [
                row["background_effective_damage_by_horizon_or_clear"]
                for row in terminals
            ]
        ),
        "conditional_clear_seed_count": len(cleared),
        "mean_focal_dps_if_cleared": _optional_mean(
            [row["focal_dps_if_cleared"] for row in cleared]
        ),
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }


def _confirmation_pairwise(
    *,
    pair_index: Mapping[tuple[int, int], Mapping[str, JSONMap]],
    pairs: Sequence[tuple[int, int]],
    comparator_id: str,
) -> JSONMap:
    winner_only_clear = comparator_only_clear = both_clear = neither_clear = 0
    clear_deltas: list[float] = []
    residual_deltas: list[float] = []
    focal_deltas: list[float] = []
    conditional_dps_deltas: list[float] = []
    for pair in pairs:
        winner = pair_index[pair][PI_STAR]["terminal_endpoint"]
        comparator = pair_index[pair][comparator_id]["terminal_endpoint"]
        winner_clear = bool(winner["cleared_by_horizon"])
        comparator_clear = bool(comparator["cleared_by_horizon"])
        clear_deltas.append(float(winner_clear) - float(comparator_clear))
        if winner_clear and comparator_clear:
            both_clear += 1
            conditional_dps_deltas.append(
                winner["focal_dps_if_cleared"]
                - comparator["focal_dps_if_cleared"]
            )
        elif winner_clear:
            winner_only_clear += 1
        elif comparator_clear:
            comparator_only_clear += 1
        else:
            neither_clear += 1
        residual_deltas.append(
            winner["residual_required_health_fraction"]
            - comparator["residual_required_health_fraction"]
        )
        focal_deltas.append(
            winner["focal_effective_damage_by_horizon_or_clear"]
            - comparator["focal_effective_damage_by_horizon_or_clear"]
        )

    completion_delta = _mean(clear_deltas)
    residual_delta = _mean(residual_deltas)
    focal_delta = _mean(focal_deltas)
    eps = 1e-12
    winner_dominates = (
        completion_delta >= -eps
        and residual_delta <= eps
        and focal_delta >= -eps
        and (
            completion_delta > eps
            or residual_delta < -eps
            or focal_delta > eps
        )
    )
    comparator_dominates = (
        completion_delta <= eps
        and residual_delta >= -eps
        and focal_delta <= eps
        and (
            completion_delta < -eps
            or residual_delta > eps
            or focal_delta < -eps
        )
    )
    return {
        "technically_valid_paired_seed_count": len(pairs),
        "both_clear_count": both_clear,
        "winner_only_clear_count": winner_only_clear,
        "comparator_only_clear_count": comparator_only_clear,
        "neither_clear_count": neither_clear,
        "completion_rate_delta": completion_delta,
        "mean_residual_health_fraction_delta": residual_delta,
        "median_residual_health_fraction_delta": _optional_median(residual_deltas),
        "mean_focal_effective_damage_delta": focal_delta,
        "median_focal_effective_damage_delta": _optional_median(focal_deltas),
        "mean_conditional_clear_dps_delta": _optional_mean(
            conditional_dps_deltas
        ),
        "median_conditional_clear_dps_delta": _optional_median(
            conditional_dps_deltas
        ),
        "residual_health_pair_counts": _winner_delta_counts(
            residual_deltas, positive_better=False
        ),
        "focal_damage_pair_counts": _winner_delta_counts(
            focal_deltas, positive_better=True
        ),
        "conditional_clear_dps_pair_counts": _winner_delta_counts(
            conditional_dps_deltas, positive_better=True
        ),
        "model_dominance": (
            "D5_WINNER_MODEL_DOMINATES"
            if winner_dominates
            else "COMPARATOR_MODEL_DOMINATES"
            if comparator_dominates
            else "MIXED_OR_TIED"
        ),
        "conditional_clear_dps_is_diagnostic_only": True,
    }


def _confirmation_action_attribution_diagnostic(
    *,
    values: Sequence[Mapping[str, Any]] | None,
    pairs: Sequence[tuple[int, int]],
    programs: Mapping[str, SearchedWaveProgramV1],
    candidate_by_controller: Mapping[str, str],
) -> JSONMap:
    if values is None:
        return {
            "status": "NOT_SUPPLIED",
            "required_for_endpoint_adjudication": False,
            "per_controller": None,
        }
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        raise OfflineWaveD5SelectionV1Error(
            "confirmation_action_attributions must be a sequence"
        )
    pair_set = set(pairs)
    grouped: dict[str, dict[tuple[int, int], JSONMap]] = {
        controller_id: {}
        for controller_id in _SEARCHED_CONFIRMATION_CONTROLLER_IDS
    }
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation attribution {index} must be a mapping"
            )
        controller_id = raw.get("controller_id")
        if controller_id not in CONFIRMATION_CONTROLLER_IDS:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation attribution {index} has an unknown controller"
            )
        candidate_id = raw.get("candidate_id")
        if candidate_id != candidate_by_controller[controller_id]:
            raise OfflineWaveD5SelectionV1Error(
                f"confirmation attribution {index} names the wrong candidate"
            )
        pair = (
            _nonnegative_int(
                raw.get("simulator_seed"),
                f"confirmation attribution {index}.simulator_seed",
            ),
            _nonnegative_int(
                raw.get("teammate_seed"),
                f"confirmation attribution {index}.teammate_seed",
            ),
        )
        if pair not in pair_set:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation attribution is outside the heldout cohort"
            )
        attribution = raw.get("attribution")
        if controller_id not in _SEARCHED_CONFIRMATION_CONTROLLER_IDS:
            if attribution is not None:
                raise OfflineWaveD5SelectionV1Error(
                    "native baseline confirmation attribution must be null"
                )
            continue
        if pair in grouped[controller_id]:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation repeats searched attribution for one seed"
            )
        grouped[controller_id][pair] = _validated_attribution(
            attribution, program=programs[candidate_id]
        )

    per_controller: JSONMap = {}
    for controller_id in _SEARCHED_CONFIRMATION_CONTROLLER_IDS:
        if set(grouped[controller_id]) != pair_set:
            raise OfflineWaveD5SelectionV1Error(
                "confirmation action attribution lacks exact searched-lane coverage"
            )
        program = programs[candidate_by_controller[controller_id]]
        source_totals: Counter[str] = Counter(
            {source: 0 for source in _ATTRIBUTION_SOURCES}
        )
        per_step: dict[str, Counter[str]] = {
            step.step_id: Counter(
                {outcome: 0 for outcome in _TERMINAL_STEP_OUTCOMES}
            )
            for step in program.steps
        }
        per_seed: list[JSONMap] = []
        for pair in pairs:
            attribution = grouped[controller_id][pair]
            counts = attribution["accepted_action_count_by_source"]
            source_totals.update(counts)
            categories = {
                "ACCEPTED": (
                    attribution["accepted_explicit_action_step_ids"]
                    + attribution["accepted_explicit_wait_step_ids"]
                ),
                "MISSED": attribution["missed_explicit_step_ids"],
                "SKIPPED": attribution["skipped_explicit_step_ids"],
                "SELECTED_BUT_UNACCEPTED": attribution[
                    "selected_but_unaccepted_explicit_step_ids"
                ],
                "NEVER_SELECTED": attribution[
                    "never_selected_explicit_step_ids"
                ],
            }
            for outcome, step_ids in categories.items():
                for step_id in step_ids:
                    per_step[step_id][outcome] += 1
            per_seed.append(
                {
                    "simulator_seed": pair[0],
                    "teammate_seed": pair[1],
                    "accepted_explicit_action_count": counts[EXPLICIT_ACTION],
                    "accepted_tail_action_count": counts[TAIL_FILL],
                    "missed_explicit_step_ids": deepcopy(
                        attribution["missed_explicit_step_ids"]
                    ),
                }
            )
        per_controller[controller_id] = {
            "accepted_action_count_by_source": dict(source_totals),
            "accepted_explicit_action_count": source_totals[EXPLICIT_ACTION],
            "accepted_tail_action_count": source_totals[TAIL_FILL],
            "per_seed": per_seed,
            "per_explicit_step": [
                {
                    "step_id": step.step_id,
                    "step_kind": step.kind.value,
                    "terminal_outcome_seed_counts": dict(per_step[step.step_id]),
                }
                for step in program.steps
            ],
        }
    return {
        "status": "EXACT_SEARCHED_LANE_ATTRIBUTION_COMPLETE",
        "required_for_endpoint_adjudication": False,
        "per_controller": per_controller,
    }


def adjudicate_d5_confirmation_v1(
    selection_contract: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    confirmation_endpoint_rows: Sequence[Mapping[str, Any]],
    confirmation_action_attributions: Sequence[Mapping[str, Any]] | None = None,
) -> JSONMap:
    """Adjudicate a frozen D5 winner on exactly 48 fresh held-out seed pairs."""

    campaign_id, manifests, cohorts, horizon_ms, retention_id, tail_only_id = (
        _contract_parts(selection_contract)
    )
    pairs = cohorts[HELDOUT]
    if len(pairs) != CONFIRMATION_PAIR_COUNT:
        raise OfflineWaveD5SelectionV1Error(
            f"D5 confirmation requires exactly {CONFIRMATION_PAIR_COUNT} heldout pairs"
        )
    if (
        not isinstance(selection_receipt, Mapping)
        or selection_receipt.get("schema") != SELECTION_SCHEMA
        or selection_receipt.get("campaign_id") != campaign_id
        or selection_receipt.get("status")
        != "ONE_D5_CANDIDATE_FROZEN_FOR_HELDOUT"
        or selection_receipt.get("heldout_rows_consumed") != 0
        or selection_receipt.get("heldout_can_reselect") is not False
    ):
        raise OfflineWaveD5SelectionV1Error(
            "confirmation requires an untouched frozen D5 selection receipt"
        )
    frozen = selection_receipt.get("frozen_candidate")
    if not isinstance(frozen, Mapping):
        raise OfflineWaveD5SelectionV1Error("selection receipt lacks frozen candidate")
    manifest_by_id = {row["candidate_id"]: row for row in manifests}
    winner_id = frozen.get("candidate_id")
    if winner_id not in manifest_by_id or frozen != manifest_by_id[winner_id]:
        raise OfflineWaveD5SelectionV1Error(
            "selection receipt frozen candidate differs from the selection contract"
        )
    candidate_by_controller = _confirmation_candidate_by_controller(
        winner_candidate_id=winner_id,
        retention_candidate_id=retention_id,
        tail_only_candidate_id=tail_only_id,
    )
    pair_index = _validated_confirmation_rows(
        raw_rows=confirmation_endpoint_rows,
        horizon_ms=horizon_ms,
        required_pairs=pairs,
        candidate_by_controller=candidate_by_controller,
    )
    per_controller = {
        controller_id: _confirmation_controller_summary(
            [pair_index[pair][controller_id] for pair in pairs]
        )
        for controller_id in CONFIRMATION_CONTROLLER_IDS
    }
    pairwise = {
        comparator_id: _confirmation_pairwise(
            pair_index=pair_index,
            pairs=pairs,
            comparator_id=comparator_id,
        )
        for comparator_id in CONFIRMATION_CONTROLLER_IDS
        if comparator_id != PI_STAR
    }
    programs = _program_by_candidate(manifests)
    attribution = _confirmation_action_attribution_diagnostic(
        values=confirmation_action_attributions,
        pairs=pairs,
        programs=programs,
        candidate_by_controller=candidate_by_controller,
    )
    dominates_all = all(
        row["model_dominance"] == "D5_WINNER_MODEL_DOMINATES"
        for row in pairwise.values()
    )
    return {
        "schema": CONFIRMATION_SCHEMA,
        "campaign_id": campaign_id,
        "status": "FRESH_D5_CONFIRMATION_COMPLETE",
        "requested_seed_count": len(pairs),
        "all_seven_controllers_valid_seed_count": len(pairs),
        "frozen_candidate": deepcopy(dict(frozen)),
        "candidate_id_by_controller": dict(candidate_by_controller),
        "per_controller": per_controller,
        "winner_pairwise_by_comparator": pairwise,
        "action_attribution_diagnostic": attribution,
        "development_model_superiority_status": (
            "D5_WINNER_PARETO_DOMINATES_ALL_COMPARATORS"
            if dominates_all
            else "NOT_ESTABLISHED"
        ),
        "selection_reopened": False,
        "heldout_can_reselect": False,
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
        "real_environment_superiority_authorized": False,
        "deployment_authorized": False,
    }


__all__ = (
    "CANDIDATE_ROLES",
    "CANDIDATE_SCHEMA",
    "CONFIRMATION_CONTROLLER_IDS",
    "CONFIRMATION_PAIR_COUNT",
    "CONFIRMATION_ROW_SCHEMA",
    "CONFIRMATION_SCHEMA",
    "CONTRACT_SCHEMA",
    "D4_RETENTION",
    "D5_TAIL_ONLY",
    "HELDOUT",
    "HELDOUT_SCHEMA",
    "OfflineWaveD5SelectionV1Error",
    "RETENTION",
    "ROW_SCHEMA",
    "SCHEMA",
    "SEARCHED",
    "SELECTION",
    "SELECTION_SCHEMA",
    "TAIL_ONLY",
    "adjudicate_d5_confirmation_v1",
    "build_d5_confirmation_endpoint_row_v1",
    "build_d5_selection_contract_v1",
    "build_d5_selection_row_v1",
    "evaluate_d5_frozen_candidate_heldout_v1",
    "select_d5_candidate_v1",
    "validate_d5_candidate_manifests_v1",
)
