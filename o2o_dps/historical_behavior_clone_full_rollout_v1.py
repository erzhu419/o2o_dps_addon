"""Dynamic-v5 full-rollout lane for the pooled clean-Fury behavior clone.

The model is always supplied explicitly.  This module does not discover or
materialize Stage5 data, and it does not replace the frozen historical-v2
runner.  It executes the V1 semi-Markov policy against the same native
``load_dynamic_v3`` request used by the other runner-v4 lanes and remains a
simulator-only, non-voting diagnostic baseline candidate.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from . import fury_full_policy_rollout_v2 as _rollout_v2
from . import fury_full_policy_rollout_v5 as _rollout_v5
from . import fury_paired_multiseed_runner_v4 as _runner_v4
from . import historical_behavior_clone_v1 as clone_v1
from .historical_behavior_clone_simulator_adapter_v1 import (
    ACTION_SINK_BINDINGS_V1,
    ADAPTER_SCHEMA,
    EPOCH_SCHEMA,
    POLICY_ID,
    HistoricalBehaviorCloneSimulatorAdapterV1,
    run_historical_behavior_clone_epoch_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import AvailableAction
from .sim_bridge_dynamic_v3 import DynamicLoadResultV3


JSONMap = dict[str, Any]
SCHEMA = "historical_behavior_clone_dynamic_v5_rollout/v1"
IMPLEMENTATION_REVISION = "v1.0_pooled_clean_fury_semi_markov_dynamic_v5"
PRODUCER = "historical_behavior_clone_full_rollout_v1"
CONTENT_ADDRESS_SCHEMA = "historical_behavior_clone_rollout_content/v1"
MODEL_BINDING_SCHEMA = "historical_behavior_clone_model_binding/v1"
STATUS = "COMPLETE_SIMULATOR_ONLY_POOLED_CLEAN_FURY_NONVOTING"


class HistoricalBehaviorCloneFullRolloutV1Error(RuntimeError):
    """The model binding, dynamic-v5 execution, or artifact is malformed."""


def _strict_json(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            f"{label} is not strict JSON: {error}"
        ) from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            f"{label} must be an object"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _model_binding(model: Mapping[str, Any]) -> JSONMap:
    clone_v1.validate_model_v1(model)
    strict = _strict_json(model, "behavior-clone model")
    profile = {
        "cohort_contract": strict["cohort_contract"],
        "action_ontology": strict["action_ontology"],
        "delay_head_kind": strict["delay_head"]["kind"],
        "mark_head_kind": strict["mark_head"]["kind"],
        "target_head_kind": strict["target_head"]["kind"],
        "claim_boundary": strict["claim_boundary"],
    }
    adapter_contract = {
        "adapter_schema": ADAPTER_SCHEMA,
        "epoch_schema": EPOCH_SCHEMA,
        "policy_id": POLICY_ID,
        "sink_bindings": [
            ACTION_SINK_BINDINGS_V1[key].to_dict()
            for key in clone_v1.ACTION_KEYS
        ],
        "runtime_clock": "SEMI_MARKOV_NEXT_ACTION_DELAY",
        "dynamic_load": "load_dynamic_v3",
    }
    return {
        "schema": MODEL_BINDING_SCHEMA,
        "policy_id": POLICY_ID,
        "model_schema": clone_v1.MODEL_SCHEMA,
        "model_sha256": _runner_v4.sha256_json(strict),
        "adapter_contract_sha256": _runner_v4.sha256_json(adapter_contract),
        "profile_sha256": _runner_v4.sha256_json(profile),
        "cohort_id": clone_v1.COHORT_ID,
        "pooled_clean_fury": True,
        "top_player_policy": False,
        "historical_dps_ranking_used": False,
    }


def load_behavior_clone_model_v1(path: str | Path) -> tuple[JSONMap, JSONMap]:
    """Read and validate one explicitly named model artifact."""

    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            f"could not read behavior-clone model: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "behavior-clone model must be an object"
        )
    clone_v1.validate_model_v1(value)
    model = _strict_json(value, "behavior-clone model")
    return model, _model_binding(model)


def build_behavior_clone_policy_descriptor_v1(
    model_path: str | Path,
) -> JSONMap:
    """Return the exact runner-v4 identity for an explicit model file."""

    _, binding = load_behavior_clone_model_v1(model_path)
    return {
        "policy_id": POLICY_ID,
        "source_sha256": binding["model_sha256"],
        "adapter_sha256": binding["adapter_contract_sha256"],
        "profile_sha256": binding["profile_sha256"],
        "role": "BASELINE_CANDIDATE_NONVOTING",
    }


def behavior_clone_lane_contract_v1() -> JSONMap:
    """Return the additive runner-v4 dynamic-v5 lane contract."""

    return _runner_v4.LaneContractV4(
        policy_id=POLICY_ID,
        producer=PRODUCER,
        artifact_schema=SCHEMA,
        source_oracle_status="POOLED_CLEAN_FURY_BEHAVIOR_CLONE_V1_READY",
        ordered_sink_status="ALL_15_TYPED_SINKS_AND_SEMI_MARKOV_WAIT_READY",
        full_policy_status="DYNAMIC_V5_FULL_ROLLOUT_V1_READY_NONVOTING",
        dynamic_v5_executable=True,
        blocker_codes=(),
    ).to_wire()


def _stance_from_state(state: Mapping[str, Any], last_action: str | None) -> str | None:
    labels = []
    auras = state.get("auras")
    if isinstance(auras, list):
        labels = [
            str(row.get("label", "")).casefold()
            for row in auras
            if isinstance(row, Mapping)
        ]
    for needle, value in (
        ("berserker stance", "berserker_stance"),
        ("defensive stance", "defensive_stance"),
        ("battle stance", "battle_stance"),
    ):
        if any(needle in label for label in labels):
            return value
    if last_action in {
        "warrior.battle_stance",
        "warrior.defensive_stance",
        "warrior.berserker_stance",
    }:
        return str(last_action).removeprefix("warrior.")
    return None


def _pending_queue(state: Mapping[str, Any]) -> str | None:
    auras = state.get("auras")
    if not isinstance(auras, list):
        return None
    for row in auras:
        action = row.get("action") if isinstance(row, Mapping) else None
        if not isinstance(action, Mapping) or action.get("tag") != 1:
            continue
        spell_id = action.get("spell_id")
        if spell_id in {11567, 25286}:
            return "warrior.heroic_strike"
        if spell_id == 20569:
            return "warrior.cleave"
    return None


def _observation_factory(
    *,
    root_time_ms: int,
    last_gcd: dict[str, Any],
    queue_started: dict[str, int],
) -> Callable[[Mapping[str, Any], tuple[AvailableAction, ...], Any], JSONMap]:
    def build(
        state: Mapping[str, Any],
        available: tuple[AvailableAction, ...],
        adapter: HistoricalBehaviorCloneSimulatorAdapterV1,
    ) -> JSONMap:
        del available
        lifecycle = _mapping(
            state.get("dynamic_team_background"),
            "dynamic_team_background",
        )
        targets = lifecycle.get("targets")
        if not isinstance(targets, list) or any(
            not isinstance(row, Mapping) or not isinstance(row.get("dead"), bool)
            for row in targets
        ):
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                "dynamic target lifecycle rows are unavailable"
            )
        now = _integer(state.get("time_ms"), "state.time_ms")
        elapsed = now - root_time_ms
        if elapsed < 0:
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                "simulator time precedes the loaded root state"
            )
        background = lifecycle.get("background_damage_applied")
        if isinstance(background, bool) or not isinstance(background, (int, float)):
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                "background damage is not numeric"
            )
        last_action = adapter.runtime.last_action_key
        last_lane = (
            None
            if last_action is None
            else clone_v1.ACTION_SPEC_BY_KEY[last_action].lane
        )
        queue = _pending_queue(state)
        if queue is not None and queue not in queue_started:
            queue_started[queue] = now
        for key in tuple(queue_started):
            if key != queue:
                del queue_started[key]
        last_gcd_time = last_gcd.get("time_ms")
        return {
            "schema": clone_v1.OBSERVATION_SCHEMA,
            # BehaviorCloneFrameV1 binds this field to simulator time_ms.
            "wave_elapsed_ms": now,
            "observed_target_count": len(targets),
            "observed_dead_target_count": sum(row["dead"] is True for row in targets),
            "background_dps": (
                0.0 if elapsed == 0 else float(background) * 1000.0 / elapsed
            ),
            "actor_has_last_target": (
                isinstance(state.get("target_index"), int)
                and not isinstance(state.get("target_index"), bool)
                and state.get("num_targets", 0) > 0
            ),
            "last_controllable_action": last_action,
            "last_controllable_lane": last_lane,
            "last_gcd_action_age_ms": (
                None
                if last_gcd_time is None
                else max(0, now - int(last_gcd_time))
            ),
            "observed_stance": _stance_from_state(state, last_action),
            "pending_queue_action": queue,
            "pending_queue_age_ms": (
                None if queue is None else max(0, now - queue_started[queue])
            ),
        }

    return build


def _update_observation_history(
    epoch: Mapping[str, Any], last_gcd: dict[str, Any], queue_started: dict[str, int]
) -> None:
    for step in epoch.get("steps", []):
        if not isinstance(step, Mapping) or step.get("kind") != "ACTION":
            continue
        if step.get("client_acceptance") != "ACCEPTED":
            continue
        binding = step.get("typed_sink_binding")
        final_state = step.get("final_state")
        if not isinstance(binding, Mapping) or not isinstance(final_state, Mapping):
            continue
        now = final_state.get("time_ms")
        if isinstance(now, bool) or not isinstance(now, int):
            continue
        action_key = binding.get("action_key")
        if binding.get("policy_lane") == "gcd":
            last_gcd.update({"action_key": action_key, "time_ms": now})
        if binding.get("policy_lane") == "queue" and isinstance(action_key, str):
            queue_started[action_key] = now


def _required_bridge_capabilities(bridge: Any) -> JSONMap:
    names = (
        "load_dynamic_v3",
        "state",
        "actions",
        "act",
        "wait",
        "advance",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "dynamic_idle_advance_receipts",
        "parsed_dynamic_state",
    )
    capabilities = {name: callable(getattr(bridge, name, None)) for name in names}
    missing = [name for name, present in capabilities.items() if not present]
    if missing:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "dynamic-v5 bridge capabilities missing: " + ", ".join(missing)
        )
    return capabilities


def run_behavior_clone_dynamic_v5_rollout_v1(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    seed: int,
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute one complete native dynamic-v5 pooled-clone scenario."""

    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    seed = _integer(seed, "seed", minimum=1)
    if dynamic_load.seed != seed:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "dynamic-load seed differs from rollout seed"
        )
    max_decisions = _integer(max_decisions, "max_decisions", minimum=1)
    max_advances = _integer(max_advances, "max_advances", minimum=1)
    if not isinstance(retain_steps, bool):
        raise TypeError("retain_steps must be boolean")
    _rollout_v2._validate_request(raid_sim_request)
    request_sha = _runner_v4.sha256_json(raid_sim_request)
    _rollout_v5._validate_dynamic_load_request_v5(
        dynamic_load, raid_sim_request, request_sha256=request_sha
    )
    binding = _model_binding(model)
    capabilities = _required_bridge_capabilities(bridge)
    adapter = HistoricalBehaviorCloneSimulatorAdapterV1(
        model, simulator_seed=seed
    )

    loaded = bridge.load_dynamic_v3(
        raid_sim_request, seed, dynamic_load.config
    )
    if not isinstance(loaded, DynamicLoadResultV3):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "bridge.load_dynamic_v3 returned the wrong type"
        )
    _rollout_v5._validate_dynamic_load_result_v5(dynamic_load, loaded)
    state = deepcopy(dict(loaded.state))
    _rollout_v5._validate_dynamic_loaded_state_v5(
        state, dynamic_load, loaded.receipt
    )
    reported = deepcopy(dict(bridge.state()))
    _rollout_v5._validate_dynamic_loaded_state_v5(
        reported, dynamic_load, loaded.receipt
    )
    if _rollout_v5._state_fingerprint_v5(state) != _rollout_v5._state_fingerprint_v5(reported):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "state() differs from the state returned by load_dynamic_v3"
        )
    state = reported
    root_state = deepcopy(state)
    last_gcd: dict[str, Any] = {}
    queue_started: dict[str, int] = {}
    observation = _observation_factory(
        root_time_ms=int(root_state["time_ms"]),
        last_gcd=last_gcd,
        queue_started=queue_started,
    )
    epochs: list[JSONMap] = []
    epoch_count = 0
    decision_count = 0
    advance_count = 0
    fatal_errors: list[JSONMap] = []

    try:
        while state.get("finished") is not True:
            if state.get("needs_input") is not True:
                if advance_count >= max_advances:
                    raise HistoricalBehaviorCloneFullRolloutV1Error(
                        f"scenario exceeded max_advances={max_advances}"
                    )
                before = state
                state = deepcopy(dict(bridge.advance()))
                _rollout_v5._validate_dynamic_loaded_state_v5(
                    state, dynamic_load, loaded.receipt
                )
                if _rollout_v5._state_fingerprint_v5(before) == _rollout_v5._state_fingerprint_v5(state):
                    raise HistoricalBehaviorCloneFullRolloutV1Error(
                        "bridge.advance returned an unchanged state"
                    )
                advance_count += 1
                continue
            if decision_count >= max_decisions:
                raise HistoricalBehaviorCloneFullRolloutV1Error(
                    f"scenario exceeded max_decisions={max_decisions}"
                )
            before = deepcopy(state)
            epoch = run_historical_behavior_clone_epoch_v1(
                adapter,
                bridge,
                state,
                observation,
                attempt_id_prefix=f"behavior-clone-epoch-{epoch_count}",
            )
            if epoch.get("schema") != EPOCH_SCHEMA or epoch.get("status") != "COMPLETE":
                raise HistoricalBehaviorCloneFullRolloutV1Error(
                    "behavior-clone epoch did not complete"
                )
            state = deepcopy(dict(_mapping(epoch.get("final_state"), "epoch.final_state")))
            _rollout_v5._validate_dynamic_loaded_state_v5(
                state, dynamic_load, loaded.receipt
            )
            _update_observation_history(epoch, last_gcd, queue_started)
            substeps = _integer(epoch.get("substep_count"), "epoch.substep_count", minimum=1)
            decision_count += substeps
            epoch_row = deepcopy(dict(epoch))
            epoch_row["epoch_index"] = epoch_count
            epoch_row["simulator_state_before"] = before
            epoch_row["time_delta_ms"] = int(state["time_ms"]) - int(before["time_ms"])
            epoch_row["damage_delta"] = float(state["damage_done"]) - float(before["damage_done"])
            if retain_steps:
                epochs.append(epoch_row)
            epoch_count += 1
    except Exception as error:
        fatal_errors.append(
            {
                "code": "BEHAVIOR_CLONE_DYNAMIC_V5_EXECUTION_ERROR",
                "error_type": type(error).__name__,
                "message": str(error),
            }
        )

    final_state = deepcopy(dict(bridge.state()))
    _rollout_v5._validate_dynamic_loaded_state_v5(
        final_state, dynamic_load, loaded.receipt
    )
    completion = _rollout_v5._completion_receipt_v5(
        raid_sim_request, root_state, final_state, dynamic_load=dynamic_load
    )
    closure = _rollout_v5._collect_runtime_receipts_v5(
        bridge, dynamic_load, loaded, final_state
    )
    complete = completion["criterion_met"] is True and not fatal_errors
    elapsed_ms = int(final_state["time_ms"]) - int(root_state["time_ms"])
    damage = float(final_state["damage_done"]) - float(root_state["damage_done"])
    if elapsed_ms <= 0:
        detail = fatal_errors[0]["message"] if fatal_errors else "no elapsed time"
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            f"behavior-clone rollout stopped before positive elapsed time: {detail}"
        )
    artifact: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS if complete else "INCOMPLETE_SIMULATOR_ONLY_NONVOTING",
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
        "model_binding": binding,
        "dynamic_load_binding": _rollout_v5._dynamic_load_binding_receipt_v5(
            dynamic_load, loaded.receipt
        ),
        "bridge_capabilities": capabilities,
        "root_state": root_state,
        "final_state": final_state,
        "configured_completion": completion,
        "scenario_complete": complete,
        "elapsed_ms": elapsed_ms,
        "damage_delta": damage,
        "diagnostic_dps": None if elapsed_ms <= 0 else damage * 1000.0 / elapsed_ms,
        "decision_count": decision_count,
        "advance_count": advance_count,
        "epochs_retained": retain_steps,
        "epochs": epochs if retain_steps else None,
        "fatal_errors": fatal_errors,
        "dynamic_v3_runtime_receipt_closure": closure,
        "claim_boundary": {
            "cohort": "POOLED_CLEAN_FURY",
            "top_player_policy": False,
            "same_runtime_request_and_loadout_as_other_lanes": True,
            "historical_players_normalized_to_this_loadout": False,
            "simulator_only": True,
            "live_fidelity": False,
            "comparison_ready": False,
            "voting_eligible": False,
            "offline_score_eligible": False,
            "scientific_result_available": False,
        },
        "version_isolation": {
            "explicit_model_required": True,
            "implicit_stage5_discovery": False,
            "frozen_historical_v2_runner_modified": False,
            "frozen_four_lane_worker_modified": False,
            "actual_process_load_command": "load_dynamic_v3",
        },
    }
    artifact["content_address"] = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _runner_v4.sha256_json(artifact),
    }
    validate_behavior_clone_dynamic_v5_rollout_v1(
        artifact,
        closure,
        dynamic_load,
        expected_model_binding=binding,
    )
    return artifact


