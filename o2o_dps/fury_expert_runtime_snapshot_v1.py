"""Capture the local Fury expert/runtime identity without executing Lua.

The deployed Cat and Contra source bundles are not complete policy identities:
both read per-character SavedVariables, and their behavior also depends on the
fixed character build and Nampower controls.  This module captures those
mutable inputs as a deterministic, content-addressed *local* artifact.  It
never executes SavedVariables or copies their unrelated account contents into
the repository.

The snapshot closes configuration provenance only.  It deliberately remains
comparison-ineligible until ordered sink submission, client acceptance,
server outcome, and a full simulator adapter are independently validated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence

from .cat_deployed_source_manifest_v1 import (
    DEFAULT_MANIFEST as DEFAULT_CAT_MANIFEST,
    load_manifest as load_cat_manifest,
)
from .import_savedvariables import SavedVariablesImportError, _LuaTable, _Parser
from .wowsims_profile import (
    ProfileSelection,
    WowsimsProfileError,
    build_wowsims_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "fury_expert_runtime_snapshot/v1"
IMPLEMENTATION_REVISION = "v1.1_same_character_savedvariables_and_build_capture"
DEFAULT_WOWSIMS_PROFILE = PROJECT_ROOT / "configs/wowsims/fury_warrior_live.json"
DEFAULT_WOWSIMS_METADATA = (
    PROJECT_ROOT / "configs/wowsims/fury_warrior_live.metadata.json"
)
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "offline_data/expert_runtime_snapshots/v1"
CONTRA_RECURSIVE_COMMENT = "--[[ skipped recursive table ]]"

CAT_PROFILE_KEYS = frozenset(
    {
        "BattleShout",
        "BerserkerRage",
        "BerserkerStance",
        "Carrot",
        "Carrot_Value",
        "Charge",
        "DeathWish",
        "DeathWishBoss",
        "Execute",
        "ExecuteOtherTarget",
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
        "Rend",
        "Rend_Value",
        "Slam_Value",
        "Soulspeed",
        "SoulspeedBoss",
        "SunderArmor",
        "SunderArmorBOSS",
        "SunderArmorOnce",
        "SuperWoW",
        "Sweeping",
        "Sweeping_Value",
        "TBBoss",
        "TUBoss",
        "Target",
        "Trinket_Below",
        "Trinket_Upper",
        "UnitXP",
        "UseExecute",
        "Whirlwind",
    }
)

CONTRA_REQUIRED_BUTTON_KEYS = frozenset(
    {
        "fangan",
        "mode",
        "xuanfeng",
        "silie",
        "shengcun",
        "baofa",
        "autoselect",
        "quanbudaduan",
        "zhidingdaduan",
        "liunudaduan",
        "bossothuanwuqi",
        "xiaoguaiothuanwuqi",
    }
)


class FuryExpertRuntimeSnapshotError(RuntimeError):
    """A mutable runtime input is missing, malformed, or changed while read."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _stable_read(path: str | Path, label: str) -> tuple[bytes, Path]:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        before = resolved.stat()
        if not resolved.is_file():
            raise FuryExpertRuntimeSnapshotError(f"{label} is not a file: {resolved}")
        payload = resolved.read_bytes()
        after = resolved.stat()
    except FuryExpertRuntimeSnapshotError:
        raise
    except OSError as error:
        raise FuryExpertRuntimeSnapshotError(
            f"cannot read {label} {candidate}: {error}"
        ) from error
    signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if signature_before != signature_after or len(payload) != after.st_size:
        raise FuryExpertRuntimeSnapshotError(f"{label} changed while being read")
    return payload, resolved


def _decode_utf8(payload: bytes, label: str) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FuryExpertRuntimeSnapshotError(f"{label} is not UTF-8: {error}") from error


