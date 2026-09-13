from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_external_press_pilot_v1 import run_cat_external_press_pilot_v1
from o2o_dps.cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from o2o_dps.cat_fury_ordered_sink_executor_v5 import execute_cat_fury_ordered_sinks_v5
from o2o_dps.expert_policy import ExpertDecision, ExpertProvenance, ExpertRole, ProvenanceKind
from o2o_dps.fury_expert_guided_search_v1 import ACTION_KEY_TO_REF
from o2o_dps.sim_bridge import ActResult, AvailableAction


REQUEST = {
    "raid": {"parties": [{"players": [{
        "distanceFromTarget": 3, "equipment": {"items": [{}, {}]},
    }]}]},
    "encounter": {"duration": 0.25, "targets": [{"level": 63, "name": "Target 0"}]},
    "simOptions": {"iterations": 1},
}
TARGET = {
    "target_index": 0,
    "target_health_pct": 100.0,
    "target_max_health": 10000,
    "target_classification": "worldboss",
    "target_name": "Target 0",
    "target_distance_yards": 3.0,
    "equipped_item_names": [],
    "field_evidence": {},
}


class StaticPressBridge:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.current = {
            "time_ms": 0, "finished": False, "needs_input": True,
            "power": {"current": 10.0},
            "mh_swing_remaining_ms": 2000,
            "mh_swing_duration_ms": 3000,
            "oh_swing_remaining_ms": 1500,
            "target_index": 0, "num_targets": 1,
            "target_health_known": True,
            "target_health": 10000,
            "target_health_percent": 100.0,
            "target_health_max": 10000,
            "gcd_remaining_ms": 0, "damage_done": 0.0,
            "auras": [],
        }
        self.period = 0
        self.next_time = 0
        self.index = 0
        self.ready = False

    def _state(self):
        state = deepcopy(self.current)
        if self.period:
            state["press_clock"] = {
                "period_ms": self.period, "phase_ms": 0,
                "press_index": self.index, "ready": self.ready,
                "next_time_ms": self.next_time,
            }
        return state

    def load(self, request, seed):
        self.commands.append("load")
        return self._state()

    def configure_press_clock(self, period_ms, phase_ms=0):
        self.commands.append("configure_press_clock")
        self.period = period_ms
        self.next_time = phase_ms
        return self._state()

    def advance(self):
        self.commands.append("advance")
        if self.ready:
            raise AssertionError("advance before finish_press")
        if self.next_time >= 250:
            self.current["time_ms"] = 250
            self.current["finished"] = True
            self.current["needs_input"] = False
        else:
            self.current["time_ms"] = self.next_time
            self.index += 1
            self.ready = True
            self.current["needs_input"] = True
        return self._state()

    def finish_press(self):
        self.commands.append("finish_press")
        if not self.ready:
            raise AssertionError("no ready key")
        self.ready = False
        self.next_time += self.period
        self.current["needs_input"] = False
        return self._state()

    def state(self):
        return self._state()

    def actions(self):
        return [AvailableAction(
            index=0, action=ACTION_KEY_TO_REF["warrior.battle_shout"],
            label="Battle Shout", legal=True, ready_in_ms=0,
            triggers_gcd=True,
        )]

    def start_attack(self):
        self.commands.append("start_attack")
        return SimpleNamespace(accepted=True, consumes_decision=False, state=self._state())

    def act(self, action, *, attempt_id=None):
        self.commands.append("act")
        if action != ACTION_KEY_TO_REF["warrior.battle_shout"]:
            raise AssertionError("unexpected source action")
        self.current["auras"] = [{"label": "Battle Shout", "remaining_ms": 30000}]
        self.current["power"] = {"current": 0.0}
        self.current["needs_input"] = False
        return ActResult(True, True, False, False, self._state())

    def wait(self, wait_ms):
        raise AssertionError("legacy bridge.wait must never run in press mode")


