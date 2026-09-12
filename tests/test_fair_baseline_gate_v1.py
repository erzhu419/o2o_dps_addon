from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fair_baseline_gate_v1 import (
    FairBaselineGateV1Error,
    INPUT_SCHEMA,
    MATCHED_CONTRACT_FIELDS,
    REQUIRED_LANE_FAMILIES,
    audit_current_project_v1,
    evaluate_fair_baseline_gate_v1,
    main,
)


def matched_contract() -> dict[str, str]:
    return {
        "character_context_id": "character-a",
        "raid_context_id": "raid-a",
        "encounter_id": "encounter-a",
        "execution_model_id": "execution-a",
        "objective_id": "objective-a",
        "seed_set_id": "seeds-a",
    }


def complete_lane(family: str) -> dict[str, object]:
    return {
        "lane_family": family,
        "policy_id": family.lower(),
        "contract": matched_contract(),
        "artifact_complete": True,
        "full_controller": True,
        "source_identity_bound": True,
        "source_runtime_parity_proven": True,
        "comparison_ready": True,
        "seed_coverage_complete": True,
        "fixture_only": False,
        "historical_expert_cohort_proven": family == "HISTORICAL_EXPERT",
        "evidence_refs": [f"evidence/{family.lower()}.json"],
    }


def complete_case() -> dict[str, object]:
    return {
        "schema": INPUT_SCHEMA,
        "comparison_id": "fixture-comparison",
        "shared_contract": matched_contract(),
        "lanes": [complete_lane(family) for family in REQUIRED_LANE_FAMILIES],
    }


def blocker_codes(lane: dict[str, object]) -> set[str]:
    return {row["code"] for row in lane["blockers"]}  # type: ignore[index]


