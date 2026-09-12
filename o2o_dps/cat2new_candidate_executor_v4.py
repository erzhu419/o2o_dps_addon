"""Cat2 2026-09-10 candidate ActionPlan compiler and readiness gate v4.

``Cat2_new`` is the intended final action outlet for a learned policy.  It is
not an independent expert baseline.  This module keeps those roles separate
and compiles a candidate's ordered intents into an auditable, source-bound
plan without executing Lua or changing the third-party addon.

The compiler distinguishes three things which older projections commonly
collapsed:

* a pinned native Cat2 profile card;
* a pinned Cat2/core or WoW API that still needs a BrainOfCat dispatch card;
* an observed client sink and its later server outcome (neither exists here).

Target, item, equipment, next-swing, stance, cast-control, auto-attack, GCD,
off-GCD, and intentional WAIT intents all have explicit ordered semantics.
Every generated plan remains ``PLAN_ONLY_NOT_DISTILLED`` and non-runnable
until a separately versioned Lua dispatcher, exact client trace, simulator
executor coverage, and deployment evidence close the typed blockers.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .cat2_capability_manifest_v1 import (
    DEFAULT_MANIFEST,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_PARTITIONS,
    MANIFEST_ID,
    SOURCE_ID,
    compute_source_tree_facts,
    load_manifest,
    verify_source_tree,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PROJECT_ROOT.parent

REQUEST_SCHEMA = "cat2new_candidate_action_request/v4"
PLAN_SCHEMA = "cat2new_candidate_action_plan/v4"
READINESS_SCHEMA = "cat2new_candidate_executor_readiness/v4"
CONTRACT_ID = "cat2new.candidate.executor.contract.v4"
EXECUTOR_ID = "cat2new.candidate.executor.source_plan.v4"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"

DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("BOC_CAT2NEW_ROOT", str(BRAIN_ROOT / "Cat2_new"))
)
DEFAULT_INSTALLED_ROOT = Path(
    os.environ.get("BOC_CAT2_INSTALLED_ROOT", str(BRAIN_ROOT / "Cat2"))
)
DEFAULT_SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CAT2_SAVEDVARIABLES",
        str(PROJECT_ROOT / ".local" / "SavedVariables" / "Cat2.lua"),
    )
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class Cat2NewCandidateExecutorV4Error(RuntimeError):
    """A request, source identity, plan, or readiness invariant failed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }


def _stable_read(path: str | Path, label: str) -> tuple[bytes, Path]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise Cat2NewCandidateExecutorV4Error(f"{label} may not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise Cat2NewCandidateExecutorV4Error(f"{label} is not a file")
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except Cat2NewCandidateExecutorV4Error:
        raise
    except OSError as error:
        raise Cat2NewCandidateExecutorV4Error(
            f"could not read {label} {candidate}: {error}"
        ) from error
    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key or len(payload) != after.st_size:
        raise Cat2NewCandidateExecutorV4Error(f"{label} changed while being read")
    return payload, resolved


def _strict_json(payload: bytes, label: str) -> JSONMap:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> JSONMap:
        result: JSONMap = {}
        for key, value in pairs:
            if key in result:
                raise Cat2NewCandidateExecutorV4Error(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs_hook)
    except Cat2NewCandidateExecutorV4Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewCandidateExecutorV4Error(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise Cat2NewCandidateExecutorV4Error(f"{label} root must be an object")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateExecutorV4Error(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise Cat2NewCandidateExecutorV4Error(f"{label} must be an array")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} key mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} must match {_IDENTIFIER_RE.pattern!r}"
        )
    return value


def _nonempty(value: Any, label: str, *, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} must be a nonempty string of at most {maximum} characters"
        )
    return value


def _integer(
    value: Any, label: str, *, minimum: int, maximum: int
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} must be an integer in [{minimum},{maximum}]"
        )
    return value


def _number(
    value: Any, label: str, *, minimum: float, maximum: float
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Cat2NewCandidateExecutorV4Error(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} must be finite and in [{minimum},{maximum}]"
        )
    return value


def _option_number(minimum: float, maximum: float) -> JSONMap:
    return {"type": "number", "minimum": minimum, "maximum": maximum}


ACTION_CARDS: Mapping[str, JSONMap] = {
    "warrior.battle_shout": {
        "lane": "gcd",
        "card_id": "warrior_battle_shout",
        "relative_path": "Cards/Warrior/BattleShout.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["rage", "buffs.battle_shout"],
    },
    "warrior.battle_stance": {
        "lane": "stance",
        "card_id": "warrior_battle_stance",
        "relative_path": "Cards/Warrior/BattleStance.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_STANCE_CHANGE",
        "required_state": ["stance", "known_stance_forms"],
    },
    "warrior.berserker_stance": {
        "lane": "stance",
        "card_id": "warrior_berserker_stance",
        "relative_path": "Cards/Warrior/BerserkerStance.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_STANCE_CHANGE",
        "required_state": ["stance", "known_stance_forms"],
    },
    "warrior.bloodrage": {
        "lane": "off_gcd",
        "card_id": "warrior_bloodrage",
        "relative_path": "Cards/Warrior/Bloodrage.lua",
        "options": {"maximumRage": _option_number(1, 100)},
        "traversal": "CONTINUE_CURRENT_PASS_AFTER_SINK_ATTEMPT",
        "required_state": ["in_combat", "target.exists", "target.melee", "rage", "cooldowns.bloodrage"],
    },
    "warrior.bloodthirst": {
        "lane": "gcd",
        "card_id": "warrior_bloodthirst",
        "relative_path": "Cards/Warrior/Bloodthirst.lua",
        "options": {"rageThreshold": _option_number(30, 100)},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "rage", "cooldowns.bloodthirst"],
    },
    "warrior.death_wish": {
        "lane": "off_gcd",
        "card_id": "warrior_death_wish",
        "relative_path": "Cards/Warrior/DeathWish.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "target.melee", "rage", "cooldowns.death_wish"],
    },
    "warrior.defensive_stance": {
        "lane": "stance",
        "card_id": "warrior_defensive_stance",
        "relative_path": "Cards/Warrior/DefensiveStance.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_STANCE_CHANGE",
        "required_state": ["stance", "known_stance_forms"],
    },
    "warrior.execute": {
        "lane": "gcd",
        "card_id": "warrior_execute",
        "relative_path": "Cards/Warrior/Execute.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "target.health_pct", "rage", "stance", "talents.improved_execute"],
    },
    "warrior.hamstring": {
        "lane": "gcd",
        "card_id": "warrior_hamstring",
        "relative_path": "Cards/Warrior/Hamstring.lua",
        "options": {},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "target.debuffs.hamstring", "rage", "stance"],
    },
    "warrior.pummel": {
        "lane": "gcd",
        "card_id": "warrior_pummel",
        "relative_path": "Cards/Warrior/Pummel.lua",
        "options": {"interruptSpellName": {"type": "string", "maximum": 120}},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "target.cast", "rage", "stance", "cooldowns.pummel", "superwow"],
    },
    "warrior.slam": {
        "lane": "gcd",
        "card_id": "warrior_slam",
        "relative_path": "Cards/Warrior/Slam.lua",
        "options": {
            "minimumSwingTime": _option_number(0.1, 5),
            "rageThreshold": _option_number(15, 100),
        },
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "rage", "main_hand.swing_remaining"],
    },
    "warrior.sunder_armor": {
        "lane": "gcd",
        "card_id": "warrior_sunder_armor",
        "relative_path": "Cards/Warrior/SunderArmor.lua",
        "options": {"rageThreshold": _option_number(5, 100)},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "rage", "equipment.brotherhood_set_count"],
    },
    "warrior.whirlwind": {
        "lane": "gcd",
        "card_id": "warrior_whirlwind",
        "relative_path": "Cards/Warrior/Whirlwind.lua",
        "options": {"rageThreshold": _option_number(20, 100)},
        "traversal": "STOP_CURRENT_PASS_ON_MATCH",
        "required_state": ["target.exists", "target.distance", "rage", "stance", "cooldowns.whirlwind", "equipment.brotherhood_set_count"],
    },
}


