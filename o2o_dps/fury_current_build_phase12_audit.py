"""Fail-closed collection audit for the Phase-12 Fury dummy campaign.

Phase 12 deliberately combines already retained Phase 1-11 evidence with a
single current-build dummy campaign.  Mechanisms outside that environment are
reported as external boundaries, never as collection gates.  This audit never
fits a simulator formula and never authorizes a simulator patch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE12_CAMPAIGN_ID = "warrior_fury_current_build_dummy_phase12"
PHASE12_REPAIR_CAMPAIGN_ID = (
    "warrior_fury_current_build_dummy_phase12_repair_v2"
)
PHASE12_ANALYZER = "fury_current_build_phase12_v2"
DEFAULT_PREREGISTRATION = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_preregistration.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_audit.json"
)
DEFAULT_HISTORY_DIRECTORY = (
    PROJECT_ROOT / "offline_data" / "calibration_summaries"
)
MECHANISM_GATE_NAMES = (
    "white_rage",
    "dual_wield",
    "action_damage_avoidance",
    "queue",
    "flurry_deep_wounds",
    "burst_stance",
    "movement_range",
    "restore",
)
EXTERNAL_MECHANISM_BOUNDARY_NAMES = (
    "multi_target",
    "incoming_rage",
    "execute_low_health",
    "queue_target_switch",
    "armor_floor",
)
PHASE12_TASK_ORDER = (
    "warrior_white_swing_rage_bridge_sword_phase12",
    "warrior_white_swing_rage_bridge_axe_phase12",
    "warrior_restore_calibration_weapon",
    "warrior_fury_dual_wield_mechanics_phase12",
    "warrior_fury_action_damage_avoidance_phase12",
    "warrior_fury_queue_execution_phase12",
    "warrior_fury_flurry_deep_wounds_phase12",
    "warrior_fury_movement_range_latency_phase12",
    "warrior_fury_self_buffs_stances_phase12",
    "warrior_fury_restore_loadout_phase12",
)
PHASE12_REPAIR_TASK_ORDER = (
    "warrior_white_swing_rage_bridge_sword_phase12_repair_v2",
    "warrior_white_swing_rage_bridge_axe_phase12_repair_v2",
    "warrior_restore_calibration_weapon_phase12_repair_v2",
    "warrior_fury_dual_wield_mechanics_phase12_repair_v2",
    "warrior_fury_action_damage_avoidance_phase12_repair_v2",
    "warrior_fury_queue_execution_phase12_repair_v2",
    "warrior_fury_movement_range_latency_phase12_repair_v2",
    "warrior_fury_restore_loadout_phase12_repair_v2",
)
PHASE12_REPAIR_TASK_BASE_IDS = {
    repair_id: base_id
    for repair_id, base_id in zip(
        PHASE12_REPAIR_TASK_ORDER,
        (
            PHASE12_TASK_ORDER[0],
            PHASE12_TASK_ORDER[1],
            PHASE12_TASK_ORDER[2],
            PHASE12_TASK_ORDER[3],
            PHASE12_TASK_ORDER[4],
            PHASE12_TASK_ORDER[5],
            PHASE12_TASK_ORDER[7],
            PHASE12_TASK_ORDER[9],
        ),
    )
}


class FuryPhase12AuditError(ValueError):
    """The Phase-12 inputs do not satisfy the audit's structural contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryPhase12AuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(document, dict):
        raise FuryPhase12AuditError(f"{label} must be a JSON object: {path}")
    return document


