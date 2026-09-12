from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.hpc_dynamic_environment_v5 import (
    DEFAULT_BRIDGE,
    DEFAULT_CONTRACT,
    EXPECTED_BRIDGE_SHA256,
    HpcDynamicEnvironmentV5Error,
    build_dynamic_probe_payload_v5,
    load_dynamic_v5_contract,
    parse_probe_v5,
    publish_receipt_v5,
    remote_probe_command_v5,
    stage_dynamic_v5_bridge,
    validate_site_v5,
    verify_local_v11_bridge,
    verify_receipt_v5,
)
from o2o_dps.hpc_environment_v1 import CommandResult, PROJECT_ROOT, load_site_config


EXAMPLE_SITE = PROJECT_ROOT / "configs" / "hpc" / "site.example.json"
WRAPPER = PROJECT_ROOT / "scripts" / "hpc_dynamic_environment_v5_windows.ps1"


def _summary(node: str, metadata: dict) -> dict:
    return {
        "schema": "o2o_hpc_dynamic_probe_summary/v5",
        "status": "READY",
        "hostname": node,
        "bridge_sha256": EXPECTED_BRIDGE_SHA256,
        "restoration_config_sha256": metadata["restoration_config_sha256"],
        "horizon_config_sha256": metadata["horizon_config_sha256"],
        "attackable_horizon_verified": True,
        "process_count": 1,
        "response_sha256": "a" * 64,
        "restored_at_ms": metadata["restored_at_ms"],
        "horizon_ms": metadata["horizon_ms"],
        "failures": [],
    }


def _ready_loads() -> list[dict]:
    return [
        {
            "name": f"node{index:03d}",
            "status": "READY",
            "logical_cpus": 192,
            "load1": 10.0,
            "headroom_by_load1": 182.0,
            "v4_pointer_target": "releases/dynamic-v4/old",
            "transport_returncode": 0,
        }
        for index in range(1, 7)
    ]