QUEUE_CARDS: Mapping[str, JSONMap] = {
    "HEROIC_STRIKE": {
        "card_id": "warrior_heroic_strike",
        "relative_path": "Cards/Warrior/HeroicStrike.lua",
        "options": {"rageThreshold": _option_number(1, 100)},
        "sink": "Cat2.Cast(英勇打击)",
    },
    "CLEAVE": {
        "card_id": "warrior_cleave",
        "relative_path": "Cards/Warrior/Cleave.lua",
        "options": {"rageThreshold": _option_number(1, 100)},
        "sink": "Cat2.Cast(顺劈斩)",
    },
    "AUTO_HS_OR_CLEAVE": {
        "card_id": "warrior_heroic_strike_alt",
        "relative_path": "Cards/Warrior/HeroicStrikeAlt.lua",
        "options": {"rageThreshold": _option_number(1, 100)},
        "sink": "Cat2.Cast(顺劈斩|英勇打击)",
    },
}

PINNED_ROUTE_FILES: Mapping[str, str] = {
    **{spec["card_id"]: spec["relative_path"] for spec in ACTION_CARDS.values()},
    **{spec["card_id"]: spec["relative_path"] for spec in QUEUE_CARDS.values()},
    "common_auto_attack": "Cards/Common/AutoAttack.lua",
    "common_auto_target": "Cards/Common/AutoTarget.lua",
    "common_auto_trinket_upper": "Cards/Common/AutoTrinketUpper.lua",
    "common_auto_trinket_lower": "Cards/Common/AutoTrinketLower.lua",
    "common_burst_auto_trinket_upper": "Cards/Common/BurstAutoTrinketUpper.lua",
    "common_burst_auto_trinket_lower": "Cards/Common/BurstAutoTrinketLower.lua",
}

PINNED_CORE_API_SITES: Mapping[str, str] = {
    "Cat2.StopAttack": "Cards/Common/AutoAttack.lua",
    "Cat2.UseItemByName": "Core/CatLib.lua",
    "Cat2.UseItemByNameToSelf": "Core/CatLib.lua",
    "Cat2.EquipItemByName": "Core/CatLib.lua",
    "Cat2.WarriorCancelHeroic": "Core/CatEvent-Warrior.lua",
}

# These cards are valuable source-derived search templates, but their built-in
# heuristics are not the learned candidate itself.  In particular, the nearby
# Execute card's Lua ``pairs`` order cannot replace an explicit target choice.
PINNED_SEARCH_TEMPLATE_FILES: Mapping[str, str] = {
    "warrior_slam_after_main_skills": "Cards/Warrior/SlamAfterMainSkills.lua",
    "warrior_slam_flurry": "Cards/Warrior/SlamFlurry.lua",
    "warrior_overpower_after_main_skill": "Cards/Warrior/OverpowerAfterMainSkill.lua",
    "warrior_pummel_flurry": "Cards/Warrior/PummelFlurry.lua",
    "warrior_cleave_front_only": "Cards/Warrior/CleaveFrontOnly.lua",
    "warrior_whirlwind_group": "Cards/Warrior/WhirlwindGroup.lua",
    "warrior_execute_nearby_target": "Cards/Warrior/ExecuteNearbyTarget.lua",
    "item_great_rage_potion": "Cards/Items/GreatRagePotion.lua",
    "item_haste_potion": "Cards/Items/HastePotion.lua",
    "item_juju_flurry": "Cards/Items/JujuFlurry.lua",
    "item_goblin_sapper_bomb": "Cards/Items/GoblinSapperBomb.lua",
}


LANE_INTENTS: Mapping[str, tuple[str, ...]] = {
    "target": ("KEEP", "AUTO_NEAREST_6", "SET_EXACT_UNIT"),
    "autoattack": ("START", "STOP"),
    "item": ("USE_TRINKET_SLOT", "USE_NAMED_ITEM"),
    "equipment": ("EQUIP_NAMED_SLOT",),
    "swing_queue": ("KEEP", "HEROIC_STRIKE", "CLEAVE", "AUTO_HS_OR_CLEAVE", "CANCEL"),
    "cast_control": ("KEEP", "STOP_CAST"),
    "stance": ("KEEP", "CAST_ACTION"),
    "off_gcd": ("CAST_ACTION",),
    "gcd": ("CAST_ACTION",),
    "wait": ("WAIT",),
}


REQUIRED_STATE_FIELDS_BY_LANE: Mapping[str, tuple[str, ...]] = {
    "target": ("target.guid", "target.exists", "target.attackable", "target.dead", "nearby_targets[].guid", "nearby_targets[].distance", "nearby_targets[].attackable"),
    "autoattack": ("autoattack.active", "autoattack.locked", "target.guid", "target.attackable"),
    "item": ("inventory[].item_id", "inventory[].item_name", "inventory[].bag_slot", "inventory[].count", "item_cooldowns", "shared_cooldowns", "in_combat"),
    "equipment": ("equipment.slots", "inventory[].item_id", "inventory[].item_name", "in_combat", "cursor.empty"),
    "swing_queue": ("queued_swing.intent", "queued_swing.accepted", "queued_swing.active", "rage", "main_hand.swing_remaining"),
    "cast_control": ("casting.active", "casting.spell_id", "casting.ends_at"),
    "stance": ("stance", "rage", "talents.tactical_mastery"),
    "off_gcd": ("rage", "cooldowns", "buffs", "target.guid", "target.distance"),
    "gcd": ("rage", "gcd", "cooldowns", "buffs", "debuffs", "target.guid", "target.health", "target.max_health", "target.distance", "main_hand.swing_remaining"),
    "wait": ("decision_time", "next_event_time", "gcd", "cooldowns", "main_hand.swing_remaining"),
}


CONTRACT: JSONMap = {
    "contract_id": CONTRACT_ID,
    "executor_id": EXECUTOR_ID,
    "source": {
        "manifest_id": MANIFEST_ID,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "source_id": SOURCE_ID,
        "tree_sha256": EXPECTED_PARTITIONS["tree"]["sha256"],
    },
    "role": {
        "policy_role": "CANDIDATE",
        "execution_role": "FINAL_ACTION_OUTLET_CANDIDATE",
        "baseline": False,
        "eligible_for_independent_vote": False,
    },
    "ordered_lanes": list(LANE_INTENTS),
    "lane_intents": {key: list(value) for key, value in LANE_INTENTS.items()},
    "action_cards": deepcopy(dict(ACTION_CARDS)),
    "queue_cards": deepcopy(dict(QUEUE_CARDS)),
    "pinned_route_files": dict(PINNED_ROUTE_FILES),
    "pinned_core_api_sites": dict(PINNED_CORE_API_SITES),
    "pinned_search_template_files": dict(PINNED_SEARCH_TEMPLATE_FILES),
    "required_state_fields_by_lane": {
        key: list(value) for key, value in REQUIRED_STATE_FIELDS_BY_LANE.items()
    },
    "semantic_invariants": [
        "candidate_intent_is_not_cat2_source_policy",
        "card_return_is_traversal_not_client_acceptance",
        "sink_attempt_is_not_client_acceptance",
        "client_acceptance_is_not_server_outcome",
        "ordered_operations_must_not_be_factorized_then_reordered",
        "intentional_wait_terminates_the_current_candidate_plan",
        "exact_target_intent_must_not_use_unspecified_lua_pairs_order",
        "equipment_change_requires_observed_postcondition_and_runtime_refresh",
        "queue_intent_acceptance_and_active_state_are_distinct",
        "cat2new_is_not_an_independent_expert_baseline",
    ],
}
CONTRACT_SHA256 = hashlib.sha256(_canonical_bytes(CONTRACT)).hexdigest()


