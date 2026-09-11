from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_character_dps_index_v1 as dps_v1
from o2o_dps import chronicle_external_character_history_v1 as history_v1


INSTANCE_A = "11111111-1111-1111-1111-111111111111"
INSTANCE_B = "22222222-2222-2222-2222-222222222222"
INSTANCE_C = "33333333-3333-3333-3333-333333333333"
RAW_ONLY_CLEAN = "44444444-4444-4444-4444-444444444444"
REQUIRED_STREAMS = ["damage", "spell_fail", "spell_go", "spell_start"]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_content_addressed(
    directory: Path,
    prefix: str,
    core: dict[str, object],
    *,
    addressor=union_v1._content_addressed,
) -> tuple[dict[str, object], Path]:
    document = addressor(core)
    digest = document["content_address"]["sha256"]
    path = directory / f"{prefix}.{digest}.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(document))
    return document, path


def _rewrite_content_addressed(
    document: dict[str, object],
    directory: Path,
    prefix: str,
    *,
    addressor=union_v1._content_addressed,
) -> tuple[dict[str, object], Path]:
    core = {
        key: deepcopy(value)
        for key, value in document.items()
        if key != "content_address"
    }
    return _write_content_addressed(directory, prefix, core, addressor=addressor)


def _raw_row(
    instance_id: str,
    *,
    guild: str,
    started_at: str,
    slug: str | None = None,
    stream_status: str = "AVAILABLE",
) -> dict[str, object]:
    label = ingest_v1.classify_range_bug(guild, started_at)
    streams: dict[str, object] = {}
    for index, stream in enumerate(REQUIRED_STREAMS):
        digest = f"{index + 1:064x}"
        streams[stream] = {
            "status": stream_status,
            "object": {
                "relative_path": f"objects/sha256/{digest[:2]}/{digest}.{stream}.gz",
                "sha256": digest,
                "size_bytes": 1,
                "media_type": "application/octet-stream",
            }
            if stream_status == "AVAILABLE"
            else None,
        }
    return {
        "instance_id": instance_id,
        "slug": slug or f"slug-{instance_id[0]}",
        "instance_name": "Upper Tower of Karazhan",
        "uploaded_at": "2026-09-11T00:00:00Z",
        "started_at": started_at,
        "contamination_guild_context": guild,
        "instance_contamination_label": label,
        "ranking_records": None,
        "streams": streams,
    }


