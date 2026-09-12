from __future__ import annotations

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.cat2new_fury_horizon_analysis_v2 import (
    Cat2NewFuryHorizonAnalysisV2Error,
    analyze_confirmation_run_v2,
    compact_analysis_v2,
    main,
    summarize_horizon_deltas_v2,
    validate_frozen_confirmation_identity_v2,
)
from o2o_dps.cat2new_fury_horizon_confirmation_v2 import (
    BASELINE_POLICY_IDS,
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
)
from o2o_dps.fury_cat2_horizon_confirmation_preparation_v2 import (
    prepare_fury_cat2_horizon_confirmation_v2,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
)
from o2o_dps.fury_multiseed_hpc_reducer_v4 import (
    REDUCTION_SCHEMA_V4,
    STATUS_V4 as REDUCTION_STATUS_V4,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


PAIR_COUNT = len(CONFIRMATION_MASTER_SEEDS)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _constant_family(value: float) -> dict[str, dict[str, list[float]]]:
    return {
        arm_id: {
            baseline_id: [value] * PAIR_COUNT
            for baseline_id in BASELINE_POLICY_IDS
        }
        for arm_id in CONFIRMATION_ARM_IDS
    }


def _strict_reduction(
    runner: dict[str, object],
    dispatch: dict[str, object],
    deltas: dict[str, list[float]],
) -> dict[str, object]:
    groups = sorted(
        runner["contract"]["groups"], key=lambda row: str(row["group_id"])
    )
    traces: dict[str, list[dict[str, object]]] = {}
    pair_stats: dict[str, dict[str, int | float]] = {}
    readiness: dict[str, dict[str, object]] = {}
    for baseline_id, label in (
        (CAT_POLICY_ID, "candidate_minus_cat"),
        (CONTRA260817_POLICY_ID, "candidate_minus_contra260817"),
    ):
        values = deltas[baseline_id]
        rows = []
        for group, delta in zip(groups, values):
            candidate_dps = 1_000.0
            baseline_dps = candidate_dps - delta
            rows.append(
                {
                    "group_id": group["group_id"],
                    "master_seed": group["master_seed"],
                    "simulator_seed": group["simulator_seed"],
                    "candidate_policy_id": CAT2NEW_POLICY_ID,
                    "baseline_policy_id": baseline_id,
                    "candidate_dps": candidate_dps,
                    "baseline_dps": baseline_dps,
                    "candidate_minus_baseline_dps": delta,
                }
            )
        traces[label] = rows
        pair_stats[label] = {
            "pair_count": PAIR_COUNT,
            "dps_delta_sum": sum(values),
            "dps_delta_squared_sum": sum(value * value for value in values),
            "both_offline_score_eligible_count": PAIR_COUNT,
        }
        readiness[label] = {
            "candidate_policy_id": CAT2NEW_POLICY_ID,
            "baseline_policy_id": baseline_id,
            "required_pair_count": PAIR_COUNT,
            "observed_pair_count": PAIR_COUNT,
            "complete_eligible_pair_count": PAIR_COUNT,
            "analysis_eligible": True,
        }
    policy_stats = {
        policy_id: {
            "rollout_count": PAIR_COUNT,
            "completion_count": PAIR_COUNT,
            "offline_score_eligible_count": PAIR_COUNT,
        }
        for policy_id in (CAT2NEW_POLICY_ID, *BASELINE_POLICY_IDS)
    }
    return {
        "schema": REDUCTION_SCHEMA_V4,
        "status": REDUCTION_STATUS_V4,
        "runner_plan_sha256": runner["plan_sha256"],
        "dispatch_plan_sha256": sha256_json(dispatch),
        "execution_kind": DEVELOPMENT_EXECUTION_KIND_V3,
        "paired_group_count": PAIR_COUNT,
        "unique_rollout_count": PAIR_COUNT * len(ALLOWED_POLICY_IDS_V3),
        "expected_rollout_count": PAIR_COUNT * len(ALLOWED_POLICY_IDS_V3),
        "policy_ids": list(ALLOWED_POLICY_IDS_V3),
        "trace_baseline_policy_ids": list(BASELINE_POLICY_IDS),
        "policy_sufficient_statistics": policy_stats,
        "paired_sufficient_statistics": pair_stats,
        "contrast_readiness": readiness,
        "paired_traces": traces,
        "heavy_execution_started": True,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


class Cat2NewFuryHorizonAnalysisV2SummaryTests(unittest.TestCase):
    def test_positive_and_negative_constants_use_null_infinity(self) -> None:
        values = _constant_family(1.0)
        values[CONFIRMATION_ARM_IDS[1]] = {
            baseline_id: [-1.0] * PAIR_COUNT
            for baseline_id in BASELINE_POLICY_IDS
        }
        result = summarize_horizon_deltas_v2(values)
        rows = {
            (row["arm_id"], row["baseline_policy_id"]): row
            for row in result["contrast_results"]
        }
        for baseline_id in BASELINE_POLICY_IDS:
            positive = rows[(CONFIRMATION_ARM_IDS[0], baseline_id)]
            negative = rows[(CONFIRMATION_ARM_IDS[1], baseline_id)]
            self.assertIsNone(positive["paired_t_statistic"])
            self.assertEqual(
                "POSITIVE_INFINITY", positive["paired_t_statistic_kind"]
            )
            self.assertEqual(0.0, positive["raw_two_sided_p"])
            self.assertIsNone(negative["paired_t_statistic"])
            self.assertEqual(
                "NEGATIVE_INFINITY", negative["paired_t_statistic_kind"]
            )
            self.assertEqual(0.0, negative["raw_two_sided_p"])
        self.assertEqual(
            CONFIRMATION_ARM_IDS[0],
            result["selection_gate"]["selected_arm_id"],
        )
        json.loads(json.dumps(result, allow_nan=False))

    def test_all_zero_family_is_finite_and_selects_nothing(self) -> None:
        result = summarize_horizon_deltas_v2(_constant_family(0.0))
        for row in result["contrast_results"]:
            self.assertEqual(0.0, row["paired_t_statistic"])
            self.assertEqual("FINITE", row["paired_t_statistic_kind"])
            self.assertEqual(1.0, row["raw_two_sided_p"])
            self.assertFalse(row["holm_reject_familywise_0_05"])
        self.assertEqual("NO_SELECTION", result["selection_gate"]["status"])
        self.assertIsNone(result["selection_gate"]["selected_arm_id"])
        self.assertFalse(result["next_simulator_diagnostic_selection_performed"])
        json.loads(json.dumps(result, allow_nan=False))


class Cat2NewFuryHorizonAnalysisV2IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        root = Path(cls._temporary.name)
        template = root / "template.json"
        template.write_text(
            json.dumps(_source_plan(tuple(range(1, 257)))), encoding="utf-8"
        )
        bridge = root / "o2obridge.linux-amd64"
        bridge.write_bytes(b"horizon-analysis-v2-test")
        import hashlib

        receipt = prepare_fury_cat2_horizon_confirmation_v2(
            template_runner_plan_path=template,
            linux_bridge_path=bridge,
            bridge_build_id="horizon-analysis-v2-test",
            source_release_root=root / "releases",
            runs_root=root / "runs",
            project_root=PROJECT_ROOT,
            expected_linux_bridge_sha256=hashlib.sha256(
                bridge.read_bytes()
            ).hexdigest(),
        )
        cls.run_root = Path(receipt["confirmation_run"]["path"])
        cls.manifest_path = cls.run_root / "horizon-confirmation-manifest.json"
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))
        cls.arm_by_plan_sha = {
            arm["runner_plan_sha256"]: arm["arm_spec"]["arm_id"]
            for arm in cls.manifest["arms"]
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _deltas(self) -> dict[str, dict[str, list[float]]]:
        pairs = ((3.0, 2.0), (4.0, 1.0), (2.0, 2.0))
        return {
            arm_id: {
                CAT_POLICY_ID: [cat] * PAIR_COUNT,
                CONTRA260817_POLICY_ID: [contra] * PAIR_COUNT,
            }
            for arm_id, (cat, contra) in zip(CONFIRMATION_ARM_IDS, pairs)
        }

    def _reducer(self, deltas, calls, *, tamper: bool = False):
        def reducer(runner, dispatch, *, output_directory):
            del output_directory
            arm_id = self.arm_by_plan_sha[runner["plan_sha256"]]
            calls.append(arm_id)
            result = _strict_reduction(runner, dispatch, deltas[arm_id])
            if tamper and arm_id == CONFIRMATION_ARM_IDS[0]:
                result["paired_sufficient_statistics"][
                    "candidate_minus_cat"
                ]["dps_delta_sum"] += 0.5
            return result

        return reducer

    def test_success_calls_v4_once_per_arm_and_selects_by_maximin(self) -> None:
        calls: list[str] = []
        result = analyze_confirmation_run_v2(
            self.manifest,
            run_root=self.run_root,
            arm_output_directories={
                arm_id: self.run_root / "outputs" / arm_id
                for arm_id in CONFIRMATION_ARM_IDS
            },
            reducer=self._reducer(self._deltas(), calls),
        )
        self.assertEqual(list(CONFIRMATION_ARM_IDS), calls)
        self.assertEqual(6, len(result["contrast_results"]))
        self.assertEqual(
            sha256_json(result["analysis_contract"]),
            result["analysis_contract_sha256"],
        )
        self.assertTrue(all(
            row["holm_reject_familywise_0_05"]
            for row in result["contrast_results"]
        ))
        self.assertEqual(
            CONFIRMATION_ARM_IDS[0], result["selection_gate"]["selected_arm_id"]
        )
        self.assertTrue(result["next_simulator_diagnostic_selection_performed"])
        self.assertFalse(result["live_policy_selection_performed"])
        self.assertFalse(result["policy_promotion_performed"])
        self.assertFalse(result["optional_stopping_performed"])
        self.assertFalse(result["deployment_allowed"])
        self.assertIn("paired_trace", result)
        self.assertTrue(all(
            "paired_traces" not in reduction
            for reduction in result["source_reductions"].values()
        ))
        compact = compact_analysis_v2(result)
        self.assertNotIn("paired_trace", compact)
        json.loads(json.dumps(result, allow_nan=False))
        json.loads(json.dumps(compact, allow_nan=False))

    def test_rejects_trace_aggregate_tampering(self) -> None:
        with self.assertRaisesRegex(
            Cat2NewFuryHorizonAnalysisV2Error,
            "differs from its strict reduction",
        ):
            analyze_confirmation_run_v2(
                self.manifest,
                run_root=self.run_root,
                arm_output_directories={
                    arm_id: self.run_root / "outputs" / arm_id
                    for arm_id in CONFIRMATION_ARM_IDS
                },
                reducer=self._reducer(self._deltas(), [], tamper=True),
            )

    def test_rejects_old_seed_family_even_with_recomputed_lock_address(self) -> None:
        manifest = deepcopy(self.manifest)
        lock = manifest["input_lock"]
        lock["master_seeds"] = list(range(257, 513))
        core = {key: value for key, value in lock.items() if key != "content_address"}
        lock["content_address"]["sha256"] = sha256_json(core)
        with self.assertRaisesRegex(
            Cat2NewFuryHorizonAnalysisV2Error,
            "master_seeds differs from the frozen contract",
        ):
            validate_frozen_confirmation_identity_v2(
                manifest, run_root=self.run_root
            )

    def test_cli_missing_receipt_is_blocked_exit_two(self) -> None:
        full = self.run_root / "missing-receipt-full.json"
        compact = self.run_root / "missing-receipt-compact.json"
        arguments = [
            "--manifest",
            str(self.manifest_path),
            "--run-root",
            str(self.run_root),
            "--output",
            str(full),
            "--compact-output",
            str(compact),
        ]
        for arm_id in CONFIRMATION_ARM_IDS:
            arguments.extend(
                ["--arm-output", f"{arm_id}={self.run_root / 'empty' / arm_id}"]
            )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(arguments)
        self.assertEqual(2, code)
        self.assertTrue(stderr.getvalue().startswith("BLOCKED:"))
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(full.exists())
        self.assertFalse(compact.exists())


if __name__ == "__main__":
    unittest.main()
