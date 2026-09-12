"""Pinned source identity for the TOC-loaded deployed Contra policy.

This module answers one deliberately narrow question: does a local ``Contra``
directory, and optionally a zip package, contain the exact audited
``Contra.toc`` -> ``Contra_ALL.lua`` source closure?  It never executes Lua and
therefore cannot prove that the public macro entry accepted the current
character, that WoW loaded the addon, or that a simulator lane is comparison
ready.

The dormant same-folder ``Contra.lua`` is recorded only to prevent it from
being mistaken for the loaded policy.  It is not part of the loaded closure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import zipfile


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get(
        "BOC_DEPLOYED_CONTRA_ROOT",
        str(PROJECT_ROOT.parent.parent / "Contra"),
    )
)
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "BOC_DEPLOYED_CONTRA_ARCHIVE",
        str(PROJECT_ROOT.parent.parent / "Contra_pirate.zip"),
    )
)

SCHEMA = "deployed_contra_source_manifest/v1"
VERIFICATION_SCHEMA = "deployed_contra_source_verification/v1"
SOURCE_POLICY_ID = "contra.deployed.fury"
EXPECTED_DIRECTORY_NAME = "Contra"
EXPECTED_TOC_LOAD_ORDER = ("Contra_ALL.lua",)
EXPECTED_TOC_METADATA: JSONMap = {
    "interface": "11200",
    "title": "Contra魂斗罗",
    "author": "音十月-卡拉赞",
    "version": "0.0.5",
    "notes": "Contra魂斗罗4.0.1",
    "defaultstate": "Enabled",
    "loadondemand": "0",
    "savedvariablespercharacter": "ContraDB",
}
EXPECTED_FILE_IDENTITIES: JSONMap = {
    "Bindings.xml": {
        "size_bytes": 216,
        "sha256": "3be47c832fad91352f154383bd1cff94cba6ab77f2b2eae694ea23f089701c7f",
    },
    "Contra.toc": {
        "size_bytes": 234,
        "sha256": "7c3adbc5b75193f36da1567d6af355a7fdb6fa48a588de7979a2b3bd67e9673d",
    },
    "Contra.lua": {
        "size_bytes": 3_567_196,
        "sha256": "b9468611ccd9ee622aa32c2d0c63415026e712fca76c9a9e8dcc4a9d751d4610",
    },
    "Contra_ALL.lua": {
        "size_bytes": 3_958_359,
        "sha256": "3cd9ab254e521b7d4719f9648cae733ad54d5ed7421d1847716d54b6512cf2cd",
    },
}

_SOURCE_ANCHORS = {
    "macro_handler": "function Contra_MacroHandler(cmd)",
    "two_hand_single": "function Contra_SSKBZ_A()",
    "two_hand_multi": "function Contra_SSKBZ_B()",
    "dual_wield_single": "function Contra_SCKBZ_A()",
    "dual_wield_multi": "function Contra_SCKBZ_B()",
    "nampower_settings": "function Contra.OnLogin.namepowerSettings()",
    "next_swing_queue": 'QueueSpellByName("英勇打击")',
    "public_entry_access_gate": (
        'playername ~= "畏了部落" and playerguild ~= "下周见"'
    ),
}


class DeployedContraSourceManifestError(RuntimeError):
    """The installed or archived source differs from the pinned closure."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_identity(payload: bytes) -> JSONMap:
    return {"size_bytes": len(payload), "sha256": _sha256(payload)}


