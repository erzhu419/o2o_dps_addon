"""Diagnostic full-policy rollout over central dynamic-v3 idle semantics.

The frozen v2 executor and the published v4 overlay remain byte-identical.
This module clones the v2 function namespace privately, binds the real process
load to ``load_dynamic_v3``, reads live target and idle state at every decision,
and closes all five runtime receipt streams.  The output is permanently
non-comparison, non-voting ``SIMULATOR_HYPOTHESIS`` evidence.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import struct
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v2 as _v2
from . import fury_full_policy_rollout_v4 as _v4
from .fury_dynamic_target_semantics_v5 import (
    DYNAMIC_LOAD_BINDING_SCHEMA_V3,
    DynamicRolloutLoadV3,
    FuryDynamicTargetSemanticsV5Error,
    validate_dynamic_load_request_v3,
)
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
    target_semantics_context_receipt_v3,
    validate_v2_execution_base_identity_v3,
)
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicArmorReceiptBatchV3,
    DynamicAttackabilityReceiptBatchV3,
    DynamicCandidateDamageReceiptBatchV3,
    DynamicDamageReceiptBatchV3,
    DynamicIdleAdvanceReceiptBatchV3,
    DynamicLoadReceiptV3,
    DynamicLoadResultV3,
    DynamicTargetSemanticsStateV3,
    DynamicTeamLifecycleStateV3,
    ParsedDynamicStateV3,
    _validate_dynamic_state_binding_v3,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V5 = "fury_full_policy_simulator_rollout/v5"
IMPLEMENTATION_REVISION = "v5.0_dynamic_v3_central_idle_receipt_closure"
RUNTIME_RECEIPT_SCHEMA_V5 = "fury_dynamic_v3_runtime_receipt_closure/v5"
ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5 = "fury_full_policy_rollout_content/v5"


class FuryFullPolicyRolloutV5Error(FuryDynamicTargetSemanticsV5Error):
    """The isolated v5 rollout or dynamic-v3 closure is malformed."""


class _DynamicV3ExecutorBridgeFacade:
    """Private compatibility seam for the frozen v2 executor bytecode."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self.last_load_result: DynamicLoadResultV3 | None = None

    def load_dynamic_v1(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: Any,
    ) -> DynamicLoadResultV3:
        from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3

        if not isinstance(config, DynamicTargetSemanticsConfigV3):
            raise TypeError("v5 facade accepts only DynamicTargetSemanticsConfigV3")
        result = self._bridge.load_dynamic_v3(request, seed, config)
        if not isinstance(result, DynamicLoadResultV3):
            raise TypeError(
                "bridge.load_dynamic_v3 must return DynamicLoadResultV3"
            )
        self.last_load_result = result
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def _validate_dynamic_load_request_v5(
    dynamic_load: DynamicRolloutLoadV3,
    request: Mapping[str, Any],
    *,
    request_sha256: str,
) -> None:
    validate_dynamic_load_request_v3(dynamic_load, request)
    if dynamic_load.request_sha256 != request_sha256:
        raise FuryFullPolicyRolloutV5Error(
            "dynamic-v3 load request digest differs from rollout request"
        )


def _validate_dynamic_load_result_v5(
    dynamic_load: DynamicRolloutLoadV3, result: DynamicLoadResultV3
) -> None:
    if not isinstance(result, DynamicLoadResultV3):
        raise TypeError("dynamic-v3 load must return DynamicLoadResultV3")
    receipt = result.receipt
    config = dynamic_load.config
    if not isinstance(receipt, DynamicLoadReceiptV3) or (
        receipt.schema,
        receipt.config_digest,
        receipt.target_count,
        receipt.background_event_count,
        receipt.attackability_event_count,
        receipt.effective_armor_event_count,
        receipt.same_timestamp_order,
        receipt.retarget_mode,
        receipt.idle_advance_mode,
        receipt.idle_advance_horizon_ms,
        receipt.idle_advance_receipt_schema,
    ) != (
        DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        len(config.attackability_events),
        len(config.effective_armor_events),
        config.same_timestamp_order,
        config.retarget_mode,
        config.idle_advance_mode,
        config.idle_advance_horizon_ms,
        DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    ):
        raise FuryFullPolicyRolloutV5Error(
            "dynamic-v3 load receipt differs from rollout contract"
        )


