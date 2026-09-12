"""Full encounter feedback-policy loop for Cat2_new candidate plans (v6).

At every real simulator decision boundary this loop reads the live dynamic
state, asks a candidate policy for ordered intents, compiles a v4 plan, and
executes it through the v5 simulator executor.  The bridge remains the owner
of rage, GCD/cooldowns, queue, target, equipment, health, death, and retarget
state; Python never reconstructs or resets those fields between decisions.

Result-bearing attempts remain pending across decisions by attempt ID.  A v5
``SIMULATOR_RESULTS_PENDING_AT_PLAN_END`` omission is provisional here.  At an
exact scoring horizon, one accepted attempt that still matches the simulator's
active hardcast is right-censored without scoring post-horizon damage.  Other
attempts still open at termination remain fatal for offline scoring.

This module runs an offline candidate only.  Even a complete zero-omission
rollout cannot register a formal runner or establish Cat2_new client fidelity.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .cat2new_candidate_executor_v4 import (
    DEFAULT_INSTALLED_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SAVEDVARIABLES,
    DEFAULT_SOURCE_ROOT,
    REQUEST_SCHEMA,
    build_readiness_report_v4,
    compile_candidate_action_plan_v4,
)
from .cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
    Cat2NewSimulatorRunBindingV5,
    execute_cat2new_candidate_plan_v5,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V6 = "cat2new_candidate_feedback_rollout/v6"
POLICY_INPUT_SCHEMA_V6 = "cat2new_candidate_feedback_policy_input/v6"
POLICY_INTENT_SCHEMA_V6 = "cat2new_candidate_feedback_policy_intent/v6"
DECISION_RECEIPT_SCHEMA_V6 = "cat2new_candidate_feedback_decision/v6"
LIFECYCLE_RECEIPT_SCHEMA_V6 = "cat2new_candidate_feedback_lifecycle/v6"
EXECUTOR_ID_V6 = "cat2new.candidate.feedback.executor.v6"
IMPLEMENTATION_REVISION = "v6.2_exact_horizon_active_hardcast_right_censor"
LEGACY_IMPLEMENTATION_REVISION = "v6.1_live_action_availability_input"
SUPPORTED_IMPLEMENTATION_REVISIONS = frozenset(
    {LEGACY_IMPLEMENTATION_REVISION, IMPLEMENTATION_REVISION}
)

_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_PROVISIONAL_PENDING_CODE = "SIMULATOR_RESULTS_PENDING_AT_PLAN_END"


class Cat2NewCandidateFeedbackLoopV6Error(RuntimeError):
    """The feedback policy, dynamic state, or lifecycle contract failed."""


@dataclass(frozen=True)
class Cat2NewPolicyIntentV6:
    """One policy decision expressed entirely in the v4 operation language."""

    operations: tuple[Mapping[str, Any], ...]
    policy_metadata: Mapping[str, Any]
    schema: str = POLICY_INTENT_SCHEMA_V6

    def __post_init__(self) -> None:
        if self.schema != POLICY_INTENT_SCHEMA_V6:
            raise ValueError("policy intent schema mismatch")
        if not isinstance(self.operations, tuple) or not self.operations:
            raise TypeError("operations must be a nonempty tuple")
        if any(not isinstance(row, Mapping) for row in self.operations):
            raise TypeError("operations must contain objects")
        _json_copy(self.policy_metadata, "policy_metadata")

    def to_wire(self) -> JSONMap:
        return {
            "schema": self.schema,
            "operations": [_json_copy(row, "policy operation") for row in self.operations],
            "policy_metadata": _json_copy(self.policy_metadata, "policy_metadata"),
        }


class Cat2NewFeedbackPolicyV6(Protocol):
    """Optimization policies consume one immutable live-state input."""

    policy_id: str

    def decide(self, policy_input: Mapping[str, Any]) -> Cat2NewPolicyIntentV6: ...


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            f"value is not finite canonical JSON: {error}"
        ) from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _digest(core),
    }


def _json_copy(value: Any, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(f"{label} must be an object")
    copy = json.loads(_canonical_bytes(value).decode("utf-8"))
    if not isinstance(copy, dict):
        raise AssertionError("JSON object did not remain an object")
    return copy


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            f"{label} must be a positive integer"
        )
    return value


def _state_identity(
    state: Mapping[str, Any], run: Cat2NewSimulatorRunBindingV5
) -> JSONMap:
    copy = _json_copy(state, "dynamic simulator state")
    for field in ("time_ms", "needs_input", "finished", "target_index"):
        if field not in copy:
            raise Cat2NewCandidateFeedbackLoopV6Error(
                f"dynamic simulator state lacks {field}"
            )
    if isinstance(copy["time_ms"], bool) or not isinstance(copy["time_ms"], int):
        raise Cat2NewCandidateFeedbackLoopV6Error("state.time_ms must be integer")
    if copy["time_ms"] < 0:
        raise Cat2NewCandidateFeedbackLoopV6Error("state.time_ms must be nonnegative")
    if not isinstance(copy["needs_input"], bool) or not isinstance(copy["finished"], bool):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "state needs_input/finished must be boolean"
        )
    lifecycle = copy.get("dynamic_team_background")
    semantics = copy.get("dynamic_target_semantics")
    if not isinstance(lifecycle, Mapping) or not isinstance(semantics, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "live dynamic lifecycle and target-semantics blocks are required"
        )
    if (
        lifecycle.get("environment_generation") != run.environment_generation
        or semantics.get("environment_generation") != run.environment_generation
        or lifecycle.get("config_digest") != run.dynamic_config_sha256
        or semantics.get("config_digest") != run.dynamic_config_sha256
    ):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "live dynamic state differs from the bound generation/config"
        )
    carried = {
        key: deepcopy(copy.get(key))
        for key in (
            "power",
            "gcd_remaining_ms",
            "auras",
            "target_auras",
            "queued_swing",
            "target_index",
            "equipment_slots",
            "mh_swing_remaining_ms",
            "oh_swing_remaining_ms",
        )
        if key in copy
    }
    return {
        "state": copy,
        "raw_state_sha256": _digest(copy),
        "time_ms": copy["time_ms"],
        "needs_input": copy["needs_input"],
        "finished": copy["finished"],
        "target_index": copy["target_index"],
        "carried_state": carried,
    }


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise Cat2NewCandidateFeedbackLoopV6Error(
        f"receipt contains unsupported {type(value).__name__}"
    )


def _result_partition(receipt: Any, requested: Sequence[str]) -> tuple[JSONMap, list[str]]:
    value = _serialize(receipt)
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "server result receipt must serialize to an object"
        )
    pending = value.get("pending_attempt_ids")
    if not isinstance(pending, list) or any(
        not isinstance(item, str) or not item for item in pending
    ):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "server result receipt lacks typed pending_attempt_ids"
        )
    events = value.get("events")
    if isinstance(events, list):
        resolved = [
            row.get("attempt_id")
            for row in events
            if isinstance(row, Mapping)
        ]
    else:
        resolved = value.get("resolved_attempt_ids", [])
    if not isinstance(resolved, list) or any(
        not isinstance(item, str) or not item for item in resolved
    ):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "server result receipt lacks typed resolved attempt IDs"
        )
    if len(set(resolved)) != len(resolved) or len(set(pending)) != len(pending):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "server result receipt repeats attempt IDs"
        )
    if set(resolved) & set(pending) or set(resolved) | set(pending) != set(requested):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "server result receipt does not partition requested attempt IDs"
        )
    return _json_copy(value, "server result receipt"), list(pending)


def _settle_pending(bridge: Any, pending: Sequence[str]) -> tuple[JSONMap | None, list[str]]:
    if not pending:
        return None, []
    method = getattr(bridge, "server_results_since_last_decision", None)
    if not callable(method):
        return {
            "status": "API_UNAVAILABLE",
            "requested_attempt_ids": list(pending),
        }, list(pending)
    receipt, remaining = _result_partition(method(list(pending)), pending)
    return {
        "status": "CAPTURED",
        "requested_attempt_ids": list(pending),
        "receipt": receipt,
        "remaining_attempt_ids": remaining,
    }, remaining


def _right_censor_exact_horizon_active_hardcast(
    current: Mapping[str, Any],
    pending: Sequence[str],
    decisions: Sequence[Mapping[str, Any]],
    run_binding: Cat2NewSimulatorRunBindingV5,
) -> tuple[JSONMap | None, list[str]]:
    """Close one causally matched active hardcast at the scoring boundary.

    The attempt remains present in every source receipt.  This only changes its
    terminal accounting from unresolved to right-censored when the frozen state
    is exactly at the configured horizon and still exposes the same active cast.
    """

    remaining = list(pending)
    if (
        len(remaining) != 1
        or current.get("finished") is not True
        or current.get("time_ms") != run_binding.horizon_end_ms
    ):
        return None, remaining
    state = current.get("state")
    cast = state.get("current_cast") if isinstance(state, Mapping) else None
    cast_action = cast.get("action") if isinstance(cast, Mapping) else None
    cast_remaining = cast.get("remaining_ms") if isinstance(cast, Mapping) else None
    if (
        not isinstance(cast_action, Mapping)
        or isinstance(cast_remaining, bool)
        or not isinstance(cast_remaining, int)
        or cast_remaining <= 0
    ):
        return None, remaining

    attempt_id = remaining[0]
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for decision in decisions:
        execution = decision.get("v5_execution")
        events = execution.get("ordered_events") if isinstance(execution, Mapping) else None
        if not isinstance(events, list):
            continue
        for event in events:
            dispatch = event.get("dispatch") if isinstance(event, Mapping) else None
            if isinstance(dispatch, Mapping) and dispatch.get("attempt_id") == attempt_id:
                matches.append((decision, event))
    if len(matches) != 1:
        return None, remaining
    decision, event = matches[0]
    dispatch = event.get("dispatch")
    acceptance = event.get("simulator_acceptance")
    assert isinstance(dispatch, Mapping)
    if (
        dispatch.get("status") != "SUBMITTED"
        or not isinstance(dispatch.get("action"), Mapping)
        or dict(dispatch["action"]) != dict(cast_action)
        or not isinstance(acceptance, Mapping)
        or acceptance.get("status") != "ACCEPTED"
    ):
        return None, remaining
    accepted_at = decision.get("time_ms")
    if (
        isinstance(accepted_at, bool)
        or not isinstance(accepted_at, int)
        or accepted_at < 0
        or accepted_at > run_binding.horizon_end_ms
    ):
        return None, remaining

    receipt = {
        "status": "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
        "requested_attempt_ids": [attempt_id],
        "remaining_attempt_ids": [],
        "receipt": {
            "complete_through_time_ms": run_binding.horizon_end_ms,
            "events": [],
            "pending_attempt_ids": [],
            "censored_attempt_ids": [attempt_id],
            "active_cast_action": _serialize(cast_action),
            "active_cast_remaining_ms": cast_remaining,
            "accepted_at_time_ms": accepted_at,
            "acceptance_is_not_result": True,
            "post_horizon_damage_scored": False,
        },
    }
    return receipt, []


def _new_attempt_partition(execution: Mapping[str, Any]) -> tuple[list[str], JSONMap | None]:
    result = execution.get("simulator_result_receipt")
    if not isinstance(result, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "v5 execution lacks simulator_result_receipt"
        )
    attempt_ids = result.get("attempt_ids")
    if not isinstance(attempt_ids, list) or any(
        not isinstance(item, str) or not item for item in attempt_ids
    ):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "v5 execution attempt_ids are malformed"
        )
    if not attempt_ids:
        return [], None
    receipt = result.get("receipt")
    if isinstance(receipt, Mapping):
        parsed, pending = _result_partition(receipt, attempt_ids)
        return pending, parsed
    if result.get("status") == "API_UNAVAILABLE_TYPED_OMISSION":
        return list(attempt_ids), None
    if result.get("status") == "CAPTURE_FAILED":
        return list(attempt_ids), None
    raise Cat2NewCandidateFeedbackLoopV6Error(
        "v5 result status does not account for submitted attempts"
    )


def _target_boundary(state: Mapping[str, Any]) -> JSONMap:
    lifecycle = state["dynamic_team_background"]
    semantics = state["dynamic_target_semantics"]
    index = state["target_index"]
    rows = semantics.get("targets")
    life_rows = lifecycle.get("targets")
    selected = None
    selected_lifecycle = None
    if isinstance(index, int) and isinstance(rows, list) and 0 <= index < len(rows):
        selected = rows[index]
    if isinstance(index, int) and isinstance(life_rows, list) and 0 <= index < len(life_rows):
        selected_lifecycle = life_rows[index]
    return {
        "target_index": index,
        "retarget_required": lifecycle.get("retarget_required"),
        "selected_target_semantics": deepcopy(selected),
        "selected_target_lifecycle": deepcopy(selected_lifecycle),
        "target_semantics_rows": deepcopy(rows),
        "target_lifecycle_rows": deepcopy(life_rows),
    }


def _requires_explicit_retarget(boundary: Mapping[str, Any]) -> bool:
    row = boundary.get("selected_target_lifecycle")
    blocked = boundary.get("retarget_required") is True or (
        isinstance(row, Mapping) and row.get("dead") is True
    )
    if not blocked:
        return False

    # A dynamic attackability phase can set retarget_required even though no
    # other target can legally receive actions.  In that state WAIT is the
    # policy's only useful choice; demanding a SET_EXACT_UNIT would deadlock the
    # encounter before the scheduled attackability transition can run.
    current_index = boundary.get("target_index")
    target_rows = boundary.get("target_semantics_rows")
    lifecycle_rows = boundary.get("target_lifecycle_rows")
    if not isinstance(target_rows, list) or not isinstance(lifecycle_rows, list):
        return False
    for index, target in enumerate(target_rows):
        if index == current_index or not isinstance(target, Mapping):
            continue
        life = lifecycle_rows[index] if index < len(lifecycle_rows) else None
        dead = target.get("dead") is True or (
            isinstance(life, Mapping) and life.get("dead") is True
        )
        if not dead and target.get("attackable") is True:
            return True
    return False


def _has_exact_target_first(intent: Cat2NewPolicyIntentV6) -> bool:
    first = intent.operations[0]
    return first.get("lane") == "target" and first.get("intent") == "SET_EXACT_UNIT"


def _omission(code: str, message: str, **evidence: Any) -> JSONMap:
    return {
        "code": code,
        "message": message,
        "evidence": _serialize(evidence),
        "offline_score_fatal": True,
    }


def _decision_receipt(core: JSONMap) -> JSONMap:
    return {**core, "content_address": _content_address(core)}


def _terminal_dynamic_receipts(bridge: Any, final_state: Mapping[str, Any]) -> JSONMap:
    method_names = (
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "parsed_dynamic_state",
    )
    if any(not callable(getattr(bridge, name, None)) for name in method_names):
        return {
            "status": "UNAVAILABLE",
            "missing": [
                name for name in method_names if not callable(getattr(bridge, name, None))
            ],
        }
    try:
        attack = _serialize(bridge.dynamic_attackability_receipts(cursor=0))
        armor = _serialize(bridge.dynamic_armor_receipts(cursor=0))
        background = _serialize(bridge.dynamic_damage_receipts(cursor=0))
        candidate = _serialize(bridge.dynamic_candidate_damage_receipts(cursor=0))
        lifecycle, semantics = bridge.parsed_dynamic_state(final_state)
        rows = {
            "attackability": attack,
            "armor": armor,
            "background_damage": background,
            "candidate_damage": candidate,
            "terminal_lifecycle": _serialize(lifecycle),
            "terminal_target_semantics": _serialize(semantics),
        }
        schedule_complete = all(
            isinstance(row, Mapping) and row.get("schedule_complete") is True
            for row in (attack, armor, background)
        )
        return {
            "status": "COMPLETE" if schedule_complete else "INCOMPLETE",
            **rows,
        }
    except Exception as error:
        return {"status": "ERROR", "error_type": type(error).__name__}


def run_cat2new_feedback_policy_v6(
    bridge: Any,
    policy: Cat2NewFeedbackPolicyV6,
    *,
    simulator_request: Mapping[str, Any],
    dynamic_load_receipt: Any,
    run_binding: Cat2NewSimulatorRunBindingV5,
    operation_bindings: Cat2NewSimulatorOperationBindingsV5 = (
        Cat2NewSimulatorOperationBindingsV5()
    ),
    optimizer_parameters: Mapping[str, Any] | None = None,
    historical_prior: Mapping[str, Any] | None = None,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT,
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Run a loaded dynamic encounter until death, finish, or bound horizon."""

    if not isinstance(run_binding, Cat2NewSimulatorRunBindingV5):
        raise TypeError("run_binding must be Cat2NewSimulatorRunBindingV5")
    if not isinstance(operation_bindings, Cat2NewSimulatorOperationBindingsV5):
        raise TypeError("operation_bindings has the wrong type")
    max_decisions = _positive_int(max_decisions, "max_decisions")
    max_advances = _positive_int(max_advances, "max_advances")
    policy_id = getattr(policy, "policy_id", None)
    if not isinstance(policy_id, str) or _ID_RE.fullmatch(policy_id) is None:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "policy.policy_id must be a normalized identifier"
        )
    decide = getattr(policy, "decide", None)
    if not callable(decide):
        raise Cat2NewCandidateFeedbackLoopV6Error("policy.decide is required")

    request = _json_copy(simulator_request, "simulator_request")
    parameters = _json_copy(optimizer_parameters or {}, "optimizer_parameters")
    prior = _json_copy(historical_prior or {}, "historical_prior")
    load_receipt = _serialize(dynamic_load_receipt)
    if not isinstance(load_receipt, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "dynamic_load_receipt must serialize to an object"
        )
    if _digest(request) != run_binding.request_sha256:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "simulator request differs from run binding"
        )
    if _digest(load_receipt) != run_binding.dynamic_load_receipt_sha256:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "dynamic load receipt differs from run binding"
        )

    source_readiness = build_readiness_report_v4(
        source_root=source_root,
        manifest_path=manifest_path,
        installed_root=installed_root,
        savedvariables_path=savedvariables_path,
    )
    if source_readiness["source_identity"]["verified"] is not True:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "Cat2_new source identity is not verified"
        )

    initial_raw = bridge.state()
    if not isinstance(initial_raw, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "bridge.state must return an object"
        )
    initial = _state_identity(initial_raw, run_binding)
    current = initial
    decisions: list[JSONMap] = []
    advances: list[JSONMap] = []
    omissions: list[JSONMap] = []
    pending: list[str] = []
    resolved_receipts: list[JSONMap] = []
    decision_count = 0
    advance_count = 0
    terminal_reason: str | None = None
    prior_command_final_sha = current["raw_state_sha256"]

    while terminal_reason is None:
        raw = bridge.state()
        if not isinstance(raw, Mapping):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "bridge.state must return an object"
            )
        observed = _state_identity(raw, run_binding)
        continuity = observed["raw_state_sha256"] == prior_command_final_sha
        if not continuity:
            omissions.append(
                _omission(
                    "CROSS_DECISION_STATE_CONTINUITY_FAILED",
                    "bridge state changed without an observed command",
                    expected_sha256=prior_command_final_sha,
                    observed_sha256=observed["raw_state_sha256"],
                )
            )
            terminal_reason = "STATE_CONTINUITY_FAILED"
            current = observed
            break
        current = observed

        settled, pending = _settle_pending(bridge, pending)
        if settled is not None:
            resolved_receipts.append(settled)
        if current["finished"] is True:
            terminal_reason = "ENCOUNTER_FINISHED"
            break
        if current["time_ms"] >= run_binding.horizon_end_ms:
            terminal_reason = "HORIZON_REACHED"
            break

        if current["needs_input"] is not True:
            if advance_count >= max_advances:
                omissions.append(
                    _omission(
                        "MAX_ADVANCES_EXCEEDED",
                        "encounter exceeded the configured advance bound",
                        max_advances=max_advances,
                    )
                )
                terminal_reason = "ADVANCE_BOUND"
                break
            advance = getattr(bridge, "advance", None)
            if not callable(advance):
                omissions.append(
                    _omission(
                        "ADVANCE_CAPABILITY_MISSING",
                        "simulator is idle but exposes no advance command",
                    )
                )
                terminal_reason = "ADVANCE_UNAVAILABLE"
                break
            after_raw = advance()
            if not isinstance(after_raw, Mapping):
                raise Cat2NewCandidateFeedbackLoopV6Error(
                    "bridge.advance must return an object"
                )
            after = _state_identity(after_raw, run_binding)
            advance_count += 1
            receipt_core = {
                "advance_index": advance_count,
                "before_raw_state_sha256": current["raw_state_sha256"],
                "after_raw_state_sha256": after["raw_state_sha256"],
                "before_time_ms": current["time_ms"],
                "after_time_ms": after["time_ms"],
                "state_after": after,
            }
            advances.append({**receipt_core, "content_address": _content_address(receipt_core)})
            if after["raw_state_sha256"] == current["raw_state_sha256"]:
                omissions.append(
                    _omission(
                        "IDLE_ADVANCE_MADE_NO_PROGRESS",
                        "advance returned an unchanged idle state",
                        advance_index=advance_count,
                    )
                )
                terminal_reason = "IDLE_NO_PROGRESS"
            if after["time_ms"] > run_binding.horizon_end_ms:
                omissions.append(
                    _omission(
                        "ADVANCE_CROSSED_BOUND_HORIZON",
                        "advance moved beyond the bound encounter horizon",
                        observed_time_ms=after["time_ms"],
                        horizon_end_ms=run_binding.horizon_end_ms,
                    )
                )
                terminal_reason = "HORIZON_OVERSHOOT"
            current = after
            prior_command_final_sha = after["raw_state_sha256"]
            continue

        if decision_count >= max_decisions:
            omissions.append(
                _omission(
                    "MAX_DECISIONS_EXCEEDED",
                    "encounter exceeded the configured decision bound",
                    max_decisions=max_decisions,
                )
            )
            terminal_reason = "DECISION_BOUND"
            break
        decision_count += 1
        target_boundary = _target_boundary(current["state"])
        available_actions = _serialize(bridge.actions())
        if not isinstance(available_actions, list) or any(
            not isinstance(row, Mapping) for row in available_actions
        ):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "bridge.actions must return serializable AvailableAction rows"
            )
        policy_input: JSONMap = {
            "schema": POLICY_INPUT_SCHEMA_V6,
            "policy_id": policy_id,
            "decision_index": decision_count,
            "run_binding_content_sha256": run_binding.content_sha256,
            "live_state": deepcopy(current["state"]),
            "available_actions": available_actions,
            "pending_attempt_ids": list(pending),
            "target_boundary": target_boundary,
            "optimizer_parameters": parameters,
            "historical_prior": prior,
        }
        intent = decide(deepcopy(policy_input))
        if not isinstance(intent, Cat2NewPolicyIntentV6):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "policy.decide must return Cat2NewPolicyIntentV6"
            )
        if _requires_explicit_retarget(target_boundary) and not _has_exact_target_first(intent):
            row = _omission(
                "EXPLICIT_RETARGET_INTENT_MISSING",
                "a dead/retarget-required target needs SET_EXACT_UNIT as the first operation",
                decision_index=decision_count,
                target_boundary=target_boundary,
            )
            omissions.append(row)
            rejected_core: JSONMap = {
                "schema": DECISION_RECEIPT_SCHEMA_V6,
                "decision_index": decision_count,
                "status": "REJECTED_PRE_EXECUTION",
                "time_ms": current["time_ms"],
                "policy_input_sha256": _digest(policy_input),
                "live_state_raw_sha256": current["raw_state_sha256"],
                "carried_state_before": current["carried_state"],
                "target_boundary": target_boundary,
                "policy_intent": intent.to_wire(),
                "v4_plan": None,
                "v5_execution": None,
                "state_continuity": None,
                "pending_attempt_ids_after": list(pending),
                "provisional_pending_count": 0,
                "permanent_omissions": [row],
            }
            decisions.append(_decision_receipt(rejected_core))
            terminal_reason = "RETARGET_INTENT_MISSING"
            break

        intent_wire = intent.to_wire()
        plan_request = {
            "schema": REQUEST_SCHEMA,
            "plan_id": f"cat2new.v6.d{decision_count:06d}",
            "candidate_policy_id": policy_id,
            "state_snapshot_sha256": _digest(policy_input),
            "operations": intent_wire["operations"],
        }
        plan = compile_candidate_action_plan_v4(
            plan_request,
            source_root=source_root,
            manifest_path=manifest_path,
            verify_live_source=False,
        )
        execution = execute_cat2new_candidate_plan_v5(
            bridge,
            plan,
            policy_state_snapshot=policy_input,
            simulator_request=request,
            dynamic_load_receipt=load_receipt,
            run_binding=run_binding,
            operation_bindings=operation_bindings,
            source_root=source_root,
            manifest_path=manifest_path,
            verify_live_source=False,
        )
        execution_initial = execution["lifecycle_receipt"]["initial_state"]
        execution_final = execution["lifecycle_receipt"]["final_state"]
        decision_continuity = (
            execution_initial["raw_state_sha256"] == current["raw_state_sha256"]
        )
        if not decision_continuity:
            omissions.append(
                _omission(
                    "POLICY_TO_EXECUTION_STATE_CHANGED",
                    "simulator state changed between policy observation and v5 execution",
                    decision_index=decision_count,
                )
            )

        new_pending, initial_result_receipt = _new_attempt_partition(execution)
        overlap = set(pending) & set(new_pending)
        if overlap:
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "new attempt IDs collide with the outstanding ledger"
            )
        pending.extend(new_pending)
        if initial_result_receipt is not None:
            resolved_receipts.append(
                {
                    "status": "CAPTURED_BY_V5",
                    "decision_index": decision_count,
                    "receipt": initial_result_receipt,
                    "remaining_attempt_ids": list(new_pending),
                }
            )
        permanent = [
            deepcopy(row)
            for row in execution["typed_omissions"]
            if row.get("code") != _PROVISIONAL_PENDING_CODE
        ]
        omissions.extend(permanent)
        decision_core: JSONMap = {
            "schema": DECISION_RECEIPT_SCHEMA_V6,
            "decision_index": decision_count,
            "status": "EXECUTED",
            "time_ms": current["time_ms"],
            "policy_input_sha256": _digest(policy_input),
            "live_state_raw_sha256": current["raw_state_sha256"],
            "carried_state_before": current["carried_state"],
            "target_boundary": target_boundary,
            "policy_intent": intent_wire,
            "v4_plan": plan,
            "v5_execution": execution,
            "state_continuity": {
                "policy_to_execution_initial": decision_continuity,
                "execution_initial_raw_sha256": execution_initial["raw_state_sha256"],
                "execution_final_raw_sha256": execution_final["raw_state_sha256"],
            },
            "pending_attempt_ids_after": list(pending),
            "provisional_pending_count": len(new_pending),
            "permanent_omissions": permanent,
        }
        decisions.append(_decision_receipt(decision_core))
        current = {
            "state": None,
            "raw_state_sha256": execution_final["raw_state_sha256"],
            "time_ms": execution_final["time_ms"],
            "needs_input": execution_final["needs_input"],
            "finished": execution_final["finished"],
            "target_index": execution_final["target_index"],
            "carried_state": {},
        }
        prior_command_final_sha = execution_final["raw_state_sha256"]
        if permanent or execution["execution_blocked"] is True or not decision_continuity:
            terminal_reason = "DECISION_EXECUTION_BLOCKED"

    terminal_settlement, pending = _settle_pending(bridge, pending)
    if terminal_settlement is not None:
        resolved_receipts.append(terminal_settlement)
    right_censor_receipt, pending = _right_censor_exact_horizon_active_hardcast(
        current,
        pending,
        decisions,
        run_binding,
    )
    if right_censor_receipt is not None:
        resolved_receipts.append(right_censor_receipt)
    right_censored_attempt_ids = (
        list(right_censor_receipt["requested_attempt_ids"])
        if right_censor_receipt is not None
        else []
    )
    if pending:
        omissions.append(
            _omission(
                "PENDING_ACTIONS_AT_ROLLOUT_END",
                "accepted simulator attempts remain unresolved at encounter termination",
                pending_attempt_ids=pending,
            )
        )

    final_raw = bridge.state()
    if not isinstance(final_raw, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "bridge.state final read must return an object"
        )
    final = _state_identity(final_raw, run_binding)
    if final["raw_state_sha256"] != prior_command_final_sha:
        omissions.append(
            _omission(
                "TERMINAL_STATE_CONTINUITY_FAILED",
                "terminal state changed without an observed command",
            )
        )
    terminal_dynamic = _terminal_dynamic_receipts(bridge, final["state"])
    if terminal_dynamic["status"] != "COMPLETE":
        omissions.append(
            _omission(
                "TERMINAL_DYNAMIC_RECEIPTS_INCOMPLETE",
                "full dynamic lifecycle receipt families did not close",
                status=terminal_dynamic["status"],
            )
        )
    reached_terminal = terminal_reason in {"ENCOUNTER_FINISHED", "HORIZON_REACHED"}
    zero_omission = not omissions
    every_decision_complete = all(
        row.get("status") == "EXECUTED"
        and (
            row["v5_execution"]["offline_plan_interval_complete"] is True
            or (
                row["v5_execution"]["offline_plan_execution_faithful"] is False
                and {
                    omission.get("code")
                    for omission in row["v5_execution"]["typed_omissions"]
                }
                == {_PROVISIONAL_PENDING_CODE}
                and row["v5_execution"]["execution_blocked"] is False
            )
        )
        for row in decisions
    )
    lifecycle_core: JSONMap = {
        "schema": LIFECYCLE_RECEIPT_SCHEMA_V6,
        "terminal_reason": terminal_reason,
        "initial_state": initial,
        "final_state": final,
        "decision_count": decision_count,
        "advance_count": advance_count,
        "decision_receipt_sha256": [
            row["content_address"]["sha256"] for row in decisions
        ],
        "advance_receipt_sha256": [
            row["content_address"]["sha256"] for row in advances
        ],
        "resolved_result_receipts": resolved_receipts,
        "pending_attempt_ids": list(pending),
        "right_censored_attempt_ids": right_censored_attempt_ids,
        "terminal_dynamic_receipts": terminal_dynamic,
        "time_nondecreasing": all(
            decisions[index]["time_ms"] <= decisions[index + 1]["time_ms"]
            for index in range(len(decisions) - 1)
        ),
        "horizon_end_ms": run_binding.horizon_end_ms,
        "within_horizon": final["time_ms"] <= run_binding.horizon_end_ms,
    }
    lifecycle = {**lifecycle_core, "content_address": _content_address(lifecycle_core)}
    omission_core = {
        "count": len(omissions),
        "rows": omissions,
    }
    omission_receipt = {
        **omission_core,
        "content_address": _content_address(omission_core),
    }
    offline_score_eligible = (
        reached_terminal
        and decision_count > 0
        and zero_omission
        and every_decision_complete
        and not pending
        and terminal_dynamic["status"] == "COMPLETE"
        and lifecycle["time_nondecreasing"] is True
        and lifecycle["within_horizon"] is True
    )
    core: JSONMap = {
        "schema": ROLLOUT_SCHEMA_V6,
        "executor_id": EXECUTOR_ID_V6,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "identity": {
            "policy_id": policy_id,
            "run_binding": run_binding.to_wire(),
            "operation_bindings": operation_bindings.to_wire(),
            "simulator_request_sha256": _digest(request),
            "dynamic_load_receipt_sha256": _digest(load_receipt),
            "cat2new_source_tree_sha256": source_readiness["identity"]["source_tree_sha256"],
            "installed_tree_matches_cat2new": source_readiness["deployed_identity"]["installed_tree"].get("matches_cat2new_tree") is True,
            "optimizer_parameters_sha256": _digest(parameters),
            "historical_prior_sha256": _digest(prior),
        },
        "policy_context": {
            "optimizer_parameters": parameters,
            "historical_prior": prior,
        },
        "decisions": decisions,
        "idle_advances": advances,
        "lifecycle_receipt": lifecycle,
        "omission_receipt": omission_receipt,
        "status": "COMPLETE" if reached_terminal and zero_omission else "INCOMPLETE",
        "offline_score_eligible": offline_score_eligible,
        "formal_runner_registration_authorized": False,
        "live_client_execution": False,
        "comparison_ready": False,
        "scientific_run_launched": False,
        "limitations": {
            "cat2new_deployed": source_readiness["deployed_identity"]["installed_tree"].get("matches_cat2new_tree") is True,
            "cat2new_client_trace": False,
            "wow_server_trace": False,
            "formal_runner_registered": False,
        },
        "claim_boundary": "complete offline feedback-loop mechanics only; no Cat2_new client fidelity, formal registration, or policy superiority claim",
    }
    return validate_cat2new_feedback_rollout_v6(
        {**core, "content_address": _content_address(core)}
    )


