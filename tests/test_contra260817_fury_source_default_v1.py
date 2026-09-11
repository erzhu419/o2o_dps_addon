from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra260817_fury_source_default_v1 import (
    BERSERKER_RAGE,
    CONCUSSION_BLOW,
    DEMORALIZING_SHOUT,
    FRESH_SOURCE_DEFAULT_PROFILE,
    PREDICTED_SAVEDVARIABLES_SHA256,
    PREDICTED_UPGRADE_POLICY_ID,
    PREDICTED_UPGRADE_PROFILE,
    SOURCE_DEFAULT_POLICY_ID,
    Contra260817FuryMacroCAdapterV1,
    Contra260817FuryMacroCStateV1,
    Contra260817FuryPredictedUpgradeMacroCAdapterV1,
    Contra260817FurySourceDefaultMacroCAdapterV1,
    Contra260817ProfileKindV1,
    OVERPOWER,
)
from o2o_dps.expert_policy import ExpertRole, SwingQueueOp, TargetOp, WAIT_ACTION
from o2o_dps.fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
)


SOURCE_SHA256 = "1" * 64
SIM_SHA256 = "2" * 64


def _source_evidence() -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=SOURCE_SHA256,
    )


def _sim_evidence() -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SIMULATOR_STATE,
        source_sha256=SIM_SHA256,
    )


def _state(
    *,
    classification: ContraTargetClassificationV2 = (
        ContraTargetClassificationV2.WORLDBOSS
    ),
    count: int = 1,
    **combat_changes: object,
) -> Contra260817FuryMacroCStateV1:
    combat = FuryExpertState(
        rage=40.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.DUAL_WIELD,
        target_exists=True,
        target_is_boss=False,  # ignored; classification is authoritative
        has_shield=False,
        target_distance_yards=3.0,
        bloodthirst_ready_in_s=0.0,
        whirlwind_ready_in_s=0.0,
        contra_st_s=1.0,
        contra_ss_s=0.2,
        contra_sd_s=2.6,
    )
    if combat_changes:
        combat = replace(combat, **combat_changes)
    return Contra260817FuryMacroCStateV1(
        combat=combat,
        authorization_gate_passed=True,
        attackable_units_within_five_yards=count,
        current_target_within_five_yards=True,
        target_classification=classification,
        heroic_strike_actionbar_present=True,
        cleave_actionbar_present=True,
        combat_evidence=_sim_evidence(),
        authorization_evidence=_source_evidence(),
        encounter_evidence=_sim_evidence(),
        target_classification_evidence=_sim_evidence(),
        actionbar_evidence=_source_evidence(),
        support_state_evidence=_sim_evidence(),
        has_battle_shout=True,
        bloodrage_cooldown_remaining_s=10.0,
        has_enrage_buff=False,
        fear_aura_active=False,
        has_demoralizing_shout_on_target=True,
    )


def _body_gcd_values(decision: object) -> list[str | None]:
    return [
        sink.value
        for sink in decision.raw_sink_order  # type: ignore[attr-defined]
        if sink.channel == "gcd"
        and sink.source_ref is not None
        and any(
            marker in sink.source_ref
            for marker in (":1269", ":1273", ":1277", ":1281", ":1285", ":1300", ":1304", ":1308", ":1312")
        )
    ]


