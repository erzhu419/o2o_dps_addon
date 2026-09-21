"""Read-only v7 initial-delay calibration summary over d900 held-out waves."""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    ABLATION_D, DELAY_UPPER_BOUNDS_MS, _actor_metadata, _delay_bucket,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from scripts.development_d900_v6_followup_plan_v1 import (
    COMPONENT, STAGE5_SHA, WAVE,
)

FOCAL = "0x0000000000576754"
CUTOFF_MS = 9098


def _wave_summary(record: dict) -> dict | None:
    provenance = record.get("raid_provenance", {})
    if provenance.get("contamination", {}).get("candidate_filter_passed") is not True:
        return None
    window = record["descriptive_outcome"]["reconstruction_binding"]["window"]
    start = window["start_offset_ms"]
    duration = window["context_duration_ms"]
    actors: dict[str, dict] = {}
    for row in record["players"]:
        actor = row["player"]["guid"]
        if actor == FOCAL or not row.get("exact_trace_indices"):
            continue
        metadata = _actor_metadata(row)
        actors[actor] = {
            "actor_guid": actor,
            "class": metadata["class"],
            "spec_key": metadata["spec_key"],
            "first_any_ms": None,
            "first_direct_hostile_start_ms": None,
            "first_direct_white6603_ms": None,
        }
    hostile_start_targets = set()
    first_hostile_start_ms = None
    for trace in record["exact_trace"]:
        if trace.get("trace_kind") != "EXACT_PLAYER_EVENT":
            continue
        actor = trace["player_guid"]
        time_ms = trace["anchor"]["offset_ms"] - start
        if time_ms < 0 or time_ms > duration:
            raise ValueError("exact player event outside reconstruction window")
        event = trace["event"]
        attribution = event["attribution"]
        target = event.get("target") or {}
        direct = (
            attribution.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
            and attribution.get("player_guid") == actor
        )
        hostile = target.get("lane") == "HOSTILE_CREATURE" and isinstance(target.get("guid"), str)
        if direct and hostile and event["event_type"] == "START":
            hostile_start_targets.add(target["guid"])
            first_hostile_start_ms = (
                time_ms if first_hostile_start_ms is None
                else min(first_hostile_start_ms, time_ms)
            )
        if actor not in actors:
            continue
        row = actors[actor]
        if row["first_any_ms"] is None:
            row["first_any_ms"] = time_ms
        if direct and hostile and event["event_type"] == "START":
            if row["first_direct_hostile_start_ms"] is None:
                row["first_direct_hostile_start_ms"] = time_ms
        if (
            direct and hostile and event["event_type"] == "DMG"
            and event.get("spell", {}).get("id") == 6603
            and row["first_direct_white6603_ms"] is None
        ):
            row["first_direct_white6603_ms"] = time_ms
    if not 2 <= len(hostile_start_targets) <= 6:
        return None
    if any(row["first_any_ms"] is None for row in actors.values()):
        raise ValueError("roster actor with exact trace indices has no exact event")
    return {
        "wave_id": record["wave"]["wave_id"],
        "start_offset_ms": start,
        "duration_ms": duration,
        "first_observed_direct_hostile_start_ms": first_hostile_start_ms,
        "direct_start_target_count": len(hostile_start_targets),
        "actors": list(actors.values()),
    }


