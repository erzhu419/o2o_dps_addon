"""Strict source-package verifier for the local Contra260817/Contra_new tree.

This is an identity and closure gate, not a policy adapter.  A verified result
means that the local tree is exactly the audited, known-incomplete source
package.  It does not mean that the addon loads, that the saved Fury profile is
known, or that the package is eligible for a DPS comparison.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get(
        "BOC_CONTRA260817_ROOT",
        str(PROJECT_ROOT.parent / "Contra_new"),
    )
)
DEFAULT_MANIFEST = Path(
    os.environ.get(
        "BOC_CONTRA260817_MANIFEST",
        str(
            PROJECT_ROOT
            / "configs/experts/contra260817_source_manifest_91baa120.json"
        ),
    )
)

SCHEMA = "contra260817_source_manifest/v1"
MANIFEST_ID = "contra260817.source.91baa120"
CODE_EXTENSIONS = (".lua", ".toc", ".xml")
CODE_MANIFEST_SHA256 = (
    "91baa120a0c895f3a9b3f26d701eb6c48eda78c6f036ad9a04065c6b8990db6d"
)
EXPECTED_MANIFEST_SHA256 = (
    "0ae04e9066ea9ae27dae6538e457acc052146c0a3993cf1e9cadb341903e5cd8"
)

EXPECTED_ALGORITHM: JSONMap = {
    "name": "sha256-canonical-code-manifest-v1",
    "included_extensions": list(CODE_EXTENSIONS),
    "entry_fields": ["relative_path", "size_bytes", "sha256"],
    "relative_path_format": "forward_slash",
    "ordering": "relative_path_unicode_codepoint",
    "serialization": (
        "json_utf8_sort_keys_compact_ensure_ascii_false_allow_nan_false"
    ),
}
EXPECTED_CODE_IDENTITY: JSONMap = {
    "file_count": 33,
    "byte_count": 4_285_993,
    "sha256": CODE_MANIFEST_SHA256,
}
EXPECTED_CRITICAL_FILES: JSONMap = {
    "Bindings.xml": (
        "3be47c832fad91352f154383bd1cff94cba6ab77f2b2eae694ea23f089701c7f"
    ),
    "Contra.toc": (
        "de16071d2e92ecac2c69104cc3f41839ef4928368e1f2d799c574850f5a8d1be"
    ),
    "Contra_Casting.lua": (
        "7e23204f971527559236a3b1bf16619254be5275c3720109a7fad54a50b0db7d"
    ),
    "Contra_DB.lua": (
        "597062fcdb8673153397e94e2f8564eceaf1131bde8755165bd8a2759dee617b"
    ),
    "Contra_Fliter.lua": (
        "f2bf3a505187999632356b28a00f60257a5243f636cee821b9a4b61aa3431260"
    ),
    "Contra_Lib.lua": (
        "091fc679d11be3a0e86e69095c3796f1eb41bff8c19123bf1673f4acd73f61e7"
    ),
    "Contra_Macro.lua": (
        "830109cc35c21b68fac4965875ac5cd370e6060749e5e3f4d7a795cd6f5a66a2"
    ),
    "Contra_Onlogin.lua": (
        "8ee56f9c584e4360242607bc08529b472653a515ca988e545a71e6e01d001430"
    ),
    "Contra_Scrip_Warrior.lua": (
        "a0f54d2a62e84084bb4d631f09e0173213190f4bea1eba670e6bfc1cd2b99713"
    ),
    "Contra_UI_Warrior.lua": (
        "be3afd6d14c2489ee5c430be213188e4fec679f333e8cef222485bdd3b39bf80"
    ),
}
EXPECTED_TOC_METADATA: JSONMap = {
    "interface": "11200",
    "title": "Contra魂斗罗",
    "author": "音十月-卡拉赞",
    "version": "4.04",
    "savedvariablespercharacter": "ContraDB",
}
EXPECTED_DECLARED_TOC: JSONMap = {
    "interface": "11200",
    "title": "Contra魂斗罗",
    "author": "音十月-卡拉赞",
    "version": "4.04",
    "saved_variables_per_character": "ContraDB",
}
EXPECTED_TOC_AUDIT: JSONMap = {
    "relative_path": "Contra.toc",
    "load_entry_count": 31,
    "present_entry_count": 29,
    "duplicate_entry_count": 0,
    "missing_members": ["Contra_Debuff.lua", "Contra_UI_DB.lua"],
    "empty_members": ["Contra_TrinketManager.lua"],
    "unlisted_lua_files": [
        "Contra_PaladinRetribution.lua",
        "Contra_log.lua",
    ],
}
EXPECTED_EMPTY_CODE_FILES = [
    "Contra_PaladinRetribution.lua",
    "Contra_TrinketManager.lua",
]
EXPECTED_PACKAGE_BLOCKERS = [
    "missing_toc_member:Contra_Debuff.lua",
    "missing_toc_member:Contra_UI_DB.lua",
    "empty_code_file:Contra_PaladinRetribution.lua",
    "empty_code_file:Contra_TrinketManager.lua",
]
EXPECTED_COMPARISON_BLOCKERS = [
    "package_incomplete",
    "per_character_ContraDB_profile_not_in_package",
    "runtime_load_not_verified",
    "full_ordered_Fury_adapter_not_implemented",
]


class Contra260817ManifestError(RuntimeError):
    """The source tree or manifest violates its pinned closure contract."""


@dataclass(frozen=True)
class CodeManifestIdentity:
    file_count: int
    byte_count: int
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Contra260817ManifestError(f"{path} must be an object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise Contra260817ManifestError(f"{path} must be an array")
    return value


def _read_manifest(path: Path) -> JSONMap:
    try:
        payload = path.read_bytes()
        observed = _sha256(payload)
        if observed != EXPECTED_MANIFEST_SHA256:
            raise Contra260817ManifestError(
                "manifest SHA-256 mismatch: "
                f"expected {EXPECTED_MANIFEST_SHA256}, got {observed}"
            )
        value = json.loads(payload.decode("utf-8"))
    except Contra260817ManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Contra260817ManifestError(
            f"could not read manifest {path.resolve()}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise Contra260817ManifestError("manifest root must be an object")
    return value


def build_code_manifest(source_root: Path) -> tuple[CodeManifestIdentity, list[JSONMap]]:
    """Hash all Lua/TOC/XML files using the manifest's canonical algorithm."""

    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise Contra260817ManifestError(f"source root is not a directory: {root}")
    paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in CODE_EXTENSIONS
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    entries: list[JSONMap] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise Contra260817ManifestError(
                f"could not read source file {path}: {exc}"
            ) from exc
        entries.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": len(data),
                "sha256": _sha256(data),
            }
        )
    identity = CodeManifestIdentity(
        file_count=len(entries),
        byte_count=sum(int(entry["size_bytes"]) for entry in entries),
        sha256=_sha256(_canonical_bytes(entries)),
    )
    return identity, entries


