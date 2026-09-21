from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from o2o_dps.causal_action_program_v1 import (
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.development_two_wave_segment_policy_v1 import (
    build_two_wave_segment_runtime_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    TrainingShardV1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    _program_receipt_v1,
    build_default_remote_runtime_v1,
    freeze_training_winner_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v2 import (
    build_default_replay_runtime_v2,
)
from o2o_dps.upper_kara_heterogeneous_two_wave_remote_v7 import (
    HeterogeneousTwoWaveSequenceProgramGeneratorV7,
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from o2o_dps.upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    imported_incumbent_programs_v1,
)
from scripts.build_upper_kara_two_wave_sequence_campaign_v1 import (
    build_upper_kara_two_wave_sequence_campaign_v1,
)
from scripts.upper_kara_causal_program_remote_stage_v1 import (
    REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    _semantic_prerequisite_errors_v1,
    build_upper_kara_causal_program_remote_plan_v2,
)


def _never_open_cat_session():
    def unavailable(*_args, **_kwargs):
        raise AssertionError("identity-only resolver was opened")

    return unavailable


def test_v7_case_binds_build_loadout_precombat_and_prefix_registry() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        920_001,
        build_id=campaign.build_id,
        loadout_id=campaign.loadout_ids[0],
        pull_time_ms=campaign.pull_time_ms,
        first_wave_arrival_ms=1_000,
    )

    assert case.case_spec["build_id"] == campaign.build_id
    assert case.case_spec["burst_loadout"]["loadout_id"] == campaign.loadout_ids[0]
    assert case.case_spec["required_target_indices"] == [0, 1, 2]
    assert case.case_spec["continuous_route"]["single_native_environment"] is True
    assert case.case_spec["continuous_route"]["wave_2_start_ms"] == 18_000
    assert case.case_spec["remote_v7"]["state_carried_across_waves"] is True
    registry = case.case_spec["policy_observation"][
        "target_introduction_registry_control_plane_only"
    ]
    assert registry["targets"] == [
        {"simulator_target_index": 0, "introduced_at_ms": 4_000},
        {"simulator_target_index": 1, "introduced_at_ms": 4_000},
        {"simulator_target_index": 2, "introduced_at_ms": 18_000},
    ]
    assert all(
        set(row) == {"simulator_target_index", "introduced_at_ms"}
        for row in registry["targets"]
    )
    # The policy-facing registry carries neither source identity nor future HP.
    assert "environment_registry" not in registry
    assert "health" not in json.dumps(registry).lower()


def test_v7_precombat_no_visible_target_uses_hp_independent_wait_sentinel() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        920_001,
        build_id=campaign.build_id,
        loadout_id=campaign.loadout_ids[0],
        pull_time_ms=campaign.pull_time_ms,
        first_wave_arrival_ms=1_000,
    )

    def raw(hidden_hp: float) -> dict:
        health = [114_362.0, 120_222.0, hidden_hp]
        return {
            "time_ms": 0,
            "target_index": 2,
            "target_auras": {"future": hidden_hp},
            "precombat": {"active": True},
            "needs_input": True,
            "finished": False,
            "damage_done": 0.0,
            "environment_generation": 1,
            "dynamic_team_background": {
                "targets": [
                    {
                        "target_index": index,
                        "initial_health": hp,
                        "current_health": hp,
                        "dead": False,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                    }
                    for index, hp in enumerate(health)
                ]
            },
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": index,
                        "attackable": False,
                        "effective_armor": 1_721,
                        "maximum_health": hp,
                        "current_health": hp,
                        "dead": False,
                    }
                    for index, hp in enumerate(health)
                ]
            },
        }

    left = build_heterogeneous_two_wave_observation_projector_v1(case)(
        raw(499_465.0)
    )
    right = build_heterogeneous_two_wave_observation_projector_v1(case)(
        raw(9_999_999.0)
    )
    assert left.state == right.state
    assert left.policy_to_simulator_target_index == ()
    assert left.state["num_targets"] == 0
    assert left.state["target_health_known"] is False
    assert left.state["dynamic_target_semantics"]["targets"][0][
        "attackable"
    ] is False
    assert "future" not in json.dumps(left.state)


