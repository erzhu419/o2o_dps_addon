from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from o2o_dps.contra_macro_queue_probe_summary import (
    combat_log_excerpt,
    summarize_probe,
)


RUN = "contra-probe-test-1"


def marker(sequence: int, event: str, **details: object) -> dict:
    return {
        "sequence": sequence,
        "event": event,
        "marker": {"runId": RUN, **details},
    }


def typed(sequence: int, event: str, spell_id: int, **details: object) -> dict:
    return {
        "sequence": sequence,
        "event": event,
        "spellID": spell_id,
        "task": {"taskRunId": RUN},
        **details,
    }


class ContraMacroQueueProbeSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            marker(1, "CALIBRATION_CONTRA_PROBE_A_REQUEST"),
            typed(2, "SPELL_CAST_EVENT", 1680, castSucceeded=True, castType=0),
            typed(3, "SPELL_CAST_EVENT", 20569, castSucceeded=True, castType=2),
            marker(4, "CALIBRATION_CONTRA_PROBE_A_CALLS_RETURNED",
                   wwCallAt=10.0, cleaveCallAt=10.02),
            typed(5, "SPELL_GO_SELF", 1680),
            typed(6, "SPELL_DAMAGE_EVENT_SELF", 1680, amount=400),
            typed(7, "SPELL_GO_SELF", 20569),
            typed(8, "SPELL_DAMAGE_EVENT_SELF", 20571, amount=500),
            marker(9, "CALIBRATION_CONTRA_PROBE_A_WINDOW_END"),
            marker(10, "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"),
            typed(11, "SPELL_CAST_EVENT", 20569, castSucceeded=True, castType=2),
            typed(12, "SPELL_QUEUE_EVENT", 20569, queueEventCode=0),
            marker(13, "CALIBRATION_CONTRA_PROBE_B_REENTRY",
                   elapsedFromQueuePress=0.13,
                   elapsedFromClientAccept=0.11,
                   queueCurrentRaw=1, queueCurrent=True),
            typed(14, "SPELL_CAST_EVENT", 1680, castSucceeded=False, castType=0),
            marker(15, "CALIBRATION_CONTRA_PROBE_B_CALLS_RETURNED",
                   sourceCleaveGuard=True, cleaveSkippedCurrent=True),
            typed(16, "SPELL_GO_SELF", 20569),
            typed(17, "SPELL_DAMAGE_EVENT_SELF", 20571, amount=500),
            marker(18, "CALIBRATION_CONTRA_PROBE_COMPLETED"),
        ]

    def test_complete_source_reject_plus_queue_only(self) -> None:
        result = summarize_probe(
            {"runId": RUN, "status": "exported_reload_seen"}, self.rows
        )
        self.assertEqual(result["status"], "typed_chain_observed")
        self.assertAlmostEqual(
            result["a_same_key_ww_then_cleave"]["call_gap_seconds"], 0.02
        )
        self.assertTrue(result["b_standalone_queue_then_reentry"]["ww_reentry_client"]["rejected"])
        self.assertFalse(result["b_standalone_queue_then_reentry"]["cleave_recast_requested"])
        self.assertEqual(
            result["b_standalone_queue_then_reentry"]["queue_outcome"],
            "server_go_after_reentry",
        )
        self.assertEqual(
            result["b_standalone_queue_then_reentry"]["elapsed_from_client_accept_seconds"],
            0.11,
        )

    def test_missing_client_accept_does_not_count_returned_call(self) -> None:
        rows = [row for row in self.rows if row["sequence"] != 11]
        result = summarize_probe({"runId": RUN}, rows)
        self.assertEqual(result["status"], "incomplete_evidence")
        self.assertIn(
            "standalone_queue_client_accept",
            result["b_standalone_queue_then_reentry"]["missing"],
        )

    def test_post_go_cast_receipt_does_not_make_reentry_recast_accepted(self) -> None:
        rows = [
            marker(1, "CALIBRATION_CONTRA_PROBE_A_REQUEST"),
            typed(2, "SPELL_CAST_EVENT", 1680, castSucceeded=True, castType=0),
            typed(3, "SPELL_CAST_EVENT", 20569, castSucceeded=False, castType=2),
            marker(4, "CALIBRATION_CONTRA_PROBE_A_CALLS_RETURNED"),
            typed(5, "SPELL_GO_SELF", 1680),
            typed(6, "SPELL_MISS_SELF", 1680),
            marker(9, "CALIBRATION_CONTRA_PROBE_A_WINDOW_END"),
            marker(10, "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"),
            typed(11, "SPELL_CAST_EVENT", 20569, castSucceeded=True, castType=2),
            marker(13, "CALIBRATION_CONTRA_PROBE_B_REENTRY"),
            typed(14, "SPELL_CAST_EVENT", 1680, castSucceeded=True, castType=0),
            typed(15, "SPELL_CAST_EVENT", 20569, castSucceeded=False, castType=2),
            marker(16, "CALIBRATION_CONTRA_PROBE_B_CALLS_RETURNED", cleaveCallAt=10.0),
            typed(17, "SPELL_GO_SELF", 20569),
            typed(18, "SPELL_CAST_EVENT", 20569, castSucceeded=True, castType=2),
            typed(19, "SPELL_DAMAGE_EVENT_SELF", 20571, amount=500),
            marker(20, "CALIBRATION_CONTRA_PROBE_COMPLETED"),
        ]
        result = summarize_probe({"runId": RUN}, rows)
        self.assertEqual(result["status"], "same_key_queue_rejected")
        self.assertEqual(
            result["a_same_key_ww_then_cleave"]["outcome"],
            "same_key_queue_client_rejected",
        )
        branch = result["b_standalone_queue_then_reentry"]
        self.assertFalse(branch["cleave_reentry_client_before_go"]["accepted"])
        self.assertTrue(branch["cleave_reentry_client_before_go"]["rejected"])
        self.assertTrue(branch["post_first_go_cast_event_unattributed"]["accepted"])
        self.assertEqual(
            branch["queue_outcome"],
            "prior_queue_survived_reentry_and_server_go",
        )
        self.assertEqual(branch["missing"], [])
        self.assertEqual(result["a_same_key_ww_then_cleave"]["missing"], [])
        self.assertIn(
            "cleave_client_on_swing_accept",
            result["a_same_key_ww_then_cleave"]["positive_chain_unmet"],
        )

    def test_only_latest_queue_attempt_is_used(self) -> None:
        rows = self.rows[:9] + [
            marker(10, "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"),
            typed(11, "SPELL_CAST_EVENT", 20569, castSucceeded=False, castType=2),
            marker(12, "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"),
            typed(13, "SPELL_CAST_EVENT", 20569, castSucceeded=True, castType=2),
            marker(14, "CALIBRATION_CONTRA_PROBE_B_REENTRY",
                   elapsedFromClientAccept=0.2),
            typed(15, "SPELL_CAST_EVENT", 1680, castSucceeded=False, castType=0),
            marker(16, "CALIBRATION_CONTRA_PROBE_B_CALLS_RETURNED"),
            typed(17, "SPELL_GO_SELF", 20569),
            marker(18, "CALIBRATION_CONTRA_PROBE_COMPLETED"),
        ]
        result = summarize_probe({"runId": RUN}, rows)
        self.assertEqual(result["b_standalone_queue_then_reentry"]["queue_request_sequence"], 12)
        self.assertTrue(result["b_standalone_queue_then_reentry"]["standalone_queue_client"]["accepted"])

    def test_exhausted_queue_attempts_are_not_completed(self) -> None:
        rows = self.rows[:9] + [
            marker(10, "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"),
            marker(11, "CALIBRATION_CONTRA_PROBE_INCOMPLETE",
                   incompleteReason="b_reentry_not_observed_after_six_attempts"),
        ]
        result = summarize_probe({"runId": RUN}, rows)
        self.assertEqual(result["status"], "incomplete_evidence")
        self.assertEqual(result["terminal_marker_sequence"], 11)
        self.assertEqual(
            result["incomplete_reason"],
            "b_reentry_not_observed_after_six_attempts",
        )

    def test_no_run(self) -> None:
        self.assertEqual(summarize_probe({}, self.rows)["status"], "not_recorded")

    def test_client_probe_is_loaded_after_timer_and_only_presses_cast(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        toc = (workspace / "BrainOfCat.toc").read_text(encoding="utf-8")
        source = (workspace / "addon" / "ContraMacroQueueProbe.lua").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            toc.index(r"addon\TimerCalibrationDebug.lua"),
            toc.index(r"addon\ContraMacroQueueProbe.lua"),
        )
        self.assertIn('tasks.OneKey = function()', source)
        self.assertIn('return probe.Press()', source)
        self.assertIn('"/bocprobe"', source)
        poll = source.split("function probe.Poll()", 1)[1].split(
            "function probe.Status()", 1
        )[0]
        self.assertNotIn("CastSpellByName", poll)

    def test_combat_log_excerpt_keeps_only_small_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "WoWCombatLog.txt"
            log.write_text(
                "9/13 19:00:00.000  旋风斩旧记录\n"
                "9/13 20:00:01.000  旋风斩新记录\n"
                "9/13 20:00:02.000  顺劈斩新记录\n",
                encoding="utf-8",
            )
            excerpt = combat_log_excerpt(
                log, {"a": "2026-09-13 20:00:00", "b_queue": None}
            )
        self.assertEqual(len(excerpt), 2)
        self.assertTrue(all("新记录" in line for line in excerpt))


if __name__ == "__main__":
    unittest.main()
