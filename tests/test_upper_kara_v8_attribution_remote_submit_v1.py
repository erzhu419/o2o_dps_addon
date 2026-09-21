from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.upper_kara_v8_attribution_remote_submit_v1 import (
    DEFAULT_BRIDGE,
    DEFAULT_POLICY_BUNDLE,
    DEFAULT_RUNTIME_BINDING,
    NODE_NAMES,
    build_v8_attribution_remote_plan_v1,
    build_v8_e0_reproduction_remote_plan_v1,
)
from o2o_dps.upper_kara_v8_attribution_panel_v1 import (
    load_v8_attribution_contract_v1,
)
from o2o_dps.upper_kara_v8_e0_reproduction_gate_v1 import (
    load_v8_e0_reproduction_contract_v1,
)


@pytest.fixture
def compact_inputs(tmp_path: Path):
    paths = {
        "policy_bundle": DEFAULT_POLICY_BUNDLE,
        "runtime_binding": DEFAULT_RUNTIME_BINDING,
        "bridge": DEFAULT_BRIDGE,
        "scenario_capsule": tmp_path / "capsule.json.gz",
        "live_profile": tmp_path / "profile.json",
        "equipped_names": tmp_path / "names.json",
    }
    for key in ("scenario_capsule", "live_profile", "equipped_names"):
        path = paths[key]
        path.write_bytes(b"test")
    return paths


def _plan(compact_inputs, **overrides):
    values = {
        "shared_home": "/home/tester",
        "run_id": "v8-attribution-fresh-20260921",
        "execution_mode": "E0_EVENT_DRIVEN",
        "scheduler_cli": "/scheduler.py",
        **compact_inputs,
    }
    values.update(overrides)
    return build_v8_attribution_remote_plan_v1(**values)


def _published_summary(contract):
    expected = contract.expected
    return {
        "campaign_id": contract.published_campaign_id,
        "frozen_program_id": contract.parent_policy_id,
        "build_id": contract.build_id,
        "selected_loadout_id": contract.loadout_id,
        "lane_status_counts": expected["published_all_four_lane_status_counts"],
        "campaign_contract": {
            "max_decisions": contract.max_decisions,
            "evaluation_examples": [
                {"seed": seed, "first_wave_arrival_ms": arrival}
                for seed, arrival in contract.examples()
            ],
        },
        "metric": {
            "policy_means": {
                "cat.fury.profile1": {
                    "mean_own_effective_damage": expected[
                        "exact_cat_mean_own_effective_damage"
                    ]
                },
                "frozen_candidate": {
                    "mean_own_effective_damage": expected[
                        "frozen_v8_mean_own_effective_damage"
                    ]
                },
            },
            "paired_candidate_minus_baseline_mean_damage": {
                "cat.fury.profile1": expected["paired_v8_minus_cat_mean_damage"]
            },
            "paired_candidate_minus_baseline_damage_statistics": {
                "cat.fury.profile1": {
                    "win_tie_loss": expected[
                        "paired_v8_minus_cat_win_tie_loss"
                    ]
                }
            },
        },
    }


