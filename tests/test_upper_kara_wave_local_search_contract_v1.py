from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import SearchCellIdentity
from o2o_dps.wave_action_sequence_search_v1 import (
    NativeDynamicV3ScheduleReplayV1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    search_wave_action_sequences_v1,
)

from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    CANDIDATE_AGGREGATION,
    LOCAL_VALUE_OUTPUT,
    OBJECTIVE_SCOPE,
    RESOURCE_PLANNING_SCOPE,
    BaselineRefV1,
    FrozenPlayerIdentityV1,
    ObservableTargetConditionV1,
    ObservedTargetStateV1,
    ResourceAvailabilityCaseV1,
    ResourceRefV1,
    SeedNamespaceV1,
    TargetStageV1,
    UpperKaraWaveLocalSearchContractV1,
    WaveSearchUnitV1,
    WaveTargetBindingV1,
    legal_collateral_target_indexes_v1,
    legal_direct_target_indexes_v1,
    stage_transition_satisfied_v1,
    upper_kara_wave_local_search_contract_from_dict_v1,
    wave_target_binding_from_dict_v1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    REQUIRED_RETARGET_MODE_V1,
    UpperKaraWaveTargetGateV1,
)


def _target(occurrence_id: str, entry: int) -> WaveTargetBindingV1:
    return WaveTargetBindingV1(
        occurrence_id=occurrence_id,
        target_guid=f"guid/{occurrence_id}",
        identity_kind="REGISTRY_CREATURE_OCCURRENCE",
        creature_entry_id=entry,
        hp_model_ref=f"hp/{occurrence_id}/v1",
        armor_model_ref=f"armor/{occurrence_id}/v1",
        team_kill_clock_ref=f"team-clock/{occurrence_id}/v1",
        attackability_ref=f"attackability/{occurrence_id}/v1",
    )


def _baselines() -> tuple[BaselineRefV1, ...]:
    return (
        BaselineRefV1("cat", "CAT", "policy/cat/frozen"),
        BaselineRefV1(
            "contra-deployed", "DEPLOYED_CONTRA", "policy/contra/deployed"
        ),
        BaselineRefV1("contra-new", "CONTRA_NEW", "policy/contra-new/frozen"),
        BaselineRefV1(
            "expert-tonyniu", "OFFLINE_EXPERT", "offline/tonyniu/clean-window"
        ),
    )


def _common(**changes) -> dict:
    fields = {
        "campaign_id": "upper-kara-wave-local-v1-test",
        "frozen_player": FrozenPlayerIdentityV1(
            build_id="fury-build-live-bonereaver",
            talent_ref="talents/fury/17-34-0",
            equipment_ref="equipment/live-bonereaver-set-1",
            loadout_ref="loadout/mighty-rage",
        ),
        "variant_budget": 256,
        "baselines": _baselines(),
        "train_seeds": SeedNamespaceV1("wave-17/train", (101, 102, 103)),
        "selection_seeds": SeedNamespaceV1("wave-17/selection", (201, 202)),
        "heldout_seeds": SeedNamespaceV1("wave-17/heldout", (301, 302)),
        "resources": (
            ResourceRefV1("death-wish", "LONG_COOLDOWN"),
            ResourceRefV1("mighty-rage", "POTION"),
        ),
        "resource_availability_cases": (
            ResourceAvailabilityCaseV1("none-ready", ()),
            ResourceAvailabilityCaseV1("death-wish-ready", ("death-wish",)),
            ResourceAvailabilityCaseV1("mighty-rage-ready", ("mighty-rage",)),
            ResourceAvailabilityCaseV1(
                "both-ready", ("death-wish", "mighty-rage")
            ),
        ),
    }
    fields.update(changes)
    return fields


def _route_contract(*, budget: int = 256) -> UpperKaraWaveLocalSearchContractV1:
    targets = (_target("mob-a", 1001), _target("mob-b", 1001), _target("mob-c", 1002))
    stage = TargetStageV1(
        stage_id="kill-in-order",
        stage_role="PULL",
        direct_target_mode="FIXED_SEQUENCE",
        direct_target_occurrence_ids=("mob-a", "mob-b", "mob-c"),
        collateral_target_occurrence_ids=("mob-a", "mob-b", "mob-c"),
    )
    return UpperKaraWaveLocalSearchContractV1(
        **_common(
            search_unit=WaveSearchUnitV1(
                "route-pull-17", "ROUTE_PULL", "upper-kara/route-v3", "pull-17"
            ),
            targets=targets,
            target_stages=(stage,),
            variant_budget=budget,
        )
    )


