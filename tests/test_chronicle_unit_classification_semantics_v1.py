from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from o2o_dps.chronicle_encounter_reconstruction_v1 import (
    iter_reconstructed_encounters,
)
from o2o_dps.chronicle_unit_classification_semantics_v1 import (
    AFFILIATION_LABELS,
    ChronicleClassificationSemanticsError,
    OFFICIAL_COMMIT,
    OFFICIAL_EVIDENCE_SHA256,
    ROW_FIELD,
    UNIT_TYPE_LABELS,
    enrich_unit_classification_row,
    official_evidence_contract,
    project_row_for_reconstruction_smoke,
    resolve_affiliation,
    resolve_unit_classification,
    resolve_unit_type,
    summarize_classification_rows,
)


PLAYER = "0x00000000000000A1"
MOB = "0xF130000001000001"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def class_row(
    index: int,
    unit_type: int,
    affiliation: int,
    *,
    target: str = MOB,
    owner: str | None = None,
    controller: str | None = None,
    target_player: dict[str, object] | None = None,
    owner_player: dict[str, object] | None = None,
) -> dict[str, object]:
    offset_ms = index * 10
    return {
        "schema": "chronicle_external_core_event/v1",
        "instance": "instance-1",
        "encounter": "encounter-1",
        "event_index": index,
        "offset_ms": offset_ms,
        "type": "CLASS",
        "source": None,
        "source_guid": None,
        "target": None,
        "target_guid": target,
        "spell": None,
        "spell_id": 0,
        "value": None,
        "outcome": (
            f"unit_type={unit_type} affiliation={affiliation} spell_id=0"
        ),
        "flags": [],
        "official": {
            "stream_type": "unit_classification",
            "message_sha256": f"message-{index}",
            "message": {
                "meta": {
                    "event_index": index,
                    "offset_ms": offset_ms,
                    "is_synthetic": False,
                    "activity": [],
                },
                "target": target,
                "unit_type": unit_type,
                "affiliation": affiliation,
                "owner": owner,
                "controller": controller,
                "spell_id": 0,
            },
        },
        "external_admission": {
            "classification": {
                "official_unit_type_numeric": unit_type,
                "official_affiliation_numeric": affiliation,
                "semantic": "UNKNOWN_NONVOTING",
            },
            "target_player": target_player,
            "owner": {
                "official_value": owner,
                "resolved_player": owner_player,
            },
        },
    }


def event_row(
    index: int,
    event_type: str,
    *,
    source_guid: str | None,
    target_guid: str | None,
    value: int | None = None,
) -> dict[str, object]:
    return {
        "instance": "instance-1",
        "encounter": "encounter-1",
        "event_index": index,
        "offset_ms": index * 100,
        "type": event_type,
        "source": None,
        "source_guid": source_guid,
        "target": None,
        "target_guid": target_guid,
        "spell": None,
        "spell_id": None,
        "value": value,
        "outcome": None,
        "flags": [],
        "official": {
            "stream_type": "damage" if event_type == "DMG" else "slain",
            "message_sha256": f"message-{index}",
        },
    }


