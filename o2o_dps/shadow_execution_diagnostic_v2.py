"""Observed Shadow execution chains, with optional independent Nampower confirmation.

This is an execution diagnostic, not a paired-policy reward or a network RTT
measurement. Client cast, START, GO and result are all locally observed events.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .brainofcat_shadow_checkpoint_v1 import import_jsonl
from .shadow_trace_diagnostic_v1 import WATCHED_SPELLS


SCHEMA = "shadow_execution_diagnostic/v2"
_BEGIN = re.compile(r"\[DEBUG\](\S+ \S+): BeginCast #(\d+) .*\((\d+)\) cast time: (\d+)")
_RESULT = re.compile(r"\[DEBUG\](\S+ \S+): Cast result for #(\d+) .*\((\d+)\) status (\d+) result (\d+)")
_RESULT_NAMES = {"SPELL_DAMAGE_EVENT_SELF": "DAMAGE", "SPELL_MISS_SELF": "MISS"}


def _time(row: Mapping[str, Any]) -> float:
    return float(row["at"]["getTimeSeconds"])


def _key(row: Mapping[str, Any]) -> tuple[str, str, int]:
    return str(row["sessionId"]), str(row["pullId"]), int(row["event"]["spellId"])


def parse_nampower_casts_v2(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Pair native BeginCast and result by its own cast number, without clock joining."""
    pending: dict[int, tuple[int, datetime, int]] = {}
    completed: list[dict[str, Any]] = []
    for line in lines:
        begin = _BEGIN.search(line)
        if begin:
            stamp, number, spell, cast_ms = begin.groups()
            pending[int(number)] = (int(spell), datetime.fromisoformat(stamp), int(cast_ms))
            continue
        result = _RESULT.search(line)
        if not result:
            continue
        stamp, number, spell, status, code = result.groups()
        begun = pending.pop(int(number), None)
        if begun is None or begun[0] != int(spell) or begun[0] not in WATCHED_SPELLS:
            continue
        elapsed = round((datetime.fromisoformat(stamp) - begun[1]).total_seconds() * 1000)
        completed.append({
            "cast_number": int(number), "spell_id": int(spell),
            "native_cast_time_ms": begun[2], "begin_to_result_ms": elapsed,
            "status": int(status), "result_code": int(code),
        })
    return completed


