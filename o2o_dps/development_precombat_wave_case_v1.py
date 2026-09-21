"""Concrete pre-pull wrapper for the model-defined development wave.

The wrapped case keeps the existing wave hypotheses intact, shifts their pull
origin onto a non-negative simulator clock, and re-binds the request/config
digest after adding the explicitly modelled consumable inventory.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    build_development_wave_case_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3
from .precombat_contract_v1 import PrecombatActionsConfigV1
from .precombat_timeline_v1 import (
    PullRelativeTimelineV1,
    shift_dynamic_config_for_precombat_v1,
    shift_raid_request_for_precombat_v1,
)
from .sim_bridge import ActionRef


SCHEMA = "development_precombat_wave_case/v1"
MIGHTY_RAGE_POTION_ITEM_ID = 13_442
MIGHTY_RAGE_POTION_ENUM = "MightyRagePotion"
MIGHTY_RAGE_POTION_ACTION = ActionRef(item_id=MIGHTY_RAGE_POTION_ITEM_ID)
DEATH_WISH_ACTION = ActionRef(spell_id=12_328)
RECKLESSNESS_ACTION = ActionRef(spell_id=1_719)
DEVELOPMENT_BURST_SELF_ACTIONS_V1 = (
    MIGHTY_RAGE_POTION_ACTION,
    DEATH_WISH_ACTION,
    RECKLESSNESS_ACTION,
)


@dataclass(frozen=True)
class DevelopmentPrecombatWaveCaseV1:
    """Search/replay case whose simulator t=0 is the precombat-window start."""

    case_spec: dict[str, Any]
    request: dict[str, Any]
    dynamic_load: DynamicRolloutLoadV3
    target_contexts: dict[int, TargetSemanticsContextV3]
    precombat: PrecombatActionsConfigV1
    timeline: PullRelativeTimelineV1


def _player(request: dict[str, Any]) -> dict[str, Any]:
    try:
        player = request["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(
            "request must contain raid.parties[0].players[0]"
        ) from error
    if not isinstance(player, dict):
        raise ValueError("request raid.parties[0].players[0] must be an object")
    return player


def wrap_development_wave_case_with_precombat_v1(
    case: DevelopmentWaveCaseV1,
    *,
    timeline: PullRelativeTimelineV1,
    precombat: PrecombatActionsConfigV1,
    request: Mapping[str, Any] | None = None,
) -> DevelopmentPrecombatWaveCaseV1:
    """Shift and content-rebind one complete development case.

    ``request`` may add the inventory needed by the declared precombat actions.
    The caller cannot accidentally retain the original request/config binding:
    a fresh :class:`DynamicRolloutLoadV3` is always produced.
    """

    if not isinstance(case, DevelopmentWaveCaseV1):
        raise TypeError("case must be DevelopmentWaveCaseV1")
    if not isinstance(timeline, PullRelativeTimelineV1):
        raise TypeError("timeline must be PullRelativeTimelineV1")
    if not isinstance(precombat, PrecombatActionsConfigV1):
        raise TypeError("precombat must be PrecombatActionsConfigV1")
    if precombat.pull_time_ms != timeline.pull_time_ms:
        raise ValueError("precombat pull_time_ms must match timeline")
    if request is not None and not isinstance(request, Mapping):
        raise TypeError("request must be a mapping or None")

    source_request = case.request if request is None else request
    shifted_request = shift_raid_request_for_precombat_v1(
        source_request, timeline
    )
    shifted_config = shift_dynamic_config_for_precombat_v1(
        case.dynamic_load.config, timeline
    )
    shifted_load = DynamicRolloutLoadV3.bind(
        shifted_request, case.dynamic_load.seed, shifted_config
    )

    player = _player(shifted_request)
    consumes = player.get("consumes")
    if consumes is None:
        consumes = {}
    if not isinstance(consumes, Mapping):
        raise ValueError("request player consumes must be an object")

    spec = deepcopy(case.case_spec)
    spec["schema"] = SCHEMA
    spec["parent_case_schema"] = case.case_spec.get("schema")
    spec["request_sha256"] = shifted_load.request_sha256
    spec["dynamic_load_contract_sha256"] = shifted_load.contract_sha256
    spec["precombat"] = {
        "pull_time_ms": timeline.pull_time_ms,
        "simulator_time_origin": "PRECOMBAT_WINDOW_START",
        "simulator_time_zero_relative_to_pull_ms": -timeline.pull_time_ms,
        "pull_at_simulator_time_ms": timeline.pull_time_ms,
        "self_actions": [action.to_wire() for action in precombat.self_actions],
    }
    spec["consumable_inventory"] = deepcopy(dict(consumes))
    initial_state = spec.get("initial_state")
    if isinstance(initial_state, dict):
        attackable_at = initial_state.get("target_attackable_at_ms", 0)
        if isinstance(attackable_at, list):
            initial_state["target_attackable_at_ms"] = [
                timeline.to_simulator_time_ms(value) for value in attackable_at
            ]
        else:
            initial_state["target_attackable_at_ms"] = (
                timeline.to_simulator_time_ms(attackable_at)
            )
        initial_state["consumes"] = deepcopy(dict(consumes))

    return DevelopmentPrecombatWaveCaseV1(
        case_spec=spec,
        request=shifted_request,
        dynamic_load=shifted_load,
        target_contexts=deepcopy(case.target_contexts),
        precombat=precombat,
        timeline=timeline,
    )


def build_development_burst_precombat_case_v1(
    seed: int,
    *,
    self_actions: tuple[ActionRef, ...] = DEVELOPMENT_BURST_SELF_ACTIONS_V1,
    pull_time_ms: int = 3_000,
    player_consumes: Mapping[str, Any] | None = None,
) -> DevelopmentPrecombatWaveCaseV1:
    """Build one explicit pre-pull burst-action development case.

    The action tuple is an exact allowlist, not a category wildcard.  Consumable
    proto fields remain caller-owned except that admitting item 13442 pins the
    corresponding Mighty Rage inventory entry when it is not already present.
    """

    base = build_development_wave_case_v1(seed)
    return wrap_development_wave_case_with_burst_precombat_v1(
        base,
        self_actions=self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=player_consumes,
    )


def wrap_development_wave_case_with_burst_precombat_v1(
    case: DevelopmentWaveCaseV1,
    *,
    self_actions: tuple[ActionRef, ...] = DEVELOPMENT_BURST_SELF_ACTIONS_V1,
    pull_time_ms: int = 3_000,
    player_consumes: Mapping[str, Any] | None = None,
) -> DevelopmentPrecombatWaveCaseV1:
    """Add the declared burst window to any already-bound wave/build case."""

    if not isinstance(case, DevelopmentWaveCaseV1):
        raise TypeError("case must be DevelopmentWaveCaseV1")
    request = deepcopy(case.request)
    player = _player(request)
    raw_consumes = player.get("consumes")
    if raw_consumes is None:
        consumes: dict[str, Any] = {}
    elif isinstance(raw_consumes, Mapping):
        consumes = deepcopy(dict(raw_consumes))
    else:
        raise ValueError("request player consumes must be an object")
    if player_consumes is not None:
        if not isinstance(player_consumes, Mapping):
            raise TypeError("player_consumes must be a mapping or None")
        consumes.update(deepcopy(dict(player_consumes)))
    if MIGHTY_RAGE_POTION_ACTION in self_actions:
        configured = consumes.get("defaultPotion")
        if configured not in (None, MIGHTY_RAGE_POTION_ENUM):
            raise ValueError(
                "item 13442 conflicts with the selected default potion"
            )
        consumes["defaultPotion"] = MIGHTY_RAGE_POTION_ENUM
    if consumes:
        player["consumes"] = consumes

    timeline = PullRelativeTimelineV1(pull_time_ms=pull_time_ms)
    precombat = PrecombatActionsConfigV1(
        pull_time_ms=pull_time_ms,
        self_actions=self_actions,
    )
    return wrap_development_wave_case_with_precombat_v1(
        case,
        timeline=timeline,
        precombat=precombat,
        request=request,
    )


def build_development_mighty_rage_precombat_case_v1(
    seed: int,
    *,
    pull_time_ms: int = 3_000,
) -> DevelopmentPrecombatWaveCaseV1:
    """Convenience case retaining the original item-13442-only behavior."""

    return build_development_burst_precombat_case_v1(
        seed,
        self_actions=(MIGHTY_RAGE_POTION_ACTION,),
        pull_time_ms=pull_time_ms,
    )


__all__ = (
    "DevelopmentPrecombatWaveCaseV1",
    "DEATH_WISH_ACTION",
    "DEVELOPMENT_BURST_SELF_ACTIONS_V1",
    "MIGHTY_RAGE_POTION_ENUM",
    "MIGHTY_RAGE_POTION_ACTION",
    "MIGHTY_RAGE_POTION_ITEM_ID",
    "RECKLESSNESS_ACTION",
    "build_development_burst_precombat_case_v1",
    "build_development_mighty_rage_precombat_case_v1",
    "wrap_development_wave_case_with_burst_precombat_v1",
    "wrap_development_wave_case_with_precombat_v1",
)
