from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8,
    CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8,
    CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8,
    CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND,
    CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8,
    CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8,
    CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8,
    CatActionPlanResidualSequenceSearchV8,
    ContinuousTwoWaveRemoteCampaignV1,
    assign_teacher_plan_shards_v8,
    campaign_from_dict_v1,
    split_training_examples_for_v8_teacher_v8,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


def _v8_campaign() -> ContinuousTwoWaveRemoteCampaignV1:
    fresh = CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8
    schedule = fresh.environment_arrival_nuisance_schedule_ms
    train = tuple(
        TwoWaveExampleV1(seed, schedule[index % len(schedule)])
        for index, seed in enumerate(fresh.train_seeds)
    )
    evaluation = tuple(
        TwoWaveExampleV1(seed, schedule[index % len(schedule)])
        for index, seed in enumerate(fresh.evaluation_seeds)
    )
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="cat-action-plan-residual-v8-test",
        build_id=BUILD_IDS[0],
        train_examples=train,
        evaluation_examples=evaluation,
        generation_config=UpperKaraCausalProgramGenerationConfigV1(),
        loadout_ids=BURST_LOADOUT_IDS_V1,
        seed_shard_count=fresh.seed_count,
        pull_time_ms=3_000,
        max_decisions=1_024,
        search_spec=CatActionPlanResidualSequenceSearchV8(),
    )


def test_v8_search_spec_and_campaign_roundtrip_are_canonical() -> None:
    campaign = _v8_campaign()
    wire = campaign.to_dict()

    assert wire["search_spec"] == {
        "kind": CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND,
        "teacher_max_states": CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8,
        "teacher_plan_shard_size": (
            CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8
        ),
        "max_distilled_candidates_per_loadout": (
            CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8
        ),
        "fresh_seed_contract": CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8.to_dict(),
        "wave_pair": list(CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8),
    }
    parsed = campaign_from_dict_v1(wire)
    assert parsed == campaign
    assert parsed.to_dict() == wire
    contract = wire["contract"]
    assert contract["teacher_task_count"] == 512
    assert contract["teacher_uses_proposal_cohort_only"] is True
    assert contract["selection_validation_examples_excluded_from_teacher"] is True
    assert contract["heldout_evaluation_examples_excluded_from_teacher"] is True
    assert contract["teacher_max_states_per_proposal_seed"] == 3
    assert contract["teacher_plan_shard_size_per_state"] == 64
    assert contract["max_distilled_candidates_per_loadout"] == 64
    assert contract["distilled_candidate_cap_includes_exact_cat_zero"] is True


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("teacher_max_states", 4, "teacher_max_states"),
        ("teacher_plan_shard_size", 32, "teacher_plan_shard_size"),
        (
            "max_distilled_candidates_per_loadout",
            65,
            "max_distilled_candidates_per_loadout",
        ),
        ("wave_pair", ["single_long", "multi_two"], "wave_pair"),
    ),
)
def test_v8_parser_rejects_nonfixed_search_fields(
    field: str, replacement: object, message: str
) -> None:
    wire = deepcopy(_v8_campaign().to_dict())
    wire["search_spec"][field] = replacement
    with pytest.raises(ValueError, match=message):
        campaign_from_dict_v1(wire)


def test_v8_requires_its_explicit_fresh_seed_ranges() -> None:
    wire = deepcopy(_v8_campaign().to_dict())
    wire["search_spec"]["fresh_seed_contract"]["train"]["first"] = 920_001
    wire["search_spec"]["fresh_seed_contract"]["train"]["last"] = 920_256
    with pytest.raises(ValueError, match="predeclared v8 seed panel"):
        campaign_from_dict_v1(wire)

    wire = deepcopy(_v8_campaign().to_dict())
    wire["train_examples"][0]["seed"] = 920_001
    with pytest.raises(ValueError, match="v8 training seeds differ"):
        campaign_from_dict_v1(wire)


def test_v8_balanced_split_and_teacher_shards_cover_only_proposal() -> None:
    campaign = _v8_campaign()
    proposal, selection = split_training_examples_for_v8_teacher_v8(campaign)
    shards = assign_teacher_plan_shards_v8(campaign)

    assert len(proposal) == len(selection) == 128
    assert {
        row.first_wave_arrival_ms for row in proposal
    } == set(CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8)
    assert {
        row.first_wave_arrival_ms for row in selection
    } == set(CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8)
    assert len(shards) == 4 * 128
    assert len({row.work_id for row in shards}) == len(shards)
    assert shards == assign_teacher_plan_shards_v8(campaign)

    proposal_seeds = tuple(row.seed for row in proposal)
    excluded = {
        row.seed for row in (*selection, *campaign.evaluation_examples)
    }
    for loadout_id in BURST_LOADOUT_IDS_V1:
        loadout_shards = tuple(
            row for row in shards if row.loadout_id == loadout_id
        )
        assert tuple(row.example.seed for row in loadout_shards) == proposal_seeds
        assert not {row.example.seed for row in loadout_shards} & excluded
        assert all(row.teacher_max_states == 3 for row in loadout_shards)
        assert all(row.teacher_plan_shard_size == 64 for row in loadout_shards)
