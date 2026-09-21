from __future__ import annotations

from copy import deepcopy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from o2o_dps.contra_incantagos_v4_policy_binding_v1 import (
    bind_contra_incantagos_v4_policy_v1,
)
from o2o_dps.deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from o2o_dps.deployed_contra_incantagos_v4_session_v1 import (
    DeployedContraIncantagosV4SessionV1,
    DeployedContraIncantagosV4SessionV1Error,
    build_deployed_contra_incantagos_v4_reactive_binding_v1,
)
from o2o_dps.development_wave_panel_v1 import DEFAULT_BINDING
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    compile_responsive_incantagos_case_v1,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    ProgramDecisionV1,
    ProgramPrefixOperationKindV1,
)
from test_policy_observation_causal_projection_v1 import _available_actions, _state
from test_upper_kara_incantagos_v4_cat_binding_v1 import _cases
from test_upper_kara_responsive_incantagos_case_v1 import _membership, _wave


def _setup():
    fixed, _, metadata, classifications = _cases()
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
    binding = bind_contra_incantagos_v4_policy_v1(
        fixed, responsive, metadata,
        source_metadata_artifact_sha256="a" * 64,
        classification_hypotheses_by_occurrence_id=classifications,
        equipment_request=profile,
        equipped_item_names=tuple(f"item-{index}" for index in range(16)),
    )
    raw = _state(
        [(100.0, 100.0, 0.0, False)],
        config_digest=responsive.dynamic_config.content_sha256,
        environment_generation=9,
    )
    raw["time_ms"] = 0
    for block in (raw["dynamic_team_background"], raw["dynamic_target_semantics"]):
        block["schema"] = "o2o_dynamic_target_semantics/v4"
    raw["dynamic_target_semantics"]["targets"][0]["maximum_health"] = 1000.0
    raw["dynamic_target_semantics"]["targets"][1]["maximum_health"] = 100.0
    projector = build_incantagos_v4_prefix_projector_v1(fixed, responsive)
    observation = projector(raw, ())
    actions = tuple(AvailableAction.from_wire(row) for row in _available_actions()) + (
        AvailableAction(8, ActionRef(spell_id=2458), "Berserker Stance", True, 0, False),
    )
    runtime = load_deployed_contra_runtime_binding_v1(DEFAULT_BINDING)
    return binding, runtime, observation, actions


def test_raid_b_session_localizes_only_visible_targets_and_opens_fresh() -> None:
    binding, runtime, observation, actions = _setup()
    reactive = build_deployed_contra_incantagos_v4_reactive_binding_v1(binding, runtime)
    assert reactive.binding_id == "contra.deployed.fury.raid_b"
    assert reactive.open_session() is not reactive.open_session()
    assert observation.policy_to_simulator_target_index == (0,)
    session = reactive.open_session()
    with (
        patch("o2o_dps.deployed_contra_incantagos_v4_session_v1._rollout._combat_state") as combat,
        patch("o2o_dps.deployed_contra_incantagos_v4_session_v1._rollout._proposal") as propose,
        patch("o2o_dps.deployed_contra_incantagos_v4_session_v1._raid_b_identity") as identity,
        patch("o2o_dps.deployed_contra_incantagos_v4_session_v1._sinks._preflight", return_value=((), ())),
        patch("o2o_dps.deployed_contra_incantagos_v4_session_v1._program_decision_from_resolved_v1", return_value=(ProgramDecisionV1(wait_ms=100), None)),
    ):
        combat.return_value.current_stance = object()
        propose.return_value = object()
        assert session(observation, actions).wait_ms == 100
        source_request = combat.call_args.args[2]
        assert len(source_request["encounter"]["targets"]) == 1
        assert source_request["encounter"]["targets"][0]["name"] == "Ley-Watcher Incantagos"
        assert combat.call_args.args[3]["target_name"] == "Ley-Watcher Incantagos"
        assert identity.called


def test_raid_b_session_real_source_reaches_ordered_sink_conversion() -> None:
    binding, runtime, observation, actions = _setup()
    session = DeployedContraIncantagosV4SessionV1(binding, runtime)
    decision = session(observation, actions)
    assert isinstance(decision, ProgramDecisionV1)
    assert decision.start_attack is True
    assert decision.queue_action is not None
    assert decision.queue_action.spell_id == 20569
    assert decision.prefix_order == (
        ProgramPrefixOperationKindV1.START_ATTACK,
        ProgramPrefixOperationKindV1.QUEUE_SET,
    )
    assert decision.gcd_action is not None or decision.wait_ms is not None


def test_raid_b_session_rejects_unrepresentable_reached_sink() -> None:
    binding, runtime, observation, actions = _setup()
    session = DeployedContraIncantagosV4SessionV1(binding, runtime)
    with patch(
        "o2o_dps.deployed_contra_incantagos_v4_session_v1._sinks._preflight",
        return_value=((), ("unbound source target sink",)),
    ):
        with pytest.raises(DeployedContraIncantagosV4SessionV1Error, match="preflight failed"):
            session(observation, actions)


def test_raid_b_session_rejects_raw_noncausal_state() -> None:
    binding, runtime, observation, actions = _setup()
    session = DeployedContraIncantagosV4SessionV1(binding, runtime)
    with pytest.raises(TypeError, match="causal live projection"):
        session(observation.state, actions)
