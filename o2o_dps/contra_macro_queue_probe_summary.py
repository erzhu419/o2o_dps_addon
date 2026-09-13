"""Decode one saved Contra_new client queue probe without copying WoW data."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any, Sequence

from .import_savedvariables import _Parser, _extract_records, _to_json_value


SCHEMA = "brainofcat_contra_macro_queue_probe_summary/v1"
WW = frozenset({1680})
CLEAVE = frozenset({845, 7369, 11608, 11609, 20569, 20571})
RESULT_EVENTS = frozenset({"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"})
_COMBAT_CLOCK = re.compile(r"^(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}):(\d{2})\.\d+")


def _marker(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((row for row in reversed(rows) if row.get("event") == name), None)


def _events_between(
    rows: list[dict[str, Any]], start: int | None, end: int | None
) -> list[dict[str, Any]]:
    if start is None:
        return []
    return [
        row for row in rows
        if isinstance(row.get("sequence"), int)
        and row["sequence"] > start
        and (end is None or row["sequence"] < end)
    ]


def _spell_events(
    rows: list[dict[str, Any]], spell_ids: frozenset[int], event: str
) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row.get("event") == event and row.get("spellID") in spell_ids
    ]


def _client(rows: list[dict[str, Any]], spell_ids: frozenset[int]) -> dict[str, Any]:
    casts = _spell_events(rows, spell_ids, "SPELL_CAST_EVENT")
    return {
        "accepted": any(row.get("castSucceeded") is True for row in casts),
        "rejected": any(row.get("castSucceeded") is False for row in casts),
        "cast_types": sorted({
            row["castType"] for row in casts
            if isinstance(row.get("castType"), int)
        }),
        "sequences": [row["sequence"] for row in casts],
    }


def _server(rows: list[dict[str, Any]], spell_ids: frozenset[int]) -> dict[str, Any]:
    goes = _spell_events(rows, spell_ids, "SPELL_GO_SELF")
    results = [
        row for row in rows
        if row.get("event") in RESULT_EVENTS and row.get("spellID") in spell_ids
    ]
    return {
        "go_sequences": [row["sequence"] for row in goes],
        "result_sequences": [row["sequence"] for row in results],
        "damage": sum(
            row.get("amount", 0) or 0
            for row in results if row.get("event") == "SPELL_DAMAGE_EVENT_SELF"
        ),
    }


def _queue_codes(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    queues = _spell_events(rows, CLEAVE, "SPELL_QUEUE_EVENT")
    return {
        str(code): [row["sequence"] for row in queues if row.get("queueEventCode") == code]
        for code in (0, 1, 3, 5)
    }


def summarize_probe(
    state: dict[str, Any], calibration: list[dict[str, Any]]
) -> dict[str, Any]:
    """Interpret only typed records belonging to the latest probe run."""
    run_id = state.get("runId")
    if not isinstance(run_id, str):
        return {"schema": SCHEMA, "status": "not_recorded", "reason": "no_probe_run_id"}
    rows = sorted(
        (
            row for row in calibration
            if row.get("task", {}).get("taskRunId") == run_id
            or row.get("marker", {}).get("runId") == run_id
        ),
        key=lambda row: row.get("sequence", 0),
    )
    a_request = _marker(rows, "CALIBRATION_CONTRA_PROBE_A_REQUEST")
    a_returned = _marker(rows, "CALIBRATION_CONTRA_PROBE_A_CALLS_RETURNED")
    a_end = _marker(rows, "CALIBRATION_CONTRA_PROBE_A_WINDOW_END")
    b_reentry = _marker(rows, "CALIBRATION_CONTRA_PROBE_B_REENTRY")
    b_returned = _marker(rows, "CALIBRATION_CONTRA_PROBE_B_CALLS_RETURNED")
    completion = _marker(rows, "CALIBRATION_CONTRA_PROBE_COMPLETED")
    incomplete = _marker(rows, "CALIBRATION_CONTRA_PROBE_INCOMPLETE")
    terminal = completion or incomplete
    b_requests = [
        row for row in rows
        if row.get("event") == "CALIBRATION_CONTRA_PROBE_B_QUEUE_REQUEST"
        and (b_reentry is None or row["sequence"] < b_reentry["sequence"])
    ]
    b_request = b_requests[-1] if b_requests else None

    a_rows = _events_between(
        rows, a_request["sequence"] if a_request else None,
        a_end["sequence"] if a_end else None,
    )
    a_ww = _client(a_rows, WW)
    a_cleave = _client(a_rows, CLEAVE)
    a_ww_server = _server(a_rows, WW)
    a_cleave_server = _server(a_rows, CLEAVE)
    a_calls = (a_returned or {}).get("marker", {})
    a_success_unmet = [
        key for key, present in (
            ("same_key_call_marker", a_returned is not None),
            ("ww_client_accept", a_ww["accepted"]),
            ("cleave_client_on_swing_accept", a_cleave["accepted"] and 2 in a_cleave["cast_types"]),
            ("ww_server_go", bool(a_ww_server["go_sequences"])),
            ("cleave_server_go", bool(a_cleave_server["go_sequences"])),
            ("ww_result", bool(a_ww_server["result_sequences"])),
            ("cleave_result", bool(a_cleave_server["result_sequences"])),
        ) if not present
    ]
    a_missing = [
        key for key, present in (
            ("same_key_call_marker", a_returned is not None),
            ("ww_client_receipt", bool(a_ww["sequences"])),
            ("cleave_client_receipt", bool(a_cleave["sequences"])),
            ("ww_server_go", bool(a_ww_server["go_sequences"])),
            ("ww_result", bool(a_ww_server["result_sequences"])),
        ) if not present
    ]

    b_pre = _events_between(
        rows, b_request["sequence"] if b_request else None,
        b_reentry["sequence"] if b_reentry else None,
    )
    b_post = _events_between(
        rows, b_reentry["sequence"] if b_reentry else None,
        terminal["sequence"] if terminal else None,
    )
    b_client = _client(b_pre, CLEAVE)
    b_ww = _client(b_post, WW)
    b_cleave_server = _server(b_post, CLEAVE)
    b_pre_server = _server(b_pre, CLEAVE)
    # An on-swing cast receipt may arrive after SPELL_GO_SELF.  It is not
    # evidence that the reentry CastSpellByName call was accepted.
    first_cleave_go = min(b_cleave_server["go_sequences"], default=None)
    b_before_go = [
        row for row in b_post
        if first_cleave_go is None or row["sequence"] < first_cleave_go
    ]
    b_after_go = [
        row for row in b_post
        if first_cleave_go is not None and row["sequence"] > first_cleave_go
    ]
    b_cleave_recast = _client(b_before_go, CLEAVE)
    b_cleave_after_go = _client(b_after_go, CLEAVE)
    b_meta = (b_reentry or {}).get("marker", {})
    b_calls = (b_returned or {}).get("marker", {})
    b_pop = _queue_codes(b_post)
    b_missing = [
        key for key, present in (
            ("standalone_queue_client_accept", b_client["accepted"] and 2 in b_client["cast_types"]),
            ("next_key_reentry_marker", b_reentry is not None),
            ("reentry_calls_returned", b_returned is not None),
            ("reentry_ww_client_receipt", bool(b_ww["sequences"])),
            ("queue_terminal_after_reentry", bool(b_cleave_server["go_sequences"])
                or any(b_pop[str(code)] for code in (1, 3, 5))),
        ) if not present
    ]
    queue_outcome = "pending_or_unobserved"
    if b_cleave_server["go_sequences"]:
        queue_outcome = "server_go_after_reentry"
    elif any(b_pop[str(code)] for code in (1, 3, 5)):
        queue_outcome = "popped_without_observed_go"
    elif b_pre_server["go_sequences"]:
        queue_outcome = "executed_before_reentry"
    if b_calls.get("cleaveCallAt") is not None and b_cleave_recast["accepted"]:
        queue_outcome = "recast_client_accepted_" + queue_outcome
    elif b_calls.get("cleaveCallAt") is not None and b_cleave_recast["rejected"]:
        if b_client["accepted"] and b_cleave_server["go_sequences"]:
            queue_outcome = "prior_queue_survived_reentry_and_server_go"
        else:
            queue_outcome = "recast_client_rejected_" + queue_outcome

    a_result = {
        "request_sequence": a_request and a_request["sequence"],
        "window_end_sequence": a_end and a_end["sequence"],
        "ww_call_at": a_calls.get("wwCallAt"),
        "cleave_call_at": a_calls.get("cleaveCallAt"),
        "call_gap_seconds": (
            a_calls["cleaveCallAt"] - a_calls["wwCallAt"]
            if isinstance(a_calls.get("cleaveCallAt"), (int, float))
            and isinstance(a_calls.get("wwCallAt"), (int, float)) else None
        ),
        "ww_client": a_ww,
        "cleave_client": a_cleave,
        "cleave_queue_codes": _queue_codes(a_rows),
        "ww_server": a_ww_server,
        "cleave_server": a_cleave_server,
        "cleave_failure_result_codes": [
            row.get("spellResult") for row in a_rows
            if row.get("event") == "SPELL_FAILED_SELF"
            and row.get("spellID") in CLEAVE
        ],
        "missing": a_missing,
        "positive_chain_unmet": a_success_unmet,
    }
    a_outcome = "not_observed"
    if a_ww["accepted"] and a_cleave["accepted"] and a_cleave_server["go_sequences"]:
        a_outcome = "same_key_queue_accepted_and_executed"
    elif a_ww["accepted"] and a_cleave["rejected"] and not a_cleave_server["go_sequences"]:
        a_outcome = "same_key_queue_client_rejected"
    a_result["outcome"] = a_outcome
    b_result = {
        "queue_request_sequence": b_request and b_request["sequence"],
        "reentry_sequence": b_reentry and b_reentry["sequence"],
        "elapsed_from_queue_press_seconds": b_meta.get("elapsedFromQueuePress"),
        "elapsed_from_client_accept_seconds": b_meta.get("elapsedFromClientAccept"),
        "queue_current_raw_on_reentry": b_meta.get("queueCurrentRaw"),
        "queue_current_equals_one_on_reentry": b_meta.get("queueCurrent"),
        "queue_current_after_ww": b_calls.get("queueCurrentAfterWW"),
        "main_hand_remaining_on_reentry_seconds": b_calls.get("mainHandRemaining"),
        "source_cleave_guard": b_calls.get("sourceCleaveGuard"),
        "cleave_skipped_as_current": b_calls.get("cleaveSkippedCurrent"),
        "cleave_recast_requested": b_calls.get("cleaveCallAt") is not None,
        "standalone_queue_client": b_client,
        "standalone_queue_codes": _queue_codes(b_pre),
        "ww_reentry_client": b_ww,
        "cleave_reentry_client_before_go": b_cleave_recast,
        "post_first_go_cast_event_unattributed": b_cleave_after_go,
        "cleave_recast_failure_result_codes": [
            row.get("spellResult") for row in b_before_go
            if row.get("event") == "SPELL_FAILED_SELF"
            and row.get("spellID") in CLEAVE
        ],
        "queue_codes_after_reentry": b_pop,
        "cleave_server_before_reentry": b_pre_server,
        "cleave_server_after_reentry": b_cleave_server,
        "queue_outcome": queue_outcome,
        "missing": b_missing,
    }
    ready = completion is not None and not a_success_unmet and not b_missing
    observed_rejection = (
        completion is not None
        and a_outcome == "same_key_queue_client_rejected"
        and not b_missing
        and not a_missing
    )
    return {
        "schema": SCHEMA,
        "status": (
            "typed_chain_observed" if ready
            else "same_key_queue_rejected" if observed_rejection
            else "incomplete_evidence"
        ),
        "run_id": run_id,
        "addon_status": state.get("status"),
        "terminal_marker_sequence": terminal and terminal["sequence"],
        "incomplete_reason": (incomplete or {}).get("marker", {}).get("incompleteReason"),
        "source_fidelity": "equivalent_client_actions_not_full_Contra_macro",
        "record_count": len(rows),
        "a_same_key_ww_then_cleave": a_result,
        "b_standalone_queue_then_reentry": b_result,
        "wall_clocks": {
            "a": (a_request or {}).get("marker", {}).get("wallClock"),
            "b_queue": (b_request or {}).get("marker", {}).get("wallClock"),
            "b_reentry": b_meta.get("wallClock"),
        },
    }


def decode_savedvariables(source: str | Path) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    assignments = _Parser(path.read_text(encoding="utf-8-sig"), path.name).parse()
    _, calibration, _ = _extract_records(assignments)
    root = assignments["BrainOfCatCharacterDB"].fields
    raw_state = root.get("contraMacroQueueProbe")
    state = (
        _to_json_value(raw_state, "BrainOfCatCharacterDB.contraMacroQueueProbe")
        if raw_state is not None else {}
    )
    result = summarize_probe(state, [row for _, row in calibration])
    result["source"] = str(path)
    return result


def combat_log_excerpt(
    path: str | Path, wall_clocks: dict[str, str | None]
) -> list[str]:
    """Small human-readable server-log cross-check; not client acceptance."""
    anchors = []
    for value in wall_clocks.values():
        if value:
            anchors.append(datetime.strptime(value, "%Y-%m-%d %H:%M:%S"))
    if not anchors:
        return []
    selected: list[tuple[int, str]] = []
    counts = [0] * len(anchors)
    with Path(path).open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line_number, line in enumerate(stream):
            match = _COMBAT_CLOCK.match(line)
            if not match or not any(name in line for name in ("旋风斩", "顺劈斩")):
                continue
            month, day, hour, minute, second = map(int, match.groups())
            distances = []
            for anchor in anchors:
                observed = anchor.replace(
                    month=month, day=day, hour=hour, minute=minute, second=second
                )
                distances.append(abs(observed - anchor))
            nearest = min(range(len(anchors)), key=distances.__getitem__)
            if distances[nearest] <= timedelta(seconds=7) and counts[nearest] < 8:
                selected.append((line_number, line.rstrip("\r\n")))
                counts[nearest] += 1
    return [line for _, line in sorted(selected)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Exact BrainOfCat.lua SavedVariables file")
    parser.add_argument("--combat-log", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    result = decode_savedvariables(args.source)
    if args.combat_log is not None:
        result["combat_log_excerpt"] = combat_log_excerpt(
            args.combat_log, result.get("wall_clocks", {})
        )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
