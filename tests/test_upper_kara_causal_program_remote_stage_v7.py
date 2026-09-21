from __future__ import annotations

import json
from pathlib import Path

from scripts.build_upper_kara_two_wave_sequence_campaign_v1 import (
    build_upper_kara_two_wave_sequence_campaign_v1,
)
from scripts.build_upper_kara_cat_action_plan_residual_campaign_v8 import (
    build_upper_kara_cat_action_plan_residual_campaign_v8,
)
from scripts.upper_kara_causal_program_remote_stage_v1 import (
    DEFAULT_SCENARIO_CAPSULE,
    build_upper_kara_causal_program_stage_plan_v1,
)


def test_v7_stage_closure_includes_only_the_pinned_derived_capsule(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps(
            build_upper_kara_two_wave_sequence_campaign_v1().to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bridge = tmp_path / "bridge.linux-amd64"
    binding = tmp_path / "binding.json"
    database = tmp_path / "db.json"
    for path in (bridge, binding, database):
        path.write_text("{}", encoding="utf-8")

    plan = build_upper_kara_causal_program_stage_plan_v1(
        shared_home="/home/tester",
        run_id="v7-stage-smoke",
        bridge=bridge,
        campaign=campaign,
        runtime_binding=binding,
        item_database=database,
    )

    capsule_rows = [
        row for row in plan["copy_specs"] if row.get("derived_scenario_capsule")
    ]
    assert len(capsule_rows) == 1
    assert Path(capsule_rows[0]["source"]) == DEFAULT_SCENARIO_CAPSULE.resolve()
    assert capsule_rows[0]["destination"].endswith(DEFAULT_SCENARIO_CAPSULE.name)
    assert plan["derived_scenario_capsule_staged"] is True
    assert plan["derived_scenario_capsule_bytes"] == DEFAULT_SCENARIO_CAPSULE.stat().st_size
    assert plan["raw_offline_data_staged"] is False
    assert plan["training_task_count"] == 1_024
    assert plan["evaluation_task_count"] == 256
    assert plan["teacher_task_count"] == 0
    assert plan["distill_task_count"] == 0


def test_v8_stage_closure_includes_the_same_pinned_capsule(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign-v8.json"
    campaign.write_text(
        json.dumps(
            build_upper_kara_cat_action_plan_residual_campaign_v8().to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bridge = tmp_path / "bridge.linux-amd64"
    binding = tmp_path / "binding.json"
    database = tmp_path / "db.json"
    for path in (bridge, binding, database):
        path.write_text("{}", encoding="utf-8")

    plan = build_upper_kara_causal_program_stage_plan_v1(
        shared_home="/home/tester",
        run_id="v8-stage-smoke",
        bridge=bridge,
        campaign=campaign,
        runtime_binding=binding,
        item_database=database,
    )

    capsule_rows = [
        row for row in plan["copy_specs"] if row.get("derived_scenario_capsule")
    ]
    assert len(capsule_rows) == 1
    assert Path(capsule_rows[0]["source"]) == DEFAULT_SCENARIO_CAPSULE.resolve()
    assert plan["derived_scenario_capsule_staged"] is True
    assert plan["raw_offline_data_staged"] is False
    assert plan["training_task_count"] == 1_024
    assert plan["evaluation_task_count"] == 256
    assert plan["teacher_task_count"] == 512
    assert plan["distill_task_count"] == 4
