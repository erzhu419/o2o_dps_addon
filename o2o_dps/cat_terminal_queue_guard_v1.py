"""Cat-relative HS reserve relaxation with an observable terminal guard.

This remains a development candidate.  The guard sees only Cat's current
proposal and its current combat observation, never simulated future damage,
team kill-clock truth, or future random outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import types
from typing import Any, Mapping

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import cat_fury_full_policy_rollout_v6 as _cat_v6
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4, validate_source_decision_v4
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .cat_residual_candidate_rollout_v1 import CatQueueResidualV1, CatResidualCandidateV1
from .expert_policy import ExpertDecision, RawSink, SwingQueueOp
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3, validate_dynamic_load_request_v3
from .fury_expert_adapters import WeaponMode
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3


POLICY_ID = "cat_terminal_queue_guard/v1"
SCHEMA = "cat_terminal_queue_guard_simulator_rollout/v1"


@dataclass(frozen=True)
class TerminalQueueGuardV1:
    reserve_discount_rage: float = 15.0
    execute_approach_health_pct: float = 35.0

    def __post_init__(self) -> None:
        CatQueueResidualV1(self.reserve_discount_rage)
        if not 20.0 <= self.execute_approach_health_pct <= 50.0:
            raise ValueError("execute_approach_health_pct must be in [20, 50]")


class CatTerminalQueueGuardV1(CatResidualCandidateV1):
    expert_id = POLICY_ID

    def __init__(self, guard: TerminalQueueGuardV1) -> None:
        super().__init__(CatQueueResidualV1(guard.reserve_discount_rage))
        self.guard = guard
        self.guarded_opportunities: list[dict[str, Any]] = []

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        decision_index = self.decision_count
        self.decision_count += 1
        base = validate_source_decision_v4(self.cat.propose(state))
        combat = state.combat
        discount = self.guard.reserve_discount_rage
        if (
            discount == 0.0
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
        if not reserve - discount < combat.rage <= reserve:
            return base

        reason = None
        if combat.target_health_pct <= self.guard.execute_approach_health_pct:
            reason = "OBSERVED_EXECUTE_APPROACH"
        elif combat.casting_slam or combat.slam_remaining_s > 0.0 or any(
            sink.channel == "gcd" and sink.value == "猛击" for sink in base.raw_sink_order
        ):
            reason = "CURRENT_SLAM_CONFLICT"
        if reason:
            self.guarded_opportunities.append({
                "decision_index": decision_index,
                "combat_elapsed_s": state.combat_elapsed_s,
                "rage": combat.rage,
                "target_health_pct": combat.target_health_pct,
                "reason": reason,
            })
            return base

        sink = RawSink(
            "swing_queue", "QueueSpellByName", "英勇打击",
            "cat_terminal_queue_guard/v1:two_hand_hs_reserve_discount",
        )
        metadata = dict(base.metadata)
        metadata["residual_candidate_id"] = POLICY_ID
        metadata["residual_rule"] = "guarded_two_hand_hs_reserve_discount"
        ordered = list(base.raw_sink_order)
        insert_at = next(
            (index for index, row in enumerate(ordered) if row.channel in {"gcd", "cast_control"}),
            len(ordered),
        )
        ordered.insert(insert_at, sink)
        decision = validate_source_decision_v4(replace(
            base,
            swing_queue=SwingQueueOp.HEROIC_STRIKE,
            raw_sink_order=tuple(ordered),
            reason=f"{base.reason}; candidate guarded two-hand HS reserve discount",
            metadata=metadata,
        ))
        self.interventions.append({
            "decision_index": decision_index,
            "combat_elapsed_s": state.combat_elapsed_s,
            "rage": combat.rage,
            "cat_reserve_rage": reserve,
            "candidate_reserve_rage": reserve - discount,
            "policy_observation": asdict(state),
            "cat_proposal": base.to_dict(),
            "candidate_proposal": decision.to_dict(),
        })
        return decision


def _guarded_core(controls: CatSimulatorControlFacadeV5, inputs: _cat_v5.CatFurySimulatorInputsV5) -> Any:
    cat_core = _cat_v6._clone_core_v6(controls, inputs)
    namespace = dict(cat_core.__globals__)
    namespace["_supported_adapter"] = lambda adapter: type(adapter) is CatTerminalQueueGuardV1

    def proposal(adapter: CatTerminalQueueGuardV1, state: CatFuryFullPolicyStateV4,
                 target: Mapping[str, Any]) -> ExpertDecision:
        del target
        return adapter.propose(state)

    namespace["_proposal"] = proposal
    core = types.FunctionType(
        cat_core.__code__, namespace, name="_run_cat_terminal_queue_guard_core",
        argdefs=cat_core.__defaults__, closure=cat_core.__closure__,
    )
    core.__kwdefaults__ = dict(cat_core.__kwdefaults__ or {})
    return core


def run_cat_terminal_queue_guard_v1(
    bridge: Any, raid_sim_request: Mapping[str, Any], candidate: CatTerminalQueueGuardV1,
    *, seed: int, target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: _cat_v5.CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000, max_advances: int = 100_000,
) -> dict[str, Any]:
    if type(candidate) is not CatTerminalQueueGuardV1:
        raise TypeError("candidate must be exact CatTerminalQueueGuardV1")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or _cat_v5.CatFurySimulatorInputsV5()
    if not isinstance(inputs, _cat_v5.CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5 or None")
    candidate.interventions.clear()
    candidate.guarded_opportunities.clear()
    candidate.decision_count = 0
    controls = CatSimulatorControlFacadeV5(
        bridge, item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_cvars={
            "NP_QueueCastTimeSpells": inputs.initial_np_queue_cast_time_spells,
            "NP_QueueInstantSpells": inputs.initial_np_queue_instant_spells,
        },
    )
    controls.reset_sidecar()
    facade = _dynamic_v5._DynamicV3ExecutorBridgeFacade(controls)
    result = _guarded_core(controls, inputs)(
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
        step["policy_proposal_origin"] = "CAT_RELATIVE_RESIDUAL" if intervention else "CAT_UNCHANGED"
        if intervention:
            step["cat_baseline_proposal"] = intervention["cat_proposal"]
    result.update({
        "schema": SCHEMA, "expert_id": POLICY_ID,
        "policy_family": "CAT_RELATIVE_GUARDED_CANDIDATE",
        "residual": {
            "rule": "guarded_two_hand_hs_reserve_discount",
            "reserve_discount_rage": candidate.guard.reserve_discount_rage,
            "execute_approach_health_pct": candidate.guard.execute_approach_health_pct,
        },
        "interventions": list(candidate.interventions),
        "intervention_count": len(candidate.interventions),
        "guarded_opportunities": list(candidate.guarded_opportunities),
        "guarded_opportunity_count": len(candidate.guarded_opportunities),
        "cat_baseline_artifact": False,
        "cat_source_order_claim": len(candidate.interventions) == 0,
        "low_level_executor_protocol": "CAT_V6_COMPATIBLE; MODIFIED_PROPOSALS_ARE_CANDIDATE_NOT_CAT_SOURCE",
        "dynamic_v3_runtime_receipt_closure": _dynamic_v5._collect_runtime_receipts_v5(
            bridge, dynamic_load, facade.last_load_result, result.get("final_state")
        ),
        "simulator_dps_comparison_eligible": False,
        "historical_truth": False, "live_fidelity": False,
        "voting_eligible": False, "comparison_ready": False,
        "scientific_run_launched": False,
        "candidate_simulator_status": result.get("status"),
    })
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_CANDIDATE_SIMULATOR_ONLY"
    return result


__all__ = (
    "POLICY_ID", "SCHEMA", "TerminalQueueGuardV1", "CatTerminalQueueGuardV1",
    "run_cat_terminal_queue_guard_v1",
)
