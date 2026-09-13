"""Cat profile-1 full-policy diagnostic over native dynamic-v3 semantics.

This additive layer keeps the frozen rollout and Cat-v5 ordered sink modules
unchanged.  It combines the Cat source-derived state/proposal/sink hooks with
the native ``load_dynamic_v3`` executor namespace, so armor, attackability,
team-background damage, candidate damage, and central idle advancement remain
owned by one simulator load.  The artifact is suitable for runner-v4 offline
diagnostics; it is not WoW client evidence and is never comparison-ready.
"""

from __future__ import annotations

import copy
import json
import types
from typing import Any, Mapping

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import cat_fury_ordered_sink_executor_v6 as _cat_executor_v6
from . import fury_full_policy_rollout_v2 as _v2
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .cat_fury_full_policy_readiness_v4 import (
    MINIMUM_FUTURE_GAME_COLLECTION,
    POLICY_ID,
    CatFuryFullPolicyAdapterV4,
)
from .cat_fury_ordered_sink_executor_v5 import CatSimulatorControlFacadeV5
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    validate_dynamic_load_request_v3,
)
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    validate_v2_execution_base_identity_v3,
)
from .sim_bridge_dynamic_v3 import DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V6 = "cat_fury_full_policy_simulator_rollout/v6"
ROLLOUT_CONTENT_SCHEMA_V6 = "cat_fury_full_policy_rollout_content/v6"
RECEIPT_BUNDLE_SCHEMA_V6 = "cat_fury_source_simulator_receipt_bundle/v6"
IMPLEMENTATION_REVISION = "v6.1_cat_ordered_sink_source_reentry_dynamic_v3"


class CatFuryFullPolicyRolloutV6Error(RuntimeError):
    """The Cat dynamic-v3 rollout or its producer receipt is malformed."""


