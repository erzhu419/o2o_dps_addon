from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_current_build_phase12_compare import (
    FuryPhase12ComparisonError,
    REVIEW_KEYS,
    SLAM_RUNTIME_TEST,
    build_phase12_comparison,
    run_from_paths,
)


def _review() -> dict:
    evidence_reviews = {}
    for key in REVIEW_KEYS:
        evidence_reviews[key] = {
            "question": f"question for {key}",
            "observed_fact": {
                "status": "OBSERVED",
                "claims": [f"observed claim for {key}"],
                "gate": "gate",
                "gate_status": "COMPLETE",
                "completed_support_stage_ids": [f"{key}_stage"],
                "historically_adjudicated_support_stage_ids": [],
                "incomplete_or_missing_stage_ids": [],
                "scope_limit": f"scope for {key}",
            },
            "simulator_comparison": {
                "status": "NOT_EVALUATED",
                "simulator_input_present": False,
                "observed_claims": [f"observed claim for {key}"],
                "simulator_value": None,
                "comparison_result": None,
            },
            "promotion_decision": {
                "decision": "PROMOTE_OBSERVED_FACT_ONLY",
                "unresolved": [f"unresolved {key}"],
                "mechanics_registry_modified": False,
                "simulator_modified": False,
                "simulator_override": None,
            },
        }
    return {
        "schema_version": 1,
        "kind": "fury_current_build_phase12_evidence_review",
        "evidence_reviews": evidence_reviews,
        "publication_gate": {
            "simulator_comparison_complete": False,
            "replacement_formula_identified": False,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
        },
        "mechanics_registry_mutations": [],
        "simulator_overrides": [],
        "mechanics_registry_modified": False,
        "simulator_modified": False,
    }


SOURCE_FILES = {
    "sim/warrior/slam.go": """
package warrior
var flags = core.SpellFlagCastWhileMoving
""",
    "sim/o2o/slam_test.go": f"""
package o2o
func {SLAM_RUNTIME_TEST}(t *testing.T) {{}}
""",
    "sim/warrior/whirlwind.go": """
package warrior
func registerWhirlwind() {
    results := make([]*core.SpellResult, min(4, warrior.Env.GetNumTargets()))
    for idx := range results {
        results[idx] = spell.CalcDamage(sim, target, 1, outcome)
    }
    for _, result := range results {
        spell.DealDamage(sim, result)
    }
}
""",
    "sim/warrior/stances.go": """
package warrior
func stance() {
    maxRetainedRage := 5 * float64(warrior.Talents.TacticalMastery)
    _ = maxRetainedRage
}
""",
    "sim/warrior/recklessness.go": """
package warrior
// Recklessness now increases critical strike chance by 50% (was 100%) and the duration is reduced to 12 seconds, but the cooldown is reduced to 5 minutes.
var reckAura = core.Aura{Duration: time.Second * 15}
var crit = 100*core.CritRatingPerCritChance
var reckCD = core.Cooldown{Duration: time.Minute * 30}
""",
    "sim/warrior/talents.go": """
package warrior
var deathWishAura = core.Aura{Duration: time.Second * 30}
func deathWish() {
    warrior.PseudoStats.SchoolDamageDealtMultiplier[stats.SchoolIndexPhysical] *= 1.2
}
func flurry() {
    attackSpeed := []float64{1.1, 1.15, 1.2, 1.25, 1.3}[points-1]
    warrior.MultiplyMeleeSpeed(sim, attackSpeed)
}
""",
    "sim/warrior/heroic_strike_cleave.go": """
package warrior
func queue() {
    queueSpell := warrior.RegisterSpell(AnyStance, core.SpellConfig{})
    warrior.PseudoStats.DisableDWMissPenalty = true
    warrior.PseudoStats.DisableDWMissPenalty = false
    _ = queueSpell
}
""",
    "sim/warrior/bloodrage.go": """
package warrior
func bloodrage() {
    actionID := core.ActionID{SpellID: 2687}
    instantRage := 10.0 + []float64{0, 2, 5}[points]
    options := core.PeriodicActionOptions{
        NumTicks: 10,
        Period:   time.Second * 1,
    }
    _, _, _ = actionID, instantRage, options
}
""",
}


def _write_wowsims(root: Path, *, whirlwind_distance_filter: bool = False) -> None:
    for relative, text in SOURCE_FILES.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == "sim/warrior/whirlwind.go" and whirlwind_distance_filter:
            text += "\nvar distance = warrior.DistanceFromTarget\n"
        destination.write_text(text, encoding="utf-8")


