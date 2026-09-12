"""Cat deployed Fury full-policy source diagnostic and readiness receipt v4.

This module is intentionally isolated from every frozen v2/v3 rollout,
evaluator, runner, and readiness artifact.  It never executes Cat's Lua.  It
binds the pinned Cat source tree, a read-only per-character ``Cat.lua`` byte
identity, and the already published runtime snapshot, then implements the
reachable source traversal of *profile 1* as an auditable synthetic adapter.

The important boundary is deliberately repetitive: a source-derived ordered
sink trace is not a Turtle WoW client trace.  It proves neither addon load nor
client acceptance, server GO, resource spend, hit, or damage.  The adapter is
therefore a non-voting diagnostic, cannot register itself with a formal
runner, and cannot make Cat comparison-ready.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .cat_deployed_source_manifest_v1 import (
    DEFAULT_MANIFEST,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_TOC_CLOSURE,
    EXPECTED_TREE,
    load_manifest,
    verify_source_tree,
)
from .expert_policy import (
    CastControl,
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
    invalid_decision,
)
from .fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    SLAM,
    WHIRLWIND,
    CAT_FURY_PROFILE1,
    CatFurySourceAdapter,
    FuryExpertState,
    WeaponMode,
    _DecisionBuilder,
)
from .fury_expert_runtime_snapshot_v1 import (
    _cat_profile,
    _parse_savedvariables,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDONS_ROOT = PROJECT_ROOT.parents[1]

SCHEMA = "cat_fury_full_policy_readiness/v4"
TRACE_SCHEMA = "cat_fury_full_policy_synthetic_differential/v4"
BRANCH_AUDIT_SCHEMA = "cat_fury_profile1_branch_audit/v4"
STATE_SCHEMA = "cat_fury_full_policy_state/v4"
POLICY_ID = "cat.fury.profile1.full_policy.source_diagnostic.v4"
LEGACY_POLICY_ID = CatFurySourceAdapter.expert_id
ADAPTER_CONTRACT_ID = "cat.fury.profile1.full_policy.adapter.v4"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"

DEFAULT_SOURCE_ROOT = ADDONS_ROOT / "Cat"
DEFAULT_SAVEDVARIABLES = Path(
    r"D:\WOW\WTF\Account\GAOZHENFENG5\Basin of Stars\意踟躇\SavedVariables\Cat.lua"
)
DEFAULT_RUNTIME_SNAPSHOT = (
    PROJECT_ROOT
    / "offline_data/expert_runtime_snapshots/v1"
    / "fury_expert_runtime_snapshot_v1.4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778.json"
)

EXPECTED_CURRENT_SAVEDVARIABLES_SHA256 = (
    "8db68093af75fce8ba4699af09b1eb9f79a4607f9ca1f3a6dbaf53f1897722ba"
)
EXPECTED_CURRENT_SAVEDVARIABLES_SIZE = 103_816
EXPECTED_SNAPSHOT_FILE_SHA256 = (
    "a8624b5b8e6f081903f3ef1fdb7c9894ed24bf54783e7e7841ea6fd3e40aecfe"
)
EXPECTED_SNAPSHOT_SHA256 = (
    "4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778"
)
EXPECTED_SNAPSHOT_CAT_SAVEDVARIABLES_SHA256 = (
    "1d652aeb710a071507a217521e75e51143b1ce8cccc96fc10ca1a668f3b6aea0"
)
EXPECTED_PROFILE1_SEMANTIC_SHA256 = (
    "094c857a26f49dc7d2511e947b6ab05f2a6d22bbdb02a9953225408e9244267d"
)
EXPECTED_LEGACY_ADAPTER_FILE_SHA256 = (
    "f4c932227ed9c9753d43d0471dcd5f6d1577f87a04e3321b1a32c8c10e3c81ed"
)
EXPECTED_WARRIOR_FURY_SHA256 = (
    "1dc652bc34117d11703e2b9481b4da7386c3e3865b85413af5b2a4031bbab6fe"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
OVERPOWER = "warrior.overpower"


class CatFuryFullPolicyV4Error(RuntimeError):
    """A v4 source, profile, trace, or readiness invariant was violated."""


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_read(path: str | Path, label: str) -> tuple[bytes, Path]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise CatFuryFullPolicyV4Error(f"{label} may not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise CatFuryFullPolicyV4Error(f"{label} is not a file")
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except CatFuryFullPolicyV4Error:
        raise
    except OSError as error:
        raise CatFuryFullPolicyV4Error(
            f"could not read {label} {candidate}: {error}"
        ) from error
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_signature != after_signature or len(payload) != after.st_size:
        raise CatFuryFullPolicyV4Error(f"{label} changed while being read")
    return payload, resolved


def _strict_json(payload: bytes, label: str) -> JSONMap:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> JSONMap:
        result: JSONMap = {}
        for key, value in pairs:
            if key in result:
                raise CatFuryFullPolicyV4Error(
                    f"{label} contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=pairs_hook)
    except CatFuryFullPolicyV4Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatFuryFullPolicyV4Error(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise CatFuryFullPolicyV4Error(f"{label} root must be an object")
    return value


def _finite(value: Any, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{label} must be finite and >= {minimum}")
    return number


@dataclass(frozen=True)
class CatInventoryItemV4:
    """The first matching bag item state consumed by ``MPUseItemByName``."""

    name: str
    bag: int
    slot: int
    cooldown_remaining_s: float = 0.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("CatInventoryItemV4.name must be nonempty")
        if type(self.bag) is not int or not 0 <= self.bag <= 4:
            raise ValueError("CatInventoryItemV4.bag must be an integer in [0,4]")
        if type(self.slot) is not int or self.slot <= 0:
            raise ValueError("CatInventoryItemV4.slot must be a positive integer")
        _finite(self.cooldown_remaining_s, "cooldown_remaining_s")
        if not isinstance(self.enabled, bool):
            raise TypeError("CatInventoryItemV4.enabled must be boolean")


@dataclass(frozen=True)
class CatFuryFullPolicyStateV4:
    """Explicit state for the profile-1 source diagnostic.

    ``combat.gcd_ready`` is intentionally not used to suppress raw source
    calls: the Lua attempts abilities from its own cooldown predicates and the
    client may reject them.  Acceptance is a later evidence lane.
    """

    combat: FuryExpertState
    target_banished: bool = False
    autoattack_active: bool = True
    autoattack_lock: bool = False
    combat_elapsed_s: float = 10.0
    upper_trinket_supported: bool = False
    upper_trinket_cooldown_s: float = 1.0
    lower_trinket_supported: bool = False
    lower_trinket_cooldown_s: float = 1.0
    player_health_pct: float = 100.0
    inventory_items: tuple[CatInventoryItemV4, ...] = ()
    overpower_proc_active: bool = False
    overpower_ready_within_1_5_s: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.combat, FuryExpertState):
            raise TypeError("combat must be FuryExpertState")
        for name in (
            "target_banished",
            "autoattack_active",
            "autoattack_lock",
            "upper_trinket_supported",
            "lower_trinket_supported",
            "overpower_proc_active",
            "overpower_ready_within_1_5_s",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        _finite(self.combat_elapsed_s, "combat_elapsed_s")
        _finite(self.upper_trinket_cooldown_s, "upper_trinket_cooldown_s")
        _finite(self.lower_trinket_cooldown_s, "lower_trinket_cooldown_s")
        health = _finite(self.player_health_pct, "player_health_pct")
        if health > 100.0:
            raise ValueError("player_health_pct must be <= 100")
        coordinates: set[tuple[int, int]] = set()
        for item in self.inventory_items:
            if not isinstance(item, CatInventoryItemV4):
                raise TypeError("inventory_items must contain CatInventoryItemV4")
            coordinate = (item.bag, item.slot)
            if coordinate in coordinates:
                raise ValueError("inventory item bag/slot coordinates must be unique")
            coordinates.add(coordinate)


ADAPTER_CONTRACT: JSONMap = {
    "contract_id": ADAPTER_CONTRACT_ID,
    "policy_id": POLICY_ID,
    "entrypoint": "Bindings.xml -> MPWarriorFuryCommand -> MPFuryDPS()",
    "profile_slot": 1,
    "profile_semantic_sha256": EXPECTED_PROFILE1_SEMANTIC_SHA256,
    "source_manifest_sha256": EXPECTED_MANIFEST_SHA256,
    "source_toc_closure_sha256": EXPECTED_TOC_CLOSURE["sha256"],
    "warrior_fury_sha256": EXPECTED_WARRIOR_FURY_SHA256,
    "ordered_sink_channels": [
        "target",
        "autoattack",
        "item",
        "off_gcd",
        "swing_queue",
        "cast_control",
        "stance",
        "gcd",
        "cvar",
    ],
    "semantics": [
        "source_translation_not_lua_execution",
        "profile1_only",
        "binding_entry_once_equals_zero",
        "raw_attempt_is_not_client_acceptance",
        "same_invocation_duplicate_attempts_preserved",
        "slam_nampower_cvar_sequence_preserved",
        "formal_runner_registration_forbidden",
    ],
}
ADAPTER_CONTRACT_SHA256 = _sha256(_canonical_bytes(ADAPTER_CONTRACT))


def _branch(
    branch_id: str,
    source_ref: str,
    profile1_status: str,
    legacy_status: str,
    claim: str,
) -> JSONMap:
    return {
        "branch_id": branch_id,
        "source_ref": source_ref,
        "profile1_status": profile1_status,
        "legacy_adapter_status": legacy_status,
        "claim": claim,
    }


# Action-bearing and return-bearing policy branches in WarriorFury.lua.  The
# catalogue distinguishes branches made unreachable by the frozen profile from
# branches mapped by this diagnostic.  Commented source is never executable.
SOURCE_BRANCH_CATALOG: tuple[JSONMap, ...] = (
    _branch("AOE_SCAN_AND_STRIKE_SELECT", "WarriorFury.lua:61-79", "MAPPED_REACHABLE", "BOUNDED", "profile1 uses the SuperWoW-derived nearby count to choose Heroic Strike or Cleave"),
    _branch("TARGET_SWITCH_HELPER", "WarriorFury.lua:83-84;CatLib.lua:125-185", "DISABLED_BY_PROFILE", "COLLAPSED", "Target=0 makes MPAutoSwitchTarget return without a sink"),
    _branch("START_ATTACK_GUARD", "WarriorFury.lua:86-87;CatLib.lua:107-112", "MAPPED_REACHABLE", "GUARD_MISSING", "AttackTarget is called only when both autoattack and its lock are false"),
    _branch("AUTO_LOOT", "WarriorFury.lua:89-92", "DISABLED_BY_PROFILE", "MISSING", "Pick=0"),
    _branch("POWER_HELPER", "WarriorFury.lua:94-97", "DISABLED_BY_PROFILE", "MISSING", "Power=0"),
    _branch("BANISH_EARLY_RETURN", "WarriorFury.lua:99-101", "MAPPED_REACHABLE", "MISSING", "a banished target stops the policy after the attack prelude"),
    _branch("INTERRUPT_HELPER", "WarriorFury.lua:103-108", "DISABLED_BY_PROFILE", "MISSING", "Interrupt=0"),
    _branch("TRINKET_UPPER", "WarriorFury.lua:113-119", "MAPPED_REACHABLE", "MISSING", "slot 13 is attempted after three combat seconds when ready and supported"),
    _branch("TRINKET_LOWER", "WarriorFury.lua:120-126", "MAPPED_REACHABLE", "MISSING", "slot 14 is attempted after three combat seconds when ready and supported"),
    _branch("SOULSPEED", "WarriorFury.lua:127-131", "DISABLED_BY_PROFILE", "MISSING", "Soulspeed=0"),
    _branch("HEALTHSTONE", "WarriorFury.lua:133-137;CatLib.lua:1085-1107", "MAPPED_REACHABLE", "MISSING", "HealthStone=1 with threshold 30"),
    _branch("HERBAL_TEA_PAIR", "WarriorFury.lua:138-141;CatLib.lua:1085-1107", "MAPPED_REACHABLE", "MISSING", "HerbalTea=1 with threshold 20 and two ordered name lookups"),
    _branch("CARROT", "WarriorFury.lua:142-144", "DISABLED_BY_PROFILE", "MISSING", "Carrot=0"),
    _branch("RACIAL_TRAITS", "WarriorFury.lua:146-168", "DISABLED_BY_PROFILE", "MISSING", "RacialTraits=0"),
    _branch("DEATH_WISH", "WarriorFury.lua:170-176", "DISABLED_BY_PROFILE", "MISSING", "DeathWish=0"),
    _branch("RECKLESSNESS", "WarriorFury.lua:178-182", "DISABLED_BY_PROFILE", "MISSING", "Recklessness=0"),
    _branch("OUT_OF_COMBAT_WAIT_RESET", "WarriorFury.lua:184-189", "MAPPED_REACHABLE", "IMPLICIT", "wait flags cannot become true under the disabled profile1 stance-transition features"),
    _branch("OVERPOWER_PRIMARY", "WarriorFury.lua:194-208", "DISABLED_BY_PROFILE", "MISSING", "Overpower=0"),
    _branch("OVERPOWER_BATTLE_STANCE_SUPPLEMENT", "WarriorFury.lua:210-214", "MAPPED_REACHABLE", "MISSING", "this supplemental Overpower branch is not guarded by the profile switch"),
    _branch("REND_COMMENT_BLOCK", "WarriorFury.lua:216-232", "COMMENTED_OUT", "NOT_EXECUTABLE", "Lua long comment excludes Rend"),
    _branch("SWEEPING_STRIKES", "WarriorFury.lua:235-258", "DISABLED_BY_PROFILE", "MISSING", "Sweeping=0"),
    _branch("CHARGE_INTERCEPT", "WarriorFury.lua:261-280", "DISABLED_BY_PROFILE", "MISSING", "Charge=0"),
    _branch("BATTLE_SHOUT_RETURN", "WarriorFury.lua:283-289", "MAPPED_REACHABLE", "BOUNDED", "BattleShout=1"),
    _branch("BERSERKER_STANCE_RETURN", "WarriorFury.lua:292-296", "MAPPED_REACHABLE", "BOUNDED", "BerserkerStance=1"),
    _branch("BLOODRAGE_BT_ATTEMPT", "WarriorFury.lua:299-301", "MAPPED_REACHABLE", "DUPLICATES_COLLAPSED", "the Bloodthirst reserve test may emit Bloodrage"),
    _branch("BLOODRAGE_WW_ATTEMPT", "WarriorFury.lua:302", "MAPPED_REACHABLE", "DUPLICATES_COLLAPSED", "the Whirlwind reserve test may emit a second Bloodrage in the same invocation"),
    _branch("SUNDER_ARMOR", "WarriorFury.lua:306-334", "DISABLED_BY_PROFILE", "MISSING", "SunderArmor=0"),
    _branch("TWO_HAND_DYNAMIC_NEXT_SWING", "WarriorFury.lua:338-366", "MAPPED_REACHABLE", "BOUNDED", "dynamic rage reserve and strict greater-than queue guard"),
    _branch("DUAL_WIELD_DYNAMIC_NEXT_SWING", "WarriorFury.lua:370-388", "MAPPED_REACHABLE", "BOUNDED", "dual-wield reserve and execute-phase AOE exception"),
    _branch("TWO_HAND_AOE_WHIRLWIND", "WarriorFury.lua:400-404", "MAPPED_REACHABLE", "COOLDOWN_GUARD_DRIFT", "optional Slam stop precedes Whirlwind"),
    _branch("TWO_HAND_NONBOSS_EXECUTE", "WarriorFury.lua:407-411", "MAPPED_REACHABLE", "BOUNDED", "source attempts Execute without a rage guard"),
    _branch("TWO_HAND_NO_TALENT_TIMING", "WarriorFury.lua:415-466", "MAPPED_SENSITIVITY", "SOURCE_SITES_COLLAPSED", "no-Flurry-talent leveling timing tree"),
    _branch("TWO_HAND_FLURRY_ACTIVE_TIMING", "WarriorFury.lua:469-519", "MAPPED_REACHABLE", "COOLDOWN_GUARD_DRIFT", "Flurry-active Slam timing tree"),
    _branch("TWO_HAND_FLURRY_INACTIVE_PRIORITY", "WarriorFury.lua:520-566", "MAPPED_REACHABLE", "COOLDOWN_GUARD_DRIFT", "Whirlwind, execute, Bloodthirst, Hamstring, then Slam"),
    _branch("DUAL_WIELD_NORMAL_PRIORITY", "WarriorFury.lua:576-601", "MAPPED_REACHABLE", "COOLDOWN_GUARD_DRIFT", "Bloodthirst, Whirlwind, then 1.4-second Hamstring probe"),
    _branch("DUAL_WIELD_EXECUTE_PRIORITY", "WarriorFury.lua:603-618", "MAPPED_REACHABLE", "BOUNDED", "exact-ready Bloodthirst reserve then Execute"),
)


def _site(
    site_id: str,
    channel: str,
    operation: str,
    value: str | None,
    source_ref: str,
) -> JSONMap:
    return {
        "site_id": site_id,
        "channel": channel,
        "operation": operation,
        "value": value,
        "source_ref": source_ref,
    }


# Every reachable raw sink site for the frozen profile, plus the no-Flurry and
# no-Nampower sensitivity branches.  Profile-disabled source calls are listed
# in SOURCE_BRANCH_CATALOG but intentionally absent here.
REQUIRED_PROFILE1_RAW_SINK_SITES: tuple[JSONMap, ...] = (
    _site("attack_start", "autoattack", "AttackTarget", "START", "WarriorFury.lua:86-87;CatLib.lua:107-112"),
    _site("trinket_13", "item", "UseInventoryItem", "13", "WarriorFury.lua:113-119"),
    _site("trinket_14", "item", "UseInventoryItem", "14", "WarriorFury.lua:120-126"),
    _site("healthstone", "item", "UseContainerItem", "0:1:特效治疗石", "WarriorFury.lua:133-137;CatLib.lua:1085-1107"),
    _site("sugary_tea", "item", "UseContainerItem", "1:2:糖水茶", "WarriorFury.lua:138-141;CatLib.lua:1085-1107"),
    _site("nordanaar_tea", "item", "UseContainerItem", "2:3:诺达纳尔草药茶", "WarriorFury.lua:138-141;CatLib.lua:1085-1107"),
    _site("overpower_supplement", "gcd", "CastSpellByName", "压制", "WarriorFury.lua:210-214"),
    _site("battle_shout", "gcd", "CastSpellByName", "战斗怒吼", "WarriorFury.lua:283-289"),
    _site("berserker_stance", "stance", "CastSpellByName", "狂暴姿态", "WarriorFury.lua:292-296"),
    _site("bloodrage_bt_queue", "off_gcd", "QueueSpellByName", "血性狂暴", "WarriorFury.lua:299-301"),
    _site("bloodrage_ww_queue", "off_gcd", "QueueSpellByName", "血性狂暴", "WarriorFury.lua:302"),
    _site("bloodrage_bt_direct", "off_gcd", "CastSpellByName", "血性狂暴", "WarriorFury.lua:299-301"),
    _site("bloodrage_ww_direct", "off_gcd", "CastSpellByName", "血性狂暴", "WarriorFury.lua:302"),
    _site("two_hand_queue_hs", "swing_queue", "QueueSpellByName", "英勇打击", "WarriorFury.lua:338-366"),
    _site("two_hand_queue_cleave", "swing_queue", "QueueSpellByName", "顺劈斩", "WarriorFury.lua:338-366"),
    _site("dual_queue_hs", "swing_queue", "QueueSpellByName", "英勇打击", "WarriorFury.lua:370-388"),
    _site("dual_queue_cleave", "swing_queue", "QueueSpellByName", "顺劈斩", "WarriorFury.lua:370-388"),
    _site("two_hand_aoe_stop", "cast_control", "SpellStopCasting", None, "WarriorFury.lua:400-403"),
    _site("two_hand_aoe_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:400-404"),
    _site("two_hand_nonboss_stop", "cast_control", "SpellStopCasting", None, "WarriorFury.lua:407-410"),
    _site("two_hand_nonboss_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:407-411"),
    _site("no_talent_early_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:419-423"),
    _site("no_talent_early_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:426-428"),
    _site("no_talent_early_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:431-433"),
    _site("no_talent_mid_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:436-440"),
    _site("no_talent_mid_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:443-445"),
    _site("no_talent_mid_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:448-450"),
    _site("no_talent_mid_slam", "gcd", "CastSpellByName", "猛击", "WarriorFury.lua:453-455;CatLib.lua:939-948"),
    _site("no_talent_late_slam", "gcd", "CastSpellByName", "猛击", "WarriorFury.lua:458-462;CatLib.lua:939-948"),
    _site("flurry_early_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:472-476"),
    _site("flurry_early_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:479-481"),
    _site("flurry_early_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:484-486"),
    _site("flurry_mid_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:489-493"),
    _site("flurry_mid_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:496-498"),
    _site("flurry_mid_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:501-503"),
    _site("flurry_mid_slam", "gcd", "CastSpellByName", "猛击", "WarriorFury.lua:506-508;CatLib.lua:939-948"),
    _site("flurry_late_slam", "gcd", "CastSpellByName", "猛击", "WarriorFury.lua:511-515;CatLib.lua:939-948"),
    _site("no_flurry_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:520-524"),
    _site("no_flurry_execute_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:527-530"),
    _site("no_flurry_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:532-535"),
    _site("no_flurry_normal_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:537-540"),
    _site("no_flurry_hamstring", "gcd", "CastSpellByName", "断筋", "WarriorFury.lua:542-546"),
    _site("no_flurry_slam", "gcd", "CastSpellByName", "猛击", "WarriorFury.lua:549-563;CatLib.lua:939-948"),
    _site("dual_normal_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:580-583"),
    _site("dual_normal_ww", "gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:586-589"),
    _site("dual_normal_hamstring", "gcd", "CastSpellByName", "断筋", "WarriorFury.lua:592-600"),
    _site("dual_execute_bt", "gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:609-610"),
    _site("dual_execute", "gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:616"),
)


def _provenance() -> ExpertProvenance:
    return ExpertProvenance(
        expert_id=POLICY_ID,
        kind=ProvenanceKind.SOURCE_DERIVED,
        role=ExpertRole.CANDIDATE,
        authority_files=(
            r"%WOW_ADDONS_ROOT%\Cat\Cat.toc",
            r"%WOW_ADDONS_ROOT%\Cat\WarriorFury.lua",
            r"%WOW_ADDONS_ROOT%\Cat\CatLib.lua",
            r"%WOW_CHARACTER_SAVEDVARIABLES%\Cat.lua",
            r"%PROJECT_ROOT%\offline_data\expert_runtime_snapshots\v1\fury_expert_runtime_snapshot_v1.4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778.json",
        ),
        source_refs=tuple(row["source_ref"] for row in SOURCE_BRANCH_CATALOG),
    )


def _with_nampower_operation(state: FuryExpertState) -> str:
    return "QueueSpellByName" if state.nampower else "CastSpellByName"


def _emit_with_nampower(
    builder: _DecisionBuilder,
    *,
    lane: str,
    action: str,
    localized: str,
    source_ref: str,
    state: FuryExpertState,
) -> None:
    operation = _with_nampower_operation(state)
    if lane == "off_gcd":
        builder.emit_off_gcd(
            action, operation=operation, value=localized, source_ref=source_ref
        )
    elif lane == "swing_queue":
        queue = (
            SwingQueueOp.CLEAVE if localized == "顺劈斩" else SwingQueueOp.HEROIC_STRIKE
        )
        builder.emit_queue(
            queue, operation=operation, value=localized, source_ref=source_ref
        )
    else:
        raise AssertionError(f"unsupported Nampower lane {lane}")


def _emit_direct_gcd(
    builder: _DecisionBuilder, action: str, localized: str, source_ref: str
) -> None:
    builder.emit_gcd(
        action,
        operation="CastSpellByName",
        value=localized,
        source_ref=source_ref,
    )


def _emit_slam(
    builder: _DecisionBuilder, state: FuryExpertState, source_ref: str
) -> None:
    if state.nampower:
        builder.raw.extend(
            (
                RawSink("cvar", "SetCVar", "NP_QueueCastTimeSpells=0", source_ref),
                RawSink("cvar", "SetCVar", "NP_QueueInstantSpells=0", source_ref),
            )
        )
    builder.emit_gcd(
        SLAM,
        operation="CastSpellByName",
        value="猛击",
        source_ref=source_ref,
    )
    if state.nampower:
        builder.raw.extend(
            (
                RawSink("cvar", "SetCVar", "NP_QueueCastTimeSpells=1", source_ref),
                RawSink("cvar", "SetCVar", "NP_QueueInstantSpells=1", source_ref),
            )
        )


class CatFuryFullPolicyAdapterV4:
    """Source-order diagnostic for the bound Cat profile 1.

    It is executable as Python for synthetic fixtures only.  Its provenance is
    CANDIDATE and every decision remains non-voting.
    """

    expert_id = POLICY_ID
    profile = CAT_FURY_PROFILE1

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        provenance = _provenance()
        if not isinstance(state, CatFuryFullPolicyStateV4):
            return invalid_decision(
                provenance,
                "Cat v4 full-policy diagnostic requires its explicit state wrapper",
                metadata=self._metadata([], ["V4_STATE_WRAPPER_MISSING"]),
            )
        combat = state.combat
        if not combat.target_exists:
            return invalid_decision(
                provenance,
                "MPFuryDPS reads target health before its target helper; absent-target arithmetic is not reconstructed",
                metadata=self._metadata([], ["ENTRY_TARGET_HEALTH_UNDEFINED"]),
            )

        builder = _DecisionBuilder()
        visited = ["AOE_SCAN_AND_STRIKE_SELECT"]
        helper_attempts: list[JSONMap] = []

        if not state.autoattack_active and not state.autoattack_lock:
            builder.raw.append(
                RawSink(
                    "autoattack",
                    "AttackTarget",
                    "START",
                    "WarriorFury.lua:86-87;CatLib.lua:107-112",
                )
            )
            visited.append("START_ATTACK_GUARD")

        if state.target_banished:
            visited.append("BANISH_EARLY_RETURN")
            return self._build(
                builder,
                state,
                visited,
                helper_attempts,
                traversal_return="BANISH_EARLY_RETURN",
            )

        if combat.in_combat:
            self._combat_utility(builder, state, visited, helper_attempts)
        else:
            visited.append("OUT_OF_COMBAT_WAIT_RESET")

        # Unlike the primary Overpower block above it, this source branch is
        # not guarded by profile1.Overpower.  It is therefore reachable even
        # though profile1 stores Overpower=0.
        if (
            state.overpower_proc_active
            and state.overpower_ready_within_1_5_s
            and combat.rage > 4.0
            and combat.current_stance is StanceOp.BATTLE
        ):
            _emit_direct_gcd(
                builder, OVERPOWER, "压制", "WarriorFury.lua:210-214"
            )
            visited.append("OVERPOWER_BATTLE_STANCE_SUPPLEMENT")
            return self._build(
                builder,
                state,
                visited,
                helper_attempts,
                traversal_return="OVERPOWER_BATTLE_STANCE_SUPPLEMENT",
            )

        if (
            self.profile.battle_shout_enabled
            and not combat.has_battle_shout
            and combat.rage > 9
            and combat.battle_shout_remaining_s < 5.0
        ):
            _emit_direct_gcd(
                builder, BATTLE_SHOUT, "战斗怒吼", "WarriorFury.lua:283-289"
            )
            visited.append("BATTLE_SHOUT_RETURN")
            return self._build(
                builder,
                state,
                visited,
                helper_attempts,
                traversal_return="BATTLE_SHOUT_RETURN",
            )

        if (
            self.profile.berserker_stance_required
            and combat.in_combat
            and combat.current_stance is not StanceOp.BERSERKER
        ):
            builder.emit_stance(
                StanceOp.BERSERKER,
                operation="CastSpellByName",
                value="狂暴姿态",
                source_ref="WarriorFury.lua:292-296",
            )
            visited.append("BERSERKER_STANCE_RETURN")
            return self._build(
                builder,
                state,
                visited,
                helper_attempts,
                traversal_return="BERSERKER_STANCE_RETURN",
            )

        self._bloodrage(builder, combat, visited)
        self._next_swing(builder, combat, visited)
        if combat.weapon_mode is WeaponMode.TWO_HAND:
            self._two_hand(builder, combat, visited)
        else:
            self._dual_wield(builder, combat, visited)
        return self._build(builder, state, visited, helper_attempts)

    def _combat_utility(
        self,
        builder: _DecisionBuilder,
        state: CatFuryFullPolicyStateV4,
        visited: list[str],
        helper_attempts: list[JSONMap],
    ) -> None:
        combat = state.combat
        target_distance = combat.in_melee_range
        if (
            target_distance
            and state.upper_trinket_supported
            and state.upper_trinket_cooldown_s == 0.0
            and state.combat_elapsed_s > 3.0
        ):
            builder.raw.append(
                RawSink("item", "UseInventoryItem", "13", "WarriorFury.lua:113-119")
            )
            visited.append("TRINKET_UPPER")
        if (
            target_distance
            and state.lower_trinket_supported
            and state.lower_trinket_cooldown_s == 0.0
            and state.combat_elapsed_s > 3.0
        ):
            builder.raw.append(
                RawSink("item", "UseInventoryItem", "14", "WarriorFury.lua:120-126")
            )
            visited.append("TRINKET_LOWER")
        if state.player_health_pct < 30.0:
            self._use_item_by_name(
                builder,
                state,
                "特效治疗石",
                "WarriorFury.lua:133-137;CatLib.lua:1085-1107",
                "HEALTHSTONE",
                visited,
                helper_attempts,
            )
        if state.player_health_pct < 20.0:
            for name in ("糖水茶", "诺达纳尔草药茶"):
                self._use_item_by_name(
                    builder,
                    state,
                    name,
                    "WarriorFury.lua:138-141;CatLib.lua:1085-1107",
                    "HERBAL_TEA_PAIR",
                    visited,
                    helper_attempts,
                )

    @staticmethod
    def _use_item_by_name(
        builder: _DecisionBuilder,
        state: CatFuryFullPolicyStateV4,
        name: str,
        source_ref: str,
        branch_id: str,
        visited: list[str],
        helper_attempts: list[JSONMap],
    ) -> None:
        first = next(
            (
                item
                for item in sorted(
                    state.inventory_items, key=lambda value: (value.bag, value.slot)
                )
                if item.name == name
            ),
            None,
        )
        emitted = bool(
            first is not None
            and first.enabled
            and first.cooldown_remaining_s <= 1.0
        )
        helper_attempts.append(
            {
                "helper": "MPUseItemByName",
                "name": name,
                "first_match": (
                    None
                    if first is None
                    else {
                        "bag": first.bag,
                        "slot": first.slot,
                        "enabled": first.enabled,
                        "cooldown_remaining_s": first.cooldown_remaining_s,
                    }
                ),
                "raw_sink_emitted": emitted,
                "source_ref": source_ref,
            }
        )
        if emitted and first is not None:
            builder.raw.append(
                RawSink(
                    "item",
                    "UseContainerItem",
                    f"{first.bag}:{first.slot}:{name}",
                    source_ref,
                )
            )
            if branch_id not in visited:
                visited.append(branch_id)

    @staticmethod
    def _bloodrage(
        builder: _DecisionBuilder,
        state: FuryExpertState,
        visited: list[str],
    ) -> None:
        if not (
            state.bloodrage_ready
            and state.in_combat
            and state.in_melee_range
            and state.current_stance is StanceOp.BERSERKER
        ):
            return
        if (
            state.bloodthirst_known
            and state.cooldown_within(state.bloodthirst_ready_in_s, 1.5)
            and state.rage < 30.0
        ):
            _emit_with_nampower(
                builder,
                lane="off_gcd",
                action=BLOODRAGE,
                localized="血性狂暴",
                source_ref="WarriorFury.lua:299-301",
                state=state,
            )
            visited.append("BLOODRAGE_BT_ATTEMPT")
        if (
            state.cooldown_within(state.whirlwind_ready_in_s, 1.5)
            and state.rage < 25.0
        ):
            _emit_with_nampower(
                builder,
                lane="off_gcd",
                action=BLOODRAGE,
                localized="血性狂暴",
                source_ref="WarriorFury.lua:302",
                state=state,
            )
            visited.append("BLOODRAGE_WW_ATTEMPT")

    @staticmethod
    def _next_swing(
        builder: _DecisionBuilder,
        state: FuryExpertState,
        visited: list[str],
    ) -> None:
        aoe = state.nearby_enemies > 1
        localized = "顺劈斩" if aoe else "英勇打击"
        cost = state.cleave_cost if aoe else state.heroic_strike_cost
        if state.weapon_mode is WeaponMode.TWO_HAND:
            reserve = 15.0 + cost
            if not aoe and state.bloodthirst_known and state.cooldown_within(
                state.bloodthirst_ready_in_s, 1.5
            ):
                reserve += 30.0
            if state.cooldown_within(state.whirlwind_ready_in_s, 1.5):
                reserve += state.whirlwind_cost
            if state.target_health_pct >= 20.0 and state.rage > reserve:
                _emit_with_nampower(
                    builder,
                    lane="swing_queue",
                    action="",
                    localized=localized,
                    source_ref="WarriorFury.lua:338-366",
                    state=state,
                )
                visited.append("TWO_HAND_DYNAMIC_NEXT_SWING")
            return

        reserve = cost * 2.0
        if state.bloodthirst_known and state.cooldown_within(
            state.bloodthirst_ready_in_s, 1.3
        ):
            reserve += 30.0
        if state.cooldown_within(state.whirlwind_ready_in_s, 1.3):
            reserve += state.whirlwind_cost
        health_allows = aoe or state.target_health_pct >= 20.0
        if health_allows and state.rage > reserve:
            _emit_with_nampower(
                builder,
                lane="swing_queue",
                action="",
                localized=localized,
                source_ref="WarriorFury.lua:370-388",
                state=state,
            )
            visited.append("DUAL_WIELD_DYNAMIC_NEXT_SWING")

    @staticmethod
    def _whirlwind_attempt_ready(state: FuryExpertState) -> bool:
        return (
            state.cooldown_within(state.whirlwind_ready_in_s, 1.5)
            and state.rage >= state.whirlwind_cost
            and state.in_melee_range
            and state.current_stance is StanceOp.BERSERKER
        )

    @staticmethod
    def _bloodthirst_attempt_ready(state: FuryExpertState) -> bool:
        return (
            state.bloodthirst_known
            and state.cooldown_within(state.bloodthirst_ready_in_s, 1.5)
            and state.rage > 29.0
        )

    def _timing_tree(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        visited: list[str],
        *,
        branch_id: str,
        refs: Mapping[str, str],
    ) -> bool:
        if state.mainhand_swing_remaining_s < self.profile.slam_timing_s:
            if self._whirlwind_attempt_ready(state):
                _emit_direct_gcd(builder, WHIRLWIND, "旋风斩", refs["early_whirlwind"])
                visited.append(branch_id)
                return True
            if self._bloodthirst_attempt_ready(state):
                _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", refs["early_bloodthirst"])
                visited.append(branch_id)
                return True
            if state.target_health_pct < 20.0 and state.rage >= state.execute_cost:
                _emit_direct_gcd(builder, EXECUTE, "斩杀", refs["early_execute"])
                visited.append(branch_id)
                return True
            return False
        if state.mainhand_swing_remaining_s < 2.0:
            if self._whirlwind_attempt_ready(state):
                _emit_direct_gcd(builder, WHIRLWIND, "旋风斩", refs["mid_whirlwind"])
                visited.append(branch_id)
                return True
            if self._bloodthirst_attempt_ready(state):
                _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", refs["mid_bloodthirst"])
                visited.append(branch_id)
                return True
            if state.target_health_pct < 20.0 and state.rage >= state.execute_cost:
                _emit_direct_gcd(builder, EXECUTE, "斩杀", refs["mid_execute"])
                visited.append(branch_id)
                return True
            if state.rage >= 15.0 and state.current_stance is StanceOp.BERSERKER:
                _emit_slam(builder, state, refs["mid_slam"])
                visited.append(branch_id)
                return True
            return False
        if state.rage >= 15.0 and state.current_stance is StanceOp.BERSERKER:
            _emit_slam(builder, state, refs["late_slam"])
            visited.append(branch_id)
            return True
        return False

    def _two_hand(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        visited: list[str],
    ) -> None:
        if state.nearby_enemies > 1 and self._whirlwind_attempt_ready(state):
            if state.casting_slam:
                builder.emit_stop_cast(source_ref="WarriorFury.lua:400-403")
            _emit_direct_gcd(
                builder, WHIRLWIND, "旋风斩", "WarriorFury.lua:400-404"
            )
            visited.append("TWO_HAND_AOE_WHIRLWIND")
            return
        if (
            not state.target_is_boss
            and state.target_health_pct < 20.0
            and self.profile.execute_enabled
            and self.profile.execute_non_boss
        ):
            if state.casting_slam:
                builder.emit_stop_cast(source_ref="WarriorFury.lua:407-410")
            _emit_direct_gcd(builder, EXECUTE, "斩杀", "WarriorFury.lua:407-411")
            visited.append("TWO_HAND_NONBOSS_EXECUTE")
            return

        if not state.flurry_talent:
            acted = self._timing_tree(
                builder,
                state,
                visited,
                branch_id="TWO_HAND_NO_TALENT_TIMING",
                refs={
                    "early_whirlwind": "WarriorFury.lua:419-423",
                    "early_bloodthirst": "WarriorFury.lua:426-428",
                    "early_execute": "WarriorFury.lua:431-433",
                    "mid_whirlwind": "WarriorFury.lua:436-440",
                    "mid_bloodthirst": "WarriorFury.lua:443-445",
                    "mid_execute": "WarriorFury.lua:448-450",
                    "mid_slam": "WarriorFury.lua:453-455;CatLib.lua:939-948",
                    "late_slam": "WarriorFury.lua:458-462;CatLib.lua:939-948",
                },
            )
            if acted:
                return

        if state.flurry_active:
            self._timing_tree(
                builder,
                state,
                visited,
                branch_id="TWO_HAND_FLURRY_ACTIVE_TIMING",
                refs={
                    "early_whirlwind": "WarriorFury.lua:472-476",
                    "early_bloodthirst": "WarriorFury.lua:479-481",
                    "early_execute": "WarriorFury.lua:484-486",
                    "mid_whirlwind": "WarriorFury.lua:489-493",
                    "mid_bloodthirst": "WarriorFury.lua:496-498",
                    "mid_execute": "WarriorFury.lua:501-503",
                    "mid_slam": "WarriorFury.lua:506-508;CatLib.lua:939-948",
                    "late_slam": "WarriorFury.lua:511-515;CatLib.lua:939-948",
                },
            )
            return

        if self._whirlwind_attempt_ready(state):
            _emit_direct_gcd(builder, WHIRLWIND, "旋风斩", "WarriorFury.lua:520-524")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")
            return
        if (
            state.target_health_pct < 20.0
            and self._bloodthirst_attempt_ready(state)
            and state.rage >= state.execute_cost + 30.0
        ):
            _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", "WarriorFury.lua:527-530")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")
            return
        if state.target_health_pct < 20.0 and state.rage >= state.execute_cost:
            _emit_direct_gcd(builder, EXECUTE, "斩杀", "WarriorFury.lua:532-535")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")
            return
        if self._bloodthirst_attempt_ready(state):
            _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", "WarriorFury.lua:537-540")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")
            return
        if (
            state.bloodthirst_ready_in_s > 1.5
            and state.whirlwind_ready_in_s > 1.5
            and state.rage > 9.0
        ):
            _emit_direct_gcd(builder, HAMSTRING, "断筋", "WarriorFury.lua:542-546")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")
            return
        if state.mainhand_swing_remaining_s > 2.0 and state.rage >= 15.0:
            _emit_slam(builder, state, "WarriorFury.lua:549-563;CatLib.lua:939-948")
            visited.append("TWO_HAND_FLURRY_INACTIVE_PRIORITY")

    def _dual_wield(
        self,
        builder: _DecisionBuilder,
        state: FuryExpertState,
        visited: list[str],
    ) -> None:
        normal_phase = state.target_health_pct >= 20.0 or not self.profile.execute_enabled
        if normal_phase:
            if self._bloodthirst_attempt_ready(state):
                _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", "WarriorFury.lua:580-583")
                visited.append("DUAL_WIELD_NORMAL_PRIORITY")
                return
            if self._whirlwind_attempt_ready(state):
                _emit_direct_gcd(builder, WHIRLWIND, "旋风斩", "WarriorFury.lua:586-589")
                visited.append("DUAL_WIELD_NORMAL_PRIORITY")
                return
            if (
                state.bloodthirst_ready_in_s > 1.4
                and state.whirlwind_ready_in_s > 1.4
                and state.rage > 9.0
            ):
                _emit_direct_gcd(builder, HAMSTRING, "断筋", "WarriorFury.lua:592-600")
                visited.append("DUAL_WIELD_NORMAL_PRIORITY")
            return

        exact_bloodthirst = (
            state.bloodthirst_known
            and state.cooldown_ready(state.bloodthirst_ready_in_s)
        )
        if exact_bloodthirst and state.rage >= state.execute_cost + 30.0:
            _emit_direct_gcd(builder, BLOODTHIRST, "嗜血", "WarriorFury.lua:609-610")
            visited.append("DUAL_WIELD_EXECUTE_PRIORITY")
            return
        if state.rage >= state.execute_cost:
            _emit_direct_gcd(builder, EXECUTE, "斩杀", "WarriorFury.lua:616")
            visited.append("DUAL_WIELD_EXECUTE_PRIORITY")

    def _metadata(
        self,
        visited: Sequence[str],
        errors: Sequence[str],
        *,
        helper_attempts: Sequence[Mapping[str, Any]] = (),
        traversal_return: str | None = None,
    ) -> JSONMap:
        return {
            "schema": STATE_SCHEMA,
            "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
            "profile_semantic_sha256": EXPECTED_PROFILE1_SEMANTIC_SHA256,
            "source_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "runtime_snapshot_sha256": EXPECTED_SNAPSHOT_SHA256,
            "visited_branch_ids": list(dict.fromkeys(visited)),
            "helper_attempts": [dict(row) for row in helper_attempts],
            "traversal_return": traversal_return,
            "input_errors": list(dict.fromkeys(errors)),
            "source_execution": False,
            "source_derived_diagnostic_executable": not errors,
            "game_client_load_observed": False,
            "game_client_ordered_sink_trace_observed": False,
            "client_acceptance_observed": False,
            "server_outcome_observed": False,
            "comparison_ready": False,
            "eligible_for_independent_vote": False,
            "runner_registration_authorized": False,
        }

    def _build(
        self,
        builder: _DecisionBuilder,
        state: CatFuryFullPolicyStateV4,
        visited: Sequence[str],
        helper_attempts: Sequence[Mapping[str, Any]],
        *,
        traversal_return: str | None = None,
    ) -> ExpertDecision:
        metadata = self._metadata(
            visited,
            [],
            helper_attempts=helper_attempts,
            traversal_return=traversal_return,
        )
        metadata.update(
            {
                "weapon_mode": state.combat.weapon_mode.value,
                "gcd_ready_input_is_acceptance_state_only": state.combat.gcd_ready,
                "source_attempts_suppressed_by_gcd_ready": False,
            }
        )
        return builder.build(
            _provenance(),
            role_can_vote=False,
            reason="Cat profile1 full-policy source-derived diagnostic",
            metadata=metadata,
        )


def validate_source_decision_v4(decision: ExpertDecision) -> ExpertDecision:
    if not isinstance(decision, ExpertDecision):
        raise TypeError("decision must be ExpertDecision")
    if decision.expert_id != POLICY_ID:
        raise CatFuryFullPolicyV4Error("unexpected policy identity")
    if decision.provenance.kind is not ProvenanceKind.SOURCE_DERIVED:
        raise CatFuryFullPolicyV4Error("provenance must remain SOURCE_DERIVED")
    if decision.provenance.role is not ExpertRole.CANDIDATE:
        raise CatFuryFullPolicyV4Error("v4 diagnostic must remain CANDIDATE")
    if decision.eligible_for_independent_vote:
        raise CatFuryFullPolicyV4Error("v4 diagnostic may not vote")
    metadata = decision.metadata
    for name in (
        "source_execution",
        "game_client_load_observed",
        "game_client_ordered_sink_trace_observed",
        "client_acceptance_observed",
        "server_outcome_observed",
        "comparison_ready",
        "eligible_for_independent_vote",
        "runner_registration_authorized",
    ):
        if metadata.get(name) is not False:
            raise CatFuryFullPolicyV4Error(f"{name} must remain false")
    if metadata.get("adapter_contract_sha256") != ADAPTER_CONTRACT_SHA256:
        raise CatFuryFullPolicyV4Error("adapter contract identity mismatch")
    allowed = set(ADAPTER_CONTRACT["ordered_sink_channels"])
    for index, sink in enumerate(decision.raw_sink_order, start=1):
        if sink.channel not in allowed:
            raise CatFuryFullPolicyV4Error(f"raw sink {index} has unknown channel")
        if not sink.source_ref:
            raise CatFuryFullPolicyV4Error(f"raw sink {index} lacks source ref")
    raw_queue = [
        sink.value for sink in decision.raw_sink_order if sink.channel == "swing_queue"
    ]
    if raw_queue:
        expected = (
            SwingQueueOp.CLEAVE
            if raw_queue[-1] == "顺劈斩"
            else SwingQueueOp.HEROIC_STRIKE
        )
        if decision.swing_queue is not expected:
            raise CatFuryFullPolicyV4Error("normalized queue disagrees with raw order")
    elif decision.swing_queue is not SwingQueueOp.KEEP:
        raise CatFuryFullPolicyV4Error("normalized queue has no raw sink")
    stop = any(sink.channel == "cast_control" for sink in decision.raw_sink_order)
    if stop != (decision.cast_control is CastControl.STOP_CAST):
        raise CatFuryFullPolicyV4Error("normalized cast-control lane mismatch")
    if decision.target is not TargetOp.KEEP:
        raise CatFuryFullPolicyV4Error("profile1 Target=0 cannot emit target lane")
    return decision


def _raw(decision: ExpertDecision) -> list[JSONMap]:
    return [sink.to_dict() for sink in decision.raw_sink_order]


def _base_combat(**changes: Any) -> FuryExpertState:
    state = FuryExpertState(
        rage=0.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.DUAL_WIELD,
        target_exists=True,
        target_is_boss=True,
        in_combat=True,
        in_melee_range=True,
        nearby_enemies=1,
        gcd_ready=True,
        current_stance=StanceOp.BERSERKER,
        has_battle_shout=True,
        battle_shout_remaining_s=600.0,
        flurry_talent=True,
        flurry_active=True,
        bloodthirst_known=True,
        bloodthirst_ready_in_s=10.0,
        whirlwind_ready_in_s=10.0,
        bloodrage_ready=False,
        mainhand_swing_remaining_s=1.0,
        execute_cost=15.0,
        heroic_strike_cost=15.0,
        cleave_cost=20.0,
        whirlwind_cost=25.0,
        nampower=True,
    )
    return replace(state, **changes)


def _state(**changes: Any) -> CatFuryFullPolicyStateV4:
    combat_changes = changes.pop("combat", {})
    return CatFuryFullPolicyStateV4(
        combat=_base_combat(**combat_changes),
        **changes,
    )


def _expected(
    channel: str, operation: str, value: str | None, source_ref: str
) -> JSONMap:
    return {
        "channel": channel,
        "operation": operation,
        "value": value,
        "source_ref": source_ref,
    }


def _slam_expected(source_ref: str) -> list[JSONMap]:
    return [
        _expected("cvar", "SetCVar", "NP_QueueCastTimeSpells=0", source_ref),
        _expected("cvar", "SetCVar", "NP_QueueInstantSpells=0", source_ref),
        _expected("gcd", "CastSpellByName", "猛击", source_ref),
        _expected("cvar", "SetCVar", "NP_QueueCastTimeSpells=1", source_ref),
        _expected("cvar", "SetCVar", "NP_QueueInstantSpells=1", source_ref),
    ]


def _synthetic_fixtures() -> list[tuple[str, CatFuryFullPolicyStateV4, list[JSONMap], tuple[str, ...]]]:
    """Literal source-oracle fixtures; expected sinks are not adapter output."""

    fixtures: list[tuple[str, CatFuryFullPolicyStateV4, list[JSONMap], tuple[str, ...]]] = []

    def add(
        fixture_id: str,
        state: CatFuryFullPolicyStateV4,
        expected: list[JSONMap],
        *branches: str,
    ) -> None:
        fixtures.append((fixture_id, state, expected, tuple(branches)))

    add(
        "attack_guard_then_banish_return",
        _state(autoattack_active=False, target_banished=True),
        [_expected("autoattack", "AttackTarget", "START", "WarriorFury.lua:86-87;CatLib.lua:107-112")],
        "START_ATTACK_GUARD",
        "BANISH_EARLY_RETURN",
    )
    add(
        "both_trinkets_source_order",
        _state(
            upper_trinket_supported=True,
            upper_trinket_cooldown_s=0.0,
            lower_trinket_supported=True,
            lower_trinket_cooldown_s=0.0,
        ),
        [
            _expected("item", "UseInventoryItem", "13", "WarriorFury.lua:113-119"),
            _expected("item", "UseInventoryItem", "14", "WarriorFury.lua:120-126"),
        ],
        "TRINKET_UPPER",
        "TRINKET_LOWER",
    )
    inventory = (
        CatInventoryItemV4("特效治疗石", 0, 1),
        CatInventoryItemV4("糖水茶", 1, 2),
        CatInventoryItemV4("诺达纳尔草药茶", 2, 3),
    )
    add(
        "low_health_inventory_helper_order",
        _state(player_health_pct=10.0, inventory_items=inventory),
        [
            _expected("item", "UseContainerItem", "0:1:特效治疗石", "WarriorFury.lua:133-137;CatLib.lua:1085-1107"),
            _expected("item", "UseContainerItem", "1:2:糖水茶", "WarriorFury.lua:138-141;CatLib.lua:1085-1107"),
            _expected("item", "UseContainerItem", "2:3:诺达纳尔草药茶", "WarriorFury.lua:138-141;CatLib.lua:1085-1107"),
        ],
        "HEALTHSTONE",
        "HERBAL_TEA_PAIR",
    )
    add(
        "battle_shout_return",
        _state(combat={"rage": 10.0, "has_battle_shout": False, "battle_shout_remaining_s": 0.0}),
        [_expected("gcd", "CastSpellByName", "战斗怒吼", "WarriorFury.lua:283-289")],
        "BATTLE_SHOUT_RETURN",
    )
    add(
        "berserker_stance_return",
        _state(combat={"current_stance": StanceOp.BATTLE}),
        [_expected("stance", "CastSpellByName", "狂暴姿态", "WarriorFury.lua:292-296")],
        "BERSERKER_STANCE_RETURN",
    )
    add(
        "unguarded_overpower_supplement",
        _state(
            overpower_proc_active=True,
            overpower_ready_within_1_5_s=True,
            combat={"rage": 5.0, "current_stance": StanceOp.BATTLE},
        ),
        [_expected("gcd", "CastSpellByName", "压制", "WarriorFury.lua:210-214")],
        "OVERPOWER_BATTLE_STANCE_SUPPLEMENT",
    )
    add(
        "out_of_combat_wait_reset_no_sink",
        _state(combat={"in_combat": False}),
        [],
        "OUT_OF_COMBAT_WAIT_RESET",
    )
    add(
        "duplicate_bloodrage_attempts",
        _state(combat={"rage": 20.0, "bloodrage_ready": True, "bloodthirst_ready_in_s": 1.0, "whirlwind_ready_in_s": 1.0}),
        [
            _expected("off_gcd", "QueueSpellByName", "血性狂暴", "WarriorFury.lua:299-301"),
            _expected("off_gcd", "QueueSpellByName", "血性狂暴", "WarriorFury.lua:302"),
        ],
        "BLOODRAGE_BT_ATTEMPT",
        "BLOODRAGE_WW_ATTEMPT",
    )
    add(
        "two_hand_single_next_swing",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "rage": 31.0, "mainhand_swing_remaining_s": 1.0}),
        [_expected("swing_queue", "QueueSpellByName", "英勇打击", "WarriorFury.lua:338-366")],
        "TWO_HAND_DYNAMIC_NEXT_SWING",
    )
    add(
        "two_hand_aoe_next_swing",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "rage": 36.0, "nearby_enemies": 2, "mainhand_swing_remaining_s": 1.0}),
        [_expected("swing_queue", "QueueSpellByName", "顺劈斩", "WarriorFury.lua:338-366")],
        "AOE_SCAN_AND_STRIKE_SELECT",
        "TWO_HAND_DYNAMIC_NEXT_SWING",
    )
    add(
        "dual_single_next_swing_then_bt",
        _state(combat={"rage": 61.0, "bloodthirst_ready_in_s": 1.0, "whirlwind_ready_in_s": 10.0}),
        [
            _expected("swing_queue", "QueueSpellByName", "英勇打击", "WarriorFury.lua:370-388"),
            _expected("gcd", "CastSpellByName", "嗜血", "WarriorFury.lua:580-583"),
        ],
        "DUAL_WIELD_DYNAMIC_NEXT_SWING",
        "DUAL_WIELD_NORMAL_PRIORITY",
    )
    add(
        "dual_aoe_execute_next_swing",
        _state(combat={"rage": 41.0, "nearby_enemies": 2, "target_health_pct": 19.0}),
        [
            _expected("swing_queue", "QueueSpellByName", "顺劈斩", "WarriorFury.lua:370-388"),
            _expected("gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:616"),
        ],
        "AOE_SCAN_AND_STRIKE_SELECT",
        "DUAL_WIELD_DYNAMIC_NEXT_SWING",
        "DUAL_WIELD_EXECUTE_PRIORITY",
    )
    add(
        "two_hand_aoe_stop_slam_whirlwind_offset",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "rage": 25.0, "nearby_enemies": 2, "whirlwind_ready_in_s": 1.0, "casting_slam": True}),
        [
            _expected("cast_control", "SpellStopCasting", None, "WarriorFury.lua:400-403"),
            _expected("gcd", "CastSpellByName", "旋风斩", "WarriorFury.lua:400-404"),
        ],
        "TWO_HAND_AOE_WHIRLWIND",
    )
    add(
        "two_hand_nonboss_execute_without_rage_guard",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "target_is_boss": False, "target_health_pct": 19.0, "casting_slam": True}),
        [
            _expected("cast_control", "SpellStopCasting", None, "WarriorFury.lua:407-410"),
            _expected("gcd", "CastSpellByName", "斩杀", "WarriorFury.lua:407-411"),
        ],
        "TWO_HAND_NONBOSS_EXECUTE",
    )

    timing_cases = (
        ("no_talent_early_ww", False, False, 1.0, 25.0, 50.0, 10.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:419-423", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_early_bt", False, False, 1.0, 30.0, 50.0, 1.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:426-428", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_early_execute", False, False, 1.0, 15.0, 19.0, 10.0, 10.0, EXECUTE, "斩杀", "WarriorFury.lua:431-433", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_mid_ww", False, False, 1.7, 25.0, 50.0, 10.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:436-440", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_mid_bt", False, False, 1.7, 30.0, 50.0, 1.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:443-445", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_mid_execute", False, False, 1.7, 15.0, 19.0, 10.0, 10.0, EXECUTE, "斩杀", "WarriorFury.lua:448-450", "TWO_HAND_NO_TALENT_TIMING"),
        ("flurry_early_ww", True, True, 1.0, 25.0, 50.0, 10.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:472-476", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_early_bt", True, True, 1.0, 30.0, 50.0, 1.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:479-481", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_early_execute", True, True, 1.0, 15.0, 19.0, 10.0, 10.0, EXECUTE, "斩杀", "WarriorFury.lua:484-486", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_mid_ww", True, True, 1.7, 25.0, 50.0, 10.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:489-493", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_mid_bt", True, True, 1.7, 30.0, 50.0, 1.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:496-498", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_mid_execute", True, True, 1.7, 15.0, 19.0, 10.0, 10.0, EXECUTE, "斩杀", "WarriorFury.lua:501-503", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
    )
    for fixture_id, talent, active, swing, rage, hp, bt, ww, action, localized, ref, branch in timing_cases:
        add(
            fixture_id,
            _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "flurry_talent": talent, "flurry_active": active, "mainhand_swing_remaining_s": swing, "rage": rage, "target_health_pct": hp, "bloodthirst_ready_in_s": bt, "whirlwind_ready_in_s": ww}),
            [_expected("gcd", "CastSpellByName", localized, ref)],
            branch,
        )

    slam_cases = (
        ("no_talent_mid_slam", False, False, 1.7, "WarriorFury.lua:453-455;CatLib.lua:939-948", "TWO_HAND_NO_TALENT_TIMING"),
        ("no_talent_late_slam", False, False, 2.5, "WarriorFury.lua:458-462;CatLib.lua:939-948", "TWO_HAND_NO_TALENT_TIMING"),
        ("flurry_mid_slam", True, True, 1.7, "WarriorFury.lua:506-508;CatLib.lua:939-948", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
        ("flurry_late_slam", True, True, 2.5, "WarriorFury.lua:511-515;CatLib.lua:939-948", "TWO_HAND_FLURRY_ACTIVE_TIMING"),
    )
    for fixture_id, talent, active, swing, ref, branch in slam_cases:
        add(
            fixture_id,
            _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "flurry_talent": talent, "flurry_active": active, "mainhand_swing_remaining_s": swing, "rage": 15.0}),
            _slam_expected(ref),
            branch,
        )

    inactive_cases = (
        ("no_flurry_whirlwind_offset", 25.0, 50.0, 10.0, 1.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:520-524"),
        ("no_flurry_execute_bt_offset", 45.0, 19.0, 1.0, 10.0, 1.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:527-530"),
        ("no_flurry_execute", 15.0, 19.0, 10.0, 10.0, 1.0, EXECUTE, "斩杀", "WarriorFury.lua:532-535"),
        ("no_flurry_normal_bt_offset", 30.0, 50.0, 1.0, 10.0, 1.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:537-540"),
        ("no_flurry_hamstring", 10.0, 50.0, 10.0, 10.0, 1.0, HAMSTRING, "断筋", "WarriorFury.lua:542-546"),
    )
    for fixture_id, rage, hp, bt, ww, swing, action, localized, ref in inactive_cases:
        add(
            fixture_id,
            _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "flurry_talent": True, "flurry_active": False, "mainhand_swing_remaining_s": swing, "rage": rage, "target_health_pct": hp, "bloodthirst_ready_in_s": bt, "whirlwind_ready_in_s": ww}),
            [_expected("gcd", "CastSpellByName", localized, ref)],
            "TWO_HAND_FLURRY_INACTIVE_PRIORITY",
        )
    no_flurry_slam_ref = "WarriorFury.lua:549-563;CatLib.lua:939-948"
    add(
        "no_flurry_slam_cvar_order",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "flurry_talent": True, "flurry_active": False, "mainhand_swing_remaining_s": 2.5, "rage": 15.0, "bloodthirst_ready_in_s": 1.0}),
        _slam_expected(no_flurry_slam_ref),
        "TWO_HAND_FLURRY_INACTIVE_PRIORITY",
    )
    direct_slam_ref = "WarriorFury.lua:506-508;CatLib.lua:939-948"
    add(
        "slam_without_nampower_direct_cast",
        _state(combat={"weapon_mode": WeaponMode.TWO_HAND, "flurry_talent": True, "flurry_active": True, "mainhand_swing_remaining_s": 1.7, "rage": 15.0, "nampower": False}),
        [_expected("gcd", "CastSpellByName", "猛击", direct_slam_ref)],
        "TWO_HAND_FLURRY_ACTIVE_TIMING",
    )
    add(
        "bloodrage_without_nampower_direct_calls",
        _state(combat={"rage": 20.0, "bloodrage_ready": True, "bloodthirst_ready_in_s": 1.0, "whirlwind_ready_in_s": 1.0, "nampower": False}),
        [
            _expected("off_gcd", "CastSpellByName", "血性狂暴", "WarriorFury.lua:299-301"),
            _expected("off_gcd", "CastSpellByName", "血性狂暴", "WarriorFury.lua:302"),
        ],
        "BLOODRAGE_BT_ATTEMPT",
        "BLOODRAGE_WW_ATTEMPT",
    )

    dual_cases = (
        ("dual_normal_bt_offset", 30.0, 50.0, 1.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:580-583", "DUAL_WIELD_NORMAL_PRIORITY"),
        ("dual_normal_ww_offset", 25.0, 50.0, 10.0, 1.0, WHIRLWIND, "旋风斩", "WarriorFury.lua:586-589", "DUAL_WIELD_NORMAL_PRIORITY"),
        ("dual_normal_hamstring", 10.0, 50.0, 10.0, 10.0, HAMSTRING, "断筋", "WarriorFury.lua:592-600", "DUAL_WIELD_NORMAL_PRIORITY"),
        ("dual_execute_bt_exact", 45.0, 19.0, 0.0, 10.0, BLOODTHIRST, "嗜血", "WarriorFury.lua:609-610", "DUAL_WIELD_EXECUTE_PRIORITY"),
        ("dual_execute", 15.0, 19.0, 1.0, 10.0, EXECUTE, "斩杀", "WarriorFury.lua:616", "DUAL_WIELD_EXECUTE_PRIORITY"),
    )
    for fixture_id, rage, hp, bt, ww, action, localized, ref, branch in dual_cases:
        add(
            fixture_id,
            _state(combat={"rage": rage, "target_health_pct": hp, "bloodthirst_ready_in_s": bt, "whirlwind_ready_in_s": ww}),
            [_expected("gcd", "CastSpellByName", localized, ref)],
            branch,
        )
    return fixtures


def _sequence_delta(expected: Sequence[Mapping[str, Any]], observed: Sequence[Mapping[str, Any]]) -> JSONMap:
    first_difference: int | None = None
    for index in range(max(len(expected), len(observed))):
        left = expected[index] if index < len(expected) else None
        right = observed[index] if index < len(observed) else None
        if left != right:
            first_difference = index
            break
    return {
        "matches": list(expected) == list(observed),
        "expected_count": len(expected),
        "observed_count": len(observed),
        "first_difference_zero_based": first_difference,
    }


def build_synthetic_differential_receipt_v4() -> JSONMap:
    adapter = CatFuryFullPolicyAdapterV4()
    legacy = CatFurySourceAdapter()
    rows: list[JSONMap] = []
    covered: set[str] = set()
    v4_matches = 0
    legacy_matches = 0
    for fixture_id, state, expected, branch_ids in _synthetic_fixtures():
        decision = validate_source_decision_v4(adapter.propose(state))
        observed = _raw(decision)
        delta = _sequence_delta(expected, observed)
        if not delta["matches"]:
            raise CatFuryFullPolicyV4Error(
                f"v4 synthetic source oracle mismatch in {fixture_id}: {delta}"
            )
        visited = set(decision.metadata["visited_branch_ids"])
        missing = sorted(set(branch_ids) - visited)
        if missing:
            raise CatFuryFullPolicyV4Error(
                f"fixture {fixture_id} did not visit declared branches {missing}"
            )
        covered.update(branch_ids)
        v4_matches += 1
        legacy_decision = legacy.propose(state.combat)
        legacy_raw = _raw(legacy_decision)
        legacy_delta = _sequence_delta(expected, legacy_raw)
        legacy_matches += int(bool(legacy_delta["matches"]))
        rows.append(
            {
                "fixture_id": fixture_id,
                "synthetic": True,
                "covered_branch_ids": list(branch_ids),
                "expected_source_raw_sink_order": expected,
                "full_policy_v4_raw_sink_order": observed,
                "full_policy_v4_delta": delta,
                "legacy_cat_fury_source_adapter_raw_sink_order": legacy_raw,
                "legacy_delta": legacy_delta,
                "client_trace_observed": False,
                "client_acceptance_observed": False,
                "server_outcome_observed": False,
            }
        )
    mapped = {
        str(row["branch_id"])
        for row in SOURCE_BRANCH_CATALOG
        if str(row["profile1_status"]).startswith("MAPPED_")
    }
    uncovered = sorted(mapped - covered)
    oracle_signatures = {
        (
            sink["channel"],
            sink["operation"],
            sink.get("value"),
            sink["source_ref"],
        )
        for row in rows
        for sink in row["expected_source_raw_sink_order"]
    }
    covered_sites = [
        str(site["site_id"])
        for site in REQUIRED_PROFILE1_RAW_SINK_SITES
        if (
            site["channel"],
            site["operation"],
            site.get("value"),
            site["source_ref"],
        )
        in oracle_signatures
    ]
    uncovered_sites = sorted(
        {str(site["site_id"]) for site in REQUIRED_PROFILE1_RAW_SINK_SITES}
        - set(covered_sites)
    )
    core: JSONMap = {
        "schema": TRACE_SCHEMA,
        "policy_id": POLICY_ID,
        "legacy_policy_id": LEGACY_POLICY_ID,
        "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
        "profile_semantic_sha256": EXPECTED_PROFILE1_SEMANTIC_SHA256,
        "synthetic": True,
        "source_execution": False,
        "fixtures": rows,
        "coverage": {
            "mapped_branch_ids": sorted(mapped),
            "covered_branch_ids": sorted(covered),
            "uncovered_mapped_branch_ids": uncovered,
            "full_policy_v4_fixture_count": len(rows),
            "full_policy_v4_exact_match_count": v4_matches,
            "legacy_exact_match_count": legacy_matches,
            "full_policy_v4_source_oracle_complete": not uncovered,
            "required_raw_sink_site_count": len(REQUIRED_PROFILE1_RAW_SINK_SITES),
            "covered_raw_sink_site_ids": sorted(covered_sites),
            "uncovered_raw_sink_site_ids": uncovered_sites,
            "full_policy_v4_raw_sink_site_coverage_complete": not uncovered_sites,
        },
        "comparison_ready": False,
        "runner_registration_authorized": False,
        "claim_boundary": "synthetic source oracle differential only; not observed in a game client",
    }
    if uncovered:
        raise CatFuryFullPolicyV4Error(
            f"synthetic fixture set does not cover mapped branches {uncovered}"
        )
    if uncovered_sites:
        raise CatFuryFullPolicyV4Error(
            f"synthetic fixture set does not cover raw sink sites {uncovered_sites}"
        )
    return {**core, "content_address": _content_address(core)}


def validate_synthetic_differential_receipt_v4(
    receipt: Mapping[str, Any],
) -> JSONMap:
    if not isinstance(receipt, Mapping):
        raise TypeError("receipt must be a mapping")
    result = json.loads(json.dumps(receipt, ensure_ascii=False))
    if result.get("schema") != TRACE_SCHEMA or result.get("policy_id") != POLICY_ID:
        raise CatFuryFullPolicyV4Error("synthetic receipt identity mismatch")
    address = result.get("content_address")
    core = {key: value for key, value in result.items() if key != "content_address"}
    if not isinstance(address, Mapping) or dict(address) != _content_address(core):
        raise CatFuryFullPolicyV4Error("synthetic receipt content address mismatch")
    if result.get("synthetic") is not True or result.get("source_execution") is not False:
        raise CatFuryFullPolicyV4Error("synthetic/source-execution boundary mismatch")
    if result.get("comparison_ready") is not False:
        raise CatFuryFullPolicyV4Error("synthetic receipt cannot self-promote")
    if result.get("runner_registration_authorized") is not False:
        raise CatFuryFullPolicyV4Error("synthetic receipt cannot authorize a runner")
    coverage = result.get("coverage")
    if not isinstance(coverage, Mapping):
        raise CatFuryFullPolicyV4Error("synthetic coverage summary missing")
    if coverage.get("uncovered_mapped_branch_ids") != []:
        raise CatFuryFullPolicyV4Error("mapped synthetic branch remains uncovered")
    if coverage.get("full_policy_v4_source_oracle_complete") is not True:
        raise CatFuryFullPolicyV4Error("source-oracle fixture coverage is incomplete")
    if coverage.get("uncovered_raw_sink_site_ids") != []:
        raise CatFuryFullPolicyV4Error("profile1 raw sink site remains uncovered")
    if coverage.get("full_policy_v4_raw_sink_site_coverage_complete") is not True:
        raise CatFuryFullPolicyV4Error("profile1 raw sink site coverage is incomplete")
    for row in result.get("fixtures", []):
        if not isinstance(row, Mapping):
            raise CatFuryFullPolicyV4Error("malformed synthetic fixture row")
        if row.get("client_trace_observed") is not False:
            raise CatFuryFullPolicyV4Error("synthetic row cannot claim a client trace")
        if row.get("client_acceptance_observed") is not False:
            raise CatFuryFullPolicyV4Error("synthetic row cannot claim acceptance")
        if row.get("server_outcome_observed") is not False:
            raise CatFuryFullPolicyV4Error("synthetic row cannot claim server outcome")
        delta = row.get("full_policy_v4_delta")
        if not isinstance(delta, Mapping) or delta.get("matches") is not True:
            raise CatFuryFullPolicyV4Error("v4 fixture differs from source oracle")
    return result


MANDATORY_BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("SOURCE_DERIVED_TRACE_IS_SYNTHETIC", "DIAGNOSTIC", "the v4 ordered trace is constructed from source fixtures, not captured from WoW.exe"),
    ("RUNTIME_LOAD_ATTESTATION_MISSING", "GAME_CLIENT", "no in-game receipt proves that this exact Cat tree and profile were loaded"),
    ("GAME_CLIENT_ORDERED_SINK_TRACE_MISSING", "GAME_CLIENT", "no hooked client ledger captures the actual ordered sinks from one Cat invocation"),
    ("CLIENT_ACCEPTANCE_TRACE_MISSING", "GAME_CLIENT", "raw calls are not bound to client accept/reject outcomes"),
    ("SERVER_OUTCOME_TRACE_MISSING", "SERVER", "server GO, resource, miss/hit, queue consumption, and damage are not bound to each attempt"),
    ("NP_QUEUE_INSTANT_SPELLS_SNAPSHOT_MISSING", "RUNTIME", "the published runtime snapshot omits NP_QueueInstantSpells although MPCastWithoutNampower mutates it"),
    ("FORMAL_SIMULATOR_EXECUTOR_NOT_VALIDATED", "SIMULATOR", "target, item, duplicate call, CVar, queue, stop-cast, stance, and GCD operations lack a v4 simulator execution receipt"),
    ("FORMAL_RUNNER_REGISTRATION_NOT_AUTHORIZED", "RUNNER", "this isolated diagnostic is not registered in a frozen evaluation runner"),
)


def _blockers(extra: Sequence[tuple[str, str, str]] = ()) -> list[JSONMap]:
    return [
        {
            "code": code,
            "scope": scope,
            "message": message,
            "comparison_fatal": True,
        }
        for code, scope, message in (*extra, *MANDATORY_BLOCKERS)
    ]


MINIMUM_FUTURE_GAME_COLLECTION: tuple[JSONMap, ...] = (
    {
        "requirement_id": "CAT_V4_LOAD_IDENTITY",
        "minimum": "one post-/reload start receipt and one logout or /reload end receipt",
        "must_bind": ["Cat TOC closure SHA-256", "WarriorFury.lua SHA-256", "Cat.lua raw SHA-256", "profile1 semantic SHA-256", "runtime snapshot SHA-256", "character talent/equipment identity", "Nampower/SuperWoW/UnitXP identities and relevant CVars"],
    },
    {
        "requirement_id": "CAT_V4_ORDERED_MULTI_SINK",
        "minimum": "one attempt-ID/order-indexed trace for each distinct reachable sink sequence",
        "scenario_groups": ["autoattack guard and banish return", "upper then lower trinket", "healthstone then both tea lookups", "battle-shout early return", "stance early return", "double Bloodrage then next-swing then primary skill", "two-hand Slam CVar sequence", "Slam stop then Whirlwind or Execute", "dual-wield normal and execute priorities"],
    },
    {
        "requirement_id": "CAT_V4_CLIENT_ACCEPTANCE",
        "minimum": "every ordered raw attempt in the scenario trace receives a same-attempt accepted/rejected/unknown client disposition",
        "must_include": ["Nampower queue event or explicit absence", "spell START/GO client events", "item/equipment/CVar result", "next-swing queued/cancelled/consumed state", "GCD and rage immediately before and after"],
    },
    {
        "requirement_id": "CAT_V4_SERVER_OUTCOME",
        "minimum": "every accepted spell or queued swing is joined to terminal server evidence",
        "must_include": ["server GO or failure", "resource spend/gain", "hit/crit/miss/dodge/parry", "damage or non-damage result", "target GUID", "server timestamp", "attempt ID"],
    },
)


def _load_snapshot(path: str | Path) -> tuple[JSONMap, JSONMap]:
    payload, _ = _stable_read(path, "runtime snapshot")
    raw_sha = _sha256(payload)
    if raw_sha != EXPECTED_SNAPSHOT_FILE_SHA256:
        raise CatFuryFullPolicyV4Error(
            f"runtime snapshot file SHA mismatch: expected {EXPECTED_SNAPSHOT_FILE_SHA256}, got {raw_sha}"
        )
    document = _strict_json(payload, "runtime snapshot")
    if document.get("schema") != "fury_expert_runtime_snapshot/v1":
        raise CatFuryFullPolicyV4Error("runtime snapshot schema mismatch")
    core = dict(document)
    supplied = core.pop("snapshot_sha256", None)
    if supplied != _sha256(_canonical_bytes(core)) or supplied != EXPECTED_SNAPSHOT_SHA256:
        raise CatFuryFullPolicyV4Error("runtime snapshot content address mismatch")
    cat = document.get("cat_profile1")
    inputs = document.get("inputs")
    authority = document.get("authority")
    if not all(isinstance(value, Mapping) for value in (cat, inputs, authority)):
        raise CatFuryFullPolicyV4Error("runtime snapshot Cat identity fields missing")
    cat_input = inputs.get("cat_savedvariables")
    if not isinstance(cat_input, Mapping):
        raise CatFuryFullPolicyV4Error("runtime snapshot Cat.lua input missing")
    if cat.get("profile_semantic_sha256") != EXPECTED_PROFILE1_SEMANTIC_SHA256:
        raise CatFuryFullPolicyV4Error("runtime snapshot profile1 semantic SHA mismatch")
    if cat_input.get("sha256") != EXPECTED_SNAPSHOT_CAT_SAVEDVARIABLES_SHA256:
        raise CatFuryFullPolicyV4Error("runtime snapshot historical Cat.lua SHA mismatch")
    for name in (
        "source_execution_observed",
        "client_acceptance_observed",
        "server_outcome_observed",
        "full_policy_simulator_adapter_complete",
        "comparison_eligible",
    ):
        if authority.get(name) is not False:
            raise CatFuryFullPolicyV4Error(
                f"runtime snapshot authority.{name} must remain false"
            )
    return document, {
        "logical_path": "%PROJECT_ROOT%\\offline_data\\expert_runtime_snapshots\\v1\\fury_expert_runtime_snapshot_v1.<semantic_sha256>.json",
        "file_sha256": raw_sha,
        "size_bytes": len(payload),
        "snapshot_sha256": supplied,
    }


def _current_savedvariables(
    path: str | Path,
    manifest: Mapping[str, Any],
    expected_sha256: str,
) -> tuple[JSONMap, JSONMap]:
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected_savedvariables_sha256 must be a lowercase SHA-256")
    payload, _ = _stable_read(path, "current Cat SavedVariables")
    raw_sha = _sha256(payload)
    parsed = _parse_savedvariables(payload, "current Cat SavedVariables")
    profile = _cat_profile(parsed, manifest)
    return profile, {
        "logical_path": "%WOW_CHARACTER_SAVEDVARIABLES%\\Cat.lua",
        "sha256": raw_sha,
        "expected_sha256": expected_sha256,
        "size_bytes": len(payload),
        "expected_size_bytes": EXPECTED_CURRENT_SAVEDVARIABLES_SIZE,
        "byte_identity_matches_v4_pin": raw_sha == expected_sha256,
    }


def build_branch_audit_v4() -> JSONMap:
    legacy_path = Path(sys.modules[CatFurySourceAdapter.__module__].__file__).resolve()
    legacy_payload, _ = _stable_read(legacy_path, "legacy Cat adapter")
    legacy_sha = _sha256(legacy_payload)
    core: JSONMap = {
        "schema": BRANCH_AUDIT_SCHEMA,
        "policy_id": POLICY_ID,
        "profile_semantic_sha256": EXPECTED_PROFILE1_SEMANTIC_SHA256,
        "warrior_fury_sha256": EXPECTED_WARRIOR_FURY_SHA256,
        "legacy_adapter": {
            "policy_id": LEGACY_POLICY_ID,
            "file_sha256": legacy_sha,
            "expected_file_sha256": EXPECTED_LEGACY_ADAPTER_FILE_SHA256,
            "identity_matches": legacy_sha == EXPECTED_LEGACY_ADAPTER_FILE_SHA256,
            "declared_scope": "bounded core Fury translation",
            "full_profile1_branch_coverage": False,
        },
        "full_policy_v4": {
            "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
            "source_derived_only": True,
            "profile_disabled_branches_not_fabricated": True,
            "known_unmapped_reachable_branch_ids": [],
            "comparison_ready": False,
        },
        "branches": [dict(row) for row in SOURCE_BRANCH_CATALOG],
        "legacy_gap_summary": {
            "guard_or_cooldown_semantic_drift": [
                "START_ATTACK_GUARD",
                "TWO_HAND_AOE_WHIRLWIND",
                "TWO_HAND_FLURRY_ACTIVE_TIMING",
                "TWO_HAND_FLURRY_INACTIVE_PRIORITY",
                "DUAL_WIELD_NORMAL_PRIORITY",
            ],
            "missing_active_prelude_or_utility": [
                "BANISH_EARLY_RETURN",
                "TRINKET_UPPER",
                "TRINKET_LOWER",
                "HEALTHSTONE",
                "HERBAL_TEA_PAIR",
            ],
            "ordered_sink_loss": [
                "START_ATTACK_GUARD",
                "BLOODRAGE_WW_ATTEMPT",
                "TWO_HAND_NO_TALENT_TIMING",
                "TWO_HAND_FLURRY_ACTIVE_TIMING",
                "TWO_HAND_FLURRY_INACTIVE_PRIORITY",
            ],
            "known_v4_state_gap": [],
        },
        "lineage": {
            "frozen_coverage_v2_modified": False,
            "frozen_coverage_v2_reused_as_live_authority": False,
            "reason": "v4 performs a fresh isolated source audit; historical frozen artifacts remain immutable",
        },
    }
    return {**core, "content_address": _content_address(core)}


def build_readiness_report_v4(
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES,
    runtime_snapshot_path: str | Path = DEFAULT_RUNTIME_SNAPSHOT,
    expected_savedvariables_sha256: str = EXPECTED_CURRENT_SAVEDVARIABLES_SHA256,
) -> JSONMap:
    extra: list[tuple[str, str, str]] = []
    source_status = "VERIFIED"
    try:
        manifest_payload, _ = _stable_read(manifest_path, "Cat source manifest")
        if _sha256(manifest_payload) != EXPECTED_MANIFEST_SHA256:
            raise CatFuryFullPolicyV4Error("Cat source manifest SHA mismatch")
        manifest = load_manifest(manifest_path)
        source_verification = verify_source_tree(source_root, manifest)
    except Exception as error:
        manifest = {}
        source_verification = {
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error": str(error),
        }
        source_status = "FAILED"
        extra.append(("CAT_SOURCE_IDENTITY_VERIFICATION_FAILED", "SOURCE", str(error)))

    current_profile: JSONMap = {}
    current_identity: JSONMap = {"status": "FAILED"}
    if manifest:
        try:
            current_profile, current_identity = _current_savedvariables(
                savedvariables_path, manifest, expected_savedvariables_sha256
            )
            current_identity["status"] = "VERIFIED_READ_ONLY"
            if not current_identity["byte_identity_matches_v4_pin"]:
                extra.append(("CURRENT_SAVEDVARIABLES_IDENTITY_DRIFT", "PROFILE", "current Cat.lua bytes differ from the v4 read-only pin"))
            if current_identity["size_bytes"] != EXPECTED_CURRENT_SAVEDVARIABLES_SIZE:
                extra.append(("CURRENT_SAVEDVARIABLES_SIZE_DRIFT", "PROFILE", "current Cat.lua size differs from the v4 read-only pin"))
            if current_profile.get("profile_semantic_sha256") != EXPECTED_PROFILE1_SEMANTIC_SHA256:
                extra.append(("CURRENT_PROFILE1_SEMANTIC_DRIFT", "PROFILE", "current profile1 semantics differ from the bound adapter profile"))
        except Exception as error:
            current_identity = {
                "status": "FAILED",
                "error_type": type(error).__name__,
                "error": str(error),
            }
            extra.append(("CURRENT_SAVEDVARIABLES_READ_OR_PARSE_FAILED", "PROFILE", str(error)))

    snapshot: JSONMap = {}
    snapshot_identity: JSONMap = {"status": "FAILED"}
    try:
        snapshot, snapshot_identity = _load_snapshot(runtime_snapshot_path)
        snapshot_identity["status"] = "VERIFIED_READ_ONLY"
    except Exception as error:
        snapshot_identity = {
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error": str(error),
        }
        extra.append(("RUNTIME_SNAPSHOT_IDENTITY_FAILED", "RUNTIME", str(error)))

    snapshot_cat_raw = None
    snapshot_profile_sha = None
    if snapshot:
        snapshot_cat_raw = snapshot["inputs"]["cat_savedvariables"]["sha256"]
        snapshot_profile_sha = snapshot["cat_profile1"]["profile_semantic_sha256"]
    current_raw = current_identity.get("sha256")
    raw_matches_snapshot = bool(current_raw and current_raw == snapshot_cat_raw)
    semantic_matches_snapshot = bool(
        current_profile
        and current_profile.get("profile_semantic_sha256") == snapshot_profile_sha
    )
    if snapshot and current_profile and not raw_matches_snapshot:
        extra.append(("CURRENT_SAVEDVARIABLES_BYTES_DIFFER_FROM_EXISTING_SNAPSHOT", "RUNTIME", "the existing snapshot binds older Cat.lua bytes; matching profile1 semantics do not erase whole-file drift"))

    branch_audit = build_branch_audit_v4()
    receipt = validate_synthetic_differential_receipt_v4(
        build_synthetic_differential_receipt_v4()
    )
    source_diagnostic = bool(
        source_status == "VERIFIED"
        and current_identity.get("byte_identity_matches_v4_pin") is True
        and current_profile.get("profile_semantic_sha256")
        == EXPECTED_PROFILE1_SEMANTIC_SHA256
        and snapshot_identity.get("status") == "VERIFIED_READ_ONLY"
        and receipt["coverage"]["full_policy_v4_source_oracle_complete"]
    )
    adapter_path = Path(__file__).resolve(strict=True)
    adapter_payload, _ = _stable_read(adapter_path, "Cat v4 adapter module")
    core: JSONMap = {
        "schema": SCHEMA,
        "policy_id": POLICY_ID,
        "deployed_policy_identity": LEGACY_POLICY_ID,
        "source_identity": {
            "manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "tree_sha256": EXPECTED_TREE["sha256"],
            "toc_closure_sha256": EXPECTED_TOC_CLOSURE["sha256"],
            "warrior_fury_sha256": EXPECTED_WARRIOR_FURY_SHA256,
            "status": source_status,
            "verification": source_verification,
        },
        "savedvariables_identity": {
            "current": current_identity,
            "current_profile1": current_profile,
            "existing_snapshot_input_sha256": snapshot_cat_raw,
            "current_raw_matches_existing_snapshot": raw_matches_snapshot,
            "current_profile1_semantics_match_existing_snapshot": semantic_matches_snapshot,
            "whole_file_drift_is_not_ignored": True,
        },
        "runtime_snapshot_identity": snapshot_identity,
        "adapter_identity": {
            "contract_id": ADAPTER_CONTRACT_ID,
            "contract_sha256": ADAPTER_CONTRACT_SHA256,
            "module_logical_path": "%PROJECT_ROOT%\\o2o_dps\\cat_fury_full_policy_readiness_v4.py",
            "module_sha256": _sha256(adapter_payload),
        },
        "branch_audit": {
            "schema": branch_audit["schema"],
            "content_sha256": branch_audit["content_address"]["sha256"],
            "legacy_full_profile1_branch_coverage": False,
            "v4_known_unmapped_reachable_branch_ids": [],
        },
        "synthetic_differential_receipt": {
            "schema": receipt["schema"],
            "content_sha256": receipt["content_address"]["sha256"],
            "fixture_count": receipt["coverage"]["full_policy_v4_fixture_count"],
            "v4_exact_match_count": receipt["coverage"]["full_policy_v4_exact_match_count"],
            "legacy_exact_match_count": receipt["coverage"]["legacy_exact_match_count"],
            "source_execution": False,
        },
        "readiness": {
            "source_identity": source_status == "VERIFIED",
            "current_savedvariables_byte_identity": current_identity.get("byte_identity_matches_v4_pin") is True,
            "profile1_semantic_identity": current_profile.get("profile_semantic_sha256") == EXPECTED_PROFILE1_SEMANTIC_SHA256,
            "existing_runtime_snapshot_identity": snapshot_identity.get("status") == "VERIFIED_READ_ONLY",
            "current_raw_matches_existing_snapshot": raw_matches_snapshot,
            "source_full_policy_diagnostic": source_diagnostic,
            "game_client_load_attestation": False,
            "game_client_ordered_sink_trace": False,
            "client_acceptance_trace": False,
            "server_outcome_trace": False,
            "formal_simulator_executor": False,
            "formal_runner_registration": False,
        },
        "blockers": _blockers(extra),
        "minimum_future_game_collection": [dict(row) for row in MINIMUM_FUTURE_GAME_COLLECTION],
        "source_derived_diagnostic_executable": source_diagnostic,
        "game_runtime_closed": False,
        "comparison_ready": False,
        "eligible_for_independent_vote": False,
        "runner_registration_authorized": False,
        "formal_runner_registry_modified": False,
        "scientific_run_launched": False,
        "claim_boundary": "source/profile diagnostic plus synthetic ordered-sink differential only; no game-client or DPS comparison claim",
    }
    report = {**core, "content_address": _content_address(core)}
    return validate_readiness_report_v4(report)


def validate_readiness_report_v4(report: Mapping[str, Any]) -> JSONMap:
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    result = json.loads(json.dumps(report, ensure_ascii=False))
    if result.get("schema") != SCHEMA or result.get("policy_id") != POLICY_ID:
        raise CatFuryFullPolicyV4Error("readiness identity mismatch")
    core = {key: value for key, value in result.items() if key != "content_address"}
    if result.get("content_address") != _content_address(core):
        raise CatFuryFullPolicyV4Error("readiness content address mismatch")
    for name in (
        "game_runtime_closed",
        "comparison_ready",
        "eligible_for_independent_vote",
        "runner_registration_authorized",
        "formal_runner_registry_modified",
        "scientific_run_launched",
    ):
        if result.get(name) is not False:
            raise CatFuryFullPolicyV4Error(f"{name} must remain false")
    readiness = result.get("readiness")
    if not isinstance(readiness, Mapping):
        raise CatFuryFullPolicyV4Error("readiness fields missing")
    for name in (
        "game_client_load_attestation",
        "game_client_ordered_sink_trace",
        "client_acceptance_trace",
        "server_outcome_trace",
        "formal_simulator_executor",
        "formal_runner_registration",
    ):
        if readiness.get(name) is not False:
            raise CatFuryFullPolicyV4Error(f"runtime field {name} must remain false")
    codes = {
        row.get("code")
        for row in result.get("blockers", [])
        if isinstance(row, Mapping)
    }
    mandatory = {code for code, _, _ in MANDATORY_BLOCKERS}
    if not mandatory.issubset(codes):
        raise CatFuryFullPolicyV4Error("mandatory typed blocker missing")
    if len(result.get("minimum_future_game_collection", [])) != len(
        MINIMUM_FUTURE_GAME_COLLECTION
    ):
        raise CatFuryFullPolicyV4Error("minimum game collection contract drifted")
    return result


def serialize_readiness_report_v4(report: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_readiness_report_v4(report)) + b"\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--savedvariables", type=Path, default=DEFAULT_SAVEDVARIABLES)
    parser.add_argument("--runtime-snapshot", type=Path, default=DEFAULT_RUNTIME_SNAPSHOT)
    parser.add_argument("--branch-audit", action="store_true")
    parser.add_argument("--synthetic-receipt", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.branch_audit:
            document = build_branch_audit_v4()
        elif args.synthetic_receipt:
            document = validate_synthetic_differential_receipt_v4(
                build_synthetic_differential_receipt_v4()
            )
        else:
            document = build_readiness_report_v4(
                source_root=args.source_root,
                manifest_path=args.manifest,
                savedvariables_path=args.savedvariables,
                runtime_snapshot_path=args.runtime_snapshot,
            )
    except (CatFuryFullPolicyV4Error, OSError, ValueError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(_canonical_bytes(document) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADAPTER_CONTRACT",
    "ADAPTER_CONTRACT_SHA256",
    "BRANCH_AUDIT_SCHEMA",
    "CONTENT_ADDRESS_ALGORITHM",
    "DEFAULT_RUNTIME_SNAPSHOT",
    "DEFAULT_SAVEDVARIABLES",
    "DEFAULT_SOURCE_ROOT",
    "EXPECTED_CURRENT_SAVEDVARIABLES_SHA256",
    "EXPECTED_PROFILE1_SEMANTIC_SHA256",
    "LEGACY_POLICY_ID",
    "MANDATORY_BLOCKERS",
    "MINIMUM_FUTURE_GAME_COLLECTION",
    "POLICY_ID",
    "REQUIRED_PROFILE1_RAW_SINK_SITES",
    "SCHEMA",
    "SOURCE_BRANCH_CATALOG",
    "STATE_SCHEMA",
    "TRACE_SCHEMA",
    "CatFuryFullPolicyAdapterV4",
    "CatFuryFullPolicyStateV4",
    "CatFuryFullPolicyV4Error",
    "CatInventoryItemV4",
    "build_branch_audit_v4",
    "build_readiness_report_v4",
    "build_synthetic_differential_receipt_v4",
    "main",
    "serialize_readiness_report_v4",
    "validate_readiness_report_v4",
    "validate_source_decision_v4",
    "validate_synthetic_differential_receipt_v4",
]
