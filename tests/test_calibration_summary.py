from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_summary import (
    CalibrationSummaryError,
    build_calibration_summary,
    main,
    summarize_calibration,
)


REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
RUN_ID = "Warrior-10000-1"
TASK_ID = "warrior_bloodthirst_transition"


def _state(
    *,
    rage: int = 98,
    gcd: float = 0,
    cooldown: float = 0,
    whirlwind_cooldown: float = 0,
) -> dict:
    return {
        "rage": rage,
        "gcd": gcd,
        "latencyMilliseconds": 22,
        "playerLevel": 60,
        "attackPower": {
            "base": 1000,
            "positive": 250,
            "negative": 0,
            "effective": 1250,
        },
        "targetGUID": "0xF13000C552000001",
        "targetName": "Raid Training Dummy",
        "targetLevel": -1,
        "targetArmor": {
            "base": 4211,
            "effective": 4211,
            "armor": 4211,
            "positive": 0,
            "negative": 0,
        },
        "mainHandSpeed": 3.5,
        "offHandSpeed": 2.6,
        "cooldowns": {
            "bloodthirst": cooldown,
            "whirlwind": whirlwind_cooldown,
        },
        "talents": [
            {
                "tab": 1,
                "index": 1,
                "name": "Improved Heroic Strike",
                "tier": 1,
                "column": 1,
                "rank": 3,
                "maxRank": 3,
            },
            {
                "tab": 2,
                "index": 13,
                "name": "Improved Execute",
                "tier": 5,
                "column": 4,
                "rank": 2,
                "maxRank": 2,
            },
            {
                "tab": 2,
                "index": 11,
                "name": "Ravager",
                "tier": 5,
                "column": 1,
                "rank": 2,
                "maxRank": 3,
            },
        ],
    }


def _task_context() -> dict:
    return {
        "taskRunId": RUN_ID,
        "taskId": TASK_ID,
        "trial": 1,
        "requiredTrials": 1,
    }


def _completed_rows(
    *,
    with_resource: bool = False,
    confounded: bool = False,
) -> list[dict]:
    telemetry = {
        "nampowerDetected": True,
        "nampowerVersion": "4.1.0",
        "typedCalibrationSupported": True,
        "registeredEventCount": 48,
        "cvars": {"NP_EnableSpellGoEvents": "1"},
    }
    rows = [
        {
            "sequence": 1,
            "time": 5.0,
            "event": "CALIBRATION_TASK_STARTED",
            "state": _state(rage=100),
            "task": _task_context(),
            "marker": {
                "taskRunId": RUN_ID,
                "taskId": TASK_ID,
                "phase": "started",
                "requiredTrials": 1,
                "trial": 1,
                "telemetry": telemetry,
            },
            "provenance": {
                "source_file": "BrainOfCat.lua",
                "raw_file": "online_raw/example/BrainOfCat.lua",
                "imported_at": "2026-08-29T10:40:47Z",
            },
        },
        {
            "sequence": 2,
            "time": 5.001,
            "event": "CALIBRATION_TRIAL_STARTED",
            "state": _state(rage=100),
            "task": _task_context(),
            "marker": {
                "taskRunId": RUN_ID,
                "taskId": TASK_ID,
                "phase": "trial_started",
                "requiredTrials": 1,
                "trial": 1,
            },
        },
        {
            "sequence": 3,
            "time": 10.0,
            "event": "SPELL_CAST_EVENT",
            "spellID": 23894,
            "castSucceeded": True,
            "state": _state(rage=98, cooldown=1.5),
            "task": _task_context(),
        },
        {
            "sequence": 4,
            "time": 10.041,
            "event": "SPELL_START_SELF",
            "spellID": 23894,
            "castTimeMilliseconds": 0,
            "castDurationMilliseconds": 0,
            "state": _state(rage=98, cooldown=1.459),
            "task": _task_context(),
        },
        {
            "sequence": 5,
            "time": 10.076,
            "event": "SPELL_GO_SELF",
            "spellID": 23894,
            "state": _state(rage=98, gcd=1.5, cooldown=1.424),
            "task": _task_context(),
        },
        {
            "sequence": 6,
            "time": 10.079,
            "event": "SPELL_UPDATE_COOLDOWN",
            "state": _state(rage=98, gcd=1.497, cooldown=5.998),
            "task": _task_context(),
        },
        {
            "sequence": 7,
            "time": 10.079,
            "event": "SPELL_DAMAGE_EVENT_SELF",
            "spellID": 23894,
            "amount": 322,
            "hitInfo": 0,
            "mitigation": "0,0,0",
            "spellSchool": 0,
            "state": _state(rage=98, gcd=1.497, cooldown=5.998),
            "task": _task_context(),
        },
    ]

    resource_sequence = None
    rage_after = None
    if confounded:
        rows.append(
            {
                "sequence": 8,
                "time": 10.08,
                "event": "AUTO_ATTACK_SELF",
                "amount": 548,
                "state": _state(rage=98, gcd=1.496, cooldown=5.997),
                "task": _task_context(),
            }
        )
    if with_resource:
        resource_sequence = 9 if confounded else 8
        rage_after = 68
        rows.append(
            {
                "sequence": resource_sequence,
                "time": 10.081,
                "event": "UNIT_RAGE",
                "unit": "player",
                "state": _state(rage=rage_after, gcd=1.495, cooldown=5.996),
                "task": _task_context(),
            }
        )

    trial_completion_sequence = len(rows) + 1
    trial_marker = {
        "taskRunId": RUN_ID,
        "taskId": TASK_ID,
        "phase": "trial_completed",
        "trial": 1,
        "requiredTrials": 1,
        "trialStartSequence": 2,
        "actionStartSequence": 3,
        "resultSequence": 7,
        "resultEvent": "SPELL_DAMAGE_EVENT_SELF",
        "triggerSequence": resource_sequence or 7,
        "endSequence": trial_completion_sequence,
        "completionSource": "automatic_typed_event",
    }
    if with_resource:
        trial_marker.update(
            {
                "resourceSeen": True,
                "resourceEvent": "UNIT_RAGE",
                "resourceSequence": resource_sequence,
                "rageBefore": 98,
                "rageAfter": rage_after,
                "rageDelta": -30,
            }
        )
    rows.append(
        {
            "sequence": trial_completion_sequence,
            "time": 10.082,
            "event": "CALIBRATION_TRIAL_COMPLETED",
            "state": _state(rage=rage_after or 98, gcd=1.494, cooldown=5.995),
            "task": _task_context(),
            "marker": trial_marker,
        }
    )
    task_completion_sequence = trial_completion_sequence + 1
    rows.append(
        {
            "sequence": task_completion_sequence,
            "time": 10.083,
            "event": "CALIBRATION_TASK_COMPLETED",
            "state": _state(rage=rage_after or 98, gcd=1.493, cooldown=5.994),
            "task": _task_context(),
            "marker": {
                "taskRunId": RUN_ID,
                "taskId": TASK_ID,
                "phase": "completed",
                "trial": 1,
                "requiredTrials": 1,
                "taskStartSequence": 1,
                "endSequence": task_completion_sequence,
                "completionSource": "automatic_typed_event",
            },
        }
    )
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _parameters(run: dict) -> dict[str, dict]:
    return {
        parameter["registry_target"]["field"]: parameter
        for parameter in run["parameters"]
    }


