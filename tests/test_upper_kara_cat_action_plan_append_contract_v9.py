from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceWaveV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from o2o_dps.upper_kara_cat_action_plan_append_contract_v9 import (
    FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS,
    FRESH_EVALUATION_SEED_START,
    FRESH_TRAIN_SEED_START,
    MANIFEST_SCHEMA,
    MAX_APPEND_CANDIDATES,
    SEED_SHARD_COUNT,
    TEACHER_MAX_STATES,
    TEACHER_PLAN_SHARD_SIZE,
    CatActionPlanAppendContractV9,
    build_append_candidate_manifest_v9,
    build_cat_action_plan_append_contract_v9,
    cat_action_plan_append_contract_from_dict_v9,
    freeze_v8_parent_ref_v9,
    validate_append_candidate_manifest_v9,
    validate_v8_parent_bundle_v9,
)
from o2o_dps.upper_kara_cat_action_plan_distiller_v8 import (
    distill_upper_kara_cat_action_plan_teacher_v8,
    load_upper_kara_cat_action_plan_distillation_v8,
)


BUILD_ID = "v8-build"
LOADOUT_ID = "mighty_rage"
WAVES = (
    CatResidualSequenceWaveV1("wave-1", (0, 1)),
    CatResidualSequenceWaveV1("wave-2", (2,)),
)


def _bundle() -> dict:
    return distill_upper_kara_cat_action_plan_teacher_v8(
        [],
        proposal_seeds=(1,),
        exact_build_id=BUILD_ID,
        loadout_id=LOADOUT_ID,
        waves=WAVES,
        policy_id_prefix="selected-v8",
        max_nonzero_candidates=0,
    )


def _program_and_bundle():
    bundle = _bundle()
    restored = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    parent_policy = next(iter(restored.values()))
    program = build_two_wave_cat_residual_sequence_runtime_v1(
        parent_policy,
        cat_resolver_factory=lambda: None,
    )[0]
    return program, bundle


def _frozen() -> dict:
    program, _ = _program_and_bundle()
    return {
        "terminal_status": "COMPLETE",
        "campaign_id": "selected-v8-campaign",
        "build_id": BUILD_ID,
        "campaign_contract": {
            "loadout_ids": ["no_potion", LOADOUT_ID],
            "search_spec": {"kind": "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8"},
        },
        "winner": {
            "loadout_id": LOADOUT_ID,
            "program_ref": program.program_id,
            "program_id": program.program_id,
            "program_key": program.program_key(),
            "program_origin": program.origin.value,
        },
        "frozen_program": program.to_dict(),
    }


def _parent():
    return freeze_v8_parent_ref_v9(
        _frozen(),
        source_freeze_ref="results/v8/freeze/frozen.json",
        policy_bundle_ref="results/v8/candidates/mighty_rage.policy.json",
    )


def _receipt(program) -> dict:
    return {
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": [],
        "program": program.to_dict(),
    }


def test_freezes_selected_v8_identity_and_resolves_bundle() -> None:
    parent = _parent()
    _, bundle = _program_and_bundle()

    policy = validate_v8_parent_bundle_v9(parent, bundle)

    assert parent.build_id == BUILD_ID
    assert parent.loadout_id == LOADOUT_ID
    assert parent.program_ref == policy.policy_id
    assert parent.program_ref == parent.program_id
    assert parent.program_key
    assert parent.policy_bundle_ref.endswith("mighty_rage.policy.json")


