"""Paired, teacher-forced white-mark selection on frozen TRAIN raid blocks.

Both v7 raw mark counts and v8 white counts must be fitted on the same 52
fit raids. This is an intra-component development gate, not external evidence
or a free-running damage comparison.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from . import chronicle_external_teammate_response_model_v1 as response
from . import chronicle_external_teammate_response_hpc_v1 as hpc
from .development_white6603_joint_training_v8 import WHITE_TOKEN
from .development_white6603_opportunity_v8 import white6603_opportunity_from_prefix_v8


EARLY_CUTOFF_MS = 9098  # Frozen from the earlier d900 diagnosis; not independent.
CANDIDATE_ALPHA = {"beta_half": 0.5, "beta_one": 1.0}
WHITE_MINIMUMS = {"CLASS_SPEC": 50, "CLASS": 100, "CLASS_PHASE": 100, "GLOBAL": 1}
MODEL_NAMES = ("v7_raw", *CANDIDATE_ALPHA)
FIT_SCORING_SCHEMA = "development_white6603_v8_fit_scoring_projection/v1"


def project_fit_scoring_v8(fit_document: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only mark and white counts needed to score frozen opportunities."""

    joint = fit_document["joint_dynamic_training_counts"]
    base = joint["base_c"]
    delta = joint["d_specific_context_delta"]
    if fit_document["schema"] != "development_white6603_joint_training/v8":
        raise ValueError("fit-only white training schema differs")
    if joint["row_count"] != fit_document["row_count"] or base["row_count"] != fit_document["row_count"]:
        raise ValueError("fit-only joint row count differs")
    return {
        "schema": FIT_SCORING_SCHEMA,
        "fit_instance_ids": list(fit_document["fit_instance_ids"]),
        "row_count": fit_document["row_count"],
        "white_count": fit_document["white_count"],
        "minimums": base["minimums"],
        "v7_shared_mark_counts": base["tables"]["mark_counts"],
        "v7_d_specific_mark_counts": delta["tables"]["mark_counts"],
        "v8_white_counts": fit_document["white_opportunity_counts"],
    }


def load_fit_scoring_v8(
    projection: Mapping[str, Any],
) -> tuple[Any, dict[tuple[Any, ...], Counter[bool]]]:
    """Load a D-routed mark view and v8 white counts without target/damage tables."""

    if projection["schema"] != FIT_SCORING_SCHEMA:
        raise ValueError("fit-only scoring projection schema differs")
    shared = hpc._load_counter_table(projection["v7_shared_mark_counts"], "v7 shared marks")
    specific = hpc._load_counter_table(projection["v7_d_specific_mark_counts"], "v7 D-specific marks")
    model = SimpleNamespace(
        minimums=dict(projection["minimums"]),
        mark_counts=hpc._JointContextTableViewV4(shared, specific, token_keyed=False),
    )
    white_counts = {
        tuple(row["context"]): Counter({True: row["white"], False: row["nonwhite"]})
        for row in projection["v8_white_counts"]
    }
    if sum(shared.get(("GLOBAL",), Counter()).values()) != projection["row_count"]:
        raise ValueError("fit-only projected marks differ from row count")
    if sum(counts[True] for context, counts in white_counts.items() if context[0] == "GLOBAL") != projection["white_count"]:
        raise ValueError("fit-only projected white marks differ from white count")
    return model, white_counts


