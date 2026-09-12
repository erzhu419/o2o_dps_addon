from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_team_background_generator_v2 as background_v2
from o2o_dps import chronicle_stage6_old50_overlap_hpc_v1 as audit


INSTANCE = "00000000-0000-0000-0000-000000000001"
ENCOUNTER = "00000000-0000-0000-0000-000000000002"
WAVE = f"{ENCOUNTER}:wave:1"
STAGE6_WAVE = f"{ENCOUNTER}:external-v2-wave:1"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"
FURY_GUID = "0x00000000000000F1"
COMPONENT = "a" * 64


def _raw_contamination() -> dict:
    return {
        "label": "NO_KNOWN_RULE_MATCH",
        "candidate_filter_passed": True,
        "raw_label_candidate_filter_passed": True,
        "candidate_authority": "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP",
        "cohort_assignment": "TRAINING_CANDIDATE",
        "cohort_reason": "fixture",
        "cohort_receipt_content_sha256": "d" * 64,
        "time_field": "started_at",
        "started_at": "2026-09-03T12:00:00Z",
        "uploaded_at_used": False,
        "player_name_used": False,
    }


def _normalized_contamination() -> dict:
    return {
        "label": "NO_KNOWN_RULE_MATCH",
        "training_candidate": True,
        "raw_label_candidate_filter_passed": True,
        "candidate_authority": "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP",
        "cohort_assignment": "TRAINING_CANDIDATE",
        "cohort_reason": "fixture",
        "cohort_receipt_content_sha256": "d" * 64,
        "time_field": "started_at",
        "started_at": "2026-09-03T12:00:00Z",
        "uploaded_at_used": False,
        "player_name_used": False,
        "named_player_blacklist_or_weighting_used": False,
        "scope": "raid_instance",
    }


def _target(guid: str, index: int, *, start: int = 0, end: int = 1000) -> dict:
    return {
        "target_guid": guid,
        "target_index": index,
        "observed_hostile_activity_proxy": {"start_ms": start, "end_ms": end},
    }


def _scenario(guids: list[str]) -> dict:
    stacked = len(guids) > 1
    core = {
        "scenario_id": "scenario-1",
        "source_identity": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "wave_id": WAVE,
            "wave_ordinal": 1,
        },
        "horizon": {"milliseconds": 1000},
        "layout": {
            "variant": "cohit_stacked" if stacked else "single_target",
            "side": "evidence_bounded",
            "sensitivity_family": WAVE,
            "spatial_assumption": {
                "coordinates": None,
                "not_observed": True,
                "status": "INFERRED" if stacked else "RECONSTRUCTED",
                "value": "stacked" if stacked else "single_target",
            },
            "coordinates_exact": {"value": None, "status": "MISSING"},
        },
        "targets": [_target(guid, index) for index, guid in enumerate(guids)],
        "runner_projection": {"component_id": "old-component"},
    }
    return {**core, "capsule_sha256": hashlib.sha256(audit._canonical(core)).hexdigest()}


def _event(guid: str, index: int, time_ms: int) -> dict:
    return {"target_guid": guid, "target_index": index, "time_ms": time_ms}


def _stage5_wave() -> dict:
    player = {
        "player": {"guid": FURY_GUID, "class": "WARRIOR"},
        "warrior_spec_lane": {
            "partition_key": "WARRIOR_FURY",
            "observed_spec": "Fury",
            "evidence_status": "OBSERVED",
            "exact_guid_match": True,
        },
        "eligibility_observation": {
            "historical_fury_candidate_filter_passed": True
        },
        "leave_one_player_out_background": {
            "excluded_focal_event_count": 1,
            "excluded_focal_damage_amount": 123,
        },
    }
    return background_v2._content_addressed(
        {
            "wave": {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "encounter_ordinal": 1,
                "wave_id": STAGE6_WAVE,
                "wave_ordinal": 1,
            },
            "players": [player],
            "raid_provenance": {"contamination": _raw_contamination()},
        }
    )