def _campaign_rows() -> list[dict]:
    campaign_id = "warrior_fury_dummy_phase1"
    campaign_run_id = "Warrior-campaign-1"
    rows = _completed_rows(with_resource=True)
    sequence_reference_keys = {
        "trialStartSequence",
        "actionStartSequence",
        "resultSequence",
        "triggerSequence",
        "resourceSequence",
        "taskStartSequence",
        "endSequence",
    }
    for row in rows:
        row["sequence"] += 1
        marker = row.get("marker")
        if isinstance(marker, dict):
            for key in sequence_reference_keys:
                if type(marker.get(key)) is int:
                    marker[key] += 1
            marker.update(
                {
                    "campaignId": campaign_id,
                    "campaignRunId": campaign_run_id,
                    "campaignStep": 1,
                    "campaignTaskCount": 4,
                }
            )
        task = row.get("task")
        if isinstance(task, dict):
            task.update(
                {
                    "campaignId": campaign_id,
                    "campaignRunId": campaign_run_id,
                    "campaignStep": 1,
                    "campaignTaskCount": 4,
                }
            )

    rows.insert(
        0,
        {
            "sequence": 1,
            "time": 1.0,
            "event": "CALIBRATION_CAMPAIGN_STARTED",
            "state": _state(rage=0),
            "marker": {
                "campaignId": campaign_id,
                "campaignRunId": campaign_run_id,
                "campaignStep": 1,
                "campaignTaskCount": 4,
                "status": "running",
            },
        },
    )
    clock = max(float(row["time"]) for row in rows) + 1

    def add(
        event: str,
        *,
        context: dict | None = None,
        state: dict | None = None,
        marker: dict | None = None,
        advance: float = 0.01,
        **fields: object,
    ) -> dict:
        nonlocal clock
        sequence = rows[-1]["sequence"] + 1
        clock += advance
        row = {
            "sequence": sequence,
            "time": clock,
            "event": event,
            "state": state if state is not None else _state(),
        }
        if context is not None:
            row["task"] = dict(context)
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    def context(task_id: str, run_id: str, step: int, trial: int, total: int) -> dict:
        return {
            "taskRunId": run_id,
            "taskId": task_id,
            "trial": trial,
            "requiredTrials": total,
            "campaignId": campaign_id,
            "campaignRunId": campaign_run_id,
            "campaignStep": step,
            "campaignTaskCount": 4,
        }

    hs_task = "warrior_heroic_strike_queue_swing"
    hs_run = "Warrior-hs-1"
    hs_start_context = context(hs_task, hs_run, 2, 1, 2)
    hs_task_start = add(
        "CALIBRATION_TASK_STARTED",
        context=hs_start_context,
        marker=dict(hs_start_context),
    )
    for trial in (1, 2):
        trial_context = context(hs_task, hs_run, 2, trial, 2)
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            context=trial_context,
            state=_state(rage=50, gcd=0),
            marker=dict(trial_context),
        )
        action = add(
            "SPELL_CAST_EVENT",
            context=trial_context,
            state=_state(rage=50, gcd=0),
            spellID=25286,
            castSucceeded=True,
            castType=2,
        )
        if trial == 2:
            add(
                "SPELL_QUEUE_EVENT",
                context=trial_context,
                state=_state(rage=50, gcd=0),
                spellID=25286,
                queueEventCode=0,
            )
            add(
                "SPELL_QUEUE_EVENT",
                context=trial_context,
                state=_state(rage=50, gcd=0),
                advance=1.0,
                spellID=25286,
                queueEventCode=1,
            )
        add(
            "SPELL_GO_SELF",
            context=trial_context,
            state=_state(rage=50, gcd=0),
            spellID=25286,
        )
        result = add(
            "SPELL_MISS_SELF" if trial == 2 else "SPELL_DAMAGE_EVENT_SELF",
            context=trial_context,
            state=_state(rage=50, gcd=0),
            spellID=25286,
            amount=None if trial == 2 else 500 + trial,
            hitInfo=None if trial == 2 else 0,
            missInfo=1 if trial == 2 else None,
            targetGUID="0xF13000C552000001",
        )
        resource = add(
            "UNIT_RAGE",
            context=trial_context,
            state=_state(rage=35, gcd=0),
            unit="player",
        )
        completion_sequence = rows[-1]["sequence"] + 1
        trial_marker = dict(trial_context)
        trial_marker.update(
            {
                "trialStartSequence": trial_start["sequence"],
                "actionStartSequence": action["sequence"],
                "resultSequence": result["sequence"],
                "resourceSequence": resource["sequence"],
                "triggerSequence": resource["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
                "queueSeen": True,
                "queuePoppedSeen": trial == 2,
                "queueEvidence": (
                    "spell_cast_event_on_swing"
                    if trial == 1
                    else "nampower_on_swing_buffer"
                ),
                "serverGoSeen": True,
                "resultSeen": True,
                "resourceSeen": True,
                "rageBefore": 50,
                "rageAfter": 35,
                "rageDelta": -15,
            }
        )
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            context=trial_context,
            state=_state(rage=35, gcd=0),
            marker=trial_marker,
        )
    hs_task_end_sequence = rows[-1]["sequence"] + 1
    hs_end_context = context(hs_task, hs_run, 2, 2, 2)
    hs_end_marker = dict(hs_end_context)
    hs_end_marker.update(
        {
            "taskStartSequence": hs_task_start["sequence"],
            "endSequence": hs_task_end_sequence,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        context=hs_end_context,
        marker=hs_end_marker,
    )

    crit_task = "warrior_bloodthirst_until_crit"
    crit_run = "Warrior-bt-crit-1"
    crit_context = context(crit_task, crit_run, 3, 1, 1)
    crit_task_start = add(
        "CALIBRATION_TASK_STARTED",
        context=crit_context,
        marker=dict(crit_context),
    )
    crit_trial_start = add(
        "CALIBRATION_TRIAL_STARTED",
        context=crit_context,
        marker=dict(crit_context),
    )
    add(
        "SPELL_CAST_EVENT",
        context=crit_context,
        spellID=23894,
        castSucceeded=True,
    )
    add(
        "SPELL_DAMAGE_EVENT_SELF",
        context=crit_context,
        spellID=23894,
        amount=320,
        hitInfo=0,
        targetGUID="0xF13000C552000001",
    )
    add(
        "SPELL_CAST_EVENT",
        context=crit_context,
        state=_state(rage=61),
        spellID=23894,
        castSucceeded=True,
        advance=6.0,
    )
    add(
        "SPELL_MISS_SELF",
        context=crit_context,
        state=_state(rage=61),
        spellID=23894,
        missInfo=1,
        targetGUID="0xF13000C552000001",
    )
    add(
        "UNIT_RAGE",
        context=crit_context,
        state=_state(rage=31),
        unit="player",
    )
    final_cast = add(
        "SPELL_CAST_EVENT",
        context=crit_context,
        state=_state(rage=60),
        spellID=23894,
        castSucceeded=True,
        advance=6.0,
    )
    crit = add(
        "SPELL_DAMAGE_EVENT_SELF",
        context=crit_context,
        state=_state(rage=30),
        spellID=23894,
        amount=644,
        hitInfo=2,
        targetGUID="0xF13000C552000001",
    )
    crit_trial_end_sequence = rows[-1]["sequence"] + 1
    crit_marker = dict(crit_context)
    crit_marker.update(
        {
            "trialStartSequence": crit_trial_start["sequence"],
            "actionStartSequence": final_cast["sequence"],
            "triggerSequence": crit["sequence"],
            "endSequence": crit_trial_end_sequence,
            "completionSource": "automatic_typed_event",
            "actionAttempt": 3,
            "damageAttempts": 2,
        }
    )
    add(
        "CALIBRATION_TRIAL_COMPLETED",
        context=crit_context,
        marker=crit_marker,
    )
    crit_task_end_sequence = rows[-1]["sequence"] + 1
    crit_end_marker = dict(crit_context)
    crit_end_marker.update(
        {
            "taskStartSequence": crit_task_start["sequence"],
            "endSequence": crit_task_end_sequence,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        context=crit_context,
        marker=crit_end_marker,
    )

    execute_task = "warrior_execute_transition"
    execute_run = "Warrior-execute-1"
    execute_start_context = context(execute_task, execute_run, 4, 1, 2)
    execute_task_start = add(
        "CALIBRATION_TASK_STARTED",
        context=execute_start_context,
        marker=dict(execute_start_context),
    )
    execute_specs = [
        (1, 10, "SPELL_DAMAGE_EVENT_SELF", 0, 602, None),
        (2, 20, "SPELL_MISS_SELF", None, None, 1),
    ]
    for trial, threshold, result_event, hit_info, amount, miss_info in execute_specs:
        trial_context = context(execute_task, execute_run, 4, trial, 2)
        trial_context["plannedRageThreshold"] = threshold
        target_state = _state(rage=threshold, gcd=0)
        target_state.update({"targetHealth": 1800, "targetMaximumHealth": 10000})
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            context=trial_context,
            state=dict(target_state),
            marker=dict(trial_context),
        )
        action_state = dict(target_state)
        action_state["gcd"] = 1.5
        action = add(
            "SPELL_CAST_EVENT",
            context=trial_context,
            state=action_state,
            spellID=20662,
            castSucceeded=True,
        )
        add(
            "SPELL_GO_SELF",
            context=trial_context,
            state=dict(action_state),
            spellID=20662,
        )
        result_state = dict(action_state)
        result_state["rage"] = 0
        result = add(
            result_event,
            context=trial_context,
            state=result_state,
            spellID=20662,
            amount=amount,
            hitInfo=hit_info,
            missInfo=miss_info,
            targetGUID="0xF13000C552000001",
        )
        rage_after = 0 if result_event == "SPELL_DAMAGE_EVENT_SELF" else 10
        resource_state = dict(action_state)
        resource_state["rage"] = rage_after
        resource = add(
            "UNIT_RAGE",
            context=trial_context,
            state=resource_state,
            unit="player",
        )
        execute_trial_end = rows[-1]["sequence"] + 1
        execute_marker = dict(trial_context)
        execute_marker.update(
            {
                "trialStartSequence": trial_start["sequence"],
                "actionStartSequence": action["sequence"],
                "resultSequence": result["sequence"],
                "resourceSequence": resource["sequence"],
                "triggerSequence": resource["sequence"],
                "endSequence": execute_trial_end,
                "completionSource": "automatic_typed_event",
                "serverGoSeen": True,
                "resultSeen": True,
                "resourceSeen": True,
                "resourceAfterResultSeen": True,
                "rageBefore": threshold,
                "rageAfter": rage_after,
                "rageDelta": rage_after - threshold,
                "targetHealthBefore": 1800,
                "targetMaximumHealthBefore": 10000,
                "targetHealthPercent": 18,
                "executePhase": True,
                "gcdAtCompletion": 1.45,
            }
        )
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            context=trial_context,
            state=resource_state,
            marker=execute_marker,
        )
    execute_task_end = rows[-1]["sequence"] + 1
    execute_end_context = context(execute_task, execute_run, 4, 2, 2)
    execute_end_marker = dict(execute_end_context)
    execute_end_marker.update(
        {
            "taskStartSequence": execute_task_start["sequence"],
            "endSequence": execute_task_end,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        context=execute_end_context,
        marker=execute_end_marker,
    )

    completion_sequence = rows[-1]["sequence"] + 1
    rows.append(
        {
            "sequence": completion_sequence,
            "time": float(completion_sequence),
            "event": "CALIBRATION_CAMPAIGN_COMPLETED",
            "state": _state(),
            "marker": {
                "campaignId": campaign_id,
                "campaignRunId": campaign_run_id,
                "campaignStep": 4,
                "campaignTaskCount": 4,
                "status": "awaiting_export_reload",
                "completedTasks": 4,
                "deferredTaskId": "warrior_target_armor_floor",
                "nextInstruction": "reload_once_for_automatic_import",
            },
        }
    )
    return rows


def _slam_completed_rows(
    *, snapshot_trial: int | None = None, snapshot_source: str = "result"
) -> list[dict]:
    task_id = "warrior_slam_timing_transition"
    run_id = "Warrior-slam-1"
    modes = [
        "no_flurry_early",
        "no_flurry_late",
        "no_flurry_late",
        "flurry_early",
        "flurry_late",
        "flurry_late",
    ]
    telemetry = {
        "nampowerDetected": True,
        "nampowerVersion": "4.1.0",
        "typedCalibrationSupported": True,
        "registeredEventCount": 48,
    }
    rows: list[dict] = []

    def context(trial: int) -> dict:
        return {
            "taskRunId": run_id,
            "taskId": task_id,
            "trial": trial,
            "requiredTrials": 6,
            "slamTrialMode": modes[trial - 1],
        }

    def add(
        event: str,
        *,
        time: float,
        trial: int,
        state: dict | None = None,
        marker: dict | None = None,
        **fields: object,
    ) -> dict:
        row = {
            "sequence": len(rows) + 1,
            "time": time,
            "event": event,
            "state": state if state is not None else _state(),
            "task": context(trial),
        }
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    task_start_context = context(1)
    task_start_marker = dict(task_start_context)
    task_start_marker["telemetry"] = telemetry
    task_start = add(
        "CALIBRATION_TASK_STARTED",
        time=1.0,
        trial=1,
        marker=task_start_marker,
    )

    for trial, mode in enumerate(modes, start=1):
        base = 10.0 * trial
        flurry = mode.startswith("flurry_")
        early = mode.endswith("_early")
        advertised_ms = 1923 if flurry else 2500
        remaining = 3.4 if early else 0.5
        speed = 3.5
        trial_context = context(trial)
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base,
            trial=trial,
            state=_state(rage=60),
            marker=dict(trial_context),
        )
        previous_main_hand = add(
            "AUTO_ATTACK_SELF",
            time=base + 0.1,
            trial=trial,
            state=_state(rage=60),
            amount=410,
            hitInfo=0,
        )
        action_time = base + 0.2
        action_state = _state(rage=60)
        action_state.update(
            {
                "cat2MainHandRemaining": remaining,
                "mainHandSpeed": speed,
                "moving": False,
            }
        )
        action = add(
            "SPELL_CAST_EVENT",
            time=action_time,
            trial=trial,
            state=action_state,
            spellID=45961,
            castSucceeded=True,
            castType=0,
        )
        server_start = add(
            "SPELL_START_SELF",
            time=action_time + 0.02,
            trial=trial,
            state=dict(action_state),
            spellID=45961,
            castTimeMilliseconds=advertised_ms,
            castDurationMilliseconds=advertised_ms,
        )
        server_go_time = float(server_start["time"]) + advertised_ms / 1000
        server_go = add(
            "SPELL_GO_SELF",
            time=server_go_time,
            trial=trial,
            state=dict(action_state),
            spellID=45961,
        )
        result_spell_id = 45964 if trial % 2 == 0 else 45961
        if trial == snapshot_trial and snapshot_source == "result_after_energize":
            add(
                "SPELL_ENERGIZE_ON_SELF",
                time=server_go_time + 0.003,
                trial=trial,
                state=dict(action_state),
                spellID=2687,
                amount=10,
            )
        result_state = dict(action_state)
        if trial == snapshot_trial and snapshot_source in {
            "result",
            "result_after_energize",
        }:
            result_state["rage"] = 45
        result = add(
            "SPELL_DAMAGE_EVENT_SELF",
            time=server_go_time + 0.005,
            trial=trial,
            state=result_state,
            spellID=result_spell_id,
            amount=800 + trial,
            hitInfo=0,
        )
        resource_state = dict(action_state)
        resource_state["rage"] = 45
        resource = None
        if trial != snapshot_trial:
            resource = add(
                "UNIT_RAGE",
                time=server_go_time + 0.006,
                trial=trial,
                state=resource_state,
                unit="player",
            )

        if trial in {1, 4}:
            next_main_hand_time = action_time + remaining
        elif trial in {2, 5}:
            next_main_hand_time = action_time + remaining + advertised_ms / 1000
        else:
            next_main_hand_time = server_go_time + speed
        next_main_hand = add(
            "AUTO_ATTACK_SELF",
            time=next_main_hand_time,
            trial=trial,
            state=resource_state,
            amount=420 + trial,
            hitInfo=0,
        )
        completion_sequence = len(rows) + 1
        trial_marker = dict(trial_context)
        trial_marker.update(
            {
                "trialStartSequence": trial_start["sequence"],
                "actionStartSequence": action["sequence"],
                "slamStartSequence": server_start["sequence"],
                "serverGoSequence": server_go["sequence"],
                "resultSequence": result["sequence"],
                "nextMainHandSequence": next_main_hand["sequence"],
                "previousMainHandSequence": previous_main_hand["sequence"],
                "triggerSequence": next_main_hand["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
                "actionSeen": True,
                "slamStartSeen": True,
                "serverGoSeen": True,
                "resultSeen": True,
                "nextMainHandSeen": True,
                "slamCastTimeMilliseconds": advertised_ms,
                "slamCastDurationMilliseconds": advertised_ms,
                "slamDelayedMilliseconds": 0,
                "serverGoTime": server_go_time,
                "resultSpellID": result_spell_id,
                "preCastMainHandRemaining": remaining,
                "preCastMainHandSpeed": speed,
                "preCastGCD": 0,
                "preCastMoving": False,
                "preCastFlurryActive": flurry,
                "previousMainHandTime": previous_main_hand["time"],
                "nextMainHandTime": next_main_hand_time,
                "rageBefore": 60,
                "rageAfter": 45,
                "rageDelta": -15,
            }
        )
        if resource is not None:
            trial_marker.update(
                {
                    "resourceSequence": resource["sequence"],
                    "resourceObservationSource": "resource_event",
                    "resourceSeen": True,
                    "resourceAfterServerGoSeen": True,
                }
            )
        else:
            if snapshot_source in {"result", "result_after_energize"}:
                snapshot_sequence = result["sequence"]
            elif snapshot_source == "next_main_hand":
                snapshot_sequence = next_main_hand["sequence"]
            else:
                raise ValueError(f"unsupported snapshot_source: {snapshot_source}")
            trial_marker.update(
                {
                    "postGoRageSnapshotSequence": snapshot_sequence,
                    "resourceObservationSource": "state_snapshot",
                    "resourceSeen": False,
                }
            )
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=next_main_hand_time + 0.01,
            trial=trial,
            state=resource_state,
            marker=trial_marker,
        )

    task_completion_sequence = len(rows) + 1
    task_end_marker = dict(context(6))
    task_end_marker.update(
        {
            "taskStartSequence": task_start["sequence"],
            "endSequence": task_completion_sequence,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=float(rows[-1]["time"]) + 0.01,
        trial=6,
        marker=task_end_marker,
    )
    return rows


def _phase3_completed_rows(
    *,
    white_proc_trial: int | None = None,
    white_proc_before_swing: bool = False,
    cleave_proc: bool = False,
) -> list[dict]:
    campaign_id = "warrior_fury_dummy_ravager_rage_phase3"
    campaign_run_id = "Warrior-phase3-campaign-1"
    telemetry = {
        "nampowerDetected": True,
        "nampowerVersion": "4.1.0",
        "typedCalibrationSupported": True,
        "registeredEventCount": 48,
    }
    rows: list[dict] = []

    def context(
        task_id: str,
        run_id: str,
        trial: int,
        required_trials: int,
        completion_kind: str,
    ) -> dict:
        return {
            "taskRunId": run_id,
            "taskId": task_id,
            "trial": trial,
            "requiredTrials": required_trials,
            "completionKind": completion_kind,
            "campaignId": campaign_id,
            "campaignRunId": campaign_run_id,
        }

    def add(
        event: str,
        *,
        time: float,
        task_context: dict | None = None,
        state: dict | None = None,
        marker: dict | None = None,
        **fields: object,
    ) -> dict:
        row = {
            "sequence": len(rows) + 1,
            "time": time,
            "event": event,
            "state": state if state is not None else _state(),
        }
        if task_context is not None:
            row["task"] = task_context
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    add(
        "CALIBRATION_CAMPAIGN_STARTED",
        time=1.0,
        marker={
            "campaignId": campaign_id,
            "campaignRunId": campaign_run_id,
            "campaignStep": 1,
            "campaignTaskCount": 3,
            "status": "running",
        },
    )

    whirlwind_task = "warrior_whirlwind_cooldown_transition"
    whirlwind_run = "Warrior-phase3-whirlwind-1"
    whirlwind_kind = "whirlwind_cooldown_transition"
    whirlwind_context = context(
        whirlwind_task, whirlwind_run, 1, 1, whirlwind_kind
    )
    whirlwind_start_marker = dict(whirlwind_context)
    whirlwind_start_marker["telemetry"] = telemetry
    whirlwind_start = add(
        "CALIBRATION_TASK_STARTED",
        time=2.0,
        task_context=whirlwind_context,
        marker=whirlwind_start_marker,
    )
    whirlwind_trial_start = add(
        "CALIBRATION_TRIAL_STARTED",
        time=9.0,
        task_context=whirlwind_context,
        state=_state(rage=100),
        marker=dict(whirlwind_context),
    )
    first_action = add(
        "SPELL_CAST_EVENT",
        time=10.0,
        task_context=whirlwind_context,
        state=_state(rage=100),
        spellID=1680,
        castSucceeded=True,
        castType=0,
    )
    first_go = add(
        "SPELL_GO_SELF",
        time=10.04,
        task_context=whirlwind_context,
        state=_state(rage=75, whirlwind_cooldown=8.5),
        spellID=1680,
    )
    first_result = add(
        "SPELL_DAMAGE_EVENT_SELF",
        time=10.05,
        task_context=whirlwind_context,
        state=_state(rage=75, whirlwind_cooldown=8.49),
        spellID=1680,
        amount=500,
        hitInfo=0,
    )
    cooldown_observation = add(
        "SPELL_UPDATE_COOLDOWN",
        time=10.06,
        task_context=whirlwind_context,
        state=_state(rage=75, whirlwind_cooldown=8.48),
    )
    second_action = add(
        "SPELL_CAST_EVENT",
        time=18.55,
        task_context=whirlwind_context,
        state=_state(rage=75),
        spellID=1680,
        castSucceeded=True,
        castType=0,
    )
    second_go = add(
        "SPELL_GO_SELF",
        time=18.56,
        task_context=whirlwind_context,
        state=_state(rage=50, whirlwind_cooldown=8.5),
        spellID=1680,
    )
    second_result = add(
        "SPELL_DAMAGE_EVENT_SELF",
        time=18.57,
        task_context=whirlwind_context,
        state=_state(rage=50, whirlwind_cooldown=8.49),
        spellID=1680,
        amount=510,
        hitInfo=0,
    )
    whirlwind_trial_end = len(rows) + 1
    whirlwind_trial_marker = dict(whirlwind_context)
    whirlwind_trial_marker.update(
        {
            "trialStartSequence": whirlwind_trial_start["sequence"],
            "firstActionSequence": first_action["sequence"],
            "firstServerGoSequence": first_go["sequence"],
            "firstServerGoTime": first_go["time"],
            "firstResultSequence": first_result["sequence"],
            "firstResultEvent": first_result["event"],
            "cooldownObservationSequence": cooldown_observation["sequence"],
            "cooldownObservationEvent": cooldown_observation["event"],
            "observedCooldownSeconds": 8.48,
            "cooldownStartTime": first_go["time"],
            "cooldownDurationSeconds": 8.5,
            "cooldownRemainingAtFirstGoSeconds": 8.5,
            "cooldownSpellbookIndex": 22,
            "secondActionSequence": second_action["sequence"],
            "secondServerGoSequence": second_go["sequence"],
            "secondServerGoTime": second_go["time"],
            "secondResultSequence": second_result["sequence"],
            "secondResultEvent": second_result["event"],
            "serverGoIntervalSeconds": 8.52,
            "serverGoIntervalInterpretation": "upper_bound_due_to_hardware_press",
            "ravagerRank": 2,
            "ravagerRankSource": "OBSERVED_TALENT_API",
            "endSequence": whirlwind_trial_end,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TRIAL_COMPLETED",
        time=18.58,
        task_context=whirlwind_context,
        marker=whirlwind_trial_marker,
    )
    whirlwind_task_end = len(rows) + 1
    whirlwind_end_marker = dict(whirlwind_context)
    whirlwind_end_marker.update(
        {
            "taskStartSequence": whirlwind_start["sequence"],
            "endSequence": whirlwind_task_end,
            "completionSource": "automatic_typed_event",
            "campaignStep": 1,
            "campaignTaskCount": 3,
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=18.59,
        task_context=whirlwind_context,
        marker=whirlwind_end_marker,
    )

    cleave_task = "warrior_cleave_queue_swing"
    cleave_run = "Warrior-phase3-cleave-1"
    cleave_kind = "cleave_queue_swing"
    cleave_context = context(cleave_task, cleave_run, 1, 1, cleave_kind)
    cleave_start_marker = dict(cleave_context)
    cleave_start_marker["telemetry"] = telemetry
    cleave_start = add(
        "CALIBRATION_TASK_STARTED",
        time=20.0,
        task_context=cleave_context,
        marker=cleave_start_marker,
    )
    cleave_trial_start = add(
        "CALIBRATION_TRIAL_STARTED",
        time=21.0,
        task_context=cleave_context,
        state=_state(rage=70),
        marker=dict(cleave_context),
    )
    cleave_action = add(
        "SPELL_CAST_EVENT",
        time=21.1,
        task_context=cleave_context,
        state=_state(rage=70),
        spellID=20569,
        castSucceeded=True,
        castType=2,
    )
    cleave_queued = add(
        "SPELL_QUEUE_EVENT",
        time=21.11,
        task_context=cleave_context,
        state=_state(rage=70),
        spellID=20569,
        queueEventCode=0,
    )
    cleave_popped = add(
        "SPELL_QUEUE_EVENT",
        time=22.0,
        task_context=cleave_context,
        state=_state(rage=70),
        spellID=20569,
        queueEventCode=1,
    )
    cleave_go = add(
        "SPELL_GO_SELF",
        time=22.001,
        task_context=cleave_context,
        state=_state(rage=70),
        spellID=20569,
    )
    cleave_result = add(
        "SPELL_DAMAGE_EVENT_SELF",
        time=22.002,
        task_context=cleave_context,
        state=_state(rage=70),
        spellID=20571,
        amount=620,
        hitInfo=0,
    )
    if cleave_proc:
        for event, offset in (
            ("SPELL_ENERGIZE_BY_SELF", 22.0022),
            ("SPELL_ENERGIZE_ON_SELF", 22.0023),
        ):
            add(
                event,
                time=offset,
                task_context=cleave_context,
                state=_state(rage=70),
                spellID=12964,
                amount=20,
                powerType=1,
                sourceGUID="0x0000000000000001",
                targetGUID="0x0000000000000001",
            )
    cleave_rage_after = 54 if cleave_proc else 52
    cleave_resource = add(
        "UNIT_RAGE",
        time=22.003,
        task_context=cleave_context,
        state=_state(rage=cleave_rage_after),
        unit="player",
    )
    cleave_next_main_hand = add(
        "AUTO_ATTACK_SELF",
        time=25.5,
        task_context=cleave_context,
        state=_state(rage=cleave_rage_after),
        amount=430,
        hitInfo=0,
    )
    cleave_trial_end = len(rows) + 1
    cleave_trial_marker = dict(cleave_context)
    cleave_trial_marker.update(
        {
            "trialStartSequence": cleave_trial_start["sequence"],
            "actionStartSequence": cleave_action["sequence"],
            "queueSeen": True,
            "queuePoppedSeen": True,
            "queueEvidence": "nampower_on_swing_buffer",
            "serverGoSequence": cleave_go["sequence"],
            "resultSequence": cleave_result["sequence"],
            "resultEvent": cleave_result["event"],
            "resourceSequence": cleave_resource["sequence"],
            "nextMainHandSequence": cleave_next_main_hand["sequence"],
            "nextMainHandTime": cleave_next_main_hand["time"],
            "rageBefore": 70,
            "rageAfter": cleave_rage_after,
            "rageDelta": cleave_rage_after - 70,
            "expectedRageCost": 18,
            "ravagerRank": 2,
            "ravagerRankSource": "OBSERVED_TALENT_API",
            "endSequence": cleave_trial_end,
            "completionSource": "automatic_typed_event",
        }
    )
    add(
        "CALIBRATION_TRIAL_COMPLETED",
        time=25.51,
        task_context=cleave_context,
        state=_state(rage=cleave_rage_after),
        marker=cleave_trial_marker,
    )
    cleave_task_end = len(rows) + 1
    cleave_end_marker = dict(cleave_context)
    cleave_end_marker.update(
        {
            "taskStartSequence": cleave_start["sequence"],
            "endSequence": cleave_task_end,
            "completionSource": "automatic_typed_event",
            "campaignStep": 2,
            "campaignTaskCount": 3,
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=25.52,
        task_context=cleave_context,
        marker=cleave_end_marker,
    )

    white_task = "warrior_white_swing_rage_transition"
    white_run = "Warrior-phase3-white-1"
    white_kind = "white_swing_rage_transition"
    white_start_context = context(white_task, white_run, 1, 3, white_kind)
    white_start_marker = dict(white_start_context)
    white_start_marker["telemetry"] = telemetry
    white_start = add(
        "CALIBRATION_TASK_STARTED",
        time=30.0,
        task_context=white_start_context,
        marker=white_start_marker,
    )
    white_samples = [
        (10, 16, 350, 2),
        (20, 29, 500, 130),
        (30, 37, 400, 16386),
    ]
    for trial, (rage_before, rage_after, damage, hit_info) in enumerate(
        white_samples, start=1
    ):
        white_context = context(white_task, white_run, trial, 3, white_kind)
        base_time = 30.0 + trial * 5
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base_time,
            task_context=white_context,
            state=_state(rage=rage_before),
            marker=dict(white_context),
        )
        if trial == white_proc_trial and white_proc_before_swing:
            for event, offset in (
                ("SPELL_ENERGIZE_BY_SELF", 0.9998),
                ("SPELL_ENERGIZE_ON_SELF", 0.9999),
            ):
                add(
                    event,
                    time=base_time + offset,
                    task_context=white_context,
                    state=_state(rage=rage_before),
                    spellID=12964,
                    amount=20,
                    powerType=1,
                    sourceGUID="0x0000000000000001",
                    targetGUID="0x0000000000000001",
                )
        swing = add(
            "AUTO_ATTACK_SELF",
            time=base_time + 1.0,
            task_context=white_context,
            state=_state(rage=rage_before),
            amount=damage,
            hitInfo=hit_info,
            targetGUID="0xF13000C552000001",
        )
        if trial == white_proc_trial and not white_proc_before_swing:
            for event, offset in (
                ("SPELL_ENERGIZE_BY_SELF", 1.0002),
                ("SPELL_ENERGIZE_ON_SELF", 1.0003),
            ):
                add(
                    event,
                    time=base_time + offset,
                    task_context=white_context,
                    state=_state(rage=rage_before),
                    spellID=12964,
                    amount=20,
                    powerType=1,
                    sourceGUID="0x0000000000000001",
                    targetGUID="0x0000000000000001",
                )
        resource = add(
            "UNIT_RAGE",
            time=base_time + 1.001,
            task_context=white_context,
            state=_state(rage=rage_after),
            unit="player",
        )
        trial_end = len(rows) + 1
        trial_marker = dict(white_context)
        trial_marker.update(
            {
                "trialStartSequence": trial_start["sequence"],
                "swingSequence": swing["sequence"],
                "swingTime": swing["time"],
                "hand": "main_hand",
                "hitInfo": hit_info,
                "damageAmount": damage,
                "rageBefore": rage_before,
                "rageAfter": rage_after,
                "rageDelta": rage_after - rage_before,
                "resourceSequence": resource["sequence"],
                "maximumRage": 100,
                "cappedObservation": False,
                "endSequence": trial_end,
                "completionSource": "automatic_typed_event",
            }
        )
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=base_time + 1.002,
            task_context=white_context,
            state=_state(rage=rage_after),
            marker=trial_marker,
        )
    white_end_context = context(white_task, white_run, 3, 3, white_kind)
    white_task_end = len(rows) + 1
    white_end_marker = dict(white_end_context)
    white_end_marker.update(
        {
            "taskStartSequence": white_start["sequence"],
            "endSequence": white_task_end,
            "completionSource": "automatic_typed_event",
            "campaignStep": 3,
            "campaignTaskCount": 3,
        }
    )
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=46.01,
        task_context=white_end_context,
        marker=white_end_marker,
    )
    add(
        "CALIBRATION_CAMPAIGN_COMPLETED",
        time=46.02,
        marker={
            "campaignId": campaign_id,
            "campaignRunId": campaign_run_id,
            "campaignStep": 3,
            "campaignTaskCount": 3,
            "status": "awaiting_export_reload",
            "completedTasks": 3,
            "nextInstruction": "reload_once_for_automatic_import",
        },
    )
    return rows


PHASE4_TASK_ID = "warrior_bloodthirst_ap_strata_damage"
PHASE4_RUN_ID = "Warrior-bloodthirst-ap-phase4-1"
PHASE4_TARGET_GUID = "0xF13000C55200A004"


def _phase4_state(
    attack_power: int,
    *,
    target_guid: str = PHASE4_TARGET_GUID,
    target_armor: int = 4211,
) -> dict:
    state = _state()
    state["attackPower"] = {
        "base": attack_power,
        "positive": 0,
        "negative": 0,
        "effective": attack_power,
    }
    state["targetGUID"] = target_guid
    state["targetArmor"] = {
        "base": target_armor,
        "effective": target_armor,
        "armor": target_armor,
        "positive": 0,
        "negative": 0,
    }
    return state


def _phase4_completed_rows(
    *,
    include_rejected_crit_and_miss: bool = False,
    include_incomplete_attempt: bool = False,
    include_setup_rejection: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    baseline_attack_power = 1000
    buffed_attack_power = 1400
    observed_delta = buffed_attack_power - baseline_attack_power

    def context(trial: int) -> dict:
        return {
            "taskRunId": PHASE4_RUN_ID,
            "taskId": PHASE4_TASK_ID,
            "trial": trial,
            "requiredTrials": 8,
        }

    def add(
        event: str,
        *,
        trial: int,
        attack_power: int,
        marker: dict | None = None,
        **extra: object,
    ) -> dict:
        sequence = len(rows) + 1
        row = {
            "sequence": sequence,
            "time": float(sequence),
            "event": event,
            "state": _phase4_state(attack_power),
            "task": context(trial),
        }
        if marker is not None:
            row["marker"] = marker
        row.update(extra)
        rows.append(row)
        return row

    task_start = add(
        "CALIBRATION_TASK_STARTED",
        trial=1,
        attack_power=baseline_attack_power,
        marker={
            **context(1),
            "phase": "started",
            "telemetry": {
                "nampowerVersion": "4.1.0",
                "typedCalibrationSupported": True,
                "registeredEventCount": 47,
            },
        },
    )

    for trial in range(1, 9):
        stratum = (
            "no_battle_shout"
            if trial <= 4
            else "battle_shout_observed_delta"
        )
        attack_power = (
            baseline_attack_power if trial <= 4 else buffed_attack_power
        )
        damage = 330 if trial <= 4 else 414
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            trial=trial,
            attack_power=attack_power,
            marker={
                **context(trial),
                "phase": "trial_started",
                "stratum": stratum,
            },
        )

        if trial == 5 and include_setup_rejection:
            setup_rejection = add(
                "CALIBRATION_SAMPLE_REJECTED",
                trial=trial,
                attack_power=attack_power,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_ap_setup_rejected",
                    "reason": "target_armor_changed_before_cast",
                    "targetArmor": 3971,
                    "targetGUID": PHASE4_TARGET_GUID,
                    "referenceTargetArmor": 4211,
                    "referenceTargetGUID": PHASE4_TARGET_GUID,
                    "stratum": stratum,
                },
            )
            setup_rejection["state"] = _phase4_state(
                attack_power,
                target_armor=3971,
            )

        if trial == 1 and include_incomplete_attempt:
            incomplete_action = add(
                "SPELL_CAST_EVENT",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                castSucceeded=True,
                targetGUID=PHASE4_TARGET_GUID,
            )
            add(
                "CALIBRATION_ATTEMPT_INCOMPLETE",
                trial=trial,
                attack_power=attack_power,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_ap_attempt_incomplete",
                    "reason": "typed_evidence_timeout",
                    "actionAttempt": 1,
                    "actionStartSequence": incomplete_action["sequence"],
                    "serverGoSeen": False,
                    "resultSeen": False,
                    "requestedAttackPower": attack_power,
                    "requestedTargetArmor": 4211,
                    "requestedTargetGUID": PHASE4_TARGET_GUID,
                    "stratum": stratum,
                },
            )

        if trial == 1 and include_rejected_crit_and_miss:
            add(
                "SPELL_CAST_EVENT",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                castSucceeded=True,
                targetGUID=PHASE4_TARGET_GUID,
            )
            add(
                "SPELL_GO_SELF",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                targetGUID=PHASE4_TARGET_GUID,
            )
            critical = add(
                "SPELL_DAMAGE_EVENT_SELF",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                targetGUID=PHASE4_TARGET_GUID,
                amount=660,
                hitInfo=2,
            )
            add(
                "CALIBRATION_SAMPLE_REJECTED",
                trial=trial,
                attack_power=attack_power,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_ap_sample_rejected",
                    "reason": "critical_hit_not_counted",
                    "triggerSequence": critical["sequence"],
                    "attackPower": attack_power,
                    "targetArmor": 4211,
                    "targetGUID": PHASE4_TARGET_GUID,
                    "damage": 660,
                    "hitInfo": 2,
                    "stratum": stratum,
                },
            )
            add(
                "SPELL_CAST_EVENT",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                castSucceeded=True,
                targetGUID=PHASE4_TARGET_GUID,
            )
            add(
                "SPELL_GO_SELF",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                targetGUID=PHASE4_TARGET_GUID,
            )
            miss = add(
                "SPELL_MISS_SELF",
                trial=trial,
                attack_power=attack_power,
                spellID=23894,
                targetGUID=PHASE4_TARGET_GUID,
                missInfo=1,
            )
            add(
                "CALIBRATION_SAMPLE_REJECTED",
                trial=trial,
                attack_power=attack_power,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_ap_sample_rejected",
                    "reason": "miss_not_counted",
                    "triggerSequence": miss["sequence"],
                    "attackPower": attack_power,
                    "targetArmor": 4211,
                    "targetGUID": PHASE4_TARGET_GUID,
                    "stratum": stratum,
                    "missInfo": 1,
                },
            )

        action = add(
            "SPELL_CAST_EVENT",
            trial=trial,
            attack_power=attack_power,
            spellID=23894,
            castSucceeded=True,
            targetGUID=PHASE4_TARGET_GUID,
        )
        server_go = add(
            "SPELL_GO_SELF",
            trial=trial,
            attack_power=attack_power,
            spellID=23894,
            targetGUID=PHASE4_TARGET_GUID,
        )
        result = add(
            "SPELL_DAMAGE_EVENT_SELF",
            trial=trial,
            attack_power=attack_power,
            spellID=23894,
            targetGUID=PHASE4_TARGET_GUID,
            amount=damage,
            hitInfo=0,
        )
        accepted_marker = {
            **context(trial),
            "phase": "bloodthirst_ap_sample_accepted",
            "attackPower": attack_power,
            "targetArmor": 4211,
            "targetGUID": PHASE4_TARGET_GUID,
            "damage": damage,
            "hitInfo": 0,
            "stratum": stratum,
        }
        if trial > 4:
            accepted_marker["observedAttackPowerDelta"] = observed_delta
        accepted = add(
            "CALIBRATION_SAMPLE_ACCEPTED",
            trial=trial,
            attack_power=attack_power,
            marker=accepted_marker,
        )
        completion_sequence = len(rows) + 1
        completion_marker = {
            **context(trial),
            "phase": "trial_completed",
            "trialStartSequence": trial_start["sequence"],
            "actionStartSequence": action["sequence"],
            "serverGoSequence": server_go["sequence"],
            "resultSequence": result["sequence"],
            "resultEvent": "SPELL_DAMAGE_EVENT_SELF",
            "triggerSequence": result["sequence"],
            "endSequence": completion_sequence,
            "completionSource": "automatic_typed_event",
            "actionSeen": True,
            "serverGoSeen": True,
            "resultSeen": True,
            "attackPower": attack_power,
            "targetArmor": 4211,
            "targetGUID": PHASE4_TARGET_GUID,
            "damage": damage,
            "hitInfo": 0,
            "stratum": stratum,
            "baselineAttackPower": baseline_attack_power,
        }
        if trial > 4:
            completion_marker.update(
                {
                    "buffedAttackPower": buffed_attack_power,
                    "observedAttackPowerDelta": observed_delta,
                }
            )
        completion = add(
            "CALIBRATION_TRIAL_COMPLETED",
            trial=trial,
            attack_power=attack_power,
            marker=completion_marker,
        )
        assert accepted["sequence"] < completion["sequence"]

    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        trial=8,
        attack_power=buffed_attack_power,
        marker={
            **context(8),
            "phase": "completed",
            "taskStartSequence": task_start["sequence"],
            "endSequence": task_completion_sequence,
            "completionSource": "automatic_typed_event",
            "stratum": "battle_shout_observed_delta",
        },
    )
    return rows


