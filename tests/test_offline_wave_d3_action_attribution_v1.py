from __future__ import annotations

import pytest

from o2o_dps.offline_wave_d3_action_attribution_v1 import (
    CONTROLLER_WAIT,
    EXPLICIT_ACTION,
    EXPLICIT_WAIT,
    OTHER,
    TAIL_FILL,
    OfflineWaveD3ActionAttributionV1Error,
    attribute_d3_searched_replay_v1,
)
from o2o_dps.offline_wave_execution_trace_v1 import SCHEMA as TRACE_SCHEMA
from o2o_dps.offline_wave_policy_v1 import LANE_GCD, LANE_OFF_GCD
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
)
from o2o_dps.sim_bridge import ActionRef


BT = ActionRef(spell_id=23894).to_wire()
BLOODRAGE = ActionRef(spell_id=2687).to_wire()


def _action_step(step_id: str, at_ms: int) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=step_id,
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=at_ms,
        max_lateness_ms=100,
        proposal_source="TEST",
        action_key="warrior.bloodthirst",
        action_ref=ActionRef.from_wire(BT),
        lane=LANE_GCD,
        target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT),
    )


def _wait_step(step_id: str, at_ms: int) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=step_id,
        kind=SearchedWaveStepKindV1.WAIT,
        at_or_after_ms=at_ms,
        max_lateness_ms=100,
        proposal_source="TEST",
        wait_ms=25,
    )


def _program() -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id="attribution-test",
        parent_program_id=None,
        source_refs=("test",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            _action_step("accepted-action", 0),
            _wait_step("accepted-wait", 10),
            _action_step("missed-action", 20),
            _action_step("skipped-action", 30),
            _action_step("selected-unaccepted-action", 40),
            _action_step("never-selected-action", 50),
        ),
        tail_gcd_priority=("warrior.bloodthirst",),
        tail_queue_priority=(),
        tail_off_gcd_once=(),
    )


def _outcome(
    decision_index: int,
    proposal_kind: str,
    *,
    step_id: str | None = None,
    kind: str = "SEARCHED_PROPOSAL_EXECUTION_CONFIRMED",
    actions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "proposal_index": decision_index,
        "execution_decision_index": decision_index,
        "proposal_kind": proposal_kind,
        "step_id": step_id,
        "accepted_actions": list(actions or []),
    }


def _accepted_action() -> dict[str, object]:
    return {"kind": "TERMINAL_GCD", "lane": "gcd", "action": BT}


def _block(
    decision_index: int,
    *,
    actions: list[dict[str, object]] | None = None,
    wait: bool = False,
) -> dict[str, object]:
    return {
        "decision_index": decision_index,
        "guide_actions": list(actions or []),
        "accepted_timing": (
            {"kind": "TERMINAL_WAIT", "wait_ms": 25} if wait else None
        ),
    }


def test_attributes_identical_action_identity_by_joined_control_source() -> None:
    explicit = _accepted_action()
    tail = _accepted_action()
    audit = [
        {"kind": "SEARCHED_STEP_SELECTED", "step_id": "accepted-action"},
        _outcome(
            0,
            "SEARCHED_PROGRAM_STEP",
            step_id="accepted-action",
            actions=[explicit],
        ),
        {"kind": "SEARCHED_TAIL_SELECTED", "proposal_index": 1},
        _outcome(1, "SEARCHED_TAIL_FILL", actions=[tail]),
        {
            "kind": "SEARCHED_EXPLICIT_WAIT_SELECTED",
            "step_id": "accepted-wait",
        },
        _outcome(2, "EXPLICIT_WAIT_STEP", step_id="accepted-wait"),
        {"kind": "SEARCHED_WAIT", "proposal_index": 3},
        _outcome(3, "CONTROLLER_WAIT"),
        {
            "kind": "SEARCHED_STEP_WINDOW_MISSED",
            "step_id": "missed-action",
        },
        {
            "kind": "SEARCHED_STEP_GUARD_SKIPPED",
            "step_id": "skipped-action",
        },
        {
            "kind": "SEARCHED_STEP_SELECTED",
            "step_id": "selected-unaccepted-action",
        },
        _outcome(
            4,
            "SEARCHED_PROGRAM_STEP",
            step_id="selected-unaccepted-action",
            kind="SEARCHED_PROPOSAL_ACTION_NOT_EXECUTED",
        ),
    ]
    trace = {
        "schema": TRACE_SCHEMA,
        "accepted_action_count": 2,
        "blocks": [
            _block(0, actions=[explicit]),
            _block(1, actions=[tail]),
            _block(2, wait=True),
            _block(3, wait=True),
            _block(4, wait=True),
        ],
    }

    result = attribute_d3_searched_replay_v1(_program(), audit, trace)

    assert result["accepted_action_count_by_source"] == {
        EXPLICIT_ACTION: 1,
        TAIL_FILL: 1,
        EXPLICIT_WAIT: 0,
        CONTROLLER_WAIT: 0,
        OTHER: 0,
    }
    assert result["accepted_wait_count_by_source"] == {
        EXPLICIT_ACTION: 0,
        TAIL_FILL: 0,
        EXPLICIT_WAIT: 1,
        CONTROLLER_WAIT: 1,
        OTHER: 1,
    }
    assert result["accepted_explicit_action_step_ids"] == ["accepted-action"]
    assert result["accepted_explicit_wait_step_ids"] == ["accepted-wait"]
    assert result["missed_explicit_step_ids"] == ["missed-action"]
    assert result["skipped_explicit_step_ids"] == ["skipped-action"]
    assert result["selected_but_unaccepted_explicit_step_ids"] == [
        "selected-unaccepted-action"
    ]
    assert result["never_selected_explicit_step_ids"] == [
        "never-selected-action"
    ]
    assert result["planned_action_acceptance_rate"] == pytest.approx(0.2)
    assert result["selected_action_proposal_acceptance_rate"] == pytest.approx(0.5)
    assert result["contract"]["action_names_used_for_attribution"] is False


