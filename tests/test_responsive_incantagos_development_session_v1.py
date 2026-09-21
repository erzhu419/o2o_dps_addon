from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    ABLATION_D,
    DynamicTeamRuntimeV1,
)
from o2o_dps.responsive_incantagos_development_session_v1 import (
    STATUS,
    ResponsiveIncantagosDevelopmentSessionV1Error,
    bind_responsive_incantagos_development_session_v1,
    responsive_incantagos_development_prefix_sha256_v1,
)
from o2o_dps.responsive_team_bridge_adapter_v1 import (
    LoadedResponsiveTeammateModelV1,
    TeammateModelProvenanceV1,
    WAKE_SCHEMA,
)
from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicLoadReceiptV4,
    DynamicLoadResultV4,
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    SCHEMA as CASE_SCHEMA,
    STATUS as CASE_STATUS,
    CompiledResponsiveIncantagosCaseV1,
)


CANDIDATE = "0x0000000000CANDIDATE"
TEAMMATE = "0x0000000000TEAMMATE"
BOSS = "0xF130000000BOSS"
ADD = "0xF130000000ADD"


class _FixedModel:
    def __init__(self) -> None:
        self.variant_id = ABLATION_D
        self.model_content_sha256 = "d" * 64
        self.delay_inputs: list[dict] = []

    def sample_delay(self, *, actor, timing_state, rng):
        self.delay_inputs.append(timing_state)
        return {
            "delay_ms": 100,
            "delay_bucket": 1,
            "context_level": "FIXED_TEST",
            "support": 1,
        }

    def sample_emission(self, *, actor, emission_state, rng):
        return {
            "event_type": "DMG",
            "spell_id": 1,
            "spell_name": "fixture",
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "exact_source_guid": actor["player_guid"],
            "target_mode": "STAY_ALIVE",
            "sampled_damage": 7,
            "context_level": "FIXED_TEST",
            "support": 1,
        }


class _Bridge:
    def __init__(self) -> None:
        self.generation = 3
        self.config_digest: str | None = None
        self.loaded_request = None
        self.loaded_seed = None
        self.armed = None

    def load_dynamic_v4(self, request, seed, config):
        self.loaded_request = request
        self.loaded_seed = seed
        self.config_digest = config.content_sha256
        return DynamicLoadResultV4(
            receipt=DynamicLoadReceiptV4(
                schema="o2o_dynamic_load_receipt/v4",
                config_digest=config.content_sha256,
                environment_generation=self.generation,
                target_count=len(config.target_health),
                background_event_count=0,
                attackability_event_count=0,
                effective_armor_event_count=0,
                same_timestamp_order=config.same_timestamp_order,
                retarget_mode=config.retarget_mode,
                idle_advance_mode=config.idle_advance_mode,
                idle_advance_horizon_ms=config.idle_advance_horizon_ms,
                idle_advance_receipt_schema="o2o_dynamic_idle_advance_receipts/v3",
            ),
            state={"time_ms": 0},
        )

    def dynamic_damage_receipts(self, *, cursor=0):
        return SimpleNamespace(
            environment_generation=self.generation,
            config_digest=self.config_digest,
            cursor=cursor,
            next_cursor=cursor,
            receipts=(),
        )

    def dynamic_candidate_damage_receipts(self, *, cursor=0):
        return SimpleNamespace(
            environment_generation=self.generation,
            config_digest=self.config_digest,
            cursor=cursor,
            next_cursor=cursor,
            receipts=(),
        )

    def _request(self, command, **payload):
        assert command == "arm_dynamic_team_wake"
        wake = payload["responsive"]
        assert wake["schema"] == WAKE_SCHEMA
        self.armed = dict(wake)
        return {
            "responsive_team_wake": {**wake, "status": "ARMED"},
            "environment_generation": self.generation,
        }


def _loaded_model(*, heldout: bool = False) -> LoadedResponsiveTeammateModelV1:
    model = _FixedModel()
    provenance = TeammateModelProvenanceV1(
        source_artifact_schema="fixture-hpc-result/v1",
        source_artifact_content_sha256="a" * 64,
        model_content_sha256=model.model_content_sha256,
        variant_id=model.variant_id,
        training_scope="FIXTURE_DEVELOPMENT_ONLY",
        current_source_held_out=heldout,
    )
    return LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=provenance,
        result_content_sha256="a" * 64,
        current_source_evidence={
            "schema": "fixture-current-source-evidence/v1",
            "current_source_stage5_content_sha256": "b" * 64,
            "current_source_component_id": "component-train",
            "current_source_held_out": heldout,
        },
    )