def _phase4_retarget_trial(rows: list[dict], trial: int, target_guid: str) -> None:
    for row in rows:
        task = row.get("task")
        if not isinstance(task, dict) or task.get("trial") != trial:
            continue
        state = row.get("state")
        if isinstance(state, dict):
            state["targetGUID"] = target_guid
        if "targetGUID" in row:
            row["targetGUID"] = target_guid
        marker = row.get("marker")
        if isinstance(marker, dict) and "targetGUID" in marker:
            marker["targetGUID"] = target_guid


def _phase4_rearmor_trial(rows: list[dict], trial: int, target_armor: int) -> None:
    for row in rows:
        task = row.get("task")
        if not isinstance(task, dict) or task.get("trial") != trial:
            continue
        state = row.get("state")
        if isinstance(state, dict) and isinstance(state.get("targetArmor"), dict):
            state["targetArmor"].update(
                {
                    "base": target_armor,
                    "effective": target_armor,
                    "armor": target_armor,
                }
            )
        marker = row.get("marker")
        if isinstance(marker, dict) and "targetArmor" in marker:
            marker["targetArmor"] = target_armor


PHASE5_TASK_ID = "warrior_bloodthirst_armor_strata_damage"
PHASE5_RUN_ID = "Warrior-bloodthirst-armor-phase5-1"
PHASE5_TARGET_GUID = "0xF13000C55226FDD2"
PHASE5_STACKS = (0, 1, 3, 5)
PHASE5_ARMORS = {0: 4211, 1: 3761, 3: 2861, 5: 1961}
PHASE5_DAMAGES = {0: 350, 1: 367, 3: 406, 5: 456}


