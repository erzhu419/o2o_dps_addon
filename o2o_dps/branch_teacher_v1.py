"""One-decision Cat-relative branch by deterministic native replay.

There is no native simulator snapshot/restore.  Each lane therefore starts a
fresh bridge with the same seed and verifies the accepted decision prefix.
This is a development counterfactual, not an assertion that hidden RNG state
was restored or that a one-seed difference establishes a better policy.
"""

from __future__ import annotations

import argparse
from dataclasses import fields
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
)
from .cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from .cat_residual_candidate_rollout_v1 import (
    CatQueueResidualV1,
    CatResidualCandidateV1,
    run_cat_residual_candidate_v1,
)
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
)
from .fury_expert_adapters import FuryExpertState
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


SCHEMA = "cat_relative_branch_teacher/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = PROJECT_ROOT / "bin/o2obridge.seedfix-v11.dynamicv3horizonround.withdb.goamd64v1.windows-amd64.exe"
_PREFIX_FIELDS = (
    "simulator_state_before",
    "available_actions_before",
    "target_semantics",
    "expert_state",
    "proposal",
    "ordered_execution",
    "server_observation_after_advance",
    "simulator_state_after_commands",
    "simulator_state_next_epoch",
)
_DECISION_STATE_FIELDS = (
    "simulator_state_before",
    "available_actions_before",
    "target_semantics",
    "expert_state",
)


class BranchReplayMismatchV1(RuntimeError):
    """A fresh replay did not reach the same observed decision prefix."""


