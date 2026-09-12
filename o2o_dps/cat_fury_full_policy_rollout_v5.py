"""Cat profile-1 diagnostic rollout over dynamic-v2 semantics (v5).

The frozen v2/v3/v4 rollout modules are not edited.  A private ``FunctionType``
clone of the v2 loop receives v4 dynamic-target hooks, the v5 Cat state
adapter, and the v5 ordered-sink executor.  This closes an offline engineering
receipt for source order, configured horizon, and dynamic lifecycle while
permanently distinguishing simulator facts from missing WoW client/server
facts.  It does not register Cat in any formal runner and cannot launch or
authorize a baseline comparison.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import types
from typing import Any, Mapping, Sequence

from . import fury_full_policy_rollout_v2 as _v2
from . import fury_full_policy_rollout_v3 as _v3
from . import fury_full_policy_rollout_v4 as _v4
from .cat_fury_full_policy_readiness_v4 import (
    MINIMUM_FUTURE_GAME_COLLECTION,
    POLICY_ID,
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    CatInventoryItemV4,
    validate_source_decision_v4,
)
from .cat_fury_ordered_sink_executor_v5 import (
    EXECUTION_SCHEMA_V5,
    CatSimulatorControlFacadeV5,
    CatSimulatorItemActionBindingV5,
    build_operation_mapping_receipt_v5,
    execute_cat_fury_ordered_sinks_v5,
    validate_operation_mapping_receipt_v5,
)
from .fury_dynamic_target_semantics_v4 import DynamicRolloutLoadV2
from .fury_full_policy_rollout_v3 import TargetSemanticsContextV3
from .sim_bridge import AvailableAction
from .sim_bridge_dynamic_v2 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
    DynamicLoadResultV2,
    DynamicTargetSemanticsConfigV2,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V5 = "cat_fury_full_policy_simulator_rollout/v5"
ROLLOUT_CONTENT_SCHEMA_V5 = "cat_fury_full_policy_rollout_content/v5"
RECEIPT_BUNDLE_SCHEMA_V5 = "cat_fury_source_simulator_receipt_bundle/v5"
IMPLEMENTATION_REVISION = "v5.0_cat_ordered_sink_dynamic_v2_diagnostic"

# These are isolation pins, not a request to mutate historical files when they
# drift.  A drift fails this additive v5 layer closed.
EXPECTED_FROZEN_SOURCE_SHA256 = {
    "fury_full_policy_rollout_v2.py": "57f691f44185b85b58f154ee91ff55f2fd23acd3d4f7753c29e2a03582b504f6",
    "fury_full_policy_rollout_v3.py": "488c8032faa6b9c10a7d348d01b5c3ea52707dce97b4afdc4db1c2e59cae9fde",
    "fury_full_policy_rollout_v4.py": "fce4f74ab61f0b7aa34ef11d6c58dcd1a1494041f3ae5b1f2605de396c1c05fb",
    "cat_fury_full_policy_readiness_v4.py": "7208ab23f2524d9c0a23beeb56b644004c7b70f60c7aee420b67c55915980ddc",
}


class CatFuryFullPolicyRolloutV5Error(RuntimeError):
    """The isolated Cat v5 rollout or receipt contract is malformed."""


@dataclass(frozen=True)
class CatFurySimulatorInputsV5:
    """Explicit simulator-only inputs absent from the dynamic bridge state."""

    target_banished_indices: tuple[int, ...] = ()
    initial_autoattack_active: bool = True
    autoattack_lock: bool = False
    combat_elapsed_offset_s: float = 10.0
    upper_trinket_supported: bool = False
    upper_trinket_cooldown_s: float = 1.0
    lower_trinket_supported: bool = False
    lower_trinket_cooldown_s: float = 1.0
    player_health_pct: float = 100.0
    inventory_items: tuple[CatInventoryItemV4, ...] = ()
    overpower_proc_active: bool = False
    overpower_ready_within_1_5_s: bool = False
    initial_np_queue_cast_time_spells: bool = True
    initial_np_queue_instant_spells: bool = True
    item_action_bindings: tuple[CatSimulatorItemActionBindingV5, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "initial_autoattack_active",
            "autoattack_lock",
            "upper_trinket_supported",
            "lower_trinket_supported",
            "overpower_proc_active",
            "overpower_ready_within_1_5_s",
            "initial_np_queue_cast_time_spells",
            "initial_np_queue_instant_spells",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        for name in (
            "combat_elapsed_offset_s",
            "upper_trinket_cooldown_s",
            "lower_trinket_cooldown_s",
            "player_health_pct",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.player_health_pct > 100:
            raise ValueError("player_health_pct must not exceed 100")
        if tuple(sorted(set(self.target_banished_indices))) != self.target_banished_indices or any(
            type(index) is not int or index < 0 for index in self.target_banished_indices
        ):
            raise ValueError("target_banished_indices must be sorted unique nonnegative integers")
        if any(not isinstance(item, CatInventoryItemV4) for item in self.inventory_items):
            raise TypeError("inventory_items must contain CatInventoryItemV4")
        if any(not isinstance(item, CatSimulatorItemActionBindingV5) for item in self.item_action_bindings):
            raise TypeError("item_action_bindings must contain v5 item bindings")

    def receipt(self) -> JSONMap:
        return {
            "target_banished_indices": list(self.target_banished_indices),
            "initial_autoattack_active": self.initial_autoattack_active,
            "autoattack_lock": self.autoattack_lock,
            "combat_elapsed_offset_s": self.combat_elapsed_offset_s,
            "upper_trinket_supported": self.upper_trinket_supported,
            "upper_trinket_cooldown_s": self.upper_trinket_cooldown_s,
            "lower_trinket_supported": self.lower_trinket_supported,
            "lower_trinket_cooldown_s": self.lower_trinket_cooldown_s,
            "player_health_pct": self.player_health_pct,
            "inventory_items": [asdict(item) for item in self.inventory_items],
            "overpower_proc_active": self.overpower_proc_active,
            "overpower_ready_within_1_5_s": self.overpower_ready_within_1_5_s,
            "initial_cvars": {
                "NP_QueueCastTimeSpells": self.initial_np_queue_cast_time_spells,
                "NP_QueueInstantSpells": self.initial_np_queue_instant_spells,
            },
            "item_action_bindings": [
                {"locator": item.locator, "action": item.action.to_wire()}
                for item in self.item_action_bindings
            ],
            "evidence_kind": "SIMULATOR_INPUT_HYPOTHESIS",
            "game_client_observed": False,
        }


class _DynamicV2CatBridgeFacadeV5:
    """Expose the frozen loop's load name while actually using dynamic-v2."""

    def __init__(self, controls: CatSimulatorControlFacadeV5) -> None:
        self.controls = controls
        self.last_load_result: DynamicLoadResultV2 | None = None

    def load_dynamic_v1(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV2,
    ) -> DynamicLoadResultV2:
        if not isinstance(config, DynamicTargetSemanticsConfigV2):
            raise TypeError("Cat v5 accepts only DynamicTargetSemanticsConfigV2")
        result = self.controls.load_dynamic_v2(request, seed, config)
        if not isinstance(result, DynamicLoadResultV2):
            raise TypeError("bridge.load_dynamic_v2 must return DynamicLoadResultV2")
        self.last_load_result = result
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.controls, name)


