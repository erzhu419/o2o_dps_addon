"""Import BrainOfCat Shadow checkpoints without promoting partial observations.

The client writes second-resolution Unix time and a monotonic local ordinal.
Chronicle supplies millisecond event time and EventMeta order, but no verified
client/server clock offset.  A unique time/GUID encounter match is therefore a
coarse identity join, never an exact causal-order join.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


SCHEMA = "brainofcat_shadow_checkpoint/v1"
IMPORT_SCHEMA = "brainofcat_shadow_checkpoint_import/v1"
REQUIRED_FIELDS = (
    "target.max_health",
    "target.current_health",
    "player.rage_current",
    "player.stance",
    "timers.gcd_remaining_ms",
    "timers.cooldowns_remaining_ms",
    "timers.main_hand_swing_remaining_ms",
    "timers.off_hand_swing_remaining_ms",
    "queue.next_swing",
    "player.self_auras_and_procs",
    "target.candidate_owned_existing_debuffs",
)
QUALITIES = {"EXACT", "PARTIAL", "MISSING"}
KINDS = {"checkpoint", "event_delta", "binding"}
_GUID = re.compile(r"^(?:0x)?[0-9a-f]+$", re.IGNORECASE)


class ShadowCheckpointError(ValueError):
    """A checkpoint row cannot be interpreted under the v1 wire contract."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShadowCheckpointError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowCheckpointError(f"{label} must be nonempty text")
    return value.strip()


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShadowCheckpointError(f"{label} must be an integer >= {minimum}")
    return value