def _boss_contract() -> UpperKaraWaveLocalSearchContractV1:
    targets = (
        _target("boss", 2001),
        _target("add-left", 2002),
        _target("add-right", 2002),
    )
    stages = (
        TargetStageV1(
            stage_id="boss-open",
            stage_role="BOSS",
            direct_target_mode="ANY_LEGAL",
            direct_target_occurrence_ids=("boss",),
            collateral_target_occurrence_ids=("boss",),
            transition_match="ALL",
            transition_conditions=(
                ObservableTargetConditionV1("boss", "attackable", False),
                ObservableTargetConditionV1("add-left", "visible", True),
                ObservableTargetConditionV1("add-left", "attackable", True),
            ),
            next_stage_id="adds",
        ),
        TargetStageV1(
            stage_id="adds",
            stage_role="ADDS",
            direct_target_mode="FIXED_SEQUENCE",
            direct_target_occurrence_ids=("add-left", "add-right"),
            collateral_target_occurrence_ids=("add-left", "add-right"),
            transition_match="ALL",
            transition_conditions=(
                ObservableTargetConditionV1("add-left", "dead", True),
                ObservableTargetConditionV1("add-right", "dead", True),
                ObservableTargetConditionV1("boss", "attackable", True),
            ),
            next_stage_id="boss-finish",
        ),
        TargetStageV1(
            stage_id="boss-finish",
            stage_role="BOSS",
            direct_target_mode="ANY_LEGAL",
            direct_target_occurrence_ids=("boss",),
            collateral_target_occurrence_ids=("boss",),
        ),
    )
    return UpperKaraWaveLocalSearchContractV1(
        **_common(
            search_unit=WaveSearchUnitV1(
                "boss-2-add-phase",
                "BOSS_PHASE",
                "upper-kara/route-v3",
                "boss-2/add-transition",
            ),
            targets=targets,
            target_stages=stages,
            variant_budget=1024,
        )
    )


def test_strict_roundtrip_freezes_one_unit_player_models_and_seed_panels() -> None:
    contract = _boss_contract()

    restored = upper_kara_wave_local_search_contract_from_dict_v1(
        contract.to_dict()
    )

    assert restored == contract
    assert restored.search_unit.unit_kind == "BOSS_PHASE"
    assert restored.frozen_player.equipment_ref == "equipment/live-bonereaver-set-1"
    assert restored.variant_budget == 1024
    assert restored.objective_scope == OBJECTIVE_SCOPE
    assert restored.candidate_aggregation == CANDIDATE_AGGREGATION
    assert restored.cross_wave_resource_planning == RESOURCE_PLANNING_SCOPE
    assert restored.local_value_output == LOCAL_VALUE_OUTPUT
    assert {row.creature_entry_id for row in restored.targets} == {2001, 2002}
    assert all(row.target_guid.startswith("guid/") for row in restored.targets)
    assert {
        row.identity_kind for row in restored.targets
    } == {"REGISTRY_CREATURE_OCCURRENCE"}
    assert all(row.hp_model_ref and row.armor_model_ref for row in restored.targets)
    assert all(
        row.team_kill_clock_ref and row.attackability_ref for row in restored.targets
    )
    assert not (
        set(restored.train_seeds.seeds)
        & set(restored.selection_seeds.seeds)
    )
    assert not (
        set(restored.train_seeds.seeds) & set(restored.heldout_seeds.seeds)
    )


def test_variant_budget_is_256_or_1024_and_baselines_are_extra_lanes() -> None:
    small = _route_contract(budget=256)
    large = _route_contract(budget=1024)

    assert small.total_policy_lanes_per_resource_case == 260
    assert large.total_policy_lanes_per_resource_case == 1028
    assert small.variant_budget_excludes_baselines is True

    with pytest.raises(ValueError, match="256 or 1024"):
        _route_contract(budget=512)

    wire = small.to_dict()
    wire["variant_budget_excludes_baselines"] = False
    with pytest.raises(ValueError, match="excluded"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)


