"""Resolve route cooldown clocks from one exact simulator build snapshot.

Contra supplies useful action identities and ordering hints, but not an exact
cooldown contract.  The native simulator snapshot is build-conditioned: item
sets and other mechanics have already modified each spell's cooldown duration.
Only actions exported with a positive duration become route-planner resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contra_fury_burst_inventory_v1 import (
    CONFIGURED_NOT_EMITTED,
    ContraFuryBurstInventoryV1,
    build_contra_fury_burst_inventory_v1,
)
from .raid_cooldown_schedule_v1 import (
    RoutePersistentEffectV1,
    RoutePersistentStatPhaseV1,
)
from .sim_bridge import ActionRef, AvailableAction
from .wave_cooldown_package_measurement_v1 import CooldownActionBindingV1


JSONMap = dict[str, Any]

# Source identities are resolved against the checked-in Turtle runtime.  Juju
# Flurry is a SpellID because sim/core/consumes.go registers that ActionID;
# the other named Contra sinks below are native item ActionIDs.
TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1: Mapping[str, ActionRef] = {
    "item.mighty_rage_potion": ActionRef(item_id=13_442),
    "item.juju_flurry": ActionRef(spell_id=16_322),
    "item.goblin_sapper_charge": ActionRef(item_id=10_646),
    "item.quickness_potion": ActionRef(item_id=61_181),
    "item.rage_potion": ActionRef(item_id=5_631),
    "item.elixir_of_rapid_growth": ActionRef(item_id=56_113),
}
RAPID_GROWTH_ROUTE_EFFECT_V1 = RoutePersistentEffectV1(
    effect_id="turtle.elixir_of_rapid_growth.46102_to_46103",
    phases=(
        RoutePersistentStatPhaseV1(
            phase_id="rapid_growth",
            starts_after_use_ms=0,
            duration_ms=120_000,
            stat_deltas=(("strength", 30.0),),
        ),
        RoutePersistentStatPhaseV1(
            phase_id="rapid_deterioration",
            starts_after_use_ms=120_000,
            duration_ms=120_000,
            stat_deltas=(("stamina", -25.0), ("strength", -25.0)),
        ),
    ),
)


@dataclass(frozen=True)
class SimulatorCooldownResourceSpecV1:
    action: ActionRef
    resource_id: str
    cooldown_group: str
    action_kind: str
    identity_source: str
    persistent_effect: RoutePersistentEffectV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionRef):
            raise TypeError("action must be ActionRef")
        for label, value in (
            ("resource_id", self.resource_id),
            ("cooldown_group", self.cooldown_group),
            ("action_kind", self.action_kind),
            ("identity_source", self.identity_source),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty")
        if self.persistent_effect is not None and not isinstance(
            self.persistent_effect, RoutePersistentEffectV1
        ):
            raise TypeError(
                "persistent_effect must be RoutePersistentEffectV1 or None"
            )


@dataclass(frozen=True)
class SimulatorCooldownBindingResolutionV1:
    bindings: tuple[CooldownActionBindingV1, ...]
    unavailable_resource_ids: tuple[str, ...]
    zero_duration_resource_ids: tuple[str, ...]
    snapshot_action_count: int

    def to_dict(self) -> JSONMap:
        return {
            "schema": "simulator_cooldown_binding/v1",
            "authority": "EXACT_BUILD_NATIVE_AVAILABLE_ACTION_SNAPSHOT",
            "bindings": [
                {
                    "action": row.action.to_wire(),
                    "resource_id": row.resource_id,
                    "cooldown_group": row.cooldown_group,
                    "cooldown_ms": row.cooldown_ms,
                    "action_kind": row.action_kind,
                    "persistent_effect": (
                        row.persistent_effect.to_dict()
                        if row.persistent_effect is not None
                        else None
                    ),
                }
                for row in self.bindings
            ],
            "unavailable_resource_ids": list(self.unavailable_resource_ids),
            "zero_duration_resource_ids": list(self.zero_duration_resource_ids),
            "snapshot_action_count": self.snapshot_action_count,
            "contract": {
                "contra_cooldown_hint_used_as_duration": False,
                "build_modified_cooldown_preserved": True,
                "unavailable_action_invented": False,
            },
        }


@dataclass(frozen=True)
class ContraSimulatorResourceSpecProjectionV1:
    """Concrete identities resolved from Contra plus exact runtime inputs."""

    resource_specs: tuple[SimulatorCooldownResourceSpecV1, ...]
    unresolved_action_ids: tuple[str, ...]
    configured_not_emitted_action_ids: tuple[str, ...]

    def to_dict(self) -> JSONMap:
        return {
            "schema": "contra_simulator_resource_spec_projection/v1",
            "resource_specs": [
                {
                    "action": row.action.to_wire(),
                    "resource_id": row.resource_id,
                    "cooldown_group": row.cooldown_group,
                    "action_kind": row.action_kind,
                    "identity_source": row.identity_source,
                    "persistent_effect": (
                        row.persistent_effect.to_dict()
                        if row.persistent_effect is not None
                        else None
                    ),
                }
                for row in self.resource_specs
            ],
            "unresolved_action_ids": list(self.unresolved_action_ids),
            "configured_not_emitted_action_ids": list(
                self.configured_not_emitted_action_ids
            ),
            "contract": {
                "contra_condition_enforced": False,
                "configured_but_not_emitted_action_may_still_be_searched": True,
                "native_snapshot_decides_availability_and_duration": True,
            },
        }


def default_fury_burst_resource_specs_v1(
) -> tuple[SimulatorCooldownResourceSpecV1, ...]:
    """Current executable precombat burst identities, not a final item list."""

    source = "CONTRA_INVENTORY_IDENTITY_PLUS_NATIVE_PRECOMBAT_CLOSURE"
    return (
        SimulatorCooldownResourceSpecV1(
            ActionRef(item_id=13_442),
            "item.mighty_rage_potion",
            "combat_potion",
            "ITEM",
            source,
        ),
        SimulatorCooldownResourceSpecV1(
            ActionRef(spell_id=12_328),
            "warrior.death_wish",
            "warrior.death_wish",
            "SPELL",
            source,
        ),
        SimulatorCooldownResourceSpecV1(
            ActionRef(spell_id=1_719),
            "warrior.recklessness",
            "warrior.recklessness",
            "SPELL",
            source,
        ),
    )


def project_contra_fury_resource_specs_v1(
    *,
    named_item_ids: Mapping[str, int] | None = None,
    named_action_refs: Mapping[str, ActionRef] | None = None,
    trinket_item_ids: Mapping[int, int] | None = None,
    inventory: ContraFuryBurstInventoryV1 | None = None,
    include_mighty_rage_extension: bool = True,
) -> ContraSimulatorResourceSpecProjectionV1:
    """Resolve Contra's whole burst inventory into simulator action identities.

    Contra's manual aura and Boss-HP conditions are intentionally absent.  A
    named item or trinket slot stays unresolved until the exact request/runtime
    identifies its item.  Sweeping Strikes is retained even though Contra's
    burst body does not emit it: the native action snapshot, not Contra, decides
    whether it can enter the search space.
    """

    resolved_inventory = inventory or build_contra_fury_burst_inventory_v1()
    named = dict(named_item_ids or {})
    named_refs = dict(named_action_refs or {})
    trinkets = dict(trinket_item_ids or {})
    if any(not isinstance(key, str) or not key.strip() for key in named):
        raise ValueError("named_item_ids keys must be nonempty action IDs")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in named.values()
    ):
        raise ValueError("named_item_ids values must be positive item IDs")
    if any(not isinstance(key, str) or not key.strip() for key in named_refs):
        raise ValueError("named_action_refs keys must be nonempty action IDs")
    if any(not isinstance(value, ActionRef) for value in named_refs.values()):
        raise TypeError("named_action_refs values must be ActionRef values")
    overlap = sorted(set(named) & set(named_refs))
    if overlap:
        raise ValueError(
            "named_item_ids and named_action_refs overlap: " + ", ".join(overlap)
        )
    if any(key not in {13, 14} for key in trinkets) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in trinkets.values()
    ):
        raise ValueError("trinket_item_ids must map slot 13/14 to positive item IDs")
    if not isinstance(include_mighty_rage_extension, bool):
        raise TypeError("include_mighty_rage_extension must be boolean")

    specs: list[SimulatorCooldownResourceSpecV1] = []
    unresolved: list[str] = []
    configured_not_emitted: list[str] = []
    for row in resolved_inventory.actions:
        if row.execution_status == CONFIGURED_NOT_EMITTED:
            configured_not_emitted.append(row.action_id)
        if row.spell_id is not None:
            action = ActionRef(spell_id=row.spell_id)
        elif row.item_id is not None:
            action = ActionRef(item_id=row.item_id)
        elif row.locator_kind == "NAMED_ITEM" and row.action_id in named_refs:
            action = named_refs[row.action_id]
        elif row.locator_kind == "NAMED_ITEM" and row.action_id in named:
            action = ActionRef(item_id=named[row.action_id])
        elif (
            row.locator_kind == "EQUIPPED_SLOT"
            and row.inventory_slot in trinkets
        ):
            action = ActionRef(item_id=trinkets[row.inventory_slot])
        else:
            unresolved.append(row.action_id)
            continue
        specs.append(SimulatorCooldownResourceSpecV1(
            action=action,
            resource_id=row.action_id,
            cooldown_group=row.cooldown_group_hint,
            action_kind=("ITEM" if action.item_id > 0 else "SPELL"),
            identity_source=(
                "CONTRA_BURST_INVENTORY_IDENTITY_ONLY:" + row.action_id
            ),
            persistent_effect=(
                RAPID_GROWTH_ROUTE_EFFECT_V1
                if row.action_id == "item.elixir_of_rapid_growth"
                else None
            ),
        ))

    if (
        include_mighty_rage_extension
        and ActionRef(item_id=13_442) not in {row.action for row in specs}
    ):
        specs.append(SimulatorCooldownResourceSpecV1(
            ActionRef(item_id=13_442),
            "item.mighty_rage_potion",
            "combat_potion",
            "ITEM",
            "TURTLE_NATIVE_ID_FOR_CONTRA_NAMED_SINK",
        ))
        unresolved = [
            value for value in unresolved
            if value != "item.mighty_rage_potion"
        ]
    by_action: dict[ActionRef, SimulatorCooldownResourceSpecV1] = {}
    for row in specs:
        if row.action in by_action:
            raise ValueError(
                "two Contra/runtime resources resolve to the same ActionRef: "
                f"{by_action[row.action].resource_id}, {row.resource_id}"
            )
        by_action[row.action] = row
    return ContraSimulatorResourceSpecProjectionV1(
        resource_specs=tuple(specs),
        unresolved_action_ids=tuple(unresolved),
        configured_not_emitted_action_ids=tuple(configured_not_emitted),
    )


def exact_trinket_item_ids_from_request_v1(
    request: Mapping[str, Any],
) -> Mapping[int, int]:
    """Resolve WoW inventory slots 13/14 from one exact simulator request."""

    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    try:
        items = request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("request lacks player equipment items") from error
    if not isinstance(items, list) or len(items) < 14:
        raise ValueError("request equipment must contain both trinket slots")
    result: dict[int, int] = {}
    for wow_slot, item_index in ((13, 12), (14, 13)):
        row = items[item_index]
        if row is None:
            continue
        if not isinstance(row, Mapping):
            raise ValueError(f"equipment item {item_index} must be an object")
        item_id = row.get("id")
        if item_id in (None, 0):
            continue
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            raise ValueError(f"equipment item {item_index} has invalid id")
        result[wow_slot] = item_id
    return result


def project_contra_turtle_resource_specs_for_request_v1(
    request: Mapping[str, Any],
    *,
    inventory: ContraFuryBurstInventoryV1 | None = None,
) -> ContraSimulatorResourceSpecProjectionV1:
    """Project the full Contra list through one exact Turtle build request."""

    return project_contra_fury_resource_specs_v1(
        named_action_refs=TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1,
        trinket_item_ids=exact_trinket_item_ids_from_request_v1(request),
        inventory=inventory,
        include_mighty_rage_extension=False,
    )


def resolve_simulator_cooldown_bindings_v1(
    available_actions: Sequence[AvailableAction],
    *,
    resource_specs: Sequence[SimulatorCooldownResourceSpecV1],
) -> SimulatorCooldownBindingResolutionV1:
    """Bind resource identities to build-conditioned native durations."""

    actions = tuple(available_actions)
    specs = tuple(resource_specs)
    if any(not isinstance(row, AvailableAction) for row in actions):
        raise TypeError("available_actions must contain AvailableAction values")
    if any(not isinstance(row, SimulatorCooldownResourceSpecV1) for row in specs):
        raise TypeError("resource_specs must contain SimulatorCooldownResourceSpecV1")
    resource_ids = [row.resource_id for row in specs]
    action_refs = [row.action for row in specs]
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("resource_specs must have unique resource IDs")
    if len(action_refs) != len(set(action_refs)):
        raise ValueError("resource_specs must have unique ActionRef values")

    snapshot: dict[ActionRef, AvailableAction] = {}
    for row in actions:
        prior = snapshot.get(row.action)
        if prior is not None and (
            prior.cooldown_duration_ms != row.cooldown_duration_ms
            or prior.triggers_gcd != row.triggers_gcd
        ):
            raise ValueError("one action has conflicting native cooldown metadata")
        snapshot.setdefault(row.action, row)

    bindings: list[CooldownActionBindingV1] = []
    unavailable: list[str] = []
    zero_duration: list[str] = []
    for spec in specs:
        action = snapshot.get(spec.action)
        if action is None:
            unavailable.append(spec.resource_id)
            continue
        if action.cooldown_duration_ms <= 0:
            zero_duration.append(spec.resource_id)
            continue
        bindings.append(CooldownActionBindingV1(
            action=spec.action,
            resource_id=spec.resource_id,
            cooldown_group=spec.cooldown_group,
            cooldown_ms=action.cooldown_duration_ms,
            action_kind=spec.action_kind,
            persistent_effect=spec.persistent_effect,
        ))
    return SimulatorCooldownBindingResolutionV1(
        bindings=tuple(bindings),
        unavailable_resource_ids=tuple(unavailable),
        zero_duration_resource_ids=tuple(zero_duration),
        snapshot_action_count=len(actions),
    )


__all__ = (
    "ContraSimulatorResourceSpecProjectionV1",
    "SimulatorCooldownBindingResolutionV1",
    "SimulatorCooldownResourceSpecV1",
    "RAPID_GROWTH_ROUTE_EFFECT_V1",
    "TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1",
    "default_fury_burst_resource_specs_v1",
    "exact_trinket_item_ids_from_request_v1",
    "project_contra_fury_resource_specs_v1",
    "project_contra_turtle_resource_specs_for_request_v1",
    "resolve_simulator_cooldown_bindings_v1",
)
