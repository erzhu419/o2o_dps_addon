"""Small descriptive CombatantInfo coverage audit for the 2026-09-20 raid."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_combatant_sidecar import decode_combatant_info_stream, sidecar_records
from o2o_dps.combatant_context_join import build_context_index

INSTANCE_ID = "0c735957-ffc1-49ea-a57a-a049d7a9857b"
RAW_MANIFEST = ROOT / "offline_data/chronicle_raw/external_api/v1/manifests/bc4f8e92fd9fcb7063207af1ec06ee2659e4d198c150a871653ec432c6356639.json"
STAGE4_MANIFEST = ROOT / "offline_data/derived/chronicle_external_team_timeline/v2/dev_descriptive_20260920_0c735/manifest.json"
OUTPUT = ROOT / "results/responsive-team-v4/dev-20260920-0c735-combatant-prefix-coverage.json"
ACTION_TYPES = frozenset({"START", "GO", "FAIL"})


def _row(guid: str, name: str, klass: str) -> dict:
    return {
        "player_guid": guid, "name": name, "class": klass,
        "raw_info_messages": 0, "raw_gear_messages": 0,
        "raw_talent_messages": 0, "raw_encounters": set(),
        "waves_with_player_event": 0, "first_event_prefix_matched": 0,
        "first_event_prefix_gear": 0, "first_event_prefix_talents": 0,
        "first_event_prefix_late_info_only": 0,
        "first_event_prefix_same_event_index": 0,
        "waves_with_action": 0, "first_action_prefix_matched": 0,
        "first_action_prefix_gear": 0, "first_action_prefix_talents": 0,
        "first_action_prefix_late_info_only": 0,
        "first_action_prefix_same_event_index": 0,
        "start_events": 0, "start_same_event_index_info": 0,
    }


def _probe(index: object, row: dict, wave: dict, event: dict, stem: str) -> None:
    anchor = event["anchor"]
    context, outcome = index.select(
        encounter_id=wave["encounter_id"], player_guid=row["player_guid"],
        start_event_index=anchor["event_index"],
    )
    if context is not None:
        row[f"{stem}_prefix_matched"] += 1
        row[f"{stem}_prefix_gear"] += int(context.gear_item_ids is not None)
        row[f"{stem}_prefix_talents"] += int(context.exact_talent_ranks is not None)
        row[f"{stem}_prefix_same_event_index"] += int(
            context.event_index == anchor["event_index"]
        )
    elif outcome == "late_info_only":
        row[f"{stem}_prefix_late_info_only"] += 1


def main() -> None:
    raw = json.loads(RAW_MANIFEST.read_text(encoding="utf-8"))
    source_instance = next(item for item in raw["instances"] if item["instance_id"] == INSTANCE_ID)
    object_path = ROOT / "offline_data/chronicle_raw/external_api/v1" / source_instance["streams"]["combatant_info"]["object"]["relative_path"]
    frames = decode_combatant_info_stream(object_path.read_bytes())
    records = sidecar_records(frames, instance_ref=INSTANCE_ID, slug=source_instance["slug"])
    index = build_context_index(records, instance_ref=INSTANCE_ID, source_artifact=str(object_path))
    info_index_keys = {
        (record["encounter_id"], record["player"]["guid"], record["anchor"]["event_index"])
        for record in records if record["anchor"] is not None
    }

    stage4 = json.loads(STAGE4_MANIFEST.read_text(encoding="utf-8"))
    stage4_instance = next(item for item in stage4["instances"] if item["instance_id"] == INSTANCE_ID)
    partition = STAGE4_MANIFEST.parent / stage4_instance["partition"]["path"]
    players = {}
    for record in records:
        player = record["player"]
        guid = player["guid"]
        current = players.setdefault(guid, _row(guid, player["name"], player["hero_class"]))
        current["raw_info_messages"] += 1
        current["raw_gear_messages"] += int(bool(record["gear"]))
        current["raw_talent_messages"] += int(record["talents"] is not None)
        current["raw_encounters"].add(record["encounter_id"])

    wave_count = 0
    with gzip.open(partition, "rt", encoding="utf-8") as source:
        for line in source:
            wave = json.loads(line)
            wave_count += 1
            for player_record in wave["players"]:
                meta = player_record["player"]
                current = players[meta["guid"]]
                events = player_record["timeline"]
                for event in events:
                    if event["event_type"] == "START":
                        current["start_events"] += 1
                        current["start_same_event_index_info"] += int(
                            (wave["encounter_id"], meta["guid"], event["anchor"]["event_index"])
                            in info_index_keys
                        )
                if events:
                    current["waves_with_player_event"] += 1
                    _probe(index, current, wave, events[0], "first_event")
                action = next((event for event in events if event["event_type"] in ACTION_TYPES), None)
                if action is not None:
                    current["waves_with_action"] += 1
                    _probe(index, current, wave, action, "first_action")

    output_players = []
    for current in sorted(players.values(), key=lambda row: row["player_guid"]):
        current["raw_encounters_with_info"] = len(current.pop("raw_encounters"))
        output_players.append(current)
    count_fields = [
        key for key in output_players[0]
        if key not in {"player_guid", "name", "class", "raw_encounters_with_info"}
    ]
    totals = {key: sum(row[key] for row in output_players) for key in count_fields}
    totals["player_encounter_pairs_with_info"] = sum(
        row["raw_encounters_with_info"] for row in output_players
    )
    result = {
        "schema": "development_20260920_combatant_prefix_coverage/v1",
        "scope": "DESCRIPTIVE_ONLY_UNKNOWN_NONVOTING_NOT_FORMAL_TRAIN_OR_COMPARISON",
        "instance_id": INSTANCE_ID,
        "source": {
            "raw_combatant_info_object": str(object_path.relative_to(ROOT)),
            "stage4_wave_partition": str(partition.relative_to(ROOT)),
            "prefix_rule": "same encounter and exact player GUID; latest (EventMeta.index, message ordinal) <= first event/action EventMeta.index",
            "action_event_types": sorted(ACTION_TYPES),
        },
        "wave_count": wave_count, "player_count": len(output_players),
        "raw_encounter_frame_count": len(frames),
        "totals": totals,
        "players": output_players,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "wave_count": wave_count,
                      "player_count": len(output_players), "totals": result["totals"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
