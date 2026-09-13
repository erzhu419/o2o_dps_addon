from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from o2o_dps.development_wave_team_retarget_v1 import (
    SourceFittedTeamRetargetBridgeV1,
    V14ProjectedDynamicV3Bridge,
    V14_BRIDGE,
    build_retarget_wave_case_v1,
    run_retarget_wave_panel_v1,
)


class DevelopmentWaveTeamRetargetV1Test(unittest.TestCase):
    @staticmethod
    def _fake_state(time_ms: int, *, ready: bool, wake: bool = False,
                    press_clock: bool = True) -> dict:
        state = {
            "time_ms": time_ms, "finished": False, "needs_input": True,
            "wake_ready": {"wake_id": "team-0"} if wake else None,
            "dynamic_target_semantics": {"targets": [
                {"target_index": 0, "dead": False, "attackable": True},
            ]},
        }
        if press_clock:
            state["press_clock"] = {"ready": ready}
        return state

    @staticmethod
    def _fake_bridge(after_emit: dict, after_advance: dict | None = None):
        class FakeBridge:
            def __init__(self):
                self.current = after_emit
                self.calls = []

            def state(self):
                return self.current

            def advance(self):
                self.calls.append("advance")
                if after_advance is None:
                    raise AssertionError("unexpected advance")
                self.current = after_advance
                return self.current

            def _request(self, command, *, responsive):
                self.calls.append(command)
                self.current = after_emit
                return {"responsive_team_event": {
                    "target_index": responsive["target_index"],
                    "status": "APPLIED", "time_ms": self.current["time_ms"],
                    "applied_damage": responsive["requested_damage"],
                    "killed": False, "actor_guid": responsive["actor_guid"],
                }}

        return FakeBridge()

    def test_non_press_wake_is_serviced_before_next_press(self) -> None:
        woke = self._fake_state(150, ready=False, wake=True)
        after_emit = self._fake_state(150, ready=False)
        at_press = self._fake_state(200, ready=True)
        bridge = self._fake_bridge(after_emit, at_press)
        event = SimpleNamespace(time_ms=150, schedule_index=0, event_id="e0",
                                target_index=0, damage=7)
        wrapper = SourceFittedTeamRetargetBridgeV1(bridge, (event,), "source")

        result = wrapper._resume_to_policy(woke)

        self.assertEqual(result["time_ms"], 200)
        self.assertTrue(result["press_clock"]["ready"])
        self.assertEqual(bridge.calls, ["emit_dynamic_team_event", "advance"])
        self.assertEqual(wrapper._receipts[0]["time_ms"], 150)

    def test_same_time_wake_is_serviced_before_press_returns(self) -> None:
        woke = self._fake_state(200, ready=True, wake=True)
        after_emit = self._fake_state(200, ready=True)
        bridge = self._fake_bridge(after_emit)
        event = SimpleNamespace(time_ms=200, schedule_index=0, event_id="e0",
                                target_index=0, damage=7)
        wrapper = SourceFittedTeamRetargetBridgeV1(bridge, (event,), "source")

        result = wrapper._resume_to_policy(woke)

        self.assertEqual(result["time_ms"], 200)
        self.assertTrue(result["press_clock"]["ready"])
        self.assertEqual(bridge.calls, ["emit_dynamic_team_event"])

    def test_no_clock_still_returns_at_needs_input(self) -> None:
        state = self._fake_state(150, ready=False, press_clock=False)
        bridge = self._fake_bridge(state)
        event = SimpleNamespace(time_ms=200, schedule_index=0, event_id="e0",
                                target_index=0, damage=7)
        wrapper = SourceFittedTeamRetargetBridgeV1(bridge, (event,), "source")

        self.assertEqual(wrapper._resume_to_policy(state)["time_ms"], 150)
        self.assertEqual(bridge.calls, [])

    def test_source_case_has_no_fixed_duplicate_damage(self) -> None:
        case, scenario, events = build_retarget_wave_case_v1(20260913)
        self.assertEqual(len(case.case_spec["required_target_ids"]), 2)
        self.assertTrue(events)
        self.assertEqual(case.dynamic_load.config.background_damage_events, ())
        self.assertEqual(scenario["dynamic_load_config"]["background_damage_events"], [])
        self.assertFalse(case.case_spec["team_background"]["learned_teammate_model_used"])

    def test_target_selection_uses_alive_attackable_prefix(self) -> None:
        state = {"dynamic_target_semantics": {"targets": [
            {"target_index": 0, "dead": True, "attackable": False},
            {"target_index": 1, "dead": False, "attackable": True},
        ]}}
        self.assertEqual(SourceFittedTeamRetargetBridgeV1._recipient(state, 0), 1)

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_native_candidate_replay_is_deterministic_and_retargets(self) -> None:
        first = run_retarget_wave_panel_v1(20260913)
        second = run_retarget_wave_panel_v1(20260913)
        candidate_first = next(row for row in first["rows"] if row["role"] == "CANDIDATE")
        candidate_second = next(row for row in second["rows"] if row["role"] == "CANDIDATE")
        cat = next(row for row in first["rows"] if row["policy_id"] == "cat.fury.profile1")
        cat_second = next(row for row in second["rows"] if row["policy_id"] == "cat.fury.profile1")
        self.assertEqual(cat["status"], "COMPLETED")
        self.assertEqual(cat["status"], cat_second["status"])
        self.assertEqual(cat["own_reported_damage"], cat_second["own_reported_damage"])
        self.assertEqual(cat["terminal_reason"], "ALL_TARGETS_DEAD")
        self.assertEqual(cat["own_effective_damage"], cat["own_reported_damage"])
        self.assertFalse(first["comparison_ready"])
        self.assertTrue(first["team_retarget_comparison_eligible"])
        self.assertEqual(first["team_retarget_comparison_gate"], "MATCHED_NATIVE_DEVELOPMENT_ONLY")
        self.assertEqual(candidate_first["status"], "COMPLETED")
        self.assertEqual(
            candidate_first["paired_own_damage_minus_baselines"],
            {"cat.fury.profile1": candidate_first["own_effective_damage"] - cat["own_effective_damage"]},
        )
        self.assertEqual(candidate_first["own_effective_damage"], candidate_second["own_effective_damage"])
        self.assertEqual(candidate_first["ttk_ms"], candidate_second["ttk_ms"])
        receipt = candidate_first["team_response_evidence"]
        self.assertGreater(receipt["events_emitted"], 0)
        self.assertGreater(receipt["events_retargeted_after_endogenous_death"], 0)
        self.assertEqual(receipt["native_runtime_receipt_status"], "COMPLETE_BOUND")
        self.assertTrue(all(receipt["native_runtime_receipt_checks"].values()))
        self.assertFalse(receipt["learned_teammate_model_used"])

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_responsive_receipt_ordinal_gap_remains_ineligible(self) -> None:
        original = V14ProjectedDynamicV3Bridge._request

        def corrupt_first_ordinal(bridge, command, **kwargs):
            response = original(bridge, command, **kwargs)
            if command == "dynamic_team_response_receipts":
                response["dynamic_team_response_receipts"]["receipts"][0]["damage_ordinal"] += 1
            return response

        with patch.object(V14ProjectedDynamicV3Bridge, "_request", corrupt_first_ordinal):
            panel = run_retarget_wave_panel_v1(20260913)
        cat = next(row for row in panel["rows"] if row["policy_id"] == "cat.fury.profile1")
        candidate = next(row for row in panel["rows"] if row["role"] == "CANDIDATE")
        self.assertEqual(cat["status"], "COMPLETED_BUT_INELIGIBLE")
        self.assertEqual(candidate["status"], "COMPLETED_BUT_INELIGIBLE")
        self.assertFalse(panel["team_retarget_comparison_eligible"])


if __name__ == "__main__":
    unittest.main()
