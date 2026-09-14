from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.branch_teacher_v1 import BranchReplayMismatchV1
from o2o_dps.cat_external_press_action_teacher_v1 import (
    ACTION_OPPORTUNITY_CONTRACT,
    STATE_SELECTION_CONTRACT,
    _action_opportunity_coverage,
    _normalized_paired_delta,
    _same_press_prefix,
    _selected_points,
    _sparse_action_opportunities,
    run_cat_external_press_action_teacher_v1,
)
from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4
from o2o_dps.cat_sparse_guard_policy_v2 import (
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
)
from o2o_dps.cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.expert_policy import SwingQueueOp, WAIT_ACTION
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.factored_cat_branch_router_v1 import _teacher_rows
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from tests.test_cat_external_press_pilot_v1 import StaticPressBridge, TARGET


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v19.exe"


def _policy_state() -> CatFuryFullPolicyStateV4:
    return CatFuryFullPolicyStateV4(combat=FuryExpertState(
        rage=100.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.TWO_HAND,
        mainhand_swing_remaining_s=1.0,
    ))


class FakeWholeWaveBridge(StaticPressBridge):
    """Three physical presses followed by a receipt-backed target death."""

    def __init__(self) -> None:
        super().__init__()
        self.current["dynamic_team_background"] = {
            "simulated_damage_applied": 100.0,
            "background_damage_applied": 900.0,
            "targets": [{
                "target_index": 0,
                "dead": False,
                "simulated_damage_applied": 100.0,
                "background_damage_applied": 900.0,
            }],
        }
        self.branch_credited = False

    def load_dynamic_v3(self, request, seed, config):
        self.commands.append("load_dynamic_v3")
        return SimpleNamespace(state=self._state(), receipt={"seed": seed})

    def actions(self):
        return [
            *super().actions(),
            AvailableAction(
                index=1,
                action=QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
                label="Heroic Strike",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=False,
            ),
            AvailableAction(
                index=2,
                action=ACTION_KEY_TO_REF["warrior.whirlwind"],
                label="Whirlwind",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
            ),
        ]

    def advance(self):
        super().advance()
        if self.current["finished"]:
            self.current["target_health"] = 0
            self.current["target_health_percent"] = 0.0
            row = self.current["dynamic_team_background"]["targets"][0]
            row["dead"] = True
        return self._state()


def _fake_execute(controls, proposal, before, **kwargs):
    del before, kwargs
    bridge = controls._bridge
    if proposal.metadata.get("action_branch_policy") and not bridge.branch_credited:
        bridge.branch_credited = True
        team = bridge.current["dynamic_team_background"]
        team["simulated_damage_applied"] += 10.0
        team["targets"][0]["simulated_damage_applied"] += 10.0
    events = []
    for sink in proposal.raw_sink_order:
        if sink.channel in {"gcd", "swing_queue"}:
            events.append({
                "source_sink": sink.to_dict(),
                "simulator_submission": {"status": "SUBMITTED"},
                "simulator_acceptance": {"status": "ACCEPTED"},
            })
    accepted = [] if proposal.gcd == WAIT_ACTION else [proposal.gcd]
    return {
        "sink_events": events,
        "accepted_gcd_actions": accepted,
        "execution_blocked": False,
        "nonfaithful_reasons": [],
        "final_state": bridge.state(),
    }


