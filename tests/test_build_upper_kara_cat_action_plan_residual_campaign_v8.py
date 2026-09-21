from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8,
    CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8,
    CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8,
    CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8,
    CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8,
    CatActionPlanResidualSequenceSearchV8,
    assign_teacher_plan_shards_v8,
    campaign_from_dict_v1,
    split_training_examples_for_v8_teacher_v8,
)
from scripts.build_upper_kara_cat_action_plan_residual_campaign_v8 import (
    DEFAULT_CAMPAIGN_ID_V8,
    build_upper_kara_cat_action_plan_residual_campaign_v8,
)


def test_v8_builder_freezes_new_seed_panels_and_arrival_schedule() -> None:
    campaign = build_upper_kara_cat_action_plan_residual_campaign_v8()

    assert campaign.campaign_id == DEFAULT_CAMPAIGN_ID_V8
    assert campaign.build_id == BUILD_IDS[0]
    assert campaign.loadout_ids == BURST_LOADOUT_IDS_V1
    assert campaign.seed_shard_count == CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8
    assert campaign.max_decisions == (
        CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8
    )
    assert tuple(row.seed for row in campaign.train_examples) == tuple(
        range(
            CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8,
            CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8
            + CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8,
        )
    )
    assert tuple(row.seed for row in campaign.evaluation_examples) == tuple(
        range(
            CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8,
            CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8
            + CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8,
        )
    )
    assert tuple(
        row.first_wave_arrival_ms for row in campaign.train_examples[:12]
    ) == CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8 * 2
    assert set(
        Counter(
            row.first_wave_arrival_ms for row in campaign.train_examples
        ).values()
    ) == {42, 43}


def test_v8_builder_has_balanced_teacher_split_and_512_assignments() -> None:
    campaign = build_upper_kara_cat_action_plan_residual_campaign_v8()
    proposal, selection = split_training_examples_for_v8_teacher_v8(campaign)
    shards = assign_teacher_plan_shards_v8(campaign)

    assert isinstance(campaign.search_spec, CatActionPlanResidualSequenceSearchV8)
    assert len(proposal) == len(selection) == 128
    assert len(shards) == len(BURST_LOADOUT_IDS_V1) * len(proposal) == 512
    assert len({shard.work_id for shard in shards}) == 512
    assert not {row.seed for row in proposal} & {
        row.seed for row in (*selection, *campaign.evaluation_examples)
    }
    assert {row.first_wave_arrival_ms for row in proposal} == set(
        CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8
    )
    assert {row.first_wave_arrival_ms for row in selection} == set(
        CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8
    )


def test_v8_builder_roundtrip_is_canonical_and_rejects_old_seed_panel() -> None:
    campaign = build_upper_kara_cat_action_plan_residual_campaign_v8()
    wire = campaign.to_dict()

    parsed = campaign_from_dict_v1(wire)
    assert parsed == campaign
    assert parsed.to_dict() == wire
    assert wire["contract"]["teacher_task_count"] == 512

    stale = deepcopy(wire)
    stale["train_examples"][0]["seed"] = 920_001
    with pytest.raises(ValueError, match="v8 training seeds differ"):
        campaign_from_dict_v1(stale)
