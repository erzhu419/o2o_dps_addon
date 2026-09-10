from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.expert_policy import (
    WAIT_ACTION,
    ExpertRole,
    ProvenanceKind,
    StanceOp,
    SwingQueueOp,
)
from o2o_dps.fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    CAT_FURY_PROFILE1,
    Cat2ProfileAdapter,
    CatFurySourceAdapter,
    ContraDeployedSourceAdapter,
    ContraNewCandidateAdapter,
    CuratedCat2Card,
    FuryExpertState,
    WeaponMode,
)


class FuryExpertAdapterTests(unittest.TestCase):
    def test_cat_profile1_dynamic_dual_wield_queue_uses_strict_reserve(self) -> None:
        adapter = CatFurySourceAdapter()
        state = FuryExpertState(
            rage=85,
            target_health_pct=50,
            weapon_mode=WeaponMode.DUAL_WIELD,
            heroic_strike_cost=15,
            whirlwind_cost=25,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=0,
        )

        at_threshold = adapter.propose(state)
        over_threshold = adapter.propose(replace(state, rage=86))

        self.assertEqual(CAT_FURY_PROFILE1.profile_index, 1)
        self.assertEqual(CAT_FURY_PROFILE1.heroic_strike_mode, "DYNAMIC")
        self.assertEqual(at_threshold.metadata["computed_queue_reserve"], 85)
        self.assertEqual(at_threshold.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(over_threshold.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(over_threshold.gcd, BLOODTHIRST)
        self.assertEqual(
            [sink.channel for sink in over_threshold.raw_sink_order],
            ["autoattack", "swing_queue", "gcd"],
        )
        self.assertEqual(
            over_threshold.provenance.kind, ProvenanceKind.SOURCE_DERIVED
        )
        self.assertTrue(over_threshold.eligible_for_independent_vote)

    def test_cat_profile1_stance_gate_returns_before_queue_and_gcd(self) -> None:
        decision = CatFurySourceAdapter().propose(
            FuryExpertState(
                rage=100,
                target_health_pct=50,
                current_stance=StanceOp.BATTLE,
            )
        )

        self.assertEqual(decision.stance, StanceOp.BERSERKER)
        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(decision.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(
            [sink.channel for sink in decision.raw_sink_order],
            ["autoattack", "stance"],
        )

    def test_deployed_contra_preserves_always_true_last_cast_disjunction(self) -> None:
        decision = ContraDeployedSourceAdapter().propose(
            FuryExpertState(
                rage=0,
                target_health_pct=50,
                weapon_mode=WeaponMode.TWO_HAND,
                last_cast_name="warrior.slam",
                bloodthirst_ready_in_s=5,
                contra_st_s=0,
            )
        )

        gcd_values = [
            sink.value for sink in decision.raw_sink_order if sink.channel == "gcd"
        ]
        self.assertIn("嗜血", gcd_values)
        self.assertEqual(decision.gcd, BLOODTHIRST)
        self.assertTrue(
            decision.metadata["always_true_last_cast_disjunction_preserved"]
        )
        self.assertTrue(
            decision.provenance.authority_files[1].endswith(
                r"Contra\Contra_ALL.lua"
            )
        )

    def test_deployed_contra_dual_wield_heroic_thresholds_are_strict(self) -> None:
        adapter = ContraDeployedSourceAdapter()
        base = FuryExpertState(
            rage=59,
            target_health_pct=50,
            weapon_mode=WeaponMode.DUAL_WIELD,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=0,
            contra_ss_s=2,
            contra_sd_s=3,
        )

        self.assertEqual(adapter.propose(base).swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(
            adapter.propose(replace(base, rage=60)).swing_queue,
            SwingQueueOp.HEROIC_STRIKE,
        )
        self.assertEqual(
            adapter.propose(
                replace(base, rage=48, bloodthirst_ready_in_s=4)
            ).swing_queue,
            SwingQueueOp.HEROIC_STRIKE,
        )
        self.assertEqual(
            adapter.propose(
                replace(base, rage=47, bloodthirst_ready_in_s=4)
            ).swing_queue,
            SwingQueueOp.KEEP,
        )

    def test_deployed_contra_dual_wield_keeps_cooldown_bloodthirst_as_raw_noop(self) -> None:
        adapter = ContraDeployedSourceAdapter()
        state = FuryExpertState(
            rage=60,
            target_health_pct=50,
            weapon_mode=WeaponMode.DUAL_WIELD,
            bloodthirst_ready_in_s=3.6,
            contra_ss_s=0.4,
        )

        decision = adapter.propose(state)

        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(decision.wait_ms, 100)
        self.assertEqual(decision.metadata["raw_gcd_calls"], [BLOODTHIRST])
        self.assertEqual(
            decision.metadata["normalized_gcd_resolution"],
            "last_state_effecting_source_gcd_sink",
        )
        noop = decision.metadata["known_noop_source_gcd_attempts"]
        self.assertEqual(len(noop), 1)
        self.assertEqual(noop[0]["action"], BLOODTHIRST)
        self.assertEqual(noop[0]["operation"], "QueueSpellByName")
        self.assertIn("bloodthirst_on_cooldown", noop[0]["reason"])
        self.assertEqual(
            [sink.value for sink in decision.raw_sink_order if sink.channel == "gcd"],
            ["嗜血"],
        )

        ready = adapter.propose(replace(state, bloodthirst_ready_in_s=0.0))
        self.assertEqual(ready.gcd, BLOODTHIRST)
        self.assertNotIn("known_noop_source_gcd_attempts", ready.metadata)

    def test_contra_new_is_candidate_not_vote_and_keeps_different_execute_cutoff(self) -> None:
        state = FuryExpertState(
            rage=60,
            target_health_pct=20,
            weapon_mode=WeaponMode.DUAL_WIELD,
            target_is_boss=True,
            contra_ss_s=2,
        )
        deployed = ContraDeployedSourceAdapter().propose(state)
        candidate = ContraNewCandidateAdapter().propose(state)

        self.assertEqual(deployed.gcd, EXECUTE)
        self.assertEqual(candidate.gcd, WAIT_ACTION)
        self.assertEqual(candidate.provenance.role, ExpertRole.CANDIDATE)
        self.assertFalse(candidate.eligible_for_independent_vote)
        self.assertTrue(candidate.metadata["candidate_only"])
        self.assertFalse(candidate.metadata["standalone_runnable"])

    def test_cat2_without_profile_is_invalid_but_curated_stack_is_candidate(self) -> None:
        state = FuryExpertState(
            rage=60,
            target_health_pct=50,
            bloodthirst_ready_in_s=0,
        )
        missing = Cat2ProfileAdapter().propose(state)
        curated = Cat2ProfileAdapter(
            (
                CuratedCat2Card(
                    "warrior_heroic_strike", {"rageThreshold": 50}
                ),
                CuratedCat2Card("warrior_bloodthirst"),
            ),
            profile_name="research-fury-v1",
        ).propose(state)

        self.assertFalse(missing.valid)
        self.assertEqual(missing.expert_id, "cat2.fury.profile")
        self.assertEqual(missing.provenance.role, ExpertRole.UNAVAILABLE)
        self.assertIn("no_profile", missing.reason)
        self.assertTrue(curated.valid)
        self.assertEqual(curated.expert_id, "cat2.fury.curated_candidate")
        self.assertEqual(curated.provenance.role, ExpertRole.CANDIDATE)
        self.assertFalse(curated.eligible_for_independent_vote)
        self.assertEqual(curated.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(curated.gcd, BLOODTHIRST)
        self.assertEqual(
            [sink.channel for sink in curated.raw_sink_order],
            ["swing_queue", "gcd"],
        )


if __name__ == "__main__":
    unittest.main()
