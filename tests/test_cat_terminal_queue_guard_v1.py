from __future__ import annotations

import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4,
)
from o2o_dps.cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from o2o_dps.cat_terminal_queue_guard_v1 import (
    POLICY_ID,
    CatTerminalQueueGuardV1, TerminalQueueGuardV1,
    run_cat_terminal_queue_guard_v1,
)
from o2o_dps.cat_terminal_queue_guard_lane_v1 import (
    PRODUCER, cat_terminal_queue_guard_lane_contract_v1,
    execute_cat_terminal_queue_guard_lane_v1,
    validate_cat_terminal_queue_guard_artifact_v1,
)
from o2o_dps.deployed_contra_runtime_binding_v1 import load_deployed_contra_runtime_binding_v1
from o2o_dps.development_wave_guard_search_v1 import reduce_guard_panels_v1
from o2o_dps.development_wave_panel_v1 import (
    DEFAULT_BINDING, _candidate, _policies, _source,
    run_development_wave_panel_v1,
)
from o2o_dps.expert_policy import StanceOp, SwingQueueOp
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode
from o2o_dps.fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT, SYNTHETIC_MODE, build_runner_plan,
    runner_scenario_bundle_sha256, validate_lane_result_v4,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import (
    _digest, _execution_bundle, _scenario,
)
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge, rollout_config_v5


def state(*, rage: float = 80.0, hp: float = 50.0, swing: float = 1.0,
          bt: float = 0.0, ww: float = 0.0, casting_slam: bool = False) -> CatFuryFullPolicyStateV4:
    return CatFuryFullPolicyStateV4(combat=FuryExpertState(
        rage=rage, target_health_pct=hp, weapon_mode=WeaponMode.TWO_HAND,
        current_stance=StanceOp.BERSERKER, bloodthirst_ready_in_s=bt,
        whirlwind_ready_in_s=ww, nearby_enemies=1,
        mainhand_swing_remaining_s=swing, casting_slam=casting_slam,
    ))


