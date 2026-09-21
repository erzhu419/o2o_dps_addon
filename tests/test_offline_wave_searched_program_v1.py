from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.offline_wave_policy_v1 import (
    EVIDENCE_KNOWN_ONTOLOGY_START,
    LANE_GCD,
    LANE_OFF_GCD,
    LANE_QUEUE,
    TARGET_CURRENT,
    OfflineWaveActionV1,
    OfflineWaveFeedbackPolicyV1,
)
from o2o_dps.offline_wave_searched_program_v1 import (
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
    searched_wave_behavior_key_v1,
    searched_wave_edit_from_dict_v1,
    searched_wave_program_from_dict_v1,
    searched_wave_program_from_offline_policy_v1,
    searched_wave_step_from_dict_v1,
)
from o2o_dps.sim_bridge import ActionRef


BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
EXECUTE = ActionRef(spell_id=20647)
DEATH_WISH = ActionRef(spell_id=12328)


def _target(index: int | None = None) -> SearchedWaveTargetV1:
    if index is None:
        return SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT)
    return SearchedWaveTargetV1(SearchedWaveTargetKindV1.INDEX, index)


def _action_step(
    step_id: str,
    at_ms: int,
    action_key: str,
    action_ref: ActionRef,
    *,
    lane: str = LANE_GCD,
    proposal_source: str = "TEST_GENERATOR",
    max_lateness_ms: int = 250,
    target: SearchedWaveTargetV1 | None = None,
    guard: ObservableCausalGuardV1 | None = None,
) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=step_id,
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=at_ms,
        max_lateness_ms=max_lateness_ms,
        proposal_source=proposal_source,
        action_key=action_key,
        action_ref=action_ref,
        lane=lane,
        target=target or _target(),
        guard=guard,
    )


def _wait_step(step_id: str, at_ms: int, wait_ms: int) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=step_id,
        kind=SearchedWaveStepKindV1.WAIT,
        at_or_after_ms=at_ms,
        max_lateness_ms=0,
        proposal_source="TEST_GENERATOR",
        wait_ms=wait_ms,
    )


def _base(*, program_id: str = "base", proposal_source: str = "BASE") -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id=program_id,
        parent_program_id=None,
        source_refs=("offline-policy-a",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            _action_step(
                "s0", 0, "warrior.bloodthirst", BLOODTHIRST,
                proposal_source=proposal_source,
            ),
            _action_step(
                "s1", 1_500, "warrior.whirlwind", WHIRLWIND,
                proposal_source=proposal_source,
            ),
            _action_step(
                "s2", 3_000, "warrior.execute", EXECUTE,
                proposal_source=proposal_source,
            ),
        ),
        tail_gcd_priority=(
            "warrior.bloodthirst",
            "warrior.whirlwind",
            "warrior.execute",
        ),
        tail_queue_priority=("warrior.heroic_strike", "warrior.cleave"),
        tail_off_gcd_once=("warrior.death_wish",),
    )


def _edit(
    kind: SearchedWaveEditKindV1,
    edit_id: str,
    **kwargs: object,
) -> SearchedWaveEditV1:
    return SearchedWaveEditV1(
        edit_id=edit_id,
        kind=kind,
        proposal_source="SEARCH_MUTATION",
        **kwargs,
    )


def test_program_and_step_wire_roundtrip_is_exact_and_has_no_evidence_claim() -> None:
    guard = ObservableCausalGuardV1(
        rage_gte=30,
        action_ready=BLOODTHIRST,
        false_semantics=SKIP_PLAN,
    )
    program = replace(
        _base(),
        steps=(
            replace(_base().steps[0], guard=guard, max_lateness_ms=500),
            _wait_step("w0", 800, 100),
            *_base().steps[1:],
        ),
    )
    wire = program.to_dict()
    assert searched_wave_program_from_dict_v1(wire) == program
    assert searched_wave_step_from_dict_v1(wire["steps"][0]) == program.steps[0]
    flattened = repr(wire).lower()
    assert "evidence_status" not in flattened
    assert "observed_channels" not in flattened
    assert "source_ordinal" not in flattened
    assert wire["contract"]["searched_proposal_not_source_evidence"] is True
    assert wire["steps"][0]["max_lateness_ms"] == 500