def _parse_savedvariables(
    payload: bytes,
    label: str,
    *,
    strip_contra_recursive_comment: bool = False,
) -> dict[str, Any]:
    text = _decode_utf8(payload, label)
    if strip_contra_recursive_comment:
        count = text.count(CONTRA_RECURSIVE_COMMENT)
        if count > 1:
            raise FuryExpertRuntimeSnapshotError(
                f"{label} contains {count} recursive-table comments; expected at most one"
            )
        text = text.replace(CONTRA_RECURSIVE_COMMENT, "")
    try:
        return _Parser(text, label).parse()
    except SavedVariablesImportError as error:
        raise FuryExpertRuntimeSnapshotError(
            f"{label} is outside the literal SavedVariables grammar: {error}"
        ) from error


def _table(value: Any, label: str) -> _LuaTable:
    if not isinstance(value, _LuaTable):
        raise FuryExpertRuntimeSnapshotError(f"{label} must be a Lua table")
    return value


def _string_map(value: Any, label: str) -> dict[str, Any]:
    table = _table(value, label)
    if any(not isinstance(key, str) for key in table.fields):
        raise FuryExpertRuntimeSnapshotError(f"{label} must use string keys only")
    return {str(key): _plain_literal(item, f"{label}.{key}") for key, item in table.fields.items()}


def _plain_literal(value: Any, label: str) -> Any:
    if isinstance(value, _LuaTable):
        keys = list(value.fields)
        if all(isinstance(key, str) for key in keys):
            return {
                str(key): _plain_literal(value.fields[key], f"{label}.{key}")
                for key in sorted(keys)
            }
        if all(type(key) is int and key >= 1 for key in keys):
            ordered = sorted(keys)
            if ordered != list(range(1, len(ordered) + 1)):
                raise FuryExpertRuntimeSnapshotError(f"{label} is not a dense Lua array")
            return [
                _plain_literal(value.fields[index], f"{label}[{index}]")
                for index in ordered
            ]
        raise FuryExpertRuntimeSnapshotError(f"{label} mixes unsupported Lua key types")
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) in {int, float}:
        if isinstance(value, float) and not math.isfinite(value):
            raise FuryExpertRuntimeSnapshotError(f"{label} is non-finite")
        return int(value) if isinstance(value, float) and value.is_integer() else value
    raise FuryExpertRuntimeSnapshotError(
        f"{label} has unsupported literal type {type(value).__name__}"
    )


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FuryExpertRuntimeSnapshotError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(_decode_utf8(payload, label), object_pairs_hook=hook)
    except (json.JSONDecodeError, ValueError) as error:
        if isinstance(error, FuryExpertRuntimeSnapshotError):
            raise
        raise FuryExpertRuntimeSnapshotError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise FuryExpertRuntimeSnapshotError(f"{label} root must be an object")
    return value


