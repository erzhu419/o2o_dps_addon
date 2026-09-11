from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.hpc_dynamic_environment_v4 import (
    CONTRACT_SCHEMA,
    DEFAULT_BRIDGE,
    DEFAULT_CONTRACT,
    EXPECTED_BRIDGE_SHA256,
    RECEIPT_SCHEMA,
    CommandResult,
    HpcDynamicEnvironmentV4Error,
    build_dynamic_probe_payload_v4,
    canonical_bytes,
    load_dynamic_v4_contract,
    parse_probe_summary_v4,
    publish_receipt_v4,
    remote_probe_command_v4,
    run_dynamic_v4_pilot,
    stage_dynamic_v4_bridge,
    validate_site_binding_v4,
    verify_local_v4_bridge,
    verify_receipt_v4,
)
from o2o_dps.hpc_environment_v1 import PROJECT_ROOT, load_site_config


EXAMPLE_SITE = PROJECT_ROOT / "configs" / "hpc" / "site.example.json"
WRAPPER = PROJECT_ROOT / "scripts" / "hpc_dynamic_environment_v4_windows.ps1"


def _success_summary(mode: str, workers: int) -> dict:
    return {
        "schema": "o2o_hpc_dynamic_probe_summary/v4",
        "mode": mode,
        "status": "READY",
        "hostname": "node001",
        "bridge_sha256": EXPECTED_BRIDGE_SHA256,
        "dynamic_config_sha256": "a" * 64,
        "worker_count": workers,
        "exit_zero_count": workers,
        "validated_count": workers,
        "deterministic_workload_sha256": "b" * 64,
        "max_rss_kib": 123456,
        "elapsed_ms": 789,
        "failures": [],
    }


