from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps import historical_fury_source_bound_team_kill_clock_control_v1 as control


T1 = "0xF130000101000001"
T2 = "0xF130000102000002"
T3 = "0xF130000103000003"


def _anchor(timestamp: int, index: int, stream_type: str) -> dict[str, object]:
    return {
        "timestamp_ms": timestamp,
        "event_index": index,
        "stream_type": stream_type,
        "frame_message_index": index,
    }


def _membership() -> dict[str, object]:
    return {
        "base_request_diagnostic_slice": True,
        "bound_decision_support": True,
        "materialized_team_trace_union": True,
        "relative_to_materialized_union": "IN_MATERIALIZED_UNION",
    }


def _event(
    target_guid: str, timestamp: int, index: int, damage: int
) -> dict[str, object]:
    return {
        "offset_ms": timestamp - 100,
        "target_guid": target_guid,
        "target_index": 0,
        "damage": damage,
        "order_key": [timestamp, index, 0, index],
        "window_membership": _membership(),
    }


def _target(
    guid: str,
    index: int,
    *,
    first: int,
    death: int,
    budget: int,
    healing: int = 0,
    nonvoting: int = 0,
) -> dict[str, object]:
    return {
        "target_guid": guid,
        "target_index": index,
        "first_observed_activity_anchor": _anchor(first, index + 1, "damage"),
        "last_observed_activity_anchor": _anchor(death, index + 20, "slain"),
        "death": {
            "observed": True,
            "anchor": _anchor(death, index + 20, "slain"),
            "marker_damage_added": 0,
        },
        "historical_outcome": {
            "observed_healing_received": healing,
        },
        "health_evidence": {
            "retrospective_kill_budget_proxy": budget,
        },
        "target_lifecycle_evidence": {
            "event_time_nonvoting_positive_damage": {
                "event_count": nonvoting,
                "amount_excluded_from_runtime_schedule": nonvoting,
            }
        },
    }


def _source_row() -> dict[str, object]:
    return {
        "segment_ref": "sha256:" + "1" * 64,
        "content_address": {"sha256": "2" * 64},
        "source_identity": {
            "instance_id": "instance-1",
            "encounter_id": "encounter-1",
            "wave_id": "wave-1",
            "player_guid": "0x0000000000000001",
        },
        "execution_window_contract": {
            "bound_decision_support": {
                "start_order_key": [100, 0, 3, 0],
                "end_order_key": [200, 100, 3, 100],
            },
            "base_request_diagnostic_slice": {"end_timestamp_ms": 200},
        },
        "targets": [
            _target(T1, 0, first=110, death=131, budget=150),
            _target(T2, 1, first=110, death=151, budget=100),
        ],
        "team_trace": {
            "leave_one_out_exact_player_damage_events": [
                _event(T1, 130, 30, 30),
                _event(T2, 140, 40, 60),
            ],
            "excluded_focal_exact_player_damage_events": [
                _event(T1, 120, 20, 120),
                _event(T2, 150, 50, 30),
            ],
            "unattributed_voting_damage_evidence_not_runtime_schedule": [
                _event(T2, 145, 45, 10),
            ],
        },
    }