def test_plan_has_six_round_robin_tasks_and_cpu_matches_nested_parallelism(
    compact_inputs,
) -> None:
    plan = _plan(compact_inputs)
    assert plan["status"] == "PLAN_ONLY_NOT_STAGED_NOT_SUBMITTED_NOT_DISPATCHED"
    assert plan["seed_count"] == 256
    assert plan["shard_count"] == 6
    assert len(plan["task_specs"]) == 6
    assert [row["preferred_node"] for row in plan["task_specs"]] == list(NODE_NAMES)
    assert [row["assigned_seed_count"] for row in plan["shards"]] == [
        43, 43, 43, 43, 42, 42,
    ]
    assert [row["declared_cpu"] for row in plan["shards"]] == [192] * 6
    for index, (spec, shard) in enumerate(zip(plan["task_specs"], plan["shards"])):
        assert spec["cpu"] == shard["seed_workers"] * 3 * 2
        assert spec["cpu_parallel_item_multiplier"] == 6
        assert spec["cpu_parallel_items"] == shard["assigned_seed_count"]
        assert spec["cpu_batch_plan"]["maximum_concurrent_arm_lanes"] == spec["cpu"]
        assert spec["cpu_batch_plan"]["native_lanes_per_paired_arm"] == 2
        assert f"--shard-index {index}" in spec["cmd"]
        assert "--shard-count 6" in spec["cmd"]
        assert "--seed-workers 32" in spec["cmd"]
        assert "--arm-workers 3" in spec["cmd"]
        assert "run-shard" in spec["cmd"]
        assert spec["cmd"].startswith("env GOMAXPROCS=1 ")
        assert "DONE v8_attribution" in spec["cmd"]
        assert spec["vram"] == 0
        assert spec["skip_launch_staging"] is True
    contract = load_v8_attribution_contract_v1(
        Path(__file__).parents[1]
        / "configs/evaluation/upper_kara_v8_attribution_fresh_v1.json"
    )
    assert plan["frozen_input_identity"] == {
        "parent_policy_id": contract.parent_policy_id,
        "loadout_id": contract.loadout_id,
        "bridge_artifact_name": contract.bridge_artifact_name,
        "runtime_binding_id": contract.runtime_binding_id,
    }


def test_plan_stages_only_compact_closure_and_leaves_seed_rows_remote(
    compact_inputs,
) -> None:
    plan = _plan(compact_inputs, execution_mode="E1_EXTERNAL_PRESS_CLOCK")
    sources = [str(row["source"]).replace("\\", "/") for row in plan["copy_specs"]]
    destinations = [
        str(row["destination"]).replace("\\", "/")
        for row in plan["copy_specs"]
    ]
    assert len(plan["copy_specs"]) == 8
    assert sum("fury_offline_scenario_capsules" in row for row in destinations) == 1
    assert not any(row.lower().endswith(".csv") for row in sources)
    assert not any("ckpt" in row.lower() for row in sources)
    assert not any(
        f"/{name}/" in row.lower()
        for row in sources
        for name in ("cat", "cat2", "contra", "contra_new", "dpssim")
    )
    assert plan["raw_chronicle_csv_staged"] is False
    assert plan["checkpoint_staged"] is False
    assert plan["third_party_project_tree_staged"] is False
    assert plan["server_resident_seed_artifacts"] is True
    assert plan["automatic_result_pull"] is False
    assert len(plan["manual_small_fetch_candidates"]) == 7
    assert "summarize" in plan["post_panel_summary_command"]
    assert "--execution-mode E1_EXTERNAL_PRESS_CLOCK" in plan[
        "post_panel_summary_command"
    ]
    assert "submit-jsonl" in plan["bulk_submit_command"]
    assert "dispatch" not in plan["bulk_submit_command"]


def test_worker_cap_changes_cpu_reservation_not_seed_assignment(compact_inputs) -> None:
    plan = _plan(compact_inputs, seed_workers=40)
    assert [row["assigned_seed_count"] for row in plan["shards"]] == [
        43, 43, 43, 43, 42, 42,
    ]
    assert [row["declared_cpu"] for row in plan["shards"]] == [240] * 6
    assert all("--seed-workers 40" in row["cmd"] for row in plan["task_specs"])


def test_plan_rejects_non_three_arm_cpu_contract(compact_inputs) -> None:
    with pytest.raises(ValueError, match="exactly 3 arm workers"):
        _plan(compact_inputs, arm_workers=2)


def test_plan_rejects_non_linux_bridge(tmp_path: Path, compact_inputs) -> None:
    bridge = tmp_path / "bridge.exe"
    bridge.write_bytes(b"not-used")
    with pytest.raises(ValueError, match="Linux amd64"):
        _plan(compact_inputs, bridge=bridge)


