from __future__ import annotations

import unittest

from o2o_dps.chronicle_external_aura_attribution_v1 import (
    STATUS,
    ChronicleExternalAuraAttributionError,
    attribute_state_rows,
)


def _row(
    stream: str,
    *,
    timestamp_ms: int,
    event_index: int,
    spell: str = "Sunder Armor",
    spell_id: int = 11597,
    source_guid: str | None = None,
    target_guid: str = "0xTARGET",
    state: str = "StateAdded",
    amount: int = 1,
    is_buff: bool = False,
    encounter: str = "encounter-1",
    message_index: int | None = None,
) -> dict[str, object]:
    if message_index is None:
        message_index = event_index
    payload: dict[str, object]
    if stream == "aura":
        payload = {
            "target": target_guid,
            "spell_name": spell,
            "current_amount": amount,
            "state": {"number": 1, "name": state},
            "spell_data": {"id": spell_id, "name": spell},
            "is_buff": is_buff,
        }
        value: int | None = amount
    else:
        payload = {
            "spell": {"id": spell_id, "name": spell},
            "caster": source_guid,
            "target": target_guid,
        }
        value = None
    return {
        "instance": "instance-1",
        "encounter": encounter,
        "timestamp_ms": timestamp_ms,
        "offset_ms": timestamp_ms,
        "event_index": event_index,
        "stream_type": stream,
        "source_guid": source_guid if stream == "aura_cast" else None,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "event_meta": {"event_index": event_index, "offset_ms": timestamp_ms},
        "state_payload": payload,
        "official": {"message": payload, "message_sha256": f"sha-{event_index}"},
        "provenance": {"frame_index": 0, "frame_message_index": message_index},
    }


