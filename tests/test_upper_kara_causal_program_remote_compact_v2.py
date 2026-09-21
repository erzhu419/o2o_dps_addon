from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.upper_kara_causal_program_remote_compact_v2 import (
    CANDIDATE_MANIFEST_SCHEMA_V2,
    TRAIN_LANE_SIDECAR_SCHEMA_V2,
    TRAIN_TERMINAL_COMMIT_SCHEMA_V2,
    artifact_path_ref_v2,
    compact_training_artifacts_from_v1_v2,
    read_candidate_manifest_v2,
    read_training_bundle_v2,
    read_training_lane_sidecar_v2,
    read_training_terminal_commit_v2,
    restore_freeze_inputs_v2,
    validate_candidate_manifest_v2,
    validate_training_lane_sidecar_v2,
    validate_training_terminal_commit_v2,
    write_candidate_manifest_v2,
    write_lane_sidecar_then_terminal_v2,
)


def _program(program_id: str, wait_ms: int) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=program_id,
        selector=OrderedGuardSelectorV1(
            alternatives=(), fallback=ProgramDecisionV1(wait_ms=wait_ms)
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=("proposal-guide:offline",),
    )


def _receipt(program: CausalActionProgramV1) -> dict:
    return {
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": ["offline"],
        "program": program.to_dict(),
    }


def _lane(program_ref: str, seed: int, arrival: int, damage: float) -> dict:
    return {
        "master_seed": seed,
        "simulator_seed": seed + 10_000,
        "status": "COMPLETE",
        "own_effective_damage": damage,
        "own_effective_dps": damage / 2.0,
        "ttk_ms": 2_000,
        "error": None,
        "program_ref": program_ref,
        "first_wave_arrival_ms": arrival,
    }


def _v1_terminal(
    *,
    seed_shard_index: int = 0,
    examples: tuple[tuple[int, int], ...] = ((10, 0), (11, 3_000)),
) -> dict:
    programs = (_program("searched::zero", 250), _program("searched::new", 500))
    receipts = [_receipt(program) for program in programs]
    example_rows = [
        {"seed": seed, "first_wave_arrival_ms": arrival}
        for seed, arrival in examples
    ]
    lanes = [
        _lane(receipt["program_ref"], seed, arrival, 100.0 + index)
        for index, receipt in enumerate(receipts)
        for seed, arrival in examples
    ]
    return {
        "schema": "upper_kara_causal_program_remote_train_shard/v1",
        "terminal_status": "COMPLETE",
        "campaign_id": "compact-test",
        "build_id": "clean_dual_weapon_probe",
        "campaign_contract": {
            "build_id": "clean_dual_weapon_probe",
            "train_examples": [
                {"seed": 10, "first_wave_arrival_ms": 0},
                {"seed": 11, "first_wave_arrival_ms": 3_000},
                {"seed": 12, "first_wave_arrival_ms": 7_000},
            ],
        },
        "loadout_id": "contra_turtle_burst__mighty_rage",
        "seed_shard_index": seed_shard_index,
        "examples": example_rows,
        "programs": receipts,
        "proposal_guide_ids": ["offline"],
        "lanes": lanes,
        "lane_status_counts": {"COMPLETE": len(lanes)},
        "metric": {"mean_own_effective_damage_across_all_program_lanes": 100.5},
        "completed_at": "2026-09-19T12:00:00Z",
        "contract": {
            "candidate_generation_used_proposal_cohort_only": True,
            "evaluation_cases_materialized": False,
        },
    }


def _compact(value: dict | None = None):
    return compact_training_artifacts_from_v1_v2(
        value or _v1_terminal(),
        candidate_manifest_ref="candidate/manifest.json",
        lane_sidecar_ref="train/shard-00.lanes.json",
    )


def _compact_for_paths(
    manifest_path: Path, sidecar_path: Path, value: dict | None = None
):
    return compact_training_artifacts_from_v1_v2(
        value or _v1_terminal(),
        candidate_manifest_ref=artifact_path_ref_v2(manifest_path),
        lane_sidecar_ref=artifact_path_ref_v2(sidecar_path),
    )


