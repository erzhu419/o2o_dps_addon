from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.offline_wave_d2_panel_v1 import (
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    PI_D,
)
from o2o_dps.offline_wave_d3_frozen_heldout_v1 import (
    PI_STAR,
    build_frozen_heldout_expansion_contract_v1,
    build_frozen_heldout_row_v1,
)
from o2o_dps.offline_wave_d3_train_eval_v1 import (
    CAMPAIGN_SCHEMA,
    CANDIDATE_SCHEMA,
    HELDOUT,
    PROPOSAL,
    SELECTION,
    SELECTION_SCHEMA,
)
from o2o_dps.offline_wave_policy_v1 import LANE_GCD
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
)
from o2o_dps.sim_bridge import ActionRef
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_remote_v1 import (
    EXPANSION_ID,
    FIRST_SEED,
    FROZEN_REMOTE,
    NODES,
    SEEDS_PER_NODE,
    SHARD_OUTPUT_SCHEMA,
    WORKERS_PER_SEED,
    build_node_batch_command_v1,
    build_seed_job_argv_v1,
    build_seed_job_specs_v1,
    merge_seed_job_results_v1,
)


CONTROLLERS = (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D, PI_STAR)


def _source():
    program = SearchedWaveProgramV1(
        program_id="frozen-winner",
        parent_program_id="offline-parent",
        source_refs=("selection-receipt",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            SearchedWaveStepV1(
                step_id="winner-step-0",
                kind=SearchedWaveStepKindV1.ACTION,
                at_or_after_ms=0,
                max_lateness_ms=500,
                proposal_source="frozen-selection",
                action_key="warrior.bloodthirst",
                action_ref=ActionRef(spell_id=23894),
                lane=LANE_GCD,
                target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT),
            ),
        ),
        tail_gcd_priority=("warrior.bloodthirst",),
        tail_queue_priority=(),
        tail_off_gcd_once=(),
    )
    frozen = {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": program.program_id,
        "proposal_arm": "combined",
        "behavior_key": program.behavior_key(),
        "program": program.to_dict(),
    }
    return {
        "schema": "development_offline_wave_policy_d900_d3/v1",
        "status": "D3_FROZEN_WINNER_HELDOUT_COMPLETE",
        "campaign": {
            "schema": CAMPAIGN_SCHEMA,
            "campaign_id": "original-d3",
            "paired_seed_cohorts": {
                PROPOSAL: [{"simulator_seed": 1, "teammate_seed": 101}],
                SELECTION: [{"simulator_seed": 11, "teammate_seed": 111}],
                HELDOUT: [{"simulator_seed": 21, "teammate_seed": 121}],
            },
        },
        "selection_receipt": {
            "schema": SELECTION_SCHEMA,
            "campaign_id": "original-d3",
            "status": "ONE_CANDIDATE_FROZEN_FOR_HELDOUT",
            "frozen_candidate": frozen,
            "heldout_rows_consumed": 0,
        },
    }


def _row(controller: str, pair: tuple[int, int], offset: float):
    dps = {
        CAT: 700.0,
        CONTRA_DEPLOYED: 500.0,
        CONTRA_NEW: 600.0,
        PI_D: 550.0,
        PI_STAR: 800.0,
    }[controller] + offset
    return {
        **build_frozen_heldout_row_v1(
            candidate_id="frozen-winner",
            controller_id=controller,
            simulator_seed=pair[0],
            teammate_seed=pair[1],
            replay_status="COMPLETE",
            score_status="COMPLETED",
            required_targets_dead=True,
            effective_damage=dps * 10.0,
            dps=dps,
            completion_time_ms=10_000,
            domain_fallback_calls=(0 if controller in {PI_D, PI_STAR} else None),
        ),
        "searched_action_attribution": (
            {"accepted_action_count": 3} if controller == PI_STAR else None
        ),
    }


def _seed_job_result(spec, ordinal: int):
    source = _source()
    contract = build_frozen_heldout_expansion_contract_v1(
        source=source,
        heldout_seed_pairs=spec.seed_pairs,
        expansion_id=f"{EXPANSION_ID}/{spec.job_id}",
    )
    rows = [
        _row(controller, pair, float(ordinal))
        for pair in spec.seed_pairs
        for controller in CONTROLLERS
    ]
    return {
        "schema": SHARD_OUTPUT_SCHEMA,
        "status": "FROZEN_WINNER_BLIND_HELDOUT_COMPLETE",
        "contract": contract,
        "receipt": {"valid_paired_seed_count": spec.seed_count},
        "runtime_rows": rows,
        "frozen_source_binding": {"component": "same"},
        "request_sha256": "request",
        "dynamic_config_sha256": "config",
        "evaluation_build_ref": "build",
        "target_rule_id": "target-rule",
    }