def _module_sha256(module: Any) -> str:
    path = Path(module.__file__).resolve(strict=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_frozen_source_identity() -> JSONMap:
    modules = {
        "fury_full_policy_rollout_v2.py": _v2,
        "fury_full_policy_rollout_v3.py": _v3,
        "fury_full_policy_rollout_v4.py": _v4,
    }
    from . import cat_fury_full_policy_readiness_v4 as cat_v4

    modules["cat_fury_full_policy_readiness_v4.py"] = cat_v4
    observed = {name: _module_sha256(module) for name, module in modules.items()}
    if observed != EXPECTED_FROZEN_SOURCE_SHA256:
        raise CatFuryFullPolicyRolloutV5Error(
            "frozen v2/v3/v4 source identity drifted; v5 will not rewrite history"
        )
    return {
        "expected_sha256": dict(EXPECTED_FROZEN_SOURCE_SHA256),
        "observed_sha256": observed,
        "all_match": True,
        "old_source_bytes_modified_by_v5": False,
    }


def _cat_state_mapper(
    controls: CatSimulatorControlFacadeV5,
    inputs: CatFurySimulatorInputsV5,
):
    def mapper(
        state: Mapping[str, Any],
        available: Sequence[AvailableAction],
        request: Mapping[str, Any],
        target: Mapping[str, Any],
        *,
        last_gcd_action: str,
    ) -> CatFuryFullPolicyStateV4:
        combat = _v2._combat_state(
            state,
            available,
            request,
            target,
            last_gcd_action=last_gcd_action,
        )
        used = set(controls.used_locators)
        inventory = tuple(
            replace(
                item,
                cooldown_remaining_s=(
                    max(2.0, item.cooldown_remaining_s)
                    if f"container:{item.bag}:{item.slot}:{item.name}" in used
                    else item.cooldown_remaining_s
                ),
            )
            for item in inputs.inventory_items
        )
        target_index = int(target["target_index"])
        return CatFuryFullPolicyStateV4(
            combat=combat,
            target_banished=target_index in inputs.target_banished_indices,
            autoattack_active=controls.autoattack_active,
            autoattack_lock=inputs.autoattack_lock,
            combat_elapsed_s=(
                inputs.combat_elapsed_offset_s + float(state["time_ms"]) / 1000.0
            ),
            upper_trinket_supported=inputs.upper_trinket_supported,
            upper_trinket_cooldown_s=(
                max(1.0, inputs.upper_trinket_cooldown_s)
                if "inventory:13" in used
                else inputs.upper_trinket_cooldown_s
            ),
            lower_trinket_supported=inputs.lower_trinket_supported,
            lower_trinket_cooldown_s=(
                max(1.0, inputs.lower_trinket_cooldown_s)
                if "inventory:14" in used
                else inputs.lower_trinket_cooldown_s
            ),
            player_health_pct=inputs.player_health_pct,
            inventory_items=inventory,
            overpower_proc_active=inputs.overpower_proc_active,
            overpower_ready_within_1_5_s=inputs.overpower_ready_within_1_5_s,
        )

    return mapper


def _proposal_v5(
    adapter: CatFuryFullPolicyAdapterV4,
    state: CatFuryFullPolicyStateV4,
    target: Mapping[str, Any],
) -> Any:
    del target
    return validate_source_decision_v4(adapter.propose(state))


def _supported_adapter_v5(adapter: Any) -> bool:
    return type(adapter) is CatFuryFullPolicyAdapterV4 and adapter.expert_id == POLICY_ID


def _annotate_attempt_ids_v5(
    execution: JSONMap, decision_index: int
) -> tuple[str, ...]:
    events = execution.get("sink_events")
    if not isinstance(events, list):
        raise CatFuryFullPolicyRolloutV5Error("ordered execution lacks sink_events")
    result: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            raise CatFuryFullPolicyRolloutV5Error("sink event must be an object")
        order = event.get("order")
        if type(order) is not int or order <= 0:
            raise CatFuryFullPolicyRolloutV5Error("sink event order is invalid")
        attempt_id = f"decision-{decision_index}:sink-{order}"
        attempt = event.get("source_attempt")
        if not isinstance(attempt, dict):
            raise CatFuryFullPolicyRolloutV5Error("source attempt is missing")
        if attempt.get("attempt_id") not in {None, attempt_id}:
            raise CatFuryFullPolicyRolloutV5Error("attempt ID differs from executor")
        attempt["attempt_id"] = attempt_id
        source = event.get("source_sink")
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        operation = event.get("operation_contract")
        if (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and isinstance(submission, Mapping)
            and submission.get("status") == "SUBMITTED"
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
            and isinstance(operation, Mapping)
            and isinstance(operation.get("action_ref"), Mapping)
            and submission.get("action") == operation.get("action_ref")
            and operation.get("canonical_action") in _RESULT_BEARING_GCD_ACTIONS_V5
        ):
            result.append(attempt_id)
    return tuple(result)


_RESULT_BEARING_GCD_ACTIONS_V5 = frozenset(
    set(_v2._RESULT_BEARING_GCD_ACTIONS) | {"warrior.overpower"}
)


def _accepted_gcd_actions_v5(execution: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    events = execution.get("sink_events")
    if not isinstance(events, list):
        return result
    for event in events:
        if not isinstance(event, Mapping):
            continue
        source = event.get("source_sink")
        acceptance = event.get("simulator_acceptance")
        operation = event.get("operation_contract")
        if (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
            and isinstance(operation, Mapping)
            and isinstance(operation.get("canonical_action"), str)
        ):
            result.append(str(operation["canonical_action"]))
    return result


def _audit_ordered_execution_v5(
    execution: Mapping[str, Any],
    proposal: Any,
    *,
    decision_index: int,
) -> list[JSONMap]:
    blockers: list[JSONMap] = []

    def add(code: str, message: str, *, fatal: bool, evidence: Mapping[str, Any] | None = None) -> None:
        blockers.append(
            _v2._blocker(
                code,
                message,
                execution_fatal=fatal,
                decision_index=decision_index,
                evidence=evidence,
            )
        )

    if execution.get("schema") != EXECUTION_SCHEMA_V5:
        add("CAT_V5_EXECUTION_SCHEMA_MISMATCH", "ordered executor schema differs", fatal=True)
    fallback = execution.get("fallback")
    if not isinstance(fallback, Mapping) or fallback.get("used") is not False:
        add("CAT_V5_FALLBACK_PRESENT", "source executor used or did not type fallback", fatal=True)
    expected = [sink.to_dict() for sink in proposal.raw_sink_order]
    events = execution.get("sink_events")
    if execution.get("raw_sink_order") != expected or not isinstance(events, list) or len(events) != len(expected):
        add("CAT_V5_RAW_LEDGER_MISMATCH", "raw source ledger/cardinality differs", fatal=True)
        return blockers
    for order, (raw, event) in enumerate(zip(expected, events), start=1):
        if not isinstance(event, Mapping) or event.get("order") != order or event.get("source_sink") != raw:
            add("CAT_V5_SOURCE_ORDER_MISMATCH", f"sink {order} changed source order", fatal=True)
            continue
        attempt = event.get("source_attempt")
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        client = event.get("client_observation")
        server = event.get("server_outcome")
        if not isinstance(attempt, Mapping) or attempt.get("status") != "ATTEMPTED":
            add("CAT_V5_SOURCE_ATTEMPT_MISSING", f"sink {order} lacks source attempt", fatal=True)
        submission_status = submission.get("status") if isinstance(submission, Mapping) else None
        if submission_status not in {"SUBMITTED", "NOT_SUBMITTED_STATE_ALREADY_SATISFIED"}:
            add(
                "CAT_V5_SIMULATOR_SUBMISSION_INCOMPLETE",
                f"sink {order} simulator submission is {submission_status!r}",
                fatal=True,
                evidence={"order": order, "status": submission_status},
            )
        acceptance_status = acceptance.get("status") if isinstance(acceptance, Mapping) else None
        if acceptance_status not in {"ACCEPTED", "REJECTED", "NOOP_STATE_ALREADY_SATISFIED"}:
            add(
                "CAT_V5_SIMULATOR_ACCEPTANCE_UNTYPED",
                f"sink {order} simulator acceptance is {acceptance_status!r}",
                fatal=True,
            )
        elif acceptance_status == "REJECTED":
            add(
                "CAT_V5_SIMULATOR_REJECTED_SOURCE_ATTEMPT",
                f"simulator rejected raw source attempt {order}",
                fatal=False,
            )
        if client != {"status": "NOT_OBSERVED_NO_WOW_CLIENT", "accepted": None, "evidence": None}:
            add("CAT_V5_CLIENT_OBSERVATION_BOUNDARY_VIOLATED", "simulator event claimed client evidence", fatal=True)
        if server != {"status": "NOT_OBSERVED_NO_GAME_SERVER_LOG", "outcome": None, "damage": None, "evidence": None}:
            add("CAT_V5_SERVER_OUTCOME_BOUNDARY_VIOLATED", "simulator event claimed game-server evidence", fatal=True)
        consumption = event.get("decision_consumption")
        if acceptance_status == "ACCEPTED" and isinstance(consumption, Mapping):
            expected_consumption = consumption.get("expected_for_lane")
            observed = consumption.get("consumes_decision")
            if isinstance(expected_consumption, bool) and expected_consumption is not observed:
                add("CAT_V5_DECISION_CONSUMPTION_MISMATCH", f"sink {order} consumption differs", fatal=True)
        omission = event.get("mechanics_omission")
        if isinstance(omission, Mapping):
            add(
                str(omission.get("code", "CAT_V5_UNTYPED_MECHANICS_OMISSION")),
                f"sink {order} is ordered but its combat mechanics are omitted",
                fatal=False,
                evidence=omission,
            )
    wait = execution.get("wait_event")
    if proposal.gcd == "WAIT":
        if not isinstance(wait, Mapping) or wait.get("simulator_submission", {}).get("status") != "SUBMITTED" or wait.get("simulator_acceptance", {}).get("status") != "ACCEPTED" or wait.get("decision_consumption", {}).get("consumes_decision") is not True:
            add("CAT_V5_SOURCE_WAIT_INCOMPLETE", "source WAIT did not consume the simulator decision", fatal=True)
    elif wait is not None:
        add("CAT_V5_UNEXPECTED_WAIT", "non-WAIT proposal contains a wait event", fatal=True)
    reasons = execution.get("nonfaithful_reasons")
    if not isinstance(reasons, list):
        add("CAT_V5_NONFAITHFUL_LEDGER_UNTYPED", "nonfaithful ledger is not an array", fatal=True)
    else:
        for reason in reasons:
            add("CAT_V5_EXECUTOR_REPORTED_NONFAITHFUL", str(reason), fatal=True)
    return blockers


def _rename_result_followups(execution: Mapping[str, Any]) -> None:
    events = execution.get("sink_events")
    if not isinstance(events, list):
        return
    for event in events:
        if isinstance(event, dict) and "server_result_followup" in event:
            followup = event.pop("server_result_followup")
            if isinstance(followup, dict):
                followup["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
            event["simulator_outcome_followup"] = followup


def _attach_result_followups_v5(execution: JSONMap, observation: Mapping[str, Any]) -> None:
    _v2._attach_result_followups(execution, observation)
    _rename_result_followups(execution)


def _attach_registered_result_followups_v5(
    registry: Mapping[str, JSONMap], observation: Mapping[str, Any]
) -> None:
    _v2._attach_registered_result_followups(registry, observation)
    for event in registry.values():
        if "server_result_followup" in event:
            followup = event.pop("server_result_followup")
            if isinstance(followup, dict):
                followup["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
            event["simulator_outcome_followup"] = followup


def _audit_result_followups_v5(
    execution: Mapping[str, Any],
    *,
    decision_index: int,
    stream_available: bool,
) -> list[JSONMap]:
    if not stream_available:
        return []
    blockers: list[JSONMap] = []
    events = execution.get("sink_events")
    if not isinstance(events, list):
        return blockers
    for event in events:
        if not isinstance(event, Mapping):
            continue
        source = event.get("source_sink")
        acceptance = event.get("simulator_acceptance")
        operation = event.get("operation_contract")
        action = operation.get("canonical_action") if isinstance(operation, Mapping) else None
        if not (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
            and action in _RESULT_BEARING_GCD_ACTIONS_V5
        ):
            continue
        followup = event.get("simulator_outcome_followup")
        status = followup.get("status") if isinstance(followup, Mapping) else None
        if status == "PENDING_TYPED_RESULT_EVENT":
            continue
        if status != "OBSERVED_TYPED_RESULT_EVENT":
            blockers.append(
                _v2._blocker(
                    "CAT_V5_ACCEPTED_GCD_SIMULATOR_OUTCOME_UNOBSERVED",
                    f"accepted simulator GCD {action} has no typed simulator follow-up",
                    execution_fatal=True,
                    decision_index=decision_index,
                )
            )
            continue
        events_value = followup.get("events")
        expected_action = operation.get("action_ref")
        if not isinstance(events_value, list) or len(events_value) != 1 or not isinstance(events_value[0], Mapping) or events_value[0].get("action") != expected_action:
            blockers.append(
                _v2._blocker(
                    "CAT_V5_SIMULATOR_OUTCOME_ACTION_MISMATCH",
                    f"simulator outcome does not match accepted GCD {action}",
                    execution_fatal=True,
                    decision_index=decision_index,
                )
            )
    return blockers


def _annotate_immediate_state_deltas_v5(execution: JSONMap) -> None:
    _v2._annotate_immediate_state_deltas(execution)
    events = execution.get("sink_events")
    if not isinstance(events, list):
        return
    for event in events:
        delta = event.get("immediate_aggregate_state_delta") if isinstance(event, Mapping) else None
        if isinstance(delta, dict):
            value = delta.pop("individual_sink_server_outcome", None)
            delta["individual_sink_simulator_outcome"] = value
            delta["game_server_outcome_observed"] = False


def _observe_simulator_boundary_v5(*args: Any, **kwargs: Any) -> Any:
    observation, blocker, pending = _v2._observe_server_boundary(*args, **kwargs)
    stream = observation.get("result_stream")
    if isinstance(stream, dict):
        stream["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
    observation["game_server_outcome_observed"] = False
    return observation, blocker, pending


def _unknown_simulator_boundary_v5(*args: Any, **kwargs: Any) -> JSONMap:
    observation = _v2._unknown_server_boundary(*args, **kwargs)
    stream = observation.get("result_stream")
    if isinstance(stream, dict):
        stream["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
    observation["game_server_outcome_observed"] = False
    return observation


def _right_censor_v5(*args: Any, **kwargs: Any) -> Any:
    observation, blocker, pending = _v2._right_censor_terminal_active_hardcast(*args, **kwargs)
    attempt_sinks = args[2] if len(args) >= 3 else kwargs.get("attempt_sinks", {})
    if isinstance(attempt_sinks, Mapping):
        for event in attempt_sinks.values():
            if isinstance(event, dict) and "server_result_followup" in event:
                followup = event.pop("server_result_followup")
                if isinstance(followup, dict):
                    followup["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
                event["simulator_outcome_followup"] = followup
    if isinstance(observation, dict):
        stream = observation.get("result_stream")
        if isinstance(stream, dict):
            stream["evidence_scope"] = "SIMULATOR_RESULT_STREAM_NOT_GAME_SERVER"
        observation["game_server_outcome_observed"] = False
    return observation, blocker, pending


def _clone_core_v5(
    controls: CatSimulatorControlFacadeV5,
    inputs: CatFurySimulatorInputsV5,
) -> Any:
    namespace = dict(_v4._RUN_V4_CORE.__globals__)
    namespace.update(
        {
            "_supported_adapter": _supported_adapter_v5,
            "_combat_state": _cat_state_mapper(controls, inputs),
            "_proposal": _proposal_v5,
            "execute_ordered_sinks_v2": execute_cat_fury_ordered_sinks_v5,
            "_audit_ordered_execution": _audit_ordered_execution_v5,
            "_annotate_attempt_ids": _annotate_attempt_ids_v5,
            "_accepted_gcd_actions_from_events": _accepted_gcd_actions_v5,
            "_RESULT_BEARING_GCD_ACTIONS": _RESULT_BEARING_GCD_ACTIONS_V5,
            "_attach_result_followups": _attach_result_followups_v5,
            "_attach_registered_result_followups": _attach_registered_result_followups_v5,
            "_audit_gcd_result_followups": _audit_result_followups_v5,
            "_annotate_immediate_state_deltas": _annotate_immediate_state_deltas_v5,
            "_observe_server_boundary": _observe_simulator_boundary_v5,
            "_unknown_server_boundary": _unknown_simulator_boundary_v5,
            "_right_censor_terminal_active_hardcast": _right_censor_v5,
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_cat_fury_full_policy_rollout_v5_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


def _deduplicate_omissions(rows: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    seen: set[str] = set()
    result: list[JSONMap] = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(dict(row))
    return result


def _receipt_bundle_v5(
    result: Mapping[str, Any],
    inputs: CatFurySimulatorInputsV5,
    controls: CatSimulatorControlFacadeV5,
    dynamic_closure: Mapping[str, Any],
) -> JSONMap:
    mapping = validate_operation_mapping_receipt_v5(
        build_operation_mapping_receipt_v5()
    )
    steps = result.get("steps")
    steps = steps if isinstance(steps, list) else []
    attempts = 0
    submitted = 0
    typed = 0
    per_run_pairs: set[tuple[str, str]] = set()
    omissions: list[JSONMap] = []
    for step in steps:
        execution = step.get("ordered_execution") if isinstance(step, Mapping) else None
        events = execution.get("sink_events") if isinstance(execution, Mapping) else None
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            attempts += 1
            source = event.get("source_sink")
            submission = event.get("simulator_submission")
            acceptance = event.get("simulator_acceptance")
            if isinstance(source, Mapping):
                per_run_pairs.add((str(source.get("channel")), str(source.get("operation"))))
            if isinstance(submission, Mapping) and submission.get("status") in {"SUBMITTED", "NOT_SUBMITTED_STATE_ALREADY_SATISFIED"}:
                submitted += 1
            if isinstance(acceptance, Mapping) and acceptance.get("status") not in {None, "NOT_EVALUATED"}:
                typed += 1
            omission = event.get("mechanics_omission")
            if isinstance(omission, Mapping):
                omissions.append(dict(omission))
    fixed_omissions = (
        ("CAT_V5_TARGET_BANISH_LIVE_STATE_NOT_EXPOSED", "target banish is a caller-supplied simulator hypothesis"),
        ("CAT_V5_AUTOATTACK_LOCK_LIVE_STATE_NOT_EXPOSED", "Cat autoattack lock is absent from bridge state"),
        ("CAT_V5_PLAYER_HEALTH_LIVE_STATE_NOT_EXPOSED", "player health is absent from bridge state"),
        ("CAT_V5_TRINKET_RUNTIME_STATE_NOT_EXPOSED", "Cat trinket support/cooldown is a simulator input"),
        ("CAT_V5_INVENTORY_RUNTIME_STATE_NOT_EXPOSED", "Cat bag search state is a simulator input"),
        ("CAT_V5_OVERPOWER_PROC_LIVE_STATE_NOT_EXPOSED", "Cat supplemental Overpower proc state is a simulator input"),
        ("CVAR_CLIENT_QUEUE_MECHANICS_NOT_MODELED", "CVar writes are ordered sidecar controls, not Nampower client behavior"),
        ("ITEM_COMBAT_EFFECT_NOT_BOUND_TO_SIMULATOR_ACTION", "unbound Cat item slots are sidecar-only and cause no simulator combat effect"),
    )
    omissions.extend(
        {
            "code": code,
            "scope": "SIMULATOR_OMISSION",
            "message": message,
            "comparison_fatal": True,
        }
        for code, message in fixed_omissions
    )
    configured = result.get("configured_completion")
    lifecycle_checks = dynamic_closure.get("cursor_and_lifecycle_checks")
    lifecycle_complete = (
        dynamic_closure.get("status") == "COMPLETE_BOUND"
        and isinstance(lifecycle_checks, Mapping)
        and bool(lifecycle_checks)
        and all(value is True for value in lifecycle_checks.values())
    )
    order_complete = attempts == submitted == typed
    return {
        "schema": RECEIPT_BUNDLE_SCHEMA_V5,
        "operation": {
            "source_oracle_mapping_receipt": mapping,
            "run_raw_attempt_count": attempts,
            "run_submitted_or_typed_noop_count": submitted,
            "run_typed_simulator_disposition_count": typed,
            "run_operation_pairs": [
                {"channel": channel, "operation": operation}
                for channel, operation in sorted(per_run_pairs)
            ],
            "run_order_and_disposition_complete": order_complete,
            "source_oracle_to_executor_operation_mapping_complete": mapping["source_oracle_to_executor_mapping_complete"],
            "all_profile_operation_mechanics_modeled": False,
        },
        "horizon": {
            "configured_completion": configured,
            "scenario_complete": result.get("scenario_complete") is True,
            "bridge_finished": result.get("bridge_scenario_finished") is True,
            "decision_count": result.get("decision_count"),
            "advance_count": result.get("advance_count"),
            "max_decisions_guard": "ENFORCED_BY_PRIVATE_V2_LOOP",
            "max_advances_guard": "ENFORCED_BY_PRIVATE_V2_LOOP",
            "no_optional_stopping": True,
            "configured_horizon_complete": isinstance(configured, Mapping) and configured.get("criterion_met") is True,
        },
        "lifecycle": {
            "dynamic_v2_runtime_receipt_closure": dict(dynamic_closure),
            "all_cursor_and_lifecycle_checks_complete": lifecycle_complete,
            "simulator_hypothesis_not_historical_truth": True,
        },
        "omission": {
            "simulator_inputs": inputs.receipt(),
            "control_sidecar": controls.sidecar_receipt(),
            "rows": _deduplicate_omissions(omissions),
            "omissions_complete_and_typed": True,
            "game_client_load_observed": False,
            "game_client_ordered_trace_observed": False,
            "client_acceptance_observed": False,
            "game_server_outcome_observed": False,
        },
        "source_simulator_engineering_gate_closed": (
            order_complete
            and mapping["source_oracle_to_executor_mapping_complete"]
            and isinstance(configured, Mapping)
            and configured.get("criterion_met") is True
            and lifecycle_complete
        ),
        "comparison_ready": False,
        "formal_runner_registration_authorized": False,
    }


def _permanent_blockers_v5() -> list[JSONMap]:
    rows = (
        ("CAT_V5_GAME_CLIENT_LOAD_ATTESTATION_MISSING", "this exact Cat source/profile has no post-/reload load receipt"),
        ("CAT_V5_GAME_CLIENT_ORDERED_TRACE_MISSING", "source-to-simulator order is not an observed WoW client sink trace"),
        ("CAT_V5_CLIENT_ACCEPTANCE_TRACE_MISSING", "simulator acceptance is not WoW client acceptance"),
        ("CAT_V5_GAME_SERVER_OUTCOME_TRACE_MISSING", "simulator result events are not Turtle WoW server outcomes"),
        ("CAT_V5_CVAR_CLIENT_MECHANICS_OMITTED", "NP CVar sidecar order does not emulate Nampower client queue behavior"),
        ("CAT_V5_ITEM_MECHANICS_INCOMPLETE", "unbound bag/trinket sinks have no simulator combat effect"),
        ("CAT_V5_FORMAL_RUNNER_REGISTRATION_FORBIDDEN", "v5 is an isolated diagnostic and is not registered in the formal runner"),
    )
    return [
        _v2._blocker(code, message, execution_fatal=False)
        for code, message in rows
    ]


def run_cat_fury_full_policy_rollout_v5(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: CatFuryFullPolicyAdapterV4,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV2,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Run one non-comparison Cat v5 dynamic simulator diagnostic."""

    isolation = _verify_frozen_source_identity()
    if type(adapter) is not CatFuryFullPolicyAdapterV4:
        raise TypeError("adapter must be exact CatFuryFullPolicyAdapterV4")
    if not isinstance(dynamic_load, DynamicRolloutLoadV2):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV2")
    inputs = simulator_inputs or CatFurySimulatorInputsV5()
    if not isinstance(inputs, CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5 or None")
    if retain_steps is not True:
        raise CatFuryFullPolicyRolloutV5Error(
            "Cat v5 requires retain_steps=True for complete ordered receipts"
        )
    required = (
        "load_dynamic_v2",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "parsed_dynamic_state",
        "set_target",
        "start_attack",
        "stop_cast",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    if missing:
        raise CatFuryFullPolicyRolloutV5Error(
            "Cat v5 bridge capabilities missing: " + ", ".join(missing)
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
    facade = _DynamicV2CatBridgeFacadeV5(controls)
    core = _clone_core_v5(controls, inputs)
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
        raise CatFuryFullPolicyRolloutV5Error("private core returned non-object")
    result["schema"] = ROLLOUT_SCHEMA_V5
    result["implementation_revision"] = IMPLEMENTATION_REVISION
    result["expert_id"] = POLICY_ID
    result["version_isolation"] = {
        **isolation,
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "actual_process_load_command": "load_dynamic_v2",
    }
    capabilities = result.get("bridge_capabilities")
    if isinstance(capabilities, dict):
        capabilities.pop("load_dynamic_v1", None)
        capabilities.update(
            {
                "load_dynamic_v2": True,
                "cat_set_cvar_sidecar": True,
                "cat_use_inventory_item_sidecar": True,
                "cat_use_container_item_sidecar": True,
                "dynamic_attackability_receipts": True,
                "dynamic_armor_receipts": True,
                "dynamic_damage_receipts": True,
                "dynamic_candidate_damage_receipts": True,
                "parsed_dynamic_state": True,
            }
        )
    result["bridge_command_contract"] = {
        "initial_load_command": "load_dynamic_v2",
        "source_raw_order_preserved_within_invocation": True,
        "native_actions": ["set_target", "start_attack", "stop_cast", "act", "wait", "advance"],
        "sidecar_controls": ["cat_set_cvar", "cat_use_inventory_item", "cat_use_container_item"],
        "sidecar_acceptance_is_native_combat_mechanics": False,
        "simulator_acceptance_is_client_acceptance": False,
        "simulator_result_is_game_server_outcome": False,
    }
    result["dynamic_load_binding"] = _v4._dynamic_load_binding_receipt_v4(
        dynamic_load,
        None if facade.last_load_result is None else facade.last_load_result.receipt,
    )
    dynamic_closure = _v4._collect_runtime_receipts_v4(
        bridge,
        dynamic_load,
        facade.last_load_result,
        result.get("final_state"),
    )
    result["dynamic_v2_runtime_receipt_closure"] = dynamic_closure
    result["cat_v5_receipts"] = _receipt_bundle_v5(
        result, inputs, controls, dynamic_closure
    )
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise CatFuryFullPolicyRolloutV5Error("rollout blockers are malformed")
    blockers.extend(_permanent_blockers_v5())
    if dynamic_closure.get("status") != "COMPLETE_BOUND":
        blockers.append(
            _v2._blocker(
                "CAT_V5_DYNAMIC_LIFECYCLE_RECEIPT_INCOMPLETE",
                "dynamic-v2 lifecycle/cursor receipt did not close",
                execution_fatal=True,
                evidence={"status": dynamic_closure.get("status")},
            )
        )
    result["blocker_summary"] = _v2._blocker_summary(blockers)
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_NONCOMPARISON_DIAGNOSTIC"
    result["source_to_simulator_order_faithful"] = result["cat_v5_receipts"]["operation"]["run_order_and_disposition_complete"]
    result["ordered_projection_faithful"] = False
    result["simulator_dps_comparison_eligible"] = False
    result["historical_truth"] = False
    result["voting_eligible"] = False
    result["comparison_ready"] = False
    result["formal_runner_registration_authorized"] = False
    result["formal_runner_registry_modified"] = False
    result["scientific_run_launched"] = False
    result["game_client_load_observed"] = False
    result["game_client_ordered_trace_observed"] = False
    result["client_acceptance_observed"] = False
    result["game_server_outcome_observed"] = False
    result["minimum_future_game_collection"] = [
        dict(row) for row in MINIMUM_FUTURE_GAME_COLLECTION
    ]
    result["claims_excluded"] = list(
        dict.fromkeys(
            list(result.get("claims_excluded") or [])
            + [
                "simulator acceptance as WoW client acceptance",
                "simulator outcome as Turtle WoW server outcome",
                "sidecar item or CVar acceptance as modeled DPS effect",
                "Cat v5 diagnostic as a formal baseline comparison",
            ]
        )
    )
    if isinstance(result.get("steps"), list):
        for step in result["steps"]:
            if isinstance(step, dict) and isinstance(step.get("simulator_state_before"), Mapping):
                dynamic = step["simulator_state_before"].get("dynamic_target_semantics")
                if isinstance(dynamic, Mapping):
                    step["dynamic_v2_live_target_state_before"] = dict(dynamic)
    result.pop("content_address", None)
    core_value = dict(result)
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _canonical_sha256(core_value),
    }
    return validate_cat_fury_full_policy_rollout_v5(
        result, dynamic_load=dynamic_load
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_cat_fury_full_policy_rollout_v5(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV2 | None = None,
) -> JSONMap:
    try:
        raw = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise CatFuryFullPolicyRolloutV5Error(f"rollout is not strict JSON: {error}") from error
    if raw.get("schema") != ROLLOUT_SCHEMA_V5 or raw.get("implementation_revision") != IMPLEMENTATION_REVISION or raw.get("expert_id") != POLICY_ID:
        raise CatFuryFullPolicyRolloutV5Error("rollout identity mismatch")
    for name in (
        "ordered_projection_faithful",
        "simulator_dps_comparison_eligible",
        "historical_truth",
        "voting_eligible",
        "comparison_ready",
        "formal_runner_registration_authorized",
        "formal_runner_registry_modified",
        "scientific_run_launched",
        "game_client_load_observed",
        "game_client_ordered_trace_observed",
        "client_acceptance_observed",
        "game_server_outcome_observed",
    ):
        if raw.get(name) is not False:
            raise CatFuryFullPolicyRolloutV5Error(f"{name} must remain false")
    isolation = raw.get("version_isolation")
    if not isinstance(isolation, Mapping) or isolation.get("expected_sha256") != EXPECTED_FROZEN_SOURCE_SHA256 or isolation.get("observed_sha256") != EXPECTED_FROZEN_SOURCE_SHA256 or isolation.get("all_match") is not True or isolation.get("private_function_namespace_clone") is not True or isolation.get("global_monkeypatch") is not False or isolation.get("old_source_bytes_modified_by_v5") is not False or isolation.get("actual_process_load_command") != "load_dynamic_v2":
        raise CatFuryFullPolicyRolloutV5Error("version isolation receipt mismatch")
    command = raw.get("bridge_command_contract")
    if not isinstance(command, Mapping) or command.get("initial_load_command") != "load_dynamic_v2" or command.get("source_raw_order_preserved_within_invocation") is not True or command.get("sidecar_acceptance_is_native_combat_mechanics") is not False or command.get("simulator_acceptance_is_client_acceptance") is not False or command.get("simulator_result_is_game_server_outcome") is not False:
        raise CatFuryFullPolicyRolloutV5Error("bridge command boundary mismatch")
    receipts = raw.get("cat_v5_receipts")
    if not isinstance(receipts, Mapping) or receipts.get("schema") != RECEIPT_BUNDLE_SCHEMA_V5 or receipts.get("comparison_ready") is not False or receipts.get("formal_runner_registration_authorized") is not False:
        raise CatFuryFullPolicyRolloutV5Error("receipt bundle identity mismatch")
    operation = receipts.get("operation")
    if not isinstance(operation, Mapping):
        raise CatFuryFullPolicyRolloutV5Error("operation receipt missing")
    validate_operation_mapping_receipt_v5(operation.get("source_oracle_mapping_receipt"))
    omission = receipts.get("omission")
    if not isinstance(omission, Mapping) or omission.get("game_client_load_observed") is not False or omission.get("game_client_ordered_trace_observed") is not False or omission.get("client_acceptance_observed") is not False or omission.get("game_server_outcome_observed") is not False:
        raise CatFuryFullPolicyRolloutV5Error("omission evidence boundary mismatch")
    steps = raw.get("steps")
    if raw.get("steps_retained") is True:
        if not isinstance(steps, list):
            raise CatFuryFullPolicyRolloutV5Error("retained steps are missing")
        for step in steps:
            execution = step.get("ordered_execution") if isinstance(step, Mapping) else None
            events = execution.get("sink_events") if isinstance(execution, Mapping) else None
            if not isinstance(events, list):
                raise CatFuryFullPolicyRolloutV5Error("step ordered execution is missing")
            for event in events:
                if not isinstance(event, Mapping) or "simulator_acceptance" not in event or "client_acceptance" in event or event.get("client_observation") != {"status": "NOT_OBSERVED_NO_WOW_CLIENT", "accepted": None, "evidence": None} or event.get("server_outcome") != {"status": "NOT_OBSERVED_NO_GAME_SERVER_LOG", "outcome": None, "damage": None, "evidence": None}:
                    raise CatFuryFullPolicyRolloutV5Error("sink evidence layers are collapsed")
    required_codes = {
        "CAT_V5_GAME_CLIENT_LOAD_ATTESTATION_MISSING",
        "CAT_V5_GAME_CLIENT_ORDERED_TRACE_MISSING",
        "CAT_V5_CLIENT_ACCEPTANCE_TRACE_MISSING",
        "CAT_V5_GAME_SERVER_OUTCOME_TRACE_MISSING",
        "CAT_V5_CVAR_CLIENT_MECHANICS_OMITTED",
        "CAT_V5_ITEM_MECHANICS_INCOMPLETE",
        "CAT_V5_FORMAL_RUNNER_REGISTRATION_FORBIDDEN",
    }
    blockers = raw.get("blockers")
    codes = {
        row.get("code")
        for row in blockers
        if isinstance(row, Mapping)
    } if isinstance(blockers, list) else set()
    if not required_codes.issubset(codes):
        raise CatFuryFullPolicyRolloutV5Error("mandatory blockers are missing")
    binding = raw.get("dynamic_load_binding")
    if not isinstance(binding, Mapping) or binding.get("actual_bridge_command") != "load_dynamic_v2" or binding.get("config_schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2 or binding.get("historical_truth") is not False:
        raise CatFuryFullPolicyRolloutV5Error("dynamic load binding mismatch")
    if dynamic_load is not None:
        if not isinstance(dynamic_load, DynamicRolloutLoadV2):
            raise TypeError("dynamic_load must be DynamicRolloutLoadV2 or None")
        if binding.get("config_digest") != dynamic_load.config.content_sha256 or binding.get("simulator_seed") != dynamic_load.seed:
            raise CatFuryFullPolicyRolloutV5Error("rollout differs from dynamic load")
    content = raw.get("content_address")
    expected_content = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _canonical_sha256(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if content != expected_content:
        raise CatFuryFullPolicyRolloutV5Error("rollout content address mismatch")
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if '"initial_load_command": "load_dynamic_v1"' in encoded:
        raise CatFuryFullPolicyRolloutV5Error("legacy dynamic load command leaked")
    return raw


__all__ = (
    "CatFuryFullPolicyRolloutV5Error",
    "CatFurySimulatorInputsV5",
    "EXPECTED_FROZEN_SOURCE_SHA256",
    "IMPLEMENTATION_REVISION",
    "RECEIPT_BUNDLE_SCHEMA_V5",
    "ROLLOUT_SCHEMA_V5",
    "run_cat_fury_full_policy_rollout_v5",
    "validate_cat_fury_full_policy_rollout_v5",
)
