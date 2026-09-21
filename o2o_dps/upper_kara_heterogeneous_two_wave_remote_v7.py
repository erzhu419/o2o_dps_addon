"""Remote v7 assembly for the heterogeneous two-wave sequence search.

This module is deliberately small: it binds the frozen ``multi_two ->
single_long`` environment to one exact build and one canonical burst inventory,
and it reconstructs the fixed 37-arm search family without consulting replay
outcomes or arrival strata.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import CausalActionProgramV1
from .contra_turtle_burst_loadout_v1 import (
    build_contra_turtle_burst_loadouts_v1,
)
from .development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
    wrap_development_wave_case_with_burst_precombat_v1,
)
from .development_two_wave_build_panel_v1 import (
    BUILD_IDS,
    _build_player_and_names,
)
from .development_two_wave_segment_policy_v1 import (
    DevelopmentTwoWaveSegmentPolicyV1,
    SegmentWaveV1,
    build_two_wave_segment_runtime_v1,
)
from .development_two_wave_sequence_candidates_v1 import (
    DevelopmentTwoWaveSequenceCandidateSetV1,
    PAIRED_CANDIDATE_COUNT_V1,
    build_development_two_wave_sequence_candidate_set_v1,
)
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import AvailableAction
from .upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from .upper_kara_causal_program_remote_contract_v1 import (
    HETEROGENEOUS_TWO_WAVE_PAIR_V7,
    HeterogeneousTwoWaveSequenceSearchV7,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    UNSEEN_UNTIL_ARRIVAL,
    build_upper_kara_heterogeneous_two_wave_case_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_heterogeneous_two_wave_remote/v7"
WAVES_V7 = (
    SegmentWaveV1(HETEROGENEOUS_TWO_WAVE_PAIR_V7[0], (0, 1)),
    SegmentWaveV1(HETEROGENEOUS_TWO_WAVE_PAIR_V7[1], (2,)),
)


def _selected_loadout_v7(
    request: Mapping[str, Any], loadout_id: str
) -> Any:
    if loadout_id not in BURST_LOADOUT_IDS_V1:
        raise ValueError("loadout_id is not a canonical burst loadout")
    rows = {
        row.loadout_id: row
        for row in build_contra_turtle_burst_loadouts_v1(request)
    }
    try:
        return rows[loadout_id]
    except KeyError as error:
        raise ValueError(f"unknown Turtle burst loadout {loadout_id!r}") from error


def _bind_exact_build_v7(case: Any, build_id: str) -> Any:
    """Apply the same controlled equipment substitution as the build panel."""

    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    request = deepcopy(case.request)
    source_player, equipped_names = _build_player_and_names(build_id)
    player = request["raid"]["parties"][0]["players"][0]
    if source_player is not None:
        player["equipment"] = deepcopy(source_player["equipment"])
    load = DynamicRolloutLoadV3.bind(request, case.dynamic_load.seed, case.dynamic_load.config)
    evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=load.request_sha256,
    )
    contexts = {
        index: replace(
            context,
            equipped_item_names=(
                equipped_names
                if equipped_names is not None
                else context.equipped_item_names
            ),
            equipment_evidence=evidence,
        )
        for index, context in case.target_contexts.items()
    }
    spec = deepcopy(case.case_spec)
    spec.update(
        {
            "build_id": build_id,
            "build_ref": (
                "configs/wowsims/fury_warrior_live.json"
                if build_id == BUILD_IDS[0]
                else "configs/wowsims/fury_warrior_clean_dual.json"
            ),
            "build_role": "CONTROLLED_BUILD_PROBE_NOT_HISTORICAL_SOURCE_PLAYER",
            "equipment_items": deepcopy(player["equipment"]["items"]),
            "request_sha256": load.request_sha256,
            "dynamic_load_contract_sha256": load.contract_sha256,
        }
    )
    return replace(
        case,
        case_spec=spec,
        request=request,
        dynamic_load=load,
        target_contexts=contexts,
    )


def _shift_target_introductions_v7(
    case: DevelopmentPrecombatWaveCaseV1,
) -> DevelopmentPrecombatWaveCaseV1:
    """Move control-plane introduction times onto the precombat sim clock."""

    spec = deepcopy(case.case_spec)
    policy = spec.get("policy_observation")
    registry = (
        policy.get("target_introduction_registry_control_plane_only")
        if isinstance(policy, Mapping)
        else None
    )
    targets = registry.get("targets") if isinstance(registry, Mapping) else None
    if not isinstance(targets, list) or not targets:
        raise ValueError("heterogeneous case lacks target introduction rows")
    for row in targets:
        if not isinstance(row, dict) or type(row.get("introduced_at_ms")) is not int:
            raise ValueError("heterogeneous target introduction row is invalid")
        row["introduced_at_ms"] += case.timeline.pull_time_ms
    continuous = spec.get("continuous_route")
    if isinstance(continuous, dict) and type(
        continuous.get("wave_2_start_ms")
    ) is int:
        continuous["wave_2_start_ms"] += case.timeline.pull_time_ms
        continuous["time_origin"] = "PRECOMBAT_WINDOW_START"
    environment = spec.get("environment_registry")
    if isinstance(environment, dict):
        environment["time_origin"] = "PRECOMBAT_WINDOW_START"
        arrival = environment.get("first_wave_arrival")
        if isinstance(arrival, dict) and type(arrival.get("arrival_ms")) is int:
            arrival["arrival_ms"] += case.timeline.pull_time_ms
        waves = environment.get("waves")
        if isinstance(waves, list):
            for wave in waves:
                if not isinstance(wave, dict):
                    continue
                if type(wave.get("simulator_start_ms")) is int:
                    wave["simulator_start_ms"] += case.timeline.pull_time_ms
                wave_targets = wave.get("targets")
                if not isinstance(wave_targets, list):
                    continue
                for target in wave_targets:
                    if isinstance(target, dict) and type(
                        target.get("policy_introduced_at_ms")
                    ) is int:
                        target["policy_introduced_at_ms"] += (
                            case.timeline.pull_time_ms
                        )
    spec["request_sha256"] = case.dynamic_load.request_sha256
    spec["dynamic_load_contract_sha256"] = case.dynamic_load.contract_sha256
    return replace(case, case_spec=spec)


def build_upper_kara_heterogeneous_two_wave_burst_case_v7(
    seed: int,
    *,
    build_id: str,
    loadout_id: str,
    pull_time_ms: int = 3_000,
    first_wave_arrival_ms: int = 0,
) -> DevelopmentPrecombatWaveCaseV1:
    """Bind build, inventory and precombat to one continuous native case."""

    built = build_upper_kara_heterogeneous_two_wave_case_v1(
        seed,
        first_wave_arrival_ms=first_wave_arrival_ms,
        first_wave_visibility=UNSEEN_UNTIL_ARRIVAL,
    )
    base = _bind_exact_build_v7(built.require_case(), build_id)
    loadout = _selected_loadout_v7(base.request, loadout_id)
    wrapped = wrap_development_wave_case_with_burst_precombat_v1(
        base,
        self_actions=loadout.precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=loadout.player_consumes,
    )
    wrapped = _shift_target_introductions_v7(wrapped)
    spec = deepcopy(wrapped.case_spec)
    spec["schema"] = SCHEMA
    spec["parent_heterogeneous_schema"] = base.case_spec.get("schema")
    spec["burst_loadout"] = loadout.to_dict()
    spec["remote_v7"] = {
        "wave_pair": list(HETEROGENEOUS_TWO_WAVE_PAIR_V7),
        "single_native_environment": True,
        "state_carried_across_waves": True,
        "arrival_nuisance_visible_to_policy": False,
        "environment_registry_visible_to_policy": False,
    }
    return replace(wrapped, case_spec=spec)


def _identity_only_cat_session_v7():
    def unavailable(*_: Any, **__: Any):
        raise AssertionError("identity-only Cat resolver must never be opened")

    return unavailable


@dataclass(frozen=True)
class HeterogeneousTwoWaveSequenceGenerationResultV7:
    guide_ids: tuple[str, ...]
    candidate_set: DevelopmentTwoWaveSequenceCandidateSetV1


def build_heterogeneous_two_wave_candidate_set_v7(
    build_id: str,
    search_spec: HeterogeneousTwoWaveSequenceSearchV7,
) -> DevelopmentTwoWaveSequenceCandidateSetV1:
    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    if not isinstance(search_spec, HeterogeneousTwoWaveSequenceSearchV7):
        raise TypeError("search_spec must be HeterogeneousTwoWaveSequenceSearchV7")
    return build_development_two_wave_sequence_candidate_set_v1(
        exact_build_id=build_id,
        waves=WAVES_V7,
        long_cooldown_action=search_spec.long_cooldown_action,
        resource_id=search_spec.resource_id,
        proposal_guide_ids=search_spec.proposal_guide_ids,
        wave_one_min_target_hp_pct=search_spec.wave_one_min_target_hp_pct,
        wave_one_min_estimated_remaining_ms=(
            search_spec.wave_one_min_estimated_remaining_ms
        ),
        seed_protocol=search_spec.fresh_seed_contract,
    )


def policies_by_program_id_v7(
    candidate_set: DevelopmentTwoWaveSequenceCandidateSetV1,
) -> dict[str, DevelopmentTwoWaveSegmentPolicyV1]:
    policies = {
        row.policy.policy_id: row.policy
        for row in candidate_set.candidates
        if row.policy is not None
    }
    if len(policies) != PAIRED_CANDIDATE_COUNT_V1 - 1:
        raise AssertionError("v7 policy registry does not contain 36 searched arms")
    return policies


def searched_programs_from_candidate_set_v7(
    loadout_id: str,
    candidate_set: DevelopmentTwoWaveSequenceCandidateSetV1,
) -> tuple[CausalActionProgramV1, ...]:
    if loadout_id not in BURST_LOADOUT_IDS_V1:
        raise ValueError("loadout_id is not a canonical burst loadout")
    programs: list[CausalActionProgramV1] = [
        cat_zero_residual_program_v1(loadout_id)
    ]
    for row in candidate_set.candidates[1:]:
        if row.policy is None:
            raise AssertionError("searched v7 candidate lacks a frozen policy")
        program, _ = build_two_wave_segment_runtime_v1(
            row.policy,
            cat_resolver_factory=_identity_only_cat_session_v7,
        )
        programs.append(program)
    if len(programs) != PAIRED_CANDIDATE_COUNT_V1:
        raise AssertionError("v7 searched family does not contain 37 arms")
    if len({row.program_id for row in programs}) != len(programs):
        raise AssertionError("v7 searched program IDs repeat")
    if len({row.program_key() for row in programs}) != len(programs):
        raise AssertionError("v7 searched program semantics repeat")
    return tuple(programs)


class HeterogeneousTwoWaveSequenceProgramGeneratorV7:
    """Generator-shaped deterministic adapter used by the remote workers."""

    def __init__(
        self,
        *,
        build_id: str,
        search_spec: HeterogeneousTwoWaveSequenceSearchV7,
    ) -> None:
        self.candidate_set = build_heterogeneous_two_wave_candidate_set_v7(
            build_id, search_spec
        )
        self.policies_by_program_id = policies_by_program_id_v7(
            self.candidate_set
        )
        self.results_by_loadout: dict[
            str, HeterogeneousTwoWaveSequenceGenerationResultV7
        ] = {}

    def __call__(
        self,
        *,
        loadout_id: str,
        train_examples: Sequence[Any],
        train_cases: Sequence[Any],
    ) -> tuple[CausalActionProgramV1, ...]:
        # Only presence/alignment is checked.  Seed, arrival, HP and future
        # schedules never enter the fixed candidate construction.
        if not train_examples or len(train_examples) != len(train_cases):
            raise ValueError("v7 proposal examples and cases must align")
        programs = searched_programs_from_candidate_set_v7(
            loadout_id, self.candidate_set
        )
        self.results_by_loadout[loadout_id] = (
            HeterogeneousTwoWaveSequenceGenerationResultV7(
                guide_ids=self.candidate_set.proposal_guide_ids,
                candidate_set=self.candidate_set,
            )
        )
        return programs


__all__ = (
    "HeterogeneousTwoWaveSequenceGenerationResultV7",
    "HeterogeneousTwoWaveSequenceProgramGeneratorV7",
    "SCHEMA",
    "WAVES_V7",
    "build_heterogeneous_two_wave_candidate_set_v7",
    "build_upper_kara_heterogeneous_two_wave_burst_case_v7",
    "policies_by_program_id_v7",
    "searched_programs_from_candidate_set_v7",
)
