from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from o2o_dps.chronicle_external_team_wave_model_v2 import (
    PARTITION_RECORD_SCHEMA,
    STATUS as TEAM_WAVE_STATUS,
)
from o2o_dps.sim_bridge import BackgroundDamageEventV1
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from o2o_dps.upper_kara_resolved_dynamic_v4_adapter_v1 import (
    CompiledResolvedIncantagosDynamicV4CaseV1,
    ResolvedDynamicLoadV4,
    ResolvedDynamicTargetIndexV1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ALL_THREE_FROM_T0_UNTIL_SIM_DEATH,
    CompiledResolvedTrashDynamicV4CaseV1,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    STATUS,
    UpperKaraResponsiveIncantagosCaseV1Error,
    compile_responsive_incantagos_case_v1,
    compile_responsive_trash_case_v1,
)


FOCAL = "0x0000000000FOCAL"


def _player(
    guid: str,
    hero_class: str,
    *,
    trace_indices: list[int],
    partition_key: str,
    observed_spec: str | None,
) -> dict:
    return {
        "player": {
            "guid": guid,
            "class": hero_class,
            "race": "Human",
        },
        "warrior_spec_lane": {
            "partition_key": partition_key,
            "observed_spec": observed_spec,
            "evidence_status": "OBSERVED" if hero_class == "WARRIOR" else "NOT_APPLICABLE_NON_WARRIOR",
        },
        "component_membership": {
            "required_split_unit": "connected component of instance, guild, and player nodes",
            "instance_node_id": "instance-node-1",
            "guild_node_ids": ["guild-node-1"],
            "player_node_id": f"player-node-{guid}",
        },
        "exact_trace_indices": trace_indices,
    }


def _fixed_case() -> CompiledResolvedIncantagosDynamicV4CaseV1:
    config = DynamicTargetSemanticsConfigV4(
        target_health=(
            DynamicTargetHealthV4(0, 1_000, 1_000),
            DynamicTargetHealthV4(1, 100, 100),
        ),
        idle_advance_horizon_ms=150,
        background_damage_events=(
            BackgroundDamageEventV1(0, 10, 0, "fixed-loo", 20),
        ),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, True),
            DynamicAttackabilityEventV2(1, 0, 1, False),
            DynamicAttackabilityEventV2(2, 20, 1, True),
            DynamicAttackabilityEventV2(3, 100, 1, False),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, 1721),
            DynamicEffectiveArmorEventV2(1, 0, 1, 0),
        ),
        retarget_mode="REQUIRE_EXPLICIT",
    )
    registry = (
        ResolvedDynamicTargetIndexV1(0, "boss", "0xF130BOSS", "REGISTRY", 61946),
        ResolvedDynamicTargetIndexV1(1, "add", "0xF130ADD", "REGISTRY", 59989),
    )
    return CompiledResolvedIncantagosDynamicV4CaseV1(
        request={"schema": "fixture"},
        dynamic_config=config,
        dynamic_load=ResolvedDynamicLoadV4(config),
        occurrence_index_registry=registry,
        boss_index=0,
        priority_add_indexes=(1,),
        collateral_only_indexes=(),
        optional_actionable_indexes=(),
        team_only_sidecar=(
            {
                "occurrence_id": "team-only",
                "target_guid": "0xF130TEAMONLY",
                "team_kill_clock_model": {
                    "focal_player_guid": FOCAL,
                    "leave_one_out_positive_damage": 25,
                },
                "schedule_reallocated_to_native_targets": False,
            },
        ),
        observation_provider=lambda state: {},
        target_gate=SimpleNamespace(),
        receipt={
            "source": {
                "instance_id": "instance-1",
                "encounter_id": "encounter-1",
                "pull_ref": "instance-1:encounter-1",
                "compact_reduction": {
                    "instance_id": "instance-1",
                    "encounter_id": "encounter-1",
                    "wave_id": "encounter-1:external-v2-wave:1",
                },
            },
            "activity_windows_inclusive_ms": [
                {
                    "target_index": 0,
                    "occurrence_id": "boss",
                    "windows": [[0, 150]],
                },
                {
                    "target_index": 1,
                    "occurrence_id": "add",
                    "windows": [[20, 100]],
                },
            ],
        },
    )


