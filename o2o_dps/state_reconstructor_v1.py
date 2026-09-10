"""Prefix-only state reconstruction for compact Chronicle decisions.

V1 deliberately upgrades only Warrior stance.  A stance becomes available for
later decisions after a uniquely associated successful stance GO.  All other
missing fields retain their existing value, mask, and provenance.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
import heapq
from typing import Any

from .o2o_observation import project_decision_record


STANCE_BY_SPELL_ID = {
    2457: "battle",
    71: "defensive",
    2458: "berserker",
}


class StateReconstructionError(ValueError):
    """A compact decision stream cannot satisfy the V1 temporal contract."""


class FutureStateEvidenceError(StateReconstructionError):
    """State evidence references an event after the current START."""


@dataclass(order=True)
class _PendingStance:
    event_index: int
    sequence: int
    value: str | None = field(compare=False)
    provenance: dict[str, Any] | None = field(compare=False)


@dataclass
class _PlayerEncounterState:
    last_start_event_index: int | None = None
    stance: str | None = None
    stance_provenance: dict[str, Any] | None = None
    pending: list[_PendingStance] = field(default_factory=list)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StateReconstructionError(f"{path} must be a mapping")
    return value


def _event_index(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateReconstructionError(f"{path} must be an integer")
    return value


def _start_event_index(record: Mapping[str, Any]) -> int:
    source = _mapping(record.get("source"), "source")
    anchor = _mapping(source.get("start_anchor"), "source.start_anchor")
    return _event_index(
        anchor.get("event_index"), "source.start_anchor.event_index"
    )


def _validate_nested_event_indices(
    value: Any,
    *,
    path: str,
    start_event_index: int,
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(key, str) and (
                key == "event_index" or key.endswith("_event_index")
            ):
                if child is None:
                    continue
                evidence_index = _event_index(child, child_path)
                if evidence_index > start_event_index:
                    raise FutureStateEvidenceError(
                        f"future state evidence at {child_path}: event_index "
                        f"{evidence_index} is after START {start_event_index}"
                    )
            else:
                _validate_nested_event_indices(
                    child,
                    path=child_path,
                    start_event_index=start_event_index,
                )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_nested_event_indices(
                child,
                path=f"{path}[{index}]",
                start_event_index=start_event_index,
            )


def validate_state_evidence_cutoff(record: Mapping[str, Any]) -> None:
    """Reject state evidence whose event anchor occurs after this START.

    Result and outcome windows are intentionally excluded: they are labels, not
    policy state.  Both field provenance and event anchors nested in state
    values are checked.
    """

    record = _mapping(record, "record")
    start_event_index = _start_event_index(record)
    for component_name in ("state_before", "state_provenance"):
        component = _mapping(record.get(component_name), component_name)
        _validate_nested_event_indices(
            component,
            path=component_name,
            start_event_index=start_event_index,
        )


class StateReconstructorV1:
    """Project a chronological compact-decision stream into observations."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _PlayerEncounterState] = {}
        self._transition_sequence = 0

    @staticmethod
    def _stream_key(record: Mapping[str, Any]) -> tuple[str, str]:
        identity = _mapping(record.get("identity"), "identity")
        player_guid = str(identity.get("player_guid") or "").strip()
        encounter_id = str(identity.get("encounter_id") or "").strip()
        if not player_guid or not encounter_id:
            raise StateReconstructionError(
                "identity.player_guid and identity.encounter_id are required"
            )
        return player_guid.casefold(), encounter_id

    @staticmethod
    def _apply_prior_transitions(
        state: _PlayerEncounterState, start_event_index: int
    ) -> None:
        while state.pending and state.pending[0].event_index < start_event_index:
            transition = heapq.heappop(state.pending)
            state.stance = transition.value
            state.stance_provenance = transition.provenance

    @staticmethod
    def _with_reconstructed_stance(
        record: Mapping[str, Any], state: _PlayerEncounterState
    ) -> Mapping[str, Any]:
        if state.stance is None or state.stance_provenance is None:
            return record

        values = _mapping(record.get("state_before"), "state_before")
        masks = _mapping(record.get("state_mask"), "state_mask")
        provenance = _mapping(record.get("state_provenance"), "state_provenance")
        stance_provenance = _mapping(
            provenance.get("stance"), "state_provenance.stance"
        )
        if (
            values.get("stance") is not None
            or masks.get("stance") is not False
            or stance_provenance.get("kind") != "MISSING"
        ):
            return record

        enriched = dict(record)
        enriched_values = dict(values)
        enriched_masks = dict(masks)
        enriched_provenance = dict(provenance)
        enriched_values["stance"] = state.stance
        enriched_masks["stance"] = True
        enriched_provenance["stance"] = dict(state.stance_provenance)
        enriched["state_before"] = enriched_values
        enriched["state_mask"] = enriched_masks
        enriched["state_provenance"] = enriched_provenance
        return enriched

    def _queue_stance_transition(
        self,
        record: Mapping[str, Any],
        state: _PlayerEncounterState,
        start_event_index: int,
    ) -> None:
        action = _mapping(record.get("action"), "action")
        spell_id = action.get("spell_id")
        if not isinstance(spell_id, int) or isinstance(spell_id, bool):
            return
        stance = STANCE_BY_SPELL_ID.get(spell_id)
        if stance is None:
            return

        result = _mapping(record.get("result"), "result")
        if result.get("status") != "succeeded":
            return
        association = result.get("association")
        if association not in ("unique", "ambiguous"):
            return
        anchor = _mapping(result.get("anchor"), "result.anchor")
        result_event_index = _event_index(
            anchor.get("event_index"), "result.anchor.event_index"
        )
        if result_event_index <= start_event_index:
            raise StateReconstructionError(
                "successful stance result must occur after its START"
            )

        self._transition_sequence += 1
        if association == "unique":
            transition_provenance = {
                "kind": "RECONSTRUCTED",
                "event_index": result_event_index,
                "csv_line": anchor.get("csv_line"),
                "offset_ms": anchor.get("offset_ms"),
                "note": (
                    "carried forward from prior uniquely associated successful "
                    "stance GO"
                ),
                "source_decision_id": action.get("decision_id"),
                "source_spell_id": spell_id,
            }
            value: str | None = stance
        else:
            # A stance GO occurred, but V1 was explicitly restricted to unique
            # associations.  Invalidate any older carried stance after this GO.
            transition_provenance = None
            value = None
        heapq.heappush(
            state.pending,
            _PendingStance(
                event_index=result_event_index,
                sequence=self._transition_sequence,
                value=value,
                provenance=transition_provenance,
            ),
        )

    def reconstruct_observation(
        self, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return one in-memory O2OObservationV1 without mutating ``record``."""

        record = _mapping(record, "record")
        start_event_index = _start_event_index(record)
        state = self._states.setdefault(
            self._stream_key(record), _PlayerEncounterState()
        )
        if (
            state.last_start_event_index is not None
            and start_event_index < state.last_start_event_index
        ):
            raise StateReconstructionError(
                "compact decisions must be ordered by START within each "
                "player/encounter"
            )

        self._apply_prior_transitions(state, start_event_index)
        enriched = self._with_reconstructed_stance(record, state)
        validate_state_evidence_cutoff(enriched)
        observation = project_decision_record(enriched)

        state.last_start_event_index = start_event_index
        self._queue_stance_transition(record, state, start_event_index)
        return observation


def iter_reconstructed_observations(
    records: Iterable[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Consume ``records`` once and yield in-memory reconstructed observations."""

    reconstructor = StateReconstructorV1()
    for record in records:
        yield reconstructor.reconstruct_observation(record)


__all__ = [
    "FutureStateEvidenceError",
    "STANCE_BY_SPELL_ID",
    "StateReconstructionError",
    "StateReconstructorV1",
    "iter_reconstructed_observations",
    "validate_state_evidence_cutoff",
]
