"""Project imported full-wave replays into accepted execution traces.

The imported selector receipt is proposal evidence, not execution evidence.
This projector joins it to bridge receipts by ``decision_index`` and exposes
only target changes, actions, and waits which the responsive replay actually
accepted.  The result can therefore seed D3 guides without relabeling an
unexecuted Cat, Contra, Contra_new, or offline-policy proposal as behavior.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


JSONMap = dict[str, Any]
SCHEMA = "offline_wave_accepted_execution_trace/v1"

_PROPOSAL_KIND = "IMPORTED_REACTIVE_INCUMBENT_SELECTED"
_ACTION_KINDS = frozenset(
    {
        "OPTIONAL_OFF_GCD_EXECUTED",
        "QUEUE_SET",
        "TERMINAL_GCD",
    }
)
_ACCEPTED_KINDS = _ACTION_KINDS | {"SET_TARGET", "TERMINAL_WAIT"}
_LANE_BY_KIND = {
    "OPTIONAL_OFF_GCD_EXECUTED": "off_gcd",
    "QUEUE_SET": "queue",
    "TERMINAL_GCD": "gcd",
}


class OfflineWaveExecutionTraceV1Error(ValueError):
    """Responsive replay receipts cannot form an attributable trace."""


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveExecutionTraceV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWaveExecutionTraceV1Error(f"{label} must be nonempty text")
    return value.strip()


def _proposal_metadata(
    receipt: Mapping[str, Any], *, receipt_index: int
) -> JSONMap:
    decision_index = _nonnegative_int(
        receipt.get("decision_index"),
        f"receipts[{receipt_index}].decision_index",
    )
    return {
        "decision_index": decision_index,
        "binding_id": _nonempty_text(
            receipt.get("binding_id"),
            f"receipts[{receipt_index}].binding_id",
        ),
        "source_policy_id": _nonempty_text(
            receipt.get("source_policy_id"),
            f"receipts[{receipt_index}].source_policy_id",
        ),
        "proposal_state_time_ms": _nonnegative_int(
            receipt.get("state_time_ms"),
            f"receipts[{receipt_index}].state_time_ms",
        ),
    }


def _accepted_target(
    receipt: Mapping[str, Any], *, receipt_index: int
) -> JSONMap:
    target = {
        "policy_target_index": _nonnegative_int(
            receipt.get("policy_target_index"),
            f"receipts[{receipt_index}].policy_target_index",
        ),
        "simulator_target_index": _nonnegative_int(
            receipt.get("simulator_target_index"),
            f"receipts[{receipt_index}].simulator_target_index",
        ),
        "changed": receipt.get("changed"),
        "state_time_ms": _nonnegative_int(
            receipt.get("state_time_ms"),
            f"receipts[{receipt_index}].state_time_ms",
        ),
        "receipt_index": receipt_index,
    }
    if type(target["changed"]) is not bool:
        raise OfflineWaveExecutionTraceV1Error(
            f"receipts[{receipt_index}].changed must be boolean"
        )
    if "operation_index" in receipt:
        target["operation_index"] = _nonnegative_int(
            receipt["operation_index"],
            f"receipts[{receipt_index}].operation_index",
        )
    if isinstance(receipt.get("target_gate"), Mapping):
        target["target_gate"] = deepcopy(dict(receipt["target_gate"]))
    return target


def _accepted_action(
    receipt: Mapping[str, Any], *, receipt_index: int
) -> JSONMap:
    kind = str(receipt["kind"])
    action = receipt.get("action")
    if not isinstance(action, Mapping) or not action:
        raise OfflineWaveExecutionTraceV1Error(
            f"receipts[{receipt_index}].action must be a nonempty action object"
        )
    projected: JSONMap = {
        "kind": kind,
        "lane": _LANE_BY_KIND[kind],
        "action": deepcopy(dict(action)),
        "state_time_ms": _nonnegative_int(
            receipt.get("state_time_ms"),
            f"receipts[{receipt_index}].state_time_ms",
        ),
        "receipt_index": receipt_index,
    }
    for field in ("operation_index", "prefix_index"):
        if field in receipt:
            projected[field] = _nonnegative_int(
                receipt[field], f"receipts[{receipt_index}].{field}"
            )
    if "attempt_id" in receipt:
        attempt_id = receipt["attempt_id"]
        if attempt_id is not None and not isinstance(attempt_id, str):
            raise OfflineWaveExecutionTraceV1Error(
                f"receipts[{receipt_index}].attempt_id must be text or None"
            )
        projected["attempt_id"] = attempt_id
    for field in ("action_gate", "queue_state_after_acceptance"):
        if isinstance(receipt.get(field), Mapping):
            projected[field] = deepcopy(dict(receipt[field]))
    return projected


def _accepted_wait(
    receipt: Mapping[str, Any], *, receipt_index: int
) -> JSONMap:
    return {
        "kind": "TERMINAL_WAIT",
        "wait_ms": _nonnegative_int(
            receipt.get("wait_ms"),
            f"receipts[{receipt_index}].wait_ms",
        ),
        "state_time_ms": _nonnegative_int(
            receipt.get("state_time_ms"),
            f"receipts[{receipt_index}].state_time_ms",
        ),
        "receipt_index": receipt_index,
    }


def project_offline_wave_execution_trace_v1(
    outcome: ScheduleReplayOutcomeV1,
) -> JSONMap:
    """Return source-attributed blocks backed only by accepted receipts.

    Proposal-only decisions are omitted.  Conversely, an accepted operation
    without its imported proposal identity is rejected because it cannot be
    attributed to the controller which produced it.
    """

    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("outcome must be ScheduleReplayOutcomeV1")

    proposals: dict[int, JSONMap] = {}
    accepted: dict[int, list[tuple[int, Mapping[str, Any]]]] = {}
    for receipt_index, receipt in enumerate(outcome.receipts):
        if not isinstance(receipt, Mapping):
            raise OfflineWaveExecutionTraceV1Error(
                f"receipts[{receipt_index}] must be a mapping"
            )
        kind = receipt.get("kind")
        if kind == _PROPOSAL_KIND:
            metadata = _proposal_metadata(receipt, receipt_index=receipt_index)
            decision_index = metadata["decision_index"]
            if decision_index in proposals:
                raise OfflineWaveExecutionTraceV1Error(
                    f"decision {decision_index} has multiple imported proposals"
                )
            proposals[decision_index] = metadata
        elif kind in _ACCEPTED_KINDS:
            decision_index = _nonnegative_int(
                receipt.get("decision_index"),
                f"receipts[{receipt_index}].decision_index",
            )
            accepted.setdefault(decision_index, []).append(
                (receipt_index, receipt)
            )

    unattributed = sorted(set(accepted) - set(proposals))
    if unattributed:
        raise OfflineWaveExecutionTraceV1Error(
            "accepted operations lack imported proposal metadata for decisions "
            f"{unattributed}"
        )

    blocks: list[JSONMap] = []
    for decision_index in sorted(accepted):
        proposal = proposals[decision_index]
        target: JSONMap | None = None
        actions: list[JSONMap] = []
        timing: JSONMap | None = None
        for receipt_index, receipt in accepted[decision_index]:
            kind = receipt["kind"]
            if kind == "SET_TARGET":
                if target is not None:
                    raise OfflineWaveExecutionTraceV1Error(
                        f"decision {decision_index} has multiple accepted targets"
                    )
                target = _accepted_target(receipt, receipt_index=receipt_index)
            elif kind in _ACTION_KINDS:
                actions.append(
                    _accepted_action(receipt, receipt_index=receipt_index)
                )
            else:
                if timing is not None:
                    raise OfflineWaveExecutionTraceV1Error(
                        f"decision {decision_index} has multiple accepted waits"
                    )
                timing = _accepted_wait(receipt, receipt_index=receipt_index)

        blocks.append(
            {
                **proposal,
                "accepted_target": target,
                "guide_actions": actions,
                "accepted_timing": timing,
            }
        )

    status = (
        outcome.status.value
        if isinstance(outcome.status, ReplayStatusV1)
        else str(outcome.status)
    )
    source_policy_ids = tuple(
        dict.fromkeys(block["source_policy_id"] for block in blocks)
    )
    return {
        "schema": SCHEMA,
        "seed": outcome.seed,
        "replay_status": status,
        "invalid_reason": outcome.invalid_reason,
        "source_policy_ids": list(source_policy_ids),
        "blocks": blocks,
        "accepted_action_count": sum(
            len(block["guide_actions"]) for block in blocks
        ),
        "contract": {
            "join_key": "decision_index",
            "proposal_receipts_are_execution_evidence": False,
            "guide_actions_come_only_from_accepted_bridge_receipts": True,
            "proposal_only_decisions_are_omitted": True,
            "target_indexes_come_only_from_accepted_set_target_receipts": True,
        },
    }


__all__ = (
    "OfflineWaveExecutionTraceV1Error",
    "SCHEMA",
    "project_offline_wave_execution_trace_v1",
)
