"""Predeclared Bloodrage timing probe on one native two-wave environment.

The candidate omits Cat's Bloodrage calls before the second-wave unlock.
All later proposals are Cat proposals. The policy sees only the current Cat
observation and a fixed elapsed-time threshold.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import types
from typing import Any, Mapping

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import cat_fury_full_policy_rollout_v6 as _cat_v6
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .branch_teacher_v1 import (
    BranchReplayMismatchV1, _DECISION_STATE_FIELDS, _PREFIX_FIELDS,
    _run_fresh, _seed_receipt,
)
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from .cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .development_two_wave_build_panel_v1 import (
    BUILD_IDS, SECOND_WAVE_UNLOCK_MS, build_two_wave_build_case_v1,
)
from .development_wave_case_v1 import adjudicate_development_wave_completion_v1
from .development_wave_panel_v1 import DEFAULT_BRIDGE, WORKSPACE_ROOT
from .expert_policy import ExpertDecision
from .fury_expert_adapters import BLOODRAGE
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


SCHEMA = "development_two_wave_bloodrage/v1"
POLICY_ID = "cat_relative_defer_first_wave_bloodrage/v1"
# Cat's modeled combat clock begins at 10 s while the bridge clock begins at 0;
# this fixed threshold is specific to the synthetic second-wave unlock at 12 s.
FIRST_WAVE_OBSERVED_COMBAT_ELAPSED_LIMIT_S = 22.0
BLOODRAGE_NAME = "血性狂暴"


class DeferFirstWaveBloodrageV1:
    expert_id = POLICY_ID

    def __init__(self) -> None:
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.interventions: list[dict[str, Any]] = []

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        index = self.decision_count
        self.decision_count += 1
        base = validate_source_decision_v4(self.cat.propose(state))
        if state.combat_elapsed_s >= FIRST_WAVE_OBSERVED_COMBAT_ELAPSED_LIMIT_S:
            return base
        kept = tuple(
            sink for sink in base.raw_sink_order
            if not (sink.channel == "off_gcd" and sink.value == BLOODRAGE_NAME)
        )
        if len(kept) == len(base.raw_sink_order):
            return base
        proposal = validate_source_decision_v4(replace(
            base, raw_sink_order=kept,
            off_gcd=tuple(action for action in base.off_gcd if action != BLOODRAGE),
            reason=f"{base.reason}; candidate reserves Bloodrage until wave two",
            metadata={**base.metadata, "candidate_action_plan": POLICY_ID},
        ))
        self.interventions.append({
            "decision_index": index,
            "combat_elapsed_s": state.combat_elapsed_s,
            "rage": state.combat.rage,
            "bloodrage_ready": state.combat.bloodrage_ready,
            "cat_proposal": base.to_dict(),
            "candidate_proposal": proposal.to_dict(),
        })
        return proposal


def _candidate_core(
    controls: CatSimulatorControlFacadeV5, inputs: _cat_v5.CatFurySimulatorInputsV5,
) -> Any:
    core = _cat_v6._clone_core_v6(controls, inputs)
    namespace = dict(core.__globals__)
    namespace["_supported_adapter"] = lambda adapter: type(adapter) is DeferFirstWaveBloodrageV1

    def proposal(adapter: DeferFirstWaveBloodrageV1, state: CatFuryFullPolicyStateV4,
                 target: Mapping[str, Any]) -> ExpertDecision:
        del target
        return adapter.propose(state)

    namespace["_proposal"] = proposal
    clone = types.FunctionType(
        core.__code__, namespace, name="_run_defer_bloodrage_core",
        argdefs=core.__defaults__, closure=core.__closure__,
    )
    clone.__kwdefaults__ = dict(core.__kwdefaults__ or {})
    return clone


def _run_candidate(bridge: Any, case: Any, policy: DeferFirstWaveBloodrageV1) -> dict[str, Any]:
    inputs = _cat_v5.CatFurySimulatorInputsV5()
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
        facade, case.request, policy, seed=case.dynamic_load.seed,
        target_contexts=case.target_contexts, dynamic_load=case.dynamic_load,
        max_decisions=10_000, max_advances=100_000, retain_steps=True,
    )
    _cat_v6._bind_source_reentry_next_epochs_v6(result)
    _dynamic_v5._rewrite_v5_identity(result, case.dynamic_load, facade.last_load_result)
    closure = _dynamic_v5._collect_runtime_receipts_v5(
        bridge, case.dynamic_load, facade.last_load_result, result.get("final_state"),
    )
    result["dynamic_v3_runtime_receipt_closure"] = closure
    # The Cat receipt pass binds ordered sink acceptance to the retained steps.
    _cat_v6._cat_v6_receipts(result, inputs, controls, closure)
    return result


def _timeline(terminal: Mapping[str, Any]) -> dict[str, Any]:
    targets = terminal.get("target_outcomes") or []
    deaths = [row.get("death_time_ms") for row in targets]
    valid = (
        terminal.get("status") == "COMPLETED"
        and len(deaths) == 2
        and isinstance(deaths[0], int) and deaths[0] <= 10_000
        and isinstance(deaths[1], int) and deaths[1] >= SECOND_WAVE_UNLOCK_MS
    )
    return {
        "two_wave_timeline_valid": valid,
        "wave_1_ttk_ms": deaths[0] if valid else None,
        "inter_wave_gap_ms": SECOND_WAVE_UNLOCK_MS - deaths[0] if valid else None,
        "wave_2_active_ms": deaths[1] - SECOND_WAVE_UNLOCK_MS if valid else None,
        "whole_two_wave_dps": (
            terminal["own_effective_damage"] * 1000.0 / deaths[1]
            if valid and deaths[1] else None
        ),
    }


def _bloodrage_acceptance(step: Mapping[str, Any]) -> bool:
    execution = step.get("ordered_execution") or {}
    return any(
        isinstance(row, Mapping)
        and (row.get("source_sink") or {}).get("channel") == "off_gcd"
        and (row.get("source_sink") or {}).get("value") == BLOODRAGE_NAME
        and (row.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
        for row in execution.get("sink_events") or []
    )


def _first_accepted_bloodrage_time_ms(rollout: Mapping[str, Any]) -> int | None:
    for step in rollout.get("steps") or []:
        if _bloodrage_acceptance(step):
            return step["simulator_state_before"]["time_ms"]
    return None


def _causal_ordered_execution(step: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude only future-attached result followups from the accepted prefix."""
    execution = step.get("ordered_execution") or {}
    return {
        **execution,
        "sink_events": [
            {key: value for key, value in event.items() if key != "simulator_outcome_followup"}
            for event in execution.get("sink_events") or []
        ],
    }


