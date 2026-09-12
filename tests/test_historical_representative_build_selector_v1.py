from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.historical_build_catalog_v1 import compile_instance_segments
from o2o_dps.historical_representative_build_selector_v1 import (
    CATALOG_SCHEMA,
    HistoricalRepresentativeBuildSelectorError,
    _Candidate,
    _select_representatives,
    select_historical_representative_builds,
)
from tests.test_historical_build_catalog_v1 import GUID, _coverage, _instance, _record


def _valid_segment() -> dict[str, object]:
    rows = compile_instance_segments(
        _instance(
            (
                _record(
                    timestamp_ms=1_100,
                    event_index=10,
                    ordinal=0,
                    item_id=100,
                    representative=True,
                ),
            )
        ),
        coverage_registry=_coverage(),
    )
    return deepcopy(
        next(row for row in rows if row["identity"]["player_guid"] == GUID)
    )


def _source_variant(
    source: dict[str, object],
    *,
    guid: str,
    instance_id: str,
    segment_id: str,
    timestamp_ms: int,
    first_slot_item_id: int | None = None,
) -> dict[str, object]:
    row = deepcopy(source)
    row["identity"]["player_guid"] = guid
    row["identity"]["instance_id"] = instance_id
    row["identity"]["build_segment_id"] = segment_id
    row["observation"]["valid_from"]["timestamp_ms"] = timestamp_ms
    row["observation"]["valid_from"]["event_index"] = timestamp_ms
    if first_slot_item_id is not None:
        slot = row["equipment"]["slots"][0]
        slot["item_id"] = first_slot_item_id
    return row


