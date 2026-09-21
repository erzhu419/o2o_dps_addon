from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    IncantagosV4CatBindingV1Error,
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    compile_responsive_incantagos_case_v1,
)
from test_policy_observation_causal_projection_v1 import _state
from test_upper_kara_responsive_incantagos_case_v1 import _fixed_case, _membership, _wave


def _cases():
    fixed = _fixed_case()
    request = {
        "raid": {
            "parties": [{"players": [{"equipment": {"items": [{"id": 18832}, {"id": 19866}]}}]}]
        },
        "encounter": {
            "targets": [
                {"name": "0xF130BOSS", "level": 63},
                {"name": "0xF130ADD", "level": 63},
            ]
        },
    }
    fixed = replace(fixed, request=request)
    responsive = compile_responsive_incantagos_case_v1(
        fixed, _wave(), source_membership_evidence=_membership()
    )
    metadata = {
        "id": "instance-1",
        "units": {
            "0xF130BOSS": {"name": "Ley-Watcher Incantagos", "entry": 61946},
            "0xF130ADD": {"name": "Arcane Guardian", "entry": 59989},
        },
    }
    classification = {"boss": "worldboss", "add": "elite"}
    return fixed, responsive, metadata, classification


def test_cat_v4_binds_metadata_names_and_explicit_hypotheses() -> None:
    fixed, responsive, metadata, classification = _cases()
    binding = build_incantagos_v4_cat_binding_v1(
        fixed,
        responsive,
        metadata,
        equipment_request=responsive.request,
        equipped_item_names=("Brutality Blade", "Warblade of the Hakkari"),
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256="a" * 64,
    )
    assert binding.request["encounter"]["targets"][0]["name"] == "Ley-Watcher Incantagos"
    assert responsive.request["encounter"]["targets"][0]["name"] == "0xF130BOSS"
    assert binding.target_contexts[0].target_max_health == 1000
    assert binding.target_contexts[1].target_classification.value == "elite"
    assert binding.receipt["comparison_authorized"] is False
    assert binding.open_session() is not binding.open_session()


def test_cat_v4_accepts_metadata_entry_when_compact_registry_entry_unknown() -> None:
    fixed, responsive, metadata, classification = _cases()
    fixed = replace(
        fixed,
        occurrence_index_registry=(
            replace(fixed.occurrence_index_registry[0], creature_entry_id=None),
            fixed.occurrence_index_registry[1],
        ),
    )
    binding = build_incantagos_v4_cat_binding_v1(
        fixed,
        responsive,
        metadata,
        equipment_request=responsive.request,
        equipped_item_names=("Brutality Blade", "Warblade of the Hakkari"),
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256="a" * 64,
    )
    assert binding.target_contexts[0].target_name == "Ley-Watcher Incantagos"


def test_cat_v4_rejects_unbound_equipment_or_classification() -> None:
    fixed, responsive, metadata, classification = _cases()
    wrong_request = deepcopy(responsive.request)
    wrong_request["raid"]["parties"][0]["players"][0]["equipment"]["items"][0]["id"] = 1
    with pytest.raises(IncantagosV4CatBindingV1Error, match="equipment IDs differ"):
        build_incantagos_v4_cat_binding_v1(
            fixed,
            responsive,
            metadata,
            equipment_request=wrong_request,
            equipped_item_names=("Brutality Blade", "Warblade of the Hakkari"),
            classification_hypotheses_by_occurrence_id=classification,
            source_metadata_artifact_sha256="a" * 64,
        )
    with pytest.raises(IncantagosV4CatBindingV1Error, match="cover native targets"):
        build_incantagos_v4_cat_binding_v1(
            fixed,
            responsive,
            metadata,
            equipment_request=responsive.request,
            equipped_item_names=("Brutality Blade", "Warblade of the Hakkari"),
            classification_hypotheses_by_occurrence_id={"boss": "worldboss"},
            source_metadata_artifact_sha256="a" * 64,
        )


def test_v4_prefix_projector_does_not_reveal_future_add_hp() -> None:
    fixed, responsive, _, _ = _cases()
    projector = build_incantagos_v4_prefix_projector_v1(fixed, responsive)
    digest = responsive.dynamic_config.content_sha256
    state = _state(
        [(100.0, 100.0, 0.0, False)],
        config_digest=digest,
        environment_generation=7,
    )
    state["time_ms"] = 0
    for block in (state["dynamic_team_background"], state["dynamic_target_semantics"]):
        block["schema"] = "o2o_dynamic_target_semantics/v4"
    for index, target in enumerate(state["dynamic_target_semantics"]["targets"]):
        target["maximum_health"] = (1000.0, 100.0)[index]
    first = projector(state, ())
    assert first.policy_to_simulator_target_index == (0,)
    assert len(first.state["dynamic_target_semantics"]["targets"]) == 1
    state["time_ms"] = 20
    state["dynamic_target_semantics"]["targets"][1]["attackable"] = True
    second = projector(state, ())
    assert second.policy_to_simulator_target_index == (0, 1)
    assert second.state["dynamic_target_semantics"]["targets"][1]["maximum_health"] == 100.0


def test_cat_source_session_accepts_v4_causal_prefix_without_v3_case() -> None:
    fixed, _, metadata, classification = _cases()
    profile = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/wowsims/fury_warrior_clean_dual.json")
        .read_text(encoding="utf-8")
    )
    template = profile["encounter"]["targets"][0]
    profile["encounter"]["targets"] = [deepcopy(template), deepcopy(template)]
    for target, guid in zip(
        profile["encounter"]["targets"], ("0xF130BOSS", "0xF130ADD")
    ):
        target["name"] = guid
    fixed = replace(fixed, request=profile)
    responsive = compile_responsive_incantagos_case_v1(
        fixed, _wave(), source_membership_evidence=_membership()
    )
    binding = build_incantagos_v4_cat_binding_v1(
        fixed,
        responsive,
        metadata,
        equipment_request=profile,
        equipped_item_names=tuple(f"item-{index}" for index in range(16)),
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256="a" * 64,
    )
    digest = responsive.dynamic_config.content_sha256
    raw = _state([(100.0, 100.0, 0.0, False)], config_digest=digest, environment_generation=9)
    raw["time_ms"] = 0
    for block in (raw["dynamic_team_background"], raw["dynamic_target_semantics"]):
        block["schema"] = "o2o_dynamic_target_semantics/v4"
    for index, target in enumerate(raw["dynamic_target_semantics"]["targets"]):
        target["maximum_health"] = (1000.0, 100.0)[index]
    observation = build_incantagos_v4_prefix_projector_v1(fixed, responsive)(raw, ())
    actions = (
        AvailableAction(0, ActionRef(spell_id=25286, tag=1), "Heroic Strike", True, 0, False),
        AvailableAction(1, ActionRef(spell_id=7373), "Hamstring", True, 0, True),
    )
    decision = binding.open_session()(observation, actions)
    assert decision is not None