class HpcDynamicEnvironmentV4Tests(unittest.TestCase):
    def test_tracked_contract_is_exact_v4_32_by_6_and_site_bound(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        self.assertEqual(contract.schema, CONTRACT_SCHEMA)
        self.assertEqual(contract.bridge_sha256, EXPECTED_BRIDGE_SHA256)
        self.assertEqual(contract.workers_per_node, 32)
        self.assertEqual(contract.total_workers, 192)
        self.assertEqual(contract.gomaxprocs_per_worker, 1)
        self.assertEqual(
            contract.nodes, tuple(f"node{index:03d}" for index in range(1, 7))
        )
        validate_site_binding_v4(load_site_config(EXAMPLE_SITE), contract)

    def test_local_binary_is_exact_static_v4_and_v3_is_rejected(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        identity = verify_local_v4_bridge(DEFAULT_BRIDGE, contract)
        self.assertEqual(identity["sha256"], EXPECTED_BRIDGE_SHA256)
        self.assertFalse(identity["pt_interp"])
        v3 = (
            PROJECT_ROOT
            / "bin"
            / "o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64"
        )
        with self.assertRaisesRegex(
            HpcDynamicEnvironmentV4Error, "filename.*v4"
        ):
            verify_local_v4_bridge(v3, contract)

    def test_dynamic_payload_is_real_load_dynamic_two_target_contract(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        payload, metadata = build_dynamic_probe_payload_v4(contract)
        messages = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(
            [message["command"] for message in messages],
            [
                "load_dynamic_v1",
                "wait",
                "advance",
                "dynamic_damage_receipts",
                "dynamic_candidate_damage_receipts",
                "state",
                "close",
            ],
        )
        load = messages[0]
        self.assertEqual(load["seed"], contract.seed_base)
        self.assertEqual(len(load["request"]["encounter"]["targets"]), 2)
        self.assertTrue(load["request"]["encounter"]["useHealth"])
        self.assertEqual(
            [target["stats"][34] for target in load["request"]["encounter"]["targets"]],
            [1.0, 100000.0],
        )
        self.assertEqual(load["dynamic"]["content_sha256"], metadata["dynamic_config_sha256"])
        self.assertEqual(load["dynamic"]["same_timestamp_order"], "BACKGROUND_BEFORE_CANDIDATE")

    def test_remote_probe_command_pins_one_cpu_per_worker_and_no_scientific_inputs(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        payload, metadata = build_dynamic_probe_payload_v4(contract)
        command = remote_probe_command_v4(
            "root/current-bridge-dynamic-v4/o2obridge.linux-amd64",
            mode="pilot",
            workers=32,
            payload=payload,
            contract=contract,
            payload_metadata=metadata,
        )
        self.assertTrue(command.startswith("set -eu;"))
        self.assertIn("probe_mode=pilot", command)
        self.assertIn("probe_workers=32", command)
        self.assertIn("GOMAXPROCS", command)
        self.assertNotIn("offline_data", command)
        self.assertNotIn("fury_multiseed", command)

    def test_probe_summary_requires_exact_worker_accounting_and_digests(self):
        summary = _success_summary("pilot", 32)
        result = CommandResult(
            0,
            "O2O_DYNAMIC_V4_PROBE=" + json.dumps(summary, separators=(",", ":")) + "\n",
            "",
        )
        parsed = parse_probe_summary_v4(
            result,
            node_name="node001",
            mode="pilot",
            workers=32,
            bridge_sha256=EXPECTED_BRIDGE_SHA256,
            dynamic_config_sha256="a" * 64,
        )
        self.assertEqual(parsed["status"], "READY")
        self.assertEqual(parsed["summary"]["validated_count"], 32)
        self.assertRegex(parsed["summary_sha256"], r"^[0-9a-f]{64}$")

        bad = copy.deepcopy(summary)
        bad["exit_zero_count"] = 31
        failed = parse_probe_summary_v4(
            CommandResult(0, "O2O_DYNAMIC_V4_PROBE=" + json.dumps(bad) + "\n", ""),
            node_name="node001",
            mode="pilot",
            workers=32,
            bridge_sha256=EXPECTED_BRIDGE_SHA256,
            dynamic_config_sha256="a" * 64,
        )
        self.assertEqual(failed["status"], "BLOCKED")
        self.assertEqual(failed["reason"], "PROBE_SUMMARY_OR_WORKER_ACCOUNTING_FAILED")

    def test_nonzero_transport_is_blocked_but_preserves_bounded_diagnostics(self):
        result = CommandResult(17, "", "worker_failed: detail")
        parsed = parse_probe_summary_v4(
            result,
            node_name="node003",
            mode="smoke",
            workers=1,
            bridge_sha256=EXPECTED_BRIDGE_SHA256,
            dynamic_config_sha256="a" * 64,
        )
        self.assertEqual(parsed["status"], "BLOCKED")
        self.assertEqual(parsed["transport_returncode"], 17)
        self.assertIn("worker_failed", parsed["diagnostic_stderr_tail"])

    def test_receipt_is_content_addressed_and_tamper_evident(self):
        report = {
            "schema": RECEIPT_SCHEMA,
            "status": "DYNAMIC_V4_ENVIRONMENT_READY_NONSCIENTIFIC",
            "scientific_experiment_started": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path, receipt = publish_receipt_v4(report, Path(temporary))
            digest = verify_receipt_v4(receipt)
            self.assertIn(digest, path.name)
            tampered = copy.deepcopy(receipt)
            tampered["scientific_experiment_started"] = True
            with self.assertRaisesRegex(HpcDynamicEnvironmentV4Error, "SHA-256 mismatch"):
                verify_receipt_v4(tampered)

    def test_stage_uses_v4_pointer_only_and_smokes_all_nodes_before_activation(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        site = load_site_config(EXAMPLE_SITE)
        identity = verify_local_v4_bridge(DEFAULT_BRIDGE, contract)
        payload, metadata = build_dynamic_probe_payload_v4(contract)

        class Transport:
            def __init__(self):
                self.commands = []

            def run(self, node, command, timeout=30):
                del timeout
                self.commands.append((node.name, command))
                if "release_state=" in command:
                    return CommandResult(0, "release_state=EXISTS\n", "")
                if "probe_mode=smoke" in command:
                    summary = _success_summary("smoke", 1)
                    summary["hostname"] = node.name
                    summary["dynamic_config_sha256"] = metadata["dynamic_config_sha256"]
                    return CommandResult(
                        0,
                        "O2O_DYNAMIC_V4_PROBE=" + json.dumps(summary) + "\n",
                        "",
                    )
                if "v4_hash_status=" in command:
                    pointer = (
                        f"pointer_target=releases/dynamic-v4/{EXPECTED_BRIDGE_SHA256}\n"
                        if "printf 'pointer_target=" in command
                        else ""
                    )
                    return CommandResult(
                        0,
                        f"verified_sha256={EXPECTED_BRIDGE_SHA256}\n"
                        "v4_hash_status=EXACT_ELF64_X86_64_READY\n"
                        + pointer,
                        "",
                    )
                return CommandResult(0, "", "")

            def copy(self, *args, **kwargs):
                raise AssertionError("identical v4 release must not be copied")

        transport = Transport()
        result = stage_dynamic_v4_bridge(
            site,
            transport,
            DEFAULT_BRIDGE,
            identity,
            payload=payload,
            contract=contract,
            payload_metadata=metadata,
        )
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["legacy_v3_pointer_mutated"])
        activation = next(
            index
            for index, (_, command) in enumerate(transport.commands)
            if "mv -Tf" in command
        )
        smokes = [
            index
            for index, (_, command) in enumerate(transport.commands)
            if "probe_mode=smoke" in command
        ]
        self.assertEqual(len(smokes), 6)
        self.assertLess(max(smokes), activation)
        for _, command in transport.commands:
            if "current-bridge" in command:
                self.assertIn("current-bridge-dynamic-v4", command)

    def test_one_failed_smoke_blocks_activation_and_preserves_all_six_results(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        site = load_site_config(EXAMPLE_SITE)
        identity = verify_local_v4_bridge(DEFAULT_BRIDGE, contract)
        payload, metadata = build_dynamic_probe_payload_v4(contract)

        class Transport:
            def __init__(self):
                self.commands = []

            def run(self, node, command, timeout=30):
                del timeout
                self.commands.append(command)
                if "release_state=" in command:
                    return CommandResult(0, "release_state=EXISTS\n", "")
                if "probe_mode=smoke" in command:
                    summary = _success_summary("smoke", 1)
                    summary["hostname"] = node.name
                    summary["dynamic_config_sha256"] = metadata["dynamic_config_sha256"]
                    if node.name == "node003":
                        summary["status"] = "BLOCKED"
                        summary["exit_zero_count"] = 0
                        summary["validated_count"] = 0
                        summary["failures"] = [{"worker_index": 0, "error": "bad"}]
                        return CommandResult(2, "O2O_DYNAMIC_V4_PROBE=" + json.dumps(summary) + "\n", "bad")
                    return CommandResult(0, "O2O_DYNAMIC_V4_PROBE=" + json.dumps(summary) + "\n", "")
                if "v4_hash_status=" in command:
                    return CommandResult(
                        0,
                        f"verified_sha256={EXPECTED_BRIDGE_SHA256}\n"
                        "v4_hash_status=EXACT_ELF64_X86_64_READY\n",
                        "",
                    )
                if "mv -Tf" in command:
                    raise AssertionError("failed smoke must block activation")
                return CommandResult(0, "", "")

            def copy(self, *args, **kwargs):
                raise AssertionError("identical v4 release must not be copied")

        transport = Transport()
        result = stage_dynamic_v4_bridge(
            site,
            transport,
            DEFAULT_BRIDGE,
            identity,
            payload=payload,
            contract=contract,
            payload_metadata=metadata,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "DYNAMIC_V4_SMOKE_FAILED_BEFORE_ACTIVATION")
        self.assertEqual(len(result["smoke"]), 6)
        self.assertEqual(result["smoke"][2]["status"], "BLOCKED")
        self.assertFalse(any("mv -Tf" in command for command in transport.commands))

    def test_pilot_requires_192_validated_exits_and_one_cross_node_hash(self):
        contract = load_dynamic_v4_contract(DEFAULT_CONTRACT)
        site = load_site_config(EXAMPLE_SITE)
        payload, metadata = build_dynamic_probe_payload_v4(contract)

        class Transport:
            def run(self, node, command, timeout=30):
                del command, timeout
                summary = _success_summary("pilot", 32)
                summary["hostname"] = node.name
                summary["dynamic_config_sha256"] = metadata["dynamic_config_sha256"]
                return CommandResult(
                    0,
                    "O2O_DYNAMIC_V4_PROBE=" + json.dumps(summary) + "\n",
                    "",
                )

        result = run_dynamic_v4_pilot(
            site,
            Transport(),
            bridge_path="root/current-bridge-dynamic-v4/o2obridge.linux-amd64",
            payload=payload,
            contract=contract,
            payload_metadata=metadata,
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["exit_zero_total"], 192)
        self.assertEqual(result["validated_total"], 192)
        self.assertTrue(result["cross_node_workload_hash_consistent"])
        self.assertEqual(len(result["nodes"]), 6)

    def test_wrapper_defaults_are_v4_only_and_apply_is_explicit(self):
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("hpc_dynamic_environment_v4", source)
        self.assertIn("seedfix-v4", source)
        self.assertNotIn("seedfix-v3", source)
        self.assertIn("[switch]$Apply", source)
        self.assertIn("if ($Apply)", source)
        self.assertNotIn("--apply'", source.split("if ($Apply)", 1)[0])

    def test_contract_content_is_strict_and_credential_free(self):
        source = DEFAULT_CONTRACT.read_text(encoding="utf-8").lower()
        for forbidden in ("password", "private_key", "identityfile", "hostname", "user"):
            self.assertNotIn(forbidden, source)
        value = json.loads(source)
        value["secret"] = "forbidden"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HpcDynamicEnvironmentV4Error, "fields"):
                load_dynamic_v4_contract(path)


if __name__ == "__main__":
    unittest.main()
