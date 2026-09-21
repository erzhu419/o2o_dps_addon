"""Read-only held-out first-hostile-intent coverage for white swing 6603."""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.chronicle_external_teammate_response_model_v1 import iter_wave_response_rows_v1


FOCAL = "0x0000000000576754"
WHITE_SWING_SPELL_ID = 6603


def summarize_wave_rows_v1(
    rows: Iterable[Mapping[str, Any]], *, focal_guid: str = FOCAL,
) -> dict[str, Any]:
    first_hostile_intent: dict[str, str] = {}
    prior_direct_start_any: set[str] = set()
    prior_direct_start_hostile: set[str] = set()
    later_start_after_white: dict[str, dict[str, Any]] = {}
    white_first: list[dict[str, Any]] = []
    event_actor_guids: set[str] = set()
    for row in rows:
        actor = row["actor"]["player_guid"]
        if actor == focal_guid:
            continue
        event_actor_guids.add(actor)
        label = row["label"]
        event_type = label["event_type"]
        direct = (
            label.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
            and label.get("source_lane") == "FRIENDLY_PLAYER"
        )
        hostile = label.get("target_lane") == "HOSTILE_CREATURE" and isinstance(label.get("target_guid"), str)
        direct_start = direct and event_type == "START"
        direct_hostile_start = direct_start and hostile
        direct_white_hit = (
            direct and hostile and event_type == "DMG"
            and label.get("spell_id") == WHITE_SWING_SPELL_ID
        )
        if direct_hostile_start and first_hostile_intent.get(actor) == "WHITE_6603_DMG":
            later_start_after_white.setdefault(actor, {
                "trace_index": row["trace_index"],
                "time_ms": row["emission_state_before_current_event"]["wave_elapsed_ms"],
                "target_guid": label["target_guid"],
            })
        if actor not in first_hostile_intent and (direct_hostile_start or direct_white_hit):
            if direct_hostile_start:
                first_hostile_intent[actor] = "DIRECT_START"
            else:
                first_hostile_intent[actor] = "WHITE_6603_DMG"
                state = row["emission_state_before_current_event"]
                candidates = state["target_state"]["target_choice_candidates"]
                selected = label["target_guid"]
                eligible = {item["target_guid"] for item in candidates}
                white_first.append({
                    "actor_guid": actor,
                    "trace_index": row["trace_index"],
                    "time_ms": state["wave_elapsed_ms"],
                    "target_guid": selected,
                    "target_mode": label.get("target_mode"),
                    "prior_direct_start_any": actor in prior_direct_start_any,
                    "prior_direct_start_hostile": actor in prior_direct_start_hostile,
                    "prior_model_target_guid": state.get("actor_last_target_guid"),
                    "prefix_candidate_count": len(candidates),
                    "label_prefix_candidate": selected in eligible,
                    "strict_prefix_multi_choice": (
                        selected in eligible and len(candidates) >= 2
                        and label.get("target_mode") in {"STAY_ALIVE", "SWITCH_ALIVE"}
                    ),
                })
        if direct_start:
            prior_direct_start_any.add(actor)
            if hostile:
                prior_direct_start_hostile.add(actor)
    return {
        "event_actor_guids": sorted(event_actor_guids),
        "first_hostile_intent_by_actor": first_hostile_intent,
        "white_first": white_first,
        "first_later_direct_hostile_start_after_white_by_actor": later_start_after_white,
    }


