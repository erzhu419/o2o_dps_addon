from __future__ import annotations

from pathlib import Path
import unittest

from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1
from o2o_dps.development_precombat_wave_case_v1 import (
    DEATH_WISH_ACTION,
    MIGHTY_RAGE_POTION_ACTION,
)
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_exact_cell_case_v1 import (
    build_upper_kara_exact_cell_case_v1,
    search_cell_from_upper_kara_case_v1,
)
from o2o_dps.upper_kara_exact_train_eval_v1 import (
    CANDIDATE_POLICY_ID,
    run_upper_kara_exact_train_eval_cell_v1,
)
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    WaveActionSequenceSearchResultV1,
)


BT = ActionRef(spell_id=23_894)
GUARD = ObservableCausalGuardV1(
    target_index=0,
    target_hp_pct_lte=100.0,
)
WINNER = ScheduledActionPlan(
    at_or_after_ms=3_000,
    target_index=0,
    off_gcd_actions=(MIGHTY_RAGE_POTION_ACTION,),
    gcd_action=BT,
    guard=GUARD,
    guide_provenance=("cat:test", "contra:test"),
    guide_priority=99.0,
)


def _dead_state(damage: float, elapsed_ms: int = 4_000):
    return {
        "time_ms": elapsed_ms,
        "finished": True,
        "damage_done": damage,
        "dynamic_team_background": {
            "simulated_damage_applied": damage,
            "targets": [{"target_index": 0, "dead": True}],
        },
    }


class _RecordingReplay:
    def __init__(self, *, invalid: bool = False):
        self.calls = []
        self.invalid = invalid

    def replay(self, seed, schedule):
        self.calls.append((seed, tuple(schedule)))
        if self.invalid:
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.INVALID,
                state={"time_ms": 0, "damage_done": 0.0},
                invalid_reason="held-out replay failed",
            )
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=ReplayStatusV1.COMPLETE,
            state=_dead_state(120.0),
        )


class _SearchFactory:
    def __init__(self, *, complete: bool = True):
        self.calls = []
        self.complete = complete

    def __call__(self, replay, cell, **kwargs):
        self.calls.append((replay, cell, kwargs))
        seeds = tuple(kwargs["seeds"])
        if self.complete:
            outcomes = tuple(
                ScheduleReplayOutcomeV1(
                    seed=seed,
                    status=ReplayStatusV1.COMPLETE,
                    state=_dead_state(100.0 + index),
                )
                for index, seed in enumerate(seeds)
            )
            status = "COMPLETE_PAIRED_SEED_SCHEDULE"
            mean_dps = 25.0
        else:
            outcomes = tuple(
                ScheduleReplayOutcomeV1(
                    seed=seed,
                    status=ReplayStatusV1.FRONTIER,
                    state={"time_ms": 1_000, "damage_done": 10.0},
                    available_actions=(
                        AvailableAction(0, BT, "Bloodthirst", True, 0, True),
                    ),
                )
                for seed in seeds
            )
            status = "INCOMPLETE_MAX_STEPS_OR_NO_COMMON_EXPANSION"
            mean_dps = None
        return WaveActionSequenceSearchResultV1(
            cell=cell,
            status=status,
            schedule=(WINNER,),
            outcomes=outcomes,
            mean_dps=mean_dps,
            search_ranking_score=1.0,
            search_ranking_metric="fixture",
            evaluated_schedule_count=7,
            completed_depth=1,
            guide_ids=("cat:test",),
            replay_workers=1,
        )


class _BaselineFactory:
    def __init__(self, *, fail_last: bool = False):
        self.calls = []
        self.fail_last = fail_last

    def __call__(self, master_seed, **kwargs):
        self.calls.append((master_seed, kwargs))
        case = build_upper_kara_exact_cell_case_v1(
            master_seed,
            representative_rank=kwargs["representative_rank"],
            stratum=kwargs["stratum"],
            precombat_self_actions=kwargs["precombat_self_actions"],
            pull_time_ms=kwargs["pull_time_ms"],
            player_consumes=kwargs["player_consumes"],
        )
        simulator_seed = derive_simulator_seed(
            master_seed,
            case.dynamic_load.request_sha256,
            namespace=PROTOCOL_ID,
        )
        executed = DynamicRolloutLoadV3.bind(
            case.request,
            simulator_seed,
            case.dynamic_load.config,
        )
        rows = []
        for index, policy_id in enumerate(BASELINE_IDS):
            failed = self.fail_last and index == 2
            damage = None if failed else float(100 - index * 10)
            rows.append({
                "policy_id": policy_id,
                "role": "BASELINE",
                "status": "FAILED" if failed else "COMPLETED",
                "own_effective_damage": damage,
                "own_effective_dps": None if failed else damage / 4.0,
                "ttk_ms": None if failed else 4_000,
            })
        return {
            "schema": "upper_kara_exact_baseline_cell/v1",
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "search_cell": search_cell_from_upper_kara_case_v1(case).to_dict(),
            "exact_request_sha256": case.dynamic_load.request_sha256,
            "dynamic_load_contract_sha256": executed.contract_sha256,
            "baseline_policy_ids": list(BASELINE_IDS),
            "rows": rows,
        }