def _cat_profile(
    document: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    root = _table(document.get("MPWarriorFurySaved"), "MPWarriorFurySaved")
    expected_version = int(manifest["fury_policy_surface"]["settings_schema_version"])
    version = root.fields.get("Version")
    if type(version) is not int or version != expected_version:
        raise FuryExpertRuntimeSnapshotError(
            f"MPWarriorFurySaved.Version must be {expected_version}, got {version!r}"
        )
    slot = int(manifest["fury_policy_surface"]["binding_selected_profile_slot"])
    profile = _string_map(root.fields.get(slot), f"MPWarriorFurySaved[{slot}]")
    if set(profile) != CAT_PROFILE_KEYS:
        missing = sorted(CAT_PROFILE_KEYS - set(profile))
        extra = sorted(set(profile) - CAT_PROFILE_KEYS)
        raise FuryExpertRuntimeSnapshotError(
            f"Cat Fury profile keys drifted; missing={missing}, extra={extra}"
        )
    active_keys = set(manifest["fury_policy_surface"]["active_runtime_config_keys"])
    if not active_keys <= set(profile):
        raise FuryExpertRuntimeSnapshotError("Cat source manifest active keys are absent")
    return {
        "profile_slot": slot,
        "settings_schema_version": version,
        "all_profile_values": profile,
        "profile_semantic_sha256": sha256_json(profile),
        "adapter_core_projection": {
            "battle_shout_enabled": profile["BattleShout"] == 1,
            "berserker_stance_required": profile["BerserkerStance"] == 1,
            "bloodrage_enabled": profile["BerserkerRage"] == 1,
            "execute_enabled": profile["UseExecute"] == 1,
            "execute_non_boss": profile["ExecuteWithoutMonster"] == 1,
            "hamstring_enabled": profile["Hamstring"] == 1,
            "heroic_strike_mode": "FIXED" if profile["HeroicStrike"] == 1 else "DYNAMIC",
            "heroic_strike_fixed_threshold": profile["HeroicStrike_Value"],
            "nearby_enemy_switch_enabled": profile["NearbyEnemies"] == 1,
            "nearby_enemy_threshold": profile["NearbyEnemies_Value"],
            "slam_timing_s": profile["Slam_Value"],
            "target_mode": profile["Target"],
            "whirlwind_enabled": profile["Whirlwind"] == 1,
        },
    }


def _contra_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    root = _table(document.get("ContraDB"), "ContraDB")
    warrior = _table(root.fields.get("Warrior"), "ContraDB.Warrior")
    buttons = _string_map(warrior.fields.get("Buttons"), "ContraDB.Warrior.Buttons")
    missing = sorted(CONTRA_REQUIRED_BUTTON_KEYS - set(buttons))
    if missing:
        raise FuryExpertRuntimeSnapshotError(f"Contra Warrior Buttons missing {missing}")
    selected_name = buttons["fangan"]
    if not isinstance(selected_name, str):
        raise FuryExpertRuntimeSnapshotError("Contra selected fangan must be a string")
    match = re.search(r"(\d+)\Z", selected_name)
    if match is None:
        raise FuryExpertRuntimeSnapshotError(
            f"Contra selected fangan has no numeric suffix: {selected_name!r}"
        )
    selected_index = int(match.group(1))
    selected_key = f"fangan{selected_index}"
    selected = _string_map(
        warrior.fields.get(selected_key), f"ContraDB.Warrior.{selected_key}"
    )
    return {
        "selected_scheme_index": selected_index,
        "selected_scheme_key": selected_key,
        "buttons": buttons,
        "selected_saved_scheme": selected,
        "buttons_semantic_sha256": sha256_json(buttons),
        "selected_saved_scheme_sha256": sha256_json(selected),
        "adapter_core_projection": {
            "mode": buttons["mode"],
            "xuanfeng": buttons["xuanfeng"],
            "interrupt_enabled": any(
                buttons[key]
                for key in ("quanbudaduan", "zhidingdaduan", "liunudaduan")
            ),
            "autoselect": buttons["autoselect"],
            "dual_wield_boss": buttons["bossothuanwuqi"],
            "dual_wield_nonboss": buttons["xiaoguaiothuanwuqi"],
            "survival": buttons["shengcun"],
            "burst": buttons["baofa"],
            "rend": buttons["silie"],
        },
    }


def _nampower_settings(payload: bytes) -> dict[str, str]:
    text = _decode_utf8(payload, "Config.wtf")
    rows: dict[str, str] = {}
    for name, value in re.findall(r'^SET\s+(NP_[A-Za-z0-9_]+)\s+"([^"]*)"\s*$', text, re.MULTILINE):
        if name in rows:
            raise FuryExpertRuntimeSnapshotError(f"Config.wtf repeats {name}")
        rows[name] = value
    if "NP_QueueSpellsOnCooldown" not in rows:
        raise FuryExpertRuntimeSnapshotError(
            "Config.wtf lacks NP_QueueSpellsOnCooldown"
        )
    return dict(sorted(rows.items()))


def _fixed_build(profile: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    try:
        player = profile["raid"]["parties"][0]["players"][0]
        mapped = metadata["mapped"]
    except (KeyError, IndexError, TypeError) as error:
        raise FuryExpertRuntimeSnapshotError(
            "wowsims profile/metadata lacks the first player or mapped identity"
        ) from error
    if not isinstance(player, Mapping) or not isinstance(mapped, Mapping):
        raise FuryExpertRuntimeSnapshotError("wowsims player/mapped identity must be objects")
    for field in ("name", "race", "talentsString"):
        if player.get(field) != mapped.get(field if field != "talentsString" else "talents_string"):
            raise FuryExpertRuntimeSnapshotError(
                f"wowsims metadata mapped {field} does not match the request"
            )
    return {
        "player_semantic_sha256": sha256_json(player),
        "name": player["name"],
        "race": player["race"],
        "talents_string": player["talentsString"],
        "equipment_slot_count": len(player.get("equipment", {}).get("items", [])),
        "turtle_talent_options": metadata.get("talent_option_mappings", []),
        "capture_source": metadata.get("source"),
    }


def _load_build_capture_record(
    metadata: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, int]:
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata lacks its static build capture source"
        )
    raw_path = source.get("calibration_jsonl")
    line_number = source.get("line_number")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata source lacks calibration_jsonl"
        )
    if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number <= 0:
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata source line_number must be positive"
        )
    candidate = Path(raw_path).expanduser()
    capture_path = (
        candidate.resolve()
        if candidate.is_absolute()
        else (PROJECT_ROOT / candidate).resolve()
    )
    try:
        with capture_path.open("r", encoding="utf-8") as handle:
            line = next(
                (value for index, value in enumerate(handle, start=1) if index == line_number),
                None,
            )
    except (OSError, UnicodeDecodeError) as error:
        raise FuryExpertRuntimeSnapshotError(
            f"cannot read static build capture {capture_path}: {error}"
        ) from error
    if line is None:
        raise FuryExpertRuntimeSnapshotError(
            f"static build capture has no line {line_number}: {capture_path}"
        )
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise FuryExpertRuntimeSnapshotError(
            f"static build capture line {line_number} is not JSON: {error}"
        ) from error
    if not isinstance(record, dict):
        raise FuryExpertRuntimeSnapshotError("static build capture record must be an object")
    if record.get("event") != source.get("event") or record.get("sequence") != source.get(
        "sequence"
    ):
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata source pointer does not match the build capture record"
        )
    return record, capture_path, line_number


