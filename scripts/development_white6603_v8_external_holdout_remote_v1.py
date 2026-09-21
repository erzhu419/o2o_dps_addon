"""Build and score one frozen external raid without fitting or re-selecting alpha.

The standard raw -> normalization -> admission -> reconstruction -> timeline
pipeline supplies the input. This script uses the unchanged Stage-5 wave kernel
but deliberately does not add the external raid to the historical cohort receipt.
All large inputs and the Stage-5 partition remain on the remote filesystem.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
import gzip
import json
import math
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps import chronicle_external_team_timeline_v2 as timeline_v2
from o2o_dps import chronicle_external_team_wave_model_v2 as stage5_v2
from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.development_white6603_joint_training_v8 import (
    JointWhiteTrainingV8, WHITE_TOKEN,
)
from o2o_dps.development_white6603_selection_eval_v8 import (
    CANDIDATE_ALPHA, EARLY_CUTOFF_MS, _mean, _nll, _no_worse,
    score_opportunity_v8,
)


EXTERNAL_ID = "418307df-0321-40cd-8065-d33dd47de197"
CONFIG = ROOT / "configs/evaluation/development_white6603_v8_external_holdout.json"
METRIC_NAMES = ("v7_raw", "v7_smoothed_alpha_one", "selected_v8")
DEFAULT_CLI_WORKERS = 8 if os.name == "posix" else 1
_FORK_MODEL: Any = None
_FORK_WHITE_COUNTS: Any = None
_FORK_CANDIDATE: str | None = None


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _external_config(path: Path) -> dict[str, Any]:
    config = _json(path)
    if (config.get("schema") != "development_white6603_v8_external_holdout/v1"
            or config.get("instance_id") != EXTERNAL_ID
            or config.get("early_cutoff_ms") != EARLY_CUTOFF_MS):
        raise ValueError("external raid or cutoff differs from frozen config")
    return config


def build_single_stage5(
    *, timeline_manifest: Path, output_directory: Path, config_path: Path = CONFIG,
    wave_workers: int = 1,
) -> dict[str, Any]:
    """Use the existing Stage-5 wave kernel, marking the raid nontraining."""

    if wave_workers < 1:
        raise ValueError("Stage-5 wave workers must be positive")
    _external_config(config_path)
    manifest, resolved = timeline_v2.load_external_team_timeline_manifest(
        timeline_manifest, verify_inputs=False, verify_partitions=False,
    )
    if manifest.get("instance_order") != [EXTERNAL_ID] or len(manifest["instances"]) != 1:
        raise ValueError("timeline must contain only the frozen external raid")
    entry = manifest["instances"][0]
    if entry.get("instance_id") != EXTERNAL_ID:
        raise ValueError("timeline entry differs from frozen external raid")
    provenance = entry["instance_provenance"]
    temporal = provenance["temporal_and_guild_provenance"]
    observed = temporal["contamination"]
    # No invented cohort receipt: this is evaluation-only and never becomes a
    # training candidate or a voting historical expert.
    contamination = {
        **observed,
        "started_at": temporal.get("started_at"),
        "time_field": "started_at",
        "uploaded_at_used": False,
        "candidate_filter_passed": False,
        "cohort_assignment": "EXTERNAL_HOLDOUT",
        "cohort_reason": "FROZEN_WHITE6603_V8_ONE_SHOT_EXTERNAL_CHECK",
        "voting_authorized": False,
    }
    source_binding = entry["source_binding"]
    context = stage5_v2.InputInstance(
        index=0, instance_id=EXTERNAL_ID, entry=entry,
        partition_path=resolved.parent / entry["partition"]["path"],
        provenance=provenance, contamination=contamination,
        source_binding_sha256=stage5_v2._sha256_bytes(
            stage5_v2._canonical_bytes(source_binding)
        ),
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    if wave_workers == 1:
        built = stage5_v2._build_partition(context, output_directory=output_directory)
    else:
        built = stage5_v2._build_partition(
            context, output_directory=output_directory, wave_workers=wave_workers,
        )
    try:
        os.link(built.temporary_path, built.final_path)
    finally:
        built.temporary_path.unlink(missing_ok=True)
    return {
        "schema": "development_white6603_v8_external_stage5/v1",
        "instance_id": EXTERNAL_ID,
        "stage5_partition": str(built.final_path),
        "wave_count": built.manifest_entry["summary"]["wave_count"],
        "cohort_assignment": "EXTERNAL_HOLDOUT",
        "wave_workers": wave_workers,
        "timeline_manifest": str(resolved),
    }


def evaluate_fixed_external_v8(
    rows: Iterable[Mapping[str, Any]], *, selected_candidate: str,
) -> dict[str, Any]:
    """Paired teacher-forced metrics for the already selected candidate only."""

    waves = _summarize_external_rows(rows, selected_candidate=selected_candidate)
    return _evaluate_external_wave_summaries(waves, selected_candidate=selected_candidate)


def _summarize_external_rows(
    rows: Iterable[Mapping[str, Any]], *, selected_candidate: str,
) -> dict[str, dict[str, Any]]:
    if selected_candidate != "beta_one" or CANDIDATE_ALPHA[selected_candidate] != 1.0:
        raise ValueError("external amendment requires the internally selected alpha=1")
    waves: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["instance_id"] != EXTERNAL_ID:
            raise ValueError("external scored row crossed raid boundary")
        wave = waves.setdefault(row["wave_id"], {
            "last_ms": -1, "first_seen": False,
            "row_count": 0,
            "observed": 0, "observed_early": 0,
            "nll": Counter(), "first_nll": Counter(),
            "expected": Counter(), "expected_early": Counter(),
        })
        time_ms = row["time_ms"]
        if type(time_ms) is not int or time_ms < wave["last_ms"]:
            raise ValueError("external opportunity timestamps are not ordered")
        wave["last_ms"] = time_ms
        label = row["observed_white"]
        if type(label) is not bool:
            raise ValueError("external white label must be boolean")
        early = time_ms <= EARLY_CUTOFF_MS
        wave["row_count"] += 1
        wave["observed"] += label
        wave["observed_early"] += bool(early and label)
        for name, source in (
            ("v7_raw", "v7_raw"),
            ("v7_smoothed_alpha_one", "v7_smoothed_alpha_one"),
            ("selected_v8", selected_candidate),
        ):
            probability = row["p_white"][source]
            contribution = _nll(label, probability)
            wave["nll"][name] += contribution
            wave["expected"][name] += probability
            if early:
                wave["expected_early"][name] += probability
            if not wave["first_seen"]:
                wave["first_nll"][name] += contribution
        if label:
            wave["first_seen"] = True
    return waves


def _evaluate_external_wave_summaries(
    waves: Mapping[str, Mapping[str, Any]], *, selected_candidate: str,
) -> dict[str, Any]:
    """Reduce whole-wave summaries; serial and parallel use the same order."""

    if selected_candidate != "beta_one" or CANDIDATE_ALPHA[selected_candidate] != 1.0:
        raise ValueError("external amendment requires the internally selected alpha=1")
    ordered = [waves[wave_id] for wave_id in sorted(waves)]
    row_count = sum(wave["row_count"] for wave in ordered)
    if row_count == 0 or not ordered:
        raise ValueError("external raid produced no eligible opportunities")
    observed_white = sum(wave["observed"] for wave in ordered)
    observed_early = sum(wave["observed_early"] for wave in ordered)
    totals = {
        name: {
            "nll": math.fsum(wave["nll"][name] for wave in ordered),
            "first_nll": math.fsum(wave["first_nll"][name] for wave in ordered),
            "expected": math.fsum(wave["expected"][name] for wave in ordered),
            "expected_early": math.fsum(wave["expected_early"][name] for wave in ordered),
        }
        for name in METRIC_NAMES
    }
    metrics = {
        name: {
            "binary_white_mean_nll": _mean(value["nll"], row_count),
            "binary_zero_likelihood": value["nll"] == float("inf"),
            "mean_wave_absolute_white_count_error": math.fsum(
                abs(w["expected"][name] - w["observed"]) for w in ordered
            ) / len(ordered),
            "mean_wave_absolute_early_white_count_error": math.fsum(
                abs(w["expected_early"][name] - w["observed_early"])
                for w in ordered
            ) / len(ordered),
            "first_white_hazard_mean_nll": _mean(value["first_nll"], len(ordered)),
            "expected_white": value["expected"],
            "expected_early_white": value["expected_early"],
        }
        for name, value in totals.items()
    }
    base, chosen = metrics["v7_smoothed_alpha_one"], metrics["selected_v8"]
    gates = {
        "binary_white_nll_improved": (
            totals["selected_v8"]["nll"] < totals["v7_smoothed_alpha_one"]["nll"]
        ),
        "wave_white_count_error_no_worse": _no_worse(
            chosen["mean_wave_absolute_white_count_error"],
            base["mean_wave_absolute_white_count_error"]),
        "wave_early_white_count_error_no_worse": _no_worse(
            chosen["mean_wave_absolute_early_white_count_error"],
            base["mean_wave_absolute_early_white_count_error"]),
        "first_white_timing_nll_no_worse": _no_worse(
            totals["selected_v8"]["first_nll"],
            totals["v7_smoothed_alpha_one"]["first_nll"]),
    }
    return {
        "schema": "development_white6603_v8_external_one_shot/v1",
        "status": "PASS_EXTERNAL_ONE_SHOT" if all(gates.values()) else "FAIL_EXTERNAL_ONE_SHOT",
        "instance_id": EXTERNAL_ID,
        "selected_candidate_from_internal": selected_candidate,
        "gate_comparator": "v7_smoothed_alpha_one",
        "v7_raw_role": "descriptive_only_zero_likelihood_possible",
        "alpha_reselected_on_external": False,
        "opportunity_count": row_count,
        "wave_count": len(ordered),
        "observed_white": observed_white,
        "observed_early_white": observed_early,
        "early_cutoff_ms": EARLY_CUTOFF_MS,
        "metrics": metrics,
        "gates": gates,
        "claim_boundary": "teacher-forced white mark only; not free-running DPS or Cat/Contra superiority",
    }


def _score_external_opportunity(
    *, row: Mapping[str, Any], v7_fit_model: Any,
    v8_fit_white_counts: Mapping[tuple[Any, ...], Counter[bool]],
    instance_id: str, wave_id: str,
) -> dict[str, Any]:
    """Add equal-alpha smoothing at the same selected v7 raw-mark context."""

    scored = score_opportunity_v8(
        row=row, v7_fit_model=v7_fit_model,
        v8_fit_white_counts=v8_fit_white_counts,
        instance_id=instance_id, wave_id=wave_id,
    )
    for context in response._context_keys(
        row["actor"], row["emission_state_before_current_event"], response.ABLATION_D
    ):
        marks = v7_fit_model.mark_counts.get(context, Counter())
        support = sum(marks.values())
        if support >= v7_fit_model.minimums[context[0]]:
            scored["p_white"]["v7_smoothed_alpha_one"] = (
                marks[WHITE_TOKEN] + 1.0
            ) / (support + 2.0)
            return scored
    raise ValueError("v7 fit-only raw mark context has no support")


def _score_stage5_shard(
    partition: Path, *, v7_fit_model: Any,
    v8_fit_white_counts: Mapping[tuple[Any, ...], Counter[bool]],
    selected_candidate: str,
) -> dict[str, dict[str, Any]]:
    """Score complete Stage-5 waves without retaining opportunity rows."""

    waves: dict[str, dict[str, Any]] = {}
    with gzip.open(partition, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            wave = record["wave"]
            if wave["instance_id"] != EXTERNAL_ID:
                raise ValueError("Stage-5 partition crossed external raid")
            wave_id = wave["wave_id"]
            if wave_id in waves:
                raise ValueError("Stage-5 partition repeated external wave")
            scored = (
                _score_external_opportunity(
                    row=row, v7_fit_model=v7_fit_model,
                    v8_fit_white_counts=v8_fit_white_counts,
                    instance_id=EXTERNAL_ID, wave_id=wave_id,
                )
                for row in response.iter_wave_response_sufficient_rows_v1(record)
            )
            summaries = _summarize_external_rows(
                scored, selected_candidate=selected_candidate,
            )
            if summaries and set(summaries) != {wave_id}:
                raise ValueError("scored opportunities crossed Stage-5 wave")
            waves.update(summaries)
    return waves


def _score_forked_stage5_shard(partition: Path) -> dict[str, dict[str, Any]]:
    if _FORK_MODEL is None or _FORK_WHITE_COUNTS is None or _FORK_CANDIDATE is None:
        raise RuntimeError("forked scorer did not inherit fit-only model")
    return _score_stage5_shard(
        partition, v7_fit_model=_FORK_MODEL,
        v8_fit_white_counts=_FORK_WHITE_COUNTS,
        selected_candidate=_FORK_CANDIDATE,
    )


def _split_stage5_waves(partition: Path, directory: Path, workers: int) -> list[Path]:
    """Single streaming pass; one complete compressed wave record per shard."""

    paths = [directory / f"wave-shard-{index:03d}.jsonl.gz" for index in range(workers)]
    counts = [0] * workers
    with ExitStack() as stack:
        source = stack.enter_context(gzip.open(partition, "rb"))
        # These shards are temporary IPC, not archival partitions.
        outputs = [stack.enter_context(gzip.open(path, "wb", compresslevel=1)) for path in paths]
        for index, line in enumerate(source):
            shard = index % workers
            outputs[shard].write(line)
            counts[shard] += 1
    return [path for path, count in zip(paths, counts) if count]


def _merge_external_shard_summaries(
    shard_summaries: Iterable[Mapping[str, Mapping[str, Any]]],
    *, selected_candidate: str,
) -> dict[str, Any]:
    waves: dict[str, Mapping[str, Any]] = {}
    for shard in shard_summaries:
        for wave_id, summary in shard.items():
            if wave_id in waves:
                raise ValueError("parallel Stage-5 shards repeated external wave")
            waves[wave_id] = summary
    return _evaluate_external_wave_summaries(waves, selected_candidate=selected_candidate)


def score_single_external(
    *, stage5_partition: Path, split_path: Path,
    selection_result_path: Path, output_path: Path,
    fit_merged_path: Path | None = None, fit_projection_path: Path | None = None,
    config_path: Path = CONFIG,
    workers: int = 1,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("external scorer workers must be positive")
    if workers > 1 and os.name != "posix":
        raise ValueError("parallel external scoring requires Linux fork; use workers=1 locally")
    if (fit_merged_path is None) == (fit_projection_path is None):
        raise ValueError("provide exactly one fit merged or compact projection input")
    config = _external_config(config_path)
    split, selection = _json(split_path), _json(selection_result_path)
    if (selection.get("status") != "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
            or selection.get("selection_instance_ids") != split["selection_instance_ids"]):
        raise ValueError("frozen internal selection has not passed")
    chosen = selection.get("selected_candidate")
    if chosen != "beta_one" or CANDIDATE_ALPHA[chosen] != 1.0:
        raise ValueError("external amendment requires frozen internal beta_one")
    if fit_projection_path is not None:
        from o2o_dps.development_white6603_selection_eval_v8 import load_fit_scoring_v8

        projection = _json(fit_projection_path)
        if projection["fit_instance_ids"] != split["fit_instance_ids"]:
            raise ValueError("external scorer fit raid membership differs")
        v7_fit_model, white_counts = load_fit_scoring_v8(projection)
        del projection
    else:
        assert fit_merged_path is not None
        with gzip.open(fit_merged_path, "rt", encoding="utf-8") as handle:
            fit_document = json.load(handle)
        if fit_document["fit_instance_ids"] != split["fit_instance_ids"]:
            raise ValueError("external scorer fit raid membership differs")
        fit = JointWhiteTrainingV8.deserialize(fit_document)
        del fit_document
        v7_fit_model = hpc._JointEvaluationModelViewV4(fit.joint, response.ABLATION_D)
        white_counts = fit.white_counts
    if workers == 1:
        summaries = [_score_stage5_shard(
            stage5_partition, v7_fit_model=v7_fit_model,
            v8_fit_white_counts=white_counts, selected_candidate=chosen,
        )]
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        global _FORK_MODEL, _FORK_WHITE_COUNTS, _FORK_CANDIDATE
        _FORK_MODEL = v7_fit_model
        _FORK_WHITE_COUNTS = white_counts
        _FORK_CANDIDATE = chosen
        try:
            with tempfile.TemporaryDirectory(prefix="white6603-external-waves-", dir=output_path.parent) as temporary:
                shards = _split_stage5_waves(stage5_partition, Path(temporary), workers)
                with ProcessPoolExecutor(
                    max_workers=min(workers, len(shards)),
                    mp_context=multiprocessing.get_context("fork"),
                ) as executor:
                    summaries = list(executor.map(_score_forked_stage5_shard, shards))
        finally:
            _FORK_MODEL = _FORK_WHITE_COUNTS = _FORK_CANDIDATE = None
    result = _merge_external_shard_summaries(summaries, selected_candidate=chosen)
    result["fit_task_count"] = len(split["fit_instance_ids"])
    result["internal_selection_result"] = str(selection_result_path)
    result["stage5_partition"] = str(stage5_partition)
    result["comparator_amendment"] = config["comparator_amendment"]
    result["amendment_timing"] = "AFTER_INTERNAL_SELECTION_BEFORE_EXTERNAL_LABELS"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("stage5")
    build.add_argument("--timeline-manifest", type=Path, required=True)
    build.add_argument("--output-directory", type=Path, required=True)
    build.add_argument("--stage5-workers", type=int, default=DEFAULT_CLI_WORKERS,
                       help="independent waves inside the one external raid")
    score = sub.add_parser("score")
    score.add_argument("--stage5-partition", type=Path, required=True)
    score.add_argument("--split", type=Path, required=True)
    fit_input = score.add_mutually_exclusive_group(required=True)
    fit_input.add_argument("--fit-projection", type=Path,
                           help="preferred compact fit-only scoring projection")
    fit_input.add_argument("--fit-merged", type=Path,
                           help="legacy complete fit-only joint counts")
    score.add_argument("--selection-result", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--workers", type=int,
                       help="Linux fork workers; defaults to 8 for compact projection, 1 for full fit")
    args = parser.parse_args()
    if args.command == "stage5":
        result = build_single_stage5(
            timeline_manifest=args.timeline_manifest,
            output_directory=args.output_directory,
            wave_workers=args.stage5_workers,
        )
    else:
        result = score_single_external(
            stage5_partition=args.stage5_partition, split_path=args.split,
            fit_merged_path=args.fit_merged, fit_projection_path=args.fit_projection,
            selection_result_path=args.selection_result,
            output_path=args.output,
            workers=args.workers if args.workers is not None else (
                DEFAULT_CLI_WORKERS if args.fit_projection is not None else 1
            ),
        )
    print(json.dumps(result if args.command == "stage5" else {
        key: result[key] for key in ("status", "selected_candidate_from_internal", "opportunity_count", "wave_count", "gates")
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
