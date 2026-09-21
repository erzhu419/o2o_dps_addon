"""Materialize one selected Chronicle raid as time-aligned Fury guide evidence.

This is an observational input slice, not a trained policy. In particular a
SpellStart is an attempted action, a SpellGo can be a triggered result, and
damage on a second target does not establish a player target switch.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .chronicle_combatant_sidecar import decode_combatant_info_stream
from .chronicle_external_event_normalizer_v1 import decode_event_stream
from .chronicle_external_historical_fury_policy_v3 import classify_action_event


SCHEMA = "chronicle_selected_fury_evidence/v1"
ROW_SCHEMA = "chronicle_selected_fury_encounter_player/v1"
REQUIRED_STREAMS = ("combatant_info", "spell_start", "spell_go", "damage")


def _event_time(frame: Mapping[str, Any], event: Mapping[str, Any]) -> int:
    return int(frame["first_timestamp_ms"]) + int(event["meta"]["offset_ms"])


def _action_row(frame: Mapping[str, Any], event: Mapping[str, Any], kind: str) -> dict[str, Any]:
    spell = event.get("spell_data") or {}
    classification = classify_action_event(
        event_type="START" if kind == "starts" else "GO", spell=spell
    )
    return {
        "time_ms": _event_time(frame, event),
        "event_index": event["meta"]["event_index"],
        "spell_id": spell.get("id"),
        "spell_name": spell.get("name"),
        "item_id": event.get("item_id"),
        "target_guid": event.get("target"),
        "role": classification.role,
        "action_key": classification.action_key,
    }


def build_selected_fury_rows_v1(
    *,
    instance_id: str,
    metadata: Mapping[str, Any],
    fury_players: list[Mapping[str, Any]],
    frames: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join four already-decoded streams by exact encounter and player GUID."""

    players = {str(player["guid"]).casefold(): player for player in fury_players}
    if len(players) != len(fury_players) or not players:
        raise ValueError("selected Fury GUIDs must be nonempty and unique")
    encounters = {row["id"]: row for row in metadata["encounters"]}
    if not encounters:
        raise ValueError("raid metadata has no encounters")
    buckets = {
        (encounter_id, guid): {
            "schema": ROW_SCHEMA,
            "instance_id": instance_id,
            "encounter_id": encounter_id,
            "player_guid": players[guid]["guid"],
            "combatant_info_snapshots": [],
            "starts": [],
            "go_events": [],
            "positive_hostile_damage": [],
        }
        for encounter_id in encounters
        for guid in players
    }
    hostile_guids = {
        encounter_id: {str(hostile["id"]).casefold() for hostile in encounter.get("hostiles", [])}
        for encounter_id, encounter in encounters.items()
    }

    for stream in REQUIRED_STREAMS:
        for frame in frames[stream]:
            encounter_id = frame["encounter_id"]
            if encounter_id not in encounters:
                raise ValueError(f"{stream} references an unknown encounter {encounter_id}")
            for wrapped in frame["messages"]:
                event = wrapped.get("event", wrapped)
                guid = str(event.get("guid") if stream == "combatant_info" else event.get("caster") or "").casefold()
                bucket = buckets.get((encounter_id, guid))
                if bucket is None:
                    continue
                if stream == "combatant_info":
                    bucket["combatant_info_snapshots"].append({
                        "time_ms": _event_time(frame, event),
                        "event_index": event["meta"]["event_index"],
                        "race": event["race"],
                        "gear": event["gear"],
                        "talents": event["talents"],
                    })
                elif stream == "spell_start":
                    bucket["starts"].append(_action_row(frame, event, "starts"))
                elif stream == "spell_go":
                    bucket["go_events"].append(_action_row(frame, event, "go_events"))
                else:
                    target = str(event.get("target") or "").casefold()
                    if target in hostile_guids[encounter_id] and event.get("amount", 0) > 0:
                        spell = event.get("spell_data") or {}
                        bucket["positive_hostile_damage"].append({
                            "time_ms": _event_time(frame, event),
                            "event_index": event["meta"]["event_index"],
                            "target_guid": event["target"],
                            "spell_id": spell.get("id"),
                            "spell_name": spell.get("name"),
                            "amount": event["amount"],
                        })

    rows = list(buckets.values())
    for row in rows:
        for field in ("combatant_info_snapshots", "starts", "go_events", "positive_hostile_damage"):
            row[field].sort(key=lambda event: (event["time_ms"], event["event_index"]))
        snapshots = row["combatant_info_snapshots"]
        next_snapshot = 0
        latest_snapshot: int | None = None
        for action in row["starts"]:
            action_order = (action["time_ms"], action["event_index"])
            while next_snapshot < len(snapshots):
                snapshot = snapshots[next_snapshot]
                if (snapshot["time_ms"], snapshot["event_index"]) > action_order:
                    break
                latest_snapshot = next_snapshot
                next_snapshot += 1
            action["causal_info_snapshot_ordinal"] = latest_snapshot
    talent_matches = {}
    for guid, player in players.items():
        snapshots = [
            snapshot
            for row in rows if row["player_guid"].casefold() == guid
            for snapshot in row["combatant_info_snapshots"]
        ]
        talent_matches[player["guid"]] = any(
            snapshot["talents"] is not None
            and "}".join(snapshot["talents"]["trees"]) == player["talent_layout"]
            for snapshot in snapshots
        )
    metrics = {
        "encounter_count": len(encounters),
        "ranked_fury_count": len(players),
        "encounter_player_rows": len(rows),
        "rows_with_start": sum(bool(row["starts"]) for row in rows),
        "rows_with_combatant_info": sum(bool(row["combatant_info_snapshots"]) for row in rows),
        "combatant_info_snapshots": sum(len(row["combatant_info_snapshots"]) for row in rows),
        "start_events": sum(len(row["starts"]) for row in rows),
        "start_events_with_prior_info_snapshot": sum(
            action["causal_info_snapshot_ordinal"] is not None
            for row in rows for action in row["starts"]
        ),
        "go_events": sum(len(row["go_events"]) for row in rows),
        "positive_hostile_damage_events": sum(len(row["positive_hostile_damage"]) for row in rows),
        "leaderboard_talent_layout_matches_any_info_snapshot_by_guid": talent_matches,
    }
    return rows, metrics


