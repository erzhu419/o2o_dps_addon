from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import o2o_dps.fury_multiseed_evaluation_v2 as evaluation_v2

from o2o_dps.fury_multiseed_evaluation_v3 import (
    ANALYSIS_KIND,
    DEFAULT_PROTOCOL,
    EXECUTION_SOURCE_IDENTITY_SCHEMA,
    HISTORICAL_POLICY_ID,
    PLAN_KIND,
    REQUIRED_BASELINE_IDS,
    REQUIRED_BASELINE_STRATUM_CELL_COUNT,
    REQUIRED_STRATA,
    V3_REQUIRED_PRODUCTION_PATHS,
    FuryMultiseedProtocolError,
    analyze_rollouts,
    build_plan,
    build_fury_execution_source_identity_v3,
    load_protocol,
    main,
    materialize_protocol,
    validate_protocol_revision_v3,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_PUBLISHED_FILE_SHA256S = {
    "configs/evaluation/fury_multiseed_protocol_v2.json": (
        "14ec63be011ad50a145567d359e9043f7604689a4ac49e1b9180168fbd64d420"
    ),
    "o2o_dps/fury_full_policy_rollout_v2.py": (
        "57f691f44185b85b58f154ee91ff55f2fd23acd3d4f7753c29e2a03582b504f6"
    ),
    "o2o_dps/fury_multiseed_evaluation_v2.py": (
        "a5213e840ac286a6c45812c3965fe9c0afb5a334acbc24ea1b7d6adf81c74dbb"
    ),
    "o2o_dps/fury_paired_multiseed_runner_v2.py": (
        "f46da79189692bc21d569168c4e479d72f60e3804912bd24615a097ec80b45d6"
    ),
}


class FuryMultiseedEvaluationV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))

    def test_default_protocol_registers_four_lanes_and_twelve_cells(self) -> None:
        materialized = materialize_protocol(self.document)
        protocol = materialized["protocol"]
        self.assertEqual(
            tuple(materialized["required_baseline_ids"]), REQUIRED_BASELINE_IDS
        )
        self.assertEqual(
            materialized["required_baseline_stratum_cell_count"],
            REQUIRED_BASELINE_STRATUM_CELL_COUNT,
        )
        self.assertEqual(
            tuple(protocol["evaluation_contract"]["required_strata"]),
            REQUIRED_STRATA,
        )
        statistics = protocol["evaluation_contract"]["statistics"]
        self.assertEqual(statistics["required_baseline_count"], 4)
        self.assertEqual(statistics["required_stratum_count"], 3)
        self.assertEqual(statistics["simultaneous_test_count"], 12)
        self.assertEqual(statistics["bonferroni_cell_count"], 12)

    def test_v3_source_closure_contains_every_versioned_entrypoint(self) -> None:
        identity = build_fury_execution_source_identity_v3()
        self.assertEqual(identity["schema"], EXECUTION_SOURCE_IDENTITY_SCHEMA)
        self.assertEqual(
            identity["required_entrypoints"], sorted(V3_REQUIRED_PRODUCTION_PATHS)
        )
        files = {
            row["relative_path"]: row["sha256"] for row in identity["files"]
        }
        for relative in V3_REQUIRED_PRODUCTION_PATHS:
            self.assertIn(relative, files)
        source_contract = self.document["execution_contract"][
            "python_source_identity_contract"
        ]
        self.assertEqual(source_contract["schema"], EXECUTION_SOURCE_IDENTITY_SCHEMA)
        self.assertEqual(source_contract["file_count"], identity["file_count"])
        self.assertEqual(
            source_contract["canonical_bundle_sha256"],
            identity["canonical_bundle"]["sha256"],
        )
        implementation = self.document["execution_contract"][
            "implementation_identity"
        ]
        self.assertEqual(
            implementation["full_policy_rollout_executor_sha256"],
            files["o2o_dps/fury_full_policy_rollout_v3.py"],
        )
        self.assertEqual(
            implementation["paired_runner_source_sha256"],
            files["o2o_dps/fury_paired_multiseed_runner_v3.py"],
        )
        self.assertEqual(
            implementation["evaluation_source_sha256"],
            files["o2o_dps/fury_multiseed_evaluation_v3.py"],
        )

    def test_v2_published_bytes_and_materializer_remain_unchanged(self) -> None:
        for relative, expected in V2_PUBLISHED_FILE_SHA256S.items():
            observed = hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(observed, expected, relative)
        v2_document = json.loads(evaluation_v2.DEFAULT_PROTOCOL.read_bytes())
        materialized = evaluation_v2.materialize_protocol(v2_document)
        self.assertEqual(
            materialized["protocol_sha256"],
            "2e13bee3a7f2f9cc938223c7fe23c21c880cc0bda8e8b763563af003afbd0d4e",
        )
        self.assertEqual(len(materialized["required_baseline_ids"]), 3)

    def test_v3_materialization_does_not_monkeypatch_v2_globals(self) -> None:
        original_materializer = evaluation_v2.materialize_protocol
        original_entrypoints = tuple(evaluation_v2.REQUIRED_PRODUCTION_PATHS)
        materialize_protocol(self.document)
        self.assertIs(evaluation_v2.materialize_protocol, original_materializer)
        self.assertEqual(
            tuple(evaluation_v2.REQUIRED_PRODUCTION_PATHS), original_entrypoints
        )
        v2_document = json.loads(evaluation_v2.DEFAULT_PROTOCOL.read_bytes())
        self.assertEqual(
            evaluation_v2.materialize_protocol(v2_document)["protocol_sha256"],
            "2e13bee3a7f2f9cc938223c7fe23c21c880cc0bda8e8b763563af003afbd0d4e",
        )

    def test_v3_rejects_falling_back_to_the_v2_source_contract(self) -> None:
        stale = copy.deepcopy(self.document)
        source = stale["execution_contract"]["python_source_identity_contract"]
        source["schema"] = "fury_execution_source_identity/v2"
        source["required_entrypoints"] = list(
            evaluation_v2.REQUIRED_PRODUCTION_PATHS
        )
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "isolated v3 production entrypoints"
        ):
            validate_protocol_revision_v3(stale)

    def test_exact_fixed_seed_contract_is_preserved_and_disjoint(self) -> None:
        materialized = materialize_protocol(self.document)
        seeds = materialized["seed_sets"]
        self.assertEqual(len(seeds["development"]), 256)
        self.assertEqual(len(seeds["selection_validation"]), 256)
        self.assertEqual(len(seeds["final_confirmation"]), 1000)
        self.assertFalse(
            set(seeds["development"]).intersection(seeds["selection_validation"])
        )
        self.assertFalse(
            set(seeds["development"]).intersection(seeds["final_confirmation"])
        )
        self.assertFalse(
            set(seeds["selection_validation"]).intersection(
                seeds["final_confirmation"]
            )
        )
        for phase in seeds:
            self.assertTrue(
                self.document["seed_contract"]["phases"][phase][
                    "fixed_sample_no_optional_stopping"
                ]
            )

    def test_default_historical_lane_is_explicitly_missing_and_blocks_every_phase(self) -> None:
        materialized = materialize_protocol(self.document)
        history = materialized["historical_baseline_readiness"]
        self.assertEqual(history["policy_id"], HISTORICAL_POLICY_ID)
        self.assertEqual(history["artifact_status"], "NOT_BUILT")
        self.assertFalse(history["artifact_ready"])
        self.assertFalse(history["readiness_complete"])
        self.assertFalse(history["comparison_eligible"])

        plan = build_plan(materialized)
        self.assertEqual(plan["schema_version"], 3)
        self.assertEqual(plan["kind"], PLAN_KIND)
        self.assertFalse(plan["execution_started"])
        self.assertFalse(plan["victory_claim_allowed"])
        self.assertIn(
            "historical_external_v2_artifact_not_ready", plan["blocked_reasons"]
        )
        self.assertIn(
            "historical_external_v2_readiness_not_closed", plan["blocked_reasons"]
        )
        self.assertTrue(
            all(
                phase["dispatch_allowed"] is False
                for phase in plan["readiness"]["phases"].values()
            )
        )

    def test_missing_fourth_lane_or_changed_cell_count_is_rejected(self) -> None:
        missing = copy.deepcopy(self.document)
        missing["baseline_contract"]["required_baselines"].pop()
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "required baseline order"
        ):
            validate_protocol_revision_v3(missing)

        wrong_cells = copy.deepcopy(self.document)
        wrong_cells["evaluation_contract"]["statistics"][
            "simultaneous_test_count"
        ] = 9
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "must be 12"):
            validate_protocol_revision_v3(wrong_cells)

    def test_missing_history_cannot_be_promoted_by_booleans(self) -> None:
        promoted = copy.deepcopy(self.document)
        history = promoted["baseline_contract"]["required_baselines"][-1]
        history["comparison_eligible"] = True
        history["readiness_contract"]["comparison_ready"] = True
        history["readiness_contract"]["satisfied_conditions"] = list(
            history["readiness_contract"]["required_conditions"]
        )
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "missing historical artifact"
        ):
            validate_protocol_revision_v3(promoted)

    def test_optional_stopping_or_final_corpus_leakage_is_rejected(self) -> None:
        stopping = copy.deepcopy(self.document)
        stopping["seed_contract"]["phases"]["development"][
            "fixed_sample_no_optional_stopping"
        ] = False
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "no-stopping"):
            validate_protocol_revision_v3(stopping)

        leaked = copy.deepcopy(self.document)
        leaked["corpus_contract"]["final_confirmation_corpus"][
            "instance_disjoint_from_current_snapshot"
        ] = False
        with self.assertRaisesRegex(FuryMultiseedProtocolError, "future final corpus"):
            validate_protocol_revision_v3(leaked)

    def test_analysis_fails_before_consuming_rows_when_history_is_absent(self) -> None:
        materialized = materialize_protocol(self.document)
        with self.assertRaisesRegex(
            FuryMultiseedProtocolError, "historical External-V2 baseline"
        ):
            analyze_rollouts(
                materialized,
                [],
                runner_plan={},
                reduction_receipt={},
                expected_protocol_sha256=materialized["protocol_sha256"],
                phase="development",
                candidate_id="candidate",
            )

    def test_cli_plan_only_emits_blocked_v3_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            output = Path(raw_tmp) / "plan.json"
            self.assertEqual(main(["--plan-only", "--output", str(output)]), 0)
            plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(plan["kind"], PLAN_KIND)
        self.assertNotEqual(plan["kind"], ANALYSIS_KIND)
        self.assertFalse(plan["execution_started"])
        self.assertFalse(plan["victory_claim_allowed"])

    def test_load_protocol_uses_the_versioned_default(self) -> None:
        materialized = load_protocol(DEFAULT_PROTOCOL)
        self.assertIn(".multiseed.v3.", materialized["protocol"]["protocol_id"])
        self.assertEqual(
            tuple(materialized["required_baseline_ids"]), REQUIRED_BASELINE_IDS
        )


if __name__ == "__main__":
    unittest.main()
