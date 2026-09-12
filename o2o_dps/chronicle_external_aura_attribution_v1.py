"""Attribute Chronicle armor-aura additions without inventing removal casters.

This is an additive, diagnostic Stage-7 sidecar.  It joins an ``AURA``
``StateAdded`` row to an official ``AURA_CAST`` row only when exactly one cast
has the same instance, encounter, target, spell ID and registered aura identity
and strictly precedes the state row by at most 100 ms.  Removal and modification
rows never inherit the nearest caster.  They only close or update the active
stack lifecycle accumulated from prior, individually attributed additions.

The output is not a policy, comparison, or dynamic-armor input.  In particular,
Bonereaver's Edge is retained as a player self-buff/armor-ignore observation and
is never projected as a hostile-target flat armor reduction.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import chronicle_external_state_event_normalizer_v1 as state_v1
from .fury_offline_scenario_capsule_v2 import ARMOR_DEBUFF_REGISTRY


SCHEMA = "chronicle_external_aura_attribution/v1"
EVENT_SCHEMA = "chronicle_external_aura_attribution_event/v1"
STATUS = "STAGE7_AURA_ATTRIBUTION_DIAGNOSTIC_NONVOTING"
IMPLEMENTATION_REVISION = "unique_preceding_100ms_add_only_lifecycle_v2"
DEFAULT_JOIN_WINDOW_MS = 100
BONEREAVER_SPELL_ID = 21153


class ChronicleExternalAuraAttributionError(RuntimeError):
    """The state sidecar cannot support an exact bounded attribution scan."""


@dataclass(frozen=True)
class _AuraCastCandidate:
    order_key: tuple[int, int, int, int, int]
    timestamp_ms: int
    source_guid: str | None
    anchor: Mapping[str, Any]


@dataclass
class _Lifecycle:
    current_amount: int = 0
    stack_owners: list[str | None] = field(default_factory=list)
    attributed_addition_count: int = 0
    unresolved_addition_count: int = 0
    attributed_refresh_count: int = 0
    unbound_update_count: int = 0


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalAuraAttributionError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalAuraAttributionError(f"{label} must be nonempty text")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalAuraAttributionError(f"{label} must be an integer")
    return value


def _aura_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, raw in ARMOR_DEBUFF_REGISTRY.items():
        debuff_id = _text(_mapping(raw, "armor registry row").get("debuff_id"), "debuff_id")
        key = name.casefold()
        prior = aliases.get(key)
        if prior is not None and prior != debuff_id:
            raise ChronicleExternalAuraAttributionError(
                f"armor aura alias {name!r} maps to multiple debuff IDs"
            )
        aliases[key] = debuff_id
    return aliases


ARMOR_AURA_ALIASES = _aura_aliases()


def _identity(row: Mapping[str, Any]) -> tuple[str, int] | None:
    name = _optional_text(row.get("spell"))
    spell_id = row.get("spell_id")
    if name is None or isinstance(spell_id, bool) or not isinstance(spell_id, int):
        return None
    debuff_id = ARMOR_AURA_ALIASES.get(name.casefold())
    if debuff_id is None:
        return None
    return debuff_id, spell_id


def _state_name(row: Mapping[str, Any]) -> str:
    payload = _mapping(row.get("state_payload"), "state_payload")
    state = _mapping(payload.get("state"), "state_payload.state")
    return _text(state.get("name"), "state_payload.state.name")


def _is_buff(row: Mapping[str, Any]) -> bool:
    value = _mapping(row.get("state_payload"), "state_payload").get("is_buff")
    if not isinstance(value, bool):
        raise ChronicleExternalAuraAttributionError("state_payload.is_buff must be boolean")
    return value


def _order_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    stream = _text(row.get("stream_type"), "stream_type")
    if stream not in {"aura", "aura_cast"}:
        raise ChronicleExternalAuraAttributionError(
            f"unsupported Stage-7 stream {stream!r}"
        )
    meta = _mapping(row.get("event_meta"), "event_meta")
    provenance = _mapping(row.get("provenance"), "provenance")
    return (
        _integer(row.get("timestamp_ms"), "timestamp_ms"),
        _integer(row.get("event_index"), "event_index"),
        state_v1.STREAM_ORDER[stream],
        _integer(provenance.get("frame_index"), "provenance.frame_index"),
        _integer(
            provenance.get("frame_message_index"),
            "provenance.frame_message_index",
        ),
    )


def _anchor(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _mapping(row.get("provenance"), "provenance")
    return {
        "timestamp_ms": _integer(row.get("timestamp_ms"), "timestamp_ms"),
        "offset_ms": _integer(row.get("offset_ms"), "offset_ms"),
        "event_index": _integer(row.get("event_index"), "event_index"),
        "stream_type": _text(row.get("stream_type"), "stream_type"),
        "frame_index": _integer(provenance.get("frame_index"), "frame_index"),
        "frame_message_index": _integer(
            provenance.get("frame_message_index"), "frame_message_index"
        ),
        "official_message_sha256": _mapping(row.get("official"), "official").get(
            "message_sha256"
        ),
    }


def _owner_counts(owners: Iterable[str | None]) -> dict[str, int]:
    return dict(sorted(Counter(owner for owner in owners if owner is not None).items()))


class AuraAttributionScannerV1:
    """Streaming exact-key AuraCast/Aura join with bounded state."""

    def __init__(self, *, join_window_ms: int = DEFAULT_JOIN_WINDOW_MS) -> None:
        if isinstance(join_window_ms, bool) or not isinstance(join_window_ms, int):
            raise TypeError("join_window_ms must be an integer")
        if join_window_ms < 1:
            raise ChronicleExternalAuraAttributionError(
                "join_window_ms must be positive"
            )
        self.join_window_ms = join_window_ms
        self._scope: tuple[str, str] | None = None
        self._closed_scopes: set[tuple[str, str]] = set()
        self._last_order: tuple[int, int, int, int, int] | None = None
        self._casts: dict[
            tuple[str, str, str, str, int], deque[_AuraCastCandidate]
        ] = defaultdict(deque)
        self._lifecycles: dict[tuple[str, str, str, str, int], _Lifecycle] = {}
        self._counts: Counter[str] = Counter()
        self._spell_counts: Counter[str] = Counter()
        self._added_status_by_debuff: Counter[tuple[str, str]] = Counter()
        self._open_at_scope_end = 0

    @staticmethod
    def _join_key(
        row: Mapping[str, Any], identity: tuple[str, int]
    ) -> tuple[str, str, str, str, int]:
        return (
            _text(row.get("instance"), "instance"),
            _text(row.get("encounter"), "encounter"),
            _text(row.get("target_guid"), "target_guid"),
            identity[0],
            identity[1],
        )

    def _enter_scope(self, row: Mapping[str, Any]) -> None:
        scope = (
            _text(row.get("instance"), "instance"),
            _text(row.get("encounter"), "encounter"),
        )
        if scope == self._scope:
            return
        if scope in self._closed_scopes:
            raise ChronicleExternalAuraAttributionError(
                "an encounter reappeared after its streaming scope closed"
            )
        if self._scope is not None:
            self._closed_scopes.add(self._scope)
            self._open_at_scope_end += sum(
                lifecycle.current_amount > 0 for lifecycle in self._lifecycles.values()
            )
        self._scope = scope
        self._last_order = None
        self._casts.clear()
        self._lifecycles.clear()

    def consume(self, raw_row: Mapping[str, Any]) -> dict[str, Any] | None:
        row = _mapping(raw_row, "state event row")
        stream = row.get("stream_type")
        if stream not in {"aura", "aura_cast"}:
            return None
        identity = _identity(row)
        if identity is None:
            return None
        self._enter_scope(row)
        order = _order_key(row)
        if self._last_order is not None and order <= self._last_order:
            raise ChronicleExternalAuraAttributionError(
                "aura sidecar rows are not in strict EventMeta order"
            )
        self._last_order = order
        self._spell_counts[identity[0]] += 1
        if _optional_text(row.get("target_guid")) is None:
            self._counts["armor_event_missing_target_rejected"] += 1
            return None
        key = self._join_key(row, identity)
        if stream == "aura_cast":
            self._consume_cast(row, key, order)
            return None
        return self._consume_aura(row, key, identity, order)

    def _consume_cast(
        self,
        row: Mapping[str, Any],
        key: tuple[str, str, str, str, int],
        order: tuple[int, int, int, int, int],
    ) -> None:
        timestamp = order[0]
        candidates = self._casts[key]
        while candidates and timestamp - candidates[0].timestamp_ms > self.join_window_ms:
            candidates.popleft()
        candidates.append(
            _AuraCastCandidate(
                order_key=order,
                timestamp_ms=timestamp,
                source_guid=_optional_text(row.get("source_guid")),
                anchor=_anchor(row),
            )
        )
        self._counts["armor_aura_cast_seen"] += 1

    def _matching_casts(
        self,
        key: tuple[str, str, str, str, int],
        order: tuple[int, int, int, int, int],
    ) -> list[_AuraCastCandidate]:
        candidates = self._casts[key]
        while candidates and order[0] - candidates[0].timestamp_ms > self.join_window_ms:
            candidates.popleft()
        return [
            candidate
            for candidate in candidates
            if candidate.order_key < order
            and 0 <= order[0] - candidate.timestamp_ms <= self.join_window_ms
        ]

    def _consume_aura(
        self,
        row: Mapping[str, Any],
        key: tuple[str, str, str, str, int],
        identity: tuple[str, int],
        order: tuple[int, int, int, int, int],
    ) -> dict[str, Any]:
        state = _state_name(row)
        current_amount = _integer(row.get("value"), "aura current_amount")
        if current_amount < 0:
            raise ChronicleExternalAuraAttributionError(
                "aura current_amount cannot be negative"
            )
        lifecycle = self._lifecycles.setdefault(key, _Lifecycle())
        prior_amount = lifecycle.current_amount
        source_guid: str | None = None
        matched_anchor: Mapping[str, Any] | None = None
        candidate_count = 0
        if state == "StateAdded":
            candidates = self._matching_casts(key, order)
            candidate_count = len(candidates)
            if candidate_count == 1 and candidates[0].source_guid is not None:
                chosen = candidates[0]
                source_guid = chosen.source_guid
                matched_anchor = chosen.anchor
                self._casts[key].remove(chosen)
                attribution_status = "UNIQUE_PRECEDING_AURA_CAST_ATTRIBUTED"
                self._counts["state_added_attributed_unique"] += 1
            elif candidate_count == 1:
                attribution_status = "UNIQUE_AURA_CAST_MISSING_CASTER_REJECTED"
                self._counts["state_added_missing_caster_rejected"] += 1
            elif candidate_count == 0:
                attribution_status = "NO_PRECEDING_AURA_CAST_WITHIN_WINDOW"
                self._counts["state_added_unmatched"] += 1
            else:
                attribution_status = "AMBIGUOUS_PRECEDING_AURA_CASTS_REJECTED"
                self._counts["state_added_ambiguous_rejected"] += 1
            lifecycle_status = self._apply_added(
                lifecycle, current_amount=current_amount, source_guid=source_guid
            )
            self._added_status_by_debuff[(identity[0], attribution_status)] += 1
        elif state == "StateRemoved":
            attribution_status = "STATE_REMOVED_CASTER_NOT_IN_AURA_SCHEMA"
            lifecycle_status = "ACTIVE_LIFECYCLE_CLOSED_NO_REMOVAL_CASTER_GUESS"
            self._counts["state_removed_no_direct_caster"] += 1
        elif state in {"StateModified", "StateUnknown"}:
            attribution_status = "NONADDITION_STATE_CASTER_NOT_ATTRIBUTED"
            lifecycle_status = self._apply_unbound_update(
                lifecycle, current_amount=current_amount
            )
            self._counts["nonaddition_state_unattributed"] += 1
        else:
            raise ChronicleExternalAuraAttributionError(
                f"unsupported AuraState name {state!r}"
            )

        before_close_owners = list(lifecycle.stack_owners)
        before_close_unresolved = sum(owner is None for owner in before_close_owners)
        closed = state == "StateRemoved"
        if closed:
            lifecycle.current_amount = 0
            lifecycle.stack_owners.clear()
            if before_close_owners:
                self._counts["state_removed_closed_active_lifecycle"] += 1
            else:
                self._counts["state_removed_without_active_lifecycle"] += 1
        role = self._role(row, identity, source_guid)
        self._counts["armor_aura_state_seen"] += 1
        self._counts[f"role::{role}"] += 1
        return {
            "schema": EVENT_SCHEMA,
            "status": STATUS,
            "instance": key[0],
            "encounter": key[1],
            "target_guid": key[2],
            "debuff_id": identity[0],
            "spell_id": identity[1],
            "spell": row.get("spell"),
            "aura_state": state,
            "current_amount": current_amount,
            "is_buff": _is_buff(row),
            "aura_role": role,
            "aura_anchor": _anchor(row),
            "attribution": {
                "status": attribution_status,
                "source_guid": source_guid,
                "owner_or_controller_resolved": False,
                "candidate_count": candidate_count,
                "matched_aura_cast_anchor": matched_anchor,
                "join_rule": (
                    "same instance+encounter+target+registered aura identity+spell_id; "
                    "strictly preceding by 0..100ms; exactly one candidate"
                ),
            },
            "lifecycle": {
                "status": lifecycle_status,
                "prior_amount": prior_amount,
                "current_amount": 0 if closed else lifecycle.current_amount,
                "active_attributed_stack_owner_counts": (
                    {} if closed else _owner_counts(lifecycle.stack_owners)
                ),
                "active_unresolved_stack_count": (
                    0 if closed else sum(owner is None for owner in lifecycle.stack_owners)
                ),
                "closed": closed,
                "closed_attributed_stack_owner_counts": (
                    _owner_counts(before_close_owners) if closed else {}
                ),
                "closed_unresolved_stack_count": (
                    before_close_unresolved if closed else 0
                ),
            },
            "projection_contract": {
                "focal_guid_loo_ready": False,
                "hostile_flat_armor_reduction_authorized": False,
                "bonereaver_hostile_reduction_forbidden": (
                    identity[1] == BONEREAVER_SPELL_ID
                ),
            },
        }

    def _apply_added(
        self,
        lifecycle: _Lifecycle,
        *,
        current_amount: int,
        source_guid: str | None,
    ) -> str:
        prior = lifecycle.current_amount
        if current_amount <= 0:
            lifecycle.current_amount = 0
            lifecycle.stack_owners.clear()
            lifecycle.unbound_update_count += 1
            return "INVALID_ZERO_AMOUNT_ADDITION_RESET_UNRESOLVED"
        if current_amount < prior:
            lifecycle.current_amount = current_amount
            lifecycle.stack_owners = [None] * current_amount
            lifecycle.unbound_update_count += 1
            return "NONMONOTONIC_ADDITION_RESET_TO_UNRESOLVED_STACKS"
        delta = current_amount - prior
        if delta == 0:
            if source_guid is not None:
                lifecycle.attributed_refresh_count += 1
                return "ATTRIBUTED_REFRESH_NO_STACK_OWNER_REWRITE"
            lifecycle.unbound_update_count += 1
            return "UNATTRIBUTED_REFRESH_NO_STACK_OWNER_REWRITE"
        if source_guid is None:
            lifecycle.stack_owners.extend([None] * delta)
            lifecycle.unresolved_addition_count += delta
            lifecycle.current_amount = current_amount
            return "UNATTRIBUTED_NEW_STACKS_RETAINED_AS_UNRESOLVED"
        lifecycle.stack_owners.append(source_guid)
        lifecycle.attributed_addition_count += 1
        if delta > 1:
            lifecycle.stack_owners.extend([None] * (delta - 1))
            lifecycle.unresolved_addition_count += delta - 1
            status = "ONE_CAST_ATTRIBUTED_REMAINING_STACK_JUMP_UNRESOLVED"
        else:
            status = "NEW_STACK_ATTRIBUTED_TO_UNIQUE_CAST"
        lifecycle.current_amount = current_amount
        return status

    @staticmethod
    def _apply_unbound_update(
        lifecycle: _Lifecycle, *, current_amount: int
    ) -> str:
        prior = lifecycle.current_amount
        if current_amount > prior:
            lifecycle.stack_owners.extend([None] * (current_amount - prior))
        elif current_amount < prior:
            # The aggregate Aura message does not identify which owner's stack
            # disappeared.  Keeping an arbitrary prefix would fabricate that
            # ownership, so every surviving stack becomes unresolved.
            lifecycle.stack_owners = [None] * current_amount
        lifecycle.current_amount = current_amount
        lifecycle.unbound_update_count += 1
        return "STATE_UPDATED_WITHOUT_CASTER_ATTRIBUTION"

    @staticmethod
    def _role(
        row: Mapping[str, Any], identity: tuple[str, int], source_guid: str | None
    ) -> str:
        if identity[1] != BONEREAVER_SPELL_ID:
            return "REGISTERED_TARGET_ARMOR_AURA_NOT_YET_LOO_PROJECTABLE"
        target = _optional_text(row.get("target_guid"))
        if _is_buff(row) and (source_guid is None or source_guid == target):
            return "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE"
        return "BONEREAVER_CONTRACT_CONFLICT_NONVOTING"

    def finish(self) -> dict[str, Any]:
        open_count = self._open_at_scope_end + sum(
            lifecycle.current_amount > 0 for lifecycle in self._lifecycles.values()
        )
        counts = dict(sorted(self._counts.items()))
        added = sum(
            counts.get(key, 0)
            for key in (
                "state_added_attributed_unique",
                "state_added_missing_caster_rejected",
                "state_added_unmatched",
                "state_added_ambiguous_rejected",
            )
        )
        return {
            "schema": SCHEMA,
            "status": STATUS,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "join_window_ms": self.join_window_ms,
            "counts": counts,
            "armor_aura_name_counts": dict(sorted(self._spell_counts.items())),
            "state_added_status_by_debuff": {
                debuff_id: {
                    status: self._added_status_by_debuff[(debuff_id, status)]
                    for status in sorted(
                        child_status
                        for child_debuff, child_status in self._added_status_by_debuff
                        if child_debuff == debuff_id
                    )
                }
                for debuff_id in sorted(
                    {key[0] for key in self._added_status_by_debuff}
                )
            },
            "state_added_resolution": {
                "total": added,
                "unique_attributed": counts.get("state_added_attributed_unique", 0),
                "ambiguous_rejected": counts.get(
                    "state_added_ambiguous_rejected", 0
                ),
                "unmatched": counts.get("state_added_unmatched", 0),
                "missing_caster_rejected": counts.get(
                    "state_added_missing_caster_rejected", 0
                ),
            },
            "open_lifecycle_count_at_scope_end": open_count,
            "claim_boundary": {
                "policy_input_authorized": False,
                "comparison_input_authorized": False,
                "training_authorized": False,
                "dynamic_armor_projection_authorized": False,
                "state_removed_caster_inferred": False,
                "direct_source_guid_only": True,
                "owner_or_controller_resolution_attempted": False,
                "factual_shared_stack_schedule_is_counterfactual_loo": False,
                "bonereaver_is_hostile_flat_armor_reduction": False,
            },
        }


def attribute_state_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    join_window_ms: int = DEFAULT_JOIN_WINDOW_MS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attribute an ordered state-sidecar iterable and retain only armor AURAs."""

    scanner = AuraAttributionScannerV1(join_window_ms=join_window_ms)
    output: list[dict[str, Any]] = []
    for row in rows:
        attributed = scanner.consume(row)
        if attributed is not None:
            output.append(attributed)
    return output, scanner.finish()


