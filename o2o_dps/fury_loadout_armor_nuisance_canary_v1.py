"""Run a capture-bound loadout-bundle x starting-armor simulator canary.

This is deliberately a small nuisance diagnostic.  It consumes the already
locked absolute-seed 392-family held-out corpus, selects one family from every
target-count x duration cell without looking at outcomes, and evaluates two
complete same-capture historical equipment bundles at four starting armor
values.  Cat, Cat2, Contra, and the frozen candidate remain source-derived
simulator policies.  Nothing emitted here is training, voting, deployment, or
real-game superiority evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
from math import isfinite
from pathlib import Path
from statistics import fmean
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .cat2_saved_profile_adapter_v1 import Cat2SavedProfileSourceAdapterV1
from .fury_current_cat2_heldout_replay_v1 import (
    SameByteDocumentStore,
    build_request_contract,
    reconstruct_frozen_corpus,
)
from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_expert_closed_loop import run_fury_expert_closed_loop
from .fury_heldout_corpus_gate_v1 import (
    SelectedCorpus,
    SelectedFamily,
    load_manifest_snapshot,
)
from .fury_policy_optimization_v1 import FuryPolicyParameters, FuryTunedPolicyAdapter
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDON_ROOT = PROJECT_ROOT.parent

DEFAULT_PREFIX_GATE = (
    PROJECT_ROOT / "offline_data/sim_validation/fury_prefix_reconstruction_gate_v1.json"
)
DEFAULT_ABSOLUTE_CORPUS_LOCK = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_current_cat2_supplemental_absolute_seed_v1.input-lock.json"
)
DEFAULT_FROZEN_GATE = (
    PROJECT_ROOT / "offline_data/sim_validation/fury_policy_heldout_corpus_gate_v1.json"
)
DEFAULT_CAT2_PROFILE = PROJECT_ROOT / "offline_data/reports/cat2_saved_profile_v1.json"
DEFAULT_CATALOG_REPORT = (
    PROJECT_ROOT / "offline_data/reports/fury_historical_state_catalog_v1.json"
)
DEFAULT_CATALOG_BUILDER = PROJECT_ROOT / "o2o_dps/fury_historical_state_catalog_v1.py"
DEFAULT_CATALOG_MANIFEST = (
    PROJECT_ROOT
    / "offline_data/derived/fury_historical_state_catalog/v1/manifest.json"
)
DEFAULT_CATALOG_DATASET = (
    PROJECT_ROOT
    / "offline_data/derived/fury_historical_state_catalog/v1/states.jsonl"
)
DEFAULT_BRIDGE = PROJECT_ROOT / "bin/o2obridge.seedfix-v1.exe"
DEFAULT_ITEM_EFFECTS_SOURCE = ADDON_ROOT / "wowsims-turtle/sim/common/item_effects.go"
DEFAULT_ENCHANT_EFFECTS_SOURCE = (
    ADDON_ROOT / "wowsims-turtle/sim/common/enchant_effects.go"
)
DEFAULT_BLOB_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_loadout_armor_nuisance_inputs_v1"
)
DEFAULT_PLAN = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_loadout_armor_nuisance_canary_v1.plan.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data/sim_validation/fury_loadout_armor_nuisance_canary_v1.json"
)

EXPECTED_PREFIX_GATE_SHA256 = (
    "71dbbf56791dbbd9ff3d33f4f2acfd13ac964b2a04baa66e8a919660efd12d28"
)
EXPECTED_ABSOLUTE_CORPUS_LOCK_SHA256 = (
    "c740facfd5c276227c07f308473ed7207edf622a586779a1fdc21096dd017442"
)
EXPECTED_FROZEN_GATE_SHA256 = (
    "d70b58829eabe2846af70a3b1cf219f610ec871dfdeb050d62b2fd1f6d08b0ba"
)
EXPECTED_CAT2_PROFILE_SHA256 = (
    "e5c17c343a46f14565a413920e635e455ad161654fb3c9aab6d9db8efff8875a"
)
EXPECTED_CATALOG_REPORT_SHA256 = (
    "913033890562fe817533a850ca438d6806e00b86e72e9905d4c8b4b17a62ac53"
)
EXPECTED_CATALOG_MANIFEST_SHA256 = (
    "d24ccbe19d13b7edb3e61adc722e145222472c632ad19b3144d732d0d1699061"
)
EXPECTED_CATALOG_DATASET_SHA256 = (
    "54d3ac7d10ad354230e4b7c27a34889d56e344b52092d80b09579581ae63caf0"
)
EXPECTED_CATALOG_BUILDER_SHA256 = (
    "2f54538e78270ea247e985f5367a84ff9645dc079fcb5e03a5099de0480436dd"
)
EXPECTED_BRIDGE_SHA256 = (
    "3f455eada0cf962f10294cc0a0db1f5e698d9211cffe5a6b715028be6ed53a9d"
)
EXPECTED_ITEM_EFFECTS_SOURCE_SHA256 = (
    "e51898f812d2233c13a10910d61f6df3074a9e6c1798c086fc8cdf4fdcbcfbf4"
)
EXPECTED_ENCHANT_EFFECTS_SOURCE_SHA256 = (
    "d62e610288e477428dc39d9a3ddaba32a11fdbeaf8f6a7b0a99317776fe2ccf0"
)

SCOPE = "STATIC_CAPTURE_BOUND_LOADOUT_BUNDLE_X_STARTING_ARMOR_SIMULATOR_NUISANCE"
ABSOLUTE_SEED = 384
STARTING_ARMORS = (4211, 3761, 2861, 1961)
TARGET_STRATA = ("1", "2", "3-4", "5+")
DURATION_STRATA = ("short_le_10s", "medium_10_30s", "long_gt_30s")
WEAPON_VARIANTS = ((21679, 0), (17076, 1900))
REQUIRED_NONZERO_TALENT_ENTRIES = 16
NONWEAPON_SLOTS = (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
SIM_SLOT_TO_GAME_SLOT = (1, 2, 3, 15, 5, 9, 10, 6, 7, 8, 11, 12, 13, 14, 16, 17, 18)
ARMOR_STAT_INDEX = 26


class FuryLoadoutArmorCanaryError(RuntimeError):
    """A precondition, identity, structure, or repeatability contract failed."""


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


def _catalog_canonical_bytes(value: Any) -> bytes:
    """Match the historical catalog builder's newline-terminated identity bytes."""

    return _canonical_bytes(value) + b"\n"


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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(path: Path, role: str) -> FileIdentity:
    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise FuryLoadoutArmorCanaryError(
            f"could not read {role}: {resolved}: {exc}"
        ) from exc
    return FileIdentity(role, str(resolved), len(data), _sha256_bytes(data))


