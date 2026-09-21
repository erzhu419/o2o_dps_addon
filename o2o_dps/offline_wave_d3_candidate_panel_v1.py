"""Bounded, seed-invariant D3 candidates for the d900 offline wave.

The parent is the searched-program projection of the source policy.  Plugin
donors are fixed mechanism edits; offline donors are built only from accepted
guide-action rows; combined donors overlay the former on the latter.  The
returned value is already a valid ``offline_wave_d3_train_eval/v1`` candidate
manifest sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Any, Iterator, Mapping, Sequence

from .offline_wave_d3_train_eval_v1 import (
    CANDIDATE_SCHEMA,
    COMBINED,
    OFFLINE_ONLY,
    PARENT_RETENTION,
    PLUGIN_ONLY,
    validate_d3_candidate_manifests_v1,
)
from .offline_wave_policy_v1 import (
    LANE_GCD,
    LANE_OFF_GCD,
    LANE_QUEUE,
    OfflineWaveFeedbackPolicyV1,
)
from .offline_wave_searched_program_v1 import (
    SearchedWaveEditKindV1,
    SearchedWaveEditV1,
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveProgramV1Error,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
    materialize_searched_wave_program_v1,
    searched_wave_program_from_offline_policy_v1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError


JSONMap = dict[str, Any]

D900_ACTION_SEQUENCE = (
    "warrior.battle_stance",
    "warrior.charge",
    "warrior.berserker_stance",
    "warrior.bloodthirst",
    "item.kiss_of_the_spider",
    "warrior.cleave",
    "warrior.whirlwind",
    "warrior.execute",
    "warrior.heroic_strike",
    "warrior.bloodthirst",
    "warrior.heroic_strike",
    "warrior.heroic_strike",
    "warrior.execute",
)

_OBSOLETE_OPENER_KEYS = frozenset(D900_ACTION_SEQUENCE[:3])
_BLOODRAGE_KEY = "warrior.bloodrage"
_BLOODRAGE_REF = ActionRef(spell_id=2687)
_PLUGIN_SOURCE = "D3_PLUGIN_MECHANISM_DONOR_V1"
_OFFLINE_SOURCE = "D3_ACCEPTED_OFFLINE_GUIDE_DONOR_V1"
_COMBINED_SOURCE = "D3_COMBINED_PLUGIN_AND_OFFLINE_DONORS_V1"


class OfflineWaveD3CandidatePanelV1Error(ValueError):
    """The d900 policy or finite donor space cannot form the requested panel."""


@dataclass(frozen=True)
class _PluginSpecV1:
    first_bloodthirst_ms: int
    secondary: str
    tail_shift: int
    gap_behavior: SearchedWaveGapBehaviorV1


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OfflineWaveD3CandidatePanelV1Error(
            f"{label} must be a positive integer"
        )
    return value


def _d900_parent(policy: OfflineWaveFeedbackPolicyV1) -> SearchedWaveProgramV1:
    if not isinstance(policy, OfflineWaveFeedbackPolicyV1):
        raise TypeError("policy must be OfflineWaveFeedbackPolicyV1")
    action_keys = tuple(action.action_key for action in policy.actions)
    if action_keys != D900_ACTION_SEQUENCE:
        raise OfflineWaveD3CandidatePanelV1Error(
            "policy action sequence differs from the frozen d900 shape"
        )
    parent = searched_wave_program_from_offline_policy_v1(
        policy,
        program_id="d3-parent-retention",
        gap_behavior=SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
        action_max_lateness_ms=0,
    )
    deadlines = tuple(
        (*[action.at_or_after_ms for action in policy.actions[1:]], policy.observed_duration_ms)
    )
    parent = replace(
        parent,
        steps=tuple(
            replace(
                step,
                max_lateness_ms=min(
                    2_000,
                    max(0, deadline - step.at_or_after_ms),
                ),
            )
            for step, deadline in zip(parent.steps, deadlines)
        ),
    )
    if len(parent.tail_gcd_priority) < 2:
        raise OfflineWaveD3CandidatePanelV1Error(
            "d900 plugin donors require a reorderable GCD tail"
        )
    return parent


def _first_step(program: SearchedWaveProgramV1, action_key: str) -> SearchedWaveStepV1:
    try:
        return next(step for step in program.steps if step.action_key == action_key)
    except StopIteration as error:  # pragma: no cover - protected by d900 shape
        raise OfflineWaveD3CandidatePanelV1Error(
            f"d900 parent lacks {action_key}"
        ) from error


def _edit(
    edit_id: str,
    kind: SearchedWaveEditKindV1,
    **kwargs: object,
) -> SearchedWaveEditV1:
    return SearchedWaveEditV1(
        edit_id=edit_id,
        kind=kind,
        proposal_source=_PLUGIN_SOURCE,
        **kwargs,
    )


def _tail_rotations(program: SearchedWaveProgramV1) -> tuple[tuple[str, ...], ...]:
    tail = program.tail_gcd_priority
    rotations = tuple(tail[index:] + tail[:index] for index in range(1, len(tail)))
    return tuple(dict.fromkeys(rotations))


def _plugin_specs(parent: SearchedWaveProgramV1) -> tuple[_PluginSpecV1, ...]:
    tails = _tail_rotations(parent)
    secondaries = ("bloodrage-5500", "bloodrage-6000", "move-windows")
    gaps = (
        SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
    )
    coverage = (
        (0, secondaries[0], 0, gaps[0]),
        (500, secondaries[1], 1 % len(tails), gaps[1]),
        (1000, secondaries[2], 2 % len(tails), gaps[0]),
        (0, secondaries[2], 1 % len(tails), gaps[1]),
        (500, secondaries[0], 2 % len(tails), gaps[0]),
        (1000, secondaries[1], 0, gaps[1]),
        (0, secondaries[1], 2 % len(tails), gaps[0]),
        (500, secondaries[2], 0, gaps[1]),
    )
    all_rows = product((0, 500, 1000), secondaries, range(len(tails)), gaps)
    ordered = (*coverage, *all_rows)
    unique = tuple(dict.fromkeys(ordered))
    return tuple(_PluginSpecV1(*row) for row in unique)


def _bloodrage_step(at_ms: int) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=f"plugin-bloodrage-{at_ms}",
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=at_ms,
        max_lateness_ms=500,
        proposal_source=_PLUGIN_SOURCE,
        action_key=_BLOODRAGE_KEY,
        action_ref=_BLOODRAGE_REF,
        lane=LANE_OFF_GCD,
        target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.SELF),
    )


def _plugin_program(
    parent: SearchedWaveProgramV1,
    spec: _PluginSpecV1,
    ordinal: int,
) -> SearchedWaveProgramV1:
    battle, charge, berserker = parent.steps[:3]
    bloodthirst = _first_step(parent, "warrior.bloodthirst")
    whirlwind = _first_step(parent, "warrior.whirlwind")
    execute = _first_step(parent, "warrior.execute")
    edits: list[SearchedWaveEditV1] = [
        _edit(f"delete-{step.action_key}", SearchedWaveEditKindV1.DELETE,
              target_step_id=step.step_id)
        for step in (battle, charge, berserker)
    ]
    edits.append(
        _edit(
            f"retime-first-bloodthirst-{spec.first_bloodthirst_ms}",
            SearchedWaveEditKindV1.RETIME,
            target_step_id=bloodthirst.step_id,
            at_or_after_ms=spec.first_bloodthirst_ms,
            max_lateness_ms=1_000,
        )
    )
    if spec.secondary == "bloodrage-5500":
        edits.extend(
            (
                _edit(
                    "insert-bloodrage-5500",
                    SearchedWaveEditKindV1.INSERT_BEFORE,
                    target_step_id=whirlwind.step_id,
                    step=_bloodrage_step(5_500),
                ),
                _edit(
                    "move-widen-execute-7500",
                    SearchedWaveEditKindV1.RETIME,
                    target_step_id=execute.step_id,
                    at_or_after_ms=7_500,
                    max_lateness_ms=3_000,
                ),
            )
        )
    elif spec.secondary == "bloodrage-6000":
        edits.extend(
            (
                _edit(
                    "move-widen-whirlwind-5250",
                    SearchedWaveEditKindV1.RETIME,
                    target_step_id=whirlwind.step_id,
                    at_or_after_ms=5_250,
                    max_lateness_ms=2_500,
                ),
                _edit(
                    "insert-bloodrage-6000",
                    SearchedWaveEditKindV1.INSERT_BEFORE,
                    target_step_id=execute.step_id,
                    step=_bloodrage_step(6_000),
                ),
            )
        )
    else:
        edits.extend(
            (
                _edit(
                    "move-widen-whirlwind-5250",
                    SearchedWaveEditKindV1.RETIME,
                    target_step_id=whirlwind.step_id,
                    at_or_after_ms=5_250,
                    max_lateness_ms=2_500,
                ),
                _edit(
                    "move-widen-execute-7500",
                    SearchedWaveEditKindV1.RETIME,
                    target_step_id=execute.step_id,
                    at_or_after_ms=7_500,
                    max_lateness_ms=3_000,
                ),
            )
        )
    tail = _tail_rotations(parent)[spec.tail_shift]
    edits.append(
        _edit(
            f"rotate-gcd-tail-{spec.tail_shift + 1}",
            SearchedWaveEditKindV1.TAIL_REORDER,
            tail_lane=LANE_GCD,
            tail_priority=tail,
        )
    )
    program = materialize_searched_wave_program_v1(
        parent,
        edits,
        program_id=f"plugin-donor-{ordinal:04d}",
        max_edits=8,
        source_refs=(_PLUGIN_SOURCE,),
    )
    return replace(
        program,
        gap_behavior=spec.gap_behavior,
        tail_off_gcd_once=tuple(
            key
            for key in program.tail_off_gcd_once
            if key not in _OBSOLETE_OPENER_KEYS
        ),
    )


def _plugin_donors(parent: SearchedWaveProgramV1) -> tuple[SearchedWaveProgramV1, ...]:
    programs: list[SearchedWaveProgramV1] = []
    for ordinal, spec in enumerate(_plugin_specs(parent)):
        try:
            programs.append(_plugin_program(parent, spec, ordinal))
        except SearchedWaveProgramV1Error:
            # A d900-shaped fixture may use compressed timestamps.  Such a
            # donor is incompatible, not a reason to weaken program ordering.
            continue
    return tuple(programs)


def _guide_rows(
    parent: SearchedWaveProgramV1,
    values: Sequence[Mapping[str, Any]],
) -> tuple[tuple[SearchedWaveStepV1, ...], tuple[str, ...]]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise TypeError("offline_guide_actions must be a sequence")
    if not values:
        raise OfflineWaveD3CandidatePanelV1Error(
            "offline_guide_actions has no accepted guide-action donor"
        )

    key_by_identity = {
        (step.action_ref, step.lane): step.action_key
        for step in parent.steps
        if step.kind is SearchedWaveStepKindV1.ACTION
    }
    key_by_identity[(_BLOODRAGE_REF, LANE_OFF_GCD)] = _BLOODRAGE_KEY
    targets: dict[tuple[ActionRef, str], list[SearchedWaveTargetV1]] = {}
    for step in parent.steps:
        if step.kind is SearchedWaveStepKindV1.ACTION:
            targets.setdefault((step.action_ref, step.lane), []).append(step.target)
    used: dict[tuple[ActionRef, str], int] = {}
    steps: list[SearchedWaveStepV1] = []
    keys: list[str] = []
    last_time = -1
    kind_by_lane = {
        LANE_OFF_GCD: "OPTIONAL_OFF_GCD_EXECUTED",
        LANE_QUEUE: "QUEUE_SET",
        LANE_GCD: "TERMINAL_GCD",
    }
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}] must be a mapping"
            )
        raw_action = value.get("action")
        if not isinstance(raw_action, Mapping):
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}].action must be an action mapping"
            )
        try:
            action = ActionRef.from_wire(raw_action)
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}].action is invalid"
            ) from error
        lane = value.get("lane")
        if lane not in {LANE_GCD, LANE_OFF_GCD, LANE_QUEUE}:
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}].lane is unsupported"
            )
        identity = (action, lane)
        action_key = key_by_identity.get(identity)
        if action_key is None:
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}] is outside the d900 action vocabulary"
            )
        kind = value.get("kind")
        if kind is not None and kind != kind_by_lane[lane]:
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}] kind and lane disagree"
            )
        at_ms = value.get("state_time_ms")
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0:
            raise OfflineWaveD3CandidatePanelV1Error(
                f"offline_guide_actions[{index}].state_time_ms must be nonnegative"
            )
        if at_ms < last_time:
            raise OfflineWaveD3CandidatePanelV1Error(
                "offline_guide_actions must preserve accepted execution order"
            )
        last_time = at_ms
        occurrence = used.get(identity, 0)
        available_targets = targets.get(identity, ())
        policy_target_index = value.get("policy_target_index")
        if policy_target_index is not None:
            if (
                isinstance(policy_target_index, bool)
                or not isinstance(policy_target_index, int)
                or policy_target_index < 0
            ):
                raise OfflineWaveD3CandidatePanelV1Error(
                    f"offline_guide_actions[{index}].policy_target_index "
                    "must be nonnegative or null"
                )
            target = SearchedWaveTargetV1(
                SearchedWaveTargetKindV1.INDEX, policy_target_index
            )
        elif available_targets:
            target = available_targets[min(occurrence, len(available_targets) - 1)]
        elif action_key == _BLOODRAGE_KEY:
            target = SearchedWaveTargetV1(SearchedWaveTargetKindV1.SELF)
        else:  # pragma: no cover - vocabulary currently covers this path
            target = SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT)
        used[identity] = occurrence + 1
        steps.append(
            SearchedWaveStepV1(
                step_id=f"offline-guide-step-{index:04d}",
                kind=SearchedWaveStepKindV1.ACTION,
                at_or_after_ms=at_ms,
                max_lateness_ms=500,
                proposal_source=_OFFLINE_SOURCE,
                action_key=action_key,
                action_ref=action,
                lane=lane,
                target=target,
            )
        )
        keys.append(action_key)
    return tuple(steps), tuple(keys)


def _guided_priority(
    parent: tuple[str, ...], guide_keys: Sequence[str], lane_by_key: Mapping[str, str], lane: str
) -> tuple[str, ...]:
    guided = tuple(
        dict.fromkeys(key for key in guide_keys if lane_by_key.get(key) == lane)
    )
    return guided + tuple(key for key in parent if key not in guided)


def _offline_donors(
    parent: SearchedWaveProgramV1,
    offline_guide_actions: Sequence[Mapping[str, Any]],
) -> tuple[SearchedWaveProgramV1, ...]:
    guide_steps, guide_keys = _guide_rows(parent, offline_guide_actions)
    lane_by_key = {
        step.action_key: step.lane
        for step in (*parent.steps, *guide_steps)
        if step.kind is SearchedWaveStepKindV1.ACTION
    }
    base_tails = (
        _guided_priority(parent.tail_gcd_priority, guide_keys, lane_by_key, LANE_GCD),
        _guided_priority(parent.tail_queue_priority, guide_keys, lane_by_key, LANE_QUEUE),
        _guided_priority(parent.tail_off_gcd_once, guide_keys, lane_by_key, LANE_OFF_GCD),
    )
    gcd = base_tails[0]
    tail_variants = tuple(
        dict.fromkeys(
            (base_tails,)
            + tuple(
                (gcd[index:] + gcd[:index], base_tails[1], base_tails[2])
                for index in range(1, len(gcd))
            )
            + ((tuple(reversed(gcd)), base_tails[1], base_tails[2]),)
        )
    )
    lateness_profiles = (
        (0, 0, 0),
        (250, 250, 250),
        (500, 500, 500),
        (1_000, 1_000, 1_000),
        (1_000, 250, 500),
    )
    gaps = (
        SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
    )
    donors: list[SearchedWaveProgramV1] = []
    ordinal = 0
    for profile, tails, gap in product(lateness_profiles, tail_variants, gaps):
        by_lane = {LANE_GCD: profile[0], LANE_OFF_GCD: profile[1], LANE_QUEUE: profile[2]}
        steps = tuple(replace(step, max_lateness_ms=by_lane[step.lane]) for step in guide_steps)
        donors.append(
            SearchedWaveProgramV1(
                program_id=f"offline-donor-{ordinal:04d}",
                parent_program_id=parent.program_id,
                source_refs=(parent.source_refs[0], _OFFLINE_SOURCE),
                applied_edit_ids=(),
                gap_behavior=gap,
                steps=steps,
                tail_gcd_priority=tails[0],
                tail_queue_priority=tails[1],
                tail_off_gcd_once=tails[2],
            )
        )
        ordinal += 1
    return tuple(donors)


def _combined_donor(
    plugin: SearchedWaveProgramV1,
    offline: SearchedWaveProgramV1,
    ordinal: int,
) -> SearchedWaveProgramV1:
    overlay_keys = {
        "warrior.bloodthirst",
        _BLOODRAGE_KEY,
        "warrior.whirlwind",
        "warrior.execute",
    }
    overlay: dict[str, SearchedWaveStepV1] = {}
    for step in plugin.steps:
        if step.action_key in overlay_keys and step.action_key not in overlay:
            overlay[step.action_key] = step

    steps: list[SearchedWaveStepV1] = []
    overlaid: set[str] = set()
    for index, step in enumerate(offline.steps):
        if step.action_key in _OBSOLETE_OPENER_KEYS:
            continue
        donor = overlay.get(step.action_key)
        if donor is not None and step.action_key not in overlaid:
            step = replace(
                step,
                at_or_after_ms=donor.at_or_after_ms,
                max_lateness_ms=donor.max_lateness_ms,
            )
            overlaid.add(step.action_key)
        steps.append(replace(step, proposal_source=_COMBINED_SOURCE))
    if _BLOODRAGE_KEY in overlay and _BLOODRAGE_KEY not in overlaid:
        steps.append(
            replace(
                overlay[_BLOODRAGE_KEY],
                step_id="combined-bloodrage",
                proposal_source=_COMBINED_SOURCE,
            )
        )
    steps.sort(key=lambda step: step.at_or_after_ms)
    steps = [replace(step, step_id=f"combined-step-{index:04d}") for index, step in enumerate(steps)]
    return SearchedWaveProgramV1(
        program_id=f"combined-donor-{ordinal:04d}",
        parent_program_id=offline.program_id,
        source_refs=tuple(
            dict.fromkeys((*plugin.source_refs, *offline.source_refs, _COMBINED_SOURCE))
        ),
        applied_edit_ids=(),
        gap_behavior=plugin.gap_behavior,
        steps=tuple(steps),
        tail_gcd_priority=plugin.tail_gcd_priority,
        tail_queue_priority=plugin.tail_queue_priority,
        tail_off_gcd_once=plugin.tail_off_gcd_once,
    )


def _combined_donors(
    plugins: Sequence[SearchedWaveProgramV1],
    offline: Sequence[SearchedWaveProgramV1],
) -> Iterator[SearchedWaveProgramV1]:
    ordinal = 0
    for offset in range(max(len(plugins), len(offline))):
        for plugin_index in range(len(plugins)):
            offline_index = (plugin_index + offset) % len(offline)
            yield _combined_donor(plugins[plugin_index], offline[offline_index], ordinal)
            ordinal += 1


def _manifest(candidate_id: str, arm: str, program: SearchedWaveProgramV1) -> JSONMap:
    candidate_program = replace(program, program_id=candidate_id)
    return {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "behavior_key": candidate_program.behavior_key(),
        "proposal_arm": arm,
        "program": candidate_program.to_dict(),
    }


def build_d3_candidate_panel_v1(
    policy: OfflineWaveFeedbackPolicyV1,
    proposal_budget_per_arm: int = 8,
    offline_guide_actions: Sequence[Mapping[str, Any]] = (),
) -> list[JSONMap]:
    """Build one parent plus equal, globally deduplicated D3 search arms.

    ``offline_guide_actions`` uses the rows emitted under ``guide_actions`` by
    :func:`offline_wave_execution_trace_v1.project_offline_wave_execution_trace_v1`:
    each row supplies ``action``, ``lane``, and ``state_time_ms``.  A missing or
    too-small finite donor space is an error rather than an unequal panel.
    """

    budget = _positive_int(proposal_budget_per_arm, "proposal_budget_per_arm")
    parent = _d900_parent(policy)
    plugins = _plugin_donors(parent)
    offline = _offline_donors(parent, offline_guide_actions)
    combined = _combined_donors(plugins, offline)

    manifests = [_manifest("d3-parent-retention", PARENT_RETENTION, parent)]
    seen = {manifests[0]["behavior_key"]}

    def fill(arm: str, donors: Sequence[SearchedWaveProgramV1] | Iterator[SearchedWaveProgramV1]) -> None:
        accepted = 0
        for donor in donors:
            behavior_key = donor.behavior_key()
            if behavior_key in seen:
                continue
            candidate_id = f"d3-{arm.replace('_', '-')}-{accepted + 1:03d}"
            manifests.append(_manifest(candidate_id, arm, donor))
            seen.add(behavior_key)
            accepted += 1
            if accepted == budget:
                return
        raise OfflineWaveD3CandidatePanelV1Error(
            f"{arm} donor space supplies {accepted} globally unique candidates; "
            f"requested {budget}"
        )

    fill(PLUGIN_ONLY, plugins)
    fill(OFFLINE_ONLY, offline)
    fill(COMBINED, combined)
    return validate_d3_candidate_manifests_v1(manifests)


__all__ = (
    "D900_ACTION_SEQUENCE",
    "OfflineWaveD3CandidatePanelV1Error",
    "build_d3_candidate_panel_v1",
)