def _write_catalog(
    directory: Path, rows: list[dict[str, object]], *, declared_eligible: int
) -> Path:
    catalog_path = directory / "catalog.jsonl.gz"
    with gzip.open(catalog_path, mode="wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "schema": CATALOG_SCHEMA,
        "implementation_revision": "fixture-v1",
        "created_at": "2026-09-12T00:00:00Z",
        "catalog_path": str(catalog_path),
        "inputs": {"coverage_registry_identity": {"schema": "fixture/v1"}},
        "summary": {
            "by_hero_class": {
                "WARRIOR": {
                    "development_build_segment_count": declared_eligible,
                }
            }
        },
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


def _read_jsonl(path: str | Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _distance_candidate(
    key: str, slot_items: tuple[int, ...], mass: Fraction
) -> _Candidate:
    slots = [
        {"inventory_slot": index + 1, "item_id": item_id}
        for index, item_id in enumerate(slot_items)
    ]
    return _Candidate(
        canonical_key=key,
        features={
            "weapon_mode": "TWO_HAND",
            "combat_equipment_slot_vector": slots,
            "semantic_talent_rank_vector": [],
            "mechanics_coverage_vector": [],
            "mechanics_versions": {"client_build": "7272"},
        },
        exemplar={},
        exemplar_line_number=1,
        player_mass=mass,
    )


class HistoricalRepresentativeBuildSelectorV1Tests(unittest.TestCase):
    def test_no_eligible_builds_emit_exact_blocker_and_remove_stale_output(self) -> None:
        row = _valid_segment()
        row["coverage"]["development_build_eligible"] = False
        row["coverage"]["development"] = {
            "eligible": False,
            "reasons": ["SIMULATOR_REPRESENTATION_NOT_RUNNABLE"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = _write_catalog(root, [row], declared_eligible=0)
            output = root / "selection"
            output.mkdir()
            stale = output / "representatives.jsonl"
            stale.write_text('{"stale":true}\n', encoding="utf-8")

            result = select_historical_representative_builds(
                catalog_manifest=manifest,
                output_directory=output,
            )

            self.assertEqual(result["status"], "BLOCKED_NO_ELIGIBLE_BUILDS")
            self.assertEqual(result["blockers"], ["BLOCKED_NO_ELIGIBLE_BUILDS"])
            self.assertEqual(result["summary"]["selected_representative_count"], 0)
            self.assertEqual(result["outputs"]["representatives_path"], None)
            self.assertFalse(stale.exists())
            persisted = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "BLOCKED_NO_ELIGIBLE_BUILDS")

    def test_one_exact_segment_is_emitted_without_synthetic_build(self) -> None:
        row = _valid_segment()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = select_historical_representative_builds(
                catalog_manifest=_write_catalog(root, [row], declared_eligible=1),
                output_directory=root / "selection",
            )
            representatives = _read_jsonl(result["outputs"]["representatives_path"])

        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(representatives), 1)
        representative = representatives[0]
        self.assertEqual(
            representative["selection_reason"], "GLOBAL_PLAYER_WEIGHTED_MEDOID"
        )
        self.assertEqual(representative["historical_segment"], row)
        self.assertTrue(
            result["selection_contract"]["representatives_are_exact_catalog_segments"]
        )
        self.assertFalse(
            result["selection_contract"]["synthetic_or_averaged_builds_allowed"]
        )

    def test_repeated_segments_do_not_increase_player_mass(self) -> None:
        base = _valid_segment()
        rows = [
            _source_variant(
                base,
                guid="player-a",
                instance_id=f"instance-a-{index}",
                segment_id=f"segment-a-{index}",
                timestamp_ms=1_100 + index,
            )
            for index in range(3)
        ]
        rows.append(
            _source_variant(
                base,
                guid="player-b",
                instance_id="instance-b",
                segment_id="segment-b",
                timestamp_ms=2_000,
                first_slot_item_id=101,
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = select_historical_representative_builds(
                catalog_manifest=_write_catalog(root, rows, declared_eligible=4),
                output_directory=root / "selection",
                max_representatives=2,
            )
            representatives = _read_jsonl(result["outputs"]["representatives_path"])

        self.assertEqual(result["summary"]["unique_eligible_player_count"], 2)
        self.assertEqual(
            result["summary"]["total_exact_player_mass"],
            {"numerator": 2, "denominator": 1, "value": 2.0},
        )
        self.assertEqual(
            sorted(row["player_mass"]["value"] for row in representatives),
            [1.0, 1.0],
        )
        self.assertEqual(
            sorted(
                row["source_segment_count_not_used_as_weight"]
                for row in representatives
            ),
            [1, 3],
        )

    def test_one_players_mass_is_split_over_distinct_exact_builds(self) -> None:
        base = _valid_segment()
        rows = [
            _source_variant(
                base,
                guid="player-a",
                instance_id="instance-a-1",
                segment_id="segment-a-1",
                timestamp_ms=1_100,
            ),
            _source_variant(
                base,
                guid="player-a",
                instance_id="instance-a-2",
                segment_id="segment-a-2",
                timestamp_ms=1_200,
                first_slot_item_id=101,
            ),
            _source_variant(
                base,
                guid="player-b",
                instance_id="instance-b",
                segment_id="segment-b",
                timestamp_ms=2_000,
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = select_historical_representative_builds(
                catalog_manifest=_write_catalog(root, rows, declared_eligible=3),
                output_directory=root / "selection",
                max_representatives=2,
            )
            representatives = _read_jsonl(result["outputs"]["representatives_path"])

        self.assertEqual(
            sorted(row["player_mass"]["value"] for row in representatives),
            [0.5, 1.5],
        )
        self.assertEqual(
            result["summary"]["total_exact_player_mass"],
            {"numerator": 2, "denominator": 1, "value": 2.0},
        )

    def test_first_representative_is_weighted_medoid_not_highest_mass_point(self) -> None:
        # A has the greatest individual mass, but B is between A and C and has
        # the smaller population-weighted total distance.
        candidates = {
            "a": _distance_candidate("a", (0, 0), Fraction(3, 2)),
            "b": _distance_candidate("b", (1, 0), Fraction(1, 1)),
            "c": _distance_candidate("c", (1, 1), Fraction(1, 1)),
        }
        selected, assignment = _select_representatives(
            candidates, max_representatives=1
        )
        self.assertEqual([row.canonical_key for row in selected], ["b"])
        self.assertGreater(
            assignment["weighted_mean_nearest_distance"]["value"], 0.0
        )

    def test_invalid_flagged_segment_blocks_instead_of_selecting_partial_population(self) -> None:
        valid = _valid_segment()
        invalid = _source_variant(
            valid,
            guid="player-b",
            instance_id="instance-b",
            segment_id="segment-b",
            timestamp_ms=2_000,
            first_slot_item_id=101,
        )
        invalid["talents"]["translation_status"] = "OBSERVED_RAW_UNTRANSLATED"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = select_historical_representative_builds(
                catalog_manifest=_write_catalog(
                    root, [valid, invalid], declared_eligible=2
                ),
                output_directory=root / "selection",
            )

        self.assertEqual(result["status"], "BLOCKED_INVALID_ELIGIBLE_SEGMENTS")
        self.assertEqual(
            result["summary"]["feature_valid_development_segment_count"], 1
        )
        self.assertEqual(result["summary"]["selected_representative_count"], 0)
        self.assertTrue(result["summary"]["selector_feature_exclusion_counts"])

    def test_declared_development_count_must_match_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = _write_catalog(root, [_valid_segment()], declared_eligible=0)
            with self.assertRaisesRegex(
                HistoricalRepresentativeBuildSelectorError,
                "development count differs",
            ):
                select_historical_representative_builds(
                    catalog_manifest=manifest,
                    output_directory=root / "selection",
                )


if __name__ == "__main__":
    unittest.main()
