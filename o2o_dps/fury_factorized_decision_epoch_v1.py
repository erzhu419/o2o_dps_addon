"""Build and audit causal Fury factorized START epochs without row copies.

Chronicle exposes server START candidates, not client keypresses or next-swing
queue intent.  V1 therefore groups only START rows with the exact same
``offset_ms`` and exact same instance, encounter, and player identity.  It
projects the observable candidates into the roadmap action lanes while keeping
the swing-queue lane missing.  Production use writes one aggregate JSON audit;
it never materializes another decision-row dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_fury_decision_dataset"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_factorized_decision_epoch_v1.json"
)
DATASET_SCHEMA = "chronicle_fury_decision_dataset/v1"
RECORD_SCHEMA = "chronicle_fury_decision/v1"
EPOCH_SCHEMA = "fury_factorized_decision_epoch/v1"
AUDIT_SCHEMA = "fury_factorized_decision_epoch_audit/v1"

ACTION_LANES = ("gcd", "swing_queue", "off_gcd", "stance", "target")
LANE_STATUSES = frozenset(("OBSERVED", "UNKNOWN", "MISSING"))
STANCE_ACTIONS = frozenset(
    (
        "warrior.battle_stance",
        "warrior.defensive_stance",
        "warrior.berserker_stance",
    )
)
SOURCE_DISPOSITIONS = (
    "gcd_start_candidate",
    "off_gcd_start_candidate",
    "stance_start_candidate",
    "on_swing_execution_only",
    "unmapped_start_candidate",
    "unsupported_lane_start_candidate",
)


class FuryFactorizedDecisionEpochError(RuntimeError):
    """A compact row, factorized epoch, or manifest violates the V1 contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryFactorizedDecisionEpochError(f"compact row lacks {label} object")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryFactorizedDecisionEpochError(
            f"cannot read compact dataset manifest {path}: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schema") != DATASET_SCHEMA:
        raise FuryFactorizedDecisionEpochError(
            f"compact manifest must use schema {DATASET_SCHEMA}"
        )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise FuryFactorizedDecisionEpochError("compact manifest has no partitions")
    return manifest


def _partition_path(entry: Mapping[str, Any], manifest_path: Path) -> Path:
    supplied = str(entry.get("partition") or "").strip()
    if not supplied:
        raise FuryFactorizedDecisionEpochError("partition manifest entry has no path")
    path = Path(supplied)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _trajectory_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = _required_mapping(record.get("identity"), "identity")
    values = (
        str(identity.get("source_instance_ref") or "").strip(),
        str(identity.get("encounter_id") or "").strip(),
        str(identity.get("player_guid") or "").strip().casefold(),
    )
    if not all(values):
        raise FuryFactorizedDecisionEpochError(
            "compact row lacks source instance, encounter, or player GUID"
        )
    return values


def _start_anchor(record: Mapping[str, Any]) -> dict[str, int]:
    source = _required_mapping(record.get("source"), "source")
    anchor = _required_mapping(source.get("start_anchor"), "source.start_anchor")
    output: dict[str, int] = {}
    for name in ("event_index", "csv_line", "offset_ms"):
        value = anchor.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise FuryFactorizedDecisionEpochError(
                f"source.start_anchor.{name} must be an integer"
            )
        output[name] = value
    return output


