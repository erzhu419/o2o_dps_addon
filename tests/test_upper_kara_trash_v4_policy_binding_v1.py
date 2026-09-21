from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.contra_incantagos_v4_policy_binding_v1 import (
    bind_contra_incantagos_v4_policy_v1,
)
from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    IncantagosV4CatBindingV1Error,
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_resolved_dynamic_v4_adapter_v1 import (
    ResolvedDynamicLoadV4,
    ResolvedDynamicTargetIndexV1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    CompiledResolvedTrashDynamicV4CaseV1,
)


def _cases():
    focal = "0x0000000000576754"
    instance = "d900a97b-b53e-4444-943b-3e0f2be8d477"
    guids = ("guid-doomguard-0", "guid-doomguard-1", "guid-doomguard-2")
    entries = (62016, 62020, 62021)
    config = DynamicTargetSemanticsConfigV4(
        target_health=tuple(
            DynamicTargetHealthV4(index, 100.0 + index, 100.0 + index)
            for index in range(3)
        ),
        idle_advance_horizon_ms=15_531,
        retarget_mode="REQUIRE_EXPLICIT",
    )
    request = {
        "raid": {"parties": [{"players": [{
            "name": "historical Fury", "talentsString": "test-fury-talents",
            "equipment": {"items": [{"id": 18832}, {"id": 19866}]},
        }]}]},
        "encounter": {"targets": [{"name": guid} for guid in guids]},
    }
    source = {
        "instance_id": instance,
        "encounter_id": "02829cd0-85c3-4b6f-adba-059398e6ae14",
        "focal_player_guid": focal,
    }
    fixed = CompiledResolvedTrashDynamicV4CaseV1(
        request=request,
        dynamic_config=config,
        dynamic_load=ResolvedDynamicLoadV4(config),
        occurrence_index_registry=tuple(
            ResolvedDynamicTargetIndexV1(
                index, f"doomguard-{index}", guid,
                "REGISTRY_CREATURE_OCCURRENCE", entries[index]
            )
            for index, guid in enumerate(guids)
        ),
        team_only_sidecar=(),
        observation_provider=None,
        receipt={"source": source, "route_priority_resolved": False},
    )
    responsive = CompiledResponsiveIncantagosCaseV1(
        request=request,
        dynamic_config=config,
        runtime=None,
        native_target_guids=guids,
        target_introduced_at_ms_by_guid={guid: 0 for guid in guids},
        actors=(),
        candidate_player_guid=focal,
        teammate_player_guids=(),
        team_only_sidecar=(),
        receipt={"source": source},
    )
    metadata = {
        "id": instance,
        "units": {
            guid: {"name": f"Doomguard {index}", "entry": entries[index]}
            for index, guid in enumerate(guids)
        },
    }
    historical = {
        "schema": "exact_historical_fury_wowsims_request/v1",
        "status": "DEVELOPMENT_CHARACTER_BUILD_ONLY",
        "comparison_authorized": False,
        "source_identity": {
            "instance_id": instance,
            "player_guid": focal,
            "build_segment_id": "segment-0042",
        },
        "request": deepcopy(request),
        "equipped_item_names": ["historical mainhand", "historical offhand"],
        "equipped_item_name_source": "wowsims-turtle/assets/database/db.json",
    }
    return fixed, responsive, metadata, historical


def _kwargs(responsive, historical):
    return {
        "source_metadata_artifact_sha256": "a" * 64,
        "equipment_request": responsive.request,
        "equipped_item_names": tuple(historical["equipped_item_names"]),
        "classification_hypotheses_by_occurrence_id": {
            f"doomguard-{index}": "elite" for index in range(3)
        },
        "equipment_request_provenance": historical,
    }


def test_cat_and_contra_bind_trash_exact_build_without_boss_or_comparison_claims() -> None:
    fixed, responsive, metadata, historical = _cases()
    kwargs = _kwargs(responsive, historical)
    cat = build_incantagos_v4_cat_binding_v1(fixed, responsive, metadata, **kwargs)
    contra = bind_contra_incantagos_v4_policy_v1(fixed, responsive, metadata, **kwargs)
    assert cat.receipt["schema"] == "upper_kara_trash_v4_cat_source_binding/v1"
    assert cat.receipt["equipment_request_provenance"]["observed_equipment_id_match"] is True
    assert cat.receipt["equipment_request_provenance"]["observed_talents_string_match"] is True
    assert cat.receipt["comparison_authorized"] is False
    assert cat.target_contexts[0].context_id.startswith("upper-kara-trash-v4-development-")
    assert build_incantagos_v4_prefix_projector_v1(fixed, responsive).introductions.targets
    assert contra.receipt["schema"] == "contra_upper_kara_trash_v4_policy_binding/v1"
    assert contra.receipt["simulator_build_is_synthetic"] is False
    assert contra.receipt["observed_equipment_id_match"] is True
    assert contra.receipt["simulator_effect_equivalence_verified"] is False
    assert contra.receipt["same_gear_as_historical_focal_verified"] is False
    assert contra.receipt["comparison_authorized"] is False
    assert [row.target_index for row in contra.exact_guid_target_bindings()] == [0, 1, 2]
    assert not hasattr(fixed, "boss_index")


def test_historical_artifact_must_match_focal_guid_and_request_build() -> None:
    fixed, responsive, metadata, historical = _cases()
    wrong_focal = deepcopy(historical)
    wrong_focal["source_identity"]["player_guid"] = "another-player"
    with pytest.raises(IncantagosV4CatBindingV1Error, match="focal case"):
        build_incantagos_v4_cat_binding_v1(
            fixed, responsive, metadata,
            **(_kwargs(responsive, historical) | {"equipment_request_provenance": wrong_focal}),
        )
    wrong_equipment = deepcopy(historical)
    wrong_equipment["request"]["raid"]["parties"][0]["players"][0]["equipment"]["items"][0]["id"] = 1
    with pytest.raises(IncantagosV4CatBindingV1Error, match="equipment or talents"):
        build_incantagos_v4_cat_binding_v1(
            fixed, responsive, metadata,
            **(_kwargs(responsive, historical) | {"equipment_request_provenance": wrong_equipment}),
        )