def test_frozen_plan_is_48_checkpointed_seed_processes_on_four_nodes():
    specs = build_seed_job_specs_v1()
    assert len(specs) == len(NODES) * SEEDS_PER_NODE
    assert tuple(dict.fromkeys(spec.node for spec in specs)) == NODES
    assert specs[0].first_seed == FIRST_SEED
    assert all(spec.seed_count == 1 for spec in specs)
    assert all(spec.lane_workers == WORKERS_PER_SEED for spec in specs)
    pairs = [pair for spec in specs for pair in spec.seed_pairs]
    assert len(pairs) == 48
    assert len(set(pairs)) == 48


def test_shard_argv_removes_old_seed_surface_and_loads_frozen_result():
    spec = build_seed_job_specs_v1()[0]
    argv = build_seed_job_argv_v1(spec)
    assert "--simulator-seed" not in argv
    assert "--teammate-seed" not in argv
    assert "--seed-count" not in argv
    assert argv[argv.index("--frozen-d3-result") + 1] == FROZEN_REMOTE
    assert argv[argv.index("--heldout-seed") + 1] == str(spec.first_seed)
    assert argv[argv.index("--heldout-seed-count") + 1] == "1"
    assert argv[argv.index("--lane-workers") + 1] == "5"


def test_node_batch_uses_one_shell_for_twelve_checkpointed_seed_processes():
    specs = tuple(
        spec for spec in build_seed_job_specs_v1() if spec.node == NODES[0]
    )
    command = build_node_batch_command_v1(specs)
    assert len(specs) == 12
    assert command.count(" & pids=\"$pids $!\"") == 12
    assert command.count("if test ! -s ") == 12
    assert "for pid in $pids; do wait $pid || status=1; done" in command
    assert "exit $status" in command


def test_merge_adjudicates_all_48_pairs_without_reselection():
    specs = build_seed_job_specs_v1()
    shards = {
        spec.job_id: _seed_job_result(spec, ordinal)
        for ordinal, spec in enumerate(specs)
    }
    merged = merge_seed_job_results_v1(
        frozen_source=_source(),
        specs=specs,
        seed_job_results=shards,
    )
    assert merged["status"] == "FROZEN_WINNER_BLIND_HELDOUT_COMPLETE"
    assert merged["receipt"]["requested_pair_count"] == 48
    assert merged["receipt"]["valid_paired_seed_count"] == 48
    assert len(merged["runtime_rows"]) == 240
    assert len(merged["pi_star_action_attribution_by_seed"]) == 48
    assert merged["contracts"]["selection_performed"] is False
    assert merged["contracts"]["candidate_generation_performed"] is False


def test_merge_rejects_a_shard_that_evaluated_different_seeds():
    specs = build_seed_job_specs_v1()
    shards = {
        spec.job_id: _seed_job_result(spec, ordinal)
        for ordinal, spec in enumerate(specs)
    }
    bad = deepcopy(shards[specs[2].job_id])
    bad["contract"]["heldout_seed_pairs"][0]["simulator_seed"] += 1
    shards[specs[2].job_id] = bad
    with pytest.raises(ValueError, match="wrong seed pairs"):
        merge_seed_job_results_v1(
            frozen_source=_source(),
            specs=specs,
            seed_job_results=shards,
        )


def test_merge_rejects_stale_checkpoint_with_same_candidate_id_but_drifted_program():
    specs = build_seed_job_specs_v1()
    shards = {
        spec.job_id: _seed_job_result(spec, ordinal)
        for ordinal, spec in enumerate(specs)
    }
    bad = deepcopy(shards[specs[2].job_id])
    bad["contract"]["frozen_candidate"]["program"]["tail_gcd_priority"] = []
    shards[specs[2].job_id] = bad

    with pytest.raises(ValueError, match="exact frozen shard contract"):
        merge_seed_job_results_v1(
            frozen_source=_source(),
            specs=specs,
            seed_job_results=shards,
        )
