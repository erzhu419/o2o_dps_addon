from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_current_cat2_short_horizon_sensitivity_v1 import (
    DEFAULT_ANALYSIS_MODULE,
    DEFAULT_ANALYSIS_TEST,
    DEFAULT_FULL_ARTIFACT_SHA256,
    DEFAULT_FULL_RECEIPT_SHA256,
    DEFAULT_INPUT_LOCK_SHA256,
    DEFAULT_REQUEST_BUNDLE_SHA256,
    DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
    ShortHorizonSensitivityError,
    _read_pinned_file,
    _read_pinned_json,
    aggregate_short_rows,
    analysis_source_provenance,
    full_scope_summary,
    subtract_short_scope,
    verify_analysis_sources,
    verify_upstream_evidence,
)
from o2o_dps.fury_current_cat2_heldout_replay_v1 import CAT2_ID
from o2o_dps.fury_heldout_corpus_gate_v1 import SelectedCorpus, SelectedFamily
from o2o_dps.fury_policy_optimization_v1 import PolicyScenario


CAT_ID = "cat.fury.profile1"
CONTRA_ID = "contra.deployed.fury.raid_a"
CANDIDATE_ID = "candidate.frozen"
EXPERT_IDS = (CAT_ID, CONTRA_ID, CANDIDATE_ID, CAT2_ID)


def _file_contract(path: Path) -> tuple[int, str]:
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


def _current_analysis_sources():
    module_size, module_sha256 = _file_contract(DEFAULT_ANALYSIS_MODULE)
    test_size, test_sha256 = _file_contract(DEFAULT_ANALYSIS_TEST)
    return verify_analysis_sources(
        expected_module_path=DEFAULT_ANALYSIS_MODULE,
        expected_module_size_bytes=module_size,
        expected_module_sha256=module_sha256,
        expected_test_path=DEFAULT_ANALYSIS_TEST,
        expected_test_size_bytes=test_size,
        expected_test_sha256=test_sha256,
    )


def _family(name: str, horizon_ms: int, weight: float = 1.0) -> SelectedFamily:
    return SelectedFamily(
        scenario=PolicyScenario(
            scenario_id=name,
            request={"encounter": {"targets": [{}]}},
            horizon_ms=horizon_ms,
            weight=weight,
            provenance={"instance_id": "heldout"},
        ),
        instance_id="heldout",
        family_id=name,
        catalog_relative_path="catalog.json.gz",
        target_count=1,
        target_count_stratum="1",
        duration_stratum="short",
    )


def _policy(dps: float, horizon_ms: int) -> dict:
    return {
        "damage_delta": dps * horizon_ms / 1000.0,
        "dps": dps,
        "configured_horizon_complete": True,
        "omitted_lane_count": 0,
        "nonfaithful_reason_counts": {},
        "source_execution": False,
        "exact_lua_replay": False,
    }


def _synthetic_full() -> dict:
    short_dps = {
        CAT_ID: 100.0,
        CONTRA_ID: 90.0,
        CANDIDATE_ID: 110.0,
        CAT2_ID: 120.0,
    }
    long_dps = {
        CAT_ID: 200.0,
        CONTRA_ID: 210.0,
        CANDIDATE_ID: 205.0,
        CAT2_ID: 220.0,
    }
    seconds = 1.005
    ranking = []
    for expert_id in EXPERT_IDS:
        damage = short_dps[expert_id] * 0.005 + long_dps[expert_id]
        ranking.append(
            {
                "expert_id": expert_id,
                "rollout_count": 2,
                "weighted_mean_dps": damage / seconds,
                "unweighted_mean_dps": (short_dps[expert_id] + long_dps[expert_id]) / 2,
            }
        )
    comparisons = {}
    for reference_id in EXPERT_IDS[:-1]:
        deltas = (
            short_dps[CAT2_ID] - short_dps[reference_id],
            long_dps[CAT2_ID] - long_dps[reference_id],
        )
        comparisons[reference_id] = {
            "pair_count": 2,
            "mean_paired_dps": sum(deltas) / 2,
            "minimum_paired_dps": min(deltas),
            "maximum_paired_dps": max(deltas),
            "wins": sum(value > 1e-9 for value in deltas),
            "ties": sum(abs(value) <= 1e-9 for value in deltas),
            "losses": sum(value < -1e-9 for value in deltas),
        }
    return {
        "evaluation": {
            "overall": {
                "ranking": ranking,
                "current_cat2_vs_each_reference": comparisons,
            }
        }
    }


