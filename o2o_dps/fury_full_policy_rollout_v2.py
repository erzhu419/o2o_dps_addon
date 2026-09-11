"""Fail-closed full-policy Fury rollouts over the ordered sink ledger.

This module is deliberately versioned beside, rather than folded into, the
frozen v1 closed loop.  It asks the audited Cat or deployed-Contra-v2 source
adapter for a proposal at every decision epoch, submits the proposal through
``fury_ordered_sink_executor_v2``, and then advances the interactive simulator
without synthesizing a fallback action or wait.

The bridge's immediate ``act`` acknowledgement is only client/simulator
acceptance.  It is never relabelled as a hit, miss, damage result, or other
server outcome.  A separately typed result batch adds later evidence and is
matched back to accepted result-bearing GCD attempts by immutable attempt ID.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
import struct
from typing import Any, Mapping, Protocol, Sequence

from .expert_policy import ExpertDecision, StanceOp, SwingQueueOp
from .expert_proposals import ACTION_KEY_TO_REF
from .fury_contra_adapter_v2 import (
    SEMANTIC_ID as CONTRA_DEPLOYED_V2_EXPERT_ID,
    TRAINING_DUMMY_NAME,
    ContraDeployedFuryAdapterV2,
    ContraDeployedFuryStateV2,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from .fury_expert_adapters import (
    CatFurySourceAdapter,
    ContraNewCandidateAdapter,
    FuryExpertState,
)
from .fury_expert_guided_search_v1 import fury_state_from_simulator
from .fury_ordered_sink_executor_v2 import (
    CAT_EXPERT_ID,
    ControlSinkResultV2,
    execute_ordered_sinks_v2,
)
from .sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    CancelQueueResult,
    BackgroundDamageEventV1,
    DynamicLoadReceiptV1,
    DynamicLoadResultV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
    SERVER_RESULT_OUTCOMES_V2,
    SERVER_TARGET_RESULT_OUTCOMES_V2,
    SetTargetResult,
)


JSONMap = dict[str, Any]

ROLLOUT_SCHEMA = "fury_full_policy_simulator_rollout/v2"
DYNAMIC_ROLLOUT_LOAD_SCHEMA = "fury_full_policy_dynamic_load/v1"
DYNAMIC_LOAD_BINDING_SCHEMA = "fury_full_policy_dynamic_load_binding/v1"
CONTRA260817_POLICY_ID = "contra260817.fury.source_candidate"
_HEALTH_STAT_INDEX = 34


class FuryFullPolicyRolloutV2Error(ValueError):
    """A caller supplied a malformed v2 rollout contract."""


@dataclass(frozen=True)
class DynamicRolloutLoadV1:
    """Typed, content-addressed dynamic load bound to one exact request.

    The request digest prevents a target-health/schedule configuration from
    being silently reused with different equipment, target armor, or encounter
    inputs.  The simulator independently enforces bit-identical target health
    at stat index 34 when ``load_dynamic_v1`` is executed.
    """

    request_sha256: str
    seed: int
    config: DynamicTeamBackgroundConfigV1
    contract_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_sha256, str)
            or len(self.request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.request_sha256)
        ):
            raise FuryFullPolicyRolloutV2Error(
                "dynamic load request_sha256 must be 64 lowercase hexadecimal characters"
            )
        if not isinstance(self.config, DynamicTeamBackgroundConfigV1):
            raise TypeError("dynamic load config must be DynamicTeamBackgroundConfigV1")
        _strict_int(self.seed, "dynamic load seed")
        identity = {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config_digest": self.config.content_sha256,
        }
        object.__setattr__(self, "contract_sha256", _sha256_json(identity))

    @classmethod
    def bind(
        cls,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTeamBackgroundConfigV1,
    ) -> "DynamicRolloutLoadV1":
        return cls(
            request_sha256=_sha256_json(request),
            seed=_strict_int(seed, "dynamic load seed"),
            config=config,
        )

    @classmethod
    def from_wire(
        cls,
        request: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> "DynamicRolloutLoadV1":
        if not isinstance(value, Mapping):
            raise TypeError("dynamic load wire value must be a mapping")
        if set(value) != {
            "schema",
            "request_sha256",
            "seed",
            "config",
            "contract_sha256",
        }:
            raise FuryFullPolicyRolloutV2Error(
                "dynamic load wire field set mismatch"
            )
        if value.get("schema") != DYNAMIC_ROLLOUT_LOAD_SCHEMA:
            raise FuryFullPolicyRolloutV2Error("dynamic load wire schema is unsupported")
        contract = cls(
            request_sha256=str(value.get("request_sha256")),
            seed=_strict_int(value.get("seed"), "dynamic load seed"),
            config=_dynamic_config_from_wire(value.get("config")),
        )
        if contract.request_sha256 != _sha256_json(request):
            raise FuryFullPolicyRolloutV2Error(
                "dynamic load wire request SHA-256 mismatch"
            )
        if value.get("contract_sha256") != contract.contract_sha256:
            raise FuryFullPolicyRolloutV2Error(
                "dynamic load wire contract SHA-256 mismatch"
            )
        return contract

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config": self.config.to_wire(),
            "contract_sha256": self.contract_sha256,
        }


def dynamic_rollout_load_from_adapter_wire_v1(
    value: Mapping[str, Any],
) -> tuple[JSONMap, DynamicRolloutLoadV1]:
    """Strictly consume one generator ``load_dynamic_v1`` wire command.

    Returning both the canonical request copy and the typed load contract keeps
    the generator-to-rollout handoff mechanical: callers do not reconstruct
    target health, schedule rows, request identity, or simulator seed.
    """

    if not isinstance(value, Mapping):
        raise TypeError("adapter wire request must be a mapping")
    if set(value) != {"command", "request", "seed", "dynamic"}:
        raise FuryFullPolicyRolloutV2Error(
            "adapter wire request field set mismatch"
        )
    if value.get("command") != "load_dynamic_v1":
        raise FuryFullPolicyRolloutV2Error(
            "adapter wire command must be load_dynamic_v1"
        )
    request = value.get("request")
    if not isinstance(request, Mapping):
        raise FuryFullPolicyRolloutV2Error(
            "adapter wire request payload must be an object"
        )
    # Strict JSON round-trip creates a detached, canonical-semantics copy and
    # rejects non-finite or non-serializable adapter payloads.
    try:
        request_copy = json.loads(
            json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as error:
        raise FuryFullPolicyRolloutV2Error(
            f"adapter wire request is not strict JSON: {error}"
        ) from error
    seed = _strict_int(value.get("seed"), "adapter wire seed")
    config = _dynamic_config_from_wire(value.get("dynamic"))
    contract = DynamicRolloutLoadV1.bind(request_copy, seed, config)
    _validate_dynamic_load_request_v1(
        contract,
        request_copy,
        request_sha256=contract.request_sha256,
    )
    return request_copy, contract


def dynamic_rollout_load_from_config_wire_v1(
    request: Mapping[str, Any],
    seed: int,
    value: Mapping[str, Any],
) -> DynamicRolloutLoadV1:
    """Bind a frozen scenario config to one paired simulator seed."""

    config = _dynamic_config_from_wire(value)
    contract = DynamicRolloutLoadV1.bind(request, seed, config)
    _validate_dynamic_load_request_v1(
        contract,
        request,
        request_sha256=contract.request_sha256,
    )
    return contract


class TargetSemanticsModeV2(str, Enum):
    """Whether target inputs are asserted exact or form a sensitivity case."""

    DECLARED_EXACT = "DECLARED_EXACT"
    SENSITIVITY = "SENSITIVITY"


@dataclass(frozen=True)
class HealthPercentPointV2:
    """One point in a piecewise-linear target-health sensitivity schedule."""

    time_ms: int
    health_pct: float

    def __post_init__(self) -> None:
        _nonnegative_int(self.time_ms, "health point time_ms")
        _bounded_percent(self.health_pct, "health point health_pct")


@dataclass(frozen=True)
class TargetSemanticsContextV2:
    """Explicit target and loadout inputs used by source policy branches.

    ``DECLARED_EXACT`` reads max health and percentage from the bridge on every
    decision and requires that the bridge says those fields are known.
    Classification, name, and the complete equipped-name list still need
    concrete provenance because o2obridge does not expose WoW's
    ``UnitClassification`` or item names.

    ``SENSITIVITY`` instead supplies max health and a health-percentage curve.
    It is useful for offline uncertainty sweeps but can never, by itself, make a
    rollout eligible for a definitive DPS comparison.
    """

    context_id: str
    mode: TargetSemanticsModeV2
    target_index: int
    target_classification: ContraTargetClassificationV2
    target_name: str
    equipped_item_names: tuple[str, ...]
    target_classification_evidence: ContraFieldEvidenceV2
    target_name_evidence: ContraFieldEvidenceV2
    equipment_evidence: ContraFieldEvidenceV2
    target_position_evidence: ContraFieldEvidenceV2
    target_health_pct_evidence: ContraFieldEvidenceV2
    target_max_health_evidence: ContraFieldEvidenceV2
    target_max_health: int | None = None
    health_pct_schedule: tuple[HealthPercentPointV2, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.context_id, "context_id")
        if not isinstance(self.mode, TargetSemanticsModeV2):
            raise TypeError("mode must be TargetSemanticsModeV2")
        _nonnegative_int(self.target_index, "target_index")
        if not isinstance(
            self.target_classification, ContraTargetClassificationV2
        ):
            raise TypeError(
                "target_classification must be ContraTargetClassificationV2"
            )
        _nonempty(self.target_name, "target_name")
        if not isinstance(self.equipped_item_names, tuple):
            raise TypeError("equipped_item_names must be a tuple")
        for index, name in enumerate(self.equipped_item_names):
            _nonempty(name, f"equipped_item_names[{index}]")
        evidences = {
            "target_classification_evidence": self.target_classification_evidence,
            "target_name_evidence": self.target_name_evidence,
            "equipment_evidence": self.equipment_evidence,
            "target_position_evidence": self.target_position_evidence,
            "target_health_pct_evidence": self.target_health_pct_evidence,
            "target_max_health_evidence": self.target_max_health_evidence,
        }
        for label, evidence in evidences.items():
            if not isinstance(evidence, ContraFieldEvidenceV2):
                raise TypeError(f"{label} must be ContraFieldEvidenceV2")
            if evidence.kind is ContraEvidenceKindV2.MISSING:
                raise FuryFullPolicyRolloutV2Error(
                    f"{label} cannot be MISSING in an executable target context"
                )

        if self.mode is TargetSemanticsModeV2.DECLARED_EXACT:
            if self.target_max_health is not None:
                raise FuryFullPolicyRolloutV2Error(
                    "DECLARED_EXACT target_max_health must come from bridge state"
                )
            if self.health_pct_schedule:
                raise FuryFullPolicyRolloutV2Error(
                    "DECLARED_EXACT must not supply a health sensitivity schedule"
                )
            if any(evidence.sensitivity_only for evidence in evidences.values()):
                raise FuryFullPolicyRolloutV2Error(
                    "DECLARED_EXACT cannot carry sensitivity-only evidence"
                )
            if self.target_health_pct_evidence.kind is not ContraEvidenceKindV2.SIMULATOR_STATE:
                raise FuryFullPolicyRolloutV2Error(
                    "DECLARED_EXACT health percentage requires SIMULATOR_STATE evidence"
                )
            if self.target_max_health_evidence.kind is not ContraEvidenceKindV2.SIMULATOR_STATE:
                raise FuryFullPolicyRolloutV2Error(
                    "DECLARED_EXACT max health requires SIMULATOR_STATE evidence"
                )
            return

        if (
            isinstance(self.target_max_health, bool)
            or not isinstance(self.target_max_health, int)
            or self.target_max_health <= 0
        ):
            raise FuryFullPolicyRolloutV2Error(
                "SENSITIVITY target_max_health must be a positive integer"
            )
        if self.target_max_health_evidence.kind is not ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
            raise FuryFullPolicyRolloutV2Error(
                "SENSITIVITY max health requires SENSITIVITY_HYPOTHESIS evidence"
            )
        if self.target_health_pct_evidence.kind is not ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
            raise FuryFullPolicyRolloutV2Error(
                "SENSITIVITY health percentage requires SENSITIVITY_HYPOTHESIS evidence"
            )
        if not self.health_pct_schedule:
            raise FuryFullPolicyRolloutV2Error(
                "SENSITIVITY requires a health_pct_schedule"
            )
        prior = -1
        for point in self.health_pct_schedule:
            if not isinstance(point, HealthPercentPointV2):
                raise TypeError(
                    "health_pct_schedule entries must be HealthPercentPointV2"
                )
            if point.time_ms <= prior:
                raise FuryFullPolicyRolloutV2Error(
                    "health_pct_schedule times must be strictly increasing"
                )
            prior = point.time_ms


@dataclass(frozen=True)
class ServerTargetResultV2:
    """One deterministic per-target delta behind an aggregate server result."""

    target_index: int
    outcome: str
    damage: float

    def __post_init__(self) -> None:
        _nonnegative_int(self.target_index, "server target result target_index")
        _nonempty(self.outcome, "server target result outcome")
        if self.outcome not in SERVER_TARGET_RESULT_OUTCOMES_V2:
            raise FuryFullPolicyRolloutV2Error(
                f"unsupported server target result outcome {self.outcome!r}"
            )
        _finite_number(self.damage, "server target result damage")
        if self.damage < 0:
            raise FuryFullPolicyRolloutV2Error(
                "server target result damage must be non-negative"
            )

    def to_dict(self) -> JSONMap:
        return {
            "target_index": self.target_index,
            "outcome": self.outcome,
            "damage": self.damage,
        }


@dataclass(frozen=True)
class ServerResultEventV2:
    """One later aggregate plus per-target result, distinct from acceptance."""

    time_ms: int
    outcome: str
    attempt_id: str
    damage: float
    action: ActionRef
    target_results: tuple[ServerTargetResultV2, ...]

    def __post_init__(self) -> None:
        _nonnegative_int(self.time_ms, "server result time_ms")
        _nonempty(self.outcome, "server result outcome")
        if self.outcome not in SERVER_RESULT_OUTCOMES_V2:
            raise FuryFullPolicyRolloutV2Error(
                f"unsupported server result outcome {self.outcome!r}"
            )
        _nonempty(self.attempt_id, "server result attempt_id")
        _finite_number(self.damage, "server result damage")
        if self.damage < 0:
            raise FuryFullPolicyRolloutV2Error(
                "server result damage must be non-negative"
            )
        if not isinstance(self.action, ActionRef):
            raise TypeError("server result action must be ActionRef")
        action_values = (
            self.action.spell_id,
            self.action.item_id,
            self.action.other_id,
            self.action.tag,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in action_values
        ):
            raise FuryFullPolicyRolloutV2Error(
                "server result action fields must be non-negative integers"
            )
        if sum(value > 0 for value in action_values[:3]) != 1:
            raise FuryFullPolicyRolloutV2Error(
                "server result action requires exactly one nonzero primary ID"
            )
        if not isinstance(self.target_results, tuple) or any(
            not isinstance(result, ServerTargetResultV2)
            for result in self.target_results
        ):
            raise TypeError(
                "server result target_results must be a tuple of ServerTargetResultV2"
            )
        target_indices = tuple(
            result.target_index for result in self.target_results
        )
        if len(set(target_indices)) != len(target_indices):
            raise FuryFullPolicyRolloutV2Error(
                "server result target indices must be unique"
            )
        if target_indices != tuple(sorted(target_indices)):
            raise FuryFullPolicyRolloutV2Error(
                "server result target indices must be in ascending order"
            )
        if self.outcome == "CANCELED":
            if self.damage != 0 or self.target_results:
                raise FuryFullPolicyRolloutV2Error(
                    "CANCELED server result requires zero damage and no target_results"
                )
        else:
            if not self.target_results:
                raise FuryFullPolicyRolloutV2Error(
                    "resolved server result requires at least one target result"
                )
            target_damage = sum(
                result.damage for result in self.target_results
            )
            if target_damage != self.damage:
                raise FuryFullPolicyRolloutV2Error(
                    "server result target damage sum must equal aggregate damage"
                )
            target_outcomes = {result.outcome for result in self.target_results}
            expected_outcome = (
                next(iter(target_outcomes))
                if len(target_outcomes) == 1 and "MIXED" not in target_outcomes
                else "MIXED"
            )
            if self.outcome != expected_outcome:
                raise FuryFullPolicyRolloutV2Error(
                    "server result aggregate outcome does not match target_results"
                )

    def to_dict(self) -> JSONMap:
        return {
            "time_ms": self.time_ms,
            "outcome": self.outcome,
            "attempt_id": self.attempt_id,
            "damage": self.damage,
            "action": self.action.to_wire(),
            "target_results": [result.to_dict() for result in self.target_results],
        }


@dataclass(frozen=True)
class ServerResultBatchV2:
    """Typed result-stream evidence complete through a simulator timestamp."""

    complete_through_time_ms: int
    events: tuple[ServerResultEventV2, ...]
    pending_attempt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonnegative_int(
            self.complete_through_time_ms, "complete_through_time_ms"
        )
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, ServerResultEventV2) for event in self.events
        ):
            raise TypeError("events must be a tuple of ServerResultEventV2")
        if not isinstance(self.pending_attempt_ids, tuple) or any(
            not isinstance(attempt_id, str) or not attempt_id.strip()
            for attempt_id in self.pending_attempt_ids
        ):
            raise TypeError(
                "pending_attempt_ids must be a tuple of nonempty strings"
            )
        event_attempt_ids = tuple(event.attempt_id for event in self.events)
        if len(set(event_attempt_ids)) != len(event_attempt_ids):
            raise FuryFullPolicyRolloutV2Error(
                "server result event attempt IDs must be unique"
            )
        if len(set(self.pending_attempt_ids)) != len(self.pending_attempt_ids):
            raise FuryFullPolicyRolloutV2Error(
                "pending server result attempt IDs must be unique"
            )
        if set(event_attempt_ids) & set(self.pending_attempt_ids):
            raise FuryFullPolicyRolloutV2Error(
                "resolved and pending server result attempt IDs must not overlap"
            )


class FullPolicyBridgeLikeV2(Protocol):
    """The full o2obridge surface used or audited by this rollout."""

    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def load_dynamic_v1(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTeamBackgroundConfigV1,
    ) -> DynamicLoadResultV1: ...

    def state(self) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def cancel_queue(self) -> CancelQueueResult: ...

    def set_target(self, target_index: int) -> SetTargetResult: ...

    def wait(self, wait_ms: int) -> JSONMap: ...

    def advance(self) -> JSONMap: ...

    def start_attack(self) -> ControlSinkResultV2: ...

    def stop_cast(self) -> ControlSinkResultV2: ...

    def server_results_since_last_decision(
        self, attempt_ids: Sequence[str] = ()
    ) -> ServerResultBatchV2: ...


_CORE_BRIDGE_METHODS = (
    "load",
    "state",
    "actions",
    "act",
    "cancel_queue",
    "set_target",
    "wait",
    "advance",
)

_OPTIONAL_FIDELITY_METHODS = (
    "start_attack",
    "stop_cast",
    "server_results_since_last_decision",
)


def run_fury_full_policy_rollout_v2(
    bridge: FullPolicyBridgeLikeV2,
    raid_sim_request: Mapping[str, Any],
    adapter: CatFurySourceAdapter | ContraDeployedFuryAdapterV2 | Any,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV2] | None,
    dynamic_load: DynamicRolloutLoadV1 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Run one complete simulator scenario with no synthesized policy action.

    A typed artifact is returned for capability, reconstruction, proposal, and
    runtime blockers.  Malformed caller arguments raise
    :class:`FuryFullPolicyRolloutV2Error`; absent scientific evidence does not.
    """

    _validate_request(raid_sim_request)
    request_sha256 = _sha256_json(raid_sim_request)
    _strict_int(seed, "seed")
    if dynamic_load is not None:
        if not isinstance(dynamic_load, DynamicRolloutLoadV1):
            raise TypeError("dynamic_load must be DynamicRolloutLoadV1 or None")
        _validate_dynamic_load_request_v1(
            dynamic_load,
            raid_sim_request,
            request_sha256=request_sha256,
        )
        if dynamic_load.seed != seed:
            raise FuryFullPolicyRolloutV2Error(
                "dynamic load seed differs from rollout seed"
            )
    _positive_int(max_decisions, "max_decisions")
    _positive_int(max_advances, "max_advances")
    if not isinstance(retain_steps, bool):
        raise TypeError("retain_steps must be boolean")
    contexts = _validate_context_map(target_contexts)

    expert_id = getattr(adapter, "expert_id", type(adapter).__name__)
    if expert_id in {
        CONTRA260817_POLICY_ID,
        ContraNewCandidateAdapter.expert_id,
    } or isinstance(adapter, ContraNewCandidateAdapter):
        return _with_request_identity(_terminal_without_bridge(
            expert_id=CONTRA260817_POLICY_ID,
            seed=seed,
            status="NOT_IMPLEMENTED_REQUIRED_BASELINE",
            blocker=_blocker(
                "CONTRA260817_FULL_POLICY_ADAPTER_NOT_IMPLEMENTED",
                "Contra260817/Contra_new needs its own full source adapter, runtime "
                "profile, and ordered sink coverage; deployed Contra v2 is not a substitute",
                execution_fatal=True,
            ),
            required_baseline=True,
        ), request_sha256, dynamic_load=dynamic_load)
    if not _supported_adapter(adapter):
        return _with_request_identity(_terminal_without_bridge(
            expert_id=str(expert_id),
            seed=seed,
            status="UNSUPPORTED_POLICY_ADAPTER",
            blocker=_blocker(
                "ADAPTER_IDENTITY_NOT_AUDITED",
                "only CatFurySourceAdapter and ContraDeployedFuryAdapterV2 are "
                "accepted by this versioned rollout",
                execution_fatal=True,
            ),
        ), request_sha256, dynamic_load=dynamic_load)
    if not contexts:
        return _with_request_identity(_terminal_without_bridge(
            expert_id=str(expert_id),
            seed=seed,
            status="TARGET_CONTEXT_REQUIRED",
            blocker=_blocker(
                "TARGET_SEMANTICS_CONTEXT_MISSING",
                "source target branches require declared-exact inputs or an explicit "
                "sensitivity context",
                execution_fatal=True,
            ),
        ), request_sha256, dynamic_load=dynamic_load)

    capability_methods = (
        *_CORE_BRIDGE_METHODS,
        *(('load_dynamic_v1',) if dynamic_load is not None else ()),
        *_OPTIONAL_FIDELITY_METHODS,
    )
    capabilities = {
        name: callable(getattr(bridge, name, None))
        for name in capability_methods
    }
    blockers: list[JSONMap] = []
    required_core = tuple(
        name
        for name in _CORE_BRIDGE_METHODS
        if name != "load" or dynamic_load is None
    ) + (("load_dynamic_v1",) if dynamic_load is not None else ())
    missing_core = [name for name in required_core if not capabilities[name]]
    if missing_core:
        for name in missing_core:
            blockers.append(
                _blocker(
                    "BRIDGE_CORE_CAPABILITY_ABSENT",
                    f"required bridge method {name} is absent",
                    execution_fatal=True,
                    evidence={"method": name},
                )
            )
        return _with_request_identity(_artifact(
            expert_id=str(expert_id),
            seed=seed,
            status="BRIDGE_CAPABILITY_BLOCKED",
            capabilities=capabilities,
            blockers=blockers,
        ), request_sha256, dynamic_load=dynamic_load)

    if not capabilities["start_attack"]:
        blockers.append(
            _blocker(
                "BRIDGE_START_ATTACK_CONTROL_ABSENT",
                "source MPStartAttack/Contra.StartAttack attempts cannot be submitted",
                execution_fatal=False,
            )
        )
    if not capabilities["stop_cast"]:
        blockers.append(
            _blocker(
                "BRIDGE_STOP_CAST_CONTROL_ABSENT",
                "a source SpellStopCasting attempt will fail closed when encountered",
                execution_fatal=False,
            )
        )
    if not capabilities["server_results_since_last_decision"]:
        blockers.append(
            _blocker(
                "BRIDGE_SERVER_RESULT_STREAM_ABSENT",
                "hit/miss/crit/damage outcomes are not exposed separately from act acceptance",
                execution_fatal=False,
            )
        )

    if any(
        context.mode is TargetSemanticsModeV2.SENSITIVITY
        for context in contexts.values()
    ):
        blockers.append(
            _blocker(
                "TARGET_SEMANTICS_SENSITIVITY_NOT_EXACT",
                "the rollout is a named target-semantics sensitivity case, not exact target truth",
                execution_fatal=False,
            )
        )

    dynamic_load_receipt: DynamicLoadReceiptV1 | None = None

    def _validated_runtime_state(value: Any, label: str) -> JSONMap:
        current = _validated_state(value, label)
        if dynamic_load is not None:
            if dynamic_load_receipt is None:
                raise FuryFullPolicyRolloutV2Error(
                    "dynamic runtime state observed before a load receipt"
                )
            _validate_dynamic_loaded_state_v1(
                current,
                dynamic_load,
                dynamic_load_receipt,
            )
        return current

    try:
        if dynamic_load is None:
            loaded = bridge.load(raid_sim_request, seed)
            state = _validated_runtime_state(loaded, "bridge.load")
            state_label = "bridge.state after load"
        else:
            loaded_dynamic = bridge.load_dynamic_v1(
                raid_sim_request,
                seed,
                dynamic_load.config,
            )
            if not isinstance(loaded_dynamic, DynamicLoadResultV1):
                raise TypeError(
                    "bridge.load_dynamic_v1 must return DynamicLoadResultV1"
            )
            dynamic_load_receipt = loaded_dynamic.receipt
            _validate_dynamic_load_result_v1(dynamic_load, loaded_dynamic)
            state = _validated_runtime_state(
                loaded_dynamic.state, "bridge.load_dynamic_v1.state"
            )
            state_label = "bridge.state after load_dynamic_v1"
        reported = _validated_runtime_state(bridge.state(), state_label)
    except Exception as error:
        blockers.append(_bridge_error("BRIDGE_LOAD_OR_STATE_ERROR", error, fatal=True))
        return _with_request_identity(_artifact(
            expert_id=str(expert_id),
            seed=seed,
            status="INCOMPLETE_BLOCKED",
            capabilities=capabilities,
            blockers=blockers,
        ), request_sha256, dynamic_load=dynamic_load)
    if _state_fingerprint(state) != _state_fingerprint(reported):
        blockers.append(
            _blocker(
                "BRIDGE_STATE_DIVERGED_FROM_LOAD",
                "state() does not agree with the state returned by load()",
                execution_fatal=True,
            )
        )
        return _with_request_identity(_artifact(
            expert_id=str(expert_id),
            seed=seed,
            status="INCOMPLETE_BLOCKED",
            capabilities=capabilities,
            blockers=blockers,
            final_state=reported,
        ), request_sha256, dynamic_load=dynamic_load, dynamic_load_receipt=dynamic_load_receipt)
    state = reported
    root_state = dict(state)
    last_gcd_action = ""
    steps: list[JSONMap] = []
    advances: list[JSONMap] = []
    decision_count = 0
    advance_count = 0
    execution_fatal = False
    outstanding_attempt_ids: list[str] = []
    attempt_sinks: dict[str, JSONMap] = {}

    while not bool(state["finished"]):
        if not bool(state["needs_input"]):
            if advance_count >= max_advances:
                blockers.append(
                    _blocker(
                        "MAX_ADVANCES_EXCEEDED",
                        f"scenario exceeded max_advances={max_advances}",
                        execution_fatal=True,
                    )
                )
                execution_fatal = True
                break
            before_advance = dict(state)
            try:
                state = _validated_runtime_state(bridge.advance(), "bridge.advance")
            except Exception as error:
                blockers.append(_bridge_error("BRIDGE_ADVANCE_ERROR", error, fatal=True))
                execution_fatal = True
                break
            advance_count += 1
            transition_blocker = _state_transition_blocker(
                before_advance, state, command="advance"
            )
            if transition_blocker is not None:
                blockers.append(transition_blocker)
                execution_fatal = True
                break
            observation, result_blocker, pending_attempt_ids = _observe_server_boundary(
                bridge,
                before_advance,
                state,
                capabilities=capabilities,
                attempt_ids=tuple(outstanding_attempt_ids),
                attempt_sinks=attempt_sinks,
            )
            _attach_registered_result_followups(attempt_sinks, observation)
            outstanding_attempt_ids = list(pending_attempt_ids)
            advances.append(observation)
            if result_blocker is not None:
                blockers.append(result_blocker)
                execution_fatal = bool(result_blocker["execution_fatal"])
                if execution_fatal:
                    break
            continue

        if decision_count >= max_decisions:
            blockers.append(
                _blocker(
                    "MAX_DECISIONS_EXCEEDED",
                    f"scenario exceeded max_decisions={max_decisions}",
                    execution_fatal=True,
                )
            )
            execution_fatal = True
            break

        before = dict(state)
        try:
            live = _validated_runtime_state(
                bridge.state(), "bridge.state before decision"
            )
        except Exception as error:
            blockers.append(_bridge_error("BRIDGE_STATE_ERROR", error, fatal=True))
            execution_fatal = True
            break
        if _state_fingerprint(before) != _state_fingerprint(live):
            blockers.append(
                _blocker(
                    "BRIDGE_STATE_DIVERGED_BEFORE_DECISION",
                    "carried state does not agree with state() at the decision epoch",
                    execution_fatal=True,
                    decision_index=decision_count,
                )
            )
            execution_fatal = True
            state = live
            break

        try:
            available = bridge.actions()
            if not isinstance(available, list) or any(
                not isinstance(row, AvailableAction) for row in available
            ):
                raise TypeError("bridge.actions must return list[AvailableAction]")
            target = _resolve_target_semantics(
                before,
                raid_sim_request,
                contexts,
            )
            combat = _combat_state(
                before,
                available,
                raid_sim_request,
                target,
                last_gcd_action=last_gcd_action,
            )
            proposal = _proposal(adapter, combat, target)
        except _TargetResolutionBlocked as error:
            blockers.append(
                _blocker(
                    error.code,
                    str(error),
                    execution_fatal=True,
                    decision_index=decision_count,
                )
            )
            execution_fatal = True
            break
        except Exception as error:
            blockers.append(
                _bridge_error(
                    "STATE_RECONSTRUCTION_OR_PROPOSAL_ERROR",
                    error,
                    fatal=True,
                    decision_index=decision_count,
                )
            )
            execution_fatal = True
            break

        try:
            execution = execute_ordered_sinks_v2(
                bridge,
                proposal,
                before,
                attempt_id_prefix=f"decision-{decision_count}",
                result_bearing_action_keys=tuple(
                    sorted(_RESULT_BEARING_GCD_ACTIONS)
                ),
            )
            new_attempt_ids = _annotate_attempt_ids(execution, decision_count)
            _register_result_attempt_sinks(
                execution,
                new_attempt_ids,
                attempt_sinks,
            )
            outstanding_attempt_ids.extend(new_attempt_ids)
            _annotate_immediate_state_deltas(execution)
            command_state = _validated_runtime_state(
                execution["final_state"], "ordered executor final_state"
            )
        except Exception as error:
            blockers.append(
                _bridge_error(
                    "ORDERED_SINK_EXECUTOR_ERROR",
                    error,
                    fatal=True,
                    decision_index=decision_count,
                )
            )
            execution_fatal = True
            break

        step_blockers = _audit_ordered_execution(
            execution,
            proposal,
            decision_index=decision_count,
        )
        successful = _accepted_gcd_actions_from_events(execution)
        if successful:
            last_gcd_action = successful[-1]

        try:
            live_after_commands = _validated_runtime_state(
                bridge.state(), "bridge.state after commands"
            )
        except Exception as error:
            step_blockers.append(
                _bridge_error(
                    "BRIDGE_STATE_ERROR_AFTER_COMMANDS",
                    error,
                    fatal=True,
                    decision_index=decision_count,
                )
            )
            live_after_commands = command_state
        if _state_fingerprint(command_state) != _state_fingerprint(
            live_after_commands
        ):
            step_blockers.append(
                _blocker(
                    "BRIDGE_STATE_DIVERGED_AFTER_COMMANDS",
                    "ordered executor final_state does not agree with state()",
                    execution_fatal=True,
                    decision_index=decision_count,
                )
            )
        command_state = live_after_commands
        command_transition_blocker = _state_transition_blocker(
            before,
            command_state,
            command="ordered_source_sinks",
            decision_index=decision_count,
        )
        if command_transition_blocker is not None:
            step_blockers.append(command_transition_blocker)

        next_state = command_state
        server_observation = _unknown_server_boundary(
            command_state, tuple(outstanding_attempt_ids), capabilities
        )
        if not any(bool(item["execution_fatal"]) for item in step_blockers):
            if bool(command_state["finished"]):
                (
                    server_observation,
                    result_blocker,
                    pending_attempt_ids,
                ) = _observe_server_boundary(
                    bridge,
                    command_state,
                    command_state,
                    capabilities=capabilities,
                    attempt_ids=tuple(outstanding_attempt_ids),
                    attempt_sinks=attempt_sinks,
                )
                _attach_registered_result_followups(
                    attempt_sinks, server_observation
                )
                outstanding_attempt_ids = list(pending_attempt_ids)
                if result_blocker is not None:
                    step_blockers.append(result_blocker)
            elif not bool(command_state["needs_input"]):
                if advance_count >= max_advances:
                    step_blockers.append(
                        _blocker(
                            "MAX_ADVANCES_EXCEEDED",
                            f"scenario exceeded max_advances={max_advances}",
                            execution_fatal=True,
                            decision_index=decision_count,
                        )
                    )
                else:
                    try:
                        next_state = _validated_runtime_state(
                            bridge.advance(), "bridge.advance after decision"
                        )
                        advance_count += 1
                        transition_blocker = _state_transition_blocker(
                            command_state,
                            next_state,
                            command="advance_after_decision",
                            decision_index=decision_count,
                        )
                        if transition_blocker is not None:
                            step_blockers.append(transition_blocker)
                        (
                            server_observation,
                            result_blocker,
                            pending_attempt_ids,
                        ) = _observe_server_boundary(
                            bridge,
                            command_state,
                            next_state,
                            capabilities=capabilities,
                            attempt_ids=tuple(outstanding_attempt_ids),
                            attempt_sinks=attempt_sinks,
                        )
                        _attach_registered_result_followups(
                            attempt_sinks, server_observation
                        )
                        outstanding_attempt_ids = list(pending_attempt_ids)
                        if result_blocker is not None:
                            step_blockers.append(result_blocker)
                    except Exception as error:
                        step_blockers.append(
                            _bridge_error(
                                "BRIDGE_ADVANCE_AFTER_DECISION_ERROR",
                                error,
                                fatal=True,
                                decision_index=decision_count,
                            )
                        )
            else:
                step_blockers.append(
                    _blocker(
                        "DECISION_NOT_CONSUMED_NO_FALLBACK",
                        "source proposal left the decision open; v2 refuses to synthesize a wait",
                        execution_fatal=True,
                        decision_index=decision_count,
                    )
                )

        _attach_result_followups(execution, server_observation)
        step_blockers.extend(
            _audit_gcd_result_followups(
                execution,
                decision_index=decision_count,
                stream_available=capabilities[
                    "server_results_since_last_decision"
                ],
            )
        )
        step = {
            "decision_index": decision_count,
            "simulator_state_before": before,
            "available_actions_before": [_available_action(row) for row in available],
            "target_semantics": _jsonable(target),
            "expert_state": _jsonable(asdict(combat)),
            "proposal": proposal.to_dict(),
            "ordered_execution": execution,
            "independent_ordered_audit": {
                "derived_from_sink_events": True,
                "executor_faithful_flag_trusted": False,
                "executor_blocked_flag_trusted": False,
                "blockers": step_blockers,
                "faithful": not step_blockers,
            },
            "server_observation_after_advance": server_observation,
            "simulator_state_after_commands": command_state,
            "simulator_state_next_epoch": next_state,
            "time_delta_ms": next_state["time_ms"] - before["time_ms"],
            "damage_delta": next_state["damage_done"] - before["damage_done"],
            "blockers": step_blockers,
        }
        if retain_steps:
            steps.append(step)
        blockers.extend(step_blockers)
        decision_count += 1
        state = next_state
        if any(bool(item["execution_fatal"]) for item in step_blockers):
            execution_fatal = True
            break

    if (
        bool(state.get("finished"))
        and capabilities["server_results_since_last_decision"]
        and outstanding_attempt_ids
    ):
        terminal_observation, terminal_blocker, pending_attempt_ids = (
            _right_censor_terminal_active_hardcast(
                state,
                tuple(outstanding_attempt_ids),
                attempt_sinks,
            )
        )
        if terminal_observation is not None:
            advances.append(terminal_observation)
        if terminal_blocker is not None:
            blockers.append(terminal_blocker)
        outstanding_attempt_ids = list(pending_attempt_ids)
    if (
        bool(state.get("finished"))
        and capabilities["server_results_since_last_decision"]
        and outstanding_attempt_ids
    ):
        blockers.append(
            _blocker(
                "SERVER_RESULT_ATTEMPTS_UNRESOLVED_AT_SCENARIO_TERMINAL",
                "non-censorable accepted result-bearing actions remain pending "
                "at the scoring horizon",
                execution_fatal=True,
                evidence={
                    "pending_attempt_ids": list(outstanding_attempt_ids),
                },
            )
        )
        execution_fatal = True
    bridge_finished = bool(state.get("finished")) and not execution_fatal
    completion = _completion_receipt(
        raid_sim_request,
        root_state,
        state,
        dynamic_load=dynamic_load,
    )
    configured_complete = bridge_finished and bool(completion["criterion_met"])
    if bridge_finished and not configured_complete:
        blockers.append(
            _blocker(
                "CONFIGURED_SCENARIO_COMPLETION_NOT_MET",
                "bridge reported finished before the configured scenario completion criterion",
                execution_fatal=True,
                evidence=completion,
            )
        )
    faithful = configured_complete and not blockers
    elapsed_ms = int(state["time_ms"]) - int(root_state["time_ms"])
    damage_delta = float(state["damage_done"]) - float(root_state["damage_done"])
    status = (
        "COMPLETE_FAITHFUL"
        if faithful
        else "COMPLETE_NONFAITHFUL"
        if configured_complete
        else "INCOMPLETE_BLOCKED"
    )
    result = _artifact(
        expert_id=str(expert_id),
        seed=seed,
        status=status,
        capabilities=capabilities,
        blockers=blockers,
        final_state=state,
    )
    result.update(
        {
            "root_state": root_state,
            "request_sha256": request_sha256,
            "bridge_scenario_finished": bridge_finished,
            "configured_completion": completion,
            "scenario_complete": configured_complete,
            "decision_count": decision_count,
            "advance_count": advance_count,
            "elapsed_ms": elapsed_ms,
            "damage_delta": damage_delta,
            "diagnostic_dps": (
                None if elapsed_ms <= 0 else damage_delta / (elapsed_ms / 1000.0)
            ),
            "ordered_projection_faithful": faithful,
            "simulator_dps_comparison_eligible": faithful,
            "target_context_receipts": [
                _context_receipt(context)
                for _, context in sorted(contexts.items())
            ],
            "autonomous_advance_observations": advances,
            "steps_retained": retain_steps,
            "steps": steps if retain_steps else None,
        }
    )
    if dynamic_load is not None:
        result["bridge_command_contract"]["initial_load_command"] = (
            "load_dynamic_v1"
        )
        result["dynamic_load_binding"] = _dynamic_load_binding_receipt_v1(
            dynamic_load,
            dynamic_load_receipt,
        )
    return result