def _producer_summary(artifact: Mapping[str, Any]) -> JSONMap:
    elapsed = _integer(artifact.get("elapsed_ms"), "elapsed_ms", minimum=1)
    final_state = _mapping(artifact.get("final_state"), "final_state")
    completion = _mapping(
        artifact.get("configured_completion"), "configured_completion"
    )
    closure = _mapping(
        artifact.get("dynamic_v3_runtime_receipt_closure"),
        "dynamic runtime receipt closure",
    )
    fatal = artifact.get("fatal_errors")
    if not isinstance(fatal, list):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "fatal_errors must be an array"
        )
    reason_counts = Counter(
        str(row.get("code")) for row in fatal if isinstance(row, Mapping)
    )
    complete = artifact.get("scenario_complete") is True
    damage = float(artifact.get("damage_delta", 0.0))
    return {
        "policy_id": POLICY_ID,
        "artifact_schema": SCHEMA,
        "completion_mode": (
            str(completion.get("terminal_reason")) if complete else "INCOMPLETE"
        ),
        "damage": damage,
        "elapsed_ms": elapsed,
        "dps": damage * 1000.0 / elapsed,
        "completion_criterion_met": complete,
        "offline_score_eligible": False,
        "omitted_lane_count": 0,
        "fatal_error_count": len(fatal),
        "nonfaithful_reason_counts": dict(sorted(reason_counts.items())),
        "end_state_sha256": _runner_v4.sha256_json(final_state),
        "dynamic_runtime_receipts_complete": closure.get("status") == "COMPLETE_BOUND",
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_behavior_clone_dynamic_v5_rollout_v1(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
    *,
    expected_model_binding: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Validate the artifact and return runner-v4's producer summary."""

    raw = _strict_json(artifact, "behavior-clone rollout")
    if not isinstance(raw, dict):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "behavior-clone rollout must be an object"
        )
    fixed = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
    }
    if any(raw.get(key) != value for key, value in fixed.items()):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "behavior-clone rollout identity mismatch"
        )
    boundary = _mapping(raw.get("claim_boundary"), "claim_boundary")
    if boundary != {
        "cohort": "POOLED_CLEAN_FURY",
        "top_player_policy": False,
        "same_runtime_request_and_loadout_as_other_lanes": True,
        "historical_players_normalized_to_this_loadout": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "voting_eligible": False,
        "offline_score_eligible": False,
        "scientific_result_available": False,
    }:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "behavior-clone claim boundary mismatch"
        )
    binding = _mapping(raw.get("model_binding"), "model_binding")
    if expected_model_binding is not None and dict(binding) != dict(expected_model_binding):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "rollout model differs from the explicitly loaded model"
        )
    dynamic = _mapping(raw.get("dynamic_load_binding"), "dynamic_load_binding")
    expected_dynamic = _rollout_v5._dynamic_load_binding_receipt_v5(
        dynamic_load, None
    )
    for field in (
        "schema",
        "actual_bridge_command",
        "contract_sha256",
        "request_sha256",
        "simulator_seed",
        "config_schema",
        "config_digest",
        "target_count",
        "background_event_count",
        "attackability_event_count",
        "effective_armor_event_count",
        "same_timestamp_order",
        "retarget_mode",
        "idle_advance_mode",
        "idle_advance_horizon_ms",
        "historical_truth",
    ):
        if dynamic.get(field) != expected_dynamic.get(field):
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                f"dynamic-v5 binding mismatch at {field}"
            )
    if dynamic.get("load_succeeded") is not True or not isinstance(
        dynamic.get("bridge_receipt"), Mapping
    ):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "dynamic-v5 load receipt is missing"
        )
    closure = _strict_json(
        producer_runtime_receipt, "producer runtime receipt"
    )
    if closure != raw.get("dynamic_v3_runtime_receipt_closure"):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "producer runtime receipt differs from artifact closure"
        )
    checks = closure.get("cursor_and_lifecycle_checks")
    if closure.get("status") == "COMPLETE_BOUND" and (
        not isinstance(checks, Mapping) or not checks or not all(checks.values())
    ):
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "complete dynamic-v5 closure has failing typed checks"
        )
    epochs = raw.get("epochs")
    if raw.get("epochs_retained") is True:
        if not isinstance(epochs, list) or sum(
            _integer(row.get("substep_count"), "epoch.substep_count", minimum=1)
            for row in epochs
            if isinstance(row, Mapping)
        ) != raw.get("decision_count"):
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                "retained epochs do not close to decision_count"
            )
    content = _mapping(raw.get("content_address"), "content_address")
    expected_content = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _runner_v4.sha256_json(
            {key: value for key, value in raw.items() if key != "content_address"}
        ),
    }
    if dict(content) != expected_content:
        raise HistoricalBehaviorCloneFullRolloutV1Error(
            "behavior-clone rollout content address mismatch"
        )
    return _producer_summary(raw)


