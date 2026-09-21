from __future__ import annotations

import unittest
import argparse
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.wave_action_sequence_pilot_v1 import (
    DEFAULT_EXPERT_GUIDES,
    _parse_expert_guides,
    _parse_seeds,
    build_expert_action_guides_v1,
    completed_prefix_chronicle_action_history_v1,
    exact_build_identity_v1,
    make_chronicle_offline_context_factory_v1,
    run_wave_action_sequence_pilot_v1,
    search_cell_from_case_v1,
)
from o2o_dps.offline_action_sequence_guide_v1 import SOURCE_BUILD_POOLED
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)
from tests.test_fury_runtime_bound_deployed_contra_adapter_v7 import _binding


class _Audit:
    def to_dict(self):
        return {"schema": "test_guide_audit/v1"}


class _Guide:
    guide_id = "cat:test"

    def audit_snapshot(self):
        return _Audit()


class _SearchResult:
    outcomes = ()

    def to_dict(self):
        return {"status": "INCOMPLETE", "guide_ids": ["cat:test"]}


class WaveActionSequencePilotV1Tests(unittest.TestCase):
    def test_exact_cell_carries_talents_equipment_and_environment(self):
        case = build_development_wave_case_v1(17)
        cell = search_cell_from_case_v1(case)
        identity = exact_build_identity_v1(case)

        self.assertEqual(cell.wave_or_boss_id, case.case_spec["source_wave_ref"])
        self.assertEqual(
            dict(cell.derived_mechanics)["target_base_armor"],
            case.case_spec["initial_state"]["target_base_armor"],
        )
        self.assertEqual(len(cell.equipment), 15)
        self.assertEqual(
            identity["talents_string"],
            case.request["raid"]["parties"][0]["players"][0]["talentsString"],
        )
        self.assertIn("warrior_options", identity)
        self.assertIn("raid_buffs", identity)
        self.assertIn("party_buffs", identity)
        self.assertIn("exact_build_context_json", dict(cell.derived_mechanics))

    def test_seed_parser_is_explicit(self):
        self.assertEqual(_parse_seeds("11, 13,17"), (11, 13, 17))

    def test_expert_guide_parser_supports_heavy_default_and_isolated_smoke(self):
        self.assertEqual(_parse_expert_guides("all"), DEFAULT_EXPERT_GUIDES)
        self.assertEqual(_parse_expert_guides("none"), ())
        self.assertEqual(
            _parse_expert_guides("cat, contra260817, offline"),
            ("cat", "contra260817", "offline"),
        )
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "unknown expert guide"):
            _parse_expert_guides("cat,future_oracle")

    def test_four_guides_use_validated_deployed_runtime_and_pooled_offline(self):
        case = build_development_wave_case_v1(17)
        binding_path = Path("captured-deployed-binding.json")
        with patch(
            "o2o_dps.wave_action_sequence_pilot_v1."
            "load_deployed_contra_runtime_binding_v1",
            return_value=_binding(),
        ) as loader:
            guides = build_expert_action_guides_v1(
                case,
                runtime_binding_path=binding_path,
            )

        loader.assert_called_once_with(binding_path)
        self.assertEqual(4, len(guides))
        self.assertTrue(guides[0].guide_id.startswith("cat:"))
        self.assertTrue(guides[1].guide_id.startswith("deployed_contra:"))
        self.assertTrue(guides[2].guide_id.startswith("contra260817:"))
        self.assertTrue(guides[3].guide_id.startswith("offline_chronicle:"))
        offline_audit = guides[3].audit_snapshot().to_dict()
        self.assertEqual(SOURCE_BUILD_POOLED, offline_audit["source_build_scope"])
        self.assertEqual(
            "OFFLINE_GUIDE_NOT_BASELINE", offline_audit["source_role"]
        )
        self.assertFalse(
            offline_audit["contract"]["guide_is_same_equipment_baseline"]
        )

    def test_offline_history_uses_only_accepted_completed_prefix_actions(self):
        bloodthirst = ActionRef(spell_id=23_894)
        whirlwind = ActionRef(spell_id=1_680)
        heroic_strike_queue = ActionRef(spell_id=25_286, tag=1)
        plan = ScheduledActionPlan(at_or_after_ms=0, gcd_action=bloodthirst)
        outcome = ScheduleReplayOutcomeV1(
            seed=17,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 1_500,
                "damage_done": 100.0,
                "future_action_hint": whirlwind.to_wire(),
            },
            available_actions=(
                AvailableAction(0, whirlwind, "Whirlwind", True, 0, True),
            ),
            receipts=(
                {
                    "step_index": 0,
                    "kind": "ACT_GCD",
                    "action": bloodthirst.to_wire(),
                    "accepted": True,
                },
                {
                    "step_index": 0,
                    "kind": "QUEUE_SET",
                    "action": heroic_strike_queue.to_wire(),
                    "accepted": True,
                },
                {
                    "step_index": 0,
                    "kind": "ACT_OFF_GCD",
                    "action": ActionRef(spell_id=2_687).to_wire(),
                    "accepted": False,
                },
                {
                    "step_index": 1,
                    "kind": "ACT_GCD",
                    "action": whirlwind.to_wire(),
                    "accepted": True,
                },
            ),
        )

        self.assertEqual(
            ("warrior.bloodthirst",),
            completed_prefix_chronicle_action_history_v1(outcome, (plan,)),
        )
        changed_future = replace(
            outcome,
            state={
                "time_ms": 1_500,
                "damage_done": 100.0,
                "future_action_hint": ActionRef(spell_id=20_662).to_wire(),
            },
            receipts=(
                *outcome.receipts[:-1],
                {
                    "step_index": 1,
                    "kind": "ACT_GCD",
                    "action": ActionRef(spell_id=20_662).to_wire(),
                    "accepted": True,
                },
            ),
        )
        self.assertEqual(
            completed_prefix_chronicle_action_history_v1(outcome, (plan,)),
            completed_prefix_chronicle_action_history_v1(
                changed_future, (plan,)
            ),
        )
        case = build_development_wave_case_v1(17)
        context = make_chronicle_offline_context_factory_v1(case)(
            outcome, (plan,)
        )
        self.assertEqual(
            ("warrior.bloodthirst",),
            context.recent_successful_action_history,
        )
        self.assertEqual("MISSING", context.last_auto_attack_elapsed_bucket)

    def test_offline_guide_is_targetless_precombat_safe_and_pooled(self):
        case = build_development_wave_case_v1(17)
        guide = build_expert_action_guides_v1(
            case, guide_names=("offline",)
        )[0]
        potion = ActionRef(item_id=13_442)
        death_wish = ActionRef(spell_id=12_328)
        outcome = ScheduleReplayOutcomeV1(
            seed=17,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 0,
                "damage_done": 0.0,
                "num_targets": 0,
                "precombat": {"active": True},
            },
            available_actions=(
                AvailableAction(0, potion, "Mighty Rage", True, 0, False),
                AvailableAction(1, death_wish, "Death Wish", True, 0, True),
            ),
        )

        priorities = guide.action_priorities(outcome, ())

        self.assertEqual({potion, death_wish}, set(priorities))
        self.assertEqual(0.0, priorities[potion])
        audit = guide.audit_snapshot().to_dict()
        self.assertEqual(SOURCE_BUILD_POOLED, audit["source_build_scope"])
        self.assertEqual(
            "INELIGIBLE_RECEIPT_PRESENT",
            audit["same_build_comparison_eligibility"],
        )
        self.assertFalse(audit["context_contract"]["future_schedule_suffix_used"])
        self.assertTrue(audit["context_contract"]["policy_observable_only"])

    def test_pilot_passes_guides_to_search_and_persists_audit(self):
        guide = _Guide()
        guard = ObservableCausalGuardV1(rage_gte=30)
        with (
            patch(
                "o2o_dps.wave_action_sequence_pilot_v1."
                "build_expert_action_guides_v1",
                return_value=(guide,),
            ),
            patch(
                "o2o_dps.wave_action_sequence_pilot_v1."
                "search_wave_action_sequences_v1",
                return_value=_SearchResult(),
            ) as search,
        ):
            payload = run_wave_action_sequence_pilot_v1(
                seeds=(17,),
                bridge_path=Path("bridge.exe"),
                bridge_cwd=Path("simulator"),
                expert_guide_names=("cat",),
                guard_options=(guard,),
            )

        self.assertEqual((guide,), search.call_args.kwargs["action_guides"])
        self.assertEqual((guard,), search.call_args.kwargs["guard_options"])
        self.assertEqual([guard.to_dict()], payload["guard_options"])
        self.assertEqual(["cat:test"], payload["expert_guides"]["resolved_guide_ids"])
        self.assertEqual(
            "PROPOSAL_ORDER_ONLY_NOT_ACTION_MEMBERSHIP_OR_RUNTIME_FALLBACK",
            payload["expert_guides"]["role"],
        )
        self.assertTrue(
            payload["expert_guides"][
                "all_legal_simulator_actions_remain_searchable"
            ]
        )
        self.assertEqual(
            [{"schema": "test_guide_audit/v1"}],
            payload["expert_guides"]["audit"],
        )

    def test_pilot_exposes_optional_offline_artifact_as_guide_not_baseline(self):
        guide = _Guide()
        artifact = Path("pooled-chronicle-prior.json")
        with (
            patch(
                "o2o_dps.wave_action_sequence_pilot_v1."
                "build_expert_action_guides_v1",
                return_value=(guide,),
            ) as builder,
            patch(
                "o2o_dps.wave_action_sequence_pilot_v1."
                "search_wave_action_sequences_v1",
                return_value=_SearchResult(),
            ),
        ):
            payload = run_wave_action_sequence_pilot_v1(
                seeds=(17,),
                bridge_path=Path("bridge.exe"),
                bridge_cwd=Path("simulator"),
                expert_guide_names=("offline",),
                offline_guide_artifact_path=artifact,
            )

        self.assertEqual(
            artifact,
            builder.call_args.kwargs["offline_guide_artifact_path"],
        )
        offline = payload["expert_guides"]["offline_chronicle"]
        self.assertEqual(SOURCE_BUILD_POOLED, offline["source_build_scope"])
        self.assertEqual(
            "GUIDE_ONLY_NOT_SAME_EQUIPMENT_BASELINE", offline["role"]
        )
        self.assertFalse(
            offline["context_contract"]["future_schedule_suffix_used"]
        )


if __name__ == "__main__":
    unittest.main()
