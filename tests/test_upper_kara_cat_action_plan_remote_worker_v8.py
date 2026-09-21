from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from o2o_dps.causal_action_program_v1 import (
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_cat_action_plan_remote_worker_v8 import (
    generate_candidate_manifest_v8,
    residual_policy_bundle_name_v8,
    run_evaluation_shard_v8,
    run_teacher_plan_shard_v8,
    run_training_shard_v8,
    teacher_terminal_name_v8,
)
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import (
    SCHEMA as TEACHER_SCHEMA,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    assign_teacher_plan_shards_v8,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    FREEZE_TERMINAL_SCHEMA,
    RemoteCausalProgramRuntimeV1,
    _campaign_contract_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v2 import (
    candidate_manifest_name_v2,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    imported_incumbent_programs_v1,
)
from scripts.build_upper_kara_cat_action_plan_residual_campaign_v8 import (
    build_upper_kara_cat_action_plan_residual_campaign_v8,
)


def _write_campaign(root: Path):
    campaign = build_upper_kara_cat_action_plan_residual_campaign_v8(
        campaign_id="v8-remote-worker-test"
    )
    path = root / "campaign.json"
    path.write_text(
        json.dumps(campaign.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return campaign, path


def _case_runtime() -> RemoteCausalProgramRuntimeV1:
    def build(seed: int, **_: object):
        return SimpleNamespace(
            dynamic_load=SimpleNamespace(
                seed=seed,
                request_sha256=f"{seed:064x}",
            )
        )

    def forbidden(*_: object, **__: object):
        raise AssertionError("not used")

    return RemoteCausalProgramRuntimeV1(
        case_builder=build,
        searched_program_generator=forbidden,
        program_replay_factory=forbidden,
    )


def test_teacher_task_uses_common_derived_simulator_seed(tmp_path: Path) -> None:
    campaign, campaign_path = _write_campaign(tmp_path)
    loadout_id = BURST_LOADOUT_IDS_V1[0]
    shard = next(
        row
        for row in assign_teacher_plan_shards_v8(campaign)
        if row.loadout_id == loadout_id and row.teacher_shard_index == 0
    )
    observed: dict[str, object] = {}

    def fake_teacher(case, **kwargs):
        observed.update(kwargs)
        return {
            "schema": TEACHER_SCHEMA,
            "master_seed": case.dynamic_load.seed,
            "simulator_seed": kwargs["simulator_seed"],
            "build_id": kwargs["build_id"],
            "loadout_id": kwargs["loadout_id"],
            "status": "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING",
            "baseline_terminal": {"status": "COMPLETED"},
            "branches": [],
        }

    output = tmp_path / "teacher.json"
    result = run_teacher_plan_shard_v8(
        campaign_path=campaign_path,
        loadout_id=loadout_id,
        teacher_shard_index=0,
        output_path=output,
        bridge_path=tmp_path,
        bridge_cwd=tmp_path,
        runtime_binding_path=tmp_path,
        case_runtime=_case_runtime(),
        teacher_runner=fake_teacher,
    )

    expected = derive_simulator_seed(
        shard.example.seed,
        f"{shard.example.seed:064x}",
        namespace=PROTOCOL_ID,
    )
    assert observed["simulator_seed"] == expected
    assert observed["max_states"] == 3
    assert observed["max_plans_per_state"] == 64
    assert observed["branch_workers"] == 2
    assert result["master_seed"] == shard.example.seed
    assert result["simulator_seed"] == expected
    assert result["remote_teacher_task"] == {
        "campaign_id": campaign.campaign_id,
        "work_id": shard.work_id,
        "teacher_shard_index": 0,
        "simulator_seed": expected,
        "first_wave_arrival_ms": shard.example.first_wave_arrival_ms,
        "proposal_cohort_only": True,
        "selection_or_heldout_seed_used": False,
    }
    assert json.loads(output.read_text(encoding="utf-8")) == result


def _write_empty_teacher_panel(
    root: Path, campaign, loadout_id: str
) -> None:
    shards = [
        row
        for row in assign_teacher_plan_shards_v8(campaign)
        if row.loadout_id == loadout_id
    ]
    assert len(shards) == 128
    for shard in shards:
        payload = {
            "schema": TEACHER_SCHEMA,
            "status": "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING",
            "master_seed": shard.example.seed,
            "simulator_seed": shard.example.seed + 10_000_000,
            "build_id": campaign.build_id,
            "loadout_id": loadout_id,
            "baseline_terminal": {"status": "COMPLETED"},
            "branches": [],
            "remote_teacher_task": {
                "campaign_id": campaign.campaign_id,
                "work_id": shard.work_id,
                "teacher_shard_index": shard.teacher_shard_index,
                "simulator_seed": shard.example.seed + 10_000_000,
                "first_wave_arrival_ms": (
                    shard.example.first_wave_arrival_ms
                ),
                "proposal_cohort_only": True,
                "selection_or_heldout_seed_used": False,
            },
        }
        (root / teacher_terminal_name_v8(shard)).write_text(
            json.dumps(payload), encoding="utf-8"
        )


def _materialize_zero_only_candidates(tmp_path: Path):
    campaign, campaign_path = _write_campaign(tmp_path)
    loadout_id = BURST_LOADOUT_IDS_V1[0]
    teacher_root = tmp_path / "teachers"
    teacher_root.mkdir()
    _write_empty_teacher_panel(teacher_root, campaign, loadout_id)
    candidate_root = tmp_path / "candidates"
    candidate_root.mkdir()
    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    manifest_path = candidate_root / candidate_manifest_name_v2(loadout_id)
    bundle_path = bundle_root / residual_policy_bundle_name_v8(loadout_id)
    manifest = generate_candidate_manifest_v8(
        campaign_path=campaign_path,
        teacher_root=teacher_root,
        loadout_id=loadout_id,
        policy_bundle_path=bundle_path,
        output_path=manifest_path,
    )
    return (
        campaign,
        campaign_path,
        loadout_id,
        manifest_path,
        bundle_root,
        bundle_path,
        manifest,
    )


def test_candidate_phase_reads_128_and_publishes_portable_exact_cat(
    tmp_path: Path,
) -> None:
    (
        campaign,
        _,
        loadout_id,
        _,
        _,
        bundle_path,
        manifest,
    ) = _materialize_zero_only_candidates(tmp_path)

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["campaign_id"] == campaign.campaign_id
    assert bundle["campaign_contract"] == _campaign_contract_v1(campaign)
    assert bundle["loadout_id"] == loadout_id
    assert bundle["accepted_proposal_teacher_result_count"] == 128
    assert bundle["max_nonzero_candidates"] == 63
    assert len(bundle["candidates"]) == 1
    assert bundle["candidates"][0]["kind"] == "EXACT_CAT_ZERO_RESIDUAL"
    assert bundle["candidates"][0]["policy_wire"]["steps"] == []

    assert manifest["campaign_contract"] == _campaign_contract_v1(campaign)
    assert len(manifest["programs"]) == 4
    programs = [
        causal_action_program_from_dict_v1(row["program"])
        for row in manifest["programs"]
    ]
    assert sum(
        row.origin is ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT
        for row in programs
    ) == 3
    searched = [
        row for row in programs if row.origin is ProgramOriginV1.SEARCHED_REACTIVE
    ]
    assert len(searched) == 1
    assert "cat-residual-sequence/v1:zero" in searched[0].source_refs


def test_train_and_eval_wrappers_restore_bundle_registry(
    tmp_path: Path,
) -> None:
    (
        campaign,
        campaign_path,
        loadout_id,
        manifest_path,
        bundle_root,
        bundle_path,
        manifest,
    ) = _materialize_zero_only_candidates(tmp_path)
    captured_train: dict[str, object] = {}

    def compact_runner(**kwargs):
        captured_train.update(kwargs)
        return {"terminal_status": "COMPLETE"}

    training = run_training_shard_v8(
        campaign_path=campaign_path,
        candidate_manifest_path=manifest_path,
        policy_bundle_path=bundle_path,
        loadout_id=loadout_id,
        seed_shard_index=0,
        lane_sidecar_path=tmp_path / "train.lanes.json",
        output_path=tmp_path / "train.json",
        bridge_path=tmp_path,
        bridge_cwd=tmp_path,
        runtime_binding_path=tmp_path,
        replay_workers=7,
        compact_runner=compact_runner,
    )
    assert training["terminal_status"] == "COMPLETE"
    assert captured_train["replay_workers"] == 7
    train_runtime = captured_train["runtime"]
    assert isinstance(train_runtime, RemoteCausalProgramRuntimeV1)
    assert len(train_runtime.incumbent_program_factory(campaign.build_id)) == 3

    searched_receipt = next(
        row
        for row in manifest["programs"]
        if row["program_origin"] == ProgramOriginV1.SEARCHED_REACTIVE.value
    )
    frozen_program = causal_action_program_from_dict_v1(
        searched_receipt["program"]
    )
    frozen_path = tmp_path / "frozen.json"
    frozen_path.write_text(
        json.dumps(
            {
                "schema": FREEZE_TERMINAL_SCHEMA,
                "terminal_status": "COMPLETE",
                "campaign_id": campaign.campaign_id,
                "build_id": campaign.build_id,
                "campaign_contract": _campaign_contract_v1(campaign),
                "winner": {
                    "loadout_id": loadout_id,
                    "program_ref": frozen_program.program_id,
                    "program_id": frozen_program.program_id,
                    "program_key": frozen_program.program_key(),
                    "program_origin": frozen_program.origin.value,
                },
                "frozen_program": frozen_program.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    captured_eval: dict[str, object] = {}

    def evaluation_runner(**kwargs):
        captured_eval.update(kwargs)
        return {
            "terminal_status": "COMPLETE",
            "baseline_policy_ids": [
                row.selector.source_policy_id
                for row in imported_incumbent_programs_v1(campaign.build_id)
            ],
        }

    evaluated = run_evaluation_shard_v8(
        campaign_path=campaign_path,
        frozen_path=frozen_path,
        policy_bundle_root=bundle_root,
        seed_shard_index=0,
        output_path=tmp_path / "eval.json",
        bridge_path=tmp_path,
        bridge_cwd=tmp_path,
        runtime_binding_path=tmp_path,
        replay_workers=5,
        evaluation_runner=evaluation_runner,
    )
    assert evaluated["terminal_status"] == "COMPLETE"
    assert len(evaluated["baseline_policy_ids"]) == 3
    assert captured_eval["replay_workers"] == 5
    assert isinstance(captured_eval["runtime"], RemoteCausalProgramRuntimeV1)
