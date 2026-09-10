from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.compile_policy import compile_policy


CANDIDATE = (
    PROJECT_ROOT
    / "configs"
    / "policies"
    / "fury_level_conditioned_candidate_v1.json"
)
POLICY = PROJECT_ROOT / "policies" / "fury_combined_candidate_shadow_v1.json"
LUA = WORKSPACE_ROOT / "addon" / "FuryCombinedCandidateShadowV1.lua"
TOC = WORKSPACE_ROOT / "BrainOfCat.toc"

KNOWN_BY_VALUE = {
    "bloodrageCooldown": "bloodrageCooldownKnown",
    "bloodthirstCooldown": "bloodthirstCooldownKnown",
    "whirlwindCooldown": "whirlwindCooldownKnown",
    "mainHandSwingRemaining": "mainHandSwingRemainingKnown",
    "queuedSwing": "queuedSwingKnown",
    "targetLevel": "targetLevelKnown",
}
OPERATORS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "lt": lambda left, right: left < right,
    "le": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "ge": lambda left, right: left >= right,
}


def resolve_document(policy: dict[str, object], state: dict[str, object]) -> dict[str, list[str]]:
    proposal: dict[str, list[str]] = {"off_gcd": [], "queue": [], "gcd": []}

    def matches(conditions: list[dict[str, object]]) -> bool:
        for condition in conditions:
            field = str(condition["field"])
            known_field = KNOWN_BY_VALUE.get(field)
            if known_field and state.get(known_field) is not True:
                return False
            if not OPERATORS[str(condition["op"])](
                state.get(field), condition["value"]
            ):
                return False
        return True

    if not matches(policy["guards"]):
        return proposal
    prelude = policy.get("prelude", {})
    proposal["off_gcd"].extend(prelude.get("off_gcd", []))
    for rule in policy["rules"]:
        if not matches(rule["when"]):
            continue
        for lane in proposal:
            proposal[lane].extend(rule.get(lane, []))
        if rule.get("continue") is not True:
            return proposal
    default = policy.get("default", {})
    for lane in proposal:
        proposal[lane].extend(default.get(lane, []))
    return proposal


class FuryCombinedShadowPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        self.policy = json.loads(POLICY.read_text(encoding="utf-8"))
        self.lua = LUA.read_text(encoding="utf-8")

    def test_candidate_parameters_are_copied_without_policy_drift(self) -> None:
        selected = self.candidate["candidates"][0]
        translated = self.policy["translation_contract"]["parameter_values"]
        self.assertEqual(translated, selected)

        costs = self.policy["translation_contract"]["current_character_costs"]
        thresholds = self.policy["translation_contract"]["derived_thresholds"]
        self.assertEqual(
            thresholds["execute_bloodthirst_reserve"],
            costs["execute"] + costs["bloodthirst"],
        )
        self.assertEqual(
            thresholds["low_level_multi_queue_bloodthirst_window"],
            costs["cleave"] + costs["bloodthirst"],
        )
        self.assertEqual(
            thresholds["low_level_multi_queue_whirlwind_window"],
            costs["cleave"] + costs["whirlwind"],
        )
        self.assertEqual(
            thresholds["single_cancel_below"],
            translated["heroic_strike_threshold"]
            - translated["queue_cancel_margin"],
        )
        self.assertEqual(
            thresholds["high_or_unknown_level_multi_queue"],
            translated["cleave_threshold"],
        )
        self.assertEqual(
            thresholds["high_or_unknown_level_multi_cancel_below"],
            translated["cleave_threshold"]
            - translated["queue_cancel_margin"],
        )

    def test_generated_artifact_is_exact_and_inactive(self) -> None:
        expected = compile_policy(self.policy, source_name=POLICY.name)
        self.assertEqual(self.lua, expected)
        self.assertFalse(self.policy["activate"])
        self.assertNotIn("brain.FuryPolicyId = policyId", self.lua)
        self.assertIn('brain.RegisterPolicy(policyId, Resolve)', self.lua)

        toc = TOC.read_text(encoding="utf-8-sig")
        self.assertIn(r"addon\FuryCombinedCandidateShadowV1.lua", toc)
        self.assertLess(
            toc.index(r"addon\O2OWarriorActionCards.lua"),
            toc.index(r"addon\FuryCombinedCandidateShadowV1.lua"),
        )
        self.assertLess(
            toc.index(r"addon\FuryCombinedCandidateShadowV1.lua"),
            toc.index(r"addon\O2OPolicyBrainCard.lua"),
        )

    def test_factorized_lanes_encode_selected_queue_and_gcd_priorities(self) -> None:
        self.assertIn(
            'table.insert(proposal.off_gcd, "warrior_bloodrage")', self.lua
        )
        self.assertNotIn("warrior_death_wish", self.lua)
        self.assertNotIn("warrior_hamstring", self.lua)
        self.assertIn(
            'table.insert(proposal.off_gcd, "warrior_o2o_cancel_queue")',
            self.lua,
        )
        self.assertIn(
            'table.insert(proposal.queue, "warrior_heroic_strike")', self.lua
        )
        self.assertIn(
            'table.insert(proposal.queue, "warrior_o2o_cleave_40")', self.lua
        )
        self.assertIn(
            'table.insert(proposal.queue, "warrior_cleave")', self.lua
        )

        rules = self.policy["rules"]
        first_gcd = next(index for index, rule in enumerate(rules) if rule.get("gcd"))
        self.assertTrue(all(rule.get("continue") is True for rule in rules[:first_gcd]))
        self.assertEqual(rules[first_gcd]["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(rules[first_gcd + 1]["gcd"], ["warrior_execute"])
        self.assertEqual(rules[first_gcd + 2]["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(rules[first_gcd + 3]["gcd"], ["warrior_whirlwind"])
        self.assertEqual(rules[first_gcd + 4]["gcd"], ["warrior_whirlwind"])
        self.assertEqual(rules[first_gcd + 5]["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(rules[first_gcd + 6]["gcd"], ["warrior_whirlwind"])
        self.assertEqual(rules[first_gcd + 7]["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(rules[first_gcd + 8]["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(rules[first_gcd + 9]["gcd"], ["warrior_whirlwind"])

    def test_unknown_probes_cannot_satisfy_comparisons(self) -> None:
        for rule in self.policy["rules"]:
            for condition in rule["when"]:
                field = condition["field"]
                known_field = KNOWN_BY_VALUE.get(field)
                if known_field:
                    comparison = f"state.{field} "
                    guard = f"state.{known_field} == true and {comparison}"
                    self.assertIn(guard, self.lua)

            numeric_nearby = any(
                condition["field"] == "nearbyEnemies"
                for condition in rule["when"]
            )
            if numeric_nearby:
                self.assertIn(
                    {
                        "field": "nearbyEnemiesKnown",
                        "op": "eq",
                        "value": True,
                    },
                    rule["when"],
                )

        self.assertNotIn("state.mainHandSwingRemaining ", self.lua)
        self.assertIn(
            "Unknown target level deliberately selects the high-level",
            self.policy["translation_contract"]["unknown_state_behavior"],
        )

    def test_representative_state_matrix_matches_candidate_boundaries(self) -> None:
        base: dict[str, object] = {
            "classFile": "WARRIOR",
            "inCombat": True,
            "targetExists": True,
            "targetCanAttack": True,
            "targetIsDead": False,
            "powerType": 1,
            "rage": 50,
            "targetPercentHealth": 100,
            "nearbyEnemiesKnown": True,
            "nearbyEnemies": 1,
            "targetLevelKnown": True,
            "targetLevel": 60,
            "queuedSwingKnown": True,
            "queuedSwing": "KEEP",
            "bloodrageCooldownKnown": True,
            "bloodrageCooldown": 0,
            "bloodthirstCooldownKnown": True,
            "bloodthirstCooldown": 5.0,
            "whirlwindCooldownKnown": True,
            "whirlwindCooldown": 5.0,
            "gcd": 0,
        }

        def proposal(**changes: object) -> dict[str, list[str]]:
            return resolve_document(self.policy, {**base, **changes})

        self.assertEqual(proposal(rage=29)["off_gcd"], ["warrior_bloodrage"])
        self.assertEqual(
            proposal(rage=29, bloodrageCooldown=12)["off_gcd"], []
        )
        self.assertEqual(
            proposal(
                rage=29,
                bloodrageCooldownKnown=False,
                bloodrageCooldown=None,
            )["off_gcd"],
            [],
        )
        self.assertEqual(proposal(rage=30)["off_gcd"], [])

        execute_bt = proposal(
            targetPercentHealth=19,
            rage=40,
            queuedSwing="HEROIC_STRIKE",
            bloodthirstCooldown=0,
        )
        self.assertEqual(execute_bt["off_gcd"], ["warrior_o2o_cancel_queue"])
        self.assertEqual(execute_bt["gcd"], ["warrior_bloodthirst"])
        self.assertEqual(
            proposal(targetPercentHealth=19, rage=39, bloodthirstCooldown=0)["gcd"],
            ["warrior_execute"],
        )

        self.assertEqual(proposal(rage=50)["queue"], ["warrior_heroic_strike"])
        self.assertEqual(
            proposal(rage=41, queuedSwing="HEROIC_STRIKE")["off_gcd"],
            ["warrior_o2o_cancel_queue"],
        )
        self.assertEqual(
            proposal(rage=30, bloodthirstCooldown=0, whirlwindCooldown=0)["gcd"],
            ["warrior_bloodthirst"],
        )

        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=48,
                bloodthirstCooldown=1.3,
                whirlwindCooldown=5,
            )["queue"],
            ["warrior_o2o_cleave_40"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=47,
                bloodthirstCooldown=1.3,
                whirlwindCooldown=5,
            )["queue"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=43,
                bloodthirstCooldown=2,
                whirlwindCooldown=1.3,
            )["queue"],
            ["warrior_o2o_cleave_40"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=42,
                bloodthirstCooldown=2,
                whirlwindCooldown=1.3,
            )["queue"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=40,
                bloodthirstCooldown=2,
                whirlwindCooldown=2,
            )["queue"],
            ["warrior_o2o_cleave_40"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=39,
                bloodthirstCooldown=2,
                whirlwindCooldown=2,
            )["queue"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=39,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=1.3,
            )["off_gcd"],
            ["warrior_o2o_cancel_queue"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=40,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=1.3,
            )["off_gcd"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=34,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=2,
                whirlwindCooldown=1.3,
            )["off_gcd"],
            ["warrior_o2o_cancel_queue"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=35,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=2,
                whirlwindCooldown=1.3,
            )["off_gcd"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=31,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=2,
                whirlwindCooldown=2,
            )["off_gcd"],
            ["warrior_o2o_cancel_queue"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=32,
                queuedSwing="CLEAVE",
                bloodthirstCooldown=2,
                whirlwindCooldown=2,
            )["off_gcd"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                rage=30,
                bloodthirstCooldown=0,
                whirlwindCooldown=0,
            )["gcd"],
            ["warrior_bloodthirst"],
        )

        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                targetLevel=61,
                rage=55,
            )["queue"],
            ["warrior_cleave"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                targetLevel=61,
                rage=54,
            )["queue"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                targetLevel=61,
                rage=46,
                queuedSwing="CLEAVE",
            )["off_gcd"],
            ["warrior_o2o_cancel_queue"],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                targetLevel=61,
                rage=47,
                queuedSwing="CLEAVE",
            )["off_gcd"],
            [],
        )
        self.assertEqual(
            proposal(
                nearbyEnemies=3,
                targetLevel=61,
                rage=30,
                bloodthirstCooldown=0,
                whirlwindCooldown=0,
            )["gcd"],
            ["warrior_whirlwind"],
        )

        unknown_level = proposal(
            nearbyEnemies=3,
            targetLevelKnown=False,
            targetLevel=None,
            rage=55,
            bloodthirstCooldown=0,
            whirlwindCooldown=0,
        )
        self.assertEqual(unknown_level["queue"], ["warrior_cleave"])
        self.assertEqual(unknown_level["gcd"], ["warrior_whirlwind"])

        unknown_level_and_cooldowns = proposal(
            nearbyEnemies=3,
            targetLevelKnown=False,
            targetLevel=None,
            rage=55,
            bloodthirstCooldownKnown=False,
            whirlwindCooldownKnown=False,
        )
        self.assertEqual(
            unknown_level_and_cooldowns["queue"], ["warrior_cleave"]
        )
        self.assertEqual(unknown_level_and_cooldowns["gcd"], [])

        unknown = proposal(
            nearbyEnemiesKnown=False,
            queuedSwingKnown=False,
            bloodthirstCooldownKnown=False,
            whirlwindCooldownKnown=False,
        )
        self.assertEqual(unknown["queue"], [])
        self.assertEqual(unknown["gcd"], [])
        self.assertNotIn("warrior_o2o_cancel_queue", unknown["off_gcd"])


if __name__ == "__main__":
    unittest.main()
