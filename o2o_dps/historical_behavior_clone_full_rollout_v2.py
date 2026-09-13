"""Minimal dynamic-v5 development rollout for one clone-v2 prototype.

The caller supplies one explicit weighted-prototype model.  The rollout binds
that prototype, the runtime-only bounded-float projection, the adapter
contract, and the ``load_dynamic_v3`` receipt closure.  Runtime evidence says
explicitly whether the bridge was the native subprocess wrapper or an in-Python
simulation fixture.  It is a local development smoke lane only: neither a
completed simulator episode nor a DPS number authorizes comparison until a
source-bound scenario and a separate evidence gate exist.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from . import fury_full_policy_rollout_v2 as _rollout_v2
from . import fury_full_policy_rollout_v5 as _rollout_v5
from . import fury_paired_multiseed_runner_v4 as _runner_v4
from . import historical_behavior_clone_simulator_adapter_v2 as adapter_v2
from . import historical_behavior_clone_v2 as clone_v2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import AvailableAction
from .sim_bridge_dynamic_v3 import (
    DynamicLoadReceiptV3,
    DynamicLoadResultV3,
    SimulatorBridgeDynamicV3,
)


JSONMap = dict[str, Any]
SCHEMA = "historical_behavior_clone_dynamic_v5_rollout/v2"
IMPLEMENTATION_REVISION = "v2.3_receipt_bound_damage_and_epoch_accounting"
PRODUCER = "historical_behavior_clone_full_rollout_v2"
MODEL_BINDING_SCHEMA = "historical_behavior_clone_model_binding/v2"
CONTENT_ADDRESS_SCHEMA = "historical_behavior_clone_rollout_content/v2"
BRIDGE_RUNTIME_EVIDENCE_SCHEMA = "historical_behavior_clone_bridge_runtime/v2"
STATUS = "COMPLETE_DEVELOPMENT_SMOKE_NOT_COMPARISON_AUTHORIZED"
INCOMPLETE_STATUS = "INCOMPLETE_DEVELOPMENT_SMOKE"


CLAIM_BOUNDARY_V2: JSONMap = {
    "development_smoke_only": True,
    "simulator_only": True,
    "source_bound_scenario_complete": False,
    "evidence_gate_passed": False,
    "comparison_authorized": False,
    "same_equipment_matched_seed_comparison_authorized": False,
    "offline_score_eligible": False,
    "voting_eligible": False,
    "true_player_policy_claim": False,
    "deployment_authorized": False,
    "historical_raid_context_reconstructed": False,
    "behavior_loadout_binding": (
        "CROSS_PLAYER_CROSS_BUILD_BEHAVIOR_TRANSPLANT_DEVELOPMENT_ONLY"
    ),
    "same_build_historical_expert_claim": False,
    "episode_decision_combatant_info_prefix_join_complete": False,
    "transport_error_evaluated": False,
    "bundle_v1_historical_lane_admitted": False,
}


class HistoricalBehaviorCloneFullRolloutV2Error(RuntimeError):
    """The explicit model, dynamic rollout, or receipt does not close."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"{label} must be an object"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"{label} must be a finite number"
        )
    return float(value)


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
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"{label} is not strict JSON: {error}"
        ) from error


def _bridge_runtime_evidence_v2(bridge: Any) -> JSONMap:
    native = isinstance(bridge, SimulatorBridgeDynamicV3)
    return {
        "schema": BRIDGE_RUNTIME_EVIDENCE_SCHEMA,
        "runtime_kind": (
            "NATIVE_SUBPROCESS_BRIDGE"
            if native
            else "PYTHON_SIMULATED_BRIDGE"
        ),
        "load_method_invoked": "load_dynamic_v3",
        "native_subprocess_bridge": native,
        "python_simulated_bridge": not native,
        "evidence_scope": (
            "NATIVE_SUBPROCESS_SMOKE_ONLY"
            if native
            else "PYTHON_SIMULATION_ONLY"
        ),
        "comparison_authorized": False,
    }


def _model_binding(
    model: Mapping[str, Any],
    *,
    expected_model_binding: Mapping[str, Any],
) -> JSONMap:
    exact_receipt = _strict_json(
        expected_model_binding, "external exact model validation receipt"
    )
    runtime = adapter_v2.runtime_model_binding_v2(
        model, expected_model_binding=exact_receipt
    )
    adapter_contract = {
        "schema": adapter_v2.ADAPTER_SCHEMA,
        "epoch_schema": adapter_v2.EPOCH_SCHEMA,
        "runtime_projection_schema": runtime["runtime_projection_schema"],
        "runtime_contract": runtime["runtime_contract"],
        "sink_bindings": [
            adapter_v2.ACTION_SINK_BINDINGS_V2[key].to_dict()
            for key in clone_v2.ACTION_KEYS
        ],
        "dynamic_load": "load_dynamic_v3",
    }
    profile = {
        "prototype_id": runtime["prototype_id"],
        "prototype_family": runtime["prototype_family"],
        "source_binding": runtime["source_binding"],
        "uncertainty_contract": runtime["uncertainty_contract"],
    }
    return {
        "schema": MODEL_BINDING_SCHEMA,
        "prototype_id": runtime["prototype_id"],
        "prototype_family": runtime["prototype_family"],
        "policy_id": runtime["policy_id"],
        "model_schema": clone_v2.MODEL_SCHEMA,
        "model_implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
        "model_sha256": runtime["model_sha256"],
        "external_exact_validation_receipt": exact_receipt,
        "runtime_projection_schema": runtime["runtime_projection_schema"],
        "source_binding": runtime["source_binding"],
        "adapter_contract_sha256": _runner_v4.sha256_json(adapter_contract),
        "profile_sha256": _runner_v4.sha256_json(profile),
        "runtime_exact_fraction_decode": False,
        "comparison_authorized": False,
    }


