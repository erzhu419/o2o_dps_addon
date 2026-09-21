"""Synthetic-only external holdout routing; never reads the frozen raid."""

import json
import gzip
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import development_white6603_v8_external_holdout_remote_v1 as external


def _row(wave, time_ms, label, base, half, one, smoothed=None):
    return {
        "instance_id": external.EXTERNAL_ID,
        "wave_id": wave,
        "time_ms": time_ms,
        "observed_white": label,
        "p_white": {
            "v7_raw": base,
            "v7_smoothed_alpha_one": base if smoothed is None else smoothed,
            "beta_half": half, "beta_one": one,
        },
    }


def test_fixed_internal_alpha_is_not_reselected_on_external_rows():
    rows = [
        _row("w1", 100, False, 0.2, 0.9, 0.1),
        _row("w1", 200, True, 0.3, 0.1, 0.8),
    ]
    chosen_one = external.evaluate_fixed_external_v8(rows, selected_candidate="beta_one")
    with pytest.raises(ValueError, match="selected alpha=1"):
        external.evaluate_fixed_external_v8(rows, selected_candidate="beta_half")
    assert chosen_one["status"] == "PASS_EXTERNAL_ONE_SHOT"
    assert chosen_one["selected_candidate_from_internal"] == "beta_one"
    assert chosen_one["gate_comparator"] == "v7_smoothed_alpha_one"
    assert chosen_one["alpha_reselected_on_external"] is False
    assert chosen_one["opportunity_count"] == 2


def test_external_scoring_rejects_wrong_raid_and_missing_rows():
    with pytest.raises(ValueError, match="no eligible opportunities"):
        external.evaluate_fixed_external_v8([], selected_candidate="beta_one")
    with pytest.raises(ValueError, match="crossed raid"):
        external.evaluate_fixed_external_v8(
            [{**_row("w1", 100, True, 0.1, 0.2, 0.3), "instance_id": "wrong"}],
            selected_candidate="beta_one",
        )


def test_zero_likelihood_v7_is_reportable_not_json_infinity():
    result = external.evaluate_fixed_external_v8(
        [_row("w1", 100, True, 0.0, 0.5, 0.6, smoothed=0.1)],
        selected_candidate="beta_one",
    )
    assert result["metrics"]["v7_raw"]["binary_zero_likelihood"] is True
    assert result["metrics"]["v7_raw"]["binary_white_mean_nll"] is None
    json.dumps(result, allow_nan=False)


def test_equal_smoothing_can_fail_while_raw_zero_likelihood_would_pass():
    result = external.evaluate_fixed_external_v8(
        [_row("w1", 100, True, 0.0, 0.5, 0.6, smoothed=0.9)],
        selected_candidate="beta_one",
    )
    assert result["metrics"]["v7_raw"]["binary_zero_likelihood"] is True
    assert result["gates"]["binary_white_nll_improved"] is False
    assert result["status"] == "FAIL_EXTERNAL_ONE_SHOT"


def test_smoothed_v7_uses_selected_v7_context_counts(monkeypatch):
    context = ("CLASS", "WARRIOR")
    model = SimpleNamespace(
        mark_counts={context: Counter({external.WHITE_TOKEN: 3, "other": 5})},
        minimums={"CLASS": 5},
    )
    monkeypatch.setattr(external.response, "_context_keys", lambda *a: [context])
    monkeypatch.setattr(external, "score_opportunity_v8", lambda **k: {
        "p_white": {"v7_raw": 3 / 8, "beta_one": 0.5}
    })
    scored = external._score_external_opportunity(
        row={"actor": {}, "emission_state_before_current_event": {}},
        v7_fit_model=model, v8_fit_white_counts={},
        instance_id=external.EXTERNAL_ID, wave_id="w1",
    )
    assert scored["p_white"]["v7_smoothed_alpha_one"] == 4 / 10


