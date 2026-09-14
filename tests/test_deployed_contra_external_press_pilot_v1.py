from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.deployed_contra_external_press_pilot_v1 import (
    _ExternalKeyBridge,
    _externalize_wait,
    run_deployed_contra_external_press_pilot_v1,
)
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_raid_b_wave_v1 import build_dual_wield_raid_b_wave_v1
from o2o_dps.development_wave_panel_v1 import DEFAULT_BINDING
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_expert_adapters import WeaponMode
from o2o_dps.fury_ordered_sink_executor_raid_b_v1 import execute_ordered_sinks_raid_b_v1
from o2o_dps.fury_ordered_sink_executor_v2 import ControlSinkResultV2
from o2o_dps.fury_runtime_bound_deployed_contra_raid_b_v1 import RuntimeBoundContraRaidBAdapterV1
from o2o_dps.sim_bridge import ActResult, AvailableAction, DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3
from tests.test_fury_contra_adapter_v2 import _state
from tests.test_fury_runtime_bound_deployed_contra_adapter_v7 import _binding


SEED = 2026091401


def _case():
    case = build_development_wave_case_v1(SEED)
    request = deepcopy(case.request)
    request["encounter"]["duration"] = 0.25
    request["encounter"]["targets"].append(deepcopy(request["encounter"]["targets"][0]))
    request["encounter"]["targets"][1]["name"] = "Target 1"
    health = case.dynamic_load.config.target_health[0].health
    config = DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, health), DynamicTargetHealthV1(1, health)),
        idle_advance_horizon_ms=250,
    )
    load = DynamicRolloutLoadV3.bind(request, SEED, config)
    contexts = {
        0: case.target_contexts[0],
        1: replace(case.target_contexts[0], target_index=1,
                   target_name="Target 1", context_id="target-1"),
    }
    return request, load, contexts


class DynamicPressBridge:
    def __init__(self):
        self.commands = []
        self.current = {
            "time_ms": 0, "finished": False, "needs_input": True,
            "target_index": 0, "num_targets": 2,
            "power": {"type": "rage", "current": 0.0, "maximum": 100},
            "auras": [{"label": "狂暴姿态", "action": {"spell_id": 2458}}],
            "dynamic_team_background": {
                "targets": [{"target_index": 0, "dead": False},
                            {"target_index": 1, "dead": False}],
                "simulated_damage_applied": 0.0,
            },
        }
        self.period = 0
        self.next_time = 0
        self.index = 0
        self.ready = False

    def state(self):
        state = deepcopy(self.current)
        if self.period:
            state["press_clock"] = {
                "period_ms": self.period, "phase_ms": 0,
                "press_index": self.index, "ready": self.ready,
                "next_time_ms": self.next_time,
            }
        return state

    def load_dynamic_v3(self, request, seed, config):
        self.commands.append("load_dynamic_v3")
        return SimpleNamespace(state=self.state())

    def configure_press_clock(self, period_ms, phase_ms=0):
        self.commands.append("configure_press_clock")
        self.period = period_ms
        self.next_time = phase_ms
        return self.state()

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
        return self.state()

    def finish_press(self):
        self.commands.append("finish_press")
        if not self.ready:
            raise AssertionError("no ready key")
        self.ready = False
        self.next_time += self.period
        self.current["needs_input"] = False
        return self.state()

    def actions(self):
        return [AvailableAction(
            index=0, action=ACTION_KEY_TO_REF["warrior.berserker_stance"],
            label="Berserker Stance", legal=True, ready_in_ms=0,
            triggers_gcd=False,
        )]

    def start_attack(self):
        self.commands.append("start_attack")
        return ControlSinkResultV2(True, False, self.state())

    def act(self, action, *, attempt_id=None):
        self.commands.append("act")
        raise AssertionError("active stance should have been idempotent")

    def wait(self, wait_ms):
        self.commands.append("legacy_wait")
        raise AssertionError("legacy wait must not reach simulator")


