"""Source-bound actor damage prefix for the fixed two-target Upper Kara wave.

The CSV supplies actor GUID, spell ID, target, event order, and time for each
positive damaging row, including the lethal DEAD rows. Native v14 retargets
only when a source recipient has died in the simulated branch. Future actor
choices, skill timing, and damage magnitude are not learned or responsive.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
import json
import math
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .development_wave_case_v1 import DevelopmentWaveCaseV1, PROJECT_ROOT
from .development_wave_stratified_v1 import (
    SOURCE_FOCAL_ACTORS, WAVE_STRATA, _source_rows,
)
from .development_wave_team_retarget_v1 import (
    SourceFittedTeamRetargetBridgeV1, V14ProjectedDynamicV3Bridge, V14_BRIDGE,
    build_retarget_wave_case_v1,
)
from .fury_cat_gap_three_baseline_registry_v1 import CatGapThreeBaselineRegistryV1
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID


SCHEMA = "development_wave_source_actor_schedule/v1"
DERIVED_SCHEMA = SCHEMA + "/derived_events"
PINNED_EVENT_COUNT = 495


@dataclass(frozen=True)
class SourceActorDamageEventV1:
    schedule_index: int
    source_event_index: int
    time_ms: int
    target_index: int
    actor_guid: str
    actor_kind: str
    owner_guid_suffix: str | None
    spell_id: int
    ability: str
    damage: float
    source_type: str

    @property
    def event_id(self) -> str:
        return f"actor-source-{self.source_event_index}"

    @property
    def wire_event_id(self) -> str:
        return self.event_id


def _source_binding_v1() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    source = _source_rows()[WAVE_STRATA["multi_two"]]
    return source, source["targets"], SOURCE_FOCAL_ACTORS["multi_two"]["guid"]


def _validate_actor_events_v1(events: tuple[SourceActorDamageEventV1, ...]) -> None:
    source, targets, focal_guid = _source_binding_v1()
    expected = [
        int(row["max_health_hypothesis_family"]["observed_kill_budget_proxy"]["value"])
        - SOURCE_FOCAL_ACTORS["multi_two"]["direct_damage_by_target"][index]
        for index, row in enumerate(targets)
    ]
    actual = [
        sum(event.damage for event in events if event.target_index == index)
        for index in range(len(targets))
    ]
    lethal_indices = {
        int(row["max_health_hypothesis_family"]["observed_kill_budget_proxy"]
            ["death_anchor"]["event_index"])
        for row in targets
    }
    if (
        len(events) != PINNED_EVENT_COUNT
        or actual != expected
        or {event.source_event_index for event in events if event.source_type == "DEAD"} != lethal_indices
        or len({event.source_event_index for event in events}) != len(events)
        or any(event.actor_guid == focal_guid for event in events)
        or any(event.schedule_index != index for index, event in enumerate(events))
        or any(event.time_ms < 0 or event.damage <= 0 or event.spell_id <= 0 for event in events)
        or tuple(events) != tuple(sorted(events, key=lambda row: (row.time_ms, row.source_event_index)))
    ):
        raise ValueError("source actor event prefix does not close the fixed wave")


def export_actor_schedule_v1(path: Path) -> dict[str, Any]:
    """Write only the compact derived event prefix, never the raw CSV."""

    events = source_actor_damage_events_v1()
    source, targets, focal_guid = _source_binding_v1()
    payload = {
        "schema": DERIVED_SCHEMA,
        "source_wave_ref": source["source_identity"]["wave_id"],
        "source_wave_capsule_sha256": source["capsule_sha256"],
        "target_guids": [row["target_guid"] for row in targets],
        "excluded_focal_guid": focal_guid,
        "events": [asdict(event) for event in events],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"path": str(path), "event_count": len(events), "bytes": path.stat().st_size}


def _load_derived_actor_events_v1(path: Path) -> tuple[SourceActorDamageEventV1, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    source, targets, focal_guid = _source_binding_v1()
    if (
        payload.get("schema") != DERIVED_SCHEMA
        or payload.get("source_wave_ref") != source["source_identity"]["wave_id"]
        or payload.get("source_wave_capsule_sha256") != source["capsule_sha256"]
        or payload.get("target_guids") != [row["target_guid"] for row in targets]
        or payload.get("excluded_focal_guid") != focal_guid
        or not isinstance(payload.get("events"), list)
    ):
        raise ValueError("derived actor schedule differs from the fixed source wave")
    names = {field.name for field in fields(SourceActorDamageEventV1)}
    if any(not isinstance(row, dict) or set(row) != names for row in payload["events"]):
        raise ValueError("derived actor event fields are incomplete")
    events = tuple(SourceActorDamageEventV1(**row) for row in payload["events"])
    _validate_actor_events_v1(events)
    return events


@lru_cache(maxsize=4)
def source_actor_damage_events_v1(
    derived_json: Path | None = None,
) -> tuple[SourceActorDamageEventV1, ...]:
    """Read only this wave's historical positive direct damage through death."""

    if derived_json is not None:
        return _load_derived_actor_events_v1(derived_json)
    source, targets, focal_guid = _source_binding_v1()
    cutoff = {
        row["target_guid"]: int(
            row["max_health_hypothesis_family"]["observed_kill_budget_proxy"]
            ["death_anchor"]["event_index"]
        )
        for row in targets
    }
    target_index = {row["target_guid"]: index for index, row in enumerate(targets)}
    raw_file = targets[0]["max_health_hypothesis_family"]["observed_kill_budget_proxy"][
        "death_anchor"
    ]["raw_file"]
    path = PROJECT_ROOT / "offline_data" / raw_file
    rows: list[dict[str, Any]] = []
    classifications: dict[str, set[str]] = defaultdict(set)
    combatant_info: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["Encounter"] == source["source_identity"]["encounter_id"]:
                if row["Type"] == "CLASS":
                    classifications[row["Target GUID"]].add(row["Outcome / Detail"])
                elif row["Type"] == "INFO":
                    combatant_info.add(row["Target GUID"])
            target_guid = row["Target GUID"]
            if (
                row["Encounter"] != source["source_identity"]["encounter_id"]
                or target_guid not in cutoff
                or row["Type"] not in {"DMG", "DEAD"}
                or int(row["Event Index"]) > cutoff[target_guid]
                or row["Source GUID"] == focal_guid
            ):
                continue
            damage = float(row["Value"].replace(",", ""))
            if damage <= 0:
                continue
            if (
                row["Synthetic"] != "false"
                or not row["Source GUID"]
                or not row["Spell ID"]
                or not row["Action / Ability"]
            ):
                raise ValueError("actor damage row lacks observed identity or skill")
            rows.append({
                "source_event_index": int(row["Event Index"]),
                "time_ms": int(row["Offset (ms)"]),
                "target_index": target_index[target_guid],
                "actor_guid": row["Source GUID"],
                "spell_id": int(row["Spell ID"]),
                "ability": row["Action / Ability"],
                "damage": damage,
                "source_type": row["Type"],
            })
    for row in rows:
        guid = row["actor_guid"]
        labels = classifications[guid]
        if labels == {"Friendly Player"} and guid in combatant_info:
            row["actor_kind"] = "PLAYER"
            row["owner_guid_suffix"] = None
        elif len(labels) == 1 and next(iter(labels)).startswith("Hostile Object owner="):
            row["actor_kind"] = "OBJECT"
            row["owner_guid_suffix"] = next(iter(labels)).split("owner=", 1)[1]
        elif len(labels) == 1 and next(iter(labels)).startswith("Friendly Creature owner="):
            row["actor_kind"] = "CREATURE"
            row["owner_guid_suffix"] = next(iter(labels)).split("owner=", 1)[1]
        else:
            raise ValueError(f"source actor {guid} has no unique CLASS/INFO identity")
    rows.sort(key=lambda row: (row["time_ms"], row["source_event_index"]))
    events = tuple(
        SourceActorDamageEventV1(schedule_index=index, **row)
        for index, row in enumerate(rows)
    )
    _validate_actor_events_v1(events)
    return events


