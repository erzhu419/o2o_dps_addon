"""Deterministic D5 whole-wave candidates around the frozen D4 winner.

D5 does not reopen or mutate the D3/D4 evidence.  It retains that exact
searched program, adds a horizon-safe tail-only ablation, and builds one
seed-invariant finite search panel.  Search candidates widen the explicit
steps that the frozen winner made easiest to miss, vary their execution
centres, both gap behaviours, and complete tail orders.  Candidate budget is
stratified before truncation: a four-candidate budget therefore covers all
four non-zero lateness profiles and both gap behaviours.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import permutations, product
from typing import Any, Mapping, Sequence

from .offline_wave_policy_v1 import LANE_GCD, LANE_OFF_GCD, LANE_QUEUE
from .offline_wave_searched_program_v1 import (
    MAX_SUPPORTED_EDITS,
    SearchedWaveEditKindV1,
    SearchedWaveEditV1,
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveProgramV1Error,
    SearchedWaveStepKindV1,
    materialize_searched_wave_program_v1,
    searched_wave_program_from_dict_v1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError


JSONMap = dict[str, Any]

SCHEMA = "offline_wave_d5_candidate_panel/v1"
CANDIDATE_SCHEMA = f"{SCHEMA}/candidate"
COVERAGE_SCHEMA = f"{SCHEMA}/coverage_receipt"

RETENTION = "RETENTION"
TAIL_ONLY = "TAIL_ONLY"
SEARCHED = "SEARCHED"
ROLES = frozenset({RETENTION, TAIL_ONLY, SEARCHED})

PARENT_CANDIDATE_ID = "d5-parent-retention"
TAIL_ONLY_CANDIDATE_ID = "d5-tail-only-ablation"

_D5_SEARCH_SOURCE = "D5_MISSED_STEP_WHOLE_PROGRAM_SEARCH_V1"
_D5_TAIL_SOURCE = "D5_TAIL_ONLY_ABLATION_V1"
_SHIFT_MS = (0, -250, 250, -500, 500)
_LANES = (LANE_GCD, LANE_OFF_GCD, LANE_QUEUE)
_PROFILES: tuple[tuple[str, dict[str, int]], ...] = (
    ("UNIFORM_250", {LANE_GCD: 250, LANE_OFF_GCD: 250, LANE_QUEUE: 250}),
    ("UNIFORM_500", {LANE_GCD: 500, LANE_OFF_GCD: 500, LANE_QUEUE: 500}),
    ("UNIFORM_1000", {LANE_GCD: 1_000, LANE_OFF_GCD: 1_000, LANE_QUEUE: 1_000}),
    ("MIXED_1000_250_500", {LANE_GCD: 1_000, LANE_OFF_GCD: 250, LANE_QUEUE: 500}),
)
_PROFILE_BY_NAME = {name: values for name, values in _PROFILES}
_CONTRACT = {
    "frozen_parent_retained_exactly": True,
    "tail_only_uses_post_horizon_sentinel": True,
    "searched_program_shape_shared_by_all_seeds": True,
    "searched_lateness_profiles_are_nonzero": True,
    "candidate_budget_is_stratified_before_truncation": True,
    "d3_semantics_modified": False,
}


class OfflineWaveD5CandidatePanelV1Error(ValueError):
    """The frozen winner or requested finite D5 panel is invalid."""


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OfflineWaveD5CandidatePanelV1Error(
            f"{label} must be a positive integer"
        )
    return value


def _manifest(candidate_id: str, role: str, program: SearchedWaveProgramV1) -> JSONMap:
    return {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "role": role,
        "proposal_arm": role,
        "behavior_key": program.behavior_key(),
        "program": program.to_dict(),
    }


def _guide_order_by_lane(
    parent: SearchedWaveProgramV1,
    values: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, ...]], int, tuple[JSONMap, ...]]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values, Sequence
    ):
        raise TypeError("offline_guide_actions must be a sequence")
    if not values:
        raise OfflineWaveD5CandidatePanelV1Error(
            "offline_guide_actions has no accepted guide action"
        )

    key_by_identity: dict[tuple[ActionRef, str], str] = {}
    for step in parent.steps:
        if step.kind is not SearchedWaveStepKindV1.ACTION:
            continue
        identity = (step.action_ref, step.lane)
        prior = key_by_identity.setdefault(identity, step.action_key)
        if prior != step.action_key:
            raise OfflineWaveD5CandidatePanelV1Error(
                "one parent action identity maps to multiple action keys"
            )

    ordered: dict[str, list[str]] = {lane: [] for lane in _LANES}
    ignored: list[JSONMap] = []
    last_time = -1
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"offline_guide_actions[{index}] must be a mapping"
            )
        lane = raw.get("lane")
        if lane not in _LANES:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"offline_guide_actions[{index}].lane is unsupported"
            )
        state_time_ms = raw.get("state_time_ms")
        if (
            isinstance(state_time_ms, bool)
            or not isinstance(state_time_ms, int)
            or state_time_ms < 0
        ):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"offline_guide_actions[{index}].state_time_ms must be nonnegative"
            )
        if state_time_ms < last_time:
            raise OfflineWaveD5CandidatePanelV1Error(
                "offline_guide_actions must preserve accepted execution order"
            )
        last_time = state_time_ms
        try:
            action = ActionRef.from_wire(raw.get("action"))
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"offline_guide_actions[{index}].action is invalid"
            ) from error
        key = key_by_identity.get((action, lane))
        if key is None:
            # The accepted historical guide can legitimately contain opener
            # actions deleted by the frozen D3/D4 winner.  They remain source
            # evidence, but cannot define a tail priority for that winner.
            ignored.append(
                {
                    "guide_index": index,
                    "state_time_ms": state_time_ms,
                    "action": action.to_wire(),
                    "lane": lane,
                    "reason": "NOT_IN_FROZEN_PARENT_ACTION_VOCABULARY",
                }
            )
            continue
        if key not in ordered[lane]:
            ordered[lane].append(key)
    supported_count = len(values) - len(ignored)
    if supported_count == 0:
        raise OfflineWaveD5CandidatePanelV1Error(
            "offline_guide_actions has no action supported by the frozen parent"
        )
    return (
        {lane: tuple(keys) for lane, keys in ordered.items()},
        supported_count,
        tuple(ignored),
    )


def _guide_priority(parent: tuple[str, ...], guide: tuple[str, ...]) -> tuple[str, ...]:
    accepted = tuple(key for key in guide if key in parent)
    return accepted + tuple(key for key in parent if key not in accepted)


def _ordered_permutations(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    if len(values) <= 1:
        return (values,)
    preferred = [values]
    preferred.extend(values[index:] + values[:index] for index in range(1, len(values)))
    preferred.append(tuple(reversed(values)))
    preferred.extend(sorted(permutations(values)))
    return tuple(dict.fromkeys(preferred))


def _tail_variants(
    parent: SearchedWaveProgramV1,
    guide_by_lane: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    guided_gcd = _guide_priority(
        parent.tail_gcd_priority, guide_by_lane[LANE_GCD]
    )
    guided_queue = _guide_priority(
        parent.tail_queue_priority, guide_by_lane[LANE_QUEUE]
    )
    gcd_orders = _ordered_permutations(guided_gcd)
    queue_orders = _ordered_permutations(guided_queue)

    # Diagonalize the first rows so a small budget changes GCD order instead
    # of exhausting every queue permutation under one GCD prefix.
    preferred: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for offset in range(max(len(gcd_orders), len(queue_orders))):
        for queue_offset in range(len(queue_orders)):
            preferred.append(
                (
                    gcd_orders[offset % len(gcd_orders)],
                    queue_orders[(offset + queue_offset) % len(queue_orders)],
                )
            )
    preferred.extend(product(gcd_orders, queue_orders))
    return tuple(dict.fromkeys(preferred))


def _spread_missed_steps(
    parent: SearchedWaveProgramV1,
    *,
    limit: int,
) -> tuple[str, ...]:
    missed = tuple(
        step.step_id
        for step in parent.steps
        if step.kind is SearchedWaveStepKindV1.ACTION
        and step.max_lateness_ms == 0
    )
    if not missed:
        raise OfflineWaveD5CandidatePanelV1Error(
            "frozen parent has no zero-lateness explicit step to repair"
        )
    if len(missed) <= limit:
        return missed
    if limit == 1:
        return (missed[len(missed) // 2],)
    indexes = tuple(
        (ordinal * (len(missed) - 1)) // (limit - 1)
        for ordinal in range(limit)
    )
    return tuple(missed[index] for index in indexes)


def _shifted_time(
    parent: SearchedWaveProgramV1,
    step_index: int,
    delta_ms: int,
) -> int:
    step = parent.steps[step_index]
    if delta_ms < 0:
        lower = parent.steps[step_index - 1].at_or_after_ms if step_index else 0
        return max(lower, step.at_or_after_ms + delta_ms)
    if delta_ms > 0:
        upper = (
            parent.steps[step_index + 1].at_or_after_ms
            if step_index + 1 < len(parent.steps)
            else step.at_or_after_ms + delta_ms
        )
        return min(upper, step.at_or_after_ms + delta_ms)
    return step.at_or_after_ms


def _tail_signature(program: SearchedWaveProgramV1) -> JSONMap:
    return {
        "gcd": list(program.tail_gcd_priority),
        "queue": list(program.tail_queue_priority),
        "off_gcd_once": list(program.tail_off_gcd_once),
    }


def _tail_only_program(
    parent: SearchedWaveProgramV1,
    horizon_ms: int,
) -> SearchedWaveProgramV1:
    sentinel_source = tuple(dict.fromkeys((*parent.source_refs, _D5_TAIL_SOURCE)))
    first_action = next(
        step
        for step in parent.steps
        if step.kind is SearchedWaveStepKindV1.ACTION
    )
    sentinel = replace(
        first_action,
        step_id="d5-tail-only-post-horizon-sentinel",
        at_or_after_ms=horizon_ms + 1,
        max_lateness_ms=0,
        proposal_source=_D5_TAIL_SOURCE,
    )
    return SearchedWaveProgramV1(
        program_id=TAIL_ONLY_CANDIDATE_ID,
        parent_program_id=parent.program_id,
        source_refs=sentinel_source,
        applied_edit_ids=("tail-only-post-horizon-sentinel",),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(sentinel,),
        tail_gcd_priority=parent.tail_gcd_priority,
        tail_queue_priority=parent.tail_queue_priority,
        tail_off_gcd_once=parent.tail_off_gcd_once,
    )


def _search_program(
    parent: SearchedWaveProgramV1,
    *,
    candidate_id: str,
    profile: Mapping[str, int],
    gap_behavior: SearchedWaveGapBehaviorV1,
    tail_variant: tuple[tuple[str, ...], tuple[str, ...]],
    shift_ms: int,
    missed_step_ids: Sequence[str],
) -> SearchedWaveProgramV1:
    index_by_id = {step.step_id: index for index, step in enumerate(parent.steps)}
    edits: list[SearchedWaveEditV1] = []
    for step_id in missed_step_ids:
        index = index_by_id[step_id]
        step = parent.steps[index]
        edits.append(
            SearchedWaveEditV1(
                edit_id=f"widen-{step_id}-{profile[step.lane]}-shift-{shift_ms}",
                kind=SearchedWaveEditKindV1.RETIME,
                proposal_source=_D5_SEARCH_SOURCE,
                target_step_id=step_id,
                at_or_after_ms=_shifted_time(parent, index, shift_ms),
                max_lateness_ms=profile[step.lane],
            )
        )
    for lane, proposed, current in (
        (LANE_GCD, tail_variant[0], parent.tail_gcd_priority),
        (LANE_QUEUE, tail_variant[1], parent.tail_queue_priority),
    ):
        if proposed == current:
            continue
        edits.append(
            SearchedWaveEditV1(
                edit_id=f"reorder-{lane}-tail",
                kind=SearchedWaveEditKindV1.TAIL_REORDER,
                proposal_source=_D5_SEARCH_SOURCE,
                tail_lane=lane,
                tail_priority=proposed,
            )
        )
    if len(edits) > MAX_SUPPORTED_EDITS:
        raise OfflineWaveD5CandidatePanelV1Error(
            "D5 whole-program edit batch exceeds searched-program v1 bound"
        )
    try:
        materialized = materialize_searched_wave_program_v1(
            parent,
            edits,
            program_id=candidate_id,
            max_edits=MAX_SUPPORTED_EDITS,
            source_refs=(_D5_SEARCH_SOURCE,),
        )
    except SearchedWaveProgramV1Error as error:
        raise OfflineWaveD5CandidatePanelV1Error(
            f"cannot materialize {candidate_id}"
        ) from error
    return replace(materialized, gap_behavior=gap_behavior)


def _candidate_specs(
    tail_count: int,
) -> tuple[tuple[int, int, int, int], ...]:
    """Return (profile, gap, tail, shift) with balanced finite prefixes."""

    rows: list[tuple[int, int, int, int]] = []
    # For each profile, q is a bijection over gap x shift x tail.  The first
    # ten q values cross every shift with both gap behaviours, rather than
    # confounding one timing shift with each lateness profile.  Tail indices
    # are spread across that prefix while remaining bijective over the full
    # finite space.  Emitting all four profiles per q keeps every finite
    # four-row prefix profile-balanced and gives budget four two gaps/four
    # distinct tail orders.
    shift_gap_count = len(_SHIFT_MS) * 2
    tail_stride = max(1, tail_count // shift_gap_count)
    for q in range(2 * tail_count * len(_SHIFT_MS)):
        residue = q % shift_gap_count
        tail_cycle = q // shift_gap_count
        for profile_index in range(len(_PROFILES)):
            rows.append(
                (
                    profile_index,
                    (residue + profile_index) % 2,
                    (
                        tail_cycle
                        + residue * tail_stride
                        + profile_index
                    )
                    % tail_count,
                    ((residue // 2) + profile_index) % len(_SHIFT_MS),
                )
            )
    if len(set(rows)) != len(rows):  # pragma: no cover - construction proof
        raise AssertionError("D5 stratified candidate schedule is not unique")
    return tuple(rows)


def _coverage_receipt(
    *,
    budget: int,
    supported_guide_action_count: int,
    ignored_guide_actions: Sequence[Mapping[str, Any]],
    common_missed_step_ids: Sequence[str],
    rows: Sequence[JSONMap],
    horizon_ms: int,
) -> JSONMap:
    profiles = sorted({row["lateness_profile"] for row in rows})
    gaps = sorted({row["gap_behavior"] for row in rows})
    tails = {
        (
            tuple(row["tail_signature"]["gcd"]),
            tuple(row["tail_signature"]["queue"]),
            tuple(row["tail_signature"]["off_gcd_once"]),
        )
        for row in rows
    }
    required_profiles = [name for name, _ in _PROFILES]
    required_shifts = list(_SHIFT_MS)
    required_gap_behaviors = sorted(
        behavior.value for behavior in SearchedWaveGapBehaviorV1
    )
    required_shift_gap_pair_count = len(required_shifts) * len(
        required_gap_behaviors
    )
    profile_cross_coverage: JSONMap = {}
    for profile_name in required_profiles:
        profile_rows = [
            row for row in rows if row["lateness_profile"] == profile_name
        ]
        covered_shifts = {
            row["timing_shift_ms"] for row in profile_rows
        }
        covered_profile_gaps = {
            row["gap_behavior"] for row in profile_rows
        }
        covered_pairs = {
            (row["timing_shift_ms"], row["gap_behavior"])
            for row in profile_rows
        }
        profile_cross_coverage[profile_name] = {
            "covered_timing_shifts": [
                shift for shift in required_shifts if shift in covered_shifts
            ],
            "covered_gap_behaviors": sorted(covered_profile_gaps),
            "covered_shift_gap_pair_count": len(covered_pairs),
            "required_shift_gap_pair_count": required_shift_gap_pair_count,
        }
    cross_coverage_required = budget >= (
        len(required_profiles) * required_shift_gap_pair_count
    )
    cross_coverage_satisfied = all(
        row["covered_timing_shifts"] == required_shifts
        and row["covered_gap_behaviors"] == required_gap_behaviors
        and row["covered_shift_gap_pair_count"]
        == row["required_shift_gap_pair_count"]
        for row in profile_cross_coverage.values()
    )
    profiles_covered = budget < len(_PROFILES) or set(profiles) == set(required_profiles)
    gaps_covered = len(gaps) == len(SearchedWaveGapBehaviorV1)
    tails_covered = len(tails) >= min(2, budget)
    nonzero = all(
        all(value > 0 for value in row["lateness_by_lane_ms"].values())
        for row in rows
    )
    return {
        "schema": COVERAGE_SCHEMA,
        "searched_candidate_budget": budget,
        "generated_searched_candidate_count": len(rows),
        "guide_action_count": supported_guide_action_count + len(ignored_guide_actions),
        "supported_guide_action_count": supported_guide_action_count,
        "ignored_guide_action_count": len(ignored_guide_actions),
        "ignored_guide_actions": [deepcopy(dict(row)) for row in ignored_guide_actions],
        "common_missed_step_ids": list(common_missed_step_ids),
        "required_lateness_profiles": required_profiles,
        "covered_lateness_profiles": profiles,
        "required_timing_shifts_ms": required_shifts,
        "profile_shift_gap_cross_coverage_required": cross_coverage_required,
        "profile_shift_gap_cross_coverage_satisfied": cross_coverage_satisfied,
        "profile_cross_coverage": profile_cross_coverage,
        "covered_gap_behaviors": gaps,
        "distinct_tail_order_count": len(tails),
        "all_search_lateness_profiles_nonzero": nonzero,
        "core_lateness_profiles_covered": profiles_covered,
        "both_gap_behaviors_covered": gaps_covered,
        "multiple_tail_orders_covered": tails_covered,
        "coverage_satisfied": (
            len(rows) == budget
            and nonzero
            and profiles_covered
            and gaps_covered
            and tails_covered
            and (not cross_coverage_required or cross_coverage_satisfied)
        ),
        "tail_only_ablation": {
            "candidate_id": TAIL_ONLY_CANDIDATE_ID,
            "status": "MATERIALIZED_WITH_POST_HORIZON_SENTINEL",
            "sentinel_at_ms": horizon_ms + 1,
            "explicit_action_executed_within_horizon": False,
        },
        "candidate_coverage": [deepcopy(row) for row in rows],
    }


def validate_d5_candidate_manifests_v1(
    values: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    """Validate manifest wire shape, executable programs, and behavior uniqueness."""

    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values, Sequence
    ):
        raise TypeError("candidate manifests must be a sequence")
    expected = {
        "schema",
        "candidate_id",
        "role",
        "proposal_arm",
        "behavior_key",
        "program",
    }
    copied: list[JSONMap] = []
    ids: set[str] = set()
    behaviors: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping) or set(value) != expected:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate manifest {index} has the wrong fields"
            )
        if value.get("schema") != CANDIDATE_SCHEMA:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate manifest {index} has the wrong schema"
            )
        candidate_id = value.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate manifest {index} has an invalid candidate_id"
            )
        if candidate_id in ids:
            raise OfflineWaveD5CandidatePanelV1Error("candidate IDs must be unique")
        ids.add(candidate_id)
        role = value.get("role")
        if role not in ROLES or value.get("proposal_arm") != role:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} has an invalid role/proposal_arm"
            )
        try:
            program = searched_wave_program_from_dict_v1(value.get("program"))
        except (SearchedWaveProgramV1Error, TypeError, ValueError) as error:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} has an invalid searched program"
            ) from error
        behavior_key = program.behavior_key()
        if value.get("behavior_key") != behavior_key:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} behavior_key does not match its program"
            )
        if behavior_key in behaviors:
            raise OfflineWaveD5CandidatePanelV1Error(
                "candidate executable behaviors must be unique"
            )
        behaviors.add(behavior_key)
        if role in {TAIL_ONLY, SEARCHED} and program.program_id != candidate_id:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} program_id must equal candidate_id"
            )
        copied.append(deepcopy(dict(value)))
    return copied


def validate_d5_candidate_panel_v1(value: Mapping[str, Any]) -> JSONMap:
    """Validate the complete panel and recompute its advertised coverage."""

    expected = {
        "schema",
        "horizon_ms",
        "searched_candidate_budget",
        "parent_behavior_key",
        "manifests",
        "coverage_receipt",
        "contract",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise OfflineWaveD5CandidatePanelV1Error("D5 panel has the wrong fields")
    if value.get("schema") != SCHEMA or value.get("contract") != _CONTRACT:
        raise OfflineWaveD5CandidatePanelV1Error(
            "D5 panel schema or contract is unsupported"
        )
    horizon_ms = _positive_int(value.get("horizon_ms"), "horizon_ms")
    budget = _positive_int(
        value.get("searched_candidate_budget"), "searched_candidate_budget"
    )
    if budget < len(_PROFILES):
        raise OfflineWaveD5CandidatePanelV1Error(
            "searched_candidate_budget must be at least 4 for stratified coverage"
        )
    manifests = validate_d5_candidate_manifests_v1(value.get("manifests"))
    expected_ids = [PARENT_CANDIDATE_ID, TAIL_ONLY_CANDIDATE_ID] + [
        f"d5-searched-{index:03d}" for index in range(1, budget + 1)
    ]
    if [row["candidate_id"] for row in manifests] != expected_ids:
        raise OfflineWaveD5CandidatePanelV1Error(
            "candidate IDs/order do not match the frozen D5 panel"
        )
    roles = [row["role"] for row in manifests]
    if roles != [RETENTION, TAIL_ONLY] + [SEARCHED] * budget:
        raise OfflineWaveD5CandidatePanelV1Error(
            "D5 panel must contain one retention, one tail-only, and the full search budget"
        )
    if manifests[0]["behavior_key"] != value.get("parent_behavior_key"):
        raise OfflineWaveD5CandidatePanelV1Error(
            "parent_behavior_key differs from the retained parent"
        )

    parent_program = searched_wave_program_from_dict_v1(manifests[0]["program"])
    tail_program = searched_wave_program_from_dict_v1(manifests[1]["program"])
    if (
        tail_program.gap_behavior
        is not SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
        or len(tail_program.steps) != 1
        or tail_program.steps[0].kind is not SearchedWaveStepKindV1.ACTION
        or tail_program.steps[0].at_or_after_ms != horizon_ms + 1
        or tail_program.parent_program_id != parent_program.program_id
        or tail_program.tail_gcd_priority != parent_program.tail_gcd_priority
        or tail_program.tail_queue_priority != parent_program.tail_queue_priority
        or tail_program.tail_off_gcd_once != parent_program.tail_off_gcd_once
        or not set(parent_program.source_refs).issubset(tail_program.source_refs)
    ):
        raise OfflineWaveD5CandidatePanelV1Error(
            "tail-only ablation must use one post-horizon sentinel action"
        )

    receipt = value.get("coverage_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("schema") != COVERAGE_SCHEMA:
        raise OfflineWaveD5CandidatePanelV1Error("coverage receipt is invalid")
    coverage_rows = receipt.get("candidate_coverage")
    if not isinstance(coverage_rows, list) or len(coverage_rows) != budget:
        raise OfflineWaveD5CandidatePanelV1Error(
            "coverage receipt does not account for the full searched budget"
        )
    by_id = {row["candidate_id"]: row for row in manifests[2:]}
    if [row.get("candidate_id") for row in coverage_rows] != list(by_id):
        raise OfflineWaveD5CandidatePanelV1Error(
            "coverage receipt does not preserve searched candidate order"
        )
    common_missed = receipt.get("common_missed_step_ids")
    expected_missed = list(_spread_missed_steps(parent_program, limit=6))
    if common_missed != expected_missed:
        raise OfflineWaveD5CandidatePanelV1Error(
            "coverage receipt names the wrong common missed steps"
        )
    rebuilt_rows: list[JSONMap] = []
    expected_coverage_fields = {
        "candidate_id",
        "lateness_profile",
        "lateness_by_lane_ms",
        "gap_behavior",
        "timing_shift_ms",
        "retimed_step_ids",
        "applied_edit_ids",
        "tail_signature",
    }
    for raw in coverage_rows:
        if not isinstance(raw, Mapping) or set(raw) != expected_coverage_fields:
            raise OfflineWaveD5CandidatePanelV1Error(
                "candidate coverage row has the wrong fields"
            )
        candidate_id = raw.get("candidate_id")
        manifest = by_id.get(candidate_id)
        if manifest is None:
            raise OfflineWaveD5CandidatePanelV1Error(
                "candidate coverage names an unknown searched candidate"
            )
        program = searched_wave_program_from_dict_v1(manifest["program"])
        if (
            program.parent_program_id != parent_program.program_id
            or not set(parent_program.source_refs).issubset(program.source_refs)
            or _D5_SEARCH_SOURCE not in program.source_refs
        ):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} does not preserve parent provenance"
            )
        profile_name = raw.get("lateness_profile")
        profile = _PROFILE_BY_NAME.get(profile_name)
        if raw.get("lateness_by_lane_ms") != profile:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} has an invalid lateness profile"
            )
        if raw.get("gap_behavior") != program.gap_behavior.value:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} gap coverage differs from its program"
            )
        if raw.get("tail_signature") != _tail_signature(program):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} tail coverage differs from its program"
            )
        retimed_ids = raw.get("retimed_step_ids")
        if retimed_ids != expected_missed:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} lacks missed-step coverage"
            )
        step_by_id = {step.step_id: step for step in program.steps}
        if any(
            step_id not in step_by_id
            or step_by_id[step_id].max_lateness_ms
            != profile[step_by_id[step_id].lane]
            for step_id in retimed_ids
        ):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} did not apply its declared windows"
            )
        shift_ms = raw.get("timing_shift_ms")
        if shift_ms not in _SHIFT_MS:
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} has an unsupported timing shift"
            )
        parent_index_by_id = {
            step.step_id: index for index, step in enumerate(parent_program.steps)
        }
        if any(
            step_by_id[step_id].at_or_after_ms
            != _shifted_time(
                parent_program,
                parent_index_by_id[step_id],
                shift_ms,
            )
            for step_id in retimed_ids
        ):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} timing shift differs from its program"
            )
        if raw.get("applied_edit_ids") != list(program.applied_edit_ids):
            raise OfflineWaveD5CandidatePanelV1Error(
                f"candidate {candidate_id!r} edit coverage differs from its program"
            )
        rebuilt_rows.append(deepcopy(dict(raw)))

    guide_count = receipt.get("guide_action_count")
    supported_guide_count = receipt.get("supported_guide_action_count")
    ignored_guide_count = receipt.get("ignored_guide_action_count")
    ignored_guide_actions = receipt.get("ignored_guide_actions")
    if (
        isinstance(guide_count, bool)
        or not isinstance(guide_count, int)
        or guide_count <= 0
        or isinstance(supported_guide_count, bool)
        or not isinstance(supported_guide_count, int)
        or supported_guide_count <= 0
        or isinstance(ignored_guide_count, bool)
        or not isinstance(ignored_guide_count, int)
        or ignored_guide_count < 0
        or not isinstance(ignored_guide_actions, list)
        or len(ignored_guide_actions) != ignored_guide_count
        or guide_count != supported_guide_count + ignored_guide_count
    ):
        raise OfflineWaveD5CandidatePanelV1Error(
            "coverage receipt lacks guide/missed-step provenance"
        )
    ignored_fields = {
        "guide_index",
        "state_time_ms",
        "action",
        "lane",
        "reason",
    }
    for ignored in ignored_guide_actions:
        if (
            not isinstance(ignored, Mapping)
            or set(ignored) != ignored_fields
            or isinstance(ignored.get("guide_index"), bool)
            or not isinstance(ignored.get("guide_index"), int)
            or ignored["guide_index"] < 0
            or isinstance(ignored.get("state_time_ms"), bool)
            or not isinstance(ignored.get("state_time_ms"), int)
            or ignored["state_time_ms"] < 0
            or ignored.get("lane") not in _LANES
            or ignored.get("reason")
            != "NOT_IN_FROZEN_PARENT_ACTION_VOCABULARY"
        ):
            raise OfflineWaveD5CandidatePanelV1Error(
                "ignored guide-action receipt is invalid"
            )
        try:
            ignored_action = ActionRef.from_wire(ignored.get("action"))
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise OfflineWaveD5CandidatePanelV1Error(
                "ignored guide-action identity is invalid"
            ) from error
        if (ignored_action, ignored["lane"]) in {
            (step.action_ref, step.lane)
            for step in parent_program.steps
            if step.kind is SearchedWaveStepKindV1.ACTION
        }:
            raise OfflineWaveD5CandidatePanelV1Error(
                "ignored guide action is supported by the frozen parent"
            )
    rebuilt = _coverage_receipt(
        budget=budget,
        supported_guide_action_count=supported_guide_count,
        ignored_guide_actions=ignored_guide_actions,
        common_missed_step_ids=common_missed,
        rows=rebuilt_rows,
        horizon_ms=horizon_ms,
    )
    if dict(receipt) != rebuilt or rebuilt["coverage_satisfied"] is not True:
        raise OfflineWaveD5CandidatePanelV1Error(
            "coverage receipt is inconsistent or incomplete"
        )
    return deepcopy(dict(value))


def build_d5_candidate_panel_v1(
    parent: SearchedWaveProgramV1,
    *,
    offline_guide_actions: Sequence[Mapping[str, Any]],
    horizon_ms: int,
    searched_candidate_budget: int = 254,
) -> JSONMap:
    """Build parent + tail-only + a stratified finite searched candidate panel."""

    if not isinstance(parent, SearchedWaveProgramV1):
        raise TypeError("parent must be SearchedWaveProgramV1")
    horizon = _positive_int(horizon_ms, "horizon_ms")
    if max(step.at_or_after_ms for step in parent.steps) > horizon:
        raise OfflineWaveD5CandidatePanelV1Error(
            "horizon_ms precedes an explicit step in the frozen parent"
        )
    budget = _positive_int(
        searched_candidate_budget, "searched_candidate_budget"
    )
    if budget < len(_PROFILES):
        raise OfflineWaveD5CandidatePanelV1Error(
            "searched_candidate_budget must be at least 4 for stratified coverage"
        )
    guide_by_lane, supported_guide_count, ignored_guide_actions = _guide_order_by_lane(
        parent, offline_guide_actions
    )
    tail_variants = _tail_variants(parent, guide_by_lane)
    if len(tail_variants) < min(4, budget):
        raise OfflineWaveD5CandidatePanelV1Error(
            "frozen tail vocabulary cannot provide the required stratified tail orders"
        )
    missed_step_ids = _spread_missed_steps(parent, limit=6)
    specs = _candidate_specs(len(tail_variants))
    if budget > len(specs):
        raise OfflineWaveD5CandidatePanelV1Error(
            f"finite D5 donor space supplies {len(specs)} searched candidates; "
            f"requested {budget}"
        )

    parent_manifest = _manifest(PARENT_CANDIDATE_ID, RETENTION, parent)
    tail_manifest = _manifest(
        TAIL_ONLY_CANDIDATE_ID,
        TAIL_ONLY,
        _tail_only_program(parent, horizon),
    )
    manifests = [parent_manifest, tail_manifest]
    coverage_rows: list[JSONMap] = []
    seen_behaviors = {parent_manifest["behavior_key"], tail_manifest["behavior_key"]}
    spec_index = 0
    while len(coverage_rows) < budget and spec_index < len(specs):
        profile_index, gap_index, tail_index, shift_index = specs[spec_index]
        spec_index += 1
        profile_name, profile = _PROFILES[profile_index]
        gap = (
            SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
            if gap_index == 0
            else SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP
        )
        candidate_id = f"d5-searched-{len(coverage_rows) + 1:03d}"
        program = _search_program(
            parent,
            candidate_id=candidate_id,
            profile=profile,
            gap_behavior=gap,
            tail_variant=tail_variants[tail_index],
            shift_ms=_SHIFT_MS[shift_index],
            missed_step_ids=missed_step_ids,
        )
        behavior_key = program.behavior_key()
        if behavior_key in seen_behaviors:
            continue
        seen_behaviors.add(behavior_key)
        manifests.append(_manifest(candidate_id, SEARCHED, program))
        coverage_rows.append(
            {
                "candidate_id": candidate_id,
                "lateness_profile": profile_name,
                "lateness_by_lane_ms": dict(profile),
                "gap_behavior": gap.value,
                "timing_shift_ms": _SHIFT_MS[shift_index],
                "retimed_step_ids": list(missed_step_ids),
                "applied_edit_ids": list(program.applied_edit_ids),
                "tail_signature": _tail_signature(program),
            }
        )
    if len(coverage_rows) != budget:
        raise OfflineWaveD5CandidatePanelV1Error(
            f"finite D5 donor space supplies {len(coverage_rows)} unique behaviors; "
            f"requested {budget}"
        )

    panel = {
        "schema": SCHEMA,
        "horizon_ms": horizon,
        "searched_candidate_budget": budget,
        "parent_behavior_key": parent.behavior_key(),
        "manifests": manifests,
        "coverage_receipt": _coverage_receipt(
            budget=budget,
            supported_guide_action_count=supported_guide_count,
            ignored_guide_actions=ignored_guide_actions,
            common_missed_step_ids=missed_step_ids,
            rows=coverage_rows,
            horizon_ms=horizon,
        ),
        "contract": dict(_CONTRACT),
    }
    return validate_d5_candidate_panel_v1(panel)


__all__ = (
    "CANDIDATE_SCHEMA",
    "COVERAGE_SCHEMA",
    "OfflineWaveD5CandidatePanelV1Error",
    "PARENT_CANDIDATE_ID",
    "RETENTION",
    "SCHEMA",
    "SEARCHED",
    "TAIL_ONLY",
    "TAIL_ONLY_CANDIDATE_ID",
    "build_d5_candidate_panel_v1",
    "validate_d5_candidate_manifests_v1",
    "validate_d5_candidate_panel_v1",
)
