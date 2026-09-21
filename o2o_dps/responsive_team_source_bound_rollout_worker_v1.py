"""Compose one source-bound dynamic-v4 rollout with a responsive team model.

This module is deliberately a thin worker/session layer.  It opens the
node-local SQLite view of one already-selected B/C/D model, keeps the atomic
``LoadedResponsiveTeammateModelV1`` binding intact, and gives the rollout a
bridge facade.  The facade creates ``ResponsiveTeamBridgeAdapterV1`` only
after ``load_dynamic_v4`` supplies the authoritative environment generation,
then consumes every ready teammate wake before returning control to the
candidate policy.

The worker has development wire-smoke authority only.  In particular, a
missing real prefix HP registry cannot be turned into comparison or training
authority by this composition layer.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .chronicle_external_teammate_response_model_v1 import (
    ABLATION_D,
    DynamicTeamRuntimeV1,
)
from .policy_observation_causal_projection_v1 import (
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
)
from .responsive_team_bridge_adapter_v1 import (
    CausalBranchBindingV1,
    LoadedResponsiveTeammateModelV1,
    ResponsiveTeamBridgeAdapterV1,
)
from .responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from .responsive_team_runtime_store_v1 import (
    open_responsive_team_runtime_store_v1,
)
from .sim_bridge_dynamic_v4 import (
    DynamicLoadResultV4,
    DynamicTargetSemanticsConfigV4,
)


JSONMap = dict[str, Any]
T = TypeVar("T")

WORKER_SCHEMA = "o2o_responsive_team_source_bound_rollout_worker/v1"
IMPLEMENTATION_REVISION = "v1.2_prefix_only_identity_ablation_d_driver"
DEVELOPMENT_WIRE_SMOKE_SCOPE = "DEVELOPMENT_ONLY_SOURCE_BOUND_WIRE_SMOKE"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ResponsiveTeamSourceBoundRolloutWorkerV1Error(RuntimeError):
    """The development-only worker binding or drive order is invalid."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _sha256(value: Any, label: str) -> str:
    result = _text(value, label)
    if _SHA256.fullmatch(result) is None:
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be lowercase SHA-256"
        )
    return result


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be numeric"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be finite"
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"{label} must be an object"
        )
    return value


def responsive_team_source_prefix_content_sha256_v1(
    *,
    runtime: DynamicTeamRuntimeV1,
    candidate_actor_guid: str,
    target_guid_by_index: Sequence[str],
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    current_source_evidence: Mapping[str, Any],
) -> str:
    """Hash the actual causal-prefix state supplied to one rollout worker."""

    if not isinstance(runtime, DynamicTeamRuntimeV1):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            "prefix runtime must be DynamicTeamRuntimeV1"
        )
    if not isinstance(target_introduction_registry, TargetIntroductionRegistryV1):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            "prefix target-introduction registry has the wrong type"
        )
    if not isinstance(target_health_prefix_registry, TargetHealthPrefixRegistryV1):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            "prefix target-health registry has the wrong type"
        )
    targets = tuple(
        _text(value, "prefix target GUID") for value in target_guid_by_index
    )
    candidate = _text(candidate_actor_guid, "prefix candidate actor GUID")
    if candidate not in runtime.actors:
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            "prefix candidate actor is absent from runtime"
        )
    if not targets or len(set(targets)) != len(targets):
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            "prefix target GUID registry must be nonempty and unique"
        )
    actors = []
    for guid in sorted(runtime.actors):
        actor = runtime.actors[guid]
        actors.append(
            {
                "player_guid": guid,
                "metadata": dict(actor.metadata),
                "current_target_guid": actor.current_target_guid,
                "live_prefix_snapshot": runtime.snapshot_for_actor(guid),
            }
        )
    document = {
        "schema": f"{WORKER_SCHEMA}/source_prefix_identity",
        "runtime": {
            "time_ms": runtime.time_ms,
            "kill_clock_ms": runtime.kill_clock_ms,
            "actors": actors,
            "target_health_by_guid": [
                {"target_guid": guid, "current_health": runtime.health[guid]}
                for guid in sorted(runtime.health)
            ],
        },
        "candidate_actor_guid": candidate,
        "target_guid_by_index": list(targets),
        "target_introduction_registry": target_introduction_registry.to_wire(),
        "target_health_prefix_registry": target_health_prefix_registry.to_wire(),
        "current_source_evidence": dict(
            _mapping(current_source_evidence, "current source evidence")
        ),
    }
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
            f"source prefix is not strict JSON: {error}"
        ) from error
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ResponsiveTeamRuntimeStoreBindingV1:
    """Node-local selected-model store and its immutable identities."""

    path: str | Path
    expected_result_content_sha256: str
    expected_model_content_sha256: str
    variant_id: str
    current_source: CurrentSourceDeclarationV1

    def __post_init__(self) -> None:
        _sha256(
            self.expected_result_content_sha256,
            "expected result content SHA-256",
        )
        _sha256(
            self.expected_model_content_sha256,
            "expected model content SHA-256",
        )
        _text(self.variant_id, "variant_id")
        if not isinstance(self.current_source, CurrentSourceDeclarationV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "current_source must be CurrentSourceDeclarationV1"
            )


