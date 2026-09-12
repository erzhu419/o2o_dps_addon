from __future__ import annotations

import json
import math
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_current_cat2_heldout_replay_v1 import (
    CAT2_ID,
    DEFAULT_CAT2_PROFILE_SHA256,
    DEFAULT_FROZEN_GATE_SHA256,
    FuryCurrentCat2HeldoutReplayError,
    InputFileIdentity,
    RunInputSnapshot,
    SameByteDocumentStore,
    _DiagnosticAccumulator,
    _require_digest,
    _snapshot_json_file,
    _snapshot_file,
    build_artifact,
    build_request_contract,
    capture_file_inputs,
    evaluate_supplemental_replay,
    main,
    make_run_input_snapshot,
    parse_input_lock,
    reconstruct_frozen_corpus,
    require_matching_input_lock,
    run_input_snapshot_document,
    stage_bridge_binary,
    verify_run_input_snapshot,
    write_artifact_with_receipt,
)
from o2o_dps.fury_heldout_corpus_gate_v1 import SelectedCorpus, SelectedFamily
from o2o_dps.fury_policy_optimization_v1 import FuryPolicyParameters, PolicyScenario


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_GATE = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_heldout_corpus_gate_v1.json"
)
CAT2_PROFILE = PROJECT_ROOT / "offline_data" / "reports" / "cat2_saved_profile_v1.json"


def _family(
    name: str = "heldout__family",
    *,
    horizon_ms: int = 10_000,
    weight: float = 2.0,
    request: dict | None = None,
) -> SelectedFamily:
    scenario = PolicyScenario(
        scenario_id=name,
        request=request or {"encounter": {"targets": [{}]}},
        horizon_ms=horizon_ms,
        weight=weight,
        provenance={"instance_id": "heldout"},
    )
    return SelectedFamily(
        scenario=scenario,
        instance_id="heldout",
        family_id=name,
        catalog_relative_path="catalog.json.gz",
        target_count=1,
        target_count_stratum="1",
        duration_stratum="short_le_10s",
    )


class _Cat2Adapter:
    expert_id = CAT2_ID

    def __init__(self, snapshot):
        self.snapshot = snapshot


