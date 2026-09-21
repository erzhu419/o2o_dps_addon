"""Held-out target-choice labels and strict-prefix target evidence.

This is a diagnostic, not a simulated policy observation.  A Chronicle action
shows what was attempted; it does not establish raid-leader instructions or
whether every other mob was in melee range.  Only direct player START rows
vote as choices; GO, damage, and splash never create a choice label.
"""

from __future__ import annotations

from collections import defaultdict
import gzip
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "upper_kara_target_prefix_validation/v1"
HOSTILE = {"HOSTILE_CREATURE", "HOSTILE_OBJECT", "HOSTILE_PLAYER"}
EVENT_CHOICES = {"FIRST_ACQUISITION", "RETARGET", "STAY"}
D900_WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
D900_TARGETS = (
    "0xF13000F240276CB6",
    "0xF13000F244276CB4",
    "0xF13000F245276CB3",
)


def _hostile_guid(side: object) -> str | None:
    if isinstance(side, Mapping) and side.get("lane") in HOSTILE:
        guid = side.get("guid")
        return guid if isinstance(guid, str) else None
    return None


def _direct_player(row: Mapping[str, Any], event: Mapping[str, Any]) -> str | None:
    actor = row.get("player_guid")
    source = event.get("source")
    attribution = event.get("attribution")
    if (
        row.get("trace_kind") == "EXACT_PLAYER_EVENT"
        and isinstance(actor, str)
        and isinstance(source, Mapping)
        and source.get("lane") == "FRIENDLY_PLAYER"
        and isinstance(attribution, Mapping)
        and attribution.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
        and attribution.get("player_guid") == actor
    ):
        return actor
    return None


def _positive_damage(event: Mapping[str, Any]) -> int:
    if event.get("event_type") != "DMG":
        return 0
    damage = event.get("damage")
    amount = damage.get("amount") if isinstance(damage, Mapping) else None
    return amount if type(amount) is int and amount > 0 else 0


def _metric() -> dict[str, Any]:
    return {
        "labels": 0,
        "label_prefix_visible": 0,
        "label_prefix_activity_visible": 0,
        "eligible_multi_choice": 0,
        "uniform_expected_correct": 0.0,
        "sticky_expected_correct": 0.0,
    }


def _snapshot_candidates(
    candidates: set[str], *, now: int, first_seen: Mapping[str, int],
    damage_by_target: Mapping[str, int], recent_actions: Mapping[str, list[int]],
    recent_damage: Mapping[str, list[tuple[int, int]]],
) -> list[dict[str, Any]]:
    rows = []
    for guid in sorted(candidates):
        rows.append({
            "target_guid": guid,
            "first_seen_ms": first_seen[guid],
            "prefix_damage": damage_by_target.get(guid, 0),
            "recent_action_count_3000ms": sum(now - 3000 < t <= now for t in recent_actions[guid]),
            "recent_damage_amount_3000ms": sum(
                amount for t, amount in recent_damage[guid] if now - 3000 < t <= now
            ),
            "observed_dead": False,
        })
    return rows


