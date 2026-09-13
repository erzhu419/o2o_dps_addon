from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from o2o_dps.expert_policy import RawSink, StanceOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.fury_ordered_sink_executor_v5 import (
    EXECUTION_SCHEMA_V5,
    SOURCE_REENTRY_CLOCK_SCHEMA_V5,
    audit_ordered_execution_v5,
    execute_ordered_sinks_v5,
)
from o2o_dps.fury_runtime_bound_deployed_contra_adapter_v7 import (
    RUNTIME_BOUND_EXPERT_ID_V7,
)
from tests.test_fury_ordered_sink_executor_v2 import BLOODTHIRST, EXECUTE, _provenance, _state
from tests.test_fury_ordered_sink_executor_v3 import (
    _IllegalForNonCooldownReasonBridge,
    _contra_decision,
)


class _SchedulingBridge(_IllegalForNonCooldownReasonBridge):
    def wait(self, wait_ms: int):
        self.calls.append(("wait", wait_ms))
        self.current["needs_input"] = False
        return deepcopy(self.current)


def _decision(source_ref: str = "Contra_ALL.lua:31683"):
    base = _contra_decision(
        gcd=BLOODTHIRST,
        wait_ms=None,
        stance=StanceOp.BERSERKER,
        sinks=(
            RawSink(
                "autoattack", "Contra.StartAttack", "START",
                "Contra_ALL.lua:31610",
            ),
            RawSink(
                "stance", "ContraZSCast", "狂暴姿态",
                "Contra_ALL.lua:31615",
            ),
            RawSink("gcd", "QueueSpellByName", "嗜血", source_ref),
        ),
    )
    return replace(base, provenance=_provenance(RUNTIME_BOUND_EXPERT_ID_V7))


def _low_hp_execute_decision():
    base = _contra_decision(
        gcd=EXECUTE,
        wait_ms=None,
        stance=StanceOp.BERSERKER,
        sinks=(
            RawSink("stance", "ContraZSCast", "狂暴姿态", "Contra_ALL.lua:31615"),
            RawSink("gcd", "QueueSpellByName", "嗜血", "Contra_ALL.lua:31683"),
            RawSink("gcd", "QueueSpellByName", "斩杀", "Contra_ALL.lua:31684"),
        ),
        metadata_overrides={"raw_gcd_calls": [BLOODTHIRST, EXECUTE]},
    )
    return replace(base, provenance=_provenance(RUNTIME_BOUND_EXPERT_ID_V7))


