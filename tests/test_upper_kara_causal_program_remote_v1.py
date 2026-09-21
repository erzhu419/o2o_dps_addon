from __future__ import annotations

import json
import hashlib
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    TERMINAL_INVALID,
    ContinuousTwoWaveRemoteCampaignV1,
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    campaign_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    NativeTrainingObservableFrontierProviderV1,
    RemoteCausalProgramRuntimeV1,
    _validate_training_terminal_v1,
    build_default_remote_runtime_v1,
    evaluation_terminal_name_v1,
    freeze_training_winner_v1,
    run_evaluation_shard_v1,
    run_training_shard_v1,
    summarize_evaluation_v1,
    train_terminal_name_v1,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1,
    TwoWaveExampleV1,
    imported_incumbent_programs_v1,
)
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)
from scripts.upper_kara_causal_program_remote_stage_v1 import (
    build_upper_kara_causal_program_stage_plan_v1,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    REMOTE_OUTPUT_PATH_BATCH_SIZE,
    _read_remote_terminal_projections_v1,
    _remote_existing_paths_v1,
    _remote_missing_required_paths_v1,
    build_upper_kara_causal_program_remote_plan_v1,
    inventory_upper_kara_causal_program_remote_v1,
)
from scripts.factored_press_remote_submit_v1 import (
    scheduler_escalation_inventory_v1,
)


BUILD_ID = "clean_dual_weapon_probe"
NATIVE_RUNTIME_AVAILABLE = (
    DEFAULT_EXACT_BRIDGE.is_file()
    and DEFAULT_BINDING.is_file()
    and (WORKSPACE_ROOT / "wowsims-turtle").is_dir()
)


def _campaign() -> ContinuousTwoWaveRemoteCampaignV1:
    train_arrivals = (0, 0, 3_000, 3_000, 7_000, 7_000)
    train = tuple(
        TwoWaveExampleV1(index + 1, train_arrivals[index])
        for index in range(len(train_arrivals))
    )
    evaluation = tuple(
        TwoWaveExampleV1(index + 101, 7_000 if index == 5 else index * 150)
        for index in range(6)
    )
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="remote-smoke",
        build_id=BUILD_ID,
        train_examples=train,
        evaluation_examples=evaluation,
        generation_config=UpperKaraCausalProgramGenerationConfigV1(
            max_programs=4
        ),
    )


def _write_campaign(root: Path) -> Path:
    path = root / "campaign.json"
    path.write_text(
        json.dumps(_campaign().to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return path


def _searched_program(loadout_id: str) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=f"searched::{loadout_id}",
        selector=OrderedGuardSelectorV1(
            alternatives=(), fallback=ProgramDecisionV1(wait_ms=250)
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=("fake-native-snapshot",),
    )


class _FakeCase:
    def __init__(
        self, seed: int, loadout_id: str, first_attackable_ms: int
    ) -> None:
        self.dynamic_load = SimpleNamespace(
            request_sha256=hashlib.sha256(loadout_id.encode("ascii")).hexdigest(),
            config=SimpleNamespace(
                attackability_events=(
                    SimpleNamespace(
                        time_ms=first_attackable_ms,
                        target_index=0,
                        attackable=True,
                    ),
                )
            ),
        )
        self.case_spec = {
            "seed": seed,
            "required_target_indices": [0],
        }


class _FakeReplay:
    def __init__(
        self,
        loadout_id: str,
        *,
        invalidate_searched: bool,
        fail_searched: bool,
        cases_by_simulator,
    ) -> None:
        self.loadout_id = loadout_id
        self.invalidate_searched = invalidate_searched
        self.fail_searched = fail_searched
        self.cases_by_simulator = cases_by_simulator

    def replay(self, seed, program, *, max_decisions):
        assert max_decisions == DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1
        if self.fail_searched and program.origin is ProgramOriginV1.SEARCHED:
            raise RuntimeError("fake bridge failure")
        if self.invalidate_searched and program.origin is ProgramOriginV1.SEARCHED:
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.INVALID,
                state={"time_ms": 1_000},
                invalid_reason="fake invalid program",
            )
        if program.origin is ProgramOriginV1.SEARCHED:
            damage = 500.0 + 10.0 * BURST_LOADOUT_IDS_V1.index(self.loadout_id)
            if "cat-residual-overlay/v1:zero" not in program.source_refs:
                damage += 5.0
        else:
            source = program.selector.source_policy_id
            baseline_ids = [
                row.selector.source_policy_id
                for row in imported_incumbent_programs_v1(BUILD_ID)
            ]
            damage = 300.0 + 25.0 * baseline_ids.index(source)
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=ReplayStatusV1.COMPLETE,
            state={
                "time_ms": (
                    self.cases_by_simulator[seed]
                    .dynamic_load.config.attackability_events[0]
                    .time_ms
                    + 1_000
                ),
                "dynamic_team_background": {
                    "simulated_damage_applied": damage,
                    "targets": [{"target_index": 0, "dead": True}],
                },
            },
        )


def _runtime(
    *, invalidate_searched: bool = False, fail_searched: bool = False
) -> RemoteCausalProgramRuntimeV1:
    def case_builder(
        seed,
        *,
        loadout_id,
        pull_time_ms,
        first_wave_arrival_ms,
        **_,
    ):
        return _FakeCase(
            seed, loadout_id, pull_time_ms + first_wave_arrival_ms
        )

    def generator(*, loadout_id, train_examples, train_cases):
        assert tuple(row.seed for row in train_examples) == (1, 3, 5)
        assert len(train_cases) == 3
        return (
            cat_zero_residual_program_v1(loadout_id),
            _searched_program(loadout_id),
        )

    def replay_factory(loadout_id, cases):
        return _FakeReplay(
            loadout_id,
            invalidate_searched=invalidate_searched,
            fail_searched=fail_searched,
            cases_by_simulator=cases,
        )

    return RemoteCausalProgramRuntimeV1(
        case_builder=case_builder,
        searched_program_generator=generator,
        program_replay_factory=replay_factory,
    )


