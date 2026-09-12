from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from o2o_dps.cat2new_candidate_executor_v4 import (
    CONTRACT_SHA256,
    EXECUTOR_ID,
    MANDATORY_BLOCKERS,
    PLAN_SCHEMA,
    READINESS_SCHEMA,
    REQUEST_SCHEMA,
    Cat2NewCandidateExecutorV4Error,
    audit_contract_source_mapping_v4,
    build_readiness_report_v4,
    compile_candidate_action_plan_v4,
    serialize_candidate_action_plan_v4,
    serialize_readiness_report_v4,
    validate_candidate_action_plan_v4,
    validate_readiness_report_v4,
)


ROOT = Path(__file__).resolve().parents[1]
CAT2_NEW = ROOT.parent / "Cat2_new"
INSTALLED_CAT2 = ROOT.parent / "Cat2"
SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CAT2_SAVEDVARIABLES",
        str(ROOT / ".local" / "SavedVariables" / "Cat2.lua"),
    )
)


def _op(
    operation_id: str,
    lane: str,
    intent: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "lane": lane,
        "intent": intent,
        "arguments": arguments or {},
    }


def _request(*operations: dict[str, object]) -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "plan_id": "fixture.plan.v4",
        "candidate_policy_id": "brainofcat.optimized.fury.fixture.v4",
        "state_snapshot_sha256": "1" * 64,
        "operations": list(operations),
    }