class DeployedContraExternalPressPilotV1Tests(unittest.TestCase):
    def test_runtime_bound_raid_b_wait_abstains_on_each_physical_key(self):
        request, load, contexts = _case()
        binding = _binding()
        bridge = DynamicPressBridge()
        source_state = _state(weapon_mode=WeaponMode.DUAL_WIELD)
        with patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._resolve_target_semantics_v4",
            return_value={},
        ), patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._combat_state",
            return_value=source_state.combat,
        ), patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._proposal",
            side_effect=lambda adapter, combat, target: adapter.propose(source_state),
        ):
            artifact = run_deployed_contra_external_press_pilot_v1(
                bridge, request, contexts, runtime_binding=binding,
                seed=SEED, period_ms=100, dynamic_load=load,
            )
        self.assertEqual("DURATION_CENSORED_NONVOTING", artifact["status"], artifact["terminal"]["reason"])
        self.assertEqual([0, 100, 200], [row["time_ms"] for row in artifact["presses"]])
        self.assertEqual([1, 2, 3], [row["press_index"] for row in artifact["presses"]])
        self.assertEqual([1, 1, 1], [row["source_invocation_count"] for row in artifact["presses"]])
        self.assertEqual(3, bridge.commands.count("finish_press"))
        self.assertEqual(3, bridge.commands.count("start_attack"))
        self.assertNotIn("legacy_wait", bridge.commands)
        self.assertFalse(artifact["comparison_ready"])
        for press in artifact["presses"]:
            self.assertTrue(press["source_wait_abstained"])
            self.assertFalse(press["finish_press_ready"])
            self.assertEqual(binding["binding_sha256"], press["proposal"]["metadata"]["runtime_binding_sha256"])
            execution = press["ordered_execution"]
            self.assertEqual("ABSTAINED_NO_EXTRA_KEY", execution["wait_event"]["external_press_disposition"])
            self.assertEqual("NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK", execution["wait_event"]["simulator_submission"]["status"])
            self.assertFalse(execution["decision_consumed"])

    def test_raid_b_low_rage_retry_is_not_an_extra_key(self):
        binding = _binding()
        source = _state(
            weapon_mode=WeaponMode.DUAL_WIELD, rage=15,
            contra_st_s=1.0, whirlwind_ready_in_s=3.0,
            bloodthirst_ready_in_s=2.0,
        )
        source = replace(source, target_health_pct=19.0)
        decision = RuntimeBoundContraRaidBAdapterV1(binding).propose(source)

        class RetryBridge(DynamicPressBridge):
            def actions(self):
                return super().actions() + [AvailableAction(
                    index=1, action=ACTION_KEY_TO_REF["warrior.bloodthirst"],
                    label="Bloodthirst", legal=False, ready_in_ms=0,
                    triggers_gcd=True,
                )]

            def act(self, action, *, attempt_id=None):
                self.commands.append("act")
                return ActResult(False, False, False, True, self.state())

        bridge = RetryBridge()
        bridge.current["power"]["current"] = 15.0
        facade = _ExternalKeyBridge(bridge)
        execution = execute_ordered_sinks_raid_b_v1(facade, decision, bridge.state())
        self.assertEqual([100], facade.intercepted_waits)
        self.assertNotIn("legacy_wait", bridge.commands)
        self.assertIsNotNone(execution["source_reentry_clock"])
        _externalize_wait(execution, facade)
        self.assertIsNone(execution["source_reentry_clock"])
        self.assertEqual("SIMULATOR_EXTERNAL_PRESS_CLOCK", execution["external_press_reentry"]["timing_authority"])
        self.assertFalse(execution["decision_consumed"])
        self.assertTrue(execution["final_state"]["needs_input"])

    def test_lethal_sink_retires_key_without_finish_press(self):
        request, load, contexts = _case()
        binding = _binding()
        source_state = _state(
            weapon_mode=WeaponMode.DUAL_WIELD, rage=65,
            contra_st_s=1.0, whirlwind_ready_in_s=3.0,
        )

        class LethalBridge(DynamicPressBridge):
            def actions(self):
                return super().actions() + [AvailableAction(
                    index=1, action=ACTION_KEY_TO_REF["warrior.bloodthirst"],
                    label="Bloodthirst", legal=True, ready_in_ms=0,
                    triggers_gcd=True,
                ), AvailableAction(
                    index=2, action=QUEUE_REFS[SwingQueueOp.CLEAVE],
                    label="Cleave", legal=True, ready_in_ms=0,
                    triggers_gcd=False,
                )]

            def act(self, action, *, attempt_id=None):
                self.commands.append("act")
                if action == ACTION_KEY_TO_REF["warrior.bloodthirst"]:
                    self.current["finished"] = True
                    self.current["needs_input"] = False
                    for target in self.current["dynamic_team_background"]["targets"]:
                        target["dead"] = True
                    return ActResult(True, True, True, False, self.state())
                return ActResult(True, False, False, True, self.state())

        bridge = LethalBridge()
        with patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._resolve_target_semantics_v4",
            return_value={},
        ), patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._combat_state",
            return_value=source_state.combat,
        ), patch(
            "o2o_dps.deployed_contra_external_press_pilot_v1._proposal",
            side_effect=lambda adapter, combat, target: adapter.propose(source_state),
        ):
            artifact = run_deployed_contra_external_press_pilot_v1(
                bridge, request, contexts, runtime_binding=binding,
                seed=SEED, period_ms=100, dynamic_load=load,
            )
        self.assertEqual("MODEL_TARGET_DEFEATED", artifact["terminal"]["kind"], artifact["terminal"]["reason"])
        self.assertEqual(1, artifact["press_count"])
        self.assertEqual("MODEL_TERMINAL_DURING_KEY", artifact["presses"][0]["finish_press_disposition"])
        self.assertEqual(0, bridge.commands.count("finish_press"))


