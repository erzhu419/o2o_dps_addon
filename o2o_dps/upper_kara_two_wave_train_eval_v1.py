"""Train-select-freeze-evaluate a continuous two-wave action program.

The driver deliberately does not define the combinatorial program search.  It
scores three imported reactive incumbents and any caller-generated declarative
programs through the same action-program replay interface, selects one
``(loadout, program)`` pair using complete training outcomes only, freezes the
semantic program, and only then constructs held-out cases.

Every case keeps both waves in one native simulator process.  Consequently a
decision to skip a burst on a nearly-dead first pack leaves the actual cooldown,
inventory, aura, and equipment state available to a later guarded decision.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping, Protocol, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    CausalObservationProjectorV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .development_two_wave_build_panel_v1 import BUILD_IDS
from .development_wave_panel_v1 import DEFAULT_BINDING, PROTOCOL_ID, WORKSPACE_ROOT
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import derive_simulator_seed
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_two_wave_baseline_panel_v1 import (
    baseline_policy_ids_for_build_v1,
    run_upper_kara_continuous_two_wave_baselines_v1,
)
from .upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
    search_cell_from_continuous_two_wave_case_v1,
)
from .wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_continuous_two_wave_train_eval/v1"
CANDIDATE_POLICY_ID = "o2o.upper_kara.continuous_two_wave.frozen_program_v1"
OBSERVATION_CONTRACT_ID_V1 = "policy_observation_causal_projection/v1"
DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1 = 512
REFERENCE_IDLE_WAIT_MS_V1 = 250


@dataclass(frozen=True)
class TwoWaveExampleV1:
    """One master seed and causally-observed first-pack arrival delay."""

    seed: int
    first_wave_arrival_ms: int

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed <= 0:
            raise ValueError("seed must be a positive integer")
        if (
            isinstance(self.first_wave_arrival_ms, bool)
            or not isinstance(self.first_wave_arrival_ms, int)
            or not 0 <= self.first_wave_arrival_ms < 10_000
        ):
            raise ValueError(
                "first_wave_arrival_ms must be an integer in [0, 10000)"
            )

    def to_dict(self) -> JSONMap:
        return {
            "seed": self.seed,
            "first_wave_arrival_ms": self.first_wave_arrival_ms,
        }


class ActionProgramReplayV1(Protocol):
    def replay(
        self,
        seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int,
    ) -> ScheduleReplayOutcomeV1: ...


CaseBuilderV1 = Callable[..., Any]
BaselineRunnerV1 = Callable[..., Mapping[str, Any]]
ProgramReplayFactoryV1 = Callable[
    [str, Mapping[int, Any]], ActionProgramReplayV1
]
SearchedProgramGeneratorV1 = Callable[..., Sequence[CausalActionProgramV1]]
ObservationProjectorFactoryV1 = Callable[
    [str, Mapping[int, Any]], CausalObservationProjectorV1
]


def _native_replay_factory_v1(
    *,
    bridge_path: Path,
    bridge_cwd: Path,
    observation_projector_factory: ObservationProjectorFactoryV1,
    imported_bindings: tuple[ImportedReactiveProgramBindingV1, ...],
) -> ProgramReplayFactoryV1:
    def build(
        loadout_id: str, cases_by_simulator: Mapping[int, Any]
    ) -> ActionProgramReplayV1:
        projector = observation_projector_factory(
            loadout_id, cases_by_simulator
        )
        if not callable(projector):
            raise TypeError("observation projector factory returned a non-callable")
        return NativeDynamicV3ActionProgramReplayV1(
            lambda: SimulatorBridgePrecombatV1(bridge_path, cwd=bridge_cwd),
            lambda seed: cases_by_simulator[seed],
            projector,
            imported_bindings=imported_bindings,
        )

    return build


def _examples_v1(
    values: Sequence[TwoWaveExampleV1], label: str
) -> tuple[TwoWaveExampleV1, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    rows = tuple(values)
    if not rows or any(not isinstance(row, TwoWaveExampleV1) for row in rows):
        raise ValueError(f"{label} must contain TwoWaveExampleV1 values")
    seeds = [row.seed for row in rows]
    if len(seeds) != len(set(seeds)):
        raise ValueError(f"{label} seeds must be unique")
    return rows


def imported_incumbent_programs_v1(build_id: str) -> tuple[CausalActionProgramV1, ...]:
    """Represent the three native baselines in the unified program language."""

    return tuple(
        CausalActionProgramV1(
            program_id=f"imported-incumbent::{policy_id}",
            selector=ImportedReactiveSelectorV1(
                binding_id=policy_id,
                source_policy_id=policy_id,
                observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
            ),
            origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
            source_refs=(policy_id,),
        )
        for policy_id in baseline_policy_ids_for_build_v1(build_id)
    )


def _finite_nonnegative(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _required_targets_dead(case: Any, state: Mapping[str, Any]) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec.get("required_target_indices")
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
    events = case.dynamic_load.config.attackability_events
    first_required_target = case.case_spec["required_target_indices"][0]
    candidates = tuple(
        row.time_ms
        for row in events
        if row.target_index == first_required_target and row.attackable
    )
    if not candidates:
        raise ValueError("case has no attackable start for its first required target")
    return min(candidates)


def _outcome_lane_v1(
    outcome: ScheduleReplayOutcomeV1,
    *,
    case: Any,
    master_seed: int,
    simulator_seed: int,
) -> JSONMap:
    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("program replay returned a non-ScheduleReplayOutcomeV1 value")
    if outcome.seed != simulator_seed:
        raise ValueError("program replay returned a different simulator seed")
    complete = bool(
        outcome.status is ReplayStatusV1.COMPLETE
        and _required_targets_dead(case, outcome.state)
    )
    damage: float | None = None
    elapsed_ms: int | None = None
    dps: float | None = None
    error: str | None = None
    if complete:
        try:
            damage = _finite_nonnegative(outcome.effective_damage)
            # Native program replay reports simulator time from the beginning
            # of the precombat window.  The standalone controller lane starts
            # its elapsed clock when the first required target becomes
            # attackable to the player (pull + any late-arrival delay).
            elapsed_ms = outcome.elapsed_ms - _first_player_attackable_ms_v1(case)
        except (TypeError, ValueError) as exception:
            error = f"{type(exception).__name__}: {exception}"
        if damage is None or elapsed_ms is None or elapsed_ms <= 0:
            complete = False
            damage = None
            elapsed_ms = None
            error = error or "complete replay has malformed terminal metrics"
        else:
            dps = damage * 1000.0 / elapsed_ms
    elif outcome.status is ReplayStatusV1.INVALID:
        error = outcome.invalid_reason
    else:
        error = "program did not kill every required target"
    return {
        "master_seed": master_seed,
        "simulator_seed": simulator_seed,
        "status": "COMPLETED" if complete else "FAILED",
        "own_effective_damage": damage if complete else None,
        "own_effective_dps": dps if complete else None,
        "ttk_ms": elapsed_ms if complete else None,
        "error": error,
    }


def _failed_lane_v1(
    *, master_seed: int, simulator_seed: int, error: BaseException | str
) -> JSONMap:
    message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return {
        "master_seed": master_seed,
        "simulator_seed": simulator_seed,
        "status": "FAILED",
        "own_effective_damage": None,
        "own_effective_dps": None,
        "ttk_ms": None,
        "error": message,
    }


def _programs_for_loadout_v1(
    *,
    loadout_id: str,
    train_examples: tuple[TwoWaveExampleV1, ...],
    train_cases: tuple[Any, ...],
    incumbent_programs: tuple[CausalActionProgramV1, ...],
    searched_program_generator: SearchedProgramGeneratorV1 | None,
) -> tuple[CausalActionProgramV1, ...]:
    searched = (
        tuple(
            searched_program_generator(
                loadout_id=loadout_id,
                train_examples=train_examples,
                train_cases=train_cases,
            )
        )
        if searched_program_generator is not None
        else ()
    )
    programs = (*incumbent_programs, *searched)
    if any(not isinstance(program, CausalActionProgramV1) for program in programs):
        raise TypeError("program generator returned a non-CausalActionProgramV1 value")
    ids = [program.program_id for program in programs]
    keys = [program.program_key() for program in programs]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate program IDs must be unique within a loadout")
    if len(keys) != len(set(keys)):
        raise ValueError("candidate program semantic keys must be unique within a loadout")
    return tuple(programs)


def _normalize_baseline_rows_v1(
    payload: Mapping[str, Any],
    *,
    expected_policy_ids: tuple[str, ...],
    case: Any,
    build_id: str,
    loadout_id: str,
    example: TwoWaveExampleV1,
    simulator_seed: int,
    executed_contract_sha256: str,
) -> tuple[list[JSONMap], list[str]]:
    blockers: list[str] = []
    expected_cell = search_cell_from_continuous_two_wave_case_v1(case).to_dict()
    checks = (
        (payload.get("master_seed") == example.seed, "BASELINE_MASTER_SEED_MISMATCH"),
        (payload.get("simulator_seed") == simulator_seed, "BASELINE_SIMULATOR_SEED_MISMATCH"),
        (payload.get("build_id") == build_id, "BASELINE_BUILD_MISMATCH"),
        (payload.get("loadout_id") == loadout_id, "BASELINE_LOADOUT_MISMATCH"),
        (
            payload.get("first_wave_arrival_ms") == example.first_wave_arrival_ms,
            "BASELINE_ARRIVAL_MISMATCH",
        ),
        (
            payload.get("exact_request_sha256") == case.dynamic_load.request_sha256,
            "BASELINE_REQUEST_MISMATCH",
        ),
        (
            payload.get("dynamic_load_contract_sha256") == executed_contract_sha256,
            "BASELINE_DYNAMIC_ENVIRONMENT_MISMATCH",
        ),
        (payload.get("search_cell") == expected_cell, "BASELINE_SEARCH_CELL_MISMATCH"),
        (
            payload.get("baseline_policy_ids") == list(expected_policy_ids),
            "BASELINE_POLICY_SET_OR_ORDER_MISMATCH",
        ),
    )
    blockers.extend(code for passed, code in checks if not passed)
    contract_mismatch = bool(blockers)
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raw_rows = []
        blockers.append("BASELINE_ROWS_MISSING")
        contract_mismatch = True
    by_id = {
        row.get("policy_id"): deepcopy(dict(row))
        for row in raw_rows
        if isinstance(row, Mapping) and row.get("policy_id") in expected_policy_ids
    }
    rows: list[JSONMap] = []
    for policy_id in expected_policy_ids:
        row = by_id.get(policy_id)
        if row is None:
            row = {"policy_id": policy_id, "status": "FAILED", "error": "row missing"}
            blockers.append(f"BASELINE_ROW_MISSING:{policy_id}")
        damage = _finite_nonnegative(row.get("own_effective_damage"))
        dps = _finite_nonnegative(row.get("own_effective_dps"))
        ttk = row.get("ttk_ms")
        complete = bool(
            not contract_mismatch
            and row.get("status") == "COMPLETED"
            and damage is not None
            and dps is not None
            and type(ttk) is int
            and ttk > 0
        )
        if not complete and row.get("status") == "COMPLETED":
            row["status"] = "FAILED_MALFORMED_OR_UNPAIRED"
            row["error"] = row.get("error") or "baseline lane is not comparison eligible"
        row.update({
            "role": "BASELINE",
            "master_seed": example.seed,
            "simulator_seed": simulator_seed,
            "own_effective_damage": damage if complete else None,
            "own_effective_dps": dps if complete else None,
            "ttk_ms": ttk if complete else None,
            "comparison_eligible": complete,
        })
        rows.append(row)
    return rows, list(dict.fromkeys(blockers))


def _complete_result_row(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("status") == "COMPLETED"
        and _finite_nonnegative(row.get("own_effective_damage")) is not None
        and _finite_nonnegative(row.get("own_effective_dps")) is not None
        and type(row.get("ttk_ms")) is int
        and row["ttk_ms"] > 0
    )


def run_upper_kara_continuous_two_wave_train_eval_v1(
    *,
    build_id: str,
    train_examples: Sequence[TwoWaveExampleV1],
    evaluation_examples: Sequence[TwoWaveExampleV1],
    program_replay_factory: ProgramReplayFactoryV1 | None = None,
    observation_projector_factory: ObservationProjectorFactoryV1 | None = None,
    imported_bindings: Sequence[ImportedReactiveProgramBindingV1] = (),
    searched_program_generator: SearchedProgramGeneratorV1 | None = None,
    imported_incumbent_programs: Sequence[CausalActionProgramV1] | None = None,
    loadout_ids: Sequence[str] = BURST_LOADOUT_IDS_V1,
    pull_time_ms: int = 3_000,
    max_decisions: int = DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1,
    bridge_path: Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    case_builder: CaseBuilderV1 = build_upper_kara_continuous_two_wave_burst_case_v1,
    baseline_runner: BaselineRunnerV1 = run_upper_kara_continuous_two_wave_baselines_v1,
) -> JSONMap:
    """Select on train examples, then run a paired held-out four-way panel."""

    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    train = _examples_v1(train_examples, "train_examples")
    evaluation = _examples_v1(evaluation_examples, "evaluation_examples")
    overlap = sorted({row.seed for row in train} & {row.seed for row in evaluation})
    if overlap:
        raise ValueError(f"training and evaluation seeds overlap: {overlap}")
    if (
        isinstance(pull_time_ms, bool)
        or not isinstance(pull_time_ms, int)
        or pull_time_ms <= 0
    ):
        raise ValueError("pull_time_ms must be a positive integer")
    bindings = tuple(imported_bindings)
    if any(
        not isinstance(row, ImportedReactiveProgramBindingV1) for row in bindings
    ):
        raise TypeError(
            "imported_bindings must contain ImportedReactiveProgramBindingV1 values"
        )
    binding_ids = [row.binding_id for row in bindings]
    if len(binding_ids) != len(set(binding_ids)):
        raise ValueError("imported binding IDs must be unique")
    if program_replay_factory is None:
        if observation_projector_factory is None and not bindings:
            # The default path is the exact per-case causal integration for all
            # three imported incumbents.  Its projector and stateful adapters
            # must both be fresh per replay, so it cannot be assembled through
            # the older one-projector-per-loadout generic factory.
            from .upper_kara_imported_incumbent_program_v1 import (
                build_imported_incumbent_program_replay_factory_v1,
            )

            resolved_replay_factory = (
                build_imported_incumbent_program_replay_factory_v1(
                    build_id,
                    bridge_path=bridge_path,
                    bridge_cwd=bridge_cwd,
                    runtime_binding_path=runtime_binding_path,
                )
            )
        elif callable(observation_projector_factory):
            resolved_replay_factory = _native_replay_factory_v1(
                bridge_path=bridge_path,
                bridge_cwd=bridge_cwd,
                observation_projector_factory=observation_projector_factory,
                imported_bindings=bindings,
            )
        else:
            raise TypeError(
                "observation_projector_factory must be callable when explicit "
                "imported_bindings are supplied"
            )
    elif callable(program_replay_factory):
        if observation_projector_factory is not None or bindings:
            raise ValueError(
                "custom program_replay_factory is mutually exclusive with "
                "observation_projector_factory and imported_bindings"
            )
        resolved_replay_factory = program_replay_factory
    else:
        raise TypeError("program_replay_factory must be callable or None")
    if isinstance(loadout_ids, (str, bytes)) or not isinstance(loadout_ids, Sequence):
        raise TypeError("loadout_ids must be a sequence")
    loadouts = tuple(loadout_ids)
    if set(loadouts) != set(BURST_LOADOUT_IDS_V1) or len(loadouts) != len(
        BURST_LOADOUT_IDS_V1
    ):
        raise ValueError("loadout_ids must contain each of the four burst loadouts once")
    if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or max_decisions <= 0:
        raise ValueError("max_decisions must be a positive integer")
    latest_configured_arrival_ms = max(
        row.first_wave_arrival_ms for row in (*train, *evaluation)
    )
    minimum_reference_idle_decisions = math.ceil(
        (pull_time_ms + latest_configured_arrival_ms)
        / REFERENCE_IDLE_WAIT_MS_V1
    )
    incumbents = tuple(
        imported_incumbent_programs
        if imported_incumbent_programs is not None
        else imported_incumbent_programs_v1(build_id)
    )
    if len(incumbents) != 3 or any(
        not isinstance(row, CausalActionProgramV1) for row in incumbents
    ):
        raise ValueError("exactly three imported incumbent programs are required")
    expected_incumbent_ids = baseline_policy_ids_for_build_v1(build_id)
    if (
        any(
            not isinstance(row.selector, ImportedReactiveSelectorV1)
            or row.origin is not ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT
            for row in incumbents
        )
        or tuple(row.selector.source_policy_id for row in incumbents)
        != expected_incumbent_ids
    ):
        raise ValueError(
            "imported incumbent programs must match the three native baseline policies"
        )

    training_rows: list[JSONMap] = []
    selectable: list[tuple[float, float, str, str, CausalActionProgramV1]] = []
    searched_selectable: list[
        tuple[float, float, str, str, CausalActionProgramV1]
    ] = []
    for loadout_id in loadouts:
        cases_by_master = {
            example.seed: case_builder(
                example.seed,
                build_id=build_id,
                loadout_id=loadout_id,
                pull_time_ms=pull_time_ms,
                first_wave_arrival_ms=example.first_wave_arrival_ms,
            )
            for example in train
        }
        simulator_by_master = {
            seed: derive_simulator_seed(
                seed,
                case.dynamic_load.request_sha256,
                namespace=PROTOCOL_ID,
            )
            for seed, case in cases_by_master.items()
        }
        case_by_simulator = {
            simulator_by_master[seed]: case for seed, case in cases_by_master.items()
        }
        if len(case_by_simulator) != len(train):
            raise ValueError("training derived simulator seeds collide")
        replay = resolved_replay_factory(loadout_id, case_by_simulator)
        programs = _programs_for_loadout_v1(
            loadout_id=loadout_id,
            train_examples=train,
            train_cases=tuple(cases_by_master[row.seed] for row in train),
            incumbent_programs=incumbents,
            searched_program_generator=searched_program_generator,
        )
        for program in programs:
            seed_rows: list[JSONMap] = []
            for example in train:
                case = cases_by_master[example.seed]
                simulator_seed = simulator_by_master[example.seed]
                try:
                    outcome = replay.replay(
                        simulator_seed,
                        program,
                        max_decisions=max_decisions,
                    )
                    lane = _outcome_lane_v1(
                        outcome,
                        case=case,
                        master_seed=example.seed,
                        simulator_seed=simulator_seed,
                    )
                except Exception as error:
                    lane = _failed_lane_v1(
                        master_seed=example.seed,
                        simulator_seed=simulator_seed,
                        error=error,
                    )
                lane["first_wave_arrival_ms"] = example.first_wave_arrival_ms
                seed_rows.append(lane)
            complete = all(_complete_result_row(row) for row in seed_rows)
            mean_damage = (
                mean(float(row["own_effective_damage"]) for row in seed_rows)
                if complete
                else None
            )
            mean_dps = (
                mean(float(row["own_effective_dps"]) for row in seed_rows)
                if complete
                else None
            )
            row = {
                "loadout_id": loadout_id,
                "program_id": program.program_id,
                "program_key": program.program_key(),
                "program_origin": program.origin.value,
                "status": "COMPLETE_TRAIN_OBJECTIVE" if complete else "INCOMPLETE_TRAIN_LANES",
                "objective": "MEAN_OWN_EFFECTIVE_DAMAGE",
                "mean_own_effective_damage": mean_damage,
                "mean_own_effective_dps": mean_dps,
                "seed_rows": seed_rows,
            }
            training_rows.append(row)
            if complete and mean_damage is not None and mean_dps is not None:
                selectable_row = (
                    mean_damage,
                    mean_dps,
                    loadout_id,
                    program.program_key(),
                    program,
                )
                selectable.append(selectable_row)
                if program.origin in (
                    ProgramOriginV1.SEARCHED,
                    ProgramOriginV1.SEARCHED_REACTIVE,
                ):
                    searched_selectable.append(selectable_row)

    # When search is requested, the evaluated candidate must be a searched
    # program.  Imported incumbents remain in the paired training table but
    # cannot silently become the thing later labelled as the candidate.
    candidate_selectable = (
        searched_selectable
        if searched_program_generator is not None
        else selectable
    )
    if not candidate_selectable:
        return {
            "schema": SCHEMA,
            "status": "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION",
            "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            "build_id": build_id,
            "train_examples": [row.to_dict() for row in train],
            "evaluation_examples": [row.to_dict() for row in evaluation],
            "training": {
                "objective": "MEAN_OWN_EFFECTIVE_DAMAGE",
                "rows": training_rows,
                "winner": None,
            },
            "evaluation": {"executed": False, "seed_rows": [], "summary": None},
            "contract": {
                "max_decisions_per_replay": max_decisions,
                "reference_idle_wait_ms": REFERENCE_IDLE_WAIT_MS_V1,
                "minimum_reference_idle_decisions_before_latest_arrival": (
                    minimum_reference_idle_decisions
                ),
                "configured_budget_exceeds_reference_idle_requirement": (
                    max_decisions > minimum_reference_idle_decisions
                ),
                "searched_program_generator_supplied": searched_program_generator is not None,
                "searched_origin_required_for_candidate": (
                    searched_program_generator is not None
                ),
                "three_imported_reactive_incumbents_scored_as_programs": True,
                "imported_runtime_binding_count": len(bindings),
                "custom_program_replay_factory_supplied": program_replay_factory is not None,
                "evaluation_cases_materialized_before_winner_freeze": False,
                "failure_scored_as_zero": False,
            },
        }

    # Deterministic tie-breaks are semantic and do not inspect held-out data.
    winner_tuple = sorted(
        candidate_selectable,
        key=lambda row: (-row[0], -row[1], row[2], row[3]),
    )[0]
    winner_damage, winner_dps, winner_loadout, _, winner_program = winner_tuple
    frozen_wire = deepcopy(winner_program.to_dict())
    frozen_program = causal_action_program_from_dict_v1(frozen_wire)
    frozen_key = frozen_program.program_key()
    if frozen_wire != winner_program.to_dict() or frozen_key != winner_program.program_key():
        raise RuntimeError("semantic program changed while freezing")

    # This is intentionally the first point at which held-out cases exist.
    evaluation_cases = {
        example.seed: case_builder(
            example.seed,
            build_id=build_id,
            loadout_id=winner_loadout,
            pull_time_ms=pull_time_ms,
            first_wave_arrival_ms=example.first_wave_arrival_ms,
        )
        for example in evaluation
    }
    eval_simulator_by_master = {
        seed: derive_simulator_seed(
            seed,
            case.dynamic_load.request_sha256,
            namespace=PROTOCOL_ID,
        )
        for seed, case in evaluation_cases.items()
    }
    if len(set(eval_simulator_by_master.values())) != len(evaluation):
        raise ValueError("evaluation derived simulator seeds collide")
    eval_case_by_simulator = {
        eval_simulator_by_master[seed]: case
        for seed, case in evaluation_cases.items()
    }
    eval_replay = resolved_replay_factory(winner_loadout, eval_case_by_simulator)
    expected_baselines = baseline_policy_ids_for_build_v1(build_id)
    evaluation_rows: list[JSONMap] = []
    for example in evaluation:
        case = evaluation_cases[example.seed]
        simulator_seed = eval_simulator_by_master[example.seed]
        executed = DynamicRolloutLoadV3.bind(
            case.request,
            simulator_seed,
            case.dynamic_load.config,
        )
        try:
            candidate_outcome = eval_replay.replay(
                simulator_seed,
                frozen_program,
                max_decisions=max_decisions,
            )
            candidate = _outcome_lane_v1(
                candidate_outcome,
                case=case,
                master_seed=example.seed,
                simulator_seed=simulator_seed,
            )
        except Exception as error:
            candidate = _failed_lane_v1(
                master_seed=example.seed,
                simulator_seed=simulator_seed,
                error=error,
            )
        candidate.update({
            "policy_id": CANDIDATE_POLICY_ID,
            "role": "CANDIDATE",
            "program_id": frozen_program.program_id,
            "program_key": frozen_key,
            "frozen_program_replayed_without_search": True,
        })
        try:
            baseline_payload = baseline_runner(
                example.seed,
                build_id=build_id,
                loadout_id=winner_loadout,
                pull_time_ms=pull_time_ms,
                first_wave_arrival_ms=example.first_wave_arrival_ms,
                bridge_path=bridge_path,
                bridge_cwd=bridge_cwd,
                runtime_binding_path=runtime_binding_path,
            )
            baselines, blockers = _normalize_baseline_rows_v1(
                baseline_payload,
                expected_policy_ids=expected_baselines,
                case=case,
                build_id=build_id,
                loadout_id=winner_loadout,
                example=example,
                simulator_seed=simulator_seed,
                executed_contract_sha256=executed.contract_sha256,
            )
        except Exception as error:
            baselines = [
                {
                    **_failed_lane_v1(
                        master_seed=example.seed,
                        simulator_seed=simulator_seed,
                        error=error,
                    ),
                    "policy_id": policy_id,
                    "role": "BASELINE",
                    "comparison_eligible": False,
                }
                for policy_id in expected_baselines
            ]
            blockers = ["BASELINE_RUNNER_FAILED"]
        four_way = bool(
            not blockers
            and _complete_result_row(candidate)
            and all(_complete_result_row(row) for row in baselines)
        )
        deltas = {
            row["policy_id"]: {
                "own_effective_damage": (
                    float(candidate["own_effective_damage"])
                    - float(row["own_effective_damage"])
                    if _complete_result_row(candidate) and _complete_result_row(row)
                    else None
                ),
                "own_effective_dps": (
                    float(candidate["own_effective_dps"])
                    - float(row["own_effective_dps"])
                    if _complete_result_row(candidate) and _complete_result_row(row)
                    else None
                ),
            }
            for row in baselines
        }
        evaluation_rows.append({
            **example.to_dict(),
            "simulator_seed": simulator_seed,
            "exact_request_sha256": case.dynamic_load.request_sha256,
            "dynamic_load_contract_sha256": executed.contract_sha256,
            "rows": [candidate, *baselines],
            "paired_candidate_minus_baselines": deltas,
            "comparison_blockers": blockers,
            "four_way_complete": four_way,
        })

    complete = all(row["four_way_complete"] for row in evaluation_rows)
    paired_summary: JSONMap = {}
    for policy_id in expected_baselines:
        values = [
            row["paired_candidate_minus_baselines"][policy_id]["own_effective_damage"]
            for row in evaluation_rows
        ]
        all_paired = all(value is not None for value in values)
        paired_summary[policy_id] = {
            "paired_seed_count": sum(value is not None for value in values),
            "all_evaluation_seeds_paired": all_paired,
            "mean_candidate_minus_baseline_own_effective_damage": (
                mean(float(value) for value in values if value is not None)
                if all_paired
                else None
            ),
        }
    policy_ids = (CANDIDATE_POLICY_ID, *expected_baselines)
    policy_means: JSONMap | None = None
    ranking: list[str] | None = None
    candidate_strictly_better: bool | None = None
    if complete:
        policy_means = {}
        for policy_id in policy_ids:
            rows = [
                next(
                    lane
                    for lane in seed_row["rows"]
                    if lane["policy_id"] == policy_id
                )
                for seed_row in evaluation_rows
            ]
            policy_means[policy_id] = {
                "mean_own_effective_damage": mean(
                    float(row["own_effective_damage"]) for row in rows
                ),
                "mean_own_effective_dps": mean(
                    float(row["own_effective_dps"]) for row in rows
                ),
                "mean_ttk_ms": mean(float(row["ttk_ms"]) for row in rows),
            }
        ranking = sorted(
            policy_ids,
            key=lambda policy_id: (
                -policy_means[policy_id]["mean_own_effective_damage"],
                -policy_means[policy_id]["mean_own_effective_dps"],
                policy_id,
            ),
        )
        candidate_damage = policy_means[CANDIDATE_POLICY_ID][
            "mean_own_effective_damage"
        ]
        candidate_strictly_better = all(
            candidate_damage
            > policy_means[policy_id]["mean_own_effective_damage"]
            for policy_id in expected_baselines
        )
    return {
        "schema": SCHEMA,
        "status": (
            "COMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY"
            if complete
            else "INCOMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY"
        ),
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "build_id": build_id,
        "train_examples": [row.to_dict() for row in train],
        "evaluation_examples": [row.to_dict() for row in evaluation],
        "training": {
            "objective": "MEAN_OWN_EFFECTIVE_DAMAGE",
            "rows": training_rows,
            "winner": {
                "loadout_id": winner_loadout,
                "program_id": frozen_program.program_id,
                "program_key": frozen_key,
                "program_origin": frozen_program.origin.value,
                "mean_own_effective_damage": winner_damage,
                "mean_own_effective_dps": winner_dps,
                "selected_using_evaluation_outcomes": False,
            },
            "frozen_program": frozen_wire,
            "search_metadata_embedded_in_frozen_program": False,
        },
        "evaluation": {
            "executed": True,
            "selected_loadout_id": winner_loadout,
            "selected_program_id": frozen_program.program_id,
            "search_or_generator_invocations_on_evaluation_examples": 0,
            "seed_rows": evaluation_rows,
            "summary": {
                "four_way_conclusion_complete": complete,
                "evaluation_seed_count": len(evaluation_rows),
                "complete_four_way_seed_count": sum(
                    row["four_way_complete"] for row in evaluation_rows
                ),
                "paired_candidate_minus_baselines": paired_summary,
                "policy_means": policy_means,
                "ranking_by_mean_own_effective_damage": ranking,
                "candidate_strictly_better_than_all_native_baselines": (
                    candidate_strictly_better
                ),
            },
        },
        "contract": {
            "max_decisions_per_replay": max_decisions,
            "reference_idle_wait_ms": REFERENCE_IDLE_WAIT_MS_V1,
            "minimum_reference_idle_decisions_before_latest_arrival": (
                minimum_reference_idle_decisions
            ),
            "configured_budget_exceeds_reference_idle_requirement": (
                max_decisions > minimum_reference_idle_decisions
            ),
            "searched_program_generator_supplied": searched_program_generator is not None,
            "searched_origin_required_for_candidate": (
                searched_program_generator is not None
            ),
            "three_imported_reactive_incumbents_scored_as_programs": True,
            "imported_runtime_binding_count": len(bindings),
            "custom_program_replay_factory_supplied": program_replay_factory is not None,
            "all_four_loadouts_scored_on_training_examples": True,
            "loadout_and_program_selected_only_on_complete_training_objective": True,
            "winner_frozen_before_evaluation_case_materialization": True,
            "same_build_loadout_arrival_request_environment_and_seed_verified": (
                program_replay_factory is None
                and all(not row["comparison_blockers"] for row in evaluation_rows)
            ),
            "continuous_two_wave_case_contract": True,
            "native_action_program_replay_factory_used": (
                program_replay_factory is None
            ),
            "failure_scored_as_zero": False,
            "failed_lane_metrics_are_null": True,
        },
        "claim_boundary": {
            "real_upper_kara_superiority_proven": False,
            "current_two_wave_case_is_synthetic_repeat": True,
        },
    }


__all__ = (
    "CANDIDATE_POLICY_ID",
    "DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1",
    "OBSERVATION_CONTRACT_ID_V1",
    "ObservationProjectorFactoryV1",
    "ProgramReplayFactoryV1",
    "SCHEMA",
    "ActionProgramReplayV1",
    "TwoWaveExampleV1",
    "imported_incumbent_programs_v1",
    "run_upper_kara_continuous_two_wave_train_eval_v1",
)