def _same_causal_prefix(baseline: Mapping[str, Any], candidate: Mapping[str, Any],
                        decision_index: int) -> None:
    left = baseline.get("steps") or []
    right = candidate.get("steps") or []
    if len(left) <= decision_index or len(right) <= decision_index:
        raise BranchReplayMismatchV1("one replay ended before Bloodrage choice")
    for index in range(decision_index):
        for key in _PREFIX_FIELDS:
            a, b = left[index].get(key), right[index].get(key)
            if key == "ordered_execution":
                a, b = _causal_ordered_execution(left[index]), _causal_ordered_execution(right[index])
            if a != b:
                raise BranchReplayMismatchV1(f"accepted causal prefix differs at {index}: {key}")
    for key in _DECISION_STATE_FIELDS:
        if left[decision_index].get(key) != right[decision_index].get(key):
            raise BranchReplayMismatchV1(f"Bloodrage choice observation differs: {key}")


def run_two_wave_bloodrage_v1(
    seed: int, *, build_id: str = BUILD_IDS[0], bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
) -> dict[str, Any]:
    """Native same-seed Cat comparator and one-policy ActionPlan candidate."""
    case, _ = build_two_wave_build_case_v1(seed, build_id)
    bridge_factory = lambda: SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd)
    arguments = dict(
        seed=seed, target_contexts=case.target_contexts, dynamic_load=case.dynamic_load,
    )
    baseline = _run_fresh(bridge_factory, lambda bridge: run_cat_fury_full_policy_rollout_v6(
        bridge, case.request, CatFuryFullPolicyAdapterV4(), **arguments,
    ))
    policy = DeferFirstWaveBloodrageV1()
    candidate = _run_fresh(bridge_factory, lambda bridge: _run_candidate(bridge, case, policy))
    _seed_receipt(baseline, seed)
    _seed_receipt(candidate, seed)
    cat = {**adjudicate_development_wave_completion_v1(case, baseline)}
    branch = {**adjudicate_development_wave_completion_v1(case, candidate)}
    cat.update(_timeline(cat))
    branch.update(_timeline(branch))
    index = policy.interventions[0]["decision_index"] if policy.interventions else None
    if index is not None:
        _same_causal_prefix(baseline, candidate, index)
    baseline_accepted = index is not None and _bloodrage_acceptance(baseline["steps"][index])
    cat_bloodrage_time_ms = _first_accepted_bloodrage_time_ms(baseline)
    candidate_bloodrage_time_ms = _first_accepted_bloodrage_time_ms(candidate)
    candidate_later_accepted = (
        candidate_bloodrage_time_ms is not None
        and candidate_bloodrage_time_ms >= SECOND_WAVE_UNLOCK_MS
    )
    complete = cat["two_wave_timeline_valid"] and branch["two_wave_timeline_valid"]
    eligible = bool(complete and baseline_accepted and index is not None)
    no_op = bool(complete and index is None and cat == branch)
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": seed, "build_id": build_id,
        "policy_id": POLICY_ID,
        "predeclared_rule": "OMIT_BLOODRAGE_WHEN_OBSERVED_COMBAT_ELAPSED_LT_22S_THEN_CAT",
        "same_native_environment_across_waves": True,
        "independent_wave_reset": False,
        "hidden_rng_snapshot_verified": False,
        "causal_effect_established": False,
        "cat": cat, "candidate": branch,
        "interventions": policy.interventions,
        "cat_branch_bloodrage_accepted": baseline_accepted,
        "candidate_later_bloodrage_accepted": candidate_later_accepted,
        "cat_bloodrage_time_ms": cat_bloodrage_time_ms,
        "candidate_bloodrage_time_ms": candidate_bloodrage_time_ms,
        "paired_effective_damage_delta": (
            branch["own_effective_damage"] - cat["own_effective_damage"] if eligible else
            0.0 if no_op else None
        ),
        "paired_whole_two_wave_dps_delta": (
            branch["whole_two_wave_dps"] - cat["whole_two_wave_dps"] if eligible else
            0.0 if no_op else None
        ),
        "status": (
            "COMPLETE_ACTION_COMPARISON" if eligible else
            "NO_OP_CAT_ALREADY_RESERVED" if no_op else
            "INCOMPLETE_OR_NO_ACCEPTED_CAT_ACTION"
        ),
        "deployment_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--build-id", choices=BUILD_IDS, default=BUILD_IDS[0])
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_two_wave_bloodrage_v1(
        args.seed, build_id=args.build_id, bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "seed": args.seed, "build_id": args.build_id, "status": result["status"],
        "cat_damage": result["cat"].get("own_effective_damage"),
        "candidate_damage": result["candidate"].get("own_effective_damage"),
        "delta": result["paired_effective_damage_delta"],
        "cat_ttk": [r["death_time_ms"] for r in result["cat"].get("target_outcomes") or []],
        "candidate_ttk": [r["death_time_ms"] for r in result["candidate"].get("target_outcomes") or []],
        "cat_branch_bloodrage_accepted": result["cat_branch_bloodrage_accepted"],
        "candidate_later_bloodrage_accepted": result["candidate_later_bloodrage_accepted"],
        "cat_bloodrage_time_ms": result["cat_bloodrage_time_ms"],
        "candidate_bloodrage_time_ms": result["candidate_bloodrage_time_ms"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