class CatExternalPressActionTeacherV1Tests(unittest.TestCase):
    def test_deterministic_float_residue_is_not_a_positive_teacher_label(self):
        self.assertEqual(0.0, _normalized_paired_delta(10.0 + 3.64e-12, 10.0))
        self.assertAlmostEqual(0.01, _normalized_paired_delta(10.01, 10.0))

    @staticmethod
    def _selection_press(
        decision_index, time_ms, kinds, receipts=(), available=(), **combat,
    ):
        if "BT_TO_WW" in kinds:
            base_gcd = "warrior.bloodthirst"
        elif "WW_TO_BT" in kinds or "DEFER_GCD" in kinds:
            base_gcd = "warrior.whirlwind"
        else:
            base_gcd = WAIT_ACTION
        base_queue = (
            SwingQueueOp.HEROIC_STRIKE.value
            if "SUPPRESS_QUEUE" in kinds else SwingQueueOp.KEEP.value
        )
        complete_combat = {
            "gcd_ready": True,
            "target_exists": True,
            "target_health_pct": 50.0,
            "rage": 55.0,
            "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
            "mainhand_swing_remaining_s": 1.0,
            "bloodthirst_ready_in_s": 0.0,
            "whirlwind_ready_in_s": 0.0,
            "flurry_talent": True,
            "flurry_active": False,
            "queued_swing": SwingQueueOp.KEEP.value,
            "casting_slam": False,
        }
        complete_combat.update(combat)
        return {
            "decision_index": decision_index,
            "press_index": decision_index + 1,
            "time_ms": time_ms,
            "available_branch_kinds": list(kinds),
            "expert_state": {
                "combat_elapsed_s": time_ms / 1000.0,
                "combat": complete_combat,
            },
            "available_actions_before": [
                {
                    "action": action.to_wire(),
                    "legal": legal,
                    "ready_in_ms": ready_in_ms,
                    "triggers_gcd": action not in QUEUE_REFS.values(),
                }
                for action, legal, ready_in_ms in available
            ],
            "proposal": {
                "gcd": {"action": base_gcd},
                "swing_queue": base_queue,
            },
            "ordered_execution": {"sink_events": [
                {
                    "source_sink": {"channel": channel},
                    "simulator_submission": {"status": "SUBMITTED"},
                    "simulator_acceptance": {"status": status},
                }
                for channel, status in receipts
            ]},
        }

    def test_only_exact_current_legal_ready_gcd_can_consume_max_states(self):
        whirlwind = ACTION_KEY_TO_REF["warrior.whirlwind"]
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("DEFER_GCD",), (),
                ((whirlwind, True, 100),),
                gcd_ready=False,
            ),
            self._selection_press(
                1, 100, ("DEFER_GCD",), (("gcd", "REJECTED"),),
                ((whirlwind, False, 0),),
                gcd_ready=False,
            ),
            self._selection_press(
                2, 200, ("DEFER_GCD",), (("gcd", "ACCEPTED"),),
                ((whirlwind, True, 0),),
            ),
        ]}
        self.assertEqual(
            [(2, 2, "DEFER_GCD")],
            _selected_points(baseline, max_states=1),
        )

    def test_selection_covers_time_and_branch_structure_with_unique_states(self):
        heroic_strike = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        whirlwind = ACTION_KEY_TO_REF["warrior.whirlwind"]
        bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"]
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("ADD_HS_QUEUE", "DEFER_GCD"),
                (("gcd", "ACCEPTED"),), (
                    (heroic_strike, True, 0),
                    (whirlwind, True, 0),
                ),
                target_health_pct=90.0,
            ),
            self._selection_press(
                1, 100, ("ADD_HS_QUEUE", "DEFER_GCD"),
                (("gcd", "REJECTED"),), ((heroic_strike, True, 100),),
                target_health_pct=90.0,
            ),
            self._selection_press(
                2, 200, ("ADD_HS_QUEUE", "DEFER_GCD"),
                (("gcd", "REJECTED"),), ((heroic_strike, True, 100),),
                target_health_pct=90.0,
            ),
            self._selection_press(
                3, 300, ("SUPPRESS_QUEUE",),
                (("swing_queue", "ACCEPTED"),),
                ((heroic_strike, True, 0),),
                target_health_pct=50.0,
            ),
            self._selection_press(
                4, 400, ("SUPPRESS_QUEUE",),
                (("swing_queue", "REJECTED"),),
                ((heroic_strike, True, 100),),
                target_health_pct=50.0,
            ),
            self._selection_press(
                5, 500, ("SUPPRESS_QUEUE",),
                (("swing_queue", "REJECTED"),),
                ((heroic_strike, False, 0),),
                target_health_pct=50.0,
            ),
            self._selection_press(
                6, 600, ("BT_TO_WW",), (("gcd", "ACCEPTED"),),
                ((whirlwind, True, 0),), target_health_pct=10.0,
            ),
            self._selection_press(
                7, 700, ("BT_TO_WW",), (("gcd", "ACCEPTED"),),
                ((whirlwind, True, 100),), target_health_pct=10.0,
            ),
            self._selection_press(
                8, 800, ("WW_TO_BT",), (("gcd", "ACCEPTED"),),
                ((bloodthirst, True, 0),), target_health_pct=10.0,
            ),
        ]}
        selected = _selected_points(baseline, max_states=4)
        self.assertEqual([0, 3, 6, 8], sorted({row[1] for row in selected}))
        self.assertEqual(4, len({row[1] for row in selected}))
        self.assertEqual(
            {
                "ADD_HS_QUEUE", "DEFER_GCD", "SUPPRESS_QUEUE",
                "BT_TO_WW", "WW_TO_BT",
            },
            {row[2] for row in selected},
        )

        six_state_budget = _selected_points(baseline, max_states=6)
        self.assertEqual(
            [0, 3, 6, 8], sorted({row[1] for row in six_state_budget})
        )
        self.assertEqual(
            {
                "ADD_HS_QUEUE", "DEFER_GCD", "SUPPRESS_QUEUE",
                "BT_TO_WW", "WW_TO_BT",
            },
            {row[2] for row in six_state_budget},
        )

    def test_candidate_action_requires_exact_legal_ready_action_ref(self):
        heroic_strike = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        whirlwind = ACTION_KEY_TO_REF["warrior.whirlwind"]
        bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"]
        accepted_gcd = (("gcd", "ACCEPTED"),)
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("ADD_HS_QUEUE",),
                available=((ActionRef(spell_id=25286), True, 0),),
                target_health_pct=90.0,
            ),
            self._selection_press(
                1, 100, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 100),),
                target_health_pct=90.0,
            ),
            self._selection_press(
                2, 200, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),),
                target_health_pct=90.0,
            ),
            self._selection_press(
                3, 300, ("BT_TO_WW",), accepted_gcd,
                ((whirlwind, True, 100),), target_health_pct=50.0,
            ),
            self._selection_press(
                4, 400, ("BT_TO_WW",), accepted_gcd,
                ((whirlwind, True, 0),), target_health_pct=50.0,
            ),
            self._selection_press(
                5, 500, ("WW_TO_BT",), accepted_gcd,
                ((bloodthirst, False, 0),), target_health_pct=10.0,
            ),
            self._selection_press(
                6, 600, ("WW_TO_BT",), accepted_gcd,
                ((bloodthirst, True, 0),), target_health_pct=10.0,
            ),
        ]}
        selected = _selected_points(baseline, max_states=3)
        self.assertEqual(
            [(2, 2, "ADD_HS_QUEUE"), (4, 4, "BT_TO_WW"), (6, 6, "WW_TO_BT")],
            selected,
        )

    def test_broad_cooldown_offer_not_ready_is_absent_from_teacher_opportunities(self):
        bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"]
        common = {
            "target_health_pct": 50.0,
            "rage": 100.0,
            "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
            "mainhand_swing_remaining_s": 1.0,
            "bloodthirst_ready_in_s": 1.0,
            "whirlwind_ready_in_s": 0.0,
            "flurry_talent": True,
            "flurry_active": False,
            "queued_swing": "KEEP",
            "casting_slam": False,
            "target_exists": True,
        }
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("WW_TO_BT",), available=((bloodthirst, True, 1000),),
                **common,
            ),
            self._selection_press(
                1, 100, ("WW_TO_BT",), available=((bloodthirst, True, 0),),
                **common,
            ),
        ]}

        self.assertEqual([(1, 1, "WW_TO_BT")], _selected_points(baseline, max_states=2))
        self.assertEqual(
            [("WW_TO_BT", 1)],
            [
                (row["kind"], row["decision_index"])
                for row in _sparse_action_opportunities(baseline)
            ],
        )

    def test_same_phase_kind_selects_new_sparse_guard_feature_value(self):
        heroic_strike = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),),
                target_health_pct=50.0, rage=55.0,
            ),
            self._selection_press(
                1, 100, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),),
                target_health_pct=50.0, rage=80.0,
            ),
        ]}

        self.assertEqual(
            [(0, 0, "ADD_HS_QUEUE"), (1, 1, "ADD_HS_QUEUE")],
            _selected_points(baseline, max_states=2),
        )

    def test_selection_is_prefix_stable_and_ignores_outcomes_and_suffix(self):
        heroic_strike = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        whirlwind = ACTION_KEY_TO_REF["warrior.whirlwind"]
        prefix = {"presses": [
            self._selection_press(
                0, 0, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),), target_health_pct=90.0,
            ),
            self._selection_press(
                1, 100, ("DEFER_GCD",), (("gcd", "ACCEPTED"),),
                ((whirlwind, True, 0),),
                target_health_pct=50.0,
            ),
        ], "terminal": {"reward": -999}, "final_state": {"damage_done": 1}}
        extended = deepcopy(prefix)
        extended["terminal"] = {"reward": 999999}
        extended["final_state"] = {"damage_done": 999999}
        extended["presses"].append(self._selection_press(
            2, 200, ("SUPPRESS_QUEUE",), (("swing_queue", "ACCEPTED"),),
            ((heroic_strike, True, 0),),
            target_health_pct=10.0,
        ))
        selected_prefix = _selected_points(prefix, max_states=3)
        selected_extended = _selected_points(extended, max_states=3)
        self.assertEqual(selected_prefix, selected_extended[:len(selected_prefix)])
        self.assertEqual(
            _selected_points(prefix, max_states=2),
            _selected_points(extended, max_states=2),
        )

    def test_action_opportunity_coverage_uses_all_exact_executable_presses(self):
        heroic_strike = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
        combat = {
            "rage": 55.0,
            "mainhand_swing_remaining_s": 1.0,
            "target_health_pct": 50.0,
            "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
        }
        baseline = {"presses": [
            self._selection_press(
                0, 0, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),), **combat,
            ),
            self._selection_press(
                1, 100, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 100),), **combat,
            ),
            self._selection_press(
                2, 200, ("ADD_HS_QUEUE",),
                available=((heroic_strike, True, 0),), **combat,
            ),
            self._selection_press(
                3, 300, ("DEFER_GCD",), (("gcd", "REJECTED"),),
                **combat,
            ),
        ]}

        coverage = _action_opportunity_coverage(baseline)

        self.assertEqual(1, len(coverage))
        self.assertEqual("ADD_HS_QUEUE", coverage[0]["rule"]["kind"])
        self.assertEqual(2, coverage[0]["opportunity_press_count"])
        self.assertEqual(0, coverage[0]["first_decision_index"])
        self.assertEqual(2, coverage[0]["last_decision_index"])
        self.assertEqual(2, coverage[0]["phase_counts"]["MIDDLE"])

    def test_action_opportunity_coverage_is_independent_of_max_states(self):
        _, one = self._run_fake(max_states=1)
        _, six = self._run_fake(max_states=6)

        self.assertEqual(
            one["action_opportunity_coverage"],
            six["action_opportunity_coverage"],
        )
        self.assertEqual(
            one["action_opportunity_press_count"],
            six["action_opportunity_press_count"],
        )
        self.assertEqual(
            ACTION_OPPORTUNITY_CONTRACT, one["action_opportunity_contract"]
        )
        self.assertEqual(
            one["sparse_action_opportunities"],
            six["sparse_action_opportunities"],
        )
        self.assertEqual(
            len(one["sparse_action_opportunities"]),
            one["sparse_action_opportunity_count"],
        )
        self.assertEqual(
            SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
            one["sparse_action_opportunity_contract"],
        )

    def _run_fake(self, *, max_states=2):
        case = build_development_wave_case_v1(2026091401)
        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
        ):
            result = run_cat_external_press_action_teacher_v1(
                case,
                FakeWholeWaveBridge,
                period_ms=100,
                max_states=max_states,
                max_presses=10,
                simulator_inputs=CatFurySimulatorInputsV5(initial_autoattack_active=False),
            )
        return case, result

    def test_same_clock_teacher_runs_one_intervention_per_fresh_whole_wave(self):
        case, result = self._run_fake(max_states=2)
        self.assertEqual("COMPLETE_EXTERNAL_PRESS_TEACHER_NONVOTING", result["status"])
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertEqual(case.dynamic_load.request_sha256, result["request_sha256"])
        self.assertEqual(
            case.dynamic_load.contract_sha256,
            result["dynamic_load_contract_sha256"],
        )
        # Both fake presses expose the same MIDDLE phase/kind cells.  The
        # second state is intentionally not rerun merely to fill the budget.
        self.assertEqual(1, result["selected_state_count"])
        self.assertEqual(2, result["independent_action_branch_count"])
        self.assertEqual(2, result["completed_teacher_label_count"])
        self.assertTrue(all(
            row["strict_single_intervention_verified"] for row in result["branches"]
        ))
        self.assertTrue(all(row["branch_action_accepted"] for row in result["branches"]))
        self.assertTrue(all(
            row["paired_effective_damage_delta"] == 10.0 for row in result["branches"]
        ))
        self.assertTrue(all(
            row["accepted_prefix_presses_verified"] == 0 for row in result["branches"]
        ))
        self.assertTrue(result["adapter_contract"]["directly_compatible"])
        self.assertIs(result["state_selection_contract"], STATE_SELECTION_CONTRACT)
        self.assertTrue(all(
            row["selection_phase"] == "MIDDLE" for row in result["branches"]
        ))
        self.assertEqual(2, result["selected_phase_kind_count"])
        self.assertEqual(
            {
                ("MIDDLE", "SUPPRESS_QUEUE"),
                ("MIDDLE", "DEFER_GCD"),
            },
            {
                (row["phase"], row["kind"])
                for row in result["selected_phase_kind_coverage"]
            },
        )
        self.assertFalse(result["comparison_ready"])
        self.assertFalse(result["voting_eligible"])

    def test_no_live_target_press_is_prefix_not_a_policy_decision(self):
        class DelayedAttackableBridge(FakeWholeWaveBridge):
            def __init__(self):
                super().__init__()
                self.current["num_targets"] = 0
                self.current["dynamic_target_semantics"] = {
                    "targets": [{
                        "target_index": 0, "dead": False, "attackable": False,
                    }],
                }

            def advance(self):
                state = super().advance()
                if self.current["time_ms"] >= 100 and not self.current["finished"]:
                    self.current["num_targets"] = 1
                    self.current["dynamic_target_semantics"]["targets"][0]["attackable"] = True
                return self._state()

        case = build_development_wave_case_v1(2026091404)
        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
        ):
            result = run_cat_external_press_action_teacher_v1(
                case, DelayedAttackableBridge, max_states=1, max_presses=10,
            )
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertTrue(result["branches"])
        self.assertTrue(all(row["decision_index"] == 0 for row in result["branches"]))
        self.assertTrue(all(
            row["accepted_prefix_presses_verified"] == 1 for row in result["branches"]
        ))

    def test_output_projects_directly_into_factored_router_teacher_rows(self):
        case, result = self._run_fake(max_states=1)
        rows = _teacher_rows(case, result)
        self.assertEqual(result["completed_teacher_label_count"], len(rows))
        self.assertTrue(all(row["baseline_status"] == "COMPLETED" for row in rows))
        self.assertTrue(all(row["combat"]["rage"] == 100.0 for row in rows))
        observation = result["branches"][0]["policy_observation"]
        self.assertFalse(observation["seed_visible"])
        self.assertFalse(observation["future_team_schedule_visible"])

    def test_prefix_verifier_rejects_a_prior_physical_press_change(self):
        press = {
            "decision_index": 0,
            "press_index": 1,
            "time_ms": 0,
            "target_index": 0,
            "source_invocation_count": 1,
            "simulator_state_before": {"time_ms": 0},
            "available_actions_before": [],
            "target_semantics": {"target_index": 0},
            "expert_state": {"combat": {"rage": 10}},
            "proposal": {"gcd": {"action": "WAIT"}},
            "ordered_execution": {"sink_events": []},
            "press_closure": "FINISH_PRESS",
            "finish_press_time_ms": 0,
            "finish_press_ready": False,
            "simulator_state_after_press": {"time_ms": 0},
        }
        second = {**deepcopy(press), "decision_index": 1, "press_index": 2, "time_ms": 100}
        baseline = {
            "seed": 7, "period_ms": 100, "mode": "DYNAMIC_V3_WHOLE_WAVE",
            "presses": [press, second],
        }
        alternative = deepcopy(baseline)
        alternative["presses"][0]["proposal"] = {"gcd": {"action": "different"}}
        with self.assertRaisesRegex(BranchReplayMismatchV1, "physical press prefix differs"):
            _same_press_prefix(baseline, alternative, 1)

    def test_max_states_is_a_positive_integer(self):
        case = build_development_wave_case_v1(5)
        with self.assertRaisesRegex(ValueError, "max_states"):
            run_cat_external_press_action_teacher_v1(case, FakeWholeWaveBridge, max_states=0)

    @unittest.skipUnless(os.name == "nt" and BRIDGE.is_file(), "native v19 bridge required")
    def test_native_one_state_teacher_completes_on_the_physical_clock(self):
        case = build_development_wave_case_v1(2026091409)
        result = run_cat_external_press_action_teacher_v1(
            case,
            lambda: V14ProjectedDynamicV3Bridge(
                BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
            ),
            period_ms=100,
            max_states=1,
            max_presses=400,
        )
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertEqual(
            "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
            result["baseline_press_clock_configuration_mode"],
        )
        self.assertEqual(1, result["selected_state_count"])
        self.assertGreaterEqual(result["independent_action_branch_count"], 1)
        self.assertTrue(all(
            row["strict_single_intervention_verified"] for row in result["branches"]
        ))
        self.assertTrue(all(
            row["accepted_prefix_presses_verified"] == row["decision_index"]
            for row in result["branches"]
        ))
        self.assertFalse(result["comparison_ready"])


if __name__ == "__main__":
    unittest.main()