def test_reproduction_plan_is_six_node_e0_and_never_mixes_fresh_outputs(
    tmp_path: Path, compact_inputs,
) -> None:
    contract_path = (
        Path(__file__).parents[1]
        / "configs/evaluation/upper_kara_v8_e0_reproduction_gate_v1.json"
    )
    contract = load_v8_e0_reproduction_contract_v1(contract_path)
    published = tmp_path / "published-summary.json"
    published.write_text(json.dumps(_published_summary(contract)), encoding="utf-8")
    plan = build_v8_e0_reproduction_remote_plan_v1(
        shared_home="/home/tester",
        run_id="v8-published-e0-reproduction",
        contract=contract_path,
        published_summary=published,
        scheduler_cli="/scheduler.py",
        **compact_inputs,
    )
    assert plan["scope"] == "REPRODUCTION_ONLY_NOT_FRESH"
    assert plan["fresh_evidence"] is False
    assert plan["published_heldout_reused"] is True
    assert plan["execution_mode"] == "E0_EVENT_DRIVEN"
    assert plan["published_reference_validation"]["status"] == (
        "PUBLISHED_REFERENCE_VALIDATED"
    )
    assert plan["seed_count"] == 256
    assert [row["assigned_seed_count"] for row in plan["shards"]] == [
        43, 43, 43, 43, 42, 42,
    ]
    assert [row["declared_cpu"] for row in plan["shards"]] == [
        86, 86, 86, 86, 84, 84,
    ]
    assert len(plan["task_specs"]) == 6
    for spec, shard in zip(plan["task_specs"], plan["shards"]):
        assert spec["cpu"] == shard["seed_workers"] * 2
        assert spec["cpu_parallel_item_multiplier"] == 2
        assert spec["cpu_batch_plan"]["paired_lanes_execute_concurrently"] is True
        assert spec["cpu_batch_plan"]["maximum_concurrent_native_bridges"] == spec["cpu"]
        assert spec["cmd"].startswith("env GOMAXPROCS=1 ")
    assert all(
        "upper_kara_v8_e0_reproduction_gate_v1 run-shard" in row["cmd"]
        for row in plan["task_specs"]
    )
    assert all("attribution--" not in row["cmd"] for row in plan["task_specs"])
    assert "results/reproduction-gate/e0-published-heldout" in plan["result_root"]
    assert "results/attribution" not in plan["result_root"]
    assert plan["results_mixed_with_fresh_attribution"] is False
    assert plan["frozen_input_identity"] == {
        "parent_policy_id": contract.parent_policy_id,
        "loadout_id": contract.loadout_id,
        "bridge_artifact_name": contract.bridge_artifact_name,
        "runtime_binding_id": contract.runtime_binding_id,
    }
    assert len(plan["copy_specs"]) == 9
    assert any(
        row["source"] == str(published.resolve())
        and row["destination"] == plan["remote_published_summary"]
        for row in plan["copy_specs"]
    )
    assert plan["raw_chronicle_csv_staged"] is False
    assert plan["checkpoint_staged"] is False
    assert plan["automatic_result_pull"] is False
    assert len(plan["manual_small_fetch_candidates"]) == 7
    assert "summarize" in plan["post_panel_summary_command"]


def test_reproduction_plan_rejects_drifted_published_reference(
    tmp_path: Path, compact_inputs,
) -> None:
    contract_path = (
        Path(__file__).parents[1]
        / "configs/evaluation/upper_kara_v8_e0_reproduction_gate_v1.json"
    )
    contract = load_v8_e0_reproduction_contract_v1(contract_path)
    value = _published_summary(contract)
    value["lane_status_counts"] = {"COMPLETE": 1023, "FAILED": 1}
    published = tmp_path / "drifted.json"
    published.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="identity/counts"):
        build_v8_e0_reproduction_remote_plan_v1(
            shared_home="/home/tester",
            run_id="v8-published-e0-reproduction",
            contract=contract_path,
            published_summary=published,
            scheduler_cli="/scheduler.py",
            **compact_inputs,
        )