def _action(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _required_mapping(record.get("action"), "action")


def _source_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    instance, encounter, player = _trajectory_key(record)
    anchor = _start_anchor(record)
    action = _action(record)
    return {
        "kind": "OBSERVED",
        "source_semantics": "server_observed_START_candidate",
        "source_instance_ref": instance,
        "encounter_id": encounter,
        "player_guid": player,
        "event_index": anchor["event_index"],
        "csv_line": anchor["csv_line"],
        "offset_ms": anchor["offset_ms"],
        "decision_id": action.get("decision_id"),
        "policy_action_key": action.get("policy_action_key"),
        "spell_id": action.get("spell_id"),
        "spell_name": action.get("spell_name"),
        "declared_lane": action.get("lane"),
    }


def _action_value(record: Mapping[str, Any]) -> dict[str, Any]:
    action = _action(record)
    return {
        "policy_action_key": action.get("policy_action_key"),
        "spell_id": action.get("spell_id"),
        "spell_name": action.get("spell_name"),
        "semantics": "server_observed_START_candidate",
    }


def _source_disposition(record: Mapping[str, Any]) -> str:
    action = _action(record)
    if action.get("catalog_status") != "MAPPED_ACTIVE":
        return "unmapped_start_candidate"
    policy_key = str(action.get("policy_action_key") or "")
    if policy_key in STANCE_ACTIONS:
        return "stance_start_candidate"
    lane = str(action.get("lane") or "")
    if lane == "gcd":
        return "gcd_start_candidate"
    if lane == "off_gcd":
        return "off_gcd_start_candidate"
    if lane == "on_swing_unknown_intent":
        return "on_swing_execution_only"
    return "unsupported_lane_start_candidate"


def _missing_lane(reason: str) -> dict[str, Any]:
    return {
        "value": None,
        "status": "MISSING",
        "provenance": [],
        "reason": reason,
    }


def _unknown_lane(records: Sequence[Mapping[str, Any]], reason: str) -> dict[str, Any]:
    return {
        "value": None,
        "status": "UNKNOWN",
        "provenance": [_source_provenance(record) for record in records],
        "reason": reason,
    }


def _action_lane(
    records: Sequence[Mapping[str, Any]],
    *,
    unmapped: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(records) == 1:
        return {
            "value": _action_value(records[0]),
            "status": "OBSERVED",
            "provenance": [_source_provenance(records[0])],
            "reason": "one_mapped_server_START_candidate",
        }
    if len(records) > 1:
        return _unknown_lane(records, "multiple_same_lane_START_candidates")
    if unmapped:
        return _unknown_lane(
            unmapped,
            "unmapped_START_candidate_could_not_be_assigned_to_this_lane",
        )
    return _missing_lane("no_mapped_server_START_candidate_for_lane")


def _target_lane(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    player_guid = _trajectory_key(records[0])[2]
    targeted: list[Mapping[str, Any]] = []
    names_without_guid: list[Mapping[str, Any]] = []
    by_guid: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        action = _action(record)
        guid = str(action.get("target_guid") or "").strip()
        name = str(action.get("target_name") or "").strip()
        if guid and guid.casefold() != player_guid:
            targeted.append(record)
            by_guid.setdefault(guid.casefold(), []).append(record)
        elif not guid and name:
            names_without_guid.append(record)
    if len(by_guid) > 1:
        return _unknown_lane(targeted, "conflicting_resolved_target_GUIDs")
    if len(by_guid) == 1:
        selected = next(iter(by_guid.values()))
        names = {
            str(_action(record).get("target_name") or "").strip()
            for record in selected
            if str(_action(record).get("target_name") or "").strip()
        }
        if len(names) > 1:
            return _unknown_lane(selected, "conflicting_names_for_resolved_target_GUID")
        first_action = _action(selected[0])
        return {
            "value": {
                "target_guid": first_action.get("target_guid"),
                "target_name": next(iter(names)) if names else None,
                "semantics": "resolved_server_target_not_target_switch_intent",
            },
            "status": "OBSERVED",
            "provenance": [_source_provenance(record) for record in selected],
            "reason": "one_resolved_nonself_target_GUID_at_epoch",
        }
    if names_without_guid:
        return _unknown_lane(
            names_without_guid,
            "target_name_without_GUID_is_not_a_stable_target_identity",
        )
    return _missing_lane("no_resolved_nonself_target_GUID_at_epoch")


def _exclusions(
    dispositions: Sequence[tuple[Mapping[str, Any], str]],
    action: Mapping[str, Any],
) -> list[dict[str, Any]]:
    exclusions: list[dict[str, Any]] = []
    reason_by_disposition = {
        "on_swing_execution_only": (
            "server_on_swing_START_is_execution_evidence_not_queue_intent"
        ),
        "unmapped_start_candidate": "unmapped_START_has_no_factorized_lane",
        "unsupported_lane_start_candidate": "mapped_START_has_unsupported_lane",
    }
    for record, disposition in dispositions:
        reason = reason_by_disposition.get(disposition)
        if reason is not None:
            exclusions.append(
                {
                    "reason": reason,
                    "source_disposition": disposition,
                    "provenance": _source_provenance(record),
                }
            )
    for lane in ("gcd", "off_gcd", "stance", "target"):
        value = action[lane]
        if value["status"] == "UNKNOWN":
            exclusions.append(
                {
                    "reason": value["reason"],
                    "source_disposition": f"{lane}_ambiguous",
                    "provenance": list(value["provenance"]),
                }
            )
    return exclusions


def factorize_start_group(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Project one exact-time, single-trajectory START group into five lanes."""

    if not records:
        raise FuryFactorizedDecisionEpochError("cannot factorize an empty START group")
    ordered = sorted(
        records,
        key=lambda record: (
            _start_anchor(record)["event_index"],
            _start_anchor(record)["csv_line"],
        ),
    )
    key = _trajectory_key(ordered[0])
    offset_ms = _start_anchor(ordered[0])["offset_ms"]
    previous_order: tuple[int, int] | None = None
    for record in ordered:
        if record.get("schema") != RECORD_SCHEMA:
            raise FuryFactorizedDecisionEpochError(
                f"factorized input must use row schema {RECORD_SCHEMA}"
            )
        if _trajectory_key(record) != key:
            raise FuryFactorizedDecisionEpochError(
                "factorized START group crosses instance, encounter, or player"
            )
        anchor = _start_anchor(record)
        if anchor["offset_ms"] != offset_ms:
            raise FuryFactorizedDecisionEpochError(
                "V1 factorized START group must have exact equal offset_ms"
            )
        order = (anchor["event_index"], anchor["csv_line"])
        if previous_order is not None and order <= previous_order:
            raise FuryFactorizedDecisionEpochError(
                "START anchors must be strictly ordered within an epoch"
            )
        previous_order = order

    dispositions = [(record, _source_disposition(record)) for record in ordered]
    by_disposition: dict[str, list[Mapping[str, Any]]] = {
        name: [] for name in SOURCE_DISPOSITIONS
    }
    for record, disposition in dispositions:
        by_disposition[disposition].append(record)
    unmapped = [
        record
        for record, disposition in dispositions
        if disposition in (
            "unmapped_start_candidate",
            "unsupported_lane_start_candidate",
        )
    ]
    on_swing = by_disposition["on_swing_execution_only"]
    action: dict[str, Any] = {
        "gcd": _action_lane(by_disposition["gcd_start_candidate"], unmapped=unmapped),
        "swing_queue": _missing_lane(
            "server_on_swing_START_does_not_reveal_client_queue_intent"
            if on_swing
            else "Chronicle_START_stream_does_not_observe_client_queue_intent"
        ),
        "off_gcd": _action_lane(
            by_disposition["off_gcd_start_candidate"], unmapped=unmapped
        ),
        "stance": _action_lane(
            by_disposition["stance_start_candidate"], unmapped=unmapped
        ),
        "target": _target_lane(ordered),
    }
    source_starts = [_source_provenance(record) for record in ordered]
    cutoff = dict(source_starts[-1])
    cutoff["kind"] = "OBSERVED"
    cutoff["note"] = "latest START anchor admitted to this exact-time epoch"
    status = (
        "AMBIGUOUS"
        if any(value["status"] == "UNKNOWN" for value in action.values())
        else "PARTIAL"
    )
    epoch = {
        "schema": EPOCH_SCHEMA,
        "identity": {
            "source_instance_ref": key[0],
            "encounter_id": key[1],
            "player_guid": key[2],
        },
        "grouping": {
            "rule": "exact_equal_offset_ms_within_same_trajectory",
            "max_gap_ms": 0,
            "offset_ms": offset_ms,
            "source_row_count": len(ordered),
        },
        "causal_cutoff": cutoff,
        "source_starts": source_starts,
        "action": action,
        "source_row_dispositions": [
            {
                "disposition": disposition,
                "provenance": _source_provenance(record),
            }
            for record, disposition in dispositions
        ],
        "exclusions": _exclusions(dispositions, action),
        "status": status,
    }
    validate_factorized_epoch(epoch)
    return epoch


def _iter_nested_anchors(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if all(name in value for name in ("event_index", "csv_line", "offset_ms")):
            yield value
        for nested in value.values():
            yield from _iter_nested_anchors(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_nested_anchors(nested)


def validate_factorized_epoch(epoch: Mapping[str, Any]) -> None:
    """Reject identity crossing, future anchors, and malformed lane statuses."""

    if epoch.get("schema") != EPOCH_SCHEMA:
        raise FuryFactorizedDecisionEpochError(
            f"factorized epoch must use schema {EPOCH_SCHEMA}"
        )
    identity = _required_mapping(epoch.get("identity"), "epoch.identity")
    expected_identity = (
        str(identity.get("source_instance_ref") or "").strip(),
        str(identity.get("encounter_id") or "").strip(),
        str(identity.get("player_guid") or "").strip().casefold(),
    )
    if not all(expected_identity):
        raise FuryFactorizedDecisionEpochError("factorized epoch identity is incomplete")
    grouping = _required_mapping(epoch.get("grouping"), "epoch.grouping")
    if (
        grouping.get("rule") != "exact_equal_offset_ms_within_same_trajectory"
        or grouping.get("max_gap_ms") != 0
    ):
        raise FuryFactorizedDecisionEpochError(
            "V1 grouping must use exact equal offsets with max_gap_ms=0"
        )
    epoch_offset = grouping.get("offset_ms")
    if not isinstance(epoch_offset, int) or isinstance(epoch_offset, bool):
        raise FuryFactorizedDecisionEpochError("epoch grouping offset_ms must be integer")
    cutoff = _required_mapping(epoch.get("causal_cutoff"), "epoch.causal_cutoff")
    cutoff_event = cutoff.get("event_index")
    if not isinstance(cutoff_event, int) or isinstance(cutoff_event, bool):
        raise FuryFactorizedDecisionEpochError(
            "epoch causal_cutoff.event_index must be integer"
        )
    starts = epoch.get("source_starts")
    if not isinstance(starts, list) or not starts:
        raise FuryFactorizedDecisionEpochError("factorized epoch has no source STARTs")
    declared_count = grouping.get("source_row_count")
    if declared_count != len(starts):
        raise FuryFactorizedDecisionEpochError(
            "factorized epoch source_row_count does not match source STARTs"
        )
    action = _required_mapping(epoch.get("action"), "epoch.action")
    if set(action) != set(ACTION_LANES):
        raise FuryFactorizedDecisionEpochError(
            "factorized action must contain exactly gcd, swing_queue, off_gcd, stance, target"
        )
    for lane in ACTION_LANES:
        item = _required_mapping(action[lane], f"epoch.action.{lane}")
        status = item.get("status")
        if status not in LANE_STATUSES:
            raise FuryFactorizedDecisionEpochError(
                f"epoch.action.{lane}.status is invalid"
            )
        if status == "OBSERVED" and item.get("value") is None:
            raise FuryFactorizedDecisionEpochError(
                f"OBSERVED epoch.action.{lane} must have a value"
            )
        if status != "OBSERVED" and item.get("value") is not None:
            raise FuryFactorizedDecisionEpochError(
                f"non-observed epoch.action.{lane} must have value=None"
            )
    if action["swing_queue"].get("status") != "MISSING":
        raise FuryFactorizedDecisionEpochError(
            "Chronicle V1 swing_queue must remain MISSING"
        )

    maximum_anchor: tuple[int, int] | None = None
    for anchor in _iter_nested_anchors(epoch):
        event_index = anchor.get("event_index")
        csv_line = anchor.get("csv_line")
        offset_ms = anchor.get("offset_ms")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (event_index, csv_line, offset_ms)
        ):
            raise FuryFactorizedDecisionEpochError(
                "every factorized provenance anchor must contain integer indices"
            )
        if event_index > cutoff_event:
            raise FuryFactorizedDecisionEpochError(
                "factorized epoch contains provenance after its causal cutoff"
            )
        if offset_ms != epoch_offset:
            raise FuryFactorizedDecisionEpochError(
                "factorized epoch contains provenance outside its exact-time group"
            )
        anchor_identity = (
            str(anchor.get("source_instance_ref") or expected_identity[0]).strip(),
            str(anchor.get("encounter_id") or expected_identity[1]).strip(),
            str(anchor.get("player_guid") or expected_identity[2]).strip().casefold(),
        )
        if anchor_identity != expected_identity:
            raise FuryFactorizedDecisionEpochError(
                "factorized epoch provenance crosses instance, encounter, or player"
            )
        maximum_anchor = max(maximum_anchor or (event_index, csv_line), (event_index, csv_line))
    cutoff_order = (cutoff_event, cutoff.get("csv_line"))
    if maximum_anchor != cutoff_order:
        raise FuryFactorizedDecisionEpochError(
            "causal cutoff must equal the latest admitted START anchor"
        )


def iter_factorized_decision_epochs(
    records: Iterable[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Stream exact-time groups while retaining at most one group per trajectory."""

    open_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    last_anchor: dict[tuple[str, str, str], tuple[int, int, int]] = {}
    for record in records:
        if record.get("schema") != RECORD_SCHEMA:
            raise FuryFactorizedDecisionEpochError(
                f"factorized input must use row schema {RECORD_SCHEMA}"
            )
        key = _trajectory_key(record)
        anchor = _start_anchor(record)
        order = (anchor["event_index"], anchor["csv_line"], anchor["offset_ms"])
        previous = last_anchor.get(key)
        if previous is not None:
            if order[:2] <= previous[:2]:
                raise FuryFactorizedDecisionEpochError(
                    "START anchors move backward within a trajectory"
                )
            if order[2] < previous[2]:
                raise FuryFactorizedDecisionEpochError(
                    "START offset_ms moves backward within a trajectory"
                )
        last_anchor[key] = order
        group = open_groups.get(key)
        if group is None:
            open_groups[key] = [record]
            continue
        if _start_anchor(group[0])["offset_ms"] == anchor["offset_ms"]:
            group.append(record)
            continue
        yield factorize_start_group(group)
        open_groups[key] = [record]
    for group in sorted(
        open_groups.values(),
        key=lambda values: (
            _start_anchor(values[0])["event_index"],
            _start_anchor(values[0])["csv_line"],
            _trajectory_key(values[0]),
        ),
    ):
        yield factorize_start_group(group)


def _iter_partition_records(partition: Path) -> Iterator[dict[str, Any]]:
    try:
        with gzip.open(partition, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FuryFactorizedDecisionEpochError(
                        f"invalid JSON at {partition}:{line_number}: {error}"
                    ) from error
                if not isinstance(record, dict):
                    raise FuryFactorizedDecisionEpochError(
                        f"compact row is not an object at {partition}:{line_number}"
                    )
                yield record
    except (OSError, UnicodeError) as error:
        raise FuryFactorizedDecisionEpochError(
            f"cannot stream compact partition {partition}: {error}"
        ) from error


def build_fury_factorized_decision_epoch_audit(
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Stream compact partitions once and return an aggregate factorization audit."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(path)
    decision_rows = 0
    epochs = 0
    singleton_epochs = 0
    multi_start_epochs = 0
    grouped_extra_rows = 0
    maximum_source_rows = 0
    parallel_action_lane_epochs = 0
    source_dispositions: Counter[str] = Counter(
        {name: 0 for name in SOURCE_DISPOSITIONS}
    )
    lane_status: dict[str, Counter[str]] = {
        lane: Counter({status: 0 for status in LANE_STATUSES}) for lane in ACTION_LANES
    }
    observed_lane_combinations: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    compressed_bytes = 0
    partition_reports: list[dict[str, Any]] = []

    for partition_index, entry_value in enumerate(manifest["partitions"]):
        if not isinstance(entry_value, Mapping):
            raise FuryFactorizedDecisionEpochError(
                f"partition manifest entry {partition_index} is not an object"
            )
        partition = _partition_path(entry_value, path)
        try:
            partition_bytes = partition.stat().st_size
        except OSError as error:
            raise FuryFactorizedDecisionEpochError(
                f"cannot stat compact partition {partition}: {error}"
            ) from error
        compressed_bytes += partition_bytes
        partition_rows = 0
        partition_epochs = 0
        for epoch in iter_factorized_decision_epochs(_iter_partition_records(partition)):
            validate_factorized_epoch(epoch)
            row_count = int(epoch["grouping"]["source_row_count"])
            decision_rows += row_count
            partition_rows += row_count
            epochs += 1
            partition_epochs += 1
            maximum_source_rows = max(maximum_source_rows, row_count)
            if row_count == 1:
                singleton_epochs += 1
            else:
                multi_start_epochs += 1
                grouped_extra_rows += row_count - 1
            observed_action_lanes: list[str] = []
            for lane in ACTION_LANES:
                status = epoch["action"][lane]["status"]
                lane_status[lane][status] += 1
                if lane in ("gcd", "off_gcd", "stance") and status == "OBSERVED":
                    observed_action_lanes.append(lane)
            if len(observed_action_lanes) >= 2:
                parallel_action_lane_epochs += 1
            combination = "+".join(observed_action_lanes) or "none"
            observed_lane_combinations[combination] += 1
            for disposition in epoch["source_row_dispositions"]:
                source_dispositions[disposition["disposition"]] += 1
            for exclusion in epoch["exclusions"]:
                exclusion_reasons[exclusion["reason"]] += 1
        partition_reports.append(
            {
                "partition": str(partition),
                "compressed_bytes": partition_bytes,
                "decision_rows": partition_rows,
                "factorized_epochs": partition_epochs,
                "streamed_once": True,
            }
        )

    disposition_rows = sum(source_dispositions.values())
    declared_output = manifest.get("output")
    declared_output = declared_output if isinstance(declared_output, Mapping) else {}
    declared_rows = declared_output.get("decision_count")
    declared_rows = declared_rows if isinstance(declared_rows, int) else None
    row_count_matches = declared_rows is None or declared_rows == decision_rows
    source_row_conservation = decision_rows == disposition_rows == epochs + grouped_extra_rows
    structural_ok = row_count_matches and source_row_conservation
    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": _utc_now(),
        "status": "ok" if structural_ok else "failed",
        "input": {
            "manifest": str(path),
            "declared_decision_rows": declared_rows,
            "partition_count": len(partition_reports),
            "compressed_bytes": compressed_bytes,
            "partitions": partition_reports,
            "partitions_streamed_once": True,
        },
        "contract": {
            "epoch_schema": EPOCH_SCHEMA,
            "action_tuple": list(ACTION_LANES),
            "grouping": (
                "exact equal offset_ms only within one instance x encounter x player"
            ),
            "max_group_gap_ms": 0,
            "input_fields_read": ["schema", "identity", "source.start_anchor", "action"],
            "future_fields_not_read": [
                "result",
                "window_until_next_start_candidate",
                "eligibility",
                "state_before",
                "state_mask",
                "state_provenance",
            ],
            "swing_queue": (
                "MISSING; an on-swing server START is execution evidence, not queue intent"
            ),
            "target": (
                "resolved nonself server target only; not target-switch intent"
            ),
            "stance": "known stance START actions are projected into the stance lane",
            "action_semantics": "server_observed_START_candidate_not_result_confirmed",
        },
        "coverage": {
            "decision_rows": decision_rows,
            "factorized_epochs": epochs,
            "singleton_epochs": singleton_epochs,
            "multi_start_epochs": multi_start_epochs,
            "grouped_extra_start_rows": grouped_extra_rows,
            "maximum_source_rows_per_epoch": maximum_source_rows,
            "parallel_action_lane_epochs": parallel_action_lane_epochs,
            "observed_action_lane_combinations": dict(
                sorted(observed_lane_combinations.items())
            ),
            "lane_status": {
                lane: dict(sorted(counts.items())) for lane, counts in lane_status.items()
            },
            "source_row_dispositions": dict(sorted(source_dispositions.items())),
            "source_row_dispositions_total": disposition_rows,
            "exclusion_reasons": dict(sorted(exclusion_reasons.items())),
        },
        "quality": {
            "row_count_matches_manifest": row_count_matches,
            "source_row_conservation": source_row_conservation,
            "future_cutoff_failures": 0,
            "cross_instance_encounter_player_failures": 0,
            "nonzero_gap_groups": 0,
            "queue_intent_inferred_rows": 0,
            "line_level_output_materialized": False,
            "compact_manifest_modified": False,
        },
        "offline_rl_gate": {
            "offline_rl_ready": False,
            "factorized_epoch_contract_ready": structural_ok,
            "blockers": [
                "Chronicle does not expose client next-swing queue intent",
                "START candidates are not treated as future result-confirmed executions",
                "resolved spell targets do not establish target-switch intent",
                "the observation contract still has missing resource and timer state",
                "the observed interval reward remains noncausal and lacks terminal weighting",
            ],
        },
    }


def write_fury_factorized_decision_epoch_audit(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, UnicodeError) as error:
        raise FuryFactorizedDecisionEpochError(
            f"cannot write factorized decision epoch audit {path}: {error}"
        ) from error
    return path


def audit_fury_factorized_decision_epochs(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> tuple[dict[str, Any], Path]:
    report = build_fury_factorized_decision_epoch_audit(manifest_path)
    return report, write_fury_factorized_decision_epoch_audit(report, output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, output = audit_fury_factorized_decision_epochs(
            args.dataset_manifest,
            output_path=args.output,
        )
    except FuryFactorizedDecisionEpochError as error:
        print(f"Fury factorized decision epoch audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "decision_rows": report["coverage"]["decision_rows"],
                "factorized_epochs": report["coverage"]["factorized_epochs"],
                "multi_start_epochs": report["coverage"]["multi_start_epochs"],
                "parallel_action_lane_epochs": report["coverage"][
                    "parallel_action_lane_epochs"
                ],
                "source_row_conservation": report["quality"][
                    "source_row_conservation"
                ],
                "offline_rl_ready": report["offline_rl_gate"]["offline_rl_ready"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 1


__all__ = [
    "ACTION_LANES",
    "AUDIT_SCHEMA",
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "EPOCH_SCHEMA",
    "FuryFactorizedDecisionEpochError",
    "SOURCE_DISPOSITIONS",
    "audit_fury_factorized_decision_epochs",
    "build_fury_factorized_decision_epoch_audit",
    "factorize_start_group",
    "iter_factorized_decision_epochs",
    "main",
    "validate_factorized_epoch",
    "write_fury_factorized_decision_epoch_audit",
]


if __name__ == "__main__":
    raise SystemExit(main())