class CompactRemoteStorageV2Tests(unittest.TestCase):
    def test_v1_split_and_restore_preserves_freeze_inputs_exactly(self) -> None:
        legacy = _v1_terminal()
        artifacts = _compact(legacy)

        self.assertEqual(
            CANDIDATE_MANIFEST_SCHEMA_V2,
            artifacts.candidate_manifest["schema"],
        )
        self.assertEqual(
            TRAIN_LANE_SIDECAR_SCHEMA_V2, artifacts.lane_sidecar["schema"]
        )
        self.assertEqual(
            TRAIN_TERMINAL_COMMIT_SCHEMA_V2, artifacts.terminal["schema"]
        )
        self.assertNotIn("programs", artifacts.terminal)
        self.assertNotIn("lanes", artifacts.terminal)
        self.assertNotIn("campaign_contract", artifacts.terminal)
        self.assertNotIn("programs", artifacts.lane_sidecar)
        self.assertNotIn("lanes", artifacts.candidate_manifest)

        programs, lanes = restore_freeze_inputs_v2(
            artifacts.candidate_manifest,
            artifacts.lane_sidecar,
            artifacts.terminal,
        )
        self.assertEqual(legacy["programs"], list(programs))
        self.assertEqual(legacy["lanes"], list(lanes))

    def test_candidate_manifest_is_identical_across_seed_shards(self) -> None:
        first = _compact(_v1_terminal(seed_shard_index=0))
        second = _compact(
            _v1_terminal(seed_shard_index=1, examples=((12, 7_000),))
        )

        self.assertEqual(first.candidate_manifest, second.candidate_manifest)
        self.assertNotEqual(first.lane_sidecar, second.lane_sidecar)
        self.assertNotEqual(first.terminal, second.terminal)

    def test_candidate_receipt_identity_is_recomputed_from_full_program(self) -> None:
        artifacts = _compact()
        corrupted = deepcopy(artifacts.candidate_manifest)
        corrupted["programs"][0]["program_key"] = "not-the-program"

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            validate_candidate_manifest_v2(corrupted)

    def test_restore_rejects_missing_unknown_and_duplicate_lanes(self) -> None:
        artifacts = _compact()
        cases = []

        missing = deepcopy(artifacts.lane_sidecar)
        missing["lanes"].pop()
        cases.append((missing, "coverage"))

        unknown = deepcopy(artifacts.lane_sidecar)
        unknown["lanes"][0]["program_ref"] = "searched::unknown"
        cases.append((unknown, "coverage"))

        duplicate = deepcopy(artifacts.lane_sidecar)
        duplicate["lanes"][1] = deepcopy(duplicate["lanes"][0])
        cases.append((duplicate, "duplicate"))

        for sidecar, reason in cases:
            terminal = deepcopy(artifacts.terminal)
            terminal["lane_count"] = len(sidecar["lanes"])
            terminal["lane_status_counts"] = {"COMPLETE": len(sidecar["lanes"])}
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    restore_freeze_inputs_v2(
                        artifacts.candidate_manifest, sidecar, terminal
                    )

    def test_restore_rejects_cross_artifact_identity_and_status_mismatch(self) -> None:
        artifacts = _compact()

        wrong_ref = deepcopy(artifacts.terminal)
        wrong_ref["candidate_manifest_ref"] = "candidate/other.json"
        with self.assertRaisesRegex(ValueError, "shard identity"):
            restore_freeze_inputs_v2(
                artifacts.candidate_manifest, artifacts.lane_sidecar, wrong_ref
            )

        wrong_status = deepcopy(artifacts.terminal)
        wrong_status["terminal_status"] = "INVALID"
        with self.assertRaisesRegex(ValueError, "status does not match"):
            restore_freeze_inputs_v2(
                artifacts.candidate_manifest, artifacts.lane_sidecar, wrong_status
            )

    def test_invalid_or_failed_lane_metrics_must_be_null(self) -> None:
        artifacts = _compact()
        invalid = deepcopy(artifacts.lane_sidecar)
        invalid["lanes"][0]["status"] = "INVALID"

        with self.assertRaisesRegex(ValueError, "metrics must be null"):
            validate_training_lane_sidecar_v2(invalid)

    def test_atomic_compact_write_read_and_create_only_publication(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "candidate.json"
            sidecar_path = root / "shard-00.lanes.json"
            terminal_path = root / "shard-00.json"
            artifacts = _compact_for_paths(manifest_path, sidecar_path)

            write_candidate_manifest_v2(
                manifest_path, artifacts.candidate_manifest
            )
            write_lane_sidecar_then_terminal_v2(
                candidate_manifest_path=manifest_path,
                lane_sidecar_path=sidecar_path,
                lane_sidecar=artifacts.lane_sidecar,
                terminal_path=terminal_path,
                terminal=artifacts.terminal,
            )

            manifest = read_candidate_manifest_v2(manifest_path)
            sidecar = read_training_lane_sidecar_v2(sidecar_path)
            terminal = read_training_terminal_commit_v2(terminal_path)
            self.assertEqual(artifacts.candidate_manifest, manifest)
            self.assertEqual(artifacts.lane_sidecar, sidecar)
            self.assertEqual(artifacts.terminal, terminal)
            self.assertEqual(
                artifacts,
                read_training_bundle_v2(
                    candidate_manifest_path=manifest_path,
                    lane_sidecar_path=sidecar_path,
                    terminal_path=terminal_path,
                ),
            )
            self.assertEqual(1, terminal_path.read_bytes().count(b"\n"))
            self.assertNotIn(b'"programs"', terminal_path.read_bytes())
            self.assertNotIn(b'"lanes"', terminal_path.read_bytes())
            with self.assertRaises(FileExistsError):
                write_candidate_manifest_v2(
                    manifest_path, artifacts.candidate_manifest
                )

    def test_sidecar_is_published_before_terminal_commit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "candidate.json"
            sidecar_path = root / "shard-00.lanes.json"
            terminal_path = root / "shard-00.json"
            artifacts = _compact_for_paths(manifest_path, sidecar_path)
            write_candidate_manifest_v2(
                manifest_path, artifacts.candidate_manifest
            )
            terminal_path.write_text("already present", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_lane_sidecar_then_terminal_v2(
                    candidate_manifest_path=manifest_path,
                    lane_sidecar_path=sidecar_path,
                    lane_sidecar=artifacts.lane_sidecar,
                    terminal_path=terminal_path,
                    terminal=artifacts.terminal,
                )
            self.assertTrue(sidecar_path.is_file())
            self.assertEqual("already present", terminal_path.read_text(encoding="utf-8"))

    def test_publish_rejects_refs_that_do_not_name_actual_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "candidate.json"
            sidecar_path = root / "shard-00.lanes.json"
            terminal_path = root / "shard-00.json"

            wrong_manifest_ref = _compact_for_paths(
                root / "other-candidate.json", sidecar_path
            )
            write_candidate_manifest_v2(
                manifest_path, wrong_manifest_ref.candidate_manifest
            )
            with self.assertRaisesRegex(ValueError, "actual manifest path"):
                write_lane_sidecar_then_terminal_v2(
                    candidate_manifest_path=manifest_path,
                    lane_sidecar_path=sidecar_path,
                    lane_sidecar=wrong_manifest_ref.lane_sidecar,
                    terminal_path=terminal_path,
                    terminal=wrong_manifest_ref.terminal,
                )
            self.assertFalse(sidecar_path.exists())
            self.assertFalse(terminal_path.exists())

            wrong_sidecar_ref = _compact_for_paths(
                manifest_path, root / "other-shard.lanes.json"
            )
            with self.assertRaisesRegex(ValueError, "actual sidecar path"):
                write_lane_sidecar_then_terminal_v2(
                    candidate_manifest_path=manifest_path,
                    lane_sidecar_path=sidecar_path,
                    lane_sidecar=wrong_sidecar_ref.lane_sidecar,
                    terminal_path=terminal_path,
                    terminal=wrong_sidecar_ref.terminal,
                )
            self.assertFalse(sidecar_path.exists())
            self.assertFalse(terminal_path.exists())

    def test_bundle_reader_rejects_same_content_at_unreferenced_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "candidate.json"
            sidecar_path = root / "shard-00.lanes.json"
            terminal_path = root / "shard-00.json"
            artifacts = _compact_for_paths(manifest_path, sidecar_path)
            write_candidate_manifest_v2(
                manifest_path, artifacts.candidate_manifest
            )
            write_lane_sidecar_then_terminal_v2(
                candidate_manifest_path=manifest_path,
                lane_sidecar_path=sidecar_path,
                lane_sidecar=artifacts.lane_sidecar,
                terminal_path=terminal_path,
                terminal=artifacts.terminal,
            )

            copied_manifest_path = root / "copied-candidate.json"
            copied_sidecar_path = root / "copied-shard.lanes.json"
            copied_manifest_path.write_bytes(manifest_path.read_bytes())
            copied_sidecar_path.write_bytes(sidecar_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "actual manifest path"):
                read_training_bundle_v2(
                    candidate_manifest_path=copied_manifest_path,
                    lane_sidecar_path=sidecar_path,
                    terminal_path=terminal_path,
                )
            with self.assertRaisesRegex(ValueError, "actual sidecar path"):
                read_training_bundle_v2(
                    candidate_manifest_path=manifest_path,
                    lane_sidecar_path=copied_sidecar_path,
                    terminal_path=terminal_path,
                )

    def test_validators_return_detached_objects(self) -> None:
        artifacts = _compact()
        manifest = validate_candidate_manifest_v2(artifacts.candidate_manifest)
        sidecar = validate_training_lane_sidecar_v2(artifacts.lane_sidecar)
        terminal = validate_training_terminal_commit_v2(artifacts.terminal)

        manifest["programs"][0]["program_id"] = "changed"
        sidecar["lanes"][0]["program_ref"] = "changed"
        terminal["training_contract"]["changed"] = True
        self.assertNotEqual(
            "changed", artifacts.candidate_manifest["programs"][0]["program_id"]
        )
        self.assertNotEqual(
            "changed", artifacts.lane_sidecar["lanes"][0]["program_ref"]
        )
        self.assertNotIn("changed", artifacts.terminal["training_contract"])


if __name__ == "__main__":
    unittest.main()
