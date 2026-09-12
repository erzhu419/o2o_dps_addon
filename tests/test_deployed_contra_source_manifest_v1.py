from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import o2o_dps.deployed_contra_source_manifest_v1 as deployed
from o2o_dps.deployed_contra_source_manifest_v1 import (
    DeployedContraSourceManifestError,
    build_deployed_contra_source_manifest_v1,
    verify_deployed_contra_archive_equivalence_v1,
    verify_deployed_contra_source_v1,
)


def _toc(load: str = "Contra_ALL.lua") -> bytes:
    return (
        "## Interface: 11200\n"
        "## Title: Contra魂斗罗\n"
        "## Author: 音十月-卡拉赞\n"
        "## Version: 0.0.5\n"
        "## Notes: Contra魂斗罗4.0.1\n"
        "## DefaultState: Enabled\n"
        "## LoadOnDemand: 0\n"
        "## SavedVariablesPerCharacter: ContraDB\n\n"
        f"{load}\n"
    ).encode("utf-8")


def _readable_source() -> bytes:
    return ("\n".join(deployed._SOURCE_ANCHORS.values()) + "\n").encode("utf-8")


def _payloads(*, toc_load: str = "Contra_ALL.lua") -> dict[str, bytes]:
    return {
        "Bindings.xml": b"<Bindings />\n",
        "Contra.toc": _toc(toc_load),
        "Contra.lua": b"function ContraTest() end; Contra = {}\n",
        "Contra_ALL.lua": _readable_source(),
    }


def _identities(payloads: dict[str, bytes]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in payloads.items()
    }


def _write_tree(root: Path, payloads: dict[str, bytes]) -> Path:
    source = root / "Contra"
    source.mkdir(parents=True)
    for name, payload in payloads.items():
        (source / name).write_bytes(payload)
    return source


def _write_zip(path: Path, payloads: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as handle:
        for name, payload in payloads.items():
            handle.writestr(f"Contra/{name}", payload)
    return path


class DeployedContraSourceManifestV1Tests(unittest.TestCase):
    def test_build_pins_loaded_file_and_keeps_runtime_claims_false(self) -> None:
        payloads = _payloads()
        with tempfile.TemporaryDirectory() as directory:
            source = _write_tree(Path(directory), payloads)
            with patch.object(
                deployed, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
            ):
                manifest = build_deployed_contra_source_manifest_v1(source)

        self.assertEqual(manifest["toc"]["load_order"], ["Contra_ALL.lua"])
        self.assertEqual(
            manifest["identity"]["loaded_closure"]["entries"][1][
                "relative_path"
            ],
            "Contra_ALL.lua",
        )
        self.assertFalse(
            manifest["identity"]["dormant_same_folder_lua"]["loaded_by_toc"]
        )
        boundary = manifest["authority_boundary"]
        self.assertTrue(boundary["source_identity_verified"])
        self.assertFalse(boundary["public_macro_entry_verified"])
        self.assertFalse(boundary["client_execution_observed"])
        self.assertFalse(boundary["comparison_ready"])

    def test_archive_verification_proves_loaded_closure_byte_equivalence_only(
        self,
    ) -> None:
        payloads = _payloads()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_tree(root, payloads)
            archive = _write_zip(root / "Contra_pirate.zip", payloads)
            with patch.object(
                deployed, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
            ):
                result = verify_deployed_contra_source_v1(
                    source_root=source, archive_path=archive
                )

        self.assertTrue(result["archive_checked"])
        self.assertTrue(result["toc_loaded_source_byte_equivalent"])
        self.assertTrue(result["source_identity_verified"])
        self.assertFalse(result["public_macro_entry_verified"])
        self.assertFalse(result["client_execution_observed"])
        self.assertFalse(result["comparison_ready"])

    def test_archive_loaded_source_tamper_fails_closed(self) -> None:
        payloads = _payloads()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_tree(root, payloads)
            changed = dict(payloads)
            changed["Contra_ALL.lua"] += b"-- changed\n"
            archive = _write_zip(root / "Contra_pirate.zip", changed)
            with patch.object(
                deployed, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
            ):
                manifest = build_deployed_contra_source_manifest_v1(source)
                with self.assertRaisesRegex(
                    DeployedContraSourceManifestError,
                    "Contra_ALL.lua identity mismatch",
                ):
                    verify_deployed_contra_archive_equivalence_v1(
                        manifest, archive
                    )

    def test_toc_cannot_silently_switch_to_dormant_minified_source(self) -> None:
        payloads = _payloads(toc_load="Contra.lua")
        with tempfile.TemporaryDirectory() as directory:
            source = _write_tree(Path(directory), payloads)
            with patch.object(
                deployed, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
            ):
                with self.assertRaisesRegex(
                    DeployedContraSourceManifestError, "load order mismatch"
                ):
                    build_deployed_contra_source_manifest_v1(source)

    def test_manifest_content_tamper_is_rejected_before_archive_comparison(
        self,
    ) -> None:
        payloads = _payloads()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_tree(root, payloads)
            archive = _write_zip(root / "Contra_pirate.zip", payloads)
            with patch.object(
                deployed, "EXPECTED_FILE_IDENTITIES", _identities(payloads)
            ):
                manifest = build_deployed_contra_source_manifest_v1(source)
                changed = deepcopy(manifest)
                changed["authority_boundary"]["comparison_ready"] = True
                with self.assertRaisesRegex(
                    DeployedContraSourceManifestError, "manifest_sha256 mismatch"
                ):
                    verify_deployed_contra_archive_equivalence_v1(
                        changed, archive
                    )

    def test_pinned_loaded_policy_identity_is_the_audited_readable_artifact(
        self,
    ) -> None:
        self.assertEqual(
            deployed.EXPECTED_FILE_IDENTITIES["Contra_ALL.lua"],
            {
                "size_bytes": 3_958_359,
                "sha256": (
                    "3cd9ab254e521b7d4719f9648cae733ad54d5ed7421d1847716d54b6512cf2cd"
                ),
            },
        )
        self.assertEqual(deployed.EXPECTED_TOC_LOAD_ORDER, ("Contra_ALL.lua",))


if __name__ == "__main__":
    unittest.main()