MANDATORY_BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("ARCHIVE_BYTES_UNAVAILABLE", "SOURCE", "the requested ZIP bytes were not available; the extracted tree alone is pinned"),
    ("CAT2_NEW_CLIENT_LOAD_NOT_ATTESTED", "DEPLOYMENT", "the candidate source tree has no post-/reload client load receipt"),
    ("CAT2_NEW_DEPLOYED_PROFILE_NOT_CAPTURED", "PROFILE", "the existing Cat2 SavedVariables belongs to the predecessor source and has not been migrated to this tree identity"),
    ("CANDIDATE_ACTION_PLAN_NOT_DISTILLED", "EXECUTOR", "no versioned Lua dispatcher implements this v4 ActionPlan contract"),
    ("ORDERED_CLIENT_SINK_TRACE_MISSING", "RUNTIME", "no Cat2_new ordered sink-attempt trace is bound to this contract"),
    ("CLIENT_ACCEPTANCE_TRACE_MISSING", "RUNTIME", "card traversal or API invocation cannot establish client acceptance"),
    ("SERVER_OUTCOME_TRACE_MISSING", "RUNTIME", "accepted actions are not joined to terminal server outcomes"),
    ("FORMAL_SIMULATOR_EXECUTOR_COVERAGE_OPEN", "SIMULATOR", "the formal ordered executor does not yet cover target, item, equipment, cancellation, and WAIT sinks under this contract"),
)


def audit_contract_source_mapping_v4(source_root: str | Path) -> JSONMap:
    """Verify that every v4 route names the exact card/function in the pin."""

    root = Path(source_root).expanduser().resolve(strict=True)
    if root.name != "Cat2_new" or not root.is_dir():
        raise Cat2NewCandidateExecutorV4Error(
            "source mapping audit requires the external Cat2_new directory"
        )
    rows: list[JSONMap] = []
    for card_id, relative_path in sorted(PINNED_ROUTE_FILES.items()):
        payload, _ = _stable_read(root / relative_path, f"Cat2_new/{relative_path}")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} is not UTF-8: {error}"
            ) from error
        ids = re.findall(r'^\s*id\s*=\s*"([^"]+)"', source, re.MULTILINE)
        if ids != [card_id]:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} expected sole card ID {card_id!r}, got {ids}"
            )
        rows.append(
            {
                "kind": "CARD",
                "symbol": card_id,
                "relative_path": relative_path,
                "file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    for symbol, relative_path in sorted(PINNED_CORE_API_SITES.items()):
        payload, _ = _stable_read(root / relative_path, f"Cat2_new/{relative_path}")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} is not UTF-8: {error}"
            ) from error
        pattern = rf"function\s+{re.escape(symbol)}\s*\("
        if len(re.findall(pattern, source)) != 1:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} expected one definition of {symbol}"
            )
        rows.append(
            {
                "kind": "CORE_API",
                "symbol": symbol,
                "relative_path": relative_path,
                "file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    for card_id, relative_path in sorted(PINNED_SEARCH_TEMPLATE_FILES.items()):
        payload, _ = _stable_read(root / relative_path, f"Cat2_new/{relative_path}")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} is not UTF-8: {error}"
            ) from error
        ids = re.findall(r'^\s*id\s*=\s*"([^"]+)"', source, re.MULTILINE)
        if ids != [card_id]:
            raise Cat2NewCandidateExecutorV4Error(
                f"Cat2_new/{relative_path} expected sole search-template card ID {card_id!r}, got {ids}"
            )
        rows.append(
            {
                "kind": "SEARCH_TEMPLATE_CARD",
                "symbol": card_id,
                "relative_path": relative_path,
                "file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "status": "PASS",
        "card_route_count": len(PINNED_ROUTE_FILES),
        "core_api_count": len(PINNED_CORE_API_SITES),
        "search_template_count": len(PINNED_SEARCH_TEMPLATE_FILES),
        "rows_sha256": hashlib.sha256(_canonical_bytes(rows)).hexdigest(),
        "rows": rows,
    }


def _normalize_options(
    value: Any, specs: Mapping[str, Mapping[str, Any]], label: str
) -> JSONMap:
    raw = _mapping(value, label)
    unknown = sorted(set(raw) - set(specs))
    if unknown:
        raise Cat2NewCandidateExecutorV4Error(
            f"{label} has unknown keys {unknown}"
        )
    normalized: JSONMap = {}
    for key in sorted(raw):
        spec = specs[key]
        option_label = f"{label}.{key}"
        if spec["type"] == "number":
            normalized[key] = _number(
                raw[key],
                option_label,
                minimum=float(spec["minimum"]),
                maximum=float(spec["maximum"]),
            )
        elif spec["type"] == "string":
            if not isinstance(raw[key], str) or len(raw[key]) > int(spec["maximum"]):
                raise Cat2NewCandidateExecutorV4Error(
                    f"{option_label} must be a string of at most {spec['maximum']} characters"
                )
            normalized[key] = raw[key]
        else:
            raise AssertionError(f"unknown option type {spec['type']!r}")
    return normalized


def _normalize_arguments(
    lane: str, intent: str, raw_value: Any, label: str
) -> JSONMap:
    raw = _mapping(raw_value, label)
    if intent in {"KEEP", "START", "STOP", "CANCEL", "STOP_CAST"}:
        _keys(raw, set(), label)
        return {}
    if intent == "AUTO_NEAREST_6":
        _keys(raw, set(), label)
        return {}
    if intent == "SET_EXACT_UNIT":
        _keys(raw, {"unit", "restore_after"}, label)
        if not isinstance(raw["restore_after"], bool):
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.restore_after must be boolean"
            )
        return {
            "unit": _nonempty(raw["unit"], f"{label}.unit"),
            "restore_after": raw["restore_after"],
        }
    if intent == "USE_TRINKET_SLOT":
        _keys(raw, {"slot", "burst_only"}, label)
        slot = _integer(raw["slot"], f"{label}.slot", minimum=13, maximum=14)
        if not isinstance(raw["burst_only"], bool):
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.burst_only must be boolean"
            )
        return {"slot": slot, "burst_only": raw["burst_only"]}
    if intent == "USE_NAMED_ITEM":
        _keys(raw, {"item_name", "self_target"}, label)
        if not isinstance(raw["self_target"], bool):
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.self_target must be boolean"
            )
        return {
            "item_name": _nonempty(raw["item_name"], f"{label}.item_name"),
            "self_target": raw["self_target"],
        }
    if intent == "EQUIP_NAMED_SLOT":
        _keys(raw, {"item_name", "slot"}, label)
        return {
            "item_name": _nonempty(raw["item_name"], f"{label}.item_name"),
            "slot": _integer(raw["slot"], f"{label}.slot", minimum=1, maximum=19),
        }
    if intent in QUEUE_CARDS:
        _keys(raw, {"options"}, label)
        return {
            "options": _normalize_options(
                raw["options"], QUEUE_CARDS[intent]["options"], f"{label}.options"
            )
        }
    if intent == "CAST_ACTION":
        _keys(raw, {"action_key", "options"}, label)
        action_key = _nonempty(raw["action_key"], f"{label}.action_key")
        if action_key not in ACTION_CARDS:
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.action_key {action_key!r} is not in the pinned candidate action surface"
            )
        expected_lane = ACTION_CARDS[action_key]["lane"]
        if expected_lane != lane:
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.action_key {action_key!r} belongs to lane {expected_lane!r}, not {lane!r}"
            )
        return {
            "action_key": action_key,
            "options": _normalize_options(
                raw["options"], ACTION_CARDS[action_key]["options"], f"{label}.options"
            ),
        }
    if intent == "WAIT":
        _keys(raw, {"wait_ms"}, label)
        return {
            "wait_ms": _integer(
                raw["wait_ms"], f"{label}.wait_ms", minimum=1, maximum=60_000
            )
        }
    raise AssertionError(f"unhandled {lane}:{intent}")


