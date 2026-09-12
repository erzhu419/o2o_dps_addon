from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import unittest

from o2o_dps.fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_dynamic_target_semantics_v3 import (
    DynamicScenarioSourceBindingV3,
    FuryDynamicTargetSemanticsV3Error,
    RetainedTargetScheduleEvidenceV3,
    compile_dynamic_target_semantics_binding_v3,
    dynamic_v2_upstream_gap_contract_v3,
    validate_dynamic_target_semantics_binding_v3,
)
from o2o_dps.fury_full_policy_rollout_v3 import (
    HealthPercentPointV2,
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
)
from o2o_dps.fury_full_policy_rollout_v3 import _resolve_target_semantics_v3
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    FuryPairedRunnerError,
    build_runner_plan,
    runner_scenario_bundle_sha256,
)
from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
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
            corpus_sha256=_digest("corpus"),
            hypothesis_id=label,
        )
    return ContraFieldEvidenceV2(kind, source_sha256=_digest(label))


def _request(*, health: float = 200.0, armor: float = 1721.0) -> dict:
    stats = [0.0] * 35
    stats[26] = armor
    stats[34] = health
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
            "targets": [{"name": "Target 0", "level": 63, "stats": stats}],
        },
        "simOptions": {"iterations": 1},
    }


def _config(*, damage: float = 50.0) -> DynamicTeamBackgroundConfigV1:
    return DynamicTeamBackgroundConfigV1(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        background_damage_events=(
            BackgroundDamageEventV1(0, 500, 0, "team-hit-0", damage),
        ),
        retarget_mode="NEXT_ALIVE_CYCLIC",
    )


def _context(*, target_max_health: int = 200) -> TargetSemanticsContextV3:
    return TargetSemanticsContextV3(
        context_id="target-0-live-health-hypothesis-v1",
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
            "initial-health-v1",
        ),
        target_max_health_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            "initial-health-v1",
        ),
        target_max_health=target_max_health,
    )


def _identity() -> dict:
    return {
        "instance_id": "instance-1",
        "component_id": "component-1",
        "scenario_id": "scenario-1-dynamic-v3",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 2000,
        "corpus_entry_sha256": _digest("corpus-entry"),
        "source_scenario_sha256": _digest("source-scenario"),
        "catalog_sha256": _digest("catalog"),
    }


def _source() -> DynamicScenarioSourceBindingV3:
    return DynamicScenarioSourceBindingV3(
        background_draw_content_sha256=_digest("background-draw"),
        background_schedule_content_sha256=_digest("background-schedule"),
        target_health_hypothesis_content_sha256=_digest("health-hypothesis"),
        target_health_hypothesis_id="initial-health-v1",
        target_semantics_source_sha256=_digest("target-semantics"),
    )


def _retained(*, armor_hash: str | None = None):
    return (
        RetainedTargetScheduleEvidenceV3(
            target_index=0,
            armor_schedule_content_sha256=armor_hash or _digest("armor-schedule"),
            armor_transition_count=3,
            attackability_schedule_content_sha256=_digest(
                "attackability-schedule"
            ),
            attackability_transition_count=2,
        ),
    )


def _compiled():
    return compile_dynamic_target_semantics_binding_v3(
        scenario_identity=_identity(),
        request=_request(),
        dynamic_config=_config(),
        target_contexts={0: _context()},
        source_binding=_source(),
        retained_schedules=_retained(),
    )


