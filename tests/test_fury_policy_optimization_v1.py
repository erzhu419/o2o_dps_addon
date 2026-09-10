from __future__ import annotations

import unittest
from unittest.mock import patch

from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_expert_adapters import FuryExpertState
from o2o_dps.fury_policy_optimization_v1 import (
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    PolicyScenario,
    optimize_fury_policy,
    scenarios_from_catalog,
)


def _request(*, target_count: int = 1) -> dict[str, object]:
    return {
        "raid": {"parties": [{"players": [{"distanceFromTarget": 3}]}]},
        "encounter": {
            "duration": 30,
            "targets": [
                {"level": 63, "name": f"target-{index}"}
                for index in range(target_count)
            ],
        },
        "simOptions": {"iterations": 1, "interactive": True},
    }


class FuryTunedPolicyAdapterTests(unittest.TestCase):
    def test_multi_target_queues_cleave_and_prioritizes_whirlwind(self) -> None:
        adapter = FuryTunedPolicyAdapter(
            FuryPolicyParameters(
                heroic_strike_threshold=35,
                cleave_threshold=40,
                multi_target_priority="WHIRLWIND_FIRST",
                use_death_wish=False,
            )
        )
        proposal = adapter.propose(
            FuryExpertState(
                rage=100,
                target_health_pct=100,
                nearby_enemies=3,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=0,
                queued_swing=SwingQueueOp.KEEP,
            )
        )
        self.assertEqual(proposal.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(proposal.gcd, "warrior.whirlwind")

    def test_execute_phase_cancels_queue_and_executes(self) -> None:
        adapter = FuryTunedPolicyAdapter(
            FuryPolicyParameters(
                execute_mode="EXECUTE_FIRST",
                use_death_wish=False,
            )
        )
        proposal = adapter.propose(
            FuryExpertState(
                rage=40,
                target_health_pct=10,
                queued_swing=SwingQueueOp.HEROIC_STRIKE,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=3,
            )
        )
        self.assertEqual(proposal.swing_queue, SwingQueueOp.CANCEL)
        self.assertEqual(proposal.gcd, "warrior.execute")

    def test_reserve_window_prevents_low_rage_queue(self) -> None:
        adapter = FuryTunedPolicyAdapter(
            FuryPolicyParameters(
                heroic_strike_threshold=35,
                reserve_window_s=1.3,
                use_death_wish=False,
            )
        )
        proposal = adapter.propose(
            FuryExpertState(
                rage=44,
                target_health_pct=100,
                bloodthirst_ready_in_s=1.0,
                whirlwind_ready_in_s=5.0,
            )
        )
        self.assertEqual(proposal.swing_queue, SwingQueueOp.KEEP)

    def test_level_conditioned_multi_target_override_is_explicit(self) -> None:
        adapter = FuryTunedPolicyAdapter(
            FuryPolicyParameters(
                cleave_threshold=55,
                multi_target_priority="WHIRLWIND_FIRST",
                low_level_cutoff=60,
                low_level_cleave_threshold=40,
                low_level_multi_target_priority="BLOODTHIRST_FIRST",
                use_death_wish=False,
            )
        )
        low = adapter.propose(
            FuryExpertState(
                rage=50,
                target_health_pct=100,
                target_level=60,
                nearby_enemies=4,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=0,
            )
        )
        high = adapter.propose(
            FuryExpertState(
                rage=50,
                target_health_pct=100,
                target_level=63,
                nearby_enemies=4,
                bloodthirst_ready_in_s=0,
                whirlwind_ready_in_s=0,
            )
        )
        self.assertEqual(low.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(low.gcd, "warrior.bloodthirst")
        self.assertEqual(high.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(high.gcd, "warrior.whirlwind")


class FuryPolicyOptimizationTests(unittest.TestCase):
    def test_scenario_rejects_aggregate_health_mode(self) -> None:
        request = _request()
        request["encounter"]["useHealth"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "duration mode"):
            PolicyScenario("bad", request, 30_000)

    def test_selects_on_train_and_passes_disjoint_validation_gate(self) -> None:
        scenario = PolicyScenario("single", _request(), 30_000)
        candidates = (
            FuryPolicyParameters(heroic_strike_threshold=35, use_death_wish=False),
            FuryPolicyParameters(heroic_strike_threshold=65, use_death_wish=False),
        )

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            if adapter.expert_id == "cat.fury.profile1":
                dps = 100.0
            elif adapter.expert_id == "contra.deployed.fury.raid_a":
                dps = 90.0
            elif adapter.parameters.heroic_strike_threshold == 35:
                dps = 120.0
            else:
                dps = 110.0
            terminal_cap = (
                hasattr(adapter, "parameters")
                and adapter.parameters.heroic_strike_threshold == 35
            )
            return {
                "damage_delta": dps * horizon_ms / 1000.0,
                "dps": dps,
                "decision_count": 10,
                "configured_horizon_complete": True,
                "all_lane_projections_faithful": not terminal_cap,
                "omitted_lane_count": 0,
                "omitted_lane_counts": {},
                "nonfaithful_reason_counts": (
                    {"gcd:wait_capped_to_remaining_horizon": 1}
                    if terminal_cap
                    else {}
                ),
                "source_execution": False,
                "exact_lua_replay": False,
            }

        with patch(
            "o2o_dps.fury_policy_optimization_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            artifact = optimize_fury_policy(
                object(),
                (scenario,),
                training_seeds=(1, 2),
                validation_seeds=(3, 4),
                candidates=candidates,
            )

        self.assertEqual(
            artifact["selected_parameters"]["heroic_strike_threshold"], 35
        )
        self.assertTrue(artifact["simulator_improvement_gate_passed"])
        self.assertEqual(
            artifact["paired_selected_vs_strongest_baseline"]["wins"], 2
        )

    def test_rejects_seed_leakage(self) -> None:
        scenario = PolicyScenario("single", _request(), 30_000)
        with self.assertRaisesRegex(ValueError, "overlap"):
            optimize_fury_policy(
                object(),
                (scenario,),
                training_seeds=(1, 2),
                validation_seeds=(2, 3),
                candidates=(FuryPolicyParameters(),),
            )

    def test_catalog_layout_slice_does_not_double_count_upper_and_lower(self) -> None:
        def row(scenario_id: str, side: str, pile: int) -> dict[str, object]:
            return {
                "scenario_id": scenario_id,
                "source": {"wave_id": "wave-1"},
                "pile": {
                    "layout_side": side,
                    "sensitivity_family": "wave-1",
                    "pile_ordinal": pile,
                },
                "duration": {"observed_span_ms": 10_000},
                "target_hypotheses": {
                    "armor": {"value": 1721},
                    "level": {"value": 60},
                },
                "kill_budget_proxies": [],
                "request": _request(target_count=2 if side == "upper" else 1),
            }

        catalog = {
            "kind": "fury_encounter_scenario_catalog_v1",
            "scenarios": [
                row("upper", "upper", 1),
                row("lower-1", "lower", 1),
                row("lower-2", "lower", 2),
            ],
        }
        upper = scenarios_from_catalog(
            catalog,
            armor_hypothesis=1721,
            level_hypothesis=60,
            layout_side="upper",
        )
        lower = scenarios_from_catalog(
            catalog,
            armor_hypothesis=1721,
            level_hypothesis=60,
            layout_side="lower",
        )
        self.assertEqual([value.scenario_id for value in upper], ["upper"])
        self.assertEqual(len(lower), 2)
        self.assertAlmostEqual(sum(value.weight for value in lower), 1.0)

    def test_upper_and_lower_retain_evidence_bounded_wave_families(self) -> None:
        def row(scenario_id: str, family: str, side: str) -> dict[str, object]:
            return {
                "scenario_id": scenario_id,
                "source": {"wave_id": family},
                "pile": {
                    "layout_side": side,
                    "sensitivity_family": family,
                    "pile_ordinal": 1,
                },
                "duration": {"observed_span_ms": 10_000},
                "target_hypotheses": {
                    "armor": {"value": 1721},
                    "level": {"value": 60},
                },
                "kill_budget_proxies": [],
                "request": _request(target_count=1),
            }

        catalog = {
            "kind": "fury_encounter_scenario_catalog_v1",
            "scenarios": [
                row("uncertain-upper", "wave-1", "upper"),
                row("uncertain-lower", "wave-1", "lower"),
                row("known-single", "wave-2", "evidence_bounded"),
            ],
        }
        upper = scenarios_from_catalog(
            catalog,
            armor_hypothesis=1721,
            level_hypothesis=60,
            layout_side="upper",
        )
        lower = scenarios_from_catalog(
            catalog,
            armor_hypothesis=1721,
            level_hypothesis=60,
            layout_side="lower",
        )
        bounded = scenarios_from_catalog(
            catalog,
            armor_hypothesis=1721,
            level_hypothesis=60,
            layout_side="evidence_bounded",
        )
        self.assertEqual(
            [value.scenario_id for value in upper],
            ["uncertain-upper", "known-single"],
        )
        self.assertEqual(
            [value.scenario_id for value in lower],
            ["uncertain-lower", "known-single"],
        )
        self.assertEqual([value.scenario_id for value in bounded], ["known-single"])


if __name__ == "__main__":
    unittest.main()