def load_behavior_clone_model_v2(path: str | Path) -> tuple[JSONMap, JSONMap]:
    """Load one explicitly named model and close file/model/source identity."""

    source = Path(path).expanduser().resolve()
    try:
        payload = source.read_bytes()
        value = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"could not read clone-v2 model: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 model must be an object"
        )
    model = _strict_json(value, "clone-v2 model")
    exact_receipt = adapter_v2.exact_validation_receipt_v2(model)
    binding = _model_binding(
        model, expected_model_binding=exact_receipt
    )
    binding["model_file_sha256"] = hashlib.sha256(payload).hexdigest()
    binding["model_file_size_bytes"] = len(payload)
    return model, binding


def build_behavior_clone_policy_descriptor_v2(model_path: str | Path) -> JSONMap:
    _, binding = load_behavior_clone_model_v2(model_path)
    return {
        "policy_id": binding["policy_id"],
        "prototype_id": binding["prototype_id"],
        "source_sha256": binding["model_sha256"],
        "adapter_sha256": binding["adapter_contract_sha256"],
        "profile_sha256": binding["profile_sha256"],
        "role": "DEVELOPMENT_SMOKE_CANDIDATE_NONVOTING",
        "behavior_loadout_binding": (
            "CROSS_PLAYER_CROSS_BUILD_BEHAVIOR_TRANSPLANT_DEVELOPMENT_ONLY"
        ),
        "same_build_historical_expert_claim": False,
        "episode_decision_combatant_info_prefix_join_complete": False,
        "transport_error_evaluated": False,
        "bundle_v1_historical_lane_admitted": False,
        "comparison_authorized": False,
    }


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
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "dynamic-v5 bridge capabilities missing: " + ", ".join(missing)
        )
    return capabilities


def validate_executable_fury_request_v2(
    raid_sim_request: Mapping[str, Any],
) -> JSONMap:
    """Reject Python-only request fixtures before they can crash the Go bridge.

    The Go raid constructor excludes ``ClassUnknown`` players, while dynamic
    target initialization requires a concrete first player.  This Fury lane
    therefore requires the same minimal Warrior identity/spec surface emitted
    by ``build_request_composer_v1`` before launching any subprocess.
    """

    raid = _mapping(raid_sim_request.get("raid"), "request.raid")
    parties = raid.get("parties")
    if not isinstance(parties, list) or not parties:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires raid.parties[0]"
        )
    first_party = _mapping(parties[0], "request.raid.parties[0]")
    players = first_party.get("players")
    if not isinstance(players, list) or not players:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires raid.parties[0].players[0]"
        )
    player = _mapping(players[0], "request.raid.parties[0].players[0]")
    if player.get("class") != "ClassWarrior":
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires class=ClassWarrior; ClassUnknown "
            "is omitted by the Go raid constructor"
        )
    if not isinstance(player.get("warrior"), Mapping):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires a Warrior spec object"
        )
    race = player.get("race")
    if not isinstance(race, str) or not race.startswith("Race") or race == "RaceUnknown":
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires a concrete Warrior race"
        )
    equipment = _mapping(player.get("equipment"), "request player equipment")
    if not isinstance(equipment.get("items"), list):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request requires equipment.items"
        )
    return {
        "schema": "historical_behavior_clone_executable_fury_request/v2",
        "first_player_class": "ClassWarrior",
        "first_player_spec": "warrior",
        "first_player_race": race,
        "go_active_player_precondition_satisfied": True,
        "source_bound_character_claim": False,
    }


def _stance_from_state(
    state: Mapping[str, Any], last_action: str | None
) -> str | None:
    auras = state.get("auras")
    labels = (
        [
            str(row.get("label", "")).casefold()
            for row in auras
            if isinstance(row, Mapping)
        ]
        if isinstance(auras, list)
        else []
    )
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