def _phase5_state(
    attack_power: int = 1180,
    *,
    target_guid: str = PHASE5_TARGET_GUID,
    target_armor: int = 4211,
) -> dict:
    state = _state()
    state["attackPower"] = {
        "base": attack_power,
        "positive": 0,
        "negative": 0,
        "effective": attack_power,
    }
    state["targetGUID"] = target_guid
    state["targetArmor"] = {
        "base": target_armor,
        "effective": target_armor,
        "armor": target_armor,
        "positive": 0,
        "negative": 0,
    }
    return state


def _phase5_completed_rows(
    *,
    include_nonvalid_attempts: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    attack_power = 1180
    baseline_armor = PHASE5_ARMORS[0]

    def stacks_for_trial(trial: int) -> int:
        return PHASE5_STACKS[(trial - 1) // 4]

    def context(trial: int) -> dict:
        stacks = stacks_for_trial(trial)
        return {
            "taskRunId": PHASE5_RUN_ID,
            "taskId": PHASE5_TASK_ID,
            "trial": trial,
            "requiredTrials": 16,
            "stratum": f"sunder_{stacks}",
            "plannedSunderStacks": stacks,
        }

    def add(
        event: str,
        *,
        trial: int,
        target_armor: int,
        marker: dict | None = None,
        current_attack_power: int = attack_power,
        target_guid: str = PHASE5_TARGET_GUID,
        **extra: object,
    ) -> dict:
        sequence = len(rows) + 1
        row = {
            "sequence": sequence,
            "time": float(sequence),
            "event": event,
            "state": _phase5_state(
                current_attack_power,
                target_guid=target_guid,
                target_armor=target_armor,
            ),
            "task": context(trial),
        }
        if marker is not None:
            row["marker"] = marker
        row.update(extra)
        rows.append(row)
        return row

    task_start = add(
        "CALIBRATION_TASK_STARTED",
        trial=1,
        target_armor=baseline_armor,
        marker={
            **context(1),
            "phase": "started",
            "telemetry": {
                "nampowerVersion": "4.1.0",
                "typedCalibrationSupported": True,
                "registeredEventCount": 47,
            },
        },
    )

    for trial in range(1, 17):
        stacks = stacks_for_trial(trial)
        stratum = f"sunder_{stacks}"
        armor = PHASE5_ARMORS[stacks]
        reduction = baseline_armor - armor
        damage = PHASE5_DAMAGES[stacks]
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            trial=trial,
            target_armor=armor,
            marker={
                **context(trial),
                "phase": "trial_started",
            },
        )
        if trial in {1, 5, 9, 13}:
            add(
                "CALIBRATION_ARMOR_STRATUM_LOCKED",
                trial=trial,
                target_armor=armor,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_armor_stratum_locked",
                    "attackPower": attack_power,
                    "targetArmor": armor,
                    "targetGUID": PHASE5_TARGET_GUID,
                    "observedSunderStacks": stacks,
                    "baselineTargetArmor": baseline_armor,
                    "armorReductionFromBaseline": reduction,
                },
            )

        if trial == 1 and include_nonvalid_attempts:
            incomplete_action = add(
                "SPELL_CAST_EVENT",
                trial=trial,
                target_armor=armor,
                spellID=23894,
                castSucceeded=True,
                targetGUID=PHASE5_TARGET_GUID,
            )
            add(
                "CALIBRATION_ATTEMPT_INCOMPLETE",
                trial=trial,
                target_armor=armor,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_armor_attempt_incomplete",
                    "reason": "typed_evidence_timeout",
                    "actionStartSequence": incomplete_action["sequence"],
                    "serverGoSeen": False,
                    "resultSeen": False,
                },
            )
            add(
                "SPELL_CAST_EVENT",
                trial=trial,
                target_armor=armor,
                spellID=23894,
                castSucceeded=True,
                targetGUID=PHASE5_TARGET_GUID,
            )
            add(
                "SPELL_GO_SELF",
                trial=trial,
                target_armor=armor,
                spellID=23894,
                targetGUID=PHASE5_TARGET_GUID,
            )
            critical = add(
                "SPELL_DAMAGE_EVENT_SELF",
                trial=trial,
                target_armor=armor,
                spellID=23894,
                targetGUID=PHASE5_TARGET_GUID,
                amount=700,
                hitInfo=2,
            )
            add(
                "CALIBRATION_SAMPLE_REJECTED",
                trial=trial,
                target_armor=armor,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_armor_sample_rejected",
                    "reason": "critical_hit_not_counted",
                    "triggerSequence": critical["sequence"],
                    "attackPower": attack_power,
                    "targetArmor": armor,
                    "targetGUID": PHASE5_TARGET_GUID,
                    "observedSunderStacks": stacks,
                    "damage": 700,
                    "hitInfo": 2,
                },
            )

        if trial == 5 and include_nonvalid_attempts:
            add(
                "CALIBRATION_ATTEMPT_INCOMPLETE",
                trial=trial,
                target_armor=PHASE5_ARMORS[0],
                marker={
                    **context(trial),
                    "phase": "bloodthirst_armor_sunder_attempt_incomplete",
                    "reason": "sunder_stack_did_not_advance",
                    "requestedFromStacks": 0,
                    "targetGUID": PHASE5_TARGET_GUID,
                    "targetArmor": PHASE5_ARMORS[0],
                },
            )
            add(
                "CALIBRATION_SAMPLE_REJECTED",
                trial=trial,
                target_armor=3971,
                marker={
                    **context(trial),
                    "phase": "bloodthirst_armor_setup_rejected",
                    "reason": "target_armor_changed_before_cast",
                    "targetArmor": 3971,
                    "targetGUID": PHASE5_TARGET_GUID,
                    "observedSunderStacks": stacks,
                    "attackPower": attack_power,
                    "expectedAttackPower": attack_power,
                    "expectedTargetArmor": armor,
                    "baselineTargetArmor": baseline_armor,
                },
            )

        action = add(
            "SPELL_CAST_EVENT",
            trial=trial,
            target_armor=armor,
            spellID=23894,
            castSucceeded=True,
            targetGUID=PHASE5_TARGET_GUID,
        )
        server_go = add(
            "SPELL_GO_SELF",
            trial=trial,
            target_armor=armor,
            spellID=23894,
            targetGUID=PHASE5_TARGET_GUID,
        )
        result = add(
            "SPELL_DAMAGE_EVENT_SELF",
            trial=trial,
            target_armor=armor,
            spellID=23894,
            targetGUID=PHASE5_TARGET_GUID,
            amount=damage,
            hitInfo=0,
        )
        payload = {
            **context(trial),
            "attackPower": attack_power,
            "referenceAttackPower": attack_power,
            "targetArmor": armor,
            "targetGUID": PHASE5_TARGET_GUID,
            "damage": damage,
            "hitInfo": 0,
            "observedSunderStacks": stacks,
            "baselineTargetArmor": baseline_armor,
            "stratumTargetArmor": armor,
            "armorReductionFromBaseline": reduction,
        }
        accepted = add(
            "CALIBRATION_SAMPLE_ACCEPTED",
            trial=trial,
            target_armor=armor,
            marker={**payload, "phase": "bloodthirst_armor_sample_accepted"},
        )
        completion_sequence = len(rows) + 1
        completion = add(
            "CALIBRATION_TRIAL_COMPLETED",
            trial=trial,
            target_armor=armor,
            marker={
                **payload,
                "phase": "trial_completed",
                "trialStartSequence": trial_start["sequence"],
                "actionStartSequence": action["sequence"],
                "serverGoSequence": server_go["sequence"],
                "resultSequence": result["sequence"],
                "resultEvent": "SPELL_DAMAGE_EVENT_SELF",
                "triggerSequence": result["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
                "actionSeen": True,
                "serverGoSeen": True,
                "resultSeen": True,
            },
        )
        assert accepted["sequence"] < completion["sequence"]

    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        trial=16,
        target_armor=PHASE5_ARMORS[5],
        marker={
            **context(16),
            "phase": "completed",
            "taskStartSequence": task_start["sequence"],
            "endSequence": task_completion_sequence,
            "completionSource": "automatic_typed_event",
        },
    )
    return rows