def _same_character_context(
    *,
    cat_path: Path,
    contra_path: Path,
    profile: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    if cat_path.parent != contra_path.parent:
        raise FuryExpertRuntimeSnapshotError(
            "Cat and Contra SavedVariables must come from the same character directory"
        )
    savedvariables = cat_path.parent
    if savedvariables.name.casefold() != "savedvariables":
        raise FuryExpertRuntimeSnapshotError(
            "Cat and Contra inputs must be inside a character SavedVariables directory"
        )
    if cat_path.name.casefold() != "cat.lua" or contra_path.name.casefold() != "contra.lua":
        raise FuryExpertRuntimeSnapshotError(
            "runtime inputs must be the Cat.lua and Contra.lua character files"
        )
    character_directory = savedvariables.parent
    realm_directory = character_directory.parent
    if not character_directory.name or not realm_directory.name:
        raise FuryExpertRuntimeSnapshotError(
            "SavedVariables path lacks realm and character directories"
        )

    capture, capture_path, line_number = _load_build_capture_record(metadata)
    state = capture.get("state")
    if not isinstance(state, Mapping):
        raise FuryExpertRuntimeSnapshotError("static build capture lacks state")
    identity = state.get("characterIdentity")
    if not isinstance(identity, Mapping):
        raise FuryExpertRuntimeSnapshotError(
            "static build capture lacks characterIdentity"
        )
    capture_name = identity.get("name")
    if capture_name != character_directory.name:
        raise FuryExpertRuntimeSnapshotError(
            "static build capture character does not match the SavedVariables directory"
        )
    if identity.get("classFile") != "WARRIOR":
        raise FuryExpertRuntimeSnapshotError(
            "static build capture is not a Warrior"
        )
    player_guid = state.get("playerGUID")
    if not isinstance(player_guid, str) or not player_guid.strip():
        raise FuryExpertRuntimeSnapshotError(
            "static build capture lacks playerGUID"
        )

    observed = metadata.get("observed_character")
    observed_identity = observed.get("identity") if isinstance(observed, Mapping) else None
    if not isinstance(observed_identity, Mapping):
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata lacks observed_character.identity"
        )
    for field in ("classFile", "raceFile", "level"):
        if observed_identity.get(field) != identity.get(field):
            raise FuryExpertRuntimeSnapshotError(
                f"wowsims metadata observed identity differs from build capture on {field}"
            )
    counts = observed.get("static_counts")
    if not isinstance(counts, Mapping):
        raise FuryExpertRuntimeSnapshotError(
            "wowsims metadata lacks observed static_counts"
        )
    for field in ("equipment", "talents"):
        rows = state.get(field)
        if not isinstance(rows, list) or counts.get(field) != len(rows):
            raise FuryExpertRuntimeSnapshotError(
                f"wowsims metadata {field} count differs from build capture"
            )

    selection = ProfileSelection(
        path=capture_path,
        line_number=line_number,
        record=dict(capture),
        selection_mode="runtime_snapshot_exact_source_record",
    )
    try:
        recomputed_request, recomputed_metadata = build_wowsims_profile(
            profile, selection
        )
    except WowsimsProfileError as error:
        raise FuryExpertRuntimeSnapshotError(
            f"cannot recompute fixed build from its capture: {error}"
        ) from error
    try:
        supplied_player = profile["raid"]["parties"][0]["players"][0]
        recomputed_player = recomputed_request["raid"]["parties"][0]["players"][0]
        supplied_options = supplied_player["warrior"]["options"]
        recomputed_options = recomputed_player["warrior"]["options"]
    except (KeyError, IndexError, TypeError) as error:
        raise FuryExpertRuntimeSnapshotError(
            "wowsims profile lacks the first Warrior player static projection"
        ) from error
    for field in ("race", "class", "equipment", "talentsString"):
        if supplied_player.get(field) != recomputed_player.get(field):
            raise FuryExpertRuntimeSnapshotError(
                f"fixed build {field} differs from the exact static capture projection"
            )
    if supplied_options.get("ravagerRank") != recomputed_options.get("ravagerRank"):
        raise FuryExpertRuntimeSnapshotError(
            "fixed build ravagerRank differs from the exact static capture projection"
        )
    recomputed_mapped = recomputed_metadata.get("mapped")
    supplied_mapped = metadata.get("mapped")
    if not isinstance(recomputed_mapped, Mapping) or not isinstance(
        supplied_mapped, Mapping
    ):
        raise FuryExpertRuntimeSnapshotError("wowsims mapped metadata is malformed")
    for field in (
        "race",
        "class",
        "equipment_slots",
        "talents_string",
        "talent_trees",
    ):
        if supplied_mapped.get(field) != recomputed_mapped.get(field):
            raise FuryExpertRuntimeSnapshotError(
                f"wowsims mapped metadata differs from static capture on {field}"
            )

    semantic_capture = {
        "character_identity": dict(identity),
        "player_guid": player_guid,
        "equipment": state.get("equipment"),
        "talents": state.get("talents"),
    }
    context_core = {
        "realm_directory": realm_directory.name,
        "character_directory": character_directory.name,
        "player_guid": player_guid,
        "build_capture_semantic_sha256": sha256_json(semantic_capture),
    }
    return {
        "status": "BOUND_SAME_CHARACTER_DIRECTORY_AND_BUILD_CAPTURE",
        **context_core,
        "character_context_id": sha256_json(context_core),
        "savedvariables_parent_contract": (
            "CAT_AND_CONTRA_SHARE_EXACT_REALM_CHARACTER_SAVEDVARIABLES_PARENT"
        ),
        "build_capture_source": {
            "logical_path": str(metadata["source"]["calibration_jsonl"]),
            "line_number": line_number,
            "event": capture.get("event"),
            "sequence": capture.get("sequence"),
        },
    }


