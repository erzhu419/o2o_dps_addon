from __future__ import annotations

import copy
import unittest

from o2o_dps.o2o_observation import OBSERVATION_FIELDS
from o2o_dps.state_reconstructor_v1 import (
    StateReconstructionError,
    StateReconstructorV1,
    iter_reconstructed_observations,
    validate_state_evidence_cutoff,
)


def _missing() -> dict[str, object]:
    return {
        "kind": "MISSING",
        "event_index": None,
        "csv_line": None,
        "offset_ms": None,
        "note": "fixture missing",
    }


def _result(
    *, association: str = "unlinked", status: str = "missing", event_index: int | None = None
) -> dict[str, object]:
    return {
        "association": association,
        "status": status,
        "anchor": (
            {
                "kind": "OBSERVED",
                "event_index": event_index,
                "csv_line": event_index + 100 if event_index is not None else None,
                "offset_ms": event_index * 10 if event_index is not None else None,
                "note": "fixture GO" if status == "succeeded" else "fixture FAIL",
            }
            if event_index is not None
            else None
        ),
        "start_to_result_ms": None,
        "candidate_action_ids": [],
    }


def _record(
    start_event_index: int,
    *,
    player_guid: str = "Player-A",
    encounter_id: str = "Encounter-1",
    spell_id: int = 23894,
    spell_name: str = "Bloodthirst",
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    values = {name: None for name in OBSERVATION_FIELDS}
    masks = {name: False for name in OBSERVATION_FIELDS}
    provenance = {name: _missing() for name in OBSERVATION_FIELDS}
    values["combat_time_ms"] = start_event_index * 10
    masks["combat_time_ms"] = True
    provenance["combat_time_ms"] = {
        "kind": "OBSERVED",
        "event_index": start_event_index,
        "csv_line": start_event_index + 10,
        "offset_ms": start_event_index * 10,
        "note": "current START",
    }
    return {
        "schema": "chronicle_fury_decision/v1",
        "identity": {
            "player_guid": player_guid,
            "encounter_id": encounter_id,
        },
        "source": {
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": start_event_index,
                "csv_line": start_event_index + 10,
                "offset_ms": start_event_index * 10,
            }
        },
        "action": {
            "decision_id": f"{player_guid}:{encounter_id}:{start_event_index}",
            "spell_id": spell_id,
            "spell_name": spell_name,
        },
        "result": result or _result(),
        "state_before": values,
        "state_mask": masks,
        "state_provenance": provenance,
        "window_until_next_start_candidate": {},
        "eligibility": {},
    }


def _stance_field(observation: dict[str, object]) -> dict[str, object]:
    return observation["fields"]["stance"]


class _OneShotRecords:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("compact decisions were iterated more than once")
        return iter(self.records)


