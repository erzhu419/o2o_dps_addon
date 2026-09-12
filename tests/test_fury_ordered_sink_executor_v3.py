from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.expert_policy import ExpertDecision, RawSink, StanceOp, SwingQueueOp, WAIT_ACTION
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_ordered_sink_executor_v3 import (
    EXECUTION_SCHEMA_V3,
    audit_ordered_execution_v3,
    execute_ordered_sinks_v3,
)
from o2o_dps.sim_bridge import ActResult, AvailableAction
from tests.test_fury_ordered_sink_executor_v2 import (
    BLOODTHIRST,
    _FakeBridge,
    _provenance,
    _state,
)


class _DelayedQueueBridge(_FakeBridge):
    def __init__(self, state, *, illegal=None) -> None:
        super().__init__(state, illegal=illegal)
        self.pending_queue = None

    def act(self, action):
        if action not in set(QUEUE_REFS.values()):
            return super().act(action)
        self.calls.append(("act", action))
        self.pending_queue = action
        return ActResult(
            casted=True,
            consumes_decision=False,
            finished=False,
            needs_input=True,
            state=deepcopy(self.current),
        )

    def wait(self, wait_ms: int):
        # Real WaitUntil returns before the 10 ms realism-ICD activation is
        # visible; the subsequent advance owns that observation.
        self.calls.append(("wait", wait_ms))
        self.current["time_ms"] = int(self.current["time_ms"]) + wait_ms
        self.current["needs_input"] = False
        return deepcopy(self.current)


class _IllegalForNonCooldownReasonBridge(_FakeBridge):
    def actions(self):
        return [
            AvailableAction(
                index=row.index,
                action=row.action,
                label=row.label,
                legal=row.legal,
                ready_in_ms=0,
                triggers_gcd=row.triggers_gcd,
            )
            for row in super().actions()
        ]


def _contra_decision(
    *,
    gcd: str = WAIT_ACTION,
    wait_ms: int | None = 100,
    stance: StanceOp = StanceOp.KEEP,
    queue: SwingQueueOp = SwingQueueOp.KEEP,
    sinks: tuple[RawSink, ...],
    metadata_overrides: dict[str, object] | None = None,
) -> ExpertDecision:
    metadata = {
        "raw_gcd_calls": ([] if gcd == WAIT_ACTION else [gcd]),
        "nampower_queue_spells_on_cooldown": False,
        "known_noop_retry_wait_ms": 100,
        "saved_xuanfeng": False,
    }
    metadata.update(metadata_overrides or {})
    return ExpertDecision(
        provenance=_provenance("contra.deployed.fury.raid_a.v2"),
        valid=True,
        gcd=gcd,
        wait_ms=wait_ms,
        stance=stance,
        swing_queue=queue,
        raw_sink_order=sinks,
        eligible_for_independent_vote=True,
        metadata=metadata,
    )