@dataclass(frozen=True)
class ResponsiveTeamBranchPlanV1:
    """Branch fields known before the bridge assigns an environment generation."""

    pair_id: str
    branch_id: str
    candidate_suffix_id: str
    prefix_content_sha256: str
    simulator_seed: int
    teammate_seed: int
    dynamic_config_sha256: str

    def __post_init__(self) -> None:
        _text(self.pair_id, "pair_id")
        _text(self.branch_id, "branch_id")
        _text(self.candidate_suffix_id, "candidate_suffix_id")
        _sha256(self.prefix_content_sha256, "prefix_content_sha256")
        _sha256(self.dynamic_config_sha256, "dynamic_config_sha256")
        _integer(self.simulator_seed, "simulator_seed")
        _integer(self.teammate_seed, "teammate_seed")

    def bind(
        self,
        *,
        environment_generation: int,
        loaded_model: LoadedResponsiveTeammateModelV1,
    ) -> CausalBranchBindingV1:
        return CausalBranchBindingV1(
            pair_id=self.pair_id,
            branch_id=self.branch_id,
            candidate_suffix_id=self.candidate_suffix_id,
            prefix_content_sha256=self.prefix_content_sha256,
            simulator_seed=self.simulator_seed,
            teammate_seed=self.teammate_seed,
            environment_generation=_integer(
                environment_generation, "environment_generation", minimum=1
            ),
            dynamic_config_sha256=self.dynamic_config_sha256,
            model_provenance_sha256=loaded_model.provenance.content_sha256,
        )


