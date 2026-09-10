"""Disk-light, instance-disjoint held-out Fury evaluation over Chronicle catalogs.

The batch manifest is a snapshot boundary.  Only entries already marked
``COMPLETED`` are read, and only their compact gzip scenario catalogs are
opened.  A reproducible instance-level split prevents encounter families from
one raid upload appearing on both sides of the held-out boundary.

Selection is bounded per held-out instance and favors coverage across target
count and observed-duration strata.  Simulator requests exist only in memory
for the selected families.  Evaluation retains aggregate and paired summaries,
not rollout traces or a merged corpus of requests.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping, Sequence

from .chronicle_encounter_batch_v1 import load_json_document
from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_expert_closed_loop import ClosedLoopBridgeLike, run_fury_expert_closed_loop
from .fury_policy_optimization_v1 import (
    DEFAULT_BRIDGE,
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    PolicyScenario,
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
    / "fury_policy_heldout_corpus_gate_v1.json"
)
BASELINE_IDS = ("cat.fury.profile1", "contra.deployed.fury.raid_a")


class FuryHeldoutCorpusError(RuntimeError):
    """The held-out split, catalog selection, or matched evaluation is invalid."""


@dataclass(frozen=True)
class CompletedCatalog:
    instance_id: str
    source_name: str
    relative_path: str
    path: Path


@dataclass(frozen=True)
class ManifestSnapshot:
    manifest_path: Path
    manifest_updated_at: str | None
    output_directory: Path
    status_counts: Mapping[str, int]
    completed_catalogs: tuple[CompletedCatalog, ...]
    require_complete: bool

    @property
    def completed_instance_ids(self) -> tuple[str, ...]:
        return tuple(sorted({row.instance_id for row in self.completed_catalogs}))


@dataclass(frozen=True)
class InstanceSplit:
    split_seed: int
    candidate_training_instances: tuple[str, ...]
    heldout_instances: tuple[str, ...]
    non_heldout_completed_instances: tuple[str, ...]


@dataclass(frozen=True)
class SelectedFamily:
    scenario: PolicyScenario
    instance_id: str
    family_id: str
    catalog_relative_path: str
    target_count: int
    target_count_stratum: str
    duration_stratum: str


@dataclass(frozen=True)
class SelectedCorpus:
    families: tuple[SelectedFamily, ...]
    instance_summaries: tuple[Mapping[str, Any], ...]


def _instance_id(value: str) -> str:
    result = value.split("__", 1)[0].strip()
    if not result:
        raise FuryHeldoutCorpusError(f"could not derive instance id from {value!r}")
    return result


def load_manifest_snapshot(
    manifest_path: Path,
    *,
    require_complete: bool = False,
    document_loader: Callable[[Path], Mapping[str, Any]] | None = None,
) -> ManifestSnapshot:
    """Read one manifest snapshot and resolve only completed gzip catalogs."""

    resolved = manifest_path.expanduser().resolve()
    manifest = (
        load_json_document(resolved)
        if document_loader is None
        else document_loader(resolved)
    )
    if manifest.get("kind") != "chronicle_encounter_batch_manifest_v1":
        raise FuryHeldoutCorpusError("invalid Chronicle encounter batch manifest kind")
    entries = manifest.get("entries")
    output = manifest.get("output")
    if not isinstance(entries, list) or not isinstance(output, Mapping):
        raise FuryHeldoutCorpusError("batch manifest lacks entries or output metadata")
    directory_value = output.get("directory")
    if not isinstance(directory_value, str) or not directory_value.strip():
        raise FuryHeldoutCorpusError("batch manifest output directory is missing")
    output_directory = Path(directory_value).expanduser()
    if not output_directory.is_absolute():
        output_directory = (resolved.parent / output_directory).resolve()
    else:
        output_directory = output_directory.resolve()

    statuses: Counter[str] = Counter()
    completed: list[CompletedCatalog] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise FuryHeldoutCorpusError("batch manifest entry must be an object")
        status = str(entry.get("processing_status") or "MISSING")
        statuses[status] += 1
        if status != "COMPLETED":
            continue
        source = entry.get("source")
        outputs = entry.get("outputs")
        if not isinstance(source, Mapping) or not isinstance(outputs, Mapping):
            raise FuryHeldoutCorpusError("completed entry lacks source or outputs")
        source_name = source.get("name")
        catalog_output = outputs.get("scenario_catalog")
        if not isinstance(source_name, str) or not isinstance(catalog_output, Mapping):
            raise FuryHeldoutCorpusError("completed entry lacks catalog lineage")
        relative = catalog_output.get("path")
        if not isinstance(relative, str) or not relative.strip():
            raise FuryHeldoutCorpusError("completed entry lacks scenario catalog path")
        catalog_path = Path(relative)
        if not catalog_path.is_absolute():
            catalog_path = output_directory / catalog_path
        catalog_path = catalog_path.resolve()
        if catalog_path.suffix.casefold() != ".gz":
            raise FuryHeldoutCorpusError(
                f"completed scenario catalog is not gzip JSON: {catalog_path}"
            )
        if not catalog_path.is_file():
            raise FuryHeldoutCorpusError(
                f"completed scenario catalog is missing: {catalog_path}"
            )
        completed.append(
            CompletedCatalog(
                instance_id=_instance_id(source_name),
                source_name=source_name,
                relative_path=relative.replace("\\", "/"),
                path=catalog_path,
            )
        )

    noncomplete = sum(count for status, count in statuses.items() if status != "COMPLETED")
    if require_complete and noncomplete:
        rendered = ", ".join(f"{key}={statuses[key]}" for key in sorted(statuses))
        raise FuryHeldoutCorpusError(
            f"require-complete rejected a growing/incomplete manifest: {rendered}"
        )
    if not completed:
        raise FuryHeldoutCorpusError("manifest snapshot has no completed gzip catalogs")
    completed.sort(key=lambda row: (row.instance_id, row.source_name, row.relative_path))
    updated_at = manifest.get("updated_at")
    return ManifestSnapshot(
        manifest_path=resolved,
        manifest_updated_at=str(updated_at) if updated_at is not None else None,
        output_directory=output_directory,
        status_counts=dict(sorted(statuses.items())),
        completed_catalogs=tuple(completed),
        require_complete=require_complete,
    )


def split_completed_instances(
    snapshot: ManifestSnapshot,
    *,
    split_seed: int,
    candidate_training_instances: Iterable[str] = (),
    heldout_fraction: float = 0.2,
    heldout_count: int | None = None,
) -> InstanceSplit:
    """Create a reproducible split whose unit is a whole Chronicle instance."""

    if isinstance(split_seed, bool) or not isinstance(split_seed, int):
        raise TypeError("split_seed must be an integer")
    if not 0 < float(heldout_fraction) <= 1:
        raise ValueError("heldout_fraction must be in (0, 1]")
    completed = set(snapshot.completed_instance_ids)
    forced = {str(value).strip() for value in candidate_training_instances if str(value).strip()}
    eligible = sorted(completed.difference(forced))
    if not eligible:
        raise FuryHeldoutCorpusError(
            "no completed instance remains after excluding candidate-training instances"
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
        raise FuryHeldoutCorpusError(
            f"heldout_count={count} exceeds {len(eligible)} eligible completed instances"
        )
    # With no known candidate-training instance, preserve at least one completed
    # instance outside held-out so the output can truthfully call itself a split.
    if not forced and count == len(eligible):
        if len(eligible) == 1:
            raise FuryHeldoutCorpusError(
                "one completed instance cannot form an instance-disjoint split"
            )
        count -= 1

    shuffled = list(eligible)
    random.Random(split_seed).shuffle(shuffled)
    heldout = set(shuffled[:count])
    non_heldout = completed.difference(heldout)
    if heldout.intersection(forced):
        raise AssertionError("candidate-training instance leaked into held-out")
    return InstanceSplit(
        split_seed=split_seed,
        candidate_training_instances=tuple(sorted(forced)),
        heldout_instances=tuple(sorted(heldout)),
        non_heldout_completed_instances=tuple(sorted(non_heldout)),
    )


def _target_count_stratum(target_count: int) -> str:
    if target_count == 1:
        return "1"
    if target_count == 2:
        return "2"
    if target_count <= 4:
        return "3-4"
    return "5+"


def _duration_stratum(duration_ms: int) -> str:
    if duration_ms <= 10_000:
        return "short_le_10s"
    if duration_ms <= 30_000:
        return "medium_10_30s"
    return "long_gt_30s"


def _matching_family(
    row: Mapping[str, Any],
    *,
    instance_id: str,
    catalog_relative_path: str,
    armor_hypothesis: int | float,
    level_hypothesis: int,
) -> SelectedFamily | None:
    pile = row.get("pile")
    hypotheses = row.get("target_hypotheses")
    duration = row.get("duration")
    request = row.get("request")
    if not all(isinstance(value, Mapping) for value in (pile, hypotheses, duration, request)):
        raise FuryHeldoutCorpusError("catalog scenario lacks pile/hypotheses/duration/request")
    assert isinstance(pile, Mapping)
    assert isinstance(hypotheses, Mapping)
    assert isinstance(duration, Mapping)
    assert isinstance(request, Mapping)
    if str(pile.get("layout_side")) != "evidence_bounded":
        return None
    armor = hypotheses.get("armor")
    level = hypotheses.get("level")
    if not isinstance(armor, Mapping) or not isinstance(level, Mapping):
        raise FuryHeldoutCorpusError("catalog scenario has incomplete target hypotheses")
    if float(armor.get("value")) != float(armor_hypothesis):
        return None
    if int(level.get("value")) != int(level_hypothesis):
        return None
    observed_span = duration.get("observed_span_ms")
    if isinstance(observed_span, bool) or not isinstance(observed_span, int):
        raise FuryHeldoutCorpusError("catalog observed_span_ms must be an integer")
    if observed_span <= 0:
        return None
    target_count = pile.get("target_count")
    if isinstance(target_count, bool) or not isinstance(target_count, int):
        encounter = request.get("encounter")
        targets = encounter.get("targets") if isinstance(encounter, Mapping) else None
        if not isinstance(targets, list):
            raise FuryHeldoutCorpusError("catalog scenario lacks a target count")
        target_count = len(targets)
    if target_count <= 0:
        raise FuryHeldoutCorpusError("catalog target count must be positive")
    family_id = str(pile.get("sensitivity_family") or "").strip()
    scenario_id = str(row.get("scenario_id") or "").strip()
    if not family_id or not scenario_id:
        raise FuryHeldoutCorpusError("catalog scenario lacks family/scenario id")
    encounter = request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise FuryHeldoutCorpusError("catalog request lacks encounter")
    if encounter.get("useHealth") is True or encounter.get("use_health") is True:
        raise FuryHeldoutCorpusError("held-out scenarios must remain duration-mode requests")
    scenario = PolicyScenario(
        scenario_id=scenario_id,
        request=copy.deepcopy(dict(request)),
        horizon_ms=observed_span,
        weight=1.0,
        provenance={
            "catalog_kind": "fury_encounter_scenario_catalog_v1",
            "instance_id": instance_id,
            "family_id": family_id,
            "catalog": catalog_relative_path,
            "layout_side": "evidence_bounded",
            "target_hypotheses": copy.deepcopy(dict(hypotheses)),
        },
    )
    return SelectedFamily(
        scenario=scenario,
        instance_id=instance_id,
        family_id=family_id,
        catalog_relative_path=catalog_relative_path,
        target_count=target_count,
        target_count_stratum=_target_count_stratum(target_count),
        duration_stratum=_duration_stratum(observed_span),
    )


def _reservoir_add(
    reservoir: list[SelectedFamily],
    value: SelectedFamily,
    *,
    seen_count: int,
    capacity: int,
    rng: random.Random,
) -> None:
    if len(reservoir) < capacity:
        reservoir.append(value)
        return
    index = rng.randrange(seen_count)
    if index < capacity:
        reservoir[index] = value


def _choose_diverse(
    reservoirs: Mapping[tuple[str, str], Sequence[SelectedFamily]],
    *,
    limit: int,
    rng: random.Random,
) -> list[SelectedFamily]:
    pool = [value for values in reservoirs.values() for value in values]
    rng.shuffle(pool)
    chosen: list[SelectedFamily] = []
    seen_target: set[str] = set()
    seen_duration: set[str] = set()
    seen_cross: set[tuple[str, str]] = set()
    while pool and len(chosen) < limit:
        best_index = max(
            range(len(pool)),
            key=lambda index: (
                int(pool[index].target_count_stratum not in seen_target),
                int(pool[index].duration_stratum not in seen_duration),
                int(
                    (pool[index].target_count_stratum, pool[index].duration_stratum)
                    not in seen_cross
                ),
            ),
        )
        value = pool.pop(best_index)
        chosen.append(value)
        seen_target.add(value.target_count_stratum)
        seen_duration.add(value.duration_stratum)
        seen_cross.add((value.target_count_stratum, value.duration_stratum))
    return chosen


def select_heldout_families(
    snapshot: ManifestSnapshot,
    split: InstanceSplit,
    *,
    armor_hypothesis: int | float,
    level_hypothesis: int,
    selection_seed: int,
    max_families_per_instance: int = 8,
    document_loader: Callable[[Path], Mapping[str, Any]] | None = None,
) -> SelectedCorpus:
    """Select bounded evidence-bounded families without materializing the corpus."""

    if isinstance(max_families_per_instance, bool) or not isinstance(
        max_families_per_instance, int
    ):
        raise TypeError("max_families_per_instance must be an integer")
    if max_families_per_instance <= 0:
        raise ValueError("max_families_per_instance must be positive")
    heldout = set(split.heldout_instances)
    by_instance: dict[str, list[CompletedCatalog]] = defaultdict(list)
    for catalog in snapshot.completed_catalogs:
        if catalog.instance_id in heldout:
            by_instance[catalog.instance_id].append(catalog)
    if set(by_instance) != heldout:
        missing = sorted(heldout.difference(by_instance))
        raise FuryHeldoutCorpusError(f"held-out instances lack completed catalogs: {missing}")

    selected_all: list[SelectedFamily] = []
    summaries: list[Mapping[str, Any]] = []
    master_rng = random.Random(selection_seed)
    for instance_id in sorted(by_instance):
        instance_rng = random.Random(master_rng.randrange(0, 2**63))
        reservoirs: dict[tuple[str, str], list[SelectedFamily]] = defaultdict(list)
        populations: Counter[tuple[str, str]] = Counter()
        seen_families: set[str] = set()
        catalogs_read = 0
        for catalog_ref in sorted(
            by_instance[instance_id], key=lambda value: value.relative_path
        ):
            catalog = (
                load_json_document(catalog_ref.path)
                if document_loader is None
                else document_loader(catalog_ref.path)
            )
            catalogs_read += 1
            if catalog.get("kind") != "fury_encounter_scenario_catalog_v1":
                raise FuryHeldoutCorpusError(
                    f"invalid scenario catalog kind: {catalog_ref.path}"
                )
            rows = catalog.get("scenarios")
            if not isinstance(rows, list):
                raise FuryHeldoutCorpusError(
                    f"scenario catalog lacks scenarios: {catalog_ref.path}"
                )
            for row in rows:
                if not isinstance(row, Mapping):
                    raise FuryHeldoutCorpusError("catalog scenario must be an object")
                value = _matching_family(
                    row,
                    instance_id=instance_id,
                    catalog_relative_path=catalog_ref.relative_path,
                    armor_hypothesis=armor_hypothesis,
                    level_hypothesis=level_hypothesis,
                )
                if value is None:
                    continue
                family_key = f"{catalog_ref.relative_path}::{value.family_id}"
                if family_key in seen_families:
                    raise FuryHeldoutCorpusError(
                        "multiple evidence-bounded requests match one family/hypothesis: "
                        f"{family_key}"
                    )
                seen_families.add(family_key)
                stratum = (value.target_count_stratum, value.duration_stratum)
                populations[stratum] += 1
                _reservoir_add(
                    reservoirs[stratum],
                    value,
                    seen_count=populations[stratum],
                    capacity=max_families_per_instance,
                    rng=instance_rng,
                )

        chosen = _choose_diverse(
            reservoirs,
            limit=max_families_per_instance,
            rng=instance_rng,
        )
        selected_counts = Counter(
            (value.target_count_stratum, value.duration_stratum) for value in chosen
        )
        weighted: list[SelectedFamily] = []
        for value in chosen:
            stratum = (value.target_count_stratum, value.duration_stratum)
            sampling_weight = populations[stratum] / selected_counts[stratum]
            weighted.append(
                replace(
                    value,
                    scenario=replace(value.scenario, weight=float(sampling_weight)),
                )
            )
        weighted.sort(key=lambda value: value.scenario.scenario_id)
        selected_all.extend(weighted)
        represented = set(selected_counts)
        summaries.append(
            {
                "instance_id": instance_id,
                "completed_gzip_catalogs_read": catalogs_read,
                "eligible_evidence_bounded_family_count": sum(populations.values()),
                "selected_family_count": len(weighted),
                "eligible_by_cross_stratum": {
                    f"target_{key[0]}__{key[1]}": populations[key]
                    for key in sorted(populations)
                },
                "selected_by_cross_stratum": {
                    f"target_{key[0]}__{key[1]}": selected_counts[key]
                    for key in sorted(selected_counts)
                },
                "unrepresented_cross_strata": [
                    f"target_{key[0]}__{key[1]}"
                    for key in sorted(set(populations).difference(represented))
                ],
            }
        )
    if not selected_all:
        raise FuryHeldoutCorpusError(
            "held-out catalogs contain no evidence-bounded family for the requested hypothesis"
        )
    selected_all.sort(key=lambda value: (value.instance_id, value.scenario.scenario_id))
    return SelectedCorpus(tuple(selected_all), tuple(summaries))


def load_candidate_parameters(document: Mapping[str, Any]) -> FuryPolicyParameters:
    """Load one candidate from selected_parameters, a one-row grid, or parameters."""

    selected = document.get("selected_parameters")
    if isinstance(selected, Mapping):
        return FuryPolicyParameters(**dict(selected))
    parameters = document.get("parameters")
    if isinstance(parameters, Mapping):
        return FuryPolicyParameters(**dict(parameters))
    candidates = document.get("candidates")
    if isinstance(candidates, list):
        if len(candidates) != 1 or not isinstance(candidates[0], Mapping):
            raise FuryHeldoutCorpusError(
                "candidate grid must contain exactly one candidate for held-out evaluation"
            )
        return FuryPolicyParameters(**dict(candidates[0]))
    raise FuryHeldoutCorpusError(
        "candidate artifact lacks selected_parameters, parameters, or a one-row candidates list"
    )


def extract_candidate_training_instances(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Recover instance IDs named by scenario lineage in a candidate artifact."""

    result: set[str] = set()

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            if key in {"scenario_id", "scenario_ids"} and "__" in value:
                result.add(_instance_id(value))
            elif key == "instance" and value.strip():
                result.add(value.strip())

    visit(document)
    return tuple(sorted(result))