def score_opportunity_v8(
    *, row: Mapping[str, Any], v7_fit_model: Any,
    v8_fit_white_counts: Mapping[tuple[Any, ...], Counter[bool]],
    instance_id: str, wave_id: str,
) -> dict[str, Any]:
    """Score one strict-prefix opportunity without reading its label as a feature."""

    actor = row["actor"]
    state = row["emission_state_before_current_event"]
    label = row["label"]
    observation = white6603_opportunity_from_prefix_v8(row)
    for context in response._context_keys(actor, state, response.ABLATION_D):
        marks = v7_fit_model.mark_counts.get(context, Counter())
        support = sum(marks.values())
        if support >= v7_fit_model.minimums[context[0]]:
            break
    else:
        raise ValueError("v7 fit-only raw mark context has no support")
    white_count = marks[WHITE_TOKEN]
    nonwhite_support = support - white_count
    token = label["mark_token"]
    # Fixed Jeffreys smoothing plus one unknown-token bucket makes the
    # conditional nonwhite score finite for marks absent from a fit context.
    nonwhite_vocab_size = sum(mark != WHITE_TOKEN for mark in marks) + 1
    p_nonwhite_token = (
        (marks[token] + 0.5) / (nonwhite_support + 0.5 * nonwhite_vocab_size)
    ) if not observation["white6603"] else None

    for white_context in observation["contexts"]:
        counts = v8_fit_white_counts.get(white_context, Counter())
        white_support = counts[True] + counts[False]
        if white_support >= WHITE_MINIMUMS[white_context[0]]:
            break
    else:
        raise ValueError("v8 fit-only white context has no support")
    return {
        "instance_id": instance_id,
        "wave_id": wave_id,
        "time_ms": observation["elapsed_ms"],
        "actor_class": actor["class"],
        "actor_spec": actor["spec_key"],
        "observed_white": observation["white6603"],
        "p_white": {
            "v7_raw": white_count / support,
            **{
                name: (counts[True] + alpha) / (white_support + 2 * alpha)
                for name, alpha in CANDIDATE_ALPHA.items()
            },
        },
        # The v8 white head reweights white vs nonwhite; the fit-only v7
        # conditional distribution over nonwhite marks remains unchanged.
        "p_nonwhite_token_given_nonwhite": p_nonwhite_token,
    }


def _nll(label: bool, probability: float) -> float:
    if not isinstance(probability, (float, int)) or isinstance(probability, bool):
        raise ValueError("mark probability must be numeric")
    if not math.isfinite(probability) or probability < 0 or probability > 1:
        raise ValueError("mark probability must be in [0, 1]")
    likelihood = probability if label else 1 - probability
    return -math.log(likelihood) if likelihood else math.inf


def _mean(total: float, count: int) -> float | None:
    return total / count if count and math.isfinite(total) else None


def _no_worse(new: float, old: float) -> bool:
    return new <= old or math.isclose(new, old, abs_tol=1e-12)


def _nll_pair() -> list[float | int]:
    # Zero-likelihood v7 rows are present in real data; keep partial JSON finite.
    return [0.0, 0]


def _add_nll(pair: list[float | int], contribution: float) -> None:
    if math.isfinite(contribution):
        pair[0] += contribution
    else:
        pair[1] += 1


def _nll_total(pairs: Iterable[list[float | int]]) -> float:
    values = list(pairs)
    return math.inf if any(value[1] for value in values) else sum(value[0] for value in values)


def _model_map(factory: Any) -> dict[str, Any]:
    return {name: factory() for name in MODEL_NAMES}


def _new_raid() -> dict[str, Any]:
    return {
        "opportunities": 0, "observed_white": 0, "observed_early_white": 0,
        "nonwhite_opportunities": 0, "nonwhite_nll": _nll_pair(),
        "binary_nll": _model_map(_nll_pair),
        "expected_white": _model_map(float),
        "expected_early_white": _model_map(float),
        "waves": {}, "strata": {},
    }


def _new_wave() -> dict[str, Any]:
    return {
        "last_ms": -1, "first_seen": False, "observed_first_white_ms": None,
        "observed_white": 0, "observed_early_white": 0,
        "expected_white": _model_map(float),
        "expected_early_white": _model_map(float),
        "first_white_hazard_nll": _model_map(_nll_pair),
    }


def _new_stratum() -> dict[str, Any]:
    return {
        "opportunities": 0, "observed_white": 0,
        "expected_white": _model_map(float), "binary_nll": _model_map(_nll_pair),
    }


