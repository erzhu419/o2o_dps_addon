"""Field-level partial-observation contract for O2O-DPS decision records.

The adapter is deliberately in-memory: it validates and projects an existing
decision record without writing another dataset.  ``O2OObservationV1`` is not a
full-state claim.  Only ``state_before``, ``state_mask``,
``state_provenance``, and the START anchor used for leakage validation are
consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


SCHEMA_NAME = "O2OObservationV1"
SCHEMA_VERSION = 1
OBSERVATION_SCOPE = "partial"
STATUSES = ("OBSERVED", "RECONSTRUCTED", "INFERRED", "MISSING")

# O2OObservationV1 is intentionally closed.  Adding a field requires a schema
# revision so identity/label/result fields cannot silently become policy state.
OBSERVATION_FIELDS = (
    "combat_time_ms",
    "recent_uniquely_linked_server_actions",
    "rage_gain_total_chronicle_units",
    "rage_gain_rows_observed",
    "rage_loss_total_chronicle_units",
    "rage_loss_rows_observed",
    "rage_scale_to_wow",
    "last_auto_attack_elapsed_ms",
    "known_player_aura_event_ledger",
    "known_outgoing_target_aura_event_ledger",
    "damaged_target_guids_seen",
    "talent_tree_point_totals",
    "gear_slot_count",
    "absolute_rage",
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
    "queue_intent",
    "client_keypress_ms",
    "target_hp",
    "player_hp",
    "boss_phase",
    "target_count",
    "stance",
    "range",
    "behind_target",
    "gear_item_ids",
    "exact_talent_ranks",
)

FORBIDDEN_STATE_SOURCES = (
    "result",
    "window_until_next_start_candidate",
    "eligibility and behavior labels",
    "identity.player_name",
    "identity.player_guid",
    "identity.leaderboard_rows rank/name/guid",
)


class ObservationContractError(ValueError):
    """A record cannot satisfy the O2OObservationV1 contract."""


def schema_descriptor() -> dict[str, Any]:
    """Return a JSON-serializable descriptor for ``O2OObservationV1``."""

    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "observation_scope": OBSERVATION_SCOPE,
        "full_state": False,
        "source_state_components": [
            "state_before",
            "state_mask",
            "state_provenance",
        ],
        "validation_anchor": "source.start_anchor.event_index",
        "fields": list(OBSERVATION_FIELDS),
        "field_record": {
            "required_keys": ["value", "status", "provenance"],
            "status_enum": list(STATUSES),
        },
        "source_invariants": [
            "MISSING requires value=null, state_mask=false, and provenance.kind=MISSING",
            "non-MISSING requires a value, state_mask=true, and no event_index after START",
        ],
        "forbidden_state_sources": list(FORBIDDEN_STATE_SOURCES),
        "materialization": "in_memory_projection_only",
    }


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservationContractError(f"{path} must be a mapping")
    return value


def _event_index(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ObservationContractError(f"{path} must be an integer")
    return value


def _validate_field_names(
    values: Mapping[str, Any],
    masks: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    expected = set(OBSERVATION_FIELDS)
    for path, component in (
        ("state_before", values),
        ("state_mask", masks),
        ("state_provenance", provenance),
    ):
        names = set(component)
        if names != expected:
            missing = sorted(expected - names)
            extra = sorted(names - expected)
            raise ObservationContractError(
                f"{path} fields do not match {SCHEMA_NAME}; "
                f"missing={missing}, extra={extra}"
            )


def _validate_source_field(
    *,
    name: str,
    value: Any,
    mask: Any,
    provenance: Mapping[str, Any],
    start_event_index: int,
) -> str:
    if not isinstance(mask, bool):
        raise ObservationContractError(f"state_mask.{name} must be boolean")
    status = provenance.get("kind")
    if status not in STATUSES:
        raise ObservationContractError(
            f"state_provenance.{name}.kind must be one of {STATUSES}"
        )

    evidence_index = provenance.get("event_index")
    if evidence_index is not None:
        evidence_index = _event_index(
            evidence_index, f"state_provenance.{name}.event_index"
        )

    if status == "MISSING":
        if value is not None or mask is not False:
            raise ObservationContractError(
                f"MISSING field {name} must have value=None and mask=false"
            )
        if evidence_index is not None:
            raise ObservationContractError(
                f"MISSING field {name} must not reference an event_index"
            )
        return status

    if value is None or mask is not True:
        raise ObservationContractError(
            f"non-MISSING field {name} must have a value and mask=true"
        )
    if evidence_index is not None and evidence_index > start_event_index:
        raise ObservationContractError(
            f"future state evidence for {name}: event_index {evidence_index} "
            f"is after START {start_event_index}"
        )
    return status


def project_decision_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one compact decision record into ``O2OObservationV1``.

    Result/window/label and leaderboard identity data are intentionally not
    inspected.  The returned object is a partial observation with one
    ``{value, status, provenance}`` object per fixed state field.
    """

    record = _mapping(record, "record")
    values = _mapping(record.get("state_before"), "state_before")
    masks = _mapping(record.get("state_mask"), "state_mask")
    provenance = _mapping(record.get("state_provenance"), "state_provenance")
    source = _mapping(record.get("source"), "source")
    start_anchor = _mapping(source.get("start_anchor"), "source.start_anchor")
    start_index = _event_index(
        start_anchor.get("event_index"), "source.start_anchor.event_index"
    )

    _validate_field_names(values, masks, provenance)
    fields: dict[str, dict[str, Any]] = {}
    for name in OBSERVATION_FIELDS:
        field_provenance = _mapping(
            provenance[name], f"state_provenance.{name}"
        )
        status = _validate_source_field(
            name=name,
            value=values[name],
            mask=masks[name],
            provenance=field_provenance,
            start_event_index=start_index,
        )
        fields[name] = {
            "value": deepcopy(values[name]),
            "status": status,
            "provenance": deepcopy(dict(field_provenance)),
        }

    observation = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "observation_scope": OBSERVATION_SCOPE,
        "start_event_index": start_index,
        "fields": fields,
    }
    validate_observation(observation)
    return observation


