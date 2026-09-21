from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    ContinuousTwoWaveRemoteCampaignV1,
    FixedParentQueueGcdBlockSearchV1,
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    campaign_from_dict_v1,
)
from o2o_dps.upper_kara_causal_program_remote_worker_v1 import (
    RemoteCausalProgramRuntimeV1,
    _program_receipt_v1,
    evaluation_terminal_name_v1,
    freeze_training_winner_v1,
    run_evaluation_shard_v1,
    run_training_shard_v1,
    summarize_evaluation_v1,
    train_terminal_name_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_paired_cat_selection_v1 import (
    exact_paired_zero_program_ref_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    TwoWaveExampleV1,
    imported_incumbent_programs_v1,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


BUILD_ID = "clean_dual_weapon_probe"
LOADOUT_ID = "contra_turtle_burst__mighty_rage"
BT = ActionRef(spell_id=23_894)
HS = ActionRef(spell_id=25_286, tag=1)


def _guard(action: ActionRef) -> ObservableCausalGuardV1:
    return ObservableCausalGuardV1(
        action_ready=action,
        false_semantics=SKIP_PLAN,
    )


def _parent_program() -> CausalActionProgramV1:
    cat = imported_incumbent_programs_v1(BUILD_ID)[0]
    return CausalActionProgramV1(
        program_id="frozen-parent-burst",
        selector=ImportedFallbackOverlaySelectorV1(
            terminal_alternatives=(),
            imported_fallback=cat.selector,
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=("fixture:frozen-burst-parent",),
    )


def _composite_program(parent: CausalActionProgramV1) -> CausalActionProgramV1:
    selector = parent.selector
    assert isinstance(selector, ImportedFallbackOverlaySelectorV1)
    return CausalActionProgramV1(
        program_id="searched-burst-plus-queue-gcd-block",
        selector=ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=selector.terminal_alternatives,
            imported_fallback=selector.imported_fallback,
            off_gcd_insertions=selector.off_gcd_insertions,
            insertion_order=selector.insertion_order,
            insertion_position=selector.insertion_position,
            block_alternatives=(
                GuardedAlternativeV1(
                    alternative_id="block:queue:heroic-strike",
                    guard=_guard(HS),
                    decision=ProgramDecisionV1(
                        queue_op=QueueLaneOp.SET,
                        queue_action=HS,
                        wait_ms=100,
                    ),
                ),
                GuardedAlternativeV1(
                    alternative_id="block:gcd:bloodthirst",
                    guard=_guard(BT),
                    decision=ProgramDecisionV1(gcd_action=BT),
                ),
            ),
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=(
            *parent.source_refs,
            "fixture:queue-gcd-block-over-parent",
        ),
    )


def _campaign(parent: CausalActionProgramV1) -> ContinuousTwoWaveRemoteCampaignV1:
    # One arrival stratum with 64 rows gives the frozen gate its predeclared
    # 32 proposal and 32 independent validation pairs.
    train = tuple(TwoWaveExampleV1(seed, 0) for seed in range(1, 65))
    evaluation = tuple(
        TwoWaveExampleV1(seed, 0 if seed < 103 else 7_000)
        for seed in range(101, 105)
    )
    return ContinuousTwoWaveRemoteCampaignV1(
        campaign_id="fixed-parent-v5-test",
        build_id=BUILD_ID,
        train_examples=train,
        evaluation_examples=evaluation,
        generation_config=UpperKaraCausalProgramGenerationConfigV1(
            max_programs=4
        ),
        loadout_ids=(LOADOUT_ID,),
        seed_shard_count=2,
        search_spec=FixedParentQueueGcdBlockSearchV1(
            parent_loadout_id=LOADOUT_ID,
            parent_program=parent,
            max_block_programs=4,
        ),
    )


def _write_campaign(root: Path, campaign: ContinuousTwoWaveRemoteCampaignV1) -> Path:
    path = root / "campaign.json"
    path.write_text(
        json.dumps(campaign.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class _FakeCase:
    def __init__(self, seed: int, loadout_id: str, first_attackable_ms: int) -> None:
        self.dynamic_load = SimpleNamespace(
            request_sha256=hashlib.sha256(
                f"{BUILD_ID}:{loadout_id}".encode("ascii")
            ).hexdigest(),
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
        cases_by_simulator: dict[int, _FakeCase],
        *,
        accept_composite: bool,
    ) -> None:
        self.loadout_id = loadout_id
        self.cases_by_simulator = cases_by_simulator
        self.accept_composite = accept_composite

    def replay(
        self,
        simulator_seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int,
    ) -> ScheduleReplayOutcomeV1:
        self.assert_valid_budget(max_decisions)
        case = self.cases_by_simulator[simulator_seed]
        master_seed = case.case_spec["seed"]
        if program.program_id == "searched-burst-plus-queue-gcd-block":
            # Odd training seeds are the proposal cohort and even seeds are
            # selection validation.  A rejected fixture therefore wins only
            # proposal training and must fall back to its exact parent.
            if self.accept_composite or master_seed >= 100 or master_seed % 2:
                damage = 120.0
            else:
                damage = 99.0
        elif program.program_id == "frozen-parent-burst":
            damage = 100.0
        elif program.program_id.startswith("shared-burst-control::"):
            policy_id = program.program_id.removeprefix("shared-burst-control::")
            raw_ids = [
                row.selector.source_policy_id
                for row in imported_incumbent_programs_v1(BUILD_ID)
            ]
            damage = 85.0 + 5.0 * raw_ids.index(policy_id)
        elif program.origin is ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT:
            raw_ids = [
                row.selector.source_policy_id
                for row in imported_incumbent_programs_v1(BUILD_ID)
            ]
            damage = 60.0 + 5.0 * raw_ids.index(
                program.selector.source_policy_id
            )
        else:
            raise AssertionError(f"unexpected fixture program: {program.program_id}")
        first_attackable_ms = (
            case.dynamic_load.config.attackability_events[0].time_ms
        )
        return ScheduleReplayOutcomeV1(
            seed=simulator_seed,
            status=ReplayStatusV1.COMPLETE,
            state={
                "time_ms": first_attackable_ms + 1_000,
                "dynamic_team_background": {
                    "simulated_damage_applied": damage,
                    "targets": [{"target_index": 0, "dead": True}],
                },
            },
        )

    @staticmethod
    def assert_valid_budget(max_decisions: int) -> None:
        if max_decisions <= 0:
            raise AssertionError("fixture received a nonpositive decision budget")


class _RuntimeProbe:
    def __init__(self) -> None:
        self.case_loadouts: list[str] = []
        self.replay_loadouts: list[str] = []


def _runtime(
    parent: CausalActionProgramV1,
    composite: CausalActionProgramV1,
    *,
    accept_composite: bool,
) -> tuple[RemoteCausalProgramRuntimeV1, _RuntimeProbe]:
    probe = _RuntimeProbe()

    def case_builder(
        seed: int,
        *,
        loadout_id: str,
        pull_time_ms: int,
        first_wave_arrival_ms: int,
        **_: object,
    ) -> _FakeCase:
        probe.case_loadouts.append(loadout_id)
        return _FakeCase(
            seed,
            loadout_id,
            pull_time_ms + first_wave_arrival_ms,
        )

    def generator(*, loadout_id, train_examples, train_cases):
        if loadout_id != LOADOUT_ID:
            raise AssertionError("generator received a different loadout")
        if len(train_examples) != 32 or len(train_cases) != 32:
            raise AssertionError("generator did not receive proposal cohort only")
        return (parent, composite)

    def replay_factory(loadout_id, cases_by_simulator):
        probe.replay_loadouts.append(loadout_id)
        return _FakeReplay(
            loadout_id,
            cases_by_simulator,
            accept_composite=accept_composite,
        )

    return (
        RemoteCausalProgramRuntimeV1(
            case_builder=case_builder,
            searched_program_generator=generator,
            program_replay_factory=replay_factory,
        ),
        probe,
    )


def _train_and_freeze(
    root: Path,
    *,
    accept_composite: bool,
) -> tuple[
    ContinuousTwoWaveRemoteCampaignV1,
    Path,
    Path,
    CausalActionProgramV1,
    CausalActionProgramV1,
    dict,
]:
    parent = _parent_program()
    composite = _composite_program(parent)
    campaign = _campaign(parent)
    campaign_path = _write_campaign(root, campaign)
    training_root = root / "train"
    runtime, _ = _runtime(
        parent,
        composite,
        accept_composite=accept_composite,
    )
    for shard in assign_training_shards_v1(campaign):
        terminal = run_training_shard_v1(
            campaign_path=campaign_path,
            loadout_id=shard.loadout_id,
            seed_shard_index=shard.seed_shard_index,
            output_path=training_root / train_terminal_name_v1(shard),
            replay_workers=4,
            runtime=runtime,
        )
        if terminal["terminal_status"] != TERMINAL_COMPLETE:
            raise AssertionError("fake training shard did not complete")
    frozen_path = root / "frozen.json"
    frozen = freeze_training_winner_v1(
        campaign_path=campaign_path,
        training_root=training_root,
        output_path=frozen_path,
    )
    return (
        campaign,
        campaign_path,
        frozen_path,
        parent,
        composite,
        frozen,
    )


class FixedParentRemoteV5Tests(unittest.TestCase):
    def test_campaign_roundtrip_binds_one_loadout_and_exact_parent(self) -> None:
        parent = _parent_program()
        campaign = _campaign(parent)
        wire = campaign.to_dict()

        self.assertEqual([LOADOUT_ID], wire["loadout_ids"])
        self.assertEqual(parent.to_dict(), wire["search_spec"]["parent_program"])
        self.assertTrue(
            wire["contract"]["fixed_parent_program_is_paired_zero"]
        )
        self.assertEqual(campaign, campaign_from_dict_v1(wire))
        self.assertEqual(2, len(assign_training_shards_v1(campaign)))
        self.assertEqual(
            {LOADOUT_ID},
            {row.loadout_id for row in assign_training_shards_v1(campaign)},
        )

        with self.assertRaisesRegex(ValueError, "only its frozen parent loadout"):
            ContinuousTwoWaveRemoteCampaignV1(
                campaign_id="wrong-loadouts",
                build_id=campaign.build_id,
                train_examples=campaign.train_examples,
                evaluation_examples=campaign.evaluation_examples,
                generation_config=campaign.generation_config,
                loadout_ids=(LOADOUT_ID, "contra_turtle_burst__none"),
                seed_shard_count=campaign.seed_shard_count,
                search_spec=campaign.search_spec,
            )

    def test_generic_paired_zero_resolves_parent_by_full_identity(self) -> None:
        parent = _parent_program()
        composite = _composite_program(parent)
        receipts = (
            _program_receipt_v1(parent),
            _program_receipt_v1(composite),
        )

        self.assertEqual(
            parent.program_id,
            exact_paired_zero_program_ref_v1(parent, receipts),
        )
        lookalike = CausalActionProgramV1(
            program_id=parent.program_id,
            selector=parent.selector,
            origin=parent.origin,
            source_refs=("fixture:different-provenance",),
        )
        with self.assertRaisesRegex(ValueError, "found 0"):
            exact_paired_zero_program_ref_v1(lookalike, receipts)

    def test_training_freeze_accepts_better_composite(self) -> None:
        with TemporaryDirectory() as raw_root:
            (_, _, _, parent, composite, frozen) = _train_and_freeze(
                Path(raw_root),
                accept_composite=True,
            )

        self.assertEqual(TERMINAL_COMPLETE, frozen["terminal_status"])
        self.assertEqual(composite.program_id, frozen["winner"]["program_id"])
        self.assertNotEqual(parent.program_key(), frozen["winner"]["program_key"])
        self.assertEqual(
            "NONZERO_RESIDUAL_ACCEPTED", frozen["selection_gate"]["status"]
        )
        self.assertFalse(
            frozen["selection_gate"]["accepted_program"]["is_zero_residual"]
        )

    def test_training_freeze_falls_back_to_exact_parent(self) -> None:
        with TemporaryDirectory() as raw_root:
            (_, _, _, parent, composite, frozen) = _train_and_freeze(
                Path(raw_root),
                accept_composite=False,
            )

        self.assertEqual(TERMINAL_COMPLETE, frozen["terminal_status"])
        self.assertEqual(parent.program_id, frozen["winner"]["program_id"])
        self.assertNotEqual(composite.program_key(), frozen["winner"]["program_key"])
        self.assertEqual(
            "FALLBACK_ZERO_RESIDUAL_LCB_NOT_POSITIVE",
            frozen["selection_gate"]["status"],
        )
        self.assertTrue(
            frozen["selection_gate"]["accepted_program"]["is_zero_residual"]
        )

    def test_evaluation_and_summary_use_dynamic_six_baseline_panel(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (
                campaign,
                campaign_path,
                frozen_path,
                parent,
                composite,
                frozen,
            ) = _train_and_freeze(root, accept_composite=True)
            runtime, probe = _runtime(
                parent,
                composite,
                accept_composite=True,
            )
            evaluation_root = root / "eval"
            terminals = []
            for shard in assign_evaluation_shards_v1(campaign):
                terminals.append(
                    run_evaluation_shard_v1(
                        campaign_path=campaign_path,
                        frozen_path=frozen_path,
                        seed_shard_index=shard.seed_shard_index,
                        output_path=(
                            evaluation_root / evaluation_terminal_name_v1(shard)
                        ),
                        replay_workers=4,
                        runtime=runtime,
                    )
                )

            raw_ids = [
                row.selector.source_policy_id
                for row in imported_incumbent_programs_v1(BUILD_ID)
            ]
            expected_baselines = [
                *raw_ids,
                *(f"shared_burst::{policy_id}" for policy_id in raw_ids),
            ]
            for terminal, shard in zip(
                terminals,
                assign_evaluation_shards_v1(campaign),
                strict=True,
            ):
                self.assertEqual(TERMINAL_COMPLETE, terminal["terminal_status"])
                self.assertEqual(expected_baselines, terminal["baseline_policy_ids"])
                self.assertEqual(LOADOUT_ID, terminal["selected_loadout_id"])
                self.assertEqual(
                    len(shard.examples) * 7,
                    len(terminal["lanes"]),
                )
                for example in shard.examples:
                    same_seed = [
                        row
                        for row in terminal["lanes"]
                        if row["master_seed"] == example.seed
                    ]
                    self.assertEqual(
                        {"frozen_candidate", *expected_baselines},
                        {row["policy_id"] for row in same_seed},
                    )
                    self.assertEqual(
                        1,
                        len({row["simulator_seed"] for row in same_seed}),
                    )
            self.assertEqual({LOADOUT_ID}, set(probe.case_loadouts))
            self.assertEqual({LOADOUT_ID}, set(probe.replay_loadouts))

            summary = summarize_evaluation_v1(
                campaign_path=campaign_path,
                frozen_path=frozen_path,
                evaluation_root=evaluation_root,
                output_path=root / "summary.json",
            )

        self.assertEqual(TERMINAL_COMPLETE, summary["terminal_status"])
        self.assertEqual(expected_baselines, summary["baseline_policy_ids"])
        self.assertEqual(
            {"frozen_candidate", *expected_baselines},
            set(summary["metric"]["policy_means"]),
        )
        self.assertEqual(
            set(expected_baselines),
            set(
                summary["metric"][
                    "paired_candidate_minus_baseline_damage_statistics"
                ]
            ),
        )
        self.assertTrue(
            summary["metric"][
                "candidate_strictly_better_than_all_native_baselines"
            ]
        )
        self.assertEqual(
            28,
            summary["lane_status_counts"][TERMINAL_COMPLETE],
        )
        self.assertEqual(composite.program_id, frozen["winner"]["program_id"])


if __name__ == "__main__":
    unittest.main()
