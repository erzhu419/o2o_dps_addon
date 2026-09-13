from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from o2o_dps import fury_cat_gap_three_baseline_registry_v1 as registry_module
from o2o_dps.cat2new_fury_cat_gap_policy_v1 import (
    PARAMETER_AXES,
    Cat2NewFuryCatGapPolicyV1,
    FuryCatGapPolicyParametersV1,
)
from o2o_dps.cat2new_fury_paired_lane_adapter_v3 import Cat2NewFuryPairedLaneAdapterV3
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
)


def _candidate() -> Cat2NewFuryCatGapPolicyV1:
    parameters = FuryCatGapPolicyParametersV1.from_mapping(
        {name: values[0] for name, values in PARAMETER_AXES}
    )
    return Cat2NewFuryCatGapPolicyV1(parameters.candidate_id, parameters)


def _scenario(target_count: int = 1) -> dict:
    return {
        "stratum": "single_target" if target_count == 1 else "multi_target",
        "request": {"encounter": {"targets": [{} for _ in range(target_count)]}},
        "dynamic_load_config": {
            "target_health": [{"target_index": index} for index in range(target_count)]
        },
    }


class CatGapThreeBaselineRegistryTests(unittest.TestCase):
    def test_native_baseline_executors_and_validators_are_wired(self) -> None:
        candidate = _candidate()
        bridge = MagicMock()
        group, scenario, policy = object(), _scenario(), {"policy_id": "unused"}
        with (
            patch.object(registry_module, "load_deployed_contra_runtime_binding_v1", return_value={"binding_sha256": "binding-v1"}) as load,
            patch.object(registry_module, "execute_cat_runner_v4_lane_v6", return_value={"lane_result": {"producer": "cat"}}) as cat,
            patch.object(registry_module, "execute_contra260817_runner_v4_lane_v4", return_value={"lane_result": {"producer": "contra-new"}}) as contra_new,
            patch.object(registry_module, "execute_deployed_contra_v7_lane_v7", return_value={
                "lane_result": {"artifact": {"lane_cache_identity": {"key": "runtime"}}},
                "lane_cache_identity": {"key": "runtime"},
            }) as deployed,
        ):
            registry = registry_module.build_cat_gap_three_baseline_registry_v1(
                bridge,
                scenarios=[scenario],
                candidate_policies={candidate.policy_id: candidate},
                runtime_binding_path="runtime-binding.json",
            )
            load.assert_called_once()
            for policy_id in (CAT_POLICY_ID, CONTRA260817_POLICY_ID, CONTRA_DEPLOYED_POLICY_ID):
                self.assertIn(policy_id, registry.executors)
                envelope = registry.executors[policy_id](group=group, scenario=scenario, policy=policy)
                self.assertEqual(set(envelope), {"lane_result"})
            cat.assert_called_once_with(bridge, group=group, scenario=scenario, policy=policy)
            contra_new.assert_called_once_with(bridge, group=group, scenario=scenario, policy=policy)
            deployed.assert_called_once_with(
                bridge,
                group=group,
                scenario=scenario,
                policy=policy,
                runtime_binding={"binding_sha256": "binding-v1"},
                controller="raid_a",
            )
        self.assertIsInstance(registry.executors[candidate.policy_id], Cat2NewFuryPairedLaneAdapterV3)
        self.assertEqual(registry.contract["runtime_binding_sha256"], "binding-v1")
        self.assertEqual(registry.contract["status"], "DEVELOPMENT_ONLY_NO_RUN")
        contracts = {row["policy_id"]: row for row in registry.contract["lane_contracts"]}
        self.assertEqual(set(contracts), set(registry.executors))
        for contract in contracts.values():
            self.assertIn(contract["producer"], registry.artifact_validators)
            self.assertNotIn("placeholder", contract["producer"].lower())
        self.assertIs(
            registry.artifact_validators[registry_module.DEPLOYED_CONTRA_V7_PRODUCER],
            registry_module.validate_deployed_contra_v7_artifact_v7,
        )
        self.assertEqual(
            contracts[CONTRA_DEPLOYED_POLICY_ID],
            registry_module.deployed_contra_runner_v4_lane_contract_v7(),
        )

    def test_multi_target_and_raid_b_are_refused_before_binding_load(self) -> None:
        candidate = _candidate()
        with patch.object(registry_module, "load_deployed_contra_runtime_binding_v1") as load:
            with self.assertRaisesRegex(ValueError, "Raid-B"):
                registry_module.build_cat_gap_three_baseline_registry_v1(
                    MagicMock(),
                    scenarios=[_scenario(2)],
                    candidate_policies={candidate.policy_id: candidate},
                    runtime_binding_path="runtime-binding.json",
                )
            with self.assertRaisesRegex(ValueError, "Raid-B"):
                registry_module.build_cat_gap_three_baseline_registry_v1(
                    MagicMock(),
                    scenarios=[_scenario()],
                    candidate_policies={candidate.policy_id: candidate},
                    runtime_binding_path="runtime-binding.json",
                    controller="raid_b",
                )
            load.assert_not_called()

    def test_deployed_cache_identity_must_survive_envelope_boundary(self) -> None:
        candidate = _candidate()
        with (
            patch.object(registry_module, "load_deployed_contra_runtime_binding_v1", return_value={"binding_sha256": "binding-v1"}),
            patch.object(registry_module, "execute_deployed_contra_v7_lane_v7", return_value={
                "lane_result": {"artifact": {"lane_cache_identity": {"key": "actual"}}},
                "lane_cache_identity": {"key": "different"},
            }),
        ):
            registry = registry_module.build_cat_gap_three_baseline_registry_v1(
                MagicMock(),
                scenarios=[_scenario()],
                candidate_policies={candidate.policy_id: candidate},
                runtime_binding_path="runtime-binding.json",
            )
            with self.assertRaisesRegex(ValueError, "cache identity"):
                registry.executors[CONTRA_DEPLOYED_POLICY_ID](
                    group={}, scenario={}, policy={}
                )


if __name__ == "__main__":
    unittest.main()
