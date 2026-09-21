"""Bind compact Chronicle target evidence to one development encounter model.

This is the small-data bridge between a metadata registry encounter and the
streaming exact-trace reducer.  It produces one target model record per exact
registry occurrence, plus a focal-player leave-one-out team schedule.  The
observed kill damage balance is retained as an HP *hypothesis*, not promoted to
an API-observed maximum-health value.  Armor remains an explicit sensitivity
grid because Chronicle damage outcomes do not reveal an exact armor state.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .chronicle_external_compact_target_reducer_v1 import SCHEMA as REDUCTION_SCHEMA
from .upper_kara_target_universe_resolution_v1 import (
    BOSS_OWNED_SUMMON_OCCURRENCE,
    EXACT_F130_CREATURE_OCCURRENCE,
    EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    REGISTRY_CREATURE_OCCURRENCE,
    SCHEMA as RESOLUTION_SCHEMA,
    STATUS as RESOLUTION_STATUS,
)
from .upper_kara_wave_local_search_contract_v1 import WaveTargetBindingV1


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_compact_encounter_development_model/v1"
STATUS = "DEVELOPMENT_HYPOTHESES_NOT_COMPARISON_AUTHORIZED"


class UpperKaraCompactEncounterModelV1Error(ValueError):
    """Compact encounter evidence cannot support the requested binding."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraCompactEncounterModelV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraCompactEncounterModelV1Error(f"{label} must be an object")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpperKaraCompactEncounterModelV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise UpperKaraCompactEncounterModelV1Error(
            f"{label} must be a positive integer"
        )
    return result


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _armor_grid(values: Sequence[int]) -> tuple[int, ...]:
    rows = tuple(_nonnegative_int(value, "armor hypothesis") for value in values)
    if not rows or len(rows) != len(set(rows)):
        raise UpperKaraCompactEncounterModelV1Error(
            "armor hypotheses must be nonempty and unique"
        )
    return tuple(sorted(rows))


def _target_ref(model_id: str, occurrence_id: str, component: str) -> str:
    return f"{model_id}/target/{occurrence_id}/{component}"