class CurrentCat2SupplementalReplayTests(unittest.TestCase):
    def test_default_inputs_are_content_addressed(self) -> None:
        self.assertEqual(
            _require_digest(FROZEN_GATE, DEFAULT_FROZEN_GATE_SHA256, "gate"),
            DEFAULT_FROZEN_GATE_SHA256,
        )
        self.assertEqual(
            _require_digest(CAT2_PROFILE, DEFAULT_CAT2_PROFILE_SHA256, "profile"),
            DEFAULT_CAT2_PROFILE_SHA256,
        )

    def test_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "SHA-256 mismatch"
            ):
                _require_digest(path, "0" * 64, "input")

    def test_reconstructs_exact_frozen_scenario_contract(self) -> None:
        frozen = json.loads(FROZEN_GATE.read_text(encoding="utf-8"))
        corpus, contract = reconstruct_frozen_corpus(frozen)
        self.assertEqual(len(corpus.families), 392)
        self.assertEqual(len(contract["validation_seeds"]), 16)
        self.assertEqual(
            contract["selected_families"], frozen["selected_corpus"]["families"]
        )
        self.assertEqual(
            contract["candidate_parameters"], frozen["candidate"]["parameters"]
        )
        self.assertEqual(
            contract["candidate_policy_id"], frozen["candidate"]["policy_id"]
        )
        reconstructed = FuryPolicyParameters(**contract["candidate_parameters"])
        self.assertEqual(reconstructed.single_target_priority, "BLOODTHIRST_FIRST")
        self.assertEqual(reconstructed.two_hand_slam_mode, "DISABLED")

    def test_changed_frozen_selection_fails_closed(self) -> None:
        frozen = json.loads(FROZEN_GATE.read_text(encoding="utf-8"))
        frozen["selected_corpus"]["families"][0]["sampling_weight"] += 1
        with self.assertRaisesRegex(
            FuryCurrentCat2HeldoutReplayError, "scenario metadata differs"
        ):
            reconstruct_frozen_corpus(frozen)

    @staticmethod
    def _rollout(dps: float, horizon_ms: int):
        return {
            "damage_delta": dps * horizon_ms / 1000.0,
            "dps": dps,
            "configured_horizon_complete": True,
            "omitted_lane_count": 0,
            "nonfaithful_reason_counts": {},
            "source_execution": False,
            "exact_lua_replay": False,
        }

    def test_evaluation_is_four_way_but_never_voting(self) -> None:
        corpus = SelectedCorpus((_family(),), ())

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            dps = {
                "cat.fury.profile1": 100.0,
                "contra.deployed.fury.raid_a": 90.0,
                CAT2_ID: 105.0,
            }.get(adapter.expert_id, 110.0)
            return self._rollout(dps, horizon_ms)

        with (
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "Cat2SavedProfileSourceAdapterV1",
                _Cat2Adapter,
            ),
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "run_fury_expert_closed_loop",
                side_effect=fake_rollout,
            ),
        ):
            result = evaluate_supplemental_replay(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                candidate_policy_id="frozen.candidate.fixture",
                cat2_snapshot={},
                validation_seeds=(1, 2),
            )
        self.assertEqual(result["rollout_count"], 8)
        self.assertEqual(len(result["overall"]["ranking"]), 4)
        self.assertEqual(
            set(result["overall"]["current_cat2_vs_each_reference"]),
            {
                "cat.fury.profile1",
                "contra.deployed.fury.raid_a",
                "frozen.candidate.fixture",
            },
        )
        self.assertFalse(result["voting_result"])
        self.assertFalse(result["deployment_gate_passed"])
        self.assertFalse(result["real_game_superiority_gate_passed"])

    def test_weighted_aggregation_respects_distinct_family_horizons_and_weights(self) -> None:
        corpus = SelectedCorpus(
            (
                _family("short", horizon_ms=1_000, weight=1.0),
                _family("long", horizon_ms=3_000, weight=3.0),
            ),
            (),
        )

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            if adapter.expert_id == CAT2_ID:
                dps = 100.0 if horizon_ms == 1_000 else 200.0
            else:
                dps = 50.0
            return self._rollout(dps, horizon_ms)

        with (
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "Cat2SavedProfileSourceAdapterV1",
                _Cat2Adapter,
            ),
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "run_fury_expert_closed_loop",
                side_effect=fake_rollout,
            ),
        ):
            result = evaluate_supplemental_replay(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                cat2_snapshot={},
                validation_seeds=(1,),
            )
        cat2 = next(
            row for row in result["overall"]["ranking"] if row["expert_id"] == CAT2_ID
        )
        self.assertAlmostEqual(cat2["weighted_mean_dps"], 190.0)
        self.assertEqual(cat2["rollout_count"], 2)

    def test_request_contract_rejects_nan_and_infinity(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            corpus = SelectedCorpus(
                (_family(request={"encounter": {"duration": value}}),), ()
            )
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "not finite strict JSON"
            ):
                build_request_contract(corpus)

    def test_request_only_mutation_is_detected_even_when_metadata_is_unchanged(self) -> None:
        request = {"encounter": {"targets": [{"level": 60}]}}
        corpus = SelectedCorpus((_family(request=request),), ())
        snapshot = make_run_input_snapshot((), corpus)
        request["encounter"]["targets"][0]["level"] = 61
        with self.assertRaisesRegex(
            FuryCurrentCat2HeldoutReplayError, "request contract changed"
        ):
            verify_run_input_snapshot(snapshot, corpus)

    def test_each_file_role_mutation_is_detected_after_capture(self) -> None:
        roles = (
            "frozen_heldout_gate",
            "cat2_profile_artifact",
            "chronicle_manifest",
            "completed_scenario_catalog",
            "simulator_bridge_binary",
        )
        for role in roles:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                files = []
                for index, current_role in enumerate(roles):
                    path = root / f"{index}.bin"
                    path.write_bytes(f"before-{current_role}".encode())
                    files.append(_snapshot_file(path, current_role))
                corpus = SelectedCorpus((_family(),), ())
                snapshot = make_run_input_snapshot(files, corpus)
                Path(next(row.path for row in files if row.role == role)).write_bytes(
                    b"after"
                )
                with self.assertRaisesRegex(
                    FuryCurrentCat2HeldoutReplayError, "run input changed"
                ):
                    verify_run_input_snapshot(snapshot, corpus)

    def test_content_addressed_input_lock_round_trip_and_mismatch(self) -> None:
        corpus = SelectedCorpus((_family(),), ())
        snapshot = make_run_input_snapshot((), corpus)
        document = run_input_snapshot_document(snapshot)
        self.assertEqual(parse_input_lock(document), snapshot)
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "input-lock.json"
            lock.write_text(
                json.dumps(document, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            digest = _snapshot_file(lock, "supplemental_input_lock").sha256
            identity = require_matching_input_lock(lock, digest, snapshot)
            self.assertEqual(identity.sha256, digest)
            changed = make_run_input_snapshot(
                (),
                SelectedCorpus(
                    (_family(request={"encounter": {"targets": [{"level": 61}]}}),),
                    (),
                ),
            )
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "do not match"
            ):
                require_matching_input_lock(lock, digest, changed)

    def test_sha_named_bridge_stage_is_an_independent_byte_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "bridge.exe"
            source_path.write_bytes(b"bridge-v1")
            source = _snapshot_file(source_path, "simulator_bridge_binary")
            staged = stage_bridge_binary(source, root / "stage")
            self.assertEqual(staged.sha256, source.sha256)
            self.assertIn(source.sha256, Path(staged.path).name)
            source_path.write_bytes(b"bridge-v2")
            self.assertEqual(Path(staged.path).read_bytes(), b"bridge-v1")

    def test_private_atomic_writer_commits_verified_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "smoke.json"
            snapshot = make_run_input_snapshot((), SelectedCorpus((_family(),), ()))
            artifact = {
                "evaluation": {
                    "status": "SMOKE_COMPLETED",
                    "evaluation_scope": "NON_EVALUATIVE_SMOKE_SUBSET",
                    "expected_rollout_count": 4,
                    "rollout_count": 4,
                    "full_replay_completed": False,
                }
            }
            receipt = write_artifact_with_receipt(
                output, artifact, run_input_snapshot=snapshot
            )
            self.assertEqual(receipt["artifact_status"], "SMOKE_COMPLETED")
            self.assertEqual(
                receipt["evaluation_scope"], "NON_EVALUATIVE_SMOKE_SUBSET"
            )
            self.assertEqual(receipt["expected_rollout_count"], 4)
            self.assertEqual(receipt["actual_rollout_count"], 4)
            self.assertFalse(receipt["full_replay_completed"])
            self.assertTrue(Path(str(output) + ".receipt.json").is_file())

    def test_full_mode_refuses_to_start_without_content_addressed_lock(self) -> None:
        with self.assertRaisesRegex(
            FuryCurrentCat2HeldoutReplayError, "full replay requires"
        ):
            main(())

    def test_writer_rejects_rollout_count_mismatch_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bad.json"
            snapshot = make_run_input_snapshot((), SelectedCorpus((_family(),), ()))
            artifact = {
                "evaluation": {
                    "status": "SMOKE_COMPLETED",
                    "evaluation_scope": "NON_EVALUATIVE_SMOKE_SUBSET",
                    "expected_rollout_count": 4,
                    "rollout_count": 3,
                    "full_replay_completed": False,
                }
            }
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "rollout count"
            ):
                write_artifact_with_receipt(
                    output, artifact, run_input_snapshot=snapshot
                )
            self.assertFalse(output.exists())

    def test_same_byte_gzip_loader_survives_aba_without_reopening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json.gz"
            original = gzip.compress(json.dumps({"value": "A"}).encode())
            path.write_bytes(original)
            store = SameByteDocumentStore()
            identity = store.capture(path, "completed_scenario_catalog")
            path.write_bytes(gzip.compress(json.dumps({"value": "B"}).encode()))
            path.write_bytes(original)
            self.assertEqual(store.load(path), {"value": "A"})
            self.assertEqual(store.identity(path), identity)
            self.assertEqual(store.load_count(path), 1)

    def test_json_identity_and_decode_use_one_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "value.json"
            path.write_text('{"value":1}', encoding="utf-8")
            original = Path.read_bytes
            calls: list[Path] = []

            def counted(current: Path) -> bytes:
                calls.append(current)
                return original(current)

            with patch.object(Path, "read_bytes", new=counted):
                identity, value = _snapshot_json_file(path, "test_json")
            self.assertEqual(value, {"value": 1})
            self.assertEqual(identity.size_bytes, len(b'{"value":1}'))
            self.assertEqual(calls, [path.resolve()])

    def test_accumulator_rejects_nonfinite_rollout_and_weight_before_mutation(self) -> None:
        expert_ids = (
            "cat.fury.profile1",
            "contra.deployed.fury.raid_a",
            "candidate",
            CAT2_ID,
        )
        base_rows = {
            key: self._rollout(100.0, 1_000) for key in expert_ids
        }
        for field, value in (
            ("damage_delta", math.nan),
            ("damage_delta", math.inf),
            ("dps", -math.inf),
        ):
            rows = json.loads(json.dumps(base_rows))
            rows[CAT2_ID][field] = value
            accumulator = _DiagnosticAccumulator(expert_ids, CAT2_ID)
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "must be finite"
            ):
                accumulator.add(rows, weight=1.0, horizon_ms=1_000)
            self.assertEqual(
                accumulator.metrics["cat.fury.profile1"]["rollout_count"], 0
            )
        accumulator = _DiagnosticAccumulator(expert_ids, CAT2_ID)
        with self.assertRaisesRegex(
            FuryCurrentCat2HeldoutReplayError, "weight must be finite"
        ):
            accumulator.add(base_rows, weight=math.inf, horizon_ms=1_000)

    def test_exact_source_claim_is_rejected(self) -> None:
        corpus = SelectedCorpus((_family(),), ())

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            row = self._rollout(100.0, horizon_ms)
            if adapter.expert_id == CAT2_ID:
                row["source_execution"] = True
            return row

        with (
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "Cat2SavedProfileSourceAdapterV1",
                _Cat2Adapter,
            ),
            patch(
                "o2o_dps.fury_current_cat2_heldout_replay_v1."
                "run_fury_expert_closed_loop",
                side_effect=fake_rollout,
            ),
        ):
            with self.assertRaisesRegex(
                FuryCurrentCat2HeldoutReplayError, "invalid exact-source claim"
            ):
                evaluate_supplemental_replay(
                    object(),
                    corpus,
                    candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                    cat2_snapshot={},
                    validation_seeds=(1,),
                )

    def test_artifact_keeps_all_gates_false(self) -> None:
        snapshot = json.loads(CAT2_PROFILE.read_text(encoding="utf-8"))
        artifact = build_artifact(
            frozen_gate_path=FROZEN_GATE,
            frozen_gate_sha256=DEFAULT_FROZEN_GATE_SHA256,
            frozen_input_contract={
                "candidate_policy_id": "candidate",
                "candidate_parameters": {},
                "validation_seeds": [1],
            },
            cat2_profile_path=CAT2_PROFILE,
            cat2_profile_sha256=DEFAULT_CAT2_PROFILE_SHA256,
            cat2_snapshot=snapshot,
            run_input_snapshot=RunInputSnapshot((), "x" * 64, {}),
            evaluation={"status": "COMPLETED"},
        )
        self.assertEqual(
            artifact["current_cat2"]["authority_state"],
            "CURRENT_UNSEALED_SOURCE_PROFILE",
        )
        self.assertEqual(
            artifact["current_cat2"]["provenance_kind"], "SOURCE_DERIVED"
        )
        self.assertFalse(artifact["current_cat2"]["role_can_vote"])
        self.assertFalse(artifact["heldout_gate_modified"])
        self.assertFalse(artifact["heldout_gate_passed"])
        self.assertFalse(artifact["deployment_allowed"])
        self.assertFalse(artifact["real_game_superiority_claimed"])

    def test_plan_only_writes_independent_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "supplement.json"
            result = main(("--plan-only", "--output", str(output)))
            self.assertEqual(result, 0)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                artifact["kind"],
                "fury_current_cat2_supplemental_heldout_replay_v1",
            )
            self.assertEqual(artifact["evaluation"]["status"], "NOT_RUN")
            self.assertTrue(Path(str(output) + ".receipt.json").is_file())
            self.assertFalse(artifact["heldout_gate_modified"])
            self.assertFalse(artifact["deployment_allowed"])
            roles = [
                row["role"] for row in artifact["run_input_snapshot"]["files"]
            ]
            self.assertEqual(roles.count("completed_scenario_catalog"), 50)
            self.assertGreaterEqual(roles.count("evaluator_python_source"), 10)
            self.assertEqual(
                artifact["run_input_snapshot"]["request_contract"]["family_count"],
                392,
            )
            self.assertFalse(
                artifact["protocol_relationship"][
                    "historical_request_byte_equality_claimed"
                ]
            )


if __name__ == "__main__":
    unittest.main()
