from __future__ import annotations

import copy
import unittest

from o2o_dps.expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_expert_adapters import FuryExpertState
from o2o_dps.fury_expert_closed_loop import (
    FuryClosedLoopError,
    SINGLETON_HEALTH_COMPLETION_MODE,
    benchmark_fury_expert_closed_loops,
    run_fury_expert_closed_loop,
)
from o2o_dps.sim_bridge import (
    ActResult,
    AvailableAction,
    CancelQueueResult,
)


BLOODTHIRST = "warrior.bloodthirst"
WHIRLWIND = "warrior.whirlwind"
BLOODRAGE = "warrior.bloodrage"
HEROIC_STRIKE = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]


def _request() -> dict[str, object]:
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "distanceFromTarget": 3,
                            "equipment": {"items": [{}, {}]},
                        }
                    ]
                }
            ]
        },
        "encounter": {
            "duration": 300,
            "targets": [{"level": 63, "name": "Test Boss"}],
        },
        "simOptions": {"iterations": 1},
    }


def _provenance(expert_id: str) -> ExpertProvenance:
    return ExpertProvenance(
        expert_id=expert_id,
        kind=ProvenanceKind.SOURCE_DERIVED,
        role=ExpertRole.DEPLOYED,
        authority_files=("test-policy.lua",),
    )


class _FeedbackAdapter:
    expert_id = "test.feedback"

    def __init__(self) -> None:
        self.states: list[FuryExpertState] = []

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        self.states.append(state)
        if state.last_cast_name != BLOODTHIRST:
            return ExpertDecision(
                provenance=_provenance(self.expert_id),
                valid=True,
                gcd=BLOODTHIRST,
                wait_ms=None,
                swing_queue=SwingQueueOp.HEROIC_STRIKE,
                off_gcd=(BLOODRAGE,),
                eligible_for_independent_vote=True,
            )
        return ExpertDecision(
            provenance=_provenance(self.expert_id),
            valid=True,
            gcd=WAIT_ACTION,
            wait_ms=100,
            eligible_for_independent_vote=True,
        )


class _ConstantAdapter:
    def __init__(
        self,
        expert_id: str,
        *,
        gcd: str = WAIT_ACTION,
        off_gcd: tuple[str, ...] = (),
        target: TargetOp = TargetOp.KEEP,
    ) -> None:
        self.expert_id = expert_id
        self.gcd = gcd
        self.off_gcd = off_gcd
        self.target = target

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        return ExpertDecision(
            provenance=_provenance(self.expert_id),
            valid=True,
            gcd=self.gcd,
            wait_ms=100 if self.gcd == WAIT_ACTION else None,
            off_gcd=self.off_gcd,
            target=self.target,
            eligible_for_independent_vote=True,
        )


class _DeclaredSourceNoopAdapter:
    expert_id = "test.declared-source-noop"

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        return ExpertDecision(
            provenance=_provenance(self.expert_id),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            raw_sink_order=(
                RawSink(
                    "gcd",
                    "QueueSpellByName",
                    "嗜血",
                    "test-policy.lua:10",
                ),
            ),
            eligible_for_independent_vote=True,
            metadata={"known_noop_retry_wait_ms": 100},
        )


class _DeclaredCat2WhirlwindNoopAdapter:
    expert_id = "test.declared-cat2-whirlwind-noop"

    def __init__(
        self,
        *,
        gcd: str = WHIRLWIND,
        operation: str = "Cat2.Cast",
        maximum_ready_in_ms_exclusive: int = 500,
    ) -> None:
        self.gcd = gcd
        self.operation = operation
        self.maximum = maximum_ready_in_ms_exclusive

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        return ExpertDecision(
            provenance=_provenance(self.expert_id),
            valid=True,
            gcd=self.gcd,
            wait_ms=None,
            raw_sink_order=(
                RawSink("gcd", self.operation, self.gcd, "Cat2-test.lua:10"),
            ),
            eligible_for_independent_vote=False,
            metadata={
                "source_api_noop_retry_contracts": [
                    {
                        "lane": "gcd",
                        "action": WHIRLWIND,
                        "operation": "Cat2.Cast",
                        "minimum_ready_in_ms_inclusive": 1,
                        "maximum_ready_in_ms_exclusive": self.maximum,
                        "retry_wait_ms": 100,
                    }
                ]
            },
        )