class CatTerminalQueueGuardV1Tests(unittest.TestCase):
    def test_zero_discount_is_exact_cat_proposal_even_near_execute(self) -> None:
        observation = state(hp=24.0)
        cat = CatFuryFullPolicyAdapterV4().propose(observation)
        candidate = CatTerminalQueueGuardV1(TerminalQueueGuardV1(0.0))
        self.assertEqual(cat, candidate.propose(observation))
        self.assertEqual([], candidate.interventions)
        self.assertEqual([], candidate.guarded_opportunities)

    def test_current_execute_approach_suppresses_only_candidate_hs(self) -> None:
        observation = state(hp=25.0)
        cat = CatFuryFullPolicyAdapterV4().propose(observation)
        candidate = CatTerminalQueueGuardV1(TerminalQueueGuardV1(15.0))
        self.assertEqual(cat, candidate.propose(observation))
        self.assertEqual([], candidate.interventions)
        self.assertEqual("OBSERVED_EXECUTE_APPROACH", candidate.guarded_opportunities[0]["reason"])

    def test_current_slam_proposal_suppresses_candidate_hs(self) -> None:
        observation = state(rage=25.0, hp=60.0, swing=2.5, bt=10.0, ww=10.0)
        cat = CatFuryFullPolicyAdapterV4().propose(observation)
        self.assertTrue(any(row.channel == "gcd" and row.value == "猛击" for row in cat.raw_sink_order))
        candidate = CatTerminalQueueGuardV1(TerminalQueueGuardV1(15.0))
        self.assertEqual(cat, candidate.propose(observation))
        self.assertEqual("CURRENT_SLAM_CONFLICT", candidate.guarded_opportunities[0]["reason"])

    def test_safe_window_changes_queue_and_preserves_order(self) -> None:
        observation = state()
        cat = CatFuryFullPolicyAdapterV4().propose(observation)
        candidate = CatTerminalQueueGuardV1(TerminalQueueGuardV1(15.0))
        proposal = candidate.propose(observation)
        self.assertEqual(SwingQueueOp.KEEP, cat.swing_queue)
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, proposal.swing_queue)
        self.assertEqual("swing_queue", proposal.raw_sink_order[0].channel)
        self.assertEqual(cat.raw_sink_order, proposal.raw_sink_order[1:])
        self.assertEqual(1, len(candidate.interventions))
        self.assertFalse(candidate.guarded_opportunities)

    def test_zero_native_rollout_matches_cat_native_steps(self) -> None:
        request = request_v4()
        seed = 20261009
        dynamic = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
        arguments = dict(seed=seed, target_contexts={0: context_v4()}, dynamic_load=dynamic)
        cat = run_cat_fury_full_policy_rollout_v6(
            _DynamicV3FullBridge(), request, CatFuryFullPolicyAdapterV4(), **arguments
        )
        guarded = run_cat_terminal_queue_guard_v1(
            _DynamicV3FullBridge(), request,
            CatTerminalQueueGuardV1(TerminalQueueGuardV1(0.0)), **arguments,
        )
        self.assertEqual(0, guarded["intervention_count"])
        self.assertEqual(0, guarded["guarded_opportunity_count"])
        for expected, observed in zip(cat["steps"], guarded["steps"], strict=True):
            self.assertEqual("CAT_UNCHANGED", observed["policy_proposal_origin"])
            self.assertEqual(expected, {key: value for key, value in observed.items()
                                        if key != "policy_proposal_origin"})
        self.assertEqual(cat["final_state"], guarded["final_state"])

    def test_guarded_runner_lane_validates_native_sink_receipts(self) -> None:
        scenario = _scenario()
        policy = {
            "policy_id": POLICY_ID, "role": "CANDIDATE",
            "source_sha256": _digest("source:terminal-guard"),
            "adapter_sha256": _digest("adapter:terminal-guard"),
            "profile_sha256": _digest("profile:terminal-guard"),
        }
        plan = build_runner_plan(
            protocol_id="cat-terminal-guard-native-fixture",
            protocol_sha256=_digest("protocol:terminal-guard"),
            phase="development",
            corpus_manifest_sha256=_digest("manifest:terminal-guard"),
            runner_inputs_sha256=_digest("inputs:terminal-guard"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
            corpus_binding_sha256=_digest("binding:terminal-guard"),
            master_seeds=[17], scenarios=[scenario], policies=[policy],
            shard_count=1,
            bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
            execution_bundle_identity=_execution_bundle(),
            execution_mode=SYNTHETIC_MODE,
            seed_namespace="cat-terminal-guard-native-fixture",
            plan_intent=DIAGNOSTIC_INTENT,
            lane_contracts=[cat_terminal_queue_guard_lane_contract_v1()],
        )
        contract = plan["contract"]
        self.assertEqual("READY_FOR_SMALL_FIXTURE", contract["status"])
        bridge = _DynamicV3FullBridge()
        bridge.two_hand = True
        bridge.rage = 80.0
        raw = execute_cat_terminal_queue_guard_lane_v1(
            bridge, CatTerminalQueueGuardV1(TerminalQueueGuardV1()),
            group=contract["groups"][0], scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
        )["lane_result"]
        lane = validate_lane_result_v4(
            raw, group=contract["groups"][0], scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
            artifact_validator=validate_cat_terminal_queue_guard_artifact_v1,
        )
        self.assertEqual(PRODUCER, lane["producer"])
        self.assertEqual(POLICY_ID, lane["policy_id"])
        self.assertGreater(lane["artifact"]["intervention_count"], 0)
        self.assertTrue(lane["offline_score_eligible"])
        self.assertFalse(lane["comparison_ready"])

    def test_full_four_way_wave_panel_binds_guarded_source_identity(self) -> None:
        binding = load_deployed_contra_runtime_binding_v1(DEFAULT_BINDING)
        policy = _policies(
            _candidate(), binding, "cat_terminal_guard", 15.0, 35.0,
        )[-1]
        self.assertEqual(POLICY_ID, policy["policy_id"])
        self.assertEqual(_source("cat_terminal_queue_guard_v1.py"), policy["source_sha256"])
        self.assertEqual(_source("cat_terminal_queue_guard_lane_v1.py"), policy["adapter_sha256"])
        panel = run_development_wave_panel_v1(
            master_seed=20261002, candidate_kind="cat_terminal_guard",
            residual_discount_rage=15.0,
            terminal_guard_execute_approach_health_pct=35.0,
        )
        self.assertTrue(panel["four_way_complete"])
        self.assertEqual("FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY", panel["status"])
        self.assertEqual({POLICY_ID, *BASELINE_IDS}, {row["policy_id"] for row in panel["rows"]})
        self.assertTrue(all(row["status"] == "COMPLETED" for row in panel["rows"]))
        self.assertEqual(15.0, panel["residual_discount_rage"])
        self.assertEqual(35.0, panel["terminal_guard_execute_approach_health_pct"])
        candidate_row = next(row for row in panel["rows"] if row["policy_id"] == POLICY_ID)
        self.assertIsInstance(candidate_row["guard_intervention_count"], int)
        self.assertIsInstance(candidate_row["guard_suppressed_opportunity_count"], int)

    def test_fresh_guard_reducer_blocks_missing_lane(self) -> None:
        panel = run_development_wave_panel_v1(
            master_seed=20261001, candidate_kind="cat_terminal_guard",
            residual_discount_rage=15.0,
            terminal_guard_execute_approach_health_pct=35.0,
        )
        one_seed = reduce_guard_panels_v1(seeds=[20261001], panels={20261001: panel})
        self.assertEqual("SMOKE_ONLY_NO_VERDICT", one_seed["status"])
        self.assertEqual(1, one_seed["complete_paired_seeds"])
        missing = reduce_guard_panels_v1(seeds=[20261001, 20261002], panels={20261001: panel})
        self.assertEqual("INCOMPLETE_NO_VERDICT", missing["status"])
        self.assertEqual({"seed": 20261002, "reason": "NOT_RUN"}, missing["failures"][0])

    def test_fresh_guard_reducer_requires_positive_delta_against_all_three(self) -> None:
        def panel(seed: int, candidate_damage: float) -> dict:
            return {
                "master_seed": seed,
                "candidate_kind": "cat_terminal_guard",
                "residual_discount_rage": 15.0,
                "terminal_guard_execute_approach_health_pct": 35.0,
                "status": "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY",
                "four_way_complete": True,
                "rows": [
                    {"policy_id": policy_id, "status": "COMPLETED",
                     "own_effective_damage": 1000.0}
                    for policy_id in BASELINE_IDS
                ] + [{"policy_id": POLICY_ID, "status": "COMPLETED",
                      "own_effective_damage": candidate_damage,
                      "guard_intervention_count": 1,
                      "guard_suppressed_opportunity_count": 2}],
            }

        seeds = list(range(20261009, 20261041))
        positive = reduce_guard_panels_v1(
            seeds=seeds, panels={seed: panel(seed, 1100.0) for seed in seeds},
        )
        self.assertEqual("POSITIVE_MODEL_DEFINED_DEVELOPMENT_ONLY", positive["status"])
        self.assertTrue(positive["development_gate_passed"])
        self.assertFalse(positive["deployment_authorized"])
        mixed = reduce_guard_panels_v1(
            seeds=seeds, panels={seed: panel(seed, 900.0 if seed % 2 else 1100.0)
                                 for seed in seeds},
        )
        self.assertEqual("NO_CONFIDENT_FOUR_WAY_GAIN_DEVELOPMENT_ONLY", mixed["status"])
        self.assertFalse(mixed["development_gate_passed"])


if __name__ == "__main__":
    unittest.main()