def contra260817_required_baseline_status_v2(*, seed: int) -> JSONMap:
    """Return the mandatory, non-substitutable Contra260817 baseline status."""

    _strict_int(seed, "seed")
    return _terminal_without_bridge(
        expert_id=CONTRA260817_POLICY_ID,
        seed=seed,
        status="NOT_IMPLEMENTED_REQUIRED_BASELINE",
        blocker=_blocker(
            "CONTRA260817_FULL_POLICY_ADAPTER_NOT_IMPLEMENTED",
            "Contra260817 requires its own adapter; deployed Contra v2 cannot stand in for it",
            execution_fatal=True,
        ),
        required_baseline=True,
    )


class _TargetResolutionBlocked(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _supported_adapter(adapter: Any) -> bool:
    return (
        type(adapter) is CatFurySourceAdapter
        and adapter.expert_id == CAT_EXPERT_ID
    ) or (
        type(adapter) is ContraDeployedFuryAdapterV2
        and adapter.expert_id == CONTRA_DEPLOYED_V2_EXPERT_ID
    )


def _resolve_target_semantics(
    state: Mapping[str, Any],
    request: Mapping[str, Any],
    contexts: Mapping[int, TargetSemanticsContextV2],
) -> JSONMap:
    raw_index = state.get("target_index", 0)
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
        raise _TargetResolutionBlocked(
            "TARGET_INDEX_UNKNOWN_OR_INVALID", "bridge target_index is invalid"
        )
    request_targets = _request_targets(request)
    if raw_index >= len(request_targets):
        raise _TargetResolutionBlocked(
            "TARGET_INDEX_OUT_OF_REQUEST_RANGE",
            f"bridge target index {raw_index} is outside the request target array",
        )
    raw_num_targets = state.get("num_targets")
    dynamic_state = state.get("dynamic_team_background")
    if dynamic_state is None:
        if raw_num_targets is not None and (
            isinstance(raw_num_targets, bool)
            or not isinstance(raw_num_targets, int)
            or raw_num_targets != len(request_targets)
        ):
            raise _TargetResolutionBlocked(
                "BRIDGE_REQUEST_TARGET_COUNT_MISMATCH",
                "bridge num_targets does not match RaidSimRequest encounter.targets",
            )
    else:
        if not isinstance(dynamic_state, Mapping):
            raise _TargetResolutionBlocked(
                "DYNAMIC_TARGET_STATE_INVALID",
                "bridge dynamic_team_background must be an object",
            )
        total_target_count = state.get("total_target_count")
        if (
            isinstance(total_target_count, bool)
            or not isinstance(total_target_count, int)
            or total_target_count != len(request_targets)
        ):
            raise _TargetResolutionBlocked(
                "BRIDGE_REQUEST_TARGET_COUNT_MISMATCH",
                "bridge total_target_count does not match RaidSimRequest encounter.targets",
            )
        dynamic_targets = dynamic_state.get("targets")
        if not isinstance(dynamic_targets, list) or len(dynamic_targets) != total_target_count:
            raise _TargetResolutionBlocked(
                "DYNAMIC_TARGET_STATE_INVALID",
                "bridge dynamic target rows do not match total_target_count",
            )
        live_target_count = 0
        for index, dynamic_target in enumerate(dynamic_targets):
            if (
                not isinstance(dynamic_target, Mapping)
                or dynamic_target.get("target_index") != index
                or not isinstance(dynamic_target.get("dead"), bool)
            ):
                raise _TargetResolutionBlocked(
                    "DYNAMIC_TARGET_STATE_INVALID",
                    "bridge dynamic target rows are not indexed typed lifecycle rows",
                )
            if dynamic_target["dead"] is False:
                live_target_count += 1
        if (
            isinstance(raw_num_targets, bool)
            or not isinstance(raw_num_targets, int)
            or raw_num_targets != live_target_count
        ):
            raise _TargetResolutionBlocked(
                "DYNAMIC_LIVE_TARGET_COUNT_MISMATCH",
                "bridge num_targets does not match dynamic live target rows",
            )
    context = contexts.get(raw_index)
    if context is None:
        raise _TargetResolutionBlocked(
            "TARGET_CONTEXT_FOR_INDEX_MISSING",
            f"no explicit target context exists for target index {raw_index}",
        )
    target = request_targets[raw_index]
    request_name = target.get("name") if isinstance(target, Mapping) else None
    if isinstance(request_name, str) and request_name and request_name != context.target_name:
        raise _TargetResolutionBlocked(
            "TARGET_CONTEXT_REQUEST_NAME_MISMATCH",
            f"context name {context.target_name!r} does not match request target {request_name!r}",
        )

    if context.mode is TargetSemanticsModeV2.DECLARED_EXACT:
        if state.get("target_health_known") is not True:
            raise _TargetResolutionBlocked(
                "EXACT_TARGET_HEALTH_NOT_EXPOSED",
                "DECLARED_EXACT requires target_health_known=true from bridge state",
            )
        health_pct = _bounded_percent(
            state.get("target_health_percent"), "bridge target_health_percent"
        )
        max_raw = state.get("target_health_max")
        max_number = _finite_number(max_raw, "bridge target_health_max")
        if max_number <= 0 or not max_number.is_integer():
            raise _TargetResolutionBlocked(
                "EXACT_TARGET_MAX_HEALTH_INVALID",
                "bridge target_health_max must be a positive integral value",
            )
        max_health = int(max_number)
    else:
        health_pct = _interpolate_health(
            context.health_pct_schedule, int(state["time_ms"])
        )
        assert context.target_max_health is not None
        max_health = context.target_max_health

    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": raw_index,
        "target_health_pct": health_pct,
        "target_max_health": max_health,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "target_distance_yards": _player_distance(request),
        "equipped_item_names": list(context.equipped_item_names),
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence,
            "target_max_health": context.target_max_health_evidence,
            "target_classification": context.target_classification_evidence,
            "target_name": context.target_name_evidence,
            "equipped_item_names": context.equipment_evidence,
            "target_position": context.target_position_evidence,
        },
        "exact_by_declared_contract": (
            context.mode is TargetSemanticsModeV2.DECLARED_EXACT
        ),
    }