def _block(
    guids: list[str], *, time_ms: int = 500, stage5_sha: str | None = None
) -> dict:
    contamination = _normalized_contamination()
    core = {
        "wave": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "encounter_ordinal": 1,
            "wave_id": STAGE6_WAVE,
            "wave_ordinal": 1,
        },
        "component_id": COMPONENT,
        "source_model": {
            "wave_content_sha256": stage5_sha or ("b" * 64),
            "exact_trace_content_sha256": "c" * 64,
        },
        "contamination_lane": contamination,
        "roster_player_guids": [FURY_GUID],
        "target_registry": [
            {"target_guid": guid, "target_index": index}
            for index, guid in enumerate(guids)
        ],
        "runtime_candidate_schedule": [_event(guids[0], 0, time_ms)],
        "nonruntime_damage_diagnostics": [],
        "dead_marker_diagnostics": [],
        "negative_damage_diagnostics": [],
    }
    return background_v2._content_addressed(core)


def _candidate(block: dict, *, relation: str = "OVERLAP_PROJECTION_EXACT") -> dict:
    split_authority = (
        "EQUIVALENT_ON_OVERLAP"
        if relation == "OVERLAP_PROJECTION_EXACT"
        else "STAGE6_COARSER_COMPONENT"
        if relation == "OLD50_PROJECTION_STRICT_SUBSET_STAGE6_AUTHORITY"
        else "NONE_REJECT"
    )
    return {
        "scenario_id": "scenario-1",
        "capsule_sha256": None,
        "stage6_block_content_sha256": block["content_address"]["sha256"],
        "component_relation": {
            "old50_component_id": "old-component",
            "stage6_component_id": COMPONENT,
            "comparison_universe": "20_INSTANCE_INTERSECTION",
            "old50_equivalence_class": [INSTANCE],
            "stage6_equivalence_class": [INSTANCE],
            "relation": relation,
            "split_authority": split_authority,
            "raw_component_id_equality_required": False,
        },
    }


