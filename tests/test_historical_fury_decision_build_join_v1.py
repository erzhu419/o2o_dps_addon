from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_fury_decision_build_join_v1 as join_v1
from o2o_dps import historical_fury_expert_episode_adapter_v1 as episode_v1


def _canonical(value: object, *, newline: bool = False) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return raw + (b"\n" if newline else b"")


def _talent(rank: int = 1) -> dict[str, object]:
    return {
        "talent_id": "warrior.bloodthirst",
        "profile_name": "bloodthirst",
        "rank": rank,
        "max_rank": 1,
        "tree_index": 1,
        "position": 16,
    }


def _segment(
    *,
    guid: str,
    segment_id: str,
    translated: bool = True,
    two_hand: bool = True,
) -> dict[str, object]:
    return {
        "schema": "historical_build_segment/v1",
        "identity": {
            "server": "Capybara",
            "realm": "Basin of Stars",
            "player_guid": guid,
            "instance_id": "instance-1",
            "build_segment_id": segment_id,
        },
        "equipment": {
            "slots": [
                {
                    "inventory_slot": 16,
                    "status": "OBSERVED_EQUIPPED",
                    "coverage": {
                        "item": {"weapon_mode": "TWO_HAND" if two_hand else "ONE_HAND"}
                    },
                },
                {
                    "inventory_slot": 17,
                    "status": "OBSERVED_EMPTY" if two_hand else "OBSERVED_EQUIPPED",
                    "coverage": {"item": {"weapon_mode": "ONE_HAND"}},
                },
            ]
        },
        "talents": {
            "translation_status": (
                "TRANSLATED_EXACT" if translated else "OBSERVED_RAW_UNTRANSLATED"
            ),
            "semantic_ranks": [_talent()] if translated else None,
        },
    }


def _request(
    *, guid: str, segment_id: str, two_hand: bool = True
) -> join_v1._RequestRoute:
    vector = [_talent()]
    weapon_mode = "TWO_HAND" if two_hand else "DUAL_WIELD"
    signature = join_v1._similarity_signature(
        talent_vector=vector, weapon_mode=weapon_mode
    )
    assert signature is not None
    return join_v1._RequestRoute(
        representative_rank=1,
        identity=(
            "Capybara",
            "Basin of Stars",
            guid.casefold(),
            "instance-1",
            segment_id,
        ),
        request_sha256="a" * 64,
        similarity_signature_sha256=signature,
        weapon_mode=weapon_mode,
    )


