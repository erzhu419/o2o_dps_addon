"""Diagnostic Fury full-policy rollout over executable dynamic-v2 semantics.

The published v2 executor remains byte-identical.  This module clones its
function namespace privately, redirects only the versioned dynamic load and
target-state hooks, and wraps the bridge locally so the actual process command
is ``load_dynamic_v2``.  No global monkeypatch is performed.

Every state accepted at load, command, advance, and decision boundaries is
validated against both live dynamic-v2 state blocks.  The resulting artifact
is permanently non-comparison/non-voting because the executed target health,
armor, attackability, and fixed team response remain simulator hypotheses.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import struct
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v2 as _v2
from .fury_dynamic_target_semantics_v4 import (
    DYNAMIC_LOAD_BINDING_SCHEMA_V2,
    DynamicRolloutLoadV2,
    FuryDynamicTargetSemanticsV4Error,
    validate_dynamic_load_request_v2,
)
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
    target_semantics_context_receipt_v3,
    validate_v2_execution_base_identity_v3,
)
from .sim_bridge_dynamic_v2 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
    DynamicAttackabilityReceiptBatchV2,
    DynamicArmorReceiptBatchV2,
    DynamicCandidateDamageReceiptBatchV2,
    DynamicDamageReceiptBatchV2,
    DynamicLoadReceiptV2,
    DynamicLoadResultV2,
    DynamicTargetSemanticsConfigV2,
    DynamicTargetSemanticsStateV2,
    DynamicTeamLifecycleStateV2,
    _validate_dynamic_state_binding_v2,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V4 = "fury_full_policy_simulator_rollout/v4"
IMPLEMENTATION_REVISION = "v4.0_dynamic_v2_live_state_receipt_closure"
RUNTIME_RECEIPT_SCHEMA_V4 = "fury_dynamic_v2_runtime_receipt_closure/v4"
ROLLOUT_CONTENT_ADDRESS_SCHEMA_V4 = "fury_full_policy_rollout_content/v4"


class FuryFullPolicyRolloutV4Error(FuryDynamicTargetSemanticsV4Error):
    """The isolated v4 rollout or dynamic-v2 closure is malformed."""


class _DynamicV2ExecutorBridgeFacade:
    """Local compatibility seam for the immutable v2 function body.

    The method name ``load_dynamic_v1`` exists only inside this private facade
    because that name is embedded in the frozen executor bytecode.  It always
    forwards to the wrapped bridge's public ``load_dynamic_v2`` method and
    rejects every non-v2 config.  The final artifact records only the actual
    process command.
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self.last_load_result: DynamicLoadResultV2 | None = None

    def load_dynamic_v1(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV2,
    ) -> DynamicLoadResultV2:
        if not isinstance(config, DynamicTargetSemanticsConfigV2):
            raise TypeError("v4 facade accepts only DynamicTargetSemanticsConfigV2")
        result = self._bridge.load_dynamic_v2(request, seed, config)
        if not isinstance(result, DynamicLoadResultV2):
            raise TypeError(
                "bridge.load_dynamic_v2 must return DynamicLoadResultV2"
            )
        self.last_load_result = result
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def _validate_context_map_v4(
    contexts: Mapping[int, TargetSemanticsContextV3] | None,
) -> dict[int, TargetSemanticsContextV3]:
    if not isinstance(contexts, Mapping) or not contexts:
        raise TypeError("v4 target_contexts must be a nonempty mapping")
    result: dict[int, TargetSemanticsContextV3] = {}
    for index, context in contexts.items():
        if type(index) is not int or index < 0:
            raise TypeError("target_contexts keys must be nonnegative integers")
        if not isinstance(context, TargetSemanticsContextV3):
            raise TypeError("target_contexts values must be TargetSemanticsContextV3")
        if context.target_index != index:
            raise FuryFullPolicyRolloutV4Error(
                "target_context key differs from context.target_index"
            )
        if context.mode is not TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS:
            raise FuryFullPolicyRolloutV4Error(
                "dynamic-v2 v4 rollout requires SIMULATOR_HYPOTHESIS contexts"
            )
        result[index] = context
    return result


