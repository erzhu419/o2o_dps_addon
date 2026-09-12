from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import o2o_dps.deployed_contra_source_manifest_v1 as deployed_source
from o2o_dps.deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    build_deployed_contra_runtime_binding_v1,
    validate_deployed_contra_runtime_binding_v1,
)
from o2o_dps.deployed_contra_source_manifest_v1 import (
    build_deployed_contra_source_manifest_v1,
)
from o2o_dps.fury_expert_runtime_snapshot_v1 import (
    SCHEMA as SNAPSHOT_SCHEMA,
    sha256_json,
)


def _toc() -> bytes:
    return (
        "## Interface: 11200\n"
        "## Title: Contra魂斗罗\n"
        "## Author: 音十月-卡拉赞\n"
        "## Version: 0.0.5\n"
        "## Notes: Contra魂斗罗4.0.1\n"
        "## DefaultState: Enabled\n"
        "## LoadOnDemand: 0\n"
        "## SavedVariablesPerCharacter: ContraDB\n\n"
        "Contra_ALL.lua\n"
    ).encode("utf-8")


def _payloads() -> dict[str, bytes]:
    readable = ("\n".join(deployed_source._SOURCE_ANCHORS.values()) + "\n").encode(
        "utf-8"
    )
    return {
        "Bindings.xml": b"<Bindings />\n",
        "Contra.toc": _toc(),
        "Contra.lua": b"function ContraTest() end; Contra = {}\n",
        "Contra_ALL.lua": readable,
    }


def _identities(payloads: dict[str, bytes]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in payloads.items()
    }


def _manifest(root: Path) -> dict[str, object]:
    source = root / "Contra"
    source.mkdir(parents=True)
    payloads = _payloads()
    for name, payload in payloads.items():
        (source / name).write_bytes(payload)
    with patch.object(
        deployed_source, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
    ):
        return build_deployed_contra_source_manifest_v1(source)


def _snapshot(*, uppercase_runtime_gates: bool = False) -> dict[str, object]:
    buttons: dict[str, object] = {
        "fangan": "方案1",
        "mode": "副本模式",
        "autoselect": False,
        "xuanfeng": False,
        "baofa": True,
        "shengcun": True,
        "silie": True,
        "quanbudaduan": False,
        "zhidingdaduan": False,
        "liunudaduan": False,
        "bossothuanwuqi": False,
        "xiaoguaiothuanwuqi": False,
    }
    if uppercase_runtime_gates:
        buttons.update({"Burst": True, "Survive": True, "interrupt": True})
    document: dict[str, object] = {
        "schema": SNAPSHOT_SCHEMA,
        "authority": {"comparison_eligible": False},
        "character_context": {
            "status": "BOUND_SAME_CHARACTER_DIRECTORY_AND_BUILD_CAPTURE",
            "character_context_id": "a" * 64,
            "build_capture_semantic_sha256": "b" * 64,
            "character_directory": "CharacterOne",
            "realm_directory": "RealmOne",
            "player_guid": "0x00000000000000A1",
        },
        "fixed_character_build": {"name": "FuryResearchCharacter"},
        "contra_current_profile": {
            "buttons": buttons,
            "adapter_core_projection": {
                "burst": True,
                "survival": True,
                "interrupt_enabled": False,
            },
            "buttons_semantic_sha256": "1" * 64,
            "selected_saved_scheme_sha256": "2" * 64,
            # Deliberately opposite: runtime must consume Buttons, not this copy.
            "selected_saved_scheme": {"xuanfeng": True, "baofa": False},
        },
        "inputs": {
            "contra_savedvariables": {"sha256": "3" * 64, "size_bytes": 100},
            "nampower_dll": {"sha256": "4" * 64, "size_bytes": 829_952},
        },
        "nampower_cvars": {
            "NP_QueueChannelingSpells": "0",
            "NP_QueueTargetingSpells": "0",
            "NP_QueueOnSwingSpells": "1",
            "NP_QueueSpellsOnCooldown": "0",
            "NP_RetryServerRejectedSpells": "0",
        },
        "nampower_cvars_semantic_sha256": "5" * 64,
    }
    document["snapshot_sha256"] = sha256_json(document)
    return document


