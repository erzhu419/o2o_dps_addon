from __future__ import annotations

import unittest
from unittest.mock import patch

from o2o_dps.contra_incantagos_v4_policy_binding_v1 import (
    bind_contra_incantagos_v4_policy_v1,
    propose_contra260817_incantagos_v4_v1,
    propose_deployed_contra_incantagos_v4_v1,
)
from o2o_dps.contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
)
from o2o_dps.contra260817_fury_full_policy_v3 import (
    POLICY_ID, Contra260817FuryFullPolicyAdapterV3,
)
from o2o_dps.fury_contra_adapter_v2 import ContraEvidenceKindV2
from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from o2o_dps.upper_kara_resolved_dynamic_v4_adapter_v1 import (
    CompiledResolvedIncantagosDynamicV4CaseV1,
    ResolvedDynamicLoadV4,
    ResolvedDynamicTargetIndexV1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)


def _case() -> CompiledResponsiveIncantagosCaseV1:
    config = DynamicTargetSemanticsConfigV4(
        target_health=(
            DynamicTargetHealthV4(0, 10000.0, 10000.0),
            DynamicTargetHealthV4(1, 2000.0, 2000.0),
        ),
        idle_advance_horizon_ms=10000,
    )
    return CompiledResponsiveIncantagosCaseV1(
        request={
            "raid": {"parties": [{"players": [{
                "name": "synthetic", "distanceFromTarget": 5,
                "equipment": {"items": [{"id": 18832}, {"id": 19866}]},
            }]}]},
            "encounter": {"duration": 10, "targets": [
                {"name": "guid-boss"}, {"name": "guid-add"},
            ]},
        },
        dynamic_config=config,
        runtime=None,  # Only the compiled request/target contract is read here.
        native_target_guids=("guid-boss", "guid-add"),
        target_introduced_at_ms_by_guid={},
        actors=(),
        candidate_player_guid="player",
        teammate_player_guids=(),
        team_only_sidecar=(),
        receipt={"source": {"instance_id": "instance-1", "encounter_id": "encounter-1"}},
    )


def _fixed_case(case: CompiledResponsiveIncantagosCaseV1):
    registry = (
        ResolvedDynamicTargetIndexV1(0, "boss", "guid-boss", "REGISTRY", 61946),
        ResolvedDynamicTargetIndexV1(1, "add", "guid-add", "REGISTRY", 59989),
    )
    return CompiledResolvedIncantagosDynamicV4CaseV1(
        request=case.request,
        dynamic_config=case.dynamic_config,
        dynamic_load=ResolvedDynamicLoadV4(case.dynamic_config),
        occurrence_index_registry=registry,
        boss_index=0,
        priority_add_indexes=(1,),
        collateral_only_indexes=(),
        optional_actionable_indexes=(),
        team_only_sidecar=(),
        observation_provider=None,
        target_gate=None,
        receipt={"source": {"instance_id": "instance-1", "encounter_id": "encounter-1"}},
    )


def _metadata():
    return {"id": "instance-1", "units": {
        "guid-boss": {"name": "Chronicle Incantagos", "entry": 61946},
        "guid-add": {"name": "Chronicle Guardian", "entry": 59989},
    }}


def _binding():
    case = _case()
    return bind_contra_incantagos_v4_policy_v1(
        _fixed_case(case), case, _metadata(),
        source_metadata_artifact_sha256="a" * 64,
        classification_hypotheses_by_occurrence_id={
            "boss": "worldboss", "add": "elite",
        },
        equipment_request=case.request,
        equipped_item_names=("test mainhand", "test offhand"),
    )


def _live_state() -> dict:
    return {
        "target_index": 1,
        "num_targets": 2,
        "total_target_count": 2,
        "target_health_known": True,
        "target_health_max": 2000,
        "target_health_percent": 25.0,
        "time_ms": 150,
        "power": {"type": "rage", "current": 60.0, "maximum": 100.0},
        "mh_swing_remaining_ms": 1000,
        "mh_swing_duration_ms": 2400,
        "oh_swing_remaining_ms": 800,
        "gcd_remaining_ms": 0,
        "auras": [],
        "dynamic_team_background": {
            "targets": [
                {"target_index": 0, "dead": False},
                {"target_index": 1, "dead": False},
            ],
        },
        "dynamic_target_semantics": {
            "targets": [
                {"target_index": 0, "dead": False, "attackable": True, "effective_armor": 1721},
                {"target_index": 1, "dead": False, "attackable": True, "effective_armor": 900},
            ],
        },
    }