class FuryOrderedSinkExecutorV3Tests(unittest.TestCase):
    def test_active_stance_is_not_submitted_to_bridge(self) -> None:
        decision = _contra_decision(
            stance=StanceOp.BERSERKER,
            sinks=(
                RawSink(
                    "stance",
                    "ContraZSCast",
                    "狂暴姿态",
                    "Contra_ALL.lua:31615",
                ),
            ),
        )
        initial = _state()
        initial["auras"] = [
            {
                "label": "狂暴姿态",
                "action": {
                    "spell_id": ACTION_KEY_TO_REF[
                        "warrior.berserker_stance"
                    ].spell_id,
                    "tag": 0,
                },
            }
        ]
        bridge = _FakeBridge(
            initial,
            illegal={ACTION_KEY_TO_REF["warrior.berserker_stance"]},
        )

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(result["schema"], EXECUTION_SCHEMA_V3)
        self.assertEqual(bridge.calls, [("wait", 100)])
        event = result["sink_events"][0]
        self.assertEqual(
            event["simulator_submission"]["status"],
            "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
        )
        self.assertEqual(
            event["client_acceptance"]["status"],
            "NOT_APPLICABLE_STATE_ALREADY_SATISFIED",
        )
        self.assertEqual(audit_ordered_execution_v3(result, decision, decision_index=0), [])

    def test_unknown_stance_does_not_suppress_berserker_submission(self) -> None:
        decision = _contra_decision(
            stance=StanceOp.BERSERKER,
            sinks=(
                RawSink(
                    "stance",
                    "ContraZSCast",
                    "狂暴姿态",
                    "Contra_ALL.lua:31615",
                ),
            ),
        )
        initial = _state()
        initial["auras"] = []
        bridge = _FakeBridge(initial)

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            bridge.calls[0],
            ("act", ACTION_KEY_TO_REF["warrior.berserker_stance"]),
        )
        self.assertNotEqual(
            result["sink_events"][0]["simulator_submission"]["status"],
            "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
        )

    def test_immediate_queue_absence_is_pending_not_nonfaithful(self) -> None:
        decision = _contra_decision(
            queue=SwingQueueOp.HEROIC_STRIKE,
            sinks=(
                RawSink(
                    "swing_queue",
                    "QueueSpellByName",
                    "英勇打击",
                    "Contra_ALL.lua:31631",
                ),
            ),
        )
        initial = _state()
        bridge = _DelayedQueueBridge(initial)

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        transition = result["sink_events"][0]["queue_transition"]
        self.assertEqual(transition["kind"], "ACCEPTED_PENDING_ACTIVATION")
        self.assertTrue(result["ordered_projection_faithful"])
        self.assertEqual(audit_ordered_execution_v3(result, decision, decision_index=0), [])

    def test_illegal_contra_queue_spell_uses_declared_retry_wait(self) -> None:
        heroic = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = _contra_decision(
            gcd=BLOODTHIRST,
            wait_ms=None,
            queue=SwingQueueOp.HEROIC_STRIKE,
            sinks=(
                RawSink(
                    "swing_queue",
                    "QueueSpellByName",
                    "英勇打击",
                    "Contra_ALL.lua:31631",
                ),
                RawSink(
                    "gcd",
                    "QueueSpellByName",
                    "嗜血",
                    "Contra_ALL.lua:31628",
                ),
            ),
        )
        initial = _state()
        bridge = _DelayedQueueBridge(initial, illegal={bloodthirst})

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            [call for call in bridge.calls if call[0] == "act"],
            [("act", heroic), ("act", bloodthirst)],
        )
        self.assertEqual(
            result["sink_events"][1]["client_acceptance"]["status"],
            "REJECTED_SOURCE_CONFIGURED_NOOP",
        )
        self.assertEqual(result["wait_event"]["kind"], "SOURCE_NOOP_RETRY_WAIT")
        self.assertEqual(result["wait_event"]["requested_wait_ms"], 100)
        self.assertTrue(result["decision_consumed"])
        self.assertTrue(result["ordered_projection_faithful"])
        self.assertEqual(audit_ordered_execution_v3(result, decision, decision_index=0), [])

    def test_legal_but_rejected_queue_spell_remains_nonfaithful(self) -> None:
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = _contra_decision(
            gcd=BLOODTHIRST,
            wait_ms=None,
            sinks=(
                RawSink(
                    "gcd",
                    "QueueSpellByName",
                    "嗜血",
                    "Contra_ALL.lua:31628",
                ),
            ),
        )
        initial = _state()
        bridge = _FakeBridge(initial)
        bridge.act = lambda action: ActResult(False, False, False, True, initial)

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            result["sink_events"][0]["client_acceptance"]["status"],
            "REJECTED_UNCLASSIFIED",
        )
        self.assertFalse(result["ordered_projection_faithful"])
        self.assertIsNone(result["wait_event"])

    def test_non_cooldown_illegal_queue_spell_remains_nonfaithful(self) -> None:
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = _contra_decision(
            gcd=BLOODTHIRST,
            wait_ms=None,
            sinks=(
                RawSink(
                    "gcd",
                    "QueueSpellByName",
                    "嗜血",
                    "Contra_ALL.lua:31628",
                ),
            ),
        )
        initial = _state()
        bridge = _IllegalForNonCooldownReasonBridge(
            initial, illegal={bloodthirst}
        )

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            result["sink_events"][0]["client_acceptance"]["status"],
            "REJECTED_UNCLASSIFIED",
        )
        self.assertFalse(result["ordered_projection_faithful"])
        self.assertIsNone(result["wait_event"])

    def test_exact_unconditional_bloodthirst_resource_retry_is_source_noop(self) -> None:
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = _contra_decision(
            gcd=BLOODTHIRST,
            wait_ms=None,
            sinks=(
                RawSink(
                    "gcd",
                    "QueueSpellByName",
                    "嗜血",
                    "Contra_ALL.lua:31672",
                ),
            ),
        )
        initial = _state()
        initial["power"] = {"type": "rage", "current": 20.0, "maximum": 100}
        bridge = _IllegalForNonCooldownReasonBridge(
            initial, illegal={bloodthirst}
        )

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
            result["sink_events"][0]["client_acceptance"]["status"],
        )
        self.assertEqual("SOURCE_NOOP_RETRY_WAIT", result["wait_event"]["kind"])
        self.assertTrue(result["ordered_projection_faithful"])
        self.assertEqual(audit_ordered_execution_v3(result, decision, decision_index=0), [])

    def test_exact_large_nonboss_slam_resource_retry_is_source_noop(self) -> None:
        slam = ACTION_KEY_TO_REF["warrior.slam"]
        decision = _contra_decision(
            gcd="warrior.slam",
            wait_ms=None,
            sinks=(
                RawSink(
                    "gcd",
                    "CastSpellByName",
                    "猛击",
                    "Contra_ALL.lua:31677",
                ),
            ),
        )
        initial = _state()
        initial["power"] = {"type": "rage", "current": 4.0, "maximum": 100}
        bridge = _IllegalForNonCooldownReasonBridge(initial, illegal={slam})

        result = execute_ordered_sinks_v3(bridge, decision, initial)

        self.assertEqual(
            "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
            result["sink_events"][0]["client_acceptance"]["status"],
        )
        self.assertEqual("SOURCE_NOOP_RETRY_WAIT", result["wait_event"]["kind"])
        self.assertTrue(result["ordered_projection_faithful"])

    def test_resource_retry_classification_requires_every_exact_fact(self) -> None:
        cases = (
            ("other-source", BLOODTHIRST, "Contra_ALL.lua:31628", 20.0, {}),
            ("enough-rage", BLOODTHIRST, "Contra_ALL.lua:31672", 30.0, {}),
            (
                "different-action",
                "warrior.whirlwind",
                "Contra_ALL.lua:31672",
                20.0,
                {},
            ),
            (
                "xuanfeng-enabled",
                BLOODTHIRST,
                "Contra_ALL.lua:31672",
                20.0,
                {"saved_xuanfeng": True},
            ),
            (
                "queue-config-unknown",
                BLOODTHIRST,
                "Contra_ALL.lua:31672",
                20.0,
                {"nampower_queue_spells_on_cooldown": None},
            ),
            (
                "slam-enough-rage",
                "warrior.slam",
                "Contra_ALL.lua:31677",
                15.0,
                {},
            ),
            (
                "slam-other-source",
                "warrior.slam",
                "Contra_ALL.lua:31674",
                4.0,
                {},
            ),
        )
        for label, action_key, source_ref, rage, overrides in cases:
            with self.subTest(label=label):
                action = ACTION_KEY_TO_REF[action_key]
                decision = _contra_decision(
                    gcd=action_key,
                    wait_ms=None,
                    sinks=(
                        RawSink(
                            "gcd",
                            "QueueSpellByName",
                            action_key,
                            source_ref,
                        ),
                    ),
                    metadata_overrides=overrides,
                )
                initial = _state()
                initial["power"] = {
                    "type": "rage",
                    "current": rage,
                    "maximum": 100,
                }
                bridge = _IllegalForNonCooldownReasonBridge(
                    initial, illegal={action}
                )

                result = execute_ordered_sinks_v3(bridge, decision, initial)

                self.assertEqual(
                    "REJECTED_UNCLASSIFIED",
                    result["sink_events"][0]["client_acceptance"]["status"],
                )
                self.assertFalse(result["ordered_projection_faithful"])
                self.assertIsNone(result["wait_event"])


if __name__ == "__main__":
    unittest.main()
