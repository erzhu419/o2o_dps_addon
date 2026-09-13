from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest
from unittest import mock

from o2o_dps import historical_behavior_clone_simulator_adapter_v2 as adapter_v2
from o2o_dps import historical_behavior_clone_v2 as clone_v2
from o2o_dps.expert_policy import SwingQueueOp, WAIT_ACTION
from o2o_dps.sim_bridge import ActResult, AvailableAction
from tests.test_historical_behavior_clone_v2 import (
    _binding,
    _prototype,
    _records,
)


def _model(action_key: str = "warrior.bloodthirst") -> dict[str, object]:
    rows = deepcopy(_records())
    lane = clone_v2.ACTION_SPEC_BY_KEY[action_key].lane
    for row in rows:
        if row["record_type"] == "weighted_server_observed_start":
            row["observed_start"]["action_key"] = action_key
            row["observed_start"]["action_lane"] = lane
            for recent in row["strict_prefix_state"][
                "recent_direct_action_observations"
            ]:
                recent["action_key"] = action_key
        else:
            preceding = row["right_censored_tail"][
                "preceding_recognized_controllable_start"
            ]
            preceding["action_key"] = action_key
            preceding["action_lane"] = lane
    return clone_v2.compile_weighted_prototype_v2(
        _prototype(), rows, source_binding=_binding()
    )


def _receipt(model: dict[str, object]) -> dict[str, object]:
    return adapter_v2.exact_validation_receipt_v2(model)


def _readdress(model: dict[str, object]) -> None:
    core = deepcopy(model)
    core.pop("content_address", None)
    payload = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    model["content_address"]["sha256"] = hashlib.sha256(payload).hexdigest()


def _row(
    action_key: str, *, legal: bool = True, ready_in_ms: int = 0, index: int = 0
) -> AvailableAction:
    binding = adapter_v2.ACTION_SINK_BINDINGS_V2[action_key]
    return AvailableAction(
        index=index,
        action=binding.action_ref,
        label=action_key,
        legal=legal,
        ready_in_ms=ready_in_ms,
        triggers_gcd=binding.triggers_gcd,
    )


class _FakeBridge:
    def __init__(self, rows: list[AvailableAction]) -> None:
        self.rows = rows
        self.current: dict[str, object] = {
            "time_ms": 0,
            "needs_input": True,
            "finished": False,
        }
        self.calls: list[tuple[str, object]] = []

    def actions(self) -> list[AvailableAction]:
        self.calls.append(("actions", None))
        return list(self.rows)

    def act(self, action, *, attempt_id=None):
        self.calls.append(("act", (action, attempt_id)))
        row = next(value for value in self.rows if value.action == action)
        casted = row.legal and bool(self.current["needs_input"])
        consumes = casted and row.triggers_gcd
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


def _observation(
    bridge: _FakeBridge,
    adapter: adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2,
) -> dict[str, object]:
    last = adapter.runtime.last_action_key
    lane = clone_v2.ACTION_SPEC_BY_KEY[last].lane if last is not None else None
    return {
        "schema": adapter_v2.OBSERVATION_SCHEMA,
        "simulator_time_ms": bridge.current["time_ms"],
        "scenario_elapsed_ms": int(bridge.current["time_ms"]),
        "observed_target_count": 1,
        "observed_dead_target_count": 0,
        "actor_has_last_target": last is not None,
        "last_controllable_action": last,
        "last_controllable_lane": lane,
        "last_gcd_action_age_ms": None,
        "observed_stance": None,
        "prefix_observation_status": "OBSERVED_FROM_WAVE_ANCHOR",
    }


def _frame(
    bridge: _FakeBridge,
    adapter: adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2,
) -> adapter_v2.BehaviorCloneFrameV2:
    return adapter_v2.BehaviorCloneFrameV2(
        state=deepcopy(bridge.current),
        observation=_observation(bridge, adapter),
        available_actions=tuple(bridge.actions()),
    )


def _advance_initial_clock(
    bridge: _FakeBridge,
    adapter: adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2,
) -> None:
    decision = adapter.propose(_frame(bridge, adapter))
    assert decision.gcd == WAIT_ACTION
    adapter_v2.execute_historical_behavior_clone_decision_v2(
        adapter, bridge, decision, bridge.current
    )
    bridge.current["needs_input"] = True


