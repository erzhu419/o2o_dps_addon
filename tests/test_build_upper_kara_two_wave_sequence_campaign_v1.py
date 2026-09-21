from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7,
    HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND,
    ContinuousTwoWaveRemoteCampaignV1,
    HeterogeneousTwoWaveSequenceSearchV7,
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    campaign_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1
from scripts.build_upper_kara_two_wave_sequence_campaign_v1 import (
    DEFAULT_CAMPAIGN_ID_V1,
    build_upper_kara_two_wave_sequence_campaign_v1,
    write_json_create_only_v1,
)


def test_builder_freezes_exact_seed_panels_shards_and_arrival_nuisance() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()

    assert len(campaign.train_examples) == 256
    assert len(campaign.evaluation_examples) == 256
    assert [row.seed for row in campaign.train_examples] == list(
        range(920_001, 920_257)
    )
    assert [row.seed for row in campaign.evaluation_examples] == list(
        range(1_020_001, 1_020_257)
    )
    expected_arrivals = (0, 1_000, 3_000, 5_000, 7_000, 9_000)
    assert tuple(
        row.first_wave_arrival_ms for row in campaign.train_examples[:12]
    ) == expected_arrivals * 2
    assert set(
        Counter(
            row.first_wave_arrival_ms for row in campaign.train_examples
        ).values()
    ) == {42, 43}
    assert campaign.seed_shard_count == 256
    train_shards = assign_training_shards_v1(campaign)
    evaluation_shards = assign_evaluation_shards_v1(campaign)
    assert len(train_shards) == 4 * 256
    assert len(evaluation_shards) == 256
    assert all(len(row.examples) == 1 for row in train_shards)
    assert all(len(row.examples) == 1 for row in evaluation_shards)
    for loadout_id in BURST_LOADOUT_IDS_V1:
        assert {
            shard.examples[0].seed
            for shard in train_shards
            if shard.loadout_id == loadout_id
        } == set(range(920_001, 920_257))


def test_search_spec_is_non_parent_37_candidate_heterogeneous_roundtrip() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    wire = campaign.to_dict()
    spec = wire["search_spec"]

    assert campaign.campaign_id == DEFAULT_CAMPAIGN_ID_V1
    assert campaign.build_id == BUILD_IDS[0]
    assert tuple(campaign.loadout_ids) == BURST_LOADOUT_IDS_V1
    assert campaign.max_decisions == 1_024
    assert spec == {
        "kind": HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND,
        "long_cooldown_action": {"spell_id": 12_328},
        "resource_id": "warrior.death_wish",
        "wave_one_min_target_hp_pct": 50.0,
        "wave_one_min_estimated_remaining_ms": 4_500,
        "candidate_count": 37,
        "wave_pair": ["multi_two", "single_long"],
        "proposal_guide_ids": list(
            HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7
        ),
        "fresh_seed_contract": {
            "train": {"first": 920_001, "last": 920_256, "count": 256},
            "evaluation": {
                "first": 1_020_001,
                "last": 1_020_256,
                "count": 256,
            },
            "disjoint": True,
            "frozen_before_search": True,
            "environment_arrival_nuisance_schedule_ms": [
                0,
                1_000,
                3_000,
                5_000,
                7_000,
                9_000,
            ],
            "arrival_nuisance_is_policy_input": False,
        },
    }
    assert "parent_loadout_id" not in spec
    assert "parent_program" not in spec
    assert campaign_from_dict_v1(wire) == campaign
    assert campaign_from_dict_v1(wire).to_dict() == wire

    contract = wire["contract"]
    assert contract["development_scope"] == "DEVELOPMENT_ONLY"
    assert contract["actual_heterogeneous_wave_pair"] == [
        "multi_two",
        "single_long",
    ]
    assert contract["wave_specific_action_sequences"] is True
    assert contract["first_wave_arrival_visible_to_policy"] is False
    assert contract["arrival_is_environment_nuisance_only"] is True
    assert contract["fixed_parent_program_is_paired_zero"] is False
    assert contract["four_canonical_burst_loadouts_searched"] is True
    assert contract["training_task_count"] == 1_024
    assert contract["heldout_task_count"] == 256
    assert contract["exact_candidate_count_per_loadout"] == 37
    assert contract["full_upper_kara_route_claim"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda wire: wire["search_spec"].__setitem__(
                "candidate_count", 36
            ),
            "candidate_count",
        ),
        (
            lambda wire: wire["search_spec"].__setitem__(
                "wave_pair", ["single_long", "multi_two"]
            ),
            "wave_pair",
        ),
        (
            lambda wire: wire["search_spec"][
                "fresh_seed_contract"
            ]["train"].__setitem__("last", 920_255),
            "canonical",
        ),
        (
            lambda wire: wire["search_spec"].__setitem__("unexpected", True),
            "fields differ",
        ),
        (
            lambda wire: wire.__setitem__(
                "loadout_ids", [BURST_LOADOUT_IDS_V1[0]]
            ),
            "four canonical",
        ),
        (
            lambda wire: wire["train_examples"][0].__setitem__(
                "seed", 920_999
            ),
            "training seeds differ",
        ),
        (
            lambda wire: wire["train_examples"][0].__setitem__(
                "first_wave_arrival_ms", 9_000
            ),
            "arrival nuisance strata differ",
        ),
    ),
)
def test_v7_parser_rejects_noncanonical_or_leaking_campaigns(
    mutation, message: str
) -> None:
    wire = deepcopy(build_upper_kara_two_wave_sequence_campaign_v1().to_dict())
    mutation(wire)
    with pytest.raises(ValueError, match=message):
        campaign_from_dict_v1(wire)