def _observation_factory(
    *, root_time_ms: int, last_gcd: dict[str, Any]
) -> Callable[[Mapping[str, Any], tuple[AvailableAction, ...], Any], JSONMap]:
    def build(
        state: Mapping[str, Any],
        available: tuple[AvailableAction, ...],
        adapter: adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2,
    ) -> JSONMap:
        del available
        lifecycle = _mapping(
            state.get("dynamic_team_background"), "dynamic_team_background"
        )
        targets = lifecycle.get("targets")
        if not isinstance(targets, list) or any(
            not isinstance(row, Mapping) or not isinstance(row.get("dead"), bool)
            for row in targets
        ):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "dynamic target lifecycle rows are unavailable"
            )
        now = _integer(state.get("time_ms"), "state.time_ms")
        elapsed = now - root_time_ms
        if elapsed < 0:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "simulator time precedes root state"
            )
        last_action = adapter.runtime.last_action_key
        last_lane = (
            None
            if last_action is None
            else clone_v2.ACTION_SPEC_BY_KEY[last_action].lane
        )
        last_gcd_time = last_gcd.get("time_ms")
        return {
            "schema": adapter_v2.OBSERVATION_SCHEMA,
            "simulator_time_ms": now,
            "scenario_elapsed_ms": elapsed,
            "observed_target_count": len(targets),
            "observed_dead_target_count": sum(row["dead"] is True for row in targets),
            "actor_has_last_target": (
                isinstance(state.get("target_index"), int)
                and not isinstance(state.get("target_index"), bool)
                and int(state.get("num_targets", 0)) > 0
            ),
            "last_controllable_action": last_action,
            "last_controllable_lane": last_lane,
            "last_gcd_action_age_ms": (
                None
                if last_gcd_time is None
                else max(0, now - int(last_gcd_time))
            ),
            "observed_stance": _stance_from_state(state, last_action),
            # This is the simulator scenario anchor, not an inferred Chronicle
            # raid prefix or a fabricated left-truncation state.
            "prefix_observation_status": "OBSERVED_FROM_WAVE_ANCHOR",
        }

    return build


def _update_last_gcd(epoch: Mapping[str, Any], last_gcd: dict[str, Any]) -> None:
    for step in epoch.get("steps", []):
        if (
            not isinstance(step, Mapping)
            or step.get("kind") != "ACTION"
            or step.get("client_acceptance") != "ACCEPTED"
        ):
            continue
        binding = step.get("typed_sink_binding")
        state = step.get("final_state")
        if (
            isinstance(binding, Mapping)
            and binding.get("policy_lane") == "gcd"
            and isinstance(state, Mapping)
            and isinstance(state.get("time_ms"), int)
            and not isinstance(state.get("time_ms"), bool)
        ):
            last_gcd.update(
                {"action_key": binding.get("action_key"), "time_ms": state["time_ms"]}
            )


def _step_counters(epoch: Mapping[str, Any]) -> Counter[str]:
    result: Counter[str] = Counter()
    for step in epoch.get("steps", []):
        if not isinstance(step, Mapping):
            continue
        if step.get("kind") == "WAIT_TO_BOUNDARY":
            result["wait_to_boundary"] += 1
        if step.get("residual_survival_outcome") is True:
            result["residual_wait_executed"] += 1
        schedule = step.get("next_delay_outcome")
        if isinstance(schedule, Mapping) and schedule.get("cause") == (
            "RESIDUAL_PRODUCT_LIMIT_SURVIVAL"
        ):
            result["residual_schedule_selected"] += 1
        binding = step.get("typed_sink_binding")
        submitted = step.get("bridge_submission") == "SUBMITTED"
        if (
            submitted
            and isinstance(binding, Mapping)
            and binding.get("policy_lane") == "queue"
        ):
            result["queue_start_proxy_submitted"] += 1
        if submitted and step.get("kind") == "ACTION":
            result["target_role_diagnostic_current_target_sink"] += 1
    return result