def _resolve_target_semantics_v4(
    state: Mapping[str, Any],
    request: Mapping[str, Any],
    contexts: Mapping[int, TargetSemanticsContextV3],
) -> JSONMap:
    request_targets = _v2._request_targets(request)
    raw_index = state.get("target_index")
    if (
        isinstance(raw_index, bool)
        or not isinstance(raw_index, int)
        or raw_index < 0
        or raw_index >= len(request_targets)
    ):
        raise _v2._TargetResolutionBlocked(
            "TARGET_INDEX_UNKNOWN_OR_INVALID",
            "bridge target_index is invalid for dynamic-v2 request",
        )
    semantics = state.get("dynamic_target_semantics")
    lifecycle = state.get("dynamic_team_background")
    if not isinstance(semantics, Mapping) or not isinstance(lifecycle, Mapping):
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_LIVE_STATE_MISSING",
            "both dynamic-v2 live state blocks are required at every decision",
        )
    semantic_targets = semantics.get("targets")
    lifecycle_targets = lifecycle.get("targets")
    if (
        not isinstance(semantic_targets, list)
        or not isinstance(lifecycle_targets, list)
        or len(semantic_targets) != len(request_targets)
        or len(lifecycle_targets) != len(request_targets)
    ):
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_TARGET_ROWS_INVALID",
            "dynamic-v2 target rows differ from the request",
        )
    active = 0
    for index, (semantic, life) in enumerate(
        zip(semantic_targets, lifecycle_targets)
    ):
        if (
            not isinstance(semantic, Mapping)
            or not isinstance(life, Mapping)
            or semantic.get("target_index") != index
            or life.get("target_index") != index
            or not isinstance(semantic.get("attackable"), bool)
            or not isinstance(semantic.get("dead"), bool)
            or semantic.get("dead") != life.get("dead")
        ):
            raise _v2._TargetResolutionBlocked(
                "DYNAMIC_V2_TARGET_ROWS_INVALID",
                "dynamic-v2 target rows are not aligned typed lifecycle rows",
            )
        if semantic["attackable"] and not semantic["dead"]:
            active += 1
    if state.get("total_target_count") != len(request_targets):
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_TOTAL_TARGET_COUNT_MISMATCH",
            "total_target_count differs from the request",
        )
    if state.get("num_targets") != active:
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_ACTIVE_TARGET_COUNT_MISMATCH",
            "num_targets differs from live attackable target rows",
        )
    selected = semantic_targets[raw_index]
    if selected.get("dead") is True:
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_CURRENT_TARGET_DEAD",
            "the current decision target is dead",
        )
    if selected.get("attackable") is not True:
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_CURRENT_TARGET_UNATTACKABLE",
            "the current decision target is not attackable",
        )
    context = contexts.get(raw_index)
    if context is None:
        raise _v2._TargetResolutionBlocked(
            "TARGET_CONTEXT_FOR_INDEX_MISSING",
            f"no target context exists for index {raw_index}",
        )
    request_name = request_targets[raw_index].get("name")
    if request_name != context.target_name:
        raise _v2._TargetResolutionBlocked(
            "TARGET_CONTEXT_REQUEST_NAME_MISMATCH",
            "target context name differs from the request",
        )
    if state.get("target_health_known") is not True:
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_TARGET_HEALTH_NOT_EXPOSED",
            "dynamic-v2 requires live known target health",
        )
    health_pct = _v2._bounded_percent(
        state.get("target_health_percent"), "bridge target_health_percent"
    )
    max_number = _v2._finite_number(
        state.get("target_health_max"), "bridge target_health_max"
    )
    if max_number <= 0 or not max_number.is_integer():
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_TARGET_MAX_HEALTH_INVALID",
            "target_health_max must be a positive integer",
        )
    max_health = int(max_number)
    if max_health != context.target_max_health:
        raise _v2._TargetResolutionBlocked(
            "SIMULATOR_HYPOTHESIS_MAX_HEALTH_MISMATCH",
            "live target max health differs from its named hypothesis",
        )
    armor = _v2._finite_number(
        selected.get("effective_armor"), "dynamic-v2 effective_armor"
    )
    if armor < 0:
        raise _v2._TargetResolutionBlocked(
            "DYNAMIC_V2_TARGET_ARMOR_INVALID",
            "dynamic-v2 effective armor must be nonnegative",
        )
    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": raw_index,
        "target_health_pct": health_pct,
        "target_max_health": max_health,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "target_distance_yards": _v2._player_distance(request),
        "equipped_item_names": list(context.equipped_item_names),
        "dynamic_attackable": True,
        "dynamic_effective_armor": armor,
        "dynamic_state_time_ms": state.get("time_ms"),
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence,
            "target_max_health": context.target_max_health_evidence,
            "target_classification": context.target_classification_evidence,
            "target_name": context.target_name_evidence,
            "equipped_item_names": context.equipment_evidence,
            "target_position": context.target_position_evidence,
        },
        "exact_by_declared_contract": False,
    }


