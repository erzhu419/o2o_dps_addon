from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps.chronicle_external_historical_fury_policy_v2 import (
    ADMISSION_KIND,
    ARMS_LANE,
    FURY_LANE,
    MODEL_KIND,
    POLICY_ID,
    STATUS_BLOCKED,
    ExternalHistoricalFuryPolicyV2Error,
    build_external_historical_fury_policy_v2,
    load_external_historical_fury_policy_v2_bundle,
    load_admission_manifest,
    load_policy_model,
    materialize_runner_artifact_admission_receipt,
    predict_action_distribution,
    sample_legal_action,
    validate_admission_manifest,
    validate_policy_evaluation,
    validate_runner_artifact_admission_receipt,
)
from o2o_dps.chronicle_external_team_wave_model_v2 import (
    build_external_team_wave_model,
)
from tests.test_chronicle_external_team_wave_model_v2 import (
    _fake_receipt_audit,
    _single_timeline,
    _write_cohort_receipt,
)


def _fixture(
    base: Path, *, nontraining: bool = False, suffix: str = "source"
) -> dict[str, object]:
    timeline = _single_timeline(base)
    if nontraining:
        timeline_manifest = Path(str(timeline["manifest_path"]))
        instance_id = json.loads(timeline_manifest.read_text("utf-8"))[
            "instance_order"
        ][0]
        timeline["cohort_receipt_path"] = str(
            _write_cohort_receipt(
                timeline_manifest, nontraining_instance_ids={instance_id}
            )
        )
    model_output = (
        base / "offline_data" / "derived" / "external_model" / suffix
    )
    return build_external_team_wave_model(
        timeline_manifest_path=timeline["manifest_path"],
        cohort_receipt_path=timeline["cohort_receipt_path"],
        output_directory=model_output,
    )


def _runtime_observation(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "chronicle_external_v2_fury_prefix_observation/v1",
        "wave_elapsed_ms": 0,
        "observed_target_count": 0,
        "observed_dead_target_count": 0,
        "background_damage": 0,
        "background_dps": 0.0,
        "prefix_event_count": 0,
        "prefix_damage": 0,
        "actor_has_last_target": False,
        "last_prefix_action_spell": "NONE",
    }
    value.update(updates)
    return value


def _legal_actions() -> list[dict[str, object]]:
    return [
        {
            "candidate_id": "direct-action",
            "spell": {"id": 1001, "name": "Direct Action"},
            "target_role": "OTHER_OR_NEW_ENEMY",
            "legal": True,
            "command": {"spell_id": 1001, "target_slot": 0},
        },
        {
            "candidate_id": "other-action",
            "spell": {"id": 9001, "name": "Other Action"},
            "target_role": "CURRENT_ENEMY",
            "legal": True,
            "command": {"spell_id": 9001, "target_slot": 0},
        },
    ]


