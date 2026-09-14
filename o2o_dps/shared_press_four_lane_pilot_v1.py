"""Compact four-source development smoke on one physical key opportunity grid.

The four native simulator loads use the same request, seed, and press period.
This is a clock/receipt diagnostic, not a trained-policy win or live comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Callable, Mapping

from .cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from .conditional_cat_branch_v1 import FrozenRuleV1
from .contra260817_fury_full_policy_rollout_v4 import Contra260817SimulatorInputsV4
from .development_wave_case_v1 import DevelopmentWaveCaseV1


SCHEMA = "shared_press_four_lane_pilot/v1"
CONTEXT_SCHEMA = "shared_press_observable_context/v1"


@dataclass(frozen=True)
class SharedPressObservableContextV1:
    """One explicit owner for every policy-side input absent from the bridge."""

    cat_inputs: CatFurySimulatorInputsV5 = field(
        default_factory=CatFurySimulatorInputsV5
    )
    contra_new_inputs: Contra260817SimulatorInputsV4 = field(
        default_factory=lambda: Contra260817SimulatorInputsV4(
            initial_autoattack_active=True,
        )
    )

    def __post_init__(self) -> None:
        if not isinstance(self.cat_inputs, CatFurySimulatorInputsV5):
            raise TypeError("cat_inputs must be CatFurySimulatorInputsV5")
        if not isinstance(self.contra_new_inputs, Contra260817SimulatorInputsV4):
            raise TypeError("contra_new_inputs must be Contra260817SimulatorInputsV4")
        shared = (
            ("initial_autoattack_active", self.cat_inputs.initial_autoattack_active,
             self.contra_new_inputs.initial_autoattack_active),
            ("player_health_pct", float(self.cat_inputs.player_health_pct),
             float(self.contra_new_inputs.player_health_pct)),
            ("target_banished_indices", self.cat_inputs.target_banished_indices,
             self.contra_new_inputs.target_banished_indices),
        )
        mismatch = [name for name, left, right in shared if left != right]
        if mismatch:
            raise ValueError(
                "shared policy-side fields differ across lane projections: "
                + ",".join(mismatch)
            )

    def receipt(self, *, runtime_binding_sha256: Any) -> dict[str, Any]:
        from .contra260817_external_press_pilot_v1 import _simulator_input_receipt_v1

        return {
            "schema": CONTEXT_SCHEMA,
            "common": {
                "initial_autoattack_active": self.cat_inputs.initial_autoattack_active,
                "player_health_pct": float(self.cat_inputs.player_health_pct),
                "target_banished_indices": list(self.cat_inputs.target_banished_indices),
            },
            "lane_projections": {
                "cat_and_candidate": self.cat_inputs.receipt(),
                "contra_new": _simulator_input_receipt_v1(self.contra_new_inputs),
                "deployed_contra_raid_b": {
                    "source": "BRIDGE_STATE_PLUS_RUNTIME_BINDING",
                    "runtime_binding_sha256": runtime_binding_sha256,
                },
            },
            "live_equivalence_claimed": False,
        }


def _row(
    lane: str, artifact: Mapping[str, Any], *, seed: int, period_ms: int,
    phase_ms: int = 0,
    expected_context_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    presses = artifact.get("presses") or []
    final = artifact.get("final_state") or {}
    configured_at = artifact.get("press_clock_configured_at_ms")
    first_scheduled = artifact.get("first_scheduled_press_ms")
    expected_first = None
    if type(configured_at) is int and configured_at >= 0:
        expected_first = (
            phase_ms
            if phase_ms >= configured_at
            else phase_ms
            + ((configured_at - phase_ms + period_ms - 1) // period_ms) * period_ms
        )

    def press_valid(index: int, press: Mapping[str, Any]) -> bool:
        closure = press.get("press_closure")
        closed = (
            (closure == "FINISH_PRESS" and press.get("finish_press_ready") is False)
            or (closure == "SKIPPED_MODEL_TERMINAL"
                and index == len(presses) - 1
                and final.get("finished") is True
                and press.get("finish_press_ready") is None)
        )
        invoked = press.get("source_invocation_count") == 1
        explicit_no_target = (
            press.get("source_invocation_count") == 0
            and press.get("policy_disposition") == "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
            and press.get("proposal") is None
            and press.get("ordered_execution") is None
        )
        return (
            press.get("press_index") == index + 1
            and type(first_scheduled) is int
            and press.get("time_ms") == first_scheduled + index * period_ms
            and (invoked or explicit_no_target)
            and closed
        )

    clock_valid = (
        artifact.get("seed") == seed
        and artifact.get("period_ms") == period_ms
        and artifact.get("press_phase_ms") == phase_ms
        and (
            artifact.get("press_clock_configuration_mode")
            == "ATOMIC_DYNAMIC_V3_PRESS_CLOCK"
            or (
                artifact.get("press_clock_configuration_mode")
                == "SEPARATE_AFTER_DYNAMIC_LOAD"
                and configured_at == 0
            )
        )
        and expected_first is not None
        and first_scheduled == expected_first
        and artifact.get("press_count") == len(presses)
        and artifact.get("mode") == "DYNAMIC_V3_WHOLE_WAVE"
        and all(press_valid(index, press) for index, press in enumerate(presses))
    )
    terminal = artifact.get("terminal") or {}
    team = final.get("dynamic_team_background") or {}
    score = team.get("simulated_damage_applied")
    complete = (
        artifact.get("status") == "TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING"
        and terminal.get("required_hostiles_defeated") is True
        and terminal.get("reason") is None
        and clock_valid and len(presses) > 0
        and type(score) in (int, float) and isfinite(score)
    )
    context_matches = True
    if expected_context_receipt is not None:
        projection_names = {
            "cat": "cat_and_candidate",
            "candidate": "cat_and_candidate",
            "contra_new": "contra_new",
            "deployed_contra_raid_b": "deployed_contra_raid_b",
        }
        projections = expected_context_receipt.get("lane_projections")
        projection_name = projection_names.get(lane)
        expected_projection = (
            projections.get(projection_name)
            if isinstance(projections, Mapping) and projection_name is not None
            else None
        )
        context_matches = (
            expected_projection is not None
            and artifact.get("shared_context_receipt") == expected_context_receipt
            and artifact.get("simulator_input_receipt") == expected_projection
        )
    score_eligible = complete and context_matches
    return {
        "lane": lane,
        "status": artifact.get("status"),
        "terminal_kind": terminal.get("kind"),
        "reason": terminal.get("reason"),
        "press_count": len(presses),
        "press_clock_configuration_mode": artifact.get(
            "press_clock_configuration_mode"
        ),
        "clock_receipts_valid": clock_valid,
        "model_wave_complete": bool(complete),
        "shared_context_receipt_matches": context_matches,
        "no_live_target_press_count": sum(
            press.get("policy_disposition") == "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
            for press in presses if isinstance(press, Mapping)
        ),
        "own_effective_damage": float(score) if score_eligible else None,
        "elapsed_ms": final.get("time_ms") if score_eligible else None,
    }


def run_shared_press_four_lane_pilot_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any], *,
    runtime_binding: Mapping[str, Any],
    rule: FrozenRuleV1 | None = None,
    observable_context: SharedPressObservableContextV1 | None = None,
    period_ms: int = 100,
    phase_ms: int = 0,
    max_presses: int = 400,
) -> dict[str, Any]:
    """Run one same-seed model wave; failed lanes keep null scores."""

    from .cat_external_press_pilot_v1 import run_cat_external_press_pilot_v1
    from .conditional_cat_external_press_lane_v1 import run_conditional_cat_external_press_lane_v1
    from .contra260817_external_press_pilot_v1 import run_contra260817_external_press_pilot_v1
    from .deployed_contra_external_press_pilot_v1 import run_deployed_contra_external_press_pilot_v1

    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    seed = case.dynamic_load.seed
    selected = rule or FrozenRuleV1()
    context = observable_context or SharedPressObservableContextV1()
    if not isinstance(context, SharedPressObservableContextV1):
        raise TypeError("observable_context must be SharedPressObservableContextV1")
    context_receipt = context.receipt(
        runtime_binding_sha256=runtime_binding.get("binding_sha256"),
    )
    calls = (
        ("cat", run_cat_external_press_pilot_v1, {
            "policy_kind": "cat", "simulator_inputs": context.cat_inputs,
        }),
        ("contra_new", run_contra260817_external_press_pilot_v1, {
            "simulator_inputs": context.contra_new_inputs,
        }),
        ("deployed_contra_raid_b", run_deployed_contra_external_press_pilot_v1,
         {"runtime_binding": runtime_binding}),
        ("candidate", run_conditional_cat_external_press_lane_v1,
         {"rule": selected, "simulator_inputs": context.cat_inputs}),
    )
    rows = []
    artifacts = {}
    for lane, runner, extra in calls:
        try:
            with bridge_factory() as bridge:
                artifact = runner(
                    bridge, case.request, case.target_contexts,
                    seed=seed, period_ms=period_ms, phase_ms=phase_ms,
                    max_presses=max_presses,
                    dynamic_load=case.dynamic_load,
                    shared_context_receipt=context_receipt,
                    **extra,
                )
        except Exception as error:
            artifact = {
                "status": "UNSUPPORTED_PRESS_LANE_NONVOTING",
                "terminal": {"kind": "UNSUPPORTED", "reason": f"{type(error).__name__}: {error}"},
                "presses": [], "final_state": None,
            }
        artifacts[lane] = artifact
        rows.append(_row(
            lane, artifact, seed=seed, period_ms=period_ms,
            phase_ms=phase_ms,
            expected_context_receipt=context_receipt,
        ))
    complete = all(row["model_wave_complete"] for row in rows)
    context_matches = all(row["shared_context_receipt_matches"] for row in rows)
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "period_ms": period_ms,
        "press_phase_ms": phase_ms,
        "candidate_rule": selected.__dict__.copy(),
        "status": (
            "FOUR_LANE_COMPLETE_NONVOTING"
            if complete and context_matches else "INCOMPLETE_NONVOTING"
        ),
        "four_lane_clock_and_terminal_complete": complete,
        "shared_context_receipt": context_receipt,
        "shared_context_receipts_match": context_matches,
        "rows": rows,
        "artifacts": artifacts,
        "comparison_ready": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


__all__ = (
    "SharedPressObservableContextV1",
    "run_shared_press_four_lane_pilot_v1",
)
