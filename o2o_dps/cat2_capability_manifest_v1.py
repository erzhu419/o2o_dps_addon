"""Fail-closed verifier for the external Cat2 2026-09-10 source tree.

This module never executes Lua and never vendors Cat2.  It verifies the local
extracted tree against a versioned capability manifest kept in this source-only
repository.  The manifest describes an undeployed, non-voting capability
source; it is not a Cat2 policy profile or runtime result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Mapping, Sequence


SCHEMA = "cat2_capability_manifest/v1"
VERIFICATION_SCHEMA = "cat2_capability_verification/v1"
MANIFEST_ID = "cat2.capabilities.2026-09-10.f7e659f9"
SOURCE_ID = "cat2.source_tree.2026-09-10.f7e659f9"
DECLARED_VERSION = "2026-09-10"
EXPECTED_MANIFEST_SHA256 = (
    "3948a01fc1a4dcd33590b8cc023a457526928b22e0f9e00ecd9890808230d164"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(
    os.environ.get(
        "BOC_CAT2_CAPABILITY_MANIFEST",
        str(
            PROJECT_ROOT
            / "configs"
            / "experts"
            / "cat2_capabilities_2026_09_10_f7e659f9.json"
        ),
    )
)

EXPECTED_PARTITIONS = {
    "tree": {
        "relative_prefix": "",
        "file_count": 613,
        "byte_count": 1_971_335,
        "sha256": "f7e659f9042d36b078d4c3ad6860742d9b2946bab455630c6db071c6ebaa6d81",
    },
    "cards": {
        "relative_prefix": "Cards/",
        "file_count": 570,
        "byte_count": 1_126_774,
        "sha256": "b57449fbd70b43040ed7e2291c6901cac3ef26a0a739036992ef0fc8c3cf3b77",
    },
    "warrior": {
        "relative_prefix": "Cards/Warrior/",
        "file_count": 55,
        "byte_count": 92_536,
        "sha256": "820f893347db2a1fce6e4bef2f47ca03687b63045d8c10d1a7b2ec17568ee904",
    },
}
EXPECTED_CRITICAL_FILES = {
    "Cat2.lua": "980eebb7be7a6831e06d0417ed466873a39051acb7060e6639541771c7c63f6f",
    "Cat2.toc": "29f9fce890b1225a3e64e7b72c91f5d0ea2f956a77dd08a117f0ce683d882da2",
    "Core/CardRegistry.lua": "8104af5f3294daa1f29174c5504005aa2e04ce65c9d39bf88b49975102d31da7",
    "Core/ConfigurationRunner.lua": "40e8bdcf525a6a7422d07c24d91b3549a5a6667b1307ed8d55e9fcfc0d9ab236",
    "Core/CatLib.lua": "9102269c37a7d17442df556bab05e9f28774d9a365b52d70a181a227cecc951c",
    "Core/CastInterruptMonitor.lua": "54689fbe90b80c86334d0dbee3ddfea2cc9e67f2f68bcd863f5f1998a3d4a9f1",
    "Core/CatEvent.lua": "061e0b7b6e2922b026ba24c383d48b155b1d21fcf0775b6f7650caeaeef1f8b2",
    "Core/CatImmuneLib.lua": "05f45fdc792bb7b52ff0e7bc98e35844d0f295f376ebd6413b5e9436ee006ab3",
    "Core/Persistence.lua": "91b6e2d5b977fd21e3baaa271e64469a3e3c22804e2cbc72293e3220271cf4f4",
    "Core/PlayerInformation.lua": "f3b1dcb5466d8c654e6f112429a75ca93fb358ba94a83408f4869dec8ca3b832",
    "Cards/Common/AutoAttack.lua": "6e6247ff826e3e8b58c74300609cd3fedd1f2f3ce6a439b235c179f68fac5a1d",
    "Cards/Common/FlowTerminate.lua": "354d27c4b9854f589804be5c2d31f19be418ef57feb0713b1550bed85daef9d3",
}
EXPECTED_CONTINUING_CARDS = [
    "warrior_bloodrage",
    "warrior_cleave",
    "warrior_execute_nearby_target",
    "warrior_heroic_strike",
    "warrior_heroic_strike_alt",
    "warrior_shield_block",
]
EXPECTED_PATH_SINKS = {
    "pending_interrupt_prepass": ["SpellStopCasting"],
    "execute_interrupt_then_cast": ["SpellStopCasting", "Cat2.Cast"],
    "nearby_execute_loop": [
        "SpellStopCasting",
        "TargetUnit",
        "CastSpellByName",
        "TargetUnit_OR_ClearTarget",
    ],
    "profile_order_nonblocking_chain": [],
    "auto_stance_multi_press": ["Cat2.Cast_STANCE", "Cat2.Cast_ABILITY"],
}
EXPECTED_BOUNDARY_IDS = {
    "ARCHIVE_BYTES_UNAVAILABLE",
    "TREE_NOT_DEPLOYED",
    "NO_POLICY_PROFILE",
    "BOOLEAN_NOT_ACCEPTANCE",
    "NEARBY_SCAN_IS_CACHED",
    "FRONT_FILTER_REQUIRES_UNITXP",
    "BOSS_GATE_USES_CURRENT_HEALTH",
    "NEARBY_EXECUTE_ORDER_UNSPECIFIED",
    "ACTIVE_ERRORS_CONTINUE",
    "DIRECT_CARD_STEP_OMITTED",
    "SAVEDVARIABLES_SCHEMA_IS_NOT_SEMANTIC_IDENTITY",
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CARD_ID_RE = re.compile(r'^\s*id\s*=\s*"([^"]+)"', re.MULTILINE)
_WARRIOR_SPEC_RE = re.compile(r"^\s*WARRIOR\s*=\s*([123])\s*,?", re.MULTILINE)
_PASSIVE_RE = re.compile(r'^\s*behavior\s*=\s*"passive"\s*,?', re.MULTILINE)
_VERSION_RE = re.compile(r'^\s*Cat2\.Version\s*=\s*"([^"]+)"', re.MULTILINE)


class Cat2CapabilityManifestError(ValueError):
    """The manifest or external source tree failed a strict check."""


def _fail(path: str, message: str) -> Cat2CapabilityManifestError:
    return Cat2CapabilityManifestError(f"{path}: {message}")


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
    except Cat2CapabilityManifestError:
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
    """Check the authority boundary and the machine-readable execution contract."""

    root = _object(document, "manifest")
    _keys(
        root,
        {
            "schema",
            "manifest_id",
            "audit_date",
            "source",
            "identity",
            "warrior_inventory",
            "reviewed_capabilities",
            "execution_contract",
            "known_boundaries",
            "lineage",
        },
        "manifest",
    )
    _same(root["schema"], SCHEMA, "manifest.schema")
    _same(root["manifest_id"], MANIFEST_ID, "manifest.manifest_id")

    source = _object(root["source"], "manifest.source")
    _same(source["source_id"], SOURCE_ID, "manifest.source.source_id")
    _same(source["declared_version"], DECLARED_VERSION, "manifest.source.declared_version")
    _same(
        dict(_object(source["archive"], "manifest.source.archive")),
        {
            "requested_name": "Cat2 2026-09-10.zip",
            "status": "NOT_FOUND",
            "sha256": None,
            "identity_claim": "EXTRACTED_TREE_ONLY",
        },
        "manifest.source.archive",
    )
    _same(
        dict(_object(source["location"], "manifest.source.location")),
        {
            "kind": "EXTERNAL_LOCAL_TREE",
            "expected_directory_name": "Cat2_new",
            "embedded_third_party_lua": False,
        },
        "manifest.source.location",
    )
    _same(
        dict(_object(source["authority"], "manifest.source.authority")),
        {
            "role": "CAPABILITY_SOURCE",
            "provenance_kind": "SOURCE_DERIVED",
            "deployed": False,
            "source_execution": False,
            "exact_runtime": False,
            "eligible_for_independent_vote": False,
            "policy_profile_included": False,
        },
        "manifest.source.authority",
    )

    identity = _object(root["identity"], "manifest.identity")
    for name, expected in EXPECTED_PARTITIONS.items():
        _same(dict(_object(identity[name], f"manifest.identity.{name}")), expected, f"manifest.identity.{name}")
    _same(
        dict(_object(identity["critical_files"], "manifest.identity.critical_files")),
        EXPECTED_CRITICAL_FILES,
        "manifest.identity.critical_files",
    )
    toc = _object(identity["toc"], "manifest.identity.toc")
    _same(toc["sha256"], EXPECTED_CRITICAL_FILES["Cat2.toc"], "manifest.identity.toc.sha256")
    _same(toc["load_entry_count"], 611, "manifest.identity.toc.load_entry_count")
    _same(toc["lua_file_count"], 611, "manifest.identity.toc.lua_file_count")
    _same(toc["complete_lua_coverage"], True, "manifest.identity.toc.complete_lua_coverage")
    for key in ("duplicate_entry_count", "missing_entry_count", "unlisted_lua_count"):
        _same(toc[key], 0, f"manifest.identity.toc.{key}")

    inventory = _object(root["warrior_inventory"], "manifest.warrior_inventory")
    _same(inventory["card_count"], 55, "manifest.warrior_inventory.card_count")
    _same(inventory["active_count"], 53, "manifest.warrior_inventory.active_count")
    _same(inventory["passive_count"], 2, "manifest.warrior_inventory.passive_count")
    spec_ids: list[str] = []
    for name, index in (("arms", 1), ("fury", 2), ("protection", 3)):
        spec = _object(inventory["specializations"][name], f"manifest.warrior_inventory.{name}")
        _same(spec["index"], index, f"manifest.warrior_inventory.{name}.index")
        ids = _array(spec["card_ids"], f"manifest.warrior_inventory.{name}.card_ids")
        _same(spec["count"], len(ids), f"manifest.warrior_inventory.{name}.count")
        spec_ids.extend(ids)
    if len(spec_ids) != 55 or len(set(spec_ids)) != 55:
        raise _fail("manifest.warrior_inventory", "Warrior card IDs are not 55 unique values")

    capabilities = _array(root["reviewed_capabilities"], "manifest.reviewed_capabilities")
    if len(capabilities) != 7:
        raise _fail("manifest.reviewed_capabilities", "expected seven reviewed capabilities")
    capability_ids: set[str] = set()
    for index, raw in enumerate(capabilities):
        item = _object(raw, f"manifest.reviewed_capabilities[{index}]")
        card_id = item["card_id"]
        if card_id in capability_ids:
            raise _fail("manifest.reviewed_capabilities", f"duplicate card {card_id!r}")
        capability_ids.add(card_id)
        _safe_relative(item["relative_path"], f"manifest.reviewed_capabilities[{index}].relative_path")
        if not isinstance(item["sha256"], str) or not _SHA256_RE.fullmatch(item["sha256"]):
            raise _fail(f"manifest.reviewed_capabilities[{index}].sha256", "invalid SHA-256")

    contract = _object(root["execution_contract"], "manifest.execution_contract")
    _same(contract["entry"], "Cat2.ExecuteConfiguration", "manifest.execution_contract.entry")
    _same(
        contract["phase_order"],
        [
            "PENDING_INTERRUPT",
            "REFRESH_PLAYER_SNAPSHOT",
            "PASSIVE_APPLY",
            "PASSIVE_VALIDATE",
            "ACTIVE_PROFILE_STEP_ORDER",
        ],
        "manifest.execution_contract.phase_order",
    )
    _same(contract["active_error_behavior"], "RECORD_FAILURE_AND_CONTINUE", "manifest.execution_contract.active_error_behavior")
    returns = _object(contract["return_semantics"], "manifest.execution_contract.return_semantics")
    _same(returns["true"], "STOP_CURRENT_CONFIGURATION_PASS_ONLY", "manifest.execution_contract.return_semantics.true")
    _same(returns["non_true"], "CONTINUE_CURRENT_CONFIGURATION_PASS", "manifest.execution_contract.return_semantics.non_true")
    _same(returns["client_acceptance_inferred"], False, "manifest.execution_contract.return_semantics.client_acceptance_inferred")
    _same(returns["server_outcome_inferred"], False, "manifest.execution_contract.return_semantics.server_outcome_inferred")
    sinks = _object(contract["sink_semantics"], "manifest.execution_contract.sink_semantics")
    _same(sinks["card_execute_boolean_is_action_label"], False, "manifest.execution_contract.sink_semantics.card_execute_boolean_is_action_label")
    _same(sinks["ordered_sink_trace_required"], True, "manifest.execution_contract.sink_semantics.ordered_sink_trace_required")
    _same(sinks["client_acceptance_trace_required"], True, "manifest.execution_contract.sink_semantics.client_acceptance_trace_required")
    _same(sinks["server_outcome_trace_required"], True, "manifest.execution_contract.sink_semantics.server_outcome_trace_required")
    _same(contract["continuing_warrior_cards"], EXPECTED_CONTINUING_CARDS, "manifest.execution_contract.continuing_warrior_cards")
    path_sinks: dict[str, list[str]] = {}
    for raw in _array(contract["multi_sink_paths"], "manifest.execution_contract.multi_sink_paths"):
        item = _object(raw, "manifest.execution_contract.multi_sink_paths[]")
        path_id = item["path_id"]
        if path_id in path_sinks:
            raise _fail("manifest.execution_contract.multi_sink_paths", f"duplicate path {path_id!r}")
        ordered = _array(item["ordered_sinks"], f"manifest.execution_contract.{path_id}.ordered_sinks")
        for expected_order, sink in enumerate(ordered, start=1):
            _same(sink["order"], expected_order, f"manifest.execution_contract.{path_id}.order")
        path_sinks[path_id] = [sink["sink"] for sink in ordered]
    _same(path_sinks, EXPECTED_PATH_SINKS, "manifest.execution_contract.multi_sink_paths")
    _same(contract["zero_sink_true_example"]["action_sink_count"], 0, "manifest.execution_contract.zero_sink_true_example")
    _same(contract["direct_execution_boundary"]["passes_step_argument"], False, "manifest.execution_contract.direct_execution_boundary")

    boundaries = _array(root["known_boundaries"], "manifest.known_boundaries")
    boundary_ids = [item["id"] for item in boundaries]
    if len(boundary_ids) != len(set(boundary_ids)):
        raise _fail("manifest.known_boundaries", "duplicate boundary ID")
    _same(set(boundary_ids), EXPECTED_BOUNDARY_IDS, "manifest.known_boundaries")

    _same(
        dict(_object(root["lineage"], "manifest.lineage")),
        {
            "relation": "CANDIDATE_SUCCESSOR_OF",
            "frozen_predecessor_expert_id": "cat2.fury.brainofcat_shadow.saved_profile_source_v1",
            "frozen_predecessor_source_bundle_sha256": "1d591e5ef60222a0f6eb1e081b4f43feb4cfa1576103558689dc6fda2aa84970",
            "replaces_predecessor": False,
            "reuse_predecessor_results": False,
            "migration_requires_new_profile_snapshot": True,
            "promotion_requires_deployed_tree_and_exact_sink_trace": True,
        },
        "manifest.lineage",
    )


def _partition(rows: Sequence[Mapping[str, Any]], prefix: str) -> dict[str, Any]:
    selected = [row for row in rows if not prefix or row["relative_path"].startswith(prefix)]
    identity = [
        {
            "root_kind": "cat2",
            "relative_path": row["relative_path"],
            "sha256": row["sha256"],
        }
        for row in selected
    ]
    return {
        "relative_prefix": prefix,
        "file_count": len(selected),
        "byte_count": sum(row["byte_count"] for row in selected),
        "sha256": hashlib.sha256(_canonical_bytes(identity)).hexdigest(),
    }


def compute_source_tree_facts(cat2_root: str | Path) -> dict[str, Any]:
    """Inspect one source tree without executing Lua or writing an artifact."""

    supplied = Path(cat2_root).expanduser()
    if supplied.is_symlink():
        raise _fail("cat2_root", "symbolic links are not permitted")
    try:
        root = supplied.resolve(strict=True)
    except OSError as error:
        raise _fail("cat2_root", f"cannot resolve: {error}") from error
    if not root.is_dir():
        raise _fail("cat2_root", "not a directory")

    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for entry in entries:
        if entry.is_symlink():
            raise _fail(f"cat2_root/{entry.relative_to(root).as_posix()}", "symbolic links are not permitted")
        if not entry.is_dir() and not entry.is_file():
            raise _fail(f"cat2_root/{entry.relative_to(root).as_posix()}", "unsupported filesystem entry")

    rows: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for entry in entries:
        if not entry.is_file():
            continue
        relative = entry.relative_to(root).as_posix()
        _safe_relative(relative, f"cat2_root/{relative}")
        payload = _stable_read(entry, f"cat2_root/{relative}")
        payloads[relative] = payload
        rows.append(
            {
                "relative_path": relative,
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    for required in ("Cat2.lua", "Cat2.toc"):
        if required not in payloads:
            raise _fail("cat2_root", f"missing {required}")

    try:
        cat2_source = payloads["Cat2.lua"].decode("utf-8")
        toc_source = payloads["Cat2.toc"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise _fail("cat2_root", f"root metadata is not UTF-8: {error}") from error
    versions = _VERSION_RE.findall(cat2_source)
    if len(versions) != 1:
        raise _fail("cat2_root/Cat2.lua", "expected exactly one Cat2.Version")

    toc_entries: list[str] = []
    for line_number, raw in enumerate(toc_source.splitlines(), start=1):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            toc_entries.append(
                _safe_relative(
                    stripped.replace("\\", "/"),
                    f"cat2_root/Cat2.toc:{line_number}",
                )
            )
    lua_files = sorted(relative for relative in payloads if relative.endswith(".lua"))
    toc_set, lua_set = set(toc_entries), set(lua_files)

    card_files = sorted(
        relative
        for relative in payloads
        if relative.startswith("Cards/") and relative.endswith(".lua")
    )
    seen_ids: dict[str, str] = {}
    warrior_ids: dict[int, list[str]] = {1: [], 2: [], 3: []}
    passive_ids: list[str] = []
    for relative in card_files:
        try:
            source = payloads[relative].decode("utf-8")
        except UnicodeDecodeError as error:
            raise _fail(f"cat2_root/{relative}", f"not UTF-8: {error}") from error
        card_ids = _CARD_ID_RE.findall(source)
        if len(card_ids) != 1:
            raise _fail(f"cat2_root/{relative}", "expected exactly one card id")
        card_id = card_ids[0]
        if card_id in seen_ids:
            raise _fail(f"cat2_root/{relative}", f"duplicate card id {card_id!r}")
        if len(re.findall(r"\bCat2\.RegisterCard\s*\(", source)) != 1:
            raise _fail(f"cat2_root/{relative}", "expected one RegisterCard call")
        seen_ids[card_id] = relative
        if relative.startswith("Cards/Warrior/"):
            specs = _WARRIOR_SPEC_RE.findall(source)
            if len(specs) != 1:
                raise _fail(f"cat2_root/{relative}", "expected one Warrior specialization")
            warrior_ids[int(specs[0])].append(card_id)
            if _PASSIVE_RE.search(source):
                passive_ids.append(card_id)
    for values in warrior_ids.values():
        values.sort()
    passive_ids.sort()

    file_hashes = {row["relative_path"]: row["sha256"] for row in rows}
    missing_toc_paths = toc_set - set(payloads)
    non_lua_toc_paths = toc_set - lua_set
    return {
        "declared_version": versions[0],
        "tree": _partition(rows, ""),
        "cards": _partition(rows, "Cards/"),
        "warrior": _partition(rows, "Cards/Warrior/"),
        "toc": {
            "sha256": file_hashes["Cat2.toc"],
            "load_entry_count": len(toc_entries),
            "lua_file_count": len(lua_files),
            "duplicate_entry_count": len(toc_entries) - len(toc_set),
            "missing_entry_count": len(missing_toc_paths | non_lua_toc_paths),
            "unlisted_lua_count": len(lua_set - toc_set),
        },
        "card_registry": {
            "card_count": len(card_files),
            "unique_card_id_count": len(seen_ids),
            "warrior_ids_by_index": warrior_ids,
            "warrior_passive_ids": passive_ids,
        },
        "file_sha256": file_hashes,
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def verify_source_tree(
    cat2_root: str | Path,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify the exact source identity and return a compact receipt."""

    document = load_manifest(manifest_path)
    try:
        source_root = Path(cat2_root).expanduser().resolve(strict=True)
        repository_root = Path(project_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise _fail("path", f"cannot resolve verification boundary: {error}") from error
    if source_root.name != "Cat2_new":
        raise _fail("cat2_root", "expected directory name Cat2_new")
    if _is_within(source_root, repository_root):
        raise _fail("cat2_root", "third-party Cat2 source must remain outside the repository")

    facts = compute_source_tree_facts(source_root)
    _same(facts["declared_version"], DECLARED_VERSION, "cat2_root.declared_version")
    for name in ("tree", "cards", "warrior"):
        _same(facts[name], document["identity"][name], f"cat2_root.{name}")
    toc_expected = document["identity"]["toc"]
    _same(
        facts["toc"],
        {
            key: toc_expected[key]
            for key in (
                "sha256",
                "load_entry_count",
                "lua_file_count",
                "duplicate_entry_count",
                "missing_entry_count",
                "unlisted_lua_count",
            )
        },
        "cat2_root.toc",
    )
    for relative, expected in document["identity"]["critical_files"].items():
        _same(facts["file_sha256"].get(relative), expected, f"cat2_root/{relative}")
    for item in document["reviewed_capabilities"]:
        _same(
            facts["file_sha256"].get(item["relative_path"]),
            item["sha256"],
            f"cat2_root/{item['relative_path']}",
        )

    registry = facts["card_registry"]
    _same(registry["card_count"], 570, "cat2_root.card_registry.card_count")
    _same(registry["unique_card_id_count"], 570, "cat2_root.card_registry.unique_card_id_count")
    for name, index in (("arms", 1), ("fury", 2), ("protection", 3)):
        manifest_ids = document["warrior_inventory"]["specializations"][name]["card_ids"]
        _same(registry["warrior_ids_by_index"][index], sorted(manifest_ids), f"cat2_root.card_registry.{name}")
    _same(
        registry["warrior_passive_ids"],
        sorted(document["warrior_inventory"]["passive_card_ids"]),
        "cat2_root.card_registry.passive_ids",
    )

    return {
        "schema": VERIFICATION_SCHEMA,
        "status": "PASS",
        "manifest_id": MANIFEST_ID,
        "source_id": SOURCE_ID,
        "cat2_root": str(source_root),
        "archive_status": "NOT_FOUND",
        "deployed": False,
        "eligible_for_independent_vote": False,
        "tree": facts["tree"],
        "cards": facts["cards"],
        "warrior": facts["warrior"],
        "toc": facts["toc"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the external Cat2 2026-09-10 capability source."
    )
    parser.add_argument("--cat2-root", required=True)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    try:
        receipt = verify_source_tree(
            args.cat2_root, args.manifest, project_root=args.project_root
        )
    except (Cat2CapabilityManifestError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
