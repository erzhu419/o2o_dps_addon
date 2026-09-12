"""Native dynamic-v5 paired-lane adapter for the frozen Cat2_new v6 loop.

The adapter owns no process: one worker passes its private dynamic-v3 bridge
and reuses this callable for its assigned paired groups.  It deliberately emits
the runner-v4 typed lane envelope and keeps the original Cat2_new v6 artifact;
no legacy ``load_dynamic_v1`` or Fury-v2 proposal is synthesized.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import math
from pathlib import Path
from typing import Any, Mapping

from .cat2new_candidate_executor_v4 import (
    DEFAULT_INSTALLED_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SAVEDVARIABLES,
    DEFAULT_SOURCE_ROOT,
)
from .cat2new_candidate_feedback_loop_v6 import (
    ROLLOUT_SCHEMA_V6,
    run_cat2new_feedback_policy_v6,
    validate_cat2new_feedback_rollout_v6,
)
from .cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
    Cat2NewSimulatorRunBindingV5,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    LaneContractV4,
    build_lane_result_v4,
    sha256_json,
    validate_lane_result_v4,
)
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicIdleAdvanceReceiptBatchV3,
    DynamicLoadResultV3,
    ParsedDynamicStateV3,
    _idle_receipt_batch_v3,
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]
PRODUCER = "cat2new_feedback_loop_v6"
OFFLINE_LANE = "OFFLINE_SIMULATOR_ONLY"


class Cat2NewFuryPairedLaneAdapterV3Error(RuntimeError):
    """The paired group cannot be executed by the native Cat2_new lane."""


def cat2new_lane_contract_v3() -> JSONMap:
    """Return the runner-v4 registration contract enabled by this adapter."""

    return LaneContractV4(
        policy_id=CAT2NEW_POLICY_ID,
        producer=PRODUCER,
        artifact_schema=ROLLOUT_SCHEMA_V6,
        source_oracle_status="CAT2NEW_V6_SOURCE_FROZEN",
        ordered_sink_status="CAT2NEW_V5_SIMULATOR_EXECUTOR_AVAILABLE",
        full_policy_status="CAT2NEW_V6_DYNAMIC_V5_ADAPTER_READY",
        dynamic_v5_executable=True,
        blocker_codes=(),
    ).to_wire()


class _V6DynamicV3Facade:
    """Expose v6's actual two-block parser surface over a dynamic-v3 bridge."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def parsed_dynamic_state(
        self, state: Mapping[str, Any]
    ) -> tuple[JSONMap, JSONMap]:
        parsed = self._bridge.parsed_dynamic_state(state)
        if not isinstance(parsed, ParsedDynamicStateV3):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "dynamic-v3 bridge returned an unexpected parsed state"
            )
        return asdict(parsed.team), asdict(parsed.target_semantics)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Cat2NewFuryPairedLaneAdapterV3Error(f"{label} must be an object")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Cat2NewFuryPairedLaneAdapterV3Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise Cat2NewFuryPairedLaneAdapterV3Error(f"{label} must be finite")
    return result


def _policy_elapsed_ms(
    initial_state: Mapping[str, Any], final_state: Mapping[str, Any]
) -> int:
    """Measure only the interval in which the policy can act.

    A dynamic load may advance autonomously before the first policy boundary
    (for example while the target is temporarily unattackable).  The Cat and
    Contra lanes already report ``final_time - root_time``.  Using the Cat2
    final timestamp directly would charge only this lane for pre-policy time
    and make paired DPS denominators unequal.
    """

    start_ms = _integer(initial_state.get("time_ms"), "initial time_ms")
    end_ms = _integer(final_state.get("time_ms"), "final time_ms")
    if end_ms <= start_ms:
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "Cat2_new policy interval must have positive elapsed time"
        )
    return end_ms - start_ms


