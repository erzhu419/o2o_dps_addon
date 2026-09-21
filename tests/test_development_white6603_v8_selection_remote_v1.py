import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import development_white6603_v8_selection_remote_v1 as runner
from o2o_dps.development_white6603_runtime_head_artifact_v8 import load_selected_white6603_head_v8


FROZEN = Path(__file__).resolve().parents[1] / "configs/evaluation/development_white6603_v8_inner_selection.json"


def _fixture(tmp_path, monkeypatch):
    split = json.loads(FROZEN.read_text(encoding="utf-8"))
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split), encoding="utf-8")
    tasks = [
        {"instance_id": instance_id, "component_id": split["train_component_id"],
         "split": "TRAIN", "partition_locator": f"shards/{instance_id}.jsonl.gz"}
        for instance_id in split["fit_instance_ids"] + split["selection_instance_ids"]
    ] + [
        {**item, "split": "VALIDATION"}
        for item in split["external_validation_tasks"]
    ]
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    fit_path = tmp_path / "fit-only.joint-white.json.gz"
    with gzip.open(fit_path, "wt", encoding="utf-8") as handle:
        json.dump({"fit_instance_ids": split["fit_instance_ids"]}, handle)
    for instance_id in split["selection_instance_ids"]:
        path = tmp_path / "shards" / f"{instance_id}.jsonl.gz"
        path.parent.mkdir(exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump({"wave": {"instance_id": instance_id, "wave_id": "wave-1"},
                       "rows": [{
                           "instance_id": instance_id, "wave_id": "wave-1",
                           "time_ms": 1000, "actor_class": "WARRIOR",
                           "actor_spec": "WARRIOR_FURY", "observed_white": True,
                           "p_white": {"v7_raw": 0.1, "beta_half": 0.5, "beta_one": 0.4},
                           "p_nonwhite_token_given_nonwhite": None,
                       }]}, handle)
            handle.write("\n")
    monkeypatch.setattr(runner.JointWhiteTrainingV8, "deserialize", lambda _doc: SimpleNamespace(
        joint=object(), white_counts={}, row_count=100, white_count=20,
    ))
    monkeypatch.setattr(runner.hpc, "_JointEvaluationModelViewV4", lambda *_args: object())
    monkeypatch.setattr(runner.response, "iter_wave_response_sufficient_rows_v1", lambda record: iter(record["rows"]))
    monkeypatch.setattr(runner, "score_opportunity_v8", lambda *, row, **_kwargs: row)
    return split_path, dispatch_path, fit_path, split


def test_remote_runner_streams_only_selection_and_writes_small_aggregate(tmp_path, monkeypatch):
    split_path, dispatch_path, fit_path, split = _fixture(tmp_path, monkeypatch)
    output_path = tmp_path / "summary.json"
    result = runner.run(
        split_path=split_path, dispatch_path=dispatch_path,
        fit_merged_path=fit_path, data_root=tmp_path, output_path=output_path,
    )
    assert result["status"] == "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
    assert result["opportunity_count"] == 13
    assert result["fit_task_count"] == 52
    assert result["selection_instance_ids"] == sorted(split["selection_instance_ids"])
    assert json.loads(output_path.read_text(encoding="utf-8"))["selected_candidate"] == "beta_half"


def test_passed_selection_exports_small_fit_only_head_without_second_fit_read(tmp_path, monkeypatch):
    split_path, dispatch_path, fit_path, split = _fixture(tmp_path, monkeypatch)
    with gzip.open(fit_path, "wt", encoding="utf-8") as handle:
        json.dump({
            "schema": "development_white6603_joint_training/v8",
            "fit_instance_ids": split["fit_instance_ids"],
            "row_count": 100,
            "white_count": 20,
            "white_opportunity_counts": [
                {"context": ["GLOBAL", "FIRST"], "white": 20, "nonwhite": 80},
            ],
        }, handle)
    head_path = tmp_path / "selected-white-head.json"
    result = runner.run(
        split_path=split_path, dispatch_path=dispatch_path,
        fit_merged_path=fit_path, data_root=tmp_path,
        output_path=tmp_path / "summary.json", head_output_path=head_path,
    )
    assert result["status"] == "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
    head = load_selected_white6603_head_v8(head_path)
    assert head.alpha == 0.5
    assert head.counts[("GLOBAL", "FIRST")][True] == 20


def test_remote_runner_rejects_fit_merge_that_includes_selection(tmp_path, monkeypatch):
    split_path, dispatch_path, fit_path, split = _fixture(tmp_path, monkeypatch)
    with gzip.open(fit_path, "wt", encoding="utf-8") as handle:
        json.dump({"fit_instance_ids": split["fit_instance_ids"] + [split["selection_instance_ids"][0]]}, handle)
    with pytest.raises(ValueError, match="fit-only merged counts"):
        runner.run(
            split_path=split_path, dispatch_path=dispatch_path,
            fit_merged_path=fit_path, data_root=tmp_path,
            output_path=tmp_path / "never-written.json",
        )


@pytest.mark.parametrize("shard_count", [6, 13])
def test_parallel_shards_reduce_to_same_serial_selection(tmp_path, monkeypatch, shard_count):
    split_path, dispatch_path, fit_path, split = _fixture(tmp_path, monkeypatch)
    projection_path = tmp_path / "fit-only.score-projection.json"
    projection_path.write_text(json.dumps({
        "fit_instance_ids": split["fit_instance_ids"],
        "row_count": 100, "white_count": 20, "v8_white_counts": [],
    }), encoding="utf-8")
    monkeypatch.setattr(runner, "load_fit_scoring_v8", lambda _projection: (object(), {}))
    serial = runner.run(
        split_path=split_path, dispatch_path=dispatch_path,
        fit_merged_path=fit_path, data_root=tmp_path,
        output_path=tmp_path / "serial.json",
    )
    paths = []
    for index in reversed(range(shard_count)):
        path = tmp_path / f"part-{index}.json"
        runner.run_shard(
            split_path=split_path, dispatch_path=dispatch_path,
            fit_projection_path=projection_path, data_root=tmp_path,
            output_path=path, shard_index=index, shard_count=shard_count,
        )
        paths.append(path)
    reduced = runner.run_reduce(
        split_path=split_path, dispatch_path=dispatch_path,
        fit_projection_path=projection_path, partial_paths=paths,
        output_path=tmp_path / "reduced.json",
    )
    for key in ("status", "selected_candidate", "opportunity_count", "wave_count", "metrics", "gates"):
        assert reduced[key] == serial[key]
    with pytest.raises(ValueError, match="source or partition"):
        runner.run_reduce(
            split_path=split_path, dispatch_path=dispatch_path,
            fit_projection_path=projection_path, partial_paths=paths[:-1] + [paths[0]],
            output_path=tmp_path / "never-written.json",
        )
