from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.upper_kara_v8_e0_reproduction_gate_v1 import (
    EXECUTION_MODE,
    SCHEMA,
    SCOPE,
    assigned_reproduction_seed_indices_v1,
    load_v8_e0_reproduction_contract_v1,
    reproduction_seed_output_name_v1,
    run_v8_e0_reproduction_seed_v1,
    run_v8_e0_reproduction_shard_v1,
    summarize_v8_e0_reproduction_v1,
    validate_published_summary_reference_v1,
    validate_v8_e0_reproduction_inputs_v1,
)


ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "configs/evaluation/upper_kara_v8_e0_reproduction_gate_v1.json"
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


def _row(contract, seed_index: int, cat_damage: float, delta: float):
    seed, arrival = contract.examples()[seed_index]
    simulator_seed = derive_simulator_seed(
        seed,
        contract.request_sha256,
        namespace=contract.simulator_seed_namespace,
    )
    return {
        "schema": f"{SCHEMA}/seed",
        "status": "COMPLETED_REPRODUCTION_PAIR",
        "scope": SCOPE,
        "fresh_evidence": False,
        "published_heldout_reused": True,
        "execution_mode": EXECUTION_MODE,
        "bridge_generation": contract.bridge_generation,
        "bridge_artifact_name": contract.bridge_artifact_name,
        "runtime_binding_id": contract.runtime_binding_id,
        "experiment_id": contract.experiment_id,
        "seed_index": seed_index,
        "seed": seed,
        "master_seed": seed,
        "simulator_seed": simulator_seed,
        "request_sha256": contract.request_sha256,
        "simulator_seed_namespace": contract.simulator_seed_namespace,
        "simulator_seed_derivation_algorithm": (
            contract.simulator_seed_derivation_algorithm
        ),
        "first_wave_arrival_ms": arrival,
        "parent_policy_id": contract.parent_policy_id,
        "build_id": contract.build_id,
        "loadout_id": contract.loadout_id,
        "exact_cat_terminal": {
            "status": "COMPLETED",
            "own_effective_damage": cat_damage,
        },
        "frozen_v8_terminal": {
            "status": "COMPLETED",
            "own_effective_damage": cat_damage + delta,
        },
        "paired_v8_minus_cat_own_effective_damage": delta,
        "policy_input_audit": {"clean": True},
        "step_audit": {},
    }


def _write_exact_expected_panel(root: Path):
    contract = load_v8_e0_reproduction_contract_v1(CONTRACT)
    expected = contract.expected
    cat = expected["exact_cat_mean_own_effective_damage"]
    target_delta = expected["paired_v8_minus_cat_mean_damage"]
    win_delta = 4_000.0
    loss_delta = (256 * target_delta - 163 * win_delta) / 51
    assert loss_delta < 0
    deltas = [win_delta] * 163 + [0.0] * 42 + [loss_delta] * 51
    root.mkdir(parents=True)
    for index, delta in enumerate(deltas):
        path = root / reproduction_seed_output_name_v1(index)
        path.write_text(json.dumps(_row(contract, index, cat, delta)), encoding="utf-8")
    return contract