class HistoricalBehaviorCloneSimulatorAdapterV2Tests(unittest.TestCase):
    def test_runtime_never_decodes_exact_fractions_and_binds_content(self) -> None:
        model = _model()
        receipt = _receipt(model)
        with mock.patch.object(
            clone_v2,
            "_fraction_from_wire",
            side_effect=AssertionError("runtime decoded an exact fraction"),
        ):
            adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
                model,
                expected_model_binding=receipt,
                simulator_seed=1,
                start_ms=0,
                horizon_ms=2000,
            )
            self.assertEqual("fixture_prototype", adapter.prototype_id)
            self.assertEqual(
                clone_v2.RUNTIME_PROJECTION_SCHEMA,
                adapter_v2.runtime_model_binding_v2(
                    model, expected_model_binding=receipt
                )[
                    "runtime_projection_schema"
                ],
            )

        tampered = deepcopy(model)
        tampered["runtime_bounded_float_projection"]["mark_global_weights"][
            "warrior.bloodthirst"
        ] = 0.5
        _readdress(tampered)
        with self.assertRaisesRegex(
            adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2Error,
            "differs from the expected exact validation receipt",
        ):
            adapter_v2.runtime_model_binding_v2(
                tampered, expected_model_binding=receipt
            )

    def test_no_smoothing_cannot_create_an_unsupported_legal_action(self) -> None:
        model = _model()
        receipt = _receipt(model)
        adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
            model,
            expected_model_binding=receipt,
            simulator_seed=2,
            start_ms=0,
            horizon_ms=2000,
        )
        bridge = _FakeBridge(
            [
                _row(
                    "warrior.bloodthirst",
                    legal=False,
                    ready_in_ms=750,
                    index=0,
                ),
                _row("warrior.whirlwind", legal=True, index=1),
            ]
        )
        _advance_initial_clock(bridge, adapter)

        predicted = adapter_v2.predict_mark_distribution_v2(
            model,
            _observation(bridge, adapter),
            ["warrior.bloodthirst", "warrior.whirlwind"],
            expected_model_binding=receipt,
        )
        self.assertEqual({"warrior.bloodthirst": 1.0}, predicted["probabilities"])
        self.assertEqual(0.0, predicted["mark_smoothing_mass"])

        decision = adapter.propose(_frame(bridge, adapter))
        self.assertEqual(WAIT_ACTION, decision.gcd)
        self.assertEqual(750, decision.wait_ms)
        self.assertEqual(
            "NO_LEGAL_SOURCE_SUPPORTED_ACTION",
            decision.metadata["runtime_proposal"]["reason"],
        )
        self.assertEqual(0, sum(name == "act" for name, _ in bridge.calls))

    def test_residual_survival_is_explicit_no_decision_to_boundary(self) -> None:
        model = _model()
        receipt = _receipt(model)
        adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
            model,
            expected_model_binding=receipt,
            simulator_seed=1,
            start_ms=0,
            horizon_ms=2000,
        )
        bridge = _FakeBridge([_row("warrior.bloodthirst")])
        _advance_initial_clock(bridge, adapter)

        action = adapter.propose(_frame(bridge, adapter))
        executed = adapter_v2.execute_historical_behavior_clone_decision_v2(
            adapter,
            bridge,
            action,
            bridge.current,
            attempt_id="fixture:gcd",
        )
        self.assertEqual("ACCEPTED", executed["client_acceptance"])
        self.assertEqual(
            "RESIDUAL_PRODUCT_LIMIT_SURVIVAL",
            executed["next_delay_outcome"]["cause"],
        )
        self.assertFalse(
            executed["next_delay_outcome"]["converted_to_action_or_finite_delay"]
        )

        bridge.current["needs_input"] = True
        terminal_wait = adapter.propose(_frame(bridge, adapter))
        self.assertEqual(WAIT_ACTION, terminal_wait.gcd)
        self.assertEqual(1900, terminal_wait.wait_ms)
        self.assertEqual(
            "WAIT_TO_BOUNDARY",
            terminal_wait.metadata["runtime_proposal"]["kind"],
        )
        receipt = adapter_v2.execute_historical_behavior_clone_decision_v2(
            adapter, bridge, terminal_wait, bridge.current
        )
        self.assertTrue(receipt["no_decision"])
        self.assertTrue(receipt["residual_survival_outcome"])
        self.assertEqual(2000, receipt["final_state"]["time_ms"])
        self.assertEqual(1, sum(name == "act" for name, _ in bridge.calls))

    def test_state_only_gcd_actions_do_not_receive_server_result_attempt_ids(self) -> None:
        for action_key in ("warrior.battle_shout", "warrior.death_wish"):
            with self.subTest(action_key=action_key):
                model = _model(action_key)
                exact = _receipt(model)
                adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
                    model,
                    expected_model_binding=exact,
                    simulator_seed=4,
                    start_ms=0,
                    horizon_ms=2000,
                )
                bridge = _FakeBridge([_row(action_key)])
                _advance_initial_clock(bridge, adapter)
                epoch = adapter_v2.run_historical_behavior_clone_epoch_v2(
                    adapter,
                    bridge,
                    bridge.current,
                    lambda _state, _available, current: _observation(
                        bridge, current
                    ),
                    attempt_id_prefix=f"fixture:{action_key}",
                )
                action_step = epoch["steps"][0]
                self.assertEqual("ACTION", action_step["kind"])
                self.assertIsNone(action_step["attempt_id"])
                act_payload = next(
                    payload for name, payload in bridge.calls if name == "act"
                )
                self.assertIsNone(act_payload[1])

    def test_waits_are_owned_by_the_issuing_adapter_instance(self) -> None:
        model = _model()
        receipt = _receipt(model)
        adapters = [
            adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
                model,
                expected_model_binding=receipt,
                simulator_seed=1,
                start_ms=0,
                horizon_ms=2000,
            )
            for _ in range(2)
        ]
        bridges = [
            _FakeBridge([_row("warrior.bloodthirst")]) for _ in range(2)
        ]

        initial_waits = [
            adapter.propose(_frame(bridge, adapter))
            for adapter, bridge in zip(adapters, bridges, strict=True)
        ]
        self.assertEqual(
            "WAIT", initial_waits[0].metadata["runtime_proposal"]["kind"]
        )
        with self.assertRaisesRegex(
            adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2Error,
            "current pending proposal",
        ):
            adapter_v2.execute_historical_behavior_clone_decision_v2(
                adapters[1], bridges[1], initial_waits[0], bridges[1].current
            )
        self.assertEqual(0, len(bridges[1].calls) - 1)

        for adapter, bridge, decision in zip(
            adapters, bridges, initial_waits, strict=True
        ):
            adapter_v2.execute_historical_behavior_clone_decision_v2(
                adapter, bridge, decision, bridge.current
            )
            bridge.current["needs_input"] = True
            action = adapter.propose(_frame(bridge, adapter))
            adapter_v2.execute_historical_behavior_clone_decision_v2(
                adapter, bridge, action, bridge.current
            )
            bridge.current["needs_input"] = True

        terminal_waits = [
            adapter.propose(_frame(bridge, adapter))
            for adapter, bridge in zip(adapters, bridges, strict=True)
        ]
        self.assertEqual(
            "WAIT_TO_BOUNDARY",
            terminal_waits[0].metadata["runtime_proposal"]["kind"],
        )
        wait_call_count = sum(name == "wait" for name, _ in bridges[1].calls)
        with self.assertRaisesRegex(
            adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2Error,
            "current pending proposal",
        ):
            adapter_v2.execute_historical_behavior_clone_decision_v2(
                adapters[1], bridges[1], terminal_waits[0], bridges[1].current
            )
        self.assertEqual(
            wait_call_count,
            sum(name == "wait" for name, _ in bridges[1].calls),
        )
        adapter_v2.execute_historical_behavior_clone_decision_v2(
            adapters[1], bridges[1], terminal_waits[1], bridges[1].current
        )

    def test_cleave_is_typed_queue_proxy_not_observed_client_intent(self) -> None:
        model = _model("warrior.cleave")
        receipt = _receipt(model)
        adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
            model,
            expected_model_binding=receipt,
            simulator_seed=4,
            start_ms=0,
            horizon_ms=2000,
        )
        bridge = _FakeBridge([_row("warrior.cleave")])
        _advance_initial_clock(bridge, adapter)

        decision = adapter.propose(_frame(bridge, adapter))
        self.assertEqual(SwingQueueOp.CLEAVE, decision.swing_queue)
        self.assertEqual(
            "QUEUE_REQUEST_AT_SERVER_START_PROXY_TIME_CLIENT_INTENT_UNKNOWN",
            decision.metadata["queue_proxy_semantics"],
        )
        self.assertFalse(
            decision.metadata["runtime_proposal"]["client_queue_intent_observed"]
        )
        receipt = adapter_v2.execute_historical_behavior_clone_decision_v2(
            adapter, bridge, decision, bridge.current
        )
        self.assertEqual("swing_queue", receipt["typed_sink_binding"]["sink_channel"])
        self.assertFalse(receipt["decision_consumed"])
        self.assertFalse(receipt["comparison_authorized"])


if __name__ == "__main__":
    unittest.main()
