from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_bridge_platform_equivalence_v2 import (
    ATTEMPT_ID,
    CAPTURE_SCHEMA,
    DEFAULT_REQUEST,
    FuryBridgePlatformEquivalenceV2Error,
    build_dynamic_fixture_v2,
    canonical_bytes,
    capture_dynamic_trace_v2,
    compare_platform_captures_v2,
    linux_wsl_arguments,
    load_request,
    publish_content_addressed_receipt_v2,
    run_equivalence_v2,
    verify_content_addressed_receipt_v2,
)
from o2o_dps.fury_full_policy_rollout_v2 import (
    ServerResultBatchV2,
    ServerResultEventV2,
    ServerTargetResultV2,
)
from o2o_dps.sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    DynamicCandidateDamageReceiptBatchV1,
    DynamicCandidateDamageReceiptV1,
    DynamicDamageReceiptBatchV1,
    DynamicDamageReceiptV1,
    DynamicLoadReceiptV1,
    DynamicLoadResultV1,
)


def _jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(child) for child in value]
    return value


class FakeDynamicRecordingBridge:
    """Deterministic typed fake with a complete pseudo-JSONL audit trace."""

    instances: list["FakeDynamicRecordingBridge"] = []

    def __init__(self, executable=None, *, arguments=(), cwd=None):
        self.executable = str(executable)
        self.arguments = tuple(arguments)
        self.cwd = str(cwd)
        self.wire_trace_v2 = []
        self.closed = False
        self.config = None
        self.generation = 7
        self._current = None
        self._bloodthirst_damage = 321.0
        self.__class__.instances.append(self)

    def _record(self, command, response, **payload):
        self.wire_trace_v2.append(
            {
                "request": copy.deepcopy({"command": command, **payload}),
                "response": copy.deepcopy(
                    {"ok": True, "command": command, **response}
                ),
            }
        )

    def _state(self, phase):
        assert self.config is not None
        if phase in {"initial", "wait"}:
            time_ms = 0
            target_index = 0
            live = 2
            health_current = 5000.0
            rage = 50.0
            targets = [
                {
                    "target_index": 0,
                    "initial_health": 1.0,
                    "current_health": 1.0,
                    "dead": False,
                    "death_time_ms": None,
                },
                {
                    "target_index": 1,
                    "initial_health": 100000.0,
                    "current_health": 100000.0,
                    "dead": False,
                    "death_time_ms": None,
                },
            ]
            counters = (0, 0, 0, 0)
        else:
            time_ms = 250
            target_index = 1
            live = 1
            health_current = 4800.0
            rage = 55.0
            remaining = 100000.0
            candidate_processed = 1
            if phase == "acted":
                remaining -= self._bloodthirst_damage
                candidate_processed = 2
            targets = [
                {
                    "target_index": 0,
                    "initial_health": 1.0,
                    "current_health": 0.0,
                    "dead": True,
                    "death_time_ms": 10,
                },
                {
                    "target_index": 1,
                    "initial_health": 100000.0,
                    "current_health": remaining,
                    "dead": False,
                    "death_time_ms": None,
                },
            ]
            counters = (2, 1, candidate_processed, 1)
        return {
            "time_ms": time_ms,
            "health_current": health_current,
            "power": {"type": "rage", "current": rage},
            "target_index": target_index,
            "num_targets": live,
            "total_target_count": 2,
            "target_health_known": True,
            "needs_input": False,
            "dynamic_team_background": {
                "schema": "o2o_dynamic_team_background/v1",
                "config_digest": self.config.content_sha256,
                "environment_generation": self.generation,
                "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
                "retarget_mode": "NEXT_ALIVE_CYCLIC",
                "background_events_processed": counters[0],
                "background_events_canceled": counters[1],
                "candidate_events_processed": counters[2],
                "candidate_events_canceled": counters[3],
                "targets": targets,
            },
        }

    def load_dynamic_v1(self, request, seed, config):
        self.config = config
        self._current = self._state("initial")
        receipt = DynamicLoadReceiptV1(
            schema="o2o_dynamic_team_background/v1",
            config_digest=config.content_sha256,
            environment_generation=self.generation,
            target_count=2,
            background_event_count=2,
            same_timestamp_order="BACKGROUND_BEFORE_CANDIDATE",
            retarget_mode="NEXT_ALIVE_CYCLIC",
        )
        result = DynamicLoadResultV1(receipt=receipt, state=copy.deepcopy(self._current))
        self._record(
            "load_dynamic_v1",
            {
                "environment_generation": self.generation,
                "dynamic_load": _jsonable(receipt),
                "state": copy.deepcopy(self._current),
            },
            request=copy.deepcopy(request),
            seed=seed,
            dynamic=config.to_wire(),
        )
        return result

    def wait(self, wait_ms):
        self._current = self._state("wait")
        self._record("wait", {"state": self._current}, wait_ms=wait_ms)
        return copy.deepcopy(self._current)

    def advance(self):
        self._current = self._state("advanced")
        self._record("advance", {"state": self._current})
        return copy.deepcopy(self._current)

    def _background_batch(self, cursor):
        receipts = ()
        if cursor == 0:
            receipts = (
                DynamicDamageReceiptV1(
                    schedule_index=0,
                    event_id="same-ms-background-kill",
                    time_ms=10,
                    target_index=0,
                    requested_damage=1.0,
                    applied_damage=1.0,
                    overkill_damage=0.0,
                    killed=True,
                    status="APPLIED",
                    damage_ordinal=1,
                    retargeted_to=1,
                ),
                DynamicDamageReceiptV1(
                    schedule_index=1,
                    event_id="post-death-background-cancel",
                    time_ms=20,
                    target_index=0,
                    requested_damage=3.0,
                    applied_damage=0.0,
                    overkill_damage=3.0,
                    killed=False,
                    status="CANCELED_TARGET_DEAD",
                    damage_ordinal=3,
                ),
            )
        return DynamicDamageReceiptBatchV1(
            schema="o2o_dynamic_team_background/v1",
            config_digest=self.config.content_sha256,
            environment_generation=self.generation,
            cursor=cursor,
            next_cursor=2,
            schedule_complete=True,
            receipts=receipts,
        )

    def dynamic_damage_receipts(self, *, cursor=0):
        batch = self._background_batch(cursor)
        self._record(
            "dynamic_damage_receipts",
            {"dynamic_damage_receipts": _jsonable(batch)},
            cursor=cursor,
        )
        return batch

    def _candidate_batch(self, cursor):
        if cursor == 0:
            receipts = (
                DynamicCandidateDamageReceiptV1(
                    damage_ordinal=2,
                    time_ms=10,
                    target_index=0,
                    requested_damage=100.0,
                    applied_damage=0.0,
                    overkill_damage=100.0,
                    killed=False,
                    status="CANCELED_TARGET_DEAD",
                    action=ActionRef(other_id=7, tag=1),
                    outcome="CANCELED",
                    execution_id=1,
                    execution_index=0,
                    landed_execution_index=0,
                    resolution_phase="TARGET_DIED_AFTER_OUTCOME",
                    outcome_computed=True,
                    random_stream_rewound=False,
                ),
            )
            next_cursor = 1
        else:
            receipts = (
                DynamicCandidateDamageReceiptV1(
                    damage_ordinal=4,
                    time_ms=250,
                    target_index=1,
                    requested_damage=self._bloodthirst_damage,
                    applied_damage=self._bloodthirst_damage,
                    overkill_damage=0.0,
                    killed=False,
                    status="APPLIED",
                    action=ActionRef(spell_id=23894),
                    outcome="HIT",
                    execution_id=2,
                    execution_index=0,
                    landed_execution_index=0,
                    resolution_phase="APPLIED_AFTER_OUTCOME",
                    outcome_computed=True,
                    random_stream_rewound=False,
                    attempt_id=ATTEMPT_ID,
                ),
            )
            next_cursor = 2
        return DynamicCandidateDamageReceiptBatchV1(
            schema="o2o_dynamic_team_background/v1",
            config_digest=self.config.content_sha256,
            environment_generation=self.generation,
            cursor=cursor,
            next_cursor=next_cursor,
            receipts=receipts,
        )

    def dynamic_candidate_damage_receipts(self, *, cursor=0):
        batch = self._candidate_batch(cursor)
        self._record(
            "dynamic_candidate_damage_receipts",
            {"dynamic_candidate_damage_receipts": _jsonable(batch)},
            cursor=cursor,
        )
        return batch

    def state(self):
        self._record("state", {"state": self._current})
        return copy.deepcopy(self._current)

    def actions(self):
        values = [
            AvailableAction(
                index=0,
                action=ActionRef(spell_id=23894),
                label="Bloodthirst",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
            )
        ]
        self._record("actions", {"actions": _jsonable(values)})
        return values

    def act(self, action, *, attempt_id=None):
        self._current = self._state("acted")
        result = ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state=copy.deepcopy(self._current),
        )
        self._record(
            "act",
            {"apply": _jsonable(result), "state": self._current},
            action=action.to_wire(),
            attempt_id=attempt_id,
        )
        return result

    def server_results_since_last_decision(self, attempt_ids=()):
        result = ServerResultBatchV2(
            complete_through_time_ms=250,
            events=(
                ServerResultEventV2(
                    time_ms=250,
                    outcome="HIT",
                    attempt_id=ATTEMPT_ID,
                    damage=self._bloodthirst_damage,
                    action=ActionRef(spell_id=23894),
                    target_results=(
                        ServerTargetResultV2(
                            target_index=1,
                            outcome="HIT",
                            damage=self._bloodthirst_damage,
                        ),
                    ),
                ),
            ),
            pending_attempt_ids=(),
        )
        self._record(
            "server_results",
            {"server_results": _jsonable(result)},
            attempt_ids=list(attempt_ids),
        )
        return result

    def close(self):
        self.closed = True
        self._record("close", {})