def _write_reference_summary(path: Path, contract) -> Path:
    expected = contract.expected
    value = {
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
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_contract_is_exact_published_256_example_reproduction_only() -> None:
    contract = load_v8_e0_reproduction_contract_v1(CONTRACT)
    assert contract.examples()[:7] == (
        (1_320_001, 0),
        (1_320_002, 1_000),
        (1_320_003, 3_000),
        (1_320_004, 5_000),
        (1_320_005, 7_000),
        (1_320_006, 9_000),
        (1_320_007, 0),
    )
    assert contract.examples()[-1] == (1_320_256, 5_000)
    assert contract.parent_policy_id.endswith("proposal-005")
    assert contract.bridge_generation == "v27-precombat-press"
    assert contract.simulator_seed_namespace == (
        "upper-kara-61944-model-wave-development-v1"
    )
    assert contract.request_sha256 == (
        "97825ec34e52357097d98f520ccdf4018f2d8ddc35e1b6e8fd29a7c36764cc04"
    )
    assert contract.deterministic_absolute_tolerance == 1e-9


def test_reference_validator_matches_named_authoritative_fields(tmp_path: Path) -> None:
    contract = load_v8_e0_reproduction_contract_v1(CONTRACT)
    expected = contract.expected
    summary = {
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
                "cat.fury.profile1": expected[
                    "paired_v8_minus_cat_mean_damage"
                ]
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
    path = tmp_path / "published.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    receipt = validate_published_summary_reference_v1(contract, path)
    assert receipt["status"] == "PUBLISHED_REFERENCE_VALIDATED"
    summary["metric"]["policy_means"]["frozen_candidate"][
        "mean_own_effective_damage"
    ] += 1e-6
    path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="mean_own_effective_damage"):
        validate_published_summary_reference_v1(contract, path)


def test_seed_runner_executes_only_exact_cat_and_full_v8_under_e0(tmp_path: Path) -> None:
    calls = []

    def fake_evaluator(received_policy, **kwargs):
        calls.append((received_policy, kwargs))
        request_sha256 = load_v8_e0_reproduction_contract_v1(
            CONTRACT
        ).request_sha256
        simulator_seed = derive_simulator_seed(
            kwargs["seed"],
            request_sha256,
            namespace=kwargs["simulator_seed_namespace"],
        )
        return {
            "status": "COMPLETED_PAIRED_EVALUATION",
            "paired_comparison_valid": True,
            "exact_cat_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": 10.0,
            },
            "residual_terminal": {
                "status": "COMPLETED",
                "own_effective_damage": 13.0,
            },
            "paired_residual_minus_cat_own_effective_damage": 3.0,
            "seed": kwargs["seed"],
            "master_seed": kwargs["seed"],
            "simulator_seed": simulator_seed,
            "request_sha256": request_sha256,
            "simulator_seed_namespace": kwargs["simulator_seed_namespace"],
            "simulator_seed_derivation_algorithm": (
                load_v8_e0_reproduction_contract_v1(
                    CONTRACT
                ).simulator_seed_derivation_algorithm
            ),
            "policy_input_audit": {"clean": True},
            "step_audit": {},
        }

    output = tmp_path / "seed.json"
    row = run_v8_e0_reproduction_seed_v1(
        contract_path=CONTRACT,
        policy_bundle_path=BUNDLE,
        seed_index=0,
        output_path=output,
        bridge_path=BRIDGE,
        bridge_cwd=tmp_path,
        runtime_binding_path=BINDING,
        paired_evaluator=fake_evaluator,
    )
    assert len(calls) == 1
    assert calls[0][0].policy_id.endswith("proposal-005")
    assert calls[0][1]["seed"] == 1_320_001
    assert calls[0][1]["first_wave_arrival_ms"] == 0
    assert calls[0][1]["simulator_seed_namespace"] == (
        load_v8_e0_reproduction_contract_v1(CONTRACT).simulator_seed_namespace
    )
    assert "selected_decision_transform" not in calls[0][1]
    assert "external_press_period_ms" not in calls[0][1]
    assert row["scope"] == SCOPE
    assert row["fresh_evidence"] is False
    assert row["paired_v8_minus_cat_own_effective_damage"] == 3.0
    assert row["bridge_artifact_name"] == BRIDGE.name
    assert row["runtime_binding_id"] == load_v8_e0_reproduction_contract_v1(
        CONTRACT
    ).runtime_binding_id
    assert row["master_seed"] == 1_320_001
    assert row["simulator_seed"] == derive_simulator_seed(
        1_320_001,
        row["request_sha256"],
        namespace=row["simulator_seed_namespace"],
    )


def test_frozen_inputs_reject_policy_loadout_runtime_and_bridge_drift(
    tmp_path: Path,
) -> None:
    contract = load_v8_e0_reproduction_contract_v1(CONTRACT)
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
        validate_v8_e0_reproduction_inputs_v1(
            contract,
            policy_bundle_path=altered,
            bridge_path=BRIDGE,
            runtime_binding_path=BINDING,
        )

    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    bundle["loadout_id"] = "contra_turtle_burst__quickness"
    altered.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="loadout identity"):
        validate_v8_e0_reproduction_inputs_v1(
            contract,
            policy_bundle_path=altered,
            bridge_path=BRIDGE,
            runtime_binding_path=BINDING,
        )

    with pytest.raises(ValueError, match="bridge artifact"):
        validate_v8_e0_reproduction_inputs_v1(
            contract,
            policy_bundle_path=BUNDLE,
            bridge_path=tmp_path / "old-bridge.linux-amd64",
            runtime_binding_path=BINDING,
        )
    with pytest.raises(ValueError, match="runtime binding"):
        validate_v8_e0_reproduction_inputs_v1(
            replace(contract, runtime_binding_id="0" * 64),
            policy_bundle_path=BUNDLE,
            bridge_path=BRIDGE,
            runtime_binding_path=BINDING,
        )


