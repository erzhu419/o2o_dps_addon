from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps import historical_behavior_clone_full_rollout_v2 as full_rollout_v2
from o2o_dps import historical_fury_source_bound_prototype_smoke_runner_v1 as smoke_v1
from o2o_dps.sim_bridge import DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3
from tests.test_historical_fury_source_bound_prototype_bundle_v1 import (
    PROTOTYPE_ID,
    _build,
    _canonical,
    _fixture,
)


def _config(*, health: float = 200.0) -> DynamicTargetSemanticsConfigV3:
    return DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(target_index=0, health=health),),
        idle_advance_horizon_ms=20_001,
    )


def _prepared_fixture(root: Path, *, zero_runtime_segments: bool = False):
    paths = _fixture(root / "sources", zero_runtime_segments=zero_runtime_segments)
    bundle = _build(paths)
    bundle_path = root / "bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(_canonical(bundle, newline=True))
    dynamic_path = root / "dynamic.json"
    dynamic_path.write_text("{}\n", encoding="utf-8")
    return paths, bundle, bundle_path, dynamic_path


def _selection(bundle: dict, *, seed_index: int = 0) -> tuple[str, int]:
    return (
        bundle["requests"][0]["segment_ref"],
        bundle["development_seeds"]["master_seeds"][seed_index],
    )


def _bundle_closure(bundle: dict) -> dict:
    return {
        "schema": bundle["schema"],
        "implementation_revision": bundle["implementation_revision"],
        "content_sha256": bundle["content_address"]["sha256"],
        "request_count": len(bundle["requests"]),
        "development_seed_list_sha256": bundle["development_seeds"][
            "seed_list_sha256"
        ],
        "development_seed_count": len(
            bundle["development_seeds"]["master_seeds"]
        ),
    }


def _ready_preparation(bundle: dict, *, health: float = 200.0):
    base_row = bundle["requests"][0]
    template = deepcopy(base_row["composition"]["request"])
    template["encounter"]["useHealth"] = True
    template["encounter"]["targets"][0]["stats"][34] = health
    config = _config(health=health)
    template_sha = hashlib.sha256(_canonical(template)).hexdigest()
    pair_sha = hashlib.sha256(
        _canonical(
            {
                "derived_request_template_sha256": template_sha,
                "dynamic_load_config": config.to_wire(),
            }
        )
    ).hexdigest()
    row = {
        "segment_ref": base_row["segment_ref"],
        "base_request_sha256": base_row["request_sha256"],
        "status": "READY",
        "development_seed_list_sha256": bundle["development_seeds"][
            "seed_list_sha256"
        ],
        "derived_request_template": template,
        "derived_request_template_sha256": template_sha,
        "dynamic_load_config": config.to_wire(),
        "derived_pair_content_sha256": pair_sha,
    }
    core = {
        "schema": "historical_fury_source_bound_dynamic_config_preparation/v1",
        "implementation_revision": "fixture-ready-contract",
        "status": "PREPARED",
        "input_closure": {"source_bound_prototype_bundle": _bundle_closure(bundle)},
        "requests": [row],
    }
    artifact = {
        **core,
        "content_address": {
            "schema": "historical_fury_source_bound_dynamic_config_preparation_content/v1",
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": hashlib.sha256(_canonical(core)).hexdigest(),
        },
    }
    return artifact, template, config


def _blocked_preparation(bundle: dict) -> dict:
    base_row = bundle["requests"][0]
    core = {
        "schema": "historical_fury_source_bound_dynamic_config_preparation/v1",
        "implementation_revision": "fixture-blocked-contract",
        "status": "BLOCKED",
        "input_closure": {"source_bound_prototype_bundle": _bundle_closure(bundle)},
        "requests": [
            {
                "segment_ref": base_row["segment_ref"],
                "base_request_sha256": base_row["request_sha256"],
                "status": "BLOCKED",
                "development_seed_list_sha256": bundle["development_seeds"][
                    "seed_list_sha256"
                ],
                "derived_request_template": None,
                "derived_request_template_sha256": None,
                "dynamic_load_config": None,
                "derived_pair_content_sha256": None,
            }
        ],
    }
    return {
        **core,
        "content_address": {
            "schema": "fixture-content/v1",
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": hashlib.sha256(_canonical(core)).hexdigest(),
        },
    }


def _dynamic_mocks(artifact: dict, template: dict, config):
    return (
        patch.object(
            smoke_v1.dynamic_prep_v1,
            "load_historical_fury_source_bound_dynamic_config_v1",
            return_value=deepcopy(artifact),
        ),
        patch.object(
            smoke_v1.dynamic_prep_v1,
            "select_ready_dynamic_config_v1",
            return_value=(deepcopy(template), config),
        ),
    )


def _bridge_identity() -> dict:
    return {
        "filename": smoke_v1.DEFAULT_WINDOWS_V11_BRIDGE.name,
        "sha256": smoke_v1.v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SHA256,
        "size_bytes": smoke_v1.v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SIZE,
        "platform": "windows-amd64",
    }


