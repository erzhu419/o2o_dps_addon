"""After formal v7 store exists, replay Cat once with v56 target diagnostics."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, build_plan_v1

RUNTIME = f"{BASE}/runtime-src-20260921-white6603-v7"
STATIC_RUNTIME = f"{BASE}/runtime-src-20260921-causal-target-v6"
DISPATCH = (
    f"{BASE}/runs/teammate-response-current/single_scan_direct_white6603_target_choice_v7/"
    "53a85ece0dbe56b95d072a47226ce6d2b5a73e540e1e9f350c1fe1cc04010c02/"
    "attempt1/dispatch/dispatch.json"
)
OUTPUT = ROOT / "results/responsive-team-v4/v62-d900-v7-formal-d-cat-target-diagnostic-seed1.json"
V56 = ROOT / "results/responsive-team-v4/v56-d900-v6-cat-target-diagnostic-seed1.json"
V57 = ROOT / "results/responsive-team-v4/v57-d900-historical-spell-target-through9098.json"


def _early_spell_rows(diagnostic: dict) -> list[dict]:
    grouped: dict[tuple[int, int | None], dict] = {}
    for row in diagnostic["groups"]:
        if row["time_bucket"] != "through_9098ms" or row["event_type"] != "DMG" or row["target_index"] is None:
            continue
        key = row["target_index"], row["spell_id"]
        item = grouped.setdefault(key, {
            "target_index": key[0], "spell_id": key[1],
            "applied_hit_count": 0, "applied_damage": 0.0,
        })
        item["applied_hit_count"] += row["applied_hit_count"]
        item["applied_damage"] += row["applied_damage"]
    return [grouped[key] for key in sorted(grouped, key=lambda item: (item[0], -1 if item[1] is None else item[1]))]


def _early_white_basis_rows(diagnostic: dict) -> list[dict]:
    return [
        {
            "target_index": row["target_index"],
            "target_selection_basis": row["target_selection_basis"],
            "event_count": row["event_count"],
            "applied_hit_count": row["applied_hit_count"],
            "applied_damage": row["applied_damage"],
        }
        for row in diagnostic["groups"]
        if row["time_bucket"] == "through_9098ms"
        and row["event_type"] == "DMG"
        and row["spell_id"] == 6603
        and row["target_index"] is not None
    ]


def _all_white_basis_counts(diagnostic: dict) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in diagnostic["groups"]:
        if row["event_type"] != "DMG" or row["spell_id"] != 6603:
            continue
        basis = row["target_selection_basis"]
        item = grouped.setdefault(basis, {
            "target_selection_basis": basis, "event_count": 0,
            "applied_hit_count": 0, "applied_damage": 0.0,
        })
        item["event_count"] += row["event_count"]
        item["applied_hit_count"] += row["applied_hit_count"]
        item["applied_damage"] += row["applied_damage"]
    return [grouped[basis] for basis in sorted(grouped)]


def _white_comparison(current: list[dict], prior: list[dict], historical: dict) -> list[dict]:
    current_white = {row["target_index"]: row for row in current if row["spell_id"] == 6603}
    prior_white = {row["target_index"]: row for row in prior if row["spell_id"] == 6603}
    historical_white = {
        historical["target_guids"].index(row["target_guid"]): row
        for row in historical["per_spell"]
        if row["cutoff_ms_inclusive"] == 9098 and row["owner"] == "teammate_direct" and row["spell_id"] == 6603
    }
    return [
        {
            "target_index": index,
            "target_guid": historical["target_guids"][index],
            "historical_logged_raw_hits": historical_white.get(index, {}).get("hit_count", 0),
            "historical_logged_raw_damage": historical_white.get(index, {}).get("raw_damage", 0),
            "v56_applied_hits": prior_white.get(index, {}).get("applied_hit_count", 0),
            "v56_applied_damage": prior_white.get(index, {}).get("applied_damage", 0.0),
            "v7_applied_hits": current_white.get(index, {}).get("applied_hit_count", 0),
            "v7_applied_damage": current_white.get(index, {}).get("applied_damage", 0.0),
        }
        for index in range(3)
    ]


def _check_same_frozen_case(current: dict, prior: dict) -> None:
    for key in (
        "instance_id", "encounter_id", "wave_id", "focal_player_guid",
        "historical_build_segment_id", "target_contract_status",
        "target_level_hypothesis", "target_armor_hypothesis",
        "attackability_mode", "current_state_route_focus_applied",
    ):
        if current[key] != prior[key]:
            raise RuntimeError(f"v7/v56 frozen case differs: {key}")
    for key in (
        "component_id", "instance_id", "partition_compressed_file_sha256",
        "partition_locator", "stage5_content_sha256",
    ):
        if current["frozen_source_binding"][key] != prior["frozen_source_binding"][key]:
            raise RuntimeError(f"v7/v56 source binding differs: {key}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-store", required=True, help="node004 absolute path to completed formal v7 store")
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    for relative in (
        "o2o_dps/responsive_incantagos_driven_bridge_v1.py",
        "o2o_dps/responsive_action_program_replay_v1.py",
        "scripts/development_responsive_upper_kara_trash_smoke_v1.py",
        "scripts/development_responsive_upper_kara_trash_full_wave_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(node)}:{RUNTIME}/{relative}"],
            check=True, timeout=120,
        )
    plan = build_plan_v1(
        code_root=RUNTIME,
        simulator_root=STATIC_RUNTIME,
        frozen_dispatch=DISPATCH,
        runtime_store=args.runtime_store,
        result_sha=args.result_sha,
        model_sha=args.model_sha,
        exact_build=f"{STATIC_RUNTIME}/results/responsive-team-v4/v34-doomguard-exact-fury-build-request.json",
        deployed_binding=f"{STATIC_RUNTIME}/results/responsive-team-v4/deployed-contra-runtime-binding-v1.951b8faa.json",
        bridge=f"{STATIC_RUNTIME}/bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64",
    )
    argv = list(plan["full_wave"]["argv"])
    argv[argv.index("--source") + 1] = "cat"
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc:
        raise RuntimeError(f"formal v7 Cat diagnostic replay failed (rc={rc}): {stderr[-2500:]}")
    result = json.loads(stdout)
    rows = result["paired_replays"]
    if len(rows) != 1 or rows[0]["source_policy_id"] != "cat.fury.profile1":
        raise RuntimeError("expected one Cat replay")
    replay = rows[0]
    if (
        replay["seed"] != 2026092001 or replay["teammate_seed"] != 2026092002
        or result["historical_build_segment_id"] != "segment-0042"
        or result["attackability_mode"] != "OBSERVED_ONSET_UNTIL_SIM_DEATH"
        or result["current_state_route_focus_applied"] is not True
    ):
        raise RuntimeError("Cat replay differs from v56 frozen setup")
    drive = replay["responsive_drive"]
    diagnostic = drive["target_event_diagnostic"]
    if sum(row["event_count"] for row in diagnostic["groups"]) != drive["responsive_event_count"] or not math.isclose(
        sum(row["applied_damage"] for row in diagnostic["groups"]),
        drive["responsive_applied_damage"], abs_tol=1e-6,
    ):
        raise RuntimeError("target diagnostic does not conserve applied event/damage totals")
    prior = json.loads(V56.read_text(encoding="utf-8"))
    _check_same_frozen_case(result, prior)
    historical = json.loads(V57.read_text(encoding="utf-8"))
    early = _early_spell_rows(diagnostic)
    prior_early = _early_spell_rows(prior["paired_replays"][0]["responsive_drive"]["target_event_diagnostic"])
    compact = {
        "schema": "development_d900_v7_formal_cat_target_diagnostic/v1",
        "status": "DEVELOPMENT_ONLY_NOT_DPS_COMPARISON",
        "formal_model_result_sha": args.result_sha,
        "formal_model_sha": args.model_sha,
        "source_policy_id": replay["source_policy_id"],
        "replay_status": replay["status"],
        "valid_development_completion": replay["valid_development_completion"],
        "invalid_reason": replay["invalid_reason"],
        "terminal_all_targets_dead": replay["terminal_all_targets_dead"],
        "v56_reference": {
            "effective_damage": prior["paired_replays"][0]["effective_damage"],
            "elapsed_ms": prior["paired_replays"][0]["elapsed_ms"],
            "valid_development_completion": prior["paired_replays"][0]["valid_development_completion"],
            "first_observed_dead_ms": prior["paired_replays"][0]["first_observed_dead_ms"],
            "target_timeline_snapshots": prior["paired_replays"][0]["target_timeline_snapshots"],
        },
        "effective_damage": replay["effective_damage"],
        "elapsed_ms": replay["elapsed_ms"],
        "first_observed_dead_ms": replay["first_observed_dead_ms"],
        "target_timeline_snapshots": replay["target_timeline_snapshots"],
        "route_focus_receipts": replay["route_focus_receipts"],
        "responsive_event_count": drive["responsive_event_count"],
        "responsive_applied_damage": drive["responsive_applied_damage"],
        "direct_start_head_counts": diagnostic["direct_start_head_counts"],
        "first_white_6603_groups": diagnostic["first_white_6603_groups"],
        "all_white_6603_by_selection_basis": _all_white_basis_counts(diagnostic),
        "early_white_6603_by_target_and_selection_basis": _early_white_basis_rows(diagnostic),
        "early_applied_damage_by_target_spell": early,
        "white_6603_through_9098ms_comparison": _white_comparison(early, prior_early, historical),
        "damage_contract": (
            "Historical Stage5 raw logged amount is not the same as simulator APPLIED HP damage; "
            "simulator emitted-DMG receipts do not expose Chronicle attribution_kind"
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(compact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "effective_damage": replay["effective_damage"],
                      "white_6603_through_9098ms_comparison": compact["white_6603_through_9098ms_comparison"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