class _ScopeAccumulator:
    def __init__(self, expert_ids: Sequence[str], candidate_id: str) -> None:
        self.expert_ids = tuple(expert_ids)
        self.candidate_id = candidate_id
        self.metrics: dict[str, JSONMap] = {
            expert_id: {
                "weighted_damage": 0.0,
                "weighted_seconds": 0.0,
                "dps_sum": 0.0,
                "rollout_count": 0,
                "omitted_lane_count": 0,
                "incomplete_horizon_count": 0,
                "terminal_horizon_wait_cap_count": 0,
                "nonfaithful_reason_counts": Counter(),
            }
            for expert_id in self.expert_ids
        }
        self.pairs: dict[str, JSONMap] = {
            baseline_id: {
                "deltas": [],
                "wins": 0,
                "ties": 0,
                "losses": 0,
            }
            for baseline_id in BASELINE_IDS
        }

    def add(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        weight: float,
        horizon_ms: int,
    ) -> None:
        for expert_id in self.expert_ids:
            row = rows[expert_id]
            metric = self.metrics[expert_id]
            metric["weighted_damage"] += weight * float(row["damage_delta"])
            metric["weighted_seconds"] += weight * horizon_ms / 1000.0
            metric["dps_sum"] += float(row["dps"])
            metric["rollout_count"] += 1
            metric["omitted_lane_count"] += int(row["omitted_lane_count"])
            metric["incomplete_horizon_count"] += int(
                not bool(row["configured_horizon_complete"])
            )
            reasons = row.get("nonfaithful_reason_counts")
            if not isinstance(reasons, Mapping):
                raise FuryHeldoutCorpusError("rollout lacks nonfaithful reason counts")
            reason_counter = Counter(
                {str(reason): int(count) for reason, count in reasons.items()}
            )
            metric["nonfaithful_reason_counts"].update(reason_counter)
            metric["terminal_horizon_wait_cap_count"] += reason_counter.get(
                "gcd:wait_capped_to_remaining_horizon", 0
            )

        candidate_dps = float(rows[self.candidate_id]["dps"])
        for baseline_id in BASELINE_IDS:
            delta = candidate_dps - float(rows[baseline_id]["dps"])
            pair = self.pairs[baseline_id]
            pair["deltas"].append(delta)
            if delta > 1e-9:
                pair["wins"] += 1
            elif delta < -1e-9:
                pair["losses"] += 1
            else:
                pair["ties"] += 1

    def finish(self) -> JSONMap:
        ranking: list[JSONMap] = []
        by_id: dict[str, JSONMap] = {}
        for expert_id in self.expert_ids:
            metric = self.metrics[expert_id]
            seconds = float(metric["weighted_seconds"])
            count = int(metric["rollout_count"])
            if seconds <= 0 or count <= 0:
                raise FuryHeldoutCorpusError("empty evaluation scope")
            row = {
                "expert_id": expert_id,
                "rollout_count": count,
                "weighted_mean_dps": float(metric["weighted_damage"]) / seconds,
                "unweighted_mean_dps": float(metric["dps_sum"]) / count,
                "omitted_lane_count": int(metric["omitted_lane_count"]),
                "incomplete_horizon_count": int(metric["incomplete_horizon_count"]),
                "terminal_horizon_wait_cap_count": int(
                    metric["terminal_horizon_wait_cap_count"]
                ),
                "nonfaithful_reason_counts": dict(
                    sorted(metric["nonfaithful_reason_counts"].items())
                ),
            }
            ranking.append(row)
            by_id[expert_id] = row
        ranking.sort(key=lambda row: (-float(row["weighted_mean_dps"]), str(row["expert_id"])))
        strongest = max(
            BASELINE_IDS,
            key=lambda baseline_id: (
                float(by_id[baseline_id]["weighted_mean_dps"]),
                baseline_id,
            ),
        )
        comparisons: dict[str, JSONMap] = {}
        candidate_dps = float(by_id[self.candidate_id]["weighted_mean_dps"])
        for baseline_id in BASELINE_IDS:
            pair = self.pairs[baseline_id]
            deltas = pair["deltas"]
            comparisons[baseline_id] = {
                "pair_count": len(deltas),
                "weighted_mean_dps_margin": (
                    candidate_dps - float(by_id[baseline_id]["weighted_mean_dps"])
                ),
                "mean_paired_dps": fmean(deltas),
                "minimum_paired_dps": min(deltas),
                "maximum_paired_dps": max(deltas),
                "wins": int(pair["wins"]),
                "ties": int(pair["ties"]),
                "losses": int(pair["losses"]),
                "higher_weighted_dps": candidate_dps
                > float(by_id[baseline_id]["weighted_mean_dps"]),
                "more_paired_wins_than_losses": int(pair["wins"])
                > int(pair["losses"]),
            }
        zero_omissions_by_expert = {
            expert_id: int(by_id[expert_id]["omitted_lane_count"]) == 0
            for expert_id in self.expert_ids
        }
        complete_horizons_by_expert = {
            expert_id: int(by_id[expert_id]["incomplete_horizon_count"]) == 0
            for expert_id in self.expert_ids
        }
        all_experts_zero_omissions = all(zero_omissions_by_expert.values())
        all_experts_all_horizons_complete = all(
            complete_horizons_by_expert.values()
        )
        gate = (
            all_experts_zero_omissions
            and all_experts_all_horizons_complete
            and all(
                row["higher_weighted_dps"]
                and row["more_paired_wins_than_losses"]
                for row in comparisons.values()
            )
        )
        return {
            "ranking": ranking,
            "strongest_baseline_id": strongest,
            "comparisons_vs_each_baseline": comparisons,
            "zero_omissions_by_expert": zero_omissions_by_expert,
            "complete_horizons_by_expert": complete_horizons_by_expert,
            "all_experts_zero_omissions": all_experts_zero_omissions,
            "all_experts_all_horizons_complete": (
                all_experts_all_horizons_complete
            ),
            "candidate_zero_omissions": zero_omissions_by_expert[
                self.candidate_id
            ],
            "candidate_all_horizons_complete": complete_horizons_by_expert[
                self.candidate_id
            ],
            "gate_passed": gate,
        }


