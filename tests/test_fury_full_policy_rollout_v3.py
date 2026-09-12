from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

from o2o_dps.fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter
from o2o_dps.fury_full_policy_rollout_v3 import (
    DynamicRolloutLoadV1,
    EXPECTED_V2_SOURCE_SHA256,
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
    run_fury_full_policy_rollout_v3,
    validate_v2_execution_base_identity_v3,
)
from o2o_dps.sim_bridge import (
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
)
from tests.test_fury_full_policy_rollout_v2 import (
    _DynamicFullBridge,
    _evidence,
    _request,
)


class FuryFullPolicyRolloutV3Tests(unittest.TestCase):
    def _hypothesis_context(self) -> TargetSemanticsContextV3:
        return TargetSemanticsContextV3(
            context_id="target-0-live-simulator-hypothesis-v1",
            mode=TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
            target_index=0,
            target_classification=ContraTargetClassificationV2.ELITE,
            target_name="Target 0",
            equipped_item_names=(),
            target_classification_evidence=_evidence(
                ContraEvidenceKindV2.OBSERVED_SOURCE
            ),
            target_name_evidence=_evidence(
                ContraEvidenceKindV2.OBSERVED_SOURCE
            ),
            equipment_evidence=_evidence(),
            target_position_evidence=_evidence(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                hypothesis_id="fixture-position-v1",
            ),
            target_health_pct_evidence=_evidence(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                hypothesis_id="fixture-live-health-v1",
            ),
            target_max_health_evidence=_evidence(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                hypothesis_id="fixture-live-health-v1",
            ),
            target_max_health=200,
        )

    def test_live_health_is_read_at_each_decision_and_never_promotes(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),)
        )
        dynamic_load = DynamicRolloutLoadV1.bind(request, 2026091101, config)

        result = run_fury_full_policy_rollout_v3(
            _DynamicFullBridge(),
            request,
            CatFurySourceAdapter(),
            seed=2026091101,
            target_contexts={0: self._hypothesis_context()},
            dynamic_load=dynamic_load,
        )

        self.assertEqual("COMPLETE_NONFAITHFUL", result["status"])
        self.assertEqual(
            [100.0, 50.0],
            [step["target_semantics"]["target_health_pct"] for step in result["steps"]],
        )
        self.assertFalse(result["ordered_projection_faithful"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        self.assertIn(
            "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
            {row["code"] for row in result["blockers"]},
        )
        self.assertIn(
            {
                "code": "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
                "count": 1,
            },
            result["blocker_summary"],
        )
        receipt = result["target_context_receipts"][0]
        self.assertEqual("SIMULATOR_HYPOTHESIS", receipt["mode"])
        self.assertFalse(receipt["exact_by_declared_contract"])

    def test_v3_overlay_attests_the_immutable_v2_base(self):
        observed = validate_v2_execution_base_identity_v3()
        self.assertEqual(EXPECTED_V2_SOURCE_SHA256, observed)
        from o2o_dps import fury_full_policy_rollout_v2 as base

        self.assertEqual(
            EXPECTED_V2_SOURCE_SHA256,
            hashlib.sha256(Path(base.__file__).read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
