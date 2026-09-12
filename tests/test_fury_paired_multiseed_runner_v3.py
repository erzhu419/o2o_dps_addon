from __future__ import annotations

import copy
import hashlib
import unittest

from o2o_dps.fury_paired_multiseed_runner_v2 import PLAN_KIND as V2_PLAN_KIND
from o2o_dps.fury_paired_multiseed_runner_v3 import (
    COMPARISON_INTENT,
    DIAGNOSTIC_INTENT,
    HISTORICAL_ARTIFACT_ADMISSION_SCHEMA,
    HISTORICAL_POLICY_ID,
    HISTORICAL_REQUIRED_CONDITIONS,
    PLAN_KIND,
    REQUIRED_BASELINE_IDS,
    SINGLE_BRIDGE_MODE,
    SYNTHETIC_MODE,
    FuryPairedRunnerError,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
    unwrap_runner_plan,
    validate_historical_artifact_admission_receipt,
    validate_runner_plan,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _receipt() -> dict[str, object]:
    core: dict[str, object] = {
        "schema": HISTORICAL_ARTIFACT_ADMISSION_SCHEMA,
        "policy_id": HISTORICAL_POLICY_ID,
        "artifact_manifest_sha256": _digest("historical-manifest"),
        "source_bundle_sha256": _digest("historical-source"),
        "policy_adapter_sha256": _digest("historical-adapter"),
        "policy_profile_sha256": _digest("historical-profile"),
        "cohort_receipt_sha256": _digest("cohort-receipt"),
        "team_wave_model_manifest_sha256": _digest("team-wave-model"),
        "policy_model_sha256": _digest("historical-policy-model"),
        "prefix_causality_receipt_sha256": _digest("prefix-causality"),
        "full_scenario_adapter_sha256": _digest("historical-scenario-adapter"),
        "ordered_execution_fidelity_sha256": _digest("ordered-fidelity"),
        "satisfied_conditions": list(HISTORICAL_REQUIRED_CONDITIONS),
        "artifact_ready": True,
        "readiness_conditions_satisfied": True,
        "comparison_ready_by_itself": False,
        "blockers": [],
    }
    return {**core, "receipt_sha256": sha256_json(core)}


def _policies(receipt: dict[str, object]) -> list[dict[str, object]]:
    rows = [
        {
            "policy_id": policy_id,
            "source_sha256": _digest(f"source:{policy_id}"),
            "adapter_sha256": _digest(f"adapter:{policy_id}"),
            "profile_sha256": _digest(f"profile:{policy_id}"),
            "role": "BASELINE",
        }
        for policy_id in REQUIRED_BASELINE_IDS
    ]
    rows[-1].update(
        {
            "source_sha256": receipt["source_bundle_sha256"],
            "adapter_sha256": receipt["policy_adapter_sha256"],
            "profile_sha256": receipt["policy_profile_sha256"],
        }
    )
    rows.append(
        {
            "policy_id": "boc.fury.candidate",
            "source_sha256": _digest("candidate-source"),
            "adapter_sha256": _digest("candidate-adapter"),
            "profile_sha256": _digest("candidate-profile"),
            "role": "CANDIDATE",
        }
    )
    return rows


def _scenario() -> dict[str, object]:
    return {
        "instance_id": "raid-a",
        "component_id": "component-a",
        "scenario_id": "wave-a",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 5_000,
        "estimated_cost_units": 5,
        "corpus_entry_sha256": _digest("corpus-entry"),
        "source_scenario_sha256": _digest("source-scenario"),
        "catalog_sha256": _digest("catalog"),
        "request": {
            "encounter": {
                "duration": 5.0,
                "useHealth": False,
                "targets": [{"id": 1}],
            }
        },
    }


def _build_kwargs(receipt: dict[str, object]) -> dict[str, object]:
    scenarios = [_scenario()]
    return {
        "historical_artifact_admission_receipt": receipt,
        "protocol_id": "fury-four-baseline-test-v3",
        "protocol_sha256": _digest("protocol"),
        "phase": "development",
        "corpus_manifest_sha256": _digest("corpus-manifest"),
        "runner_inputs_sha256": _digest("runner-inputs"),
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256(scenarios),
        "corpus_binding_sha256": _digest("corpus-binding"),
        "master_seeds": [1234567],
        "scenarios": scenarios,
        "policies": _policies(receipt),
        "shard_count": 1,
        "bridge_identity": {
            "sha256": _digest("bridge"),
            "platform": "test",
        },
        "execution_bundle_identity": {
            "python_source_closure_sha256": _digest("python-closure"),
            "ordered_sink_executor_sha256": _digest("ordered-sink"),
            "full_policy_rollout_executor_sha256": _digest("full-rollout"),
            "paired_runner_source_sha256": _digest("paired-runner"),
            "evaluation_source_sha256": _digest("evaluation"),
            "runtime_snapshot_sha256": _digest("runtime-snapshot"),
        },
        "execution_mode": SYNTHETIC_MODE,
        "seed_namespace": "fury-four-baseline-test-v3",
        "plan_intent": DIAGNOSTIC_INTENT,
    }


class FuryPairedMultiseedRunnerV3Tests(unittest.TestCase):
    def test_absent_history_receipt_fails_before_plan_construction(self) -> None:
        with self.assertRaisesRegex(FuryPairedRunnerError, "receipt is required"):
            build_runner_plan(historical_artifact_admission_receipt=None)

    def test_receipt_is_complete_content_addressed_and_nonpromoting(self) -> None:
        receipt = validate_historical_artifact_admission_receipt(_receipt())
        self.assertTrue(receipt["artifact_ready"])
        self.assertTrue(receipt["readiness_conditions_satisfied"])
        self.assertFalse(receipt["comparison_ready_by_itself"])

        promoted = _receipt()
        promoted["comparison_ready_by_itself"] = True
        unsigned = dict(promoted)
        unsigned.pop("receipt_sha256")
        promoted["receipt_sha256"] = sha256_json(unsigned)
        with self.assertRaisesRegex(FuryPairedRunnerError, "self-promoting"):
            validate_historical_artifact_admission_receipt(promoted)

    def test_v3_envelope_binds_four_baselines_and_v2_transport(self) -> None:
        receipt = _receipt()
        plan = build_runner_plan(**_build_kwargs(receipt))
        validated = validate_runner_plan(plan)
        self.assertEqual(validated["schema_version"], 3)
        self.assertEqual(validated["kind"], PLAN_KIND)
        self.assertEqual(
            validated["contract"]["required_baseline_ids"],
            list(REQUIRED_BASELINE_IDS),
        )
        self.assertEqual(
            validated["contract"]["required_baseline_stratum_cell_count"], 12
        )
        self.assertFalse(
            validated["contract"][
                "comparison_authority_inherited_from_artifact_receipt"
            ]
        )
        delegate = unwrap_runner_plan(plan)
        self.assertEqual(delegate["kind"], V2_PLAN_KIND)
        self.assertEqual(delegate["contract"]["expected_rollout_count"], 5)
        self.assertEqual(
            tuple(delegate["contract"]["policy_ids"][:-1]),
            REQUIRED_BASELINE_IDS,
        )

    def test_missing_or_reordered_historical_lane_is_rejected(self) -> None:
        receipt = _receipt()
        kwargs = _build_kwargs(receipt)
        policies = copy.deepcopy(kwargs["policies"])
        policies.pop(3)
        kwargs["policies"] = policies
        with self.assertRaisesRegex(FuryPairedRunnerError, "four baselines"):
            build_runner_plan(**kwargs)

        kwargs = _build_kwargs(receipt)
        policies = copy.deepcopy(kwargs["policies"])
        policies[2], policies[3] = policies[3], policies[2]
        kwargs["policies"] = policies
        with self.assertRaisesRegex(FuryPairedRunnerError, "baseline order"):
            build_runner_plan(**kwargs)

    def test_history_policy_identity_must_match_receipt(self) -> None:
        receipt = _receipt()
        kwargs = _build_kwargs(receipt)
        kwargs["policies"][3]["adapter_sha256"] = _digest("wrong-adapter")
        with self.assertRaisesRegex(FuryPairedRunnerError, "differs from"):
            build_runner_plan(**kwargs)

    def test_production_dispatch_is_blocked_until_history_executor_is_registered(self) -> None:
        kwargs = _build_kwargs(_receipt())
        kwargs["execution_mode"] = SINGLE_BRIDGE_MODE
        kwargs["plan_intent"] = COMPARISON_INTENT
        with self.assertRaisesRegex(
            FuryPairedRunnerError, "executor adapter is not registered"
        ):
            build_runner_plan(**kwargs)

    def test_tampered_embedded_receipt_or_delegate_is_rejected(self) -> None:
        plan = build_runner_plan(**_build_kwargs(_receipt()))
        stale_receipt = copy.deepcopy(plan)
        stale_receipt["contract"]["historical_artifact_admission_receipt"][
            "policy_model_sha256"
        ] = _digest("tampered")
        stale_receipt["plan_sha256"] = sha256_json(stale_receipt["contract"])
        with self.assertRaisesRegex(FuryPairedRunnerError, "receipt SHA-256"):
            validate_runner_plan(stale_receipt)

        stale_delegate = copy.deepcopy(plan)
        stale_delegate["contract"]["delegate_plan"]["contract"]["phase"] = "other"
        stale_delegate["plan_sha256"] = sha256_json(stale_delegate["contract"])
        with self.assertRaises(FuryPairedRunnerError):
            validate_runner_plan(stale_delegate)


if __name__ == "__main__":
    unittest.main()