def build_actor_schedule_wave_case_v1(
    seed: int, *, derived_json: Path | None = None,
) -> tuple[DevelopmentWaveCaseV1, dict[str, Any], tuple[SourceActorDamageEventV1, ...]]:
    """Replace the aggregate rate schedule with source actor event prefixes."""

    original, scenario, _ = build_retarget_wave_case_v1(seed)
    events = source_actor_damage_events_v1(derived_json)
    budgets = [row["modeled_team_damage_budget"] for row in original.case_spec["team_background"]["per_target"]]
    observed = [
        sum(event.damage for event in events if event.target_index == index)
        for index in range(len(budgets))
    ]
    if observed != budgets:
        raise ValueError("source actor event totals differ from focal-excluded kill budgets")
    spec = deepcopy(original.case_spec)
    spec["schema"] = SCHEMA
    team = spec["team_background"]
    team.pop("per_target")
    team.pop("source_schedule_content_sha256")
    team["model"] = "OBSERVED_PER_ACTOR_DAMAGE_PREFIX_WITH_NATIVE_ALIVE_RETARGET"
    team["source_wave_capsule_sha256"] = spec["source_evidence"]["wave_capsule_sha256"]
    team["source_event_count"] = len(events)
    team["source_actor_count"] = len({event.actor_guid for event in events})
    actor_kinds = {event.actor_guid: event.actor_kind for event in events}
    team["source_actor_kind_counts"] = dict(sorted(Counter(actor_kinds.values()).items()))
    team["actor_guid_and_spell_id_preserved"] = True
    team["source_damage_by_target"] = observed
    team["source_event_types"] = sorted({event.source_type for event in events})
    team["positive_damage_events_only"] = True
    team["source_lethal_dead_rows_included"] = True
    team["source_damage_after_first_target_death_excluded"] = True
    team["post_source_death_rate_extrapolated"] = False
    team["pet_owner_attribution_known"] = False
    team["actor_skill_timing_reactive"] = False
    team["actor_damage_magnitude_reactive"] = False
    team["target_death_retarget_response"] = True
    team["future_schedule_policy_visible"] = False
    case = DevelopmentWaveCaseV1(spec, original.request, original.dynamic_load, original.target_contexts)
    scenario = deepcopy(scenario)
    scenario["scenario_id"] += "__source_actor_prefix"
    scenario["scenario_model"]["limitation_codes"] = [
        code for code in scenario["scenario_model"]["limitation_codes"]
        if code not in {
            "TEAM_RATE_FROM_DIRECT_GUID_LEAVE_ONE_OUT_DEATH_ANCHOR",
            "POST_SOURCE_DEATH_TEAM_RATE_EXTRAPOLATED",
        }
    ]
    scenario["scenario_model"]["limitation_codes"].append(
        "TEAM_ACTOR_TIMING_AND_DAMAGE_FIXED_TO_OBSERVED_PREFIX"
    )
    scenario["scenario_model"]["limitation_codes"].append(
        "SOURCE_ACTOR_PREFIX_OMITS_NON_DAMAGE_ACTIONS"
    )
    return case, scenario, events


