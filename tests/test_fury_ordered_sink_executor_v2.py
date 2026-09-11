from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.expert_policy import (
    CastControl,
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    WAIT_ACTION,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_ordered_sink_executor_v2 import (
    CAT_EXPERT_ID,
    ControlSinkResultV2,
    EXECUTION_SCHEMA,
    SUPPORTED_OPERATION_COVERAGE_V2,
    UNSUPPORTED_OPERATION_COVERAGE_V2,
    execute_ordered_sinks_v2,
)
from o2o_dps.fury_contra_adapter_v2 import (
    ContraDeployedFuryAdapterV2,
    ContraDeployedFuryStateV2,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
)
from o2o_dps.fury_expert_adapters import (
    CatFurySourceAdapter,
    FuryExpertState,
    WeaponMode,
)
from o2o_dps.sim_bridge import ActResult, AvailableAction


BLOODTHIRST = "warrior.bloodthirst"
EXECUTE = "warrior.execute"
WHIRLWIND = "warrior.whirlwind"


def _provenance(expert_id: str = CAT_EXPERT_ID) -> ExpertProvenance:
    return ExpertProvenance(
        expert_id=expert_id,
        kind=ProvenanceKind.SOURCE_DERIVED,
        role=ExpertRole.DEPLOYED,
        authority_files=("audited-source.lua",),
    )


def _state(
    *,
    needs_input: bool = True,
    queue: SwingQueueOp = SwingQueueOp.KEEP,
    casting: bool = False,
) -> dict[str, object]:
    auras: list[dict[str, object]] = []
    if queue is not SwingQueueOp.KEEP:
        ref = QUEUE_REFS[queue]
        auras.append(
            {
                "label": queue.value,
                "action": {"spell_id": ref.spell_id, "tag": ref.tag},
            }
        )
    return {
        "time_ms": 0,
        "needs_input": needs_input,
        "finished": False,
        "damage_done": 0.0,
        "auras": auras,
        "current_cast": (
            {"action": {"spell_id": 45961}, "remaining_ms": 500}
            if casting
            else None
        ),
    }


class _FakeBridge:
    def __init__(
        self,
        state: dict[str, object],
        *,
        illegal: set[object] | None = None,
        absent: set[object] | None = None,
    ) -> None:
        self.current = deepcopy(state)
        self.illegal = illegal or set()
        self.absent = absent or set()
        self.calls: list[tuple[str, object]] = []

    def actions(self) -> list[AvailableAction]:
        refs = list(ACTION_KEY_TO_REF.values()) + list(QUEUE_REFS.values())
        rows: list[AvailableAction] = []
        seen: set[object] = set()
        for ref in refs:
            if ref in seen or ref in self.absent:
                continue
            seen.add(ref)
            triggers_gcd = ref not in set(QUEUE_REFS.values()) and ref not in {
                ACTION_KEY_TO_REF["warrior.bloodrage"],
                ACTION_KEY_TO_REF["warrior.battle_stance"],
                ACTION_KEY_TO_REF["warrior.defensive_stance"],
                ACTION_KEY_TO_REF["warrior.berserker_stance"],
            }
            rows.append(
                AvailableAction(
                    index=len(rows),
                    action=ref,
                    label=f"spell-{ref.spell_id}-{ref.tag}",
                    legal=ref not in self.illegal,
                    ready_in_ms=1500 if ref in self.illegal else 0,
                    triggers_gcd=triggers_gcd,
                )
            )
        return rows

    def act(self, action):
        self.calls.append(("act", action))
        row = next(row for row in self.actions() if row.action == action)
        casted = row.legal and bool(self.current["needs_input"])
        consumes = casted and row.triggers_gcd
        if casted and action in set(QUEUE_REFS.values()):
            self.current["auras"] = [
                {
                    "label": "queue",
                    "action": {"spell_id": action.spell_id, "tag": action.tag},
                }
            ]
        if consumes:
            self.current["needs_input"] = False
        return ActResult(
            casted=casted,
            consumes_decision=consumes,
            finished=False,
            needs_input=bool(self.current["needs_input"]),
            state=deepcopy(self.current),
        )

    def wait(self, wait_ms: int):
        self.calls.append(("wait", wait_ms))
        self.current["time_ms"] = int(self.current["time_ms"]) + wait_ms
        self.current["needs_input"] = False
        return deepcopy(self.current)

    def start_attack(self):
        self.calls.append(("start_attack", None))
        self.current["autoattack_active"] = True
        return ControlSinkResultV2(True, False, deepcopy(self.current))

    def stop_cast(self):
        self.calls.append(("stop_cast", None))
        self.current["current_cast"] = None
        return ControlSinkResultV2(True, False, deepcopy(self.current))


class OrderedSinkExecutorV2Tests(unittest.TestCase):
    def test_exact_audited_operation_coverage_is_public(self) -> None:
        self.assertEqual(
            SUPPORTED_OPERATION_COVERAGE_V2[CAT_EXPERT_ID],
            {
                ("autoattack", "MPStartAttack"),
                ("cast_control", "SpellStopCasting"),
                ("gcd", "CastSpellByName"),
                ("gcd", "MPCastWithoutNampower"),
                ("off_gcd", "MPCastWithNampower"),
                ("stance", "CastSpellByName"),
                ("swing_queue", "MPCastWithNampower"),
            },
        )
        for expert_id in (
            "contra.deployed.fury.raid_a",
            "contra.deployed.fury.raid_a.v2",
        ):
            self.assertEqual(
                SUPPORTED_OPERATION_COVERAGE_V2[expert_id],
                {
                    ("autoattack", "Contra.StartAttack"),
                    ("cast_control", "SpellStopCasting"),
                    ("gcd", "CastSpellByName"),
                    ("gcd", "QueueSpellByName"),
                    ("off_gcd", "Contra prelude helper"),
                    ("stance", "ContraZSCast"),
                    ("swing_queue", "CastSpellByName"),
                    ("swing_queue", "QueueSpellByName"),
                },
            )
        self.assertTrue(any("cancellation" in item for item in UNSUPPORTED_OPERATION_COVERAGE_V2))
        self.assertTrue(any("Cat2" in item for item in UNSUPPORTED_OPERATION_COVERAGE_V2))

    def test_source_wait_is_exact_and_never_replaced_by_fallback(self) -> None:
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=137,
            raw_sink_order=(
                RawSink("autoattack", "MPStartAttack", "START", "Cat.lua:10"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state())

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertEqual(execution["schema"], EXECUTION_SCHEMA)
        self.assertEqual(bridge.calls, [("start_attack", None), ("wait", 137)])
        self.assertFalse(execution["fallback"]["used"])
        self.assertEqual(execution["wait_event"]["requested_wait_ms"], 137)
        self.assertEqual(
            execution["wait_event"]["decision_consumption"]["status"],
            "CONSUMED_BY_EXPLICIT_SOURCE_WAIT",
        )
        self.assertEqual(execution["raw_sink_order"], [decision.raw_sink_order[0].to_dict()])
        self.assertTrue(execution["ordered_projection_faithful"])

    def test_current_cat_and_deployed_contra_v2_decisions_pass_operation_preflight(self) -> None:
        cat = CatFurySourceAdapter().propose(
            FuryExpertState(
                rage=86,
                target_health_pct=50,
                weapon_mode=WeaponMode.DUAL_WIELD,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=0,
            )
        )
        combat = FuryExpertState(
            rage=0,
            target_health_pct=50,
            weapon_mode=WeaponMode.TWO_HAND,
            bloodthirst_ready_in_s=5,
            contra_st_s=0,
        )
        contra = ContraDeployedFuryAdapterV2().propose(
            ContraDeployedFuryStateV2(
                combat=combat,
                target_health_pct=50,
                target_max_health=60_000,
                target_classification="elite",
                target_name="卡拉赞精英",
                target_health_pct_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.SIMULATOR_STATE,
                    source_sha256="1" * 64,
                ),
                target_max_health_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.PINNED_STATIC_INPUT,
                    source_sha256="1" * 64,
                ),
                target_classification_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.PINNED_STATIC_INPUT,
                    source_sha256="1" * 64,
                ),
                target_name_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.PINNED_STATIC_INPUT,
                    source_sha256="1" * 64,
                ),
                equipped_item_names=(),
                equipment_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.PINNED_STATIC_INPUT,
                    source_sha256="1" * 64,
                ),
                target_position_evidence=ContraFieldEvidenceV2(
                    ContraEvidenceKindV2.SIMULATOR_STATE,
                    source_sha256="1" * 64,
                ),
            )
        )

        for label, decision in (("cat", cat), ("contra", contra)):
            with self.subTest(label=label):
                initial = _state()
                bridge = _FakeBridge(initial)
                execution = execute_ordered_sinks_v2(bridge, decision, initial)
                self.assertFalse(execution["execution_blocked"])
                self.assertFalse(
                    any(
                        "unsupported_operation" in reason
                        for reason in execution["nonfaithful_reasons"]
                    )
                )
                self.assertEqual(
                    execution["raw_sink_order"],
                    [sink.to_dict() for sink in decision.raw_sink_order],
                )

    def test_multiple_contra_gcd_sinks_remain_after_first_acceptance(self) -> None:
        sinks = (
            RawSink("autoattack", "Contra.StartAttack", "START", "Contra.lua:1"),
            RawSink("gcd", "QueueSpellByName", "旋风斩", "Contra.lua:2"),
            RawSink("gcd", "QueueSpellByName", "嗜血", "Contra.lua:3"),
            RawSink("gcd", "QueueSpellByName", "嗜血", "Contra.lua:4"),
        )
        decision = ExpertDecision(
            provenance=_provenance("contra.deployed.fury.raid_a.v2"),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            raw_sink_order=sinks,
            eligible_for_independent_vote=True,
            metadata={"raw_gcd_calls": [WHIRLWIND, BLOODTHIRST, BLOODTHIRST]},
        )
        bridge = _FakeBridge(_state())

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertEqual(execution["raw_sink_order"], [sink.to_dict() for sink in sinks])
        self.assertEqual(len(execution["sink_events"]), 4)
        self.assertEqual(
            [event["source_attempt"]["status"] for event in execution["sink_events"]],
            ["ATTEMPTED"] * 4,
        )
        self.assertEqual(
            execution["sink_events"][1]["client_acceptance"]["status"],
            "ACCEPTED",
        )
        self.assertEqual(
            [
                execution["sink_events"][index]["client_acceptance"]["status"]
                for index in (2, 3)
            ],
            [
                "NOT_APPLICABLE_DECISION_ALREADY_CONSUMED",
                "NOT_APPLICABLE_DECISION_ALREADY_CONSUMED",
            ],
        )
        self.assertEqual(
            [name for name, _ in bridge.calls],
            ["start_attack", "act"],
        )
        self.assertEqual(execution["accepted_gcd_actions"], [WHIRLWIND])
        self.assertFalse(execution["ordered_projection_faithful"])
        self.assertEqual(
            execution["nonfaithful_reasons"],
            [
                "raw_sink[3]:simulator_cannot_submit_after_decision_consumed",
                "raw_sink[4]:simulator_cannot_submit_after_decision_consumed",
            ],
        )
        self.assertEqual(
            execution["sink_events"][1]["traversal"]["source_sequence"],
            "CONTINUE_TO_NEXT_RAW_SINK",
        )

    def test_queue_replacement_is_distinct_from_acceptance_and_consumption(self) -> None:
        initial = _state(queue=SwingQueueOp.HEROIC_STRIKE)
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=50,
            swing_queue=SwingQueueOp.CLEAVE,
            raw_sink_order=(
                RawSink("autoattack", "MPStartAttack", "START", "Cat.lua:1"),
                RawSink(
                    "swing_queue",
                    "MPCastWithNampower",
                    "顺劈斩",
                    "Cat.lua:2",
                ),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(initial)

        execution = execute_ordered_sinks_v2(bridge, decision, initial)
        queue_event = execution["sink_events"][1]

        self.assertEqual(queue_event["client_acceptance"]["status"], "ACCEPTED")
        self.assertEqual(queue_event["decision_consumption"]["status"], "NOT_CONSUMED")
        self.assertEqual(
            queue_event["queue_transition"],
            {
                "kind": "REPLACED",
                "before": "HEROIC_STRIKE",
                "requested": "CLEAVE",
                "after": "CLEAVE",
                "cancel_requested": False,
            },
        )
        self.assertEqual(bridge.calls[-1], ("wait", 50))

    def test_unknown_operation_preflights_entire_decision_without_mutation(self) -> None:
        sinks = (
            RawSink("autoattack", "MPStartAttack", "START", "Cat.lua:1"),
            RawSink("gcd", "RunMacroText", "嗜血", "Cat.lua:2"),
        )
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            raw_sink_order=sinks,
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state())

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertTrue(execution["execution_blocked"])
        self.assertEqual(bridge.calls, [])
        self.assertEqual(len(execution["sink_events"]), 2)
        self.assertFalse(execution["sink_events"][1]["operation_contract"]["recognized"])
        self.assertTrue(
            any("unsupported_operation:gcd/RunMacroText" in item for item in execution["nonfaithful_reasons"])
        )
        self.assertEqual(
            {event["simulator_submission"]["status"] for event in execution["sink_events"]},
            {"NOT_SUBMITTED_FAIL_CLOSED"},
        )

    def test_stop_cast_then_gcd_uses_two_independent_acceptance_records(self) -> None:
        initial = _state(casting=True)
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=EXECUTE,
            wait_ms=None,
            cast_control=CastControl.STOP_CAST,
            raw_sink_order=(
                RawSink("cast_control", "SpellStopCasting", None, "Cat.lua:1"),
                RawSink("gcd", "CastSpellByName", "斩杀", "Cat.lua:2"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(initial)

        execution = execute_ordered_sinks_v2(bridge, decision, initial)

        self.assertEqual([name for name, _ in bridge.calls], ["stop_cast", "act"])
        first, second = execution["sink_events"]
        self.assertEqual(first["client_acceptance"]["status"], "ACCEPTED")
        self.assertFalse(first["decision_consumption"]["consumes_decision"])
        self.assertEqual(second["client_acceptance"]["status"], "ACCEPTED")
        self.assertTrue(second["decision_consumption"]["consumes_decision"])
        self.assertEqual(second["server_result"]["status"], "PENDING_OR_NOT_EXPOSED")
        self.assertIsNone(second["server_result"]["damage_or_miss_result"])

    def test_missing_stop_cast_control_blocks_following_sink_when_casting(self) -> None:
        initial = _state(casting=True)
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=EXECUTE,
            wait_ms=None,
            cast_control=CastControl.STOP_CAST,
            raw_sink_order=(
                RawSink("cast_control", "SpellStopCasting", None, "Cat.lua:1"),
                RawSink("gcd", "CastSpellByName", "斩杀", "Cat.lua:2"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(initial)
        bridge.stop_cast = None  # type: ignore[method-assign]

        execution = execute_ordered_sinks_v2(bridge, decision, initial)

        self.assertTrue(execution["execution_blocked"])
        self.assertEqual(bridge.calls, [])
        self.assertEqual(
            execution["sink_events"][1]["simulator_submission"]["status"],
            "NOT_SUBMITTED_FAIL_CLOSED",
        )
        self.assertIn(
            "cast_control:bridge_has_no_stop_cast_control_while_casting",
            execution["nonfaithful_reasons"],
        )

    def test_declared_known_noop_is_rejected_then_source_wait_is_submitted(self) -> None:
        sink = RawSink(
            "gcd", "QueueSpellByName", "嗜血", "Contra.lua:32062"
        )
        decision = ExpertDecision(
            provenance=_provenance("contra.deployed.fury.raid_a.v2"),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=100,
            raw_sink_order=(
                RawSink("autoattack", "Contra.StartAttack", "START", "Contra.lua:1"),
                sink,
            ),
            eligible_for_independent_vote=True,
            metadata={
                "raw_gcd_calls": [BLOODTHIRST],
                "known_noop_source_gcd_attempts": [
                    {
                        "action": BLOODTHIRST,
                        "operation": sink.operation,
                        "source_ref": sink.source_ref,
                        "reason": "NP_QueueSpellsOnCooldown=0",
                    }
                ],
            },
        )
        ref = ACTION_KEY_TO_REF[BLOODTHIRST]
        bridge = _FakeBridge(_state(), illegal={ref})

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertEqual([name for name, _ in bridge.calls], ["start_attack", "act", "wait"])
        self.assertEqual(
            execution["sink_events"][1]["client_acceptance"]["status"],
            "REJECTED_SOURCE_DECLARED_NOOP",
        )
        self.assertTrue(execution["ordered_projection_faithful"])
        self.assertEqual(execution["wait_event"]["requested_wait_ms"], 100)
        self.assertFalse(execution["wait_event"]["fallback"])

    def test_absent_source_action_is_nonfaithful_and_blocks_later_mutation(self) -> None:
        ref = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            raw_sink_order=(
                RawSink("gcd", "CastSpellByName", "嗜血", "Cat.lua:1"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state(), absent={ref})

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertTrue(execution["execution_blocked"])
        self.assertFalse(execution["ordered_projection_faithful"])
        self.assertEqual(bridge.calls, [])
        self.assertEqual(
            execution["sink_events"][0]["simulator_submission"]["status"],
            "NOT_SUBMITTED_ACTION_ABSENT_FROM_SPELLBOOK",
        )
        self.assertIn(
            "raw_sink[1]:action_absent_from_simulator_spellbook",
            execution["nonfaithful_reasons"],
        )

    def test_undeclared_client_rejection_is_nonfaithful(self) -> None:
        ref = ACTION_KEY_TO_REF[BLOODTHIRST]
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            raw_sink_order=(
                RawSink("gcd", "CastSpellByName", "嗜血", "Cat.lua:1"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state(), illegal={ref})

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertEqual(
            execution["sink_events"][0]["client_acceptance"]["status"],
            "REJECTED_UNCLASSIFIED",
        )
        self.assertFalse(execution["ordered_projection_faithful"])
        self.assertIn(
            "raw_sink[1]:unclassified_client_rejection",
            execution["nonfaithful_reasons"],
        )

    def test_normalized_lane_mismatch_fails_before_first_sink(self) -> None:
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=EXECUTE,
            wait_ms=None,
            raw_sink_order=(
                RawSink("gcd", "CastSpellByName", "嗜血", "Cat.lua:1"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state())

        execution = execute_ordered_sinks_v2(bridge, decision, _state())

        self.assertTrue(execution["execution_blocked"])
        self.assertEqual(bridge.calls, [])
        self.assertIn(
            "proposal:normalized_gcd_is_not_last_state_effecting_raw_sink",
            execution["nonfaithful_reasons"],
        )

    def test_queue_cancel_is_explicitly_unsupported_not_silently_kept(self) -> None:
        decision = ExpertDecision(
            provenance=_provenance(),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=100,
            swing_queue=SwingQueueOp.CANCEL,
            raw_sink_order=(
                RawSink("autoattack", "MPStartAttack", "START", "Cat.lua:1"),
            ),
            eligible_for_independent_vote=True,
        )
        bridge = _FakeBridge(_state(queue=SwingQueueOp.HEROIC_STRIKE))

        execution = execute_ordered_sinks_v2(bridge, decision, bridge.current)

        self.assertTrue(execution["execution_blocked"])
        self.assertEqual(bridge.calls, [])
        self.assertIn(
            "proposal:queue_cancel_has_no_audited_raw_sink_operation",
            execution["nonfaithful_reasons"],
        )
        self.assertFalse(execution["fallback"]["used"])


if __name__ == "__main__":
    unittest.main()