def _fake_rollout(prepared: smoke_v1.PreparedSourceBoundPrototypeSmokeV1) -> dict:
    prototype_id = prepared.exact_model_receipt["prototype_id"]
    policy_id = next(
        binding["policy_id"]
        for binding in prepared.request_row["historical_policy_bindings"]
        if binding["prototype_id"] == prototype_id
    )
    return {
        "schema": full_rollout_v2.SCHEMA,
        "implementation_revision": full_rollout_v2.IMPLEMENTATION_REVISION,
        "status": full_rollout_v2.STATUS,
        "prototype_id": prototype_id,
        "policy_id": policy_id,
        "executable_request_preflight": {
            "schema": "historical_behavior_clone_executable_fury_request/v2"
        },
        "bridge_runtime_evidence": {
            "schema": full_rollout_v2.BRIDGE_RUNTIME_EVIDENCE_SCHEMA,
            "runtime_kind": "NATIVE_SUBPROCESS_BRIDGE",
            "load_method_invoked": "load_dynamic_v3",
            "native_subprocess_bridge": True,
            "python_simulated_bridge": False,
            "evidence_scope": "NATIVE_SUBPROCESS_SMOKE_ONLY",
            "comparison_authorized": False,
        },
        "dynamic_load_binding": {},
        "scenario_complete": True,
        "elapsed_ms": 1_025,
        "damage_delta": 200.0,
        "diagnostic_dps": 200_000.0 / 1_025,
        "configured_completion": {
            "criterion_met": True,
            "terminal_reason": "ALL_TARGETS_DEAD",
        },
        "dynamic_v3_runtime_receipt_closure": {
            "status": "COMPLETE_BOUND",
            "candidate_damage": {
                "receipts": [{"attempt_id": "a", "applied_damage": 200.0}]
            },
            "terminal_lifecycle": {"simulated_damage_applied": 200.0},
        },
    }


