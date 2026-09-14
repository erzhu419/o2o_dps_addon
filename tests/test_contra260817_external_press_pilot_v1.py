from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from o2o_dps.contra260817_external_press_pilot_v1 import (
    run_contra260817_external_press_pilot_v1,
)
from o2o_dps.contra260817_fury_full_policy_v3 import (
    Contra260817FuryFullPolicyAdapterV3,
)
from o2o_dps.contra260817_fury_ordered_sink_executor_v4 import (
    Contra260817SimulatorControlFacadeV4,
    execute_contra260817_ordered_sinks_v4,
)
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.expert_policy import (
    CastControl, RawSink, StanceOp, SwingQueueOp, TargetOp, WAIT_ACTION,
)
from o2o_dps.expert_proposals import QUEUE_REFS
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_dynamic_target_semantics_v4 import HEALTH_STAT_INDEX
from o2o_dps.fury_expert_guided_search_v1 import ACTION_KEY_TO_REF
from o2o_dps.fury_expert_adapters import BLOODTHIRST
from o2o_dps.sim_bridge import ActResult, AvailableAction, DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3
from tests.test_cat_external_press_pilot_v1 import TARGET, StaticPressBridge
from tests.test_contra260817_fury_full_policy_v3 import _state


class _DynamicPressBridge(StaticPressBridge):
    def __init__(self) -> None:
        super().__init__()
        self.current["dynamic_team_background"] = {
            "targets": [{"target_index": 0, "dead": False}],
            "simulated_damage_applied": 0.0,
        }

    def load_dynamic_v3(self, request, seed, config):
        self.commands.append("load_dynamic_v3")
        return SimpleNamespace(state=self._state())

    def actions(self):
        return [AvailableAction(
            index=0, action=ACTION_KEY_TO_REF[BLOODTHIRST],
            label="Bloodthirst", legal=True, ready_in_ms=0,
            triggers_gcd=True,
        )]

    def act(self, action, *, attempt_id=None):
        self.commands.append("act")
        if action != ACTION_KEY_TO_REF[BLOODTHIRST]:
            raise AssertionError("unexpected source action")
        self.current["needs_input"] = False
        return ActResult(True, True, False, False, self._state())


def _decisions(adapter):
    base = adapter.propose(_state())
    common = {
        "off_gcd": (),
        "swing_queue": SwingQueueOp.KEEP,
        "stance": StanceOp.KEEP,
        "target": TargetOp.KEEP,
        "cast_control": CastControl.KEEP,
    }
    waiting = replace(
        base, **common, gcd=WAIT_ACTION, wait_ms=125,
        raw_sink_order=(),
        metadata={**base.metadata, "raw_gcd_calls": []},
    )
    action = replace(
        base, **common, gcd=BLOODTHIRST, wait_ms=None,
        raw_sink_order=(
            RawSink("autoattack", "UseAction", "START", "test:start-attack"),
            RawSink("gcd", "CastSpellByName", "嗜血", "test:bloodthirst"),
        ),
        metadata={**base.metadata, "raw_gcd_calls": [BLOODTHIRST]},
    )
    return waiting, action