def _normalize_request(request: Any) -> JSONMap:
    root = _mapping(request, "request")
    _keys(
        root,
        {
            "schema",
            "plan_id",
            "candidate_policy_id",
            "state_snapshot_sha256",
            "operations",
        },
        "request",
    )
    if root["schema"] != REQUEST_SCHEMA:
        raise Cat2NewCandidateExecutorV4Error(
            f"request.schema must be {REQUEST_SCHEMA!r}"
        )
    plan_id = _identifier(root["plan_id"], "request.plan_id")
    policy_id = _identifier(root["candidate_policy_id"], "request.candidate_policy_id")
    snapshot_sha = root["state_snapshot_sha256"]
    if not isinstance(snapshot_sha, str) or _SHA256_RE.fullmatch(snapshot_sha) is None:
        raise Cat2NewCandidateExecutorV4Error(
            "request.state_snapshot_sha256 must be a lowercase SHA-256"
        )
    raw_operations = _sequence(root["operations"], "request.operations")
    if not raw_operations or len(raw_operations) > 64:
        raise Cat2NewCandidateExecutorV4Error(
            "request.operations must contain between 1 and 64 operations"
        )

    operations: list[JSONMap] = []
    seen_ids: set[str] = set()
    wait_ordinals: list[int] = []
    lane_counts: dict[str, int] = {}
    exact_target_count = 0
    for index, value in enumerate(raw_operations):
        label = f"request.operations[{index}]"
        operation = _mapping(value, label)
        _keys(operation, {"operation_id", "lane", "intent", "arguments"}, label)
        operation_id = _identifier(operation["operation_id"], f"{label}.operation_id")
        if operation_id in seen_ids:
            raise Cat2NewCandidateExecutorV4Error(
                f"{label}.operation_id is duplicated"
            )
        seen_ids.add(operation_id)
        lane = operation["lane"]
        intent = operation["intent"]
        if lane not in LANE_INTENTS or intent not in LANE_INTENTS[lane]:
            raise Cat2NewCandidateExecutorV4Error(
                f"{label} unsupported lane/intent {lane!r}/{intent!r}"
            )
        arguments = _normalize_arguments(lane, intent, operation["arguments"], f"{label}.arguments")
        operations.append(
            {
                "operation_id": operation_id,
                "lane": lane,
                "intent": intent,
                "arguments": arguments,
            }
        )
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
        if intent == "WAIT":
            wait_ordinals.append(index + 1)
        if lane == "target" and intent == "SET_EXACT_UNIT":
            exact_target_count += 1

    if wait_ordinals and wait_ordinals != [len(operations)]:
        raise Cat2NewCandidateExecutorV4Error(
            "intentional WAIT must be the final and only WAIT operation"
        )
    for lane in ("gcd", "stance", "swing_queue", "wait"):
        if lane_counts.get(lane, 0) > 1:
            raise Cat2NewCandidateExecutorV4Error(
                f"request.operations contains more than one {lane!r} lane decision"
            )
    if exact_target_count > 1:
        raise Cat2NewCandidateExecutorV4Error(
            "request.operations supports at most one exact target binding"
        )

    return {
        "schema": REQUEST_SCHEMA,
        "plan_id": plan_id,
        "candidate_policy_id": policy_id,
        "state_snapshot_sha256": snapshot_sha,
        "operations": operations,
    }


def _native_card_route(
    *, card_id: str, relative_path: str, options: Mapping[str, Any], traversal: str,
    sinks: Sequence[str], required_state: Sequence[str],
    internal_bookkeeping: Sequence[str] = (),
    required_postconditions: Sequence[str] = (),
) -> JSONMap:
    return {
        "support_level": "PINNED_NATIVE_PROFILE_CARD",
        "entry": "Cat2.ExecuteConfiguration.PROFILE_STEP",
        "card_id": card_id,
        "relative_path": relative_path,
        "step": {"option_values": deepcopy(dict(options))},
        "possible_ordered_sinks": list(sinks),
        "internal_bookkeeping": list(internal_bookkeeping),
        "required_postconditions": list(required_postconditions),
        "traversal_if_gate_matches": traversal,
        "required_state_fields": list(required_state),
        "sink_attempt_observed": False,
        "client_acceptance_observed": False,
        "server_outcome_observed": False,
    }


def _extension_route(
    *, call: str, relative_path: str | None, sinks: Sequence[str],
    required_state: Sequence[str], traversal: str = "EXTERNAL_DISPATCHER_MUST_DEFINE",
    internal_bookkeeping: Sequence[str] = (),
    required_postconditions: Sequence[str] = (),
) -> JSONMap:
    return {
        "support_level": "PINNED_SOURCE_API_REQUIRES_BRAIN_EXTENSION",
        "entry": "UNIMPLEMENTED_BRAINOF_CAT_V4_DISPATCH_CARD",
        "core_call": call,
        "relative_path": relative_path,
        "possible_ordered_sinks": list(sinks),
        "internal_bookkeeping": list(internal_bookkeeping),
        "required_postconditions": list(required_postconditions),
        "traversal_if_gate_matches": traversal,
        "required_state_fields": list(required_state),
        "sink_attempt_observed": False,
        "client_acceptance_observed": False,
        "server_outcome_observed": False,
    }


