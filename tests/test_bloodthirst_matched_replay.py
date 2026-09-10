from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.bloodthirst_matched_replay import (
    BLOODTHIRST_SPELL_ID,
    BATTLE_SHOUT_SPELL_ID,
    build_matched_request,
    infer_real_armor_penetration,
    replay_phase4,
    run_from_paths,
)
from o2o_dps.sim_bridge import ActionRef, ActResult, AvailableAction


def _summary(calibration_jsonl: str = "capture.jsonl") -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "source": {"calibration_jsonl": calibration_jsonl},
        "specialized_runs": [
            {
                "task_id": "warrior_bloodthirst_ap_strata_damage",
                "task_run_id": "phase4-run",
                "status": "completed",
                "completion_confirmed": True,
                "fixed_control": {
                    "target_guid": "target-guid",
                    "observed_target_armor": 4211,
                    "same_target_and_armor_all_valid_samples": True,
                },
                "attack_power_strata": {
                    "no_battle_shout": {
                        "valid_normal_hit_count": 4,
                        "attack_power": 1180,
                        "damages": [350, 350, 350, 350],
                    },
                    "battle_shout_observed_delta": {
                        "valid_normal_hit_count": 4,
                        "attack_power": 1470,
                        "damages": [407, 407, 408, 408],
                    },
                    "observed_attack_power_delta": 290,
                },
            }
        ],
    }


def _profile() -> dict[str, object]:
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "name": "warrior",
                            "equipment": {
                                "items": [
                                    {},
                                    {},
                                    {"id": 21330, "enchant": 3038},
                                    {},
                                    {},
                                    {},
                                    {},
                                    {},
                                    {"id": 61365, "enchant": 2543},
                                ]
                            },
                        }
                    ]
                }
            ]
        },
        "encounter": {"targets": [{"stats": [0] * 46}]},
        "simOptions": {"iterations": 100, "interactive": False},
    }


class _FakeBridge:
    def __init__(
        self,
        *,
        baseline_attack_power: float,
        armor_penetration: float,
        export_armor_penetration: bool = True,
    ) -> None:
        self.baseline_attack_power = baseline_attack_power
        self.armor_penetration = armor_penetration
        self.export_armor_penetration = export_armor_penetration
        self.request_records: list[dict[str, object]] = []
        self.seed = 0
        self.shouted = False
        self.damage_done = 0.0
        self.target_armor = 0.0

    def load(self, request: dict[str, object], seed: int) -> dict[str, object]:
        self.request_records.append(request)
        self.seed = seed
        self.shouted = False
        self.damage_done = 0.0
        self.target_armor = float(request["encounter"]["targets"][0]["stats"][26])
        return self._state()

    def advance(self) -> dict[str, object]:
        return self._state(time_ms=1500)

    def actions(self) -> list[AvailableAction]:
        return [
            AvailableAction(
                index=1,
                action=ActionRef(spell_id=BLOODTHIRST_SPELL_ID),
                label="Bloodthirst",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
            ),
            AvailableAction(
                index=2,
                action=ActionRef(spell_id=BATTLE_SHOUT_SPELL_ID),
                label="Battle Shout",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
            ),
        ]

    def act(self, action: ActionRef) -> ActResult:
        if action.spell_id == BATTLE_SHOUT_SPELL_ID:
            self.shouted = True
            return ActResult(
                casted=True,
                consumes_decision=True,
                finished=False,
                needs_input=False,
                state=self._state(),
            )
        if action.spell_id != BLOODTHIRST_SPELL_ID:
            raise AssertionError(action)
        attack_power = self.baseline_attack_power + (290 if self.shouted else 0)
        self.damage_done += round((200 + 0.35 * attack_power) * 0.57, 3)
        return ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state=self._state(),
        )

    def _state(self, *, time_ms: int = 0) -> dict[str, object]:
        result: dict[str, object] = {
            "time_ms": time_ms,
            "damage_done": self.damage_done,
            "strength": 361,
            "melee_attack_power": self.baseline_attack_power
            + (290 if self.shouted else 0),
            "target_armor": self.target_armor,
            "effective_target_armor": max(
                self.target_armor - self.armor_penetration, 0
            ),
            "power": {"type": "rage", "current": 100},
        }
        if self.export_armor_penetration:
            result["armor_penetration"] = self.armor_penetration
        return result


