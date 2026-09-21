from __future__ import annotations

import gzip
import json

import pytest

from o2o_dps.offline_team_wave_policy_v1 import (
    compile_offline_team_wave_feedback_policy_v1,
    load_offline_team_wave_record_v1,
    offline_team_wave_boundary_metadata_v1,
)
from o2o_dps.offline_wave_policy_v1 import OfflineWavePolicyV1Error
from o2o_dps.sim_bridge import ActionRef


PLAYER_GUID = "0x0000000000576754"
TARGET_A = "0xF13000F240276CB6"
TARGET_B = "0xF13000F244276CB4"
TARGET_C = "0xF13000F245276CB3"
WAVE_ID = "encounter-1:external-v2-wave:1"


def _transition(
    spell_id: int,
    *,
    elapsed_ms: int,
    serial: int,
    target_guid: str | None = None,
    event_type: str = "START",
    item_id: int | None = None,
) -> dict[str, object]:
    target = {
        "guid": target_guid,
        "hostile_object_preserved_nonvoting": False,
        "hostile_player_preserved_nonvoting": False,
        "lane": "HOSTILE_CREATURE" if target_guid else "UNKNOWN_NONVOTING",
        "voting_enemy_target": target_guid is not None,
    }
    return {
        "order_key": [69, 1_788_532_098_282 + elapsed_ms, serial, 3, serial],
        "current_event_label": {
            "event_type": event_type,
            "learning_role": "OBSERVED_ACTION_LABEL",
            "source_lane": "FRIENDLY_PLAYER",
            "action": {
                "phase": event_type,
                "item_id": item_id,
                "cast_flags": 2,
                "cast_time_ms": 0,
                "channel_time_ms": 0,
            },
            "spell": {"id": spell_id, "name": f"spell-{spell_id}"},
            "target": target,
        },
        "state_before": {"wave_elapsed_ms": elapsed_ms},
    }


def _team_wave() -> dict[str, object]:
    transitions = [
        _transition(2457, elapsed_ms=890, serial=1),
        _transition(11578, elapsed_ms=1_093, serial=2, target_guid=TARGET_A),
        _transition(2458, elapsed_ms=2_234, serial=3),
        _transition(23894, elapsed_ms=3_515, serial=4, target_guid=TARGET_A),
        _transition(28866, elapsed_ms=4_281, serial=5),
        _transition(20569, elapsed_ms=4_687, serial=6, target_guid=TARGET_A),
        _transition(1680, elapsed_ms=5_609, serial=7),
        _transition(20662, elapsed_ms=8_078, serial=8, target_guid=TARGET_A),
        _transition(25286, elapsed_ms=11_968, serial=9, target_guid=TARGET_B),
        _transition(23894, elapsed_ms=12_125, serial=10, target_guid=TARGET_B),
        _transition(25286, elapsed_ms=13_234, serial=11, target_guid=TARGET_C),
        _transition(25286, elapsed_ms=14_312, serial=12, target_guid=TARGET_C),
        _transition(20662, elapsed_ms=15_187, serial=13, target_guid=TARGET_C),
        _transition(99999, elapsed_ms=15_200, serial=14, target_guid=TARGET_C),
        _transition(
            23894,
            elapsed_ms=15_250,
            serial=15,
            target_guid=TARGET_C,
            event_type="DMG",
        ),
    ]
    return {
        "schema": "chronicle_external_team_wave_model_wave/v2",
        "status": "DESCRIPTIVE_NONVOTING_NOT_COMPARISON",
        "wave": {
            "instance_id": "raid-d900",
            "encounter_id": "encounter-1",
            "encounter_ordinal": 69,
            "wave_id": WAVE_ID,
            "wave_ordinal": 1,
        },
        "descriptive_outcome": {
            "reconstruction_binding": {
                "window": {
                    "boundary_duration_ms": 15_531,
                    "context_duration_ms": 15_531,
                    "first_anchor": {
                        "offset_ms": 0,
                        "timestamp_ms": 1_788_532_098_282,
                    },
                    "last_boundary_anchor": {
                        "offset_ms": 15_531,
                        "timestamp_ms": 1_788_532_113_813,
                    },
                }
            }
        },
        "players": [
            {
                "player": {
                    "class": "WARRIOR",
                    "guid": PLAYER_GUID,
                    "level": 60,
                    "name": "Expert One",
                    "race": "Tauren",
                },
                "warrior_spec_lane": {
                    "evidence_status": "OBSERVED",
                    "exact_guid_match": True,
                    "fury_or_arms_conflict_free_observation": True,
                    "observed_spec": "Fury",
                },
                "prefix_transitions": transitions,
            }
        ],
    }