class ResponsiveTeamDrivenBridgeV1:
    """Proxy a dynamic-v4 bridge and service responsive wakes on ``advance``."""

    def __init__(
        self,
        *,
        bridge: Any,
        loaded_model: LoadedResponsiveTeammateModelV1,
        runtime: DynamicTeamRuntimeV1,
        candidate_actor_guid: str,
        target_guid_by_index: Sequence[str],
        target_introduction_registry: TargetIntroductionRegistryV1,
        target_health_prefix_registry: TargetHealthPrefixRegistryV1,
        branch_plan: ResponsiveTeamBranchPlanV1,
        max_responsive_events: int,
    ) -> None:
        if not isinstance(loaded_model, LoadedResponsiveTeammateModelV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "worker requires the runtime store's atomic loaded-model wrapper"
            )
        if loaded_model.provenance.variant_id != ABLATION_D:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "source-bound worker requires ablation D because candidate action "
                "intensity receipts are not available for B/C"
            )
        if not isinstance(runtime, DynamicTeamRuntimeV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "runtime must be DynamicTeamRuntimeV1"
            )
        if not isinstance(
            target_introduction_registry, TargetIntroductionRegistryV1
        ):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "target introduction registry must be explicit prefix evidence"
            )
        if not isinstance(target_health_prefix_registry, TargetHealthPrefixRegistryV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "target health registry must be explicit prefix evidence"
            )
        if not isinstance(branch_plan, ResponsiveTeamBranchPlanV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "branch_plan must be ResponsiveTeamBranchPlanV1"
            )
        targets = tuple(_text(value, "target guid") for value in target_guid_by_index)
        if not targets or len(set(targets)) != len(targets):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "target GUID registry must be nonempty and unique"
            )
        if set(targets) != set(runtime.health):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "target GUID registry differs from the responsive runtime"
            )
        candidate = _text(candidate_actor_guid, "candidate_actor_guid")
        if candidate not in runtime.actors or len(runtime.actors) < 2:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive runtime requires the candidate and at least one teammate"
            )
        introductions = {
            row.simulator_target_index: row
            for row in target_introduction_registry.targets
        }
        health = {
            row.simulator_target_index: row
            for row in target_health_prefix_registry.targets
        }
        expected_indexes = set(range(len(targets)))
        if set(introductions) != expected_indexes or set(health) != expected_indexes:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "prefix target registries must cover every simulator target exactly"
            )
        max_events = _integer(
            max_responsive_events, "max_responsive_events", minimum=1
        )

        self._bridge = bridge
        self._loaded_model = loaded_model
        self._runtime = runtime
        self._candidate_actor_guid = candidate
        self._target_guid_by_index = targets
        self._target_introduction_registry = target_introduction_registry
        self._target_health_prefix_registry = target_health_prefix_registry
        self._branch_plan = branch_plan
        self._max_responsive_events = max_events
        self.source_prefix_content_sha256: str | None = None
        self.adapter: ResponsiveTeamBridgeAdapterV1 | None = None
        self.branch: CausalBranchBindingV1 | None = None
        self.initial_wake: JSONMap | None = None
        self.responsive_event_count = 0
        self.applied_damage = 0.0
        self.status_counts: dict[str, int] = {}
        self.first_wire_receipt: JSONMap | None = None
        self.last_wire_receipt: JSONMap | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def load_dynamic_v4(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV4,
    ) -> DynamicLoadResultV4:
        if self.adapter is not None:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "one responsive rollout worker may load exactly one environment"
            )
        if seed != self._branch_plan.simulator_seed:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "rollout seed differs from the causal branch plan"
            )
        if not isinstance(config, DynamicTargetSemanticsConfigV4):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive source-bound worker requires dynamic-v4 max/current HP"
            )
        if config.content_sha256 != self._branch_plan.dynamic_config_sha256:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "dynamic-v4 config differs from the causal branch plan"
            )
        if config.background_damage_events:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive B/C/D worker rejects fixed background damage without "
                "actor-disjoint source provenance"
            )
        actual_prefix = responsive_team_source_prefix_content_sha256_v1(
            runtime=self._runtime,
            candidate_actor_guid=self._candidate_actor_guid,
            target_guid_by_index=self._target_guid_by_index,
            target_introduction_registry=self._target_introduction_registry,
            target_health_prefix_registry=self._target_health_prefix_registry,
            current_source_evidence=self._loaded_model.current_source_evidence,
        )
        if actual_prefix != self._branch_plan.prefix_content_sha256:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "causal branch prefix digest differs from the actual source-bound prefix"
            )
        self.source_prefix_content_sha256 = actual_prefix

        result = self._bridge.load_dynamic_v4(request, seed, config)
        if not isinstance(result, DynamicLoadResultV4):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "bridge load_dynamic_v4 did not return DynamicLoadResultV4"
            )
        receipt = result.receipt
        if receipt.config_digest != config.content_sha256:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "dynamic-v4 load receipt differs from the branch config"
            )
        self._validate_checkpoint_binding(result.state, config)
        self.branch = self._branch_plan.bind(
            environment_generation=receipt.environment_generation,
            loaded_model=self._loaded_model,
        )
        self.adapter = ResponsiveTeamBridgeAdapterV1(
            bridge=self._bridge,
            runtime=self._runtime,
            loaded_model=self._loaded_model,
            candidate_actor_guid=self._candidate_actor_guid,
            target_guid_by_index=self._target_guid_by_index,
            branch=self.branch,
            wake_horizon_exclusive_ms=config.idle_advance_horizon_ms,
        )
        wake = self.adapter.arm_global_next()
        self.initial_wake = dict(wake) if wake is not None else None
        live = self._drain_ready(_mapping(self._bridge.state(), "bridge state"))
        return replace(result, state=dict(live))

    def advance(self) -> JSONMap:
        if self.adapter is None:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "advance requires a successful responsive dynamic-v4 load"
            )
        state = _mapping(self._bridge.advance(), "bridge advance state")
        return dict(self._drain_ready(state))

    def _drain_ready(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        current = state
        while current.get("wake_ready") is not None:
            if self.adapter is None:
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "bridge exposed a responsive wake before adapter creation"
                )
            if self.responsive_event_count >= self._max_responsive_events:
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "responsive event limit exceeded before rollout completion"
                )
            step = self.adapter.emit_global_ready_and_rearm()
            emitted = _mapping(step.get("emitted"), "responsive emitted step")
            receipt = dict(
                _mapping(emitted.get("wire_receipt"), "responsive wire receipt")
            )
            status = _text(receipt.get("status"), "responsive receipt status")
            applied = _number(
                receipt.get("applied_damage"), "responsive receipt applied_damage"
            )
            self.responsive_event_count += 1
            self.applied_damage += applied
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            if self.first_wire_receipt is None:
                self.first_wire_receipt = receipt
            self.last_wire_receipt = receipt
            current = _mapping(self._bridge.state(), "bridge state after response")
        return current

    def _validate_checkpoint_binding(
        self,
        state: Mapping[str, Any],
        config: DynamicTargetSemanticsConfigV4,
    ) -> None:
        now = _integer(state.get("time_ms"), "loaded state time_ms")
        if self._runtime.time_ms != now:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive runtime time differs from the loaded checkpoint"
            )
        health_rows = {
            row.simulator_target_index: row
            for row in self._target_health_prefix_registry.targets
        }
        introduction_rows = {
            row.simulator_target_index: row
            for row in self._target_introduction_registry.targets
        }
        team = _mapping(
            state.get("dynamic_team_background"),
            "loaded dynamic team background",
        )
        raw_targets = team.get("targets")
        if not isinstance(raw_targets, list) or len(raw_targets) != len(
            self._target_guid_by_index
        ):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "loaded bridge target registry differs from source prefix"
            )
        bridge_rows: dict[int, Mapping[str, Any]] = {}
        for raw in raw_targets:
            row = _mapping(raw, "loaded bridge target")
            index = _integer(row.get("target_index"), "loaded target_index")
            if index in bridge_rows:
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "loaded bridge target registry repeats an index"
                )
            bridge_rows[index] = row
        if set(bridge_rows) != set(range(len(self._target_guid_by_index))):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "loaded bridge target registry is incomplete"
            )
        semantics = _mapping(
            state.get("dynamic_target_semantics"),
            "loaded dynamic target semantics",
        )
        raw_semantic_targets = semantics.get("targets")
        if not isinstance(raw_semantic_targets, list) or len(
            raw_semantic_targets
        ) != len(self._target_guid_by_index):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "loaded bridge target-semantics registry differs from source prefix"
            )
        semantic_rows: dict[int, Mapping[str, Any]] = {}
        for raw in raw_semantic_targets:
            row = _mapping(raw, "loaded bridge target semantics")
            index = _integer(
                row.get("target_index"), "loaded semantics target_index"
            )
            if index in semantic_rows:
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "loaded target-semantics registry repeats an index"
                )
            semantic_rows[index] = row
        if set(semantic_rows) != set(range(len(self._target_guid_by_index))):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "loaded target-semantics registry is incomplete"
            )
        for checkpoint in config.target_health:
            index = checkpoint.target_index
            baseline = health_rows[index]
            introduction = introduction_rows[index]
            bridge_row = bridge_rows[index]
            semantic_row = semantic_rows[index]
            target_guid = self._target_guid_by_index[index]
            if introduction.introduced_at_ms > now or baseline.observed_at_ms != now:
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "target was not prefix-visible at the HP checkpoint"
                )
            expected = (
                checkpoint.maximum_health,
                checkpoint.current_health,
                float(self._runtime.health[target_guid]),
                _number(
                    semantic_row.get("maximum_health"),
                    "bridge semantics maximum_health",
                ),
                _number(bridge_row.get("initial_health"), "bridge initial_health"),
                _number(bridge_row.get("current_health"), "bridge current_health"),
                _number(
                    semantic_row.get("current_health"),
                    "bridge semantics current_health",
                ),
            )
            if any(
                not math.isclose(value, baseline.maximum_health, rel_tol=0, abs_tol=0)
                for value in (expected[0], expected[3])
            ) or any(
                not math.isclose(value, baseline.current_health, rel_tol=0, abs_tol=0)
                for value in (
                    expected[1],
                    expected[2],
                    expected[4],
                    expected[5],
                    expected[6],
                )
            ):
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "max/current HP differs across prefix registry, runtime, config, and bridge"
                )
            for field, baseline_value in (
                (
                    "simulated_damage_applied",
                    baseline.simulated_damage_applied_at_observation,
                ),
                (
                    "background_damage_applied",
                    baseline.background_damage_applied_at_observation,
                ),
            ):
                if not math.isclose(
                    _number(bridge_row.get(field), f"bridge {field}"),
                    baseline_value,
                    rel_tol=0,
                    abs_tol=0,
                ):
                    raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                        "prefix damage counters differ from the loaded bridge checkpoint"
                    )

    def evidence(self) -> JSONMap:
        branch = self.branch.to_wire() if self.branch is not None else None
        return {
            "schema": f"{WORKER_SCHEMA}/drive_evidence",
            "dynamic_v4_loaded": self.adapter is not None,
            "atomic_loaded_model_passed_to_adapter": (
                self.adapter is not None
                and self.adapter.loaded_model is self._loaded_model
            ),
            "branch_binding": branch,
            "actual_source_prefix_content_sha256": (
                self.source_prefix_content_sha256
            ),
            "branch_prefix_matches_actual_source_prefix": (
                self.branch is not None
                and self.source_prefix_content_sha256
                == self.branch.prefix_content_sha256
            ),
            "initial_wake": dict(self.initial_wake) if self.initial_wake else None,
            "wake_horizon_exclusive_ms": (
                self.adapter.wake_horizon_exclusive_ms
                if self.adapter is not None
                else None
            ),
            "discarded_deadlines_at_or_after_horizon": (
                list(self.adapter.horizon_discard_evidence())
                if self.adapter is not None
                else []
            ),
            "responsive_event_count": self.responsive_event_count,
            "responsive_applied_damage": self.applied_damage,
            "responsive_status_counts": dict(sorted(self.status_counts.items())),
            "first_wire_receipt": self.first_wire_receipt,
            "last_wire_receipt": self.last_wire_receipt,
            "future_candidate_suffix_visible_to_model": False,
            "development_only": True,
            "comparison_eligible": False,
            "training_authorized": False,
            "deployment_eligible": False,
        }


