from o2o_dps.chronicle_selected_fury_evidence_v1 import build_selected_fury_rows_v1


def _frame(stream, events):
    return [{"encounter_id": "enc-1", "first_timestamp_ms": 1000,
             "messages": ([{"event": row} for row in events]
                          if stream != "combatant_info" else events)}]


def _meta(index, offset):
    return {"event_index": index, "offset_ms": offset}


def test_selected_raid_preserves_timed_builds_and_separates_action_from_splash():
    guid = "0x0000000000000001"
    hostile_a = "0xF130000000000001"
    hostile_b = "0xF130000000000002"
    info = [
        {"meta": _meta(1, 0), "guid": guid, "race": "Human",
         "gear": [{"item_id": 10}], "talents": {"trees": ["a", "b", "c"]}},
        {"meta": _meta(5, 50), "guid": guid, "race": "Human",
         "gear": [{"item_id": 11}], "talents": {"trees": ["a", "b", "c"]}},
    ]
    frames = {
        "combatant_info": _frame("combatant_info", info),
        "spell_start": _frame("spell_start", [
            {"meta": _meta(0, 0), "caster": guid, "target": hostile_a,
             "spell_data": {"id": 2458, "name": "Berserker Stance"}},
            {"meta": _meta(2, 10), "caster": guid, "target": hostile_a,
             "spell_data": {"id": 23894, "name": "Bloodthirst"}},
            {"meta": _meta(6, 60), "caster": guid, "target": hostile_a,
             "spell_data": {"id": 11597, "name": "Sunder Armor"}},
        ]),
        "spell_go": _frame("spell_go", [
            {"meta": _meta(3, 11), "caster": guid, "target": hostile_a,
             "spell_data": {"id": 23894, "name": "Bloodthirst"}},
        ]),
        "damage": _frame("damage", [
            {"meta": _meta(4, 12), "caster": guid, "target": hostile_b,
             "amount": 300, "spell_data": {"id": 1680, "name": "Whirlwind"}},
        ]),
    }
    rows, metrics = build_selected_fury_rows_v1(
        instance_id="raid-1",
        metadata={"encounters": [{"id": "enc-1", "hostiles": [
            {"id": hostile_a}, {"id": hostile_b}]}]},
        fury_players=[{"guid": guid, "talent_layout": "a}b}c"}],
        frames=frames,
    )
    assert len(rows) == 1
    row = rows[0]
    assert [snapshot["gear"][0]["item_id"] for snapshot in row["combatant_info_snapshots"]] == [10, 11]
    assert [snapshot["time_ms"] for snapshot in row["combatant_info_snapshots"]] == [1000, 1050]
    assert [start["causal_info_snapshot_ordinal"] for start in row["starts"]] == [None, 0, 1]
    assert row["starts"][1]["action_key"] == "warrior.bloodthirst"
    assert row["starts"][1]["target_guid"] == hostile_a
    assert row["go_events"][0]["time_ms"] == 1011
    assert row["positive_hostile_damage"][0]["target_guid"] == hostile_b
    assert metrics["leaderboard_talent_layout_matches_any_info_snapshot_by_guid"][guid]
    assert metrics["combatant_info_snapshots"] == 2
    assert metrics["start_events_with_prior_info_snapshot"] == 2
