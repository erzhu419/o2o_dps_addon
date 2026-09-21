"""Compact Chronicle evidence audit for Fury cooldowns and on-use effects.

Only manifest-selected ``historical_fury_expert_episodes`` partitions are
streamed.  Raw Chronicle objects and the multi-gigabyte team timeline are not
opened.  Successful-cast spacing is reported as observed spacing: without an
availability observation it can upper-bound, but cannot lower-bound, the true
cooldown.  Spell START/GO events also cannot identify aura expiry or effect
magnitude.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import gzip
import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping


JSONMap = dict[str, Any]
DEFAULT_EPISODE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "offline_data/derived/historical_fury_expert_episodes/v1/manifest.json"
)

# IDs here identify Chronicle spell events, not inventory items.  17528 is the
# observed "Mighty Rage" effect spell and does not resolve the localized Contra
# item name to a concrete item ID.
TARGET_ACTIONS: Mapping[str, frozenset[int]] = {
    "warrior.recklessness": frozenset({1719}),
    "warrior.death_wish": frozenset({12328}),
    "contra_rage_potion_effect_unresolved_item": frozenset({17528}),
    "trinket.slayers_crest_effect": frozenset({28777}),
    "trinket.kiss_of_the_spider_effect": frozenset({28866}),
}
TARGET_BY_SPELL_ID = {
    spell_id: action_id
    for action_id, spell_ids in TARGET_ACTIONS.items()
    for spell_id in spell_ids
}


def _percentiles(values: list[int]) -> JSONMap | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(fraction: float) -> int:
        return ordered[round((len(ordered) - 1) * fraction)]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": pick(0.25),
        "median": int(median(ordered)),
        "p75": pick(0.75),
        "max": ordered[-1],
    }


def iter_target_observations_v1(
    manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
) -> Iterable[JSONMap]:
    """Yield target action events from the 24 current compact partitions."""

    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    for partition in manifest["partitions"]:
        path = manifest_file.parent / partition["path"]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                episode = json.loads(line)
                player_guid = episode["player"]["guid"].upper()
                for wave in episode["wave_observations"]:
                    for transition in wave["prefix_transitions"]:
                        event = transition["observed_event"]
                        spell = event.get("spell") or {}
                        spell_id = spell.get("id")
                        action_id = TARGET_BY_SPELL_ID.get(spell_id)
                        if action_id is None:
                            continue
                        anchor = event["anchor"]
                        payload = event.get("action_payload") or {}
                        yield {
                            "action_id": action_id,
                            "instance_id": episode["instance_id"],
                            "encounter_id": episode["encounter_id"],
                            "encounter_ordinal": wave.get("encounter_ordinal"),
                            "player_guid": player_guid,
                            "phase": event["phase"],
                            "stream_type": anchor["stream_type"],
                            "timestamp_ms": anchor["timestamp_ms"],
                            "wave_elapsed_ms": transition["state_before"][
                                "wave_elapsed_ms"
                            ],
                            "order_key": anchor["order_key"]
                            if "order_key" in anchor
                            else event["order_key"],
                            "spell_id": spell_id,
                            "spell_name": spell.get("name"),
                            "item_id": payload.get("item_id"),
                        }


def summarize_cooldown_observations_v1(
    observations: Iterable[Mapping[str, Any]],
    *,
    state_event_partition_count: int = 0,
) -> JSONMap:
    """Summarize START/GO evidence without inventing cooldown or aura facts."""

    unique: dict[tuple[Any, ...], JSONMap] = {}
    for raw in observations:
        row = dict(raw)
        identity = (
            row["instance_id"],
            row["encounter_id"],
            row["player_guid"],
            tuple(row["order_key"]),
            row["phase"],
            row["spell_id"],
        )
        unique[identity] = row
    rows = sorted(
        unique.values(),
        key=lambda row: (
            row["timestamp_ms"],
            tuple(row["order_key"]),
            row["player_guid"],
        ),
    )

    grouped: dict[str, list[JSONMap]] = defaultdict(list)
    for row in rows:
        grouped[row["action_id"]].append(row)

    action_reports: dict[str, JSONMap] = {}
    successful_starts: dict[str, list[JSONMap]] = defaultdict(list)
    for action_id in TARGET_ACTIONS:
        action_rows = grouped.get(action_id, [])
        phase_counts = Counter(row["phase"] for row in action_rows)
        stream_counts = Counter(row["stream_type"] for row in action_rows)
        pending: dict[tuple[str, str, str], deque[JSONMap]] = defaultdict(deque)
        start_go_ms: list[int] = []
        for row in action_rows:
            key = (row["instance_id"], row["encounter_id"], row["player_guid"])
            if row["phase"] == "START":
                pending[key].append(row)
            elif row["phase"] in {"GO", "FAIL"} and pending[key]:
                start = pending[key].popleft()
                if row["phase"] == "GO":
                    start_go_ms.append(row["timestamp_ms"] - start["timestamp_ms"])
                    successful_starts[action_id].append(start)

        starts_by_player: dict[tuple[str, str], list[JSONMap]] = defaultdict(list)
        for row in successful_starts[action_id]:
            starts_by_player[(row["instance_id"], row["player_guid"])].append(row)
        adjacent_pairs: list[tuple[int, JSONMap, JSONMap]] = []
        for player_rows in starts_by_player.values():
            ordered_rows = sorted(
                {row["timestamp_ms"]: row for row in player_rows}.values(),
                key=lambda row: row["timestamp_ms"],
            )
            adjacent_pairs.extend(
                (later["timestamp_ms"] - earlier["timestamp_ms"], earlier, later)
                for earlier, later in zip(ordered_rows, ordered_rows[1:])
            )
        adjacent_ms = [delta for delta, _, _ in adjacent_pairs]
        start_offsets = [
            row["wave_elapsed_ms"]
            for row in action_rows
            if row["phase"] == "START"
        ]
        observed_names = sorted(
            {row["spell_name"] for row in action_rows if row.get("spell_name")}
        )
        observed_item_ids = sorted(
            {row["item_id"] for row in action_rows if row.get("item_id") is not None}
        )
        minimum_interval = min(adjacent_ms) if adjacent_ms else None
        minimum_witness = None
        if adjacent_pairs:
            delta, earlier, later = min(adjacent_pairs, key=lambda row: row[0])
            minimum_witness = {
                "interval_ms": delta,
                "instance_id": earlier["instance_id"],
                "player_guid": earlier["player_guid"],
                "first": {
                    "encounter_id": earlier["encounter_id"],
                    "timestamp_ms": earlier["timestamp_ms"],
                    "wave_elapsed_ms": earlier["wave_elapsed_ms"],
                },
                "second": {
                    "encounter_id": later["encounter_id"],
                    "timestamp_ms": later["timestamp_ms"],
                    "wave_elapsed_ms": later["wave_elapsed_ms"],
                },
            }
        action_reports[action_id] = {
            "spell_ids": sorted(TARGET_ACTIONS[action_id]),
            "observed_spell_names": observed_names,
            "observed_item_ids": observed_item_ids,
            "event_count": len(action_rows),
            "phase_counts": dict(sorted(phase_counts.items())),
            "stream_type_counts": dict(sorted(stream_counts.items())),
            "unique_player_count": len({row["player_guid"] for row in action_rows}),
            "start_wave_elapsed_ms": _percentiles(start_offsets),
            "start_to_go_ms": _percentiles(start_go_ms),
            "same_instance_player_adjacent_successful_start_ms": _percentiles(
                adjacent_ms
            ),
            "minimum_observed_successful_start_interval_ms": minimum_interval,
            "minimum_interval_witness": minimum_witness,
            "cooldown_lower_bound_ms": None,
            "cooldown_inference": (
                "minimum successful-cast spacing is only an upper bound on the "
                "true cooldown if no reset occurred; it supplies no nontrivial "
                "cooldown lower bound"
            ),
            "aura_begin_or_end_event_count": sum(
                count
                for stream, count in stream_counts.items()
                if stream in {"aura", "aura_cast"}
            ),
            "aura_duration_ms": None,
            "effect_magnitude": None,
        }

    reck = action_reports["warrior.recklessness"]
    minimum = reck["minimum_observed_successful_start_interval_ms"]
    if minimum is not None and minimum < 30 * 60 * 1000:
        thirty_minute = "CONTRADICTED_IF_NO_COOLDOWN_RESET_OR_EVENT_ALIAS"
    elif minimum is not None and minimum < 31 * 60 * 1000:
        thirty_minute = "CONSISTENT_WITH_NEAR_30_MINUTE_OBSERVATION_NOT_PROOF"
    else:
        thirty_minute = "NOT_DISCRIMINATED_BY_OBSERVED_SPACING"
    if minimum is not None and minimum < 5 * 60 * 1000:
        five_minute = "CONTRADICTED_IF_NO_COOLDOWN_RESET_OR_EVENT_ALIAS"
    elif minimum is not None and minimum < 6 * 60 * 1000:
        five_minute = "CONSISTENT_WITH_NEAR_5_MINUTE_OBSERVATION_NOT_PROOF"
    elif minimum is not None:
        five_minute = "NOT_FALSIFIED_BUT_NO_NEAR_5_MINUTE_REPEAT_OBSERVED"
    else:
        five_minute = "NOT_DISCRIMINATED_BY_OBSERVED_SPACING"

    return {
        "schema": "chronicle_fury_cooldown_evidence/v1",
        "input_scope": {
            "source": "manifest-selected historical_fury_expert_episodes/v1 partitions",
            "raw_chronicle_opened": False,
            "team_timeline_opened": False,
            "chronicle_external_state_event_partition_count": state_event_partition_count,
        },
        "observation_count": len(rows),
        "actions": action_reports,
        "recklessness_simulator_conflict_adjudication": {
            "observed_minimum_successful_start_interval_ms": minimum,
            "five_minute_comment": five_minute,
            "thirty_minute_code": thirty_minute,
            "practical_reading": (
                "the near-30-minute minimum repeat supports using the 30-minute "
                "implementation as the current evidence-leading candidate, but "
                "successful-cast spacing cannot prove cooldown readiness or "
                "strictly falsify the 5-minute comment"
            ),
            "twelve_vs_fifteen_second_aura_duration": "UNKNOWN_NO_AURA_END_EVENTS",
            "fifty_vs_one_hundred_percent_crit": "UNKNOWN_NO_CONTROLLED_STAT_OR_OUTCOME_COUNTERFACTUAL",
            "mechanics_change_authorized": False,
        },
        "claim_boundary": (
            "Chronicle START is a server-observed action proxy, GO is an outcome, "
            "and neither is a client request or cooldown-ready observation"
        ),
    }


def build_chronicle_fury_cooldown_evidence_v1(
    manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
) -> JSONMap:
    state_root = (
        Path(manifest_path).resolve().parents[2] / "chronicle_external_state_events"
    )
    state_partition_count = (
        sum(1 for path in state_root.rglob("*.jsonl.gz") if path.is_file())
        if state_root.is_dir()
        else 0
    )
    report = summarize_cooldown_observations_v1(
        iter_target_observations_v1(manifest_path),
        state_event_partition_count=state_partition_count,
    )
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    report["input_scope"]["episode_partition_count"] = len(manifest["partitions"])
    return report


__all__ = [
    "DEFAULT_EPISODE_MANIFEST",
    "TARGET_ACTIONS",
    "build_chronicle_fury_cooldown_evidence_v1",
    "iter_target_observations_v1",
    "summarize_cooldown_observations_v1",
]