def _parse_toc(source_root: Path) -> tuple[JSONMap, list[str]]:
    toc = source_root / "Contra.toc"
    try:
        lines = toc.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise Contra260817ManifestError(f"could not read TOC {toc}: {exc}") from exc
    metadata: JSONMap = {}
    entries: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("##"):
            key, separator, value = line[2:].partition(":")
            if separator:
                metadata[key.strip().casefold()] = value.strip()
            continue
        if line.startswith("#"):
            continue
        entries.append(line.replace("\\", "/"))
    return metadata, entries


def audit_toc(source_root: Path) -> tuple[JSONMap, JSONMap]:
    """Return declared metadata and computed TOC closure facts."""

    metadata, load_entries = _parse_toc(source_root)
    present = [name for name in load_entries if (source_root / name).is_file()]
    missing = sorted(set(load_entries) - set(present))
    empty_members = sorted(
        name
        for name in present
        if (source_root / name).stat().st_size == 0
    )
    lua_files = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.lua")
        if path.is_file()
    )
    audit: JSONMap = {
        "relative_path": "Contra.toc",
        "load_entry_count": len(load_entries),
        "present_entry_count": len(present),
        "duplicate_entry_count": len(load_entries) - len(set(load_entries)),
        "missing_members": missing,
        "empty_members": empty_members,
        "unlisted_lua_files": sorted(set(lua_files) - set(load_entries)),
    }
    return metadata, audit


