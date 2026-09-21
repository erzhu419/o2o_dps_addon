"""Frozen-parent contract for the V9 Cat action-plan append search.

V9 is a continuation of one already selected V8 policy.  It does not reopen
the build or loadout search: every proposed policy appends one step to the
same frozen parent and every score is paired against that parent on the same
simulator seed.  The proposal, selection, and held-out cohorts are declared
before any V9 outcome is observed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .development_two_wave_cat_residual_sequence_v1 import (
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from .development_two_wave_sequence_candidates_v1 import (
    DevelopmentTwoWaveFreshSeedProtocolV1,
)
from .upper_kara_cat_action_plan_distiller_v8 import (
    load_upper_kara_cat_action_plan_distillation_v8,
)
from .upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_append_contract/v9"
PARENT_REF_SCHEMA = "upper_kara_cat_action_plan_append_parent_ref/v9"
MANIFEST_SCHEMA = "upper_kara_cat_action_plan_append_manifest/v9"
SEARCH_KIND = "CAT_ACTION_PLAN_APPEND_V9"
WAVE_PAIR = ("multi_two", "single_long")
SOURCE_SEARCH_KIND = "CAT_ACTION_PLAN_RESIDUAL_SEQUENCE_V8"

FRESH_TRAIN_SEED_START = 1_420_001
FRESH_EVALUATION_SEED_START = 1_520_001
FRESH_SEED_COUNT = 256
FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS = (0, 1_000, 3_000, 5_000, 7_000, 9_000)
FRESH_SEED_CONTRACT = DevelopmentTwoWaveFreshSeedProtocolV1(
    train_seed_start=FRESH_TRAIN_SEED_START,
    evaluation_seed_start=FRESH_EVALUATION_SEED_START,
    seed_count=FRESH_SEED_COUNT,
    environment_arrival_nuisance_schedule_ms=(
        FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS
    ),
)
PROPOSAL_EXAMPLE_COUNT = 128
SELECTION_EXAMPLE_COUNT = 128
HELDOUT_EXAMPLE_COUNT = 256
TEACHER_MAX_STATES = 3
TEACHER_PLAN_SHARD_SIZE = 64
MAX_APPEND_CANDIDATES = 63
SEED_SHARD_COUNT = 256

_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_PROGRAM_RECEIPT_FIELDS = frozenset(
    {
        "program_ref",
        "program_id",
        "program_key",
        "program_origin",
        "proposal_guide_ids",
        "program",
        "paired_parent_ref",
    }
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _mapping(value: object, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact_mapping(
    value: object, fields: set[str] | frozenset[str], label: str
) -> JSONMap:
    row = _mapping(value, label)
    if set(row) != set(fields):
        raise ValueError(f"{label} fields differ from the V9 contract")
    return row


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = [_text(row, f"{label} row") for row in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique values")
    return result


@dataclass(frozen=True)
class FrozenV8ParentRefV9:
    """Portable identity of the single V8 winner that V9 may extend."""

    source_campaign_id: str
    source_freeze_ref: str
    build_id: str
    loadout_id: str
    program_ref: str
    program_id: str
    program_key: str
    policy_bundle_ref: str
    schema: str = PARENT_REF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PARENT_REF_SCHEMA:
            raise ValueError("parent reference schema differs from V9")
        for field in (
            "source_campaign_id",
            "source_freeze_ref",
            "build_id",
            "loadout_id",
            "program_ref",
            "program_id",
            "program_key",
            "policy_bundle_ref",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if self.program_ref != self.program_id:
            raise ValueError("V8 parent program_ref and program_id must match")

    def to_dict(self) -> JSONMap:
        return {
            "schema": self.schema,
            "source_campaign_id": self.source_campaign_id,
            "source_freeze_ref": self.source_freeze_ref,
            "build_id": self.build_id,
            "loadout_id": self.loadout_id,
            "program_ref": self.program_ref,
            "program_id": self.program_id,
            "program_key": self.program_key,
            "policy_bundle_ref": self.policy_bundle_ref,
        }


def frozen_v8_parent_ref_from_dict_v9(value: object) -> FrozenV8ParentRefV9:
    fields = {
        "schema",
        "source_campaign_id",
        "source_freeze_ref",
        "build_id",
        "loadout_id",
        "program_ref",
        "program_id",
        "program_key",
        "policy_bundle_ref",
    }
    return FrozenV8ParentRefV9(
        **_exact_mapping(value, fields, "V8 parent reference")
    )


def freeze_v8_parent_ref_v9(
    frozen_v8: Mapping[str, Any],
    *,
    source_freeze_ref: str,
    policy_bundle_ref: str,
) -> FrozenV8ParentRefV9:
    """Extract and verify the selected V8 identity without reading V9 data."""

    source = _mapping(frozen_v8, "V8 frozen result")
    winner = _mapping(source.get("winner"), "V8 frozen winner")
    if source.get("terminal_status") != "COMPLETE":
        raise ValueError("V8 frozen result is not complete")
    source_campaign_id = _text(
        source.get("campaign_id"), "V8 frozen campaign_id"
    )
    build_id = _text(source.get("build_id"), "V8 frozen build_id")
    campaign_contract = _mapping(
        source.get("campaign_contract"), "V8 campaign contract"
    )
    search_spec = _mapping(
        campaign_contract.get("search_spec"), "V8 search_spec"
    )
    if search_spec.get("kind") != SOURCE_SEARCH_KIND:
        raise ValueError("frozen source is not the V8 action-plan campaign")
    program = causal_action_program_from_dict_v1(source.get("frozen_program"))
    if program.origin is not ProgramOriginV1.SEARCHED_REACTIVE:
        raise ValueError("V8 parent must be a searched reactive program")
    if (
        winner.get("program_ref") != program.program_id
        or winner.get("program_id") != program.program_id
        or winner.get("program_key") != program.program_key()
        or winner.get("program_origin") != program.origin.value
    ):
        raise ValueError("V8 frozen winner program identity is inconsistent")
    loadout_id = _text(winner.get("loadout_id"), "V8 winner loadout_id")
    loadouts = campaign_contract.get("loadout_ids")
    if not isinstance(loadouts, list) or loadout_id not in loadouts:
        raise ValueError("V8 winner loadout is absent from its campaign")
    return FrozenV8ParentRefV9(
        source_campaign_id=source_campaign_id,
        source_freeze_ref=source_freeze_ref,
        build_id=build_id,
        loadout_id=loadout_id,
        program_ref=program.program_id,
        program_id=program.program_id,
        program_key=program.program_key(),
        policy_bundle_ref=policy_bundle_ref,
    )


def build_cat_action_plan_append_contract_v9(
    *,
    campaign_id: str,
    frozen_v8: Mapping[str, Any],
    source_freeze_ref: str,
    policy_bundle_ref: str,
    pull_time_ms: int = 3_000,
    max_decisions: int = 1_024,
) -> "CatActionPlanAppendContractV9":
    """Build the canonical V9 contract directly from a frozen V8 result."""

    return CatActionPlanAppendContractV9(
        campaign_id=campaign_id,
        paired_parent_ref=freeze_v8_parent_ref_v9(
            frozen_v8,
            source_freeze_ref=source_freeze_ref,
            policy_bundle_ref=policy_bundle_ref,
        ),
        pull_time_ms=pull_time_ms,
        max_decisions=max_decisions,
    )


def validate_v8_parent_bundle_v9(
    parent: FrozenV8ParentRefV9,
    bundle: Mapping[str, Any],
) -> DevelopmentTwoWaveCatResidualSequenceV1:
    """Resolve the frozen parent from its portable V8 policy bundle."""

    if not isinstance(parent, FrozenV8ParentRefV9):
        raise TypeError("parent must be FrozenV8ParentRefV9")
    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    if (
        bundle.get("exact_build_id") != parent.build_id
        or bundle.get("loadout_id") != parent.loadout_id
    ):
        raise ValueError("V8 policy bundle build/loadout differs from parent")
    try:
        policy = policies[parent.program_ref]
    except KeyError as error:
        raise ValueError("V8 policy bundle does not contain the frozen parent") from error
    program = build_two_wave_cat_residual_sequence_runtime_v1(
        policy,
        cat_resolver_factory=lambda: (_ for _ in ()).throw(
            AssertionError("identity validation must not open Cat")
        ),
    )[0]
    if (
        policy.exact_build_id != parent.build_id
        or program.program_id != parent.program_id
        or program.program_key() != parent.program_key
    ):
        raise ValueError("V8 policy bundle parent identity differs from freeze")
    return policy


def _examples_for_seeds(
    seeds: Sequence[int], schedule: tuple[int, ...]
) -> tuple[TwoWaveExampleV1, ...]:
    return tuple(
        TwoWaveExampleV1(seed, schedule[index % len(schedule)])
        for index, seed in enumerate(seeds)
    )


def split_append_training_examples_v9(
    train_examples: Sequence[TwoWaveExampleV1],
) -> tuple[tuple[TwoWaveExampleV1, ...], tuple[TwoWaveExampleV1, ...]]:
    """Return the fixed, nuisance-balanced 128/128 V9 split."""

    rows = tuple(train_examples)
    if any(not isinstance(row, TwoWaveExampleV1) for row in rows):
        raise TypeError("train_examples must contain TwoWaveExampleV1 values")
    if tuple(row.seed for row in rows) != FRESH_SEED_CONTRACT.train_seeds:
        raise ValueError("V9 training examples differ from the fresh seed panel")
    schedule = FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS
    expected_arrivals = tuple(
        schedule[index % len(schedule)] for index in range(FRESH_SEED_COUNT)
    )
    if tuple(row.first_wave_arrival_ms for row in rows) != expected_arrivals:
        raise ValueError("V9 training arrivals differ from the nuisance schedule")

    stratum_index = {arrival: index for index, arrival in enumerate(schedule)}
    occurrence_by_arrival: dict[int, int] = {}
    proposal: list[TwoWaveExampleV1] = []
    selection: list[TwoWaveExampleV1] = []
    for row in rows:
        occurrence = occurrence_by_arrival.get(row.first_wave_arrival_ms, 0)
        occurrence_by_arrival[row.first_wave_arrival_ms] = occurrence + 1
        proposal_parity = stratum_index[row.first_wave_arrival_ms] % 2
        (proposal if occurrence % 2 == proposal_parity else selection).append(row)
    if (
        len(proposal) != PROPOSAL_EXAMPLE_COUNT
        or len(selection) != SELECTION_EXAMPLE_COUNT
    ):
        raise AssertionError("V9 proposal/selection split is not exactly 128/128")
    proposal_seeds = {row.seed for row in proposal}
    selection_seeds = {row.seed for row in selection}
    if proposal_seeds & selection_seeds or proposal_seeds | selection_seeds != {
        row.seed for row in rows
    }:
        raise AssertionError("V9 proposal/selection split is not disjoint-complete")
    for arrival in schedule:
        if not any(row.first_wave_arrival_ms == arrival for row in proposal):
            raise AssertionError("V9 proposal split lost an arrival stratum")
        if not any(row.first_wave_arrival_ms == arrival for row in selection):
            raise AssertionError("V9 selection split lost an arrival stratum")
    return tuple(proposal), tuple(selection)


@dataclass(frozen=True)
class CatActionPlanAppendContractV9:
    """Complete pre-outcome protocol for one frozen-parent V9 campaign."""

    campaign_id: str
    paired_parent_ref: FrozenV8ParentRefV9
    pull_time_ms: int = 3_000
    max_decisions: int = 1_024
    teacher_max_states: int = TEACHER_MAX_STATES
    teacher_plan_shard_size: int = TEACHER_PLAN_SHARD_SIZE
    max_append_candidates: int = MAX_APPEND_CANDIDATES
    seed_shard_count: int = SEED_SHARD_COUNT
    kind: str = SEARCH_KIND
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.kind != SEARCH_KIND:
            raise ValueError("append campaign schema or kind differs from V9")
        if not isinstance(self.campaign_id, str) or not _CAMPAIGN_ID.fullmatch(
            self.campaign_id
        ):
            raise ValueError("campaign_id has unsupported characters")
        if not isinstance(self.paired_parent_ref, FrozenV8ParentRefV9):
            raise TypeError("paired_parent_ref must be FrozenV8ParentRefV9")
        for field in ("pull_time_ms", "max_decisions"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        if self.max_decisions < 1_024:
            raise ValueError("V9 max_decisions must be at least 1024")
        fixed_budgets = (
            ("teacher_max_states", self.teacher_max_states, TEACHER_MAX_STATES),
            (
                "teacher_plan_shard_size",
                self.teacher_plan_shard_size,
                TEACHER_PLAN_SHARD_SIZE,
            ),
            (
                "max_append_candidates",
                self.max_append_candidates,
                MAX_APPEND_CANDIDATES,
            ),
            ("seed_shard_count", self.seed_shard_count, SEED_SHARD_COUNT),
        )
        for label, value, expected in fixed_budgets:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be the fixed integer {expected}")
            if value != expected:
                raise ValueError(f"{label} must equal the fixed V9 value {expected}")

    @property
    def build_id(self) -> str:
        return self.paired_parent_ref.build_id

    @property
    def loadout_ids(self) -> tuple[str, ...]:
        return (self.paired_parent_ref.loadout_id,)

    @property
    def train_examples(self) -> tuple[TwoWaveExampleV1, ...]:
        return _examples_for_seeds(
            FRESH_SEED_CONTRACT.train_seeds,
            FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS,
        )

    @property
    def heldout_examples(self) -> tuple[TwoWaveExampleV1, ...]:
        return _examples_for_seeds(
            FRESH_SEED_CONTRACT.evaluation_seeds,
            FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS,
        )

    @property
    def proposal_examples(self) -> tuple[TwoWaveExampleV1, ...]:
        return split_append_training_examples_v9(self.train_examples)[0]

    @property
    def selection_examples(self) -> tuple[TwoWaveExampleV1, ...]:
        return split_append_training_examples_v9(self.train_examples)[1]

    def to_dict(self) -> JSONMap:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "campaign_id": self.campaign_id,
            "build_id": self.build_id,
            "loadout_ids": list(self.loadout_ids),
            "paired_parent_ref": self.paired_parent_ref.to_dict(),
            "fresh_seed_contract": FRESH_SEED_CONTRACT.to_dict(),
            "proposal_examples": [row.to_dict() for row in self.proposal_examples],
            "selection_examples": [row.to_dict() for row in self.selection_examples],
            "heldout_examples": [row.to_dict() for row in self.heldout_examples],
            "pull_time_ms": self.pull_time_ms,
            "max_decisions": self.max_decisions,
            "teacher_max_states": self.teacher_max_states,
            "teacher_plan_shard_size": self.teacher_plan_shard_size,
            "max_append_candidates": self.max_append_candidates,
            "seed_shard_count": self.seed_shard_count,
            "wave_pair": list(WAVE_PAIR),
            "contract": {
                "single_frozen_build": True,
                "single_frozen_loadout": True,
                "parent_is_legal_retention_arm": True,
                "proposal_ranking_metric": "PAIRED_CANDIDATE_MINUS_PARENT_DAMAGE",
                "selection_gate_metric": "PAIRED_CANDIDATE_MINUS_PARENT_DAMAGE",
                "selection_acceptance_rule": "PAIRED_LOWER_BOUND_STRICTLY_GT_ZERO",
                "proposal_selection_heldout_disjoint": True,
                "arrival_nuisance_is_policy_input": False,
                "heldout_excluded_from_search_and_selection": True,
                "candidate_cap_excludes_parent": True,
                "full_upper_kara_route_claim": False,
            },
        }


def cat_action_plan_append_contract_from_dict_v9(
    value: object,
) -> CatActionPlanAppendContractV9:
    fields = {
        "schema",
        "kind",
        "campaign_id",
        "build_id",
        "loadout_ids",
        "paired_parent_ref",
        "fresh_seed_contract",
        "proposal_examples",
        "selection_examples",
        "heldout_examples",
        "pull_time_ms",
        "max_decisions",
        "teacher_max_states",
        "teacher_plan_shard_size",
        "max_append_candidates",
        "seed_shard_count",
        "wave_pair",
        "contract",
    }
    row = _exact_mapping(value, fields, "V9 append contract")
    parent = frozen_v8_parent_ref_from_dict_v9(row["paired_parent_ref"])
    result = CatActionPlanAppendContractV9(
        campaign_id=row["campaign_id"],
        paired_parent_ref=parent,
        pull_time_ms=row["pull_time_ms"],
        max_decisions=row["max_decisions"],
        teacher_max_states=row["teacher_max_states"],
        teacher_plan_shard_size=row["teacher_plan_shard_size"],
        max_append_candidates=row["max_append_candidates"],
        seed_shard_count=row["seed_shard_count"],
        kind=row["kind"],
        schema=row["schema"],
    )
    if result.to_dict() != row:
        raise ValueError("V9 append contract is not in canonical form")
    return result


def _program_receipts_v9(
    value: object,
    parent: FrozenV8ParentRefV9,
) -> list[JSONMap]:
    if not isinstance(value, list) or not value:
        raise ValueError("append manifest programs must be a nonempty list")
    receipts: list[JSONMap] = []
    refs: list[str] = []
    keys: list[str] = []
    parent_count = 0
    for raw in value:
        row = _exact_mapping(raw, _PROGRAM_RECEIPT_FIELDS, "program receipt")
        program = causal_action_program_from_dict_v1(row["program"])
        if (
            row["program_ref"] != program.program_id
            or row["program_id"] != program.program_id
            or row["program_key"] != program.program_key()
            or row["program_origin"] != program.origin.value
            or row["paired_parent_ref"] != parent.program_ref
        ):
            raise ValueError("append program receipt identity mismatch")
        if program.origin is not ProgramOriginV1.SEARCHED_REACTIVE:
            raise ValueError("append programs must be searched reactive programs")
        guides = _string_list(
            row["proposal_guide_ids"], "program receipt proposal_guide_ids"
        )
        parsed = deepcopy(row)
        parsed["proposal_guide_ids"] = guides
        receipts.append(parsed)
        refs.append(program.program_id)
        keys.append(program.program_key())
        if (
            program.program_id == parent.program_ref
            and program.program_key() == parent.program_key
        ):
            parent_count += 1
    if len(refs) != len(set(refs)) or len(keys) != len(set(keys)):
        raise ValueError("append program receipts must have unique identities")
    if len(receipts) > MAX_APPEND_CANDIDATES + 1:
        raise ValueError("append manifest exceeds the fixed candidate budget")
    if parent_count != 1:
        raise ValueError("append manifest must contain the frozen parent exactly once")
    if (
        receipts[0]["program_ref"] != parent.program_ref
        or receipts[0]["program_key"] != parent.program_key
    ):
        raise ValueError("the frozen parent must be the first append program")
    return receipts


def build_append_candidate_manifest_v9(
    *,
    contract: CatActionPlanAppendContractV9,
    proposal_guide_ids: Sequence[str],
    programs: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Build a manifest whose every candidate names its paired parent."""

    if not isinstance(contract, CatActionPlanAppendContractV9):
        raise TypeError("contract must be CatActionPlanAppendContractV9")
    parent = contract.paired_parent_ref
    normalized_programs: list[JSONMap] = []
    for raw in programs:
        row = _mapping(raw, "program receipt")
        if set(row) == _PROGRAM_RECEIPT_FIELDS - {"paired_parent_ref"}:
            row["paired_parent_ref"] = parent.program_ref
        normalized_programs.append(row)
    payload = {
        "schema": MANIFEST_SCHEMA,
        "campaign_id": contract.campaign_id,
        "build_id": contract.build_id,
        "loadout_id": parent.loadout_id,
        "paired_parent_ref": parent.to_dict(),
        "proposal_guide_ids": list(proposal_guide_ids),
        "programs": normalized_programs,
        "contract": {
            "all_candidates_use_same_build_and_loadout": True,
            "all_candidate_scores_paired_to_parent": True,
            "parent_is_present_once_and_may_be_retained": True,
            "parent_identity_source": "FROZEN_V8_SELECTION_AND_POLICY_BUNDLE",
        },
    }
    return validate_append_candidate_manifest_v9(payload, contract=contract)


