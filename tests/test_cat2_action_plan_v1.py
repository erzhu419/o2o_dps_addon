from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.cat2_action_plan_v1 import (
    CONTENT_ADDRESS_ALGORITHM,
    PLAN_SCHEMA,
    REQUEST_SCHEMA,
    Cat2ActionPlanError,
    build_action_plan,
    main,
    serialize_action_plan,
    validate_action_plan,
)


def _invocation(
    invocation_id: str,
    card_id: str,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "invocation_id": invocation_id,
        "card_id": card_id,
        "step": {"option_values": options or {}},
    }


def _profile_request(*invocations: dict[str, object]) -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "plan_name": "focused-fixture",
        "entry_mode": "PROFILE",
        "invocations": list(invocations),
    }


def _direct_request(card_id: str, *, step: object = None) -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "plan_name": "direct-fixture",
        "entry_mode": "DIRECT_CARD",
        "invocations": [
            {
                "invocation_id": "direct-1",
                "card_id": card_id,
                "step": step,
            }
        ],
    }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _rehash(plan: dict[str, object]) -> None:
    core = {key: value for key, value in plan.items() if key != "content_address"}
    plan["content_address"] = {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


class Cat2ActionPlanTests(unittest.TestCase):
    def test_profile_plan_is_deterministic_content_addressed_and_plan_only(self) -> None:
        request = _profile_request(
            _invocation("front", "warrior_cleave_front_only"),
            _invocation("rage", "warrior_bloodrage", {"maximumRage": 20}),
            _invocation("nearby", "warrior_execute_nearby_target"),
            _invocation(
                "slam",
                "warrior_slam_after_main_skills",
                {
                    "rageThreshold": 25,
                    "minimumSwingTime": 1.5,
                    "mainSkillCooldownThreshold": 2,
                },
            ),
        )

        first = build_action_plan(request)
        second = build_action_plan(deepcopy(request))

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], PLAN_SCHEMA)
        self.assertRegex(first["content_address"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            first["content_address"]["algorithm"], CONTENT_ADDRESS_ALGORITHM
        )
        provenance = first["provenance"]
        self.assertEqual(provenance["authority_role"], "CAPABILITY_SOURCE")
        self.assertFalse(provenance["deployed"])
        self.assertFalse(provenance["runtime_validated"])
        self.assertFalse(provenance["eligible_for_independent_vote"])
        self.assertEqual(provenance["capability_manifest_verification"], "STRICT_LOAD_PASS")
        self.assertEqual(
            provenance["source_tree_verification"],
            "NOT_ATTESTED_IN_PLAN_REFERENCES_PIN_ONLY",
        )
        self.assertIn("BOOLEAN_NOT_ACCEPTANCE", provenance["known_boundary_ids"])
        self.assertIn("DIRECT_CARD_STEP_OMITTED", provenance["known_boundary_ids"])
        distillation = first["distillation_contract"]
        self.assertEqual(distillation["status"], "PLAN_ONLY_NOT_DISTILLED")
        self.assertFalse(distillation["deployment_authorized"])
        self.assertTrue(distillation["runtime_validation_required_after_distillation"])

        serialized = serialize_action_plan(first)
        self.assertTrue(serialized.endswith(b"\n"))
        self.assertNotIn(b'": ', serialized)
        self.assertEqual(json.loads(serialized), first)
        self.assertEqual(validate_action_plan(first), first)

    def test_execute_keeps_ordered_sinks_separate_from_outcomes(self) -> None:
        plan = build_action_plan(
            _profile_request(_invocation("execute", "warrior_execute"))
        )

        node = plan["nodes"][0]
        operations = node["operations"]
        self.assertEqual(operations["kind"], "ORDERED_SINK_SEQUENCE")
        self.assertEqual(
            [item["operation"] for item in operations["operations"]],
            ["SpellStopCasting", "Cat2.Cast"],
        )
        self.assertEqual(
            [item["order"] for item in operations["operations"]], [1, 2]
        )
        self.assertEqual(operations["operations"][1]["arguments"], {"spell": "斩杀"})
        self.assertEqual(
            node["traversal"]["return_signal_on_operation_path"], "LITERAL_TRUE"
        )
        self.assertEqual(
            node["traversal"]["effect_on_operation_path"],
            "STOP_CURRENT_CONFIGURATION_PASS_ONLY",
        )
        self.assertEqual(
            node["outcome_evidence"],
            {
                "sink_attempt": "PLANNED_NOT_OBSERVED",
                "client_acceptance": "UNKNOWN_NOT_INFERRED",
                "server_outcome": "UNKNOWN_NOT_INFERRED",
            },
        )
        forbidden = set(plan["evidence_contract"]["forbidden_collapses"])
        self.assertIn("TRAVERSAL_TRUE_TO_CLIENT_ACCEPTANCE", forbidden)
        self.assertIn("SINK_ATTEMPT_TO_CLIENT_ACCEPTANCE", forbidden)
        self.assertIn("CLIENT_ACCEPTANCE_TO_SERVER_OUTCOME", forbidden)
        self.assertIn("ORDERED_MULTI_SINK_TO_SINGLE_CATEGORICAL_ACTION", forbidden)

    def test_nearby_loop_is_zero_to_unbounded_with_unspecified_pairs_order(self) -> None:
        plan = build_action_plan(
            _profile_request(
                _invocation("nearby", "warrior_execute_nearby_target")
            )
        )

        loop = plan["nodes"][0]["operations"]
        self.assertEqual(loop["kind"], "FOR_EACH_TARGET")
        self.assertEqual(loop["target_cardinality"], {"minimum": 0, "maximum": None})
        self.assertEqual(
            loop["iteration_order"],
            {
                "kind": "UNSPECIFIED",
                "source": "LUA_PAIRS",
                "must_not_sort_or_flatten": True,
            },
        )
        self.assertEqual(
            [item["operation"] for item in loop["body"]["operations"]],
            [
                "SpellStopCasting",
                "TargetUnit",
                "CastSpellByName",
                "TargetUnit_OR_ClearTarget",
            ],
        )
        self.assertEqual(
            loop["body"]["operations"][1]["arguments"],
            {"unit": "LOOP_TARGET_UNIT"},
        )
        self.assertEqual(
            loop["body"]["operations"][2]["arguments"], {"spell": "斩杀"}
        )
        self.assertEqual(
            loop["body"]["operations"][3]["arguments"],
            {"saved_target": "PRE_HELPER_CURRENT_TARGET_OR_CLEAR"},
        )
        bookkeeping = loop["body"]["internal_bookkeeping"]
        self.assertEqual(
            [item["operation"] for item in bookkeeping],
            ["Cat2.RecordPendingCastTarget"],
        )
        self.assertEqual(
            bookkeeping[0]["timing"], "AFTER_SINK_ORDER_1_BEFORE_SINK_ORDER_2"
        )
        self.assertEqual(loop["traversal_after_loop"], "CONTINUE_AFTER_LITERAL_FALSE")
        self.assertEqual(
            plan["nodes"][0]["traversal"]["effect_on_operation_path"],
            "CONTINUE_CURRENT_CONFIGURATION_PASS",
        )

    def test_profile_prepass_and_auto_stance_remain_separate_press_semantics(self) -> None:
        plan = build_action_plan(
            _profile_request(
                _invocation("charge", "warrior_charge_auto_stance", {"maximumRage": 8})
            )
        )

        prepass = plan["entry_contract"]["entry_prepass"]
        self.assertEqual(prepass["semantic_path_id"], "pending_interrupt_prepass")
        self.assertEqual(
            [item["operation"] for item in prepass["operations"]],
            ["SpellStopCasting"],
        )
        sequence = plan["nodes"][0]["operations"]
        self.assertEqual(sequence["kind"], "STATE_GATED_MULTI_PRESS_SEQUENCE")
        self.assertFalse(sequence["atomic"])
        self.assertEqual(
            [item["operation"] for item in sequence["ordered_stages"]],
            ["Cat2.Cast_STANCE", "Cat2.Cast_ABILITY"],
        )
        self.assertEqual(
            [item["arguments"]["spell"] for item in sequence["ordered_stages"]],
            ["战斗姿态", "冲锋"],
        )
        self.assertEqual(
            sequence["repeat"], "ORDERED_ACROSS_SEPARATE_HARDWARE_PRESSES_NOT_ATOMIC"
        )
        absent, present = sequence["entry_state_branches"]
        self.assertEqual(absent["condition"], "REQUIRED_STANCE_ABSENT")
        self.assertEqual(absent["stage_on_this_press"], 1)
        self.assertTrue(absent["ability_requires_later_matching_press"])
        self.assertEqual(present["condition"], "REQUIRED_STANCE_PRESENT")
        self.assertEqual(present["stage_on_this_press"], 2)
        self.assertTrue(present["ability_may_run_on_first_matching_press"])
        self.assertEqual(
            sequence["stage_order_constraint"],
            "STANCE_PRECEDES_ABILITY_ONLY_WHEN_A_STANCE_CHANGE_IS_REQUIRED",
        )
        self.assertEqual(
            [item["operation"] for item in prepass["internal_bookkeeping"]],
            ["Cat2.DispatchCardInternalEvent", "StopMonitoring"],
        )

    def test_direct_entry_rejects_step_and_optioned_cards(self) -> None:
        with self.assertRaisesRegex(
            Cat2ActionPlanError, "does not pass a step argument"
        ):
            build_action_plan(
                _direct_request("warrior_execute", step={"option_values": {}})
            )

        with self.assertRaisesRegex(Cat2ActionPlanError, "direct optioned-card"):
            build_action_plan(_direct_request("warrior_slam_after_main_skills"))
        with self.assertRaisesRegex(Cat2ActionPlanError, "direct passive-card"):
            build_action_plan(_direct_request("warrior_cleave_front_only"))

        direct = build_action_plan(_direct_request("warrior_execute"))
        self.assertEqual(direct["entry_contract"]["call"], "Cat2.ExecuteCardById")
        self.assertFalse(direct["entry_contract"]["passes_step_argument"])
        self.assertIsNone(direct["entry_contract"]["entry_prepass"])
        self.assertIsNone(direct["nodes"][0]["profile_ordinal"])
        self.assertIsNone(direct["nodes"][0]["step"])

    def test_profile_options_groups_and_identifiers_fail_closed(self) -> None:
        with self.assertRaisesRegex(Cat2ActionPlanError, "unknown or unaudited"):
            build_action_plan(
                _profile_request(
                    _invocation("slam", "warrior_slam_after_main_skills", {"bogus": 1})
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "canonical integer"):
            build_action_plan(
                _profile_request(
                    _invocation(
                        "slam", "warrior_slam_after_main_skills", {"rageThreshold": float("nan")}
                    )
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "canonical integer"):
            build_action_plan(
                _profile_request(
                    _invocation(
                        "slam", "warrior_slam_after_main_skills", {"rageThreshold": "25"}
                    )
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "inclusive range"):
            build_action_plan(
                _profile_request(
                    _invocation(
                        "slam", "warrior_slam_after_main_skills", {"minimumSwingTime": 0.01}
                    )
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "exclusive group"):
            build_action_plan(
                _profile_request(
                    _invocation("cleave", "warrior_cleave", {"rageThreshold": 20}),
                    _invocation(
                        "heroic",
                        "warrior_heroic_strike_alt",
                        {"rageThreshold": 30},
                    ),
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "unique passive"):
            build_action_plan(
                _profile_request(
                    _invocation("front-1", "warrior_cleave_front_only"),
                    _invocation("front-2", "warrior_cleave_front_only"),
                )
            )
        with self.assertRaisesRegex(Cat2ActionPlanError, "expected 1-128"):
            build_action_plan(
                {
                    **_profile_request(_invocation("execute", "warrior_execute")),
                    "plan_name": "spaces are rejected",
                }
            )

    def test_unsupported_card_fails_closed(self) -> None:
        with self.assertRaisesRegex(Cat2ActionPlanError, "unsupported or unaudited"):
            build_action_plan(_profile_request(_invocation("x", "warrior_bloodthirst")))

    def test_zero_sink_true_and_nonblocking_cast_are_not_action_outcomes(self) -> None:
        terminate = build_action_plan(
            _profile_request(_invocation("stop", "common_flow_terminate"))
        )["nodes"][0]
        self.assertEqual(terminate["operations"], {"kind": "NO_SINK", "operations": []})
        self.assertEqual(
            terminate["traversal"]["return_signal_on_operation_path"], "LITERAL_TRUE"
        )
        self.assertEqual(
            terminate["outcome_evidence"]["client_acceptance"],
            "NOT_APPLICABLE_NO_CLIENT_SINK",
        )
        self.assertEqual(
            terminate["outcome_evidence"]["sink_attempt"], "NO_SINK_BY_DESIGN"
        )

        continuing = build_action_plan(
            _profile_request(
                _invocation("rage", "warrior_bloodrage", {"maximumRage": 20})
            )
        )["nodes"][0]
        self.assertEqual(
            continuing["traversal"]["return_signal_on_operation_path"],
            "LITERAL_FALSE_OR_NIL",
        )
        self.assertEqual(
            continuing["traversal"]["effect_on_operation_path"],
            "CONTINUE_CURRENT_CONFIGURATION_PASS",
        )

    def test_tampering_fails_address_then_deterministic_recompilation(self) -> None:
        plan = build_action_plan(
            _profile_request(
                _invocation("nearby", "warrior_execute_nearby_target")
            )
        )
        stale_address = deepcopy(plan)
        stale_address["nodes"][0]["operations"]["iteration_order"]["kind"] = "SORTED"
        with self.assertRaisesRegex(Cat2ActionPlanError, "content hash mismatch"):
            validate_action_plan(stale_address)

        rehashed = deepcopy(stale_address)
        _rehash(rehashed)
        with self.assertRaisesRegex(Cat2ActionPlanError, "deterministic compilation"):
            validate_action_plan(rehashed)

    def test_canonical_hash_normalizes_option_key_order_but_not_step_order(self) -> None:
        options_a = {
            "rageThreshold": 25,
            "minimumSwingTime": 1.5,
            "mainSkillCooldownThreshold": 2,
        }
        options_b = {
            "mainSkillCooldownThreshold": 2,
            "minimumSwingTime": 1.5,
            "rageThreshold": 25,
        }
        plan_a = build_action_plan(
            _profile_request(
                _invocation("slam", "warrior_slam_after_main_skills", options_a),
                _invocation("execute", "warrior_execute"),
            )
        )
        plan_b = build_action_plan(
            _profile_request(
                _invocation("slam", "warrior_slam_after_main_skills", options_b),
                _invocation("execute", "warrior_execute"),
            )
        )
        reversed_plan = build_action_plan(
            _profile_request(
                _invocation("execute", "warrior_execute"),
                _invocation("slam", "warrior_slam_after_main_skills", options_a),
            )
        )

        self.assertEqual(plan_a, plan_b)
        self.assertNotEqual(
            plan_a["content_address"]["sha256"],
            reversed_plan["content_address"]["sha256"],
        )

    def test_cli_writes_only_compact_plan_to_stdout(self) -> None:
        request = _profile_request(_invocation("execute", "warrior_execute"))
        with tempfile.TemporaryDirectory() as raw_tmp:
            request_path = Path(raw_tmp) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            stdout = _BinaryStdout()
            with patch("sys.stdout", stdout):
                return_code = main(["--request", str(request_path)])

        self.assertEqual(return_code, 0)
        payload = stdout.buffer.getvalue()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b'": ', payload)
        document = json.loads(payload)
        self.assertEqual(document["schema"], PLAN_SCHEMA)
        self.assertEqual(document["distillation_contract"]["status"], "PLAN_ONLY_NOT_DISTILLED")

    def test_cli_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            request_path = Path(raw_tmp) / "duplicate.json"
            request_path.write_text(
                '{"schema":"cat2_action_plan_request/v1",'
                '"schema":"cat2_action_plan_request/v1"}',
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                return_code = main(["--request", str(request_path)])

        self.assertEqual(return_code, 2)
        self.assertIn("duplicate JSON key", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
