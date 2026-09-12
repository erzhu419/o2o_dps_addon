"""Runtime-bound Raid-A adapter for the installed deployed Contra policy.

V2 deliberately froze source-derived class constants.  This additive adapter
instead consumes ``deployed_contra_runtime_binding/v1`` and binds the exact
current Buttons table and captured Nampower CVar values.  It covers only the
already translated Raid-A single-target controller.  Raid-B is a distinct
multi-target source body and remains an explicit implementation blocker.

The installed f894 profile has the case-sensitive ``Burst`` and ``Survive``
keys absent, so both helpers receive ``enabled=False``.  The lower-case legacy
``baofa``/``shengcun`` values are diagnostic only and never re-enable them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    validate_deployed_contra_runtime_binding_v1,
)
from .expert_policy import ExpertDecision, invalid_decision
from .fury_contra_adapter_v2 import (
    ContraDeployedFuryAdapterV2,
    ContraDeployedFuryStateV2,
)
from .fury_expert_adapters import FuryExpertState


JSONMap = dict[str, Any]
ADAPTER_SCHEMA_V7 = "fury_runtime_bound_deployed_contra_adapter/v7"
RUNTIME_BOUND_EXPERT_ID_V7 = "contra.deployed.fury.raid_a.v7.runtime_bound"
RAID_A_CONTROLLER = "raid_a"
RAID_B_CONTROLLER = "raid_b"
RAID_B_BLOCKER = "RAID_B_CONTROLLER_NOT_IMPLEMENTED_V7"
UNSUPPORTED_CVAR_BLOCKER = "NAMPOWER_CVAR_PROFILE_NOT_EXECUTABLE_V7"
UNSUPPORTED_HELPER_BLOCKER = "ENABLED_CONTRA_HELPER_BODY_NOT_IMPLEMENTED_V7"


class FuryRuntimeBoundDeployedContraAdapterV7Error(RuntimeError):
    """The v7 runtime binding cannot drive the bounded Raid-A adapter."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryRuntimeBoundDeployedContraAdapterV7Error(
            f"{label} must be an object"
        )
    return value


def raid_a_helper_call_inputs_v7(
    runtime_binding: Mapping[str, Any],
) -> JSONMap:
    """Return the exact outer-gate inputs sent to Raid-A helper traversal."""

    try:
        binding = validate_deployed_contra_runtime_binding_v1(runtime_binding)
    except DeployedContraRuntimeBindingError as error:
        raise FuryRuntimeBoundDeployedContraAdapterV7Error(str(error)) from error
    inputs = _mapping(binding.get("adapter_inputs"), "adapter_inputs")
    return {
        "schema": "deployed_contra_raid_a_helper_call_inputs/v7",
        "runtime_binding_sha256": binding["binding_sha256"],
        "controller": RAID_A_CONTROLLER,
        "interrupt": bool(inputs["interrupt_runtime_gate"]),
        "burst": bool(inputs["burst_runtime_gate"]),
        # ZS_FZ has no outer enable gate in the deployed source.
        "support": True,
        "survival": bool(inputs["survival_runtime_gate"]),
        "source_order": ["interrupt", "burst", "support", "survival"],
    }