def validate_preregistration(document: dict[str, Any]) -> None:
    """Validate fields that materially determine the Phase-12 audit."""

    if document.get("schema_version") != 1:
        raise FuryPhase12AuditError("Phase-12 preregistration schema_version must be 1")
    if document.get("kind") != "fury_current_build_phase12_preregistration":
        raise FuryPhase12AuditError("unexpected Phase-12 preregistration kind")
    if document.get("campaign_id") != PHASE12_CAMPAIGN_ID:
        raise FuryPhase12AuditError("Phase-12 preregistration campaign_id drifted")
    if document.get("analyzer") != PHASE12_ANALYZER:
        raise FuryPhase12AuditError("Phase-12 preregistration analyzer drifted")

    scope = document.get("scope")
    if not isinstance(scope, dict):
        raise FuryPhase12AuditError("Phase-12 preregistration lacks scope")
    if scope.get("talent_respec_permitted") is not False:
        raise FuryPhase12AuditError("Phase-12 must remain a no-respec campaign")
    if scope.get("consumable_use_permitted") is not False:
        raise FuryPhase12AuditError("Phase-12 must not consume potions or other items")

    marker_contract = document.get("marker_contract")
    if not isinstance(marker_contract, dict):
        raise FuryPhase12AuditError("Phase-12 preregistration lacks marker_contract")
    if marker_contract.get("required_identity_fields") != [
        "campaignId",
        "campaignRunId",
        "taskId",
        "completionKind",
    ]:
        raise FuryPhase12AuditError(
            "Phase-12 marker identity must match fields emitted by MarkerDetails"
        )
    role_fields = marker_contract.get("bridge_role_fields")
    if role_fields != {
        "role": "bridgeModelUse",
        "identification_value": "identification",
        "internal_holdout_value": "internal_holdout",
        "fit_permission": "fitPermitted",
        "holdout_fit_permission": "holdoutFitPermitted",
    }:
        raise FuryPhase12AuditError("Phase-12 bridge role marker contract drifted")
    if tuple(document.get("campaign_task_order", ())) != PHASE12_TASK_ORDER:
        raise FuryPhase12AuditError("Phase-12 campaign task order drifted from the addon")

    gates = document.get("mechanism_gates")
    if not isinstance(gates, dict) or tuple(gates) != MECHANISM_GATE_NAMES:
        raise FuryPhase12AuditError(
            "Phase-12 mechanism gates must be present in their preregistered order"
        )
    for name, gate in gates.items():
        if not isinstance(gate, dict):
            raise FuryPhase12AuditError(f"mechanism gate {name} must be an object")
        minimum = gate.get("minimum_completed_current_tasks")
        if type(minimum) is not int or minimum < 0:
            raise FuryPhase12AuditError(
                f"mechanism gate {name} has an invalid current-task minimum"
            )
        if type(gate.get("deferred_allowed")) is not bool:
            raise FuryPhase12AuditError(
                f"mechanism gate {name} must declare deferred_allowed"
            )
        if not isinstance(gate.get("match"), dict):
            raise FuryPhase12AuditError(f"mechanism gate {name} lacks match rules")
        if not isinstance(gate.get("reuse_task_ids"), list):
            raise FuryPhase12AuditError(
                f"mechanism gate {name} lacks reuse_task_ids"
            )
        expected = gate.get("expected_task_ids")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(task_id, str) or not task_id for task_id in expected)
        ):
            raise FuryPhase12AuditError(
                f"mechanism gate {name} lacks exact expected_task_ids"
            )
        if minimum > len(expected):
            raise FuryPhase12AuditError(
                f"mechanism gate {name} minimum exceeds expected task count"
            )

    expected_union = {
        task_id
        for gate in gates.values()
        for task_id in gate["expected_task_ids"]
    }
    if expected_union != set(PHASE12_TASK_ORDER):
        raise FuryPhase12AuditError(
            "Phase-12 gate expected_task_ids do not cover the exact campaign"
        )
    if gates["restore"]["minimum_completed_current_tasks"] != 2:
        raise FuryPhase12AuditError("Phase-12 must observe both restoration tasks")

    boundaries = document.get("external_mechanism_boundaries")
    if (
        not isinstance(boundaries, dict)
        or tuple(boundaries) != EXTERNAL_MECHANISM_BOUNDARY_NAMES
    ):
        raise FuryPhase12AuditError(
            "Phase-12 external mechanism boundaries must be present in their "
            "preregistered order"
        )
    for name, boundary in boundaries.items():
        if not isinstance(boundary, dict):
            raise FuryPhase12AuditError(
                f"external mechanism boundary {name} must be an object"
            )
        if boundary.get("collection_complete_contribution") is not False:
            raise FuryPhase12AuditError(
                f"external mechanism boundary {name} must not enter collection_complete"
            )
        if boundary.get("simulator_patch_allowed") is not False:
            raise FuryPhase12AuditError(
                f"external mechanism boundary {name} must not authorize a simulator patch"
            )

    multi_target = boundaries["multi_target"]
    observations = multi_target.get("chronicle_observations")
    whirlwind = observations.get("whirlwind") if isinstance(observations, dict) else None
    cleave = observations.get("cleave") if isinstance(observations, dict) else None
    if (
        multi_target.get("status") != "PARTIAL"
        or multi_target.get("simulator_calibration_status") != "not_calibrated"
        or multi_target.get("expert_policy_sources") != ["Cat", "Cat2", "Contra"]
        or multi_target.get("candidate_policy_sources") != ["Contra_new"]
        or not isinstance(whirlwind, dict)
        or whirlwind.get("damage_target_group_counts")
        != {"1": 101, "2": 44, "3": 13, "4": 2}
        or whirlwind.get("max_observed_targets") != 4
        or not isinstance(cleave, dict)
        or cleave.get("damage_target_group_counts") != {"1": 44, "2": 22}
        or cleave.get("max_observed_targets") != 2
    ):
        raise FuryPhase12AuditError(
            "multi-target boundary must remain partial observational/policy evidence"
        )
    incoming = boundaries["incoming_rage"]
    if (
        incoming.get("status") != "UNRESOLVED"
        or incoming.get("evidence_roles") != ["expert_action_prior_only"]
        or incoming.get("simulator_formula_status") != "unresolved"
        or incoming.get("simulator_calibration_status") != "not_calibrated"
        or incoming.get("expert_policy_sources") != ["Cat", "Cat2", "Contra"]
        or incoming.get("candidate_policy_sources") != ["Contra_new"]
        or "IsTargetOfTargetMeor"
        not in str(incoming.get("candidate_policy_limitation", ""))
    ):
        raise FuryPhase12AuditError(
            "incoming-rage boundary must remain unresolved expert-only evidence"
        )
    execute = boundaries["execute_low_health"]
    if (
        execute.get("status") != "REUSED"
        or execute.get("reuse_task_id") != "warrior_execute_transition"
        or execute.get("current_campaign_retest") is not False
    ):
        raise FuryPhase12AuditError(
            "execute low-health boundary must reuse warrior_execute_transition"
        )
    queue_switch = boundaries["queue_target_switch"]
    if (
        queue_switch.get("status") != "EXPERT_PROVENANCE"
        or queue_switch.get("expert_policy_sources") != ["Cat"]
        or queue_switch.get("cat_policy_rule")
        != "clear_or_reselect_cancels_queued_next_swing"
        or queue_switch.get("preserve_on_target_switch_assumed") is not False
    ):
        raise FuryPhase12AuditError(
            "queue target-switch boundary must retain the Cat cancellation provenance"
        )
    armor_floor = boundaries["armor_floor"]
    if (
        armor_floor.get("status") != "UNRESOLVED"
        or armor_floor.get("active_campaign_task") is not False
        or armor_floor.get("simulator_calibration_status") != "not_calibrated"
    ):
        raise FuryPhase12AuditError(
            "armor-floor boundary must remain inactive and unresolved"
        )

    bridge = document.get("white_rage_bridge")
    if not isinstance(bridge, dict):
        raise FuryPhase12AuditError("Phase-12 preregistration lacks white-rage bridge")
    weapons = bridge.get("weapons")
    if not isinstance(weapons, list) or [item.get("item_id") for item in weapons] != [
        17076,
        19353,
    ]:
        raise FuryPhase12AuditError("white-rage bridge weapon identities drifted")
    if any(item.get("required_samples") != 12 for item in weapons):
        raise FuryPhase12AuditError("white-rage bridge requires 12 samples per weapon")
    cells = bridge.get("cells")
    if (
        not isinstance(cells, dict)
        or cells.get("sunder_stacks") != [0, 5]
        or cells.get("outcomes") != ["ordinary", "critical"]
        or cells.get("samples_per_weapon_armor_outcome") != 3
        or cells.get("holdout_sample_ordinal") != 3
    ):
        raise FuryPhase12AuditError("white-rage bridge cell allocation drifted")
    model_ids = [
        item.get("id")
        for item in bridge.get("model_families", [])
        if isinstance(item, dict)
    ]
    if model_ids != [
        "M_base_common",
        "M_live_common",
        "M_weapon_offset",
        "M_fixed_intercept",
    ]:
        raise FuryPhase12AuditError("white-rage bridge model families drifted")
    holdout = bridge.get("holdout_policy")
    if (
        not isinstance(holdout, dict)
        or holdout.get("refit_on_holdout_permitted") is not False
        or holdout.get("simulator_patch_permitted_by_collection_alone") is not False
    ):
        raise FuryPhase12AuditError("white-rage bridge must remain no-refit/no-patch")
    publication = document.get("publication_gate")
    if (
        not isinstance(publication, dict)
        or publication.get("simulator_patch_allowed") is not False
        or publication.get("simulator_patch") is not None
    ):
        raise FuryPhase12AuditError("Phase-12 preregistration must fail closed")


