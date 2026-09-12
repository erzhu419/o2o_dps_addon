from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps import historical_behavior_clone_simulator_adapter_v1 as adapter_v1
from o2o_dps import historical_behavior_clone_v1 as clone_v1
from o2o_dps.expert_policy import StanceOp, SwingQueueOp, WAIT_ACTION
from o2o_dps.sim_bridge import ActResult, AvailableAction


def _model(
    episodes: list[list[clone_v1.TrainingDecisionV1]],
) -> dict[str, object]:
    return clone_v1.compile_training_episodes_v1(episodes)


def _decision(elapsed_ms: int, action_key: str) -> clone_v1.TrainingDecisionV1:
    return clone_v1.TrainingDecisionV1(
        elapsed_ms=elapsed_ms,
        action_key=action_key,
        target_role="CURRENT_ENEMY",
    )


def _row(
    action_key: str, *, legal: bool = True, index: int = 0
) -> AvailableAction:
    binding = adapter_v1.ACTION_SINK_BINDINGS_V1[action_key]
    return AvailableAction(
        index=index,
        action=binding.action_ref,
        label=action_key,
        legal=legal,
        ready_in_ms=0 if legal else 1000,
        triggers_gcd=binding.triggers_gcd,
    )


def _observation(
    state: dict[str, object],
    adapter: adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1,
) -> dict[str, object]:
    last = adapter.runtime.last_action_key
    lane = clone_v1.ACTION_SPEC_BY_KEY[last].lane if last is not None else None
    queue = state.get("pending_queue_action")
    return {
        "schema": clone_v1.OBSERVATION_SCHEMA,
        "wave_elapsed_ms": state["time_ms"],
        "observed_target_count": 1,
        "observed_dead_target_count": 0,
        "background_dps": 1000.0,
        "actor_has_last_target": last is not None,
        "last_controllable_action": last,
        "last_controllable_lane": lane,
        "last_gcd_action_age_ms": None,
        "observed_stance": state.get("observed_stance"),
        "pending_queue_action": queue,
        "pending_queue_age_ms": 0 if queue is not None else None,
    }


def _frame(
    bridge: "_FakeBridge",
    adapter: adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1,
) -> adapter_v1.BehaviorCloneFrameV1:
    actions = tuple(bridge.actions())
    return adapter_v1.BehaviorCloneFrameV1(
        state=deepcopy(bridge.current),
        observation=_observation(bridge.current, adapter),
        available_actions=actions,
    )