class HistoricalFuryDecisionBuildJoinV1Tests(unittest.TestCase):
    def test_portable_episode_binding_and_output_partition_survive_relocation(self) -> None:
        cohort_sha = "c" * 64

        def write_episode_manifest(directory: Path, locator: str) -> Path:
            core = {
                "schema": episode_v1.MANIFEST_SCHEMA,
                "implementation_revision": episode_v1.IMPLEMENTATION_REVISION,
                "status": episode_v1.STATUS,
                "input_closure": {
                    "frozen_cohort": {
                        "schema": "historical_fury_expert_cohort/v2",
                        "file_sha256": cohort_sha,
                        "path": locator,
                    },
                    "external_v2_timeline": {"path": locator},
                },
                "join_contract": {"identity": ["exact"]},
                "action_contract": {"policy_label": "recognized START only"},
                "unresolved_observations": [],
                "partitions": [],
                "summary": {},
                "scientific_boundaries": {
                    "comparison_authorized": False,
                    "training_authorized": False,
                    "closed_loop_baseline_authorized": False,
                    "superiority_claim_authorized": False,
                },
            }
            address = hashlib.sha256(_canonical(core)).hexdigest()
            manifest = {
                **core,
                "content_address": {
                    "algorithm": "sha256",
                    "scope": "canonical JSON excluding content_address",
                    "sha256": address,
                },
            }
            payload = _canonical(manifest, newline=True)
            stable = directory / "manifest.json"
            stable.write_bytes(payload)
            (directory / f"episodes.{address}.manifest.json").write_bytes(payload)
            return stable

        with tempfile.TemporaryDirectory(prefix="join_relocate_") as raw:
            root = Path(raw)
            first = root / "windows-copy"
            second = root / "linux-copy"
            first.mkdir()
            second.mkdir()
            first_manifest = write_episode_manifest(
                first, r"D:\\data\\cohort.json"
            )
            second_manifest = write_episode_manifest(
                second, "/srv/o2o/offline_data/cohort.json"
            )

            _, first_binding = join_v1._load_episode_manifest(
                first_manifest, cohort_sha256=cohort_sha
            )
            _, second_binding = join_v1._load_episode_manifest(
                second_manifest, cohort_sha256=cohort_sha
            )
            self.assertEqual(first_binding, second_binding)
            self.assertNotIn("path", json.dumps(first_binding))

            descriptors = []
            for directory in (first, second):
                writer = join_v1._GzipJsonlWriter(
                    directory, "mapping", join_v1.MAPPING_SCHEMA
                )
                writer.write({"schema": join_v1.MAPPING_SCHEMA, "value": 1})
                descriptors.append(writer.finish())
            self.assertEqual(descriptors[0], descriptors[1])
            first_address = join_v1._content_addressed(
                {"input": first_binding, "partition": descriptors[0]}
            )["content_address"]["sha256"]
            second_address = join_v1._content_addressed(
                {"input": second_binding, "partition": descriptors[1]}
            )["content_address"]["sha256"]
            self.assertEqual(first_address, second_address)

    def test_routes_distinguish_exact_similar_transplant_and_unknown(self) -> None:
        request = _request(guid="0x0000000000000001", segment_id="segment-0001")

        exact, _ = join_v1.request_routes_for_segment_v1(
            _segment(guid="0x0000000000000001", segment_id="segment-0001"),
            [request],
        )
        similar, _ = join_v1.request_routes_for_segment_v1(
            _segment(guid="0x0000000000000002", segment_id="segment-0002"),
            [request],
        )
        transplant, _ = join_v1.request_routes_for_segment_v1(
            _segment(
                guid="0x0000000000000002",
                segment_id="segment-0002",
                two_hand=False,
            ),
            [request],
        )
        unknown, _ = join_v1.request_routes_for_segment_v1(
            _segment(
                guid="0x0000000000000002",
                segment_id="segment-0002",
                translated=False,
            ),
            [request],
        )

        self.assertEqual(join_v1.ROUTE_EXACT, exact[0]["route"])
        self.assertEqual(join_v1.ROUTE_SIMILAR, similar[0]["route"])
        self.assertEqual(join_v1.ROUTE_TRANSPLANT, transplant[0]["route"])
        self.assertEqual(join_v1.ROUTE_UNKNOWN_BUILD, unknown[0]["route"])

    def test_binding_runs_preserve_build_changes_and_missing_prefix(self) -> None:
        bindings = [
            {"decision_ordinal": 0, "join_status": join_v1.MISSING_PREFIX, "segment_ref": None},
            {"decision_ordinal": 1, "join_status": join_v1.JOINED, "segment_ref": "a"},
            {"decision_ordinal": 2, "join_status": join_v1.JOINED, "segment_ref": "a"},
            {"decision_ordinal": 3, "join_status": join_v1.JOINED, "segment_ref": "b"},
            {"decision_ordinal": 4, "join_status": join_v1.JOINED, "segment_ref": "a"},
        ]

        runs, changes = join_v1._binding_runs(bindings)

        self.assertEqual(4, len(runs))
        self.assertEqual(2, runs[1]["decision_count"])
        self.assertEqual(2, changes)
        self.assertIsNone(runs[0]["segment_ref"])
        self.assertEqual("b", runs[2]["segment_ref"])

        across_unknown, changes = join_v1._binding_runs(
            [
                {"decision_ordinal": 0, "join_status": join_v1.JOINED, "segment_ref": "a"},
                {"decision_ordinal": 1, "join_status": join_v1.MISSING_PREFIX, "segment_ref": None},
                {"decision_ordinal": 2, "join_status": join_v1.JOINED, "segment_ref": "b"},
            ]
        )
        self.assertEqual(3, len(across_unknown))
        self.assertEqual(0, changes)

    def test_route_report_keeps_zero_exact_visible(self) -> None:
        self.assertEqual(
            {
                join_v1.ROUTE_EXACT: 0,
                join_v1.ROUTE_SIMILAR: 2,
                join_v1.ROUTE_TRANSPLANT: 0,
                join_v1.ROUTE_UNKNOWN_BUILD: 0,
                join_v1.ROUTE_UNKNOWN_PREFIX: 0,
            },
            join_v1._route_counter_wire(
                join_v1.Counter({join_v1.ROUTE_SIMILAR: 2})
            ),
        )

    def test_verified_episode_partition_rejects_declared_logical_drift(self) -> None:
        episode = {
            "schema": episode_v1.SCHEMA,
            "implementation_revision": episode_v1.IMPLEMENTATION_REVISION,
            "status": episode_v1.STATUS,
            "instance_id": "instance-1",
        }
        logical = _canonical(episode, newline=True)
        with tempfile.TemporaryDirectory(prefix="decision_build_join_") as raw:
            directory = Path(raw)
            partition = directory / "episode.jsonl.gz"
            with partition.open("wb") as output:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=output, mtime=0
                ) as compressed:
                    compressed.write(logical)
            descriptor = {
                "instance_id": "instance-1",
                "path": partition.name,
                "record_schema": episode_v1.SCHEMA,
                "record_count": 1,
                "compressed_size_bytes": partition.stat().st_size,
                "compressed_file_sha256": hashlib.sha256(
                    partition.read_bytes()
                ).hexdigest(),
                "logical_size_bytes": len(logical) + 1,
                "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
            }

            with self.assertRaisesRegex(
                join_v1.HistoricalFuryDecisionBuildJoinV1Error,
                "logical_size_bytes differs",
            ):
                list(join_v1._verified_episode_rows(directory / "manifest.json", descriptor))


if __name__ == "__main__":
    unittest.main()