def _case(*, add_introduced_at_ms: int = 20):
    config = DynamicTargetSemanticsConfigV4(
        target_health=(
            DynamicTargetHealthV4(0, 1_000, 1_000),
            DynamicTargetHealthV4(1, 100, 100),
        ),
        idle_advance_horizon_ms=150,
    )
    actors = (
        {
            "player_guid": CANDIDATE,
            "class": "WARRIOR",
            "spec_key": "WARRIOR_FURY",
        },
        {
            "player_guid": TEAMMATE,
            "class": "MAGE",
            "spec_key": "MAGE_SPEC_NOT_AVAILABLE",
        },
    )
    introductions = {BOSS: 0, ADD: add_introduced_at_ms}
    runtime = DynamicTeamRuntimeV1(
        actors=actors,
        target_health_by_guid={BOSS: 1_000, ADD: 100},
        target_introduced_at_ms_by_guid=introductions,
    )
    receipt = {
        "schema": CASE_SCHEMA,
        "status": CASE_STATUS,
        "source": {
            "instance_id": "instance-1",
            "encounter_id": "encounter-1",
            "wave_id": "wave-1",
            "focal_player_guid": CANDIDATE,
            "source_membership_evidence": {
                "stage5_content_sha256": "b" * 64,
                "component_id": "component-train",
                "split": "TRAIN",
                "heldout_performance_evidence_eligible": False,
                "comparison_authorized": False,
            },
        },
        "scientific_boundaries": {
            "development_only": True,
            "comparison_authorized": False,
            "policy_training_authorized": False,
            "deployment_authorized": False,
            "heldout_performance_evidence_eligible": False,
            "actor_roster_is_outcome_derived": True,
        },
    }
    return CompiledResponsiveIncantagosCaseV1(
        request={"schema": "fixture-request", "encounter": {"targets": [{}, {}]}},
        dynamic_config=config,
        runtime=runtime,
        native_target_guids=(BOSS, ADD),
        target_introduced_at_ms_by_guid=introductions,
        actors=actors,
        candidate_player_guid=CANDIDATE,
        teammate_player_guids=(TEAMMATE,),
        team_only_sidecar=(),
        receipt=receipt,
    )


def test_binds_load_branch_adapter_and_initial_global_wake() -> None:
    case = _case()
    loaded = _loaded_model()
    bridge = _Bridge()

    session = bind_responsive_incantagos_development_session_v1(
        bridge=bridge,
        case=case,
        loaded_model=loaded,
        simulator_seed=123,
        teammate_seed=456,
        pair_id="pair-development",
        branch_id="branch-a",
        candidate_suffix_id="candidate-a",
    )

    assert bridge.loaded_request == case.request
    assert bridge.loaded_request is not case.request
    assert bridge.loaded_seed == 123
    assert session.branch.environment_generation == 3
    assert session.branch.dynamic_config_sha256 == case.dynamic_config.content_sha256
    assert session.adapter.runtime is not case.runtime
    assert session.initial_wake is not None
    assert session.initial_wake["actor_guid"] == TEAMMATE
    assert session.initial_wake["time_ms"] == 100
    assert bridge.armed["wake_id"] == session.initial_wake["wake_id"]
    assert loaded.model.delay_inputs[0]["target_state"]["alive_target_guids"] == [BOSS]
    assert session.adapter.runtime.remaining_target_guids() == [ADD, BOSS]
    assert session.receipt["status"] == STATUS
    assert session.receipt["future_target_arrival_supported"] is True
    assert session.receipt["exact_source_bound_worker_used"] is False
    boundaries = session.receipt["scientific_boundaries"]
    assert boundaries["comparison_authorized"] is False
    assert boundaries["policy_training_authorized"] is False
    assert boundaries["deployment_authorized"] is False


def test_prefix_identity_binds_future_target_introduction() -> None:
    loaded = _loaded_model()
    early = responsive_incantagos_development_prefix_sha256_v1(
        _case(add_introduced_at_ms=20), loaded
    )
    later = responsive_incantagos_development_prefix_sha256_v1(
        _case(add_introduced_at_ms=30), loaded
    )
    assert early != later


def test_rejects_heldout_model_claim_for_nonheldout_case() -> None:
    with pytest.raises(
        ResponsiveIncantagosDevelopmentSessionV1Error,
        match="differs from case model-training split",
    ):
        bind_responsive_incantagos_development_session_v1(
            bridge=_Bridge(),
            case=_case(),
            loaded_model=_loaded_model(heldout=True),
            simulator_seed=123,
            teammate_seed=456,
            pair_id="pair-development",
            branch_id="branch-a",
            candidate_suffix_id="candidate-a",
        )


def test_validation_model_source_binds_without_performance_authority() -> None:
    case = _case()
    receipt = dict(case.receipt)
    source = dict(receipt["source"])
    source["source_membership_evidence"] = {
        **source["source_membership_evidence"],
        "split": "VALIDATION",
        "model_training_held_out": True,
    }
    receipt["source"] = source
    validation_case = replace(case, receipt=receipt)

    session = bind_responsive_incantagos_development_session_v1(
        bridge=_Bridge(),
        case=validation_case,
        loaded_model=_loaded_model(heldout=True),
        simulator_seed=123,
        teammate_seed=456,
        pair_id="pair-validation-development",
        branch_id="branch-a",
        candidate_suffix_id="candidate-a",
    )

    assert session.initial_wake is not None
    assert session.receipt["scientific_boundaries"]["comparison_authorized"] is False
    assert session.receipt["scientific_boundaries"]["heldout_performance_evidence_eligible"] is False


def test_rejects_case_whose_noncomparison_boundary_was_changed() -> None:
    case = _case()
    receipt = dict(case.receipt)
    receipt["scientific_boundaries"] = {
        **receipt["scientific_boundaries"],
        "comparison_authorized": True,
    }
    changed = replace(case, receipt=receipt)
    with pytest.raises(
        ResponsiveIncantagosDevelopmentSessionV1Error,
        match="development-only scope",
    ):
        responsive_incantagos_development_prefix_sha256_v1(
            changed, _loaded_model()
        )