def build_compact_encounter_development_model_v1(
    encounter: Mapping[str, Any],
    reduction: Mapping[str, Any],
    resolution: Mapping[str, Any],
    *,
    model_id: str,
    focal_player_guid: str,
    armor_hypotheses: Sequence[int],
) -> JSONMap:
    """Join exact target GUIDs and prepare a focal leave-one-out model bundle."""

    raw_encounter = _mapping(encounter, "encounter")
    raw_reduction = _mapping(reduction, "reduction")
    raw_resolution = _mapping(resolution, "resolution")
    identity = _text(model_id, "model_id")
    focal = _text(focal_player_guid, "focal_player_guid")
    armors = _armor_grid(armor_hypotheses)
    if raw_reduction.get("schema") != REDUCTION_SCHEMA:
        raise UpperKaraCompactEncounterModelV1Error(
            "unexpected compact target reduction schema"
        )
    if raw_reduction.get("status") != "DESCRIPTIVE_OUTCOME_ONLY":
        raise UpperKaraCompactEncounterModelV1Error(
            "compact target reduction is not descriptive outcome evidence"
        )
    if (
        raw_resolution.get("schema") != RESOLUTION_SCHEMA
        or raw_resolution.get("status") != RESOLUTION_STATUS
    ):
        raise UpperKaraCompactEncounterModelV1Error(
            "unexpected target-universe resolution schema or status"
        )
    resolution_gate = _mapping(raw_resolution.get("gate"), "resolution.gate")
    if resolution_gate.get("development_case_assembly_authorized") is not True:
        raise UpperKaraCompactEncounterModelV1Error(
            "target-universe resolution is not authorized for development assembly"
        )
    source = _mapping(raw_reduction.get("source_selection"), "source_selection")
    for field in ("instance_id", "encounter_id"):
        if source.get(field) != raw_encounter.get(field):
            raise UpperKaraCompactEncounterModelV1Error(
                f"reduction {field} differs from the registry encounter"
            )
    resolution_source = _mapping(raw_resolution.get("source"), "resolution.source")
    for field in ("instance_id", "encounter_id", "pull_ref"):
        if resolution_source.get(field) != raw_encounter.get(field):
            raise UpperKaraCompactEncounterModelV1Error(
                f"resolution {field} differs from the registry encounter"
            )

    compact_rows = raw_reduction.get("hostile_targets")
    if not isinstance(compact_rows, list):
        raise UpperKaraCompactEncounterModelV1Error(
            "reduction.hostile_targets must be a list"
        )
    by_guid: dict[str, Mapping[str, Any]] = {}
    for value in compact_rows:
        row = _mapping(value, "hostile target")
        guid = _text(row.get("target_guid"), "hostile target GUID")
        if guid in by_guid:
            raise UpperKaraCompactEncounterModelV1Error(
                "compact reduction repeats a hostile target GUID"
            )
        by_guid[guid] = row

    raw_targets = raw_encounter.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise UpperKaraCompactEncounterModelV1Error(
            "registry encounter targets must be a nonempty list"
        )
    registry_by_guid: dict[str, Mapping[str, Any]] = {}
    for value in raw_targets:
        target = _mapping(value, "registry target")
        guid = _text(target.get("target_guid"), "target_guid")
        if guid in registry_by_guid:
            raise UpperKaraCompactEncounterModelV1Error(
                "registry encounter repeats a target GUID"
            )
        registry_by_guid[guid] = target

    raw_resolution_targets = raw_resolution.get("targets")
    if not isinstance(raw_resolution_targets, list) or not raw_resolution_targets:
        raise UpperKaraCompactEncounterModelV1Error(
            "resolution.targets must be a nonempty list"
        )
    resolution_by_guid: dict[str, Mapping[str, Any]] = {}
    occurrence_ids: set[str] = set()
    eligible_resolution_rows: list[Mapping[str, Any]] = []
    eligible_guids: set[str] = set()
    excluded_guids: set[str] = set()
    allowed_eligible_kinds = {
        REGISTRY_CREATURE_OCCURRENCE,
        EXACT_F130_CREATURE_OCCURRENCE,
        BOSS_OWNED_SUMMON_OCCURRENCE,
    }
    for value in raw_resolution_targets:
        row = _mapping(value, "resolved target")
        guid = _text(row.get("target_guid"), "resolved target GUID")
        occurrence_id = _text(
            row.get("resolved_occurrence_id"), "resolved occurrence ID"
        )
        if guid in resolution_by_guid:
            raise UpperKaraCompactEncounterModelV1Error(
                "target-universe resolution repeats a target GUID"
            )
        if occurrence_id in occurrence_ids:
            raise UpperKaraCompactEncounterModelV1Error(
                "target-universe resolution repeats a resolved occurrence ID"
            )
        resolution_by_guid[guid] = row
        occurrence_ids.add(occurrence_id)
        identity_kind = _text(row.get("resolution"), "target identity kind")
        eligible = row.get("eligible_encounter_hostile")
        if not isinstance(eligible, bool):
            raise UpperKaraCompactEncounterModelV1Error(
                "resolved target eligibility must be boolean"
            )
        stable_entry = _optional_positive_int(
            row.get("stable_template_creature_entry_id"),
            "stable template creature entry",
        )
        registry_target = registry_by_guid.get(guid)
        if eligible:
            if identity_kind not in allowed_eligible_kinds:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"eligible target {guid} has an unresolved identity kind"
                )
            if identity_kind == REGISTRY_CREATURE_OCCURRENCE:
                if registry_target is None:
                    raise UpperKaraCompactEncounterModelV1Error(
                        f"resolved registry target {guid} is absent from encounter"
                    )
                registry_occurrence_id = _text(
                    registry_target.get("occurrence_id"),
                    "registry occurrence ID",
                )
                registry_entry = _positive_int(
                    registry_target.get("creature_entry_id"),
                    "registry creature entry",
                )
                if occurrence_id != registry_occurrence_id or stable_entry != registry_entry:
                    raise UpperKaraCompactEncounterModelV1Error(
                        f"resolved registry identity differs for target {guid}"
                    )
            elif registry_target is not None:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"registry target {guid} has a nonregistry identity kind"
                )
            if (
                identity_kind == EXACT_F130_CREATURE_OCCURRENCE
                and stable_entry is None
            ):
                raise UpperKaraCompactEncounterModelV1Error(
                    f"exact F130 target {guid} has no stable creature entry"
                )
            if (
                identity_kind == BOSS_OWNED_SUMMON_OCCURRENCE
                and guid.upper().startswith("0XF140")
                and stable_entry is not None
            ):
                raise UpperKaraCompactEncounterModelV1Error(
                    f"F140 boss-owned summon {guid} must not expose a template entry"
                )
            eligible_resolution_rows.append(row)
            eligible_guids.add(guid)
        else:
            if identity_kind != EXCLUDED_EXACT_PLAYER_OWNED_UNIT:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"ineligible target {guid} lacks exact player-owned exclusion"
                )
            if registry_target is not None:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"registry target {guid} cannot be excluded as player-owned"
                )
            excluded_guids.add(guid)

    registry_target_guids = set(registry_by_guid)
    if not registry_target_guids.issubset(eligible_guids):
        raise UpperKaraCompactEncounterModelV1Error(
            "target-universe resolution is missing an eligible registry target"
        )
    if set(by_guid) != eligible_guids | excluded_guids:
        raise UpperKaraCompactEncounterModelV1Error(
            "compact reduction target universe differs from the resolved universe: "
            f"missing={sorted((eligible_guids | excluded_guids) - set(by_guid))!r}, "
            f"extra={sorted(set(by_guid) - (eligible_guids | excluded_guids))!r}"
        )
    if set(by_guid) - excluded_guids != eligible_guids:
        raise UpperKaraCompactEncounterModelV1Error(
            "compact reduction minus exact player-owned exclusions differs from "
            "the eligible resolved target universe"
        )

    raw_fury_candidates = raw_reduction.get("fury_focal_candidates")
    if not isinstance(raw_fury_candidates, list):
        raise UpperKaraCompactEncounterModelV1Error(
            "reduction.fury_focal_candidates must be a list"
        )
    candidate_rows = [
        _mapping(value, "Fury focal candidate")
        for value in raw_fury_candidates
        if isinstance(value, Mapping) and value.get("player_guid") == focal
    ]
    if len(candidate_rows) != 1:
        raise UpperKaraCompactEncounterModelV1Error(
            "focal_player_guid must identify exactly one observed exact-GUID Fury candidate"
        )
    focal_candidate = candidate_rows[0]
    if (
        focal_candidate.get("player_class") != "WARRIOR"
        or focal_candidate.get("observed_spec") != "Fury"
        or focal_candidate.get("source_summary_closes_to_leave_one_out") is not True
        or focal_candidate.get("comparison_authorized") is not False
    ):
        raise UpperKaraCompactEncounterModelV1Error(
            "focal Fury candidate evidence is not exact or remains comparison-authorized"
        )

    modeled_targets: list[JSONMap] = []
    for value in eligible_resolution_rows:
        resolved_target = _mapping(value, "eligible resolved target")
        occurrence_id = _text(
            resolved_target.get("resolved_occurrence_id"), "occurrence_id"
        )
        guid = _text(resolved_target.get("target_guid"), "target_guid")
        identity_kind = _text(
            resolved_target.get("resolution"), "target identity kind"
        )
        target = registry_by_guid.get(guid)
        try:
            evidence = by_guid[guid]
        except KeyError as error:
            raise UpperKaraCompactEncounterModelV1Error(
                f"compact reduction has no evidence for eligible target {guid}"
            ) from error
        incoming = _mapping(evidence.get("incoming_damage"), "incoming_damage")
        if incoming.get("scope") != "STRICTLY_BEFORE_FIRST_DEAD_MARKER":
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} incoming damage is not death-truncated"
            )
        if _nonnegative_int(
            incoming.get("amount_unavailable_event_count"),
            "incoming damage unavailable count",
        ):
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} has pre-death damage with unavailable amount"
            )
        adjusted = _mapping(
            incoming.get("overkill_adjusted_effective_damage"),
            "overkill-adjusted damage",
        )
        if adjusted.get("status") != "FIELD_DERIVATION":
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} lacks complete overkill-adjusted damage"
            )
        effective_damage = _positive_int(
            adjusted.get("value"), "overkill-adjusted damage value"
        )
        healing = _mapping(evidence.get("healing_received"), "healing_received")
        if healing.get("scope") != "STRICTLY_BEFORE_FIRST_DEAD_MARKER":
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} healing is not death-truncated"
            )
        if _nonnegative_int(
            healing.get("amount_unavailable_event_count"),
            "healing unavailable count",
        ):
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} has pre-death healing with unavailable amount"
            )
        effective_healing = _nonnegative_int(
            healing.get("positive_sum"), "positive healing sum"
        )
        hp_balance = effective_damage - effective_healing
        if hp_balance <= 0:
            raise UpperKaraCompactEncounterModelV1Error(
                f"target {guid} has a nonpositive observed damage balance"
            )

        death = _mapping(evidence.get("death"), "death")
        if death.get("status") != "OBSERVED":
            raise UpperKaraCompactEncounterModelV1Error(
                f"eligible kill target {guid} has no observed death"
            )
        death_offset = _nonnegative_int(death.get("offset_ms"), "death offset")
        if target is not None:
            metadata_death = target.get("death_offset_ms")
            if metadata_death != death_offset:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"target {guid} death offset differs between metadata and exact trace"
                )
            death_offset_source = "REGISTRY_CROSSCHECKED_EXTERNAL_REDUCTION"
        else:
            death_offset_source = "EXTERNAL_REDUCTION_DESCRIPTIVE_OUTCOME"

        by_player = {
            str(player): _nonnegative_int(amount, "positive damage by player")
            for player, amount in _mapping(
                incoming.get("by_player_guid"), "damage by player"
            ).items()
        }
        focal_damage = _nonnegative_int(
            by_player.get(focal, 0), "focal positive damage"
        )
        positive_sum = _positive_int(
            incoming.get("positive_sum"), "positive incoming damage"
        )
        if focal_damage > positive_sum:
            raise UpperKaraCompactEncounterModelV1Error(
                "focal damage exceeds total incoming damage"
            )
        named_player_total = sum(by_player.values())
        if named_player_total > positive_sum:
            raise UpperKaraCompactEncounterModelV1Error(
                "named player damage exceeds total incoming damage"
            )
        residual_damage = positive_sum - named_player_total
        explicit_unattributed = _nonnegative_int(
            incoming.get("unattributed_positive_sum"),
            "explicit unattributed positive damage",
        )
        if explicit_unattributed > residual_damage:
            raise UpperKaraCompactEncounterModelV1Error(
                "explicit unattributed damage exceeds damage without an exact player GUID"
            )
        teammate_totals = {
            player: amount
            for player, amount in sorted(by_player.items())
            if player != focal
        }
        if focal_damage + sum(teammate_totals.values()) + residual_damage != positive_sum:
            raise UpperKaraCompactEncounterModelV1Error(
                "focal, teammate and residual target damage do not close"
            )

        timing = _mapping(evidence.get("team_damage_timing"), "team_damage_timing")
        raw_bins = timing.get("positive_incoming_damage_bins")
        if not isinstance(raw_bins, list):
            raise UpperKaraCompactEncounterModelV1Error(
                "team damage timing bins must be a list"
            )
        bins: list[JSONMap] = []
        observed_total = 0
        observed_focal = 0
        for raw_bin in raw_bins:
            bucket = _mapping(raw_bin, "team damage bin")
            total = _nonnegative_int(
                bucket.get("positive_damage_sum"), "bin positive damage"
            )
            per_player = {
                str(player): _nonnegative_int(amount, "bin positive damage by player")
                for player, amount in _mapping(
                    bucket.get("positive_damage_by_player_guid"),
                    "bin damage by player",
                ).items()
            }
            focal_bin = _nonnegative_int(
                per_player.get(focal, 0), "bin focal damage"
            )
            if focal_bin > total:
                raise UpperKaraCompactEncounterModelV1Error(
                    "bin focal damage exceeds bin total damage"
                )
            named_bin_total = sum(per_player.values())
            if named_bin_total > total:
                raise UpperKaraCompactEncounterModelV1Error(
                    "named player damage exceeds bin total damage"
                )
            residual_bin = total - named_bin_total
            bins.append(
                {
                    "start_offset_ms": _nonnegative_int(
                        bucket.get("start_offset_ms"), "bin start"
                    ),
                    "end_offset_ms_exclusive": _positive_int(
                        bucket.get("end_offset_ms_exclusive"), "bin end"
                    ),
                    "all_positive_damage": total,
                    "focal_positive_damage_removed": focal_bin,
                    "leave_one_out_positive_damage": total - focal_bin,
                    "residual_positive_damage_without_exact_player_guid": residual_bin,
                    "teammate_positive_damage_by_player_guid": {
                        player: amount
                        for player, amount in sorted(per_player.items())
                        if player != focal
                    },
                }
            )
            observed_total += total
            observed_focal += focal_bin
        if observed_total != positive_sum or observed_focal != focal_damage:
            raise UpperKaraCompactEncounterModelV1Error(
                "damage bins do not close to target/player totals"
            )

        post_death = _mapping(
            evidence.get("post_first_death_activity"),
            "post_first_death_activity",
        )
        if post_death.get("excluded_from_hp_balance_and_team_kill_clock") is not True:
            raise UpperKaraCompactEncounterModelV1Error(
                "post-death activity is not explicitly excluded"
            )

        if target is not None:
            periods = target.get("periods")
            if not isinstance(periods, list) or not periods:
                raise UpperKaraCompactEncounterModelV1Error(
                    "registry target periods must be a nonempty list"
                )
            activity_windows = [
                {
                    "start_offset_ms": _nonnegative_int(
                        _mapping(period, "target period").get("start_offset_ms"),
                        "period start",
                    ),
                    "end_offset_ms": _nonnegative_int(
                        _mapping(period, "target period").get("end_offset_ms"),
                        "period end",
                    ),
                }
                for period in periods
            ]
            activity_window_source = "REGISTRY_METADATA_PERIOD_PROXY"
        else:
            activity = _mapping(evidence.get("activity"), "activity")
            if activity.get("status") != "OBSERVED":
                raise UpperKaraCompactEncounterModelV1Error(
                    f"extra target {guid} has no observed reduction activity window"
                )
            activity_start = _nonnegative_int(
                activity.get("first_relevant_offset_ms"), "activity start"
            )
            activity_end = _nonnegative_int(
                activity.get("last_relevant_offset_ms"), "activity end"
            )
            if activity_start > activity_end:
                raise UpperKaraCompactEncounterModelV1Error(
                    f"extra target {guid} has a reversed reduction activity window"
                )
            activity_windows = [
                {
                    "start_offset_ms": activity_start,
                    "end_offset_ms": activity_end,
                }
            ]
            activity_window_source = "EXTERNAL_REDUCTION_DESCRIPTIVE_OUTCOME_PROXY"
        entry = _optional_positive_int(
            resolved_target.get("stable_template_creature_entry_id"),
            "stable template creature entry",
        )
        modeled_targets.append(
            {
                "occurrence_id": occurrence_id,
                "target_guid": guid,
                "identity_kind": identity_kind,
                "creature_entry_id": entry,
                "hp_model_ref": _target_ref(identity, occurrence_id, "hp"),
                "armor_model_ref": _target_ref(identity, occurrence_id, "armor"),
                "team_kill_clock_ref": _target_ref(
                    identity, occurrence_id, "team-kill-clock"
                ),
                "attackability_ref": _target_ref(
                    identity, occurrence_id, "attackability"
                ),
                "hp_model": {
                    "kind": "OBSERVED_KILL_DAMAGE_BALANCE_POINT_HYPOTHESIS",
                    "point_health": hp_balance,
                    "overkill_adjusted_positive_damage": effective_damage,
                    "positive_healing_received": effective_healing,
                    "observed_max_health": None,
                    "status": "DEVELOPMENT_HYPOTHESIS",
                    "post_first_death_activity": dict(post_death),
                },
                "armor_model": {
                    "kind": "EXPLICIT_SENSITIVITY_GRID",
                    "base_armor_hypotheses": list(armors),
                    "chronicle_observed_armor": None,
                },
                "team_kill_clock_model": {
                    "kind": "EXACT_ATTEMPT_BINNED_FOCAL_LEAVE_ONE_OUT",
                    "focal_player_guid": focal,
                    "all_positive_damage": positive_sum,
                    "focal_positive_damage_removed": focal_damage,
                    "leave_one_out_positive_damage": positive_sum - focal_damage,
                    "teammate_positive_damage_by_player_guid": teammate_totals,
                    "residual_positive_damage_without_exact_player_guid": residual_damage,
                    "explicit_unattributed_positive_damage": explicit_unattributed,
                    "damage_bins": bins,
                    "death_offset_ms": death_offset,
                    "death_offset_source": death_offset_source,
                    "death_offset_is_descriptive_outcome": True,
                    "responsive_to_candidate_policy": False,
                },
                "attackability_model": {
                    "kind": activity_window_source,
                    "activity_windows": activity_windows,
                    "exact_attackability": None,
                    "descriptive_outcome_proxy": True,
                    "policy_visible_future_schedule": False,
                },
            }
        )

    return {
        "schema": SCHEMA,
        "status": STATUS,
        "model_id": identity,
        "source": {
            "instance_id": raw_encounter.get("instance_id"),
            "encounter_id": raw_encounter.get("encounter_id"),
            "pull_ref": raw_encounter.get("pull_ref"),
            "compact_reduction": dict(source),
            "target_universe_resolution": {
                "schema": raw_resolution.get("schema"),
                "status": raw_resolution.get("status"),
                "authorized": True,
            },
        },
        "focal_player_guid": focal,
        "focal_player_source_evidence": dict(focal_candidate),
        "targets": modeled_targets,
        "scientific_boundaries": {
            "target_max_health_observed": False,
            "target_armor_observed": False,
            "team_schedule_focal_leave_one_out": True,
            "per_teammate_damage_timing_retained": True,
            "residual_damage_retained_exactly_once": True,
            "target_universe_matches_authorized_eligible_resolution_exactly": True,
            "target_universe_matches_registry_exactly": (
                eligible_guids == registry_target_guids
            ),
            "eligible_target_count": len(eligible_guids),
            "registry_target_count": len(registry_target_guids),
            "excluded_exact_player_owned_target_count": len(excluded_guids),
            "nonregistry_eligible_targets_use_descriptive_activity_and_death_proxies": True,
            "descriptive_activity_and_death_are_not_policy_visible_future_state": True,
            "team_response_to_candidate_policy_learned": False,
            "comparison_authorized": False,
        },
    }


