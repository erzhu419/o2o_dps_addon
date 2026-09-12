from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps import fury_cat_gap_search_plan_v1 as search
from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json


CAPSULE = {"content_address": {"sha256": "a" * 64}, "scenarios": []}
EQUIPPED = {"equipped_item_names": ["Bonereaver's Edge"]}
CAPSULE_BUNDLE_SHA = "1" * 64
CAPSULE_FILE_SHA = "2" * 64
REQUEST_FILE_SHA = "3" * 64
EQUIPPED_FILE_SHA = "4" * 64
MATERIALIZED_SHA = "5" * 64


def _negative_observation() -> dict:
    return {
        "schema": "fury_cat2_horizon_v2_diagnostic_result_observation/v1",
        "confirmation_id": search.EXPECTED_HORIZON_CONFIRMATION_ID,
        "status": "COMPLETE_NO_SELECTION_NEGATIVE_RESULT_PRESERVED",
        "receipt_counts": {arm_id: 256 for arm_id in search.EXPECTED_HORIZON_ARMS},
        "analysis_identity": {
            "analysis_contract_sha256": search.EXPECTED_HORIZON_ANALYSIS_CONTRACT_SHA256
        },
        "remote_results": {"compact_sha256": search.EXPECTED_HORIZON_COMPACT_SHA256},
        "selection_gate": {
            "status": "NO_SELECTION",
            "selected_arm_id": None,
            "passing_arm_ids": [],
        },
        "contrasts": [
            {"arm_id": arm_id, "baseline_policy_id": baseline_id}
            for arm_id in search.EXPECTED_HORIZON_ARMS
            for baseline_id in search.BASELINE_POLICY_IDS
        ],
        "interpretation": {
            "candidate_beats_both_baselines": False,
            "same_seed_extension_or_retuning_allowed": False,
            "policy_promotion_performed": False,
            "deployment_allowed": False,
        },
    }


def _fixed_request() -> dict:
    items = [{} for _ in range(17)]
    items[14] = {
        "id": search.FIXED_MAIN_HAND_ITEM_ID,
        "enchant": search.FIXED_MAIN_HAND_ENCHANT_ID,
    }
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "name": "FuryResearchCharacter",
                            "race": "RaceGnome",
                            "class": "ClassWarrior",
                            "equipment": {"items": items},
                            "talentsString": search.FIXED_TALENTS,
                            "rotation": {},
                            "reactionTimeMs": 150,
                            "distanceFromTarget": 5,
                            "warrior": {
                                "options": {
                                    "startingRage": 100,
                                    "stance": "WarriorStanceBerserker",
                                    "ravagerRank": 2,
                                }
                            },
                        }
                    ],
                    "buffs": {},
                }
            ],
            "buffs": {},
            "debuffs": {},
        },
        "encounter": {"duration": 300, "targets": [{"level": 63}]},
        "simOptions": {"iterations": 1, "randomSeed": "20260828"},
    }


def _reseal(plan: dict) -> dict:
    result = deepcopy(plan)
    result.pop("plan_sha256", None)
    result["plan_sha256"] = sha256_json(result)
    return result


def _readdress_binding(binding: dict) -> dict:
    result = deepcopy(binding)
    core = deepcopy(result)
    core.pop("content_address", None)
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    return result


def _source_reader(
    request: dict | None = None, *, capsule_file_sha: str = CAPSULE_FILE_SHA
):
    documents = {
        "old-50 capsule bundle": (deepcopy(CAPSULE), capsule_file_sha),
        "base simulator request": (
            deepcopy(request or _fixed_request()),
            REQUEST_FILE_SHA,
        ),
        "equipped names receipt": (deepcopy(EQUIPPED), EQUIPPED_FILE_SHA),
    }

    def read(_path: str | Path, label: str):
        return deepcopy(documents[label])

    return read