def validate_append_candidate_manifest_v9(
    value: object,
    *,
    contract: CatActionPlanAppendContractV9 | None = None,
) -> JSONMap:
    fields = {
        "schema",
        "campaign_id",
        "build_id",
        "loadout_id",
        "paired_parent_ref",
        "proposal_guide_ids",
        "programs",
        "contract",
    }
    row = _exact_mapping(value, fields, "V9 append manifest")
    if row["schema"] != MANIFEST_SCHEMA:
        raise ValueError("append manifest schema differs from V9")
    parent = frozen_v8_parent_ref_from_dict_v9(row["paired_parent_ref"])
    if contract is not None:
        if not isinstance(contract, CatActionPlanAppendContractV9):
            raise TypeError("contract must be CatActionPlanAppendContractV9")
        if parent != contract.paired_parent_ref:
            raise ValueError("append manifest paired parent differs from campaign")
    campaign_id = _text(row["campaign_id"], "manifest campaign_id")
    if not _CAMPAIGN_ID.fullmatch(campaign_id):
        raise ValueError("manifest campaign_id has unsupported characters")
    if (
        row["build_id"] != parent.build_id
        or row["loadout_id"] != parent.loadout_id
        or (
            contract is not None
            and campaign_id != contract.campaign_id
        )
    ):
        raise ValueError("append manifest build/loadout/campaign differs from parent")
    parsed = {
        **deepcopy(row),
        "campaign_id": campaign_id,
        "proposal_guide_ids": _string_list(
            row["proposal_guide_ids"], "manifest proposal_guide_ids"
        ),
        "programs": _program_receipts_v9(row["programs"], parent),
    }
    expected_contract = {
        "all_candidates_use_same_build_and_loadout": True,
        "all_candidate_scores_paired_to_parent": True,
        "parent_is_present_once_and_may_be_retained": True,
        "parent_identity_source": "FROZEN_V8_SELECTION_AND_POLICY_BUNDLE",
    }
    if parsed["contract"] != expected_contract:
        raise ValueError("append manifest contract differs from V9")
    return parsed


__all__ = (
    "CatActionPlanAppendContractV9",
    "FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS",
    "FRESH_EVALUATION_SEED_START",
    "FRESH_SEED_CONTRACT",
    "FRESH_SEED_COUNT",
    "FRESH_TRAIN_SEED_START",
    "FrozenV8ParentRefV9",
    "HELDOUT_EXAMPLE_COUNT",
    "MANIFEST_SCHEMA",
    "MAX_APPEND_CANDIDATES",
    "PARENT_REF_SCHEMA",
    "PROPOSAL_EXAMPLE_COUNT",
    "SCHEMA",
    "SEARCH_KIND",
    "SEED_SHARD_COUNT",
    "SELECTION_EXAMPLE_COUNT",
    "TEACHER_MAX_STATES",
    "TEACHER_PLAN_SHARD_SIZE",
    "WAVE_PAIR",
    "build_append_candidate_manifest_v9",
    "build_cat_action_plan_append_contract_v9",
    "cat_action_plan_append_contract_from_dict_v9",
    "freeze_v8_parent_ref_v9",
    "frozen_v8_parent_ref_from_dict_v9",
    "split_append_training_examples_v9",
    "validate_append_candidate_manifest_v9",
    "validate_v8_parent_bundle_v9",
)