def _combat_state(
    state: Mapping[str, Any],
    available: Sequence[AvailableAction],
    request: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    last_gcd_action: str,
) -> FuryExpertState:
    base = fury_state_from_simulator(state, available, request)
    target_index = int(target["target_index"])
    request_target = _request_targets(request)[target_index]
    raw_level = request_target.get("level")
    level = (
        int(raw_level)
        if isinstance(raw_level, (int, float)) and not isinstance(raw_level, bool)
        else None
    )
    current_cast = state.get("current_cast")
    casting_slam = False
    slam_remaining_s = 0.0
    if isinstance(current_cast, Mapping):
        action = current_cast.get("action")
        casting_slam = (
            isinstance(action, Mapping)
            and action.get("spell_id")
            == ACTION_KEY_TO_REF["warrior.slam"].spell_id
        )
        remaining = current_cast.get("remaining_ms")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            slam_remaining_s = max(0.0, float(remaining) / 1000.0)
    classification = ContraTargetClassificationV2(target["target_classification"])
    name = str(target["target_name"])
    return replace(
        base,
        target_health_pct=float(target["target_health_pct"]),
        target_name=name,
        target_level=level,
        target_is_boss=(
            classification is ContraTargetClassificationV2.WORLDBOSS
        ),
        target_is_training_dummy=(
            name == TRAINING_DUMMY_NAME
            or "dummy" in name.casefold()
            or "木桩" in name
            or "假人" in name
        ),
        target_distance_yards=float(target["target_distance_yards"]),
        in_melee_range=float(target["target_distance_yards"]) < 8.0,
        casting_slam=casting_slam,
        slam_remaining_s=slam_remaining_s,
        last_cast_name=last_gcd_action,
        queued_swing=_queued_swing(state),
        nearby_enemies=(
            int(state["num_targets"])
            if state.get("dynamic_team_background") is not None
            else len(_request_targets(request))
        ),
    )