def run_actor_schedule_wave_panel_v1(
    seed: int, *, bridge_path: Path = V14_BRIDGE,
    derived_json: Path | None = None,
    bridge_cwd: Path | None = None,
    runtime_binding_path: Path | None = None,
) -> dict[str, Any]:
    """Run Cat and the same candidate with source actor IDs and v14 retarget."""

    from .development_two_wave_build_panel_v1 import _two_lane_registry
    from .development_wave_panel_v1 import run_development_wave_panel_v1

    def registry_factory(bridge: SourceFittedTeamRetargetBridgeV1,
                         candidate: Any, path: Path) -> CatGapThreeBaselineRegistryV1:
        registry = _two_lane_registry(bridge, candidate, path)
        executors = {}
        for policy_id, execute in registry.executors.items():
            contract = "source" if policy_id == CAT_POLICY_ID else "duration"

            def run(*, group: Any, scenario: Any, policy: Any,
                    _execute: Any = execute, _contract: str = contract) -> Any:
                bridge.wait_contract = _contract
                return _execute(group=group, scenario=scenario, policy=policy)

            executors[policy_id] = run
        return CatGapThreeBaselineRegistryV1(
            executors=executors,
            artifact_validators=registry.artifact_validators,
            contract=registry.contract,
        )

    case, scenario, events = build_actor_schedule_wave_case_v1(seed, derived_json=derived_json)
    identity = case.case_spec["team_background"]["source_wave_capsule_sha256"]
    panel = run_development_wave_panel_v1(
        master_seed=seed,
        bridge_path=bridge_path,
        **({"bridge_cwd": bridge_cwd} if bridge_cwd is not None else {}),
        **({"runtime_binding_path": runtime_binding_path} if runtime_binding_path is not None else {}),
        case_override=case,
        scenario_override=scenario,
        candidate_kind="anchor_13d",
        baseline_ids=(CAT_POLICY_ID,),
        registry_factory=registry_factory,
        bridge_factory=lambda bridge: SourceFittedTeamRetargetBridgeV1(bridge, events, identity),
        native_bridge_type=V14ProjectedDynamicV3Bridge,
    )
    panel["team_retarget_comparison_eligible"] = all(
        row["status"] == "COMPLETED" for row in panel["rows"]
    )
    panel["team_retarget_comparison_gate"] = (
        "MATCHED_NATIVE_SOURCE_ACTOR_PREFIX_DEVELOPMENT_ONLY"
        if panel["team_retarget_comparison_eligible"]
        else "SOURCE_ACTOR_PREFIX_OR_LANE_INCOMPLETE"
    )
    return panel


