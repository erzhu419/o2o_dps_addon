"""Aggregate the three required Fury baselines and Cat2's non-voting source.

Version 3 is a read-only readiness report.  It validates the frozen protocol,
expert registry, and source manifests, and can additionally verify the live
source trees.  Source presence or a source-derived action model never implies
that a policy has a profile, ran in the client, produced an accepted action,
or has a server outcome.  The older v1/v2 gates remain untouched.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

from . import cat2_capability_manifest_v1 as cat2_manifest_v1
from . import cat_deployed_source_manifest_v1 as cat_manifest_v1
from . import contra260817_source_manifest_v1 as contra260817_manifest_v1
from .cat2_action_plan_v1 import PLAN_SCHEMA as CAT2_ACTION_PLAN_SCHEMA
from .contra260817_fury_source_default_v1 import (
    FRESH_SOURCE_DEFAULT_PROFILE,
    PREDICTED_UPGRADE_PROFILE,
)
from .fury_multiseed_evaluation_v2 import materialize_protocol


SCHEMA = "fury_baseline_readiness_gate/v3"
GATE_ID = "fury.baseline_readiness.2026-09-10.v3"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDONS_ROOT = PROJECT_ROOT.parents[1]
DEFAULT_PROTOCOL = PROJECT_ROOT / "configs/evaluation/fury_multiseed_protocol_v2.json"
# V3 is a frozen historical report.  Keep its exact expert-registry bytes
# separate from the living registry used by newer runtime-bound work.
DEFAULT_EXPERT_MANIFEST = (
    PROJECT_ROOT
    / "configs/experts/fury_experts_v1.baseline_readiness_v3_frozen.json"
)
DEFAULT_RUNTIME_SNAPSHOT = (
    PROJECT_ROOT
    / "offline_data/expert_runtime_snapshots/v1"
    / "fury_expert_runtime_snapshot_v1.4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778.json"
)
DEFAULT_CAT_MANIFEST = cat_manifest_v1.DEFAULT_MANIFEST
DEFAULT_CONTRA260817_MANIFEST = contra260817_manifest_v1.DEFAULT_MANIFEST
DEFAULT_CAT2_MANIFEST = cat2_manifest_v1.DEFAULT_MANIFEST
DEFAULT_CAT_ROOT = ADDONS_ROOT / "Cat"
DEFAULT_CONTRA_DEPLOYED_ROOT = ADDONS_ROOT / "Contra"
DEFAULT_CONTRA260817_ROOT = contra260817_manifest_v1.DEFAULT_SOURCE_ROOT
DEFAULT_CAT2_ROOT = PROJECT_ROOT.parent / "Cat2_new"

EXPECTED_PROTOCOL_FILE_SHA256 = (
    "14ec63be011ad50a145567d359e9043f7604689a4ac49e1b9180168fbd64d420"
)
EXPECTED_PROTOCOL_SEMANTIC_SHA256 = (
    "2e13bee3a7f2f9cc938223c7fe23c21c880cc0bda8e8b763563af003afbd0d4e"
)
EXPECTED_EXPERT_MANIFEST_SHA256 = (
    "1a6a81089b256e4d15033e876e7b7a0a1450b752a345e2a2e8e8f0927a8eafac"
)
EXPECTED_CONTRA_TOC_SHA256 = (
    "7c3adbc5b75193f36da1567d6af355a7fdb6fa48a588de7979a2b3bd67e9673d"
)
EXPECTED_CONTRA_ADAPTER_V2_SHA256 = (
    "520a32274ca5368c7f26b1d11af06fdb90f19099ef612e5f8d51c41b4e7e6995"
)
EXPECTED_CONTRA260817_DEVELOPMENT_ADAPTER_SHA256 = (
    "ef89eeaa9d72a11da773983d96fe055c53a407573144772f22614d326893d483"
)
EXPECTED_CONTRA260817_FRESH_PROFILE_SHA256 = (
    "1c109be557b2390f7519ea8781f5e0cd49ed73adafa2ac411fceae9a5064b23a"
)
EXPECTED_CONTRA260817_UPGRADE_PROFILE_SHA256 = (
    "824fdaf46082b53b0976c378cb5130dcae378431f695b6798443f21b8d1ce1a5"
)
EXPECTED_CAT2_ACTION_PLAN_SOURCE_SHA256 = (
    "5e5b55f15988ada42631cb743862843ceaa6f3ba4166c48d262fa993db974c9d"
)
EXPECTED_RUNTIME_SNAPSHOT_SHA256 = (
    "4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778"
)
EXPECTED_RUNTIME_SNAPSHOT_FILE_SHA256 = (
    "a8624b5b8e6f081903f3ef1fdb7c9894ed24bf54783e7e7841ea6fd3e40aecfe"
)
EXPECTED_PROTOCOL_ID = (
    "brainofcat.fury.fixed_loadout.full_policy.multiseed.v2.2026-09-10"
)

CAT_POLICY = "cat.fury.profile1"
CONTRA_DEPLOYED_POLICY = "contra.deployed.fury.raid_a"
CONTRA260817_POLICY = "contra260817.fury.source_candidate"
CAT2_NONVOTING_POLICY = "cat2_new.fury.execution_and_candidate_source"
REQUIRED_BASELINE_IDS = (
    CAT_POLICY,
    CONTRA_DEPLOYED_POLICY,
    CONTRA260817_POLICY,
)
NONVOTING_SOURCE_IDS = (CAT2_NONVOTING_POLICY,)

READINESS_FIELDS = (
    "policy_profile",
    "runtime_load",
    "ordered_sink_trace",
    "client_acceptance_trace",
    "server_outcome_trace",
    "full_scenario_adapter",
)

CAT_READINESS = {
    "policy_profile": True,
    "runtime_load": False,
    "ordered_sink_trace": False,
    "client_acceptance_trace": False,
    "server_outcome_trace": False,
    "full_scenario_adapter": False,
}
CONTRA_DEPLOYED_READINESS = {
    "policy_profile": True,
    "runtime_load": False,
    "ordered_sink_trace": False,
    "client_acceptance_trace": False,
    "server_outcome_trace": False,
    "full_scenario_adapter": False,
}
CONTRA260817_READINESS = {field: False for field in READINESS_FIELDS}
CAT2_READINESS = {field: False for field in READINESS_FIELDS}

CAT_BLOCKERS = [
    "RUNTIME_LOAD_ATTESTATION_MISSING",
    "ORDERED_SINK_TRACE_MISSING",
    "CLIENT_ACCEPTANCE_TRACE_MISSING",
    "SERVER_OUTCOME_TRACE_MISSING",
    "FULL_POLICY_SIMULATOR_ADAPTER_MISSING",
]
CONTRA_DEPLOYED_BLOCKERS = [
    "RUNTIME_LOAD_ATTESTATION_MISSING",
    "HISTORICAL_TARGET_STATE_FIELDS_INCOMPLETE",
    "ORDERED_SINK_TRACE_MISSING",
    "CLIENT_ACCEPTANCE_TRACE_MISSING",
    "SERVER_OUTCOME_TRACE_MISSING",
    "FULL_POLICY_SIMULATOR_ADAPTER_MISSING",
]
CONTRA260817_BLOCKERS = [
    "PACKAGE_INCOMPLETE",
    "BOUNDED_MACRO_C_ADAPTER_DEVELOPMENT_SENSITIVITY_ONLY",
    "PER_CHARACTER_CONTRADB_PROFILE_MISSING",
    "RUNTIME_LOAD_ATTESTATION_MISSING",
    "ORDERED_SINK_TRACE_MISSING",
    "CLIENT_ACCEPTANCE_TRACE_MISSING",
    "SERVER_OUTCOME_TRACE_MISSING",
    "TARGET_SELECTION_AND_AUXILIARY_SINK_COVERAGE_MISSING",
    "FULL_POLICY_SIMULATOR_ADAPTER_MISSING",
]
CAT2_BLOCKERS = [
    "POLICY_PROFILE_MISSING",
    "SOURCE_TREE_NOT_DEPLOYED",
    "ACTION_PLAN_NOT_DISTILLED",
    "RUNTIME_LOAD_ATTESTATION_MISSING",
    "ORDERED_SINK_TRACE_MISSING",
    "CLIENT_ACCEPTANCE_TRACE_MISSING",
    "SERVER_OUTCOME_TRACE_MISSING",
    "FULL_POLICY_SIMULATOR_ADAPTER_MISSING",
    "NONVOTING_SOURCE_BY_PROTOCOL",
]

ENTRY_CONTRACTS = {
    CAT_POLICY: {
        "role": "REQUIRED_BASELINE",
        "authority_kind": "cat_deployed_source_manifest/v1",
        "authority_id": "cat.deployed_source.1_18_1.c31be2f9",
        "authority_sha256": cat_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "readiness": CAT_READINESS,
        "blockers": CAT_BLOCKERS,
        "profile_declaration_present": True,
        "runtime_snapshot_bound": True,
        "action_plan_status": None,
    },
    CONTRA_DEPLOYED_POLICY: {
        "role": "REQUIRED_BASELINE",
        "authority_kind": "multiseed_protocol_v2_plus_fury_expert_manifest_v1",
        "authority_id": "contra.deployed.fury.raid_a.v2",
        "authority_sha256": EXPECTED_CONTRA_ADAPTER_V2_SHA256,
        "readiness": CONTRA_DEPLOYED_READINESS,
        "blockers": CONTRA_DEPLOYED_BLOCKERS,
        "profile_declaration_present": True,
        "runtime_snapshot_bound": True,
        "action_plan_status": None,
    },
    CONTRA260817_POLICY: {
        "role": "REQUIRED_BASELINE",
        "authority_kind": "contra260817_source_manifest/v1",
        "authority_id": "contra260817.source.91baa120",
        "authority_sha256": contra260817_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "readiness": CONTRA260817_READINESS,
        "blockers": CONTRA260817_BLOCKERS,
        "profile_declaration_present": False,
        "runtime_snapshot_bound": False,
        "action_plan_status": None,
    },
    CAT2_NONVOTING_POLICY: {
        "role": "NONVOTING_CAPABILITY_SOURCE",
        "authority_kind": "cat2_capability_manifest/v1",
        "authority_id": "cat2.capabilities.2026-09-10.f7e659f9",
        "authority_sha256": cat2_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "readiness": CAT2_READINESS,
        "blockers": CAT2_BLOCKERS,
        "profile_declaration_present": False,
        "runtime_snapshot_bound": False,
        "action_plan_status": "PLAN_ONLY_NOT_DISTILLED",
    },
}


class FuryBaselineReadinessGateV3Error(RuntimeError):
    """A pinned input or readiness-report invariant failed."""


def _fail(message: str) -> FuryBaselineReadinessGateV3Error:
    return FuryBaselineReadinessGateV3Error(message)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _fail(f"report is not strict canonical JSON: {error}") from error


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _stable_read(path: str | Path, label: str) -> bytes:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise _fail(f"{label} must not be a symbolic link")
    try:
        resolved = supplied.resolve(strict=True)
        before = resolved.stat()
        if not resolved.is_file():
            raise _fail(f"{label} is not a regular file")
        payload = resolved.read_bytes()
        after = resolved.stat()
    except FuryBaselineReadinessGateV3Error:
        raise
    except OSError as error:
        raise _fail(f"cannot read {label}: {error}") from error
    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key or len(payload) != after.st_size:
        raise _fail(f"{label} changed while being read")
    return payload


def _load_pinned_json(
    path: str | Path,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    payload = _stable_read(path, label)
    observed = _sha256(payload)
    if observed != expected_sha256:
        raise _fail(
            f"{label} SHA-256 mismatch: expected {expected_sha256}, got {observed}"
        )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                FuryBaselineReadinessGateV3Error(
                    f"{label} contains non-finite JSON constant {item}"
                )
            ),
        )
    except FuryBaselineReadinessGateV3Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(f"{label} is not strict UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise _fail(f"{label} root must be an object")
    return value


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise _fail(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _require_file_hash(path: str | Path, expected: str, label: str) -> None:
    observed = _sha256(_stable_read(path, label))
    if observed != expected:
        raise _fail(f"{label} SHA-256 mismatch: expected {expected}, got {observed}")


def _expert_map(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    _require_equal(document.get("schema"), "fury_expert_manifest/v1", "expert schema")
    _require_equal(document.get("raw_sink_order_required"), True, "raw sink contract")
    claims = document.get("claims")
    if not isinstance(claims, Mapping):
        raise _fail("expert claims must be an object")
    _require_equal(claims.get("exact_runtime_labels_available"), False, "runtime-label claim")
    raw_experts = document.get("experts")
    if not isinstance(raw_experts, list):
        raise _fail("expert manifest experts must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(raw_experts):
        if not isinstance(item, Mapping):
            raise _fail(f"expert manifest entry {index} must be an object")
        expert_id = item.get("expert_id")
        if not isinstance(expert_id, str) or not expert_id:
            raise _fail(f"expert manifest entry {index} has no expert_id")
        if expert_id in result:
            raise _fail(f"duplicate expert_id {expert_id!r}")
        result[expert_id] = item
    for policy_id in (CAT_POLICY, CONTRA_DEPLOYED_POLICY):
        expert = result.get(policy_id)
        if expert is None:
            raise _fail(f"expert manifest lacks {policy_id}")
        _require_equal(expert.get("role"), "DEPLOYED", f"{policy_id}.role")
        profile = expert.get("frozen_profile")
        if not isinstance(profile, Mapping) or not profile:
            raise _fail(f"{policy_id} lacks a frozen profile declaration")
    return result


def _load_runtime_snapshot(
    path: str | Path,
    runtime_contract: Mapping[str, Any],
) -> dict[str, Any]:
    document = _load_pinned_json(
        path,
        EXPECTED_RUNTIME_SNAPSHOT_FILE_SHA256,
        "Fury expert runtime snapshot v1",
    )
    _require_equal(
        document.get("schema"),
        "fury_expert_runtime_snapshot/v1",
        "runtime snapshot schema",
    )
    supplied_digest = document.get("snapshot_sha256")
    core = dict(document)
    core.pop("snapshot_sha256", None)
    _require_equal(
        supplied_digest,
        _sha256(_canonical_bytes(core)),
        "runtime snapshot content address",
    )
    _require_equal(
        supplied_digest,
        EXPECTED_RUNTIME_SNAPSHOT_SHA256,
        "runtime snapshot frozen identity",
    )
    _require_equal(
        runtime_contract.get("snapshot_sha256"),
        supplied_digest,
        "protocol/runtime snapshot identity",
    )
    _require_equal(
        runtime_contract.get("snapshot_schema"),
        document["schema"],
        "protocol/runtime snapshot schema",
    )
    authority = document.get("authority")
    if not isinstance(authority, Mapping):
        raise _fail("runtime snapshot authority must be an object")
    for field in (
        "source_execution_observed",
        "client_acceptance_observed",
        "server_outcome_observed",
        "full_policy_simulator_adapter_complete",
        "comparison_eligible",
    ):
        _require_equal(authority.get(field), False, f"runtime snapshot authority.{field}")

    cat = document.get("cat_profile1")
    contra = document.get("contra_current_profile")
    fixed = document.get("fixed_character_build")
    inputs = document.get("inputs")
    if not all(isinstance(value, Mapping) for value in (cat, contra, fixed, inputs)):
        raise _fail("runtime snapshot profile/build/input sections must be objects")
    _require_equal(
        cat["profile_semantic_sha256"],
        runtime_contract.get("cat_profile1_semantic_sha256"),
        "runtime snapshot Cat profile",
    )
    _require_equal(
        contra["buttons_semantic_sha256"],
        runtime_contract.get("contra_buttons_semantic_sha256"),
        "runtime snapshot Contra profile",
    )
    _require_equal(
        fixed["player_semantic_sha256"],
        runtime_contract.get("fixed_player_semantic_sha256"),
        "runtime snapshot fixed player",
    )
    input_contracts = (
        ("cat_savedvariables", "cat_savedvariables_sha256"),
        ("contra_savedvariables", "contra_savedvariables_sha256"),
        ("nampower_dll", "nampower_dll_sha256"),
        ("superwow_hook_dll", "superwow_hook_dll_sha256"),
    )
    for input_key, contract_key in input_contracts:
        input_row = inputs.get(input_key)
        if not isinstance(input_row, Mapping):
            raise _fail(f"runtime snapshot input {input_key} must be an object")
        _require_equal(
            input_row.get("sha256"),
            runtime_contract.get(contract_key),
            f"runtime snapshot input {input_key}",
        )
    _require_equal(
        document.get("remaining_blockers"),
        [
            "CLIENT_LOAD_OF_THE_PINNED_EXPERT_SOURCE_NOT_OBSERVED_IN_THIS_ARTIFACT",
            "ORDERED_SINK_CLIENT_ACCEPTANCE_TRACE_NOT_BOUND",
            "SERVER_OUTCOME_TRACE_NOT_BOUND",
            "FULL_SCENARIO_BOUND_SIMULATOR_ADAPTER_NOT_COMPLETE",
        ],
        "runtime snapshot remaining blockers",
    )
    return document


def _load_protocol(path: str | Path) -> dict[str, Any]:
    document = _load_pinned_json(
        path,
        EXPECTED_PROTOCOL_FILE_SHA256,
        "multiseed protocol v2",
    )
    try:
        materialized = materialize_protocol(document)
    except Exception as error:
        raise _fail(f"multiseed protocol validation failed: {error}") from error
    _require_equal(
        materialized["protocol_sha256"],
        EXPECTED_PROTOCOL_SEMANTIC_SHA256,
        "multiseed protocol semantic hash",
    )
    _require_equal(
        tuple(materialized["required_baseline_ids"]),
        REQUIRED_BASELINE_IDS,
        "required baseline membership",
    )
    return materialized


def _protocol_maps(
    protocol: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    contract = protocol["baseline_contract"]
    required = {
        item["policy_id"]: item for item in contract["required_baselines"]
    }
    nonvoting = {
        item["policy_id"]: item for item in contract["nonvoting_sources"]
    }
    _require_equal(tuple(required), REQUIRED_BASELINE_IDS, "required baseline order")
    _require_equal(tuple(nonvoting), NONVOTING_SOURCE_IDS, "non-voting source order")
    for policy_id, entry in required.items():
        _require_equal(
            entry.get("comparison_eligible"),
            False,
            f"protocol baseline {policy_id} readiness",
        )
    return required, nonvoting


def _source_check(
    requested: bool,
    verifier: Callable[[], Mapping[str, Any]],
    summarizer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if not requested:
        return {"status": "NOT_REQUESTED", "failure_type": None, "facts": None}
    try:
        receipt = verifier()
        facts = dict(summarizer(receipt))
    except Exception as error:
        return {
            "status": "FAIL",
            "failure_type": type(error).__name__,
            "facts": None,
        }
    return {"status": "PASS", "failure_type": None, "facts": facts}


def _source_blocker(status: Mapping[str, Any]) -> str | None:
    if status["status"] == "PASS":
        return None
    if status["status"] == "NOT_REQUESTED":
        return "SOURCE_IDENTITY_NOT_LIVE_ATTESTED"
    return "SOURCE_IDENTITY_LIVE_VERIFICATION_FAILED"


def _with_source_blocker(
    blockers: Sequence[str], status: Mapping[str, Any]
) -> list[str]:
    result = list(blockers)
    extra = _source_blocker(status)
    if extra is not None:
        result.append(extra)
    return result


def _verify_deployed_contra(
    source_root: str | Path,
    source_sha256: str,
) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise _fail("deployed Contra root must be a non-symlink directory")
    _require_file_hash(root / "Contra.toc", EXPECTED_CONTRA_TOC_SHA256, "Contra.toc")
    _require_file_hash(root / "Contra_ALL.lua", source_sha256, "Contra_ALL.lua")
    return {
        "source_bundle_sha256": source_sha256,
        "toc_sha256": EXPECTED_CONTRA_TOC_SHA256,
    }


def _entry(
    *,
    policy_id: str,
    role: str,
    authority_kind: str,
    authority_id: str,
    authority_sha256: str,
    source_status: Mapping[str, Any],
    readiness: Mapping[str, bool],
    blockers: Sequence[str],
    source_order_model_available: bool,
    profile_declaration_present: bool,
    runtime_snapshot_bound: bool,
    action_plan_status: str | None = None,
) -> dict[str, Any]:
    ready = dict(readiness)
    _require_equal(set(ready), set(READINESS_FIELDS), f"{policy_id} readiness fields")
    return {
        "policy_id": policy_id,
        "role": role,
        "authority": {
            "kind": authority_kind,
            "id": authority_id,
            "sha256": authority_sha256,
            "strictly_verified": True,
        },
        "source_identity": dict(source_status),
        "profile_declaration_present": profile_declaration_present,
        "runtime_snapshot_bound": runtime_snapshot_bound,
        "source_order_model_available": source_order_model_available,
        "action_plan_status": action_plan_status,
        "readiness": ready,
        "comparison_eligible": False,
        "blockers": list(blockers),
    }


def build_readiness_report(
    *,
    protocol_path: str | Path = DEFAULT_PROTOCOL,
    expert_manifest_path: str | Path = DEFAULT_EXPERT_MANIFEST,
    runtime_snapshot_path: str | Path = DEFAULT_RUNTIME_SNAPSHOT,
    cat_manifest_path: str | Path = DEFAULT_CAT_MANIFEST,
    contra260817_manifest_path: str | Path = DEFAULT_CONTRA260817_MANIFEST,
    cat2_manifest_path: str | Path = DEFAULT_CAT2_MANIFEST,
    cat_root: str | Path = DEFAULT_CAT_ROOT,
    contra_deployed_root: str | Path = DEFAULT_CONTRA_DEPLOYED_ROOT,
    contra260817_root: str | Path = DEFAULT_CONTRA260817_ROOT,
    cat2_root: str | Path = DEFAULT_CAT2_ROOT,
    verify_live_sources: bool = True,
) -> dict[str, Any]:
    """Build a deterministic fail-closed readiness report."""

    materialized = _load_protocol(protocol_path)
    protocol = materialized["protocol"]
    required, nonvoting = _protocol_maps(protocol)

    runtime_contract = protocol.get("runtime_identity_contract")
    if not isinstance(runtime_contract, Mapping):
        raise _fail("protocol lacks runtime_identity_contract")
    runtime_snapshot = _load_runtime_snapshot(
        runtime_snapshot_path,
        runtime_contract,
    )

    expert_document = _load_pinned_json(
        expert_manifest_path,
        EXPECTED_EXPERT_MANIFEST_SHA256,
        "Fury expert manifest v1",
    )
    experts = _expert_map(expert_document)

    try:
        cat_manifest = cat_manifest_v1.load_manifest(cat_manifest_path)
        cat2_manifest = cat2_manifest_v1.load_manifest(cat2_manifest_path)
        contra260817_manifest = _load_pinned_json(
            contra260817_manifest_path,
            contra260817_manifest_v1.EXPECTED_MANIFEST_SHA256,
            "Contra260817 source manifest",
        )
        contra260817_manifest_v1._validate_static_manifest(contra260817_manifest)
    except Exception as error:
        raise _fail(f"source manifest verification failed: {error}") from error

    cat_protocol = required[CAT_POLICY]
    _require_equal(
        cat_protocol["source_bundle_sha256"],
        cat_manifest["identity"]["toc_closure"]["sha256"],
        "Cat protocol/source bundle",
    )
    _require_equal(
        cat_protocol["source_tree_sha256"],
        cat_manifest["identity"]["tree"]["sha256"],
        "Cat protocol/source tree",
    )
    _require_equal(
        cat_protocol["source_manifest_sha256"],
        cat_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "Cat protocol/manifest",
    )
    _require_equal(
        cat_protocol["warrior_fury_lua_sha256"],
        cat_manifest["identity"]["critical_files"]["WarriorFury.lua"],
        "Cat protocol/WarriorFury.lua",
    )
    _require_equal(
        cat_protocol["runtime_snapshot_sha256"],
        runtime_snapshot["snapshot_sha256"],
        "Cat protocol/runtime snapshot",
    )
    _require_equal(
        cat_protocol["profile_semantic_sha256"],
        runtime_snapshot["cat_profile1"]["profile_semantic_sha256"],
        "Cat protocol/profile semantic identity",
    )

    contra_protocol = required[CONTRA_DEPLOYED_POLICY]
    _require_equal(
        contra_protocol["adapter_v2_source_sha256"],
        EXPECTED_CONTRA_ADAPTER_V2_SHA256,
        "Contra deployed adapter pin",
    )
    _require_file_hash(
        Path(__file__).with_name("fury_contra_adapter_v2.py"),
        EXPECTED_CONTRA_ADAPTER_V2_SHA256,
        "Contra deployed adapter v2",
    )
    _require_equal(
        contra_protocol["runtime_snapshot_sha256"],
        runtime_snapshot["snapshot_sha256"],
        "Contra protocol/runtime snapshot",
    )
    _require_equal(
        contra_protocol["buttons_semantic_sha256"],
        runtime_snapshot["contra_current_profile"]["buttons_semantic_sha256"],
        "Contra protocol/profile semantic identity",
    )

    contra260817_protocol = required[CONTRA260817_POLICY]
    _require_equal(
        contra260817_protocol["source_bundle_sha256"],
        contra260817_manifest_v1.CODE_MANIFEST_SHA256,
        "Contra260817 protocol/source bundle",
    )
    _require_equal(
        contra260817_protocol["source_manifest_sha256"],
        contra260817_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "Contra260817 protocol/manifest",
    )
    _require_equal(
        contra260817_protocol["development_sensitivity_adapter_sha256"],
        EXPECTED_CONTRA260817_DEVELOPMENT_ADAPTER_SHA256,
        "Contra260817 bounded development adapter pin",
    )
    _require_file_hash(
        Path(__file__).with_name("contra260817_fury_source_default_v1.py"),
        EXPECTED_CONTRA260817_DEVELOPMENT_ADAPTER_SHA256,
        "Contra260817 bounded development adapter",
    )
    _require_equal(
        contra260817_protocol["fresh_source_default_profile_sha256"],
        EXPECTED_CONTRA260817_FRESH_PROFILE_SHA256,
        "Contra260817 fresh-source profile pin",
    )
    _require_equal(
        _sha256(_canonical_bytes(FRESH_SOURCE_DEFAULT_PROFILE.to_dict())),
        EXPECTED_CONTRA260817_FRESH_PROFILE_SHA256,
        "Contra260817 fresh-source profile identity",
    )
    _require_equal(
        contra260817_protocol["predicted_upgrade_profile_sha256"],
        EXPECTED_CONTRA260817_UPGRADE_PROFILE_SHA256,
        "Contra260817 predicted-upgrade profile pin",
    )
    _require_equal(
        _sha256(_canonical_bytes(PREDICTED_UPGRADE_PROFILE.to_dict())),
        EXPECTED_CONTRA260817_UPGRADE_PROFILE_SHA256,
        "Contra260817 predicted-upgrade profile identity",
    )

    cat2_protocol = nonvoting[CAT2_NONVOTING_POLICY]
    _require_equal(
        cat2_protocol["source_tree_sha256"],
        cat2_manifest["identity"]["tree"]["sha256"],
        "Cat2 protocol/source tree",
    )
    _require_equal(
        cat2_protocol["capability_manifest_sha256"],
        cat2_manifest_v1.EXPECTED_MANIFEST_SHA256,
        "Cat2 protocol/manifest",
    )
    _require_equal(
        cat2_protocol["action_plan_schema"],
        CAT2_ACTION_PLAN_SCHEMA,
        "Cat2 action-plan schema",
    )
    _require_equal(
        cat2_protocol["action_plan_compiler_source_sha256"],
        EXPECTED_CAT2_ACTION_PLAN_SOURCE_SHA256,
        "Cat2 action-plan compiler pin",
    )
    _require_equal(
        cat2_protocol["action_plan_status"],
        "PLAN_ONLY_NOT_DISTILLED",
        "Cat2 action-plan status",
    )
    _require_file_hash(
        Path(__file__).with_name("cat2_action_plan_v1.py"),
        EXPECTED_CAT2_ACTION_PLAN_SOURCE_SHA256,
        "Cat2 ActionPlan compiler",
    )

    cat_source = _source_check(
        verify_live_sources,
        lambda: cat_manifest_v1.verify_source_tree(cat_root, cat_manifest),
        lambda receipt: {
            "tree_sha256": receipt["tree"]["sha256"],
            "toc_closure_sha256": receipt["toc_closure"]["sha256"],
        },
    )
    contra_source = _source_check(
        verify_live_sources,
        lambda: _verify_deployed_contra(
            contra_deployed_root,
            contra_protocol["source_bundle_sha256"],
        ),
        lambda receipt: receipt,
    )
    contra260817_source = _source_check(
        verify_live_sources,
        lambda: contra260817_manifest_v1.verify_contra260817_source_package(
            manifest_path=Path(contra260817_manifest_path),
            source_root=Path(contra260817_root),
        ),
        lambda receipt: {
            "code_manifest_sha256": receipt["code_manifest"]["sha256"],
            "package_complete": receipt["package_complete"],
        },
    )
    cat2_source = _source_check(
        verify_live_sources,
        lambda: cat2_manifest_v1.verify_source_tree(cat2_root, cat2_manifest_path),
        lambda receipt: {
            "tree_sha256": receipt["tree"]["sha256"],
            "deployed": receipt["deployed"],
        },
    )

    cat_blockers = _with_source_blocker(CAT_BLOCKERS, cat_source)
    contra_blockers = _with_source_blocker(CONTRA_DEPLOYED_BLOCKERS, contra_source)
    contra260817_blockers = _with_source_blocker(
        CONTRA260817_BLOCKERS, contra260817_source
    )
    cat2_blockers = _with_source_blocker(CAT2_BLOCKERS, cat2_source)

    required_entries = [
        _entry(
            policy_id=CAT_POLICY,
            role="REQUIRED_BASELINE",
            authority_kind="cat_deployed_source_manifest/v1",
            authority_id=cat_manifest["manifest_id"],
            authority_sha256=cat_manifest_v1.EXPECTED_MANIFEST_SHA256,
            source_status=cat_source,
            readiness=CAT_READINESS,
            blockers=cat_blockers,
            source_order_model_available=True,
            profile_declaration_present="frozen_profile" in experts[CAT_POLICY],
            runtime_snapshot_bound=True,
        ),
        _entry(
            policy_id=CONTRA_DEPLOYED_POLICY,
            role="REQUIRED_BASELINE",
            authority_kind="multiseed_protocol_v2_plus_fury_expert_manifest_v1",
            authority_id="contra.deployed.fury.raid_a.v2",
            authority_sha256=contra_protocol["adapter_v2_source_sha256"],
            source_status=contra_source,
            readiness=CONTRA_DEPLOYED_READINESS,
            blockers=contra_blockers,
            source_order_model_available=True,
            profile_declaration_present="frozen_profile"
            in experts[CONTRA_DEPLOYED_POLICY],
            runtime_snapshot_bound=True,
        ),
        _entry(
            policy_id=CONTRA260817_POLICY,
            role="REQUIRED_BASELINE",
            authority_kind="contra260817_source_manifest/v1",
            authority_id=contra260817_manifest["manifest_id"],
            authority_sha256=contra260817_manifest_v1.EXPECTED_MANIFEST_SHA256,
            source_status=contra260817_source,
            readiness=CONTRA260817_READINESS,
            blockers=contra260817_blockers,
            source_order_model_available=True,
            profile_declaration_present=False,
            runtime_snapshot_bound=False,
        ),
    ]
    nonvoting_entries = [
        _entry(
            policy_id=CAT2_NONVOTING_POLICY,
            role="NONVOTING_CAPABILITY_SOURCE",
            authority_kind="cat2_capability_manifest/v1",
            authority_id=cat2_manifest["manifest_id"],
            authority_sha256=cat2_manifest_v1.EXPECTED_MANIFEST_SHA256,
            source_status=cat2_source,
            readiness=CAT2_READINESS,
            blockers=cat2_blockers,
            source_order_model_available=True,
            profile_declaration_present=False,
            runtime_snapshot_bound=False,
            action_plan_status=cat2_protocol["action_plan_status"],
        )
    ]

    core = {
        "schema": SCHEMA,
        "gate_id": GATE_ID,
        "protocol": {
            "protocol_id": protocol["protocol_id"],
            "file_sha256": EXPECTED_PROTOCOL_FILE_SHA256,
            "semantic_sha256": EXPECTED_PROTOCOL_SEMANTIC_SHA256,
            "required_baseline_ids": list(REQUIRED_BASELINE_IDS),
            "nonvoting_source_ids": list(NONVOTING_SOURCE_IDS),
        },
        "runtime_identity": {
            "schema": runtime_snapshot["schema"],
            "snapshot_sha256": runtime_snapshot["snapshot_sha256"],
            "strictly_verified": True,
            "source_execution_observed": False,
            "comparison_eligible_by_itself": False,
        },
        "readiness_contract": {
            "required_fields": list(READINESS_FIELDS),
            "all_fields_required": True,
            "source_presence_is_comparison_readiness": False,
            "source_order_model_is_ordered_sink_trace": False,
            "traversal_or_call_is_client_acceptance": False,
            "client_acceptance_is_server_outcome": False,
        },
        "required_baselines": required_entries,
        "nonvoting_sources": nonvoting_entries,
        "summary": {
            "status": "BLOCKED",
            "required_baseline_count": len(required_entries),
            "comparison_ready_baseline_count": 0,
            "all_required_baselines_ready": False,
            "comparison_allowed": False,
            "nonvoting_sources_can_vote": False,
        },
    }
    return {**core, "content_address": _content_address(core)}


def _content_address(core: Mapping[str, Any]) -> dict[str, str]:
    return {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": _sha256(_canonical_bytes(core)),
    }


def validate_readiness_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise _fail("report must be an object")
    expected_keys = {
        "schema",
        "gate_id",
        "protocol",
        "runtime_identity",
        "readiness_contract",
        "required_baselines",
        "nonvoting_sources",
        "summary",
        "content_address",
    }
    if set(report) != expected_keys:
        raise _fail("report top-level keys mismatch")
    _require_equal(report["schema"], SCHEMA, "report schema")
    _require_equal(report["gate_id"], GATE_ID, "report gate_id")
    core = {key: deepcopy(value) for key, value in report.items() if key != "content_address"}
    _require_equal(report["content_address"], _content_address(core), "content address")

    _require_equal(
        report["protocol"],
        {
            "protocol_id": EXPECTED_PROTOCOL_ID,
            "file_sha256": EXPECTED_PROTOCOL_FILE_SHA256,
            "semantic_sha256": EXPECTED_PROTOCOL_SEMANTIC_SHA256,
            "required_baseline_ids": list(REQUIRED_BASELINE_IDS),
            "nonvoting_source_ids": list(NONVOTING_SOURCE_IDS),
        },
        "report protocol identity",
    )
    _require_equal(
        report["runtime_identity"],
        {
            "schema": "fury_expert_runtime_snapshot/v1",
            "snapshot_sha256": EXPECTED_RUNTIME_SNAPSHOT_SHA256,
            "strictly_verified": True,
            "source_execution_observed": False,
            "comparison_eligible_by_itself": False,
        },
        "report runtime identity",
    )
    _require_equal(
        report["readiness_contract"],
        {
            "required_fields": list(READINESS_FIELDS),
            "all_fields_required": True,
            "source_presence_is_comparison_readiness": False,
            "source_order_model_is_ordered_sink_trace": False,
            "traversal_or_call_is_client_acceptance": False,
            "client_acceptance_is_server_outcome": False,
        },
        "report readiness contract",
    )

    required = report["required_baselines"]
    nonvoting = report["nonvoting_sources"]
    if not isinstance(required, list) or not isinstance(nonvoting, list):
        raise _fail("baseline collections must be arrays")
    _require_equal(
        tuple(item.get("policy_id") for item in required),
        REQUIRED_BASELINE_IDS,
        "report required baseline membership",
    )
    _require_equal(
        tuple(item.get("policy_id") for item in nonvoting),
        NONVOTING_SOURCE_IDS,
        "report non-voting membership",
    )
    for entry in [*required, *nonvoting]:
        policy_id = entry["policy_id"]
        expected = ENTRY_CONTRACTS[policy_id]
        _require_equal(entry.get("role"), expected["role"], f"{policy_id} role")
        _require_equal(
            entry.get("authority"),
            {
                "kind": expected["authority_kind"],
                "id": expected["authority_id"],
                "sha256": expected["authority_sha256"],
                "strictly_verified": True,
            },
            f"{policy_id} authority",
        )
        _require_equal(
            entry.get("profile_declaration_present"),
            expected["profile_declaration_present"],
            f"{policy_id} profile declaration",
        )
        _require_equal(
            entry.get("runtime_snapshot_bound"),
            expected["runtime_snapshot_bound"],
            f"{policy_id} runtime snapshot",
        )
        _require_equal(
            entry.get("source_order_model_available"),
            True,
            f"{policy_id} source-order model",
        )
        _require_equal(
            entry.get("action_plan_status"),
            expected["action_plan_status"],
            f"{policy_id} action-plan status",
        )
        _require_equal(
            entry.get("readiness"), expected["readiness"], f"{policy_id} readiness"
        )
        _require_equal(entry.get("comparison_eligible"), False, f"{policy_id} eligibility")
        source = entry.get("source_identity")
        if not isinstance(source, Mapping) or source.get("status") not in {
            "PASS",
            "FAIL",
            "NOT_REQUESTED",
        }:
            raise _fail(f"{policy_id} has invalid source verification status")
        _require_equal(
            set(source),
            {"status", "failure_type", "facts"},
            f"{policy_id} source verification fields",
        )
        if source["status"] == "PASS":
            if source["failure_type"] is not None or not isinstance(
                source["facts"], Mapping
            ) or not source["facts"]:
                raise _fail(f"{policy_id} PASS source verification lacks facts")
        elif source["status"] == "NOT_REQUESTED":
            _require_equal(
                (source["failure_type"], source["facts"]),
                (None, None),
                f"{policy_id} skipped source verification",
            )
        else:
            if not isinstance(source["failure_type"], str) or not source["failure_type"]:
                raise _fail(f"{policy_id} failed source verification lacks failure type")
            _require_equal(
                source["facts"], None, f"{policy_id} failed source verification facts"
            )
        _require_equal(
            entry.get("blockers"),
            _with_source_blocker(expected["blockers"], source),
            f"{policy_id} blockers",
        )
    summary = report["summary"]
    _require_equal(summary.get("status"), "BLOCKED", "summary status")
    _require_equal(summary.get("required_baseline_count"), 3, "summary baseline count")
    _require_equal(summary.get("comparison_ready_baseline_count"), 0, "summary ready count")
    _require_equal(summary.get("all_required_baselines_ready"), False, "summary all-ready")
    _require_equal(summary.get("comparison_allowed"), False, "summary comparison")
    _require_equal(summary.get("nonvoting_sources_can_vote"), False, "summary voting")
    return dict(report)


def serialize_readiness_report(report: Any) -> bytes:
    return _canonical_bytes(validate_readiness_report(report)) + b"\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--expert-manifest", type=Path, default=DEFAULT_EXPERT_MANIFEST)
    parser.add_argument("--runtime-snapshot", type=Path, default=DEFAULT_RUNTIME_SNAPSHOT)
    parser.add_argument("--cat-manifest", type=Path, default=DEFAULT_CAT_MANIFEST)
    parser.add_argument(
        "--contra260817-manifest", type=Path, default=DEFAULT_CONTRA260817_MANIFEST
    )
    parser.add_argument("--cat2-manifest", type=Path, default=DEFAULT_CAT2_MANIFEST)
    parser.add_argument("--cat-root", type=Path, default=DEFAULT_CAT_ROOT)
    parser.add_argument(
        "--contra-deployed-root", type=Path, default=DEFAULT_CONTRA_DEPLOYED_ROOT
    )
    parser.add_argument(
        "--contra260817-root", type=Path, default=DEFAULT_CONTRA260817_ROOT
    )
    parser.add_argument("--cat2-root", type=Path, default=DEFAULT_CAT2_ROOT)
    parser.add_argument("--skip-live-source-verification", action="store_true")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="return exit code 3 after emitting the report when the gate is blocked",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_readiness_report(
            protocol_path=args.protocol,
            expert_manifest_path=args.expert_manifest,
            runtime_snapshot_path=args.runtime_snapshot,
            cat_manifest_path=args.cat_manifest,
            contra260817_manifest_path=args.contra260817_manifest,
            cat2_manifest_path=args.cat2_manifest,
            cat_root=args.cat_root,
            contra_deployed_root=args.contra_deployed_root,
            contra260817_root=args.contra260817_root,
            cat2_root=args.cat2_root,
            verify_live_sources=not args.skip_live_source_verification,
        )
        sys.stdout.buffer.write(serialize_readiness_report(report))
    except (FuryBaselineReadinessGateV3Error, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2
    if args.require_ready and not report["summary"]["comparison_allowed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAT2_NONVOTING_POLICY",
    "CAT_POLICY",
    "CONTRA260817_POLICY",
    "CONTRA_DEPLOYED_POLICY",
    "DEFAULT_PROTOCOL",
    "GATE_ID",
    "READINESS_FIELDS",
    "REQUIRED_BASELINE_IDS",
    "SCHEMA",
    "FuryBaselineReadinessGateV3Error",
    "build_readiness_report",
    "serialize_readiness_report",
    "validate_readiness_report",
]
