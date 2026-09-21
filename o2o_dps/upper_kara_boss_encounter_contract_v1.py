"""Build one continuous, model-bound Upper Karazhan boss search cell.

Registry phase candidates are observational change points.  They are not
independent simulator episodes: rage, stance, GCDs, cooldowns, swing timers,
queued attacks, auras, debuffs, and weapon state persist through a boss fight.
This adapter therefore binds one whole boss encounter to an authoritative
observable target-stage plan and records the entry-state/model references that
continuous replay must consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .brainofcat_shadow_checkpoint_v1 import REQUIRED_FIELDS as CHECKPOINT_FIELDS
from .upper_kara_registry_local_contract_adapter_v1 import (
    resource_availability_cases_v1,
)
from .upper_kara_route_wave_registry_v1 import (
    OVERLAY_SOURCE_KINDS,
    validate_route_wave_registry_v1,
)
from .upper_kara_wave_local_search_contract_v1 import (
    BaselineRefV1,
    FrozenPlayerIdentityV1,
    ObservableTargetConditionV1,
    ResourceRefV1,
    SeedNamespaceV1,
    TargetStageV1,
    UpperKaraWaveLocalSearchContractV1,
    WaveSearchUnitV1,
    WaveTargetBindingV1,
    upper_kara_wave_local_search_contract_from_dict_v1,
)


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_boss_encounter_search_cell/v1"
STATE_CONTINUITY = "NO_RESET_BETWEEN_OBSERVABLE_TARGET_STAGES"
REQUIRED_CONTINUOUS_STATE_FIELDS = tuple(CHECKPOINT_FIELDS) + (
    "player.weapon_state",
)

INCANTAGOS_BOSS_ENTRY = 61946
INCANTAGOS_OPENING_ADD_ENTRY = 59989
INCANTAGOS_MID_ADD_ENTRY = 59955
INCANTAGOS_RULE_SOURCE = (
    "user:incantagos-opening-adds-first-midfight-adds-switch-focus-one"
)


class UpperKaraBossEncounterContractError(ValueError):
    """A boss encounter cannot be represented without adding assumptions."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraBossEncounterContractError(f"{label} must be nonempty text")
    return value.strip()


@dataclass(frozen=True)
class AuthoritativeBossStagePlanV1:
    source_kind: str
    source_ref: str
    stages: tuple[TargetStageV1, ...]

    def __post_init__(self) -> None:
        source_kind = _text(self.source_kind, "source_kind")
        if source_kind not in OVERLAY_SOURCE_KINDS:
            raise UpperKaraBossEncounterContractError(
                "boss target-stage plan source is not authoritative"
            )
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_ref", _text(self.source_ref, "source_ref"))
        stages = tuple(self.stages)
        if not stages or any(not isinstance(row, TargetStageV1) for row in stages):
            raise TypeError("stages must contain TargetStageV1 values")
        if any(row.stage_role not in {"BOSS", "ADDS"} for row in stages):
            raise UpperKaraBossEncounterContractError(
                "boss encounter plan may contain only BOSS/ADDS stages"
            )
        object.__setattr__(self, "stages", stages)