def _wave() -> dict:
    return {
        "schema": PARTITION_RECORD_SCHEMA,
        "status": TEAM_WAVE_STATUS,
        "wave": {
            "instance_id": "instance-1",
            "encounter_id": "encounter-1",
            "wave_id": "encounter-1:external-v2-wave:1",
            "wave_ordinal": 1,
        },
        "players": [
            _player(
                FOCAL,
                "WARRIOR",
                trace_indices=[0, 2],
                partition_key="WARRIOR_FURY",
                observed_spec="Fury",
            ),
            _player(
                "0x0000000000MAGE",
                "MAGE",
                trace_indices=[1],
                partition_key="MAGE_UNSPECIFIED",
                observed_spec=None,
            ),
            _player(
                "0x0000000000IDLE",
                "PRIEST",
                trace_indices=[],
                partition_key="PRIEST_UNSPECIFIED",
                observed_spec=None,
            ),
        ],
        "scientific_boundaries": {
            "comparison_authorized": False,
            "policy_training_authorized": False,
        },
    }


def _membership() -> dict:
    return {
        "component_id": "component-old50-train",
        "split": "TRAIN",
        "heldout_performance_evidence_eligible": False,
        "comparison_authorized": False,
    }


def test_compiles_observed_roster_and_removes_fixed_loo_schedule() -> None:
    fixed = _fixed_case()
    compiled = compile_responsive_incantagos_case_v1(
        fixed, _wave(), source_membership_evidence=_membership()
    )

    assert compiled.request == fixed.request
    assert compiled.request is not fixed.request
    assert compiled.receipt["status"] == STATUS
    assert compiled.dynamic_config.background_damage_events == ()
    assert compiled.dynamic_config.target_health == fixed.dynamic_config.target_health
    assert compiled.dynamic_config.attackability_events == fixed.dynamic_config.attackability_events
    assert compiled.dynamic_config.effective_armor_events == fixed.dynamic_config.effective_armor_events
    assert compiled.dynamic_config.retarget_mode == fixed.dynamic_config.retarget_mode
    assert compiled.native_target_guids == ("0xF130BOSS", "0xF130ADD")
    assert compiled.target_introduced_at_ms_by_guid == {
        "0xF130BOSS": 0,
        "0xF130ADD": 20,
    }
    assert tuple(row["player_guid"] for row in compiled.actors) == (
        FOCAL,
        "0x0000000000MAGE",
    )
    assert compiled.actors[0]["spec_key"] == "WARRIOR_FURY"
    assert compiled.actors[1]["spec_key"] == "MAGE_SPEC_NOT_AVAILABLE"
    assert compiled.teammate_player_guids == ("0x0000000000MAGE",)
    assert compiled.runtime.snapshot_for_actor(FOCAL)["target_state"][
        "alive_target_guids"
    ] == ["0xF130BOSS"]
    compiled.runtime.advance_to(20)
    assert compiled.runtime.snapshot_for_actor(FOCAL)["target_state"][
        "alive_target_guids"
    ] == ["0xF130ADD", "0xF130BOSS"]
    assert compiled.team_only_sidecar[0]["target_guid"] == "0xF130TEAMONLY"
    assert compiled.receipt["team_only_schedule_reallocated"] is False
    boundaries = compiled.receipt["scientific_boundaries"]
    assert boundaries["comparison_authorized"] is False
    assert boundaries["policy_training_authorized"] is False
    assert boundaries["deployment_authorized"] is False
    assert boundaries["actor_roster_is_outcome_derived"] is True
    assert boundaries["maximum_and_current_health_are_point_hypotheses"] is True
    assert boundaries["target_armor_is_hypothesized_not_observed"] is True
    assert boundaries["attackability_and_introduction_are_descriptive_outcome_proxies"] is True


def test_trash_entry_uses_receipt_focal_and_removes_fixed_loo_schedule() -> None:
    boss_fixture = _fixed_case()
    receipt = dict(boss_fixture.receipt)
    receipt["source"] = {**receipt["source"], "focal_player_guid": FOCAL}
    trash = CompiledResolvedTrashDynamicV4CaseV1(
        request=boss_fixture.request,
        dynamic_config=boss_fixture.dynamic_config,
        dynamic_load=boss_fixture.dynamic_load,
        occurrence_index_registry=boss_fixture.occurrence_index_registry,
        team_only_sidecar=boss_fixture.team_only_sidecar,
        observation_provider=boss_fixture.observation_provider,
        receipt=receipt,
    )
    compiled = compile_responsive_trash_case_v1(
        trash, _wave(), source_membership_evidence=_membership()
    )
    assert compiled.candidate_player_guid == FOCAL
    assert compiled.dynamic_config.background_damage_events == ()
    assert compiled.native_target_guids == ("0xF130BOSS", "0xF130ADD")


