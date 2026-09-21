"""Small d900 exact-trace diagnostic for early teammate timing and target spill."""

from __future__ import annotations

from collections import defaultdict
import argparse
import gzip
import json
from pathlib import Path


ENCOUNTER = "02829cd0-85c3-4b6f-adba-059398e6ae14"
WAVE = f"{ENCOUNTER}:external-v2-wave:1"
FOCAL = "0x0000000000576754"
TARGETS = (
    "0xF13000F240276CB6",
    "0xF13000F244276CB4",
    "0xF13000F245276CB3",
)
CUTOFFS = (0, 3_000, 6_000, 9_093)


def analyze(record: dict) -> dict:
    if record.get("wave", {}).get("wave_id") != WAVE:
        raise ValueError("selected wave differs")
    first_actor: dict[str, int] = {}
    damage_by_target_cutoff = {
        cutoff: {guid: {"all": 0, "focal": 0, "other_player": 0, "other": 0} for guid in TARGETS}
        for cutoff in CUTOFFS
    }
    spell_damage: dict[str, dict[tuple[int | None, str | None], int]] = defaultdict(lambda: defaultdict(int))
    spell_events: dict[str, dict[tuple[int | None, str | None], int]] = defaultdict(lambda: defaultdict(int))
    first_early_events = 0
    for row in record["exact_trace"]:
        event = row.get("event")
        anchor = row.get("anchor")
        if not isinstance(event, dict) or not isinstance(anchor, dict):
            continue
        offset = anchor.get("offset_ms")
        if type(offset) is not int or offset < 0:
            continue
        actor = row.get("player_guid") if row.get("trace_kind") == "EXACT_PLAYER_EVENT" else None
        if isinstance(actor, str) and actor != FOCAL:
            first_actor.setdefault(actor, offset)
            if offset < 3_000:
                first_early_events += 1
        target = event.get("target")
        guid = target.get("guid") if isinstance(target, dict) else None
        if guid not in TARGETS or event.get("event_type") != "DMG":
            continue
        damage = event.get("damage")
        amount = damage.get("amount") if isinstance(damage, dict) else None
        if type(amount) is not int or amount <= 0:
            continue
        attribution = event.get("attribution")
        kind = attribution.get("attribution_kind") if isinstance(attribution, dict) else None
        lane = event.get("source", {}).get("lane")
        if actor == FOCAL:
            owner = "focal"
        elif isinstance(actor, str) and kind == "DIRECT_FRIENDLY_PLAYER" and lane == "FRIENDLY_PLAYER":
            owner = "other_player"
        else:
            owner = "other"
        for cutoff in CUTOFFS:
            if offset <= cutoff:
                damage_by_target_cutoff[cutoff][guid]["all"] += amount
                damage_by_target_cutoff[cutoff][guid][owner] += amount
        if offset <= CUTOFFS[-1] and actor != FOCAL:
            spell = event.get("spell")
            key = (spell.get("id"), spell.get("name")) if isinstance(spell, dict) else (None, None)
            spell_damage[guid][key] += amount
            spell_events[guid][key] += 1
    return {
        "schema": "development_d900_team_spill_probe/v1",
        "status": "DESCRIPTIVE_DIAGNOSTIC_NOT_POLICY_INPUT",
        "wave_id": WAVE,
        "target_guids": list(TARGETS),
        "cutoffs_ms_inclusive": list(CUTOFFS),
        "damage_by_target_at_cutoff": {str(k): v for k, v in damage_by_target_cutoff.items()},
        "first_teammate_actor_event_offsets_ms": sorted(first_actor.values()),
        "first_teammate_actor_event_count_before_3000_ms": sum(value < 3000 for value in first_actor.values()),
        "teammate_exact_player_event_count_before_3000_ms": first_early_events,
        "teammate_spell_damage_before_9093_ms": {
            guid: [
                {"spell_id": key[0], "spell_name": key[1], "positive_damage": amount, "event_count": spell_events[guid][key]}
                for key, amount in sorted(spell_damage[guid].items(), key=lambda item: -item[1])[:15]
            ] for guid in TARGETS
        },
        "boundary": "Observed trace timing and hit target only; no inference of raid leader mandate or general melee reachability",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.stage5, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("wave", {}).get("wave_id") == WAVE:
                print(json.dumps(analyze(record), ensure_ascii=False, indent=2))
                return
    raise ValueError("exact wave not found")


if __name__ == "__main__":
    main()