def analyze_target_prefix_wave_v1(
    record: Mapping[str, Any], *, detail: bool = False,
) -> dict[str, Any]:
    """Read one exact trace; snapshots are made before observing each START row."""
    wave = record.get("wave")
    trace = record.get("exact_trace")
    if not isinstance(wave, Mapping) or not isinstance(trace, list):
        raise ValueError("wave and exact_trace required")
    wave_id = wave.get("wave_id")
    if not isinstance(wave_id, str):
        raise ValueError("wave_id required")
    first_context_seen: dict[str, int] = {}
    first_activity_seen: dict[str, int] = {}
    observed_dead: set[str] = set()
    damage_by_target: dict[str, int] = defaultdict(int)
    recent_actions: dict[str, list[int]] = defaultdict(list)
    recent_damage: dict[str, list[tuple[int, int]]] = defaultdict(list)
    actor_last_start: dict[str, str] = {}
    first_directed: dict[str, dict[str, Any]] = {}
    first_positive_damage: dict[str, int] = {}
    death_times: dict[str, int] = {}
    metrics = {kind: {mode: _metric() for mode in ("CONTEXT_OR_ACTIVITY", "ACTIVITY_ONLY")}
               for kind in EVENT_CHOICES}
    detailed_choices = []
    unseen_first_acquisitions = []
    action_targets: set[str] = set()
    hostile_guids: set[str] = set()
    for order, row in enumerate(trace):
        if not isinstance(row, Mapping):
            continue
        anchor = row.get("anchor")
        if not isinstance(anchor, Mapping):
            continue
        now = anchor.get("offset_ms")
        if type(now) is not int or now < 0:
            continue
        if row.get("trace_kind") == "CLASSIFICATION_CONTEXT":
            classification = row.get("classification")
            if isinstance(classification, Mapping) and classification.get("lane") in HOSTILE:
                guid = classification.get("guid")
                if isinstance(guid, str):
                    first_context_seen.setdefault(guid, now)
                    hostile_guids.add(guid)
            continue
        event = row.get("event")
        if not isinstance(event, Mapping):
            continue
        source_guid = _hostile_guid(event.get("source"))
        target_guid = _hostile_guid(event.get("target"))
        actor = _direct_player(row, event)
        kind = event.get("event_type")
        # A START's target is deliberately not added to its own candidate set.
        if kind == "START" and actor and target_guid:
            previous = actor_last_start.get(actor)
            choice_type = (
                "FIRST_ACQUISITION" if previous is None else
                "STAY" if previous == target_guid else "RETARGET"
            )
            first_directed.setdefault(target_guid, {
                "time_ms": now, "trace_index": row.get("trace_index", order), "actor_guid": actor,
            })
            action_targets.add(target_guid)
            context_candidates = (
                set(first_context_seen) | set(first_activity_seen)
            ) - observed_dead
            activity_candidates = set(first_activity_seen) - observed_dead
            candidate_sets = {
                "CONTEXT_OR_ACTIVITY": context_candidates,
                "ACTIVITY_ONLY": activity_candidates,
            }
            for mode, candidates in candidate_sets.items():
                stat = metrics[choice_type][mode]
                stat["labels"] += 1
                if target_guid in candidates:
                    stat["label_prefix_visible"] += 1
                    if target_guid in activity_candidates:
                        stat["label_prefix_activity_visible"] += 1
                    if len(candidates) >= 2:
                        stat["eligible_multi_choice"] += 1
                        stat["uniform_expected_correct"] += 1.0 / len(candidates)
                        stat["sticky_expected_correct"] += (
                            1.0 if previous in candidates and previous == target_guid
                            else 0.0 if previous in candidates else 1.0 / len(candidates)
                        )
            if choice_type == "FIRST_ACQUISITION" and target_guid not in activity_candidates:
                unseen_first_acquisitions.append({
                    "actor_guid": actor,
                    "time_ms": now,
                    "trace_index": row.get("trace_index", order),
                    "label_target_guid": target_guid,
                    "label_context_visible": target_guid in context_candidates,
                    "prefix_activity_candidate_count": len(activity_candidates),
                    "prefix_context_candidate_count": len(context_candidates),
                })
            if detail and choice_type != "STAY":
                detailed_choices.append({
                    "actor_guid": actor,
                    "time_ms": now,
                    "trace_index": row.get("trace_index", order),
                    "choice_type": choice_type,
                    "label_target_guid": target_guid,
                    "actor_previous_start_target_guid": previous,
                    "prefix_context_candidates": _snapshot_candidates(
                        context_candidates, now=now,
                        first_seen={guid: min(first_context_seen.get(guid, now), first_activity_seen.get(guid, now))
                                    for guid in context_candidates},
                        damage_by_target=damage_by_target, recent_actions=recent_actions,
                        recent_damage=recent_damage,
                    ),
                    "prefix_activity_candidate_guids": sorted(activity_candidates),
                    "label_context_visible": target_guid in context_candidates,
                    "label_activity_visible": target_guid in activity_candidates,
                })
            actor_last_start[actor] = target_guid
        for guid in (source_guid, target_guid):
            if guid:
                hostile_guids.add(guid)
                first_activity_seen.setdefault(guid, now)
        if target_guid and kind == "DEAD":
            observed_dead.add(target_guid)
            death_times.setdefault(target_guid, now)
        if target_guid and actor and kind in {"START", "GO"}:
            recent_actions[target_guid].append(now)
        if target_guid:
            amount = _positive_damage(event)
            if amount:
                damage_by_target[target_guid] += amount
                recent_damage[target_guid].append((now, amount))
                first_positive_damage.setdefault(target_guid, now)
    result = {
        "schema": SCHEMA,
        "wave_id": wave_id,
        "trace_rows": len(trace),
        "observed_hostile_target_count": len(hostile_guids),
        "direct_start_target_count": len(action_targets),
        "target_choice_metrics": metrics,
        "first_acquisition_unseen_activity_labels": unseen_first_acquisitions,
        "source_boundary": {
            "candidate_cutoff": "strictly before current trace row, including same-time earlier rows",
            "label": "direct friendly-player START only; GO and DMG cannot vote",
            "context_candidates": "prior classification context or prior hostile event; dead removed",
            "activity_candidates": "prior hostile event only; dead removed",
            "attackability_known": False,
            "historical_death_times_as_feature": False,
            "raid_leader_route_known": False,
        },
    }
    if detail:
        result["descriptive_target_onset_and_death"] = {
            guid: {
                "first_classification_context_ms": first_context_seen.get(guid),
                "first_hostile_activity_ms": first_activity_seen.get(guid),
                "first_directed_start": first_directed.get(guid),
                "first_positive_damage_ms": first_positive_damage.get(guid),
                "historical_death_ms": death_times.get(guid),
            }
            for guid in sorted(hostile_guids)
        }
        result["first_acquisition_and_retarget_labels"] = detailed_choices
    return result


