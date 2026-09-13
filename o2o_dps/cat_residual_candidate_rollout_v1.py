"""Executable Cat-relative candidate on the native dynamic-v3 simulator.

The Cat v6 producer remains the frozen baseline.  This candidate uses its
ordered-sink execution substrate, but is never returned as a Cat v6 artifact.
The inner Cat-compatible decision identity is an executor protocol constraint;
the outer identity and intervention ledger identify the modified policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import types
from typing import Any, Mapping

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import cat_fury_full_policy_rollout_v6 as _cat_v6
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .expert_policy import ExpertDecision, RawSink, SwingQueueOp
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3, validate_dynamic_load_request_v3
from .fury_expert_adapters import WeaponMode
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3


POLICY_ID = "cat_residual_candidate/v1"
SCHEMA = "cat_residual_candidate_simulator_rollout/v1"


@dataclass(frozen=True)
class CatQueueResidualV1:
    """Relax Cat's two-hand HS reserve by a bounded rage amount."""

    reserve_discount_rage: float = 0.0
    intervention_at_decision: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.reserve_discount_rage <= 25.0:
            raise ValueError("reserve_discount_rage must be in [0, 25]")
        if self.intervention_at_decision is not None and (
            type(self.intervention_at_decision) is not int
            or self.intervention_at_decision < 0
        ):
            raise ValueError("intervention_at_decision must be a nonnegative integer")


class CatResidualCandidateV1:
    expert_id = POLICY_ID

    def __init__(self, residual: CatQueueResidualV1) -> None:
        self.residual = residual
        self.cat = CatFuryFullPolicyAdapterV4()
        self.interventions: list[dict[str, Any]] = []
        self.decision_count = 0

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        decision_index = self.decision_count
        self.decision_count += 1
        base = validate_source_decision_v4(self.cat.propose(state))
        combat = state.combat
        if (
            self.residual.reserve_discount_rage == 0.0
            or (
                self.residual.intervention_at_decision is not None
                and decision_index != self.residual.intervention_at_decision
            )
            or not base.valid
            or base.swing_queue is not SwingQueueOp.KEEP
            or combat.weapon_mode is not WeaponMode.TWO_HAND
            or combat.nearby_enemies != 1
            or combat.target_health_pct < 20.0
            or not combat.target_exists
            or not combat.in_melee_range
            or not combat.nampower
        ):
            return base
        reserve = 15.0 + combat.heroic_strike_cost
        if combat.bloodthirst_known and combat.cooldown_within(combat.bloodthirst_ready_in_s, 1.5):
            reserve += 30.0
        if combat.cooldown_within(combat.whirlwind_ready_in_s, 1.5):
            reserve += combat.whirlwind_cost
        if not reserve - self.residual.reserve_discount_rage < combat.rage <= reserve:
            return base
        sink = RawSink(
            "swing_queue",
            "QueueSpellByName",
            "英勇打击",
            "cat_residual_candidate/v1:two_hand_hs_reserve_discount",
        )
        metadata = dict(base.metadata)
        metadata["residual_candidate_id"] = POLICY_ID
        metadata["residual_rule"] = "two_hand_hs_reserve_discount"
        ordered = list(base.raw_sink_order)
        # Cat's next-swing branch precedes its direct GCD branches.  A queue
        # appended after an accepted GCD cannot be submitted in one epoch.
        insert_at = next(
            (index for index, row in enumerate(ordered) if row.channel in {"gcd", "cast_control"}),
            len(ordered),
        )
        ordered.insert(insert_at, sink)
        decision = validate_source_decision_v4(
            replace(
                base,
                swing_queue=SwingQueueOp.HEROIC_STRIKE,
                raw_sink_order=tuple(ordered),
                reason=f"{base.reason}; candidate two-hand HS reserve discount",
                metadata=metadata,
            )
        )
        self.interventions.append(
            {
                "decision_index": decision_index,
                "combat_elapsed_s": state.combat_elapsed_s,
                "rage": combat.rage,
                "cat_reserve_rage": reserve,
                "candidate_reserve_rage": reserve - self.residual.reserve_discount_rage,
                "policy_observation": asdict(state),
                "cat_proposal": base.to_dict(),
                "candidate_proposal": decision.to_dict(),
            }
        )
        return decision


