from __future__ import annotations

from dataclasses import replace
import unittest

from o2o_dps.expert_policy import RawSink, StanceOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.fury_ordered_sink_executor_raid_b_v1 import (
    SCHEMA,
    audit_ordered_execution_raid_b_v1,
    execute_ordered_sinks_raid_b_v1,
)
from o2o_dps.fury_runtime_bound_deployed_contra_raid_b_v1 import EXPERT_ID
from tests.test_fury_ordered_sink_executor_v2 import BLOODTHIRST, _provenance, _state
from tests.test_fury_ordered_sink_executor_v3 import _contra_decision
from tests.test_fury_ordered_sink_executor_v5 import _SchedulingBridge


def _decision(source_ref="Contra_ALL.lua:32089"):
    base = _contra_decision(
        gcd=BLOODTHIRST, wait_ms=None, stance=StanceOp.BERSERKER,
        sinks=(
            RawSink("stance", "ContraZSCast", "狂暴姿态", "Contra_ALL.lua:32078"),
            RawSink("gcd", "QueueSpellByName", "嗜血", source_ref),
        ),
    )
    return replace(base, provenance=_provenance(EXPERT_ID))


class FuryOrderedSinkRaidBV1Tests(unittest.TestCase):
    def test_exact_low_rage_bloodthirst_source_branch_reenters(self):
        initial = _state()
        initial["time_ms"] = 5000
        initial["power"] = {"type": "rage", "current": 15.0, "maximum": 100}
        initial["auras"] = [{
            "label": "狂暴姿态",
            "action": {"spell_id": ACTION_KEY_TO_REF["warrior.berserker_stance"].spell_id},
        }]
        bridge = _SchedulingBridge(initial, illegal={ACTION_KEY_TO_REF[BLOODTHIRST]})
        decision = _decision()
        result = execute_ordered_sinks_raid_b_v1(bridge, decision, initial)
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["expert_id"], EXPERT_ID)
        self.assertEqual(result["sink_events"][-1]["client_acceptance"]["status"],
                         "REJECTED_SOURCE_RESOURCE_RETRY_NOOP")
        self.assertEqual(result["source_reentry_clock"]["scheduled_at_time_ms"], 5000)
        self.assertEqual(bridge.calls[-1], ("wait", 100))
        self.assertEqual(audit_ordered_execution_raid_b_v1(result, decision, decision_index=0), [])

    def test_other_source_ref_does_not_gain_resource_reentry(self):
        initial = _state()
        initial["power"] = {"type": "rage", "current": 15.0, "maximum": 100}
        bridge = _SchedulingBridge(initial, illegal={ACTION_KEY_TO_REF[BLOODTHIRST]})
        result = execute_ordered_sinks_raid_b_v1(
            bridge, _decision("Contra_ALL.lua:32071"), initial,
        )
        self.assertEqual(result["sink_events"][-1]["client_acceptance"]["status"],
                         "REJECTED_UNCLASSIFIED")
        self.assertIsNone(result["source_reentry_clock"])


if __name__ == "__main__":
    unittest.main()
