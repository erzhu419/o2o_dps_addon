from __future__ import annotations

from o2o_dps.offline_wave_d2_panel_v1 import (
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    NATIVE_READY_INPUT_CONTRACT,
    PI_D,
    PI_STAR_NOT_IMPLEMENTED,
    V8_NOT_APPLICABLE,
    build_d2_full_wave_panel_v1,
    summarize_d2_lane_outcome_v1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


REQUEST = "request-one"
CONFIG = "config-one"
BUILD = "exact-build-segment:raid:player:segment-0042"
TARGET_RULE = "d900:observed-onset-until-death:no-route-focus"


def _outcome(
    *,
    damage: float = 8_000.0,
    elapsed_ms: int = 16_000,
    all_dead: bool = True,
    invalid: str | None = None,
) -> ScheduleReplayOutcomeV1:
    targets = [
        {
            "target_index": index,
            "current_health": 0.0 if all_dead else 10.0,
            "maximum_health": 10_000.0,
            "dead": all_dead,
            "attackable": not all_dead,
            "death_time_ms": elapsed_ms if all_dead else None,
        }
        for index in range(3)
    ]
    receipts = (
        {
            "kind": "TERMINAL_GCD",
            "action": ActionRef(spell_id=23894).to_wire(),
            "state_time_ms": elapsed_ms - 100,
        },
        {
            "kind": "NATIVE_TERMINAL_TELEMETRY_V1",
            "terminal_action_surface": {
                "status": "OBSERVED",
                "actions": [
                    {
                        "action": ActionRef(spell_id=23894).to_wire(),
                        "label": "Bloodthirst",
                        "ready_in_ms": 2_000,
                        "cooldown_duration_ms": 6_000,
                    }
                ],
            },
            "candidate_damage_surface": {"status": "OBSERVED", "receipts": []},
        },
    )
    return ScheduleReplayOutcomeV1(
        seed=77,
        status=(ReplayStatusV1.INVALID if invalid else ReplayStatusV1.COMPLETE),
        state={
            "time_ms": elapsed_ms,
            "power": {"type": "Rage", "current": 42.0, "maximum": 100.0},
            "dynamic_team_background": {
                "simulated_damage_applied": damage,
                "targets": targets,
            },
            "dynamic_target_semantics": {"targets": targets},
        },
        receipts=receipts,
        invalid_reason=invalid,
    )


def _row(lane_id: str, *, damage: float, fallback: int = 0):
    offline = lane_id == PI_D
    return summarize_d2_lane_outcome_v1(
        _outcome(damage=damage),
        lane_id=lane_id,
        source_policy_id=f"policy::{lane_id}",
        simulator_seed=77,
        teammate_seed=88,
        request_sha256=REQUEST,
        dynamic_config_sha256=CONFIG,
        evaluation_build_ref=BUILD,
        target_rule_id=TARGET_RULE,
        offline_domain_fallback_calls=fallback if offline else None,
        offline_accepted_execution=(
            {"accepted_action_sequence": ["spell_id:23894:tag:0"]}
            if offline
            else None
        ),
    )


def _panel(rows):
    return build_d2_full_wave_panel_v1(
        rows,
        request_sha256=REQUEST,
        dynamic_config_sha256=CONFIG,
        evaluation_build_ref=BUILD,
        target_rule_id=TARGET_RULE,
        v8_policy_id="frozen-v8",
        v8_exact_build_id="live_bonereaver",
        v8_wave_contract="continuous_two_wave:[0,1]+[2]",
    )


def test_lane_scores_only_complete_all_dead_replay_and_keeps_terminal_state():
    row = _row(PI_D, damage=9_000.0)

    assert row["score_status"] == "COMPLETED"
    assert row["effective_damage"] == 9_000.0
    assert row["dps"] == 562.5
    assert row["dps_denominator"]["elapsed_ms"] == 16_000
    assert row["terminal_resource"]["current"] == 42.0
    assert row["terminal_cooldowns"]["actions"][0]["ready_in_ms"] == 2_000
    assert row["offline_accepted_action_count"] == 1

    incomplete = summarize_d2_lane_outcome_v1(
        _outcome(all_dead=False),
        lane_id=CAT,
        source_policy_id="cat",
        simulator_seed=77,
        teammate_seed=88,
        request_sha256=REQUEST,
        dynamic_config_sha256=CONFIG,
        evaluation_build_ref=BUILD,
        target_rule_id=TARGET_RULE,
    )
    assert incomplete["score_status"] == "INCOMPLETE_REQUIRED_TARGETS"
    assert incomplete["effective_damage"] is None
    assert incomplete["dps"] is None
    assert incomplete["failed_or_incomplete_lane_imputed_as_zero"] is False


def test_four_lane_panel_scores_one_strict_pair_and_keeps_v8_and_pi_star_na():
    panel = _panel(
        [
            _row(CAT, damage=8_000.0),
            _row(CONTRA_DEPLOYED, damage=7_000.0),
            _row(CONTRA_NEW, damage=7_500.0),
            _row(PI_D, damage=9_000.0),
        ]
    )

    assert panel["model_defined_paired_comparison_valid"] is True
    assert panel["full_requested_d2_complete"] is False
    assert panel["valid_paired_seed_count"] == 1
    assert panel["pairs"][0]["paired_delta_vs_cat"][PI_D][
        "effective_damage"
    ] == 1_000.0
    assert panel["common_contract"]["input_opportunity_contract"] == (
        NATIVE_READY_INPUT_CONTRACT
    )
    assert panel["common_contract"]["strict_physical_press_grid_shared"] is False
    assert panel["non_numeric_lanes"][0]["status"] == V8_NOT_APPLICABLE
    assert panel["non_numeric_lanes"][1]["status"] == PI_STAR_NOT_IMPLEMENTED
    assert panel["per_controller_status"][PI_D] == {
        "paired_seed_denominator": 1,
        "observed_lane_count": 1,
        "missing_lane_count": 0,
        "completed_count": 1,
        "failure_or_incomplete_count": 0,
        "completion_rate": 1.0,
        "failure_or_incomplete_rate": 0.0,
        "replay_status_counts": {"COMPLETE": 1},
        "score_status_counts": {"COMPLETED": 1},
        "failed_or_incomplete_lane_imputed_as_zero": False,
    }


def test_offline_fallback_invalidates_pair_without_zero_imputation():
    panel = _panel(
        [
            _row(CAT, damage=8_000.0),
            _row(CONTRA_DEPLOYED, damage=7_000.0),
            _row(CONTRA_NEW, damage=7_500.0),
            _row(PI_D, damage=9_000.0, fallback=1),
        ]
    )

    assert panel["model_defined_paired_comparison_valid"] is False
    assert panel["valid_paired_seed_count"] == 0
    assert panel["aggregate_on_valid_pairs_only"] == {}
    assert "PI_D:DOMAIN_FALLBACK_USED" in panel["pairs"][0][
        "not_scored_reasons"
    ]
    assert panel["pairs"][0]["failed_or_incomplete_lane_imputed_as_zero"] is False


def test_pre_session_offline_invalid_is_retained_as_failure_not_runner_error():
    invalid = summarize_d2_lane_outcome_v1(
        _outcome(invalid="projector construction failed"),
        lane_id=PI_D,
        source_policy_id="offline",
        simulator_seed=77,
        teammate_seed=88,
        request_sha256=REQUEST,
        dynamic_config_sha256=CONFIG,
        evaluation_build_ref=BUILD,
        target_rule_id=TARGET_RULE,
    )
    panel = _panel(
        [
            _row(CAT, damage=8_000.0),
            _row(CONTRA_DEPLOYED, damage=7_000.0),
            _row(CONTRA_NEW, damage=7_500.0),
            invalid,
        ]
    )

    assert invalid["offline_execution_evidence_status"] == (
        "NOT_OBSERVED_PRE_SESSION_INVALID_REPLAY"
    )
    assert panel["valid_paired_seed_count"] == 0
    assert "PI_D:INVALID_REPLAY" in panel["pairs"][0]["not_scored_reasons"]
    assert "PI_D:EXECUTION_EVIDENCE_UNAVAILABLE" in panel["pairs"][0][
        "not_scored_reasons"
    ]
    status = panel["per_controller_status"][PI_D]
    assert status["completed_count"] == 0
    assert status["failure_or_incomplete_count"] == 1
    assert status["failure_or_incomplete_rate"] == 1.0