def reduce_actor_terminal_rows_v1(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize the tiny retained terminal rows, not full rollout panels."""

    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    counts = {"CAT": Counter(), "CANDIDATE": Counter()}
    for row in rows:
        seed, lane = row["seed"], row["lane"]
        if lane not in counts or lane in by_seed.setdefault(seed, {}):
            raise ValueError("duplicate or unknown actor terminal lane")
        by_seed[seed][lane] = row
        counts[lane][row["status"]] += 1
    if any(set(lanes) != set(counts) for lanes in by_seed.values()):
        raise ValueError("actor terminal rows lack a paired lane")
    paired = [
        lanes for lanes in by_seed.values()
        if all(lanes[lane]["status"] == "COMPLETED" for lane in counts)
    ]
    deltas = [
        lanes["CANDIDATE"]["own_effective_damage"]
        - lanes["CAT"]["own_effective_damage"]
        for lanes in paired
    ]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in deltas):
        raise ValueError("completed actor terminal row lacks finite effective damage")
    average = mean(deltas) if deltas else None
    se = stdev(deltas) / math.sqrt(len(deltas)) if len(deltas) >= 2 else None
    return {
        "seed_start": min(by_seed) if by_seed else None,
        "seed_count": len(by_seed),
        "lane_status_counts": {lane: dict(sorted(values.items())) for lane, values in counts.items()},
        "paired_complete_count": len(deltas),
        "paired_candidate_minus_cat_mean": average,
        "paired_candidate_minus_cat_se": se,
        "paired_candidate_minus_cat_normal_approx_95_lower": (
            average - 1.96 * se if se is not None else None
        ),
        "paired_candidate_minus_cat_wins": sum(value > 0 for value in deltas),
        "paired_candidate_minus_cat_ties": sum(value == 0 for value in deltas),
        "paired_candidate_minus_cat_losses": sum(value < 0 for value in deltas),
        "censored": [
            {"seed": seed, "lanes": {
                lane: {"status": row["status"], "reason": row["censor_reason"]}
                for lane, row in lanes.items()
            }}
            for seed, lanes in sorted(by_seed.items())
            if not all(lanes[lane]["status"] == "COMPLETED" for lane in counts)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--bridge", type=Path, default=V14_BRIDGE)
    parser.add_argument("--actor-schedule-json", type=Path)
    parser.add_argument("--bridge-cwd", type=Path)
    parser.add_argument("--runtime-binding", type=Path)
    parser.add_argument("--export-derived-json", type=Path)
    args = parser.parse_args()
    if args.export_derived_json is not None:
        print(json.dumps(export_actor_schedule_v1(args.export_derived_json), sort_keys=True))
        return
    panel = run_actor_schedule_wave_panel_v1(
        args.seed, bridge_path=args.bridge, derived_json=args.actor_schedule_json,
        bridge_cwd=args.bridge_cwd, runtime_binding_path=args.runtime_binding,
    )
    print(json.dumps({
        "schema": SCHEMA + "/smoke_summary",
        "seed": args.seed,
        "status": panel["status"],
        "comparison_eligible": panel["team_retarget_comparison_eligible"],
        "source_actor_count": panel["case"]["team_background"]["source_actor_count"],
        "source_event_count": panel["case"]["team_background"]["source_event_count"],
        "rows": [{
            "policy_id": row["policy_id"], "status": row["status"],
            "own_effective_damage": row.get("own_effective_damage"),
            "ttk_ms": row.get("ttk_ms"),
            "team_events_retargeted": row.get("team_response_evidence", {}).get(
                "events_retargeted_after_endogenous_death"
            ),
            "retargeted_by_actor": row.get("team_response_evidence", {}).get(
                "retargeted_by_actor"
            ),
        } for row in panel["rows"]],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