class _FakeBridge:
    def __init__(
        self,
        *,
        illegal: set[object] | None = None,
        illegal_ready_ms: int = 1500,
        trigger_overrides: dict[object, bool] | None = None,
    ) -> None:
        self.illegal = illegal or set()
        self.illegal_ready_ms = illegal_ready_ms
        self.trigger_overrides = trigger_overrides or {}
        self.calls: list[tuple[str, object]] = []
        self.duration_ms = 0
        self.time_ms = 0
        self.damage = 0.0
        self.needs_input = True
        self.finished = False
        self.pending_wait_ms = 0
        self.pending_gcd = None
        self.auras: list[dict[str, object]] = []
        self.loaded_request = None

    def load(self, request, seed):
        self.calls.append(("load", seed))
        self.loaded_request = copy.deepcopy(request)
        self.duration_ms = round(float(request["encounter"]["duration"]) * 1000)
        self.time_ms = 0
        self.damage = 0.0
        self.needs_input = True
        self.finished = False
        self.pending_wait_ms = 0
        self.pending_gcd = None
        self.auras = [
            {"label": "Battle Shout", "remaining_ms": 600_000, "stacks": 0},
            {"label": "Berserker Stance", "remaining_ms": 1, "stacks": 0},
        ]
        return self._state()

    def actions(self):
        refs = (
            ACTION_KEY_TO_REF[BLOODTHIRST],
            ACTION_KEY_TO_REF[WHIRLWIND],
            ACTION_KEY_TO_REF[BLOODRAGE],
            ACTION_KEY_TO_REF["warrior.death_wish"],
            ACTION_KEY_TO_REF["warrior.battle_shout"],
            ACTION_KEY_TO_REF["warrior.berserker_stance"],
            HEROIC_STRIKE,
            QUEUE_REFS[SwingQueueOp.CLEAVE],
        )
        rows = []
        for index, ref in enumerate(refs):
            default_trigger = ref in {
                ACTION_KEY_TO_REF[BLOODTHIRST],
                ACTION_KEY_TO_REF[WHIRLWIND],
                ACTION_KEY_TO_REF["warrior.battle_shout"],
            }
            rows.append(
                AvailableAction(
                    index=index,
                    action=ref,
                    label=f"action-{ref.spell_id}-{ref.tag}",
                    legal=self.needs_input and ref not in self.illegal,
                    ready_in_ms=self.illegal_ready_ms if ref in self.illegal else 0,
                    triggers_gcd=self.trigger_overrides.get(ref, default_trigger),
                )
            )
        return rows

    def act(self, action):
        self.calls.append(("act", action))
        row = next(row for row in self.actions() if row.action == action)
        if not row.legal:
            return ActResult(False, False, self.finished, self.needs_input, self._state())
        consumes = row.triggers_gcd
        if action == HEROIC_STRIKE:
            self.auras.append(
                {
                    "label": "HS/Cleave Queue Aura-Heroic Strike",
                    "action": {"spell_id": action.spell_id, "tag": 1},
                    "remaining_ms": 999_999,
                    "stacks": 0,
                }
            )
        if consumes:
            self.needs_input = False
            self.pending_gcd = action
        return ActResult(True, consumes, self.finished, self.needs_input, self._state())

    def cancel_queue(self):
        self.calls.append(("cancel_queue", None))
        before = len(self.auras)
        self.auras = [
            aura
            for aura in self.auras
            if not (
                isinstance(aura.get("action"), dict)
                and aura["action"].get("tag") == 1
            )
        ]
        canceled = len(self.auras) != before
        return CancelQueueResult(canceled, False, self.finished, self.needs_input, self._state())

    def wait(self, wait_ms):
        self.calls.append(("wait", wait_ms))
        self.pending_wait_ms = wait_ms
        self.needs_input = False
        return self._state()

    def advance(self):
        self.calls.append(("advance", None))
        amount = self.pending_wait_ms or 1500
        next_time = min(self.duration_ms, self.time_ms + amount)
        if self.pending_gcd is not None:
            if self.pending_gcd == ACTION_KEY_TO_REF[BLOODTHIRST]:
                self.damage += 100.0
            elif self.pending_gcd == ACTION_KEY_TO_REF[WHIRLWIND]:
                self.damage += 80.0
        else:
            self.damage += (next_time - self.time_ms) / 10.0
        self.time_ms = next_time
        self.pending_wait_ms = 0
        self.pending_gcd = None
        self.finished = self.time_ms >= self.duration_ms
        self.needs_input = not self.finished
        return self._state()

    def _state(self):
        return {
            "time_ms": self.time_ms,
            "remaining_ms": max(0, self.duration_ms - self.time_ms),
            "finished": self.finished,
            "needs_input": self.needs_input,
            "power": {"type": "Rage", "current": 100.0, "maximum": 100.0},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1000,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": 800,
            "current_cast": None,
            "target_health_known": False,
            "target_health_percent": 0.0,
            "execute_phase_20": False,
            "target_armor": 3000.0,
            "effective_target_armor": 3000.0,
            "damage_done": self.damage,
            "auras": copy.deepcopy(self.auras),
        }


