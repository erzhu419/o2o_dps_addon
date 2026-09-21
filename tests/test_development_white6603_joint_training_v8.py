from __future__ import annotations

from copy import deepcopy
import gzip
import json

import pytest

from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.development_white6603_joint_training_v8 import JointWhiteTrainingV8
from o2o_dps.development_white6603_selection_eval_v8 import (
    load_fit_scoring_v8, project_fit_scoring_v8,
)
from o2o_dps.development_white6603_opportunity_v8 import (
    White6603OpportunityClockV8,
)
from tests.test_chronicle_external_teammate_response_model_v1 import (
    ACTOR, TARGET_B, _event, _wave,
)
from scripts.development_white6603_merge_fit_v8 import (
    merge_fit, merge_fit_projections, project_fit_shard, write_fit_merge,
)


def _white_wave() -> dict:
    wave = deepcopy(_wave())
    wave["exact_trace"].extend([
        _event(7, 500, ACTOR, "DMG", TARGET_B, spell_id=6603, damage=31),
        _event(8, 600, ACTOR, "DMG", TARGET_B, spell_id=6603, damage=23),
    ])
    wave["exact_trace"][8]["event"]["target"]["lane"] = "FRIENDLY_PLAYER"
    wave["exact_trace"][8]["event"]["target"]["voting_enemy_target"] = False
    return wave


def test_joint_and_white_training_single_pass_roundtrip() -> None:
    wave = _white_wave()
    trainer = JointWhiteTrainingV8()
    trainer.begin_wave()
    clock = White6603OpportunityClockV8()
    rows = list(response_v1.iter_wave_response_sufficient_rows_v1(wave))
    for row in rows:
        assert trainer.update(row) == clock.observe(row)
    assert trainer.row_count == len(rows)
    assert trainer.white_count == 2
    serialized = trainer.serialize()
    loaded = JointWhiteTrainingV8.deserialize(serialized)
    assert loaded.serialize() == serialized
    assert loaded.white_counts[("GLOBAL", "FIRST")][True] == 1
    assert loaded.white_counts[("GLOBAL", "REPEAT")][True] == 1
    assert all(ACTOR not in context for context in loaded.white_counts)


def test_wave_reset_and_joint_white_count_mismatch_is_rejected() -> None:
    trainer = JointWhiteTrainingV8()
    first_rows = list(response_v1.iter_wave_response_sufficient_rows_v1(_white_wave()))
    with pytest.raises(ValueError, match="begin_wave"):
        trainer.update(first_rows[0])
    for _ in range(2):
        trainer.begin_wave()
        for row in first_rows:
            trainer.update(row)
    assert trainer.wave_count == 2
    assert trainer.white_count == 4
    trainer.white_counts[("GLOBAL", "FIRST")][True] += 1
    trainer.white_counts[("GLOBAL", "FIRST")][False] -= 1
    with pytest.raises(ValueError, match="positives differ"):
        trainer.serialize()


def test_fit_merge_keeps_selection_out_and_records_exact_ids(tmp_path, monkeypatch) -> None:
    trainer = JointWhiteTrainingV8()
    trainer.begin_wave()
    for row in response_v1.iter_wave_response_sufficient_rows_v1(_white_wave()):
        trainer.update(row)
    shard = trainer.serialize()
    fit_ids = [f"fit-{index:02d}" for index in range(52)]
    selection_ids = [f"selection-{index:02d}" for index in range(13)]
    (tmp_path / "fit").mkdir()
    for instance_id in fit_ids:
        prefix = tmp_path / "fit" / instance_id
        prefix.with_suffix(".summary.json").write_text(json.dumps({
            "status": "PASS", "instance_id": instance_id,
            "compiled_row_count": trainer.row_count,
            "v8_direct_white6603_mark_count": trainer.white_count,
        }), encoding="utf-8")
        with gzip.open(prefix.with_suffix(".joint-white.json.gz"), "wt", encoding="utf-8") as handle:
            json.dump(shard, handle)
    merged = merge_fit(tmp_path, {
        "fit_instance_ids": fit_ids, "selection_instance_ids": selection_ids,
    })
    assert merged["fit_instance_ids"] == fit_ids
    restored = JointWhiteTrainingV8.deserialize(merged)
    assert restored.row_count == 52 * trainer.row_count
    assert restored.white_count == 52 * trainer.white_count
    assert restored.wave_count == 52
    actual_open = gzip.open
    modes = []

    def tracked_open(path, mode, **kwargs):
        modes.append(mode)
        return actual_open(path, mode, **kwargs)

    monkeypatch.setattr(gzip, "open", tracked_open)
    receipt = write_fit_merge(tmp_path, merged)
    assert modes == ["wt"]
    assert receipt["fit_task_count"] == 52
    assert receipt["row_count"] == restored.row_count


def test_compact_projection_shard_map_reduce_matches_full_joint_mark_view(tmp_path) -> None:
    trainer = JointWhiteTrainingV8()
    trainer.begin_wave()
    for row in response_v1.iter_wave_response_sufficient_rows_v1(_white_wave()):
        trainer.update(row)
    shard = trainer.serialize()
    fit_ids = [f"fit-{index:02d}" for index in range(52)]
    split = {"fit_instance_ids": fit_ids, "selection_instance_ids": [f"sel-{index:02d}" for index in range(13)]}
    (tmp_path / "fit").mkdir()
    for instance_id in fit_ids:
        prefix = tmp_path / "fit" / instance_id
        prefix.with_suffix(".summary.json").write_text(json.dumps({
            "status": "PASS", "instance_id": instance_id,
            "compiled_row_count": trainer.row_count,
            "v8_direct_white6603_mark_count": trainer.white_count,
        }), encoding="utf-8")
        with gzip.open(prefix.with_suffix(".joint-white.json.gz"), "wt", encoding="utf-8") as handle:
            json.dump(shard, handle)
        project_fit_shard(tmp_path, split, instance_id)
    receipt = merge_fit_projections(tmp_path, split)
    projected = json.loads((tmp_path / "fit-only.score-projection.json").read_text(encoding="utf-8"))
    full = merge_fit(tmp_path, split)
    direct = project_fit_scoring_v8(full)
    assert receipt["fit_task_count"] == 52
    assert projected == direct
    projected_model, white_counts = load_fit_scoring_v8(projected)
    assert projected_model.mark_counts[("GLOBAL",)] == {
        token: count * 52 for token, count in trainer.joint.base_c.mark_counts[("GLOBAL",)].items()
    }
    assert sum(value[True] for key, value in white_counts.items() if key[0] == "GLOBAL") == 52 * trainer.white_count
