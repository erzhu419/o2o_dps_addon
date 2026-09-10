"""Stream bounded Fury simulator transitions without retaining full rollouts.

Each JSONL row is explicitly simulated and points back to one ``PolicyScenario``
whose full provenance is stored once in the companion manifest.  Adding seeds
can produce arbitrarily many simulator samples, but those samples are not new
independent evidence about the real game or the Chronicle encounters.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .fury_expert_closed_loop import run_fury_expert_closed_loop
from .fury_policy_optimization_v1 import (
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    PolicyScenario,
    scenario_from_request,
    scenarios_from_catalog,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_TRANSITIONS = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_teacher"
    / "fury_synthetic_rollouts_v1.jsonl"
)
DEFAULT_MANIFEST = DEFAULT_TRANSITIONS.with_suffix(".manifest.json")
DEFAULT_SEEDS = (2026090131, 2026090132, 2026090133, 2026090134)
TERMINAL_WAIT_CAP_REASON = "gcd:wait_capped_to_remaining_horizon"


class FurySyntheticRolloutError(RuntimeError):
    """The bounded streaming generation contract could not be satisfied."""


class PolicyAdapterLike(Protocol):
    expert_id: str

    def propose(self, state: Any) -> Any: ...


def generate_fury_synthetic_rollouts(
    bridge: Any,
    policy: FuryPolicyParameters | PolicyAdapterLike,
    scenarios: Iterable[PolicyScenario],
    *,
    seeds: Iterable[int],
    transitions_path: str | Path,
    manifest_path: str | Path,
    max_rollouts: int | None = None,
) -> JSONMap:
    """Generate compact transition JSONL plus a small reproducibility manifest.

    The transition sink writes each decision immediately.  The closed-loop
    runner is invoked with ``retain_steps=False`` so no complete rollout trace
    is accumulated in memory or copied into the manifest.
    """

    adapter, parameters = _policy_adapter(policy)
    scenario_values = tuple(scenarios)
    if not scenario_values:
        raise ValueError("scenarios must not be empty")
    scenario_ids = [scenario.scenario_id for scenario in scenario_values]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("scenario IDs must be unique")
    seed_values = _seed_tuple(seeds)
    if max_rollouts is not None:
        if isinstance(max_rollouts, bool) or not isinstance(max_rollouts, int):
            raise TypeError("max_rollouts must be an integer")
        if max_rollouts <= 0:
            raise ValueError("max_rollouts must be positive")

    transition_output = Path(transitions_path)
    manifest_output = Path(manifest_path)
    if transition_output.resolve() == manifest_output.resolve():
        raise ValueError("transitions_path and manifest_path must differ")
    transition_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_transitions = transition_output.with_name(
        transition_output.name + ".tmp"
    )

    transition_count = 0
    training_eligible_count = 0
    boundary_event_count = 0
    lane_nonfaithful_count = 0
    omission_count = 0
    exclusion_reasons: Counter[str] = Counter()
    rollout_summaries: list[JSONMap] = []
    policy_provenance: JSONMap | None = None
    emitted_rollouts = 0

    try:
        with temporary_transitions.open(
            "w", encoding="utf-8", newline="\n", buffering=1024 * 1024
        ) as output:
            stop = False
            for scenario in scenario_values:
                if stop:
                    break
                for seed in seed_values:
                    if max_rollouts is not None and emitted_rollouts >= max_rollouts:
                        stop = True
                        break
                    rollout_id = f"{scenario.scenario_id}/seed-{seed}"

                    def sink(step: JSONMap) -> None:
                        nonlocal transition_count
                        nonlocal training_eligible_count
                        nonlocal boundary_event_count
                        nonlocal lane_nonfaithful_count
                        nonlocal omission_count
                        nonlocal policy_provenance

                        row = _transition_row(
                            step,
                            scenario=scenario,
                            seed=seed,
                            rollout_id=rollout_id,
                            manifest_name=manifest_output.name,
                        )
                        proposal = step.get("proposal")
                        if policy_provenance is None and isinstance(proposal, Mapping):
                            provenance = proposal.get("provenance")
                            if isinstance(provenance, Mapping):
                                policy_provenance = copy.deepcopy(dict(provenance))
                        output.write(
                            json.dumps(
                                row,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        transition_count += 1
                        training_eligible_count += int(row["training_eligible"])
                        boundary_event_count += int(row["boundary_event"])
                        lane_nonfaithful_count += int(
                            not row["lane_projection_faithful"]
                        )
                        omission_count += len(row["omissions"])
                        if not row["training_eligible"]:
                            exclusion_reasons[str(row["training_exclusion_reason"])] += 1

                    rollout = run_fury_expert_closed_loop(
                        bridge,
                        scenario.request,
                        adapter,
                        seed=seed,
                        horizon_ms=scenario.horizon_ms,
                        transition_sink=sink,
                        retain_steps=False,
                    )
                    if "steps" in rollout or rollout.get("steps_retained") is not False:
                        raise FurySyntheticRolloutError(
                            "closed-loop runner retained a full rollout despite streaming mode"
                        )
                    rollout_summaries.append(
                        _rollout_summary(rollout, scenario, seed)
                    )
                    emitted_rollouts += 1
                output.flush()
        temporary_transitions.replace(transition_output)
    except BaseException:
        temporary_transitions.unlink(missing_ok=True)
        raise

    manifest: JSONMap = {
        "schema_version": 1,
        "kind": "fury_synthetic_rollouts_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data_origin": "SIMULATED",
        "transition_kind": "fury_simulated_transition_v1",
        "transitions_path": str(transition_output),
        "manifest_path": str(manifest_output),
        "transition_bytes": transition_output.stat().st_size,
        "policy": {
            "policy_id": adapter.expert_id,
            "parameters": None if parameters is None else asdict(parameters),
            "provenance": policy_provenance,
            "source_execution": False,
            "exact_lua_replay": False,
        },
        "scenarios": [
            _scenario_manifest_row(scenario) for scenario in scenario_values
        ],
        "seeds": list(seed_values),
        "limits": {
            "possible_rollouts": len(scenario_values) * len(seed_values),
            "max_rollouts": max_rollouts,
            "emitted_rollouts": emitted_rollouts,
            "default_generation_is_finite": True,
        },
        "counts": {
            "rollouts": emitted_rollouts,
            "transitions": transition_count,
            "training_eligible": training_eligible_count,
            "training_excluded": transition_count - training_eligible_count,
            "terminal_boundary_events": boundary_event_count,
            "lane_nonfaithful_transitions": lane_nonfaithful_count,
            "omitted_lanes": omission_count,
            "training_exclusion_reasons": dict(sorted(exclusion_reasons.items())),
        },
        "rollout_summaries": rollout_summaries,
        "generation_contract": {
            "streaming_jsonl": True,
            "full_rollout_steps_retained": False,
            "chronicle_raw_data_read": False,
            "chronicle_raw_data_copied": False,
            "scenario_provenance_stored_once_in_manifest": True,
            "transition_scenario_lineage": (
                "each row stores scenario_id and a manifest provenance reference"
            ),
            "scale_semantics": (
                "additional seeds can generate arbitrarily many simulator transitions; "
                "they are not additional independent real-world evidence"
            ),
        },
        "training_contract": {
            "lane_nonfaithful_transition_eligible": False,
            "omitted_lane_transition_eligible": False,
            "terminal_wait_cap_is_boundary_event": True,
            "terminal_boundary_event_eligible": False,
        },
        "source_execution": False,
        "exact_lua_replay": False,
        "claims_excluded": [
            "new independent Chronicle evidence",
            "exact Cat or Contra Lua execution",
            "real-game transition truth",
            "real-game policy superiority",
        ],
    }
    _write_manifest(manifest_output, manifest)
    return manifest


def _transition_row(
    step: Mapping[str, Any],
    *,
    scenario: PolicyScenario,
    seed: int,
    rollout_id: str,
    manifest_name: str,
) -> JSONMap:
    omissions = step.get("omitted_lanes")
    omission_rows = copy.deepcopy(omissions) if isinstance(omissions, list) else []
    raw_reasons = step.get("nonfaithful_reasons")
    reasons = [str(value) for value in raw_reasons] if isinstance(raw_reasons, list) else []
    next_state = step.get("simulator_state_next_epoch")
    if not isinstance(next_state, Mapping):
        raise FurySyntheticRolloutError("streamed step lacks next simulator state")
    next_time = next_state.get("time_ms")
    if isinstance(next_time, bool) or not isinstance(next_time, int):
        raise FurySyntheticRolloutError("streamed next state lacks integer time_ms")
    terminal = bool(next_state.get("finished")) or next_time >= scenario.horizon_ms
    boundary_cap = TERMINAL_WAIT_CAP_REASON in reasons and terminal
    lane_reasons = [
        reason
        for reason in reasons
        if not (boundary_cap and reason == TERMINAL_WAIT_CAP_REASON)
    ]
    lane_faithful = not omission_rows and not lane_reasons
    if omission_rows:
        training_eligible = False
        exclusion = "omitted_lane"
    elif lane_reasons:
        training_eligible = False
        exclusion = "lane_projection_nonfaithful"
    elif boundary_cap:
        training_eligible = False
        exclusion = "terminal_horizon_wait_cap_boundary"
    else:
        training_eligible = True
        exclusion = None

    decision_index = step.get("decision_index")
    if isinstance(decision_index, bool) or not isinstance(decision_index, int):
        raise FurySyntheticRolloutError("streamed step lacks integer decision_index")
    return {
        "schema_version": 1,
        "kind": "fury_simulated_transition_v1",
        "data_origin": "SIMULATED",
        "transition_id": f"{rollout_id}/decision-{decision_index}",
        "rollout_id": rollout_id,
        "scenario_id": scenario.scenario_id,
        "scenario_weight": float(scenario.weight),
        "scenario_provenance_ref": {
            "manifest": manifest_name,
            "scenario_id": scenario.scenario_id,
        },
        "seed": seed,
        "decision_index": decision_index,
        "state_before": copy.deepcopy(step.get("simulator_state_before")),
        "expert_state": copy.deepcopy(step.get("expert_state")),
        "proposal": _compact_proposal(step.get("proposal")),
        "executed_commands": copy.deepcopy(step.get("commands", [])),
        "omissions": omission_rows,
        "lane_nonfaithful_reasons": lane_reasons,
        "delta_time_ms": step.get("time_delta_ms"),
        "delta_damage": step.get("damage_delta"),
        "next_state": copy.deepcopy(next_state),
        "terminal": terminal,
        "boundary_event": boundary_cap,
        "boundary_event_kind": "TERMINAL_WAIT_CAP" if boundary_cap else None,
        "lane_projection_faithful": lane_faithful,
        "training_eligible": training_eligible,
        "training_exclusion_reason": exclusion,
        "source_execution": False,
        "exact_lua_replay": False,
    }


def _compact_proposal(value: Any) -> JSONMap | None:
    if not isinstance(value, Mapping):
        return None
    return {
        key: copy.deepcopy(value.get(key))
        for key in (
            "expert_id",
            "valid",
            "gcd",
            "swing_queue",
            "off_gcd",
            "stance",
            "target",
            "cast_control",
            "raw_sink_order",
            "eligible_for_independent_vote",
            "reason",
        )
    }


def _rollout_summary(
    rollout: Mapping[str, Any], scenario: PolicyScenario, seed: int
) -> JSONMap:
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_weight": float(scenario.weight),
        "seed": seed,
        "expert_id": rollout.get("expert_id"),
        "horizon_ms": scenario.horizon_ms,
        "damage": rollout.get("damage_delta"),
        "dps": rollout.get("dps"),
        "decision_count": rollout.get("decision_count"),
        "configured_horizon_complete": rollout.get("configured_horizon_complete"),
        "omitted_lane_count": rollout.get("omitted_lane_count"),
        "omitted_lane_counts": copy.deepcopy(rollout.get("omitted_lane_counts")),
        "nonfaithful_reason_counts": copy.deepcopy(
            rollout.get("nonfaithful_reason_counts")
        ),
        "source_execution": False,
        "exact_lua_replay": False,
    }


def _scenario_manifest_row(scenario: PolicyScenario) -> JSONMap:
    encounter = scenario.request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise FurySyntheticRolloutError(
            f"scenario {scenario.scenario_id} lacks encounter"
        )
    targets = encounter.get("targets")
    return {
        "scenario_id": scenario.scenario_id,
        "horizon_ms": scenario.horizon_ms,
        "weight": float(scenario.weight),
        "provenance": copy.deepcopy(scenario.provenance),
        "request_summary": {
            "mode": "duration",
            "source_duration_seconds": encounter.get("duration"),
            "effective_duration_seconds": scenario.horizon_ms / 1000.0,
            "target_count": len(targets) if isinstance(targets, list) else None,
        },
    }


def _policy_adapter(
    policy: FuryPolicyParameters | PolicyAdapterLike,
) -> tuple[PolicyAdapterLike, FuryPolicyParameters | None]:
    if isinstance(policy, FuryPolicyParameters):
        return FuryTunedPolicyAdapter(policy), policy
    expert_id = getattr(policy, "expert_id", None)
    propose = getattr(policy, "propose", None)
    if not isinstance(expert_id, str) or not expert_id or not callable(propose):
        raise TypeError("policy must be FuryPolicyParameters or an expert adapter")
    return policy, None


def _seed_tuple(seeds: Iterable[int]) -> tuple[int, ...]:
    values = tuple(seeds)
    if not values:
        raise ValueError("seeds must not be empty")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("seeds must contain integers")
    if len(set(values)) != len(values):
        raise ValueError("seeds must be unique")
    return values


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _load_parameters(path: Path | None) -> FuryPolicyParameters:
    if path is None:
        # Death Wish is represented as a GCD action by the current bridge while
        # the V1 policy factorizes it as off-GCD.  Keep the finite default corpus
        # on the faithful lane contract; explicit parameter files may still run
        # the cooldown-on probe and will receive training_eligible=false rows.
        return FuryPolicyParameters(use_death_wish=False)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise FurySyntheticRolloutError("policy parameters file must be an object")
    selected = document.get("selected_parameters")
    if isinstance(selected, Mapping):
        document = selected
    elif isinstance(document.get("parameters"), Mapping):
        document = document["parameters"]
    return FuryPolicyParameters(**dict(document))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--scenario-catalog", type=Path)
    parser.add_argument("--armor-hypothesis", type=float)
    parser.add_argument("--level-hypothesis", type=int)
    parser.add_argument(
        "--layout-side",
        choices=("upper", "lower", "evidence_bounded"),
        default="upper",
    )
    parser.add_argument("--policy-parameters", type=Path)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--transitions-output", type=Path, default=DEFAULT_TRANSITIONS)
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--horizon-ms", type=int, default=30_000)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--max-rollouts", type=int, default=32)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    parameters = _load_parameters(args.policy_parameters)
    if args.scenario_catalog is None:
        request = json.loads(args.profile.read_text(encoding="utf-8"))
        if not isinstance(request, Mapping):
            raise FurySyntheticRolloutError("profile root must be an object")
        scenarios = (
            scenario_from_request(
                request,
                scenario_id="clean_dual_single_target_v1",
                horizon_ms=args.horizon_ms,
                provenance={
                    "status": "CALIBRATED_PROFILE",
                    "profile": str(args.profile),
                    "chronicle_derived": False,
                },
            ),
        )
    else:
        if args.armor_hypothesis is None or args.level_hypothesis is None:
            raise ValueError(
                "scenario-catalog requires armor-hypothesis and level-hypothesis"
            )
        catalog = json.loads(args.scenario_catalog.read_text(encoding="utf-8"))
        if not isinstance(catalog, Mapping):
            raise FurySyntheticRolloutError("scenario catalog root must be an object")
        scenarios = scenarios_from_catalog(
            catalog,
            armor_hypothesis=args.armor_hypothesis,
            level_hypothesis=args.level_hypothesis,
            layout_side=args.layout_side,
        )
    seeds = tuple(args.seeds or DEFAULT_SEEDS)
    with SimulatorBridge(args.bridge) as bridge:
        manifest = generate_fury_synthetic_rollouts(
            bridge,
            parameters,
            scenarios,
            seeds=seeds,
            transitions_path=args.transitions_output,
            manifest_path=args.manifest_output,
            max_rollouts=args.max_rollouts,
        )
    print(
        json.dumps(
            {
                "policy_id": manifest["policy"]["policy_id"],
                "counts": manifest["counts"],
                "transitions": manifest["transitions_path"],
                "manifest": manifest["manifest_path"],
                "scale_semantics": manifest["generation_contract"][
                    "scale_semantics"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "DEFAULT_MANIFEST",
    "DEFAULT_TRANSITIONS",
    "FurySyntheticRolloutError",
    "generate_fury_synthetic_rollouts",
    "main",
)


if __name__ == "__main__":
    raise SystemExit(main())
