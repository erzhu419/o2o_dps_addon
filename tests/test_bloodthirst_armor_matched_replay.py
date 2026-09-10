from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.bloodthirst_armor_matched_replay import (
    ArmorMatchedReplayError,
    BLOODTHIRST_SPELL_ID,
    build_stratum_request,
    continuous_bloodthirst_normal_damage,
    replay_phase5,
    run_from_paths,
)
from o2o_dps.sim_bridge import ActionRef, ActResult, AvailableAction


def _strata() -> dict[str, dict[str, object]]:
    return {
        "sunder_0": {
            "planned_sunder_stacks": 0,
            "observed_sunder_stacks": 0,
            "valid_normal_hit_count": 4,
            "attack_power": 1180,
            "observed_target_armor": 4211,
            "armor_reduction_from_baseline": 0,
            "damages": [349, 350, 350, 349],
            "mean_damage": 349.5,
        },
        "sunder_1": {
            "planned_sunder_stacks": 1,
            "observed_sunder_stacks": 1,
            "valid_normal_hit_count": 4,
            "attack_power": 1180,
            "observed_target_armor": 3761,
            "armor_reduction_from_baseline": 450,
            "damages": [366, 367, 367, 366],
            "mean_damage": 366.5,
        },
        "sunder_3": {
            "planned_sunder_stacks": 3,
            "observed_sunder_stacks": 3,
            "valid_normal_hit_count": 4,
            "attack_power": 1180,
            "observed_target_armor": 2861,
            "armor_reduction_from_baseline": 1350,
            "damages": [406, 407, 406, 407],
            "mean_damage": 406.5,
        },
        "sunder_5": {
            "planned_sunder_stacks": 5,
            "observed_sunder_stacks": 5,
            "valid_normal_hit_count": 4,
            "attack_power": 1180,
            "observed_target_armor": 1961,
            "armor_reduction_from_baseline": 2250,
            "damages": [455, 456, 456, 455],
            "mean_damage": 455.5,
        },
    }


def _summary(calibration_jsonl: str = "capture.jsonl") -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "source": {"calibration_jsonl": calibration_jsonl},
        "specialized_runs": [
            {
                "task_id": "warrior_bloodthirst_armor_strata_damage",
                "task_run_id": "phase5-run",
                "analyzer": "bloodthirst_armor_strata_damage_v1",
                "status": "completed",
                "completion_confirmed": True,
                "fixed_control": {
                    "target_guid": "target-guid",
                    "attack_power": 1180,
                    "baseline_target_armor": 4211,
                    "same_target_and_attack_power_all_valid_samples": True,
                },
                "armor_strata": _strata(),
                "armor_floor_status": "NOT_REACHED",
                "armor_zero_claim": False,
            }
        ],
    }


def _profile() -> dict[str, object]:
    return {
        "raid": {
            "parties": [{"players": [{"name": "warrior"}]}],
            "debuffs": {
                "sunderArmor": True,
                "exposeArmor": "TristateEffectImproved",
                "faerieFire": True,
                "curseOfRecklessness": True,
                "crystalYield": True,
            },
        },
        "encounter": {"targets": [{"stats": [0] * 46}]},
        "simOptions": {"iterations": 100, "interactive": False},
    }


class _FakeBridge:
    def __init__(
        self,
        *,
        attack_power: float = 1180,
        armor_penetration: float = 65,
        target_auras: list[dict[str, object]] | None = None,
    ) -> None:
        self.attack_power = attack_power
        self.armor_penetration = armor_penetration
        self.target_auras = [] if target_auras is None else target_auras
        self.request_records: list[dict[str, object]] = []
        self.target_armor = 0.0
        self.damage_done = 0.0

    def load(self, request: dict[str, object], seed: int) -> dict[str, object]:
        self.request_records.append(request)
        self.target_armor = float(request["encounter"]["targets"][0]["stats"][26])
        self.damage_done = 0.0
        return self._state()

    def actions(self) -> list[AvailableAction]:
        return [
            AvailableAction(
                index=1,
                action=ActionRef(spell_id=BLOODTHIRST_SPELL_ID),
                label="Bloodthirst",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
            )
        ]

    def act(self, action: ActionRef) -> ActResult:
        if action.spell_id != BLOODTHIRST_SPELL_ID:
            raise AssertionError(action)
        self.damage_done += continuous_bloodthirst_normal_damage(
            self.attack_power,
            max(self.target_armor - self.armor_penetration, 0),
        )
        return ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state=self._state(),
        )

    def _state(self) -> dict[str, object]:
        return {
            "time_ms": 0,
            "damage_done": self.damage_done,
            "strength": 361,
            "melee_attack_power": self.attack_power,
            "armor_penetration": self.armor_penetration,
            "target_armor": self.target_armor,
            "effective_target_armor": max(
                self.target_armor - self.armor_penetration, 0
            ),
            "power": {"type": "rage", "current": 100},
            "target_auras": self.target_auras,
        }


