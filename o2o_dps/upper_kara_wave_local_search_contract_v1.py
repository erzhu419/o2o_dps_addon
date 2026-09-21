"""Frozen contract for search scoped to one real Upper Karazhan wave.

The unit of optimization is exactly one route pull, one legacy single boss
phase, or one continuous boss encounter.  A continuous boss encounter keeps
combat state across its observable target stages; the stages are target rules,
not simulator resets.  Target legality is stage-local and derived only from the
currently observed target state.  Cross-wave cooldown allocation is
deliberately left to a later route planner; this contract asks the local search
to report a value for each frozen resource-availability case instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_wave_local_search_contract/v1"
SEARCH_KIND = "UPPER_KARA_WAVE_LOCAL_SEARCH_V1"
UNIT_KINDS = frozenset({"ROUTE_PULL", "BOSS_PHASE", "BOSS_ENCOUNTER"})
STAGE_ROLES = frozenset({"PULL", "BOSS", "ADDS"})
DIRECT_TARGET_MODES = frozenset(
    {"FIXED_SEQUENCE", "ANY_LEGAL", "FOCUS_ONE_UNTIL_DEAD"}
)
OBSERVABLE_FIELDS = frozenset({"visible", "attackable", "dead"})
TRANSITION_MATCHES = frozenset({"ALL", "ANY"})
VARIANT_BUDGETS = frozenset({256, 1024})
BASELINE_KINDS = frozenset(
    {"CAT", "DEPLOYED_CONTRA", "CONTRA_NEW", "OFFLINE_EXPERT"}
)
RESOURCE_KINDS = frozenset({"LONG_COOLDOWN", "POTION"})
TARGET_IDENTITY_KINDS = frozenset(
    {
        "REGISTRY_CREATURE_OCCURRENCE",
        "EXACT_F130_CREATURE_OCCURRENCE",
        "BOSS_OWNED_SUMMON_OCCURRENCE",
    }
)

OBJECTIVE_SCOPE = "ONE_SEARCH_UNIT_ONLY"
CANDIDATE_AGGREGATION = "NO_CROSS_WAVE_AVERAGING"
RESOURCE_PLANNING_SCOPE = "OUTSIDE_LOCAL_SEARCH"
LOCAL_VALUE_OUTPUT = "VALUE_BY_RESOURCE_AVAILABILITY_CASE"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _choice(value: object, choices: frozenset[str], label: str) -> str:
    result = _text(value, label)
    if result not in choices:
        raise ValueError(f"{label} must be one of {sorted(choices)}")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _mapping(value: object, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact_mapping(value: object, fields: set[str], label: str) -> JSONMap:
    row = _mapping(value, label)
    if set(row) != fields:
        raise ValueError(f"{label} fields differ from the v1 contract")
    return row


def _tuple_of_text(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    result = tuple(_text(item, f"{label} item") for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique values")
    return result


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


@dataclass(frozen=True)
class WaveSearchUnitV1:
    unit_id: str
    unit_kind: str
    route_ref: str
    pull_or_phase_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit_id", _text(self.unit_id, "unit_id"))
        object.__setattr__(
            self, "unit_kind", _choice(self.unit_kind, UNIT_KINDS, "unit_kind")
        )
        object.__setattr__(self, "route_ref", _text(self.route_ref, "route_ref"))
        object.__setattr__(
            self,
            "pull_or_phase_ref",
            _text(self.pull_or_phase_ref, "pull_or_phase_ref"),
        )

    def to_dict(self) -> JSONMap:
        return {
            "unit_id": self.unit_id,
            "unit_kind": self.unit_kind,
            "route_ref": self.route_ref,
            "pull_or_phase_ref": self.pull_or_phase_ref,
        }


def wave_search_unit_from_dict_v1(value: object) -> WaveSearchUnitV1:
    return WaveSearchUnitV1(
        **_exact_mapping(
            value,
            {"unit_id", "unit_kind", "route_ref", "pull_or_phase_ref"},
            "search unit",
        )
    )


@dataclass(frozen=True)
class FrozenPlayerIdentityV1:
    build_id: str
    talent_ref: str
    equipment_ref: str
    loadout_ref: str

    def __post_init__(self) -> None:
        for field in ("build_id", "talent_ref", "equipment_ref", "loadout_ref"):
            object.__setattr__(self, field, _text(getattr(self, field), field))

    def to_dict(self) -> JSONMap:
        return {
            "build_id": self.build_id,
            "talent_ref": self.talent_ref,
            "equipment_ref": self.equipment_ref,
            "loadout_ref": self.loadout_ref,
        }


def frozen_player_identity_from_dict_v1(value: object) -> FrozenPlayerIdentityV1:
    return FrozenPlayerIdentityV1(
        **_exact_mapping(
            value,
            {"build_id", "talent_ref", "equipment_ref", "loadout_ref"},
            "frozen player identity",
        )
    )


@dataclass(frozen=True)
class WaveTargetBindingV1:
    occurrence_id: str
    target_guid: str
    identity_kind: str
    creature_entry_id: int | None
    hp_model_ref: str
    armor_model_ref: str
    team_kill_clock_ref: str
    attackability_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", _text(self.occurrence_id, "occurrence_id")
        )
        object.__setattr__(self, "target_guid", _text(self.target_guid, "target_guid"))
        object.__setattr__(
            self,
            "identity_kind",
            _choice(self.identity_kind, TARGET_IDENTITY_KINDS, "identity_kind"),
        )
        object.__setattr__(
            self,
            "creature_entry_id",
            _optional_positive_int(self.creature_entry_id, "creature_entry_id"),
        )
        if (
            self.identity_kind in {
                "REGISTRY_CREATURE_OCCURRENCE",
                "EXACT_F130_CREATURE_OCCURRENCE",
            }
            and self.creature_entry_id is None
        ):
            raise ValueError(
                "registry and exact F130 target identities require creature_entry_id"
            )
        for field in (
            "hp_model_ref",
            "armor_model_ref",
            "team_kill_clock_ref",
            "attackability_ref",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))

    def to_dict(self) -> JSONMap:
        return {
            "occurrence_id": self.occurrence_id,
            "target_guid": self.target_guid,
            "identity_kind": self.identity_kind,
            "creature_entry_id": self.creature_entry_id,
            "hp_model_ref": self.hp_model_ref,
            "armor_model_ref": self.armor_model_ref,
            "team_kill_clock_ref": self.team_kill_clock_ref,
            "attackability_ref": self.attackability_ref,
        }


def wave_target_binding_from_dict_v1(value: object) -> WaveTargetBindingV1:
    return WaveTargetBindingV1(
        **_exact_mapping(
            value,
            {
                "occurrence_id",
                "target_guid",
                "identity_kind",
                "creature_entry_id",
                "hp_model_ref",
                "armor_model_ref",
                "team_kill_clock_ref",
                "attackability_ref",
            },
            "target binding",
        )
    )


@dataclass(frozen=True)
class ObservableTargetConditionV1:
    occurrence_id: str
    observable: str
    equals: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", _text(self.occurrence_id, "occurrence_id")
        )
        object.__setattr__(
            self,
            "observable",
            _choice(self.observable, OBSERVABLE_FIELDS, "observable"),
        )
        if not isinstance(self.equals, bool):
            raise ValueError("condition equals must be boolean")

    def to_dict(self) -> JSONMap:
        return {
            "occurrence_id": self.occurrence_id,
            "observable": self.observable,
            "equals": self.equals,
        }


def observable_target_condition_from_dict_v1(
    value: object,
) -> ObservableTargetConditionV1:
    return ObservableTargetConditionV1(
        **_exact_mapping(
            value,
            {"occurrence_id", "observable", "equals"},
            "observable target condition",
        )
    )


@dataclass(frozen=True)
class TargetStageV1:
    stage_id: str
    stage_role: str
    direct_target_mode: str
    direct_target_occurrence_ids: tuple[str, ...]
    collateral_target_occurrence_ids: tuple[str, ...]
    transition_match: str | None = None
    transition_conditions: tuple[ObservableTargetConditionV1, ...] = ()
    next_stage_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_id", _text(self.stage_id, "stage_id"))
        object.__setattr__(
            self, "stage_role", _choice(self.stage_role, STAGE_ROLES, "stage_role")
        )
        object.__setattr__(
            self,
            "direct_target_mode",
            _choice(
                self.direct_target_mode, DIRECT_TARGET_MODES, "direct_target_mode"
            ),
        )
        direct = _tuple_of_text(
            self.direct_target_occurrence_ids, "direct_target_occurrence_ids"
        )
        collateral = _tuple_of_text(
            self.collateral_target_occurrence_ids,
            "collateral_target_occurrence_ids",
            allow_empty=True,
        )
        object.__setattr__(self, "direct_target_occurrence_ids", direct)
        object.__setattr__(self, "collateral_target_occurrence_ids", collateral)
        conditions = tuple(self.transition_conditions)
        if any(not isinstance(row, ObservableTargetConditionV1) for row in conditions):
            raise TypeError(
                "transition_conditions must contain ObservableTargetConditionV1"
            )
        object.__setattr__(self, "transition_conditions", conditions)
        if self.next_stage_id is None:
            if self.transition_match is not None or conditions:
                raise ValueError("terminal stage cannot declare a transition")
        else:
            object.__setattr__(
                self, "next_stage_id", _text(self.next_stage_id, "next_stage_id")
            )
            object.__setattr__(
                self,
                "transition_match",
                _choice(
                    self.transition_match, TRANSITION_MATCHES, "transition_match"
                ),
            )
            if not conditions:
                raise ValueError("nonterminal stage needs observable conditions")

    def to_dict(self) -> JSONMap:
        return {
            "stage_id": self.stage_id,
            "stage_role": self.stage_role,
            "direct_target_mode": self.direct_target_mode,
            "direct_target_occurrence_ids": list(self.direct_target_occurrence_ids),
            "collateral_target_occurrence_ids": list(
                self.collateral_target_occurrence_ids
            ),
            "transition_match": self.transition_match,
            "transition_conditions": [row.to_dict() for row in self.transition_conditions],
            "next_stage_id": self.next_stage_id,
        }


def target_stage_from_dict_v1(value: object) -> TargetStageV1:
    row = _exact_mapping(
        value,
        {
            "stage_id",
            "stage_role",
            "direct_target_mode",
            "direct_target_occurrence_ids",
            "collateral_target_occurrence_ids",
            "transition_match",
            "transition_conditions",
            "next_stage_id",
        },
        "target stage",
    )
    row["direct_target_occurrence_ids"] = _tuple_of_text(
        row["direct_target_occurrence_ids"], "direct_target_occurrence_ids"
    )
    row["collateral_target_occurrence_ids"] = _tuple_of_text(
        row["collateral_target_occurrence_ids"],
        "collateral_target_occurrence_ids",
        allow_empty=True,
    )
    row["transition_conditions"] = tuple(
        observable_target_condition_from_dict_v1(item)
        for item in _rows(row["transition_conditions"], "transition_conditions")
    )
    return TargetStageV1(**row)


@dataclass(frozen=True)
class ObservedTargetStateV1:
    visible: bool
    attackable: bool
    dead: bool

    def __post_init__(self) -> None:
        if not all(isinstance(value, bool) for value in self.to_dict().values()):
            raise ValueError("observed target state fields must be boolean")
        if self.attackable and (not self.visible or self.dead):
            raise ValueError("an attackable target must be visible and alive")

    def to_dict(self) -> dict[str, bool]:
        return {
            "visible": self.visible,
            "attackable": self.attackable,
            "dead": self.dead,
        }


def _indexed_observations_v1(
    targets: Sequence[WaveTargetBindingV1],
    observations: Sequence[ObservedTargetStateV1],
) -> tuple[tuple[WaveTargetBindingV1, ...], tuple[ObservedTargetStateV1, ...], dict[str, int]]:
    target_rows = tuple(targets)
    observed_rows = tuple(observations)
    if not target_rows or any(
        not isinstance(row, WaveTargetBindingV1) for row in target_rows
    ):
        raise TypeError("targets must contain WaveTargetBindingV1 values")
    if len(observed_rows) != len(target_rows) or any(
        not isinstance(row, ObservedTargetStateV1) for row in observed_rows
    ):
        raise ValueError("observations must align one-to-one with targets")
    index = {row.occurrence_id: position for position, row in enumerate(target_rows)}
    if len(index) != len(target_rows):
        raise ValueError("target occurrence identities must be unique")
    return target_rows, observed_rows, index


def legal_direct_target_indexes_v1(
    stage: TargetStageV1,
    targets: Sequence[WaveTargetBindingV1],
    observations: Sequence[ObservedTargetStateV1],
) -> tuple[int, ...]:
    """Resolve direct-target indexes from current state only.

    In ``FIXED_SEQUENCE`` mode the first ordered target not yet observed dead
    blocks every later target, including while it is temporarily invisible or
    unattackable.  This prevents the optimizer from inventing a forbidden
    target swap.  ``ANY_LEGAL`` and ``FOCUS_ONE_UNTIL_DEAD`` expose every
    currently visible, attackable, living member of the stage's direct-target
    set here.  The runtime gate applies the latter mode's stateful focus lock
    using the currently selected target; this pure helper intentionally has no
    selected-target input.
    """

    if not isinstance(stage, TargetStageV1):
        raise TypeError("stage must be TargetStageV1")
    _, states, indexes = _indexed_observations_v1(targets, observations)
    try:
        ordered = tuple(indexes[target_id] for target_id in stage.direct_target_occurrence_ids)
    except KeyError as error:
        raise ValueError("stage references an unknown direct target") from error
    if stage.direct_target_mode == "FIXED_SEQUENCE":
        for index in ordered:
            state = states[index]
            if state.dead:
                continue
            return (index,) if state.visible and state.attackable else ()
        return ()
    return tuple(
        index
        for index in ordered
        if states[index].visible and states[index].attackable and not states[index].dead
    )


def legal_collateral_target_indexes_v1(
    stage: TargetStageV1,
    targets: Sequence[WaveTargetBindingV1],
    observations: Sequence[ObservedTargetStateV1],
) -> tuple[int, ...]:
    """Resolve independently declared AOE/collateral target indexes."""

    if not isinstance(stage, TargetStageV1):
        raise TypeError("stage must be TargetStageV1")
    _, states, indexes = _indexed_observations_v1(targets, observations)
    try:
        allowed = tuple(
            indexes[target_id] for target_id in stage.collateral_target_occurrence_ids
        )
    except KeyError as error:
        raise ValueError("stage references an unknown collateral target") from error
    return tuple(
        index
        for index in allowed
        if states[index].visible and states[index].attackable and not states[index].dead
    )


def stage_transition_satisfied_v1(
    stage: TargetStageV1,
    targets: Sequence[WaveTargetBindingV1],
    observations: Sequence[ObservedTargetStateV1],
) -> bool:
    """Evaluate a stage edge from dead/visible/attackable observations only."""

    if not isinstance(stage, TargetStageV1):
        raise TypeError("stage must be TargetStageV1")
    _, states, indexes = _indexed_observations_v1(targets, observations)
    if stage.next_stage_id is None:
        return False
    try:
        values = tuple(
            getattr(states[indexes[row.occurrence_id]], row.observable) is row.equals
            for row in stage.transition_conditions
        )
    except KeyError as error:
        raise ValueError("stage transition references an unknown target") from error
    return all(values) if stage.transition_match == "ALL" else any(values)


@dataclass(frozen=True)
class SeedNamespaceV1:
    namespace: str
    seeds: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "namespace", _text(self.namespace, "seed namespace")
        )
        seeds = tuple(_positive_int(seed, "seed") for seed in self.seeds)
        if not seeds or len(seeds) != len(set(seeds)):
            raise ValueError("seed namespace must contain unique seeds")
        object.__setattr__(self, "seeds", seeds)

    def to_dict(self) -> JSONMap:
        return {"namespace": self.namespace, "seeds": list(self.seeds)}


def seed_namespace_from_dict_v1(value: object) -> SeedNamespaceV1:
    row = _exact_mapping(value, {"namespace", "seeds"}, "seed namespace")
    seeds = row["seeds"]
    if not isinstance(seeds, list):
        raise ValueError("seed namespace seeds must be a list")
    row["seeds"] = tuple(seeds)
    return SeedNamespaceV1(**row)


@dataclass(frozen=True)
class BaselineRefV1:
    baseline_id: str
    baseline_kind: str
    policy_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "baseline_id", _text(self.baseline_id, "baseline_id")
        )
        object.__setattr__(
            self,
            "baseline_kind",
            _choice(self.baseline_kind, BASELINE_KINDS, "baseline_kind"),
        )
        object.__setattr__(self, "policy_ref", _text(self.policy_ref, "policy_ref"))

    def to_dict(self) -> JSONMap:
        return {
            "baseline_id": self.baseline_id,
            "baseline_kind": self.baseline_kind,
            "policy_ref": self.policy_ref,
        }


def baseline_ref_from_dict_v1(value: object) -> BaselineRefV1:
    return BaselineRefV1(
        **_exact_mapping(
            value,
            {"baseline_id", "baseline_kind", "policy_ref"},
            "baseline reference",
        )
    )


@dataclass(frozen=True)
class ResourceRefV1:
    resource_id: str
    resource_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_id", _text(self.resource_id, "resource_id"))
        object.__setattr__(
            self,
            "resource_kind",
            _choice(self.resource_kind, RESOURCE_KINDS, "resource_kind"),
        )

    def to_dict(self) -> JSONMap:
        return {"resource_id": self.resource_id, "resource_kind": self.resource_kind}


def resource_ref_from_dict_v1(value: object) -> ResourceRefV1:
    return ResourceRefV1(
        **_exact_mapping(
            value, {"resource_id", "resource_kind"}, "resource reference"
        )
    )


@dataclass(frozen=True)
class ResourceAvailabilityCaseV1:
    case_id: str
    available_resource_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id"))
        object.__setattr__(
            self,
            "available_resource_ids",
            _tuple_of_text(
                self.available_resource_ids,
                "available_resource_ids",
                allow_empty=True,
            ),
        )

    def to_dict(self) -> JSONMap:
        return {
            "case_id": self.case_id,
            "available_resource_ids": list(self.available_resource_ids),
        }


def resource_availability_case_from_dict_v1(
    value: object,
) -> ResourceAvailabilityCaseV1:
    row = _exact_mapping(
        value, {"case_id", "available_resource_ids"}, "resource availability case"
    )
    row["available_resource_ids"] = _tuple_of_text(
        row["available_resource_ids"], "available_resource_ids", allow_empty=True
    )
    return ResourceAvailabilityCaseV1(**row)


@dataclass(frozen=True)
class UpperKaraWaveLocalSearchContractV1:
    campaign_id: str
    search_unit: WaveSearchUnitV1
    frozen_player: FrozenPlayerIdentityV1
    targets: tuple[WaveTargetBindingV1, ...]
    target_stages: tuple[TargetStageV1, ...]
    variant_budget: int
    baselines: tuple[BaselineRefV1, ...]
    train_seeds: SeedNamespaceV1
    selection_seeds: SeedNamespaceV1
    heldout_seeds: SeedNamespaceV1
    resources: tuple[ResourceRefV1, ...]
    resource_availability_cases: tuple[ResourceAvailabilityCaseV1, ...]
    objective_scope: str = OBJECTIVE_SCOPE
    candidate_aggregation: str = CANDIDATE_AGGREGATION
    variant_budget_excludes_baselines: bool = True
    cross_wave_resource_planning: str = RESOURCE_PLANNING_SCOPE
    local_value_output: str = LOCAL_VALUE_OUTPUT
    kind: str = SEARCH_KIND
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.kind != SEARCH_KIND:
            raise ValueError("schema or search kind differs from v1")
        object.__setattr__(self, "campaign_id", _text(self.campaign_id, "campaign_id"))
        if not isinstance(self.search_unit, WaveSearchUnitV1):
            raise TypeError("search_unit must be WaveSearchUnitV1")
        if not isinstance(self.frozen_player, FrozenPlayerIdentityV1):
            raise TypeError("frozen_player must be FrozenPlayerIdentityV1")
        for field, expected in (
            ("objective_scope", OBJECTIVE_SCOPE),
            ("candidate_aggregation", CANDIDATE_AGGREGATION),
            ("cross_wave_resource_planning", RESOURCE_PLANNING_SCOPE),
            ("local_value_output", LOCAL_VALUE_OUTPUT),
        ):
            if getattr(self, field) != expected:
                raise ValueError(f"{field} differs from the local-search contract")
        if self.variant_budget not in VARIANT_BUDGETS:
            raise ValueError("variant_budget must be exactly 256 or 1024")
        if self.variant_budget_excludes_baselines is not True:
            raise ValueError("baselines must be excluded from variant_budget")

        targets = tuple(self.targets)
        stages = tuple(self.target_stages)
        baselines = tuple(self.baselines)
        resources = tuple(self.resources)
        cases = tuple(self.resource_availability_cases)
        if not targets or any(not isinstance(row, WaveTargetBindingV1) for row in targets):
            raise TypeError("targets must contain WaveTargetBindingV1 values")
        if not stages or any(not isinstance(row, TargetStageV1) for row in stages):
            raise TypeError("target_stages must contain TargetStageV1 values")
        if not baselines or any(not isinstance(row, BaselineRefV1) for row in baselines):
            raise TypeError("baselines must contain BaselineRefV1 values")
        if any(not isinstance(row, ResourceRefV1) for row in resources):
            raise TypeError("resources must contain ResourceRefV1 values")
        if not cases or any(
            not isinstance(row, ResourceAvailabilityCaseV1) for row in cases
        ):
            raise TypeError(
                "resource_availability_cases must contain ResourceAvailabilityCaseV1"
            )
        for field, rows in (
            ("targets", targets),
            ("target_stages", stages),
            ("baselines", baselines),
            ("resources", resources),
            ("resource_availability_cases", cases),
        ):
            object.__setattr__(self, field, rows)

        target_ids = tuple(row.occurrence_id for row in targets)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target occurrence identities must be unique")
        stage_ids = tuple(row.stage_id for row in stages)
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("stage identities must be unique")
        target_set = set(target_ids)
        for index, stage in enumerate(stages):
            mentioned = set(stage.direct_target_occurrence_ids) | set(
                stage.collateral_target_occurrence_ids
            )
            mentioned.update(row.occurrence_id for row in stage.transition_conditions)
            if not mentioned <= target_set:
                raise ValueError(f"stage {stage.stage_id} references an unknown target")
            expected_next = stages[index + 1].stage_id if index + 1 < len(stages) else None
            if stage.next_stage_id != expected_next:
                raise ValueError("stages must advance only to the next declared stage")
            if stage.direct_target_mode in {
                "FIXED_SEQUENCE",
                "FOCUS_ONE_UNTIL_DEAD",
            } and stage.next_stage_id:
                dead_ids = {
                    row.occurrence_id
                    for row in stage.transition_conditions
                    if row.observable == "dead" and row.equals
                }
                if stage.transition_match != "ALL" or not set(
                    stage.direct_target_occurrence_ids
                ) <= dead_ids:
                    raise ValueError(
                        "a fixed/focus stage may advance only after every direct target is observed dead"
                    )
        if self.search_unit.unit_kind == "ROUTE_PULL" and any(
            stage.stage_role != "PULL" for stage in stages
        ):
            raise ValueError("route-pull units may contain only PULL stages")
        if self.search_unit.unit_kind in {"BOSS_PHASE", "BOSS_ENCOUNTER"} and any(
            stage.stage_role not in {"BOSS", "ADDS"} for stage in stages
        ):
            raise ValueError("boss units may contain only BOSS/ADDS stages")

        baseline_ids = [row.baseline_id for row in baselines]
        if len(baseline_ids) != len(set(baseline_ids)):
            raise ValueError("baseline identities must be unique")
        if {row.baseline_kind for row in baselines} != BASELINE_KINDS:
            raise ValueError(
                "baselines must include Cat, deployed Contra, Contra_new, and offline expert"
            )

        cohorts = (self.train_seeds, self.selection_seeds, self.heldout_seeds)
        if any(not isinstance(row, SeedNamespaceV1) for row in cohorts):
            raise TypeError("seed cohorts must be SeedNamespaceV1 values")
        namespaces = [row.namespace for row in cohorts]
        if len(namespaces) != len(set(namespaces)):
            raise ValueError("train/selection/heldout namespaces must be distinct")
        for index, left in enumerate(cohorts):
            for right in cohorts[index + 1 :]:
                if set(left.seeds) & set(right.seeds):
                    raise ValueError("train/selection/heldout seeds must not overlap")

        resource_ids = [row.resource_id for row in resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("resource identities must be unique")
        case_ids = [row.case_id for row in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("resource availability case identities must be unique")
        resource_set = set(resource_ids)
        combinations: set[tuple[str, ...]] = set()
        for case in cases:
            if not set(case.available_resource_ids) <= resource_set:
                raise ValueError("resource availability case names an unknown resource")
            canonical = tuple(sorted(case.available_resource_ids))
            if canonical in combinations:
                raise ValueError("resource availability cases must be distinct")
            combinations.add(canonical)
        ordered_resource_ids = tuple(sorted(resource_ids))
        expected_combinations = {
            tuple(
                resource_id
                for index, resource_id in enumerate(ordered_resource_ids)
                if mask & (1 << index)
            )
            for mask in range(1 << len(ordered_resource_ids))
        }
        if combinations != expected_combinations:
            raise ValueError(
                "resource availability cases must cover every declared resource state"
            )

    @property
    def total_policy_lanes_per_resource_case(self) -> int:
        """Candidate lanes plus separately accounted baseline lanes."""

        return self.variant_budget + len(self.baselines)

    def legal_direct_targets(
        self,
        stage_id: str,
        observations: Mapping[str, ObservedTargetStateV1],
    ) -> tuple[str, ...]:
        return tuple(
            self.targets[index].occurrence_id
            for index in self.legal_direct_target_indexes(stage_id, observations)
        )

    def legal_direct_target_indexes(
        self,
        stage_id: str,
        observations: Mapping[str, ObservedTargetStateV1],
    ) -> tuple[int, ...]:
        stage = self._stage(stage_id)
        current = self._validate_observations(observations)
        return legal_direct_target_indexes_v1(
            stage,
            self.targets,
            tuple(current[row.occurrence_id] for row in self.targets),
        )

    def legal_collateral_targets(
        self,
        stage_id: str,
        observations: Mapping[str, ObservedTargetStateV1],
    ) -> tuple[str, ...]:
        return tuple(
            self.targets[index].occurrence_id
            for index in self.legal_collateral_target_indexes(stage_id, observations)
        )

    def legal_collateral_target_indexes(
        self,
        stage_id: str,
        observations: Mapping[str, ObservedTargetStateV1],
    ) -> tuple[int, ...]:
        stage = self._stage(stage_id)
        current = self._validate_observations(observations)
        return legal_collateral_target_indexes_v1(
            stage,
            self.targets,
            tuple(current[row.occurrence_id] for row in self.targets),
        )

    def transition_satisfied(
        self,
        stage_id: str,
        observations: Mapping[str, ObservedTargetStateV1],
    ) -> bool:
        stage = self._stage(stage_id)
        current = self._validate_observations(observations)
        return stage_transition_satisfied_v1(
            stage,
            self.targets,
            tuple(current[row.occurrence_id] for row in self.targets),
        )

    def _stage(self, stage_id: str) -> TargetStageV1:
        wanted = _text(stage_id, "stage_id")
        for stage in self.target_stages:
            if stage.stage_id == wanted:
                return stage
        raise ValueError(f"unknown stage_id: {wanted}")

    def _validate_observations(
        self, observations: Mapping[str, ObservedTargetStateV1]
    ) -> dict[str, ObservedTargetStateV1]:
        if not isinstance(observations, Mapping):
            raise TypeError("observations must be a mapping")
        current = dict(observations)
        expected = {row.occurrence_id for row in self.targets}
        if set(current) != expected or any(
            not isinstance(row, ObservedTargetStateV1) for row in current.values()
        ):
            raise ValueError("observations must contain current state for every target")
        return current

    def to_dict(self) -> JSONMap:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "campaign_id": self.campaign_id,
            "search_unit": self.search_unit.to_dict(),
            "frozen_player": self.frozen_player.to_dict(),
            "targets": [row.to_dict() for row in self.targets],
            "target_stages": [row.to_dict() for row in self.target_stages],
            "variant_budget": self.variant_budget,
            "baselines": [row.to_dict() for row in self.baselines],
            "train_seeds": self.train_seeds.to_dict(),
            "selection_seeds": self.selection_seeds.to_dict(),
            "heldout_seeds": self.heldout_seeds.to_dict(),
            "resources": [row.to_dict() for row in self.resources],
            "resource_availability_cases": [
                row.to_dict() for row in self.resource_availability_cases
            ],
            "objective_scope": self.objective_scope,
            "candidate_aggregation": self.candidate_aggregation,
            "variant_budget_excludes_baselines": self.variant_budget_excludes_baselines,
            "cross_wave_resource_planning": self.cross_wave_resource_planning,
            "local_value_output": self.local_value_output,
        }


def upper_kara_wave_local_search_contract_from_dict_v1(
    value: object,
) -> UpperKaraWaveLocalSearchContractV1:
    row = _exact_mapping(
        value,
        {
            "schema",
            "kind",
            "campaign_id",
            "search_unit",
            "frozen_player",
            "targets",
            "target_stages",
            "variant_budget",
            "baselines",
            "train_seeds",
            "selection_seeds",
            "heldout_seeds",
            "resources",
            "resource_availability_cases",
            "objective_scope",
            "candidate_aggregation",
            "variant_budget_excludes_baselines",
            "cross_wave_resource_planning",
            "local_value_output",
        },
        "wave-local search contract",
    )
    row["search_unit"] = wave_search_unit_from_dict_v1(row["search_unit"])
    row["frozen_player"] = frozen_player_identity_from_dict_v1(
        row["frozen_player"]
    )
    row["targets"] = tuple(
        wave_target_binding_from_dict_v1(item)
        for item in _rows(row["targets"], "targets")
    )
    row["target_stages"] = tuple(
        target_stage_from_dict_v1(item)
        for item in _rows(row["target_stages"], "target_stages")
    )
    row["baselines"] = tuple(
        baseline_ref_from_dict_v1(item)
        for item in _rows(row["baselines"], "baselines")
    )
    row["train_seeds"] = seed_namespace_from_dict_v1(row["train_seeds"])
    row["selection_seeds"] = seed_namespace_from_dict_v1(row["selection_seeds"])
    row["heldout_seeds"] = seed_namespace_from_dict_v1(row["heldout_seeds"])
    row["resources"] = tuple(
        resource_ref_from_dict_v1(item)
        for item in _rows(row["resources"], "resources")
    )
    row["resource_availability_cases"] = tuple(
        resource_availability_case_from_dict_v1(item)
        for item in _rows(
            row["resource_availability_cases"], "resource_availability_cases"
        )
    )
    return UpperKaraWaveLocalSearchContractV1(**row)
