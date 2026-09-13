from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from o2o_dps import historical_behavior_clone_v2 as clone_v2
from o2o_dps import historical_build_catalog_v1 as catalog_v1
from o2o_dps import historical_fury_decision_build_join_v1 as join_v1
from o2o_dps import historical_fury_behavior_prototypes_v1 as prototypes_v1
from o2o_dps import historical_fury_prototype_build_gap_priority_v1 as gap_v1
from o2o_dps import historical_fury_source_bound_prototype_bundle_v1 as source_v1
from tests.test_historical_behavior_clone_v2 import (
    _binding as _clone_binding,
    _prototype as _clone_prototype,
    _records as _clone_records,
)
from tests.test_historical_build_catalog_v1 import (
    GUID,
    _coverage,
    _instance,
    _record,
)


PROTOTYPE_ID = "fixture_prototype"


def _canonical(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _write_gzip(path: Path, logical: bytes) -> dict[str, object]:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(logical)
    compressed = path.read_bytes()
    return {
        "path": path.name,
        "record_count": logical.count(b"\n"),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
        "compressed_size_bytes": len(compressed),
        "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
        "gzip_mtime": 0,
    }


def _content_address(core: dict[str, object]) -> dict[str, object]:
    result = deepcopy(core)
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }
    return result


def _readdress(value: dict[str, object]) -> dict[str, object]:
    core = deepcopy(value)
    core.pop("content_address", None)
    return _content_address(core)


def _catalog_segment() -> dict[str, object]:
    record = _record(
        timestamp_ms=1_100,
        event_index=10,
        ordinal=0,
        item_id=100,
        representative=True,
    )
    return next(
        row
        for row in catalog_v1.compile_instance_segments(
            _instance((record,)), coverage_registry=_coverage()
        )
        if row["identity"]["player_guid"] == GUID
    )


def _dictionary_row(segment: dict[str, object]) -> dict[str, object]:
    portable = join_v1._portable_projection(segment)
    source_sha = hashlib.sha256(_canonical(portable)).hexdigest()
    equipment = segment["equipment"]
    talents = segment["talents"]
    valid_from = segment["observation"]["valid_from"]
    coverage = segment["coverage"]
    return {
        "schema": join_v1.SEGMENT_DICTIONARY_SCHEMA,
        "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
        "segment_ref": f"sha256:{source_sha}",
        "portable_source_segment_content_sha256": source_sha,
        "catalog_line_number": 1,
        "identity": deepcopy(segment["identity"]),
        "valid_from": {
            key: valid_from.get(key)
            for key in (
                "timestamp_ms",
                "encounter_id",
                "event_index",
                "message_ordinal",
            )
        },
        "equipment_content_sha256": hashlib.sha256(
            _canonical(join_v1._portable_projection(equipment))
        ).hexdigest(),
        "talents_content_sha256": hashlib.sha256(
            _canonical(join_v1._portable_projection(talents))
        ).hexdigest(),
        "coverage_flags": {
            "runtime_executable": coverage["runtime_executable"] is True,
            "representative_build_eligible": (
                coverage["representative_build_eligible"] is True
            ),
            "development_build_eligible": (
                coverage["development_build_eligible"] is True
            ),
            "comparison_eligible": coverage["comparison"]["eligible"] is True,
        },
        "decision_support": {
            "all_source": 2,
            "by_prototype": {PROTOTYPE_ID: 2},
            "prototype_member_union": 2,
        },
        "semantic_talent_count": len(talents["semantic_ranks"]),
        "semantic_talent_rank_vector_sha256": hashlib.sha256(
            _canonical(talents["semantic_ranks"])
        ).hexdigest(),
        "weapon_mode": join_v1._weapon_mode(segment),
    }