def _write_raw_manifest(
    data_root: Path,
    rows: list[dict[str, object]],
    *,
    revision: str = ingest_v1.IMPLEMENTATION_REVISION,
    canonical: bool = True,
) -> Path:
    manifest = {
        "schema": ingest_v1.SCHEMA,
        "implementation_revision": revision,
        "parser_contract_revision": ingest_v1.PARSER_CONTRACT_REVISION,
        "kind": "chronicle_external_api_raw_snapshot",
        "api_base": ingest_v1.EXTERNAL_API_BASE,
        "cursor_contract": deepcopy(union_v1._CURRENT_CURSOR_CONTRACT),
        "contamination_contract": ingest_v1._contamination_contract(),
        "request": {"fixture": True},
        "recent_pages": [],
        "leaderboard_snapshots": [],
        "instances": rows,
    }
    payload = _canonical(manifest)
    if not canonical:
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    digest = _sha(payload)
    path = (
        data_root
        / "chronicle_raw"
        / "external_api"
        / "v1"
        / "manifests"
        / f"{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _ranking(record_id: str) -> dict[str, object]:
    return {
        "ranking_record_id": record_id,
        "encounter_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "encounter_name": "King",
        "player_spec": "Fury",
        "player_role": "dps",
        "killed_at": "2026-09-10T13:00:00Z",
        "damage_done": 120000,
        "duration_secs": 100,
        "dps": 1200,
    }


def _membership(
    raw: dict[str, object],
    *,
    guid: str,
    training: bool,
    exact: bool = True,
) -> dict[str, object]:
    record = _ranking(f"{guid[-2:].lower():0>8}-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    if exact:
        ranking = {
            "status": dps_v1.EXACT_AVAILABLE,
            "identity_match": "EXACT_PLAYER_GUID_ONLY",
            "record_count": 1,
            "records": [record],
            "exact_dps_available": True,
            "usable_for_exact_dps_training": training,
            "raid_contamination_training_eligible": training,
            "ranking_endpoint_captured": True,
            "ranking_endpoint_capture_count": 1,
            "expected_name_exact_match_record_count": 1,
            "expected_name_match_guids": [guid],
            "ranking_fetch_required": False,
            "negative_evidence_contract": "fixture",
        }
    else:
        ranking = {
            "status": dps_v1.CAPTURED_GUID_ABSENT,
            "identity_match": "EXACT_PLAYER_GUID_ONLY",
            "record_count": 0,
            "records": [],
            "exact_dps_available": False,
            "usable_for_exact_dps_training": False,
            "raid_contamination_training_eligible": training,
            "ranking_endpoint_captured": True,
            "ranking_endpoint_capture_count": 1,
            "expected_name_exact_match_record_count": 0,
            "expected_name_match_guids": [],
            "ranking_fetch_required": False,
            "negative_evidence_contract": "fixture",
        }
    character_name = f"战士{guid[-1]}"
    return {
        "character_guid": guid,
        "character_name": character_name,
        "instance_id": raw["instance_id"],
        "name": raw["instance_name"],
        "slug": raw["slug"],
        "started_at": raw["started_at"],
        "uploaded_at": raw["uploaded_at"],
        "guild": {"id": "guild-1", "name": raw["contamination_guild_context"]},
        "contamination_label": raw["instance_contamination_label"],
        "performance": [],
        "history_version_audit": {
            "versions": [
                {
                    "selected": True,
                    "query": {
                        "server": "Capybara",
                        "realm": "Basin of Stars",
                        "character": character_name,
                    },
                }
            ]
        },
        "coverage": {
            "required_streams": {
                "status": "COMPLETE",
                "required": list(REQUIRED_STREAMS),
                "available": list(REQUIRED_STREAMS),
                "fetch_required": [],
                "missing": [],
                "uncaptured": [],
            },
            "action_events": {
                "status": "COMPLETE",
                "required": list(history_v1.ACTION_STREAMS),
                "available": list(history_v1.ACTION_STREAMS),
                "fetch_required": [],
                "missing": [],
                "uncaptured": [],
            },
            "ranking_exact_dps": ranking,
        },
        "missing_plan": {
            "training_candidate": training,
            "training_fetch_planned": False,
            "required_streams": [],
            "required_streams_fetch_required": [],
        },
    }


class UnionFixture:
    def __init__(self, base: Path, *, overlap: bool = False, conflict: bool = False):
        self.data_root = base / "offline_data"
        self.a = _raw_row(
            INSTANCE_A,
            guild="其他",
            started_at="2026-09-10T12:00:00Z",
        )
        self.b = _raw_row(
            INSTANCE_B,
            guild="南北",
            started_at="2026-09-03T13:00:00+08:00",
        )
        self.c = _raw_row(
            INSTANCE_C,
            guild="南北",
            started_at="2026-09-02T13:00:00+08:00",
        )
        self.raw_only = _raw_row(
            RAW_ONLY_CLEAN,
            guild="其他",
            started_at="2026-09-10T12:00:00Z",
        )
        first_rows = [self.a, self.c]
        second_rows = [self.b, self.raw_only]
        if overlap:
            duplicate = deepcopy(self.a)
            if conflict:
                duplicate["slug"] = "conflicting-slug"
            second_rows.append(duplicate)
        self.raw_paths = [
            _write_raw_manifest(self.data_root, first_rows),
            _write_raw_manifest(self.data_root, second_rows),
        ]
        raw_shas = sorted(path.stem for path in self.raw_paths)

        inventory_core: dict[str, object] = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "kind": history_v1.INVENTORY_KIND,
            "inventory_request": {
                "required_streams": list(REQUIRED_STREAMS),
            },
            "source_bindings": {
                "character_history_manifest_sha256": ["a" * 64],
                "ingest_manifest_sha256": raw_shas,
            },
            "instances": [
                _membership(
                    self.a,
                    guid="0x00000000000000A1",
                    training=True,
                    exact=True,
                ),
                _membership(
                    self.b,
                    guid="0x00000000000000B2",
                    training=True,
                    exact=False,
                ),
                _membership(
                    self.c,
                    guid="0x00000000000000C3",
                    training=False,
                    exact=True,
                ),
            ],
        }
        inventory_dir = (
            self.data_root / Path(history_v1.INVENTORY_MANIFEST_DIRECTORY)
        )
        self.inventory, self.inventory_path = _write_content_addressed(
            inventory_dir,
            history_v1.INVENTORY_MANIFEST_PREFIX,
            inventory_core,
            addressor=history_v1._content_addressed,
        )
        inventory_binding = {
            "schema": history_v1.INVENTORY_SCHEMA,
            "implementation_revision": history_v1.INVENTORY_IMPLEMENTATION_REVISION,
            "content_sha256": self.inventory["content_address"]["sha256"],
            "file_sha256": _sha(self.inventory_path.read_bytes()),
            "size_bytes": self.inventory_path.stat().st_size,
            "path": self.inventory_path.relative_to(self.data_root).as_posix(),
        }
        dps_core: dict[str, object] = {
            "schema": dps_v1.SCHEMA,
            "implementation_revision": dps_v1.IMPLEMENTATION_REVISION,
            "kind": dps_v1.KIND,
            "source_inventory": inventory_binding,
            "partition": {
                "logical_content_sha256": "1" * 64,
                "logical_size_bytes": 100,
                "compressed_file_sha256": "2" * 64,
                "compressed_size_bytes": 50,
                "record_count": 2,
            },
            "summary": {"training_eligible_exact_membership_count": 1},
        }
        dps_dir = (
            self.data_root
            / dps_v1.OUTPUT_DIRECTORY
            / dps_v1.MANIFEST_DIRECTORY
        )
        self.dps, self.dps_path = _write_content_addressed(
            dps_dir, dps_v1.MANIFEST_PREFIX, dps_core
        )

    def patches(self):
        def replay(path: str | Path, *, data_root: Path):
            resolved = Path(path).resolve()
            digest = resolved.stem
            return {
                "status": "ALREADY_CURRENT_LOCAL_RAW",
                "source_manifest_sha256": digest,
                "manifest_sha256": digest,
                "network_requests_made": 0,
                "watermark_mutated": False,
            }

        return (
            mock.patch.object(
                union_v1.ingest_v1,
                "replay_manifest_from_local_raw",
                side_effect=replay,
            ),
            mock.patch.object(
                union_v1.history_v1,
                "load_character_instance_inventory_manifest",
                return_value=(deepcopy(self.inventory), self.inventory_path.resolve()),
            ),
            mock.patch.object(
                union_v1.dps_v1,
                "load_character_dps_index_manifest",
                return_value=(deepcopy(self.dps), self.dps_path.resolve()),
            ),
        )

    def publish(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "source_manifest_paths": self.raw_paths,
            "inventory_manifest_path": self.inventory_path,
            "dps_index_manifest_path": self.dps_path,
            "scope_name": "fixture_scope",
            "population": "full-descriptive-with-inventory-training-mask",
            "data_root": self.data_root,
            "expect_union_count": 4,
            "expect_training_count": 2,
            "expect_nontraining_instance_ids": [RAW_ONLY_CLEAN],
        }
        arguments.update(overrides)
        patches = self.patches()
        with patches[0], patches[1], patches[2]:
            return union_v1.publish_manifest_union(**arguments)


class ChronicleExternalApiManifestUnionTests(unittest.TestCase):
    def test_publish_is_deterministic_manifest_last_and_inventory_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            first = fixture.publish()
            second = fixture.publish(
                source_manifest_paths=list(reversed(fixture.raw_paths))
            )
            self.assertEqual(first, second)
            self.assertEqual("PASS_OFFLINE_UNION_AND_COHORT_BOUND", first["status"])
            self.assertEqual(0, first["network_requests_made"])
            self.assertFalse(first["training_or_comparison_authorized"])
            self.assertEqual(4, first["descriptive_instance_count"])
            self.assertEqual(2, first["training_instance_count"])
            self.assertEqual(2, first["descriptive_nontraining_instance_count"])

            union_path = Path(str(first["raw_union_manifest_path"]))
            receipt_path = Path(str(first["receipt_manifest_path"]))
            raw_union = json.loads(union_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(_canonical(raw_union), union_path.read_bytes())
            self.assertEqual(union_path.stem, _sha(union_path.read_bytes()))
            self.assertEqual(_canonical(receipt), receipt_path.read_bytes())
            self.assertEqual(
                receipt["content_address"]["sha256"],
                first["receipt_content_sha256"],
            )
            self.assertEqual(
                [INSTANCE_A, INSTANCE_B, INSTANCE_C, RAW_ONLY_CLEAN],
                receipt["cohorts"]["descriptive"]["instance_ids"],
            )
            self.assertEqual(
                [INSTANCE_A, INSTANCE_B],
                receipt["cohorts"]["training"]["instance_ids"],
            )
            self.assertEqual(
                [INSTANCE_C, RAW_ONLY_CLEAN],
                receipt["cohorts"]["descriptive_nontraining"]["instance_ids"],
            )
            reasons = {
                row["instance_id"]: row["reason"]
                for row in receipt["cohorts"]["descriptive_nontraining"]["reasons"]
            }
            self.assertEqual(
                "ABSENT_FROM_BOUND_INVENTORY_DEFAULT_DESCRIPTIVE_NONTRAINING",
                reasons[RAW_ONLY_CLEAN],
            )
            self.assertEqual(
                "BOUND_INVENTORY_EXPLICITLY_NONTRAINING", reasons[INSTANCE_C]
            )
            for cohort in receipt["cohorts"].values():
                self.assertEqual(
                    _sha(_canonical(cohort["instance_ids"])),
                    cohort["instance_ids_sha256"],
                )
            evidence = receipt["cohorts"]["training"]["evidence_counts"]
            self.assertEqual(2, evidence["candidate_membership_count"])
            self.assertEqual(1, evidence["exact_training_membership_count"])
            self.assertEqual(
                1, evidence["captured_exact_GUID_absent_training_membership_count"]
            )
            self.assertEqual(1, evidence["selected_exact_DPS_row_count"])
            self.assertEqual(
                fixture.inventory["content_address"]["sha256"],
                receipt["source_dps_index"]["source_inventory_content_sha256"],
            )
            self.assertTrue(receipt["publication_contract"]["receipt_published_last"])
            self.assertFalse(
                receipt["scientific_status"]["training_or_comparison_authorized"]
            )
            stable_pointer = receipt_path.parent.parent / "manifest.json"
            self.assertFalse(stable_pointer.exists())

    def test_identical_duplicate_instance_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary), overlap=True)
            result = fixture.publish()
            self.assertEqual(1, result["duplicate_instance_row_count"])
            self.assertEqual(5, result["source_instance_row_count"])
            self.assertEqual(4, result["descriptive_instance_count"])

    def test_conflicting_duplicate_instance_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary), overlap=True, conflict=True)
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError,
                "conflicting complete rows",
            ):
                fixture.publish()
            self.assertFalse(
                (fixture.data_root / Path(union_v1.OUTPUT_DIRECTORY)).exists()
            )

    def test_raw_only_clean_label_remains_nontraining_and_assertion_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            self.assertEqual(
                ingest_v1.NO_KNOWN_RULE_MATCH,
                fixture.raw_only["instance_contamination_label"],
            )
            result = fixture.publish()
            receipt = json.loads(
                Path(str(result["receipt_manifest_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [RAW_ONLY_CLEAN],
                receipt["assertions"]["expected_nontraining_instance_ids"],
            )

    def test_assertion_mismatch_writes_neither_union_nor_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            before = set(
                (fixture.data_root / "chronicle_raw" / "external_api" / "v1" / "manifests").glob("*.json")
            )
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError,
                "count assertion failed",
            ):
                fixture.publish(expect_union_count=5)
            after = set(
                (fixture.data_root / "chronicle_raw" / "external_api" / "v1" / "manifests").glob("*.json")
            )
            self.assertEqual(before, after)
            self.assertFalse(
                (fixture.data_root / Path(union_v1.OUTPUT_DIRECTORY)).exists()
            )

    def test_expected_nontraining_id_cannot_be_absent_or_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "absent from the raw union"
            ):
                fixture.publish(expect_nontraining_instance_ids=["missing-instance"])
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "entered the training cohort"
            ):
                fixture.publish(expect_nontraining_instance_ids=[INSTANCE_A])

    def test_noncanonical_and_legacy_raw_manifests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            noncanonical = _write_raw_manifest(
                fixture.data_root, [fixture.a], canonical=False
            )
            legacy = _write_raw_manifest(
                fixture.data_root,
                [fixture.a],
                revision=ingest_v1.LEGACY_IMPLEMENTATION_REVISION,
            )
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "not canonical"
            ):
                fixture.publish(source_manifest_paths=[noncanonical, fixture.raw_paths[1]])
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "not the current"
            ):
                fixture.publish(source_manifest_paths=[legacy, fixture.raw_paths[1]])

    def test_filename_hash_mismatch_and_source_repetition_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            wrong = fixture.raw_paths[0].with_name(f"{'f' * 64}.json")
            wrong.write_bytes(fixture.raw_paths[0].read_bytes())
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError,
                "filename/content SHA-256 mismatch",
            ):
                fixture.publish(source_manifest_paths=[wrong, fixture.raw_paths[1]])
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError,
                "unique content addresses",
            ):
                fixture.publish(
                    source_manifest_paths=[fixture.raw_paths[0], fixture.raw_paths[0]]
                )

    def test_source_local_replay_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            with mock.patch.object(
                union_v1.ingest_v1,
                "replay_manifest_from_local_raw",
                side_effect=ingest_v1.ChronicleIngestError("object hash changed"),
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "local replay failed",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )

    def test_inventory_must_bind_every_raw_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            poisoned = deepcopy(fixture.inventory)
            poisoned["source_bindings"]["ingest_manifest_sha256"] = [
                fixture.raw_paths[0].stem
            ]
            poisoned, poisoned_path = _rewrite_content_addressed(
                poisoned,
                fixture.inventory_path.parent,
                history_v1.INVENTORY_MANIFEST_PREFIX,
                addressor=history_v1._content_addressed,
            )
            patches = fixture.patches()
            with (
                patches[0],
                mock.patch.object(
                    union_v1.history_v1,
                    "load_character_instance_inventory_manifest",
                    return_value=(poisoned, poisoned_path.resolve()),
                ),
                patches[2],
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "does not include every raw union source",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )

    def test_inventory_training_ids_must_all_exist_in_union(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            poisoned = deepcopy(fixture.inventory)
            extra_raw = _raw_row(
                "55555555-5555-5555-5555-555555555555",
                guild="其他",
                started_at="2026-09-10T12:00:00Z",
            )
            poisoned["instances"].append(
                _membership(
                    extra_raw,
                    guid="0x00000000000000D4",
                    training=True,
                )
            )
            poisoned, poisoned_path = _rewrite_content_addressed(
                poisoned,
                fixture.inventory_path.parent,
                history_v1.INVENTORY_MANIFEST_PREFIX,
                addressor=history_v1._content_addressed,
            )
            patches = fixture.patches()
            with (
                patches[0],
                mock.patch.object(
                    union_v1.history_v1,
                    "load_character_instance_inventory_manifest",
                    return_value=(poisoned, poisoned_path.resolve()),
                ),
                patches[2],
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "not a subset of the raw union",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )

    def test_training_identity_and_stream_coverage_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            identity = deepcopy(fixture.inventory)
            identity["instances"][0]["slug"] = "wrong"
            stream = deepcopy(fixture.inventory)
            stream["instances"][0]["coverage"]["action_events"]["status"] = "PARTIAL"
            for poisoned, pattern in (
                (identity, "raw/inventory identity mismatch"),
                (stream, "lacks complete stream/action evidence"),
            ):
                poisoned, poisoned_path = _rewrite_content_addressed(
                    poisoned,
                    fixture.inventory_path.parent,
                    history_v1.INVENTORY_MANIFEST_PREFIX,
                    addressor=history_v1._content_addressed,
                )
                patches = fixture.patches()
                with (
                    patches[0],
                    mock.patch.object(
                        union_v1.history_v1,
                        "load_character_instance_inventory_manifest",
                        return_value=(poisoned, poisoned_path.resolve()),
                    ),
                    patches[2],
                ):
                    with self.assertRaisesRegex(
                        union_v1.ChronicleExternalManifestUnionError, pattern
                    ):
                        union_v1.publish_manifest_union(
                            fixture.raw_paths,
                            fixture.inventory_path,
                            fixture.dps_path,
                            scope_name="fixture",
                            population="fixture",
                            data_root=fixture.data_root,
                        )

    def test_dps_index_must_bind_the_exact_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            poisoned = deepcopy(fixture.dps)
            poisoned["source_inventory"]["content_sha256"] = "f" * 64
            poisoned, poisoned_path = _rewrite_content_addressed(
                poisoned,
                fixture.dps_path.parent,
                dps_v1.MANIFEST_PREFIX,
            )
            patches = fixture.patches()
            with (
                patches[0],
                patches[1],
                mock.patch.object(
                    union_v1.dps_v1,
                    "load_character_dps_index_manifest",
                    return_value=(poisoned, poisoned_path.resolve()),
                ),
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "does not bind the exact supplied inventory",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )

    def test_failed_union_fixed_point_replay_never_publishes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            source_shas = {path.stem for path in fixture.raw_paths}

            def replay(path: str | Path, *, data_root: Path):
                digest = Path(path).stem
                if digest not in source_shas:
                    raise ingest_v1.ChronicleIngestError("combined evidence mismatch")
                return {
                    "status": "ALREADY_CURRENT_LOCAL_RAW",
                    "source_manifest_sha256": digest,
                    "manifest_sha256": digest,
                    "network_requests_made": 0,
                    "watermark_mutated": False,
                }

            with (
                mock.patch.object(
                    union_v1.ingest_v1,
                    "replay_manifest_from_local_raw",
                    side_effect=replay,
                ),
                mock.patch.object(
                    union_v1.history_v1,
                    "load_character_instance_inventory_manifest",
                    return_value=(fixture.inventory, fixture.inventory_path.resolve()),
                ),
                mock.patch.object(
                    union_v1.dps_v1,
                    "load_character_dps_index_manifest",
                    return_value=(fixture.dps, fixture.dps_path.resolve()),
                ),
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "raw union local replay failed",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )
            manifests = list(
                (fixture.data_root / "chronicle_raw" / "external_api" / "v1" / "manifests").glob("*.json")
            )
            self.assertEqual(3, len(manifests))
            self.assertTrue(set(fixture.raw_paths).issubset(set(manifests)))
            self.assertFalse(
                (fixture.data_root / Path(union_v1.OUTPUT_DIRECTORY)).exists()
            )

    def test_publication_oserror_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            patches = fixture.patches()
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    union_v1.ingest_v1,
                    "_atomic_write_bytes",
                    side_effect=OSError("disk unavailable"),
                ),
            ):
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "raw union local replay failed: disk unavailable",
                ):
                    union_v1.publish_manifest_union(
                        fixture.raw_paths,
                        fixture.inventory_path,
                        fixture.dps_path,
                        scope_name="fixture",
                        population="fixture",
                        data_root=fixture.data_root,
                    )

    def test_cli_matches_frozen_interface_and_emits_canonical_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            output = io.BytesIO()
            arguments = [
                "--source-manifest",
                str(fixture.raw_paths[0]),
                "--source-manifest",
                str(fixture.raw_paths[1]),
                "--inventory-manifest",
                str(fixture.inventory_path),
                "--dps-index-manifest",
                str(fixture.dps_path),
                "--scope-name",
                "fixture_scope",
                "--population",
                "full-descriptive-with-inventory-training-mask",
                "--data-root",
                str(fixture.data_root),
                "--expect-union-count",
                "4",
                "--expect-training-count",
                "2",
                "--expect-nontraining-instance-id",
                RAW_ONLY_CLEAN,
            ]
            patches = fixture.patches()
            with patches[0], patches[1], patches[2]:
                self.assertEqual(0, union_v1.main(arguments, stdout_buffer=output))
            result = json.loads(output.getvalue().decode("utf-8"))
            self.assertEqual(_canonical(result), output.getvalue())
            self.assertEqual("PASS_OFFLINE_UNION_AND_COHORT_BOUND", result["status"])

    def test_audit_replays_receipt_union_sources_inventory_dps_and_cohorts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            published = fixture.publish()
            patches = fixture.patches()
            with patches[0], patches[1], patches[2]:
                audited = union_v1.audit_manifest_union_receipt(
                    published["receipt_manifest_path"], data_root=fixture.data_root
                )
            self.assertEqual("PASS_STRICT_FULL_SOURCE_REPLAY", audited["status"])
            self.assertEqual(
                published["receipt_content_sha256"],
                audited["receipt_content_sha256"],
            )
            self.assertEqual(4, audited["descriptive_instance_count"])
            self.assertEqual(2, audited["training_instance_count"])
            self.assertEqual(0, audited["network_requests_made"])
            self.assertFalse(audited["training_or_comparison_authorized"])

    def test_audit_rejects_resealed_receipt_with_poisoned_cohort_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            published = fixture.publish()
            receipt_path = Path(str(published["receipt_manifest_path"]))
            poisoned = json.loads(receipt_path.read_text(encoding="utf-8"))
            poisoned["cohorts"]["training"]["instance_ids_sha256"] = "f" * 64
            poisoned, poisoned_path = _rewrite_content_addressed(
                poisoned, receipt_path.parent, union_v1.MANIFEST_PREFIX
            )
            patches = fixture.patches()
            with patches[0], patches[1], patches[2]:
                with self.assertRaisesRegex(
                    union_v1.ChronicleExternalManifestUnionError,
                    "deterministic full-source replay",
                ):
                    union_v1.audit_manifest_union_receipt(
                        poisoned_path, data_root=fixture.data_root
                    )

    def test_audit_rejects_changed_raw_source_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            published = fixture.publish()
            source = fixture.raw_paths[0]
            original = source.read_bytes()
            source.write_bytes(original + b" ")
            try:
                patches = fixture.patches()
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(
                        union_v1.ChronicleExternalManifestUnionError,
                        "filename/content SHA-256 mismatch",
                    ):
                        union_v1.audit_manifest_union_receipt(
                            published["receipt_manifest_path"],
                            data_root=fixture.data_root,
                        )
            finally:
                source.write_bytes(original)

    def test_audit_cli_is_mutually_exclusive_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            published = fixture.publish()
            output = io.BytesIO()
            patches = fixture.patches()
            with patches[0], patches[1], patches[2]:
                self.assertEqual(
                    0,
                    union_v1.main(
                        [
                            "--audit-receipt",
                            str(published["receipt_manifest_path"]),
                            "--data-root",
                            str(fixture.data_root),
                        ],
                        stdout_buffer=output,
                    ),
                )
            audited = json.loads(output.getvalue().decode("utf-8"))
            self.assertEqual(_canonical(audited), output.getvalue())
            self.assertEqual("PASS_STRICT_FULL_SOURCE_REPLAY", audited["status"])
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "cannot be combined"
            ):
                union_v1.main(
                    [
                        "--audit-receipt",
                        str(published["receipt_manifest_path"]),
                        "--source-manifest",
                        str(fixture.raw_paths[0]),
                    ]
                )

    def test_requires_two_sources_and_an_offline_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = UnionFixture(Path(temporary))
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "at least two"
            ):
                fixture.publish(source_manifest_paths=[fixture.raw_paths[0]])
            with self.assertRaisesRegex(
                union_v1.ChronicleExternalManifestUnionError, "offline_data"
            ):
                fixture.publish(data_root=Path(temporary) / "wrong-root")


if __name__ == "__main__":
    unittest.main()