def _require_identity(path: Path, role: str, expected_sha256: str) -> FileIdentity:
    identity = _snapshot(path, role)
    if identity.sha256 != expected_sha256.casefold():
        raise FuryLoadoutArmorCanaryError(
            f"{role} SHA-256 mismatch: expected {expected_sha256}, got {identity.sha256}"
        )
    return identity


def _load_json(path: Path, role: str, expected_sha256: str | None = None) -> tuple[FileIdentity, JSONMap]:
    identity = (
        _require_identity(path, role, expected_sha256)
        if expected_sha256 is not None
        else _snapshot(path, role)
    )
    try:
        value = json.loads(Path(identity.path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FuryLoadoutArmorCanaryError(f"invalid {role} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FuryLoadoutArmorCanaryError(f"{role} JSON must be an object")
    return identity, value


def _write_new_or_identical(path: Path, data: bytes) -> FileIdentity:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        current = resolved.read_bytes()
        if current != data:
            raise FuryLoadoutArmorCanaryError(
                f"refusing to overwrite non-identical artifact: {resolved}"
            )
        return _snapshot(resolved, "preexisting_identical_output")
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
    return _snapshot(resolved, "new_output")


def _write_content_blob(directory: Path, role: str, value: Mapping[str, Any]) -> FileIdentity:
    data = _pretty_bytes(value)
    digest = _sha256_bytes(data)
    path = directory.expanduser().resolve() / f"{role}.{digest}.json"
    identity = _write_new_or_identical(path, data)
    return FileIdentity(role, identity.path, identity.size_bytes, identity.sha256)


def _write_with_receipt(path: Path, value: Mapping[str, Any], receipt_fields: Mapping[str, Any]) -> tuple[FileIdentity, FileIdentity]:
    identity = _write_new_or_identical(path, _pretty_bytes(value))
    artifact_identity = FileIdentity(
        "artifact", identity.path, identity.size_bytes, identity.sha256
    )
    receipt = {
        "schema_version": 1,
        "kind": "fury_loadout_armor_nuisance_canary_receipt_v1",
        "artifact": asdict(artifact_identity),
        **copy.deepcopy(dict(receipt_fields)),
    }
    receipt_identity_raw = _write_new_or_identical(
        Path(str(Path(identity.path)) + ".receipt.json"), _pretty_bytes(receipt)
    )
    receipt_identity = FileIdentity(
        "artifact_receipt",
        receipt_identity_raw.path,
        receipt_identity_raw.size_bytes,
        receipt_identity_raw.sha256,
    )
    return artifact_identity, receipt_identity


def _verify_identity(identity: Mapping[str, Any] | FileIdentity) -> None:
    expected = identity if isinstance(identity, FileIdentity) else FileIdentity(**dict(identity))
    observed = _snapshot(Path(expected.path), expected.role)
    if observed != expected:
        raise FuryLoadoutArmorCanaryError(
            f"input changed after lock: {expected.role} {expected.path}"
        )


def _verify_upstream_lock_files(lock: Mapping[str, Any]) -> tuple[FileIdentity, ...]:
    if lock.get("schema_version") != 1 or lock.get("kind") != "fury_current_cat2_heldout_input_lock_v1":
        raise FuryLoadoutArmorCanaryError("absolute-seed corpus lock kind is invalid")
    raw_files = lock.get("files")
    if not isinstance(raw_files, list):
        raise FuryLoadoutArmorCanaryError("absolute-seed corpus lock lacks files")
    identities: list[FileIdentity] = []
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise FuryLoadoutArmorCanaryError("absolute-seed lock file row is invalid")
        identity = FileIdentity(**dict(raw))
        _verify_identity(identity)
        identities.append(identity)
    bridges = [row for row in identities if row.role == "simulator_bridge_binary"]
    if len(bridges) != 1 or bridges[0].sha256 != EXPECTED_BRIDGE_SHA256:
        raise FuryLoadoutArmorCanaryError(
            "absolute-seed corpus lock does not bind the required seed-fixed bridge"
        )
    return tuple(identities)


def _load_locked_corpus(
    frozen_gate_path: Path,
    absolute_lock_path: Path,
) -> tuple[SelectedCorpus, JSONMap, tuple[FileIdentity, ...]]:
    frozen_identity, frozen = _load_json(
        frozen_gate_path, "frozen_heldout_gate", EXPECTED_FROZEN_GATE_SHA256
    )
    lock_identity, lock = _load_json(
        absolute_lock_path,
        "absolute_seed_392_family_input_lock",
        EXPECTED_ABSOLUTE_CORPUS_LOCK_SHA256,
    )
    locked_files = _verify_upstream_lock_files(lock)
    manifest_meta = frozen.get("manifest_snapshot")
    if not isinstance(manifest_meta, Mapping) or not isinstance(manifest_meta.get("path"), str):
        raise FuryLoadoutArmorCanaryError("frozen gate lacks manifest path")
    manifest_path = Path(manifest_meta["path"]).expanduser().resolve()
    store = SameByteDocumentStore()
    store.capture(manifest_path, "chronicle_manifest")
    snapshot = load_manifest_snapshot(
        manifest_path,
        require_complete=bool(manifest_meta.get("require_complete")),
        document_loader=store.load,
    )
    for value in snapshot.completed_catalogs:
        store.capture(value.path, "completed_scenario_catalog")
    corpus, frozen_contract = reconstruct_frozen_corpus(
        frozen, manifest_snapshot=snapshot, document_loader=store.load
    )
    store.release_documents()
    if len(corpus.families) != 392:
        raise FuryLoadoutArmorCanaryError(
            f"absolute-seed held-out corpus must contain 392 families, got {len(corpus.families)}"
        )
    request_contract = build_request_contract(corpus)
    if lock.get("request_contract") != request_contract:
        raise FuryLoadoutArmorCanaryError(
            "reconstructed 392-family requests do not match the absolute-seed lock"
        )
    training = set(frozen_contract["instance_split"]["candidate_training_instances"])
    selected_instances = {row.instance_id for row in corpus.families}
    if training.intersection(selected_instances):
        raise FuryLoadoutArmorCanaryError("candidate-training instance leaked into canary")
    return corpus, frozen_contract, (frozen_identity, lock_identity, *locked_files)


def _capture_identity_is_valid(row: Mapping[str, Any]) -> bool:
    binding = row.get("binding")
    state = row.get("state")
    if not isinstance(binding, Mapping) or not isinstance(state, Mapping):
        return False
    task = binding.get("task")
    if not isinstance(task, Mapping):
        return False
    identity = {
        "source_jsonl_sha256": binding.get("source_jsonl_sha256"),
        "source_line": binding.get("source_line"),
        "sequence": binding.get("sequence"),
        "task_run_id": task.get("task_run_id"),
        "captured_at": binding.get("captured_at"),
        "projected_state": state,
    }
    return row.get("capture_id") == _sha256_bytes(_catalog_canonical_bytes(identity))


def _equipment_signature(equipment: Sequence[Mapping[str, Any]], slots: Iterable[int]) -> tuple[tuple[int, int, int], ...]:
    wanted = set(slots)
    result: list[tuple[int, int, int]] = []
    for item in equipment:
        slot = item.get("slot")
        if slot not in wanted:
            continue
        item_id = item.get("item_id")
        enchant_id = item.get("enchant_id")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (slot, item_id, enchant_id)):
            raise FuryLoadoutArmorCanaryError("capture equipment identity is not integral")
        result.append((slot, item_id, enchant_id))
    result.sort()
    if {row[0] for row in result} != wanted or len(result) != len(wanted):
        raise FuryLoadoutArmorCanaryError("capture lacks the exact required equipment slots")
    return tuple(result)


def _talent_signature(talents: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_bytes(_canonical_bytes(list(talents)))


def select_loadout_captures(rows: Iterable[Mapping[str, Any]]) -> tuple[JSONMap, JSONMap, JSONMap]:
    """Select a deterministic, controlled pair without inspecting sim outcomes."""

    grouped: dict[tuple[str, tuple[tuple[int, int, int], ...], str], dict[tuple[int, int], list[JSONMap]]] = defaultdict(lambda: defaultdict(list))
    eligible_rows = 0
    for raw in rows:
        row = copy.deepcopy(dict(raw))
        if (
            row.get("schema") != "fury_historical_state_capture/v1"
            or row.get("evidence_scope") != "CALIBRATION_CAPTURE_BOUND"
            or row.get("use_class") != "HISTORICAL_PRIOR_ONLY"
            or not _capture_identity_is_valid(row)
        ):
            continue
        state = row.get("state")
        assert isinstance(state, Mapping)
        actor = state.get("actor")
        equipment = state.get("equipment")
        talents = state.get("talents")
        if not isinstance(actor, Mapping) or actor.get("class_file") != "WARRIOR":
            continue
        player_guid = actor.get("player_guid")
        if not isinstance(player_guid, str) or not player_guid:
            continue
        if not isinstance(equipment, list) or not isinstance(talents, list):
            continue
        if len(talents) != REQUIRED_NONZERO_TALENT_ENTRIES:
            continue
        if not all(isinstance(value, Mapping) for value in equipment + talents):
            continue
        try:
            nonweapon = _equipment_signature(equipment, NONWEAPON_SLOTS)
            mainhand = _equipment_signature(equipment, (16,))[0]
        except FuryLoadoutArmorCanaryError:
            continue
        variant = (mainhand[1], mainhand[2])
        if variant not in WEAPON_VARIANTS:
            continue
        other_slots = {item.get("slot") for item in equipment}.difference(
            set(NONWEAPON_SLOTS) | {16}
        )
        if other_slots:
            continue
        key = (player_guid, nonweapon, _talent_signature(talents))
        grouped[key][variant].append(row)
        eligible_rows += 1

    complete_groups = [
        (key, values)
        for key, values in grouped.items()
        if all(variant in values for variant in WEAPON_VARIANTS)
    ]
    if not complete_groups:
        raise FuryLoadoutArmorCanaryError(
            "no same-player exact-nonweapon exact-talent capture group has both variants"
        )
    complete_groups.sort(
        key=lambda pair: _sha256_bytes(
            _canonical_bytes(
                {
                    "player_guid": pair[0][0],
                    "nonweapon": pair[0][1],
                    "talent_sha256": pair[0][2],
                }
            )
        )
    )
    group_key, variants = complete_groups[0]

    def choose(values: Sequence[JSONMap]) -> JSONMap:
        return min(
            values,
            key=lambda row: (
                str(row["binding"]["source_jsonl"]).casefold(),
                int(row["binding"]["source_line"]),
                str(row["capture_id"]),
            ),
        )

    selected = tuple(choose(variants[variant]) for variant in WEAPON_VARIANTS)
    if selected[0]["capture_id"] == selected[1]["capture_id"]:
        raise FuryLoadoutArmorCanaryError("loadout variants cannot share one capture id")
    audit = {
        "selection_rule": {
            "eligibility": (
                "CALIBRATION_CAPTURE_BOUND/HISTORICAL_PRIOR_ONLY Warrior rows with "
                "a recomputed-valid capture_id, exactly 14 declared nonweapon slots, "
                "only slot 16 in addition, and exactly 16 nonzero talent entries"
            ),
            "group_rule": (
                "group by player GUID + exact full nonweapon (slot,item,enchant) tuple + "
                "canonical full talent-list SHA; require both weapon variants; choose the "
                "lexicographically smallest canonical group SHA"
            ),
            "within_variant_rule": (
                "minimum (source_jsonl.casefold(), source_line, capture_id)"
            ),
            "outcome_blind": True,
        },
        "eligible_variant_row_count": eligible_rows,
        "complete_control_group_count": len(complete_groups),
        "selected_group": {
            "player_guid": group_key[0],
            "nonweapon_signature": [list(row) for row in group_key[1]],
            "talent_list_sha256": group_key[2],
            "nonweapon_slot_count": len(group_key[1]),
            "nonzero_talent_entry_count": REQUIRED_NONZERO_TALENT_ENTRIES,
        },
    }
    return selected[0], selected[1], audit


def _read_catalog_rows(path: Path) -> Iterable[JSONMap]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FuryLoadoutArmorCanaryError(
                    f"catalog dataset line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(value, dict):
                raise FuryLoadoutArmorCanaryError(
                    f"catalog dataset line {line_number} is not an object"
                )
            yield value


def _loadout_blob(row: Mapping[str, Any], label: str) -> JSONMap:
    state = row["state"]
    binding = row["binding"]
    equipment = copy.deepcopy(state["equipment"])
    talents = copy.deepcopy(state["talents"])
    mainhand = next(item for item in equipment if item["slot"] == 16)
    return {
        "schema_version": 1,
        "kind": "capture_bound_loadout_bundle_v1",
        "scope": "CALIBRATION_CAPTURE_BOUND",
        "use_class": "HISTORICAL_PRIOR_ONLY",
        "label": label,
        "capture_id": row["capture_id"],
        "source_binding": {
            "source_jsonl": binding["source_jsonl"],
            "source_jsonl_sha256": binding["source_jsonl_sha256"],
            "source_line": binding["source_line"],
            "sequence": binding["sequence"],
            "captured_at": binding["captured_at"],
            "event": binding["event"],
            "task": copy.deepcopy(binding["task"]),
        },
        "player_guid": state["actor"]["player_guid"],
        "mainhand": {
            "item_id": mainhand["item_id"],
            "enchant_id": mainhand["enchant_id"],
            "link": mainhand.get("link"),
        },
        "equipment": equipment,
        "talents": talents,
        "semantic_boundary": {
            "same_capture_equipment_and_talents": True,
            "initial_auras_imported": False,
            "historical_midstate_imported": False,
            "missing_slots_17_18_rendered_empty_for_simulator": True,
        },
    }


def select_canary_families(corpus: SelectedCorpus) -> tuple[tuple[SelectedFamily, ...], tuple[JSONMap, ...]]:
    """Choose one outcome-blind representative from each of the 12 cells."""

    ultras = tuple(
        {
            "scenario_id": row.scenario.scenario_id,
            "instance_id": row.instance_id,
            "horizon_ms": row.scenario.horizon_ms,
            "target_count_stratum": row.target_count_stratum,
            "duration_stratum": row.duration_stratum,
        }
        for row in corpus.families
        if row.scenario.horizon_ms < 100
    )
    pools: dict[tuple[str, str], list[SelectedFamily]] = defaultdict(list)
    for row in corpus.families:
        if row.scenario.horizon_ms >= 100:
            pools[(row.target_count_stratum, row.duration_stratum)].append(row)
    selected: list[SelectedFamily] = []
    for target in TARGET_STRATA:
        for duration in DURATION_STRATA:
            values = pools.get((target, duration), [])
            if not values:
                raise FuryLoadoutArmorCanaryError(
                    f"required canary cell has no >=100 ms family: {target}/{duration}"
                )
            selected.append(
                min(
                    values,
                    key=lambda row: (
                        _sha256_bytes(
                            f"fury_loadout_armor_nuisance_canary_v1|{row.scenario.scenario_id}".encode(
                                "utf-8"
                            )
                        ),
                        row.scenario.scenario_id,
                    ),
                )
            )
    if len({row.scenario.scenario_id for row in selected}) != 12:
        raise FuryLoadoutArmorCanaryError("12-cell family selection is not unique")
    return tuple(selected), ultras


def _family_blob(selected: Sequence[SelectedFamily], ultras: Sequence[Mapping[str, Any]]) -> JSONMap:
    return {
        "schema_version": 1,
        "kind": "fury_loadout_armor_nuisance_family_selection_v1",
        "source_corpus_family_count": 392,
        "selection_rule": {
            "required_cells": [
                {"target_count_stratum": target, "duration_stratum": duration}
                for target in TARGET_STRATA
                for duration in DURATION_STRATA
            ],
            "minimum_horizon_ms_inclusive": 100,
            "representative": (
                "minimum SHA256('fury_loadout_armor_nuisance_canary_v1|' + "
                "scenario_id), tie scenario_id"
            ),
            "outcome_blind": True,
            "missing_cell_behavior": "FAIL_CLOSED_NO_SUBSTITUTION",
        },
        "selected": [
            {
                "scenario_id": row.scenario.scenario_id,
                "instance_id": row.instance_id,
                "family_id": row.family_id,
                "catalog_relative_path": row.catalog_relative_path,
                "target_count": row.target_count,
                "target_count_stratum": row.target_count_stratum,
                "duration_stratum": row.duration_stratum,
                "horizon_ms": row.scenario.horizon_ms,
                "source_sampling_weight": row.scenario.weight,
                "request": copy.deepcopy(row.scenario.request),
                "request_sha256": _sha256_bytes(_canonical_bytes(row.scenario.request)),
            }
            for row in selected
        ],
        "ultrashort_lt_100ms": {
            "excluded_from_primary_comparison": True,
            "count": len(ultras),
            "rows": copy.deepcopy(list(ultras)),
            "silently_deleted": False,
        },
    }


def _sim_equipment(loadout: Mapping[str, Any]) -> list[JSONMap]:
    by_slot = {int(item["slot"]): item for item in loadout["equipment"]}
    result: list[JSONMap] = []
    for slot in SIM_SLOT_TO_GAME_SLOT:
        item = by_slot.get(slot)
        if item is None:
            if slot not in {17, 18}:
                raise FuryLoadoutArmorCanaryError(
                    f"loadout is missing required simulator slot source {slot}"
                )
            result.append({})
            continue
        row: JSONMap = {"id": int(item["item_id"])}
        enchant = int(item["enchant_id"])
        if enchant:
            row["enchant"] = enchant
        result.append(row)
    return result


def request_with_loadout_and_armor(
    request: Mapping[str, Any], loadout: Mapping[str, Any], armor: int
) -> JSONMap:
    if armor not in STARTING_ARMORS:
        raise FuryLoadoutArmorCanaryError(f"unregistered starting armor: {armor}")
    result = copy.deepcopy(dict(request))
    raid = result.get("raid")
    parties = raid.get("parties") if isinstance(raid, Mapping) else None
    players = parties[0].get("players") if isinstance(parties, list) and len(parties) == 1 and isinstance(parties[0], Mapping) else None
    if not isinstance(players, list) or len(players) != 1 or not isinstance(players[0], dict):
        raise FuryLoadoutArmorCanaryError("request must contain exactly one mutable player")
    players[0]["equipment"] = {"items": _sim_equipment(loadout)}
    encounter = result.get("encounter")
    targets = encounter.get("targets") if isinstance(encounter, Mapping) else None
    if not isinstance(targets, list) or not targets:
        raise FuryLoadoutArmorCanaryError("request must contain at least one target")
    for target in targets:
        if not isinstance(target, dict):
            raise FuryLoadoutArmorCanaryError("target must be mutable")
        stats = target.get("stats")
        if not isinstance(stats, list) or len(stats) <= ARMOR_STAT_INDEX:
            raise FuryLoadoutArmorCanaryError("target lacks armor stat index")
        stats[ARMOR_STAT_INDEX] = armor
    options = result.get("simOptions")
    if not isinstance(options, dict):
        raise FuryLoadoutArmorCanaryError("request lacks mutable simOptions")
    options["iterations"] = 1
    options["randomSeed"] = str(ABSOLUTE_SEED)
    options["interactive"] = True
    return result


def _policy_adapters(candidate_parameters: Mapping[str, Any], cat2_snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        FuryTunedPolicyAdapter(FuryPolicyParameters(**dict(candidate_parameters))),
        CatFurySourceAdapter(),
        Cat2SavedProfileSourceAdapterV1(cat2_snapshot),
        ContraDeployedSourceAdapter(),
    )


def _experiment_rows(
    families: Sequence[SelectedFamily], loadouts: Sequence[Mapping[str, Any]], policy_ids: Sequence[str]
) -> tuple[JSONMap, ...]:
    rows: list[JSONMap] = []
    for family in families:
        for loadout in loadouts:
            label = str(loadout["label"])
            for armor in STARTING_ARMORS:
                request = request_with_loadout_and_armor(
                    family.scenario.request, loadout, armor
                )
                request_sha = _sha256_bytes(_canonical_bytes(request))
                for policy_id in policy_ids:
                    key = {
                        "scenario_id": family.scenario.scenario_id,
                        "target_count_stratum": family.target_count_stratum,
                        "duration_stratum": family.duration_stratum,
                        "horizon_ms": family.scenario.horizon_ms,
                        "loadout": label,
                        "capture_id": loadout["capture_id"],
                        "starting_armor": armor,
                        "policy_id": policy_id,
                        "absolute_seed": ABSOLUTE_SEED,
                        "request_sha256": request_sha,
                    }
                    rows.append(
                        {
                            **key,
                            "episode_key_sha256": _sha256_bytes(_canonical_bytes(key)),
                        }
                    )
    if len(rows) != 384 or len({row["episode_key_sha256"] for row in rows}) != 384:
        raise FuryLoadoutArmorCanaryError("experiment matrix is not exactly 384 unique episodes")
    return tuple(rows)


def _direct_source_identities() -> tuple[FileIdentity, ...]:
    names = (
        "o2o_dps.fury_loadout_armor_nuisance_canary_v1",
        "o2o_dps.fury_current_cat2_heldout_replay_v1",
        "o2o_dps.cat2_saved_profile_adapter_v1",
        "o2o_dps.fury_expert_adapters",
        "o2o_dps.fury_expert_closed_loop",
        "o2o_dps.fury_heldout_corpus_gate_v1",
        "o2o_dps.fury_policy_optimization_v1",
        "o2o_dps.sim_bridge",
        "o2o_dps.expert_policy",
        "o2o_dps.expert_proposals",
        "o2o_dps.fury_expert_guided_search_v1",
        "o2o_dps.fury_encounter_scenarios_v1",
    )
    paths: set[Path] = set()
    for name in names:
        module = importlib.import_module(name)
        value = getattr(module, "__file__", None)
        if not isinstance(value, str):
            raise FuryLoadoutArmorCanaryError(f"module has no source path: {name}")
        path = Path(value).resolve()
        if path.suffix == ".pyc":
            path = path.with_suffix(".py")
        paths.add(path)
    return tuple(
        _snapshot(path, "evaluator_python_source")
        for path in sorted(paths, key=lambda value: str(value).casefold())
    )


def build_plan_contract(blob_directory: Path = DEFAULT_BLOB_DIRECTORY) -> JSONMap:
    prefix_identity, prefix = _load_json(
        DEFAULT_PREFIX_GATE, "prefix_reconstruction_gate", EXPECTED_PREFIX_GATE_SHA256
    )
    if prefix.get("status") != "PASS" or prefix.get("prefix_reconstruction_gate_passed") is not True:
        raise FuryLoadoutArmorCanaryError("prefix reconstruction gate is not PASS")
    if prefix.get("capabilities", {}).get("canonical_time_zero_prefix_replay") is not True:
        raise FuryLoadoutArmorCanaryError("prefix gate lacks canonical time-zero replay")

    corpus, frozen_contract, upstream_identities = _load_locked_corpus(
        DEFAULT_FROZEN_GATE, DEFAULT_ABSOLUTE_CORPUS_LOCK
    )
    profile_identity, cat2_snapshot = _load_json(
        DEFAULT_CAT2_PROFILE, "cat2_profile", EXPECTED_CAT2_PROFILE_SHA256
    )
    Cat2SavedProfileSourceAdapterV1(cat2_snapshot)
    report_identity, report = _load_json(
        DEFAULT_CATALOG_REPORT,
        "historical_state_catalog_report",
        EXPECTED_CATALOG_REPORT_SHA256,
    )
    manifest_identity, manifest = _load_json(
        DEFAULT_CATALOG_MANIFEST,
        "historical_state_catalog_manifest",
        EXPECTED_CATALOG_MANIFEST_SHA256,
    )
    dataset_identity = _require_identity(
        DEFAULT_CATALOG_DATASET,
        "historical_state_catalog_dataset",
        EXPECTED_CATALOG_DATASET_SHA256,
    )
    catalog_builder_identity = _require_identity(
        DEFAULT_CATALOG_BUILDER,
        "historical_state_catalog_builder_source",
        EXPECTED_CATALOG_BUILDER_SHA256,
    )
    if (
        report.get("status") != "COMPLETE_HISTORICAL_PRIOR_ONLY"
        or report.get("scope") != "CALIBRATION_CAPTURE_BOUND"
        or report.get("output", {}).get("sha256") != dataset_identity.sha256
        or manifest.get("scope") != "CALIBRATION_CAPTURE_BOUND"
        or manifest.get("dataset", {}).get("sha256") != dataset_identity.sha256
        or manifest.get("dataset", {}).get("path_base") != "manifest_parent"
        or manifest.get("builder", {}).get("sha256") != catalog_builder_identity.sha256
        or manifest.get("report", {}).get("sha256") != report_identity.sha256
        or manifest.get("report", {}).get("path_base") != "manifest_parent"
        or manifest.get("inputs", {}).get("calibration_jsonl_path_base")
        != "manifest_parent"
        or manifest.get("inputs", {}).get("calibration_summaries_path_base")
        != "manifest_parent"
        or not isinstance(
            manifest.get("inputs", {}).get("calibration_jsonl_root"), str
        )
        or not isinstance(
            manifest.get("inputs", {}).get("calibration_summaries_root"), str
        )
    ):
        raise FuryLoadoutArmorCanaryError("historical catalog contract is inconsistent")
    left, right, capture_audit = select_loadout_captures(
        _read_catalog_rows(DEFAULT_CATALOG_DATASET)
    )
    loadouts = (
        _loadout_blob(left, "kalimdors_revenge_21679_unenchanted"),
        _loadout_blob(right, "bonereavers_edge_17076_crusader_1900"),
    )
    loadout_blob_ids = tuple(
        _write_content_blob(blob_directory, f"loadout_{index + 1}", value)
        for index, value in enumerate(loadouts)
    )

    selected, ultras = select_canary_families(corpus)
    if len(ultras) != 11:
        raise FuryLoadoutArmorCanaryError(
            f"absolute-seed corpus ultrashort audit expected 11, got {len(ultras)}"
        )
    family_document = _family_blob(selected, ultras)
    family_blob_id = _write_content_blob(
        blob_directory, "selected_families", family_document
    )

    adapters = _policy_adapters(
        frozen_contract["candidate_parameters"], cat2_snapshot
    )
    policy_ids = tuple(adapter.expert_id for adapter in adapters)
    expected_policy_ids = (
        frozen_contract["candidate_policy_id"],
        "cat.fury.profile1",
        "cat2.fury.brainofcat_shadow.saved_profile_source_v1",
        "contra.deployed.fury.raid_a",
    )
    if policy_ids != expected_policy_ids:
        raise FuryLoadoutArmorCanaryError(
            f"policy order/identity drifted: {policy_ids!r}"
        )
    matrix_rows = _experiment_rows(selected, loadouts, policy_ids)
    matrix_document = {
        "schema_version": 1,
        "kind": "fury_loadout_armor_nuisance_experiment_matrix_v1",
        "scope": SCOPE,
        "absolute_seed": ABSOLUTE_SEED,
        "reference_pass_episode_count": 384,
        "fresh_repeat_pass_episode_count": 384,
        "total_episode_count": 768,
        "rows": list(matrix_rows),
    }
    matrix_blob_id = _write_content_blob(
        blob_directory, "experiment_matrix", matrix_document
    )

    bridge_identity = _require_identity(
        DEFAULT_BRIDGE, "seed_fixed_simulator_bridge", EXPECTED_BRIDGE_SHA256
    )
    item_source_identity = _require_identity(
        DEFAULT_ITEM_EFFECTS_SOURCE,
        "static_item_effects_source",
        EXPECTED_ITEM_EFFECTS_SOURCE_SHA256,
    )
    enchant_source_identity = _require_identity(
        DEFAULT_ENCHANT_EFFECTS_SOURCE,
        "static_enchant_effects_source",
        EXPECTED_ENCHANT_EFFECTS_SOURCE_SHA256,
    )
    direct_sources = _direct_source_identities()
    additional_inputs = (
        prefix_identity,
        profile_identity,
        report_identity,
        manifest_identity,
        dataset_identity,
        catalog_builder_identity,
        bridge_identity,
        item_source_identity,
        enchant_source_identity,
        *direct_sources,
        *loadout_blob_ids,
        family_blob_id,
        matrix_blob_id,
    )
    identities: dict[tuple[str, str], FileIdentity] = {}
    for identity in (*upstream_identities, *additional_inputs):
        identities[(identity.role, identity.path)] = identity
    ordered_inputs = tuple(
        identities[key]
        for key in sorted(identities, key=lambda value: (value[0], value[1].casefold()))
    )

    return {
        "schema_version": 1,
        "kind": "fury_loadout_armor_nuisance_canary_plan_contract_v1",
        "scope": SCOPE,
        "preconditions": {
            "prefix_gate_status": "PASS",
            "prefix_gate_sha256": prefix_identity.sha256,
            "absolute_seed_corpus_lock_sha256": EXPECTED_ABSOLUTE_CORPUS_LOCK_SHA256,
            "absolute_seed_corpus_family_count": len(corpus.families),
            "candidate_training_instances_excluded": True,
            "historical_catalog_scope": "CALIBRATION_CAPTURE_BOUND",
            "historical_catalog_use_class": "HISTORICAL_PRIOR_ONLY",
        },
        "inputs": {
            "file_count": len(ordered_inputs),
            "files": [asdict(value) for value in ordered_inputs],
            "file_bundle_sha256": _sha256_bytes(
                _canonical_bytes([asdict(value) for value in ordered_inputs])
            ),
            "pre_run_verified": True,
            "post_run_reverification_required": True,
            "transitive_python_dependency_closure_claimed": False,
        },
        "historical_loadout_selection": {
            **capture_audit,
            "loadout_blobs": [asdict(value) for value in loadout_blob_ids],
            "selected": [
                {
                    "label": loadout["label"],
                    "capture_id": loadout["capture_id"],
                    "source_binding": copy.deepcopy(loadout["source_binding"]),
                    "mainhand": copy.deepcopy(loadout["mainhand"]),
                }
                for loadout in loadouts
            ],
            "a1cd40d_not_selected_reason": (
                "capture a1cd40d348abe6173a20a77394e1a9825527e7457a74a6debcd480a0d50fa7c1 "
                "is another eligible 21679 capture, but the preregistered within-variant "
                "minimum source-jsonl/source-line/capture-id rule selects source line 30 "
                "capture 8bb31d8e198a7aafd310ed20aeafd07d9724b0fdf09d07bac47858c577ef4489"
            ),
        },
        "family_selection": {
            "blob": asdict(family_blob_id),
            "selected_family_count": len(selected),
            "required_cell_count": 12,
            "minimum_primary_horizon_ms": min(row.scenario.horizon_ms for row in selected),
            "ultrashort_lt_100ms_count": len(ultras),
            "ultrashort_rows_preserved_in_blob": True,
        },
        "experiment": {
            "matrix_blob": asdict(matrix_blob_id),
            "policy_ids": list(policy_ids),
            "loadout_count": 2,
            "starting_armors": list(STARTING_ARMORS),
            "starting_armor_semantics": "simulator request target stat index 26 at time zero",
            "exact_sunder_or_proc_stack_claimed": False,
            "absolute_seed": ABSOLUTE_SEED,
            "reference_pass_episode_count": 384,
            "fresh_repeat_pass_episode_count": 384,
            "total_episode_count": 768,
            "fresh_bridge_process_per_pass": True,
            "canonical_episode_hash_equality_required": True,
            "zero_omissions_required": True,
            "complete_horizons_required": True,
        },
        "simulator_proc_mechanism_boundary": {
            "binary_sha256": bridge_identity.sha256,
            "static_source_correspondence_only_not_binary_build_attestation": True,
            "kalimdors_revenge_21679": (
                "current static source registers a 1.25 PPM Nature damage proc; the "
                "interactive bridge result does not expose a direct proc-event stream"
            ),
            "bonereavers_edge_17076": (
                "current static source registers a 2.0 PPM aura reducing every encounter "
                "target by 700 armor per stack, up to 3 stacks for 10 seconds"
            ),
            "crusader_enchant_1900": (
                "current static source registers a 1.0 PPM main-hand Strength aura for "
                "15 seconds at level 60"
            ),
            "bundle_causal_boundary": (
                "Bonereaver and Crusader vary together as one historical capture-bound "
                "bundle and cannot be separated causally by this canary"
            ),
        },
        "claim_boundary": {
            "exact_armor_debuff_stack_claimed": False,
            "historical_joint_state_claimed": False,
            "independent_raid_claimed": False,
            "exact_lua_execution_claimed": False,
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
    }


def _load_blob(identity: Mapping[str, Any], expected_kind: str) -> JSONMap:
    expected = FileIdentity(**dict(identity))
    _verify_identity(expected)
    _, value = _load_json(Path(expected.path), expected.role, expected.sha256)
    if value.get("kind") != expected_kind:
        raise FuryLoadoutArmorCanaryError(
            f"content blob kind mismatch for {expected.path}"
        )
    return value


def _proc_observer() -> tuple[JSONMap, Any]:
    summary: JSONMap = {
        "player_aura_labels_seen": set(),
        "minimum_effective_target_armor": None,
    }

    def sink(step: Mapping[str, Any]) -> None:
        for field in (
            "simulator_state_before",
            "simulator_state_after_commands",
            "simulator_state_next_epoch",
        ):
            state = step.get(field)
            if not isinstance(state, Mapping):
                continue
            armor = state.get("effective_target_armor")
            if isinstance(armor, (int, float)) and not isinstance(armor, bool) and isfinite(float(armor)):
                current = summary["minimum_effective_target_armor"]
                if current is None or float(armor) < float(current):
                    summary["minimum_effective_target_armor"] = float(armor)
            auras = state.get("auras")
            if isinstance(auras, list):
                for aura in auras:
                    label = aura.get("label") if isinstance(aura, Mapping) else None
                    if isinstance(label, str) and label:
                        summary["player_aura_labels_seen"].add(label)

    return summary, sink


def _finalize_proc_observation(value: Mapping[str, Any]) -> JSONMap:
    labels = sorted(value["player_aura_labels_seen"])
    return {
        "player_aura_labels_seen": labels,
        "minimum_effective_target_armor": value["minimum_effective_target_armor"],
        "bonereaver_aura_observed": "Bonereaver's Edge" in labels,
        "crusader_mh_aura_observed": "Crusader Enchant MH" in labels,
        "kalimdor_direct_proc_observable": False,
    }


def _run_pass(
    bridge_path: Path,
    families: Mapping[str, Mapping[str, Any]],
    loadouts: Sequence[Mapping[str, Any]],
    candidate_parameters: Mapping[str, Any],
    cat2_snapshot: Mapping[str, Any],
    matrix_rows: Sequence[Mapping[str, Any]],
    *,
    pass_label: str,
) -> tuple[JSONMap, ...]:
    adapters = {adapter.expert_id: adapter for adapter in _policy_adapters(candidate_parameters, cat2_snapshot)}
    results: list[JSONMap] = []
    with SimulatorBridge(bridge_path) as bridge:
        for ordinal, matrix in enumerate(matrix_rows, start=1):
            family = families[str(matrix["scenario_id"])]
            loadout = next(
                row for row in loadouts if row["capture_id"] == matrix["capture_id"]
            )
            request = request_with_loadout_and_armor(
                family["request"], loadout, int(matrix["starting_armor"])
            )
            if _sha256_bytes(_canonical_bytes(request)) != matrix["request_sha256"]:
                raise FuryLoadoutArmorCanaryError("runtime request differs from matrix lock")
            proc_summary, sink = _proc_observer()
            rollout = run_fury_expert_closed_loop(
                bridge,
                request,
                adapters[str(matrix["policy_id"])],
                seed=ABSOLUTE_SEED,
                horizon_ms=int(matrix["horizon_ms"]),
                retain_steps=False,
                transition_sink=sink,
            )
            proc = _finalize_proc_observation(proc_summary)
            canonical_result = {
                "rollout": rollout,
                "proc_observation": proc,
            }
            results.append(
                {
                    "episode_key_sha256": matrix["episode_key_sha256"],
                    "canonical_result_sha256": _sha256_bytes(
                        _canonical_bytes(canonical_result)
                    ),
                    "damage_delta": float(rollout["damage_delta"]),
                    "dps": float(rollout["dps"]),
                    "omitted_lane_count": int(rollout["omitted_lane_count"]),
                    "configured_horizon_complete": bool(
                        rollout["configured_horizon_complete"]
                    ),
                    "nonfaithful_reason_counts": copy.deepcopy(
                        rollout["nonfaithful_reason_counts"]
                    ),
                    "proc_observation": proc,
                }
            )
            if ordinal % 24 == 0:
                print(
                    json.dumps(
                        {
                            "pass": pass_label,
                            "completed": ordinal,
                            "expected": len(matrix_rows),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
    return tuple(results)


def compare_passes(reference: Sequence[Mapping[str, Any]], repeat: Sequence[Mapping[str, Any]]) -> JSONMap:
    if len(reference) != 384 or len(repeat) != 384:
        raise FuryLoadoutArmorCanaryError("each pass must contain exactly 384 episodes")
    ref = {str(row["episode_key_sha256"]): row for row in reference}
    rep = {str(row["episode_key_sha256"]): row for row in repeat}
    if len(ref) != 384 or set(ref) != set(rep):
        raise FuryLoadoutArmorCanaryError("reference/repeat episode key structure differs")
    mismatches = [
        key
        for key in sorted(ref)
        if ref[key]["canonical_result_sha256"] != rep[key]["canonical_result_sha256"]
    ]
    return {
        "reference_episode_count": len(reference),
        "fresh_repeat_episode_count": len(repeat),
        "total_episode_count": len(reference) + len(repeat),
        "unique_episode_key_count": len(ref),
        "canonical_hash_match_count": len(ref) - len(mismatches),
        "canonical_hash_mismatch_count": len(mismatches),
        "mismatch_episode_keys": mismatches,
        "deterministic_repeat_passed": not mismatches,
    }


def _summaries(matrix_rows: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]) -> JSONMap:
    by_key = {row["episode_key_sha256"]: row for row in matrix_rows}
    buckets: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    omission_count = 0
    incomplete_count = 0
    nonfaithful: Counter[str] = Counter()
    proc_counts: Counter[str] = Counter()
    for result in results:
        matrix = by_key[result["episode_key_sha256"]]
        buckets[(str(matrix["loadout"]), int(matrix["starting_armor"]), str(matrix["policy_id"]))].append(float(result["dps"]))
        omission_count += int(result["omitted_lane_count"])
        incomplete_count += int(not result["configured_horizon_complete"])
        nonfaithful.update(
            {str(key): int(value) for key, value in result["nonfaithful_reason_counts"].items()}
        )
        proc = result["proc_observation"]
        if proc["bonereaver_aura_observed"]:
            proc_counts["bonereaver_aura_episode_count"] += 1
        if proc["crusader_mh_aura_observed"]:
            proc_counts["crusader_mh_aura_episode_count"] += 1
    rows = [
        {
            "loadout": key[0],
            "starting_armor": key[1],
            "policy_id": key[2],
            "episode_count": len(values),
            "equal_cell_mean_dps": fmean(values),
        }
        for key, values in sorted(buckets.items())
    ]
    if len(rows) != 32 or any(row["episode_count"] != 12 for row in rows):
        raise FuryLoadoutArmorCanaryError("summary does not contain 32 complete 12-cell groups")
    return {
        "equal_cell_dps": rows,
        "omitted_lane_count": omission_count,
        "incomplete_horizon_count": incomplete_count,
        "all_zero_omissions": omission_count == 0,
        "all_horizons_complete": incomplete_count == 0,
        "nonfaithful_reason_counts": dict(sorted(nonfaithful.items())),
        "proc_observation_counts": dict(sorted(proc_counts.items())),
        "weighting_boundary": (
            "equal weight over the 12 preregistered cross-stratum representatives; "
            "not a population or raid-frequency estimate"
        ),
    }


def _verify_plan_inputs(contract: Mapping[str, Any]) -> None:
    raw = contract.get("inputs", {}).get("files")
    if not isinstance(raw, list):
        raise FuryLoadoutArmorCanaryError("plan contract lacks input identities")
    for identity in raw:
        if not isinstance(identity, Mapping):
            raise FuryLoadoutArmorCanaryError("plan input identity is invalid")
        _verify_identity(identity)


def run_from_plan(plan_path: Path, expected_plan_sha256: str, output: Path) -> JSONMap:
    plan_identity, plan = _load_json(
        plan_path, "canary_plan", expected_plan_sha256.casefold()
    )
    contract = plan.get("contract")
    if not isinstance(contract, Mapping):
        raise FuryLoadoutArmorCanaryError("canary plan lacks contract")
    if plan.get("contract_sha256") != _sha256_bytes(_canonical_bytes(contract)):
        raise FuryLoadoutArmorCanaryError("canary plan contract digest is invalid")
    _verify_plan_inputs(contract)
    rebuilt = build_plan_contract(DEFAULT_BLOB_DIRECTORY)
    if rebuilt != contract:
        raise FuryLoadoutArmorCanaryError("current deterministic plan differs from locked plan")

    loadout_blobs = [
        _load_blob(row, "capture_bound_loadout_bundle_v1")
        for row in contract["historical_loadout_selection"]["loadout_blobs"]
    ]
    family_blob = _load_blob(
        contract["family_selection"]["blob"],
        "fury_loadout_armor_nuisance_family_selection_v1",
    )
    matrix_blob = _load_blob(
        contract["experiment"]["matrix_blob"],
        "fury_loadout_armor_nuisance_experiment_matrix_v1",
    )
    families = {row["scenario_id"]: row for row in family_blob["selected"]}
    matrix_rows = matrix_blob["rows"]
    _, frozen = _load_json(
        DEFAULT_FROZEN_GATE, "frozen_heldout_gate", EXPECTED_FROZEN_GATE_SHA256
    )
    _, cat2 = _load_json(
        DEFAULT_CAT2_PROFILE, "cat2_profile", EXPECTED_CAT2_PROFILE_SHA256
    )
    candidate = frozen.get("candidate")
    if not isinstance(candidate, Mapping):
        raise FuryLoadoutArmorCanaryError("frozen gate lacks candidate")
    candidate_parameters = candidate.get("parameters")
    if not isinstance(candidate_parameters, Mapping):
        raise FuryLoadoutArmorCanaryError("frozen candidate lacks parameters")

    reference = _run_pass(
        DEFAULT_BRIDGE,
        families,
        loadout_blobs,
        candidate_parameters,
        cat2,
        matrix_rows,
        pass_label="reference",
    )
    repeat = _run_pass(
        DEFAULT_BRIDGE,
        families,
        loadout_blobs,
        candidate_parameters,
        cat2,
        matrix_rows,
        pass_label="fresh_repeat",
    )
    comparison = compare_passes(reference, repeat)
    summary = _summaries(matrix_rows, reference)
    _verify_plan_inputs(contract)
    structural_pass = bool(
        comparison["deterministic_repeat_passed"]
        and summary["all_zero_omissions"]
        and summary["all_horizons_complete"]
        and comparison["total_episode_count"] == 768
    )
    result_rows = [
        {
            **copy.deepcopy(dict(matrix)),
            **copy.deepcopy(dict(reference[index])),
            "fresh_repeat_canonical_result_sha256": repeat[index][
                "canonical_result_sha256"
            ],
            "repeat_exact": reference[index]["canonical_result_sha256"]
            == repeat[index]["canonical_result_sha256"],
        }
        for index, matrix in enumerate(matrix_rows)
    ]
    artifact = {
        "schema_version": 1,
        "kind": "fury_loadout_armor_nuisance_canary_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "STRUCTURAL_AND_DETERMINISM_PASS" if structural_pass else "FAIL_CLOSED",
        "scope": SCOPE,
        "plan": asdict(plan_identity),
        "contract_sha256": plan["contract_sha256"],
        "input_preverification_passed": True,
        "input_post_reverification_passed": True,
        "execution": {
            **comparison,
            "fresh_bridge_process_per_pass": True,
            "process_pass_count": 2,
            "structural_and_determinism_passed": structural_pass,
        },
        "summary": summary,
        "results": result_rows,
        "interpretation": {
            "nuisance_only": True,
            "bonereaver_plus_crusader_is_one_bundle": True,
            "separate_item_vs_enchant_causal_effect_claimed": False,
            "starting_armor_is_not_exact_debuff_stack": True,
            "historical_joint_state_claimed": False,
            "independent_raid_claimed": False,
            "exact_lua_execution_claimed": False,
            "ultrashort_lt_100ms_excluded_from_primary_but_preserved": True,
            "ultrashort_lt_100ms_count": 11,
        },
        "gates": {
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
    }
    artifact_identity, receipt_identity = _write_with_receipt(
        output,
        artifact,
        {
            "artifact_status": artifact["status"],
            "scope": SCOPE,
            "plan_sha256": plan_identity.sha256,
            "contract_sha256": plan["contract_sha256"],
            "reference_episode_count": 384,
            "fresh_repeat_episode_count": 384,
            "total_episode_count": 768,
            "canonical_hash_mismatch_count": comparison[
                "canonical_hash_mismatch_count"
            ],
            "all_zero_omissions": summary["all_zero_omissions"],
            "all_horizons_complete": summary["all_horizons_complete"],
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
                "total_episode_count": 768,
                "repeat_mismatches": comparison["canonical_hash_mismatch_count"],
                "zero_omissions": summary["all_zero_omissions"],
                "complete_horizons": summary["all_horizons_complete"],
                "all_gates": False,
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
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--blob-directory", type=Path, default=DEFAULT_BLOB_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.plan_only:
        contract = build_plan_contract(args.blob_directory)
        plan = {
            "schema_version": 1,
            "kind": "fury_loadout_armor_nuisance_canary_plan_v1",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "contract_sha256": _sha256_bytes(_canonical_bytes(contract)),
            "contract": contract,
            "execution_status": "NOT_RUN",
            "all_downstream_gates": False,
        }
        plan_identity, receipt_identity = _write_with_receipt(
            args.plan,
            plan,
            {
                "artifact_status": "NOT_RUN",
                "scope": SCOPE,
                "contract_sha256": plan["contract_sha256"],
                "planned_total_episode_count": 768,
                "all_downstream_gates": False,
            },
        )
        print(
            json.dumps(
                {
                    "status": "PLAN_LOCKED_NOT_RUN",
                    "plan": asdict(plan_identity),
                    "receipt": asdict(receipt_identity),
                    "contract_sha256": plan["contract_sha256"],
                    "planned_total_episode_count": 768,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.expected_plan_sha256:
        raise FuryLoadoutArmorCanaryError(
            "--run requires --expected-plan-sha256"
        )
    run_from_plan(args.plan, args.expected_plan_sha256, args.output)
    return 0


__all__: Sequence[str] = (
    "FuryLoadoutArmorCanaryError",
    "build_plan_contract",
    "compare_passes",
    "main",
    "request_with_loadout_and_armor",
    "run_from_plan",
    "select_canary_families",
    "select_loadout_captures",
)


if __name__ == "__main__":
    raise SystemExit(main())
