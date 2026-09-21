from __future__ import annotations

from types import SimpleNamespace

import pytest

from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceWaveV1,
)
from o2o_dps.upper_kara_cat_residual_paired_eval_v8 import _terminal
from o2o_dps.upper_kara_v8_attribution_panel_v1 import (
    A0_EXACT_CAT,
    ARM_IDS,
    SCHEMA as PANEL_SCHEMA,
    summarize_v8_attribution_rows_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


def _fixture() -> (
    tuple[
        SimpleNamespace, tuple[CatResidualSequenceWaveV1, ...], ScheduleReplayOutcomeV1
    ]
):
    waves = (
        CatResidualSequenceWaveV1("wave-1", (0, 1)),
        CatResidualSequenceWaveV1("wave-2", (2,)),
    )
    events = (
        SimpleNamespace(target_index=0, time_ms=3_000, attackable=True),
        SimpleNamespace(target_index=1, time_ms=3_000, attackable=True),
        SimpleNamespace(target_index=2, time_ms=9_000, attackable=True),
    )
    case = SimpleNamespace(
        case_spec={"required_target_indices": [0, 1, 2]},
        dynamic_load=SimpleNamespace(
            config=SimpleNamespace(attackability_events=events)
        ),
    )
    state = {
        "time_ms": 12_000,
        "dynamic_team_background": {
            "simulated_damage_applied": 123.0,
            "targets": [
                {"target_index": 0, "dead": True, "death_time_ms": 7_000},
                {"target_index": 1, "dead": True, "death_time_ms": 6_000},
                {"target_index": 2, "dead": True, "death_time_ms": 12_000},
            ],
        },
        "power": {"type": "rage", "current": 33, "maximum": 100},
        "auras": [
            {
                "label": "Battle Shout",
                "action": {"spell_id": 25_289},
                "remaining_ms": 112_500,
                "stacks": 0,
            }
        ],
        "swing_queue": {"kind": "NONE", "status": "NONE"},
        "mh_swing_remaining_ms": 500,
        "oh_swing_remaining_ms": 700,
    }
    damage_rows = [
        {
            "time_ms": 3_500,
            "target_index": 0,
            "applied_damage": 10.0,
            "status": "APPLIED",
            "action": {"other_id": 7, "tag": 1},
            "outcome": "HIT",
            "execution_id": 1,
            "attempt_id": None,
        },
        {
            "time_ms": 4_000,
            "target_index": 0,
            "applied_damage": 5.0,
            "status": "APPLIED",
            "action": {"other_id": 7, "tag": 2},
            "outcome": "HIT",
            "execution_id": 2,
            "attempt_id": None,
        },
        {
            "time_ms": 5_000,
            "target_index": 0,
            "applied_damage": 20.0,
            "status": "APPLIED",
            "action": {"spell_id": 20_569},
            "outcome": "CRIT",
            "execution_id": 3,
            "attempt_id": None,
        },
        {
            "time_ms": 5_000,
            "target_index": 1,
            "applied_damage": 12.0,
            "status": "APPLIED",
            "action": {"spell_id": 20_569},
            "outcome": "HIT",
            "execution_id": 3,
            "attempt_id": None,
        },
    ]
    receipts = (
        {
            "kind": "TERMINAL_GCD",
            "action": {"spell_id": 12_328},
            "state_time_ms": 3_000,
        },
        {
            "kind": "QUEUE_SET",
            "action": {"spell_id": 20_569, "tag": 1},
            "state_time_ms": 3_000,
            "queue_state_after_acceptance": {
                "kind": "CLEAVE",
                "status": "QUEUED",
            },
        },
        {
            "kind": "TERMINAL_GCD",
            "action": {"spell_id": 25_289},
            "state_time_ms": 4_500,
        },
        {
            "kind": "NATIVE_TERMINAL_TELEMETRY_V1",
            "state_time_ms": 12_000,
            "terminal_action_surface": {
                "status": "OBSERVED",
                "actions": [
                    {
                        "action": {"spell_id": 12_328},
                        "label": "Death Wish",
                        "ready_in_ms": 170_000,
                        "cooldown_duration_ms": 180_000,
                    }
                ],
            },
            "candidate_damage_surface": {
                "status": "OBSERVED",
                "receipts": damage_rows,
            },
        },
    )
    outcome = ScheduleReplayOutcomeV1(
        seed=1,
        status=ReplayStatusV1.COMPLETE,
        state=state,
        receipts=receipts,
    )
    return case, waves, outcome


def test_compact_telemetry_attributes_timings_outcomes_and_assumed_uptime() -> None:
    case, waves, outcome = _fixture()

    terminal = _terminal(case, outcome, waves, {"GUARD_FALSE": 1})
    telemetry = terminal["compact_telemetry"]

    assert telemetry["timing"]["full_route"]["elapsed_ms"] == 9_000
    assert [row["elapsed_ms"] for row in telemetry["timing"]["waves"]] == [
        4_000,
        3_000,
    ]
    assert (
        telemetry["death_wish"]["useful_uptime"]["attackable_wave_useful_uptime_ms"]
        == 7_000
    )
    assert (
        telemetry["battle_shout"]["uptime"]["attackable_wave_useful_uptime_ms"] == 5_500
    )
    assert telemetry["cleave"]["queue_acceptance"]["accepted_count"] == 1
    assert telemetry["cleave"]["queue_acceptance"]["confirmed_queued_count"] == 1
    assert telemetry["cleave"]["resolution"]["execution_count"] == 1
    assert telemetry["cleave"]["resolution"]["outcome_counts"] == {
        "CRIT": 1,
        "HIT": 1,
    }
    assert telemetry["swing_timing"]["main_hand"]["event_count"] == 2
    assert telemetry["swing_timing"]["off_hand"]["event_count"] == 1
    assert telemetry["terminal"]["rage"]["current"] == 33
    assert telemetry["terminal"]["cooldowns"]["actions"][0]["ready_in_ms"] == 170_000
    assert telemetry["execution"]["fallback_reasons"]["counts"] == {"GUARD_FALSE": 1}
    assert telemetry["cleave"]["accepted_to_resolution_join"] == {
        "status": "NOT_OBSERVED",
        "reason": "QUEUE_ACCEPTANCE_AND_SWING_RESOLUTION_LACK_SHARED_ATTEMPT_ID",
    }

    rows = [
        {
            "seed": 1,
            "first_wave_arrival_ms": 0,
            "arms": {
                arm_id: {
                    **terminal,
                    "intervention_executed": arm_id != A0_EXACT_CAT,
                }
                for arm_id in ARM_IDS
            },
        }
    ]
    summary = summarize_v8_attribution_rows_v1(rows)["compact_telemetry"]
    assert summary["arms"][A0_EXACT_CAT]["full_route_elapsed_ms"]["mean"] == 9_000
    assert summary["arms"][A0_EXACT_CAT]["cleave"]["resolved_outcome_counts"] == {
        "CRIT": 1,
        "HIT": 1,
    }
    assert summary["arms"][A0_EXACT_CAT]["terminal_rage"]["mean"] == 33
    aggregate = summarize_v8_attribution_rows_v1(rows)
    assert aggregate["arrival_randomization"]["observed_first_wave_arrival_ms"] == [0]


def test_missing_terminal_bridge_receipt_is_typed_not_observed() -> None:
    case, waves, outcome = _fixture()
    without_capture = ScheduleReplayOutcomeV1(
        seed=outcome.seed,
        status=outcome.status,
        state=outcome.state,
        receipts=tuple(
            row
            for row in outcome.receipts
            if row.get("kind") != "NATIVE_TERMINAL_TELEMETRY_V1"
        ),
    )

    telemetry = _terminal(case, without_capture, waves, {})["compact_telemetry"]

    assert telemetry["status"] == "PARTIALLY_OBSERVED_WITH_LABELED_ASSUMPTIONS"
    assert (
        "NATIVE_TERMINAL_TELEMETRY_RECEIPT_ABSENT" in telemetry["not_observed_reasons"]
    )


def test_formal_seed_artifact_cannot_reduce_without_versioned_telemetry() -> None:
    case, waves, outcome = _fixture()
    terminal = _terminal(case, outcome, waves, {})
    arms = {
        arm_id: {
            **terminal,
            "intervention_executed": arm_id != A0_EXACT_CAT,
        }
        for arm_id in ARM_IDS
    }
    arms[ARM_IDS[-1]] = {**arms[ARM_IDS[-1]]}
    arms[ARM_IDS[-1]].pop("compact_telemetry")

    with pytest.raises(ValueError, match="lacks frozen compact telemetry"):
        summarize_v8_attribution_rows_v1(
            [
                {
                    "schema": f"{PANEL_SCHEMA}/seed",
                    "compact_telemetry_schema": ("upper_kara_v8_compact_telemetry/v1"),
                    "seed": 1,
                    "first_wave_arrival_ms": 0,
                    "arms": arms,
                }
            ]
        )
