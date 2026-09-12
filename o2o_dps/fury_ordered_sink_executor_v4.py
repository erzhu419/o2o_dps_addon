"""Ordered executor for the runtime-bound deployed-Contra Raid-A identity.

The operation vocabulary is exactly the already audited v2 Raid-A vocabulary.
V4 projects only the new expert identity to the frozen v2 semantic identity
while v3 executes, then restores the runtime-bound identity in its receipt.
No operation, helper output, or Raid-B coverage is added by this projection.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Mapping, Sequence

from .expert_policy import ExpertDecision
from .fury_contra_adapter_v2 import SEMANTIC_ID as CONTRA_V2_EXPERT_ID
from .fury_ordered_sink_executor_v3 import (
    EXECUTION_SCHEMA_V3,
    audit_ordered_execution_v3,
    execute_ordered_sinks_v3,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RUNTIME_BOUND_EXPERT_ID_V7,
)


EXECUTION_SCHEMA_V4 = "fury_ordered_sink_execution/v4"


class FuryOrderedSinkExecutorV4Error(RuntimeError):
    """The v7 expert identity cannot use the bounded Raid-A projection."""


def _v2_identity(decision: ExpertDecision) -> ExpertDecision:
    if decision.expert_id != RUNTIME_BOUND_EXPERT_ID_V7:
        raise FuryOrderedSinkExecutorV4Error(
            "executor v4 accepts only the runtime-bound deployed-Contra Raid-A identity"
        )
    provenance = replace(decision.provenance, expert_id=CONTRA_V2_EXPERT_ID)
    return replace(decision, provenance=provenance)


def execute_ordered_sinks_v4(
    bridge: Any,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> dict[str, Any]:
    projected = _v2_identity(decision)
    result = execute_ordered_sinks_v3(
        bridge,
        projected,
        state,
        attempt_id_prefix=attempt_id_prefix,
        result_bearing_action_keys=result_bearing_action_keys,
    )
    result["schema"] = EXECUTION_SCHEMA_V4
    result["base_executor_schema"] = EXECUTION_SCHEMA_V3
    result["expert_id"] = decision.expert_id
    result["v4_semantics"] = {
        "identity_projection_only": True,
        "projected_from_expert_id": decision.expert_id,
        "projected_to_audited_expert_id": CONTRA_V2_EXPERT_ID,
        "operation_coverage_extended": False,
        "raid_b_covered": False,
        "runtime_binding_sha256": decision.metadata.get(
            "runtime_binding_sha256"
        ),
    }
    return result


def audit_ordered_execution_v4(
    execution: Mapping[str, Any],
    proposal: ExpertDecision,
    *,
    decision_index: int,
) -> list[dict[str, Any]]:
    projected_execution = deepcopy(dict(execution))
    projected_execution["schema"] = EXECUTION_SCHEMA_V3
    projected_execution["base_executor_schema"] = "fury_ordered_sink_execution/v2"
    projected_execution["expert_id"] = CONTRA_V2_EXPERT_ID
    projected_execution.pop("v4_semantics", None)
    return audit_ordered_execution_v3(
        projected_execution,
        _v2_identity(proposal),
        decision_index=decision_index,
    )


__all__ = (
    "EXECUTION_SCHEMA_V4",
    "FuryOrderedSinkExecutorV4Error",
    "audit_ordered_execution_v4",
    "execute_ordered_sinks_v4",
)