def _dynamic_runtime_complete(
    bridge: Any,
    rollout: Mapping[str, Any],
    loaded: DynamicLoadResultV3,
    dynamic_load: DynamicRolloutLoadV3,
) -> tuple[bool, JSONMap]:
    idle = bridge.dynamic_idle_advance_receipts(cursor=0)
    if not isinstance(idle, DynamicIdleAdvanceReceiptBatchV3):
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "dynamic-v3 bridge returned an unexpected idle receipt batch"
        )
    final_state = _mapping(
        _mapping(rollout.get("lifecycle_receipt"), "lifecycle_receipt")
        .get("final_state"),
        "final_state",
    )
    final_raw = _mapping(final_state.get("state"), "final_state.state")
    idle_state = _mapping(
        final_raw.get("dynamic_idle_advance"),
        "final_state.state.dynamic_idle_advance",
    )
    terminal = _mapping(
        _mapping(rollout["lifecycle_receipt"], "lifecycle_receipt").get(
            "terminal_dynamic_receipts"
        ),
        "terminal_dynamic_receipts",
    )
    zero_consumption = all(
        row.policy_actions_consumed == 0
        and row.policy_target_selections_consumed == 0
        and row.scheduler_random_draws == 0
        for row in idle.receipts
    )
    finished = final_raw.get("finished") is True
    checks = {
        "v6_terminal_dynamic_receipts_complete": terminal.get("status")
        == "COMPLETE",
        "idle_schema_bound": idle.schema
        == DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
        "idle_config_bound": idle.config_digest
        == loaded.receipt.config_digest,
        "idle_generation_bound": idle.environment_generation
        == loaded.receipt.environment_generation,
        "idle_cursor_closed": idle.cursor == 0
        and idle.next_cursor == len(idle.receipts),
        "idle_consumed_no_policy_or_rng": zero_consumption,
        "terminal_idle_state_bound": idle_state.get("schema")
        == DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
        and idle_state.get("config_digest") == loaded.receipt.config_digest
        and idle_state.get("environment_generation")
        == loaded.receipt.environment_generation,
        "finished_stream_closed": not finished
        or idle.stream_closed is True
        and idle_state.get("stream_closed") is True,
    }
    return all(checks.values()), {
        "schema": "cat2new_dynamic_v5_runtime_closure/v3",
        "status": "COMPLETE" if all(checks.values()) else "INCOMPLETE",
        "dynamic_load_contract_sha256": dynamic_load.contract_sha256,
        "config_digest": loaded.receipt.config_digest,
        "environment_generation": loaded.receipt.environment_generation,
        "checks": checks,
        "idle_advance": asdict(idle),
        "v6_terminal_dynamic_receipts": terminal,
    }