def test_behavior_key_excludes_all_identity_and_proposal_provenance() -> None:
    first = _base(program_id="candidate-a", proposal_source="GUIDE_A")
    second = replace(
        first,
        program_id="candidate-b",
        parent_program_id="parent-b",
        source_refs=("different-source",),
        applied_edit_ids=("different-edit",),
        steps=tuple(
            replace(
                step,
                step_id=f"renamed-{index}",
                proposal_source="GENERATOR_B",
            )
            for index, step in enumerate(first.steps)
        ),
    )
    assert searched_wave_behavior_key_v1(first) == second.behavior_key()
    assert "candidate-a" not in first.behavior_key()
    assert "offline-policy-a" not in first.behavior_key()
    assert "GUIDE_A" not in first.behavior_key()
    assert first.behavior_key() != replace(
        second,
        gap_behavior=SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
    ).behavior_key()
    assert first.behavior_key() != replace(
        second,
        steps=(replace(second.steps[0], max_lateness_ms=500), *second.steps[1:]),
    ).behavior_key()


@pytest.mark.parametrize(
    "field",
    ["seed", "simulator_seed", "seed_overrides", "per-seed-programs"],
)
def test_wire_parser_rejects_per_seed_program_shapes_anywhere(field: str) -> None:
    wire = _base().to_dict()
    wire["steps"][0][field] = {"1": {"kind": "WAIT"}}
    with pytest.raises(SearchedWaveProgramV1Error, match="per-seed"):
        searched_wave_program_from_dict_v1(wire)


def test_insert_and_tail_reorder_materialize_and_roundtrip_edits() -> None:
    inserted = _action_step(
        "inserted",
        1_000,
        "warrior.death_wish",
        DEATH_WISH,
        lane=LANE_OFF_GCD,
        max_lateness_ms=0,
    )
    edits = (
        _edit(
            SearchedWaveEditKindV1.INSERT_BEFORE,
            "insert-death-wish",
            target_step_id="s1",
            step=inserted,
        ),
        _edit(
            SearchedWaveEditKindV1.TAIL_REORDER,
            "reverse-gcd-tail",
            tail_lane=LANE_GCD,
            tail_priority=(
                "warrior.execute",
                "warrior.whirlwind",
                "warrior.bloodthirst",
            ),
        ),
    )
    for edit in edits:
        assert searched_wave_edit_from_dict_v1(edit.to_dict()) == edit
    result = materialize_searched_wave_program_v1(
        _base(), edits, program_id="candidate-insert"
    )
    assert [step.step_id for step in result.steps] == ["s0", "inserted", "s1", "s2"]
    assert result.parent_program_id == "base"
    assert result.applied_edit_ids == ("insert-death-wish", "reverse-gcd-tail")
    assert result.tail_gcd_priority[0] == "warrior.execute"


def test_every_edit_kind_has_an_exact_wire_roundtrip() -> None:
    guard = ObservableCausalGuardV1(
        rage_lte=30,
        false_semantics=SKIP_PLAN,
    )
    edits = (
        _edit(
            SearchedWaveEditKindV1.INSERT_BEFORE,
            "insert",
            target_step_id="s1",
            step=_wait_step("inserted-wait", 1_000, 50),
        ),
        _edit(SearchedWaveEditKindV1.DELETE, "delete", target_step_id="s1"),
        _edit(
            SearchedWaveEditKindV1.REPLACE,
            "replace",
            target_step_id="s1",
            step=_action_step("s1", 1_500, "warrior.execute", EXECUTE),
        ),
        _edit(
            SearchedWaveEditKindV1.REPEAT_AFTER,
            "repeat",
            target_step_id="s1",
            new_step_id="repeated-s1",
        ),
        _edit(
            SearchedWaveEditKindV1.RETIME,
            "retime",
            target_step_id="s1",
            at_or_after_ms=1_250,
            max_lateness_ms=500,
        ),
        _edit(
            SearchedWaveEditKindV1.RETARGET,
            "retarget",
            target_step_id="s1",
            target=_target(2),
        ),
        _edit(
            SearchedWaveEditKindV1.REPLACE_GUARD,
            "replace-guard",
            target_step_id="s1",
            guard=guard,
        ),
        _edit(
            SearchedWaveEditKindV1.REPLACE_GUARD,
            "clear-guard",
            target_step_id="s1",
            guard=None,
        ),
        _edit(
            SearchedWaveEditKindV1.TAIL_REORDER,
            "tail",
            tail_lane=LANE_QUEUE,
            tail_priority=("warrior.cleave", "warrior.heroic_strike"),
        ),
    )
    for edit in edits:
        assert searched_wave_edit_from_dict_v1(edit.to_dict()) == edit


