"""Replay live Phase-5 Bloodthirst armor strata in the O2O simulator.

Each live stratum already contains the target's observed post-debuff armor.
That value is written directly to the simulator target.  Simulator armor
debuffs (including Sunder Armor) are disabled, so the replay cannot apply the
same reduction twice.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Protocol, Sequence

from .bloodthirst_matched_replay import infer_real_armor_penetration
from .sim_bridge import ActionRef, AvailableAction, SimBridgeError, SimulatorBridge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_live.json"
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "bloodthirst_phase5_armor_matched_replay.json"
)
DEFAULT_SEEDS = tuple(range(1, 33))

BLOODTHIRST_ARMOR_TASK = "warrior_bloodthirst_armor_strata_damage"
EXPECTED_ANALYZER = "bloodthirst_armor_strata_damage_v1"
BLOODTHIRST_SPELL_ID = 23894
TARGET_ARMOR_STAT_INDEX = 26
ATTACKER_LEVEL = 60
EXPECTED_SUNDER_STACKS = (0, 1, 3, 5)
ARMOR_DEBUFF_FIELDS: Mapping[str, Any] = {
    "sunderArmor": False,
    "exposeArmor": "TristateEffectMissing",
    "faerieFire": False,
    "curseOfRecklessness": False,
    "crystalYield": False,
}
ARMOR_AURA_LABELS = frozenset(
    {
        "Sunder Armor",
        "ExposeArmor",
        "Faerie Fire",
        "Faerie Fire (Feral)",
        "Curse of Recklessness",
        "Crystal Yield",
    }
)


JSONMap = dict[str, Any]


class ArmorMatchedReplayError(ValueError):
    """The Phase-5 summary or replay inputs do not satisfy the data contract."""


class BridgeLike(Protocol):
    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(self, action: ActionRef | Mapping[str, Any]) -> Any: ...


def load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArmorMatchedReplayError(f"could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ArmorMatchedReplayError(f"{label} must be a JSON object")
    return value


def build_stratum_request(
    profile: Mapping[str, Any], observed_target_armor: float
) -> JSONMap:
    """Clone the profile and apply one live post-debuff armor observation."""

    armor = _nonnegative_number(observed_target_armor, "observed_target_armor")
    request = deepcopy(dict(profile))
    try:
        raid = request["raid"]
        player = raid["parties"][0]["players"][0]
        target = request["encounter"]["targets"][0]
        sim_options = request["simOptions"]
    except (KeyError, IndexError, TypeError) as error:
        raise ArmorMatchedReplayError(
            "profile must contain raid.parties[0].players[0], "
            "encounter.targets[0], and simOptions"
        ) from error
    if not all(isinstance(value, dict) for value in (raid, player, target, sim_options)):
        raise ArmorMatchedReplayError(
            "profile raid, player, target, and simOptions must be objects"
        )

    # The retained live profile needs this captured world buff to reproduce the
    # campaign AP.  The AP gate below prevents this overlay from being accepted
    # for a capture made under different buffs.
    player_buffs = player.setdefault("buffs", {})
    if not isinstance(player_buffs, dict):
        raise ArmorMatchedReplayError("profile player.buffs must be an object")
    player_buffs["rallyingCryOfTheDragonslayer"] = True

    debuffs = raid.setdefault("debuffs", {})
    if not isinstance(debuffs, dict):
        raise ArmorMatchedReplayError("profile raid.debuffs must be an object")
    debuffs.update(ARMOR_DEBUFF_FIELDS)

    stats = target.get("stats")
    if not isinstance(stats, list):
        raise ArmorMatchedReplayError("profile encounter.targets[0].stats must be an array")
    while len(stats) <= TARGET_ARMOR_STAT_INDEX:
        stats.append(0)
    stats[TARGET_ARMOR_STAT_INDEX] = armor
    sim_options["iterations"] = 1
    sim_options["interactive"] = True
    return request


def continuous_bloodthirst_normal_damage(
    melee_attack_power: float,
    effective_target_armor: float,
    *,
    attacker_level: int = ATTACKER_LEVEL,
) -> float:
    """Return the simulator's pre-rounding, landed non-critical BT damage."""

    attack_power = _nonnegative_number(melee_attack_power, "melee_attack_power")
    armor = _nonnegative_number(effective_target_armor, "effective_target_armor")
    if isinstance(attacker_level, bool) or not isinstance(attacker_level, int):
        raise ArmorMatchedReplayError("attacker_level must be an integer")
    if attacker_level <= 0:
        raise ArmorMatchedReplayError("attacker_level must be positive")
    raw_damage = 200.0 + 0.35 * attack_power
    armor_multiplier = 1.0 - armor / (armor + 400.0 + 85.0 * attacker_level)
    return raw_damage * armor_multiplier