def target_bindings_from_compact_encounter_model_v1(
    model: Mapping[str, Any],
) -> dict[str, WaveTargetBindingV1]:
    raw = _mapping(model, "model")
    if raw.get("schema") != SCHEMA or raw.get("status") != STATUS:
        raise UpperKaraCompactEncounterModelV1Error(
            "unexpected compact encounter model schema or status"
        )
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise UpperKaraCompactEncounterModelV1Error(
            "compact encounter model targets must be a nonempty list"
        )
    result: dict[str, WaveTargetBindingV1] = {}
    for value in targets:
        row = _mapping(value, "modeled target")
        occurrence_id = _text(row.get("occurrence_id"), "occurrence_id")
        if occurrence_id in result:
            raise UpperKaraCompactEncounterModelV1Error(
                "compact encounter model repeats an occurrence_id"
            )
        result[occurrence_id] = WaveTargetBindingV1(
            occurrence_id=occurrence_id,
            target_guid=_text(row.get("target_guid"), "target_guid"),
            identity_kind=_text(row.get("identity_kind"), "identity_kind"),
            creature_entry_id=_optional_positive_int(
                row.get("creature_entry_id"), "creature entry"
            ),
            hp_model_ref=_text(row.get("hp_model_ref"), "hp_model_ref"),
            armor_model_ref=_text(row.get("armor_model_ref"), "armor_model_ref"),
            team_kill_clock_ref=_text(
                row.get("team_kill_clock_ref"), "team_kill_clock_ref"
            ),
            attackability_ref=_text(
                row.get("attackability_ref"), "attackability_ref"
            ),
        )
    return result


__all__ = (
    "SCHEMA",
    "STATUS",
    "UpperKaraCompactEncounterModelV1Error",
    "build_compact_encounter_development_model_v1",
    "target_bindings_from_compact_encounter_model_v1",
)