def _validate_dynamic_loaded_state_v5(
    state: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
    receipt: DynamicLoadReceiptV3,
) -> None:
    _validate_dynamic_state_binding_v3(
        state,
        generation=receipt.environment_generation,
        config=dynamic_load.config,
    )


def _dynamic_load_binding_receipt_v5(
    dynamic_load: DynamicRolloutLoadV3,
    receipt: DynamicLoadReceiptV3 | None,
) -> JSONMap:
    config = dynamic_load.config
    return {
        "schema": DYNAMIC_LOAD_BINDING_SCHEMA_V3,
        "actual_bridge_command": "load_dynamic_v3",
        "contract_sha256": dynamic_load.contract_sha256,
        "request_sha256": dynamic_load.request_sha256,
        "simulator_seed": dynamic_load.seed,
        "config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        "config_digest": config.content_sha256,
        "target_count": len(config.target_health),
        "background_event_count": len(config.background_damage_events),
        "attackability_event_count": len(config.attackability_events),
        "effective_armor_event_count": len(config.effective_armor_events),
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
        "idle_advance_mode": config.idle_advance_mode,
        "idle_advance_horizon_ms": config.idle_advance_horizon_ms,
        "load_succeeded": receipt is not None,
        "bridge_receipt": None if receipt is None else asdict(receipt),
        "historical_truth": False,
    }


def _state_fingerprint_v5(state: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _v4._state_fingerprint_v4(state),
        _v2._jsonable(state.get("dynamic_idle_advance")),
    )


def _state_transition_blocker_v5(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    command: str,
    decision_index: int | None = None,
) -> JSONMap | None:
    blocker = _v2._state_transition_blocker(
        before,
        after,
        command=command,
        decision_index=decision_index,
    )
    if blocker is not None:
        if (
            blocker.get("code") == "SIMULATOR_COMMAND_MADE_NO_PROGRESS"
            and _state_fingerprint_v5(before) != _state_fingerprint_v5(after)
        ):
            return None
        return blocker
    if (
        command != "ordered_source_sinks"
        and after.get("time_ms") == before.get("time_ms")
        and _state_fingerprint_v5(before) == _state_fingerprint_v5(after)
    ):
        return _v2._blocker(
            "SIMULATOR_COMMAND_MADE_NO_PROGRESS",
            f"{command} returned an unchanged dynamic-v3 state",
            execution_fatal=True,
            decision_index=decision_index,
        )
    return None


def _completion_receipt_v5(
    request: Mapping[str, Any],
    root_state: Mapping[str, Any],
    final_state: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
) -> JSONMap:
    """Accept either target death or the explicit dynamic-v3 wave horizon."""

    if dynamic_load is None:
        return _v2._completion_receipt(
            request, root_state, final_state, dynamic_load=None
        )
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise FuryFullPolicyRolloutV5Error(
            "v5 completion requires a DynamicRolloutLoadV3"
        )
    team = final_state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    all_targets_dead = (
        isinstance(targets, list)
        and len(targets) == len(dynamic_load.config.target_health)
        and all(
            isinstance(target, Mapping) and target.get("dead") is True
            for target in targets
        )
    )
    root_time_ms = int(root_state["time_ms"])
    final_time_ms = int(final_state["time_ms"])
    horizon_ms = dynamic_load.config.idle_advance_horizon_ms
    elapsed_ms = final_time_ms - root_time_ms
    within_horizon = 0 <= root_time_ms <= final_time_ms <= horizon_ms
    horizon_reached = final_time_ms == horizon_ms
    finished = final_state.get("finished") is True
    criterion_met = finished and within_horizon and (
        all_targets_dead or horizon_reached
    )
    return {
        "mode": "DYNAMIC_ALL_TARGETS_DEAD_OR_EXPLICIT_HORIZON",
        "criterion_met": criterion_met,
        "terminal_reason": (
            "ALL_TARGETS_DEAD"
            if criterion_met and all_targets_dead
            else "SCENARIO_HORIZON_REACHED"
            if criterion_met and horizon_reached
            else "INCOMPLETE"
        ),
        "bridge_finished": finished,
        "all_targets_dead": all_targets_dead,
        "horizon_reached": horizon_reached,
        "live_target_count": final_state.get("num_targets"),
        "target_count": len(dynamic_load.config.target_health),
        "root_time_ms": root_time_ms,
        "final_time_ms": final_time_ms,
        "elapsed_ms": elapsed_ms,
        "watchdog_horizon_ms": horizon_ms,
        "within_watchdog_horizon": within_horizon,
    }