def _validate_dynamic_load_request_v4(
    dynamic_load: DynamicRolloutLoadV2,
    request: Mapping[str, Any],
    *,
    request_sha256: str,
) -> None:
    validate_dynamic_load_request_v2(dynamic_load, request)
    if dynamic_load.request_sha256 != request_sha256:
        raise FuryFullPolicyRolloutV4Error(
            "dynamic-v2 load request digest differs from rollout request"
        )


def _validate_dynamic_load_result_v4(
    dynamic_load: DynamicRolloutLoadV2, result: DynamicLoadResultV2
) -> None:
    if not isinstance(result, DynamicLoadResultV2):
        raise TypeError("dynamic-v2 load must return DynamicLoadResultV2")
    receipt = result.receipt
    config = dynamic_load.config
    if not isinstance(receipt, DynamicLoadReceiptV2) or (
        receipt.schema,
        receipt.config_digest,
        receipt.target_count,
        receipt.background_event_count,
        receipt.attackability_event_count,
        receipt.effective_armor_event_count,
        receipt.same_timestamp_order,
        receipt.retarget_mode,
    ) != (
        DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        len(config.attackability_events),
        len(config.effective_armor_events),
        config.same_timestamp_order,
        config.retarget_mode,
    ):
        raise FuryFullPolicyRolloutV4Error(
            "dynamic-v2 load receipt differs from rollout contract"
        )


def _validate_dynamic_loaded_state_v4(
    state: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV2,
    receipt: DynamicLoadReceiptV2,
) -> None:
    _validate_dynamic_state_binding_v2(
        state,
        generation=receipt.environment_generation,
        config=dynamic_load.config,
    )


def _dynamic_load_binding_receipt_v4(
    dynamic_load: DynamicRolloutLoadV2,
    receipt: DynamicLoadReceiptV2 | None,
) -> JSONMap:
    bridge_receipt = None
    if receipt is not None:
        bridge_receipt = asdict(receipt)
    config = dynamic_load.config
    return {
        "schema": DYNAMIC_LOAD_BINDING_SCHEMA_V2,
        "actual_bridge_command": "load_dynamic_v2",
        "contract_sha256": dynamic_load.contract_sha256,
        "request_sha256": dynamic_load.request_sha256,
        "simulator_seed": dynamic_load.seed,
        "config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        "config_digest": config.content_sha256,
        "target_count": len(config.target_health),
        "background_event_count": len(config.background_damage_events),
        "attackability_event_count": len(config.attackability_events),
        "effective_armor_event_count": len(config.effective_armor_events),
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
        "load_succeeded": receipt is not None,
        "bridge_receipt": bridge_receipt,
        "historical_truth": False,
    }


def _state_fingerprint_v4(state: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _v2._state_fingerprint(state),
        state.get("target_health"),
        state.get("target_armor"),
        state.get("effective_target_armor"),
        _v2._jsonable(state.get("dynamic_target_semantics")),
    )