def test_v7_generator_is_fixed_37_arm_family_independent_of_arrival() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    generator = HeterogeneousTwoWaveSequenceProgramGeneratorV7(
        build_id=campaign.build_id,
        search_spec=campaign.search_spec,
    )
    cases = tuple(
        build_upper_kara_heterogeneous_two_wave_burst_case_v7(
            example.seed,
            build_id=campaign.build_id,
            loadout_id=campaign.loadout_ids[0],
            pull_time_ms=campaign.pull_time_ms,
            first_wave_arrival_ms=example.first_wave_arrival_ms,
        )
        for example in campaign.train_examples[:2]
    )
    first = generator(
        loadout_id=campaign.loadout_ids[0],
        train_examples=campaign.train_examples[:2],
        train_cases=cases,
    )
    reversed_result = generator(
        loadout_id=campaign.loadout_ids[0],
        train_examples=tuple(reversed(campaign.train_examples[:2])),
        train_cases=tuple(reversed(cases)),
    )

    assert len(first) == 37
    assert first[0].origin is ProgramOriginV1.SEARCHED
    assert all(
        row.origin is ProgramOriginV1.SEARCHED_REACTIVE for row in first[1:]
    )
    assert [row.program_key() for row in first] == [
        row.program_key() for row in reversed_result
    ]
    assert len(generator.policies_by_program_id) == 36
    assert generator.results_by_loadout[campaign.loadout_ids[0]].guide_ids == (
        campaign.search_spec.proposal_guide_ids
    )


def test_v7_wire_programs_rebuild_from_frozen_policy_registry_exactly() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    generator = HeterogeneousTwoWaveSequenceProgramGeneratorV7(
        build_id=campaign.build_id,
        search_spec=campaign.search_spec,
    )
    case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        920_001,
        build_id=campaign.build_id,
        loadout_id=campaign.loadout_ids[0],
        pull_time_ms=campaign.pull_time_ms,
    )
    programs = generator(
        loadout_id=campaign.loadout_ids[0],
        train_examples=(campaign.train_examples[0],),
        train_cases=(case,),
    )
    for source in programs[1:]:
        wire = causal_action_program_from_dict_v1(source.to_dict())
        policy = generator.policies_by_program_id[wire.program_id]
        rebuilt, _ = build_two_wave_segment_runtime_v1(
            policy,
            cat_resolver_factory=_never_open_cat_session,
        )
        assert rebuilt.program_key() == wire.program_key()


def test_default_candidate_and_compact_replay_runtimes_select_v7_path() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    candidate = build_default_remote_runtime_v1(
        campaign,
        bridge_path="bridge",
        bridge_cwd=".",
        runtime_binding_path="binding.json",
        offline_guide_artifact_path="guide.json",
    )
    compact = build_default_replay_runtime_v2(
        campaign,
        bridge_path="bridge",
        bridge_cwd=".",
        runtime_binding_path="binding.json",
    )
    assert (
        candidate.case_builder
        is build_upper_kara_heterogeneous_two_wave_burst_case_v7
    )
    assert (
        compact.case_builder
        is build_upper_kara_heterogeneous_two_wave_burst_case_v7
    )
    assert isinstance(
        candidate.searched_program_generator,
        HeterogeneousTwoWaveSequenceProgramGeneratorV7,
    )