def test_target_binding_strictly_serializes_guid_identity_and_optional_template() -> None:
    boss_owned = WaveTargetBindingV1(
        occurrence_id="raid:enc:0xF140000001000001",
        target_guid="0xF140000001000001",
        identity_kind="BOSS_OWNED_SUMMON_OCCURRENCE",
        creature_entry_id=None,
        hp_model_ref="hp/f140",
        armor_model_ref="armor/f140",
        team_kill_clock_ref="clock/f140",
        attackability_ref="attackability/f140",
    )
    assert wave_target_binding_from_dict_v1(boss_owned.to_dict()) == boss_owned

    missing_guid = boss_owned.to_dict()
    missing_guid.pop("target_guid")
    with pytest.raises(ValueError, match="fields differ"):
        wave_target_binding_from_dict_v1(missing_guid)

    with pytest.raises(ValueError, match="require creature_entry_id"):
        WaveTargetBindingV1(
            occurrence_id="registry-mob",
            target_guid="0xF13000007B000001",
            identity_kind="REGISTRY_CREATURE_OCCURRENCE",
            creature_entry_id=None,
            hp_model_ref="hp/registry",
            armor_model_ref="armor/registry",
            team_kill_clock_ref="clock/registry",
            attackability_ref="attackability/registry",
        )


def test_fixed_sequence_cannot_skip_the_commanded_focus_target() -> None:
    contract = _route_contract()
    observations = {
        "mob-a": ObservedTargetStateV1(True, True, False),
        "mob-b": ObservedTargetStateV1(True, True, False),
        "mob-c": ObservedTargetStateV1(True, True, False),
    }

    assert contract.legal_direct_targets("kill-in-order", observations) == ("mob-a",)
    assert contract.legal_direct_target_indexes("kill-in-order", observations) == (0,)
    assert contract.legal_collateral_targets("kill-in-order", observations) == (
        "mob-a",
        "mob-b",
        "mob-c",
    )

    observations["mob-a"] = ObservedTargetStateV1(True, False, True)
    assert contract.legal_direct_targets("kill-in-order", observations) == ("mob-b",)

    observations["mob-b"] = ObservedTargetStateV1(True, False, False)
    assert contract.legal_direct_targets("kill-in-order", observations) == ()


def test_pure_target_index_gates_keep_direct_and_collateral_legality_separate() -> None:
    contract = _route_contract()
    stage = contract.target_stages[0]
    states = (
        ObservedTargetStateV1(True, True, False),
        ObservedTargetStateV1(True, True, False),
        ObservedTargetStateV1(True, False, False),
    )

    assert legal_direct_target_indexes_v1(stage, contract.targets, states) == (0,)
    assert legal_collateral_target_indexes_v1(stage, contract.targets, states) == (
        0,
        1,
    )

    states = (
        ObservedTargetStateV1(True, False, True),
        states[1],
        states[2],
    )
    assert legal_direct_target_indexes_v1(stage, contract.targets, states) == (1,)


def test_boss_add_switch_uses_only_current_observable_state() -> None:
    contract = _boss_contract()
    before_adds = {
        "boss": ObservedTargetStateV1(True, True, False),
        "add-left": ObservedTargetStateV1(False, False, False),
        "add-right": ObservedTargetStateV1(False, False, False),
    }
    assert contract.legal_direct_targets("boss-open", before_adds) == ("boss",)
    assert not contract.transition_satisfied("boss-open", before_adds)

    adds_active = {
        "boss": ObservedTargetStateV1(True, False, False),
        "add-left": ObservedTargetStateV1(True, True, False),
        "add-right": ObservedTargetStateV1(True, True, False),
    }
    assert contract.transition_satisfied("boss-open", adds_active)
    assert stage_transition_satisfied_v1(
        contract.target_stages[0],
        contract.targets,
        tuple(adds_active[row.occurrence_id] for row in contract.targets),
    )
    assert contract.legal_direct_targets("adds", adds_active) == ("add-left",)
    assert contract.legal_collateral_targets("adds", adds_active) == (
        "add-left",
        "add-right",
    )

    first_dead = dict(adds_active)
    first_dead["add-left"] = ObservedTargetStateV1(True, False, True)
    assert contract.legal_direct_targets("adds", first_dead) == ("add-right",)
    assert not contract.transition_satisfied("adds", first_dead)

    adds_dead = {
        "boss": ObservedTargetStateV1(True, True, False),
        "add-left": ObservedTargetStateV1(True, False, True),
        "add-right": ObservedTargetStateV1(True, False, True),
    }
    assert contract.transition_satisfied("adds", adds_dead)
    assert contract.legal_direct_targets("boss-finish", adds_dead) == ("boss",)


