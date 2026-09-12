from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_HORIZON_MS,
    CONFIRMATION_MASTER_SEEDS,
    NONBINDING_TARGET_HEALTH,
    Cat2NewFuryHorizonConfirmationV1Error,
    build_horizon_confirmation_v1,
    extend_screening_scenario_v1,
)
from o2o_dps.fury_encounter_scenarios_v1 import HEALTH_STAT_INDEX
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    build_runner_plan,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)
from o2o_dps.sim_bridge_dynamic_v3 import (
    dynamic_target_semantics_config_from_wire_v3,
)
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan


def _source_scenario(horizon_ms: int = 5_001) -> dict[str, object]:
    source = deepcopy(_plan((1,))["contract"]["scenarios"][0])
    request = source["request"]
    request["encounter"]["duration"] = horizon_ms / 1000.0
    config = dynamic_target_semantics_config_from_wire_v3(
        source["dynamic_load_config"]
    )
    source["dynamic_load_config"] = replace(
        config, idle_advance_horizon_ms=horizon_ms
    ).to_wire()
    source["horizon_ms"] = horizon_ms
    source["estimated_cost_units"] = horizon_ms
    request_sha = sha256_json(request)
    source["scenario_model"]["request_sha256"] = request_sha
    source["target_context_bundle"]["request_sha256"] = request_sha
    return normalize_runner_scenarios([source])[0]


def _source_plan(seeds: tuple[int, ...]) -> dict[str, object]:
    template = _plan(seeds)
    contract = template["contract"]
    scenario = _source_scenario()
    return build_runner_plan(
        protocol_id=contract["protocol_id"],
        protocol_sha256=contract["protocol_sha256"],
        phase=contract["phase"],
        corpus_manifest_sha256=contract["corpus_manifest_sha256"],
        runner_inputs_sha256=contract["runner_inputs_sha256"],
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
        corpus_binding_sha256=contract["corpus_binding_sha256"],
        master_seeds=seeds,
        scenarios=[scenario],
        policies=contract["policies"],
        shard_count=contract["shard_count"],
        bridge_identity=contract["bridge_identity"],
        execution_bundle_identity=contract["execution_bundle_identity"],
        execution_mode=contract["execution_mode"],
        seed_namespace=contract["seed_derivation"]["namespace"],
        plan_intent=contract["plan_intent"],
        lane_contracts=contract["lane_contracts"],
    )


class Cat2NewFuryHorizonConfirmationV1Tests(unittest.TestCase):
    def test_extends_only_horizon_and_nonbinding_health_mechanism_controls(self) -> None:
        source = _source_scenario()
        result = extend_screening_scenario_v1(source)
        self.assertEqual(CONFIRMATION_HORIZON_MS, result["horizon_ms"])
        self.assertEqual(
            CONFIRMATION_HORIZON_MS / 1000.0,
            result["request"]["encounter"]["duration"],
        )
        self.assertEqual(
            NONBINDING_TARGET_HEALTH,
            result["dynamic_load_config"]["target_health"][0]["health"],
        )
        self.assertEqual(
            NONBINDING_TARGET_HEALTH,
            result["request"]["encounter"]["targets"][0]["stats"][
                HEALTH_STAT_INDEX
            ],
        )
        self.assertEqual(
            int(NONBINDING_TARGET_HEALTH),
            result["target_context_bundle"]["contexts"][0]["target_max_health"],
        )
        health_evidence = result["target_context_bundle"]["contexts"][0][
            "field_evidence"
        ]
        self.assertEqual(
            "SENSITIVITY_HYPOTHESIS",
            health_evidence["target_max_health"]["kind"],
        )
        self.assertFalse(result["scenario_model"]["comparison_eligible"])
        self.assertTrue(result["scenario_model"]["bridge_execution_eligible"])

    def test_prepares_exact_three_arm_fresh_seed_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge"
            bridge.write_bytes(b"long-horizon-v1-test")
            bundle = build_horizon_confirmation_v1(
                _source_plan(tuple(range(1, 257))),
                bridge_path=bridge,
                bridge_platform="linux-amd64",
                bridge_build_id="long-horizon-v1-test",
                workers_per_node=8,
            )
        self.assertEqual(list(CONFIRMATION_ARM_IDS), [
            row["arm_spec"]["arm_id"] for row in bundle["arms"]
        ])
        self.assertEqual(3, bundle["arm_count"])
        self.assertEqual(256, bundle["master_seed_count"])
        self.assertEqual(3, bundle["analysis_contract"]["multiplicity_family_size"])
        self.assertEqual(
            "two_sided_paired_student_t",
            bundle["analysis_contract"]["paired_test"],
        )
        self.assertEqual(
            256,
            bundle["analysis_contract"][
                "required_complete_eligible_pairs_per_arm"
            ],
        )
        self.assertFalse(bundle["comparison_ready"])
        self.assertFalse(bundle["deployment_allowed"])
        plan_ids = set()
        for row in bundle["arms"]:
            plan = validate_runner_plan(row["runner_plan"])
            plan_ids.add(plan["plan_sha256"])
            self.assertEqual(
                list(CONFIRMATION_MASTER_SEEDS),
                plan["contract"]["seed_derivation"]["master_seeds"],
            )
            self.assertEqual(256, plan["contract"]["group_count"])
            self.assertEqual(1024, plan["contract"]["expected_rollout_count"])
            self.assertEqual(
                CONFIRMATION_HORIZON_MS,
                plan["contract"]["scenarios"][0]["horizon_ms"],
            )
            self.assertEqual([8] * 6, [
                node["workers"] for node in row["dispatch_plan"]["nodes"]
            ])
        self.assertEqual(3, len(plan_ids))

    def test_rejects_a_nonfrozen_source_horizon(self) -> None:
        source = _source_scenario(6_001)
        with self.assertRaisesRegex(
            Cat2NewFuryHorizonConfirmationV1Error, "5.001-second"
        ):
            extend_screening_scenario_v1(source)


if __name__ == "__main__":
    unittest.main()
