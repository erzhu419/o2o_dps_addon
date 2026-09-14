from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from o2o_dps.cat_external_press_action_teacher_v1 import STATE_SELECTION_CONTRACT
from o2o_dps.conditional_cat_branch_v1 import FrozenRuleV1, _signature
from o2o_dps.cat_sparse_guard_policy_v2 import (
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
    sparse_guard_features_v2,
)
from o2o_dps.development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    build_development_wave_case_v1,
)
from o2o_dps.development_wave_stratified_v1 import WAVE_STRATA
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.factored_cat_branch_router_v1 import mechanism_route_v1
from o2o_dps.factored_external_press_matrix_v1 import (
    MAX_SAMPLE_INDEX,
    MATRIX_PHASES,
    MATRIX_RANKS,
    MATRIX_STRATA,
    STRATUM_BINDINGS,
    _execution_semantic,
    _existing_matrix_artifact_valid,
    authorize_matrix_transfer_v1,
    _write_json,
    build_matrix_case_v1,
    execute_matrix_case_v1,
    fit_matrix_proposal_v1,
    fit_matrix_sparse_guard_shortlist_v2,
    matrix_case_filename_v1,
    matrix_seed_v1,
    reduce_matrix_fresh_v1,
    run_matrix_batch_v1,
)


def _case(rank: int, stratum: str, phase: str, sample_index: int) -> DevelopmentWaveCaseV1:
    seed = matrix_seed_v1(rank, stratum, phase, sample_index)
    original = build_development_wave_case_v1(seed)
    spec = deepcopy(original.case_spec)
    request = deepcopy(original.request)
    items = request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
    items[14] = {"id": {7: 700, 11: 1100, 9: 900}[rank]}
    items[15] = (
        {"id": 701} if rank == 7 else {"id": 1101} if rank == 11 else {}
    )
    spec["source_wave_ref"] = WAVE_STRATA[STRATUM_BINDINGS[stratum]]
    spec["build_ref"] = f"test-rank-{rank}"
    spec["build_transplant"] = {"representative_rank": rank}
    spec["initial_state"]["target_max_hp"] = {
        "q05": 30_000, "q60": 100_000, "q95": 250_000, "multi_2": 100_000,
    }[stratum]
    if stratum == "multi_2":
        spec["two_wave_model"] = {"model_wave_count": 2}
    load = DynamicRolloutLoadV3.bind(
        request, seed, original.dynamic_load.config,
    )
    return DevelopmentWaveCaseV1(
        spec, request, load, original.target_contexts,
    )


def _database(_case: DevelopmentWaveCaseV1) -> dict:
    return {"items": [
        {"id": 700, "type": 13, "handType": 3, "weaponSpeed": 2.8},
        {"id": 701, "type": 13, "handType": 3, "weaponSpeed": 1.8},
        {"id": 1100, "type": 13, "handType": 3, "weaponSpeed": 2.4},
        {"id": 1101, "type": 13, "handType": 3, "weaponSpeed": 1.8},
        {"id": 900, "type": 13, "handType": 4, "weaponSpeed": 3.4},
    ]}


