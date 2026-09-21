import json
from pathlib import Path

from o2o_dps.chronicle_fury_cooldown_evidence_v1 import (
    summarize_cooldown_observations_v1,
)


EVIDENCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs/evaluation/chronicle_fury_cooldown_evidence_v1.json"
)


def _row(
    action_id: str,
    spell_id: int,
    phase: str,
    timestamp_ms: int,
    order_index: int,
    *,
    player: str = "PLAYER-A",
    stream_type: str | None = None,
) -> dict[str, object]:
    return {
        "action_id": action_id,
        "instance_id": "INSTANCE-A",
        "encounter_id": "ENCOUNTER-A",
        "encounter_ordinal": 1,
        "player_guid": player,
        "phase": phase,
        "stream_type": stream_type or f"spell_{phase.lower()}",
        "timestamp_ms": timestamp_ms,
        "wave_elapsed_ms": timestamp_ms - 1_000,
        "order_key": [timestamp_ms, order_index, 1, 1],
        "spell_id": spell_id,
        "spell_name": "Recklessness" if spell_id == 1719 else "Death Wish",
        "item_id": None,
    }


def test_successful_spacing_is_not_mislabeled_as_a_cooldown_lower_bound() -> None:
    observations = [
        _row("warrior.recklessness", 1719, "START", 1_000, 1),
        _row("warrior.recklessness", 1719, "GO", 1_050, 2),
        _row("warrior.recklessness", 1719, "START", 301_000, 3),
        _row("warrior.recklessness", 1719, "GO", 301_040, 4),
    ]

    report = summarize_cooldown_observations_v1(observations)
    reck = report["actions"]["warrior.recklessness"]

    assert reck["minimum_observed_successful_start_interval_ms"] == 300_000
    assert reck["cooldown_lower_bound_ms"] is None
    assert report["recklessness_simulator_conflict_adjudication"][
        "thirty_minute_code"
    ] == "CONTRADICTED_IF_NO_COOLDOWN_RESET_OR_EVENT_ALIAS"
    assert report["recklessness_simulator_conflict_adjudication"][
        "five_minute_comment"
    ] == "CONSISTENT_WITH_NEAR_5_MINUTE_OBSERVATION_NOT_PROOF"


def test_failed_start_is_not_used_as_a_successful_cooldown_use() -> None:
    observations = [
        _row("warrior.death_wish", 12328, "START", 1_000, 1),
        _row("warrior.death_wish", 12328, "FAIL", 1_010, 2),
        _row("warrior.death_wish", 12328, "START", 2_000, 3),
        _row("warrior.death_wish", 12328, "GO", 2_020, 4),
    ]

    report = summarize_cooldown_observations_v1(observations)
    death_wish = report["actions"]["warrior.death_wish"]

    assert death_wish["start_to_go_ms"]["count"] == 1
    assert death_wish["same_instance_player_adjacent_successful_start_ms"] is None


def test_spell_streams_do_not_fabricate_aura_duration_or_effect_size() -> None:
    observations = [
        _row("warrior.recklessness", 1719, "START", 1_000, 1),
        _row("warrior.recklessness", 1719, "GO", 1_000, 2),
    ]

    report = summarize_cooldown_observations_v1(observations)
    reck = report["actions"]["warrior.recklessness"]

    assert reck["aura_begin_or_end_event_count"] == 0
    assert reck["aura_duration_ms"] is None
    assert reck["effect_magnitude"] is None
    adjudication = report["recklessness_simulator_conflict_adjudication"]
    assert adjudication["twelve_vs_fifteen_second_aura_duration"].startswith("UNKNOWN")
    assert adjudication["fifty_vs_one_hundred_percent_crit"].startswith("UNKNOWN")


def test_frozen_compact_evidence_keeps_identity_and_inference_boundaries() -> None:
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))

    assert evidence["input_scope"]["episode_partition_count"] == 24
    assert evidence["input_scope"][
        "chronicle_external_state_event_partition_count"
    ] == 0
    assert evidence["input_scope"]["raw_chronicle_opened"] is False
    assert evidence["measurement_semantics"][
        "cooldown_lower_bound_from_repeat_spacing_ms"
    ] is None

    actions = evidence["actions"]
    assert actions["warrior.recklessness"][
        "same_instance_player_adjacent_successful_start_ms"
    ]["min"] == 1_806_942
    assert actions["warrior.recklessness"]["aura_duration_ms"] is None
    assert actions["warrior.recklessness"]["effect_magnitude"] is None
    assert actions["warrior.death_wish"][
        "same_instance_player_adjacent_successful_start_ms"
    ]["min"] == 180_077

    potion = actions["contra_rage_potion_effect_unresolved_item"]
    assert potion["spell_id"] == 17_528
    assert potion["observed_spell_names"] == ["Mighty Rage"]
    assert potion["observed_item_ids"] == []
    assert actions["trinket.slayers_crest_effect"]["observed_item_ids"] == [23_041]

    adjudication = evidence["recklessness_simulator_conflict_adjudication"]
    assert adjudication["evidence_leading_candidate"] == "thirty_minute_code"
    assert adjudication["twelve_vs_fifteen_second_aura_duration"].startswith(
        "UNKNOWN"
    )
    assert adjudication["fifty_vs_one_hundred_percent_crit"].startswith("UNKNOWN")