def analyze_stage5_v1(stage5: str | Path, cohort_json: str | Path) -> dict[str, Any]:
    cohort = json.loads(Path(cohort_json).read_text(encoding="utf-8"))
    wanted = set(cohort["cohort_wave_ids"])
    wave_count = 0
    actor_wave_pairs: set[tuple[str, str]] = set()
    first_start_pairs: set[tuple[str, str]] = set()
    first_white_pairs: set[tuple[str, str]] = set()
    primary_white_pairs: set[tuple[str, str]] = set()
    multi_white_pairs: set[tuple[str, str]] = set()
    primary_multi_white_pairs: set[tuple[str, str]] = set()
    later_start_pairs: set[tuple[str, str]] = set()
    later_start_details: dict[tuple[str, str], dict[str, Any]] = {}
    primary_later_start_pairs: set[tuple[str, str]] = set()
    primary_multi_later_start_pairs: set[tuple[str, str]] = set()
    white_rows = []
    per_wave_counts = []
    for line in gzip.open(stage5, "rt", encoding="utf-8"):
        record = json.loads(line)
        wave_id = record.get("wave", {}).get("wave_id")
        if wave_id not in wanted:
            continue
        wave_count += 1
        summary = summarize_wave_rows_v1(iter_wave_response_rows_v1(record, copy_rows=False))
        per_wave_counts.append({
            "wave_id": wave_id,
            "first_hostile_intent_white_6603": len(summary["white_first"]),
            "first_white_without_earlier_direct_start_any": sum(
                not row["prior_direct_start_any"] for row in summary["white_first"]
            ),
            "first_white_strict_prefix_multi_choice": sum(
                row["strict_prefix_multi_choice"] for row in summary["white_first"]
            ),
            "first_white_without_earlier_start_and_multi_choice": sum(
                not row["prior_direct_start_any"] and row["strict_prefix_multi_choice"]
                for row in summary["white_first"]
            ),
        })
        actor_wave_pairs.update((wave_id, guid) for guid in summary["event_actor_guids"])
        for guid, intent in summary["first_hostile_intent_by_actor"].items():
            pair = wave_id, guid
            if intent == "DIRECT_START":
                first_start_pairs.add(pair)
            else:
                first_white_pairs.add(pair)
        for guid, detail in summary["first_later_direct_hostile_start_after_white_by_actor"].items():
            pair = wave_id, guid
            later_start_pairs.add(pair)
            later_start_details[pair] = detail
        for row in summary["white_first"]:
            pair = wave_id, row["actor_guid"]
            if not row["prior_direct_start_any"]:
                primary_white_pairs.add(pair)
            if row["strict_prefix_multi_choice"]:
                multi_white_pairs.add(pair)
                if not row["prior_direct_start_any"]:
                    primary_multi_white_pairs.add(pair)
            white_rows.append({"wave_id": wave_id, **row})
    primary_later_start_pairs = primary_white_pairs & later_start_pairs
    primary_multi_later_start_pairs = primary_multi_white_pairs & later_start_pairs
    first_white_by_pair = {(row["wave_id"], row["actor_guid"]): row for row in white_rows}
    primary_multi_later_same = sum(
        first_white_by_pair[pair]["target_guid"] == later_start_details[pair]["target_guid"]
        for pair in primary_multi_later_start_pairs
    )
    primary_multi_by_player = Counter(guid for _, guid in primary_multi_white_pairs)
    if wave_count != len(wanted):
        raise ValueError(f"cohort wave coverage differs: {wave_count} != {len(wanted)}")
    return {
        "schema": "development_d900_white_swing_first_target_coverage/v1",
        "status": "HELDOUT_DESCRIPTIVE_NOT_TRAINING_LABEL",
        "cohort_wave_count": wave_count,
        "teammate_actor_wave_pairs_with_events": len(actor_wave_pairs),
        "teammate_unique_player_guids_with_events": len({guid for _, guid in actor_wave_pairs}),
        "first_hostile_intent_direct_start_actor_wave_pairs": len(first_start_pairs),
        "first_hostile_intent_white_6603_actor_wave_pairs": len(first_white_pairs),
        "first_white_without_earlier_direct_start_any_actor_wave_pairs": len(primary_white_pairs),
        "first_white_strict_prefix_multi_choice_actor_wave_pairs": len(multi_white_pairs),
        "first_white_without_earlier_start_and_multi_choice_actor_wave_pairs": len(primary_multi_white_pairs),
        "first_white_unique_player_guids": len({guid for _, guid in first_white_pairs}),
        "first_white_without_earlier_start_unique_player_guids": len(
            {guid for _, guid in primary_white_pairs}
        ),
        "first_white_label_prefix_visible_actor_wave_pairs": sum(
            row["label_prefix_candidate"] for row in white_rows
        ),
        "first_white_then_later_direct_hostile_start_actor_wave_pairs": len(later_start_pairs),
        "first_white_without_earlier_start_then_later_direct_hostile_start_actor_wave_pairs": len(
            primary_later_start_pairs
        ),
        "first_white_without_earlier_start_multi_choice_then_later_direct_hostile_start_actor_wave_pairs": len(
            primary_multi_later_start_pairs
        ),
        "first_white_without_earlier_start_multi_choice_later_start_same_target_actor_wave_pairs": primary_multi_later_same,
        "first_white_without_earlier_start_multi_choice_later_start_other_target_actor_wave_pairs": (
            len(primary_multi_later_start_pairs) - primary_multi_later_same
        ),
        "first_white_without_earlier_start_multi_choice_repeat_counts_by_player": dict(
            sorted(primary_multi_by_player.items())
        ),
        "per_wave_counts": per_wave_counts,
        "first_white_examples": white_rows[:20],
        "white_6603_event_definition": {
            "event_type": "DMG",
            "spell_id": WHITE_SWING_SPELL_ID,
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "source_lane": "FRIENDLY_PLAYER",
            "target_lane": "HOSTILE_CREATURE",
            "focal_player_excluded": FOCAL,
        },
        "boundary": (
            "6603 DMG is a landed swing result, not the time the swing target was chosen; "
            "its strict-prefix candidates are diagnostic only and cannot be copied "
            "as a decision-time training label without a causal timing bridge"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--cohort-json", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze_stage5_v1(args.stage5, args.cohort_json), ensure_ascii=False))


if __name__ == "__main__":
    main()
