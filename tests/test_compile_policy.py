from __future__ import annotations

import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.compile_policy import PolicyCompileError, compile_policy, main


POLICY = PROJECT_ROOT / "policies" / "fury_seed.json"


class CompilePolicyTests(unittest.TestCase):
    def test_seed_compiles_to_legacy_lua_policy(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        lua = compile_policy(document, source_name=POLICY.name)

        self.assertIn('local policyId = "fury_warrior_compiled_seed_v1"', lua)
        self.assertIn("state.rage >= 50", lua)
        self.assertIn("off_gcd = {}", lua)
        self.assertIn('table.insert(proposal.queue, "warrior_heroic_strike")', lua)
        self.assertIn('table.insert(proposal.gcd, "warrior_bloodthirst")', lua)
        self.assertNotIn("brain.FuryPolicyId = policyId", lua)
        self.assertNotIn("#", "\n".join(line for line in lua.splitlines() if not line.startswith("--")))
        self.assertNotIn("%", lua)

    def test_unknown_state_field_is_rejected(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["rules"][0]["when"][0]["field"] = "futureBossHealth"
        with self.assertRaisesRegex(PolicyCompileError, "not exported"):
            compile_policy(document)

    def test_typed_energy_state_is_exported_to_lua(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["rules"][0]["when"][0] = {
            "field": "energy",
            "op": "ge",
            "value": 40,
        }
        lua = compile_policy(document)
        self.assertIn("state.energy >= 40", lua)

    def test_new_timer_and_queue_fields_gate_unknown_values(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["rules"] = [
            {
                "when": [
                    {
                        "field": "bloodrageCooldown",
                        "op": "le",
                        "value": 0,
                    },
                    {
                        "field": "bloodthirstCooldown",
                        "op": "le",
                        "value": 0,
                    },
                    {
                        "field": "whirlwindCooldownKnown",
                        "op": "eq",
                        "value": True,
                    },
                    {
                        "field": "mainHandSwingRemaining",
                        "op": "lt",
                        "value": 0.5,
                    },
                    {
                        "field": "queuedSwing",
                        "op": "eq",
                        "value": "KEEP",
                    },
                    {
                        "field": "targetLevel",
                        "op": "ge",
                        "value": 60,
                    },
                ],
                "gcd": ["warrior_bloodthirst"],
            }
        ]
        lua = compile_policy(document)

        self.assertIn(
            "(state.bloodrageCooldownKnown == true and "
            "state.bloodrageCooldown <= 0)",
            lua,
        )
        self.assertIn(
            "(state.bloodthirstCooldownKnown == true and "
            "state.bloodthirstCooldown <= 0)",
            lua,
        )
        self.assertIn("state.whirlwindCooldownKnown == true", lua)
        self.assertIn(
            "(state.mainHandSwingRemainingKnown == true and "
            "state.mainHandSwingRemaining < 0.5)",
            lua,
        )
        self.assertIn(
            '(state.queuedSwingKnown == true and state.queuedSwing == "KEEP")',
            lua,
        )
        self.assertIn(
            "(state.targetLevelKnown == true and state.targetLevel >= 60)",
            lua,
        )

    def test_prelude_rule_and_default_support_off_gcd_lane(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["prelude"] = {"off_gcd": ["warrior_bloodrage"]}
        document["rules"] = [
            {
                "when": [{"field": "rage", "op": "ge", "value": 40}],
                "off_gcd": ["warrior_o2o_cancel_queue"],
                "queue": ["warrior_o2o_cleave_40"],
                "gcd": ["warrior_bloodthirst"],
            }
        ]
        document["default"] = {"off_gcd": ["warrior_berserker_rage"]}
        lua = compile_policy(document)

        prelude = lua.index(
            'table.insert(proposal.off_gcd, "warrior_bloodrage")'
        )
        rule = lua.index("if state.rage >= 40 then")
        rule_off_gcd = lua.index(
            'table.insert(proposal.off_gcd, "warrior_o2o_cancel_queue")'
        )
        rule_queue = lua.index(
            'table.insert(proposal.queue, "warrior_o2o_cleave_40")'
        )
        rule_gcd = lua.index(
            'table.insert(proposal.gcd, "warrior_bloodthirst")'
        )
        default = lua.index(
            'table.insert(proposal.off_gcd, "warrior_berserker_rage")'
        )
        self.assertLess(prelude, rule)
        self.assertLess(rule, rule_off_gcd)
        self.assertLess(rule_off_gcd, rule_queue)
        self.assertLess(rule_queue, rule_gcd)
        self.assertLess(rule_gcd, default)

    def test_prelude_must_be_an_object(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["prelude"] = ["warrior_bloodrage"]
        with self.assertRaisesRegex(PolicyCompileError, "prelude must be an object"):
            compile_policy(document)

    def test_continue_rule_composes_queue_then_gcd_lane(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["rules"] = [
            {
                "when": [{"field": "queuedSwing", "op": "ne", "value": "KEEP"}],
                "off_gcd": ["warrior_o2o_cancel_queue"],
                "continue": True,
            },
            {
                "when": [{"field": "rage", "op": "ge", "value": 30}],
                "gcd": ["warrior_bloodthirst"],
            },
        ]
        lua = compile_policy(document)

        cancel = lua.index(
            'table.insert(proposal.off_gcd, "warrior_o2o_cancel_queue")'
        )
        gcd_rule = lua.index("if state.rage >= 30 then")
        self.assertLess(cancel, gcd_rule)
        self.assertNotIn("return proposal", lua[cancel:gcd_rule])
        self.assertIn("return proposal", lua[gcd_rule:])

    def test_continue_rule_flag_must_be_boolean(self) -> None:
        document = json.loads(POLICY.read_text(encoding="utf-8"))
        document["rules"][0]["continue"] = "yes"
        with self.assertRaisesRegex(PolicyCompileError, "continue must be boolean"):
            compile_policy(document)

    def test_cli_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "GeneratedPolicy.lua"
            with redirect_stdout(io.StringIO()):
                return_code = main([str(POLICY), str(output)])
            self.assertEqual(return_code, 0)
            self.assertTrue(output.is_file())
            self.assertIn("RegisterPolicy", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