def _proposal(
    adapter: CatFurySourceAdapter | ContraDeployedFuryAdapterV2,
    combat: FuryExpertState,
    target: Mapping[str, Any],
) -> ExpertDecision:
    if isinstance(adapter, CatFurySourceAdapter):
        return adapter.propose(combat)
    field_evidence = target["field_evidence"]
    return adapter.propose(
        ContraDeployedFuryStateV2(
            combat=combat,
            target_health_pct=float(target["target_health_pct"]),
            target_max_health=int(target["target_max_health"]),
            target_classification=str(target["target_classification"]),
            target_name=str(target["target_name"]),
            target_health_pct_evidence=field_evidence["target_health_pct"],
            target_max_health_evidence=field_evidence["target_max_health"],
            target_classification_evidence=field_evidence[
                "target_classification"
            ],
            target_name_evidence=field_evidence["target_name"],
            equipped_item_names=tuple(target["equipped_item_names"]),
            equipment_evidence=field_evidence["equipped_item_names"],
            target_position_evidence=field_evidence["target_position"],
        )
    )


def _audit_ordered_execution(
    execution: Mapping[str, Any],
    proposal: ExpertDecision,
    *,
    decision_index: int,
) -> list[JSONMap]:
    """Re-derive fidelity from trace facts instead of trusting summary flags."""

    blockers: list[JSONMap] = []

    def add(
        code: str,
        message: str,
        *,
        fatal: bool,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        blockers.append(
            _blocker(
                code,
                message,
                execution_fatal=fatal,
                decision_index=decision_index,
                evidence=evidence,
            )
        )

    fallback = execution.get("fallback")
    if not isinstance(fallback, Mapping) or fallback.get("used") is not False:
        add(
            "ORDERED_TRACE_FALLBACK_PRESENT_OR_UNKNOWN",
            "ordered execution did not prove that fallback was unused",
            fatal=True,
        )
    expected_raw = [sink.to_dict() for sink in proposal.raw_sink_order]
    if execution.get("raw_sink_order") != expected_raw:
        add(
            "ORDERED_TRACE_RAW_LEDGER_MISMATCH",
            "execution raw sink ledger differs from the source proposal",
            fatal=True,
        )
    events = execution.get("sink_events")
    if not isinstance(events, list) or len(events) != len(expected_raw):
        add(
            "ORDERED_TRACE_EVENT_CARDINALITY_MISMATCH",
            "one trace event is required for every raw source sink",
            fatal=True,
            evidence={
                "expected": len(expected_raw),
                "observed": len(events) if isinstance(events, list) else None,
            },
        )
        return blockers

    for expected_order, (expected_sink, event) in enumerate(
        zip(expected_raw, events), start=1
    ):
        if not isinstance(event, Mapping):
            add(
                "ORDERED_TRACE_EVENT_UNTYPED",
                f"sink event {expected_order} is not an object",
                fatal=True,
            )
            continue
        if event.get("order") != expected_order or event.get("source_sink") != expected_sink:
            add(
                "ORDERED_TRACE_SOURCE_ORDER_MISMATCH",
                f"sink event {expected_order} does not preserve source order/identity",
                fatal=True,
            )
        attempt = event.get("source_attempt")
        if not isinstance(attempt, Mapping) or attempt.get("status") != "ATTEMPTED":
            add(
                "ORDERED_TRACE_SOURCE_ATTEMPT_MISSING",
                f"sink event {expected_order} lacks an explicit source attempt",
                fatal=True,
            )
        submission = event.get("simulator_submission")
        submission_status = (
            submission.get("status") if isinstance(submission, Mapping) else None
        )
        channel = expected_sink["channel"]
        accepted_submission = submission_status in {
            "SUBMITTED",
            "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
        }
        if not accepted_submission:
            fatal = submission_status in {
                "NOT_SUBMITTED_FAIL_CLOSED",
                "BRIDGE_ERROR_FAIL_CLOSED",
            }
            if (
                submission_status == "NOT_SUBMITTED_BRIDGE_CAPABILITY_ABSENT"
                and channel == "cast_control"
            ):
                fatal = True
            add(
                "ORDERED_TRACE_SINK_NOT_SUBMITTED",
                f"raw sink {expected_order} submission status is {submission_status!r}",
                fatal=fatal,
                evidence={"order": expected_order, "status": submission_status},
            )

        acceptance = event.get("client_acceptance")
        acceptance_status = (
            acceptance.get("status") if isinstance(acceptance, Mapping) else None
        )
        if acceptance_status not in {
            "ACCEPTED",
            "NOT_APPLICABLE_NO_ACTIVE_CAST",
            "REJECTED_SOURCE_DECLARED_NOOP",
        }:
            fatal = channel == "cast_control" or acceptance_status in {
                "UNKNOWN_BRIDGE_ERROR",
                "REJECTED_ACTION_ABSENT_FROM_SIMULATOR_SPELLBOOK",
            }
            add(
                "ORDERED_TRACE_CLIENT_ACCEPTANCE_NONFAITHFUL",
                f"raw sink {expected_order} acceptance status is {acceptance_status!r}",
                fatal=fatal,
                evidence={"order": expected_order, "status": acceptance_status},
            )
        consumption = event.get("decision_consumption")
        if acceptance_status == "ACCEPTED":
            if not isinstance(consumption, Mapping):
                add(
                    "ORDERED_TRACE_DECISION_CONSUMPTION_MISSING",
                    f"accepted sink {expected_order} lacks decision consumption evidence",
                    fatal=True,
                )
            else:
                expected = consumption.get("expected_for_lane")
                observed = consumption.get("consumes_decision")
                if isinstance(expected, bool) and observed is not expected:
                    add(
                        "ORDERED_TRACE_DECISION_CONSUMPTION_MISMATCH",
                        f"accepted sink {expected_order} consumption differs from its lane",
                        fatal=True,
                        evidence={"expected": expected, "observed": observed},
                    )
        queue = event.get("queue_transition")
        if channel == "swing_queue":
            kind = queue.get("kind") if isinstance(queue, Mapping) else None
            if kind not in {"QUEUED", "REPLACED", "ACCEPTED_ALREADY_ACTIVE"}:
                add(
                    "ORDERED_TRACE_QUEUE_TRANSITION_NONFAITHFUL",
                    f"queue sink {expected_order} transition is {kind!r}",
                    fatal=False,
                    evidence={"kind": kind},
                )
        immediate_result = event.get("server_result")
        if not isinstance(immediate_result, Mapping):
            add(
                "ORDERED_TRACE_IMMEDIATE_RESULT_BOUNDARY_MISSING",
                f"sink {expected_order} lacks immediate server-result boundary",
                fatal=True,
            )
        elif immediate_result.get("damage_or_miss_result") is not None:
            add(
                "ORDERED_TRACE_ACCEPTANCE_MISLABELED_AS_RESULT",
                f"sink {expected_order} put a result in the immediate acceptance record",
                fatal=True,
            )

    wait_event = execution.get("wait_event")
    if proposal.gcd == "WAIT":
        if not isinstance(wait_event, Mapping):
            add(
                "ORDERED_TRACE_SOURCE_WAIT_MISSING",
                "WAIT proposal lacks its explicit source wait event",
                fatal=True,
            )
        else:
            submission = wait_event.get("simulator_submission")
            acceptance = wait_event.get("client_acceptance")
            consumption = wait_event.get("decision_consumption")
            if (
                not isinstance(submission, Mapping)
                or submission.get("status") != "SUBMITTED"
                or not isinstance(acceptance, Mapping)
                or acceptance.get("status") != "ACCEPTED"
                or not isinstance(consumption, Mapping)
                or consumption.get("consumes_decision") is not True
            ):
                add(
                    "ORDERED_TRACE_SOURCE_WAIT_NOT_ACCEPTED",
                    "WAIT proposal was not submitted and consumed exactly",
                    fatal=True,
                )
    elif wait_event is not None:
        add(
            "ORDERED_TRACE_UNEXPECTED_WAIT_EVENT",
            "non-WAIT source proposal contains a wait event",
            fatal=True,
        )

    reasons = execution.get("nonfaithful_reasons")
    if not isinstance(reasons, list):
        add(
            "ORDERED_TRACE_NONFAITHFUL_LEDGER_UNTYPED",
            "executor nonfaithful reason ledger is not an array",
            fatal=True,
        )
    else:
        for reason in reasons:
            add(
                "ORDERED_EXECUTOR_REPORTED_NONFAITHFUL",
                str(reason),
                fatal=False,
            )
    return blockers


def _accepted_gcd_actions_from_events(execution: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    events = execution.get("sink_events")
    if not isinstance(events, list):
        return result
    for event in events:
        if not isinstance(event, Mapping):
            continue
        source = event.get("source_sink")
        acceptance = event.get("client_acceptance")
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


_RESULT_BEARING_GCD_ACTIONS = frozenset(
    {
        "warrior.bloodthirst",
        "warrior.whirlwind",
        "warrior.slam",
        "warrior.execute",
        "warrior.hamstring",
        "warrior.pummel",
        "warrior.sunder_armor",
    }
)


def _audit_gcd_result_followups(
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
        acceptance = event.get("client_acceptance")
        operation = event.get("operation_contract")
        action = operation.get("canonical_action") if isinstance(operation, Mapping) else None
        if not (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
            and action in _RESULT_BEARING_GCD_ACTIONS
        ):
            continue
        followup = event.get("server_result_followup")
        if (
            isinstance(followup, Mapping)
            and followup.get("status") == "PENDING_TYPED_RESULT_EVENT"
        ):
            continue
        if (
            not isinstance(followup, Mapping)
            or followup.get("status") != "OBSERVED_TYPED_RESULT_EVENT"
        ):
            blockers.append(
                _blocker(
                    "ACCEPTED_GCD_SERVER_RESULT_UNOBSERVED",
                    f"accepted result-bearing GCD {action} has no typed follow-up event",
                    execution_fatal=True,
                    decision_index=decision_index,
                )
            )
            continue
        followup_events = followup.get("events")
        expected_action = operation.get("action_ref")
        if (
            not isinstance(followup_events, list)
            or len(followup_events) != 1
            or not isinstance(followup_events[0], Mapping)
        ):
            blockers.append(
                _blocker(
                    "ACCEPTED_GCD_SERVER_RESULT_CARDINALITY_INVALID",
                    f"accepted result-bearing GCD {action} requires exactly one result event",
                    execution_fatal=True,
                    decision_index=decision_index,
                )
            )
            continue
        observed_action = followup_events[0].get("action")
        if not isinstance(expected_action, Mapping) or observed_action != expected_action:
            blockers.append(
                _blocker(
                    "ACCEPTED_GCD_SERVER_RESULT_ACTION_MISMATCH",
                    f"result event action does not match accepted GCD {action}",
                    execution_fatal=True,
                    decision_index=decision_index,
                    evidence={
                        "expected_action": expected_action,
                        "observed_action": observed_action,
                    },
                )
            )
    return blockers


def _attempt_partition_blocker(
    *,
    requested: Sequence[str],
    resolved: Sequence[str],
    pending: Sequence[str],
) -> JSONMap | None:
    resolved_set = set(resolved)
    pending_set = set(pending)
    requested_set = set(requested)
    valid = (
        len(resolved_set) == len(resolved)
        and len(pending_set) == len(pending)
        and not (resolved_set & pending_set)
        and resolved_set | pending_set == requested_set
        and list(resolved)
        == [attempt_id for attempt_id in requested if attempt_id in resolved_set]
        and list(pending)
        == [attempt_id for attempt_id in requested if attempt_id in pending_set]
    )
    if valid:
        return None
    return _blocker(
        "BRIDGE_SERVER_RESULT_ATTEMPT_ALIGNMENT_MISMATCH",
        "resolved and pending result IDs must form an ordered partition of "
        "the requested outstanding ledger",
        execution_fatal=True,
        evidence={
            "requested_attempt_ids": list(requested),
            "resolved_attempt_ids": list(resolved),
            "pending_attempt_ids": list(pending),
        },
    )


def _observe_server_boundary(
    bridge: FullPolicyBridgeLikeV2,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    capabilities: Mapping[str, bool],
    attempt_ids: Sequence[str],
    attempt_sinks: Mapping[str, Mapping[str, Any]],
) -> tuple[JSONMap, JSONMap | None, tuple[str, ...]]:
    result: JSONMap = {
        "state_delta": {
            "status": "OBSERVED_AGGREGATE_SIMULATOR_STATE",
            "time_before_ms": before["time_ms"],
            "time_after_ms": after["time_ms"],
            "damage_before": before["damage_done"],
            "damage_after": after["damage_done"],
            "damage_delta": after["damage_done"] - before["damage_done"],
            "individual_sink_attribution": "NOT_AVAILABLE_FROM_AGGREGATE_STATE",
        },
        "result_stream": {
            "status": "UNKNOWN_RESULT_STREAM_NOT_EXPOSED",
            "complete_through_time_ms": None,
            "events": [],
            "pending_attempt_ids": list(attempt_ids),
        },
        "attempt_ids": list(attempt_ids),
    }
    if not capabilities["server_results_since_last_decision"]:
        return result, None, tuple(attempt_ids)
    try:
        batch = bridge.server_results_since_last_decision(  # type: ignore[attr-defined]
            tuple(attempt_ids)
        )
    except Exception as error:
        return (
            result,
            _bridge_error("BRIDGE_SERVER_RESULT_STREAM_ERROR", error, fatal=True),
            tuple(attempt_ids),
        )
    if not isinstance(batch, ServerResultBatchV2):
        return (
            result,
            _blocker(
                "BRIDGE_SERVER_RESULT_STREAM_UNTYPED",
                "server_results_since_last_decision must return ServerResultBatchV2",
                execution_fatal=True,
            ),
            tuple(attempt_ids),
        )
    observed_attempt_ids = tuple(event.attempt_id for event in batch.events)
    partition_blocker = _attempt_partition_blocker(
        requested=tuple(attempt_ids),
        resolved=observed_attempt_ids,
        pending=batch.pending_attempt_ids,
    )
    if partition_blocker is not None:
        return result, partition_blocker, tuple(attempt_ids)
    state_time_ms = int(after["time_ms"])
    if batch.complete_through_time_ms != state_time_ms:
        return (
            result,
            _blocker(
                "BRIDGE_SERVER_RESULT_COMPLETENESS_TIME_MISMATCH",
                "result completeness must equal the returned simulator-state time",
                execution_fatal=True,
                evidence={
                    "complete_through_time_ms": batch.complete_through_time_ms,
                    "state_time_ms": state_time_ms,
                },
            ),
            tuple(attempt_ids),
        )
    for event in batch.events:
        sink = attempt_sinks.get(event.attempt_id)
        operation = sink.get("operation_contract") if isinstance(sink, Mapping) else None
        expected_action = (
            operation.get("action_ref") if isinstance(operation, Mapping) else None
        )
        if not isinstance(expected_action, Mapping) or event.action.to_wire() != expected_action:
            return result, _blocker(
                "ACCEPTED_GCD_SERVER_RESULT_ACTION_MISMATCH",
                "result event action differs from its accepted source sink",
                execution_fatal=True,
                evidence={
                    "attempt_id": event.attempt_id,
                    "expected_action": expected_action,
                    "observed_action": event.action.to_wire(),
                },
            ), tuple(attempt_ids)
        source_attempt = (
            sink.get("source_attempt") if isinstance(sink, Mapping) else None
        )
        accepted_at = (
            source_attempt.get("accepted_at_time_ms")
            if isinstance(source_attempt, Mapping)
            else None
        )
        if (
            isinstance(accepted_at, bool)
            or not isinstance(accepted_at, int)
            or accepted_at < 0
        ):
            return result, _blocker(
                "ACCEPTED_GCD_TIMESTAMP_MISSING",
                "result-bearing source sink lacks its acceptance timestamp",
                execution_fatal=True,
                evidence={"attempt_id": event.attempt_id},
            ), tuple(attempt_ids)
        if event.time_ms < accepted_at:
            return (
                result,
                _blocker(
                    "BRIDGE_SERVER_RESULT_TIME_BEFORE_ACCEPTANCE",
                    "result event precedes the accepted action attempt",
                    execution_fatal=True,
                    evidence={
                        "attempt_id": event.attempt_id,
                        "accepted_at_time_ms": accepted_at,
                        "event_time_ms": event.time_ms,
                    },
                ),
                tuple(attempt_ids),
            )
        if event.time_ms > state_time_ms:
            return (
                result,
                _blocker(
                    "BRIDGE_SERVER_RESULT_TIME_AFTER_STATE",
                    "result event time exceeds the returned simulator-state time",
                    execution_fatal=True,
                    evidence={
                        "attempt_id": event.attempt_id,
                        "event_time_ms": event.time_ms,
                        "state_time_ms": state_time_ms,
                    },
                ),
                tuple(attempt_ids),
            )
    result["result_stream"] = {
        "status": "OBSERVED_TYPED_RESULT_STREAM",
        "complete_through_time_ms": batch.complete_through_time_ms,
        "events": [event.to_dict() for event in batch.events],
        "pending_attempt_ids": list(batch.pending_attempt_ids),
    }
    return result, None, batch.pending_attempt_ids


def _unknown_server_boundary(
    state: Mapping[str, Any],
    attempt_ids: Sequence[str],
    capabilities: Mapping[str, bool],
) -> JSONMap:
    return {
        "state_delta": {
            "status": "NOT_OBSERVED_WITHOUT_ADVANCE",
            "time_before_ms": state["time_ms"],
            "time_after_ms": state["time_ms"],
            "damage_before": state["damage_done"],
            "damage_after": state["damage_done"],
            "damage_delta": 0.0,
            "individual_sink_attribution": "NOT_AVAILABLE_FROM_AGGREGATE_STATE",
        },
        "result_stream": {
            "status": (
                "PENDING_ADVANCE"
                if capabilities["server_results_since_last_decision"]
                else "UNKNOWN_RESULT_STREAM_NOT_EXPOSED"
            ),
            "complete_through_time_ms": None,
            "events": [],
            "pending_attempt_ids": list(attempt_ids),
        },
        "attempt_ids": list(attempt_ids),
    }


def _right_censor_terminal_active_hardcast(
    state: Mapping[str, Any],
    attempt_ids: Sequence[str],
    attempt_sinks: Mapping[str, Mapping[str, Any]],
) -> tuple[JSONMap | None, JSONMap | None, tuple[str, ...]]:
    """Right-censor one still-casting result exactly at the scoring horizon.

    The scored simulator state is already terminal, so an active hardcast has
    contributed no post-horizon damage.  Only the one attempt whose action is
    byte-for-byte equal to ``current_cast.action`` may be censored.  Any other
    pending ledger remains fatal instead of being silently discarded.
    """

    if len(attempt_ids) != 1:
        return None, None, tuple(attempt_ids)
    current_cast = state.get("current_cast")
    cast_action = (
        current_cast.get("action") if isinstance(current_cast, Mapping) else None
    )
    if not isinstance(cast_action, Mapping):
        return None, None, tuple(attempt_ids)
    attempt_id = str(attempt_ids[0])
    sink = attempt_sinks.get(attempt_id)
    operation = sink.get("operation_contract") if isinstance(sink, Mapping) else None
    expected_action = (
        operation.get("action_ref") if isinstance(operation, Mapping) else None
    )
    if not isinstance(expected_action, Mapping) or dict(expected_action) != dict(
        cast_action
    ):
        return None, None, tuple(attempt_ids)
    source_attempt = sink.get("source_attempt") if isinstance(sink, Mapping) else None
    accepted_at = (
        source_attempt.get("accepted_at_time_ms")
        if isinstance(source_attempt, Mapping)
        else None
    )
    horizon = state.get("time_ms")
    if (
        isinstance(accepted_at, bool)
        or not isinstance(accepted_at, int)
        or isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or accepted_at < 0
        or horizon < accepted_at
    ):
        return None, _blocker(
            "TERMINAL_HARDCAST_CENSOR_TIMESTAMP_INVALID",
            "active terminal hardcast lacks a causal acceptance/horizon timestamp",
            execution_fatal=True,
            evidence={
                "attempt_id": attempt_id,
                "accepted_at_time_ms": accepted_at,
                "scoring_horizon_ms": horizon,
            },
        ), tuple(attempt_ids)
    if isinstance(sink, dict):
        sink["server_result_followup"] = {
            "status": "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
            "events": [],
            "pending_attempt_ids": [],
            "attempt_id": attempt_id,
            "acceptance_is_not_result": True,
            "scoring_horizon_ms": horizon,
            "post_horizon_damage_scored": False,
        }
    observation: JSONMap = {
        "state_delta": {
            "status": "SCORING_STATE_FROZEN_AT_EXACT_HORIZON",
            "time_before_ms": horizon,
            "time_after_ms": horizon,
            "damage_before": state.get("damage_done"),
            "damage_after": state.get("damage_done"),
            "damage_delta": 0.0,
            "individual_sink_attribution": "RIGHT_CENSORED_NO_RESULT",
        },
        "result_stream": {
            "status": "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
            "complete_through_time_ms": horizon,
            "events": [],
            "pending_attempt_ids": [],
            "censored_attempt_ids": [attempt_id],
            "post_horizon_damage_scored": False,
        },
        "attempt_ids": [attempt_id],
    }
    return observation, None, ()


def _annotate_attempt_ids(execution: JSONMap, decision_index: int) -> tuple[str, ...]:
    events = execution.get("sink_events")
    if not isinstance(events, list):
        raise FuryFullPolicyRolloutV2Error("ordered execution lacks sink_events")
    attempt_ids: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            raise FuryFullPolicyRolloutV2Error("ordered sink event must be an object")
        order = event.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
            raise FuryFullPolicyRolloutV2Error("ordered sink event order is invalid")
        attempt_id = f"decision-{decision_index}:sink-{order}"
        source_attempt = event.get("source_attempt")
        if not isinstance(source_attempt, dict):
            raise FuryFullPolicyRolloutV2Error("sink event lacks source_attempt")
        existing_attempt_id = source_attempt.get("attempt_id")
        if existing_attempt_id is not None and existing_attempt_id != attempt_id:
            raise FuryFullPolicyRolloutV2Error(
                "ordered sink executor attempt ID differs from rollout ledger"
            )
        source_attempt["attempt_id"] = attempt_id
        source = event.get("source_sink")
        submission = event.get("simulator_submission")
        acceptance = event.get("client_acceptance")
        operation = event.get("operation_contract")
        canonical_action = (
            operation.get("canonical_action")
            if isinstance(operation, Mapping)
            else None
        )
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
            and canonical_action in _RESULT_BEARING_GCD_ACTIONS
        ):
            attempt_ids.append(attempt_id)
    return tuple(attempt_ids)


def _register_result_attempt_sinks(
    execution: Mapping[str, Any],
    attempt_ids: Sequence[str],
    registry: dict[str, JSONMap],
) -> None:
    expected = set(attempt_ids)
    observed: set[str] = set()
    events = execution.get("sink_events")
    if not isinstance(events, list):
        raise FuryFullPolicyRolloutV2Error("ordered execution lacks sink_events")
    for event in events:
        if not isinstance(event, dict):
            continue
        source_attempt = event.get("source_attempt")
        attempt_id = (
            source_attempt.get("attempt_id")
            if isinstance(source_attempt, Mapping)
            else None
        )
        if attempt_id not in expected:
            continue
        if attempt_id in registry or attempt_id in observed:
            raise FuryFullPolicyRolloutV2Error(
                f"duplicate result-bearing attempt ID {attempt_id!r}"
            )
        immediate_state = event.get("simulator_state_after_immediate")
        if not isinstance(immediate_state, Mapping):
            raise FuryFullPolicyRolloutV2Error(
                "accepted result-bearing sink lacks immediate simulator state"
            )
        accepted_at = immediate_state.get("time_ms")
        if (
            isinstance(accepted_at, bool)
            or not isinstance(accepted_at, int)
            or accepted_at < 0
        ):
            raise FuryFullPolicyRolloutV2Error(
                "accepted result-bearing sink has an invalid acceptance timestamp"
            )
        if not isinstance(source_attempt, dict):
            raise FuryFullPolicyRolloutV2Error(
                "accepted result-bearing sink lacks a mutable source_attempt"
            )
        source_attempt["accepted_at_time_ms"] = accepted_at
        registry[str(attempt_id)] = event
        observed.add(str(attempt_id))
    if observed != expected:
        raise FuryFullPolicyRolloutV2Error(
            "accepted result-bearing attempt IDs do not match sink events"
        )


def _annotate_immediate_state_deltas(execution: JSONMap) -> None:
    """Expose aggregate immediate deltas without calling them server results."""

    events = execution.get("sink_events")
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, dict):
            continue
        before = event.get("simulator_state_before")
        after = event.get("simulator_state_after_immediate")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            event["immediate_aggregate_state_delta"] = {
                "status": "NOT_OBSERVED_NO_SUBMITTED_STATE_TRANSITION",
                "individual_sink_server_outcome": "UNKNOWN",
            }
            continue
        try:
            time_delta = int(after["time_ms"]) - int(before["time_ms"])
            damage_delta = float(after["damage_done"]) - float(before["damage_done"])
        except (KeyError, TypeError, ValueError):
            event["immediate_aggregate_state_delta"] = {
                "status": "UNTYPED_STATE_DELTA",
                "individual_sink_server_outcome": "UNKNOWN",
            }
            continue
        event["immediate_aggregate_state_delta"] = {
            "status": "OBSERVED_AGGREGATE_SIMULATOR_STATE",
            "time_delta_ms": time_delta,
            "damage_delta": damage_delta,
            "individual_sink_server_outcome": "UNKNOWN_UNTIL_RESULT_STREAM",
            "acceptance_is_not_result": True,
        }


def _attach_result_followups(
    execution: JSONMap, observation: Mapping[str, Any]
) -> None:
    stream = observation.get("result_stream")
    events = stream.get("events") if isinstance(stream, Mapping) else []
    if not isinstance(events, list):
        events = []
    by_attempt: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if isinstance(event, Mapping) and isinstance(event.get("attempt_id"), str):
            by_attempt.setdefault(str(event["attempt_id"]), []).append(event)
    pending_values = (
        stream.get("pending_attempt_ids") if isinstance(stream, Mapping) else []
    )
    pending = {
        str(attempt_id)
        for attempt_id in pending_values
        if isinstance(attempt_id, str)
    }
    sink_events = execution.get("sink_events")
    if not isinstance(sink_events, list):
        return
    for sink in sink_events:
        if isinstance(sink, dict):
            _attach_sink_result_followup(sink, stream, by_attempt, pending)


def _attach_registered_result_followups(
    registry: Mapping[str, JSONMap], observation: Mapping[str, Any]
) -> None:
    stream = observation.get("result_stream")
    events = stream.get("events") if isinstance(stream, Mapping) else []
    if not isinstance(events, list):
        events = []
    by_attempt: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if isinstance(event, Mapping) and isinstance(event.get("attempt_id"), str):
            by_attempt.setdefault(str(event["attempt_id"]), []).append(event)
    pending_values = (
        stream.get("pending_attempt_ids") if isinstance(stream, Mapping) else []
    )
    pending = {
        str(attempt_id)
        for attempt_id in pending_values
        if isinstance(attempt_id, str)
    }
    for sink in registry.values():
        _attach_sink_result_followup(sink, stream, by_attempt, pending)


def _attach_sink_result_followup(
    sink: JSONMap,
    stream: Any,
    by_attempt: Mapping[str, list[Mapping[str, Any]]],
    pending: set[str],
) -> None:
    existing = sink.get("server_result_followup")
    if (
        isinstance(existing, Mapping)
        and existing.get("status") == "OBSERVED_TYPED_RESULT_EVENT"
    ):
        return
    attempt = sink.get("source_attempt")
    attempt_id = attempt.get("attempt_id") if isinstance(attempt, Mapping) else None
    matched = by_attempt.get(str(attempt_id), [])
    if matched:
        sink["server_result_followup"] = {
            "status": "OBSERVED_TYPED_RESULT_EVENT",
            "events": [dict(item) for item in matched],
            "acceptance_is_not_result": True,
        }
    elif isinstance(attempt_id, str) and attempt_id in pending:
        sink["server_result_followup"] = {
            "status": "PENDING_TYPED_RESULT_EVENT",
            "events": [],
            "acceptance_is_not_result": True,
        }
    elif (
        isinstance(stream, Mapping)
        and stream.get("status") == "OBSERVED_TYPED_RESULT_STREAM"
    ):
        sink["server_result_followup"] = {
            "status": "NO_MATCHING_RESULT_EVENT_IN_COMPLETE_BATCH",
            "events": [],
            "acceptance_is_not_result": True,
        }
    else:
        sink["server_result_followup"] = {
            "status": "UNKNOWN_RESULT_STREAM_NOT_EXPOSED",
            "events": [],
            "acceptance_is_not_result": True,
        }


def _artifact(
    *,
    expert_id: str,
    seed: int,
    status: str,
    capabilities: Mapping[str, bool] | None = None,
    blockers: Sequence[Mapping[str, Any]] = (),
    final_state: Mapping[str, Any] | None = None,
) -> JSONMap:
    blocker_rows = [dict(item) for item in blockers]
    return {
        "schema": ROLLOUT_SCHEMA,
        "status": status,
        "expert_id": expert_id,
        "seed": seed,
        "source_execution": False,
        "exact_lua_replay": False,
        "fallback": {"used": False, "allowed": False},
        "scenario_complete": False,
        "ordered_projection_faithful": False,
        "simulator_dps_comparison_eligible": False,
        "bridge_capabilities": dict(capabilities or {}),
        "bridge_command_contract": {
            "initial_load_command": "load",
            "load_state_actions_act_wait_advance": (
                "source-driven full-scenario loop; no fallback command"
            ),
            "cancel_queue": (
                "capability-audited; never invoked unless an audited raw source "
                "cancellation operation exists (none in current Cat/Contra v2)"
            ),
            "set_target": (
                "capability-audited; never invoked unless an audited raw source "
                "target-selection operation exists (none in current Cat/Contra v2)"
            ),
            "start_attack_stop_cast": (
                "optional bridge controls invoked only by ordered raw source sinks"
            ),
            "server_results_since_last_decision": (
                "typed follow-up evidence; immediate act acceptance is never a result"
            ),
        },
        "blockers": blocker_rows,
        "blocker_summary": _blocker_summary(blocker_rows),
        "final_state": None if final_state is None else dict(final_state),
        "claims_excluded": [
            "original Cat, Contra, or Contra260817 Lua execution",
            "act acceptance as hit, crit, miss, or damage outcome",
            "DPS superiority when any blocker is present",
            "sensitivity target inputs as exact target truth",
        ],
    }


def _terminal_without_bridge(
    *,
    expert_id: str,
    seed: int,
    status: str,
    blocker: Mapping[str, Any],
    required_baseline: bool = False,
) -> JSONMap:
    result = _artifact(
        expert_id=expert_id,
        seed=seed,
        status=status,
        blockers=(blocker,),
    )
    result["bridge_mutated"] = False
    result["required_baseline"] = required_baseline
    return result


def _blocker(
    code: str,
    message: str,
    *,
    execution_fatal: bool,
    decision_index: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> JSONMap:
    return {
        "code": code,
        "message": message,
        "execution_fatal": execution_fatal,
        "comparison_fatal": True,
        "decision_index": decision_index,
        "evidence": dict(evidence or {}),
    }


def _bridge_error(
    code: str,
    error: Exception,
    *,
    fatal: bool,
    decision_index: int | None = None,
) -> JSONMap:
    return _blocker(
        code,
        f"bridge boundary raised {type(error).__name__}",
        execution_fatal=fatal,
        decision_index=decision_index,
        evidence={"error_type": type(error).__name__},
    )


def _blocker_summary(blockers: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    counts = Counter(str(item.get("code", "UNKNOWN")) for item in blockers)
    return [
        {"code": code, "count": count}
        for code, count in sorted(counts.items())
    ]


def _context_receipt(context: TargetSemanticsContextV2) -> JSONMap:
    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": context.target_index,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "equipped_item_count": len(context.equipped_item_names),
        "target_max_health": context.target_max_health,
        "health_pct_schedule": [
            {"time_ms": point.time_ms, "health_pct": point.health_pct}
            for point in context.health_pct_schedule
        ],
        "exact_by_declared_contract": (
            context.mode is TargetSemanticsModeV2.DECLARED_EXACT
        ),
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        },
    }


def _completion_receipt(
    request: Mapping[str, Any],
    root_state: Mapping[str, Any],
    final_state: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV1 | None = None,
) -> JSONMap:
    encounter = request["encounter"]
    assert isinstance(encounter, Mapping)
    if dynamic_load is not None:
        dynamic_state = final_state.get("dynamic_team_background")
        targets = (
            dynamic_state.get("targets")
            if isinstance(dynamic_state, Mapping)
            else None
        )
        all_targets_dead = (
            isinstance(targets, list)
            and len(targets) == len(dynamic_load.config.target_health)
            and all(
                isinstance(target, Mapping) and target.get("dead") is True
                for target in targets
            )
        )
        duration = _finite_number(encounter.get("duration"), "encounter.duration")
        watchdog_horizon_ms = round(duration * 1000.0)
        elapsed_ms = int(final_state["time_ms"]) - int(root_state["time_ms"])
        within_watchdog = 0 <= elapsed_ms <= watchdog_horizon_ms
        return {
            "mode": "DYNAMIC_ALL_TARGETS_DEAD",
            "criterion_met": (
                bool(final_state.get("finished"))
                and final_state.get("num_targets") == 0
                and all_targets_dead
                and within_watchdog
            ),
            "bridge_finished": bool(final_state.get("finished")),
            "all_targets_dead": all_targets_dead,
            "live_target_count": final_state.get("num_targets"),
            "target_count": len(dynamic_load.config.target_health),
            "elapsed_ms": elapsed_ms,
            "watchdog_horizon_ms": watchdog_horizon_ms,
            "within_watchdog_horizon": within_watchdog,
        }
    health_mode = encounter.get("useHealth") is True
    if health_mode:
        return {
            "mode": "SIMULATOR_HEALTH_COMPLETION",
            "criterion_met": bool(final_state.get("finished")),
            "bridge_finished": bool(final_state.get("finished")),
            "remaining_ms": final_state.get("remaining_ms"),
        }
    duration = _finite_number(encounter.get("duration"), "encounter.duration")
    if duration <= 0:
        raise FuryFullPolicyRolloutV2Error("encounter.duration must be positive")
    configured_ms = round(duration * 1000.0)
    root_time = int(root_state["time_ms"])
    final_time = int(final_state["time_ms"])
    remaining = final_state.get("remaining_ms")
    remaining_zero = (
        isinstance(remaining, (int, float))
        and not isinstance(remaining, bool)
        and float(remaining) <= 0.0
    )
    configured_end_time_ms = root_time + configured_ms
    exact_end_time = final_time == configured_end_time_ms
    return {
        "mode": "CONFIGURED_DURATION",
        "criterion_met": bool(final_state.get("finished"))
        and remaining_zero
        and exact_end_time,
        "bridge_finished": bool(final_state.get("finished")),
        "configured_duration_ms": configured_ms,
        "root_time_ms": root_time,
        "configured_end_time_ms": configured_end_time_ms,
        "final_time_ms": final_time,
        "exact_end_time": exact_end_time,
        "remaining_ms": remaining,
        "remaining_zero": remaining_zero,
    }


def _interpolate_health(
    points: Sequence[HealthPercentPointV2], time_ms: int
) -> float:
    if time_ms <= points[0].time_ms:
        return float(points[0].health_pct)
    if time_ms >= points[-1].time_ms:
        return float(points[-1].health_pct)
    for left, right in zip(points, points[1:]):
        if left.time_ms <= time_ms <= right.time_ms:
            fraction = (time_ms - left.time_ms) / (right.time_ms - left.time_ms)
            return float(left.health_pct) + fraction * (
                float(right.health_pct) - float(left.health_pct)
            )
    raise AssertionError("validated schedule did not bracket time")


def _queued_swing(state: Mapping[str, Any]) -> SwingQueueOp:
    auras = state.get("auras")
    if not isinstance(auras, list):
        return SwingQueueOp.KEEP
    for aura in auras:
        if not isinstance(aura, Mapping):
            continue
        action = aura.get("action")
        if not isinstance(action, Mapping) or action.get("tag") != 1:
            continue
        if action.get("spell_id") in {11567, 25286}:
            return SwingQueueOp.HEROIC_STRIKE
        if action.get("spell_id") == 20569:
            return SwingQueueOp.CLEAVE
    return SwingQueueOp.KEEP


def _available_action(row: AvailableAction) -> JSONMap:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


def _validated_state(value: Any, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must return a mapping")
    state = dict(value)
    _nonnegative_int(state.get("time_ms"), f"{label}.time_ms")
    if not isinstance(state.get("finished"), bool):
        raise TypeError(f"{label}.finished must be boolean")
    if not isinstance(state.get("needs_input"), bool):
        raise TypeError(f"{label}.needs_input must be boolean")
    _finite_number(state.get("damage_done"), f"{label}.damage_done")
    return state


def _state_fingerprint(state: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        state.get("time_ms"),
        state.get("finished"),
        state.get("needs_input"),
        state.get("damage_done"),
        state.get("target_index", 0),
        state.get("target_health_known"),
        state.get("target_health_percent"),
        state.get("target_health_max"),
        state.get("num_targets"),
        state.get("total_target_count"),
        state.get("gcd_remaining_ms"),
        state.get("current_cast"),
        state.get("auras"),
        _jsonable(state.get("dynamic_team_background")),
    )


def _state_transition_blocker(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    command: str,
    decision_index: int | None = None,
) -> JSONMap | None:
    before_time = int(before["time_ms"])
    after_time = int(after["time_ms"])
    before_damage = float(before["damage_done"])
    after_damage = float(after["damage_done"])
    if after_time < before_time:
        return _blocker(
            "SIMULATOR_TIME_MOVED_BACKWARD",
            f"{command} moved simulator time backward",
            execution_fatal=True,
            decision_index=decision_index,
            evidence={"before_time_ms": before_time, "after_time_ms": after_time},
        )
    if after_damage < before_damage - 1e-9:
        return _blocker(
            "SIMULATOR_DAMAGE_COUNTER_DECREASED",
            f"{command} decreased cumulative damage_done",
            execution_fatal=True,
            decision_index=decision_index,
            evidence={"before_damage": before_damage, "after_damage": after_damage},
        )
    if (
        command != "ordered_source_sinks"
        and after_time == before_time
        and _state_fingerprint(before) == _state_fingerprint(after)
    ):
        return _blocker(
            "SIMULATOR_COMMAND_MADE_NO_PROGRESS",
            f"{command} returned an unchanged state",
            execution_fatal=True,
            decision_index=decision_index,
        )
    return None


def _validate_request(request: Mapping[str, Any]) -> None:
    if not isinstance(request, Mapping):
        raise TypeError("raid_sim_request must be a mapping")
    targets = _request_targets(request)
    if not targets:
        raise FuryFullPolicyRolloutV2Error("RaidSimRequest has no encounter targets")
    encounter = request["encounter"]
    assert isinstance(encounter, Mapping)
    duration = _finite_number(encounter.get("duration"), "encounter.duration")
    if duration <= 0:
        raise FuryFullPolicyRolloutV2Error("encounter.duration must be positive")
    _player_distance(request)


def _dynamic_config_from_wire(value: Any) -> DynamicTeamBackgroundConfigV1:
    if not isinstance(value, Mapping):
        raise TypeError("dynamic load config wire value must be a mapping")
    expected_fields = {
        "schema",
        "content_sha256",
        "target_health",
        "background_damage_events",
        "same_timestamp_order",
        "retarget_mode",
    }
    if set(value) != expected_fields:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load config wire field set mismatch"
        )
    if value.get("schema") != "o2o_dynamic_team_background/v1":
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load config wire schema is unsupported"
        )
    raw_health = value.get("target_health")
    raw_events = value.get("background_damage_events")
    if not isinstance(raw_health, list) or any(
        not isinstance(row, Mapping) for row in raw_health
    ):
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load target_health must be an array of objects"
        )
    if not isinstance(raw_events, list) or any(
        not isinstance(row, Mapping) for row in raw_events
    ):
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load background_damage_events must be an array of objects"
        )
    try:
        config = DynamicTeamBackgroundConfigV1(
            target_health=tuple(
                DynamicTargetHealthV1(
                    target_index=_strict_int(
                        row.get("target_index"), "dynamic target_index"
                    ),
                    health=_finite_number(row.get("health"), "dynamic target health"),
                )
                for row in raw_health
            ),
            background_damage_events=tuple(
                BackgroundDamageEventV1(
                    schedule_index=_strict_int(
                        row.get("schedule_index"), "dynamic schedule_index"
                    ),
                    time_ms=_strict_int(row.get("time_ms"), "dynamic event time_ms"),
                    target_index=_strict_int(
                        row.get("target_index"), "dynamic event target_index"
                    ),
                    event_id=_nonempty(row.get("event_id"), "dynamic event_id"),
                    damage=_finite_number(row.get("damage"), "dynamic event damage"),
                )
                for row in raw_events
            ),
            same_timestamp_order=_nonempty(
                value.get("same_timestamp_order"), "dynamic same_timestamp_order"
            ),
            retarget_mode=_nonempty(value.get("retarget_mode"), "dynamic retarget_mode"),
        )
    except (TypeError, ValueError) as error:
        raise FuryFullPolicyRolloutV2Error(
            f"dynamic load config wire is invalid: {error}"
        ) from error
    if value.get("content_sha256") != config.content_sha256:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load config content SHA-256 mismatch"
        )
    return config