def _validate_static_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA:
        raise Contra260817ManifestError(f"manifest.schema must be {SCHEMA}")
    if manifest.get("manifest_id") != MANIFEST_ID:
        raise Contra260817ManifestError(
            f"manifest.manifest_id must be {MANIFEST_ID}"
        )

    package = _require_mapping(manifest.get("package"), "package")
    if package.get("user_label") != "Contra260817":
        raise Contra260817ManifestError("package.user_label drifted")
    if package.get("local_alias") != "Contra_new":
        raise Contra260817ManifestError("package.local_alias drifted")
    if package.get("expected_directory_name") != "Contra_new":
        raise Contra260817ManifestError("package.expected_directory_name drifted")
    if package.get("deployed") is not False:
        raise Contra260817ManifestError("Contra260817 must not be marked deployed")
    if package.get("declared_toc") != EXPECTED_DECLARED_TOC:
        raise Contra260817ManifestError("package.declared_toc drifted")
    for field in ("source_execution_verified", "exact_runtime_verified"):
        if package.get(field) is not False:
            raise Contra260817ManifestError(f"package.{field} must remain false")

    identity = _require_mapping(manifest.get("identity"), "identity")
    if identity.get("algorithm") != EXPECTED_ALGORITHM:
        raise Contra260817ManifestError("identity.algorithm drifted")
    if identity.get("code_manifest") != EXPECTED_CODE_IDENTITY:
        raise Contra260817ManifestError("identity.code_manifest drifted")
    if identity.get("critical_files") != EXPECTED_CRITICAL_FILES:
        raise Contra260817ManifestError("identity.critical_files drifted")
    if identity.get("toc") != EXPECTED_TOC_AUDIT:
        raise Contra260817ManifestError("identity.toc drifted")
    if identity.get("empty_code_files") != EXPECTED_EMPTY_CODE_FILES:
        raise Contra260817ManifestError("identity.empty_code_files drifted")

    fury = _require_mapping(manifest.get("fury"), "fury")
    entrypoints = _require_mapping(fury.get("entrypoints"), "fury.entrypoints")
    expected_symbols = {
        "single_target": ("Contra_SSKBZ_A", "Contra_SCKBZ_A"),
        "multi_target": ("Contra_SSKBZ_B", "Contra_SCKBZ_B"),
    }
    for lane, symbols in expected_symbols.items():
        entry = _require_mapping(entrypoints.get(lane), f"fury.entrypoints.{lane}")
        actual = tuple(
            entry.get(key)
            for key in (
                ("two_hand_raid", "dual_wield_raid")
                if lane == "single_target"
                else ("two_hand", "dual_wield")
            )
        )
        if actual != symbols:
            raise Contra260817ManifestError(
                f"fury.entrypoints.{lane} symbols drifted"
            )
    configuration = _require_mapping(
        fury.get("configuration_inputs"), "fury.configuration_inputs"
    )
    hazards = _require_list(
        configuration.get("source_configuration_hazards"),
        "fury.configuration_inputs.source_configuration_hazards",
    )
    if len(hazards) != 3 or not any(
        "baofa/shengcun" in str(value) and "Burst/Survive" in str(value)
        for value in hazards
    ):
        raise Contra260817ManifestError("source configuration hazards drifted")
    ordered = _require_mapping(
        fury.get("ordered_sink_semantics"), "fury.ordered_sink_semantics"
    )
    if ordered.get("normalization_rule") != (
        "preserve every raw sink in source order; any last-sink proposal is a "
        "separate normalized view and is not proof that earlier or later client "
        "calls executed"
    ):
        raise Contra260817ManifestError("ordered sink normalization rule drifted")
    if ordered.get("return_rule") != (
        "helper return values control Lua traversal; they are not general "
        "cast-success acknowledgements"
    ):
        raise Contra260817ManifestError("ordered sink return rule drifted")

    reuse = _require_mapping(
        manifest.get("deployed_v2_reuse_map"), "deployed_v2_reuse_map"
    )
    skeleton = _require_mapping(
        reuse.get("reusable_two_hand_raid_a_branch_skeleton"),
        "deployed_v2_reuse_map.reusable_two_hand_raid_a_branch_skeleton",
    )
    if skeleton.get("status") != "SOURCE_SIMILAR_NOT_RUNTIME_EQUIVALENT":
        raise Contra260817ManifestError("reuse status overclaims equivalence")
    if not str(reuse.get("reuse_prohibition") or "").strip():
        raise Contra260817ManifestError("reuse prohibition is missing")

    closure = _require_mapping(manifest.get("closure"), "closure")
    if closure.get("source_identity_verified_by_manifest") is not True:
        raise Contra260817ManifestError(
            "closure.source_identity_verified_by_manifest must remain true"
        )
    for field in (
        "package_complete",
        "comparison_eligible",
        "eligible_for_independent_vote",
        "baseline_sealed",
    ):
        if closure.get(field) is not False:
            raise Contra260817ManifestError(f"closure.{field} must remain false")
    if closure.get("package_blockers") != EXPECTED_PACKAGE_BLOCKERS:
        raise Contra260817ManifestError("closure.package_blockers drifted")
    if closure.get("comparison_blockers") != EXPECTED_COMPARISON_BLOCKERS:
        raise Contra260817ManifestError("closure.comparison_blockers drifted")
    promotion = _require_list(
        closure.get("promotion_requirements"), "closure.promotion_requirements"
    )
    if len(promotion) < 6 or any(not str(value).strip() for value in promotion):
        raise Contra260817ManifestError("promotion requirements are incomplete")