def test_append_contract_roundtrip_has_one_loadout_and_fresh_balanced_cohorts() -> None:
    contract = build_cat_action_plan_append_contract_v9(
        campaign_id="cat-action-plan-append-v9-test",
        frozen_v8=_frozen(),
        source_freeze_ref="results/v8/freeze/frozen.json",
        policy_bundle_ref="results/v8/candidates/mighty_rage.policy.json",
    )
    parsed = cat_action_plan_append_contract_from_dict_v9(contract.to_dict())

    assert parsed == contract
    assert parsed.loadout_ids == (LOADOUT_ID,)
    assert len(parsed.proposal_examples) == 128
    assert len(parsed.selection_examples) == 128
    assert len(parsed.heldout_examples) == 256
    assert parsed.teacher_max_states == TEACHER_MAX_STATES == 3
    assert parsed.teacher_plan_shard_size == TEACHER_PLAN_SHARD_SIZE == 64
    assert parsed.max_append_candidates == MAX_APPEND_CANDIDATES == 63
    assert parsed.seed_shard_count == SEED_SHARD_COUNT == 256
    assert parsed.train_examples[0].seed == FRESH_TRAIN_SEED_START
    assert parsed.heldout_examples[0].seed == FRESH_EVALUATION_SEED_START
    assert not (
        {row.seed for row in parsed.proposal_examples}
        & {row.seed for row in parsed.selection_examples}
    )
    assert not (
        {row.seed for row in parsed.train_examples}
        & {row.seed for row in parsed.heldout_examples}
    )
    assert {
        row.first_wave_arrival_ms for row in parsed.proposal_examples
    } == set(FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS)
    assert {
        row.first_wave_arrival_ms for row in parsed.selection_examples
    } == set(FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS)


def test_contract_parser_rejects_a_second_loadout_or_changed_seed() -> None:
    wire = CatActionPlanAppendContractV9(
        campaign_id="cat-action-plan-append-v9-test",
        paired_parent_ref=_parent(),
    ).to_dict()
    changed = deepcopy(wire)
    changed["loadout_ids"].append("rage")
    with pytest.raises(ValueError, match="canonical"):
        cat_action_plan_append_contract_from_dict_v9(changed)

    changed = deepcopy(wire)
    changed["teacher_max_states"] = 4
    with pytest.raises(ValueError, match="teacher_max_states"):
        cat_action_plan_append_contract_from_dict_v9(changed)

    changed = deepcopy(wire)
    changed["proposal_examples"][0]["seed"] += 1
    with pytest.raises(ValueError, match="canonical"):
        cat_action_plan_append_contract_from_dict_v9(changed)


def test_manifest_names_same_parent_at_top_level_and_on_every_program() -> None:
    parent_program, _ = _program_and_bundle()
    contract = CatActionPlanAppendContractV9(
        campaign_id="cat-action-plan-append-v9-test",
        paired_parent_ref=_parent(),
    )
    manifest = build_append_candidate_manifest_v9(
        contract=contract,
        proposal_guide_ids=("v8-parent", "offline-expert"),
        programs=(_receipt(parent_program),),
    )

    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["paired_parent_ref"] == contract.paired_parent_ref.to_dict()
    assert manifest["programs"][0]["paired_parent_ref"] == parent_program.program_id
    assert validate_append_candidate_manifest_v9(
        manifest, contract=contract
    ) == manifest


def test_manifest_rejects_missing_or_changed_parent_identity() -> None:
    parent_program, _ = _program_and_bundle()
    contract = CatActionPlanAppendContractV9(
        campaign_id="cat-action-plan-append-v9-test",
        paired_parent_ref=_parent(),
    )
    manifest = build_append_candidate_manifest_v9(
        contract=contract,
        proposal_guide_ids=(),
        programs=(_receipt(parent_program),),
    )
    changed = deepcopy(manifest)
    changed["programs"][0]["paired_parent_ref"] = "another-parent"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_append_candidate_manifest_v9(changed, contract=contract)

    changed = deepcopy(manifest)
    changed["paired_parent_ref"]["policy_bundle_ref"] = "other.json"
    with pytest.raises(ValueError, match="differs from campaign"):
        validate_append_candidate_manifest_v9(changed, contract=contract)


def test_parent_freeze_rejects_non_v8_or_inconsistent_program_key() -> None:
    frozen = _frozen()
    frozen["campaign_contract"]["search_spec"]["kind"] = "OTHER"
    with pytest.raises(ValueError, match="not the V8"):
        freeze_v8_parent_ref_v9(
            frozen,
            source_freeze_ref="frozen.json",
            policy_bundle_ref="bundle.json",
        )

    frozen = _frozen()
    frozen["winner"]["program_key"] += "changed"
    with pytest.raises(ValueError, match="inconsistent"):
        freeze_v8_parent_ref_v9(
            frozen,
            source_freeze_ref="frozen.json",
            policy_bundle_ref="bundle.json",
        )