def _validate_dynamic_load_request_v1(
    dynamic_load: DynamicRolloutLoadV1,
    request: Mapping[str, Any],
    *,
    request_sha256: str,
) -> None:
    if dynamic_load.request_sha256 != request_sha256:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load request SHA-256 differs from raid_sim_request"
        )
    encounter = request.get("encounter")
    assert isinstance(encounter, Mapping)
    if encounter.get("useHealth") is not True:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load requires encounter.useHealth == true"
        )
    targets = _request_targets(request)
    if len(targets) != len(dynamic_load.config.target_health):
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load target count differs from raid_sim_request"
        )
    for index, configured in enumerate(dynamic_load.config.target_health):
        stats = targets[index].get("stats")
        if not isinstance(stats, list) or len(stats) <= _HEALTH_STAT_INDEX:
            raise FuryFullPolicyRolloutV2Error(
                f"dynamic request target {index} lacks explicit stats[34] health"
            )
        request_health = _finite_number(
            stats[_HEALTH_STAT_INDEX], f"dynamic request target {index} health"
        )
        if struct.pack(">d", request_health) != struct.pack(">d", configured.health):
            raise FuryFullPolicyRolloutV2Error(
                f"dynamic request target {index} health bits differ from config"
            )


def _validate_dynamic_load_result_v1(
    dynamic_load: DynamicRolloutLoadV1,
    result: DynamicLoadResultV1,
) -> None:
    receipt = result.receipt
    if not isinstance(receipt, DynamicLoadReceiptV1):
        raise TypeError("dynamic load result receipt must be DynamicLoadReceiptV1")
    config = dynamic_load.config
    expected = (
        "o2o_dynamic_team_background/v1",
        config.content_sha256,
        len(config.target_health),
        len(config.background_damage_events),
        config.same_timestamp_order,
        config.retarget_mode,
    )
    observed = (
        receipt.schema,
        receipt.config_digest,
        receipt.target_count,
        receipt.background_event_count,
        receipt.same_timestamp_order,
        receipt.retarget_mode,
    )
    if observed != expected or receipt.environment_generation <= 0:
        raise FuryFullPolicyRolloutV2Error(
            "bridge dynamic load receipt differs from the typed load contract"
        )