class _FakeNativeBridge:
    created: list[tuple[Path, dict]] = []

    def __init__(self, executable, *, cwd, environment):
        self.__class__.created.append(
            (Path(executable), {"cwd": Path(cwd), "environment": environment})
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class HistoricalFurySourceBoundPrototypeSmokeRunnerV1Tests(unittest.TestCase):
    def test_ready_preparation_keeps_base_duration_request_and_binds_seeded_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, bundle, bundle_path, dynamic_path = _prepared_fixture(
                Path(temporary)
            )
            artifact, template, config = _ready_preparation(bundle)
            segment_ref, seed = _selection(bundle, seed_index=1)
            load_mock, select_mock = _dynamic_mocks(artifact, template, config)
            with load_mock, select_mock:
                prepared = smoke_v1.prepare_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=seed,
                )

        base = bundle["requests"][0]["composition"]["request"]
        self.assertEqual(base, prepared.base_raid_sim_request)
        self.assertIsNot(base["encounter"].get("useHealth"), True)
        self.assertTrue(prepared.raid_sim_request["encounter"]["useHealth"])
        self.assertEqual(
            200.0,
            prepared.raid_sim_request["encounter"]["targets"][0]["stats"][34],
        )
        self.assertEqual(str(seed), prepared.raid_sim_request["simOptions"]["randomSeed"])
        expected_seeded = deepcopy(template)
        expected_seeded["simOptions"]["randomSeed"] = str(seed)
        self.assertEqual(expected_seeded, prepared.raid_sim_request)
        self.assertEqual(seed, prepared.dynamic_load.seed)
        self.assertEqual(
            prepared.dynamic_load.request_sha256,
            prepared.execution_pair_binding["final_request_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(_canonical(prepared.dynamic_load.to_wire())).hexdigest(),
            prepared.execution_pair_binding["dynamic_rollout_load_sha256"],
        )
        self.assertEqual(
            template["simOptions"]["randomSeed"],
            artifact["requests"][0]["derived_request_template"]["simOptions"][
                "randomSeed"
            ],
        )

    def test_blocked_dynamic_preparation_fails_before_bridge_or_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, bundle, bundle_path, dynamic_path = _prepared_fixture(
                Path(temporary)
            )
            artifact = _blocked_preparation(bundle)
            segment_ref, seed = _selection(bundle)
            with (
                patch.object(
                    smoke_v1.dynamic_prep_v1,
                    "load_historical_fury_source_bound_dynamic_config_v1",
                    return_value=artifact,
                ),
                patch.object(smoke_v1, "_verify_windows_v11_bridge_v1") as verify,
                patch.object(smoke_v1, "SimulatorBridgeDynamicV3") as bridge,
                self.assertRaisesRegex(
                    smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
                    "not READY",
                ),
            ):
                smoke_v1.run_local_native_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=seed,
                )
            verify.assert_not_called()
            bridge.assert_not_called()

    def test_undeclared_seed_and_health_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, bundle, bundle_path, dynamic_path = _prepared_fixture(
                Path(temporary)
            )
            artifact, template, config = _ready_preparation(bundle)
            segment_ref, _ = _selection(bundle)
            load_mock, select_mock = _dynamic_mocks(artifact, template, config)
            with load_mock, select_mock, self.assertRaisesRegex(
                smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
                "seed is not declared",
            ):
                smoke_v1.prepare_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=max(bundle["development_seeds"]["master_seeds"]) + 1,
                )

            bad_template = deepcopy(template)
            bad_template["encounter"]["targets"][0]["stats"][34] = 201
            bad_artifact = deepcopy(artifact)
            bad_artifact["requests"][0]["derived_request_template"] = bad_template
            bad_artifact["requests"][0]["derived_request_template_sha256"] = (
                hashlib.sha256(_canonical(bad_template)).hexdigest()
            )
            _, seed = _selection(bundle)
            load_mock, select_mock = _dynamic_mocks(bad_artifact, bad_template, config)
            with load_mock, select_mock, self.assertRaisesRegex(
                smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
                "health bits differ",
            ):
                smoke_v1.prepare_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=seed,
                )

    def test_local_runner_passes_only_ready_derived_request_to_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths, bundle, bundle_path, dynamic_path = _prepared_fixture(root)
            artifact, template, config = _ready_preparation(bundle)
            segment_ref, seed = _selection(bundle)
            simulator_root = root / "simulator"
            simulator_root.mkdir()
            bridge_path = root / smoke_v1.DEFAULT_WINDOWS_V11_BRIDGE.name
            bridge_path.write_bytes(b"fixture; identity verification is mocked")
            captured = {}

            def fake_run(bridge, request, **kwargs):
                captured["request"] = deepcopy(request)
                captured.update(kwargs)
                load_mock, select_mock = _dynamic_mocks(artifact, template, config)
                with load_mock, select_mock:
                    prepared = smoke_v1.prepare_source_bound_prototype_smoke_v1(
                        bundle_path=bundle_path,
                        model_manifest_path=paths["model_manifest_path"],
                        dynamic_preparation_path=dynamic_path,
                        segment_ref=segment_ref,
                        prototype_id=PROTOTYPE_ID,
                        seed=seed,
                    )
                return _fake_rollout(prepared)

            _FakeNativeBridge.created.clear()
            load_mock, select_mock = _dynamic_mocks(artifact, template, config)
            with (
                load_mock,
                select_mock,
                patch.object(
                    smoke_v1,
                    "_verify_windows_v11_bridge_v1",
                    return_value=_bridge_identity(),
                ),
                patch.object(smoke_v1, "SimulatorBridgeDynamicV3", _FakeNativeBridge),
                patch.object(
                    full_rollout_v2,
                    "run_behavior_clone_dynamic_v5_rollout_v2",
                    side_effect=fake_run,
                ),
                patch.object(
                    full_rollout_v2,
                    "validate_behavior_clone_dynamic_v5_rollout_v2",
                    side_effect=lambda value, *args, **kwargs: value,
                ),
            ):
                receipt = smoke_v1.run_local_native_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=seed,
                    bridge_path=bridge_path,
                    simulator_root=simulator_root,
                )

        self.assertTrue(captured["request"]["encounter"]["useHealth"])
        self.assertEqual(seed, captured["dynamic_load"].seed)
        self.assertEqual(smoke_v1.COMPLETE_STATUS, receipt["status"])
        self.assertEqual(
            "READY", receipt["input_bindings"]["dynamic_preparation"]["row_status"]
        )
        self.assertFalse(receipt["execution_scope"]["hpc_dispatch_performed"])
        self.assertFalse(receipt["claim_boundary"]["comparison_authorized"])
        self.assertEqual(1, len(_FakeNativeBridge.created))

    def test_receipt_rejects_readdressed_dynamic_pair_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, bundle, bundle_path, dynamic_path = _prepared_fixture(
                Path(temporary)
            )
            artifact, template, config = _ready_preparation(bundle)
            segment_ref, seed = _selection(bundle)
            load_mock, select_mock = _dynamic_mocks(artifact, template, config)
            with load_mock, select_mock:
                prepared = smoke_v1.prepare_source_bound_prototype_smoke_v1(
                    bundle_path=bundle_path,
                    model_manifest_path=paths["model_manifest_path"],
                    dynamic_preparation_path=dynamic_path,
                    segment_ref=segment_ref,
                    prototype_id=PROTOTYPE_ID,
                    seed=seed,
                )
            receipt = smoke_v1._build_receipt(
                prepared,
                bridge_identity=_bridge_identity(),
                rollout=_fake_rollout(prepared),
            )
            tampered = deepcopy(receipt)
            tampered["input_bindings"]["dynamic_execution_pair"][
                "final_request_sha256"
            ] = "0" * 64
            tampered = smoke_v1._content_addressed(
                {key: value for key, value in tampered.items() if key != "content_address"}
            )
            with self.assertRaisesRegex(
                smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
                "execution pair differs",
            ):
                smoke_v1.validate_source_bound_prototype_smoke_receipt_v1(
                    tampered, prepared=prepared
                )


if __name__ == "__main__":
    unittest.main()
