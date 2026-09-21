from __future__ import annotations

import gzip
import json

from o2o_dps.historical_v4_build_match_diagnostic_v1 import (
    _REQUEST_SLOT_ORDER,
    diagnose_historical_v4_build_match_v1,
)


def _write_jsonl(path, *rows):
    with gzip.open(path, "wt", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row) + "\n")


def test_first_exact_segment_compares_build_without_claiming_sim_runnability(tmp_path):
    request_items = [{"id": 100 + index} for index in range(17)]
    request_items[14] = {"id": 55127}
    request_items[15] = {"id": 19866}
    request = {
        "raid": {"parties": [{"players": [{
            "equipment": {"items": request_items}, "talentsString": "123-04",
            "race": "RaceGnome",
        }]}]},
    }
    slots = [
        {
            "inventory_slot": slot,
            "slot_name": "OFF_HAND" if slot == 17 else f"SLOT_{slot}",
            "item_id": 0 if slot == 17 else request_items[index]["id"],
            "permanent_enchant_id": None,
            "status": "OBSERVED_EMPTY" if slot == 17 else "OBSERVED_EQUIPPED",
        }
        for index, slot in enumerate(_REQUEST_SLOT_ORDER)
    ]
    manifest = {
        "mapping_partitions": [{"instance_id": "raid", "path": "mapping.jsonl.gz"}],
        "segment_dictionary": {"partition": {"path": "dictionary.jsonl.gz"}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_jsonl(tmp_path / "mapping.jsonl.gz", {
        "source": {"encounter_id": "fight", "player_guid": "0xABC", "wave_id": "wave"},
        "controllable_start_count": 8,
        "decision_bindings": [
            {"decision_ordinal": 0, "timestamp_ms": 1020},
            {"decision_ordinal": 3, "timestamp_ms": 7200},
        ],
        "binding_runs": [
            {"join_status": "JOINED_EXACT_CAUSAL_PREFIX", "segment_ref": "first",
             "first_decision_ordinal": 0, "last_decision_ordinal": 2},
            {"join_status": "JOINED_EXACT_CAUSAL_PREFIX", "segment_ref": "later",
             "first_decision_ordinal": 3, "last_decision_ordinal": 7},
        ],
    })
    _write_jsonl(tmp_path / "dictionary.jsonl.gz", {
        "segment_ref": "first", "catalog_line_number": 1, "weapon_mode": "TWO_HAND",
    })
    _write_jsonl(tmp_path / "catalog.jsonl.gz", {
        "identity": {"build_segment_id": "segment-0001"},
        "player": {"race": "Gnome"},
        "observation": {
            "valid_from": {"timestamp_ms": 900},
            "valid_until_or_unknown": {"timestamp_ms": 7100},
        },
        "equipment": {"slots": slots},
        "talents": {
            "translation_status": "TRANSLATED_EXACT",
            "original_summary": [3, 4, 0],
            "original_tree_rank_strings": ["123000", "0400", "000"],
        },
        "coverage": {
            "runtime_executable": False,
            "simulator_representation": {"reasons": ["ITEM_EFFECT_NOT_COVERED"]},
            "comparison": {"eligible": False},
        },
    })
    with gzip.open(tmp_path / "wave.json.gz", "wt", encoding="utf-8") as target:
        json.dump({"waves": [{"wave_id": "wave", "window": {
            "first_anchor": {"timestamp_ms": 1000},
        }}]}, target)

    result = diagnose_historical_v4_build_match_v1(
        instance_id="raid", encounter_id="fight", focal_guid="0xabc",
        controlled_request=request, join_manifest_path=tmp_path / "manifest.json",
        catalog_path=tmp_path / "catalog.jsonl.gz",
        wave_reconstruction_path=tmp_path / "wave.json.gz",
        controlled_stop_ms=6000,
    )
    assert result["source"]["first_segment_historical_start_ordinals"] == [0, 2]
    assert result["source"]["segment_id"] == "segment-0001"
    assert result["historical_build"]["weapon_mode"] == "TWO_HAND"
    assert result["controlled_request"]["weapon_mode"] == "DUAL_WIELD"
    assert result["comparison"]["weapon_mode_equal"] is False
    assert result["comparison"]["race_equal"] is True
    assert result["comparison"]["talent_string_equal"] is True
    assert result["comparison"]["equipment_item_or_enchant_difference_count"] == 1
    assert result["comparison"]["same_historical_build_as_controlled_request"] is False
    assert result["historical_build"]["simulator_blockers"] == ["ITEM_EFFECT_NOT_COVERED"]
    assert result["historical_build"]["runtime_executable"] is False
    assert result["controlled_request"]["race"] == "RaceGnome"
    assert result["temporal_binding"]["first_segment_covers_controlled_window"] is True
    assert result["temporal_binding"]["first_segment_valid_until_offset_ms"] == 6100
    assert result["temporal_binding"]["next_segment_first_historical_start_offset_ms"] == 6200