def _validate_dynamic_loaded_state_v1(
    state: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV1,
    receipt: DynamicLoadReceiptV1,
) -> None:
    dynamic = state.get("dynamic_team_background")
    if not isinstance(dynamic, Mapping):
        raise FuryFullPolicyRolloutV2Error(
            "dynamic load state lacks dynamic_team_background"
        )
    if (
        dynamic.get("schema") != receipt.schema
        or dynamic.get("config_digest") != receipt.config_digest
        or dynamic.get("environment_generation") != receipt.environment_generation
        or dynamic.get("same_timestamp_order") != receipt.same_timestamp_order
        or dynamic.get("retarget_mode") != receipt.retarget_mode
    ):
        raise FuryFullPolicyRolloutV2Error(
            "dynamic state binding differs from its load receipt"
        )
    targets = dynamic.get("targets")
    if not isinstance(targets, list) or len(targets) != receipt.target_count:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic state target rows differ from its load receipt"
        )
    for index, (target, configured) in enumerate(
        zip(targets, dynamic_load.config.target_health)
    ):
        if not isinstance(target, Mapping) or target.get("target_index") != index:
            raise FuryFullPolicyRolloutV2Error(
                "dynamic state target identity is not contiguous"
            )
        initial = _finite_number(
            target.get("initial_health"), f"dynamic state target {index} initial_health"
        )
        if struct.pack(">d", initial) != struct.pack(">d", configured.health):
            raise FuryFullPolicyRolloutV2Error(
                f"dynamic state target {index} health bits differ from config"
            )
    live_count = sum(
        1
        for target in targets
        if isinstance(target, Mapping) and target.get("dead") is False
    )
    if state.get("num_targets") != live_count:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic state num_targets differs from its live target rows"
        )
    total_count = state.get("total_target_count")
    if total_count != receipt.target_count:
        raise FuryFullPolicyRolloutV2Error(
            "dynamic state total_target_count differs from its load receipt"
        )