def materialize_selected_fury_evidence_v1(
    *, raw_manifest_path: Path, candidate_manifest_path: Path, instance_id: str,
    data_root: Path, output_dir: Path,
) -> dict[str, Any]:
    raw = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    matching = [row for row in raw["instances"] if row["instance_id"] == instance_id]
    candidate_matching = [row for row in candidate["raids"] if row["instance_id"] == instance_id]
    if len(matching) != 1 or len(candidate_matching) != 1:
        raise ValueError("instance must occur once in each source manifest")
    instance = matching[0]
    selected = candidate_matching[0]
    raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
    streams = instance["streams"]
    if any(streams.get(name, {}).get("status") != "AVAILABLE" for name in REQUIRED_STREAMS):
        raise ValueError("selected instance lacks a required event stream")
    metadata = json.loads((raw_root / instance["metadata"]["object"]["relative_path"]).read_text(encoding="utf-8"))
    frames: dict[str, list[dict[str, Any]]] = {}
    for stream in REQUIRED_STREAMS:
        payload = (raw_root / streams[stream]["object"]["relative_path"]).read_bytes()
        frames[stream] = (
            decode_combatant_info_stream(payload) if stream == "combatant_info"
            else decode_event_stream(payload, stream_type=stream)
        )
    rows, metrics = build_selected_fury_rows_v1(
        instance_id=instance_id, metadata=metadata,
        fury_players=selected["fury_players"], frames=frames,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    slice_path = output_dir / f"{instance_id}.jsonl.gz"
    content = b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for row in rows
    )
    slice_path.write_bytes(gzip.compress(content, mtime=0))
    receipt = {
        "schema": SCHEMA,
        "cohort": "separate_development_postfix_incremental",
        "instance_id": instance_id,
        "started_at": selected["started_at"],
        "uploaded_at": selected["uploaded_at"],
        "raw_manifest": raw_manifest_path.as_posix(),
        "candidate_manifest": candidate_manifest_path.as_posix(),
        "slice": slice_path.as_posix(),
        "slice_gzip_bytes": slice_path.stat().st_size,
        "metrics": metrics,
        "contract": {
            "start_is_attempt_not_success": True,
            "go_may_be_triggered_result_not_player_decision": True,
            "splash_damage_is_not_target_switch_intent": True,
            "info_snapshots_preserve_within_encounter_build_changes": True,
            "causal_build_lookup": "last same-encounter INFO snapshot whose (time_ms,event_index) does not exceed START; otherwise unknown",
            "frozen_union_modified": False,
            "model_training_performed": False,
        },
    }
    (output_dir / f"{instance_id}.manifest.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-manifest", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(materialize_selected_fury_evidence_v1(
        raw_manifest_path=args.raw_manifest,
        candidate_manifest_path=args.candidate_manifest,
        instance_id=args.instance_id,
        data_root=args.data_root,
        output_dir=args.output_dir,
    ), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