def test_stage5_uses_existing_kernel_with_explicit_external_nontraining_lane(
    monkeypatch, tmp_path: Path,
):
    entry = {
        "instance_id": external.EXTERNAL_ID,
        "partition": {"path": "synthetic-timeline.jsonl.gz"},
        "source_binding": {"synthetic": True},
        "instance_provenance": {
            "temporal_and_guild_provenance": {
                "started_at": "2026-09-18T14:28:20Z",
                "contamination": {"label": "POSTFIX_KNOWN_CLEAN", "guild_context": "诸神夜"},
            }
        },
    }
    timeline = tmp_path / "manifest.json"
    observed = {}

    def fake_timeline_loader(*args, **kwargs):
        observed["timeline_options"] = kwargs
        return {"instance_order": [external.EXTERNAL_ID], "instances": [entry]}, timeline

    monkeypatch.setattr(
        external.timeline_v2, "load_external_team_timeline_manifest",
        fake_timeline_loader,
    )

    def fake_kernel(context, *, output_directory, wave_workers=1):
        observed["context"] = context
        observed["wave_workers"] = wave_workers
        temporary = output_directory / "synthetic.tmp"
        temporary.write_bytes(b"synthetic stage5")
        return SimpleNamespace(
            temporary_path=temporary,
            final_path=output_directory / "synthetic.jsonl.gz",
            compressed_file_sha256="unused",
            manifest_entry={"summary": {"wave_count": 3}},
        )

    monkeypatch.setattr(external.stage5_v2, "_build_partition", fake_kernel)
    result = external.build_single_stage5(
        timeline_manifest=timeline, output_directory=tmp_path / "stage5",
    )
    lane = observed["context"].contamination
    assert observed["timeline_options"] == {"verify_inputs": False, "verify_partitions": False}
    assert lane["candidate_filter_passed"] is False
    assert lane["cohort_assignment"] == "EXTERNAL_HOLDOUT"
    assert lane["voting_authorized"] is False
    assert result["wave_count"] == 3
    assert result["wave_workers"] == 1
    assert Path(result["stage5_partition"]).read_bytes() == b"synthetic stage5"
    parallel = external.build_single_stage5(
        timeline_manifest=timeline, output_directory=tmp_path / "parallel-stage5",
        wave_workers=3,
    )
    assert parallel["wave_workers"] == observed["wave_workers"] == 3
    with pytest.raises(FileExistsError):
        external.build_single_stage5(
            timeline_manifest=timeline, output_directory=tmp_path / "stage5",
        )
    assert Path(result["stage5_partition"]).read_bytes() == b"synthetic stage5"


