from __future__ import annotations

import unittest

from o2o_dps.chronicle_external_aura_attribution_v2 import (
    BONEREAVER_SELF_IGNORE_EFFECT_TUPLE,
    PHYSICAL_ARMOR_EFFECT_TUPLE,
    ChronicleExternalAuraAttributionV2Error,
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
    effect_tuple: tuple[int, int, int] = PHYSICAL_ARMOR_EFFECT_TUPLE,
    duration_ms: int = 30_000,
    cap_status: int = 2,
    message_index: int | None = None,
) -> dict[str, object]:
    if message_index is None:
        message_index = event_index
    if stream == "aura":
        payload: dict[str, object] = {
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
            "effect": effect_tuple[0],
            "effect_aura_name": effect_tuple[1],
            "effect_misc_value": effect_tuple[2],
            "duration_ms": duration_ms,
            "cap_status": cap_status,
        }
        value = None
    return {
        "instance": "instance-1",
        "encounter": "encounter-1",
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


class ChronicleExternalAuraAttributionV2Tests(unittest.TestCase):
    def test_multi_effect_spell_is_unique_after_physical_lane_filter(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                message_index=10,
                spell="Curse of Recklessness",
                spell_id=11717,
                source_guid="0xWARLOCK",
                effect_tuple=(6, 99, 0),
                duration_ms=120_000,
            ),
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=11,
                message_index=11,
                spell="Curse of Recklessness",
                spell_id=11717,
                source_guid="0xWARLOCK",
                effect_tuple=PHYSICAL_ARMOR_EFFECT_TUPLE,
                duration_ms=120_000,
            ),
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=12,
                message_index=12,
                spell="Curse of Recklessness",
                spell_id=11717,
                source_guid="0xWARLOCK",
                effect_tuple=(6, 92, 0),
                duration_ms=120_000,
            ),
            _row(
                "aura",
                timestamp_ms=1008,
                event_index=20,
                spell="Curse of Recklessness",
                spell_id=11717,
            ),
        ]

        events, summary = attribute_state_rows(rows)

        self.assertEqual(events[0]["attribution"]["legacy_candidate_row_count"], 3)
        self.assertEqual(
            events[0]["attribution"]["physical_effect_candidate_row_count"], 1
        )
        self.assertEqual(events[0]["attribution"]["source_guid"], "0xWARLOCK")
        self.assertEqual(
            summary["counts"]["candidate_decomposition::ambiguous_to_unique"], 1
        )
        self.assertEqual(
            summary["counts"]["nonarmor_or_conflicting_effect_lane_rejected"], 2
        )
        self.assertEqual(
            summary["counts_by_debuff"]["curse_of_recklessness"][
                "state_added_attributed_unique_physical_effect"
            ],
            1,
        )

    def test_two_physical_effect_rows_remain_ambiguous_and_fail_closed(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row(
                "aura_cast",
                timestamp_ms=1001,
                event_index=11,
                source_guid="0xP2",
            ),
            _row("aura", timestamp_ms=1008, event_index=20),
        ]

        events, summary = attribute_state_rows(rows)

        self.assertIsNone(events[0]["attribution"]["source_guid"])
        self.assertEqual(
            events[0]["attribution"]["status"],
            "MULTIPLE_PHYSICAL_EFFECT_CASTS_REJECTED",
        )
        self.assertEqual(
            summary["counts"]["candidate_decomposition::ambiguous_to_ambiguous"],
            1,
        )
        self.assertEqual(
            summary["counts_by_debuff"]["sunder_armor"][
                "state_added_multiple_physical_effect_rejected"
            ],
            1,
        )
        self.assertFalse(summary["claim_boundary"]["dynamic_armor_projection_authorized"])

    def test_spell_id_and_effect_tuple_are_both_required(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                spell_id=99999,
                source_guid="0xUNKNOWN_ID",
            ),
            _row(
                "aura",
                timestamp_ms=1008,
                event_index=20,
                spell_id=99999,
            ),
            _row(
                "aura_cast",
                timestamp_ms=2000,
                event_index=30,
                source_guid="0xWRONG_EFFECT",
                target_guid="0xOTHER",
                effect_tuple=(6, 99, 0),
            ),
            _row(
                "aura",
                timestamp_ms=2008,
                event_index=40,
                target_guid="0xOTHER",
            ),
        ]

        events, summary = attribute_state_rows(rows)

        self.assertEqual(
            [event["attribution"]["status"] for event in events],
            [
                "NO_PHYSICAL_EFFECT_CAST_WITHIN_WINDOW",
                "NO_PHYSICAL_EFFECT_CAST_WITHIN_WINDOW",
            ],
        )
        self.assertEqual(summary["counts"]["unsupported_spell_id_rejected"], 1)
        self.assertEqual(
            summary["counts"]["nonarmor_or_conflicting_effect_lane_rejected"], 1
        )

    def test_observed_lower_rank_is_admitted_only_on_physical_lane(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                spell="Expose Armor",
                spell_id=8647,
                source_guid="0xROGUE",
            ),
            _row(
                "aura",
                timestamp_ms=1008,
                event_index=20,
                spell="Expose Armor",
                spell_id=8647,
            ),
        ]

        events, summary = attribute_state_rows(rows)

        self.assertEqual(
            events[0]["attribution"]["status"],
            "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED",
        )
        self.assertEqual(summary["counts"].get("unsupported_spell_id_rejected", 0), 0)

    def test_sunder_full_stack_cast_is_refresh_with_zero_stack_delta(self) -> None:
        rows: list[dict[str, object]] = []
        for stack in range(1, 6):
            ts = 1000 + stack * 100
            rows.extend(
                [
                    _row(
                        "aura_cast",
                        timestamp_ms=ts,
                        event_index=stack * 10,
                        source_guid="0xTANK",
                    ),
                    _row(
                        "aura",
                        timestamp_ms=ts + 5,
                        event_index=stack * 10 + 1,
                        amount=stack,
                    ),
                ]
            )
        rows.append(
            _row(
                "aura_cast",
                timestamp_ms=2000,
                event_index=100,
                source_guid="0xTANK",
            )
        )

        _events, summary = attribute_state_rows(rows, include_cast_actions=True)

        refresh = summary["cast_actions"][-1]
        observed_addition = summary["cast_actions"][0]
        self.assertTrue(observed_addition["factual_result_event_observed"])
        self.assertEqual(observed_addition["factual_stack_delta"], 1)
        self.assertIsNotNone(observed_addition["matched_aura_state_anchor"])
        self.assertFalse(
            observed_addition["loo_counterfactual"]["counterfactual_executable"]
        )
        self.assertFalse(
            observed_addition["loo_counterfactual"]["focal_context_evaluated"]
        )
        self.assertFalse(
            observed_addition["loo_counterfactual"]["counterfactual_nonexecutable"]
        )
        self.assertTrue(
            observed_addition["loo_counterfactual"][
                "requires_no_focal_predecessor_contribution"
            ]
        )
        self.assertEqual(
            observed_addition["loo_counterfactual"]["status"],
            "CONDITIONALLY_ADMISSIBLE_REQUIRES_NO_FOCAL_PREDECESSOR_CONTRIBUTION",
        )
        self.assertEqual(
            refresh["action_status"],
            "SUNDER_FULL_STACK_REFRESH_CANDIDATE_PRESERVED",
        )
        self.assertEqual(refresh["factual_stack_delta"], 0)
        self.assertEqual(
            refresh["factual_lifecycle_status"],
            "CAST_ONLY_FULL_STACK_REFRESH_CANDIDATE",
        )
        self.assertFalse(refresh["factual_result_event_observed"])
        self.assertTrue(
            refresh["loo_counterfactual"]["counterfactual_nonexecutable"]
        )
        self.assertEqual(
            refresh["loo_counterfactual"]["reason"],
            "AURA_CAST_HAS_NO_SUCCESS_RESULT",
        )
        self.assertEqual(summary["cast_action_counts"]["MATCHED_AURA_STACK_INCREASE"], 5)
        self.assertEqual(
            summary["cast_action_counts"][
                "SUNDER_FULL_STACK_REFRESH_CANDIDATE_PRESERVED"
            ],
            1,
        )
        self.assertEqual(summary["factual_result_event_observed_count"], 5)
        self.assertEqual(summary["factual_refresh_candidate_count"], 1)
        self.assertEqual(summary["loo_conditionally_admissible_action_count"], 5)
        self.assertEqual(summary["loo_counterfactual_nonexecutable_count"], 1)

    def test_unmatched_sunder_below_cap_is_not_called_refresh(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xTANK",
            )
        ]

        _events, summary = attribute_state_rows(rows, include_cast_actions=True)

        action = summary["cast_actions"][0]
        self.assertEqual(action["action_status"], "UNMATCHED_CAST_NOT_EXECUTABLE_AS_REFRESH")
        self.assertIsNone(action["factual_stack_delta"])
        self.assertFalse(action["factual_result_event_observed"])
        self.assertTrue(
            action["loo_counterfactual"]["counterfactual_nonexecutable"]
        )

    def test_observed_full_stack_sunder_refresh_is_factual_zero_delta_only(self) -> None:
        rows: list[dict[str, object]] = []
        for stack in range(1, 6):
            ts = 1000 + stack * 100
            rows.extend(
                [
                    _row(
                        "aura_cast",
                        timestamp_ms=ts,
                        event_index=stack * 10,
                        source_guid="0xTANK",
                    ),
                    _row(
                        "aura",
                        timestamp_ms=ts + 5,
                        event_index=stack * 10 + 1,
                        amount=stack,
                    ),
                ]
            )
        rows.extend(
            [
                _row(
                    "aura_cast",
                    timestamp_ms=2000,
                    event_index=100,
                    source_guid="0xTANK",
                ),
                _row(
                    "aura",
                    timestamp_ms=2005,
                    event_index=101,
                    amount=5,
                ),
            ]
        )

        _events, summary = attribute_state_rows(rows, include_cast_actions=True)

        refresh = summary["cast_actions"][-1]
        self.assertEqual(
            refresh["action_status"], "MATCHED_AURA_REFRESH_ZERO_STACK_DELTA"
        )
        self.assertEqual(refresh["factual_stack_delta"], 0)
        self.assertTrue(refresh["factual_result_event_observed"])
        self.assertTrue(refresh["factual_aura_lifecycle_supported"])
        self.assertTrue(refresh["factual_refresh_observed"])
        self.assertFalse(refresh["factual_refresh_candidate"])
        self.assertTrue(
            refresh["loo_counterfactual"]["counterfactual_nonexecutable"]
        )
        self.assertEqual(
            refresh["loo_counterfactual"]["reason"],
            "ZERO_DELTA_REFRESH_CANNOT_REPAIR_REMOVED_FOCAL_PREDECESSOR",
        )

    def test_bonereaver_self_ignore_tuple_never_enters_hostile_contract(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                spell="Bonereaver's Edge",
                spell_id=21153,
                source_guid="0xP1",
                target_guid="0xP1",
                effect_tuple=BONEREAVER_SELF_IGNORE_EFFECT_TUPLE,
                duration_ms=10_000,
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
        ]

        events, summary = attribute_state_rows(rows, include_cast_actions=True)

        self.assertEqual(events[0]["aura_role"], "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE")
        self.assertTrue(
            events[0]["projection_contract"]["bonereaver_hostile_reduction_forbidden"]
        )
        self.assertEqual(summary["counts"]["bonereaver_self_ignore_effect_seen"], 1)
        self.assertEqual(summary["physical_effect_cast_count"], 0)
        self.assertEqual(summary["cast_actions"], [])

    def test_removal_never_borrows_caster(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row("aura", timestamp_ms=1005, event_index=20),
            _row(
                "aura",
                timestamp_ms=31_005,
                event_index=30,
                state="StateRemoved",
                amount=0,
            ),
        ]

        events, _summary = attribute_state_rows(rows)

        self.assertIsNone(events[-1]["attribution"]["source_guid"])
        self.assertEqual(
            events[-1]["attribution"]["status"],
            "STATE_REMOVED_CASTER_NOT_IN_AURA_PROTO",
        )
        self.assertEqual(
            events[-1]["lifecycle"]["closed_exact_source_counts"], {"0xP1": 1}
        )
        self.assertEqual(
            events[-1]["lifecycle"]["closed_unresolved_stack_count"], 0
        )

    def test_state_removed_reported_amount_is_not_post_event_active_state(self) -> None:
        rows = [
            _row(
                "aura_cast",
                timestamp_ms=1000,
                event_index=10,
                source_guid="0xP1",
            ),
            _row("aura", timestamp_ms=1005, event_index=20, amount=1),
            _row(
                "aura",
                timestamp_ms=31_005,
                event_index=30,
                state="StateRemoved",
                amount=1,
            ),
        ]

        events, _summary = attribute_state_rows(rows)
        removal = events[-1]

        self.assertEqual(removal["current_amount"], 1)
        self.assertEqual(removal["factual_transition"]["reported_current_amount"], 1)
        self.assertEqual(removal["factual_transition"]["prior_amount"], 1)
        self.assertEqual(removal["factual_transition"]["current_amount"], 0)
        self.assertEqual(removal["factual_transition"]["stack_delta"], -1)
        self.assertEqual(removal["lifecycle"]["current_amount"], 0)

    def test_cap_status_never_changes_stack_or_result_semantics(self) -> None:
        actions = []
        for cap_status in range(4):
            rows = [
                _row(
                    "aura_cast",
                    timestamp_ms=1000,
                    event_index=10,
                    source_guid="0xP1",
                    cap_status=cap_status,
                ),
                _row("aura", timestamp_ms=1005, event_index=20),
            ]
            _events, summary = attribute_state_rows(rows, include_cast_actions=True)
            actions.append(summary["cast_actions"][0])

        self.assertEqual({action["factual_stack_delta"] for action in actions}, {1})
        self.assertEqual(
            {action["factual_lifecycle_status"] for action in actions},
            {"OBSERVED_STATE_ADDED_STACK_INCREASE"},
        )
        self.assertEqual({action["cap_status"] for action in actions}, {0, 1, 2, 3})

    def test_out_of_order_rows_fail_closed(self) -> None:
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
            ChronicleExternalAuraAttributionV2Error, "strict EventMeta order"
        ):
            attribute_state_rows(rows)


if __name__ == "__main__":
    unittest.main()
