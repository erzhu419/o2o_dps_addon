"""Native, development-only retargeting of a source-fitted team schedule.

This is not the learned teammate model.  It uses the same focal-excluded,
per-target rate hypotheses as the stratified wave, but chooses the recipient
at each native v14 wake from the *current* alive/attackable registry.  The
historical team event times and magnitudes remain exogenous hypotheses.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_stratified_v1 import build_stratified_wave_case_v1
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import BackgroundDamageEventV1
from .sim_bridge_dynamic_v3 import (
    DynamicTargetSemanticsConfigV3, SimulatorBridgeDynamicV3,
)


SCHEMA = "development_wave_source_team_retarget/v1"
V14_BRIDGE = Path(__file__).resolve().parents[1] / (
    "bin/o2obridge.seedfix-v14.dynamicv4responsive.withdb.goamd64v1.windows-amd64.exe"
)


class V14ProjectedDynamicV3Bridge(SimulatorBridgeDynamicV3):
    """Project v14's extra aggregate counter onto the v3 policy parser."""

    def _request(self, command: str, **kwargs: Any) -> dict[str, Any]:
        response = super()._request(command, **kwargs)
        state = response.get("state")
        if isinstance(state, dict):
            team = state.get("dynamic_team_background")
            if isinstance(team, dict):
                team.pop("background_damage_applications_processed", None)
                response_state = state.get("dynamic_team_response")
                if isinstance(response_state, dict):
                    response_applications = response_state.get("damage_applications_processed", 0)
                    team["damage_applications_total"] -= response_applications
        return response


def build_retarget_wave_case_v1(
    seed: int,
) -> tuple[DevelopmentWaveCaseV1, dict[str, Any], tuple[BackgroundDamageEventV1, ...]]:
    """Keep the two-target source assumptions; move its team schedule to wakes."""

    original, scenario = build_stratified_wave_case_v1(seed, "multi_two")
    old = original.dynamic_load.config
    events = old.background_damage_events
    if len(original.target_contexts) != 2 or not events:
        raise ValueError("the pinned multi_two wave lacks its two-target schedule")
    config = DynamicTargetSemanticsConfigV3(
        target_health=old.target_health,
        idle_advance_horizon_ms=old.idle_advance_horizon_ms,
        background_damage_events=(),
        attackability_events=old.attackability_events,
        effective_armor_events=old.effective_armor_events,
    )
    load = DynamicRolloutLoadV3.bind(original.request, seed, config)
    spec = deepcopy(original.case_spec)
    spec["schema"] = SCHEMA
    spec["team_background"]["model"] = (
        "FOCAL_EXCLUDED_SOURCE_FITTED_RATE_WITH_NATIVE_ALIVE_RETARGET"
    )
    spec["team_background"]["event_time_and_damage_response"] = False
    spec["team_background"]["target_death_retarget_response"] = True
    spec["team_background"]["learned_teammate_model_used"] = False
    spec["team_background"]["source_schedule_content_sha256"] = old.content_sha256
    spec["dynamic_load_contract_sha256"] = load.contract_sha256
    case = DevelopmentWaveCaseV1(spec, original.request, load, original.target_contexts)
    scenario = deepcopy(scenario)
    scenario["scenario_id"] += "__native_team_retarget"
    scenario["dynamic_load_config"] = config.to_wire()
    scenario["scenario_model"]["limitation_codes"].append(
        "TEAM_RETARGETS_ON_DEATH_BUT_TIMING_AND_DAMAGE_ARE_EXOGENOUS"
    )
    return case, scenario, events


