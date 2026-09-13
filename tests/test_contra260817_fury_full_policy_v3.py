from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra260817_fury_full_policy_v3 import (
    ADAPTER_CONTRACT_SHA256,
    CONTENT_ADDRESS_ALGORITHM,
    POLICY_ID,
    PROMOTION_BLOCKERS,
    READINESS_SCHEMA,
    REQUIRED_BASELINE_POLICY_ID,
    SOURCE_DEFAULT_PROFILE_V3,
    TRACE_SCHEMA,
    BurstStageV3,
    Contra260817FullPolicyV3Error,
    Contra260817FuryFullPolicyAdapterV3,
    Contra260817FuryFullPolicyStateV3,
    Contra260817FuryTalentV3,
    ProfileOriginV3,
    SurvivalProfileV3,
    TargetSelectionStateV3,
    build_readiness_report_v3,
    build_source_trace_v3,
    main,
    serialize_readiness_report_v3,
    validate_readiness_report_v3,
)
from o2o_dps.expert_policy import (
    CastControl,
    ExpertRole,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
)
from o2o_dps.fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    PUMMEL,
    SLAM,
    SUNDER_ARMOR,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
)


SOURCE_SHA = "1" * 64
SIM_SHA = "2" * 64


def _source_evidence() -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=SOURCE_SHA,
    )


def _sim_evidence() -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SIMULATOR_STATE,
        source_sha256=SIM_SHA,
    )


def _target_state(**changes: object) -> TargetSelectionStateV3:
    target = TargetSelectionStateV3(
        current_target_exists=True,
        current_target_dead=False,
        current_target_friendly=False,
        current_target_is_player=False,
        current_target_banished=False,
        current_target_in_melee_range=True,
        current_target_in_front=True,
        switch_throttle_open=True,
        five_yard_guid_candidate=None,
        five_yard_guid_is_current=None,
        previous_target_guid=None,
        nearest_enemy_changed_target=None,
        nearest_enemy_target_within_five_yards=None,
        post_target_exists=True,
        start_attack_banish_branch_active=False,
        attack_actionbar_present=True,
        autoattack_current=False,
    )
    return replace(target, **changes)


def _combat(
    *,
    weapon_mode: WeaponMode = WeaponMode.DUAL_WIELD,
    **changes: object,
) -> FuryExpertState:
    combat = FuryExpertState(
        rage=40.0,
        target_health_pct=50.0,
        weapon_mode=weapon_mode,
        target_exists=True,
        target_is_boss=False,
        target_name="ignored-convenience-name",
        in_combat=True,
        in_melee_range=True,
        target_distance_yards=3.0,
        bloodthirst_ready_in_s=0.0,
        whirlwind_ready_in_s=0.0,
        slam_cast_time_s=1.5,
        slam_remaining_s=0.0,
        queued_swing=SwingQueueOp.KEEP,
        contra_st_s=1.2,
        contra_ss_s=0.2,
        contra_sd_s=2.6,
        contra_zssdw=0,  # v3 must ignore this convenience value and derive 3.
    )
    return replace(combat, **changes)


def _state(
    *,
    talent: Contra260817FuryTalentV3 = Contra260817FuryTalentV3.DUAL_WIELD,
    count: int = 1,
    classification: ContraTargetClassificationV2 = ContraTargetClassificationV2.WORLDBOSS,
    target_name: str = "Synthetic Boss",
    target_max_health: int = 100_000,
    target_selection: TargetSelectionStateV3 | None = None,
    **combat_changes: object,
) -> Contra260817FuryFullPolicyStateV3:
    weapon = (
        WeaponMode.DUAL_WIELD
        if talent is Contra260817FuryTalentV3.DUAL_WIELD
        else WeaponMode.TWO_HAND
    )
    entry = _combat(weapon_mode=weapon, **combat_changes)
    post = replace(entry)
    return Contra260817FuryFullPolicyStateV3(
        entry_combat=entry,
        post_target_combat=post,
        talent=talent,
        authorization_gate_passed=True,
        attackable_units_within_five_yards=count,
        current_target_within_five_yards=True,
        entry_target_classification=classification,
        entry_target_max_health=target_max_health,
        entry_target_name=target_name,
        post_target_classification=classification,
        post_target_max_health=target_max_health,
        post_target_name=target_name,
        target_selection=target_selection or _target_state(),
        heroic_strike_probe_texture_present=True,
        cleave_probe_texture_present=True,
        target_affecting_combat=True,
        target_casting_spell=None,
        target_sunder_stacks=5,
        player_health_pct=100.0,
        target_of_target_is_player=False,
        offhand_is_shield_after_prelude=False,
        equipped_item_names=(
            "兄弟会头盔",
            "兄弟会胸甲",
            "兄弟会护腿",
        ),
        equipped_mainhand_name="Main",
        equipped_offhand_name="Off",
        player_buffs=frozenset({"战斗怒吼", "狂怒"}),
        target_debuffs=frozenset({"挫志怒吼"}),
        cooldowns_s={"血性狂暴": 10.0, "震荡猛击": 10.0},
        spell_rage_costs={},
        authorization_evidence=_source_evidence(),
        route_evidence=_sim_evidence(),
        entry_snapshot_evidence=_sim_evidence(),
        post_target_snapshot_evidence=_sim_evidence(),
        target_selection_evidence=_sim_evidence(),
        actionbar_evidence=_source_evidence(),
        equipment_evidence=_source_evidence(),
    )


