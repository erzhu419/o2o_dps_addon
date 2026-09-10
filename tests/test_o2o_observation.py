from __future__ import annotations

import copy
import unittest

from o2o_dps.o2o_observation import (
    OBSERVATION_FIELDS,
    ObservationContractError,
    project_decision_record,
    schema_descriptor,
    validate_observation,
)


EXPECTED_FIELDS = (
    "combat_time_ms",
    "recent_uniquely_linked_server_actions",
    "rage_gain_total_chronicle_units",
    "rage_gain_rows_observed",
    "rage_loss_total_chronicle_units",
    "rage_loss_rows_observed",
    "rage_scale_to_wow",
    "last_auto_attack_elapsed_ms",
    "known_player_aura_event_ledger",
    "known_outgoing_target_aura_event_ledger",
    "damaged_target_guids_seen",
    "talent_tree_point_totals",
    "gear_slot_count",
    "absolute_rage",
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
    "queue_intent",
    "client_keypress_ms",
    "target_hp",
    "player_hp",
    "boss_phase",
    "target_count",
    "stance",
    "range",
    "behind_target",
    "gear_item_ids",
    "exact_talent_ranks",
)


def _missing() -> dict[str, object]:
    return {
        "kind": "MISSING",
        "event_index": None,
        "csv_line": None,
        "offset_ms": None,
        "note": "unavailable",
    }


def _record() -> dict[str, object]:
    values = {name: None for name in OBSERVATION_FIELDS}
    masks = {name: False for name in OBSERVATION_FIELDS}
    provenance = {name: _missing() for name in OBSERVATION_FIELDS}

    supplied = {
        "combat_time_ms": (1250, "OBSERVED", 100),
        "recent_uniquely_linked_server_actions": ([], "RECONSTRUCTED", 100),
        "rage_gain_total_chronicle_units": (14.0, "RECONSTRUCTED", 97),
        "rage_gain_rows_observed": (1, "RECONSTRUCTED", 97),
        "rage_loss_rows_observed": (0, "RECONSTRUCTED", 100),
        "known_player_aura_event_ledger": (
            {"initial_state_complete": False, "duration_complete": False, "entries": []},
            "RECONSTRUCTED",
            98,
        ),
        "known_outgoing_target_aura_event_ledger": (
            {"initial_state_complete": False, "duration_complete": False, "entries": []},
            "RECONSTRUCTED",
            99,
        ),
        "damaged_target_guids_seen": ([], "RECONSTRUCTED", 100),
        "talent_tree_point_totals": ([21, 30, 0], "INFERRED", 10),
        "gear_slot_count": (19, "RECONSTRUCTED", 10),
    }
    for name, (value, status, event_index) in supplied.items():
        values[name] = value
        masks[name] = True
        provenance[name] = {
            "kind": status,
            "event_index": event_index,
            "csv_line": event_index + 1,
            "offset_ms": event_index * 10,
            "note": "fixture",
        }
    return {
        "schema": "chronicle_fury_decision/v1",
        "source": {"start_anchor": {"event_index": 100}},
        "state_before": values,
        "state_mask": masks,
        "state_provenance": provenance,
        "result": {"future_damage": 999999},
        "window_until_next_start_candidate": {"future_rage": 100},
        "eligibility": {"observable_behavior_label": True},
        "identity": {
            "player_name": "DoNotRead",
            "player_guid": "Player-Secret",
            "leaderboard_rows": [{"rank": 1, "name": "DoNotRead"}],
        },
    }


class O2OObservationTests(unittest.TestCase):
    def test_descriptor_and_projection_cover_current_known_fields(self) -> None:
        self.assertEqual(OBSERVATION_FIELDS, EXPECTED_FIELDS)
        descriptor = schema_descriptor()
        self.assertEqual(tuple(descriptor["fields"]), EXPECTED_FIELDS)
        self.assertFalse(descriptor["full_state"])
        self.assertEqual(descriptor["materialization"], "in_memory_projection_only")

        observation = project_decision_record(_record())
        self.assertEqual(observation["schema"], "O2OObservationV1")
        self.assertEqual(observation["observation_scope"], "partial")
        self.assertEqual(set(observation["fields"]), set(EXPECTED_FIELDS))
        self.assertEqual(
            observation["fields"]["combat_time_ms"],
            {
                "value": 1250,
                "status": "OBSERVED",
                "provenance": {
                    "kind": "OBSERVED",
                    "event_index": 100,
                    "csv_line": 101,
                    "offset_ms": 1000,
                    "note": "fixture",
                },
            },
        )
        self.assertEqual(
            observation["fields"]["talent_tree_point_totals"]["status"],
            "INFERRED",
        )
        self.assertEqual(
            observation["fields"]["absolute_rage"],
            {"value": None, "status": "MISSING", "provenance": _missing()},
        )
        validate_observation(observation)

    def test_result_window_labels_and_leaderboard_identity_are_not_state(self) -> None:
        first = _record()
        second = copy.deepcopy(first)
        second["result"] = {"future_damage": -1}
        second["window_until_next_start_candidate"] = {"future_rage": -1}
        second["eligibility"] = {"observable_behavior_label": False}
        second["identity"] = {
            "player_name": "Different",
            "player_guid": "Different",
            "leaderboard_rows": [{"rank": 999, "name": "Different"}],
        }
        self.assertEqual(
            project_decision_record(first), project_decision_record(second)
        )

    def test_unknown_identity_like_state_field_is_rejected(self) -> None:
        record = _record()
        for component in ("state_before", "state_mask", "state_provenance"):
            record[component]["leaderboard_rank"] = (
                1 if component == "state_before" else True
            )
        record["state_provenance"]["leaderboard_rank"] = {
            "kind": "OBSERVED",
            "event_index": 100,
        }
        with self.assertRaisesRegex(ObservationContractError, "extra=.*leaderboard_rank"):
            project_decision_record(record)

    def test_missing_source_invariants_are_enforced(self) -> None:
        mutations = (
            ("state_before", 10),
            ("state_mask", True),
        )
        for component, value in mutations:
            with self.subTest(component=component):
                record = _record()
                record[component]["absolute_rage"] = value
                with self.assertRaisesRegex(
                    ObservationContractError, "value=None and mask=false"
                ):
                    project_decision_record(record)

        record = _record()
        record["state_provenance"]["absolute_rage"]["kind"] = "INFERRED"
        with self.assertRaisesRegex(ObservationContractError, "mask=true"):
            project_decision_record(record)

    def test_future_state_evidence_is_rejected(self) -> None:
        record = _record()
        record["state_provenance"]["rage_gain_total_chronicle_units"][
            "event_index"
        ] = 101
        with self.assertRaisesRegex(ObservationContractError, "after START 100"):
            project_decision_record(record)

        observation = project_decision_record(_record())
        observation["fields"]["combat_time_ms"]["provenance"]["event_index"] = 101
        with self.assertRaisesRegex(ObservationContractError, "after START 100"):
            validate_observation(observation)

    def test_projected_missing_value_cannot_be_populated(self) -> None:
        observation = project_decision_record(_record())
        observation["fields"]["absolute_rage"]["value"] = 55
        with self.assertRaisesRegex(ObservationContractError, "must have value=None"):
            validate_observation(observation)

    def test_projected_contract_rejects_side_channel_state(self) -> None:
        observation = project_decision_record(_record())
        observation["result"] = {"damage": 999999}
        with self.assertRaisesRegex(ObservationContractError, "must contain only"):
            validate_observation(observation)


if __name__ == "__main__":
    unittest.main()