def validate_observation(observation: Mapping[str, Any]) -> None:
    """Validate an already projected ``O2OObservationV1`` object."""

    observation = _mapping(observation, "observation")
    required_keys = {
        "schema",
        "schema_version",
        "observation_scope",
        "start_event_index",
        "fields",
    }
    if set(observation) != required_keys:
        raise ObservationContractError(
            "observation must contain only schema/schema_version/"
            "observation_scope/start_event_index/fields"
        )
    if observation.get("schema") != SCHEMA_NAME:
        raise ObservationContractError(f"observation.schema must be {SCHEMA_NAME}")
    if observation.get("schema_version") != SCHEMA_VERSION:
        raise ObservationContractError(
            f"observation.schema_version must be {SCHEMA_VERSION}"
        )
    if observation.get("observation_scope") != OBSERVATION_SCOPE:
        raise ObservationContractError("observation must declare partial scope")
    start_index = _event_index(
        observation.get("start_event_index"), "observation.start_event_index"
    )
    fields = _mapping(observation.get("fields"), "observation.fields")
    expected = set(OBSERVATION_FIELDS)
    if set(fields) != expected:
        raise ObservationContractError(
            f"observation.fields must contain exactly the {SCHEMA_NAME} fields"
        )

    for name in OBSERVATION_FIELDS:
        field = _mapping(fields[name], f"observation.fields.{name}")
        if set(field) != {"value", "status", "provenance"}:
            raise ObservationContractError(
                f"observation.fields.{name} must contain only value/status/provenance"
            )
        provenance = _mapping(
            field.get("provenance"), f"observation.fields.{name}.provenance"
        )
        status = field.get("status")
        if status not in STATUSES or provenance.get("kind") != status:
            raise ObservationContractError(
                f"observation.fields.{name} status must match provenance.kind"
            )
        evidence_index = provenance.get("event_index")
        if evidence_index is not None:
            evidence_index = _event_index(
                evidence_index,
                f"observation.fields.{name}.provenance.event_index",
            )
        if status == "MISSING":
            if field.get("value") is not None:
                raise ObservationContractError(
                    f"MISSING observation field {name} must have value=None"
                )
            if evidence_index is not None:
                raise ObservationContractError(
                    f"MISSING observation field {name} must not reference event_index"
                )
        else:
            if field.get("value") is None:
                raise ObservationContractError(
                    f"non-MISSING observation field {name} must have a value"
                )
            if evidence_index is not None and evidence_index > start_index:
                raise ObservationContractError(
                    f"future state evidence for {name}: event_index {evidence_index} "
                    f"is after START {start_index}"
                )


__all__ = [
    "OBSERVATION_FIELDS",
    "OBSERVATION_SCOPE",
    "ObservationContractError",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "STATUSES",
    "project_decision_record",
    "schema_descriptor",
    "validate_observation",
]
