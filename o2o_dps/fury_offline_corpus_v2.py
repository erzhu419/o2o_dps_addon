"""Select an evidence-bounded Chronicle corpus for paired Fury evaluation.

V2 reads only the compact per-instance scenario catalogs produced by
``chronicle_encounter_batch_v1``.  It never opens the normalized event rows.
The selector is deliberately a corpus compiler, not an evaluation result:
the currently downloaded 50 raids are development data and cannot become a
validation or final-confirmation holdout by changing a command-line flag.

Leakage groups are connected components of raid instances, repeated players,
and guilds.  The local Chronicle export queue is a supported link source, as
is a small explicit ``fury_offline_leakage_links_v2`` sidecar.  Unlinked raids
share one conservative unresolved component during development and block any
validation/final corpus build.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence


JSONMap = dict[str, Any]
SCHEMA_VERSION = 2
IMPLEMENTATION_REVISION = "v2.0_compact_catalog_evidence_bounded"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BATCH_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_encounter_batch"
    / "v1"
    / "manifest.json"
)
DEFAULT_LINK_SOURCE = PROJECT_ROOT / "offline_data" / "chronicle_raw" / "export_queue.json"
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT / "offline_data" / "derived" / "fury_offline_corpus" / "v2"
)

MAIN_VARIANTS = frozenset(("single_target", "cohit_stacked"))
MAIN_LAYOUT_SIDE = "evidence_bounded"
MIN_PRIMARY_OBSERVED_SPAN_MS = 5_000
CURRENT_LOCAL_COHORT = "current_local_50"
DEVELOPMENT_ROLE = "development"
NON_DEVELOPMENT_ROLES = frozenset(("selection_validation", "final_confirmation"))


class FuryOfflineCorpusError(RuntimeError):
    """The compact inputs cannot satisfy the V2 corpus contract."""


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, value: str) -> str:
        self.parent.setdefault(value, value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryOfflineCorpusError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryOfflineCorpusError(f"{label} must be a JSON array")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryOfflineCorpusError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryOfflineCorpusError(f"{label} must be an integer")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise FuryOfflineCorpusError(f"value is not canonical JSON: {error}") from error
    return rendered.encode("utf-8")


def sha256_json(value: Any) -> str:
    """Return the full SHA-256 of a canonical JSON value."""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FuryOfflineCorpusError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _load_json(path: Path) -> JSONMap:
    resolved = path.expanduser().resolve()
    try:
        if resolved.suffix.casefold() == ".gz":
            with gzip.open(resolved, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryOfflineCorpusError(f"cannot read JSON object {resolved}: {error}") from error
    if not isinstance(value, dict):
        raise FuryOfflineCorpusError(f"JSON artifact is not an object: {resolved}")
    return value


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _resolve_catalog_path(batch_manifest_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = batch_manifest_path.parent / candidate
    return candidate.resolve()


def _catalog_inputs(
    batch_manifest_path: Path,
    batch_manifest: Mapping[str, Any],
) -> list[tuple[Path, Mapping[str, Any]]]:
    if batch_manifest.get("kind") != "chronicle_encounter_batch_manifest_v1":
        raise FuryOfflineCorpusError(
            "batch manifest kind must be chronicle_encounter_batch_manifest_v1"
        )
    entries = _array(batch_manifest.get("entries"), "batch_manifest.entries")
    selected: list[tuple[Path, Mapping[str, Any]]] = []
    incomplete = 0
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"batch_manifest.entries[{index}]")
        if entry.get("processing_status") != "COMPLETED":
            incomplete += 1
            continue
        outputs = _mapping(entry.get("outputs"), f"batch_manifest.entries[{index}].outputs")
        catalog_ref = _mapping(
            outputs.get("scenario_catalog"),
            f"batch_manifest.entries[{index}].outputs.scenario_catalog",
        )
        raw_path = _nonempty_string(
            catalog_ref.get("path"),
            f"batch_manifest.entries[{index}].outputs.scenario_catalog.path",
        )
        path = _resolve_catalog_path(batch_manifest_path, raw_path)
        if not path.is_file():
            raise FuryOfflineCorpusError(f"declared compact catalog does not exist: {path}")
        declared_size = catalog_ref.get("size_bytes")
        if declared_size is not None and _integer(
            declared_size,
            f"batch_manifest.entries[{index}].outputs.scenario_catalog.size_bytes",
        ) != path.stat().st_size:
            raise FuryOfflineCorpusError(f"catalog size no longer matches manifest: {path}")
        selected.append((path, entry))
    if incomplete:
        raise FuryOfflineCorpusError(
            f"batch manifest contains {incomplete} incomplete entries; corpus selection is fail-closed"
        )
    if not selected:
        raise FuryOfflineCorpusError("batch manifest has no completed compact catalogs")
    selected.sort(key=lambda pair: _display_path(pair[0]).casefold())
    declared_completed = (
        _mapping(batch_manifest.get("summary"), "batch_manifest.summary")
    ).get("completed_file_count")
    if declared_completed is not None and _integer(
        declared_completed, "batch_manifest.summary.completed_file_count"
    ) != len(selected):
        raise FuryOfflineCorpusError(
            "completed catalog count does not match batch manifest summary"
        )
    return selected


def _identity_token(kind: str, value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip().casefold()
    if not rendered:
        return None
    return f"{kind}:{rendered}"


def _link_records_from_export_queue(document: Mapping[str, Any]) -> list[JSONMap]:
    records: list[JSONMap] = []
    for index, raw_entry in enumerate(_array(document.get("entries"), "link_source.entries")):
        entry = _mapping(raw_entry, f"link_source.entries[{index}]")
        aliases = [
            str(value).strip()
            for value in (
                entry.get("instance_id"),
                entry.get("download_instance_id"),
                entry.get("slug"),
            )
            if value is not None and str(value).strip()
        ]
        entities: set[str] = set()
        guild = entry.get("guild")
        if isinstance(guild, Mapping):
            token = _identity_token("guild", guild.get("id") or guild.get("name"))
            if token:
                entities.add(token)
        for row_index, raw_row in enumerate(entry.get("leaderboard_rows") or []):
            row = _mapping(
                raw_row,
                f"link_source.entries[{index}].leaderboard_rows[{row_index}]",
            )
            character = str(row.get("character") or "").strip()
            realm = str(row.get("realm") or "").strip()
            token = _identity_token("player", f"{realm}|{character}" if character else None)
            if token:
                entities.add(token)
        # ``discovered_from`` records how the URL was found; it is not a
        # roster-membership contract.  Only leaderboard rows and the resolved
        # guild identity are admitted as leakage links.
        records.append({"aliases": sorted(set(aliases)), "entities": sorted(entities)})
    return records


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _link_records_from_explicit_sidecar(document: Mapping[str, Any]) -> list[JSONMap]:
    records: list[JSONMap] = []
    for index, raw_link in enumerate(_array(document.get("links"), "link_source.links")):
        link = _mapping(raw_link, f"link_source.links[{index}]")
        aliases: set[str] = set()
        for field in ("instance_ref", "instance_id", "instance_slug", "instance_aliases"):
            for value in _values(link.get(field)):
                if value is not None and str(value).strip():
                    aliases.add(str(value).strip())
        if not aliases:
            raise FuryOfflineCorpusError(
                f"link_source.links[{index}] has no instance reference or alias"
            )
        entities: set[str] = set()
        for field in ("player_guid", "player_id", "player_token"):
            for value in _values(link.get(field)):
                token = _identity_token("player", value)
                if token:
                    entities.add(token)
        for field in ("guild_id", "guild_name", "guild_token"):
            for value in _values(link.get(field)):
                token = _identity_token("guild", value)
                if token:
                    entities.add(token)
        for raw_player in _values(link.get("players")):
            player = _mapping(raw_player, f"link_source.links[{index}].players[]")
            direct = player.get("guid") or player.get("id") or player.get("token")
            if direct is None and player.get("character"):
                direct = f"{player.get('realm') or ''}|{player.get('character')}"
            token = _identity_token("player", direct)
            if token:
                entities.add(token)
        records.append({"aliases": sorted(aliases), "entities": sorted(entities)})
    return records


def _load_link_records(link_source_path: Path) -> tuple[list[JSONMap], JSONMap]:
    document = _load_json(link_source_path)
    kind = str(document.get("kind") or document.get("schema") or "")
    if kind == "chronicle_manual_export_queue":
        records = _link_records_from_export_queue(document)
    elif kind in (
        "fury_offline_leakage_links_v2",
        "fury_offline_leakage_links/v2",
    ):
        records = _link_records_from_explicit_sidecar(document)
    else:
        raise FuryOfflineCorpusError(
            "link source must be chronicle_manual_export_queue or "
            "fury_offline_leakage_links_v2"
        )
    if not records:
        raise FuryOfflineCorpusError("link source contains no link records")
    return records, {
        "path": _display_path(link_source_path),
        "kind": kind,
        "size_bytes": link_source_path.stat().st_size,
        "sha256": _sha256_file(link_source_path),
        "raw_identity_values_embedded_in_output": False,
    }


def derive_leakage_components(
    instance_refs: Sequence[str],
    link_records: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, JSONMap], JSONMap]:
    """Derive conservative instance/player/guild connected components.

    Raw player and guild identifiers are used only inside the disjoint set.
    The returned structure contains instance membership and content-derived
    component identifiers, but never those raw identities.
    """

    instances = sorted(set(instance_refs))
    if not instances:
        raise FuryOfflineCorpusError("cannot group an empty instance set")
    disjoint = _DisjointSet()
    instance_nodes = {value: f"instance:{value}" for value in instances}
    for node in instance_nodes.values():
        disjoint.find(node)

    alias_to_instances: dict[str, set[str]] = defaultdict(set)
    for instance in instances:
        alias_to_instances[instance.casefold()].add(instance)
    linked: set[str] = set()
    matched_records = 0
    for record_index, raw_record in enumerate(link_records or []):
        record = _mapping(raw_record, f"link_records[{record_index}]")
        aliases = {
            str(value).strip().casefold()
            for value in _array(record.get("aliases"), f"link_records[{record_index}].aliases")
            if str(value).strip()
        }
        matched = sorted(
            {
                instance
                for alias in aliases
                for instance in alias_to_instances.get(alias, set())
            }
        )
        if not matched:
            continue
        matched_records += 1
        entities = {
            str(value).strip().casefold()
            for value in _array(record.get("entities"), f"link_records[{record_index}].entities")
            if str(value).strip()
        }
        first_node = instance_nodes[matched[0]]
        for instance in matched[1:]:
            disjoint.union(first_node, instance_nodes[instance])
        for entity in sorted(entities):
            disjoint.union(first_node, f"entity:{entity}")
        if entities:
            linked.update(matched)

    unresolved = sorted(set(instances) - linked)
    if unresolved:
        unresolved_node = "entity:__all_unresolved_instances__"
        for instance in unresolved:
            disjoint.union(instance_nodes[instance], unresolved_node)

    grouped: dict[str, list[str]] = defaultdict(list)
    for instance in instances:
        grouped[disjoint.find(instance_nodes[instance])].append(instance)
    components = sorted(
        (sorted(values) for values in grouped.values()),
        key=lambda values: (-len(values), values),
    )
    assignments: dict[str, JSONMap] = {}
    component_documents: list[JSONMap] = []
    unresolved_set = set(unresolved)
    for members in components:
        component_id = f"leakage-{sha256_json(members)[:16]}"
        has_unresolved = bool(unresolved_set.intersection(members))
        component_documents.append(
            {
                "component_id": component_id,
                "instance_count": len(members),
                "instance_refs": members,
                "link_status": (
                    "unresolved_conservative_shared" if has_unresolved else "resolved"
                ),
            }
        )
        for member in members:
            assignments[member] = {
                "component_id": component_id,
                "link_status": (
                    "unresolved_conservative_shared"
                    if member in unresolved_set
                    else "resolved"
                ),
            }
    return assignments, {
        "method": "connected_components_of_instance_player_and_guild_links",
        "component_count": len(components),
        "component_sizes_descending": sorted(
            (len(values) for values in components), reverse=True
        ),
        "matched_link_record_count": matched_records,
        "linked_instance_count": len(linked),
        "unresolved_instance_count": len(unresolved),
        "unresolved_instance_refs": unresolved,
        "unresolved_policy": "one_shared_component; validation_and_final_fail_closed",
        "raw_player_or_guild_identity_embedded": False,
        "components": component_documents,
    }


def _scenario_bucket(scenario: Mapping[str, Any]) -> tuple[str, str, str, int]:
    duration = _mapping(scenario.get("duration"), "scenario.duration")
    observed_span_ms = _integer(
        duration.get("observed_span_ms"), "scenario.duration.observed_span_ms"
    )
    if observed_span_ms < 0:
        raise FuryOfflineCorpusError("scenario observed span must be nonnegative")
    pile = _mapping(scenario.get("pile"), "scenario.pile")
    variant = _nonempty_string(pile.get("layout_variant"), "scenario.pile.layout_variant")
    layout_side = _nonempty_string(pile.get("layout_side"), "scenario.pile.layout_side")
    if observed_span_ms < MIN_PRIMARY_OBSERVED_SPAN_MS:
        return "short_auxiliary", variant, layout_side, observed_span_ms
    if variant in MAIN_VARIANTS and layout_side == MAIN_LAYOUT_SIDE:
        return "main_comparison", variant, layout_side, observed_span_ms
    return "unknown_layout_sensitivity", variant, layout_side, observed_span_ms


def _provenance_guard(scenario: Mapping[str, Any]) -> JSONMap:
    hypotheses = _mapping(scenario.get("target_hypotheses"), "scenario.target_hypotheses")
    armor = _mapping(hypotheses.get("armor"), "scenario.target_hypotheses.armor")
    pile = _mapping(scenario.get("pile"), "scenario.pile")
    spatial = _mapping(pile.get("spatial_assumption"), "scenario.pile.spatial_assumption")
    if armor.get("status") == "OBSERVED":
        raise FuryOfflineCorpusError(
            "catalog claims observed numeric armor, contrary to the V1 provenance contract"
        )
    coordinates = spatial.get("coordinates")
    if coordinates is not None:
        raise FuryOfflineCorpusError(
            "catalog contains coordinates even though Chronicle positions are not observed"
        )
    proxies = _array(scenario.get("kill_budget_proxies"), "scenario.kill_budget_proxies")
    proxy_values: list[float] = []
    for index, raw_proxy in enumerate(proxies):
        proxy = _mapping(raw_proxy, f"scenario.kill_budget_proxies[{index}]")
        value = proxy.get("value")
        if isinstance(value, bool) or (
            value is not None and not isinstance(value, (int, float))
        ):
            raise FuryOfflineCorpusError(
                f"scenario.kill_budget_proxies[{index}].value must be numeric or null"
            )
        if isinstance(value, (int, float)):
            if value < 0:
                raise FuryOfflineCorpusError("kill-budget proxy values must be nonnegative")
            proxy_values.append(float(value))

    # The compact V1 catalog intentionally retained cumulative incoming-damage
    # proxies, not the reconstruction's single-hit survival witnesses.  A
    # cumulative kill budget can cross Contra's 25k/51k branches, but overkill,
    # healing, and partial rows mean that it is not a strict max-health lower
    # bound.  Report both facts so downstream code cannot silently substitute
    # one for the other.
    proxy_thresholds = {
        str(threshold): {
            "observed_kill_budget_at_or_above": any(
                value >= threshold for value in proxy_values
            ),
            "identifies_target_max_health_at_or_above": False,
        }
        for threshold in (25_000, 51_000)
    }
    return {
        "target_max_health": {
            "status": "MISSING",
            "threshold_fact_status": "UNKNOWN",
            "strict_lower_bound_available_in_compact_catalog": False,
            "rule": (
                "kill-budget proxies are cumulative incoming damage through death, not "
                "a strict maximum-health lower bound"
            ),
        },
        "observed_kill_budget_proxy": {
            "status": "OBSERVED_PROXY" if proxy_values else "MISSING",
            "available_target_count": len(proxy_values),
            "target_count": len(proxies),
            "maximum_value": max(proxy_values) if proxy_values else None,
            "thresholds": proxy_thresholds,
            "usable_as_unit_health_max": False,
        },
        "numeric_armor": {
            "status": armor.get("status"),
            "rule": "preserved catalog hypothesis; not observed Chronicle data",
        },
        "unit_classification": {
            "status": "MISSING",
            "rule": "Hostile Creature class rows are not WoW UnitClassification enums",
        },
        "positions": {
            "status": spatial.get("status"),
            "coordinates": None,
            "rule": "preserve positive co-hit/layout sensitivity semantics only",
        },
    }


def _selected_scenario(
    scenario: Mapping[str, Any],
    *,
    catalog_path: Path,
    catalog_sha256: str,
    catalog_index: int,
    component: Mapping[str, Any],
) -> JSONMap:
    scenario_id = _nonempty_string(scenario.get("scenario_id"), "scenario.scenario_id")
    source = deepcopy(dict(_mapping(scenario.get("source"), "scenario.source")))
    request = _mapping(scenario.get("request"), "scenario.request")
    encounter = _mapping(request.get("encounter"), "scenario.request.encounter")
    if encounter.get("useHealth") is not False:
        raise FuryOfflineCorpusError(
            "V2 accepts only duration-mode requests with encounter.useHealth == false"
        )
    pile = deepcopy(dict(_mapping(scenario.get("pile"), "scenario.pile")))
    hypotheses = deepcopy(
        dict(_mapping(scenario.get("target_hypotheses"), "scenario.target_hypotheses"))
    )
    kill_budget_proxies = deepcopy(
        _array(scenario.get("kill_budget_proxies"), "scenario.kill_budget_proxies")
    )
    weight = deepcopy(dict(_mapping(scenario.get("weight"), "scenario.weight")))
    bucket, variant, layout_side, observed_span_ms = _scenario_bucket(scenario)
    return {
        "scenario_id": scenario_id,
        "bucket": bucket,
        "source": source,
        "pile": pile,
        "target_hypotheses": hypotheses,
        "kill_budget_proxies": kill_budget_proxies,
        "weight": weight,
        "observed_span_ms": observed_span_ms,
        "layout_variant": variant,
        "layout_side": layout_side,
        "leakage_component": dict(component),
        "catalog_locator": {
            "path": _display_path(catalog_path),
            "catalog_sha256": catalog_sha256,
            "scenario_index": catalog_index,
        },
        "hashes": {
            "scenario_sha256": sha256_json(scenario),
            "source_sha256": sha256_json(source),
            "request_sha256": sha256_json(request),
            "pile_sha256": sha256_json(pile),
            "target_hypotheses_sha256": sha256_json(hypotheses),
            "kill_budget_proxies_sha256": sha256_json(kill_budget_proxies),
            "weight_sha256": sha256_json(weight),
        },
        "provenance_guard": _provenance_guard(scenario),
    }


def _summary(entries: Sequence[Mapping[str, Any]], catalog_count: int) -> JSONMap:
    buckets: Counter[str] = Counter()
    variants: dict[str, Counter[str]] = defaultdict(Counter)
    families: dict[str, set[str]] = defaultdict(set)
    instances: dict[str, set[str]] = defaultdict(set)
    encounters: dict[str, set[str]] = defaultdict(set)
    target_counts: dict[str, Counter[str]] = defaultdict(Counter)
    field_counts: Counter[str] = Counter()
    for entry in entries:
        bucket = str(entry["bucket"])
        buckets[bucket] += 1
        variants[bucket][str(entry["layout_variant"])] += 1
        pile = _mapping(entry["pile"], "selected_entry.pile")
        family = _nonempty_string(
            pile.get("sensitivity_family"), "selected_entry.pile.sensitivity_family"
        )
        families[bucket].add(family)
        source = _mapping(entry["source"], "selected_entry.source")
        instances[bucket].add(
            _nonempty_string(source.get("instance"), "selected_entry.source.instance")
        )
        encounters[bucket].add(
            _nonempty_string(source.get("encounter"), "selected_entry.source.encounter")
        )
        count = _integer(pile.get("target_count"), "selected_entry.pile.target_count")
        target_counts[bucket][str(count)] += 1
        guard = _mapping(entry.get("provenance_guard"), "selected_entry.provenance_guard")
        kill_budget = _mapping(
            guard.get("observed_kill_budget_proxy"),
            "selected_entry.provenance_guard.observed_kill_budget_proxy",
        )
        if kill_budget.get("status") == "OBSERVED_PROXY":
            field_counts["scenarios_with_any_observed_kill_budget_proxy"] += 1
        else:
            field_counts["scenarios_without_observed_kill_budget_proxy"] += 1
        for threshold in (25_000, 51_000):
            threshold_row = _mapping(
                _mapping(kill_budget.get("thresholds"), "kill_budget.thresholds").get(
                    str(threshold)
                ),
                f"kill_budget.thresholds.{threshold}",
            )
            if threshold_row.get("observed_kill_budget_at_or_above") is True:
                field_counts[f"scenarios_with_kill_budget_ge_{threshold}"] += 1
    ordered_buckets = (
        "main_comparison",
        "unknown_layout_sensitivity",
        "short_auxiliary",
    )
    return {
        "compact_catalog_count": catalog_count,
        "scenario_count": len(entries),
        "unique_scenario_id_count": len({str(entry["scenario_id"]) for entry in entries}),
        "buckets": {
            bucket: {
                "scenario_count": buckets[bucket],
                "family_count": len(families[bucket]),
                "instance_count": len(instances[bucket]),
                "encounter_count": len(encounters[bucket]),
                "variant_counts": dict(sorted(variants[bucket].items())),
                "target_count_distribution": dict(
                    sorted(target_counts[bucket].items(), key=lambda item: int(item[0]))
                ),
            }
            for bucket in ordered_buckets
        },
        "field_availability": {
            "target_max_health_exact": "MISSING",
            "target_max_health_threshold": "UNKNOWN",
            "strict_health_lower_bound_in_compact_catalog": "MISSING",
            "unit_classification": "MISSING",
            "numeric_armor": "INFERRED_SENSITIVITY_HYPOTHESIS",
            "exact_positions": "MISSING",
            "request_mode": "duration_only_useHealth_false",
            "observed_kill_budget_proxy_counts": dict(sorted(field_counts.items())),
            "kill_budget_proxy_is_usable_as_unit_health_max": False,
            "environment_contract_blockers": [
                "target_max_health_exact_missing",
                "target_max_health_25k_51k_branch_unknown",
                "strict_health_lower_bound_not_retained_in_compact_v1_catalog",
                "wow_unit_classification_missing",
                "numeric_armor_is_an_explicit_sensitivity_hypothesis_only",
                "exact_target_coordinates_and_separation_missing",
            ],
            "downstream_branch_rule": (
                "evaluate explicit max-health, armor, classification, and unknown-layout "
                "sensitivity hypotheses; do not choose a Contra branch from kill budget"
            ),
        },
    }


def compile_fury_offline_corpus_v2(
    batch_manifest_path: Path = DEFAULT_BATCH_MANIFEST,
    *,
    link_source_path: Path | None = DEFAULT_LINK_SOURCE,
    corpus_role: str = DEVELOPMENT_ROLE,
    source_cohort: str = CURRENT_LOCAL_COHORT,
) -> JSONMap:
    """Compile a deterministic, content-addressed V2 corpus manifest."""

    if corpus_role not in ({DEVELOPMENT_ROLE} | NON_DEVELOPMENT_ROLES):
        raise FuryOfflineCorpusError(f"unsupported corpus role: {corpus_role}")
    if source_cohort == CURRENT_LOCAL_COHORT and corpus_role != DEVELOPMENT_ROLE:
        raise FuryOfflineCorpusError(
            "the current downloaded 50-raid cohort is development-only and cannot be "
            "relabeled as validation or final confirmation"
        )
    resolved_manifest = batch_manifest_path.expanduser().resolve()
    batch_manifest = _load_json(resolved_manifest)
    catalogs = _catalog_inputs(resolved_manifest, batch_manifest)

    catalog_documents: list[tuple[Path, str, Mapping[str, Any], Mapping[str, Any]]] = []
    instance_refs: set[str] = set()
    declared_total = 0
    for path, batch_entry in catalogs:
        catalog_sha = _sha256_file(path)
        catalog = _load_json(path)
        if catalog.get("kind") != "fury_encounter_scenario_catalog_v1":
            raise FuryOfflineCorpusError(f"unexpected compact catalog kind: {path}")
        scenarios = _array(catalog.get("scenarios"), f"{path}.scenarios")
        summary = _mapping(catalog.get("summary"), f"{path}.summary")
        declared = _integer(summary.get("scenario_count"), f"{path}.summary.scenario_count")
        if declared != len(scenarios):
            raise FuryOfflineCorpusError(f"catalog scenario count mismatch: {path}")
        batch_summary = _mapping(batch_entry.get("catalog_summary"), "batch entry catalog_summary")
        if _integer(batch_summary.get("scenario_count"), "batch catalog_summary.scenario_count") != len(
            scenarios
        ):
            raise FuryOfflineCorpusError(f"batch/catalog scenario count mismatch: {path}")
        declared_total += declared
        for raw_scenario in scenarios:
            scenario = _mapping(raw_scenario, f"{path}.scenario")
            source = _mapping(scenario.get("source"), f"{path}.scenario.source")
            instance_refs.add(
                _nonempty_string(source.get("instance"), f"{path}.scenario.source.instance")
            )
        catalog_documents.append((path, catalog_sha, catalog, batch_entry))

    batch_catalog_total = _mapping(
        _mapping(batch_manifest.get("summary"), "batch_manifest.summary").get("catalog_totals"),
        "batch_manifest.summary.catalog_totals",
    ).get("scenario_count")
    if batch_catalog_total is not None and _integer(
        batch_catalog_total, "batch_manifest.summary.catalog_totals.scenario_count"
    ) != declared_total:
        raise FuryOfflineCorpusError("total scenario count does not match batch manifest")
    if source_cohort == CURRENT_LOCAL_COHORT and len(instance_refs) != 50:
        raise FuryOfflineCorpusError(
            f"current_local_50 must contain exactly 50 instances; found {len(instance_refs)}"
        )

    records: list[JSONMap] | None = None
    link_source_provenance: JSONMap = {
        "path": None,
        "kind": None,
        "size_bytes": 0,
        "sha256": None,
        "raw_identity_values_embedded_in_output": False,
    }
    if link_source_path is not None:
        resolved_links = link_source_path.expanduser().resolve()
        if not resolved_links.is_file():
            raise FuryOfflineCorpusError(f"link source does not exist: {resolved_links}")
        records, link_source_provenance = _load_link_records(resolved_links)
    assignments, leakage = derive_leakage_components(sorted(instance_refs), records)
    if corpus_role in NON_DEVELOPMENT_ROLES and leakage["unresolved_instance_count"]:
        raise FuryOfflineCorpusError(
            "validation/final corpus has instances without player/guild leakage links"
        )

    selected: list[JSONMap] = []
    seen_ids: set[str] = set()
    catalog_provenance: list[JSONMap] = []
    for path, catalog_sha, catalog, _batch_entry in catalog_documents:
        scenarios = _array(catalog.get("scenarios"), f"{path}.scenarios")
        catalog_provenance.append(
            {
                "path": _display_path(path),
                "size_bytes": path.stat().st_size,
                "sha256": catalog_sha,
                "scenario_count": len(scenarios),
            }
        )
        for index, raw_scenario in enumerate(scenarios):
            scenario = _mapping(raw_scenario, f"{path}.scenarios[{index}]")
            scenario_id = _nonempty_string(
                scenario.get("scenario_id"), f"{path}.scenarios[{index}].scenario_id"
            )
            if scenario_id in seen_ids:
                raise FuryOfflineCorpusError(f"duplicate scenario_id across catalogs: {scenario_id}")
            seen_ids.add(scenario_id)
            source = _mapping(scenario.get("source"), f"{path}.scenarios[{index}].source")
            instance = _nonempty_string(
                source.get("instance"), f"{path}.scenarios[{index}].source.instance"
            )
            selected.append(
                _selected_scenario(
                    scenario,
                    catalog_path=path,
                    catalog_sha256=catalog_sha,
                    catalog_index=index,
                    component=assignments[instance],
                )
            )
    selected.sort(key=lambda entry: str(entry["scenario_id"]))
    summary = _summary(selected, len(catalogs))

    core: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "schema": "fury_offline_corpus/v2",
        "kind": "fury_offline_corpus_manifest_v2",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "corpus_role": corpus_role,
        "source_cohort": source_cohort,
        "claim_boundary": {
            "current_local_50_is_development_only": True,
            "final_holdout_claim": False,
            "meaning": "corpus compilation is not simulator execution or a superiority result",
        },
        "selection_contract": {
            "main_comparison": {
                "minimum_observed_span_ms": MIN_PRIMARY_OBSERVED_SPAN_MS,
                "layout_side": MAIN_LAYOUT_SIDE,
                "allowed_variants": sorted(MAIN_VARIANTS),
            },
            "unknown_layout_sensitivity": {
                "minimum_observed_span_ms": MIN_PRIMARY_OBSERVED_SPAN_MS,
                "rule": "every non-main layout; never pooled into the main comparison",
            },
            "short_auxiliary": {
                "maximum_observed_span_ms_exclusive": MIN_PRIMARY_OBSERVED_SPAN_MS,
                "allowed_uses": ["cumulative_damage", "action_legality"],
                "dps_superiority_use": False,
            },
            "normalized_event_rows_opened": False,
            "target_hp_armor_classification_or_coordinates_fabricated": False,
        },
        "inputs": {
            "batch_manifest": {
                "path": _display_path(resolved_manifest),
                "size_bytes": resolved_manifest.stat().st_size,
                "sha256": _sha256_file(resolved_manifest),
            },
            "link_source": link_source_provenance,
            "catalogs": catalog_provenance,
        },
        "leakage_components": leakage,
        "summary": summary,
        "scenarios": selected,
    }
    result = deepcopy(core)
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    return result


def verify_content_address(manifest: Mapping[str, Any]) -> bool:
    """Verify the manifest's self-declared canonical content address."""

    content_address = _mapping(manifest.get("content_address"), "manifest.content_address")
    expected = _nonempty_string(content_address.get("sha256"), "content_address.sha256")
    core = {key: value for key, value in manifest.items() if key != "content_address"}
    return expected == sha256_json(core)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_write_json_gzip(path: Path, value: Mapping[str, Any]) -> None:
    """Write deterministic pretty JSON in gzip form (mtime/name are pinned)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=handle,
                mtime=0,
            ) as compressed:
                compressed.write(payload)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_content_addressed_manifest(
    manifest: Mapping[str, Any], output_directory: Path = DEFAULT_OUTPUT_DIRECTORY
) -> Path:
    if not verify_content_address(manifest):
        raise FuryOfflineCorpusError("refusing to write a manifest with an invalid content address")
    digest = str(_mapping(manifest["content_address"], "content_address")["sha256"])
    output = output_directory.expanduser().resolve() / f"fury_offline_corpus_v2.{digest}.json.gz"
    if output.exists():
        existing = _load_json(output)
        if existing != dict(manifest):
            raise FuryOfflineCorpusError(f"content-address collision at {output}")
        return output
    _atomic_write_json_gzip(output, manifest)
    return output


def _plan_document(manifest: Mapping[str, Any]) -> JSONMap:
    address = _mapping(manifest.get("content_address"), "manifest.content_address")
    return {
        "kind": "fury_offline_corpus_plan_v2",
        "execution_started": False,
        "corpus_role": manifest.get("corpus_role"),
        "source_cohort": manifest.get("source_cohort"),
        "manifest_sha256": address.get("sha256"),
        "summary": manifest.get("summary"),
        "leakage_summary": {
            key: manifest["leakage_components"][key]
            for key in (
                "component_count",
                "component_sizes_descending",
                "linked_instance_count",
                "unresolved_instance_count",
            )
        },
        "final_holdout_claim": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-manifest", type=Path, default=DEFAULT_BATCH_MANIFEST)
    parser.add_argument("--link-source", type=Path, default=DEFAULT_LINK_SOURCE)
    parser.add_argument(
        "--without-link-source",
        action="store_true",
        help="development only; all instances become one unresolved conservative component",
    )
    parser.add_argument(
        "--corpus-role",
        choices=(DEVELOPMENT_ROLE, *sorted(NON_DEVELOPMENT_ROLES)),
        default=DEVELOPMENT_ROLE,
    )
    parser.add_argument(
        "--source-cohort",
        choices=(CURRENT_LOCAL_COHORT, "future_post_freeze"),
        default=CURRENT_LOCAL_COHORT,
    )
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="print deterministic counts and hashes without writing an artifact",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    link_source = None if args.without_link_source else args.link_source
    manifest = compile_fury_offline_corpus_v2(
        args.batch_manifest,
        link_source_path=link_source,
        corpus_role=args.corpus_role,
        source_cohort=args.source_cohort,
    )
    if args.plan_only:
        print(json.dumps(_plan_document(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    output = write_content_addressed_manifest(manifest, args.output_directory)
    result = _plan_document(manifest)
    result["output"] = _display_path(output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
