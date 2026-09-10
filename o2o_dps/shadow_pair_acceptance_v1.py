"""Audit one compact BrainOfCat Shadow session without rereading SavedVariables.

The audit deliberately separates three claims:

* the append-only transport contains confirmed, non-executed candidate pairs;
* each state is causally linked to an exact Cat2/expert action;
* typed outcome telemetry is complete enough to construct an actual reward.

Passing transport does not imply either of the latter claims, and this report
never authorizes deployment by itself.  Candidate superiority remains a
separate simulator/live-evaluation decision.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOURNAL = (
    PROJECT_ROOT
    / "offline_data"
    / "online_decisions"
    / "brainofcat_shadow_pairs_v2.jsonl"
)
DEFAULT_MANIFEST = DEFAULT_JOURNAL.with_suffix(".manifest.json")
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_shadow_pair_acceptance_v1.json"
)
SCHEMA = "brainofcat_shadow_pair_acceptance/v1"
MANIFEST_KIND = "brainofcat_shadow_pair_journal"
EXPECTED_SCHEMA_VERSION = 2
NEXT_SWING_FAMILIES = frozenset({"heroic_strike", "cleave"})
TARGET_AGNOSTIC_FAMILIES = frozenset({"whirlwind", "cleave"})
CAUSAL_COMPACT_EVENTS = frozenset(
    {
        "SPELL_CAST_EVENT",
        "SPELL_QUEUE_EVENT",
        "SPELL_GO_SELF",
        "SPELL_FAILED_SELF",
        "SPELL_DAMAGE_EVENT_SELF",
        "SPELL_MISS_SELF",
        "UNIT_CASTEVENT",
        "AUTO_ATTACK_SELF",
    }
)


class ShadowPairAcceptanceError(RuntimeError):
    """The compact journal or manifest cannot be audited."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ShadowPairAcceptanceError(f"cannot read {label} {path}: {error}") from error


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite_json
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ShadowPairAcceptanceError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ShadowPairAcceptanceError(f"{label} {path} must contain a JSON object")
    return value