class HpcDynamicEnvironmentV5Tests(unittest.TestCase):
    def test_contract_binds_v11_route_and_one_process_per_node(self):
        contract = load_dynamic_v5_contract(DEFAULT_CONTRACT)
        self.assertEqual(contract.horizon_ms, 8059)
        self.assertEqual(contract.restored_at_ms, 100)
        self.assertEqual(contract.target_health, 1000000.0)
        validate_site_v5(load_site_config(EXAMPLE_SITE))
        identity = verify_local_v11_bridge(DEFAULT_BRIDGE)
        self.assertEqual(identity["sha256"], EXPECTED_BRIDGE_SHA256)
        self.assertFalse(identity["pt_interp"])

    def test_payload_uses_real_dynamic_v3_restoration_and_horizon(self):
        contract = load_dynamic_v5_contract(DEFAULT_CONTRACT)
        payload, metadata = build_dynamic_probe_payload_v5(contract)
        messages = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(len(messages), 13)
        self.assertEqual(messages[0]["command"], "load_dynamic_v3")
        self.assertEqual(messages[7]["command"], "load_dynamic_v3")
        self.assertNotIn("wait", [row["command"] for row in messages[:7]])
        self.assertNotIn("act", [row["command"] for row in messages])
        first = messages[0]["dynamic"]
        second = messages[7]["dynamic"]
        self.assertEqual(first["content_sha256"], metadata["restoration_config_sha256"])
        self.assertEqual(second["content_sha256"], metadata["horizon_config_sha256"])
        self.assertEqual(first["attackability_events"][-1]["time_ms"], 100)
        self.assertEqual(second["idle_advance_horizon_ms"], 8059)
        self.assertEqual(messages[8], {"command": "wait", "wait_ms": 8159})
        self.assertEqual(messages[9], {"command": "advance"})
        self.assertEqual(second["attackability_events"], [])
        command = remote_probe_command_v5(
            "release/o2obridge",
            payload,
            contract,
            metadata,
            python_executable="node/python",
        )
        self.assertIn("node/python", command)
        self.assertIn("GOMAXPROCS=1", command)
        self.assertNotIn("GOMAXPROCS=32", command)
        self.assertNotIn("fury_multiseed", command)

    def test_probe_requires_exact_v11_and_one_process(self):
        _, metadata = build_dynamic_probe_payload_v5(load_dynamic_v5_contract())
        good = _summary("node001", metadata)
        result = CommandResult(0, "O2O_DYNAMIC_V5_PROBE=" + json.dumps(good) + "\n", "")
        self.assertEqual(parse_probe_v5(result, "node001", metadata)["status"], "READY")
        bad = copy.deepcopy(good)
        bad["process_count"] = 2
        failed = parse_probe_v5(
            CommandResult(0, "O2O_DYNAMIC_V5_PROBE=" + json.dumps(bad) + "\n", ""),
            "node001",
            metadata,
        )
        self.assertEqual(failed["status"], "BLOCKED")

    def test_stage_smokes_six_nodes_before_new_pointer_and_preserves_v4(self):
        contract = load_dynamic_v5_contract()
        site = load_site_config(EXAMPLE_SITE)
        payload, metadata = build_dynamic_probe_payload_v5(contract)

        class Transport:
            def __init__(self):
                self.commands: list[tuple[str, str]] = []

            def run(self, node, command, timeout=30):
                del timeout
                self.commands.append((node.name, command))
                if "printf 'state=" in command:
                    return CommandResult(0, "state=EXISTS\n", "")
                if site.node_python in command:
                    summary = _summary(node.name, metadata)
                    return CommandResult(0, "O2O_DYNAMIC_V5_PROBE=" + json.dumps(summary) + "\n", "")
                if "logical_cpus=" in command:
                    return CommandResult(0, f"hostname={node.name}\nlogical_cpus=192\nload1=10\nv4_pointer_target=releases/dynamic-v4/old\n", "")
                if "verified_sha256=" in command:
                    pointer = (
                        f"pointer_target=releases/dynamic-v5/{EXPECTED_BRIDGE_SHA256}\n"
                        if "pointer_target=$(readlink" in command
                        else ""
                    )
                    return CommandResult(0, f"verified_sha256={EXPECTED_BRIDGE_SHA256}\n" + pointer, "")
                return CommandResult(0, "", "")

            def copy(self, *args, **kwargs):
                raise AssertionError("existing exact release must not be copied")

        transport = Transport()
        result = stage_dynamic_v5_bridge(
            site,
            transport,
            DEFAULT_BRIDGE,
            payload,
            contract,
            metadata,
            _ready_loads(),
        )
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["v4_pointer_unchanged"])
        smoke_positions = [i for i, (_, cmd) in enumerate(transport.commands) if site.node_python in cmd]
        activation = next(i for i, (_, cmd) in enumerate(transport.commands) if "mv -Tf" in cmd)
        self.assertEqual(len(smoke_positions), 6)
        self.assertLess(max(smoke_positions), activation)
        for _, command in transport.commands:
            if "mv -Tf" in command:
                self.assertIn("current-bridge-dynamic-v5", command)
                self.assertNotIn("current-bridge-dynamic-v4", command)

    def test_failed_load_guard_makes_no_remote_mutation(self):
        contract = load_dynamic_v5_contract()
        site = load_site_config(EXAMPLE_SITE)
        payload, metadata = build_dynamic_probe_payload_v5(contract)
        loads = _ready_loads()
        loads[3]["status"] = "BLOCKED"

        class Transport:
            def run(self, *args, **kwargs):
                raise AssertionError("load guard must stop before staging")

            def copy(self, *args, **kwargs):
                raise AssertionError("load guard must stop before copy")

        result = stage_dynamic_v5_bridge(site, Transport(), DEFAULT_BRIDGE, payload, contract, metadata, loads)
        self.assertEqual(result, {"status": "BLOCKED", "reason": "NODE_LOAD_HEADROOM_NOT_READY"})

    def test_receipt_is_content_addressed_and_wrapper_is_v5_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, receipt = publish_receipt_v5(
                {"schema": "o2o_hpc_dynamic_environment_receipt/v5", "status": "READY"},
                Path(temporary),
            )
            digest = verify_receipt_v5(receipt)
            self.assertIn(digest, path.name)
            changed = copy.deepcopy(receipt)
            changed["status"] = "BLOCKED"
            with self.assertRaises(HpcDynamicEnvironmentV5Error):
                verify_receipt_v5(changed)
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("hpc_dynamic_environment_v5", wrapper)
        self.assertIn("seedfix-v11", wrapper)
        self.assertNotIn("dynamic_v4", wrapper)
        self.assertNotIn("seedfix-v4", wrapper)


if __name__ == "__main__":
    unittest.main()
