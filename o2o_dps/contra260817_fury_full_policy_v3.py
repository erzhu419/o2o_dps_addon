"""Contra260817 Fury full-policy source adapter and fail-closed readiness gate.

This module is deliberately version-isolated from the deployed Contra adapter
and from the v2/v3 rollout runners.  It translates the readable
``Contra_new`` package as source; it does not execute the Lua and it does not
claim that the package is the profile used by a player.

The adapter covers the level-60 raid-mode ``/contra c`` traversal for both
two-hand and dual-wield Fury.  It retains the important non-action semantics
that a first-action projection loses:

* the five-yard A/B router and the source's target-selection helper;
* target, auto-attack, stop-cast, spell, next-swing, item, equipment, and
  stance sinks in source order;
* same-invocation multiple attempts and Lua operator-precedence bugs;
* interrupt and Sunder helper returns, even when ``ContraZSCast`` itself emits
  no client call; and
* the stale entry-target snapshot used by the rotation after autoselection,
  separated from the post-selection snapshots used by helper functions.

The local package is known incomplete and the per-character ``ContraDB`` is
absent.  Consequently this module exposes a source-default diagnostic profile
and an explicit-profile constructor, but every proposal remains non-voting.
The readiness document is content addressed and cannot be promoted by caller
booleans or self-reported hashes.  Runtime load, the actual profile, client
acceptance, server outcomes, rollout registration, and simulator operation
coverage must be closed by later independent evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .contra260817_source_manifest_v1 import (
    CODE_MANIFEST_SHA256,
    DEFAULT_MANIFEST,
    DEFAULT_SOURCE_ROOT,
    EXPECTED_MANIFEST_SHA256,
    verify_contra260817_source_package,
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
from .fury_contra_adapter_v2 import (
    BROTHERHOOD_SET_ITEMS,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
    derive_zssdw_from_loadout,
)
from .fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    DEATH_WISH,
    EXECUTE,
    HAMSTRING,
    PUMMEL,
    SLAM,
    SUNDER_ARMOR,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
    _DecisionBuilder,
)


JSONMap = dict[str, Any]
SCHEMA = "contra260817_fury_full_policy/v3"
STATE_SCHEMA = "contra260817_fury_full_policy_state/v3"
PROFILE_SCHEMA = "contra260817_fury_full_policy_profile/v3"
READINESS_SCHEMA = "contra260817_fury_full_policy_readiness/v3"
TRACE_SCHEMA = "contra260817_fury_source_trace/v3"
POLICY_ID = "contra260817.fury.full_policy.source_diagnostic.v3"
REQUIRED_BASELINE_POLICY_ID = "contra260817.fury.source_candidate"
ADAPTER_CONTRACT_ID = "contra260817.fury.full_policy.adapter.v3"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _finite_range(
    value: Any,
    minimum: float,
    maximum: float | None,
    strict_minimum: bool = False,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    if strict_minimum and number <= minimum:
        return False
    if not strict_minimum and number < minimum:
        return False
    return maximum is None or number <= maximum


def _percent(value: Any, label: str) -> float:
    if not _finite_range(value, 0.0, 100.0):
        raise ValueError(f"{label} must be a finite percent")
    return float(value)


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _lower_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value

SOURCE_DEFAULT_DB_SHA256 = (
    "597062fcdb8673153397e94e2f8564eceaf1131bde8755165bd8a2759dee617b"
)
ONLOGIN_SHA256 = (
    "8ee56f9c584e4360242607bc08529b472653a515ca988e545a71e6e01d001430"
)
WARRIOR_SOURCE_SHA256 = (
    "a0f54d2a62e84084bb4d631f09e0173213190f4bea1eba670e6bfc1cd2b99713"
)
MACRO_SOURCE_SHA256 = (
    "830109cc35c21b68fac4965875ac5cd370e6060749e5e3f4d7a795cd6f5a66a2"
)
LIB_SOURCE_SHA256 = (
    "091fc679d11be3a0e86e69095c3796f1eb41bff8c19123bf1673f4acd73f61e7"
)

OVERPOWER = "warrior.overpower"
BERSERKER_RAGE = "warrior.berserker_rage"
CONCUSSION_BLOW = "warrior.concussion_blow"
DEMORALIZING_SHOUT = "warrior.demoralizing_shout"
RECKLESSNESS = "warrior.recklessness"
SHIELD_BASH = "warrior.shield_bash"
SHIELD_WALL = "warrior.shield_wall"
LAST_STAND = "warrior.last_stand"
SHIELD_BLOCK = "warrior.shield_block"
PERCEPTION = "racial.perception"
BLOOD_FURY = "racial.blood_fury"
BERSERKING = "racial.berserking"

_LOCALIZED_ACTIONS: Mapping[str, str] = {
    "战斗怒吼": BATTLE_SHOUT,
    "血性狂暴": BLOODRAGE,
    "嗜血": BLOODTHIRST,
    "斩杀": EXECUTE,
    "断筋": HAMSTRING,
    "拳击": PUMMEL,
    "盾击": SHIELD_BASH,
    "猛击": SLAM,
    "破甲攻击": SUNDER_ARMOR,
    "旋风斩": WHIRLWIND,
    "压制": OVERPOWER,
    "狂暴之怒": BERSERKER_RAGE,
    "震荡猛击": CONCUSSION_BLOW,
    "挫志怒吼": DEMORALIZING_SHOUT,
    "鲁莽": RECKLESSNESS,
    "死亡之愿": DEATH_WISH,
    "盾墙": SHIELD_WALL,
    "破釜沉舟": LAST_STAND,
    "盾牌格挡": SHIELD_BLOCK,
    "感知": PERCEPTION,
    "血性狂怒": BLOOD_FURY,
    "狂暴": BERSERKING,
}

_STANCE_NAMES: Mapping[str, StanceOp] = {
    "战斗姿态": StanceOp.BATTLE,
    "防御姿态": StanceOp.DEFENSIVE,
    "狂暴姿态": StanceOp.BERSERKER,
}

_OFF_GCD_NAMES = frozenset(
    {
        "血性狂暴",
        "狂暴之怒",
        "鲁莽",
        "死亡之愿",
        "感知",
        "血性狂怒",
        "狂暴",
    }
)

_BURST_TRIGGER_BUFF: Mapping[str, str] = {
    "强效怒气药水": "强效怒气",
    "鲁莽": "鲁莽",
    "死亡之愿": "死亡之愿",
    "横扫攻击": "横扫攻击",
    "屠龙者的纹章": "屠龙者的纹章",
    "蜘蛛之吻": "蜘蛛之吻",
}


class Contra260817FullPolicyV3Error(RuntimeError):
    """A v3 source/profile/readiness invariant was violated."""


class Contra260817FuryTalentV3(str, Enum):
    TWO_HAND = "双手狂暴"
    DUAL_WIELD = "双持狂暴"


class ProfileOriginV3(str, Enum):
    SOURCE_DEFAULT_DIAGNOSTIC = "SOURCE_DEFAULT_DIAGNOSTIC"
    EXPLICIT_ARTIFACT_UNATTESTED = "EXPLICIT_ARTIFACT_UNATTESTED"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


@dataclass(frozen=True)
class BurstStageV3:
    manual_enabled: bool
    trigger_skill: str
    auto_enabled: bool
    boss_health_threshold_pct: float
    recklessness: bool
    death_wish: bool
    racial_triplet: bool
    slot_13: bool
    slot_14: bool
    mighty_rage_potion: bool
    juju_flurry: bool
    goblin_sapper_charge: bool
    haste_potion: bool
    rage_potion: bool
    elixir_of_rapid_growth: bool

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if name in {"trigger_skill", "boss_health_threshold_pct"}:
                continue
            if not isinstance(value, bool):
                raise TypeError(f"BurstStageV3.{name} must be boolean")
        _nonempty(self.trigger_skill, "BurstStageV3.trigger_skill")
        _percent(
            self.boss_health_threshold_pct,
            "BurstStageV3.boss_health_threshold_pct",
        )

    def to_dict(self) -> JSONMap:
        return {
            "manual_enabled": self.manual_enabled,
            "trigger_skill": self.trigger_skill,
            "auto_enabled": self.auto_enabled,
            "boss_health_threshold_pct": float(self.boss_health_threshold_pct),
            "recklessness": self.recklessness,
            "death_wish": self.death_wish,
            "racial_triplet": self.racial_triplet,
            "slot_13": self.slot_13,
            "slot_14": self.slot_14,
            "mighty_rage_potion": self.mighty_rage_potion,
            "juju_flurry": self.juju_flurry,
            "goblin_sapper_charge": self.goblin_sapper_charge,
            "haste_potion": self.haste_potion,
            "rage_potion": self.rage_potion,
            "elixir_of_rapid_growth": self.elixir_of_rapid_growth,
        }


@dataclass(frozen=True)
class SurvivalProfileV3:
    shield_wall: bool
    shield_wall_below_pct: float
    last_stand: bool
    last_stand_below_pct: float
    healthstone: bool
    healthstone_below_pct: float
    healing_potion: bool
    healing_potion_below_pct: float
    herbal_tea: bool
    herbal_tea_below_pct: float
    boss_ot_weapon_swap: bool
    output_mainhand: str
    output_offhand: str
    defensive_mainhand: str
    defensive_offhand: str

    def __post_init__(self) -> None:
        for name in (
            "shield_wall",
            "last_stand",
            "healthstone",
            "healing_potion",
            "herbal_tea",
            "boss_ot_weapon_swap",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"SurvivalProfileV3.{name} must be boolean")
        for name in (
            "shield_wall_below_pct",
            "last_stand_below_pct",
            "healthstone_below_pct",
            "healing_potion_below_pct",
            "herbal_tea_below_pct",
        ):
            _percent(getattr(self, name), f"SurvivalProfileV3.{name}")
        for name in (
            "output_mainhand",
            "output_offhand",
            "defensive_mainhand",
            "defensive_offhand",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"SurvivalProfileV3.{name} must be a string")

    def to_dict(self) -> JSONMap:
        return {
            "shield_wall": self.shield_wall,
            "shield_wall_below_pct": float(self.shield_wall_below_pct),
            "last_stand": self.last_stand,
            "last_stand_below_pct": float(self.last_stand_below_pct),
            "healthstone": self.healthstone,
            "healthstone_below_pct": float(self.healthstone_below_pct),
            "healing_potion": self.healing_potion,
            "healing_potion_below_pct": float(self.healing_potion_below_pct),
            "herbal_tea": self.herbal_tea,
            "herbal_tea_below_pct": float(self.herbal_tea_below_pct),
            "boss_ot_weapon_swap": self.boss_ot_weapon_swap,
            "output_mainhand": self.output_mainhand,
            "output_offhand": self.output_offhand,
            "defensive_mainhand": self.defensive_mainhand,
            "defensive_offhand": self.defensive_offhand,
        }


@dataclass(frozen=True)
class Contra260817FuryProfileV3:
    """Every raid-mode Fury button read by the translated source surface."""

    profile_id: str
    origin: ProfileOriginV3
    source_version: int
    mode: str
    autoselect: bool
    xuanfeng: bool
    burst_enabled: bool
    survive_enabled: bool
    interrupt_enabled: bool
    sunder_enabled: bool
    sunder_mode: str
    reserve_ten_rage_for_interrupt: bool
    interrupt_all: bool
    interrupt_named: bool
    interrupt_spell_name: str
    battle_shout: bool
    overpower: bool
    bloodrage: bool
    berserker_rage: bool
    concussion_blow: bool
    thunder_clap: bool
    demoralizing_shout: bool
    burst_stages: tuple[BurstStageV3, BurstStageV3]
    survival: SurvivalProfileV3
    evidence_sha256: tuple[str, ...]
    profile_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        if not isinstance(self.origin, ProfileOriginV3):
            raise TypeError("origin must be ProfileOriginV3")
        if isinstance(self.source_version, bool) or not isinstance(
            self.source_version, int
        ) or self.source_version <= 0:
            raise ValueError("source_version must be a positive integer")
        if self.mode != "副本模式":
            raise ValueError("v3 translates only the raid-mode Fury policy")
        for name in (
            "autoselect",
            "xuanfeng",
            "burst_enabled",
            "survive_enabled",
            "interrupt_enabled",
            "sunder_enabled",
            "reserve_ten_rage_for_interrupt",
            "interrupt_all",
            "interrupt_named",
            "battle_shout",
            "overpower",
            "bloodrage",
            "berserker_rage",
            "concussion_blow",
            "thunder_clap",
            "demoralizing_shout",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if self.sunder_mode not in {"startone", "keepfive"}:
            raise ValueError("sunder_mode must be startone or keepfive")
        _nonempty(self.interrupt_spell_name, "interrupt_spell_name")
        if not isinstance(self.burst_stages, tuple) or len(self.burst_stages) != 2:
            raise TypeError("burst_stages must contain exactly two stages")
        if any(not isinstance(stage, BurstStageV3) for stage in self.burst_stages):
            raise TypeError("burst_stages must contain BurstStageV3 values")
        if not isinstance(self.survival, SurvivalProfileV3):
            raise TypeError("survival must be SurvivalProfileV3")
        if not isinstance(self.evidence_sha256, tuple) or not self.evidence_sha256:
            raise ValueError("evidence_sha256 must be a nonempty tuple")
        for digest in self.evidence_sha256:
            _lower_sha(digest, "evidence_sha256")
        if self.profile_artifact_sha256 is not None:
            _lower_sha(self.profile_artifact_sha256, "profile_artifact_sha256")
        if (
            self.origin is ProfileOriginV3.EXPLICIT_ARTIFACT_UNATTESTED
            and self.profile_artifact_sha256 is None
        ):
            raise ValueError(
                "EXPLICIT_ARTIFACT_UNATTESTED requires profile_artifact_sha256"
            )

    def semantic_payload(self) -> JSONMap:
        return {
            "schema": PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "origin": self.origin.value,
            "source_version": self.source_version,
            "mode": self.mode,
            "buttons": {
                "autoselect": self.autoselect,
                "xuanfeng": self.xuanfeng,
                "Burst": self.burst_enabled,
                "Survive": self.survive_enabled,
                "interrupt": self.interrupt_enabled,
                "Sunder": self.sunder_enabled,
                "SunderMode": self.sunder_mode,
                "liunudaduan": self.reserve_ten_rage_for_interrupt,
                "quanbudaduan": self.interrupt_all,
                "zhidingdaduan": self.interrupt_named,
                "daduanjineng": self.interrupt_spell_name,
                "nuhou": self.battle_shout,
                "yazhi": self.overpower,
                "xuexing": self.bloodrage,
                "kuangbao": self.berserker_rage,
                "zhendang": self.concussion_blow,
                "leiting": self.thunder_clap,
                "cuozhi": self.demoralizing_shout,
            },
            "burst_stages": [stage.to_dict() for stage in self.burst_stages],
            "survival": self.survival.to_dict(),
            "evidence_sha256": list(self.evidence_sha256),
            "profile_artifact_sha256": self.profile_artifact_sha256,
        }

    @property
    def semantic_sha256(self) -> str:
        return _sha256_json(self.semantic_payload())

    def to_dict(self) -> JSONMap:
        payload = self.semantic_payload()
        payload.update(
            {
                "semantic_sha256": self.semantic_sha256,
                "runtime_profile_observed": False,
                "comparison_ready": False,
                "eligible_for_independent_vote": False,
            }
        )
        return payload


def _default_burst_stage() -> BurstStageV3:
    return BurstStageV3(
        manual_enabled=True,
        trigger_skill="强效怒气药水",
        auto_enabled=False,
        boss_health_threshold_pct=25.0,
        recklessness=True,
        death_wish=True,
        racial_triplet=True,
        slot_13=True,
        slot_14=True,
        mighty_rage_potion=True,
        juju_flurry=True,
        goblin_sapper_charge=True,
        haste_potion=True,
        rage_potion=True,
        elixir_of_rapid_growth=True,
    )


SOURCE_DEFAULT_PROFILE_V3 = Contra260817FuryProfileV3(
    profile_id="contra260817.fury.fresh_source_default.v3",
    origin=ProfileOriginV3.SOURCE_DEFAULT_DIAGNOSTIC,
    source_version=20251017022403,
    mode="副本模式",
    autoselect=True,
    xuanfeng=True,
    # The DB defines lowercase baofa/shengcun.  The executable reads uppercase
    # Burst/Survive and InitWarriorMiniUI fills those absent keys with false.
    burst_enabled=False,
    survive_enabled=False,
    interrupt_enabled=False,
    sunder_enabled=False,
    sunder_mode="startone",
    reserve_ten_rage_for_interrupt=False,
    interrupt_all=True,
    interrupt_named=True,
    interrupt_spell_name="暗影箭",
    battle_shout=True,
    overpower=True,
    bloodrage=True,
    berserker_rage=True,
    concussion_blow=True,
    thunder_clap=True,
    demoralizing_shout=True,
    burst_stages=(_default_burst_stage(), _default_burst_stage()),
    survival=SurvivalProfileV3(
        shield_wall=True,
        shield_wall_below_pct=50.0,
        last_stand=True,
        last_stand_below_pct=50.0,
        healthstone=True,
        healthstone_below_pct=50.0,
        healing_potion=True,
        healing_potion_below_pct=50.0,
        herbal_tea=True,
        herbal_tea_below_pct=50.0,
        boss_ot_weapon_swap=True,
        output_mainhand="无",
        output_offhand="无",
        defensive_mainhand="无",
        defensive_offhand="无",
    ),
    evidence_sha256=(SOURCE_DEFAULT_DB_SHA256, ONLOGIN_SHA256),
)


@dataclass(frozen=True)
class TargetSelectionStateV3:
    """Inputs and observed result of ``Contra.SelectNearestTarget``/attack."""

    current_target_exists: bool
    current_target_dead: bool
    current_target_friendly: bool
    current_target_is_player: bool
    current_target_banished: bool
    current_target_in_melee_range: bool
    current_target_in_front: bool
    switch_throttle_open: bool
    five_yard_guid_candidate: str | None
    five_yard_guid_is_current: bool | None
    previous_target_guid: str | None
    nearest_enemy_changed_target: bool | None
    nearest_enemy_target_within_five_yards: bool | None
    post_target_exists: bool
    start_attack_banish_branch_active: bool
    attack_actionbar_present: bool
    autoattack_current: bool

    def __post_init__(self) -> None:
        for name in (
            "current_target_exists",
            "current_target_dead",
            "current_target_friendly",
            "current_target_is_player",
            "current_target_banished",
            "current_target_in_melee_range",
            "current_target_in_front",
            "switch_throttle_open",
            "post_target_exists",
            "start_attack_banish_branch_active",
            "attack_actionbar_present",
            "autoattack_current",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"TargetSelectionStateV3.{name} must be boolean")
        for name in (
            "five_yard_guid_candidate",
            "previous_target_guid",
        ):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, f"TargetSelectionStateV3.{name}")
        for name in (
            "five_yard_guid_is_current",
            "nearest_enemy_changed_target",
            "nearest_enemy_target_within_five_yards",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"TargetSelectionStateV3.{name} must be bool or None")


@dataclass(frozen=True)
class Contra260817FuryFullPolicyStateV3:
    """Explicit source snapshots; absent reached inputs fail before any sink."""

    entry_combat: FuryExpertState
    post_target_combat: FuryExpertState
    talent: Contra260817FuryTalentV3
    authorization_gate_passed: bool | None
    attackable_units_within_five_yards: int | None
    current_target_within_five_yards: bool | None
    entry_target_classification: ContraTargetClassificationV2 | str | None
    entry_target_max_health: int | None
    entry_target_name: str | None
    post_target_classification: ContraTargetClassificationV2 | str | None
    post_target_max_health: int | None
    post_target_name: str | None
    target_selection: TargetSelectionStateV3 | None
    heroic_strike_probe_texture_present: bool | None
    cleave_probe_texture_present: bool | None
    target_affecting_combat: bool | None
    target_casting_spell: str | None
    target_sunder_stacks: int | None
    player_health_pct: float | None
    target_of_target_is_player: bool | None
    offhand_is_shield_after_prelude: bool | None
    equipped_item_names: tuple[str, ...] | None
    equipped_mainhand_name: str | None
    equipped_offhand_name: str | None
    player_buffs: frozenset[str] | None
    target_debuffs: frozenset[str] | None
    cooldowns_s: Mapping[str, float] | None
    spell_rage_costs: Mapping[str, float] | None
    authorization_evidence: ContraFieldEvidenceV2 | None
    route_evidence: ContraFieldEvidenceV2 | None
    entry_snapshot_evidence: ContraFieldEvidenceV2 | None
    post_target_snapshot_evidence: ContraFieldEvidenceV2 | None
    target_selection_evidence: ContraFieldEvidenceV2 | None
    actionbar_evidence: ContraFieldEvidenceV2 | None
    equipment_evidence: ContraFieldEvidenceV2 | None
    profile_runtime_evidence: ContraFieldEvidenceV2 | None = None


ADAPTER_CONTRACT: JSONMap = {
    "schema": SCHEMA,
    "adapter_contract_id": ADAPTER_CONTRACT_ID,
    "required_baseline_policy_id": REQUIRED_BASELINE_POLICY_ID,
    "diagnostic_expert_id": POLICY_ID,
    "source_code_manifest_sha256": CODE_MANIFEST_SHA256,
    "source_manifest_sha256": EXPECTED_MANIFEST_SHA256,
    "macro": "/contra c",
    "mode": "副本模式",
    "talents": [member.value for member in Contra260817FuryTalentV3],
    "entrypoints": {
        "two_hand": {"single": "Contra_SSKBZ_A", "multi": "Contra_SSKBZ_B"},
        "dual_wield": {"single": "Contra_SCKBZ_A", "multi": "Contra_SCKBZ_B"},
    },
    "sink_channels": [
        "target",
        "autoattack",
        "cast_control",
        "gcd",
        "off_gcd",
        "swing_queue",
        "item",
        "equipment",
        "stance",
    ],
    "source_refs": [
        "Contra_Macro.lua:221-292",
        "Contra_Lib.lua:10-105",
        "Contra_Scrip_Warrior.lua:134-193",
        "Contra_Scrip_Warrior.lua:203-325",
        "Contra_Scrip_Warrior.lua:328-583",
        "Contra_Scrip_Warrior.lua:586-823",
        "Contra_Scrip_Warrior.lua:1257-1315",
        "Contra_Onlogin.lua:288-340",
        "Contra_DB.lua:27492-27702",
    ],
    "semantic_limits": [
        "source_translation_not_lua_execution",
        "raid_mode_level60_fury_only",
        "runtime_ContraDB_not_in_package",
        "client_acceptance_and_server_results_not_inferred",
    ],
}
ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        ADAPTER_CONTRACT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


PROMOTION_BLOCKERS: tuple[tuple[str, str], ...] = (
    (
        "PACKAGE_INCOMPLETE",
        "Contra.toc names two absent files and the pinned tree has zero-byte code members",
    ),
    (
        "PER_CHARACTER_CONTRADB_PROFILE_MISSING",
        "the package contains defaults but no per-character runtime ContraDB artifact",
    ),
    (
        "RUNTIME_LOAD_ATTESTATION_MISSING",
        "no Turtle WoW client load receipt binds this package and profile",
    ),
    (
        "NAME_AND_GUILD_RUNTIME_ATTESTATION_MISSING",
        "the runtime authorization result was not captured for the evaluated character",
    ),
    (
        "CLIENT_ORDERED_SINK_TRACE_MISSING",
        "synthetic source traversal is not an observed client sink trace",
    ),
    (
        "CLIENT_ACCEPTANCE_TRACE_MISSING",
        "client acceptance/rejection of every same-invocation attempt is unknown",
    ),
    (
        "SERVER_OUTCOME_TRACE_MISSING",
        "server GO and result evidence is not bound to source attempts",
    ),
    (
        "ORDERED_SINK_EXECUTOR_V3_OPERATION_COVERAGE_MISSING",
        "the frozen ordered executor does not cover Contra260817 target/item/equipment operations",
    ),
    (
        "FULL_POLICY_ROLLOUT_V3_ADAPTER_REGISTRATION_MISSING",
        "the frozen full-policy rollout rejects Contra260817 and is intentionally unchanged",
    ),
)


class Contra260817FuryFullPolicyAdapterV3:
    """Bug-preserving source traversal for raid ``/contra c`` Fury."""

    expert_id = POLICY_ID

    def __init__(
        self,
        profile: Contra260817FuryProfileV3 = SOURCE_DEFAULT_PROFILE_V3,
        *,
        manifest_path: Path = DEFAULT_MANIFEST,
        source_root: Path = DEFAULT_SOURCE_ROOT,
        verify_live_source: bool = True,
    ) -> None:
        if not isinstance(profile, Contra260817FuryProfileV3):
            raise TypeError("profile must be Contra260817FuryProfileV3")
        if not isinstance(verify_live_source, bool):
            raise TypeError("verify_live_source must be boolean")
        self.profile = profile
        self.adapter_file_sha256 = _stable_file_sha256(Path(__file__))
        if verify_live_source:
            try:
                self.source_verification = verify_contra260817_source_package(
                    manifest_path=manifest_path,
                    source_root=source_root,
                )
                code_manifest = self.source_verification.get("code_manifest")
                self.source_identity_verified = (
                    self.source_verification.get("source_identity_verified") is True
                    and isinstance(code_manifest, Mapping)
                    and code_manifest.get("sha256") == CODE_MANIFEST_SHA256
                )
            except Exception as error:
                self.source_verification = {
                    "source_identity_verified": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
                self.source_identity_verified = False
        else:
            self.source_verification = {
                "source_identity_verified": False,
                "result": "NOT_REQUESTED",
            }
            self.source_identity_verified = False

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra.toc",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_DB.lua",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Onlogin.lua",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Macro.lua",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Lib.lua",
                r"%BRAIN_OF_CAT_ROOT%\Contra_new\Contra_Scrip_Warrior.lua",
            ),
            source_refs=tuple(ADAPTER_CONTRACT["source_refs"]),
        )

    def propose(self, state: Contra260817FuryFullPolicyStateV3) -> ExpertDecision:
        provenance = self._provenance()
        if not isinstance(state, Contra260817FuryFullPolicyStateV3):
            return invalid_decision(
                provenance,
                "Contra260817 full policy requires its explicit v3 state wrapper",
                metadata=self._invalid_metadata(["V3_STATE_WRAPPER_MISSING"]),
            )
        if not self.source_identity_verified:
            return invalid_decision(
                provenance,
                "Contra260817 source identity is not verified",
                metadata=self._invalid_metadata(
                    ["SOURCE_IDENTITY_NOT_VERIFIED_FOR_ADAPTER"]
                ),
            )
        auth_errors = self._validate_authorization(state)
        if auth_errors:
            return invalid_decision(
                provenance,
                "Contra260817 authorization state is unknown or unproven",
                metadata=self._invalid_metadata(auth_errors),
            )
        if state.authorization_gate_passed is False:
            return _DecisionBuilder().build(
                provenance,
                role_can_vote=False,
                reason="NameAndGuild gate returned before macro-c routing",
                metadata=self._metadata(
                    state,
                    route="AUTHORIZATION_RETURN",
                    traversal_returns=("Contra_Macro.lua:225",),
                    helper_no_sink_attempts=(),
                    target_helper_trace=(),
                    effective_nearby_count=None,
                ),
            )

        errors = self._validate_reached_state(state)
        if errors:
            return invalid_decision(
                provenance,
                "Contra260817 full-policy reached state is incomplete",
                metadata=self._invalid_metadata(errors),
            )

        assert state.attackable_units_within_five_yards is not None
        assert state.current_target_within_five_yards is not None
        count = state.attackable_units_within_five_yards
        fallback = (
            count == 0
            and state.entry_combat.target_exists
            and state.current_target_within_five_yards
        )
        effective_count = 1 if fallback else count
        route = "MULTI_B" if effective_count > 1 else "SINGLE_A"

        entry = self._entry_combat(state)
        post = self._post_target_combat(state)
        builder = _DecisionBuilder()
        helper_no_sink: list[JSONMap] = []
        returns: list[str] = []
        target_trace: list[JSONMap] = []

        self._emit_attack_prelude(
            builder,
            state,
            target_helper_trace=target_trace,
            helper_no_sink_attempts=helper_no_sink,
        )

        if self._emit_interrupt(
            builder,
            state,
            post,
            helper_no_sink_attempts=helper_no_sink,
        ):
            returns.append("ZS_DaDuan:true->main_rotation_return")
            return builder.build(
                provenance,
                role_can_vote=False,
                reason="Contra260817 interrupt helper ended this macro invocation",
                metadata=self._metadata(
                    state,
                    route=route,
                    traversal_returns=returns,
                    helper_no_sink_attempts=helper_no_sink,
                    target_helper_trace=target_trace,
                    effective_nearby_count=effective_count,
                    count_fallback_applied=fallback,
                ),
            )

        self._emit_burst(builder, state, post, helper_no_sink_attempts=helper_no_sink)
        self._emit_support(builder, state, post, helper_no_sink_attempts=helper_no_sink)
        self._emit_survival(builder, state, post, helper_no_sink_attempts=helper_no_sink)

        if state.offhand_is_shield_after_prelude is False:
            self._cast(
                builder,
                state,
                "狂暴姿态",
                "Contra_Scrip_Warrior.lua:592/757/1263/1296",
                helper_no_sink_attempts=helper_no_sink,
            )

        if state.talent is Contra260817FuryTalentV3.DUAL_WIELD:
            calls = 1 if route == "MULTI_B" else 2
            for ordinal in range(calls):
                if self._emit_sunder(
                    builder,
                    state,
                    post,
                    helper_no_sink_attempts=helper_no_sink,
                    ordinal=ordinal + 1,
                ):
                    returns.append(f"ZS_POJIA[{ordinal + 1}]:true->rotation_return")
                    return builder.build(
                        provenance,
                        role_can_vote=False,
                        reason="Contra260817 Sunder helper ended this macro invocation",
                        metadata=self._metadata(
                            state,
                            route=route,
                            traversal_returns=returns,
                            helper_no_sink_attempts=helper_no_sink,
                            target_helper_trace=target_trace,
                            effective_nearby_count=effective_count,
                            count_fallback_applied=fallback,
                        ),
                    )
            if route == "MULTI_B":
                self._dual_wield_b(builder, state, entry, helper_no_sink)
            else:
                self._dual_wield_a(builder, state, entry, helper_no_sink)
        else:
            if route == "MULTI_B":
                self._two_hand_b(builder, state, entry, helper_no_sink)
            else:
                self._two_hand_a(builder, state, entry, helper_no_sink)

        return builder.build(
            provenance,
            role_can_vote=False,
            reason=(
                "source-derived Contra260817 Fury full macro-c traversal; "
                "diagnostic only"
            ),
            metadata=self._metadata(
                state,
                route=route,
                traversal_returns=returns,
                helper_no_sink_attempts=helper_no_sink,
                target_helper_trace=target_trace,
                effective_nearby_count=effective_count,
                count_fallback_applied=fallback,
            ),
        )

    def _validate_authorization(
        self, state: Contra260817FuryFullPolicyStateV3
    ) -> list[str]:
        errors: list[str] = []
        if not isinstance(state.authorization_gate_passed, bool):
            errors.append("AUTHORIZATION_GATE_UNKNOWN")
        _validate_evidence(
            state.authorization_evidence,
            "AUTHORIZATION_EVIDENCE_MISSING",
            errors,
        )
        return errors

    def _validate_reached_state(
        self, state: Contra260817FuryFullPolicyStateV3
    ) -> list[str]:
        errors: list[str] = []
        if not isinstance(state.entry_combat, FuryExpertState):
            errors.append("ENTRY_COMBAT_STATE_INVALID")
        if not isinstance(state.post_target_combat, FuryExpertState):
            errors.append("POST_TARGET_COMBAT_STATE_INVALID")
        if errors:
            return errors
        if not isinstance(state.talent, Contra260817FuryTalentV3):
            errors.append("FURY_TALENT_UNKNOWN")
        expected_weapon = (
            WeaponMode.DUAL_WIELD
            if state.talent is Contra260817FuryTalentV3.DUAL_WIELD
            else WeaponMode.TWO_HAND
        )
        if state.entry_combat.weapon_mode is not expected_weapon:
            errors.append("TALENT_WEAPON_MODE_MISMATCH")
        if not state.entry_combat.target_exists:
            errors.append("ENTRY_TARGET_REQUIRED_BEFORE_GETCOMBATINFO")
        if not isinstance(state.attackable_units_within_five_yards, int) or isinstance(
            state.attackable_units_within_five_yards, bool
        ) or (
            isinstance(state.attackable_units_within_five_yards, int)
            and state.attackable_units_within_five_yards < 0
        ):
            errors.append("FIVE_YARD_ATTACKABLE_COUNT_UNKNOWN")
        if not isinstance(state.current_target_within_five_yards, bool):
            errors.append("CURRENT_TARGET_FIVE_YARD_STATE_UNKNOWN")
        if not isinstance(state.target_selection, TargetSelectionStateV3):
            errors.append("TARGET_SELECTION_STATE_MISSING")
        for label, value in (
            ("HEROIC_STRIKE_PROBE_TEXTURE_UNKNOWN", state.heroic_strike_probe_texture_present),
            ("CLEAVE_PROBE_TEXTURE_UNKNOWN", state.cleave_probe_texture_present),
            ("TARGET_AFFECTING_COMBAT_UNKNOWN", state.target_affecting_combat),
            ("TARGET_OF_TARGET_UNKNOWN", state.target_of_target_is_player),
            ("POST_PRELUDE_OFFHAND_SHIELD_UNKNOWN", state.offhand_is_shield_after_prelude),
        ):
            if not isinstance(value, bool):
                errors.append(label)
        if state.target_casting_spell is not None and not isinstance(
            state.target_casting_spell, str
        ):
            errors.append("TARGET_CASTING_SPELL_INVALID")
        elif state.target_casting_spell == "":
            errors.append("TARGET_CASTING_SPELL_INVALID")
        if not isinstance(state.target_sunder_stacks, int) or isinstance(
            state.target_sunder_stacks, bool
        ) or (
            isinstance(state.target_sunder_stacks, int)
            and not 0 <= state.target_sunder_stacks <= 5
        ):
            errors.append("TARGET_SUNDER_STACKS_UNKNOWN")
        if not _finite_range(state.player_health_pct, 0.0, 100.0):
            errors.append("PLAYER_HEALTH_PERCENT_UNKNOWN")
        for label, value in (
            ("equipped_mainhand_name", state.equipped_mainhand_name),
            ("equipped_offhand_name", state.equipped_offhand_name),
        ):
            if value is not None and not isinstance(value, str):
                errors.append(f"{label.upper()}_INVALID")
        if not isinstance(state.equipped_item_names, tuple) or any(
            not isinstance(value, str) or not value
            for value in (state.equipped_item_names or ())
        ):
            errors.append("COMPLETE_EQUIPPED_ITEM_NAMES_UNKNOWN")
        if not isinstance(state.player_buffs, frozenset) or any(
            not isinstance(value, str) for value in (state.player_buffs or ())
        ):
            errors.append("PLAYER_BUFF_SET_UNKNOWN")
        if not isinstance(state.target_debuffs, frozenset) or any(
            not isinstance(value, str) for value in (state.target_debuffs or ())
        ):
            errors.append("TARGET_DEBUFF_SET_UNKNOWN")
        if not _numeric_mapping(state.cooldowns_s):
            errors.append("COOLDOWN_MAP_UNKNOWN")
        else:
            required_cooldowns = {"血性狂暴", "震荡猛击"}
            missing = sorted(required_cooldowns - set(state.cooldowns_s or {}))
            if missing:
                errors.extend(f"COOLDOWN_MISSING:{name}" for name in missing)
        if not _numeric_mapping(state.spell_rage_costs):
            errors.append("SPELL_RAGE_COST_MAP_UNKNOWN")
        if self.profile.reserve_ten_rage_for_interrupt and self.profile.interrupt_enabled:
            required_costs = set(_LOCALIZED_ACTIONS) | {
                "英勇打击",
                "顺劈斩",
                "战斗姿态",
                "防御姿态",
                "狂暴姿态",
            }
            missing = sorted(required_costs - set(state.spell_rage_costs or {}))
            if missing:
                errors.extend(f"SPELL_RAGE_COST_MISSING:{name}" for name in missing)
        for label, value in (
            ("entry_target", state.entry_target_classification),
            ("post_target", state.post_target_classification),
        ):
            try:
                ContraTargetClassificationV2(value)
            except (TypeError, ValueError):
                errors.append(f"{label.upper()}_CLASSIFICATION_UNKNOWN")
        for label, value in (
            ("ENTRY_TARGET_MAX_HEALTH", state.entry_target_max_health),
            ("POST_TARGET_MAX_HEALTH", state.post_target_max_health),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                errors.append(f"{label}_UNKNOWN")
        for label, value in (
            ("ENTRY_TARGET_NAME", state.entry_target_name),
            ("POST_TARGET_NAME", state.post_target_name),
        ):
            if not isinstance(value, str) or not value:
                errors.append(f"{label}_UNKNOWN")
        for evidence, code in (
            (state.route_evidence, "ROUTE_EVIDENCE_MISSING"),
            (state.entry_snapshot_evidence, "ENTRY_SNAPSHOT_EVIDENCE_MISSING"),
            (state.post_target_snapshot_evidence, "POST_TARGET_SNAPSHOT_EVIDENCE_MISSING"),
            (state.target_selection_evidence, "TARGET_SELECTION_EVIDENCE_MISSING"),
            (state.actionbar_evidence, "ACTIONBAR_EVIDENCE_MISSING"),
            (state.equipment_evidence, "EQUIPMENT_EVIDENCE_MISSING"),
        ):
            _validate_evidence(evidence, code, errors)
        for label, combat in (
            ("ENTRY", state.entry_combat),
            ("POST_TARGET", state.post_target_combat),
        ):
            for field_name, value, minimum, maximum, strict in (
                ("rage", combat.rage, 0.0, 100.0, False),
                ("target_health_pct", combat.target_health_pct, 0.0, 100.0, False),
                ("bloodthirst_ready_in_s", combat.bloodthirst_ready_in_s, 0.0, None, False),
                ("whirlwind_ready_in_s", combat.whirlwind_ready_in_s, 0.0, None, False),
                ("contra_st_s", combat.contra_st_s, 0.0, None, False),
                ("contra_ss_s", combat.contra_ss_s, 0.0, None, False),
                ("contra_sd_s", combat.contra_sd_s, 0.0, None, True),
                ("target_distance_yards", combat.target_distance_yards, 0.0, None, False),
                ("slam_cast_time_s", combat.slam_cast_time_s, 0.0, None, True),
                ("slam_remaining_s", combat.slam_remaining_s, 0.0, None, False),
            ):
                if not _finite_range(value, minimum, maximum, strict):
                    errors.append(f"{label}_{field_name.upper()}_UNKNOWN")
        if isinstance(state.target_selection, TargetSelectionStateV3):
            errors.extend(self._validate_target_path(state.target_selection))
            if (
                state.post_target_combat.target_exists
                is not state.target_selection.post_target_exists
            ):
                errors.append("POST_TARGET_EXISTENCE_MISMATCH")
        return list(dict.fromkeys(errors))

    def _validate_target_path(self, target: TargetSelectionStateV3) -> list[str]:
        errors: list[str] = []
        invalid = (
            not target.current_target_exists
            or target.current_target_dead
            or target.current_target_friendly
            or target.current_target_is_player
            or target.current_target_banished
        )
        needs_range_switch = (
            not invalid
            and (
                not target.current_target_in_melee_range
                or not target.current_target_in_front
            )
            and target.switch_throttle_open
        )
        if self.profile.autoselect and (invalid or needs_range_switch):
            if target.five_yard_guid_candidate is not None:
                if target.five_yard_guid_is_current is None:
                    errors.append("FIVE_YARD_GUID_CURRENTNESS_UNKNOWN")
            else:
                if target.nearest_enemy_changed_target is None:
                    errors.append("NEAREST_ENEMY_CHANGE_RESULT_UNKNOWN")
                if needs_range_switch and target.nearest_enemy_changed_target:
                    if target.nearest_enemy_target_within_five_yards is None:
                        errors.append("NEAREST_ENEMY_FIVE_YARD_RESULT_UNKNOWN")
                    if target.previous_target_guid is None:
                        errors.append("PREVIOUS_TARGET_GUID_MISSING_FOR_RESTORE")
        return errors

    def _entry_combat(self, state: Contra260817FuryFullPolicyStateV3) -> FuryExpertState:
        classification = ContraTargetClassificationV2(state.entry_target_classification)
        assert state.equipped_item_names is not None
        return replace(
            state.entry_combat,
            target_is_boss=classification is ContraTargetClassificationV2.WORLDBOSS,
            target_name=str(state.entry_target_name),
            contra_zssdw=derive_zssdw_from_loadout(state.equipped_item_names),
        )

    def _post_target_combat(self, state: Contra260817FuryFullPolicyStateV3) -> FuryExpertState:
        classification = ContraTargetClassificationV2(state.post_target_classification)
        return replace(
            state.post_target_combat,
            target_is_boss=classification is ContraTargetClassificationV2.WORLDBOSS,
            target_name=str(state.post_target_name),
        )

    def _emit_attack_prelude(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        *,
        target_helper_trace: list[JSONMap],
        helper_no_sink_attempts: list[JSONMap],
    ) -> None:
        assert state.target_selection is not None
        target = state.target_selection
        if self.profile.autoselect:
            invalid = (
                not target.current_target_exists
                or target.current_target_dead
                or target.current_target_friendly
                or target.current_target_is_player
                or target.current_target_banished
            )
            range_switch = (
                not invalid
                and (
                    not target.current_target_in_melee_range
                    or not target.current_target_in_front
                )
            )
            if invalid:
                if target.five_yard_guid_candidate is not None:
                    self._emit_target(
                        builder,
                        "TargetUnit",
                        target.five_yard_guid_candidate,
                        "Contra_Lib.lua:67-72",
                    )
                    target_helper_trace.append({"branch": "INVALID_GUID_5YD", "changed": True})
                else:
                    self._emit_target(
                        builder,
                        "TargetNearestEnemy",
                        "NEAREST_ENEMY",
                        "Contra_Lib.lua:67-77",
                        nearest=True,
                    )
                    target_helper_trace.append(
                        {
                            "branch": "INVALID_NEAREST_FALLBACK",
                            "changed": target.nearest_enemy_changed_target,
                            "restore_attempted": False,
                        }
                    )
            elif range_switch and target.switch_throttle_open:
                if target.five_yard_guid_candidate is not None:
                    changed = target.five_yard_guid_is_current is False
                    if changed:
                        self._emit_target(
                            builder,
                            "TargetUnit",
                            target.five_yard_guid_candidate,
                            "Contra_Lib.lua:79-88",
                        )
                    else:
                        helper_no_sink_attempts.append(
                            {
                                "operation": "Contra.SelectNearestTarget",
                                "reason": "five_yard_guid_is_current_target",
                                "source_ref": "Contra_Lib.lua:83-88",
                            }
                        )
                    target_helper_trace.append(
                        {"branch": "RANGE_OR_FACING_GUID_5YD", "changed": changed}
                    )
                else:
                    self._emit_target(
                        builder,
                        "TargetNearestEnemy",
                        "NEAREST_ENEMY",
                        "Contra_Lib.lua:89-99",
                        nearest=True,
                    )
                    restore = bool(target.nearest_enemy_changed_target) and (
                        target.nearest_enemy_target_within_five_yards is False
                    )
                    if restore:
                        assert target.previous_target_guid is not None
                        self._emit_target(
                            builder,
                            "TargetUnit",
                            target.previous_target_guid,
                            "Contra_Lib.lua:95-100",
                        )
                    target_helper_trace.append(
                        {
                            "branch": "RANGE_OR_FACING_NEAREST_FALLBACK",
                            "changed": target.nearest_enemy_changed_target,
                            "restore_attempted": restore,
                        }
                    )
            else:
                helper_no_sink_attempts.append(
                    {
                        "operation": "Contra.SelectNearestTarget",
                        "reason": (
                            "range_switch_throttled"
                            if range_switch
                            else "current_target_valid_melee_and_front"
                        ),
                        "source_ref": "Contra_Lib.lua:51-105",
                    }
                )
                target_helper_trace.append(
                    {
                        "branch": (
                            "RANGE_OR_FACING_THROTTLED"
                            if range_switch
                            else "KEEP_VALID_TARGET"
                        ),
                        "changed": False,
                    }
                )

        # Contra.StartAttack can be a no-op or can even toggle attack off in
        # the banished branch; recording a universal START would be false.
        if not target.post_target_exists:
            helper_no_sink_attempts.append(
                {
                    "operation": "Contra.StartAttack",
                    "reason": "post_selection_target_absent",
                    "source_ref": "Contra_Lib.lua:10-14",
                }
            )
            return
        if target.attack_actionbar_present:
            if not target.start_attack_banish_branch_active and target.autoattack_current:
                helper_no_sink_attempts.append(
                    {
                        "operation": "Contra.StartAttack",
                        "reason": "attack_action_already_current",
                        "source_ref": "Contra_Lib.lua:24-27",
                    }
                )
                return
            if target.start_attack_banish_branch_active and not target.autoattack_current:
                helper_no_sink_attempts.append(
                    {
                        "operation": "Contra.StartAttack",
                        "reason": "banished_and_attack_action_not_current",
                        "source_ref": "Contra_Lib.lua:31-43",
                    }
                )
                return
            value = "STOP" if target.start_attack_banish_branch_active else "START"
            builder.raw.append(
                RawSink(
                    "autoattack",
                    "UseAction",
                    value,
                    "Contra_Lib.lua:24-27/40-43",
                )
            )
            return
        builder.raw.append(
            RawSink(
                "autoattack",
                "AttackTarget",
                "TOGGLE",
                "Contra_Lib.lua:28-30/44-46",
            )
        )

    @staticmethod
    def _emit_target(
        builder: _DecisionBuilder,
        operation: str,
        value: str,
        source_ref: str,
        *,
        nearest: bool = False,
    ) -> None:
        builder.target = TargetOp.NEAREST_ENEMY if nearest else TargetOp.AUTO_SWITCH
        builder.raw.append(RawSink("target", operation, value, source_ref))

    def _emit_interrupt(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[JSONMap],
    ) -> bool:
        profile = self.profile
        if not profile.interrupt_enabled or state.target_affecting_combat is False:
            return False
        spell = state.target_casting_spell
        should_interrupt = bool(profile.interrupt_all and spell)
        if profile.interrupt_named and spell == profile.interrupt_spell_name:
            should_interrupt = True
        if not should_interrupt:
            return False
        # ZS_DaDuan calls GetSpellCastTime("猛击"), which reads the spellbook
        # tooltip, not GetMySlamTime.  With Slam known this stop call happens
        # even when the player is not currently casting.
        if combat.slam_cast_time_s > 0.0:
            builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:293-321")
        self._cast(
            builder,
            state,
            "盾击" if state.offhand_is_shield_after_prelude else "拳击",
            "Contra_Scrip_Warrior.lua:293-321",
            helper_no_sink_attempts=helper_no_sink_attempts,
        )
        # Source returns true based on target-cast detection, not cast success.
        return True

    def _emit_burst(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[JSONMap],
    ) -> None:
        if not self.profile.burst_enabled:
            return
        assert state.player_buffs is not None
        for ordinal, stage in enumerate(self.profile.burst_stages, start=1):
            if not stage.manual_enabled and not stage.auto_enabled:
                continue
            if stage.manual_enabled:
                if stage.trigger_skill == "种族天赋":
                    if not any(
                        buff in state.player_buffs
                        for buff in ("感知", "血性狂怒", "狂暴")
                    ):
                        continue
                else:
                    required = _BURST_TRIGGER_BUFF.get(stage.trigger_skill)
                    if required is not None and required not in state.player_buffs:
                        continue
            if stage.auto_enabled and (
                not combat.target_is_boss
                or combat.target_health_pct > stage.boss_health_threshold_pct
            ):
                continue
            ref = f"Contra_Scrip_Warrior.lua:{328 if ordinal == 1 else 367}-{365 if ordinal == 1 else 402}"
            for enabled, spell in (
                (stage.recklessness, "鲁莽"),
                (stage.death_wish, "死亡之愿"),
            ):
                if enabled and (ordinal == 1 or combat.in_melee_range):
                    self._cast(builder, state, spell, ref, helper_no_sink_attempts=helper_no_sink_attempts)
            if stage.racial_triplet:
                for spell in ("感知", "血性狂怒", "狂暴"):
                    self._cast(builder, state, spell, ref, helper_no_sink_attempts=helper_no_sink_attempts)
            if stage.slot_13 and (ordinal == 1 or combat.in_melee_range):
                self._emit_inventory_item(builder, 13, ref)
            if stage.slot_14 and (ordinal == 1 or combat.in_melee_range):
                self._emit_inventory_item(builder, 14, ref)
            for enabled, item, needs_melee in (
                (stage.mighty_rage_potion, "强效怒气药水", False),
                (stage.juju_flurry, "魂能之速", ordinal == 2),
                (stage.goblin_sapper_charge, "地精工兵炸弹", ordinal == 2),
                (stage.haste_potion, "加速药水", ordinal == 2),
                (stage.rage_potion, "暴怒药水", False),
                (stage.elixir_of_rapid_growth, "急速生长药剂", ordinal == 2),
            ):
                if enabled and (not needs_melee or combat.in_melee_range):
                    self._emit_named_item(builder, item, ref)

    def _emit_support(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[JSONMap],
    ) -> None:
        profile = self.profile
        assert state.player_buffs is not None
        assert state.target_debuffs is not None
        assert state.cooldowns_s is not None
        if profile.battle_shout and "战斗怒吼" not in state.player_buffs:
            self._cast(builder, state, "战斗怒吼", "Contra_Scrip_Warrior.lua:414-416", helper_no_sink_attempts=helper_no_sink_attempts)
        if profile.overpower and combat.target_health_pct > 21.0 and 4.0 < combat.rage < 29.0 and combat.in_melee_range:
            self._cast(builder, state, "压制", "Contra_Scrip_Warrior.lua:418-420", helper_no_sink_attempts=helper_no_sink_attempts)
        if profile.bloodrage and state.cooldowns_s["血性狂暴"] == 0.0 and "狂怒" not in state.player_buffs and combat.in_combat and combat.in_melee_range:
            self._cast(builder, state, "血性狂暴", "Contra_Scrip_Warrior.lua:422-424", helper_no_sink_attempts=helper_no_sink_attempts)
        # IsTargetOfTargetMeor is an undefined global; only the aura disjuncts
        # can make the source condition truthy for Fury.
        if profile.berserker_rage and combat.in_combat and any(
            aura in state.player_buffs for aura in ("恐惧", "恐惧嚎叫", "破胆怒吼")
        ):
            self._cast(builder, state, "狂暴之怒", "Contra_Scrip_Warrior.lua:426-438", helper_no_sink_attempts=helper_no_sink_attempts)
        if profile.concussion_blow and state.cooldowns_s["震荡猛击"] == 0.0:
            # Level-60 Bloodthirst Fury cannot also know the 21-point Protection
            # talent. Contra.CD returns zero for an unknown spell, so this is a
            # real source attempt and a statically known client no-op.
            if self._reserve_guard_blocks(state, "震荡猛击"):
                helper_no_sink_attempts.append(
                    {
                        "operation": "ContraZSCast",
                        "value": "震荡猛击",
                        "reason": "liunudaduan_reserve_guard_return",
                        "source_ref": "Contra_Scrip_Warrior.lua:446-448",
                    }
                )
            else:
                builder.emit_known_noop_gcd_attempt(
                    CONCUSSION_BLOW,
                    operation="CastSpellByName",
                    value="震荡猛击",
                    source_ref="Contra_Scrip_Warrior.lua:446-448",
                    reason="level60_bloodthirst_fury_cannot_know_concussion_blow",
                )
        # The level<30 term makes both Thunder Clap conjunctions false for the
        # fixed level-60 Fury scope, notwithstanding the source's OR typo.
        if profile.demoralizing_shout and "挫志怒吼" not in state.target_debuffs and combat.in_melee_range:
            self._cast(builder, state, "挫志怒吼", "Contra_Scrip_Warrior.lua:462-464", helper_no_sink_attempts=helper_no_sink_attempts)

    def _emit_survival(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[JSONMap],
    ) -> None:
        if not self.profile.survive_enabled:
            return
        survival = self.profile.survival
        assert state.player_health_pct is not None
        hp = state.player_health_pct
        for enabled, threshold, spell, ref in (
            (survival.shield_wall, survival.shield_wall_below_pct, "盾墙", "Contra_Scrip_Warrior.lua:476-478"),
            (survival.last_stand, survival.last_stand_below_pct, "破釜沉舟", "Contra_Scrip_Warrior.lua:480-482"),
        ):
            if enabled and hp < threshold:
                self._cast(builder, state, spell, ref, helper_no_sink_attempts=helper_no_sink_attempts)
        for enabled, threshold, item, ref in (
            (survival.healthstone, survival.healthstone_below_pct, "特效治疗石", "Contra_Scrip_Warrior.lua:484-486"),
            (survival.healing_potion, survival.healing_potion_below_pct, "特效治疗药水", "Contra_Scrip_Warrior.lua:488-490"),
            (survival.herbal_tea, survival.herbal_tea_below_pct, "诺达纳尔草药茶", "Contra_Scrip_Warrior.lua:492-494"),
        ):
            if enabled and hp < threshold:
                self._emit_named_item(builder, item, ref)
        if not survival.boss_ot_weapon_swap or not combat.target_is_boss:
            return
        if state.target_of_target_is_player:
            self._emit_direct_spell(
                builder,
                "防御姿态",
                "Contra_Scrip_Warrior.lua:497-512",
            )
            self._emit_weapon_if_needed(builder, state.equipped_mainhand_name, survival.defensive_mainhand, 16, "Contra_Scrip_Warrior.lua:501-504")
            self._emit_weapon_if_needed(builder, state.equipped_offhand_name, survival.defensive_offhand, 17, "Contra_Scrip_Warrior.lua:506-509")
            self._emit_direct_spell(
                builder,
                "盾牌格挡",
                "Contra_Scrip_Warrior.lua:512",
            )
        else:
            self._emit_weapon_if_needed(builder, state.equipped_mainhand_name, survival.output_mainhand, 16, "Contra_Scrip_Warrior.lua:531-536")
            self._emit_weapon_if_needed(builder, state.equipped_offhand_name, survival.output_offhand, 17, "Contra_Scrip_Warrior.lua:538-541")

    def _emit_sunder(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        *,
        helper_no_sink_attempts: list[JSONMap],
        ordinal: int,
    ) -> bool:
        if not self.profile.sunder_enabled:
            return False
        assert state.target_sunder_stacks is not None
        required = (
            state.target_sunder_stacks == 0
            if self.profile.sunder_mode == "startone"
            else state.target_sunder_stacks < 5
        )
        if required and combat.rage >= 10.0 and combat.in_melee_range:
            self._cast(
                builder,
                state,
                "破甲攻击",
                f"Contra_Scrip_Warrior.lua:269-277;call={ordinal}",
                helper_no_sink_attempts=helper_no_sink_attempts,
            )
            # The helper returns true after calling ContraZSCast even if the
            # reserve guard caused ContraZSCast to return without a sink.
            return True
        return False

    def _dual_wield_a(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        helper_no_sink: list[JSONMap],
    ) -> None:
        hp, rage = combat.target_health_pct, combat.rage
        if (hp <= 20.0 and rage > 69.0 and combat.target_is_boss) or (hp <= 20.0 and not combat.target_is_boss):
            self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:1269-1271", helper_no_sink_attempts=helper_no_sink)
        if (hp > 20.0 and rage > 59.0) or (hp > 21.0 and combat.bloodthirst_ready_in_s > combat.contra_sd_s and rage > 47.0):
            self._cast(builder, state, "英勇打击", "Contra_Scrip_Warrior.lua:1273-1275", helper_no_sink_attempts=helper_no_sink)
        if (hp > 21.0 and combat.contra_ss_s < 0.9) or (1.0 < hp < 19.0 and 29.0 < rage < 45.0 and combat.target_is_boss and combat.contra_ss_s < 0.5):
            self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:1277-1279", helper_no_sink_attempts=helper_no_sink)
        if self.profile.xuanfeng and hp > 21.0 and combat.bloodthirst_ready_in_s > 1.5 and combat.contra_ss_s < 0.9 and rage > 34.0 and _whirlwind_range(combat):
            self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:1281-1283", helper_no_sink_attempts=helper_no_sink)
        if hp > 21.0 and combat.bloodthirst_ready_in_s > 1.4 and combat.whirlwind_ready_in_s > 1.4 and rage > 69.0 and combat.contra_ss_s < 0.9 and not combat.has_shield_break_trinket_buff:
            self._cast(builder, state, "断筋", "Contra_Scrip_Warrior.lua:1285-1287", helper_no_sink_attempts=helper_no_sink)

    def _dual_wield_b(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        helper_no_sink: list[JSONMap],
    ) -> None:
        hp, rage = combat.target_health_pct, combat.rage
        if hp < 20.0 and combat.whirlwind_ready_in_s > combat.contra_st_s and ((rage > 44.0 and combat.contra_st_s > 0.5) or (combat.bloodthirst_ready_in_s != 0.0 and rage > 15.0 and combat.contra_st_s > 0.5)):
            self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:1300-1302", helper_no_sink_attempts=helper_no_sink)
        # Preserve Lua precedence: Tauren arm is not gated by xuanfeng.
        if (self.profile.xuanfeng and not combat.race_is_tauren and combat.target_distance_yards < 8.0) or (combat.race_is_tauren and combat.target_distance_yards < 5.5):
            self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:1304-1306", helper_no_sink_attempts=helper_no_sink)
        if rage > 36.0 or (combat.whirlwind_ready_in_s > combat.contra_st_s and rage > 27.0):
            self._cast(builder, state, "顺劈斩", "Contra_Scrip_Warrior.lua:1308-1310", helper_no_sink_attempts=helper_no_sink)
        if (hp > 20.0 and rage > 68.0 and combat.whirlwind_ready_in_s > 1.4) or (hp > 20.0 and rage > 88.0 and combat.whirlwind_ready_in_s <= 1.4) or (hp < 20.0 and rage < 44.0 and combat.whirlwind_ready_in_s > 1.4):
            self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:1312-1314", helper_no_sink_attempts=helper_no_sink)

    def _two_hand_a(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        helper_no_sink: list[JSONMap],
    ) -> None:
        assert state.entry_target_max_health is not None
        assert state.player_buffs is not None
        hp, rage = combat.target_health_pct, combat.rage
        maxhp = state.entry_target_max_health
        dummy = combat.target_name == "学徒训练假人"
        if combat.target_is_boss or dummy:
            if "乱舞" not in state.player_buffs and hp > 70.0:
                if _unparenthesized_xuanfeng_range(self.profile.xuanfeng, combat):
                    self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:596-599", helper_no_sink_attempts=helper_no_sink)
                if not self.profile.xuanfeng and combat.bloodthirst_ready_in_s == 0.0:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:601-603", helper_no_sink_attempts=helper_no_sink)
            # The three ~= terms are joined by OR and are therefore always true.
            if hp > 20.0 and _whirlwind_range(combat) and self.profile.xuanfeng:
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:606-608", helper_no_sink_attempts=helper_no_sink)
            if hp > 20.0 and not self.profile.xuanfeng:
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:610-612", helper_no_sink_attempts=helper_no_sink)
            if combat.contra_zssdw >= 3 and ((hp > 20.0 and rage > 82.0 and combat.contra_st_s > 1.0) or (hp > 20.0 and rage > 67.0 and combat.contra_st_s < 1.0)):
                self._cast(builder, state, "英勇打击", "Contra_Scrip_Warrior.lua:614-616", helper_no_sink_attempts=helper_no_sink)
            if combat.contra_zssdw < 3 and ((hp > 20.0 and rage > 87.0 and combat.contra_st_s > 1.0) or (hp > 20.0 and rage > 72.0 and combat.contra_st_s < 1.0)):
                self._cast(builder, state, "英勇打击", "Contra_Scrip_Warrior.lua:618-620", helper_no_sink_attempts=helper_no_sink)
            if hp > 20.0:
                if self.profile.xuanfeng and combat.slam_remaining_s == 0.0 and (combat.last_cast_name in {"嗜血", "旋风斩"} or (combat.bloodthirst_ready_in_s != 0.0 and combat.whirlwind_ready_in_s != 0.0 and combat.contra_st_s > 1.2)):
                    self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:624-626", helper_no_sink_attempts=helper_no_sink)
                if not self.profile.xuanfeng and (combat.last_cast_name == "嗜血" or (combat.bloodthirst_ready_in_s != 0.0 and combat.contra_st_s > 1.2)):
                    self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:628-630", helper_no_sink_attempts=helper_no_sink)
                if combat.last_cast_name == "猛击" and combat.bloodthirst_ready_in_s < 0.5:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:632-634", helper_no_sink_attempts=helper_no_sink)
                if self.profile.xuanfeng and combat.last_cast_name == "猛击" and combat.whirlwind_ready_in_s < 0.5 and _whirlwind_range(combat) and rage > 34.0:
                    self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:636-638", helper_no_sink_attempts=helper_no_sink)
            if hp <= 20.0 and hp > 2.0:
                if combat.contra_ss_s < 1.2 and combat.bloodthirst_ready_in_s == 0.0 and rage > 44.0:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:641-644", helper_no_sink_attempts=helper_no_sink)
                if combat.contra_ss_s > 1.0 or (rage > 94.0 and combat.bloodthirst_ready_in_s != 0.0):
                    self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:641-644", helper_no_sink_attempts=helper_no_sink)
                if (combat.contra_st_s > combat.slam_cast_time_s + 0.3 and combat.bloodthirst_ready_in_s != 0.0) or (rage <= 44.0 and combat.contra_st_s > combat.slam_cast_time_s + 0.3 and combat.bloodthirst_ready_in_s == 0.0):
                    self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:641-644", helper_no_sink_attempts=helper_no_sink)
            if hp <= 2.0 and combat.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:647-649")
            if hp <= 2.0:
                self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:651-653", helper_no_sink_attempts=helper_no_sink)

        if maxhp >= 51000 and not combat.target_is_boss and not dummy:
            if hp > 20.0:
                self._two_hand_queue_threshold(builder, state, combat, 82.0, 67.0, 87.0, 72.0, "Contra_Scrip_Warrior.lua:656-666", helper_no_sink)
                if self.profile.xuanfeng and combat.whirlwind_ready_in_s < 6.5:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:668", helper_no_sink_attempts=helper_no_sink)
                if not self.profile.xuanfeng:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:669", helper_no_sink_attempts=helper_no_sink)
                if self.profile.xuanfeng and combat.bloodthirst_ready_in_s < 4.5 and _whirlwind_range(combat) and rage > 34.0:
                    self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:670", helper_no_sink_attempts=helper_no_sink)
                if self.profile.xuanfeng and combat.slam_remaining_s == 0.0 and (combat.last_cast_name in {"嗜血", "旋风斩"} or (combat.bloodthirst_ready_in_s != 0.0 and combat.whirlwind_ready_in_s != 0.0 and combat.contra_st_s > 1.2)):
                    self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:671-673", helper_no_sink_attempts=helper_no_sink)
                if not self.profile.xuanfeng and (combat.last_cast_name == "嗜血" or (combat.bloodthirst_ready_in_s != 0.0 and combat.contra_st_s > 1.2)):
                    self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:675-677", helper_no_sink_attempts=helper_no_sink)
            if hp <= 20.0:
                if combat.slam_remaining_s > 0.2:
                    builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:680-681")
                if rage < 35.0:
                    self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:682", helper_no_sink_attempts=helper_no_sink)
                if rage >= 35.0 or combat.bloodthirst_ready_in_s != 0.0:
                    self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:683", helper_no_sink_attempts=helper_no_sink)

        if 25000 <= maxhp < 51000:
            if hp < 20.0 and combat.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:687-691")
            self._two_hand_queue_threshold(builder, state, combat, 82.0, 67.0, 87.0, 72.0, "Contra_Scrip_Warrior.lua:693-699", helper_no_sink, require_hp=True)
            if (hp > 21.0 and combat.contra_st_s > 0.5 and combat.bloodthirst_ready_in_s == 0.0) or (hp < 19.0 and combat.contra_st_s > 1.5 and combat.bloodthirst_ready_in_s == 0.0 and combat.target_is_boss and (rage > 44.0 or rage < 35.0)):
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:702-704", helper_no_sink_attempts=helper_no_sink)
            if hp < 20.0:
                self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:706-708", helper_no_sink_attempts=helper_no_sink)
            if self.profile.xuanfeng and hp > 21.0 and combat.bloodthirst_ready_in_s > 1.0 and combat.whirlwind_ready_in_s == 0.0 and _whirlwind_range(combat) and rage > 39.0 and (combat.contra_st_s < 1.8 or combat.contra_ss_s < 0.2):
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:710-712", helper_no_sink_attempts=helper_no_sink)
            if hp > 21.0 and combat.contra_st_s > combat.slam_cast_time_s and combat.bloodthirst_ready_in_s > 0.1:
                self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:714-716", helper_no_sink_attempts=helper_no_sink)

        if maxhp < 25000:
            if hp < 20.0 and combat.slam_remaining_s > 0.2:
                builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:719-723")
            self._two_hand_queue_threshold(builder, state, combat, 52.0, 34.0, 57.0, 39.0, "Contra_Scrip_Warrior.lua:725-731", helper_no_sink, require_hp=True)
            if hp > 21.0 and combat.bloodthirst_ready_in_s == 0.0:
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:734-736", helper_no_sink_attempts=helper_no_sink)
            if hp < 20.0:
                self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:738-740", helper_no_sink_attempts=helper_no_sink)
            if self.profile.xuanfeng and hp > 21.0 and combat.bloodthirst_ready_in_s > 1.0 and combat.whirlwind_ready_in_s == 0.0 and _whirlwind_range(combat) and rage > 39.0 and (combat.contra_st_s < 1.8 or combat.contra_ss_s < 0.2):
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:742-744", helper_no_sink_attempts=helper_no_sink)
            if hp > 21.0 and combat.contra_st_s > combat.slam_cast_time_s and combat.bloodthirst_ready_in_s > 1.5 and combat.whirlwind_ready_in_s > 1.5:
                self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:746-748", helper_no_sink_attempts=helper_no_sink)

    def _two_hand_queue_threshold(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        high_zssdw_long: float,
        high_zssdw_short: float,
        low_zssdw_long: float,
        low_zssdw_short: float,
        source_ref: str,
        helper_no_sink: list[JSONMap],
        *,
        require_hp: bool = False,
    ) -> None:
        hp_ok = combat.target_health_pct > 20.0 if require_hp else True
        if combat.contra_st_s == 1.0:
            return
        threshold = (
            high_zssdw_long
            if combat.contra_zssdw >= 3 and combat.contra_st_s > 1.0
            else high_zssdw_short
            if combat.contra_zssdw >= 3
            else low_zssdw_long
            if combat.contra_st_s > 1.0
            else low_zssdw_short
        )
        if hp_ok and combat.rage > threshold:
            self._cast(builder, state, "英勇打击", source_ref, helper_no_sink_attempts=helper_no_sink)

    def _two_hand_b(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        combat: FuryExpertState,
        helper_no_sink: list[JSONMap],
    ) -> None:
        assert state.entry_target_max_health is not None
        hp, rage = combat.target_health_pct, combat.rage
        if hp < 20.0 and combat.slam_remaining_s > 0.2:
            builder.emit_stop_cast(source_ref="Contra_Scrip_Warrior.lua:759-761")
        if (hp < 20.0 and rage >= 62.0) or (hp < 20.0 and combat.whirlwind_ready_in_s > 0.5 and rage > 19.0) or (hp < 20.0 and combat.whirlwind_ready_in_s > 0.5 and rage < 20.0):
            self._cast(builder, state, "斩杀", "Contra_Scrip_Warrior.lua:763-765", helper_no_sink_attempts=helper_no_sink)
        if not combat.in_melee_range:
            return
        maxhp = state.entry_target_max_health
        if maxhp >= 38000:
            if (combat.whirlwind_ready_in_s > combat.contra_sd_s * 2 and rage > 34.0) or (combat.whirlwind_ready_in_s > combat.contra_sd_s and rage > 59.0) or (combat.whirlwind_ready_in_s < combat.contra_sd_s and rage > 59.0):
                self._cast(builder, state, "顺劈斩", "Contra_Scrip_Warrior.lua:767-771", helper_no_sink_attempts=helper_no_sink)
            if self.profile.xuanfeng and combat.whirlwind_ready_in_s == 0.0 and _whirlwind_range(combat):
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:773-775", helper_no_sink_attempts=helper_no_sink)
            if (hp > 20.0 and combat.bloodthirst_ready_in_s == 0.0 and combat.whirlwind_ready_in_s > 7.0 and rage > 89.0) or (hp > 20.0 and combat.bloodthirst_ready_in_s == 0.0 and combat.whirlwind_ready_in_s > combat.contra_sd_s * 2 and rage > 94.0) or (hp > 20.0 and combat.bloodthirst_ready_in_s == 0.0 and combat.contra_sd_s < combat.whirlwind_ready_in_s < combat.contra_sd_s * 2 and rage > 99.0) or (hp > 20.0 and combat.bloodthirst_ready_in_s == 0.0 and combat.whirlwind_ready_in_s < combat.contra_sd_s and 1.4 < combat.whirlwind_ready_in_s <= 7.0 and rage > 94.0):
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:777-779", helper_no_sink_attempts=helper_no_sink)
            if hp > 20.0 and combat.contra_ss_s < combat.slam_cast_time_s and combat.whirlwind_ready_in_s > 0.5:
                self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:781-783", helper_no_sink_attempts=helper_no_sink)
        if 15000 <= maxhp < 38000:
            if rage > 44.0 or (combat.whirlwind_ready_in_s > combat.contra_sd_s and rage > 19.0):
                self._cast(builder, state, "顺劈斩", "Contra_Scrip_Warrior.lua:786-790", helper_no_sink_attempts=helper_no_sink)
            if self.profile.xuanfeng and combat.whirlwind_ready_in_s == 0.0 and _whirlwind_range(combat):
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:792-794", helper_no_sink_attempts=helper_no_sink)
            if hp > 20.0 and combat.bloodthirst_ready_in_s == 0.0 and combat.whirlwind_ready_in_s > combat.contra_st_s and ((rage > 84.0 and combat.whirlwind_ready_in_s < combat.contra_sd_s * 2) or (rage > 59.0 and combat.whirlwind_ready_in_s > combat.contra_sd_s * 2)):
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:796-798", helper_no_sink_attempts=helper_no_sink)
            if hp > 30.0 and combat.contra_st_s > combat.slam_cast_time_s and combat.whirlwind_ready_in_s > 1.4:
                self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:800-802", helper_no_sink_attempts=helper_no_sink)
        if maxhp < 15000:
            if rage > 44.0 or (combat.whirlwind_ready_in_s > combat.contra_sd_s and rage > 19.0):
                self._cast(builder, state, "顺劈斩", "Contra_Scrip_Warrior.lua:805-809", helper_no_sink_attempts=helper_no_sink)
            if hp > 20.0 and combat.whirlwind_ready_in_s > 1.4 and rage > 29.0:
                self._cast(builder, state, "嗜血", "Contra_Scrip_Warrior.lua:811-813", helper_no_sink_attempts=helper_no_sink)
            if self.profile.xuanfeng and combat.whirlwind_ready_in_s == 0.0 and _whirlwind_range(combat):
                self._cast(builder, state, "旋风斩", "Contra_Scrip_Warrior.lua:815-817", helper_no_sink_attempts=helper_no_sink)
            if hp > 25.0 and combat.contra_st_s > combat.slam_cast_time_s and combat.whirlwind_ready_in_s > 2.0 and combat.bloodthirst_ready_in_s > 2.0:
                self._cast(builder, state, "猛击", "Contra_Scrip_Warrior.lua:819-821", helper_no_sink_attempts=helper_no_sink)

    def _cast(
        self,
        builder: _DecisionBuilder,
        state: Contra260817FuryFullPolicyStateV3,
        localized: str,
        source_ref: str,
        *,
        helper_no_sink_attempts: list[JSONMap],
    ) -> bool:
        assert state.spell_rage_costs is not None
        if self._reserve_guard_blocks(state, localized):
                helper_no_sink_attempts.append(
                    {
                        "operation": "ContraZSCast",
                        "value": localized,
                        "reason": "liunudaduan_reserve_guard_return",
                        "source_ref": source_ref,
                    }
                )
                return False
        if localized in {"英勇打击", "顺劈斩"}:
            queue = SwingQueueOp.HEROIC_STRIKE if localized == "英勇打击" else SwingQueueOp.CLEAVE
            present = (
                state.heroic_strike_probe_texture_present
                if queue is SwingQueueOp.HEROIC_STRIKE
                else state.cleave_probe_texture_present
            )
            if present is not True:
                helper_no_sink_attempts.append(
                    {
                        "operation": "Contra.IsHeroicStrikActive",
                        "value": localized,
                        "reason": (
                            "Rogue_Ambush_texture_not_found"
                            if queue is SwingQueueOp.HEROIC_STRIKE
                            else "Warrior_Cleave_texture_not_found"
                        ),
                        "source_ref": source_ref,
                    }
                )
                return False
            if state.entry_combat.queued_swing is queue:
                # The source tests IsCurrentAction *at this call*, after WW.
                # An accepted WW can clear the action's current marker inside
                # the same key (observed by the equivalent-action client B
                # probe). Keep this conditional call for the ordered executor.
                deferred_after_ww = (
                    queue is SwingQueueOp.CLEAVE
                    and any(
                        sink.channel == "gcd" and sink.value == "旋风斩"
                        for sink in builder.raw
                    )
                )
                if deferred_after_ww:
                    helper_no_sink_attempts.append(
                        {
                            "operation": "Contra.IsHeroicStrikActive",
                            "value": localized,
                            "reason": "IsCurrentAction_deferred_until_after_whirlwind",
                            "source_ref": source_ref,
                        }
                    )
                else:
                    helper_no_sink_attempts.append(
                        {
                            "operation": "Contra.IsHeroicStrikActive",
                            "value": localized,
                            "reason": "IsCurrentAction_returned_true",
                            "source_ref": source_ref,
                        }
                    )
                    return False
            builder.emit_queue(
                queue,
                operation="CastSpellByName",
                value=localized,
                source_ref=source_ref,
            )
            return True
        if localized in {"旋风斩", "猛击"} and state.offhand_is_shield_after_prelude:
            helper_no_sink_attempts.append(
                {
                    "operation": "ContraZSCast",
                    "value": localized,
                    "reason": "offhand_shield_guard_return",
                    "source_ref": source_ref,
                }
            )
            return False
        stance = _STANCE_NAMES.get(localized)
        if stance is not None:
            builder.emit_stance(
                stance,
                operation="ContraZSCast",
                value=localized,
                source_ref=source_ref,
            )
            return True
        action = _LOCALIZED_ACTIONS.get(localized, f"contra260817.localized:{localized}")
        if localized in _OFF_GCD_NAMES:
            builder.emit_off_gcd(
                action,
                operation="CastSpellByName",
                value=localized,
                source_ref=source_ref,
            )
        else:
            builder.emit_gcd(
                action,
                operation="CastSpellByName",
                value=localized,
                source_ref=source_ref,
            )
        return True

    def _reserve_guard_blocks(
        self,
        state: Contra260817FuryFullPolicyStateV3,
        localized: str,
    ) -> bool:
        assert state.spell_rage_costs is not None
        return (
            self.profile.reserve_ten_rage_for_interrupt
            and self.profile.interrupt_enabled
            and state.post_target_combat.rage
            - float(state.spell_rage_costs[localized])
            < 10.0
        )

    @staticmethod
    def _emit_inventory_item(builder: _DecisionBuilder, slot: int, source_ref: str) -> None:
        builder.raw.append(RawSink("item", "UseInventoryItem", str(slot), source_ref))

    @staticmethod
    def _emit_direct_spell(
        builder: _DecisionBuilder,
        localized: str,
        source_ref: str,
    ) -> None:
        """Emit direct CastSpellByName calls that bypass ContraZSCast."""

        stance = _STANCE_NAMES.get(localized)
        if stance is not None:
            builder.emit_stance(
                stance,
                operation="CastSpellByName",
                value=localized,
                source_ref=source_ref,
            )
            return
        builder.emit_gcd(
            _LOCALIZED_ACTIONS.get(
                localized, f"contra260817.localized:{localized}"
            ),
            operation="CastSpellByName",
            value=localized,
            source_ref=source_ref,
        )

    @staticmethod
    def _emit_named_item(builder: _DecisionBuilder, name: str, source_ref: str) -> None:
        builder.raw.append(RawSink("item", "Contra.UseItemByName", name, source_ref))

    @staticmethod
    def _emit_weapon_if_needed(
        builder: _DecisionBuilder,
        equipped: str | None,
        requested: str,
        slot: int,
        source_ref: str,
    ) -> None:
        if requested and requested != "无" and equipped != requested:
            builder.raw.append(
                RawSink(
                    "equipment",
                    "Contra.EquipItemByName",
                    f"{requested}@{slot}",
                    source_ref,
                )
            )

    def _invalid_metadata(self, errors: Sequence[str]) -> JSONMap:
        return {
            "schema": STATE_SCHEMA,
            "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
            "adapter_file_sha256": self.adapter_file_sha256,
            "source_code_manifest_sha256": CODE_MANIFEST_SHA256,
            "source_identity_verified": self.source_identity_verified,
            "profile": self.profile.to_dict(),
            "fail_closed": True,
            "input_errors": list(dict.fromkeys(errors)),
            "comparison_ready": False,
            "eligible_for_independent_vote": False,
            "blockers": _typed_blockers(),
        }

    def _metadata(
        self,
        state: Contra260817FuryFullPolicyStateV3,
        *,
        route: str,
        traversal_returns: Sequence[str],
        helper_no_sink_attempts: Sequence[Mapping[str, Any]],
        target_helper_trace: Sequence[Mapping[str, Any]],
        effective_nearby_count: int | None,
        count_fallback_applied: bool = False,
    ) -> JSONMap:
        return {
            "schema": STATE_SCHEMA,
            "macro_parameter": "c",
            "mode": self.profile.mode,
            "talent": state.talent.value,
            "weapon_mode": state.entry_combat.weapon_mode.value,
            "route": route,
            "attackable_units_within_five_yards": state.attackable_units_within_five_yards,
            "effective_nearby_count": effective_nearby_count,
            "count_zero_target_fallback_applied": count_fallback_applied,
            "entry_snapshot_precedes_autoselect": True,
            "entry_target_name": state.entry_target_name,
            "post_target_name": state.post_target_name,
            "derived_zssdw": (
                derive_zssdw_from_loadout(state.equipped_item_names)
                if isinstance(state.equipped_item_names, tuple)
                else None
            ),
            "zssdw_source_items": sorted(BROTHERHOOD_SET_ITEMS),
            "target_helper_trace": [dict(row) for row in target_helper_trace],
            "traversal_returns": list(traversal_returns),
            "helper_no_sink_attempts": [
                dict(row) for row in helper_no_sink_attempts
                if row.get("reason") != "IsCurrentAction_deferred_until_after_whirlwind"
            ],
            "deferred_queue_guard_checks": [
                dict(row) for row in helper_no_sink_attempts
                if row.get("reason") == "IsCurrentAction_deferred_until_after_whirlwind"
            ],
            "profile": self.profile.to_dict(),
            "profile_semantic_sha256": self.profile.semantic_sha256,
            "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
            "adapter_file_sha256": self.adapter_file_sha256,
            "source_code_manifest_sha256": CODE_MANIFEST_SHA256,
            "source_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "source_identity_verified": self.source_identity_verified,
            "source_execution": False,
            "source_derived_diagnostic_executable": True,
            "runtime_profile_observed": False,
            "client_trace_observed": False,
            "server_outcome_observed": False,
            "development_sensitivity_only": True,
            "evaluation_scope": "SOURCE_DERIVED_DIAGNOSTIC",
            "voting_status": "NONVOTING",
            "comparison_ready": False,
            "eligible_for_independent_vote": False,
            "deployed_contra_substitution_used": False,
            "ordered_return_semantics_preserved": True,
            "blockers": _typed_blockers(),
            "field_evidence": {
                "authorization": _evidence_dict(state.authorization_evidence),
                "route": _evidence_dict(state.route_evidence),
                "entry_snapshot": _evidence_dict(state.entry_snapshot_evidence),
                "post_target_snapshot": _evidence_dict(state.post_target_snapshot_evidence),
                "target_selection": _evidence_dict(state.target_selection_evidence),
                "actionbar": _evidence_dict(state.actionbar_evidence),
                "equipment": _evidence_dict(state.equipment_evidence),
                "profile_runtime": _evidence_dict(state.profile_runtime_evidence),
            },
        }


def build_source_trace_v3(
    adapter: Contra260817FuryFullPolicyAdapterV3,
    state: Contra260817FuryFullPolicyStateV3,
    *,
    fixture_id: str,
) -> JSONMap:
    """Build and independently validate one synthetic source traversal trace."""

    if not isinstance(adapter, Contra260817FuryFullPolicyAdapterV3):
        raise TypeError("adapter must be Contra260817FuryFullPolicyAdapterV3")
    _nonempty(fixture_id, "fixture_id")
    decision = adapter.propose(state)
    validate_source_decision_v3(decision)
    core: JSONMap = {
        "schema": TRACE_SCHEMA,
        "fixture_id": fixture_id,
        "synthetic": True,
        "source_execution": False,
        "client_acceptance_observed": False,
        "server_outcome_observed": False,
        "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
        "profile_semantic_sha256": adapter.profile.semantic_sha256,
        "decision": decision.to_dict(),
        "comparison_ready": False,
    }
    return {**core, "content_address": _content_address(core)}


def validate_source_decision_v3(decision: ExpertDecision) -> ExpertDecision:
    """Re-derive lane/order integrity; never treat it as runtime evidence."""

    if not isinstance(decision, ExpertDecision):
        raise TypeError("decision must be ExpertDecision")
    if decision.expert_id != POLICY_ID:
        raise Contra260817FullPolicyV3Error("unexpected expert identity")
    if decision.provenance.kind is not ProvenanceKind.SOURCE_DERIVED:
        raise Contra260817FullPolicyV3Error("proposal provenance must be SOURCE_DERIVED")
    if decision.provenance.role is not ExpertRole.CANDIDATE:
        raise Contra260817FullPolicyV3Error("proposal role must remain CANDIDATE")
    if decision.eligible_for_independent_vote:
        raise Contra260817FullPolicyV3Error("source diagnostic cannot vote")
    if decision.metadata.get("comparison_ready") is not False:
        raise Contra260817FullPolicyV3Error("source diagnostic cannot be comparison ready")
    if decision.metadata.get("adapter_contract_sha256") != ADAPTER_CONTRACT_SHA256:
        raise Contra260817FullPolicyV3Error("adapter contract identity mismatch")
    if decision.metadata.get("adapter_file_sha256") != _stable_file_sha256(
        Path(__file__)
    ):
        raise Contra260817FullPolicyV3Error("adapter file identity mismatch")
    if decision.metadata.get("source_identity_verified") is not True:
        raise Contra260817FullPolicyV3Error("source identity was not verified")
    for index, sink in enumerate(decision.raw_sink_order, start=1):
        if sink.channel not in ADAPTER_CONTRACT["sink_channels"]:
            raise Contra260817FullPolicyV3Error(f"raw sink {index} has unknown channel")
        if not sink.source_ref:
            raise Contra260817FullPolicyV3Error(f"raw sink {index} lacks source ref")
    raw_queue = [
        sink.value for sink in decision.raw_sink_order if sink.channel == "swing_queue"
    ]
    if raw_queue:
        expected = {
            "英勇打击": SwingQueueOp.HEROIC_STRIKE,
            "顺劈斩": SwingQueueOp.CLEAVE,
        }[str(raw_queue[-1])]
        if decision.swing_queue is not expected:
            raise Contra260817FullPolicyV3Error("normalized queue is not the last raw queue")
    elif decision.swing_queue is not SwingQueueOp.KEEP:
        raise Contra260817FullPolicyV3Error("normalized queue has no raw source sink")
    has_stop = any(sink.channel == "cast_control" for sink in decision.raw_sink_order)
    if has_stop != (decision.cast_control is CastControl.STOP_CAST):
        raise Contra260817FullPolicyV3Error("normalized stop-cast lane mismatch")
    raw_target = [sink for sink in decision.raw_sink_order if sink.channel == "target"]
    if not raw_target and decision.target is not TargetOp.KEEP:
        raise Contra260817FullPolicyV3Error("normalized target has no raw target sink")
    return decision


def build_readiness_report_v3(
    *,
    profile: Contra260817FuryProfileV3 = SOURCE_DEFAULT_PROFILE_V3,
    manifest_path: Path = DEFAULT_MANIFEST,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    verify_live_source: bool = True,
) -> JSONMap:
    """Create a non-promoting readiness report from independently derived facts."""

    if not isinstance(profile, Contra260817FuryProfileV3):
        raise TypeError("profile must be Contra260817FuryProfileV3")
    source_verification: JSONMap
    if verify_live_source:
        try:
            source_verification = verify_contra260817_source_package(
                manifest_path=manifest_path,
                source_root=source_root,
            )
            source_status = "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE"
        except Exception as error:
            source_verification = {
                "schema": "contra260817_source_verification/v1",
                "source_identity_verified": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
            source_status = "SOURCE_IDENTITY_VERIFICATION_FAILED"
    else:
        source_verification = {
            "schema": "contra260817_source_verification/v1",
            "source_identity_verified": False,
            "result": "NOT_REQUESTED",
        }
        source_status = "SOURCE_IDENTITY_NOT_LIVE_ATTESTED"

    adapter_path = Path(__file__).resolve(strict=True)
    adapter_file_sha256 = _stable_file_sha256(adapter_path)
    blockers = _typed_blockers()
    if source_status != "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE":
        blockers.insert(
            0,
            {
                "code": source_status,
                "message": "the pinned Contra260817 source identity was not verified live",
                "scope": "SOURCE_IDENTITY",
                "execution_fatal": True,
            },
        )
    if profile.origin is ProfileOriginV3.SOURCE_DEFAULT_DIAGNOSTIC:
        blockers.insert(
            1,
            {
                "code": "SOURCE_DEFAULT_PROFILE_IS_NOT_RUNTIME_PROFILE",
                "message": "defaults plus InitWarriorMiniUI are a diagnostic interpretation only",
                "scope": "PROFILE",
                "execution_fatal": False,
            },
        )
    elif profile.origin is ProfileOriginV3.SYNTHETIC_FIXTURE:
        blockers.insert(
            1,
            {
                "code": "SYNTHETIC_PROFILE_NOT_RUNTIME_PROFILE",
                "message": "synthetic profile exercises branches but is not player evidence",
                "scope": "PROFILE",
                "execution_fatal": False,
            },
        )
    else:
        blockers.insert(
            1,
            {
                "code": "EXPLICIT_PROFILE_ARTIFACT_NOT_RUNTIME_ATTESTED",
                "message": "an explicit artifact hash alone cannot attest client load or use",
                "scope": "PROFILE",
                "execution_fatal": False,
            },
        )

    core: JSONMap = {
        "schema": READINESS_SCHEMA,
        "policy_id": REQUIRED_BASELINE_POLICY_ID,
        "adapter_expert_id": POLICY_ID,
        "source_identity": {
            "manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "code_manifest_sha256": CODE_MANIFEST_SHA256,
            "status": source_status,
            "verification": source_verification,
            "manifest_comparison_blockers_are_frozen_v1_snapshot": True,
            "v3_closes_source_translation_only": True,
            "v3_closes_package_or_runtime": False,
        },
        "profile_identity": profile.to_dict(),
        "adapter_identity": {
            "contract_id": ADAPTER_CONTRACT_ID,
            "contract_sha256": ADAPTER_CONTRACT_SHA256,
            "file": str(adapter_path),
            "file_sha256": adapter_file_sha256,
        },
        "source_derived_coverage": {
            "macro_c_router": True,
            "two_hand_single_A": True,
            "two_hand_multi_B": True,
            "dual_wield_single_A": True,
            "dual_wield_multi_B": True,
            "target_helper": True,
            "next_swing_helper": True,
            "interrupt_stop_cast_and_return": True,
            "burst_support_survival": True,
            "sunder_return": True,
            "ordered_sink_ledger": True,
            "synthetic_trace_executable": True,
        },
        "readiness": {
            "source_identity": source_status == "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE",
            "package_complete": False,
            "runtime_profile": False,
            "runtime_load": False,
            "name_and_guild_authorization": False,
            "client_ordered_sink_trace": False,
            "client_acceptance_trace": False,
            "server_outcome_trace": False,
            "ordered_executor_operation_coverage": False,
            "full_policy_rollout_registration": False,
        },
        "blockers": blockers,
        "source_derived_diagnostic_executable": (
            source_status == "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE"
        ),
        "runtime_closed": False,
        "comparison_ready": False,
        "eligible_for_independent_vote": False,
        "runner_registration_authorized": False,
        "deployed_contra_substitution_used": False,
        "claim_boundary": (
            "source-derived synthetic traversal only; neither a client trace nor "
            "a DPS comparison receipt"
        ),
    }
    report = {**core, "content_address": _content_address(core)}
    return validate_readiness_report_v3(report)


def validate_readiness_report_v3(report: Mapping[str, Any]) -> JSONMap:
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    result = json.loads(json.dumps(report, ensure_ascii=False))
    if result.get("schema") != READINESS_SCHEMA:
        raise Contra260817FullPolicyV3Error("readiness schema mismatch")
    if result.get("policy_id") != REQUIRED_BASELINE_POLICY_ID:
        raise Contra260817FullPolicyV3Error("readiness policy identity mismatch")
    if result.get("adapter_expert_id") != POLICY_ID:
        raise Contra260817FullPolicyV3Error("readiness adapter expert identity mismatch")
    address = result.get("content_address")
    if not isinstance(address, Mapping):
        raise Contra260817FullPolicyV3Error("content address missing")
    core = {key: value for key, value in result.items() if key != "content_address"}
    if dict(address) != _content_address(core):
        raise Contra260817FullPolicyV3Error("readiness content address mismatch")
    if result.get("comparison_ready") is not False:
        raise Contra260817FullPolicyV3Error("v3 readiness cannot self-promote")
    if result.get("eligible_for_independent_vote") is not False:
        raise Contra260817FullPolicyV3Error("v3 readiness cannot authorize a vote")
    if result.get("runner_registration_authorized") is not False:
        raise Contra260817FullPolicyV3Error("v3 readiness cannot authorize registration")
    if result.get("runtime_closed") is not False:
        raise Contra260817FullPolicyV3Error("v3 readiness cannot close runtime evidence")
    if result.get("deployed_contra_substitution_used") is not False:
        raise Contra260817FullPolicyV3Error("deployed Contra substitution is forbidden")

    source = result.get("source_identity")
    if not isinstance(source, Mapping):
        raise Contra260817FullPolicyV3Error("source identity missing")
    if source.get("manifest_sha256") != EXPECTED_MANIFEST_SHA256 or source.get(
        "code_manifest_sha256"
    ) != CODE_MANIFEST_SHA256:
        raise Contra260817FullPolicyV3Error("source identity digest mismatch")
    source_status = source.get("status")
    if source_status not in {
        "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE",
        "SOURCE_IDENTITY_VERIFICATION_FAILED",
        "SOURCE_IDENTITY_NOT_LIVE_ATTESTED",
    }:
        raise Contra260817FullPolicyV3Error("source identity status is unknown")
    verification = source.get("verification")
    if not isinstance(verification, Mapping):
        raise Contra260817FullPolicyV3Error("source verification facts missing")
    if (
        source.get("manifest_comparison_blockers_are_frozen_v1_snapshot") is not True
        or source.get("v3_closes_source_translation_only") is not True
        or source.get("v3_closes_package_or_runtime") is not False
    ):
        raise Contra260817FullPolicyV3Error("source closure boundary drifted")
    if source_status == "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE":
        code_manifest = verification.get("code_manifest")
        if (
            verification.get("source_identity_verified") is not True
            or not isinstance(code_manifest, Mapping)
            or code_manifest.get("sha256") != CODE_MANIFEST_SHA256
        ):
            raise Contra260817FullPolicyV3Error(
                "verified source status lacks matching verification facts"
            )

    adapter = result.get("adapter_identity")
    if not isinstance(adapter, Mapping):
        raise Contra260817FullPolicyV3Error("adapter identity missing")
    if adapter.get("contract_id") != ADAPTER_CONTRACT_ID or adapter.get(
        "contract_sha256"
    ) != ADAPTER_CONTRACT_SHA256:
        raise Contra260817FullPolicyV3Error("adapter contract identity mismatch")
    if adapter.get("file") != str(Path(__file__).resolve(strict=True)):
        raise Contra260817FullPolicyV3Error("adapter file path identity mismatch")
    if adapter.get("file_sha256") != _stable_file_sha256(Path(__file__)):
        raise Contra260817FullPolicyV3Error("adapter file SHA-256 mismatch")

    profile = result.get("profile_identity")
    if not isinstance(profile, Mapping):
        raise Contra260817FullPolicyV3Error("profile identity missing")
    semantic_keys = (
        "schema",
        "profile_id",
        "origin",
        "source_version",
        "mode",
        "buttons",
        "burst_stages",
        "survival",
        "evidence_sha256",
        "profile_artifact_sha256",
    )
    if set(profile) != {
        *semantic_keys,
        "semantic_sha256",
        "runtime_profile_observed",
        "comparison_ready",
        "eligible_for_independent_vote",
    }:
        raise Contra260817FullPolicyV3Error("profile identity fields mismatch")
    semantic_profile = {key: profile.get(key) for key in semantic_keys}
    if profile.get("semantic_sha256") != _sha256_json(semantic_profile):
        raise Contra260817FullPolicyV3Error("profile semantic SHA-256 mismatch")
    for field in (
        "runtime_profile_observed",
        "comparison_ready",
        "eligible_for_independent_vote",
    ):
        if profile.get(field) is not False:
            raise Contra260817FullPolicyV3Error(
                f"profile {field} must remain false"
            )

    origin = profile.get("origin")
    expected_profile_blocker = {
        ProfileOriginV3.SOURCE_DEFAULT_DIAGNOSTIC.value: (
            "SOURCE_DEFAULT_PROFILE_IS_NOT_RUNTIME_PROFILE"
        ),
        ProfileOriginV3.SYNTHETIC_FIXTURE.value: (
            "SYNTHETIC_PROFILE_NOT_RUNTIME_PROFILE"
        ),
        ProfileOriginV3.EXPLICIT_ARTIFACT_UNATTESTED.value: (
            "EXPLICIT_PROFILE_ARTIFACT_NOT_RUNTIME_ATTESTED"
        ),
    }.get(origin)
    if expected_profile_blocker is None:
        raise Contra260817FullPolicyV3Error("profile origin is unknown")

    expected_coverage = {
        "macro_c_router": True,
        "two_hand_single_A": True,
        "two_hand_multi_B": True,
        "dual_wield_single_A": True,
        "dual_wield_multi_B": True,
        "target_helper": True,
        "next_swing_helper": True,
        "interrupt_stop_cast_and_return": True,
        "burst_support_survival": True,
        "sunder_return": True,
        "ordered_sink_ledger": True,
        "synthetic_trace_executable": True,
    }
    if result.get("source_derived_coverage") != expected_coverage:
        raise Contra260817FullPolicyV3Error("source-derived coverage drifted")
    readiness = result.get("readiness")
    if not isinstance(readiness, Mapping):
        raise Contra260817FullPolicyV3Error("readiness fields missing")
    expected_readiness_keys = {
        "source_identity",
        "package_complete",
        "runtime_profile",
        "runtime_load",
        "name_and_guild_authorization",
        "client_ordered_sink_trace",
        "client_acceptance_trace",
        "server_outcome_trace",
        "ordered_executor_operation_coverage",
        "full_policy_rollout_registration",
    }
    if set(readiness) != expected_readiness_keys:
        raise Contra260817FullPolicyV3Error("readiness field set mismatch")
    if readiness.get("source_identity") is not (
        source_status == "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE"
    ):
        raise Contra260817FullPolicyV3Error("source readiness does not match status")
    forbidden_true = set(readiness) - {"source_identity"}
    if any(readiness.get(name) is not False for name in forbidden_true):
        raise Contra260817FullPolicyV3Error("runtime readiness fields must remain false")
    codes = {
        row.get("code")
        for row in result.get("blockers", [])
        if isinstance(row, Mapping)
    }
    required_codes = {code for code, _ in PROMOTION_BLOCKERS}
    if not required_codes.issubset(codes):
        raise Contra260817FullPolicyV3Error("mandatory typed blocker missing")
    if expected_profile_blocker not in codes:
        raise Contra260817FullPolicyV3Error("profile-origin typed blocker missing")
    expected_blockers = {row["code"]: row for row in _typed_blockers()}
    observed_rows = {
        row.get("code"): row
        for row in result.get("blockers", [])
        if isinstance(row, Mapping)
    }
    for code, expected in expected_blockers.items():
        if observed_rows.get(code) != expected:
            raise Contra260817FullPolicyV3Error(
                f"mandatory typed blocker {code} drifted"
            )
    diagnostic_expected = source_status == "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE"
    if result.get("source_derived_diagnostic_executable") is not diagnostic_expected:
        raise Contra260817FullPolicyV3Error(
            "diagnostic executability does not match source status"
        )
    return result


def serialize_readiness_report_v3(report: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_readiness_report_v3(report)) + b"\n"


def _typed_blockers() -> list[JSONMap]:
    scope_by_code = {
        "PACKAGE_INCOMPLETE": "PACKAGE",
        "PER_CHARACTER_CONTRADB_PROFILE_MISSING": "PROFILE",
        "RUNTIME_LOAD_ATTESTATION_MISSING": "RUNTIME",
        "NAME_AND_GUILD_RUNTIME_ATTESTATION_MISSING": "RUNTIME",
        "CLIENT_ORDERED_SINK_TRACE_MISSING": "CLIENT_TRACE",
        "CLIENT_ACCEPTANCE_TRACE_MISSING": "CLIENT_TRACE",
        "SERVER_OUTCOME_TRACE_MISSING": "SERVER_TRACE",
        "ORDERED_SINK_EXECUTOR_V3_OPERATION_COVERAGE_MISSING": "SIMULATOR",
        "FULL_POLICY_ROLLOUT_V3_ADAPTER_REGISTRATION_MISSING": "RUNNER",
    }
    fatal = {
        "ORDERED_SINK_EXECUTOR_V3_OPERATION_COVERAGE_MISSING",
        "FULL_POLICY_ROLLOUT_V3_ADAPTER_REGISTRATION_MISSING",
    }
    return [
        {
            "code": code,
            "message": message,
            "scope": scope_by_code[code],
            "execution_fatal": code in fatal,
        }
        for code, message in PROMOTION_BLOCKERS
    ]


def _validate_evidence(
    evidence: ContraFieldEvidenceV2 | None,
    code: str,
    errors: list[str],
) -> None:
    if not isinstance(evidence, ContraFieldEvidenceV2) or evidence.kind is ContraEvidenceKindV2.MISSING:
        errors.append(code)


def _evidence_dict(evidence: ContraFieldEvidenceV2 | None) -> JSONMap | None:
    return evidence.to_dict() if isinstance(evidence, ContraFieldEvidenceV2) else None


def _whirlwind_range(combat: FuryExpertState) -> bool:
    return combat.target_distance_yards < (5.5 if combat.race_is_tauren else 8.0)


def _unparenthesized_xuanfeng_range(enabled: bool, combat: FuryExpertState) -> bool:
    return (
        enabled
        and not combat.race_is_tauren
        and combat.target_distance_yards < 8.0
    ) or (combat.race_is_tauren and combat.target_distance_yards < 5.5)


def _numeric_mapping(value: Mapping[str, float] | None) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str)
        and key
        and _finite_range(number, 0.0, None)
        for key, number in value.items()
    )


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
        raise Contra260817FullPolicyV3Error(
            f"value is not strict canonical JSON: {error}"
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": _sha256_json(core),
    }


def _stable_file_sha256(path: Path) -> str:
    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise Contra260817FullPolicyV3Error("identity path must be a regular file")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise Contra260817FullPolicyV3Error("identity file changed during read")
    return hashlib.sha256(payload).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--skip-live-source-verification", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_readiness_report_v3(
        manifest_path=args.manifest,
        source_root=args.source_root,
        verify_live_source=not args.skip_live_source_verification,
    )
    payload = serialize_readiness_report_v3(report)
    if args.output is None:
        sys.stdout.buffer.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
    if args.require_ready and not report["comparison_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "ADAPTER_CONTRACT",
    "ADAPTER_CONTRACT_ID",
    "ADAPTER_CONTRACT_SHA256",
    "CONTENT_ADDRESS_ALGORITHM",
    "Contra260817FullPolicyV3Error",
    "Contra260817FuryFullPolicyAdapterV3",
    "Contra260817FuryFullPolicyStateV3",
    "Contra260817FuryProfileV3",
    "Contra260817FuryTalentV3",
    "BurstStageV3",
    "POLICY_ID",
    "PROFILE_SCHEMA",
    "PROMOTION_BLOCKERS",
    "ProfileOriginV3",
    "READINESS_SCHEMA",
    "REQUIRED_BASELINE_POLICY_ID",
    "SCHEMA",
    "SOURCE_DEFAULT_PROFILE_V3",
    "STATE_SCHEMA",
    "SurvivalProfileV3",
    "TRACE_SCHEMA",
    "TargetSelectionStateV3",
    "build_readiness_report_v3",
    "build_source_trace_v3",
    "main",
    "serialize_readiness_report_v3",
    "validate_readiness_report_v3",
    "validate_source_decision_v3",
)