def capture_runtime_snapshot(
    *,
    cat_savedvariables: str | Path,
    contra_savedvariables: str | Path,
    wowsims_profile: str | Path = DEFAULT_WOWSIMS_PROFILE,
    wowsims_metadata: str | Path = DEFAULT_WOWSIMS_METADATA,
    config_wtf: str | Path,
    nampower_dll: str | Path,
    superwow_dll: str | Path,
    cat_manifest: str | Path = DEFAULT_CAT_MANIFEST,
) -> dict[str, Any]:
    """Read and bind all supplied mutable files into one deterministic record."""

    cat_bytes, cat_path = _stable_read(cat_savedvariables, "Cat SavedVariables")
    contra_bytes, contra_path = _stable_read(contra_savedvariables, "Contra SavedVariables")
    profile_bytes, _ = _stable_read(wowsims_profile, "wowsims profile")
    metadata_bytes, _ = _stable_read(wowsims_metadata, "wowsims metadata")
    config_bytes, _ = _stable_read(config_wtf, "Config.wtf")
    nampower_bytes, _ = _stable_read(nampower_dll, "nampower.dll")
    superwow_bytes, _ = _stable_read(superwow_dll, "SuperWoWhook.dll")

    manifest = load_cat_manifest(cat_manifest)
    cat = _cat_profile(_parse_savedvariables(cat_bytes, "Cat.lua"), manifest)
    contra = _contra_profile(
        _parse_savedvariables(
            contra_bytes,
            "Contra.lua",
            strip_contra_recursive_comment=True,
        )
    )
    profile = _strict_json(profile_bytes, "wowsims profile")
    metadata = _strict_json(metadata_bytes, "wowsims metadata")
    np_settings = _nampower_settings(config_bytes)
    character_context = _same_character_context(
        cat_path=cat_path,
        contra_path=contra_path,
        profile=profile,
        metadata=metadata,
    )

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "authority": {
            "state": "LOCAL_MUTABLE_RUNTIME_IDENTITY_CAPTURE",
            "source_execution_observed": False,
            "client_acceptance_observed": False,
            "server_outcome_observed": False,
            "full_policy_simulator_adapter_complete": False,
            "comparison_eligible": False,
        },
        "source_identity": {
            "cat_manifest_sha256": hashlib.sha256(
                Path(cat_manifest).expanduser().resolve(strict=True).read_bytes()
            ).hexdigest(),
            "cat_toc_closure_sha256": manifest["identity"]["toc_closure"]["sha256"],
        },
        "character_context": character_context,
        "inputs": {
            "cat_savedvariables": {
                "logical_path": "%WOW_CHARACTER_SAVEDVARIABLES%\\Cat.lua",
                "sha256": hashlib.sha256(cat_bytes).hexdigest(),
                "size_bytes": len(cat_bytes),
            },
            "contra_savedvariables": {
                "logical_path": "%WOW_CHARACTER_SAVEDVARIABLES%\\Contra.lua",
                "sha256": hashlib.sha256(contra_bytes).hexdigest(),
                "size_bytes": len(contra_bytes),
                "recursive_table_comments_stripped_for_literal_parse": contra_bytes.count(
                    CONTRA_RECURSIVE_COMMENT.encode("utf-8")
                ),
            },
            "wowsims_profile": {
                "logical_path": "%PROJECT_ROOT%\\configs\\wowsims\\fury_warrior_live.json",
                "sha256": hashlib.sha256(profile_bytes).hexdigest(),
                "size_bytes": len(profile_bytes),
            },
            "wowsims_metadata": {
                "logical_path": "%PROJECT_ROOT%\\configs\\wowsims\\fury_warrior_live.metadata.json",
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "size_bytes": len(metadata_bytes),
            },
            "config_wtf": {
                "logical_path": "%WOW_ROOT%\\WTF\\Config.wtf",
                "sha256": hashlib.sha256(config_bytes).hexdigest(),
                "size_bytes": len(config_bytes),
            },
            "nampower_dll": {
                "logical_path": "%WOW_ROOT%\\nampower.dll",
                "sha256": hashlib.sha256(nampower_bytes).hexdigest(),
                "size_bytes": len(nampower_bytes),
            },
            "superwow_hook_dll": {
                "logical_path": "%WOW_ROOT%\\SuperWoWhook.dll",
                "sha256": hashlib.sha256(superwow_bytes).hexdigest(),
                "size_bytes": len(superwow_bytes),
            },
        },
        "cat_profile1": cat,
        "contra_current_profile": contra,
        "fixed_character_build": _fixed_build(profile, metadata),
        "nampower_cvars": np_settings,
        "nampower_cvars_semantic_sha256": sha256_json(np_settings),
        "remaining_blockers": [
            "CLIENT_LOAD_OF_THE_PINNED_EXPERT_SOURCE_NOT_OBSERVED_IN_THIS_ARTIFACT",
            "ORDERED_SINK_CLIENT_ACCEPTANCE_TRACE_NOT_BOUND",
            "SERVER_OUTCOME_TRACE_NOT_BOUND",
            "FULL_SCENARIO_BOUND_SIMULATOR_ADAPTER_NOT_COMPLETE",
        ],
    }
    document["snapshot_sha256"] = sha256_json(document)
    return document