def _phase5_retarget_trial(rows: list[dict], trial: int, target_guid: str) -> None:
    for row in rows:
        if row.get("task", {}).get("trial") != trial:
            continue
        row["state"]["targetGUID"] = target_guid
        if "targetGUID" in row:
            row["targetGUID"] = target_guid
        marker = row.get("marker")
        if isinstance(marker, dict) and "targetGUID" in marker:
            marker["targetGUID"] = target_guid


def _phase5_rearmor_trials(
    rows: list[dict], trials: set[int], target_armor: int
) -> None:
    baseline_armor = PHASE5_ARMORS[0]
    for row in rows:
        if row.get("task", {}).get("trial") not in trials:
            continue
        row["state"]["targetArmor"].update(
            {
                "base": target_armor,
                "effective": target_armor,
                "armor": target_armor,
            }
        )
        marker = row.get("marker")
        if not isinstance(marker, dict):
            continue
        if "targetArmor" in marker:
            marker["targetArmor"] = target_armor
        if "stratumTargetArmor" in marker:
            marker["stratumTargetArmor"] = target_armor
        if "armorReductionFromBaseline" in marker:
            marker["armorReductionFromBaseline"] = baseline_armor - target_armor


def _phase5_repower_trial(rows: list[dict], trial: int, attack_power: int) -> None:
    for row in rows:
        if row.get("task", {}).get("trial") != trial:
            continue
        row["state"]["attackPower"]["base"] = attack_power
        row["state"]["attackPower"]["effective"] = attack_power
        marker = row.get("marker")
        if isinstance(marker, dict):
            if "attackPower" in marker:
                marker["attackPower"] = attack_power
            if "referenceAttackPower" in marker:
                marker["referenceAttackPower"] = attack_power


