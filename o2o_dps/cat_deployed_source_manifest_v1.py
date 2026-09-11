"""Fail-closed verifier for the installed Cat source tree.

The verifier content-addresses both the whole directory and the exact Cat.toc
load closure.  It does not execute Lua, read private SavedVariables, or turn a
source audit into runtime/comparison evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Mapping, Sequence


SCHEMA = "cat_deployed_source_manifest/v1"
VERIFICATION_SCHEMA = "cat_deployed_source_verification/v1"
MANIFEST_ID = "cat.deployed_source.1_18_1.c31be2f9"
SOURCE_ID = "cat.addon_directory.1_18_1.c31be2f9"
EXPECTED_MANIFEST_SHA256 = (
    "61951c4843a1dd7310cbee9ef671212535ce4fdae87e547af673c5d168abe2b2"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "configs"
    / "experts"
    / "cat_deployed_source_manifest_c31be2f9.json"
)

EXPECTED_TREE = {
    "root_kind": "cat_deployed_tree",
    "file_count": 102,
    "byte_count": 2_528_102,
    "sha256": "3444597ef6b162b6d65c11f28b49301226b55e9b9a94d74d4c62ad56350e2c75",
}
EXPECTED_TOC_CLOSURE = {
    "root_kind": "cat_deployed_toc_closure",
    "file_count": 100,
    "byte_count": 2_519_838,
    "sha256": "c31be2f97a0af6c9f996e642585cd6a5c3acbedfd2f45b23b4e18813bdc2273e",
    "lua_file_count": 99,
    "xml_file_count": 1,
    "duplicate_entry_count": 0,
    "missing_entry_count": 0,
    "unlisted_lua_or_xml_count": 0,
}
EXPECTED_CRITICAL_FILES = {
    "Cat.toc": "aaf871e3d9ea6bc41af60da775b3388d443c7b61a751b6961a1edafbf0a3c3e5",
    "CatLib.lua": "44d1c9d02653a7966c6bf2aef9f35652b9ce261d2d01ea857449cdf17f0fd211",
    "CatEvent.lua": "de4039dd5ca529e9f1893b002d36a009346f1aeed760c18a03199fc59a2fbe67",
    "CatEvent-Warrior.lua": "d4b72ffc68aa84d65ad8723c87b02cab4b1bbd8635fa47bb81a76e3d800c3f09",
    "WarriorFury.lua": "1dc652bc34117d11703e2b9481b4da7386c3e3865b85413af5b2a4031bbab6fe",
    "UI/Settings-WarriorFury.lua": "f0d8ffe0ebed334cbcc854ea2cdd0d4f76c1a206d1f21c48cc5ffed1613ddba9",
    "UI/Command.lua": "1e8014088496c0a17e8111bda1ba38d7db1fbe3a297416067c4de32a30460b92",
    "Bindings.xml": "81324380180cb3fad0bb7ee48df9166b5fe216e321ba13e534b09c935147194e",
    "Expand/TWT.lua": "094014031f7c77dab03ca0477007473a706ae02e79d477668f49a647c185faf4",
    "CatImmuneLib.lua": "e4dc6cb99f54cd3f6761c1faff2f17592208c3f40a4f3e451e64ab26c8652985",
}
EXPECTED_RUNTIME_KEYS = [
    "BattleShout",
    "BerserkerRage",
    "BerserkerStance",
    "Carrot",
    "Carrot_Value",
    "Charge",
    "DeathWish",
    "DeathWishBoss",
    "ExecuteWithoutMonster",
    "Hamstring",
    "HealthStone",
    "HealthStone_Value",
    "HerbalTea",
    "HerbalTea_Value",
    "HeroicStrike",
    "HeroicStrike_Value",
    "Interrupt",
    "NearbyEnemies",
    "NearbyEnemies_Value",
    "Overpower",
    "Overpower_Value",
    "Pick",
    "Power",
    "RacialTraits",
    "RacialTraitsBoss",
    "Recklessness",
    "Slam_Value",
    "Soulspeed",
    "SoulspeedBoss",
    "SunderArmor",
    "SunderArmorBOSS",
    "SunderArmorOnce",
    "Sweeping",
    "Sweeping_Value",
    "Target",
    "TBBoss",
    "Trinket_Below",
    "Trinket_Upper",
    "TUBoss",
    "UseExecute",
    "Whirlwind",
]
EXPECTED_INACTIVE_KEYS = [
    "Execute",
    "ExecuteOtherTarget",
    "Rend",
    "Rend_Value",
    "SuperWoW",
    "UnitXP",
]
EXPECTED_CAPABILITY_IDS = {
    "DUAL_WIELD_AND_TWO_HAND_BRANCHES",
    "TURTLE_SLAM_TIMING",
    "CORE_FURY_PRIORITY",
    "AOE_NEXT_SWING_SELECTION",
    "SUNDER_AND_STANCE_TRANSITIONS",
    "LOADOUT_AND_TALENT_COST_ADJUSTMENTS",
    "UTILITY_ITEMS_AND_COOLDOWNS",
}
EXPECTED_PATH_IDS = {
    "target_and_attack_prelude",
    "sunder_then_next_swing",
    "next_swing_then_primary_skill",
    "slam_interrupt_then_skill",
    "stance_then_charge",
    "independent_off_gcd_and_item_chain",
}
EXPECTED_BLOCKERS = [
    "EXACT_SAVED_PROFILE_MISSING",
    "EXACT_CHARACTER_BUILD_MISSING",
    "OPTIONAL_EXTENSION_IDENTITY_MISSING",
    "ORDERED_SINK_TRACE_MISSING",
    "CLIENT_ACCEPTANCE_TRACE_MISSING",
    "SERVER_OUTCOME_TRACE_MISSING",
    "FULL_POLICY_SIMULATOR_ADAPTER_MISSING",
]
EXPECTED_BOUNDARY_IDS = {
    "ADDON_DIRECTORY_IS_NOT_LOAD_EVIDENCE",
    "PROFILE_SNAPSHOT_ABSENT",
    "SOURCE_DEFAULT_IS_NOT_EXPERT_IDENTITY",
    "FUNCTION_RETURN_IS_NOT_ACTION",
    "ONE_PRESS_CAN_HAVE_MULTIPLE_SINKS",
    "NAMPOWER_QUEUE_IS_NOT_ACCEPTANCE",
    "OPTIONAL_EXTENSION_STATE_UNFROZEN",
    "TOC_CLOSURE_EXCLUDES_RUNTIME_ECOSYSTEM",
    "NEARBY_SCAN_IS_NOT_GROUND_TRUTH",
    "NO_ARCHIVE_OR_RELEASE_SIGNATURE",
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class CatDeployedSourceManifestError(ValueError):
    """The manifest or external Cat source tree failed a strict check."""


def _fail(path: str, message: str) -> CatDeployedSourceManifestError:
    return CatDeployedSourceManifestError(f"{path}: {message}")


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(path, f"expected object, got {type(value).__name__}")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(path, f"expected array, got {type(value).__name__}")
    return value


def _same(value: Any, expected: Any, path: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise _fail(path, f"expected {expected!r}, got {value!r}")


def _keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise _fail(
            path,
            f"key mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}",
        )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("manifest", f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_relative(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise _fail(path, "expected normalized POSIX relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail(path, "path traversal is not permitted")
    return value


def _stable_read(path: Path, label: str) -> bytes:
    if path.is_symlink():
        raise _fail(label, "symbolic links are not permitted")
    try:
        before = path.stat()
        if not path.is_file():
            raise _fail(label, "not a regular file")
        payload = path.read_bytes()
        after = path.stat()
    except CatDeployedSourceManifestError:
        raise
    except OSError as error:
        raise _fail(label, f"cannot read: {error}") from error
    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key or len(payload) != after.st_size:
        raise _fail(label, "file changed while it was being read")
    return payload


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Load the exact versioned manifest, rejecting byte or JSON drift."""

    manifest_path = Path(path).expanduser().resolve(strict=True)
    payload = _stable_read(manifest_path, "manifest")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise _fail(
            "manifest",
            f"content hash mismatch: expected {EXPECTED_MANIFEST_SHA256}, got {digest}",
        )
    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail("manifest", f"invalid UTF-8 JSON: {error}") from error
    validate_manifest(document)
    return document