def validate_cat2new_fury_paired_artifact_v3(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    """Validate the producer artifact and the dynamic-v5 evidence it lacks."""

    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    validated = validate_cat2new_feedback_rollout_v6(artifact)
    identity = _mapping(validated.get("identity"), "artifact.identity")
    run_binding = _mapping(identity.get("run_binding"), "artifact run_binding")
    expected_run_binding = {
        "request_sha256": dynamic_load.request_sha256,
        "seed": dynamic_load.seed,
        "dynamic_config_sha256": dynamic_load.config.content_sha256,
        "horizon_end_ms": dynamic_load.config.idle_advance_horizon_ms,
    }
    if any(
        run_binding.get(key) != expected
        for key, expected in expected_run_binding.items()
    ) or identity.get("simulator_request_sha256") != dynamic_load.request_sha256:
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "Cat2_new artifact differs from the paired dynamic-v5 load"
        )
    receipt = dict(_mapping(producer_runtime_receipt, "producer_runtime_receipt"))
    if set(receipt) != {
        "schema",
        "status",
        "dynamic_load_contract_sha256",
        "config_digest",
        "environment_generation",
        "checks",
        "idle_advance",
        "v6_terminal_dynamic_receipts",
    } or receipt.get("schema") != "cat2new_dynamic_v5_runtime_closure/v3":
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "producer runtime receipt field set or schema mismatch"
        )
    if (
        receipt.get("dynamic_load_contract_sha256")
        != dynamic_load.contract_sha256
        or receipt.get("config_digest") != dynamic_load.config.content_sha256
    ):
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "producer runtime receipt differs from its dynamic-v5 load"
        )
    generation = _integer(
        receipt.get("environment_generation"),
        "producer runtime environment_generation",
        minimum=1,
    )
    idle = _idle_receipt_batch_v3(
        _mapping(receipt.get("idle_advance"), "idle_advance"),
        requested_cursor=0,
        generation=generation,
        config=dynamic_load.config,
    )
    lifecycle = _mapping(validated.get("lifecycle_receipt"), "lifecycle_receipt")
    final = _mapping(_mapping(lifecycle.get("final_state"), "final_state").get("state"), "final_state.state")
    idle_state = _mapping(final.get("dynamic_idle_advance"), "dynamic_idle_advance")
    terminal = _mapping(
        lifecycle.get("terminal_dynamic_receipts"),
        "terminal_dynamic_receipts",
    )
    if receipt.get("v6_terminal_dynamic_receipts") != terminal:
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "producer receipt does not preserve the v6 terminal receipts"
        )
    checks = {
        "v6_terminal_dynamic_receipts_complete": terminal.get("status")
        == "COMPLETE",
        "idle_schema_bound": idle.schema
        == DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
        "idle_config_bound": idle.config_digest
        == dynamic_load.config.content_sha256,
        "idle_generation_bound": idle.environment_generation == generation,
        "idle_cursor_closed": idle.cursor == 0
        and idle.next_cursor == len(idle.receipts),
        "idle_consumed_no_policy_or_rng": all(
            row.policy_actions_consumed == 0
            and row.policy_target_selections_consumed == 0
            and row.scheduler_random_draws == 0
            for row in idle.receipts
        ),
        "terminal_idle_state_bound": idle_state.get("schema")
        == DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
        and idle_state.get("config_digest") == dynamic_load.config.content_sha256
        and idle_state.get("environment_generation") == generation,
        "finished_stream_closed": final.get("finished") is not True
        or idle.stream_closed is True
        and idle_state.get("stream_closed") is True,
    }
    if receipt.get("checks") != checks or receipt.get("status") != (
        "COMPLETE" if all(checks.values()) else "INCOMPLETE"
    ):
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "producer runtime closure differs from physical receipts"
        )
    mode, completed, initial, terminal_state = _completion(
        validated,
        horizon_ms=dynamic_load.config.idle_advance_horizon_ms,
    )
    elapsed_ms = _policy_elapsed_ms(initial, terminal_state)
    initial_damage = _number(initial.get("damage_done", 0.0), "initial damage")
    damage = _number(terminal_state.get("damage_done", 0.0), "final damage")
    if initial_damage != 0.0 or damage < initial_damage:
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "candidate damage before policy start or decreasing damage is unsupported"
        )
    omissions = _mapping(
        validated.get("omission_receipt"), "omission_receipt"
    ).get("rows")
    if not isinstance(omissions, list):
        raise Cat2NewFuryPairedLaneAdapterV3Error(
            "Cat2_new omission receipt is malformed"
        )
    runtime_complete = all(checks.values())
    reason_counts = Counter(
        str(row.get("code"))
        for row in omissions
        if isinstance(row, Mapping) and isinstance(row.get("code"), str)
    )
    if not runtime_complete:
        reason_counts["DYNAMIC_V5_RUNTIME_RECEIPTS_INCOMPLETE"] += 1
    omitted = len(omissions) + int(not runtime_complete)
    return {
        "artifact_schema": ROLLOUT_SCHEMA_V6,
        "policy_id": identity.get("policy_id"),
        "completion_mode": mode,
        "damage": damage,
        "elapsed_ms": elapsed_ms,
        "dps": damage * 1000.0 / elapsed_ms,
        "completion_criterion_met": completed,
        "offline_score_eligible": (
            validated.get("offline_score_eligible") is True
            and completed
            and runtime_complete
            and omitted == 0
        ),
        "omitted_lane_count": omitted,
        "fatal_error_count": 0,
        "nonfaithful_reason_counts": dict(sorted(reason_counts.items())),
        "end_state_sha256": str(
            _mapping(
                _mapping(validated["lifecycle_receipt"], "lifecycle_receipt")
                .get("final_state"),
                "final_state",
            ).get("raw_state_sha256")
        ),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def _completion(
    rollout: Mapping[str, Any], *, horizon_ms: int
) -> tuple[str, bool, JSONMap, JSONMap]:
    lifecycle = _mapping(rollout.get("lifecycle_receipt"), "lifecycle_receipt")
    initial_receipt = _mapping(lifecycle.get("initial_state"), "initial_state")
    final_receipt = _mapping(lifecycle.get("final_state"), "final_state")
    initial = _mapping(initial_receipt.get("state"), "initial_state.state")
    final = _mapping(final_receipt.get("state"), "final_state.state")
    team = _mapping(final.get("dynamic_team_background"), "final dynamic team")
    targets = team.get("targets")
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(
            isinstance(target, Mapping) and target.get("dead") is True
            for target in targets
        )
    )
    final_time = _integer(final.get("time_ms"), "final time_ms")
    if final.get("finished") is True and all_dead:
        mode = "ALL_TARGETS_DEAD"
    elif final.get("finished") is True and final_time >= horizon_ms:
        mode = "SCENARIO_HORIZON_REACHED"
    else:
        mode = "INCOMPLETE"
    completed = mode != "INCOMPLETE" and lifecycle.get("terminal_reason") in {
        "ENCOUNTER_FINISHED",
        "HORIZON_REACHED",
    }
    return mode, completed, dict(initial), dict(final)