class ChronicleUnitClassificationSemanticsV1Tests(unittest.TestCase):
    def test_every_official_ordinal_has_the_exact_pinned_label(self) -> None:
        expected_types = {
            0: "UNKNOWN",
            1: "PLAYER",
            2: "CREATURE",
            3: "OBJECT",
            4: "VEHICLE",
        }
        expected_affiliations = {
            0: "UNKNOWN",
            1: "FRIENDLY",
            2: "HOSTILE",
            3: "NEUTRAL",
        }
        self.assertEqual(dict(UNIT_TYPE_LABELS), expected_types)
        self.assertEqual(dict(AFFILIATION_LABELS), expected_affiliations)
        for number, label in expected_types.items():
            self.assertEqual(resolve_unit_type(number)["label"], label)
        for number, label in expected_affiliations.items():
            self.assertEqual(resolve_affiliation(number)["label"], label)

    def test_zero_and_out_of_range_values_remain_nonvoting(self) -> None:
        zero = resolve_unit_classification(0, 0)
        self.assertEqual(zero["pair_label"], "UNKNOWN_UNKNOWN")
        self.assertEqual(zero["status"], "UNKNOWN_NONVOTING")
        conflict = resolve_unit_classification(8, 16)
        self.assertIsNone(conflict["pair_label"])
        self.assertEqual(
            conflict["status"], "SCHEMA_CONVENTION_CONFLICT_NONVOTING"
        )
        self.assertEqual(conflict["unit_type"]["numeric"], 8)
        self.assertFalse(conflict["numeric_values_treated_as_bitmask"])

    def test_non_integer_and_bool_are_not_coerced(self) -> None:
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ChronicleClassificationSemanticsError):
                    resolve_unit_type(value)

    def test_hostile_metadata_player_is_preserved_as_hostile_player(self) -> None:
        resolved = resolve_unit_classification(1, 2)
        self.assertEqual(resolved["pair_label"], "HOSTILE_PLAYER")
        self.assertEqual(resolved["status"], "OFFICIAL_ENUM_PAIR")
        self.assertFalse(resolved["official_friendly_player"])
        self.assertFalse(resolved["official_hostile_creature"])

        row = class_row(
            4,
            1,
            2,
            target=PLAYER,
            target_player={"guid": PLAYER, "name": "Possessed"},
        )
        enriched = enrich_unit_classification_row(row)
        self.assertEqual(
            enriched[ROW_FIELD]["resolution"]["pair_label"], "HOSTILE_PLAYER"
        )
        self.assertFalse(
            enriched[ROW_FIELD]["temporal_contract"][
                "metadata_roster_overrode_affiliation"
            ]
        )

    def test_enrichment_preserves_all_original_fields_and_eventmeta_order(self) -> None:
        row = class_row(
            7,
            2,
            2,
            owner="00000000000000A1",
            controller=PLAYER,
        )
        before = deepcopy(row)
        enriched = enrich_unit_classification_row(row)
        self.assertEqual(row, before)
        for key, value in before.items():
            self.assertEqual(enriched[key], value)
        self.assertEqual(enriched["event_index"], 7)
        self.assertEqual(enriched["offset_ms"], 70)
        self.assertEqual(
            enriched[ROW_FIELD]["resolution"]["pair_label"], "HOSTILE_CREATURE"
        )
        self.assertEqual(
            enriched[ROW_FIELD]["wire_values"]["controller"], PLAYER
        )

    def test_conflicting_admission_or_eventmeta_is_rejected(self) -> None:
        admission_conflict = class_row(1, 2, 2)
        admission_conflict["external_admission"]["classification"][
            "official_affiliation_numeric"
        ] = 1
        with self.assertRaisesRegex(
            ChronicleClassificationSemanticsError, "conflicts"
        ):
            enrich_unit_classification_row(admission_conflict)

        meta_conflict = class_row(2, 2, 2)
        meta_conflict["official"]["message"]["meta"]["event_index"] = 3
        with self.assertRaisesRegex(
            ChronicleClassificationSemanticsError, "EventMeta"
        ):
            enrich_unit_classification_row(meta_conflict)

    def test_evidence_contract_is_pinned_and_content_addressed(self) -> None:
        contract = official_evidence_contract()
        self.assertEqual(contract["official_commit"], OFFICIAL_COMMIT)
        self.assertGreaterEqual(len(contract["official_sources"]), 8)
        self.assertTrue(
            all(OFFICIAL_COMMIT in source["url"] for source in contract["official_sources"])
        )
        content_address = contract.pop("content_address")
        self.assertEqual(
            hashlib.sha256(_canonical_bytes(contract)).hexdigest(),
            content_address["sha256"],
        )
        self.assertEqual(content_address["sha256"], OFFICIAL_EVIDENCE_SHA256)
        boundaries = contract["semantic_boundaries"]
        self.assertFalse(
            boundaries["neutral_reachable_from_audited_pinned_production_emitters"]
        )
        self.assertFalse(boundaries["creature_vs_pet_resolvable_from_unit_type"])

    def test_smoke_projection_unlocks_only_supported_official_pairs(self) -> None:
        hostile = class_row(1, 2, 2)
        projected = project_row_for_reconstruction_smoke(hostile)
        self.assertEqual(projected["outcome"], "Hostile Creature")
        self.assertEqual(hostile["outcome"], "unit_type=2 affiliation=2 spell_id=0")
        self.assertTrue(projected[ROW_FIELD]["smoke_projection"]["projected"])

        hostile_player = project_row_for_reconstruction_smoke(
            class_row(2, 1, 2, target=PLAYER)
        )
        self.assertEqual(
            hostile_player["outcome"], "unit_type=1 affiliation=2 spell_id=0"
        )
        self.assertFalse(
            hostile_player[ROW_FIELD]["smoke_projection"]["projected"]
        )

    def test_read_only_projection_reconstructs_wave_without_double_counting_slain(self) -> None:
        rows = [
            class_row(
                0,
                1,
                1,
                target=PLAYER,
                target_player={"guid": PLAYER, "name": "Warrior"},
            ),
            class_row(1, 2, 2, target=MOB),
            event_row(
                2,
                "DMG",
                source_guid=PLAYER,
                target_guid=MOB,
                value=100,
            ),
            event_row(
                3,
                "DEAD",
                source_guid=PLAYER,
                target_guid=MOB,
                value=None,
            ),
        ]
        projected = [project_row_for_reconstruction_smoke(row) for row in rows]
        encounter = list(iter_reconstructed_encounters(projected))[0]
        self.assertEqual(encounter["wave_count"], 1)
        target = encounter["waves"][0]["targets"][0]
        self.assertEqual(target["observed_incoming_damage_sum"]["value"], 100)
        self.assertEqual(target["observed_incoming_damage_sum"]["event_count"], 1)
        self.assertEqual(target["kill_budget_proxy"]["value"], 100)

    def test_summary_counts_temporal_changes_and_is_deterministic(self) -> None:
        owner_player = {"guid": PLAYER, "name": "Owner"}
        rows = [
            enrich_unit_classification_row(
                class_row(
                    0,
                    1,
                    1,
                    target=PLAYER,
                    target_player={"guid": PLAYER},
                )
            ),
            enrich_unit_classification_row(
                class_row(
                    1,
                    1,
                    2,
                    target=PLAYER,
                    target_player={"guid": PLAYER},
                )
            ),
            enrich_unit_classification_row(
                class_row(
                    2,
                    2,
                    1,
                    owner="00000000000000A1",
                    owner_player=owner_player,
                )
            ),
        ]
        first = summarize_classification_rows(rows)
        second = summarize_classification_rows(rows)
        self.assertEqual(first, second)
        self.assertEqual(first["classification_event_count"], 3)
        self.assertEqual(first["unique_player_target_count"], 1)
        self.assertEqual(first["exact_metadata_player_hostile_event_count"], 1)
        self.assertEqual(first["temporal_pair_transition_count"], 1)
        self.assertEqual(first["owner_event_count"], 1)
        self.assertEqual(first["owner_exact_metadata_player_resolution_count"], 1)


if __name__ == "__main__":
    unittest.main()