class CalibrationSummaryTests(unittest.TestCase):
    def test_phase5_armor_strata_builds_strict_completed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase5-valid.jsonl"
            _write_jsonl(source, _phase5_completed_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["deferred_analysis"], [])
            self.assertEqual(
                document["task_completions"][0]["analysis_status"], "detailed"
            )
            self.assertEqual(len(document["specialized_runs"]), 1)
            run = document["specialized_runs"][0]
            self.assertEqual(run["task_id"], PHASE5_TASK_ID)
            self.assertEqual(
                run["analyzer"], "bloodthirst_armor_strata_damage_v1"
            )
            self.assertTrue(run["completion_confirmed"])
            self.assertEqual(run["completed_trials"], 16)
            self.assertEqual(
                run["fixed_control"],
                {
                    "target_guid": PHASE5_TARGET_GUID,
                    "attack_power": 1180,
                    "baseline_target_armor": 4211,
                    "same_target_and_attack_power_all_valid_samples": True,
                },
            )
            self.assertEqual(list(run["armor_strata"]), [
                "sunder_0",
                "sunder_1",
                "sunder_3",
                "sunder_5",
            ])
            for stacks in PHASE5_STACKS:
                stratum = run["armor_strata"][f"sunder_{stacks}"]
                self.assertEqual(stratum["valid_normal_hit_count"], 4)
                self.assertEqual(
                    stratum["observed_target_armor"], PHASE5_ARMORS[stacks]
                )
                self.assertEqual(stratum["planned_sunder_stacks"], stacks)
                self.assertEqual(stratum["observed_sunder_stacks"], stacks)
            self.assertTrue(
                run["armor_response"]["observed_target_armors_strictly_decrease"]
            )
            self.assertTrue(
                run["armor_response"][
                    "normal_hit_mean_strictly_increases_as_armor_decreases"
                ]
            )
            self.assertEqual(run["armor_floor_status"], "NOT_REACHED")
            self.assertFalse(run["armor_zero_claim"])
            self.assertFalse(run["registry_promotion_allowed"])
            self.assertEqual(run["simulator_overrides"], [])

    def test_phase5_nonvalid_attempts_are_separate_inventory_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase5-attempts.jsonl"
            _write_jsonl(
                source,
                _phase5_completed_rows(include_nonvalid_attempts=True),
            )

            run = build_calibration_summary(source, registry=REGISTRY)[
                "specialized_runs"
            ][0]

            inventory = run["attempt_inventory"]
            self.assertEqual(inventory["accepted_sample_count"], 16)
            self.assertEqual(inventory["rejected_marker_count"], 2)
            self.assertEqual(inventory["incomplete_marker_count"], 1)
            self.assertEqual(inventory["setup_incomplete_marker_count"], 1)
            self.assertEqual(inventory["nonvalid_attempt_marker_count"], 4)
            self.assertEqual(
                inventory["counts_by_reason"],
                {
                    "critical_hit_not_counted": 1,
                    "target_armor_changed_before_cast": 1,
                },
            )
            self.assertEqual(
                inventory["incomplete_counts_by_reason"],
                {"typed_evidence_timeout": 1},
            )
            self.assertEqual(
                inventory["setup_incomplete_counts_by_reason"],
                {"sunder_stack_did_not_advance": 1},
            )
            setup = inventory["setup_incomplete_attempts"][0]
            self.assertFalse(setup["counted_as_valid_sample"])
            self.assertFalse(setup["counted_as_bloodthirst_attempt"])
            self.assertEqual(setup["requested_from_stacks"], 0)
            self.assertEqual(len(run["trials"]), 16)

    def test_phase5_rejects_typed_chain_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase5-ordering.jsonl"
            rows = _phase5_completed_rows()
            completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row["marker"]["trial"] == 1
            )
            completion["marker"]["serverGoSequence"] = completion["marker"][
                "resultSequence"
            ]
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("ordering", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_phase5_rejects_target_ap_and_within_stratum_armor_drift(self) -> None:
        cases = {
            "target GUID drift": lambda rows: _phase5_retarget_trial(
                rows, 6, "0xF13000C55200D1FF"
            ),
            "attack-power drift": lambda rows: _phase5_repower_trial(rows, 6, 1181),
            "target armor drift within sunder_1": lambda rows: _phase5_rearmor_trials(
                rows, {6}, 3760
            ),
        }
        for expected_reason, mutate in cases.items():
            with self.subTest(expected_reason=expected_reason):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    source = Path(temporary_directory) / "phase5-drift.jsonl"
                    rows = _phase5_completed_rows()
                    mutate(rows)
                    _write_jsonl(source, rows)

                    document = build_calibration_summary(source, registry=REGISTRY)

                    inventory = document["task_completions"][0]
                    self.assertEqual(
                        inventory["analysis_status"], "incomplete_evidence"
                    )
                    self.assertIn(expected_reason, inventory["analysis_reason"])
                    self.assertEqual(document["specialized_runs"], [])

    def test_phase5_requires_armor_to_strictly_decrease_across_strata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase5-nondecreasing.jsonl"
            rows = _phase5_completed_rows()
            _phase5_rearmor_trials(rows, {5, 6, 7, 8}, PHASE5_ARMORS[0])
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("not strictly decreasing", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_phase5_claims_zero_only_when_target_armor_is_observed_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase5-zero.jsonl"
            rows = _phase5_completed_rows()
            _phase5_rearmor_trials(rows, {13, 14, 15, 16}, 0)
            _write_jsonl(source, rows)

            run = build_calibration_summary(source, registry=REGISTRY)[
                "specialized_runs"
            ][0]

            self.assertEqual(run["armor_floor_status"], "OBSERVED_ZERO")
            self.assertTrue(run["armor_zero_claim"])
            self.assertFalse(run["registry_promotion_allowed"])

    def test_phase4_ap_strata_supports_rank4_raw_damage_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase4-valid.jsonl"
            _write_jsonl(source, _phase4_completed_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["deferred_analysis"], [])
            self.assertEqual(
                document["task_completions"][0]["analysis_status"], "detailed"
            )
            self.assertEqual(document["runs"], [])
            self.assertEqual(len(document["specialized_runs"]), 1)
            run = document["specialized_runs"][0]
            self.assertEqual(run["task_id"], PHASE4_TASK_ID)
            self.assertEqual(
                run["analyzer"], "bloodthirst_ap_strata_damage_v1"
            )
            self.assertTrue(run["completion_confirmed"])
            self.assertEqual(run["completed_trials"], 8)
            self.assertEqual(
                run["fixed_control"],
                {
                    "target_guid": PHASE4_TARGET_GUID,
                    "observed_target_armor": 4211,
                    "same_target_and_armor_all_valid_samples": True,
                },
            )
            strata = run["attack_power_strata"]
            self.assertEqual(strata["no_battle_shout"]["valid_normal_hit_count"], 4)
            self.assertEqual(strata["no_battle_shout"]["attack_power"], 1000)
            self.assertEqual(
                strata["battle_shout_observed_delta"]["valid_normal_hit_count"],
                4,
            )
            self.assertEqual(
                strata["battle_shout_observed_delta"]["attack_power"], 1400
            )
            self.assertEqual(strata["observed_attack_power_delta"], 400)

            comparison = run["damage_model_comparison"]
            fits = {
                fit["candidate"]: fit for fit in comparison["candidate_fits"]
            }
            rank4 = fits["rank4_raw_200_plus_0_35_ap"]
            legacy = fits["legacy_raw_0_45_ap"]
            self.assertEqual(rank4["fitted_common_scale"], 0.6)
            self.assertEqual(rank4["rmse_damage"], 0)
            self.assertTrue(
                all(item["residual_damage"] == 0 for item in rank4["residuals"])
            )
            self.assertGreater(legacy["rmse_damage"], rank4["rmse_damage"])
            self.assertEqual(comparison["judgment"]["status"], "SUPPORT")
            self.assertEqual(
                comparison["judgment"]["supported_candidate"],
                "rank4_raw_200_plus_0_35_ap",
            )
            self.assertFalse(comparison["absolute_damage_conclusion"])
            self.assertFalse(comparison["live_simulator_template_armor_used"])
            self.assertFalse(comparison["registry_promotion_allowed"])
            self.assertEqual(run["simulator_overrides"], [])

    def test_phase4_ap_strata_rejects_target_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase4-target-drift.jsonl"
            rows = _phase4_completed_rows()
            _phase4_retarget_trial(rows, 5, "0xF13000C55200D1FF")
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("target GUID drift", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_phase4_ap_strata_rejects_target_armor_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase4-armor-drift.jsonl"
            rows = _phase4_completed_rows()
            _phase4_rearmor_trial(rows, 5, 3761)
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("target armor drift", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_phase4_nonvalid_attempts_are_inventory_not_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase4-rejected.jsonl"
            _write_jsonl(
                source,
                _phase4_completed_rows(
                    include_rejected_crit_and_miss=True,
                    include_incomplete_attempt=True,
                    include_setup_rejection=True,
                ),
            )

            run = build_calibration_summary(source, registry=REGISTRY)[
                "specialized_runs"
            ][0]

            inventory = run["attempt_inventory"]
            self.assertEqual(inventory["accepted_sample_count"], 8)
            self.assertEqual(inventory["rejected_marker_count"], 3)
            self.assertEqual(inventory["incomplete_marker_count"], 1)
            self.assertEqual(inventory["nonvalid_attempt_marker_count"], 4)
            self.assertEqual(
                inventory["counts_by_reason"],
                {
                    "critical_hit_not_counted": 1,
                    "miss_not_counted": 1,
                    "target_armor_changed_before_cast": 1,
                },
            )
            self.assertFalse(inventory["rejected_markers_count_as_valid_samples"])
            self.assertFalse(inventory["incomplete_markers_count_as_valid_samples"])
            self.assertEqual(
                inventory["incomplete_counts_by_reason"],
                {"typed_evidence_timeout": 1},
            )
            self.assertTrue(
                all(
                    attempt["counted_as_valid_sample"] is False
                    for attempt in inventory["rejected_attempts"]
                )
            )
            setup_rejection = next(
                attempt
                for attempt in inventory["rejected_attempts"]
                if attempt["reason"] == "target_armor_changed_before_cast"
            )
            self.assertEqual(
                setup_rejection["rejection_phase"],
                "bloodthirst_ap_setup_rejected",
            )
            self.assertIsNone(setup_rejection["trigger_sequence"])
            self.assertEqual(setup_rejection["target_armor"], 3971)
            self.assertEqual(setup_rejection["reference_target_armor"], 4211)
            self.assertEqual(
                setup_rejection["reference_target_guid"], PHASE4_TARGET_GUID
            )
            self.assertFalse(
                inventory["incomplete_attempts"][0]["counted_as_valid_sample"]
            )
            self.assertEqual(
                inventory["incomplete_attempts"][0]["action_sequence"],
                inventory["incomplete_attempts"][0]["sequence"] - 1,
            )
            self.assertEqual(len(run["trials"]), 8)

    def test_phase4_ap_strata_requires_all_eight_normal_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase4-short.jsonl"
            rows = _phase4_completed_rows()
            rows = [
                row
                for row in rows
                if not (
                    row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                    and row.get("marker", {}).get("trial") == 8
                )
            ]
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("exactly 8", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_phase3_campaign_builds_strict_mechanics_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3.jsonl"
            _write_jsonl(source, _phase3_completed_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["deferred_analysis"], [])
            self.assertEqual(
                {item["analysis_status"] for item in document["task_completions"]},
                {"detailed"},
            )
            specialized = {
                run["task_id"]: run for run in document["specialized_runs"]
            }
            self.assertEqual(
                set(specialized),
                {
                    "warrior_whirlwind_cooldown_transition",
                    "warrior_cleave_queue_swing",
                    "warrior_white_swing_rage_transition",
                },
            )

            whirlwind = specialized["warrior_whirlwind_cooldown_transition"]
            self.assertEqual(
                whirlwind["analyzer"], "whirlwind_cooldown_transition_v1"
            )
            self.assertTrue(whirlwind["promotion_gate"]["ready"])
            cooldown = whirlwind["evidence_comparisons"][0]
            self.assertEqual(cooldown["estimate"], 8.5)
            self.assertEqual(cooldown["comparison"], "CONSISTENT")
            self.assertFalse(
                cooldown["supporting_observations"]["interval_is_exact_duration"]
            )
            self.assertEqual(
                cooldown["supporting_observations"][
                    "successful_server_go_intervals"
                ],
                [8.52],
            )

            cleave = specialized["warrior_cleave_queue_swing"]
            self.assertEqual(cleave["analyzer"], "cleave_queue_swing_v1")
            self.assertTrue(cleave["promotion_gate"]["ready"])
            cleave_parameters = {
                item["registry_target"]["field"]: item
                for item in cleave["evidence_comparisons"]
            }
            self.assertEqual(
                cleave_parameters["current_character_rage_cost"]["estimate"], 18
            )
            self.assertEqual(
                cleave_parameters["replaces_next_main_hand_swing"]["estimate"],
                True,
            )
            self.assertFalse(cleave["promotion_gate"]["expected_cost_is_observation"])

            white = specialized["warrior_white_swing_rage_transition"]
            self.assertEqual(
                white["analyzer"], "white_swing_rage_transition_v2"
            )
            self.assertEqual(white["observation_gate"]["clean_sample_count"], 3)
            self.assertEqual(
                [
                    (
                        trial["swing"]["hit_info"],
                        trial["swing"]["critical"],
                        trial["swing"]["glancing"],
                    )
                    for trial in white["trials"]
                ],
                [
                    (2, False, False),
                    (130, True, False),
                    (16386, False, True),
                ],
            )
            self.assertEqual(
                white["observation_gate"]["outcome_coverage"],
                ["critical", "glancing", "ordinary"],
            )
            self.assertEqual(
                white["observation_gate"]["known_unbridled_wrath_proc_samples"],
                0,
            )
            self.assertFalse(
                white["trials"][0]["rage"]["unbridled_wrath_proc"]["observed"]
            )
            self.assertEqual(
                white["trials"][0]["rage"]["base_gain_after_known_proc"], 6
            )
            self.assertFalse(white["observation_gate"]["formula_identified"])
            self.assertFalse(
                white["observation_gate"]["registry_promotion_allowed"]
            )
            self.assertEqual(document["campaigns"][0]["campaign_id"], "warrior_fury_dummy_ravager_rage_phase3")

    def test_phase3_white_terminal_marker_salvages_only_contiguous_retained_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-white-ring.jsonl"
            rows = _phase3_completed_rows()
            first_retained = next(
                index
                for index, row in enumerate(rows)
                if row["event"] == "CALIBRATION_TRIAL_STARTED"
                and row.get("task", {}).get("taskId")
                == "warrior_white_swing_rage_transition"
                and row.get("task", {}).get("trial") == 2
            )
            _write_jsonl(source, rows[first_retained:])

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "partial_retained")
            self.assertEqual(inventory["completed_trials"], 3)
            self.assertEqual(inventory["retained_evidence_trials"], 2)
            self.assertEqual(inventory["missing_trial_numbers"], [1])
            white = document["specialized_runs"][0]
            self.assertEqual(white["status"], "completed_with_truncated_evidence")
            self.assertTrue(white["terminal_completion_confirmed"])
            self.assertFalse(white["completion_confirmed"])
            self.assertEqual(white["missing_trial_numbers"], [1])
            self.assertEqual([trial["trial"] for trial in white["trials"]], [2, 3])

    def test_phase3_marker_state_mismatch_is_incomplete_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-mismatch.jsonl"
            rows = _phase3_completed_rows()
            whirlwind_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row.get("marker", {}).get("taskId")
                == "warrior_whirlwind_cooldown_transition"
            )
            whirlwind_completion["marker"]["observedCooldownSeconds"] = 7.0
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = next(
                item
                for item in document["task_completions"]
                if item["task_id"] == "warrior_whirlwind_cooldown_transition"
            )
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("cooldown marker/state mismatch", inventory["analysis_reason"])
            self.assertNotIn(
                "warrior_whirlwind_cooldown_transition",
                {run["task_id"] for run in document["specialized_runs"]},
            )

    def test_phase3_whirlwind_accepts_first_go_direct_cooldown_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-go-fallback.jsonl"
            rows = _phase3_completed_rows()
            trial_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row.get("marker", {}).get("taskId")
                == "warrior_whirlwind_cooldown_transition"
            )
            first_go_sequence = trial_completion["marker"]["firstServerGoSequence"]
            first_go = next(
                row for row in rows if row["sequence"] == first_go_sequence
            )
            del first_go["state"]["cooldowns"]["whirlwind"]
            trial_completion["marker"].update(
                {
                    "cooldownObservationSequence": first_go_sequence,
                    "cooldownObservationEvent": "SPELL_GO_SELF",
                    "observedCooldownSeconds": 8.5,
                }
            )
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            whirlwind = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_whirlwind_cooldown_transition"
            )
            cooldown = whirlwind["trials"][0]["cooldown"]
            self.assertEqual(
                cooldown["remaining_observation_source"],
                "direct_GetSpellCooldown_remaining",
            )
            self.assertTrue(whirlwind["promotion_gate"]["ready"])

    def test_phase3_cleave_accepts_cast_type_on_swing_without_queue_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-cleave-cast-type.jsonl"
            rows = _phase3_completed_rows()
            rows = [
                row
                for row in rows
                if not (
                    row["event"] == "SPELL_QUEUE_EVENT"
                    and row.get("task", {}).get("taskId")
                    == "warrior_cleave_queue_swing"
                )
            ]
            trial_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row.get("marker", {}).get("taskId")
                == "warrior_cleave_queue_swing"
            )
            trial_completion["marker"].update(
                {
                    "queuePoppedSeen": False,
                    "queueEvidence": "spell_cast_event_on_swing",
                }
            )
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            cleave = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_cleave_queue_swing"
            )
            queue = cleave["trials"][0]["queue"]
            self.assertTrue(queue["accepted"])
            self.assertFalse(queue["buffered"])
            self.assertFalse(queue["popped"])
            self.assertTrue(cleave["promotion_gate"]["ready"])

    def test_phase3_cleave_accepts_same_packet_miss_before_server_go(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-cleave-miss-before-go.jsonl"
            rows = _phase3_completed_rows()
            cleave_go = next(
                row
                for row in rows
                if row["event"] == "SPELL_GO_SELF"
                and row.get("task", {}).get("taskId")
                == "warrior_cleave_queue_swing"
            )
            cleave_result = next(
                row
                for row in rows
                if row["event"] == "SPELL_DAMAGE_EVENT_SELF"
                and row.get("task", {}).get("taskId")
                == "warrior_cleave_queue_swing"
            )
            cleave_go["sequence"], cleave_result["sequence"] = (
                cleave_result["sequence"],
                cleave_go["sequence"],
            )
            cleave_result.update(
                {
                    "event": "SPELL_MISS_SELF",
                    "spellID": 20569,
                    "time": cleave_go["time"],
                    "missInfo": 3,
                }
            )
            cleave_result.pop("amount")
            cleave_result.pop("hitInfo")
            trial_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row.get("marker", {}).get("taskId")
                == "warrior_cleave_queue_swing"
            )
            trial_completion["marker"].update(
                {
                    "serverGoSequence": cleave_go["sequence"],
                    "resultSequence": cleave_result["sequence"],
                    "resultEvent": cleave_result["event"],
                }
            )
            rows.sort(key=lambda row: row["sequence"])
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            cleave = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_cleave_queue_swing"
            )
            self.assertTrue(cleave["promotion_gate"]["ready"])
            self.assertEqual(cleave["trials"][0]["outcome"]["event"], "SPELL_MISS_SELF")
            self.assertTrue(
                cleave["trials"][0]["outcome"]["reported_before_server_go"]
            )

    def test_phase3_white_normalizes_one_complete_unbridled_wrath_event_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-white-proc.jsonl"
            _write_jsonl(source, _phase3_completed_rows(white_proc_trial=1))

            document = build_calibration_summary(source, registry=REGISTRY)

            white = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_white_swing_rage_transition"
            )
            first_rage = white["trials"][0]["rage"]
            self.assertTrue(first_rage["identifiable"])
            self.assertEqual(first_rage["confounds"], [])
            self.assertEqual(
                first_rage["unbridled_wrath_proc"]["raw_energize_amount"], 20
            )
            self.assertEqual(
                first_rage["unbridled_wrath_proc"]["normalized_rage_gain"], 2
            )
            self.assertEqual(first_rage["base_gain_after_known_proc"], 4)
            self.assertEqual(
                white["observation_gate"]["known_unbridled_wrath_proc_samples"],
                1,
            )
            self.assertEqual(first_rage["rage_per_damage"], round(4 / 350, 6))
            self.assertEqual(
                first_rage["net_rage_per_damage"], round(6 / 350, 6)
            )

    def test_phase3_white_accepts_same_packet_proc_before_swing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-white-proc-before-swing.jsonl"
            _write_jsonl(
                source,
                _phase3_completed_rows(
                    white_proc_trial=1,
                    white_proc_before_swing=True,
                ),
            )

            document = build_calibration_summary(source, registry=REGISTRY)

            white = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_white_swing_rage_transition"
            )
            first_rage = white["trials"][0]["rage"]
            self.assertTrue(first_rage["identifiable"])
            self.assertEqual(first_rage["confounds"], [])
            proc_sequences = first_rage["unbridled_wrath_proc"]["event_sequences"]
            self.assertEqual(len(proc_sequences), 2)
            self.assertTrue(
                all(
                    sequence < white["trials"][0]["sequences"]["swing"]
                    for sequence in proc_sequences
                )
            )
            self.assertEqual(
                first_rage["unbridled_wrath_proc"]["normalized_rage_gain"], 2
            )
            self.assertEqual(
                first_rage["unbridled_wrath_proc"]["packet_order"],
                "before_swing",
            )
            self.assertEqual(first_rage["base_gain_after_known_proc"], 4)

    def test_phase3_cleave_normalizes_one_complete_unbridled_wrath_event_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-cleave-proc.jsonl"
            _write_jsonl(source, _phase3_completed_rows(cleave_proc=True))

            document = build_calibration_summary(source, registry=REGISTRY)

            cleave = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_cleave_queue_swing"
            )
            rage = cleave["trials"][0]["rage"]
            self.assertTrue(rage["identifiable"])
            self.assertEqual(rage["observed_net_drop"], 16)
            self.assertEqual(rage["inferred_cost"], 18)
            self.assertEqual(rage["confounds"], [])
            self.assertEqual(
                rage["unbridled_wrath_proc"]["normalized_rage_gain"], 2
            )
            self.assertTrue(cleave["promotion_gate"]["ready"])

    def test_phase3_white_does_not_normalize_an_incomplete_energize_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "phase3-white-incomplete-proc.jsonl"
            rows = [
                row
                for row in _phase3_completed_rows(white_proc_trial=1)
                if not (
                    row["event"] == "SPELL_ENERGIZE_ON_SELF"
                    and row.get("task", {}).get("taskId")
                    == "warrior_white_swing_rage_transition"
                    and row.get("task", {}).get("trial") == 1
                )
            ]
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            white = next(
                run
                for run in document["specialized_runs"]
                if run["task_id"] == "warrior_white_swing_rage_transition"
            )
            first_rage = white["trials"][0]["rage"]
            self.assertFalse(first_rage["identifiable"])
            self.assertFalse(
                first_rage["unbridled_wrath_proc"]["observed"]
            )
            self.assertEqual(
                [item["event"] for item in first_rage["confounds"]],
                ["SPELL_ENERGIZE_BY_SELF"],
            )

    def test_slam_six_trial_run_builds_detailed_timing_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam.jsonl"
            _write_jsonl(source, _slam_completed_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["deferred_analysis"], [])
            self.assertEqual(
                document["task_completions"][0]["analysis_status"], "detailed"
            )
            self.assertEqual(len(document["specialized_runs"]), 1)
            slam = document["specialized_runs"][0]
            self.assertEqual(slam["task_id"], "warrior_slam_timing_transition")
            self.assertEqual(slam["analyzer"], "slam_timing_transition_v1")
            self.assertEqual(slam["completed_trials"], 6)
            self.assertTrue(slam["completion_confirmed"])
            self.assertEqual(
                [trial["mode"] for trial in slam["trials"]],
                [
                    "no_flurry_early",
                    "no_flurry_late",
                    "no_flurry_late",
                    "flurry_early",
                    "flurry_late",
                    "flurry_late",
                ],
            )

            first, second, third, fourth = slam["trials"][:4]
            self.assertEqual(first["wrapper_spell_id"], 45961)
            self.assertEqual(second["result_spell_id"], 45964)
            self.assertEqual(first["cast_timing_ms"]["advertised"], 2500)
            self.assertEqual(first["cast_timing_ms"]["actual_start_to_go"], 2500)
            self.assertFalse(first["precast"]["flurry_active"])
            self.assertTrue(fourth["precast"]["flurry_active"])
            self.assertEqual(
                fourth["cast_timing_ms"]["actual_start_to_go"], 1923
            )
            self.assertTrue(first["rage_drop_evidence"]["identifiable"])
            self.assertEqual(first["rage_drop_evidence"]["net_drop"], 15)
            self.assertEqual(
                first["first_main_hand_after_action"]["sequence"],
                first["sequences"]["next_main_hand"],
            )
            self.assertEqual(first["swing_deadline"]["deadline_deviation_ms"], 0)
            self.assertEqual(
                first["swing_deadline"]["classification"],
                "preserved_precast_deadline",
            )
            self.assertEqual(
                second["swing_deadline"]["classification"],
                "paused_for_actual_cast",
            )
            self.assertEqual(
                third["swing_deadline"]["classification"],
                "reset_after_server_go",
            )

    def test_slam_accepts_clean_post_go_rage_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam-snapshot.jsonl"
            _write_jsonl(source, _slam_completed_rows(snapshot_trial=2))

            document = build_calibration_summary(source, registry=REGISTRY)

            slam = document["specialized_runs"][0]
            second = slam["trials"][1]
            rage = second["rage_drop_evidence"]
            self.assertEqual(rage["evidence_kind"], "state_snapshot")
            self.assertEqual(rage["observation_source"], "state_snapshot")
            self.assertEqual(rage["snapshot_event"], "SPELL_DAMAGE_EVENT_SELF")
            self.assertEqual(
                rage["snapshot_sequence"], second["sequences"]["result"]
            )
            self.assertEqual(
                second["sequences"]["rage_observation"],
                second["sequences"]["result"],
            )
            self.assertIsNone(rage["resource_event"])
            self.assertIsNone(rage["resource_sequence"])
            self.assertEqual(rage["snapshot_state_rage"], 45)
            self.assertEqual(rage["net_drop"], 15)
            self.assertTrue(rage["identifiable"])
            self.assertEqual(second["quality_flags"], [])
            self.assertTrue(slam["completion_confirmed"])

    def test_slam_salvages_completed_trials_after_ring_buffer_prefix_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam-truncated-prefix.jsonl"
            rows = _slam_completed_rows()
            second_trial_start = next(
                row["sequence"]
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_STARTED"
                and row["task"]["trial"] == 2
            )
            retained = [row for row in rows if row["sequence"] >= second_trial_start]
            _write_jsonl(source, retained)

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["deferred_analysis"], [])
            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "partial_retained")
            self.assertEqual(inventory["completed_trials"], 6)
            self.assertEqual(inventory["retained_evidence_trials"], 5)
            self.assertEqual(inventory["missing_trial_numbers"], [1])

            slam = document["specialized_runs"][0]
            self.assertEqual(slam["status"], "completed_with_truncated_evidence")
            self.assertEqual(slam["completed_trials"], 6)
            self.assertEqual(slam["retained_evidence_trials"], 5)
            self.assertEqual(slam["missing_trial_numbers"], [1])
            self.assertEqual(slam["missing_trial_modes"], ["no_flurry_early"])
            self.assertFalse(slam["completion_confirmed"])
            self.assertTrue(slam["terminal_completion_confirmed"])
            self.assertTrue(slam["timing_coverage_sufficient"])
            self.assertTrue(slam["rage_cost_coverage_sufficient"])
            self.assertEqual(
                [trial["trial"] for trial in slam["trials"]],
                [2, 3, 4, 5, 6],
            )

    def test_slam_next_main_hand_rage_snapshot_is_conservatively_confounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam-snapshot-confounded.jsonl"
            _write_jsonl(
                source,
                _slam_completed_rows(
                    snapshot_trial=2, snapshot_source="next_main_hand"
                ),
            )

            document = build_calibration_summary(source, registry=REGISTRY)

            slam = document["specialized_runs"][0]
            second = slam["trials"][1]
            rage = second["rage_drop_evidence"]
            self.assertEqual(rage["evidence_kind"], "state_snapshot")
            self.assertEqual(rage["snapshot_event"], "AUTO_ATTACK_SELF")
            self.assertFalse(rage["identifiable"])
            self.assertEqual(
                [item["event"] for item in rage["confounds"]],
                ["AUTO_ATTACK_SELF"],
            )
            self.assertIn("rage_transition_confounded", second["quality_flags"])
            self.assertFalse(slam["completion_confirmed"])

    def test_slam_energize_before_rage_snapshot_is_conservatively_confounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam-snapshot-energize.jsonl"
            _write_jsonl(
                source,
                _slam_completed_rows(
                    snapshot_trial=2, snapshot_source="result_after_energize"
                ),
            )

            document = build_calibration_summary(source, registry=REGISTRY)

            slam = document["specialized_runs"][0]
            second = slam["trials"][1]
            rage = second["rage_drop_evidence"]
            self.assertFalse(rage["identifiable"])
            self.assertEqual(
                [item["event"] for item in rage["confounds"]],
                ["SPELL_ENERGIZE_ON_SELF"],
            )
            self.assertIn("rage_transition_confounded", second["quality_flags"])

    def test_slam_rage_snapshot_cannot_reference_an_unrelated_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "slam-snapshot-invalid.jsonl"
            rows = _slam_completed_rows(snapshot_trial=2)
            second_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row["marker"]["trial"] == 2
            )
            second_completion["marker"]["postGoRageSnapshotSequence"] = (
                second_completion["marker"]["actionStartSequence"]
            )
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            inventory = document["task_completions"][0]
            self.assertEqual(inventory["analysis_status"], "incomplete_evidence")
            self.assertIn("same-run GO, result, or next main-hand", inventory["analysis_reason"])
            self.assertEqual(document["specialized_runs"], [])

    def test_incomplete_specialized_task_is_inventoried_without_blocking_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "incomplete-hs.jsonl"
            rows = _campaign_rows()
            hs_trial = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
                and row.get("marker", {}).get("taskId")
                == "warrior_heroic_strike_queue_swing"
            )
            del hs_trial["marker"]["actionStartSequence"]
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

            hs_inventory = next(
                item
                for item in document["task_completions"]
                if item["task_id"] == "warrior_heroic_strike_queue_swing"
            )
            self.assertEqual(
                hs_inventory["analysis_status"], "incomplete_evidence"
            )
            self.assertIn("actionStartSequence", hs_inventory["analysis_reason"])
            self.assertNotIn(
                "warrior_heroic_strike_queue_swing",
                {run["task_id"] for run in document["specialized_runs"]},
            )
            self.assertEqual(
                document["deferred_analysis"][0]["task_id"],
                "warrior_heroic_strike_queue_swing",
            )
            self.assertEqual(
                document["campaigns"][0]["status"], "awaiting_export_reload"
            )

    def test_campaign_builds_all_fury_dummy_specialized_analyses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "campaign.jsonl"
            output = root / "campaign-summary.json"
            _write_jsonl(source, _campaign_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["schema_version"], 2)
            self.assertEqual(len(document["runs"]), 1)
            self.assertEqual(
                document["runs"][0]["task_id"],
                "warrior_bloodthirst_transition",
            )
            self.assertEqual(len(document["task_completions"]), 4)
            completion_by_task = {
                item["task_id"]: item for item in document["task_completions"]
            }
            self.assertEqual(
                completion_by_task["warrior_bloodthirst_transition"][
                    "analysis_status"
                ],
                "detailed",
            )
            self.assertEqual(
                completion_by_task["warrior_heroic_strike_queue_swing"][
                    "completed_trials"
                ],
                2,
            )
            self.assertEqual(
                completion_by_task["warrior_bloodthirst_until_crit"][
                    "completed_trials"
                ],
                1,
            )
            self.assertEqual(
                completion_by_task["warrior_execute_transition"][
                    "completed_trials"
                ],
                2,
            )
            self.assertEqual(
                {
                    item["analysis_status"]
                    for item in document["task_completions"]
                },
                {"detailed"},
            )
            self.assertEqual(document["deferred_analysis"], [])

            specialized = {
                run["task_id"]: run for run in document["specialized_runs"]
            }
            self.assertEqual(len(specialized), 3)
            hs = specialized["warrior_heroic_strike_queue_swing"]
            self.assertEqual(hs["completed_trials"], 2)
            self.assertEqual(hs["analyzer"], "heroic_strike_queue_swing_v2")
            self.assertEqual(hs["trials"][0]["execution_ms"]["cast_to_acceptance"], 0)
            self.assertIsNone(hs["trials"][0]["execution_ms"]["cast_to_queued"])
            self.assertFalse(hs["trials"][0]["queue"]["buffered"])
            self.assertFalse(hs["trials"][0]["queue"]["popped"])
            self.assertEqual(
                hs["trials"][0]["queue"]["evidence"],
                "spell_cast_event_on_swing",
            )
            self.assertEqual(hs["trials"][1]["execution_ms"]["cast_to_queued"], 10)
            self.assertTrue(hs["trials"][1]["queue"]["buffered"])
            self.assertTrue(hs["trials"][1]["queue"]["popped"])
            self.assertEqual(hs["trials"][0]["rage"]["inferred_cost"], 15)
            hs_comparisons = {
                item["registry_target"]["field"]: item
                for item in hs["evidence_comparisons"]
            }
            self.assertEqual(
                hs_comparisons["consumes_gcd"]["comparison"], "CONSISTENT"
            )
            self.assertEqual(
                hs_comparisons["replaces_next_main_hand_swing"]["comparison"],
                "CONSISTENT",
            )
            self.assertEqual(
                hs_comparisons["rage_cost"]["comparison"],
                "CONSISTENT_WITH_DESCRIPTION",
            )
            self.assertEqual(
                hs_comparisons["miss_refund_fraction"]["estimate"], 0
            )
            self.assertEqual(
                hs_comparisons["miss_refund_fraction"]["comparison"],
                "OBSERVED_DIFFERS_SINGLE_TRIAL",
            )
            self.assertEqual(
                hs["talent_context"]["improved_heroic_strike"]["rank"], 3
            )

            crit = specialized["warrior_bloodthirst_until_crit"]
            self.assertEqual(crit["analyzer"], "bloodthirst_until_crit_v2")
            self.assertTrue(crit["completion_confirmed"])
            self.assertEqual(crit["total_cast_attempts"], 3)
            self.assertEqual(crit["total_damage_attempts"], 2)
            crit_trial = crit["trials"][0]
            self.assertEqual(crit_trial["misses_before_crit"], 1)
            self.assertEqual(crit_trial["crit_completion"]["hit_info"], 2)
            self.assertEqual(crit_trial["crit_completion"]["amount"], 644)
            self.assertEqual(
                crit_trial["combat_context"]["attack_power"]["effective"], 1250
            )
            self.assertEqual(
                crit["evidence_comparisons"][0]["estimate"], 0
            )
            self.assertEqual(
                crit["evidence_comparisons"][0]["comparison"],
                "OBSERVED_DIFFERS_SINGLE_TRIAL",
            )

            execute = specialized["warrior_execute_transition"]
            self.assertEqual(execute["analyzer"], "execute_transition_v2")
            self.assertEqual(execute["completed_trials"], 2)
            self.assertEqual(
                [point["planned_rage_threshold"] for point in execute["regression_series"]],
                [10, 20],
            )
            self.assertEqual(
                execute["regression_series"][1]["miss_net_rage_spent"], 10
            )
            self.assertEqual(
                execute["regression_series"][1]["miss_extra_rage_retained"], 10
            )
            execute_comparisons = {
                item["registry_target"]["field"]: item
                for item in execute["evidence_comparisons"]
            }
            self.assertEqual(
                execute_comparisons["gcd_seconds"]["comparison"], "CONSISTENT"
            )
            self.assertEqual(
                execute_comparisons["execute_phase"]["comparison"],
                "CONSISTENT_BELOW_20_PERCENT",
            )
            self.assertEqual(
                execute_comparisons["base_rage_cost"]["comparison"],
                "CONSISTENT_WITH_CURRENT_BUILD_DESCRIPTION",
            )
            self.assertEqual(
                execute_comparisons["miss_refund_fraction"]["estimate"], 0
            )
            self.assertEqual(
                execute_comparisons["extra_rage_retained_on_miss"]["estimate"],
                True,
            )
            self.assertEqual(
                execute["talent_context"]["improved_execute"]["rank"], 2
            )

            self.assertEqual(len(document["campaigns"]), 1)
            campaign = document["campaigns"][0]
            self.assertEqual(campaign["campaign_id"], "warrior_fury_dummy_phase1")
            self.assertEqual(campaign["status"], "awaiting_export_reload")
            self.assertEqual(campaign["completed_task_count"], 4)
            self.assertEqual(campaign["reported_completed_task_count"], 4)
            self.assertEqual(
                campaign["deferred_tasks"],
                [
                    {
                        "task_id": "warrior_target_armor_floor",
                        "status": "deferred_collection",
                    }
                ],
            )
            self.assertEqual(
                campaign["next_instruction"],
                "reload_once_for_automatic_import",
            )

            result = summarize_calibration(
                source,
                registry=REGISTRY,
                output=output,
            )
            self.assertEqual(result.run_count, 1)
            self.assertEqual(result.trial_count, 1)
            self.assertEqual(result.specialized_run_count, 3)
            self.assertEqual(result.completed_task_count, 4)
            self.assertEqual(result.completed_trial_count, 6)
            self.assertEqual(result.deferred_analysis_count, 0)
            self.assertEqual(result.campaign_count, 1)
            self.assertTrue(output.is_file())

    def test_old_completed_run_extracts_typed_timing_without_inventing_rage_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "calibration.jsonl"
            _write_jsonl(source, _completed_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

            self.assertEqual(document["kind"], "brainofcat_calibration_summary")
            self.assertEqual(len(document["runs"]), 1)
            run = document["runs"][0]
            self.assertEqual(run["task_run_id"], RUN_ID)
            self.assertEqual(run["telemetry"]["nampower_version"], "4.1.0")
            trial = run["trials"][0]
            self.assertEqual(trial["sequences"]["action"], 3)
            self.assertEqual(trial["sequences"]["result"], 7)
            self.assertEqual(trial["execution_ms"]["client_to_server_start"], 41)
            self.assertEqual(trial["execution_ms"]["client_to_go"], 76)
            self.assertEqual(trial["execution_ms"]["client_to_result"], 79)
            self.assertEqual(trial["mechanics"]["gcd_seconds"], 1.5)
            self.assertEqual(
                trial["mechanics"]["cooldown"]["estimated_total_seconds"],
                6.001,
            )
            self.assertEqual(trial["outcome"]["amount"], 322)
            self.assertEqual(trial["combat_context"]["player_level"], 60)
            self.assertEqual(
                trial["combat_context"]["attack_power"]["effective"], 1250
            )
            self.assertEqual(
                trial["combat_context"]["target_armor"]["effective"], 4211
            )
            self.assertFalse(trial["rage"]["identifiable"])
            self.assertEqual(
                trial["rage"]["reason"],
                "missing_task_bounded_resource_transition",
            )

            parameters = _parameters(run)
            self.assertEqual(parameters["rage_cost"]["status"], "UNKNOWN")
            self.assertEqual(parameters["rage_cost"]["comparison"], "NOT_OBSERVED")
            self.assertEqual(parameters["gcd_seconds"]["comparison"], "CONSISTENT")
            self.assertEqual(parameters["cooldown_seconds"]["comparison"], "CONSISTENT")
            self.assertEqual(run["simulator_overrides"], [])

    def test_clean_unit_rage_transition_produces_registry_comparison_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "calibration.jsonl"
            _write_jsonl(source, _completed_rows(with_resource=True))

            run = build_calibration_summary(source, registry=REGISTRY)["runs"][0]
            trial = run["trials"][0]
            self.assertTrue(trial["rage"]["identifiable"])
            self.assertEqual(trial["rage"]["before"], 98)
            self.assertEqual(trial["rage"]["after"], 68)
            self.assertEqual(trial["rage"]["inferred_cost"], 30)

            rage = _parameters(run)["rage_cost"]
            self.assertEqual(rage["observations"], [30])
            self.assertEqual(rage["estimate"], 30)
            self.assertEqual(rage["status"], "OBSERVED_SINGLE_TRIAL")
            self.assertEqual(rage["comparison"], "CONSISTENT")
            self.assertEqual(run["simulator_overrides"], [])

    def test_auto_attack_before_resource_event_marks_rage_transition_confounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "calibration.jsonl"
            _write_jsonl(
                source,
                _completed_rows(with_resource=True, confounded=True),
            )

            run = build_calibration_summary(source, registry=REGISTRY)["runs"][0]
            trial = run["trials"][0]
            self.assertFalse(trial["rage"]["identifiable"])
            self.assertEqual(trial["rage"]["reason"], "resource_transition_confounded")
            self.assertIn("confound:AUTO_ATTACK_SELF@8", trial["quality_flags"])
            self.assertEqual(_parameters(run)["rage_cost"]["status"], "UNKNOWN")

    def test_cli_does_not_publish_when_no_task_completion_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "incomplete.jsonl"
            output = root / "summary.json"
            _write_jsonl(source, _completed_rows()[:2])
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(
                    [
                        str(source),
                        "--registry",
                        str(REGISTRY),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(return_code, 2)
            self.assertFalse(output.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no completed task markers", stderr.getvalue())

    def test_marker_action_reference_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "calibration.jsonl"
            rows = _completed_rows()
            trial_completion = next(
                row
                for row in rows
                if row["event"] == "CALIBRATION_TRIAL_COMPLETED"
            )
            trial_completion["marker"]["actionStartSequence"] = 4
            _write_jsonl(source, rows)

            with self.assertRaisesRegex(
                CalibrationSummaryError,
                "not a successful Bloodthirst cast",
            ):
                build_calibration_summary(source, registry=REGISTRY)


if __name__ == "__main__":
    unittest.main()
