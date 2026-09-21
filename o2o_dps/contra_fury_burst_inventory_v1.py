"""Source-audited Contra Fury burst inventory for proposal-only search use.

The installed ``Contra/Contra.toc`` currently loads the one-line
``Contra.lua`` runtime body.  ``Contra_ALL.lua`` is a readable counterpart,
not the TOC-loaded policy.  Contra260817 (``Contra_new``) exposes the same
burst helper in ``Contra_Scrip_Warrior.lua``, but its package/runtime closure
is not attested here.

This module deliberately records Contra conditions as provenance strings.  It
does not enforce them and never removes a simulator action.  The simulator is
the authority for action legality and cooldown mechanics; Contra is only a
source of identities and ordering hints.  In particular, named item IDs and
the identities of equipped slot-13/14 trinkets are unresolved until the exact
build/runtime supplies them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .sim_bridge import ActionRef, AvailableAction


JSONMap = dict[str, Any]

EXECUTED_BURST_SINK = "EXECUTED_BURST_SINK"
EXECUTED_SUPPORT_SINK = "EXECUTED_SUPPORT_SINK"
CONFIGURED_NOT_EMITTED = "CONFIGURED_NOT_EMITTED"


@dataclass(frozen=True)
class ContraBurstSourceRefV1:
    package_id: str
    path: str
    locator: str
    role: str

    def __post_init__(self) -> None:
        for label, value in (
            ("package_id", self.package_id),
            ("path", self.path),
            ("locator", self.locator),
            ("role", self.role),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty text")

    def to_dict(self) -> JSONMap:
        return {
            "package_id": self.package_id,
            "path": self.path,
            "locator": self.locator,
            "role": self.role,
        }


@dataclass(frozen=True)
class ContraFuryBurstActionV1:
    action_id: str
    localized_names: tuple[str, ...]
    category: str
    locator_kind: str
    execution_status: str
    source_order: int | None
    cooldown_group_hint: str
    cooldown_group_authority: str
    cooldown_ms: int | None
    spell_id: int | None = None
    item_id: int | None = None
    inventory_slot: int | None = None
    condition_hints: tuple[str, ...] = ()
    source_refs: tuple[ContraBurstSourceRefV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be nonempty text")
        if not isinstance(self.localized_names, tuple) or not self.localized_names:
            raise ValueError("localized_names must be a nonempty tuple")
        if any(not isinstance(name, str) or not name.strip() for name in self.localized_names):
            raise ValueError("localized_names must contain nonempty text")
        if len(set(self.localized_names)) != len(self.localized_names):
            raise ValueError("localized_names must be unique")
        if self.execution_status not in {
            EXECUTED_BURST_SINK,
            EXECUTED_SUPPORT_SINK,
            CONFIGURED_NOT_EMITTED,
        }:
            raise ValueError("unknown execution_status")
        if self.source_order is not None and (
            isinstance(self.source_order, bool)
            or not isinstance(self.source_order, int)
            or self.source_order <= 0
        ):
            raise ValueError("source_order must be a positive integer or None")
        if not isinstance(self.cooldown_group_hint, str) or not self.cooldown_group_hint.strip():
            raise ValueError("cooldown_group_hint must be nonempty text")
        if not isinstance(self.cooldown_group_authority, str) or not self.cooldown_group_authority.strip():
            raise ValueError("cooldown_group_authority must be nonempty text")
        if self.cooldown_ms is not None and (
            isinstance(self.cooldown_ms, bool)
            or not isinstance(self.cooldown_ms, int)
            or self.cooldown_ms <= 0
        ):
            raise ValueError("cooldown_ms must be a positive integer or None")
        identities = (
            self.spell_id is not None,
            self.item_id is not None,
            self.inventory_slot is not None,
        )
        if sum(identities) > 1:
            raise ValueError("an action may have only one concrete locator")
        if self.locator_kind == "SPELL" and self.spell_id is None:
            raise ValueError("SPELL actions require spell_id")
        if self.locator_kind == "NAMED_ITEM" and any(identities):
            raise ValueError("Contra named items must remain unresolved by source name")
        if self.locator_kind == "EQUIPPED_SLOT" and self.inventory_slot not in {13, 14}:
            raise ValueError("EQUIPPED_SLOT must use slot 13 or 14")
        if any(not isinstance(ref, ContraBurstSourceRefV1) for ref in self.source_refs):
            raise TypeError("source_refs must contain ContraBurstSourceRefV1")

    @property
    def executable(self) -> bool:
        return self.execution_status != CONFIGURED_NOT_EMITTED

    def to_dict(self) -> JSONMap:
        result: JSONMap = {
            "action_id": self.action_id,
            "localized_names": list(self.localized_names),
            "category": self.category,
            "locator_kind": self.locator_kind,
            "execution_status": self.execution_status,
            "executable": self.executable,
            "source_order": self.source_order,
            "cooldown_group_hint": self.cooldown_group_hint,
            "cooldown_group_authority": self.cooldown_group_authority,
            "cooldown_ms": self.cooldown_ms,
            "condition_hints": list(self.condition_hints),
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }
        if self.spell_id is not None:
            result["spell_id"] = self.spell_id
        if self.item_id is not None:
            result["item_id"] = self.item_id
        if self.inventory_slot is not None:
            result["inventory_slot"] = self.inventory_slot
        return result


@dataclass(frozen=True)
class ContraFuryBurstTriggerV1:
    trigger_id: str
    selected_name: str
    required_buffs_any: tuple[str, ...]
    directly_emitted_by_trigger_selection: bool
    source_refs: tuple[ContraBurstSourceRefV1, ...]

    def to_dict(self) -> JSONMap:
        return {
            "trigger_id": self.trigger_id,
            "selected_name": self.selected_name,
            "required_buffs_any": list(self.required_buffs_any),
            "directly_emitted_by_trigger_selection": self.directly_emitted_by_trigger_selection,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }


@dataclass(frozen=True)
class ContraFuryBurstInventoryV1:
    actions: tuple[ContraFuryBurstActionV1, ...]
    manual_triggers: tuple[ContraFuryBurstTriggerV1, ...]

    def __post_init__(self) -> None:
        action_ids = [row.action_id for row in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action_id values must be unique")
        trigger_ids = [row.trigger_id for row in self.manual_triggers]
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("trigger_id values must be unique")

    def to_dict(self) -> JSONMap:
        return {
            "schema": "contra_fury_burst_inventory/v1",
            "runtime_authority": {
                "deployed": {
                    "toc_entry": "Contra.lua",
                    "loaded_policy": "Contra/Contra.lua",
                    "source_shape": "ONE_LINE_MINIFIED_RUNTIME",
                    "readable_counterpart": "Contra/Contra_ALL.lua",
                    "readable_counterpart_loaded_by_toc": False,
                },
                "contra260817": {
                    "local_alias": "Contra_new",
                    "warrior_source": "Contra_new/Contra_Scrip_Warrior.lua",
                    "toc_listed": True,
                    "runtime_execution_attested": False,
                },
            },
            "guide_contract": {
                "contra_conditions_are_hints_only": True,
                "simulator_actions_define_membership": True,
                "unmatched_legal_actions_receive_zero_weight": True,
                "named_item_ids_inferred_from_contra_source": False,
                "cooldown_ms_inferred_from_contra_source": False,
            },
            "actions": [row.to_dict() for row in self.actions],
            "manual_triggers": [row.to_dict() for row in self.manual_triggers],
            "source_findings": [
                "slot 13 and slot 14 are dynamic UseInventoryItem sinks; the source does not identify the equipped trinkets",
                "the UI/configuration exposes Sweeping Strikes as a follower, but ZS_BAOF1 and ZS_BAOF2 never emit it",
                "Slayer's Crest and Kiss of the Spider are manual aura triggers, not direct named-item sinks",
                "the deployed frozen runtime profile may disable the Burst outer gate; this inventory is source capability, not proof of an active cast",
            ],
        }


_DEPLOYED_BURST = ContraBurstSourceRefV1(
    "contra.deployed.fury",
    "Contra/Contra.lua",
    "line 1; function Contra.ZS_BAOF1/ZS_BAOF2/ZS_BAOF",
    "TOC_LOADED_RUNTIME_EXECUTION",
)
_DEPLOYED_READABLE = ContraBurstSourceRefV1(
    "contra.deployed.fury",
    "Contra/Contra_ALL.lua",
    "31790-31850",
    "READABLE_COUNTERPART_NOT_TOC_LOADED",
)
_NEW_BURST = ContraBurstSourceRefV1(
    "contra260817.fury",
    "Contra_new/Contra_Scrip_Warrior.lua",
    "328-408",
    "TOC_LISTED_SOURCE_RUNTIME_UNATTESTED",
)
_BURST_REFS = (_DEPLOYED_BURST, _DEPLOYED_READABLE, _NEW_BURST)

_DEPLOYED_SUPPORT = ContraBurstSourceRefV1(
    "contra.deployed.fury",
    "Contra/Contra.lua",
    "line 1; function Contra.ZS_FZ",
    "TOC_LOADED_RUNTIME_EXECUTION",
)
_DEPLOYED_SUPPORT_READABLE = ContraBurstSourceRefV1(
    "contra.deployed.fury",
    "Contra/Contra_ALL.lua",
    "31852-31876",
    "READABLE_COUNTERPART_NOT_TOC_LOADED",
)
_NEW_SUPPORT = ContraBurstSourceRefV1(
    "contra260817.fury",
    "Contra_new/Contra_Scrip_Warrior.lua",
    "410-444",
    "TOC_LISTED_SOURCE_RUNTIME_UNATTESTED",
)
_SUPPORT_REFS = (_DEPLOYED_SUPPORT, _DEPLOYED_SUPPORT_READABLE, _NEW_SUPPORT)

_DEPLOYED_UI = ContraBurstSourceRefV1(
    "contra.deployed.fury",
    "Contra/Contra_ALL.lua",
    "40178,40254-40359",
    "READABLE_CONFIGURATION_NOT_TOC_LOADED_SEPARATELY",
)
_NEW_UI = ContraBurstSourceRefV1(
    "contra260817.fury",
    "Contra_new/Contra_UI_Warrior.lua",
    "41,134-265",
    "TOC_LISTED_CONFIGURATION_RUNTIME_UNATTESTED",
)
_UI_REFS = (_DEPLOYED_UI, _NEW_UI)


def _burst_action(
    action_id: str,
    name: str,
    category: str,
    order: int,
    cooldown_group_hint: str,
    cooldown_group_authority: str,
    *,
    spell_id: int | None = None,
    locator_kind: str = "SPELL",
    inventory_slot: int | None = None,
    aliases: tuple[str, ...] = (),
    conditions: tuple[str, ...] = (),
    refs: tuple[ContraBurstSourceRefV1, ...] = _BURST_REFS,
    status: str = EXECUTED_BURST_SINK,
) -> ContraFuryBurstActionV1:
    return ContraFuryBurstActionV1(
        action_id=action_id,
        localized_names=(name, *aliases),
        category=category,
        locator_kind=locator_kind,
        execution_status=status,
        source_order=order,
        cooldown_group_hint=cooldown_group_hint,
        cooldown_group_authority=cooldown_group_authority,
        cooldown_ms=None,
        spell_id=spell_id,
        inventory_slot=inventory_slot,
        condition_hints=conditions,
        source_refs=refs,
    )


def build_contra_fury_burst_inventory_v1() -> ContraFuryBurstInventoryV1:
    """Return the fixed source inventory without pretending to know item IDs."""

    common = (
        "ContraDB.Warrior.Buttons.Burst is true",
        "stage manual trigger aura is present OR auto boss HP threshold is met",
        "source call success is not checked before later sinks are attempted",
    )
    stage2_melee = (*common, "stage 2 additionally requires melee range for this sink")
    actions = (
        _burst_action(
            "warrior.recklessness", "鲁莽", "WARRIOR_MAJOR_COOLDOWN", 1,
            "warrior.recklessness", "RESOURCE_IDENTITY_ONLY", spell_id=1719,
            conditions=stage2_melee,
        ),
        _burst_action(
            "warrior.death_wish", "死亡之愿", "WARRIOR_MAJOR_COOLDOWN", 2,
            "warrior.death_wish", "RESOURCE_IDENTITY_ONLY", spell_id=12328,
            conditions=stage2_melee,
        ),
        _burst_action(
            "racial.perception", "感知", "RACIAL", 3,
            "racial.perception", "RESOURCE_IDENTITY_ONLY", spell_id=20600,
            conditions=common,
        ),
        _burst_action(
            "racial.blood_fury", "血性狂怒", "RACIAL", 4,
            "racial.blood_fury", "RESOURCE_IDENTITY_ONLY", spell_id=20572,
            conditions=common,
        ),
        _burst_action(
            "racial.berserking", "狂暴", "RACIAL", 5,
            "racial.berserking", "RESOURCE_IDENTITY_ONLY", spell_id=26297,
            conditions=common,
        ),
        _burst_action(
            "trinket.slot_13", "上饰品位", "TRINKET_SLOT", 6,
            "runtime_trinket_item:slot_13", "RUNTIME_ITEM_REQUIRED",
            locator_kind="EQUIPPED_SLOT", inventory_slot=13, conditions=stage2_melee,
        ),
        _burst_action(
            "trinket.slot_14", "下饰品位", "TRINKET_SLOT", 7,
            "runtime_trinket_item:slot_14", "RUNTIME_ITEM_REQUIRED",
            locator_kind="EQUIPPED_SLOT", inventory_slot=14, conditions=stage2_melee,
        ),
        _burst_action(
            "item.mighty_rage_potion", "强效怒气药水", "POTION", 8,
            "combat_potion", "ITEM_CLASS_HINT_REQUIRES_SIMULATOR_BINDING",
            locator_kind="NAMED_ITEM", conditions=common,
        ),
        _burst_action(
            "item.juju_flurry", "魂能之速", "CONSUMABLE", 9,
            "item.juju_flurry", "RESOURCE_IDENTITY_ONLY_REQUIRES_ITEM_BINDING",
            locator_kind="NAMED_ITEM", conditions=stage2_melee,
        ),
        _burst_action(
            "item.goblin_sapper_charge", "地精工兵炸弹", "ENGINEERING_EXPLOSIVE", 10,
            "engineering_explosive", "ITEM_CLASS_HINT_REQUIRES_SIMULATOR_BINDING",
            locator_kind="NAMED_ITEM", aliases=("地精工兵炸药",), conditions=stage2_melee,
        ),
        _burst_action(
            "item.quickness_potion", "加速药水", "POTION", 11,
            "combat_potion", "ITEM_CLASS_HINT_REQUIRES_SIMULATOR_BINDING",
            locator_kind="NAMED_ITEM", conditions=stage2_melee,
        ),
        _burst_action(
            "item.rage_potion", "暴怒药水", "POTION", 12,
            "combat_potion", "ITEM_CLASS_HINT_REQUIRES_SIMULATOR_BINDING",
            locator_kind="NAMED_ITEM", conditions=common,
        ),
        _burst_action(
            "item.elixir_of_rapid_growth", "急速生长药剂", "CONSUMABLE", 13,
            "item.elixir_of_rapid_growth", "RESOURCE_IDENTITY_ONLY_REQUIRES_ITEM_BINDING",
            locator_kind="NAMED_ITEM", conditions=stage2_melee,
        ),
        _burst_action(
            "warrior.sweeping_strikes", "横扫攻击", "WARRIOR_MAJOR_COOLDOWN", 14,
            "warrior.sweeping_strikes", "RESOURCE_IDENTITY_ONLY", spell_id=12292,
            conditions=("configuration checkbox exists", "execution body has no corresponding cast"),
            refs=_UI_REFS, status=CONFIGURED_NOT_EMITTED,
        ),
        _burst_action(
            "warrior.bloodrage", "血性狂暴", "WARRIOR_RESOURCE_COOLDOWN", 15,
            "warrior.bloodrage", "RESOURCE_IDENTITY_ONLY", spell_id=2687,
            conditions=(
                "support toggle xuexing is true",
                "Bloodrage cooldown is zero",
                "player lacks 狂怒 buff",
                "player is in combat and in melee range",
            ),
            refs=_SUPPORT_REFS, status=EXECUTED_SUPPORT_SINK,
        ),
        _burst_action(
            "warrior.berserker_rage", "狂暴之怒", "WARRIOR_UTILITY_COOLDOWN", 16,
            "warrior.berserker_rage", "RESOURCE_IDENTITY_ONLY", spell_id=18499,
            conditions=(
                "support toggle kuangbao is true",
                "Fury source requires target-of-target or a listed fear aura",
                "player is in combat",
            ),
            refs=_SUPPORT_REFS, status=EXECUTED_SUPPORT_SINK,
        ),
    )
    triggers = tuple(
        ContraFuryBurstTriggerV1(trigger_id, selected, buffs, False, _UI_REFS + _BURST_REFS)
        for trigger_id, selected, buffs in (
            ("trigger.great_rage", "强效怒气药水", ("强效怒气",)),
            ("trigger.recklessness", "鲁莽", ("鲁莽",)),
            ("trigger.death_wish", "死亡之愿", ("死亡之愿",)),
            ("trigger.sweeping_strikes", "横扫攻击", ("横扫攻击",)),
            ("trigger.slayers_crest", "屠龙者的纹章", ("屠龙者的纹章",)),
            ("trigger.kiss_of_the_spider", "蜘蛛之吻", ("蜘蛛之吻",)),
            ("trigger.racial", "种族天赋", ("感知", "血性狂怒", "狂暴")),
        )
    )
    return ContraFuryBurstInventoryV1(actions=actions, manual_triggers=triggers)


@dataclass(frozen=True)
class ContraFuryBurstGuideResolutionV1:
    action_priorities: Mapping[ActionRef, float]
    matched_action_ids: tuple[str, ...]
    unresolved_action_ids: tuple[str, ...]
    configured_not_emitted_action_ids: tuple[str, ...]

    def to_dict(self) -> JSONMap:
        return {
            "schema": "contra_fury_burst_guide_resolution/v1",
            "action_priorities": [
                {"action": action.to_wire(), "weight": float(weight)}
                for action, weight in sorted(self.action_priorities.items())
            ],
            "matched_action_ids": list(self.matched_action_ids),
            "unresolved_action_ids": list(self.unresolved_action_ids),
            "configured_not_emitted_action_ids": list(self.configured_not_emitted_action_ids),
            "contract": {
                "all_current_legal_actions_are_present": True,
                "contra_source_conditions_enforced": False,
                "zero_weight_filters_action": False,
            },
        }


def resolve_contra_fury_burst_guide_v1(
    available_actions: Sequence[AvailableAction],
    *,
    named_item_ids: Mapping[str, int] | None = None,
    named_action_refs: Mapping[str, ActionRef] | None = None,
    trinket_item_ids: Mapping[int, int] | None = None,
    inventory: ContraFuryBurstInventoryV1 | None = None,
) -> ContraFuryBurstGuideResolutionV1:
    """Project Contra's source order onto every legal simulator action.

    Item mappings must come from the exact simulator build/runtime.  Supplying a
    mapping resolves identity only; cooldown legality remains simulator-owned.
    """

    resolved_inventory = inventory or build_contra_fury_burst_inventory_v1()
    named = dict(named_item_ids or {})
    named_refs = dict(named_action_refs or {})
    slots = dict(trinket_item_ids or {})
    if any(not isinstance(key, str) or not key.strip() for key in named):
        raise ValueError("named_item_ids keys must be nonempty action IDs")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in named.values()):
        raise ValueError("named_item_ids values must be positive integers")
    if any(not isinstance(key, str) or not key.strip() for key in named_refs):
        raise ValueError("named_action_refs keys must be nonempty action IDs")
    if any(not isinstance(value, ActionRef) for value in named_refs.values()):
        raise TypeError("named_action_refs values must be ActionRef values")
    overlap = sorted(set(named) & set(named_refs))
    if overlap:
        raise ValueError(
            "named_item_ids and named_action_refs overlap: " + ", ".join(overlap)
        )
    if any(slot not in {13, 14} for slot in slots):
        raise ValueError("trinket_item_ids keys must be slot 13 or 14")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in slots.values()):
        raise ValueError("trinket_item_ids values must be positive integers")

    legal: list[ActionRef] = []
    seen: set[ActionRef] = set()
    for row in available_actions:
        if not isinstance(row, AvailableAction):
            raise TypeError("available_actions must contain AvailableAction")
        if row.legal and row.action not in seen:
            seen.add(row.action)
            legal.append(row.action)
    priorities = {action: 0.0 for action in legal}
    matched: list[str] = []
    unresolved: list[str] = []
    configured_only: list[str] = []
    executable = [row for row in resolved_inventory.actions if row.executable]
    max_order = max(row.source_order or 0 for row in executable)
    for row in resolved_inventory.actions:
        if not row.executable:
            configured_only.append(row.action_id)
            continue
        action: ActionRef | None = None
        if row.spell_id is not None:
            action = ActionRef(spell_id=row.spell_id)
        elif row.locator_kind == "NAMED_ITEM":
            action = named_refs.get(row.action_id)
            if action is None:
                item_id = named.get(row.action_id)
                if item_id is not None:
                    action = ActionRef(item_id=item_id)
        elif row.inventory_slot is not None:
            item_id = slots.get(row.inventory_slot)
            if item_id is not None:
                action = ActionRef(item_id=item_id)
        if action is None:
            unresolved.append(row.action_id)
            continue
        if action in priorities:
            priorities[action] = max(
                priorities[action],
                float(max_order + 1 - (row.source_order or max_order)),
            )
            matched.append(row.action_id)
    return ContraFuryBurstGuideResolutionV1(
        action_priorities=priorities,
        matched_action_ids=tuple(matched),
        unresolved_action_ids=tuple(unresolved),
        configured_not_emitted_action_ids=tuple(configured_only),
    )


__all__ = [
    "CONFIGURED_NOT_EMITTED",
    "EXECUTED_BURST_SINK",
    "EXECUTED_SUPPORT_SINK",
    "ContraBurstSourceRefV1",
    "ContraFuryBurstActionV1",
    "ContraFuryBurstGuideResolutionV1",
    "ContraFuryBurstInventoryV1",
    "ContraFuryBurstTriggerV1",
    "build_contra_fury_burst_inventory_v1",
    "resolve_contra_fury_burst_guide_v1",
]