def _nonnegative(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShadowCheckpointError(f"{label} must be a nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ShadowCheckpointError(f"{label} must be a finite nonnegative number")
    return value


def _guid(value: Any, label: str) -> str:
    guid = _text(value, label)
    if not _GUID.fullmatch(guid):
        raise ShadowCheckpointError(f"{label} must be a hexadecimal WoW GUID")
    digits = guid[2:] if guid.lower().startswith("0x") else guid
    return "0x" + digits.upper()


def _typed_exact(name: str, value: Any) -> None:
    if name in {"target.max_health", "target.current_health", "player.rage_current"}:
        _nonnegative(value, name)
        if name == "target.max_health" and value == 0:
            raise ShadowCheckpointError("exact target maximum health must be positive")
    elif name in {
        "timers.gcd_remaining_ms",
        "timers.main_hand_swing_remaining_ms",
        "timers.off_hand_swing_remaining_ms",
    }:
        _integer(value, name)
    elif name == "player.stance":
        if value not in {"BATTLE", "DEFENSIVE", "BERSERKER"}:
            raise ShadowCheckpointError("exact stance is not a supported Warrior stance")
    elif name == "queue.next_swing":
        if value not in {"NONE", "HEROIC_STRIKE", "CLEAVE"}:
            raise ShadowCheckpointError("exact queue state is not a supported action")
    elif name in {
        "timers.cooldowns_remaining_ms",
        "player.self_auras_and_procs",
        "target.candidate_owned_existing_debuffs",
    }:
        if not isinstance(value, list):
            raise ShadowCheckpointError(f"exact {name} must be an array")
        for index, member in enumerate(value):
            item = _object(member, f"{name}[{index}]")
            _integer(item.get("spellId"), f"{name}[{index}].spellId", minimum=1)
            if name == "timers.cooldowns_remaining_ms":
                _integer(item.get("remainingMs"), f"{name}[{index}].remainingMs")
            else:
                _integer(item.get("stacks"), f"{name}[{index}].stacks", minimum=1)
                _integer(item.get("remainingMs"), f"{name}[{index}].remainingMs")
            if name == "target.candidate_owned_existing_debuffs":
                _guid(item.get("targetGuid"), f"{name}[{index}].targetGuid")
                _guid(item.get("casterGuid"), f"{name}[{index}].casterGuid")


def _validate_fields(fields: Any, target_guid: str | None) -> tuple[dict[str, Any], list[str]]:
    source = _object(fields, "fields")
    if set(source) != set(REQUIRED_FIELDS):
        missing = sorted(set(REQUIRED_FIELDS) - set(source))
        extra = sorted(set(source) - set(REQUIRED_FIELDS))
        raise ShadowCheckpointError(f"fields differ from v1 contract: missing={missing}, extra={extra}")
    validated: dict[str, Any] = {}
    blockers: list[str] = []
    for name in REQUIRED_FIELDS:
        item = _object(source[name], f"fields.{name}")
        quality = item.get("quality")
        if quality not in QUALITIES:
            raise ShadowCheckpointError(f"fields.{name}.quality is invalid")
        origin = _text(item.get("source"), f"fields.{name}.source")
        if any(term in origin.upper() for term in ("FUTURE", "HYPOTHESIS", "SIMULATOR_DEFAULT")):
            raise ShadowCheckpointError(f"fields.{name} uses non-observed source")
        value = item.get("value")
        if quality == "MISSING" and value is not None:
            raise ShadowCheckpointError(f"missing fields.{name} carries a value")
        if quality == "EXACT":
            if value is None:
                raise ShadowCheckpointError(f"exact fields.{name} has no value")
            _typed_exact(name, value)
            if name.startswith("target.") and target_guid is None:
                raise ShadowCheckpointError(f"exact fields.{name} has no targetGuid")
        else:
            blockers.append(name)
        validated[name] = deepcopy(dict(item))
    return validated, blockers


def validate_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one append-only client envelope; never infer omitted state."""
    record = deepcopy(dict(_object(raw, "record")))
    if record.get("schema") != SCHEMA:
        raise ShadowCheckpointError("schema is not brainofcat_shadow_checkpoint/v1")
    kind = record.get("kind")
    if kind not in KINDS:
        raise ShadowCheckpointError("kind must be checkpoint, event_delta, or binding")
    for key in ("sessionId", "characterKey"):
        _text(record.get(key), key)
    if kind != "binding" or record.get("pullId") is not None:
        _text(record.get("pullId"), "pullId")
    record["playerGuid"] = _guid(record.get("playerGuid"), "playerGuid")
    at = _object(record.get("at"), "at")
    _integer(at.get("epochSeconds"), "at.epochSeconds", minimum=1)
    _nonnegative(at.get("getTimeSeconds"), "at.getTimeSeconds")
    _integer(at.get("causalOrdinal"), "at.causalOrdinal")
    if kind == "checkpoint" or record.get("instance") is not None:
        instance = _object(record.get("instance"), "instance")
        if instance.get("idQuality") not in QUALITIES:
            raise ShadowCheckpointError("instance.idQuality is invalid")
        if instance.get("zone") not in (None, ""):
            _text(instance["zone"], "instance.zone")
        if instance.get("subZone") is not None and instance["subZone"] != "":
            _text(instance["subZone"], "instance.subZone")
    _text(record.get("trigger"), "trigger")
    target_guid = record.get("targetGuid")
    if kind == "event_delta" and target_guid == "":
        # Nampower UNIT_CASTEVENT uses an empty string for an untargeted cast.
        # This is observed in the first live client file; it is not a GUID.
        target_guid = None
        record.pop("targetGuid", None)
    if target_guid is not None:
        target_guid = _guid(target_guid, "targetGuid")
        record["targetGuid"] = target_guid

    if kind == "checkpoint":
        if record.get("exactCheckpointReady") is True:
            raise ShadowCheckpointError("v1 client row cannot claim an exact checkpoint")
        fields, blockers = _validate_fields(record.get("fields"), target_guid)
        record["fields"] = fields
        targets = record.get("targets")
        if not isinstance(targets, list):
            raise ShadowCheckpointError("checkpoint.targets must be an array")
        observed: set[str] = set()
        for index, target in enumerate(targets):
            item = _object(target, f"targets[{index}]")
            guid = _guid(item.get("guid"), f"targets[{index}].guid")
            if guid in observed:
                raise ShadowCheckpointError("checkpoint.targets repeats a GUID")
            observed.add(guid)
            for hp_name in ("maxHealth", "currentHealth"):
                if item.get(hp_name) is not None:
                    _nonnegative(item[hp_name], f"targets[{index}].{hp_name}")
            if item.get("attackable") is not None and not isinstance(item["attackable"], bool):
                raise ShadowCheckpointError(f"targets[{index}].attackable must be boolean")
        if target_guid is not None and target_guid not in observed:
            raise ShadowCheckpointError("targetGuid is absent from observed targets")
        max_hp = fields["target.max_health"]
        current_hp = fields["target.current_health"]
        selected = next((item for item in targets if _guid(item["guid"], "target GUID") == target_guid), None)
        if selected is not None:
            for field, target_key in ((max_hp, "maxHealth"), (current_hp, "currentHealth")):
                if field["quality"] == "EXACT" and selected.get(target_key) != field["value"]:
                    raise ShadowCheckpointError(f"exact {target_key} disagrees with targets row")
        if max_hp["quality"] == current_hp["quality"] == "EXACT":
            if current_hp["value"] > max_hp["value"]:
                raise ShadowCheckpointError("current health exceeds maximum health")
        # The v1 writer observes only the visible target.  Exact scalar HP is
        # valid for that GUID, not for the whole prefix-visible target registry.
        blockers.append("target.registry_scope")
        record["exactCheckpointReady"] = not blockers
        record["exactCheckpointBlockers"] = blockers
    elif kind == "event_delta":
        event = dict(_object(record.get("event"), "event"))
        _text(event.get("name"), "event.name")
        if event.get("kind") is not None and event["kind"] not in {"START", "GO", "FAIL", "DMG", "MISS", "CLIENT_OR_UNIT_CAST", "CLIENT_LOG_RESULT"}:
            raise ShadowCheckpointError("event.kind is not a supported server event kind")
        if event.get("targetGuid") == "":
            event.pop("targetGuid")
        for name in ("sourceGuid", "targetGuid"):
            if event.get(name) is not None:
                _guid(event[name], f"event.{name}")
        record["event"] = event
        if event.get("spellId") is not None:
            _integer(event["spellId"], "event.spellId", minimum=1)
    elif kind == "binding":
        if not isinstance(record.get("equipment"), list) or not isinstance(record.get("talents"), list):
            raise ShadowCheckpointError("binding needs equipment and talents arrays")
        _object(record.get("clientBuild"), "clientBuild")
    return record


def _event_identity(row: Mapping[str, Any]) -> tuple[str, str, int] | None:
    instance = row.get("instance")
    encounter = row.get("encounter")
    timestamp = row.get("timestamp_ms")
    if not isinstance(instance, str) or not instance or not isinstance(encounter, str) or not encounter:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        return None
    return instance, encounter, timestamp


def _signature(event: Mapping[str, Any], *, chronicle: bool) -> tuple[Any, ...] | None:
    payload = event if chronicle else _object(event.get("event"), "event")
    kind = payload.get("type" if chronicle else "kind")
    if kind not in {"START", "GO", "FAIL", "DMG"}:
        return None
    source = payload.get("source_guid" if chronicle else "sourceGuid")
    target = payload.get("target_guid" if chronicle else "targetGuid")
    spell_id = payload.get("spell_id" if chronicle else "spellId")
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    if not _GUID.fullmatch(source) or not _GUID.fullmatch(target):
        return None
    if isinstance(spell_id, bool) or not isinstance(spell_id, int) or spell_id <= 0:
        return None
    amount = payload.get("value" if chronicle else "amount") if kind == "DMG" else None
    if kind == "DMG" and (isinstance(amount, bool) or not isinstance(amount, int) or amount < 0):
        return None
    return kind, _guid(source, "source GUID"), _guid(target, "target GUID"), spell_id, amount


def _index_chronicle(
    events: Iterable[Mapping[str, Any]], needed: set[tuple[Any, ...]]
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[Any, ...], dict[tuple[str, str], list[tuple[int, int]]]]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    anchors: dict[tuple[Any, ...], dict[tuple[str, str], list[tuple[int, int]]]] = {}
    for raw_event in events:
        event = _object(raw_event, "Chronicle event")
        identity = _event_identity(event)
        if identity is None:
            continue
        instance, encounter, timestamp = identity
        key = instance, encounter
        summary = grouped.setdefault(key, {"first": timestamp, "last": timestamp, "guids": set()})
        summary["first"] = min(summary["first"], timestamp)
        summary["last"] = max(summary["last"], timestamp)
        for field in ("source_guid", "target_guid"):
            value = event.get(field)
            if isinstance(value, str) and _GUID.fullmatch(value):
                summary["guids"].add(_guid(value, field))
        signature = _signature(event, chronicle=True)
        if signature in needed:
            event_index = event.get("event_index")
            if isinstance(event_index, bool) or not isinstance(event_index, int):
                continue
            found = anchors.setdefault(signature, {}).setdefault(key, [])
            if len(found) < 2:  # two occurrences suffice to reject ambiguity.
                found.append((timestamp, event_index))
    return grouped, anchors


def _join_from_index(
    record: Mapping[str, Any],
    local_records: Iterable[Mapping[str, Any]],
    grouped: Mapping[tuple[str, str], Mapping[str, Any]],
    anchors: Mapping[tuple[Any, ...], Mapping[tuple[str, str], list[tuple[int, int]]]],
) -> dict[str, Any]:
    lower = record["at"]["epochSeconds"] * 1000
    upper = lower + 1000
    guid = record["playerGuid"]
    matches = [
        key for key, summary in grouped.items()
        if summary["first"] < lower and summary["last"] >= upper
        and guid in summary["guids"]
    ]
    if len(matches) != 1:
        return {
            "status": "AMBIGUOUS_TIME_GUID" if matches else "NO_TIME_GUID_MATCH",
            "source_identity": None,
            "candidate_count": len(matches),
            "client_time_interval_utc_ms": [lower, upper],
            "exact_event_order_equivalent": False,
        }
    key = matches[0]
    ordinal = record["at"]["causalOrdinal"]
    bracket: dict[str, list[tuple[int, int]]] = {"before": [], "after": []}
    for local in local_records:
        if local["kind"] != "event_delta":
            continue
        if (local["sessionId"], local["pullId"], local["playerGuid"]) != (
            record["sessionId"], record["pullId"], guid
        ):
            continue
        local_ordinal = local["at"]["causalOrdinal"]
        if local_ordinal == ordinal:
            continue
        signature = _signature(local, chronicle=False)
        if signature is None:
            continue
        positions = anchors.get(signature, {}).get(key, [])
        if len(positions) != 1:
            continue
        side = "before" if local_ordinal < ordinal else "after"
        bracket[side].append(positions[0])
    if bracket["before"] and bracket["after"]:
        before = max(bracket["before"])
        after = min(bracket["after"])
        if before < after:
            return {
                "status": "CLIENT_OBSERVED_EVENT_BRACKET",
                "source_identity": {"instance_id": key[0], "encounter_id": key[1]},
                "client_time_interval_utc_ms": [lower, upper],
                "chronicle_order_bracket": {"before": list(before), "after": list(after)},
                "exact_event_order_equivalent": False,
                "reason": "matching server events bracket client observation, not server checkpoint time",
            }
    return {
        "status": "COARSE_UNIQUE_TIME_GUID",
        "source_identity": {"instance_id": key[0], "encounter_id": key[1]},
        "client_time_interval_utc_ms": [lower, upper],
        "exact_event_order_equivalent": False,
        "reason": "client Unix second has no two unique matched EventMeta anchors",
    }


def join_chronicle_encounter(
    checkpoint: Mapping[str, Any], chronicle_events: Iterable[Mapping[str, Any]],
    *, local_records: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Join by coarse time/GUID, optionally proving a two-sided event bracket."""
    record = validate_record(checkpoint)
    if record["kind"] != "checkpoint":
        raise ShadowCheckpointError("Chronicle join requires a checkpoint")
    local = [validate_record(item) for item in local_records]
    needed = {signature for item in local if item["kind"] == "event_delta"
              if (signature := _signature(item, chronicle=False)) is not None}
    grouped, anchors = _index_chronicle(chronicle_events, needed)
    return _join_from_index(record, local, grouped, anchors)


def import_jsonl(
    path: Path, *, chronicle_events: Iterable[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """Read a small append-only CustomData export and report checkpoint coverage."""
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    row = validate_record(raw)
                except (ValueError, TypeError) as error:
                    raise ShadowCheckpointError(f"{path}:{line_number}: {error}") from error
                rows.append(row)
    except OSError as error:
        raise ShadowCheckpointError(f"cannot read {path}: {error}") from error
    needed = {signature for row in rows if row["kind"] == "event_delta"
              if (signature := _signature(row, chronicle=False)) is not None}
    grouped, anchors = _index_chronicle(chronicle_events, needed)
    joined = [
        {"sessionId": row["sessionId"], "pullId": row["pullId"],
         "causalOrdinal": row["at"]["causalOrdinal"],
         "exactCheckpointReady": row["exactCheckpointReady"],
         "blockers": row["exactCheckpointBlockers"],
         "chronicle_join": _join_from_index(row, rows, grouped, anchors) if grouped else None}
        for row in rows if row["kind"] == "checkpoint"
    ]
    return {
        "schema": IMPORT_SCHEMA,
        "records": rows,
        "checkpoints": joined,
        "record_count": len(rows),
        "checkpoint_count": len(joined),
        "exact_checkpoint_count": sum(item["exactCheckpointReady"] for item in joined),
        "client_observed_event_bracket_count": sum(
            item["chronicle_join"] is not None
            and item["chronicle_join"]["status"] == "CLIENT_OBSERVED_EVENT_BRACKET"
            for item in joined
        ),
        "exact_chronicle_join_count": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--chronicle-events", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    events: Iterable[dict[str, Any]] = ()
    if args.chronicle_events:
        with args.chronicle_events.open("r", encoding="utf-8") as source:
            events = (json.loads(line) for line in source if line.strip())
            imported = import_jsonl(args.input, chronicle_events=events)
    else:
        imported = import_jsonl(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(imported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