def _teacher(
    case: DevelopmentWaveCaseV1, delta: float = 5.0, *,
    weapon_mode: str = "TWO_HAND", nearby_enemies: int = 1,
) -> dict:
    combat = {
        "rage": 55.0,
        "mainhand_swing_remaining_s": 1.5,
        "target_health_pct": 70.0,
        "nearby_enemies": nearby_enemies,
        "weapon_mode": weapon_mode,
        "target_exists": True,
        "bloodthirst_ready_in_s": 0.0,
        "whirlwind_ready_in_s": 0.0,
        "queued_swing": "KEEP",
        "flurry_talent": True,
        "flurry_active": False,
        "casting_slam": False,
        "gcd_ready": True,
    }
    observation = {"combat": combat, "combat_elapsed_s": 5.0}
    opportunity_rule = asdict(FrozenRuleV1(
        "WW_TO_BT", *_signature(combat, "WW_TO_BT")
    ))
    return {
        "schema": "cat_external_press_action_teacher/v1",
        "status": "COMPLETE_EXTERNAL_PRESS_TEACHER_NONVOTING",
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "period_ms": 100,
        "state_selection_contract": STATE_SELECTION_CONTRACT,
        "baseline_press_clock_configuration_mode": "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
        "baseline_terminal": {"status": "COMPLETED"},
        "action_opportunity_rule_count": 1,
        "action_opportunity_press_count": 1,
        "action_opportunity_coverage": [{
            "rule": opportunity_rule,
            "opportunity_press_count": 1,
            "first_decision_index": 0,
            "last_decision_index": 0,
            "phase_counts": {"EARLY": 0, "MIDDLE": 1, "LATE": 0},
        }],
        "sparse_action_opportunity_contract": (
            SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2
        ),
        "sparse_action_opportunity_count": 1,
        "sparse_action_opportunities": [{
            "kind": "WW_TO_BT",
            "decision_index": 0,
            "features": sparse_guard_features_v2(observation),
        }],
        "branches": [{
            "kind": "WW_TO_BT",
            "status": "COMPLETE_BRANCH_SMOKE",
            "branch_action_accepted": True,
            "strict_single_intervention_verified": True,
            "press_clock_configuration_mode": "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
            "paired_effective_damage_delta": delta,
            "decision_index": 0,
            "policy_observation": {"observation": observation},
        }],
    }


def _pair(case: DevelopmentWaveCaseV1, rule, *_args, **_kwargs) -> dict:
    active = rule.kind is not None
    return {
        "schema": "factored_external_press_fresh_pair/v1",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "period_ms": 100,
        "press_clock_configuration_modes": {
            "cat": "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
            "no_op": "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
            "candidate": "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
        },
        "rule": asdict(rule),
        "rule_active": active,
        "status": "COMPLETE_FRESH_PAIR" if active else "EXACT_CAT_NOOP_FALLBACK",
        "technical_receipts_ready": True,
        "comparison_ready": active,
        "candidate_branch_action_accepted": active,
        "strict_single_intervention_verified": active,
        "candidate_intervention_count": 1 if active else 0,
        "paired_effective_damage_delta": 5.0 if active else None,
        "cat_terminal": {"status": "COMPLETED", "own_effective_damage": 100.0},
        "candidate_terminal": {
            "status": "COMPLETED",
            "own_effective_damage": 105.0 if active else 100.0,
        },
        "no_op_terminal": {"status": "COMPLETED", "own_effective_damage": 100.0},
    }


class _InlineExecutor:
    def __init__(self, *, max_workers, initializer, initargs):
        self.max_workers = max_workers
        self.initializer = initializer
        self.initargs = initargs

    def __enter__(self):
        self.initializer(*self.initargs)
        return self

    def __exit__(self, *_args):
        return False

    def submit(self, function, item):
        future = Future()
        try:
            future.set_result(function(item))
        except Exception as error:
            future.set_exception(error)
        return future


