from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_external_compact_target_reducer_v1 import (
    ChronicleExternalCompactTargetReducerV1Error,
    reduce_external_team_wave_record,
)


INSTANCE = "instance-1"
ENCOUNTER = "encounter-1"
PLAYER = "0x0000000000000001"
PET = "0xF140000000000001"
MOB_A = "0xF1300000000000A1"
MOB_B = "0xF1300000000000B2"


def _side(guid: str, lane: str) -> dict[str, object]:
    return {"guid": guid, "lane": lane}


def _event(
    *,
    offset: int,
    index: int,
    event_type: str,
    source: dict[str, object],
    target: dict[str, object],
    player_guid: str | None = PLAYER,
    trace_kind: str = "EXACT_PLAYER_EVENT",
    amount: int | None = None,
    overkill: int = 0,
    attribution_kind: str = "DIRECT_FRIENDLY_PLAYER",
    spell_name: str = "Test Spell",
) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": event_type,
        "source": source,
        "target": target,
        "attribution": {
            "attribution_kind": attribution_kind,
            "player_guid": player_guid,
        },
        "spell": {"id": 101, "name": spell_name},
    }
    if event_type == "DMG":
        event["damage"] = {"amount": amount, "overkill": overkill}
    if event_type == "HEAL":
        event["healing"] = {"amount": amount}
    return {
        "trace_kind": trace_kind,
        "player_guid": player_guid,
        "trace_index": index,
        "anchor": {"offset_ms": offset},
        "event": event,
    }


def _record(trace: list[dict[str, object]], *, ordinal: int = 1) -> dict[str, object]:
    return {
        "wave": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "encounter_ordinal": 4,
            "wave_id": f"{ENCOUNTER}:wave:{ordinal}",
            "wave_ordinal": ordinal,
        },
        "exact_trace": trace,
    }


def _fury_player(*, guid: str = PLAYER, damage: int = 200) -> dict[str, object]:
    return {
        "player": {
            "guid": guid,
            "class": "WARRIOR",
            "level": 60,
        },
        "warrior_spec_lane": {
            "partition_key": "WARRIOR_FURY",
            "observed_spec": "Fury",
            "evidence_status": "OBSERVED",
            "exact_guid_match": True,
            "role": "FUTURE_EXPLICIT_ADAPTER_FURY_CANDIDATE_DESCRIPTIVE_NONVOTING",
            "voting_authorized": False,
        },
        "summary": {"damage_amount": damage},
        "leave_one_player_out_background": {
            "focal_player_guid": guid,
            "excluded_focal_damage_amount": damage,
        },
    }