def _phase12_runs(
    summary: dict[str, Any], campaign_id: str
) -> list[dict[str, Any]]:
    runs = summary.get("specialized_campaigns")
    if not isinstance(runs, list):
        raise FuryPhase12AuditError("summary has no specialized_campaigns list")
    return [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("campaign_id") == campaign_id
        and run.get("analyzer") == PHASE12_ANALYZER
    ]


def _phase12_run(summary: dict[str, Any]) -> dict[str, Any]:
    repairs = _phase12_runs(summary, PHASE12_REPAIR_CAMPAIGN_ID)
    if len(repairs) > 1:
        raise FuryPhase12AuditError(
            "summary contains more than one Phase-12 repair campaign run"
        )
    if repairs:
        return repairs[0]
    matches = _phase12_runs(summary, PHASE12_CAMPAIGN_ID)
    if len(matches) != 1:
        raise FuryPhase12AuditError(
            "summary must contain exactly one Phase-12 base or repair campaign run"
        )
    return matches[0]


def _exact_base_phase12_run(
    summary: dict[str, Any],
    *,
    base_campaign_run_id: str,
) -> dict[str, Any] | None:
    matches = [
        run
        for run in _phase12_runs(summary, PHASE12_CAMPAIGN_ID)
        if run.get("campaign_run_id") == base_campaign_run_id
    ]
    if len(matches) > 1:
        raise FuryPhase12AuditError(
            f"summary contains duplicate exact baseCampaignRunId {base_campaign_run_id}"
        )
    return matches[0] if matches else None


def _find_exact_base_phase12_run(
    summary: dict[str, Any],
    *,
    base_campaign_run_id: str,
    current_summary: Path,
    history_directory: Path | None,
) -> tuple[dict[str, Any], str, list[str]]:
    candidates: list[tuple[dict[str, Any], str]] = []
    current = _exact_base_phase12_run(
        summary, base_campaign_run_id=base_campaign_run_id
    )
    if current is not None:
        candidates.append((current, str(current_summary.resolve())))
    if history_directory is not None and history_directory.is_dir():
        for path in sorted(history_directory.glob("*.json")):
            if path.resolve() == current_summary.resolve():
                continue
            try:
                document = _load_object(path, "historical calibration summary")
                if document.get("kind") != "brainofcat_calibration_summary":
                    continue
                candidate = _exact_base_phase12_run(
                    document, base_campaign_run_id=base_campaign_run_id
                )
            except FuryPhase12AuditError:
                continue
            if candidate is not None:
                candidates.append((candidate, str(path.resolve())))
    strict = [
        candidate
        for candidate in candidates
        if isinstance(candidate[0].get("exact_action_chain_complete"), bool)
        and all(
            isinstance(task, dict)
            and isinstance(task.get("exact_action_chain_complete"), bool)
            for task in candidate[0].get("tasks", [])
        )
    ]
    if not strict:
        raise FuryPhase12AuditError(
            "repair audit cannot find a strictly re-decoded base summary for exact "
            f"baseCampaignRunId {base_campaign_run_id}"
        )
    base_run, origin = strict[0]
    return base_run, origin, [path for _, path in strict[1:]]


