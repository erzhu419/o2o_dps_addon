from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveSelectorV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_two_wave_baseline_panel_v1 import (
    baseline_policy_ids_for_build_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
    search_cell_from_continuous_two_wave_case_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    TwoWaveExampleV1,
    imported_incumbent_programs_v1,
    run_upper_kara_continuous_two_wave_train_eval_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


def _searched_program(loadout_id: str) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=f"searched::{loadout_id}",
        selector=OrderedGuardSelectorV1(
            alternatives=(),
            fallback=ProgramDecisionV1(wait_ms=100),
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=("fixture-search",),
    )


def _searched_reactive_program(loadout_id: str) -> CausalActionProgramV1:
    program_id = f"searched::{loadout_id}"
    return CausalActionProgramV1(
        program_id=program_id,
        selector=ImportedReactiveSelectorV1(
            binding_id=f"development-two-wave-segment::{program_id}",
            source_policy_id=program_id,
            observation_contract_id="policy_observation_causal_projection/v1",
        ),
        origin=ProgramOriginV1.SEARCHED_REACTIVE,
        source_refs=("development_two_wave_segment_policy/v1",),
    )


def _terminal(seed: int, damage: float) -> ScheduleReplayOutcomeV1:
    return ScheduleReplayOutcomeV1(
        seed=seed,
        status=ReplayStatusV1.COMPLETE,
        state={
            "time_ms": 20_000,
            "dynamic_team_background": {
                "simulated_damage_applied": damage,
                "targets": [
                    {"target_index": 0, "dead": True},
                    {"target_index": 1, "dead": True},
                ],
            },
        },
    )


class _FakeReplay:
    def __init__(self, loadout_id, cases, score, calls, fail=None):
        self.loadout_id = loadout_id
        self.cases = cases
        self.score = score
        self.calls = calls
        self.fail = fail or set()

    def replay(self, seed, program, *, max_decisions):
        self.calls.append((
            "replay",
            self.loadout_id,
            seed,
            program.program_id,
            max_decisions,
        ))
        if (self.loadout_id, seed, program.program_id) in self.fail:
            raise RuntimeError("fixture lane failed")
        return _terminal(seed, self.score(self.loadout_id, program.program_id))


class TwoWaveTrainEvalV1Tests(unittest.TestCase):
    def setUp(self):
        self.build_id = BUILD_IDS[1]
        self.train = (TwoWaveExampleV1(101, 0), TwoWaveExampleV1(103, 7_000))
        self.evaluation = (
            TwoWaveExampleV1(211, 0),
            TwoWaveExampleV1(223, 7_000),
        )

    def _harness(
        self,
        *,
        fail=None,
        baseline_failure=False,
        searched_scores=None,
    ):
        calls = []
        cases = {}

        def case_builder(seed, **kwargs):
            phase = "eval" if seed in {row.seed for row in self.evaluation} else "train"
            calls.append(("case", phase, seed, kwargs["loadout_id"]))
            case = build_upper_kara_continuous_two_wave_burst_case_v1(seed, **kwargs)
            cases[(seed, kwargs["loadout_id"])] = case
            return case

        def generator(**kwargs):
            calls.append((
                "generate",
                kwargs["loadout_id"],
                tuple(row.seed for row in kwargs["train_examples"]),
            ))
            self.assertEqual(
                tuple(row.seed for row in self.train),
                tuple(row.seed for row in kwargs["train_examples"]),
            )
            return (_searched_program(kwargs["loadout_id"]),)

        def score(loadout_id, program_id):
            if program_id.startswith("searched::"):
                return (searched_scores or {
                    "contra_turtle_burst__no_potion": 120.0,
                    "contra_turtle_burst__mighty_rage": 150.0,
                    "contra_turtle_burst__rage": 140.0,
                    "contra_turtle_burst__quickness": 220.0,
                })[loadout_id]
            return 100.0

        def replay_factory(loadout_id, cases_by_simulator):
            calls.append(("factory", loadout_id, tuple(sorted(cases_by_simulator))))
            return _FakeReplay(loadout_id, cases_by_simulator, score, calls, fail)

        def baseline_runner(seed, **kwargs):
            calls.append(("baseline", seed, kwargs["loadout_id"]))
            case = cases[(seed, kwargs["loadout_id"])]
            simulator_seed = derive_simulator_seed(
                seed,
                case.dynamic_load.request_sha256,
                namespace=PROTOCOL_ID,
            )
            executed = DynamicRolloutLoadV3.bind(
                case.request, simulator_seed, case.dynamic_load.config
            )
            policy_ids = baseline_policy_ids_for_build_v1(self.build_id)
            rows = []
            for index, policy_id in enumerate(policy_ids):
                failed = baseline_failure and index == 1
                rows.append({
                    "policy_id": policy_id,
                    "status": "FAILED" if failed else "COMPLETED",
                    "own_effective_damage": None if failed else 130.0 + index,
                    "own_effective_dps": None if failed else 6.5 + index,
                    "ttk_ms": None if failed else 20_000,
                    "error": "fixture baseline failure" if failed else None,
                })
            return {
                "master_seed": seed,
                "simulator_seed": simulator_seed,
                "build_id": self.build_id,
                "loadout_id": kwargs["loadout_id"],
                "first_wave_arrival_ms": kwargs["first_wave_arrival_ms"],
                "search_cell": search_cell_from_continuous_two_wave_case_v1(case).to_dict(),
                "exact_request_sha256": case.dynamic_load.request_sha256,
                "dynamic_load_contract_sha256": executed.contract_sha256,
                "baseline_policy_ids": list(policy_ids),
                "rows": rows,
            }

        return calls, case_builder, generator, replay_factory, baseline_runner

    def test_selects_loadout_and_program_on_train_then_materializes_eval(self):
        calls, case_builder, generator, replay_factory, baseline_runner = self._harness()
        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=replay_factory,
            searched_program_generator=generator,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )
        self.assertEqual(
            "contra_turtle_burst__quickness",
            payload["training"]["winner"]["loadout_id"],
        )
        self.assertEqual(
            "searched::contra_turtle_burst__quickness",
            payload["training"]["winner"]["program_id"],
        )
        self.assertFalse(
            payload["training"]["winner"]["selected_using_evaluation_outcomes"]
        )
        self.assertTrue(payload["contract"]["searched_program_generator_supplied"])
        self.assertEqual(16, len(payload["training"]["rows"]))
        self.assertEqual(2, len(payload["evaluation"]["seed_rows"]))
        self.assertTrue(
            payload["evaluation"]["summary"][
                "candidate_strictly_better_than_all_native_baselines"
            ]
        )
        self.assertFalse(
            payload["contract"]["native_action_program_replay_factory_used"]
        )
        self.assertFalse(
            payload["contract"][
                "same_build_loadout_arrival_request_environment_and_seed_verified"
            ]
        )
        self.assertEqual(512, payload["contract"]["max_decisions_per_replay"])
        self.assertEqual(
            40,
            payload["contract"][
                "minimum_reference_idle_decisions_before_latest_arrival"
            ],
        )
        self.assertTrue(
            payload["contract"][
                "configured_budget_exceeds_reference_idle_requirement"
            ]
        )
        self.assertTrue(
            all(row[4] == 512 for row in calls if row[0] == "replay")
        )
        self.assertEqual(
            {0: {17_000}, 7_000: {10_000}},
            {
                arrival: {
                    seed_row["ttk_ms"]
                    for row in payload["training"]["rows"]
                    for seed_row in row["seed_rows"]
                    if seed_row["first_wave_arrival_ms"] == arrival
                }
                for arrival in (0, 7_000)
            },
        )
        generate_calls = [row for row in calls if row[0] == "generate"]
        self.assertEqual(4, len(generate_calls))
        first_eval_case = next(index for index, row in enumerate(calls) if row[:2] == ("case", "eval"))
        last_generate = max(index for index, row in enumerate(calls) if row[0] == "generate")
        self.assertGreater(first_eval_case, last_generate)
        self.assertEqual(
            {"contra_turtle_burst__quickness"},
            {
                row[3]
                for row in calls
                if row[:2] == ("case", "eval")
            },
        )
        self.assertEqual(
            {row.seed for row in self.evaluation},
            {row[1] for row in calls if row[0] == "baseline"},
        )

    def test_no_generator_still_scores_and_can_select_imported_incumbent(self):
        calls, case_builder, _, replay_factory, baseline_runner = self._harness()
        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=replay_factory,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )
        self.assertFalse(payload["contract"]["searched_program_generator_supplied"])
        self.assertEqual(12, len(payload["training"]["rows"]))
        self.assertTrue(
            payload["training"]["winner"]["program_id"].startswith(
                "imported-incumbent::"
            )
        )

    def test_supplied_search_freezes_searched_candidate_even_below_incumbent(self):
        searched_scores = {loadout_id: 10.0 for loadout_id in BURST_LOADOUT_IDS_V1}
        calls, case_builder, generator, replay_factory, baseline_runner = self._harness(
            searched_scores=searched_scores
        )
        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=replay_factory,
            searched_program_generator=generator,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )
        winner = payload["training"]["winner"]
        self.assertTrue(winner["program_id"].startswith("searched::"))
        self.assertEqual(ProgramOriginV1.SEARCHED.value, winner["program_origin"])
        self.assertTrue(
            payload["contract"]["searched_origin_required_for_candidate"]
        )

    def test_searched_reactive_generator_is_selected_and_wire_frozen(self):
        calls, case_builder, _, replay_factory, baseline_runner = self._harness()

        def generator(**kwargs):
            return (_searched_reactive_program(kwargs["loadout_id"]),)

        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=replay_factory,
            searched_program_generator=generator,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )

        winner = payload["training"]["winner"]
        frozen = payload["training"]["frozen_program"]
        self.assertEqual(
            ProgramOriginV1.SEARCHED_REACTIVE.value,
            winner["program_origin"],
        )
        self.assertEqual(
            winner["program_key"],
            causal_action_program_from_dict_v1(frozen).program_key(),
        )

    def test_default_replay_path_installs_concrete_imported_integration(self):
        _, case_builder, _, replay_factory, baseline_runner = self._harness()
        factory_builder = (
            "o2o_dps.upper_kara_imported_incumbent_program_v1."
            "build_imported_incumbent_program_replay_factory_v1"
        )
        with patch(factory_builder, return_value=replay_factory) as integrated:
            payload = run_upper_kara_continuous_two_wave_train_eval_v1(
                build_id=self.build_id,
                train_examples=self.train,
                evaluation_examples=self.evaluation,
                case_builder=case_builder,
                baseline_runner=baseline_runner,
            )
        integrated.assert_called_once()
        self.assertEqual(self.build_id, integrated.call_args.args[0])
        self.assertTrue(
            payload["contract"]["native_action_program_replay_factory_used"]
        )

    def test_failed_baseline_is_null_and_blocks_four_way_conclusion(self):
        calls, case_builder, generator, replay_factory, baseline_runner = self._harness(
            baseline_failure=True
        )
        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=replay_factory,
            searched_program_generator=generator,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )
        self.assertEqual(
            "INCOMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY", payload["status"]
        )
        failed = payload["evaluation"]["seed_rows"][0]["rows"][2]
        self.assertIsNone(failed["own_effective_damage"])
        self.assertIsNone(
            payload["evaluation"]["seed_rows"][0][
                "paired_candidate_minus_baselines"
            ][failed["policy_id"]]["own_effective_damage"]
        )
        self.assertFalse(payload["contract"]["failure_scored_as_zero"])

    def test_loadout_iteration_order_cannot_change_training_winner(self):
        def run(order):
            _, case_builder, generator, replay_factory, baseline_runner = self._harness()
            return run_upper_kara_continuous_two_wave_train_eval_v1(
                build_id=self.build_id,
                train_examples=self.train,
                evaluation_examples=self.evaluation,
                program_replay_factory=replay_factory,
                searched_program_generator=generator,
                loadout_ids=order,
                case_builder=case_builder,
                baseline_runner=baseline_runner,
            )["training"]["winner"]

        forward = run(BURST_LOADOUT_IDS_V1)
        reverse = run(tuple(reversed(BURST_LOADOUT_IDS_V1)))
        self.assertEqual(forward["loadout_id"], reverse["loadout_id"])
        self.assertEqual(forward["program_key"], reverse["program_key"])

    def test_training_failure_does_not_materialize_held_out_cases(self):
        calls, case_builder, generator, _, baseline_runner = self._harness()

        def invalid_factory(loadout_id, cases_by_simulator):
            class InvalidReplay:
                def replay(self, seed, program, *, max_decisions):
                    raise RuntimeError("all train lanes invalid")
            return InvalidReplay()

        payload = run_upper_kara_continuous_two_wave_train_eval_v1(
            build_id=self.build_id,
            train_examples=self.train,
            evaluation_examples=self.evaluation,
            program_replay_factory=invalid_factory,
            searched_program_generator=generator,
            case_builder=case_builder,
            baseline_runner=baseline_runner,
        )
        self.assertEqual(
            "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION", payload["status"]
        )
        self.assertFalse(payload["evaluation"]["executed"])
        self.assertFalse(any(row[:2] == ("case", "eval") for row in calls))

    def test_examples_require_unique_disjoint_seeds(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            run_upper_kara_continuous_two_wave_train_eval_v1(
                build_id=self.build_id,
                train_examples=(TwoWaveExampleV1(7, 0),),
                evaluation_examples=(TwoWaveExampleV1(7, 7_000),),
                program_replay_factory=lambda *_: None,
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            run_upper_kara_continuous_two_wave_train_eval_v1(
                build_id=self.build_id,
                train_examples=(TwoWaveExampleV1(7, 0), TwoWaveExampleV1(7, 1)),
                evaluation_examples=(TwoWaveExampleV1(9, 0),),
                program_replay_factory=lambda *_: None,
            )

    def test_imported_program_descriptors_match_build_baselines(self):
        programs = imported_incumbent_programs_v1(self.build_id)
        self.assertEqual(3, len(programs))
        self.assertEqual(
            baseline_policy_ids_for_build_v1(self.build_id),
            tuple(program.selector.source_policy_id for program in programs),
        )
        self.assertTrue(all(program.origin is ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT for program in programs))


if __name__ == "__main__":
    unittest.main()
