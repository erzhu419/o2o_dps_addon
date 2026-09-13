"""One Upper Kara model-defined whole-wave case, separate from exact replay.

The source log selects a one-target wave and supplies a *kill-budget proxy*.
The reset, team process, and watchdog below are declared experiment inputs;
they do not purport to recover the historical pull's initial state.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

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
from .fury_paired_multiseed_runner_v2 import sha256_json
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = PROJECT_ROOT / "configs/wowsims/fury_warrior_live.json"
EQUIPPED_NAMES_PATH = (
    PROJECT_ROOT / "configs/wowsims/fury_warrior_live.equipped_names.json"
)

SCHEMA = "development_wave_case/v1"
INSTANCE_ID = "88cc0f66-9243-4b88-a58b-0bb8c978a6fd"
WAVE_ID = "86b561f2-1428-4fa4-b9c8-287f351c5fd0:wave:1"
TARGET_GUID = "0xF13000F1F8276B9F"
TARGET_HP_HYPOTHESIS = 126_397
TARGET_ARMOR_HYPOTHESIS = 1_721
WATCHDOG_MS = 30_000
TEAM_HIT_INTERVAL_MS = 500
TEAM_DAMAGE_PER_HIT = 6_500.0
SOURCE_CAPSULE_SHA256 = (
    "4d0711570c4c4f4000e68676ab62f7625eae83d4850b3f86097b376d25330b3b"
)
SOURCE_CAPSULE_BUNDLE_SHA256 = (
    "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e"
)
SOURCE_SCENARIO_SHA256 = (
    "4eca45e1601e8add938d5f417b0261d7a258543c9111714805e9a950eb98a693"
)
CORPUS_ENTRY_SHA256 = (
    "28ab7c59f1f6752ff3eb1661ddcc4707a90b0eca1f094f9ce2b812a33af69dfc"
)
CATALOG_SHA256 = (
    "5f22af66c2cb29b8259b9bc9cebfe2e0e757cc7f45e6a9c8cde72722c092ca40"
)


@dataclass(frozen=True)
class DevelopmentWaveCaseV1:
    case_spec: dict[str, Any]
    request: dict[str, Any]
    dynamic_load: DynamicRolloutLoadV3
    target_contexts: dict[int, TargetSemanticsContextV3]


def _hypothesis(label: str) -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
        corpus_sha256=SOURCE_CAPSULE_SHA256,
        hypothesis_id=label,
    )


def build_development_wave_case_v1(seed: int) -> DevelopmentWaveCaseV1:
    """Pin one complete simulation with a common reset for every policy lane."""

    request = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    request = deepcopy(request)
    encounter = request["encounter"]
    encounter["duration"] = WATCHDOG_MS / 1000
    encounter["durationVariation"] = 0
    encounter["useHealth"] = True
    target = encounter["targets"][0]
    target["name"] = "Upper Kara creature 61944 (model)"
    target["level"] = 60
    target["mobType"] = "MobTypeUnknown"
    target["minBaseDamage"] = 0
    target["damageSpread"] = 0
    target["parryHaste"] = False
    target["stats"][26] = TARGET_ARMOR_HYPOTHESIS
    target["stats"][34] = TARGET_HP_HYPOTHESIS
    player = request["raid"]["parties"][0]["players"][0]
    player["warrior"]["options"]["startingRage"] = 50
    request["simOptions"].update(
        {"iterations": 1, "interactive": True}
    )
    # The native load command binds each paired group's seed separately.
    request["simOptions"].pop("randomSeed", None)

    config = DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, TARGET_HP_HYPOTHESIS),),
        idle_advance_horizon_ms=WATCHDOG_MS,
        background_damage_events=tuple(
            BackgroundDamageEventV1(
                schedule_index=index,
                time_ms=TEAM_HIT_INTERVAL_MS * (index + 1),
                target_index=0,
                event_id=f"model-team-{index:03d}",
                damage=TEAM_DAMAGE_PER_HIT,
            )
            for index in range(WATCHDOG_MS // TEAM_HIT_INTERVAL_MS)
        ),
    )
    load = DynamicRolloutLoadV3.bind(request, seed, config)
    equipped = json.loads(EQUIPPED_NAMES_PATH.read_text(encoding="utf-8"))[
        "equipped_item_names"
    ]
    context = TargetSemanticsContextV3(
        context_id="upper-kara-61944-controlled-reset-v1",
        mode=TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
        target_index=0,
        target_classification=ContraTargetClassificationV2.ELITE,
        target_name=target["name"],
        equipped_item_names=tuple(equipped),
        target_classification_evidence=_hypothesis("creature61944-elite-assumed"),
        target_name_evidence=_hypothesis("creature61944-label-assumed"),
        equipment_evidence=ContraFieldEvidenceV2(
            ContraEvidenceKindV2.PINNED_STATIC_INPUT,
            source_sha256=sha256_json(request),
        ),
        target_position_evidence=_hypothesis("single-target-melee-range"),
        target_health_pct_evidence=_hypothesis("kill-budget-hp-126397"),
        target_max_health_evidence=_hypothesis("kill-budget-hp-126397"),
        target_max_health=TARGET_HP_HYPOTHESIS,
    )
    spec = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT",
        "historical_exact": False,
        "real_superiority_authorized": False,
        "deployment_authorized": False,
        "source_cohort": "old50/main_comparison/1197-wave-capsule-v2",
        "source_instance_name": "Upper Tower of Karazhan",
        "source_instance_id": INSTANCE_ID,
        "source_instance_slug": "DzMCldka_9sethFu",
        "source_raid_date": "2026-08-25",
        "source_wave_ref": WAVE_ID,
        "source_target_ref": TARGET_GUID,
        "source_wave_model_target_count": 1,
        "source_build_ref": None,
        "build_ref": "configs/wowsims/fury_warrior_live.json",
        "build_role": "CONTROLLED_LIVE_PROFILE_NOT_SOURCE_PLAYER",
        "character": {
            "name": player["name"], "class": player["class"],
            "race": player["race"], "talents_string": player["talentsString"],
        },
        "equipment_items": deepcopy(player["equipment"]["items"]),
        "skills_source": "NATIVE_WOWSIMS_TURTLE_WARRIOR_ACTION_SET_AT_RUNTIME",
        "consumable_inventory": "NONE_CONFIGURED",
        "historical_player_policy_used": False,
        "source_evidence": {
            "queue_entry": "offline_data/chronicle_raw/export_queue.json",
            "wave_capsule": (
                "offline_data/derived/fury_offline_scenario_capsules/v2/"
                "fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
            ),
            "wave_capsule_sha256": SOURCE_CAPSULE_SHA256,
            "wave_capsule_bundle_sha256": SOURCE_CAPSULE_BUNDLE_SHA256,
            "source_csv": (
                "offline_data/chronicle_raw/20260828T095121873271Z/"
                "all-activity-88cc0f66-9243-4b88-a58b-0bb8c978a6fd.csv"
            ),
            "death_csv_line": 112538,
            "observed_incoming_damage_to_death": TARGET_HP_HYPOTHESIS,
            "hp_interpretation": "MODEL_HP_FROM_COMPLETE_NORMALIZED_KILL_BUDGET_NOT_EXACT_MAX_HP",
        },
        "initial_state_mode": "CONTROLLED_RESET",
        "initial_state": {
            "target_max_hp": TARGET_HP_HYPOTHESIS,
            "target_current_hp": TARGET_HP_HYPOTHESIS,
            "target_base_armor": TARGET_ARMOR_HYPOTHESIS,
            "target_level": 60,
            "target_classification": "elite_assumed",
            "target_attackable_at_ms": 0,
            "target_placement": "single_target_melee_range",
            "rage": 50,
            "stance": "WarriorStanceBerserker",
            "queue": "empty",
            "cooldowns": "reset",
            "proc_auras": "reset",
            "raid_buffs": "none_configured",
            "consumes": "none_configured",
            "target_autoattack_damage": 0,
        },
        "team_background": {
            "model": "EXOGENOUS_CONSTANT_DAMAGE_EVENTS",
            "branch": "low_team_13k_development_sensitivity",
            "focal_damage_excluded": True,
            "damage_per_500ms": TEAM_DAMAGE_PER_HIT,
            "team_dps_assumed": 13_000,
            "source_total_damage_divided_by_activity_span_dps": 20_556.0,
            "source_total_rate_is_not_focal_excluded_team_dps": True,
            "future_schedule_policy_visible": False,
        },
        "policy_observation": {
            "projection": "NATIVE_DYNAMIC_V3_CURRENT_STATE_ONLY",
            "target_current_hp_and_percent": "live_simulator_state_at_decision",
            "source_death_or_future_team_events": "excluded",
            "simulator_seed_or_rng_future": "excluded",
        },
        "required_target_ids": [TARGET_GUID],
        "required_target_indices": [0],
        "terminal": {
            "success": "ALL_REQUIRED_TARGETS_DEAD_CONFIRMED_BY_RECEIPT_AND_FINAL_STATE",
            "watchdog_ms": WATCHDOG_MS,
            "watchdog_outcome": "CENSORED_WATCHDOG_NOT_COMPLETE",
        },
        "seed": seed,
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    }
    return DevelopmentWaveCaseV1(spec, request, load, {0: context})


def build_development_wave_scenario_v1(seed: int) -> dict[str, Any]:
    """Project the case into the existing runner-v4 scenario wire shape."""

    case = build_development_wave_case_v1(seed)
    context = case.target_contexts[0]
    request_sha = case.dynamic_load.request_sha256
    context_wire = {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": context.target_index,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "equipped_item_names": list(context.equipped_item_names),
        "target_max_health": context.target_max_health,
        "health_pct_schedule": [],
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        },
    }
    return {
        "instance_id": INSTANCE_ID,
        "component_id": "leakage-fe55a732014134fc",
        "scenario_id": "upper-kara-61944-controlled-reset-low-team-13k",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": WATCHDOG_MS,
        "estimated_cost_units": WATCHDOG_MS,
        "request": case.request,
        "dynamic_load_config": case.dynamic_load.config.to_wire(),
        "scenario_model": {
            "schema": "fury_dynamic_v5_scenario_model/v4",
            "status": "MODEL_DEFINED_DEVELOPMENT",
            "request_sha256": request_sha,
            "bridge_execution_eligible": True,
            "historical_truth": False,
            "comparison_eligible": False,
            "limitation_codes": [
                "KILL_BUDGET_HP_MODEL_NOT_EXACT",
                "EXOGENOUS_TEAM_13K_DPS_ASSUMED",
                "WATCHDOG_IS_CENSOR_NOT_WAVE_SUCCESS",
            ],
        },
        "target_context_bundle": {
            "schema": "fury_dynamic_v5_target_context_bundle/v4",
            "status": "POLICY_CONTEXT_BOUND",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [context_wire],
            "bridge_execution_eligible": True,
            "comparison_eligible": False,
            "limitation_codes": ["SIMULATOR_HYPOTHESIS"],
        },
        "corpus_entry_sha256": CORPUS_ENTRY_SHA256,
        "source_scenario_sha256": SOURCE_SCENARIO_SHA256,
        "catalog_sha256": CATALOG_SHA256,
    }


def adjudicate_development_wave_completion_v1(
    case: DevelopmentWaveCaseV1, rollout: Mapping[str, Any]
) -> dict[str, Any]:
    """Adjudicate either a Cat/Contra artifact or a runner-v4 lane result."""

    artifact = rollout.get("artifact", rollout)
    if not isinstance(artifact, Mapping):
        return {"status": "FAILED_MISSING_TERMINAL_EVIDENCE"}
    receipt = artifact.get("configured_completion")
    lifecycle = artifact.get("lifecycle_receipt")
    if isinstance(receipt, Mapping):
        final_state = artifact.get("final_state")
        reason = receipt.get("terminal_reason")
        complete_receipt = (
            reason == "ALL_TARGETS_DEAD"
            and receipt.get("all_targets_dead") is True
            and receipt.get("criterion_met") is True
            and artifact.get("scenario_complete") is True
        )
        elapsed = rollout.get("elapsed_ms", artifact.get("elapsed_ms"))
        damage = rollout.get("damage", artifact.get("damage_delta"))
    elif isinstance(lifecycle, Mapping):
        final_receipt = lifecycle.get("final_state")
        final_state = (
            final_receipt.get("state")
            if isinstance(final_receipt, Mapping)
            else None
        )
        reason = rollout.get("completion_mode")
        complete_receipt = (
            reason == "ALL_TARGETS_DEAD"
            and lifecycle.get("terminal_reason") == "ENCOUNTER_FINISHED"
            and rollout.get("completion_criterion_met") is True
            and isinstance(final_state, Mapping)
            and final_state.get("finished") is True
        )
        elapsed = rollout.get("elapsed_ms")
        damage = rollout.get("damage")
    else:
        return {"status": "FAILED_MISSING_TERMINAL_EVIDENCE"}
    if not isinstance(final_state, Mapping):
        return {"status": "FAILED_MISSING_TERMINAL_EVIDENCE"}
    lane_mode = rollout.get("completion_mode")
    if lane_mode is not None and lane_mode != reason:
        return {"status": "FAILED_TERMINAL_INCONSISTENT_OR_INCOMPLETE"}
    team = final_state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required_indices = case.case_spec["required_target_indices"]
    dead = (
        isinstance(targets, list)
        and all(
            index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required_indices
        )
    )
    if reason == "SCENARIO_HORIZON_REACHED":
        status = "CENSORED_WATCHDOG"
    elif complete_receipt and dead:
        status = "COMPLETED"
    else:
        status = "FAILED_TERMINAL_INCONSISTENT_OR_INCOMPLETE"
    return {
        "status": status,
        "terminal_reason": reason,
        "required_target_ids": case.case_spec["required_target_ids"],
        "required_targets_dead": bool(dead),
        "elapsed_ms": elapsed,
        "own_effective_damage": (
            team.get("simulated_damage_applied") if isinstance(team, Mapping) else None
        ),
        "own_reported_damage": damage,
        "background_effective_damage": (
            team.get("background_damage_applied") if isinstance(team, Mapping) else None
        ),
        "target_outcomes": [
            {
                "target_index": target.get("target_index"),
                "death_time_ms": target.get("death_time_ms"),
                "simulated_damage_applied": target.get("simulated_damage_applied"),
                "background_damage_applied": target.get("background_damage_applied"),
                "dead": target.get("dead"),
            }
            for target in targets
        ] if isinstance(targets, list) else None,
    }