class FuryOrderedSinkExecutorV5Tests(unittest.TestCase):
    def test_low_hp_execute_rage_rejection_preserves_two_sinks_and_reenters(self) -> None:
        initial = _state()
        initial["time_ms"] = 8_658
        initial["power"] = {"type": "rage", "current": 5.6759, "maximum": 100}
        initial["auras"] = [{
            "label": "狂暴姿态",
            "action": {"spell_id": ACTION_KEY_TO_REF["warrior.berserker_stance"].spell_id},
        }]
        decision = _low_hp_execute_decision()
        bridge = _SchedulingBridge(initial, illegal={
            ACTION_KEY_TO_REF[BLOODTHIRST], ACTION_KEY_TO_REF[EXECUTE],
        })

        result = execute_ordered_sinks_v5(bridge, decision, initial)

        gcd_events = [row for row in result["sink_events"] if row["source_sink"]["channel"] == "gcd"]
        self.assertEqual([row["source_sink"]["source_ref"] for row in gcd_events], [
            "Contra_ALL.lua:31683", "Contra_ALL.lua:31684",
        ])
        self.assertEqual([row["client_acceptance"]["status"] for row in gcd_events], [
            "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
            "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
        ])
        self.assertTrue(all(
            row["server_result"]["status"] == "NOT_APPLICABLE_CLIENT_REJECTED"
            and row["decision_consumption"]["consumes_decision"] is False
            for row in gcd_events
        ))
        self.assertEqual(result["source_reentry_clock"]["reclassified_source_refs"], [
            "Contra_ALL.lua:31683", "Contra_ALL.lua:31684",
        ])
        self.assertEqual(
            result["source_reentry_clock"]["trigger"],
            "LOW_HP_EXECUTE_31684_TYPED_RAGE_REJECTION_NONCONSUMING",
        )
        self.assertEqual(bridge.calls, [
            ("act", ACTION_KEY_TO_REF[BLOODTHIRST]),
            ("act", ACTION_KEY_TO_REF[EXECUTE]),
            ("wait", 100),
        ])
        self.assertEqual(audit_ordered_execution_v5(result, decision, decision_index=0), [])

    def test_execute_rejection_at_or_above_actual_talented_cost_stays_unclassified(self) -> None:
        initial = _state()
        initial["power"] = {"type": "rage", "current": 10.0, "maximum": 100}
        initial["auras"] = [{
            "label": "狂暴姿态",
            "action": {"spell_id": ACTION_KEY_TO_REF["warrior.berserker_stance"].spell_id},
        }]
        bridge = _SchedulingBridge(initial, illegal={
            ACTION_KEY_TO_REF[BLOODTHIRST], ACTION_KEY_TO_REF[EXECUTE],
        })

        result = execute_ordered_sinks_v5(bridge, _low_hp_execute_decision(), initial)

        self.assertEqual(result["sink_events"][-1]["client_acceptance"]["status"], "REJECTED_UNCLASSIFIED")
        self.assertIsNone(result["source_reentry_clock"])
        self.assertNotIn(("wait", 100), bridge.calls)

    def test_low_hp_bt_rage_rejection_reenters_without_successful_cast(self) -> None:
        initial = _state()
        initial["time_ms"] = 5_000
        initial["power"] = {"type": "rage", "current": 15.0, "maximum": 100}
        initial["auras"] = [{
            "label": "狂暴姿态",
            "action": {"spell_id": ACTION_KEY_TO_REF["warrior.berserker_stance"].spell_id},
        }]
        decision = _decision()
        bridge = _SchedulingBridge(
            initial,
            illegal={ACTION_KEY_TO_REF[BLOODTHIRST]},
        )

        result = execute_ordered_sinks_v5(bridge, decision, initial)

        self.assertEqual(result["schema"], EXECUTION_SCHEMA_V5)
        self.assertEqual(
            bridge.calls,
            [("start_attack", None), ("act", ACTION_KEY_TO_REF[BLOODTHIRST]), ("wait", 100)],
        )
        self.assertEqual(
            result["sink_events"][-1]["client_acceptance"]["status"],
            "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
        )
        self.assertEqual(
            result["sink_events"][-1]["server_result"]["status"],
            "NOT_APPLICABLE_CLIENT_REJECTED",
        )
        self.assertEqual(result["source_reentry_clock"]["schema"], SOURCE_REENTRY_CLOCK_SCHEMA_V5)
        self.assertFalse(result["source_reentry_clock"]["policy_action"])
        self.assertFalse(result["source_reentry_clock"]["source_sink_decision_consumed"])
        self.assertEqual(result["final_state"]["time_ms"], 5_000)
        self.assertFalse(result["final_state"]["needs_input"])
        self.assertEqual(audit_ordered_execution_v5(result, decision, decision_index=0), [])

    def test_other_source_branch_stays_unclassified_and_does_not_wait(self) -> None:
        initial = _state()
        initial["power"] = {"type": "rage", "current": 15.0, "maximum": 100}
        initial["auras"] = [{
            "label": "狂暴姿态",
            "action": {"spell_id": ACTION_KEY_TO_REF["warrior.berserker_stance"].spell_id},
        }]
        decision = _decision("Contra_ALL.lua:31684")
        bridge = _SchedulingBridge(initial, illegal={ACTION_KEY_TO_REF[BLOODTHIRST]})

        result = execute_ordered_sinks_v5(bridge, decision, initial)

        self.assertEqual(result["sink_events"][-1]["client_acceptance"]["status"], "REJECTED_UNCLASSIFIED")
        self.assertIsNone(result["source_reentry_clock"])
        self.assertNotIn(("wait", 100), bridge.calls)


if __name__ == "__main__":
    unittest.main()
