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

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CONTENT_ADDRESS_ALGORITHM,
    DEFAULT_SAVEDVARIABLES,
    EXPECTED_CURRENT_SAVEDVARIABLES_SHA256,
    EXPECTED_PROFILE1_SEMANTIC_SHA256,
    MANDATORY_BLOCKERS,
    POLICY_ID,
    REQUIRED_PROFILE1_RAW_SINK_SITES,
    SCHEMA,
    SOURCE_BRANCH_CATALOG,
    TRACE_SCHEMA,
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    CatFuryFullPolicyV4Error,
    CatInventoryItemV4,
    build_branch_audit_v4,
    build_readiness_report_v4,
    build_synthetic_differential_receipt_v4,
    validate_readiness_report_v4,
    validate_source_decision_v4,
    validate_synthetic_differential_receipt_v4,
)
from o2o_dps.expert_policy import ExpertRole, ProvenanceKind, StanceOp
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode


CAT_SAVEDVARIABLES = DEFAULT_SAVEDVARIABLES


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _rehash(document: dict[str, object]) -> None:
    core = {key: value for key, value in document.items() if key != "content_address"}
    document["content_address"] = {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _combat(**changes: object) -> FuryExpertState:
    state = FuryExpertState(
        rage=0.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.DUAL_WIELD,
        target_exists=True,
        target_is_boss=True,
        in_combat=True,
        in_melee_range=True,
        nearby_enemies=1,
        gcd_ready=True,
        current_stance=StanceOp.BERSERKER,
        has_battle_shout=True,
        flurry_talent=True,
        flurry_active=True,
        bloodthirst_ready_in_s=10.0,
        whirlwind_ready_in_s=10.0,
        mainhand_swing_remaining_s=1.0,
        nampower=True,
    )
    return replace(state, **changes)


class CatFuryFullPolicyReadinessV4Tests(unittest.TestCase):
    def test_live_read_only_identity_and_readiness_layers_are_distinct(self) -> None:
        report = build_readiness_report_v4()

        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["policy_id"], POLICY_ID)
        self.assertEqual(report["source_identity"]["status"], "VERIFIED")
        self.assertTrue(report["readiness"]["source_identity"])
        self.assertTrue(report["readiness"]["current_savedvariables_byte_identity"])
        self.assertTrue(report["readiness"]["profile1_semantic_identity"])
        self.assertTrue(report["readiness"]["existing_runtime_snapshot_identity"])
        self.assertEqual(
            report["savedvariables_identity"]["current"]["sha256"],
            EXPECTED_CURRENT_SAVEDVARIABLES_SHA256,
        )
        self.assertEqual(
            report["savedvariables_identity"]["current_profile1"][
                "profile_semantic_sha256"
            ],
            EXPECTED_PROFILE1_SEMANTIC_SHA256,
        )
        self.assertTrue(
            report["savedvariables_identity"][
                "current_profile1_semantics_match_existing_snapshot"
            ]
        )
        self.assertFalse(
            report["savedvariables_identity"]["current_raw_matches_existing_snapshot"]
        )
        codes = {row["code"] for row in report["blockers"]}
        self.assertIn(
            "CURRENT_SAVEDVARIABLES_BYTES_DIFFER_FROM_EXISTING_SNAPSHOT", codes
        )
        self.assertTrue(
            {code for code, _, _ in MANDATORY_BLOCKERS}.issubset(codes)
        )
        self.assertTrue(report["source_derived_diagnostic_executable"])
        self.assertFalse(report["game_runtime_closed"])
        self.assertFalse(report["comparison_ready"])
        self.assertFalse(report["eligible_for_independent_vote"])
        self.assertFalse(report["runner_registration_authorized"])
        self.assertFalse(report["formal_runner_registry_modified"])
        self.assertFalse(report["scientific_run_launched"])

    def test_branch_audit_is_exhaustive_and_does_not_promote_legacy_adapter(self) -> None:
        audit = build_branch_audit_v4()
        rows = audit["branches"]
        ids = [row["branch_id"] for row in rows]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 35)
        self.assertEqual(
            audit["full_policy_v4"]["known_unmapped_reachable_branch_ids"], []
        )
        self.assertFalse(
            audit["legacy_adapter"]["full_profile1_branch_coverage"]
        )
        self.assertTrue(audit["legacy_adapter"]["identity_matches"])
        statuses = {row["profile1_status"] for row in rows}
        self.assertTrue(
            {"MAPPED_REACHABLE", "MAPPED_SENSITIVITY", "DISABLED_BY_PROFILE", "COMMENTED_OUT"}.issubset(statuses)
        )
        lineage = audit["lineage"]
        self.assertFalse(lineage["frozen_coverage_v2_modified"])
        self.assertFalse(lineage["frozen_coverage_v2_reused_as_live_authority"])

    def test_literal_source_oracle_receipt_covers_order_and_legacy_differences(self) -> None:
        receipt = validate_synthetic_differential_receipt_v4(
            build_synthetic_differential_receipt_v4()
        )

        self.assertEqual(receipt["schema"], TRACE_SCHEMA)
        coverage = receipt["coverage"]
        self.assertEqual(coverage["uncovered_mapped_branch_ids"], [])
        self.assertTrue(coverage["full_policy_v4_source_oracle_complete"])
        self.assertTrue(
            coverage["full_policy_v4_raw_sink_site_coverage_complete"]
        )
        self.assertEqual(coverage["uncovered_raw_sink_site_ids"], [])
        self.assertEqual(
            coverage["required_raw_sink_site_count"],
            len(REQUIRED_PROFILE1_RAW_SINK_SITES),
        )
        self.assertEqual(
            coverage["full_policy_v4_fixture_count"],
            coverage["full_policy_v4_exact_match_count"],
        )
        self.assertLess(
            coverage["legacy_exact_match_count"],
            coverage["full_policy_v4_exact_match_count"],
        )
        rows = {row["fixture_id"]: row for row in receipt["fixtures"]}
        bloodrage = rows["duplicate_bloodrage_attempts"][
            "full_policy_v4_raw_sink_order"
        ]
        self.assertEqual(
            [(row["channel"], row["operation"], row["value"]) for row in bloodrage],
            [
                ("off_gcd", "QueueSpellByName", "血性狂暴"),
                ("off_gcd", "QueueSpellByName", "血性狂暴"),
            ],
        )
        slam = rows["no_flurry_slam_cvar_order"][
            "full_policy_v4_raw_sink_order"
        ]
        self.assertEqual(
            [(row["channel"], row["operation"], row["value"]) for row in slam],
            [
                ("cvar", "SetCVar", "NP_QueueCastTimeSpells=0"),
                ("cvar", "SetCVar", "NP_QueueInstantSpells=0"),
                ("gcd", "CastSpellByName", "猛击"),
                ("cvar", "SetCVar", "NP_QueueCastTimeSpells=1"),
                ("cvar", "SetCVar", "NP_QueueInstantSpells=1"),
            ],
        )
        offset = rows["dual_normal_bt_offset"]
        self.assertEqual(
            offset["full_policy_v4_raw_sink_order"][0]["value"], "嗜血"
        )
        self.assertFalse(offset["legacy_delta"]["matches"])
        self.assertFalse(receipt["source_execution"])
        self.assertFalse(receipt["comparison_ready"])
        self.assertFalse(receipt["runner_registration_authorized"])

    def test_adapter_attempts_source_call_when_gcd_ready_input_is_false(self) -> None:
        state = CatFuryFullPolicyStateV4(
            combat=_combat(
                rage=30.0,
                gcd_ready=False,
                bloodthirst_ready_in_s=1.0,
            )
        )
        decision = validate_source_decision_v4(
            CatFuryFullPolicyAdapterV4().propose(state)
        )

        self.assertEqual(decision.gcd, "warrior.bloodthirst")
        self.assertEqual(decision.raw_sink_order[-1].value, "嗜血")
        self.assertFalse(
            decision.metadata["gcd_ready_input_is_acceptance_state_only"]
        )
        self.assertFalse(decision.metadata["source_attempts_suppressed_by_gcd_ready"])
        self.assertEqual(decision.provenance.kind, ProvenanceKind.SOURCE_DERIVED)
        self.assertEqual(decision.provenance.role, ExpertRole.CANDIDATE)
        self.assertFalse(decision.eligible_for_independent_vote)

    def test_full_same_invocation_utility_order_is_preserved(self) -> None:
        items = (
            CatInventoryItemV4("特效治疗石", 0, 1),
            CatInventoryItemV4("糖水茶", 1, 2),
            CatInventoryItemV4("诺达纳尔草药茶", 2, 3),
        )
        state = CatFuryFullPolicyStateV4(
            combat=_combat(
                rage=10.0,
                has_battle_shout=False,
                battle_shout_remaining_s=0.0,
            ),
            autoattack_active=False,
            upper_trinket_supported=True,
            upper_trinket_cooldown_s=0.0,
            lower_trinket_supported=True,
            lower_trinket_cooldown_s=0.0,
            player_health_pct=10.0,
            inventory_items=items,
        )
        decision = validate_source_decision_v4(
            CatFuryFullPolicyAdapterV4().propose(state)
        )

        self.assertEqual(
            [(sink.channel, sink.value) for sink in decision.raw_sink_order],
            [
                ("autoattack", "START"),
                ("item", "13"),
                ("item", "14"),
                ("item", "0:1:特效治疗石"),
                ("item", "1:2:糖水茶"),
                ("item", "2:3:诺达纳尔草药茶"),
                ("gcd", "战斗怒吼"),
            ],
        )
        self.assertEqual(decision.metadata["traversal_return"], "BATTLE_SHOUT_RETURN")

    def test_self_promotion_is_rejected_even_after_rehash(self) -> None:
        report = deepcopy(build_readiness_report_v4())
        report["comparison_ready"] = True
        _rehash(report)
        with self.assertRaisesRegex(CatFuryFullPolicyV4Error, "comparison_ready"):
            validate_readiness_report_v4(report)

        receipt = deepcopy(build_synthetic_differential_receipt_v4())
        receipt["runner_registration_authorized"] = True
        _rehash(receipt)
        with self.assertRaisesRegex(CatFuryFullPolicyV4Error, "authorize a runner"):
            validate_synthetic_differential_receipt_v4(receipt)

    def test_savedvariables_byte_and_semantic_drift_fail_closed(self) -> None:
        source = CAT_SAVEDVARIABLES.read_bytes()
        marker = b"MPWarriorFurySaved = {"
        start = source.index(marker)
        prefix, body = source[:start], source[start:]
        self.assertIn(b'["Whirlwind"] = 1,', body)
        drifted = prefix + body.replace(
            b'["Whirlwind"] = 1,', b'["Whirlwind"] = 0,', 1
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Cat.lua"
            path.write_bytes(drifted)
            report = build_readiness_report_v4(savedvariables_path=path)

        codes = {row["code"] for row in report["blockers"]}
        self.assertIn("CURRENT_SAVEDVARIABLES_IDENTITY_DRIFT", codes)
        self.assertIn("CURRENT_PROFILE1_SEMANTIC_DRIFT", codes)
        self.assertFalse(report["readiness"]["current_savedvariables_byte_identity"])
        self.assertFalse(report["readiness"]["profile1_semantic_identity"])
        self.assertFalse(report["source_derived_diagnostic_executable"])
        self.assertFalse(report["comparison_ready"])

    def test_savedvariables_raw_only_drift_does_not_inherit_semantic_authority(self) -> None:
        source = CAT_SAVEDVARIABLES.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Cat.lua"
            path.write_bytes(source + b"\n-- v4 raw identity drift\n")
            report = build_readiness_report_v4(savedvariables_path=path)

        self.assertTrue(report["readiness"]["profile1_semantic_identity"])
        self.assertFalse(report["readiness"]["current_savedvariables_byte_identity"])
        self.assertFalse(report["source_derived_diagnostic_executable"])
        self.assertFalse(report["comparison_ready"])

    def test_runtime_snapshot_tamper_is_typed_and_blocked(self) -> None:
        source = (
            PROJECT_ROOT
            / "offline_data/expert_runtime_snapshots/v1"
            / "fury_expert_runtime_snapshot_v1.4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778.json"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            path.write_bytes(source + b" ")
            report = build_readiness_report_v4(runtime_snapshot_path=path)

        codes = {row["code"] for row in report["blockers"]}
        self.assertIn("RUNTIME_SNAPSHOT_IDENTITY_FAILED", codes)
        self.assertFalse(report["readiness"]["existing_runtime_snapshot_identity"])
        self.assertFalse(report["comparison_ready"])

    def test_v4_builders_do_not_mutate_frozen_v2_v3_or_coverage_files(self) -> None:
        frozen = [
            PROJECT_ROOT / "configs/evaluation/fury_multiseed_protocol_v2.json",
            PROJECT_ROOT / "configs/evaluation/fury_multiseed_protocol_v3.json",
            PROJECT_ROOT / "o2o_dps/fury_multiseed_evaluation_v2.py",
            PROJECT_ROOT / "o2o_dps/fury_multiseed_evaluation_v3.py",
            PROJECT_ROOT / "o2o_dps/fury_paired_multiseed_runner_v2.py",
            PROJECT_ROOT / "o2o_dps/fury_paired_multiseed_runner_v3.py",
            PROJECT_ROOT / "o2o_dps/fury_full_policy_rollout_v2.py",
            PROJECT_ROOT / "o2o_dps/fury_full_policy_rollout_v3.py",
            PROJECT_ROOT / "o2o_dps/fury_dynamic_target_semantics_v3.py",
            PROJECT_ROOT / "o2o_dps/fury_expert_adapter_coverage_gate_v2.py",
            PROJECT_ROOT / "offline_data/sim_validation/fury_expert_adapter_coverage_gate_v2.plan.json",
        ]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen}

        build_branch_audit_v4()
        build_synthetic_differential_receipt_v4()
        build_readiness_report_v4()

        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen}
        self.assertEqual(after, before)

    def test_inventory_state_rejects_ambiguous_coordinates(self) -> None:
        with self.assertRaisesRegex(ValueError, "coordinates"):
            CatFuryFullPolicyStateV4(
                combat=_combat(),
                inventory_items=(
                    CatInventoryItemV4("特效治疗石", 0, 1),
                    CatInventoryItemV4("糖水茶", 0, 1),
                ),
            )


if __name__ == "__main__":
    unittest.main()