def _no_guides(*args, **kwargs):
    return ()


class UpperKaraExactTrainEvalV1Tests(unittest.TestCase):
    def test_train_freeze_then_only_replay_eval_with_exact_four_way_binding(self):
        replay = _RecordingReplay()
        search = _SearchFactory()
        baselines = _BaselineFactory()
        payload = run_upper_kara_exact_train_eval_cell_v1(
            representative_rank=7,
            stratum="q05",
            train_seeds=(101, 103),
            evaluation_seeds=(107, 109),
            bridge_path=Path("bridge.exe"),
            bridge_cwd=Path("sim"),
            precombat_self_actions=(
                MIGHTY_RAGE_POTION_ACTION,
                DEATH_WISH_ACTION,
            ),
            guard_options=(GUARD,),
            max_off_gcd_actions=2,
            search_runner=search,
            baseline_runner=baselines,
            guide_builder=_no_guides,
            replay=replay,
        )

        self.assertEqual(1, len(search.calls))
        search_kwargs = search.calls[0][2]
        self.assertEqual(
            tuple(payload["training"]["simulator_seeds"]),
            tuple(search_kwargs["seeds"]),
        )
        self.assertEqual((GUARD,), search_kwargs["guard_options"])
        self.assertEqual(2, search_kwargs["max_off_gcd_actions"])
        self.assertEqual([107, 109], [call[0] for call in baselines.calls])
        self.assertEqual(2, len(replay.calls))
        self.assertEqual(
            tuple(row["simulator_seed"] for row in payload["evaluation"]["master_to_simulator_seed"]),
            tuple(call[0] for call in replay.calls),
        )
        for _, frozen in replay.calls:
            self.assertEqual(1, len(frozen))
            self.assertEqual((), frozen[0].guide_provenance)
            self.assertEqual(0.0, frozen[0].guide_priority)
            self.assertEqual(GUARD, frozen[0].guard)
            self.assertEqual(0, frozen[0].target_index)
            self.assertEqual((MIGHTY_RAGE_POTION_ACTION,), frozen[0].off_gcd_actions)

        self.assertEqual(
            "COMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY", payload["status"]
        )
        self.assertTrue(
            payload["evaluation"]["summary"]["four_way_conclusion_complete"]
        )
        self.assertEqual(0, payload["evaluation"]["search_or_guide_invocations_on_evaluation_seeds"])
        first = payload["evaluation"]["seed_rows"][0]
        self.assertEqual(
            [CANDIDATE_POLICY_ID, *BASELINE_IDS],
            [row["policy_id"] for row in first["rows"]],
        )
        self.assertEqual(
            20.0,
            first["paired_candidate_minus_baselines"][BASELINE_IDS[0]][
                "own_effective_damage"
            ],
        )
        self.assertEqual(
            first["exact_request_sha256"],
            first["rows"][0]["exact_request_sha256"],
        )
        for row in first["rows"]:
            self.assertEqual(first["simulator_seed"], row["simulator_seed"])
            self.assertEqual(
                first["dynamic_load_contract_sha256"],
                row["dynamic_load_contract_sha256"],
            )
        self.assertFalse(
            payload["claim_boundary"]["training_outcomes_used_for_held_out_winner"]
        )
        self.assertFalse(payload["claim_boundary"]["real_upper_kara_superiority_proven"])

    def test_candidate_or_baseline_failure_stays_null_and_blocks_conclusion(self):
        replay = _RecordingReplay(invalid=True)
        baselines = _BaselineFactory(fail_last=True)
        payload = run_upper_kara_exact_train_eval_cell_v1(
            representative_rank=7,
            stratum="q05",
            train_seeds=(201,),
            evaluation_seeds=(203,),
            search_runner=_SearchFactory(),
            baseline_runner=baselines,
            guide_builder=_no_guides,
            replay=replay,
        )

        row = payload["evaluation"]["seed_rows"][0]
        self.assertFalse(row["four_way_complete"])
        self.assertEqual("FAILED_REPLAY", row["rows"][0]["status"])
        self.assertIsNone(row["rows"][0]["own_effective_damage"])
        self.assertEqual("FAILED", row["rows"][-1]["status"])
        self.assertIsNone(row["rows"][-1]["own_effective_damage"])
        self.assertEqual(100.0, row["rows"][1]["own_effective_damage"])
        for diff in row["paired_candidate_minus_baselines"].values():
            self.assertIsNone(diff["own_effective_damage"])
        summary = payload["evaluation"]["summary"]
        self.assertFalse(summary["four_way_conclusion_complete"])
        self.assertIsNone(summary["ranking_by_mean_own_effective_damage"])
        self.assertIsNone(summary["candidate_strictly_better_than_all_native_baselines"])

    def test_incomplete_training_never_touches_evaluation_seeds(self):
        replay = _RecordingReplay()
        baselines = _BaselineFactory()
        built_seeds = []

        def recording_case_builder(seed, **kwargs):
            built_seeds.append(seed)
            return build_upper_kara_exact_cell_case_v1(seed, **kwargs)

        payload = run_upper_kara_exact_train_eval_cell_v1(
            representative_rank=7,
            stratum="q05",
            train_seeds=(301,),
            evaluation_seeds=(307,),
            search_runner=_SearchFactory(complete=False),
            baseline_runner=baselines,
            guide_builder=_no_guides,
            exact_case_builder=recording_case_builder,
            replay=replay,
        )

        self.assertEqual(
            "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION", payload["status"]
        )
        self.assertFalse(payload["evaluation"]["executed"])
        self.assertEqual([301], built_seeds)
        self.assertEqual([], replay.calls)
        self.assertEqual([], baselines.calls)

    def test_train_and_evaluation_seed_labels_must_be_disjoint(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            run_upper_kara_exact_train_eval_cell_v1(
                representative_rank=7,
                stratum="q05",
                train_seeds=(401, 403),
                evaluation_seeds=(403, 409),
                guide_builder=_no_guides,
                replay=_RecordingReplay(),
            )

    def test_remote_catalog_paths_are_forwarded_to_case_and_baseline_builders(self):
        observed_case_kwargs = []

        def relocated_case_builder(seed, **kwargs):
            observed_case_kwargs.append((seed, dict(kwargs)))
            local_kwargs = dict(kwargs)
            local_kwargs.pop("item_database_path")
            local_kwargs.pop("selector_manifest_path")
            local_kwargs.pop("profile_path_overrides")
            return build_upper_kara_exact_cell_case_v1(seed, **local_kwargs)

        baselines = _BaselineFactory()
        remote_db = Path("/remote/compact/db.json")
        remote_selector = Path("/remote/selector/manifest.json")
        remote_profiles = {"rank_7": "/remote/profiles/rank-7.json.gz"}
        run_upper_kara_exact_train_eval_cell_v1(
            representative_rank=7,
            stratum="q05",
            train_seeds=(501,),
            evaluation_seeds=(503,),
            item_database_path=remote_db,
            selector_manifest_path=remote_selector,
            profile_path_overrides=remote_profiles,
            search_runner=_SearchFactory(),
            baseline_runner=baselines,
            guide_builder=_no_guides,
            exact_case_builder=relocated_case_builder,
            replay=_RecordingReplay(),
        )

        self.assertEqual([501, 503], [seed for seed, _ in observed_case_kwargs])
        for _, row in observed_case_kwargs:
            self.assertEqual(remote_db, row["item_database_path"])
            self.assertEqual(remote_selector, row["selector_manifest_path"])
            self.assertEqual(remote_profiles, row["profile_path_overrides"])
        baseline_kwargs = baselines.calls[0][1]
        self.assertEqual(remote_db, baseline_kwargs["item_database_path"])
        self.assertEqual(remote_selector, baseline_kwargs["selector_manifest_path"])
        self.assertEqual(remote_profiles, baseline_kwargs["profile_path_overrides"])


if __name__ == "__main__":
    unittest.main()
