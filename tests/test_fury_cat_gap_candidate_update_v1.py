from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.fury_cat_gap_candidate_update_v1 import (
    FuryCatGapCandidateUpdateV1Error,
    SCHEMA,
    STATUS,
    build_next_candidate_registry_v1,
    validate_candidate_update_receipt_v1,
)
from o2o_dps.cat2new_fury_cat_gap_policy_v1 import PARAMETER_AXES
from o2o_dps.fury_cat_gap_search_plan_v1 import build_candidate_design_v1
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)


def _address(core: dict[str, object]) -> dict[str, object]:
    return {
        **deepcopy(core),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _development_reference_audit() -> dict[str, object]:
    references = []
    for policy_id in (CAT_POLICY_ID, CONTRA260817_POLICY_ID):
        references.append(
            {
                "policy_id": policy_id,
                "exact_policy_identity_matches": True,
                "exact_lane_contract_matches": True,
                "dynamic_v5_executable": True,
                "blocker_codes": [],
                "runtime_artifact_validation_required": True,
                "optimization_reference_eligible": True,
                "scientific_comparison_eligible": False,
            }
        )
    return {
        "schema": "fury_cat_gap_development_optimization_reference_audit/v1",
        "status": "DEVELOPMENT_OPTIMIZATION_REFERENCES_ELIGIBLE",
        "reference_policy_ids": [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
        "references": references,
        "optimization_reference_eligible": True,
        "scientific_comparison_eligible": False,
        "authority": (
            "EXACT_SOURCE_SIMULATOR_TRANSLATIONS_FOR_DEVELOPMENT_SEARCH_ONLY"
        ),
        "live_or_scientific_promotion_performed": False,
    }


def _reduction(
    stage_id: str,
    registry: list[dict[str, object]],
    retained_count: int,
) -> dict[str, object]:
    ranking = []
    for index, candidate in enumerate(registry):
        score = float(len(registry) - index)
        ranking.append(
            {
                "candidate_id": candidate["candidate_id"],
                "equal_instance_weighted_mean_by_baseline": {
                    CAT_POLICY_ID: score + 1.0,
                    CONTRA260817_POLICY_ID: score,
                },
                "dual_baseline_maximin_mean_dps": score,
            }
        )
    core = {
        "schema": "fury_cat_gap_variable_lane_reduction/v1",
        "status": "STAGE_COMPLETE_RETENTION_READY",
        "stage_id": stage_id,
        "execution_kind": "ADMITTED_OLD50_DEVELOPMENT_STAGE",
        "search_plan_sha256": "1" * 64,
        "execution_plan_sha256": "2" * 64,
        "dispatch_plan_sha256": "3" * 64,
        "expected_task_count": 1,
        "observed_unique_task_count": 1,
        "group_count": 1,
        "candidate_count": len(registry),
        "baseline_policy_ids": [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
        "development_reference_audit": _development_reference_audit(),
        "contrast_count": 2 * len(registry),
        "contrasts": {},
        "ranking_metric": (
            "dual_baseline_maximin_equal_instance_weighted_paired_mean_dps"
        ),
        "ranking_tie_break": "candidate_id_ascending",
        "candidate_ranking": ranking,
        "retained_candidate_ids": [
            row["candidate_id"] for row in ranking[:retained_count]
        ],
        "retention_allowed": True,
        "selection": None,
        "complete_accounting": True,
        "failure_status_if_any_lane_missing_duplicate_or_ineligible": (
            "STAGE_FAILED_NO_RETENTION"
        ),
        "heavy_execution_started": True,
        "simulator_only": True,
        "development_only": True,
        "old50_heldout_evidence": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


def _assert_adjacent_child(
    case: unittest.TestCase,
    parent: dict[str, object],
    child: dict[str, object],
) -> None:
    parent_parameters = parent["parameters"]
    child_parameters = child["parameters"]
    assert isinstance(parent_parameters, dict)
    assert isinstance(child_parameters, dict)
    changed = [
        name
        for name, _ in PARAMETER_AXES
        if parent_parameters[name] != child_parameters[name]
    ]
    case.assertEqual(1, len(changed))
    name = changed[0]
    allowed = dict(PARAMETER_AXES)[name]
    case.assertEqual(
        1,
        abs(
            allowed.index(parent_parameters[name])
            - allowed.index(child_parameters[name])
        ),
    )


class FuryCatGapCandidateUpdateV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.initial = [dict(row) for row in build_candidate_design_v1()]

    def test_stage_one_creates_deterministic_novel_next_population(self) -> None:
        reduction = _reduction("successive_halving_1", self.initial, 16)
        first = build_next_candidate_registry_v1(
            reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        repeated = build_next_candidate_registry_v1(
            reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        self.assertEqual(repeated, first)
        self.assertEqual(SCHEMA, first["schema"])
        self.assertEqual(STATUS, first["status"])
        self.assertEqual(16, first["candidate_count"])
        self.assertEqual(8, len(first["incumbent_candidate_ids"]))
        self.assertEqual(8, len(first["novel_candidate_ids"]))
        self.assertEqual(
            sha256_json(first["candidate_registry"]),
            first["candidate_registry_sha256"],
        )
        initial_ids = {row["candidate_id"] for row in self.initial}
        self.assertTrue(set(first["novel_candidate_ids"]).isdisjoint(initial_ids))
        self.assertEqual(16, len({row["candidate_id"] for row in first["candidate_registry"]}))
        by_id = {row["candidate_id"]: row for row in self.initial}
        children = {
            row["candidate_id"]: row for row in first["candidate_registry"][8:]
        }
        for lineage in first["candidate_lineage"]:
            _assert_adjacent_child(
                self,
                by_id[lineage["parent_candidate_id"]],
                children[lineage["candidate_id"]],
            )

    def test_second_update_adds_parameters_and_selection_freezes_evaluated_top_two(self) -> None:
        first_reduction = _reduction("successive_halving_1", self.initial, 16)
        first = build_next_candidate_registry_v1(
            first_reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        second_reduction = _reduction(
            "successive_halving_2", first["candidate_registry"], 4
        )
        second = build_next_candidate_registry_v1(
            second_reduction,
            first,
            next_stage_id="successive_halving_3",
            next_candidate_count=4,
        )
        third_reduction = _reduction(
            "successive_halving_3", second["candidate_registry"], 2
        )
        third = build_next_candidate_registry_v1(
            third_reduction,
            second,
            next_stage_id="selection_validation",
            next_candidate_count=2,
        )
        self.assertEqual(2, len(second["novel_candidate_ids"]))
        previously_seen = {
            row["parameter_sha256"] for row in self.initial
        } | {
            row["parameter_sha256"] for row in first["candidate_registry"]
        }
        second_novel = {
            row["parameter_sha256"]
            for row in second["candidate_registry"]
            if row["candidate_id"] in second["novel_candidate_ids"]
        }
        self.assertTrue(second_novel.isdisjoint(previously_seen))
        self.assertEqual([], third["novel_candidate_ids"])
        self.assertEqual([], third["candidate_lineage"])
        self.assertEqual(
            [row["candidate_id"] for row in second["candidate_registry"][:2]],
            third["incumbent_candidate_ids"],
        )
        self.assertEqual(
            third["incumbent_candidate_ids"],
            [row["candidate_id"] for row in third["candidate_registry"]],
        )
        self.assertEqual(
            "disabled at the selection boundary",
            third["update_rule"]["mutation"],
        )
        self.assertEqual(
            72,
            len(second["evaluated_parameter_sha256s"]),
        )
        self.assertEqual(
            74,
            len(third["evaluated_parameter_sha256s"]),
        )

    def test_rejects_incomplete_or_noncanonical_reduction(self) -> None:
        reduction = _reduction("successive_halving_1", self.initial, 16)
        broken_address = deepcopy(reduction)
        broken_address["complete_accounting"] = False
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "content address"
        ):
            build_next_candidate_registry_v1(
                broken_address,
                self.initial,
                next_stage_id="successive_halving_2",
                next_candidate_count=16,
            )

        incomplete_core = deepcopy(reduction)
        incomplete_core.pop("content_address")
        incomplete_core["complete_accounting"] = False
        incomplete = _address(incomplete_core)
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "incomplete"
        ):
            build_next_candidate_registry_v1(
                incomplete,
                self.initial,
                next_stage_id="successive_halving_2",
                next_candidate_count=16,
            )

        ineligible_core = deepcopy(reduction)
        ineligible_core.pop("content_address")
        ineligible_core["development_reference_audit"][
            "optimization_reference_eligible"
        ] = False
        ineligible = _address(ineligible_core)
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "references are absent or ineligible"
        ):
            build_next_candidate_registry_v1(
                ineligible,
                self.initial,
                next_stage_id="successive_halving_2",
                next_candidate_count=16,
            )

    def test_rejects_wrong_transition_and_unbound_later_registry(self) -> None:
        first_reduction = _reduction("successive_halving_1", self.initial, 16)
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "registered Cat-gap transition"
        ):
            build_next_candidate_registry_v1(
                first_reduction,
                self.initial,
                next_stage_id="successive_halving_3",
                next_candidate_count=4,
            )
        first = build_next_candidate_registry_v1(
            first_reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        second_reduction = _reduction(
            "successive_halving_2", first["candidate_registry"], 4
        )
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "preceding content-addressed"
        ):
            build_next_candidate_registry_v1(
                second_reduction,
                first["candidate_registry"],
                next_stage_id="successive_halving_3",
                next_candidate_count=4,
            )

    def test_receipt_validator_recomputes_exact_result(self) -> None:
        reduction = _reduction("successive_halving_1", self.initial, 16)
        receipt = build_next_candidate_registry_v1(
            reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        self.assertEqual(
            receipt,
            validate_candidate_update_receipt_v1(
                receipt,
                reduction,
                self.initial,
                next_stage_id="successive_halving_2",
                next_candidate_count=16,
            ),
        )
        changed = deepcopy(receipt)
        changed["candidate_lineage"][0]["elite_destination_count"] += 1
        with self.assertRaisesRegex(
            FuryCatGapCandidateUpdateV1Error, "differs"
        ):
            validate_candidate_update_receipt_v1(
                changed,
                reduction,
                self.initial,
                next_stage_id="successive_halving_2",
                next_candidate_count=16,
            )


if __name__ == "__main__":
    unittest.main()