def _clone_core_v6(
    controls: CatSimulatorControlFacadeV5,
    inputs: _cat_v5.CatFurySimulatorInputsV5,
) -> Any:
    """Bind Cat's exact ordered policy hooks into the dynamic-v3 core."""

    namespace = dict(_dynamic_v5._RUN_V5_CORE.__globals__)
    namespace.update(
        {
            "_supported_adapter": _cat_v5._supported_adapter_v5,
            "_combat_state": _cat_v5._cat_state_mapper(controls, inputs),
            "_proposal": _cat_v5._proposal_v5,
            "execute_ordered_sinks_v2": (
                _cat_executor_v6.execute_cat_fury_ordered_sinks_v6
            ),
            "_audit_ordered_execution": (
                _cat_executor_v6._audit_ordered_execution_v6
            ),
            "_annotate_attempt_ids": _cat_v5._annotate_attempt_ids_v5,
            "_accepted_gcd_actions_from_events": (
                _cat_v5._accepted_gcd_actions_v5
            ),
            "_RESULT_BEARING_GCD_ACTIONS": (
                _cat_v5._RESULT_BEARING_GCD_ACTIONS_V5
            ),
            "_attach_result_followups": (
                _cat_v5._attach_result_followups_v5
            ),
            "_attach_registered_result_followups": (
                _cat_v5._attach_registered_result_followups_v5
            ),
            "_audit_gcd_result_followups": (
                _cat_v5._audit_result_followups_v5
            ),
            "_annotate_immediate_state_deltas": (
                _cat_v5._annotate_immediate_state_deltas_v5
            ),
            "_observe_server_boundary": (
                _cat_v5._observe_simulator_boundary_v5
            ),
            "_unknown_server_boundary": (
                _cat_v5._unknown_simulator_boundary_v5
            ),
            "_right_censor_terminal_active_hardcast": (
                _cat_v5._right_censor_v5
            ),
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_cat_fury_full_policy_rollout_v6_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


def _append_blocker_once(
    blockers: list[Any],
    code: str,
    message: str,
    *,
    execution_fatal: bool,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    if any(isinstance(row, Mapping) and row.get("code") == code for row in blockers):
        return
    blockers.append(
        _v2._blocker(
            code,
            message,
            execution_fatal=execution_fatal,
            evidence=evidence,
        )
    )


def _cat_v6_receipts(
    result: Mapping[str, Any],
    inputs: _cat_v5.CatFurySimulatorInputsV5,
    controls: CatSimulatorControlFacadeV5,
    dynamic_closure: Mapping[str, Any],
) -> JSONMap:
    receipt = copy.deepcopy(
        _cat_v5._receipt_bundle_v5(result, inputs, controls, dynamic_closure)
    )
    receipt["schema"] = RECEIPT_BUNDLE_SCHEMA_V6
    lifecycle = receipt.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise CatFuryFullPolicyRolloutV6Error("Cat receipt lifecycle is missing")
    prior = lifecycle.pop("dynamic_v2_runtime_receipt_closure", None)
    if prior != dynamic_closure:
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat receipt lost the native dynamic-v3 closure"
        )
    lifecycle["dynamic_v3_runtime_receipt_closure"] = copy.deepcopy(
        dynamic_closure
    )
    reentry_clocks = [
        execution["source_reentry_clock"]
        for step in result.get("steps") or []
        if isinstance(step, Mapping)
        and isinstance(step.get("ordered_execution"), Mapping)
        for execution in (step["ordered_execution"],)
        if isinstance(execution.get("source_reentry_clock"), Mapping)
    ]
    operation = receipt.get("operation")
    if not isinstance(operation, dict):
        raise CatFuryFullPolicyRolloutV6Error("Cat operation receipt is missing")
    operation.update(
        {
            "source_reentry_count": len(reentry_clocks),
            "source_reentry_timing_authority": (
                _cat_executor_v6.SOURCE_REENTRY_TIMING_AUTHORITY_V6
            ),
            "source_reentry_retry_ms": (
                _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
            ),
            "source_reentry_exact_client_cadence": False,
            "source_reentry_policy_action_count": 0,
            "source_reentry_simulator_epoch_consumed_count": len(
                reentry_clocks
            ),
            "source_reentry_actual_next_epoch_bound_count": sum(
                type(row.get("actual_next_epoch_time_ms")) is int
                for row in reentry_clocks
            ),
        }
    )
    receipt["runner_v4_diagnostic_registration_ready"] = True
    return receipt


def _version_isolation_v6() -> JSONMap:
    return {
        "frozen_v2_source_sha256": validate_v2_execution_base_identity_v3(),
        "frozen_v4_overlay_sha256": _dynamic_v5._frozen_v4_source_sha256(),
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
    }


def _bind_source_reentry_next_epochs_v6(result: Mapping[str, Any]) -> None:
    """Bind each scheduled reentry clock to the epoch produced by one advance."""

    steps = result.get("steps")
    if not isinstance(steps, list):
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat v6 retained steps are required for source reentry binding"
        )
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise CatFuryFullPolicyRolloutV6Error(
                f"Cat v6 step {index} is malformed"
            )
        execution = step.get("ordered_execution")
        reentry = (
            execution.get("source_reentry_clock")
            if isinstance(execution, Mapping)
            else None
        )
        if not isinstance(reentry, dict):
            continue
        next_state = step.get("simulator_state_next_epoch")
        next_time = (
            next_state.get("time_ms")
            if isinstance(next_state, Mapping)
            else None
        )
        scheduled_at = reentry.get("scheduled_at_time_ms")
        if (
            type(next_time) is not int
            or type(scheduled_at) is not int
            or next_time < scheduled_at
        ):
            raise CatFuryFullPolicyRolloutV6Error(
                f"Cat v6 step {index} cannot bind its source reentry epoch"
            )
        reentry["actual_next_epoch_time_ms"] = next_time


def run_cat_fury_full_policy_rollout_v6(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: CatFuryFullPolicyAdapterV4,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: _cat_v5.CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute Cat's source-ordered policy against one native v3 load."""

    cat_source_isolation = _cat_v5._verify_frozen_source_identity()
    if type(adapter) is not CatFuryFullPolicyAdapterV4:
        raise TypeError("adapter must be exact CatFuryFullPolicyAdapterV4")
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or _cat_v5.CatFurySimulatorInputsV5()
    if not isinstance(inputs, _cat_v5.CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5 or None")
    if retain_steps is not True:
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat v6 requires retain_steps=True for exact ordered receipts"
        )
    required = (
        "load_dynamic_v3",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "dynamic_idle_advance_receipts",
        "parsed_dynamic_state",
        "set_target",
        "start_attack",
        "stop_cast",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    if missing:
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat v6 bridge capabilities missing: " + ", ".join(missing)
        )

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
    core = _clone_core_v6(controls, inputs)
    result = core(
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
        raise CatFuryFullPolicyRolloutV6Error("private core returned non-object")

    _bind_source_reentry_next_epochs_v6(result)

    _dynamic_v5._rewrite_v5_identity(
        result, dynamic_load, facade.last_load_result
    )
    result["schema"] = ROLLOUT_SCHEMA_V6
    result["implementation_revision"] = IMPLEMENTATION_REVISION
    result["expert_id"] = POLICY_ID
    result["version_isolation"] = _version_isolation_v6()
    result["cat_source_isolation"] = cat_source_isolation
    capabilities = result.get("bridge_capabilities")
    if isinstance(capabilities, dict):
        capabilities.update(
            {
                "cat_source_full_policy_v4": True,
                "cat_ordered_sink_executor_v6": True,
                "cat_set_cvar_sidecar": True,
                "cat_use_inventory_item_sidecar": True,
                "cat_use_container_item_sidecar": True,
            }
        )
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        raise CatFuryFullPolicyRolloutV6Error("bridge command contract is missing")
    command.update(
        {
            "source_raw_order_preserved_within_invocation": True,
            "cat_full_policy_source_adapter": "CatFuryFullPolicyAdapterV4",
            "cat_ordered_sink_executor": "execute_cat_fury_ordered_sinks_v6",
            "source_reentry_clock": (
                _cat_executor_v6.SOURCE_REENTRY_CLOCK_SCHEMA_V6
            ),
            "source_reentry_timing_authority": (
                _cat_executor_v6.SOURCE_REENTRY_TIMING_AUTHORITY_V6
            ),
            "source_reentry_retry_ms": (
                _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
            ),
            "source_reentry_is_policy_wait": False,
            "source_reentry_consumes_simulator_epoch": True,
            "source_reentry_actual_boundary_bound_after_advance": True,
            "native_actions": [
                "set_target",
                "start_attack",
                "stop_cast",
                "act",
                "wait",
                "advance",
            ],
            "sidecar_controls": [
                "cat_set_cvar",
                "cat_use_inventory_item",
                "cat_use_container_item",
            ],
            "sidecar_acceptance_is_native_combat_mechanics": False,
            "simulator_acceptance_is_client_acceptance": False,
            "simulator_result_is_game_server_outcome": False,
        }
    )

    dynamic_closure = _dynamic_v5._collect_runtime_receipts_v5(
        bridge,
        dynamic_load,
        facade.last_load_result,
        result.get("final_state"),
    )
    close_responsive = getattr(bridge, "close_responsive_runtime_receipts_v1", None)
    if callable(close_responsive):
        dynamic_closure = close_responsive(
            dynamic_closure, final_state=result.get("final_state")
        )
    result["dynamic_v3_runtime_receipt_closure"] = dynamic_closure
    result["cat_v6_receipts"] = _cat_v6_receipts(
        result, inputs, controls, dynamic_closure
    )
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise CatFuryFullPolicyRolloutV6Error("rollout blockers are malformed")
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
            "central idle closure does not establish real-client fidelity",
        ),
        (
            "CAT_V6_GAME_CLIENT_LOAD_ATTESTATION_MISSING",
            "this exact Cat source/profile lacks a post-/reload load receipt",
        ),
        (
            "CAT_V6_GAME_CLIENT_ORDERED_TRACE_MISSING",
            "source-to-simulator order is not an observed WoW client sink trace",
        ),
        (
            "CAT_V6_CLIENT_ACCEPTANCE_TRACE_MISSING",
            "simulator acceptance is not WoW client acceptance",
        ),
        (
            "CAT_V6_GAME_SERVER_OUTCOME_TRACE_MISSING",
            "simulator results are not Turtle WoW server outcomes",
        ),
        (
            "CAT_V6_CVAR_CLIENT_MECHANICS_OMITTED",
            "NP CVar sidecars preserve order but do not emulate Nampower",
        ),
        (
            "CAT_V6_ITEM_MECHANICS_INCOMPLETE",
            "unbound Cat bag/trinket sinks have no simulator combat effect",
        ),
    ):
        _append_blocker_once(
            blockers, code, message, execution_fatal=False
        )
    if dynamic_closure.get("status") != "COMPLETE_BOUND":
        _append_blocker_once(
            blockers,
            "CAT_V6_DYNAMIC_V3_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE",
            "dynamic-v3 target, damage, idle, or lifecycle streams did not close",
            execution_fatal=True,
            evidence={"closure_status": dynamic_closure.get("status")},
        )
    operation = result["cat_v6_receipts"]["operation"]
    if operation["source_reentry_count"] > 0:
        _append_blocker_once(
            blockers,
            "CAT_V6_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY",
            "rejected Cat source GCDs use a fixed 100 ms runner reentry proxy, not observed client cadence",
            execution_fatal=False,
            evidence={
                "source_reentry_count": operation["source_reentry_count"],
                "timing_authority": operation[
                    "source_reentry_timing_authority"
                ],
                "retry_ms": operation["source_reentry_retry_ms"],
            },
        )
    result["blocker_summary"] = _v2._blocker_summary(blockers)
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_SIMULATOR_ONLY_NONVOTING"
    receipts = result["cat_v6_receipts"]
    result["source_to_simulator_order_faithful"] = (
        receipts["operation"]["run_order_and_disposition_complete"]
    )
    result["ordered_projection_faithful"] = False
    result["simulator_dps_comparison_eligible"] = False
    result["historical_truth"] = False
    result["voting_eligible"] = False
    result["live_fidelity"] = False
    result["comparison_ready"] = False
    result["formal_runner_registration_authorized"] = False
    result["formal_runner_registry_modified"] = False
    result["runner_v4_diagnostic_registration_ready"] = True
    result["scientific_run_launched"] = False
    result["game_client_load_observed"] = False
    result["game_client_ordered_trace_observed"] = False
    result["client_acceptance_observed"] = False
    result["game_server_outcome_observed"] = False
    result["minimum_future_game_collection"] = [
        dict(row) for row in MINIMUM_FUTURE_GAME_COLLECTION
    ]
    claims = list(result.get("claims_excluded") or [])
    for claim in (
        "SIMULATOR_HYPOTHESIS as historical target truth",
        "simulator acceptance as WoW client acceptance",
        "simulator outcome as Turtle WoW server outcome",
        "sidecar item or CVar acceptance as modeled DPS effect",
        "fixed 100 ms Cat source reentry as exact player input cadence",
        "Cat v6 runner diagnostic as a formal or live-fidelity comparison",
    ):
        if claim not in claims:
            claims.append(claim)
    result["claims_excluded"] = claims
    steps = result.get("steps")
    if isinstance(steps, list):
        for step in steps:
            state = step.get("simulator_state_before") if isinstance(step, dict) else None
            if isinstance(state, Mapping):
                step["dynamic_v3_live_target_state_before"] = copy.deepcopy(
                    state["dynamic_target_semantics"]
                )
                step["dynamic_v3_idle_state_before"] = copy.deepcopy(
                    state["dynamic_idle_advance"]
                )
    result.pop("content_address", None)
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V6,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: item for key, item in result.items() if key != "content_address"}
        ),
    }
    return validate_cat_fury_full_policy_rollout_v6(
        result, dynamic_load=dynamic_load
    )