def _bind(blueprint: dict, *, request: dict | None = None) -> dict:
    with patch.object(
        search,
        "_sha256_file_bytes",
        return_value=search.EXPECTED_OVERLAY_MANIFEST_FILE_SHA256,
    ), patch.object(
        search,
        "_read_json_source_path",
        side_effect=_source_reader(request),
    ), patch.object(
        search.adapter_v1,
        "load_formal_exact_fury_overlay_corpus_v1",
        return_value=SimpleNamespace(
            manifest={
                "content_address": {
                    "sha256": search.EXPECTED_OVERLAY_MANIFEST_SHA256
                }
            }
        ),
    ), patch.object(
        search.adapter_v1,
        "build_validated_old50_capsule_index_v1",
        return_value=SimpleNamespace(bundle_content_sha256=CAPSULE_BUNDLE_SHA),
    ):
        return search.bind_prematerialization_environment_v1(
            blueprint,
            overlay_manifest_path=Path("formal-overlay") / "manifest.json",
            capsule_path=Path("capsule.json.gz"),
            base_request_path=Path("fury_warrior_live.json"),
            equipped_names_receipt_path=Path("fury_warrior_live.equipped_names.json"),
            target_level=63,
            initial_base_armor=4211,
            health_branch_selection_policy=(
                search.adapter_v1.HEALTH_BRANCH_FALLBACK_POLICY_V1
            ),
            attackability_branch_id="observed_hostile_activity_proxy",
            target_classification="elite",
            armor_magnitude_by_debuff={"expose_armor": 1700},
        )


def _strict_manifest(environment_plan: dict) -> dict:
    binding = environment_plan["pre_materialization_environment_binding"]
    generic_lane = search.selector_v2.GENERIC_LANE
    pdf_lane = search.selector_v2.PDF_LANE
    return {
        "schema": search.adapter_v1.MATERIALIZED_MANIFEST_SCHEMA,
        "revision": search.adapter_v1.REVISION,
        "status": search.adapter_v1.MATERIALIZED_MANIFEST_STATUS,
        "source_bindings": deepcopy(
            binding["strict_manifest_expected_source_bindings"]
        ),
        "materialization_parameters": deepcopy(binding["materialization_parameters"]),
        "summary": {
            "instance_count": 20,
            "compiled_wave_count": search.EXPECTED_TOTAL_WAVES,
            "generic_outcome_free_wave_count": search.EXPECTED_GENERIC_WAVES,
            "pdf_outcome_conditioned_wave_count": search.EXPECTED_PDF_WAVES,
            "health_branch_selection_counts": deepcopy(
                search.adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
            ),
        },
        "instances": [
            *(
                {"selection_lane": generic_lane}
                for _ in range(search.EXPECTED_GENERIC_INSTANCES)
            ),
            *(
                {"selection_lane": pdf_lane}
                for _ in range(search.EXPECTED_PDF_INSTANCES)
            ),
        ],
        "content_address": {"sha256": MATERIALIZED_SHA},
    }


def _admit(environment_plan: dict, *, manifest: dict | None = None) -> dict:
    with patch.object(
        search,
        "_sha256_file_bytes",
        return_value=search.EXPECTED_OVERLAY_MANIFEST_FILE_SHA256,
    ), patch.object(
        search,
        "_read_json_source_path",
        side_effect=_source_reader(),
    ), patch.object(
        search.adapter_v1,
        "build_validated_old50_capsule_index_v1",
        return_value=SimpleNamespace(bundle_content_sha256=CAPSULE_BUNDLE_SHA),
    ), patch.object(
        search.adapter_v1,
        "validate_materialized_exact_fury_dynamic_v3_corpus_v1",
        return_value=manifest or _strict_manifest(environment_plan),
    ) as strict_validator:
        admitted = search.admit_real_adapter_artifacts_v1(
            environment_plan,
            materialized_manifest_path=Path("materialized") / "manifest.json",
            overlay_manifest_path=Path("formal-overlay") / "manifest.json",
            capsule_path=Path("capsule.json.gz"),
            base_request_path=Path("fury_warrior_live.json"),
            equipped_names_receipt_path=Path("fury_warrior_live.equipped_names.json"),
        )
    strict_validator.assert_called_once()
    return admitted