def _combined_white_rage_bridge(
    base_bridge: dict[str, Any], repair_bridge: dict[str, Any]
) -> dict[str, Any]:
    base_samples = base_bridge.get("samples")
    repair_samples = repair_bridge.get("samples")
    if not isinstance(base_samples, list) or not isinstance(repair_samples, list):
        raise FuryPhase12AuditError("base and repair bridge must expose clean samples")
    samples: list[dict[str, Any]] = []
    for source, source_samples in (("base", base_samples), ("repair", repair_samples)):
        for raw_sample in source_samples:
            if not isinstance(raw_sample, dict):
                raise FuryPhase12AuditError("bridge sample must be an object")
            sample = dict(raw_sample)
            sample["combined_evidence_source"] = source
            samples.append(sample)

    coverage: dict[str, dict[str, Any]] = {}
    marker_violation_count = 0
    canonical_samples: list[dict[str, Any]] = []
    for item_id in (17076, 19353):
        for sunder in (0, 5):
            for outcome in ("ordinary", "critical"):
                cell_samples = [
                    sample
                    for sample in samples
                    if sample.get("weapon_item_id") == item_id
                    and sample.get("sunder_stacks") == sunder
                    and sample.get("outcome") == outcome
                ]
                # Base evidence always precedes its explicitly linked repair.
                cell_samples.sort(
                    key=lambda sample: (
                        0
                        if sample.get("combined_evidence_source") == "base"
                        else 1,
                        sample.get("sequence")
                        if type(sample.get("sequence")) is int
                        else 10**18,
                    )
                )
                for ordinal, sample in enumerate(cell_samples, start=1):
                    expected_marker = (
                        "internal_holdout" if ordinal == 3 else "identification"
                    )
                    sample["sample_ordinal_in_cell"] = ordinal
                    sample["sample_role"] = (
                        "holdout" if ordinal == 3 else "identification"
                    )
                    sample["sample_role_assignment_basis"] = (
                        "preregistered_clean_acceptance_ordinal"
                    )
                    sample["canonical_fit_permitted"] = ordinal <= 2
                    sample["canonical_holdout_fit_permitted"] = False
                    emitted_valid = (
                        sample.get("sample_role_marker") == expected_marker
                        and sample.get("fit_permitted") is (ordinal <= 2)
                        and sample.get("holdout_fit_permitted") is False
                    )
                    sample["emitted_marker_contract_valid"] = emitted_valid
                    sample["marker_contract_valid"] = emitted_valid
                    if not emitted_valid:
                        marker_violation_count += 1
                key = f"item_{item_id}_sunder_{sunder}_{outcome}"
                coverage[key] = {
                    "accepted_clean_samples": len(cell_samples),
                    "required_samples": 3,
                    "missing_clean_samples": max(0, 3 - len(cell_samples)),
                    "identification_samples": min(len(cell_samples), 2),
                    "holdout_samples": 1 if len(cell_samples) >= 3 else 0,
                    "complete": len(cell_samples) == 3,
                }
                canonical_samples.extend(cell_samples)
    retest = [key for key, cell in coverage.items() if not cell["complete"]]
    return {
        "parser_basis": "exact baseCampaignRunId plus repair_v2 clean samples",
        "merge_mode": "base_then_repair_preregistered_clean_ordinal",
        "required_clean_sample_count": 24,
        "accepted_marker_count": int(base_bridge.get("accepted_marker_count") or 0)
        + int(repair_bridge.get("accepted_marker_count") or 0),
        "clean_sample_count": len(canonical_samples),
        "contaminated_accepted_marker_count": int(
            base_bridge.get("contaminated_accepted_marker_count") or 0
        )
        + int(repair_bridge.get("contaminated_accepted_marker_count") or 0),
        "known_proc_aura_active_sample_count": int(
            base_bridge.get("known_proc_aura_active_sample_count") or 0
        )
        + int(repair_bridge.get("known_proc_aura_active_sample_count") or 0),
        "source_bridge_controls": {
            "base": {
                "known_proc_aura_control": base_bridge.get(
                    "known_proc_aura_control"
                ),
                "attribution_control": base_bridge.get("attribution_control"),
            },
            "repair": {
                "known_proc_aura_control": repair_bridge.get(
                    "known_proc_aura_control"
                ),
                "attribution_control": repair_bridge.get("attribution_control"),
            },
        },
        "coverage": coverage,
        "coverage_complete": not retest,
        "retest_required_cell_ids": retest,
        "holdout_sample_ordinal": 3,
        "holdout_refit_detected": False,
        "emitted_marker_contract_violation_count": marker_violation_count,
        "marker_contract_violation_count": marker_violation_count,
        "canonical_role_assignment_complete": True,
        "bridge_role_contract_complete": True,
        "role_assignment_basis": (
            "preregistered_clean_acceptance_ordinal_first_two_identification_third_holdout"
        ),
        "model_fit_performed": False,
        "replacement_formula_identified": False,
        "simulator_patch_allowed": False,
        "simulator_patch": None,
        "samples": canonical_samples,
    }


