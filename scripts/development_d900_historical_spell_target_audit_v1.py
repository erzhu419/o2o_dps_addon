"""Read one Stage5 wave and summarize early DMG by owner, target and spell."""

from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path

WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
FOCAL = "0x0000000000576754"
TARGETS = (
    "0xF13000F240276CB6", "0xF13000F244276CB4", "0xF13000F245276CB3",
)
CUTOFFS_MS = (9093, 9098)


def _owner(row: dict, event: dict) -> str:
    actor = row.get("player_guid") if row.get("trace_kind") == "EXACT_PLAYER_EVENT" else None
    attribution = event.get("attribution")
    kind = attribution.get("attribution_kind") if isinstance(attribution, dict) else None
    source = event.get("source")
    lane = source.get("lane") if isinstance(source, dict) else None
    if isinstance(actor, str) and kind == "DIRECT_FRIENDLY_PLAYER" and lane == "FRIENDLY_PLAYER":
        return "focal_direct" if actor == FOCAL else "teammate_direct"
    if row.get("trace_kind") == "UNATTRIBUTED_EVENT":
        return "unattributed_fixed_background"
    return "other_non_direct"


def analyze(record: dict) -> dict:
    if record.get("wave", {}).get("wave_id") != WAVE:
        raise ValueError("selected wave differs")
    grouped = defaultdict(lambda: {
        "hit_count": 0, "raw_damage": 0, "declared_overkill": 0,
        "amount_minus_overkill_proxy": 0,
    })
    for row in record["exact_trace"]:
        event = row.get("event")
        anchor = row.get("anchor")
        if not isinstance(event, dict) or not isinstance(anchor, dict):
            continue
        offset = anchor.get("offset_ms")
        if type(offset) is not int or offset < 0 or offset > CUTOFFS_MS[-1]:
            continue
        target = event.get("target")
        guid = target.get("guid") if isinstance(target, dict) else None
        if event.get("event_type") != "DMG" or guid not in TARGETS:
            continue
        damage = event.get("damage")
        amount = damage.get("amount") if isinstance(damage, dict) else None
        if type(amount) is not int or amount <= 0:
            continue
        overkill = damage.get("overkill")
        declared_overkill = max(0, overkill) if type(overkill) is int else 0
        spell = event.get("spell")
        spell_id = spell.get("id") if isinstance(spell, dict) else None
        owner = _owner(row, event)
        for cutoff in CUTOFFS_MS:
            if offset > cutoff:
                continue
            item = grouped[(cutoff, owner, guid, spell_id)]
            item["hit_count"] += 1
            item["raw_damage"] += amount
            item["declared_overkill"] += declared_overkill
            item["amount_minus_overkill_proxy"] += max(0, amount - declared_overkill)
    rows = [
        {
            "cutoff_ms_inclusive": cutoff, "owner": owner,
            "target_guid": guid, "spell_id": spell_id, **counts,
        }
        for (cutoff, owner, guid, spell_id), counts in grouped.items()
    ]
    rows.sort(key=lambda row: (
        row["cutoff_ms_inclusive"], row["owner"], TARGETS.index(row["target_guid"]),
        -1 if row["spell_id"] is None else row["spell_id"],
    ))
    totals = []
    for cutoff in CUTOFFS_MS:
        for owner in (
            "teammate_direct", "focal_direct", "unattributed_fixed_background",
            "other_non_direct",
        ):
            for guid in TARGETS:
                matching = [row for row in rows if row["cutoff_ms_inclusive"] == cutoff
                            and row["owner"] == owner and row["target_guid"] == guid]
                totals.append({
                    "cutoff_ms_inclusive": cutoff, "owner": owner, "target_guid": guid,
                    **{name: sum(row[name] for row in matching) for name in (
                        "hit_count", "raw_damage", "declared_overkill",
                        "amount_minus_overkill_proxy",
                    )},
                })
    return {
        "schema": "development_d900_historical_spell_target_audit/v1",
        "status": "STAGE5_EXACT_TRACE_OBSERVED_NOT_RUNTIME_RECEIPT",
        "wave_id": WAVE,
        "target_guids": list(TARGETS),
        "cutoffs_ms_inclusive": list(CUTOFFS_MS),
        "per_spell": rows,
        "totals": totals,
        "damage_contract": (
            "Stage5 DMG.damage.amount is the logged raw positive amount; overkill is "
            "the separately logged field. amount_minus_overkill_proxy is not an "
            "authoritative applied-HP receipt."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.stage5, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("wave", {}).get("wave_id") == WAVE:
                print(json.dumps(analyze(record), ensure_ascii=False))
                return
    raise ValueError("exact wave not found")


if __name__ == "__main__":
    main()