def test_external_score_requires_passed_internal_selection_before_fit_read(tmp_path: Path):
    split = tmp_path / "split.json"
    selection = tmp_path / "selection.json"
    split.write_text(json.dumps({"selection_instance_ids": ["s1"]}), encoding="utf-8")
    selection.write_text(json.dumps({
        "status": "FAIL_INTRA_COMPONENT_DEVELOPMENT_GATE",
        "selection_instance_ids": ["s1"], "selected_candidate": "beta_half",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="has not passed"):
        external.score_single_external(
            stage5_partition=tmp_path / "unopened-stage5.gz", split_path=split,
            fit_merged_path=tmp_path / "unopened-fit.gz",
            selection_result_path=selection, output_path=tmp_path / "never.json",
        )


def test_whole_wave_map_reduce_matches_serial_exactly():
    rows = [
        _row("w2", 100, False, 0.1, 0.1, 0.2),
        _row("w2", 200, True, 0.0, 0.7, 0.6),
        _row("w1", 10, False, 0.3, 0.2, 0.4),
        _row("w1", 10000, True, 0.2, 0.6, 0.5),
        _row("w3", 200, False, 0.2, 0.3, 0.1),
    ]
    serial = external.evaluate_fixed_external_v8(rows, selected_candidate="beta_one")
    shards = [
        external._summarize_external_rows(rows[:2], selected_candidate="beta_one"),
        external._summarize_external_rows(rows[2:4], selected_candidate="beta_one"),
        external._summarize_external_rows(rows[4:], selected_candidate="beta_one"),
    ]
    parallel = external._merge_external_shard_summaries(
        reversed(shards), selected_candidate="beta_one",
    )
    assert parallel == serial
    assert parallel["metrics"]["v7_raw"]["binary_zero_likelihood"] is True
    with pytest.raises(ValueError, match="repeated external wave"):
        external._merge_external_shard_summaries(
            [shards[0], shards[0]], selected_candidate="beta_one",
        )


def test_stage5_sharding_keeps_complete_wave_records(tmp_path: Path):
    source = tmp_path / "stage5.jsonl.gz"
    records = [json.dumps({"wave": {"wave_id": f"w{i}"}}) + "\n" for i in range(5)]
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.writelines(records)
    paths = external._split_stage5_waves(source, tmp_path, workers=3)
    assert len(paths) == 3
    observed = []
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            observed.extend(handle.readlines())
    assert sorted(observed) == sorted(records)


def test_parallel_workers_are_explicit_and_positive(tmp_path: Path):
    with pytest.raises(ValueError, match="positive"):
        external.score_single_external(
            stage5_partition=tmp_path / "unopened-stage5.gz",
            split_path=tmp_path / "unopened-split.json",
            fit_merged_path=tmp_path / "unopened-fit.gz",
            selection_result_path=tmp_path / "unopened-selection.json",
            output_path=tmp_path / "never.json", workers=0,
        )


@pytest.mark.skipif(os.name != "posix", reason="forked scoring runs on Linux HPC nodes")
def test_forked_shards_match_serial_scoring_with_synthetic_waves(monkeypatch, tmp_path: Path):
    records = [
        {"wave": {"instance_id": external.EXTERNAL_ID, "wave_id": "w2"},
         "rows": [_row("w2", 100, False, 0.1, 0.2, 0.3)]},
        {"wave": {"instance_id": external.EXTERNAL_ID, "wave_id": "w1"},
         "rows": [_row("w1", 100, True, 0.2, 0.5, 0.8)]},
    ]
    source = tmp_path / "stage5.jsonl.gz"
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    monkeypatch.setattr(
        external.response, "iter_wave_response_sufficient_rows_v1",
        lambda record: record["rows"],
    )
    monkeypatch.setattr(
        external, "_score_external_opportunity", lambda **kwargs: kwargs["row"],
    )
    serial = external._merge_external_shard_summaries([
        external._score_stage5_shard(
            source, v7_fit_model=object(), v8_fit_white_counts={},
            selected_candidate="beta_one",
        )
    ], selected_candidate="beta_one")
    paths = external._split_stage5_waves(source, tmp_path, workers=2)
    monkeypatch.setattr(external, "_FORK_MODEL", object())
    monkeypatch.setattr(external, "_FORK_WHITE_COUNTS", {})
    monkeypatch.setattr(external, "_FORK_CANDIDATE", "beta_one")
    with external.ProcessPoolExecutor(
        max_workers=2, mp_context=external.multiprocessing.get_context("fork"),
    ) as executor:
        shards = list(executor.map(external._score_forked_stage5_shard, paths))
    parallel = external._merge_external_shard_summaries(
        shards, selected_candidate="beta_one",
    )
    assert parallel == serial


def test_compact_projection_is_preferred_without_opening_full_fit(monkeypatch, tmp_path: Path):
    from o2o_dps import development_white6603_selection_eval_v8 as selection_eval

    split = tmp_path / "split.json"
    selection = tmp_path / "selection.json"
    projection = tmp_path / "compact.json"
    output = tmp_path / "result.json"
    split.write_text(json.dumps({
        "fit_instance_ids": ["fit-1"], "selection_instance_ids": ["select-1"],
    }), encoding="utf-8")
    selection.write_text(json.dumps({
        "status": "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE",
        "selection_instance_ids": ["select-1"],
        "selected_candidate": "beta_one",
    }), encoding="utf-8")
    projection.write_text(json.dumps({
        "schema": selection_eval.FIT_SCORING_SCHEMA,
        "fit_instance_ids": ["fit-1"],
    }), encoding="utf-8")
    observed = {}

    def fake_loader(document):
        observed["loaded"] = document["fit_instance_ids"]
        return object(), {}

    monkeypatch.setattr(selection_eval, "load_fit_scoring_v8", fake_loader)
    monkeypatch.setattr(external, "_score_stage5_shard", lambda *a, **k:
        external._summarize_external_rows(
            [_row("w1", 100, True, 0.1, 0.4, 0.9)],
            selected_candidate="beta_one",
        ))
    result = external.score_single_external(
        stage5_partition=tmp_path / "not-opened-stage5.gz", split_path=split,
        fit_projection_path=projection, selection_result_path=selection,
        output_path=output,
    )
    assert observed == {"loaded": ["fit-1"]}
    assert result["fit_task_count"] == 1
    assert output.exists()
