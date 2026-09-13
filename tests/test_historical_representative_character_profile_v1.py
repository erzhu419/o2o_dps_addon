from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.historical_build_catalog_v1 import (
    IMPLEMENTATION_REVISION as CATALOG_IMPLEMENTATION_REVISION,
)
from o2o_dps.historical_representative_build_selector_v1 import (
    select_historical_representative_builds,
)
from o2o_dps.historical_representative_character_profile_v1 import (
    SELECTION_EVENT,
    SELECTION_MODE,
    HistoricalRepresentativeCharacterProfileV1Error,
    historical_representative_to_character_profile,
    load_historical_representative_character_profile,
)
from o2o_dps.wowsims_profile import build_wowsims_profile
from tests.test_historical_representative_build_selector_v1 import (
    _read_jsonl,
    _valid_segment,
    _write_catalog,
)
def _verified_selection(
    root: Path, segment: dict[str, object]
) -> tuple[Path, dict[str, object], Path, Path]:
    catalog_manifest_path = _write_catalog(
        root, [segment], declared_eligible=1
    )
    catalog_manifest = json.loads(
        catalog_manifest_path.read_text(encoding="utf-8")
    )
    catalog_manifest["implementation_revision"] = CATALOG_IMPLEMENTATION_REVISION
    catalog_manifest["kind"] = "historical_build_catalog_manifest"
    catalog_manifest["summary"]["build_segment_count"] = 1
    catalog_manifest_path.write_text(
        json.dumps(catalog_manifest, ensure_ascii=False), encoding="utf-8"
    )
    selection = select_historical_representative_builds(
        catalog_manifest=catalog_manifest_path,
        output_directory=root / "selection",
    )
    selector_manifest_path = Path(selection["manifest_path"])
    representatives_path = Path(selection["outputs"]["representatives_path"])
    representative = _read_jsonl(representatives_path)[0]
    return (
        selector_manifest_path,
        representative,
        Path(catalog_manifest["catalog_path"]),
        representatives_path,
    )