def _clone_v2_executor_v5() -> Any:
    namespace = dict(_v2.run_fury_full_policy_rollout_v2.__globals__)
    namespace.update(
        {
            "TargetSemanticsModeV2": TargetSemanticsModeV3,
            "DynamicRolloutLoadV1": DynamicRolloutLoadV3,
            "DynamicLoadReceiptV1": DynamicLoadReceiptV3,
            "DynamicLoadResultV1": DynamicLoadResultV3,
            "_validate_context_map": _v4._validate_context_map_v4,
            "_resolve_target_semantics": _v4._resolve_target_semantics_v4,
            "_context_receipt": target_semantics_context_receipt_v3,
            "_validate_dynamic_load_request_v1": (
                _validate_dynamic_load_request_v5
            ),
            "_validate_dynamic_load_result_v1": (
                _validate_dynamic_load_result_v5
            ),
            "_validate_dynamic_loaded_state_v1": (
                _validate_dynamic_loaded_state_v5
            ),
            "_dynamic_load_binding_receipt_v1": (
                _dynamic_load_binding_receipt_v5
            ),
            "_state_fingerprint": _state_fingerprint_v5,
            "_state_transition_blocker": _state_transition_blocker_v5,
            "_completion_receipt": _completion_receipt_v5,
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v5_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V5_CORE = _clone_v2_executor_v5()


def run_fury_full_policy_rollout_v5(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: Any,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute one diagnostic dynamic-v3 rollout with idle receipt closure."""

    frozen_v2_sha256 = validate_v2_execution_base_identity_v3()
    frozen_v4_sha256 = _frozen_v4_source_sha256()
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    required = (
        "load_dynamic_v3",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "dynamic_idle_advance_receipts",
        "parsed_dynamic_state",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    if missing:
        raise FuryFullPolicyRolloutV5Error(
            "dynamic-v3 bridge capabilities missing: " + ", ".join(missing)
        )
    facade = _DynamicV3ExecutorBridgeFacade(bridge)
    result = _RUN_V5_CORE(
        facade,
        raid_sim_request,
        adapter,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=retain_steps,
    )
    if not isinstance(result, dict):
        raise FuryFullPolicyRolloutV5Error("v5 core returned a non-object artifact")
    _rewrite_v5_identity(result, dynamic_load, facade.last_load_result)
    result["version_isolation"] = {
        "frozen_v2_source_sha256": frozen_v2_sha256,
        "frozen_v4_overlay_sha256": frozen_v4_sha256,
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
    }
    closure = _collect_runtime_receipts_v5(
        bridge,
        dynamic_load,
        facade.last_load_result,
        result.get("final_state"),
    )
    result["dynamic_v3_runtime_receipt_closure"] = closure
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise FuryFullPolicyRolloutV5Error("rollout blockers are malformed")
    if closure["status"] != "COMPLETE_BOUND":
        blockers.append(
            _v2._blocker(
                "DYNAMIC_V3_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE",
                "dynamic-v3 target, damage, idle, or lifecycle streams did not close",
                execution_fatal=True,
                evidence={"closure_status": closure["status"]},
            )
        )
    for code, message in (
        (
            "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
            "live health is generated from a named initial-HP hypothesis",
        ),
        (
            "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
            "dynamic effective armor is an executed simulator hypothesis",
        ),
        (
            "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
            "dynamic attackability is an executed simulator hypothesis",
        ),
        (
            "CENTRAL_IDLE_ADVANCE_MECHANISM_NOT_COMPARISON_ADMITTED",
            "central idle mechanism closure does not admit policy comparison",
        ),
    ):
        if not any(
            isinstance(row, Mapping) and row.get("code") == code
            for row in blockers
        ):
            blockers.append(_v2._blocker(code, message, execution_fatal=False))
    result["blocker_summary"] = _v2._blocker_summary(blockers)
    if result.get("status") == "COMPLETE_FAITHFUL":
        result["status"] = "COMPLETE_NONFAITHFUL"
    result["ordered_projection_faithful"] = False
    result["simulator_dps_comparison_eligible"] = False
    result["historical_truth"] = False
    result["voting_eligible"] = False
    steps = result.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and isinstance(
                step.get("simulator_state_before"), Mapping
            ):
                state = step["simulator_state_before"]
                step["dynamic_v3_live_target_state_before"] = dict(
                    state["dynamic_target_semantics"]
                )
                step["dynamic_v3_idle_state_before"] = dict(
                    state["dynamic_idle_advance"]
                )
    excluded = result.get("claims_excluded")
    if not isinstance(excluded, list):
        excluded = []
        result["claims_excluded"] = excluded
    for claim in (
        "SIMULATOR_HYPOTHESIS as historical target truth",
        "dynamic-v3 mechanism PASS as policy comparison admission",
        "Python-side pausing as attackability emulation",
    ):
        if claim not in excluded:
            excluded.append(claim)
    core = {key: item for key, item in result.items() if key != "content_address"}
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256_v5(core),
    }
    validate_fury_full_policy_rollout_v5(result, dynamic_load=dynamic_load)
    return result


def _rewrite_v5_identity(
    result: JSONMap,
    dynamic_load: DynamicRolloutLoadV3,
    loaded: DynamicLoadResultV3 | None,
) -> None:
    result["schema"] = ROLLOUT_SCHEMA_V5
    result["implementation_revision"] = IMPLEMENTATION_REVISION
    capabilities = result.get("bridge_capabilities")
    if isinstance(capabilities, dict):
        legacy = capabilities.pop("load_dynamic_v1", None)
        capabilities["load_dynamic_v3"] = bool(legacy)
        for name in (
            "dynamic_attackability_receipts",
            "dynamic_armor_receipts",
            "dynamic_damage_receipts",
            "dynamic_candidate_damage_receipts",
            "dynamic_idle_advance_receipts",
            "parsed_dynamic_state",
        ):
            capabilities[name] = True
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        command = {}
        result["bridge_command_contract"] = command
    command.update(
        {
            "initial_load_command": "load_dynamic_v3",
            "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            "live_target_state_read_at_every_decision": True,
            "live_idle_state_read_at_every_decision": True,
            "central_simulator_idle_advance": True,
            "python_policy_pause_attackability_emulation": False,
            "runtime_receipt_cursors_required": True,
            "idle_receipt_cursor_required": True,
        }
    )
    result["dynamic_load_binding"] = _dynamic_load_binding_receipt_v5(
        dynamic_load, None if loaded is None else loaded.receipt
    )


def _collect_runtime_receipts_v5(
    bridge: Any,
    dynamic_load: DynamicRolloutLoadV3,
    loaded: DynamicLoadResultV3 | None,
    final_state: Any,
) -> JSONMap:
    base: JSONMap = {
        "schema": RUNTIME_RECEIPT_SCHEMA_V5,
        "status": "NOT_LOADED",
        "config_digest": dynamic_load.config.content_sha256,
        "environment_generation": (
            None if loaded is None else loaded.receipt.environment_generation
        ),
        "same_timestamp_order": dynamic_load.config.same_timestamp_order,
        "attackability": None,
        "armor": None,
        "background_damage": None,
        "candidate_damage": None,
        "idle_advance": None,
        "terminal_lifecycle": None,
        "terminal_target_semantics": None,
        "terminal_idle_state": None,
        "cursor_and_lifecycle_checks": {},
        "error_type": None,
        "historical_truth": False,
        "comparison_eligible": False,
    }
    if loaded is None or not isinstance(final_state, Mapping):
        return base
    try:
        attack = bridge.dynamic_attackability_receipts(cursor=0)
        armor = bridge.dynamic_armor_receipts(cursor=0)
        background = bridge.dynamic_damage_receipts(cursor=0)
        candidate = bridge.dynamic_candidate_damage_receipts(cursor=0)
        idle = bridge.dynamic_idle_advance_receipts(cursor=0)
        parsed = bridge.parsed_dynamic_state(final_state)
        expected_types = (
            (attack, DynamicAttackabilityReceiptBatchV3),
            (armor, DynamicArmorReceiptBatchV3),
            (background, DynamicDamageReceiptBatchV3),
            (candidate, DynamicCandidateDamageReceiptBatchV3),
            (idle, DynamicIdleAdvanceReceiptBatchV3),
            (parsed, ParsedDynamicStateV3),
        )
        if any(not isinstance(value, kind) for value, kind in expected_types):
            raise TypeError("dynamic-v3 runtime receipt or state has wrong type")
        checks = _runtime_closure_checks_v5(
            dynamic_load,
            attack,
            armor,
            background,
            candidate,
            idle,
            parsed.team,
            parsed.target_semantics,
            parsed.idle_advance,
            final_finished=final_state.get("finished") is True,
        )
        base.update(
            {
                "status": (
                    "COMPLETE_BOUND" if all(checks.values()) else "INCOMPLETE"
                ),
                "attackability": asdict(attack),
                "armor": asdict(armor),
                "background_damage": asdict(background),
                "candidate_damage": asdict(candidate),
                "idle_advance": asdict(idle),
                "terminal_lifecycle": asdict(parsed.team),
                "terminal_target_semantics": asdict(parsed.target_semantics),
                "terminal_idle_state": asdict(parsed.idle_advance),
                "cursor_and_lifecycle_checks": checks,
            }
        )
    except Exception as error:
        base["status"] = "ERROR"
        base["error_type"] = type(error).__name__
    return base


def _runtime_closure_checks_v5(
    dynamic_load: DynamicRolloutLoadV3,
    attack: DynamicAttackabilityReceiptBatchV3,
    armor: DynamicArmorReceiptBatchV3,
    background: DynamicDamageReceiptBatchV3,
    candidate: DynamicCandidateDamageReceiptBatchV3,
    idle: DynamicIdleAdvanceReceiptBatchV3,
    lifecycle: DynamicTeamLifecycleStateV3,
    semantics: DynamicTargetSemanticsStateV3,
    idle_state: Any,
    *,
    final_finished: bool,
) -> dict[str, bool]:
    config = dynamic_load.config
    generation = lifecycle.environment_generation
    all_batches_bound = all(
        batch.schema == DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        and batch.config_digest == config.content_sha256
        and batch.environment_generation == generation
        and batch.cursor == 0
        for batch in (attack, armor, background, candidate)
    ) and (
        idle.schema == DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
        and idle.config_digest == config.content_sha256
        and idle.environment_generation == generation
        and idle.cursor == 0
    )
    transition_cursors = (
        attack.schedule_complete
        and attack.next_cursor == len(config.attackability_events)
        and len(attack.receipts) == attack.next_cursor
        and semantics.attackability_events_processed == attack.next_cursor
        and armor.schedule_complete
        and armor.next_cursor == len(config.effective_armor_events)
        and len(armor.receipts) == armor.next_cursor
        and semantics.effective_armor_events_processed == armor.next_cursor
    )
    lifecycle_cursors = (
        background.schedule_complete
        and background.next_cursor == len(config.background_damage_events)
        and len(background.receipts) == background.next_cursor
        and lifecycle.background_events_processed == background.next_cursor
        and lifecycle.candidate_events_processed == candidate.next_cursor
        and len(candidate.receipts) == candidate.next_cursor
    )
    ordinals = sorted(
        [row.damage_ordinal for row in background.receipts if row.damage_ordinal > 0]
        + [row.damage_ordinal for row in candidate.receipts]
    )
    damage_ordinal_closure = ordinals == list(
        range(1, lifecycle.damage_applications_total + 1)
    )
    # Native per-event and lifecycle accumulators use different float sum
    # orders; a 255-hit five-target wave differs by 1.3e-9 at ~603k damage.
    damage_agreement = (
        math.isclose(
            sum(row.applied_damage for row in candidate.receipts),
            lifecycle.simulated_damage_applied,
            rel_tol=1e-14,
            abs_tol=1e-9,
        )
        and math.isclose(
            sum(row.applied_damage for row in background.receipts),
            lifecycle.background_damage_applied,
            rel_tol=1e-14,
            abs_tol=1e-9,
        )
        and lifecycle.background_events_canceled
        == sum(row.status != "APPLIED" for row in background.receipts)
        and lifecycle.candidate_events_canceled
        == sum(row.status.startswith("CANCELED_") for row in candidate.receipts)
    )
    target_agreement = len(lifecycle.targets) == len(semantics.targets) and all(
        life.target_index == target.target_index
        and life.dead == target.dead
        and struct.pack(">d", life.current_health)
        == struct.pack(">d", target.current_health)
        for life, target in zip(lifecycle.targets, semantics.targets)
    )
    idle_receipts = (
        idle.next_cursor == len(idle.receipts)
        and idle_state.receipts_processed == idle.next_cursor
        and idle_state.total_auto_advanced_ms
        == sum(row.auto_advanced_duration_ms for row in idle.receipts)
        and idle.active == idle_state.active
        and idle.stream_closed == idle_state.stream_closed
        and all(
            row.idle_advance_ordinal == index
            and row.policy_actions_consumed == 0
            and row.policy_target_selections_consumed == 0
            and row.scheduler_random_draws == 0
            for index, row in enumerate(idle.receipts)
        )
    )
    idle_cursor_ranges = all(
        row.attackability_cursor_end <= attack.next_cursor
        and row.armor_cursor_end <= armor.next_cursor
        and row.background_cursor_end <= background.next_cursor
        and row.candidate_cursor_end <= candidate.next_cursor
        for row in idle.receipts
    )
    return {
        "all_batches_content_and_generation_bound": all_batches_bound,
        "transition_schedule_cursors_closed": transition_cursors,
        "damage_lifecycle_cursors_closed": lifecycle_cursors,
        "global_damage_ordinals_contiguous": damage_ordinal_closure,
        "receipt_damage_matches_terminal_lifecycle": damage_agreement,
        "terminal_target_state_blocks_agree": target_agreement,
        "terminal_state_matches_last_transitions": (
            _v4._final_transition_state_agreement(attack, armor, semantics)
        ),
        "same_timestamp_order_bound": (
            lifecycle.same_timestamp_order == config.same_timestamp_order
            and semantics.same_timestamp_order == config.same_timestamp_order
        ),
        "idle_receipt_cursor_and_zero_consumption_closed": idle_receipts,
        "idle_receipt_environment_cursors_bounded": idle_cursor_ranges,
        "terminal_idle_stream_closed": (
            not final_finished
            or idle.stream_closed
            and idle_state.stream_closed
            and not idle.active
            and not idle_state.active
        ),
    }


def validate_fury_full_policy_rollout_v5(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
) -> JSONMap:
    try:
        raw = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryFullPolicyRolloutV5Error(
            f"v5 rollout is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise FuryFullPolicyRolloutV5Error("v5 rollout must be an object")
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V5
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("ordered_projection_faithful") is not False
        or raw.get("simulator_dps_comparison_eligible") is not False
        or raw.get("historical_truth") is not False
        or raw.get("voting_eligible") is not False
    ):
        raise FuryFullPolicyRolloutV5Error(
            "v5 rollout identity or scientific boundary mismatch"
        )
    isolation = raw.get("version_isolation")
    if isolation != {
        "frozen_v2_source_sha256": validate_v2_execution_base_identity_v3(),
        "frozen_v4_overlay_sha256": _frozen_v4_source_sha256(),
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
    }:
        raise FuryFullPolicyRolloutV5Error(
            "v5 execution-base isolation receipt mismatch"
        )
    command = raw.get("bridge_command_contract")
    if not isinstance(command, dict) or (
        command.get("initial_load_command") != "load_dynamic_v3"
        or command.get("dynamic_config_schema")
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        or command.get("live_target_state_read_at_every_decision") is not True
        or command.get("live_idle_state_read_at_every_decision") is not True
        or command.get("central_simulator_idle_advance") is not True
        or command.get("python_policy_pause_attackability_emulation") is not False
        or command.get("runtime_receipt_cursors_required") is not True
        or command.get("idle_receipt_cursor_required") is not True
    ):
        raise FuryFullPolicyRolloutV5Error(
            "v5 bridge command contract mismatch"
        )
    binding = raw.get("dynamic_load_binding")
    closure = raw.get("dynamic_v3_runtime_receipt_closure")
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != DYNAMIC_LOAD_BINDING_SCHEMA_V3
        or binding.get("actual_bridge_command") != "load_dynamic_v3"
        or binding.get("config_schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        or binding.get("historical_truth") is not False
        or not isinstance(closure, dict)
        or closure.get("schema") != RUNTIME_RECEIPT_SCHEMA_V5
        or closure.get("config_digest") != binding.get("config_digest")
        or closure.get("historical_truth") is not False
        or closure.get("comparison_eligible") is not False
    ):
        raise FuryFullPolicyRolloutV5Error(
            "v5 load binding or runtime receipt closure mismatch"
        )
    checks = closure.get("cursor_and_lifecycle_checks")
    if not isinstance(checks, dict) or any(
        not isinstance(item, bool) for item in checks.values()
    ) or (
        closure.get("status") == "COMPLETE_BOUND"
        and (not checks or not all(checks.values()))
    ):
        raise FuryFullPolicyRolloutV5Error(
            "v5 runtime closure status disagrees with typed checks"
        )
    idle_batch = closure.get("idle_advance")
    if idle_batch is not None:
        if (
            not isinstance(idle_batch, dict)
            or idle_batch.get("schema")
            != DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
            or idle_batch.get("config_digest") != binding.get("config_digest")
            or not isinstance(idle_batch.get("receipts"), list)
        ):
            raise FuryFullPolicyRolloutV5Error(
                "v5 idle receipt closure binding is malformed"
            )
        for index, row in enumerate(idle_batch["receipts"]):
            if (
                not isinstance(row, dict)
                or row.get("idle_advance_ordinal") != index
                or row.get("policy_actions_consumed") != 0
                or row.get("policy_target_selections_consumed") != 0
                or row.get("scheduler_random_draws") != 0
            ):
                raise FuryFullPolicyRolloutV5Error(
                    "v5 idle receipt lost zero-consumption/ordinal semantics"
                )
    if dynamic_load is not None:
        if not isinstance(dynamic_load, DynamicRolloutLoadV3):
            raise TypeError("dynamic_load must be DynamicRolloutLoadV3 or None")
        config = dynamic_load.config
        expected = {
            "contract_sha256": dynamic_load.contract_sha256,
            "request_sha256": dynamic_load.request_sha256,
            "simulator_seed": dynamic_load.seed,
            "config_digest": config.content_sha256,
            "target_count": len(config.target_health),
            "background_event_count": len(config.background_damage_events),
            "attackability_event_count": len(config.attackability_events),
            "effective_armor_event_count": len(config.effective_armor_events),
            "same_timestamp_order": config.same_timestamp_order,
            "retarget_mode": config.retarget_mode,
            "idle_advance_mode": config.idle_advance_mode,
            "idle_advance_horizon_ms": config.idle_advance_horizon_ms,
        }
        if any(binding.get(key) != item for key, item in expected.items()):
            raise FuryFullPolicyRolloutV5Error(
                "v5 rollout differs from the supplied dynamic load"
            )
    required_codes = {
        "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "CENTRAL_IDLE_ADVANCE_MECHANISM_NOT_COMPARISON_ADMITTED",
    }
    blockers = raw.get("blockers")
    blocker_codes = {
        item.get("code") for item in blockers if isinstance(item, Mapping)
    } if isinstance(blockers, list) else set()
    if not required_codes.issubset(blocker_codes):
        raise FuryFullPolicyRolloutV5Error(
            "v5 rollout lacks permanent hypothesis blockers"
        )
    if raw.get("steps_retained") is True:
        steps = raw.get("steps")
        if not isinstance(steps, list) or any(
            not isinstance(step, Mapping)
            or not isinstance(
                step.get("dynamic_v3_live_target_state_before"), Mapping
            )
            or not isinstance(step.get("dynamic_v3_idle_state_before"), Mapping)
            for step in steps
        ):
            raise FuryFullPolicyRolloutV5Error(
                "retained v5 decisions lack live target/idle state"
            )
    content = raw.get("content_address")
    expected_content = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256_v5(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if content != expected_content:
        raise FuryFullPolicyRolloutV5Error(
            "v5 rollout content address mismatch"
        )
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if "load_dynamic_v1" in encoded or "load_dynamic_v2" in encoded:
        raise FuryFullPolicyRolloutV5Error(
            "v5 artifact leaked an older dynamic-load command"
        )
    return raw


def _frozen_v4_source_sha256() -> str:
    return hashlib.sha256(Path(_v4.__file__).read_bytes()).hexdigest()


def _canonical_sha256_v5(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = (
    "FuryFullPolicyRolloutV5Error",
    "IMPLEMENTATION_REVISION",
    "ROLLOUT_SCHEMA_V5",
    "RUNTIME_RECEIPT_SCHEMA_V5",
    "run_fury_full_policy_rollout_v5",
    "validate_fury_full_policy_rollout_v5",
)