def iter_state_partition(path: str | Path) -> Iterator[Mapping[str, Any]]:
    """Stream normalized state rows from one gzip JSONL partition."""

    import gzip

    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ChronicleExternalAuraAttributionError(
                    f"invalid state JSONL at line {line_number}: {error}"
                ) from error
            yield _mapping(row, f"state JSONL line {line_number}")


def _raw_rows(
    instance: Mapping[str, Any], *, raw_root: Path
) -> Iterator[Mapping[str, Any]]:
    instance_id = _text(instance.get("instance_id"), "instance_id")
    streams = _mapping(instance.get("streams"), "instance.streams")
    decoded: list[tuple[tuple[int, int, int, int, int, str], dict[str, Any]]] = []
    encounter_origins: dict[str, int] = {}
    for stream in ("aura", "aura_cast"):
        wrapper = _mapping(streams.get(stream), f"streams.{stream}")
        if wrapper.get("status") != "AVAILABLE":
            raise ChronicleExternalAuraAttributionError(
                f"instance {instance_id} lacks available {stream} stream"
            )
        reference = _mapping(wrapper.get("object"), f"streams.{stream}.object")
        compressed, _ = state_v1._read_object_reference(
            raw_root, reference, label=f"{instance_id}.{stream}"
        )
        frames = state_v1.decode_event_stream(compressed, stream_type=stream)
        for frame_index, frame in enumerate(frames):
            encounter = _text(frame.get("encounter_id"), "encounter_id")
            origin = _integer(frame.get("first_timestamp_ms"), "first_timestamp_ms")
            previous = encounter_origins.get(encounter)
            if previous is None or (previous == 0 and origin != 0):
                encounter_origins[encounter] = origin
            elif previous != origin and origin != 0 and previous != 0:
                raise ChronicleExternalAuraAttributionError(
                    f"encounter {encounter} has conflicting stream origins"
                )
            for message_index, wrapped in enumerate(frame["messages"]):
                event = _mapping(wrapped.get("event"), "decoded event")
                meta = _mapping(event.get("meta"), "decoded EventMeta")
                offset = _integer(meta.get("offset_ms"), "EventMeta.offset_ms")
                event_index = _integer(meta.get("event_index"), "EventMeta.event_index")
                spell, spell_id, _ = state_v1._spell_projection(event, stream_type=stream)
                if (
                    spell is None
                    or isinstance(spell_id, bool)
                    or not isinstance(spell_id, int)
                    or spell.casefold() not in ARMOR_AURA_ALIASES
                ):
                    continue
                source_guid = (
                    _optional_text(event.get("caster")) if stream == "aura_cast" else None
                )
                target_guid = _optional_text(event.get("target"))
                row = {
                    "instance": instance_id,
                    "encounter": encounter,
                    "timestamp_ms": origin + offset,
                    "offset_ms": offset,
                    "event_index": event_index,
                    "stream_type": stream,
                    "source_guid": source_guid,
                    "target_guid": target_guid,
                    "spell": spell,
                    "spell_id": spell_id,
                    "value": (
                        int(event["current_amount"]) if stream == "aura" else None
                    ),
                    "event_meta": meta,
                    "state_payload": {
                        key: value for key, value in event.items() if key != "meta"
                    },
                    "official": {
                        "message": event,
                        "message_sha256": wrapped.get("message_sha256"),
                    },
                    "provenance": {
                        "frame_index": frame_index,
                        "frame_message_index": message_index,
                    },
                }
                decoded.append(
                    (
                        (
                            origin + offset,
                            event_index,
                            state_v1.STREAM_ORDER[stream],
                            frame_index,
                            message_index,
                            encounter,
                        ),
                        row,
                    )
                )
    encounter_order = {
        encounter: index
        for index, (encounter, _origin) in enumerate(
            sorted(encounter_origins.items(), key=lambda item: (item[1], item[0]))
        )
    }
    decoded.sort(key=lambda item: (encounter_order[item[1]["encounter"]], item[0]))
    for _order, row in decoded:
        yield row