class _FakeBridge:
    def __init__(self, rows: list[AvailableAction]) -> None:
        self.rows = rows
        self.current: dict[str, object] = {
            "time_ms": 0,
            "needs_input": True,
            "finished": False,
            "pending_queue_action": None,
            "observed_stance": None,
        }
        self.calls: list[tuple[str, object]] = []
        self.after_action_legal: dict[str, set[str]] = {}

    def actions(self) -> list[AvailableAction]:
        self.calls.append(("actions", None))
        return list(self.rows)

    def act(self, action):
        self.calls.append(("act", action))
        row = next(row for row in self.rows if row.action == action)
        casted = row.legal and bool(self.current["needs_input"])
        consumes = casted and row.triggers_gcd
        action_key = next(
            key
            for key, binding in adapter_v1.ACTION_SINK_BINDINGS_V1.items()
            if binding.action_ref == action
        )
        if casted and action_key in {
            "warrior.heroic_strike",
            "warrior.cleave",
        }:
            self.current["pending_queue_action"] = action_key
        if casted and action_key in adapter_v1.STANCE_BY_ACTION:
            self.current["observed_stance"] = action_key
        if casted and action_key in self.after_action_legal:
            legal_after = self.after_action_legal[action_key]
            self.rows = [
                AvailableAction(
                    index=value.index,
                    action=value.action,
                    label=value.label,
                    legal=value.label in legal_after,
                    ready_in_ms=0 if value.label in legal_after else 1000,
                    triggers_gcd=value.triggers_gcd,
                )
                for value in self.rows
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


class HistoricalBehaviorCloneSimulatorAdapterV1Tests(unittest.TestCase):
    def test_all_fifteen_actions_have_one_explicit_typed_sink_mapping(self) -> None:
        coverage = adapter_v1.action_sink_coverage_v1()
        self.assertEqual(coverage["action_count"], 15)
        self.assertEqual(
            {row["action_key"] for row in coverage["rows"]},
            set(clone_v1.ACTION_KEYS),
        )
        self.assertTrue(coverage["all_actions_mapped_or_not_applicable"])

        for action_key in clone_v1.ACTION_KEYS:
            with self.subTest(action_key=action_key):
                adapter = adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1(
                    _model([[_decision(0, action_key)]]), simulator_seed=1
                )
                bridge = _FakeBridge([_row(action_key)])
                decision = adapter.propose(_frame(bridge, adapter))
                binding = adapter_v1.ACTION_SINK_BINDINGS_V1[action_key]
                self.assertEqual(len(decision.raw_sink_order), 1)
                self.assertEqual(
                    decision.raw_sink_order[0].channel, binding.sink_channel
                )
                if binding.sink_channel == "gcd":
                    self.assertEqual(decision.gcd, action_key)
                elif binding.sink_channel == "stance":
                    self.assertEqual(decision.stance, binding.stance)
                elif binding.sink_channel == "off_gcd":
                    self.assertEqual(decision.off_gcd, (action_key,))
                else:
                    self.assertEqual(decision.swing_queue, binding.queue)
                execution = adapter_v1.execute_historical_behavior_clone_decision_v1(
                    adapter, bridge, decision, bridge.current
                )
                self.assertEqual(execution["client_acceptance"], "ACCEPTED")
                self.assertEqual(
                    execution["decision_consumed"], binding.triggers_gcd
                )
                self.assertFalse(
                    execution["normalized_wait_placeholder_submitted"]
                )

    def test_pending_proposal_is_stable_and_cannot_be_executed_twice(self) -> None:
        adapter = adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1(
            _model([[_decision(0, "warrior.bloodthirst")]]), simulator_seed=2
        )
        bridge = _FakeBridge([_row("warrior.bloodthirst")])
        frame = _frame(bridge, adapter)
        before_act_count = sum(name == "act" for name, _ in bridge.calls)

        first = adapter.propose(frame)
        second = adapter.propose(frame)
        self.assertIs(first, second)
        self.assertEqual(
            sum(name == "act" for name, _ in bridge.calls), before_act_count
        )
        adapter_v1.execute_historical_behavior_clone_decision_v1(
            adapter, bridge, first, bridge.current
        )
        with self.assertRaisesRegex(
            adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1Error,
            "current pending",
        ):
            adapter_v1.execute_historical_behavior_clone_decision_v1(
                adapter, bridge, first, bridge.current
            )
        self.assertEqual(sum(name == "act" for name, _ in bridge.calls), 1)

    def test_illegal_high_probability_mark_is_removed_before_sampling(self) -> None:
        model = _model(
            [[_decision(0, "warrior.bloodthirst")]] * 20
            + [[_decision(0, "warrior.whirlwind")]]
        )
        adapter = adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1(
            model, simulator_seed=3
        )
        bridge = _FakeBridge(
            [
                _row("warrior.bloodthirst", legal=False, index=0),
                _row("warrior.whirlwind", legal=True, index=1),
            ]
        )

        proposal = adapter.propose(_frame(bridge, adapter))

        self.assertEqual(proposal.gcd, "warrior.whirlwind")
        self.assertEqual(
            proposal.metadata["legal_action_status"]["warrior.bloodthirst"],
            "NOT_APPLICABLE_CURRENTLY_ILLEGAL",
        )
        self.assertEqual(sum(name == "act" for name, _ in bridge.calls), 0)

    def test_wait_delay_is_submitted_without_sampling_an_action(self) -> None:
        adapter = adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1(
            _model([[_decision(100, "warrior.bloodthirst")]]), simulator_seed=4
        )
        bridge = _FakeBridge([_row("warrior.bloodthirst")])
        proposal = adapter.propose(_frame(bridge, adapter))

        self.assertEqual(proposal.gcd, WAIT_ACTION)
        self.assertEqual(proposal.wait_ms, 100)
        execution = adapter_v1.execute_historical_behavior_clone_decision_v1(
            adapter, bridge, proposal, bridge.current
        )
        self.assertEqual(execution["kind"], "WAIT")
        self.assertEqual(execution["final_state"]["time_ms"], 100)
        self.assertEqual(sum(name == "act" for name, _ in bridge.calls), 0)
        self.assertEqual(sum(name == "wait" for name, _ in bridge.calls), 1)

    def test_epoch_preserves_zero_delay_off_gcd_then_gcd_sequence(self) -> None:
        model = _model(
            [
                [
                    _decision(0, "warrior.bloodrage"),
                    _decision(0, "warrior.bloodthirst"),
                ]
            ]
        )
        adapter = adapter_v1.HistoricalBehaviorCloneSimulatorAdapterV1(
            model, simulator_seed=5
        )
        bridge = _FakeBridge(
            [
                _row("warrior.bloodrage", legal=True, index=0),
                _row("warrior.bloodthirst", legal=False, index=1),
            ]
        )
        bridge.after_action_legal["warrior.bloodrage"] = {
            "warrior.bloodthirst"
        }

        epoch = adapter_v1.run_historical_behavior_clone_epoch_v1(
            adapter,
            bridge,
            bridge.current,
            lambda state, _available, current_adapter: _observation(
                dict(state), current_adapter
            ),
        )

        self.assertEqual(epoch["status"], "COMPLETE")
        self.assertEqual(epoch["substep_count"], 2)
        self.assertEqual(
            [
                step["typed_sink_binding"]["action_key"]
                for step in epoch["steps"]
            ],
            ["warrior.bloodrage", "warrior.bloodthirst"],
        )
        self.assertEqual(sum(name == "act" for name, _ in bridge.calls), 2)
        self.assertEqual(sum(name == "wait" for name, _ in bridge.calls), 0)

    def test_queue_and_stance_use_non_gcd_typed_lanes(self) -> None:
        self.assertEqual(
            adapter_v1.ACTION_SINK_BINDINGS_V1["warrior.heroic_strike"].queue,
            SwingQueueOp.HEROIC_STRIKE,
        )
        self.assertEqual(
            adapter_v1.ACTION_SINK_BINDINGS_V1["warrior.cleave"].queue,
            SwingQueueOp.CLEAVE,
        )
        self.assertEqual(
            adapter_v1.ACTION_SINK_BINDINGS_V1["warrior.battle_stance"].stance,
            StanceOp.BATTLE,
        )
        self.assertEqual(
            adapter_v1.ACTION_SINK_BINDINGS_V1["warrior.defensive_stance"].stance,
            StanceOp.DEFENSIVE,
        )
        self.assertEqual(
            adapter_v1.ACTION_SINK_BINDINGS_V1[
                "warrior.berserker_stance"
            ].stance,
            StanceOp.BERSERKER,
        )


if __name__ == "__main__":
    unittest.main()