def _request_targets(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    encounter = request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise FuryFullPolicyRolloutV2Error("RaidSimRequest lacks encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or any(
        not isinstance(target, Mapping) for target in targets
    ):
        raise FuryFullPolicyRolloutV2Error(
            "RaidSimRequest encounter.targets must be an array of objects"
        )
    return targets


def _player_distance(request: Mapping[str, Any]) -> float:
    raid = request.get("raid")
    parties = raid.get("parties") if isinstance(raid, Mapping) else None
    if not isinstance(parties, list) or not parties:
        raise FuryFullPolicyRolloutV2Error("RaidSimRequest lacks raid.parties[0]")
    party = parties[0]
    players = party.get("players") if isinstance(party, Mapping) else None
    if not isinstance(players, list) or not players or not isinstance(players[0], Mapping):
        raise FuryFullPolicyRolloutV2Error(
            "RaidSimRequest lacks raid.parties[0].players[0]"
        )
    distance = _finite_number(
        players[0].get("distanceFromTarget", 5),
        "raid.parties[0].players[0].distanceFromTarget",
    )
    if distance < 0:
        raise FuryFullPolicyRolloutV2Error("distanceFromTarget must be non-negative")
    return distance


def _validate_context_map(
    contexts: Mapping[int, TargetSemanticsContextV2] | None,
) -> dict[int, TargetSemanticsContextV2]:
    if contexts is None:
        return {}
    if not isinstance(contexts, Mapping):
        raise TypeError("target_contexts must be a mapping or None")
    result: dict[int, TargetSemanticsContextV2] = {}
    for key, context in contexts.items():
        _nonnegative_int(key, "target_contexts key")
        if not isinstance(context, TargetSemanticsContextV2):
            raise TypeError("target_contexts values must be TargetSemanticsContextV2")
        if key != context.target_index:
            raise FuryFullPolicyRolloutV2Error(
                "target_contexts key must equal context.target_index"
            )
        result[key] = context
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, ContraFieldEvidenceV2):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _sha256_json(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FuryFullPolicyRolloutV2Error(
            f"raid_sim_request is not strict JSON: {error}"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _dynamic_load_binding_receipt_v1(
    dynamic_load: DynamicRolloutLoadV1,
    receipt: DynamicLoadReceiptV1 | None,
) -> JSONMap:
    bridge_receipt = None
    if receipt is not None:
        bridge_receipt = {
            "schema": receipt.schema,
            "config_digest": receipt.config_digest,
            "environment_generation": receipt.environment_generation,
            "target_count": receipt.target_count,
            "background_event_count": receipt.background_event_count,
            "same_timestamp_order": receipt.same_timestamp_order,
            "retarget_mode": receipt.retarget_mode,
        }
    return {
        "schema": DYNAMIC_LOAD_BINDING_SCHEMA,
        "contract_sha256": dynamic_load.contract_sha256,
        "request_sha256": dynamic_load.request_sha256,
        "simulator_seed": dynamic_load.seed,
        "config_digest": dynamic_load.config.content_sha256,
        "target_count": len(dynamic_load.config.target_health),
        "background_event_count": len(
            dynamic_load.config.background_damage_events
        ),
        "same_timestamp_order": dynamic_load.config.same_timestamp_order,
        "retarget_mode": dynamic_load.config.retarget_mode,
        "load_succeeded": receipt is not None,
        "bridge_receipt": bridge_receipt,
        "target_health_ieee754_binary64_hex": [
            struct.pack(">d", target.health).hex()
            for target in dynamic_load.config.target_health
        ],
    }


def _with_request_identity(
    artifact: JSONMap,
    request_sha256: str,
    *,
    dynamic_load: DynamicRolloutLoadV1 | None = None,
    dynamic_load_receipt: DynamicLoadReceiptV1 | None = None,
) -> JSONMap:
    artifact["request_sha256"] = request_sha256
    if dynamic_load is not None:
        artifact["bridge_command_contract"]["initial_load_command"] = (
            "load_dynamic_v1"
        )
        artifact["dynamic_load_binding"] = _dynamic_load_binding_receipt_v1(
            dynamic_load,
            dynamic_load_receipt,
        )
    return artifact


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed <= 0:
        raise FuryFullPolicyRolloutV2Error(f"{label} must be positive")
    return parsed


def _nonnegative_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed < 0:
        raise FuryFullPolicyRolloutV2Error(f"{label} must be non-negative")
    return parsed


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FuryFullPolicyRolloutV2Error(f"{label} must be finite")
    return result


def _bounded_percent(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if not 0.0 <= result <= 100.0:
        raise FuryFullPolicyRolloutV2Error(f"{label} must be within [0, 100]")
    return result


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{label} must be a nonempty string")
    return value


__all__ = (
    "CONTRA260817_POLICY_ID",
    "DYNAMIC_LOAD_BINDING_SCHEMA",
    "DYNAMIC_ROLLOUT_LOAD_SCHEMA",
    "DynamicRolloutLoadV1",
    "FullPolicyBridgeLikeV2",
    "FuryFullPolicyRolloutV2Error",
    "HealthPercentPointV2",
    "ROLLOUT_SCHEMA",
    "ServerResultBatchV2",
    "ServerResultEventV2",
    "ServerTargetResultV2",
    "TargetSemanticsContextV2",
    "TargetSemanticsModeV2",
    "contra260817_required_baseline_status_v2",
    "dynamic_rollout_load_from_adapter_wire_v1",
    "dynamic_rollout_load_from_config_wire_v1",
    "run_fury_full_policy_rollout_v2",
)