def _source_anchor_checks(source_root: Path) -> JSONMap:
    files = {
        name: (source_root / name).read_text(encoding="utf-8-sig")
        for name in (
            "Contra_Macro.lua",
            "Contra_Scrip_Warrior.lua",
            "Contra_Onlogin.lua",
            "Contra_Lib.lua",
        )
    }
    checks = {
        "macro_handler": "function Contra_MacroHandler(cmd)"
        in files["Contra_Macro.lua"],
        "auto_single_multi_route": (
            'if param == "c" then' in files["Contra_Macro.lua"]
            and "Contra.Filter.CountAttackableUnitsInRangeFivema()"
            in files["Contra_Macro.lua"]
        ),
        "two_hand_single_and_multi": (
            "function Contra_SSKBZ_A()" in files["Contra_Scrip_Warrior.lua"]
            and "function Contra_SSKBZ_B()" in files["Contra_Scrip_Warrior.lua"]
        ),
        "dual_wield_single_and_multi": (
            "function Contra_SCKBZ_A()" in files["Contra_Scrip_Warrior.lua"]
            and "function Contra_SCKBZ_B()" in files["Contra_Scrip_Warrior.lua"]
        ),
        "cast_helper": "function ContraZSCast(spellName)"
        in files["Contra_Scrip_Warrior.lua"],
        "talent_sync": "function Contra.SyncCheckTalent()"
        in files["Contra_Onlogin.lua"],
        "zssdw_loadout_derivation": "Contra.ZSSDW = 0"
        in files["Contra_Onlogin.lua"],
        "worldboss_classification": (
            'classification == "worldboss"' in files["Contra_Lib.lua"]
        ),
    }
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise Contra260817ManifestError(
            f"critical Fury source anchors missing: {failed}"
        )
    return checks