class ChronicleStage6Old50OverlapHpcV1Tests(unittest.TestCase):
    def test_equal_guid_set_is_exact_and_strict_pile_is_subset(self) -> None:
        block = _block([TARGET_A, TARGET_B])
        contamination = block["contamination_lane"]
        exact_scenario = _scenario([TARGET_A, TARGET_B])
        exact = audit.audit_pair(
            scenario=exact_scenario,
            block=block,
            candidate=_candidate(block),
            expected_contamination=contamination,
        )
        subset_scenario = _scenario([TARGET_A])
        subset = audit.audit_pair(
            scenario=subset_scenario,
            block=block,
            candidate=_candidate(block),
            expected_contamination=contamination,
        )
        self.assertEqual("EXACT", exact["classification"])
        self.assertEqual("SUBSET", subset["classification"])
        self.assertEqual("STRICT_SUBSET", subset["target_join"]["guid_relation"])
        self.assertFalse(
            subset["health_boundary"]["observed_kill_or_damage_budget_used_as_exact_hp"]
        )

    def test_stage6_coarser_component_is_conservative_split_authority(self) -> None:
        relation = audit._component_relation(
            old_component="old",
            stage6_component=COMPONENT,
            common_instances={INSTANCE, "second"},
            old_instances_by_component={"old": {INSTANCE}},
            stage6_instances_by_component={COMPONENT: {INSTANCE, "second"}},
        )
        block = _block([TARGET_A])
        result = audit.audit_pair(
            scenario=_scenario([TARGET_A]),
            block=block,
            candidate=_candidate(block, relation=relation["relation"])
            | {"component_relation": relation},
            expected_contamination=_normalized_contamination(),
        )
        self.assertEqual("EXACT", result["classification"])
        self.assertEqual(
            "STAGE6_COARSER_COMPONENT", result["component_join"]["split_authority"]
        )
        self.assertIn(
            "STAGE6_COARSER_LEAKAGE_COMPONENT_RETAINED_AS_SPLIT_AUTHORITY",
            result["reason_codes"],
        )

    def test_out_of_horizon_and_component_conflict_are_precise_rejects(self) -> None:
        block = _block([TARGET_A], time_ms=1001)
        result = audit.audit_pair(
            scenario=_scenario([TARGET_A]),
            block=block,
            candidate=_candidate(
                block, relation="OVERLAP_PROJECTION_CONFLICT"
            ),
            expected_contamination=block["contamination_lane"],
        )
        self.assertEqual("REJECT", result["classification"])
        self.assertIn(
            "SELECTED_STAGE6_EVENT_OUTSIDE_CAPSULE_HORIZON",
            result["reason_codes"],
        )
        self.assertIn(
            "COMPONENT_EQUIVALENCE_CLASS_MISMATCH_ON_OVERLAP",
            result["reason_codes"],
        )

    def test_wave_namespace_is_validated_and_ordinal_only_is_disallowed(self) -> None:
        block = _block([TARGET_A])
        block["wave"]["wave_id"] = "unbound-wave-1"
        block = background_v2._content_addressed(
            {key: value for key, value in block.items() if key != "content_address"}
        )
        with self.assertRaisesRegex(
            audit.Stage6Old50OverlapError, "wave_id does not encode"
        ):
            audit.audit_pair(
                scenario=_scenario([TARGET_A]),
                block=block,
                candidate=_candidate(block),
                expected_contamination=block["contamination_lane"],
            )

    def test_worker_opens_stage6_partition_once_and_reducer_preserves_nonvoting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "inputs"
            input_dir.mkdir()
            stage5_wave = _stage5_wave()
            block = _block(
                [TARGET_A],
                stage5_sha=stage5_wave["content_address"]["sha256"],
            )
            logical = audit._canonical(block) + b"\n"
            partition_path = input_dir / "partition.jsonl.gz"
            with partition_path.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                    zipped.write(logical)
            partition_binding = {
                "locator": "inputs/partition.jsonl.gz",
                "compressed_file_sha256": audit._sha256_file(partition_path),
                "compressed_size_bytes": partition_path.stat().st_size,
                "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
                "logical_size_bytes": len(logical),
                "record_count": 1,
            }
            stage5_logical = audit._canonical(stage5_wave) + b"\n"
            stage5_partition_path = input_dir / "stage5.jsonl.gz"
            with stage5_partition_path.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                    zipped.write(stage5_logical)
            stage5_partition_binding = {
                "locator": "inputs/stage5.jsonl.gz",
                "compressed_file_sha256": audit._sha256_file(stage5_partition_path),
                "compressed_size_bytes": stage5_partition_path.stat().st_size,
                "logical_content_sha256": hashlib.sha256(stage5_logical).hexdigest(),
                "logical_size_bytes": len(stage5_logical),
                "record_count": 1,
            }
            contamination = _raw_contamination()
            instance_entry = background_v2._content_addressed(
                {"instance_id": INSTANCE, "partition": partition_binding}
            )
            manifest = background_v2._content_addressed(
                {
                    "schema": background_v2.SCHEMA,
                    "implementation_revision": background_v2.IMPLEMENTATION_REVISION,
                    "status": background_v2.STATUS,
                    "instances": [instance_entry],
                }
            )
            manifest_path = input_dir / "manifest.json"
            manifest_payload = audit._canonical(manifest) + b"\n"
            manifest_path.write_bytes(manifest_payload)
            stage5_entry = background_v2._content_addressed(
                {"instance_id": INSTANCE, "partition": stage5_partition_binding}
            )
            stage5_manifest = background_v2._content_addressed(
                {"instances": [stage5_entry]}
            )
            stage5_manifest_path = input_dir / "stage5-manifest.json"
            stage5_manifest_payload = audit._canonical(stage5_manifest) + b"\n"
            stage5_manifest_path.write_bytes(stage5_manifest_payload)
            capsule_path = input_dir / "capsule.json.gz"
            capsule_path.write_bytes(b"fixture")
            scenario = _scenario([TARGET_A])
            candidate = _candidate(block)
            candidate["capsule_sha256"] = scenario["capsule_sha256"]
            candidate["key"] = {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "wave_ordinal": 1,
            }
            plan = audit._content_addressed(
                {
                    "schema": audit.PLAN_SCHEMA,
                    "revision": audit.REVISION,
                    "status": "PLANNED_NONVOTING_AUDIT",
                    "inputs": {
                        "stage6_manifest": {
                            "locator": "inputs/manifest.json",
                            "content_sha256": manifest["content_address"]["sha256"],
                            "file_sha256": hashlib.sha256(manifest_payload).hexdigest(),
                        },
                        "old50_capsule": {
                            "locator": "inputs/capsule.json.gz",
                            "content_sha256": "capsule-content",
                            "file_sha256": "capsule-file",
                        },
                        "stage5_manifest": {
                            "locator": "inputs/stage5-manifest.json",
                            "content_sha256": stage5_manifest["content_address"]["sha256"],
                            "file_sha256": hashlib.sha256(
                                stage5_manifest_payload
                            ).hexdigest(),
                        },
                    },
                    "overlap": {
                        "common_instance_count": 1,
                        "common_instance_ids": [INSTANCE],
                        "candidate_exact_key_count": 1,
                    },
                    "shards": [
                        {
                            "instance_id": INSTANCE,
                            "assigned_node": "node001",
                            "instance_entry_content_sha256": instance_entry[
                                "content_address"
                            ]["sha256"],
                            "stage5_instance_entry_content_sha256": stage5_entry[
                                "content_address"
                            ]["sha256"],
                            "partition": partition_binding,
                            "stage5_partition": stage5_partition_binding,
                            "stage6_contamination_lane": contamination,
                            "stage5_contamination_lane": contamination,
                            "candidate_count": 1,
                            "candidates": [candidate],
                            "old50_wave_count": 1,
                            "stage6_wave_count": 1,
                        }
                    ],
                }
            )
            plan_path = audit.save_plan(plan, root / "run")
            fake_bundle = {"scenarios": [scenario]}
            original_open = Path.open
            partition_opens = 0
            stage5_partition_opens = 0

            def counted_open(path_self: Path, *args, **kwargs):
                nonlocal partition_opens
                nonlocal stage5_partition_opens
                if path_self.resolve() == partition_path.resolve() and args[:1] == ("rb",):
                    partition_opens += 1
                if path_self.resolve() == stage5_partition_path.resolve() and args[:1] == ("rb",):
                    stage5_partition_opens += 1
                return original_open(path_self, *args, **kwargs)

            with (
                mock.patch.object(
                    audit,
                    "_load_capsule",
                    return_value=(fake_bundle, "capsule-content", "capsule-file"),
                ),
                mock.patch.object(background_v2, "_validate_block"),
                mock.patch.object(audit.model_v2, "_validate_model_wave"),
                mock.patch.object(Path, "open", counted_open),
            ):
                worker = audit.run_worker(
                    plan_path=plan_path,
                    shared_root=root,
                    output_directory=root / "run",
                    instance_id=INSTANCE,
                    node="node001",
                )
            self.assertEqual(1, partition_opens)
            self.assertEqual(1, stage5_partition_opens)
            self.assertEqual({"EXACT": 1, "SUBSET": 0, "REJECT": 0}, worker["classification_counts"])
            reduced = audit.reduce_audit(
                plan_path=plan_path, output_directory=root / "run"
            )
            self.assertEqual(1, reduced["summary"]["EXACT"])
            self.assertEqual(
                1, reduced["summary"]["accepted_with_exact_fury_focal_guid"]
            )
            self.assertEqual(1, reduced["summary"]["dynamic_v3_compiler_input_count"])
            saved = json.loads(Path(reduced["audit"]).read_text(encoding="utf-8"))
            self.assertTrue(saved["scientific_boundary"]["nonvoting"])
            self.assertFalse(saved["scientific_boundary"]["comparison_ready"])
            compiler_input = saved["compiler_input_schema"]["inputs"][0]
            audit.validate_compiler_input(compiler_input)
            self.assertEqual(
                "EXACT_CONTENT_ADDRESSED_STAGE6_BLOCK",
                compiler_input["causal_schedule_projection"]["selection_unit"],
            )
            self.assertFalse(
                compiler_input["causal_schedule_projection"][
                    "component_wide_random_draw_allowed"
                ]
            )

    @unittest.skipUnless(
        (
            Path(__file__).resolve().parents[1]
            / "offline_data/derived/fury_offline_scenario_capsules/v2"
            / "fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
        ).is_file(),
        "requires the ignored old50 capsule",
    )
    def test_real_old50_capsule_validates_without_large_sources(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "offline_data/derived/fury_offline_scenario_capsules/v2"
            / "fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
        )
        bundle, content_sha, file_sha = audit._load_capsule(path)
        self.assertEqual(1197, len(bundle["scenarios"]))
        self.assertEqual(
            "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e",
            content_sha,
        )
        self.assertEqual(
            "f1e80f84cbf40c42f60bc82426eb66d2628143c4b1cb2d281a75f9920732a45c",
            file_sha,
        )


if __name__ == "__main__":
    unittest.main()
