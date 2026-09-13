"""Zero residual around the current Cat v6 baseline controller.

The controller is the exact CatFuryFullPolicyAdapterV4 required by the Cat v6
rollout. Direct proposals delegate to that controller, and a caller can pass
``controller`` to the native Cat v6 rollout. No Cat source branch or ordered
sink is copied or changed here. Nonzero residual execution is not implemented.
"""

from __future__ import annotations

from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
)
from .expert_policy import ExpertDecision


class CatFullPolicyResidualZeroV1:
    """Expose Cat's current full-policy controller with zero modification."""

    residual_id = "cat_full_policy_residual_zero/v1"

    def __init__(self) -> None:
        self.controller = CatFuryFullPolicyAdapterV4()

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        return self.controller.propose(state)


__all__ = ("CatFullPolicyResidualZeroV1",)