class FuryPhase12ComparisonTests(unittest.TestCase):
    def _build(
        self,
        root: Path,
        *,
        review: dict | None = None,
        go_executable: Path | None = None,
    ) -> dict:
        return build_phase12_comparison(
            review or _review(),
            review_path=root / "review.json",
            wowsims_root=root / "wowsims",
            go_executable=go_executable,
        )

    def test_without_go_only_whirlwind_is_decidable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims")

            document = self._build(root)

        self.assertEqual(tuple(document["comparisons"]), REVIEW_KEYS)
        self.assertEqual(
            document["comparisons"]["movement_slam"]["comparison_result"],
            "NOT_COMPARABLE",
        )
        self.assertEqual(
            document["comparisons"]["whirlwind_no_current_target_hit"][
                "comparison_result"
            ],
            "MISMATCH",
        )
        for key in REVIEW_KEYS[2:]:
            self.assertEqual(
                document["comparisons"][key]["comparison_result"],
                "NOT_COMPARABLE",
            )
        self.assertEqual(
            document["summary"]["result_counts"],
            {"MATCH": 0, "MISMATCH": 1, "NOT_COMPARABLE": 7},
        )
        self.assertFalse(
            document["publication_gate"]["simulator_comparison_complete"]
        )
        self.assertFalse(
            document["publication_gate"]["replacement_formula_identified"]
        )
        self.assertFalse(document["publication_gate"]["simulator_patch_allowed"])

    def test_exact_named_go_runtime_pass_is_required_for_slam_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims")
            go_executable = root / "go" / "bin" / "go.exe"
            go_executable.parent.mkdir(parents=True)
            go_executable.write_bytes(b"")
            completed = SimpleNamespace(
                returncode=0,
                stdout=(
                    f"=== RUN   {SLAM_RUNTIME_TEST}\n"
                    f"--- PASS: {SLAM_RUNTIME_TEST} (0.00s)\n"
                    "PASS\n"
                ),
                stderr="",
            )
            with patch(
                "o2o_dps.fury_current_build_phase12_compare.subprocess.run",
                return_value=completed,
            ) as run:
                document = self._build(root, go_executable=go_executable)

        movement = document["comparisons"]["movement_slam"]
        self.assertEqual(movement["comparison_result"], "MATCH")
        self.assertEqual(movement["deciding_channel"], "runtime_test")
        runtime = next(
            channel
            for channel in movement["evidence_channels"]
            if channel["channel"] == "runtime_test"
        )
        self.assertEqual(runtime["status"], "PASS")
        command = run.call_args.args[0]
        self.assertIn("--tags=with_db", command)
        self.assertIn(f"^{SLAM_RUNTIME_TEST}$", command)
        self.assertIn("-count=1", command)

    def test_failed_runtime_test_cannot_be_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims")
            go_executable = root / "go.exe"
            go_executable.write_bytes(b"")
            completed = SimpleNamespace(
                returncode=1,
                stdout=f"--- FAIL: {SLAM_RUNTIME_TEST} (0.00s)\n",
                stderr="compile failed\n",
            )
            with patch(
                "o2o_dps.fury_current_build_phase12_compare.subprocess.run",
                return_value=completed,
            ):
                document = self._build(root, go_executable=go_executable)

        movement = document["comparisons"]["movement_slam"]
        self.assertEqual(movement["comparison_result"], "NOT_COMPARABLE")
        self.assertEqual(document["summary"]["slam_runtime_test_status"], "FAIL")

    def test_whirlwind_mismatch_requires_recognized_no_distance_source_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims", whirlwind_distance_filter=True)

            document = self._build(root)

        whirlwind = document["comparisons"]["whirlwind_no_current_target_hit"]
        self.assertEqual(whirlwind["comparison_result"], "NOT_COMPARABLE")
        self.assertEqual(whirlwind["deciding_channel"], "interface_gap")

    def test_recklessness_comment_implementation_conflict_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims")

            document = self._build(root)

        comparison = document["comparisons"][
            "death_wish_recklessness_duration"
        ]
        self.assertEqual(comparison["comparison_result"], "NOT_COMPARABLE")
        self.assertTrue(
            comparison["simulator_value"]["source_internal_conflict"]
        )
        self.assertEqual(
            comparison["simulator_value"]["recklessness_comment"],
            {
                "crit_bonus_percent": 50,
                "duration_seconds": 12,
                "cooldown_minutes": 5,
            },
        )
        self.assertEqual(
            comparison["simulator_value"]["recklessness_implementation"],
            {
                "crit_bonus_percent": 100,
                "duration_seconds": 15,
                "cooldown_minutes": 30,
            },
        )
        source = next(
            channel
            for channel in comparison["evidence_channels"]
            if channel["channel"] == "source_model"
        )
        self.assertEqual(source["status"], "INTERNAL_CONFLICT")

    def test_rejects_mutating_or_incomplete_review_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_wowsims(root / "wowsims")
            review = _review()
            review["simulator_modified"] = True
            with self.assertRaisesRegex(
                FuryPhase12ComparisonError, "modifies the simulator"
            ):
                self._build(root, review=review)

            review = _review()
            review["evidence_reviews"].pop("movement_slam")
            with self.assertRaisesRegex(
                FuryPhase12ComparisonError, "exactly the eight"
            ):
                self._build(root, review=review)

    def test_run_from_paths_writes_separate_artifact_without_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wowsims_root = root / "wowsims"
            _write_wowsims(wowsims_root)
            review_path = root / "review.json"
            output = root / "comparison.json"
            review_path.write_text(json.dumps(_review()), encoding="utf-8")

            document = run_from_paths(
                review_path,
                wowsims_root=wowsims_root,
                output=output,
            )

            self.assertTrue(output.is_file())
            written = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(written["kind"], document["kind"])
        self.assertEqual(written["mechanics_registry_mutations"], [])
        self.assertEqual(written["simulator_overrides"], [])
        self.assertFalse(written["mechanics_registry_modified"])
        self.assertFalse(written["simulator_modified"])


if __name__ == "__main__":
    unittest.main()