def test_delete_replace_repeat_retime_retarget_and_guard_each_materialize() -> None:
    base = _base()
    deleted = materialize_searched_wave_program_v1(
        base,
        (_edit(SearchedWaveEditKindV1.DELETE, "delete", target_step_id="s1"),),
        program_id="deleted",
    )
    assert [step.step_id for step in deleted.steps] == ["s0", "s2"]

    replacement = replace(
        base.steps[1],
        action_key="warrior.execute",
        action_ref=EXECUTE,
        proposal_source="REPLACEMENT",
    )
    replaced = materialize_searched_wave_program_v1(
        base,
        (
            _edit(
                SearchedWaveEditKindV1.REPLACE,
                "replace",
                target_step_id="s1",
                step=replacement,
            ),
        ),
        program_id="replaced",
    )
    assert replaced.steps[1].action_ref == EXECUTE
    assert replaced.steps[1].step_id == "s1"

    repeated = materialize_searched_wave_program_v1(
        base,
        (
            _edit(
                SearchedWaveEditKindV1.REPEAT_AFTER,
                "repeat",
                target_step_id="s1",
                new_step_id="repeat-s1",
            ),
        ),
        program_id="repeated",
    )
    assert [step.step_id for step in repeated.steps] == ["s0", "s1", "repeat-s1", "s2"]
    assert repeated.steps[2].action_ref == WHIRLWIND

    retimed = materialize_searched_wave_program_v1(
        base,
        (
            _edit(
                SearchedWaveEditKindV1.RETIME,
                "retime",
                target_step_id="s1",
                at_or_after_ms=1_250,
                max_lateness_ms=500,
            ),
        ),
        program_id="retimed",
    )
    assert (retimed.steps[1].at_or_after_ms, retimed.steps[1].max_lateness_ms) == (
        1_250,
        500,
    )

    retargeted = materialize_searched_wave_program_v1(
        base,
        (
            _edit(
                SearchedWaveEditKindV1.RETARGET,
                "retarget",
                target_step_id="s2",
                target=_target(2),
            ),
        ),
        program_id="retargeted",
    )
    assert retargeted.steps[2].target == _target(2)

    guard = ObservableCausalGuardV1(
        target_index=0,
        target_hp_pct_lte=20,
        action_ready=EXECUTE,
        false_semantics=SKIP_PLAN,
    )
    guarded = materialize_searched_wave_program_v1(
        base,
        (
            _edit(
                SearchedWaveEditKindV1.REPLACE_GUARD,
                "guard",
                target_step_id="s2",
                guard=guard,
            ),
        ),
        program_id="guarded",
    )
    assert guarded.steps[2].guard == guard
    cleared = materialize_searched_wave_program_v1(
        guarded,
        (
            _edit(
                SearchedWaveEditKindV1.REPLACE_GUARD,
                "clear",
                target_step_id="s2",
                guard=None,
            ),
        ),
        program_id="cleared",
    )
    assert cleared.steps[2].guard is None


def test_default_two_edit_limit_can_be_explicitly_raised_but_not_past_eight() -> None:
    edits = tuple(
        _edit(
            SearchedWaveEditKindV1.INSERT_BEFORE,
            f"edit-{index}",
            target_step_id=target_id,
            step=_wait_step(f"wait-{index}", at_ms, 10),
        )
        for index, (target_id, at_ms) in enumerate(
            (("s0", 0), ("s1", 1_000), ("s2", 2_000))
        )
    )
    with pytest.raises(SearchedWaveProgramV1Error, match="limit is 2"):
        materialize_searched_wave_program_v1(
            _base(), edits, program_id="too-many-default"
        )
    expanded = materialize_searched_wave_program_v1(
        _base(), edits, program_id="joint-opener", max_edits=3
    )
    assert len(expanded.steps) == 6
    with pytest.raises(SearchedWaveProgramV1Error, match="cannot exceed 8"):
        materialize_searched_wave_program_v1(
            _base(), (), program_id="unsupported-limit", max_edits=9
        )


