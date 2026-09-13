"""Multi-state, one-decision ActionPlan branches around Cat's visited states.

Each alternative uses a fresh native load and Cat continuation.  The teacher
may see completed outcomes; the candidate receives only its current Cat state.
These paired replays are development evidence, not a restored hidden RNG state.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import types
from typing import Any, Callable, Mapping

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import cat_fury_full_policy_rollout_v6 as _cat_v6
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .branch_teacher_v1 import (
    DEFAULT_BRIDGE, PROJECT_ROOT, BranchReplayMismatchV1,
    _observation_receipt, _run_fresh, _same_prefix, _seed_receipt,
)
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4, CatInventoryItemV4,
    validate_source_decision_v4,
)
from .cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1, adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
)
from .expert_policy import ExpertDecision, RawSink, StanceOp, SwingQueueOp, WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3, validate_dynamic_load_request_v3
from .fury_expert_adapters import BLOODTHIRST, WHIRLWIND, FuryExpertState, WeaponMode
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


SCHEMA = "cat_action_branch_search/v2"
POLICY_ID = "cat_action_branch_candidate/v1"
_GCD_NAMES = {BLOODTHIRST: "嗜血", WHIRLWIND: "旋风斩"}
_REPLACEMENT = {
    "BT_TO_WW": (BLOODTHIRST, WHIRLWIND),
    "WW_TO_BT": (WHIRLWIND, BLOODTHIRST),
}
BRANCH_KINDS = (
    "SUPPRESS_QUEUE", "ADD_HS_QUEUE", "BT_TO_WW", "WW_TO_BT", "DEFER_GCD",
)


@dataclass(frozen=True)
class ActionBranchV1:
    decision_index: int
    kind: str

    def __post_init__(self) -> None:
        if type(self.decision_index) is not int or self.decision_index < 0:
            raise ValueError("decision_index must be a nonnegative integer")
        if self.kind not in BRANCH_KINDS:
            raise ValueError(f"unknown action branch {self.kind}")


def _single_gcd(decision: ExpertDecision) -> tuple[int, RawSink] | None:
    matches = [(i, sink) for i, sink in enumerate(decision.raw_sink_order) if sink.channel == "gcd"]
    return matches[0] if len(matches) == 1 else None


def available_branches_v1(
    state: CatFuryFullPolicyStateV4, base: ExpertDecision,
) -> tuple[str, ...]:
    """Offer only current-state alternatives expressible by the sink executor."""

    if not base.valid or not state.combat.target_exists or not state.combat.in_melee_range:
        return ()
    combat = state.combat
    choices: list[str] = []
    queues = [sink for sink in base.raw_sink_order if sink.channel == "swing_queue"]
    if len(queues) == 1 and base.swing_queue in (SwingQueueOp.HEROIC_STRIKE, SwingQueueOp.CLEAVE):
        choices.append("SUPPRESS_QUEUE")
    elif (
        not queues and base.swing_queue is SwingQueueOp.KEEP
        and combat.queued_swing is SwingQueueOp.KEEP
        and combat.nearby_enemies == 1 and combat.target_health_pct >= 20.0
        and combat.rage >= combat.heroic_strike_cost
    ):
        choices.append("ADD_HS_QUEUE")
    gcd = _single_gcd(base)
    if gcd and base.gcd in _GCD_NAMES and gcd[1].value == _GCD_NAMES[base.gcd]:
        if (
            base.gcd == BLOODTHIRST and combat.current_stance is StanceOp.BERSERKER
            and combat.whirlwind_ready_in_s <= 1.5 and combat.rage >= combat.whirlwind_cost
        ):
            choices.append("BT_TO_WW")
        if (
            base.gcd == WHIRLWIND and combat.bloodthirst_known
            and combat.bloodthirst_ready_in_s <= 1.5 and combat.rage >= 30.0
        ):
            choices.append("WW_TO_BT")
        choices.append("DEFER_GCD")
    return tuple(choices)


def apply_action_branch_v1(
    state: CatFuryFullPolicyStateV4, base: ExpertDecision, kind: str,
) -> ExpertDecision:
    if kind not in available_branches_v1(state, base):
        raise ValueError(f"{kind} is not available in this observed state")
    ordered = list(base.raw_sink_order)
    metadata = dict(base.metadata)
    metadata["action_branch_kind"] = kind
    metadata["action_branch_policy"] = POLICY_ID
    changes: dict[str, Any] = {}
    if kind == "SUPPRESS_QUEUE":
        ordered = [sink for sink in ordered if sink.channel != "swing_queue"]
        changes["swing_queue"] = SwingQueueOp.KEEP
    elif kind == "ADD_HS_QUEUE":
        sink = RawSink(
            "swing_queue",
            "QueueSpellByName" if state.combat.nampower else "CastSpellByName",
            "英勇打击", f"{POLICY_ID}:ADD_HS_QUEUE",
        )
        position = next(
            (i for i, row in enumerate(ordered) if row.channel in {"gcd", "cast_control"}),
            len(ordered),
        )
        ordered.insert(position, sink)
        changes["swing_queue"] = SwingQueueOp.HEROIC_STRIKE
    else:
        index, old_sink = _single_gcd(base)  # availability guarantees exactly one
        if kind == "DEFER_GCD":
            ordered.pop(index)
            changes.update(gcd=WAIT_ACTION, wait_ms=100)
            metadata["raw_gcd_calls"] = []
        else:
            _, action = _REPLACEMENT[kind]
            ordered[index] = RawSink(
                "gcd", old_sink.operation, _GCD_NAMES[action], f"{POLICY_ID}:{kind}",
            )
            changes.update(gcd=action, wait_ms=None)
            metadata["raw_gcd_calls"] = [action]
    return validate_source_decision_v4(replace(
        base, raw_sink_order=tuple(ordered), metadata=metadata,
        reason=f"{base.reason}; candidate {kind}", **changes,
    ))


class CatActionBranchCandidateV1:
    expert_id = POLICY_ID

    def __init__(self, branch: ActionBranchV1) -> None:
        self.branch = branch
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.interventions: list[dict[str, Any]] = []

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        index = self.decision_count
        self.decision_count += 1
        base = validate_source_decision_v4(self.cat.propose(state))
        if index != self.branch.decision_index:
            return base
        changed = apply_action_branch_v1(state, base, self.branch.kind)
        self.interventions.append({
            "decision_index": index, "kind": self.branch.kind,
            "policy_observation": asdict(state),
            "cat_proposal": base.to_dict(), "candidate_proposal": changed.to_dict(),
        })
        return changed


def _candidate_core(
    controls: CatSimulatorControlFacadeV5, inputs: _cat_v5.CatFurySimulatorInputsV5,
) -> Any:
    cat_core = _cat_v6._clone_core_v6(controls, inputs)
    namespace = dict(cat_core.__globals__)
    namespace["_supported_adapter"] = lambda adapter: type(adapter) is CatActionBranchCandidateV1

    def proposal(adapter: CatActionBranchCandidateV1, state: CatFuryFullPolicyStateV4,
                 target: Mapping[str, Any]) -> ExpertDecision:
        del target
        return adapter.propose(state)

    namespace["_proposal"] = proposal
    core = types.FunctionType(
        cat_core.__code__, namespace, name="_run_cat_action_branch_core",
        argdefs=cat_core.__defaults__, closure=cat_core.__closure__,
    )
    core.__kwdefaults__ = dict(cat_core.__kwdefaults__ or {})
    return core


def run_cat_action_branch_candidate_v1(
    bridge: Any, raid_sim_request: Mapping[str, Any], candidate: CatActionBranchCandidateV1,
    *, seed: int, target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: _cat_v5.CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000, max_advances: int = 100_000,
) -> dict[str, Any]:
    if type(candidate) is not CatActionBranchCandidateV1:
        raise TypeError("candidate must be exact CatActionBranchCandidateV1")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or _cat_v5.CatFurySimulatorInputsV5()
    candidate.interventions.clear()
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
    result = _candidate_core(controls, inputs)(
        facade, raid_sim_request, candidate, seed=seed,
        target_contexts=target_contexts, dynamic_load=dynamic_load,
        max_decisions=max_decisions, max_advances=max_advances, retain_steps=True,
    )
    _cat_v6._bind_source_reentry_next_epochs_v6(result)
    _dynamic_v5._rewrite_v5_identity(result, dynamic_load, facade.last_load_result)
    changed = {row["decision_index"]: row for row in candidate.interventions}
    for step in result.get("steps") or []:
        intervention = changed.get(step.get("decision_index"))
        step["policy_proposal_origin"] = "ACTION_BRANCH" if intervention else "CAT_UNCHANGED"
        if intervention:
            step["cat_baseline_proposal"] = intervention["cat_proposal"]
    result.update({
        "schema": "cat_action_branch_simulator_rollout/v1", "expert_id": POLICY_ID,
        "policy_family": "CAT_RELATIVE_ACTION_BRANCH",
        "branch": asdict(candidate.branch), "interventions": list(candidate.interventions),
        "intervention_count": len(candidate.interventions),
        "cat_baseline_artifact": False, "cat_source_order_claim": not candidate.interventions,
        "low_level_executor_protocol": "CAT_V6_COMPATIBLE; MODIFIED_PROPOSAL_IS_CANDIDATE",
        "dynamic_v3_runtime_receipt_closure": _dynamic_v5._collect_runtime_receipts_v5(
            bridge, dynamic_load, facade.last_load_result, result.get("final_state")
        ),
        "simulator_dps_comparison_eligible": False, "historical_truth": False,
        "live_fidelity": False, "voting_eligible": False, "comparison_ready": False,
        "scientific_run_launched": False, "candidate_simulator_status": result.get("status"),
    })
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_CANDIDATE_SIMULATOR_ONLY"
    return result


def _state_from_step(step: Mapping[str, Any]) -> CatFuryFullPolicyStateV4:
    observation = step.get("expert_state")
    if not isinstance(observation, Mapping) or not isinstance(observation.get("combat"), Mapping):
        raise BranchReplayMismatchV1("Cat step lacks its complete policy observation")
    combat = dict(observation["combat"])
    combat["weapon_mode"] = WeaponMode(combat["weapon_mode"])
    combat["current_stance"] = StanceOp(combat["current_stance"])
    combat["queued_swing"] = SwingQueueOp(combat["queued_swing"])
    outer = dict(observation)
    outer["combat"] = FuryExpertState(**combat)
    outer["inventory_items"] = tuple(
        CatInventoryItemV4(**item) for item in outer["inventory_items"]
    )
    state = CatFuryFullPolicyStateV4(**outer)
    base = validate_source_decision_v4(CatFuryFullPolicyAdapterV4().propose(state))
    if base.to_dict() != step.get("proposal"):
        raise BranchReplayMismatchV1("reconstructed current Cat state changes its proposal")
    return state


def _visited_branch_points(
    baseline: Mapping[str, Any], *, max_states: int,
) -> list[tuple[int, str]]:
    """Select early/middle/late visited states before looking at rewards."""
    steps = baseline.get("steps") or []
    strata: list[list[tuple[int, tuple[str, ...]]]] = [[], [], []]
    for index, step in enumerate(steps):
        if step.get("decision_index") != index:
            raise BranchReplayMismatchV1("Cat decision indices are not consecutive")
        state = _state_from_step(step)
        base = validate_source_decision_v4(CatFuryFullPolicyAdapterV4().propose(state))
        kinds = available_branches_v1(state, base)
        if kinds:
            strata[min(2, index * 3 // max(1, len(steps)))].append((index, kinds))
    selected: list[tuple[int, tuple[str, ...]]] = []
    while len(selected) < max_states and any(strata):
        for group in strata:
            if group and len(selected) < max_states:
                selected.append(group.pop(0))
    return [(index, kind) for index, kinds in selected for kind in kinds]


def run_cat_action_branch_search_v1(
    case: DevelopmentWaveCaseV1, bridge_factory: Callable[[], Any],
    *, max_states: int = 6,
) -> dict[str, Any]:
    """Evaluate predeclared alternatives at several Cat-visited decisions."""
    if type(max_states) is not int or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    seed = case.dynamic_load.seed
    arguments = dict(seed=seed, target_contexts=case.target_contexts, dynamic_load=case.dynamic_load)
    baseline = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_fury_full_policy_rollout_v6(
            bridge, case.request, CatFuryFullPolicyAdapterV4(), **arguments,
        ),
    )
    _seed_receipt(baseline, seed)
    baseline_terminal = adjudicate_development_wave_completion_v1(case, baseline)
    if baseline_terminal["status"] != "COMPLETED":
        return {
            "schema": SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            "seed": seed, "source_wave_ref": case.case_spec["source_wave_ref"],
            "status": "BASELINE_INCOMPLETE_NO_BRANCHES_SCORED",
            "baseline_terminal": baseline_terminal,
            "visited_cat_decisions": len(baseline.get("steps") or []),
            "selected_state_count": 0, "independent_action_branch_count": 0,
            "accepted_action_branch_count": 0,
            "action_structure_count": 0, "completed_teacher_label_count": 0,
            "positive_single_seed_label_count": 0,
            "policy_update_rounds_completed": 0, "branches": [],
        }
    branches = []
    for index, kind in _visited_branch_points(baseline, max_states=max_states):
        alternative = _run_fresh(
            bridge_factory,
            lambda bridge: run_cat_action_branch_candidate_v1(
                bridge, case.request, CatActionBranchCandidateV1(ActionBranchV1(index, kind)),
                **arguments,
            ),
        )
        _seed_receipt(alternative, seed)
        _same_prefix(baseline, alternative, index)
        interventions = alternative.get("interventions") or []
        if len(interventions) != 1 or interventions[0]["decision_index"] != index:
            raise BranchReplayMismatchV1("targeted branch did not occur exactly once")
        if interventions[0]["cat_proposal"] != baseline["steps"][index]["proposal"]:
            raise BranchReplayMismatchV1("Cat decision changed at branch state")
        if any(step.get("policy_proposal_origin") != "CAT_UNCHANGED"
               for step in alternative["steps"][index + 1:]):
            raise BranchReplayMismatchV1("branch did not resume Cat continuation")
        terminal = adjudicate_development_wave_completion_v1(case, alternative)
        complete = baseline_terminal["status"] == terminal["status"] == "COMPLETED"
        def action_receipts(step: Mapping[str, Any]) -> list[dict[str, Any]]:
            execution = step.get("ordered_execution") or {}
            return [
                {
                    "source_sink": event.get("source_sink"),
                    "simulator_submission": event.get("simulator_submission"),
                    "simulator_acceptance": event.get("simulator_acceptance"),
                }
                for event in execution.get("sink_events") or []
                if isinstance(event, Mapping)
                and isinstance(event.get("source_sink"), Mapping)
                and event["source_sink"].get("channel") in {"gcd", "swing_queue"}
            ]
        baseline_actions = action_receipts(baseline["steps"][index])
        candidate_actions = action_receipts(alternative["steps"][index])
        state_changed_after_commands = (
            baseline["steps"][index].get("simulator_state_after_commands")
            != alternative["steps"][index].get("simulator_state_after_commands")
        )
        if kind in {"ADD_HS_QUEUE", "BT_TO_WW", "WW_TO_BT"}:
            accepted_action = any(
                (row.get("source_sink") or {}).get("source_ref", "").startswith(POLICY_ID)
                and (row.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
                for row in candidate_actions
            )
        else:
            removed_channel = "swing_queue" if kind == "SUPPRESS_QUEUE" else "gcd"
            accepted_action = any(
                (row.get("source_sink") or {}).get("channel") == removed_channel
                and (row.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
                for row in baseline_actions
            )
        branches.append({
            "decision_index": index, "kind": kind,
            "accepted_prefix_decisions_verified": index,
            "policy_observation": _observation_receipt(interventions[0]["policy_observation"]),
            "cat_proposal": interventions[0]["cat_proposal"],
            "candidate_proposal": interventions[0]["candidate_proposal"],
            "branch_action_receipt": {
                "cat": baseline_actions, "candidate": candidate_actions,
            },
            "branch_state_changed_after_commands": state_changed_after_commands,
            "branch_action_accepted": accepted_action,
            "branch_terminal": terminal,
            "paired_effective_damage_delta": (
                terminal["own_effective_damage"] - baseline_terminal["own_effective_damage"]
                if complete else None
            ),
            "status": (
                "COMPLETE_BRANCH_SMOKE" if complete and accepted_action
                else "COMPLETE_NO_ACCEPTED_ACTION" if complete
                else "CENSORED_OR_FAILED_BRANCH"
            ),
        })
    return {
        "schema": SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY", "seed": seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "replay_method": "FRESH_SAME_SEED_NATIVE_LOAD_PER_BRANCH_WITH_ACCEPTED_PREFIX_CHECK",
        "hidden_rng_snapshot_verified": False, "causal_effect_established": False,
        "baseline_terminal": baseline_terminal,
        "visited_cat_decisions": len(baseline.get("steps") or []),
        "selected_state_count": len({row["decision_index"] for row in branches}),
        "independent_action_branch_count": len(branches),
        "accepted_action_branch_count": sum(row["branch_action_accepted"] for row in branches),
        "action_structure_count": len({row["kind"] for row in branches}),
        "completed_teacher_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_SMOKE" for row in branches
        ),
        "positive_single_seed_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_SMOKE"
            and row["paired_effective_damage_delta"] > 0 for row in branches
        ),
        "policy_update_rounds_completed": 0,
        "branches": branches,
    }


def _run_cli_seed(
    seed: int, stratum: str, max_states: int, bridge: Path, bridge_cwd: Path,
) -> dict[str, Any]:
    if stratum == "controlled":
        case = build_development_wave_case_v1(seed)
    else:
        from .development_wave_stratified_v1 import build_stratified_wave_case_v1
        case, _ = build_stratified_wave_case_v1(seed, stratum)
    return run_cat_action_branch_search_v1(
        case,
        lambda: SimulatorBridgeDynamicV3(bridge, cwd=bridge_cwd),
        max_states=max_states,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2026091301)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--stratum", choices=("controlled", "single_short", "single_medium", "single_long", "multi_two"), default="controlled")
    parser.add_argument("--max-states", type=int, default=6)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=PROJECT_ROOT.parent / "wowsims-turtle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seed_count < 1 or args.workers < 1:
        parser.error("--seed-count and --workers must be positive")
    seeds = range(args.seed, args.seed + args.seed_count)
    if args.workers == 1:
        runs = [
            _run_cli_seed(seed, args.stratum, args.max_states, args.bridge, args.bridge_cwd)
            for seed in seeds
        ]
    else:
        with ProcessPoolExecutor(max_workers=min(args.workers, args.seed_count)) as pool:
            runs = list(pool.map(
                _run_cli_seed, seeds,
                [args.stratum] * args.seed_count,
                [args.max_states] * args.seed_count,
                [args.bridge] * args.seed_count,
                [args.bridge_cwd] * args.seed_count,
            ))
    result = runs[0] if args.seed_count == 1 else {
        "schema": "cat_action_branch_seed_panel/v2",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "stratum": args.stratum,
        "seed_count": len(runs),
        "visited_cat_decisions": sum(row["visited_cat_decisions"] for row in runs),
        "selected_state_count": sum(row["selected_state_count"] for row in runs),
        "independent_action_branch_count": sum(row["independent_action_branch_count"] for row in runs),
        "accepted_action_branch_count": sum(row["accepted_action_branch_count"] for row in runs),
        "completed_teacher_label_count": sum(row["completed_teacher_label_count"] for row in runs),
        "positive_single_seed_label_count": sum(row["positive_single_seed_label_count"] for row in runs),
        "policy_update_rounds_completed": 0,
        "runs": runs,
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