class RuntimeBoundContraDeployedFuryAdapterV7(ContraDeployedFuryAdapterV2):
    """Use one validated runtime binding for the translated Raid-A body."""

    expert_id = RUNTIME_BOUND_EXPERT_ID_V7

    def __init__(
        self,
        runtime_binding: Mapping[str, Any],
        *,
        controller: str = RAID_A_CONTROLLER,
    ) -> None:
        if controller == RAID_B_CONTROLLER:
            raise FuryRuntimeBoundDeployedContraAdapterV7Error(
                f"{RAID_B_BLOCKER}: deployed Raid-B multi-target controller "
                "has not been translated into this adapter"
            )
        if controller != RAID_A_CONTROLLER:
            raise FuryRuntimeBoundDeployedContraAdapterV7Error(
                f"unsupported deployed Contra controller {controller!r}"
            )
        try:
            binding = validate_deployed_contra_runtime_binding_v1(
                runtime_binding
            )
        except DeployedContraRuntimeBindingError as error:
            raise FuryRuntimeBoundDeployedContraAdapterV7Error(str(error)) from error
        adapter_inputs = _mapping(binding["adapter_inputs"], "adapter_inputs")
        if (
            adapter_inputs["queue_on_swing"] is not True
            or adapter_inputs["queue_spells_on_cooldown"] is not False
            or adapter_inputs["retry_server_rejected_spells"] is not False
        ):
            raise FuryRuntimeBoundDeployedContraAdapterV7Error(
                f"{UNSUPPORTED_CVAR_BLOCKER}: ordered executor v3 only models "
                "the deployed Warrior source-initialization queue profile"
            )

        self.runtime_binding = binding
        self.controller = controller
        self.xuanfeng = bool(adapter_inputs["saved_xuanfeng"])
        self.interrupt = bool(adapter_inputs["interrupt_runtime_gate"])
        self.nampower_queue_spells_on_cooldown = bool(
            adapter_inputs["queue_spells_on_cooldown"]
        )
        self.helper_call_inputs = raid_a_helper_call_inputs_v7(binding)

    @property
    def runtime_binding_sha256(self) -> str:
        return str(self.runtime_binding["binding_sha256"])

    def propose(self, state: ContraDeployedFuryStateV2) -> ExpertDecision:
        helper_inputs = dict(self.helper_call_inputs)
        if not isinstance(state, ContraDeployedFuryStateV2) or not isinstance(
            state.combat, FuryExpertState
        ):
            decision = super().propose(state)
            return self._attach_runtime_metadata(decision, helper_inputs)

        enabled_untranslated = [
            name
            for name in ("burst", "survival")
            if helper_inputs[name] is True
        ]
        if enabled_untranslated:
            metadata = self._invalid_metadata(
                [f"{name}_helper_body_not_implemented" for name in enabled_untranslated]
            )
            metadata.update(self._runtime_metadata(helper_inputs))
            return invalid_decision(
                self._provenance(),
                f"{UNSUPPORTED_HELPER_BLOCKER}: "
                + ",".join(enabled_untranslated),
                metadata=metadata,
            )

        # The legacy state field is retained as the already reconstructed
        # always-called ZS_FZ support output.  Disabled Burst/Survive helpers
        # contribute no sinks, and a disabled interrupt cannot return early.
        effective = replace(
            state,
            combat=replace(
                state.combat,
                contra_interrupt_action=(
                    state.combat.contra_interrupt_action
                    if helper_inputs["interrupt"]
                    else None
                ),
            ),
        )
        decision = super().propose(effective)
        return self._attach_runtime_metadata(decision, helper_inputs)

    def _runtime_metadata(self, helper_inputs: Mapping[str, Any]) -> JSONMap:
        inputs = _mapping(self.runtime_binding["adapter_inputs"], "adapter_inputs")
        return {
            "adapter_contract": ADAPTER_SCHEMA_V7,
            "runtime_binding_sha256": self.runtime_binding_sha256,
            "runtime_binding_consumed": True,
            "controller": self.controller,
            "runtime_helper_call_inputs": dict(helper_inputs),
            "runtime_options": {
                "xuanfeng": inputs["saved_xuanfeng"],
                "interrupt": inputs["interrupt_runtime_gate"],
                "queue_on_swing": inputs["queue_on_swing"],
                "queue_spells_on_cooldown": inputs[
                    "queue_spells_on_cooldown"
                ],
                "retry_server_rejected_spells": inputs[
                    "retry_server_rejected_spells"
                ],
            },
            "raid_b_blocker": RAID_B_BLOCKER,
            "source_execution": False,
            "client_execution_observed": False,
        }

    def _attach_runtime_metadata(
        self,
        decision: ExpertDecision,
        helper_inputs: Mapping[str, Any],
    ) -> ExpertDecision:
        metadata = dict(decision.metadata)
        metadata.update(self._runtime_metadata(helper_inputs))
        return replace(decision, metadata=metadata)


__all__ = (
    "ADAPTER_SCHEMA_V7",
    "FuryRuntimeBoundDeployedContraAdapterV7Error",
    "RAID_A_CONTROLLER",
    "RAID_B_BLOCKER",
    "RAID_B_CONTROLLER",
    "RUNTIME_BOUND_EXPERT_ID_V7",
    "RuntimeBoundContraDeployedFuryAdapterV7",
    "UNSUPPORTED_CVAR_BLOCKER",
    "UNSUPPORTED_HELPER_BLOCKER",
    "raid_a_helper_call_inputs_v7",
)