def _candidate_core(
    controls: CatSimulatorControlFacadeV5,
    inputs: _cat_v5.CatFurySimulatorInputsV5,
) -> Any:
    cat_core = _cat_v6._clone_core_v6(controls, inputs)
    namespace = dict(cat_core.__globals__)
    namespace["_supported_adapter"] = lambda adapter: type(adapter) is CatResidualCandidateV1

    def proposal(adapter: CatResidualCandidateV1, state: CatFuryFullPolicyStateV4, target: Mapping[str, Any]) -> ExpertDecision:
        del target
        return adapter.propose(state)

    namespace["_proposal"] = proposal
    core = types.FunctionType(
        cat_core.__code__, namespace, name="_run_cat_residual_candidate_core",
        argdefs=cat_core.__defaults__, closure=cat_core.__closure__,
    )
    core.__kwdefaults__ = dict(cat_core.__kwdefaults__ or {})
    return core


def run_cat_residual_candidate_v1(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    candidate: CatResidualCandidateV1,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: _cat_v5.CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> dict[str, Any]:
    """Run a candidate, retaining Cat-v6-compatible low-level sink receipts."""

    if type(candidate) is not CatResidualCandidateV1:
        raise TypeError("candidate must be exact CatResidualCandidateV1")
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or _cat_v5.CatFurySimulatorInputsV5()
    if not isinstance(inputs, _cat_v5.CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5 or None")
    candidate.interventions.clear()
    candidate.decision_count = 0
    controls = CatSimulatorControlFacadeV5(
        bridge,
        item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_cvars={
            "NP_QueueCastTimeSpells": inputs.initial_np_queue_cast_time_spells,
            "NP_QueueInstantSpells": inputs.initial_np_queue_instant_spells,
        },
    )
    controls.reset_sidecar()
    facade = _dynamic_v5._DynamicV3ExecutorBridgeFacade(controls)
    result = _candidate_core(controls, inputs)(
        facade, raid_sim_request, candidate, seed=seed,
        target_contexts=target_contexts, dynamic_load=dynamic_load,
        max_decisions=max_decisions, max_advances=max_advances, retain_steps=True,
    )
    _cat_v6._bind_source_reentry_next_epochs_v6(result)
    _dynamic_v5._rewrite_v5_identity(result, dynamic_load, facade.last_load_result)
    for step in result.get("steps") or []:
        state = step.get("simulator_state_before")
        if isinstance(state, Mapping):
            step["dynamic_v3_live_target_state_before"] = state["dynamic_target_semantics"]
            step["dynamic_v3_idle_state_before"] = state["dynamic_idle_advance"]
    changed = {row["decision_index"]: row for row in candidate.interventions}
    for step in result.get("steps") or []:
        intervention = changed.get(step.get("decision_index"))
        step["policy_proposal_origin"] = (
            "CAT_RELATIVE_RESIDUAL" if intervention else "CAT_UNCHANGED"
        )
        if intervention:
            step["cat_baseline_proposal"] = intervention["cat_proposal"]
    result["schema"] = SCHEMA
    result["expert_id"] = POLICY_ID
    result["policy_family"] = "CAT_RELATIVE_CANDIDATE"
    result["residual"] = {
        "rule": "two_hand_hs_reserve_discount",
        "reserve_discount_rage": candidate.residual.reserve_discount_rage,
        "intervention_at_decision": candidate.residual.intervention_at_decision,
    }
    result["interventions"] = list(candidate.interventions)
    result["intervention_count"] = len(candidate.interventions)
    result["cat_baseline_artifact"] = False
    result["cat_source_order_claim"] = len(candidate.interventions) == 0
    result["low_level_executor_protocol"] = "CAT_V6_COMPATIBLE; MODIFIED_PROPOSALS_ARE_CANDIDATE_NOT_CAT_SOURCE"
    result["dynamic_v3_runtime_receipt_closure"] = _dynamic_v5._collect_runtime_receipts_v5(
        bridge, dynamic_load, facade.last_load_result, result.get("final_state")
    )
    result["simulator_dps_comparison_eligible"] = False
    result["historical_truth"] = False
    result["live_fidelity"] = False
    result["voting_eligible"] = False
    result["comparison_ready"] = False
    result["scientific_run_launched"] = False
    result["candidate_simulator_status"] = result.get("status")
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_CANDIDATE_SIMULATOR_ONLY"
    return result


__all__ = ("CatQueueResidualV1", "CatResidualCandidateV1", "run_cat_residual_candidate_v1")