def test_v7_requires_256_shards_and_conservative_decision_budget() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    with pytest.raises(ValueError, match="one seed per shard"):
        ContinuousTwoWaveRemoteCampaignV1(
            campaign_id="wrong-shards",
            build_id=campaign.build_id,
            train_examples=campaign.train_examples,
            evaluation_examples=campaign.evaluation_examples,
            generation_config=campaign.generation_config,
            loadout_ids=campaign.loadout_ids,
            seed_shard_count=128,
            pull_time_ms=campaign.pull_time_ms,
            max_decisions=campaign.max_decisions,
            search_spec=campaign.search_spec,
        )
    with pytest.raises(ValueError, match="at least 1024"):
        ContinuousTwoWaveRemoteCampaignV1(
            campaign_id="short-budget",
            build_id=campaign.build_id,
            train_examples=campaign.train_examples,
            evaluation_examples=campaign.evaluation_examples,
            generation_config=campaign.generation_config,
            loadout_ids=campaign.loadout_ids,
            seed_shard_count=campaign.seed_shard_count,
            pull_time_ms=campaign.pull_time_ms,
            max_decisions=1_023,
            search_spec=campaign.search_spec,
        )


def test_create_only_writer_never_replaces_existing_campaign() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1()
    with TemporaryDirectory() as raw_root:
        destination = Path(raw_root) / "campaign.json"
        resolved = write_json_create_only_v1(destination, campaign.to_dict())
        assert resolved == destination.resolve()
        first = destination.read_bytes()
        assert campaign_from_dict_v1(
            json.loads(first.decode("utf-8"))
        ) == campaign
        with pytest.raises(FileExistsError):
            write_json_create_only_v1(destination, {"replacement": True})
        assert destination.read_bytes() == first


def test_existing_no_search_spec_roundtrip_is_unchanged() -> None:
    arrivals = (0, 1_000, 3_000, 5_000, 7_000, 9_000)
    campaign = ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="legacy-no-search-spec",
        build_id=BUILD_IDS[0],
        train_examples=tuple(
            TwoWaveExampleV1(10_000 + index, arrivals[index % 6])
            for index in range(12)
        ),
        evaluation_examples=tuple(
            TwoWaveExampleV1(20_000 + index, arrivals[index % 6])
            for index in range(6)
        ),
        generation_config=UpperKaraCausalProgramGenerationConfigV1(),
        seed_shard_count=6,
    )
    wire = campaign.to_dict()
    assert "search_spec" not in wire
    assert campaign_from_dict_v1(wire) == campaign


def test_v7_search_spec_accepts_caller_selected_death_wish_action_only_as_data() -> None:
    campaign = build_upper_kara_two_wave_sequence_campaign_v1(
        long_cooldown_action=ActionRef(spell_id=12_328)
    )
    assert isinstance(campaign.search_spec, HeterogeneousTwoWaveSequenceSearchV7)
    assert campaign.search_spec.long_cooldown_action == ActionRef(
        spell_id=12_328
    )