def _rehash(document: dict[str, object]) -> None:
    core = {key: value for key, value in document.items() if key != "content_address"}
    payload = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    document["content_address"] = {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class Cat2NewCandidateExecutorV4Tests(unittest.TestCase):
    def test_remote_runtime_paths_can_be_bound_independently(self) -> None:
        source_root = ROOT / "remote-fixture" / "Cat2_new"
        installed_root = ROOT / "remote-fixture" / "Cat2"
        savedvariables = ROOT / "remote-fixture" / "Cat2.lua"
        manifest = ROOT / "remote-fixture" / "manifest.json"
        environment = os.environ.copy()
        environment["BOC_CAT2NEW_ROOT"] = str(source_root)
        environment["BOC_CAT2_INSTALLED_ROOT"] = str(installed_root)
        environment["BOC_CAT2_SAVEDVARIABLES"] = str(savedvariables)
        environment["BOC_CAT2_CAPABILITY_MANIFEST"] = str(manifest)
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                (
                    "from o2o_dps.cat2new_candidate_executor_v4 import "
                    "DEFAULT_SOURCE_ROOT,DEFAULT_INSTALLED_ROOT,"
                    "DEFAULT_SAVEDVARIABLES,DEFAULT_MANIFEST; "
                    "print(DEFAULT_SOURCE_ROOT); print(DEFAULT_INSTALLED_ROOT); "
                    "print(DEFAULT_SAVEDVARIABLES); print(DEFAULT_MANIFEST)"
                ),
            ),
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                str(source_root),
                str(installed_root),
                str(savedvariables),
                str(manifest),
            ],
        )

    def test_contract_routes_name_real_pinned_cards_and_core_functions(self) -> None:
        audit = audit_contract_source_mapping_v4(CAT2_NEW)
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["card_route_count"], 22)
        self.assertEqual(audit["core_api_count"], 5)
        self.assertEqual(audit["search_template_count"], 11)
        self.assertRegex(audit["rows_sha256"], r"^[0-9a-f]{64}$")

    def test_source_bound_readiness_keeps_candidate_separate_from_baselines(self) -> None:
        report = build_readiness_report_v4(
            source_root=CAT2_NEW,
            installed_root=INSTALLED_CAT2,
            savedvariables_path=SAVEDVARIABLES,
        )

        self.assertEqual(report["schema"], READINESS_SCHEMA)
        self.assertEqual(report["executor_id"], EXECUTOR_ID)
        self.assertEqual(report["identity"]["contract_sha256"], CONTRACT_SHA256)
        self.assertTrue(report["source_identity"]["verified"])
        self.assertEqual(report["source_identity"]["verification"]["status"], "PASS")
        self.assertEqual(
            report["source_identity"]["contract_source_mapping_audit"]["status"],
            "PASS",
        )
        self.assertFalse(
            report["deployed_identity"]["installed_tree"]["matches_cat2new_tree"]
        )
        self.assertEqual(
            report["deployed_identity"]["installed_tree"]["declared_version"],
            "2026-08-28",
        )
        self.assertFalse(report["role"]["baseline"])
        self.assertEqual(report["role"]["policy_role"], "CANDIDATE")
        self.assertFalse(report["runtime_executable"])
        self.assertFalse(report["runner_registration_authorized"])
        self.assertFalse(report["comparison_ready"])
        self.assertEqual(
            report["capability_matrix"]["search_template_role"],
            "CANDIDATE_GENERATION_HINT_ONLY_NOT_LEARNED_POLICY_OR_BASELINE",
        )
        self.assertEqual(
            report["capability_matrix"]["nearby_execute_target_order"],
            "UNSPECIFIED_LUA_PAIRS_NONOPTIMALITY_CLAIM_FORBIDDEN",
        )
        codes = {row["code"] for row in report["blockers"]}
        self.assertTrue({code for code, _, _ in MANDATORY_BLOCKERS}.issubset(codes))
        self.assertIn("INSTALLED_CAT2_TREE_IS_NOT_CAT2_NEW", codes)

    def test_full_lane_plan_preserves_order_and_typed_extension_gaps(self) -> None:
        request = _request(
            _op(
                "target",
                "target",
                "SET_EXACT_UNIT",
                {"unit": "Creature-0-0001", "restore_after": True},
            ),
            _op("attack", "autoattack", "START"),
            _op(
                "trinket",
                "item",
                "USE_TRINKET_SLOT",
                {"slot": 13, "burst_only": True},
            ),
            _op(
                "potion",
                "item",
                "USE_NAMED_ITEM",
                {"item_name": "强效怒气药水", "self_target": False},
            ),
            _op(
                "weapon",
                "equipment",
                "EQUIP_NAMED_SLOT",
                {"item_name": "削骨之刃", "slot": 16},
            ),
            _op(
                "stance",
                "stance",
                "CAST_ACTION",
                {"action_key": "warrior.berserker_stance", "options": {}},
            ),
            _op(
                "queue",
                "swing_queue",
                "HEROIC_STRIKE",
                {"options": {"rageThreshold": 40}},
            ),
            _op(
                "bloodrage",
                "off_gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodrage", "options": {"maximumRage": 30}},
            ),
            _op(
                "bloodthirst",
                "gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodthirst", "options": {"rageThreshold": 30}},
            ),
        )
        plan = compile_candidate_action_plan_v4(request, source_root=CAT2_NEW)

        self.assertEqual(plan["schema"], PLAN_SCHEMA)
        self.assertEqual(
            [row["operation_id"] for row in plan["ordered_schedule"]],
            [
                "target",
                "attack",
                "trinket",
                "potion",
                "weapon",
                "stance",
                "queue",
                "bloodrage",
                "bloodthirst",
            ],
        )
        self.assertEqual(
            [row["ordinal"] for row in plan["ordered_schedule"]],
            list(range(1, 10)),
        )
        self.assertEqual(
            plan["ordered_schedule"][0]["route"]["core_call"],
            "TargetUnit + observed GUID verification",
        )
        self.assertEqual(
            plan["ordered_schedule"][0]["route"]["possible_ordered_sinks"],
            ["TargetUnit(requested unit)"],
        )
        self.assertIn(
            "capture current target GUID or absence",
            plan["ordered_schedule"][0]["route"]["internal_bookkeeping"],
        )
        self.assertNotEqual(
            plan["ordered_schedule"][0]["route"].get("card_id"),
            "common_auto_target",
        )
        self.assertEqual(
            plan["ordered_schedule"][2]["route"]["card_id"],
            "common_burst_auto_trinket_upper",
        )
        self.assertEqual(
            plan["ordered_schedule"][4]["route"]["core_call"],
            "Cat2.EquipItemByName",
        )
        self.assertEqual(
            plan["ordered_schedule"][6]["route"]["card_id"],
            "warrior_heroic_strike",
        )
        self.assertEqual(
            plan["finally_schedule"][0]["intent"],
            "RESTORE_CAPTURED_TARGET",
        )
        self.assertTrue(plan["finally_schedule"][0]["compiler_generated"])
        self.assertEqual(
            plan["finally_schedule"][0]["arguments"]["captured_by_operation_id"],
            "target",
        )
        self.assertEqual(plan["finally_schedule"][0]["execution_region"], "FINALLY")
        self.assertTrue(
            plan["schedule_analysis"]["finally_must_run_after_body_stop_or_completion"]
        )
        self.assertFalse(plan["schedule_analysis"]["finally_runtime_implemented"])
        self.assertTrue(plan["schedule_analysis"]["source_mapping_complete"])
        self.assertFalse(plan["schedule_analysis"]["native_profile_card_only"])
        self.assertIn(6, plan["schedule_analysis"]["possible_early_stop_ordinals"])
        blocker_codes = {row["code"] for row in plan["operation_blockers"]}
        self.assertIn("EXACT_TARGET_DISPATCH_CARD_MISSING", blocker_codes)
        self.assertIn("TARGET_STATE_REFRESH_NOT_IMPLEMENTED", blocker_codes)
        self.assertIn("NAMED_ITEM_DISPATCH_CARD_MISSING", blocker_codes)
        self.assertIn("WARRIOR_EQUIPMENT_DISPATCH_CARD_MISSING", blocker_codes)
        self.assertIn("EQUIPMENT_DEPENDENCY_REFRESH_NOT_IMPLEMENTED", blocker_codes)
        self.assertIn("QUEUE_ACCEPTED_VS_ACTIVE_STATE_NOT_OBSERVED", blocker_codes)
        self.assertIn("TARGET_RESTORE_FINALLY_DISPATCH_NOT_IMPLEMENTED", blocker_codes)
        self.assertFalse(plan["runtime_executable"])

    def test_queue_cancel_is_not_silently_treated_as_keep(self) -> None:
        plan = compile_candidate_action_plan_v4(
            _request(_op("cancel", "swing_queue", "CANCEL")),
            source_root=CAT2_NEW,
        )
        row = plan["ordered_schedule"][0]
        self.assertEqual(row["route"]["core_call"], "Cat2.WarriorCancelHeroic")
        self.assertEqual(
            row["route"]["support_level"],
            "PINNED_SOURCE_API_REQUIRES_BRAIN_EXTENSION",
        )
        self.assertEqual(
            {item["code"] for item in plan["operation_blockers"]},
            {
                "QUEUE_CANCEL_DISPATCH_CARD_MISSING",
                "QUEUE_CANCEL_POSTCONDITION_NOT_OBSERVED",
            },
        )

    def test_wait_is_terminal_and_explicitly_blocks_fallback(self) -> None:
        plan = compile_candidate_action_plan_v4(
            _request(_op("wait", "wait", "WAIT", {"wait_ms": 175})),
            source_root=CAT2_NEW,
        )
        self.assertTrue(plan["schedule_analysis"]["intentional_wait_is_terminal"])
        self.assertFalse(plan["schedule_analysis"]["fallback_after_intentional_wait_allowed"])
        self.assertEqual(
            plan["ordered_schedule"][0]["route"]["traversal_if_gate_matches"],
            "STOP_CURRENT_CONFIGURATION_PASS_WITH_NO_FALLBACK",
        )
        self.assertEqual(plan["ordered_schedule"][0]["route"]["possible_ordered_sinks"], [])
        codes = {row["code"] for row in plan["operation_blockers"]}
        self.assertEqual(
            codes,
            {
                "DYNAMIC_WAIT_TERMINATION_CARD_MISSING",
                "WAIT_SCHEDULER_RUNTIME_NOT_IMPLEMENTED",
            },
        )

        target_then_wait = compile_candidate_action_plan_v4(
            _request(
                _op(
                    "target",
                    "target",
                    "SET_EXACT_UNIT",
                    {"unit": "Creature-0-0001", "restore_after": True},
                ),
                _op("wait", "wait", "WAIT", {"wait_ms": 175}),
            ),
            source_root=CAT2_NEW,
        )
        self.assertEqual(target_then_wait["ordered_schedule"][-1]["intent"], "WAIT")
        self.assertEqual(
            target_then_wait["finally_schedule"][0]["intent"],
            "RESTORE_CAPTURED_TARGET",
        )
        self.assertTrue(
            target_then_wait["schedule_analysis"]["intentional_wait_is_terminal"]
        )

        with self.assertRaisesRegex(Cat2NewCandidateExecutorV4Error, "WAIT must be the final"):
            compile_candidate_action_plan_v4(
                _request(
                    _op("wait", "wait", "WAIT", {"wait_ms": 100}),
                    _op(
                        "bt",
                        "gcd",
                        "CAST_ACTION",
                        {"action_key": "warrior.bloodthirst", "options": {}},
                    ),
                ),
                source_root=CAT2_NEW,
            )

    def test_action_lane_and_card_option_domains_fail_closed(self) -> None:
        with self.assertRaisesRegex(Cat2NewCandidateExecutorV4Error, "belongs to lane"):
            compile_candidate_action_plan_v4(
                _request(
                    _op(
                        "bad",
                        "gcd",
                        "CAST_ACTION",
                        {"action_key": "warrior.bloodrage", "options": {}},
                    )
                ),
                source_root=CAT2_NEW,
            )
        with self.assertRaisesRegex(Cat2NewCandidateExecutorV4Error, "finite and in"):
            compile_candidate_action_plan_v4(
                _request(
                    _op(
                        "bad",
                        "gcd",
                        "CAST_ACTION",
                        {
                            "action_key": "warrior.whirlwind",
                            "options": {"rageThreshold": 19},
                        },
                    )
                ),
                source_root=CAT2_NEW,
            )

    def test_omitted_whirlwind_option_stays_omitted_for_loadout_default(self) -> None:
        plan = compile_candidate_action_plan_v4(
            _request(
                _op(
                    "ww",
                    "gcd",
                    "CAST_ACTION",
                    {"action_key": "warrior.whirlwind", "options": {}},
                )
            ),
            source_root=CAT2_NEW,
        )
        self.assertEqual(
            plan["ordered_schedule"][0]["route"]["step"]["option_values"],
            {},
        )
        self.assertIn(
            "equipment.brotherhood_set_count",
            plan["ordered_schedule"][0]["route"]["required_state_fields"],
        )

    def test_plan_validation_recompiles_and_rejects_self_promotion(self) -> None:
        plan = compile_candidate_action_plan_v4(
            _request(_op("queue", "swing_queue", "KEEP")),
            source_root=CAT2_NEW,
        )
        self.assertEqual(
            validate_candidate_action_plan_v4(plan, source_root=CAT2_NEW),
            plan,
        )
        self.assertTrue(serialize_candidate_action_plan_v4(plan, source_root=CAT2_NEW).endswith(b"\n"))

        tampered = deepcopy(plan)
        tampered["role"]["baseline"] = True
        _rehash(tampered)
        with self.assertRaises(Cat2NewCandidateExecutorV4Error):
            validate_candidate_action_plan_v4(tampered, source_root=CAT2_NEW)

    def test_readiness_cannot_self_promote_even_after_rehash(self) -> None:
        report = build_readiness_report_v4(
            source_root=CAT2_NEW,
            installed_root=INSTALLED_CAT2,
            savedvariables_path=SAVEDVARIABLES,
        )
        self.assertEqual(validate_readiness_report_v4(report), report)
        self.assertTrue(serialize_readiness_report_v4(report).endswith(b"\n"))

        tampered = deepcopy(report)
        tampered["runner_registration_authorized"] = True
        _rehash(tampered)
        with self.assertRaisesRegex(
            Cat2NewCandidateExecutorV4Error,
            "runner_registration_authorized must remain false",
        ):
            validate_readiness_report_v4(tampered)

        removed = deepcopy(report)
        removed["blockers"] = [
            row
            for row in removed["blockers"]
            if row["code"] != "SERVER_OUTCOME_TRACE_MISSING"
        ]
        _rehash(removed)
        with self.assertRaisesRegex(
            Cat2NewCandidateExecutorV4Error,
            "mandatory typed blocker missing",
        ):
            validate_readiness_report_v4(removed)


if __name__ == "__main__":
    unittest.main()
