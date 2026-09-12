"""Native dynamic-v5 diagnostic executor for the External Historical V2 policy.

The learned Historical V2 artifact is still blocked and non-voting.  This
module proves only that its legal-action API can drive the native
``load_dynamic_v3`` simulator contract without projecting through an older
dynamic load.  GCD actions are executed through an isolated ordered-sink
namespace; single-label non-GCD actions fail closed because their temporal
consumption semantics are not represented by the historical artifact.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import types
from typing import Any, Callable, Mapping, Sequence

from . import chronicle_external_historical_fury_policy_v2 as _policy
from . import fury_full_policy_rollout_v5 as _rollout_v5
from . import fury_ordered_sink_executor_v2 as _ordered_v2
from . import fury_paired_multiseed_runner_v4 as _runner_v4
from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    SwingQueueOp,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
)
from .sim_bridge import AvailableAction


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_simulator_rollout/v2"
IMPLEMENTATION_REVISION = "v2.0_native_dynamic_v5_diagnostic_fail_closed"
PRODUCER = "chronicle_external_historical_fury_simulator_runner_v2"
CONTENT_ADDRESS_SCHEMA = (
    "chronicle_external_historical_fury_simulator_rollout_content/v2"
)
TRACE_SCHEMA = "chronicle_external_historical_fury_runtime_trace/v2"
POLICY_ID = _policy.POLICY_ID
STATUS = _policy.STATUS_BLOCKED

_SELF_ACTION_KEYS = frozenset(
    {
        "warrior.battle_shout",
        "warrior.battle_stance",
        "warrior.berserker_stance",
        "warrior.bloodrage",
        "warrior.death_wish",
        "warrior.defensive_stance",
    }
)
_STANCE_ACTION_KEYS = frozenset(
    {
        "warrior.battle_stance",
        "warrior.berserker_stance",
        "warrior.defensive_stance",
    }
)
_OFF_GCD_ACTION_KEYS = frozenset(
    {"warrior.bloodrage", "warrior.death_wish"}
)
_ACTION_KEY_BY_REF = {value: key for key, value in ACTION_KEY_TO_REF.items()}
_QUEUE_BY_REF = {value: key for key, value in QUEUE_REFS.items()}
_CONTROLLABLE_SPELL_IDS = frozenset(
    ref.spell_id for ref in (*ACTION_KEY_TO_REF.values(), *QUEUE_REFS.values())
)

_PERMANENT_BLOCKERS = {
    "HISTORICAL_V2_ACTION_ONTOLOGY_INCLUDES_NONCONTROLLABLE_EVENTS": (
        "the frozen V2 labels START plus unpaired GO and includes proc events "
        "that are not player-controllable simulator actions"
    ),
    "HISTORICAL_V2_UNPAIRED_GO_LABELS_NOT_ACTION_ACCEPTANCE": (
        "an unpaired GO label does not prove a submitted and accepted player action"
    ),
    "HISTORICAL_PREFIX_SIMULATOR_PROJECTION_NOT_FIDELITY_ADMITTED": (
        "the simulator exposes full target and damage counters, not the exact "
        "historical strict-prefix event stream"
    ),
    "HISTORICAL_SINGLE_LABEL_NON_GCD_TEMPORAL_SEMANTICS_NOT_CLOSED": (
        "one historical categorical label cannot yet express a non-GCD action "
        "and a source-authored decision-consuming continuation"
    ),
    "HISTORICAL_ORDERED_CLIENT_SERVER_FIDELITY_NOT_CLOSED": (
        "no independent WoW client ordered action/acceptance/server-outcome "
        "fidelity artifact is bound"
    ),
    "HISTORICAL_PAIRED_RUNNER_V4_REGISTRATION_NOT_CLOSED": (
        "the formal runner-v4 lane remains intentionally unregistered"
    ),
}


class HistoricalFurySimulatorRunnerV2Error(RuntimeError):
    """The simulator adapter, runner envelope, or artifact is malformed."""


@dataclass(frozen=True)
class _HistoricalRuntimeFrame:
    simulator_state: Mapping[str, Any]
    available_actions: tuple[AvailableAction, ...]
    request: Mapping[str, Any]
    target: Mapping[str, Any]
    last_gcd_action: str


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _adapter_contract_sha256() -> str:
    return _canonical_sha256(
        {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "policy_id": POLICY_ID,
            "dynamic_load": "load_dynamic_v3",
            "executable_lane": "gcd_only",
            "non_gcd": "fail_closed",
            "comparison_ready": False,
        }
    )


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
        raise HistoricalFurySimulatorRunnerV2Error(
            f"{label} is not strict JSON: {error}"
        ) from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySimulatorRunnerV2Error(f"{label} must be an object")
    return value


def _integer_damage(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFurySimulatorRunnerV2Error(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise HistoricalFurySimulatorRunnerV2Error(
            f"{label} must be a finite nonnegative integer-valued amount"
        )
    return int(number)


def _content_sha(document: Mapping[str, Any], label: str) -> str:
    content = _mapping(document.get("content_address"), f"{label}.content_address")
    digest = content.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise HistoricalFurySimulatorRunnerV2Error(
            f"{label} lacks a lowercase SHA-256 content address"
        )
    return digest


def _policy_binding(bundle: Mapping[str, Any]) -> JSONMap:
    model = _mapping(bundle.get("model"), "historical bundle.model")
    admission = _mapping(bundle.get("admission"), "historical bundle.admission")
    evaluation = _mapping(bundle.get("evaluation"), "historical bundle.evaluation")
    prefix = _mapping(bundle.get("prefix_receipt"), "historical bundle.prefix_receipt")
    source = _mapping(model.get("source"), "historical model.source")
    accounting = _mapping(prefix.get("accounting"), "historical prefix.accounting")
    audit = _mapping(accounting.get("action_label_audit"), "action_label_audit")
    fitted = _mapping(
        _mapping(model.get("lanes"), "historical model.lanes")
        .get(_policy.FURY_LANE),
        "historical Fury lane",
    ).get("fitted_policy")
    catalog = _mapping(fitted, "historical Fury fitted_policy").get("action_catalog")
    if not isinstance(catalog, list):
        raise HistoricalFurySimulatorRunnerV2Error(
            "historical Fury action_catalog must be an array"
        )
    controllable = 0
    noncontrollable: list[JSONMap] = []
    for raw in catalog:
        if not isinstance(raw, str):
            raise HistoricalFurySimulatorRunnerV2Error(
                "historical Fury action_catalog entry must be canonical JSON text"
            )
        try:
            identity = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HistoricalFurySimulatorRunnerV2Error(
                "historical Fury action_catalog entry is not JSON"
            ) from error
        spell_id = identity.get("spell_id") if isinstance(identity, Mapping) else None
        if spell_id in _CONTROLLABLE_SPELL_IDS:
            controllable += 1
        else:
            noncontrollable.append(
                {"action_key": raw, "spell_id": spell_id}
            )
    return {
        "bundle_content_sha256": bundle.get("bundle_content_sha256"),
        "admission_content_sha256": _content_sha(admission, "admission"),
        "model_content_sha256": _content_sha(model, "model"),
        "evaluation_content_sha256": _content_sha(evaluation, "evaluation"),
        "prefix_receipt_content_sha256": _content_sha(prefix, "prefix receipt"),
        "source_bundle_sha256": source.get("source_bundle_sha256"),
        "policy_profile_sha256": model.get("policy_profile_sha256"),
        "adapter_interface_spec_sha256": model.get("adapter_interface_spec_sha256"),
        "simulator_adapter_contract_sha256": _adapter_contract_sha256(),
        "admission_status": admission.get("status"),
        "artifact_ready": admission.get("artifact_ready"),
        "outer_fidelity_status": _mapping(
            evaluation.get("outer_full_roster_evaluation"),
            "outer_full_roster_evaluation",
        ).get("fidelity_status"),
        "upstream_typed_blocker_codes": sorted(
            str(row.get("code"))
            for row in admission.get("typed_blockers", [])
            if isinstance(row, Mapping)
        ),
        "action_ontology": {
            "catalog_size": len(catalog),
            "controllable_catalog_entries": controllable,
            "noncontrollable_catalog_entries": len(noncontrollable),
            "noncontrollable_examples": noncontrollable[:8],
            "direct_unpaired_go_labels": audit.get("direct_unpaired_go_labels"),
            "labels_are_start_plus_unpaired_go": True,
            "runtime_mask_is_exact_bridge_legal_actions_only": True,
        },
    }


def build_historical_policy_descriptor_v2(
    admission_manifest_path: str | Path,
) -> JSONMap:
    """Build the runner-v4 policy identity without promoting its lane."""

    bundle = _policy.load_external_historical_fury_policy_v2_bundle(
        admission_manifest_path
    )
    binding = _policy_binding(bundle)
    return {
        "policy_id": POLICY_ID,
        "source_sha256": binding["model_content_sha256"],
        "adapter_sha256": binding["simulator_adapter_contract_sha256"],
        "profile_sha256": binding["policy_profile_sha256"],
        "role": "BASELINE_CANDIDATE_NONVOTING",
    }


def _observation(frame: _HistoricalRuntimeFrame) -> JSONMap:
    state = _mapping(frame.simulator_state, "simulator state")
    lifecycle = _mapping(
        state.get("dynamic_team_background"), "dynamic_team_background"
    )
    targets = lifecycle.get("targets")
    if not isinstance(targets, list) or any(
        not isinstance(row, Mapping) or not isinstance(row.get("dead"), bool)
        for row in targets
    ):
        raise HistoricalFurySimulatorRunnerV2Error(
            "dynamic team target lifecycle rows are unavailable"
        )
    elapsed = state.get("time_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
        raise HistoricalFurySimulatorRunnerV2Error(
            "simulator time_ms must be a nonnegative integer"
        )
    background_damage = _integer_damage(
        lifecycle.get("background_damage_applied"),
        "dynamic_team_background.background_damage_applied",
    )
    prefix_damage = _integer_damage(
        lifecycle.get("combined_damage_applied"),
        "dynamic_team_background.combined_damage_applied",
    )
    prefix_events = lifecycle.get("damage_applications_total")
    if (
        isinstance(prefix_events, bool)
        or not isinstance(prefix_events, int)
        or prefix_events < 0
    ):
        raise HistoricalFurySimulatorRunnerV2Error(
            "dynamic damage_applications_total must be a nonnegative integer"
        )
    hostile_last = frame.last_gcd_action not in _SELF_ACTION_KEYS and bool(
        frame.last_gcd_action
    )
    last_spell: str | JSONMap = "NONE"
    if frame.last_gcd_action:
        ref = ACTION_KEY_TO_REF.get(frame.last_gcd_action)
        if ref is not None:
            last_spell = {"id": ref.spell_id}
    observation = {
        "schema": "chronicle_external_v2_fury_prefix_observation/v1",
        "wave_elapsed_ms": elapsed,
        "observed_target_count": len(targets),
        "observed_dead_target_count": sum(row["dead"] is True for row in targets),
        "background_damage": background_damage,
        "background_dps": (
            0.0 if elapsed == 0 else background_damage * 1000.0 / elapsed
        ),
        "prefix_event_count": prefix_events,
        "prefix_damage": prefix_damage,
        "actor_has_last_target": hostile_last,
        "last_prefix_action_spell": last_spell,
    }
    _policy._runtime_contexts(observation)
    return observation


def _lane_and_action(row: AvailableAction) -> tuple[str, str, SwingQueueOp | None] | None:
    key = _ACTION_KEY_BY_REF.get(row.action)
    if key is not None:
        if row.triggers_gcd:
            return "gcd", key, None
        if key in _STANCE_ACTION_KEYS:
            return "stance", key, None
        if key in _OFF_GCD_ACTION_KEYS:
            return "off_gcd", key, None
        return None
    queue = _QUEUE_BY_REF.get(row.action)
    if queue is not None and not row.triggers_gcd:
        return "swing_queue", f"warrior.{queue.value.lower()}", queue
    return None


class ExternalHistoricalFuryDynamicV5AdapterV2:
    """Causal legal-action adapter; never an independent voting expert."""

    expert_id = POLICY_ID

    def __init__(self, bundle: Mapping[str, Any], *, simulator_seed: int) -> None:
        self._bundle = bundle
        self._model = _mapping(bundle.get("model"), "historical bundle.model")
        self.binding = _policy_binding(bundle)
        self.simulator_seed = simulator_seed
        self._decision_ordinal = 0

    @property
    def provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=POLICY_ID,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                f"historical-model:{self.binding['model_content_sha256']}",
            ),
            source_refs=("predict_action_distribution/sample_legal_action",),
        )

    def propose(self, frame: _HistoricalRuntimeFrame) -> ExpertDecision:
        if not isinstance(frame, _HistoricalRuntimeFrame):
            raise TypeError("historical adapter requires _HistoricalRuntimeFrame")
        observation = _observation(frame)
        legal_actions: list[JSONMap] = []
        excluded: list[JSONMap] = []
        hostile_last = observation["actor_has_last_target"] is True
        for row in frame.available_actions:
            resolved = _lane_and_action(row)
            if not row.legal or resolved is None or row.action.spell_id <= 0:
                excluded.append(
                    {
                        "index": row.index,
                        "action": row.action.to_wire(),
                        "legal": row.legal,
                        "reason": (
                            "NOT_LEGAL_AT_DECISION"
                            if not row.legal
                            else "NOT_EXACTLY_REPRESENTABLE"
                        ),
                    }
                )
                continue
            lane, action_key, queue = resolved
            target_role = (
                "SELF"
                if action_key in _SELF_ACTION_KEYS
                else "CURRENT_ENEMY"
                if hostile_last
                else "OTHER_OR_NEW_ENEMY"
            )
            legal_actions.append(
                {
                    "candidate_id": (
                        f"sim-action:{row.index}:{row.action.spell_id}:"
                        f"{row.action.tag}:{target_role}"
                    ),
                    "spell": {"id": row.action.spell_id},
                    "target_role": target_role,
                    "legal": True,
                    "command": {
                        "lane": lane,
                        "action_key": action_key,
                        "action": row.action.to_wire(),
                        "queue": None if queue is None else queue.value,
                        "triggers_gcd": row.triggers_gcd,
                        "target_role": target_role,
                    },
                }
            )
        if not legal_actions:
            raise HistoricalFurySimulatorRunnerV2Error(
                "no current bridge-legal Historical action is exactly representable"
            )
        sample_seed = {
            "simulator_seed": self.simulator_seed,
            "decision_ordinal": self._decision_ordinal,
            "time_ms": observation["wave_elapsed_ms"],
        }
        sample = _policy.sample_legal_action(
            self._model,
            observation=observation,
            legal_actions=legal_actions,
            seed=sample_seed,
        )
        self._decision_ordinal += 1
        command = _mapping(sample.get("command"), "historical sampled command")
        trace = {
            "schema": TRACE_SCHEMA,
            "decision_ordinal": self._decision_ordinal - 1,
            "policy_bundle_content_sha256": self.binding["bundle_content_sha256"],
            "model_content_sha256": self.binding["model_content_sha256"],
            "observation": observation,
            "sample_seed": sample_seed,
            "legal_actions": legal_actions,
            "excluded_available_actions": excluded,
            "sample": sample,
            "selected_lane": command.get("lane"),
            "selected_action_key": command.get("action_key"),
            "selected_action": command.get("action"),
            "selected_target_role": command.get("target_role"),
            "simulator_accepted": None,
            "client_observed": False,
            "server_outcome_observed_at_proposal": False,
        }
        metadata = {
            "historical_runtime_trace_v2": trace,
            "raw_gcd_calls": (
                [command.get("action_key")] if command.get("lane") == "gcd" else []
            ),
        }
        if command.get("lane") != "gcd" or command.get("triggers_gcd") is not True:
            return ExpertDecision(
                provenance=self.provenance,
                valid=False,
                wait_ms=1,
                eligible_for_independent_vote=False,
                reason=(
                    "HISTORICAL_SINGLE_LABEL_NON_GCD_TEMPORAL_SEMANTICS_NOT_CLOSED"
                ),
                metadata=metadata,
            )
        action_key = command.get("action_key")
        if not isinstance(action_key, str) or action_key not in ACTION_KEY_TO_REF:
            raise HistoricalFurySimulatorRunnerV2Error(
                "sampled GCD command lacks an exact canonical action key"
            )
        return ExpertDecision(
            provenance=self.provenance,
            valid=True,
            gcd=action_key,
            wait_ms=None,
            raw_sink_order=(
                RawSink(
                    "gcd",
                    "HistoricalPolicySample",
                    action_key,
                    "ExternalHistoricalFuryDynamicV5AdapterV2.propose",
                ),
            ),
            eligible_for_independent_vote=False,
            reason="historical legal-conditioned GCD diagnostic",
            metadata=metadata,
        )


def _historical_preflight(
    decision: ExpertDecision,
) -> tuple[list[Any], list[str]]:
    reasons: list[str] = []
    resolved: list[Any] = []
    if not decision.valid:
        reasons.append(f"proposal:invalid:{decision.reason or 'unspecified'}")
    if decision.expert_id != POLICY_ID:
        reasons.append(f"proposal:unsupported_expert_id:{decision.expert_id}")
    if decision.provenance.role is not ExpertRole.CANDIDATE:
        reasons.append("proposal:historical_role_is_not_candidate")
    if decision.provenance.kind is not ProvenanceKind.SOURCE_DERIVED:
        reasons.append("proposal:provenance_is_not_source_derived")
    for index, sink in enumerate(decision.raw_sink_order, start=1):
        item, sink_reasons = _ordered_v2._resolve_sink(sink, index=index)
        reasons.extend(sink_reasons)
        supported = sink.channel == "gcd" and sink.operation == "HistoricalPolicySample"
        if not supported:
            reasons.append(
                f"raw_sink[{index}]:unsupported_operation:{sink.channel}/{sink.operation}"
            )
        resolved.append(
            replace(
                item,
                recognized=(
                    supported
                    and not any(":unsupported_" in reason for reason in sink_reasons)
                ),
            )
        )
    reasons.extend(_ordered_v2._normalized_lane_reasons(decision, resolved))
    return resolved, _ordered_v2._deduplicate(reasons)


def _clone_ordered_executor() -> Callable[..., JSONMap]:
    source = _ordered_v2.execute_ordered_sinks_v2
    namespace = dict(source.__globals__)
    namespace["_preflight"] = _historical_preflight
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_execute_historical_ordered_sinks_v2",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


_EXECUTE_HISTORICAL_ORDERED_SINKS = _clone_ordered_executor()


def _historical_supported_adapter(adapter: Any) -> bool:
    return (
        type(adapter) is ExternalHistoricalFuryDynamicV5AdapterV2
        and adapter.expert_id == POLICY_ID
    )


def _historical_combat_state(
    state: Mapping[str, Any],
    available: Sequence[AvailableAction],
    request: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    last_gcd_action: str,
) -> _HistoricalRuntimeFrame:
    return _HistoricalRuntimeFrame(
        simulator_state=deepcopy(dict(state)),
        available_actions=tuple(available),
        request=deepcopy(dict(request)),
        target=deepcopy(dict(target)),
        last_gcd_action=last_gcd_action,
    )


def _historical_proposal(
    adapter: ExternalHistoricalFuryDynamicV5AdapterV2,
    frame: _HistoricalRuntimeFrame,
    target: Mapping[str, Any],
) -> ExpertDecision:
    del target
    return adapter.propose(frame)


def _clone_v5_core() -> Callable[..., JSONMap]:
    source = _rollout_v5._RUN_V5_CORE
    namespace = dict(source.__globals__)
    namespace.update(
        {
            "_supported_adapter": _historical_supported_adapter,
            "_combat_state": _historical_combat_state,
            "_proposal": _historical_proposal,
            "execute_ordered_sinks_v2": _EXECUTE_HISTORICAL_ORDERED_SINKS,
        }
    )
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_external_historical_fury_v5_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


_RUN_HISTORICAL_V5_CORE = _clone_v5_core()


def _clone_v5_public() -> Callable[..., JSONMap]:
    source = _rollout_v5.run_fury_full_policy_rollout_v5
    namespace = dict(source.__globals__)
    namespace["_RUN_V5_CORE"] = _RUN_HISTORICAL_V5_CORE
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_external_historical_fury_v5",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


_RUN_HISTORICAL_V5 = _clone_v5_public()


def _blocker(code: str, message: str, *, stage: str) -> JSONMap:
    return {
        "code": code,
        "stage": stage,
        "message": message,
        "comparison_fatal": True,
    }


def _run_with_bundle(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    bundle: Mapping[str, Any],
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    adapter = ExternalHistoricalFuryDynamicV5AdapterV2(
        bundle, simulator_seed=seed
    )
    inner = _RUN_HISTORICAL_V5(
        bridge,
        raid_sim_request,
        adapter,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=True,
    )
    traces = []
    steps = inner.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            proposal = step.get("proposal")
            metadata = proposal.get("metadata") if isinstance(proposal, Mapping) else None
            trace = (
                metadata.get("historical_runtime_trace_v2")
                if isinstance(metadata, Mapping)
                else None
            )
            if isinstance(trace, Mapping):
                row = deepcopy(dict(trace))
                execution = step.get("ordered_execution")
                events = execution.get("sink_events") if isinstance(execution, Mapping) else None
                if isinstance(events, list) and events:
                    acceptance = events[0].get("client_acceptance")
                    row["simulator_accepted"] = (
                        isinstance(acceptance, Mapping)
                        and acceptance.get("status") == "ACCEPTED"
                    )
                row["simulator_server_observation"] = deepcopy(
                    step.get("server_observation_after_advance")
                )
                traces.append(row)
    binding = adapter.binding
    upstream = [
        _blocker(code, "retained from the frozen V2 admission", stage="upstream")
        for code in binding["upstream_typed_blocker_codes"]
    ]
    blockers = upstream + [
        _blocker(code, message, stage="runtime_adapter")
        for code, message in sorted(_PERMANENT_BLOCKERS.items())
    ]
    if any(
        row.get("selected_lane") != "gcd" for row in traces
    ):
        blockers.append(
            _blocker(
                "HISTORICAL_NON_GCD_SAMPLE_FAILED_CLOSED",
                "a sampled non-GCD action was recorded but not submitted",
                stage="ordered_execution",
            )
        )
    outer: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
        "policy_bundle_binding": binding,
        "dynamic_load_binding": {
            "required_command": "load_dynamic_v3",
            "schema": "fury_full_policy_dynamic_load/v3",
            "request_sha256": dynamic_load.request_sha256,
            "simulator_seed": dynamic_load.seed,
            "contract_sha256": dynamic_load.contract_sha256,
            "config_digest": dynamic_load.config.content_sha256,
        },
        "execution": inner,
        "adapter_trace": traces,
        "evidence_boundary": {
            "simulator_submission_and_acceptance_recorded": True,
            "simulator_server_outcome_stream_separate": True,
            "wow_client_observed": False,
            "wow_server_outcome_observed": False,
            "source_derived_executable_diagnostic": True,
        },
        "typed_blockers": blockers,
        "remaining_admission_gates": [
            "CONTROLLABLE_ACTION_ONTOLOGY_V3_REQUIRED",
            "OUTER_HELDOUT_FIDELITY_GATE_MUST_PASS",
            "NON_GCD_AND_QUEUE_TEMPORAL_CONSUMPTION_MUST_CLOSE",
            "INDEPENDENT_ORDERED_ACTION_ACCEPTANCE_OUTCOME_FIDELITY_REQUIRED",
            "FORMAL_RUNNER_V4_LANE_REGISTRATION_AND_PAIRED_IDENTITY_REQUIRED",
        ],
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "voting_eligible": False,
        "offline_score_eligible": False,
        "scientific_result_available": False,
        "version_isolation": {
            "private_function_namespace_clone": True,
            "global_monkeypatch": False,
            "frozen_v2_v5_runner_sources_modified": False,
            "actual_process_load_command": "load_dynamic_v3",
        },
    }
    outer["content_address"] = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(outer),
    }
    _validate_historical_artifact_v2(
        outer,
        inner["dynamic_v3_runtime_receipt_closure"],
        dynamic_load,
    )
    return outer


def run_external_historical_fury_simulator_rollout_v2(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    admission_manifest_path: str | Path,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    bundle = _policy.load_external_historical_fury_policy_v2_bundle(
        admission_manifest_path
    )
    return _run_with_bundle(
        bridge,
        raid_sim_request,
        bundle=bundle,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
    )


def _evidence(
    kind: ContraEvidenceKindV2,
    *,
    digest: str | None = None,
    hypothesis: str | None = None,
) -> ContraFieldEvidenceV2:
    if kind is ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
        return ContraFieldEvidenceV2(
            kind, corpus_sha256=digest, hypothesis_id=hypothesis
        )
    return ContraFieldEvidenceV2(kind, source_sha256=digest)


def target_contexts_from_runner_v4(
    scenario: Mapping[str, Any], dynamic_load: DynamicRolloutLoadV3
) -> dict[int, TargetSemanticsContextV3]:
    """Bind the raw runner-v4 target bundle to executable v5 contexts."""

    request = _mapping(scenario.get("request"), "scenario.request")
    request_sha = _runner_v4.sha256_json(request)
    if request_sha != dynamic_load.request_sha256:
        raise HistoricalFurySimulatorRunnerV2Error(
            "scenario request differs from the native dynamic load"
        )
    bundle = _mapping(
        scenario.get("target_context_bundle"), "scenario.target_context_bundle"
    )
    if bundle.get("request_sha256") != request_sha:
        raise HistoricalFurySimulatorRunnerV2Error(
            "target-context bundle request binding mismatch"
        )
    rows = bundle.get("contexts")
    if not isinstance(rows, list) or len(rows) != len(dynamic_load.config.target_health):
        raise HistoricalFurySimulatorRunnerV2Error(
            "target-context rows differ from dynamic target count"
        )
    request_targets = _mapping(request.get("encounter"), "request.encounter").get(
        "targets"
    )
    if not isinstance(request_targets, list) or len(request_targets) != len(rows):
        raise HistoricalFurySimulatorRunnerV2Error(
            "request target rows differ from target-context rows"
        )
    bundle_sha = _runner_v4.sha256_json(bundle)
    contexts: dict[int, TargetSemanticsContextV3] = {}
    for index, raw in enumerate(rows):
        row = _mapping(raw, f"target_context_bundle.contexts[{index}]")
        if row.get("target_index") != index:
            raise HistoricalFurySimulatorRunnerV2Error(
                "target context indices are not contiguous"
            )
        name = row.get("target_name")
        if not isinstance(name, str) or not name or request_targets[index].get("name") != name:
            raise HistoricalFurySimulatorRunnerV2Error(
                "target context name differs from request target"
            )
        equipped = row.get("equipped_item_names", [])
        if not isinstance(equipped, list) or any(
            not isinstance(item, str) or not item for item in equipped
        ):
            raise HistoricalFurySimulatorRunnerV2Error(
                "equipped_item_names must be an array of nonempty strings"
            )
        health = dynamic_load.config.target_health[index].health
        if not float(health).is_integer():
            raise HistoricalFurySimulatorRunnerV2Error(
                "dynamic target max health must be integer-valued for Fury contexts"
            )
        try:
            classification = ContraTargetClassificationV2(
                row.get("target_classification")
            )
        except (TypeError, ValueError) as error:
            raise HistoricalFurySimulatorRunnerV2Error(
                "target classification is unsupported"
            ) from error
        contexts[index] = TargetSemanticsContextV3(
            context_id=f"runner-v4-historical-target-{index}",
            mode=TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
            target_index=index,
            target_classification=classification,
            target_name=name,
            equipped_item_names=tuple(equipped),
            target_classification_evidence=_evidence(
                ContraEvidenceKindV2.PINNED_STATIC_INPUT, digest=bundle_sha
            ),
            target_name_evidence=_evidence(
                ContraEvidenceKindV2.PINNED_STATIC_INPUT, digest=bundle_sha
            ),
            equipment_evidence=_evidence(
                ContraEvidenceKindV2.PINNED_STATIC_INPUT, digest=bundle_sha
            ),
            target_position_evidence=_evidence(
                ContraEvidenceKindV2.PINNED_STATIC_INPUT, digest=request_sha
            ),
            target_health_pct_evidence=_evidence(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                digest=dynamic_load.config.content_sha256,
                hypothesis="dynamic-v5-live-health",
            ),
            target_max_health_evidence=_evidence(
                ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
                digest=dynamic_load.config.content_sha256,
                hypothesis="dynamic-v5-initial-health",
            ),
            target_max_health=int(health),
        )
    return contexts


def _validate_trace(trace: Mapping[str, Any], step: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    if trace.get("schema") != TRACE_SCHEMA:
        raise HistoricalFurySimulatorRunnerV2Error("historical trace schema mismatch")
    if trace.get("model_content_sha256") != binding.get("model_content_sha256"):
        raise HistoricalFurySimulatorRunnerV2Error("historical trace model binding mismatch")
    observation = _mapping(trace.get("observation"), "historical trace observation")
    _policy._runtime_contexts(observation)
    legal = trace.get("legal_actions")
    if not isinstance(legal, list) or not legal:
        raise HistoricalFurySimulatorRunnerV2Error("historical trace legal mask is empty")
    sample = _mapping(trace.get("sample"), "historical trace sample")
    if (
        sample.get("policy_id") != POLICY_ID
        or sample.get("model_content_sha256") != binding.get("model_content_sha256")
        or sample.get("comparison_status") != "NOT_COMPARISON_READY"
    ):
        raise HistoricalFurySimulatorRunnerV2Error("historical sample identity mismatch")
    selected = [
        row
        for row in legal
        if isinstance(row, Mapping)
        and row.get("candidate_id") == sample.get("candidate_id")
        and legal.index(row) == sample.get("candidate_index")
    ]
    if len(selected) != 1 or sample.get("command") != selected[0].get("command"):
        raise HistoricalFurySimulatorRunnerV2Error("sample is outside its legal mask")
    seed_material = {
        "model_content_sha256": binding.get("model_content_sha256"),
        "policy_id": POLICY_ID,
        "seed": trace.get("sample_seed"),
        "observation": observation,
        "candidate_ids": [row.get("candidate_id") for row in legal],
    }
    if sample.get("seed_sha256") != _policy._sha256_json(seed_material):
        raise HistoricalFurySimulatorRunnerV2Error("sample seed receipt mismatch")
    command = _mapping(sample.get("command"), "sample command")
    available = step.get("available_actions_before")
    if not isinstance(available, list) or not any(
        isinstance(row, Mapping)
        and row.get("legal") is True
        and row.get("action") == command.get("action")
        and row.get("triggers_gcd") == command.get("triggers_gcd")
        for row in available
    ):
        raise HistoricalFurySimulatorRunnerV2Error(
            "sampled command was not an exact current bridge-legal action"
        )
    proposal = _mapping(step.get("proposal"), "step.proposal")
    if command.get("lane") == "gcd":
        if (
            proposal.get("valid") is not True
            or _mapping(proposal.get("gcd"), "proposal.gcd").get("action")
            != command.get("action_key")
            or proposal.get("eligible_for_independent_vote") is not False
        ):
            raise HistoricalFurySimulatorRunnerV2Error(
                "sampled GCD differs from the nonvoting expert proposal"
            )
    elif (
        proposal.get("valid") is not False
        or proposal.get("reason")
        != "HISTORICAL_SINGLE_LABEL_NON_GCD_TEMPORAL_SEMANTICS_NOT_CLOSED"
    ):
        raise HistoricalFurySimulatorRunnerV2Error(
            "sampled non-GCD action did not fail closed"
        )


def _validate_historical_artifact_v2(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    raw = _strict_json(artifact, "historical simulator rollout")
    if not isinstance(raw, dict):
        raise HistoricalFurySimulatorRunnerV2Error("artifact must be an object")
    fixed = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "voting_eligible": False,
        "offline_score_eligible": False,
        "scientific_result_available": False,
    }
    if any(raw.get(key) != value for key, value in fixed.items()):
        raise HistoricalFurySimulatorRunnerV2Error(
            "historical simulator identity or claim boundary mismatch"
        )
    isolation = raw.get("version_isolation")
    if isolation != {
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "frozen_v2_v5_runner_sources_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
    }:
        raise HistoricalFurySimulatorRunnerV2Error("version isolation receipt mismatch")
    binding = _mapping(raw.get("policy_bundle_binding"), "policy_bundle_binding")
    if (
        binding.get("admission_status") != STATUS
        or binding.get("artifact_ready") is not False
        or not isinstance(binding.get("upstream_typed_blocker_codes"), list)
    ):
        raise HistoricalFurySimulatorRunnerV2Error(
            "blocked historical policy bundle binding mismatch"
        )
    dynamic = _mapping(raw.get("dynamic_load_binding"), "dynamic_load_binding")
    expected_dynamic = {
        "required_command": "load_dynamic_v3",
        "schema": "fury_full_policy_dynamic_load/v3",
        "request_sha256": dynamic_load.request_sha256,
        "simulator_seed": dynamic_load.seed,
        "contract_sha256": dynamic_load.contract_sha256,
        "config_digest": dynamic_load.config.content_sha256,
    }
    if dynamic != expected_dynamic:
        raise HistoricalFurySimulatorRunnerV2Error("native dynamic-v5 binding mismatch")
    inner = _mapping(raw.get("execution"), "execution")
    _rollout_v5.validate_fury_full_policy_rollout_v5(
        inner, dynamic_load=dynamic_load
    )
    receipt = _strict_json(producer_runtime_receipt, "producer runtime receipt")
    if receipt != inner.get("dynamic_v3_runtime_receipt_closure"):
        raise HistoricalFurySimulatorRunnerV2Error(
            "producer runtime receipt differs from native v5 closure"
        )
    traces = raw.get("adapter_trace")
    steps = inner.get("steps")
    if not isinstance(traces, list) or not isinstance(steps, list) or len(traces) != len(steps):
        raise HistoricalFurySimulatorRunnerV2Error(
            "adapter trace does not cover every retained decision"
        )
    for trace, step in zip(traces, steps, strict=True):
        _validate_trace(
            _mapping(trace, "adapter trace"),
            _mapping(step, "execution step"),
            binding,
        )
    boundary = raw.get("evidence_boundary")
    if boundary != {
        "simulator_submission_and_acceptance_recorded": True,
        "simulator_server_outcome_stream_separate": True,
        "wow_client_observed": False,
        "wow_server_outcome_observed": False,
        "source_derived_executable_diagnostic": True,
    }:
        raise HistoricalFurySimulatorRunnerV2Error("evidence boundary mismatch")
    blockers = raw.get("typed_blockers")
    codes = {
        row.get("code") for row in blockers if isinstance(row, Mapping)
    } if isinstance(blockers, list) else set()
    required_codes = set(_PERMANENT_BLOCKERS) | {
        "TEAM_BACKGROUND_RUNTIME_ADMISSION_NOT_CLOSED",
        "FULL_SCENARIO_ADAPTER_NOT_IMPLEMENTED",
        "ORDERED_EXECUTION_FIDELITY_NOT_CLOSED",
        "PAIRED_ROLLOUT_IDENTITY_NOT_CLOSED",
    }
    if not required_codes.issubset(codes):
        raise HistoricalFurySimulatorRunnerV2Error(
            "historical simulator artifact omitted required blockers"
        )
    content = raw.get("content_address")
    expected_content = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: value for key, value in raw.items() if key != "content_address"}
        ),
    }
    if content != expected_content:
        raise HistoricalFurySimulatorRunnerV2Error("artifact content address mismatch")
    return raw


def _producer_summary(raw: Mapping[str, Any]) -> JSONMap:
    inner = _mapping(raw.get("execution"), "execution")
    elapsed = inner.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed <= 0:
        raise HistoricalFurySimulatorRunnerV2Error(
            "blocked zero-elapsed Historical execution cannot mint a lane result"
        )
    blockers = inner.get("blockers")
    reason_counts = Counter(
        str(row.get("code"))
        for row in blockers
        if isinstance(row, Mapping)
    ) if isinstance(blockers, list) else Counter()
    fatal_count = sum(
        row.get("execution_fatal") is True
        for row in blockers
        if isinstance(row, Mapping)
    ) if isinstance(blockers, list) else 0
    final_state = _mapping(inner.get("final_state"), "final_state")
    lifecycle = final_state.get("dynamic_team_background")
    targets = lifecycle.get("targets") if isinstance(lifecycle, Mapping) else None
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(
            isinstance(row, Mapping) and row.get("dead") is True
            for row in targets
        )
    )
    complete = inner.get("scenario_complete") is True
    damage = float(inner.get("damage_delta", 0.0))
    closure = _mapping(
        inner.get("dynamic_v3_runtime_receipt_closure"),
        "dynamic runtime receipt closure",
    )
    return {
        "policy_id": POLICY_ID,
        "artifact_schema": SCHEMA,
        "completion_mode": (
            "ALL_TARGETS_DEAD"
            if complete and all_dead
            else "SCENARIO_HORIZON_REACHED"
            if complete
            else "INCOMPLETE"
        ),
        "damage": damage,
        "elapsed_ms": elapsed,
        "dps": damage * 1000.0 / elapsed,
        "completion_criterion_met": complete,
        "offline_score_eligible": False,
        "omitted_lane_count": sum(
            trace.get("selected_lane") != "gcd"
            for trace in raw.get("adapter_trace", [])
            if isinstance(trace, Mapping)
        ),
        "fatal_error_count": fatal_count,
        "nonfaithful_reason_counts": dict(sorted(reason_counts.items())),
        "end_state_sha256": _runner_v4.sha256_json(final_state),
        "dynamic_runtime_receipts_complete": closure.get("status") == "COMPLETE_BOUND",
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_historical_fury_simulator_rollout_v2(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    """Validate the artifact and return runner-v4's exact producer summary."""

    raw = _validate_historical_artifact_v2(
        artifact, producer_runtime_receipt, dynamic_load
    )
    return _producer_summary(raw)