def test_explicit_off_gcd_action_may_legitimately_end_with_a_wait() -> None:
    step = SearchedWaveStepV1(
        step_id="bloodrage",
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=0,
        max_lateness_ms=500,
        proposal_source="TEST",
        action_key="warrior.bloodrage",
        action_ref=ActionRef.from_wire(BLOODRAGE),
        lane=LANE_OFF_GCD,
        target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.SELF),
    )
    program = SearchedWaveProgramV1(
        program_id="off-gcd-attribution-test",
        parent_program_id=None,
        source_refs=("test",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
        steps=(step,),
        tail_gcd_priority=("warrior.bloodthirst",),
        tail_queue_priority=(),
        tail_off_gcd_once=("warrior.bloodrage",),
    )
    action = {
        "kind": "OPTIONAL_OFF_GCD_EXECUTED",
        "lane": "off_gcd",
        "action": BLOODRAGE,
    }
    audit = [
        {"kind": "SEARCHED_STEP_SELECTED", "step_id": "bloodrage"},
        _outcome(
            0,
            "SEARCHED_PROGRAM_STEP",
            step_id="bloodrage",
            actions=[action],
        ),
    ]
    trace = {
        "schema": TRACE_SCHEMA,
        "accepted_action_count": 1,
        "blocks": [_block(0, actions=[action], wait=True)],
    }

    result = attribute_d3_searched_replay_v1(program, audit, trace)

    assert result["accepted_action_count_by_source"][EXPLICIT_ACTION] == 1
    assert result["accepted_wait_count_by_source"][EXPLICIT_ACTION] == 1
    assert result["accepted_explicit_action_step_ids"] == ["bloodrage"]


def test_rejects_accepted_operation_without_runtime_decision_join() -> None:
    trace = {
        "schema": TRACE_SCHEMA,
        "accepted_action_count": 1,
        "blocks": [_block(7, actions=[_accepted_action()])],
    }

    with pytest.raises(
        OfflineWaveD3ActionAttributionV1Error,
        match="lacks joined runtime outcome",
    ):
        attribute_d3_searched_replay_v1(_program(), [], trace)


def test_rejects_action_evidence_disagreement_after_valid_join() -> None:
    audit = [
        {"kind": "SEARCHED_STEP_SELECTED", "step_id": "accepted-action"},
        _outcome(
            0,
            "SEARCHED_PROGRAM_STEP",
            step_id="accepted-action",
            actions=[_accepted_action()],
        ),
    ]
    different = {
        "kind": "TERMINAL_GCD",
        "lane": "gcd",
        "action": ActionRef(spell_id=1680).to_wire(),
    }
    trace = {
        "schema": TRACE_SCHEMA,
        "accepted_action_count": 1,
        "blocks": [_block(0, actions=[different])],
    }

    with pytest.raises(
        OfflineWaveD3ActionAttributionV1Error,
        match="accepted actions differ",
    ):
        attribute_d3_searched_replay_v1(_program(), audit, trace)


def test_rejects_runtime_proposal_bridge_index_disagreement() -> None:
    audit = [
        {
            **_outcome(0, "SEARCHED_TAIL_FILL", actions=[_accepted_action()]),
            "proposal_index": 1,
        }
    ]
    trace = {
        "schema": TRACE_SCHEMA,
        "accepted_action_count": 1,
        "blocks": [_block(0, actions=[_accepted_action()])],
    }

    with pytest.raises(
        OfflineWaveD3ActionAttributionV1Error,
        match="differs from runtime proposal ordinal",
    ):
        attribute_d3_searched_replay_v1(_program(), audit, trace)