def _state_transition_blocker_v4(
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
        # The frozen v2 fingerprint predates dynamic target semantics.  A
        # transition that changes only attackability/armor/lifecycle cursors is
        # real simulator progress and must not be rejected as a no-op.
        if (
            blocker.get("code") == "SIMULATOR_COMMAND_MADE_NO_PROGRESS"
            and _state_fingerprint_v4(before) != _state_fingerprint_v4(after)
        ):
            return None
        return blocker
    if (
        command != "ordered_source_sinks"
        and after.get("time_ms") == before.get("time_ms")
        and _state_fingerprint_v4(before) == _state_fingerprint_v4(after)
    ):
        return _v2._blocker(
            "SIMULATOR_COMMAND_MADE_NO_PROGRESS",
            f"{command} returned an unchanged dynamic-v2 state",
            execution_fatal=True,
            decision_index=decision_index,
        )
    return None


def _clone_v2_executor_v4() -> Any:
    namespace = dict(_v2.run_fury_full_policy_rollout_v2.__globals__)
    namespace.update(
        {
            "TargetSemanticsModeV2": TargetSemanticsModeV3,
            "DynamicRolloutLoadV1": DynamicRolloutLoadV2,
            "DynamicLoadReceiptV1": DynamicLoadReceiptV2,
            "DynamicLoadResultV1": DynamicLoadResultV2,
            "_validate_context_map": _validate_context_map_v4,
            "_resolve_target_semantics": _resolve_target_semantics_v4,
            "_context_receipt": target_semantics_context_receipt_v3,
            "_validate_dynamic_load_request_v1": (
                _validate_dynamic_load_request_v4
            ),
            "_validate_dynamic_load_result_v1": (
                _validate_dynamic_load_result_v4
            ),
            "_validate_dynamic_loaded_state_v1": (
                _validate_dynamic_loaded_state_v4
            ),
            "_dynamic_load_binding_receipt_v1": (
                _dynamic_load_binding_receipt_v4
            ),
            "_state_fingerprint": _state_fingerprint_v4,
            "_state_transition_blocker": _state_transition_blocker_v4,
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v4_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V4_CORE = _clone_v2_executor_v4()


def run_fury_full_policy_rollout_v4(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: Any,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV2,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute one diagnostic dynamic-v2 rollout with receipt closure."""

    frozen_v2_sha256 = validate_v2_execution_base_identity_v3()
    if not isinstance(dynamic_load, DynamicRolloutLoadV2):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV2")
    required = (
        "load_dynamic_v2",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "parsed_dynamic_state",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    if missing:
        raise FuryFullPolicyRolloutV4Error(
            "dynamic-v2 bridge capabilities missing: " + ", ".join(missing)
        )
    facade = _DynamicV2ExecutorBridgeFacade(bridge)
    result = _RUN_V4_CORE(
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
        raise FuryFullPolicyRolloutV4Error("v4 core returned a non-object artifact")
    _rewrite_v4_identity(result, dynamic_load, facade.last_load_result)
    result["version_isolation"] = {
        "frozen_v2_source_sha256": frozen_v2_sha256,
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v2",
    }
    closure = _collect_runtime_receipts_v4(
        bridge,
        dynamic_load,
        facade.last_load_result,
        result.get("final_state"),
    )
    result["dynamic_v2_runtime_receipt_closure"] = closure
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise FuryFullPolicyRolloutV4Error("rollout blockers are malformed")
    if closure["status"] != "COMPLETE_BOUND":
        blockers.append(
            _v2._blocker(
                "DYNAMIC_V2_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE",
                "dynamic-v2 schedules, lifecycle, or cursor receipts did not close",
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
            "DYNAMIC_V2_MECHANISM_NOT_COMPARISON_ADMITTED",
            "mechanism tests and receipt closure do not admit policy comparison",
        ),
    ):
        if not any(
            isinstance(row, Mapping) and row.get("code") == code
            for row in blockers
        ):
            blockers.append(
                _v2._blocker(code, message, execution_fatal=False)
            )
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
                step["dynamic_v2_live_target_state_before"] = dict(
                    step["simulator_state_before"]["dynamic_target_semantics"]
                )
    excluded = result.get("claims_excluded")
    if not isinstance(excluded, list):
        excluded = []
        result["claims_excluded"] = excluded
    for claim in (
        "SIMULATOR_HYPOTHESIS as historical target truth",
        "dynamic-v2 mechanism PASS as policy comparison admission",
        "Python-side pausing as attackability emulation",
    ):
        if claim not in excluded:
            excluded.append(claim)
    core = {key: item for key, item in result.items() if key != "content_address"}
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V4,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256_v4(core),
    }
    validate_fury_full_policy_rollout_v4(result, dynamic_load=dynamic_load)
    return result


def validate_fury_full_policy_rollout_v4(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV2 | None = None,
) -> JSONMap:
    """Validate the v4 identity, scientific boundary, and content address."""

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
        raise FuryFullPolicyRolloutV4Error(
            f"v4 rollout is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise FuryFullPolicyRolloutV4Error("v4 rollout must be an object")
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V4
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("ordered_projection_faithful") is not False
        or raw.get("simulator_dps_comparison_eligible") is not False
        or raw.get("historical_truth") is not False
        or raw.get("voting_eligible") is not False
    ):
        raise FuryFullPolicyRolloutV4Error(
            "v4 rollout identity or scientific boundary mismatch"
        )
    isolation = raw.get("version_isolation")
    if isolation != {
        "frozen_v2_source_sha256": validate_v2_execution_base_identity_v3(),
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v2",
    }:
        raise FuryFullPolicyRolloutV4Error(
            "v4 execution-base isolation receipt mismatch"
        )
    command = raw.get("bridge_command_contract")
    if not isinstance(command, dict) or (
        command.get("initial_load_command") != "load_dynamic_v2"
        or command.get("dynamic_config_schema")
        != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or command.get("live_target_state_read_at_every_decision") is not True
        or command.get("python_policy_pause_attackability_emulation") is not False
        or command.get("runtime_receipt_cursors_required") is not True
    ):
        raise FuryFullPolicyRolloutV4Error(
            "v4 bridge command contract mismatch"
        )
    binding = raw.get("dynamic_load_binding")
    closure = raw.get("dynamic_v2_runtime_receipt_closure")
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != DYNAMIC_LOAD_BINDING_SCHEMA_V2
        or binding.get("actual_bridge_command") != "load_dynamic_v2"
        or binding.get("config_schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        or binding.get("historical_truth") is not False
        or not isinstance(closure, dict)
        or closure.get("schema") != RUNTIME_RECEIPT_SCHEMA_V4
        or closure.get("config_digest") != binding.get("config_digest")
        or closure.get("historical_truth") is not False
        or closure.get("comparison_eligible") is not False
    ):
        raise FuryFullPolicyRolloutV4Error(
            "v4 load binding or runtime receipt closure mismatch"
        )
    if dynamic_load is not None:
        if not isinstance(dynamic_load, DynamicRolloutLoadV2):
            raise TypeError("dynamic_load must be DynamicRolloutLoadV2 or None")
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
        }
        if any(binding.get(key) != item for key, item in expected.items()):
            raise FuryFullPolicyRolloutV4Error(
                "v4 rollout differs from the supplied dynamic load"
            )
    required_codes = {
        "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "DYNAMIC_V2_MECHANISM_NOT_COMPARISON_ADMITTED",
    }
    blockers = raw.get("blockers")
    blocker_codes = {
        item.get("code")
        for item in blockers
        if isinstance(item, Mapping)
    } if isinstance(blockers, list) else set()
    if not required_codes.issubset(blocker_codes):
        raise FuryFullPolicyRolloutV4Error(
            "v4 rollout lacks permanent hypothesis blockers"
        )
    if raw.get("steps_retained") is True:
        steps = raw.get("steps")
        if not isinstance(steps, list) or any(
            not isinstance(step, Mapping)
            or not isinstance(
                step.get("dynamic_v2_live_target_state_before"), Mapping
            )
            for step in steps
        ):
            raise FuryFullPolicyRolloutV4Error(
                "retained v4 decisions lack live dynamic-v2 target state"
            )
    content = raw.get("content_address")
    expected_content = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V4,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256_v4(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if content != expected_content:
        raise FuryFullPolicyRolloutV4Error(
            "v4 rollout content address mismatch"
        )
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if "load_dynamic_v1" in encoded:
        raise FuryFullPolicyRolloutV4Error(
            "v4 artifact leaked the legacy dynamic-load command"
        )
    return raw


def _canonical_sha256_v4(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _rewrite_v4_identity(
    result: JSONMap,
    dynamic_load: DynamicRolloutLoadV2,
    loaded: DynamicLoadResultV2 | None,
) -> None:
    result["schema"] = ROLLOUT_SCHEMA_V4
    result["implementation_revision"] = IMPLEMENTATION_REVISION
    capabilities = result.get("bridge_capabilities")
    if isinstance(capabilities, dict):
        legacy = capabilities.pop("load_dynamic_v1", None)
        capabilities["load_dynamic_v2"] = bool(legacy)
        for name in (
            "dynamic_attackability_receipts",
            "dynamic_armor_receipts",
            "dynamic_damage_receipts",
            "dynamic_candidate_damage_receipts",
            "parsed_dynamic_state",
        ):
            capabilities[name] = True
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        command = {}
        result["bridge_command_contract"] = command
    command.update(
        {
            "initial_load_command": "load_dynamic_v2",
            "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            "live_target_state_read_at_every_decision": True,
            "python_policy_pause_attackability_emulation": False,
            "runtime_receipt_cursors_required": True,
        }
    )
    result["dynamic_load_binding"] = _dynamic_load_binding_receipt_v4(
        dynamic_load, None if loaded is None else loaded.receipt
    )


def _collect_runtime_receipts_v4(
    bridge: Any,
    dynamic_load: DynamicRolloutLoadV2,
    loaded: DynamicLoadResultV2 | None,
    final_state: Any,
) -> JSONMap:
    base: JSONMap = {
        "schema": RUNTIME_RECEIPT_SCHEMA_V4,
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
        "terminal_lifecycle": None,
        "terminal_target_semantics": None,
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
        lifecycle, semantics = bridge.parsed_dynamic_state(final_state)
        if not isinstance(attack, DynamicAttackabilityReceiptBatchV2):
            raise TypeError("attackability receipt batch has wrong type")
        if not isinstance(armor, DynamicArmorReceiptBatchV2):
            raise TypeError("armor receipt batch has wrong type")
        if not isinstance(background, DynamicDamageReceiptBatchV2):
            raise TypeError("background receipt batch has wrong type")
        if not isinstance(candidate, DynamicCandidateDamageReceiptBatchV2):
            raise TypeError("candidate receipt batch has wrong type")
        if not isinstance(lifecycle, DynamicTeamLifecycleStateV2) or not isinstance(
            semantics, DynamicTargetSemanticsStateV2
        ):
            raise TypeError("terminal dynamic-v2 state has wrong type")
        checks = _runtime_closure_checks(
            dynamic_load,
            attack,
            armor,
            background,
            candidate,
            lifecycle,
            semantics,
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
                "terminal_lifecycle": asdict(lifecycle),
                "terminal_target_semantics": asdict(semantics),
                "cursor_and_lifecycle_checks": checks,
            }
        )
    except Exception as error:
        base["status"] = "ERROR"
        base["error_type"] = type(error).__name__
    return base


def _runtime_closure_checks(
    dynamic_load: DynamicRolloutLoadV2,
    attack: DynamicAttackabilityReceiptBatchV2,
    armor: DynamicArmorReceiptBatchV2,
    background: DynamicDamageReceiptBatchV2,
    candidate: DynamicCandidateDamageReceiptBatchV2,
    lifecycle: DynamicTeamLifecycleStateV2,
    semantics: DynamicTargetSemanticsStateV2,
) -> dict[str, bool]:
    config = dynamic_load.config
    generation = lifecycle.environment_generation
    all_batches_bound = all(
        batch.schema == DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2
        and batch.config_digest == config.content_sha256
        and batch.environment_generation == generation
        and batch.cursor == 0
        for batch in (attack, armor, background, candidate)
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
        [row.damage_ordinal for row in background.receipts]
        + [row.damage_ordinal for row in candidate.receipts]
    )
    damage_ordinal_closure = ordinals == list(
        range(1, lifecycle.damage_applications_total + 1)
    )
    receipt_lifecycle_damage_agreement = (
        math.isclose(
            sum(row.applied_damage for row in candidate.receipts),
            lifecycle.simulated_damage_applied,
            rel_tol=0,
            abs_tol=1e-9,
        )
        and math.isclose(
            sum(row.applied_damage for row in background.receipts),
            lifecycle.background_damage_applied,
            rel_tol=0,
            abs_tol=1e-9,
        )
        and lifecycle.background_events_canceled
        == sum(row.status != "APPLIED" for row in background.receipts)
        and lifecycle.candidate_events_canceled
        == sum(row.status.startswith("CANCELED_") for row in candidate.receipts)
    )
    if receipt_lifecycle_damage_agreement:
        for target in lifecycle.targets:
            candidate_damage = sum(
                row.applied_damage
                for row in candidate.receipts
                if (
                    row.retargeted_to
                    if row.retargeted_to is not None
                    else row.target_index
                )
                == target.target_index
            )
            background_damage = sum(
                row.applied_damage
                for row in background.receipts
                if (
                    row.retargeted_to
                    if row.retargeted_to is not None
                    else row.target_index
                )
                == target.target_index
            )
            if not (
                math.isclose(
                    candidate_damage,
                    target.simulated_damage_applied,
                    rel_tol=0,
                    abs_tol=1e-9,
                )
                and math.isclose(
                    background_damage,
                    target.background_damage_applied,
                    rel_tol=0,
                    abs_tol=1e-9,
                )
            ):
                receipt_lifecycle_damage_agreement = False
                break
    target_state_agreement = len(lifecycle.targets) == len(semantics.targets)
    if target_state_agreement:
        for life, target in zip(lifecycle.targets, semantics.targets):
            if (
                life.target_index != target.target_index
                or life.dead != target.dead
                or struct.pack(">d", life.current_health)
                != struct.pack(">d", target.current_health)
            ):
                target_state_agreement = False
                break
    final_transition_agreement = _final_transition_state_agreement(
        attack, armor, semantics
    )
    return {
        "all_batches_content_and_generation_bound": all_batches_bound,
        "transition_schedule_cursors_closed": transition_cursors,
        "damage_lifecycle_cursors_closed": lifecycle_cursors,
        "global_damage_ordinals_contiguous": damage_ordinal_closure,
        "receipt_damage_matches_terminal_lifecycle": (
            receipt_lifecycle_damage_agreement
        ),
        "terminal_target_state_blocks_agree": target_state_agreement,
        "terminal_state_matches_last_transitions": final_transition_agreement,
        "same_timestamp_order_bound": (
            lifecycle.same_timestamp_order == config.same_timestamp_order
            and semantics.same_timestamp_order == config.same_timestamp_order
        ),
    }


def _final_transition_state_agreement(
    attack: DynamicAttackabilityReceiptBatchV2,
    armor: DynamicArmorReceiptBatchV2,
    semantics: DynamicTargetSemanticsStateV2,
) -> bool:
    for target in semantics.targets:
        attack_rows = [
            row
            for row in attack.receipts
            if row.target_index == target.target_index
            and row.status not in {"CANCELED_ENCOUNTER_FINISHED"}
        ]
        if attack_rows:
            expected = attack_rows[-1].resulting_attackable
            if target.dead:
                expected = False
            if target.attackable != expected:
                return False
        armor_rows = [
            row
            for row in armor.receipts
            if row.target_index == target.target_index
            and row.status in {"APPLIED", "NO_CHANGE"}
        ]
        if armor_rows and not math.isclose(
            target.effective_armor,
            armor_rows[-1].resulting_target_armor,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            return False
    return True


__all__ = (
    "FuryFullPolicyRolloutV4Error",
    "IMPLEMENTATION_REVISION",
    "ROLLOUT_SCHEMA_V4",
    "RUNTIME_RECEIPT_SCHEMA_V4",
    "run_fury_full_policy_rollout_v4",
    "validate_fury_full_policy_rollout_v4",
)
