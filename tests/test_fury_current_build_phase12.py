from __future__ import annotations

import json
import copy
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_summary import build_calibration_summary
from o2o_dps.calibration_watch import (
    CalibrationWatcher,
    _summary_contains_terminal_phase12,
)
from o2o_dps.fury_current_build_phase12_audit import (
    DEFAULT_PREREGISTRATION,
    EXTERNAL_MECHANISM_BOUNDARY_NAMES,
    FuryPhase12AuditError,
    MECHANISM_GATE_NAMES,
    PHASE12_TASK_ORDER,
    PHASE12_REPAIR_CAMPAIGN_ID,
    PHASE12_REPAIR_TASK_BASE_IDS,
    PHASE12_REPAIR_TASK_ORDER,
    run_from_path,
    validate_preregistration,
)


CAMPAIGN_ID = "warrior_fury_current_build_dummy_phase12"
CAMPAIGN_RUN_ID = "campaign-phase12-test"
REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _phase12_rows(
    *,
    action_partial: bool = True,
    bridge_action_per_trial: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    sequence = 0

    def add(
        event: str,
        *,
        marker: dict | None = None,
        task: dict | None = None,
        state: dict | None = None,
        **fields: object,
    ) -> dict:
        nonlocal sequence
        sequence += 1
        row = {
            "sequence": sequence,
            "time": float(sequence),
            "event": event,
            "state": state or {
                "inCombat": True,
                "moving": False,
                "targetExists": True,
                "targetGUID": "dummy-guid",
                "targetClassification": "worldboss",
                "playerLevel": 60,
                "targetArmor": {"effective": 4211},
            },
        }
        if marker is not None:
            row["marker"] = marker
        if task is not None:
            row["task"] = task
        row.update(fields)
        rows.append(row)
        return row

    task_specs = [
        ("warrior_white_swing_rage_bridge_sword_phase12", "white_swing_rage_bridge_sword", "white_rage", 12, "completed"),
        ("warrior_white_swing_rage_bridge_axe_phase12", "white_swing_rage_bridge_axe", "white_rage", 12, "completed"),
        ("warrior_restore_calibration_weapon", "restore_calibration_weapon", "restore", 1, "completed"),
        ("warrior_fury_dual_wield_mechanics_phase12", "fury_phase12_scripted", "dual_wield", 1, "completed"),
        (
            "warrior_fury_action_damage_avoidance_phase12",
            "fury_phase12_scripted",
            "action_damage_avoidance",
            2,
            "coverage_partial" if action_partial else "completed",
        ),
        ("warrior_fury_queue_execution_phase12", "fury_phase12_scripted", "queue", 1, "completed"),
        ("warrior_fury_flurry_deep_wounds_phase12", "fury_phase12_scripted", "flurry_deep_wounds", 1, "completed"),
        ("warrior_fury_movement_range_latency_phase12", "fury_phase12_scripted", "movement_range", 1, "completed"),
        ("warrior_fury_self_buffs_stances_phase12", "fury_phase12_scripted", "burst_stance", 1, "completed"),
        ("warrior_fury_restore_loadout_phase12", "fury_phase12_restore_loadout", "restore", 1, "completed"),
    ]
    add(
        "CALIBRATION_CAMPAIGN_STARTED",
        marker={
            "campaignId": CAMPAIGN_ID,
            "campaignRunId": CAMPAIGN_RUN_ID,
            "campaignTaskCount": len(task_specs),
            "status": "running",
        },
    )
    for step, (task_id, kind, category, required, status) in enumerate(task_specs, start=1):
        common = {
            "campaignId": CAMPAIGN_ID,
            "campaignRunId": CAMPAIGN_RUN_ID,
            "campaignStep": step,
            "campaignTaskCount": len(task_specs),
            "taskId": task_id,
            "taskRunId": f"task-phase12-{step}",
            "completionKind": kind,
            "requiredTrials": required,
            "environment": f"environment for {category}",
        }
        if status == "deferred":
            add("CALIBRATION_TASK_STARTED", marker=common, task=common)
            add(
                "CALIBRATION_TASK_DEFERRED",
                marker={
                    **common,
                    "reason": "special_environment_unavailable",
                    "phase12Coverage": "deferred",
                },
            )
            add(
                "CALIBRATION_TRIAL_COMPLETED",
                marker={
                    **common,
                    "trial": required,
                    "completionSource": "environment_station_deferred",
                    "phase12Coverage": "deferred",
                },
                task={**common, "trial": required},
            )
            add(
                "CALIBRATION_TASK_COMPLETED",
                marker={
                    **common,
                    "trial": required,
                    "completionSource": "environment_station_deferred",
                    "phase12Coverage": "deferred",
                },
            )
            continue
        add("CALIBRATION_TASK_STARTED", marker=common, task=common)
        completed_trials = required if status == "completed" else 1
        for trial in range(1, completed_trials + 1):
            stage_id = None
            if category == "dual_wield":
                stage_id = "dual_unqueued_1"
            elif category == "action_damage_avoidance":
                stage_id = f"heroic_strike_attempt_{trial}"
            elif category == "queue":
                stage_id = "hs_early"
            elif category == "flurry_deep_wounds":
                stage_id = "flurry_dw_baseline_1"
            elif category == "movement_range":
                stage_id = "slam_stationary"
            elif category == "burst_stance":
                stage_id = "stance_battle"
            trial_context = {
                **common,
                "trial": trial,
                **({"phase12StageId": stage_id} if stage_id else {}),
            }
            add("CALIBRATION_TRIAL_STARTED", marker=trial_context, task=trial_context)
            if category not in {"white_rage", "restore"} or bridge_action_per_trial or (
                category == "white_rage" and trial == 1
            ):
                add(
                    "CALIBRATION_ACTION_REQUESTED",
                    marker={
                        **trial_context,
                        "actionAttempt": 1,
                        "action": (
                            "observe"
                            if category in {"dual_wield", "flurry_deep_wounds"}
                            else "cast"
                        ),
                    },
                    task=trial_context,
                )
            if category == "white_rage":
                cell_index = (trial - 1) // 3
                item_id = 17076 if "sword" in task_id else 19353
                hit_info = 130 if cell_index % 2 else 2
                swing = add(
                    "AUTO_ATTACK_SELF",
                    task=trial_context,
                    amount=200 + trial,
                    hitInfo=hit_info,
                )
                resource = add("UNIT_RAGE", task=trial_context, unit="player")
                add(
                    "CALIBRATION_WHITE_SWING_ACCEPTED",
                    marker={
                        **trial_context,
                        "mainHandItemID": item_id,
                        "plannedSunderStacks": 0 if cell_index < 2 else 5,
                        "sampleQuota": "ordinary" if cell_index % 2 == 0 else "critical",
                        "bridgeModelUse": (
                            "internal_holdout" if trial % 3 == 0 else "identification"
                        ),
                        "fitPermitted": trial % 3 != 0,
                        "holdoutFitPermitted": False,
                        "damageAmount": 200 + trial,
                        "rageDeltaRawTenths": 100 + trial,
                        "rageRawScale": 10,
                        "swingSequence": swing["sequence"],
                        "resourceSequence": resource["sequence"],
                        "flurryActive": False,
                    },
                    task=trial_context,
                )
            elif task_id == "warrior_restore_calibration_weapon":
                add(
                    "CALIBRATION_WEAPON_RESTORE_CONFIRMED",
                    marker={**trial_context, "restoredItemID": 21679},
                    task=trial_context,
                )
            elif task_id == "warrior_fury_restore_loadout_phase12":
                add(
                    "CALIBRATION_LOADOUT_RESTORE_CONFIRMED",
                    marker=trial_context,
                    task=trial_context,
                )
            elif category == "dual_wield":
                add("AUTO_ATTACK_SELF", task=trial_context, amount=100, hitInfo=2)
                add("AUTO_ATTACK_SELF", task=trial_context, amount=50, hitInfo=6)
            elif category in {"action_damage_avoidance", "queue"}:
                add(
                    "SPELL_CAST_EVENT",
                    task=trial_context,
                    spellID=25286,
                    castSucceeded=True,
                    castType=2,
                )
                add("SPELL_START_SELF", task=trial_context, spellID=25286)
                add("SPELL_GO_SELF", task=trial_context, spellID=25286)
                add(
                    "SPELL_DAMAGE_EVENT_SELF",
                    marker={**trial_context, "outcome": "ordinary"},
                    task=trial_context,
                    spellID=25286,
                )
            elif category == "flurry_deep_wounds":
                add("AUTO_ATTACK_SELF", task=trial_context, amount=100, hitInfo=2)
            elif category == "movement_range":
                add(
                    "SPELL_CAST_EVENT",
                    task=trial_context,
                    spellID=45961,
                    castSucceeded=True,
                    castType=0,
                )
                add("SPELL_START_SELF", task=trial_context, spellID=45961)
                add("SPELL_GO_SELF", task=trial_context, spellID=45961)
                add(
                    "SPELL_DAMAGE_EVENT_SELF",
                    marker={**trial_context, "outcome": "ordinary"},
                    task=trial_context,
                    spellID=45961,
                )
            elif category == "burst_stance":
                add(
                    "SPELL_CAST_EVENT",
                    task=trial_context,
                    spellID=2457,
                    castSucceeded=True,
                    castType=1,
                )
                add("SPELL_START_SELF", task=trial_context, spellID=2457)
                add("SPELL_GO_SELF", task=trial_context, spellID=2457)
                add("BUFF_ADDED_SELF", task=trial_context, spellID=2457)
            else:
                raise AssertionError(f"unhandled fixture category: {category}")
            add(
                "CALIBRATION_TRIAL_COMPLETED",
                marker={
                    **trial_context,
                    **({"phase12StageID": stage_id} if stage_id else {}),
                    "completionSource": "automatic_typed_event",
                    "outcome": "ordinary",
                    "phase12Coverage": (
                        "coverage_partial"
                        if status == "coverage_partial"
                        else "observed"
                    ),
                },
                task=trial_context,
            )
        add(
            "CALIBRATION_TASK_COMPLETED",
            marker={
                **common,
                "trial": completed_trials,
                "completionSource": "automatic_typed_event",
                "phase12Coverage": (
                    "coverage_partial"
                    if status == "coverage_partial"
                    else "observed"
                ),
                "coverageReason": (
                    None if status == "completed" else "missing_random_avoidance_outcome"
                ),
            },
        )
    add(
        "CALIBRATION_CAMPAIGN_COMPLETED",
        marker={
            "campaignId": CAMPAIGN_ID,
            "campaignRunId": CAMPAIGN_RUN_ID,
            "campaignTaskCount": len(task_specs),
            "completedTasks": len(task_specs),
            "status": "awaiting_export_reload",
        },
    )
    return rows


def _savedvariables(campaign_run_id: str) -> str:
    return f'''BrainOfCatCharacterDB = {{
    ["schemaVersion"] = 1,
    ["entries"] = {{}},
    ["calibration"] = {{
        ["schemaVersion"] = 1,
        ["maxEntries"] = 15000,
        ["count"] = 1,
        ["nextIndex"] = 2,
        ["nextSequence"] = 2,
        ["entries"] = {{
            [1] = {{
                ["sequence"] = 1,
                ["time"] = 100.5,
                ["event"] = "CALIBRATION_CAMPAIGN_COMPLETED",
                ["state"] = {{}},
                ["marker"] = {{
                    ["campaignId"] = "{CAMPAIGN_ID}",
                    ["campaignRunId"] = "{campaign_run_id}",
                    ["campaignTaskCount"] = 10,
                }},
            }},
        }},
    }},
}}
'''


def _repair_summary_from_base(base_summary: dict) -> tuple[dict, dict]:
    base_summary = copy.deepcopy(base_summary)
    base_run = base_summary["specialized_campaigns"][0]
    base_run_id = base_run["campaign_run_id"]
    tasks = {task["task_id"]: task for task in base_run["tasks"]}

    repair_stage_ids = {
        PHASE12_TASK_ORDER[3]: ["dual_hs_cancel"],
        PHASE12_TASK_ORDER[4]: ["whirlwind_attempt_4"],
        PHASE12_TASK_ORDER[5]: ["hs_cancel"],
        PHASE12_TASK_ORDER[7]: [
            "slam_moving",
            "bt_outside_5",
            "ww_inside_8",
            "ww_outside_8",
        ],
    }
    for task_id, stage_ids in repair_stage_ids.items():
        task = tasks[task_id]
        source_trial = copy.deepcopy(task["trials"][0])
        replacement_trials = []
        for trial_number, stage_id in enumerate(stage_ids, start=1):
            trial = copy.deepcopy(source_trial)
            trial["trial"] = trial_number
            trial["stage_id"] = stage_id
            trial["status"] = "incomplete"
            trial["exact_action_chain_complete"] = False
            trial["exact_action_chain"] = {
                "contract_version": "phase12_exact_action_chain_v1",
                "stage_id": stage_id,
                "exact_action_chain_complete": False,
                "steps": [],
                "failure_reasons": ["fixture_base_gap"],
            }
            replacement_trials.append(trial)
        if task_id == PHASE12_TASK_ORDER[4] and len(task["trials"]) > 1:
            replacement_trials.extend(copy.deepcopy(task["trials"][1:]))
        task["trials"] = replacement_trials
        task["status"] = "coverage_partial"
        task["exact_action_chain_complete"] = False
        task["strictly_valid_trial_count"] = sum(
            trial["exact_action_chain_complete"] for trial in replacement_trials
        )
        task["retest_required_stage_ids"] = list(stage_ids)
        task["reusable_stage_ids"] = [
            trial["stage_id"]
            for trial in replacement_trials
            if trial["exact_action_chain_complete"]
        ]

    keep_counts = {
        (17076, 0, "ordinary"): 3,
        (17076, 0, "critical"): 1,
        (17076, 5, "ordinary"): 3,
        (17076, 5, "critical"): 2,
        (19353, 0, "ordinary"): 1,
        (19353, 0, "critical"): 1,
        (19353, 5, "ordinary"): 0,
        (19353, 5, "critical"): 1,
    }
    old_samples = base_run["white_rage_bridge"]["samples"]
    base_samples: list[dict] = []
    repair_samples: list[dict] = []
    for cell, keep in keep_counts.items():
        cell_samples = [
            copy.deepcopy(sample)
            for sample in old_samples
            if (
                sample["weapon_item_id"],
                sample["sunder_stacks"],
                sample["outcome"],
            )
            == cell
        ]
        base_samples.extend(cell_samples[:keep])
        repair_samples.extend(cell_samples[keep:])
    base_bridge = base_run["white_rage_bridge"]
    base_bridge["samples"] = base_samples
    base_bridge["clean_sample_count"] = len(base_samples)
    base_bridge["coverage_complete"] = False
    base_bridge["retest_required_cell_ids"] = [
        f"item_{item}_sunder_{sunder}_{outcome}"
        for (item, sunder, outcome), count in keep_counts.items()
        if count < 3
    ]
    base_run["status"] = "coverage_partial"
    base_run["exact_action_chain_complete"] = False
    base_run["retest_required_stage_ids"] = [
        stage_id
        for stage_ids in repair_stage_ids.values()
        for stage_id in stage_ids
    ] + [
        f"white_rage_bridge:{cell_id}"
        for cell_id in base_bridge["retest_required_cell_ids"]
    ]

    repair_tasks: list[dict] = []
    for repair_id in PHASE12_REPAIR_TASK_ORDER:
        base_task_id = PHASE12_REPAIR_TASK_BASE_IDS[repair_id]
        base_task = tasks[base_task_id]
        stage_ids = repair_stage_ids.get(base_task_id)
        if stage_ids:
            source_by_stage = {
                trial["stage_id"]: trial for trial in base_task["trials"]
            }
            trials = []
            for stage_id in stage_ids:
                trial = copy.deepcopy(source_by_stage[stage_id])
                trial["status"] = "completed"
                trial["exact_action_chain_complete"] = True
                trial["exact_action_chain"] = {
                    "contract_version": "phase12_exact_action_chain_v1",
                    "stage_id": stage_id,
                    "exact_action_chain_complete": True,
                    "steps": [],
                    "failure_reasons": [],
                }
                trials.append(trial)
        else:
            trials = [copy.deepcopy(base_task["trials"][0])]
            trials[0]["stage_id"] = repair_id
            trials[0]["status"] = "completed"
            trials[0]["exact_action_chain_complete"] = True
        repair_task = copy.deepcopy(base_task)
        repair_task.update(
            {
                "task_id": repair_id,
                "base_task_id": base_task_id,
                "status": "completed",
                "exact_action_chain_complete": True,
                "trials": trials,
                "strictly_valid_trial_count": len(trials),
                "reusable_stage_ids": [trial["stage_id"] for trial in trials],
                "retest_required_stage_ids": [],
            }
        )
        repair_tasks.append(repair_task)

    repair_run_id = "campaign-phase12-repair-test"
    for sample in repair_samples:
        sample["campaign_run_id"] = repair_run_id
        sample["base_campaign_run_id"] = base_run_id
    repair_bridge = copy.deepcopy(base_bridge)
    repair_bridge.update(
        {
            "samples": repair_samples,
            "accepted_marker_count": len(repair_samples),
            "clean_sample_count": len(repair_samples),
            "contaminated_accepted_marker_count": 0,
            "standalone_repair_delta": True,
            "repair_delta_complete": True,
            "retest_required_cell_ids": [],
        }
    )
    repair_run = {
        "campaign_id": PHASE12_REPAIR_CAMPAIGN_ID,
        "campaign_run_id": repair_run_id,
        "campaign_kind": "repair_v2",
        "base_campaign_id": CAMPAIGN_ID,
        "base_campaign_run_id": base_run_id,
        "collector_revision": 2,
        "analyzer": "fury_current_build_phase12_v2",
        "status": "completed",
        "completion_confirmed": True,
        "task_count_consistent": True,
        "declared_task_count": len(repair_tasks),
        "observed_task_count": len(repair_tasks),
        "exact_action_chain_complete": True,
        "retest_required_stage_ids": [],
        "tasks": repair_tasks,
        "white_rage_bridge": repair_bridge,
    }
    repair_summary = {
        "schema_version": base_summary["schema_version"],
        "kind": "brainofcat_calibration_summary",
        "specialized_campaigns": [repair_run],
        "task_completions": [],
        "source": {},
    }
    return base_summary, repair_summary


class FuryCurrentBuildPhase12Tests(unittest.TestCase):
    def test_summary_preserves_incomplete_phase12_task_and_campaign_terminals(
        self,
    ) -> None:
        rows = _phase12_rows(action_partial=False)
        incomplete_task_id = "warrior_fury_dual_wield_mechanics_phase12"
        for row in rows:
            marker = row.get("marker")
            marker_task_id = marker.get("taskId") if isinstance(marker, dict) else None
            if (
                row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and marker_task_id == incomplete_task_id
            ):
                row["event"] = "CALIBRATION_TRIAL_INCOMPLETE"
                marker["phase12Coverage"] = "coverage_partial"
                marker["trialIncomplete"] = True
            elif (
                row["event"] == "CALIBRATION_TASK_COMPLETED"
                and marker_task_id == incomplete_task_id
            ):
                row["event"] = "CALIBRATION_TASK_INCOMPLETE"
                marker.pop("phase12Coverage", None)
                marker["phase"] = "collection_partial"
                marker["diagnosticText"] = "missing_queue_transaction"
                marker["taskIncomplete"] = True
            elif row["event"] == "CALIBRATION_CAMPAIGN_COMPLETED":
                row["event"] = "CALIBRATION_CAMPAIGN_INCOMPLETE"
                marker["completedTasks"] = len(PHASE12_TASK_ORDER) - 1
                marker["processedTasks"] = len(PHASE12_TASK_ORDER)
                marker["collectionStatus"] = "collection_partial"

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-incomplete.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        run = document["specialized_campaigns"][0]
        self.assertEqual(run["terminal_event"], "CALIBRATION_CAMPAIGN_INCOMPLETE")
        self.assertTrue(run["terminal_confirmed"])
        self.assertFalse(run["completion_confirmed"])
        self.assertEqual(run["status"], "coverage_partial")
        self.assertEqual(run["observed_task_count"], len(PHASE12_TASK_ORDER))
        self.assertTrue(run["task_count_consistent"])
        self.assertEqual(run["task_completion_marker_count"], 9)
        self.assertEqual(run["task_incomplete_marker_count"], 1)
        self.assertEqual(run["task_terminal_marker_count"], 10)
        incomplete = next(
            task for task in run["tasks"] if task["task_id"] == incomplete_task_id
        )
        self.assertEqual(incomplete["terminal_event"], "CALIBRATION_TASK_INCOMPLETE")
        self.assertEqual(incomplete["status"], "coverage_partial")
        self.assertEqual(incomplete["reported_status"], "coverage_partial")
        self.assertEqual(incomplete["reason"], "missing_queue_transaction")
        self.assertFalse(incomplete["exact_action_chain_complete"])
        self.assertEqual(incomplete["trial_completion_marker_count"], 0)
        self.assertEqual(incomplete["trial_incomplete_marker_count"], 1)
        self.assertEqual(incomplete["trial_terminal_marker_count"], 1)
        self.assertEqual(incomplete["duplicate_trial_completion_marker_count"], 0)
        self.assertEqual(
            incomplete["trials"][0]["terminal_event"],
            "CALIBRATION_TRIAL_INCOMPLETE",
        )

    def test_preregistration_freezes_scope_bridge_and_no_patch_gate(self) -> None:
        document = json.loads(DEFAULT_PREREGISTRATION.read_text(encoding="utf-8"))
        validate_preregistration(document)

        self.assertEqual(document["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(document["analyzer"], "fury_current_build_phase12_v2")
        self.assertEqual(tuple(document["mechanism_gates"]), MECHANISM_GATE_NAMES)
        self.assertEqual(tuple(document["campaign_task_order"]), PHASE12_TASK_ORDER)
        self.assertEqual(len(document["campaign_task_order"]), 10)
        self.assertNotIn("multi_target", document["mechanism_gates"])
        self.assertNotIn("incoming_rage", document["mechanism_gates"])
        self.assertNotIn(
            "category", document["marker_contract"]["required_identity_fields"]
        )
        self.assertFalse(document["scope"]["talent_respec_permitted"])
        self.assertFalse(document["scope"]["consumable_use_permitted"])
        bridge = document["white_rage_bridge"]
        self.assertEqual([item["item_id"] for item in bridge["weapons"]], [17076, 19353])
        self.assertEqual([item["required_samples"] for item in bridge["weapons"]], [12, 12])
        self.assertEqual(bridge["cells"]["holdout_sample_ordinal"], 3)
        self.assertEqual(
            bridge["passive_collection_contract"],
            {
                "action_request_required_per_logical_trial": False,
                "one_accepted_sample_per_logical_trial": True,
                "required_logical_trials_per_weapon": 12,
                "hardware_press_role": (
                    "setup, protected action, rage drain, armor-stratum transition, "
                    "or recovery only; passive white swings may advance consecutive "
                    "logical trials"
                ),
            },
        )
        proc_rule = bridge["attribution_rules"]["weapon_proc_armor_ignore_aura"]
        self.assertNotIn(
            "weapon_proc_target_armor_change", bridge["attribution_rules"]
        )
        self.assertIn(
            "spell 21153 is primarily a caster/self armor-ignore aura", proc_rule
        )
        self.assertIn("700 armor ignored per stack for 10 seconds", proc_rule)
        self.assertIn("stacking up to 3 times", proc_rule)
        self.assertIn("trigger rate is not treated as measured in-client", proc_rule)
        self.assertNotIn("2 PPM", proc_rule)
        self.assertIn(
            "swing-to-acceptance window",
            proc_rule,
        )
        self.assertIn(
            "do not back-attribute",
            proc_rule,
        )
        self.assertFalse(bridge["holdout_policy"]["refit_on_holdout_permitted"])
        self.assertEqual(
            document["marker_contract"]["bridge_role_fields"]["internal_holdout_value"],
            "internal_holdout",
        )
        boundaries = document["external_mechanism_boundaries"]
        self.assertEqual(tuple(boundaries), EXTERNAL_MECHANISM_BOUNDARY_NAMES)
        self.assertTrue(
            all(
                boundary["collection_complete_contribution"] is False
                and boundary["simulator_patch_allowed"] is False
                for boundary in boundaries.values()
            )
        )
        self.assertEqual(
            boundaries["multi_target"]["chronicle_observations"]["whirlwind"],
            {
                "spell_id": 1680,
                "damage_target_group_counts": {
                    "1": 101,
                    "2": 44,
                    "3": 13,
                    "4": 2,
                },
                "max_observed_targets": 4,
            },
        )
        self.assertEqual(
            boundaries["multi_target"]["chronicle_observations"]["cleave"],
            {
                "spell_id": 20569,
                "damage_target_group_counts": {"1": 44, "2": 22},
                "max_observed_targets": 2,
            },
        )
        self.assertEqual(
            boundaries["multi_target"]["candidate_policy_sources"],
            ["Contra_new"],
        )
        self.assertEqual(
            boundaries["incoming_rage"]["simulator_formula_status"],
            "unresolved",
        )
        self.assertEqual(
            boundaries["incoming_rage"]["candidate_policy_sources"],
            ["Contra_new"],
        )
        self.assertIn(
            "IsTargetOfTargetMeor",
            boundaries["incoming_rage"]["candidate_policy_limitation"],
        )
        self.assertEqual(
            boundaries["execute_low_health"]["reuse_task_id"],
            "warrior_execute_transition",
        )
        self.assertFalse(
            boundaries["queue_target_switch"][
                "preserve_on_target_switch_assumed"
            ]
        )
        self.assertFalse(boundaries["armor_floor"]["active_campaign_task"])
        self.assertFalse(document["publication_gate"]["simulator_patch_allowed"])

    def test_summary_inventory_retains_ten_dummy_tasks_and_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12.jsonl"
            _write_jsonl(source, _phase12_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(len(document["specialized_campaigns"]), 1)
        run = document["specialized_campaigns"][0]
        self.assertEqual(run["analyzer"], "fury_current_build_phase12_v2")
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["status"], "coverage_partial")
        self.assertEqual(run["observed_task_count"], 10)
        self.assertEqual(
            tuple(task["task_id"] for task in run["tasks"]), PHASE12_TASK_ORDER
        )
        statuses = {task["task_id"]: task["status"] for task in run["tasks"]}
        self.assertEqual(statuses["warrior_fury_dual_wield_mechanics_phase12"], "completed")
        self.assertEqual(statuses["warrior_fury_action_damage_avoidance_phase12"], "coverage_partial")
        self.assertNotIn("warrior_fury_multi_target_phase12", statuses)
        self.assertNotIn("warrior_fury_hostile_incoming_phase12", statuses)
        action = next(
            task
            for task in run["tasks"]
            if task["task_id"] == "warrior_fury_action_damage_avoidance_phase12"
        )
        self.assertEqual(action["completed_trials"], 1)
        self.assertEqual(action["attempt_count"], 1)
        self.assertEqual(action["outcomes"], {"ordinary": 1})
        self.assertEqual(
            action["action_markers"]["counts_by_event"]["SPELL_CAST_EVENT"], 1
        )
        self.assertIn("in_combat", action["environment_evidence"]["observed"])
        bridge = run["white_rage_bridge"]
        self.assertEqual(bridge["clean_sample_count"], 24)
        self.assertTrue(bridge["coverage_complete"])
        self.assertEqual(bridge["known_proc_aura_active_sample_count"], 0)
        self.assertEqual(
            bridge["known_proc_aura_control"]["spell_id"], 21153
        )
        self.assertFalse(bridge["holdout_refit_detected"])
        self.assertTrue(bridge["bridge_role_contract_complete"])
        self.assertEqual(bridge["marker_contract_violation_count"], 0)
        holdouts = [
            sample
            for sample in bridge["samples"]
            if sample["sample_role"] == "holdout"
        ]
        self.assertEqual(len(holdouts), 8)
        self.assertTrue(
            all(
                sample["sample_role_marker"] == "internal_holdout"
                and sample["fit_permitted"] is False
                and sample["holdout_fit_permitted"] is False
                for sample in holdouts
            )
        )
        self.assertFalse(bridge["simulator_patch_allowed"])
        bridge_tasks = [
            task for task in run["tasks"] if task["category"] == "white_rage"
        ]
        self.assertEqual(len(bridge_tasks), 2)
        self.assertTrue(
            all(task["completed_trials"] == 12 for task in bridge_tasks)
        )
        self.assertTrue(
            all(
                task["action_markers"]["counts_by_event"].get(
                    "CALIBRATION_ACTION_REQUESTED", 0
                )
                == 1
                for task in bridge_tasks
            )
        )
        inventories = {task["task_id"]: task for task in document["task_completions"]}
        self.assertEqual(
            inventories["warrior_fury_dual_wield_mechanics_phase12"]["analysis_status"],
            "campaign_specialized",
        )
        self.assertEqual(
            inventories["warrior_fury_action_damage_avoidance_phase12"]["analysis_status"],
            "coverage_partial",
        )

    def test_audit_keeps_partial_gate_fail_closed_and_reports_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            summary_path = root / "phase12-summary.json"
            audit_path = root / "phase12-audit.json"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows())
            summary_path.write_text(
                json.dumps(build_calibration_summary(source, registry=REGISTRY)),
                encoding="utf-8",
            )

            audit = run_from_path(
                summary_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
                output=audit_path,
            )
            audit_written = audit_path.is_file()

        self.assertEqual(audit["status"], "partial")
        self.assertEqual(audit["mechanism_gates"]["action_damage_avoidance"]["status"], "PARTIAL")
        self.assertNotIn("multi_target", audit["mechanism_gates"])
        self.assertNotIn("incoming_rage", audit["mechanism_gates"])
        self.assertEqual(
            audit["external_mechanism_boundaries"]["multi_target"]["status"],
            "PARTIAL",
        )
        self.assertEqual(
            audit["external_mechanism_boundaries"]["incoming_rage"]["status"],
            "UNRESOLVED",
        )
        self.assertFalse(audit["completion_gate"]["fully_observed_without_deferred"])
        self.assertFalse(
            audit["completion_gate"][
                "external_boundaries_count_toward_collection_complete"
            ]
        )
        self.assertFalse(audit["conclusion_gate"]["collection_complete"])
        self.assertFalse(audit["conclusion_gate"]["simulator_patch_allowed"])
        self.assertIsNone(audit["conclusion_gate"]["simulator_patch"])
        self.assertTrue(audit_written)

    def test_external_boundaries_do_not_block_dummy_collection_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            summary_path = root / "phase12-summary.json"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows(action_partial=False))
            summary_path.write_text(
                json.dumps(build_calibration_summary(source, registry=REGISTRY)),
                encoding="utf-8",
            )

            audit = run_from_path(
                summary_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        self.assertEqual(audit["status"], "complete")
        self.assertTrue(audit["conclusion_gate"]["campaign_terminal"])
        self.assertTrue(audit["conclusion_gate"]["collection_complete"])
        self.assertTrue(audit["conclusion_gate"]["fully_observed"])
        self.assertTrue(
            audit["conclusion_gate"][
                "external_boundaries_excluded_from_collection_complete"
            ]
        )
        self.assertFalse(
            audit["conclusion_gate"][
                "external_boundaries_authorize_simulator_patch"
            ]
        )
        self.assertFalse(audit["conclusion_gate"]["simulator_patch_allowed"])

    def test_cast_type_2_is_exact_queue_evidence_without_queue_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12.jsonl"
            rows = _phase12_rows(action_partial=False)
            self.assertFalse(any(row["event"] == "SPELL_QUEUE_EVENT" for row in rows))
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        run = document["specialized_campaigns"][0]
        queue = next(
            task
            for task in run["tasks"]
            if task["task_id"] == "warrior_fury_queue_execution_phase12"
        )
        self.assertTrue(queue["exact_action_chain_complete"])
        self.assertTrue(queue["trials"][0]["exact_action_chain_complete"])
        self.assertEqual(queue["trials"][0]["stage_id"], "hs_early")
        queue_step = next(
            step
            for step in queue["trials"][0]["exact_action_chain"]["steps"]
            if step["name"] == "on_swing_queue_observed"
        )
        self.assertTrue(queue_step["satisfied"])

    def test_cancel_requires_second_marker_pop_and_no_later_execution(self) -> None:
        for execute_after_pop in (False, True):
            with self.subTest(execute_after_pop=execute_after_pop):
                rows = _phase12_rows(action_partial=False)
                task_id = "warrior_fury_queue_execution_phase12"
                rows = [
                    row
                    for row in rows
                    if not (
                        (row.get("task") or {}).get("taskId") == task_id
                        and row["event"]
                        in {
                            "SPELL_START_SELF",
                            "SPELL_GO_SELF",
                            "SPELL_DAMAGE_EVENT_SELF",
                        }
                    )
                ]
                queue_rows = [
                    row
                    for row in rows
                    if (row.get("task") or {}).get("taskId") == task_id
                ]
                for row in queue_rows:
                    if isinstance(row.get("task"), dict):
                        row["task"]["phase12StageId"] = "hs_cancel"
                    if isinstance(row.get("marker"), dict):
                        row["marker"]["phase12StageID"] = "hs_cancel"
                action = next(
                    row
                    for row in queue_rows
                    if row["event"] == "CALIBRATION_ACTION_REQUESTED"
                )
                action["marker"]["action"] = "queue_cancel"
                cast = next(
                    row for row in queue_rows if row["event"] == "SPELL_CAST_EVENT"
                )
                context = dict(cast["task"])
                inserted = [
                    {
                        "sequence": 0,
                        "time": 0.0,
                        "event": "SPELL_CAST_EVENT",
                        "spellID": 25286,
                        "castSucceeded": False,
                        "castType": 2,
                        "task": context,
                        "state": dict(cast["state"]),
                    },
                    {
                        "sequence": 0,
                        "time": 0.0,
                        "event": "CALIBRATION_ACTION_REQUESTED",
                        "marker": {**context, "actionAttempt": 1},
                        "task": context,
                        "state": dict(cast["state"]),
                    },
                    {
                        "sequence": 0,
                        "time": 0.0,
                        "event": "SPELL_QUEUE_EVENT",
                        "spellID": 25286,
                        "queueEventCode": 1,
                        "task": context,
                        "state": dict(cast["state"]),
                    },
                ]
                if execute_after_pop:
                    inserted.extend(
                        [
                            {
                                "sequence": 0,
                                "time": 0.0,
                                "event": "SPELL_GO_SELF",
                                "spellID": 25286,
                                "task": context,
                                "state": dict(cast["state"]),
                            },
                            {
                                "sequence": 0,
                                "time": 0.0,
                                "event": "SPELL_DAMAGE_EVENT_SELF",
                                "spellID": 25286,
                                "task": context,
                                "state": dict(cast["state"]),
                            },
                        ]
                    )
                cast_index = rows.index(cast)
                rows[cast_index + 1:cast_index + 1] = inserted
                for sequence, row in enumerate(rows, start=1):
                    row["sequence"] = sequence
                    row["time"] = float(sequence)
                with tempfile.TemporaryDirectory() as temporary_directory:
                    source = Path(temporary_directory) / "phase12-cancel.jsonl"
                    _write_jsonl(source, rows)
                    document = build_calibration_summary(source, registry=REGISTRY)
                queue = next(
                    task
                    for task in document["specialized_campaigns"][0]["tasks"]
                    if task["task_id"] == task_id
                )
                self.assertIs(
                    queue["exact_action_chain_complete"], not execute_after_pop
                )

    def test_replace_accepts_cast_type_2_then_second_marker_and_cleave_chain(self) -> None:
        rows = _phase12_rows(action_partial=False)
        task_id = "warrior_fury_queue_execution_phase12"
        rows = [
            row
            for row in rows
            if not (
                (row.get("task") or {}).get("taskId") == task_id
                and row["event"]
                in {"SPELL_START_SELF", "SPELL_GO_SELF", "SPELL_DAMAGE_EVENT_SELF"}
            )
        ]
        queue_rows = [
            row
            for row in rows
            if (row.get("task") or {}).get("taskId") == task_id
        ]
        for row in queue_rows:
            if isinstance(row.get("task"), dict):
                row["task"]["phase12StageId"] = "hs_cleave_replace"
            if isinstance(row.get("marker"), dict):
                row["marker"]["phase12StageID"] = "hs_cleave_replace"
        action = next(
            row
            for row in queue_rows
            if row["event"] == "CALIBRATION_ACTION_REQUESTED"
        )
        action["marker"]["action"] = "queue_replace"
        cast = next(row for row in queue_rows if row["event"] == "SPELL_CAST_EVENT")
        context = dict(cast["task"])
        inserted = [
            {
                "sequence": 0,
                "time": 0.0,
                "event": "SPELL_CAST_EVENT",
                "spellID": 20569,
                "castSucceeded": True,
                "castType": 2,
                "task": context,
                "state": dict(cast["state"]),
            },
            {
                "sequence": 0,
                "time": 0.0,
                "event": "CALIBRATION_ACTION_REQUESTED",
                "marker": {**context, "actionAttempt": 1},
                "task": context,
                "state": dict(cast["state"]),
            },
            *[
                {
                    "sequence": 0,
                    "time": 0.0,
                    "event": event,
                    "spellID": 20569,
                    "task": context,
                    "state": dict(cast["state"]),
                }
                for event in (
                    "SPELL_START_SELF",
                    "SPELL_GO_SELF",
                    "SPELL_DAMAGE_EVENT_SELF",
                )
            ],
        ]
        cast_index = rows.index(cast)
        rows[cast_index + 1:cast_index + 1] = inserted
        for sequence, row in enumerate(rows, start=1):
            row["sequence"] = sequence
            row["time"] = float(sequence)
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-replace.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)
        queue = next(
            task
            for task in document["specialized_campaigns"][0]["tasks"]
            if task["task_id"] == task_id
        )
        self.assertTrue(queue["exact_action_chain_complete"])

    def test_terminal_completed_marker_cannot_hide_missing_exact_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            summary_path = root / "phase12-summary.json"
            history = root / "history"
            history.mkdir()
            rows = _phase12_rows(action_partial=False)
            rows = [
                row
                for row in rows
                if not (
                    (row.get("task") or {}).get("taskId")
                    == "warrior_fury_queue_execution_phase12"
                    and row["event"]
                    in {
                        "SPELL_CAST_EVENT",
                        "SPELL_START_SELF",
                        "SPELL_GO_SELF",
                        "SPELL_DAMAGE_EVENT_SELF",
                    }
                )
            ]
            for sequence, row in enumerate(rows, start=1):
                row["sequence"] = sequence
                row["time"] = float(sequence)
            _write_jsonl(source, rows)
            summary = build_calibration_summary(source, registry=REGISTRY)
            run = summary["specialized_campaigns"][0]
            queue = next(
                task
                for task in run["tasks"]
                if task["task_id"] == "warrior_fury_queue_execution_phase12"
            )
            self.assertFalse(queue["exact_action_chain_complete"])
            self.assertEqual(queue["retest_required_stage_ids"], ["hs_early"])
            # Simulate the old optimistic decoder: the auditor must still
            # fail closed when status says completed but exact evidence does not.
            queue["status"] = "completed"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            audit = run_from_path(
                summary_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        gate = audit["mechanism_gates"]["queue"]
        self.assertEqual(gate["status"], "PARTIAL")
        self.assertFalse(gate["exact_action_chain_complete"])
        self.assertIn(
            "current_campaign_exact_action_chain_incomplete", gate["reasons"]
        )
        self.assertIn("hs_early", audit["completion_gate"]["retest_required_stage_ids"])

    def test_repair_v2_merges_only_the_declared_exact_base_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows(action_partial=False))
            base_summary, repair_summary = _repair_summary_from_base(
                build_calibration_summary(source, registry=REGISTRY)
            )
            base_path = history / "strict-base.json"
            repair_path = root / "repair.json"
            base_path.write_text(json.dumps(base_summary), encoding="utf-8")
            repair_path.write_text(json.dumps(repair_summary), encoding="utf-8")

            audit = run_from_path(
                repair_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        self.assertEqual(audit["status"], "complete")
        self.assertEqual(
            audit["evidence_merge"]["mode"],
            "exact_base_campaign_run_plus_repair_v2",
        )
        self.assertEqual(
            audit["evidence_merge"]["base"]["campaign_run_id"],
            CAMPAIGN_RUN_ID,
        )
        self.assertFalse(
            audit["completion_gate"]["base_exact_action_chain_complete"]
        )
        self.assertTrue(
            audit["completion_gate"]["repair_exact_action_chain_complete"]
        )
        self.assertTrue(
            audit["completion_gate"]["combined_exact_action_chain_complete"]
        )
        self.assertTrue(audit["white_rage_bridge"]["coverage_complete"])
        self.assertEqual(audit["white_rage_bridge"]["clean_sample_count"], 24)
        self.assertEqual(audit["completion_gate"]["retest_required_stage_ids"], [])

    def test_eight_task_repair_v2_incomplete_terminal_audits_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows(action_partial=False))
            base_summary, repair_summary = _repair_summary_from_base(
                build_calibration_summary(source, registry=REGISTRY)
            )
            repair_run = repair_summary["specialized_campaigns"][0]
            self.assertEqual(
                [task["task_id"] for task in repair_run["tasks"]],
                list(PHASE12_REPAIR_TASK_ORDER),
            )

            queue_task = next(
                task
                for task in repair_run["tasks"]
                if task["task_id"]
                == "warrior_fury_queue_execution_phase12_repair_v2"
            )
            queue_trial = queue_task["trials"][0]
            queue_trial.update(
                {
                    "status": "incomplete",
                    "terminal_event": "CALIBRATION_TRIAL_INCOMPLETE",
                    "exact_action_chain_complete": False,
                    "exact_action_chain": {
                        "contract_version": "phase12_exact_action_chain_v1",
                        "stage_id": "hs_cancel",
                        "exact_action_chain_complete": False,
                        "steps": [],
                        "failure_reasons": ["missing_second_queue_pop"],
                    },
                }
            )
            queue_task.update(
                {
                    "status": "coverage_partial",
                    "terminal_event": "CALIBRATION_TASK_INCOMPLETE",
                    "exact_action_chain_complete": False,
                    "strictly_valid_trial_count": 0,
                    "reusable_stage_ids": [],
                    "retest_required_stage_ids": ["hs_cancel"],
                }
            )
            repair_run.update(
                {
                    "status": "coverage_partial",
                    "terminal_event": "CALIBRATION_CAMPAIGN_INCOMPLETE",
                    "terminal_confirmed": True,
                    "completion_confirmed": False,
                    "exact_action_chain_complete": False,
                    "retest_required_stage_ids": ["hs_cancel"],
                }
            )

            base_path = history / "strict-base.json"
            repair_path = root / "repair-incomplete.json"
            base_path.write_text(json.dumps(base_summary), encoding="utf-8")
            repair_path.write_text(json.dumps(repair_summary), encoding="utf-8")
            audit = run_from_path(
                repair_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        self.assertEqual(audit["status"], "partial")
        self.assertEqual(
            audit["evidence_merge"]["mode"],
            "exact_base_campaign_run_plus_repair_v2",
        )
        self.assertFalse(audit["completion_gate"]["campaign_completed"])
        self.assertFalse(
            audit["completion_gate"]["repair_exact_action_chain_complete"]
        )
        self.assertFalse(
            audit["completion_gate"]["combined_exact_action_chain_complete"]
        )
        self.assertEqual(audit["mechanism_gates"]["queue"]["status"], "PARTIAL")
        self.assertIn(
            "hs_cancel", audit["completion_gate"]["retest_required_stage_ids"]
        )
        self.assertEqual(
            audit["completion_gate"]["observed_task_order"],
            list(PHASE12_TASK_ORDER),
        )
        self.assertFalse(audit["conclusion_gate"]["simulator_patch_allowed"])

    def test_repair_merge_preserves_per_sample_phase12_armor_relocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            history = root / "history"
            history.mkdir()
            rows = _phase12_rows(action_partial=False)

            # This is one bridge cell split two samples into the base run and
            # one into repair_v2.  The shared dummy lost an unrelated armor
            # debuff before the repair sample, so the stratum was legitimately
            # relocked between samples.  Each individual sample remains exact.
            for row in rows:
                marker = row.get("marker", {})
                if (
                    row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
                    and marker.get("taskId")
                    == "warrior_white_swing_rage_bridge_sword_phase12"
                    and marker.get("plannedSunderStacks") == 5
                    and marker.get("sampleQuota") == "critical"
                ):
                    armor = 1961 if marker.get("trial") == 12 else 1721
                    marker["targetArmor"] = armor
                    marker["stratumTargetArmor"] = armor

            _write_jsonl(source, rows)
            complete_summary = build_calibration_summary(source, registry=REGISTRY)
            complete_samples = complete_summary["specialized_campaigns"][0][
                "white_rage_bridge"
            ]["samples"]
            complete_cell = [
                sample
                for sample in complete_samples
                if sample["weapon_item_id"] == 17076
                and sample["sunder_stacks"] == 5
                and sample["outcome"] == "critical"
            ]
            self.assertEqual(
                [sample["target_armor"] for sample in complete_cell],
                [1721, 1721, 1961],
            )
            self.assertTrue(
                all(
                    sample["target_armor"] == sample["stratum_target_armor"]
                    for sample in complete_cell
                )
            )

            base_summary, repair_summary = _repair_summary_from_base(
                complete_summary
            )
            base_path = history / "strict-base.json"
            repair_path = root / "repair.json"
            base_path.write_text(json.dumps(base_summary), encoding="utf-8")
            repair_path.write_text(json.dumps(repair_summary), encoding="utf-8")
            audit = run_from_path(
                repair_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        combined_cell = [
            sample
            for sample in audit["white_rage_bridge"]["samples"]
            if sample["weapon_item_id"] == 17076
            and sample["sunder_stacks"] == 5
            and sample["outcome"] == "critical"
        ]
        self.assertEqual(
            [sample["target_armor"] for sample in combined_cell],
            [1721, 1721, 1961],
        )
        self.assertEqual(
            [sample["combined_evidence_source"] for sample in combined_cell],
            ["base", "base", "repair"],
        )
        self.assertTrue(
            all(
                sample["target_armor"] == sample["stratum_target_armor"]
                for sample in combined_cell
            )
        )
        self.assertTrue(audit["white_rage_bridge"]["coverage_complete"])
        self.assertEqual(audit["white_rage_bridge"]["clean_sample_count"], 24)

    def test_repair_v2_never_falls_back_to_another_base_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows(action_partial=False))
            base_summary, repair_summary = _repair_summary_from_base(
                build_calibration_summary(source, registry=REGISTRY)
            )
            repair_summary["specialized_campaigns"][0][
                "base_campaign_run_id"
            ] = "campaign-that-does-not-exist"
            base_path = history / "strict-base.json"
            repair_path = root / "repair.json"
            base_path.write_text(json.dumps(base_summary), encoding="utf-8")
            repair_path.write_text(json.dumps(repair_summary), encoding="utf-8")

            with self.assertRaisesRegex(
                FuryPhase12AuditError, "exact baseCampaignRunId"
            ):
                run_from_path(
                    repair_path,
                    DEFAULT_PREREGISTRATION,
                    history_directory=history,
                )

    def test_summary_recognizes_repair_v2_metadata_and_base_task_id(self) -> None:
        repair_run_id = "campaign-repair-raw-fixture"
        repair_task_id = (
            "warrior_fury_dual_wield_mechanics_phase12_repair_v2"
        )
        base_task_id = "warrior_fury_dual_wield_mechanics_phase12"
        task_run_id = "task-repair-cancel"
        common = {
            "campaignId": PHASE12_REPAIR_CAMPAIGN_ID,
            "campaignRunId": repair_run_id,
            "baseCampaignId": CAMPAIGN_ID,
            "baseCampaignRunId": CAMPAIGN_RUN_ID,
            "collectorRevision": 2,
            "campaignStep": 1,
            "campaignTaskCount": 1,
            "taskId": repair_task_id,
            "baseTaskId": base_task_id,
            "taskRunId": task_run_id,
            "completionKind": "fury_phase12_scripted",
            "requiredTrials": 1,
            "trial": 1,
            "phase12StageID": "dual_hs_cancel",
        }
        rows: list[dict] = []

        def add(event: str, **fields: object) -> None:
            row = {
                "sequence": len(rows) + 1,
                "time": float(len(rows) + 1),
                "event": event,
                "state": {"rage": 50, "inCombat": True},
                **fields,
            }
            rows.append(row)

        campaign = {
            key: common[key]
            for key in (
                "campaignId",
                "campaignRunId",
                "baseCampaignId",
                "baseCampaignRunId",
                "collectorRevision",
                "campaignTaskCount",
            )
        }
        add("CALIBRATION_CAMPAIGN_STARTED", marker=campaign)
        add("CALIBRATION_TASK_STARTED", marker=common, task=common)
        add("CALIBRATION_TRIAL_STARTED", marker=common, task=common)
        add(
            "CALIBRATION_ACTION_REQUESTED",
            marker={**common, "action": "queue_cancel", "actionAttempt": 1},
            task=common,
        )
        add(
            "SPELL_CAST_EVENT",
            spellID=25286,
            castSucceeded=True,
            castType=2,
            task=common,
        )
        add(
            "SPELL_CAST_EVENT",
            spellID=25286,
            castSucceeded=False,
            castType=2,
            task=common,
        )
        add(
            "CALIBRATION_ACTION_REQUESTED",
            marker={**common, "actionAttempt": 1},
            task=common,
        )
        add(
            "SPELL_QUEUE_EVENT",
            spellID=25286,
            queueEventCode=1,
            task=common,
        )
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            marker={
                **common,
                "phase12Coverage": "observed",
                "completionSource": "strict_stage_transaction",
            },
            task=common,
        )
        add(
            "CALIBRATION_TASK_COMPLETED",
            marker={
                **common,
                "phase12Coverage": "observed",
                "completionSource": "strict_stage_transaction",
            },
        )
        add("CALIBRATION_CAMPAIGN_COMPLETED", marker=campaign)

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "repair.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        run = document["specialized_campaigns"][0]
        self.assertEqual(run["campaign_id"], PHASE12_REPAIR_CAMPAIGN_ID)
        self.assertEqual(run["campaign_kind"], "repair_v2")
        self.assertEqual(run["base_campaign_run_id"], CAMPAIGN_RUN_ID)
        self.assertEqual(run["collector_revision"], 2)
        self.assertTrue(run["exact_action_chain_complete"])
        self.assertEqual(run["tasks"][0]["base_task_id"], base_task_id)
        self.assertTrue(run["tasks"][0]["exact_action_chain_complete"])

    def test_clean_ordinal_role_survives_stale_emitted_fit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            rows = _phase12_rows(action_partial=False)
            holdout = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
                and row["marker"].get("bridgeModelUse") == "internal_holdout"
            )
            holdout["marker"]["fitPermitted"] = True
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertTrue(bridge["coverage_complete"])
        self.assertFalse(bridge["holdout_refit_detected"])
        self.assertTrue(
            bridge["emitted_holdout_fit_permission_conflict_detected"]
        )
        self.assertEqual(bridge["marker_contract_violation_count"], 1)
        self.assertEqual(
            bridge["role_assignment_basis"],
            "preregistered_clean_acceptance_ordinal_first_two_identification_third_holdout",
        )

    def test_bridge_excludes_accepted_sample_inside_item_17076_proc_interval(
        self,
    ) -> None:
        for active_event, inactive_event, marker_state in (
            ("AURA_CAST_ON_SELF", "BUFF_REMOVED_SELF", False),
            ("BUFF_ADDED_SELF", "BUFF_REMOVED_SELF", False),
            ("DEBUFF_ADDED_OTHER", "DEBUFF_REMOVED_OTHER", False),
            (
                "CALIBRATION_BRIDGE_PROC_AURA_STATE",
                "CALIBRATION_BRIDGE_PROC_AURA_STATE",
                True,
            ),
        ):
            with self.subTest(active_event=active_event, marker_state=marker_state):
                rows = _phase12_rows(action_partial=False)
                accepted_index = next(
                    index
                    for index, row in enumerate(rows)
                    if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
                    and row["marker"].get("mainHandItemID") == 17076
                )
                accepted = rows[accepted_index]
                task_context = dict(accepted["task"])
                swing = {
                    "sequence": 0,
                    "time": 0.0,
                    "event": "AUTO_ATTACK_SELF",
                    "amount": accepted["marker"]["damageAmount"],
                    "hitInfo": 0,
                    "task": task_context,
                    "state": dict(accepted["state"]),
                }
                proc_added = {
                    "sequence": 0,
                    "time": 0.0,
                    "event": active_event,
                    "spellID": 21153,
                    "task": task_context,
                    "state": dict(accepted["state"]),
                }
                proc_removed = {
                    "sequence": 0,
                    "time": 0.0,
                    "event": inactive_event,
                    "spellID": 21153,
                    "task": task_context,
                    "state": dict(accepted["state"]),
                }
                if marker_state:
                    proc_added["marker"] = {
                        **task_context,
                        "phase": "phase12_bridge_proc_aura_active",
                        "procAuraSpellID": 21153,
                        "procAuraActive": True,
                    }
                    proc_removed["marker"] = {
                        **task_context,
                        "phase": "phase12_bridge_proc_aura_removed",
                        "procAuraSpellID": 21153,
                        "procAuraActive": False,
                    }
                rows[accepted_index:accepted_index + 1] = [
                    swing,
                    proc_added,
                    accepted,
                    proc_removed,
                ]
                for sequence, row in enumerate(rows, start=1):
                    row["sequence"] = sequence
                    row["time"] = float(sequence)
                accepted["marker"]["swingSequence"] = swing["sequence"]

                with tempfile.TemporaryDirectory() as temporary_directory:
                    source = Path(temporary_directory) / "phase12-proc.jsonl"
                    _write_jsonl(source, rows)
                    document = build_calibration_summary(source, registry=REGISTRY)

                bridge = document["specialized_campaigns"][0][
                    "white_rage_bridge"
                ]
                self.assertEqual(bridge["accepted_marker_count"], 24)
                self.assertEqual(bridge["clean_sample_count"], 23)
                self.assertEqual(bridge["contaminated_accepted_marker_count"], 1)
                self.assertEqual(bridge["known_proc_aura_active_sample_count"], 1)
                self.assertFalse(bridge["coverage_complete"])
                self.assertEqual(
                    bridge["coverage"]["item_17076_sunder_0_ordinary"]
                    ["accepted_clean_samples"],
                    2,
                )
                proc_control = bridge["known_proc_aura_control"]
                self.assertEqual(proc_control["spell_id"], 21153)
                self.assertIn("BUFF_ADDED_SELF", proc_control["active_events"])
                self.assertIn("DEBUFF_ADDED_OTHER", proc_control["active_events"])
                self.assertIn("BUFF_REMOVED_SELF", proc_control["inactive_events"])
                self.assertEqual(
                    proc_control["accepted_samples_excluded_while_active"], 1
                )
                self.assertEqual(len(proc_control["intervals"]), 1)

    def test_bridge_does_not_back_attribute_proc_observed_after_acceptance(
        self,
    ) -> None:
        rows = _phase12_rows(action_partial=False)
        accepted_index = next(
            index
            for index, row in enumerate(rows)
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["marker"].get("mainHandItemID") == 17076
        )
        accepted = rows[accepted_index]
        task_context = dict(accepted["task"])
        proc_rows = [
            {
                "sequence": 0,
                "time": 0.0,
                "event": event,
                "spellID": 21153,
                "task": task_context,
                "state": dict(accepted["state"]),
            }
            for event in ("BUFF_ADDED_SELF", "BUFF_REMOVED_SELF")
        ]
        rows[accepted_index + 1:accepted_index + 1] = proc_rows
        for sequence, row in enumerate(rows, start=1):
            row["sequence"] = sequence
            row["time"] = float(sequence)

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-future-proc.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertEqual(bridge["clean_sample_count"], 24)
        self.assertEqual(bridge["known_proc_aura_active_sample_count"], 0)
        self.assertTrue(bridge["coverage_complete"])

    def test_bridge_treats_unclosed_item_17076_proc_interval_as_active(
        self,
    ) -> None:
        rows = _phase12_rows(action_partial=False)
        accepted_indices = [
            index
            for index, row in enumerate(rows)
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["marker"].get("mainHandItemID") == 17076
        ]
        accepted_index = accepted_indices[-1]
        expected_excluded = sum(
            row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            for row in rows[accepted_index:]
        )
        accepted = rows[accepted_index]
        task_context = dict(accepted["task"])
        swing = {
            "sequence": 0,
            "time": 0.0,
            "event": "AUTO_ATTACK_SELF",
            "amount": accepted["marker"]["damageAmount"],
            "hitInfo": 0,
            "task": task_context,
            "state": dict(accepted["state"]),
        }
        proc_added = {
            "sequence": 0,
            "time": 0.0,
            "event": "AURA_CAST_ON_SELF",
            "spellID": 21153,
            "task": task_context,
            "state": dict(accepted["state"]),
        }
        rows[accepted_index:accepted_index + 1] = [swing, proc_added, accepted]
        for sequence, row in enumerate(rows, start=1):
            row["sequence"] = sequence
            row["time"] = float(sequence)
        accepted["marker"]["swingSequence"] = swing["sequence"]

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-open-proc.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertEqual(
            bridge["known_proc_aura_active_sample_count"], expected_excluded
        )
        self.assertEqual(bridge["clean_sample_count"], 24 - expected_excluded)
        self.assertFalse(bridge["coverage_complete"])
        self.assertIsNone(
            bridge["known_proc_aura_control"]["intervals"][0]["end_sequence"]
        )

    def test_bridge_excludes_flurry_active_accepted_sample(self) -> None:
        rows = _phase12_rows(action_partial=False)
        contaminated = next(
            row
            for row in rows
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["marker"].get("mainHandItemID") == 17076
            and row["marker"].get("sampleQuota") == "ordinary"
        )
        contaminated["marker"]["flurryActive"] = True

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-flurry.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertEqual(bridge["accepted_marker_count"], 24)
        self.assertEqual(bridge["clean_sample_count"], 23)
        self.assertEqual(bridge["contaminated_accepted_marker_count"], 1)
        self.assertFalse(bridge["coverage_complete"])
        self.assertEqual(
            bridge["coverage"]["item_17076_sunder_0_ordinary"]
            ["accepted_clean_samples"],
            2,
        )

    def test_bridge_critical_flurry_events_do_not_back_attribute_pre_swing_buff(
        self,
    ) -> None:
        rows = _phase12_rows(action_partial=False)
        accepted = next(
            row
            for row in rows
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["marker"].get("sampleQuota") == "critical"
        )
        swing = next(
            row
            for row in rows
            if row["sequence"] == accepted["marker"]["swingSequence"]
        )
        resource = next(
            row
            for row in rows
            if row["sequence"] == accepted["marker"]["resourceSequence"]
        )
        swing_index = rows.index(swing)
        task_context = dict(accepted["task"])
        aura_cast = {
            "sequence": 0,
            "time": 0.0,
            "event": "AURA_CAST_ON_SELF",
            "spellID": 12970,
            "task": task_context,
            "state": dict(accepted["state"]),
        }
        buff_added = {
            "sequence": 0,
            "time": 0.0,
            "event": "BUFF_ADDED_SELF",
            "spellID": 12970,
            "task": task_context,
            "state": dict(accepted["state"]),
        }
        rows[swing_index:swing_index] = [aura_cast]
        rows[rows.index(swing) + 1:rows.index(swing) + 1] = [buff_added]
        for sequence, row in enumerate(rows, start=1):
            row["sequence"] = sequence
            row["time"] = float(sequence)
        accepted["marker"]["swingSequence"] = swing["sequence"]
        accepted["marker"]["resourceSequence"] = resource["sequence"]

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-crit-flurry.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertEqual(bridge["clean_sample_count"], 24)
        self.assertTrue(bridge["coverage_complete"])
        self.assertFalse(accepted["marker"]["flurryActive"])

    def test_bridge_excludes_attack_power_mismatch_accepted_sample(self) -> None:
        rows = _phase12_rows(action_partial=False)
        contaminated = next(
            row
            for row in rows
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["marker"].get("mainHandItemID") == 17076
            and row["marker"].get("sampleQuota") == "ordinary"
        )
        contaminated["marker"]["attackPower"] = 1192
        contaminated["marker"]["referenceAttackPower"] = 992

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase12-attack-power.jsonl"
            _write_jsonl(source, rows)
            document = build_calibration_summary(source, registry=REGISTRY)

        bridge = document["specialized_campaigns"][0]["white_rage_bridge"]
        self.assertEqual(bridge["accepted_marker_count"], 24)
        self.assertEqual(bridge["clean_sample_count"], 23)
        self.assertEqual(bridge["contaminated_accepted_marker_count"], 1)
        self.assertFalse(bridge["coverage_complete"])
        self.assertEqual(
            bridge["coverage"]["item_17076_sunder_0_ordinary"]
            ["accepted_clean_samples"],
            2,
        )
        self.assertEqual(
            bridge["attribution_control"]
            ["attack_power_mismatch_samples_excluded"],
            1,
        )

    def test_reused_history_mirror_has_one_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "phase12.jsonl"
            summary_path = root / "phase12-summary.json"
            history = root / "history"
            history.mkdir()
            _write_jsonl(source, _phase12_rows(action_partial=False))
            summary_path.write_text(
                json.dumps(build_calibration_summary(source, registry=REGISTRY)),
                encoding="utf-8",
            )
            task = {
                "task_id": "warrior_execute_transition",
                "task_run_id": "execute-run-1",
                "status": "completed",
                "analysis_status": "detailed",
            }
            for name, imported_at in (
                ("origin.json", "2026-08-29T01:00:00Z"),
                ("mirror.json", "2026-08-30T01:00:00Z"),
            ):
                (history / name).write_text(
                    json.dumps(
                        {
                            "kind": "brainofcat_calibration_summary",
                            "source": {
                                "calibration_jsonl": f"calibration/{name}.jsonl",
                                "raw_file": f"online_raw/{name}.lua",
                                "imported_at": imported_at,
                            },
                            "task_completions": [task],
                        }
                    ),
                    encoding="utf-8",
                )

            audit = run_from_path(
                summary_path,
                DEFAULT_PREREGISTRATION,
                history_directory=history,
            )

        reused = audit["mechanism_gates"]["action_damage_avoidance"]["reused_tasks"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["mirror_summary_count"], 1)
        self.assertTrue(
            reused[0]["evidence_origin"]["summary"].endswith("origin.json")
        )

    def test_watcher_triggers_phase12_audit_for_completed_specialized_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables(CAMPAIGN_RUN_ID), encoding="utf-8")
            data_root = root / "offline_data"
            summary_path = root / "phase12-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_campaigns": [
                            {
                                "campaign_id": CAMPAIGN_ID,
                                "campaign_run_id": CAMPAIGN_RUN_ID,
                                "analyzer": "fury_current_build_phase12_v2",
                                "completion_confirmed": True,
                                "status": "completed",
                                "declared_task_count": 10,
                                "observed_task_count": 10,
                                "task_count_consistent": True,
                                "tasks": [
                                    {"task_id": task_id}
                                    for task_id in PHASE12_TASK_ORDER
                                ],
                            }
                        ],
                        "specialized_runs": [],
                        "task_completions": [],
                    }
                ),
                encoding="utf-8",
            )
            calls: list[Path] = []

            def fake_summarizer(_calibration: str | Path) -> SimpleNamespace:
                return SimpleNamespace(
                    as_dict=lambda: {
                        "status": "ok",
                        "run_count": 0,
                        "trial_count": 0,
                        "output": str(summary_path),
                    }
                )

            def fake_phase12_auditor(path: Path) -> dict:
                calls.append(path)
                gates = {
                    name: {"status": "COMPLETE"}
                    for name in MECHANISM_GATE_NAMES
                }
                boundaries = {
                    name: {
                        "status": "PARTIAL",
                        "collection_complete_contribution": False,
                        "simulator_patch_allowed": False,
                    }
                    for name in EXTERNAL_MECHANISM_BOUNDARY_NAMES
                }
                return {
                    "schema_version": 1,
                    "kind": "fury_current_build_phase12_audit",
                    "status": "complete",
                    "campaign_run_id": CAMPAIGN_RUN_ID,
                    "mechanism_gates": gates,
                    "external_mechanism_boundaries": boundaries,
                    "conclusion_gate": {
                        "campaign_terminal": True,
                        "collection_complete": True,
                        "fully_observed": True,
                        "replacement_formula_identified": False,
                        "holdout_refit_permitted": False,
                        "simulator_patch_allowed": False,
                        "simulator_patch": None,
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=fake_summarizer,
                phase12_auditor=fake_phase12_auditor,
            )
            result = watcher.process_once()

            self.assertEqual(
                result["status"],
                "campaign_imported_phase12_complete",
            )
            self.assertEqual(calls, [summary_path])
            self.assertEqual(result["phase12_audit"]["status"], "complete")
            self.assertEqual(
                tuple(result["phase12_audit"]["external_boundary_statuses"]),
                EXTERNAL_MECHANISM_BOUNDARY_NAMES,
            )
            self.assertFalse(result["phase12_audit"]["simulator_patch_allowed"])
            self.assertTrue(
                (data_root / "sim_validation" / "fury_current_build_phase12_audit.json").is_file()
            )
            state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["processed_campaigns"][0]["phase12_audit"]["status"],
                "complete",
            )

    def test_watcher_ignores_phase12_with_incomplete_task_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_campaigns": [
                            {
                                "campaign_id": CAMPAIGN_ID,
                                "analyzer": "fury_current_build_phase12_v2",
                                "completion_confirmed": True,
                                "declared_task_count": 10,
                                "observed_task_count": 9,
                                "task_count_consistent": False,
                                "tasks": [
                                    {"task_id": task_id}
                                    for task_id in PHASE12_TASK_ORDER[:-1]
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            detected = _summary_contains_terminal_phase12(str(summary_path))

        self.assertFalse(detected)

    def test_watcher_detects_completed_repair_v2_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_campaigns": [
                            {
                                "campaign_id": PHASE12_REPAIR_CAMPAIGN_ID,
                                "analyzer": "fury_current_build_phase12_v2",
                                "completion_confirmed": True,
                                "declared_task_count": len(
                                    PHASE12_REPAIR_TASK_ORDER
                                ),
                                "observed_task_count": len(
                                    PHASE12_REPAIR_TASK_ORDER
                                ),
                                "task_count_consistent": True,
                                "tasks": [
                                    {"task_id": task_id}
                                    for task_id in PHASE12_REPAIR_TASK_ORDER
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            detected = _summary_contains_terminal_phase12(str(summary_path))

        self.assertTrue(detected)

    def test_watcher_detects_incomplete_terminal_repair_v2_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_campaigns": [
                            {
                                "campaign_id": PHASE12_REPAIR_CAMPAIGN_ID,
                                "analyzer": "fury_current_build_phase12_v2",
                                "terminal_confirmed": True,
                                "completion_confirmed": False,
                                "declared_task_count": len(
                                    PHASE12_REPAIR_TASK_ORDER
                                ),
                                "observed_task_count": len(
                                    PHASE12_REPAIR_TASK_ORDER
                                ),
                                "task_count_consistent": True,
                                "tasks": [
                                    {"task_id": task_id}
                                    for task_id in PHASE12_REPAIR_TASK_ORDER
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            detected = _summary_contains_terminal_phase12(str(summary_path))

        self.assertTrue(detected)

    def test_watcher_ignores_superseded_complete_campaign_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "kind": "brainofcat_calibration_summary",
                        "specialized_campaigns": [
                            {
                                "campaign_id": "warrior_fury_current_build_complete_phase12",
                                "analyzer": "fury_current_build_phase12_v1",
                                "completion_confirmed": True,
                                "declared_task_count": 10,
                                "observed_task_count": 10,
                                "task_count_consistent": True,
                                "tasks": [
                                    {"task_id": task_id}
                                    for task_id in PHASE12_TASK_ORDER
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            detected = _summary_contains_terminal_phase12(str(summary_path))

        self.assertFalse(detected)


if __name__ == "__main__":
    unittest.main()