def evaluate_heldout_candidate(
    bridge: ClosedLoopBridgeLike,
    corpus: SelectedCorpus,
    *,
    candidate_parameters: FuryPolicyParameters,
    validation_seeds: Iterable[int],
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> JSONMap:
    """Run matched Cat/Contra/candidate seeds and retain compact summaries only."""

    seeds = _seed_tuple(validation_seeds, "validation_seeds")
    candidate = FuryTunedPolicyAdapter(candidate_parameters)
    adapters = (
        CatFurySourceAdapter(),
        ContraDeployedSourceAdapter(),
        candidate,
    )
    expert_ids = tuple(adapter.expert_id for adapter in adapters)
    overall = _ScopeAccumulator(expert_ids, candidate.expert_id)
    by_target: dict[str, _ScopeAccumulator] = {}
    for family_ordinal, family in enumerate(corpus.families, start=1):
        scope = by_target.setdefault(
            family.target_count_stratum,
            _ScopeAccumulator(expert_ids, candidate.expert_id),
        )
        for seed in seeds:
            rows: dict[str, Mapping[str, Any]] = {}
            for adapter in adapters:
                rollout = run_fury_expert_closed_loop(
                    bridge,
                    family.scenario.request,
                    adapter,
                    seed=seed,
                    horizon_ms=family.scenario.horizon_ms,
                    retain_steps=False,
                )
                if bool(rollout.get("source_execution")) or bool(
                    rollout.get("exact_lua_replay")
                ):
                    raise FuryHeldoutCorpusError(
                        "held-out evaluator received an invalid exact-source execution claim"
                    )
                rows[adapter.expert_id] = rollout
            overall.add(
                rows,
                weight=family.scenario.weight,
                horizon_ms=family.scenario.horizon_ms,
            )
            scope.add(
                rows,
                weight=family.scenario.weight,
                horizon_ms=family.scenario.horizon_ms,
            )
        if progress is not None:
            progress(
                {
                    "family_ordinal": family_ordinal,
                    "family_count": len(corpus.families),
                    "instance_id": family.instance_id,
                    "scenario_id": family.scenario.scenario_id,
                    "target_count_stratum": family.target_count_stratum,
                    "completed_rollouts": family_ordinal * len(seeds) * len(adapters),
                    "total_rollouts": len(corpus.families)
                    * len(seeds)
                    * len(adapters),
                }
            )
    overall_result = overall.finish()
    strata = {key: by_target[key].finish() for key in sorted(by_target)}
    heldout_gate = bool(overall_result["gate_passed"]) and all(
        bool(value["gate_passed"]) for value in strata.values()
    )
    return {
        "status": "COMPLETED",
        "validation_seeds": list(seeds),
        "matched_seed_contract": True,
        "retained_rollout_rows": False,
        "rollout_count": len(corpus.families) * len(seeds) * len(adapters),
        "overall": overall_result,
        "target_count_strata": strata,
        "heldout_gate_passed": heldout_gate,
    }


def build_artifact(
    *,
    snapshot: ManifestSnapshot,
    split: InstanceSplit,
    corpus: SelectedCorpus,
    candidate_document: Mapping[str, Any],
    candidate_path: Path,
    candidate_parameters: FuryPolicyParameters,
    armor_hypothesis: int | float,
    level_hypothesis: int,
    selection_seed: int,
    max_families_per_instance: int,
    evaluation: Mapping[str, Any],
) -> JSONMap:
    selected_metadata = [
        {
            "scenario_id": value.scenario.scenario_id,
            "instance_id": value.instance_id,
            "family_id": value.family_id,
            "catalog": value.catalog_relative_path,
            "target_count": value.target_count,
            "target_count_stratum": value.target_count_stratum,
            "duration_ms": value.scenario.horizon_ms,
            "duration_stratum": value.duration_stratum,
            "sampling_weight": value.scenario.weight,
        }
        for value in corpus.families
    ]
    return {
        "schema_version": 1,
        "kind": "fury_policy_heldout_corpus_gate_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifest_snapshot": {
            "path": str(snapshot.manifest_path),
            "updated_at": snapshot.manifest_updated_at,
            "status_counts": dict(snapshot.status_counts),
            "require_complete": snapshot.require_complete,
            "completed_gzip_catalog_count": len(snapshot.completed_catalogs),
            "completed_instance_count": len(snapshot.completed_instance_ids),
            "completed_only": True,
            "raw_normalized_files_read": False,
            "merged_request_corpus_written": False,
        },
        "instance_split": {
            "unit": "INSTANCE",
            "split_seed": split.split_seed,
            "candidate_training_instances": list(split.candidate_training_instances),
            "heldout_instances": list(split.heldout_instances),
            "non_heldout_completed_instances": list(
                split.non_heldout_completed_instances
            ),
            "candidate_training_heldout_overlap": [],
            "instance_disjoint": True,
        },
        "selection_contract": {
            "selection_seed": selection_seed,
            "layout_side": "evidence_bounded",
            "armor_hypothesis": armor_hypothesis,
            "level_hypothesis": level_hypothesis,
            "max_families_per_heldout_instance": max_families_per_instance,
            "target_count_strata": ["1", "2", "3-4", "5+"],
            "duration_strata": ["short_le_10s", "medium_10_30s", "long_gt_30s"],
            "sampling_weight": (
                "eligible family count divided by selected family count within each "
                "represented instance target-count/duration stratum"
            ),
            "unrepresented_cross_strata_are_not_claimed": True,
        },
        "selected_corpus": {
            "family_count": len(corpus.families),
            "instance_summaries": [dict(value) for value in corpus.instance_summaries],
            "families": selected_metadata,
            "request_payloads_persisted": False,
        },
        "candidate": {
            "artifact": str(candidate_path.resolve()),
            "artifact_kind": candidate_document.get("kind"),
            "policy_id": candidate_parameters.policy_id,
            "parameters": asdict(candidate_parameters),
            "source_execution": False,
            "exact_lua_replay": False,
        },
        "baselines": {
            "ids": list(BASELINE_IDS),
            "source_execution": False,
            "exact_lua_replay": False,
        },
        "evaluation": copy.deepcopy(dict(evaluation)),
        "heldout_gate_passed": bool(evaluation.get("heldout_gate_passed", False)),
        "deployment_allowed": False,
        "next_gate": "calibrated real-game shadow evaluation",
        "claims_excluded": [
            "exact Cat or Contra Lua execution",
            "real-game DPS superiority",
            "numeric armor or level identified by Chronicle",
            "independent information from repeated simulator seeds",
            "coverage of unrepresented target-count/duration cross strata",
        ],
    }


def _resolve_hypothesis(
    manifest: Mapping[str, Any],
    explicit: int | float | None,
    key: str,
) -> int | float:
    if explicit is not None:
        return explicit
    contract = manifest.get("artifact_contract")
    values = contract.get(key) if isinstance(contract, Mapping) else None
    if not isinstance(values, list) or len(values) != 1:
        raise FuryHeldoutCorpusError(
            f"--{key.replace('_hypotheses', '-hypothesis')} is required when manifest has "
            "zero or multiple hypotheses"
        )
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryHeldoutCorpusError(f"manifest {key} is not numeric")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--training-instance", action="append", default=[])
    parser.add_argument("--split-seed", type=int, default=2026091401)
    split_group = parser.add_mutually_exclusive_group()
    split_group.add_argument("--heldout-fraction", type=float, default=0.2)
    split_group.add_argument("--heldout-count", type=int)
    parser.add_argument("--selection-seed", type=int, default=2026091402)
    parser.add_argument("--max-families-per-instance", type=int, default=8)
    parser.add_argument("--armor-hypothesis", type=float)
    parser.add_argument("--level-hypothesis", type=int)
    parser.add_argument("--validation-seed", type=int, action="append", dest="seeds")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="write the compact split/selection plan without launching simulator rollouts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest_document = load_json_document(args.manifest.expanduser().resolve())
    snapshot = load_manifest_snapshot(
        args.manifest,
        require_complete=args.require_complete,
    )
    candidate_document = load_json_document(args.candidate.expanduser().resolve())
    candidate_parameters = load_candidate_parameters(candidate_document)
    artifact_training_instances = set(
        extract_candidate_training_instances(candidate_document)
    )
    artifact_training_instances.update(args.training_instance)
    split = split_completed_instances(
        snapshot,
        split_seed=args.split_seed,
        candidate_training_instances=artifact_training_instances,
        heldout_fraction=args.heldout_fraction,
        heldout_count=args.heldout_count,
    )
    armor = _resolve_hypothesis(
        manifest_document, args.armor_hypothesis, "armor_hypotheses"
    )
    level_value = _resolve_hypothesis(
        manifest_document, args.level_hypothesis, "level_hypotheses"
    )
    level = int(level_value)
    corpus = select_heldout_families(
        snapshot,
        split,
        armor_hypothesis=armor,
        level_hypothesis=level,
        selection_seed=args.selection_seed,
        max_families_per_instance=args.max_families_per_instance,
    )
    seeds = tuple(args.seeds or range(2026091501, 2026091517))
    if args.plan_only:
        evaluation: Mapping[str, Any] = {
            "status": "NOT_RUN",
            "validation_seeds": list(seeds),
            "matched_seed_contract": True,
            "retained_rollout_rows": False,
            "heldout_gate_passed": False,
        }
    else:
        with SimulatorBridge(args.bridge) as bridge:
            evaluation = evaluate_heldout_candidate(
                bridge,
                corpus,
                candidate_parameters=candidate_parameters,
                validation_seeds=seeds,
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
        candidate_path=args.candidate,
        candidate_parameters=candidate_parameters,
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
                "status": artifact["evaluation"]["status"],
                "completed_manifest_instances": len(snapshot.completed_instance_ids),
                "heldout_instances": len(split.heldout_instances),
                "selected_families": len(corpus.families),
                "heldout_gate_passed": artifact["heldout_gate_passed"],
                "deployment_allowed": False,
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "CompletedCatalog",
    "FuryHeldoutCorpusError",
    "InstanceSplit",
    "ManifestSnapshot",
    "SelectedCorpus",
    "SelectedFamily",
    "build_artifact",
    "evaluate_heldout_candidate",
    "extract_candidate_training_instances",
    "load_candidate_parameters",
    "load_manifest_snapshot",
    "main",
    "select_heldout_families",
    "split_completed_instances",
)


if __name__ == "__main__":
    raise SystemExit(main())
