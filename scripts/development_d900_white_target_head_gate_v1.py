"""Held-out strict-prefix candidate gate for landed white-swing target labels."""

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
SPELL_ID = 6603


def summarize_wave_rows_v1(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    uniform_sum = {"FIRST_ACQUISITION": 0.0, "RETARGET": 0.0}
    first_white_actors: set[str] = set()
    for row in rows:
        actor = row["actor"]["player_guid"]
        label = row["label"]
        if actor == FOCAL or not (
            label.get("event_type") == "DMG"
            and label.get("spell_id") == SPELL_ID
            and label.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
            and label.get("source_lane") == "FRIENDLY_PLAYER"
            and label.get("target_lane") == "HOSTILE_CREATURE"
            and isinstance(label.get("target_guid"), str)
        ):
            continue
        counts["direct_hostile_white_6603"] += 1
        state = row["emission_state_before_current_event"]
        selected = label["target_guid"]
        previous = state.get("actor_last_target_guid")
        candidates = state["target_state"]["target_choice_candidates"]
        eligible = {item["target_guid"] for item in candidates}
        visible = selected in eligible
        if actor not in first_white_actors:
            first_white_actors.add(actor)
            counts["first_white_actor_wave"] += 1
            counts["first_white_target_prefix_visible" if visible
                   else "first_white_target_not_prefix_visible"] += 1
        if previous == selected:
            counts["stay_deterministic"] += 1
            continue
        phase = "FIRST_ACQUISITION" if previous is None else "RETARGET"
        counts[f"{phase}_all"] += 1
        if not visible:
            counts[f"{phase}_target_not_prefix_visible"] += 1
            continue
        if label.get("target_mode") not in {"STAY_ALIVE", "SWITCH_ALIVE"}:
            counts[f"{phase}_non_alive_or_unknown_mode"] += 1
            continue
        alternatives = eligible - ({previous} if previous is not None else set())
        if len(alternatives) < 2:
            counts[f"{phase}_singleton_or_zero"] += 1
            continue
        counts[f"{phase}_strict_prefix_multi_choice"] += 1
        counts[f"{phase}_candidate_count_{len(alternatives)}"] += 1
        uniform_sum[phase] += 1.0 / len(alternatives)
    return {"counts": dict(counts), "uniform_expected_hit_sum": uniform_sum}


def analyze_stage5_v1(stage5: str | Path, cohort_json: str | Path) -> dict[str, Any]:
    cohort = json.loads(Path(cohort_json).read_text(encoding="utf-8"))
    wanted = set(cohort["cohort_wave_ids"])
    wave_count = 0
    combined: Counter[str] = Counter()
    chance = {"FIRST_ACQUISITION": 0.0, "RETARGET": 0.0}
    for line in gzip.open(stage5, "rt", encoding="utf-8"):
        record = json.loads(line)
        if record.get("wave", {}).get("wave_id") not in wanted:
            continue
        wave_count += 1
        summary = summarize_wave_rows_v1(iter_wave_response_rows_v1(record, copy_rows=False))
        combined.update(summary["counts"])
        for phase in chance:
            chance[phase] += summary["uniform_expected_hit_sum"][phase]
    if wave_count != len(wanted):
        raise ValueError(f"cohort wave coverage differs: {wave_count} != {len(wanted)}")
    phases = {}
    for phase in chance:
        denominator = combined[f"{phase}_strict_prefix_multi_choice"]
        phases[phase] = {
            "all_changed_target_white_hits": combined[f"{phase}_all"],
            "strict_prefix_multi_choice": denominator,
            "target_not_prefix_visible": combined[f"{phase}_target_not_prefix_visible"],
            "singleton_or_zero": combined[f"{phase}_singleton_or_zero"],
            "uniform_expected_hit_probability": chance[phase] / denominator if denominator else None,
            "uniform_expected_hits": chance[phase],
        }
    return {
        "schema": "development_d900_white_target_head_gate/v1",
        "status": "HELDOUT_GATE_ONLY_NOT_TRAINED",
        "cohort_wave_count": wave_count,
        "direct_hostile_white_6603_hits": combined["direct_hostile_white_6603"],
        "first_white_actor_wave": combined["first_white_actor_wave"],
        "first_white_target_not_prefix_visible": combined["first_white_target_not_prefix_visible"],
        "first_white_target_not_prefix_visible_fraction": (
            combined["first_white_target_not_prefix_visible"] / combined["first_white_actor_wave"]
            if combined["first_white_actor_wave"] else None
        ),
        "stay_deterministic_hits": combined["stay_deterministic"],
        "phases": phases,
        "counts": dict(sorted(combined.items())),
        "label_contract": (
            "DIRECT_FRIENDLY_PLAYER hostile DMG spell 6603, focal excluded; "
            "at DMG time only, strict-prefix activity-visible alive candidates, "
            "previous target excluded for RETARGET; singleton and unseen labels omitted"
        ),
        "boundary": "Landed white DMG cannot retroactively supervise earlier swing target selection",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--cohort-json", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze_stage5_v1(args.stage5, args.cohort_json), ensure_ascii=False))


if __name__ == "__main__":
    main()