def _combine_base_and_repair_runs(
    base_run: dict[str, Any], repair_run: dict[str, Any]
) -> dict[str, Any]:
    base_tasks = base_run.get("tasks")
    repair_tasks = repair_run.get("tasks")
    if not isinstance(base_tasks, list) or not isinstance(repair_tasks, list):
        raise FuryPhase12AuditError("base and repair runs must expose task inventories")
    observed_repair_order = tuple(
        task.get("task_id") for task in repair_tasks if isinstance(task, dict)
    )
    if observed_repair_order != PHASE12_REPAIR_TASK_ORDER:
        raise FuryPhase12AuditError("repair task inventory/order does not match repair_v2")
    repair_by_base: dict[str, dict[str, Any]] = {}
    for task in repair_tasks:
        repair_id = task.get("task_id")
        expected_base = PHASE12_REPAIR_TASK_BASE_IDS.get(repair_id)
        if task.get("base_task_id") != expected_base:
            raise FuryPhase12AuditError(
                f"repair task {repair_id} does not declare exact baseTaskId {expected_base}"
            )
        repair_by_base[expected_base] = task

    combined_tasks: list[dict[str, Any]] = []
    for base_task in base_tasks:
        if not isinstance(base_task, dict):
            raise FuryPhase12AuditError("base task inventory entry must be an object")
        base_task_id = base_task.get("task_id")
        repair_task = repair_by_base.get(base_task_id)
        replacement_by_stage: dict[str, dict[str, Any]] = {}
        if repair_task is not None:
            for trial in repair_task.get("trials", []):
                if isinstance(trial, dict) and isinstance(trial.get("stage_id"), str):
                    replacement_by_stage[trial["stage_id"]] = trial
        combined_trials: list[dict[str, Any]] = []
        applied: list[str] = []
        base_stage_ids: set[str] = set()
        for base_trial in base_task.get("trials", []):
            if not isinstance(base_trial, dict):
                continue
            stage_id = base_trial.get("stage_id")
            if isinstance(stage_id, str):
                base_stage_ids.add(stage_id)
            replacement = replacement_by_stage.get(stage_id)
            selected = dict(replacement if replacement is not None else base_trial)
            selected["combined_evidence_source"] = (
                "repair" if replacement is not None else "base"
            )
            if replacement is not None and isinstance(stage_id, str):
                applied.append(stage_id)
            combined_trials.append(selected)
        unmatched = set(replacement_by_stage) - base_stage_ids
        if unmatched and base_task_id not in {
            PHASE12_TASK_ORDER[0],
            PHASE12_TASK_ORDER[1],
            PHASE12_TASK_ORDER[2],
            PHASE12_TASK_ORDER[9],
        }:
            raise FuryPhase12AuditError(
                f"repair stages do not exist in exact base task {base_task_id}: "
                + ",".join(sorted(unmatched))
            )
        exact = bool(combined_trials) and all(
            trial.get("exact_action_chain_complete") is True
            for trial in combined_trials
        )
        if repair_task is not None:
            exact = exact and repair_task.get("exact_action_chain_complete") is True
        combined = dict(base_task)
        combined["trials"] = combined_trials
        combined["status"] = "completed" if exact else "coverage_partial"
        combined["exact_action_chain_complete"] = exact
        combined["strictly_valid_trial_count"] = sum(
            trial.get("exact_action_chain_complete") is True
            for trial in combined_trials
        )
        combined["reusable_stage_ids"] = [
            trial["stage_id"]
            for trial in combined_trials
            if trial.get("exact_action_chain_complete") is True
            and isinstance(trial.get("stage_id"), str)
        ]
        combined["retest_required_stage_ids"] = [
            trial["stage_id"]
            for trial in combined_trials
            if trial.get("exact_action_chain_complete") is not True
            and isinstance(trial.get("stage_id"), str)
        ]
        if repair_task is not None:
            combined["repair_task_id"] = repair_task.get("task_id")
            combined["repair_applied_stage_ids"] = applied
            for stage_id in repair_task.get("retest_required_stage_ids", []):
                if (
                    isinstance(stage_id, str)
                    and stage_id not in combined["retest_required_stage_ids"]
                ):
                    combined["retest_required_stage_ids"].append(stage_id)
        combined_tasks.append(combined)

    combined_bridge = _combined_white_rage_bridge(
        base_run.get("white_rage_bridge", {}),
        repair_run.get("white_rage_bridge", {}),
    )
    retest = [
        stage_id
        for task in combined_tasks
        for stage_id in task.get("retest_required_stage_ids", [])
    ]
    retest.extend(
        f"white_rage_bridge:{cell_id}"
        for cell_id in combined_bridge["retest_required_cell_ids"]
    )
    exact = all(
        task.get("exact_action_chain_complete") is True
        for task in combined_tasks
    )
    return {
        "campaign_id": PHASE12_CAMPAIGN_ID,
        "campaign_run_id": base_run.get("campaign_run_id"),
        "campaign_kind": "base_plus_repair_v2",
        "base_campaign_run_id": base_run.get("campaign_run_id"),
        "repair_campaign_run_id": repair_run.get("campaign_run_id"),
        "collector_revision": repair_run.get("collector_revision"),
        "analyzer": PHASE12_ANALYZER,
        "status": (
            "completed"
            if exact and combined_bridge["coverage_complete"]
            else "coverage_partial"
        ),
        "completion_confirmed": (
            base_run.get("completion_confirmed") is True
            and repair_run.get("completion_confirmed") is True
        ),
        "task_count_consistent": len(combined_tasks) == len(PHASE12_TASK_ORDER),
        "declared_task_count": len(PHASE12_TASK_ORDER),
        "observed_task_count": len(combined_tasks),
        "exact_action_chain_complete": exact,
        "reusable_stage_ids": [
            stage_id
            for task in combined_tasks
            for stage_id in task.get("reusable_stage_ids", [])
        ],
        "retest_required_stage_ids": list(dict.fromkeys(retest)),
        "tasks": combined_tasks,
        "white_rage_bridge": combined_bridge,
        "simulator_patch_allowed": False,
        "simulator_patch": None,
    }


def _text_values(value: Any) -> set[str]:
    if isinstance(value, str) and value:
        return {value.lower()}
    if isinstance(value, list):
        return {item.lower() for item in value if isinstance(item, str) and item}
    return set()


def _task_matches(task: dict[str, Any], match: dict[str, Any]) -> bool:
    task_id = task.get("task_id")
    completion_kind = task.get("completion_kind")
    category = task.get("category")
    lowered_id = task_id.lower() if isinstance(task_id, str) else ""
    lowered_kind = completion_kind.lower() if isinstance(completion_kind, str) else ""
    lowered_category = category.lower() if isinstance(category, str) else ""
    if lowered_category in _text_values(match.get("categories")):
        return True
    if lowered_kind in _text_values(match.get("completion_kinds")):
        return True
    return any(
        lowered_id.startswith(prefix)
        for prefix in _text_values(match.get("task_id_prefixes"))
    )