class ContraIncantagosV4BindingTests(unittest.TestCase):
    def test_explicit_hypotheses_and_live_target_state(self):
        binding = _binding()
        target = binding.resolve_target(_live_state())
        self.assertEqual(target["target_index"], 1)
        self.assertEqual(target["target_health_pct"], 25.0)
        self.assertEqual(target["target_max_health"], 2000)
        self.assertEqual(target["dynamic_effective_armor"], 900)
        self.assertEqual(target["target_classification"], "elite")
        self.assertEqual(target["target_name"], "Chronicle Guardian")
        self.assertEqual(
            [(row.operation, row.value, row.target_index) for row in binding.exact_guid_target_bindings()],
            [("TargetUnit", "guid-boss", 0), ("TargetUnit", "guid-add", 1)],
        )
        self.assertTrue(binding.receipt["chronicle_metadata_name_bound"])
        self.assertFalse(binding.receipt["game_client_unit_name_observed"])
        self.assertEqual(
            binding.target_contexts[1].target_name_evidence.kind,
            ContraEvidenceKindV2.OBSERVED_SOURCE,
        )
        self.assertEqual(binding.receipt["source_metadata_artifact_sha256"], "a" * 64)
        self.assertEqual(binding.request["encounter"]["targets"][1]["name"], "Chronicle Guardian")
        self.assertFalse(binding.receipt["same_gear_as_historical_focal_verified"])
        self.assertFalse(binding.receipt["comparison_authorized"])

    def test_metadata_name_projection_does_not_mutate_load_request(self):
        case = _case()
        binding = bind_contra_incantagos_v4_policy_v1(
            _fixed_case(case), case, _metadata(),
            source_metadata_artifact_sha256="a" * 64,
            classification_hypotheses_by_occurrence_id={
                "boss": "worldboss", "add": "elite",
            },
            equipment_request=case.request,
            equipped_item_names=("test mainhand", "test offhand"),
        )
        self.assertEqual(case.request["encounter"]["targets"][1]["name"], "guid-add")
        self.assertEqual(binding.request["encounter"]["targets"][1]["name"], "Chronicle Guardian")

    def test_missing_classification_or_guid_label_fails(self):
        case = _case()
        fixed = _fixed_case(case)
        kwargs = dict(
            source_metadata_artifact_sha256="a" * 64,
            equipment_request=case.request,
            equipped_item_names=("main", "off"),
        )
        with self.assertRaisesRegex(ValueError, "cover native targets"):
            bind_contra_incantagos_v4_policy_v1(
                fixed, case, _metadata(),
                classification_hypotheses_by_occurrence_id={"boss": "worldboss"},
                **kwargs,
            )
        with self.assertRaisesRegex(ValueError, "artifact identity"):
            bind_contra_incantagos_v4_policy_v1(
                fixed, case, _metadata(),
                source_metadata_artifact_sha256="not-an-artifact-sha",
                classification_hypotheses_by_occurrence_id={
                    "boss": "worldboss", "add": "elite",
                },
                equipment_request=case.request,
                equipped_item_names=("main", "off"),
            )
        metadata = _metadata()
        metadata["units"]["guid-add"]["entry"] = 0
        with self.assertRaisesRegex(ValueError, "entry differs"):
            bind_contra_incantagos_v4_policy_v1(
                fixed, case, metadata,
                classification_hypotheses_by_occurrence_id={
                    "boss": "worldboss", "add": "elite",
                },
                **kwargs,
            )

    def test_both_policy_entrypoints_use_the_v4_binding_not_a_v3_load(self):
        binding = _binding()
        state = _live_state()
        marker = object()
        with (
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1.validate_deployed_contra_runtime_binding_v1", return_value={}) as validate,
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1.RuntimeBoundContraRaidBAdapterV1") as deployed,
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1._combat_state", return_value=marker) as combat,
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1._proposal", return_value="deployed-decision") as proposal,
        ):
            self.assertEqual(
                propose_deployed_contra_incantagos_v4_v1(
                    binding, runtime_binding={}, state=state, available=(),
                    last_gcd_action="",
                ),
                "deployed-decision",
            )
            validate.assert_called_once()
            deployed.assert_called_once()
            self.assertEqual(combat.call_args.args[3]["target_name"], "Chronicle Guardian")
            proposal.assert_called_once()
        inputs = Contra260817SimulatorInputsV4(
            equipped_mainhand_name="test mainhand",
            equipped_offhand_name="test offhand",
        )
        with (
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1._contra_state_mapper") as mapper,
            patch("o2o_dps.contra_incantagos_v4_policy_binding_v1.validate_source_decision_v3", return_value="source-decision"),
        ):
            mapper.return_value.return_value = marker
            source = Contra260817FuryFullPolicyAdapterV3()
            source.propose = unittest.mock.Mock(return_value=marker)
            self.assertEqual(
                propose_contra260817_incantagos_v4_v1(
                    binding, controls=unittest.mock.Mock(), inputs=inputs,
                    source=source, state=state, available=(), last_gcd_action="",
                ),
                "source-decision",
            )
            self.assertEqual(mapper.call_args.args[2], binding.dynamic_config_digest)
            self.assertEqual(mapper.return_value.call_args.args[3]["target_name"], "Chronicle Guardian")
            source.propose.assert_called_once_with(marker)

    def test_contra260817_real_mapper_and_source_accept_live_v4_state(self):
        binding = _binding()
        controls = unittest.mock.Mock()
        controls.autoattack_active = False
        controls.equipped_name.side_effect = {16: "test mainhand", 17: "test offhand"}.get
        inputs = Contra260817SimulatorInputsV4(
            equipped_mainhand_name="test mainhand",
            equipped_offhand_name="test offhand",
        )
        decision = propose_contra260817_incantagos_v4_v1(
            binding, controls=controls, inputs=inputs,
            source=Contra260817FuryFullPolicyAdapterV3(),
            state=_live_state(), available=(), last_gcd_action="",
        )
        self.assertEqual(decision.provenance.expert_id, POLICY_ID)


if __name__ == "__main__":
    unittest.main()