class ChronicleExternalAuraAttributionV1Tests(unittest.TestCase):
    def test_unique_additions_bind_and_removal_closes_known_stack_owners(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row("aura", timestamp_ms=1008, event_index=20, amount=1),
            _row(
                "aura_cast",
                timestamp_ms=1100,
                event_index=30,
                source_guid="0xP2",
            ),
            _row("aura", timestamp_ms=1105, event_index=40, amount=2),
            _row(
                "aura",
                timestamp_ms=31_105,
                event_index=50,
                state="StateRemoved",
                amount=0,
            ),
        ]

        attributed, summary = attribute_state_rows(rows)

        self.assertEqual(len(attributed), 3)
        self.assertEqual(
            attributed[0]["attribution"]["source_guid"],  # type: ignore[index]
            "0xP1",
        )
        self.assertEqual(
            attributed[1]["lifecycle"]["active_attributed_stack_owner_counts"],  # type: ignore[index]
            {"0xP1": 1, "0xP2": 1},
        )
        removal = attributed[2]
        self.assertIsNone(removal["attribution"]["source_guid"])  # type: ignore[index]
        self.assertEqual(
            removal["attribution"]["status"],  # type: ignore[index]
            "STATE_REMOVED_CASTER_NOT_IN_AURA_SCHEMA",
        )
        self.assertEqual(
            removal["lifecycle"]["closed_attributed_stack_owner_counts"],  # type: ignore[index]
            {"0xP1": 1, "0xP2": 1},
        )
        self.assertEqual(summary["state_added_resolution"]["unique_attributed"], 2)
        self.assertFalse(summary["claim_boundary"]["dynamic_armor_projection_authorized"])

    def test_multiple_preceding_casts_are_rejected_not_nearest_guessed(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row(
                "aura_cast",
                timestamp_ms=1005,
                event_index=11,
                source_guid="0xP2",
            ),
            _row("aura", timestamp_ms=1008, event_index=20),
            _row(
                "aura",
                timestamp_ms=31_008,
                event_index=30,
                state="StateRemoved",
                amount=0,
            ),
        ]

        attributed, summary = attribute_state_rows(rows)

        addition = attributed[0]
        self.assertIsNone(addition["attribution"]["source_guid"])  # type: ignore[index]
        self.assertEqual(addition["attribution"]["candidate_count"], 2)  # type: ignore[index]
        self.assertEqual(
            addition["attribution"]["status"],  # type: ignore[index]
            "AMBIGUOUS_PRECEDING_AURA_CASTS_REJECTED",
        )
        self.assertEqual(
            attributed[1]["lifecycle"]["closed_unresolved_stack_count"], 1  # type: ignore[index]
        )
        self.assertEqual(summary["state_added_resolution"]["ambiguous_rejected"], 1)

    def test_join_requires_exact_encounter_target_and_spell_identity(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xWRONG_TARGET",
                target_guid="0xOTHER",
            ),
            _row(
                "aura_cast",
                timestamp_ms=1001,
                event_index=11,
                source_guid="0xWRONG_SPELL",
                spell="Expose Armor",
                spell_id=11198,
            ),
            _row("aura", timestamp_ms=1008, event_index=20),
        ]

        attributed, summary = attribute_state_rows(rows)

        self.assertEqual(
            attributed[0]["attribution"]["status"],  # type: ignore[index]
            "NO_PRECEDING_AURA_CAST_WITHIN_WINDOW",
        )
        self.assertEqual(summary["state_added_resolution"]["unmatched"], 1)

    def test_modified_state_never_borrows_a_cast_and_marks_new_stack_unknown(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row("aura", timestamp_ms=1008, event_index=20, amount=1),
            _row(
                "aura",
                timestamp_ms=1100,
                event_index=30,
                state="StateModified",
                amount=2,
            ),
        ]

        attributed, summary = attribute_state_rows(rows)

        modified = attributed[1]
        self.assertIsNone(modified["attribution"]["source_guid"])  # type: ignore[index]
        self.assertEqual(
            modified["lifecycle"]["active_attributed_stack_owner_counts"],  # type: ignore[index]
            {"0xP1": 1},
        )
        self.assertEqual(
            modified["lifecycle"]["active_unresolved_stack_count"], 1  # type: ignore[index]
        )
        self.assertEqual(summary["counts"]["nonaddition_state_unattributed"], 1)

    def test_bonereaver_is_player_self_buff_and_hostile_reduction_is_forbidden(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                spell="Bonereaver's Edge",
                spell_id=21153,
                source_guid="0xP1",
                target_guid="0xP1",
            ),
            _row(
                "aura",
                timestamp_ms=1007,
                event_index=20,
                spell="Bonereaver's Edge",
                spell_id=21153,
                target_guid="0xP1",
                is_buff=True,
            ),
            _row(
                "aura",
                timestamp_ms=11_007,
                event_index=30,
                spell="Bonereaver's Edge",
                spell_id=21153,
                target_guid="0xP1",
                state="StateRemoved",
                amount=0,
                is_buff=True,
            ),
        ]

        attributed, summary = attribute_state_rows(rows)

        event = attributed[0]
        self.assertEqual(event["status"], STATUS)
        self.assertEqual(event["aura_role"], "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE")
        self.assertEqual(
            attributed[1]["aura_role"], "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE"
        )
        self.assertIsNone(attributed[1]["attribution"]["source_guid"])  # type: ignore[index]
        self.assertTrue(
            event["projection_contract"]["bonereaver_hostile_reduction_forbidden"]  # type: ignore[index]
        )
        self.assertFalse(
            event["projection_contract"]["hostile_flat_armor_reduction_authorized"]  # type: ignore[index]
        )
        self.assertFalse(
            summary["claim_boundary"]["bonereaver_is_hostile_flat_armor_reduction"]
        )

    def test_out_of_order_state_rows_fail_closed(self) -> None:
        rows = [
            _row("aura", timestamp_ms=1008, event_index=20),
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
        ]

        with self.assertRaisesRegex(
            ChronicleExternalAuraAttributionError, "strict EventMeta order"
        ):
            attribute_state_rows(rows)


if __name__ == "__main__":
    unittest.main()