class BadOrdinalFakeBridge(FakeDynamicRecordingBridge):
    def _candidate_batch(self, cursor):
        batch = super()._candidate_batch(cursor)
        if cursor != 0:
            return batch
        return replace(
            batch,
            receipts=(replace(batch.receipts[0], damage_ordinal=4),),
        )


def _fixture():
    _, base = load_request(DEFAULT_REQUEST)
    return build_dynamic_fixture_v2(base)


def _capture(platform="windows"):
    request, config = _fixture()
    bridge = FakeDynamicRecordingBridge(platform)
    capture = capture_dynamic_trace_v2(
        bridge,
        request,
        config,
        seed=37,
        platform_metadata={"platform": platform, "binary": {"sha256": platform}},
    )
    return bridge, capture


class FuryBridgePlatformEquivalenceV2Tests(unittest.TestCase):
    def setUp(self):
        FakeDynamicRecordingBridge.instances.clear()

    def test_fixture_binds_two_explicit_health_targets_and_ordered_schedule(self):
        request, config = _fixture()
        self.assertTrue(request["encounter"]["useHealth"])
        self.assertEqual(len(request["encounter"]["targets"]), 2)
        self.assertEqual(
            [target["stats"][34] for target in request["encounter"]["targets"]],
            [1.0, 100000.0],
        )
        self.assertEqual(
            [event.time_ms for event in config.background_damage_events], [10, 20]
        )
        self.assertEqual(config.same_timestamp_order, "BACKGROUND_BEFORE_CANDIDATE")
        self.assertEqual(config.retarget_mode, "NEXT_ALIVE_CYCLIC")

    def test_fake_capture_closes_full_dynamic_contract(self):
        bridge, capture = _capture()
        self.assertTrue(bridge.closed)
        self.assertEqual(capture["schema"], CAPTURE_SCHEMA)
        self.assertEqual(
            [row["request"]["command"] for row in bridge.wire_trace_v2],
            [
                "load_dynamic_v1",
                "wait",
                "advance",
                "dynamic_damage_receipts",
                "dynamic_candidate_damage_receipts",
                "state",
                "actions",
                "act",
                "server_results",
                "dynamic_candidate_damage_receipts",
                "dynamic_damage_receipts",
                "state",
                "close",
            ],
        )
        load_request_wire = bridge.wire_trace_v2[0]["request"]
        self.assertEqual(load_request_wire["seed"], 37)
        self.assertEqual(
            load_request_wire["dynamic"]["content_sha256"],
            bridge.config.content_sha256,
        )
        incoming = capture["physical_trace"]["typed_validated"][3]["result"]
        self.assertEqual(incoming["health_delta"], -200.0)
        self.assertEqual(incoming["rage_delta"], 5.0)
        self.assertEqual(capture["coverage"]["live_target_count"], 1)
        self.assertEqual(capture["coverage"]["total_target_count"], 2)

    def test_capture_rejects_candidate_order_tamper_and_still_closes(self):
        request, config = _fixture()
        bridge = BadOrdinalFakeBridge("bad")
        with self.assertRaisesRegex(
            FuryBridgePlatformEquivalenceV2Error,
            "same-ms candidate cancellation/order",
        ):
            capture_dynamic_trace_v2(
                bridge,
                request,
                config,
                seed=37,
                platform_metadata={"platform": "bad"},
            )
        self.assertTrue(bridge.closed)

    def test_compare_removes_only_platform_metadata_and_detects_physical_tamper(self):
        _, windows = _capture("windows")
        _, linux = _capture("linux")
        passed = compare_platform_captures_v2(windows, linux)
        self.assertEqual(passed["status"], "PASS_EXACT_DYNAMIC_TRACE")
        self.assertEqual(passed["excluded_platform_metadata_paths"], ["$.platform_metadata"])
        self.assertEqual(passed["excluded_physical_result_or_state_fields"], [])

        tampered = copy.deepcopy(linux)
        tampered["physical_trace"]["wire_jsonl"][2]["response"]["state"][
            "health_current"
        ] = 4799.0
        failed = compare_platform_captures_v2(windows, tampered)
        self.assertEqual(failed["status"], "FAIL_TRACE_DIFFERENCE")
        self.assertEqual(
            failed["first_difference"]["path"],
            "$.physical_trace.wire_jsonl[2].response.state.health_current",
        )

    def test_content_addressed_receipt_verifies_and_rejects_tamper(self):
        report = {"schema": "example/v1", "status": "PASS", "value": 3}
        with tempfile.TemporaryDirectory() as raw_directory:
            destination, receipt = publish_content_addressed_receipt_v2(
                report, Path(raw_directory)
            )
            digest = verify_content_addressed_receipt_v2(receipt)
            self.assertIn(digest, destination.name)
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")), receipt
            )
            tampered = copy.deepcopy(receipt)
            tampered["value"] = 4
            with self.assertRaisesRegex(
                FuryBridgePlatformEquivalenceV2Error, "SHA-256 mismatch"
            ):
                verify_content_addressed_receipt_v2(tampered)

    def test_run_equivalence_fake_factory_pins_files_and_both_launches(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            windows = root / "bridge.exe"
            linux = root / "bridge-linux"
            windows.write_bytes(b"windows-v4")
            linux.write_bytes(b"linux-v4")
            report = run_equivalence_v2(
                windows_bridge=windows,
                linux_bridge=linux,
                simulator_root=root,
                expected_windows_sha256=hashlib.sha256(b"windows-v4").hexdigest(),
                expected_linux_sha256=hashlib.sha256(b"linux-v4").hexdigest(),
                bridge_factory=FakeDynamicRecordingBridge,
                seed=37,
            )
        self.assertEqual(report["status"], "PASS_EXACT_DYNAMIC_TRACE")
        self.assertEqual(len(FakeDynamicRecordingBridge.instances), 2)
        self.assertEqual(FakeDynamicRecordingBridge.instances[0].arguments, ())
        self.assertEqual(FakeDynamicRecordingBridge.instances[1].executable, "wsl.exe")
        self.assertIn("--", FakeDynamicRecordingBridge.instances[1].arguments)
        self.assertEqual(
            report["comparison"]["windows_normalized_capture_sha256"],
            report["comparison"]["linux_normalized_capture_sha256"],
        )

    @unittest.skipUnless(__import__("os").name == "nt", "Windows path contract")
    def test_wsl_arguments_keep_space_containing_paths_as_single_arguments(self):
        arguments = linux_wsl_arguments(
            distro="Ubuntu-22.04",
            simulator_root=Path("D:/WOW Tree/wowsims-turtle"),
            linux_bridge=Path("D:/WOW Tree/bin/bridge"),
        )
        self.assertEqual(
            arguments[:4],
            ("-d", "Ubuntu-22.04", "--cd", "/mnt/d/WOW Tree/wowsims-turtle"),
        )
        self.assertEqual(arguments[-2:], ("--", "/mnt/d/WOW Tree/bin/bridge"))

    def test_canonical_json_rejects_nonfinite_values(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaisesRegex(
            FuryBridgePlatformEquivalenceV2Error, "not finite canonical JSON"
        ):
            canonical_bytes({"x": float("nan")})


if __name__ == "__main__":
    unittest.main()
