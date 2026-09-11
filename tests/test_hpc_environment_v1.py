from __future__ import annotations

import base64
import copy
import gzip
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.hpc_environment_v1 import (
    ArtifactIdentity,
    CommandResult,
    HpcEnvironmentError,
    PROJECT_ROOT,
    SchedulerNodeTransport,
    SITE_SCHEMA,
    WslControlTransport,
    apply_node_overrides,
    build_transfer_release,
    classify_transfer_artifact,
    inspect_bridge,
    load_site_config,
    stage_control_release,
    stage_shared_bridge,
    _bootstrap_command,
    _bridge_smoke_payload_base64,
    _bridge_verification_command,
    _remote_release_verification_command,
    _run,
)
from o2o_dps.fury_execution_source_identity_v2 import (
    build_fury_execution_source_identity_v2,
)


EXAMPLE = PROJECT_ROOT / "configs" / "hpc" / "site.example.json"
WRAPPER = PROJECT_ROOT / "scripts" / "hpc_environment_windows.ps1"


class HpcEnvironmentV1Tests(unittest.TestCase):
    def test_binary_stdin_preserves_posix_lf_and_decodes_results(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["tool"], returncode=0, stdout=b"ok\n", stderr=b""
        )
        with patch(
            "o2o_dps.hpc_environment_v1.subprocess.run", return_value=completed
        ) as runner:
            result = _run(
                ("tool",), timeout=7, stdin_utf8="set -eu\nprintf '%s\\n' \"$x\"\n"
            )
        self.assertEqual(result, CommandResult(0, "ok\n", ""))
        kwargs = runner.call_args.kwargs
        self.assertEqual(
            kwargs["input"], b"set -eu\nprintf '%s\\n' \"$x\"\n"
        )
        self.assertNotIn("text", kwargs)

    def test_wsl_control_runs_remote_script_via_binary_stdin(self) -> None:
        site = load_site_config(EXAMPLE)
        transport = WslControlTransport(site)
        with patch(
            "o2o_dps.hpc_environment_v1._run",
            return_value=CommandResult(0, "", ""),
        ) as runner:
            transport.run("printf '%s\\n' \"$remote_only\"", timeout=9)
        args = runner.call_args.args[0]
        self.assertEqual(args[-2:], ("sh", "-s"))
        self.assertEqual(
            runner.call_args.kwargs["stdin_utf8"],
            "printf '%s\\n' \"$remote_only\"\n",
        )

    def test_example_is_credential_free_scheduler_site(self) -> None:
        site = load_site_config(EXAMPLE)
        self.assertEqual(site.node_transport_kind, "scheduler_run_on")
        self.assertEqual(site.control_ssh_alias, "jtl110gpu2")
        self.assertEqual(
            [node.name for node in site.nodes],
            [f"node{index:03d}" for index in range(1, 7)],
        )
        self.assertEqual(site.pilot_workers_per_node, 32)
        self.assertEqual(site.maximum_workers_per_node_before_benchmark, 160)
        text = EXAMPLE.read_text(encoding="utf-8").lower()
        for forbidden in ("password", "private_key", "identityfile", "hostname", "user"):
            self.assertNotIn(forbidden, text)

    def test_unknown_or_secret_site_fields_fail_closed(self) -> None:
        value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        value["control_plane"]["identity_file"] = "secret"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "site.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HpcEnvironmentError, "fields do not match"):
                load_site_config(path)

    def test_paths_cannot_escape_remote_home(self) -> None:
        value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        value["node_shared_root"] = "../outside"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "site.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HpcEnvironmentError, "unsafe path"):
                load_site_config(path)

    def test_scheduler_skill_home_relative_path_cannot_escape(self) -> None:
        value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        value["node_transport"]["scheduler_skill_dir"] = "~/../outside"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "site.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HpcEnvironmentError, "traverse"):
                load_site_config(path)

    def test_node_targets_can_be_overridden_without_adding_nodes(self) -> None:
        site = load_site_config(EXAMPLE)
        changed = apply_node_overrides(site, ["node003=node003-direct"])
        self.assertEqual(changed.nodes[2].transport_name, "node003-direct")
        with self.assertRaisesRegex(HpcEnvironmentError, "not present"):
            apply_node_overrides(site, ["node999=node999"])
        with self.assertRaisesRegex(HpcEnvironmentError, "credential-free"):
            apply_node_overrides(site, ["node001=user@host"])

    def test_scheduler_copy_rejects_remote_home_escape_before_transport(self) -> None:
        site = load_site_config(EXAMPLE)
        transport = SchedulerNodeTransport(site)
        with self.assertRaisesRegex(HpcEnvironmentError, "unsafe path"):
            transport.copy(
                site.nodes[0],
                PROJECT_ROOT / "README.md",
                "~/../escape",
            )

    def test_raw_or_arbitrary_files_never_enter_transfer_release(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            raw = Path(temporary) / "all-activity.csv"
            raw.write_text("raw", encoding="utf-8")
            with self.assertRaisesRegex(HpcEnvironmentError, "four-role"):
                classify_transfer_artifact(raw)

    def test_allowlisted_filename_cannot_disguise_arbitrary_gzip_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capsule = (
                root
                / "offline_data"
                / "derived"
                / "fury_offline_scenario_capsules"
                / "v2"
                / ("fury_offline_scenario_capsules_v2." + "0" * 64 + ".json.gz")
            )
            capsule.parent.mkdir(parents=True)
            with gzip.open(capsule, "wb") as handle:
                handle.write(b"{}")
            with patch("o2o_dps.hpc_environment_v1.PROJECT_ROOT", root):
                with self.assertRaisesRegex(HpcEnvironmentError, "schema/content"):
                    classify_transfer_artifact(capsule)

    def test_release_requires_exactly_four_allowlisted_roles(self) -> None:
        protocol = PROJECT_ROOT / "configs" / "evaluation" / "fury_multiseed_protocol_v2.json"
        with self.assertRaisesRegex(HpcEnvironmentError, "exactly one"):
            build_transfer_release([protocol])

    def _current_release_paths(self):
        protocol = PROJECT_ROOT / "configs" / "evaluation" / "fury_multiseed_protocol_v2.json"
        document = json.loads(protocol.read_text(encoding="utf-8"))
        capsule_pin = document["corpus_contract"]["phase_corpus_bindings"][
            "development"
        ]["scenario_model_capsule"]

        def pinned_path(value: str) -> Path:
            relative = value.replace("%PROJECT_ROOT%\\", "", 1)
            return PROJECT_ROOT / Path(relative)

        bridge = PROJECT_ROOT / "bin" / "o2obridge.seedfix-v2.withdb.goamd64v1.linux-amd64"
        paths = (
            protocol,
            bridge,
            pinned_path(capsule_pin["path"]),
            pinned_path(capsule_pin["static_control_binding"]["path"]),
        )
        return document, bridge, paths

    def test_current_scientific_release_source_closure_is_exact(self) -> None:
        document, bridge, paths = self._current_release_paths()
        pinned_source_sha = document["execution_contract"][
            "python_source_identity_contract"
        ]["canonical_bundle_sha256"]
        live_source_sha = build_fury_execution_source_identity_v2()["canonical_bundle"][
            "sha256"
        ]
        self.assertEqual(pinned_source_sha, live_source_sha)
        release_id, manifest, identities = build_transfer_release(
            paths, expected_bridge=inspect_bridge(bridge)
        )
        self.assertEqual(manifest["release_id"], release_id)
        self.assertEqual(len(identities), 4)

    def test_stale_scientific_release_fails_closed_after_source_change(self) -> None:
        _, bridge, paths = self._current_release_paths()
        tampered_live_identity = copy.deepcopy(
            build_fury_execution_source_identity_v2()
        )
        original = tampered_live_identity["canonical_bundle"]["sha256"]
        tampered_live_identity["canonical_bundle"]["sha256"] = (
            "0" * 64 if original != "0" * 64 else "1" * 64
        )
        with self.assertRaisesRegex(
            HpcEnvironmentError, "live Python execution closure differs"
        ), patch(
            "o2o_dps.fury_multiseed_evaluation_v2."
            "build_fury_execution_source_identity_v2",
            return_value=tampered_live_identity,
        ):
            build_transfer_release(
                paths, expected_bridge=inspect_bridge(bridge)
            )

    def test_remote_commands_are_fail_fast_and_pin_expected_sidecars(self) -> None:
        bootstrap = _bootstrap_command("o2o-dps-hpc")
        self.assertTrue(bootstrap.startswith("set -eu;"))
        expected = {
            "protocol.json": "1" * 64,
            "manifest.json": "2" * 64,
            "SHA256SUMS": "3" * 64,
        }
        verify = _remote_release_verification_command("root/releases/id", expected)
        self.assertTrue(verify.startswith("set -eu;"))
        self.assertIn("find -H", verify)
        for digest in expected.values():
            self.assertIn(digest, verify)
        self.assertNotIn("sha256sum -c SHA256SUMS", verify)

    def test_bridge_smoke_exercises_two_target_server_results(self) -> None:
        command = _bridge_verification_command("root/bridge", "a" * 64)
        messages = [
            json.loads(line)
            for line in base64.b64decode(_bridge_smoke_payload_base64()).splitlines()
        ]
        self.assertTrue(command.startswith("set -eu;"))
        self.assertEqual(
            [message["command"] for message in messages],
            ["load", "actions", "act", "server_results", "close"],
        )
        self.assertEqual(len(messages[0]["request"]["encounter"]["targets"]), 2)
        self.assertEqual(messages[2]["action"], {"spell_id": 1680})
        self.assertEqual(
            messages[3]["attempt_ids"], ["hpc-target-results-smoke"]
        )
        self.assertIn('"target_results":[{"target_index":0', command)
        self.assertIn('"target_index":1', command)
        self.assertIn(
            "REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY", command
        )

    def test_bridge_is_activated_only_after_all_nodes_then_current_is_rechecked(self) -> None:
        site = load_site_config(EXAMPLE)
        bridge_path = PROJECT_ROOT / "bin" / "o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64"
        bridge = inspect_bridge(bridge_path)

        class Transport:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def run(self, node, command, timeout=30):
                del node, timeout
                self.commands.append(command)
                pointer = (
                    f"pointer_target=releases/bridge/{bridge['sha256']}\n"
                    if "pointer_target=" in command
                    else ""
                )
                if (
                    "smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY"
                    in command
                ):
                    return CommandResult(
                        0,
                        f"verified_sha256={bridge['sha256']}\n"
                        "elf_magic=ELF64_X86_64\n"
                        "smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY\n"
                        + pointer,
                        "",
                    )
                return CommandResult(0, "", "")

            def copy(self, node, local_path, remote_path, timeout=180):
                raise AssertionError("identical release must not copy")

        transport = Transport()
        result = stage_shared_bridge(site, transport, bridge_path, bridge)
        self.assertEqual(result["status"], "READY")
        activation = next(
            index for index, command in enumerate(transport.commands) if "mv -Tf" in command
        )
        pre_smokes = [
            index
            for index, command in enumerate(transport.commands)
            if "smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY" in command
            and "/current-bridge/o2obridge.linux-amd64" not in command
        ]
        post_smokes = [
            index
            for index, command in enumerate(transport.commands)
            if "/current-bridge/o2obridge.linux-amd64" in command
        ]
        self.assertEqual(len(pre_smokes), 6)
        self.assertEqual(len(post_smokes), 6)
        self.assertLess(max(pre_smokes), activation)
        self.assertGreater(min(post_smokes), activation)

    def test_failed_bridge_precheck_never_reaches_activation(self) -> None:
        site = load_site_config(EXAMPLE)
        bridge_path = PROJECT_ROOT / "bin" / "o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64"
        bridge = inspect_bridge(bridge_path)

        class Transport:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.smoke_count = 0

            def run(self, node, command, timeout=30):
                del node, timeout
                self.commands.append(command)
                if (
                    "smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY"
                    not in command
                ):
                    return CommandResult(0, "", "")
                self.smoke_count += 1
                if self.smoke_count == 3:
                    return CommandResult(17, "", "simulator_failed")
                return CommandResult(
                    0,
                    f"verified_sha256={bridge['sha256']}\n"
                    "elf_magic=ELF64_X86_64\n"
                    "smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY\n",
                    "",
                )

        transport = Transport()
        result = stage_shared_bridge(site, transport, bridge_path, bridge)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            result["reason"],
            "BRIDGE_FINAL_NOT_VERIFIED_ON_EVERY_NODE_BEFORE_ACTIVATION",
        )
        self.assertFalse(any("mv -Tf" in command for command in transport.commands))

    def test_control_activation_follows_exact_final_verification(self) -> None:
        site = load_site_config(EXAMPLE)
        identities = tuple(
            ArtifactIdentity(role, Path(__file__), name, 1, digest)
            for role, name, digest in (
                ("protocol", "protocol.json", "1" * 64),
                ("static_bridge", "bridge.linux-amd64", "2" * 64),
                ("compact_capsule", "capsule.json.gz", "3" * 64),
                ("compact_binding", "binding.json.gz", "4" * 64),
            )
        )
        manifest = {
            "schema": "o2o_hpc_transfer_release/v1",
            "release_id": "5" * 64,
            "artifacts": [],
        }

        class Transport:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.first_release_probe = True

            def run(self, command, timeout=30):
                del timeout
                self.commands.append(command)
                if "entry_marks=" in command and self.first_release_probe:
                    self.first_release_probe = False
                    return CommandResult(1, "", "missing")
                pointer = (
                    "pointer_target=releases/" + "5" * 64 + "\n"
                    if "pointer_target=" in command
                    else ""
                )
                output = (
                    "release_verification=EXACT_MANIFEST_AND_FILES_READY\n" + pointer
                    if "entry_marks=" in command
                    else ""
                )
                return CommandResult(0, output, "")

            def copy(self, local_path, remote_path, timeout=120):
                del local_path, remote_path, timeout
                return CommandResult(0, "", "")

        transport = Transport()
        with patch(
            "o2o_dps.hpc_environment_v1.build_transfer_release",
            return_value=("5" * 64, manifest, identities),
        ):
            result = stage_control_release(site, transport, ())
        self.assertEqual(result["status"], "READY")
        activation = next(
            index for index, command in enumerate(transport.commands) if "mv -Tf" in command
        )
        exact_prechecks = [
            index
            for index, command in enumerate(transport.commands[:activation])
            if "EXACT_MANIFEST_AND_FILES_READY" in command
        ]
        self.assertTrue(exact_prechecks)
        self.assertTrue(
            any("pointer_target=" in command for command in transport.commands[activation + 1 :])
        )

    def test_invalid_existing_control_release_is_not_trusted_or_replaced(self) -> None:
        site = load_site_config(EXAMPLE)
        identity = ArtifactIdentity(
            "protocol", Path(__file__), "protocol.json", 1, "1" * 64
        )
        manifest = {
            "schema": "o2o_hpc_transfer_release/v1",
            "release_id": "5" * 64,
            "artifacts": [],
        }

        class Transport:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.copies = 0

            def run(self, command, timeout=30):
                del timeout
                self.commands.append(command)
                return CommandResult(9, "", "immutable_conflict")

            def copy(self, local_path, remote_path, timeout=120):
                del local_path, remote_path, timeout
                self.copies += 1
                return CommandResult(0, "", "")

        transport = Transport()
        with patch(
            "o2o_dps.hpc_environment_v1.build_transfer_release",
            return_value=("5" * 64, manifest, (identity,)),
        ):
            result = stage_control_release(site, transport, ())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            result["reason"], "CONTROL_EXISTING_IMMUTABLE_RELEASE_INVALID"
        )
        self.assertEqual(transport.copies, 0)
        self.assertFalse(any("mv -Tf" in command for command in transport.commands))

    def test_wrapper_defaults_to_dry_run_and_only_apply_switch_mutates(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("[switch]$Apply", source)
        self.assertIn("if ($Apply)", source)
        self.assertIn("$arguments += '--apply'", source)
        self.assertNotIn("--apply'", source.split("if ($Apply)", 1)[0])

    def test_site_schema_constant_is_versioned(self) -> None:
        self.assertEqual(SITE_SCHEMA, "o2o_hpc_site/v1")


if __name__ == "__main__":
    unittest.main()