def verify_contra260817_source_package(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    source_root: Path = DEFAULT_SOURCE_ROOT,
) -> JSONMap:
    """Verify the pinned known-incomplete package without promoting it."""

    manifest = _read_manifest(manifest_path.expanduser().resolve())
    _validate_static_manifest(manifest)
    root = source_root.expanduser().resolve()
    if root.name != "Contra_new":
        raise Contra260817ManifestError(
            f"source root directory must be named Contra_new, got {root.name}"
        )

    code_identity, entries = build_code_manifest(root)
    if asdict(code_identity) != EXPECTED_CODE_IDENTITY:
        raise Contra260817ManifestError(
            "33-file code manifest mismatch: "
            f"expected {EXPECTED_CODE_IDENTITY}, got {asdict(code_identity)}"
        )
    entry_by_name = {entry["relative_path"]: entry for entry in entries}
    actual_critical = {
        name: entry_by_name.get(name, {}).get("sha256")
        for name in EXPECTED_CRITICAL_FILES
    }
    if actual_critical != EXPECTED_CRITICAL_FILES:
        raise Contra260817ManifestError("critical source-file identity mismatch")

    toc_metadata, toc_audit = audit_toc(root)
    actual_metadata = {
        key: toc_metadata.get(key) for key in EXPECTED_TOC_METADATA
    }
    if actual_metadata != EXPECTED_TOC_METADATA:
        raise Contra260817ManifestError(
            f"TOC metadata mismatch: {actual_metadata}"
        )
    if toc_audit != EXPECTED_TOC_AUDIT:
        raise Contra260817ManifestError(
            f"TOC closure mismatch: expected {EXPECTED_TOC_AUDIT}, got {toc_audit}"
        )

    empty_code_files = sorted(
        str(entry["relative_path"])
        for entry in entries
        if entry["size_bytes"] == 0
    )
    if empty_code_files != EXPECTED_EMPTY_CODE_FILES:
        raise Contra260817ManifestError(
            f"zero-byte code file set changed: {empty_code_files}"
        )
    package_blockers = [
        *(f"missing_toc_member:{name}" for name in toc_audit["missing_members"]),
        *(f"empty_code_file:{name}" for name in empty_code_files),
    ]
    if package_blockers != EXPECTED_PACKAGE_BLOCKERS:
        raise Contra260817ManifestError(
            f"computed package blockers drifted: {package_blockers}"
        )

    anchors = _source_anchor_checks(root)
    closure = _require_mapping(manifest["closure"], "closure")
    return {
        "schema": "contra260817_source_verification/v1",
        "manifest_id": manifest["manifest_id"],
        "manifest_path": str(manifest_path.expanduser().resolve()),
        "source_root": str(root),
        "source_identity_verified": True,
        "code_manifest": asdict(code_identity),
        "toc_metadata": actual_metadata,
        "toc": toc_audit,
        "empty_code_files": empty_code_files,
        "source_anchor_checks": anchors,
        "package_blockers": package_blockers,
        "comparison_blockers": list(closure["comparison_blockers"]),
        "package_complete": False,
        "comparison_eligible": False,
        "eligible_for_independent_vote": False,
        "baseline_sealed": False,
        "result": "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = verify_contra260817_source_package(
        manifest_path=args.manifest,
        source_root=args.source_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CODE_MANIFEST_SHA256",
    "DEFAULT_MANIFEST",
    "DEFAULT_SOURCE_ROOT",
    "Contra260817ManifestError",
    "audit_toc",
    "build_code_manifest",
    "verify_contra260817_source_package",
]