def score_selection_partial_v8(
    rows: Iterable[Mapping[str, Any]], selection_instance_ids: Iterable[str],
) -> dict[str, Any]:
    """Score complete raid blocks into small JSON-compatible sufficient statistics."""

    expected_instances = set(selection_instance_ids)
    if not expected_instances:
        raise ValueError("selection shard has no raids")
    raids: dict[str, dict[str, Any]] = {}
    for row in rows:
        instance_id = row["instance_id"]
        if instance_id not in expected_instances:
            raise ValueError("opportunity belongs outside frozen TRAIN selection")
        raid = raids.setdefault(instance_id, _new_raid())
        wave = raid["waves"].setdefault(row["wave_id"], _new_wave())
        time_ms = row["time_ms"]
        if type(time_ms) is not int or time_ms < wave["last_ms"]:
            raise ValueError("wave opportunity timestamps must be ordered")
        wave["last_ms"] = time_ms
        label = row["observed_white"]
        if type(label) is not bool:
            raise ValueError("white label must be boolean")
        stratum_key = json.dumps([row["actor_class"], row["actor_spec"]], ensure_ascii=False)
        stratum = raid["strata"].setdefault(stratum_key, _new_stratum())
        stratum["opportunities"] += 1
        stratum["observed_white"] += label
        raid["opportunities"] += 1
        raid["observed_white"] += label
        early = time_ms <= EARLY_CUTOFF_MS
        raid["observed_early_white"] += bool(early and label)
        wave["observed_white"] += label
        wave["observed_early_white"] += bool(early and label)
        for name in MODEL_NAMES:
            p = row["p_white"][name]
            contribution = _nll(label, p)
            _add_nll(raid["binary_nll"][name], contribution)
            raid["expected_white"][name] += p
            wave["expected_white"][name] += p
            if early:
                raid["expected_early_white"][name] += p
                wave["expected_early_white"][name] += p
            stratum["expected_white"][name] += p
            _add_nll(stratum["binary_nll"][name], contribution)
            if not wave["first_seen"]:
                _add_nll(wave["first_white_hazard_nll"][name], contribution)
        if label and not wave["first_seen"]:
            wave["first_seen"] = True
            wave["observed_first_white_ms"] = time_ms
        if not label:
            _add_nll(raid["nonwhite_nll"], _nll(True, row["p_nonwhite_token_given_nonwhite"]))
            raid["nonwhite_opportunities"] += 1
    if set(raids) != expected_instances or any(not raid["opportunities"] for raid in raids.values()):
        raise ValueError("frozen selection raid coverage is incomplete")
    return {"schema": "development_white6603_selection_partial/v1", "raids": raids}