class ResponsiveTeamSourceBoundRolloutWorkerV1(AbstractContextManager[Any]):
    """Own one SQLite model connection for one long-lived rollout worker."""

    def __init__(
        self,
        *,
        bridge: Any,
        store: ResponsiveTeamRuntimeStoreBindingV1,
        runtime: DynamicTeamRuntimeV1,
        candidate_actor_guid: str,
        target_guid_by_index: Sequence[str],
        target_introduction_registry: TargetIntroductionRegistryV1,
        target_health_prefix_registry: TargetHealthPrefixRegistryV1,
        branch_plan: ResponsiveTeamBranchPlanV1,
        execution_scope: str = DEVELOPMENT_WIRE_SMOKE_SCOPE,
        max_responsive_events: int = 100_000,
    ) -> None:
        if execution_scope != DEVELOPMENT_WIRE_SMOKE_SCOPE:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive source-bound worker has no comparison or training authority"
            )
        if not isinstance(store, ResponsiveTeamRuntimeStoreBindingV1):
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "store must be ResponsiveTeamRuntimeStoreBindingV1"
            )
        self._bridge = bridge
        self._store = store
        self._runtime = runtime
        self._candidate_actor_guid = candidate_actor_guid
        self._target_guid_by_index = tuple(target_guid_by_index)
        self._target_introduction_registry = target_introduction_registry
        self._target_health_prefix_registry = target_health_prefix_registry
        self._branch_plan = branch_plan
        self._max_responsive_events = max_responsive_events
        self.loaded_model: LoadedResponsiveTeammateModelV1 | None = None
        self.driven_bridge: ResponsiveTeamDrivenBridgeV1 | None = None
        self.store_closed = False

    def __enter__(self) -> ResponsiveTeamDrivenBridgeV1:
        if self.loaded_model is not None:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "responsive rollout worker cannot be entered twice"
            )
        try:
            loaded = open_responsive_team_runtime_store_v1(
                self._store.path,
                expected_result_content_sha256=(
                    self._store.expected_result_content_sha256
                ),
                expected_model_content_sha256=(
                    self._store.expected_model_content_sha256
                ),
                variant_id=self._store.variant_id,
                current_source=self._store.current_source,
            )
            self.loaded_model = loaded
            self.driven_bridge = ResponsiveTeamDrivenBridgeV1(
                bridge=self._bridge,
                loaded_model=loaded,
                runtime=self._runtime,
                candidate_actor_guid=self._candidate_actor_guid,
                target_guid_by_index=self._target_guid_by_index,
                target_introduction_registry=self._target_introduction_registry,
                target_health_prefix_registry=self._target_health_prefix_registry,
                branch_plan=self._branch_plan,
                max_responsive_events=self._max_responsive_events,
            )
            return self.driven_bridge
        except Exception:
            self._close_store()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        missing_load = exc_type is None and (
            self.driven_bridge is None or self.driven_bridge.adapter is None
        )
        self._close_store()
        if missing_load:
            raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                "rollout returned without calling responsive load_dynamic_v4"
            )
        return False

    def _close_store(self) -> None:
        if self.store_closed:
            return
        loaded = self.loaded_model
        if loaded is not None:
            close = getattr(loaded.model, "close", None)
            if not callable(close):
                raise ResponsiveTeamSourceBoundRolloutWorkerV1Error(
                    "runtime-store model does not expose close()"
                )
            close()
        self.store_closed = True

    def evidence(self) -> JSONMap:
        return {
            "schema": WORKER_SCHEMA,
            "revision": IMPLEMENTATION_REVISION,
            "execution_scope": DEVELOPMENT_WIRE_SMOKE_SCOPE,
            "runtime_store_opened": self.loaded_model is not None,
            "runtime_store_closed": self.store_closed,
            "rollout_worker_loaded_reducer_json": False,
            "drive": (
                self.driven_bridge.evidence()
                if self.driven_bridge is not None
                else None
            ),
            "scientific_boundary": {
                "prefix_hp_registry_required_before_store_open": True,
                "max_and_current_hp_bound_separately": True,
                "fixed_background_damage_authorized": False,
                "future_target_arrival_supported": False,
                "responsive_model_variant": ABLATION_D,
                "candidate_action_intensity_available": False,
                "development_only": True,
                "comparison_authorized": False,
                "training_authorized": False,
                "voting_eligible": False,
                "deployment_authorized": False,
            },
        }


