"""Held-out 6603 mark/delay calibration at historical event opportunities."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from scripts.development_d900_v6_followup_plan_v1 import COMPONENT, STAGE5_SHA, WAVE
from scripts.development_d900_v7_initial_delay_calibration_audit_v1 import _wave_summary

FOCAL = "0x0000000000576754"
CUTOFF_MS = 9098
HOSTILE_MODES = frozenset({"STAY_ALIVE", "SWITCH_ALIVE"})


def _white_mark_probabilities(model: object, actor: dict, state: dict) -> tuple[float, float, str]:
    """Use the exact actionable context backoff, without drawing or applying."""

    for context in response._context_keys(actor, state, model.variant_id):
        projection = model._actionable_projection(context)
        if (
            projection is None
            or projection.mark_support < model.minimums[context[0]]
            or projection.actionable_support <= 0
        ):
            continue
        white = positive = 0
        for (token, mode, damage_bucket, _joint_support), count in projection.choices:
            event_type, spell_id, attribution = json.loads(token)
            if (
                event_type == "DMG" and spell_id == 6603
                and attribution == "DIRECT_FRIENDLY_PLAYER" and mode in HOSTILE_MODES
            ):
                white += count
                if damage_bucket > 0:
                    positive += count
        return white / projection.actionable_support, positive / projection.actionable_support, context[0]
    raise ValueError("formal store has no actionable mark context")


def _compact_rows(record: dict, model: object) -> list[dict]:
    rows = []
    actor_time_ms: dict[str, int] = {}
    for row in response.iter_wave_response_sufficient_rows_v1(record):
        actor = row["actor"]
        if actor["player_guid"] == FOCAL:
            continue
        state = row["emission_state_before_current_event"]
        label = row["label"]
        guid = actor["player_guid"]
        if label["delay_origin"] == "WAVE_START":
            if guid in actor_time_ms:
                raise ValueError("actor repeats WAVE_START delay origin")
            time_ms = label["inter_event_delay_ms"]
        else:
            if guid not in actor_time_ms:
                raise ValueError("actor inter-event delay lacks WAVE_START origin")
            time_ms = actor_time_ms[guid] + label["inter_event_delay_ms"]
        actor_time_ms[guid] = time_ms
        p_white, p_positive, mark_level = _white_mark_probabilities(model, actor, state)
        delay_context, delay_distribution = model._select_context(
            "delay_counts", actor, row["timing_state_after_previous_actor_event"]
        )
        bucket = response._delay_bucket(label["inter_event_delay_ms"])
        support, choices = delay_distribution
        bucket_count = dict(choices).get(bucket, 0)
        observed_white = (
            label["event_type"] == "DMG" and label["spell_id"] == 6603
            and label["attribution_kind"] == "DIRECT_FRIENDLY_PLAYER"
            and label["target_lane"] == "HOSTILE_CREATURE"
        )
        rows.append({
            "actor_guid": actor["player_guid"], "time_ms": time_ms,
            "actor_class": actor["class"], "actor_spec_key": actor["spec_key"],
            "p_direct_hostile_white6603": p_white,
            "p_direct_hostile_positive_raw_white6603": p_positive,
            "observed_direct_hostile_white6603": observed_white,
            "observed_positive_raw_white6603": observed_white and label["damage_amount"] > 0,
            "mark_context_level": mark_level,
            "delay_context_level": delay_context[0],
            "delay_origin": label["delay_origin"],
            "delay_bucket_nll": -math.log(bucket_count / support) if bucket_count else None,
        })
    return rows


def _aggregate(rows: list[dict], *, cutoff_ms: int | None) -> dict:
    chosen = [row for row in rows if cutoff_ms is None or row["time_ms"] <= cutoff_ms]
    actor_rows: dict[str, dict] = {}
    survival = 1.0
    first_weighted_time = 0.0
    first_historical_ms = None
    mark_levels = Counter()
    delay_levels = Counter()
    delay_nll = []
    zero_delay_bucket = 0
    first_delay_nll = []
    later_delay_nll = []
    for row in chosen:
        actor = actor_rows.setdefault(row["actor_guid"], {
            "actor_guid": row["actor_guid"], "event_opportunities": 0,
            "observed_white6603": 0, "observed_positive_raw_white6603": 0,
            "expected_white6603": 0.0, "expected_positive_raw_white6603": 0.0,
            "first_observed_white6603_ms": None, "first_any_ms": row["time_ms"],
            "no_white_probability": 1.0,
        })
        actor["event_opportunities"] += 1
        actor["expected_white6603"] += row["p_direct_hostile_white6603"]
        actor["expected_positive_raw_white6603"] += row["p_direct_hostile_positive_raw_white6603"]
        actor["no_white_probability"] *= 1 - row["p_direct_hostile_white6603"]
        if row["observed_direct_hostile_white6603"]:
            actor["observed_white6603"] += 1
            if actor["first_observed_white6603_ms"] is None:
                actor["first_observed_white6603_ms"] = row["time_ms"]
            if first_historical_ms is None:
                first_historical_ms = row["time_ms"]
        if row["observed_positive_raw_white6603"]:
            actor["observed_positive_raw_white6603"] += 1
        first_weighted_time += survival * row["p_direct_hostile_white6603"] * row["time_ms"]
        survival *= 1 - row["p_direct_hostile_white6603"]
        mark_levels[row["mark_context_level"]] += 1
        delay_levels[row["delay_context_level"]] += 1
        nll = row["delay_bucket_nll"]
        if nll is None:
            zero_delay_bucket += 1
        else:
            delay_nll.append(nll)
            (first_delay_nll if row["delay_origin"] == "WAVE_START" else later_delay_nll).append(nll)
    actors = sorted(actor_rows.values(), key=lambda row: row["actor_guid"])
    for actor in actors:
        actor["p_any_white6603_at_historical_opportunities"] = 1 - actor.pop("no_white_probability")
        actor["white_count_residual_observed_minus_expected"] = (
            actor["observed_white6603"] - actor["expected_white6603"]
        )
    most_over = sorted(actors, key=lambda row: -row["white_count_residual_observed_minus_expected"])[:3]
    most_under = sorted(actors, key=lambda row: row["white_count_residual_observed_minus_expected"])[:3]
    white_probability = 1 - survival
    return {
        "event_opportunities": len(chosen),
        "actor_count_with_opportunity": len(actors),
        "observed_direct_white6603_emitted_raw": sum(row["observed_white6603"] for row in actors),
        "observed_direct_white6603_positive_raw": sum(row["observed_positive_raw_white6603"] for row in actors),
        "expected_direct_white6603_mark_at_observed_opportunities": sum(row["expected_white6603"] for row in actors),
        "expected_direct_white6603_positive_raw_mark_at_observed_opportunities": sum(
            row["expected_positive_raw_white6603"] for row in actors
        ),
        "observed_actor_count_with_white6603": sum(row["observed_white6603"] > 0 for row in actors),
        "expected_actor_count_with_white6603_at_observed_opportunities": sum(
            row["p_any_white6603_at_historical_opportunities"] for row in actors
        ),
        "observed_first_direct_white6603_ms": first_historical_ms,
        "p_any_first_white6603_at_observed_opportunities": white_probability,
        "expected_first_white6603_ms_conditional_on_any_at_observed_opportunities": (
            first_weighted_time / white_probability if white_probability > 0 else None
        ),
        "mark_context_levels": dict(sorted(mark_levels.items())),
        "delay_context_levels": dict(sorted(delay_levels.items())),
        "delay_bucket_score": {
            "zero_probability_count": zero_delay_bucket,
            "mean_nll_finite": sum(delay_nll) / len(delay_nll) if delay_nll else None,
            "first_any_mean_nll_finite": sum(first_delay_nll) / len(first_delay_nll) if first_delay_nll else None,
            "after_event_mean_nll_finite": sum(later_delay_nll) / len(later_delay_nll) if later_delay_nll else None,
            "first_any_count_finite": len(first_delay_nll),
            "after_event_count_finite": len(later_delay_nll),
        },
        "top_three_observed_excess_actors": most_over,
        "top_three_model_excess_actors": most_under,
        "all_actor_rows": actors,
    }


def evaluate(stage5: Path, model: object, *, only_wave: str | None) -> dict:
    waves = []
    with gzip.open(stage5, "rt", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            wave = _wave_summary(record)
            if wave is None or (only_wave is not None and wave["wave_id"] != only_wave):
                continue
            compact = _compact_rows(record, model)
            full = _aggregate(compact, cutoff_ms=None)
            early = _aggregate(compact, cutoff_ms=CUTOFF_MS)
            if wave["wave_id"] != WAVE:
                full.pop("all_actor_rows")
                early.pop("all_actor_rows")
            waves.append({
                "wave_id": wave["wave_id"],
                "context_duration_ms": wave["duration_ms"],
                "first_observed_direct_hostile_start_ms": wave["first_observed_direct_hostile_start_ms"],
                "direct_start_target_count": wave["direct_start_target_count"],
                "full_window": full,
                "through_9098ms": early,
            })
    if only_wave is None and len(waves) != 37:
        raise ValueError("held-out multi-target cohort differs from frozen 37 waves")
    if only_wave is not None and len(waves) != 1:
        raise ValueError("requested held-out smoke wave absent or not in cohort")
    return {
        "schema": "development_d900_v7_white6603_conditional_audit/v1",
        "scope": "held-out historical-prefix conditional mark/delay calibration, not free-running simulation",
        "cohort_rule": "clean raid candidate; 2-6 direct friendly-player hostile START target GUIDs",
        "wave_count": len(waves),
        "wave_ids": [row["wave_id"] for row in waves],
        "raw_vs_sim_boundary": {
            "historical_raw": "EXACT_PLAYER_EVENT direct DMG 6603; positive means raw damage.amount > 0",
            "predicted": "v7 D-store actionable mark and damage-bucket probabilities at historical strict-prefix event opportunities; no HP application",
            "sim_applied": "not evaluated; bridge target HP, attackability, cancellation and kill-clock are absent",
            "first_white": "predicted hazard conditional on historical opportunity timestamps and prefixes, not a free-running first-swing clock",
        },
        "waves": waves,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--runtime-store", type=Path, required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--only-wave")
    args = parser.parse_args()
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha,
        expected_model_content_sha256=args.model_sha,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=STAGE5_SHA, component_id=COMPONENT,
            declared_held_out=True,
        ),
    )
    try:
        result = evaluate(args.stage5, loaded.model, only_wave=args.only_wave)
    finally:
        loaded.model.close()
    result["formal_result_sha"] = args.result_sha
    result["formal_model_sha"] = args.model_sha
    result["heldout_component_id"] = COMPONENT
    result["source_stage5_sha"] = STAGE5_SHA
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
