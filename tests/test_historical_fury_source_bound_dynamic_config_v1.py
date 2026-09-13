from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_team_timeline_v2 as timeline_v2
from o2o_dps import historical_fury_decision_build_join_v1 as join_v1
from o2o_dps import historical_fury_source_bound_dynamic_config_v1 as dynamic_v1
from o2o_dps import historical_fury_source_bound_prototype_bundle_v1 as source_v1


def _canonical(value: object, *, newline: bool = False) -> bytes:
    result = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return result + (b"\n" if newline else b"")


def _write_gzip(path: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    logical = b"".join(_canonical(row, newline=True) for row in rows)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(logical)
    compressed = path.read_bytes()
    return {
        "path": path.name,
        "record_schema": join_v1.MAPPING_SCHEMA,
        "instance_id": "instance-0",
        "record_count": len(rows),
        "controllable_start_count": sum(
            len(row["decision_bindings"]) for row in rows
        ),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
        "compressed_size_bytes": len(compressed),
        "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
        "gzip_mtime": 0,
    }


def _request(index: int) -> tuple[dict[str, object], dict[str, object]]:
    segment_ref = f"sha256:{index + 1:064x}"
    stats = [0] * 46
    stats[17] = 320
    stats[26] = 1104
    request = {
        "raid": {"parties": [{"players": [{"name": f"warrior-{index}"}]}]},
        "encounter": {
            "duration": 20.001,
            "targets": [{"name": f"duration-target-{index}", "stats": stats}],
        },
        "simOptions": {"iterations": 1, "randomSeed": "11"},
    }
    row = {
        "segment_ref": segment_ref,
        "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "causal_source_identity": {
            "identity": {
                "server": "Capybara",
                "realm": "Basin of Stars",
                "player_guid": "0x0000000000000001",
                "instance_id": "instance-0",
                "build_segment_id": f"segment-{index}",
            },
            "valid_from": {
                "timestamp_ms": 100 + index,
                "encounter_id": f"catalog-valid-from-{index}",
                "event_index": index,
                "message_ordinal": index,
            },
        },
        "composition": {"request": request},
    }
    mapping = {
        "schema": join_v1.MAPPING_SCHEMA,
        "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
        "record_type": "exact_fury_wave_decision_build_mapping",
        "source": {
            "episode_id": f"episode-{index}",
            "instance_id": "instance-0",
            "encounter_id": f"decision-encounter-{index}",
            "player_guid": "0x0000000000000001",
            "server": "Capybara",
            "realm": "Basin of Stars",
            "wave_id": f"decision-encounter-{index}:external-v2-wave:1",
            "wave_ordinal": 1,
            "source_wave_content_sha256": f"{index + 101:064x}",
        },
        "decision_bindings": [
            {
                "action_key": "warrior.bloodthirst",
                "decision_ordinal": index,
                "event_index": 1000 + index,
                "join_status": "JOINED_EXACT_CAUSAL_PREFIX",
                "order_key": [10000 + index, 1000 + index, 3, index],
                "segment_ref": segment_ref,
                "timestamp_ms": 10000 + index,
            }
        ],
    }
    return row, mapping


def _fixture(root: Path, *, ambiguous_first: bool = False) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    request_rows: list[dict[str, object]] = []
    mapping_rows: list[dict[str, object]] = []
    for index in range(dynamic_v1.EXPECTED_REQUEST_COUNT):
        request, mapping = _request(index)
        request_rows.append(request)
        mapping_rows.append(mapping)
    if ambiguous_first:
        second = deepcopy(mapping_rows[0])
        second["source"]["episode_id"] = "episode-0-second"
        second["source"]["encounter_id"] = "decision-encounter-0-second"
        second["source"]["wave_id"] = (
            "decision-encounter-0-second:external-v2-wave:1"
        )
        second["source"]["source_wave_content_sha256"] = "f" * 64
        mapping_rows.append(second)

    partition_path = root / "mapping.jsonl.gz"
    descriptor = _write_gzip(partition_path, mapping_rows)
    join = join_v1._content_addressed(
        {
            "schema": join_v1.MANIFEST_SCHEMA,
            "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
            "status": join_v1.STATUS,
            "mapping_partitions": [descriptor],
        }
    )
    join_path = root / "join.json"
    join_path.write_bytes(_canonical(join, newline=True))

    seeds = [11, 12]
    seed_list_sha = hashlib.sha256(_canonical(seeds)).hexdigest()
    bundle = source_v1._content_addressed(
        {
            "schema": source_v1.SCHEMA,
            "implementation_revision": source_v1.IMPLEMENTATION_REVISION,
            "kind": source_v1.KIND,
            "status": "PREPARED",
            "execution_status": "NOT_RUN",
            "input_closure": {
                "decision_build_join": {
                    "schema": join_v1.MANIFEST_SCHEMA,
                    "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
                    "content_sha256": join["content_address"]["sha256"],
                }
            },
            "development_seeds": {
                "count": len(seeds),
                "master_seeds": seeds,
                "seed_list_sha256": seed_list_sha,
            },
            "requests": request_rows,
        }
    )
    bundle_path = root / "bundle.json"
    bundle_path.write_bytes(_canonical(bundle, newline=True))

    timeline = join_v1._content_addressed(
        {
            "schema": timeline_v2.SCHEMA,
            "implementation_revision": timeline_v2.IMPLEMENTATION_REVISION,
            "status": timeline_v2.STATUS,
            "summary": {"wave_count": len(mapping_rows)},
        }
    )
    timeline_path = root / "timeline.json"
    timeline_path.write_bytes(_canonical(timeline, newline=True))
    return {
        "source_bundle_path": bundle_path,
        "decision_build_join_path": join_path,
        "team_timeline_path": timeline_path,
    }