@pytest.mark.parametrize(
    ("mode", "source"),
    [
        (ALL_THREE_FROM_T0_UNTIL_SIM_DEATH, "EXPLICIT_ALL_TARGETS_T0_COUNTERFACTUAL_HYPOTHESIS"),
        (OBSERVED_ONSET_UNTIL_SIM_DEATH, "FIRST_OBSERVED_ACTIVITY_ONSET_WITH_COUNTERFACTUAL_PERSISTENCE"),
    ],
)
def test_trash_responsive_receipt_names_counterfactual_attackability(mode: str, source: str) -> None:
    boss_fixture = _fixed_case()
    receipt = dict(boss_fixture.receipt)
    receipt["source"] = {**receipt["source"], "focal_player_guid": FOCAL}
    receipt["attackability_mode"] = mode
    trash = CompiledResolvedTrashDynamicV4CaseV1(
        request=boss_fixture.request,
        dynamic_config=boss_fixture.dynamic_config,
        dynamic_load=boss_fixture.dynamic_load,
        occurrence_index_registry=boss_fixture.occurrence_index_registry,
        team_only_sidecar=boss_fixture.team_only_sidecar,
        observation_provider=boss_fixture.observation_provider,
        receipt=receipt,
    )
    compiled = compile_responsive_trash_case_v1(
        trash, _wave(), source_membership_evidence=_membership()
    )
    assert compiled.receipt["fixed_attackability_mode"] == mode
    assert compiled.receipt["target_introduction_source"] == source
    assert compiled.receipt["scientific_boundaries"]["historical_activity_end_closure_removed"] is True
    assert compiled.receipt["scientific_boundaries"]["attackability_and_introduction_are_descriptive_outcome_proxies"] is False


@pytest.mark.parametrize("field", ["instance_id", "encounter_id", "wave_id"])
def test_rejects_source_identity_mismatch(field: str) -> None:
    wave = _wave()
    wave["wave"][field] = "different"
    with pytest.raises(
        UpperKaraResponsiveIncantagosCaseV1Error,
        match=f"differ at {field}",
    ):
        compile_responsive_incantagos_case_v1(
            _fixed_case(), wave, source_membership_evidence=_membership()
        )


def test_rejects_focal_without_exact_trace_rows() -> None:
    wave = _wave()
    wave["players"][0]["exact_trace_indices"] = []
    with pytest.raises(
        UpperKaraResponsiveIncantagosCaseV1Error,
        match="focal player has no exact Stage-5 trace rows",
    ):
        compile_responsive_incantagos_case_v1(
            _fixed_case(), wave, source_membership_evidence=_membership()
        )


def test_rejects_membership_that_claims_heldout_evidence() -> None:
    membership = _membership()
    membership["heldout_performance_evidence_eligible"] = True
    with pytest.raises(
        UpperKaraResponsiveIncantagosCaseV1Error,
        match="nonheldout and noncomparison",
    ):
        compile_responsive_incantagos_case_v1(
            _fixed_case(), _wave(), source_membership_evidence=membership
        )


def test_future_target_suffix_does_not_change_prefix_team_observation() -> None:
    baseline = compile_responsive_incantagos_case_v1(
        _fixed_case(), _wave(), source_membership_evidence=_membership()
    )

    changed_fixed = _fixed_case()
    changed_config = replace(
        changed_fixed.dynamic_config,
        target_health=(
            changed_fixed.dynamic_config.target_health[0],
            DynamicTargetHealthV4(1, 500, 500),
        ),
    )
    changed_fixed = replace(
        changed_fixed,
        dynamic_config=changed_config,
        dynamic_load=ResolvedDynamicLoadV4(changed_config),
    )
    changed_fixed.receipt["activity_windows_inclusive_ms"][1]["windows"] = [[80, 150]]
    changed = compile_responsive_incantagos_case_v1(
        changed_fixed, _wave(), source_membership_evidence=_membership()
    )

    assert baseline.runtime.snapshot_for_actor(FOCAL) == changed.runtime.snapshot_for_actor(FOCAL)
    assert baseline.runtime.snapshot_for_actor("0x0000000000MAGE") == changed.runtime.snapshot_for_actor("0x0000000000MAGE")
    baseline.runtime.advance_to(10)
    changed.runtime.advance_to(10)
    assert baseline.runtime.snapshot_for_actor(FOCAL) == changed.runtime.snapshot_for_actor(FOCAL)

    baseline.runtime.advance_to(20)
    changed.runtime.advance_to(20)
    assert "0xF130ADD" in baseline.runtime.snapshot_for_actor(FOCAL)["target_state"]["alive_target_guids"]
    assert "0xF130ADD" not in changed.runtime.snapshot_for_actor(FOCAL)["target_state"]["alive_target_guids"]