def _run_fresh(bridge_factory: Callable[[], Any], run: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    bridge = bridge_factory()
    try:
        return run(bridge)
    finally:
        close = getattr(bridge, "close", None)
        if callable(close):
            close()


def _same_prefix(
    baseline: Mapping[str, Any], alternative: Mapping[str, Any],
    decision_index: int,
) -> int:
    base_steps = baseline.get("steps") or []
    other_steps = alternative.get("steps") or []
    if len(base_steps) <= decision_index or len(other_steps) <= decision_index:
        raise BranchReplayMismatchV1("one replay ended before the proposed branch")
    for index in range(decision_index):
        base, other = base_steps[index], other_steps[index]
        if base.get("decision_index") != index or other.get("decision_index") != index:
            raise BranchReplayMismatchV1(f"decision index drifted at {index}")
        for key in _PREFIX_FIELDS:
            if base.get(key) != other.get(key):
                raise BranchReplayMismatchV1(f"accepted prefix differs at decision {index}: {key}")
    base, other = base_steps[decision_index], other_steps[decision_index]
    for key in _DECISION_STATE_FIELDS:
        if base.get(key) != other.get(key):
            raise BranchReplayMismatchV1(f"branch observation differs: {key}")
    return decision_index


def _seed_receipt(artifact: Mapping[str, Any], seed: int) -> None:
    binding = artifact.get("dynamic_load_binding")
    if (
        artifact.get("seed") != seed
        or not isinstance(binding, Mapping)
        or binding.get("simulator_seed") != seed
        or binding.get("load_succeeded") is not True
    ):
        raise BranchReplayMismatchV1("replay seed/load receipt does not match the case")


def _observation_receipt(observation: Mapping[str, Any]) -> dict[str, Any]:
    outer = {item.name for item in fields(CatFuryFullPolicyStateV4)}
    combat = {item.name for item in fields(FuryExpertState)}
    if set(observation) != outer or not isinstance(observation.get("combat"), Mapping):
        raise BranchReplayMismatchV1("candidate observation is not the declared Cat state")
    if set(observation["combat"]) != combat:
        raise BranchReplayMismatchV1("candidate combat observation has undeclared fields")
    forbidden = {"seed", "simulator_seed", "future_schedule", "background_damage_events", "source_death_ms"}
    if forbidden.intersection(outer | combat):
        raise BranchReplayMismatchV1("Cat policy state exposes a future or seed field")
    return {
        "schema": "cat_fury_full_policy_state/v4",
        "available_field_count": len(outer) + len(combat),
        "future_team_schedule_visible": False,
        "seed_visible": False,
        "observation": dict(observation),
    }


def run_cat_relative_branch_teacher_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    *,
    reserve_discount_rage: float = 25.0,
) -> dict[str, Any]:
    """Return a compact, auditable proposal/value record for one eligible state."""

    seed = case.dynamic_load.seed
    arguments = dict(
        seed=seed, target_contexts=case.target_contexts,
        dynamic_load=case.dynamic_load,
    )
    baseline = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_fury_full_policy_rollout_v6(
            bridge, case.request, CatFuryFullPolicyAdapterV4(), **arguments
        ),
    )
    probe = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_residual_candidate_v1(
            bridge, case.request,
            CatResidualCandidateV1(CatQueueResidualV1(reserve_discount_rage)),
            **arguments,
        ),
    )
    for artifact in (baseline, probe):
        _seed_receipt(artifact, seed)
    baseline_terminal = adjudicate_development_wave_completion_v1(case, baseline)
    common = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": seed,
        "case_spec": case.case_spec["schema"],
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "required_target_ids": case.case_spec["required_target_ids"],
        "replay_method": "THREE_FRESH_SAME_SEED_NATIVE_LOADS_WITH_ACCEPTED_PREFIX_CHECK",
        "hidden_rng_snapshot_verified": False,
        "causal_effect_established": False,
        "uncertainty": "ONE_SEED_SMOKE_NO_CONFIDENCE_INTERVAL",
        "baseline_terminal": baseline_terminal,
    }
    interventions = probe.get("interventions") or []
    if not interventions:
        if len(baseline.get("steps") or []) != len(probe.get("steps") or []):
            raise BranchReplayMismatchV1("no-op probe changed decision count")
        if baseline.get("steps"):
            _same_prefix(baseline, probe, len(baseline["steps"]) - 1)
            for key in _PREFIX_FIELDS:
                if baseline["steps"][-1].get(key) != probe["steps"][-1].get(key):
                    raise BranchReplayMismatchV1(f"no-op final decision differs: {key}")
        return {**common, "status": "NO_ADMISSIBLE_BRANCH", "proposal": None, "value": None}

    first = interventions[0]
    index = first["decision_index"]
    _same_prefix(baseline, probe, index)
    if first["cat_proposal"] != baseline["steps"][index]["proposal"]:
        raise BranchReplayMismatchV1("probe's Cat proposal differs from baseline")
    targeted = _run_fresh(
        bridge_factory,
        lambda bridge: run_cat_residual_candidate_v1(
            bridge, case.request,
            CatResidualCandidateV1(
                CatQueueResidualV1(reserve_discount_rage, intervention_at_decision=index)
            ),
            **arguments,
        ),
    )
    _seed_receipt(targeted, seed)
    _same_prefix(baseline, targeted, index)
    changed = targeted.get("interventions") or []
    if len(changed) != 1 or changed[0]["decision_index"] != index:
        raise BranchReplayMismatchV1("targeted replay did not make exactly one intervention")
    if targeted["steps"][index].get("cat_baseline_proposal") != baseline["steps"][index]["proposal"]:
        raise BranchReplayMismatchV1("Cat proposal at the branch differs from baseline")
    if first["policy_observation"] != changed[0]["policy_observation"]:
        raise BranchReplayMismatchV1("probe and targeted branch observations differ")
    if first["candidate_proposal"] != changed[0]["candidate_proposal"]:
        raise BranchReplayMismatchV1("probe and targeted branch proposals differ")
    if any(
        step.get("policy_proposal_origin") != "CAT_UNCHANGED"
        for step in targeted["steps"][index + 1 :]
    ):
        raise BranchReplayMismatchV1("targeted replay did not resume Cat proposals")

    branch_terminal = adjudicate_development_wave_completion_v1(case, targeted)
    branch_execution = targeted["steps"][index].get("ordered_execution") or {}
    queue_sink_receipts = [
        {
            "source_sink": event.get("source_sink"),
            "simulator_submission": event.get("simulator_submission"),
        }
        for event in branch_execution.get("sink_events", [])
        if isinstance(event, Mapping)
        and isinstance(event.get("source_sink"), Mapping)
        and event["source_sink"].get("channel") == "swing_queue"
    ]
    both_complete = (
        baseline_terminal["status"] == "COMPLETED"
        and branch_terminal["status"] == "COMPLETED"
    )
    baseline_value = baseline_terminal.get("own_effective_damage") if both_complete else None
    branch_value = branch_terminal.get("own_effective_damage") if both_complete else None
    return {
        **common,
        "status": "COMPLETE_BRANCH_SMOKE" if both_complete else "CENSORED_OR_FAILED_BRANCH",
        "decision_index": index,
        "accepted_prefix_decisions_verified": index,
        "branch_observation_equal": True,
        "policy_observation": _observation_receipt(changed[0]["policy_observation"]),
        "proposal": {
            "cat": first["cat_proposal"],
            "branch": changed[0]["candidate_proposal"],
            "rule": "two_hand_hs_reserve_discount",
            "reserve_discount_rage": reserve_discount_rage,
            "cat_reserve_rage": first["cat_reserve_rage"],
            "rage": first["rage"],
            "queue_sink_simulator_receipts": queue_sink_receipts,
        },
        "branch_terminal": branch_terminal,
        "value": {
            "metric": "own_effective_damage_at_all_targets_dead",
            "cat": baseline_value,
            "branch": branch_value,
            "paired_delta": branch_value - baseline_value if both_complete else None,
            "cat_ttk_ms": baseline_terminal.get("elapsed_ms") if both_complete else None,
            "branch_ttk_ms": branch_terminal.get("elapsed_ms") if both_complete else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2026091301)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    case = build_development_wave_case_v1(args.seed)
    result = run_cat_relative_branch_teacher_v1(
        case,
        lambda: SimulatorBridgeDynamicV3(args.bridge, cwd=PROJECT_ROOT.parent / "wowsims-turtle"),
    )
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
