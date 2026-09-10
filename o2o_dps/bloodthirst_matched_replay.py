"""Replay the live Phase-4 Bloodthirst AP strata in the O2O simulator.

The replay is an environment-equivalence gate, not a damage-model evaluator.
It refuses to authorize formula or talent-specialization conclusions whenever
the simulator's exported AP / armor state differs from the live observations.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, Protocol, Sequence

from .sim_bridge import ActionRef, AvailableAction, SimBridgeError, SimulatorBridge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARY = (
    PROJECT_ROOT
    / "offline_data"
    / "calibration_summaries"
    / "BrainOfCat__20260829T183555926397Z.json"
)
DEFAULT_PROFILE = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_live.json"
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "bloodthirst_phase4_matched_replay.json"
)
DEFAULT_SEEDS = tuple(range(1, 33))

BLOODTHIRST_TASK = "warrior_bloodthirst_ap_strata_damage"
BLOODTHIRST_SPELL_ID = 23894
BATTLE_SHOUT_SPELL_ID = 25289
TARGET_ARMOR_STAT_INDEX = 26
STATE_FIELDS = (
    "strength",
    "melee_attack_power",
    "armor_penetration",
    "target_armor",
    "effective_target_armor",
)


JSONMap = dict[str, Any]


class MatchedReplayError(ValueError):
    """The supplied Phase-4 evidence or simulator replay is unusable."""


class BridgeLike(Protocol):
    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def advance(self) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(self, action: ActionRef | Mapping[str, Any]) -> Any: ...


def load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MatchedReplayError(f"could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise MatchedReplayError(f"{label} must be a JSON object")
    return value


def build_matched_request(summary: Mapping[str, Any], profile: Mapping[str, Any]) -> JSONMap:
    """Clone the live profile and apply only the Phase-4 observed controls."""

    run = _phase4_run(summary)
    target_armor = _number(
        _mapping(run, "fixed_control").get("observed_target_armor"),
        "fixed_control.observed_target_armor",
    )
    request = deepcopy(dict(profile))
    try:
        player = request["raid"]["parties"][0]["players"][0]
        target = request["encounter"]["targets"][0]
        sim_options = request["simOptions"]
    except (KeyError, IndexError, TypeError) as error:
        raise MatchedReplayError(
            "profile must contain raid.parties[0].players[0], "
            "encounter.targets[0], and simOptions"
        ) from error
    if not isinstance(player, dict) or not isinstance(target, dict) or not isinstance(
        sim_options, dict
    ):
        raise MatchedReplayError("profile player, target, and simOptions must be objects")

    buffs = player.setdefault("buffs", {})
    if not isinstance(buffs, dict):
        raise MatchedReplayError("profile player.buffs must be an object")
    buffs["rallyingCryOfTheDragonslayer"] = True

    stats = target.get("stats")
    if not isinstance(stats, list):
        raise MatchedReplayError("profile encounter.targets[0].stats must be an array")
    while len(stats) <= TARGET_ARMOR_STAT_INDEX:
        stats.append(0)
    stats[TARGET_ARMOR_STAT_INDEX] = target_armor
    sim_options["iterations"] = 1
    sim_options["interactive"] = True
    return request


def replay_phase4(
    summary: Mapping[str, Any],
    profile: Mapping[str, Any],
    bridge: BridgeLike,
    *,
    seeds: Sequence[int] = DEFAULT_SEEDS,
    real_armor_penetration: float | None = None,
    real_armor_penetration_evidence: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Run both AP strata and return a strict environment-match document."""

    normalized_seeds = _seeds(seeds)
    run = _phase4_run(summary)
    request = build_matched_request(summary, profile)
    fixed_control = _mapping(run, "fixed_control")
    target_armor = _number(
        fixed_control.get("observed_target_armor"),
        "fixed_control.observed_target_armor",
    )
    strata = _mapping(run, "attack_power_strata")
    live_baseline = _live_stratum(strata, "no_battle_shout")
    live_buffed = _live_stratum(strata, "battle_shout_observed_delta")
    observed_delta = _number(
        strata.get("observed_attack_power_delta"),
        "attack_power_strata.observed_attack_power_delta",
    )

    replay_rows: list[JSONMap] = []
    for seed in normalized_seeds:
        baseline = _cast_bloodthirst(bridge, request, seed)
        buffed = _cast_battle_shout_then_bloodthirst(bridge, request, seed)
        replay_rows.append(
            {
                "seed": seed,
                "no_battle_shout": baseline,
                "battle_shout_observed_delta": buffed,
            }
        )

    checks = _environment_checks(
        replay_rows,
        baseline_attack_power=live_baseline["attack_power"],
        buffed_attack_power=live_buffed["attack_power"],
        attack_power_delta=observed_delta,
        target_armor=target_armor,
        real_armor_penetration=real_armor_penetration,
    )
    environment_matched = all(check["matched"] for check in checks)
    failed_fields = [check["field"] for check in checks if not check["matched"]]
    validation_status = "matched" if environment_matched else "not_matched"
    gate_reason = (
        "all exported simulator controls match the live Phase-4 observations"
        if environment_matched
        else "simulator controls differ from or are not evidenced by the live "
        "Phase-4 environment: " + ", ".join(failed_fields)
    )

    return {
        "schema_version": 1,
        "kind": "bloodthirst_phase4_matched_replay",
        "validation_status": validation_status,
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "task_id": BLOODTHIRST_TASK,
        "task_run_id": run.get("task_run_id"),
        "seeds": normalized_seeds,
        "request_controls": {
            "dragonslayer_player_buff": True,
            "target_armor": target_armor,
            "baseline_action": {"spell_id": BLOODTHIRST_SPELL_ID},
            "buffed_actions": [
                {"spell_id": BATTLE_SHOUT_SPELL_ID},
                {"spell_id": BLOODTHIRST_SPELL_ID},
            ],
        },
        "real_environment": {
            "target_guid": fixed_control.get("target_guid"),
            "target_armor": target_armor,
            "armor_penetration": real_armor_penetration,
            "armor_penetration_evidence": (
                dict(real_armor_penetration_evidence)
                if real_armor_penetration_evidence is not None
                else {"status": "missing"}
            ),
            "strata": {
                "no_battle_shout": live_baseline,
                "battle_shout_observed_delta": live_buffed,
            },
            "observed_attack_power_delta": observed_delta,
        },
        "simulator_replay": {
            "outcomes_are_unfiltered": True,
            "note": (
                "damage rows include misses and critical hits; environment matching "
                "uses pre-Bloodthirst state, not damage equality"
            ),
            "rows": replay_rows,
            "damage_summary": {
                "no_battle_shout": _damage_summary(
                    row["no_battle_shout"]["bloodthirst_damage"]
                    for row in replay_rows
                ),
                "battle_shout_observed_delta": _damage_summary(
                    row["battle_shout_observed_delta"]["bloodthirst_damage"]
                    for row in replay_rows
                ),
            },
        },
        "environment_checks": checks,
        "conclusion_gate": {
            "environment_matched": environment_matched,
            "reason": gate_reason,
            "formula_or_specialization_conclusion_allowed": environment_matched,
            "formula_conclusion": None,
            "specialization_conclusion": None,
            "damage_interpretation": (
                "descriptive_replay_ready_for_separate_mechanism_analysis"
                if environment_matched
                else "descriptive_only_environment_not_matched"
            ),
        },
        "matched_request": request,
    }