def run_responsive_team_source_bound_rollout_worker_v1(
    rollout: Callable[[ResponsiveTeamDrivenBridgeV1], T],
    **worker_kwargs: Any,
) -> JSONMap:
    """Run one callable with the driven bridge and return compact session evidence."""

    if not callable(rollout):
        raise TypeError("rollout must be callable")
    worker = ResponsiveTeamSourceBoundRolloutWorkerV1(**worker_kwargs)
    with worker as bridge:
        artifact = rollout(bridge)
    evidence = worker.evidence()
    return {
        "schema": f"{WORKER_SCHEMA}/result",
        "status": "COMPLETE_DEVELOPMENT_RESPONSIVE_TEAM_WIRE_RUN",
        "rollout_artifact": artifact,
        "worker_evidence": evidence,
        "comparison_authorized": False,
        "training_authorized": False,
        "deployment_authorized": False,
    }


__all__ = [
    "DEVELOPMENT_WIRE_SMOKE_SCOPE",
    "IMPLEMENTATION_REVISION",
    "ResponsiveTeamBranchPlanV1",
    "ResponsiveTeamDrivenBridgeV1",
    "ResponsiveTeamRuntimeStoreBindingV1",
    "ResponsiveTeamSourceBoundRolloutWorkerV1",
    "ResponsiveTeamSourceBoundRolloutWorkerV1Error",
    "WORKER_SCHEMA",
    "responsive_team_source_prefix_content_sha256_v1",
    "run_responsive_team_source_bound_rollout_worker_v1",
]