def _read_file(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise DeployedContraSourceManifestError(
            f"could not read {label} {path}: {error}"
        ) from error


def _parse_toc(payload: bytes, label: str) -> tuple[JSONMap, tuple[str, ...]]:
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise DeployedContraSourceManifestError(
            f"{label} is not UTF-8 text: {error}"
        ) from error
    metadata: JSONMap = {}
    entries: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("##"):
            key, separator, value = line[2:].partition(":")
            if separator:
                normalized = key.strip().casefold()
                if normalized in metadata:
                    raise DeployedContraSourceManifestError(
                        f"{label} repeats TOC metadata {normalized}"
                    )
                metadata[normalized] = value.strip()
            continue
        if not line.startswith("#"):
            entries.append(line.replace("\\", "/"))
    return metadata, tuple(entries)


def _validate_pinned_identity(name: str, payload: bytes) -> JSONMap:
    observed = _file_identity(payload)
    expected = EXPECTED_FILE_IDENTITIES[name]
    if observed != expected:
        raise DeployedContraSourceManifestError(
            f"{name} identity mismatch: expected {expected}, got {observed}"
        )
    return observed


def _source_anchor_checks(payload: bytes) -> JSONMap:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DeployedContraSourceManifestError(
            f"Contra_ALL.lua is not readable UTF-8 Lua: {error}"
        ) from error
    checks = {name: anchor in text for name, anchor in _SOURCE_ANCHORS.items()}
    if not all(checks.values()):
        missing = sorted(name for name, present in checks.items() if not present)
        raise DeployedContraSourceManifestError(
            f"Contra_ALL.lua critical anchors are missing: {missing}"
        )
    if b"\x00" in payload or payload.startswith(b"\x1bLua"):
        raise DeployedContraSourceManifestError(
            "Contra_ALL.lua is not the audited readable text source"
        )
    checks["readable_text_source"] = True
    return checks


def _loaded_closure_identity(toc: bytes, loaded: bytes) -> JSONMap:
    entries = [
        {"relative_path": "Contra.toc", **_file_identity(toc)},
        {"relative_path": "Contra_ALL.lua", **_file_identity(loaded)},
    ]
    return {
        "algorithm": "sha256-canonical-loaded-closure-v1",
        "entries": entries,
        "sha256": _sha256(_canonical_bytes(entries)),
    }


def build_deployed_contra_source_manifest_v1(
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
) -> JSONMap:
    """Build and pin the installed directory's loaded-source manifest."""

    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise DeployedContraSourceManifestError(
            f"deployed Contra source root is not a directory: {root}"
        )
    if root.name != EXPECTED_DIRECTORY_NAME:
        raise DeployedContraSourceManifestError(
            f"deployed source directory must be named Contra, got {root.name}"
        )

    payloads = {
        name: _read_file(root / name, name) for name in EXPECTED_FILE_IDENTITIES
    }
    identities = {
        name: _validate_pinned_identity(name, payload)
        for name, payload in payloads.items()
    }
    toc_metadata, load_order = _parse_toc(payloads["Contra.toc"], "Contra.toc")
    if toc_metadata != EXPECTED_TOC_METADATA:
        raise DeployedContraSourceManifestError(
            f"Contra.toc metadata mismatch: {toc_metadata}"
        )
    if load_order != EXPECTED_TOC_LOAD_ORDER:
        raise DeployedContraSourceManifestError(
            "Contra.toc load order mismatch: "
            f"expected {EXPECTED_TOC_LOAD_ORDER}, got {load_order}"
        )
    anchors = _source_anchor_checks(payloads["Contra_ALL.lua"])
    closure = _loaded_closure_identity(
        payloads["Contra.toc"], payloads["Contra_ALL.lua"]
    )
    document: JSONMap = {
        "schema": SCHEMA,
        "source_policy_id": SOURCE_POLICY_ID,
        "package": {
            "expected_directory_name": EXPECTED_DIRECTORY_NAME,
            "declared_version": EXPECTED_TOC_METADATA["version"],
            "saved_variables_per_character": "ContraDB",
        },
        "toc": {
            "metadata": toc_metadata,
            "load_order": list(load_order),
            "loaded_policy_file": "Contra_ALL.lua",
        },
        "identity": {
            "loaded_closure": closure,
            "critical_files": identities,
            "dormant_same_folder_lua": {
                "relative_path": "Contra.lua",
                "loaded_by_toc": False,
                **identities["Contra.lua"],
            },
        },
        "source_anchor_checks": anchors,
        "authority_boundary": {
            "source_identity_verified": True,
            "readable_policy_body_available": True,
            "public_macro_entry_verified": False,
            "client_load_observed": False,
            "client_execution_observed": False,
            "client_acceptance_observed": False,
            "server_outcome_observed": False,
            "comparison_ready": False,
        },
        "remaining_requirements": [
            "BIND_CURRENT_CHARACTER_BUTTONS_AND_NAMPOWER_CVARS",
            "PROVE_OR_EXPLICITLY_BYPASS_PUBLIC_ENTRY_ACCESS_GATE",
            "VALIDATE_ORDERED_SINKS_AGAINST_CLIENT_OR_LUA_ORACLE",
            "RUN_MATCHED_FULL_CONTROLLER_ROLLOUT",
        ],
    }
    document["manifest_sha256"] = _sha256(_canonical_bytes(document))
    return document


def _archive_members(archive: Path) -> tuple[str, dict[str, bytes]]:
    try:
        with zipfile.ZipFile(archive) as handle:
            names = [
                info.filename.replace("\\", "/")
                for info in handle.infolist()
                if not info.is_dir()
            ]
            toc_members = [name for name in names if name.endswith("/Contra.toc")]
            if len(toc_members) != 1:
                raise DeployedContraSourceManifestError(
                    "archive must contain exactly one */Contra.toc"
                )
            prefix = toc_members[0][: -len("Contra.toc")]
            members: dict[str, bytes] = {}
            for relative in EXPECTED_FILE_IDENTITIES:
                member = prefix + relative
                if member not in names:
                    raise DeployedContraSourceManifestError(
                        f"archive lacks {member}"
                    )
                members[relative] = handle.read(member)
    except DeployedContraSourceManifestError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise DeployedContraSourceManifestError(
            f"could not read Contra archive {archive}: {error}"
        ) from error
    return prefix.rstrip("/"), members


def verify_deployed_contra_archive_equivalence_v1(
    manifest: Mapping[str, Any],
    archive_path: str | Path = DEFAULT_ARCHIVE,
) -> JSONMap:
    """Verify byte equality of the installed and archived loaded closure."""

    if manifest.get("schema") != SCHEMA:
        raise DeployedContraSourceManifestError(
            f"manifest.schema must equal {SCHEMA}"
        )
    expected_manifest_sha = manifest.get("manifest_sha256")
    unhashed = dict(manifest)
    unhashed.pop("manifest_sha256", None)
    if expected_manifest_sha != _sha256(_canonical_bytes(unhashed)):
        raise DeployedContraSourceManifestError("manifest_sha256 mismatch")

    archive = Path(archive_path).expanduser().resolve()
    prefix, members = _archive_members(archive)
    archive_identities = {
        name: _validate_pinned_identity(name, payload)
        for name, payload in members.items()
    }
    archive_metadata, archive_load_order = _parse_toc(
        members["Contra.toc"], "archive Contra.toc"
    )
    if archive_metadata != EXPECTED_TOC_METADATA:
        raise DeployedContraSourceManifestError(
            f"archive TOC metadata mismatch: {archive_metadata}"
        )
    if archive_load_order != EXPECTED_TOC_LOAD_ORDER:
        raise DeployedContraSourceManifestError(
            f"archive TOC load order mismatch: {archive_load_order}"
        )
    closure = _loaded_closure_identity(
        members["Contra.toc"], members["Contra_ALL.lua"]
    )
    installed_closure = manifest.get("identity", {}).get("loaded_closure")
    if closure != installed_closure:
        raise DeployedContraSourceManifestError(
            "archive loaded closure differs from installed manifest"
        )
    installed_critical = manifest.get("identity", {}).get("critical_files")
    if archive_identities != installed_critical:
        raise DeployedContraSourceManifestError(
            "archive critical files differ from installed manifest"
        )
    return {
        "schema": VERIFICATION_SCHEMA,
        "source_policy_id": SOURCE_POLICY_ID,
        "manifest_sha256": expected_manifest_sha,
        "archive_root": prefix,
        "toc_loaded_policy_file": "Contra_ALL.lua",
        "loaded_closure_sha256": closure["sha256"],
        "toc_loaded_source_byte_equivalent": True,
        "dormant_same_folder_source_byte_equivalent": True,
        "source_identity_verified": True,
        "public_macro_entry_verified": False,
        "client_execution_observed": False,
        "comparison_ready": False,
        "result": "INSTALLED_AND_ARCHIVE_LOADED_SOURCE_EQUIVALENT",
    }


def verify_deployed_contra_source_v1(
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    archive_path: str | Path | None = DEFAULT_ARCHIVE,
) -> JSONMap:
    manifest = build_deployed_contra_source_manifest_v1(source_root)
    if archive_path is None:
        return {
            "schema": VERIFICATION_SCHEMA,
            "source_policy_id": SOURCE_POLICY_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "loaded_closure_sha256": manifest["identity"]["loaded_closure"][
                "sha256"
            ],
            "source_identity_verified": True,
            "archive_checked": False,
            "public_macro_entry_verified": False,
            "client_execution_observed": False,
            "comparison_ready": False,
            "result": "INSTALLED_LOADED_SOURCE_IDENTITY_VERIFIED",
        }
    result = verify_deployed_contra_archive_equivalence_v1(manifest, archive_path)
    result["archive_checked"] = True
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--without-archive", action="store_true")
    parser.add_argument("--print-manifest", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_deployed_contra_source_manifest_v1(args.source_root)
        result = verify_deployed_contra_source_v1(
            source_root=args.source_root,
            archive_path=None if args.without_archive else args.archive,
        )
    except DeployedContraSourceManifestError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            manifest if args.print_manifest else result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_ARCHIVE",
    "DEFAULT_SOURCE_ROOT",
    "DeployedContraSourceManifestError",
    "EXPECTED_FILE_IDENTITIES",
    "SCHEMA",
    "SOURCE_POLICY_ID",
    "VERIFICATION_SCHEMA",
    "build_deployed_contra_source_manifest_v1",
    "verify_deployed_contra_archive_equivalence_v1",
    "verify_deployed_contra_source_v1",
]