def _write_gzip(path: Path, records: list[object]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class ChronicleExternalCompactTargetReducerV1Tests(unittest.TestCase):
    def test_reduces_compact_damage_healing_death_and_first_direct_events(self) -> None:
        direct_source = _side(PLAYER, "FRIENDLY_PLAYER")
        trace = [
            _event(
                offset=5,
                index=0,
                event_type="START",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
            ),
            _event(
                offset=10,
                index=1,
                event_type="DMG",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=100,
            ),
            _event(
                offset=20,
                index=2,
                event_type="DMG",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=60,
                overkill=10,
            ),
            _event(
                offset=25,
                index=3,
                event_type="HEAL",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=20,
            ),
            _event(
                offset=30,
                index=4,
                event_type="DMG",
                source=_side(PET, "FRIENDLY_CREATURE"),
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=40,
                attribution_kind="EXACT_OFFICIAL_OWNER",
            ),
            _event(
                offset=35,
                index=5,
                event_type="DEAD",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                player_guid=None,
                trace_kind="DEATH_MARKER",
            ),
            _event(
                offset=36,
                index=6,
                event_type="DMG",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=500,
            ),
            _event(
                offset=37,
                index=7,
                event_type="HEAL",
                source=direct_source,
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=70,
            ),
            _event(
                offset=45,
                index=8,
                event_type="DMG",
                source=direct_source,
                target=_side(MOB_B, "HOSTILE_CREATURE"),
                amount=50,
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "waves.jsonl.gz"
            _write_gzip(path, [_record(trace)])
            result = reduce_external_team_wave_record(
                path,
                instance_id=INSTANCE,
                encounter_id=ENCOUNTER,
                wave_ordinal=1,
                bin_width_ms=10,
            )
        self.assertEqual("DESCRIPTIVE_OUTCOME_ONLY", result["status"])
        self.assertEqual(2, result["target_count"])
        first, second = result["hostile_targets"]
        self.assertEqual(MOB_A, first["target_guid"])
        self.assertEqual(5, first["activity"]["first_relevant_offset_ms"])
        self.assertEqual(37, first["activity"]["last_relevant_offset_ms"])
        self.assertEqual(200, first["incoming_damage"]["positive_sum"])
        self.assertEqual(190, first["incoming_damage"]["overkill_adjusted_effective_damage"]["value"])
        self.assertEqual(
            {PLAYER: 200}, first["incoming_damage"]["by_player_guid"]
        )
        self.assertEqual(
            {PLAYER: 3},
            first["incoming_damage"]["event_count_by_player_guid"],
        )
        self.assertEqual(20, first["healing_received"]["positive_sum"])
        self.assertEqual(
            500,
            first["post_first_death_activity"]["incoming_damage"][
                "positive_sum"
            ],
        )
        self.assertEqual(
            70,
            first["post_first_death_activity"]["healing_received"][
                "positive_sum"
            ],
        )
        self.assertTrue(
            first["post_first_death_activity"][
                "excluded_from_hp_balance_and_team_kill_clock"
            ]
        )
        self.assertEqual("OBSERVED", first["death"]["status"])
        self.assertEqual(35, first["death"]["offset_ms"])
        self.assertEqual(5, first["first_direct_friendly_player_action"]["offset_ms"])
        self.assertEqual(10, first["first_direct_friendly_player_damage"]["offset_ms"])
        self.assertEqual(10, first["first_direct_friendly_player_positive_damage"]["offset_ms"])
        self.assertEqual(
            [
                {
                    "start_offset_ms": 10,
                    "end_offset_ms_exclusive": 20,
                    "positive_damage_sum": 100,
                    "event_count": 1,
                    "positive_damage_by_player_guid": {PLAYER: 100},
                },
                {
                    "start_offset_ms": 20,
                    "end_offset_ms_exclusive": 30,
                    "positive_damage_sum": 60,
                    "event_count": 1,
                    "positive_damage_by_player_guid": {PLAYER: 60},
                },
                {
                    "start_offset_ms": 30,
                    "end_offset_ms_exclusive": 40,
                    "positive_damage_sum": 40,
                    "event_count": 1,
                    "positive_damage_by_player_guid": {PLAYER: 40},
                },
            ],
            first["team_damage_timing"]["positive_incoming_damage_bins"],
        )
        self.assertEqual("CENSORED_AT_RECORD_WAVE_END", second["death"]["status"])
        self.assertEqual(45, second["death"]["offset_ms"])
        self.assertIsNone(second["first_direct_friendly_player_action"])
        self.assertIsNone(first["binding_boundaries"]["target_max_health"]["value"])
        self.assertIsNone(first["binding_boundaries"]["effective_armor"]["value"])

    def test_stops_after_selected_record_without_reading_later_invalid_json(self) -> None:
        trace = [
            _event(
                offset=1,
                index=0,
                event_type="DMG",
                source=_side(PLAYER, "FRIENDLY_PLAYER"),
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=1,
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "waves.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(_record(trace)) + "\n")
                handle.write("not-json\n")
            result = reduce_external_team_wave_record(
                path,
                instance_id=INSTANCE,
                encounter_id=ENCOUNTER,
                wave_id=f"{ENCOUNTER}:wave:1",
            )
        self.assertEqual(1, result["target_count"])

    def test_retains_exact_fury_focal_candidates_without_claiming_target_closure(self) -> None:
        trace = [
            _event(
                offset=10,
                index=0,
                event_type="DMG",
                source=_side(PLAYER, "FRIENDLY_PLAYER"),
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=160,
            )
        ]
        record = _record(trace)
        record["players"] = [_fury_player(damage=200)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "waves.jsonl.gz"
            _write_gzip(path, [record])
            result = reduce_external_team_wave_record(
                path,
                instance_id=INSTANCE,
                encounter_id=ENCOUNTER,
                wave_ordinal=1,
            )
        self.assertEqual(
            [
                {
                    "player_guid": PLAYER,
                    "player_class": "WARRIOR",
                    "observed_spec": "Fury",
                    "source_summary_damage_amount": 200,
                    "exact_leave_one_out_excluded_damage_amount": 200,
                    "source_summary_closes_to_leave_one_out": True,
                    "retained_predeath_positive_hostile_target_damage_amount": 160,
                    "source_summary_minus_retained_predeath_target_damage_amount": 40,
                    "retained_predeath_target_damage_closes_to_source_summary": False,
                    "source_lane_role": "FUTURE_EXPLICIT_ADAPTER_FURY_CANDIDATE_DESCRIPTIVE_NONVOTING",
                    "source_voting_authorized": False,
                    "comparison_authorized": False,
                }
            ],
            result["fury_focal_candidates"],
        )
        self.assertFalse(
            result["scientific_boundaries"]["fury_candidates_selected_by_outcome"]
        )

    def test_invalid_overkill_is_explicitly_unavailable_and_selector_is_unambiguous(self) -> None:
        trace = [
            _event(
                offset=1,
                index=0,
                event_type="DMG",
                source=_side(PLAYER, "FRIENDLY_PLAYER"),
                target=_side(MOB_A, "HOSTILE_CREATURE"),
                amount=10,
                overkill=11,
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "waves.jsonl.gz"
            _write_gzip(path, [_record(trace)])
            result = reduce_external_team_wave_record(
                path,
                instance_id=INSTANCE,
                encounter_id=ENCOUNTER,
                wave_ordinal=1,
            )
            with self.assertRaisesRegex(
                ChronicleExternalCompactTargetReducerV1Error,
                "exactly one of wave_id or wave_ordinal",
            ):
                reduce_external_team_wave_record(
                    path,
                    instance_id=INSTANCE,
                    encounter_id=ENCOUNTER,
                    wave_id=f"{ENCOUNTER}:wave:1",
                    wave_ordinal=1,
                )
        adjusted = result["hostile_targets"][0]["incoming_damage"]["overkill_adjusted_effective_damage"]
        self.assertIsNone(adjusted["value"])
        self.assertEqual("UNAVAILABLE", adjusted["status"])


if __name__ == "__main__":
    unittest.main()