def write_content_addressed_snapshot(
    document: Mapping[str, Any], output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY
) -> Path:
    expected = document.get("snapshot_sha256")
    without_hash = dict(document)
    without_hash.pop("snapshot_sha256", None)
    if expected != sha256_json(without_hash):
        raise FuryExpertRuntimeSnapshotError("snapshot_sha256 does not match content")
    destination_directory = Path(output_directory).expanduser().resolve()
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / f"fury_expert_runtime_snapshot_v1.{expected}.json"
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != payload:
            raise FuryExpertRuntimeSnapshotError(
                f"content-addressed destination already differs: {destination}"
            )
        return destination
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination_directory
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cat-savedvariables", type=Path, required=True)
    parser.add_argument("--contra-savedvariables", type=Path, required=True)
    parser.add_argument("--wowsims-profile", type=Path, default=DEFAULT_WOWSIMS_PROFILE)
    parser.add_argument("--wowsims-metadata", type=Path, default=DEFAULT_WOWSIMS_METADATA)
    parser.add_argument("--config-wtf", type=Path, required=True)
    parser.add_argument("--nampower-dll", type=Path, required=True)
    parser.add_argument("--superwow-dll", type=Path, required=True)
    parser.add_argument("--cat-manifest", type=Path, default=DEFAULT_CAT_MANIFEST)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = capture_runtime_snapshot(
            cat_savedvariables=args.cat_savedvariables,
            contra_savedvariables=args.contra_savedvariables,
            wowsims_profile=args.wowsims_profile,
            wowsims_metadata=args.wowsims_metadata,
            config_wtf=args.config_wtf,
            nampower_dll=args.nampower_dll,
            superwow_dll=args.superwow_dll,
            cat_manifest=args.cat_manifest,
        )
        output = write_content_addressed_snapshot(document, args.output_directory)
    except (FuryExpertRuntimeSnapshotError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "snapshot_sha256": document["snapshot_sha256"],
                "output": str(output),
                "comparison_eligible": False,
                "remaining_blockers": document["remaining_blockers"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAT_PROFILE_KEYS",
    "CONTRA_REQUIRED_BUTTON_KEYS",
    "DEFAULT_OUTPUT_DIRECTORY",
    "FuryExpertRuntimeSnapshotError",
    "SCHEMA",
    "capture_runtime_snapshot",
    "sha256_json",
    "write_content_addressed_snapshot",
]