class FactoredExternalPressMatrixV1Tests(unittest.TestCase):
    def test_phase_seeds_are_disjoint_and_reused_as_randomized_blocks(self) -> None:
        seeds = {
            matrix_seed_v1(rank, stratum, phase, index)
            for rank in MATRIX_RANKS
            for stratum in MATRIX_STRATA
            for phase in MATRIX_PHASES
            for index in (0, 17, 99_999)
        }
        self.assertEqual(3 * 3, len(seeds))
        for phase in MATRIX_PHASES:
            for index in (0, 17):
                self.assertEqual(1, len({
                    matrix_seed_v1(rank, stratum, phase, index)
                    for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
                }))
        with self.assertRaisesRegex(ValueError, "sample_index"):
            matrix_seed_v1(7, "q05", "training", 1_000_000)

    def test_relocated_paths_are_forwarded_to_exact_build_binder(self) -> None:
        destination = _case(7, "q05", "training", 0)
        selector = Path("/relocated/selector/manifest.json")
        representatives = Path("/relocated/selector/representatives.jsonl")
        catalog_manifest = Path("/relocated/catalog/manifest.json")
        catalog_data = Path("/relocated/catalog/catalog.jsonl.gz")
        item_db = Path("/relocated/wowsims/db.json")
        with (
            patch(
                "o2o_dps.factored_external_press_matrix_v1."
                "build_stratified_wave_case_v1",
                return_value=(destination, {}),
            ),
            patch(
                "o2o_dps.factored_external_press_matrix_v1."
                "bind_historical_build_to_wave_case_v1",
                return_value=destination,
            ) as bind,
        ):
            result = build_matrix_case_v1(
                7, "q05", "training", 0,
                item_database_path=item_db,
                selector_manifest_path=selector,
                representatives_path=representatives,
                catalog_manifest_path=catalog_manifest,
                catalog_data_path=catalog_data,
            )
        self.assertIs(destination, result)
        self.assertEqual(item_db, bind.call_args.kwargs["item_database_path"])
        self.assertEqual(selector, bind.call_args.kwargs["selector_manifest_path"])
        self.assertEqual({
            "representatives": representatives,
            "catalog_manifest": catalog_manifest,
            "catalog_data": catalog_data,
        }, bind.call_args.kwargs["profile_path_overrides"])

    def test_one_training_worker_is_compact_and_forwards_max_states(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            return_value=_teacher(case),
        ) as teacher:
            artifact = execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database, max_states=2, max_presses=19,
            )
        self.assertEqual("factored_external_press_matrix_case/v1", artifact["schema"])
        self.assertFalse(artifact["raw_chronicle_rows_loaded"])
        self.assertNotIn("dynamic_load_config", artifact["case_projection"])
        self.assertNotIn("target_contexts", artifact["case_projection"])
        self.assertEqual(2, teacher.call_args.kwargs["max_states"])
        self.assertEqual(19, teacher.call_args.kwargs["max_presses"])
        self.assertEqual(
            STATE_SELECTION_CONTRACT,
            artifact["execution_contract"]["semantic"][
                "teacher_state_selection_contract"
            ],
        )

    def test_worker_rejects_mismatched_teacher_selection_contract(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        teacher = _teacher(case)
        teacher["state_selection_contract"] = "STALE_SELECTION_CONTRACT"
        with (
            patch(
                "o2o_dps.factored_external_press_matrix_v1."
                "run_cat_external_press_action_teacher_v1",
                return_value=teacher,
            ),
            self.assertRaisesRegex(ValueError, "state-selection contract differs"),
        ):
            execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database,
            )

    def test_legacy_selection_contract_is_readable_but_not_reusable(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            return_value=_teacher(case),
        ):
            current = execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database,
            )
        legacy = deepcopy(current)
        legacy["execution_contract"]["semantic"].pop(
            "teacher_state_selection_contract"
        )
        legacy_semantic = _execution_semantic(legacy["execution_contract"])
        self.assertNotIn("teacher_state_selection_contract", legacy_semantic)

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / matrix_case_filename_v1(
                7, "q05", "training", 0,
            )
            _write_json(path, legacy)
            self.assertFalse(_existing_matrix_artifact_valid(
                path, rank=7, stratum="q05", phase="training",
                sample_index=0, expected_case=case,
                expected_execution_contract=current["execution_contract"],
            ))

    def test_full_staged_reducer_requires_transfer_before_fresh_rule(self) -> None:
        base = _case(7, "q05", "training", 0)
        database = _database(base)
        training = []
        def compatible_teacher(case, *_args, **_kwargs):
            route = mechanism_route_v1(case, item_database=database)
            nearby_enemies = {
                "one": 1, "two": 2, "three_plus": 3,
            }[route.target_count_band]
            return _teacher(
                case, weapon_mode=route.weapon_mode,
                nearby_enemies=nearby_enemies,
            )

        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            side_effect=compatible_teacher,
        ):
            for rank in MATRIX_RANKS:
                for stratum in MATRIX_STRATA:
                    for sample_index in (0, 1, 2):
                        case = _case(rank, stratum, "training", sample_index)
                        training.append(execute_matrix_case_v1(
                            case, rank=rank, stratum=stratum, phase="training",
                            sample_index=sample_index, bridge_factory=lambda: None,
                            item_database=database,
                        ))
        proposal = fit_matrix_proposal_v1(
            training, item_database=database, min_distinct_seeds=2,
        )
        self.assertGreater(proposal["router"]["proposed_transfer_route_count"], 0)
        self.assertEqual(0, proposal["router"]["eligible_fresh_test_route_count"])

        transfer = []
        with patch(
            "o2o_dps.factored_external_press_matrix_v1._evaluate_pair",
            side_effect=_pair,
        ):
            for rank in MATRIX_RANKS:
                for stratum in MATRIX_STRATA:
                    for sample_index in (0, 1):
                        case = _case(
                            rank, stratum, "held_out_transfer", sample_index,
                        )
                        transfer.append(execute_matrix_case_v1(
                            case, rank=rank, stratum=stratum,
                            phase="held_out_transfer", sample_index=sample_index,
                            bridge_factory=lambda: None, item_database=database,
                            router=proposal["router"],
                        ))
        authorization = authorize_matrix_transfer_v1(
            proposal, transfer, item_database=database, min_distinct_seeds=2,
        )
        self.assertGreater(authorization["authorized_route_count"], 0)
        self.assertEqual(
            proposal["execution_semantic_contract"],
            authorization["execution_semantic_contract"],
        )
        self.assertTrue(all(
            row["execution_contract"]["router_lineage"] == {
                "router_wrapper_schema": "factored_external_press_matrix_proposal/v1",
                "mechanism_route": row["mechanism_route"],
                "selected_rule": row["result"]["rule"],
            }
            for row in transfer
        ))

        fresh = []
        with patch(
            "o2o_dps.factored_external_press_matrix_v1._evaluate_pair",
            side_effect=_pair,
        ):
            for rank in MATRIX_RANKS:
                for stratum in MATRIX_STRATA:
                    case = _case(rank, stratum, "untouched_fresh", 0)
                    fresh.append(execute_matrix_case_v1(
                        case, rank=rank, stratum=stratum,
                        phase="untouched_fresh", sample_index=0,
                        bridge_factory=lambda: None, item_database=database,
                        router=authorization["router"],
                    ))
        result = reduce_matrix_fresh_v1(
            authorization, fresh, item_database=database,
        )
        self.assertEqual("COMPLETE_MATRIX_FRESH_NONVOTING", result["status"])
        self.assertEqual(12, result["fresh_case_count"])
        self.assertEqual(12, result["active_fresh_pair_count"])
        self.assertEqual(5.0, result["mean_paired_effective_damage_delta"])
        self.assertEqual(
            authorization["execution_semantic_contract"],
            result["execution_semantic_contract"],
        )
        self.assertTrue(all(
            row["execution_contract"]["router_lineage"] == {
                "router_wrapper_schema": "factored_external_press_matrix_authorization/v1",
                "mechanism_route": row["mechanism_route"],
                "selected_rule": row["result"]["rule"],
            }
            for row in fresh
        ))
        self.assertTrue(result["comparison_ready"])
        self.assertFalse(result["voting_eligible"])
        self.assertFalse(result["deployment_eligible"])

    def test_balanced_training_matrix_fits_sparse_guard_shortlist(self) -> None:
        base = _case(7, "q05", "training", 0)
        database = _database(base)
        training = []

        def compatible_teacher(case, *_args, **_kwargs):
            route = mechanism_route_v1(case, item_database=database)
            nearby_enemies = {
                "one": 1, "two": 2, "three_plus": 3,
            }[route.target_count_band]
            return _teacher(
                case, weapon_mode=route.weapon_mode,
                nearby_enemies=nearby_enemies,
            )

        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            side_effect=compatible_teacher,
        ):
            for rank in MATRIX_RANKS:
                for stratum in MATRIX_STRATA:
                    for sample_index in range(6):
                        case = _case(rank, stratum, "training", sample_index)
                        training.append(execute_matrix_case_v1(
                            case, rank=rank, stratum=stratum, phase="training",
                            sample_index=sample_index, bridge_factory=lambda: None,
                            item_database=database,
                        ))

        result = fit_matrix_sparse_guard_shortlist_v2(
            training, item_database=database,
        )

        self.assertEqual(72, result["training_case_count"])
        self.assertEqual(6, result["training_distinct_seed_count"])
        self.assertGreater(result["learner"]["shortlisted_guard_count"], 0)
        self.assertFalse(result["full_wave_candidate_policy_evaluated"])
        self.assertFalse(result["voting_eligible"])
        self.assertFalse(result["deployment_eligible"])

    def test_reducer_rejects_a_partial_matrix(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            return_value=_teacher(case),
        ):
            artifact = execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database,
            )
        with self.assertRaisesRegex(ValueError, "incomplete or unbalanced"):
            fit_matrix_proposal_v1(
                [artifact], item_database=database, min_distinct_seeds=2,
            )

    def test_reducer_rejects_mixed_semantic_execution_contracts(self) -> None:
        base = _case(7, "q05", "training", 0)
        database = _database(base)
        rows = []
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            side_effect=lambda case, *_args, **_kwargs: _teacher(case),
        ):
            for rank in MATRIX_RANKS:
                for stratum in MATRIX_STRATA:
                    case = _case(rank, stratum, "training", 0)
                    rows.append(execute_matrix_case_v1(
                        case, rank=rank, stratum=stratum, phase="training",
                        sample_index=0, bridge_factory=lambda: None,
                        item_database=database,
                    ))
        changed = deepcopy(rows[0])
        changed["execution_contract"]["semantic"]["period_ms"] = 125
        changed["result"]["period_ms"] = 125
        rows[0] = changed
        with self.assertRaisesRegex(ValueError, "mixes semantic execution contracts"):
            fit_matrix_proposal_v1(
                rows, item_database=database, min_distinct_seeds=2,
            )

    def test_batch_modulo_shard_has_requested_48_training_items(self) -> None:
        def completed(item):
            rank, stratum, sample_index, output = item
            return {
                "rank": rank, "stratum": stratum,
                "sample_index": sample_index,
                "seed": matrix_seed_v1(rank, stratum, "training", sample_index),
                "output": output, "result_status": "FAKE_COMPLETE",
            }

        with TemporaryDirectory() as temporary:
            with (
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.ProcessPoolExecutor",
                    _InlineExecutor,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_initializer"
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_execute_one",
                    side_effect=completed,
                ),
            ):
                result = run_matrix_batch_v1(
                    phase="training", samples_per_cell=24,
                    shard_index=0, shard_count=6, workers=48,
                    output_directory=Path(temporary),
                )
        self.assertEqual("COMPLETE_BATCH_SHARD", result["status"])
        self.assertEqual(288, result["global_item_count"])
        self.assertEqual(48, result["assigned_item_count"])
        self.assertEqual(48, result["executed_item_count"])
        self.assertEqual(48, result["workers"])
        self.assertEqual(0, result["failed_item_count"])

    def test_batch_sample_start_offsets_seeds_without_changing_item_count(self) -> None:
        def completed(item):
            rank, stratum, sample_index, output = item
            return {
                "rank": rank, "stratum": stratum,
                "sample_index": sample_index,
                "seed": matrix_seed_v1(rank, stratum, "held_out_transfer", sample_index),
                "output": output, "result_status": "FAKE_COMPLETE",
            }

        with TemporaryDirectory() as temporary:
            with (
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.ProcessPoolExecutor",
                    _InlineExecutor,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_initializer"
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._phase_router_wrapper",
                    return_value=({"execution_semantic_contract": {}}, {}),
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.build_matrix_case_v1",
                    side_effect=lambda rank, stratum, phase, sample_index, **_kwargs: (
                        _case(rank, stratum, phase, sample_index)
                    ),
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._execution_contract_v1",
                    return_value={"semantic": {}},
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_execute_one",
                    side_effect=completed,
                ),
            ):
                result = run_matrix_batch_v1(
                    phase="held_out_transfer", sample_start=8,
                    samples_per_cell=2, shard_index=0, shard_count=1,
                    workers=24, output_directory=Path(temporary),
                    router_path=Path("proposal.json"),
                )
        self.assertEqual(8, result["sample_start"])
        self.assertEqual(24, result["global_item_count"])
        self.assertEqual(24, result["assigned_item_count"])
        self.assertEqual({8, 9}, {
            row["sample_index"] for row in result["completed"]
        })

    def test_batch_rejects_sample_window_outside_seed_namespace(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "sample_start"):
                run_matrix_batch_v1(
                    phase="training", sample_start=-1, samples_per_cell=1,
                    shard_index=0, shard_count=1, workers=1,
                    output_directory=Path(temporary),
                )
            with self.assertRaisesRegex(ValueError, "sample range"):
                run_matrix_batch_v1(
                    phase="training", sample_start=MAX_SAMPLE_INDEX,
                    samples_per_cell=2, shard_index=0, shard_count=1,
                    workers=1, output_directory=Path(temporary),
                )

    def test_batch_skips_one_existing_valid_atomic_artifact(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            return_value=_teacher(case),
        ):
            artifact = execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database,
            )
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / matrix_case_filename_v1(
                7, "q05", "training", 0,
            )
            _write_json(path, artifact)
            self.assertFalse(any(directory.glob("*.tmp-*")))
            with (
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.build_matrix_case_v1",
                    return_value=case,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._load_item_database",
                    return_value=database,
                ),
            ):
                result = run_matrix_batch_v1(
                    phase="training", samples_per_cell=1,
                    shard_index=0, shard_count=12, workers=1,
                    output_directory=directory,
                )
        self.assertEqual(1, result["assigned_item_count"])
        self.assertEqual(1, result["skipped_existing_count"])
        self.assertEqual(0, result["executed_item_count"])

    def test_batch_does_not_skip_artifact_from_a_different_execution_contract(self) -> None:
        case = _case(7, "q05", "training", 0)
        database = _database(case)
        with patch(
            "o2o_dps.factored_external_press_matrix_v1."
            "run_cat_external_press_action_teacher_v1",
            return_value=_teacher(case),
        ):
            artifact = execute_matrix_case_v1(
                case, rank=7, stratum="q05", phase="training",
                sample_index=0, bridge_factory=lambda: None,
                item_database=database,
            )

        def completed(item):
            rank, stratum, sample_index, output = item
            return {
                "rank": rank, "stratum": stratum,
                "sample_index": sample_index,
                "seed": matrix_seed_v1(rank, stratum, "training", sample_index),
                "output": output, "result_status": "FAKE_COMPLETE",
            }

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / matrix_case_filename_v1(
                7, "q05", "training", 0,
            )
            _write_json(path, artifact)
            with (
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.build_matrix_case_v1",
                    return_value=case,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._load_item_database",
                    return_value=database,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1.ProcessPoolExecutor",
                    _InlineExecutor,
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_initializer"
                ),
                patch(
                    "o2o_dps.factored_external_press_matrix_v1._batch_execute_one",
                    side_effect=completed,
                ),
            ):
                result = run_matrix_batch_v1(
                    phase="training", samples_per_cell=1,
                    shard_index=0, shard_count=12, workers=1,
                    output_directory=directory, period_ms=101,
                )
        self.assertEqual(0, result["skipped_existing_count"])
        self.assertEqual(1, result["executed_item_count"])


if __name__ == "__main__":
    unittest.main()