class DeployedContraRuntimeBindingV1Tests(unittest.TestCase):
    def test_binding_uses_current_buttons_and_exact_case_sensitive_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = build_deployed_contra_runtime_binding_v1(
                source_manifest=_manifest(Path(directory)),
                runtime_snapshot=_snapshot(),
            )

        profile = result["runtime_profile"]
        self.assertFalse(profile["xuanfeng"])
        self.assertFalse(profile["burst"])
        self.assertFalse(profile["survival"])
        self.assertTrue(profile["legacy_lowercase_baofa"])
        self.assertTrue(profile["legacy_lowercase_shengcun"])
        self.assertEqual(
            set(
                result["runtime_profile_diagnostics"][
                    "legacy_projection_disagreements"
                ]
            ),
            {"burst", "survival"},
        )
        self.assertTrue(
            result["runtime_profile_diagnostics"][
                "selected_saved_scheme_is_not_substituted_for_buttons"
            ]
        )
        self.assertTrue(
            result["authority_boundary"]["current_buttons_consumed"]
        )
        self.assertTrue(
            result["authority_boundary"]["same_character_configuration_bound"]
        )
        self.assertFalse(result["authority_boundary"]["comparison_ready"])

    def test_present_uppercase_runtime_gates_are_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = build_deployed_contra_runtime_binding_v1(
                source_manifest=_manifest(Path(directory)),
                runtime_snapshot=_snapshot(uppercase_runtime_gates=True),
            )

        self.assertTrue(result["runtime_profile"]["burst"])
        self.assertTrue(result["runtime_profile"]["survival"])
        self.assertTrue(result["runtime_profile"]["interrupt_enabled"])
        disagreements = result["runtime_profile_diagnostics"][
            "legacy_projection_disagreements"
        ]
        self.assertEqual(disagreements, {"interrupt_enabled": {
            "legacy_projection": False,
            "source_exact": True,
        }})

    def test_queue_cvar_drift_gets_a_distinct_incomplete_binding(self) -> None:
        baseline_snapshot = _snapshot()
        snapshot = _snapshot()
        snapshot["nampower_cvars"]["NP_QueueSpellsOnCooldown"] = "1"
        snapshot["nampower_cvars_semantic_sha256"] = "6" * 64
        snapshot["snapshot_sha256"] = sha256_json(
            {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(Path(directory))
            baseline = build_deployed_contra_runtime_binding_v1(
                source_manifest=manifest,
                runtime_snapshot=baseline_snapshot,
            )
            changed = build_deployed_contra_runtime_binding_v1(
                source_manifest=manifest,
                runtime_snapshot=snapshot,
            )

        self.assertNotEqual(baseline["binding_sha256"], changed["binding_sha256"])
        self.assertTrue(changed["adapter_inputs"]["queue_spells_on_cooldown"])
        self.assertFalse(changed["nampower"]["matches_source_initialization"])
        self.assertFalse(
            changed["authority_boundary"][
                "configuration_complete_for_source_derived_simulator"
            ]
        )
        self.assertEqual(
            validate_deployed_contra_runtime_binding_v1(changed), changed
        )

    def test_snapshot_content_tamper_is_rejected(self) -> None:
        snapshot = _snapshot()
        snapshot["contra_current_profile"]["buttons"]["xuanfeng"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                DeployedContraRuntimeBindingError, "snapshot_sha256 mismatch"
            ):
                build_deployed_contra_runtime_binding_v1(
                    source_manifest=_manifest(Path(directory)),
                    runtime_snapshot=snapshot,
                )

    def test_source_manifest_cannot_overclaim_public_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _manifest(Path(directory))
            changed = deepcopy(manifest)
            changed["authority_boundary"]["public_macro_entry_verified"] = True
            unhashed = {
                key: value for key, value in changed.items() if key != "manifest_sha256"
            }
            changed["manifest_sha256"] = sha256_json(unhashed)
            with self.assertRaisesRegex(
                DeployedContraRuntimeBindingError,
                "overclaims public_macro_entry_verified",
            ):
                build_deployed_contra_runtime_binding_v1(
                    source_manifest=changed,
                    runtime_snapshot=_snapshot(),
                )


if __name__ == "__main__":
    unittest.main()