class ChronicleExternalHistoricalFuryPolicyV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1,
            "audit_manifest_union_receipt",
            side_effect=_fake_receipt_audit,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_build_consumes_current_external_v2_and_filters_nondecisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            output = base / "offline_data" / "behavior_models" / "historical"
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=output,
            )
            model = load_policy_model(result.model_path)
            admission = load_admission_manifest(result.admission_path)
            bundle = load_external_historical_fury_policy_v2_bundle(
                result.admission_content_addressed_path
            )
            self.assertEqual(model["kind"], MODEL_KIND)
            self.assertEqual(model["policy_id"], POLICY_ID)
            self.assertEqual(
                model["source"]["schema"],
                "chronicle_external_team_wave_model/v2",
            )
            self.assertEqual(
                model["lanes"][FURY_LANE]["fitted_policy"]["decision_count"],
                1,
            )
            self.assertEqual(
                model["lanes"][ARMS_LANE]["fitted_policy"]["decision_count"],
                0,
            )
            prefix = json.loads(result.prefix_receipt_path.read_text("utf-8"))
            action_audit = prefix["accounting"]["action_label_audit"]
            self.assertEqual(action_audit["accepted_action_labels"], 1)
            self.assertEqual(action_audit["owned_or_controller_actions_excluded"], 1)
            self.assertTrue(prefix["prefix_causality_verified"])
            self.assertEqual(admission["kind"], ADMISSION_KIND)
            self.assertEqual(admission["status"], STATUS_BLOCKED)
            self.assertFalse(admission["runner_receipt_emitted"])
            self.assertIsNone(admission["runner_receipt"])
            self.assertEqual(bundle["model"], model)
            self.assertRegex(bundle["bundle_content_sha256"], r"^[0-9a-f]{64}$")

    def test_outer_gate_and_focal_diagnostic_are_noninterchangeable(self) -> None:
        perfect_metrics = {
            "heldout_decision_count": 10_000,
            "known_action_count": 10_000,
            "known_action_coverage": 1.0,
            "top1_accuracy": 1.0,
            "top3_accuracy": 1.0,
            "contextual_log_loss": 0.1,
            "global_log_loss": 1.0,
            "contextual_log_loss_improvement": 0.9,
            "expected_calibration_error": 0.0,
        }
        rejected = policy_v2._outer_rejections(
            {"component_count": 5, "metrics": perfect_metrics}
        )
        self.assertEqual(
            [value["code"] for value in rejected],
            ["INSUFFICIENT_INDEPENDENT_OUTER_COMPONENTS"],
        )
        self.assertEqual(
            policy_v2._outer_rejections(
                {"component_count": 21, "metrics": perfect_metrics}
            ),
            [],
        )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "historical",
            )
            evaluation = json.loads(result.evaluation_path.read_text("utf-8"))
            self.assertTrue(
                evaluation["outer_full_roster_evaluation"]["promotion_authority"]
            )
            focal = evaluation["focal_fury_component_diagnostic"]
            self.assertFalse(focal["promotion_authority"])
            self.assertFalse(focal["can_override_outer_failure"])
            self.assertFalse(focal["shared_teammate_or_guild_leakage_blocked"])

            promoted = copy.deepcopy(evaluation)
            promoted["focal_fury_component_diagnostic"][
                "promotion_authority"
            ] = True
            promoted = policy_v2._content_addressed(promoted)
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error,
                "FOCAL_DIAGNOSTIC_PREMATURE_PROMOTION",
            ):
                validate_policy_evaluation(promoted)

    def test_receipt_bound_nontraining_instance_never_enters_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base, nontraining=True)
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "nontraining",
            )
            model = load_policy_model(result.model_path)
            self.assertEqual(
                model["lanes"][FURY_LANE]["fitted_policy"]["decision_count"],
                0,
            )
            prefix = json.loads(result.prefix_receipt_path.read_text("utf-8"))
            self.assertEqual(
                prefix["accounting"]["excluded_episode_reasons"][
                    "WARRIOR_FURY:DESCRIPTIVE_NONTRAINING"
                ],
                2,
            )

    def test_runtime_distribution_is_legal_conditioned_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "runtime",
            )
            distribution = predict_action_distribution(
                result.model_path,
                observation=_runtime_observation(),
                legal_actions=_legal_actions(),
            )
            self.assertAlmostEqual(distribution["probability_sum"], 1.0)
            rows = distribution["candidate_distribution"]
            self.assertEqual([value["candidate_id"] for value in rows], ["direct-action", "other-action"])
            self.assertGreater(rows[0]["probability"], rows[1]["probability"])
            first = sample_legal_action(
                result.model_path,
                observation=_runtime_observation(),
                legal_actions=_legal_actions(),
                seed={"master": 7, "scenario": "wave-a"},
            )
            second = sample_legal_action(
                result.model_path,
                observation=_runtime_observation(),
                legal_actions=_legal_actions(),
                seed={"master": 7, "scenario": "wave-a"},
            )
            self.assertEqual(first, second)
            self.assertEqual(first["comparison_status"], "NOT_COMPARISON_READY")

    def test_runtime_future_feature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "future",
            )
            observation = _runtime_observation()
            observation["remaining_wave_ms"] = 500
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error, "FUTURE_FEATURE_DETECTED"
            ):
                predict_action_distribution(
                    result.model_path,
                    observation=observation,
                    legal_actions=_legal_actions(),
                )

    def test_admission_is_typed_and_cannot_self_mint_runner_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            result = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "admission",
            )
            admission = load_admission_manifest(result.admission_path)
            codes = {value["code"] for value in admission["typed_blockers"]}
            self.assertIn("INSUFFICIENT_INDEPENDENT_OUTER_COMPONENTS", codes)
            self.assertIn("TEAM_BACKGROUND_RUNTIME_ADMISSION_NOT_CLOSED", codes)
            self.assertIn("FULL_SCENARIO_ADAPTER_NOT_IMPLEMENTED", codes)
            self.assertIn("ORDERED_EXECUTION_FIDELITY_NOT_CLOSED", codes)
            self.assertIn("PAIRED_ROLLOUT_IDENTITY_NOT_CLOSED", codes)
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error,
                "HISTORICAL_ARTIFACT_ADMISSION_BLOCKED",
            ) as raised:
                materialize_runner_artifact_admission_receipt(admission)
            self.assertEqual(
                raised.exception.details["typed_blockers"],
                admission["typed_blockers"],
            )
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error,
                "DETACHED_RUNNER_RECEIPT_REJECTED",
            ):
                validate_runner_artifact_admission_receipt(
                    {"artifact_ready": True, "self_reported": True},
                    admission_manifest_or_path=admission,
                )

            promoted = copy.deepcopy(admission)
            promoted["runner_receipt"] = {"self_reported": True}
            promoted["runner_receipt_emitted"] = True
            promoted["artifact_ready"] = True
            promoted["readiness_conditions_satisfied"] = True
            promoted = policy_v2._content_addressed(promoted)
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error,
                "ADMISSION_PREMATURE_PROMOTION",
            ):
                validate_admission_manifest(promoted)

    def test_build_is_content_addressed_and_worker_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _fixture(base)
            first = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "first",
            )
            second = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=base
                / "offline_data"
                / "behavior_models"
                / "second",
            )
            for left, right in (
                (first.model_path, second.model_path),
                (first.evaluation_path, second.evaluation_path),
                (first.prefix_receipt_path, second.prefix_receipt_path),
                (first.admission_path, second.admission_path),
            ):
                self.assertEqual(left.read_bytes(), right.read_bytes())
            self.assertEqual(
                first.model_path.read_bytes(),
                first.model_content_addressed_path.read_bytes(),
            )
            first.model_content_addressed_path.unlink()
            with self.assertRaisesRegex(
                ExternalHistoricalFuryPolicyV2Error,
                "ARTIFACT_PAIR_MISSING_OR_DIFFERENT",
            ):
                load_external_historical_fury_policy_v2_bundle(
                    first.admission_path
                )


if __name__ == "__main__":
    unittest.main()
