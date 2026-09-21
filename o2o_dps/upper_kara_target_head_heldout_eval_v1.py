"""Evaluate a learned target-choice head on disjoint Stage-5 prefix rows.

The label gate and candidate features are delegated to the training compiler,
so the baseline and learned head always share precisely the same denominator.
No held-out row updates the model.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from o2o_dps import chronicle_external_teammate_response_model_v1 as response


SCHEMA = "upper_kara_target_head_heldout_eval/v2"
D900_WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
FOCAL = "0x0000000000576754"


def _metric() -> dict[str, Any]:
    return {
        "eligible_choices": 0,
        "uniform_expected_correct": 0.0,
        "learned_expected_correct": 0.0,
        "learned_top1_correct": 0,
        "learned_supported_choices": 0,
        "learned_fallback_choices": 0,
        "context_level_counts": {},
    }


def _distribution(model: Any, context: tuple[str, ...], phase: str,
                  feature: tuple[str, str, str]) -> tuple[int, int]:
    if hasattr(model, "target_choice_counts"):
        counts = model.target_choice_counts.get((context, phase, feature))
        return (int(counts[True]), int(counts[False])) if counts else (0, 0)
    distribution = model._distribution("target_choice_counts", (context, phase, feature))
    if distribution is None:
        return 0, 0
    values = {bool(value): int(count) for value, count in distribution[1]}
    return values.get(True, 0), values.get(False, 0)


def _learned_probabilities(model: Any, row: Mapping[str, Any], phase: str,
                           features: Mapping[str, tuple[str, str, str]]) -> tuple[dict[str, float], str]:
    state = row["emission_state_before_current_event"]
    actor = row["actor"]
    for context in response._context_keys(actor, state, model.variant_id):
        feature_counts = {
            feature: _distribution(model, context, phase, feature)
            for feature in set(features.values())
        }
        counts = {guid: feature_counts[feature] for guid, feature in features.items()}
        support = sum(positive + negative for positive, negative in feature_counts.values())
        if support < model.minimums[context[0]]:
            continue
        weights = {guid: (positive + 1) / (positive + negative + 2)
                   for guid, (positive, negative) in counts.items()}
        total = sum(weights.values())
        return {guid: weight / total for guid, weight in weights.items()}, context[0]
    chance = 1.0 / len(features)
    return {guid: chance for guid in features}, "UNIFORM_FALLBACK"


def score_compiled_rows_v1(rows: Iterable[Mapping[str, Any]], *, model: Any | None = None) -> dict[str, Any]:
    """Score START and WHITE6603 on separate strict-prefix choice denominators."""
    metrics = {"FIRST_ACQUISITION": _metric(), "RETARGET": _metric()}
    white_metrics = {"FIRST_ACQUISITION": _metric(), "RETARGET": _metric()}
    direct_start_rows = 0
    gate = {
        "direct_hostile_start_rows": 0,
        "nonvoting_or_unseen_mode_rows": 0,
        "stay_on_previous_target_rows": 0,
        "phase_precheck": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
        "phase_label_not_prefix_candidate": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
        "phase_fewer_than_two_alternatives": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
    }
    white_gate = {
        "direct_hostile_white_rows": 0,
        "compiler_prefix_candidates_unavailable_rows": 0,
        "nonvoting_or_unseen_mode_rows": 0,
        "stay_on_previous_target_rows": 0,
        "phase_precheck": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
        "phase_label_not_prefix_candidate": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
        "phase_fewer_than_two_alternatives": {"FIRST_ACQUISITION": 0, "RETARGET": 0},
    }
    direct_white_rows = 0
    for row in rows:
        label = row.get("label")
        if not isinstance(label, Mapping):
            continue
        is_start = label.get("event_type") == "START"
        is_white = label.get("event_type") == "DMG" and label.get("spell_id") == 6603
        is_direct = label.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
        if is_direct and (is_start or is_white):
            current_gate = gate if is_start else white_gate
            if is_start:
                direct_start_rows += 1
            else:
                direct_white_rows += 1
                if "target_choice_candidates" not in row["emission_state_before_current_event"]["target_state"]:
                    white_gate["compiler_prefix_candidates_unavailable_rows"] += 1
                    continue
            if label.get("target_lane") == "HOSTILE_CREATURE":
                current_gate["direct_hostile_start_rows" if is_start else "direct_hostile_white_rows"] += 1
                if label.get("target_mode") not in {"STAY_ALIVE", "SWITCH_ALIVE"}:
                    current_gate["nonvoting_or_unseen_mode_rows"] += 1
                else:
                    state = row["emission_state_before_current_event"]
                    previous = state.get("actor_last_target_guid")
                    selected = label.get("target_guid")
                    if previous == selected:
                        current_gate["stay_on_previous_target_rows"] += 1
                    else:
                        phase = "FIRST_ACQUISITION" if previous is None else "RETARGET"
                        current_gate["phase_precheck"][phase] += 1
                        candidates = state["target_state"]["target_choice_candidates"]
                        if selected not in {candidate["target_guid"] for candidate in candidates}:
                            current_gate["phase_label_not_prefix_candidate"][phase] += 1
                        elif sum(candidate["target_guid"] != previous for candidate in candidates) < 2:
                            current_gate["phase_fewer_than_two_alternatives"][phase] += 1
        observation = response._target_choice_training_observations(row)
        if observation is None:
            continue
        phase, features, selected = observation
        if phase.startswith("WHITE6603_"):
            intent_kind, simple_phase = "WHITE6603", phase.removeprefix("WHITE6603_")
        elif phase.startswith("START_"):
            intent_kind, simple_phase = "START", phase.removeprefix("START_")
        else:
            # The frozen v6 D-store has the same START label gate with unprefixed phases.
            intent_kind, simple_phase = "START", phase
        metric = (metrics if intent_kind == "START" else white_metrics)[simple_phase]
        metric["eligible_choices"] += 1
        metric["uniform_expected_correct"] += 1.0 / len(features)
        if model is not None:
            probabilities, level = _learned_probabilities(model, row, phase, features)
            metric["learned_expected_correct"] += probabilities[selected]
            metric["learned_top1_correct"] += int(
                min(probabilities, key=lambda guid: (-probabilities[guid], guid)) == selected
            )
            support_key = "learned_fallback_choices" if level == "UNIFORM_FALLBACK" else "learned_supported_choices"
            metric[support_key] += 1
            levels = metric["context_level_counts"]
            levels[level] = levels.get(level, 0) + 1
    for metric in (*metrics.values(), *white_metrics.values()):
        denominator = metric["eligible_choices"]
        metric["uniform_expected_accuracy"] = (
            metric["uniform_expected_correct"] / denominator if denominator else None
        )
        metric["learned_expected_accuracy"] = (
            metric["learned_expected_correct"] / denominator if denominator and model is not None else None
        )
        metric["learned_top1_accuracy"] = (
            metric["learned_top1_correct"] / denominator if denominator and model is not None else None
        )
        if model is None:
            for key in (
                "learned_expected_correct", "learned_top1_correct",
                "learned_supported_choices", "learned_fallback_choices", "context_level_counts",
            ):
                metric[key] = None
    return {
        "direct_player_start_rows": direct_start_rows,
        "coverage_gate": gate,
        "metrics": metrics,
        "direct_player_white_6603_rows": direct_white_rows,
        "white_coverage_gate": white_gate,
        "white_metrics": white_metrics,
        "white_head_status": (
            "UNAVAILABLE_IN_COMPILER" if white_gate["compiler_prefix_candidates_unavailable_rows"]
            else "STRICT_PREFIX_CHOICES_AVAILABLE"
        ),
        "model_scored": model is not None,
    }


def evaluate_heldout_stage5_gzip_v1(
    path: str | Path, *, model: Any | None = None,
) -> dict[str, Any]:
    """One server-local scan; emit no raw event data or training mutations."""
    clean_waves = 0
    clean_multi_start_waves = 0
    cohort_wave_ids: list[str] = []
    evaluated_rows: list[Mapping[str, Any]] = []
    selected_rows: list[Mapping[str, Any]] = []
    instance_id = None
    for line in gzip.open(path, "rt", encoding="utf-8"):
        record = json.loads(line)
        wave = record.get("wave")
        provenance = record.get("raid_provenance")
        contamination = provenance.get("contamination") if isinstance(provenance, Mapping) else None
        if not isinstance(wave, Mapping) or not isinstance(contamination, Mapping):
            continue
        if contamination.get("candidate_filter_passed") is not True:
            continue
        clean_waves += 1
        instance_id = wave.get("instance_id")
        direct_targets = {
            event["target"]["guid"]
            for trace in record.get("exact_trace", ())
            if trace.get("trace_kind") == "EXACT_PLAYER_EVENT"
            and isinstance((event := trace.get("event")), Mapping)
            and event.get("event_type") == "START"
            and isinstance(event.get("attribution"), Mapping)
            and event["attribution"].get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
            and event["attribution"].get("player_guid") == trace.get("player_guid")
            and isinstance(event.get("target"), Mapping)
            and event["target"].get("lane") == "HOSTILE_CREATURE"
            and isinstance(event["target"].get("guid"), str)
        }
        is_selected = wave.get("wave_id") == D900_WAVE
        is_cohort = 2 <= len(direct_targets) <= 6
        if not (is_selected or is_cohort):
            continue
        rows = list(response.iter_wave_response_sufficient_rows_v1(record))
        if is_selected:
            selected_rows = rows
        if is_cohort:
            clean_multi_start_waves += 1
            cohort_wave_ids.append(str(wave["wave_id"]))
            evaluated_rows.extend(rows)
    if not selected_rows:
        raise ValueError("d900 wave absent from held-out Stage-5 file")
    return {
        "schema": SCHEMA,
        "status": "HELDOUT_TARGET_CHOICE_DIAGNOSTIC_NOT_DPS_COMPARISON",
        "heldout_instance_id": instance_id,
        "clean_wave_count": clean_waves,
        "cohort_wave_count": clean_multi_start_waves,
        "cohort_wave_ids": cohort_wave_ids,
        "cohort_rule": "clean raid candidate; 2-6 direct-player START target GUIDs",
        "cohort": score_compiled_rows_v1(evaluated_rows, model=model),
        "cohort_teammates_only": score_compiled_rows_v1(
            [row for row in evaluated_rows if row["actor"]["player_guid"] != FOCAL], model=model
        ),
        "selected_d900": score_compiled_rows_v1(selected_rows, model=model),
        "selected_d900_teammates_only": score_compiled_rows_v1(
            [row for row in selected_rows if row["actor"]["player_guid"] != FOCAL], model=model
        ),
        "denominator_views": {
            "cohort": "all players, including current focal",
            "cohort_teammates_only": f"same waves, excluding player {FOCAL}",
            "selected_d900": "all players, including current focal",
            "selected_d900_teammates_only": f"same wave, excluding player {FOCAL}",
        },
        "causal_contract": {
            "label_gate": "model _target_choice_training_observations, START and WHITE6603 separately",
            "teammate_only_excluded_player": FOCAL,
            "candidate_cutoff": "strictly before current EventMeta order key",
            "retarget_excludes_actor_previous_target": True,
            "heldout_rows_update_model": False,
            "historical_future_death_or_HP_as_feature": False,
            "simulator_attackability_inferred_from_log": False,
        },
    }


__all__ = ("score_compiled_rows_v1", "evaluate_heldout_stage5_gzip_v1")
