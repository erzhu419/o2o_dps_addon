"""Executable Turtle loadouts for Contra-derived burst search.

Contra provides the inventory names, not the experiment inventory or native
ActionIDs.  This module turns the supported named sinks into explicit request
variants.  Potion variants are separate because the simulator request admits
one default combat potion; the route planner later compares them under the
same ``combat_potion`` cooldown group.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .development_precombat_wave_case_v1 import (
    DEATH_WISH_ACTION,
    RECKLESSNESS_ACTION,
    DevelopmentPrecombatWaveCaseV1,
    wrap_development_wave_case_with_burst_precombat_v1,
)
from .sim_bridge import ActionRef
from .simulator_cooldown_binding_v1 import (
    exact_trinket_item_ids_from_request_v1,
)


JSONMap = dict[str, Any]
JUJU_FLURRY_ACTION = ActionRef(spell_id=16_322)
GOBLIN_SAPPER_ACTION = ActionRef(item_id=10_646)
MIGHTY_RAGE_ACTION = ActionRef(item_id=13_442)
RAGE_POTION_ACTION = ActionRef(item_id=5_631)
QUICKNESS_POTION_ACTION = ActionRef(item_id=61_181)
RAPID_GROWTH_ACTION = ActionRef(item_id=56_113)
CONTRA_PRECOMBAT_TRINKET_IDS = frozenset({22_954, 23_041})


@dataclass(frozen=True)
class ContraTurtleBurstLoadoutV1:
    loadout_id: str
    player_consumes: Mapping[str, Any]
    precombat_self_actions: tuple[ActionRef, ...]
    potion_resource_id: str | None
    modeled_source_action_ids: tuple[str, ...]
    unsupported_source_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id.strip():
            raise ValueError("loadout_id must be nonempty")
        if not isinstance(self.player_consumes, Mapping):
            raise TypeError("player_consumes must be a mapping")
        if not isinstance(self.precombat_self_actions, tuple) or not self.precombat_self_actions:
            raise TypeError("precombat_self_actions must be a nonempty tuple")
        if any(not isinstance(row, ActionRef) for row in self.precombat_self_actions):
            raise TypeError("precombat_self_actions must contain ActionRef values")
        if len(set(self.precombat_self_actions)) != len(self.precombat_self_actions):
            raise ValueError("precombat_self_actions must be unique")

    def to_dict(self) -> JSONMap:
        return {
            "schema": "contra_turtle_burst_loadout/v1",
            "loadout_id": self.loadout_id,
            "player_consumes": deepcopy(dict(self.player_consumes)),
            "precombat_self_actions": [
                action.to_wire() for action in self.precombat_self_actions
            ],
            "potion_resource_id": self.potion_resource_id,
            "modeled_source_action_ids": list(self.modeled_source_action_ids),
            "unsupported_source_action_ids": list(
                self.unsupported_source_action_ids
            ),
            "contract": {
                "contra_conditions_enforced": False,
                "loadout_only_changes_action_availability": True,
                "search_decides_action_order_timing_target_and_guard": True,
                "single_default_potion_per_request": True,
            },
        }


def _precombat_trinkets(request: Mapping[str, Any]) -> tuple[ActionRef, ...]:
    slots = exact_trinket_item_ids_from_request_v1(request)
    return tuple(
        ActionRef(item_id=item_id)
        for _, item_id in sorted(slots.items())
        if item_id in CONTRA_PRECOMBAT_TRINKET_IDS
    )


def build_contra_turtle_burst_loadouts_v1(
    request: Mapping[str, Any],
) -> tuple[ContraTurtleBurstLoadoutV1, ...]:
    """Return the supported no-potion and potion inventory variants.

    Juju Flurry and Goblin Sapper are registered in every variant.  Sapper can
    still be absent from the native action snapshot when the exact request has
    no Engineering profession; that absence is evidence, not silently patched.
    Rapid Growth is registered through the Turtle-specific consumes field and
    remains an independently schedulable native item action.
    """

    trinkets = _precombat_trinkets(request)
    common_precombat = (
        DEATH_WISH_ACTION,
        RECKLESSNESS_ACTION,
        JUJU_FLURRY_ACTION,
        RAPID_GROWTH_ACTION,
        *trinkets,
    )
    common_consumes: JSONMap = {
        "miscConsumes": {
            "jujuFlurry": True,
            "elixirOfRapidGrowth": True,
        },
        "sapperExplosive": "SapperGoblinSapper",
    }
    common_ids = (
        "warrior.death_wish",
        "warrior.recklessness",
        "item.juju_flurry",
        "item.goblin_sapper_charge",
        "item.elixir_of_rapid_growth",
        *(("trinket.equipped_on_use",) if trinkets else ()),
    )
    unsupported: tuple[str, ...] = ()
    variants = (
        ("no_potion", None, None),
        ("mighty_rage", "MightyRagePotion", MIGHTY_RAGE_ACTION),
        ("rage", "RagePotion", RAGE_POTION_ACTION),
        ("quickness", "QuicknessPotion", QUICKNESS_POTION_ACTION),
    )
    result = []
    for suffix, potion_enum, potion_action in variants:
        consumes = deepcopy(common_consumes)
        precombat = common_precombat
        potion_resource_id = None
        modeled = common_ids
        if potion_enum is not None and potion_action is not None:
            consumes["defaultPotion"] = potion_enum
            precombat = (*common_precombat, potion_action)
            potion_resource_id = {
                "MightyRagePotion": "item.mighty_rage_potion",
                "RagePotion": "item.rage_potion",
                "QuicknessPotion": "item.quickness_potion",
            }[potion_enum]
            modeled = (*common_ids, potion_resource_id)
        result.append(ContraTurtleBurstLoadoutV1(
            loadout_id="contra_turtle_burst__" + suffix,
            player_consumes=consumes,
            precombat_self_actions=precombat,
            potion_resource_id=potion_resource_id,
            modeled_source_action_ids=modeled,
            unsupported_source_action_ids=unsupported,
        ))
    return tuple(result)


def build_upper_kara_contra_burst_loadout_case_v1(
    seed: int,
    *,
    representative_rank: int,
    stratum: str,
    loadout_id: str,
    pull_time_ms: int = 3_000,
    **exact_case_options: Any,
) -> DevelopmentPrecombatWaveCaseV1:
    """Bind one named burst inventory variant to one exact Upper Kara cell."""

    from .upper_kara_exact_cell_case_v1 import (
        build_upper_kara_exact_cell_case_v1,
    )

    if "precombat_self_actions" in exact_case_options or "player_consumes" in exact_case_options:
        raise ValueError(
            "loadout owns precombat_self_actions and player_consumes"
        )
    base = build_upper_kara_exact_cell_case_v1(
        seed,
        representative_rank=representative_rank,
        stratum=stratum,
        **exact_case_options,
    )
    loadouts = {
        row.loadout_id: row
        for row in build_contra_turtle_burst_loadouts_v1(base.request)
    }
    try:
        loadout = loadouts[loadout_id]
    except KeyError as error:
        raise ValueError(f"unknown Contra Turtle burst loadout {loadout_id!r}") from error
    wrapped = wrap_development_wave_case_with_burst_precombat_v1(
        base,
        self_actions=loadout.precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=loadout.player_consumes,
    )
    wrapped.case_spec["burst_loadout"] = loadout.to_dict()
    return wrapped


__all__ = (
    "CONTRA_PRECOMBAT_TRINKET_IDS",
    "ContraTurtleBurstLoadoutV1",
    "GOBLIN_SAPPER_ACTION",
    "JUJU_FLURRY_ACTION",
    "MIGHTY_RAGE_ACTION",
    "QUICKNESS_POTION_ACTION",
    "RAPID_GROWTH_ACTION",
    "RAGE_POTION_ACTION",
    "build_contra_turtle_burst_loadouts_v1",
    "build_upper_kara_contra_burst_loadout_case_v1",
)