class FairBaselineGateV1Tests(unittest.TestCase):
    def test_complete_declarations_never_substitute_for_evidence_admission(self) -> None:
        result = evaluate_fair_baseline_gate_v1(complete_case())

        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertFalse(result["comparison_allowed"])
        self.assertEqual(0, result["admitted_lane_count"])
        self.assertEqual(5, result["declaration_complete_lane_count"])
        self.assertEqual(0, result["refused_lane_count"])
        self.assertTrue(
            all(
                row["status"] == "DECLARATION_COMPLETE_NOT_ADMITTED"
                for row in result["lanes"]
            )
        )
        self.assertIn(
            "EVIDENCE_ADMISSION_NOT_IMPLEMENTED_V1",
            {row["code"] for row in result["global_blockers"]},
        )

    def test_readable_contra_source_does_not_substitute_for_runtime_parity(self) -> None:
        case = complete_case()
        contra = case["lanes"][1]  # type: ignore[index]
        contra["source_identity_bound"] = True
        contra["source_runtime_parity_proven"] = False
        contra["evidence_refs"] = ["Contra_pirate.zip:readable-loaded-source-match"]

        result = evaluate_fair_baseline_gate_v1(case)
        lane = next(
            row for row in result["lanes"] if row["lane_family"] == "CONTRA_DEPLOYED"
        )

        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertIn("SOURCE_RUNTIME_PARITY_NOT_PROVEN", blocker_codes(lane))
        self.assertNotIn("SOURCE_IDENTITY_NOT_BOUND", blocker_codes(lane))

    def test_pooled_historical_clone_fixture_cannot_stand_for_top_expert(self) -> None:
        case = complete_case()
        historical = case["lanes"][3]  # type: ignore[index]
        historical["fixture_only"] = True
        historical["historical_expert_cohort_proven"] = False

        result = evaluate_fair_baseline_gate_v1(case)
        lane = next(
            row for row in result["lanes"] if row["lane_family"] == "HISTORICAL_EXPERT"
        )

        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertIn("FIXTURE_ONLY_LANE", blocker_codes(lane))
        self.assertIn("HISTORICAL_EXPERT_COHORT_NOT_PROVEN", blocker_codes(lane))

    def test_contract_and_seed_mismatch_are_fatal(self) -> None:
        case = complete_case()
        cat = case["lanes"][0]  # type: ignore[index]
        cat["contract"]["character_context_id"] = "other-character"  # type: ignore[index]
        cat["contract"]["seed_set_id"] = None  # type: ignore[index]
        cat["seed_coverage_complete"] = False

        result = evaluate_fair_baseline_gate_v1(case)
        lane = result["lanes"][0]
        codes = blocker_codes(lane)

        self.assertIn("MATCHED_CONTRACT_MISMATCH", codes)
        self.assertIn("MATCHED_CONTRACT_NOT_BOUND", codes)
        self.assertIn("SEED_COVERAGE_INCOMPLETE", codes)

    def test_missing_and_duplicate_lanes_refuse_without_hiding_rows(self) -> None:
        case = complete_case()
        case["lanes"] = case["lanes"][:-1] + [deepcopy(case["lanes"][0])]  # type: ignore[index]

        result = evaluate_fair_baseline_gate_v1(case)
        codes = {row["code"] for row in result["global_blockers"]}

        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertEqual(
            {
                "MISSING_REQUIRED_LANES",
                "DUPLICATE_REQUIRED_LANES",
                "EVIDENCE_ADMISSION_NOT_IMPLEMENTED_V1",
            },
            codes,
        )
        self.assertEqual(2, sum(row["lane_family"] == "CAT" for row in result["lanes"]))

    def test_malformed_boolean_is_rejected_instead_of_coerced(self) -> None:
        case = complete_case()
        case["lanes"][0]["full_controller"] = 1  # type: ignore[index]

        with self.assertRaisesRegex(FairBaselineGateV1Error, "must be boolean"):
            evaluate_fair_baseline_gate_v1(case)

    def test_current_repository_formal_artifacts_refuse_comparison(self) -> None:
        result = audit_current_project_v1()
        lanes = {row["lane_family"]: row for row in result["lanes"]}
        global_codes = {row["code"] for row in result["global_blockers"]}
        character_context = result["audit_metadata"]["runtime_character_context"]

        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertFalse(result["comparison_allowed"])
        self.assertIn("STALE_OR_MISMATCHED_CHARACTER_CONTEXT", global_codes)
        self.assertEqual(
            "STALE_OR_MISMATCHED_CHARACTER_CONTEXT", character_context["status"]
        )
        self.assertFalse(character_context["matches"])
        self.assertTrue(
            character_context["protocol_runtime_snapshot_sha256"].startswith("4c70ae")
        )
        self.assertTrue(
            character_context["deployed_contra_runtime_snapshot_sha256"].startswith(
                "e52f07"
            )
        )
        self.assertNotIn(
            "SOURCE_IDENTITY_NOT_BOUND", blocker_codes(lanes["CONTRA_DEPLOYED"])
        )
        self.assertIn(
            "SOURCE_RUNTIME_PARITY_NOT_PROVEN",
            blocker_codes(lanes["CONTRA_DEPLOYED"]),
        )
        self.assertIn("FIXTURE_ONLY_LANE", blocker_codes(lanes["HISTORICAL_EXPERT"]))
        self.assertEqual(
            "ABSENT", result["audit_metadata"]["matched_five_lane_result_report"]
        )

    def test_cli_evaluates_fixture_and_writes_versioned_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "case.json"
            output_path = root / "gate.json"
            input_path.write_text(json.dumps(complete_case()), encoding="utf-8")

            self.assertEqual(
                0,
                main(["--input", str(input_path), "--output", str(output_path)]),
            )
            result = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual("fair_baseline_gate/v1", result["schema"])
        self.assertEqual("REFUSE_COMPARISON", result["decision"])
        self.assertFalse(result["comparison_allowed"])
        self.assertEqual(0, result["admitted_lane_count"])
        self.assertEqual(5, result["declaration_complete_lane_count"])
        self.assertEqual(list(MATCHED_CONTRACT_FIELDS), result["matched_contract_fields"])


if __name__ == "__main__":
    unittest.main()