class SourceFittedTeamRetargetBridgeV1:
    """Service native v14 wakes without showing future team events to policy."""

    def __init__(self, bridge: Any, events: tuple[Any, ...],
                 schedule_identity: str) -> None:
        self._bridge = bridge
        self._events = tuple(sorted(events, key=lambda row: (row.time_ms, row.schedule_index)))
        if not self._events or len({row.event_id for row in self._events}) != len(self._events):
            raise ValueError("source-fitted team events must be nonempty and unique")
        self._identity = schedule_identity
        self._next = 0
        self._receipts: list[dict[str, Any]] = []
        self._retargeted = 0
        self._loaded = None
        self._dynamic_load = None
        self.wait_contract = "source"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def load_dynamic_v3(self, request: Mapping[str, Any], seed: int,
                        config: DynamicTargetSemanticsConfigV3) -> Any:
        if config.background_damage_events:
            raise ValueError("source-fitted team would be counted twice")
        self._next = 0
        self._receipts = []
        self._retargeted = 0
        result = self._bridge.load_dynamic_v3(request, seed, config)
        self._loaded = result
        self._dynamic_load = DynamicRolloutLoadV3.bind(request, seed, config)
        self._arm_next(result.state)
        return replace(result, state=self._drain(self._bridge.state()))

    def advance(self) -> dict[str, Any]:
        return self._resume_to_policy(self._bridge.advance())

    def wait(self, wait_ms: int) -> dict[str, Any]:
        if self.wait_contract == "source":
            # Cat's ordered sink treats the accepted wait command as the
            # decision consumption and performs its own later advance.
            return self._drain(self._bridge.wait(wait_ms))
        start = self._bridge.state()
        deadline = start["time_ms"] + wait_ms
        live = self._resume_to_policy(self._bridge.wait(wait_ms))
        if live["time_ms"] < deadline and live.get("finished") is not True:
            raise RuntimeError("native wait ended before the requested duration")
        return live

    def _resume_to_policy(self, state: Mapping[str, Any]) -> dict[str, Any]:
        live = self._drain(state)
        while live.get("finished") is not True and live.get("needs_input") is not True:
            # A responsive wake interrupts the simulator's outstanding wait
            # or event traversal.  Resume that traversal, never expose the
            # halfway state as a candidate decision.
            live = self._drain(self._bridge.advance())
        return live

    def _arm_next(self, state: Mapping[str, Any]) -> None:
        if self._next >= len(self._events) or state.get("finished") is True:
            return
        event = self._events[self._next]
        if event.time_ms < state["time_ms"]:
            raise RuntimeError("team wake passed before it was armed")
        self._bridge._request("arm_dynamic_team_wake", responsive={
            "schema": "o2o_dynamic_team_wake/v1",
            "model_content_sha256": self._identity,
            "wake_id": f"team-{self._next}",
            "time_ms": event.time_ms,
        })

    @staticmethod
    def _recipient(state: Mapping[str, Any], historical_index: int) -> int:
        rows = state["dynamic_target_semantics"]["targets"]
        eligible = sorted(
            int(row["target_index"]) for row in rows
            if row["dead"] is False and row["attackable"] is True
        )
        if historical_index in eligible:
            return historical_index
        if not eligible:
            raise RuntimeError("team wake has no alive attackable target")
        return next((index for index in eligible if index > historical_index), eligible[0])

    def _drain(self, state: Mapping[str, Any]) -> dict[str, Any]:
        live = dict(state)
        while live.get("wake_ready") is not None:
            if self._next >= len(self._events):
                raise RuntimeError("unexpected native team wake")
            event = self._events[self._next]
            if live["time_ms"] != event.time_ms:
                raise RuntimeError("native team wake was not consumed at its deadline")
            target_index = self._recipient(live, event.target_index)
            response = self._bridge._request("emit_dynamic_team_event", responsive={
                "schema": "o2o_dynamic_team_event/v1",
                "model_content_sha256": self._identity,
                "wake_id": f"team-{self._next}",
                "event_id": getattr(event, "wire_event_id", f"source-team-{self._next}"),
                "actor_guid": getattr(event, "actor_guid", "SOURCE_FITTED_TEAM_AGGREGATE"),
                "event_type": "DMG",
                "target_index": target_index,
                "requested_damage": event.damage,
            })
            receipt = response["responsive_team_event"]
            if receipt["target_index"] != target_index or receipt["status"] != "APPLIED":
                raise RuntimeError(f"native team event not applied: {receipt['status']}")
            if target_index != event.target_index:
                self._retargeted += 1
            self._receipts.append({
                "schedule_index": event.schedule_index,
                "historical_target_index": event.target_index,
                "actual_target_index": target_index,
                "time_ms": receipt["time_ms"],
                "applied_damage": receipt["applied_damage"],
                "killed": receipt["killed"],
                "actor_guid": receipt["actor_guid"],
                "actor_kind": getattr(event, "actor_kind", "AGGREGATE"),
                "spell_id": getattr(event, "spell_id", None),
                "source_event_index": getattr(event, "source_event_index", None),
            })
            self._next += 1
            live = self._bridge.state()
            self._arm_next(live)
            live = self._bridge.state()
        return dict(live)

    def team_response_evidence(self) -> dict[str, Any]:
        from .fury_full_policy_rollout_v5 import _collect_runtime_receipts_v5

        final_state = self._bridge.state()
        closure = self.close_responsive_runtime_receipts_v1(
            _collect_runtime_receipts_v5(
                self, self._dynamic_load, self._loaded, final_state,
            ),
            final_state=final_state,
        )
        return {
            "schema": SCHEMA + "/native_receipt_summary",
            "source_schedule_event_count": len(self._events),
            "events_emitted": len(self._receipts),
            "events_retargeted_after_endogenous_death": self._retargeted,
            "applied_damage": sum(row["applied_damage"] for row in self._receipts),
            "damage_by_actual_target": dict(sorted({
                index: sum(row["applied_damage"] for row in self._receipts
                           if row["actual_target_index"] == index)
                for index in {row["actual_target_index"] for row in self._receipts}
            }.items())),
            "source_actor_count": len({row["actor_guid"] for row in self._receipts}),
            "source_actor_kind_counts": dict(sorted({
                kind: len({
                    row["actor_guid"] for row in self._receipts if row["actor_kind"] == kind
                })
                for kind in {row["actor_kind"] for row in self._receipts}
            }.items())),
            "retargeted_source_events": [
                {
                    "source_event_index": row["source_event_index"],
                    "actor_guid": row["actor_guid"],
                    "actor_kind": row["actor_kind"],
                    "spell_id": row["spell_id"],
                    "time_ms": row["time_ms"],
                    "historical_target_index": row["historical_target_index"],
                    "actual_target_index": row["actual_target_index"],
                }
                for row in self._receipts
                if row["actual_target_index"] != row["historical_target_index"]
            ],
            "retargeted_by_actor": dict(sorted({
                actor: sum(
                    row["actual_target_index"] != row["historical_target_index"]
                    for row in self._receipts if row["actor_guid"] == actor
                )
                for actor in {row["actor_guid"] for row in self._receipts}
                if any(
                    row["actual_target_index"] != row["historical_target_index"]
                    for row in self._receipts if row["actor_guid"] == actor
                )
            }.items())),
            "event_times_and_damage_are_exogenous": True,
            "candidate_actions_change_target_death_and_subsequent_retarget": True,
            "learned_teammate_model_used": False,
            "native_runtime_receipt_status": closure["status"],
            "native_runtime_receipt_checks": closure["cursor_and_lifecycle_checks"],
        }

    def close_responsive_runtime_receipts_v1(
        self, closure: Mapping[str, Any], *, final_state: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Close v14's extra HP-ledger stream alongside Cat v6's v3 streams."""

        result = deepcopy(dict(closure))
        if not isinstance(final_state, Mapping) or not result.get("cursor_and_lifecycle_checks"):
            return result
        response = self._bridge._request(
            "dynamic_team_response_receipts", cursor=0
        )["dynamic_team_response_receipts"]
        state = final_state.get("dynamic_team_response")
        life = result.get("terminal_lifecycle")
        background = result.get("background_damage")
        candidate = result.get("candidate_damage")
        if not all(isinstance(row, Mapping) for row in (response, state, life, background, candidate)):
            return result
        receipts = response.get("receipts")
        if not isinstance(receipts, list):
            return result
        generation = result["environment_generation"]
        stream_bound = (
            response.get("schema") == "o2o_dynamic_team_response_receipts/v1"
            and state.get("schema") == response.get("schema")
            and response.get("model_content_sha256") == self._identity
            and state.get("model_content_sha256") == self._identity
            and response.get("environment_generation") == generation
            and state.get("environment_generation") == generation
            and response.get("cursor") == 0
            and response.get("next_cursor") == len(receipts)
            and state.get("receipts_processed") == len(receipts)
            and state.get("damage_applications_processed") == len(receipts)
            and state.get("stream_closed") is True
            and state.get("pending_wake_id") is None
            and len(receipts) == len(self._receipts) == self._next
            and len(receipts) <= len(self._events)
            and (len(receipts) == len(self._events) or final_state.get("finished") is True)
        )
        schedule_bound = stream_bound and all(
            receipt.get("schema") == "o2o_dynamic_team_response_receipt/v1"
            and receipt.get("model_content_sha256") == self._identity
            and receipt.get("wake_id") == f"team-{index}"
            and receipt.get("event_id") == getattr(event, "wire_event_id", f"source-team-{index}")
            and receipt.get("actor_guid") == getattr(event, "actor_guid", "SOURCE_FITTED_TEAM_AGGREGATE")
            and receipt.get("event_type") == "DMG"
            and receipt.get("status") == "APPLIED"
            and receipt.get("time_ms") == event.time_ms == local["time_ms"]
            and receipt.get("target_index") == local["actual_target_index"]
            and math.isclose(receipt.get("requested_damage", -1), event.damage, rel_tol=0, abs_tol=1e-9)
            and math.isclose(receipt.get("applied_damage", -1), local["applied_damage"], rel_tol=0, abs_tol=1e-9)
            and receipt.get("killed") is local["killed"]
            and isinstance(receipt.get("damage_ordinal"), int)
            and receipt["damage_ordinal"] > 0
            for index, (receipt, event, local) in enumerate(zip(receipts, self._events, self._receipts))
        )
        fixed = background["receipts"]
        own = candidate["receipts"]
        ordinals = sorted(
            [row["damage_ordinal"] for row in fixed if row["damage_ordinal"] > 0]
            + [row["damage_ordinal"] for row in own]
            + [row["damage_ordinal"] for row in receipts if row["damage_ordinal"] > 0]
        )
        global_total = life["damage_applications_total"] + len(receipts)
        ledger_closed = schedule_bound and ordinals == list(range(1, global_total + 1))
        damage_closed = schedule_bound and (
            math.isclose(
                sum(row["applied_damage"] for row in own),
                life["simulated_damage_applied"], rel_tol=1e-14, abs_tol=1e-9,
            )
            and math.isclose(
                sum(row["applied_damage"] for row in fixed)
                + sum(row["applied_damage"] for row in receipts),
                life["background_damage_applied"], rel_tol=1e-14, abs_tol=1e-9,
            )
            and life["background_events_canceled"]
            == sum(row["status"] != "APPLIED" for row in fixed)
            and life["candidate_events_canceled"]
            == sum(row["status"].startswith("CANCELED_") for row in own)
        )
        checks = result["cursor_and_lifecycle_checks"]
        checks["responsive_team_stream_and_generation_bound"] = stream_bound
        checks["responsive_team_source_schedule_bound"] = schedule_bound
        checks["global_damage_ordinals_contiguous"] = ledger_closed
        checks["receipt_damage_matches_terminal_lifecycle"] = damage_closed
        result["responsive_team_receipt_closure"] = {
            "schema": SCHEMA + "/runtime_receipt_closure",
            "event_count": len(receipts),
            "responsive_damage_applied": sum(row["applied_damage"] for row in receipts),
            "global_damage_application_count": global_total,
            "native_receipt_batch": response,
        }
        result["status"] = "COMPLETE_BOUND" if all(checks.values()) else "INCOMPLETE"
        return result


def run_retarget_wave_panel_v1(seed: int, *, bridge_path: Path = V14_BRIDGE) -> dict[str, Any]:
    """Execute native Cat and 13D candidate in one paired two-target seed."""

    from .development_two_wave_build_panel_v1 import _two_lane_registry
    from .development_wave_panel_v1 import run_development_wave_panel_v1
    from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
    from .fury_cat_gap_three_baseline_registry_v1 import CatGapThreeBaselineRegistryV1

    def registry_factory(bridge: SourceFittedTeamRetargetBridgeV1,
                         candidate: Any, path: Path) -> CatGapThreeBaselineRegistryV1:
        registry = _two_lane_registry(bridge, candidate, path)
        executors = {}
        for policy_id, execute in registry.executors.items():
            contract = "source" if policy_id == CAT_POLICY_ID else "duration"

            def run(*, group: Any, scenario: Any, policy: Any,
                    _execute: Any = execute, _contract: str = contract) -> Any:
                bridge.wait_contract = _contract
                return _execute(group=group, scenario=scenario, policy=policy)

            executors[policy_id] = run
        return CatGapThreeBaselineRegistryV1(
            executors=executors,
            artifact_validators=registry.artifact_validators,
            contract=registry.contract,
        )

    case, scenario, events = build_retarget_wave_case_v1(seed)
    identity = case.case_spec["team_background"]["source_schedule_content_sha256"]
    panel = run_development_wave_panel_v1(
        master_seed=seed,
        bridge_path=bridge_path,
        case_override=case,
        scenario_override=scenario,
        candidate_kind="anchor_13d",
        baseline_ids=(CAT_POLICY_ID,),
        registry_factory=registry_factory,
        bridge_factory=lambda bridge: SourceFittedTeamRetargetBridgeV1(bridge, events, identity),
        native_bridge_type=V14ProjectedDynamicV3Bridge,
    )
    panel["team_retarget_comparison_eligible"] = all(
        row["status"] == "COMPLETED" for row in panel["rows"]
    )
    panel["team_retarget_comparison_gate"] = (
        "MATCHED_NATIVE_DEVELOPMENT_ONLY" if panel["team_retarget_comparison_eligible"]
        else "RESPONSIVE_TEAM_RECEIPT_OR_LANE_INCOMPLETE"
    )
    return panel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--bridge", type=Path, default=V14_BRIDGE)
    args = parser.parse_args()
    panel = run_retarget_wave_panel_v1(args.seed, bridge_path=args.bridge)
    print(json.dumps({
        "schema": SCHEMA + "/smoke_summary",
        "source_wave_ref": panel["case"]["source_wave_ref"],
        "seed": args.seed,
        "status": panel["status"],
        "comparison_eligible": panel["team_retarget_comparison_eligible"],
        "comparison_gate": panel["team_retarget_comparison_gate"],
        "rows": [{
            "policy_id": row["policy_id"], "status": row["status"],
            "own_effective_damage": row.get("own_effective_damage"),
            "reported_damage_nonvoting": row.get("own_reported_damage"),
            "ttk_ms": row.get("ttk_ms"),
            "team_events_retargeted": row.get("team_response_evidence", {}).get(
                "events_retargeted_after_endogenous_death"
            ),
        } for row in panel["rows"]],
    }, ensure_ascii=False, sort_keys=True))


__all__ = [
    "SourceFittedTeamRetargetBridgeV1",
    "build_retarget_wave_case_v1",
    "run_retarget_wave_panel_v1",
]


if __name__ == "__main__":
    main()