def _quantiles(values: list[int]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": ordered[(len(ordered) - 1) // 4],
        "median": ordered[(len(ordered) - 1) // 2],
        "p75": ordered[3 * (len(ordered) - 1) // 4],
        "p90": ordered[9 * (len(ordered) - 1) // 10],
        "max": ordered[-1],
        "ge_9098_count": sum(value >= 9098 for value in ordered),
        "ge_14997_count": sum(value >= 14997 for value in ordered),
    }


def _model_distribution(distribution: tuple[int, tuple[tuple[int, int], ...]]) -> dict:
    support, choices = distribution
    buckets = []
    for bucket, count in choices:
        lower = 0 if bucket == 0 else DELAY_UPPER_BOUNDS_MS[bucket - 1] + 1
        upper = (
            DELAY_UPPER_BOUNDS_MS[bucket]
            if bucket < len(DELAY_UPPER_BOUNDS_MS)
            else DELAY_UPPER_BOUNDS_MS[-1] * 2
        )
        buckets.append({"bucket": bucket, "lower_ms": lower, "upper_ms": upper,
                        "count": count, "mass": count / support})
    def tail(threshold: int) -> float:
        return sum(
            row["mass"] * max(0, row["upper_ms"] - max(threshold, row["lower_ms"]) + 1)
            / (row["upper_ms"] - row["lower_ms"] + 1)
            for row in buckets
        )
    return {
        "context": ["GLOBAL", "WAVE_START"], "support": support,
        "buckets": buckets,
        "p_delay_ge_9098": tail(9098),
        "p_delay_ge_14997": tail(14997),
        "p_at_least_one_of_13_ge_14997_independent_draws": 1 - (1 - tail(14997)) ** 13,
        "p_at_least_one_of_18_ge_14997_independent_draws": 1 - (1 - tail(14997)) ** 18,
    }


def _score_initial_delays(waves: list[dict], model: object, d900_white_guids: set[str]) -> dict:
    def timing_state(alive_count: int) -> dict:
        return {
            "actor_last_mark_token": None,
            "marked_activity": {"other_team_including_unattributed": {
                "action_event_count_3000ms": 0, "damage_amount_3000ms": 0,
            }},
            "target_state": {"alive_target_count": alive_count},
        }

    score_rows = []
    for wave in waves:
        for actor in wave["actors"]:
            identity = {
                "player_guid": actor["actor_guid"], "class": actor["class"],
                "spec_key": actor["spec_key"],
            }
            bucket = _delay_bucket(actor["first_any_ms"])
            row = {
                "wave_id": wave["wave_id"], "actor_guid": actor["actor_guid"],
                "duration_ms": wave["duration_ms"],
                "first_any_ms": actor["first_any_ms"], "bucket": bucket,
            }
            for label, alive in (("old_nonzero_alive", 1), ("corrected_wave_start_alive0", 0)):
                context, distribution = model._select_context(
                    "delay_counts", identity, timing_state(alive)
                )
                support, choices = distribution
                count = dict(choices).get(bucket, 0)
                row[label] = {
                    "context_level": context[0], "context": list(context),
                    "support": support, "bucket_count": count,
                    "bucket_probability": count / support,
                    "bucket_nll": -math.log(count / support) if count else None,
                }
            score_rows.append(row)

    def aggregate(selected: list[dict]) -> dict:
        levels = {}
        scores = {}
        for label in ("old_nonzero_alive", "corrected_wave_start_alive0"):
            levels[label] = dict(sorted(Counter(row[label]["context_level"] for row in selected).items()))
            finite = [row[label]["bucket_nll"] for row in selected if row[label]["bucket_nll"] is not None]
            scores[label] = {
                "zero_bucket_probability_count": len(selected) - len(finite),
                "mean_bucket_nll_finite": sum(finite) / len(finite) if finite else None,
            }
        return {"actor_wave_count": len(selected), "context_level_counts": levels,
                "bucket_log_score": scores}

    d900_rows = [
        row for row in score_rows
        if row["wave_id"] == WAVE and row["actor_guid"] in d900_white_guids
    ]
    return {
        "old_condition": "hypothetical WAVE_START alive_target_count=1 (any nonzero bucket); matches d900 fallback, not a replay of every cohort runtime",
        "corrected_condition": "WAVE_START alive_target_count=0, as in training prefix; same held-out first-any event",
        "score_unit": "unsmoothed negative log probability of the observed delay bucket",
        "minimums": dict(model.minimums),
        "all_actor_waves": aggregate(score_rows),
        "long_wave_actor_waves": aggregate([
            row for row in score_rows if row["duration_ms"] >= 16000
        ]),
        "d900_early_white_actors": aggregate(d900_rows),
        "d900_actor_rows": d900_rows,
        "above_model_sampling_max_64000_count": sum(
            row["first_any_ms"] > 64000 for row in score_rows
        ),
    }


def summarize(waves: list[dict], model_distribution: dict, delay_scores: dict) -> dict:
    d900 = next((wave for wave in waves if wave["wave_id"] == WAVE), None)
    if d900 is None or len(waves) != 37:
        raise ValueError("held-out cohort is not the frozen 37-wave d900 cohort")
    d900_white = [
        row for row in d900["actors"]
        if row["first_direct_white6603_ms"] is not None
        and row["first_direct_white6603_ms"] <= CUTOFF_MS
    ]
    if len(d900_white) != 13:
        raise ValueError("d900 early direct-white actor count differs from 13")
    actor_waves = [row for wave in waves for row in wave["actors"]]
    long_actor_waves = [
        row for wave in waves if wave["duration_ms"] >= 16000
        for row in wave["actors"]
    ]
    white_actor_waves = [
        row for row in actor_waves if row["first_direct_white6603_ms"] is not None
    ]
    return {
        "schema": "development_d900_v7_initial_delay_calibration_audit/v1",
        "scope": "read-only held-out descriptive timing; no simulator or training mutation",
        "model_global_wave_start_delay": model_distribution,
        "same_event_old_vs_corrected_delay_bucket_score": delay_scores,
        "d900": {
            "wave_id": WAVE,
            "wave_duration_ms": d900["duration_ms"],
            "wave_start_offset_ms": d900["start_offset_ms"],
            "first_observed_direct_hostile_start_ms": d900["first_observed_direct_hostile_start_ms"],
            "early_direct_white_actor_count": len(d900_white),
            "first_any_ms": _quantiles([row["first_any_ms"] for row in d900_white]),
            "first_direct_white_ms": _quantiles([
                row["first_direct_white6603_ms"] for row in d900_white
            ]),
            "actors": d900_white,
        },
        "heldout_multi_start_cohort": {
            "wave_count": len(waves),
            "actor_wave_count_with_exact_event": len(actor_waves),
            "direct_white_actor_wave_count": len(white_actor_waves),
            "wave_duration_ms": _quantiles([wave["duration_ms"] for wave in waves]),
            "first_observed_direct_hostile_start_ms": _quantiles([
                wave["first_observed_direct_hostile_start_ms"] for wave in waves
            ]),
            "first_any_ms": _quantiles([row["first_any_ms"] for row in actor_waves]),
            "first_direct_white_ms": _quantiles([
                row["first_direct_white6603_ms"] for row in white_actor_waves
            ]),
            "waves_at_least_16000ms": sum(wave["duration_ms"] >= 16000 for wave in waves),
            "long_wave_actor_count": len(long_actor_waves),
            "long_wave_first_any_ms": _quantiles([
                row["first_any_ms"] for row in long_actor_waves
            ]),
        },
        "timing_conventions": {
            "first_any": "first EXACT_PLAYER_EVENT of any accepted attribution, relative to reconstruction window start",
            "first_white": "first direct friendly-player hostile DMG spell 6603, relative to same start",
            "observed_attackability_proxy": "first direct friendly-player hostile START across players; not a true client attackability flag",
            "first_actor_start": "first direct friendly-player hostile START for actor; white attacks may precede START",
            "censoring": "actor-wave rows require an observed exact event; short waves cannot reveal long first delays",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--runtime-store", type=Path, required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha,
        expected_model_content_sha256=args.model_sha,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA,
            component_id=COMPONENT,
            declared_held_out=True,
        ),
    )
    try:
        distribution = loaded.model._distribution("delay_counts", ("GLOBAL", "WAVE_START"))
        if distribution is None:
            raise ValueError("formal v7 store has no GLOBAL WAVE_START delay distribution")
        model_distribution = _model_distribution(distribution)
        waves = []
        with gzip.open(args.stage5, "rt", encoding="utf-8") as source:
            for line in source:
                wave = _wave_summary(json.loads(line))
                if wave is not None:
                    waves.append(wave)
        d900 = next((wave for wave in waves if wave["wave_id"] == WAVE), None)
        if d900 is None:
            raise ValueError("d900 wave absent")
        d900_white_guids = {
            row["actor_guid"] for row in d900["actors"]
            if row["first_direct_white6603_ms"] is not None
            and row["first_direct_white6603_ms"] <= CUTOFF_MS
        }
        delay_scores = _score_initial_delays(waves, loaded.model, d900_white_guids)
    finally:
        loaded.model.close()
    print(json.dumps(summarize(waves, model_distribution, delay_scores), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
