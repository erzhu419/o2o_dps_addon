"""Run an exact historical Warrior build in a declared development wave.

The build is source-bound, but the wave reset is not a reconstruction of that
player's pull.  Character-owned fields come only from the catalogue adapter;
raid, encounter, and execution fields are independently declared here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from .build_request_composer_v1 import (
    EncounterModel,
    ExecutionModel,
    Objective,
    RaidContext,
    compose_build_request_v1,
)
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    PROJECT_ROOT,
    build_development_wave_case_v1,
    build_development_wave_scenario_v1,
)
from .fury_contra_adapter_v2 import ContraEvidenceKindV2, ContraFieldEvidenceV2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import sha256_json
from .historical_representative_character_profile_v1 import (
    load_historical_representative_character_profile,
)


SCHEMA = "development_historical_build_wave_case/v1"
DEFAULT_ITEM_DATABASE = PROJECT_ROOT.parent / "wowsims-turtle/assets/database/db.json"

# The source-derived Contra ZSSDW feature in this panel checks these six
# Brotherhood-set names. Other item names are for trace context.
_CONTRA_BROTHERHOOD_NAMES = {
    47270: "兄弟会头盔",
    47271: "兄弟会肩甲",
    47272: "兄弟会胸甲",
    47273: "兄弟会护腿",
    47274: "兄弟会胫甲",
    47275: "兄弟会项链",
}


def _equipment_names(items: list[dict[str, Any]], database_path: Path) -> tuple[str, ...]:
    database = json.loads(database_path.read_text(encoding="utf-8"))
    names_by_id = {
        int(row["id"]): str(row["name"])
        for row in database["items"]
        if isinstance(row, dict) and isinstance(row.get("id"), int)
        and isinstance(row.get("name"), str) and row["name"]
    }
    names: list[str] = []
    for index, item in enumerate(items):
        if item == {}:  # observed empty simulator slot, e.g. two-hand offhand
            continue
        item_id = item.get("id")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise ValueError(f"equipment.items[{index}] has no integer item id")
        if item_id <= 0:
            continue
        if item_id not in names_by_id:
            raise ValueError(f"item {item_id} has no name in the pinned simulator database")
        names.append(_CONTRA_BROTHERHOOD_NAMES.get(item_id, names_by_id[item_id]))
    return tuple(names)


def build_historical_representative_development_wave_case_v1(
    seed: int,
    *,
    rank: int = 7,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
) -> DevelopmentWaveCaseV1:
    """Import one verified representative without inheriting live-player state."""

    environment = build_development_wave_case_v1(seed)
    character = load_historical_representative_character_profile(
        rank=rank, consumes={}, database={},
    )
    learned_talents = character.selection.record["state"]["talents"]
    if not any(
        talent.get("name") == "bloodthirst" and talent.get("rank") == 1
        for talent in learned_talents
    ):
        raise ValueError(
            f"historical build rank {rank} lacks Bloodthirst; both pinned "
            "Contra Fury source lanes attempt this unlearned spell at decision 0, "
            "so this four-lane panel is not eligible"
        )
    composition = compose_build_request_v1(
        character,
        RaidContext(
            individual_buffs={}, party_buffs={}, raid_buffs={}, debuffs={},
            additional_party_players=[], additional_parties=[],
            raid_options={"numActiveParties": 1},
            provenance={"source": SCHEMA, "mode": "NO_RAID_BUFFS_ASSUMED"},
        ),
        EncounterModel(
            request_fields=deepcopy(environment.request["encounter"]),
            provenance={"source": SCHEMA, "wave_ref": environment.case_spec["source_wave_ref"]},
        ),
        ExecutionModel(
            rotation={}, cooldowns={},
            warrior_options={"startingRage": 50, "stance": "WarriorStanceBerserker"},
            reaction_time_ms=150, channel_clip_delay_ms=0,
            in_front_of_target=False, distance_from_target=5,
            provenance={"source": SCHEMA, "mode": "CONTROLLED_RESET"},
        ),
        Objective(
            kind="MODEL_DEFINED_WAVE_EFFECTIVE_DAMAGE",
            sim_options={"iterations": 1, "randomSeed": str(seed), "interactive": True},
            provenance={"source": SCHEMA},
        ),
    )
    if not composition.admitted:
        blockers = composition.audit["admission"]["blockers"]
        raise ValueError(f"historical build rank {rank} is not simulator-admitted: {blockers}")
    assert composition.request is not None
    request = deepcopy(composition.request)
    request["simOptions"].pop("randomSeed")  # dynamic load binds paired seed
    load = DynamicRolloutLoadV3.bind(request, seed, environment.dynamic_load.config)
    player = request["raid"]["parties"][0]["players"][0]
    offhand_item = player["equipment"]["items"][15]
    dual_wield = isinstance(offhand_item, dict) and isinstance(offhand_item.get("id"), int) and offhand_item["id"] > 0
    equipped_names = _equipment_names(
        player["equipment"]["items"], item_database_path
    )
    source_identity = deepcopy(character.provenance["source_identity"])
    catalog_path = character.provenance["catalog_path"]
    catalog_line = character.provenance["catalog_line_number"]
    context = replace(
        environment.target_contexts[0],
        context_id=f"upper-kara-61944-historical-build-rank-{rank}-controlled-reset-v1",
        equipped_item_names=equipped_names,
        equipment_evidence=ContraFieldEvidenceV2(
            ContraEvidenceKindV2.PINNED_STATIC_INPUT,
            source_sha256=sha256_json(request),
        ),
    )
    spec = deepcopy(environment.case_spec)
    spec.update({
        "schema": SCHEMA,
        "source_build_ref": source_identity,
        "build_ref": f"{catalog_path}:{catalog_line}",
        "build_role": "EXACT_HISTORICAL_BUILD_TRANSPLANTED_TO_MODEL_WAVE",
        "character": {
            "name": player["name"], "class": player["class"],
            "race": player["race"], "talents_string": player["talentsString"],
        },
        "equipment_items": deepcopy(player["equipment"]["items"]),
        "consumable_inventory": "NOT_OBSERVED; NONE_CONFIGURED_FOR_MODEL",
        "historical_player_policy_used": False,
        "baseline_fidelity": {
            "deployed_contra_source_branch": "RAID_A_ONLY",
            "weapon_mode": "DUAL_WIELD" if dual_wield else "TWO_HAND",
            "deployed_contra_build_interpretation": (
                "RAID_A_BEHAVIOR_TRANSPLANT_ON_DUAL_WIELD_BUILD"
                if dual_wield else "RAID_A_TWO_HAND_BUILD_CANDIDATE"
            ),
            "source_faithful_four_way_eligible": False if dual_wield else "NOT_YET_ASSESSED",
        },
        "historical_build": {
            "representative_rank": rank,
            "catalog_line_number": catalog_line,
            "source_identity": source_identity,
            "composer_admission": composition.audit["admission"],
            "composer_residual_audit": composition.audit["residual_audit"],
            "composer_equipment_coverage": composition.audit["coverage"]["equipment"],
            "composer_talent_coverage": composition.audit["coverage"]["talents"],
            "consumes_observed_in_historical_source": False,
            "skills_observed_in_historical_source": False,
            "inventory_observed_in_historical_source": False,
            "item_names_source": str(item_database_path),
            "brotherhood_names_localized_for_contra": True,
        },
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    })
    spec["initial_state"]["consumes"] = "not_observed_none_configured"
    return DevelopmentWaveCaseV1(spec, request, load, {0: context})


def build_historical_representative_development_wave_scenario_v1(
    seed: int,
    *,
    rank: int = 7,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
) -> dict[str, Any]:
    """Bind the imported build to the existing runner-v4 whole-wave wire."""

    case = build_historical_representative_development_wave_case_v1(
        seed, rank=rank, item_database_path=item_database_path,
    )
    scenario = build_development_wave_scenario_v1(seed)
    scenario["scenario_id"] = f"upper-kara-61944-historical-build-rank-{rank}-model"
    scenario["request"] = case.request
    scenario["dynamic_load_config"] = case.dynamic_load.config.to_wire()
    scenario["scenario_model"]["request_sha256"] = case.dynamic_load.request_sha256
    scenario["scenario_model"]["limitation_codes"].extend([
        "HISTORICAL_BUILD_TRANSPLANTED_TO_OTHER_RAID_MODEL",
        "HISTORICAL_CONSUMES_SKILLS_INVENTORY_NOT_OBSERVED",
    ])
    if case.case_spec["baseline_fidelity"]["weapon_mode"] == "DUAL_WIELD":
        scenario["scenario_model"]["limitation_codes"].append(
            "RAID_A_BEHAVIOR_TRANSPLANT_ON_DUAL_WIELD_BUILD"
        )
    bundle = scenario["target_context_bundle"]
    bundle["request_sha256"] = case.dynamic_load.request_sha256
    context = case.target_contexts[0]
    row = bundle["contexts"][0]
    row["context_id"] = context.context_id
    row["equipped_item_names"] = list(context.equipped_item_names)
    row["field_evidence"]["equipped_item_names"] = context.equipment_evidence.to_dict()
    return scenario


__all__ = [
    "SCHEMA",
    "build_historical_representative_development_wave_case_v1",
    "build_historical_representative_development_wave_scenario_v1",
]
