"""Held-out singleton health sensitivity for the Fury candidate.

This evaluator is deliberately separate from the duration-mode held-out gate.
It reads only completed compact Chronicle reconstruction ``.json.gz`` files and
the manifest's repeated-entry median/IQR kill-budget aggregate.  Those values
are INFERRED sensitivity budgets, not exact NPC health.  Only waves containing
one reconstructed hostile target are eligible, so wowsims' global health-fight
counter is equivalent to the one simulated target for this bounded purpose.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import random
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping, Sequence

from .chronicle_encounter_batch_v1 import load_json_document
from .fury_encounter_scenarios_v1 import (
    HEALTH_STAT_INDEX,
    compile_fury_encounter_scenarios,
)
from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_expert_closed_loop import (
    ClosedLoopBridgeLike,
    SINGLETON_HEALTH_COMPLETION_MODE,
    run_fury_expert_closed_loop,
)
from .fury_heldout_corpus_gate_v1 import (
    BASELINE_IDS,
    extract_candidate_training_instances,
    load_candidate_parameters,
)
from .fury_policy_optimization_v1 import (
    DEFAULT_BRIDGE,
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    _seed_tuple,
    _write_json,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_encounter_batch"
    / "v1"
    / "manifest.json"
)
DEFAULT_CANDIDATE = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_level_conditioned_robust_v1.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_singleton_health_sensitivity_v1.json"
)
HEALTH_VARIANTS = ("q1", "median", "q3")


class FurySingletonHealthError(RuntimeError):
    """The compact join, singleton request, or matched evaluation is invalid."""


@dataclass(frozen=True)
class HealthProfile:
    creature_entry_id: int
    target_name: str | None
    sample_count: int
    source_file_count: int
    q1: float
    median: float
    q3: float

    def variants(self) -> tuple[tuple[str, float], ...]:
        return (("q1", self.q1), ("median", self.median), ("q3", self.q3))


@dataclass(frozen=True)
class CompletedFeature:
    instance_id: str
    source_name: str
    relative_path: str
    path: Path


@dataclass(frozen=True)
class HealthFeatureSnapshot:
    manifest_path: Path
    manifest_updated_at: str | None
    output_directory: Path
    status_counts: Mapping[str, int]
    completed_features: tuple[CompletedFeature, ...]
    health_profiles: Mapping[int, HealthProfile]
    base_request_path: Path
    require_complete: bool

    @property
    def completed_instance_ids(self) -> tuple[str, ...]:
        return tuple(sorted({row.instance_id for row in self.completed_features}))


@dataclass(frozen=True)
class HealthInstanceSplit:
    split_seed: int
    candidate_training_instances: tuple[str, ...]
    heldout_instances: tuple[str, ...]
    non_heldout_completed_instances: tuple[str, ...]


@dataclass(frozen=True)
class SingletonHealthFamily:
    instance_id: str
    feature_relative_path: str
    encounter_id: str
    wave_id: str
    observed_duration_ms: int
    duration_stratum: str
    creature_entry_id: int
    target_name: str | None
    health_profile: HealthProfile
    source_wave: Mapping[str, Any]
    sampling_weight: float = 1.0

    @property
    def family_id(self) -> str:
        return (
            f"{self.feature_relative_path}::{self.encounter_id}::{self.wave_id}"
        )


@dataclass(frozen=True)
class SingletonHealthCorpus:
    families: tuple[SingletonHealthFamily, ...]
    instance_summaries: tuple[Mapping[str, Any], ...]
    completed_feature_gzip_read_count: int


def _instance_id(source_name: str) -> str:
    result = source_name.split("__", 1)[0].strip()
    if not result:
        raise FurySingletonHealthError(
            f"could not derive instance id from {source_name!r}"
        )
    return result


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FurySingletonHealthError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise FurySingletonHealthError(f"{label} must be positive and finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FurySingletonHealthError(f"{label} must be a positive integer")
    return value


def _resolve_output_path(directory: Path, raw_path: str, label: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = directory / path
    path = path.resolve()
    if path.suffix.casefold() != ".gz":
        raise FurySingletonHealthError(f"{label} is not gzip JSON: {path}")
    if not path.is_file():
        raise FurySingletonHealthError(f"{label} is missing: {path}")
    return path


def _load_health_profiles(manifest: Mapping[str, Any]) -> dict[int, HealthProfile]:
    aggregate = manifest.get("npc_identity_aggregates")
    items = aggregate.get("items") if isinstance(aggregate, Mapping) else None
    if not isinstance(items, list):
        raise FurySingletonHealthError(
            "manifest lacks npc_identity_aggregates.items"
        )
    profiles: dict[int, HealthProfile] = {}
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise FurySingletonHealthError(
                f"health aggregate item {index} must be an object"
            )
        identity = raw.get("identity")
        scenario = raw.get("health_scenario")
        summary = raw.get("kill_budget_proxy_summary")
        if not all(isinstance(value, Mapping) for value in (identity, scenario, summary)):
            raise FurySingletonHealthError(
                f"health aggregate item {index} lacks identity/scenario/summary"
            )
        assert isinstance(identity, Mapping)
        assert isinstance(scenario, Mapping)
        assert isinstance(summary, Mapping)
        entry_id = identity.get("creature_entry_id")
        if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0:
            continue
        if scenario.get("status") != "INFERRED":
            continue
        sample_count = _positive_int(summary.get("count"), "health sample count")
        if sample_count < 2:
            raise FurySingletonHealthError(
                f"entry {entry_id} INFERRED health has fewer than two samples"
            )
        range_value = scenario.get("range")
        if not isinstance(range_value, list) or len(range_value) != 2:
            raise FurySingletonHealthError(
                f"entry {entry_id} health scenario lacks an IQR"
            )
        q1 = _positive_number(range_value[0], f"entry {entry_id} q1")
        median = _positive_number(scenario.get("center"), f"entry {entry_id} median")
        q3 = _positive_number(range_value[1], f"entry {entry_id} q3")
        if q1 > median or median > q3:
            raise FurySingletonHealthError(
                f"entry {entry_id} health IQR does not contain its median"
            )
        source_file_count = _positive_int(
            summary.get("source_file_count"),
            f"entry {entry_id} source_file_count",
        )
        if entry_id in profiles:
            raise FurySingletonHealthError(
                f"duplicate repeated-entry health aggregate for {entry_id}"
            )
        target_name = identity.get("target_name")
        profiles[entry_id] = HealthProfile(
            creature_entry_id=entry_id,
            target_name=target_name if isinstance(target_name, str) else None,
            sample_count=sample_count,
            source_file_count=source_file_count,
            q1=q1,
            median=median,
            q3=q3,
        )
    if not profiles:
        raise FurySingletonHealthError(
            "manifest has no same-entry INFERRED median/IQR health profiles"
        )
    return profiles


def load_health_feature_snapshot(
    manifest_path: Path,
    *,
    require_complete: bool = False,
) -> HealthFeatureSnapshot:
    """Resolve only COMPLETED compact feature reports and the global aggregate."""

    resolved = manifest_path.expanduser().resolve()
    manifest = load_json_document(resolved)
    if manifest.get("kind") != "chronicle_encounter_batch_manifest_v1":
        raise FurySingletonHealthError("invalid Chronicle batch manifest kind")
    output = manifest.get("output")
    entries = manifest.get("entries")
    contract = manifest.get("artifact_contract")
    if not isinstance(output, Mapping) or not isinstance(entries, list):
        raise FurySingletonHealthError("manifest lacks output metadata or entries")
    directory_value = output.get("directory")
    if not isinstance(directory_value, str) or not directory_value.strip():
        raise FurySingletonHealthError("manifest output directory is missing")
    output_directory = Path(directory_value).expanduser()
    if not output_directory.is_absolute():
        output_directory = resolved.parent / output_directory
    output_directory = output_directory.resolve()

    base_contract = contract.get("base_request") if isinstance(contract, Mapping) else None
    base_value = base_contract.get("path") if isinstance(base_contract, Mapping) else None
    if not isinstance(base_value, str) or not base_value.strip():
        raise FurySingletonHealthError("manifest artifact contract lacks base_request.path")
    base_request_path = Path(base_value).expanduser()
    if not base_request_path.is_absolute():
        base_request_path = resolved.parent / base_request_path
    base_request_path = base_request_path.resolve()
    if not base_request_path.is_file():
        raise FurySingletonHealthError(
            f"manifest base request is missing: {base_request_path}"
        )

    statuses: Counter[str] = Counter()
    completed: list[CompletedFeature] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise FurySingletonHealthError("manifest entry must be an object")
        status = str(entry.get("processing_status") or "MISSING")
        statuses[status] += 1
        if status != "COMPLETED":
            continue
        source = entry.get("source")
        outputs = entry.get("outputs")
        if not isinstance(source, Mapping) or not isinstance(outputs, Mapping):
            raise FurySingletonHealthError("completed entry lacks source or outputs")
        source_name = source.get("name")
        feature = outputs.get("feature_report")
        if not isinstance(source_name, str) or not isinstance(feature, Mapping):
            raise FurySingletonHealthError(
                "completed entry lacks source name or feature_report"
            )
        relative = feature.get("path")
        if not isinstance(relative, str) or not relative.strip():
            raise FurySingletonHealthError(
                "completed entry lacks compact feature-report path"
            )
        completed.append(
            CompletedFeature(
                instance_id=_instance_id(source_name),
                source_name=source_name,
                relative_path=relative.replace("\\", "/"),
                path=_resolve_output_path(
                    output_directory,
                    relative,
                    "completed feature report",
                ),
            )
        )
    noncomplete = sum(
        count for status, count in statuses.items() if status != "COMPLETED"
    )
    if require_complete and noncomplete:
        rendered = ", ".join(f"{key}={statuses[key]}" for key in sorted(statuses))
        raise FurySingletonHealthError(
            f"require-complete rejected an incomplete manifest: {rendered}"
        )
    if not completed:
        raise FurySingletonHealthError("manifest has no completed compact feature gzip")
    completed.sort(key=lambda row: (row.instance_id, row.source_name, row.relative_path))
    updated = manifest.get("updated_at")
    return HealthFeatureSnapshot(
        manifest_path=resolved,
        manifest_updated_at=str(updated) if updated is not None else None,
        output_directory=output_directory,
        status_counts=dict(sorted(statuses.items())),
        completed_features=tuple(completed),
        health_profiles=_load_health_profiles(manifest),
        base_request_path=base_request_path,
        require_complete=require_complete,
    )


def split_health_instances(
    snapshot: HealthFeatureSnapshot,
    *,
    split_seed: int,
    candidate_training_instances: Iterable[str] = (),
    heldout_fraction: float = 0.2,
    heldout_count: int | None = None,
) -> HealthInstanceSplit:
    """Create an explicit RNG-seeded whole-instance split."""

    if isinstance(split_seed, bool) or not isinstance(split_seed, int):
        raise TypeError("split_seed must be an integer")
    if not 0 < float(heldout_fraction) <= 1:
        raise ValueError("heldout_fraction must be in (0, 1]")
    completed = set(snapshot.completed_instance_ids)
    forced = {
        str(value).strip()
        for value in candidate_training_instances
        if str(value).strip()
    }
    eligible = sorted(completed.difference(forced))
    if not eligible:
        raise FurySingletonHealthError(
            "no completed instance remains after candidate-training exclusions"
        )
    if heldout_count is None:
        count = max(1, int(len(eligible) * float(heldout_fraction) + 0.5))
    else:
        if isinstance(heldout_count, bool) or not isinstance(heldout_count, int):
            raise TypeError("heldout_count must be an integer")
        if heldout_count <= 0:
            raise ValueError("heldout_count must be positive")
        count = heldout_count
    if count > len(eligible):
        raise FurySingletonHealthError(
            f"heldout_count={count} exceeds {len(eligible)} eligible instances"
        )
    if not forced and count == len(eligible):
        if len(eligible) == 1:
            raise FurySingletonHealthError(
                "one completed instance cannot form an instance-disjoint split"
            )
        count -= 1
    shuffled = list(eligible)
    random.Random(split_seed).shuffle(shuffled)
    heldout = set(shuffled[:count])
    return HealthInstanceSplit(
        split_seed=split_seed,
        candidate_training_instances=tuple(sorted(forced)),
        heldout_instances=tuple(sorted(heldout)),
        non_heldout_completed_instances=tuple(sorted(completed.difference(heldout))),
    )


def _duration_stratum(duration_ms: int) -> str:
    if duration_ms <= 10_000:
        return "short_le_10s"
    if duration_ms <= 30_000:
        return "medium_10_30s"
    return "long_gt_30s"


def _reservoir_add(
    bucket: list[SingletonHealthFamily],
    value: SingletonHealthFamily,
    *,
    seen_count: int,
    capacity: int,
    rng: random.Random,
) -> None:
    if len(bucket) < capacity:
        bucket.append(value)
        return
    slot = rng.randrange(seen_count)
    if slot < capacity:
        bucket[slot] = value


def _choose_duration_and_identity_diverse(
    reservoirs: Mapping[str, list[SingletonHealthFamily]],
    *,
    limit: int,
    rng: random.Random,
) -> list[SingletonHealthFamily]:
    pools = {key: list(values) for key, values in reservoirs.items() if values}
    for values in pools.values():
        rng.shuffle(values)
    chosen: list[SingletonHealthFamily] = []
    strata = sorted(pools)
    rng.shuffle(strata)
    for stratum in strata:
        if len(chosen) >= limit:
            break
        chosen.append(pools[stratum].pop())
    while len(chosen) < limit:
        available = [
            (stratum, value)
            for stratum, values in pools.items()
            for value in values
        ]
        if not available:
            break
        seen_entries = {value.creature_entry_id for value in chosen}
        unseen = [
            item for item in available if item[1].creature_entry_id not in seen_entries
        ]
        stratum, value = rng.choice(unseen or available)
        pools[stratum].remove(value)
        chosen.append(value)
    return chosen


def _singleton_family(
    *,
    feature: CompletedFeature,
    encounter: Mapping[str, Any],
    wave: Mapping[str, Any],
    profiles: Mapping[int, HealthProfile],
) -> SingletonHealthFamily | None:
    target_count = wave.get("target_count")
    targets = wave.get("targets")
    if target_count != 1 or not isinstance(targets, list) or len(targets) != 1:
        return None
    target = targets[0]
    if not isinstance(target, Mapping):
        raise FurySingletonHealthError("singleton wave target must be an object")
    entry_id = target.get("creature_entry_id")
    if isinstance(entry_id, bool) or not isinstance(entry_id, int):
        return None
    profile = profiles.get(entry_id)
    if profile is None:
        return None
    duration_ms = wave.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise FurySingletonHealthError("singleton wave duration_ms must be an integer")
    if duration_ms <= 0:
        return None
    encounter_id = str(encounter.get("encounter") or "").strip()
    wave_id = str(wave.get("wave_id") or "").strip()
    if not encounter_id or not wave_id:
        raise FurySingletonHealthError("singleton wave lacks encounter or wave id")
    target_name = target.get("target_name")
    return SingletonHealthFamily(
        instance_id=feature.instance_id,
        feature_relative_path=feature.relative_path,
        encounter_id=encounter_id,
        wave_id=wave_id,
        observed_duration_ms=duration_ms,
        duration_stratum=_duration_stratum(duration_ms),
        creature_entry_id=entry_id,
        target_name=target_name if isinstance(target_name, str) else None,
        health_profile=profile,
        source_wave=copy.deepcopy(dict(wave)),
    )


def select_singleton_health_families(
    snapshot: HealthFeatureSnapshot,
    split: HealthInstanceSplit,
    *,
    selection_seed: int,
    max_families_per_instance: int = 8,
) -> SingletonHealthCorpus:
    """Stream held-out feature gzip files into bounded duration/identity reservoirs."""

    if isinstance(max_families_per_instance, bool) or not isinstance(
        max_families_per_instance, int
    ):
        raise TypeError("max_families_per_instance must be an integer")
    if max_families_per_instance <= 0:
        raise ValueError("max_families_per_instance must be positive")
    heldout = set(split.heldout_instances)
    by_instance: dict[str, list[CompletedFeature]] = defaultdict(list)
    for feature in snapshot.completed_features:
        if feature.instance_id in heldout:
            by_instance[feature.instance_id].append(feature)

    selected_all: list[SingletonHealthFamily] = []
    summaries: list[Mapping[str, Any]] = []
    master_rng = random.Random(selection_seed)
    features_read = 0
    for instance_id in sorted(heldout):
        instance_rng = random.Random(master_rng.randrange(0, 2**63))
        reservoirs: dict[str, list[SingletonHealthFamily]] = defaultdict(list)
        populations: Counter[str] = Counter()
        eligible_entry_counts: Counter[int] = Counter()
        seen_family_ids: set[str] = set()
        instance_features_read = 0
        for feature in sorted(
            by_instance.get(instance_id, []), key=lambda value: value.relative_path
        ):
            report = load_json_document(feature.path)
            features_read += 1
            instance_features_read += 1
            if report.get("kind") != "chronicle_encounter_reconstruction_v1":
                raise FurySingletonHealthError(
                    f"invalid compact reconstruction kind: {feature.path}"
                )
            encounters = report.get("encounters")
            if not isinstance(encounters, list):
                raise FurySingletonHealthError(
                    f"compact reconstruction lacks encounters: {feature.path}"
                )
            for encounter in encounters:
                if not isinstance(encounter, Mapping):
                    raise FurySingletonHealthError("reconstructed encounter must be an object")
                waves = encounter.get("waves")
                if not isinstance(waves, list):
                    raise FurySingletonHealthError("reconstructed encounter lacks waves")
                for wave in waves:
                    if not isinstance(wave, Mapping):
                        raise FurySingletonHealthError("reconstructed wave must be an object")
                    family = _singleton_family(
                        feature=feature,
                        encounter=encounter,
                        wave=wave,
                        profiles=snapshot.health_profiles,
                    )
                    if family is None:
                        continue
                    if family.family_id in seen_family_ids:
                        raise FurySingletonHealthError(
                            f"duplicate singleton family: {family.family_id}"
                        )
                    seen_family_ids.add(family.family_id)
                    stratum = family.duration_stratum
                    populations[stratum] += 1
                    eligible_entry_counts[family.creature_entry_id] += 1
                    _reservoir_add(
                        reservoirs[stratum],
                        family,
                        seen_count=populations[stratum],
                        capacity=max_families_per_instance * 2,
                        rng=instance_rng,
                    )
        chosen = _choose_duration_and_identity_diverse(
            reservoirs,
            limit=max_families_per_instance,
            rng=instance_rng,
        )
        selected_counts = Counter(value.duration_stratum for value in chosen)
        weighted = [
            replace(
                value,
                sampling_weight=(
                    populations[value.duration_stratum]
                    / selected_counts[value.duration_stratum]
                ),
            )
            for value in chosen
        ]
        weighted.sort(key=lambda value: value.family_id)
        selected_all.extend(weighted)
        summaries.append(
            {
                "instance_id": instance_id,
                "completed_feature_gzip_read_count": instance_features_read,
                "eligible_joined_singleton_family_count": sum(populations.values()),
                "eligible_distinct_creature_entry_count": len(eligible_entry_counts),
                "eligible_by_duration_stratum": {
                    key: populations[key] for key in sorted(populations)
                },
                "selected_family_count": len(weighted),
                "selected_distinct_creature_entry_count": len(
                    {value.creature_entry_id for value in weighted}
                ),
                "selected_by_duration_stratum": {
                    key: selected_counts[key] for key in sorted(selected_counts)
                },
            }
        )
    if not selected_all:
        raise FurySingletonHealthError(
            "held-out compact features contain no joinable actual singleton wave"
        )
    selected_all.sort(key=lambda value: (value.instance_id, value.family_id))
    return SingletonHealthCorpus(
        families=tuple(selected_all),
        instance_summaries=tuple(summaries),
        completed_feature_gzip_read_count=features_read,
    )


def _duration_request_for_family(
    family: SingletonHealthFamily,
    base_request: Mapping[str, Any],
    *,
    armor_hypothesis: int | float,
    level_hypothesis: int,
) -> JSONMap:
    report = {
        "kind": "chronicle_encounter_reconstruction_v1",
        "encounters": [
            {
                "instance": family.instance_id,
                "encounter": family.encounter_id,
                "waves": [copy.deepcopy(dict(family.source_wave))],
            }
        ],
    }
    catalog = compile_fury_encounter_scenarios(
        report,
        base_request,
        armor_hypotheses=(armor_hypothesis,),
        level_hypotheses=(level_hypothesis,),
        base_request_label="manifest artifact_contract.base_request",
    )
    rows = catalog.get("scenarios")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise FurySingletonHealthError(
            f"actual singleton wave did not compile to exactly one request: {family.family_id}"
        )
    row = rows[0]
    pile = row.get("pile")
    request = row.get("request")
    if not isinstance(pile, Mapping) or not isinstance(request, Mapping):
        raise FurySingletonHealthError("compiled singleton scenario is incomplete")
    if pile.get("layout_variant") != "single_target" or pile.get("target_count") != 1:
        raise FurySingletonHealthError(
            "health evaluator refused a separated or multi-target scenario"
        )
    return copy.deepcopy(dict(request))


def _request_with_inferred_health(
    duration_request: Mapping[str, Any],
    health: float,
) -> JSONMap:
    request = copy.deepcopy(dict(duration_request))
    encounter = request.get("encounter")
    if not isinstance(encounter, dict):
        raise FurySingletonHealthError("compiled request lacks mutable encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or len(targets) != 1:
        raise FurySingletonHealthError("health request must have exactly one target")
    target = targets[0]
    if not isinstance(target, dict):
        raise FurySingletonHealthError("health request target must be mutable")
    stats = target.get("stats")
    if not isinstance(stats, list) or len(stats) <= HEALTH_STAT_INDEX:
        raise FurySingletonHealthError("health request target lacks stats[34]")
    stats[HEALTH_STAT_INDEX] = health
    encounter["useHealth"] = True
    encounter["durationVariation"] = 0.0
    return request


class _HealthScopeAccumulator:
    def __init__(
        self,
        expert_ids: Sequence[str],
        candidate_id: str,
        *,
        expected_rollout_count_per_expert: int,
    ) -> None:
        self.expert_ids = tuple(expert_ids)
        self.candidate_id = candidate_id
        self.expected_rollout_count_per_expert = expected_rollout_count_per_expert
        self.metrics: dict[str, JSONMap] = {
            expert_id: {
                "weighted_damage": 0.0,
                "weighted_elapsed_seconds": 0.0,
                "weight_sum": 0.0,
                "dps_sum": 0.0,
                "elapsed_seconds_sum": 0.0,
                "rollout_count": 0,
                "omitted_lane_count": 0,
                "unfaithful_rollout_count": 0,
                "incomplete_health_horizon_count": 0,
                "nonfaithful_reason_counts": Counter(),
                "termination_reason_counts": Counter(),
            }
            for expert_id in self.expert_ids
        }
        self.pairs: dict[str, JSONMap] = {
            baseline_id: {
                "dps_deltas": [],
                "ttk_wins": 0,
                "ttk_ties": 0,
                "ttk_losses": 0,
                "complete_pair_count": 0,
            }
            for baseline_id in BASELINE_IDS
        }

    def add(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        weight: float,
    ) -> None:
        for expert_id in self.expert_ids:
            row = rows.get(expert_id)
            if not isinstance(row, Mapping):
                raise FurySingletonHealthError(
                    f"matched health row is missing expert {expert_id}"
                )
            metric = self.metrics[expert_id]
            elapsed_seconds = float(row["elapsed_ms"]) / 1000.0
            metric["weighted_damage"] += weight * float(row["damage_delta"])
            metric["weighted_elapsed_seconds"] += weight * elapsed_seconds
            metric["weight_sum"] += weight
            metric["dps_sum"] += float(row["dps"])
            metric["elapsed_seconds_sum"] += elapsed_seconds
            metric["rollout_count"] += 1
            metric["omitted_lane_count"] += int(row["omitted_lane_count"])
            metric["unfaithful_rollout_count"] += int(
                not bool(row["all_lane_projections_faithful"])
            )
            metric["incomplete_health_horizon_count"] += int(
                not bool(row["health_completed_within_watchdog"])
            )
            reasons = row.get("nonfaithful_reason_counts")
            if not isinstance(reasons, Mapping):
                raise FurySingletonHealthError(
                    "health rollout lacks nonfaithful reason counts"
                )
            metric["nonfaithful_reason_counts"].update(
                {str(key): int(value) for key, value in reasons.items()}
            )
            metric["termination_reason_counts"].update(
                [str(row.get("termination_reason") or "missing")]
            )

        candidate = rows[self.candidate_id]
        for baseline_id in BASELINE_IDS:
            baseline = rows[baseline_id]
            if not bool(candidate["health_completed_within_watchdog"]) or not bool(
                baseline["health_completed_within_watchdog"]
            ):
                continue
            pair = self.pairs[baseline_id]
            candidate_elapsed = int(candidate["elapsed_ms"])
            baseline_elapsed = int(baseline["elapsed_ms"])
            pair["complete_pair_count"] += 1
            pair["dps_deltas"].append(
                float(candidate["dps"]) - float(baseline["dps"])
            )
            if candidate_elapsed < baseline_elapsed:
                pair["ttk_wins"] += 1
            elif candidate_elapsed > baseline_elapsed:
                pair["ttk_losses"] += 1
            else:
                pair["ttk_ties"] += 1

    def finish(self) -> JSONMap:
        ranking: list[JSONMap] = []
        by_id: dict[str, JSONMap] = {}
        for expert_id in self.expert_ids:
            metric = self.metrics[expert_id]
            elapsed = float(metric["weighted_elapsed_seconds"])
            weight_sum = float(metric["weight_sum"])
            count = int(metric["rollout_count"])
            if count <= 0 or elapsed <= 0 or weight_sum <= 0:
                raise FurySingletonHealthError("empty health evaluation scope")
            row = {
                "expert_id": expert_id,
                "rollout_count": count,
                "expected_rollout_count": self.expected_rollout_count_per_expert,
                "weighted_mean_dps": float(metric["weighted_damage"]) / elapsed,
                "weighted_mean_ttk_seconds": elapsed / weight_sum,
                "unweighted_mean_dps": float(metric["dps_sum"]) / count,
                "unweighted_mean_ttk_seconds": (
                    float(metric["elapsed_seconds_sum"]) / count
                ),
                "omitted_lane_count": int(metric["omitted_lane_count"]),
                "unfaithful_rollout_count": int(
                    metric["unfaithful_rollout_count"]
                ),
                "incomplete_health_horizon_count": int(
                    metric["incomplete_health_horizon_count"]
                ),
                "nonfaithful_reason_counts": dict(
                    sorted(metric["nonfaithful_reason_counts"].items())
                ),
                "termination_reason_counts": dict(
                    sorted(metric["termination_reason_counts"].items())
                ),
            }
            ranking.append(row)
            by_id[expert_id] = row
        ranking.sort(
            key=lambda row: (float(row["weighted_mean_ttk_seconds"]), str(row["expert_id"]))
        )

        comparisons: dict[str, JSONMap] = {}
        candidate_dps = float(by_id[self.candidate_id]["weighted_mean_dps"])
        for baseline_id in BASELINE_IDS:
            pair = self.pairs[baseline_id]
            deltas = pair["dps_deltas"]
            comparisons[baseline_id] = {
                "primary_metric": "paired_ttk",
                "complete_pair_count": int(pair["complete_pair_count"]),
                "ttk_wins": int(pair["ttk_wins"]),
                "ttk_ties": int(pair["ttk_ties"]),
                "ttk_losses": int(pair["ttk_losses"]),
                "more_paired_ttk_wins_than_losses": int(pair["ttk_wins"])
                > int(pair["ttk_losses"]),
                "dps_is_secondary_overkill_sensitive_diagnostic": True,
                "weighted_mean_dps_margin": (
                    candidate_dps - float(by_id[baseline_id]["weighted_mean_dps"])
                ),
                "mean_paired_dps_margin": fmean(deltas) if deltas else None,
                "higher_weighted_dps": candidate_dps
                > float(by_id[baseline_id]["weighted_mean_dps"]),
            }

        all_expected = all(
            int(by_id[expert_id]["rollout_count"])
            == self.expected_rollout_count_per_expert
            for expert_id in self.expert_ids
        )
        zero_omissions = {
            expert_id: (
                int(by_id[expert_id]["omitted_lane_count"]) == 0
                and int(by_id[expert_id]["unfaithful_rollout_count"]) == 0
            )
            for expert_id in self.expert_ids
        }
        complete = {
            expert_id: int(
                by_id[expert_id]["incomplete_health_horizon_count"]
            )
            == 0
            for expert_id in self.expert_ids
        }
        gate = (
            all_expected
            and all(zero_omissions.values())
            and all(complete.values())
            and all(
                row["more_paired_ttk_wins_than_losses"]
                for row in comparisons.values()
            )
        )
        return {
            "primary_comparison_metric": "paired_ttk",
            "dps_role": "secondary_overkill_sensitive_diagnostic",
            "ranking": ranking,
            "comparisons_vs_each_baseline": comparisons,
            "all_expected_rollouts_present": all_expected,
            "zero_omissions_and_faithful_by_expert": zero_omissions,
            "complete_health_horizons_by_expert": complete,
            "all_experts_zero_omissions_and_faithful": all(
                zero_omissions.values()
            ),
            "all_experts_all_health_horizons_complete": all(complete.values()),
            "gate_passed": gate,
        }


def evaluate_singleton_health_sensitivity(
    bridge: ClosedLoopBridgeLike,
    corpus: SingletonHealthCorpus,
    base_request: Mapping[str, Any],
    *,
    candidate_parameters: FuryPolicyParameters,
    armor_hypothesis: int | float,
    level_hypothesis: int,
    validation_seeds: Iterable[int],
    watchdog_cap_ms: int,
    max_decisions: int = 200_000,
    fallback_wait_ms: int = 100,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> JSONMap:
    """Run matched q1/median/q3 health fights without retaining rollout rows."""

    seeds = _seed_tuple(validation_seeds, "validation_seeds")
    _positive_int(watchdog_cap_ms, "watchdog_cap_ms")
    _positive_int(max_decisions, "max_decisions")
    _positive_int(fallback_wait_ms, "fallback_wait_ms")
    candidate = FuryTunedPolicyAdapter(candidate_parameters)
    adapters = (
        CatFurySourceAdapter(),
        ContraDeployedSourceAdapter(),
        candidate,
    )
    expert_ids = tuple(adapter.expert_id for adapter in adapters)
    per_variant_rows = len(corpus.families) * len(seeds)
    overall = _HealthScopeAccumulator(
        expert_ids,
        candidate.expert_id,
        expected_rollout_count_per_expert=per_variant_rows * len(HEALTH_VARIANTS),
    )
    variants = {
        variant: _HealthScopeAccumulator(
            expert_ids,
            candidate.expert_id,
            expected_rollout_count_per_expert=per_variant_rows,
        )
        for variant in HEALTH_VARIANTS
    }
    completed_rollouts = 0
    total_rollouts = (
        len(corpus.families) * len(HEALTH_VARIANTS) * len(seeds) * len(adapters)
    )
    for family_ordinal, family in enumerate(corpus.families, start=1):
        duration_request = _duration_request_for_family(
            family,
            base_request,
            armor_hypothesis=armor_hypothesis,
            level_hypothesis=level_hypothesis,
        )
        for variant, health in family.health_profile.variants():
            request = _request_with_inferred_health(duration_request, health)
            for seed in seeds:
                rows: dict[str, Mapping[str, Any]] = {}
                for adapter in adapters:
                    rollout = run_fury_expert_closed_loop(
                        bridge,
                        request,
                        adapter,
                        seed=seed,
                        horizon_ms=watchdog_cap_ms,
                        fallback_wait_ms=fallback_wait_ms,
                        max_decisions=max_decisions,
                        clamp_encounter_to_horizon=False,
                        completion_mode=SINGLETON_HEALTH_COMPLETION_MODE,
                        retain_steps=False,
                    )
                    if bool(rollout.get("source_execution")) or bool(
                        rollout.get("exact_lua_replay")
                    ):
                        raise FurySingletonHealthError(
                            "health evaluator received an invalid exact-source claim"
                        )
                    if rollout.get("completion_mode") != SINGLETON_HEALTH_COMPLETION_MODE:
                        raise FurySingletonHealthError(
                            "health evaluator received the wrong completion mode"
                        )
                    root_time = int(rollout["root_time_ms"])
                    final_time = int(rollout["final_time_ms"])
                    completed = (
                        bool(rollout.get("finished"))
                        and bool(rollout.get("completion_criterion_met"))
                        and bool(rollout.get("health_depleted"))
                        and final_time <= root_time + watchdog_cap_ms
                    )
                    rows[adapter.expert_id] = {
                        "damage_delta": float(rollout["damage_delta"]),
                        "elapsed_ms": int(rollout["elapsed_ms"]),
                        "dps": float(rollout["dps"]),
                        "health_completed_within_watchdog": completed,
                        "termination_reason": str(
                            rollout.get("termination_reason") or "missing"
                        ),
                        "all_lane_projections_faithful": bool(
                            rollout["all_lane_projections_faithful"]
                        ),
                        "omitted_lane_count": int(rollout["omitted_lane_count"]),
                        "nonfaithful_reason_counts": dict(
                            rollout["nonfaithful_reason_counts"]
                        ),
                    }
                    completed_rollouts += 1
                overall.add(rows, weight=family.sampling_weight)
                variants[variant].add(rows, weight=family.sampling_weight)
        if progress is not None:
            progress(
                {
                    "family_ordinal": family_ordinal,
                    "family_count": len(corpus.families),
                    "instance_id": family.instance_id,
                    "wave_id": family.wave_id,
                    "creature_entry_id": family.creature_entry_id,
                    "completed_rollouts": completed_rollouts,
                    "total_rollouts": total_rollouts,
                }
            )
    overall_result = overall.finish()
    variant_results = {
        variant: variants[variant].finish() for variant in HEALTH_VARIANTS
    }
    gate = bool(overall_result["gate_passed"]) and all(
        bool(row["gate_passed"]) for row in variant_results.values()
    )
    return {
        "status": "COMPLETED",
        "validation_seeds": list(seeds),
        "matched_seed_contract": True,
        "candidate_expert_id": candidate.expert_id,
        "health_variants": list(HEALTH_VARIANTS),
        "watchdog_cap_ms": watchdog_cap_ms,
        "max_decisions": max_decisions,
        "retained_rollout_rows": False,
        "expected_rollout_count": total_rollouts,
        "rollout_count": completed_rollouts,
        "overall": overall_result,
        "health_variant_scopes": variant_results,
        "target_count_stratum": "1",
        "health_sensitivity_gate_passed": gate,
    }


def _compact_family(family: SingletonHealthFamily) -> JSONMap:
    profile = family.health_profile
    return {
        "instance_id": family.instance_id,
        "feature_report": family.feature_relative_path,
        "encounter_id": family.encounter_id,
        "wave_id": family.wave_id,
        "observed_duration_ms": family.observed_duration_ms,
        "duration_stratum": family.duration_stratum,
        "target_count": 1,
        "layout_variant": "single_target",
        "creature_entry_id": family.creature_entry_id,
        "target_name": family.target_name,
        "sampling_weight": family.sampling_weight,
        "health_scenario": {
            "status": "INFERRED",
            "q1": profile.q1,
            "median": profile.median,
            "q3": profile.q3,
            "repeated_kill_budget_sample_count": profile.sample_count,
            "source_file_count": profile.source_file_count,
            "not_equal_to": "exact NPC health distribution",
        },
    }


def build_artifact(
    *,
    snapshot: HealthFeatureSnapshot,
    split: HealthInstanceSplit,
    corpus: SingletonHealthCorpus,
    candidate_document: Mapping[str, Any],
    candidate_path: Path,
    candidate_parameters: FuryPolicyParameters,
    base_request_path: Path,
    armor_hypothesis: int | float,
    level_hypothesis: int,
    selection_seed: int,
    max_families_per_instance: int,
    evaluation: Mapping[str, Any],
) -> JSONMap:
    gate = bool(evaluation.get("health_sensitivity_gate_passed"))
    represented_instances = sorted({value.instance_id for value in corpus.families})
    return {
        "schema_version": 1,
        "kind": "fury_singleton_health_sensitivity_v1",
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "status": evaluation.get("status"),
        "source_execution": False,
        "exact_lua_replay": False,
        "deployment_allowed": False,
        "duration_heldout_gate_modified": False,
        "input_contract": {
            "manifest": str(snapshot.manifest_path),
            "manifest_updated_at": snapshot.manifest_updated_at,
            "manifest_status_counts": dict(snapshot.status_counts),
            "require_complete": snapshot.require_complete,
            "completed_feature_gzip_count": len(snapshot.completed_features),
            "completed_feature_gzip_read_count": (
                corpus.completed_feature_gzip_read_count
            ),
            "global_same_entry_health_profile_count": len(
                snapshot.health_profiles
            ),
            "scenario_catalog_gzip_read_count": 0,
            "raw_jsonl_row_count_read": 0,
            "base_request": str(base_request_path),
            "request_payloads_written": False,
            "rollout_rows_written": False,
        },
        "health_claim_boundary": {
            "status": "INFERRED",
            "source": "median and IQR of repeated complete Chronicle kill-budget proxies",
            "not_exact_npc_health": True,
            "target_count": 1,
            "actual_single_target_wave_required": True,
            "separated_singleton_from_multi_target_wave_allowed": False,
            "individual_multi_target_death_modeled": False,
            "interpretation": "singleton health sensitivity only",
            "primary_comparison_metric": "paired TTK",
            "dps_role": "secondary diagnostic because terminal overkill changes DPS",
        },
        "instance_split": {
            "unit": "whole Chronicle instance",
            "rng": "random.Random",
            "split_seed": split.split_seed,
            "candidate_training_instances": list(
                split.candidate_training_instances
            ),
            "heldout_instances": list(split.heldout_instances),
            "represented_heldout_instances": represented_instances,
            "non_heldout_completed_instances": list(
                split.non_heldout_completed_instances
            ),
            "instance_disjoint": not bool(
                set(split.candidate_training_instances).intersection(
                    represented_instances
                )
            ),
        },
        "selection": {
            "selection_seed": selection_seed,
            "max_families_per_instance": max_families_per_instance,
            "armor_hypothesis": armor_hypothesis,
            "level_hypothesis": level_hypothesis,
            "selected_family_count": len(corpus.families),
            "selected_health_request_branch_count": (
                len(corpus.families) * len(HEALTH_VARIANTS)
            ),
            "instance_summaries": list(corpus.instance_summaries),
            "families": [_compact_family(value) for value in corpus.families],
            "full_request_payloads_retained": False,
        },
        "candidate": {
            "path": str(candidate_path.expanduser().resolve()),
            "kind": candidate_document.get("kind"),
            "parameters": asdict(candidate_parameters),
        },
        "evaluation": dict(evaluation),
        "health_sensitivity_gate_passed": gate,
        "claim_boundary": (
            "Cat and Contra are source-derived adapters, not exact Lua execution; "
            "the q1/median/q3 budgets are INFERRED repeated kill-budget sensitivity "
            "values; this artifact neither validates multi-target death nor enables deployment"
        ),
    }


def _manifest_hypothesis(
    manifest: Mapping[str, Any],
    explicit: int | float | None,
    field: str,
) -> int | float:
    if explicit is not None:
        return explicit
    contract = manifest.get("artifact_contract")
    values = contract.get(field) if isinstance(contract, Mapping) else None
    if not isinstance(values, list) or len(values) != 1:
        raise FurySingletonHealthError(
            f"manifest does not provide exactly one {field}; pass it explicitly"
        )
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FurySingletonHealthError(f"manifest {field} value must be numeric")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--base-request", type=Path)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--exclude-instance", action="append", default=[])
    parser.add_argument("--split-seed", type=int, default=2026091601)
    split_group = parser.add_mutually_exclusive_group()
    split_group.add_argument("--heldout-fraction", type=float, default=0.2)
    split_group.add_argument("--heldout-count", type=int)
    parser.add_argument("--selection-seed", type=int, default=2026091602)
    parser.add_argument("--max-families-per-instance", type=int, default=8)
    parser.add_argument("--armor-hypothesis", type=float)
    parser.add_argument("--level-hypothesis", type=int)
    parser.add_argument("--validation-seed", type=int, action="append", dest="seeds")
    parser.add_argument("--watchdog-cap-ms", type=int, default=7_200_000)
    parser.add_argument("--max-decisions", type=int, default=200_000)
    parser.add_argument("--fallback-wait-ms", type=int, default=100)
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="write the compact split/selection plan without launching rollouts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve()
    manifest_document = load_json_document(manifest_path)
    snapshot = load_health_feature_snapshot(
        manifest_path,
        require_complete=args.require_complete,
    )
    candidate_path = args.candidate.expanduser().resolve()
    candidate_document = load_json_document(candidate_path)
    candidate_parameters = load_candidate_parameters(candidate_document)
    excluded = set(extract_candidate_training_instances(candidate_document))
    excluded.update(args.exclude_instance)
    split = split_health_instances(
        snapshot,
        split_seed=args.split_seed,
        candidate_training_instances=excluded,
        heldout_fraction=args.heldout_fraction,
        heldout_count=args.heldout_count,
    )
    corpus = select_singleton_health_families(
        snapshot,
        split,
        selection_seed=args.selection_seed,
        max_families_per_instance=args.max_families_per_instance,
    )
    armor = _manifest_hypothesis(
        manifest_document,
        args.armor_hypothesis,
        "armor_hypotheses",
    )
    level = int(
        _manifest_hypothesis(
            manifest_document,
            args.level_hypothesis,
            "level_hypotheses",
        )
    )
    base_request_path = (
        args.base_request.expanduser().resolve()
        if args.base_request is not None
        else snapshot.base_request_path
    )
    base_request = load_json_document(base_request_path)
    seeds = tuple(args.seeds or range(2026091601, 2026091617))
    if args.plan_only:
        evaluation: Mapping[str, Any] = {
            "status": "NOT_RUN",
            "validation_seeds": list(seeds),
            "matched_seed_contract": True,
            "retained_rollout_rows": False,
            "health_sensitivity_gate_passed": False,
        }
    else:
        with SimulatorBridge(args.bridge) as bridge:
            evaluation = evaluate_singleton_health_sensitivity(
                bridge,
                corpus,
                base_request,
                candidate_parameters=candidate_parameters,
                armor_hypothesis=armor,
                level_hypothesis=level,
                validation_seeds=seeds,
                watchdog_cap_ms=args.watchdog_cap_ms,
                max_decisions=args.max_decisions,
                fallback_wait_ms=args.fallback_wait_ms,
                progress=lambda value: print(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    flush=True,
                ),
            )
    artifact = build_artifact(
        snapshot=snapshot,
        split=split,
        corpus=corpus,
        candidate_document=candidate_document,
        candidate_path=candidate_path,
        candidate_parameters=candidate_parameters,
        base_request_path=base_request_path,
        armor_hypothesis=armor,
        level_hypothesis=level,
        selection_seed=args.selection_seed,
        max_families_per_instance=args.max_families_per_instance,
        evaluation=evaluation,
    )
    _write_json(args.output, artifact)
    print(
        json.dumps(
            {
                "status": evaluation.get("status"),
                "completed_manifest_instances": len(snapshot.completed_instance_ids),
                "health_profile_count": len(snapshot.health_profiles),
                "heldout_instances": len(split.heldout_instances),
                "represented_heldout_instances": len(
                    {value.instance_id for value in corpus.families}
                ),
                "selected_families": len(corpus.families),
                "health_sensitivity_gate_passed": artifact[
                    "health_sensitivity_gate_passed"
                ],
                "deployment_allowed": False,
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "CompletedFeature",
    "FurySingletonHealthError",
    "HealthFeatureSnapshot",
    "HealthInstanceSplit",
    "HealthProfile",
    "SingletonHealthCorpus",
    "SingletonHealthFamily",
    "build_artifact",
    "evaluate_singleton_health_sensitivity",
    "load_health_feature_snapshot",
    "main",
    "select_singleton_health_families",
    "split_health_instances",
)


if __name__ == "__main__":
    raise SystemExit(main())