def validate_manifest(document: Any) -> None:
    """Validate the source-only authority boundary and reviewed contract."""

    root = _object(document, "manifest")
    _keys(
        root,
        {
            "schema",
            "manifest_id",
            "audit_date",
            "source",
            "identity",
            "fury_policy_surface",
            "reviewed_capabilities",
            "execution_contract",
            "comparison_readiness",
            "known_boundaries",
            "lineage",
        },
        "manifest",
    )
    _same(root["schema"], SCHEMA, "manifest.schema")
    _same(root["manifest_id"], MANIFEST_ID, "manifest.manifest_id")

    source = _object(root["source"], "manifest.source")
    _same(source["source_id"], SOURCE_ID, "manifest.source.source_id")
    _same(source["declared_interface"], "11200", "manifest.source.declared_interface")
    _same(source["declared_version"], "1.18.1", "manifest.source.declared_version")
    _same(source["fury_source_date"], "2026-07-22", "manifest.source.fury_source_date")
    _same(
        dict(_object(source["location"], "manifest.source.location")),
        {
            "kind": "EXTERNAL_ADDON_DIRECTORY",
            "expected_directory_name": "Cat",
            "installed_under_interface_addons": True,
            "client_enabled_observed": False,
            "client_load_observed": False,
        },
        "manifest.source.location",
    )
    _same(
        dict(_object(source["authority"], "manifest.source.authority")),
        {
            "role": "SOURCE_IDENTITY_AND_CAPABILITY",
            "provenance_kind": "SOURCE_DERIVED",
            "source_execution": False,
            "exact_runtime": False,
            "policy_profile_included": False,
            "eligible_for_comparison": False,
            "eligible_for_independent_vote": False,
        },
        "manifest.source.authority",
    )

    identity = _object(root["identity"], "manifest.identity")
    _same(dict(_object(identity["tree"], "manifest.identity.tree")), EXPECTED_TREE, "manifest.identity.tree")
    _same(
        dict(_object(identity["toc_closure"], "manifest.identity.toc_closure")),
        EXPECTED_TOC_CLOSURE,
        "manifest.identity.toc_closure",
    )
    _same(identity["unloaded_files"], ["Cat.toc", "\u4f7f\u7528\u6307\u5357.txt"], "manifest.identity.unloaded_files")
    critical = dict(_object(identity["critical_files"], "manifest.identity.critical_files"))
    _same(critical, EXPECTED_CRITICAL_FILES, "manifest.identity.critical_files")
    for relative, digest in critical.items():
        _safe_relative(relative, f"manifest.identity.critical_files.{relative}")
        if not _SHA256_RE.fullmatch(digest):
            raise _fail(f"manifest.identity.critical_files.{relative}", "invalid SHA-256")

    surface = _object(root["fury_policy_surface"], "manifest.fury_policy_surface")
    _same(
        surface["entry_chain"],
        ["Bindings.xml:WARRIOR_FURY_BINDING", "MPWarriorFuryCommand", "MPFuryDPS"],
        "manifest.fury_policy_surface.entry_chain",
    )
    _same(surface["slash_command"], "/fury", "manifest.fury_policy_surface.slash_command")
    _same(surface["saved_variables_scope"], "SavedVariablesPerCharacter", "manifest.fury_policy_surface.saved_variables_scope")
    _same(surface["saved_variables_name"], "MPWarriorFurySaved", "manifest.fury_policy_surface.saved_variables_name")
    _same(surface["settings_schema_version"], 32, "manifest.fury_policy_surface.settings_schema_version")
    _same(surface["profile_slot_count"], 3, "manifest.fury_policy_surface.profile_slot_count")
    _same(surface["binding_selected_profile_slot"], 1, "manifest.fury_policy_surface.binding_selected_profile_slot")
    _same(surface["source_default_is_runtime_profile"], False, "manifest.fury_policy_surface.source_default_is_runtime_profile")
    _same(surface["active_runtime_config_keys"], EXPECTED_RUNTIME_KEYS, "manifest.fury_policy_surface.active_runtime_config_keys")
    _same(surface["initialized_but_not_active_policy_keys"], EXPECTED_INACTIVE_KEYS, "manifest.fury_policy_surface.initialized_but_not_active_policy_keys")

    capabilities = _array(root["reviewed_capabilities"], "manifest.reviewed_capabilities")
    capability_ids = [item["id"] for item in capabilities]
    if len(capability_ids) != len(set(capability_ids)):
        raise _fail("manifest.reviewed_capabilities", "duplicate capability ID")
    _same(set(capability_ids), EXPECTED_CAPABILITY_IDS, "manifest.reviewed_capabilities")

    contract = _object(root["execution_contract"], "manifest.execution_contract")
    returns = _object(contract["entry_return_semantics"], "manifest.execution_contract.entry_return_semantics")
    _same(returns["MPWarriorFuryCommand"], "NO_ACTION_RESULT", "manifest.execution_contract.entry_return_semantics.MPWarriorFuryCommand")
    _same(returns["MPFuryDPS"], "NO_ACTION_RESULT", "manifest.execution_contract.entry_return_semantics.MPFuryDPS")
    _same(returns["client_acceptance_inferred"], False, "manifest.execution_contract.entry_return_semantics.client_acceptance_inferred")
    _same(returns["server_outcome_inferred"], False, "manifest.execution_contract.entry_return_semantics.server_outcome_inferred")
    sinks = _object(contract["sink_semantics"], "manifest.execution_contract.sink_semantics")
    _same(sinks["one_press_one_sink"], False, "manifest.execution_contract.sink_semantics.one_press_one_sink")
    _same(sinks["ordered_sink_trace_required"], True, "manifest.execution_contract.sink_semantics.ordered_sink_trace_required")
    _same(sinks["press_count_is_action_count"], False, "manifest.execution_contract.sink_semantics.press_count_is_action_count")
    paths = _array(contract["reviewed_multi_sink_paths"], "manifest.execution_contract.reviewed_multi_sink_paths")
    path_ids = [item["path_id"] for item in paths]
    if len(path_ids) != len(set(path_ids)):
        raise _fail("manifest.execution_contract.reviewed_multi_sink_paths", "duplicate path ID")
    _same(set(path_ids), EXPECTED_PATH_IDS, "manifest.execution_contract.reviewed_multi_sink_paths")

    readiness = _object(root["comparison_readiness"], "manifest.comparison_readiness")
    _same(readiness["status"], "BLOCKED", "manifest.comparison_readiness.status")
    _same(readiness["source_identity_complete"], True, "manifest.comparison_readiness.source_identity_complete")
    _same(readiness["toc_closure_complete"], True, "manifest.comparison_readiness.toc_closure_complete")
    for field in (
        "exact_saved_profile_captured",
        "exact_talents_and_equipment_captured",
        "optional_extension_versions_captured",
        "ordered_client_sink_trace_captured",
        "client_acceptance_trace_captured",
        "server_outcome_trace_captured",
        "simulator_adapter_complete",
        "eligible_for_comparison",
    ):
        _same(readiness[field], False, f"manifest.comparison_readiness.{field}")
    _same(readiness["blockers"], EXPECTED_BLOCKERS, "manifest.comparison_readiness.blockers")

    boundaries = _array(root["known_boundaries"], "manifest.known_boundaries")
    boundary_ids = [item["id"] for item in boundaries]
    if len(boundary_ids) != len(set(boundary_ids)):
        raise _fail("manifest.known_boundaries", "duplicate boundary ID")
    _same(set(boundary_ids), EXPECTED_BOUNDARY_IDS, "manifest.known_boundaries")
    lineage = _object(root["lineage"], "manifest.lineage")
    _same(lineage["previous_warrior_fury_lua_sha256"], EXPECTED_CRITICAL_FILES["WarriorFury.lua"], "manifest.lineage.previous_warrior_fury_lua_sha256")
    _same(lineage["replaces_existing_baseline_result"], False, "manifest.lineage.replaces_existing_baseline_result")
    _same(lineage["reuse_existing_baseline_result"], False, "manifest.lineage.reuse_existing_baseline_result")