def test_conflicts_and_nonpermutation_tail_are_rejected() -> None:
    same_target = (
        _edit(SearchedWaveEditKindV1.DELETE, "a", target_step_id="s1"),
        _edit(
            SearchedWaveEditKindV1.RETIME,
            "b",
            target_step_id="s1",
            at_or_after_ms=1_000,
            max_lateness_ms=0,
        ),
    )
    with pytest.raises(SearchedWaveProgramV1Error, match="conflicting edits"):
        materialize_searched_wave_program_v1(
            _base(), same_target, program_id="conflict"
        )

    duplicate_id = _edit(
        SearchedWaveEditKindV1.INSERT_BEFORE,
        "duplicate",
        target_step_id="s1",
        step=_wait_step("s0", 1_000, 10),
    )
    with pytest.raises(SearchedWaveProgramV1Error, match="not new and unique"):
        materialize_searched_wave_program_v1(
            _base(), (duplicate_id,), program_id="duplicate"
        )

    bad_tail = _edit(
        SearchedWaveEditKindV1.TAIL_REORDER,
        "bad-tail",
        tail_lane=LANE_QUEUE,
        tail_priority=("warrior.heroic_strike",),
    )
    with pytest.raises(SearchedWaveProgramV1Error, match="exact permutation"):
        materialize_searched_wave_program_v1(
            _base(), (bad_tail,), program_id="bad-tail"
        )


def test_invalid_replacement_identity_order_and_wait_target_edit_are_rejected() -> None:
    wrong_id = replace(_base().steps[1], step_id="replacement-id")
    with pytest.raises(SearchedWaveProgramV1Error, match="preserve"):
        materialize_searched_wave_program_v1(
            _base(),
            (
                _edit(
                    SearchedWaveEditKindV1.REPLACE,
                    "wrong-id",
                    target_step_id="s1",
                    step=wrong_id,
                ),
            ),
            program_id="wrong-id",
        )

    with pytest.raises(SearchedWaveProgramV1Error, match="ordered"):
        materialize_searched_wave_program_v1(
            _base(),
            (
                _edit(
                    SearchedWaveEditKindV1.RETIME,
                    "bad-order",
                    target_step_id="s1",
                    at_or_after_ms=4_000,
                    max_lateness_ms=0,
                ),
            ),
            program_id="bad-order",
        )

    with_wait = replace(
        _base(),
        steps=(_base().steps[0], _wait_step("wait", 1_000, 100), *_base().steps[1:]),
    )
    with pytest.raises(SearchedWaveProgramV1Error, match="ACTION"):
        materialize_searched_wave_program_v1(
            with_wait,
            (
                _edit(
                    SearchedWaveEditKindV1.RETARGET,
                    "wait-target",
                    target_step_id="wait",
                    target=_target(1),
                ),
            ),
            program_id="wait-target",
        )


def test_offline_conversion_is_a_separate_tail_fill_proposal() -> None:
    offline_action = OfflineWaveActionV1(
        source_ordinal=0,
        at_or_after_ms=700,
        action_key="warrior.bloodthirst",
        action_ref=BLOODTHIRST,
        lane=LANE_GCD,
        target_role=TARGET_CURRENT,
        source_target_ordinal=0,
        source_order_key=(700, 0),
        evidence_status=EVIDENCE_KNOWN_ONTOLOGY_START,
        build_segment_ref="segment-a",
    )
    policy = OfflineWaveFeedbackPolicyV1(
        policy_id="offline-a",
        mode_id="mode-a",
        encounter_name="trash",
        source_instance_id="instance-a",
        source_episode_id="episode-a",
        source_wave_id="wave-a",
        source_player_guid="player-a",
        source_player_name="warrior",
        source_wave_ordinal=0,
        observed_duration_ms=5_000,
        complete_wave_coverage=True,
        observed_target_count=1,
        source_target_guids=("target-a",),
        build_segment_refs=("segment-a",),
        actions=(offline_action,),
        unresolved_start_evidence=(),
        tail_gcd_priority=("warrior.bloodthirst",),
        tail_queue_priority=("warrior.heroic_strike",),
        tail_off_gcd_once=(),
    )
    before = deepcopy(policy.to_dict())
    program = searched_wave_program_from_offline_policy_v1(
        policy,
        program_id="searched-a",
        action_max_lateness_ms=250,
    )
    assert policy.to_dict() == before
    assert program.source_refs == ("offline-a",)
    assert program.parent_program_id is None
    assert program.gap_behavior is SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
    assert program.steps[0].step_id == "step-0000"
    assert program.steps[0].at_or_after_ms == 700
    assert program.steps[0].max_lateness_ms == 250
    assert program.steps[0].proposal_source == "OFFLINE_POLICY_ACTION_GUIDE"
    assert "evidence_status" not in repr(program.to_dict()).lower()