def _read_journal(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, parse_constant=_reject_nonfinite_json)
                except (json.JSONDecodeError, ValueError) as error:
                    raise ShadowPairAcceptanceError(
                        f"invalid JSON at {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(value, dict):
                    raise ShadowPairAcceptanceError(
                        f"journal row {path}:{line_number} must be an object"
                    )
                rows.append(value)
    except (OSError, UnicodeError) as error:
        raise ShadowPairAcceptanceError(f"cannot read journal {path}: {error}") from error
    if not rows:
        raise ShadowPairAcceptanceError(f"journal {path} contains no rows")
    return rows


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _session_id(row: Mapping[str, Any]) -> str:
    return str(_mapping(row.get("shadowSample")).get("exportSessionId") or "").strip()


def _identity(row: Mapping[str, Any]) -> tuple[str, str]:
    provenance = _mapping(row.get("provenance"))
    return (
        str(provenance.get("source_identity") or "").strip(),
        str(row.get("decisionId") or "").strip(),
    )


def _manifest_identities(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for entry in _list(manifest.get("identities")):
        item = _mapping(entry)
        output.append(
            (
                str(item.get("source") or "").strip(),
                str(item.get("decision_id") or "").strip(),
            )
        )
    return output


def _resolved_manifest_output(manifest: Mapping[str, Any], manifest_path: Path) -> Path | None:
    supplied = str(manifest.get("output") or "").strip()
    if not supplied:
        return None
    path = Path(supplied)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _counter(values: Sequence[Any]) -> dict[str, int]:
    counts = Counter(str(value) if value not in (None, "") else "<missing>" for value in values)
    return dict(sorted(counts.items()))


def _proposal_lane_distribution(
    rows: Sequence[Mapping[str, Any]], owner: str, lane: str
) -> dict[str, int]:
    values: list[str] = []
    for row in rows:
        proposal = _mapping(_mapping(row.get(owner)).get("proposal"))
        actions = [str(value) for value in _list(proposal.get(lane)) if str(value)]
        values.append("+".join(actions) if actions else "<empty>")
    return _counter(values)


def _rage_bucket(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    rage = float(value)
    if rage < 20:
        return "00-19"
    if rage < 40:
        return "20-39"
    if rage < 60:
        return "40-59"
    if rage < 80:
        return "60-79"
    return "80-100"


def _health_bucket(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    health = float(value)
    if health <= 20:
        return "00-20"
    if health <= 35:
        return "21-35"
    if health <= 50:
        return "36-50"
    return "51-100"


def _nonempty_exact_sink(row: Mapping[str, Any]) -> bool:
    decision_id = str(row.get("decisionId") or "")
    actions = _list(_mapping(row.get("expertActual")).get("sinkActions"))
    return bool(actions) and all(
        isinstance(action, Mapping) and action.get("decisionId") == decision_id
        for action in actions
    )


def _nonempty_exact_trace(row: Mapping[str, Any]) -> bool:
    links = _list(_mapping(row.get("expertActual")).get("expertTraceLinks"))
    return bool(links) and any(
        isinstance(link, Mapping)
        and link.get("completed") is True
        and link.get("errored") is not True
        for link in links
    )


def _lower_confidence(value: Any) -> bool:
    return "lower_confidence" in str(value or "").casefold()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _integer(value: Any) -> int | None:
    numeric = _finite_number(value)
    if numeric is None or not numeric.is_integer():
        return None
    return int(numeric)


def _expected_spell_ids(action: Mapping[str, Any]) -> set[int]:
    return {
        int(value)
        for value in _list(action.get("expectedSpellIds"))
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value).is_integer()
    }


def _event_matches_action(
    event: Mapping[str, Any], action: Mapping[str, Any]
) -> bool:
    spell_id = _integer(event.get("spellID"))
    return spell_id is not None and spell_id in _expected_spell_ids(action)


def _event_token_audit(
    decision_id: str,
    actions: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Validate the v4 decision/sink/generation token on one causal event."""

    candidates = [action for action in actions if _event_matches_action(event, action)]
    if not candidates:
        return False, "event_not_matched_to_outcome_action"
    if str(event.get("actionDecisionId") or "").strip() != decision_id:
        return False, "event_action_decision_id_missing_or_mismatched"
    event_sink_seq = _integer(event.get("sinkSeq"))
    if event_sink_seq is None:
        return False, "event_sink_seq_missing"
    event_generation = str(event.get("generation") or "").strip()
    if not event_generation:
        return False, "event_generation_missing"
    for action in candidates:
        if (
            _integer(action.get("sinkSeq")) == event_sink_seq
            and str(action.get("generation") or "").strip() == event_generation
        ):
            return True, None
    return False, "event_sink_generation_mismatched"


def audit_v4_outcome_integrity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the exact sink/action/event lifecycle for one v4 row.

    This is intentionally stricter than merely observing a terminal flag.  It
    verifies a sink/action token bijection, monotonic causal clocks, per-action
    event counts and damage, row aggregates, and decision-state anchors.  The
    result is diagnostic rather than an exception so the acceptance report can
    explain every rejected row.  Pre-v4 rows are left to the legacy audit.
    """

    sample = _mapping(row.get("shadowSample"))
    contract_version = _integer(sample.get("contractVersion"))
    if contract_version != 4:
        return {
            "applicable": False,
            "passed": True,
            "reasons": [],
            "sink_count": 0,
            "action_count": 0,
            "causal_event_count": 0,
        }

    reasons: set[str] = set()
    decision_id = str(row.get("decisionId") or "").strip()
    actual = _mapping(row.get("expertActual"))
    outcome = _mapping(row.get("observedActualOutcome"))
    state = _mapping(row.get("state"))

    def number_equal(left: Any, right: Any) -> bool:
        left_number = _finite_number(left)
        right_number = _finite_number(right)
        return (
            left_number is not None
            and right_number is not None
            and left_number == right_number
        )

    def nonnegative_count(container: Mapping[str, Any], key: str) -> int | None:
        value = _integer(container.get(key))
        if value is None or value < 0:
            reasons.add(f"invalid_{key}")
            return None
        return value

    sink_by_token: dict[tuple[int, str], Mapping[str, Any]] = {}
    for sink in [_mapping(value) for value in _list(actual.get("sinkActions"))]:
        sink_seq = _integer(sink.get("seq"))
        generation = str(sink.get("generation") or "").strip()
        token = (sink_seq or 0, generation)
        if (
            sink_seq is None
            or sink_seq <= 0
            or not generation
            or str(sink.get("decisionId") or "").strip() != decision_id
        ):
            reasons.add("invalid_sink_identity")
            continue
        if token in sink_by_token:
            reasons.add("duplicate_sink_token")
        sink_by_token[token] = sink
        if _integer(sink.get("issuedOrdinal")) is None:
            reasons.add("sink_issued_ordinal_missing")
        if _finite_number(sink.get("issuedAt")) is None:
            reasons.add("sink_issued_time_missing")
        if not str(sink.get("api") or "").strip():
            reasons.add("sink_api_missing")

    action_by_token: dict[tuple[int, str], Mapping[str, Any]] = {}
    actions = [_mapping(value) for value in _list(outcome.get("actions"))]
    for action in actions:
        sink_seq = _integer(action.get("sinkSeq"))
        generation = str(action.get("generation") or "").strip()
        token = (sink_seq or 0, generation)
        if (
            sink_seq is None
            or sink_seq <= 0
            or not generation
            or str(action.get("decisionId") or "").strip() != decision_id
        ):
            reasons.add("invalid_outcome_action_identity")
            continue
        if token in action_by_token:
            reasons.add("duplicate_outcome_action_token")
        action_by_token[token] = action
        sink = sink_by_token.get(token)
        if sink is None:
            reasons.add("outcome_action_without_sink")
            continue
        if action.get("api") != sink.get("api"):
            reasons.add("sink_action_api_mismatch")
        if _integer(action.get("issuedOrdinal")) != _integer(
            sink.get("issuedOrdinal")
        ):
            reasons.add("sink_action_issued_ordinal_mismatch")
        if not number_equal(action.get("issuedAt"), sink.get("issuedAt")):
            reasons.add("sink_action_issued_time_mismatch")
        if action.get("clientAccepted") is not True:
            reasons.add("outcome_action_not_client_accepted")

    if set(sink_by_token) != set(action_by_token):
        reasons.add("sink_action_token_bijection_failed")

    events_by_token: dict[tuple[int, str], list[Mapping[str, Any]]] = {
        token: [] for token in action_by_token
    }
    causal_events = [_mapping(value) for value in _list(outcome.get("compactEvents"))]
    previous_ordinal: int | None = None
    previous_time: float | None = None
    seen_ordinals: set[int] = set()
    for event in causal_events:
        event_name = str(event.get("event") or "").strip()
        if event_name not in CAUSAL_COMPACT_EVENTS:
            reasons.add("unsupported_compact_event")
            continue
        ordinal = _integer(event.get("causalOrdinal"))
        event_time = _finite_number(event.get("time"))
        if ordinal is None or ordinal <= 0:
            reasons.add("compact_event_causal_ordinal_missing")
        else:
            if ordinal in seen_ordinals:
                reasons.add("duplicate_compact_event_causal_ordinal")
            if previous_ordinal is not None and ordinal <= previous_ordinal:
                reasons.add("compact_event_causal_ordinal_nonmonotonic")
            seen_ordinals.add(ordinal)
            previous_ordinal = ordinal
        if event_time is None:
            reasons.add("compact_event_time_missing")
        else:
            if previous_time is not None and event_time < previous_time:
                reasons.add("compact_event_time_nonmonotonic")
            previous_time = event_time
        token = (
            _integer(event.get("sinkSeq")) or 0,
            str(event.get("generation") or "").strip(),
        )
        if (
            str(event.get("actionDecisionId") or "").strip() != decision_id
            or token not in action_by_token
            or _integer(event.get("actionSinkSeq")) not in {None, token[0]}
            or str(event.get("actionGeneration") or token[1]).strip() != token[1]
        ):
            reasons.add("compact_event_action_token_mismatch")
            continue
        action = action_by_token[token]
        if not _event_matches_action(event, action):
            reasons.add("compact_event_spell_mismatch")
        issued_ordinal = _integer(action.get("issuedOrdinal"))
        if ordinal is not None and issued_ordinal is not None and ordinal <= issued_ordinal:
            reasons.add("compact_event_not_after_sink_issue")
        events_by_token[token].append(event)

    for token, action in action_by_token.items():
        token_events = events_by_token.get(token, [])
        cast_events = [
            event for event in token_events if event.get("event") == "SPELL_CAST_EVENT"
        ]
        successful_casts = [
            event for event in cast_events if event.get("castSucceeded") is True
        ]
        rejected_casts = [
            event for event in cast_events if event.get("castSucceeded") is not True
        ]
        go_events = [
            event for event in token_events if event.get("event") == "SPELL_GO_SELF"
        ]
        damage_events = [
            event
            for event in token_events
            if event.get("event") in {"SPELL_DAMAGE_EVENT_SELF", "AUTO_ATTACK_SELF"}
            and _finite_number(event.get("amount")) is not None
        ]
        miss_events = [
            event for event in token_events if event.get("event") == "SPELL_MISS_SELF"
        ]
        failed_events = [
            event for event in token_events if event.get("event") == "SPELL_FAILED_SELF"
        ]
        queue_events = [
            event for event in token_events if event.get("event") == "SPELL_QUEUE_EVENT"
        ]
        queue_pops = [event for event in queue_events if event.get("queueEventCode") == 1]

        expected_counts = {
            "clientCastCount": len(cast_events),
            "serverGoCount": len(go_events),
            "resultCount": len(damage_events) + len(miss_events),
            "missCount": len(miss_events),
            "failedCount": len(failed_events),
            "queueEventCount": len(queue_events),
            "queuePopCount": len(queue_pops),
        }
        for field, expected in expected_counts.items():
            observed = nonnegative_count(action, field)
            if observed is not None and observed != expected:
                reasons.add(f"action_{field}_mismatch")
        action_damage = _finite_number(action.get("damage"))
        event_damage = sum(float(event.get("amount")) for event in damage_events)
        if action_damage is None or action_damage < 0 or action_damage != event_damage:
            reasons.add("action_damage_mismatch")

        if not successful_casts:
            reasons.add("action_successful_client_cast_missing")
        else:
            accepted_event = successful_casts[-1]
            if _integer(action.get("clientAcceptedOrdinal")) != _integer(
                accepted_event.get("causalOrdinal")
            ):
                reasons.add("action_client_accepted_ordinal_mismatch")
            if not number_equal(action.get("clientAcceptedAt"), accepted_event.get("time")):
                reasons.add("action_client_accepted_time_mismatch")
            if _integer(action.get("clientCastOrdinal")) != _integer(
                accepted_event.get("causalOrdinal")
            ):
                reasons.add("action_client_cast_ordinal_mismatch")
        if bool(rejected_casts) != (action.get("clientRejected") is True):
            reasons.add("action_client_rejected_flag_mismatch")
        if rejected_casts and _integer(action.get("clientRejectedOrdinal")) != _integer(
            rejected_casts[-1].get("causalOrdinal")
        ):
            reasons.add("action_client_rejected_ordinal_mismatch")
        if go_events:
            if _integer(action.get("serverGoOrdinal")) != _integer(
                go_events[-1].get("causalOrdinal")
            ):
                reasons.add("action_server_go_ordinal_mismatch")
            if not number_equal(action.get("serverGoAt"), go_events[-1].get("time")):
                reasons.add("action_server_go_time_mismatch")
        result_events = damage_events + miss_events
        result_ordinals = sorted(
            ordinal
            for ordinal in (_integer(event.get("causalOrdinal")) for event in result_events)
            if ordinal is not None
        )
        if result_ordinals and (
            _integer(action.get("resultFirstOrdinal")) != result_ordinals[0]
            or _integer(action.get("resultLastOrdinal")) != result_ordinals[-1]
        ):
            reasons.add("action_result_ordinal_mismatch")
        token_ordinals = [
            ordinal
            for ordinal in (_integer(event.get("causalOrdinal")) for event in token_events)
            if ordinal is not None
        ]
        accepted_ordinal = (
            _integer(successful_casts[-1].get("causalOrdinal"))
            if successful_casts
            else None
        )
        first_go_ordinal = (
            _integer(go_events[0].get("causalOrdinal")) if go_events else None
        )
        last_go_ordinal = (
            _integer(go_events[-1].get("causalOrdinal")) if go_events else None
        )
        if (
            successful_casts
            and go_events
            and (
                accepted_ordinal is None
                or first_go_ordinal is None
                or accepted_ordinal >= first_go_ordinal
            )
        ):
            reasons.add("action_cast_go_lifecycle_nonmonotonic")
        if (
            go_events
            and result_ordinals
            and (last_go_ordinal is None or last_go_ordinal >= result_ordinals[0])
        ):
            reasons.add("action_go_result_lifecycle_nonmonotonic")
        token_times = [
            value
            for value in (_finite_number(event.get("time")) for event in token_events)
            if value is not None
        ]
        if token_times and not number_equal(action.get("lastEventAt"), max(token_times)):
            reasons.add("action_last_event_time_mismatch")

    aggregate_fields = {
        "damage": sum(_finite_number(action.get("damage")) or 0 for action in actions),
        "resultCount": sum(_integer(action.get("resultCount")) or 0 for action in actions),
        "missCount": sum(_integer(action.get("missCount")) or 0 for action in actions),
        "failedCount": sum(_integer(action.get("failedCount")) or 0 for action in actions),
        "serverGoCount": sum(_integer(action.get("serverGoCount")) or 0 for action in actions),
        "queueEventCount": sum(_integer(action.get("queueEventCount")) or 0 for action in actions),
        "queuePopCount": sum(_integer(action.get("queuePopCount")) or 0 for action in actions),
    }
    for field, expected in aggregate_fields.items():
        observed = _finite_number(outcome.get(field))
        if observed is None or observed != float(expected):
            reasons.add(f"outcome_{field}_mismatch")
    if _integer(outcome.get("acceptedActionCount")) != len(actions):
        reasons.add("outcome_accepted_action_count_mismatch")
    if _integer(outcome.get("materializedActionCount")) != len(actions):
        reasons.add("outcome_materialized_action_count_mismatch")
    if _integer(actual.get("clientAcceptedActionCount")) != len(actions):
        reasons.add("expert_actual_client_accepted_count_mismatch")
    if _integer(outcome.get("unsupportedSinkCount")) != 0 or _list(
        outcome.get("unsupportedSinks")
    ):
        reasons.add("outcome_contains_unsupported_sink")
    if not number_equal(state.get("rage"), outcome.get("rageAtDecision")):
        reasons.add("outcome_rage_decision_anchor_mismatch")
    if not number_equal(
        state.get("targetHealth"), outcome.get("targetHealthAtDecision")
    ):
        reasons.add("outcome_target_health_decision_anchor_mismatch")
    event_times = [
        value
        for value in (_finite_number(event.get("time")) for event in causal_events)
        if value is not None
    ]
    if event_times and not number_equal(outcome.get("lastEventAt"), max(event_times)):
        reasons.add("outcome_last_event_time_mismatch")

    return {
        "applicable": True,
        "passed": not reasons,
        "reasons": sorted(reasons),
        "sink_count": len(sink_by_token),
        "action_count": len(action_by_token),
        "causal_event_count": len(causal_events),
    }


def _row_timeline(row: Mapping[str, Any], journal_index: int) -> dict[str, Any]:
    """Return only causal evidence serialized in one compact journal row.

    A sink/trace link proves that Cat2 reached a client API.  It does not prove
    that a later family-matched server event belongs to that invocation.  The
    latter claim needs a successful client-cast acceptance anchor and an
    unambiguous cross-row timeline.
    """

    decision_id = str(row.get("decisionId") or "").strip()
    sample = _mapping(row.get("shadowSample"))
    outcome = _mapping(row.get("observedActualOutcome"))
    contract_version = _integer(sample.get("contractVersion"))
    if contract_version is None:
        contract_version = _integer(outcome.get("causalContractVersion"))
    state = _mapping(row.get("state"))
    actions = [
        _mapping(action)
        for action in _list(outcome.get("actions"))
        if str(_mapping(action).get("family") or "").strip()
    ]
    families = {
        str(action.get("family") or "").strip() for action in actions
    }
    events = [_mapping(event) for event in _list(outcome.get("compactEvents"))]
    outcome_integrity = audit_v4_outcome_integrity(row)

    captured_at = _finite_number(row.get("capturedAt"))
    armed_at = _finite_number(sample.get("armedAt"))
    bound_at = _finite_number(sample.get("boundAt"))
    confirmed_at = _finite_number(sample.get("confirmedAt"))
    materialized_at = _finite_number(
        _mapping(row.get("expertActual")).get("materializedAt")
    )
    last_event_at = _finite_number(outcome.get("lastEventAt"))
    event_times = [_finite_number(event.get("time")) for event in events]
    valid_event_times = [value for value in event_times if value is not None]
    end_candidates = [
        value
        for value in (confirmed_at, materialized_at, last_event_at, *valid_event_times)
        if value is not None
    ]
    end_at = max(end_candidates) if end_candidates else None

    timeline_errors: list[str] = []
    timeline_errors.extend(outcome_integrity["reasons"])
    if not decision_id:
        timeline_errors.append("missing_decision_id")
    if not families:
        timeline_errors.append("missing_outcome_action_family")
    if captured_at is None or bound_at is None or confirmed_at is None or end_at is None:
        timeline_errors.append("missing_causal_timestamp")
    if any(value is None for value in event_times):
        timeline_errors.append("compact_event_missing_timestamp")
    if (
        captured_at is not None
        and armed_at is not None
        and armed_at != captured_at
    ):
        timeline_errors.append("armed_capture_timestamp_mismatch")
    if (
        captured_at is not None
        and bound_at is not None
        and confirmed_at is not None
        and end_at is not None
        and not (captured_at <= bound_at <= confirmed_at <= end_at)
    ):
        timeline_errors.append("nonmonotonic_causal_timestamps")
    if captured_at is not None and end_at is not None and any(
        value is not None and not (captured_at <= value <= end_at)
        for value in event_times
    ):
        timeline_errors.append("compact_event_outside_causal_window")

    anchored_families: set[str] = set()
    client_cast_types_by_family: dict[str, set[int]] = {
        family: set() for family in families
    }
    for action in actions:
        family = str(action.get("family") or "").strip()
        expected_ids = {
            int(value)
            for value in _list(action.get("expectedSpellIds"))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        for event in events:
            event_spell_id = event.get("spellID")
            event_time = _finite_number(event.get("time"))
            if (
                event.get("event") == "SPELL_CAST_EVENT"
                and event.get("castSucceeded") is True
                and isinstance(event_spell_id, (int, float))
                and not isinstance(event_spell_id, bool)
                and int(event_spell_id) in expected_ids
                and captured_at is not None
                and end_at is not None
                and event_time is not None
                and captured_at <= event_time <= end_at
            ):
                anchored_families.add(family)
                cast_type = _integer(event.get("castType"))
                if cast_type is not None:
                    client_cast_types_by_family[family].add(cast_type)

    causal_events = [
        event
        for event in events
        if str(event.get("event") or "").strip() in CAUSAL_COMPACT_EVENTS
    ]
    token_errors: list[dict[str, Any]] = []
    tokened_event_count = 0
    for event_index, event in enumerate(causal_events):
        tokened, token_error = _event_token_audit(decision_id, actions, event)
        tokened_event_count += int(tokened)
        if not tokened:
            token_errors.append(
                {
                    "event_index": event_index,
                    "event": event.get("event"),
                    "time": _finite_number(event.get("time")),
                    "reason": token_error,
                }
            )
    all_causal_events_tokened = bool(causal_events) and tokened_event_count == len(
        causal_events
    )
    if contract_version is not None and contract_version >= 4:
        if not all_causal_events_tokened:
            timeline_errors.append("causal_v4_event_token_incomplete")

    sample_family = str(sample.get("actionFamily") or "").strip()
    if sample_family and sample_family not in families:
        timeline_errors.append("sample_outcome_family_mismatch")

    return {
        "journal_index": journal_index,
        "decision_id": decision_id,
        "contract_version": contract_version,
        "families": families,
        "actions": actions,
        "events": events,
        "state": state,
        "captured_at": captured_at,
        "bound_at": bound_at,
        "confirmed_at": confirmed_at,
        "end_at": end_at,
        "timeline_errors": sorted(set(timeline_errors)),
        "client_cast_anchored": bool(families) and anchored_families == families,
        "missing_client_cast_families": sorted(families - anchored_families),
        "client_cast_types_by_family": {
            family: sorted(cast_types)
            for family, cast_types in sorted(client_cast_types_by_family.items())
        },
        "causal_event_count": len(causal_events),
        "tokened_causal_event_count": tokened_event_count,
        "all_causal_events_tokened": all_causal_events_tokened,
        "causal_event_token_errors": token_errors,
        "outcome_integrity": outcome_integrity,
    }


def _targets_can_overlap(
    left: Mapping[str, Any], right: Mapping[str, Any], shared_families: set[str]
) -> bool:
    if shared_families & TARGET_AGNOSTIC_FAMILIES:
        return True
    left_target = str(_mapping(left.get("state")).get("targetGUID") or "").strip()
    right_target = str(_mapping(right.get("state")).get("targetGUID") or "").strip()
    return not left_target or not right_target or left_target == right_target


def _event_fingerprint(event: Mapping[str, Any]) -> tuple[Any, ...] | None:
    event_time = _finite_number(event.get("time"))
    event_name = str(event.get("event") or "").strip()
    if not event_name or event_time is None:
        return None
    causal_ordinal = _integer(event.get("causalOrdinal"))
    sequence = _integer(event.get("sequence"))
    if causal_ordinal is not None:
        return ("causalOrdinal", causal_ordinal)
    if sequence is not None:
        return ("sequence", sequence)
    return (
        "raw",
        event_name,
        event_time,
        event.get("spellID"),
        event.get("sourceGUID"),
        event.get("targetGUID"),
        event.get("queueEventCode"),
        event.get("castKind"),
        event.get("castType"),
        event.get("castSucceeded"),
    )


def _event_group_strictly_precedes(
    left_events: Sequence[Mapping[str, Any]],
    right_events: Sequence[Mapping[str, Any]],
) -> bool | None:
    """Order two lifecycle event groups, preferring the v4 causal clock.

    Turtle's ``GetTime`` export is millisecond-quantized, so two consecutive
    callbacks can legitimately share a timestamp.  Contract-v4's monotonic
    ``causalOrdinal`` is the authoritative ordering signal when every event
    in both groups carries it; legacy rows fall back to strict timestamps.
    ``None`` means that neither clock is complete enough to decide.
    """

    if not left_events or not right_events:
        return None
    left_ordinals = [_integer(event.get("causalOrdinal")) for event in left_events]
    right_ordinals = [_integer(event.get("causalOrdinal")) for event in right_events]
    if None not in left_ordinals and None not in right_ordinals:
        return max(left_ordinals) < min(right_ordinals)
    left_times = [_finite_number(event.get("time")) for event in left_events]
    right_times = [_finite_number(event.get("time")) for event in right_events]
    if None not in left_times and None not in right_times:
        return max(left_times) < min(right_times)
    return None


def _journal_causal_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    timelines = [_row_timeline(row, index) for index, row in enumerate(rows)]
    reasons_by_id: dict[str, set[str]] = {
        str(timeline["decision_id"]): set(timeline["timeline_errors"])
        for timeline in timelines
    }

    for timeline in timelines:
        if not timeline["client_cast_anchored"]:
            reasons_by_id[timeline["decision_id"]].add(
                "missing_successful_client_cast_acceptance_anchor"
            )

    capture_inversions: list[dict[str, Any]] = []
    for left_index, left in enumerate(timelines):
        for right in timelines[left_index + 1 :]:
            if (
                left["captured_at"] is not None
                and right["captured_at"] is not None
                and right["captured_at"] < left["captured_at"]
            ):
                capture_inversions.append(
                    {
                        "earlier_journal_decision_id": left["decision_id"],
                        "later_journal_decision_id": right["decision_id"],
                        "earlier_journal_captured_at": left["captured_at"],
                        "later_journal_captured_at": right["captured_at"],
                    }
                )

    overlaps: list[dict[str, Any]] = []
    unresolved_overlaps: list[dict[str, Any]] = []
    parseable = [
        timeline
        for timeline in timelines
        if not timeline["timeline_errors"]
        and timeline["captured_at"] is not None
        and timeline["bound_at"] is not None
        and timeline["end_at"] is not None
    ]
    ordered = sorted(
        parseable,
        key=lambda timeline: (timeline["captured_at"], timeline["journal_index"]),
    )
    overlap_keys: set[frozenset[str]] = set()
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            if right["captured_at"] > left["end_at"]:
                break
            shared = set(left["families"]) & set(right["families"])
            if not shared or not _targets_can_overlap(left, right, shared):
                continue
            before_bound = right["captured_at"] <= left["bound_at"]
            inversion = right["journal_index"] < left["journal_index"]
            token_disambiguated = bool(left["all_causal_events_tokened"])
            token_disambiguated = token_disambiguated and bool(
                right["all_causal_events_tokened"]
            )
            detail = {
                "earlier_decision_id": left["decision_id"],
                "later_decision_id": right["decision_id"],
                "families": sorted(shared),
                "earlier_captured_at": left["captured_at"],
                "earlier_bound_at": left["bound_at"],
                "earlier_end_at": left["end_at"],
                "later_captured_at": right["captured_at"],
                "later_before_earlier_bound": before_bound,
                "journal_capture_order_inverted": inversion,
                "token_disambiguated": token_disambiguated,
            }
            overlaps.append(detail)
            overlap_keys.add(frozenset({left["decision_id"], right["decision_id"]}))
            if token_disambiguated:
                continue
            unresolved_overlaps.append(detail)
            reasons_by_id[left["decision_id"]].add(
                "same_family_outcome_window_overlap"
            )
            reasons_by_id[right["decision_id"]].add(
                "same_family_outcome_window_overlap"
            )
            if before_bound:
                reasons_by_id[left["decision_id"]].add(
                    "later_capture_precedes_family_bound"
                )
                reasons_by_id[right["decision_id"]].add(
                    "captured_before_prior_family_bound"
                )
            if inversion:
                reasons_by_id[left["decision_id"]].add(
                    "journal_capture_order_inversion_with_overlap"
                )
                reasons_by_id[right["decision_id"]].add(
                    "journal_capture_order_inversion_with_overlap"
                )

    queue_diagnostics: list[dict[str, Any]] = []
    next_swing_paths: list[dict[str, Any]] = []
    queue_timelines = [
        timeline
        for timeline in timelines
        if set(timeline["families"]) & NEXT_SWING_FAMILIES
    ]
    for timeline in queue_timelines:
        decision_id = timeline["decision_id"]
        queue_families = set(timeline["families"]) & NEXT_SWING_FAMILIES
        events = timeline["events"]
        code_zero = [
            event
            for event in events
            if event.get("event") == "SPELL_QUEUE_EVENT"
            and event.get("queueEventCode") == 0
        ]
        code_one = [
            event
            for event in events
            if event.get("event") == "SPELL_QUEUE_EVENT"
            and event.get("queueEventCode") == 1
        ]
        state_queue = str(_mapping(timeline.get("state")).get("queuedSwing") or "").strip()
        expected_state_queues = {
            "HEROIC_STRIKE" if family == "heroic_strike" else "CLEAVE"
            for family in queue_families
        }
        family_actions = [
            action
            for action in timeline["actions"]
            if str(action.get("family") or "").strip() in queue_families
        ]
        all_client_cast_events = [
            event
            for event in events
            if event.get("event") == "SPELL_CAST_EVENT"
            and any(_event_matches_action(event, action) for action in family_actions)
        ]
        successful_client_cast_events = [
            event
            for event in all_client_cast_events
            if event.get("castSucceeded") is True
        ]
        precursor_client_cast_events = [
            event
            for event in all_client_cast_events
            if event.get("castSucceeded") is not True
        ]
        go_events = [
            event
            for event in events
            if event.get("event") == "SPELL_GO_SELF"
            and any(_event_matches_action(event, action) for action in family_actions)
        ]
        result_events = [
            event
            for event in events
            if event.get("event") in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
            and any(_event_matches_action(event, action) for action in family_actions)
        ]
        client_cast_count = sum(
            _integer(action.get("clientCastCount")) or 0
            for action in family_actions
        )
        queue_event_count = sum(
            _integer(action.get("queueEventCount")) or 0
            for action in family_actions
        )
        queue_pop_count = sum(
            _integer(action.get("queuePopCount")) or 0
            for action in family_actions
        )
        client_accepted = bool(family_actions) and all(
            action.get("clientAccepted") is True for action in family_actions
        )
        cast_types = {
            cast_type
            for family in queue_families
            for cast_type in timeline["client_cast_types_by_family"].get(family, [])
        }
        has_queue_lifecycle = bool(code_zero or code_one) or queue_event_count > 0
        preexisting_queue = state_queue in expected_state_queues
        immediate_path = (
            not has_queue_lifecycle
            and not preexisting_queue
            and client_accepted
            and client_cast_count == len(family_actions)
            and len(all_client_cast_events) == len(family_actions)
            and len(successful_client_cast_events) == len(family_actions)
            and queue_event_count == 0
            and queue_pop_count == 0
            and cast_types == {2}
        )
        row_reasons: list[str] = []
        if not timeline["client_cast_anchored"]:
            row_reasons.append("next_swing_missing_client_acceptance")
        if len(successful_client_cast_events) != len(family_actions):
            row_reasons.append("next_swing_client_acceptance_count_mismatch")
        if client_cast_count != len(all_client_cast_events):
            row_reasons.append("next_swing_client_cast_count_mismatch")
        if cast_types and cast_types != {2}:
            row_reasons.append("next_swing_client_cast_type_mismatch")
        if not go_events or not result_events:
            row_reasons.append("next_swing_terminal_chain_incomplete")
        if not immediate_path:
            if not has_queue_lifecycle and not preexisting_queue:
                row_reasons.append("immediate_next_swing_client_metadata_incomplete")
            if not code_zero:
                row_reasons.append("queued_next_swing_missing_code0_queue")
            if not code_one:
                row_reasons.append("queued_next_swing_missing_code1_pop")
            if preexisting_queue and not timeline["all_causal_events_tokened"]:
                row_reasons.append("preexisting_queue_owner_unknown")
            if len(code_one) > 1:
                row_reasons.append("queued_next_swing_multiple_code1_pops")
            if len(code_zero) > 1:
                row_reasons.append("queued_next_swing_multiple_code0_events")
            if queue_event_count != len(code_zero) + len(code_one):
                row_reasons.append("queued_next_swing_event_count_mismatch")
            if queue_pop_count != len(code_one):
                row_reasons.append("queued_next_swing_pop_count_mismatch")
            lifecycle_groups = (
                code_zero,
                code_one,
                successful_client_cast_events,
                go_events,
                result_events,
            )
            lifecycle_order = [
                _event_group_strictly_precedes(left, right)
                for left, right in zip(lifecycle_groups, lifecycle_groups[1:])
            ]
            if None not in lifecycle_order and not all(lifecycle_order):
                row_reasons.append("queued_next_swing_lifecycle_nonmonotonic")
            precursor_before_queue = _event_group_strictly_precedes(
                precursor_client_cast_events, code_zero
            )
            if precursor_before_queue is False:
                row_reasons.append("queued_next_swing_precursor_cast_not_before_code0")
        path = "immediate_client_acceptance" if immediate_path else "queued_lifecycle"
        for reason in row_reasons:
            reasons_by_id[decision_id].add(reason)
        next_swing_paths.append(
            {
                "decision_id": decision_id,
                "families": sorted(queue_families),
                "path": path,
                "state_queued_swing": state_queue or None,
                "client_cast_count": client_cast_count,
                "client_cast_event_count": len(all_client_cast_events),
                "successful_client_cast_event_count": len(
                    successful_client_cast_events
                ),
                "client_cast_types": sorted(cast_types),
                "code0_count": len(code_zero),
                "code1_count": len(code_one),
                "token_complete": timeline["all_causal_events_tokened"],
            }
        )
        if row_reasons:
            queue_diagnostics.append(
                {
                    "decision_id": decision_id,
                    "families": sorted(queue_families),
                    "state_queued_swing": state_queue or None,
                    "code0_count": len(code_zero),
                    "code1_count": len(code_one),
                    "reasons": sorted(row_reasons),
                }
            )

    cross_row_queue_events: list[dict[str, Any]] = []
    unresolved_cross_row_queue_events: list[dict[str, Any]] = []
    for owner in queue_timelines:
        pop_times = [
            _finite_number(event.get("time"))
            for event in owner["events"]
            if event.get("event") == "SPELL_QUEUE_EVENT"
            and event.get("queueEventCode") == 1
        ]
        for other in queue_timelines:
            if owner is other or not (set(owner["families"]) & set(other["families"])):
                continue
            go_times = [
                _finite_number(event.get("time"))
                for event in other["events"]
                if event.get("event") == "SPELL_GO_SELF"
            ]
            result_times = [
                _finite_number(event.get("time"))
                for event in other["events"]
                if event.get("event") in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
            ]
            go_times = [value for value in go_times if value is not None]
            result_times = [value for value in result_times if value is not None]
            if not go_times or not result_times:
                continue
            go_at = min(go_times)
            result_at = max(result_times)
            for pop_at in (value for value in pop_times if value is not None):
                if go_at <= pop_at <= result_at:
                    detail = {
                        "pop_owner_decision_id": owner["decision_id"],
                        "materialized_decision_id": other["decision_id"],
                        "pop_at": pop_at,
                        "other_go_at": go_at,
                        "other_result_at": result_at,
                        "token_disambiguated": bool(
                            owner["all_causal_events_tokened"]
                            and other["all_causal_events_tokened"]
                        ),
                    }
                    if detail not in cross_row_queue_events:
                        cross_row_queue_events.append(detail)
                    if not detail["token_disambiguated"]:
                        unresolved_cross_row_queue_events.append(detail)
                        reasons_by_id[owner["decision_id"]].add(
                            "queue_pop_inside_other_action_result_window"
                        )
                        reasons_by_id[other["decision_id"]].add(
                            "other_queue_pop_inside_action_result_window"
                        )

    fingerprints: dict[tuple[Any, ...], list[str]] = {}
    for timeline in timelines:
        for event in timeline["events"]:
            fingerprint = _event_fingerprint(event)
            if fingerprint is not None:
                fingerprints.setdefault(fingerprint, []).append(timeline["decision_id"])
    duplicate_events: list[dict[str, Any]] = []
    for fingerprint, decision_ids in fingerprints.items():
        unique_ids = sorted(set(decision_ids))
        if len(unique_ids) < 2:
            continue
        duplicate_events.append(
            {"fingerprint": list(fingerprint), "decision_ids": unique_ids}
        )
        for decision_id in unique_ids:
            reasons_by_id[decision_id].add("duplicate_compact_event_across_rows")

    ambiguous_ids = sorted(
        decision_id for decision_id, reasons in reasons_by_id.items() if reasons
    )
    return {
        "timelines": timelines,
        "reasons_by_decision_id": {
            decision_id: sorted(reasons)
            for decision_id, reasons in sorted(reasons_by_id.items())
            if reasons
        },
        "ambiguous_decision_ids": ambiguous_ids,
        "timeline_parseable_rows": sum(
            not timeline["timeline_errors"] for timeline in timelines
        ),
        "successful_client_cast_anchor_rows": sum(
            timeline["client_cast_anchored"] for timeline in timelines
        ),
        "causal_v4_rows": sum(
            timeline["contract_version"] is not None
            and timeline["contract_version"] >= 4
            for timeline in timelines
        ),
        "causal_v4_token_complete_rows": sum(
            timeline["contract_version"] is not None
            and timeline["contract_version"] >= 4
            and timeline["all_causal_events_tokened"]
            for timeline in timelines
        ),
        "causal_v4_outcome_integrity_rows": sum(
            timeline["outcome_integrity"]["applicable"]
            for timeline in timelines
        ),
        "causal_v4_outcome_integrity_pass_rows": sum(
            timeline["outcome_integrity"]["applicable"]
            and timeline["outcome_integrity"]["passed"]
            for timeline in timelines
        ),
        "capture_inversions": capture_inversions,
        "same_family_overlaps": overlaps,
        "unresolved_same_family_overlaps": unresolved_overlaps,
        "same_family_overlap_component_count": len(overlap_keys),
        "queue_diagnostics": queue_diagnostics,
        "next_swing_paths": next_swing_paths,
        "cross_row_queue_events": cross_row_queue_events,
        "unresolved_cross_row_queue_events": unresolved_cross_row_queue_events,
        "duplicate_events": duplicate_events,
    }


def _gate(passed: bool, reasons: list[str], evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "rejection_reasons": reasons,
        "evidence": dict(evidence),
    }


def build_shadow_pair_acceptance(
    journal_path: str | Path = DEFAULT_JOURNAL,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    expected_pairs: int = 60,
    export_session_id: str | None = None,
) -> dict[str, Any]:
    """Audit the latest (or requested) export session in the compact journal."""

    if expected_pairs <= 0:
        raise ShadowPairAcceptanceError("expected_pairs must be positive")
    journal = Path(journal_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    before_hashes = {
        "journal": _file_sha256(journal, "journal"),
        "manifest": _file_sha256(manifest_file, "manifest"),
    }
    manifest = _read_json(manifest_file, "manifest")
    rows = _read_journal(journal)
    after_hashes = {
        "journal": _file_sha256(journal, "journal"),
        "manifest": _file_sha256(manifest_file, "manifest"),
    }
    if after_hashes != before_hashes:
        raise ShadowPairAcceptanceError(
            "journal or manifest changed while the acceptance audit was reading it"
        )

    requested_session = str(export_session_id or "").strip()
    selected_session = requested_session or _session_id(rows[-1])
    if not selected_session:
        raise ShadowPairAcceptanceError(
            "the requested/latest journal row has no shadowSample.exportSessionId"
        )
    selected = [row for row in rows if _session_id(row) == selected_session]

    manifest_pairs = manifest.get("journal_pair_total")
    manifest_pairs = (
        manifest_pairs
        if isinstance(manifest_pairs, int) and not isinstance(manifest_pairs, bool)
        else None
    )
    journal_identities = [_identity(row) for row in rows]
    selected_identities = [_identity(row) for row in selected]
    declared_identities = _manifest_identities(manifest)
    expected_source = f"brainofcat-shadow-session:{selected_session}"

    unconfirmed = 0
    candidate_executed = 0
    missing_session = 0
    selected_provenance_mismatch = 0
    for row in selected:
        sample = _mapping(row.get("shadowSample"))
        candidate = _mapping(row.get("candidateShadow"))
        if not (
            row.get("schemaVersion") == EXPECTED_SCHEMA_VERSION
            and sample.get("status") == "confirmed"
            and sample.get("materialized") is True
            and sample.get("counted") is True
        ):
            unconfirmed += 1
        if candidate.get("executed") is not False:
            candidate_executed += 1
        if not _session_id(row):
            missing_session += 1
        if _identity(row)[0] != expected_source:
            selected_provenance_mismatch += 1

    transport_reasons: list[str] = []
    if manifest.get("schema_version") != EXPECTED_SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
        transport_reasons.append("unsupported_manifest_contract")
    if _resolved_manifest_output(manifest, manifest_file) != journal:
        transport_reasons.append("manifest_output_does_not_match_journal")
    if manifest_pairs != len(rows):
        transport_reasons.append("manifest_journal_pair_total_mismatch")
    if Counter(declared_identities) != Counter(journal_identities):
        transport_reasons.append("manifest_identity_set_mismatch")
    if len(set(journal_identities)) != len(journal_identities):
        transport_reasons.append("duplicate_global_source_decision_identity")
    if len(selected) != expected_pairs:
        transport_reasons.append("expected_pair_count_mismatch")
    if len(set(selected_identities)) != len(selected_identities):
        transport_reasons.append("duplicate_selected_source_decision_identity")
    if unconfirmed:
        transport_reasons.append("unconfirmed_or_uncounted_shadow_pair")
    if candidate_executed:
        transport_reasons.append("candidate_execution_detected")
    if missing_session:
        transport_reasons.append("missing_export_session_id")
    if selected_provenance_mismatch:
        transport_reasons.append("session_provenance_mismatch")

    transport_gate = _gate(
        not transport_reasons,
        transport_reasons,
        {
            "expected_pair_count": expected_pairs,
            "selected_pair_count": len(selected),
            "journal_pair_total": len(rows),
            "manifest_journal_pair_total": manifest_pairs,
            "manifest_identity_count": len(declared_identities),
            "unique_global_identity_count": len(set(journal_identities)),
            "unique_selected_decision_count": len(
                {identity[1] for identity in selected_identities}
            ),
            "event_confirmed_materialized_counted_rows": len(selected) - unconfirmed,
            "candidate_executed_false_rows": len(selected) - candidate_executed,
            "session_provenance_match_rows": len(selected) - selected_provenance_mismatch,
        },
    )

    causal_audit = _journal_causal_audit(selected)
    causally_ambiguous_ids = set(causal_audit["ambiguous_decision_ids"])
    state_rows = 0
    actual_materialized_rows = 0
    candidate_proposal_rows = 0
    exact_sink_rows = 0
    exact_trace_rows = 0
    exact_sink_and_trace_rows = 0
    lower_confidence_actual_rows = 0
    exact_causal_rows = 0
    exact_causal_decision_ids: set[str] = set()
    for row in selected:
        state_ok = isinstance(row.get("state"), Mapping) and bool(row.get("state"))
        actual = _mapping(row.get("expertActual"))
        candidate = _mapping(row.get("candidateShadow"))
        actual_ok = (
            actual.get("observed") is True
            and actual.get("executed") is True
            and actual.get("materialized") is True
        )
        candidate_ok = candidate.get("proposalAvailable") is True and isinstance(
            candidate.get("proposal"), Mapping
        )
        sink_ok = _nonempty_exact_sink(row)
        trace_ok = _nonempty_exact_trace(row)
        attribution = actual.get("attribution")
        lower = _lower_confidence(attribution)
        state_rows += int(state_ok)
        actual_materialized_rows += int(actual_ok)
        candidate_proposal_rows += int(candidate_ok)
        exact_sink_rows += int(sink_ok)
        exact_trace_rows += int(trace_ok)
        exact_sink_and_trace_rows += int(sink_ok and trace_ok)
        lower_confidence_actual_rows += int(lower)
        exact_causal = (
            state_ok
            and actual_ok
            and candidate_ok
            and sink_ok
            and trace_ok
            and not lower
            and str(row.get("decisionId") or "") not in causally_ambiguous_ids
        )
        exact_causal_rows += int(exact_causal)
        if exact_causal:
            exact_causal_decision_ids.add(str(row.get("decisionId") or ""))

    causal_reasons: list[str] = []
    if state_rows != len(selected):
        causal_reasons.append("missing_state_snapshot")
    if actual_materialized_rows != len(selected):
        causal_reasons.append("actual_action_not_observed_executed_materialized")
    if candidate_proposal_rows != len(selected):
        causal_reasons.append("candidate_proposal_unavailable")
    if lower_confidence_actual_rows:
        causal_reasons.append("lower_confidence_actual_attribution")
    if exact_sink_rows != len(selected):
        causal_reasons.append("missing_exact_sink_actions")
    if exact_trace_rows != len(selected):
        causal_reasons.append("missing_exact_expert_trace_links")
    if causal_audit["timeline_parseable_rows"] != len(selected):
        causal_reasons.append("causal_timeline_unusable")
    if causal_audit["successful_client_cast_anchor_rows"] != len(selected):
        causal_reasons.append("missing_successful_client_cast_acceptance_anchor")
    if causal_audit["causal_v4_token_complete_rows"] != causal_audit["causal_v4_rows"]:
        causal_reasons.append("causal_v4_event_tokens_incomplete")
    if (
        causal_audit["causal_v4_outcome_integrity_pass_rows"]
        != causal_audit["causal_v4_outcome_integrity_rows"]
    ):
        causal_reasons.append("causal_v4_outcome_integrity_failed")
    if causal_audit["unresolved_same_family_overlaps"]:
        causal_reasons.append("same_family_overlapping_outcome_windows")
    if any(
        detail.get("journal_capture_order_inverted") is True
        for detail in causal_audit["unresolved_same_family_overlaps"]
    ):
        causal_reasons.append("journal_capture_order_inversion_with_overlap")
    if (
        causal_audit["queue_diagnostics"]
        or causal_audit["unresolved_cross_row_queue_events"]
    ):
        causal_reasons.append("ambiguous_next_swing_queue_chain")
    if causal_audit["duplicate_events"]:
        causal_reasons.append("duplicate_compact_event_across_rows")
    if exact_causal_rows != len(selected):
        causal_reasons.append("state_action_pairs_not_exactly_causal")

    causal_gate = _gate(
        bool(selected) and not causal_reasons,
        causal_reasons,
        {
            "state_snapshot_rows": state_rows,
            "actual_observed_executed_materialized_rows": actual_materialized_rows,
            "candidate_proposal_available_rows": candidate_proposal_rows,
            "exact_decision_linked_sink_rows": exact_sink_rows,
            "exact_completed_trace_rows": exact_trace_rows,
            "exact_sink_and_trace_rows": exact_sink_and_trace_rows,
            "lower_confidence_actual_attribution_rows": lower_confidence_actual_rows,
            "exact_causal_state_action_rows": exact_causal_rows,
            "timeline_parseable_rows": causal_audit["timeline_parseable_rows"],
            "successful_client_cast_acceptance_anchor_rows": causal_audit[
                "successful_client_cast_anchor_rows"
            ],
            "causal_v4_rows": causal_audit["causal_v4_rows"],
            "causal_v4_token_complete_rows": causal_audit[
                "causal_v4_token_complete_rows"
            ],
            "causal_v4_outcome_integrity_rows": causal_audit[
                "causal_v4_outcome_integrity_rows"
            ],
            "causal_v4_outcome_integrity_pass_rows": causal_audit[
                "causal_v4_outcome_integrity_pass_rows"
            ],
            "causally_ambiguous_row_count": len(causally_ambiguous_ids),
            "causally_ambiguous_decision_ids": causal_audit[
                "ambiguous_decision_ids"
            ],
            "causal_ambiguity_by_decision_id": causal_audit[
                "reasons_by_decision_id"
            ],
            "journal_capture_order_inversion_count": len(
                causal_audit["capture_inversions"]
            ),
            "journal_capture_order_inversions": causal_audit[
                "capture_inversions"
            ],
            "same_family_overlap_pair_count": len(
                causal_audit["same_family_overlaps"]
            ),
            "same_family_overlap_pairs": causal_audit["same_family_overlaps"],
            "unresolved_same_family_overlap_pair_count": len(
                causal_audit["unresolved_same_family_overlaps"]
            ),
            "token_disambiguated_same_family_overlap_pair_count": sum(
                detail.get("token_disambiguated") is True
                for detail in causal_audit["same_family_overlaps"]
            ),
            "next_swing_queue_ambiguous_row_count": len(
                {
                    detail["decision_id"]
                    for detail in causal_audit["queue_diagnostics"]
                }
                | {
                    detail[key]
                    for detail in causal_audit["unresolved_cross_row_queue_events"]
                    for key in (
                        "pop_owner_decision_id",
                        "materialized_decision_id",
                    )
                }
            ),
            "next_swing_queue_diagnostics": causal_audit["queue_diagnostics"],
            "next_swing_path_diagnostics": causal_audit["next_swing_paths"],
            "cross_row_queue_event_diagnostics": causal_audit[
                "cross_row_queue_events"
            ],
            "unresolved_cross_row_queue_event_count": len(
                causal_audit["unresolved_cross_row_queue_events"]
            ),
            "duplicate_compact_event_diagnostics": causal_audit[
                "duplicate_events"
            ],
        },
    )

    telemetry_available_rows = 0
    complete_outcome_rows = 0
    terminal_outcome_rows = 0
    compact_event_rows = 0
    outcome_action_rows = 0
    lower_confidence_outcome_rows = 0
    telemetry_complete_reward_rows = 0
    causally_usable_reward_rows = 0
    for row in selected:
        outcome = _mapping(row.get("observedActualOutcome"))
        telemetry = _mapping(outcome.get("telemetry"))
        telemetry_ok = (
            telemetry.get("available") is True
            and telemetry.get("reason") == "available"
            and not _list(telemetry.get("missingCVars"))
        )
        complete = outcome.get("status") == "complete"
        terminal = outcome.get("endReason") in {
            "all_actions_terminal",
            "all_actions_terminal_causal",
        }
        compact = bool(_list(outcome.get("compactEvents")))
        actions = bool(_list(outcome.get("actions")))
        lower = _lower_confidence(outcome.get("actionAttribution"))
        telemetry_available_rows += int(telemetry_ok)
        complete_outcome_rows += int(complete)
        terminal_outcome_rows += int(terminal)
        compact_event_rows += int(compact)
        outcome_action_rows += int(actions)
        lower_confidence_outcome_rows += int(lower)
        telemetry_complete_reward = (
            telemetry_ok and complete and terminal and compact and actions and not lower
        )
        telemetry_complete_reward_rows += int(telemetry_complete_reward)
        causally_usable_reward_rows += int(
            telemetry_complete_reward
            and str(row.get("decisionId") or "") in exact_causal_decision_ids
        )

    outcome_reasons: list[str] = []
    if telemetry_available_rows != len(selected):
        outcome_reasons.append("typed_outcome_telemetry_unavailable")
    if complete_outcome_rows != len(selected):
        outcome_reasons.append("outcome_window_not_complete")
    if terminal_outcome_rows != len(selected):
        outcome_reasons.append("outcome_actions_not_terminal")
    if compact_event_rows != len(selected):
        outcome_reasons.append("compact_outcome_events_missing")
    if outcome_action_rows != len(selected):
        outcome_reasons.append("materialized_outcome_actions_missing")
    if lower_confidence_outcome_rows:
        outcome_reasons.append("lower_confidence_outcome_attribution")
    if telemetry_complete_reward_rows != len(selected):
        outcome_reasons.append("actual_reward_rows_not_usable")
    if causally_usable_reward_rows != len(selected):
        outcome_reasons.append("causally_attributed_reward_rows_not_usable")

    outcome_gate = _gate(
        bool(selected) and not outcome_reasons,
        outcome_reasons,
        {
            "typed_telemetry_available_rows": telemetry_available_rows,
            "complete_outcome_rows": complete_outcome_rows,
            "all_actions_terminal_rows": terminal_outcome_rows,
            "nonempty_compact_event_rows": compact_event_rows,
            "nonempty_outcome_action_rows": outcome_action_rows,
            "lower_confidence_outcome_attribution_rows": lower_confidence_outcome_rows,
            "telemetry_complete_reward_rows": telemetry_complete_reward_rows,
            "causally_usable_reward_rows": causally_usable_reward_rows,
            "reward_usable_rows": causally_usable_reward_rows,
        },
    )

    actuals = [_mapping(row.get("expertActual")) for row in selected]
    outcomes = [_mapping(row.get("observedActualOutcome")) for row in selected]
    samples = [_mapping(row.get("shadowSample")) for row in selected]
    states = [_mapping(row.get("state")) for row in selected]
    missing_cvars: list[str] = []
    for outcome in outcomes:
        missing_cvars.extend(
            str(value) for value in _list(_mapping(outcome.get("telemetry")).get("missingCVars"))
        )

    all_evidence_gates_pass = (
        transport_gate["passed"] and causal_gate["passed"] and outcome_gate["passed"]
    )
    deployment_reasons = [
        "shadow_acceptance_gates_not_all_passed"
        if not all_evidence_gates_pass
        else "shadow_journal_does_not_establish_candidate_superiority",
        "candidate_policy_remains_inactive_shadow",
    ]

    return {
        "schema": SCHEMA,
        "generated_at": _utc_now(),
        "status": "PASS" if all_evidence_gates_pass else "FAIL",
        "input": {
            "journal": str(journal),
            "journal_sha256": before_hashes["journal"],
            "manifest": str(manifest_file),
            "manifest_sha256": before_hashes["manifest"],
            "selected_export_session_id": selected_session,
            "session_selection": "explicit" if requested_session else "latest_journal_row",
            "journal_sessions": _counter([_session_id(row) for row in rows]),
        },
        "transport_action_gate": transport_gate,
        "causal_state_action_gate": causal_gate,
        "outcome_reward_gate": outcome_gate,
        "deployment_allowed": False,
        "deployment_rejection_reasons": deployment_reasons,
        "distributions": {
            "actual_action_family": _counter([sample.get("actionFamily") for sample in samples]),
            "confirmed_event": _counter([sample.get("confirmedEvent") for sample in samples]),
            "actual_attribution": _counter([actual.get("attribution") for actual in actuals]),
            "outcome_status": _counter([outcome.get("status") for outcome in outcomes]),
            "outcome_end_reason": _counter([outcome.get("endReason") for outcome in outcomes]),
            "telemetry_reason": _counter(
                [_mapping(outcome.get("telemetry")).get("reason") for outcome in outcomes]
            ),
            "missing_cvar": _counter(missing_cvars),
            "coalesced_macro_evaluations": _counter(
                [sample.get("coalescedMacroEvaluations") for sample in samples]
            ),
            "candidate_proposal": {
                lane: _proposal_lane_distribution(selected, "candidateShadow", lane)
                for lane in ("gcd", "off_gcd", "queue")
            },
            "active_policy_proposal": {
                lane: _proposal_lane_distribution(selected, "activePolicy", lane)
                for lane in ("gcd", "off_gcd", "queue")
            },
            "state_coverage": {
                "rage_bucket": _counter([_rage_bucket(state.get("rage")) for state in states]),
                "target_health_percent_bucket": _counter(
                    [_health_bucket(state.get("targetPercentHealth")) for state in states]
                ),
                "nearby_enemies": _counter([state.get("nearbyEnemies") for state in states]),
                "in_combat": _counter([state.get("inCombat") for state in states]),
            },
        },
        "interpretation": {
            "transport_scope": "durable event-confirmed actual-action pairs only",
            "causal_scope": (
                "successful client-cast acceptance plus an unambiguous, "
                "decision-linked Cat2/expert sink, trace, and outcome timeline"
            ),
            "reward_scope": (
                "typed terminal outcome completeness is reported separately from "
                "causally attributable reward usability"
            ),
            "candidate_counterfactual_outcome_available": False,
            "candidate_superiority_claim_supported": False,
        },
    }


def write_shadow_pair_acceptance(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, UnicodeError) as error:
        raise ShadowPairAcceptanceError(
            f"cannot write Shadow pair acceptance report {path}: {error}"
        ) from error
    return path


def audit_shadow_pairs(
    journal_path: str | Path = DEFAULT_JOURNAL,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
    expected_pairs: int = 60,
    export_session_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    report = build_shadow_pair_acceptance(
        journal_path,
        manifest_path,
        expected_pairs=expected_pairs,
        export_session_id=export_session_id,
    )
    return report, write_shadow_pair_acceptance(report, output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-pairs", type=int, default=60)
    parser.add_argument("--export-session-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, output = audit_shadow_pairs(
            args.journal,
            args.manifest,
            output_path=args.output,
            expected_pairs=args.expected_pairs,
            export_session_id=args.export_session_id,
        )
    except ShadowPairAcceptanceError as error:
        print(f"Shadow pair acceptance audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "export_session_id": report["input"]["selected_export_session_id"],
                "transport_action_gate": report["transport_action_gate"]["status"],
                "causal_state_action_gate": report["causal_state_action_gate"]["status"],
                "outcome_reward_gate": report["outcome_reward_gate"]["status"],
                "deployment_allowed": report["deployment_allowed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "PASS" else 1


__all__ = [
    "DEFAULT_JOURNAL",
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "SCHEMA",
    "ShadowPairAcceptanceError",
    "audit_v4_outcome_integrity",
    "audit_shadow_pairs",
    "build_shadow_pair_acceptance",
    "write_shadow_pair_acceptance",
]


if __name__ == "__main__":
    raise SystemExit(main())
