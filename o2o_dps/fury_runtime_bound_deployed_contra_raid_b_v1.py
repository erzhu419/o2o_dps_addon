"""Source-derived dual-wield Raid-B proposal for the installed Contra.

This is the distinct ``Contra_MacroHandler('b')`` controller.  It consumes the
same current-character Buttons/CVar binding as Raid-A, but it does not claim
that a historical player used this macro or that the client accepted a sink.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .deployed_contra_runtime_binding_v1 import (
    validate_deployed_contra_runtime_binding_v1,
)
from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    SwingQueueOp,
    invalid_decision,
)
from .fury_contra_adapter_v2 import (
    ContraDeployedFuryAdapterV2,
    ContraDeployedFuryStateV2,
)
from .fury_expert_adapters import (
    BLOODTHIRST,
    EXECUTE,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
    _DecisionBuilder,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    UNSUPPORTED_CVAR_BLOCKER,
    UNSUPPORTED_HELPER_BLOCKER,
)


SCHEMA = "fury_runtime_bound_deployed_contra_raid_b_adapter/v1"
POLICY_ID = "contra.deployed.fury.raid_b"
EXPERT_ID = "contra.deployed.fury.raid_b.v1.runtime_bound"


class RuntimeBoundContraRaidBError(RuntimeError):
    """Current runtime binding is not executable by this Raid-B adapter."""


class RuntimeBoundContraRaidBAdapterV1(ContraDeployedFuryAdapterV2):
    """Translate ``Contra_SCKBZ_B``; reject non-dual-wield states."""

    expert_id = EXPERT_ID

    def __init__(self, runtime_binding: Mapping[str, Any]) -> None:
        binding = validate_deployed_contra_runtime_binding_v1(runtime_binding)
        inputs = binding["adapter_inputs"]
        if (
            inputs["queue_on_swing"] is not True
            or inputs["queue_spells_on_cooldown"] is not False
            or inputs["retry_server_rejected_spells"] is not False
        ):
            raise RuntimeBoundContraRaidBError(
                f"{UNSUPPORTED_CVAR_BLOCKER}: Raid-B ordered execution requires "
                "the captured Warrior queue profile"
            )
        self.runtime_binding = binding
        self.xuanfeng = bool(inputs["saved_xuanfeng"])
        self.interrupt = bool(inputs["interrupt_runtime_gate"])
        self.nampower_queue_spells_on_cooldown = False

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.DEPLOYED,
            authority_files=(
                r"%WOW_ADDONS_ROOT%\Contra\Contra.toc",
                r"%WOW_ADDONS_ROOT%\Contra\Contra_ALL.lua",
                r"%WOW_CHARACTER_SAVEDVARIABLES%\Contra.lua",
                r"%WOW_ROOT%\WTF\Config.wtf",
            ),
            source_refs=(
                "Contra_ALL.lua:31277-31347",
                "Contra_ALL.lua:31355-31387",
                "Contra_ALL.lua:32075-32089",
                "Contra_ALL.lua:36435-36444",
            ),
        )

    def _ref(self, name: str) -> str:
        return {
            "prelude": "Contra_ALL.lua:32077",
            "interrupt": "Contra_ALL.lua:32077",
            "stance": "Contra_ALL.lua:32078",
        }.get(name, "Contra_ALL.lua")

    def propose(self, state: ContraDeployedFuryStateV2) -> ExpertDecision:
        if isinstance(state, ContraDeployedFuryStateV2) and isinstance(
            state.combat, FuryExpertState
        ) and state.combat.weapon_mode is not WeaponMode.DUAL_WIELD:
            return self._attach_runtime(invalid_decision(
                self._provenance(),
                "Raid-B v1 translates only Contra_SCKBZ_B dual-wield source",
                metadata={"adapter_contract": SCHEMA, "fail_closed": True},
            ))
        inputs = self.runtime_binding["adapter_inputs"]
        enabled = [name for name, key in (
            ("burst", "burst_runtime_gate"),
            ("survival", "survival_runtime_gate"),
        ) if inputs[key]]
        if enabled:
            return self._attach_runtime(invalid_decision(
                self._provenance(),
                f"{UNSUPPORTED_HELPER_BLOCKER}: {','.join(enabled)}",
                metadata={"adapter_contract": SCHEMA, "fail_closed": True},
            ))
        effective = state
        if isinstance(state, ContraDeployedFuryStateV2) and isinstance(
            state.combat, FuryExpertState
        ) and not inputs["interrupt_runtime_gate"]:
            effective = replace(state, combat=replace(
                state.combat, contra_interrupt_action=None,
            ))
        return self._attach_runtime(super().propose(effective))

    def _attach_runtime(self, decision: ExpertDecision) -> ExpertDecision:
        inputs = self.runtime_binding["adapter_inputs"]
        metadata = dict(decision.metadata)
        metadata.update({
            "adapter_contract": SCHEMA,
            "semantic_parent_id": "contra.deployed.fury.raid_b",
            "runtime_binding_sha256": self.runtime_binding["binding_sha256"],
            "runtime_binding_consumed": True,
            "controller": "raid_b",
            "runtime_helper_call_inputs": {
                "controller": "raid_b",
                "source_order": ["interrupt", "burst", "support", "survival"],
                "interrupt": bool(inputs["interrupt_runtime_gate"]),
                "burst": bool(inputs["burst_runtime_gate"]),
                "support": True,
                "survival": bool(inputs["survival_runtime_gate"]),
            },
            "source_execution": False,
            "client_execution_observed": False,
        })
        return replace(
            decision,
            metadata=metadata,
            reason=(
                "source-derived installed Contra_ALL.lua dual-wield Raid-B proposal"
                if decision.valid else decision.reason
            ),
        )

    def _metadata_v2(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        metadata = super()._metadata_v2(*args, **kwargs)
        metadata.pop("complete_two_hand_raid_a_body", None)
        metadata.pop("max_health_branches_are_independent_source_ifs", None)
        metadata["complete_dual_wield_raid_b_body"] = True
        return metadata

    def _dual_wield(self, builder: _DecisionBuilder, state: FuryExpertState) -> None:
        hp, rage = state.target_health_pct, state.rage
        ww_cd, bt_cd = state.whirlwind_ready_in_s, state.bloodthirst_ready_in_s
        st = state.contra_st_s
        if hp < 20 and ww_cd > st and st > 0.5 and (
            rage > 44 or (bt_cd != 0 and rage > 15)
        ):
            self._emit_contra_gcd(builder, state, EXECUTE, self._dual_ref(32080))
        if rage > 49 or (ww_cd > st and rage > 29):
            if state.queued_swing is not SwingQueueOp.CLEAVE:
                builder.emit_queue(
                    SwingQueueOp.CLEAVE,
                    operation="QueueSpellByName" if state.nampower else "CastSpellByName",
                    value="顺劈斩",
                    source_ref=self._dual_ref(32083),
                )
        # Preserve Lua precedence: Tauren in range casts even when xuanfeng=false.
        if (self.xuanfeng and not state.race_is_tauren and
                state.target_distance_yards < 8) or (
                state.race_is_tauren and state.target_distance_yards < 5.5):
            self._emit_contra_gcd(builder, state, WHIRLWIND, self._dual_ref(32086))
        if (
            (hp > 20 and rage > 59 and ww_cd > st)
            or (hp > 20 and rage > 84 and ww_cd < st)
            or (hp < 20 and rage < 44 and ww_cd > 1.4)
        ):
            self._emit_contra_gcd(builder, state, BLOODTHIRST, self._dual_ref(32089))


__all__ = (
    "EXPERT_ID", "POLICY_ID", "RuntimeBoundContraRaidBAdapterV1",
    "RuntimeBoundContraRaidBError", "SCHEMA",
)
