"""Regression repro: untargeted self actions must not erase hostile focus."""

import random

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    DynamicTeamRuntimeV1,
    _PrefixReplay,
    _event_target_mode,
)


ACTOR = "0x00000000000000A1"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"


def _trace(time_ms, event_type, target_guid):
    hostile = target_guid is not None
    event = {
        "event_type": event_type,
        "source": {"guid": ACTOR, "lane": "FRIENDLY_PLAYER"},
        "target": {
            "guid": target_guid,
            "lane": "HOSTILE_CREATURE" if hostile else "UNKNOWN",
            "voting_enemy_target": hostile,
        },
        "spell": {"id": 1, "name": "test"},
        "attribution": {"attribution_kind": "DIRECT_FRIENDLY_PLAYER", "player_guid": ACTOR},
    }
    if event_type == "DMG":
        event["damage"] = {"amount": 1, "amount_source": "OBSERVED_DMG_EVENT"}
    return {
        "trace_kind": "EXACT_PLAYER_EVENT",
        "player_guid": ACTOR,
        "anchor": {"offset_ms": time_ms},
        "event": event,
    }


def test_training_prefix_self_action_preserves_last_hostile_target():
    replay = _PrefixReplay(0)
    replay._observe_target(TARGET_A, "HOSTILE_CREATURE", 0)
    replay._observe_target(TARGET_B, "HOSTILE_CREATURE", 0)
    replay.observe(_trace(100, "DMG", TARGET_A))
    replay.observe(_trace(200, "GO", None))  # self buff does not switch target
    assert replay.actors[ACTOR].last_target_guid == TARGET_A
    assert _event_target_mode(_trace(300, "DMG", TARGET_A)["event"], replay, ACTOR) == "STAY_ALIVE"


def test_live_runtime_self_action_preserves_selected_hostile_target():
    runtime = DynamicTeamRuntimeV1(
        actors=[{"player_guid": ACTOR, "class": "WARRIOR", "spec_key": "WARRIOR_FURY"}],
        target_health_by_guid={TARGET_A: 100, TARGET_B: 100},
    )
    common = dict(
        actor_guid=ACTOR,
        attribution_kind="DIRECT_FRIENDLY_PLAYER",
        exact_source_guid=ACTOR,
        rng=random.Random(0),
        actor_role="TEAMMATE_RESPONSE_MODEL",
    )
    runtime.apply_event(
        time_ms=100, event_type="DMG", spell_id=1, spell_name="hit",
        target_mode="SWITCH_ALIVE", observed_damage=1, requested_damage=1,
        explicit_target_guid=TARGET_A, **common,
    )
    runtime.apply_event(
        time_ms=200, event_type="GO", spell_id=2, spell_name="self-buff",
        target_mode="NO_TARGET", observed_damage=0, requested_damage=0,
        **common,
    )
    assert runtime.actors[ACTOR].current_target_guid == TARGET_A
    next_hit = runtime.apply_event(
        time_ms=300, event_type="DMG", spell_id=1, spell_name="hit",
        target_mode="STAY_ALIVE", observed_damage=1, requested_damage=1,
        **common,
    )
    assert next_hit["target_guid"] == TARGET_A