class ShortHorizonSensitivityTests(unittest.TestCase):
    def test_analysis_module_and_test_have_explicit_identity_contract(self) -> None:
        identities = _current_analysis_sources()
        provenance = analysis_source_provenance(identities)
        self.assertEqual(provenance["file_count"], 2)
        self.assertTrue(provenance["caller_supplied_path_size_sha256_contract"])
        self.assertTrue(provenance["verified_before_and_after_replay"])
        self.assertEqual(
            [value["role"] for value in provenance["files"]],
            [
                "short_horizon_analysis_module_source",
                "short_horizon_analysis_test_source",
            ],
        )

    def test_analysis_source_path_size_and_sha_fail_closed(self) -> None:
        module_size, module_sha256 = _file_contract(DEFAULT_ANALYSIS_MODULE)
        test_size, test_sha256 = _file_contract(DEFAULT_ANALYSIS_TEST)
        common = {
            "expected_module_path": DEFAULT_ANALYSIS_MODULE,
            "expected_module_size_bytes": module_size,
            "expected_module_sha256": module_sha256,
            "expected_test_path": DEFAULT_ANALYSIS_TEST,
            "expected_test_size_bytes": test_size,
            "expected_test_sha256": test_sha256,
        }
        with self.assertRaisesRegex(ShortHorizonSensitivityError, "path mismatch"):
            verify_analysis_sources(
                **{**common, "expected_module_path": DEFAULT_ANALYSIS_TEST}
            )
        with self.assertRaisesRegex(ShortHorizonSensitivityError, "size mismatch"):
            verify_analysis_sources(
                **{**common, "expected_module_size_bytes": module_size + 1}
            )
        with self.assertRaisesRegex(ShortHorizonSensitivityError, "SHA-256 mismatch"):
            verify_analysis_sources(
                **{**common, "expected_test_sha256": "0" * 64}
            )

    def test_analysis_source_mutation_fails_post_replay_style_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "analysis.py"
            path.write_text("value = 1\n", encoding="utf-8")
            size, sha256 = _file_contract(path)
            identity = _read_pinned_file(
                path,
                role="analysis_test_fixture",
                expected_path=path,
                expected_size_bytes=size,
                expected_sha256=sha256,
            )
            self.assertEqual(identity.sha256, sha256)
            path.write_text("value = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ShortHorizonSensitivityError, "SHA-256 mismatch"
            ):
                _read_pinned_file(
                    path,
                    role="analysis_test_fixture",
                    expected_path=path,
                    expected_size_bytes=size,
                    expected_sha256=sha256,
                )

    def test_canonical_absolute_artifact_and_receipt_lock_analysis_sources(self) -> None:
        artifact_path = (
            DEFAULT_ANALYSIS_MODULE.parents[1]
            / "offline_data"
            / "sim_validation"
            / "fury_current_cat2_short_horizon_sensitivity_absolute_seed_v1.json"
        )
        artifact = json.loads(artifact_path.read_bytes())
        receipt = json.loads(Path(str(artifact_path) + ".receipt.json").read_bytes())
        identities = _current_analysis_sources()
        provenance = analysis_source_provenance(identities)
        expected_files = [asdict(value) for value in identities]
        self.assertEqual(
            artifact["analysis_source_provenance"]["files"], expected_files
        )
        self.assertEqual(
            artifact["analysis_source_provenance"]["bundle_sha256"],
            provenance["bundle_sha256"],
        )
        self.assertEqual(receipt["analysis_source_files"], expected_files)
        self.assertEqual(
            receipt["analysis_source_bundle_sha256"], provenance["bundle_sha256"]
        )
        self.assertTrue(receipt["analysis_sources_verified_before_and_after_replay"])

    def test_default_upstream_chain_is_content_addressed(self) -> None:
        evidence = verify_upstream_evidence()
        self.assertEqual(evidence.full_identity.sha256, DEFAULT_FULL_ARTIFACT_SHA256)
        self.assertEqual(evidence.receipt_identity.sha256, DEFAULT_FULL_RECEIPT_SHA256)
        self.assertEqual(evidence.lock_identity.sha256, DEFAULT_INPUT_LOCK_SHA256)
        self.assertEqual(
            evidence.lock_snapshot.request_contract["request_bundle_sha256"],
            DEFAULT_REQUEST_BUNDLE_SHA256,
        )
        self.assertEqual(len(evidence.lock_snapshot.files), 66)
        self.assertEqual(
            evidence.lock_snapshot.file_bundle_sha256,
            DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
        )

    def test_file_bundle_must_be_explicitly_pinned(self) -> None:
        with self.assertRaisesRegex(
            ShortHorizonSensitivityError, "explicitly expected bundle"
        ):
            verify_upstream_evidence(
                expected_run_input_file_bundle_sha256="0" * 64
            )

    def test_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "value.json"
            path.write_text("{}", encoding="utf-8")
            observed = hashlib.sha256(b"{}").hexdigest()
            self.assertNotEqual(observed, "0" * 64)
            with self.assertRaisesRegex(ShortHorizonSensitivityError, "SHA-256 mismatch"):
                _read_pinned_json(
                    path,
                    role="test_input",
                    expected_sha256="0" * 64,
                )

    def test_short_sufficient_statistics_subtract_algebraically(self) -> None:
        corpus = SelectedCorpus(
            (
                _family("short", 5),
                _family("long", 1000),
            ),
            (),
        )
        short_row = {
            "scenario_id": "short",
            "horizon_ms": 5,
            "sampling_weight": 1.0,
            "target_count_stratum": "1",
            "seed": 1,
            "policies": {
                CAT_ID: _policy(100.0, 5),
                CONTRA_ID: _policy(90.0, 5),
                CANDIDATE_ID: _policy(110.0, 5),
                CAT2_ID: _policy(120.0, 5),
            },
        }
        short = aggregate_short_rows(
            [short_row], EXPERT_IDS, threshold_ms=10
        )
        result = subtract_short_scope(
            _synthetic_full(),
            corpus,
            (1,),
            short,
        )
        expected = {
            CAT_ID: 200.0,
            CONTRA_ID: 210.0,
            CANDIDATE_ID: 205.0,
            CAT2_ID: 220.0,
        }
        for expert_id, dps in expected.items():
            self.assertAlmostEqual(
                result["policies"][expert_id]["remaining_weighted_mean_dps"],
                dps,
            )
            self.assertAlmostEqual(
                result["policies"][expert_id]["remaining_unweighted_mean_dps"],
                dps,
            )
        self.assertEqual(result["ranking"][0]["expert_id"], CAT2_ID)
        self.assertAlmostEqual(
            result["current_cat2_vs_each_reference"][CAT_ID][
                "remaining_weighted_mean_dps_margin"
            ],
            20.0,
        )
        self.assertFalse(result["exact_additive_subtraction"])
        self.assertEqual(
            result["full_additive_totals_source"],
            "DERIVED_FROM_REPORTED_AGGREGATE_FLOAT",
        )
        self.assertGreaterEqual(
            result["policies"][CAT2_ID][
                "remaining_weighted_mean_dps_absolute_error_bound_from_upstream_rounding"
            ],
            0.0,
        )

    def test_pair_extrema_are_not_claimed_for_remaining_scope(self) -> None:
        corpus = SelectedCorpus((_family("short", 5), _family("long", 1000)), ())
        short = aggregate_short_rows(
            [
                {
                    "scenario_id": "short",
                    "horizon_ms": 5,
                    "sampling_weight": 1.0,
                    "target_count_stratum": "1",
                    "seed": 1,
                    "policies": {
                        CAT_ID: _policy(100.0, 5),
                        CONTRA_ID: _policy(90.0, 5),
                        CANDIDATE_ID: _policy(110.0, 5),
                        CAT2_ID: _policy(120.0, 5),
                    },
                }
            ],
            EXPERT_IDS,
            threshold_ms=10,
        )
        result = subtract_short_scope(_synthetic_full(), corpus, (1,), short)
        for comparison in result["current_cat2_vs_each_reference"].values():
            self.assertTrue(
                comparison[
                    "remaining_minimum_and_maximum_not_identifiable_from_aggregate_only"
                ]
            )
            self.assertNotIn("remaining_minimum_paired_dps", comparison)
            self.assertNotIn("remaining_maximum_paired_dps", comparison)

    def test_every_derived_scope_keeps_all_gates_false(self) -> None:
        summary = full_scope_summary(_synthetic_full())
        self.assertFalse(summary["voting_result"])
        self.assertFalse(summary["deployment_gate_passed"])
        self.assertFalse(summary["real_game_superiority_gate_passed"])


if __name__ == "__main__":
    unittest.main()