def _validate_address(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateFeedbackLoopV6Error(f"{label} must be an object")
    core = {key: item for key, item in value.items() if key != "content_address"}
    if value.get("content_address") != _content_address(core):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            f"{label} content address mismatch"
        )


def validate_cat2new_feedback_rollout_v6(value: Mapping[str, Any]) -> JSONMap:
    document = _json_copy(value, "v6 rollout")
    if document.get("schema") != ROLLOUT_SCHEMA_V6:
        raise Cat2NewCandidateFeedbackLoopV6Error("rollout schema mismatch")
    revision = document.get("implementation_revision")
    if revision not in SUPPORTED_IMPLEMENTATION_REVISIONS:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "rollout implementation revision is unsupported"
        )
    _validate_address(document, "rollout")
    for field in (
        "formal_runner_registration_authorized",
        "live_client_execution",
        "comparison_ready",
        "scientific_run_launched",
    ):
        if document.get(field) is not False:
            raise Cat2NewCandidateFeedbackLoopV6Error(
                f"rollout {field} must remain false"
            )
    decisions = document.get("decisions")
    if not isinstance(decisions, list):
        raise Cat2NewCandidateFeedbackLoopV6Error("decisions must be an array")
    for index, decision in enumerate(decisions, start=1):
        _validate_address(decision, f"decision[{index}]")
        if decision.get("decision_index") != index:
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "decision indexes are not contiguous"
            )
    advances = document.get("idle_advances")
    if not isinstance(advances, list):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "idle_advances must be an array"
        )
    for index, advance in enumerate(advances, start=1):
        _validate_address(advance, f"idle_advance[{index}]")
        if advance.get("advance_index") != index:
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "advance indexes are not contiguous"
            )
    lifecycle = document.get("lifecycle_receipt")
    omissions = document.get("omission_receipt")
    _validate_address(lifecycle, "lifecycle_receipt")
    _validate_address(omissions, "omission_receipt")
    assert isinstance(lifecycle, Mapping) and isinstance(omissions, Mapping)
    if lifecycle.get("decision_count") != len(decisions):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "lifecycle decision count mismatch"
        )
    if lifecycle.get("advance_count") != len(advances):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "lifecycle advance count mismatch"
        )
    rows = omissions.get("rows")
    if not isinstance(rows, list) or omissions.get("count") != len(rows):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "omission receipt count mismatch"
        )
    censored = lifecycle.get("right_censored_attempt_ids", [])
    if (
        not isinstance(censored, list)
        or any(not isinstance(item, str) or not item for item in censored)
        or len(set(censored)) != len(censored)
    ):
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "right-censored attempt IDs are malformed"
        )
    if revision == IMPLEMENTATION_REVISION and "right_censored_attempt_ids" not in lifecycle:
        raise Cat2NewCandidateFeedbackLoopV6Error(
            "current rollout revision lacks right-censor accounting"
        )
    if censored:
        receipts = lifecycle.get("resolved_result_receipts")
        final_state = lifecycle.get("final_state")
        raw_state = final_state.get("state") if isinstance(final_state, Mapping) else None
        active_cast = raw_state.get("current_cast") if isinstance(raw_state, Mapping) else None
        active_action = active_cast.get("action") if isinstance(active_cast, Mapping) else None
        active_remaining = (
            active_cast.get("remaining_ms") if isinstance(active_cast, Mapping) else None
        )
        censor_receipts = [
            row
            for row in receipts
            if isinstance(row, Mapping)
            and row.get("status")
            == "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON"
        ] if isinstance(receipts, list) else []
        if (
            len(censored) != 1
            or lifecycle.get("pending_attempt_ids")
            or not isinstance(final_state, Mapping)
            or final_state.get("time_ms") != lifecycle.get("horizon_end_ms")
            or not isinstance(active_action, Mapping)
            or isinstance(active_remaining, bool)
            or not isinstance(active_remaining, int)
            or active_remaining <= 0
            or len(censor_receipts) != 1
        ):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "right-censored hardcast lacks an exact terminal boundary"
            )
        censor = censor_receipts[0]
        evidence = censor.get("receipt")
        if (
            censor.get("requested_attempt_ids") != censored
            or censor.get("remaining_attempt_ids") != []
            or not isinstance(evidence, Mapping)
            or evidence.get("censored_attempt_ids") != censored
            or evidence.get("pending_attempt_ids") != []
            or evidence.get("events") != []
            or evidence.get("post_horizon_damage_scored") is not False
            or evidence.get("active_cast_action") != active_action
            or evidence.get("active_cast_remaining_ms") != active_remaining
        ):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "right-censor receipt differs from the terminal active cast"
            )
    if document.get("offline_score_eligible") is True:
        if rows or document.get("status") != "COMPLETE":
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "offline scoring requires a complete zero-omission rollout"
            )
        if lifecycle.get("pending_attempt_ids"):
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "offline scoring forbids pending actions"
            )
        terminal = lifecycle.get("terminal_dynamic_receipts")
        if not isinstance(terminal, Mapping) or terminal.get("status") != "COMPLETE":
            raise Cat2NewCandidateFeedbackLoopV6Error(
                "offline scoring requires complete terminal dynamic receipts"
            )
    return document


def serialize_cat2new_feedback_rollout_v6(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_cat2new_feedback_rollout_v6(value)) + b"\n"


__all__ = [
    "Cat2NewCandidateFeedbackLoopV6Error",
    "Cat2NewFeedbackPolicyV6",
    "Cat2NewPolicyIntentV6",
    "DECISION_RECEIPT_SCHEMA_V6",
    "EXECUTOR_ID_V6",
    "IMPLEMENTATION_REVISION",
    "LIFECYCLE_RECEIPT_SCHEMA_V6",
    "POLICY_INPUT_SCHEMA_V6",
    "POLICY_INTENT_SCHEMA_V6",
    "ROLLOUT_SCHEMA_V6",
    "run_cat2new_feedback_policy_v6",
    "serialize_cat2new_feedback_rollout_v6",
    "validate_cat2new_feedback_rollout_v6",
]