def _history_task_inventory(
    history_directory: Path | None,
    task_ids: Iterable[str],
    current_summary: Path,
) -> list[dict[str, Any]]:
    wanted = set(task_ids)
    if not wanted or history_directory is None or not history_directory.is_dir():
        return []
    # The logger can retain an earlier task window into a later export.  Such a
    # summary is a mirror of the same taskRunId, not an independent evidence
    # source.  Keep every unique run, attribute it to its earliest imported
    # summary, and expose later mirrors explicitly.
    occurrences: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in sorted(history_directory.glob("*.json")):
        try:
            if path.resolve() == current_summary.resolve():
                continue
            document = _load_object(path, "historical calibration summary")
        except (FuryPhase12AuditError, OSError):
            continue
        if document.get("kind") != "brainofcat_calibration_summary":
            continue
        tasks = document.get("task_completions")
        if not isinstance(tasks, list):
            continue
        source = document.get("source")
        source = source if isinstance(source, dict) else {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            if (
                task_id in wanted
                and task.get("status") == "completed"
                and task.get("analysis_status")
                not in {"incomplete_evidence", "coverage_partial"}
            ):
                task_run_id = task.get("task_run_id")
                stable_run_id = (
                    task_run_id
                    if isinstance(task_run_id, str) and task_run_id
                    else f"missing-run-id:{path.resolve()}"
                )
                occurrences.setdefault((task_id, stable_run_id), []).append({
                    "task_id": task_id,
                    "task_run_id": task_run_id,
                    "summary_path": str(path.resolve()),
                    "calibration_jsonl": source.get("calibration_jsonl"),
                    "raw_file": source.get("raw_file"),
                    "imported_at": source.get("imported_at"),
                    "analysis_status": task.get("analysis_status"),
                })
    inventory: list[dict[str, Any]] = []
    for (task_id, _), copies in occurrences.items():
        copies.sort(
            key=lambda copy: (
                copy.get("imported_at")
                if isinstance(copy.get("imported_at"), str)
                else "9999",
                copy["summary_path"],
            )
        )
        origin = copies[0]
        inventory.append(
            {
                "task_id": task_id,
                "task_run_id": origin.get("task_run_id"),
                "analysis_status": origin.get("analysis_status"),
                "evidence_origin": {
                    "summary": origin["summary_path"],
                    "calibration_jsonl": origin.get("calibration_jsonl"),
                    "raw_file": origin.get("raw_file"),
                    "imported_at": origin.get("imported_at"),
                },
                "mirror_summary_count": max(0, len(copies) - 1),
                "mirror_summaries": [
                    copy["summary_path"] for copy in copies[1:]
                ],
            }
        )
    return sorted(
        inventory,
        key=lambda item: (
            item["task_id"],
            str(item.get("task_run_id") or ""),
        ),
    )


def build_phase12_audit(
    summary: dict[str, Any],
    preregistration: dict[str, Any],
    *,
    summary_path: Path,
    preregistration_path: Path,
    history_directory: Path | None,
) -> dict[str, Any]:
    """Build a collection audit without fitting or changing the simulator."""

    validate_preregistration(preregistration)
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise FuryPhase12AuditError("input is not a BrainOfCat calibration summary")
    selected_run = _phase12_run(summary)
    evidence_merge: dict[str, Any] | None = None
    base_summary_origin: str | None = None
    if selected_run.get("campaign_id") == PHASE12_REPAIR_CAMPAIGN_ID:
        if selected_run.get("base_campaign_id") != PHASE12_CAMPAIGN_ID:
            raise FuryPhase12AuditError(
                "repair campaign does not declare the preregistered baseCampaignId"
            )
        base_campaign_run_id = selected_run.get("base_campaign_run_id")
        if not isinstance(base_campaign_run_id, str) or not base_campaign_run_id:
            raise FuryPhase12AuditError("repair campaign lacks exact baseCampaignRunId")
        if selected_run.get("collector_revision") != 2:
            raise FuryPhase12AuditError("repair campaign collectorRevision must be 2")
        base_run, base_summary_origin, base_mirrors = _find_exact_base_phase12_run(
            summary,
            base_campaign_run_id=base_campaign_run_id,
            current_summary=summary_path,
            history_directory=history_directory,
        )
        run = _combine_base_and_repair_runs(base_run, selected_run)
        evidence_merge = {
            "mode": "exact_base_campaign_run_plus_repair_v2",
            "base": {
                "campaign_id": base_run.get("campaign_id"),
                "campaign_run_id": base_run.get("campaign_run_id"),
                "summary_path": base_summary_origin,
                "mirror_summary_paths": base_mirrors,
                "exact_action_chain_complete": base_run.get(
                    "exact_action_chain_complete"
                ),
                "retest_required_stage_ids": base_run.get(
                    "retest_required_stage_ids", []
                ),
            },
            "repair": {
                "campaign_id": selected_run.get("campaign_id"),
                "campaign_run_id": selected_run.get("campaign_run_id"),
                "base_campaign_run_id": base_campaign_run_id,
                "collector_revision": selected_run.get("collector_revision"),
                "exact_action_chain_complete": selected_run.get(
                    "exact_action_chain_complete"
                ),
                "retest_required_stage_ids": selected_run.get(
                    "retest_required_stage_ids", []
                ),
            },
            "combined": {
                "exact_action_chain_complete": run.get(
                    "exact_action_chain_complete"
                ),
                "bridge_coverage_complete": run.get(
                    "white_rage_bridge", {}
                ).get("coverage_complete"),
                "retest_required_stage_ids": run.get(
                    "retest_required_stage_ids", []
                ),
            },
        }
    else:
        run = selected_run
    tasks = run.get("tasks")
    if not isinstance(tasks, list):
        raise FuryPhase12AuditError("Phase-12 specialized run has no task inventory")

    task_by_id = {
        task.get("task_id"): task
        for task in tasks
        if isinstance(task, dict)
        and isinstance(task.get("task_id"), str)
        and task.get("task_id")
    }
    ordered_task_ids = tuple(
        task.get("task_id")
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("task_id"), str)
    )
    task_inventory_exact = (
        len(task_by_id) == len(tasks)
        and ordered_task_ids == PHASE12_TASK_ORDER
    )

    gates: dict[str, dict[str, Any]] = {}
    for name, gate_spec in preregistration["mechanism_gates"].items():
        matching = [
            task
            for task in tasks
            if isinstance(task, dict) and _task_matches(task, gate_spec["match"])
        ]
        expected_ids = gate_spec["expected_task_ids"]
        expected_tasks = [
            task_by_id[task_id] for task_id in expected_ids if task_id in task_by_id
        ]
        missing_ids = [task_id for task_id in expected_ids if task_id not in task_by_id]
        exact_incomplete = [
            task
            for task in expected_tasks
            if task.get("exact_action_chain_complete") is not True
            and task.get("status") != "deferred"
        ]
        completed = [
            task
            for task in expected_tasks
            if task.get("status") == "completed"
            and task.get("exact_action_chain_complete") is True
        ]
        partial = [
            task
            for task in expected_tasks
            if task.get("status") in {"coverage_partial", "incomplete"}
        ]
        deferred = [
            task for task in expected_tasks if task.get("status") == "deferred"
        ]
        unexpected_status = [
            task
            for task in expected_tasks
            if task.get("status")
            not in {"completed", "coverage_partial", "incomplete", "deferred"}
        ]
        reused = _history_task_inventory(
            history_directory,
            gate_spec["reuse_task_ids"],
            summary_path,
        )
        minimum = gate_spec["minimum_completed_current_tasks"]
        reasons: list[str] = []
        if missing_ids:
            status = "INCOMPLETE"
            reasons.append("missing_expected_task_ids:" + ",".join(missing_ids))
        elif partial:
            status = "PARTIAL"
            reasons.append("current_campaign_coverage_partial")
            if exact_incomplete:
                reasons.append("current_campaign_exact_action_chain_incomplete")
        elif exact_incomplete:
            status = "PARTIAL"
            reasons.append("current_campaign_exact_action_chain_incomplete")
        elif unexpected_status:
            status = "INCOMPLETE"
            reasons.append("unexpected_current_task_status")
        elif deferred:
            status = "DEFERRED" if gate_spec["deferred_allowed"] else "INCOMPLETE"
            reasons.append(
                "special_environment_deferred"
                if gate_spec["deferred_allowed"]
                else "deferred_not_allowed_for_gate"
            )
        elif len(completed) == len(expected_ids) and len(completed) >= minimum:
            status = "COMPLETE"
        elif minimum == 0 and reused:
            status = "REUSED"
        else:
            status = "INCOMPLETE"
            reasons.append(
                f"requires_{minimum}_completed_current_task(s); observed_{len(completed)}"
            )
        if reused and status in {"COMPLETE", "DEFERRED", "PARTIAL"}:
            reasons.append("phase1_11_evidence_retained")
        gates[name] = {
            "status": status,
            "deferred_allowed": gate_spec["deferred_allowed"],
            "expected_task_ids": expected_ids,
            "missing_expected_task_ids": missing_ids,
            "minimum_completed_current_tasks": minimum,
            "current_tasks": expected_tasks,
            "exact_action_chain_complete": (
                not missing_ids
                and not exact_incomplete
                and len(expected_tasks) == len(expected_ids)
            ),
            "retest_required_stage_ids": [
                stage_id
                for task in exact_incomplete
                for stage_id in (
                    task.get("retest_required_stage_ids")
                    if isinstance(task.get("retest_required_stage_ids"), list)
                    and task.get("retest_required_stage_ids")
                    else [task.get("task_id")]
                )
                if isinstance(stage_id, str) and stage_id
            ],
            "supplemental_matching_tasks": [
                task
                for task in matching
                if task.get("task_id") not in set(expected_ids)
            ],
            "reused_tasks": reused,
            "reused_unique_task_run_count": len(reused),
            "reasons": reasons,
        }

    bridge = run.get("white_rage_bridge")
    bridge_coverage_complete = (
        isinstance(bridge, dict)
        and bridge.get("coverage_complete") is True
        and bridge.get("holdout_refit_detected") is False
        and bridge.get("bridge_role_contract_complete") is True
        and bridge.get("canonical_role_assignment_complete") is True
    )
    if gates["white_rage"]["status"] == "COMPLETE" and not bridge_coverage_complete:
        gates["white_rage"]["status"] = "PARTIAL"
        gates["white_rage"]["reasons"].append(
            "white_rage_bridge_24_sample_no_refit_gate_incomplete"
        )

    statuses = [gate["status"] for gate in gates.values()]
    terminal = all(status in {"COMPLETE", "REUSED", "DEFERRED"} for status in statuses)
    fully_observed = all(status in {"COMPLETE", "REUSED"} for status in statuses)
    lifecycle_completion_confirmed = run.get("completion_confirmed") is True
    summary_task_count_consistent = run.get("task_count_consistent") is True
    campaign_completed = (
        lifecycle_completion_confirmed
        and summary_task_count_consistent
        and task_inventory_exact
    )
    exact_action_chain_complete = (
        run.get("exact_action_chain_complete") is True
        and all(
            gate.get("exact_action_chain_complete") is True
            for gate in gates.values()
        )
    )
    retest_required_stage_ids = [
        stage_id
        for gate in gates.values()
        for stage_id in gate.get("retest_required_stage_ids", [])
    ]
    if isinstance(bridge, dict):
        retest_required_stage_ids.extend(
            f"white_rage_bridge:{cell_id}"
            for cell_id in bridge.get("retest_required_cell_ids", [])
            if isinstance(cell_id, str) and cell_id
        )
    retest_required_stage_ids = list(dict.fromkeys(retest_required_stage_ids))
    if not campaign_completed or not terminal:
        collection_status = "partial"
    elif fully_observed:
        collection_status = "complete"
    else:
        collection_status = "complete_with_deferred"

    return {
        "schema_version": 1,
        "kind": "fury_current_build_phase12_audit",
        "created_at": _utc_now(),
        "status": collection_status,
        "campaign_id": PHASE12_CAMPAIGN_ID,
        "campaign_run_id": run.get("campaign_run_id"),
        "repair_campaign_run_id": run.get("repair_campaign_run_id"),
        "analyzer": PHASE12_ANALYZER,
        "sources": {
            "calibration_summary": str(summary_path.resolve()),
            "preregistration": str(preregistration_path.resolve()),
            "phase1_11_summary_directory": (
                str(history_directory.resolve())
                if history_directory is not None
                else None
            ),
            "exact_base_campaign_summary": base_summary_origin,
        },
        "evidence_merge": evidence_merge,
        "scope": preregistration["scope"],
        "completion_gate": {
            "campaign_completed": campaign_completed,
            "lifecycle_completion_marker_present": lifecycle_completion_confirmed,
            "summary_task_count_consistent": summary_task_count_consistent,
            "exact_preregistered_task_inventory": task_inventory_exact,
            "exact_action_chain_complete": exact_action_chain_complete,
            "base_exact_action_chain_complete": (
                evidence_merge["base"]["exact_action_chain_complete"]
                if evidence_merge is not None
                else run.get("exact_action_chain_complete")
            ),
            "repair_exact_action_chain_complete": (
                evidence_merge["repair"]["exact_action_chain_complete"]
                if evidence_merge is not None
                else None
            ),
            "combined_exact_action_chain_complete": (
                evidence_merge["combined"]["exact_action_chain_complete"]
                if evidence_merge is not None
                else run.get("exact_action_chain_complete")
            ),
            "retest_required_stage_ids": retest_required_stage_ids,
            "observed_task_order": list(ordered_task_ids),
            "expected_task_order": list(PHASE12_TASK_ORDER),
            "all_required_gates_terminal": terminal,
            "fully_observed_without_deferred": fully_observed,
            "deferred_is_not_calibrated_evidence": True,
            "external_boundaries_count_toward_collection_complete": False,
            "external_boundaries_authorize_simulator_patch": False,
        },
        "mechanism_gates": gates,
        "external_mechanism_boundaries": preregistration[
            "external_mechanism_boundaries"
        ],
        "white_rage_bridge": bridge,
        "model_families": preregistration["white_rage_bridge"]["model_families"],
        "conclusion_gate": {
            "campaign_terminal": campaign_completed and terminal,
            "collection_complete": campaign_completed and fully_observed,
            "fully_observed": campaign_completed and fully_observed,
            "replacement_formula_identified": False,
            "holdout_refit_permitted": False,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "external_boundaries_excluded_from_collection_complete": True,
            "external_boundaries_authorize_simulator_patch": False,
            "reason": (
                "one or more required gates remain partial or incomplete"
                if not campaign_completed or not terminal
                else "deferred gates are not calibrated evidence"
                if not fully_observed
                else "separate evidence review required after Phase-12 collection"
            ),
        },
        "simulator_overrides": [],
    }


