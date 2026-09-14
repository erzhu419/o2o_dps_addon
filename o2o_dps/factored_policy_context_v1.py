"""Static, observable mechanism context for development policy routing.

This projection contains build mechanics and declared wave-model assumptions,
not a historical actor/build ID, simulator seed, scheduled team hits, or the
source raid's eventual death times. Current combat state (rage, active target,
cooldowns, nearby *attackable* enemies) must still come from the rollout.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping

from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .wowsims_profile import WARRIOR_TALENT_FIELDS


@dataclass(frozen=True)
class BuildMechanismFeaturesV1:
    weapon_mode: str
    main_hand_speed_s: float | None
    off_hand_speed_s: float | None
    bloodthirst_known: bool | None


@dataclass(frozen=True)
class WaveMechanismFeaturesV1:
    # Encounter targets can belong to consecutive waves; this is not the
    # current nearby/attackable enemy count.
    route_target_count: int
    wave_topology: str
    target_base_armor_by_index: tuple[int | None, ...]
    target_max_hp_by_index: tuple[int | None, ...]
    team_dps_assumed: float | None
    team_dps_prior_by_target: tuple[float | None, ...]
    team_dps_prior_band_by_target: tuple[str, ...]
    background_team_ttk_s_prior_by_target: tuple[float | None, ...]
    background_team_ttk_band_by_target: tuple[str, ...]
    team_prior_source: str


@dataclass(frozen=True)
class FactoredPolicyContextV1:
    build: BuildMechanismFeaturesV1
    wave: WaveMechanismFeaturesV1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _item_definitions(database: Mapping[str, Any] | None) -> dict[int, Mapping[str, Any]]:
    if database is None:
        return {}
    rows = database.get("items")
    if not isinstance(rows, list):
        raise ValueError("item_database.items must be a list")
    return {
        row["id"]: row for row in rows
        if isinstance(row, Mapping) and type(row.get("id")) is int
    }


def _item_id(raw: Any) -> int | None:
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("id")
    return value if type(value) is int and value > 0 else None


def _weapon_speed(item: Mapping[str, Any] | None) -> float | None:
    if item is None:
        return None
    value = item.get("weaponSpeed")
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        return None
    return float(value)


def _weapon_mode(
    main: Mapping[str, Any] | None, off: Mapping[str, Any] | None,
    off_equipped: bool,
) -> str:
    if main is None or main.get("type") != 13:
        return "UNKNOWN"
    if off_equipped:
        if off is None:
            return "UNKNOWN"
        return "DUAL_WIELD" if off.get("type") == 13 and off.get("weaponSpeed") else "ONE_HAND_WITH_OFFHAND"
    return "TWO_HAND" if main.get("handType") == 4 else "ONE_HAND"


def _bloodthirst_from_simulator_talents(
    talents_string: Any, talent_position_map: Mapping[str, Any] | None,
) -> bool | None:
    if talent_position_map is not None:
        # The client map establishes the *semantic* Bloodthirst binding. The
        # simulator string uses its own field order, not Turtle tab positions.
        if (
            talent_position_map.get("admission") is not True
            or talent_position_map.get("admission_status")
            != "ADMITTED_EXACT_CLIENT_BUILD_POSITION_MAP"
        ):
            raise ValueError("talent_position_map must be admitted")
        trees = talent_position_map.get("position_map", {}).get("tree_fields", [])
        if not any(
            field.get("talent_id") == "warrior.bloodthirst"
            and field.get("simulator_target") == "warrior.talents.bloodthirst"
            for tree in trees if isinstance(tree, list)
            for field in tree if isinstance(field, Mapping)
        ):
            raise ValueError("talent_position_map lacks Bloodthirst semantic binding")
    if not isinstance(talents_string, str) or not talents_string:
        return None
    trees = talents_string.split("-")
    if len(trees) > 3 or any(not tree.isdigit() for tree in trees if tree):
        return None
    fury = trees[1] if len(trees) > 1 else ""
    index = WARRIOR_TALENT_FIELDS[1].index("bloodthirst")
    return int(fury[index]) > 0 if index < len(fury) else False


def _by_target(raw: Any, count: int) -> tuple[int | None, ...]:
    if type(raw) is int and raw >= 0:
        return (raw,) * count
    if isinstance(raw, list) and len(raw) == count:
        return tuple(value if type(value) is int and value >= 0 else None for value in raw)
    return (None,) * count


def _finite_nonnegative(value: Any) -> float | None:
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        return None
    return float(value)


def _dps_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 8_000:
        return "under_8k"
    if value < 16_000:
        return "8k_to_16k"
    return "16k_plus"


def _ttk_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 8.0:
        return "under_8s"
    if value < 15.0:
        return "8s_to_15s"
    return "15s_plus"


def _team_prior(
    team: Mapping[str, Any], hp: tuple[int | None, ...], count: int,
) -> tuple[
    float | None, tuple[float | None, ...], tuple[float | None, ...], str,
]:
    """Return a frozen declared/offline prior, never a realized future schedule."""

    declared = _finite_nonnegative(team.get("team_dps_assumed"))
    if declared is not None:
        rates = (declared,) * count
        source = "DECLARED_STATIC_TEAM_DPS_PRIOR"
    else:
        rows = team.get("per_target")
        admitted = (
            team.get("model")
            == "EXOGENOUS_PER_TARGET_DIRECT_GUID_LEAVE_ONE_OUT_RATE_EXTRAPOLATED"
            and team.get("future_schedule_policy_visible") is False
            and isinstance(rows, list)
            and len(rows) == count
            and all(
                isinstance(row, Mapping)
                and row.get("focal_player_direct_guid_excluded_from_source_budget") is True
                for row in rows
            )
        )
        derived: list[float | None] = []
        if admitted:
            for row in rows:
                damage = _finite_nonnegative(row.get("damage_per_hit"))
                interval = _finite_nonnegative(row.get("damage_interval_ms"))
                derived.append(
                    damage * 1000.0 / interval
                    if damage is not None and interval is not None and interval > 0
                    else None
                )
        if not admitted or any(value is None for value in derived):
            rates = (None,) * count
            source = "UNAVAILABLE"
        else:
            rates = tuple(derived)
            source = "OFFLINE_REGISTRY_LEAVE_ONE_OUT_RATE_PRIOR"
    ttk = tuple(
        float(target_hp) / rate
        if target_hp is not None and rate is not None and rate > 0 else None
        for target_hp, rate in zip(hp, rates)
    )
    return declared, rates, ttk, source


def extract_factored_policy_context_v1(
    case: DevelopmentWaveCaseV1,
    *,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> FactoredPolicyContextV1:
    """Extract only pre-decision, mechanism-level fields from a prepared case.

    Pass the simulator item database to resolve weapon speed/type; absent
    definitions remain unknown rather than silently assigning a build ID bin.
    """
    player = case.request["raid"]["parties"][0]["players"][0]
    items = player["equipment"]["items"]
    if len(items) < 16:
        raise ValueError("request has no complete main/off-hand equipment slots")
    definitions = _item_definitions(item_database)
    main_id, off_id = _item_id(items[14]), _item_id(items[15])
    main = definitions.get(main_id) if main_id is not None else None
    off = definitions.get(off_id) if off_id is not None else None
    target_count = len(case.request["encounter"]["targets"])
    if target_count < 1:
        raise ValueError("request encounter has no targets")
    initial = case.case_spec.get("initial_state", {})
    team = case.case_spec.get("team_background", {})
    two_wave = case.case_spec.get("two_wave_model")
    topology = (
        "SEQUENTIAL_WAVES" if isinstance(two_wave, Mapping)
        and two_wave.get("model_wave_count", 1) > 1 else "SINGLE_WAVE"
    )
    target_hp = _by_target(initial.get("target_max_hp"), target_count)
    declared_rate, team_rates, team_ttk, team_prior_source = _team_prior(
        team, target_hp, target_count,
    )
    return FactoredPolicyContextV1(
        build=BuildMechanismFeaturesV1(
            weapon_mode=_weapon_mode(main, off, off_id is not None),
            main_hand_speed_s=_weapon_speed(main),
            off_hand_speed_s=_weapon_speed(off),
            bloodthirst_known=_bloodthirst_from_simulator_talents(
                player.get("talentsString"), talent_position_map,
            ),
        ),
        wave=WaveMechanismFeaturesV1(
            route_target_count=target_count,
            wave_topology=topology,
            target_base_armor_by_index=_by_target(initial.get("target_base_armor"), target_count),
            target_max_hp_by_index=target_hp,
            team_dps_assumed=declared_rate,
            team_dps_prior_by_target=team_rates,
            team_dps_prior_band_by_target=tuple(_dps_band(value) for value in team_rates),
            background_team_ttk_s_prior_by_target=team_ttk,
            background_team_ttk_band_by_target=tuple(_ttk_band(value) for value in team_ttk),
            team_prior_source=team_prior_source,
        ),
    )


__all__ = (
    "BuildMechanismFeaturesV1", "WaveMechanismFeaturesV1",
    "FactoredPolicyContextV1", "extract_factored_policy_context_v1",
)