class FuryCatGapSearchPlanV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.blueprint = search.build_cat_gap_search_blueprint_v1(
            _negative_observation()
        )

    def test_blueprint_is_exact_frozen_search_contract(self) -> None:
        plan = search.validate_cat_gap_search_plan_v1(self.blueprint)
        self.assertEqual(search.STATUS_BLOCKED, plan["status"])
        self.assertEqual(13, plan["parameter_design"]["dimension_count"])
        self.assertEqual(
            2_571_912_000,
            plan["parameter_design"]["admissible_lattice_point_count"],
        )
        self.assertEqual(64, len(plan["parameter_design"]["candidates"]))
        self.assertEqual(240_576, plan["cost"]["successive_halving_policy_rollouts"])
        self.assertEqual(
            591_808, plan["cost"]["total_through_old50_selection_validation"]
        )
        self.assertFalse(plan["scientific_boundary"]["deployment_allowed"])
        self.assertTrue(
            plan["scientific_boundary"]["historical_health_proxy_availability_used"]
        )
        self.assertFalse(
            plan["scientific_boundary"][
                "candidate_or_simulator_outcome_used_for_health_selection"
            ]
        )
        self.assertEqual(
            search.adapter_v1.HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1,
            plan["adapter_admission"]["health_branch_selection_policy_receipt"],
        )
        self.assertEqual(
            "POLICY_READY_VARIABLE_LANE_WORKER_PENDING",
            plan["candidate_executor_contract"]["status"],
        )
        self.assertEqual([], plan["candidate_executor_contract"]["missing_runtime_axes"])

    def test_approved_13d_policy_pair_and_runtime_guard_are_bound(self) -> None:
        contract = self.blueprint["candidate_executor_contract"]
        approved = contract["approved_policy"]
        self.assertTrue(contract["policy_ready"])
        self.assertFalse(contract["variable_lane_worker_ready"])
        self.assertEqual(
            [name for name, _ in search.PARAMETER_AXES],
            contract["implemented_runtime_axes"],
        )
        self.assertEqual(search.APPROVED_CAT_GAP_POLICY_MODULE, approved["module"])
        self.assertEqual(
            search.APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
            hashlib.sha256(
                (search.PROJECT_ROOT / approved["source_path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            search.APPROVED_CAT_GAP_POLICY_TEST_SHA256,
            hashlib.sha256(
                (search.PROJECT_ROOT / approved["test_path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertTrue(
            contract[
                "each_axis_has_at_least_one_predeclared_reachable_policy_family_witness"
            ]
        )
        self.assertTrue(contract["local_parameter_interaction_inactivity_allowed"])
        self.assertFalse(contract["complete_behavior_duplicate_candidates_allowed"])
        self.assertNotIn(
            "all_13_axes_must_change_runtime_decisions_when_their_guard_is_reached",
            contract,
        )

    def test_planner_parameter_membership_is_exact_type(self) -> None:
        for replacement in (35.0, True):
            with self.subTest(replacement=replacement):
                parameters = deepcopy(
                    self.blueprint["parameter_design"]["candidates"][0]["parameters"]
                )
                parameters["heroic_strike_base_rage"] = replacement
                with self.assertRaisesRegex(
                    search.FuryCatGapSearchPlanV1Error, "outside the frozen axis"
                ):
                    search._validate_parameters(parameters)

    def test_resealed_policy_binding_or_guard_drift_is_rejected(self) -> None:
        source = deepcopy(self.blueprint)
        source["candidate_executor_contract"]["approved_policy"][
            "source_sha256"
        ] = "0" * 64
        guard = deepcopy(self.blueprint)
        guard["candidate_executor_contract"][
            "complete_behavior_duplicate_candidates_allowed"
        ] = True
        for tampered in (source, guard):
            with self.assertRaisesRegex(
                search.FuryCatGapSearchPlanV1Error,
                "reconstructed canonical contract",
            ):
                search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_real_capsule_input_surface_reads_json_gzip_and_hashes_file_bytes(self) -> None:
        document = {"content_address": {"sha256": "a" * 64}, "scenarios": []}
        payload = gzip.compress(
            json.dumps(document, sort_keys=True).encode("utf-8"), mtime=0
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capsule.json.gz"
            path.write_bytes(payload)
            observed, file_sha = search._read_json_source_path(path, "capsule")
        self.assertEqual(document, observed)
        self.assertEqual(64, len(file_sha))

    def test_canonical_reconstruction_rejects_every_critical_subtree_tamper(self) -> None:
        def fixed(plan):
            plan["fixed_player_domain"]["weapon_mode"] = "DUAL_WIELD"

        def halving(plan):
            plan["successive_halving"]["stages"][0]["candidate_count"] = 63

        def selection(plan):
            plan["selection_validation"]["master_seed_count"] = 255

        def future(plan):
            plan["future_confirmation_reservation"]["run_now"] = True

        def sampling(plan):
            plan["scenario_sampling_contract"]["stage_counts"][0] = 41

        def cost(plan):
            plan["cost"]["total_through_old50_selection_validation"] -= 1

        def gate(plan):
            plan["execution_gate"]["local_process_limit"] = 32

        def baseline(plan):
            plan["baseline_contract"]["policy_ids"].pop()

        def seeds(plan):
            plan["seed_contract"]["phases"]["successive_halving_1"][
                "master_seeds"
            ][0] = 513

        def executor(plan):
            plan["candidate_executor_contract"]["ready"] = True

        for label, mutate in (
            ("fixed", fixed),
            ("halving", halving),
            ("selection", selection),
            ("future", future),
            ("sampling", sampling),
            ("cost", cost),
            ("gate", gate),
            ("baseline", baseline),
            ("seeds", seeds),
            ("executor", executor),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(self.blueprint)
                mutate(tampered)
                with self.assertRaisesRegex(
                    search.FuryCatGapSearchPlanV1Error,
                    "reconstructed canonical contract",
                ):
                    search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_exact_candidate_design_rejects_ids_hashes_origins_and_duplicates(self) -> None:
        cases = []
        changed = deepcopy(self.blueprint)
        changed["parameter_design"]["candidates"][0]["parameters"][
            "heroic_strike_base_rage"
        ] = 40
        cases.append(("parameters_only", changed))

        rederived = deepcopy(self.blueprint)
        candidate = rederived["parameter_design"]["candidates"][0]
        candidate["parameters"]["heroic_strike_base_rage"] = 40
        identity = sha256_json(candidate["parameters"])
        candidate["parameter_sha256"] = identity
        candidate["candidate_id"] = f"cat-gap-v1-{identity[:16]}"
        cases.append(("fully_rederived_noncanonical_point", rederived))

        duplicate = deepcopy(self.blueprint)
        duplicate["parameter_design"]["candidates"][1]["parameters"] = deepcopy(
            duplicate["parameter_design"]["candidates"][0]["parameters"]
        )
        identity = sha256_json(
            duplicate["parameter_design"]["candidates"][1]["parameters"]
        )
        duplicate["parameter_design"]["candidates"][1]["parameter_sha256"] = identity
        duplicate["parameter_design"]["candidates"][1]["candidate_id"] = (
            f"cat-gap-v1-{identity[:16]}-duplicate"
        )
        cases.append(("duplicate_parameters_unique_label", duplicate))

        origin = deepcopy(self.blueprint)
        origin["parameter_design"]["candidates"][0]["origin"] = "ww_wait_winner"
        cases.append(("origin", origin))
        reordered = deepcopy(self.blueprint)
        reordered["parameter_design"]["candidates"][0:2] = reversed(
            reordered["parameter_design"]["candidates"][0:2]
        )
        cases.append(("order", reordered))

        for label, tampered in cases:
            with self.subTest(label=label):
                tampered["parameter_design"]["candidate_design_sha256"] = sha256_json(
                    tampered["parameter_design"]["candidates"]
                )
                with self.assertRaisesRegex(
                    search.FuryCatGapSearchPlanV1Error,
                    "reconstructed canonical contract",
                ):
                    search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_environment_binding_freezes_complete_sources_and_health_policy(self) -> None:
        bound = search.validate_cat_gap_search_plan_v1(_bind(self.blueprint))
        self.assertEqual(search.STATUS_ENVIRONMENT_BOUND, bound["status"])
        binding = bound["pre_materialization_environment_binding"]
        self.assertEqual(
            CAPSULE_BUNDLE_SHA,
            binding["source_inputs"]["capsule_bundle"]["bundle_content_sha256"],
        )
        self.assertEqual(
            CAPSULE_FILE_SHA,
            binding["source_inputs"]["capsule_bundle"]["file_sha256"],
        )
        self.assertEqual(_fixed_request(), binding["canonical_base_request"])
        self.assertEqual(
            _fixed_request()["raid"]["parties"][0]["players"][0],
            binding["complete_player_semantic_identity"]["semantic"],
        )
        self.assertEqual(
            search.adapter_v1.HEALTH_BRANCH_FALLBACK_POLICY_V1,
            binding["materialization_parameters"]["health_branch_selection_policy"],
        )
        self.assertEqual(
            {"expose_armor": 1700},
            binding["materialization_parameters"]["armor_magnitude_by_debuff"],
        )
        self.assertEqual(
            search.adapter_v1.HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1,
            binding["health_fallback_contract"]["policy_receipt"],
        )
        self.assertTrue(
            binding["health_fallback_contract"]["policy_receipt"][
                "historical_outcome_proxy_availability_used"
            ]
        )
        self.assertFalse(
            binding["health_fallback_contract"]["policy_receipt"][
                "candidate_or_simulator_outcome_used_for_selection"
            ]
        )
        self.assertEqual(
            search.adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1,
            binding["health_fallback_contract"]["formal_selection_counts"],
        )
        self.assertEqual(
            "EXACT_470_ROW_DETERMINISTIC_SOURCE_RECOMPILE",
            binding["health_fallback_contract"][
                "per_target_available_branch_provenance"
            ]["validation"],
        )

    def test_environment_binding_rejects_health_or_player_coupled_tamper(self) -> None:
        bound = _bind(self.blueprint)
        for label, mutate in (
            (
                "health_policy",
                lambda binding: binding["materialization_parameters"].__setitem__(
                    "health_branch_selection_policy", "legacy_threshold_25000"
                ),
            ),
            (
                "player_semantic",
                lambda binding: binding["complete_player_semantic_identity"][
                    "semantic"
                ].__setitem__("name", "tampered"),
            ),
            (
                "old_false_field",
                lambda binding: binding["health_fallback_contract"].__setitem__(
                    "outcome_or_future_information_used_for_selection", False
                ),
            ),
            (
                "historical_proxy_false",
                lambda binding: binding["health_fallback_contract"][
                    "policy_receipt"
                ].__setitem__(
                    "historical_outcome_proxy_availability_used", False
                ),
            ),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(bound)
                binding = tampered["pre_materialization_environment_binding"]
                mutate(binding)
                tampered["pre_materialization_environment_binding"] = (
                    _readdress_binding(binding)
                )
                with self.assertRaises(search.FuryCatGapSearchPlanV1Error):
                    search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_admission_requires_prebinding_and_exact_source_files(self) -> None:
        with self.assertRaisesRegex(
            search.FuryCatGapSearchPlanV1Error, "pre-materialization"
        ):
            search.admit_real_adapter_artifacts_v1(
                self.blueprint,
                materialized_manifest_path="materialized/manifest.json",
                overlay_manifest_path="formal-overlay/manifest.json",
                capsule_path="capsule.json.gz",
                base_request_path="fury_warrior_live.json",
                equipped_names_receipt_path="equipped.json",
            )
        bound = _bind(self.blueprint)
        with patch.object(
            search,
            "_sha256_file_bytes",
            return_value=search.EXPECTED_OVERLAY_MANIFEST_FILE_SHA256,
        ), patch.object(
            search,
            "_read_json_source_path",
            side_effect=_source_reader(capsule_file_sha="9" * 64),
        ), patch.object(
            search.adapter_v1,
            "build_validated_old50_capsule_index_v1",
            return_value=SimpleNamespace(bundle_content_sha256=CAPSULE_BUNDLE_SHA),
        ), patch.object(
            search.adapter_v1,
            "validate_materialized_exact_fury_dynamic_v3_corpus_v1",
        ) as strict_validator:
            with self.assertRaisesRegex(
                search.FuryCatGapSearchPlanV1Error, "differ from the pre-bound"
            ):
                search.admit_real_adapter_artifacts_v1(
                    bound,
                    materialized_manifest_path="materialized/manifest.json",
                    overlay_manifest_path="formal-overlay/manifest.json",
                    capsule_path="capsule.json.gz",
                    base_request_path="fury_warrior_live.json",
                    equipped_names_receipt_path="equipped.json",
                )
        strict_validator.assert_not_called()

    def test_strict_admission_matches_prebound_sources_parameters_and_counts(self) -> None:
        bound = _bind(self.blueprint)
        admitted = search.validate_cat_gap_search_plan_v1(_admit(bound))
        self.assertEqual(search.STATUS_ADAPTER_READY, admitted["status"])
        admission = admitted["adapter_admission"]
        self.assertTrue(admission["real_artifact_validated"])
        self.assertEqual(
            MATERIALIZED_SHA, admission["materialized_manifest_content_sha256"]
        )
        self.assertEqual(
            search.adapter_v1.FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1,
            admission["health_branch_selection_counts"],
        )
        self.assertFalse(admitted["execution_gate"]["ready_for_heavy_execution"])

    def test_strict_admission_rejects_manifest_source_parameter_or_health_drift(self) -> None:
        bound = _bind(self.blueprint)
        source = _strict_manifest(bound)
        source["source_bindings"]["base_request_sha256"] = "9" * 64
        parameters = _strict_manifest(bound)
        parameters["materialization_parameters"]["initial_base_armor"] = 1721.0
        health = _strict_manifest(bound)
        health["summary"]["health_branch_selection_counts"]["proxy_center"] -= 1
        for label, manifest in (
            ("source", source),
            ("parameters", parameters),
            ("health", health),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    search.FuryCatGapSearchPlanV1Error, "pre-bound contract"
                ):
                    _admit(bound, manifest=manifest)

    def test_horizon_selection_and_old_seed_extension_remain_rejected(self) -> None:
        observation = _negative_observation()
        observation["selection_gate"]["status"] = "SELECTED"
        observation["selection_gate"]["selected_arm_id"] = "ww_wait_cat_timing"
        with self.assertRaisesRegex(search.FuryCatGapSearchPlanV1Error, "NO_SELECTION"):
            search.build_cat_gap_search_blueprint_v1(observation)
        phase_sets = []
        for phase in self.blueprint["seed_contract"]["phases"].values():
            values = set(phase["master_seeds"])
            self.assertFalse(values.intersection(search.FORBIDDEN_HORIZON_SEEDS))
            phase_sets.append(values)
        for index, left in enumerate(phase_sets):
            for right in phase_sets[index + 1 :]:
                self.assertFalse(left.intersection(right))

    def test_wrong_formal_overlay_sha_and_execution_claim_fail_closed(self) -> None:
        tampered = deepcopy(self.blueprint)
        tampered["adapter_admission"]["required_overlay_manifest_content_sha256"] = (
            "862d629a99ab428c" + "0" * 43 + "beb98"
        )
        with self.assertRaisesRegex(
            search.FuryCatGapSearchPlanV1Error, "reconstructed canonical contract"
        ):
            search.validate_cat_gap_search_plan_v1(_reseal(tampered))
        tampered = deepcopy(self.blueprint)
        tampered["execution_gate"]["ready_for_heavy_execution"] = True
        with self.assertRaisesRegex(
            search.FuryCatGapSearchPlanV1Error, "reconstructed canonical contract"
        ):
            search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_old_health_field_or_false_historical_marker_fails_when_resealed(self) -> None:
        false_history = deepcopy(self.blueprint)
        false_history["adapter_admission"][
            "health_branch_selection_policy_receipt"
        ]["historical_outcome_proxy_availability_used"] = False
        old_field = deepcopy(self.blueprint)
        old_field["adapter_admission"][
            "health_branch_selection_policy_receipt"
        ] = {
            "policy_id": search.adapter_v1.HEALTH_BRANCH_FALLBACK_POLICY_V1,
            "priority_order": list(search.adapter_v1.HEALTH_BRANCH_FALLBACK_ORDER_V1),
            "outcome_or_future_information_used_for_selection": False,
            "all_selected_values_are_sensitivity_hypotheses": True,
        }
        for label, tampered in (
            ("false_history", false_history),
            ("old_field", old_field),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    search.FuryCatGapSearchPlanV1Error,
                    "reconstructed canonical contract",
                ):
                    search.validate_cat_gap_search_plan_v1(_reseal(tampered))

    def test_no_row_jsonl_or_custom_validator_admission_surface(self) -> None:
        parameters = inspect.signature(search.admit_real_adapter_artifacts_v1).parameters
        self.assertNotIn("artifacts", parameters)
        self.assertNotIn("validator", parameters)
        self.assertNotIn("capsule_bundle", parameters)
        self.assertIn("capsule_path", parameters)


if __name__ == "__main__":
    unittest.main()