def summarize_shadow_execution_v2(
    records: Iterable[Mapping[str, Any]], *, nampower_casts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    source_rows = list(records)
    events = [row for row in source_rows if row.get("kind") == "event_delta"
              and isinstance(row.get("event"), Mapping)
              and row["event"].get("spellId") in WATCHED_SPELLS]
    grouped: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in events:
        grouped[_key(row)].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["at"]["causalOrdinal"])

    executions: list[dict[str, Any]] = []
    for (session, pull, spell), rows in grouped.items():
        previous_go_time = float("-inf")
        go_rows = [row for row in rows if row["event"].get("name") == "SPELL_GO_SELF"
                   and row["event"].get("sourceGuid") == row.get("playerGuid")]
        for go_index, go in enumerate(go_rows):
            go_time = _time(go)
            interval = [row for row in rows if previous_go_time < _time(row) <= go_time]
            casts = [row for row in interval if row["event"].get("name") == "SPELL_CAST_EVENT"]
            starts = [row for row in interval if row["event"].get("name") == "SPELL_START_SELF"
                      and row["event"].get("sourceGuid") == row.get("playerGuid")]
            following_go = _time(go_rows[go_index + 1]) if go_index + 1 < len(go_rows) else float("inf")
            results = [row for row in rows if row["event"].get("name") in _RESULT_NAMES
                       and row["event"].get("sourceGuid") == go.get("playerGuid")
                       and previous_go_time < _time(row) < following_go
                       and abs(_time(row) - go_time) <= 0.05]
            timing = (round((go_time - _time(casts[0])) * 1000)
                      if len(casts) == 1 and 0 <= go_time - _time(casts[0]) <= 5 else None)
            damage = sum(row["event"].get("amount", 0) for row in results
                         if row["event"]["name"] == "SPELL_DAMAGE_EVENT_SELF")
            execution = {
                "session_id": session, "pull_id": pull, "spell_id": spell,
                "spell_name": WATCHED_SPELLS[spell],
                "go_causal_ordinal": go["at"]["causalOrdinal"],
                "client_cast_count_before_go": len(casts),
                "client_cast_to_go_ms": timing,
                "start_count_before_go": len(starts),
                "start_to_go_ms": round((go_time - _time(starts[0])) * 1000) if len(starts) == 1 else None,
                "result_kinds": [
                    {"kind": _RESULT_NAMES[row["event"]["name"]],
                     "amount": row["event"].get("amount") if row["event"]["name"] == "SPELL_DAMAGE_EVENT_SELF" else None,
                     "causal_ordinal": row["at"]["causalOrdinal"]}
                    for row in results
                ],
                "observed_damage": damage,
                "result_status": "OBSERVED" if results else "NOT_OBSERVED_IN_JOURNAL",
                "nampower": None,
            }
            executions.append(execution)
            previous_go_time = go_time

    # Match independent native casts by spell occurrence, and compare durations.
    # No absolute clock join or client/server one-way latency is inferred.
    executions.sort(key=lambda item: (item["session_id"], item["go_causal_ordinal"]))
    native: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in nampower_casts:
        if row.get("spell_id") in WATCHED_SPELLS:
            native[int(row["spell_id"])].append(row)
    seen: dict[int, int] = defaultdict(int)
    for execution in executions:
        spell = execution["spell_id"]
        index = seen[spell]
        seen[spell] += 1
        if index < len(native[spell]):
            cast = native[spell][index]
            elapsed = execution["client_cast_to_go_ms"]
            execution["nampower"] = {
                **cast,
                "duration_agrees_with_single_client_cast": (
                    abs(elapsed - cast["begin_to_result_ms"]) <= 20 if elapsed is not None else None
                ),
            }

    by_spell = []
    for spell, name in WATCHED_SPELLS.items():
        matching = [item for item in executions if item["spell_id"] == spell]
        by_spell.append({
            "spell_id": spell, "name": name, "server_go_count": len(matching),
            "result_observed_count": sum(item["result_status"] == "OBSERVED" for item in matching),
            "damage_result_count": sum(any(result["kind"] == "DAMAGE" for result in item["result_kinds"])
                                       for item in matching),
            "miss_result_count": sum(any(result["kind"] == "MISS" for result in item["result_kinds"])
                                     for item in matching),
            "observed_damage": sum(item["observed_damage"] for item in matching),
            "single_cast_timing_count": sum(item["client_cast_to_go_ms"] is not None for item in matching),
            "native_cast_result_count": sum(item["nampower"] is not None for item in matching),
            "native_duration_agreement_count": sum(
                item["nampower"] is not None
                and item["nampower"]["duration_agrees_with_single_client_cast"] is True
                for item in matching
            ),
        })
    return {
        "schema": SCHEMA,
        "scope": "OBSERVED_EXECUTION_ONLY_NOT_CANDIDATE_REWARD_OR_NETWORK_RTT",
        "event_delta_count": sum(row.get("kind") == "event_delta" for row in source_rows),
        "spells": by_spell, "executions": executions,
        "missing_evidence": [
            "no executed candidate-vs-baseline paired outcome",
            "no synchronized client/server clock or one-way network latency",
            "no full real-combat target-health and team-damage trajectory in this journal",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-jsonl", required=True, type=Path)
    parser.add_argument("--nampower-log", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    native = parse_nampower_casts_v2(args.nampower_log.read_text(encoding="utf-8").splitlines()) if args.nampower_log else []
    result = summarize_shadow_execution_v2(import_jsonl(args.checkpoint_jsonl)["records"], nampower_casts=native)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "spells": result["spells"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