def _partition(rows: Sequence[Mapping[str, Any]], root_kind: str) -> dict[str, Any]:
    identity = [
        {
            "root_kind": root_kind,
            "relative_path": row["relative_path"],
            "sha256": row["sha256"],
        }
        for row in sorted(rows, key=lambda item: item["relative_path"])
    ]
    return {
        "root_kind": root_kind,
        "file_count": len(identity),
        "byte_count": sum(int(row["byte_count"]) for row in rows),
        "sha256": hashlib.sha256(_canonical_bytes(identity)).hexdigest(),
    }


def compute_source_tree_facts(cat_root: str | Path) -> dict[str, Any]:
    """Inspect an external Cat tree without executing Lua or reading profiles."""

    supplied = Path(cat_root).expanduser()
    if supplied.is_symlink():
        raise _fail("cat_root", "symbolic links are not permitted")
    try:
        root = supplied.resolve(strict=True)
    except OSError as error:
        raise _fail("cat_root", f"cannot resolve: {error}") from error
    if not root.is_dir():
        raise _fail("cat_root", "not a directory")

    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for entry in entries:
        if entry.is_symlink():
            raise _fail(f"cat_root/{entry.relative_to(root).as_posix()}", "symbolic links are not permitted")
        if not entry.is_dir() and not entry.is_file():
            raise _fail(f"cat_root/{entry.relative_to(root).as_posix()}", "unsupported filesystem entry")

    rows: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for entry in entries:
        if not entry.is_file():
            continue
        relative = entry.relative_to(root).as_posix()
        _safe_relative(relative, f"cat_root/{relative}")
        payload = _stable_read(entry, f"cat_root/{relative}")
        payloads[relative] = payload
        rows.append(
            {
                "relative_path": relative,
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if "Cat.toc" not in payloads:
        raise _fail("cat_root", "missing Cat.toc")

    try:
        toc_source = payloads["Cat.toc"].decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _fail("cat_root/Cat.toc", f"not UTF-8: {error}") from error

    toc_entries: list[str] = []
    metadata: dict[str, list[str]] = {}
    for line_number, raw in enumerate(toc_source.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            key, separator, value = stripped[2:].partition(":")
            if separator:
                metadata.setdefault(key.strip(), []).append(value.strip())
            continue
        if stripped.startswith("#"):
            continue
        toc_entries.append(
            _safe_relative(
                stripped.replace("\\", "/"),
                f"cat_root/Cat.toc:{line_number}",
            )
        )

    folded_entries = [entry.casefold() for entry in toc_entries]
    duplicate_count = len(folded_entries) - len(set(folded_entries))
    rows_by_fold = {row["relative_path"].casefold(): row for row in rows}
    missing = [entry for entry in toc_entries if entry.casefold() not in rows_by_fold]
    if missing:
        raise _fail("cat_root/Cat.toc", f"missing listed files: {missing}")
    closure_rows = [rows_by_fold[entry.casefold()] for entry in toc_entries]
    listed = set(folded_entries)
    unlisted_code = sorted(
        row["relative_path"]
        for row in rows
        if PurePosixPath(row["relative_path"]).suffix.lower() in {".lua", ".xml"}
        and row["relative_path"].casefold() not in listed
    )

    critical: dict[str, str] = {}
    for relative in EXPECTED_CRITICAL_FILES:
        row = rows_by_fold.get(relative.casefold())
        if row is None:
            raise _fail("cat_root", f"missing critical file {relative}")
        critical[relative] = row["sha256"]

    closure = _partition(closure_rows, "cat_deployed_toc_closure")
    closure.update(
        {
            "lua_file_count": sum(path.lower().endswith(".lua") for path in toc_entries),
            "xml_file_count": sum(path.lower().endswith(".xml") for path in toc_entries),
            "duplicate_entry_count": duplicate_count,
            "missing_entry_count": len(missing),
            "unlisted_lua_or_xml_count": len(unlisted_code),
        }
    )
    saved_variables = metadata.get("SavedVariablesPerCharacter", [])
    return {
        "tree": _partition(rows, "cat_deployed_tree"),
        "toc_closure": closure,
        "critical_files": critical,
        "toc_metadata": {
            "interface": metadata.get("Interface", [None])[-1],
            "version": metadata.get("Version", [None])[-1],
            "saved_variables_per_character": saved_variables,
        },
        "unloaded_files": sorted(
            row["relative_path"]
            for row in rows
            if row["relative_path"].casefold() not in listed
        ),
        "unlisted_lua_or_xml": unlisted_code,
    }


def verify_source_tree(
    cat_root: str | Path, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Verify a live Cat directory against the pinned source identity."""

    document = dict(manifest) if manifest is not None else load_manifest()
    validate_manifest(document)
    facts = compute_source_tree_facts(cat_root)
    for key, expected in (
        ("tree", EXPECTED_TREE),
        ("toc_closure", EXPECTED_TOC_CLOSURE),
        ("critical_files", EXPECTED_CRITICAL_FILES),
        ("unloaded_files", ["Cat.toc", "\u4f7f\u7528\u6307\u5357.txt"]),
    ):
        _same(facts[key], expected, f"cat_root.{key}")
    _same(facts["unlisted_lua_or_xml"], [], "cat_root.unlisted_lua_or_xml")
    _same(facts["toc_metadata"]["interface"], "11200", "cat_root.toc_metadata.interface")
    _same(facts["toc_metadata"]["version"], "1.18.1", "cat_root.toc_metadata.version")
    if "MPWarriorFurySaved" not in facts["toc_metadata"]["saved_variables_per_character"]:
        raise _fail("cat_root.toc_metadata.saved_variables_per_character", "MPWarriorFurySaved missing")

    source = document["source"]
    readiness = document["comparison_readiness"]
    return {
        "schema": VERIFICATION_SCHEMA,
        "manifest_id": MANIFEST_ID,
        "source_id": SOURCE_ID,
        "status": "PASS",
        "tree": facts["tree"],
        "toc_closure": facts["toc_closure"],
        "installed_under_interface_addons": source["location"]["installed_under_interface_addons"],
        "client_load_observed": source["location"]["client_load_observed"],
        "policy_profile_included": source["authority"]["policy_profile_included"],
        "eligible_for_comparison": readiness["eligible_for_comparison"],
        "blockers": list(readiness["blockers"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cat_root", type=Path, help="external Cat addon directory")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        receipt = verify_source_tree(args.cat_root, manifest)
    except (CatDeployedSourceManifestError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