class TeamKillClockControlTests(unittest.TestCase):
    def test_factual_calibration_and_candidate_sensitive_retarget(self) -> None:
        row = control.validate_control_row(control.build_control_row(_source_row()))

        self.assertEqual(
            row["status"], "FACTUAL_CONTROL_COMPUTED_NOT_COUNTERFACTUAL_MODEL"
        )
        self.assertEqual(row["target_selection"]["selected_target_count"], 2)
        self.assertEqual(row["source_schedule_accounting"]["observed_unattributed_damage"], 10)

        zero, factual, stronger = row["arms"]
        self.assertFalse(zero["terminal"])
        self.assertEqual(factual["predicted_kill_clock_ms"], 50)
        self.assertEqual(stronger["predicted_kill_clock_ms"], 45)
        self.assertFalse(factual["team_assignment_changed_relative_multiplier_1"])
        self.assertTrue(stronger["team_assignment_changed_relative_multiplier_1"])
        self.assertIn(
            {
                "lane": "EXACT_PLAYER_LOO_TEAM",
                "original_target_guid": T1,
                "applied_target_guid": T2,
                "event_count": 1,
                "scaled_damage": 30,
            },
            stronger["team_dynamic_assignments"],
        )
        self.assertEqual(
            row["factual_multiplier_one_calibration"]
            ["mean_absolute_death_anchor_error_ms"],
            1.0,
        )
        self.assertEqual(
            row["factual_multiplier_one_calibration"]["absolute_kill_clock_error_ms"],
            1,
        )
        self.assertTrue(
            row["candidate_sensitivity"]
            ["terminal_state_or_kill_clock_changes_with_focal_multiplier"]
        )
        self.assertTrue(
            row["candidate_sensitivity"]
            ["team_dynamic_assignment_changes_with_focal_multiplier"]
        )
        self.assertTrue(
            row["candidate_sensitivity"]
            ["stronger_focal_vs_factual_state_or_kill_clock_changed"]
        )
        self.assertFalse(row["control_contract"]["held_out_validation"])
        self.assertFalse(
            row["control_contract"]["learned_counterfactual_team_response_model"]
        )

    def test_selection_rejects_preunion_nonvoting_and_open_ledgers(self) -> None:
        source = _source_row()
        source["targets"].extend(
            [
                _target(T3, 2, first=90, death=132, budget=10),
                _target("target-nonvoting", 3, first=110, death=132, budget=10, nonvoting=1),
                _target("target-open-ledger", 4, first=110, death=132, budget=11),
            ]
        )
        source["team_trace"]["leave_one_out_exact_player_damage_events"].extend(
            [
                _event(T3, 120, 60, 10),
                _event("target-nonvoting", 120, 61, 10),
                _event("target-open-ledger", 120, 62, 10),
            ]
        )

        row = control.build_control_row(source)

        self.assertEqual(row["target_selection"]["selected_target_count"], 2)
        rejected = {
            item["target_guid"]: item["reason_codes"]
            for item in row["target_selection"]["rejected_targets"]
        }
        self.assertIn("INTRODUCED_BEFORE_MATERIALIZED_UNION_ORIGIN", rejected[T3])
        self.assertIn(
            "EVENT_TIME_TARGET_IDENTITY_INCOMPLETE", rejected["target-nonvoting"]
        )
        self.assertIn(
            "COMPLETE_DAMAGE_LEDGER_DOES_NOT_CLOSE_TO_KILL_BUDGET",
            rejected["target-open-ledger"],
        )

    def test_empty_selection_fails_closed(self) -> None:
        source = _source_row()
        source["targets"] = [
            _target(T1, 0, first=90, death=131, budget=150)
        ]
        source["team_trace"]["leave_one_out_exact_player_damage_events"] = [
            _event(T1, 130, 30, 30)
        ]
        source["team_trace"]["excluded_focal_exact_player_damage_events"] = [
            _event(T1, 120, 20, 120)
        ]
        source["team_trace"][
            "unattributed_voting_damage_evidence_not_runtime_schedule"
        ] = []

        row = control.validate_control_row(control.build_control_row(source))

        self.assertEqual(row["status"], "BLOCKED_NO_CLOSED_FULLY_OBSERVED_TARGETS")
        self.assertEqual(row["arms"], [])
        self.assertEqual(row["blockers"][0]["code"], "NO_CLOSED_FULLY_OBSERVED_TARGETS")

    def test_content_address_detects_mutation(self) -> None:
        row = control.build_control_row(_source_row())
        changed = deepcopy(row)
        changed["source_schedule_accounting"]["observed_unattributed_damage"] = 11

        with self.assertRaises(
            control.HistoricalFurySourceBoundTeamKillClockControlV1Error
        ):
            control.validate_control_row(changed)

    def test_manifest_keeps_every_adoption_authorization_closed(self) -> None:
        row = control.build_control_row(_source_row())
        manifest = control.validate_historical_fury_source_bound_team_kill_clock_control_v1(
            control._manifest_for_rows(
                rows=[row],
                metadata={
                    "source_evidence_manifest_sha256": "a" * 64,
                    "source_evidence_partition_logical_sha256": "b" * 64,
                    "source_evidence_request_count": 1,
                },
                partition={
                    "path": "partition.jsonl.gz",
                    "record_count": 1,
                },
            )
        )

        for key in (
            "comparison_authorized",
            "training_authorized",
            "deployment_authorized",
            "superiority_claim_authorized",
            "calibration_pass_claimed",
            "learned_counterfactual_team_response_model",
            "candidate_policy_evaluated",
            "held_out_validation",
        ):
            self.assertFalse(manifest[key])
        self.assertEqual(manifest["summary"]["computed_request_count"], 1)
        self.assertEqual(
            manifest["summary"]["stronger_focal_vs_factual_state_or_kill_clock_changed_request_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