def _state_with_observations(
    observations: dict[str, ObservedTargetStateV1],
    *,
    time_ms: int = 0,
) -> dict:
    return {
        "time_ms": time_ms,
        "damage_done": 0.0,
        "needs_input": True,
        "finished": False,
        "wave_observations": observations,
        "dynamic_target_semantics": {
            "targets": [
                {
                    "target_index": index,
                    "dead": observation.dead,
                    "attackable": observation.attackable,
                }
                for index, observation in enumerate(observations.values())
            ]
        },
    }


def _observation_provider(state: dict) -> dict[str, ObservedTargetStateV1]:
    return state["wave_observations"]


def test_runtime_gate_keeps_stage_cursor_and_collateral_separate() -> None:
    contract = _boss_contract()
    gate = UpperKaraWaveTargetGateV1(contract, _observation_provider)
    before_adds = {
        "boss": ObservedTargetStateV1(True, True, False),
        "add-left": ObservedTargetStateV1(False, False, False),
        "add-right": ObservedTargetStateV1(False, False, False),
    }
    open_decision = gate.evaluate_state(_state_with_observations(before_adds))
    assert open_decision.stage_id == "boss-open"
    assert open_decision.direct_target_indexes == (0,)

    adds_active = {
        "boss": ObservedTargetStateV1(True, False, False),
        "add-left": ObservedTargetStateV1(True, True, False),
        "add-right": ObservedTargetStateV1(True, True, False),
    }
    adds_decision = gate.evaluate_state(
        _state_with_observations(adds_active),
        previous_stage_id=open_decision.stage_id,
    )
    assert adds_decision.stage_id == "adds"
    assert adds_decision.direct_target_indexes == (1,)
    assert adds_decision.collateral_target_indexes == (1, 2)

    adds_dead = {
        "boss": ObservedTargetStateV1(True, True, False),
        "add-left": ObservedTargetStateV1(True, False, True),
        "add-right": ObservedTargetStateV1(True, False, True),
    }
    finish_decision = gate.evaluate_state(
        _state_with_observations(adds_dead),
        previous_stage_id=adds_decision.stage_id,
    )
    assert finish_decision.stage_id == "boss-finish"
    assert finish_decision.direct_target_indexes == (0,)


