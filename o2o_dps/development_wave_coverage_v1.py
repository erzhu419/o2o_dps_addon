"""Compact source-wave inventory for the local old50 Upper Kara capsule.

Source kill budgets, armor branches, and attackability windows remain model
inputs or hypotheses, never exact NPC HP/armor/positions.  This inventory does
not execute the simulator or promote any unimplemented wave to a ready panel.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .development_wave_case_v1 import (
    INSTANCE_ID, SOURCE_CAPSULE_BUNDLE_SHA256, SOURCE_CAPSULE_SHA256, WAVE_ID,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / (
    "offline_data/derived/fury_offline_scenario_capsules/v2/"
    "fury_offline_scenario_capsules_v2."
    "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
)
SCHEMA = "development_wave_source_coverage/v1"


def _positive(value: Any) -> bool:
    return type(value) in (int, float) and value > 0


def _complete_proxy(target: Mapping[str, Any]) -> float | None:
    proxy = target.get("max_health_hypothesis_family", {}).get("observed_kill_budget_proxy", {})
    if (
        proxy.get("status") == "OBSERVED"
        and proxy.get("completeness") == "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS"
        and _positive(proxy.get("value"))
        and proxy.get("unparsed_damage_event_count") == 0
        and isinstance(proxy.get("death_anchor"), Mapping)
    ):
        return float(proxy["value"])
    return None


def _model_source_blockers(scenario: Mapping[str, Any]) -> list[str]:
    blockers = []
    identity = scenario.get("source_identity", {})
    if not identity.get("instance_id") or not identity.get("wave_id"):
        blockers.append("MISSING_SOURCE_WAVE_IDENTITY")
    if not _positive(scenario.get("horizon", {}).get("milliseconds")):
        blockers.append("MISSING_POSITIVE_HOSTILE_ACTIVITY_SPAN")
    targets = scenario.get("targets", [])
    if not targets or len({target.get("target_guid") for target in targets}) != len(targets):
        blockers.append("MISSING_DISTINCT_TARGET_IDENTITY")
    if any(_complete_proxy(target) is None for target in targets):
        blockers.append("MISSING_COMPLETE_PER_TARGET_KILL_BUDGET_PROXY")
    if not any(
        _positive(branch.get("base_armor"))
        and branch.get("status") == "SENSITIVITY_HYPOTHESIS"
        for branch in scenario.get("base_armor_hypothesis_family", [])
    ):
        blockers.append("MISSING_BASE_ARMOR_SENSITIVITY_BRANCH")
    if any(
        not any(branch.get("branch_id") == "full_wave" for branch in target.get("attackable_window_hypothesis_family", []))
        for target in targets
    ):
        blockers.append("MISSING_ATTACKABILITY_SENSITIVITY_BRANCH")
    return blockers


def _candidate_record(scenario: Mapping[str, Any], stratum: str) -> dict[str, Any]:
    targets = scenario["targets"]
    return {
        "stratum": stratum,
        "instance_id": scenario["source_identity"]["instance_id"],
        "wave_id": scenario["source_identity"]["wave_id"],
        "scenario_id": scenario["scenario_id"],
        "target_count": len(targets),
        "hostile_activity_span_ms": scenario["horizon"]["milliseconds"],
        "per_target_observed_kill_budget_proxies": [
            {"target_guid": target["target_guid"], "damage_to_first_death": _complete_proxy(target)}
            for target in targets
        ],
        "status": "SOURCE_FIELDS_PRESENT_CASE_NOT_IMPLEMENTED_OR_RUN",
        "hp_semantics": "OBSERVED_DAMAGE_TO_FIRST_DEATH_NOT_EXACT_MAX_HP",
        "armor_semantics": "CATALOG_SENSITIVITY_BRANCH_NOT_OBSERVED_ARMOR",
        "team_semantics": "PLAYER_CONDITIONED_BACKGROUND_MISSING",
    }


def _choose_stratified(
    scenarios: Sequence[Mapping[str, Any]], *, supported_wave_id: str,
) -> list[dict[str, Any]]:
    pool = [
        scenario for scenario in scenarios
        if scenario["source_identity"]["wave_id"] != supported_wave_id
        and not _model_source_blockers(scenario)
    ]
    selected = []
    used_waves = set()
    used_instances = set()

    def choose(label: str, group: list[Mapping[str, Any]], fraction: float) -> None:
        if not group:
            return
        ordered = sorted(group, key=lambda row: (row["horizon"]["milliseconds"], row["source_identity"]["wave_id"]))
        pivot = ordered[round((len(ordered) - 1) * fraction)]["horizon"]["milliseconds"]
        choices = sorted(
            ordered,
            key=lambda row: (
                row["source_identity"]["instance_id"] in used_instances,
                abs(row["horizon"]["milliseconds"] - pivot),
                row["source_identity"]["wave_id"],
            ),
        )
        scenario = next((row for row in choices if row["source_identity"]["wave_id"] not in used_waves), None)
        if scenario is None:
            return
        used_waves.add(scenario["source_identity"]["wave_id"])
        used_instances.add(scenario["source_identity"]["instance_id"])
        selected.append(_candidate_record(scenario, label))

    singles = [row for row in pool if len(row["targets"]) == 1]
    for label, fraction in (
        ("single_duration_q05", 0.05), ("single_duration_q20", 0.20),
        ("single_duration_q40", 0.40), ("single_duration_q60", 0.60),
        ("single_duration_q80", 0.80), ("single_duration_q95", 0.95),
    ):
        choose(label, singles, fraction)
    multis = [row for row in pool if len(row["targets"]) > 1]
    for label, lower, upper in (
        ("multi_2_targets", 2, 2), ("multi_3_targets", 3, 3),
        ("multi_4_targets", 4, 4), ("multi_5_6_targets", 5, 6),
        ("multi_7_9_targets", 7, 9), ("multi_10plus_targets", 10, 1000),
    ):
        choose(label, [row for row in multis if lower <= len(row["targets"]) <= upper], 0.5)
    return selected


def build_development_wave_coverage_v1(
    manifest: Mapping[str, Any], *, manifest_reference: str,
) -> dict[str, Any]:
    scenarios = manifest.get("scenarios", [])
    if manifest.get("bucket") != "main_comparison" or len(scenarios) != 1197:
        raise ValueError("expected the old50 main-comparison 1197-wave capsule")
    wave_ids = [row["source_identity"]["wave_id"] for row in scenarios]
    if len(set(wave_ids)) != len(wave_ids):
        raise ValueError("source wave IDs are not unique")
    supported = [
        row for row in scenarios
        if row["source_identity"]["instance_id"] == INSTANCE_ID
        and row["source_identity"]["wave_id"] == WAVE_ID
    ]
    if len(supported) != 1:
        raise ValueError("the implemented one-wave case is absent or ambiguous")
    source_blockers = {row["source_identity"]["wave_id"]: _model_source_blockers(row) for row in scenarios}
    ready = sum(not blockers for blockers in source_blockers.values())
    supported_row = supported[0]
    candidate_rows = _choose_stratified(scenarios, supported_wave_id=WAVE_ID)
    exact_hp = sum(all(
        target["max_health_hypothesis_family"]["exact_max_health"].get("status") != "MISSING"
        for target in row["targets"]
    ) for row in scenarios)
    exact_armor = sum(all(
        target["armor"]["base_armor_exact"].get("status") != "MISSING"
        for target in row["targets"]
    ) for row in scenarios)
    exact_attackability = sum(all(
        target["attackability_cause_exact"].get("status") != "MISSING"
        for target in row["targets"]
    ) for row in scenarios)
    conditioned_team = sum(all(
        target["background_team_damage_model"].get("status") != "MISSING_REQUIRES_PLAYER_CONDITIONING"
        for target in row["targets"]
    ) for row in scenarios)
    return {
        "schema": SCHEMA,
        "manifest_reference": manifest_reference,
        "scope": "OLD50_UPPER_KARA_SOURCE_CAPSULE_MODEL_DESIGN_ONLY",
        "source_instance_count": len({row["source_identity"]["instance_id"] for row in scenarios}),
        "source_wave_count": len(scenarios),
        "source_target_count": sum(len(row["targets"]) for row in scenarios),
        "source_strata": dict(Counter(row["runner_projection"]["stratum"] for row in scenarios)),
        "source_field_ready_for_model_design_count": ready,
        "source_field_incomplete_count": len(scenarios) - ready,
        "source_field_blocker_counts": dict(Counter(code for codes in source_blockers.values() for code in codes)),
        "exact_max_hp_identified_wave_count": exact_hp,
        "exact_base_armor_identified_wave_count": exact_armor,
        "exact_attackability_cause_identified_wave_count": exact_attackability,
        "player_conditioned_team_model_wave_count": conditioned_team,
        "implemented_case_count": 1,
        "other_source_waves_not_configured_or_run_in_this_lane": len(scenarios) - 1,
        "ready_source_waves_without_implemented_case": ready - int(not source_blockers[WAVE_ID]),
        "implemented_case": {
            "instance_id": INSTANCE_ID,
            "wave_id": WAVE_ID,
            "manifest_capsule_sha256": supported_row["capsule_sha256"],
            "capsule_binding_matches_code": supported_row["capsule_sha256"] == SOURCE_CAPSULE_SHA256,
            "manifest_bundle_sha256": manifest["content_address"]["sha256"],
            "bundle_binding_matches_code": manifest["content_address"]["sha256"] == SOURCE_CAPSULE_BUNDLE_SHA256,
            "source_field_blockers": source_blockers[WAVE_ID],
            "status": "ONE_MODEL_DEFINED_WAVE_CASE_AVAILABLE",
            "historical_exact": False,
        },
        "twelve_source_field_ready_candidates": candidate_rows,
        "candidate_count": len(candidate_rows),
        "candidate_selection_status": (
            "TWELVE_STRATIFIED_SOURCE_CANDIDATES_NOT_EXECUTABLE_YET"
            if len(candidate_rows) == 12 else "INSUFFICIENT_SOURCE_FIELDS_FOR_TWELVE_CANDIDATES"
        ),
        "cross_wave_blockers": [
            "PLAYER_CONDITIONED_TEAM_BACKGROUND_NOT_IN_CAPSULE",
            "EXACT_MAX_HP_BASE_ARMOR_ATTACKABILITY_NOT_IDENTIFIED",
            "PER_WAVE_SCENARIO_AND_MULTI_TARGET_POLICY_CONTRACT_NOT_IMPLEMENTED",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.manifest, "rt", encoding="utf-8") as source:
        manifest = json.load(source)
    result = build_development_wave_coverage_v1(manifest, manifest_reference=str(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: result[key] for key in (
            "source_wave_count", "source_field_ready_for_model_design_count",
            "source_field_incomplete_count", "implemented_case_count", "candidate_count",
            "candidate_selection_status",
        )
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