@dataclass(frozen=True)
class BossEncounterSearchCellV1:
    """Auditable wrapper around the local contract and its state bindings."""

    target_rule_source_kind: str
    target_rule_source_ref: str
    entry_state_ref: str
    encounter_model_ref: str
    contract: UpperKaraWaveLocalSearchContractV1
    state_continuity: str = STATE_CONTINUITY
    required_continuous_state_fields: tuple[str, ...] = (
        REQUIRED_CONTINUOUS_STATE_FIELDS
    )
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise UpperKaraBossEncounterContractError("unexpected search-cell schema")
        source_kind = _text(
            self.target_rule_source_kind, "target_rule_source_kind"
        )
        if source_kind not in OVERLAY_SOURCE_KINDS:
            raise UpperKaraBossEncounterContractError(
                "target-rule source is not authoritative"
            )
        object.__setattr__(self, "target_rule_source_kind", source_kind)
        for field in (
            "target_rule_source_ref",
            "entry_state_ref",
            "encounter_model_ref",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if self.state_continuity != STATE_CONTINUITY:
            raise UpperKaraBossEncounterContractError(
                "boss target stages must preserve continuous combat state"
            )
        required = tuple(self.required_continuous_state_fields)
        if required != REQUIRED_CONTINUOUS_STATE_FIELDS:
            raise UpperKaraBossEncounterContractError(
                "continuous-state field contract differs from v1"
            )
        object.__setattr__(self, "required_continuous_state_fields", required)
        if not isinstance(self.contract, UpperKaraWaveLocalSearchContractV1):
            raise TypeError("contract must be UpperKaraWaveLocalSearchContractV1")
        if self.contract.search_unit.unit_kind != "BOSS_ENCOUNTER":
            raise UpperKaraBossEncounterContractError(
                "continuous boss cell requires a BOSS_ENCOUNTER search unit"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": self.schema,
            "target_rule_source_kind": self.target_rule_source_kind,
            "target_rule_source_ref": self.target_rule_source_ref,
            "entry_state_ref": self.entry_state_ref,
            "encounter_model_ref": self.encounter_model_ref,
            "state_continuity": self.state_continuity,
            "required_continuous_state_fields": list(
                self.required_continuous_state_fields
            ),
            "contract": self.contract.to_dict(),
        }


def boss_encounter_search_cell_from_dict_v1(
    value: object,
) -> BossEncounterSearchCellV1:
    if not isinstance(value, Mapping):
        raise UpperKaraBossEncounterContractError("search cell must be an object")
    row = dict(value)
    expected = {
        "schema",
        "target_rule_source_kind",
        "target_rule_source_ref",
        "entry_state_ref",
        "encounter_model_ref",
        "state_continuity",
        "required_continuous_state_fields",
        "contract",
    }
    if set(row) != expected:
        raise UpperKaraBossEncounterContractError(
            "search-cell fields differ from v1"
        )
    raw_fields = row["required_continuous_state_fields"]
    if not isinstance(raw_fields, list):
        raise UpperKaraBossEncounterContractError(
            "required_continuous_state_fields must be a list"
        )
    row["required_continuous_state_fields"] = tuple(raw_fields)
    row["contract"] = upper_kara_wave_local_search_contract_from_dict_v1(
        row["contract"]
    )
    return BossEncounterSearchCellV1(**row)


def _find_boss_encounter_v1(
    registry: Mapping[str, Any],
    *,
    instance_id: str,
    pull_ref: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    wanted_instance = _text(instance_id, "instance_id")
    wanted_pull = _text(pull_ref, "pull_ref")
    for instance in registry["instances"]:
        if instance["instance_id"] != wanted_instance:
            continue
        for encounter in instance["encounters"]:
            if encounter["pull_ref"] == wanted_pull:
                if encounter["metadata_boss_flag"] is not True:
                    raise UpperKaraBossEncounterContractError(
                        "selected pull is not a boss encounter"
                    )
                return instance, encounter
    raise UpperKaraBossEncounterContractError(
        "selected instance/pull was not found"
    )


def _ordered_target_bindings_v1(
    encounter: Mapping[str, Any],
    target_bindings: Mapping[str, WaveTargetBindingV1],
) -> tuple[WaveTargetBindingV1, ...]:
    encounter_targets = tuple(encounter["targets"])
    occurrence_ids = tuple(row["occurrence_id"] for row in encounter_targets)
    if not isinstance(target_bindings, Mapping):
        raise TypeError("target_bindings must be a mapping")
    if set(target_bindings) != set(occurrence_ids):
        raise UpperKaraBossEncounterContractError(
            "target_bindings must cover exactly every encounter target"
        )
    result: list[WaveTargetBindingV1] = []
    for target in encounter_targets:
        occurrence_id = target["occurrence_id"]
        binding = target_bindings[occurrence_id]
        if not isinstance(binding, WaveTargetBindingV1):
            raise TypeError("target_bindings values must be WaveTargetBindingV1")
        if binding.occurrence_id != occurrence_id:
            raise UpperKaraBossEncounterContractError(
                "target binding key and occurrence_id differ"
            )
        if binding.target_guid != target["target_guid"]:
            raise UpperKaraBossEncounterContractError(
                "target binding GUID differs from registry metadata"
            )
        if binding.identity_kind != "REGISTRY_CREATURE_OCCURRENCE":
            raise UpperKaraBossEncounterContractError(
                "registry encounter target binding has a nonregistry identity kind"
            )
        if binding.creature_entry_id != target["creature_entry_id"]:
            raise UpperKaraBossEncounterContractError(
                "target binding creature entry differs from registry metadata"
            )
        result.append(binding)
    return tuple(result)


def build_boss_encounter_search_cell_v1(
    registry: Mapping[str, Any],
    *,
    instance_id: str,
    pull_ref: str,
    campaign_id: str,
    frozen_player: FrozenPlayerIdentityV1,
    target_bindings: Mapping[str, WaveTargetBindingV1],
    stage_plan: AuthoritativeBossStagePlanV1,
    entry_state_ref: str,
    encounter_model_ref: str,
    variant_budget: int,
    baselines: Sequence[BaselineRefV1],
    train_seeds: SeedNamespaceV1,
    selection_seeds: SeedNamespaceV1,
    heldout_seeds: SeedNamespaceV1,
    resources: Sequence[ResourceRefV1],
) -> BossEncounterSearchCellV1:
    """Bind one whole boss pull without resetting at target-stage edges."""

    validate_route_wave_registry_v1(registry)
    if not isinstance(stage_plan, AuthoritativeBossStagePlanV1):
        raise TypeError("stage_plan must be AuthoritativeBossStagePlanV1")
    instance, encounter = _find_boss_encounter_v1(
        registry, instance_id=instance_id, pull_ref=pull_ref
    )
    ordered_bindings = _ordered_target_bindings_v1(encounter, target_bindings)
    resources_tuple = tuple(resources)
    contract = UpperKaraWaveLocalSearchContractV1(
        campaign_id=campaign_id,
        search_unit=WaveSearchUnitV1(
            unit_id=(
                f"{encounter['pull_slot_id']}:attempt-"
                f"{int(encounter['attempt_ordinal']):02d}"
            ),
            unit_kind="BOSS_ENCOUNTER",
            route_ref=instance["route_variant_id"],
            pull_or_phase_ref=encounter["pull_ref"],
        ),
        frozen_player=frozen_player,
        targets=ordered_bindings,
        target_stages=stage_plan.stages,
        variant_budget=variant_budget,
        baselines=tuple(baselines),
        train_seeds=train_seeds,
        selection_seeds=selection_seeds,
        heldout_seeds=heldout_seeds,
        resources=resources_tuple,
        resource_availability_cases=resource_availability_cases_v1(
            resources_tuple
        ),
    )
    return BossEncounterSearchCellV1(
        target_rule_source_kind=stage_plan.source_kind,
        target_rule_source_ref=stage_plan.source_ref,
        entry_state_ref=entry_state_ref,
        encounter_model_ref=encounter_model_ref,
        contract=contract,
    )


def incantagos_authoritative_stage_plan_v1(
    encounter: Mapping[str, Any],
) -> AuthoritativeBossStagePlanV1:
    """Map the user's Incantagos tactic to observable, causal target stages.

    The user-authoritative rule is: opening adds first, focus one add until it
    dies, attack the boss after the opening adds, switch the whole raid to the
    mid-fight adds, then return to the boss.  Metadata determines occurrence
    identity and deterministic enumeration order only; observed damage never
    grants target permission.  Boss collateral damage during add stages remains
    forbidden until a mechanic-authoritative source permits it.
    """

    if not isinstance(encounter, Mapping):
        raise TypeError("encounter must be a mapping")
    if encounter.get("metadata_boss_flag") is not True:
        raise UpperKaraBossEncounterContractError(
            "Incantagos plan requires a boss encounter"
        )
    if encounter.get("pull_outcome") != "KILL":
        raise UpperKaraBossEncounterContractError(
            "first Incantagos search cell requires a completed kill attempt"
        )
    by_entry: dict[int, list[Mapping[str, Any]]] = {}
    for target in encounter.get("targets", ()):
        entry = target.get("creature_entry_id")
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise UpperKaraBossEncounterContractError(
                "encounter target creature entry is invalid"
            )
        by_entry.setdefault(entry, []).append(target)
    expected_counts = {
        INCANTAGOS_BOSS_ENTRY: 1,
        INCANTAGOS_OPENING_ADD_ENTRY: 4,
        INCANTAGOS_MID_ADD_ENTRY: 6,
    }
    if {entry: len(rows) for entry, rows in by_entry.items()} != expected_counts:
        raise UpperKaraBossEncounterContractError(
            "Incantagos encounter target multiset is not 61946x1/59989x4/59955x6"
        )
    if any(
        target.get("death_right_censored") is True
        for rows in by_entry.values()
        for target in rows
    ):
        raise UpperKaraBossEncounterContractError(
            "first Incantagos search cell requires observed deaths for every target"
        )

    def ordered_ids(entry: int) -> tuple[str, ...]:
        rows = sorted(
            by_entry[entry],
            key=lambda row: (
                int(row["first_activity_offset_ms"]),
                str(row["occurrence_id"]),
            ),
        )
        return tuple(str(row["occurrence_id"]) for row in rows)

    boss = ordered_ids(INCANTAGOS_BOSS_ENTRY)[0]
    opening_adds = ordered_ids(INCANTAGOS_OPENING_ADD_ENTRY)
    mid_adds = ordered_ids(INCANTAGOS_MID_ADD_ENTRY)
    opening_dead = tuple(
        ObservableTargetConditionV1(target, "dead", True)
        for target in opening_adds
    )
    mid_visible = tuple(
        ObservableTargetConditionV1(target, "visible", True)
        for target in mid_adds
    )
    mid_dead = tuple(
        ObservableTargetConditionV1(target, "dead", True) for target in mid_adds
    )
    stages = (
        TargetStageV1(
            "incantagos-opening-adds",
            "ADDS",
            "FOCUS_ONE_UNTIL_DEAD",
            opening_adds,
            opening_adds,
            "ALL",
            opening_dead,
            "incantagos-boss-main",
        ),
        TargetStageV1(
            "incantagos-boss-main",
            "BOSS",
            "ANY_LEGAL",
            (boss,),
            (boss,),
            "ANY",
            mid_visible,
            "incantagos-mid-adds",
        ),
        TargetStageV1(
            "incantagos-mid-adds",
            "ADDS",
            "FOCUS_ONE_UNTIL_DEAD",
            mid_adds,
            mid_adds,
            "ALL",
            mid_dead,
            "incantagos-boss-finish",
        ),
        TargetStageV1(
            "incantagos-boss-finish",
            "BOSS",
            "ANY_LEGAL",
            (boss,),
            (boss,),
        ),
    )
    return AuthoritativeBossStagePlanV1(
        source_kind="USER_AUTHORITATIVE",
        source_ref=INCANTAGOS_RULE_SOURCE,
        stages=stages,
    )


def build_incantagos_clean_attempt_search_cell_v1(
    registry: Mapping[str, Any],
    **kwargs: Any,
) -> BossEncounterSearchCellV1:
    """Build the first real Incantagos cell after validating its exact shape."""

    validate_route_wave_registry_v1(registry)
    instance_id = _text(kwargs.get("instance_id"), "instance_id")
    pull_ref = _text(kwargs.get("pull_ref"), "pull_ref")
    _, encounter = _find_boss_encounter_v1(
        registry, instance_id=instance_id, pull_ref=pull_ref
    )
    stage_plan = incantagos_authoritative_stage_plan_v1(encounter)
    forwarded = dict(kwargs)
    forwarded["instance_id"] = instance_id
    forwarded["pull_ref"] = pull_ref
    forwarded["stage_plan"] = stage_plan
    return build_boss_encounter_search_cell_v1(registry, **forwarded)


__all__ = (
    "AuthoritativeBossStagePlanV1",
    "BossEncounterSearchCellV1",
    "INCANTAGOS_BOSS_ENTRY",
    "INCANTAGOS_MID_ADD_ENTRY",
    "INCANTAGOS_OPENING_ADD_ENTRY",
    "INCANTAGOS_RULE_SOURCE",
    "REQUIRED_CONTINUOUS_STATE_FIELDS",
    "SCHEMA",
    "STATE_CONTINUITY",
    "UpperKaraBossEncounterContractError",
    "boss_encounter_search_cell_from_dict_v1",
    "build_boss_encounter_search_cell_v1",
    "build_incantagos_clean_attempt_search_cell_v1",
    "incantagos_authoritative_stage_plan_v1",
)