def infer_real_armor_penetration(
    summary: Mapping[str, Any], summary_path: Path
) -> tuple[float | None, JSONMap]:
    """Infer equipped armor penetration from the captured live item tooltips."""

    source = summary.get("source")
    calibration_value = source.get("calibration_jsonl") if isinstance(source, Mapping) else None
    if not isinstance(calibration_value, str) or not calibration_value:
        return None, {"status": "missing", "reason": "summary_has_no_calibration_jsonl"}
    calibration_path = Path(calibration_value)
    if not calibration_path.is_absolute():
        calibration_path = (summary_path.parent / calibration_path).resolve()
    try:
        lines = calibration_path.open("r", encoding="utf-8")
    except OSError as error:
        return None, {
            "status": "missing",
            "reason": f"could_not_read_calibration_jsonl: {error}",
            "calibration_jsonl": str(calibration_path),
        }

    components: list[JSONMap] = []
    with lines:
        for line_number, line in enumerate(lines, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("event") != "STATIC_PROFILE_CAPTURED":
                continue
            state = row.get("state")
            equipment = state.get("equipment") if isinstance(state, Mapping) else None
            if not isinstance(equipment, list):
                break
            for item in equipment:
                if not isinstance(item, Mapping):
                    continue
                tooltip = item.get("tooltipText")
                if not isinstance(tooltip, str):
                    continue
                # Item tooltips append the full set description.  Only text
                # before the ``Set Name (equipped/total)`` line describes the
                # equipped item's active stats; otherwise an inactive 4-piece
                # armor-penetration bonus would be counted as live equipment.
                direct_tooltip = _tooltip_before_set_description(tooltip)
                for pattern, label in (
                    (r"护甲穿透\s*\+(\d+(?:\.\d+)?)", "armor_penetration_bonus"),
                    (r"无视目标\s*(\d+(?:\.\d+)?)\s*点护甲", "ignores_target_armor"),
                ):
                    for match in re.finditer(pattern, direct_tooltip):
                        components.append(
                            {
                                "slot": item.get("slot"),
                                "item_link": item.get("link"),
                                "amount": float(match.group(1)),
                                "tooltip_semantic": label,
                            }
                        )
            if components:
                return sum(component["amount"] for component in components), {
                    "status": "observed_equipment_tooltips",
                    "calibration_jsonl": str(calibration_path),
                    "line_number": line_number,
                    "components": components,
                }
            break
    return None, {
        "status": "missing",
        "reason": "static_profile_has_no_supported_armor_penetration_tooltip",
        "calibration_jsonl": str(calibration_path),
    }


def run_from_paths(
    summary_path: Path,
    profile_path: Path,
    bridge: BridgeLike,
    *,
    seeds: Sequence[int] = DEFAULT_SEEDS,
    real_armor_penetration: float | None = None,
) -> JSONMap:
    summary = load_json_object(summary_path, "calibration summary")
    profile = load_json_object(profile_path, "simulator profile")
    if real_armor_penetration is None:
        real_armor_penetration, armor_penetration_evidence = (
            infer_real_armor_penetration(summary, summary_path)
        )
    else:
        real_armor_penetration = _number(
            real_armor_penetration, "real armor penetration"
        )
        armor_penetration_evidence = {
            "status": "explicit_cli_value",
            "amount": real_armor_penetration,
        }
    document = replay_phase4(
        summary,
        profile,
        bridge,
        seeds=seeds,
        real_armor_penetration=real_armor_penetration,
        real_armor_penetration_evidence=armor_penetration_evidence,
    )
    document["sources"] = {
        "calibration_summary": str(summary_path.resolve()),
        "simulator_profile": str(profile_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument(
        "--seed",
        type=int,
        action="append",
        dest="seeds",
        help="replay seed; repeat to override the default seeds 1..32",
    )
    parser.add_argument(
        "--real-armor-penetration",
        type=float,
        help="explicit live value; otherwise infer it from captured equipment tooltips",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        with SimulatorBridge(args.bridge) as bridge:
            document = run_from_paths(
                args.summary,
                args.profile,
                bridge,
                seeds=DEFAULT_SEEDS if args.seeds is None else args.seeds,
                real_armor_penetration=args.real_armor_penetration,
            )
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, MatchedReplayError, SimBridgeError) as error:
        print(f"Bloodthirst matched replay failed: {error}", file=sys.stderr)
        return 2


def _phase4_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise MatchedReplayError("input is not a BrainOfCat calibration summary")
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise MatchedReplayError("summary.specialized_runs must be an array")
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping) and run.get("task_id") == BLOODTHIRST_TASK
    ]
    if len(matches) != 1:
        raise MatchedReplayError(
            f"expected exactly one completed {BLOODTHIRST_TASK} run, got {len(matches)}"
        )
    run = matches[0]
    if run.get("status") != "completed" or run.get("completion_confirmed") is not True:
        raise MatchedReplayError("Phase-4 Bloodthirst run is not confirmed complete")
    fixed_control = _mapping(run, "fixed_control")
    if fixed_control.get("same_target_and_armor_all_valid_samples") is not True:
        raise MatchedReplayError("live Phase-4 samples did not hold target and armor fixed")
    return run


def _live_stratum(strata: Mapping[str, Any], name: str) -> JSONMap:
    value = _mapping(strata, name)
    attack_power = _number(value.get("attack_power"), f"{name}.attack_power")
    damages = value.get("damages")
    if not isinstance(damages, list) or not damages:
        raise MatchedReplayError(f"{name}.damages must be a non-empty array")
    rendered_damages = [_number(damage, f"{name}.damages") for damage in damages]
    return {
        "attack_power": attack_power,
        "damages": rendered_damages,
        "valid_normal_hit_count": value.get("valid_normal_hit_count"),
    }


def _cast_bloodthirst(
    bridge: BridgeLike, request: Mapping[str, Any], seed: int
) -> JSONMap:
    before = bridge.load(request, seed)
    result = bridge.act(_action(bridge.actions(), BLOODTHIRST_SPELL_ID))
    if result.casted is not True:
        raise MatchedReplayError(f"seed {seed}: simulator did not cast Bloodthirst")
    damage = _damage_delta(before, result.state, seed, "Bloodthirst")
    return {
        "state_before_bloodthirst": _state_projection(before),
        "bloodthirst_damage": damage,
        "state_after_bloodthirst": _state_projection(result.state),
    }


def _cast_battle_shout_then_bloodthirst(
    bridge: BridgeLike, request: Mapping[str, Any], seed: int
) -> JSONMap:
    loaded = bridge.load(request, seed)
    shout = bridge.act(_action(bridge.actions(), BATTLE_SHOUT_SPELL_ID))
    if shout.casted is not True or shout.consumes_decision is not True:
        raise MatchedReplayError(f"seed {seed}: simulator did not cast Battle Shout")
    before = bridge.advance()
    result = bridge.act(_action(bridge.actions(), BLOODTHIRST_SPELL_ID))
    if result.casted is not True:
        raise MatchedReplayError(
            f"seed {seed}: simulator did not cast Bloodthirst after Battle Shout"
        )
    damage = _damage_delta(before, result.state, seed, "buffed Bloodthirst")
    return {
        "state_on_load": _state_projection(loaded),
        "state_before_bloodthirst": _state_projection(before),
        "bloodthirst_damage": damage,
        "state_after_bloodthirst": _state_projection(result.state),
    }


def _action(actions: Sequence[AvailableAction], spell_id: int) -> ActionRef:
    matches = [
        action.action
        for action in actions
        if action.action.spell_id == spell_id and action.action.tag == 0
    ]
    if len(matches) != 1:
        raise MatchedReplayError(
            f"expected exactly one tag-0 simulator action for spell {spell_id}, "
            f"got {len(matches)}"
        )
    return matches[0]


def _state_projection(state: Mapping[str, Any]) -> JSONMap:
    projected = {field: state.get(field) for field in STATE_FIELDS}
    for field in ("time_ms", "damage_done", "power"):
        projected[field] = state.get(field)
    return projected


def _damage_delta(
    before: Mapping[str, Any], after: Mapping[str, Any], seed: int, label: str
) -> float:
    before_damage = _number(before.get("damage_done"), f"seed {seed} before {label}")
    after_damage = _number(after.get("damage_done"), f"seed {seed} after {label}")
    return after_damage - before_damage


def _environment_checks(
    rows: Sequence[Mapping[str, Any]],
    *,
    baseline_attack_power: float,
    buffed_attack_power: float,
    attack_power_delta: float,
    target_armor: float,
    real_armor_penetration: float | None,
) -> list[JSONMap]:
    def values(stratum: str, field: str) -> list[Any]:
        return [row[stratum]["state_before_bloodthirst"].get(field) for row in rows]

    baseline_ap = values("no_battle_shout", "melee_attack_power")
    buffed_ap = values("battle_shout_observed_delta", "melee_attack_power")
    target_armor_values = values("no_battle_shout", "target_armor") + values(
        "battle_shout_observed_delta", "target_armor"
    )
    armor_penetration_values = values(
        "no_battle_shout", "armor_penetration"
    ) + values("battle_shout_observed_delta", "armor_penetration")
    effective_values = values(
        "no_battle_shout", "effective_target_armor"
    ) + values("battle_shout_observed_delta", "effective_target_armor")
    deltas = [
        buffed - baseline
        if _is_number(baseline) and _is_number(buffed)
        else None
        for baseline, buffed in zip(baseline_ap, buffed_ap)
    ]
    expected_effective = (
        max(target_armor - real_armor_penetration, 0)
        if real_armor_penetration is not None
        else None
    )
    return [
        _check(
            "melee_attack_power.no_battle_shout", baseline_attack_power, baseline_ap
        ),
        _check(
            "melee_attack_power.battle_shout_observed_delta",
            buffed_attack_power,
            buffed_ap,
        ),
        _check("melee_attack_power.battle_shout_delta", attack_power_delta, deltas),
        _check("target_armor", target_armor, target_armor_values),
        _check(
            "armor_penetration", real_armor_penetration, armor_penetration_values
        ),
        _check("effective_target_armor", expected_effective, effective_values),
    ]


def _check(field: str, expected: float | None, observed: Sequence[Any]) -> JSONMap:
    unique = _unique(observed)
    matched = expected is not None and all(
        _is_number(value) and math.isclose(float(value), expected, abs_tol=1e-9)
        for value in observed
    )
    reason = None
    if expected is None:
        reason = "live expected value is not evidenced"
    elif not matched:
        reason = "one or more simulator values differ or are missing"
    return {
        "field": field,
        "expected_live": expected,
        "observed_simulator_values": unique,
        "matched": matched,
        "reason": reason,
    }


def _damage_summary(values: Any) -> JSONMap:
    damages = [float(value) for value in values]
    return {
        "count": len(damages),
        "values": damages,
        "minimum": min(damages),
        "maximum": max(damages),
        "mean": statistics.fmean(damages),
        "zero_damage_count": sum(value == 0 for value in damages),
    }


def _mapping(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    result = value.get(field)
    if not isinstance(result, Mapping):
        raise MatchedReplayError(f"{field} must be an object")
    return result


def _number(value: Any, field: str) -> float:
    if not _is_number(value):
        raise MatchedReplayError(f"{field} must be a finite number")
    return float(value)


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _seeds(values: Sequence[int]) -> list[int]:
    if not values:
        raise MatchedReplayError("at least one seed is required")
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MatchedReplayError("seeds must be positive integers")
        if value in result:
            raise MatchedReplayError(f"duplicate seed: {value}")
        result.append(value)
    return result


def _unique(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _tooltip_before_set_description(tooltip: str) -> str:
    lines = tooltip.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"[（(]\s*\d+\s*/\s*\d+\s*[）)]", line):
            return "\n".join(lines[:index])
    return tooltip


if __name__ == "__main__":
    raise SystemExit(main())