def _canonical_sha256(value: Any) -> str:
    return _dynamic_v5._canonical_sha256_v5(value)


def _validate_dynamic_v3_projection(
    raw: Mapping[str, Any], dynamic_load: DynamicRolloutLoadV3 | None
) -> None:
    projected = copy.deepcopy(dict(raw))
    projected["schema"] = _dynamic_v5.ROLLOUT_SCHEMA_V5
    projected["implementation_revision"] = _dynamic_v5.IMPLEMENTATION_REVISION
    projected["content_address"] = {
        "schema": _dynamic_v5.ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _dynamic_v5._canonical_sha256_v5(
            {
                key: item
                for key, item in projected.items()
                if key != "content_address"
            }
        ),
    }
    _dynamic_v5.validate_fury_full_policy_rollout_v5(
        projected, dynamic_load=dynamic_load
    )


def validate_cat_fury_full_policy_rollout_v6(
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
        raise CatFuryFullPolicyRolloutV6Error(
            f"Cat v6 rollout is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise CatFuryFullPolicyRolloutV6Error("Cat v6 rollout must be an object")
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V6
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("expert_id") != POLICY_ID
        or raw.get("runner_v4_diagnostic_registration_ready") is not True
    ):
        raise CatFuryFullPolicyRolloutV6Error("Cat v6 identity mismatch")
    for field in (
        "ordered_projection_faithful",
        "simulator_dps_comparison_eligible",
        "historical_truth",
        "voting_eligible",
        "live_fidelity",
        "comparison_ready",
        "formal_runner_registration_authorized",
        "formal_runner_registry_modified",
        "scientific_run_launched",
        "game_client_load_observed",
        "game_client_ordered_trace_observed",
        "client_acceptance_observed",
        "game_server_outcome_observed",
    ):
        if raw.get(field) is not False:
            raise CatFuryFullPolicyRolloutV6Error(f"{field} must remain false")
    if raw.get("version_isolation") != _version_isolation_v6():
        raise CatFuryFullPolicyRolloutV6Error("dynamic-v3 isolation mismatch")
    if raw.get("cat_source_isolation") != _cat_v5._verify_frozen_source_identity():
        raise CatFuryFullPolicyRolloutV6Error("Cat source isolation mismatch")

    command = raw.get("bridge_command_contract")
    if not isinstance(command, Mapping) or (
        command.get("initial_load_command") != "load_dynamic_v3"
        or command.get("dynamic_config_schema")
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        or command.get("live_target_state_read_at_every_decision") is not True
        or command.get("live_idle_state_read_at_every_decision") is not True
        or command.get("central_simulator_idle_advance") is not True
        or command.get("source_raw_order_preserved_within_invocation") is not True
        or command.get("cat_full_policy_source_adapter")
        != "CatFuryFullPolicyAdapterV4"
        or command.get("cat_ordered_sink_executor")
        != "execute_cat_fury_ordered_sinks_v6"
        or command.get("source_reentry_clock")
        != _cat_executor_v6.SOURCE_REENTRY_CLOCK_SCHEMA_V6
        or command.get("source_reentry_timing_authority")
        != _cat_executor_v6.SOURCE_REENTRY_TIMING_AUTHORITY_V6
        or command.get("source_reentry_retry_ms")
        != _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
        or command.get("source_reentry_is_policy_wait") is not False
        or command.get("source_reentry_consumes_simulator_epoch") is not True
        or command.get("source_reentry_actual_boundary_bound_after_advance")
        is not True
        or command.get("sidecar_acceptance_is_native_combat_mechanics") is not False
    ):
        raise CatFuryFullPolicyRolloutV6Error("Cat bridge command boundary mismatch")

    receipts = raw.get("cat_v6_receipts")
    if not isinstance(receipts, Mapping) or (
        receipts.get("schema") != RECEIPT_BUNDLE_SCHEMA_V6
        or receipts.get("runner_v4_diagnostic_registration_ready") is not True
        or receipts.get("comparison_ready") is not False
    ):
        raise CatFuryFullPolicyRolloutV6Error("Cat v6 receipt identity mismatch")
    operation = receipts.get("operation")
    if not isinstance(operation, Mapping) or (
        operation.get("run_order_and_disposition_complete")
        is not raw.get("source_to_simulator_order_faithful")
        or operation.get("source_reentry_timing_authority")
        != _cat_executor_v6.SOURCE_REENTRY_TIMING_AUTHORITY_V6
        or operation.get("source_reentry_retry_ms")
        != _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
        or operation.get("source_reentry_exact_client_cadence") is not False
        or operation.get("source_reentry_policy_action_count") != 0
        or operation.get("source_reentry_simulator_epoch_consumed_count")
        != operation.get("source_reentry_count")
        or operation.get("source_reentry_actual_next_epoch_bound_count")
        != operation.get("source_reentry_count")
    ):
        raise CatFuryFullPolicyRolloutV6Error("Cat ordered receipt mismatch")
    _cat_v5.validate_operation_mapping_receipt_v5(
        operation.get("source_oracle_mapping_receipt")
    )
    lifecycle = receipts.get("lifecycle")
    closure = raw.get("dynamic_v3_runtime_receipt_closure")
    if not isinstance(lifecycle, Mapping) or (
        lifecycle.get("dynamic_v3_runtime_receipt_closure") != closure
        or lifecycle.get("all_cursor_and_lifecycle_checks_complete")
        is not (isinstance(closure, Mapping) and closure.get("status") == "COMPLETE_BOUND")
    ):
        raise CatFuryFullPolicyRolloutV6Error("Cat dynamic-v3 lifecycle mismatch")

    steps = raw.get("steps")
    observed_reentry_count = 0
    if raw.get("steps_retained") is not True:
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat v6 requires retained steps"
        )
    if raw.get("steps_retained") is True:
        if not isinstance(steps, list) or not steps:
            raise CatFuryFullPolicyRolloutV6Error("retained Cat steps are missing")
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                raise CatFuryFullPolicyRolloutV6Error("Cat step is malformed")
            proposal = step.get("proposal")
            execution = step.get("ordered_execution")
            if not isinstance(proposal, Mapping) or not isinstance(execution, Mapping):
                raise CatFuryFullPolicyRolloutV6Error(
                    "Cat proposal or ordered execution is missing"
                )
            if (
                execution.get("schema")
                != _cat_executor_v6.EXECUTION_SCHEMA_V6
                or execution.get("implementation_revision")
                != _cat_executor_v6.IMPLEMENTATION_REVISION
                or execution.get("expert_id") != POLICY_ID
                or execution.get("source_decision") != proposal
                or execution.get("raw_sink_order") != proposal.get("raw_sink_order")
            ):
                raise CatFuryFullPolicyRolloutV6Error(
                    f"Cat step {index} lost its source decision/order binding"
                )
            events = execution.get("sink_events")
            raw_order = proposal.get("raw_sink_order")
            if not isinstance(events, list) or not isinstance(raw_order, list) or (
                [event.get("source_sink") for event in events if isinstance(event, Mapping)]
                != raw_order
            ):
                raise CatFuryFullPolicyRolloutV6Error(
                    f"Cat step {index} sink ledger is not source ordered"
                )
            proposal_gcd = proposal.get("gcd")
            reentry_expected = (
                isinstance(proposal_gcd, Mapping)
                and proposal_gcd.get("action") != "WAIT"
                and execution.get("execution_blocked") is False
                and execution.get("decision_consumed") is False
                and execution.get("source_to_simulator_order_faithful") is True
                and execution.get("nonfaithful_reasons") == []
                and _cat_executor_v6._all_reached_sinks_typed_nonconsuming_with_rejected_gcd_v6(
                    events
                )
            )
            reentry = execution.get("source_reentry_clock")
            if (
                execution.get("decision_consumption_scope")
                != "SOURCE_SINKS_ONLY"
                or execution.get(
                    "simulator_epoch_consumed_by_source_reentry_clock"
                )
                is not reentry_expected
                or reentry_expected != isinstance(reentry, Mapping)
            ):
                raise CatFuryFullPolicyRolloutV6Error(
                    f"Cat step {index} source reentry clock mismatch"
                )
            if isinstance(reentry, Mapping):
                observed_reentry_count += 1
                final_state = execution.get("final_state")
                scheduled_at = reentry.get("scheduled_at_time_ms")
                nominal_wake = (
                    scheduled_at + _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
                    if type(scheduled_at) is int
                    else None
                )
                next_state = step.get("simulator_state_next_epoch")
                actual_next_epoch = (
                    next_state.get("time_ms")
                    if isinstance(next_state, Mapping)
                    else None
                )
                if (
                    reentry.get("schema")
                    != _cat_executor_v6.SOURCE_REENTRY_CLOCK_SCHEMA_V6
                    or reentry.get("trigger")
                    != _cat_executor_v6.SOURCE_REENTRY_TRIGGER_V6
                    or reentry.get("timing_authority")
                    != _cat_executor_v6.SOURCE_REENTRY_TIMING_AUTHORITY_V6
                    or reentry.get("requested_ms")
                    != _cat_executor_v6.SOURCE_REENTRY_RETRY_MS_V6
                    or reentry.get("exact_client_cadence") is not False
                    or reentry.get("policy_action") is not False
                    or reentry.get("source_sink") is not False
                    or reentry.get("source_sink_decision_consumed") is not False
                    or reentry.get("simulator_decision_consumed") is not True
                    or execution.get("wait_event") is not None
                    or not isinstance(final_state, Mapping)
                    or final_state.get("time_ms") != scheduled_at
                    or final_state.get("needs_input") is not False
                    or reentry.get("nominal_wake_time_ms") != nominal_wake
                    or type(actual_next_epoch) is not int
                    or actual_next_epoch < scheduled_at
                    or reentry.get("actual_next_epoch_time_ms")
                    != actual_next_epoch
                ):
                    raise CatFuryFullPolicyRolloutV6Error(
                        f"Cat step {index} source reentry clock invalid"
                    )
            if not isinstance(step.get("dynamic_v3_live_target_state_before"), Mapping) or not isinstance(
                step.get("dynamic_v3_idle_state_before"), Mapping
            ):
                raise CatFuryFullPolicyRolloutV6Error(
                    f"Cat step {index} lacks live dynamic-v3 state"
                )
    if operation.get("source_reentry_count") != observed_reentry_count:
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat source reentry receipt count mismatch"
        )

    required_codes = {
        "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "CENTRAL_IDLE_ADVANCE_MECHANISM_NOT_COMPARISON_ADMITTED",
        "CAT_V6_GAME_CLIENT_LOAD_ATTESTATION_MISSING",
        "CAT_V6_GAME_CLIENT_ORDERED_TRACE_MISSING",
        "CAT_V6_CLIENT_ACCEPTANCE_TRACE_MISSING",
        "CAT_V6_GAME_SERVER_OUTCOME_TRACE_MISSING",
        "CAT_V6_CVAR_CLIENT_MECHANICS_OMITTED",
        "CAT_V6_ITEM_MECHANICS_INCOMPLETE",
    }
    blockers = raw.get("blockers")
    codes = {
        row.get("code") for row in blockers if isinstance(row, Mapping)
    } if isinstance(blockers, list) else set()
    if not required_codes.issubset(codes):
        raise CatFuryFullPolicyRolloutV6Error("mandatory Cat v6 blockers are missing")
    reentry_blocker = "CAT_V6_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY"
    if (observed_reentry_count > 0) != (reentry_blocker in codes):
        raise CatFuryFullPolicyRolloutV6Error(
            "Cat source reentry blocker mismatch"
        )

    _validate_dynamic_v3_projection(raw, dynamic_load)
    content = raw.get("content_address")
    expected = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V6,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if content != expected:
        raise CatFuryFullPolicyRolloutV6Error("Cat v6 content address mismatch")
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if "load_dynamic_v1" in encoded or "load_dynamic_v2" in encoded:
        raise CatFuryFullPolicyRolloutV6Error("older dynamic load leaked into Cat v6")
    return raw


__all__ = (
    "CatFuryFullPolicyRolloutV6Error",
    "IMPLEMENTATION_REVISION",
    "RECEIPT_BUNDLE_SCHEMA_V6",
    "ROLLOUT_SCHEMA_V6",
    "run_cat_fury_full_policy_rollout_v6",
    "validate_cat_fury_full_policy_rollout_v6",
)