class BloodthirstArmorMatchedReplayTests(unittest.TestCase):
    def test_request_assigns_observed_armor_and_disables_all_armor_debuffs(self) -> None:
        profile = _profile()
        request = build_stratum_request(profile, 2861)

        self.assertEqual(request["encounter"]["targets"][0]["stats"][26], 2861)
        self.assertTrue(
            request["raid"]["parties"][0]["players"][0]["buffs"][
                "rallyingCryOfTheDragonslayer"
            ]
        )
        self.assertEqual(
            request["raid"]["debuffs"],
            {
                "sunderArmor": False,
                "exposeArmor": "TristateEffectMissing",
                "faerieFire": False,
                "curseOfRecklessness": False,
                "crystalYield": False,
            },
        )
        self.assertEqual(profile["encounter"]["targets"][0]["stats"][26], 0)
        self.assertTrue(profile["raid"]["debuffs"]["sunderArmor"])

    def test_exact_controls_and_floor_ceil_comparisons_match(self) -> None:
        bridge = _FakeBridge()
        document = replay_phase5(
            _summary(),
            _profile(),
            bridge,
            seeds=[3, 7],
            real_armor_penetration=65,
            real_armor_penetration_evidence={"status": "test"},
        )

        self.assertEqual(document["validation_status"], "matched")
        self.assertTrue(document["conclusion_gate"]["environment_matched"])
        self.assertTrue(
            document["conclusion_gate"][
                "all_live_integer_damages_in_prediction_floor_ceil_sets"
            ]
        )
        self.assertFalse(document["armor_zero_claim"])
        self.assertEqual(document["armor_zero_evidence"]["minimum_observed_target_armor"], 1961)
        self.assertEqual(len(bridge.request_records), 8)

        predictions = {
            row["stratum"]: row["continuous_normal_hit_prediction"]["damage"]
            for row in document["armor_strata"]
        }
        self.assertTrue(math.isclose(predictions["sunder_0"], 349.52311839104294))
        self.assertTrue(math.isclose(predictions["sunder_5"], 455.85451595457))
        for row in document["armor_strata"]:
            self.assertTrue(row["real_integer_comparison"]["all_in_floor_ceil_set"])
            self.assertEqual(
                row["real_integer_comparison"]["maximum_error_to_floor_ceil_set"],
                0,
            )
            self.assertFalse(row["request_controls"]["sunder_action_used"])
            self.assertEqual(row["request_controls"]["actions"], [{"spell_id": 23894}])

    def test_control_mismatch_and_simulator_sunder_aura_block_gate(self) -> None:
        bridge = _FakeBridge(
            attack_power=1190,
            armor_penetration=25,
            target_auras=[{"label": "Sunder Armor", "action": {"spell_id": 11597}}],
        )
        document = replay_phase5(
            _summary(),
            _profile(),
            bridge,
            seeds=[1],
            real_armor_penetration=65,
        )

        self.assertEqual(document["validation_status"], "not_matched")
        self.assertFalse(document["conclusion_gate"]["damage_comparison_allowed"])
        failed_fields = {
            check["field"]
            for check in document["environment_checks"]
            if not check["matched"]
        }
        self.assertEqual(
            failed_fields,
            {
                "melee_attack_power",
                "armor_penetration",
                "effective_target_armor",
                "simulator_armor_debuff_auras",
            },
        )

    def test_armor_zero_claim_requires_a_direct_zero_observation(self) -> None:
        summary = _summary()
        final_stratum = summary["specialized_runs"][0]["armor_strata"]["sunder_5"]
        final_stratum["observed_target_armor"] = 0
        final_stratum["armor_reduction_from_baseline"] = 4211
        final_stratum["damages"] = [613, 613, 613, 613]
        final_stratum["mean_damage"] = 613
        summary["specialized_runs"][0]["armor_floor_status"] = "OBSERVED_ZERO"
        summary["specialized_runs"][0]["armor_zero_claim"] = True

        document = replay_phase5(
            summary,
            _profile(),
            _FakeBridge(),
            seeds=[1],
            real_armor_penetration=65,
        )

        self.assertTrue(document["armor_zero_claim"])
        self.assertTrue(document["armor_zero_evidence"]["observed_zero_target_armor"])
        self.assertEqual(document["armor_zero_evidence"]["minimum_observed_target_armor"], 0)

    def test_rejects_wrong_analyzer_and_inconsistent_reduction(self) -> None:
        wrong_analyzer = _summary()
        wrong_analyzer["specialized_runs"][0]["analyzer"] = "generic"
        with self.assertRaisesRegex(ArmorMatchedReplayError, "analyzer"):
            replay_phase5(
                wrong_analyzer,
                _profile(),
                _FakeBridge(),
                seeds=[1],
                real_armor_penetration=65,
            )

        wrong_reduction = _summary()
        wrong_reduction["specialized_runs"][0]["armor_strata"]["sunder_1"][
            "armor_reduction_from_baseline"
        ] = 451
        with self.assertRaisesRegex(ArmorMatchedReplayError, "reduction"):
            replay_phase5(
                wrong_reduction,
                _profile(),
                _FakeBridge(),
                seeds=[1],
                real_armor_penetration=65,
            )

    def test_requires_actual_keyed_object_schema(self) -> None:
        summary = _summary()
        summary["specialized_runs"][0]["armor_strata"] = list(_strata().values())
        with self.assertRaisesRegex(ArmorMatchedReplayError, "keyed by stratum"):
            replay_phase5(
                summary,
                _profile(),
                _FakeBridge(),
                seeds=[1],
                real_armor_penetration=65,
            )

    def test_run_from_paths_infers_live_armor_penetration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            capture_path = root / "capture.jsonl"
            capture_path.write_text(
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
                json.dumps(_summary(str(capture_path))), encoding="utf-8"
            )
            profile_path.write_text(json.dumps(_profile()), encoding="utf-8")

            document = run_from_paths(
                summary_path,
                profile_path,
                _FakeBridge(),
                seeds=[1],
            )

        self.assertEqual(document["real_environment"]["armor_penetration"], 65)
        self.assertEqual(document["validation_status"], "matched")
        self.assertEqual(
            document["sources"]["calibration_summary"], str(summary_path.resolve())
        )


if __name__ == "__main__":
    unittest.main()
