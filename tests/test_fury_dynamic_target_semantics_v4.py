from __future__ import annotations

import copy
import hashlib
import json
import unittest

from o2o_dps.fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_dynamic_target_semantics_v4 import (
    DynamicRolloutLoadV2,
    DynamicScenarioSourceBindingV4,
    FuryDynamicTargetSemanticsV4Error,
    compile_dynamic_target_semantics_binding_v4,
    dynamic_rollout_load_from_adapter_wire_v2,
    validate_dynamic_target_semantics_binding_v4,
)
from o2o_dps.fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
)
from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
)
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
    DynamicTargetSemanticsConfigV2,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _evidence(
    kind: ContraEvidenceKindV2,
    label: str,
) -> ContraFieldEvidenceV2:
    if kind is ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
        return ContraFieldEvidenceV2(
            kind,
            corpus_sha256=_digest("fixture-corpus"),
            hypothesis_id=label,
        )
    return ContraFieldEvidenceV2(kind, source_sha256=_digest(label))


def request_v4() -> dict:
    stats = [0.0] * 35
    stats[26] = 1721.0
    stats[34] = 200.0
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "distanceFromTarget": 3,
                            "equipment": {"items": [{}, {}]},
                        }
                    ]
                }
            ]
        },
        "encounter": {
            "duration": 2,
            "durationVariation": 0,
            "useHealth": True,
            "targets": [
                {"name": "Target 0", "level": 63, "stats": stats}
            ],
        },
        "simOptions": {"iterations": 1, "interactive": True},
    }


def config_v4() -> DynamicTargetSemanticsConfigV2:
    return DynamicTargetSemanticsConfigV2(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        background_damage_events=(
            BackgroundDamageEventV1(0, 0, 0, "same-time-team-hit", 10.0),
        ),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, False),
            DynamicAttackabilityEventV2(1, 100, 0, True),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, 1721.0),
            DynamicEffectiveArmorEventV2(1, 100, 0, 1234.0),
        ),
    )


def context_v4() -> TargetSemanticsContextV3:
    return TargetSemanticsContextV3(
        context_id="target-0-live-health-dynamic-v2",
        mode=TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
        target_index=0,
        target_classification=ContraTargetClassificationV2.ELITE,
        target_name="Target 0",
        equipped_item_names=(),
        target_classification_evidence=_evidence(
            ContraEvidenceKindV2.OBSERVED_SOURCE, "classification"
        ),
        target_name_evidence=_evidence(
            ContraEvidenceKindV2.OBSERVED_SOURCE, "target-name"
        ),
        equipment_evidence=_evidence(
            ContraEvidenceKindV2.PINNED_STATIC_INPUT, "equipment"
        ),
        target_position_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            "stacked-position-v1",
        ),
        target_health_pct_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            "initial-health-v2",
        ),
        target_max_health_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            "initial-health-v2",
        ),
        target_max_health=200,
    )


def identity_v4() -> dict:
    return {
        "instance_id": "instance-1",
        "component_id": "component-1",
        "scenario_id": "scenario-1-dynamic-v4",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 2000,
        "corpus_entry_sha256": _digest("corpus-entry"),
        "source_scenario_sha256": _digest("source-scenario"),
        "catalog_sha256": _digest("catalog"),
    }


def source_v4(config: DynamicTargetSemanticsConfigV2):
    return DynamicScenarioSourceBindingV4.bind_config(
        config=config,
        background_draw_content_sha256=_digest("background-draw"),
        target_health_hypothesis_id="initial-health-v2",
        target_semantics_source_sha256=_digest("target-semantics-source"),
    )


class FuryDynamicTargetSemanticsV4Tests(unittest.TestCase):
    def test_compile_binds_executable_schedules_without_truth_promotion(self):
        config = config_v4()
        compiled = compile_dynamic_target_semantics_binding_v4(
            scenario_identity=identity_v4(),
            request=request_v4(),
            dynamic_config=config,
            target_contexts={0: context_v4()},
            source_binding=source_v4(config),
            seed=2026091101,
        )
        artifact = validate_dynamic_target_semantics_binding_v4(
            compiled.artifact
        )

        self.assertEqual(
            "load_dynamic_v2",
            artifact["bridge_capability"]["load_command"],
        )
        self.assertFalse(artifact["claim_boundary"]["comparison_eligible"])
        self.assertFalse(artifact["claim_boundary"]["historical_truth"])
        scenario = compiled.scenario
        self.assertEqual(
            config.content_sha256,
            scenario["dynamic_load_config"]["content_sha256"],
        )
        receipt = scenario["scenario_model"]["dynamic_semantics_receipt"]
        self.assertFalse(
            receipt["executed_by_load_dynamic_v2"][
                "python_policy_pause_emulation"
            ]
        )
        self.assertFalse(receipt["exact_historical_armor_claimed"])
        self.assertFalse(receipt["exact_historical_attackability_claimed"])
        serialized = json.dumps(artifact, sort_keys=True)
        self.assertNotIn("load_dynamic_v1", serialized)

    def test_adapter_wire_and_content_addresses_fail_closed(self):
        request = request_v4()
        config = config_v4()
        wire = {
            "command": "load_dynamic_v2",
            "request": request,
            "seed": 7,
            "dynamic": config.to_wire(),
        }
        parsed_request, load = dynamic_rollout_load_from_adapter_wire_v2(wire)
        self.assertEqual(request, parsed_request)
        self.assertIsInstance(load, DynamicRolloutLoadV2)
        self.assertEqual(config.content_sha256, load.config.content_sha256)

        with self.assertRaisesRegex(Exception, "field set mismatch"):
            dynamic_rollout_load_from_adapter_wire_v2({**wire, "extra": True})
        wrong_command = {**wire, "command": "load_dynamic_v1"}
        with self.assertRaisesRegex(Exception, "must be load_dynamic_v2"):
            dynamic_rollout_load_from_adapter_wire_v2(wrong_command)
        tampered = copy.deepcopy(wire)
        tampered["dynamic"]["effective_armor_events"][1][
            "effective_armor"
        ] = 999.0
        with self.assertRaisesRegex(Exception, "content SHA-256 mismatch"):
            dynamic_rollout_load_from_adapter_wire_v2(tampered)
        with self.assertRaisesRegex(Exception, "signed int64"):
            DynamicRolloutLoadV2.bind(request, 1 << 80, config)

    def test_compiled_artifact_rejects_claim_and_projection_tampering(self):
        config = config_v4()
        compiled = compile_dynamic_target_semantics_binding_v4(
            scenario_identity=identity_v4(),
            request=request_v4(),
            dynamic_config=config,
            target_contexts={0: context_v4()},
            source_binding=source_v4(config),
            seed=11,
        )
        promoted = copy.deepcopy(compiled.artifact)
        promoted["claim_boundary"]["historical_truth"] = True
        with self.assertRaises(FuryDynamicTargetSemanticsV4Error):
            validate_dynamic_target_semantics_binding_v4(promoted)

        drifted = copy.deepcopy(compiled.artifact)
        drifted["scenario_projection"]["scenario"]["dynamic_load_config"][
            "effective_armor_events"
        ][0]["effective_armor"] = 1700.0
        with self.assertRaises(FuryDynamicTargetSemanticsV4Error):
            validate_dynamic_target_semantics_binding_v4(drifted)


if __name__ == "__main__":
    unittest.main()
