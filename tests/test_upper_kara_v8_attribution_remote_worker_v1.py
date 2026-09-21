from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from o2o_dps.upper_kara_v8_attribution_panel_v1 import (
    ARM_IDS,
    SCHEMA as PANEL_SCHEMA,
    load_v8_attribution_contract_v1,
)
from o2o_dps.upper_kara_cat_residual_paired_eval_v8 import (
    COMPACT_TELEMETRY_SCHEMA,
)
from o2o_dps.upper_kara_v8_attribution_remote_worker_v1 import (
    SCHEMA,
    assigned_seed_indices_v1,
    run_v8_attribution_shard_remote_v1,
    seed_output_name_v1,
    summarize_v8_attribution_remote_v1,
    validate_v8_attribution_inputs_v1,
)


ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "configs/evaluation/upper_kara_v8_attribution_fresh_v1.json"
MODE = "E0_EVENT_DRIVEN"
BUNDLE = (
    ROOT
    / "results/upper_kara_cat_action_plan_residual_remote_v8_256x256_v1"
    / "cat-action-plan-residual-20260920-v8"
    / "residual-bundle--contra_turtle_burst__rage.json"
)
BINDING = (
    ROOT
    / "results/responsive-team-v4"
    / "deployed-contra-runtime-binding-v1.951b8faa.json"
)
BRIDGE = (
    ROOT
    / "bin/o2obridge.seedfix-v27.precombat-press.withdb.goamd64v1.linux-amd64"
)


def _fake_seed_runner(
    contract_path,
    seed_index,
    execution_mode,
    output_path,
    bridge_path,
    runtime_binding_path,
    **_,
):
    contract = load_v8_attribution_contract_v1(contract_path)
    seed, arrival = contract.examples()[seed_index]
    arms = {
        arm_id: {
            "status": "COMPLETED",
            "own_effective_damage": float(100 + offset),
            "elapsed_ms": float(1_000 - offset),
            "intervention_executed": offset > 0,
            "compact_telemetry": {
                "schema": COMPACT_TELEMETRY_SCHEMA,
                "status": "PARTIALLY_OBSERVED_WITH_LABELED_ASSUMPTIONS",
                "not_observed_reasons": ["SYNTHETIC_REMOTE_WORKER_TEST_ROW"],
            },
        }
        for offset, arm_id in enumerate(ARM_IDS)
    }
    value = {
        "schema": f"{PANEL_SCHEMA}/seed",
        "compact_telemetry_schema": COMPACT_TELEMETRY_SCHEMA,
        "status": "COMPLETED_ATTRIBUTION_BLOCK",
        "seed": seed,
        "build_id": contract.build_id,
        "loadout_id": contract.loadout_id,
        "first_wave_arrival_ms": arrival,
        "parent_policy_id": contract.parent_policy_id,
        "execution_mode": execution_mode,
        "arms": arms,
        "remote_task": {
            "schema": SCHEMA,
            "experiment_id": contract.experiment_id,
            "seed_index": seed_index,
            "execution_mode": execution_mode,
            "external_press_clock": None,
            "bridge_artifact_name": Path(bridge_path).name,
            "runtime_binding_artifact_name": Path(runtime_binding_path).name,
            "runtime_binding_id": contract.runtime_binding_id,
        },
    }
    Path(output_path).write_text(json.dumps(value), encoding="utf-8")
    return value


def test_round_robin_assignment_is_complete_and_disjoint() -> None:
    shards = [
        assigned_seed_indices_v1(256, shard_index=index, shard_count=6)
        for index in range(6)
    ]
    assert [len(row) for row in shards] == [43, 43, 43, 43, 42, 42]
    assert sorted(value for row in shards for value in row) == list(range(256))
    assert len({value for row in shards for value in row}) == 256


def test_fresh_attribution_inputs_are_bound_to_exact_frozen_closure(
    tmp_path: Path,
) -> None:
    contract = load_v8_attribution_contract_v1(CONTRACT)
    identity = validate_v8_attribution_inputs_v1(
        contract,
        policy_bundle_path=BUNDLE,
        bridge_path=BRIDGE,
        runtime_binding_path=BINDING,
    )
    assert identity["bridge_artifact_name"] == contract.bridge_artifact_name
    assert identity["runtime_binding_id"] == contract.runtime_binding_id

    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    proposal = next(
        row for row in bundle["candidates"] if row["candidate_id"] == "proposal-005"
    )
    proposal["policy_wire"]["steps"][0]["decision"]["queue"]["action"] = {
        "spell_id": 25_286,
        "tag": 1,
    }
    altered = tmp_path / "altered-bundle.json"
    altered.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen wire"):
        validate_v8_attribution_inputs_v1(
            contract,
            policy_bundle_path=altered,
            bridge_path=BRIDGE,
            runtime_binding_path=BINDING,
        )
    with pytest.raises(ValueError, match="bridge artifact"):
        validate_v8_attribution_inputs_v1(
            contract,
            policy_bundle_path=BUNDLE,
            bridge_path=tmp_path / "old-bridge.linux-amd64",
            runtime_binding_path=BINDING,
        )
    with pytest.raises(ValueError, match="runtime binding"):
        validate_v8_attribution_inputs_v1(
            replace(contract, runtime_binding_id="0" * 64),
            policy_bundle_path=BUNDLE,
            bridge_path=BRIDGE,
            runtime_binding_path=BINDING,
        )


