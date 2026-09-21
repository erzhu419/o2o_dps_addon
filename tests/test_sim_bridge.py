from __future__ import annotations

from copy import deepcopy
import io
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.sim_bridge import (
    ActionRef,
    AvailableAction,
    BackgroundDamageEventV1,
    CancelQueueResult,
    DynamicCandidateDamageReceiptBatchV1,
    DynamicDamageReceiptBatchV1,
    DynamicLoadResultV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
    SetTargetResult,
    SimBridgeCommandError,
    SimBridgeProcessError,
    SimBridgeProtocolError,
    SimulatorBridge,
)


class _ResponseStream:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def readline(self) -> str:
        if not self.lines:
            return ""
        return self.lines.pop(0)

    def close(self) -> None:
        self.closed = True


class _RequestStream:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.closed = False

    def write(self, text: str) -> int:
        request = json.loads(text)
        self.process.requests.append(request)
        response = self.process.handler(request)
        if isinstance(response, str):
            line = response
        else:
            line = json.dumps(response)
        self.process.stdout.lines.append(line + "\n")
        if request["command"] == "close":
            self.process.return_code = 0
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, handler, stderr: str = "") -> None:
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.return_code: int | None = None
        self.stdout = _ResponseStream()
        self.stdin = _RequestStream(self)
        self.stderr = io.StringIO(stderr)
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            self.return_code = 0
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.killed = True
        self.return_code = 1


def _state(**changes: object) -> dict[str, object]:
    state: dict[str, object] = {
        "time_ms": 0,
        "remaining_ms": 60000,
        "finished": False,
        "needs_input": True,
        "damage_done": 0.0,
    }
    state.update(changes)
    dynamic = state.get("dynamic_team_background")
    if isinstance(dynamic, dict):
        targets = dynamic.get("targets")
        if isinstance(targets, list):
            state.setdefault("total_target_count", len(targets))
            state.setdefault(
                "num_targets",
                sum(
                    1
                    for target in targets
                    if isinstance(target, dict) and target.get("dead") is False
                ),
            )
            state.setdefault("target_index", 0)
    return state


def _dynamic_state(
    config: DynamicTeamBackgroundConfigV1,
    generation: int,
    *,
    config_digest: str | None = None,
) -> dict[str, object]:
    return {
        "schema": "o2o_dynamic_team_background/v1",
        "config_digest": config.content_sha256 if config_digest is None else config_digest,
        "environment_generation": generation,
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
        "retarget_required": False,
        "simulated_damage_applied": 0.0,
        "background_damage_applied": 0.0,
        "combined_damage_applied": 0.0,
        "background_events_processed": 0,
        "background_events_total": len(config.background_damage_events),
        "background_events_canceled": 0,
        "candidate_events_processed": 0,
        "candidate_events_canceled": 0,
        "damage_applications_total": 0,
        "targets": [
            {
                "target_index": target.target_index,
                "initial_health": target.health,
                "current_health": target.health,
                "dead": False,
                "simulated_damage_applied": 0.0,
                "background_damage_applied": 0.0,
            }
            for target in config.target_health
        ],
    }