def evaluate_selection_partials_v8(
    partials: Iterable[Mapping[str, Any]], selection_instance_ids: Iterable[str],
) -> dict[str, Any]:
    """Deterministically reduce disjoint raid shards to the frozen 13-raid gate."""

    expected_instances = set(selection_instance_ids)
    if len(expected_instances) != 13:
        raise ValueError("expected 13 frozen TRAIN selection raids")
    raids: dict[str, Mapping[str, Any]] = {}
    for partial in partials:
        if partial["schema"] != "development_white6603_selection_partial/v1":
            raise ValueError("selection partial schema differs")
        for instance_id, raid in partial["raids"].items():
            if instance_id not in expected_instances or instance_id in raids:
                raise ValueError("selection partials overlap or include another raid")
            raids[instance_id] = raid
    if set(raids) != expected_instances:
        raise ValueError("frozen selection raid coverage is incomplete")
    ordered = [raids[instance_id] for instance_id in sorted(raids)]
    waves = [wave for raid in ordered for wave in raid["waves"].values()]
    row_count = sum(raid["opportunities"] for raid in ordered)
    if not row_count or not waves:
        raise ValueError("frozen selection raid coverage is incomplete")
    observed_white = sum(raid["observed_white"] for raid in ordered)
    early_white = sum(raid["observed_early_white"] for raid in ordered)
    nonwhite_count = sum(raid["nonwhite_opportunities"] for raid in ordered)
    nonwhite_nll = _nll_total(raid["nonwhite_nll"] for raid in ordered)
    totals = {
        name: {
            "binary_nll": _nll_total(raid["binary_nll"][name] for raid in ordered),
            "expected_white": sum(raid["expected_white"][name] for raid in ordered),
            "expected_early_white": sum(raid["expected_early_white"][name] for raid in ordered),
            "first_white_hazard_nll": _nll_total(
                wave["first_white_hazard_nll"][name] for wave in waves
            ),
        }
        for name in MODEL_NAMES
    }
    metrics = {
        name: {
            "binary_mean_nll": _mean(values["binary_nll"], row_count),
            "binary_zero_likelihood": not math.isfinite(values["binary_nll"]),
            "expected_white": values["expected_white"],
            "absolute_white_count_bias": abs(values["expected_white"] - observed_white),
            "mean_wave_absolute_white_count_error": sum(
                abs(wave["expected_white"][name] - wave["observed_white"])
                for wave in waves
            ) / len(waves),
            "expected_early_white": values["expected_early_white"],
            "absolute_early_white_count_bias": abs(values["expected_early_white"] - early_white),
            "mean_wave_absolute_early_white_count_error": sum(
                abs(wave["expected_early_white"][name] - wave["observed_early_white"])
                for wave in waves
            ) / len(waves),
            "nonwhite_mark_mean_nll": _mean(nonwhite_nll, nonwhite_count),
            "first_white_hazard_mean_nll": _mean(values["first_white_hazard_nll"], len(waves)),
        }
        for name, values in totals.items()
    }
    winner = min(CANDIDATE_ALPHA, key=lambda name: (totals[name]["binary_nll"], name))
    base, chosen = totals["v7_raw"], totals[winner]
    gates = {
        "binary_white_nll_improved": chosen["binary_nll"] < base["binary_nll"],
        "wave_white_count_error_no_worse": _no_worse(
            metrics[winner]["mean_wave_absolute_white_count_error"],
            metrics["v7_raw"]["mean_wave_absolute_white_count_error"],
        ),
        "wave_early_white_count_error_no_worse": _no_worse(
            metrics[winner]["mean_wave_absolute_early_white_count_error"],
            metrics["v7_raw"]["mean_wave_absolute_early_white_count_error"],
        ),
        "first_white_timing_nll_no_worse": _no_worse(
            chosen["first_white_hazard_nll"], base["first_white_hazard_nll"]
        ),
    }
    return {
        "schema": "development_white6603_v8_teacher_forced_selection/v1",
        "status": "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE" if all(gates.values()) else "FAIL_INTRA_COMPONENT_DEVELOPMENT_GATE",
        "scope": "13 TRAIN raid instances in same component; both count models fit only on frozen other 52 TRAIN raids",
        "candidate_rule": "two fixed Beta alphas 0.5/1.0; choose lowest white binary NLL, ties by candidate name",
        "early_cutoff_ms": EARLY_CUTOFF_MS,
        "early_cutoff_origin": "earlier d900 diagnosis, non-independent; cannot alone determine PASS",
        "first_white_timing_definition": "wave-first discrete hazard NLL to observed first white, or right-censor at last opportunity",
        "nonwhite_mark_definition": "same fit-only v7 conditional nonwhite distribution in both arms; fixed Jeffreys 0.5 and one unknown-token bucket",
        "nonwhite_mark_invariant": "v8 changes only white-vs-nonwhite mass; conditional nonwhite mark NLL is identical by construction and is descriptive, not an independent improvement gate",
        "opportunity_count": row_count,
        "wave_count": len(waves),
        "observed_white": observed_white,
        "observed_early_white": early_white,
        "nonwhite_opportunity_count": nonwhite_count,
        "waves_with_observed_white": sum(wave["first_seen"] for wave in waves),
        "selection_instance_ids": sorted(expected_instances),
        "metrics": metrics,
        "selected_candidate": winner,
        "gates": gates,
        "class_spec_descriptive_only": _reduce_strata(ordered),
    }


def _reduce_strata(raids: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = sorted({key for raid in raids for key in raid["strata"]})
    answer = []
    for key in keys:
        values = [raid["strata"][key] for raid in raids if key in raid["strata"]]
        hero_class, spec = json.loads(key)
        opportunities = sum(value["opportunities"] for value in values)
        answer.append({
            "class": hero_class, "spec": spec,
            "opportunities": opportunities,
            "observed_white": sum(value["observed_white"] for value in values),
            "expected_white": {
                name: sum(value["expected_white"][name] for value in values)
                for name in MODEL_NAMES
            },
            "binary_mean_nll": {
                name: _mean(_nll_total(value["binary_nll"][name] for value in values), opportunities)
                for name in MODEL_NAMES
            },
        })
    return answer


def evaluate_selection_v8(
    rows: Iterable[Mapping[str, Any]], selection_instance_ids: Iterable[str],
) -> dict[str, Any]:
    """Score exactly the 13 frozen selection raids at identical opportunities."""

    instance_ids = set(selection_instance_ids)
    if len(instance_ids) != 13:
        raise ValueError("expected 13 frozen TRAIN selection raids")
    return evaluate_selection_partials_v8(
        [score_selection_partial_v8(rows, instance_ids)], instance_ids
    )