class Contra260817FurySourceDefaultV1Tests(unittest.TestCase):
    def test_fresh_profile_identity_is_source_default_not_runtime_profile(self) -> None:
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(
            _state()
        )

        self.assertTrue(decision.valid)
        self.assertEqual(decision.expert_id, SOURCE_DEFAULT_POLICY_ID)
        self.assertEqual(decision.provenance.role, ExpertRole.CANDIDATE)
        self.assertFalse(decision.eligible_for_independent_vote)
        self.assertFalse(decision.metadata["comparison_ready"])
        self.assertTrue(decision.metadata["development_sensitivity_only"])
        self.assertEqual(
            decision.metadata["evaluation_scope"], "DEVELOPMENT_SENSITIVITY"
        )
        self.assertEqual(decision.metadata["voting_status"], "NONVOTING")
        self.assertFalse(decision.metadata["runtime_profile_observed"])
        self.assertFalse(decision.metadata["source_execution"])
        self.assertIn(
            "source_defaults_are_not_user_runtime_profile",
            decision.metadata["blockers"],
        )
        self.assertIn(
            "runtime_ContraDB_profile_unknown", decision.metadata["blockers"]
        )
        profile = decision.metadata["profile"]
        self.assertEqual(profile, FRESH_SOURCE_DEFAULT_PROFILE.to_dict())
        self.assertEqual(profile["kind"], "fresh_source_default")
        self.assertEqual(
            profile["buttons"],
            {
                "autoselect": True,
                "xuanfeng": True,
                "Burst": False,
                "Survive": False,
                "interrupt": False,
                "Sunder": False,
                "nuhou": True,
                "yazhi": True,
                "xuexing": True,
                "kuangbao": True,
                "zhendang": True,
                "leiting": True,
                "cuozhi": True,
            },
        )
        self.assertEqual(decision.target, TargetOp.AUTO_SWITCH)
        self.assertEqual(
            [(sink.channel, sink.operation) for sink in decision.raw_sink_order[:2]],
            [
                ("target", "Contra.SelectNearestTarget"),
                ("autoattack", "Contra.StartAttack"),
            ],
        )
        self.assertEqual(decision.gcd, BLOODTHIRST)
        self.assertEqual(
            decision.metadata["source_helper_returns"]["ZS_POJIA"]["values"],
            [False, False],
        )

    def test_fresh_support_sinks_keep_lua_order_before_stance_and_body(self) -> None:
        state = replace(
            _state(rage=10.0, contra_ss_s=1.0),
            has_battle_shout=False,
            bloodrage_cooldown_remaining_s=0.0,
            has_enrage_buff=False,
            fear_aura_active=True,
            has_demoralizing_shout_on_target=False,
        )
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(state)

        self.assertTrue(decision.valid)
        ordered = [
            (sink.channel, sink.value)
            for sink in decision.raw_sink_order
        ]
        self.assertEqual(
            ordered,
            [
                ("target", "AUTO_SWITCH"),
                ("autoattack", "START"),
                ("gcd", "战斗怒吼"),
                ("gcd", "压制"),
                ("off_gcd", "血性狂暴"),
                ("off_gcd", "狂暴之怒"),
                ("gcd", "震荡猛击"),
                ("gcd", "挫志怒吼"),
                ("stance", "狂暴姿态"),
            ],
        )
        self.assertEqual(
            decision.metadata["raw_gcd_calls"],
            [BATTLE_SHOUT, OVERPOWER, CONCUSSION_BLOW, DEMORALIZING_SHOUT],
        )
        self.assertEqual(decision.off_gcd, (BLOODRAGE, BERSERKER_RAGE))
        self.assertEqual(decision.gcd, DEMORALIZING_SHOUT)

    def test_concussion_source_attempt_is_preserved_as_static_noop(self) -> None:
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(
            _state(contra_ss_s=1.0)
        )

        self.assertTrue(decision.valid)
        attempts = decision.metadata["known_noop_source_gcd_attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["action"], CONCUSSION_BLOW)
        self.assertIn("cannot_know_concussion_blow", attempts[0]["reason"])
        self.assertIn(
            ("gcd", "CastSpellByName", "震荡猛击"),
            [
                (sink.channel, sink.operation, sink.value)
                for sink in decision.raw_sink_order
            ],
        )
        self.assertEqual(decision.gcd, WAIT_ACTION)

    def test_single_a_major_branches_preserve_source_order(self) -> None:
        cases = (
            (
                "nonboss_execute_attempt_ignores_rage",
                _state(
                    classification=ContraTargetClassificationV2.ELITE,
                    target_health_pct=20.0,
                    rage=0.0,
                    contra_ss_s=1.0,
                ),
                ["斩杀"],
                EXECUTE,
                SwingQueueOp.KEEP,
            ),
            (
                "heroic_strike_only",
                _state(rage=60.0, contra_ss_s=1.0),
                [],
                WAIT_ACTION,
                SwingQueueOp.HEROIC_STRIKE,
            ),
            (
                "bloodthirst_attempt",
                _state(rage=30.0, contra_ss_s=0.8),
                ["嗜血"],
                BLOODTHIRST,
                SwingQueueOp.KEEP,
            ),
            (
                "bloodthirst_then_whirlwind",
                _state(
                    rage=40.0,
                    contra_ss_s=0.8,
                    bloodthirst_ready_in_s=2.0,
                ),
                ["嗜血", "旋风斩"],
                WHIRLWIND,
                SwingQueueOp.KEEP,
            ),
            (
                "same_macro_multi_sink_final_hamstring",
                _state(
                    rage=70.0,
                    contra_ss_s=0.8,
                    bloodthirst_ready_in_s=2.0,
                    whirlwind_ready_in_s=2.0,
                ),
                ["嗜血", "旋风斩", "断筋"],
                HAMSTRING,
                SwingQueueOp.HEROIC_STRIKE,
            ),
        )
        adapter = Contra260817FurySourceDefaultMacroCAdapterV1()
        for label, state, expected_values, expected_gcd, expected_queue in cases:
            with self.subTest(label=label):
                decision = adapter.propose(state)
                self.assertTrue(decision.valid)
                self.assertEqual(decision.metadata["route"], "SINGLE_A")
                self.assertEqual(_body_gcd_values(decision), expected_values)
                self.assertEqual(decision.gcd, expected_gcd)
                self.assertEqual(decision.swing_queue, expected_queue)

    def test_multi_b_keeps_execute_whirlwind_cleave_bloodthirst_order(self) -> None:
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(
            _state(
                count=2,
                target_health_pct=19.0,
                rage=43.0,
                contra_st_s=0.6,
                contra_ss_s=1.0,
                bloodthirst_ready_in_s=2.0,
                whirlwind_ready_in_s=2.0,
            )
        )

        self.assertTrue(decision.valid)
        self.assertEqual(decision.metadata["route"], "MULTI_B")
        self.assertEqual(
            _body_gcd_values(decision), ["斩杀", "旋风斩", "嗜血"]
        )
        body_order = [
            (sink.channel, sink.value)
            for sink in decision.raw_sink_order
            if sink.source_ref is not None
            and any(
                marker in sink.source_ref
                for marker in (":1300", ":1304", ":1308", ":1312")
            )
        ]
        self.assertEqual(
            body_order,
            [
                ("gcd", "斩杀"),
                ("gcd", "旋风斩"),
                ("swing_queue", "顺劈斩"),
                ("gcd", "嗜血"),
            ],
        )
        self.assertEqual(decision.gcd, BLOODTHIRST)
        self.assertEqual(decision.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(
            decision.metadata["source_helper_returns"]["ZS_POJIA"],
            {
                "call_count": 1,
                "values": [False],
                "reason": "profile_Sunder_false",
                "continues_to_body": True,
            },
        )

    def test_multi_b_tauren_or_arm_is_not_gated_by_xuanfeng(self) -> None:
        adapter = Contra260817FuryPredictedUpgradeMacroCAdapterV1()
        tauren = adapter.propose(
            _state(
                count=2,
                rage=0.0,
                contra_ss_s=1.0,
                race_is_tauren=True,
                target_distance_yards=5.0,
            )
        )
        non_tauren = adapter.propose(
            _state(
                count=2,
                rage=0.0,
                contra_ss_s=1.0,
                race_is_tauren=False,
                target_distance_yards=5.0,
            )
        )

        self.assertFalse(adapter.profile.xuanfeng)
        self.assertEqual(_body_gcd_values(tauren), ["旋风斩"])
        self.assertEqual(tauren.gcd, WHIRLWIND)
        self.assertEqual(_body_gcd_values(non_tauren), [])
        self.assertEqual(non_tauren.gcd, WAIT_ACTION)
        self.assertTrue(tauren.metadata["or_precedence_preserved"])

    def test_macro_c_route_and_zero_count_target_fallback_are_exact(self) -> None:
        adapter = Contra260817FurySourceDefaultMacroCAdapterV1()
        cases = (
            (0, True, "SINGLE_A", 1, True),
            (0, False, "SINGLE_A", 0, False),
            (1, True, "SINGLE_A", 1, False),
            (2, True, "MULTI_B", 2, False),
        )
        for count, within, route, effective, fallback in cases:
            with self.subTest(count=count, within=within):
                state = replace(
                    _state(count=count, contra_ss_s=1.0),
                    current_target_within_five_yards=within,
                )
                decision = adapter.propose(state)
                self.assertEqual(decision.metadata["route"], route)
                self.assertEqual(
                    decision.metadata["effective_nearby_count"], effective
                )
                self.assertEqual(
                    decision.metadata["count_zero_target_fallback_applied"],
                    fallback,
                )

    def test_multi_b_bloodthirst_keeps_middle_high_rage_whirlwind_window(self) -> None:
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(
            _state(
                count=2,
                rage=89.0,
                target_health_pct=50.0,
                whirlwind_ready_in_s=1.0,
                target_distance_yards=20.0,
                contra_ss_s=1.0,
            )
        )

        self.assertIn("嗜血", _body_gcd_values(decision))
        self.assertEqual(decision.gcd, BLOODTHIRST)

    def test_actionbar_helper_no_sink_semantics_are_not_invented(self) -> None:
        state = replace(
            _state(rage=60.0, contra_ss_s=1.0),
            heroic_strike_actionbar_present=False,
        )
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(state)

        self.assertTrue(decision.valid)
        self.assertEqual(decision.swing_queue, SwingQueueOp.KEEP)
        self.assertEqual(len(decision.metadata["helper_no_sink_attempts"]), 1)
        self.assertEqual(
            decision.metadata["helper_no_sink_attempts"][0]["reason"],
            "required_actionbar_texture_not_found",
        )

    def test_name_and_guild_false_returns_before_unreached_missing_inputs(self) -> None:
        state = replace(
            _state(),
            authorization_gate_passed=False,
            attackable_units_within_five_yards=None,
            current_target_within_five_yards=None,
            target_classification=None,
            heroic_strike_actionbar_present=None,
            cleave_actionbar_present=None,
            combat_evidence=None,
            encounter_evidence=None,
            target_classification_evidence=None,
            actionbar_evidence=None,
            support_state_evidence=None,
            has_battle_shout=None,
            bloodrage_cooldown_remaining_s=None,
            has_enrage_buff=None,
            fear_aura_active=None,
            has_demoralizing_shout_on_target=None,
        )
        decision = Contra260817FurySourceDefaultMacroCAdapterV1().propose(state)

        self.assertTrue(decision.valid)
        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(decision.raw_sink_order, ())
        self.assertEqual(decision.metadata["route"], "AUTHORIZATION_RETURN")
        self.assertFalse(decision.eligible_for_independent_vote)

    def test_every_reached_branch_input_fails_closed_without_any_sink(self) -> None:
        base = _state()
        mutations = {
            "wrapper": base.combat,
            "authorization": replace(base, authorization_gate_passed=None),
            "authorization_evidence": replace(base, authorization_evidence=None),
            "count": replace(base, attackable_units_within_five_yards=None),
            "target_range": replace(base, current_target_within_five_yards=None),
            "classification": replace(base, target_classification=None),
            "actionbar": replace(base, heroic_strike_actionbar_present=None),
            "combat_evidence": replace(base, combat_evidence=None),
            "encounter_evidence": replace(base, encounter_evidence=None),
            "classification_evidence": replace(
                base, target_classification_evidence=None
            ),
            "actionbar_evidence": replace(base, actionbar_evidence=None),
            "support_evidence": replace(base, support_state_evidence=None),
            "support_value": replace(base, fear_aura_active=None),
            "unsafe_default_sd": replace(
                base, combat=replace(base.combat, contra_sd_s=0.0)
            ),
            "wrong_weapon": replace(
                base,
                combat=replace(base.combat, weapon_mode=WeaponMode.TWO_HAND),
            ),
        }
        adapter = Contra260817FurySourceDefaultMacroCAdapterV1()
        for label, state in mutations.items():
            with self.subTest(label=label):
                decision = adapter.propose(state)  # type: ignore[arg-type]
                self.assertFalse(decision.valid)
                self.assertEqual(decision.raw_sink_order, ())
                self.assertFalse(decision.eligible_for_independent_vote)
                self.assertTrue(decision.metadata["fail_closed"])
                self.assertTrue(decision.metadata["input_errors"])

    def test_predicted_upgrade_profile_is_separate_and_never_runtime_proof(self) -> None:
        state = replace(
            _state(contra_ss_s=1.0),
            support_state_evidence=None,
            has_battle_shout=None,
            bloodrage_cooldown_remaining_s=None,
            has_enrage_buff=None,
            fear_aura_active=None,
            has_demoralizing_shout_on_target=None,
        )
        decision = Contra260817FuryPredictedUpgradeMacroCAdapterV1().propose(
            state
        )

        self.assertTrue(decision.valid)
        self.assertEqual(decision.expert_id, PREDICTED_UPGRADE_POLICY_ID)
        self.assertEqual(decision.metadata["profile"], PREDICTED_UPGRADE_PROFILE.to_dict())
        self.assertEqual(
            decision.metadata["profile"]["evidence_sha256"][0],
            PREDICTED_SAVEDVARIABLES_SHA256,
        )
        self.assertFalse(decision.metadata["profile"]["runtime_profile_observed"])
        self.assertIn(
            "upgrade_profile_is_prediction_not_post_load_observation",
            decision.metadata["blockers"],
        )
        self.assertNotIn(
            "Contra.SelectNearestTarget",
            [sink.operation for sink in decision.raw_sink_order],
        )

    def test_generic_adapter_accepts_only_the_two_pinned_profiles(self) -> None:
        fresh = Contra260817FuryMacroCAdapterV1(
            Contra260817ProfileKindV1.FRESH_SOURCE_DEFAULT
        )
        predicted = Contra260817FuryMacroCAdapterV1(
            "predicted_upgrade_preserve_current_buttons"
        )
        self.assertEqual(fresh.expert_id, SOURCE_DEFAULT_POLICY_ID)
        self.assertEqual(predicted.expert_id, PREDICTED_UPGRADE_POLICY_ID)
        with self.assertRaisesRegex(ValueError, "unsupported Contra260817 profile"):
            Contra260817FuryMacroCAdapterV1("user_runtime_guess")


if __name__ == "__main__":
    unittest.main()