def _compile_operation(operation: Mapping[str, Any]) -> tuple[JSONMap, list[str]]:
    lane = operation["lane"]
    intent = operation["intent"]
    arguments = operation["arguments"]
    blockers: list[str] = []

    if intent == "KEEP":
        route = {
            "support_level": "NO_SINK_BY_DESIGN",
            "entry": None,
            "possible_ordered_sinks": [],
            "internal_bookkeeping": [],
            "required_postconditions": [],
            "traversal_if_gate_matches": "CONTINUE",
            "required_state_fields": list(REQUIRED_STATE_FIELDS_BY_LANE[lane]),
            "sink_attempt_observed": False,
            "client_acceptance_observed": False,
            "server_outcome_observed": False,
        }
    elif lane == "target" and intent == "AUTO_NEAREST_6":
        route = _native_card_route(
            card_id="common_auto_target",
            relative_path="Cards/Common/AutoTarget.lua",
            options={},
            traversal="CONTINUE_CURRENT_PASS",
            sinks=("TargetUnit|TargetNearestEnemy|ClearTarget (state-dependent)",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
        )
    elif lane == "target" and intent == "SET_EXACT_UNIT":
        route = _extension_route(
            call="TargetUnit + observed GUID verification",
            relative_path=None,
            sinks=("TargetUnit(requested unit)",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            internal_bookkeeping=("capture current target GUID or absence",),
            required_postconditions=("selected target GUID equals requested unit", "Cat2 target-dependent temporary snapshot refreshed before the next BODY operation"),
        )
        blockers.extend(("EXACT_TARGET_DISPATCH_CARD_MISSING", "EXACT_TARGET_POSTCONDITION_NOT_OBSERVED", "TARGET_STATE_REFRESH_NOT_IMPLEMENTED"))
    elif lane == "autoattack" and intent == "START":
        route = _native_card_route(
            card_id="common_auto_attack",
            relative_path="Cards/Common/AutoAttack.lua",
            options={},
            traversal="CONTINUE_CURRENT_PASS",
            sinks=("AttackTarget (only when Cat2 autoattack guard opens)",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
        )
    elif lane == "autoattack" and intent == "STOP":
        route = _extension_route(
            call="Cat2.StopAttack",
            relative_path="Cards/Common/AutoAttack.lua",
            sinks=("Cat2.Cast(攻击) when Cat2.AutoAttack is true",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            required_postconditions=("autoattack inactive or explicit rejection",),
        )
        blockers.append("STOP_AUTOATTACK_DISPATCH_CARD_MISSING")
    elif lane == "item" and intent == "USE_TRINKET_SLOT":
        slot = arguments["slot"]
        suffix = "upper" if slot == 13 else "lower"
        prefix = "burst_auto" if arguments["burst_only"] else "auto"
        camel_prefix = "BurstAuto" if arguments["burst_only"] else "Auto"
        route = _native_card_route(
            card_id=f"common_{prefix}_trinket_{suffix}",
            relative_path=f"Cards/Common/{camel_prefix}Trinket{suffix.title()}.lua",
            options={},
            traversal="CONTINUE_CURRENT_PASS_AFTER_ATTEMPT",
            sinks=(f"UseInventoryItem({slot})",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            required_postconditions=("slot cooldown/count or typed rejection observed",),
        )
        blockers.append("ITEM_CLIENT_POSTCONDITION_NOT_OBSERVED")
    elif lane == "item" and intent == "USE_NAMED_ITEM":
        call = "Cat2.UseItemByNameToSelf" if arguments["self_target"] else "Cat2.UseItemByName"
        sinks = (
            "TargetUnit(player)", "UseContainerItem(first name match)", "TargetUnit(saved GUID)|ClearTarget"
        ) if arguments["self_target"] else ("UseContainerItem(first name match)",)
        route = _extension_route(
            call=call,
            relative_path="Core/CatLib.lua",
            sinks=sinks,
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            internal_bookkeeping=("save current target before self-target use",) if arguments["self_target"] else (),
            required_postconditions=("item count/cooldown transition or typed rejection observed", "original target restored when self_target is true"),
        )
        blockers.extend(("NAMED_ITEM_DISPATCH_CARD_MISSING", "ITEM_CLIENT_POSTCONDITION_NOT_OBSERVED"))
    elif lane == "equipment" and intent == "EQUIP_NAMED_SLOT":
        route = _extension_route(
            call="Cat2.EquipItemByName",
            relative_path="Core/CatLib.lua",
            sinks=("PickupContainerItem(first name match)", f"EquipCursorItem({arguments['slot']})"),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            required_postconditions=("requested item identity observed in requested equipment slot", "cursor state observed", "equipment-dependent card and candidate state refreshed"),
        )
        blockers.extend(("WARRIOR_EQUIPMENT_DISPATCH_CARD_MISSING", "EQUIPMENT_POSTCONDITION_NOT_OBSERVED", "EQUIPMENT_DEPENDENCY_REFRESH_NOT_IMPLEMENTED"))
    elif lane == "swing_queue" and intent in QUEUE_CARDS:
        spec = QUEUE_CARDS[intent]
        route = _native_card_route(
            card_id=spec["card_id"],
            relative_path=spec["relative_path"],
            options=arguments["options"],
            traversal="CONTINUE_CURRENT_PASS_AFTER_ATTEMPT",
            sinks=(spec["sink"],),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            required_postconditions=("requested/accepted/active/replaced/consumed queue states remain distinct",),
        )
        blockers.append("QUEUE_ACCEPTED_VS_ACTIVE_STATE_NOT_OBSERVED")
    elif lane == "swing_queue" and intent == "CANCEL":
        route = _extension_route(
            call="Cat2.WarriorCancelHeroic",
            relative_path="Core/CatEvent-Warrior.lua",
            sinks=("ClearTarget", "TargetUnit(saved GUID)", "Cat2.StartAttack (state-dependent)"),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            required_postconditions=("next-swing queue is inactive or explicit rejection observed", "target identity restored"),
        )
        blockers.extend(("QUEUE_CANCEL_DISPATCH_CARD_MISSING", "QUEUE_CANCEL_POSTCONDITION_NOT_OBSERVED"))
    elif lane == "cast_control" and intent == "STOP_CAST":
        route = _extension_route(
            call="SpellStopCasting",
            relative_path=None,
            sinks=("SpellStopCasting",),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
        )
        blockers.append("GENERIC_STOP_CAST_DISPATCH_CARD_MISSING")
    elif intent == "CAST_ACTION":
        spec = ACTION_CARDS[arguments["action_key"]]
        possible_sinks = (
            "CastShapeshiftForm|Cat2.Cast",
        ) if lane == "stance" else ("Cat2.Cast -> possible CastSpellByName",)
        route = _native_card_route(
            card_id=spec["card_id"],
            relative_path=spec["relative_path"],
            options=arguments["options"],
            traversal=spec["traversal"],
            sinks=possible_sinks,
            required_state=spec["required_state"],
        )
    elif lane == "wait" and intent == "WAIT":
        route = _extension_route(
            call=f"return true without a client sink; scheduler waits {arguments['wait_ms']} ms",
            relative_path=None,
            sinks=(),
            required_state=REQUIRED_STATE_FIELDS_BY_LANE[lane],
            traversal="STOP_CURRENT_CONFIGURATION_PASS_WITH_NO_FALLBACK",
        )
        blockers.extend(("DYNAMIC_WAIT_TERMINATION_CARD_MISSING", "WAIT_SCHEDULER_RUNTIME_NOT_IMPLEMENTED"))
    else:
        raise AssertionError(f"unhandled normalized operation {lane}:{intent}")

    return (
        {
            "operation_id": operation["operation_id"],
            "lane": lane,
            "intent": intent,
            "arguments": deepcopy(dict(arguments)),
            "route": route,
        },
        blockers,
    )


def _typed_blockers(codes: Sequence[str]) -> list[JSONMap]:
    messages: Mapping[str, str] = {
        "EXACT_TARGET_DISPATCH_CARD_MISSING": "exact target selection needs a versioned external card; AutoTarget or ExecuteNearbyTarget is not an equivalent substitute",
        "EXACT_TARGET_POSTCONDITION_NOT_OBSERVED": "TargetUnit is silent and the selected GUID must be checked before continuing",
        "TARGET_STATE_REFRESH_NOT_IMPLEMENTED": "after an exact target change, Cat2 target-dependent temporary state must refresh before any dependent action",
        "TARGET_RESTORE_FINALLY_DISPATCH_NOT_IMPLEMENTED": "temporary-target restoration must run even after a stopping card or WAIT, but the required dispatcher finally block is not implemented",
        "STOP_AUTOATTACK_DISPATCH_CARD_MISSING": "Cat2.StopAttack exists but has no pinned native profile card",
        "ITEM_CLIENT_POSTCONDITION_NOT_OBSERVED": "a UseInventoryItem/UseContainerItem call does not prove consumption or cooldown transition",
        "NAMED_ITEM_DISPATCH_CARD_MISSING": "Cat2.UseItemByName exists but has no generic pinned profile card",
        "WARRIOR_EQUIPMENT_DISPATCH_CARD_MISSING": "Cat2.EquipItemByName has no Warrior candidate dispatch card",
        "EQUIPMENT_POSTCONDITION_NOT_OBSERVED": "Cat2.EquipItemByName returns after EquipCursorItem without verifying the equipped item",
        "EQUIPMENT_DEPENDENCY_REFRESH_NOT_IMPLEMENTED": "equipment-dependent card caches and the candidate state must refresh after a confirmed swap",
        "QUEUE_ACCEPTED_VS_ACTIVE_STATE_NOT_OBSERVED": "the queue card call does not distinguish requested, accepted, active, replaced, and consumed states",
        "QUEUE_CANCEL_DISPATCH_CARD_MISSING": "Cat2.WarriorCancelHeroic exists but has no pinned native profile card",
        "QUEUE_CANCEL_POSTCONDITION_NOT_OBSERVED": "the source cancel helper does not directly report the resulting queue state",
        "GENERIC_STOP_CAST_DISPATCH_CARD_MISSING": "SpellStopCasting is only reachable through conditional source paths, not a generic candidate card",
        "DYNAMIC_WAIT_TERMINATION_CARD_MISSING": "the current PolicyBrain returns false for an empty plan, so later fallback cards can erase WAIT",
        "WAIT_SCHEDULER_RUNTIME_NOT_IMPLEMENTED": "no runtime scheduler binds wait_ms to the next eligible decision time",
    }
    seen: set[str] = set()
    rows: list[JSONMap] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        rows.append(
            {
                "code": code,
                "scope": "PLAN_OPERATION",
                "message": messages[code],
                "runtime_fatal": True,
            }
        )
    return rows


def compile_candidate_action_plan_v4(
    request: Any,
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    verify_live_source: bool = True,
) -> JSONMap:
    """Compile one candidate decision into a deterministic non-runnable plan."""

    normalized = _normalize_request(request)
    load_manifest(manifest_path)
    if verify_live_source:
        source_verification = verify_source_tree(source_root, manifest_path)
        source_mapping_audit = audit_contract_source_mapping_v4(source_root)
        source_status = "VERIFIED_EXTRACTED_TREE"
    else:
        source_verification = {"status": "SKIPPED_BY_CALLER_NONPROMOTING"}
        source_mapping_audit = {"status": "SKIPPED_BY_CALLER_NONPROMOTING"}
        source_status = "NOT_VERIFIED"

    compiled: list[JSONMap] = []
    finally_schedule: list[JSONMap] = []
    operation_blocker_codes: list[str] = []
    deferred_restores: list[Mapping[str, Any]] = []
    for ordinal, operation in enumerate(normalized["operations"], start=1):
        row, blockers = _compile_operation(operation)
        row["ordinal"] = ordinal
        row["compiler_generated"] = False
        compiled.append(row)
        operation_blocker_codes.extend(blockers)
        if (
            operation["lane"] == "target"
            and operation["intent"] == "SET_EXACT_UNIT"
            and operation["arguments"]["restore_after"] is True
        ):
            deferred_restores.append(operation)

    for source_operation in deferred_restores:
        finally_schedule.append(
            {
                "operation_id": f"{source_operation['operation_id']}.restore",
                "lane": "target",
                "intent": "RESTORE_CAPTURED_TARGET",
                "arguments": {
                    "captured_by_operation_id": source_operation["operation_id"]
                },
                "route": _extension_route(
                    call="TargetUnit(saved GUID)|ClearTarget + observed restoration verification",
                    relative_path=None,
                    sinks=(
                        "TargetUnit(saved GUID)|ClearTarget",
                    ),
                    required_state=REQUIRED_STATE_FIELDS_BY_LANE["target"],
                    required_postconditions=("restored target GUID or absence equals captured value",),
                ),
                "ordinal": len(finally_schedule) + 1,
                "compiler_generated": True,
                "execution_region": "FINALLY",
            }
        )
    for ordinal, row in enumerate(compiled, start=1):
        row["ordinal"] = ordinal
        row["execution_region"] = "BODY"
    if finally_schedule:
        operation_blocker_codes.append(
            "TARGET_RESTORE_FINALLY_DISPATCH_NOT_IMPLEMENTED"
        )

    early_barriers = [
        row["ordinal"]
        for row in compiled[:-1]
        if str(row["route"]["traversal_if_gate_matches"]).startswith("STOP_")
    ]
    extension_ordinals = [
        row["ordinal"]
        for row in compiled
        if row["route"]["support_level"]
        == "PINNED_SOURCE_API_REQUIRES_BRAIN_EXTENSION"
    ]
    finally_extension_ordinals = [
        row["ordinal"]
        for row in finally_schedule
        if row["route"]["support_level"]
        == "PINNED_SOURCE_API_REQUIRES_BRAIN_EXTENSION"
    ]
    operation_blockers = _typed_blockers(operation_blocker_codes)

    core: JSONMap = {
        "schema": PLAN_SCHEMA,
        "request": normalized,
        "identity": {
            "executor_id": EXECUTOR_ID,
            "contract_id": CONTRACT_ID,
            "contract_sha256": CONTRACT_SHA256,
            "capability_manifest_id": MANIFEST_ID,
            "capability_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "source_id": SOURCE_ID,
            "source_tree_sha256": EXPECTED_PARTITIONS["tree"]["sha256"],
            "source_status": source_status,
            "source_verification": source_verification,
            "contract_source_mapping_audit": source_mapping_audit,
        },
        "role": {
            "candidate_policy_id": normalized["candidate_policy_id"],
            "policy_role": "CANDIDATE",
            "execution_role": "FINAL_ACTION_OUTLET_CANDIDATE",
            "baseline": False,
            "eligible_for_independent_vote": False,
        },
        "evidence_layers": {
            "candidate_intent": "CALLER_SUPPLIED_CONTENT_ADDRESSED_STATE_BINDING",
            "cat2_source_semantics": "SOURCE_DERIVED_FROM_VERIFIED_EXTRACTED_TREE" if verify_live_source else "UNVERIFIED",
            "deployed_profile": "ABSENT_FOR_CAT2_NEW_IDENTITY",
            "sink_attempt": "PLANNED_NOT_OBSERVED",
            "client_acceptance": "UNKNOWN_NOT_OBSERVED",
            "server_outcome": "UNKNOWN_NOT_OBSERVED",
        },
        "ordered_schedule": compiled,
        "finally_schedule": finally_schedule,
        "schedule_analysis": {
            "source_mapping_complete": True,
            "native_profile_card_only": not extension_ordinals,
            "extension_required_ordinals": extension_ordinals,
            "finally_extension_required_ordinals": finally_extension_ordinals,
            "possible_early_stop_ordinals": early_barriers,
            "may_require_later_hardware_press": bool(early_barriers),
            "profile_restart_semantics": "EACH_HARDWARE_PRESS_RESTARTS_AT_PROFILE_STEP_1",
            "intentional_wait_is_terminal": bool(compiled and compiled[-1]["intent"] == "WAIT"),
            "fallback_after_intentional_wait_allowed": False,
            "finally_must_run_after_body_stop_or_completion": bool(finally_schedule),
            "finally_runtime_implemented": False,
            "ordered_operations_may_be_reordered": False,
        },
        "operation_blockers": operation_blockers,
        "distillation": {
            "status": "PLAN_ONLY_NOT_DISTILLED",
            "target": "BRAINOF_CAT_CAT2NEW_DISPATCH_CARD_V4",
            "preserve_order": True,
            "observe_every_sink_attempt": True,
            "observe_postconditions_before_dependent_operations": True,
            "refresh_after_equipment_change": True,
            "stop_fallback_on_wait": True,
            "deployment_authorized": False,
        },
        "runtime_executable": False,
        "formal_runner_registration_authorized": False,
        "comparison_ready": False,
        "scientific_run_authorized": False,
        "claim_boundary": "source-bound candidate intent plan only; not Lua execution, client acceptance, server outcome, baseline vote, or DPS evidence",
    }
    return {**core, "content_address": _content_address(core)}


def validate_candidate_action_plan_v4(
    plan: Any,
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    verify_live_source: bool = True,
) -> JSONMap:
    document = _mapping(plan, "plan")
    core = {key: deepcopy(value) for key, value in document.items() if key != "content_address"}
    if document.get("schema") != PLAN_SCHEMA:
        raise Cat2NewCandidateExecutorV4Error("plan schema mismatch")
    if document.get("content_address") != _content_address(core):
        raise Cat2NewCandidateExecutorV4Error("plan content address mismatch")
    rebuilt = compile_candidate_action_plan_v4(
        document.get("request"),
        source_root=source_root,
        manifest_path=manifest_path,
        verify_live_source=verify_live_source,
    )
    if dict(document) != rebuilt:
        raise Cat2NewCandidateExecutorV4Error(
            "plan differs from deterministic request recompilation"
        )
    for field in (
        "runtime_executable",
        "formal_runner_registration_authorized",
        "comparison_ready",
        "scientific_run_authorized",
    ):
        if document.get(field) is not False:
            raise Cat2NewCandidateExecutorV4Error(f"plan {field} must remain false")
    role = _mapping(document.get("role"), "plan.role")
    if role.get("policy_role") != "CANDIDATE" or role.get("baseline") is not False:
        raise Cat2NewCandidateExecutorV4Error("plan role cannot self-promote to baseline")
    return deepcopy(dict(document))


def _read_savedvariables_identity(path: str | Path) -> JSONMap:
    try:
        payload, resolved = _stable_read(path, "Cat2 SavedVariables")
    except Cat2NewCandidateExecutorV4Error as error:
        return {
            "status": "ABSENT_OR_UNREADABLE",
            "error": str(error),
            "bound_to_cat2new_source": False,
        }
    return {
        "status": "READ_ONLY_BYTES_PRESENT",
        "logical_path": "%WOW_CHARACTER_SAVEDVARIABLES%\\Cat2.lua",
        "resolved_path": str(resolved),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "semantic_profile_parsed_for_cat2new": False,
        "bound_to_cat2new_source": False,
        "reason": "schema-compatible predecessor SavedVariables cannot establish semantic identity after source change",
    }


def _installed_tree_identity(path: str | Path) -> JSONMap:
    try:
        facts = compute_source_tree_facts(path)
    except Exception as error:
        return {
            "status": "ABSENT_OR_UNVERIFIED",
            "error_type": type(error).__name__,
            "error": str(error),
            "matches_cat2new_tree": False,
        }
    tree = facts["tree"]
    matches = tree["sha256"] == EXPECTED_PARTITIONS["tree"]["sha256"]
    return {
        "status": "FILESYSTEM_TREE_OBSERVED_NOT_CLIENT_LOAD_ATTESTED",
        "declared_version": facts["declared_version"],
        "tree": tree,
        "toc": facts["toc"],
        "matches_cat2new_tree": matches,
        "client_load_attested": False,
    }


def _readiness_blockers(extra: Sequence[tuple[str, str, str]]) -> list[JSONMap]:
    rows = []
    seen: set[str] = set()
    for code, scope, message in (*extra, *MANDATORY_BLOCKERS):
        if code in seen:
            continue
        seen.add(code)
        rows.append(
            {
                "code": code,
                "scope": scope,
                "message": message,
                "runner_fatal": True,
            }
        )
    return rows


MINIMUM_FUTURE_EVIDENCE: tuple[JSONMap, ...] = (
    {
        "requirement_id": "CAT2NEW_DEPLOYED_IDENTITY_AND_PROFILE",
        "minimum": "post-/reload load receipt plus a new Cat2CharacterDB profile snapshot",
        "must_bind": ["Cat2_new tree SHA-256", "TOC SHA-256", "candidate policy/distillation SHA-256", "profile semantic SHA-256", "character talent/equipment identity"],
    },
    {
        "requirement_id": "CAT2NEW_ORDERED_SINK_AND_ACCEPTANCE",
        "minimum": "attempt-ID and ordinal trace for every requested sink and postcondition",
        "must_cover": ["exact target set/restore", "named item and slots 13/14", "weapon and nonweapon equipment", "HS/Cleave queue set/replace/cancel/consume", "stance stop barrier", "off-GCD then GCD", "intentional WAIT with no fallback"],
    },
    {
        "requirement_id": "CAT2NEW_TERMINAL_SERVER_OUTCOME",
        "minimum": "each accepted result-bearing action joins to a terminal typed outcome",
        "must_cover": ["server GO or failure", "resource update", "hit/crit/miss/dodge/parry", "damage", "target GUID", "queue terminal state"],
    },
    {
        "requirement_id": "CAT2NEW_FORMAL_EXECUTOR_DIFFERENTIAL",
        "minimum": "same content-addressed plans replay identically in Windows client traces and the formal simulator executor across registered operation families",
        "must_cover": ["all lanes", "same-time order", "multi-press barriers", "equipment refresh", "target death/retarget", "no future-state access"],
    },
)


def build_readiness_report_v4(
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT,
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES,
) -> JSONMap:
    extra: list[tuple[str, str, str]] = []
    try:
        load_manifest(manifest_path)
        source_verification = verify_source_tree(source_root, manifest_path)
        source_mapping_audit = audit_contract_source_mapping_v4(source_root)
        source_verified = True
    except Exception as error:
        source_verification = {
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error": str(error),
        }
        source_mapping_audit = {"status": "FAILED", "error": str(error)}
        source_verified = False
        extra.append(("CAT2_NEW_SOURCE_IDENTITY_FAILED", "SOURCE", str(error)))

    installed = _installed_tree_identity(installed_root)
    if installed.get("matches_cat2new_tree") is not True:
        extra.append(("INSTALLED_CAT2_TREE_IS_NOT_CAT2_NEW", "DEPLOYMENT", "the Cat2 directory observed on disk does not match the pinned Cat2_new tree"))
    savedvariables = _read_savedvariables_identity(savedvariables_path)

    adapter_payload, _ = _stable_read(__file__, "Cat2_new v4 executor module")
    core: JSONMap = {
        "schema": READINESS_SCHEMA,
        "executor_id": EXECUTOR_ID,
        "identity": {
            "contract_id": CONTRACT_ID,
            "contract_sha256": CONTRACT_SHA256,
            "module_logical_path": "%PROJECT_ROOT%\\o2o_dps\\cat2new_candidate_executor_v4.py",
            "module_sha256": hashlib.sha256(adapter_payload).hexdigest(),
            "capability_manifest_id": MANIFEST_ID,
            "capability_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "source_id": SOURCE_ID,
            "source_tree_sha256": EXPECTED_PARTITIONS["tree"]["sha256"],
        },
        "source_identity": {
            "verified": source_verified,
            "verification": source_verification,
            "contract_source_mapping_audit": source_mapping_audit,
            "authority": "EXTRACTED_CAPABILITY_SOURCE_ONLY",
            "source_execution": False,
        },
        "deployed_identity": {
            "installed_tree": installed,
            "savedvariables": savedvariables,
            "cat2new_tree_loaded": False,
            "cat2new_profile_bound": False,
            "predecessor_profile_reused_as_cat2new_evidence": False,
        },
        "role": {
            "policy_role": "CANDIDATE",
            "execution_role": "FINAL_ACTION_OUTLET_CANDIDATE",
            "baseline": False,
            "independent_expert_vote": False,
            "contra_or_cat_substitution": False,
        },
        "capability_matrix": {
            "native_profile_card": ["auto target by Cat2 heuristic", "start autoattack", "slot 13/14 trinket", "Heroic Strike/Cleave queue", "bounded Warrior action and stance cards"],
            "pinned_source_api_needing_extension": ["exact target with verification/restoration", "stop autoattack", "named item", "equipment slot", "queue cancel", "generic stop cast", "dynamic terminal WAIT"],
            "source_derived_search_templates": list(PINNED_SEARCH_TEMPLATE_FILES),
            "search_template_role": "CANDIDATE_GENERATION_HINT_ONLY_NOT_LEARNED_POLICY_OR_BASELINE",
            "nearby_execute_target_order": "UNSPECIFIED_LUA_PAIRS_NONOPTIMALITY_CLAIM_FORBIDDEN",
            "all_plan_lanes_have_typed_semantics": True,
            "all_plan_lanes_have_runtime_dispatch": False,
        },
        "evidence_layers": {
            "source": "VERIFIED" if source_verified else "FAILED",
            "candidate_policy": "INTERFACE_ONLY_NO_POLICY_ARTIFACT",
            "deployed_profile": "NOT_BOUND_TO_CAT2_NEW",
            "client_load": "NOT_ATTESTED",
            "ordered_sink_trace": "MISSING",
            "client_acceptance": "MISSING",
            "server_outcome": "MISSING",
        },
        "readiness": {
            "source_identity_verified": source_verified,
            "candidate_intent_contract_complete": True,
            "cat2new_installed_tree_match": installed.get("matches_cat2new_tree") is True,
            "cat2new_client_load_attested": False,
            "cat2new_deployed_profile_bound": False,
            "lua_plan_distilled": False,
            "ordered_runtime_trace_closed": False,
            "client_acceptance_closed": False,
            "server_outcome_closed": False,
            "formal_simulator_executor_complete": False,
            "formal_runner_registration": False,
        },
        "blockers": _readiness_blockers(extra),
        "minimum_future_evidence": [deepcopy(row) for row in MINIMUM_FUTURE_EVIDENCE],
        "source_plan_compiler_available": source_verified,
        "runtime_executable": False,
        "comparison_ready": False,
        "eligible_for_independent_vote": False,
        "runner_registration_authorized": False,
        "formal_runner_registry_modified": False,
        "scientific_run_launched": False,
        "claim_boundary": "Cat2_new source identity and candidate ActionPlan interface only; no deployed policy, runtime fidelity, baseline vote, or superiority evidence",
    }
    return validate_readiness_report_v4({**core, "content_address": _content_address(core)})


def validate_readiness_report_v4(report: Any) -> JSONMap:
    document = _mapping(report, "readiness")
    core = {key: deepcopy(value) for key, value in document.items() if key != "content_address"}
    if document.get("schema") != READINESS_SCHEMA or document.get("executor_id") != EXECUTOR_ID:
        raise Cat2NewCandidateExecutorV4Error("readiness identity mismatch")
    if document.get("content_address") != _content_address(core):
        raise Cat2NewCandidateExecutorV4Error("readiness content address mismatch")
    for field in (
        "runtime_executable",
        "comparison_ready",
        "eligible_for_independent_vote",
        "runner_registration_authorized",
        "formal_runner_registry_modified",
        "scientific_run_launched",
    ):
        if document.get(field) is not False:
            raise Cat2NewCandidateExecutorV4Error(f"readiness {field} must remain false")
    role = _mapping(document.get("role"), "readiness.role")
    if role.get("baseline") is not False or role.get("policy_role") != "CANDIDATE":
        raise Cat2NewCandidateExecutorV4Error("Cat2_new cannot self-promote to a baseline")
    readiness = _mapping(document.get("readiness"), "readiness.readiness")
    for field in (
        "cat2new_client_load_attested",
        "cat2new_deployed_profile_bound",
        "lua_plan_distilled",
        "ordered_runtime_trace_closed",
        "client_acceptance_closed",
        "server_outcome_closed",
        "formal_simulator_executor_complete",
        "formal_runner_registration",
    ):
        if readiness.get(field) is not False:
            raise Cat2NewCandidateExecutorV4Error(
                f"readiness field {field} must remain false"
            )
    codes = {
        row.get("code")
        for row in _sequence(document.get("blockers"), "readiness.blockers")
        if isinstance(row, Mapping)
    }
    mandatory = {code for code, _, _ in MANDATORY_BLOCKERS}
    if not mandatory.issubset(codes):
        raise Cat2NewCandidateExecutorV4Error("mandatory typed blocker missing")
    if len(_sequence(document.get("minimum_future_evidence"), "readiness.minimum_future_evidence")) != len(MINIMUM_FUTURE_EVIDENCE):
        raise Cat2NewCandidateExecutorV4Error("minimum future evidence contract drifted")
    return deepcopy(dict(document))


def serialize_candidate_action_plan_v4(plan: Any, **kwargs: Any) -> bytes:
    return _canonical_bytes(validate_candidate_action_plan_v4(plan, **kwargs)) + b"\n"


def serialize_readiness_report_v4(report: Any) -> bytes:
    return _canonical_bytes(validate_readiness_report_v4(report)) + b"\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--installed-root", type=Path, default=DEFAULT_INSTALLED_ROOT)
    parser.add_argument("--savedvariables", type=Path, default=DEFAULT_SAVEDVARIABLES)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--skip-live-source-verification", action="store_true")
    parser.add_argument("--require-runner-ready", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.request is not None:
            payload, _ = _stable_read(args.request, "candidate request")
            request = _strict_json(payload, "candidate request")
            document = compile_candidate_action_plan_v4(
                request,
                source_root=args.source_root,
                manifest_path=args.manifest,
                verify_live_source=not args.skip_live_source_verification,
            )
            ready = document["formal_runner_registration_authorized"]
        else:
            document = build_readiness_report_v4(
                source_root=args.source_root,
                manifest_path=args.manifest,
                installed_root=args.installed_root,
                savedvariables_path=args.savedvariables,
            )
            ready = document["runner_registration_authorized"]
    except (Cat2NewCandidateExecutorV4Error, OSError, ValueError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(_canonical_bytes(document) + b"\n")
    return 0 if (not args.require_runner_ready or ready) else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTION_CARDS",
    "CONTRACT",
    "CONTRACT_ID",
    "CONTRACT_SHA256",
    "DEFAULT_INSTALLED_ROOT",
    "DEFAULT_SAVEDVARIABLES",
    "DEFAULT_SOURCE_ROOT",
    "EXECUTOR_ID",
    "LANE_INTENTS",
    "MANDATORY_BLOCKERS",
    "MINIMUM_FUTURE_EVIDENCE",
    "PLAN_SCHEMA",
    "PINNED_CORE_API_SITES",
    "PINNED_ROUTE_FILES",
    "PINNED_SEARCH_TEMPLATE_FILES",
    "QUEUE_CARDS",
    "READINESS_SCHEMA",
    "REQUEST_SCHEMA",
    "Cat2NewCandidateExecutorV4Error",
    "audit_contract_source_mapping_v4",
    "build_readiness_report_v4",
    "compile_candidate_action_plan_v4",
    "main",
    "serialize_candidate_action_plan_v4",
    "serialize_readiness_report_v4",
    "validate_candidate_action_plan_v4",
    "validate_readiness_report_v4",
]