class BloodthirstMatchedReplayTests(unittest.TestCase):
    def test_build_request_applies_only_observed_phase4_controls(self) -> None:
        profile = _profile()
        request = build_matched_request(_summary(), profile)

        player = request["raid"]["parties"][0]["players"][0]
        self.assertTrue(player["buffs"]["rallyingCryOfTheDragonslayer"])
        self.assertEqual(request["encounter"]["targets"][0]["stats"][26], 4211)
        self.assertEqual(request["simOptions"]["iterations"], 1)
        self.assertTrue(request["simOptions"]["interactive"])
        self.assertNotIn("buffs", profile["raid"]["parties"][0]["players"][0])
        self.assertEqual(profile["encounter"]["targets"][0]["stats"][26], 0)

    def test_mismatch_blocks_formula_and_specialization_conclusions(self) -> None:
        bridge = _FakeBridge(baseline_attack_power=1194, armor_penetration=25)
        document = replay_phase4(
            _summary(),
            _profile(),
            bridge,
            seeds=[1, 2, 3],
            real_armor_penetration=65,
            real_armor_penetration_evidence={"status": "test"},
        )

        self.assertEqual(document["validation_status"], "not_matched")
        gate = document["conclusion_gate"]
        self.assertFalse(gate["environment_matched"])
        self.assertFalse(gate["formula_or_specialization_conclusion_allowed"])
        self.assertIsNone(gate["formula_conclusion"])
        self.assertIsNone(gate["specialization_conclusion"])
        failed = {
            check["field"]
            for check in document["environment_checks"]
            if not check["matched"]
        }
        self.assertEqual(
            failed,
            {
                "melee_attack_power.no_battle_shout",
                "melee_attack_power.battle_shout_observed_delta",
                "armor_penetration",
                "effective_target_armor",
            },
        )
        self.assertEqual(len(document["simulator_replay"]["rows"]), 3)
        self.assertEqual(bridge.request_records[0]["encounter"]["targets"][0]["stats"][26], 4211)

    def test_exact_exported_controls_pass_environment_gate(self) -> None:
        bridge = _FakeBridge(baseline_attack_power=1180, armor_penetration=65)
        document = replay_phase4(
            _summary(),
            _profile(),
            bridge,
            seeds=[7, 9],
            real_armor_penetration=65,
        )

        self.assertEqual(document["validation_status"], "matched")
        self.assertTrue(document["conclusion_gate"]["environment_matched"])
        self.assertTrue(
            document["conclusion_gate"][
                "formula_or_specialization_conclusion_allowed"
            ]
        )
        self.assertTrue(all(check["matched"] for check in document["environment_checks"]))
        self.assertEqual(
            document["simulator_replay"]["damage_summary"]["no_battle_shout"][
                "count"
            ],
            2,
        )

    def test_missing_simulator_state_is_not_silently_treated_as_a_match(self) -> None:
        bridge = _FakeBridge(
            baseline_attack_power=1180,
            armor_penetration=65,
            export_armor_penetration=False,
        )
        document = replay_phase4(
            _summary(),
            _profile(),
            bridge,
            seeds=[1],
            real_armor_penetration=65,
        )

        checks = {check["field"]: check for check in document["environment_checks"]}
        self.assertFalse(checks["armor_penetration"]["matched"])
        self.assertEqual(checks["armor_penetration"]["observed_simulator_values"], [None])
        self.assertEqual(document["validation_status"], "not_matched")

    def test_infers_65_armor_penetration_from_live_equipment_tooltips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            capture = root / "capture.jsonl"
            capture.write_text(
                json.dumps(
                    {
                        "event": "STATIC_PROFILE_CAPTURED",
                        "state": {
                            "equipment": [
                                {
                                    "slot": 3,
                                    "link": "item:21330:3038",
                                    "tooltipText": "肩部\n护甲穿透 +40",
                                },
                                {
                                    "slot": 7,
                                    "link": "item:61365:2543",
                                    "tooltipText": (
                                        "腿部\n装备：你的攻击无视目标25点护甲。\n"
                                        "梦境钢铁护甲（0/4）\n"
                                        "(4) 套装：你的攻击无视目标100点护甲。"
                                    ),
                                },
                            ]
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            summary_path = root / "summary.json"
            summary = _summary(str(capture))
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            amount, evidence = infer_real_armor_penetration(summary, summary_path)

        self.assertEqual(amount, 65)
        self.assertEqual(evidence["status"], "observed_equipment_tooltips")
        self.assertEqual(
            [component["amount"] for component in evidence["components"]],
            [40.0, 25.0],
        )

    def test_run_from_paths_carries_source_paths_and_inferred_live_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            capture = root / "capture.jsonl"
            capture.write_text(
                json.dumps(
                    {
                        "event": "STATIC_PROFILE_CAPTURED",
                        "state": {
                            "equipment": [
                                {"slot": 3, "tooltipText": "护甲穿透 +40"},
                                {"slot": 7, "tooltipText": "无视目标25点护甲"},
                            ]
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            summary_path = root / "summary.json"
            profile_path = root / "profile.json"
            summary_path.write_text(
                json.dumps(_summary(str(capture))), encoding="utf-8"
            )
            profile_path.write_text(json.dumps(_profile()), encoding="utf-8")

            document = run_from_paths(
                summary_path,
                profile_path,
                _FakeBridge(baseline_attack_power=1180, armor_penetration=65),
                seeds=[1],
            )

        self.assertEqual(document["real_environment"]["armor_penetration"], 65)
        self.assertEqual(document["validation_status"], "matched")
        self.assertEqual(
            document["sources"]["calibration_summary"], str(summary_path.resolve())
        )


if __name__ == "__main__":
    unittest.main()
