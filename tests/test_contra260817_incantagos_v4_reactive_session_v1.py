from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from o2o_dps.contra260817_fury_full_policy_rollout_v4 import Contra260817SimulatorInputsV4
from o2o_dps.contra260817_incantagos_v4_reactive_session_v1 import (
    Contra260817IncantagosV4ReactiveSessionV1,
    Contra260817IncantagosV4SessionError,
    build_contra260817_incantagos_v4_imported_binding_v1,
)
from o2o_dps.contra_incantagos_v4_policy_binding_v1 import bind_contra_incantagos_v4_policy_v1
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_paired_multiseed_runner_v4 import CONTRA260817_POLICY_ID
from o2o_dps.sim_bridge import AvailableAction
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import build_incantagos_v4_prefix_projector_v1
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import compile_responsive_incantagos_case_v1
from test_policy_observation_causal_projection_v1 import _state
from test_upper_kara_incantagos_v4_cat_binding_v1 import _cases
from test_upper_kara_responsive_incantagos_case_v1 import _membership, _wave


def _session_inputs():
    fixed, _, metadata, classification = _cases()
    profile = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/wowsims/fury_warrior_clean_dual.json")
        .read_text(encoding="utf-8")
    )
    template = profile["encounter"]["targets"][0]
    profile["encounter"]["targets"] = [deepcopy(template), deepcopy(template)]
    for target, guid in zip(profile["encounter"]["targets"], ("0xF130BOSS", "0xF130ADD")):
        target["name"] = guid
    fixed = replace(fixed, request=profile)
    responsive = compile_responsive_incantagos_case_v1(
        fixed, _wave(), source_membership_evidence=_membership()
    )
    names = tuple(f"item-{index}" for index in range(16))
    binding = bind_contra_incantagos_v4_policy_v1(
        fixed,
        responsive,
        metadata,
        equipment_request=profile,
        equipped_item_names=names,
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256="a" * 64,
    )
    inputs = Contra260817SimulatorInputsV4(
        equipped_mainhand_name=names[-2],
        equipped_offhand_name=names[-1],
    )
    digest = responsive.dynamic_config.content_sha256
    raw = _state([(100.0, 100.0, 0.0, False)], config_digest=digest, environment_generation=9)
    raw["time_ms"] = 0
    for block in (raw["dynamic_team_background"], raw["dynamic_target_semantics"]):
        block["schema"] = "o2o_dynamic_target_semantics/v4"
    for index, target in enumerate(raw["dynamic_target_semantics"]["targets"]):
        target["maximum_health"] = (1000.0, 100.0)[index]
        target["effective_armor"] = 1721.0
    projector = build_incantagos_v4_prefix_projector_v1(fixed, responsive)
    available = tuple(
        AvailableAction(index, action, key, True, 0, key not in {"warrior.berserker_stance", "queue"})
        for index, (key, action) in enumerate((
            ("warrior.berserker_stance", ACTION_KEY_TO_REF["warrior.berserker_stance"]),
            ("warrior.bloodthirst", ACTION_KEY_TO_REF["warrior.bloodthirst"]),
            ("queue", QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]),
        ))
    )
    return binding, inputs, projector(raw, available), available


def test_reactive_binding_opens_fresh_session_and_keeps_identity():
    binding, inputs, _, _ = _session_inputs()
    imported = build_contra260817_incantagos_v4_imported_binding_v1(binding, inputs)
    assert imported.binding_id == CONTRA260817_POLICY_ID
    assert imported.source_policy_id == CONTRA260817_POLICY_ID
    assert imported.open_session() is not imported.open_session()


def test_reactive_session_uses_only_visible_prefix_and_source_order():
    binding, inputs, observation, available = _session_inputs()
    assert observation.policy_to_simulator_target_index == (0,)
    assert len(observation.state["dynamic_target_semantics"]["targets"]) == 1
    session = Contra260817IncantagosV4ReactiveSessionV1(binding, inputs)
    seen = {}
    original_mapper = session._mapper

    def mapper(state, actions, request, target, *, last_gcd_action):
        seen["target_names"] = [row["name"] for row in request["encounter"]["targets"]]
        seen["target_count"] = len(state["dynamic_target_semantics"]["targets"])
        seen["armor_absent"] = (
            "dynamic_effective_armor" not in target
            and "effective_armor" not in state["dynamic_target_semantics"]["targets"][0]
        )
        return original_mapper(state, actions, request, target, last_gcd_action=last_gcd_action)

    session._mapper = mapper
    decision = session(observation, available)
    assert seen == {
        "target_names": ["Ley-Watcher Incantagos"],
        "target_count": 1,
        "armor_absent": True,
    }
    # This compact fixture's real source returns WAIT after queueing, while
    # the frozen d900 bridge has a different executable Bloodthirst prefix.
    assert decision.wait_ms == 100
    assert decision.start_attack is True
    assert decision.queue_action == QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
    assert [kind.value for kind in decision.prefix_order] == [
        "START_ATTACK", "QUEUE_SET"
    ]
    assert session.last_gcd_action == ""
    assert binding.request["encounter"]["targets"][1]["name"] == "Arcane Guardian"


def test_reactive_session_rejects_gear_mismatch():
    binding, inputs, _, _ = _session_inputs()
    with pytest.raises(Contra260817IncantagosV4SessionError, match="weapon-name hypotheses"):
        Contra260817IncantagosV4ReactiveSessionV1(
            binding, replace(inputs, equipped_mainhand_name="not equipped")
        )