class HistoricalFurySourceBoundDynamicConfigV1Tests(unittest.TestCase):
    def test_exact_decision_wave_is_not_catalog_valid_from_encounter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary))
            artifact = dynamic_v1.build_historical_fury_source_bound_dynamic_config_v1(
                **paths
            )

        self.assertEqual("BLOCKED", artifact["status"])
        self.assertEqual(9, artifact["summary"]["exact_unique_decision_wave_count"])
        self.assertEqual(0, artifact["summary"]["ready_request_count"])
        self.assertEqual(9, artifact["summary"]["blocked_request_count"])
        first = artifact["requests"][0]
        self.assertEqual(
            "catalog-valid-from-0", first["catalog_valid_from"]["encounter_id"]
        )
        self.assertEqual(
            "decision-encounter-0", first["source_identity"]["encounter_id"]
        )
        self.assertFalse(
            artifact["binding_contract"][
                "catalog_valid_from_encounter_is_wave_selector"
            ]
        )
        self.assertFalse(first["base_request_dynamic_gate"]["health_mode_declared"])
        self.assertEqual(
            0,
            first["base_request_dynamic_gate"]["target_health_stats"][0]["value"],
        )
        self.assertFalse(
            first["evidence"]["exogenous_armor"][
                "candidate_endogenous_sunder_encoded_as_environment"
            ]
        )
        self.assertFalse(
            artifact["runtime_capability"]["dynamic_probe_scenario_values_consumed"]
        )

    def test_zero_ready_rows_fail_closed_and_publish_null_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = _fixture(root / "inputs")
            _, _, artifact = (
                dynamic_v1.publish_historical_fury_source_bound_dynamic_config_v1(
                    output_directory=root / "output", **paths
                )
            )
            loaded = dynamic_v1.load_historical_fury_source_bound_dynamic_config_v1(
                root / "output"
            )

        self.assertEqual(artifact, loaded)
        required = dynamic_v1._REQUIRED_EVIDENCE_BLOCKERS
        for row in loaded["requests"]:
            self.assertIsNone(row["derived_request_template"])
            self.assertIsNone(row["dynamic_load_config"])
            self.assertIsNone(row["derived_pair_content_sha256"])
            self.assertTrue(
                required.issubset({blocker["code"] for blocker in row["blockers"]})
            )
        selected = loaded["requests"][0]
        with self.assertRaisesRegex(
            dynamic_v1.HistoricalFurySourceBoundDynamicConfigV1Error,
            "source-bound dynamic config is BLOCKED",
        ):
            dynamic_v1.select_ready_dynamic_config_v1(
                loaded,
                segment_ref=selected["segment_ref"],
                request_sha256=selected["base_request_sha256"],
            )

    def test_incompatible_waves_block_without_selecting_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = _fixture(Path(temporary), ambiguous_first=True)
            artifact = dynamic_v1.build_historical_fury_source_bound_dynamic_config_v1(
                **paths
            )

        first = artifact["requests"][0]
        self.assertIsNone(first["source_identity"])
        self.assertEqual(2, first["decision_prefix"]["mapping_cardinality"])
        self.assertIn(
            "MULTIPLE_INCOMPATIBLE_DECISION_WAVE_MAPPINGS",
            {blocker["code"] for blocker in first["blockers"]},
        )
        self.assertEqual(8, artifact["summary"]["exact_unique_decision_wave_count"])
        self.assertEqual(0, artifact["summary"]["ready_request_count"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