def _fixture(root: Path, *, zero_runtime_segments: bool = False) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    catalog_directory = root / "catalog"
    join_directory = root / "join"
    gap_directory = root / "gap"
    prototype_directory = root / "prototypes"
    model_directory = root / "models"
    for directory in (
        catalog_directory,
        join_directory,
        gap_directory,
        prototype_directory,
        model_directory,
    ):
        directory.mkdir()

    segment = _catalog_segment()
    catalog_data = catalog_directory / "catalog.jsonl.gz"
    _write_gzip(catalog_data, _canonical(segment, newline=True))
    catalog_manifest = {
        "schema": catalog_v1.SCHEMA,
        "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
        "kind": "historical_build_catalog_manifest",
        "catalog_path": catalog_data.name,
        "causal_contract": {
            "future_info_backfill_allowed": False,
            "selection": "latest exact INFO at or before action",
        },
    }
    catalog_manifest_path = catalog_directory / "manifest.json"
    catalog_manifest_path.write_bytes(_canonical(catalog_manifest, newline=True))
    catalog_contract = {
        "schema": catalog_manifest["schema"],
        "implementation_revision": catalog_manifest["implementation_revision"],
        "kind": catalog_manifest["kind"],
        "causal_contract": catalog_manifest["causal_contract"],
    }

    prototype = _clone_prototype()
    clone_partition_binding = _clone_binding()["prototype_partition"]
    prototype["partition"] = {
        key: deepcopy(value)
        for key, value in clone_partition_binding.items()
        if key != "implementation_revision"
    }
    episode_portable_sha = "e" * 64
    frozen_cohort_sha = "d" * 64
    prototype_manifest_core = {
        "schema": prototypes_v1.MANIFEST_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "status": prototypes_v1.STATUS,
        "input_closure": {
            "frozen_cohort": {"file_sha256": frozen_cohort_sha}
        },
        "selection_contract": {"selection_fixed_before_simulator_evaluation": True},
        "prototypes": [prototype],
        "source_audit": {"fixture": True},
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
        },
    }
    prototype_manifest = _content_address(prototype_manifest_core)
    prototype_manifest_path = prototype_directory / "manifest.json"
    prototype_raw = _canonical(prototype_manifest, newline=True)
    prototype_manifest_path.write_bytes(prototype_raw)
    prototype_source = {
        "schema": prototypes_v1.MANIFEST_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "file_sha256": hashlib.sha256(prototype_raw).hexdigest(),
        "content_sha256": prototype_manifest["content_address"]["sha256"],
    }
    portable_membership_contract = {
        "schema": prototype_manifest["schema"],
        "implementation_revision": prototype_manifest["implementation_revision"],
        "status": prototype_manifest["status"],
        "selection_contract": prototype_manifest["selection_contract"],
        "prototypes": [
            {
                key: deepcopy(prototype.get(key))
                for key in (
                    "prototype_id",
                    "prototype_family",
                    "selection_rule",
                    "member_count",
                    "members",
                )
            }
        ],
        "source_audit": prototype_manifest["source_audit"],
        "scientific_boundaries": prototype_manifest["scientific_boundaries"],
        "frozen_cohort_file_sha256": frozen_cohort_sha,
        "fury_episode_portable_manifest_contract_sha256": episode_portable_sha,
    }
    prototype_portable_sha = hashlib.sha256(
        _canonical(portable_membership_contract)
    ).hexdigest()

    dictionary_rows = [] if zero_runtime_segments else [_dictionary_row(segment)]
    logical_dictionary = b"".join(
        _canonical(row, newline=True) for row in dictionary_rows
    )
    dictionary_path = join_directory / "dictionary.jsonl.gz"
    dictionary_descriptor = _write_gzip(dictionary_path, logical_dictionary)
    dictionary_descriptor["record_schema"] = join_v1.SEGMENT_DICTIONARY_SCHEMA
    join_core = {
        "schema": join_v1.MANIFEST_SCHEMA,
        "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
        "status": join_v1.STATUS,
        "portability_contract": {
            "host_absolute_input_locators_stored": False,
            "content_address_host_path_independent": True,
        },
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
        "input_closure": {
            "behavior_prototype_manifest": {
                "schema": prototypes_v1.MANIFEST_SCHEMA,
                "implementation_revision": (
                    prototypes_v1.IMPLEMENTATION_REVISION
                ),
                "portable_membership_contract_sha256": prototype_portable_sha,
                "prototype_count": 1,
            },
            "fury_episode_manifest": {
                "portable_manifest_contract_sha256": episode_portable_sha
            },
            "historical_build_catalog": {
                "schema": catalog_v1.SCHEMA,
                "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
                "portable_manifest_contract_sha256": hashlib.sha256(
                    _canonical(catalog_contract)
                ).hexdigest(),
            }
        },
        "segment_dictionary": {"partition": dictionary_descriptor},
        "statistics": {
            "prototype_member_union": {
                "distinct_joined_segment_count": len(dictionary_rows)
            }
        },
    }
    join_manifest = _content_address(join_core)
    join_manifest_path = join_directory / "manifest.json"
    join_manifest_path.write_bytes(_canonical(join_manifest, newline=True))
    join_sha = join_manifest["content_address"]["sha256"]

    clone_binding = _clone_binding()
    clone_binding["prototype_manifest"] = prototype_source
    model = clone_v2.compile_weighted_prototype_v2(
        _clone_prototype(), _clone_records(), source_binding=clone_binding
    )
    model_path = model_directory / "fixture.model.json"
    model_raw = _canonical(model, newline=True)
    model_path.write_bytes(model_raw)
    model_manifest_core = {
        "schema": clone_v2.MANIFEST_SCHEMA,
        "implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
        "status": clone_v2.STATUS,
        "input_closure": {"prototype_manifest": prototype_source},
        "models": [
            {
                "path": model_path.name,
                "prototype_id": PROTOTYPE_ID,
                "prototype_family": (
                    model["prototype_identity"]["prototype_family"]
                ),
                "schema": clone_v2.MODEL_SCHEMA,
                "content_sha256": model["content_address"]["sha256"],
                "file_sha256": hashlib.sha256(model_raw).hexdigest(),
                "file_size_bytes": len(model_raw),
            }
        ],
        "summary": {"model_count": 1},
        "scientific_boundaries": {
            "comparison_authorized": False,
            "same_equipment_matched_seed_comparison_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    model_manifest = _content_address(model_manifest_core)
    model_manifest_path = model_directory / "manifest.json"
    model_manifest_path.write_bytes(_canonical(model_manifest, newline=True))

    runtime_count = 0 if zero_runtime_segments else 1
    runtime_support = 0 if zero_runtime_segments else 2
    action_support = {} if zero_runtime_segments else {"warrior.bloodthirst": 2}
    gap_core = {
        "schema": gap_v1.SCHEMA,
        "implementation_revision": gap_v1.IMPLEMENTATION_REVISION,
        "status": gap_v1.STATUS,
        "input_closure": {
            "decision_build_join": {
                "schema": join_v1.MANIFEST_SCHEMA,
                "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
                "content_sha256": join_sha,
                "portable_content_identity_verified": True,
            },
            "segment_dictionary": {
                **{
                    key: value
                    for key, value in dictionary_descriptor.items()
                    if key != "path"
                },
                "selected_prototype_union_segment_count": len(dictionary_rows),
            },
            "historical_build_catalog": {
                "schema": catalog_v1.SCHEMA,
                "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
                "portable_manifest_contract_sha256": hashlib.sha256(
                    _canonical(catalog_contract)
                ).hexdigest(),
            },
            "network_request_count": 0,
            "simulator_run_count": 0,
        },
        "priority_contract": {},
        "summary": {
            "prototype_count": 1,
            "prototype_ids": [PROTOTYPE_ID],
            "prototype_union_distinct_segment_count": len(dictionary_rows),
            "prototype_union_weighted_decision_support": runtime_support,
            "prototype_union_action_support": action_support,
            "runtime_executable_segment_count": runtime_count,
            "runtime_executable_weighted_decision_support": runtime_support,
            "runtime_blocked_segment_count": 0,
            "runtime_blocked_weighted_decision_support": 0,
            "development_build_eligible_segment_count": runtime_count,
            "runtime_blocker_count": 0,
            "runtime_blocker_counts_by_category": {
                category: 0 for category in gap_v1.BLOCKER_CATEGORY_ORDER
            },
            "development_only_constraint_count": 0,
            "comparison_calibration_missing_mechanism_count": 0,
            "source_candidate_count": 0,
        },
        "runtime_blocker_priority": [],
        "development_only_constraints": [],
        "comparison_calibration": {
            "classification": "COMPARISON_ONLY_NOT_A_DEVELOPMENT_BLOCKER",
            "missing_mechanisms": [],
            "reason_support": [],
        },
        "coverage_ceiling": {
            "currently_runtime_executable": {
                "weighted_decision_support": runtime_support,
                "distinct_segment_count": runtime_count,
                "action_support": action_support,
            },
            "if_all_reported_runtime_blockers_resolved": {
                "weighted_decision_support": 0,
                "distinct_segment_count": 0,
                "action_coverage_upper_bound": {},
                "guaranteed_by_this_artifact": False,
            },
        },
        "source_segment_candidates": [],
        "prototype_candidate_selection": {
            PROTOTYPE_ID: {
                "available_segment_count": len(dictionary_rows),
                "requested_candidate_count": 1,
                "selection_rule": "fixture",
                "candidates": [],
            }
        },
        "scientific_boundaries": deepcopy(gap_v1.SCIENTIFIC_BOUNDARIES),
    }
    gap_manifest = gap_v1._content_addressed(gap_core)
    gap_manifest_path = gap_directory / "manifest.json"
    gap_manifest_path.write_bytes(_canonical(gap_manifest, newline=True))
    return {
        "gap_manifest_path": gap_manifest_path,
        "join_manifest_path": join_manifest_path,
        "catalog_manifest_path": catalog_manifest_path,
        "catalog_path": catalog_data,
        "prototype_manifest_path": prototype_manifest_path,
        "model_manifest_path": model_manifest_path,
    }


def _build(paths: dict[str, Path]) -> dict[str, object]:
    return source_v1.build_historical_fury_source_bound_prototype_bundle_v1(
        **paths
    )


def _absolute_strings(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            result.extend(_absolute_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_absolute_strings(child))
    elif isinstance(value, str) and source_v1._looks_absolute(value):
        result.append(value)
    return result


class HistoricalFurySourceBoundPrototypeBundleV1Tests(unittest.TestCase):
    def test_exact_source_segment_and_model_are_prepared_without_transplant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary) / "source")
            bundle = _build(paths)
            source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                bundle, **paths
            )

        self.assertEqual("PREPARED", bundle["status"])
        self.assertEqual("NOT_RUN", bundle["execution_status"])
        self.assertFalse(bundle["comparison_authorized"])
        self.assertEqual(1, len(bundle["requests"]))
        request = bundle["requests"][0]
        self.assertEqual("EXACT_SOURCE_BUILD_SEGMENT", request["source_route"])
        self.assertEqual(PROTOTYPE_ID, request["historical_policy_bindings"][0]["prototype_id"])
        self.assertFalse(request["current_character_profile_consumed"])
        self.assertFalse(request["representative_selector_consumed"])
        self.assertEqual(0, bundle["request_contract"]["current_representative_transplant_count"])
        self.assertEqual([], _absolute_strings(bundle))

    def test_bundle_identity_and_validation_survive_source_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_root = base / "first"
            first_paths = _fixture(first_root)
            first = _build(first_paths)
            second_root = base / "relocated"
            shutil.copytree(first_root, second_root)
            second_paths = {
                key: second_root / path.relative_to(first_root)
                for key, path in first_paths.items()
            }
            second = _build(second_paths)
            source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                first, **second_paths
            )

        self.assertEqual(first, second)
        self.assertEqual(
            first["content_address"]["sha256"],
            second["content_address"]["sha256"],
        )
        self.assertEqual([], _absolute_strings(second))

    def test_foreign_absolute_catalog_locator_resolves_after_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_root = base / "first"
            paths = _fixture(first_root)
            catalog_manifest = json.loads(
                paths["catalog_manifest_path"].read_text(encoding="utf-8")
            )
            catalog_manifest["catalog_path"] = (
                r"Z:\foreign\windows\origin\catalog.jsonl.gz"
            )
            paths["catalog_manifest_path"].write_bytes(
                _canonical(catalog_manifest, newline=True)
            )
            implicit_first = dict(paths)
            implicit_first.pop("catalog_path")
            first = _build(implicit_first)

            second_root = base / "relocated"
            shutil.copytree(first_root, second_root)
            implicit_second = {
                key: second_root / path.relative_to(first_root)
                for key, path in implicit_first.items()
            }
            second = _build(implicit_second)
            source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                first, **implicit_second
            )

        self.assertEqual(first, second)
        self.assertEqual([], _absolute_strings(second))

    def test_source_byte_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary) / "source")
            bundle = _build(paths)
            with gzip.open(paths["catalog_path"], "rt", encoding="utf-8") as handle:
                segment = json.loads(handle.read())
            segment["player"]["name"] = "Tampered"
            _write_gzip(paths["catalog_path"], _canonical(segment, newline=True))
            with self.assertRaisesRegex(
                source_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
                "content-addressed segment_ref",
            ):
                source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                    bundle, **paths
                )

    def test_model_identity_drift_from_bound_prototype_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary) / "source")
            manifest = json.loads(
                paths["model_manifest_path"].read_text(encoding="utf-8")
            )
            model_path = paths["model_manifest_path"].parent / manifest["models"][0][
                "path"
            ]
            model = json.loads(model_path.read_text(encoding="utf-8"))
            model["prototype_identity"]["member_count"] = 999
            model = _readdress(model)
            model_raw = _canonical(model, newline=True)
            model_path.write_bytes(model_raw)
            manifest["models"][0].update(
                {
                    "content_sha256": model["content_address"]["sha256"],
                    "file_sha256": hashlib.sha256(model_raw).hexdigest(),
                    "file_size_bytes": len(model_raw),
                }
            )
            manifest = _readdress(manifest)
            paths["model_manifest_path"].write_bytes(
                _canonical(manifest, newline=True)
            )
            with self.assertRaisesRegex(
                source_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
                "descriptor identity differs",
            ):
                _build(paths)

    def test_readdressing_cannot_enable_training_or_superiority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary) / "source")
            bundle = _build(paths)
            core = deepcopy(bundle)
            core.pop("content_address", None)
            core["training_authorized"] = True
            core["superiority_claim_authorized"] = True
            tampered = source_v1._content_addressed(core)
            with self.assertRaisesRegex(
                source_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
                "preparation boundary",
            ):
                source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                    tampered, verify_source_bytes=False
                )

    def test_zero_runtime_source_case_is_explicitly_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(
                Path(temporary) / "source", zero_runtime_segments=True
            )
            bundle = _build(paths)
            source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                bundle, **paths
            )

        self.assertEqual("BLOCKED", bundle["status"])
        self.assertEqual("NOT_RUN", bundle["execution_status"])
        self.assertEqual([], bundle["requests"])
        self.assertEqual(
            "NO_RUNTIME_EXECUTABLE_SOURCE_BOUND_SEGMENTS",
            bundle["blockers"][0]["code"],
        )
        self.assertFalse(bundle["comparison_authorized"])


if __name__ == "__main__":
    unittest.main()