class Contra260817ExternalPressPilotTests(unittest.TestCase):
    def test_one_source_invocation_per_press_wait_abstains_and_sinks_keep_order(self):
        seed = 2026091401
        request = deepcopy(build_development_wave_case_v1(seed).request)
        request["encounter"]["duration"] = 0.25
        target_health = float(request["encounter"]["targets"][0]["stats"][HEALTH_STAT_INDEX])
        dynamic_load = DynamicRolloutLoadV3.bind(
            request, seed,
            DynamicTargetSemanticsConfigV3(
                target_health=(DynamicTargetHealthV1(0, target_health),),
                idle_advance_horizon_ms=250,
            ),
        )
        adapter = Contra260817FuryFullPolicyAdapterV3()
        waiting, action = _decisions(adapter)
        bridge = _DynamicPressBridge()
        with (
            patch.object(adapter, "propose", side_effect=[waiting, action, waiting]) as propose,
            patch(
                "o2o_dps.contra260817_external_press_pilot_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.contra260817_external_press_pilot_v1._contra_state_mapper",
                return_value=lambda *args, **kwargs: _state(),
            ),
        ):
            artifact = run_contra260817_external_press_pilot_v1(
                bridge, request, {0: object()}, seed=seed, period_ms=100,
                dynamic_load=dynamic_load, adapter=adapter,
            )
        self.assertEqual(3, propose.call_count)
        self.assertEqual("DURATION_CENSORED_NONVOTING", artifact["status"])
        self.assertEqual("DURATION_CENSORED", artifact["terminal"]["kind"])
        self.assertEqual([0, 100, 200], [row["time_ms"] for row in artifact["presses"]])
        self.assertEqual([1, 2, 3], [row["press_index"] for row in artifact["presses"]])
        self.assertEqual([1, 1, 1], [row["source_invocation_count"] for row in artifact["presses"]])
        self.assertTrue(all(not row["finish_press_ready"] for row in artifact["presses"]))
        self.assertEqual(3, bridge.commands.count("finish_press"))
        self.assertEqual(1, bridge.commands.count("act"))
        self.assertEqual(1, bridge.commands.count("start_attack"))
        self.assertNotIn("wait", bridge.commands)
        self.assertEqual(
            "ABSTAINED_NO_EXTRA_KEY",
            artifact["presses"][0]["ordered_execution"]["wait_event"]["external_press_disposition"],
        )
        self.assertEqual(
            ["autoattack", "gcd"],
            [row["source_sink"]["channel"] for row in artifact["presses"][1]["ordered_execution"]["sink_events"]],
        )
        self.assertTrue(all(row["ordered_execution"]["source_reentry_clock"] is None for row in artifact["presses"]))
        self.assertFalse(artifact["comparison_ready"])
        self.assertFalse(artifact["voting_eligible"])

    def test_legacy_wait_and_nonconsuming_reentry_are_not_scheduled_by_external_mode(self):
        adapter = Contra260817FuryFullPolicyAdapterV3()
        waiting, action = _decisions(adapter)
        bridge = _DynamicPressBridge()
        current = bridge._state()
        controls = Contra260817SimulatorControlFacadeV4(bridge)
        waited = execute_contra260817_ordered_sinks_v4(
            controls, waiting, current, external_press_clock=True,
        )
        self.assertIsNone(waited["source_reentry_clock"])
        self.assertEqual("NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK", waited["wait_event"]["simulator_submission"]["status"])
        self.assertNotIn("wait", bridge.commands)

        # A rejected nonconsuming spell would trigger the old 100 ms reentry.
        def reject(action_ref, *, attempt_id=None):
            bridge.commands.append("act")
            return ActResult(False, False, False, False, bridge._state())

        bridge.act = reject
        rejected = execute_contra260817_ordered_sinks_v4(
            controls, action, current, external_press_clock=True,
        )
        self.assertIsNone(rejected["source_reentry_clock"])
        self.assertNotIn("wait", bridge.commands)

        queued = replace(
            waiting,
            swing_queue=SwingQueueOp.HEROIC_STRIKE,
            raw_sink_order=(
                RawSink("swing_queue", "CastSpellByName", "英勇打击", "test:queue"),
            ),
        )
        bridge.actions = lambda: [AvailableAction(
            index=0, action=QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
            label="Heroic Strike", legal=True, ready_in_ms=0,
            triggers_gcd=False,
        )]

        def accept_queue(action_ref, *, attempt_id=None):
            bridge.commands.append("queue")
            return ActResult(True, False, False, True, bridge._state())

        bridge.act = accept_queue
        queue_receipt = execute_contra260817_ordered_sinks_v4(
            controls, queued, current, external_press_clock=True,
        )
        self.assertEqual("ACCEPTED", queue_receipt["sink_events"][0]["simulator_acceptance"]["status"])
        self.assertIsNone(queue_receipt["source_reentry_clock"])
        self.assertIsNone(queue_receipt["wait_event"])
        self.assertNotIn("wait", bridge.commands)

        class LegacyBridge(_DynamicPressBridge):
            def wait(self, wait_ms):
                self.commands.append("wait")
                self.current["needs_input"] = False
                return self._state()

        legacy_bridge = LegacyBridge()
        legacy_controls = Contra260817SimulatorControlFacadeV4(legacy_bridge)
        legacy = execute_contra260817_ordered_sinks_v4(
            legacy_controls, waiting, legacy_bridge._state(),
        )
        self.assertEqual(["wait"], legacy_bridge.commands)
        self.assertEqual("SUBMITTED", legacy["wait_event"]["simulator_submission"]["status"])

    def test_killing_sink_closes_last_press_without_second_finish_command(self):
        class KillingBridge(_DynamicPressBridge):
            def act(self, action, *, attempt_id=None):
                result = super().act(action, attempt_id=attempt_id)
                self.current["finished"] = True
                self.current["needs_input"] = False
                self.current["dynamic_team_background"]["targets"][0]["dead"] = True
                self.ready = False
                self.next_time += self.period
                return ActResult(True, True, True, False, self._state())

            def finish_press(self):
                if self.current["finished"]:
                    raise AssertionError("terminal key has no live press opportunity")
                return super().finish_press()

        seed = 2026091402
        request = deepcopy(build_development_wave_case_v1(seed).request)
        request["encounter"]["duration"] = 0.25
        target_health = float(request["encounter"]["targets"][0]["stats"][HEALTH_STAT_INDEX])
        load = DynamicRolloutLoadV3.bind(
            request, seed,
            DynamicTargetSemanticsConfigV3(
                target_health=(DynamicTargetHealthV1(0, target_health),),
                idle_advance_horizon_ms=250,
            ),
        )
        adapter = Contra260817FuryFullPolicyAdapterV3()
        waiting, action = _decisions(adapter)
        bridge = KillingBridge()
        with (
            patch.object(adapter, "propose", side_effect=[waiting, action]),
            patch(
                "o2o_dps.contra260817_external_press_pilot_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.contra260817_external_press_pilot_v1._contra_state_mapper",
                return_value=lambda *args, **kwargs: _state(),
            ),
        ):
            artifact = run_contra260817_external_press_pilot_v1(
                bridge, request, {0: object()}, seed=seed, period_ms=100,
                dynamic_load=load, adapter=adapter,
            )
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", artifact["status"])
        self.assertEqual(2, artifact["press_count"])
        self.assertEqual(1, bridge.commands.count("finish_press"))
        self.assertEqual(
            "SKIPPED_MODEL_TERMINAL",
            artifact["presses"][-1]["press_closure"],
        )
        self.assertIsNone(artifact["presses"][-1]["finish_press_ready"])


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NATIVE_BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v18.exe"


@unittest.skipUnless(os.name == "nt" and NATIVE_BRIDGE.is_file(), "native v18 bridge required")
class NativeContra260817PressPilotTests(unittest.TestCase):
    def test_real_bridge_whole_wave_uses_only_one_source_call_per_key(self):
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        with V14ProjectedDynamicV3Bridge(
            NATIVE_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
        ) as bridge:
            artifact = run_contra260817_external_press_pilot_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100,
                dynamic_load=case.dynamic_load, max_presses=200,
            )
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", artifact["status"])
        self.assertTrue(artifact["terminal"]["required_hostiles_defeated"])
        self.assertTrue(all(
            row["press_closure"] == "FINISH_PRESS"
            for row in artifact["presses"][:-1]
        ))
        self.assertIn(
            artifact["presses"][-1]["press_closure"],
            {"FINISH_PRESS", "SKIPPED_MODEL_TERMINAL"},
        )
        self.assertIsNone(artifact["terminal"]["reason"])
        self.assertEqual(
            [1] * artifact["press_count"],
            [row["source_invocation_count"] for row in artifact["presses"]],
        )


if __name__ == "__main__":
    unittest.main()