class CatExternalPressPilotTests(unittest.TestCase):
    def _run(self, kind, *, max_presses=5, bridge_type=StaticPressBridge):
        bridge = bridge_type()
        with patch(
            "o2o_dps.cat_external_press_pilot_v1._resolve_target_semantics",
            return_value=TARGET,
        ):
            artifact = run_cat_external_press_pilot_v1(
                bridge, REQUEST, {0: object()}, seed=2026091401,
                period_ms=100, policy_kind=kind,
                max_presses=max_presses,
                simulator_inputs=CatFurySimulatorInputsV5(initial_autoattack_active=False),
            )
        return artifact, bridge

    def test_cat_and_zero_residual_share_exact_ticks_and_source_actions(self):
        cat, cat_bridge = self._run("cat")
        residual, residual_bridge = self._run("zero_residual")
        self.assertEqual("DURATION_CENSORED_NONVOTING", cat["status"])
        self.assertEqual("DURATION_CENSORED", cat["terminal"]["kind"])
        self.assertFalse(cat["terminal"]["required_hostiles_defeated"])
        self.assertEqual([0, 100, 200], [row["time_ms"] for row in cat["presses"]])
        self.assertEqual([1, 2, 3], [row["press_index"] for row in cat["presses"]])
        self.assertEqual(3, cat_bridge.commands.count("finish_press"))
        self.assertEqual(2, len(cat["presses"][0]["ordered_execution"]["sink_events"]))
        self.assertEqual(1, cat_bridge.commands.count("start_attack"))
        self.assertEqual(cat_bridge.commands, residual_bridge.commands)
        self.assertEqual(
            [row["proposal"] for row in cat["presses"]],
            [row["proposal"] for row in residual["presses"]],
        )
        self.assertEqual([1, 1, 1], [row["source_invocation_count"] for row in cat["presses"]])
        self.assertTrue(all(not row["finish_press_ready"] for row in cat["presses"]))
        self.assertFalse(cat["comparison_ready"])

    def test_wait_abstains_without_calling_legacy_wait(self):
        artifact, bridge = self._run("cat")
        self.assertEqual("DURATION_CENSORED_NONVOTING", artifact["status"])
        self.assertTrue(artifact["presses"][1]["source_wait_abstained"])
        wait = artifact["presses"][1]["ordered_execution"]["wait_event"]
        self.assertEqual("ABSTAINED_NO_EXTRA_KEY", wait["external_press_disposition"])
        self.assertEqual("NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK", wait["simulator_submission"]["status"])
        self.assertNotIn("wait", bridge.commands)
        self.assertEqual(1, bridge.commands.count("act"))

    def test_watchdog_is_not_a_model_terminal(self):
        artifact, bridge = self._run("cat", max_presses=2)
        self.assertEqual("WATCHDOG_TRUNCATED_NONVOTING", artifact["status"])
        self.assertEqual("WATCHDOG_TRUNCATED", artifact["terminal"]["kind"])
        self.assertFalse(artifact["terminal"]["model_finished"])
        self.assertTrue(artifact["terminal"]["watchdog_truncated"])
        self.assertEqual(2, bridge.commands.count("finish_press"))

    def test_target_defeat_requires_observed_zero_health(self):
        class DefeatedBridge(StaticPressBridge):
            def advance(self):
                state = super().advance()
                if state["finished"]:
                    self.current["target_health"] = 0
                    return self._state()
                return state

        artifact, _ = self._run("cat", bridge_type=DefeatedBridge)
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", artifact["status"])
        self.assertEqual("MODEL_TARGET_DEFEATED", artifact["terminal"]["kind"])
        self.assertTrue(artifact["terminal"]["required_hostiles_defeated"])

    def test_unknown_static_health_does_not_report_raw_zero_as_defeat(self):
        class UnknownHealthBridge(StaticPressBridge):
            def __init__(self):
                super().__init__()
                self.current["target_health_known"] = False
                self.current["target_health"] = 0
                self.current["target_health_max"] = 0

        artifact, _ = self._run("cat", bridge_type=UnknownHealthBridge)
        self.assertEqual("DURATION_CENSORED_NONVOTING", artifact["status"])
        self.assertIsNone(artifact["terminal"]["required_hostiles_defeated"])
        self.assertIsNone(artifact["terminal"]["target_health"])
        self.assertIsNone(artifact["terminal"]["target_health_max"])

    def test_unsupported_sink_keeps_a_typed_nonterminal_receipt(self):
        class MissingActionBridge(StaticPressBridge):
            def actions(self):
                return []

        artifact, bridge = self._run("cat", bridge_type=MissingActionBridge)
        self.assertEqual("UNSUPPORTED_PRESS_PILOT_NONVOTING", artifact["status"])
        self.assertEqual("UNSUPPORTED", artifact["terminal"]["kind"])
        self.assertFalse(artifact["terminal"]["model_finished"])
        self.assertEqual(1, artifact["press_count"])
        self.assertEqual(1, bridge.commands.count("finish_press"))

    def test_existing_executor_default_keeps_legacy_source_wait(self):
        class LegacyWaitBridge:
            waits: list[int] = []

            def wait(self, wait_ms):
                self.waits.append(wait_ms)
                return {"time_ms": 0, "needs_input": False, "finished": False}

        bridge = LegacyWaitBridge()
        proposal = ExpertDecision(
            ExpertProvenance(
                "cat-wait-regression", ProvenanceKind.SOURCE_DERIVED,
                ExpertRole.CANDIDATE, ("fixture",),
            ),
            valid=True, wait_ms=125,
        )
        with patch(
            "o2o_dps.cat_fury_ordered_sink_executor_v5._preflight",
            return_value=([], []),
        ):
            receipt = execute_cat_fury_ordered_sinks_v5(
                bridge, proposal,
                {"time_ms": 0, "needs_input": True, "finished": False},
            )
        self.assertEqual([125], bridge.waits)
        self.assertEqual("SUBMITTED", receipt["wait_event"]["simulator_submission"]["status"])
        self.assertTrue(receipt["decision_consumed"])


if __name__ == "__main__":
    unittest.main()
