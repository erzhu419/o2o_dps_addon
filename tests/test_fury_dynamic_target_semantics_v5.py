from __future__ import annotations

import copy
import json
import unittest

from o2o_dps.fury_dynamic_target_semantics_v4 import DynamicRolloutLoadV2
from o2o_dps.fury_dynamic_target_semantics_v5 import (
    DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3,
    DynamicRolloutLoadV3,
    FuryDynamicTargetSemanticsV5Error,
    compile_dynamic_idle_binding_v5,
    dynamic_rollout_load_from_adapter_wire_v3,
    upgrade_dynamic_rollout_load_v5,
    validate_dynamic_idle_binding_v5,
)
from o2o_dps.fury_paired_multiseed_runner_v2 import sha256_json
from o2o_dps.sim_bridge_dynamic_v3 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
)
from tests.test_fury_dynamic_target_semantics_v4 import config_v4, request_v4


class FuryDynamicTargetSemanticsV5Tests(unittest.TestCase):
    def test_upgrade_preserves_all_hypothesis_schedules_and_binds_horizon(self):
        request = request_v4()
        prior = DynamicRolloutLoadV2.bind(request, 17, config_v4())
        upgraded = upgrade_dynamic_rollout_load_v5(request, prior)
        self.assertIsInstance(upgraded, DynamicRolloutLoadV3)
        self.assertEqual(2000, upgraded.config.idle_advance_horizon_ms)
        self.assertEqual(
            [row.to_wire() for row in prior.config.target_health],
            [row.to_wire() for row in upgraded.config.target_health],
        )
        self.assertEqual(
            [row.to_wire() for row in prior.config.background_damage_events],
            [row.to_wire() for row in upgraded.config.background_damage_events],
        )
        self.assertEqual(
            [row.to_wire() for row in prior.config.attackability_events],
            [row.to_wire() for row in upgraded.config.attackability_events],
        )
        self.assertEqual(
            [row.to_wire() for row in prior.config.effective_armor_events],
            [row.to_wire() for row in upgraded.config.effective_armor_events],
        )
        self.assertNotEqual(
            prior.config.content_sha256, upgraded.config.content_sha256
        )
        self.assertEqual(
            upgraded,
            DynamicRolloutLoadV3.from_wire(request, upgraded.to_wire()),
        )

    def test_adapter_wire_and_content_artifact_fail_closed(self):
        request = request_v4()
        upgraded = upgrade_dynamic_rollout_load_v5(
            request, DynamicRolloutLoadV2.bind(request, 18, config_v4())
        )
        wire = {
            "command": "load_dynamic_v3",
            "request": request,
            "seed": upgraded.seed,
            "dynamic": upgraded.config.to_wire(),
        }
        parsed_request, parsed = dynamic_rollout_load_from_adapter_wire_v3(wire)
        self.assertEqual(request, parsed_request)
        self.assertEqual(upgraded, parsed)
        compiled = compile_dynamic_idle_binding_v5(request, upgraded)
        artifact = validate_dynamic_idle_binding_v5(
            compiled.artifact, request=request
        )
        self.assertEqual(
            DYNAMIC_ROLLOUT_LOAD_SCHEMA_V3,
            artifact["dynamic_rollout_load"]["schema"],
        )
        self.assertEqual(
            DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            artifact["dynamic_config_schema"],
        )
        self.assertFalse(artifact["formal_runner_connected"])
        self.assertFalse(artifact["historical_truth"])
        self.assertFalse(artifact["comparison_eligible"])
        self.assertNotIn("load_dynamic_v2", json.dumps(artifact, sort_keys=True))

        wrong_command = {**wire, "command": "load_dynamic_v2"}
        with self.assertRaisesRegex(Exception, "must be load_dynamic_v3"):
            dynamic_rollout_load_from_adapter_wire_v3(wrong_command)
        promoted = copy.deepcopy(compiled.artifact)
        promoted["comparison_eligible"] = True
        with self.assertRaises(FuryDynamicTargetSemanticsV5Error):
            validate_dynamic_idle_binding_v5(promoted, request=request)
        tampered = copy.deepcopy(compiled.artifact)
        tampered["claim_boundary"].append("comparison admitted")
        tampered["content_address"]["sha256"] = sha256_json(
            {
                key: value
                for key, value in tampered.items()
                if key != "content_address"
            }
        )
        with self.assertRaisesRegex(Exception, "scientific boundary"):
            validate_dynamic_idle_binding_v5(tampered, request=request)


if __name__ == "__main__":
    unittest.main()