def scan_raw_manifest_shard(
    *,
    source_manifest_path: str | Path,
    data_root: str | Path,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    """Scan one deterministic instance shard without materializing raw rows."""

    if not 0 <= shard_index < shard_count:
        raise ChronicleExternalAuraAttributionError("invalid shard index/count")
    source_path = Path(source_manifest_path).expanduser().resolve()
    source, _source_bytes, source_sha = state_v1._load_source_manifest(source_path)
    raw_root = state_v1._resolve_raw_root(Path(data_root))
    instances = sorted(
        (_mapping(row, "source instance") for row in source["instances"]),
        key=lambda row: _text(row.get("instance_id"), "instance_id"),
    )
    selected = instances[shard_index::shard_count]
    per_instance: list[dict[str, Any]] = []
    total = Counter()
    spell_total = Counter()
    added_status_total: Counter[tuple[str, str]] = Counter()
    for instance in selected:
        scanner = AuraAttributionScannerV1()
        for row in _raw_rows(instance, raw_root=raw_root):
            scanner.consume(row)
        summary = scanner.finish()
        total.update(summary["counts"])
        spell_total.update(summary["armor_aura_name_counts"])
        for debuff_id, statuses in summary["state_added_status_by_debuff"].items():
            for status, count in statuses.items():
                added_status_total[(debuff_id, status)] += count
        per_instance.append(
            {
                "instance_id": instance["instance_id"],
                "counts": summary["counts"],
                "state_added_resolution": summary["state_added_resolution"],
                "open_lifecycle_count_at_scope_end": summary[
                    "open_lifecycle_count_at_scope_end"
                ],
            }
        )
    added_total = sum(
        total.get(key, 0)
        for key in (
            "state_added_attributed_unique",
            "state_added_missing_caster_rejected",
            "state_added_unmatched",
            "state_added_ambiguous_rejected",
        )
    )
    return {
        "schema": SCHEMA + "/raw_manifest_shard_scan",
        "status": STATUS,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "source_manifest_sha256": source_sha,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "instance_count": len(selected),
        "instance_ids": [str(row["instance_id"]) for row in selected],
        "counts": dict(sorted(total.items())),
        "armor_aura_name_counts": dict(sorted(spell_total.items())),
        "state_added_status_by_debuff": {
            debuff_id: {
                status: added_status_total[(debuff_id, status)]
                for status in sorted(
                    child_status
                    for child_debuff, child_status in added_status_total
                    if child_debuff == debuff_id
                )
            }
            for debuff_id in sorted({key[0] for key in added_status_total})
        },
        "state_added_resolution": {
            "total": added_total,
            "unique_attributed": total.get("state_added_attributed_unique", 0),
            "ambiguous_rejected": total.get("state_added_ambiguous_rejected", 0),
            "unmatched": total.get("state_added_unmatched", 0),
            "missing_caster_rejected": total.get(
                "state_added_missing_caster_rejected", 0
            ),
        },
        "per_instance": per_instance,
        "network_request_count": 0,
        "raw_object_copy_count": 0,
        "claim_boundary": {
            "policy_input_authorized": False,
            "comparison_input_authorized": False,
            "training_authorized": False,
            "dynamic_armor_projection_authorized": False,
            "direct_source_guid_only": True,
            "owner_or_controller_resolution_attempted": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    partition = sub.add_parser("scan-partition")
    partition.add_argument("--partition", type=Path, required=True)
    partition.add_argument("--join-window-ms", type=int, default=DEFAULT_JOIN_WINDOW_MS)
    raw = sub.add_parser("scan-raw-manifest")
    raw.add_argument("--source-manifest", type=Path, required=True)
    raw.add_argument("--data-root", type=Path, required=True)
    raw.add_argument("--shard-index", type=int, default=0)
    raw.add_argument("--shard-count", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan-partition":
            _rows, result = attribute_state_rows(
                iter_state_partition(args.partition),
                join_window_ms=args.join_window_ms,
            )
        else:
            result = scan_raw_manifest_shard(
                source_manifest_path=args.source_manifest,
                data_root=args.data_root,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
            )
    except ChronicleExternalAuraAttributionError as error:
        print(f"Chronicle Stage-7 aura attribution failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BONEREAVER_SPELL_ID",
    "DEFAULT_JOIN_WINDOW_MS",
    "EVENT_SCHEMA",
    "SCHEMA",
    "STATUS",
    "AuraAttributionScannerV1",
    "ChronicleExternalAuraAttributionError",
    "attribute_state_rows",
    "iter_state_partition",
    "scan_raw_manifest_shard",
]
