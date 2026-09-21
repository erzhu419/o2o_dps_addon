from __future__ import annotations

import json

import pytest

from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps.development_white6603_joint_training_v8 import JointWhiteTrainingV8
from o2o_dps.development_white6603_runtime_head_artifact_v8 import (
    build_selected_white6603_head_artifact_v8,
    load_selected_white6603_head_v8,
)
from tests.test_development_white6603_joint_training_v8 import _white_wave


def _fit_and_selection(candidate: str) -> tuple[dict, dict, JointWhiteTrainingV8]:
    trainer = JointWhiteTrainingV8()
    trainer.begin_wave()
    for row in response_v1.iter_wave_response_sufficient_rows_v1(_white_wave()):
        trainer.update(row)
    fit = trainer.serialize()
    fit["fit_instance_ids"] = ["fit-a"]
    selection = {
        "schema": "development_white6603_v8_teacher_forced_selection/v1",
        "status": "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE",
        "selected_candidate": candidate,
        "fit_task_count": 1,
        "selection_instance_ids": ["selection-b"],
        "fit_count_rows": trainer.row_count,
        "fit_count_white": trainer.white_count,
    }
    return fit, selection, trainer


@pytest.mark.parametrize("candidate,alpha", [("beta_half", 0.5), ("beta_one", 1.0)])
def test_fit_only_selected_head_roundtrips_exact_counts_and_alpha(
    tmp_path, candidate: str, alpha: float
) -> None:
    fit, selection, trainer = _fit_and_selection(candidate)
    artifact = build_selected_white6603_head_artifact_v8(fit, selection)
    assert "joint_dynamic_training_counts" not in artifact
    assert len(json.dumps(artifact)) < len(json.dumps(fit))
    assert artifact["alpha"] == alpha
    path = tmp_path / "selected-white-head.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    loaded = load_selected_white6603_head_v8(path)
    assert loaded.alpha == alpha
    assert loaded.counts == trainer.white_counts
    assert loaded.minimums == artifact["minimums"]


def test_failed_inner_selection_cannot_export_runtime_head() -> None:
    fit, selection, _ = _fit_and_selection("beta_half")
    selection["status"] = "FAIL_INTRA_COMPONENT_DEVELOPMENT_GATE"
    with pytest.raises(ValueError, match="did not pass"):
        build_selected_white6603_head_artifact_v8(fit, selection)