class SimulatorBridgeTests(unittest.TestCase):
    def test_available_action_preserves_native_result_bearing_metadata(self) -> None:
        wire = {
            "index": 4,
            "action": {"spell_id": 23894},
            "label": "SpellID:23894",
            "legal": True,
            "ready_in_ms": 0,
            "cooldown_duration_ms": 90_000,
            "triggers_gcd": True,
            "result_bearing": True,
        }
        parsed = AvailableAction.from_wire(wire)
        self.assertTrue(parsed.result_bearing)
        self.assertEqual(parsed.cooldown_duration_ms, 90_000)
        legacy = dict(wire)
        legacy.pop("result_bearing")
        legacy.pop("cooldown_duration_ms")
        self.assertFalse(AvailableAction.from_wire(legacy).result_bearing)
        self.assertEqual(AvailableAction.from_wire(legacy).cooldown_duration_ms, 0)
        with self.assertRaisesRegex(SimBridgeProtocolError, "result_bearing"):
            AvailableAction.from_wire({**wire, "result_bearing": "yes"})
        with self.assertRaisesRegex(SimBridgeProtocolError, "cooldown_duration_ms"):
            AvailableAction.from_wire({**wire, "cooldown_duration_ms": -1})

    def test_state_preserves_warrior_control_fields(self) -> None:
        warrior_state = _state(
            autoattack_active=True,
            stance="BERSERKER",
            swing_queue={"kind": "HEROIC_STRIKE", "status": "PENDING"},
        )
        process = _FakeProcess(
            lambda request: {
                "ok": True,
                "command": request["command"],
                "state": warrior_state,
            }
        )
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            with SimulatorBridge("o2obridge.exe") as bridge:
                exported = bridge.state()

        self.assertEqual(exported, warrior_state)
        self.assertTrue(exported["autoattack_active"])
        self.assertEqual(exported["stance"], "BERSERKER")
        self.assertEqual(
            exported["swing_queue"],
            {"kind": "HEROIC_STRIKE", "status": "PENDING"},
        )

    def test_close_releases_both_process_output_pipes(self) -> None:
        process = _FakeProcess(
            lambda request: {"ok": True, "command": request["command"]}
        )
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            bridge.close()
            bridge.close()
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_dynamic_load_is_content_bound_typed_and_generation_scoped(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(
                DynamicTargetHealthV1(target_index=0, health=100.0),
                DynamicTargetHealthV1(target_index=1, health=250.5),
            ),
            background_damage_events=(
                BackgroundDamageEventV1(
                    schedule_index=0,
                    time_ms=0,
                    target_index=0,
                    event_id="wave-1:event-0",
                    damage=40.0,
                ),
                BackgroundDamageEventV1(
                    schedule_index=1,
                    time_ms=20,
                    target_index=1,
                    event_id="wave-1:event-1",
                    damage=12.25,
                ),
            ),
            retarget_mode="NEXT_ALIVE_CYCLIC",
        )
        self.assertEqual(
            config.content_sha256,
            "f1656786047ebf1559d5749d64a8297ce37549865aac94d536eea40e91b99d20",
        )

        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {"ok": True, "command": command}
            if command == "load_dynamic_v1":
                dynamic = request["dynamic"]
                assert isinstance(dynamic, dict)
                digest = dynamic["content_sha256"]
                response.update(
                    {
                        "environment_generation": 4,
                        "dynamic_load": {
                            "schema": "o2o_dynamic_team_background/v1",
                            "config_digest": digest,
                            "environment_generation": 4,
                            "target_count": 2,
                            "background_event_count": 2,
                            "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                            "retarget_mode": "NEXT_ALIVE_CYCLIC",
                        },
                        "state": _state(
                            target_index=0,
                            num_targets=2,
                            encounter_damage_taken=40.0,
                            encounter_health_target=350.5,
                            dynamic_team_background={
                                "schema": "o2o_dynamic_team_background/v1",
                                "config_digest": digest,
                                "environment_generation": 4,
                                "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                                "retarget_mode": "NEXT_ALIVE_CYCLIC",
                                "retarget_required": False,
                                "simulated_damage_applied": 0.0,
                                "background_damage_applied": 40.0,
                                "combined_damage_applied": 40.0,
                                "background_events_processed": 1,
                                "background_events_total": 2,
                                "background_events_canceled": 0,
                                "candidate_events_processed": 0,
                                "candidate_events_canceled": 0,
                                "damage_applications_total": 1,
                                "targets": [
                                    {
                                        "target_index": 0,
                                        "initial_health": 100.0,
                                        "current_health": 60.0,
                                        "dead": False,
                                        "simulated_damage_applied": 0.0,
                                        "background_damage_applied": 40.0,
                                    },
                                    {
                                        "target_index": 1,
                                        "initial_health": 250.5,
                                        "current_health": 250.5,
                                        "dead": False,
                                        "simulated_damage_applied": 0.0,
                                        "background_damage_applied": 0.0,
                                    },
                                ],
                            },
                        ),
                    }
                )
            elif command in {"state", "actions"}:
                response["environment_generation"] = 4
                if command == "state":
                    response["state"] = _state(
                        dynamic_team_background=_dynamic_state(config, 4)
                    )
                else:
                    response["actions"] = []
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            result = bridge.load_dynamic_v1(
                {"raid": {}, "encounter": {}}, seed=19, config=config
            )
            self.assertIsInstance(result, DynamicLoadResultV1)
            self.assertEqual(result.receipt.environment_generation, 4)
            self.assertEqual(result.receipt.config_digest, config.content_sha256)
            self.assertEqual(
                bridge.state()["dynamic_team_background"]["environment_generation"],
                4,
            )
            self.assertEqual(bridge.actions(), [])
            bridge.close()

        self.assertEqual(
            process.requests[0],
            {
                "command": "load_dynamic_v1",
                "request": {"raid": {}, "encounter": {}},
                "seed": 19,
                "dynamic": config.to_wire(),
            },
        )

    def test_dynamic_config_rejects_nonfinite_duplicate_and_unsorted_inputs(self) -> None:
        target = (DynamicTargetHealthV1(target_index=0, health=100.0),)
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            DynamicTargetHealthV1(target_index=0, health=math.nan)
        with self.assertRaisesRegex(ValueError, "target_health must cover"):
            DynamicTeamBackgroundConfigV1(
                target_health=(
                    DynamicTargetHealthV1(target_index=0, health=1),
                    DynamicTargetHealthV1(target_index=0, health=2),
                )
            )
        with self.assertRaisesRegex(ValueError, "event_id must match"):
            BackgroundDamageEventV1(
                schedule_index=0,
                time_ms=0,
                target_index=0,
                event_id="bad id",
                damage=1,
            )
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            BackgroundDamageEventV1(
                schedule_index=0,
                time_ms=0,
                target_index=0,
                event_id="bad-damage",
                damage=math.inf,
            )
        with self.assertRaisesRegex(ValueError, "duration range"):
            BackgroundDamageEventV1(
                schedule_index=0,
                time_ms=1 << 63,
                target_index=0,
                event_id="too-late",
                damage=1,
            )
        with self.assertRaisesRegex(ValueError, "duplicate background event_id"):
            DynamicTeamBackgroundConfigV1(
                target_health=target,
                background_damage_events=(
                    BackgroundDamageEventV1(0, 0, 0, "same", 1),
                    BackgroundDamageEventV1(1, 1, 0, "same", 1),
                ),
            )
        with self.assertRaisesRegex(ValueError, "strictly sorted"):
            DynamicTeamBackgroundConfigV1(
                target_health=target,
                background_damage_events=(
                    BackgroundDamageEventV1(0, 2, 0, "later", 1),
                    BackgroundDamageEventV1(1, 1, 0, "earlier", 1),
                ),
            )
        with self.assertRaisesRegex(ValueError, "target_index out of range"):
            DynamicTeamBackgroundConfigV1(
                target_health=target,
                background_damage_events=(
                    BackgroundDamageEventV1(0, 0, 1, "wrong-target", 1),
                ),
            )
        with self.assertRaisesRegex(ValueError, "retarget_mode"):
            DynamicTeamBackgroundConfigV1(
                target_health=target, retarget_mode="RANDOM"
            )

    def test_dynamic_receipts_are_typed_cursor_bound_and_conservative(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),),
            background_damage_events=(
                BackgroundDamageEventV1(0, 0, 0, "hit", 90),
                BackgroundDamageEventV1(1, 10, 0, "kill", 20),
                BackgroundDamageEventV1(2, 20, 0, "dead", 1),
            ),
        )

        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {
                "ok": True,
                "command": command,
                "environment_generation": 9,
            }
            if command == "load_dynamic_v1":
                response["dynamic_load"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 9,
                    "target_count": 1,
                    "background_event_count": 3,
                    "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                    "retarget_mode": "NEXT_ALIVE_CYCLIC",
                }
                response["state"] = _state(
                    dynamic_team_background=_dynamic_state(config, 9)
                )
            elif command == "dynamic_damage_receipts":
                response["dynamic_damage_receipts"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 9,
                    "cursor": request["cursor"],
                    "next_cursor": 3,
                    "schedule_complete": True,
                    "receipts": [
                        {
                            "damage_ordinal": 2,
                            "schedule_index": 1,
                            "event_id": "kill",
                            "time_ms": 10,
                            "target_index": 0,
                            "requested_damage": 20.0,
                            "applied_damage": 10.0,
                            "overkill_damage": 10.0,
                            "killed": True,
                            "status": "APPLIED",
                        },
                        {
                            "schedule_index": 2,
                            "event_id": "dead",
                            "time_ms": 20,
                            "target_index": 0,
                            "requested_damage": 1.0,
                            "applied_damage": 0.0,
                            "overkill_damage": 1.0,
                            "killed": False,
                            "status": "CANCELED_TARGET_DEAD",
                        },
                    ],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            bridge.load_dynamic_v1({}, seed=1, config=config)
            batch = bridge.dynamic_damage_receipts(cursor=1)
            bridge.close()

        self.assertIsInstance(batch, DynamicDamageReceiptBatchV1)
        self.assertEqual(batch.cursor, 1)
        self.assertEqual(batch.next_cursor, 3)
        self.assertTrue(batch.schedule_complete)
        self.assertEqual(sum(row.applied_damage for row in batch.receipts), 10)
        self.assertEqual(sum(row.overkill_damage for row in batch.receipts), 11)
        self.assertEqual(batch.receipts[-1].status, "CANCELED_TARGET_DEAD")
        self.assertEqual(
            process.requests[1],
            {"command": "dynamic_damage_receipts", "cursor": 1},
        )

    def test_dynamic_response_generation_or_digest_mismatch_fails_closed(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),)
        )
        cases = (
            (5, config.content_sha256, 6, config.content_sha256, "generation"),
            (5, config.content_sha256, 5, "0" * 64, "config_digest"),
        )
        for receipt_generation, receipt_digest, state_generation, state_digest, match in cases:
            with self.subTest(match=match):
                def handler(request: dict[str, object]) -> dict[str, object]:
                    return {
                        "ok": True,
                        "command": request["command"],
                        "environment_generation": receipt_generation,
                        "dynamic_load": {
                            "schema": "o2o_dynamic_team_background/v1",
                            "config_digest": receipt_digest,
                            "environment_generation": receipt_generation,
                            "target_count": 1,
                            "background_event_count": 0,
                            "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                            "retarget_mode": "NEXT_ALIVE_CYCLIC",
                        },
                        "state": _state(
                            dynamic_team_background=_dynamic_state(
                                config,
                                state_generation,
                                config_digest=state_digest,
                            )
                        ),
                    }

                process = _FakeProcess(handler)
                with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
                    bridge = SimulatorBridge("o2obridge.exe")
                    with self.assertRaisesRegex(SimBridgeProtocolError, match):
                        bridge.load_dynamic_v1({}, seed=1, config=config)
                    bridge.close()

    def test_dynamic_state_damage_conservation_mismatch_fails_closed(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),)
        )

        def handler(request: dict[str, object]) -> dict[str, object]:
            dynamic_state = _dynamic_state(config, 8)
            dynamic_state["combined_damage_applied"] = 1.0
            return {
                "ok": True,
                "command": request["command"],
                "environment_generation": 8,
                "dynamic_load": {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 8,
                    "target_count": 1,
                    "background_event_count": 0,
                    "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                    "retarget_mode": "NEXT_ALIVE_CYCLIC",
                },
                "state": _state(dynamic_team_background=dynamic_state),
            }

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(SimBridgeProtocolError, "combined damage"):
                bridge.load_dynamic_v1({}, seed=1, config=config)
            bridge.close()

    def test_dynamic_state_validates_config_counts_indexes_health_and_death_time(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(
                DynamicTargetHealthV1(0, 100.0),
                DynamicTargetHealthV1(1, 200.0),
            ),
            retarget_mode="NEXT_ALIVE_CYCLIC",
        )

        def retarget_mismatch(state: dict[str, object]) -> None:
            state["dynamic_team_background"]["retarget_mode"] = "REQUIRE_EXPLICIT"

        def target_rows_mismatch(state: dict[str, object]) -> None:
            state["dynamic_team_background"]["targets"].pop()

        def total_count_mismatch(state: dict[str, object]) -> None:
            state["total_target_count"] = 3

        def live_count_mismatch(state: dict[str, object]) -> None:
            state["num_targets"] = 1

        def selected_index_out_of_range(state: dict[str, object]) -> None:
            state["target_index"] = 2

        def initial_health_bit_mismatch(state: dict[str, object]) -> None:
            state["dynamic_team_background"]["targets"][0]["initial_health"] = (
                math.nextafter(100.0, math.inf)
            )

        def missing_death_time(state: dict[str, object]) -> None:
            dynamic = state["dynamic_team_background"]
            target = dynamic["targets"][0]
            target.update(
                current_health=0.0,
                dead=True,
                simulated_damage_applied=100.0,
            )
            dynamic.update(
                simulated_damage_applied=100.0,
                combined_damage_applied=100.0,
                candidate_events_processed=1,
                damage_applications_total=1,
            )
            state["num_targets"] = 1

        def future_death_time(state: dict[str, object]) -> None:
            missing_death_time(state)
            state["dynamic_team_background"]["targets"][0]["death_time_ms"] = 1

        def alive_death_time(state: dict[str, object]) -> None:
            state["dynamic_team_background"]["targets"][0]["death_time_ms"] = 0

        cases = (
            ("retarget_mode", retarget_mismatch),
            ("target count", target_rows_mismatch),
            ("total_target_count", total_count_mismatch),
            ("num_targets", live_count_mismatch),
            ("target_index", selected_index_out_of_range),
            ("IEEE-754", initial_health_bit_mismatch),
            ("death_time_ms", missing_death_time),
            ("exceeds current state time", future_death_time),
            ("must not expose death_time_ms", alive_death_time),
        )
        for match, mutate in cases:
            with self.subTest(match=match):
                state = _state(dynamic_team_background=_dynamic_state(config, 21))
                mutate(state)

                def handler(request: dict[str, object]) -> dict[str, object]:
                    return {
                        "ok": True,
                        "command": request["command"],
                        "environment_generation": 21,
                        "dynamic_load": {
                            "schema": "o2o_dynamic_team_background/v1",
                            "config_digest": config.content_sha256,
                            "environment_generation": 21,
                            "target_count": 2,
                            "background_event_count": 0,
                            "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                            "retarget_mode": "NEXT_ALIVE_CYCLIC",
                        },
                        "state": deepcopy(state),
                    }

                process = _FakeProcess(handler)
                with patch(
                    "o2o_dps.sim_bridge.subprocess.Popen", return_value=process
                ):
                    bridge = SimulatorBridge("o2obridge.exe")
                    with self.assertRaisesRegex(SimBridgeProtocolError, match):
                        bridge.load_dynamic_v1({}, seed=1, config=config)
                    bridge.close()

    def test_dynamic_candidate_receipts_are_typed_bound_and_conservative(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),)
        )

        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {
                "ok": True,
                "command": command,
                "environment_generation": 12,
            }
            if command == "load_dynamic_v1":
                response["dynamic_load"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 12,
                    "target_count": 1,
                    "background_event_count": 0,
                    "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                    "retarget_mode": "NEXT_ALIVE_CYCLIC",
                }
                response["state"] = _state(
                    dynamic_team_background=_dynamic_state(config, 12)
                )
            elif command == "dynamic_candidate_damage_receipts":
                response["dynamic_candidate_damage_receipts"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 12,
                    "cursor": 1,
                    "next_cursor": 3,
                    "receipts": [
                        {
                            "damage_ordinal": 4,
                            "time_ms": 10,
                            "target_index": 0,
                            "requested_damage": 10.0,
                            "applied_damage": 10.0,
                            "overkill_damage": 0.0,
                            "killed": False,
                            "status": "APPLIED",
                            "action": {"spell_id": 1680},
                            "outcome": "HIT",
                            "execution_id": 3,
                            "execution_index": 1,
                            "landed_execution_index": 1,
                            "resolution_phase": "APPLIED_AFTER_OUTCOME",
                            "outcome_computed": True,
                            "random_stream_rewound": False,
                            "attempt_id": "ww-1",
                        },
                        {
                            "damage_ordinal": 6,
                            "time_ms": 10,
                            "target_index": 0,
                            "requested_damage": 8.0,
                            "applied_damage": 0.0,
                            "overkill_damage": 8.0,
                            "killed": False,
                            "status": "CANCELED_TARGET_DEAD",
                            "action": {"other_id": 1},
                            "outcome": "CANCELED",
                            "execution_id": 4,
                            "execution_index": 2,
                            "landed_execution_index": 0,
                            "resolution_phase": "TARGET_DIED_AFTER_OUTCOME",
                            "outcome_computed": True,
                            "random_stream_rewound": False,
                        },
                    ],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            bridge.load_dynamic_v1({}, seed=1, config=config)
            batch = bridge.dynamic_candidate_damage_receipts(cursor=1)
            bridge.close()

        self.assertIsInstance(batch, DynamicCandidateDamageReceiptBatchV1)
        self.assertEqual((batch.cursor, batch.next_cursor), (1, 3))
        self.assertEqual(batch.receipts[0].attempt_id, "ww-1")
        self.assertEqual(
            batch.receipts[0].resolution_phase, "APPLIED_AFTER_OUTCOME"
        )
        self.assertTrue(batch.receipts[0].outcome_computed)
        self.assertFalse(batch.receipts[0].random_stream_rewound)
        self.assertEqual(batch.receipts[1].status, "CANCELED_TARGET_DEAD")
        self.assertEqual(
            batch.receipts[1].resolution_phase, "TARGET_DIED_AFTER_OUTCOME"
        )
        self.assertEqual(batch.receipts[1].landed_execution_index, 0)
        self.assertEqual(
            process.requests[1],
            {"command": "dynamic_candidate_damage_receipts", "cursor": 1},
        )

    def test_dynamic_candidate_receipt_malformed_conservation_fails_closed(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),)
        )

        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {
                "ok": True,
                "command": command,
                "environment_generation": 13,
            }
            if command == "load_dynamic_v1":
                response["dynamic_load"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 13,
                    "target_count": 1,
                    "background_event_count": 0,
                    "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                    "retarget_mode": "NEXT_ALIVE_CYCLIC",
                }
                response["state"] = _state(
                    dynamic_team_background=_dynamic_state(config, 13)
                )
            else:
                response["dynamic_candidate_damage_receipts"] = {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": config.content_sha256,
                    "environment_generation": 13,
                    "cursor": 0,
                    "next_cursor": 1,
                    "receipts": [{
                        "damage_ordinal": 1,
                        "time_ms": 0,
                        "target_index": 0,
                        "requested_damage": 5.0,
                        "applied_damage": 4.0,
                        "overkill_damage": 2.0,
                        "killed": False,
                        "status": "APPLIED",
                        "action": {"spell_id": 1},
                        "outcome": "HIT",
                        "execution_id": 1,
                        "execution_index": 1,
                        "landed_execution_index": 1,
                        "resolution_phase": "APPLIED_AFTER_OUTCOME",
                        "outcome_computed": True,
                        "random_stream_rewound": False,
                    }],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            bridge.load_dynamic_v1({}, seed=1, config=config)
            with self.assertRaisesRegex(SimBridgeProtocolError, "requested damage"):
                bridge.dynamic_candidate_damage_receipts()
            bridge.close()

    def test_dynamic_receipt_parsers_reject_target_bounds_and_phase_claims(self) -> None:
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 100),),
            background_damage_events=(
                BackgroundDamageEventV1(0, 0, 0, "fixture-event", 1),
            ),
        )
        background_receipt = {
            "damage_ordinal": 1,
            "schedule_index": 0,
            "event_id": "fixture-event",
            "time_ms": 0,
            "target_index": 1,
            "requested_damage": 1.0,
            "applied_damage": 1.0,
            "overkill_damage": 0.0,
            "killed": False,
            "status": "APPLIED",
        }
        candidate_receipt = {
            "damage_ordinal": 1,
            "time_ms": 0,
            "target_index": 0,
            "requested_damage": 1.0,
            "applied_damage": 1.0,
            "overkill_damage": 0.0,
            "killed": False,
            "status": "APPLIED",
            "action": {"spell_id": 1},
            "outcome": "HIT",
            "execution_id": 1,
            "execution_index": 1,
            "landed_execution_index": 1,
            "resolution_phase": "APPLIED_AFTER_OUTCOME",
            "outcome_computed": True,
            "random_stream_rewound": False,
        }
        cases = (
            (
                "dynamic_damage_receipts",
                "dynamic_damage_receipts",
                background_receipt,
                "target_index is out of range",
                1,
            ),
            (
                "dynamic_damage_receipts",
                "dynamic_damage_receipts",
                {
                    **background_receipt,
                    "target_index": 0,
                    "event_id": "wrong-event",
                },
                "config schedule row",
                1,
            ),
            (
                "dynamic_damage_receipts",
                "dynamic_damage_receipts",
                {
                    **background_receipt,
                    "target_index": 0,
                    "requested_damage": 2.0,
                    "applied_damage": 2.0,
                },
                "config schedule row",
                1,
            ),
            (
                "dynamic_candidate_damage_receipts",
                "dynamic_candidate_damage_receipts",
                {**candidate_receipt, "target_index": 1},
                "target_index is out of range",
                1,
            ),
            (
                "dynamic_candidate_damage_receipts",
                "dynamic_candidate_damage_receipts",
                {
                    **candidate_receipt,
                    "resolution_phase": "TARGET_DEAD_BEFORE_OUTCOME",
                    "outcome_computed": False,
                },
                "phase/outcome boundary",
                1,
            ),
            (
                "dynamic_candidate_damage_receipts",
                "dynamic_candidate_damage_receipts",
                {
                    **candidate_receipt,
                    "requested_damage": 100.0,
                    "applied_damage": 100.0,
                    "killed": True,
                    "retargeted_to": 1,
                },
                "retargeted_to is out of range",
                1,
            ),
            (
                "dynamic_damage_receipts",
                "dynamic_damage_receipts",
                {**background_receipt, "target_index": 0},
                "next_cursor exceeds config",
                2,
            ),
        )
        for command_name, response_key, receipt, match, next_cursor in cases:
            with self.subTest(match=match):
                def handler(request: dict[str, object]) -> dict[str, object]:
                    command = str(request["command"])
                    response: dict[str, object] = {
                        "ok": True,
                        "command": command,
                        "environment_generation": 31,
                    }
                    if command == "load_dynamic_v1":
                        response["dynamic_load"] = {
                            "schema": "o2o_dynamic_team_background/v1",
                            "config_digest": config.content_sha256,
                            "environment_generation": 31,
                            "target_count": 1,
                            "background_event_count": 1,
                            "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                            "retarget_mode": "NEXT_ALIVE_CYCLIC",
                        }
                        response["state"] = _state(
                            dynamic_team_background=_dynamic_state(config, 31)
                        )
                    elif command == command_name:
                        response[response_key] = {
                            "schema": "o2o_dynamic_team_background/v1",
                            "config_digest": config.content_sha256,
                            "environment_generation": 31,
                            "cursor": 0,
                            "next_cursor": next_cursor,
                            **(
                                {"schedule_complete": True}
                                if command_name == "dynamic_damage_receipts"
                                else {}
                            ),
                            "receipts": [receipt],
                        }
                    return response

                process = _FakeProcess(handler)
                with patch(
                    "o2o_dps.sim_bridge.subprocess.Popen", return_value=process
                ):
                    bridge = SimulatorBridge("o2obridge.exe")
                    bridge.load_dynamic_v1({}, seed=1, config=config)
                    with self.assertRaisesRegex(SimBridgeProtocolError, match):
                        getattr(bridge, command_name)()
                    bridge.close()

    def test_persistent_process_round_trip_preserves_action_tag(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {"ok": True, "command": command}
            if command == "load":
                response["state"] = _state()
            elif command == "actions":
                response["actions"] = [
                    {
                        "index": 7,
                        "action": {"spell_id": 25286, "tag": 1},
                        "label": "SpellID:25286 (Tag: 1)",
                        "legal": True,
                        "ready_in_ms": 0,
                        "triggers_gcd": False,
                    }
                ]
            elif command == "act":
                response["apply"] = {
                    "casted": True,
                    "consumes_decision": False,
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state()
            elif command == "cancel_queue":
                response["cancel_queue"] = {
                    "canceled": True,
                    "consumes_decision": False,
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state()
            elif command == "set_target":
                response["set_target"] = {
                    "changed": True,
                    "target_index": request["target_index"],
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state(
                    target_index=request["target_index"], num_targets=2
                )
            elif command == "wait":
                response["state"] = _state(time_ms=100, needs_input=False)
            elif command == "advance":
                response["state"] = _state(time_ms=1500)
            elif command == "state":
                response["state"] = _state(time_ms=1500)
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process) as popen:
            with SimulatorBridge("C:/tools/o2obridge.exe") as bridge:
                loaded = bridge.load({"raid": {}, "encounter": {}}, seed=41)
                actions = bridge.actions()
                applied = bridge.act(actions[0].action)
                canceled = bridge.cancel_queue()
                targeted = bridge.set_target(1)
                bridge.wait(100)
                bridge.advance()
                bridge.state()

        self.assertTrue(loaded["needs_input"])
        self.assertEqual(actions[0].action, ActionRef(spell_id=25286, tag=1))
        self.assertTrue(applied.casted)
        self.assertFalse(applied.consumes_decision)
        self.assertEqual(
            canceled,
            CancelQueueResult(
                canceled=True,
                consumes_decision=False,
                finished=False,
                needs_input=True,
                state=_state(),
            ),
        )
        self.assertEqual(
            targeted,
            SetTargetResult(
                changed=True,
                target_index=1,
                finished=False,
                needs_input=True,
                state=_state(target_index=1, num_targets=2),
            ),
        )
        self.assertEqual(
            process.requests[0],
            {
                "command": "load",
                "request": {"raid": {}, "encounter": {}},
                "seed": 41,
            },
        )
        self.assertEqual(
            process.requests[2]["action"], {"spell_id": 25286, "tag": 1}
        )
        self.assertEqual(process.requests[3], {"command": "cancel_queue"})
        self.assertEqual(
            process.requests[4], {"command": "set_target", "target_index": 1}
        )
        self.assertEqual(process.requests[-1], {"command": "close"})
        self.assertEqual(popen.call_count, 1)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_full_policy_controls_and_result_stream_use_runtime_types(self) -> None:
        attempt_id = "decision-4:sink-3"

        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {"ok": True, "command": command}
            if command in {"start_attack", "stop_cast"}:
                response["control"] = {
                    "accepted": command == "start_attack",
                    "consumes_decision": False,
                }
                response["state"] = _state(time_ms=250)
            elif command == "act":
                response["apply"] = {
                    "casted": True,
                    "consumes_decision": True,
                    "finished": False,
                    "needs_input": False,
                }
                response["state"] = _state(time_ms=250, needs_input=False)
            elif command == "server_results":
                response["server_results"] = {
                    "complete_through_time_ms": 1500,
                    "events": [
                        {
                            "time_ms": 1200,
                            "outcome": "CRIT",
                            "attempt_id": attempt_id,
                            "damage": 321.5,
                            "action": {"spell_id": 23894},
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "CRIT",
                                    "damage": 321.5,
                                }
                            ],
                        }
                    ],
                    "pending_attempt_ids": [],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            started = bridge.start_attack()
            stopped = bridge.stop_cast()
            applied = bridge.act(
                ActionRef(spell_id=23894), attempt_id=attempt_id
            )
            batch = bridge.server_results_since_last_decision((attempt_id,))
            bridge.close()

        # Importing these public identities only after calling the bridge also
        # exercises the runtime-local imports used to avoid a module cycle.
        from o2o_dps.fury_full_policy_rollout_v2 import ServerResultBatchV2
        from o2o_dps.fury_ordered_sink_executor_v2 import ControlSinkResultV2

        self.assertIsInstance(started, ControlSinkResultV2)
        self.assertTrue(started.accepted)
        self.assertFalse(started.consumes_decision)
        self.assertIsInstance(stopped, ControlSinkResultV2)
        self.assertFalse(stopped.accepted)
        self.assertTrue(applied.casted)
        self.assertIsInstance(batch, ServerResultBatchV2)
        self.assertEqual(batch.events[0].attempt_id, attempt_id)
        self.assertEqual(batch.events[0].action, ActionRef(spell_id=23894))
        self.assertEqual(batch.events[0].target_results[0].target_index, 0)
        self.assertEqual(batch.events[0].target_results[0].outcome, "CRIT")
        self.assertEqual(
            process.requests[2],
            {
                "command": "act",
                "action": {"spell_id": 23894},
                "attempt_id": attempt_id,
            },
        )
        self.assertEqual(
            process.requests[3],
            {"command": "server_results", "attempt_ids": [attempt_id]},
        )

    def test_full_policy_control_response_requires_typed_fields_and_state(self) -> None:
        cases = (
            (
                {"control": {"consumes_decision": False}, "state": _state()},
                "accepted must be a boolean",
            ),
            (
                {"control": {"accepted": True, "consumes_decision": 0}, "state": _state()},
                "consumes_decision must be a boolean",
            ),
            (
                {"control": {"accepted": True, "consumes_decision": False}},
                "missing the simulator state object",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                def handler(request: dict[str, object]) -> dict[str, object]:
                    response = {"ok": True, "command": request["command"]}
                    if request["command"] == "start_attack":
                        response.update(payload)
                    return response

                process = _FakeProcess(handler)
                with patch(
                    "o2o_dps.sim_bridge.subprocess.Popen", return_value=process
                ):
                    bridge = SimulatorBridge("o2obridge.exe")
                    with self.assertRaisesRegex(SimBridgeProtocolError, message):
                        bridge.start_attack()
                    bridge.close()

    def test_server_result_stream_fails_closed_on_malformed_or_misaligned_wire(self) -> None:
        attempt_id = "decision-2:sink-1"
        valid_event: dict[str, object] = {
            "time_ms": 100,
            "outcome": "HIT",
            "attempt_id": attempt_id,
            "damage": 10.0,
            "action": {"spell_id": 23894},
            "target_results": [
                {"target_index": 0, "outcome": "HIT", "damage": 10.0}
            ],
        }
        cases = (
            (
                {"complete_through_time_ms": 100, "events": [valid_event]},
                "missing the pending_attempt_ids array",
            ),
            (
                {"events": [valid_event]},
                "complete_through_time_ms is required",
            ),
            (
                {"complete_through_time_ms": 100, "events": []},
                "partition the requested ledger",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "attempt_id": "wrong"}],
                },
                "partition the requested ledger",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "action": None}],
                },
                "action must be an object",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "action": {}}],
                },
                "exactly one nonzero primary ID",
            ),
            (
                {
                    "complete_through_time_ms": 99,
                    "events": [valid_event],
                },
                "time_ms exceeds complete_through_time_ms",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "damage": True}],
                },
                "damage must be numeric",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "damage": float("inf")}],
                },
                "damage must be finite",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "damage": 10**400}],
                },
                "damage must be finite",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "outcome": " "}],
                },
                "outcome must be a nonempty string",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "outcome": "UNKNOWN"}],
                },
                "unsupported server result outcome",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            key: value
                            for key, value in valid_event.items()
                            if key != "target_results"
                        }
                    ],
                },
                "target_results must be an array",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "target_results": [None]}],
                },
                "each server target result must be an object",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": True,
                                    "outcome": "HIT",
                                    "damage": 10.0,
                                }
                            ],
                        }
                    ],
                },
                "target_index must be an integer",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": -1,
                                    "outcome": "HIT",
                                    "damage": 10.0,
                                }
                            ],
                        }
                    ],
                },
                "target_index must be non-negative",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": True,
                                }
                            ],
                        }
                    ],
                },
                "damage must be numeric",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": -1,
                                }
                            ],
                        }
                    ],
                },
                "damage must be non-negative",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 4.0,
                                },
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 6.0,
                                },
                            ],
                        }
                    ],
                },
                "target indices must be unique",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 1,
                                    "outcome": "HIT",
                                    "damage": 4.0,
                                },
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 6.0,
                                },
                            ],
                        }
                    ],
                },
                "target indices must be in ascending order",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 9.0,
                                }
                            ],
                        }
                    ],
                },
                "target damage sum must equal aggregate damage",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "outcome": "CRIT"}],
                },
                "aggregate outcome does not match target_results",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "CANCELED",
                                    "damage": 10.0,
                                }
                            ],
                        }
                    ],
                },
                "unsupported server target result outcome",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "outcome": "CANCELED",
                            "damage": 0,
                        }
                    ],
                },
                "CANCELED server result requires zero damage and no target_results",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [
                        {
                            **valid_event,
                            "outcome": "CANCELED",
                            "target_results": [],
                        }
                    ],
                },
                "CANCELED server result requires zero damage and no target_results",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [{**valid_event, "target_results": []}],
                },
                "resolved server result requires at least one target result",
            ),
            (
                {
                    "complete_through_time_ms": 100,
                    "events": [valid_event],
                    "pending_attempt_ids": [attempt_id],
                },
                "must not overlap",
            ),
        )
        for server_results, message in cases:
            with self.subTest(message=message):
                if (
                    message != "missing the pending_attempt_ids array"
                    and "pending_attempt_ids" not in server_results
                ):
                    server_results = {
                        **server_results,
                        "pending_attempt_ids": [],
                    }
                def handler(request: dict[str, object]) -> dict[str, object]:
                    response = {"ok": True, "command": request["command"]}
                    if request["command"] == "server_results":
                        response["server_results"] = server_results
                    return response

                process = _FakeProcess(handler)
                with patch(
                    "o2o_dps.sim_bridge.subprocess.Popen", return_value=process
                ):
                    bridge = SimulatorBridge("o2obridge.exe")
                    with self.assertRaisesRegex(SimBridgeProtocolError, message):
                        bridge.server_results_since_last_decision((attempt_id,))
                    bridge.close()

    def test_server_result_stream_preserves_mixed_and_zero_damage_targets(self) -> None:
        attempt_ids = ("whirlwind", "miss")

        def handler(request: dict[str, object]) -> dict[str, object]:
            response: dict[str, object] = {
                "ok": True,
                "command": request["command"],
            }
            if request["command"] == "server_results":
                response["server_results"] = {
                    "complete_through_time_ms": 500,
                    "events": [
                        {
                            "time_ms": 400,
                            "outcome": "MIXED",
                            "attempt_id": "whirlwind",
                            "damage": 350.0,
                            "action": {"spell_id": 1680},
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 100.0,
                                },
                                {
                                    "target_index": 2,
                                    "outcome": "CRIT",
                                    "damage": 250.0,
                                },
                            ],
                        },
                        {
                            "time_ms": 450,
                            "outcome": "MISS",
                            "attempt_id": "miss",
                            "damage": 0,
                            "action": {"spell_id": 23894},
                            "target_results": [
                                {
                                    "target_index": 1,
                                    "outcome": "MISS",
                                    "damage": 0,
                                }
                            ],
                        },
                    ],
                    "pending_attempt_ids": [],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            batch = bridge.server_results_since_last_decision(attempt_ids)
            bridge.close()

        mixed, missed = batch.events
        self.assertEqual(
            tuple(result.target_index for result in mixed.target_results), (0, 2)
        )
        self.assertEqual(
            tuple(result.outcome for result in mixed.target_results),
            ("HIT", "CRIT"),
        )
        self.assertEqual(
            sum(result.damage for result in mixed.target_results), mixed.damage
        )
        self.assertEqual(missed.outcome, "MISS")
        self.assertEqual(missed.damage, 0.0)
        self.assertEqual(missed.target_results[0].damage, 0.0)

    def test_native_four_target_whirlwind_roundoff_is_not_a_protocol_failure(self) -> None:
        damages = (496.618271560117, 487.5592982917421,
                   928.7920836555717, 454.7964155797238)

        def handler(request: dict[str, object]) -> dict[str, object]:
            return {
                "ok": True, "command": request["command"],
                "server_results": {
                    "complete_through_time_ms": 1500,
                    "events": [{
                        "time_ms": 1500, "outcome": "MIXED", "attempt_id": "ww-1",
                        "damage": 2367.766069087155,
                        "action": {"spell_id": 1680},
                        "target_results": [
                            {"target_index": index,
                             "outcome": "CRIT" if index == 2 else "HIT", "damage": damage}
                            for index, damage in enumerate(damages)
                        ],
                    }],
                    "pending_attempt_ids": [],
                },
            }

        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=_FakeProcess(handler)):
            bridge = SimulatorBridge("o2obridge.exe")
            batch = bridge.server_results_since_last_decision(("ww-1",))
            bridge.close()
        self.assertEqual(sum(damages), batch.events[0].damage)

    def test_server_result_stream_can_keep_the_complete_ledger_pending(self) -> None:
        attempt_ids = ("decision-1:sink-2", "decision-2:sink-4")

        def handler(request: dict[str, object]) -> dict[str, object]:
            response: dict[str, object] = {
                "ok": True,
                "command": request["command"],
            }
            if request["command"] == "server_results":
                response["server_results"] = {
                    "complete_through_time_ms": 900,
                    "events": [],
                    "pending_attempt_ids": list(attempt_ids),
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            batch = bridge.server_results_since_last_decision(attempt_ids)
            bridge.close()

        self.assertEqual(batch.events, ())
        self.assertEqual(batch.pending_attempt_ids, attempt_ids)

    def test_newer_result_can_resolve_while_an_older_hardcast_stays_pending(self) -> None:
        attempt_ids = ("old-slam", "new-instant")

        def handler(request: dict[str, object]) -> dict[str, object]:
            response: dict[str, object] = {
                "ok": True,
                "command": request["command"],
            }
            if request["command"] == "server_results":
                response["server_results"] = {
                    "complete_through_time_ms": 1200,
                    "events": [
                        {
                            "time_ms": 1200,
                            "outcome": "HIT",
                            "attempt_id": "new-instant",
                            "damage": 88.0,
                            "action": {"spell_id": 23894},
                            "target_results": [
                                {
                                    "target_index": 0,
                                    "outcome": "HIT",
                                    "damage": 88.0,
                                }
                            ],
                        }
                    ],
                    "pending_attempt_ids": ["old-slam"],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            batch = bridge.server_results_since_last_decision(attempt_ids)
            bridge.close()

        self.assertEqual(
            tuple(event.attempt_id for event in batch.events), ("new-instant",)
        )
        self.assertEqual(batch.pending_attempt_ids, ("old-slam",))

    def test_canceled_hardcast_is_a_typed_zero_damage_result(self) -> None:
        attempt_id = "old-slam"

        def handler(request: dict[str, object]) -> dict[str, object]:
            response: dict[str, object] = {
                "ok": True,
                "command": request["command"],
            }
            if request["command"] == "server_results":
                response["server_results"] = {
                    "complete_through_time_ms": 700,
                    "events": [
                        {
                            "time_ms": 700,
                            "outcome": "CANCELED",
                            "attempt_id": attempt_id,
                            "damage": 0,
                            "action": {"spell_id": 45961},
                            "target_results": [],
                        }
                    ],
                    "pending_attempt_ids": [],
                }
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            batch = bridge.server_results_since_last_decision((attempt_id,))
            bridge.close()

        self.assertEqual(batch.events[0].outcome, "CANCELED")
        self.assertEqual(batch.events[0].damage, 0.0)
        self.assertEqual(batch.events[0].target_results, ())

    def test_attempt_ids_are_nonempty_unique_strings(self) -> None:
        process = _FakeProcess(
            lambda request: {"ok": True, "command": request["command"]}
        )
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(TypeError, "sequence of strings"):
                bridge.server_results_since_last_decision("not-a-sequence")
            with self.assertRaisesRegex(TypeError, "nonempty string"):
                bridge.server_results_since_last_decision(("",))
            with self.assertRaisesRegex(TypeError, "nonempty string"):
                bridge.server_results_since_last_decision(("   ",))
            with self.assertRaisesRegex(ValueError, "must be unique"):
                bridge.server_results_since_last_decision(("a", "a"))
            with self.assertRaisesRegex(TypeError, "nonempty string or None"):
                bridge.act(ActionRef(spell_id=23894), attempt_id=" ")
            bridge.close()

    def test_explicit_arguments_support_a_non_shell_transport_wrapper(self) -> None:
        process = _FakeProcess(
            lambda request: {"ok": True, "command": request["command"]}
        )
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process) as popen:
            with SimulatorBridge(
                "wsl.exe",
                arguments=("-d", "Ubuntu-22.04", "--", "/tmp/o2obridge"),
            ):
                pass

        self.assertEqual(
            popen.call_args.args[0],
            ["wsl.exe", "-d", "Ubuntu-22.04", "--", "/tmp/o2obridge"],
        )
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_bridge_arguments_fail_closed_before_process_start(self) -> None:
        with self.assertRaisesRegex(TypeError, "sequence of strings"):
            SimulatorBridge("o2obridge.exe", arguments="--bad")
        with self.assertRaisesRegex(TypeError, "nonempty string"):
            SimulatorBridge("o2obridge.exe", arguments=("--ok", ""))

    def test_remote_error_names_command_and_bridge_error(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            if command == "load":
                return {
                    "ok": False,
                    "command": command,
                    "error": "raid, encounter, and sim_options are required",
                }
            return {"ok": True, "command": command}

        process = _FakeProcess(handler, stderr="sim setup failed\n")
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeCommandError,
                "rejected load: raid, encounter, and sim_options are required",
            ):
                bridge.load({}, seed=1)
            bridge.close()

    def test_cancel_queue_requires_a_typed_result(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            return {"ok": True, "command": request["command"], "state": _state()}

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeProtocolError,
                "cancel_queue response is missing cancel_queue result",
            ):
                bridge.cancel_queue()
            bridge.close()

    def test_invalid_json_response_is_a_protocol_error(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object] | str:
            if request["command"] == "state":
                return "not-json"
            return {"ok": True, "command": request["command"]}

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeProtocolError, "invalid JSON for state"
            ):
                bridge.state()
            bridge.close()

    def test_start_failure_reports_executable(self) -> None:
        with patch(
            "o2o_dps.sim_bridge.subprocess.Popen",
            side_effect=FileNotFoundError("missing"),
        ):
            with self.assertRaisesRegex(
                SimBridgeProcessError, "could not start simulator bridge.*missing.exe"
            ):
                SimulatorBridge("C:/missing.exe")


if __name__ == "__main__":
    unittest.main()