class FuryDynamicTargetSemanticsV3Tests(unittest.TestCase):
    def test_simulator_hypothesis_reads_live_counterfactual_health(self):
        state = {
            "time_ms": 750,
            "target_index": 0,
            "num_targets": 1,
            "total_target_count": 1,
            "target_health_known": True,
            "target_health_percent": 25.0,
            "target_health_max": 200.0,
            "dynamic_team_background": {
                "targets": [{"target_index": 0, "dead": False}]
            },
        }
        resolved = _resolve_target_semantics_v3(
            state,
            _request(),
            {0: _context()},
        )
        self.assertEqual(25.0, resolved["target_health_pct"])
        self.assertEqual(200, resolved["target_max_health"])
        self.assertFalse(resolved["exact_by_declared_contract"])
        self.assertEqual(
            "SENSITIVITY_HYPOTHESIS",
            resolved["field_evidence"]["target_health_pct"].kind.value,
        )

        drifted = copy.deepcopy(state)
        drifted["target_health_max"] = 201.0
        with self.assertRaisesRegex(
            Exception, "bridge target_health_max differs"
        ):
            _resolve_target_semantics_v3(drifted, _request(), {0: _context()})

    def test_dynamic_v1_subset_is_executable_but_permanently_nonvoting(self):
        compiled = _compiled()
        artifact = validate_dynamic_target_semantics_binding_v3(compiled.artifact)
        scenario = compiled.scenario

        self.assertTrue(artifact["claim_boundary"]["bridge_execution_eligible"])
        self.assertFalse(artifact["claim_boundary"]["comparison_eligible"])
        self.assertEqual(
            "o2o_dynamic_team_background/v1",
            scenario["dynamic_load_config"]["schema"],
        )
        receipt = scenario["scenario_model"]["dynamic_semantics_receipt"]
        self.assertTrue(
            receipt["executed_by_load_dynamic_v1"][
                "live_policy_health_from_bridge_state"
            ]
        )
        self.assertEqual(
            "RETAINED_PROVENANCE_NOT_EXECUTED",
            receipt["retained_not_executed"][0]["armor_schedule"]["status"],
        )
        context = scenario["target_context_bundle"]["contexts"][0]
        self.assertEqual("SIMULATOR_HYPOTHESIS", context["mode"])
        self.assertFalse(context["exact_by_declared_contract"])
        self.assertEqual([], context["health_pct_schedule"])

        plan = build_runner_plan(
            protocol_id="dynamic-v3-diagnostic-test",
            protocol_sha256=_digest("protocol"),
            phase="dynamic_diagnostic",
            corpus_manifest_sha256=_digest("corpus-manifest"),
            runner_inputs_sha256=_digest("runner-inputs"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                [scenario]
            ),
            corpus_binding_sha256=_digest("corpus-binding"),
            master_seeds=(101,),
            scenarios=(scenario,),
            policies=(
                {
                    "policy_id": "cat.fury.profile1",
                    "source_sha256": _digest("cat-source"),
                    "adapter_sha256": _digest("cat-adapter"),
                    "profile_sha256": _digest("cat-profile"),
                    "role": "BASELINE",
                },
            ),
            shard_count=1,
            bridge_identity={
                "sha256": _digest("bridge"),
                "platform": "windows-amd64",
            },
            execution_bundle_identity={
                "python_source_closure_sha256": _digest("closure"),
                "ordered_sink_executor_sha256": _digest("ordered"),
                "full_policy_rollout_executor_sha256": _digest("rollout"),
                "paired_runner_source_sha256": _digest("runner"),
                "evaluation_source_sha256": _digest("evaluation"),
                "runtime_snapshot_sha256": _digest("snapshot"),
            },
            execution_mode=SINGLE_BRIDGE_MODE,
            seed_namespace="dynamic-v3-diagnostic-test",
            plan_intent=DIAGNOSTIC_INTENT,
        )
        self.assertEqual("DIAGNOSTIC_NONVOTING", plan["contract"]["plan_intent"])

        with self.assertRaisesRegex(
            FuryPairedRunnerError, "non-comparison scenario models"
        ):
            build_runner_plan(
                protocol_id="dynamic-v3-illegal-comparison",
                protocol_sha256=_digest("protocol"),
                phase="development",
                corpus_manifest_sha256=_digest("corpus-manifest"),
                runner_inputs_sha256=_digest("runner-inputs"),
                runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                    [scenario]
                ),
                corpus_binding_sha256=_digest("corpus-binding"),
                master_seeds=(101,),
                scenarios=(scenario,),
                policies=plan["contract"]["policies"],
                shard_count=1,
                bridge_identity=plan["contract"]["bridge_identity"],
                execution_bundle_identity=plan["contract"][
                    "execution_bundle_identity"
                ],
                execution_mode=SINGLE_BRIDGE_MODE,
                seed_namespace="dynamic-v3-illegal-comparison",
                plan_intent=COMPARISON_INTENT,
            )

    def test_every_material_input_changes_the_content_address(self):
        first = _compiled().artifact["content_address"]["sha256"]
        changed_damage = compile_dynamic_target_semantics_binding_v3(
            scenario_identity=_identity(),
            request=_request(),
            dynamic_config=_config(damage=51.0),
            target_contexts={0: _context()},
            source_binding=_source(),
            retained_schedules=_retained(),
        ).artifact["content_address"]["sha256"]
        changed_schedule = compile_dynamic_target_semantics_binding_v3(
            scenario_identity=_identity(),
            request=_request(),
            dynamic_config=_config(),
            target_contexts={0: _context()},
            source_binding=_source(),
            retained_schedules=_retained(armor_hash=_digest("other-armor")),
        ).artifact["content_address"]["sha256"]
        self.assertEqual(3, len({first, changed_damage, changed_schedule}))

    def test_health_hypothesis_must_match_bridge_config_and_never_be_exact(self):
        with self.assertRaisesRegex(
            FuryDynamicTargetSemanticsV3Error, "context max health differs"
        ):
            compile_dynamic_target_semantics_binding_v3(
                scenario_identity=_identity(),
                request=_request(),
                dynamic_config=_config(),
                target_contexts={0: _context(target_max_health=201)},
                source_binding=_source(),
                retained_schedules=_retained(),
            )

        bad_context = replace(
            _context(),
            mode=TargetSemanticsModeV3.SENSITIVITY,
            health_pct_schedule=(
                HealthPercentPointV2(0, 100.0),
                HealthPercentPointV2(2000, 0.0),
            ),
        )
        with self.assertRaisesRegex(
            FuryDynamicTargetSemanticsV3Error, "SIMULATOR_HYPOTHESIS"
        ):
            compile_dynamic_target_semantics_binding_v3(
                scenario_identity=_identity(),
                request=_request(),
                dynamic_config=_config(),
                target_contexts={0: bad_context},
                source_binding=_source(),
                retained_schedules=_retained(),
            )

    def test_tampering_and_schedule_emulation_claims_fail_closed(self):
        artifact = copy.deepcopy(_compiled().artifact)
        artifact["claim_boundary"]["exact_target_health"] = True
        with self.assertRaisesRegex(
            FuryDynamicTargetSemanticsV3Error, "claim boundary"
        ):
            validate_dynamic_target_semantics_binding_v3(artifact)

        gap = dynamic_v2_upstream_gap_contract_v3()
        self.assertFalse(gap["current_pinned_bridge_supports_this_contract"])
        self.assertIn(
            "python_policy_only_action_suppression", gap["forbidden_emulations"]
        )
        self.assertIn(
            "direct_periodic_aoe_and_travel_damage_resolution_without_rng_rewind",
            gap["attackability_application_sites"],
        )


if __name__ == "__main__":
    unittest.main()