def replay_phase5(
    summary: Mapping[str, Any],
    profile: Mapping[str, Any],
    bridge: BridgeLike,
    *,
    seeds: Sequence[int] = DEFAULT_SEEDS,
    real_armor_penetration: float | None = None,
    real_armor_penetration_evidence: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Replay every observed armor stratum and evaluate strict controls."""

    normalized_seeds = _seeds(seeds)
    run = _phase5_run(summary)
    fixed = _fixed_control(run)
    strata = _armor_strata(
        run,
        fixed["baseline_target_armor"],
        fixed["attack_power"],
    )
    observed_zero = any(
        math.isclose(stratum["observed_target_armor"], 0.0, abs_tol=1e-9)
        for stratum in strata
    )
    if run.get("armor_zero_claim") is not observed_zero:
        raise ArmorMatchedReplayError(
            "Phase-5 armor_zero_claim disagrees with the observed armor strata"
        )
    expected_floor_status = "OBSERVED_ZERO" if observed_zero else "NOT_REACHED"
    if run.get("armor_floor_status") != expected_floor_status:
        raise ArmorMatchedReplayError(
            "Phase-5 armor_floor_status disagrees with the observed armor strata"
        )
    armor_penetration = (
        None
        if real_armor_penetration is None
        else _nonnegative_number(real_armor_penetration, "real_armor_penetration")
    )

    rendered_strata: list[JSONMap] = []
    all_checks: list[JSONMap] = []
    for stratum in strata:
        observed_armor = stratum["observed_target_armor"]
        request = build_stratum_request(profile, observed_armor)
        replay_rows = [
            _cast_bloodthirst(bridge, request, seed) for seed in normalized_seeds
        ]
        checks = _stratum_environment_checks(
            replay_rows,
            stratum=stratum,
            reference_attack_power=fixed["attack_power"],
            real_armor_penetration=armor_penetration,
        )
        all_checks.extend(checks)

        expected_effective = (
            max(observed_armor - armor_penetration, 0.0)
            if armor_penetration is not None
            else None
        )
        continuous_prediction = (
            continuous_bloodthirst_normal_damage(
                fixed["attack_power"], expected_effective
            )
            if expected_effective is not None
            else None
        )
        integer_comparison = _integer_display_comparison(
            stratum["damages"], continuous_prediction
        )
        rendered_strata.append(
            {
                **stratum,
                "request_controls": {
                    "target_armor_direct_from_live_observation": observed_armor,
                    "simulator_armor_debuffs": dict(ARMOR_DEBUFF_FIELDS),
                    "actions": [{"spell_id": BLOODTHIRST_SPELL_ID}],
                    "sunder_action_used": False,
                },
                "expected_effective_target_armor": expected_effective,
                "continuous_normal_hit_prediction": {
                    "damage": continuous_prediction,
                    "formula": (
                        "(200 + 0.35 * melee_attack_power) * "
                        "(1 - effective_armor / "
                        "(effective_armor + 400 + 85 * attacker_level))"
                    ),
                    "attacker_level": ATTACKER_LEVEL,
                    "melee_attack_power": fixed["attack_power"],
                    "effective_target_armor": expected_effective,
                },
                "real_integer_comparison": integer_comparison,
                "unfiltered_simulator_outcomes": _damage_summary(
                    row["bloodthirst_damage"] for row in replay_rows
                ),
                "replay_rows": replay_rows,
                "environment_checks": checks,
            }
        )

    environment_matched = all(check["matched"] for check in all_checks)
    integer_matches = all(
        stratum["real_integer_comparison"]["all_in_floor_ceil_set"]
        for stratum in rendered_strata
    )
    failed_checks = [
        f"{check['stratum']}.{check['field']}"
        for check in all_checks
        if not check["matched"]
    ]
    validation_status = "matched" if environment_matched else "not_matched"
    return {
        "schema_version": 1,
        "kind": "bloodthirst_phase5_armor_matched_replay",
        "validation_status": validation_status,
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "task_id": BLOODTHIRST_ARMOR_TASK,
        "task_run_id": run.get("task_run_id") or run.get("run_id"),
        "analyzer": EXPECTED_ANALYZER,
        "seeds": normalized_seeds,
        "real_environment": {
            "target_guid": fixed["target_guid"],
            "attack_power": fixed["attack_power"],
            "baseline_target_armor": fixed["baseline_target_armor"],
            "armor_penetration": armor_penetration,
            "armor_penetration_evidence": (
                dict(real_armor_penetration_evidence)
                if real_armor_penetration_evidence is not None
                else {"status": "missing"}
            ),
        },
        "armor_semantics": {
            "observed_target_armor_is_applied_directly": True,
            "simulator_sunder_enabled": False,
            "simulator_sunder_cast": False,
            "double_application_prevented": True,
        },
        "armor_strata": rendered_strata,
        "environment_checks": all_checks,
        "conclusion_gate": {
            "environment_matched": environment_matched,
            "failed_checks": failed_checks,
            "damage_comparison_allowed": environment_matched,
            "all_live_integer_damages_in_prediction_floor_ceil_sets": (
                integer_matches if environment_matched else None
            ),
            "interpretation": (
                "matched_armor_response_replay"
                if environment_matched and integer_matches
                else (
                    "environment_matched_damage_differs"
                    if environment_matched
                    else "descriptive_only_environment_not_matched"
                )
            ),
        },
        "armor_zero_claim": observed_zero,
        "armor_zero_evidence": {
            "observed_zero_target_armor": observed_zero,
            "minimum_observed_target_armor": min(
                stratum["observed_target_armor"] for stratum in strata
            ),
            "reason": (
                "a live stratum directly observed target armor 0"
                if observed_zero
                else "no live stratum observed target armor 0"
            ),
        },
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
        real_armor_penetration, evidence = infer_real_armor_penetration(
            summary, summary_path
        )
    else:
        real_armor_penetration = _nonnegative_number(
            real_armor_penetration, "real_armor_penetration"
        )
        evidence = {
            "status": "explicit_cli_value",
            "amount": real_armor_penetration,
        }
    document = replay_phase5(
        summary,
        profile,
        bridge,
        seeds=seeds,
        real_armor_penetration=real_armor_penetration,
        real_armor_penetration_evidence=evidence,
    )
    document["sources"] = {
        "calibration_summary": str(summary_path.resolve()),
        "simulator_profile": str(profile_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument(
        "--seed",
        type=int,
        action="append",
        dest="seeds",
        help="replay seed; repeat to override default seeds 1..32",
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
    except (OSError, ArmorMatchedReplayError, SimBridgeError) as error:
        print(f"Bloodthirst armor matched replay failed: {error}", file=sys.stderr)
        return 2


def _phase5_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise ArmorMatchedReplayError("input is not a BrainOfCat calibration summary")
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise ArmorMatchedReplayError("summary.specialized_runs must be an array")
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping)
        and run.get("task_id") == BLOODTHIRST_ARMOR_TASK
    ]
    if len(matches) != 1:
        raise ArmorMatchedReplayError(
            f"expected exactly one completed {BLOODTHIRST_ARMOR_TASK} run, "
            f"got {len(matches)}"
        )
    run = matches[0]
    if run.get("analyzer") != EXPECTED_ANALYZER:
        raise ArmorMatchedReplayError(
            f"Phase-5 analyzer must be exactly {EXPECTED_ANALYZER!r}"
        )
    if run.get("status") != "completed" or run.get("completion_confirmed") is not True:
        raise ArmorMatchedReplayError("Phase-5 armor-strata run is not confirmed complete")
    task_run_id = run.get("task_run_id")
    if not isinstance(task_run_id, str) or not task_run_id:
        raise ArmorMatchedReplayError("Phase-5 task_run_id must be non-empty")
    return run


def _fixed_control(run: Mapping[str, Any]) -> JSONMap:
    fixed = _mapping(run, "fixed_control")
    target_guid = fixed.get("target_guid")
    if not isinstance(target_guid, str) or not target_guid.strip():
        raise ArmorMatchedReplayError("fixed_control.target_guid must be non-empty")
    if fixed.get("same_target_and_attack_power_all_valid_samples") is not True:
        raise ArmorMatchedReplayError(
            "fixed_control does not confirm one target and attack power"
        )
    return {
        "target_guid": target_guid,
        "attack_power": _nonnegative_number(
            fixed.get("attack_power"),
            "fixed_control.attack_power",
        ),
        "baseline_target_armor": _nonnegative_number(
            fixed.get("baseline_target_armor"),
            "fixed_control.baseline_target_armor",
        ),
    }


def _armor_strata(
    run: Mapping[str, Any], baseline_target_armor: float, attack_power: float
) -> list[JSONMap]:
    source = run.get("armor_strata")
    if not isinstance(source, Mapping):
        raise ArmorMatchedReplayError("armor_strata must be an object keyed by stratum")
    expected_keys = tuple(f"sunder_{stacks}" for stacks in EXPECTED_SUNDER_STACKS)
    if set(source) != set(expected_keys):
        raise ArmorMatchedReplayError(
            "armor_strata must contain exactly sunder_0, sunder_1, sunder_3, and sunder_5"
        )

    result: list[JSONMap] = []
    seen_stacks: set[int] = set()
    for index, mapping_key in enumerate(expected_keys):
        value = source[mapping_key]
        if not isinstance(value, Mapping):
            raise ArmorMatchedReplayError(f"armor_strata[{index}] must be an object")
        label = mapping_key
        stacks = _nonnegative_int(
            value.get("planned_sunder_stacks"),
            f"armor_strata[{label}].planned_sunder_stacks",
        )
        if stacks in seen_stacks:
            raise ArmorMatchedReplayError(f"duplicate planned_sunder_stacks: {stacks}")
        seen_stacks.add(stacks)
        expected_stacks = EXPECTED_SUNDER_STACKS[index]
        if stacks != expected_stacks:
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}].planned_sunder_stacks must be {expected_stacks}"
            )
        observed_stacks = _nonnegative_int(
            value.get("observed_sunder_stacks"),
            f"armor_strata[{label}].observed_sunder_stacks",
        )
        if observed_stacks != stacks:
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}] observed Sunder stacks differ from planned"
            )
        sample_count = _nonnegative_int(
            value.get("valid_normal_hit_count"),
            f"armor_strata[{label}].valid_normal_hit_count",
        )
        if sample_count != 4:
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}] must contain exactly four valid normal hits"
            )
        stratum_attack_power = _nonnegative_number(
            value.get("attack_power"), f"armor_strata[{label}].attack_power"
        )
        if not math.isclose(stratum_attack_power, attack_power, abs_tol=1e-9):
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}] attack power differs from fixed control"
            )
        observed = _nonnegative_number(
            value.get("observed_target_armor"),
            f"armor_strata[{label}].observed_target_armor",
        )
        reduction = _nonnegative_number(
            value.get("armor_reduction_from_baseline"),
            f"armor_strata[{label}].armor_reduction_from_baseline",
        )
        expected_reduction = baseline_target_armor - observed
        if expected_reduction < -1e-9 or not math.isclose(
            reduction, expected_reduction, abs_tol=1e-9
        ):
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}] reduction does not equal baseline minus observed armor"
            )
        damages_value = value.get("damages")
        if not isinstance(damages_value, list) or len(damages_value) != sample_count:
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}].damages must contain four values"
            )
        damages = [
            _nonnegative_int(damage, f"armor_strata[{label}].damages")
            for damage in damages_value
        ]
        mean_damage = _nonnegative_number(
            value.get("mean_damage"), f"armor_strata[{label}].mean_damage"
        )
        if not math.isclose(
            mean_damage,
            statistics.fmean(float(damage) for damage in damages),
            abs_tol=1e-9,
        ):
            raise ArmorMatchedReplayError(
                f"armor_strata[{label}].mean_damage disagrees with damages"
            )
        result.append(
            {
                "stratum": label,
                "planned_sunder_stacks": stacks,
                "observed_sunder_stacks": observed_stacks,
                "valid_normal_hit_count": sample_count,
                "attack_power": stratum_attack_power,
                "observed_target_armor": observed,
                "armor_reduction_from_baseline": reduction,
                "damages": damages,
                "mean_damage": mean_damage,
            }
        )

    if tuple(sorted(seen_stacks)) != EXPECTED_SUNDER_STACKS:
        raise ArmorMatchedReplayError(
            "Phase-5 armor strata must contain planned Sunder stacks 0, 1, 3, and 5"
        )
    result.sort(key=lambda item: item["planned_sunder_stacks"])
    if not all(
        later["observed_target_armor"] < earlier["observed_target_armor"]
        for earlier, later in zip(result, result[1:])
    ):
        raise ArmorMatchedReplayError(
            "observed target armor must strictly decrease across the four strata"
        )
    baseline = result[0]
    if not math.isclose(
        baseline["observed_target_armor"], baseline_target_armor, abs_tol=1e-9
    ):
        raise ArmorMatchedReplayError(
            "the 0-stack stratum must equal fixed_control.baseline_target_armor"
        )
    return result


def _cast_bloodthirst(
    bridge: BridgeLike, request: Mapping[str, Any], seed: int
) -> JSONMap:
    before = bridge.load(request, seed)
    target_auras = before.get("target_auras")
    if target_auras is not None and not isinstance(target_auras, list):
        raise ArmorMatchedReplayError("simulator target_auras state must be an array")
    result = bridge.act(_action(bridge.actions(), BLOODTHIRST_SPELL_ID))
    if result.casted is not True:
        raise ArmorMatchedReplayError(f"seed {seed}: simulator did not cast Bloodthirst")
    damage = _damage_delta(before, result.state, seed)
    return {
        "seed": seed,
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
        raise ArmorMatchedReplayError(
            f"expected exactly one tag-0 simulator action for spell {spell_id}, "
            f"got {len(matches)}"
        )
    return matches[0]


def _state_projection(state: Mapping[str, Any]) -> JSONMap:
    return {
        field: state.get(field)
        for field in (
            "time_ms",
            "damage_done",
            "strength",
            "melee_attack_power",
            "armor_penetration",
            "target_armor",
            "effective_target_armor",
            "power",
            "target_auras",
        )
    }


def _damage_delta(
    before: Mapping[str, Any], after: Mapping[str, Any], seed: int
) -> float:
    before_damage = _number(before.get("damage_done"), f"seed {seed} before Bloodthirst")
    after_damage = _number(after.get("damage_done"), f"seed {seed} after Bloodthirst")
    return after_damage - before_damage


def _stratum_environment_checks(
    rows: Sequence[Mapping[str, Any]],
    *,
    stratum: Mapping[str, Any],
    reference_attack_power: float,
    real_armor_penetration: float | None,
) -> list[JSONMap]:
    label = str(stratum["stratum"])

    def values(field: str) -> list[Any]:
        return [row["state_before_bloodthirst"].get(field) for row in rows]

    observed_armor = float(stratum["observed_target_armor"])
    expected_effective = (
        max(observed_armor - real_armor_penetration, 0.0)
        if real_armor_penetration is not None
        else None
    )
    forbidden_auras = [
        aura
        for row in rows
        for aura in (row["state_before_bloodthirst"].get("target_auras") or [])
        if isinstance(aura, Mapping) and aura.get("label") in ARMOR_AURA_LABELS
    ]
    return [
        _check(label, "melee_attack_power", reference_attack_power, values("melee_attack_power")),
        _check(label, "target_armor", observed_armor, values("target_armor")),
        _check(label, "armor_penetration", real_armor_penetration, values("armor_penetration")),
        _check(
            label,
            "effective_target_armor",
            expected_effective,
            values("effective_target_armor"),
        ),
        {
            "stratum": label,
            "field": "simulator_armor_debuff_auras",
            "expected_live": [],
            "observed_simulator_values": [
                {"label": aura.get("label"), "action": aura.get("action")}
                for aura in forbidden_auras
            ],
            "matched": not forbidden_auras,
            "reason": (
                None
                if not forbidden_auras
                else "simulator applied an armor debuff after direct armor assignment"
            ),
        },
    ]


def _check(
    stratum: str, field: str, expected: float | None, observed: Sequence[Any]
) -> JSONMap:
    matched = expected is not None and all(
        _is_number(value) and math.isclose(float(value), expected, abs_tol=1e-9)
        for value in observed
    )
    return {
        "stratum": stratum,
        "field": field,
        "expected_live": expected,
        "observed_simulator_values": _unique(observed),
        "matched": matched,
        "reason": (
            "live expected value is not evidenced"
            if expected is None
            else (
                None
                if matched
                else "one or more simulator values differ or are missing"
            )
        ),
    }


def _integer_display_comparison(
    real_damages: Sequence[int], continuous_prediction: float | None
) -> JSONMap:
    if continuous_prediction is None:
        return {
            "floor_ceil_set": None,
            "rows": [
                {"real_damage": damage, "error_to_floor_ceil_set": None}
                for damage in real_damages
            ],
            "all_in_floor_ceil_set": False,
            "maximum_error_to_floor_ceil_set": None,
            "mean_error_to_floor_ceil_set": None,
        }
    allowed = sorted({math.floor(continuous_prediction), math.ceil(continuous_prediction)})
    rows = [
        {
            "real_damage": damage,
            "error_to_floor_ceil_set": min(abs(damage - value) for value in allowed),
            "in_floor_ceil_set": damage in allowed,
        }
        for damage in real_damages
    ]
    errors = [row["error_to_floor_ceil_set"] for row in rows]
    return {
        "floor_ceil_set": allowed,
        "rows": rows,
        "all_in_floor_ceil_set": all(row["in_floor_ceil_set"] for row in rows),
        "maximum_error_to_floor_ceil_set": max(errors),
        "mean_error_to_floor_ceil_set": statistics.fmean(errors),
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
        raise ArmorMatchedReplayError(f"{field} must be an object")
    return result


def _number(value: Any, field: str) -> float:
    if not _is_number(value):
        raise ArmorMatchedReplayError(f"{field} must be a finite number")
    return float(value)


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise ArmorMatchedReplayError(f"{field} must be non-negative")
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArmorMatchedReplayError(f"{field} must be a non-negative integer")
    return value


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _seeds(values: Sequence[int]) -> list[int]:
    if not values:
        raise ArmorMatchedReplayError("at least one seed is required")
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ArmorMatchedReplayError("seeds must be positive integers")
        if value in result:
            raise ArmorMatchedReplayError(f"duplicate seed: {value}")
        result.append(value)
    return result


def _unique(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