class _HealthFakeBridge(_FakeBridge):
    def load(self, request, seed):
        state = super().load(request, seed)
        self.health_target = float(request["encounter"]["targets"][0]["stats"][34])
        return self._state()

    def advance(self):
        self.calls.append(("advance", None))
        amount = self.pending_wait_ms or 100
        self.time_ms += amount
        self.damage += 100.0
        self.pending_wait_ms = 0
        self.pending_gcd = None
        self.finished = self.damage >= self.health_target
        self.needs_input = not self.finished
        return self._state()

    def _state(self):
        state = super()._state()
        health_target = getattr(self, "health_target", 0.0)
        state.update(
            {
                "remaining_ms": 0,
                "target_health_known": True,
                "target_health_percent": 100.0,
                "encounter_damage_taken": self.damage,
                "encounter_health_target": health_target,
                "execute_phase_20": (
                    health_target > 0 and self.damage >= health_target * 0.8
                ),
            }
        )
        return state


class FuryExpertClosedLoopTests(unittest.TestCase):
    def test_rebuilds_feedback_state_and_executes_supported_lanes_in_loop(self):
        bridge = _FakeBridge()
        adapter = _FeedbackAdapter()
        request = _request()
        original = copy.deepcopy(request)

        result = run_fury_expert_closed_loop(
            bridge,
            request,
            adapter,
            seed=41,
            horizon_ms=1600,
        )

        self.assertEqual(request, original)
        self.assertEqual(
            bridge.loaded_request["encounter"]["durationVariation"], 0.0
        )
        self.assertEqual(result["decision_count"], 2)
        self.assertEqual(result["final_time_ms"], 1600)
        self.assertEqual(result["damage_delta"], 110.0)
        self.assertFalse(result["source_execution"])
        self.assertFalse(result["exact_lua_replay"])
        self.assertTrue(result["all_lane_projections_faithful"])
        self.assertEqual(adapter.states[1].last_cast_name, BLOODTHIRST)
        self.assertIs(adapter.states[1].queued_swing, SwingQueueOp.HEROIC_STRIKE)
        first_lanes = [row["lane"] for row in result["steps"][0]["commands"]]
        self.assertEqual(first_lanes, ["off_gcd", "swing_queue", "gcd"])

    def test_unsupported_target_lane_is_explicit_and_never_claimed_exact(self):
        bridge = _FakeBridge()
        adapter = _ConstantAdapter(
            "test.target",
            target=TargetOp.NEAREST_ENEMY,
        )
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            adapter,
            seed=7,
            horizon_ms=100,
        )

        self.assertEqual(result["omitted_lane_counts"], {"target": 1})
        self.assertFalse(result["all_lane_projections_faithful"])
        omission = result["steps"][0]["omitted_lanes"][0]
        self.assertEqual(omission["reason"], "bridge_has_no_target_selection_command")

    def test_off_gcd_contract_mismatch_is_not_executed(self):
        bloodrage = ACTION_KEY_TO_REF[BLOODRAGE]
        bridge = _FakeBridge(trigger_overrides={bloodrage: True})
        adapter = _ConstantAdapter(
            "test.offgcd",
            off_gcd=(BLOODRAGE,),
        )
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            adapter,
            seed=8,
            horizon_ms=100,
        )

        self.assertNotIn(("act", bloodrage), bridge.calls)
        omission = result["steps"][0]["omitted_lanes"][0]
        self.assertEqual(omission["lane"], "off_gcd")
        self.assertIn("triggers_gcd_contract_mismatch", omission["reason"])

    def test_illegal_gcd_is_recorded_then_fallback_wait_progresses(self):
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        bridge = _FakeBridge(illegal={bloodthirst})
        adapter = _ConstantAdapter("test.illegal", gcd=BLOODTHIRST)
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            adapter,
            seed=9,
            horizon_ms=100,
        )

        step = result["steps"][0]
        self.assertEqual(step["omitted_lanes"][0]["lane"], "gcd")
        self.assertEqual(step["commands"][-1]["lane"], "fallback_progress")
        self.assertEqual(result["final_time_ms"], 100)

    def test_declared_source_api_illegal_gcd_is_audited_noop_then_retry(self):
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        bridge = _FakeBridge(illegal={bloodthirst}, illegal_ready_ms=0)
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _DeclaredSourceNoopAdapter(),
            seed=10,
            horizon_ms=100,
        )

        step = result["steps"][0]
        self.assertEqual(result["omitted_lane_count"], 0)
        self.assertTrue(result["all_lane_projections_faithful"])
        self.assertTrue(result["configured_horizon_complete"])
        self.assertNotIn(("act", bloodthirst), bridge.calls)
        self.assertEqual(len(step["commands"]), 1)
        command = step["commands"][0]
        self.assertEqual(command["operation"], "source_api_noop_wait")
        self.assertEqual(command["status"], "known_noop")
        self.assertFalse(command["available"]["legal"])
        self.assertEqual(command["available"]["ready_in_ms"], 0)
        self.assertEqual(command["result"], {"wait_ms": 100})
        self.assertEqual(
            command["source_attempts"][0]["operation"], "QueueSpellByName"
        )

    def test_cat2_whirlwind_retry_window_is_audited_noop_without_fallback(self):
        whirlwind = ACTION_KEY_TO_REF[WHIRLWIND]
        bridge = _FakeBridge(illegal={whirlwind}, illegal_ready_ms=400)
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _DeclaredCat2WhirlwindNoopAdapter(),
            seed=11,
            horizon_ms=100,
        )

        step = result["steps"][0]
        self.assertEqual(result["omitted_lane_count"], 0)
        self.assertFalse(result["all_lane_projections_faithful"])
        self.assertEqual(
            result["nonfaithful_reason_counts"],
            {"gcd:source_api_noop_unknown_blocker_proxy": 1},
        )
        self.assertNotIn(("act", whirlwind), bridge.calls)
        self.assertEqual(len(step["commands"]), 1)
        self.assertEqual(step["commands"][0]["operation"], "source_api_noop_wait")
        self.assertEqual(step["commands"][0]["status"], "known_noop")
        self.assertEqual(
            step["commands"][0]["source_attempts"][0]["operation"],
            "Cat2.Cast",
        )
        self.assertNotIn(
            "fallback_progress:proposal_did_not_consume_decision",
            result["nonfaithful_reason_counts"],
        )

    def test_cat2_retry_contract_requires_a_gcd_triggering_spell_row(self):
        whirlwind = ACTION_KEY_TO_REF[WHIRLWIND]
        bridge = _FakeBridge(
            illegal={whirlwind},
            illegal_ready_ms=400,
            trigger_overrides={whirlwind: False},
        )
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _DeclaredCat2WhirlwindNoopAdapter(),
            seed=15,
            horizon_ms=100,
        )

        self.assertEqual(result["omitted_lane_counts"], {"gcd": 1})
        self.assertEqual(
            result["steps"][0]["commands"][-1]["lane"], "fallback_progress"
        )

    def test_cat2_retry_contract_does_not_hide_cooldown_at_boundary(self):
        whirlwind = ACTION_KEY_TO_REF[WHIRLWIND]
        bridge = _FakeBridge(illegal={whirlwind}, illegal_ready_ms=500)
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _DeclaredCat2WhirlwindNoopAdapter(),
            seed=12,
            horizon_ms=100,
        )

        self.assertEqual(result["omitted_lane_counts"], {"gcd": 1})
        self.assertEqual(
            result["steps"][0]["commands"][-1]["lane"], "fallback_progress"
        )
        self.assertNotIn(("act", whirlwind), bridge.calls)

    def test_cat2_retry_contract_does_not_hide_zero_ready_unknown_illegality(self):
        whirlwind = ACTION_KEY_TO_REF[WHIRLWIND]
        bridge = _FakeBridge(illegal={whirlwind}, illegal_ready_ms=0)
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _DeclaredCat2WhirlwindNoopAdapter(),
            seed=13,
            horizon_ms=100,
        )

        self.assertEqual(result["omitted_lane_counts"], {"gcd": 1})
        self.assertNotIn(("act", whirlwind), bridge.calls)

    def test_cat2_retry_contract_does_not_hide_wrong_action_or_operation(self):
        bloodthirst = ACTION_KEY_TO_REF[BLOODTHIRST]
        for adapter in (
            _DeclaredCat2WhirlwindNoopAdapter(gcd=BLOODTHIRST),
            _DeclaredCat2WhirlwindNoopAdapter(operation="CastSpellByName"),
        ):
            illegal_action = (
                bloodthirst
                if adapter.gcd == BLOODTHIRST
                else ACTION_KEY_TO_REF[WHIRLWIND]
            )
            bridge = _FakeBridge(illegal={illegal_action}, illegal_ready_ms=400)
            result = run_fury_expert_closed_loop(
                bridge,
                _request(),
                adapter,
                seed=14,
                horizon_ms=100,
            )
            self.assertEqual(result["omitted_lane_counts"], {"gcd": 1})
            self.assertNotIn(("act", illegal_action), bridge.calls)

    def test_matched_seed_benchmark_ranks_closed_loop_damage(self):
        bridge = _FakeBridge()
        result = benchmark_fury_expert_closed_loops(
            bridge,
            _request(),
            (
                _ConstantAdapter("test.bt", gcd=BLOODTHIRST),
                _ConstantAdapter("test.ww", gcd=WHIRLWIND),
            ),
            seeds=(1, 2),
            horizon_ms=3000,
            reference_expert_id="test.bt",
        )

        self.assertEqual(result["rollout_count"], 4)
        self.assertEqual(len(result["rollout_summaries"]), 4)
        self.assertNotIn("steps", result["rollout_summaries"][0])
        self.assertLessEqual(len(result["omission_examples"]), 20)
        self.assertEqual(result["ranking"][0]["expert_id"], "test.bt")
        self.assertEqual(result["ranking"][0]["mean_damage"], 200.0)
        self.assertEqual(result["ranking"][1]["mean_paired_damage_vs_reference"], -40.0)
        self.assertFalse(result["exact_lua_replay"])

    def test_health_mode_is_rejected_before_loading_bridge(self):
        bridge = _FakeBridge()
        request = _request()
        request["encounter"]["useHealth"] = True

        with self.assertRaisesRegex(FuryClosedLoopError, "useHealth=true"):
            run_fury_expert_closed_loop(
                bridge,
                request,
                _ConstantAdapter("test.health"),
                seed=1,
                horizon_ms=1000,
            )
        self.assertEqual(bridge.calls, [])

    def test_singleton_health_mode_uses_global_progress_and_actual_elapsed_dps(self):
        bridge = _HealthFakeBridge()
        adapter = _FeedbackAdapter()
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 35
        request["encounter"]["targets"][0]["stats"][34] = 400.0

        result = run_fury_expert_closed_loop(
            bridge,
            request,
            adapter,
            seed=2,
            horizon_ms=1000,
            clamp_encounter_to_horizon=False,
            completion_mode=SINGLETON_HEALTH_COMPLETION_MODE,
        )

        self.assertTrue(result["completion_criterion_met"])
        self.assertTrue(result["configured_horizon_complete"])
        self.assertEqual(result["completion_mode"], "singleton_health")
        self.assertEqual(result["final_time_ms"], 400)
        self.assertEqual(result["dps_denominator_ms"], 400)
        self.assertEqual(result["dps"], 1000.0)
        self.assertEqual(
            [state.target_health_pct for state in adapter.states],
            [100.0, 75.0, 50.0, 25.0],
        )
        self.assertTrue(
            all(step["simulator_state_before"]["target_health_percent"] == 100.0 for step in result["steps"])
        )

    def test_singleton_health_watchdog_incompletion_is_explicit(self):
        bridge = _HealthFakeBridge()
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 35
        request["encounter"]["targets"][0]["stats"][34] = 1000.0

        result = run_fury_expert_closed_loop(
            bridge,
            request,
            _ConstantAdapter("test.health-timeout"),
            seed=3,
            horizon_ms=200,
            clamp_encounter_to_horizon=False,
            completion_mode=SINGLETON_HEALTH_COMPLETION_MODE,
        )

        self.assertFalse(result["finished"])
        self.assertFalse(result["completion_criterion_met"])
        self.assertFalse(result["configured_horizon_complete"])

    def test_transition_sink_streams_without_retaining_steps(self):
        bridge = _FakeBridge()
        streamed = []
        result = run_fury_expert_closed_loop(
            bridge,
            _request(),
            _ConstantAdapter("test.stream"),
            seed=12,
            horizon_ms=200,
            transition_sink=streamed.append,
            retain_steps=False,
        )

        self.assertEqual(result["decision_count"], 2)
        self.assertEqual(len(streamed), 2)
        self.assertNotIn("steps", result)
        self.assertFalse(result["steps_retained"])
        self.assertEqual(streamed[0]["decision_index"], 0)
        self.assertEqual(streamed[1]["decision_index"], 1)


if __name__ == "__main__":
    unittest.main()