def analyze_heldout_instance_gzip_v1(path: str | Path) -> dict[str, Any]:
    """Sweep one held-out instance on the server, emitting only compact JSON."""
    cohort = []
    selected = None
    instance_id = None
    for line in gzip.open(path, "rt", encoding="utf-8"):
        record = json.loads(line)
        wave = record.get("wave")
        if not isinstance(wave, Mapping):
            continue
        instance_id = wave.get("instance_id")
        provenance = record.get("raid_provenance")
        contamination = provenance.get("contamination") if isinstance(provenance, Mapping) else None
        clean_candidate = (
            isinstance(contamination, Mapping)
            and contamination.get("candidate_filter_passed") is True
        )
        is_selected = wave.get("wave_id") == D900_WAVE
        result = analyze_target_prefix_wave_v1(record, detail=is_selected)
        if is_selected:
            selected = result
            result["raid_clean_candidate_filter_passed"] = clean_candidate
        if clean_candidate and 2 <= result["direct_start_target_count"] <= 6:
            cohort.append(result)
    if selected is None:
        raise ValueError("selected d900 wave absent")
    summary = {kind: {mode: _metric() for mode in ("CONTEXT_OR_ACTIVITY", "ACTIVITY_ONLY")}
               for kind in EVENT_CHOICES}
    for wave in cohort:
        for kind, modes in wave["target_choice_metrics"].items():
            for mode, values in modes.items():
                for key, value in values.items():
                    summary[kind][mode][key] += value
    return {
        "schema": "upper_kara_heldout_target_prefix_validation/v1",
        "status": "DESCRIPTIVE_LABEL_COVERAGE_NOT_POLICY_COMPARISON",
        "instance_id": instance_id,
        "cohort_rule": (
            "same held-out instance; raid contamination candidate filter passed; "
            "2-6 distinct direct-player START targets"
        ),
        "cohort_wave_count": len(cohort),
        "cohort_wave_ids": [wave["wave_id"] for wave in cohort],
        "cohort_metrics": summary,
        "cohort_first_acquisition_unseen_activity_labels": [
            {"wave_id": wave["wave_id"], **row}
            for wave in cohort for row in wave["first_acquisition_unseen_activity_labels"]
        ],
        "descriptive_uniform_contract": {
            "denominator": "raw observed candidates; NOT the learned head's retarget denominator",
            "uniform_expected_accuracy": {
                kind: {mode: (
                    values["uniform_expected_correct"] / values["eligible_multi_choice"]
                    if values["eligible_multi_choice"] else None
                ) for mode, values in modes.items()}
                for kind, modes in summary.items()
            },
            "learned_head_comparison_authorized": False,
            "reason": (
                "retarget here retains the actor's old target and is descriptive; "
                "use upper_kara_target_head_heldout_eval/v1 for the exact head denominator"
            ),
        },
        "selected_d900": selected,
    }


__all__ = (
    "analyze_target_prefix_wave_v1",
    "analyze_heldout_instance_gzip_v1",
)