def test_focus_mode_searches_first_add_then_locks_that_target_until_death() -> None:
    targets = (
        _target("boss", 2001),
        _target("add-left", 2002),
        _target("add-right", 2002),
    )
    contract = UpperKaraWaveLocalSearchContractV1(
        **_common(
            search_unit=WaveSearchUnitV1(
                "boss-2-continuous",
                "BOSS_ENCOUNTER",
                "upper-kara/route-v3",
                "boss-2",
            ),
            targets=targets,
            target_stages=(
                TargetStageV1(
                    "boss-open",
                    "BOSS",
                    "ANY_LEGAL",
                    ("boss",),
                    ("boss",),
                    "ANY",
                    (
                        ObservableTargetConditionV1(
                            "add-left", "visible", True
                        ),
                        ObservableTargetConditionV1(
                            "add-right", "visible", True
                        ),
                    ),
                    "adds",
                ),
                TargetStageV1(
                    "adds",
                    "ADDS",
                    "FOCUS_ONE_UNTIL_DEAD",
                    ("add-left", "add-right"),
                    ("add-left", "add-right"),
                ),
            ),
        )
    )
    gate = UpperKaraWaveTargetGateV1(contract, _observation_provider)
    observations = {
        "boss": ObservedTargetStateV1(True, True, False),
        "add-left": ObservedTargetStateV1(True, True, False),
        "add-right": ObservedTargetStateV1(True, True, False),
    }

    stage_entry = _state_with_observations(observations)
    stage_entry["target_index"] = 0
    first_choice = gate.evaluate_state(stage_entry)
    assert first_choice.stage_id == "adds"
    assert first_choice.direct_target_indexes == (1, 2)

    right_selected = dict(stage_entry)
    right_selected["target_index"] = 2
    locked = gate.evaluate_state(
        right_selected, previous_stage_id=first_choice.stage_id
    )
    assert locked.direct_target_indexes == (2,)

    observations["add-right"] = ObservedTargetStateV1(True, False, False)
    temporarily_blocked = gate.evaluate_state(
        right_selected, previous_stage_id=locked.stage_id
    )
    assert temporarily_blocked.direct_target_indexes == ()

    observations["add-right"] = ObservedTargetStateV1(True, False, True)
    after_death = gate.evaluate_state(
        right_selected, previous_stage_id=locked.stage_id
    )
    assert after_death.direct_target_indexes == (1,)


def test_finite_search_uses_direct_gate_without_rewriting_attackability() -> None:
    contract = _route_contract()
    observations = {
        target.occurrence_id: ObservedTargetStateV1(True, True, False)
        for target in contract.targets
    }
    state = _state_with_observations(observations)
    action = ActionRef(spell_id=1001)

    class OneStepReplay:
        def __init__(self) -> None:
            self.seen_targets: list[int | None] = []

        def replay(self, seed, schedule):
            if not schedule:
                return ScheduleReplayOutcomeV1(
                    seed,
                    ReplayStatusV1.FRONTIER,
                    state,
                    available_actions=(
                        AvailableAction(0, action, "hit", True, 0, True),
                    ),
                )
            plan = schedule[-1]
            self.seen_targets.append(plan.target_index)
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.COMPLETE,
                {**state, "time_ms": 1000, "damage_done": 10.0 if plan.gcd_action else 0.0},
            )

    replay = OneStepReplay()
    gate = UpperKaraWaveTargetGateV1(contract, _observation_provider)
    initial = gate.evaluate_state(state)
    assert initial.direct_target_indexes == (0,)
    assert initial.collateral_target_indexes == (0, 1, 2)
    assert all(
        row["attackable"]
        for row in state["dynamic_target_semantics"]["targets"]
    )

    result = search_wave_action_sequences_v1(
        replay,
        SearchCellIdentity("upper-kara", "pull-17", "exact-build"),
        seeds=(7,),
        max_steps=1,
        beam_width=4,
        max_off_gcd_actions=0,
        target_gate=gate,
    )

    assert result.schedule[0].target_index == 0
    assert {target for target in replay.seen_targets if target is not None} == {0}
    assert all(
        row["attackable"]
        for row in state["dynamic_target_semantics"]["targets"]
    )


def test_native_gated_replay_requires_explicit_retarget_case() -> None:
    contract = _route_contract()
    observations = {
        target.occurrence_id: ObservedTargetStateV1(True, True, False)
        for target in contract.targets
    }
    state = _state_with_observations(observations)
    action = ActionRef(spell_id=1001)

    class LoadOnlyBridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def load_dynamic_v3(self, request, seed, config):
            return SimpleNamespace(state=state)

        def actions(self):
            return (AvailableAction(0, action, "hit", True, 0, True),)

    def case(mode: str):
        return SimpleNamespace(
            request={},
            dynamic_load=SimpleNamespace(
                config=SimpleNamespace(retarget_mode=mode)
            ),
        )

    gate = UpperKaraWaveTargetGateV1(contract, _observation_provider)
    rejected = NativeDynamicV3ScheduleReplayV1(
        LoadOnlyBridge,
        lambda seed: case("NEXT_ALIVE_CYCLIC"),
        target_gate=gate,
    ).replay(1, ())
    assert rejected.status is ReplayStatusV1.INVALID
    assert REQUIRED_RETARGET_MODE_V1 in rejected.invalid_reason

    accepted = NativeDynamicV3ScheduleReplayV1(
        LoadOnlyBridge,
        lambda seed: case(REQUIRED_RETARGET_MODE_V1),
        target_gate=gate,
    ).replay(1, ())
    assert accepted.status is ReplayStatusV1.FRONTIER
    assert accepted.target_gate is not None
    assert accepted.target_gate.direct_target_indexes == (0,)