@dataclass
class Cat2NewFuryPairedLaneAdapterV3:
    """Callable bound to one worker-owned bridge and one candidate policy."""

    bridge: Any
    feedback_policy: Any
    operation_bindings: Cat2NewSimulatorOperationBindingsV5 = field(
        default_factory=Cat2NewSimulatorOperationBindingsV5
    )
    optimizer_parameters: Mapping[str, Any] = field(default_factory=dict)
    historical_prior: Mapping[str, Any] = field(default_factory=dict)
    source_root: str | Path = DEFAULT_SOURCE_ROOT
    manifest_path: str | Path = DEFAULT_MANIFEST
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES
    max_decisions: int = 10_000
    max_advances: int = 100_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.bridge, "load_dynamic_v3", None)):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "bridge must expose load_dynamic_v3"
            )
        if not callable(getattr(self.feedback_policy, "decide", None)):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "feedback_policy must expose decide"
            )
        _integer(self.max_decisions, "max_decisions", minimum=1)
        _integer(self.max_advances, "max_advances", minimum=1)

    def __call__(
        self,
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> JSONMap:
        group_row = _mapping(group, "group")
        scenario_row = _mapping(scenario, "scenario")
        policy_row = _mapping(policy, "policy")
        policy_id = policy_row.get("policy_id")
        if (
            not isinstance(policy_id, str)
            or policy_id != getattr(self.feedback_policy, "policy_id", None)
            or policy_row.get("role") != "CANDIDATE"
        ):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "planned candidate identity differs from the feedback policy"
            )
        request = _mapping(scenario_row.get("request"), "scenario.request")
        config = dynamic_target_semantics_config_from_wire_v3(
            _mapping(
                scenario_row.get("dynamic_load_config"),
                "scenario.dynamic_load_config",
            )
        )
        simulator_seed = _integer(
            group_row.get("simulator_seed"), "group.simulator_seed", minimum=1
        )
        dynamic_load = DynamicRolloutLoadV3.bind(request, simulator_seed, config)
        if scenario_row.get("request_sha256") != dynamic_load.request_sha256:
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "scenario request digest differs from dynamic-v5 load"
            )
        if (
            group_row.get("dynamic_load_contract_sha256")
            != dynamic_load.contract_sha256
        ):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "paired group dynamic-v5 contract digest mismatch"
            )
        horizon_ms = _integer(
            scenario_row.get("horizon_ms"), "scenario.horizon_ms", minimum=1
        )
        if horizon_ms != config.idle_advance_horizon_ms:
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "scenario horizon differs from central idle horizon"
            )

        loaded = self.bridge.load_dynamic_v3(request, simulator_seed, config)
        if not isinstance(loaded, DynamicLoadResultV3):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "load_dynamic_v3 returned an unexpected result"
            )
        load_receipt = asdict(loaded.receipt)
        run_binding = Cat2NewSimulatorRunBindingV5(
            run_id=str(group_row.get("group_id")),
            request_sha256=dynamic_load.request_sha256,
            seed=simulator_seed,
            dynamic_config_sha256=config.content_sha256,
            dynamic_load_receipt_sha256=sha256_json(load_receipt),
            environment_generation=loaded.receipt.environment_generation,
            horizon_end_ms=horizon_ms,
        )
        rollout = run_cat2new_feedback_policy_v6(
            _V6DynamicV3Facade(self.bridge),
            self.feedback_policy,
            simulator_request=request,
            dynamic_load_receipt=load_receipt,
            run_binding=run_binding,
            operation_bindings=self.operation_bindings,
            optimizer_parameters=self.optimizer_parameters,
            historical_prior=self.historical_prior,
            source_root=self.source_root,
            manifest_path=self.manifest_path,
            installed_root=self.installed_root,
            savedvariables_path=self.savedvariables_path,
            max_decisions=self.max_decisions,
            max_advances=self.max_advances,
        )
        artifact = validate_cat2new_feedback_rollout_v6(rollout)
        runtime_complete, runtime_receipt = _dynamic_runtime_complete(
            self.bridge, artifact, loaded, dynamic_load
        )
        mode, completed, initial, final = _completion(
            artifact, horizon_ms=horizon_ms
        )
        elapsed_ms = _policy_elapsed_ms(initial, final)
        initial_damage = _number(initial.get("damage_done", 0.0), "initial damage")
        final_damage = _number(final.get("damage_done", 0.0), "final damage")
        if initial_damage != 0.0 or final_damage < initial_damage:
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "candidate damage before policy start or decreasing damage is unsupported"
            )
        damage = final_damage
        omission_rows = _mapping(
            artifact.get("omission_receipt"), "omission_receipt"
        ).get("rows")
        if not isinstance(omission_rows, list):
            raise Cat2NewFuryPairedLaneAdapterV3Error(
                "Cat2_new omission receipt is malformed"
            )
        reason_counts = Counter(
            str(row.get("code"))
            for row in omission_rows
            if isinstance(row, Mapping) and isinstance(row.get("code"), str)
        )
        if not runtime_complete:
            reason_counts["DYNAMIC_V5_RUNTIME_RECEIPTS_INCOMPLETE"] += 1
        omitted = len(omission_rows) + int(not runtime_complete)
        offline_eligible = (
            artifact.get("offline_score_eligible") is True
            and completed
            and runtime_complete
            and omitted == 0
        )
        lane_result = build_lane_result_v4(
            policy_id=policy_id,
            producer=PRODUCER,
            artifact=artifact,
            request_sha256=dynamic_load.request_sha256,
            simulator_seed=simulator_seed,
            dynamic_load_contract_sha256=dynamic_load.contract_sha256,
            completion_mode=mode,
            damage=damage,
            elapsed_ms=elapsed_ms,
            completion_criterion_met=completed,
            offline_score_eligible=offline_eligible,
            omitted_lane_count=omitted,
            fatal_error_count=0,
            nonfaithful_reason_counts=dict(sorted(reason_counts.items())),
            end_state_sha256=str(
                _mapping(
                    _mapping(artifact["lifecycle_receipt"], "lifecycle_receipt")
                    .get("final_state"),
                    "final_state",
                ).get("raw_state_sha256")
            ),
            dynamic_runtime_receipts_complete=runtime_complete,
            producer_runtime_receipt=runtime_receipt,
            live_fidelity=False,
            comparison_ready=False,
        )
        validated = validate_lane_result_v4(
            lane_result,
            group=group_row,
            scenario=scenario_row,
            policy=policy_row,
            artifact_validator=validate_cat2new_fury_paired_artifact_v3,
        )
        return {"lane_result": validated}


__all__ = (
    "Cat2NewFuryPairedLaneAdapterV3",
    "Cat2NewFuryPairedLaneAdapterV3Error",
    "OFFLINE_LANE",
    "PRODUCER",
    "cat2new_lane_contract_v3",
    "validate_cat2new_fury_paired_artifact_v3",
)
