from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from o2o_dps.deployed_contra_runtime_binding_v1 import (
    build_deployed_contra_runtime_binding_v1,
    validate_deployed_contra_runtime_binding_v1,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
    execute_deployed_contra_v7_lane_v7,
    validate_deployed_contra_v7_artifact_v7,
)
from o2o_dps.fury_expert_runtime_snapshot_v1 import sha256_json
from o2o_dps.fury_full_policy_rollout_v7 import (
    ROLLOUT_SCHEMA_V7,
    FuryFullPolicyRolloutV7Error,
    build_deployed_contra_lane_cache_identity_v7,
    run_fury_full_policy_rollout_v7,
    validate_fury_full_policy_rollout_v7,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    validate_lane_result_v4,
)
from o2o_dps.fury_runtime_bound_deployed_contra_adapter_v7 import RAID_B_BLOCKER
from tests.test_deployed_contra_runtime_binding_v1 import _manifest, _snapshot
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _plan
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


def _binding():
    with tempfile.TemporaryDirectory() as directory:
        return build_deployed_contra_runtime_binding_v1(
            source_manifest=_manifest(Path(directory)),
            runtime_snapshot=_snapshot(),
        )


def _readdress(value):
    result = deepcopy(value)
    result.pop("binding_sha256", None)
    result["binding_sha256"] = sha256_json(result)
    return validate_deployed_contra_runtime_binding_v1(result)


def _source_variant(binding):
    result = deepcopy(binding)
    result["source_manifest_sha256"] = "a" * 64
    result["loaded_source_closure_sha256"] = "b" * 64
    return _readdress(result)


def _profile_variant(binding):
    result = deepcopy(binding)
    result["runtime_profile"]["xuanfeng"] = True
    result["runtime_profile_diagnostics"]["buttons_semantic_sha256"] = "c" * 64
    result["adapter_inputs"]["saved_xuanfeng"] = True
    result["runtime_snapshot_sha256"] = "d" * 64
    result["contra_savedvariables_sha256"] = "e" * 64
    return _readdress(result)


def _cvar_variant(binding):
    result = deepcopy(binding)
    observed = result["nampower"]["observed_config_values"]
    observed["NP_QueueSpellsOnCooldown"] = "1"
    result["nampower"]["queue_spells_on_cooldown_enabled"] = True
    result["nampower"]["matches_source_initialization"] = False
    result["nampower"]["config_semantic_sha256"] = "f" * 64
    result["adapter_inputs"]["queue_spells_on_cooldown"] = True
    result["authority_boundary"][
        "configuration_complete_for_source_derived_simulator"
    ] = False
    result["runtime_snapshot_sha256"] = "9" * 64
    return _readdress(result)


def _run(binding, seed=31):
    request = request_v4()
    load = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
    artifact = run_fury_full_policy_rollout_v7(
        _DynamicV3FullBridge(),
        request,
        runtime_binding=binding,
        seed=seed,
        target_contexts={0: context_v4()},
        dynamic_load=load,
    )
    return artifact, load


class FuryFullPolicyRolloutV7Tests(unittest.TestCase):
    def test_runtime_binding_is_consumed_and_content_addressed(self) -> None:
        binding = _binding()
        artifact, load = _run(binding)
        checked = validate_fury_full_policy_rollout_v7(
            artifact, dynamic_load=load
        )

        self.assertEqual(checked["schema"], ROLLOUT_SCHEMA_V7)
        self.assertEqual(checked["deployed_contra_runtime_binding"], binding)
        self.assertEqual(
            checked["lane_cache_identity"]["runtime_binding_sha256"],
            binding["binding_sha256"],
        )
        self.assertEqual(
            checked["controller_coverage"]["unimplemented"]["raid_b"],
            RAID_B_BLOCKER,
        )
        self.assertTrue(checked["steps"])
        helper_inputs = checked["steps"][0]["proposal"]["metadata"][
            "runtime_helper_call_inputs"
        ]
        self.assertFalse(helper_inputs["burst"])
        self.assertFalse(helper_inputs["survival"])

    def test_source_profile_and_cvar_change_binding_and_cache_identity(self) -> None:
        baseline = _binding()
        variants = [
            baseline,
            _source_variant(baseline),
            _profile_variant(baseline),
            _cvar_variant(baseline),
        ]
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 37, rollout_config_v5())
        identities = [
            build_deployed_contra_lane_cache_identity_v7(
                runtime_binding=binding,
                request_sha256=load.request_sha256,
                simulator_seed=load.seed,
                dynamic_load_contract_sha256=load.contract_sha256,
            )["sha256"]
            for binding in variants
        ]

        self.assertEqual(len({item["binding_sha256"] for item in variants}), 4)
        self.assertEqual(len(set(identities)), 4)

        first, _ = _run(baseline, 38)
        profile_changed, _ = _run(_profile_variant(baseline), 38)
        self.assertNotEqual(
            first["content_address"]["sha256"],
            profile_changed["content_address"]["sha256"],
        )

    def test_runner_v4_lane_contains_v7_artifact_and_cache_identity(self) -> None:
        plan = _plan(CONTRA_DEPLOYED_POLICY_ID)
        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]

        envelope = execute_deployed_contra_v7_lane_v7(
            _DynamicV3FullBridge(),
            group=group,
            scenario=scenario,
            policy=policy,
            runtime_binding=_binding(),
        )
        lane = validate_lane_result_v4(
            envelope["lane_result"],
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validate_deployed_contra_v7_artifact_v7,
        )

        self.assertEqual(lane["producer"], DEPLOYED_CONTRA_V7_PRODUCER)
        self.assertEqual(lane["artifact_schema"], ROLLOUT_SCHEMA_V7)
        self.assertEqual(
            envelope["lane_cache_identity"],
            lane["artifact"]["lane_cache_identity"],
        )
        self.assertEqual(lane["artifact_sha256"], sha256_json(lane["artifact"]))
        self.assertFalse(lane["comparison_ready"])

    def test_raid_b_cannot_be_mislabeled_as_implemented(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 41, rollout_config_v5())
        with self.assertRaisesRegex(FuryFullPolicyRolloutV7Error, RAID_B_BLOCKER):
            run_fury_full_policy_rollout_v7(
                _DynamicV3FullBridge(),
                request,
                runtime_binding=_binding(),
                seed=41,
                target_contexts={0: context_v4()},
                dynamic_load=load,
                controller="raid_b",
            )


if __name__ == "__main__":
    unittest.main()