class RemoteContractTests(unittest.TestCase):
    def test_campaign_is_not_48_cells_and_carries_7s_250ms_budget(self) -> None:
        campaign = _campaign()
        wire = campaign.to_dict()

        self.assertEqual(
            DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1, wire["max_decisions"]
        )
        self.assertEqual(250, wire["contract"]["reference_idle_wait_ms"])
        self.assertEqual(7_000, wire["contract"]["latest_configured_arrival_ms"])
        self.assertEqual(
            40,
            wire["contract"][
                "minimum_reference_idle_decisions_before_latest_arrival"
            ],
        )
        self.assertTrue(
            wire["contract"][
                "configured_budget_exceeds_reference_idle_requirement"
            ]
        )
        self.assertNotIn("cells", wire)
        self.assertFalse(wire["contract"]["legacy_independent_exact_cells"])
        self.assertEqual(campaign, campaign_from_dict_v1(wire))

        with self.assertRaisesRegex(ValueError, "idle-decision budget"):
            ContinuousTwoWaveRemoteCampaignV1(
                campaign_id="too-short",
                build_id=BUILD_ID,
                train_examples=campaign.train_examples,
                evaluation_examples=campaign.evaluation_examples,
                generation_config=campaign.generation_config,
                max_decisions=40,
            )

    def test_loadout_seed_shards_are_disjoint_and_complete(self) -> None:
        campaign = _campaign()
        train = assign_training_shards_v1(campaign)
        evaluation = assign_evaluation_shards_v1(campaign)

        self.assertEqual(24, len(train))
        self.assertEqual(6, len(evaluation))
        for loadout_id in campaign.loadout_ids:
            seeds = [
                example.seed
                for shard in train
                if shard.loadout_id == loadout_id
                for example in shard.examples
            ]
            self.assertEqual(
                sorted(row.seed for row in campaign.train_examples), sorted(seeds)
            )