def _write_representative(path: Path, representative: dict[str, object]) -> None:
    path.write_text(
        json.dumps(representative, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_catalog_row(path: Path, segment: dict[str, object]) -> None:
    with gzip.open(path, mode="wt", encoding="utf-8") as handle:
        handle.write(json.dumps(segment, ensure_ascii=False) + "\n")


class HistoricalRepresentativeCharacterProfileV1Tests(unittest.TestCase):
    def test_exact_representative_preserves_identity_build_and_ravager(self) -> None:
        segment = _valid_segment()
        segment["equipment"]["slots"][15]["random_suffix"] = 13
        segment["talents"]["semantic_ranks"].append(
            {
                "talent_id": "turtle.warrior.ravager",
                "profile_name": "Ravager",
                "max_rank": 3,
                "rank": 2,
                "tree_index": 1,
                "position": 11,
                "tab": 2,
                "index": 12,
                "tier": 5,
                "column": 1,
                "definition_status": "KNOWN",
                "effect_status": "IMPLEMENTED",
                "calibrated_scopes": ["fixture"],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, _, _ = _verified_selection(
                Path(temporary), segment
            )
            profile = load_historical_representative_character_profile(
                selector,
                rank=1,
                consumes={"defaultPotion": "OtherActionPotion"},
                database={"items": []},
            )

        self.assertEqual(profile.selection.selection_mode, SELECTION_MODE)
        self.assertEqual(profile.selection.record["event"], SELECTION_EVENT)
        self.assertNotEqual(profile.selection.record["event"], "STATIC_PROFILE_CAPTURED")
        identity = profile.selection.record["state"]["characterIdentity"]
        self.assertEqual(
            identity,
            {
                "name": "Fury",
                "level": 60,
                "raceName": "Orc",
                "className": "WARRIOR",
            },
        )
        main_hand = next(
            row
            for row in profile.selection.record["state"]["equipment"]
            if row["slot"] == 16
        )
        self.assertEqual(main_hand["link"], "item:100:10:13")
        self.assertEqual(profile.slot_statuses[16], "OBSERVED_EQUIPPED")
        self.assertEqual(profile.slot_statuses[17], "OBSERVED_EMPTY")
        self.assertEqual(profile.item_effect_coverage[16]["item_id"], 100)
        self.assertEqual(
            profile.item_effect_coverage[16]["permanent_enchant_status"],
            "IMPLEMENTED",
        )
        talents = profile.selection.record["state"]["talents"]
        ravager = next(row for row in talents if row["name"] == "Ravager")
        self.assertEqual(ravager["rank"], 2)
        self.assertEqual(ravager["maxRank"], 3)
        request, _ = build_wowsims_profile(
            {"raid": {"parties": [{"players": [{"warrior": {"options": {}}}]}]}},
            profile.selection,
        )
        self.assertEqual(
            request["raid"]["parties"][0]["players"][0]["warrior"]["options"][
                "ravagerRank"
            ],
            2,
        )
        self.assertEqual(profile.provenance["profile_kind"], "HISTORICAL_EXACT_BUILD")
        self.assertTrue(profile.provenance["historical_exact_build"])
        self.assertTrue(profile.provenance["development_eligible"])
        self.assertTrue(profile.provenance["runtime_executable"])
        self.assertFalse(profile.provenance["comparison_eligible"])
        self.assertEqual(profile.provenance["source_identity"], segment["identity"])
        self.assertEqual(
            profile.provenance["source_exact_features"],
            representative["exact_features"],
        )
        binding = profile.provenance["verified_selector_binding"]
        self.assertEqual(
            binding["status"],
            "VERIFIED_READY_SELECTOR_AND_EXACT_CATALOGUE_LINE",
        )
        self.assertTrue(binding["selector_row_match"])
        self.assertTrue(binding["catalog_line_match"])

    def test_supplied_row_must_equal_selector_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, _, _ = _verified_selection(
                Path(temporary), _valid_segment()
            )
            verified = historical_representative_to_character_profile(
                representative,
                selector_manifest_path=selector,
                consumes={},
                database={},
            )
            self.assertEqual(verified.selection.record["event"], SELECTION_EVENT)

            changed = deepcopy(representative)
            changed["selection_reason"] = "NOT_THE_SELECTED_ROW"
            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "differs from the exact selected artifact row",
            ):
                historical_representative_to_character_profile(
                    changed,
                    selector_manifest_path=selector,
                    consumes={},
                    database={},
                )

    def test_nonexistent_catalog_cannot_emit_exact_selected_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, _, catalog_path, _ = _verified_selection(
                Path(temporary), _valid_segment()
            )
            catalog_path.unlink()

            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "catalog_path does not exist",
            ):
                load_historical_representative_character_profile(
                    selector, rank=1, consumes={}, database={}
                )

    def test_catalog_line_must_equal_embedded_selected_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, _, catalog_path, _ = _verified_selection(
                Path(temporary), _valid_segment()
            )
            changed = _valid_segment()
            changed["identity"]["build_segment_id"] = "different-catalog-row"
            _write_catalog_row(catalog_path, changed)

            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "differs from its exact catalogue line",
            ):
                load_historical_representative_character_profile(
                    selector, rank=1, consumes={}, database={}
                )

    def test_loader_can_select_by_exact_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, _, _ = _verified_selection(
                Path(temporary), _valid_segment()
            )
            profile = load_historical_representative_character_profile(
                selector,
                source_identity=representative["provenance"]["source_identity"],
                consumes={},
                database={},
            )
        self.assertEqual(
            profile.provenance["source_identity"],
            representative["provenance"]["source_identity"],
        )

    def test_source_identity_must_equal_embedded_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, _, representatives_path = _verified_selection(
                Path(temporary), _valid_segment()
            )
            representative["provenance"]["source_identity"]["player_guid"] = "other"
            _write_representative(representatives_path, representative)

            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "source_identity differs",
            ):
                load_historical_representative_character_profile(
                    selector, rank=1, consumes={}, database={}
                )

    def test_declared_features_must_equal_recomputed_exact_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, _, representatives_path = _verified_selection(
                Path(temporary), _valid_segment()
            )
            representative["exact_features"]["weapon_mode"] = "DUAL_WIELD"
            _write_representative(representatives_path, representative)

            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "exact_features differ",
            ):
                load_historical_representative_character_profile(
                    selector, rank=1, consumes={}, database={}
                )

    def test_development_and_runtime_gates_are_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector, representative, catalog_path, representatives_path = (
                _verified_selection(Path(temporary), _valid_segment())
            )
            representative["historical_segment"]["coverage"][
                "development_build_eligible"
            ] = False
            _write_representative(representatives_path, representative)
            _write_catalog_row(catalog_path, representative["historical_segment"])

            with self.assertRaisesRegex(
                HistoricalRepresentativeCharacterProfileV1Error,
                "no longer selector-eligible",
            ):
                load_historical_representative_character_profile(
                    selector, rank=1, consumes={}, database={}
                )

    def test_nonempty_gem_and_temporary_enchant_are_rejected_not_dropped(self) -> None:
        mutations = (
            (
                "gem",
                lambda slot: (
                    slot.__setitem__("gem_enchant_ids", [77]),
                    slot["coverage"].__setitem__(
                        "gems",
                        [
                            {
                                "id": 77,
                                "definition_status": "KNOWN",
                                "effect_status": "IMPLEMENTED",
                                "calibrated_scopes": ["fixture"],
                            }
                        ],
                    ),
                ),
                "cannot encode historical gem",
            ),
            (
                "temporary enchant",
                lambda slot: (
                    slot.__setitem__("temporary_enchant_id", 11),
                    slot["coverage"]["enchants"].__setitem__(
                        "temporary",
                        {
                            "id": 11,
                            "definition_status": "KNOWN",
                            "effect_status": "IMPLEMENTED",
                            "calibrated_scopes": ["fixture"],
                        },
                    ),
                ),
                "cannot encode historical temporary enchants",
            ),
        )
        for label, mutate, error_text in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                segment = _valid_segment()
                mutate(segment["equipment"]["slots"][15])
                selector, _, _, _ = _verified_selection(Path(temporary), segment)
                with self.assertRaisesRegex(
                    HistoricalRepresentativeCharacterProfileV1Error, error_text
                ):
                    load_historical_representative_character_profile(
                        selector, rank=1, consumes={}, database={}
                    )


if __name__ == "__main__":
    unittest.main()