class StateReconstructorV1Tests(unittest.TestCase):
    def test_unique_successful_stance_go_applies_only_to_later_start(self) -> None:
        records = [
            _record(
                10,
                spell_id=2457,
                spell_name="Battle Stance",
                result=_result(
                    association="unique", status="succeeded", event_index=20
                ),
            ),
            _record(15),
            _record(21),
        ]

        observations = list(iter_reconstructed_observations(records))
        self.assertEqual(_stance_field(observations[0])["status"], "MISSING")
        self.assertEqual(_stance_field(observations[1])["status"], "MISSING")
        self.assertEqual(
            _stance_field(observations[2]),
            {
                "value": "battle",
                "status": "RECONSTRUCTED",
                "provenance": {
                    "kind": "RECONSTRUCTED",
                    "event_index": 20,
                    "csv_line": 120,
                    "offset_ms": 200,
                    "note": (
                        "carried forward from prior uniquely associated successful "
                        "stance GO"
                    ),
                    "source_decision_id": "Player-A:Encounter-1:10",
                    "source_spell_id": 2457,
                },
            },
        )
        for name in (
            "absolute_rage",
            "gcd_remaining_ms",
            "cooldown_remaining_ms",
            "mainhand_swing_remaining_ms",
            "offhand_swing_remaining_ms",
            "queue_intent",
            "client_keypress_ms",
        ):
            self.assertEqual(observations[2]["fields"][name]["status"], "MISSING")

    def test_failed_and_ambiguous_stance_results_do_not_establish_stance(self) -> None:
        records = [
            _record(
                10,
                player_guid="Player-Failed",
                spell_id=2458,
                spell_name="Berserker Stance",
                result=_result(
                    association="unique", status="failed", event_index=11
                ),
            ),
            _record(12, player_guid="Player-Failed"),
            _record(
                20,
                player_guid="Player-Ambiguous",
                spell_id=71,
                spell_name="Defensive Stance",
                result=_result(
                    association="ambiguous", status="succeeded", event_index=21
                ),
            ),
            _record(22, player_guid="Player-Ambiguous"),
        ]

        observations = list(iter_reconstructed_observations(records))
        self.assertEqual(_stance_field(observations[1])["status"], "MISSING")
        self.assertEqual(_stance_field(observations[3])["status"], "MISSING")

    def test_ambiguous_success_invalidates_an_older_carried_stance(self) -> None:
        records = [
            _record(
                10,
                spell_id=2457,
                spell_name="Battle Stance",
                result=_result(
                    association="unique", status="succeeded", event_index=11
                ),
            ),
            _record(
                20,
                spell_id=2458,
                spell_name="Berserker Stance",
                result=_result(
                    association="ambiguous", status="succeeded", event_index=21
                ),
            ),
            _record(22),
        ]

        observations = list(iter_reconstructed_observations(records))
        self.assertEqual(_stance_field(observations[1])["value"], "battle")
        self.assertEqual(_stance_field(observations[2])["status"], "MISSING")

    def test_state_is_isolated_by_player_and_encounter(self) -> None:
        records = [
            _record(
                10,
                spell_id=71,
                spell_name="Defensive Stance",
                result=_result(
                    association="unique", status="succeeded", event_index=11
                ),
            ),
            _record(20, player_guid="Player-B"),
            _record(20, encounter_id="Encounter-2"),
            _record(20),
        ]

        observations = list(iter_reconstructed_observations(records))
        self.assertEqual(_stance_field(observations[1])["status"], "MISSING")
        self.assertEqual(_stance_field(observations[2])["status"], "MISSING")
        self.assertEqual(_stance_field(observations[3])["value"], "defensive")

    def test_nested_state_evidence_after_start_is_rejected(self) -> None:
        record = _record(10)
        record["state_before"]["recent_uniquely_linked_server_actions"] = [
            {"result_event_index": 11}
        ]
        record["state_mask"]["recent_uniquely_linked_server_actions"] = True
        record["state_provenance"]["recent_uniquely_linked_server_actions"] = {
            "kind": "RECONSTRUCTED",
            "event_index": 9,
            "csv_line": 19,
            "offset_ms": 90,
            "note": "fixture ledger",
        }
        with self.assertRaisesRegex(
            StateReconstructionError, "result_event_index.*after START 10"
        ):
            validate_state_evidence_cutoff(record)

        record = _record(10)
        record["state_provenance"]["combat_time_ms"]["support"] = {
            "event_index": 12
        }
        with self.assertRaisesRegex(
            StateReconstructionError, "support.event_index.*after START 10"
        ):
            validate_state_evidence_cutoff(record)

    def test_iterator_is_consumed_once_and_inputs_are_not_mutated(self) -> None:
        records = [
            _record(
                10,
                spell_id=2458,
                spell_name="Berserker Stance",
                result=_result(
                    association="unique", status="succeeded", event_index=11
                ),
            ),
            _record(12),
        ]
        original = copy.deepcopy(records)
        source = _OneShotRecords(records)

        observations = list(iter_reconstructed_observations(source))

        self.assertEqual(source.iterations, 1)
        self.assertEqual(records, original)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[1]["schema"], "O2OObservationV1")
        self.assertEqual(_stance_field(observations[1])["value"], "berserker")

    def test_player_encounter_stream_must_be_start_ordered(self) -> None:
        reconstructor = StateReconstructorV1()
        reconstructor.reconstruct_observation(_record(20))
        with self.assertRaisesRegex(StateReconstructionError, "ordered by START"):
            reconstructor.reconstruct_observation(_record(19))


if __name__ == "__main__":
    unittest.main()