BRIDGE = Path(__file__).resolve().parents[1] / "bin/o2obridge.press-v18.exe"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    os.name == "nt" and BRIDGE.is_file() and DEFAULT_BINDING.is_file(),
    "native v18 bridge and deployed binding required",
)
class NativeDeployedContraPressPilotV1Tests(unittest.TestCase):
    def test_runtime_bound_raid_b_accepts_single_target_dual_wield(self):
        base = build_development_wave_case_v1(SEED)
        dual, _ = build_dual_wield_raid_b_wave_v1(SEED)
        request = deepcopy(base.request)
        request["raid"]["parties"][0]["players"][0]["equipment"] = deepcopy(
            dual.request["raid"]["parties"][0]["players"][0]["equipment"]
        )
        load = DynamicRolloutLoadV3.bind(request, SEED, base.dynamic_load.config)
        context = replace(
            base.target_contexts[0],
            equipped_item_names=dual.target_contexts[0].equipped_item_names,
            equipment_evidence=dual.target_contexts[0].equipment_evidence,
        )
        binding = json.loads(DEFAULT_BINDING.read_text(encoding="utf-8"))
        with V14ProjectedDynamicV3Bridge(
            BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
        ) as bridge:
            artifact = run_deployed_contra_external_press_pilot_v1(
                bridge, request, {0: context}, runtime_binding=binding,
                seed=SEED, period_ms=100, dynamic_load=load, max_presses=3,
            )
        self.assertEqual("WATCHDOG_TRUNCATED", artifact["terminal"]["kind"], artifact["terminal"]["reason"])
        self.assertEqual(3, artifact["press_count"])
        self.assertEqual([0, 100, 200], [press["time_ms"] for press in artifact["presses"]])

    def test_runtime_bound_raid_b_finishes_model_wave_on_external_keys(self):
        case, _ = build_dual_wield_raid_b_wave_v1(SEED)
        binding = json.loads(DEFAULT_BINDING.read_text(encoding="utf-8"))
        with V14ProjectedDynamicV3Bridge(
            BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
        ) as bridge:
            artifact = run_deployed_contra_external_press_pilot_v1(
                bridge, case.request, case.target_contexts,
                runtime_binding=binding, seed=SEED, period_ms=100,
                dynamic_load=case.dynamic_load, max_presses=200,
            )
        self.assertEqual("MODEL_TARGET_DEFEATED", artifact["terminal"]["kind"], artifact["terminal"]["reason"])
        self.assertTrue(artifact["terminal"]["required_hostiles_defeated"])
        self.assertGreater(artifact["press_count"], 1)
        self.assertEqual(
            list(range(0, artifact["press_count"] * 100, 100)),
            [press["time_ms"] for press in artifact["presses"]],
        )
        self.assertTrue(all(press["source_invocation_count"] == 1 for press in artifact["presses"]))
        self.assertTrue(all(press["ordered_execution"]["nonfaithful_reasons"] == [] for press in artifact["presses"]))
        self.assertEqual(binding["binding_sha256"], artifact["runtime_binding_sha256"])
        self.assertFalse(artifact["comparison_ready"])


if __name__ == "__main__":
    unittest.main()