def run_behavior_clone_dynamic_v5_rollout_v2(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    expected_model_binding: Mapping[str, Any],
    seed: int,
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute one dynamic-v5 development smoke for one prototype."""

    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    seed = _integer(seed, "seed", minimum=1)
    if dynamic_load.seed != seed:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "dynamic-load seed differs from rollout seed"
        )
    max_decisions = _integer(max_decisions, "max_decisions", minimum=1)
    max_advances = _integer(max_advances, "max_advances", minimum=1)
    if retain_steps is not True:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 development rollouts require retained epochs for "
            "persistent accounting"
        )
    _rollout_v2._validate_request(raid_sim_request)
    request_preflight = validate_executable_fury_request_v2(raid_sim_request)
    request_sha = _runner_v4.sha256_json(raid_sim_request)
    _rollout_v5._validate_dynamic_load_request_v5(
        dynamic_load, raid_sim_request, request_sha256=request_sha
    )
    exact_model_receipt = _strict_json(
        expected_model_binding, "external exact model validation receipt"
    )
    binding = _model_binding(
        model, expected_model_binding=exact_model_receipt
    )
    capabilities = _required_bridge_capabilities(bridge)
    bridge_runtime_evidence = _bridge_runtime_evidence_v2(bridge)

    loaded = bridge.load_dynamic_v3(
        raid_sim_request, seed, dynamic_load.config
    )
    if not isinstance(loaded, DynamicLoadResultV3):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
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
    if _rollout_v5._state_fingerprint_v5(state) != _rollout_v5._state_fingerprint_v5(
        reported
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "state() differs from load_dynamic_v3 state"
        )
    state = reported
    root_state = deepcopy(state)
    root_time = _integer(root_state.get("time_ms"), "root_state.time_ms")
    boundary = dynamic_load.config.idle_advance_horizon_ms
    if root_time >= boundary:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "loaded root state is at or beyond the scenario horizon"
        )
    adapter = adapter_v2.HistoricalBehaviorCloneSimulatorAdapterV2(
        model,
        expected_model_binding=exact_model_receipt,
        simulator_seed=seed,
        start_ms=root_time,
        horizon_ms=boundary - root_time,
    )
    last_gcd: dict[str, Any] = {}
    observation = _observation_factory(root_time_ms=root_time, last_gcd=last_gcd)
    epochs: list[JSONMap] = []
    counters: Counter[str] = Counter()
    epoch_count = decision_count = advance_count = 0
    fatal_errors: list[JSONMap] = []

    try:
        while state.get("finished") is not True:
            if state.get("needs_input") is not True:
                if advance_count >= max_advances:
                    raise HistoricalBehaviorCloneFullRolloutV2Error(
                        f"scenario exceeded max_advances={max_advances}"
                    )
                before = state
                state = deepcopy(dict(bridge.advance()))
                _rollout_v5._validate_dynamic_loaded_state_v5(
                    state, dynamic_load, loaded.receipt
                )
                if _rollout_v5._state_fingerprint_v5(
                    before
                ) == _rollout_v5._state_fingerprint_v5(state):
                    raise HistoricalBehaviorCloneFullRolloutV2Error(
                        "bridge.advance returned an unchanged state"
                    )
                advance_count += 1
                continue
            if decision_count >= max_decisions:
                raise HistoricalBehaviorCloneFullRolloutV2Error(
                    f"scenario exceeded max_decisions={max_decisions}"
                )
            before = deepcopy(state)
            epoch = adapter_v2.run_historical_behavior_clone_epoch_v2(
                adapter,
                bridge,
                state,
                observation,
                attempt_id_prefix=f"clone-v2:{binding['prototype_id']}:epoch-{epoch_count}",
            )
            if (
                epoch.get("schema") != adapter_v2.EPOCH_SCHEMA
                or epoch.get("status") != "COMPLETE_DEVELOPMENT_ONLY"
            ):
                raise HistoricalBehaviorCloneFullRolloutV2Error(
                    "clone-v2 epoch did not complete"
                )
            state = deepcopy(dict(_mapping(epoch.get("final_state"), "epoch.final_state")))
            _rollout_v5._validate_dynamic_loaded_state_v5(
                state, dynamic_load, loaded.receipt
            )
            _update_last_gcd(epoch, last_gcd)
            counters.update(_step_counters(epoch))
            substeps = _integer(
                epoch.get("substep_count"), "epoch.substep_count", minimum=1
            )
            decision_count += substeps
            row = deepcopy(dict(epoch))
            row["epoch_index"] = epoch_count
            row["simulator_state_before"] = before
            row["time_delta_ms"] = int(state["time_ms"]) - int(before["time_ms"])
            row["damage_delta"] = float(state["damage_done"]) - float(
                before["damage_done"]
            )
            epochs.append(row)
            epoch_count += 1
    except Exception as error:
        fatal_errors.append(
            {
                "code": "CLONE_V2_DYNAMIC_V5_EXECUTION_ERROR",
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
    dynamic_load_binding = _rollout_v5._dynamic_load_binding_receipt_v5(
        dynamic_load, loaded.receipt
    )
    if closure.get("status") != "COMPLETE_BOUND":
        fatal_errors.append(
            {
                "code": "DYNAMIC_V3_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE",
                "error_type": None,
                "message": (
                    "dynamic-v3 target, damage, idle, or lifecycle receipts did "
                    "not close"
                ),
            }
        )
    complete = completion["criterion_met"] is True and not fatal_errors
    elapsed = int(final_state["time_ms"]) - root_time
    damage = float(final_state["damage_done"]) - float(root_state["damage_done"])
    if elapsed <= 0:
        detail = fatal_errors[0]["message"] if fatal_errors else "no elapsed time"
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"clone-v2 rollout stopped before positive elapsed time: {detail}"
        )
    artifact: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS if complete else INCOMPLETE_STATUS,
        "producer": PRODUCER,
        "policy_id": binding["policy_id"],
        "prototype_id": binding["prototype_id"],
        "prototype_family": binding["prototype_family"],
        "model_binding": deepcopy(binding),
        "dynamic_load_binding": deepcopy(dynamic_load_binding),
        "bridge_runtime_evidence": deepcopy(bridge_runtime_evidence),
        "bridge_capabilities": capabilities,
        "executable_request_preflight": request_preflight,
        "runtime_contract": deepcopy(
            adapter_v2.EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2
        ),
        "root_state": root_state,
        "final_state": final_state,
        "configured_completion": completion,
        "scenario_complete": complete,
        "elapsed_ms": elapsed,
        "damage_delta": damage,
        "diagnostic_dps": damage * 1000.0 / elapsed if complete else None,
        "decision_count": decision_count,
        "epoch_count": epoch_count,
        "residual_survival_schedule_count": counters[
            "residual_schedule_selected"
        ],
        "residual_survival_wait_executed_count": counters[
            "residual_wait_executed"
        ],
        "wait_to_boundary_count": counters["wait_to_boundary"],
        "queue_start_proxy_submission_count": counters[
            "queue_start_proxy_submitted"
        ],
        "target_role_diagnostic_current_target_sink_count": counters[
            "target_role_diagnostic_current_target_sink"
        ],
        "epochs_retained": True,
        "epochs": epochs,
        "fatal_errors": fatal_errors,
        "dynamic_v3_runtime_receipt_closure": deepcopy(closure),
        "claim_boundary": deepcopy(CLAIM_BOUNDARY_V2),
        "version_isolation": {
            "explicit_model_required": True,
            "runner_registry_modified": False,
            "comparison_lane_registered": False,
            "bridge_load_method_invoked": "load_dynamic_v3",
            "native_subprocess_bridge": bridge_runtime_evidence[
                "native_subprocess_bridge"
            ],
            "python_simulated_bridge": bridge_runtime_evidence[
                "python_simulated_bridge"
            ],
        },
    }
    artifact["content_address"] = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _runner_v4.sha256_json(artifact),
    }
    validate_behavior_clone_dynamic_v5_rollout_v2(
        artifact,
        closure,
        dynamic_load,
        expected_model_binding=binding,
        expected_exact_model_validation_receipt=exact_model_receipt,
        loaded_receipt=loaded.receipt,
        expected_bridge_runtime_evidence=bridge_runtime_evidence,
    )
    return artifact


def validate_behavior_clone_dynamic_v5_rollout_v2(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
    *,
    expected_model_binding: Mapping[str, Any],
    expected_exact_model_validation_receipt: Mapping[str, Any],
    expected_bridge_runtime_evidence: Mapping[str, Any],
    loaded_receipt: DynamicLoadReceiptV3 | None = None,
    expected_dynamic_load_binding: Mapping[str, Any] | None = None,
) -> JSONMap:
    if (loaded_receipt is None) == (expected_dynamic_load_binding is None):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "provide exactly one of loaded_receipt or expected_dynamic_load_binding"
        )
    # These expected values are trusted caller-supplied validation context.
    # Python object identity is not provenance and is therefore deliberately
    # irrelevant; the checks below compare their complete strict-JSON values.
    raw = _strict_json(artifact, "clone-v2 rollout")
    if not isinstance(raw, dict):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 rollout must be an object"
        )
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("producer") != PRODUCER
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 rollout identity differs"
        )
    binding = _mapping(raw.get("model_binding"), "model_binding")
    if (
        raw.get("prototype_id") != binding.get("prototype_id")
        or raw.get("policy_id") != binding.get("policy_id")
        or raw.get("prototype_family") != binding.get("prototype_family")
        or binding.get("comparison_authorized") is not False
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "rollout prototype/model identity differs"
        )
    expected_model = _strict_json(
        expected_model_binding, "expected model binding"
    )
    if not isinstance(expected_model, dict) or dict(binding) != expected_model:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "rollout model differs from explicit model binding"
        )
    exact_receipt = _strict_json(
        expected_exact_model_validation_receipt,
        "expected exact model validation receipt",
    )
    if binding.get("external_exact_validation_receipt") != exact_receipt:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "rollout model binding differs from exact validation receipt"
        )
    if _mapping(raw.get("claim_boundary"), "claim_boundary") != CLAIM_BOUNDARY_V2:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 claim boundary differs"
        )
    if raw.get("runtime_contract") != (
        adapter_v2.EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 runtime reconstruction contract differs"
        )
    preflight = _mapping(
        raw.get("executable_request_preflight"), "executable_request_preflight"
    )
    if (
        preflight.get("schema")
        != "historical_behavior_clone_executable_fury_request/v2"
        or preflight.get("go_active_player_precondition_satisfied") is not True
        or preflight.get("source_bound_character_claim") is not False
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "executable Fury request preflight differs"
        )
    bridge_evidence = _mapping(
        raw.get("bridge_runtime_evidence"), "bridge_runtime_evidence"
    )
    expected_bridge_evidence = _strict_json(
        expected_bridge_runtime_evidence, "expected bridge runtime evidence"
    )
    if (
        not isinstance(expected_bridge_evidence, dict)
        or dict(bridge_evidence) != expected_bridge_evidence
        or bridge_evidence.get("schema") != BRIDGE_RUNTIME_EVIDENCE_SCHEMA
        or bridge_evidence.get("load_method_invoked") != "load_dynamic_v3"
        or bridge_evidence.get("comparison_authorized") is not False
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "bridge runtime evidence differs from producer expectation"
        )
    native_bridge = bridge_evidence.get("native_subprocess_bridge")
    simulated_bridge = bridge_evidence.get("python_simulated_bridge")
    expected_runtime_tuple = (
        (
            "NATIVE_SUBPROCESS_BRIDGE",
            True,
            False,
            "NATIVE_SUBPROCESS_SMOKE_ONLY",
        )
        if native_bridge is True
        else (
            "PYTHON_SIMULATED_BRIDGE",
            False,
            True,
            "PYTHON_SIMULATION_ONLY",
        )
    )
    if (
        bridge_evidence.get("runtime_kind"),
        native_bridge,
        simulated_bridge,
        bridge_evidence.get("evidence_scope"),
    ) != expected_runtime_tuple:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "bridge runtime evidence classification differs"
        )
    closure = _strict_json(producer_runtime_receipt, "producer runtime receipt")
    if closure != raw.get("dynamic_v3_runtime_receipt_closure"):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "producer runtime receipt differs from artifact closure"
        )
    checks = closure.get("cursor_and_lifecycle_checks")
    if closure.get("status") == "COMPLETE_BOUND" and (
        not isinstance(checks, Mapping) or not checks or not all(checks.values())
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "complete dynamic-v5 closure has failing typed checks"
        )
    fatal = raw.get("fatal_errors")
    if not isinstance(fatal, list):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "fatal_errors must be an array"
        )
    dynamic = _mapping(raw.get("dynamic_load_binding"), "dynamic_load_binding")
    if loaded_receipt is not None:
        if not isinstance(loaded_receipt, DynamicLoadReceiptV3):
            raise TypeError("loaded_receipt must be DynamicLoadReceiptV3")
        expected_dynamic = _rollout_v5._dynamic_load_binding_receipt_v5(
            dynamic_load, loaded_receipt
        )
    else:
        expected_dynamic = _strict_json(
            expected_dynamic_load_binding, "expected dynamic load binding"
        )
        if not isinstance(expected_dynamic, dict):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "expected dynamic load binding must be an object"
            )
    static_dynamic = _rollout_v5._dynamic_load_binding_receipt_v5(
        dynamic_load, None
    )
    if set(expected_dynamic) != set(static_dynamic):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "expected dynamic-v5 binding field set differs"
        )
    for key, value in static_dynamic.items():
        if key not in {"load_succeeded", "bridge_receipt"} and (
            expected_dynamic.get(key) != value
        ):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                f"dynamic-v5 binding differs at {key}"
            )
    if dict(dynamic) != expected_dynamic:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "dynamic-v5 binding differs from producer expectation"
        )
    expected_load_receipt = _mapping(
        expected_dynamic.get("bridge_receipt"),
        "expected dynamic load bridge receipt",
    )
    if expected_dynamic.get("load_succeeded") is not True:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "dynamic-v5 load did not succeed"
        )
    if (
        closure.get("schema") != _rollout_v5.RUNTIME_RECEIPT_SCHEMA_V5
        or closure.get("config_digest") != expected_dynamic.get("config_digest")
        or closure.get("environment_generation")
        != expected_load_receipt.get("environment_generation")
        or closure.get("historical_truth") is not False
        or closure.get("comparison_eligible") is not False
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "producer runtime receipt is not bound to the expected dynamic load"
        )
    try:
        bound_load_receipt = (
            loaded_receipt
            if loaded_receipt is not None
            else DynamicLoadReceiptV3(**dict(expected_load_receipt))
        )
    except (TypeError, ValueError) as error:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"dynamic load receipt is malformed: {error}"
        ) from error
    root_state = _mapping(raw.get("root_state"), "root_state")
    final_state = _mapping(raw.get("final_state"), "final_state")
    try:
        _rollout_v5._validate_dynamic_loaded_state_v5(
            root_state, dynamic_load, bound_load_receipt
        )
        _rollout_v5._validate_dynamic_loaded_state_v5(
            final_state, dynamic_load, bound_load_receipt
        )
    except Exception as error:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            f"persisted rollout state does not match the dynamic load: {error}"
        ) from error
    expected_completion = _rollout_v5._completion_receipt_v5(
        {}, root_state, final_state, dynamic_load=dynamic_load
    )
    if raw.get("configured_completion") != expected_completion:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "configured completion differs from persisted root/final states"
        )
    expected_complete = (
        expected_completion.get("criterion_met") is True
        and closure.get("status") == "COMPLETE_BOUND"
        and not fatal
    )
    if raw.get("scenario_complete") is not expected_complete:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "scenario_complete differs from completion and receipt closure"
        )
    expected_status = STATUS if expected_complete else INCOMPLETE_STATUS
    if raw.get("status") != expected_status:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "rollout status differs from completion evidence"
        )
    terminal_lifecycle = _mapping(
        closure.get("terminal_lifecycle"), "terminal lifecycle receipt"
    )
    root_time_ms = _integer(root_state.get("time_ms"), "root_state.time_ms")
    final_time_ms = _integer(final_state.get("time_ms"), "final_state.time_ms")
    root_idle = _mapping(
        root_state.get("dynamic_idle_advance"),
        "root_state.dynamic_idle_advance",
    )
    idle_batch = _mapping(closure.get("idle_advance"), "idle advance receipt batch")
    idle_receipts = idle_batch.get("receipts")
    if not isinstance(idle_receipts, list) or not all(
        isinstance(receipt, Mapping) for receipt in idle_receipts
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "idle advance receipts must be an array of objects"
        )
    root_idle_count = _integer(
        root_idle.get("receipts_processed"), "root idle receipts_processed"
    )
    if root_idle_count > len(idle_receipts):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "root idle receipt cursor exceeds runtime receipt batch"
        )
    root_idle_prefix = idle_receipts[:root_idle_count]
    expected_root_time_ms = (
        _integer(root_idle_prefix[-1].get("end_time_ms"), "root idle end_time_ms")
        if root_idle_prefix
        else 0
    )
    expected_root_idle_total = sum(
        _integer(
            receipt.get("auto_advanced_duration_ms"),
            "root idle auto_advanced_duration_ms",
        )
        for receipt in root_idle_prefix
    )
    if (
        root_time_ms != expected_root_time_ms
        or _integer(
            root_idle.get("total_auto_advanced_ms"),
            "root idle total_auto_advanced_ms",
        )
        != expected_root_idle_total
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "root time differs from dynamic idle receipt prefix"
        )
    if expected_completion.get("criterion_met") is True:
        terminal_reason = expected_completion.get("terminal_reason")
        if terminal_reason == "ALL_TARGETS_DEAD":
            terminal_targets = terminal_lifecycle.get("targets")
            if not isinstance(terminal_targets, list) or not terminal_targets:
                raise HistoricalBehaviorCloneFullRolloutV2Error(
                    "terminal lifecycle lacks dead target receipts"
                )
            death_times = [
                _integer(target.get("death_time_ms"), "target death_time_ms")
                for target in terminal_targets
                if isinstance(target, Mapping) and target.get("dead") is True
            ]
            if len(death_times) != len(terminal_targets):
                raise HistoricalBehaviorCloneFullRolloutV2Error(
                    "all-targets-dead completion lacks exact death times"
                )
            expected_final_time_ms = max(death_times)
        elif terminal_reason == "SCENARIO_HORIZON_REACHED":
            expected_final_time_ms = dynamic_load.config.idle_advance_horizon_ms
        else:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "configured completion terminal reason differs"
            )
        if final_time_ms != expected_final_time_ms:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "final time differs from terminal lifecycle evidence"
            )
    elapsed_ms = final_time_ms - root_time_ms
    if elapsed_ms <= 0 or raw.get("elapsed_ms") != elapsed_ms:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "elapsed_ms differs from persisted root/final states"
        )
    root_damage = _finite_number(root_state.get("damage_done"), "root_state.damage_done")
    final_damage = _finite_number(
        final_state.get("damage_done"), "final_state.damage_done"
    )
    damage_delta = final_damage - root_damage
    if damage_delta < 0:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "persisted rollout damage cannot decrease"
        )
    candidate_damage = _mapping(
        closure.get("candidate_damage"), "candidate damage receipt batch"
    )
    candidate_receipts = candidate_damage.get("receipts")
    if not isinstance(candidate_receipts, list):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "candidate damage receipts must be an array"
        )
    receipt_damage = sum(
        _finite_number(receipt.get("applied_damage"), "candidate applied_damage")
        for receipt in candidate_receipts
        if isinstance(receipt, Mapping)
    )
    if len(candidate_receipts) != sum(
        isinstance(receipt, Mapping) for receipt in candidate_receipts
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "candidate damage receipt must be an object"
        )
    lifecycle_damage = _finite_number(
        terminal_lifecycle.get("simulated_damage_applied"),
        "terminal lifecycle simulated_damage_applied",
    )
    root_lifecycle = _mapping(
        root_state.get("dynamic_team_background"),
        "root_state.dynamic_team_background",
    )
    final_lifecycle = _mapping(
        final_state.get("dynamic_team_background"),
        "final_state.dynamic_team_background",
    )
    root_lifecycle_damage = _finite_number(
        root_lifecycle.get("simulated_damage_applied"),
        "root state simulated_damage_applied",
    )
    final_lifecycle_damage = _finite_number(
        final_lifecycle.get("simulated_damage_applied"),
        "final state simulated_damage_applied",
    )
    tolerance = max(1e-7, abs(lifecycle_damage) * 1e-12)
    if not math.isclose(receipt_damage, lifecycle_damage, rel_tol=0.0, abs_tol=tolerance):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "candidate damage receipts differ from terminal lifecycle"
        )
    if not math.isclose(
        final_lifecycle_damage - root_lifecycle_damage,
        lifecycle_damage,
        rel_tol=0.0,
        abs_tol=tolerance,
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "persisted dynamic lifecycle damage differs from runtime receipts"
        )
    if not math.isclose(
        damage_delta, lifecycle_damage, rel_tol=0.0, abs_tol=tolerance
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "persisted damage_done differs from runtime damage receipts"
        )
    observed_damage = _finite_number(raw.get("damage_delta"), "damage_delta")
    expected_dps = damage_delta * 1000.0 / elapsed_ms
    if not math.isclose(observed_damage, damage_delta, rel_tol=0.0, abs_tol=1e-9):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "damage_delta differs from persisted root/final states"
        )
    if expected_complete:
        observed_dps = _finite_number(raw.get("diagnostic_dps"), "diagnostic_dps")
        if not math.isclose(
            observed_dps, expected_dps, rel_tol=0.0, abs_tol=1e-9
        ):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "diagnostic_dps differs from persisted damage and elapsed time"
            )
    elif raw.get("diagnostic_dps") is not None:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "incomplete rollout must not publish diagnostic_dps"
        )
    version_isolation = _mapping(
        raw.get("version_isolation"), "version_isolation"
    )
    if (
        version_isolation.get("bridge_load_method_invoked") != "load_dynamic_v3"
        or version_isolation.get("native_subprocess_bridge") is not native_bridge
        or version_isolation.get("python_simulated_bridge") is not simulated_bridge
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "rollout bridge evidence differs from version isolation receipt"
        )
    epochs = raw.get("epochs")
    if raw.get("epochs_retained") is not True or not isinstance(epochs, list):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 rollout must contain its retained decision epoch list"
        )
    decision_count = 0
    counted = Counter()
    retained_attempt_ids: set[str] = set()
    for epoch_index, epoch in enumerate(epochs):
        if (
            not isinstance(epoch, Mapping)
            or epoch.get("prototype_id") != raw.get("prototype_id")
            or epoch.get("policy_id") != raw.get("policy_id")
        ):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "retained epoch index or prototype identity differs"
            )
        if _integer(epoch.get("epoch_index"), "epoch.epoch_index") != epoch_index:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "retained epoch index or prototype identity differs"
            )
        steps = epoch.get("steps")
        substeps = _integer(
            epoch.get("substep_count"), "epoch.substep_count", minimum=1
        )
        if not isinstance(steps, list) or len(steps) != substeps:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "retained epoch steps do not close to substep_count"
            )
        decision_count += substeps
        before = _mapping(
            epoch.get("simulator_state_before"), "epoch.simulator_state_before"
        )
        after = _mapping(epoch.get("final_state"), "epoch.final_state")
        try:
            _rollout_v5._validate_dynamic_loaded_state_v5(
                before, dynamic_load, bound_load_receipt
            )
            _rollout_v5._validate_dynamic_loaded_state_v5(
                after, dynamic_load, bound_load_receipt
            )
        except Exception as error:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                f"retained epoch state does not match the dynamic load: {error}"
            ) from error
        epoch_elapsed = _integer(after.get("time_ms"), "epoch.final_state.time_ms") - _integer(
            before.get("time_ms"), "epoch.simulator_state_before.time_ms"
        )
        if _integer(epoch.get("time_delta_ms"), "epoch.time_delta_ms") != epoch_elapsed:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "retained epoch time_delta_ms differs from its states"
            )
        epoch_damage = _finite_number(
            after.get("damage_done"), "epoch.final_state.damage_done"
        ) - _finite_number(
            before.get("damage_done"), "epoch.simulator_state_before.damage_done"
        )
        if not math.isclose(
            _finite_number(epoch.get("damage_delta"), "epoch.damage_delta"),
            epoch_damage,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "retained epoch damage_delta differs from its states"
            )
        for step in steps:
            if (
                not isinstance(step, Mapping)
                or step.get("prototype_id") != raw.get("prototype_id")
                or step.get("policy_id") != raw.get("policy_id")
            ):
                raise HistoricalBehaviorCloneFullRolloutV2Error(
                    "retained execution receipt lost prototype identity"
                )
            attempt_id = step.get("attempt_id")
            if (
                step.get("kind") == "ACTION"
                and step.get("bridge_submission") == "SUBMITTED"
                and attempt_id is not None
            ):
                if not isinstance(attempt_id, str) or not attempt_id:
                    raise HistoricalBehaviorCloneFullRolloutV2Error(
                        "retained action attempt_id is invalid"
                    )
                if attempt_id in retained_attempt_ids:
                    raise HistoricalBehaviorCloneFullRolloutV2Error(
                        "retained action attempt_id is duplicated"
                    )
                retained_attempt_ids.add(attempt_id)
        counted.update(_step_counters(epoch))
    observed_epoch_count = _integer(raw.get("epoch_count"), "epoch_count")
    observed_decision_count = _integer(raw.get("decision_count"), "decision_count")
    if observed_epoch_count != len(epochs) or observed_decision_count != decision_count:
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "retained epochs do not close to epoch/decision counts"
        )
    candidate_attempt_ids: set[str] = set()
    for receipt in candidate_receipts:
        attempt_id = receipt.get("attempt_id")
        if attempt_id is None:
            continue
        if not isinstance(attempt_id, str) or not attempt_id:
            raise HistoricalBehaviorCloneFullRolloutV2Error(
                "candidate damage receipt attempt_id is invalid"
            )
        candidate_attempt_ids.add(attempt_id)
    if not candidate_attempt_ids.issubset(retained_attempt_ids) or (
        closure.get("status") == "COMPLETE_BOUND"
        and candidate_attempt_ids != retained_attempt_ids
    ):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "retained action attempts differ from candidate damage receipts"
        )
    expected_counts = {
        "residual_schedule_selected": _integer(
            raw.get("residual_survival_schedule_count"),
            "residual_survival_schedule_count",
        ),
        "residual_wait_executed": _integer(
            raw.get("residual_survival_wait_executed_count"),
            "residual_survival_wait_executed_count",
        ),
        "wait_to_boundary": _integer(
            raw.get("wait_to_boundary_count"), "wait_to_boundary_count"
        ),
        "queue_start_proxy_submitted": _integer(
            raw.get("queue_start_proxy_submission_count"),
            "queue_start_proxy_submission_count",
        ),
        "target_role_diagnostic_current_target_sink": _integer(
            raw.get("target_role_diagnostic_current_target_sink_count"),
            "target_role_diagnostic_current_target_sink_count",
        ),
    }
    if any(counted[key] != value for key, value in expected_counts.items()):
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "retained epochs do not close to runtime outcome counters"
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
        raise HistoricalBehaviorCloneFullRolloutV2Error(
            "clone-v2 rollout content address differs"
        )
    return raw


__all__ = [
    "BRIDGE_RUNTIME_EVIDENCE_SCHEMA",
    "CLAIM_BOUNDARY_V2",
    "CONTENT_ADDRESS_SCHEMA",
    "HistoricalBehaviorCloneFullRolloutV2Error",
    "IMPLEMENTATION_REVISION",
    "INCOMPLETE_STATUS",
    "MODEL_BINDING_SCHEMA",
    "PRODUCER",
    "SCHEMA",
    "STATUS",
    "build_behavior_clone_policy_descriptor_v2",
    "load_behavior_clone_model_v2",
    "run_behavior_clone_dynamic_v5_rollout_v2",
    "validate_executable_fury_request_v2",
    "validate_behavior_clone_dynamic_v5_rollout_v2",
]