class RemoteWorkerTests(unittest.TestCase):
    def test_train_freeze_eval_summary_pipeline_with_injected_runtime(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            campaign = _campaign()
            train_root = root / "train"
            runtime = _runtime()
            for shard in assign_training_shards_v1(campaign):
                terminal = run_training_shard_v1(
                    campaign_path=campaign_path,
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    output_path=train_root / train_terminal_name_v1(shard),
                    replay_workers=1,
                    runtime=runtime,
                )
                self.assertEqual(TERMINAL_COMPLETE, terminal["terminal_status"])
                self.assertFalse(
                    terminal["contract"]["evaluation_cases_materialized"]
                )
                self.assertTrue(
                    terminal["contract"][
                        "candidate_generation_used_proposal_cohort_only"
                    ]
                )
                self.assertTrue(
                    terminal["contract"][
                        "selection_validation_examples_not_passed_to_generator"
                    ]
                )
                receipts = {
                    row["program_ref"]: row for row in terminal["programs"]
                }
                self.assertEqual(len(terminal["programs"]), len(receipts))
                self.assertTrue(
                    all(row["program_ref"] in receipts for row in terminal["lanes"])
                )
                self.assertTrue(
                    all("program_key" not in row for row in terminal["lanes"])
                )
                self.assertTrue(
                    all("program_id" not in row for row in terminal["lanes"])
                )
                compact_lane_bytes = len(
                    json.dumps(
                        terminal["lanes"], separators=(",", ":")
                    ).encode("utf-8")
                )
                legacy_lane_bytes = len(
                    json.dumps(
                        [
                            {
                                **row,
                                "program_key": receipts[row["program_ref"]][
                                    "program_key"
                                ],
                            }
                            for row in terminal["lanes"]
                        ],
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                self.assertGreater(legacy_lane_bytes, compact_lane_bytes + 1_000)
                if shard.examples[0].first_wave_arrival_ms == 7_000:
                    self.assertTrue(
                        all(row["ttk_ms"] == 1_000 for row in terminal["lanes"])
                    )

            frozen_path = root / "frozen.json"
            frozen = freeze_training_winner_v1(
                campaign_path=campaign_path,
                training_root=train_root,
                output_path=frozen_path,
            )
            self.assertEqual(TERMINAL_COMPLETE, frozen["terminal_status"])
            self.assertEqual(BURST_LOADOUT_IDS_V1[-1], frozen["winner"]["loadout_id"])
            self.assertEqual(
                f"cat-residual-overlay::{BURST_LOADOUT_IDS_V1[-1]}::zero",
                frozen["winner"]["program_id"],
            )
            self.assertEqual(
                frozen["winner"]["program_id"],
                frozen["winner"]["program_ref"],
            )
            self.assertEqual(
                ProgramOriginV1.SEARCHED.value,
                frozen["winner"]["program_origin"],
            )
            self.assertEqual(
                ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT.value,
                frozen["best_incumbent"]["program_origin"],
            )
            self.assertEqual(
                ProgramOriginV1.SEARCHED.value,
                frozen["overall_best"]["program_origin"],
            )
            self.assertGreater(
                frozen["searched_minus_best_incumbent_train_gap"][
                    "mean_own_effective_damage"
                ],
                0.0,
            )
            self.assertFalse(frozen["contract"]["heldout_cases_materialized"])
            self.assertTrue(frozen["winner"]["program_id"].endswith("::zero"))
            self.assertEqual(
                "FALLBACK_ZERO_RESIDUAL_INSUFFICIENT_VALIDATION_PAIRS",
                frozen["selection_gate"]["status"],
            )
            self.assertEqual(
                len(BURST_LOADOUT_IDS_V1),
                len(frozen["selection_gate"]["per_loadout_admission"]),
            )
            self.assertEqual(
                len(BURST_LOADOUT_IDS_V1) * 2,
                len(frozen["selection_gate"]["training_ranking"]),
            )
            self.assertEqual(
                "DENSE_NUMERIC_ARRAYS_BY_PROGRAM_AND_SEED",
                frozen["selection_gate"]["contract"]["freeze_aggregation"],
            )
            self.assertTrue(
                frozen["selection_gate"]["contract"][
                    "all_complete_candidates_retained_in_training_ranking"
                ]
            )
            self.assertEqual(
                len(BURST_LOADOUT_IDS_V1) * 2 * len(campaign.train_examples),
                frozen["selection_gate"]["contract"][
                    "materialized_pair_row_count"
                ],
            )
            self.assertTrue(
                all("program_key" not in row for row in frozen["training_rows"])
            )

            def heldout_generator_forbidden(**_):
                raise AssertionError("held-out evaluation invoked training guides")

            eval_runtime = RemoteCausalProgramRuntimeV1(
                case_builder=runtime.case_builder,
                searched_program_generator=heldout_generator_forbidden,
                program_replay_factory=runtime.program_replay_factory,
                incumbent_program_factory=runtime.incumbent_program_factory,
            )
            eval_root = root / "eval"
            for shard in assign_evaluation_shards_v1(campaign):
                terminal = run_evaluation_shard_v1(
                    campaign_path=campaign_path,
                    frozen_path=frozen_path,
                    seed_shard_index=shard.seed_shard_index,
                    output_path=eval_root / evaluation_terminal_name_v1(shard),
                    replay_workers=1,
                    runtime=eval_runtime,
                )
                self.assertEqual(TERMINAL_COMPLETE, terminal["terminal_status"])
                self.assertEqual(
                    0, terminal["contract"]["search_or_generator_invocations"]
                )
                self.assertEqual(
                    ProgramOriginV1.SEARCHED.value,
                    terminal["frozen_program_origin"],
                )
                candidate = [
                    row for row in terminal["lanes"] if row["role"] == "CANDIDATE"
                ]
                baselines = [
                    row for row in terminal["lanes"] if row["role"] == "BASELINE"
                ]
                self.assertTrue(
                    all(
                        row["program_origin"] == ProgramOriginV1.SEARCHED.value
                        for row in candidate
                    )
                )
                self.assertTrue(
                    all(
                        row["program_origin"]
                        == ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT.value
                        for row in baselines
                    )
                )

            summary = summarize_evaluation_v1(
                campaign_path=campaign_path,
                frozen_path=frozen_path,
                evaluation_root=eval_root,
                output_path=root / "summary.json",
            )
            self.assertEqual(TERMINAL_COMPLETE, summary["terminal_status"])
            self.assertEqual(
                ProgramOriginV1.SEARCHED.value,
                summary["frozen_program_origin"],
            )
            self.assertTrue(
                summary["metric"][
                    "candidate_strictly_better_than_all_native_baselines"
                ]
            )

    def test_invalid_lane_is_distinct_and_has_null_metrics(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            shard = assign_training_shards_v1(_campaign())[0]
            terminal = run_training_shard_v1(
                campaign_path=campaign_path,
                loadout_id=shard.loadout_id,
                seed_shard_index=shard.seed_shard_index,
                output_path=root / "invalid.json",
                replay_workers=1,
                runtime=_runtime(invalidate_searched=True),
            )

            self.assertEqual(TERMINAL_INVALID, terminal["terminal_status"])
            invalid = [row for row in terminal["lanes"] if row["status"] == "INVALID"]
            self.assertTrue(invalid)
            for lane in invalid:
                self.assertIsNone(lane["own_effective_damage"])
                self.assertIsNone(lane["own_effective_dps"])
                self.assertIsNone(lane["ttk_ms"])
            self.assertIsNone(terminal["metric"])

    def test_failed_lane_is_distinct_and_has_null_metrics(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            shard = assign_training_shards_v1(_campaign())[0]
            terminal = run_training_shard_v1(
                campaign_path=campaign_path,
                loadout_id=shard.loadout_id,
                seed_shard_index=shard.seed_shard_index,
                output_path=root / "failed.json",
                replay_workers=1,
                runtime=_runtime(fail_searched=True),
            )

            self.assertEqual("FAILED", terminal["terminal_status"])
            failed = [row for row in terminal["lanes"] if row["status"] == "FAILED"]
            self.assertTrue(failed)
            for lane in failed:
                self.assertIsNone(lane["own_effective_damage"])
                self.assertIsNone(lane["own_effective_dps"])
                self.assertIsNone(lane["ttk_ms"])
            self.assertIsNone(terminal["metric"])

    def test_training_lane_program_ref_must_resolve_exact_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            campaign = _campaign()
            shard = assign_training_shards_v1(campaign)[0]
            terminal = run_training_shard_v1(
                campaign_path=campaign_path,
                loadout_id=shard.loadout_id,
                seed_shard_index=shard.seed_shard_index,
                output_path=root / "valid.json",
                replay_workers=1,
                runtime=_runtime(),
            )
            corrupted = json.loads(json.dumps(terminal))
            corrupted["lanes"][0]["program_ref"] = "not-a-receipt"

            with self.assertRaisesRegex(
                ValueError, "training terminal lane coverage mismatch"
            ):
                _validate_training_terminal_v1(corrupted, campaign, shard)

    def test_imported_incumbent_cannot_be_frozen_as_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            campaign = _campaign()
            train_root = root / "train"
            runtime = _runtime(invalidate_searched=True)
            for shard in assign_training_shards_v1(campaign):
                run_training_shard_v1(
                    campaign_path=campaign_path,
                    loadout_id=shard.loadout_id,
                    seed_shard_index=shard.seed_shard_index,
                    output_path=train_root / train_terminal_name_v1(shard),
                    replay_workers=1,
                    runtime=runtime,
                )

            frozen = freeze_training_winner_v1(
                campaign_path=campaign_path,
                training_root=train_root,
                output_path=root / "frozen.json",
            )
            self.assertEqual("FAILED", frozen["terminal_status"])
            self.assertIsNone(frozen["winner"])
            self.assertIsNone(frozen["frozen_program"])
            self.assertIsNone(frozen["metric"])
            self.assertEqual(
                ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT.value,
                frozen["best_incumbent"]["program_origin"],
            )
            self.assertEqual(
                frozen["best_incumbent"], frozen["overall_best"]
            )
            self.assertIsNone(
                frozen["searched_minus_best_incumbent_train_gap"]
            )


@unittest.skipUnless(
    NATIVE_RUNTIME_AVAILABLE,
    "the native bridge, runtime binding, and simulator cwd are required",
)
class RemoteWorkerNativeConcurrencyTests(unittest.TestCase):
    def test_searched_and_cat_lanes_match_with_one_and_four_workers(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            campaign = _campaign()
            shard = assign_training_shards_v1(campaign)[0]
            base_runtime = build_default_remote_runtime_v1(
                campaign,
                bridge_path=DEFAULT_EXACT_BRIDGE,
                bridge_cwd=WORKSPACE_ROOT / "wowsims-turtle",
                runtime_binding_path=DEFAULT_BINDING,
            )
            cat_program = next(
                program
                for program in imported_incumbent_programs_v1(BUILD_ID)
                if program.selector.source_policy_id == CAT_POLICY_ID
            )
            runtime = RemoteCausalProgramRuntimeV1(
                case_builder=base_runtime.case_builder,
                searched_program_generator=base_runtime.searched_program_generator,
                program_replay_factory=base_runtime.program_replay_factory,
                incumbent_program_factory=lambda _build_id: (cat_program,),
            )

            serial = run_training_shard_v1(
                campaign_path=campaign_path,
                loadout_id=shard.loadout_id,
                seed_shard_index=shard.seed_shard_index,
                output_path=root / "serial.json",
                replay_workers=1,
                runtime=runtime,
            )
            parallel = run_training_shard_v1(
                campaign_path=campaign_path,
                loadout_id=shard.loadout_id,
                seed_shard_index=shard.seed_shard_index,
                output_path=root / "parallel.json",
                replay_workers=4,
                runtime=runtime,
            )

            def selected_lanes(terminal):
                receipts = {
                    row["program_ref"]: row for row in terminal["programs"]
                }
                selected_refs = {
                    ref
                    for ref, receipt in receipts.items()
                    if receipt["program_origin"] == ProgramOriginV1.SEARCHED.value
                    or receipt["program"]["selector"].get("source_policy_id")
                    == CAT_POLICY_ID
                }
                return {
                    row["program_ref"]: row
                    for row in terminal["lanes"]
                    if row["program_ref"] in selected_refs
                }

            serial_lanes = selected_lanes(serial)
            parallel_lanes = selected_lanes(parallel)
            searched_count = sum(
                row["program_origin"] == ProgramOriginV1.SEARCHED.value
                for row in serial["programs"]
            )
            self.assertEqual(searched_count + 1, len(serial_lanes))
            self.assertEqual(serial_lanes, parallel_lanes)
            self.assertTrue(
                serial["contract"]["searched_candidates_are_cat_residual_overlays"]
            )
            self.assertTrue(
                serial["contract"]["zero_residual_exact_cat_included"]
            )

            searched_receipts = [
                row
                for row in serial["programs"]
                if row["program_origin"] == ProgramOriginV1.SEARCHED.value
            ]
            self.assertTrue(searched_receipts)
            self.assertEqual(4, len(serial["proposal_guide_ids"]))
            zero_receipts = [
                row
                for row in searched_receipts
                if "cat-residual-overlay/v1:zero"
                in row["program"]["source_refs"]
            ]
            self.assertEqual(1, len(zero_receipts))
            self.assertEqual([], zero_receipts[0]["proposal_guide_ids"])
            guided_residual_receipts = [
                row for row in searched_receipts if row not in zero_receipts
            ]
            self.assertTrue(guided_residual_receipts)
            self.assertTrue(
                all(
                    len(row["proposal_guide_ids"]) == 4
                    for row in guided_residual_receipts
                )
            )
            self.assertTrue(
                all(
                    any("cat.fury.profile1" in guide_id for guide_id in row["proposal_guide_ids"])
                    and any("contra260817" in guide_id for guide_id in row["proposal_guide_ids"])
                    and any("contra.deployed" in guide_id for guide_id in row["proposal_guide_ids"])
                    and any("offline_chronicle" in guide_id for guide_id in row["proposal_guide_ids"])
                    for row in guided_residual_receipts
                )
            )

    def test_train_frontier_projection_rejects_heldout_and_drops_future_fields(self) -> None:
        campaign = _campaign()
        runtime = build_default_remote_runtime_v1(
            campaign,
            bridge_path=DEFAULT_EXACT_BRIDGE,
            bridge_cwd=WORKSPACE_ROOT / "wowsims-turtle",
            runtime_binding_path=DEFAULT_BINDING,
        )
        loadout_id = campaign.loadout_ids[0]
        train_example = campaign.train_examples[-1]
        train_case = runtime.case_builder(
            train_example.seed,
            build_id=campaign.build_id,
            loadout_id=loadout_id,
            pull_time_ms=campaign.pull_time_ms,
            first_wave_arrival_ms=train_example.first_wave_arrival_ms,
        )
        heldout_example = campaign.evaluation_examples[-1]
        heldout_case = runtime.case_builder(
            heldout_example.seed,
            build_id=campaign.build_id,
            loadout_id=loadout_id,
            pull_time_ms=campaign.pull_time_ms,
            first_wave_arrival_ms=heldout_example.first_wave_arrival_ms,
        )
        generator = runtime.searched_program_generator
        train_cases = tuple(
            runtime.case_builder(
                row.seed,
                build_id=campaign.build_id,
                loadout_id=loadout_id,
                pull_time_ms=campaign.pull_time_ms,
                first_wave_arrival_ms=row.first_wave_arrival_ms,
            )
            for row in campaign.train_examples
        )
        generator(
            loadout_id=loadout_id,
            train_examples=campaign.train_examples,
            train_cases=train_cases,
        )
        generated = generator.results_by_loadout[loadout_id]
        source_generated = generated.source_candidate_set
        snapshot = source_generated.native_snapshot
        self.assertEqual(4, len(source_generated.guide_ids))
        bound_guides = generator.proposal_guides_by_loadout[loadout_id]
        self.assertEqual(
            source_generated.guide_ids,
            tuple(row.guide_id for row in bound_guides),
        )
        for guide in bound_guides:
            priorities = guide.action_priorities(train_cases[0], snapshot)
            self.assertTrue(
                any(value > 0 for value in priorities.values()),
                f"default guide {guide.guide_id} had no ranking signal",
            )
        provider = NativeTrainingObservableFrontierProviderV1(
            (train_case,),
            bridge_path=DEFAULT_EXACT_BRIDGE,
            bridge_cwd=WORKSPACE_ROOT / "wowsims-turtle",
        )
        outcome = provider(train_case, snapshot)

        self.assertEqual([], provider.observation_contract["future_fields_used"])
        self.assertFalse(
            provider.observation_contract["heldout_examples_materialized"]
        )
        self.assertNotIn("remaining_ms", outcome.state)
        self.assertNotIn("dynamic_idle_advance", outcome.state)
        self.assertNotIn("effective_target_armor", outcome.state)
        self.assertEqual(
            1, len(outcome.state["dynamic_team_background"]["targets"])
        )
        self.assertNotIn(
            "background_events_total", outcome.state["dynamic_team_background"]
        )
        with self.assertRaisesRegex(ValueError, "outside the bound training panel"):
            provider(heldout_case, snapshot)


class RemotePlanTests(unittest.TestCase):
    def test_stage_plan_includes_only_compact_offline_guide_and_no_external_directories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = _write_campaign(root)
            bridge = root / "bridge.linux-amd64"
            binding = root / "binding.json"
            database = root / "db.json"
            for path in (bridge, binding, database):
                path.write_text("{}", encoding="utf-8")

            plan = build_upper_kara_causal_program_stage_plan_v1(
                shared_home="/home/tester",
                run_id="remote-smoke",
                bridge=bridge,
                campaign=campaign,
                runtime_binding=binding,
                item_database=database,
            )

            self.assertEqual("DRY_RUN_NO_REMOTE_IO", plan["status"])
            self.assertFalse(plan["offline_data_staged"])
            self.assertFalse(plan["raw_offline_data_staged"])
            self.assertTrue(plan["compact_offline_guide_staged"])
            self.assertGreater(plan["compact_offline_guide_bytes"], 1_000_000)
            self.assertLess(plan["compact_offline_guide_bytes"], 2_000_000)
            self.assertFalse(plan["external_project_directories_staged"])
            self.assertTrue(plan["external_source_code_closure_staged"])
            source_closures = [
                row for row in plan["copy_specs"] if row.get("include_extensions")
            ]
            self.assertEqual(1, len(source_closures))
            self.assertEqual(
                [".lua", ".toc", ".xml"],
                source_closures[0]["include_extensions"],
            )
            self.assertEqual(33, plan["contra260817_source_code_file_count"])
            self.assertGreater(plan["contra260817_source_code_bytes"], 4_000_000)
            self.assertLess(plan["contra260817_source_code_bytes"], 5_000_000)
            self.assertEqual(
                "LIVE_TREE", plan["contra260817_source_verification_mode"]
            )
            self.assertFalse(plan["job_submitted"])
            offline_specs = [
                row
                for row in plan["copy_specs"]
                if "offline_data" in Path(row["source"]).parts
            ]
            self.assertEqual(1, len(offline_specs))
            self.assertTrue(offline_specs[0]["compact_offline_guide"])

    def test_submit_plans_only_one_ordered_phase(self) -> None:
        with TemporaryDirectory() as temporary:
            campaign_path = _write_campaign(Path(temporary))
            expected_counts = {"train": 24, "freeze": 1, "eval": 6, "summarize": 1}
            for phase, expected in expected_counts.items():
                plan = build_upper_kara_causal_program_remote_plan_v1(
                    shared_home="/home/tester",
                    run_id="remote-smoke",
                    phase=phase,
                    campaign=campaign_path,
                    bridge_name="bridge.linux-amd64",
                )
                self.assertEqual(expected, plan["task_count"])
                self.assertEqual("PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED", plan["status"])
                self.assertFalse(plan["phase_contract"]["submit_performed"])
                self.assertFalse(plan["phase_contract"]["legacy_independent_48_cell_semantics"])
                self.assertTrue(plan["required_remote_paths"])
                if phase in {"train", "eval"}:
                    self.assertTrue(plan["external_source_code_closure_required"])
                    self.assertIn(
                        "BOC_CONTRA260817_ROOT=",
                        plan["task_specs"][0]["cmd"],
                    )
                if phase == "train":
                    self.assertTrue(plan["compact_offline_guide_required"])
                    self.assertIn("--offline-guide-artifact", plan["task_specs"][0]["cmd"])
                    self.assertEqual(
                        (
                            1
                            + _campaign().generation_config.max_programs * 5
                            + 3
                        ),
                        plan["candidate_count_audit"][
                            "total_training_program_upper_bound"
                        ],
                    )
                    self.assertTrue(
                        plan["candidate_count_audit"][
                            "projection_may_deduplicate"
                        ]
                    )
                    for node_index in range(6):
                        node = f"node{node_index + 1:03d}"
                        assigned = [
                            row
                            for row in plan["task_specs"]
                            if row["preferred_node"] == node
                        ]
                        self.assertEqual(4, len(assigned))
                        self.assertEqual(16, sum(row["cpu"] for row in assigned))
                        self.assertEqual(
                            16_384, sum(row["ram_mb"] for row in assigned)
                        )
                else:
                    self.assertFalse(plan["compact_offline_guide_required"])

    def test_inventory_rejects_queued_done_and_existing_terminal_duplicates(self) -> None:
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Scheduler:
            def __init__(self, tasks, existing):
                self.tasks = tasks
                self.existing = set(existing)
                self.required_batch_calls = 0

            def state_lock(self, **_):
                return Lock()

            def load_state(self):
                return {"tasks": self.tasks}

            def run_on(self, _node, command, **_):
                parts = shlex.split(command)
                if "upper_kara_required_path_batch_v1" in parts[2]:
                    self.required_batch_calls += 1
                    probes = json.loads(parts[3])
                    return (
                        0,
                        json.dumps(
                            [
                                {"index": row["index"], "path": row["path"]}
                                for row in probes
                                if row["path"] not in self.existing
                            ]
                        ),
                        "",
                    )
                if "os.path.exists" in parts[2]:
                    return (
                        0,
                        json.dumps(
                            [path for path in parts[3:] if path in self.existing]
                        ),
                        "",
                    )
                raise AssertionError(f"unexpected remote command: {command}")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = _write_campaign(root)
            plan = build_upper_kara_causal_program_remote_plan_v1(
                shared_home="/home/tester",
                run_id="inventory-smoke",
                phase="train",
                campaign=campaign_path,
                bridge_name="bridge.linux-amd64",
            )
            required = {row["path"] for row in plan["required_remote_paths"]}
            clear_scheduler = Scheduler([], required)
            clear = inventory_upper_kara_causal_program_remote_v1(
                clear_scheduler, plan
            )
            self.assertEqual("READY_TO_QUEUE", clear["status"])
            self.assertEqual(1, clear_scheduler.required_batch_calls)

            expected_missing = dict(plan["required_remote_paths"][-1])
            missing_scheduler = Scheduler(
                [], required - {expected_missing["path"]}
            )
            missing = inventory_upper_kara_causal_program_remote_v1(
                missing_scheduler, plan
            )
            self.assertEqual("NOT_READY_TO_QUEUE", missing["status"])
            self.assertEqual(
                [expected_missing], missing["missing_required_paths"]
            )
            self.assertEqual(1, missing_scheduler.required_batch_calls)

            signature = plan["task_specs"][0]["signature"]
            for status in ("queued", "running", "done"):
                conflict = inventory_upper_kara_causal_program_remote_v1(
                    Scheduler(
                        [
                            {
                                "id": f"task-{status}",
                                "status": status,
                                "signature": signature,
                                "cmd": "true",
                            }
                        ],
                        required,
                    ),
                    plan,
                )
                self.assertEqual("NOT_READY_TO_QUEUE", conflict["status"])
                self.assertEqual(
                    status, conflict["scheduler_task_conflicts"][0]["status"]
                )

            terminal = plan["terminal_outputs"][0]
            artifact = inventory_upper_kara_causal_program_remote_v1(
                Scheduler([], required | {terminal}), plan
            )
            self.assertEqual("NOT_READY_TO_QUEUE", artifact["status"])
            self.assertEqual([terminal], artifact["remote_terminal_conflicts"])

            # The shared escalation helper used by main() must also accept the
            # new plan shape rather than raising for a missing batch prefix.
            escalation = scheduler_escalation_inventory_v1(
                plan, state_directory=root
            )
            self.assertEqual(0, escalation["pending_escalation_count_related"])

    def test_remote_output_inventory_batches_large_campaigns(self) -> None:
        class Scheduler:
            def __init__(self, existing):
                self.existing = set(existing)
                self.batches = []

            def run_on(self, _node, command, **_):
                parts = shlex.split(command)
                paths = parts[3:]
                self.batches.append(paths)
                return (
                    0,
                    json.dumps(
                        [
                            path
                            for path in reversed(paths)
                            if path in self.existing
                        ]
                    ),
                    "",
                )

        for count in (1_024, 2_048):
            with self.subTest(count=count):
                unique_paths = tuple(
                    "/remote/train/"
                    f"shard-{index:04d}/terminal-with-long-name.json"
                    for index in range(count)
                )
                expected = tuple(unique_paths[::137])
                scheduler = Scheduler(expected)
                actual = _remote_existing_paths_v1(
                    scheduler,
                    "node001",
                    remote_python="/remote/python",
                    paths=tuple(reversed(unique_paths)) + unique_paths[:5],
                )
                self.assertEqual(sorted(expected), actual)
                self.assertEqual(
                    count // REMOTE_OUTPUT_PATH_BATCH_SIZE,
                    len(scheduler.batches),
                )
                self.assertEqual(
                    sorted(unique_paths),
                    sorted(path for batch in scheduler.batches for path in batch),
                )
                self.assertLessEqual(
                    max(map(len, scheduler.batches)),
                    REMOTE_OUTPUT_PATH_BATCH_SIZE,
                )

    def test_remote_required_inventory_batches_large_campaigns(self) -> None:
        class Scheduler:
            def __init__(self, missing):
                self.missing = set(missing)
                self.batches = []

            def run_on(self, _node, command, **_):
                probes = json.loads(shlex.split(command)[3])
                self.batches.append(probes)
                return (
                    0,
                    json.dumps(
                        [
                            {"index": row["index"], "path": row["path"]}
                            for row in reversed(probes)
                            if row["index"] in self.missing
                        ]
                    ),
                    "",
                )

        for count in (1_024, 2_048):
            with self.subTest(count=count):
                rows = tuple(
                    {
                        "path": f"/remote/train/shard-{index:04d}.json",
                        "kind": "file",
                    }
                    for index in range(count)
                )
                missing_indices = set(range(0, count, 137))
                scheduler = Scheduler(missing_indices)
                missing = _remote_missing_required_paths_v1(
                    scheduler,
                    "node001",
                    remote_python="/remote/python",
                    rows=rows,
                )
                self.assertEqual(
                    [dict(rows[index]) for index in sorted(missing_indices)],
                    missing,
                )
                self.assertEqual(
                    count // REMOTE_OUTPUT_PATH_BATCH_SIZE,
                    len(scheduler.batches),
                )
                self.assertEqual(
                    list(range(count)),
                    sorted(
                        row["index"]
                        for batch in scheduler.batches
                        for row in batch
                    ),
                )

    def test_remote_output_inventory_stops_on_failed_batch(self) -> None:
        class Scheduler:
            def __init__(self):
                self.calls = 0

            def run_on(self, _node, _command, **_):
                self.calls += 1
                if self.calls == 2:
                    return 1, "", "remote probe failed"
                return 0, "[]", ""

        scheduler = Scheduler()
        paths = tuple(f"/remote/output/{index:04d}.json" for index in range(129))
        with self.assertRaisesRegex(RuntimeError, "failed at batch 1"):
            _remote_existing_paths_v1(
                scheduler,
                "node001",
                remote_python="/remote/python",
                paths=paths,
            )
        self.assertEqual(2, scheduler.calls)

    def test_remote_terminal_projections_batch_large_campaigns(self) -> None:
        class Scheduler:
            def __init__(self, failing):
                self.failing = set(failing)
                self.batches = []

            def run_on(self, _node, command, **_):
                paths = shlex.split(command)[3:]
                self.batches.append(paths)
                return (
                    0,
                    json.dumps(
                        [
                            (
                                {"path": path, "error": "FAILED:test"}
                                if path in self.failing
                                else {
                                    "path": path,
                                    "projection": {"terminal_status": "COMPLETE"},
                                }
                            )
                            for path in reversed(paths)
                        ]
                    ),
                    "",
                )

        for count in (1_024, 2_048):
            with self.subTest(count=count):
                unique_paths = tuple(
                    f"/remote/train/shard-{index:04d}.json"
                    for index in range(count)
                )
                failing = set(unique_paths[::137])
                scheduler = Scheduler(failing)
                projections, failures = _read_remote_terminal_projections_v1(
                    scheduler,
                    "node001",
                    remote_python="/remote/python",
                    paths=tuple(reversed(unique_paths)) + unique_paths[:5],
                )
                self.assertEqual(failing, set(failures))
                self.assertEqual(set(unique_paths) - failing, set(projections))
                self.assertEqual(
                    count // REMOTE_OUTPUT_PATH_BATCH_SIZE,
                    len(scheduler.batches),
                )
                self.assertEqual(
                    sorted(unique_paths),
                    sorted(path for batch in scheduler.batches for path in batch),
                )

    def test_remote_terminal_projections_stop_on_failed_batch(self) -> None:
        class Scheduler:
            def __init__(self):
                self.calls = 0

            def run_on(self, _node, command, **_):
                self.calls += 1
                if self.calls == 2:
                    return 1, "", "remote projection failed"
                return (
                    0,
                    json.dumps(
                        [
                            {"path": path, "projection": {}}
                            for path in shlex.split(command)[3:]
                        ]
                    ),
                    "",
                )

        scheduler = Scheduler()
        with self.assertRaisesRegex(ValueError, "failed at batch 1"):
            _read_remote_terminal_projections_v1(
                scheduler,
                "node001",
                remote_python="/remote/python",
                paths=tuple(
                    f"/remote/train/{index:04d}.json" for index in range(129)
                ),
            )
        self.assertEqual(2, scheduler.calls)

    def test_inventory_requires_semantically_complete_frozen_winner(self) -> None:
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Scheduler:
            def __init__(self, existing, projections):
                self.existing = set(existing)
                self.projections = dict(projections)
                self.semantic_batch_calls = 0
                self.required_batch_calls = 0

            def state_lock(self, **_):
                return Lock()

            def load_state(self):
                return {"tasks": []}

            def run_on(self, _node, command, **_):
                parts = shlex.split(command)
                if "upper_kara_required_path_batch_v1" in parts[2]:
                    self.required_batch_calls += 1
                    probes = json.loads(parts[3])
                    return (
                        0,
                        json.dumps(
                            [
                                {"index": row["index"], "path": row["path"]}
                                for row in probes
                                if row["path"] not in self.existing
                            ]
                        ),
                        "",
                    )
                if "os.path.exists" in parts[2]:
                    return (
                        0,
                        json.dumps(
                            [row for row in parts[3:] if row in self.existing]
                        ),
                        "",
                    )
                if "upper_kara_semantic_projection_batch_v1" in parts[2]:
                    self.semantic_batch_calls += 1
                    return (
                        0,
                        json.dumps(
                            [
                                (
                                    {"path": path, "projection": projection}
                                    if projection is not None
                                    else {
                                        "path": path,
                                        "error": "FileNotFoundError:missing projection",
                                    }
                                )
                                for path in parts[3:]
                                for projection in (self.projections.get(path),)
                            ]
                        ),
                        "",
                    )
                raise AssertionError(f"unexpected remote command: {command}")

        with TemporaryDirectory() as temporary:
            campaign_path = _write_campaign(Path(temporary))
            eval_plan = build_upper_kara_causal_program_remote_plan_v1(
                shared_home="/home/tester",
                run_id="semantic-gate",
                phase="eval",
                campaign=campaign_path,
                bridge_name="bridge.linux-amd64",
            )
            frozen_path = eval_plan["semantic_prerequisites"][0]["path"]
            winner = {
                "loadout_id": BURST_LOADOUT_IDS_V1[0],
                "program_ref": "searched::winner",
                "program_id": "searched::winner",
                "program_key": "full-canonical-program-key",
                "program_origin": ProgramOriginV1.SEARCHED.value,
            }
            frozen = {
                "schema": "upper_kara_causal_program_remote_freeze/v1",
                "terminal_status": TERMINAL_COMPLETE,
                "campaign_id": eval_plan["campaign_id"],
                "build_id": eval_plan["build_id"],
                "campaign_contract": eval_plan["campaign_contract"],
                "winner": winner,
            }
            required = {row["path"] for row in eval_plan["required_remote_paths"]}
            scheduler = Scheduler(required, {frozen_path: frozen})
            ready = inventory_upper_kara_causal_program_remote_v1(
                scheduler, eval_plan
            )
            self.assertEqual("READY_TO_QUEUE", ready["status"])
            self.assertEqual(1, ready["semantic_terminal_projection_count"])
            self.assertEqual(1, scheduler.semantic_batch_calls)
            self.assertEqual(1, scheduler.required_batch_calls)

            failed_frozen = {**frozen, "terminal_status": "FAILED", "winner": None}
            failed_scheduler = Scheduler(required, {frozen_path: failed_frozen})
            blocked = inventory_upper_kara_causal_program_remote_v1(
                failed_scheduler, eval_plan
            )
            self.assertEqual("NOT_READY_TO_QUEUE", blocked["status"])
            self.assertEqual(1, failed_scheduler.semantic_batch_calls)
            self.assertIn(
                "TERMINAL_STATUS_NOT_ALLOWED",
                blocked["semantic_prerequisite_conflicts"][0]["errors"],
            )

            missing_projection_scheduler = Scheduler(required, {})
            missing_projection = inventory_upper_kara_causal_program_remote_v1(
                missing_projection_scheduler, eval_plan
            )
            self.assertEqual("NOT_READY_TO_QUEUE", missing_projection["status"])
            self.assertEqual(1, missing_projection_scheduler.semantic_batch_calls)
            self.assertIn(
                "PROJECTION_FAILED:FileNotFoundError:missing projection",
                missing_projection["semantic_prerequisite_conflicts"][0][
                    "errors"
                ],
            )

    def test_summary_gate_preserves_valid_failed_heldout_terminal(self) -> None:
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Scheduler:
            def __init__(self, existing, projections):
                self.existing = set(existing)
                self.projections = dict(projections)
                self.semantic_batch_calls = 0
                self.required_batch_calls = 0

            def state_lock(self, **_):
                return Lock()

            def load_state(self):
                return {"tasks": []}

            def run_on(self, _node, command, **_):
                parts = shlex.split(command)
                if "upper_kara_required_path_batch_v1" in parts[2]:
                    self.required_batch_calls += 1
                    probes = json.loads(parts[3])
                    return (
                        0,
                        json.dumps(
                            [
                                {"index": row["index"], "path": row["path"]}
                                for row in probes
                                if row["path"] not in self.existing
                            ]
                        ),
                        "",
                    )
                if "os.path.exists" in parts[2]:
                    return (
                        0,
                        json.dumps(
                            [row for row in parts[3:] if row in self.existing]
                        ),
                        "",
                    )
                if "upper_kara_semantic_projection_batch_v1" in parts[2]:
                    self.semantic_batch_calls += 1
                    return (
                        0,
                        json.dumps(
                            [
                                (
                                    {"path": path, "projection": projection}
                                    if projection is not None
                                    else {
                                        "path": path,
                                        "error": "FileNotFoundError:missing projection",
                                    }
                                )
                                for path in parts[3:]
                                for projection in (self.projections.get(path),)
                            ]
                        ),
                        "",
                    )
                raise AssertionError(f"unexpected remote command: {command}")

        with TemporaryDirectory() as temporary:
            campaign_path = _write_campaign(Path(temporary))
            plan = build_upper_kara_causal_program_remote_plan_v1(
                shared_home="/home/tester",
                run_id="summary-semantic-gate",
                phase="summarize",
                campaign=campaign_path,
                bridge_name="bridge.linux-amd64",
            )
            winner = {
                "loadout_id": BURST_LOADOUT_IDS_V1[0],
                "program_ref": "searched::winner",
                "program_id": "searched::winner",
                "program_key": "full-canonical-program-key",
                "program_origin": ProgramOriginV1.SEARCHED.value,
            }
            projections = {}
            for spec in plan["semantic_prerequisites"]:
                common = {
                    "schema": spec["schema"],
                    "terminal_status": (
                        TERMINAL_COMPLETE
                        if spec["role"] == "FROZEN_SEARCHED_WINNER"
                        else "FAILED"
                    ),
                    "campaign_id": plan["campaign_id"],
                    "build_id": plan["build_id"],
                    "campaign_contract": plan["campaign_contract"],
                    **spec["expected_fields"],
                }
                if spec["role"] == "FROZEN_SEARCHED_WINNER":
                    common["winner"] = winner
                else:
                    common.update(
                        {
                            "selected_loadout_id": winner["loadout_id"],
                            "frozen_program_id": winner["program_id"],
                            "frozen_program_key": winner["program_key"],
                            "frozen_program_origin": winner["program_origin"],
                        }
                    )
                projections[spec["path"]] = common
            required = {row["path"] for row in plan["required_remote_paths"]}
            scheduler = Scheduler(required, projections)
            ready = inventory_upper_kara_causal_program_remote_v1(
                scheduler, plan
            )
            self.assertEqual("READY_TO_QUEUE", ready["status"])
            self.assertEqual(1, scheduler.semantic_batch_calls)
            self.assertEqual(1, scheduler.required_batch_calls)

            bad_path = next(
                spec["path"]
                for spec in plan["semantic_prerequisites"]
                if spec["role"] == "EVAL_SHARD"
            )
            projections[bad_path] = {
                **projections[bad_path],
                "campaign_id": "different-campaign",
            }
            scheduler.projections[bad_path] = projections[bad_path]
            blocked = inventory_upper_kara_causal_program_remote_v1(
                scheduler, plan
            )
            self.assertEqual("NOT_READY_TO_QUEUE", blocked["status"])
            self.assertEqual(2, scheduler.semantic_batch_calls)
            self.assertTrue(blocked["semantic_prerequisite_conflicts"])


if __name__ == "__main__":
    unittest.main()