def test_parser_rejects_future_signal_extra_fields_and_cross_wave_objective() -> None:
    wire = _boss_contract().to_dict()
    wire["target_stages"][0]["transition_conditions"][0][
        "observable"
    ] = "future_dead_at_ms"
    with pytest.raises(ValueError, match="observable"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)

    wire = _boss_contract().to_dict()
    wire["future_raid_result"] = "win"
    with pytest.raises(ValueError, match="fields differ"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)

    wire = _boss_contract().to_dict()
    wire["candidate_aggregation"] = "AVERAGE_ACROSS_ROUTE"
    with pytest.raises(ValueError, match="candidate_aggregation"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)


def test_seed_namespaces_and_values_are_strictly_disjoint() -> None:
    fields = _common(
        search_unit=WaveSearchUnitV1("pull", "ROUTE_PULL", "route", "pull-1"),
        targets=(_target("mob", 1),),
        target_stages=(
            TargetStageV1(
                "pull",
                "PULL",
                "ANY_LEGAL",
                ("mob",),
                ("mob",),
            ),
        ),
        selection_seeds=SeedNamespaceV1("wave-17/selection", (103, 201)),
    )
    with pytest.raises(ValueError, match="must not overlap"):
        UpperKaraWaveLocalSearchContractV1(**fields)

    fields["selection_seeds"] = SeedNamespaceV1("wave-17/train", (201, 202))
    with pytest.raises(ValueError, match="namespaces must be distinct"):
        UpperKaraWaveLocalSearchContractV1(**fields)


def test_stage_graph_and_target_bindings_cannot_escape_the_declared_unit() -> None:
    contract = _boss_contract()
    wire = contract.to_dict()
    wire["target_stages"][0]["next_stage_id"] = "boss-finish"
    with pytest.raises(ValueError, match="next declared stage"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)

    wire = contract.to_dict()
    wire["target_stages"][1]["direct_target_occurrence_ids"].append("other-wave")
    with pytest.raises(ValueError, match="unknown target"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)

    wire = contract.to_dict()
    wire["targets"][0]["team_kill_clock_ref"] = ""
    with pytest.raises(ValueError, match="team_kill_clock_ref"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)


def test_fixed_sequence_transition_requires_observed_deaths_for_all_targets() -> None:
    fields = _common(
        search_unit=WaveSearchUnitV1("pull", "ROUTE_PULL", "route", "pull-1"),
        targets=(_target("a", 1), _target("b", 1)),
        target_stages=(
            TargetStageV1(
                "first",
                "PULL",
                "FIXED_SEQUENCE",
                ("a", "b"),
                ("a", "b"),
                "ALL",
                (ObservableTargetConditionV1("a", "dead"),),
                "second",
            ),
            TargetStageV1(
                "second", "PULL", "ANY_LEGAL", ("b",), ("b",)
            ),
        ),
    )
    with pytest.raises(ValueError, match="every direct target"):
        UpperKaraWaveLocalSearchContractV1(**fields)


def test_resource_cases_are_local_values_not_cross_wave_allocations() -> None:
    contract = _route_contract()

    assert contract.cross_wave_resource_planning == "OUTSIDE_LOCAL_SEARCH"
    assert contract.local_value_output == "VALUE_BY_RESOURCE_AVAILABILITY_CASE"
    assert {row.case_id for row in contract.resource_availability_cases} == {
        "none-ready",
        "death-wish-ready",
        "mighty-rage-ready",
        "both-ready",
    }

    wire = deepcopy(contract.to_dict())
    wire["resource_availability_cases"][1]["available_resource_ids"] = [
        "recklessness"
    ]
    with pytest.raises(ValueError, match="unknown resource"):
        upper_kara_wave_local_search_contract_from_dict_v1(wire)
