from __future__ import annotations

import pytest

from o2o_dps.offline_wave_execution_trace_v1 import (
    OfflineWaveExecutionTraceV1Error,
    project_offline_wave_execution_trace_v1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


BT = ActionRef(spell_id=23894).to_wire()
WW = ActionRef(spell_id=1680).to_wire()
CLEAVE = ActionRef(spell_id=20569, tag=1).to_wire()
DEATH_WISH = ActionRef(spell_id=12328).to_wire()


def _proposal(index: int, policy_id: str, *, time_ms: int) -> dict[str, object]:
    return {
        "decision_index": index,
        "kind": "IMPORTED_REACTIVE_INCUMBENT_SELECTED",
        "binding_id": policy_id,
        "source_policy_id": policy_id,
        "state_time_ms": time_ms,
        # These proposal fields are intentionally not projected as guide acts.
        "gcd_action": WW,
        "queue_op": "SET",
        "wait_ms": 999,
    }


def _outcome(*receipts: dict[str, object]) -> ScheduleReplayOutcomeV1:
    return ScheduleReplayOutcomeV1(
        seed=17,
        status=ReplayStatusV1.COMPLETE,
        state={"time_ms": 2_000, "damage_done": 1.0},
        receipts=receipts,
    )


def test_projects_only_accepted_actions_target_and_wait() -> None:
    outcome = _outcome(
        _proposal(0, "cat.fury.profile1", time_ms=100),
        {
            "decision_index": 0,
            "operation_index": 0,
            "kind": "SET_TARGET",
            "policy_target_index": 2,
            "simulator_target_index": 7,
            "changed": True,
            "state_time_ms": 100,
        },
        {
            "decision_index": 0,
            "operation_index": 1,
            "prefix_index": 0,
            "kind": "OPTIONAL_OFF_GCD_SKIPPED",
            "action": ActionRef(item_id=13442).to_wire(),
            "state_time_ms": 100,
        },
        {
            "decision_index": 0,
            "operation_index": 2,
            "prefix_index": 1,
            "kind": "OPTIONAL_OFF_GCD_EXECUTED",
            "action": DEATH_WISH,
            "attempt_id": None,
            "state_time_ms": 100,
        },
        {
            "decision_index": 0,
            "operation_index": 3,
            "kind": "QUEUE_SET",
            "action": CLEAVE,
            "attempt_id": None,
            "state_time_ms": 100,
            "queue_state_after_acceptance": {"status": "QUEUED"},
        },
        {
            "decision_index": 0,
            "kind": "TERMINAL_GCD",
            "action": BT,
            "attempt_id": "program-decision-0:terminal-gcd",
            "state_time_ms": 100,
        },
        _proposal(1, "cat.fury.profile1", time_ms=1_600),
        {
            "decision_index": 1,
            "kind": "QUEUE_KEEP",
            "state_time_ms": 1_600,
        },
        {
            "decision_index": 1,
            "kind": "TERMINAL_WAIT",
            "wait_ms": 400,
            "state_time_ms": 2_000,
        },
    )

    trace = project_offline_wave_execution_trace_v1(outcome)

    assert trace["source_policy_ids"] == ["cat.fury.profile1"]
    assert trace["accepted_action_count"] == 3
    assert [block["decision_index"] for block in trace["blocks"]] == [0, 1]
    first, second = trace["blocks"]
    assert first["accepted_target"]["policy_target_index"] == 2
    assert first["accepted_target"]["simulator_target_index"] == 7
    assert [row["action"] for row in first["guide_actions"]] == [
        DEATH_WISH,
        CLEAVE,
        BT,
    ]
    assert [row["lane"] for row in first["guide_actions"]] == [
        "off_gcd",
        "queue",
        "gcd",
    ]
    assert first["accepted_timing"] is None
    assert second["guide_actions"] == []
    assert second["accepted_timing"] == {
        "kind": "TERMINAL_WAIT",
        "wait_ms": 400,
        "state_time_ms": 2_000,
        "receipt_index": 8,
    }


@pytest.mark.parametrize(
    "policy_id",
    (
        "cat.fury.profile1",
        "contra.deployed.fury.raid_b",
        "contra260817.fury.source_candidate",
        "offline.wave.pi_d",
    ),
)
def test_preserves_arbitrary_imported_controller_identity(policy_id: str) -> None:
    trace = project_offline_wave_execution_trace_v1(
        _outcome(
            _proposal(0, policy_id, time_ms=0),
            {
                "decision_index": 0,
                "kind": "TERMINAL_GCD",
                "action": BT,
                "state_time_ms": 0,
            },
        )
    )

    assert trace["blocks"][0]["source_policy_id"] == policy_id
    assert trace["blocks"][0]["binding_id"] == policy_id


def test_proposal_only_decision_never_becomes_a_guide_action() -> None:
    trace = project_offline_wave_execution_trace_v1(
        _outcome(
            _proposal(0, "offline.wave.pi_d", time_ms=0),
            _proposal(1, "offline.wave.pi_d", time_ms=100),
            {
                "decision_index": 1,
                "kind": "TERMINAL_GCD",
                "action": BT,
                "state_time_ms": 100,
            },
        )
    )

    assert [row["decision_index"] for row in trace["blocks"]] == [1]
    assert trace["blocks"][0]["guide_actions"][0]["action"] == BT
    assert WW not in [
        row["action"]
        for block in trace["blocks"]
        for row in block["guide_actions"]
    ]


def test_rejects_accepted_action_without_source_proposal_identity() -> None:
    with pytest.raises(
        OfflineWaveExecutionTraceV1Error,
        match="lack imported proposal metadata",
    ):
        project_offline_wave_execution_trace_v1(
            _outcome(
                {
                    "decision_index": 0,
                    "kind": "TERMINAL_GCD",
                    "action": BT,
                    "state_time_ms": 0,
                }
            )
        )


def test_invalid_replay_keeps_prefix_actions_that_were_actually_accepted() -> None:
    outcome = ScheduleReplayOutcomeV1(
        seed=18,
        status=ReplayStatusV1.INVALID,
        state={"time_ms": 100, "damage_done": 0.0},
        receipts=(
            _proposal(0, "contra260817.fury.source_candidate", time_ms=100),
            {
                "decision_index": 0,
                "operation_index": 0,
                "prefix_index": 0,
                "kind": "OPTIONAL_OFF_GCD_EXECUTED",
                "action": DEATH_WISH,
                "state_time_ms": 100,
            },
        ),
        invalid_reason="terminal action rejected",
    )

    trace = project_offline_wave_execution_trace_v1(outcome)

    assert trace["replay_status"] == "INVALID"
    assert trace["invalid_reason"] == "terminal action rejected"
    assert trace["blocks"][0]["guide_actions"][0]["action"] == DEATH_WISH
