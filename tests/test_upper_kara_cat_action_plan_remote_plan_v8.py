from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path

import pytest

from scripts.build_upper_kara_cat_action_plan_residual_campaign_v8 import (
    build_upper_kara_cat_action_plan_residual_campaign_v8,
)
import scripts.upper_kara_causal_program_remote_submit_v1 as submit


@pytest.fixture()
def campaign_path(tmp_path: Path) -> Path:
    path = tmp_path / "campaign-v8.json"
    path.write_text(
        json.dumps(
            build_upper_kara_cat_action_plan_residual_campaign_v8().to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _plan(campaign_path: Path, phase: str) -> dict:
    return submit.build_upper_kara_causal_program_remote_plan_v8(
        shared_home="/home/tester",
        run_id="v8-plan-smoke",
        phase=phase,
        campaign=campaign_path,
        bridge_name="bridge-v8.linux-amd64",
    )


def test_v8_plan_has_frozen_six_phase_topology_and_all_nodes(
    campaign_path: Path,
) -> None:
    plans = {phase: _plan(campaign_path, phase) for phase in submit.PHASES_V8}
    assert {phase: plan["task_count"] for phase, plan in plans.items()} == {
        "teacher": 512,
        "distill": 4,
        "train": 1_024,
        "freeze": 1,
        "eval": 256,
        "summarize": 1,
    }
    assert all(
        plan["phase_contract"]["order"] == list(submit.PHASES_V8)
        for plan in plans.values()
    )

    teacher = plans["teacher"]
    assert all(row["cpu"] == 2 for row in teacher["task_specs"])
    assert all("--teacher-workers 2" in row["cmd"] for row in teacher["task_specs"])
    assert all(submit.WORKER_MODULE_V8 in row["cmd"] for row in teacher["task_specs"])
    assert {row["preferred_node"] for row in teacher["task_specs"]} == {
        f"node{index:03d}" for index in range(1, 7)
    }

    distill = plans["distill"]
    assert len(distill["semantic_prerequisites"]) == 512
    assert all(
        row["role"] == "V8_TEACHER_RESULT"
        for row in distill["semantic_prerequisites"]
    )
    assert all(" candidate " in row["cmd"] for row in distill["task_specs"])
    assert len(distill["terminal_outputs"]) == 4
    assert len(distill["artifact_outputs"]) == 8

    train = plans["train"]
    assert len(train["semantic_prerequisites"]) == 8
    assert {
        row["role"] for row in train["semantic_prerequisites"]
    } == {"CANDIDATE_MANIFEST_V2", "V8_RESIDUAL_POLICY_BUNDLE"}
    assert all(row["cpu"] == 4 for row in train["task_specs"])
    assert all("--policy-bundle" in row["cmd"] for row in train["task_specs"])
    assert {row["preferred_node"] for row in train["task_specs"]} == {
        f"node{index:03d}" for index in range(1, 7)
    }

    freeze = plans["freeze"]
    assert sum(
        row["role"] == "TRAIN_COMMIT_V2"
        for row in freeze["semantic_prerequisites"]
    ) == 1_024
    assert sum(
        row["role"] == "CANDIDATE_MANIFEST_V2"
        for row in freeze["semantic_prerequisites"]
    ) == 4

    evaluation = plans["eval"]
    assert len(evaluation["task_specs"]) == 256
    assert all(row["cpu"] == 4 for row in evaluation["task_specs"])
    assert all(
        "--policy-bundle-root" in row["cmd"]
        for row in evaluation["task_specs"]
    )
    assert {row["preferred_node"] for row in evaluation["task_specs"]} == {
        f"node{index:03d}" for index in range(1, 7)
    }

    summary = plans["summarize"]
    assert sum(
        row["role"] == "EVAL_SHARD"
        for row in summary["semantic_prerequisites"]
    ) == 256
    assert plans["teacher"]["status"] == "PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED"
    assert all(
        plan["phase_contract"]["submit_performed"] is False
        and plan["phase_contract"]["dispatch_performed"] is False
        for plan in plans.values()
    )


class _InventoryScheduler:
    def state_lock(self, **_: object):
        return nullcontext()

    @staticmethod
    def load_state() -> dict:
        return {"tasks": []}


def _projection_for(plan: dict, spec: dict) -> dict:
    role = spec["role"]
    projection = {
        "schema": spec["schema"],
        "campaign_id": plan["campaign_id"],
        "build_id": plan["build_id"],
        "campaign_contract": plan["campaign_contract"],
        **spec.get("expected_fields", {}),
    }
    if role == "V8_TEACHER_RESULT":
        projection.update(
            {
                "status": spec["allowed_statuses"][-1],
                "simulator_seed": int(projection["master_seed"]) + 10_000,
                **spec.get("expected_nested_fields", {}),
            }
        )
    return projection


def test_distill_inventory_requires_complete_semantically_bound_teacher_panel(
    campaign_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(campaign_path, "distill")
    projections = {
        spec["path"]: _projection_for(plan, spec)
        for spec in plan["semantic_prerequisites"]
    }
    monkeypatch.setattr(
        submit,
        "_remote_missing_required_paths_v1",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        submit,
        "_remote_existing_paths_v1",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        submit,
        "_read_remote_terminal_projections_v1",
        lambda *_args, **_kwargs: (projections, {}),
    )

    ready = submit.inventory_upper_kara_causal_program_remote_v1(
        _InventoryScheduler(), plan
    )
    assert ready["status"] == "READY_TO_QUEUE"
    assert ready["semantic_terminal_projection_count"] == 512

    first = plan["semantic_prerequisites"][0]
    projections[first["path"]]["remote_teacher_task"] = {
        **projections[first["path"]]["remote_teacher_task"],
        "teacher_shard_index": 999,
    }
    blocked = submit.inventory_upper_kara_causal_program_remote_v1(
        _InventoryScheduler(), plan
    )
    assert blocked["status"] == "NOT_READY_TO_QUEUE"
    assert blocked["semantic_prerequisite_conflicts"] == [
        {
            "path": first["path"],
            "role": "V8_TEACHER_RESULT",
            "errors": [
                "NESTED_FIELD_MISMATCH:remote_teacher_task.teacher_shard_index"
            ],
        }
    ]


def test_v8_auto_dispatch_does_not_change_legacy_phase_contract(
    campaign_path: Path,
) -> None:
    auto = submit.build_upper_kara_causal_program_remote_plan_v1(
        shared_home="/home/tester",
        run_id="v8-auto-dispatch",
        phase="teacher",
        campaign=campaign_path,
        bridge_name="bridge-v8.linux-amd64",
    )
    assert auto["schema"] == "upper_kara_cat_action_plan_remote_plan/v8"
    with pytest.raises(ValueError, match="phase must be one of"):
        submit.build_upper_kara_causal_program_remote_plan_v1(
            shared_home="/home/tester",
            run_id="v8-auto-dispatch",
            phase="candidate",
            campaign=campaign_path,
            bridge_name="bridge-v8.linux-amd64",
        )