def test_freeze_accepts_positive_searched_reactive_winner(monkeypatch) -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    generator = HeterogeneousTwoWaveSequenceProgramGeneratorV7(
        build_id=campaign.build_id,
        search_spec=campaign.search_spec,
    )
    policy = next(iter(generator.policies_by_program_id.values()))
    reactive, _ = build_two_wave_segment_runtime_v1(
        policy,
        cat_resolver_factory=_never_open_cat_session,
    )

    shards = tuple(
        TrainingShardV1(loadout_id, 0, campaign.train_examples)
        for loadout_id in campaign.loadout_ids
    )
    monkeypatch.setattr(
        "o2o_dps.upper_kara_causal_program_remote_worker_v1.assign_training_shards_v1",
        lambda requested: shards if requested == campaign else (),
    )

    def load_shard(requested, shard):
        assert requested == campaign
        from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
            cat_zero_residual_program_v1,
        )

        zero = cat_zero_residual_program_v1(shard.loadout_id)
        receipts = tuple(_program_receipt_v1(row) for row in (zero, reactive))
        lanes = []
        for program, damage in ((zero, 1_000.0), (reactive, 1_100.0)):
            lanes.extend(
                {
                    "program_ref": program.program_id,
                    "master_seed": example.seed,
                    "simulator_seed": example.seed + 10_000_000,
                    "status": TERMINAL_COMPLETE,
                    "own_effective_damage": damage,
                    "own_effective_dps": damage / 10.0,
                }
                for example in shard.examples
            )
        return receipts, tuple(lanes)

    with TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        campaign_path = root / "campaign.json"
        campaign_path.write_text(
            json.dumps(campaign.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        frozen = freeze_training_winner_v1(
            campaign_path=campaign_path,
            training_root=root,
            output_path=root / "frozen.json",
            _training_shard_loader=load_shard,
        )

    assert frozen["terminal_status"] == TERMINAL_COMPLETE
    assert frozen["winner"]["program_origin"] == (
        ProgramOriginV1.SEARCHED_REACTIVE.value
    )
    assert frozen["frozen_program"]["origin"] == (
        ProgramOriginV1.SEARCHED_REACTIVE.value
    )


def test_v7_submit_plan_uses_exact_count_and_requires_small_capsule() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    with TemporaryDirectory() as raw_root:
        campaign_path = Path(raw_root) / "campaign.json"
        campaign_path.write_text(
            json.dumps(campaign.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        plan = build_upper_kara_causal_program_remote_plan_v2(
            shared_home="/home/tester",
            run_id="heterogeneous-v7-plan",
            phase="candidate",
            campaign=campaign_path,
            bridge_name="bridge-v23.linux-amd64",
        )

    audit = plan["candidate_count_audit"]
    assert plan["task_count"] == 4
    assert audit["searched_heterogeneous_sequence_count"] == 37
    assert audit["total_training_program_upper_bound"] == 40
    assert audit["exact_unique_count_known_at_plan_time"] is True
    assert audit["projection_may_deduplicate"] is False
    assert plan["derived_scenario_capsule_required"] is True
    assert any(
        row["path"].endswith(str(REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH))
        for row in plan["required_remote_paths"]
    )


def test_submit_semantic_gate_accepts_frozen_reactive_identity() -> None:
    winner = {
        "loadout_id": "loadout",
        "program_ref": "reactive",
        "program_id": "reactive",
        "program_key": "key",
        "program_origin": "SEARCHED_REACTIVE",
    }
    plan = {
        "campaign_id": "campaign",
        "build_id": "build",
        "campaign_contract": {"contract": True},
    }
    frozen_spec = {
        "role": "FROZEN_SEARCHED_WINNER",
        "schema": "freeze",
        "allowed_terminal_statuses": ["COMPLETE"],
        "expected_fields": {},
    }
    frozen = {
        "schema": "freeze",
        "terminal_status": "COMPLETE",
        "campaign_id": "campaign",
        "build_id": "build",
        "campaign_contract": {"contract": True},
        "winner": winner,
    }
    assert _semantic_prerequisite_errors_v1(
        plan, frozen_spec, frozen, frozen_projection=None
    ) == []

    evaluation_spec = {
        "role": "EVAL_SHARD",
        "schema": "eval",
        "allowed_terminal_statuses": ["COMPLETE"],
        "expected_fields": {},
    }
    evaluation = {
        "schema": "eval",
        "terminal_status": "COMPLETE",
        "campaign_id": "campaign",
        "build_id": "build",
        "campaign_contract": {"contract": True},
        "selected_loadout_id": "loadout",
        "frozen_program_id": "reactive",
        "frozen_program_key": "key",
        "frozen_program_origin": "SEARCHED_REACTIVE",
    }
    assert _semantic_prerequisite_errors_v1(
        plan,
        evaluation_spec,
        evaluation,
        frozen_projection=frozen,
    ) == []