def test_run_shard_writes_canonical_seed_files_and_reuses_only_valid_rows(
    tmp_path: Path,
) -> None:
    outputs = tmp_path / "seeds"
    first_summary = tmp_path / "shard-first.json"
    kwargs = {
        "contract_path": CONTRACT,
        "policy_bundle_path": tmp_path / "unused-bundle.json",
        "shard_index": 5,
        "shard_count": 6,
        "execution_mode": MODE,
        "output_dir": outputs,
        "summary_path": first_summary,
        "bridge_path": (
            tmp_path
            / load_v8_attribution_contract_v1(CONTRACT).bridge_artifact_name
        ),
        "bridge_cwd": tmp_path,
        "runtime_binding_path": tmp_path / "unused-binding.json",
        "seed_workers": 4,
        "arm_workers": 3,
        "seed_runner": _fake_seed_runner,
    }
    first = run_v8_attribution_shard_remote_v1(**kwargs)
    expected = list(range(5, 256, 6))
    assert first["status"] == "COMPLETED_ATTRIBUTION_SHARD"
    assert first["assigned_seed_indices"] == expected
    assert first["new_seed_count"] == len(expected)
    assert first["reused_valid_seed_count"] == 0
    assert first["native_lanes_per_paired_arm"] == 2
    assert first["maximum_concurrent_arm_lanes"] == 24
    assert first["external_press_clock"] is None
    assert first["bridge_artifact_name"] == load_v8_attribution_contract_v1(
        CONTRACT
    ).bridge_artifact_name
    assert first["runtime_binding_artifact_name"] == "unused-binding.json"
    assert first["runtime_binding_id"] == load_v8_attribution_contract_v1(
        CONTRACT
    ).runtime_binding_id
    assert first["partial_attribution_summary"]["status"] == "COMPLETE"
    assert sorted(path.name for path in outputs.iterdir()) == sorted(
        seed_output_name_v1(MODE, index) for index in expected
    )

    second_summary = tmp_path / "shard-resume.json"

    def should_not_run(**_):
        raise AssertionError("valid immutable outputs must be reused")

    second = run_v8_attribution_shard_remote_v1(
        **{**kwargs, "summary_path": second_summary, "seed_runner": should_not_run}
    )
    assert second["new_seed_count"] == 0
    assert second["reused_valid_seed_count"] == len(expected)


def test_run_shard_rejects_reuse_from_a_different_bridge(tmp_path: Path) -> None:
    outputs = tmp_path / "seeds"
    kwargs = {
        "contract_path": CONTRACT,
        "policy_bundle_path": tmp_path / "unused-bundle.json",
        "shard_index": 0,
        "shard_count": 256,
        "execution_mode": MODE,
        "output_dir": outputs,
        "summary_path": tmp_path / "first-summary.json",
        "bridge_path": (
            tmp_path
            / load_v8_attribution_contract_v1(CONTRACT).bridge_artifact_name
        ),
        "bridge_cwd": tmp_path,
        "runtime_binding_path": tmp_path / "binding-v1.json",
        "seed_workers": 1,
        "arm_workers": 3,
        "seed_runner": _fake_seed_runner,
    }
    run_v8_attribution_shard_remote_v1(**kwargs)

    with pytest.raises(ValueError, match="identity differs"):
        run_v8_attribution_shard_remote_v1(
            **{
                **kwargs,
                "summary_path": tmp_path / "second-summary.json",
                "bridge_path": tmp_path / "bridge-v2",
            }
        )


def test_run_shard_rejects_foreign_existing_seed_result(tmp_path: Path) -> None:
    outputs = tmp_path / "seeds"
    outputs.mkdir()
    foreign = outputs / seed_output_name_v1(MODE, 0)
    foreign.write_text(json.dumps({"schema": "foreign"}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity differs"):
        run_v8_attribution_shard_remote_v1(
            contract_path=CONTRACT,
            policy_bundle_path=tmp_path / "unused.json",
            shard_index=0,
            shard_count=256,
            execution_mode=MODE,
            output_dir=outputs,
            summary_path=tmp_path / "summary.json",
            bridge_path=tmp_path / "unused",
            bridge_cwd=tmp_path,
            runtime_binding_path=tmp_path / "unused.json",
            seed_workers=1,
            arm_workers=3,
            seed_runner=_fake_seed_runner,
        )


def test_global_summary_refuses_an_incomplete_256_seed_panel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read attribution seed result"):
        summarize_v8_attribution_remote_v1(
            contract_path=CONTRACT,
            input_root=tmp_path,
            execution_mode=MODE,
            output_path=tmp_path / "summary.json",
        )


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"seed_count": 0, "shard_index": 0, "shard_count": 6}, "seed_count"),
        ({"seed_count": 4, "shard_index": 6, "shard_count": 6}, "shard_index"),
        ({"seed_count": 4, "shard_index": 5, "shard_count": 6}, "no assigned"),
    ],
)
def test_assignment_rejects_invalid_or_empty_shards(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        assigned_seed_indices_v1(**kwargs)
