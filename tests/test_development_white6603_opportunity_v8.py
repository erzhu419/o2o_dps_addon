from __future__ import annotations

from copy import deepcopy

from o2o_dps.development_white6603_opportunity_v8 import (
    count_white6603_opportunities_v8,
    iter_white6603_opportunities_v8,
)


def _row(
    actor: str, delay: int, event: str, spell: int,
    *, direct: bool = True, hostile: bool = True, first: bool = False,
) -> dict:
    return {
        "actor": {
            "player_guid": actor,
            "class": "WARRIOR",
            "spec_key": "WARRIOR_FURY",
        },
        "label": {
            "delay_origin": "WAVE_START" if first else "PREVIOUS_ACTOR_EVENT",
            "inter_event_delay_ms": delay,
            "event_type": event,
            "spell_id": spell,
            "attribution_kind": (
                "DIRECT_FRIENDLY_PLAYER" if direct else "EXACT_OFFICIAL_OWNER"
            ),
            "target_lane": "HOSTILE_CREATURE" if hostile else "FRIENDLY_PLAYER",
        },
    }


def test_current_white_is_label_only_and_updates_next_prefix() -> None:
    rows = [
        _row("A", 1000, "START", 100, first=True),
        _row("A", 250, "DMG", 6603),
        _row("A", 0, "GO", 100),
        _row("A", 3000, "DMG", 6603),
    ]
    observations = list(iter_white6603_opportunities_v8(rows))
    assert [item["elapsed_ms"] for item in observations] == [1000, 1250, 1250, 4250]
    assert [item["phase"] for item in observations] == ["FIRST", "FIRST", "REPEAT", "REPEAT"]
    assert [item["age_ms"] for item in observations] == [1000, 1250, 0, 3000]
    assert [item["white6603"] for item in observations] == [False, True, False, True]
    assert observations[1]["prior_direct_hostile_start"] is True

    changed = deepcopy(rows)
    changed[1]["label"]["spell_id"] = 12345
    altered = list(iter_white6603_opportunities_v8(changed))
    assert observations[1]["contexts"] == altered[1]["contexts"]
    assert altered[2]["phase"] == "FIRST"


def test_actor_clocks_are_independent_and_owner_white_is_not_player_white() -> None:
    rows = [
        _row("A", 500, "DMG", 6603, first=True),
        _row("B", 700, "DMG", 6603, direct=False, first=True),
        _row("B", 300, "START", 100),
        _row("A", 800, "DMG", 6603, hostile=False),
        _row("B", 200, "DMG", 6603),
    ]
    observations = list(iter_white6603_opportunities_v8(rows))
    assert [(item["phase"], item["white6603"]) for item in observations] == [
        ("FIRST", True), ("FIRST", False), ("FIRST", False),
        ("REPEAT", True), ("FIRST", True),
    ]
    assert observations[4]["elapsed_ms"] == 1200
    assert observations[4]["prior_direct_hostile_start"] is True
    counts = count_white6603_opportunities_v8(rows)
    assert counts[("GLOBAL", "FIRST")][True] == 2
    assert counts[("GLOBAL", "FIRST")][False] == 2
    assert counts[("GLOBAL", "REPEAT")][True] == 1
    assert all("A" not in key and "B" not in key for key in counts)