def run_from_path(
    summary_path: str | Path,
    preregistration_path: str | Path = DEFAULT_PREREGISTRATION,
    *,
    history_directory: str | Path | None = DEFAULT_HISTORY_DIRECTORY,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Load, audit, and optionally publish one completed Phase-12 summary."""

    summary_file = Path(summary_path).expanduser().resolve()
    prereg_file = Path(preregistration_path).expanduser().resolve()
    history = (
        Path(history_directory).expanduser().resolve()
        if history_directory is not None
        else None
    )
    document = build_phase12_audit(
        _load_object(summary_file, "calibration summary"),
        _load_object(prereg_file, "Phase-12 preregistration"),
        summary_path=summary_file,
        preregistration_path=prereg_file,
        history_directory=history,
    )
    if output is not None:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    return document


__all__ = [
    "DEFAULT_HISTORY_DIRECTORY",
    "DEFAULT_OUTPUT",
    "DEFAULT_PREREGISTRATION",
    "EXTERNAL_MECHANISM_BOUNDARY_NAMES",
    "FuryPhase12AuditError",
    "MECHANISM_GATE_NAMES",
    "PHASE12_ANALYZER",
    "PHASE12_CAMPAIGN_ID",
    "PHASE12_REPAIR_CAMPAIGN_ID",
    "PHASE12_REPAIR_TASK_BASE_IDS",
    "PHASE12_REPAIR_TASK_ORDER",
    "PHASE12_TASK_ORDER",
    "build_phase12_audit",
    "run_from_path",
    "validate_preregistration",
]