def test_summarizer_passes_only_exact_published_metrics_and_counts(tmp_path: Path) -> None:
    seed_root = tmp_path / "seeds"
    contract = _write_exact_expected_panel(seed_root)
    reference = _write_reference_summary(tmp_path / "published.json", contract)
    output = tmp_path / "summary.json"
    summary = summarize_v8_e0_reproduction_v1(
        contract_path=CONTRACT,
        published_summary_path=reference,
        input_root=seed_root,
        output_path=output,
    )
    assert summary["status"] == "PASSED_PUBLISHED_V8_E0_REPRODUCTION_GATE"
    assert summary["gate_passed"] is True
    assert summary["scope"] == SCOPE
    assert summary["fresh_evidence"] is False
    assert summary["seed_count"] == 256
    assert summary["actual"]["paired_v8_minus_cat_win_tie_loss"] == {
        "wins": 163, "ties": 42, "losses": 51,
    }
    assert summary["actual"]["selected_lane_terminal_counts"] == contract.expected[
        "selected_lane_terminal_counts"
    ]


def test_summarizer_writes_failed_gate_on_complete_metric_drift(tmp_path: Path) -> None:
    seed_root = tmp_path / "seeds"
    contract = _write_exact_expected_panel(seed_root)
    reference = _write_reference_summary(tmp_path / "published.json", contract)
    first = seed_root / reproduction_seed_output_name_v1(0)
    row = json.loads(first.read_text(encoding="utf-8"))
    row["exact_cat_terminal"]["own_effective_damage"] += 1.0
    row["frozen_v8_terminal"]["own_effective_damage"] += 1.0
    first.write_text(json.dumps(row), encoding="utf-8")
    output = tmp_path / "failed.json"
    summary = summarize_v8_e0_reproduction_v1(
        contract_path=CONTRACT,
        published_summary_path=reference,
        input_root=seed_root,
        output_path=output,
    )
    assert summary["gate_passed"] is False
    assert summary["status"] == "FAILED_PUBLISHED_V8_E0_REPRODUCTION_GATE"
    assert output.is_file()


def test_six_shards_cover_published_indices_once() -> None:
    shards = [
        assigned_reproduction_seed_indices_v1(
            256, shard_index=index, shard_count=6
        )
        for index in range(6)
    ]
    assert [len(row) for row in shards] == [43, 43, 43, 43, 42, 42]
    assert sorted(index for shard in shards for index in shard) == list(range(256))


def test_shard_writes_separate_reproduction_outputs_and_resumes(tmp_path: Path) -> None:
    contract = load_v8_e0_reproduction_contract_v1(CONTRACT)
    output_dir = tmp_path / "reproduction-seeds"

    def fake_runner(seed_index, output_path, **_):
        row = _row(contract, seed_index, 100.0, 1.0)
        Path(output_path).write_text(json.dumps(row), encoding="utf-8")
        return row

    common = {
        "contract_path": CONTRACT,
        "policy_bundle_path": tmp_path / "unused.json",
        "shard_index": 5,
        "shard_count": 6,
        "output_dir": output_dir,
        "bridge_path": tmp_path / "unused",
        "bridge_cwd": tmp_path,
        "runtime_binding_path": tmp_path / "unused.json",
        "seed_workers": 4,
    }
    first = run_v8_e0_reproduction_shard_v1(
        **common, summary_path=tmp_path / "first.json", seed_runner=fake_runner
    )
    assert first["scope"] == SCOPE
    assert first["new_seed_count"] == 42
    assert first["reused_valid_seed_count"] == 0

    def should_not_run(**_):
        raise AssertionError("valid completed reproduction rows must be reused")

    second = run_v8_e0_reproduction_shard_v1(
        **common, summary_path=tmp_path / "second.json", seed_runner=should_not_run
    )
    assert second["new_seed_count"] == 0
    assert second["reused_valid_seed_count"] == 42
