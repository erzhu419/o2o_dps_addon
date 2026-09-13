"""Small in-memory Cat/residual action trace for selected development seeds.

This diagnostic replays only the requested seed and two native lanes. It never
persists producer artifacts or treats a matched seed as a historical combat log.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from . import development_wave_panel_v1 as panel_module
from .cat_residual_candidate_rollout_v1 import POLICY_ID as RESIDUAL_ID
from .development_wave_case_v1 import build_development_wave_scenario_v1
from .fury_cat_gap_three_baseline_registry_v1 import (
    CatGapThreeBaselineRegistryV1,
    build_cat_gap_three_baseline_registry_v1,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID


def project_action_artifact_v1(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded counts and accepted actions, never the full raw artifact."""

    accepted: list[dict[str, Any]] = []
    typed: Counter[str] = Counter()
    for step in artifact["steps"]:
        before = step["simulator_state_before"]
        for event in step["ordered_execution"]["sink_events"]:
            if event["simulator_acceptance"]["status"] != "ACCEPTED":
                continue
            sink = event["source_sink"]
            contract = event.get("operation_contract", {})
            accepted.append({
                "time_ms": before["time_ms"],
                "channel": sink["channel"],
                "operation": sink["operation"],
                "action": contract.get("canonical_action") or sink["operation"],
                "rage_before": before["power"]["current"],
                "mh_remaining_ms": before["mh_swing_remaining_ms"],
            })
        observation = step.get("server_observation_after_advance", {})
        result = observation.get("result_stream", {})
        for event in result.get("events", []):
            action = event.get("action") or {}
            key = f"{action.get('spell_id')}:{event.get('outcome')}"
            typed[key] += 1
    return {
        "accepted": accepted,
        "accepted_counts": dict(Counter(
            f"{row['channel']}:{row['action']}"
            for row in accepted
        )),
        "typed_result_counts": dict(typed),
        "final_rage": artifact.get("final_state", {}).get("power", {}).get("current"),
        "interventions": [
            {
                "decision_index": row["decision_index"],
                "combat_elapsed_s": row["combat_elapsed_s"],
                "rage": row["rage"],
                "cat_reserve_rage": row["cat_reserve_rage"],
                "candidate_reserve_rage": row["candidate_reserve_rage"],
            }
            for row in artifact.get("interventions", [])
        ],
    }


def run_selected_seed_v1(
    seed: int,
    discount: float,
    *,
    bridge_path: Path = panel_module.DEFAULT_BRIDGE,
    bridge_cwd: Path = panel_module.WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = panel_module.DEFAULT_BINDING,
) -> dict[str, Any]:
    scenario = build_development_wave_scenario_v1(seed)
    captured: dict[str, Mapping[str, Any]] = {}

    def registry_factory(bridge: Any, candidate: Any, binding_path: Path) -> CatGapThreeBaselineRegistryV1:
        registry = build_cat_gap_three_baseline_registry_v1(
            bridge, scenarios=[scenario],
            candidate_policies={candidate.policy_id: candidate},
            runtime_binding_path=binding_path,
        )
        original = registry.executors[CAT_POLICY_ID]

        def capture_cat(*, group: Mapping[str, Any], scenario: Mapping[str, Any], policy: Mapping[str, Any]) -> Mapping[str, Any]:
            envelope = original(group=group, scenario=scenario, policy=policy)
            captured[CAT_POLICY_ID] = envelope["lane_result"]["artifact"]
            return envelope

        executors = dict(registry.executors)
        executors[CAT_POLICY_ID] = capture_cat
        return replace(registry, executors=executors)

    original_residual = panel_module.execute_cat_residual_runner_v4_lane_v1

    def capture_residual(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        envelope = original_residual(*args, **kwargs)
        captured[RESIDUAL_ID] = envelope["lane_result"]["artifact"]
        return envelope

    with patch.object(panel_module, "execute_cat_residual_runner_v4_lane_v1", capture_residual):
        panel = panel_module.run_development_wave_panel_v1(
            master_seed=seed, bridge_path=bridge_path, bridge_cwd=bridge_cwd,
            runtime_binding_path=runtime_binding_path, candidate_kind="cat_residual",
            residual_discount_rage=discount, baseline_ids=(CAT_POLICY_ID,),
            registry_factory=registry_factory,
        )
    if panel["completed_count"] != 2 or set(captured) != {CAT_POLICY_ID, RESIDUAL_ID}:
        raise RuntimeError(f"selected seed did not complete two traces: {panel['status']}")
    rows = {row["policy_id"]: row for row in panel["rows"]}
    return {
        "seed": seed,
        "discount": discount,
        "paired_effective_damage": {
            policy_id: rows[policy_id]["own_effective_damage"]
            for policy_id in (CAT_POLICY_ID, RESIDUAL_ID)
        },
        "paired_ttk_ms": {
            policy_id: rows[policy_id]["ttk_ms"]
            for policy_id in (CAT_POLICY_ID, RESIDUAL_ID)
        },
        "traces": {
            policy_id: project_action_artifact_v1(captured[policy_id])
            for policy_id in (CAT_POLICY_ID, RESIDUAL_ID)
        },
        "scope": "TWO_LANE_SELECTED_SEED_REPLAY_DIAGNOSTIC_ONLY",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--discount", type=float, required=True)
    parser.add_argument("--bridge", type=Path, default=panel_module.DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=panel_module.WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=panel_module.DEFAULT_BINDING)
    args = parser.parse_args()
    result = run_selected_seed_v1(
        args.seed, args.discount, bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd, runtime_binding_path=args.runtime_binding,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
