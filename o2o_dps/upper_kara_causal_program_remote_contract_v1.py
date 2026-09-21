"""Frozen campaign and shard contract for continuous two-wave program search.

This is intentionally a campaign contract, not an adaptation of the legacy
independent exact-cell panel.  A training work item is one loadout and one
disjoint seed shard.  Held-out shards do not acquire a loadout until the
training reducer has frozen the winning ``(loadout, program)`` pair.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedFallbackOverlaySelectorV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .development_two_wave_build_panel_v1 import BUILD_IDS
from .development_two_wave_sequence_candidates_v1 import (
    DevelopmentTwoWaveFreshSeedProtocolV1,
    PAIRED_CANDIDATE_COUNT_V1,
)
from .sim_bridge import ActionRef, SimBridgeProtocolError
from .upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from .upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from .upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1,
    HP_THRESHOLD_GRID_V1,
    UpperKaraCatHpGuardedSparseRoutingV1Error,
    V5_PROTOTYPE_INDEXES_V1,
    validate_upper_kara_cat_hp_guarded_sparse_routing_inputs_v1,
)
from .upper_kara_two_wave_train_eval_v1 import (
    DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1,
    REFERENCE_IDLE_WAIT_MS_V1,
    TwoWaveExampleV1,
)


JSONMap = dict[str, Any]
CAMPAIGN_SCHEMA = "upper_kara_causal_program_remote_campaign/v1"
DEFAULT_SEED_SHARD_COUNT = 6
TRAIN_PROPOSAL_INDEX_PARITY = 0
TRAIN_SELECTION_INDEX_PARITY = 1
TERMINAL_COMPLETE = "COMPLETE"
TERMINAL_INVALID = "INVALID"
TERMINAL_FAILED = "FAILED"
TERMINAL_STATUSES = frozenset(
    {TERMINAL_COMPLETE, TERMINAL_INVALID, TERMINAL_FAILED}
)
_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FIXED_PARENT_QUEUE_GCD_BLOCK_SEARCH_KIND = (
    "FIXED_PARENT_CAT_BURST_QUEUE_GCD_BLOCK"
)
FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND = (
    "FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING"
)
HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND = (
    "HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7"
)
HETEROGENEOUS_TWO_WAVE_PAIR_V7 = ("multi_two", "single_long")
HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7 = 1_024
CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND = (
    "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8"
)
CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8 = (
    HETEROGENEOUS_TWO_WAVE_PAIR_V7
)
CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8 = 1_024
CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8 = 3
CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8 = 64
CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8 = 64
CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8 = 1_220_001
CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8 = 1_320_001
CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8 = 256
CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8 = (
    0,
    1_000,
    3_000,
    5_000,
    7_000,
    9_000,
)
CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8 = (
    DevelopmentTwoWaveFreshSeedProtocolV1(
        train_seed_start=CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8,
        evaluation_seed_start=(
            CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8
        ),
        seed_count=CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8,
        environment_arrival_nuisance_schedule_ms=(
            CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8
        ),
    )
)
HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7 = (
    "imported-source-program:cat.fury.profile1",
    "imported-source-program:contra260817.fury.source_candidate",
    "imported-source-program:contra.deployed.fury.raid_a",
    "wave-action-guide:offline_chronicle:pooled_partial_server_observations_v1",
)
_HP_PROTOTYPE_RECEIPT_FIELDS = frozenset(
    {
        "prototype_index",
        "program_ref",
        "program_id",
        "program_key",
        "program_origin",
        "proposal_guide_ids",
        "program",
    }
)


@dataclass(frozen=True)
class FixedParentQueueGcdBlockSearchV1:
    """Search one queue/GCD block coordinate over a frozen burst program."""

    parent_loadout_id: str
    parent_program: CausalActionProgramV1
    max_block_programs: int = 512
    kind: str = FIXED_PARENT_QUEUE_GCD_BLOCK_SEARCH_KIND

    def __post_init__(self) -> None:
        if self.kind != FIXED_PARENT_QUEUE_GCD_BLOCK_SEARCH_KIND:
            raise ValueError("fixed-parent search kind differs from v1")
        if self.parent_loadout_id not in BURST_LOADOUT_IDS_V1:
            raise ValueError("parent_loadout_id is not a canonical burst loadout")
        if not isinstance(self.parent_program, CausalActionProgramV1):
            raise TypeError("parent_program must be CausalActionProgramV1")
        if (
            self.parent_program.origin is not ProgramOriginV1.SEARCHED
            or not isinstance(
                self.parent_program.selector,
                ImportedFallbackOverlaySelectorV1,
            )
        ):
            raise ValueError(
                "parent_program must be a frozen searched burst overlay"
            )
        if (
            isinstance(self.max_block_programs, bool)
            or not isinstance(self.max_block_programs, int)
            or self.max_block_programs < 2
        ):
            raise ValueError("max_block_programs must be an integer of at least two")

    def to_dict(self) -> JSONMap:
        return {
            "kind": self.kind,
            "parent_loadout_id": self.parent_loadout_id,
            "parent_program": self.parent_program.to_dict(),
            "max_block_programs": self.max_block_programs,
        }


def _proposal_guide_ids_v1(
    program: CausalActionProgramV1,
) -> tuple[str, ...]:
    return tuple(
        source_ref.removeprefix("proposal-guide:")
        for source_ref in program.source_refs
        if source_ref.startswith("proposal-guide:")
    )


def _prototype_receipt_v1(
    prototype_index: int,
    program: CausalActionProgramV1,
) -> JSONMap:
    """Return the strict embedded v5 prototype identity receipt."""

    return {
        "prototype_index": prototype_index,
        "program_ref": program.program_id,
        "program_id": program.program_id,
        "program_key": program.program_key(),
        "program_origin": program.origin.value,
        "proposal_guide_ids": list(_proposal_guide_ids_v1(program)),
        "program": program.to_dict(),
    }


@dataclass(frozen=True)
class FixedParentCatHpGuardedSparseRoutingSearchV1:
    """Route four exact v5 block prototypes by current target HP."""

    parent_loadout_id: str
    parent_program: CausalActionProgramV1
    prototype_programs: tuple[CausalActionProgramV1, ...]
    max_programs: int = HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1
    hp_thresholds: tuple[int, ...] = tuple(HP_THRESHOLD_GRID_V1)
    kind: str = FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND

    def __post_init__(self) -> None:
        if self.kind != FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND:
            raise ValueError("HP-guarded search kind differs from v1")
        if self.parent_loadout_id not in BURST_LOADOUT_IDS_V1:
            raise ValueError("parent_loadout_id is not a canonical burst loadout")
        if not isinstance(self.parent_program, CausalActionProgramV1):
            raise TypeError("parent_program must be CausalActionProgramV1")
        if (
            not isinstance(self.prototype_programs, tuple)
            or len(self.prototype_programs) != len(V5_PROTOTYPE_INDEXES_V1)
            or any(
                not isinstance(program, CausalActionProgramV1)
                for program in self.prototype_programs
            )
        ):
            raise TypeError(
                "prototype_programs must contain exactly four "
                "CausalActionProgramV1 values"
            )
        if self.max_programs != HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1:
            raise ValueError(
                "HP-guarded max_programs must equal the exact emitted "
                f"count {HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1}"
            )
        if (
            not isinstance(self.hp_thresholds, tuple)
            or self.hp_thresholds != tuple(HP_THRESHOLD_GRID_V1)
        ):
            raise ValueError(
                "HP-guarded thresholds must equal the predeclared v1 grid"
            )
        # This validates prototype index/ID, selector structure, exact Cat
        # fallback semantics, and the parent relationship without consulting
        # any training or held-out outcome.
        try:
            validate_upper_kara_cat_hp_guarded_sparse_routing_inputs_v1(
                loadout_id=self.parent_loadout_id,
                parent_program=self.parent_program,
                prototype_programs=dict(
                    zip(
                        V5_PROTOTYPE_INDEXES_V1,
                        self.prototype_programs,
                        strict=True,
                    )
                ),
            )
        except UpperKaraCatHpGuardedSparseRoutingV1Error as error:
            raise ValueError(f"invalid HP-guarded prototype panel: {error}") from error

    @property
    def prototype_programs_by_index(
        self,
    ) -> Mapping[int, CausalActionProgramV1]:
        return dict(
            zip(
                V5_PROTOTYPE_INDEXES_V1,
                self.prototype_programs,
                strict=True,
            )
        )

    def to_dict(self) -> JSONMap:
        return {
            "kind": self.kind,
            "parent_loadout_id": self.parent_loadout_id,
            "parent_program": self.parent_program.to_dict(),
            "prototype_programs": [
                _prototype_receipt_v1(index, program)
                for index, program in zip(
                    V5_PROTOTYPE_INDEXES_V1,
                    self.prototype_programs,
                    strict=True,
                )
            ],
            "max_programs": self.max_programs,
            "hp_thresholds": list(self.hp_thresholds),
        }


def _strict_action_ref_v7(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if (
        any(
            isinstance(field, bool)
            or not isinstance(field, int)
            or field < 0
            for field in (*identities, value.tag)
        )
        or sum(field > 0 for field in identities) != 1
    ):
        raise ValueError(
            f"{label} must have exactly one positive identity and a nonnegative tag"
        )
    return value


def _nonempty_ids_v7(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not value
        or any(not isinstance(row, str) or not row.strip() for row in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be unique nonempty strings")
    return value


@dataclass(frozen=True)
class HeterogeneousTwoWaveSequenceSearchV7:
    """Search wave-specific finite prefixes on the actual heterogeneous pair."""

    long_cooldown_action: ActionRef
    resource_id: str
    wave_one_min_target_hp_pct: float
    wave_one_min_estimated_remaining_ms: int
    proposal_guide_ids: tuple[str, ...]
    fresh_seed_contract: DevelopmentTwoWaveFreshSeedProtocolV1
    candidate_count: int = PAIRED_CANDIDATE_COUNT_V1
    wave_pair: tuple[str, str] = HETEROGENEOUS_TWO_WAVE_PAIR_V7
    kind: str = HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND

    def __post_init__(self) -> None:
        if self.kind != HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND:
            raise ValueError("heterogeneous sequence search kind differs from v7")
        _strict_action_ref_v7(
            self.long_cooldown_action, "long_cooldown_action"
        )
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ValueError("resource_id must be nonempty text")
        if (
            isinstance(self.wave_one_min_target_hp_pct, bool)
            or not isinstance(self.wave_one_min_target_hp_pct, (int, float))
            or not math.isfinite(float(self.wave_one_min_target_hp_pct))
            or not 0 <= float(self.wave_one_min_target_hp_pct) <= 100
        ):
            raise ValueError("wave_one_min_target_hp_pct must be in [0, 100]")
        _positive_int(
            self.wave_one_min_estimated_remaining_ms,
            "wave_one_min_estimated_remaining_ms",
        )
        if self.candidate_count != PAIRED_CANDIDATE_COUNT_V1:
            raise ValueError(
                "heterogeneous sequence candidate_count must equal "
                f"{PAIRED_CANDIDATE_COUNT_V1}"
            )
        if self.wave_pair != HETEROGENEOUS_TWO_WAVE_PAIR_V7:
            raise ValueError(
                "heterogeneous wave_pair must be multi_two -> single_long"
            )
        _nonempty_ids_v7(self.proposal_guide_ids, "proposal_guide_ids")
        if (
            self.proposal_guide_ids
            != HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7
        ):
            raise ValueError(
                "proposal_guide_ids must equal the four resolved v7 guide names"
            )
        if not isinstance(
            self.fresh_seed_contract, DevelopmentTwoWaveFreshSeedProtocolV1
        ):
            raise TypeError("fresh_seed_contract has the wrong type")
        if self.fresh_seed_contract != DevelopmentTwoWaveFreshSeedProtocolV1():
            raise ValueError(
                "fresh_seed_contract must equal the predeclared v7 seed panel"
            )

    def to_dict(self) -> JSONMap:
        return {
            "kind": self.kind,
            "long_cooldown_action": self.long_cooldown_action.to_wire(),
            "resource_id": self.resource_id,
            "wave_one_min_target_hp_pct": float(
                self.wave_one_min_target_hp_pct
            ),
            "wave_one_min_estimated_remaining_ms": (
                self.wave_one_min_estimated_remaining_ms
            ),
            "candidate_count": self.candidate_count,
            "wave_pair": list(self.wave_pair),
            "proposal_guide_ids": list(self.proposal_guide_ids),
            "fresh_seed_contract": self.fresh_seed_contract.to_dict(),
        }


@dataclass(frozen=True)
class CatActionPlanResidualSequenceSearchV8:
    """Freeze the teacher and total residual budget, including exact Cat."""

    teacher_max_states: int = CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8
    teacher_plan_shard_size: int = (
        CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8
    )
    max_distilled_candidates_per_loadout: int = (
        CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8
    )
    fresh_seed_contract: DevelopmentTwoWaveFreshSeedProtocolV1 = (
        CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8
    )
    wave_pair: tuple[str, str] = CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8
    kind: str = CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND

    def __post_init__(self) -> None:
        if self.kind != CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND:
            raise ValueError("Cat action-plan residual search kind differs from v8")
        if self.teacher_max_states != CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8:
            raise ValueError(
                "teacher_max_states must equal the fixed v8 value "
                f"{CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8}"
            )
        if (
            self.teacher_plan_shard_size
            != CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8
        ):
            raise ValueError(
                "teacher_plan_shard_size must equal the fixed v8 value "
                f"{CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8}"
            )
        if (
            self.max_distilled_candidates_per_loadout
            != CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8
        ):
            raise ValueError(
                "max_distilled_candidates_per_loadout must equal the fixed "
                "v8 value "
                f"{CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8}"
            )
        if not isinstance(
            self.fresh_seed_contract, DevelopmentTwoWaveFreshSeedProtocolV1
        ):
            raise TypeError("fresh_seed_contract has the wrong type")
        if self.fresh_seed_contract != CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8:
            raise ValueError(
                "fresh_seed_contract must equal the predeclared v8 seed panel"
            )
        if self.wave_pair != CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8:
            raise ValueError(
                "Cat action-plan residual wave_pair must be multi_two -> "
                "single_long"
            )

    def to_dict(self) -> JSONMap:
        return {
            "kind": self.kind,
            "teacher_max_states": self.teacher_max_states,
            "teacher_plan_shard_size": self.teacher_plan_shard_size,
            "max_distilled_candidates_per_loadout": (
                self.max_distilled_candidates_per_loadout
            ),
            "fresh_seed_contract": self.fresh_seed_contract.to_dict(),
            "wave_pair": list(self.wave_pair),
        }


def _fresh_seed_contract_from_wire_v7(
    value: object,
) -> DevelopmentTwoWaveFreshSeedProtocolV1:
    if not isinstance(value, Mapping) or set(value) != {
        "train",
        "evaluation",
        "disjoint",
        "frozen_before_search",
        "environment_arrival_nuisance_schedule_ms",
        "arrival_nuisance_is_policy_input",
    }:
        raise ValueError("fresh_seed_contract fields differ from v7")
    train = value.get("train")
    evaluation = value.get("evaluation")
    range_fields = {"first", "last", "count"}
    if not isinstance(train, Mapping) or set(train) != range_fields:
        raise ValueError("fresh_seed_contract.train fields differ from v7")
    if not isinstance(evaluation, Mapping) or set(evaluation) != range_fields:
        raise ValueError("fresh_seed_contract.evaluation fields differ from v7")
    nuisance = value.get("environment_arrival_nuisance_schedule_ms")
    if not isinstance(nuisance, list):
        raise ValueError(
            "fresh_seed_contract environment arrival schedule must be a list"
        )
    contract = DevelopmentTwoWaveFreshSeedProtocolV1(
        train_seed_start=train.get("first"),
        evaluation_seed_start=evaluation.get("first"),
        seed_count=train.get("count"),
        environment_arrival_nuisance_schedule_ms=tuple(nuisance),
    )
    if evaluation.get("count") != contract.seed_count:
        raise ValueError("fresh train and evaluation counts differ")
    if contract.to_dict() != dict(value):
        raise ValueError("fresh_seed_contract is not in canonical form")
    return contract


def _heterogeneous_sequence_search_from_wire_v7(
    value: Mapping[str, Any],
) -> HeterogeneousTwoWaveSequenceSearchV7:
    expected_fields = {
        "kind",
        "long_cooldown_action",
        "resource_id",
        "wave_one_min_target_hp_pct",
        "wave_one_min_estimated_remaining_ms",
        "candidate_count",
        "wave_pair",
        "proposal_guide_ids",
        "fresh_seed_contract",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "heterogeneous sequence search_spec fields differ from v7"
        )
    raw_action = value.get("long_cooldown_action")
    try:
        action = ActionRef.from_wire(raw_action)
    except (TypeError, ValueError, SimBridgeProtocolError) as error:
        raise ValueError(f"invalid long_cooldown_action: {error}") from error
    raw_pair = value.get("wave_pair")
    raw_guides = value.get("proposal_guide_ids")
    if not isinstance(raw_pair, list):
        raise ValueError("heterogeneous wave_pair must be a list")
    if not isinstance(raw_guides, list):
        raise ValueError("heterogeneous proposal_guide_ids must be a list")
    spec = HeterogeneousTwoWaveSequenceSearchV7(
        long_cooldown_action=action,
        resource_id=value.get("resource_id"),
        wave_one_min_target_hp_pct=value.get(
            "wave_one_min_target_hp_pct"
        ),
        wave_one_min_estimated_remaining_ms=value.get(
            "wave_one_min_estimated_remaining_ms"
        ),
        candidate_count=value.get("candidate_count"),
        wave_pair=tuple(raw_pair),
        proposal_guide_ids=tuple(raw_guides),
        fresh_seed_contract=_fresh_seed_contract_from_wire_v7(
            value.get("fresh_seed_contract")
        ),
    )
    if spec.to_dict() != dict(value):
        raise ValueError(
            "heterogeneous sequence search_spec is not in canonical form"
        )
    return spec


def _cat_action_plan_residual_sequence_search_from_wire_v8(
    value: Mapping[str, Any],
) -> CatActionPlanResidualSequenceSearchV8:
    expected_fields = {
        "kind",
        "teacher_max_states",
        "teacher_plan_shard_size",
        "max_distilled_candidates_per_loadout",
        "fresh_seed_contract",
        "wave_pair",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "Cat action-plan residual search_spec fields differ from v8"
        )
    raw_pair = value.get("wave_pair")
    if not isinstance(raw_pair, list):
        raise ValueError("Cat action-plan residual wave_pair must be a list")
    spec = CatActionPlanResidualSequenceSearchV8(
        teacher_max_states=value.get("teacher_max_states"),
        teacher_plan_shard_size=value.get("teacher_plan_shard_size"),
        max_distilled_candidates_per_loadout=value.get(
            "max_distilled_candidates_per_loadout"
        ),
        fresh_seed_contract=_fresh_seed_contract_from_wire_v7(
            value.get("fresh_seed_contract")
        ),
        wave_pair=tuple(raw_pair),
    )
    if spec.to_dict() != dict(value):
        raise ValueError(
            "Cat action-plan residual search_spec is not in canonical form"
        )
    return spec


def _search_spec_from_wire_v1(
    value: object,
) -> (
    FixedParentQueueGcdBlockSearchV1
    | FixedParentCatHpGuardedSparseRoutingSearchV1
    | HeterogeneousTwoWaveSequenceSearchV7
    | CatActionPlanResidualSequenceSearchV8
    | None
):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("search_spec must be an object")
    kind = value.get("kind")
    if kind == FIXED_PARENT_QUEUE_GCD_BLOCK_SEARCH_KIND:
        if set(value) != {
            "kind",
            "parent_loadout_id",
            "parent_program",
            "max_block_programs",
        }:
            raise ValueError("search_spec fields differ from the v1 contract")
        return FixedParentQueueGcdBlockSearchV1(
            parent_loadout_id=value.get("parent_loadout_id"),
            parent_program=causal_action_program_from_dict_v1(
                value.get("parent_program")
            ),
            max_block_programs=value.get("max_block_programs"),
        )
    if kind == HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND:
        return _heterogeneous_sequence_search_from_wire_v7(value)
    if kind == CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND:
        return _cat_action_plan_residual_sequence_search_from_wire_v8(value)
    if kind != FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND:
        raise ValueError("search_spec kind is unsupported")
    expected_fields = {
        "kind",
        "parent_loadout_id",
        "parent_program",
        "prototype_programs",
        "max_programs",
        "hp_thresholds",
    }
    if set(value) != expected_fields:
        raise ValueError("HP-guarded search_spec fields differ from v1")
    raw_prototypes = value.get("prototype_programs")
    if not isinstance(raw_prototypes, list) or len(raw_prototypes) != len(
        V5_PROTOTYPE_INDEXES_V1
    ):
        raise ValueError(
            "HP-guarded prototype_programs must contain exactly four receipts"
        )
    programs: list[CausalActionProgramV1] = []
    for position, expected_index in enumerate(V5_PROTOTYPE_INDEXES_V1):
        raw_receipt = raw_prototypes[position]
        if not isinstance(raw_receipt, Mapping):
            raise ValueError(f"prototype receipt {position} must be an object")
        if set(raw_receipt) != _HP_PROTOTYPE_RECEIPT_FIELDS:
            raise ValueError(
                f"prototype receipt {position} fields differ from v1"
            )
        program = causal_action_program_from_dict_v1(raw_receipt.get("program"))
        expected_receipt = _prototype_receipt_v1(expected_index, program)
        if dict(raw_receipt) != expected_receipt:
            raise ValueError(
                f"prototype receipt {position} identity differs from its program"
            )
        programs.append(program)
    spec = FixedParentCatHpGuardedSparseRoutingSearchV1(
        parent_loadout_id=value.get("parent_loadout_id"),
        parent_program=causal_action_program_from_dict_v1(
            value.get("parent_program")
        ),
        prototype_programs=tuple(programs),
        max_programs=value.get("max_programs"),
        hp_thresholds=tuple(value.get("hp_thresholds", ())),
    )
    if spec.to_dict() != dict(value):
        raise ValueError("HP-guarded search_spec is not in canonical form")
    return spec


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _examples_from_wire_v1(value: object, label: str) -> tuple[TwoWaveExampleV1, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a nonempty list")
    rows: list[TwoWaveExampleV1] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {
            "seed",
            "first_wave_arrival_ms",
        }:
            raise ValueError(
                f"{label}[{index}] must contain exactly seed and "
                "first_wave_arrival_ms"
            )
        try:
            rows.append(
                TwoWaveExampleV1(
                    seed=raw["seed"],
                    first_wave_arrival_ms=raw["first_wave_arrival_ms"],
                )
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {label}[{index}]: {error}") from error
    seeds = [row.seed for row in rows]
    if len(seeds) != len(set(seeds)):
        raise ValueError(f"{label} seeds must be unique")
    return tuple(rows)


def generation_config_from_wire_v1(
    value: object,
) -> UpperKaraCausalProgramGenerationConfigV1:
    """Parse the exact public generator configuration without open kwargs."""

    if not isinstance(value, Mapping):
        raise ValueError("generation_config must be an object")
    defaults = UpperKaraCausalProgramGenerationConfigV1().to_dict()
    unknown = sorted(set(value) - set(defaults))
    missing = sorted(set(defaults) - set(value))
    if unknown or missing:
        raise ValueError(
            "generation_config fields differ from the v1 generator contract: "
            f"missing={missing}, unknown={unknown}"
        )
    kwargs = dict(value)
    for name in (
        "hp_thresholds",
        "precombat_relative_times_ms",
        "queue_rage_thresholds",
    ):
        raw = kwargs[name]
        if not isinstance(raw, list):
            raise ValueError(f"generation_config.{name} must be a list")
        kwargs[name] = tuple(raw)
    try:
        config = UpperKaraCausalProgramGenerationConfigV1(**kwargs)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid generation_config: {error}") from error
    if config.to_dict() != dict(value):
        raise ValueError("generation_config is not in canonical form")
    return config


@dataclass(frozen=True)
class ContinuousTwoWaveRemoteCampaignV1:
    campaign_id: str
    build_id: str
    train_examples: tuple[TwoWaveExampleV1, ...]
    evaluation_examples: tuple[TwoWaveExampleV1, ...]
    generation_config: UpperKaraCausalProgramGenerationConfigV1
    loadout_ids: tuple[str, ...] = BURST_LOADOUT_IDS_V1
    seed_shard_count: int = DEFAULT_SEED_SHARD_COUNT
    pull_time_ms: int = 3_000
    max_decisions: int = DEFAULT_ACTION_PROGRAM_MAX_DECISIONS_V1
    search_spec: (
        FixedParentQueueGcdBlockSearchV1
        | FixedParentCatHpGuardedSparseRoutingSearchV1
        | HeterogeneousTwoWaveSequenceSearchV7
        | CatActionPlanResidualSequenceSearchV8
        | None
    ) = None

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or not _CAMPAIGN_ID.fullmatch(
            self.campaign_id
        ):
            raise ValueError("campaign_id has unsupported characters")
        if self.build_id not in BUILD_IDS:
            raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
        if not self.train_examples or not self.evaluation_examples:
            raise ValueError("train and evaluation examples must be nonempty")
        if any(
            not isinstance(row, TwoWaveExampleV1)
            for row in (*self.train_examples, *self.evaluation_examples)
        ):
            raise TypeError("example panels must contain TwoWaveExampleV1 values")
        train_seeds = [row.seed for row in self.train_examples]
        eval_seeds = [row.seed for row in self.evaluation_examples]
        if len(train_seeds) != len(set(train_seeds)):
            raise ValueError("training seeds must be unique")
        if len(eval_seeds) != len(set(eval_seeds)):
            raise ValueError("evaluation seeds must be unique")
        overlap = sorted(set(train_seeds) & set(eval_seeds))
        if overlap:
            raise ValueError(f"training and evaluation seeds overlap: {overlap}")
        if not isinstance(
            self.generation_config, UpperKaraCausalProgramGenerationConfigV1
        ):
            raise TypeError("generation_config has the wrong type")
        if self.search_spec is None or isinstance(
            self.search_spec,
            (
                HeterogeneousTwoWaveSequenceSearchV7,
                CatActionPlanResidualSequenceSearchV8,
            ),
        ):
            if tuple(self.loadout_ids) != BURST_LOADOUT_IDS_V1:
                raise ValueError(
                    "loadout_ids must preserve the four canonical burst loadouts"
                )
        else:
            if not isinstance(
                self.search_spec,
                (
                    FixedParentQueueGcdBlockSearchV1,
                    FixedParentCatHpGuardedSparseRoutingSearchV1,
                    HeterogeneousTwoWaveSequenceSearchV7,
                    CatActionPlanResidualSequenceSearchV8,
                ),
            ):
                raise TypeError("search_spec has the wrong type")
            if tuple(self.loadout_ids) != (
                self.search_spec.parent_loadout_id,
            ):
                raise ValueError(
                    "fixed-parent search must use only its frozen parent loadout"
                )
        _positive_int(self.seed_shard_count, "seed_shard_count")
        if len(self.train_examples) < self.seed_shard_count:
            raise ValueError("training panel is smaller than seed_shard_count")
        if len(self.evaluation_examples) < self.seed_shard_count:
            raise ValueError("evaluation panel is smaller than seed_shard_count")
        _positive_int(self.pull_time_ms, "pull_time_ms")
        _positive_int(self.max_decisions, "max_decisions")
        minimum_idle_decisions = math.ceil(
            (
                self.pull_time_ms
                + max(
                    row.first_wave_arrival_ms
                    for row in (*self.train_examples, *self.evaluation_examples)
                )
            )
            / REFERENCE_IDLE_WAIT_MS_V1
        )
        if self.max_decisions <= minimum_idle_decisions:
            raise ValueError(
                "max_decisions must exceed the 250ms idle-decision budget "
                "through the latest configured arrival"
            )
        if isinstance(
            self.search_spec,
            (
                HeterogeneousTwoWaveSequenceSearchV7,
                CatActionPlanResidualSequenceSearchV8,
            ),
        ):
            fresh = self.search_spec.fresh_seed_contract
            version = (
                "v8"
                if isinstance(
                    self.search_spec,
                    CatActionPlanResidualSequenceSearchV8,
                )
                else "v7"
            )
            if tuple(train_seeds) != fresh.train_seeds:
                raise ValueError(
                    f"{version} training seeds differ from fresh_seed_contract"
                )
            if tuple(eval_seeds) != fresh.evaluation_seeds:
                raise ValueError(
                    f"{version} evaluation seeds differ from fresh_seed_contract"
                )
            schedule = fresh.environment_arrival_nuisance_schedule_ms
            expected_train_arrivals = tuple(
                schedule[index % len(schedule)]
                for index in range(fresh.seed_count)
            )
            expected_evaluation_arrivals = expected_train_arrivals
            if tuple(
                row.first_wave_arrival_ms for row in self.train_examples
            ) != expected_train_arrivals:
                raise ValueError(
                    f"{version} training arrival nuisance strata differ from "
                    "contract"
                )
            if tuple(
                row.first_wave_arrival_ms for row in self.evaluation_examples
            ) != expected_evaluation_arrivals:
                raise ValueError(
                    f"{version} evaluation arrival nuisance strata differ from "
                    "contract"
                )
            if self.seed_shard_count != fresh.seed_count:
                raise ValueError(
                    f"{version} requires one seed per shard in the 256-shard "
                    "campaign"
                )
            minimum_max_decisions = (
                CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8
                if version == "v8"
                else HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7
            )
            if self.max_decisions < minimum_max_decisions:
                raise ValueError(
                    f"{version} max_decisions must be at least "
                    f"{minimum_max_decisions}"
                )
            if isinstance(
                self.search_spec,
                CatActionPlanResidualSequenceSearchV8,
            ):
                proposal, selection = split_training_examples_for_v8_teacher_v8(
                    self
                )
                if len(proposal) != 128 or len(selection) != 128:
                    raise ValueError(
                        "v8 requires an exact 128/128 proposal-selection split"
                    )

    def to_dict(self) -> JSONMap:
        if isinstance(
            self.search_spec,
            CatActionPlanResidualSequenceSearchV8,
        ):
            proposal_examples, selection_examples = (
                split_training_examples_for_v8_teacher_v8(self)
            )
            split_contract = (
                "FIRST_WAVE_ARRIVAL_STRATIFIED_OCCURRENCE_PARITY_"
                "ALTERNATING_ODD_STRATUM_TIE_128_128"
            )
        else:
            proposal_examples, selection_examples = (
                split_training_examples_for_selection_v1(self)
            )
            split_contract = (
                "FIRST_WAVE_ARRIVAL_STRATIFIED_OCCURRENCE_PARITY_"
                "EVEN_PROPOSAL_ODD_SELECTION"
            )
        latest_arrival = max(
            row.first_wave_arrival_ms
            for row in (*self.train_examples, *self.evaluation_examples)
        )
        minimum_idle_decisions = math.ceil(
            (self.pull_time_ms + latest_arrival) / REFERENCE_IDLE_WAIT_MS_V1
        )
        wire = {
            "schema": CAMPAIGN_SCHEMA,
            "campaign_id": self.campaign_id,
            "build_id": self.build_id,
            "train_examples": [row.to_dict() for row in self.train_examples],
            "evaluation_examples": [
                row.to_dict() for row in self.evaluation_examples
            ],
            "generation_config": self.generation_config.to_dict(),
            "loadout_ids": list(self.loadout_ids),
            "seed_shard_count": self.seed_shard_count,
            "pull_time_ms": self.pull_time_ms,
            "max_decisions": self.max_decisions,
            "contract": {
                "continuous_two_wave_process_per_replay": True,
                "training_partition_unit": "LOADOUT_AND_SEED_SHARD",
                "training_candidate_proposal_examples": [
                    row.to_dict() for row in proposal_examples
                ],
                "training_selection_validation_examples": [
                    row.to_dict() for row in selection_examples
                ],
                "training_proposal_selection_split": (
                    split_contract
                ),
                "heldout_loadout_selected_from_frozen_training_winner": True,
                "legacy_independent_exact_cells": False,
                "failed_metric_is_null": True,
                "raw_offline_data_required": False,
                "max_decisions_per_replay": self.max_decisions,
                "reference_idle_wait_ms": REFERENCE_IDLE_WAIT_MS_V1,
                "latest_configured_arrival_ms": latest_arrival,
                "minimum_reference_idle_decisions_before_latest_arrival": (
                    minimum_idle_decisions
                ),
                "configured_budget_exceeds_reference_idle_requirement": (
                    self.max_decisions > minimum_idle_decisions
                ),
            },
        }
        if self.search_spec is not None:
            wire["search_spec"] = self.search_spec.to_dict()
            if isinstance(
                self.search_spec, HeterogeneousTwoWaveSequenceSearchV7
            ):
                search_contract = {
                    "search_family": self.search_spec.kind,
                    "development_scope": "DEVELOPMENT_ONLY",
                    "actual_heterogeneous_wave_pair": list(
                        self.search_spec.wave_pair
                    ),
                    "wave_specific_action_sequences": True,
                    "paired_zero_is_exact_cat_no_sequence": True,
                    "fixed_parent_program_is_paired_zero": False,
                    "fixed_parent_loadout_reused_for_every_candidate": False,
                    "four_canonical_burst_loadouts_searched": True,
                    "training_task_count": (
                        len(self.loadout_ids) * self.seed_shard_count
                    ),
                    "heldout_task_count": self.seed_shard_count,
                    "first_wave_arrival_visible_to_policy": False,
                    "arrival_is_environment_nuisance_only": True,
                    "future_or_terminal_fields_visible_to_policy": False,
                    "exact_candidate_count_per_loadout": (
                        self.search_spec.candidate_count
                    ),
                    "wave_one_failed_resource_guard_consumes_resource": False,
                    "wave_two_retries_same_resource_id": True,
                    "full_upper_kara_route_claim": False,
                }
            elif isinstance(
                self.search_spec,
                CatActionPlanResidualSequenceSearchV8,
            ):
                teacher_shards = assign_teacher_plan_shards_v8(self)
                search_contract = {
                    "search_family": self.search_spec.kind,
                    "development_scope": "DEVELOPMENT_ONLY",
                    "actual_heterogeneous_wave_pair": list(
                        self.search_spec.wave_pair
                    ),
                    "cat_relative_action_plan_teacher": True,
                    "variable_length_residual_sequences": True,
                    "paired_zero_is_exact_cat_no_sequence": True,
                    "four_canonical_burst_loadouts_searched": True,
                    "teacher_max_states_per_proposal_seed": (
                        self.search_spec.teacher_max_states
                    ),
                    "teacher_plan_shard_size_per_state": (
                        self.search_spec.teacher_plan_shard_size
                    ),
                    "max_distilled_candidates_per_loadout": (
                        self.search_spec.max_distilled_candidates_per_loadout
                    ),
                    "distilled_candidate_cap_includes_exact_cat_zero": True,
                    "teacher_task_count": len(teacher_shards),
                    "teacher_uses_proposal_cohort_only": True,
                    "selection_validation_examples_excluded_from_teacher": True,
                    "heldout_evaluation_examples_excluded_from_teacher": True,
                    "first_wave_arrival_visible_to_policy": False,
                    "arrival_is_environment_nuisance_only": True,
                    "future_or_terminal_fields_visible_to_policy": False,
                    "full_upper_kara_route_claim": False,
                }
            else:
                search_contract = {
                    "search_family": self.search_spec.kind,
                    "fixed_parent_program_is_paired_zero": True,
                    "fixed_parent_loadout_reused_for_every_candidate": True,
                }
            if isinstance(self.search_spec, FixedParentQueueGcdBlockSearchV1):
                search_contract[
                    "search_changes_queue_and_ordinary_gcd_block_only"
                ] = True
            elif isinstance(
                self.search_spec,
                FixedParentCatHpGuardedSparseRoutingSearchV1,
            ):
                search_contract.update(
                    {
                        "search_routes_exact_v5_prototypes_by_current_hp": True,
                        "first_wave_arrival_visible_to_policy": False,
                        "hp_thresholds_predeclared_before_campaign": True,
                        "exact_searched_program_count": (
                            HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1
                        ),
                    }
                )
            wire["contract"].update(search_contract)
        return wire


@dataclass(frozen=True)
class TrainingShardV1:
    loadout_id: str
    seed_shard_index: int
    examples: tuple[TwoWaveExampleV1, ...]

    @property
    def work_id(self) -> str:
        return f"{self.loadout_id}__seed-shard-{self.seed_shard_index:02d}"


@dataclass(frozen=True)
class EvaluationShardV1:
    seed_shard_index: int
    examples: tuple[TwoWaveExampleV1, ...]

    @property
    def work_id(self) -> str:
        return f"heldout__seed-shard-{self.seed_shard_index:02d}"


@dataclass(frozen=True)
class TeacherPlanShardV8:
    """One proposal-seed/loadout teacher task with a bounded plan panel."""

    loadout_id: str
    teacher_shard_index: int
    example: TwoWaveExampleV1
    teacher_max_states: int
    teacher_plan_shard_size: int

    def __post_init__(self) -> None:
        if self.loadout_id not in BURST_LOADOUT_IDS_V1:
            raise ValueError("teacher loadout_id is not canonical")
        if (
            isinstance(self.teacher_shard_index, bool)
            or not isinstance(self.teacher_shard_index, int)
            or self.teacher_shard_index < 0
        ):
            raise ValueError("teacher_shard_index must be a nonnegative integer")
        if not isinstance(self.example, TwoWaveExampleV1):
            raise TypeError("teacher example must be TwoWaveExampleV1")
        if self.teacher_max_states != CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8:
            raise ValueError("teacher shard max_states differs from v8")
        if (
            self.teacher_plan_shard_size
            != CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8
        ):
            raise ValueError("teacher shard plan cap differs from v8")

    @property
    def work_id(self) -> str:
        return (
            f"{self.loadout_id}__teacher-plan-shard-"
            f"{self.teacher_shard_index:03d}"
        )


def campaign_from_dict_v1(value: object) -> ContinuousTwoWaveRemoteCampaignV1:
    if not isinstance(value, Mapping):
        raise ValueError("campaign must be an object")
    allowed = {
        "schema",
        "campaign_id",
        "build_id",
        "train_examples",
        "evaluation_examples",
        "generation_config",
        "loadout_ids",
        "seed_shard_count",
        "pull_time_ms",
        "max_decisions",
        "search_spec",
        "contract",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"campaign has unsupported fields: {unknown}")
    if value.get("schema") != CAMPAIGN_SCHEMA:
        raise ValueError(f"campaign.schema must be {CAMPAIGN_SCHEMA!r}")
    contract = value.get("contract")
    try:
        campaign = ContinuousTwoWaveRemoteCampaignV1(
            campaign_id=value.get("campaign_id"),
            build_id=value.get("build_id"),
            train_examples=_examples_from_wire_v1(
                value.get("train_examples"), "train_examples"
            ),
            evaluation_examples=_examples_from_wire_v1(
                value.get("evaluation_examples"), "evaluation_examples"
            ),
            generation_config=generation_config_from_wire_v1(
                value.get("generation_config")
            ),
            loadout_ids=tuple(value.get("loadout_ids", ())),
            seed_shard_count=value.get("seed_shard_count"),
            pull_time_ms=value.get("pull_time_ms"),
            max_decisions=value.get("max_decisions"),
            search_spec=_search_spec_from_wire_v1(value.get("search_spec")),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid campaign: {error}") from error
    if contract is not None and contract != campaign.to_dict()["contract"]:
        raise ValueError("campaign.contract differs from the v1 contract")
    return campaign


def load_continuous_two_wave_remote_campaign_v1(
    path: str | Path,
) -> ContinuousTwoWaveRemoteCampaignV1:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read campaign: {error}") from error
    return campaign_from_dict_v1(value)


def _seed_shards_v1(
    examples: Sequence[TwoWaveExampleV1], shard_count: int
) -> tuple[tuple[TwoWaveExampleV1, ...], ...]:
    shards = tuple(tuple(examples[index::shard_count]) for index in range(shard_count))
    if any(not shard for shard in shards):
        raise AssertionError("seed shard unexpectedly empty")
    flattened = [row.seed for shard in shards for row in shard]
    expected = [row.seed for row in examples]
    if len(flattened) != len(set(flattened)) or sorted(flattened) != sorted(expected):
        raise AssertionError("seed shards are not disjoint and complete")
    return shards


def split_training_examples_for_selection_v1(
    campaign: ContinuousTwoWaveRemoteCampaignV1,
) -> tuple[tuple[TwoWaveExampleV1, ...], tuple[TwoWaveExampleV1, ...]]:
    """Return the predeclared proposal and selection-validation cohorts.

    V8 dispatches to its fixed balanced splitter because a 256-row panel over
    six nuisance strata contains four odd-sized strata.  Earlier campaigns
    retain the original even-proposal/odd-selection rule unchanged.

    Input order is part of the campaign identity.  Within each predeclared
    ``first_wave_arrival_ms`` stratum, occurrence parity assigns alternating
    rows to proposal and selection.  This is deterministic, does not inspect
    rewards or any future kill result, preserves arrival coverage in both
    cohorts, and leaves ``evaluation_examples`` untouched for final comparison.
    """

    if not isinstance(campaign, ContinuousTwoWaveRemoteCampaignV1):
        raise TypeError("campaign must be ContinuousTwoWaveRemoteCampaignV1")
    if isinstance(
        campaign.search_spec,
        CatActionPlanResidualSequenceSearchV8,
    ):
        return split_training_examples_for_v8_teacher_v8(campaign)
    occurrence_by_arrival: dict[int, int] = {}
    proposal_rows: list[TwoWaveExampleV1] = []
    selection_rows: list[TwoWaveExampleV1] = []
    for row in campaign.train_examples:
        occurrence = occurrence_by_arrival.get(row.first_wave_arrival_ms, 0)
        occurrence_by_arrival[row.first_wave_arrival_ms] = occurrence + 1
        destination = (
            proposal_rows
            if occurrence % 2 == TRAIN_PROPOSAL_INDEX_PARITY
            else selection_rows
        )
        destination.append(row)
    undersized = sorted(
        arrival for arrival, count in occurrence_by_arrival.items() if count < 2
    )
    if undersized:
        raise ValueError(
            "every first_wave_arrival_ms stratum needs at least two training "
            f"examples: {undersized}"
        )
    proposal = tuple(proposal_rows)
    selection = tuple(selection_rows)
    if {row.seed for row in proposal} & {row.seed for row in selection}:
        raise AssertionError("proposal and selection seed cohorts overlap")
    return proposal, selection


def split_training_examples_for_v8_teacher_v8(
    campaign: ContinuousTwoWaveRemoteCampaignV1,
) -> tuple[tuple[TwoWaveExampleV1, ...], tuple[TwoWaveExampleV1, ...]]:
    """Return the fixed balanced V8 proposal/selection cohorts.

    The 256-example cyclic nuisance panel has four odd-sized arrival strata.
    Within every stratum rows still alternate by input occurrence.  The
    declared stratum order alternates which cohort receives the odd extra row,
    producing an exact 128/128 split without changing the seed panel.
    """

    if not isinstance(campaign, ContinuousTwoWaveRemoteCampaignV1):
        raise TypeError("campaign must be ContinuousTwoWaveRemoteCampaignV1")
    spec = campaign.search_spec
    if not isinstance(spec, CatActionPlanResidualSequenceSearchV8):
        raise ValueError("the balanced teacher split requires a v8 search_spec")
    schedule = spec.fresh_seed_contract.environment_arrival_nuisance_schedule_ms
    stratum_index = {arrival: index for index, arrival in enumerate(schedule)}
    occurrence_by_arrival: dict[int, int] = {}
    proposal_rows: list[TwoWaveExampleV1] = []
    selection_rows: list[TwoWaveExampleV1] = []
    for row in campaign.train_examples:
        if row.first_wave_arrival_ms not in stratum_index:
            raise ValueError("training arrival is outside the fixed v8 schedule")
        occurrence = occurrence_by_arrival.get(row.first_wave_arrival_ms, 0)
        occurrence_by_arrival[row.first_wave_arrival_ms] = occurrence + 1
        proposal_parity = stratum_index[row.first_wave_arrival_ms] % 2
        destination = (
            proposal_rows
            if occurrence % 2 == proposal_parity
            else selection_rows
        )
        destination.append(row)
    if set(occurrence_by_arrival) != set(schedule):
        raise ValueError("v8 training examples do not cover every arrival stratum")
    proposal = tuple(proposal_rows)
    selection = tuple(selection_rows)
    proposal_seeds = {row.seed for row in proposal}
    selection_seeds = {row.seed for row in selection}
    expected_seeds = {row.seed for row in campaign.train_examples}
    if proposal_seeds & selection_seeds:
        raise AssertionError("v8 proposal and selection cohorts overlap")
    if proposal_seeds | selection_seeds != expected_seeds:
        raise AssertionError("v8 proposal and selection cohorts are incomplete")
    for arrival in schedule:
        if not any(row.first_wave_arrival_ms == arrival for row in proposal):
            raise AssertionError("v8 proposal cohort lost an arrival stratum")
        if not any(row.first_wave_arrival_ms == arrival for row in selection):
            raise AssertionError("v8 selection cohort lost an arrival stratum")
    return proposal, selection


def assign_training_shards_v1(
    campaign: ContinuousTwoWaveRemoteCampaignV1,
) -> tuple[TrainingShardV1, ...]:
    shards = _seed_shards_v1(campaign.train_examples, campaign.seed_shard_count)
    return tuple(
        TrainingShardV1(loadout_id, shard_index, examples)
        for loadout_id in campaign.loadout_ids
        for shard_index, examples in enumerate(shards)
    )


def assign_evaluation_shards_v1(
    campaign: ContinuousTwoWaveRemoteCampaignV1,
) -> tuple[EvaluationShardV1, ...]:
    return tuple(
        EvaluationShardV1(index, examples)
        for index, examples in enumerate(
            _seed_shards_v1(
                campaign.evaluation_examples, campaign.seed_shard_count
            )
        )
    )


def assign_teacher_plan_shards_v8(
    campaign: ContinuousTwoWaveRemoteCampaignV1,
) -> tuple[TeacherPlanShardV8, ...]:
    """Assign one deterministic V8 teacher task per proposal seed/loadout.

    ``teacher_plan_shard_size`` is the full per-state ActionPlan cap inside
    each task.  It does not create another task axis.  Selection-validation
    and held-out evaluation examples never enter these assignments.
    """

    if not isinstance(campaign, ContinuousTwoWaveRemoteCampaignV1):
        raise TypeError("campaign must be ContinuousTwoWaveRemoteCampaignV1")
    spec = campaign.search_spec
    if not isinstance(spec, CatActionPlanResidualSequenceSearchV8):
        raise ValueError("teacher plan shards require a v8 search_spec")
    proposal, selection = split_training_examples_for_v8_teacher_v8(campaign)
    shards = tuple(
        TeacherPlanShardV8(
            loadout_id=loadout_id,
            teacher_shard_index=index,
            example=example,
            teacher_max_states=spec.teacher_max_states,
            teacher_plan_shard_size=spec.teacher_plan_shard_size,
        )
        for loadout_id in campaign.loadout_ids
        for index, example in enumerate(proposal)
    )
    proposal_seeds = tuple(row.seed for row in proposal)
    excluded_seeds = {
        row.seed for row in (*selection, *campaign.evaluation_examples)
    }
    if set(proposal_seeds) & excluded_seeds:
        raise AssertionError("teacher and excluded seed cohorts overlap")
    for loadout_id in campaign.loadout_ids:
        assigned = tuple(
            shard.example.seed
            for shard in shards
            if shard.loadout_id == loadout_id
        )
        if assigned != proposal_seeds or len(set(assigned)) != len(assigned):
            raise AssertionError(
                "teacher plan shards are not disjoint and complete for "
                f"{loadout_id}"
            )
    if len(shards) != len(campaign.loadout_ids) * len(proposal):
        raise AssertionError("teacher plan shard count is incomplete")
    return shards


def terminal_status_for_lanes_v1(lanes: Sequence[Mapping[str, Any]]) -> str:
    statuses = [row.get("status") for row in lanes]
    if any(status == TERMINAL_FAILED for status in statuses):
        return TERMINAL_FAILED
    if any(status == TERMINAL_INVALID for status in statuses):
        return TERMINAL_INVALID
    if statuses and all(status == TERMINAL_COMPLETE for status in statuses):
        return TERMINAL_COMPLETE
    return TERMINAL_FAILED


__all__ = (
    "CAT_ACTION_PLAN_FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V8",
    "CAT_ACTION_PLAN_FRESH_EVALUATION_SEED_START_V8",
    "CAT_ACTION_PLAN_FRESH_SEED_CONTRACT_V8",
    "CAT_ACTION_PLAN_FRESH_SEED_COUNT_V8",
    "CAT_ACTION_PLAN_FRESH_TRAIN_SEED_START_V8",
    "CAT_ACTION_PLAN_MAX_DISTILLED_CANDIDATES_PER_LOADOUT_V8",
    "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_MIN_MAX_DECISIONS_V8",
    "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8_KIND",
    "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_WAVE_PAIR_V8",
    "CAT_ACTION_PLAN_TEACHER_MAX_STATES_V8",
    "CAT_ACTION_PLAN_TEACHER_PLAN_SHARD_SIZE_V8",
    "CAMPAIGN_SCHEMA",
    "DEFAULT_SEED_SHARD_COUNT",
    "TRAIN_PROPOSAL_INDEX_PARITY",
    "TRAIN_SELECTION_INDEX_PARITY",
    "EvaluationShardV1",
    "TERMINAL_COMPLETE",
    "TERMINAL_FAILED",
    "TERMINAL_INVALID",
    "TERMINAL_STATUSES",
    "TrainingShardV1",
    "TeacherPlanShardV8",
    "ContinuousTwoWaveRemoteCampaignV1",
    "FIXED_PARENT_QUEUE_GCD_BLOCK_SEARCH_KIND",
    "FIXED_PARENT_CAT_HP_GUARDED_SPARSE_ROUTING_KIND",
    "HETEROGENEOUS_TWO_WAVE_MIN_MAX_DECISIONS_V7",
    "HETEROGENEOUS_TWO_WAVE_PAIR_V7",
    "HETEROGENEOUS_TWO_WAVE_PROPOSAL_GUIDE_IDS_V7",
    "HETEROGENEOUS_TWO_WAVE_SEQUENCE_V7_KIND",
    "FixedParentCatHpGuardedSparseRoutingSearchV1",
    "FixedParentQueueGcdBlockSearchV1",
    "HeterogeneousTwoWaveSequenceSearchV7",
    "CatActionPlanResidualSequenceSearchV8",
    "assign_evaluation_shards_v1",
    "assign_training_shards_v1",
    "assign_teacher_plan_shards_v8",
    "campaign_from_dict_v1",
    "generation_config_from_wire_v1",
    "load_continuous_two_wave_remote_campaign_v1",
    "split_training_examples_for_selection_v1",
    "split_training_examples_for_v8_teacher_v8",
    "terminal_status_for_lanes_v1",
)