class HistoricalBehaviorCloneRunnerV4ExecutorV1:
    """Runner-v4 callable bound to one explicit model and bridge factory."""

    def __init__(
        self,
        model_path: str | Path,
        bridge_factory: Callable[..., Any],
    ) -> None:
        if not callable(bridge_factory):
            raise TypeError("bridge_factory must be callable")
        self.model, self.binding = load_behavior_clone_model_v1(model_path)
        self.policy_descriptor = {
            "policy_id": POLICY_ID,
            "source_sha256": self.binding["model_sha256"],
            "adapter_sha256": self.binding["adapter_contract_sha256"],
            "profile_sha256": self.binding["profile_sha256"],
            "role": "BASELINE_CANDIDATE_NONVOTING",
        }
        self._bridge_factory = bridge_factory

    def __call__(
        self,
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> JSONMap:
        for field in ("policy_id", "source_sha256", "adapter_sha256", "profile_sha256"):
            if policy.get(field) != self.policy_descriptor[field]:
                raise HistoricalBehaviorCloneFullRolloutV1Error(
                    f"runner policy {field} differs from explicit model identity"
                )
        request = _mapping(scenario.get("request"), "scenario.request")
        seed = _integer(group.get("simulator_seed"), "group.simulator_seed", minimum=1)
        load = _runner_v4.bind_dynamic_v5_load(
            request,
            seed,
            _mapping(scenario.get("dynamic_load_config"), "dynamic_load_config"),
        )
        if group.get("dynamic_load_contract_sha256") != load.contract_sha256:
            raise HistoricalBehaviorCloneFullRolloutV1Error(
                "group dynamic-v5 identity differs from scenario"
            )
        bridge = self._bridge_factory(
            group=deepcopy(dict(group)),
            scenario=deepcopy(dict(scenario)),
            policy=deepcopy(dict(policy)),
        )
        artifact = run_behavior_clone_dynamic_v5_rollout_v1(
            bridge,
            request,
            model=self.model,
            seed=seed,
            dynamic_load=load,
        )
        closure = _mapping(
            artifact.get("dynamic_v3_runtime_receipt_closure"),
            "dynamic runtime receipt closure",
        )
        summary = validate_behavior_clone_dynamic_v5_rollout_v1(
            artifact,
            closure,
            load,
            expected_model_binding=self.binding,
        )
        lane = _runner_v4.build_lane_result_v4(
            policy_id=POLICY_ID,
            producer=PRODUCER,
            artifact=artifact,
            request_sha256=load.request_sha256,
            simulator_seed=seed,
            dynamic_load_contract_sha256=load.contract_sha256,
            completion_mode=summary["completion_mode"],
            damage=summary["damage"],
            elapsed_ms=summary["elapsed_ms"],
            completion_criterion_met=summary["completion_criterion_met"],
            offline_score_eligible=False,
            omitted_lane_count=summary["omitted_lane_count"],
            fatal_error_count=summary["fatal_error_count"],
            nonfaithful_reason_counts=summary["nonfaithful_reason_counts"],
            end_state_sha256=summary["end_state_sha256"],
            dynamic_runtime_receipts_complete=summary[
                "dynamic_runtime_receipts_complete"
            ],
            producer_runtime_receipt=closure,
        )
        return {"lane_result": lane}


__all__ = (
    "CONTENT_ADDRESS_SCHEMA",
    "HistoricalBehaviorCloneFullRolloutV1Error",
    "HistoricalBehaviorCloneRunnerV4ExecutorV1",
    "IMPLEMENTATION_REVISION",
    "MODEL_BINDING_SCHEMA",
    "POLICY_ID",
    "PRODUCER",
    "SCHEMA",
    "behavior_clone_lane_contract_v1",
    "build_behavior_clone_policy_descriptor_v1",
    "load_behavior_clone_model_v1",
    "run_behavior_clone_dynamic_v5_rollout_v1",
    "validate_behavior_clone_dynamic_v5_rollout_v1",
)
