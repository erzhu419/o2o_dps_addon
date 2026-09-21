"""Remote workers for one continuous two-wave causal-program campaign.

The four phases are deliberately separate:

* ``train`` evaluates the same candidate family on one loadout/seed shard;
* ``freeze`` reduces every training shard and serializes one winning program;
* ``eval`` replays that frozen program and three imported incumbents on one
  held-out seed shard, without invoking a search/generator;
* ``summarize`` reduces the paired held-out lanes.

The callable runtime is injectable so contract and reducer behavior can be
smoked on Windows without launching the Go bridge.  The default runtime is
resolved lazily from the native imported-incumbent adapter.
"""

from __future__ import annotations

import argparse
from array import array
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from statistics import NormalDist, mean, stdev
import tempfile
from typing import Any, Callable, Mapping, Protocol, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    CausalLiveStateProjectionV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
    causal_action_program_from_dict_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, PROTOCOL_ID, WORKSPACE_ROOT
from .fury_paired_multiseed_runner_v2 import derive_simulator_seed
from .upper_kara_causal_program_remote_contract_v1 import (
    CatActionPlanResidualSequenceSearchV8,
    EvaluationShardV1,
    FixedParentCatHpGuardedSparseRoutingSearchV1,
    FixedParentQueueGcdBlockSearchV1,
    HeterogeneousTwoWaveSequenceSearchV7,
    TERMINAL_COMPLETE,
    TERMINAL_FAILED,
    TERMINAL_INVALID,
    TrainingShardV1,
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
    terminal_status_for_lanes_v1,
)
from .development_two_wave_cat_residual_sequence_v1 import (
    ZERO_RESIDUAL_SOURCE_REF_V1,
)
from .upper_kara_causal_program_search_v1 import (
    ProposalPriorityProviderV1,
    UpperKaraCausalProgramGeneratorV1,
    WaveActionGuideProgramProposalAdapterV1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    UpperKaraCatResidualOverlayGeneratorV1,
    cat_zero_residual_program_v1,
)
from .upper_kara_paired_cat_selection_v1 import (
    DEFAULT_MINIMUM_VALIDATION_PAIRS,
    PairedCatDamageV1,
    exact_cat_zero_residual_ref_v1,
    exact_paired_zero_program_ref_v1,
    select_paired_cat_residual_v1,
)
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from .upper_kara_heterogeneous_two_wave_remote_v7 import (
    HeterogeneousTwoWaveSequenceProgramGeneratorV7,
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from .upper_kara_two_wave_train_eval_v1 import (
    ActionProgramReplayV1,
    TwoWaveExampleV1,
    imported_incumbent_programs_v1,
)
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1
from .fury_chronicle_prior import DEFAULT_MODEL as DEFAULT_OFFLINE_GUIDE
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .wave_action_guides_v1 import complete_legal_action_priorities_v1
from .wave_action_sequence_pilot_v1 import build_expert_action_guides_v1


JSONMap = dict[str, Any]
TRAIN_TERMINAL_SCHEMA = "upper_kara_causal_program_remote_train_shard/v1"
FREEZE_TERMINAL_SCHEMA = "upper_kara_causal_program_remote_freeze/v1"
EVAL_TERMINAL_SCHEMA = "upper_kara_causal_program_remote_eval_shard/v1"
SUMMARY_TERMINAL_SCHEMA = "upper_kara_causal_program_remote_summary/v1"
DEFAULT_REPLAY_WORKERS = 48
TRAINING_FRONTIER_WAIT_MS = 250
_SEARCHED_ORIGINS_V1 = frozenset(
    {ProgramOriginV1.SEARCHED, ProgramOriginV1.SEARCHED_REACTIVE}
)


ProgramReplayFactoryV1 = Callable[
    [str, Mapping[int, Any]], ActionProgramReplayV1
]
SearchedProgramGeneratorV1 = Callable[..., Sequence[CausalActionProgramV1]]
CaseBuilderV1 = Callable[..., Any]
IncumbentProgramFactoryV1 = Callable[[str], Sequence[CausalActionProgramV1]]


class NativeTrainingObservableFrontierProviderV1:
    """Materialize only the first train-time observable combat frontier.

    The dynamic configuration remains control-plane input to the simulator.
    Guides receive the causal policy projection after the player can first
    attack, never the raw target registry or any held-out case.
    """

    def __init__(
        self,
        train_cases: Sequence[Any],
        *,
        bridge_path: str | Path,
        bridge_cwd: str | Path,
    ) -> None:
        cases = tuple(train_cases)
        if not cases:
            raise ValueError("train_cases must not be empty")
        self._case_ids = frozenset(id(case) for case in cases)
        if len(self._case_ids) != len(cases):
            raise ValueError("train_cases must contain distinct case objects")
        self._bridge_path = Path(bridge_path).expanduser().resolve()
        self._bridge_cwd = Path(bridge_cwd).expanduser().resolve()
        self._cache: dict[
            int, tuple[ScheduleReplayOutcomeV1, CausalLiveStateProjectionV1]
        ] = {}

    @property
    def observation_contract(self) -> JSONMap:
        return {
            "case_scope": "BOUND_TRAIN_CASE_OBJECTS_ONLY",
            "frontier": "FIRST_CURRENTLY_ATTACKABLE_PLAYER_FRONTIER",
            "state_projection": "policy_observation_causal_projection/v1",
            "accepted_action_prefix": [],
            "heldout_examples_materialized": False,
            "future_fields_used": [],
            "raw_dynamic_state_passed_to_guide": False,
        }

    @staticmethod
    def _advance_to_input(bridge: Any, state: Mapping[str, Any]) -> JSONMap:
        current = dict(state)
        advances = 0
        while not current.get("finished") and not current.get("needs_input"):
            current = dict(bridge.advance())
            advances += 1
            if advances > 100_000:
                raise RuntimeError("training frontier advance loop did not terminate")
        return current

    @staticmethod
    def _currently_attackable(state: Mapping[str, Any]) -> bool:
        semantics = state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        index = state.get("target_index")
        return bool(
            isinstance(rows, list)
            and type(index) is int
            and 0 <= index < len(rows)
            and isinstance(rows[index], Mapping)
            and rows[index].get("attackable") is True
            and rows[index].get("dead") is False
            and state.get("num_targets", 0) > 0
        )

    def _materialize(
        self,
        case: Any,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> tuple[ScheduleReplayOutcomeV1, CausalLiveStateProjectionV1]:
        if id(case) not in self._case_ids:
            raise ValueError("guide requested a case outside the bound training panel")
        cached = self._cache.get(id(case))
        if cached is not None:
            return cached
        if any(not isinstance(row, AvailableAction) for row in native_snapshot):
            raise TypeError("native_snapshot must contain AvailableAction values")

        from .upper_kara_imported_incumbent_program_v1 import (
            build_continuous_two_wave_observation_projector_v1,
        )

        with SimulatorBridgePrecombatV1(
            self._bridge_path, cwd=self._bridge_cwd
        ) as bridge:
            loaded = bridge.load_dynamic_v3_precombat(
                case.request,
                case.dynamic_load.seed,
                case.dynamic_load.config,
                case.precombat,
            )
            state = self._advance_to_input(bridge, loaded.state)
            waits = 0
            while not state.get("finished") and not self._currently_attackable(state):
                state = self._advance_to_input(
                    bridge, bridge.wait(TRAINING_FRONTIER_WAIT_MS)
                )
                waits += 1
                if waits > 10_000:
                    raise RuntimeError("no observable combat frontier before wait budget")
            if state.get("finished"):
                raise RuntimeError("encounter finished before an observable combat frontier")
            available = tuple(bridge.actions())

        snapshot_actions = {row.action for row in native_snapshot}
        frontier_actions = {row.action for row in available}
        if snapshot_actions != frontier_actions:
            raise ValueError("training frontier changed native action membership")
        projector = build_continuous_two_wave_observation_projector_v1(case)
        observation = projector(state, available)
        outcome = ScheduleReplayOutcomeV1(
            seed=case.dynamic_load.seed,
            status=ReplayStatusV1.FRONTIER,
            state=observation.state,
            available_actions=available,
            receipts=tuple(
                {
                    "kind": "TRAIN_OBSERVATION_WAIT",
                    "accepted": True,
                    "wait_ms": TRAINING_FRONTIER_WAIT_MS,
                }
                for _ in range(waits)
            ),
        )
        result = (outcome, observation)
        self._cache[id(case)] = result
        return result

    def __call__(
        self,
        case: Any,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> ScheduleReplayOutcomeV1:
        return self._materialize(case, native_snapshot)[0]

    def observation(
        self,
        case: Any,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> CausalLiveStateProjectionV1:
        return self._materialize(case, native_snapshot)[1]


class ImportedIncumbentProgramProposalGuideV1:
    """Rank native actions by one exact imported source-policy decision."""

    def __init__(
        self,
        source_policy_id: str,
        bindings_by_case_id: Mapping[int, Any],
        frontier_provider: NativeTrainingObservableFrontierProviderV1,
    ) -> None:
        if not isinstance(source_policy_id, str) or not source_policy_id:
            raise ValueError("source_policy_id must be nonempty")
        if not bindings_by_case_id:
            raise ValueError("bindings_by_case_id must not be empty")
        self.guide_id = f"imported-source-program:{source_policy_id}"
        self._source_policy_id = source_policy_id
        self._bindings = dict(bindings_by_case_id)
        self._frontier_provider = frontier_provider

    @staticmethod
    def _ordered_actions(decision: ProgramDecisionV1) -> tuple[ActionRef, ...]:
        prefixes = iter(decision.optional_off_gcd_prefixes)
        ordered: list[ActionRef] = []
        for kind in decision.prefix_order or ():
            action: ActionRef | None = None
            if kind is ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD:
                action = next(prefixes).action
            elif kind is ProgramPrefixOperationKindV1.QUEUE_SET:
                action = decision.queue_action
            if action is not None and action not in ordered:
                ordered.append(action)
        if decision.gcd_action is not None and decision.gcd_action not in ordered:
            ordered.append(decision.gcd_action)
        return tuple(ordered)

    def action_priorities(
        self,
        case: Any,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> Mapping[ActionRef, float]:
        try:
            binding = self._bindings[id(case)]
        except KeyError as error:
            raise ValueError("expert guide received an unbound training case") from error
        outcome = self._frontier_provider(case, native_snapshot)
        observation = self._frontier_provider.observation(case, native_snapshot)
        decision = binding.open_session()(observation, outcome.available_actions)
        if not isinstance(decision, ProgramDecisionV1):
            raise TypeError("imported source resolver returned a non-program decision")
        ordered = self._ordered_actions(decision)
        proposed = {
            action: float(len(ordered) - index)
            for index, action in enumerate(ordered)
        }
        return complete_legal_action_priorities_v1(
            outcome.available_actions, proposed
        )


def build_default_training_proposal_guides_v1(
    train_cases: Sequence[Any],
    *,
    build_id: str,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    offline_guide_artifact_path: str | Path,
) -> tuple[ProposalPriorityProviderV1, ...]:
    """Bind three exact source guides plus one compact Chronicle prior."""

    cases = tuple(train_cases)
    if not cases:
        raise ValueError("train_cases must not be empty")
    from .upper_kara_imported_incumbent_program_v1 import (
        build_imported_incumbent_bindings_v1,
    )

    frontier = NativeTrainingObservableFrontierProviderV1(
        cases, bridge_path=bridge_path, bridge_cwd=bridge_cwd
    )
    by_policy: dict[str, dict[int, Any]] = {}
    expected_ids: tuple[str, ...] | None = None
    for case in cases:
        bindings = build_imported_incumbent_bindings_v1(
            build_id, case, runtime_binding_path=runtime_binding_path
        )
        policy_ids = tuple(binding.source_policy_id for binding in bindings)
        if expected_ids is None:
            expected_ids = policy_ids
        elif policy_ids != expected_ids:
            raise ValueError("imported guide panel differs across training cases")
        for binding in bindings:
            by_policy.setdefault(binding.source_policy_id, {})[id(case)] = binding
    assert expected_ids is not None
    expert_guides: list[ProposalPriorityProviderV1] = [
        ImportedIncumbentProgramProposalGuideV1(
            policy_id, by_policy[policy_id], frontier
        )
        for policy_id in expected_ids
    ]

    # Reuse the established Chronicle guide factory and the existing
    # ActionGuide-to-program-proposal adapter.  Its history is the accepted
    # training prefix; at this first frontier that prefix is deliberately empty.
    offline_wave_guide = build_expert_action_guides_v1(
        cases[0],
        guide_names=("offline",),
        runtime_binding_path=Path(runtime_binding_path),
        offline_guide_artifact_path=Path(offline_guide_artifact_path),
    )[0]
    expert_guides.append(
        WaveActionGuideProgramProposalAdapterV1(
            offline_wave_guide, frontier
        )
    )
    return tuple(expert_guides)


@dataclass(frozen=True)
class RemoteCausalProgramRuntimeV1:
    case_builder: CaseBuilderV1
    searched_program_generator: SearchedProgramGeneratorV1
    program_replay_factory: ProgramReplayFactoryV1
    incumbent_program_factory: IncumbentProgramFactoryV1 = (
        imported_incumbent_programs_v1
    )

    def __post_init__(self) -> None:
        for name in (
            "case_builder",
            "searched_program_generator",
            "program_replay_factory",
            "incumbent_program_factory",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: str | Path, label: str) -> JSONMap:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def train_terminal_name_v1(shard: TrainingShardV1) -> str:
    return f"{shard.work_id}.json"


def _campaign_contract_v1(campaign: Any) -> JSONMap:
    wire = campaign.to_dict()
    proposal_examples, selection_examples = (
        split_training_examples_for_selection_v1(campaign)
    )
    contract = {
        "build_id": wire["build_id"],
        "train_examples": wire["train_examples"],
        "evaluation_examples": wire["evaluation_examples"],
        "generation_config": wire["generation_config"],
        "loadout_ids": wire["loadout_ids"],
        "seed_shard_count": wire["seed_shard_count"],
        "pull_time_ms": wire["pull_time_ms"],
        "max_decisions": wire["max_decisions"],
        "training_candidate_proposal_examples": [
            row.to_dict() for row in proposal_examples
        ],
        "training_selection_validation_examples": [
            row.to_dict() for row in selection_examples
        ],
    }
    if "search_spec" in wire:
        contract["search_spec"] = wire["search_spec"]
    return contract


def evaluation_terminal_name_v1(shard: EvaluationShardV1) -> str:
    return f"{shard.work_id}.json"


def _build_cases_v1(
    runtime: RemoteCausalProgramRuntimeV1,
    campaign: Any,
    loadout_id: str,
    examples: Sequence[TwoWaveExampleV1],
) -> tuple[tuple[Any, ...], dict[int, Any], dict[int, int]]:
    cases: list[Any] = []
    by_simulator: dict[int, Any] = {}
    simulator_by_master: dict[int, int] = {}
    for example in examples:
        case = runtime.case_builder(
            example.seed,
            build_id=campaign.build_id,
            loadout_id=loadout_id,
            pull_time_ms=campaign.pull_time_ms,
            first_wave_arrival_ms=example.first_wave_arrival_ms,
        )
        request_sha = getattr(getattr(case, "dynamic_load", None), "request_sha256", None)
        if not isinstance(request_sha, str) or not request_sha:
            raise ValueError("case lacks dynamic_load.request_sha256")
        simulator_seed = derive_simulator_seed(
            example.seed, request_sha, namespace=PROTOCOL_ID
        )
        if simulator_seed in by_simulator:
            raise ValueError("derived simulator seeds collide")
        cases.append(case)
        by_simulator[simulator_seed] = case
        simulator_by_master[example.seed] = simulator_seed
    return tuple(cases), by_simulator, simulator_by_master


def _required_targets_dead(case: Any, state: Mapping[str, Any]) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    case_spec = getattr(case, "case_spec", None)
    required = (
        case_spec.get("required_target_indices")
        if isinstance(case_spec, Mapping)
        else None
    )
    return bool(
        isinstance(targets, list)
        and isinstance(required, list)
        and required
        and all(
            type(index) is int
            and 0 <= index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required
        )
    )


def _first_player_attackable_ms_v1(case: Any) -> int:
    config = getattr(getattr(case, "dynamic_load", None), "config", None)
    events = getattr(config, "attackability_events", None)
    case_spec = getattr(case, "case_spec", None)
    required = (
        case_spec.get("required_target_indices")
        if isinstance(case_spec, Mapping)
        else None
    )
    if not isinstance(events, Sequence) or not isinstance(required, list) or not required:
        raise ValueError("case lacks first-target attackability contract")
    target_index = required[0]
    candidates = [
        getattr(row, "time_ms", None)
        for row in events
        if getattr(row, "target_index", None) == target_index
        and getattr(row, "attackable", None) is True
    ]
    if not candidates or any(type(value) is not int or value < 0 for value in candidates):
        raise ValueError("case has no valid first-target attackable start")
    return min(candidates)


def _finite_nonnegative(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _lane_from_outcome_v1(
    outcome: ScheduleReplayOutcomeV1,
    *,
    case: Any,
    master_seed: int,
    simulator_seed: int,
) -> JSONMap:
    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("program replay returned the wrong outcome type")
    if outcome.seed != simulator_seed:
        raise ValueError("program replay returned a different simulator seed")
    if outcome.status is ReplayStatusV1.INVALID:
        return {
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "status": TERMINAL_INVALID,
            "own_effective_damage": None,
            "own_effective_dps": None,
            "ttk_ms": None,
            "error": outcome.invalid_reason,
        }
    if outcome.status is not ReplayStatusV1.COMPLETE or not _required_targets_dead(
        case, outcome.state
    ):
        return {
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "status": TERMINAL_FAILED,
            "own_effective_damage": None,
            "own_effective_dps": None,
            "ttk_ms": None,
            "error": "replay did not kill every required target",
        }
    try:
        damage = _finite_nonnegative(outcome.effective_damage)
        elapsed = outcome.elapsed_ms - _first_player_attackable_ms_v1(case)
    except (TypeError, ValueError) as error:
        damage = None
        elapsed = None
        message = f"{type(error).__name__}: {error}"
    else:
        message = None
    if damage is None or type(elapsed) is not int or elapsed <= 0:
        return {
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "status": TERMINAL_FAILED,
            "own_effective_damage": None,
            "own_effective_dps": None,
            "ttk_ms": None,
            "error": message or "complete replay has malformed terminal metrics",
        }
    return {
        "master_seed": master_seed,
        "simulator_seed": simulator_seed,
        "status": TERMINAL_COMPLETE,
        "own_effective_damage": damage,
        "own_effective_dps": damage * 1000.0 / elapsed,
        "ttk_ms": elapsed,
        "error": None,
    }


def _failed_lane_v1(
    *, master_seed: int, simulator_seed: int, error: BaseException | str
) -> JSONMap:
    message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return {
        "master_seed": master_seed,
        "simulator_seed": simulator_seed,
        "status": TERMINAL_FAILED,
        "own_effective_damage": None,
        "own_effective_dps": None,
        "ttk_ms": None,
        "error": message,
    }


def _program_receipt_v1(program: CausalActionProgramV1) -> JSONMap:
    if not isinstance(program, CausalActionProgramV1):
        raise TypeError("program provider returned a non-CausalActionProgramV1")
    proposal_guide_ids = tuple(
        source_ref.removeprefix("proposal-guide:")
        for source_ref in program.source_refs
        if source_ref.startswith("proposal-guide:")
    )
    return {
        # Training lanes refer to this receipt by the already unique program
        # ID.  The large canonical program key and full wire program therefore
        # occur once per shard instead of once per (program, seed) lane.
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": list(proposal_guide_ids),
        "program": program.to_dict(),
    }


def build_default_remote_runtime_v1(
    campaign: Any,
    *,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    offline_guide_artifact_path: str | Path = DEFAULT_OFFLINE_GUIDE,
) -> RemoteCausalProgramRuntimeV1:
    """Resolve the native backend only in a real train/eval worker process."""

    from .upper_kara_imported_incumbent_program_v1 import (
        build_imported_incumbent_program_replay_factory_v1,
    )

    resolved_bridge = Path(bridge_path)
    resolved_bridge_cwd = Path(bridge_cwd)
    resolved_binding = Path(runtime_binding_path)
    resolved_offline_guide = Path(offline_guide_artifact_path)
    if isinstance(campaign.search_spec, HeterogeneousTwoWaveSequenceSearchV7):
        from .upper_kara_heterogeneous_two_wave_case_v1 import (
            build_heterogeneous_two_wave_observation_projector_v1,
        )
        from .upper_kara_two_wave_segment_replay_v1 import (
            build_two_wave_segment_program_replay_factory_v1,
        )

        generator = HeterogeneousTwoWaveSequenceProgramGeneratorV7(
            build_id=campaign.build_id,
            search_spec=campaign.search_spec,
        )
        replay_factory = build_two_wave_segment_program_replay_factory_v1(
            campaign.build_id,
            generator.policies_by_program_id,
            bridge_path=resolved_bridge,
            bridge_cwd=resolved_bridge_cwd,
            runtime_binding_path=resolved_binding,
            observation_projector_factory=(
                build_heterogeneous_two_wave_observation_projector_v1
            ),
        )
        return RemoteCausalProgramRuntimeV1(
            case_builder=build_upper_kara_heterogeneous_two_wave_burst_case_v7,
            searched_program_generator=generator,
            program_replay_factory=replay_factory,
        )

    source_generator = UpperKaraCausalProgramGeneratorV1(
        config=campaign.generation_config,
        proposal_guide_factory=lambda train_cases: (
            build_default_training_proposal_guides_v1(
                train_cases,
                build_id=campaign.build_id,
                bridge_path=resolved_bridge,
                bridge_cwd=resolved_bridge_cwd,
                runtime_binding_path=resolved_binding,
                offline_guide_artifact_path=resolved_offline_guide,
            )
        ),
        bridge_path=resolved_bridge,
        bridge_cwd=resolved_bridge_cwd,
    )
    if isinstance(
        campaign.search_spec,
        FixedParentCatHpGuardedSparseRoutingSearchV1,
    ):
        from .upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
            UpperKaraCatHpGuardedSparseRoutingGeneratorV1,
        )

        generator = UpperKaraCatHpGuardedSparseRoutingGeneratorV1(
            loadout_id=campaign.search_spec.parent_loadout_id,
            parent_program=campaign.search_spec.parent_program,
            prototype_programs=(
                campaign.search_spec.prototype_programs_by_index
            ),
        )
    elif isinstance(campaign.search_spec, FixedParentQueueGcdBlockSearchV1):
        from .upper_kara_cat_burst_sparse_queue_gcd_search_v1 import (
            UpperKaraCatBurstSparseQueueGcdGeneratorV1,
        )

        generator = UpperKaraCatBurstSparseQueueGcdGeneratorV1(
            source_generator,
            parent_program=campaign.search_spec.parent_program,
            max_programs=campaign.search_spec.max_block_programs,
        )
    else:
        generator = UpperKaraCatResidualOverlayGeneratorV1(source_generator)
    replay_factory = build_imported_incumbent_program_replay_factory_v1(
        campaign.build_id,
        bridge_path=resolved_bridge,
        bridge_cwd=resolved_bridge_cwd,
        runtime_binding_path=resolved_binding,
    )
    return RemoteCausalProgramRuntimeV1(
        case_builder=build_upper_kara_continuous_two_wave_burst_case_v1,
        searched_program_generator=generator,
        program_replay_factory=replay_factory,
    )


def _paired_zero_program_v1(
    campaign: Any, loadout_id: str
) -> CausalActionProgramV1:
    spec = campaign.search_spec
    if isinstance(
        spec,
        (
            FixedParentQueueGcdBlockSearchV1,
            FixedParentCatHpGuardedSparseRoutingSearchV1,
        ),
    ):
        if loadout_id != spec.parent_loadout_id:
            raise ValueError("fixed-parent zero requested for a different loadout")
        return spec.parent_program
    return cat_zero_residual_program_v1(loadout_id)


def _is_fixed_parent_search_v1(campaign: Any) -> bool:
    return isinstance(
        campaign.search_spec,
        (
            FixedParentQueueGcdBlockSearchV1,
            FixedParentCatHpGuardedSparseRoutingSearchV1,
        ),
    )


def _is_searched_origin_v1(origin: object) -> bool:
    return origin in _SEARCHED_ORIGINS_V1


def _paired_zero_ref_v1(
    campaign: Any,
    loadout_id: str,
    program_receipts: Sequence[Mapping[str, Any]],
) -> str:
    spec = campaign.search_spec
    if isinstance(spec, CatActionPlanResidualSequenceSearchV8):
        matches: list[str] = []
        for receipt in program_receipts:
            program = causal_action_program_from_dict_v1(
                receipt.get("program")
            )
            if (
                program.origin is ProgramOriginV1.SEARCHED_REACTIVE
                and ZERO_RESIDUAL_SOURCE_REF_V1 in program.source_refs
                and receipt.get("program_ref") == program.program_id
                and receipt.get("program_key") == program.program_key()
            ):
                matches.append(program.program_id)
        if len(matches) != 1:
            raise ValueError(
                f"{loadout_id} Cat residual sequence zero must occur "
                f"exactly once; found {len(matches)}"
            )
        return matches[0]
    if isinstance(
        spec,
        (
            FixedParentQueueGcdBlockSearchV1,
            FixedParentCatHpGuardedSparseRoutingSearchV1,
        ),
    ):
        return exact_paired_zero_program_ref_v1(
            _paired_zero_program_v1(campaign, loadout_id),
            program_receipts,
            label=f"{loadout_id} frozen burst parent",
        )
    return exact_cat_zero_residual_ref_v1(loadout_id, program_receipts)


def _shared_burst_control_programs_v1(
    campaign: Any,
    incumbents: Sequence[CausalActionProgramV1],
) -> tuple[tuple[str, CausalActionProgramV1], ...]:
    """Rebase the frozen parent burst rules onto each imported rotation."""

    spec = campaign.search_spec
    if not isinstance(
        spec,
        (
            FixedParentQueueGcdBlockSearchV1,
            FixedParentCatHpGuardedSparseRoutingSearchV1,
        ),
    ):
        return ()
    parent_selector = spec.parent_program.selector
    if not isinstance(parent_selector, ImportedFallbackOverlaySelectorV1):
        raise ValueError("fixed-parent control source is not a burst overlay")
    controls: list[tuple[str, CausalActionProgramV1]] = []
    for incumbent in incumbents:
        imported = incumbent.selector
        if not isinstance(imported, ImportedReactiveSelectorV1):
            raise ValueError("shared-burst control requires imported incumbents")
        policy_id = imported.source_policy_id
        controls.append(
            (
                f"shared_burst::{policy_id}",
                CausalActionProgramV1(
                    program_id=f"shared-burst-control::{policy_id}",
                    selector=replace(
                        parent_selector,
                        imported_fallback=imported,
                    ),
                    origin=ProgramOriginV1.SEARCHED,
                    source_refs=(
                        *spec.parent_program.source_refs,
                        f"shared-burst-control:{policy_id}",
                    ),
                ),
            )
        )
    return tuple(controls)


def run_training_shard_v1(
    *,
    campaign_path: str | Path,
    loadout_id: str,
    seed_shard_index: int,
    output_path: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    offline_guide_artifact_path: str | Path = DEFAULT_OFFLINE_GUIDE,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
) -> JSONMap:
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    _positive_int(replay_workers, "replay_workers")
    matches = [
        row
        for row in assign_training_shards_v1(campaign)
        if row.loadout_id == loadout_id
        and row.seed_shard_index == seed_shard_index
    ]
    if len(matches) != 1:
        raise ValueError("loadout_id/seed_shard_index is not one campaign shard")
    shard = matches[0]
    resolved_runtime = runtime or build_default_remote_runtime_v1(
        campaign,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
        offline_guide_artifact_path=offline_guide_artifact_path,
    )

    # Candidate generation and all proposal guides see only the predeclared
    # proposal cohort.  Replay still covers this shard's proposal and
    # selection-validation examples; final evaluation cases do not exist here.
    proposal_examples, _ = split_training_examples_for_selection_v1(campaign)
    all_cases, _, _ = _build_cases_v1(
        resolved_runtime,
        campaign,
        loadout_id,
        proposal_examples,
    )
    searched = tuple(
        resolved_runtime.searched_program_generator(
            loadout_id=loadout_id,
            train_examples=proposal_examples,
            train_cases=all_cases,
        )
    )
    generation_result = getattr(
        resolved_runtime.searched_program_generator,
        "results_by_loadout",
        {},
    ).get(loadout_id)
    source_generation_result = getattr(
        generation_result, "source_candidate_set", generation_result
    )
    proposal_guide_ids = tuple(
        getattr(source_generation_result, "guide_ids", ())
    )
    incumbents = tuple(
        resolved_runtime.incumbent_program_factory(campaign.build_id)
    )
    programs = (*incumbents, *searched)
    receipts = [_program_receipt_v1(program) for program in programs]
    program_identity = {
        id(program): receipt["program_ref"]
        for program, receipt in zip(programs, receipts, strict=True)
    }
    keys = [row["program_key"] for row in receipts]
    ids = [row["program_id"] for row in receipts]
    if not programs or len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
        raise ValueError("training programs must have unique IDs and semantic keys")

    shard_cases, cases_by_simulator, simulator_by_master = _build_cases_v1(
        resolved_runtime, campaign, loadout_id, shard.examples
    )
    case_by_master = dict(
        zip((row.seed for row in shard.examples), shard_cases, strict=True)
    )
    replay = resolved_runtime.program_replay_factory(
        loadout_id, cases_by_simulator
    )

    def evaluate(job: tuple[CausalActionProgramV1, TwoWaveExampleV1]) -> JSONMap:
        program, example = job
        simulator_seed = simulator_by_master[example.seed]
        try:
            outcome = replay.replay(
                simulator_seed, program, max_decisions=campaign.max_decisions
            )
            lane = _lane_from_outcome_v1(
                outcome,
                case=case_by_master[example.seed],
                master_seed=example.seed,
                simulator_seed=simulator_seed,
            )
        except Exception as error:
            lane = _failed_lane_v1(
                master_seed=example.seed,
                simulator_seed=simulator_seed,
                error=error,
            )
        lane.update(
            {
                "program_ref": program_identity[id(program)],
                "first_wave_arrival_ms": example.first_wave_arrival_ms,
            }
        )
        return lane

    jobs = [
        (program, example) for program in programs for example in shard.examples
    ]
    with ThreadPoolExecutor(max_workers=min(replay_workers, len(jobs))) as executor:
        lanes = list(executor.map(evaluate, jobs))
    terminal_status = terminal_status_for_lanes_v1(lanes)
    payload = {
        "schema": TRAIN_TERMINAL_SCHEMA,
        "terminal_status": terminal_status,
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": _campaign_contract_v1(campaign),
        "loadout_id": loadout_id,
        "seed_shard_index": seed_shard_index,
        "examples": [row.to_dict() for row in shard.examples],
        "programs": receipts,
        "proposal_guide_ids": list(proposal_guide_ids),
        "lanes": lanes,
        "lane_status_counts": dict(
            sorted(Counter(row["status"] for row in lanes).items())
        ),
        "metric": (
            {
                "mean_own_effective_damage_across_all_program_lanes": mean(
                    float(row["own_effective_damage"]) for row in lanes
                )
            }
            if terminal_status == TERMINAL_COMPLETE
            else None
        ),
        "completed_at": _utc_now(),
        "contract": {
            "candidate_generation_used_full_training_panel": False,
            "candidate_generation_used_proposal_cohort_only": True,
            "selection_validation_examples_not_passed_to_generator": True,
            "searched_candidates_are_cat_residual_overlays": (
                campaign.search_spec is None
                and all(
                program.origin is ProgramOriginV1.SEARCHED
                and getattr(program.selector, "imported_fallback", None) is not None
                for program in searched
                )
            ),
            "searched_candidates_share_fixed_burst_parent": (
                _is_fixed_parent_search_v1(campaign)
                and bool(searched)
                and searched[0] == _paired_zero_program_v1(campaign, loadout_id)
            ),
            "paired_zero_program_included_by_full_identity": (
                sum(
                    program == _paired_zero_program_v1(campaign, loadout_id)
                    for program in searched
                )
                == 1
            ),
            "zero_residual_exact_cat_included": (
                campaign.search_spec is None
                and any(
                    "cat-residual-overlay/v1:zero" in program.source_refs
                    for program in searched
                )
            ),
            "replay_used_only_assigned_seed_shard": True,
            "evaluation_cases_materialized": False,
            "invalid_or_failed_lane_metrics_null": all(
                row["own_effective_damage"] is None
                and row["own_effective_dps"] is None
                and row["ttk_ms"] is None
                for row in lanes
                if row["status"] != TERMINAL_COMPLETE
            ),
        },
    }
    _atomic_create_json(output_path, payload)
    return payload


def _validate_training_terminal_v1(
    value: Mapping[str, Any], campaign: Any, shard: TrainingShardV1
) -> tuple[tuple[JSONMap, ...], tuple[JSONMap, ...]]:
    if (
        value.get("schema") != TRAIN_TERMINAL_SCHEMA
        or value.get("campaign_id") != campaign.campaign_id
        or value.get("build_id") != campaign.build_id
        or value.get("campaign_contract") != _campaign_contract_v1(campaign)
        or value.get("loadout_id") != shard.loadout_id
        or value.get("seed_shard_index") != shard.seed_shard_index
        or value.get("examples") != [row.to_dict() for row in shard.examples]
    ):
        raise ValueError(f"training terminal identity mismatch: {shard.work_id}")
    programs = value.get("programs")
    lanes = value.get("lanes")
    if not isinstance(programs, list) or not programs or not isinstance(lanes, list):
        raise ValueError(f"training terminal payload missing: {shard.work_id}")
    parsed_programs: list[JSONMap] = []
    for row in programs:
        if not isinstance(row, Mapping):
            raise ValueError("training program receipt must be an object")
        program = causal_action_program_from_dict_v1(row.get("program"))
        if (
            row.get("program_ref") != program.program_id
            or row.get("program_id") != program.program_id
            or row.get("program_key") != program.program_key()
            or row.get("program_origin") != program.origin.value
        ):
            raise ValueError("training program receipt identity mismatch")
        parsed_programs.append(dict(row))
    refs = [row["program_ref"] for row in parsed_programs]
    ids = [row["program_id"] for row in parsed_programs]
    keys = [row["program_key"] for row in parsed_programs]
    if (
        len(refs) != len(set(refs))
        or len(ids) != len(set(ids))
        or len(keys) != len(set(keys))
    ):
        raise ValueError("training program receipts must have unique identities")
    expected = {
        (row["program_ref"], example.seed)
        for row in parsed_programs
        for example in shard.examples
    }
    observed: set[tuple[str, int]] = set()
    parsed_lanes: list[JSONMap] = []
    for lane in lanes:
        if not isinstance(lane, Mapping):
            raise ValueError("training lane must be an object")
        key = (lane.get("program_ref"), lane.get("master_seed"))
        if key in observed:
            raise ValueError("training terminal contains a duplicate lane")
        observed.add(key)
        status = lane.get("status")
        if status not in {TERMINAL_COMPLETE, TERMINAL_INVALID, TERMINAL_FAILED}:
            raise ValueError("training lane has invalid status")
        metrics = (
            lane.get("own_effective_damage"),
            lane.get("own_effective_dps"),
            lane.get("ttk_ms"),
        )
        if status != TERMINAL_COMPLETE and any(value is not None for value in metrics):
            raise ValueError("invalid/failed training lane metric must be null")
        parsed_lanes.append(dict(lane))
    if observed != expected:
        raise ValueError("training terminal lane coverage mismatch")
    return tuple(parsed_programs), tuple(parsed_lanes)


def freeze_training_winner_v1(
    *,
    campaign_path: str | Path,
    training_root: str | Path,
    output_path: str | Path,
    _training_shard_loader: Callable[
        [Any, TrainingShardV1],
        tuple[tuple[JSONMap, ...], tuple[JSONMap, ...]],
    ]
    | None = None,
) -> JSONMap:
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    root = Path(training_root).expanduser()
    seed_order = tuple(row.seed for row in campaign.train_examples)
    seed_index = {seed: index for index, seed in enumerate(seed_order)}
    if len(seed_index) != len(seed_order):
        raise AssertionError("campaign training seeds unexpectedly repeat")
    lane_count = len(seed_order)
    unseen = 255
    status_to_code = {
        TERMINAL_COMPLETE: 0,
        TERMINAL_INVALID: 1,
        TERMINAL_FAILED: 2,
    }
    code_to_status = {code: status for status, code in status_to_code.items()}
    programs_by_loadout: dict[str, tuple[JSONMap, ...]] = {}
    damage_by_pair: dict[tuple[str, str], array[float]] = {}
    dps_by_pair: dict[tuple[str, str], array[float]] = {}
    status_by_pair: dict[tuple[str, str], bytearray] = {}
    simulator_seed_by_loadout_seed: dict[tuple[str, int], int] = {}
    for shard in assign_training_shards_v1(campaign):
        if _training_shard_loader is None:
            terminal = _read_json(
                root / train_terminal_name_v1(shard), "training terminal"
            )
            programs, lanes = _validate_training_terminal_v1(
                terminal, campaign, shard
            )
        else:
            programs, lanes = _training_shard_loader(campaign, shard)
        first_shard = shard.loadout_id not in programs_by_loadout
        previous = programs_by_loadout.setdefault(shard.loadout_id, programs)
        if previous != programs:
            raise ValueError(
                f"candidate family differs across seed shards: {shard.loadout_id}"
            )
        if first_shard:
            for receipt in programs:
                key = (shard.loadout_id, receipt["program_ref"])
                damage_by_pair[key] = array("d", [math.nan]) * lane_count
                dps_by_pair[key] = array("d", [math.nan]) * lane_count
                status_by_pair[key] = bytearray([unseen]) * lane_count
        for lane in lanes:
            key = (shard.loadout_id, lane["program_ref"])
            try:
                index = seed_index[lane["master_seed"]]
                statuses = status_by_pair[key]
            except KeyError as error:
                raise ValueError(
                    "training lane refers to an unknown seed or program"
                ) from error
            if statuses[index] != unseen:
                raise ValueError("aggregated training lane repeats program/seed")
            status = lane["status"]
            statuses[index] = status_to_code[status]
            simulator_key = (shard.loadout_id, lane["master_seed"])
            simulator_seed = lane["simulator_seed"]
            previous_simulator_seed = simulator_seed_by_loadout_seed.setdefault(
                simulator_key, simulator_seed
            )
            if previous_simulator_seed != simulator_seed:
                raise ValueError(
                    "paired training lanes have different simulator seeds"
                )
            if status == TERMINAL_COMPLETE:
                damage_by_pair[key][index] = float(lane["own_effective_damage"])
                dps_by_pair[key][index] = float(lane["own_effective_dps"])

    complete_options: list[tuple[float, float, str, str, JSONMap]] = []
    training_rows: list[JSONMap] = []
    for loadout_id in campaign.loadout_ids:
        for receipt in programs_by_loadout[loadout_id]:
            key = (loadout_id, receipt["program_ref"])
            statuses = status_by_pair[key]
            if any(code == unseen for code in statuses):
                raise ValueError("aggregated training seed coverage mismatch")
            status_counts = Counter(code_to_status[code] for code in statuses)
            complete = status_counts == {TERMINAL_COMPLETE: lane_count}
            mean_damage = (
                mean(damage_by_pair[key])
                if complete
                else None
            )
            mean_dps = (
                mean(dps_by_pair[key])
                if complete
                else None
            )
            if complete:
                aggregate_status = TERMINAL_COMPLETE
            elif status_counts.get(TERMINAL_FAILED, 0):
                aggregate_status = TERMINAL_FAILED
            else:
                aggregate_status = TERMINAL_INVALID
            training_rows.append(
                {
                    "loadout_id": loadout_id,
                    "program_ref": receipt["program_ref"],
                    "program_id": receipt["program_id"],
                    "program_origin": receipt["program_origin"],
                    "status": aggregate_status,
                    "mean_own_effective_damage": mean_damage,
                    "mean_own_effective_dps": mean_dps,
                    "lane_status_counts": dict(sorted(status_counts.items())),
                }
            )
            if complete and mean_damage is not None and mean_dps is not None:
                complete_options.append(
                    (
                        mean_damage,
                        mean_dps,
                        loadout_id,
                        receipt["program_key"],
                        receipt,
                    )
                )
    def ranked(
        rows: Sequence[tuple[float, float, str, str, JSONMap]],
    ) -> list[tuple[float, float, str, str, JSONMap]]:
        return sorted(rows, key=lambda row: (-row[0], -row[1], row[2], row[3]))

    def option_summary(
        row: tuple[float, float, str, str, JSONMap] | None,
    ) -> JSONMap | None:
        if row is None:
            return None
        damage, dps, loadout_id, program_key, receipt = row
        return {
            "loadout_id": loadout_id,
            "program_ref": receipt["program_ref"],
            "program_id": receipt["program_id"],
            "program_key": program_key,
            "program_origin": receipt["program_origin"],
            "mean_own_effective_damage": damage,
            "mean_own_effective_dps": dps,
        }

    searched_options = [
        row
        for row in complete_options
        if row[4]["program_origin"]
        in {origin.value for origin in _SEARCHED_ORIGINS_V1}
    ]
    incumbent_options = [
        row
        for row in complete_options
        if row[4]["program_origin"]
        == ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT.value
    ]
    best_incumbent_option = ranked(incumbent_options)[0] if incumbent_options else None
    overall_best_option = ranked(complete_options)[0] if complete_options else None

    proposal_examples, selection_examples = (
        split_training_examples_for_selection_v1(campaign)
    )
    proposal_indexes = tuple(seed_index[row.seed] for row in proposal_examples)
    selection_indexes = tuple(seed_index[row.seed] for row in selection_examples)
    paired_train_rows: list[PairedCatDamageV1] = []
    paired_selection_rows: list[PairedCatDamageV1] = []
    compact_training_ranking: list[JSONMap] = []
    selection_failure_reason: str | None = None
    if searched_options:
        for loadout_id in campaign.loadout_ids:
            receipts = programs_by_loadout[loadout_id]
            searched_receipts = tuple(
                row
                for row in receipts
                if row["program_origin"]
                in {origin.value for origin in _SEARCHED_ORIGINS_V1}
            )
            zero_ref = _paired_zero_ref_v1(
                campaign, loadout_id, searched_receipts
            )
            complete_refs = tuple(
                row["program_ref"]
                for row in searched_receipts
                if all(
                    code == status_to_code[TERMINAL_COMPLETE]
                    for code in status_by_pair[(loadout_id, row["program_ref"])]
                )
            )
            if zero_ref not in complete_refs:
                selection_failure_reason = (
                    f"paired zero program did not complete every training "
                    f"seed for {loadout_id}"
                )
                break
            zero_damage = damage_by_pair[(loadout_id, zero_ref)]
            ranked_refs: list[tuple[float, str]] = []
            for candidate_ref in sorted(complete_refs):
                candidate_damage = damage_by_pair[(loadout_id, candidate_ref)]
                deltas = (
                    candidate_damage[index] - zero_damage[index]
                    for index in proposal_indexes
                )
                delta_mean = mean(deltas)
                compact_training_ranking.append(
                    {
                        "loadout_id": loadout_id,
                        "candidate_ref": candidate_ref,
                        "zero_residual_ref": zero_ref,
                        "is_zero_residual": candidate_ref == zero_ref,
                        "pair_count": len(proposal_indexes),
                        "mean_candidate_damage": mean(
                            candidate_damage[index] for index in proposal_indexes
                        ),
                        "mean_zero_residual_damage": mean(
                            zero_damage[index] for index in proposal_indexes
                        ),
                        "mean_paired_damage_delta": delta_mean,
                    }
                )
                ranked_refs.append((delta_mean, candidate_ref))
            selected_ref = sorted(
                ranked_refs,
                key=lambda row: (
                    -row[0],
                    row[1] != zero_ref,
                    row[1],
                ),
            )[0][1]
            # The general selector only needs the per-loadout training winner
            # and its exact zero arm after the compact reducer has applied the
            # same deterministic training ranking.  This bounds object
            # materialization by 2 * loadouts * seeds instead of
            # candidates * loadouts * seeds.
            selected_refs = (
                (zero_ref,)
                if selected_ref == zero_ref
                else (zero_ref, selected_ref)
            )
            for candidate_ref in selected_refs:
                candidate_damage = damage_by_pair[(loadout_id, candidate_ref)]
                for cohort, indexes in (
                    ("TRAIN", proposal_indexes),
                    ("VALIDATION", selection_indexes),
                ):
                    destination = (
                        paired_train_rows
                        if cohort == "TRAIN"
                        else paired_selection_rows
                    )
                    destination.extend(
                        PairedCatDamageV1(
                            cohort=cohort,
                            loadout_id=loadout_id,
                            candidate_ref=candidate_ref,
                            zero_residual_ref=zero_ref,
                            seed=seed_order[index],
                            candidate_damage=candidate_damage[index],
                            zero_residual_damage=zero_damage[index],
                        )
                        for index in indexes
                    )

    if not searched_options or selection_failure_reason is not None:
        payload = {
            "schema": FREEZE_TERMINAL_SCHEMA,
            "terminal_status": TERMINAL_FAILED,
            "campaign_id": campaign.campaign_id,
            "build_id": campaign.build_id,
            "campaign_contract": _campaign_contract_v1(campaign),
            "winner": None,
            "frozen_program": None,
            "metric": None,
            "best_incumbent": option_summary(best_incumbent_option),
            "overall_best": option_summary(overall_best_option),
            "searched_minus_best_incumbent_train_gap": None,
            "training_rows": training_rows,
            "selection_gate": None,
            "reason": selection_failure_reason
            or "no searched program completed every training seed",
            "completed_at": _utc_now(),
        }
    else:
        selection_gate = select_paired_cat_residual_v1(
            training_rows=tuple(paired_train_rows),
            validation_rows=tuple(paired_selection_rows),
            minimum_validation_pairs=DEFAULT_MINIMUM_VALIDATION_PAIRS,
        )
        # Preserve the complete pre-v4 training-ranking artifact even though
        # only each loadout's deterministic winner and zero arm need to enter
        # the validation selector as per-seed Python objects.
        selection_gate["training_ranking"] = sorted(
            compact_training_ranking,
            key=lambda row: (row["loadout_id"], row["candidate_ref"]),
        )
        selection_gate["contract"].update(
            {
                "freeze_aggregation": "DENSE_NUMERIC_ARRAYS_BY_PROGRAM_AND_SEED",
                "all_complete_candidates_retained_in_training_ranking": True,
                "materialized_pair_row_count": (
                    len(paired_train_rows) + len(paired_selection_rows)
                ),
                "materialized_pair_scope": (
                    "PER_LOADOUT_TRAIN_WINNER_AND_PAIRED_ZERO_ONLY"
                ),
            }
        )
        accepted = selection_gate["accepted_program"]
        loadout_id = accepted["loadout_id"]
        accepted_ref = accepted["program_ref"]
        matching = [
            row
            for row in searched_options
            if row[2] == loadout_id and row[4]["program_ref"] == accepted_ref
        ]
        if len(matching) != 1:
            raise ValueError("selection gate accepted an unresolved searched program")
        winner = matching[0]
        damage, dps, loadout_id, program_key, receipt = winner
        frozen_program = causal_action_program_from_dict_v1(receipt["program"])
        if (
            frozen_program.program_key() != program_key
            or not _is_searched_origin_v1(frozen_program.origin)
        ):
            raise ValueError("winner changed during semantic freeze")
        train_gap = (
            {
                "mean_own_effective_damage": damage - best_incumbent_option[0],
                "mean_own_effective_dps": dps - best_incumbent_option[1],
            }
            if best_incumbent_option is not None
            else None
        )
        payload = {
            "schema": FREEZE_TERMINAL_SCHEMA,
            "terminal_status": TERMINAL_COMPLETE,
            "campaign_id": campaign.campaign_id,
            "build_id": campaign.build_id,
            "campaign_contract": _campaign_contract_v1(campaign),
            "winner": {
                "loadout_id": loadout_id,
                "program_ref": receipt["program_ref"],
                "program_id": frozen_program.program_id,
                "program_key": program_key,
                "program_origin": frozen_program.origin.value,
                "mean_own_effective_damage": damage,
                "mean_own_effective_dps": dps,
                "selection_validation_mean_own_effective_damage": next(
                    row["accepted_validation_mean_damage"]
                    for row in selection_gate["per_loadout_admission"]
                    if row["loadout_id"] == loadout_id
                ),
            },
            "frozen_program": frozen_program.to_dict(),
            "metric": {
                "mean_own_effective_damage": damage,
                "selection_validation_mean_own_effective_damage": next(
                    row["accepted_validation_mean_damage"]
                    for row in selection_gate["per_loadout_admission"]
                    if row["loadout_id"] == loadout_id
                ),
            },
            "best_incumbent": option_summary(best_incumbent_option),
            "overall_best": option_summary(overall_best_option),
            "searched_minus_best_incumbent_train_gap": train_gap,
            "selection_gate": selection_gate,
            "training_rows": training_rows,
            "completed_at": _utc_now(),
            "contract": {
                "all_loadouts_and_training_seed_shards_reduced": True,
                "winner_selected_without_heldout_examples": True,
                "winner_must_be_searched_not_imported_incumbent": True,
                "candidate_proposal_used_only_stratified_even_occurrences": True,
                "selection_validation_used_only_stratified_odd_occurrences": True,
                "per_loadout_nonzero_candidate_requires_positive_paired_lcb": True,
                "final_loadout_selected_by_accepted_validation_absolute_damage": True,
                "heldout_cases_materialized": False,
                "failed_lanes_scored_as_zero": False,
            },
        }
    _atomic_create_json(output_path, payload)
    return payload


def _load_frozen_v1(path: str | Path, campaign: Any) -> tuple[JSONMap, CausalActionProgramV1]:
    value = _read_json(path, "frozen winner")
    if (
        value.get("schema") != FREEZE_TERMINAL_SCHEMA
        or value.get("campaign_id") != campaign.campaign_id
        or value.get("build_id") != campaign.build_id
        or value.get("campaign_contract") != _campaign_contract_v1(campaign)
        or value.get("terminal_status") != TERMINAL_COMPLETE
        or not isinstance(value.get("winner"), Mapping)
    ):
        raise ValueError("frozen winner is not a complete matching campaign artifact")
    program = causal_action_program_from_dict_v1(value.get("frozen_program"))
    if (
        value["winner"].get("program_id") != program.program_id
        or value["winner"].get("program_key") != program.program_key()
        or value["winner"].get("program_origin") != program.origin.value
        or value["winner"].get("program_ref") != program.program_id
        or value["winner"].get("loadout_id") not in campaign.loadout_ids
        or not _is_searched_origin_v1(program.origin)
    ):
        raise ValueError("frozen winner identity is inconsistent")
    return value, program


def run_evaluation_shard_v1(
    *,
    campaign_path: str | Path,
    frozen_path: str | Path,
    seed_shard_index: int,
    output_path: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
) -> JSONMap:
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    _positive_int(replay_workers, "replay_workers")
    matches = [
        row
        for row in assign_evaluation_shards_v1(campaign)
        if row.seed_shard_index == seed_shard_index
    ]
    if len(matches) != 1:
        raise ValueError("seed_shard_index is not one evaluation shard")
    shard = matches[0]
    frozen, candidate_program = _load_frozen_v1(frozen_path, campaign)
    loadout_id = frozen["winner"]["loadout_id"]
    resolved_runtime = runtime or build_default_remote_runtime_v1(
        campaign,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )

    cases, cases_by_simulator, simulator_by_master = _build_cases_v1(
        resolved_runtime, campaign, loadout_id, shard.examples
    )
    case_by_master = dict(zip((row.seed for row in shard.examples), cases, strict=True))
    incumbents = tuple(
        resolved_runtime.incumbent_program_factory(campaign.build_id)
    )
    if len(incumbents) != 3:
        raise ValueError("evaluation requires exactly three imported incumbents")
    panel: list[tuple[str, str, CausalActionProgramV1]] = [
        ("CANDIDATE", "frozen_candidate", candidate_program)
    ]
    panel.extend(
        ("BASELINE", program.selector.source_policy_id, program)
        for program in incumbents
    )
    panel.extend(
        ("BASELINE", policy_id, program)
        for policy_id, program in _shared_burst_control_programs_v1(
            campaign, incumbents
        )
    )
    baseline_ids = [policy_id for role, policy_id, _ in panel if role == "BASELINE"]
    if len(baseline_ids) != len(set(baseline_ids)):
        raise ValueError("imported incumbent policy IDs must be unique")
    replay = resolved_runtime.program_replay_factory(loadout_id, cases_by_simulator)
    program_keys = {id(program): program.program_key() for _, _, program in panel}

    def evaluate(
        job: tuple[str, str, CausalActionProgramV1, TwoWaveExampleV1]
    ) -> JSONMap:
        role, policy_id, program, example = job
        simulator_seed = simulator_by_master[example.seed]
        try:
            outcome = replay.replay(
                simulator_seed, program, max_decisions=campaign.max_decisions
            )
            lane = _lane_from_outcome_v1(
                outcome,
                case=case_by_master[example.seed],
                master_seed=example.seed,
                simulator_seed=simulator_seed,
            )
        except Exception as error:
            lane = _failed_lane_v1(
                master_seed=example.seed,
                simulator_seed=simulator_seed,
                error=error,
            )
        lane.update(
            {
                "role": role,
                "policy_id": policy_id,
                "program_id": program.program_id,
                "program_key": program_keys[id(program)],
                "program_origin": program.origin.value,
                "first_wave_arrival_ms": example.first_wave_arrival_ms,
            }
        )
        return lane

    jobs = [
        (role, policy_id, program, example)
        for example in shard.examples
        for role, policy_id, program in panel
    ]
    with ThreadPoolExecutor(max_workers=min(replay_workers, len(jobs))) as executor:
        lanes = list(executor.map(evaluate, jobs))
    terminal_status = terminal_status_for_lanes_v1(lanes)
    candidate_lanes = [row for row in lanes if row["role"] == "CANDIDATE"]
    payload = {
        "schema": EVAL_TERMINAL_SCHEMA,
        "terminal_status": terminal_status,
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": _campaign_contract_v1(campaign),
        "selected_loadout_id": loadout_id,
        "frozen_program_id": candidate_program.program_id,
        "frozen_program_key": candidate_program.program_key(),
        "frozen_program_origin": candidate_program.origin.value,
        "seed_shard_index": seed_shard_index,
        "examples": [row.to_dict() for row in shard.examples],
        "baseline_policy_ids": baseline_ids,
        "lanes": lanes,
        "lane_status_counts": dict(
            sorted(Counter(row["status"] for row in lanes).items())
        ),
        "metric": (
            {
                "mean_candidate_own_effective_damage": mean(
                    float(row["own_effective_damage"]) for row in candidate_lanes
                )
            }
            if terminal_status == TERMINAL_COMPLETE
            else None
        ),
        "completed_at": _utc_now(),
        "contract": {
            "search_or_generator_invocations": 0,
            "heldout_cases_materialized_after_freeze_read": True,
            "same_loadout_case_seed_and_replay_interface_for_full_panel": True,
            "shared_burst_controls_reuse_frozen_parent_rules": (
                _is_fixed_parent_search_v1(campaign)
            ),
            "shared_burst_controls_independently_optimized": False,
            "invalid_or_failed_lane_metrics_null": all(
                row["own_effective_damage"] is None
                and row["own_effective_dps"] is None
                and row["ttk_ms"] is None
                for row in lanes
                if row["status"] != TERMINAL_COMPLETE
            ),
        },
    }
    _atomic_create_json(output_path, payload)
    return payload


def summarize_evaluation_v1(
    *,
    campaign_path: str | Path,
    frozen_path: str | Path,
    evaluation_root: str | Path,
    output_path: str | Path,
) -> JSONMap:
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    frozen, candidate_program = _load_frozen_v1(frozen_path, campaign)
    root = Path(evaluation_root).expanduser()
    all_lanes: list[JSONMap] = []
    baseline_ids: list[str] | None = None
    for shard in assign_evaluation_shards_v1(campaign):
        value = _read_json(root / evaluation_terminal_name_v1(shard), "evaluation terminal")
        if (
            value.get("schema") != EVAL_TERMINAL_SCHEMA
            or value.get("campaign_id") != campaign.campaign_id
            or value.get("build_id") != campaign.build_id
            or value.get("campaign_contract") != _campaign_contract_v1(campaign)
            or value.get("selected_loadout_id") != frozen["winner"]["loadout_id"]
            or value.get("frozen_program_key") != candidate_program.program_key()
            or value.get("frozen_program_origin") != candidate_program.origin.value
            or value.get("seed_shard_index") != shard.seed_shard_index
            or value.get("examples") != [row.to_dict() for row in shard.examples]
            or not isinstance(value.get("lanes"), list)
        ):
            raise ValueError(f"evaluation terminal identity mismatch: {shard.work_id}")
        observed_ids = value.get("baseline_policy_ids")
        if not isinstance(observed_ids, list) or not observed_ids:
            raise ValueError("evaluation terminal lacks baseline IDs")
        if baseline_ids is None:
            baseline_ids = list(observed_ids)
        elif baseline_ids != observed_ids:
            raise ValueError("baseline panel differs across evaluation shards")
        all_lanes.extend(dict(row) for row in value["lanes"] if isinstance(row, Mapping))
    assert baseline_ids is not None
    expected_seeds = {row.seed for row in campaign.evaluation_examples}
    arrival_by_seed = {
        row.seed: row.first_wave_arrival_ms
        for row in campaign.evaluation_examples
    }
    expected_ids = {"frozen_candidate", *baseline_ids}
    observed_pairs: set[tuple[int, str]] = set()
    for lane in all_lanes:
        seed = lane.get("master_seed")
        if (
            seed not in arrival_by_seed
            or lane.get("first_wave_arrival_ms") != arrival_by_seed[seed]
        ):
            raise ValueError(
                "held-out lane arrival differs from the predeclared "
                "campaign seed binding"
            )
        pair = (lane.get("master_seed"), lane.get("policy_id"))
        if pair in observed_pairs:
            raise ValueError("duplicate held-out policy/seed lane")
        observed_pairs.add(pair)
        if lane.get("status") not in {
            TERMINAL_COMPLETE,
            TERMINAL_INVALID,
            TERMINAL_FAILED,
        }:
            raise ValueError("held-out lane has invalid terminal status")
        if lane.get("status") != TERMINAL_COMPLETE and any(
            lane.get(name) is not None
            for name in ("own_effective_damage", "own_effective_dps", "ttk_ms")
        ):
            raise ValueError("invalid/failed held-out lane metric must be null")
    if observed_pairs != {
        (seed, policy_id) for seed in expected_seeds for policy_id in expected_ids
    }:
        raise ValueError("held-out panel lane coverage mismatch")
    terminal_status = terminal_status_for_lanes_v1(all_lanes)
    metrics: JSONMap | None = None
    if terminal_status == TERMINAL_COMPLETE:
        def complete_panel_metrics(rows: Sequence[Mapping[str, Any]]) -> JSONMap:
            by_pair = {
                (row["master_seed"], row["policy_id"]): row
                for row in rows
            }
            panel_seeds = sorted(
                row["master_seed"]
                for row in rows
                if row["policy_id"] == "frozen_candidate"
            )
            policy_means: JSONMap = {}
            for policy_id in ("frozen_candidate", *baseline_ids):
                policy_rows = [
                    by_pair[(seed, policy_id)] for seed in panel_seeds
                ]
                policy_means[policy_id] = {
                    "mean_own_effective_damage": mean(
                        float(row["own_effective_damage"])
                        for row in policy_rows
                    ),
                    "mean_own_effective_dps": mean(
                        float(row["own_effective_dps"])
                        for row in policy_rows
                    ),
                    "mean_ttk_ms": mean(
                        float(row["ttk_ms"]) for row in policy_rows
                    ),
                }

            paired_statistics: JSONMap = {}
            for policy_id in baseline_ids:
                deltas = [
                    float(
                        by_pair[(seed, "frozen_candidate")][
                            "own_effective_damage"
                        ]
                    )
                    - float(
                        by_pair[(seed, policy_id)]["own_effective_damage"]
                    )
                    for seed in panel_seeds
                ]
                delta_mean = mean(deltas)
                sample_standard_deviation = (
                    stdev(deltas) if len(deltas) >= 2 else None
                )
                standard_error = (
                    sample_standard_deviation / math.sqrt(len(deltas))
                    if sample_standard_deviation is not None
                    else None
                )
                critical_value = NormalDist().inv_cdf(0.975)
                normal_interval = (
                    {
                        "lower_bound": delta_mean
                        - critical_value * standard_error,
                        "upper_bound": delta_mean
                        + critical_value * standard_error,
                    }
                    if standard_error is not None
                    else None
                )
                paired_statistics[policy_id] = {
                    "pair_count": len(deltas),
                    "mean_damage_delta": delta_mean,
                    "sample_standard_deviation": sample_standard_deviation,
                    "standard_error": standard_error,
                    "confidence_level": 0.95,
                    "normal_critical_value": critical_value,
                    "normal_confidence_interval": normal_interval,
                    "win_tie_loss": {
                        "wins": sum(value > 0.0 for value in deltas),
                        "ties": sum(value == 0.0 for value in deltas),
                        "losses": sum(value < 0.0 for value in deltas),
                    },
                }

            candidate_damage = policy_means["frozen_candidate"][
                "mean_own_effective_damage"
            ]
            return {
                "policy_means": policy_means,
                # Preserve the original public scalar field alongside the
                # richer paired distribution summary.
                "paired_candidate_minus_baseline_mean_damage": {
                    policy_id: paired_statistics[policy_id][
                        "mean_damage_delta"
                    ]
                    for policy_id in baseline_ids
                },
                "paired_candidate_minus_baseline_damage_statistics": (
                    paired_statistics
                ),
                "candidate_strictly_better_than_all_native_baselines": all(
                    candidate_damage
                    > policy_means[policy_id]["mean_own_effective_damage"]
                    for policy_id in baseline_ids
                ),
                "candidate_strictly_better_than_all_reported_baselines": all(
                    candidate_damage
                    > policy_means[policy_id]["mean_own_effective_damage"]
                    for policy_id in baseline_ids
                ),
            }

        metrics = complete_panel_metrics(all_lanes)
        metrics["by_first_wave_arrival_ms"] = [
            {
                "first_wave_arrival_ms": arrival_ms,
                "seed_count": len(stratum_seeds),
                **complete_panel_metrics(
                    [
                        row
                        for row in all_lanes
                        if row["master_seed"] in stratum_seeds
                    ]
                ),
            }
            for arrival_ms in sorted(set(arrival_by_seed.values()))
            for stratum_seeds in (
                {
                    seed
                    for seed, bound_arrival in arrival_by_seed.items()
                    if bound_arrival == arrival_ms
                },
            )
        ]
    payload = {
        "schema": SUMMARY_TERMINAL_SCHEMA,
        "terminal_status": terminal_status,
        "campaign_id": campaign.campaign_id,
        "build_id": campaign.build_id,
        "campaign_contract": _campaign_contract_v1(campaign),
        "selected_loadout_id": frozen["winner"]["loadout_id"],
        "frozen_program_id": candidate_program.program_id,
        "frozen_program_key": candidate_program.program_key(),
        "frozen_program_origin": candidate_program.origin.value,
        "evaluation_seed_count": len(expected_seeds),
        "baseline_policy_ids": baseline_ids,
        "lane_status_counts": dict(
            sorted(Counter(row["status"] for row in all_lanes).items())
        ),
        "metric": metrics,
        "completed_at": _utc_now(),
        "claim_boundary": {
            "model_defined_development_only": True,
            "real_upper_kara_superiority_proven": False,
            "failed_lanes_scored_as_zero": False,
            "raw_default_baselines_are_same_resource_controls": False,
            "shared_burst_controls_use_same_loadout_and_planner_rules": (
                _is_fixed_parent_search_v1(campaign)
            ),
            "shared_burst_controls_independently_optimized_per_rotation": False,
        },
    }
    _atomic_create_json(output_path, payload)
    return payload


def _fatal_terminal_v1(schema: str, error: BaseException) -> JSONMap:
    return {
        "schema": schema,
        "terminal_status": TERMINAL_FAILED,
        "metric": None,
        "error_type": type(error).__name__,
        "reason": str(error),
        "completed_at": _utc_now(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)

    def common_runtime(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--campaign", type=Path, required=True)
        subparser.add_argument("--bridge", type=Path, required=True)
        subparser.add_argument("--bridge-cwd", type=Path, required=True)
        subparser.add_argument("--runtime-binding", type=Path, required=True)
        subparser.add_argument(
            "--replay-workers", type=int, default=DEFAULT_REPLAY_WORKERS
        )
        subparser.add_argument("--output", type=Path, required=True)

    train = subparsers.add_parser("train")
    common_runtime(train)
    train.add_argument("--offline-guide-artifact", type=Path, required=True)
    train.add_argument("--loadout-id", required=True)
    train.add_argument("--seed-shard-index", type=int, required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--campaign", type=Path, required=True)
    freeze.add_argument("--training-root", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)

    evaluation = subparsers.add_parser("eval")
    common_runtime(evaluation)
    evaluation.add_argument("--frozen", type=Path, required=True)
    evaluation.add_argument("--seed-shard-index", type=int, required=True)

    summary = subparsers.add_parser("summarize")
    summary.add_argument("--campaign", type=Path, required=True)
    summary.add_argument("--frozen", type=Path, required=True)
    summary.add_argument("--evaluation-root", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schema = {
        "train": TRAIN_TERMINAL_SCHEMA,
        "freeze": FREEZE_TERMINAL_SCHEMA,
        "eval": EVAL_TERMINAL_SCHEMA,
        "summarize": SUMMARY_TERMINAL_SCHEMA,
    }[args.phase]
    try:
        if args.phase == "train":
            payload = run_training_shard_v1(
                campaign_path=args.campaign,
                loadout_id=args.loadout_id,
                seed_shard_index=args.seed_shard_index,
                output_path=args.output,
                replay_workers=args.replay_workers,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
                offline_guide_artifact_path=args.offline_guide_artifact,
            )
        elif args.phase == "freeze":
            payload = freeze_training_winner_v1(
                campaign_path=args.campaign,
                training_root=args.training_root,
                output_path=args.output,
            )
        elif args.phase == "eval":
            payload = run_evaluation_shard_v1(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                seed_shard_index=args.seed_shard_index,
                output_path=args.output,
                replay_workers=args.replay_workers,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
            )
        else:
            payload = summarize_evaluation_v1(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                evaluation_root=args.evaluation_root,
                output_path=args.output,
            )
    except Exception as error:
        payload = _fatal_terminal_v1(schema, error)
        try:
            _atomic_create_json(args.output, payload)
        except FileExistsError:
            pass
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = (
    "DEFAULT_REPLAY_WORKERS",
    "EVAL_TERMINAL_SCHEMA",
    "FREEZE_TERMINAL_SCHEMA",
    "RemoteCausalProgramRuntimeV1",
    "ImportedIncumbentProgramProposalGuideV1",
    "NativeTrainingObservableFrontierProviderV1",
    "SUMMARY_TERMINAL_SCHEMA",
    "TRAIN_TERMINAL_SCHEMA",
    "build_default_remote_runtime_v1",
    "build_default_training_proposal_guides_v1",
    "evaluation_terminal_name_v1",
    "freeze_training_winner_v1",
    "run_evaluation_shard_v1",
    "run_training_shard_v1",
    "summarize_evaluation_v1",
    "train_terminal_name_v1",
)
