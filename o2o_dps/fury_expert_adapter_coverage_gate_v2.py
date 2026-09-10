"""Audit exceptional Fury expert lanes without mutating frozen V1 artifacts.

The canonical loadout/armor canary exposed three kinds of exceptional policy
projection: Cat's low-rage non-boss Execute calls, Cat2's bounded Whirlwind
cooldown retries, and the current adapter/data gap for Contra's two-hand
non-boss branch.  This
sidecar replays the exact 384-episode matrix twice, classifies every observed
omission or declared source-API no-op, and writes a deterministic event ledger.

This is deliberately not an exact Lua replay and it does not publish a DPS
ranking.  In particular, a classified source no-op with a runner-chosen retry
cadence remains ineligible for DPS comparison, and a missing source guard input
withholds the current policy lane rather than deleting affected decisions or
discarding that expert.  Contra remains a required strong baseline once its
repairable target-state features are reconstructed and its entry is attested.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from math import isclose
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .fury_expert_closed_loop import run_fury_expert_closed_loop
from .fury_loadout_armor_nuisance_canary_v1 import (
    DEFAULT_BRIDGE,
    DEFAULT_CAT2_PROFILE,
    DEFAULT_FROZEN_GATE,
    DEFAULT_PLAN as DEFAULT_V1_PLAN,
    _finalize_proc_observation,
    _load_blob,
    _load_json,
    _policy_adapters,
    _proc_observer,
    _verify_plan_inputs,
    request_with_loadout_and_armor,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WOW_ROOT = Path(os.environ.get("BOC_WOW_ROOT", r"D:\WOW"))
ADDONS_ROOT = Path(
    os.environ.get(
        "BOC_WOW_ADDONS_ROOT",
        str(DEFAULT_WOW_ROOT / "Interface" / "AddOns"),
    )
)
CHARACTER_SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CHARACTER_SAVEDVARIABLES",
        str(PROJECT_ROOT / ".local" / "SavedVariables"),
    )
)

DEFAULT_V1_ARTIFACT = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_loadout_armor_nuisance_canary_v1.json"
)
DEFAULT_V1_RECEIPT = Path(str(DEFAULT_V1_ARTIFACT) + ".receipt.json")
DEFAULT_ABSOLUTE_FULL = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_current_cat2_supplemental_absolute_seed_v1.full.json"
)
DEFAULT_ABSOLUTE_LOCK = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_current_cat2_supplemental_absolute_seed_v1.input-lock.json"
)
DEFAULT_CAT2_NOOP = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_current_cat2_noop_adjudication_absolute_seed_v1.json"
)
DEFAULT_HISTORICAL_STATES = (
    PROJECT_ROOT
    / "offline_data/derived/fury_historical_state_catalog/v1/states.jsonl"
)
DEFAULT_ADAPTER_V1 = PROJECT_ROOT / "o2o_dps/fury_expert_adapters.py"
DEFAULT_CLOSED_LOOP_V1 = PROJECT_ROOT / "o2o_dps/fury_expert_closed_loop.py"
DEFAULT_GUIDED_STATE_V1 = PROJECT_ROOT / "o2o_dps/fury_expert_guided_search_v1.py"
DEFAULT_SELF = Path(__file__).resolve()

CAT_TOC = ADDONS_ROOT / "Cat/Cat.toc"
CAT_FURY = ADDONS_ROOT / "Cat/WarriorFury.lua"
CONTRA_TOC = ADDONS_ROOT / "Contra/Contra.toc"
CONTRA_ALL = ADDONS_ROOT / "Contra/Contra_ALL.lua"
CONTRA_UNLOADED_NEIGHBOR = ADDONS_ROOT / "Contra/Contra.lua"
CAT_SAVED = Path(
    os.environ.get("BOC_CAT_SAVEDVARIABLES", str(CHARACTER_SAVEDVARIABLES / "Cat.lua"))
)
CONTRA_SAVED = Path(
    os.environ.get(
        "BOC_CONTRA_SAVEDVARIABLES",
        str(CHARACTER_SAVEDVARIABLES / "Contra.lua"),
    )
)
WOW_CONFIG = Path(
    os.environ.get("BOC_WOW_CONFIG", str(DEFAULT_WOW_ROOT / "WTF" / "Config.wtf"))
)

DEFAULT_INPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_expert_adapter_coverage_inputs_v2"
)
DEFAULT_LEDGER_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_expert_adapter_coverage_v2"
)
DEFAULT_PLAN = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_expert_adapter_coverage_gate_v2.plan.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_expert_adapter_coverage_gate_v2.json"
)

EXPECTED_V1_PLAN_SHA256 = (
    "e56cf279a6dfddfdae763673c2af7411104b18925a4d83575e47bac37abff48b"
)
EXPECTED_V1_ARTIFACT_SHA256 = (
    "c0768bc61c2ed3512e5b4a57c58180b12fffb7263f68af75ca4baea670801fa8"
)
EXPECTED_V1_RECEIPT_SHA256 = (
    "b33af65501dec5598741366303c6428c4bdeae3b498be60676fe053981b92903"
)
EXPECTED_ABSOLUTE_FULL_SHA256 = (
    "3d9696ebea2c6f4d8a17c5f241ae5edd5b87395b54f32fb91e60fb78baacc8e6"
)
EXPECTED_ABSOLUTE_LOCK_SHA256 = (
    "c740facfd5c276227c07f308473ed7207edf622a586779a1fdc21096dd017442"
)
EXPECTED_CAT2_NOOP_SHA256 = (
    "424e12c7da29060e406e958455e40b3c51b12a8686f55e99206bf2b01798d6e8"
)
EXPECTED_HISTORICAL_STATES_SHA256 = (
    "54d3ac7d10ad354230e4b7c27a34889d56e344b52092d80b09579581ae63caf0"
)
EXPECTED_BRIDGE_SHA256 = (
    "3f455eada0cf962f10294cc0a0db1f5e698d9211cffe5a6b715028be6ed53a9d"
)
EXPECTED_ADAPTER_V1_SHA256 = (
    "f4c932227ed9c9753d43d0471dcd5f6d1577f87a04e3321b1a32c8c10e3c81ed"
)
EXPECTED_CLOSED_LOOP_V1_SHA256 = (
    "443e48f7443a013a02fb521c849912eb189bf2bf3a7e2dc58e1a2053b8381458"
)
EXPECTED_CAT_TOC_SHA256 = (
    "aaf871e3d9ea6bc41af60da775b3388d443c7b61a751b6961a1edafbf0a3c3e5"
)
EXPECTED_CAT_FURY_SHA256 = (
    "1dc652bc34117d11703e2b9481b4da7386c3e3865b85413af5b2a4031bbab6fe"
)
EXPECTED_CONTRA_TOC_SHA256 = (
    "7c3adbc5b75193f36da1567d6af355a7fdb6fa48a588de7979a2b3bd67e9673d"
)
EXPECTED_CONTRA_ALL_SHA256 = (
    "3cd9ab254e521b7d4719f9648cae733ad54d5ed7421d1847716d54b6512cf2cd"
)

CAT_POLICY = "cat.fury.profile1"
CAT2_POLICY = "cat2.fury.brainofcat_shadow.saved_profile_source_v1"
CONTRA_POLICY = "contra.deployed.fury.raid_a"
EXECUTE = "warrior.execute"
WHIRLWIND = "warrior.whirlwind"

EXECUTED = "EXECUTED"
SOURCE_DECLARED_NOOP = "SOURCE_DECLARED_NOOP"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_COVERED = "NOT_COVERED"
ALLOWED_STATUSES = (EXECUTED, SOURCE_DECLARED_NOOP, NOT_APPLICABLE, NOT_COVERED)

SCOPE = "TWO_HAND_X_SIM_REQUEST_NON_BOSS_NON_DUMMY_EXCEPTIONAL_LANE_COVERAGE"
ABSOLUTE_SEED = 384
EXPECTED_EPISODES_PER_PASS = 384
EXPECTED_CAT_OMISSIONS = 1049
EXPECTED_CAT_AFFECTED_EPISODES = 47
EXPECTED_CAT2_NOOPS = 344
EXPECTED_CONTRA_NOT_COVERED = 22648
EXPECTED_CONTRA_AFFECTED_EPISODES = 96


class FuryAdapterCoverageGateError(RuntimeError):
    """A frozen input, replay, classification, or cardinality check failed."""


@dataclass(frozen=True)
class FileIdentity:
    role: str
    path: str
    size_bytes: int
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot(path: Path, role: str) -> FileIdentity:
    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise FuryAdapterCoverageGateError(
            f"could not read {role}: {resolved}: {exc}"
        ) from exc
    return FileIdentity(role, str(resolved), len(data), _sha256_bytes(data))


def _require(path: Path, role: str, expected_sha256: str) -> FileIdentity:
    identity = _snapshot(path, role)
    if identity.sha256 != expected_sha256.casefold():
        raise FuryAdapterCoverageGateError(
            f"{role} SHA-256 mismatch: expected {expected_sha256}, got {identity.sha256}"
        )
    return identity


def _verify(identity: Mapping[str, Any] | FileIdentity) -> None:
    expected = identity if isinstance(identity, FileIdentity) else FileIdentity(**dict(identity))
    if _snapshot(Path(expected.path), expected.role) != expected:
        raise FuryAdapterCoverageGateError(
            f"input changed after lock: {expected.role} {expected.path}"
        )


def _write_new_or_identical(path: Path, data: bytes, role: str) -> FileIdentity:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        if resolved.read_bytes() != data:
            raise FuryAdapterCoverageGateError(
                f"refusing to overwrite non-identical {role}: {resolved}"
            )
        current = _snapshot(resolved, role)
        return FileIdentity(role, current.path, current.size_bytes, current.sha256)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
        temporary.replace(resolved)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    current = _snapshot(resolved, role)
    return FileIdentity(role, current.path, current.size_bytes, current.sha256)


def _write_json_with_receipt(
    path: Path,
    value: Mapping[str, Any],
    *,
    receipt_kind: str,
    receipt_fields: Mapping[str, Any],
) -> tuple[FileIdentity, FileIdentity]:
    artifact = _write_new_or_identical(path, _pretty_bytes(value), "artifact")
    receipt_value = {
        "schema_version": 2,
        "kind": receipt_kind,
        "artifact": asdict(artifact),
        **copy.deepcopy(dict(receipt_fields)),
    }
    receipt = _write_new_or_identical(
        Path(str(Path(artifact.path)) + ".receipt.json"),
        _pretty_bytes(receipt_value),
        "artifact_receipt",
    )
    return artifact, receipt


def _write_content_blob(directory: Path, role: str, value: Mapping[str, Any]) -> FileIdentity:
    data = _pretty_bytes(value)
    digest = _sha256_bytes(data)
    return _write_new_or_identical(
        directory.expanduser().resolve() / f"{role}.{digest}.json",
        data,
        role,
    )


def _source_site_manifest() -> JSONMap:
    return {
        "schema_version": 2,
        "kind": "fury_expert_adapter_coverage_source_site_manifest_v2",
        "scope": SCOPE,
        "classification_taxonomy": {
            EXECUTED: "source-derived normalized lane reached a successful bridge command",
            SOURCE_DECLARED_NOOP: (
                "source emitted a call and a sufficient observed blocker establishes no "
                "simulator combat-state transition; retry timing is audited separately"
            ),
            NOT_APPLICABLE: (
                "all inputs to a source guard are observed and the guard is false, or an "
                "earlier source return excludes the site"
            ),
            NOT_COVERED: (
                "a source guard input, entry attestation, bridge operation, or unique "
                "rejection explanation is absent"
            ),
        },
        "sites": [
            {
                "source_branch_id": "cat_twohand_nonboss_execute",
                "authority": "Cat/WarriorFury.lua",
                "source_ref": "WarriorFury.lua:407-411",
                "guard": (
                    "not MPIsBossTarget() and HPP<20 and UseExecute==1 and "
                    "ExecuteWithoutMonster==1"
                ),
                "action": EXECUTE,
                "observed_noop_oracle": {
                    "all_required": [
                        "proposal.valid",
                        "gcd=warrior.execute",
                        "single GCD raw sink CastSpellByName(斩杀) at 407-411",
                        "target_is_boss=false",
                        "target_health_pct<20",
                        "execute_phase_20=true",
                        "gcd_ready=true",
                        "current_cast=null",
                        "rage<execute_cost",
                        "omission reason action_not_legal:ready_in_ms=0",
                    ],
                    "classification": SOURCE_DECLARED_NOOP,
                    "observable_boundary": (
                        "no simulator combat-state transition; the Lua call returns and "
                        "autoattack continues, but client failure text/events are unmodeled"
                    ),
                },
            },
            {
                "source_branch_id": "contra_twohand_raid_a_nonboss_health_bucket",
                "authority": "Contra/Contra_ALL.lua",
                "source_ref": "Contra_ALL.lua:31663-31732",
                "entry_refs": [
                    "Contra_ALL.lua:36383-36397",
                    "Contra_ALL.lua:36636-36646",
                ],
                "required_observed_fields": [
                    "policy_entry_attestation",
                    "UnitClassification(target)",
                    "UnitHealthMax(target)",
                    "true current target health percent",
                    "capture-derived Contra.ZSSDW",
                ],
                "max_health_buckets": [
                    {"lower_inclusive": 0, "upper_exclusive": 25000},
                    {"lower_inclusive": 25000, "upper_exclusive": 51000},
                    {"lower_inclusive": 51000, "upper_exclusive": None},
                ],
                "classification_when_any_required_field_missing": NOT_COVERED,
            },
            {
                "source_branch_id": "cat2_whirlwind_offset_retry",
                "authority": "Cards/Warrior/Whirlwind.lua",
                "source_ref": "Cards/Warrior/Whirlwind.lua:35-62",
                "action": WHIRLWIND,
                "bounded_ready_in_ms": {"minimum_inclusive": 1, "maximum_exclusive": 500},
                "classification": SOURCE_DECLARED_NOOP,
                "retry_timing_authority": "runner-fixed 100 ms proxy only",
            },
        ],
        "nonclaims": {
            "all_source_sites_enumerated": False,
            "raw_autoattack_sink_covered": False,
            "exact_lua_execution": False,
            "client_failure_event_replayed": False,
            "exact_retry_cadence": False,
        },
    }


def _bundle_digest(identities: Sequence[FileIdentity]) -> str:
    rows = [asdict(value) for value in identities]
    return _sha256_bytes(_canonical_bytes(rows))


def build_plan_contract(input_directory: Path = DEFAULT_INPUT_DIRECTORY) -> JSONMap:
    source_manifest = _write_content_blob(
        input_directory, "source_site_manifest", _source_site_manifest()
    )
    locked = [
        _require(DEFAULT_V1_PLAN, "canonical_canary_plan", EXPECTED_V1_PLAN_SHA256),
        _require(
            DEFAULT_V1_ARTIFACT,
            "canonical_canary_artifact",
            EXPECTED_V1_ARTIFACT_SHA256,
        ),
        _require(
            DEFAULT_V1_RECEIPT,
            "canonical_canary_receipt",
            EXPECTED_V1_RECEIPT_SHA256,
        ),
        _require(
            DEFAULT_ABSOLUTE_FULL,
            "absolute_seed_full",
            EXPECTED_ABSOLUTE_FULL_SHA256,
        ),
        _require(
            DEFAULT_ABSOLUTE_LOCK,
            "absolute_seed_input_lock",
            EXPECTED_ABSOLUTE_LOCK_SHA256,
        ),
        _require(
            DEFAULT_CAT2_NOOP,
            "cat2_absolute_noop_adjudication",
            EXPECTED_CAT2_NOOP_SHA256,
        ),
        _require(
            DEFAULT_HISTORICAL_STATES,
            "historical_state_catalog_dataset",
            EXPECTED_HISTORICAL_STATES_SHA256,
        ),
        _require(DEFAULT_BRIDGE, "seed_fixed_bridge", EXPECTED_BRIDGE_SHA256),
        _require(
            DEFAULT_ADAPTER_V1,
            "frozen_v1_expert_adapters",
            EXPECTED_ADAPTER_V1_SHA256,
        ),
        _require(
            DEFAULT_CLOSED_LOOP_V1,
            "frozen_v1_closed_loop",
            EXPECTED_CLOSED_LOOP_V1_SHA256,
        ),
        _snapshot(DEFAULT_GUIDED_STATE_V1, "frozen_v1_state_mapper"),
        _snapshot(DEFAULT_SELF, "coverage_gate_source"),
        _require(CAT_TOC, "cat_toc_authority", EXPECTED_CAT_TOC_SHA256),
        _require(CAT_FURY, "cat_fury_authority", EXPECTED_CAT_FURY_SHA256),
        _snapshot(CAT_SAVED, "cat_saved_variables_snapshot"),
        _require(CONTRA_TOC, "contra_toc_authority", EXPECTED_CONTRA_TOC_SHA256),
        _require(
            CONTRA_ALL,
            "contra_toc_loaded_authority",
            EXPECTED_CONTRA_ALL_SHA256,
        ),
        _snapshot(CONTRA_SAVED, "contra_saved_variables_snapshot"),
        _snapshot(WOW_CONFIG, "nampower_cvar_snapshot"),
        _snapshot(
            CONTRA_UNLOADED_NEIGHBOR,
            "contra_unloaded_neighbor_not_policy_authority",
        ),
        FileIdentity(
            "source_site_manifest",
            source_manifest.path,
            source_manifest.size_bytes,
            source_manifest.sha256,
        ),
    ]
    locked = sorted(locked, key=lambda row: (row.role, row.path.casefold()))
    return {
        "schema_version": 2,
        "kind": "fury_expert_adapter_coverage_gate_plan_contract_v2",
        "scope": SCOPE,
        "upstream": {
            "canonical_canary_plan_sha256": EXPECTED_V1_PLAN_SHA256,
            "canonical_canary_artifact_sha256": EXPECTED_V1_ARTIFACT_SHA256,
            "canonical_canary_status_required": "FAIL_CLOSED",
            "absolute_seed": ABSOLUTE_SEED,
            "matrix_episode_count_per_pass": EXPECTED_EPISODES_PER_PASS,
            "fresh_replay_pass_count": 2,
        },
        "authority": {
            "cat_toc_loads_warrior_fury": True,
            "contra_toc_loads": "Contra_ALL.lua",
            "contra_lua_disposition": "UNLOADED_NEIGHBOR_NOT_AUTHORITY",
            "contra_all_vs_contra_lua_equivalence_claimed": False,
            "contra_runtime_entry_attested": False,
        },
        "classification": {
            "allowed_statuses": list(ALLOWED_STATUSES),
            "ledger_scope": (
                "normalized bridge actions plus every V1 omitted lane and declared "
                "source-API no-op; not an enumeration of every absent raw source site"
            ),
            "expected_exception_counts_per_pass": {
                "cat_source_declared_noop": EXPECTED_CAT_OMISSIONS,
                "cat_affected_episodes": EXPECTED_CAT_AFFECTED_EPISODES,
                "cat2_source_declared_noop": EXPECTED_CAT2_NOOPS,
                "contra_not_covered": EXPECTED_CONTRA_NOT_COVERED,
                "contra_affected_episodes": EXPECTED_CONTRA_AFFECTED_EPISODES,
            },
            "source_site_manifest": asdict(source_manifest),
        },
        "dps_gate": {
            "minimum_unit": "policy x weapon_mode x target_class",
            "zero_not_covered_required": True,
            "source_noop_retry_timing_authority_required": True,
            "same_complete_episode_key_set_required": True,
            "posthoc_episode_deletion_forbidden": True,
            "expected_eligible_pairwise_dps": [],
        },
        "stopping_rules": [
            "any direct or inherited frozen input identity changes",
            "TOC loaded authority changes",
            "fresh replay differs from canonical V1 result hashes",
            "reference and repeat classification ledgers differ",
            "an exceptional lane receives zero or multiple classifications",
            "Cat low-rage Execute predicate is incomplete",
            "Contra entry, classification, max-health, true-health, or ZSSDW input is absent",
            "any policy proposed for DPS has NOT_COVERED or proxy retry timing",
        ],
        "inputs": {
            "files": [asdict(value) for value in locked],
            "file_count": len(locked),
            "file_bundle_sha256": _bundle_digest(locked),
            "canonical_canary_transitive_inputs_reverified_pre_and_post": True,
        },
        "claim_boundary": {
            "classification_only": True,
            "exact_lua_execution": False,
            "exact_target_classification": False,
            "exact_target_health": False,
            "exact_retry_cadence": False,
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
    }


def _contra_health_bucket(max_health: float | int | None) -> str | None:
    if max_health is None or isinstance(max_health, bool):
        return None
    value = float(max_health)
    if value < 0:
        return None
    if value < 25000:
        return "lt_25000"
    if value < 51000:
        return "25000_to_lt_51000"
    return "gte_51000"


def _target_health_evidence(request: Mapping[str, Any]) -> JSONMap:
    encounter = request.get("encounter")
    if not isinstance(encounter, Mapping):
        return {
            "use_health": None,
            "target_health_stat": None,
            "target_max_health_available": False,
            "true_health_percent_available": False,
        }
    targets = encounter.get("targets")
    target = (
        targets[0]
        if isinstance(targets, list) and targets and isinstance(targets[0], Mapping)
        else {}
    )
    stats = target.get("stats") if isinstance(target, Mapping) else None
    health = (
        stats[34]
        if isinstance(stats, list)
        and len(stats) > 34
        and isinstance(stats[34], (int, float))
        and not isinstance(stats[34], bool)
        else None
    )
    use_health = encounter.get("useHealth")
    available = use_health is True and health is not None and float(health) > 0
    return {
        "use_health": use_health,
        "target_health_stat": health,
        "target_max_health_available": available,
        "true_health_percent_available": available,
        "mob_type": target.get("mobType") if isinstance(target, Mapping) else None,
        "target_name": target.get("name") if isinstance(target, Mapping) else None,
        "target_level": target.get("level") if isinstance(target, Mapping) else None,
    }


def _raw_lane_sinks(proposal: Mapping[str, Any], lane: str) -> list[JSONMap]:
    rows = proposal.get("raw_sink_order")
    if not isinstance(rows, list):
        return []
    return [copy.deepcopy(dict(row)) for row in rows if isinstance(row, Mapping) and row.get("channel") == lane]


def _source_ref(sinks: Sequence[Mapping[str, Any]]) -> str | None:
    if not sinks:
        return None
    value = sinks[-1].get("source_ref")
    return str(value) if value is not None else None


def _event_base(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    event_ordinal: int,
    lane: str,
    action_key: Any,
    status: str,
    source_branch_id: str,
    source_sinks: Sequence[Mapping[str, Any]],
    bridge_command: Mapping[str, Any] | None,
) -> JSONMap:
    if status not in ALLOWED_STATUSES:
        raise FuryAdapterCoverageGateError(f"invalid classification status {status!r}")
    expert = step.get("expert_state")
    if not isinstance(expert, Mapping):
        expert = {}
    before = step.get("simulator_state_before")
    if not isinstance(before, Mapping):
        before = {}
    if bool(expert.get("target_is_training_dummy")):
        target_class = "SIM_REQUEST_TRAINING_DUMMY"
    elif bool(expert.get("target_is_boss")):
        target_class = "SIM_REQUEST_LEVEL_HEURISTIC_BOSS"
    else:
        target_class = "SIM_REQUEST_NON_BOSS_NON_DUMMY"
    row = {
        "episode_key": matrix["episode_key_sha256"],
        "decision_index": int(step["decision_index"]),
        "event_ordinal": event_ordinal,
        "time_ms": int(before.get("time_ms", 0)),
        "policy_id": matrix["policy_id"],
        "weapon_mode": expert.get("weapon_mode"),
        "target_class": target_class,
        "target_count_stratum": matrix["target_count_stratum"],
        "duration_stratum": matrix["duration_stratum"],
        "loadout": matrix["loadout"],
        "starting_armor": matrix["starting_armor"],
        "source_branch_id": source_branch_id,
        "source_ref": _source_ref(source_sinks),
        "lane": lane,
        "action_key": copy.deepcopy(action_key),
        "status": status,
        "source_sink_digest": (
            _sha256_bytes(_canonical_bytes(list(source_sinks))) if source_sinks else None
        ),
        "bridge_command_digest": (
            _sha256_bytes(_canonical_bytes(dict(bridge_command)))
            if bridge_command is not None
            else None
        ),
    }
    row["event_id"] = _sha256_bytes(_canonical_bytes(row))
    return row


def _classify_cat_omission(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    omission: Mapping[str, Any],
    *,
    event_ordinal: int,
) -> JSONMap:
    proposal = step.get("proposal")
    expert = step.get("expert_state")
    before = step.get("simulator_state_before")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    expert = expert if isinstance(expert, Mapping) else {}
    before = before if isinstance(before, Mapping) else {}
    sinks = _raw_lane_sinks(proposal, "gcd")
    rage = expert.get("rage")
    cost = expert.get("execute_cost")
    exact = bool(
        matrix.get("policy_id") == CAT_POLICY
        and omission.get("lane") == "gcd"
        and omission.get("requested") == EXECUTE
        and omission.get("reason") == "action_not_legal:ready_in_ms=0"
        and proposal.get("valid") is True
        and proposal.get("gcd", {}).get("action") == EXECUTE
        and len(sinks) == 1
        and sinks[0].get("operation") == "CastSpellByName"
        and sinks[0].get("source_ref") == "WarriorFury.lua:407-411"
        and expert.get("target_is_boss") is False
        and isinstance(expert.get("target_health_pct"), (int, float))
        and float(expert["target_health_pct"]) < 20.0
        and before.get("execute_phase_20") is True
        and expert.get("gcd_ready") is True
        and before.get("current_cast") is None
        and isinstance(rage, (int, float))
        and not isinstance(rage, bool)
        and isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and float(rage) < float(cost)
    )
    status = SOURCE_DECLARED_NOOP if exact else NOT_COVERED
    event = _event_base(
        matrix,
        step,
        event_ordinal=event_ordinal,
        lane="gcd",
        action_key=omission.get("requested"),
        status=status,
        source_branch_id="cat_twohand_nonboss_execute",
        source_sinks=sinks,
        bridge_command=None,
    )
    event.update(
        {
            "guard_evidence": {
                "target_is_boss": expert.get("target_is_boss"),
                "target_health_pct": expert.get("target_health_pct"),
                "target_health_known": before.get("target_health_known"),
                "execute_phase_20": before.get("execute_phase_20"),
                "gcd_ready": expert.get("gcd_ready"),
                "current_cast": copy.deepcopy(before.get("current_cast")),
                "rage": rage,
                "execute_cost": cost,
                "omission_reason": omission.get("reason"),
            },
            "noop_blocker": "RAGE_BELOW_EXECUTE_COST" if exact else None,
            "retry_timing_authority": "RUNNER_FIXED_100MS_PROXY" if exact else None,
            "reason": (
                "source emits CastSpellByName(斩杀) and returns without a rage guard; "
                "in this observed no-cast state the unique combat-state blocker is rage"
                if exact
                else "Cat illegal Execute did not satisfy the complete registered no-op oracle"
            ),
            "observable_boundary": (
                "state no-op in simulator; client failure text/event not modeled; source "
                "function return and continuing autoattack remain relevant"
                if exact
                else None
            ),
        }
    )
    event_without_id = dict(event)
    event_without_id.pop("event_id", None)
    event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
    return event


def _classify_cat2_noop(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    event_ordinal: int,
) -> JSONMap:
    proposal = step.get("proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    sinks = _raw_lane_sinks(proposal, "gcd")
    available = command.get("available")
    available = available if isinstance(available, Mapping) else {}
    ready = available.get("ready_in_ms")
    exact_window = bool(
        matrix.get("policy_id") == CAT2_POLICY
        and command.get("operation") == "source_api_noop_wait"
        and command.get("requested") == WHIRLWIND
        and available.get("legal") is False
        and isinstance(ready, int)
        and not isinstance(ready, bool)
        and 1 <= ready < 500
        and len(sinks) == 1
        and sinks[0].get("operation") == "Cat2.Cast"
        and sinks[0].get("source_ref") == "Cards/Warrior/Whirlwind.lua:35-62"
    )
    status = SOURCE_DECLARED_NOOP if exact_window else NOT_COVERED
    event = _event_base(
        matrix,
        step,
        event_ordinal=event_ordinal,
        lane="gcd",
        action_key=command.get("requested"),
        status=status,
        source_branch_id="cat2_whirlwind_offset_retry",
        source_sinks=sinks,
        bridge_command=command,
    )
    event.update(
        {
            "guard_evidence": {
                "available_legal": available.get("legal"),
                "ready_in_ms": ready,
                "minimum_inclusive": 1,
                "maximum_exclusive": 500,
            },
            "noop_blocker": (
                "WHIRLWIND_COOLDOWN_WINDOW_PROXY" if exact_window else None
            ),
            "retry_timing_authority": (
                "RUNNER_FIXED_100MS_PROXY" if exact_window else None
            ),
            "reason": (
                "Cat2 source declares the sub-500ms Whirlwind API retry window; the "
                "bridge cooldown is sufficient for a state-preserving failed cast"
                if exact_window
                else "Cat2 no-op command fell outside the registered source contract"
            ),
        }
    )
    event_without_id = dict(event)
    event_without_id.pop("event_id", None)
    event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
    return event


def _classify_contra_omission(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    omission: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    event_ordinal: int,
) -> JSONMap:
    proposal = step.get("proposal")
    expert = step.get("expert_state")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    expert = expert if isinstance(expert, Mapping) else {}
    health = _target_health_evidence(request)
    exact_gap = bool(
        matrix.get("policy_id") == CONTRA_POLICY
        and omission.get("lane") == "proposal"
        and omission.get("reason")
        == "current adapter lacks Contra non-boss max-health branch inputs"
        and proposal.get("valid") is False
        and expert.get("weapon_mode") == "TWO_HAND"
        and expert.get("target_is_boss") is False
        and expert.get("target_is_training_dummy") is False
        and health["target_max_health_available"] is False
        and health["true_health_percent_available"] is False
    )
    event = _event_base(
        matrix,
        step,
        event_ordinal=event_ordinal,
        lane="proposal",
        action_key=CONTRA_POLICY,
        status=NOT_COVERED,
        source_branch_id="contra_twohand_raid_a_nonboss_health_bucket",
        source_sinks=[],
        bridge_command=None,
    )
    event.update(
        {
            "guard_evidence": {
                **health,
                "adapter_target_is_boss": expert.get("target_is_boss"),
                "adapter_target_is_training_dummy": expert.get(
                    "target_is_training_dummy"
                ),
                "adapter_target_health_pct": expert.get("target_health_pct"),
                "adapter_contra_zssdw": expert.get("contra_zssdw"),
                "capture_derived_contra_zssdw": 0,
                "policy_entry_attested": False,
            },
            "missing_required_inputs": [
                "policy_entry_attestation",
                "UnitClassification(target)",
                "UnitHealthMax(target)",
                "true current target health percent",
                "capture-derived Contra.ZSSDW in normalized state",
            ],
            "noop_blocker": None,
            "retry_timing_authority": None,
            "reason": (
                "Contra_ALL.lua continues into one of three non-boss max-health branches; "
                "the canary cannot select or replay that branch"
                if exact_gap
                else "Contra invalid proposal did not match the registered non-boss gap"
            ),
            "registered_gap_matched": exact_gap,
        }
    )
    event_without_id = dict(event)
    event_without_id.pop("event_id", None)
    event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
    return event


def _classify_generic_omission(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    omission: Mapping[str, Any],
    *,
    event_ordinal: int,
) -> JSONMap:
    proposal = step.get("proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    lane = str(omission.get("lane"))
    sinks = _raw_lane_sinks(proposal, lane)
    event = _event_base(
        matrix,
        step,
        event_ordinal=event_ordinal,
        lane=lane,
        action_key=omission.get("requested"),
        status=NOT_COVERED,
        source_branch_id="unregistered_exception",
        source_sinks=sinks,
        bridge_command=None,
    )
    event.update(
        {
            "guard_evidence": {"omission_reason": omission.get("reason")},
            "noop_blocker": None,
            "retry_timing_authority": None,
            "reason": "exception did not match any preregistered source-site oracle",
        }
    )
    event_without_id = dict(event)
    event_without_id.pop("event_id", None)
    event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
    return event


def _classify_executed_command(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    event_ordinal: int,
) -> JSONMap:
    proposal = step.get("proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    lane = str(command.get("lane"))
    sinks = _raw_lane_sinks(proposal, lane)
    event = _event_base(
        matrix,
        step,
        event_ordinal=event_ordinal,
        lane=lane,
        action_key=command.get("requested"),
        status=EXECUTED,
        source_branch_id="normalized_policy_lane",
        source_sinks=sinks,
        bridge_command=command,
    )
    event.update(
        {
            "guard_evidence": {"bridge_status": command.get("status")},
            "noop_blocker": None,
            "retry_timing_authority": None,
            "reason": "normalized policy lane produced a successful bridge command",
        }
    )
    event_without_id = dict(event)
    event_without_id.pop("event_id", None)
    event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
    return event


def _classify_step(
    matrix: Mapping[str, Any],
    step: Mapping[str, Any],
    request: Mapping[str, Any],
) -> tuple[list[JSONMap], Counter[str]]:
    events: list[JSONMap] = []
    auxiliary: Counter[str] = Counter()
    ordinal = 0
    commands = step.get("commands")
    if not isinstance(commands, list):
        commands = []
    for command in commands:
        if not isinstance(command, Mapping):
            continue
        if command.get("lane") == "fallback_progress":
            auxiliary["fallback_progress"] += 1
            continue
        if command.get("operation") == "wait":
            auxiliary["policy_wait"] += 1
            continue
        ordinal += 1
        if command.get("operation") == "source_api_noop_wait":
            events.append(
                _classify_cat2_noop(
                    matrix, step, command, event_ordinal=ordinal
                )
            )
        elif command.get("status") == "executed":
            events.append(
                _classify_executed_command(
                    matrix, step, command, event_ordinal=ordinal
                )
            )
        else:
            proposal = step.get("proposal")
            proposal = proposal if isinstance(proposal, Mapping) else {}
            lane = str(command.get("lane"))
            sinks = _raw_lane_sinks(proposal, lane)
            event = _event_base(
                matrix,
                step,
                event_ordinal=ordinal,
                lane=lane,
                action_key=command.get("requested"),
                status=NOT_COVERED,
                source_branch_id="bridge_rejected_command",
                source_sinks=sinks,
                bridge_command=command,
            )
            event.update(
                {
                    "guard_evidence": {"bridge_status": command.get("status")},
                    "noop_blocker": None,
                    "retry_timing_authority": None,
                    "reason": "bridge command was not executed",
                }
            )
            event_without_id = dict(event)
            event_without_id.pop("event_id", None)
            event["event_id"] = _sha256_bytes(_canonical_bytes(event_without_id))
            events.append(event)

    omissions = step.get("omitted_lanes")
    if not isinstance(omissions, list):
        omissions = []
    for omission in omissions:
        if not isinstance(omission, Mapping):
            continue
        ordinal += 1
        if matrix.get("policy_id") == CAT_POLICY:
            event = _classify_cat_omission(
                matrix, step, omission, event_ordinal=ordinal
            )
        elif matrix.get("policy_id") == CONTRA_POLICY:
            event = _classify_contra_omission(
                matrix,
                step,
                omission,
                request,
                event_ordinal=ordinal,
            )
        else:
            event = _classify_generic_omission(
                matrix, step, omission, event_ordinal=ordinal
            )
        events.append(event)
    return events, auxiliary


def _load_upstream() -> tuple[
    JSONMap,
    JSONMap,
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    tuple[Any, ...],
    tuple[Mapping[str, Any], ...],
]:
    _, plan = _load_json(
        DEFAULT_V1_PLAN, "canonical_canary_plan", EXPECTED_V1_PLAN_SHA256
    )
    contract = plan.get("contract")
    if not isinstance(contract, Mapping):
        raise FuryAdapterCoverageGateError("canonical canary plan lacks contract")
    if plan.get("contract_sha256") != _sha256_bytes(_canonical_bytes(contract)):
        raise FuryAdapterCoverageGateError("canonical canary plan contract digest mismatch")
    _verify_plan_inputs(contract)
    _, artifact = _load_json(
        DEFAULT_V1_ARTIFACT,
        "canonical_canary_artifact",
        EXPECTED_V1_ARTIFACT_SHA256,
    )
    if artifact.get("status") != "FAIL_CLOSED":
        raise FuryAdapterCoverageGateError("canonical canary status is not FAIL_CLOSED")
    if artifact.get("plan", {}).get("sha256") != EXPECTED_V1_PLAN_SHA256:
        raise FuryAdapterCoverageGateError("canonical artifact does not bind the expected plan")
    results = artifact.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_EPISODES_PER_PASS:
        raise FuryAdapterCoverageGateError("canonical artifact result cardinality mismatch")

    loadouts = tuple(
        _load_blob(value, "capture_bound_loadout_bundle_v1")
        for value in contract["historical_loadout_selection"]["loadout_blobs"]
    )
    family_blob = _load_blob(
        contract["family_selection"]["blob"],
        "fury_loadout_armor_nuisance_family_selection_v1",
    )
    matrix_blob = _load_blob(
        contract["experiment"]["matrix_blob"],
        "fury_loadout_armor_nuisance_experiment_matrix_v1",
    )
    families = {str(row["scenario_id"]): row for row in family_blob["selected"]}
    loadout_by_capture = {str(row["capture_id"]): row for row in loadouts}
    matrix_rows = tuple(matrix_blob["rows"])
    if len(matrix_rows) != EXPECTED_EPISODES_PER_PASS:
        raise FuryAdapterCoverageGateError("matrix does not contain 384 episodes")

    _, frozen = _load_json(
        DEFAULT_FROZEN_GATE,
        "frozen_heldout_gate",
        "d70b58829eabe2846af70a3b1cf219f610ec871dfdeb050d62b2fd1f6d08b0ba",
    )
    _, cat2 = _load_json(
        DEFAULT_CAT2_PROFILE,
        "cat2_profile",
        "e5c17c343a46f14565a413920e635e455ad161654fb3c9aab6d9db8efff8875a",
    )
    candidate = frozen.get("candidate")
    parameters = candidate.get("parameters") if isinstance(candidate, Mapping) else None
    if not isinstance(parameters, Mapping):
        raise FuryAdapterCoverageGateError("frozen candidate parameters are absent")
    adapters = _policy_adapters(parameters, cat2)
    return (
        dict(contract),
        artifact,
        families,
        loadout_by_capture,
        adapters,
        matrix_rows,
    )


def _pass_digest(events: Sequence[Mapping[str, Any]], episodes: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_bytes(
        _canonical_bytes({"events": list(events), "episodes": list(episodes)})
    )


def _run_pass(pass_label: str) -> JSONMap:
    (
        _,
        v1_artifact,
        families,
        loadouts,
        adapters,
        matrix_rows,
    ) = _load_upstream()
    adapter_by_id = {adapter.expert_id: adapter for adapter in adapters}
    v1_by_key = {
        str(row["episode_key_sha256"]): row for row in v1_artifact["results"]
    }
    events: list[JSONMap] = []
    episodes: list[JSONMap] = []
    auxiliary: Counter[str] = Counter()
    with SimulatorBridge(DEFAULT_BRIDGE) as bridge:
        for index, matrix in enumerate(matrix_rows, start=1):
            family = families[str(matrix["scenario_id"])]
            loadout = loadouts[str(matrix["capture_id"])]
            request = request_with_loadout_and_armor(
                family["request"], loadout, int(matrix["starting_armor"])
            )
            episode_events: list[JSONMap] = []
            episode_auxiliary: Counter[str] = Counter()
            proc_summary, proc_sink = _proc_observer()

            def sink(step: Mapping[str, Any]) -> None:
                proc_sink(step)
                classified, counts = _classify_step(matrix, step, request)
                episode_events.extend(classified)
                episode_auxiliary.update(counts)

            rollout = run_fury_expert_closed_loop(
                bridge,
                request,
                adapter_by_id[str(matrix["policy_id"])],
                seed=ABSOLUTE_SEED,
                horizon_ms=int(matrix["horizon_ms"]),
                retain_steps=False,
                transition_sink=sink,
            )
            proc = _finalize_proc_observation(proc_summary)
            canonical_result_sha = _sha256_bytes(
                _canonical_bytes({"rollout": rollout, "proc_observation": proc})
            )
            upstream = v1_by_key[str(matrix["episode_key_sha256"])]
            if canonical_result_sha != upstream.get("canonical_result_sha256"):
                raise FuryAdapterCoverageGateError(
                    "fresh replay canonical result differs from frozen V1 artifact: "
                    f"{matrix['episode_key_sha256']}"
                )
            if not isclose(
                float(rollout["damage_delta"]),
                float(upstream["damage_delta"]),
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                raise FuryAdapterCoverageGateError("fresh replay damage changed")
            status_counts = Counter(str(row["status"]) for row in episode_events)
            if sum(status_counts.values()) != len(episode_events):
                raise FuryAdapterCoverageGateError("event classification cardinality mismatch")
            if any(status not in ALLOWED_STATUSES for status in status_counts):
                raise FuryAdapterCoverageGateError("event has an unregistered status")
            events.extend(episode_events)
            auxiliary.update(episode_auxiliary)
            episodes.append(
                {
                    "episode_key_sha256": matrix["episode_key_sha256"],
                    "policy_id": matrix["policy_id"],
                    "target_count_stratum": matrix["target_count_stratum"],
                    "duration_stratum": matrix["duration_stratum"],
                    "loadout": matrix["loadout"],
                    "starting_armor": matrix["starting_armor"],
                    "horizon_ms": matrix["horizon_ms"],
                    "canonical_v1_result_sha256": canonical_result_sha,
                    "configured_horizon_complete": bool(
                        rollout["configured_horizon_complete"]
                    ),
                    "v1_omitted_lane_count": int(rollout["omitted_lane_count"]),
                    "classified_event_counts": dict(sorted(status_counts.items())),
                    "fallback_progress_count": episode_auxiliary[
                        "fallback_progress"
                    ],
                    "policy_wait_count": episode_auxiliary["policy_wait"],
                }
            )
            if index % 48 == 0:
                print(
                    json.dumps(
                        {
                            "pass": pass_label,
                            "completed": index,
                            "expected": len(matrix_rows),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
    return {
        "events": events,
        "episodes": episodes,
        "auxiliary_counts": dict(sorted(auxiliary.items())),
        "pass_digest": _pass_digest(events, episodes),
    }


def _policy_summaries(
    events: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    policies = sorted({str(row["policy_id"]) for row in episodes})
    summaries: list[JSONMap] = []
    for policy in policies:
        policy_events = [row for row in events if row["policy_id"] == policy]
        policy_episodes = [row for row in episodes if row["policy_id"] == policy]
        statuses = Counter(str(row["status"]) for row in policy_events)
        affected_source_noop = {
            row["episode_key"]
            for row in policy_events
            if row["status"] == SOURCE_DECLARED_NOOP
        }
        affected_not_covered = {
            row["episode_key"]
            for row in policy_events
            if row["status"] == NOT_COVERED
        }
        retry_proxy_count = sum(
            row.get("retry_timing_authority") == "RUNNER_FIXED_100MS_PROXY"
            for row in policy_events
        )
        action_coverage_complete = statuses[NOT_COVERED] == 0
        retry_timing_exact = retry_proxy_count == 0
        assumption_conditioned_target_state = policy in {
            CAT_POLICY,
            CAT2_POLICY,
            CONTRA_POLICY,
        }
        simulator_policy_only = policy.startswith("boc.fury.v1.")
        coverage_complete_for_dps = bool(
            action_coverage_complete
            and retry_timing_exact
            and all(row["configured_horizon_complete"] for row in policy_episodes)
            and not assumption_conditioned_target_state
        )
        summaries.append(
            {
                "policy_id": policy,
                "episode_count": len(policy_episodes),
                "status_counts": {
                    status: statuses[status] for status in ALLOWED_STATUSES
                },
                "source_declared_noop_affected_episode_count": len(
                    affected_source_noop
                ),
                "not_covered_affected_episode_count": len(affected_not_covered),
                "v1_omitted_lane_count": sum(
                    int(row["v1_omitted_lane_count"]) for row in policy_episodes
                ),
                "retry_timing_proxy_event_count": retry_proxy_count,
                "all_horizons_complete": all(
                    row["configured_horizon_complete"] for row in policy_episodes
                ),
                "action_coverage_complete_for_registered_exception_scope": (
                    action_coverage_complete
                ),
                "retry_timing_exact": retry_timing_exact,
                "target_state_assumption_conditioned": (
                    assumption_conditioned_target_state
                ),
                "coverage_complete_for_dps": coverage_complete_for_dps,
                "simulator_policy_only": simulator_policy_only,
                "upstream_dps_withheld": True,
            }
        )
    return summaries


def _exception_count(events: Sequence[Mapping[str, Any]], policy: str, status: str) -> int:
    return sum(row["policy_id"] == policy and row["status"] == status for row in events)


def _affected_count(events: Sequence[Mapping[str, Any]], policy: str, status: str) -> int:
    return len(
        {
            row["episode_key"]
            for row in events
            if row["policy_id"] == policy and row["status"] == status
        }
    )


def _verify_expected_classification(events: Sequence[Mapping[str, Any]]) -> None:
    observed = {
        "cat_source_declared_noop": _exception_count(
            events, CAT_POLICY, SOURCE_DECLARED_NOOP
        ),
        "cat_affected_episodes": _affected_count(
            events, CAT_POLICY, SOURCE_DECLARED_NOOP
        ),
        "cat2_source_declared_noop": _exception_count(
            events, CAT2_POLICY, SOURCE_DECLARED_NOOP
        ),
        "contra_not_covered": _exception_count(
            events, CONTRA_POLICY, NOT_COVERED
        ),
        "contra_affected_episodes": _affected_count(
            events, CONTRA_POLICY, NOT_COVERED
        ),
    }
    expected = {
        "cat_source_declared_noop": EXPECTED_CAT_OMISSIONS,
        "cat_affected_episodes": EXPECTED_CAT_AFFECTED_EPISODES,
        "cat2_source_declared_noop": EXPECTED_CAT2_NOOPS,
        "contra_not_covered": EXPECTED_CONTRA_NOT_COVERED,
        "contra_affected_episodes": EXPECTED_CONTRA_AFFECTED_EPISODES,
    }
    if observed != expected:
        raise FuryAdapterCoverageGateError(
            f"classification counts differ from preregistration: {observed} != {expected}"
        )
    unexpected_not_covered = [
        row
        for row in events
        if row["status"] == NOT_COVERED and row["policy_id"] != CONTRA_POLICY
    ]
    if unexpected_not_covered:
        raise FuryAdapterCoverageGateError(
            "a non-Contra exceptional lane remains NOT_COVERED"
        )
    contra_unmatched = [
        row
        for row in events
        if row["policy_id"] == CONTRA_POLICY
        and row["status"] == NOT_COVERED
        and row.get("registered_gap_matched") is not True
    ]
    if contra_unmatched:
        raise FuryAdapterCoverageGateError(
            "a Contra omission did not match the preregistered missing-state gap"
        )


def _ledger_bytes(events: Sequence[Mapping[str, Any]]) -> tuple[bytes, str]:
    raw = b"".join(_canonical_bytes(row) + b"\n" for row in events)
    return gzip.compress(raw, compresslevel=9, mtime=0), _sha256_bytes(raw)


def _write_ledger(
    directory: Path, events: Sequence[Mapping[str, Any]]
) -> tuple[FileIdentity, str]:
    compressed, payload_sha = _ledger_bytes(events)
    path = (
        directory.expanduser().resolve()
        / f"coverage_events.{payload_sha}.jsonl.gz"
    )
    return (
        _write_new_or_identical(path, compressed, "coverage_event_ledger"),
        payload_sha,
    )


def _verify_direct_inputs(contract: Mapping[str, Any]) -> None:
    rows = contract.get("inputs", {}).get("files")
    if not isinstance(rows, list):
        raise FuryAdapterCoverageGateError("plan contract lacks input identities")
    for row in rows:
        if not isinstance(row, Mapping):
            raise FuryAdapterCoverageGateError("invalid direct input identity")
        _verify(row)


def run_from_plan(
    plan_path: Path,
    expected_plan_sha256: str,
    output: Path,
    *,
    input_directory: Path = DEFAULT_INPUT_DIRECTORY,
    ledger_directory: Path = DEFAULT_LEDGER_DIRECTORY,
) -> JSONMap:
    plan_identity = _require(
        plan_path, "coverage_gate_plan", expected_plan_sha256.casefold()
    )
    try:
        plan = json.loads(Path(plan_identity.path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FuryAdapterCoverageGateError(f"invalid coverage gate plan: {exc}") from exc
    contract = plan.get("contract") if isinstance(plan, Mapping) else None
    if not isinstance(contract, Mapping):
        raise FuryAdapterCoverageGateError("coverage gate plan lacks contract")
    if plan.get("contract_sha256") != _sha256_bytes(_canonical_bytes(contract)):
        raise FuryAdapterCoverageGateError("coverage gate plan contract digest mismatch")
    _verify_direct_inputs(contract)
    rebuilt = build_plan_contract(input_directory)
    if rebuilt != contract:
        raise FuryAdapterCoverageGateError(
            "current deterministic coverage contract differs from locked plan"
        )

    reference = _run_pass("reference")
    _verify_expected_classification(reference["events"])
    repeat = _run_pass("fresh_repeat")
    _verify_expected_classification(repeat["events"])
    if reference["pass_digest"] != repeat["pass_digest"]:
        raise FuryAdapterCoverageGateError(
            "reference and fresh-repeat classification ledgers differ"
        )
    if reference["events"] != repeat["events"]:
        raise FuryAdapterCoverageGateError(
            "classification event equality failed despite digest comparison"
        )
    if reference["episodes"] != repeat["episodes"]:
        raise FuryAdapterCoverageGateError("episode summary repeat equality failed")

    policy_summaries = _policy_summaries(
        reference["events"], reference["episodes"]
    )
    eligible_pairwise_dps: list[JSONMap] = []
    ledger_identity, payload_sha = _write_ledger(
        ledger_directory, reference["events"]
    )
    _verify_direct_inputs(contract)
    v1_contract, _, _, _, _, _ = _load_upstream()
    _verify_plan_inputs(v1_contract)

    status_counts = Counter(str(row["status"]) for row in reference["events"])
    artifact = {
        "schema_version": 2,
        "kind": "fury_expert_adapter_coverage_gate_v2",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "CLASSIFICATION_COMPLETE_DPS_FAIL_CLOSED",
        "scope": SCOPE,
        "plan": asdict(plan_identity),
        "contract_sha256": plan["contract_sha256"],
        "input_preverification_passed": True,
        "input_post_reverification_passed": True,
        "execution": {
            "absolute_seed": ABSOLUTE_SEED,
            "reference_episode_count": len(reference["episodes"]),
            "fresh_repeat_episode_count": len(repeat["episodes"]),
            "total_episode_count": len(reference["episodes"])
            + len(repeat["episodes"]),
            "fresh_bridge_process_per_pass": True,
            "canonical_v1_result_hash_match_count_per_pass": len(
                reference["episodes"]
            ),
            "classification_repeat_exact": True,
            "reference_pass_digest": reference["pass_digest"],
            "fresh_repeat_pass_digest": repeat["pass_digest"],
            "classification_event_count_per_pass": len(reference["events"]),
            "classification_status_counts_per_pass": {
                status: status_counts[status] for status in ALLOWED_STATUSES
            },
            "auxiliary_non_source_command_counts_per_pass": reference[
                "auxiliary_counts"
            ],
        },
        "coverage_event_ledger": {
            **asdict(ledger_identity),
            "encoding": "deterministic gzip over canonical UTF-8 JSONL",
            "uncompressed_payload_sha256": payload_sha,
            "row_count": len(reference["events"]),
            "scope": (
                "normalized bridge actions and every V1 exception; excludes fallback "
                "progress and ordinary policy waits from source-action counts"
            ),
        },
        "policy_summaries": policy_summaries,
        "adjudication": {
            "cat": {
                "classification": SOURCE_DECLARED_NOOP,
                "count": EXPECTED_CAT_OMISSIONS,
                "affected_episode_count": EXPECTED_CAT_AFFECTED_EPISODES,
                "action": EXECUTE,
                "source_ref": "WarriorFury.lua:407-411",
                "observed_rage_range": [0.0, 9.980018673990472],
                "execute_cost": 10.0,
                "current_cast_null_for_all_classified_rows": True,
                "target_health_is_execute_phase_proxy": True,
                "retry_timing_is_runner_proxy": True,
                "dps_withheld": True,
            },
            "cat2": {
                "classification": SOURCE_DECLARED_NOOP,
                "count": EXPECTED_CAT2_NOOPS,
                "action": WHIRLWIND,
                "ready_in_ms_contract": "1 <= ready_in_ms < 500",
                "retry_timing_is_runner_proxy": True,
                "dps_withheld": True,
            },
            "contra": {
                "classification": NOT_COVERED,
                "count": EXPECTED_CONTRA_NOT_COVERED,
                "affected_episode_count": EXPECTED_CONTRA_AFFECTED_EPISODES,
                "source_ref": "Contra_ALL.lua:31663-31732",
                "not_a_source_noop": True,
                "current_canary_fallback_dps_invalid_for_ranking": True,
                "baseline_disposition": (
                    "RETAIN_AS_REQUIRED_STRONG_BASELINE_AFTER_FEATURE_RECONSTRUCTION"
                ),
                "not_covered_scope": (
                    "current canary adapter inputs only; not a rejection of Contra"
                ),
                "missing_required_inputs": [
                    "policy entry attestation",
                    "UnitClassification target",
                    "UnitHealthMax target",
                    "true current target health percent",
                    "capture-derived Contra.ZSSDW in normalized state",
                ],
                "capture_derived_zssdw_for_both_loadouts": 0,
                "current_mapper_zssdw": 3,
                "dps_withheld": True,
            },
        },
        "dps_gate": {
            "status": "FAIL_CLOSED",
            "eligible_pairwise_dps": eligible_pairwise_dps,
            "current_canary_policy_dps_values_withheld": True,
            "reason": (
                "Contra is NOT_COVERED; Cat and Cat2 use runner-fixed no-op retry "
                "timing; all expert target states remain assumption-conditioned"
            ),
            "posthoc_complete_subset_ranking_permitted": False,
            "contra_baseline_retained": True,
        },
        "next_requirements": [
            "reconstruct Contra UnitClassification, UnitHealthMax, and true health from offline trajectories where identifiable",
            "capture or independently attest Contra policy entry when offline trajectories cannot prove it",
            "derive Contra.ZSSDW from each exact loadout rather than defaulting to 3",
            "export an authoritative source retry cadence or run a preregistered cadence sensitivity envelope",
            "add exact target-class and target-health state before any Cat/Contra DPS baseline comparison",
        ],
        "gates": {
            "classification_integrity_passed": True,
            "adapter_coverage_complete_for_all_experts": False,
            "dps_ranking_eligible": False,
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
        "claim_boundary": copy.deepcopy(contract["claim_boundary"]),
    }
    artifact_identity, receipt_identity = _write_json_with_receipt(
        output,
        artifact,
        receipt_kind="fury_expert_adapter_coverage_gate_receipt_v2",
        receipt_fields={
            "artifact_status": artifact["status"],
            "scope": SCOPE,
            "plan_sha256": plan_identity.sha256,
            "contract_sha256": plan["contract_sha256"],
            "classification_repeat_exact": True,
            "classification_event_count_per_pass": len(reference["events"]),
            "coverage_event_ledger_sha256": ledger_identity.sha256,
            "eligible_pairwise_dps": [],
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
    )
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "artifact": asdict(artifact_identity),
                "receipt": asdict(receipt_identity),
                "ledger": asdict(ledger_identity),
                "classification_events_per_pass": len(reference["events"]),
                "repeat_exact": True,
                "eligible_pairwise_dps": [],
                "all_downstream_gates": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--input-directory", type=Path, default=DEFAULT_INPUT_DIRECTORY)
    parser.add_argument("--ledger-directory", type=Path, default=DEFAULT_LEDGER_DIRECTORY)
    parser.add_argument("--expected-plan-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.plan_only:
        contract = build_plan_contract(args.input_directory)
        plan = {
            "schema_version": 2,
            "kind": "fury_expert_adapter_coverage_gate_plan_v2",
            "generated_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "execution_status": "NOT_EXECUTED",
            "contract_sha256": _sha256_bytes(_canonical_bytes(contract)),
            "contract": contract,
            "all_downstream_gates": False,
        }
        artifact, receipt = _write_json_with_receipt(
            args.plan,
            plan,
            receipt_kind="fury_expert_adapter_coverage_gate_plan_receipt_v2",
            receipt_fields={
                "execution_status": "NOT_EXECUTED",
                "contract_sha256": plan["contract_sha256"],
                "all_downstream_gates": False,
            },
        )
        print(
            json.dumps(
                {
                    "plan": asdict(artifact),
                    "receipt": asdict(receipt),
                    "contract_sha256": plan["contract_sha256"],
                    "execution_status": "NOT_EXECUTED",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.expected_plan_sha256:
        raise FuryAdapterCoverageGateError(
            "--run requires --expected-plan-sha256"
        )
    run_from_plan(
        args.plan,
        args.expected_plan_sha256,
        args.output,
        input_directory=args.input_directory,
        ledger_directory=args.ledger_directory,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