def test_compile_team_wave_preserves_sequence_targets_items_and_build() -> None:
    policy = compile_offline_team_wave_feedback_policy_v1(
        _team_wave(),
        player_guid=PLAYER_GUID.lower(),
        build_segment_ref="catalog-segment:raid-d900:segment-0042",
    )

    assert [row.action_key for row in policy.actions] == [
        "warrior.battle_stance",
        "warrior.charge",
        "warrior.berserker_stance",
        "warrior.bloodthirst",
        "item.kiss_of_the_spider",
        "warrior.cleave",
        "warrior.whirlwind",
        "warrior.execute",
        "warrior.heroic_strike",
        "warrior.bloodthirst",
        "warrior.heroic_strike",
        "warrior.heroic_strike",
        "warrior.execute",
    ]
    assert [row.at_or_after_ms for row in policy.actions] == [
        890,
        1_093,
        2_234,
        3_515,
        4_281,
        4_687,
        5_609,
        8_078,
        11_968,
        12_125,
        13_234,
        14_312,
        15_187,
    ]
    assert policy.actions[4].action_ref == ActionRef(item_id=22954)
    assert policy.actions[5].lane == "queue"
    assert policy.actions[8].action_ref == ActionRef(spell_id=25286, tag=1)
    assert policy.complete_wave_coverage is True
    assert policy.observed_duration_ms == 15_531
    assert policy.observed_target_count == 3
    assert policy.source_target_guids == (TARGET_A, TARGET_B, TARGET_C)
    assert policy.build_segment_refs == (
        "catalog-segment:raid-d900:segment-0042",
    )
    assert all(
        row.build_segment_ref == "catalog-segment:raid-d900:segment-0042"
        for row in policy.actions
    )
    assert policy.unresolved_start_evidence == (
        {
            "source_order_key": [
                69,
                1_788_532_113_482,
                14,
                3,
                14,
            ],
            "source_action_key": None,
            "spell_id": 99999,
            "spell_name": "spell-99999",
            "item_id": None,
            "status": "OBSERVED_START_NOT_EXECUTABLE_WITH_CURRENT_REGISTRY",
        },
    )


def test_boundary_metadata_retains_exact_complete_anchors() -> None:
    boundary = offline_team_wave_boundary_metadata_v1(_team_wave())

    assert boundary["wave_id"] == WAVE_ID
    assert boundary["encounter_ordinal"] == 69
    assert boundary["boundary_duration_ms"] == 15_531
    assert boundary["first_anchor"]["timestamp_ms"] == 1_788_532_098_282
    assert boundary["last_boundary_anchor"]["timestamp_ms"] == 1_788_532_113_813
    assert boundary["complete_wave_coverage"] is True
    assert boundary["comparison_authorized"] is False


def test_load_streams_plain_and_gzip_jsonl(tmp_path) -> None:
    first = _team_wave()
    first["wave"] = dict(first["wave"], wave_id="other-wave")
    wanted = _team_wave()
    lines = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in (first, wanted)
    )
    plain = tmp_path / "waves.jsonl"
    plain.write_text(lines, encoding="utf-8")
    compressed = tmp_path / "waves.jsonl.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as handle:
        handle.write(lines)

    assert load_offline_team_wave_record_v1(
        plain, wave_id=WAVE_ID
    )["wave"]["wave_id"] == WAVE_ID
    assert load_offline_team_wave_record_v1(
        compressed, wave_id=WAVE_ID
    )["wave"]["wave_id"] == WAVE_ID
    with pytest.raises(OfflineWavePolicyV1Error, match="not found"):
        load_offline_team_wave_record_v1(plain, wave_id="missing-wave")


def test_rejects_unknown_spec_and_incomplete_boundary() -> None:
    unknown = _team_wave()
    unknown["players"][0]["warrior_spec_lane"]["observed_spec"] = "Unknown"
    unknown["players"][0]["warrior_spec_lane"][
        "fury_or_arms_conflict_free_observation"
    ] = False
    with pytest.raises(OfflineWavePolicyV1Error, match="conflict-free"):
        compile_offline_team_wave_feedback_policy_v1(
            unknown,
            player_guid=PLAYER_GUID,
            build_segment_ref="segment-0042",
        )

    incomplete = _team_wave()
    incomplete["descriptive_outcome"]["reconstruction_binding"]["window"][
        "last_boundary_anchor"
    ]["offset_ms"] = 15_000
    with pytest.raises(OfflineWavePolicyV1Error, match="complete boundary"):
        offline_team_wave_boundary_metadata_v1(incomplete)