def _synthetic_profile(**changes: object):
    return replace(
        SOURCE_DEFAULT_PROFILE_V3,
        profile_id="contra260817.synthetic.fixture.v3",
        origin=ProfileOriginV3.SYNTHETIC_FIXTURE,
        evidence_sha256=(SOURCE_SHA,),
        **changes,
    )


def _body_sinks(decision: object) -> list[tuple[str, str | None]]:
    return [
        (sink.channel, sink.value)
        for sink in decision.raw_sink_order  # type: ignore[attr-defined]
        if sink.source_ref
        and any(
            token in sink.source_ref
            for token in (
                ":1269",
                ":1273",
                ":1277",
                ":1281",
                ":1285",
                ":1300",
                ":1304",
                ":1308",
                ":1312",
                ":596",
                ":606",
                ":614",
                ":624",
                ":641",
                ":647",
                ":651",
                ":759",
                ":763",
                ":767",
                ":773",
                ":777",
                ":781",
            )
        )
    ]


def _rehash(report: dict[str, object]) -> None:
    core = {key: value for key, value in report.items() if key != "content_address"}
    payload = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    report["content_address"] = {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class Contra260817FuryFullPolicyV3Tests(unittest.TestCase):
    def test_prequeued_cleave_without_prior_whirlwind_keeps_source_guard(self) -> None:
        decision = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                count=2,
                rage=100.0,
                target_distance_yards=10.0,
                queued_swing=SwingQueueOp.CLEAVE,
            )
        )
        self.assertNotIn("顺劈斩", [sink.value for sink in decision.raw_sink_order])
        self.assertIn(
            "IsCurrentAction_returned_true",
            [row["reason"] for row in decision.metadata["helper_no_sink_attempts"]],
        )

    def test_missing_cleave_actionbar_slot_precedes_current_action_check(self) -> None:
        state = replace(
            _state(count=2, rage=100.0, queued_swing=SwingQueueOp.CLEAVE),
            cleave_probe_texture_present=False,
        )
        decision = Contra260817FuryFullPolicyAdapterV3().propose(state)
        self.assertNotIn("顺劈斩", [sink.value for sink in decision.raw_sink_order])
        self.assertIn(
            "Warrior_Cleave_texture_not_found",
            [row["reason"] for row in decision.metadata["helper_no_sink_attempts"]],
        )

    def test_identity_layers_are_distinct_and_readiness_is_typed_blocked(self) -> None:
        report = build_readiness_report_v3()

        self.assertEqual(report["schema"], READINESS_SCHEMA)
        self.assertEqual(report["policy_id"], REQUIRED_BASELINE_POLICY_ID)
        self.assertEqual(report["adapter_expert_id"], POLICY_ID)
        self.assertEqual(
            report["adapter_identity"]["contract_sha256"],
            ADAPTER_CONTRACT_SHA256,
        )
        self.assertEqual(
            report["profile_identity"]["semantic_sha256"],
            SOURCE_DEFAULT_PROFILE_V3.semantic_sha256,
        )
        self.assertEqual(
            report["source_identity"]["status"],
            "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE",
        )
        self.assertTrue(report["source_derived_diagnostic_executable"])
        self.assertFalse(report["runtime_closed"])
        self.assertFalse(report["comparison_ready"])
        self.assertFalse(report["eligible_for_independent_vote"])
        self.assertFalse(report["deployed_contra_substitution_used"])
        codes = {row["code"] for row in report["blockers"]}
        self.assertTrue({code for code, _ in PROMOTION_BLOCKERS}.issubset(codes))
        self.assertIn("SOURCE_DEFAULT_PROFILE_IS_NOT_RUNTIME_PROFILE", codes)

    def test_source_default_dual_single_keeps_full_same_call_order(self) -> None:
        decision = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                rage=70.0,
                target_health_pct=50.0,
                contra_ss_s=0.8,
                bloodthirst_ready_in_s=2.0,
                whirlwind_ready_in_s=2.0,
            )
        )

        self.assertTrue(decision.valid)
        self.assertEqual(decision.provenance.role, ExpertRole.CANDIDATE)
        self.assertFalse(decision.eligible_for_independent_vote)
        self.assertEqual(decision.metadata["route"], "SINGLE_A")
        self.assertEqual(decision.metadata["derived_zssdw"], 3)
        self.assertEqual(
            [(sink.channel, sink.value) for sink in decision.raw_sink_order],
            [
                ("autoattack", "START"),
                ("stance", "狂暴姿态"),
                ("swing_queue", "英勇打击"),
                ("gcd", "嗜血"),
                ("gcd", "旋风斩"),
                ("gcd", "断筋"),
            ],
        )
        self.assertEqual(decision.swing_queue, SwingQueueOp.HEROIC_STRIKE)
        self.assertEqual(decision.gcd, HAMSTRING)
        self.assertEqual(
            decision.metadata["target_helper_trace"],
            [{"branch": "KEEP_VALID_TARGET", "changed": False}],
        )

    def test_multi_tauren_or_bug_and_next_swing_order_are_preserved(self) -> None:
        profile = _synthetic_profile(xuanfeng=False)
        decision = Contra260817FuryFullPolicyAdapterV3(profile).propose(
            _state(
                count=2,
                rage=43.0,
                target_health_pct=19.0,
                contra_st_s=0.6,
                contra_ss_s=1.0,
                bloodthirst_ready_in_s=2.0,
                whirlwind_ready_in_s=2.0,
                race_is_tauren=True,
                target_distance_yards=5.0,
            )
        )

        self.assertEqual(decision.metadata["route"], "MULTI_B")
        self.assertEqual(
            _body_sinks(decision),
            [
                ("gcd", "斩杀"),
                ("gcd", "旋风斩"),
                ("swing_queue", "顺劈斩"),
                ("gcd", "嗜血"),
            ],
        )
        self.assertEqual(decision.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(decision.gcd, BLOODTHIRST)

    def test_interrupt_always_emits_source_stop_call_then_returns(self) -> None:
        profile = _synthetic_profile(interrupt_enabled=True)
        state = replace(
            _state(rage=100.0, slam_remaining_s=0.0),
            target_casting_spell="暗影箭",
        )
        decision = Contra260817FuryFullPolicyAdapterV3(profile).propose(state)

        self.assertTrue(decision.valid)
        self.assertEqual(
            [(sink.channel, sink.value) for sink in decision.raw_sink_order],
            [
                ("autoattack", "START"),
                ("cast_control", None),
                ("gcd", "拳击"),
            ],
        )
        self.assertEqual(decision.cast_control, CastControl.STOP_CAST)
        self.assertEqual(decision.gcd, PUMMEL)
        self.assertEqual(
            decision.metadata["traversal_returns"],
            ["ZS_DaDuan:true->main_rotation_return"],
        )
        self.assertNotIn("狂暴姿态", [sink.value for sink in decision.raw_sink_order])

    def test_sunder_helper_return_stops_body_independent_of_cast_acceptance(self) -> None:
        profile = _synthetic_profile(sunder_enabled=True, sunder_mode="startone")
        state = replace(_state(rage=70.0), target_sunder_stacks=0)
        decision = Contra260817FuryFullPolicyAdapterV3(profile).propose(state)

        self.assertEqual(
            [(sink.channel, sink.value) for sink in decision.raw_sink_order],
            [
                ("autoattack", "START"),
                ("stance", "狂暴姿态"),
                ("gcd", "破甲攻击"),
            ],
        )
        self.assertEqual(decision.gcd, SUNDER_ARMOR)
        self.assertEqual(
            decision.metadata["traversal_returns"],
            ["ZS_POJIA[1]:true->rotation_return"],
        )
        self.assertNotIn("嗜血", [sink.value for sink in decision.raw_sink_order])

    def test_target_fallback_restore_is_ordered_before_attack_and_uses_stale_body(self) -> None:
        target = _target_state(
            current_target_in_melee_range=False,
            five_yard_guid_candidate=None,
            previous_target_guid="old-guid",
            nearest_enemy_changed_target=True,
            nearest_enemy_target_within_five_yards=False,
        )
        state = replace(
            _state(
                target_selection=target,
                target_name="Old target",
                rage=30.0,
                contra_ss_s=1.0,
            ),
            post_target_name="Old target",
        )
        decision = Contra260817FuryFullPolicyAdapterV3().propose(state)

        self.assertEqual(
            [(sink.channel, sink.operation, sink.value) for sink in decision.raw_sink_order[:3]],
            [
                ("target", "TargetNearestEnemy", "NEAREST_ENEMY"),
                ("target", "TargetUnit", "old-guid"),
                ("autoattack", "UseAction", "START"),
            ],
        )
        self.assertEqual(decision.target, TargetOp.AUTO_SWITCH)
        self.assertTrue(decision.metadata["entry_snapshot_precedes_autoselect"])
        self.assertEqual(
            decision.metadata["target_helper_trace"][0]["restore_attempted"],
            True,
        )

    def test_two_hand_single_and_multi_include_stop_cast_and_queue_semantics(self) -> None:
        single = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                talent=Contra260817FuryTalentV3.TWO_HAND,
                target_health_pct=2.0,
                rage=100.0,
                slam_remaining_s=0.5,
                contra_ss_s=1.5,
            )
        )
        self.assertEqual(
            _body_sinks(single),
            [("cast_control", None), ("gcd", "斩杀")],
        )
        self.assertEqual(single.cast_control, CastControl.STOP_CAST)
        self.assertEqual(single.gcd, EXECUTE)

        multi = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                talent=Contra260817FuryTalentV3.TWO_HAND,
                count=2,
                target_max_health=40_000,
                target_health_pct=50.0,
                rage=100.0,
                whirlwind_ready_in_s=6.0,
                bloodthirst_ready_in_s=0.0,
                contra_ss_s=1.0,
                contra_sd_s=2.6,
            )
        )
        self.assertEqual(
            _body_sinks(multi),
            [
                ("swing_queue", "顺劈斩"),
                ("gcd", "嗜血"),
                ("gcd", "猛击"),
            ],
        )
        self.assertEqual(multi.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(multi.gcd, SLAM)

    def test_burst_survival_item_and_equipment_sinks_are_not_dropped(self) -> None:
        stage = BurstStageV3(
            manual_enabled=True,
            trigger_skill="强效怒气药水",
            auto_enabled=False,
            boss_health_threshold_pct=25.0,
            recklessness=True,
            death_wish=False,
            racial_triplet=False,
            slot_13=True,
            slot_14=False,
            mighty_rage_potion=False,
            juju_flurry=False,
            goblin_sapper_charge=False,
            haste_potion=False,
            rage_potion=False,
            elixir_of_rapid_growth=False,
        )
        disabled_stage = replace(stage, manual_enabled=False, recklessness=False, slot_13=False)
        survival = SurvivalProfileV3(
            shield_wall=True,
            shield_wall_below_pct=50.0,
            last_stand=False,
            last_stand_below_pct=50.0,
            healthstone=True,
            healthstone_below_pct=50.0,
            healing_potion=False,
            healing_potion_below_pct=50.0,
            herbal_tea=False,
            herbal_tea_below_pct=50.0,
            boss_ot_weapon_swap=True,
            output_mainhand="Output MH",
            output_offhand="Output OH",
            defensive_mainhand="Tank MH",
            defensive_offhand="Tank Shield",
        )
        profile = _synthetic_profile(
            burst_enabled=True,
            survive_enabled=True,
            burst_stages=(stage, disabled_stage),
            survival=survival,
        )
        state = replace(
            _state(rage=30.0, contra_ss_s=1.0),
            player_health_pct=30.0,
            target_of_target_is_player=True,
            offhand_is_shield_after_prelude=True,
            player_buffs=frozenset({"战斗怒吼", "狂怒", "强效怒气"}),
        )
        decision = Contra260817FuryFullPolicyAdapterV3(profile).propose(state)
        ordered = [(sink.channel, sink.value) for sink in decision.raw_sink_order]

        self.assertIn(("off_gcd", "鲁莽"), ordered)
        self.assertIn(("item", "13"), ordered)
        self.assertIn(("gcd", "盾墙"), ordered)
        self.assertIn(("item", "特效治疗石"), ordered)
        self.assertIn(("stance", "防御姿态"), ordered)
        self.assertIn(("equipment", "Tank MH@16"), ordered)
        self.assertIn(("equipment", "Tank Shield@17"), ordered)
        self.assertIn(("gcd", "盾牌格挡"), ordered)
        self.assertLess(ordered.index(("item", "13")), ordered.index(("gcd", "盾墙")))
        self.assertLess(
            ordered.index(("stance", "防御姿态")),
            ordered.index(("equipment", "Tank MH@16")),
        )

    def test_missing_reached_evidence_fails_before_any_source_sink(self) -> None:
        state = replace(_state(), target_selection_evidence=None)
        decision = Contra260817FuryFullPolicyAdapterV3().propose(state)

        self.assertFalse(decision.valid)
        self.assertEqual(decision.gcd, WAIT_ACTION)
        self.assertEqual(decision.raw_sink_order, ())
        self.assertIn(
            "TARGET_SELECTION_EVIDENCE_MISSING",
            decision.metadata["input_errors"],
        )
        self.assertFalse(decision.metadata["comparison_ready"])

    def test_unverified_source_identity_blocks_before_any_source_sink(self) -> None:
        adapter = Contra260817FuryFullPolicyAdapterV3(
            verify_live_source=False
        )
        decision = adapter.propose(_state())

        self.assertFalse(decision.valid)
        self.assertEqual(decision.raw_sink_order, ())
        self.assertIn(
            "SOURCE_IDENTITY_NOT_VERIFIED_FOR_ADAPTER",
            decision.metadata["input_errors"],
        )
        self.assertFalse(decision.metadata["source_identity_verified"])

    def test_source_trace_is_end_to_end_content_addressed_but_nonruntime(self) -> None:
        trace = build_source_trace_v3(
            Contra260817FuryFullPolicyAdapterV3(),
            _state(rage=70.0, contra_ss_s=0.8, bloodthirst_ready_in_s=2.0, whirlwind_ready_in_s=2.0),
            fixture_id="dual-single-multisink",
        )

        self.assertEqual(trace["schema"], TRACE_SCHEMA)
        self.assertTrue(trace["synthetic"])
        self.assertFalse(trace["source_execution"])
        self.assertFalse(trace["client_acceptance_observed"])
        self.assertFalse(trace["server_outcome_observed"])
        self.assertFalse(trace["comparison_ready"])
        core = {key: value for key, value in trace.items() if key != "content_address"}
        expected = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(trace["content_address"]["sha256"], expected)

    def test_readiness_tampering_cannot_self_report_promotion(self) -> None:
        report = build_readiness_report_v3(verify_live_source=False)
        tampered = deepcopy(report)
        tampered["comparison_ready"] = True
        _rehash(tampered)
        with self.assertRaisesRegex(
            Contra260817FullPolicyV3Error,
            "cannot self-promote",
        ):
            validate_readiness_report_v3(tampered)

        removed = deepcopy(report)
        removed["blockers"] = [
            row
            for row in removed["blockers"]
            if row["code"] != "PER_CHARACTER_CONTRADB_PROFILE_MISSING"
        ]
        _rehash(removed)
        with self.assertRaisesRegex(
            Contra260817FullPolicyV3Error,
            "mandatory typed blocker missing",
        ):
            validate_readiness_report_v3(removed)

    def test_cli_is_canonical_and_require_ready_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            output = Path(raw_tmp) / "readiness.json"
            code = main(
                [
                    "--skip-live-source-verification",
                    "--output",
                    str(output),
                    "--require-ready",
                ]
            )
            self.assertEqual(code, 3)
            payload = output.read_bytes()
            self.assertTrue(payload.endswith(b"\n"))
            self.assertNotIn(b'": ', payload)
            parsed = json.loads(payload)
            self.assertFalse(parsed["comparison_ready"])
            self.assertEqual(
                serialize_readiness_report_v3(parsed),
                payload,
            )


if __name__ == "__main__":
    unittest.main()