class ExternalHistoricalFuryRunnerV4ExecutorV2:
    """Callable runner-v4 envelope producer; not a lane registration."""

    def __init__(
        self,
        admission_manifest_path: str | Path,
        bridge_factory: Callable[..., Any],
    ) -> None:
        if not callable(bridge_factory):
            raise TypeError("bridge_factory must be callable")
        self._bundle = _policy.load_external_historical_fury_policy_v2_bundle(
            admission_manifest_path
        )
        self._binding = _policy_binding(self._bundle)
        self._bridge_factory = bridge_factory

    def __call__(
        self,
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> JSONMap:
        expected_policy = {
            "policy_id": POLICY_ID,
            "source_sha256": self._binding["model_content_sha256"],
            "adapter_sha256": self._binding["simulator_adapter_contract_sha256"],
            "profile_sha256": self._binding["policy_profile_sha256"],
        }
        if any(policy.get(key) != value for key, value in expected_policy.items()):
            raise HistoricalFurySimulatorRunnerV2Error(
                "runner policy identity differs from the loaded historical bundle"
            )
        request = _mapping(scenario.get("request"), "scenario.request")
        seed = group.get("simulator_seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise HistoricalFurySimulatorRunnerV2Error(
                "group.simulator_seed must be an integer"
            )
        load = _runner_v4.bind_dynamic_v5_load(
            request,
            seed,
            _mapping(scenario.get("dynamic_load_config"), "dynamic_load_config"),
        )
        if group.get("dynamic_load_contract_sha256") != load.contract_sha256:
            raise HistoricalFurySimulatorRunnerV2Error(
                "group dynamic-load identity differs from scenario"
            )
        contexts = target_contexts_from_runner_v4(scenario, load)
        bridge = self._bridge_factory(
            group=deepcopy(dict(group)),
            scenario=deepcopy(dict(scenario)),
            policy=deepcopy(dict(policy)),
        )
        artifact = _run_with_bundle(
            bridge,
            request,
            bundle=self._bundle,
            seed=seed,
            target_contexts=contexts,
            dynamic_load=load,
        )
        inner = artifact["execution"]
        closure = _mapping(
            inner.get("dynamic_v3_runtime_receipt_closure"),
            "dynamic runtime receipt closure",
        )
        summary = validate_historical_fury_simulator_rollout_v2(
            artifact, closure, load
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
            offline_score_eligible=summary["offline_score_eligible"],
            omitted_lane_count=summary["omitted_lane_count"],
            fatal_error_count=summary["fatal_error_count"],
            nonfaithful_reason_counts=summary["nonfaithful_reason_counts"],
            end_state_sha256=summary["end_state_sha256"],
            dynamic_runtime_receipts_complete=summary[
                "dynamic_runtime_receipts_complete"
            ],
            producer_runtime_receipt=closure,
            live_fidelity=False,
            comparison_ready=False,
        )
        return {"lane_result": lane}


__all__ = (
    "CONTENT_ADDRESS_SCHEMA",
    "ExternalHistoricalFuryDynamicV5AdapterV2",
    "ExternalHistoricalFuryRunnerV4ExecutorV2",
    "HistoricalFurySimulatorRunnerV2Error",
    "IMPLEMENTATION_REVISION",
    "POLICY_ID",
    "PRODUCER",
    "SCHEMA",
    "STATUS",
    "TRACE_SCHEMA",
    "build_historical_policy_descriptor_v2",
    "run_external_historical_fury_simulator_rollout_v2",
    "target_contexts_from_runner_v4",
    "validate_historical_fury_simulator_rollout_v2",
)
