from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from o2o_dps.development_wave_actor_schedule_v1 import (
    build_actor_schedule_wave_case_v1,
    export_actor_schedule_v1,
    reduce_actor_terminal_rows_v1,
    run_actor_schedule_wave_panel_v1,
    source_actor_damage_events_v1,
)
from o2o_dps.development_wave_case_v1 import PROJECT_ROOT
from o2o_dps.development_wave_team_retarget_v1 import V14_BRIDGE


RAW = PROJECT_ROOT / (
    "offline_data/chronicle_raw/20260828T212558001220Z/"
    "all-activity-cc330b4f-b688-489f-b03f-b6841ddb029e.csv"
)


@unittest.skipUnless(RAW.exists(), "fixed wave's local Chronicle CSV is unavailable")
class DevelopmentWaveActorScheduleV1Test(unittest.TestCase):
    def test_source_actor_spell_timing_and_lethal_rows_close_budgets(self) -> None:
        events = source_actor_damage_events_v1()
        case, scenario, actual = build_actor_schedule_wave_case_v1(20260913)
        self.assertTrue(events == actual)
        self.assertEqual(len(events), 495)
        self.assertEqual(len({row.actor_guid for row in events}), 35)
        self.assertEqual(
            case.case_spec["team_background"]["source_actor_kind_counts"],
            {"CREATURE": 1, "OBJECT": 2, "PLAYER": 32},
        )
        self.assertEqual(
            (events[0].source_event_index, events[0].time_ms, events[0].spell_id),
            (610, 0, 10333),
        )
        self.assertEqual(
            [(row.source_event_index, row.damage) for row in events if row.source_type == "DEAD"],
            [(2855, 2684.0), (3304, 12.0)],
        )
        self.assertFalse(any(row.actor_guid == "0x00000000004D4CBE" for row in events))
        self.assertEqual(case.case_spec["team_background"]["source_damage_by_target"], [110481.0, 117460.0])
        self.assertFalse(case.case_spec["team_background"]["future_schedule_policy_visible"])
        self.assertFalse(case.case_spec["team_background"]["post_source_death_rate_extrapolated"])
        self.assertNotIn(
            "POST_SOURCE_DEATH_TEAM_RATE_EXTRAPOLATED",
            scenario["scenario_model"]["limitation_codes"],
        )

    def test_compact_derived_schedule_roundtrip_and_truncation_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "actor-derived.json"
            summary = export_actor_schedule_v1(path)
            self.assertEqual(summary["event_count"], 495)
            self.assertEqual(source_actor_damage_events_v1(), source_actor_damage_events_v1(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["events"].pop()
            bad = Path(directory) / "actor-truncated.json"
            bad.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not close the fixed wave"):
                source_actor_damage_events_v1(bad)

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_native_death_change_retargets_same_source_actor_events(self) -> None:
        panel = run_actor_schedule_wave_panel_v1(20260913)
        self.assertTrue(panel["team_retarget_comparison_eligible"])
        self.assertFalse(panel["comparison_ready"])
        cat, candidate = panel["rows"]
        self.assertEqual([cat["status"], candidate["status"]], ["COMPLETED", "COMPLETED"])
        self.assertEqual(cat["team_response_evidence"]["events_retargeted_after_endogenous_death"], 0)
        evidence = candidate["team_response_evidence"]
        self.assertEqual(evidence["native_runtime_receipt_status"], "COMPLETE_BOUND")
        self.assertEqual(evidence["source_actor_count"], 35)
        self.assertEqual(evidence["source_actor_kind_counts"], {"CREATURE": 1, "OBJECT": 2, "PLAYER": 32})
        self.assertEqual(evidence["events_retargeted_after_endogenous_death"], 3)
        self.assertEqual(evidence["retargeted_by_actor"], {"0x0000000000686599": 3})
        self.assertTrue(all(
            row["actor_guid"] == "0x0000000000686599"
            and row["actor_kind"] == "PLAYER"
            and row["historical_target_index"] != row["actual_target_index"]
            for row in evidence["retargeted_source_events"]
        ))
        self.assertLess(candidate["ttk_ms"], cat["ttk_ms"])

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_no_post_source_death_extrapolation_can_censor(self) -> None:
        panel = run_actor_schedule_wave_panel_v1(20260914)
        self.assertFalse(panel["team_retarget_comparison_eligible"])
        self.assertTrue(all(row["status"] == "CENSORED_WATCHDOG" for row in panel["rows"]))
        self.assertTrue(all(row["own_effective_damage"] is None for row in panel["rows"]))


class ActorTerminalSummaryV1Test(unittest.TestCase):
    def test_paired_mean_se_lower_and_censor_are_from_terminal_rows(self) -> None:
        rows = [
            {"seed": 1, "lane": "CAT", "status": "COMPLETED", "own_effective_damage": 100.0, "censor_reason": None},
            {"seed": 1, "lane": "CANDIDATE", "status": "COMPLETED", "own_effective_damage": 110.0, "censor_reason": None},
            {"seed": 2, "lane": "CAT", "status": "COMPLETED", "own_effective_damage": 100.0, "censor_reason": None},
            {"seed": 2, "lane": "CANDIDATE", "status": "COMPLETED", "own_effective_damage": 96.0, "censor_reason": None},
            {"seed": 3, "lane": "CAT", "status": "CENSORED_WATCHDOG", "own_effective_damage": None, "censor_reason": "SCENARIO_HORIZON_REACHED"},
            {"seed": 3, "lane": "CANDIDATE", "status": "COMPLETED", "own_effective_damage": 120.0, "censor_reason": None},
        ]
        summary = reduce_actor_terminal_rows_v1(rows)
        self.assertEqual(summary["paired_complete_count"], 2)
        self.assertEqual(summary["paired_candidate_minus_cat_mean"], 3.0)
        self.assertAlmostEqual(summary["paired_candidate_minus_cat_se"], 7.0)
        self.assertAlmostEqual(summary["paired_candidate_minus_cat_normal_approx_95_lower"], -10.72)
        self.assertEqual(summary["paired_candidate_minus_cat_wins"], 1)
        self.assertEqual(summary["paired_candidate_minus_cat_losses"], 1)
        self.assertEqual(summary["lane_status_counts"]["CAT"]["CENSORED_WATCHDOG"], 1)
        self.assertEqual(summary["censored"][0]["seed"], 3)


if __name__ == "__main__":
    unittest.main()
