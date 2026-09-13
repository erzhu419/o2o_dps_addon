"""Join exact Fury START observations to decision-time historical builds.

This is a development-only evidence product.  Every recognized controllable
START is joined by exact server/realm/player GUID/instance identity and the
latest CombatantInfo anchor at or before that START.  A later build is never
used to fill an earlier missing prefix.

The emitted mapping keeps one compact reference per decision.  Historical
equipment and talent objects remain in the content-bound source catalogue;
selected segment identities and compact routing features are written once in a
deduplicated dictionary partition.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Iterator, Mapping, Sequence, TextIO

from . import fury_build_conditioned_development_bundle_v1 as bundle_v1
from . import historical_build_catalog_v1 as catalog_v1
from . import historical_fury_behavior_prototypes_v1 as prototypes_v1
from . import historical_fury_expert_cohort_v2 as cohort_v2
from . import historical_fury_expert_episode_adapter_v1 as episode_v1
from .chronicle_external_reconstruction_admission_v1 import canonical_guid


JSONMap = dict[str, Any]
MANIFEST_SCHEMA = "historical_fury_decision_build_join/v1"
MAPPING_SCHEMA = "historical_fury_decision_build_mapping/v1"
SEGMENT_DICTIONARY_SCHEMA = "historical_fury_build_segment_reference/v1"
IMPLEMENTATION_REVISION = "v1.1_exact_identity_strict_prefix_portable_bindings"
STATUS = "DEVELOPMENT_CAUSAL_BUILD_JOIN_NOT_COMPARISON"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_COHORT = (
    DEFAULT_DATA_ROOT / "derived" / "historical_fury_expert_cohort" / "v2" / "cohort.json"
)
DEFAULT_EPISODE_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_expert_episodes"
    / "v1"
    / "manifest.json"
)
DEFAULT_CATALOG_MANIFEST = (
    DEFAULT_DATA_ROOT / "derived" / "historical_build_catalog" / "v1" / "manifest.json"
)
DEFAULT_PROTOTYPE_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_behavior_prototypes"
    / "v1"
    / "manifest.json"
)
DEFAULT_DEVELOPMENT_BUNDLE = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "fury_build_conditioned_development_bundle"
    / "v1"
    / "current.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_decision_build_join"
    / "v1"
)

JOINED = "JOINED_EXACT_CAUSAL_PREFIX"
MISSING_IDENTITY = "MISSING_NO_EXACT_IDENTITY_SEGMENTS"
MISSING_PREFIX = "MISSING_NO_PRIOR_EXACT_BUILD_INFO"

ROUTE_EXACT = "EXACT_SOURCE_BUILD"
ROUTE_SIMILAR = "SIMILAR_TALENTS_AND_WEAPON_MODE"
ROUTE_TRANSPLANT = "TRANSPLANT_DIFFERENT_KNOWN_BUILD"
ROUTE_UNKNOWN_BUILD = "UNKNOWN_INSUFFICIENT_BUILD_SEMANTICS"
ROUTE_UNKNOWN_PREFIX = "UNKNOWN_NO_PREFIX_BUILD"
ROUTE_REPORT_ORDER = (
    ROUTE_EXACT,
    ROUTE_SIMILAR,
    ROUTE_TRANSPLANT,
    ROUTE_UNKNOWN_BUILD,
    ROUTE_UNKNOWN_PREFIX,
)


class HistoricalFuryDecisionBuildJoinV1Error(RuntimeError):
    """An input closure, causal join, or output accounting contract failed."""


@dataclass(frozen=True)
class DecisionBuildJoinResult:
    manifest: Path
    content_addressed_manifest: Path
    decision_count: int
    joined_count: int
    missing_count: int
    selected_segment_count: int
    mapping_partition_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": MANIFEST_SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "decision_count": self.decision_count,
            "joined_count": self.joined_count,
            "missing_count": self.missing_count,
            "selected_segment_count": self.selected_segment_count,
            "mapping_partition_count": self.mapping_partition_count,
        }


@dataclass(frozen=True)
class _RequestRoute:
    representative_rank: int
    identity: tuple[str, str, str, str, str]
    request_sha256: str
    similarity_signature_sha256: str
    weapon_mode: str


@dataclass
class _ScopeStats:
    request_ranks: tuple[int, ...]
    decisions: int = 0
    joined: int = 0
    missing: int = 0
    binding_runs: int = 0
    joined_build_changes: int = 0
    episodes: set[str] = field(default_factory=set)
    waves: set[str] = field(default_factory=set)
    segments: set[str] = field(default_factory=set)
    similarity_signatures: set[str] = field(default_factory=set)
    join_statuses: Counter[str] = field(default_factory=Counter)
    actions: Counter[str] = field(default_factory=Counter)
    talent_statuses: Counter[str] = field(default_factory=Counter)
    weapon_modes: Counter[str] = field(default_factory=Counter)
    runtime_flags: Counter[str] = field(default_factory=Counter)
    representative_flags: Counter[str] = field(default_factory=Counter)
    development_flags: Counter[str] = field(default_factory=Counter)
    comparison_flags: Counter[str] = field(default_factory=Counter)
    best_routes: Counter[str] = field(default_factory=Counter)
    routes_by_request: dict[int, Counter[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.routes_by_request = {rank: Counter() for rank in self.request_ranks}

    def register_wave(
        self,
        *,
        episode_id: str,
        wave_id: str,
        binding_run_count: int,
        joined_build_change_count: int,
    ) -> None:
        self.episodes.add(episode_id)
        self.waves.add(f"{episode_id}\x00{wave_id}")
        self.binding_runs += binding_run_count
        self.joined_build_changes += joined_build_change_count

    def observe(
        self,
        *,
        action_key: str,
        join_status: str,
        segment_meta: Mapping[str, Any] | None,
        routes: Sequence[Mapping[str, Any]] | None,
    ) -> None:
        self.decisions += 1
        self.actions[action_key] += 1
        self.join_statuses[join_status] += 1
        if segment_meta is None:
            self.missing += 1
            self.best_routes[ROUTE_UNKNOWN_PREFIX] += 1
            for rank in self.request_ranks:
                self.routes_by_request[rank][ROUTE_UNKNOWN_PREFIX] += 1
            return
        self.joined += 1
        segment_ref = _text(segment_meta.get("segment_ref"), "segment_ref")
        self.segments.add(segment_ref)
        signature = segment_meta.get("similarity_signature_sha256")
        if isinstance(signature, str):
            self.similarity_signatures.add(signature)
        self.talent_statuses[
            str(segment_meta.get("talent_translation_status") or "UNKNOWN")
        ] += 1
        self.weapon_modes[str(segment_meta.get("weapon_mode") or "UNKNOWN")] += 1
        coverage = _mapping(segment_meta.get("coverage_flags"), "coverage_flags")
        self.runtime_flags[str(bool(coverage.get("runtime_executable"))).lower()] += 1
        self.representative_flags[
            str(bool(coverage.get("representative_build_eligible"))).lower()
        ] += 1
        self.development_flags[
            str(bool(coverage.get("development_build_eligible"))).lower()
        ] += 1
        self.comparison_flags[
            str(bool(coverage.get("comparison_eligible"))).lower()
        ] += 1
        if routes is None:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "joined segment is missing request routes"
            )
        route_by_rank = {
            _integer(route.get("representative_rank"), "route representative_rank"): _text(
                route.get("route"), "route"
            )
            for route in routes
        }
        if set(route_by_rank) != set(self.request_ranks):
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "segment routes do not cover all five requests"
            )
        for rank, route in route_by_rank.items():
            self.routes_by_request[rank][route] += 1
        route_values = set(route_by_rank.values())
        if ROUTE_EXACT in route_values:
            best = ROUTE_EXACT
        elif ROUTE_SIMILAR in route_values:
            best = ROUTE_SIMILAR
        elif route_values == {ROUTE_TRANSPLANT}:
            best = ROUTE_TRANSPLANT
        else:
            best = ROUTE_UNKNOWN_BUILD
        self.best_routes[best] += 1

    def wire(self) -> JSONMap:
        if self.joined + self.missing != self.decisions:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "scope joined/missing accounting does not close"
            )
        return {
            "episode_with_controllable_start_count": len(self.episodes),
            "player_wave_with_controllable_start_count": len(self.waves),
            "controllable_start_count": self.decisions,
            "joined_count": self.joined,
            "missing_count": self.missing,
            "join_rate": self.joined / self.decisions if self.decisions else None,
            "binding_run_count": self.binding_runs,
            "joined_build_change_count_within_waves": self.joined_build_changes,
            "distinct_joined_segment_count": len(self.segments),
            "distinct_known_similarity_signature_count": len(
                self.similarity_signatures
            ),
            "join_status_counts": _counter_wire(self.join_statuses),
            "action_counts": _counter_wire(self.actions),
            "build_distribution": {
                "talent_translation_status_counts": _counter_wire(
                    self.talent_statuses
                ),
                "weapon_mode_counts": _counter_wire(self.weapon_modes),
                "runtime_executable_decision_counts": _counter_wire(
                    self.runtime_flags
                ),
                "representative_build_eligible_decision_counts": _counter_wire(
                    self.representative_flags
                ),
                "development_build_eligible_decision_counts": _counter_wire(
                    self.development_flags
                ),
                "comparison_eligible_decision_counts": _counter_wire(
                    self.comparison_flags
                ),
                "per_segment_decision_counts_location": (
                    "segment_dictionary.partition[*].decision_support"
                ),
            },
            "best_available_route_counts": _route_counter_wire(self.best_routes),
            "route_counts_by_current_bloodthirst_request": {
                str(rank): _route_counter_wire(self.routes_by_request[rank])
                for rank in self.request_ranks
            },
        }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFuryDecisionBuildJoinV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryDecisionBuildJoinV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFuryDecisionBuildJoinV1Error(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} must be >= {minimum}"
        )
    return value


def _guid(value: Any, label: str) -> str:
    try:
        return canonical_guid(value, label=label).casefold()
    except Exception as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(str(error)) from error


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return raw + (b"\n" if newline else b"")


def _strict_json_bytes(raw: bytes, label: str) -> JSONMap:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> JSONMap:
        result: JSONMap = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate,
        )
    except (UnicodeError, ValueError) as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"cannot decode {label}: {error}"
        ) from error
    return deepcopy(dict(_mapping(value, label)))


def _load_json(path: Path, label: str) -> tuple[JSONMap, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"cannot read {label}: {error}"
        ) from error
    return _strict_json_bytes(raw, label), raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _counter_wire(counter: Counter[str]) -> JSONMap:
    return {key: counter[key] for key in sorted(counter)}


def _route_counter_wire(counter: Counter[str]) -> JSONMap:
    return {route: counter.get(route, 0) for route in ROUTE_REPORT_ORDER}


def _portable_projection(value: Any, *, key: str | None = None) -> Any:
    """Remove host locators while retaining the scientific value structure."""

    locator_key = key is not None and (
        key == "path"
        or key.endswith("_path")
        or key
        in {
            "catalog_path",
            "database",
            "manifest_path",
            "wowsims_root",
        }
    )
    if locator_key and isinstance(value, str):
        return "NON_IDENTITY_HOST_LOCATOR"
    if isinstance(value, Mapping):
        return {
            str(child_key): _portable_projection(child, key=str(child_key))
            for child_key, child in value.items()
            if child_key != "content_address"
        }
    if isinstance(value, list):
        return [_portable_projection(child) for child in value]
    return deepcopy(value)


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _verify_content_address(document: Mapping[str, Any], label: str) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} content-address contract differs"
        )
    observed = _text(address.get("sha256"), f"{label}.content_address.sha256")
    core = deepcopy(dict(document))
    core.pop("content_address", None)
    expected = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    if observed != expected:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} content address does not bind canonical content"
        )
    return observed


def _verify_addressed_sibling(path: Path, address: str, raw: bytes, label: str) -> Path:
    if address in path.name:
        return path
    matches = sorted(path.parent.glob(f"*.{address}.manifest.json"))
    if not matches:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} content-addressed manifest sibling is missing"
        )
    for candidate in matches:
        try:
            if candidate.read_bytes() == raw:
                return candidate
        except OSError as error:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"cannot read {label} addressed manifest: {error}"
            ) from error
    raise HistoricalFuryDecisionBuildJoinV1Error(
        f"{label} stable and content-addressed manifests differ"
    )


def _resolve_reference(reference: Any, *, base: Path, label: str) -> Path:
    raw = _text(reference, label)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if ".." in path.parts:
            raise HistoricalFuryDecisionBuildJoinV1Error(f"{label} is unsafe")
        path = base / path
    result = path.resolve()
    if not result.is_file():
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"{label} does not exist: {result}"
        )
    return result


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class _GzipJsonlWriter:
    def __init__(self, directory: Path, stem: str, schema: str) -> None:
        self.directory = directory
        self.stem = stem
        self.schema = schema
        fd, raw_name = tempfile.mkstemp(
            prefix=f".{stem}.", suffix=".jsonl.gz.tmp", dir=directory
        )
        os.close(fd)
        self.temporary = Path(raw_name)
        self.raw = self.temporary.open("wb")
        self.compressed = gzip.GzipFile(
            filename="", mode="wb", fileobj=self.raw, compresslevel=6, mtime=0
        )
        self.logical_hash = hashlib.sha256()
        self.logical_size = 0
        self.record_count = 0
        self.decision_count = 0
        self.closed = False

    def write(self, row: Mapping[str, Any], *, decision_count: int = 0) -> None:
        if self.closed:
            raise HistoricalFuryDecisionBuildJoinV1Error("writer is already closed")
        payload = _canonical_bytes(row, newline=True)
        self.compressed.write(payload)
        self.logical_hash.update(payload)
        self.logical_size += len(payload)
        self.record_count += 1
        self.decision_count += decision_count

    def finish(self) -> JSONMap:
        if self.closed:
            raise HistoricalFuryDecisionBuildJoinV1Error("writer is already closed")
        self.closed = True
        self.compressed.close()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        logical_sha = self.logical_hash.hexdigest()
        final = self.directory / f"{self.stem}.{logical_sha}.jsonl.gz"
        os.replace(self.temporary, final)
        descriptor: JSONMap = {
            "path": final.name,
            "record_schema": self.schema,
            "record_count": self.record_count,
            "logical_size_bytes": self.logical_size,
            "logical_content_sha256": logical_sha,
            "compressed_size_bytes": final.stat().st_size,
            "compressed_file_sha256": _sha256_file(final),
            "gzip_mtime": 0,
        }
        if self.decision_count:
            descriptor["controllable_start_count"] = self.decision_count
        return descriptor

    def abort(self) -> None:
        if not self.closed:
            try:
                self.compressed.close()
            finally:
                self.raw.close()
                self.temporary.unlink(missing_ok=True)
                self.closed = True


def _cohort_identity_index(cohort: Mapping[str, Any]) -> dict[tuple[str, str], tuple[str, str]]:
    try:
        cohort_v2.validate_cohort_document(cohort)
    except Exception as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"frozen cohort validation failed: {error}"
        ) from error
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for raw_candidate in _array(cohort.get("player_candidates"), "player_candidates"):
        candidate = _mapping(raw_candidate, "player candidate")
        guid = _guid(candidate.get("character_guid"), "candidate character_guid")
        server = _text(candidate.get("server"), "candidate server")
        realm = _text(candidate.get("realm"), "candidate realm")
        for raw_raid in _array(candidate.get("raids"), "candidate raids"):
            raid = _mapping(raw_raid, "candidate raid")
            instance_id = _text(raid.get("instance_id"), "candidate raid instance_id")
            key = (guid, instance_id)
            previous = result.get(key)
            if previous is not None and previous != (server, realm):
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "cohort player/instance identity maps to multiple server/realms"
                )
            result[key] = (server, realm)
    expected = _mapping(cohort.get("summary"), "cohort summary").get(
        "unique_player_raid_membership_count"
    )
    if expected is not None and _integer(
        expected, "unique_player_raid_membership_count"
    ) != len(result):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "cohort player/raid membership summary differs"
        )
    return result


def _load_episode_manifest(
    path: Path, *, cohort_sha256: str
) -> tuple[JSONMap, JSONMap]:
    manifest, raw = _load_json(path, "episode manifest")
    if (
        manifest.get("schema") != episode_v1.MANIFEST_SCHEMA
        or manifest.get("implementation_revision") != episode_v1.IMPLEMENTATION_REVISION
        or manifest.get("status") != episode_v1.STATUS
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode manifest implementation identity differs"
        )
    address = _verify_content_address(manifest, "episode manifest")
    addressed = _verify_addressed_sibling(path, address, raw, "episode manifest")
    frozen = _mapping(
        _mapping(manifest.get("input_closure"), "episode input_closure").get(
            "frozen_cohort"
        ),
        "episode frozen_cohort",
    )
    if (
        frozen.get("schema") != cohort_v2.SCHEMA
        or frozen.get("file_sha256") != cohort_sha256
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode manifest does not bind the supplied frozen cohort bytes"
        )
    boundaries = _mapping(
        manifest.get("scientific_boundaries"), "episode scientific_boundaries"
    )
    for key in (
        "comparison_authorized",
        "training_authorized",
        "closed_loop_baseline_authorized",
        "superiority_claim_authorized",
    ):
        if boundaries.get(key) is not False:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"episode manifest must keep {key}=false"
            )
    portable_contract = {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "status": manifest["status"],
        "join_contract": deepcopy(manifest.get("join_contract")),
        "action_contract": deepcopy(manifest.get("action_contract")),
        "unresolved_observations": deepcopy(
            manifest.get("unresolved_observations")
        ),
        "partitions": [
            {
                key: deepcopy(value)
                for key, value in _mapping(row, "episode partition").items()
                if key != "path"
            }
            for row in _array(manifest.get("partitions"), "episode partitions")
        ],
        "summary": deepcopy(manifest.get("summary")),
        "scientific_boundaries": deepcopy(manifest.get("scientific_boundaries")),
        "frozen_cohort_file_sha256": cohort_sha256,
    }
    return manifest, {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "portable_manifest_contract_sha256": hashlib.sha256(
            _canonical_bytes(portable_contract)
        ).hexdigest(),
        "partition_count": len(portable_contract["partitions"]),
        "stable_and_addressed_bytes_verified": addressed.is_file(),
        "declared_partition_bytes_verified_during_join": True,
    }


def _prototype_memberships(
    path: Path,
    *,
    cohort: Mapping[str, Any],
    cohort_sha256: str,
    episode_manifest: Mapping[str, Any],
    episode_manifest_path: Path,
    episode_binding: Mapping[str, Any],
) -> tuple[dict[str, set[tuple[str, str]]], JSONMap]:
    manifest, raw = _load_json(path, "prototype manifest")
    if (
        manifest.get("schema") != prototypes_v1.MANIFEST_SCHEMA
        or manifest.get("implementation_revision")
        != prototypes_v1.IMPLEMENTATION_REVISION
        or manifest.get("status") != prototypes_v1.STATUS
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "prototype manifest implementation identity differs"
        )
    address = _verify_content_address(manifest, "prototype manifest")
    addressed = _verify_addressed_sibling(path, address, raw, "prototype manifest")
    closure = _mapping(manifest.get("input_closure"), "prototype input_closure")
    frozen = _mapping(closure.get("frozen_cohort"), "prototype frozen_cohort")
    source_episode = _mapping(
        closure.get("fury_episode_manifest"), "prototype fury_episode_manifest"
    )
    if (
        frozen.get("schema") != cohort_v2.SCHEMA
        or frozen.get("file_sha256") != cohort_sha256
        or source_episode.get("schema") != episode_binding["schema"]
        or source_episode.get("content_sha256")
        != _verify_content_address(episode_manifest, "episode manifest")
        or source_episode.get("file_sha256") != _sha256_file(episode_manifest_path)
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "prototype manifest input closure differs from supplied cohort/episodes"
        )
    expected, _ = prototypes_v1.derive_prototype_memberships_v1(cohort)
    actual = _array(manifest.get("prototypes"), "prototypes")
    expected_by_id = {row["prototype_id"]: row for row in expected}
    actual_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_prototype in actual:
        prototype = _mapping(raw_prototype, "prototype")
        prototype_id = _text(prototype.get("prototype_id"), "prototype_id")
        if prototype_id in actual_by_id:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "prototype_id is duplicated"
            )
        actual_by_id[prototype_id] = prototype
    if set(actual_by_id) != set(expected_by_id) or len(actual_by_id) != 5:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "prototype manifest does not contain the rederived five memberships"
        )
    memberships: dict[str, set[tuple[str, str]]] = {}
    for prototype_id, expected_row in expected_by_id.items():
        actual_row = actual_by_id[prototype_id]
        observed_membership = {
            key: deepcopy(actual_row.get(key))
            for key in (
                "prototype_id",
                "prototype_family",
                "selection_rule",
                "member_count",
                "members",
            )
        }
        if _canonical_bytes(observed_membership) != _canonical_bytes(expected_row):
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"prototype membership differs from cohort replay: {prototype_id}"
            )
        selected: set[tuple[str, str]] = set()
        for raw_member in _array(expected_row.get("members"), "prototype members"):
            member = _mapping(raw_member, "prototype member")
            guid = _guid(member.get("character_guid"), "prototype member GUID")
            for instance_id in _array(member.get("raid_ids"), "prototype raid_ids"):
                selected.add((guid, _text(instance_id, "prototype raid id")))
        memberships[prototype_id] = selected
    portable_membership_contract = {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "status": manifest["status"],
        "selection_contract": deepcopy(manifest.get("selection_contract")),
        "prototypes": expected,
        "source_audit": deepcopy(manifest.get("source_audit")),
        "scientific_boundaries": deepcopy(manifest.get("scientific_boundaries")),
        "frozen_cohort_file_sha256": cohort_sha256,
        "fury_episode_portable_manifest_contract_sha256": episode_binding[
            "portable_manifest_contract_sha256"
        ],
    }
    return memberships, {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "portable_membership_contract_sha256": hashlib.sha256(
            _canonical_bytes(portable_membership_contract)
        ).hexdigest(),
        "prototype_count": len(expected),
        "stable_and_addressed_bytes_verified": addressed.is_file(),
        "membership_rederived_from_cohort": True,
    }


def _identity_tuple(identity: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _text(identity.get("server"), "identity.server"),
        _text(identity.get("realm"), "identity.realm"),
        _guid(identity.get("player_guid"), "identity.player_guid"),
        _text(identity.get("instance_id"), "identity.instance_id"),
        _text(identity.get("build_segment_id"), "identity.build_segment_id"),
    )


def _semantic_talent_vector(segment: Mapping[str, Any]) -> list[JSONMap] | None:
    talents = _mapping(segment.get("talents"), "segment talents")
    if talents.get("translation_status") != "TRANSLATED_EXACT":
        return None
    result: list[JSONMap] = []
    seen: set[str] = set()
    for raw in _array(talents.get("semantic_ranks"), "talents.semantic_ranks"):
        talent = _mapping(raw, "semantic talent")
        talent_id = _text(talent.get("talent_id"), "semantic talent_id")
        if talent_id in seen:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"duplicate semantic talent {talent_id}"
            )
        seen.add(talent_id)
        rank = _integer(talent.get("rank"), f"{talent_id}.rank", minimum=1)
        max_rank = _integer(
            talent.get("max_rank"), f"{talent_id}.max_rank", minimum=1
        )
        if rank > max_rank:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"semantic talent {talent_id} rank exceeds max_rank"
            )
        result.append(
            {
                "talent_id": talent_id,
                "profile_name": _text(
                    talent.get("profile_name"), f"{talent_id}.profile_name"
                ),
                "rank": rank,
                "max_rank": max_rank,
                "tree_index": _integer(
                    talent.get("tree_index"), f"{talent_id}.tree_index", minimum=0
                ),
                "position": _integer(
                    talent.get("position"), f"{talent_id}.position", minimum=0
                ),
            }
        )
    result.sort(key=lambda row: row["talent_id"])
    return result


def _weapon_mode(segment: Mapping[str, Any]) -> str | None:
    equipment = _mapping(segment.get("equipment"), "segment equipment")
    by_slot: dict[int, Mapping[str, Any]] = {}
    for raw in _array(equipment.get("slots"), "equipment.slots"):
        slot = _mapping(raw, "equipment slot")
        slot_id = _integer(slot.get("inventory_slot"), "inventory_slot", minimum=1)
        if slot_id in by_slot:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "equipment inventory slot is duplicated"
            )
        by_slot[slot_id] = slot
    main = by_slot.get(16)
    off = by_slot.get(17)
    if main is None or off is None or main.get("status") != "OBSERVED_EQUIPPED":
        return None
    main_coverage = _mapping(main.get("coverage"), "main-hand coverage")
    main_item = _mapping(main_coverage.get("item"), "main-hand item coverage")
    main_mode = main_item.get("weapon_mode")
    off_status = off.get("status")
    if main_mode == "TWO_HAND":
        return "TWO_HAND" if off_status == "OBSERVED_EMPTY" else None
    if main_mode != "ONE_HAND":
        return None
    if off_status == "OBSERVED_EMPTY":
        return "ONE_HAND_NO_OFFHAND"
    if off_status != "OBSERVED_EQUIPPED":
        return None
    off_coverage = _mapping(off.get("coverage"), "off-hand coverage")
    off_item = _mapping(off_coverage.get("item"), "off-hand item coverage")
    if off_item.get("weapon_mode") == "ONE_HAND":
        return "DUAL_WIELD"
    return "ONE_HAND_WITH_EQUIPPED_OFFHAND"


def _similarity_signature(
    *, talent_vector: list[JSONMap] | None, weapon_mode: str | None
) -> str | None:
    if talent_vector is None or weapon_mode is None:
        return None
    return hashlib.sha256(
        _canonical_bytes(
            {
                "semantic_talent_rank_vector": talent_vector,
                "weapon_mode": weapon_mode,
            }
        )
    ).hexdigest()


def _load_current_requests(
    path: Path, *, input_root: str | Path = PROJECT_ROOT
) -> tuple[tuple[_RequestRoute, ...], JSONMap]:
    document, raw = _load_json(path, "development bundle")
    try:
        checked = bundle_v1.validate_fury_build_conditioned_development_bundle_v1(
            document,
            verify_input_bytes=True,
            input_root=input_root,
        )
    except Exception as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"development bundle validation failed: {error}"
        ) from error
    if checked.get("status") != "PREPARED" or checked.get("comparison_authorized") is not False:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "development bundle must be PREPARED and not authorize comparison"
        )
    address = _text(checked.get("bundle_sha256"), "bundle_sha256")
    if address not in path.name:
        matches = sorted(path.parent.glob(f"*.{address}.json"))
        if not matches or not any(candidate.read_bytes() == raw for candidate in matches):
            raise HistoricalFuryDecisionBuildJoinV1Error(
                "development bundle stable and content-addressed files differ"
            )
    routes: list[_RequestRoute] = []
    for raw_request in _array(checked.get("requests"), "bundle requests"):
        request = _mapping(raw_request, "bundle request")
        rank = _integer(
            request.get("representative_rank"), "representative_rank", minimum=1
        )
        identity = _identity_tuple(
            _mapping(request.get("source_identity"), "request source_identity")
        )
        components = _mapping(
            request.get("five_part_components"), "five_part_components"
        )
        profile = _mapping(components.get("CharacterProfile"), "CharacterProfile")
        provenance = _mapping(profile.get("provenance"), "CharacterProfile provenance")
        exact_features = _mapping(
            provenance.get("source_exact_features"), "source_exact_features"
        )
        raw_vector = _array(
            exact_features.get("semantic_talent_rank_vector"),
            "request semantic_talent_rank_vector",
        )
        talent_vector = [deepcopy(dict(_mapping(row, "request semantic talent"))) for row in raw_vector]
        talent_vector.sort(key=lambda row: str(row.get("talent_id")))
        if not any(
            row.get("talent_id") == "warrior.bloodthirst"
            and isinstance(row.get("rank"), int)
            and row["rank"] > 0
            for row in talent_vector
        ):
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"representative rank {rank} is not a Bloodthirst build"
            )
        weapon_mode = _text(exact_features.get("weapon_mode"), "request weapon_mode")
        signature = _similarity_signature(
            talent_vector=talent_vector, weapon_mode=weapon_mode
        )
        assert signature is not None
        routes.append(
            _RequestRoute(
                representative_rank=rank,
                identity=identity,
                request_sha256=_text(request.get("request_sha256"), "request_sha256"),
                similarity_signature_sha256=signature,
                weapon_mode=weapon_mode,
            )
        )
    routes.sort(key=lambda route: route.representative_rank)
    if len(routes) != 5 or len({route.representative_rank for route in routes}) != 5:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "development bundle must contain five unique representative requests"
        )
    portable_requests = [
        {
            "representative_rank": route.representative_rank,
            "source_identity": list(route.identity),
            "request_sha256": route.request_sha256,
            "similarity_signature_sha256": route.similarity_signature_sha256,
            "weapon_mode": route.weapon_mode,
        }
        for route in routes
    ]
    return tuple(routes), {
        "schema": checked["schema"],
        "implementation_revision": checked["implementation_revision"],
        "status": checked["status"],
        "portable_request_route_binding_sha256": hashlib.sha256(
            _canonical_bytes(portable_requests)
        ).hexdigest(),
        "request_count": len(routes),
        "source_bytes_and_declared_bundle_sha256_verified": True,
    }


def request_routes_for_segment_v1(
    segment: Mapping[str, Any], requests: Sequence[_RequestRoute]
) -> tuple[list[JSONMap], JSONMap]:
    """Classify one historical segment against the five current requests."""

    identity = _identity_tuple(_mapping(segment.get("identity"), "segment identity"))
    talent_vector = _semantic_talent_vector(segment)
    weapon_mode = _weapon_mode(segment)
    signature = _similarity_signature(
        talent_vector=talent_vector, weapon_mode=weapon_mode
    )
    routes: list[JSONMap] = []
    for request in requests:
        if identity == request.identity:
            route = ROUTE_EXACT
            basis = "EXACT_SERVER_REALM_GUID_INSTANCE_BUILD_SEGMENT"
        elif signature is None:
            route = ROUTE_UNKNOWN_BUILD
            basis = "TALENT_TRANSLATION_OR_WEAPON_MODE_UNKNOWN"
        elif signature == request.similarity_signature_sha256:
            route = ROUTE_SIMILAR
            basis = "EXACT_SEMANTIC_TALENT_VECTOR_AND_WEAPON_MODE_ONLY"
        else:
            route = ROUTE_TRANSPLANT
            basis = "KNOWN_BUILD_DIFFERS_FROM_REQUEST_TALENTS_OR_WEAPON_MODE"
        routes.append(
            {
                "representative_rank": request.representative_rank,
                "route": route,
                "basis": basis,
            }
        )
    talents = _mapping(segment.get("talents"), "segment talents")
    return routes, {
        "talent_translation_status": str(
            talents.get("translation_status") or "UNKNOWN"
        ),
        "semantic_talent_rank_vector_sha256": (
            hashlib.sha256(_canonical_bytes(talent_vector)).hexdigest()
            if talent_vector is not None
            else None
        ),
        "semantic_talent_count": len(talent_vector) if talent_vector is not None else None,
        "weapon_mode": weapon_mode,
        "similarity_signature_sha256": signature,
    }


def _catalog_segment_meta(
    segment: Mapping[str, Any], requests: Sequence[_RequestRoute]
) -> tuple[str, JSONMap, list[JSONMap]]:
    source = {key: deepcopy(value) for key, value in segment.items() if key != "_catalog_line_number"}
    portable_source = _portable_projection(source)
    source_sha = hashlib.sha256(_canonical_bytes(portable_source)).hexdigest()
    segment_ref = f"sha256:{source_sha}"
    routes, similarity = request_routes_for_segment_v1(source, requests)
    identity = deepcopy(dict(_mapping(source.get("identity"), "segment identity")))
    observation = _mapping(source.get("observation"), "segment observation")
    valid_from = _mapping(observation.get("valid_from"), "segment valid_from")
    coverage = _mapping(source.get("coverage"), "segment coverage")
    equipment = _mapping(source.get("equipment"), "segment equipment")
    talents = _mapping(source.get("talents"), "segment talents")
    meta = {
        "segment_ref": segment_ref,
        "catalog_line_number": _integer(
            segment.get("_catalog_line_number"), "catalog line number", minimum=1
        ),
        "portable_source_segment_content_sha256": source_sha,
        "identity": identity,
        "valid_from": {
            "timestamp_ms": _integer(valid_from.get("timestamp_ms"), "valid_from timestamp"),
            "encounter_id": _text(valid_from.get("encounter_id"), "valid_from encounter_id"),
            "event_index": _integer(valid_from.get("event_index"), "valid_from event_index"),
            "message_ordinal": _integer(
                valid_from.get("message_ordinal"), "valid_from message_ordinal"
            ),
        },
        "equipment_content_sha256": hashlib.sha256(
            _canonical_bytes(_portable_projection(equipment))
        ).hexdigest(),
        "talents_content_sha256": hashlib.sha256(
            _canonical_bytes(_portable_projection(talents))
        ).hexdigest(),
        **similarity,
        "coverage_flags": {
            "runtime_executable": coverage.get("runtime_executable") is True,
            "representative_build_eligible": coverage.get(
                "representative_build_eligible"
            )
            is True,
            "development_build_eligible": coverage.get(
                "development_build_eligible"
            )
            is True,
            "comparison_eligible": _mapping(
                coverage.get("comparison"), "coverage.comparison"
            ).get("eligible")
            is True,
        },
        "request_routes": routes,
    }
    return segment_ref, meta, routes


def _scan_catalog(
    manifest_path: Path,
    *,
    required_memberships: set[tuple[str, str]],
) -> tuple[
    dict[tuple[str, str, str, str], list[JSONMap]],
    JSONMap,
]:
    manifest, _ = _load_json(manifest_path, "build catalog manifest")
    if (
        manifest.get("schema") != catalog_v1.SCHEMA
        or manifest.get("implementation_revision") != catalog_v1.IMPLEMENTATION_REVISION
        or manifest.get("kind") != "historical_build_catalog_manifest"
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "build catalog manifest implementation identity differs"
        )
    causal = _mapping(manifest.get("causal_contract"), "catalog causal_contract")
    if (
        causal.get("policy_join") != "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY"
        or causal.get("future_info_backfill_allowed") is not False
        or causal.get("retrospective_valid_until_is_policy_input") is not False
        or causal.get("missing_initial_state_preserved") is not True
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "build catalog causal contract differs"
        )
    catalog_path = _resolve_reference(
        manifest.get("catalog_path"), base=manifest_path.parent, label="catalog_path"
    )
    portable_logical_hash = hashlib.sha256()
    record_count = 0
    observation_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    identities: set[tuple[str, str, str, str, str]] = set()
    retained: dict[tuple[str, str, str, str], list[JSONMap]] = defaultdict(list)
    try:
        handle = gzip.open(catalog_path, "rb")
    except OSError as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"cannot open catalog partition: {error}"
        ) from error
    with handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "catalog contains a blank JSONL row"
                )
            segment = _strict_json_bytes(raw_line, f"catalog row {line_number}")
            if segment.get("schema") != catalog_v1.RECORD_SCHEMA:
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    f"catalog row {line_number} schema differs"
                )
            identity = _mapping(segment.get("identity"), "catalog identity")
            identity_key = _identity_tuple(identity)
            if identity_key in identities:
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "catalog segment identity is duplicated"
                )
            identities.add(identity_key)
            player = _mapping(segment.get("player"), "catalog player")
            hero_class = _text(player.get("hero_class"), "catalog hero_class")
            observation = _mapping(segment.get("observation"), "catalog observation")
            observation_status = _text(
                observation.get("status"), "catalog observation status"
            )
            class_counts[hero_class] += 1
            observation_counts[observation_status] += 1
            record_count += 1
            membership = (identity_key[2], identity_key[3])
            if hero_class == "WARRIOR" and membership in required_memberships:
                portable_logical_hash.update(
                    _canonical_bytes(_portable_projection(segment), newline=True)
                )
                retained_segment = deepcopy(segment)
                retained_segment["_catalog_line_number"] = line_number
                retained[identity_key[:4]].append(retained_segment)
    summary = _mapping(manifest.get("summary"), "catalog summary")
    if _integer(summary.get("build_segment_count"), "catalog build_segment_count") != record_count:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "catalog record count differs from manifest summary"
        )
    declared_observations = {
        str(key): _integer(value, f"observation count {key}")
        for key, value in _mapping(
            summary.get("observation_status_counts"), "observation_status_counts"
        ).items()
    }
    if declared_observations != dict(observation_counts):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "catalog observation status counts differ from bytes"
        )
    by_class = _mapping(summary.get("by_hero_class"), "catalog by_hero_class")
    declared_class_counts = {
        str(hero_class): _integer(
            _mapping(row, f"class {hero_class}").get("build_segment_count"),
            f"class {hero_class} build_segment_count",
        )
        for hero_class, row in by_class.items()
    }
    if declared_class_counts != dict(class_counts):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "catalog class segment counts differ from bytes"
        )
    for values in retained.values():
        values.sort(
            key=lambda segment: (
                _mapping(
                    _mapping(segment.get("observation"), "observation").get(
                        "valid_from"
                    ),
                    "valid_from",
                ).get("timestamp_ms"),
                _mapping(
                    _mapping(segment.get("observation"), "observation").get(
                        "valid_from"
                    ),
                    "valid_from",
                ).get("encounter_id"),
                _mapping(
                    _mapping(segment.get("observation"), "observation").get(
                        "valid_from"
                    ),
                    "valid_from",
                ).get("event_index"),
            )
        )
    portable_manifest_contract = {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "kind": manifest["kind"],
        "causal_contract": deepcopy(manifest.get("causal_contract")),
    }
    return dict(retained), {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "portable_manifest_contract_sha256": hashlib.sha256(
            _canonical_bytes(portable_manifest_contract)
        ).hexdigest(),
        "portable_retained_segment_content_sha256": portable_logical_hash.hexdigest(),
        "record_count": record_count,
        "retained_exact_player_instance_segment_count": sum(
            len(values) for values in retained.values()
        ),
        "manifest_and_catalog_physical_bytes_verified": True,
        "declared_record_class_and_status_counts_verified": True,
    }


def _verified_episode_rows(
    manifest_path: Path, descriptor: Mapping[str, Any]
) -> Iterator[JSONMap]:
    instance_id = _text(descriptor.get("instance_id"), "partition instance_id")
    partition_path = _resolve_reference(
        descriptor.get("path"), base=manifest_path.parent, label="episode partition path"
    )
    if descriptor.get("record_schema") != episode_v1.SCHEMA:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode partition record schema differs"
        )
    if _integer(
        descriptor.get("compressed_size_bytes"), "compressed_size_bytes", minimum=0
    ) != partition_path.stat().st_size:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode partition compressed size differs"
        )
    if _text(
        descriptor.get("compressed_file_sha256"), "compressed_file_sha256"
    ) != _sha256_file(partition_path):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode partition compressed bytes differ"
        )
    logical_hash = hashlib.sha256()
    logical_size = 0
    record_count = 0
    try:
        handle = gzip.open(partition_path, "rb")
    except OSError as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            f"cannot open episode partition: {error}"
        ) from error
    with handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "episode partition contains a blank JSONL row"
                )
            logical_hash.update(raw_line)
            logical_size += len(raw_line)
            row = _strict_json_bytes(raw_line, f"episode row {line_number}")
            if (
                row.get("schema") != episode_v1.SCHEMA
                or row.get("implementation_revision") != episode_v1.IMPLEMENTATION_REVISION
                or row.get("status") != episode_v1.STATUS
                or row.get("instance_id") != instance_id
            ):
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "episode row implementation or partition identity differs"
                )
            record_count += 1
            yield row
    expected = {
        "record_count": record_count,
        "logical_size_bytes": logical_size,
        "logical_content_sha256": logical_hash.hexdigest(),
    }
    for key, observed in expected.items():
        if descriptor.get(key) != observed:
            raise HistoricalFuryDecisionBuildJoinV1Error(
                f"episode partition {key} differs"
            )


def _decision_identity(
    episode: Mapping[str, Any], wave: Mapping[str, Any], transition: Mapping[str, Any]
) -> tuple[int, int, list[int], str]:
    observed = _mapping(transition.get("observed_event"), "observed_event")
    if (
        observed.get("phase") != "START"
        or observed.get("server_observed_start_proxy") is not True
        or observed.get("policy_decision_label") is not True
        or transition.get("feature_cutoff_is_strict_prefix") is not True
        or transition.get("current_event_present_in_state_before") is not False
        or transition.get("future_outcomes_in_state_before") is not False
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "controllable observation is not a strict-prefix START proxy"
        )
    player = _mapping(episode.get("player"), "episode player")
    if _guid(observed.get("source_guid"), "START source_guid") != _guid(
        player.get("guid"), "episode player GUID"
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "START source GUID differs from episode player"
        )
    anchor = _mapping(observed.get("anchor"), "START anchor")
    timestamp_ms = _integer(anchor.get("timestamp_ms"), "START timestamp_ms")
    event_index = _integer(anchor.get("event_index"), "START event_index")
    order_key = _array(observed.get("order_key"), "START order_key")
    if len(order_key) != 4 or any(
        isinstance(value, bool) or not isinstance(value, int) for value in order_key
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "START order_key must contain four integers"
        )
    if order_key[0] != timestamp_ms or order_key[1] != event_index:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "START anchor and order_key differ"
        )
    state = _mapping(transition.get("state_before"), "state_before")
    if (
        state.get("cutoff_semantics") != "strictly before current event order_key"
        or state.get("cutoff_exclusive_order_key") != order_key
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "START state cutoff is not the exact exclusive order key"
        )
    return timestamp_ms, event_index, [int(value) for value in order_key], _text(
        observed.get("action_key"), "START action_key"
    )


def _binding_runs(bindings: Sequence[Mapping[str, Any]]) -> tuple[list[JSONMap], int]:
    runs: list[JSONMap] = []
    joined_changes = 0
    previous_joined_ref: str | None = None
    for binding in bindings:
        state = (binding.get("join_status"), binding.get("segment_ref"))
        if not runs or (runs[-1]["join_status"], runs[-1]["segment_ref"]) != state:
            runs.append(
                {
                    "first_decision_ordinal": binding["decision_ordinal"],
                    "last_decision_ordinal": binding["decision_ordinal"],
                    "decision_count": 1,
                    "join_status": binding["join_status"],
                    "segment_ref": binding.get("segment_ref"),
                }
            )
        else:
            runs[-1]["last_decision_ordinal"] = binding["decision_ordinal"]
            runs[-1]["decision_count"] += 1
        current_ref = binding.get("segment_ref")
        if current_ref is not None:
            if previous_joined_ref is not None and previous_joined_ref != current_ref:
                joined_changes += 1
            previous_joined_ref = str(current_ref)
        else:
            previous_joined_ref = None
    return runs, joined_changes


def _distinct_segment_distribution(
    *,
    segment_meta_by_ref: Mapping[str, Mapping[str, Any]],
    segment_support: Mapping[str, Mapping[str, Counter[str] | int]],
    scope_name: str,
) -> JSONMap:
    flags: dict[str, Counter[str]] = {
        "runtime_executable": Counter(),
        "representative_build_eligible": Counter(),
        "development_build_eligible": Counter(),
        "comparison_eligible": Counter(),
    }
    talent_statuses: Counter[str] = Counter()
    weapon_modes: Counter[str] = Counter()
    selected = 0
    for segment_ref, meta in segment_meta_by_ref.items():
        support = segment_support[segment_ref]
        if scope_name == "all_source":
            count = int(support["all_source"])
        elif scope_name == "prototype_member_union":
            count = int(support["prototype_member_union"])
        else:
            by_prototype = support["by_prototype"]
            assert isinstance(by_prototype, Counter)
            count = int(by_prototype.get(scope_name, 0))
        if count <= 0:
            continue
        selected += 1
        coverage = _mapping(meta.get("coverage_flags"), "coverage_flags")
        for flag_name, counter in flags.items():
            counter[str(bool(coverage.get(flag_name))).lower()] += 1
        talent_statuses[
            str(meta.get("talent_translation_status") or "UNKNOWN")
        ] += 1
        weapon_modes[str(meta.get("weapon_mode") or "UNKNOWN")] += 1
    return {
        "distinct_segment_count": selected,
        "runtime_executable_segment_counts": _counter_wire(
            flags["runtime_executable"]
        ),
        "representative_build_eligible_segment_counts": _counter_wire(
            flags["representative_build_eligible"]
        ),
        "development_build_eligible_segment_counts": _counter_wire(
            flags["development_build_eligible"]
        ),
        "comparison_eligible_segment_counts": _counter_wire(
            flags["comparison_eligible"]
        ),
        "talent_translation_status_counts": _counter_wire(talent_statuses),
        "weapon_mode_counts": _counter_wire(weapon_modes),
    }


def build_historical_fury_decision_build_join_v1(
    *,
    cohort_path: str | Path = DEFAULT_COHORT,
    episode_manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    prototype_manifest_path: str | Path = DEFAULT_PROTOTYPE_MANIFEST,
    development_bundle_path: str | Path = DEFAULT_DEVELOPMENT_BUNDLE,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> DecisionBuildJoinResult:
    """Materialize exact causal decision/build references without running a sim."""

    root = Path(data_root).expanduser().resolve()
    cohort_file = Path(cohort_path).expanduser().resolve()
    episode_file = Path(episode_manifest_path).expanduser().resolve()
    catalog_file = Path(catalog_manifest_path).expanduser().resolve()
    prototype_file = Path(prototype_manifest_path).expanduser().resolve()
    bundle_file = Path(development_bundle_path).expanduser().resolve()
    destination = Path(output_directory).expanduser().resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "generated join output must stay under offline_data"
        ) from error
    destination.mkdir(parents=True, exist_ok=True)

    cohort, cohort_raw = _load_json(cohort_file, "frozen cohort")
    cohort_sha = hashlib.sha256(cohort_raw).hexdigest()
    identities = _cohort_identity_index(cohort)
    episode_manifest, episode_binding = _load_episode_manifest(
        episode_file, cohort_sha256=cohort_sha
    )
    prototype_memberships, prototype_binding = _prototype_memberships(
        prototype_file,
        cohort=cohort,
        cohort_sha256=cohort_sha,
        episode_manifest=episode_manifest,
        episode_manifest_path=episode_file,
        episode_binding=episode_binding,
    )
    requests, bundle_binding = _load_current_requests(
        bundle_file,
        input_root=root.parent,
    )
    request_ranks = tuple(request.representative_rank for request in requests)
    catalog_by_identity, catalog_binding = _scan_catalog(
        catalog_file, required_memberships=set(identities)
    )

    memberships_to_prototypes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for prototype_id, memberships in prototype_memberships.items():
        for membership in memberships:
            memberships_to_prototypes[membership].append(prototype_id)
    for values in memberships_to_prototypes.values():
        values.sort()

    scopes: dict[str, _ScopeStats] = {
        "all_source": _ScopeStats(request_ranks),
        "prototype_member_union": _ScopeStats(request_ranks),
        **{
            prototype_id: _ScopeStats(request_ranks)
            for prototype_id in sorted(prototype_memberships)
        },
    }
    segment_meta_by_ref: dict[str, JSONMap] = {}
    segment_projection_by_catalog_line: dict[
        int, tuple[str, JSONMap, list[JSONMap]]
    ] = {}
    segment_support: dict[str, dict[str, Counter[str] | int]] = {}
    seen_episode_ids: set[str] = set()
    mapping_partitions: list[JSONMap] = []
    total_decisions = 0
    total_episode_rows = 0

    partitions = _array(episode_manifest.get("partitions"), "episode partitions")
    for raw_descriptor in partitions:
        descriptor = _mapping(raw_descriptor, "episode partition")
        instance_id = _text(descriptor.get("instance_id"), "partition instance_id")
        writer = _GzipJsonlWriter(
            destination, f"decision-build-mapping-{instance_id}", MAPPING_SCHEMA
        )
        partition_decisions = 0
        partition_episode_rows = 0
        try:
            for episode in _verified_episode_rows(episode_file, descriptor):
                partition_episode_rows += 1
                total_episode_rows += 1
                episode_id = _text(episode.get("episode_id"), "episode_id")
                if episode_id in seen_episode_ids:
                    raise HistoricalFuryDecisionBuildJoinV1Error(
                        "episode_id is duplicated across partitions"
                    )
                seen_episode_ids.add(episode_id)
                encounter_id = _text(episode.get("encounter_id"), "encounter_id")
                player = _mapping(episode.get("player"), "episode player")
                if (
                    player.get("class") != "WARRIOR"
                    or player.get("window_level_spec") != "Fury"
                ):
                    raise HistoricalFuryDecisionBuildJoinV1Error(
                        "episode is not an exact Fury Warrior observation"
                    )
                guid = _guid(player.get("guid"), "episode player GUID")
                membership = (guid, instance_id)
                if membership not in identities:
                    raise HistoricalFuryDecisionBuildJoinV1Error(
                        "episode membership is absent from frozen cohort"
                    )
                server, realm = identities[membership]
                exact_key = (server, realm, guid, instance_id)
                segments = catalog_by_identity.get(exact_key, [])
                prototype_ids = memberships_to_prototypes.get(membership, [])
                wave_ids_seen: set[str] = set()
                for raw_wave in _array(
                    episode.get("wave_observations"), "wave_observations"
                ):
                    wave = _mapping(raw_wave, "wave observation")
                    wave_id = _text(wave.get("wave_id"), "wave_id")
                    if wave_id in wave_ids_seen:
                        raise HistoricalFuryDecisionBuildJoinV1Error(
                            "wave_id is duplicated inside episode"
                        )
                    wave_ids_seen.add(wave_id)
                    bindings: list[JSONMap] = []
                    for transition in _array(
                        wave.get("prefix_transitions"), "prefix_transitions"
                    ):
                        transition = _mapping(transition, "prefix transition")
                        observed = _mapping(
                            transition.get("observed_event"), "observed_event"
                        )
                        if observed.get("policy_decision_label") is not True:
                            continue
                        timestamp_ms, event_index, order_key, action_key = _decision_identity(
                            episode, wave, transition
                        )
                        selected = catalog_v1.select_prefix_segment(
                            segments,
                            server=server,
                            realm=realm,
                            player_guid=guid,
                            instance_id=instance_id,
                            decision_timestamp_ms=timestamp_ms,
                            encounter_id=encounter_id,
                            decision_event_index=event_index,
                        )
                        if selected is None:
                            join_status = MISSING_PREFIX if segments else MISSING_IDENTITY
                            segment_ref = None
                            segment_meta = None
                            routes = None
                        else:
                            catalog_line = _integer(
                                selected.get("_catalog_line_number"),
                                "selected catalog line number",
                                minimum=1,
                            )
                            projection = segment_projection_by_catalog_line.get(
                                catalog_line
                            )
                            if projection is None:
                                projection = _catalog_segment_meta(selected, requests)
                                segment_projection_by_catalog_line[catalog_line] = projection
                            segment_ref, segment_meta, routes = projection
                            join_status = JOINED
                            previous_meta = segment_meta_by_ref.get(segment_ref)
                            if previous_meta is not None and _canonical_bytes(
                                previous_meta
                            ) != _canonical_bytes(segment_meta):
                                raise HistoricalFuryDecisionBuildJoinV1Error(
                                    "segment reference maps to inconsistent metadata"
                                )
                            segment_meta_by_ref[segment_ref] = segment_meta
                            support = segment_support.setdefault(
                                segment_ref,
                                {
                                    "all_source": 0,
                                    "prototype_member_union": 0,
                                    "by_prototype": Counter(),
                                },
                            )
                            support["all_source"] = int(support["all_source"]) + 1
                            if prototype_ids:
                                support["prototype_member_union"] = int(
                                    support["prototype_member_union"]
                                ) + 1
                                by_prototype = support["by_prototype"]
                                assert isinstance(by_prototype, Counter)
                                for prototype_id in prototype_ids:
                                    by_prototype[prototype_id] += 1
                        binding = {
                            "decision_ordinal": len(bindings),
                            "timestamp_ms": timestamp_ms,
                            "event_index": event_index,
                            "order_key": order_key,
                            "action_key": action_key,
                            "join_status": join_status,
                            "segment_ref": segment_ref,
                        }
                        bindings.append(binding)
                        total_decisions += 1
                        partition_decisions += 1
                        scopes["all_source"].observe(
                            action_key=action_key,
                            join_status=join_status,
                            segment_meta=segment_meta,
                            routes=routes,
                        )
                        if prototype_ids:
                            scopes["prototype_member_union"].observe(
                                action_key=action_key,
                                join_status=join_status,
                                segment_meta=segment_meta,
                                routes=routes,
                            )
                            for prototype_id in prototype_ids:
                                scopes[prototype_id].observe(
                                    action_key=action_key,
                                    join_status=join_status,
                                    segment_meta=segment_meta,
                                    routes=routes,
                                )
                    if not bindings:
                        continue
                    runs, joined_changes = _binding_runs(bindings)
                    for scope_name in [
                        "all_source",
                        *(["prototype_member_union"] if prototype_ids else []),
                        *prototype_ids,
                    ]:
                        scopes[scope_name].register_wave(
                            episode_id=episode_id,
                            wave_id=wave_id,
                            binding_run_count=len(runs),
                            joined_build_change_count=joined_changes,
                        )
                    mapping = {
                        "schema": MAPPING_SCHEMA,
                        "implementation_revision": IMPLEMENTATION_REVISION,
                        "record_type": "exact_fury_wave_decision_build_mapping",
                        "source": {
                            "episode_id": episode_id,
                            "instance_id": instance_id,
                            "encounter_id": encounter_id,
                            "player_guid": guid,
                            "server": server,
                            "realm": realm,
                            "wave_id": wave_id,
                            "wave_ordinal": wave.get("wave_ordinal"),
                            "source_wave_content_sha256": wave.get(
                                "source_wave_content_sha256"
                            ),
                        },
                        "prototype_ids": list(prototype_ids),
                        "controllable_start_count": len(bindings),
                        "decision_bindings": bindings,
                        "binding_runs": runs,
                        "joined_build_change_count": joined_changes,
                        "contracts": {
                            "decision_encounter_id": (
                                "source.encounter_id applies to every decision binding"
                            ),
                            "segment_lookup": (
                                "exact server/realm/GUID/instance and latest INFO at or "
                                "before decision timestamp/encounter/event_index"
                            ),
                            "future_backfill": False,
                        },
                    }
                    writer.write(mapping, decision_count=len(bindings))
            if partition_episode_rows != _integer(
                descriptor.get("record_count"), "partition record_count", minimum=0
            ):
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "episode partition yielded a different record count"
                )
            if partition_decisions != _integer(
                descriptor.get("controllable_policy_label_count"),
                "partition controllable_policy_label_count",
                minimum=0,
            ):
                raise HistoricalFuryDecisionBuildJoinV1Error(
                    "episode partition controllable label count differs"
                )
            output_descriptor = writer.finish()
            output_descriptor["instance_id"] = instance_id
            output_descriptor["source_episode_partition"] = {
                "logical_content_sha256": descriptor.get(
                    "logical_content_sha256"
                ),
                "compressed_file_sha256": descriptor.get(
                    "compressed_file_sha256"
                ),
            }
            mapping_partitions.append(output_descriptor)
        except Exception:
            writer.abort()
            raise

    episode_summary = _mapping(episode_manifest.get("summary"), "episode summary")
    if total_decisions != _integer(
        episode_summary.get("controllable_policy_label_count"),
        "episode controllable_policy_label_count",
        minimum=0,
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "all-source controllable START accounting differs from episode manifest"
        )
    if total_episode_rows != _integer(
        episode_summary.get("candidate_nonnull_encounter_episode_count"),
        "episode count",
        minimum=0,
    ):
        raise HistoricalFuryDecisionBuildJoinV1Error(
            "episode record accounting differs from manifest"
        )

    dictionary_writer = _GzipJsonlWriter(
        destination, "selected-build-segment-dictionary", SEGMENT_DICTIONARY_SCHEMA
    )
    try:
        for segment_ref in sorted(segment_meta_by_ref):
            support = segment_support[segment_ref]
            by_prototype = support["by_prototype"]
            assert isinstance(by_prototype, Counter)
            dictionary_writer.write(
                {
                    "schema": SEGMENT_DICTIONARY_SCHEMA,
                    "implementation_revision": IMPLEMENTATION_REVISION,
                    **deepcopy(segment_meta_by_ref[segment_ref]),
                    "decision_support": {
                        "all_source": int(support["all_source"]),
                        "prototype_member_union": int(
                            support["prototype_member_union"]
                        ),
                        "by_prototype": _counter_wire(by_prototype),
                    },
                }
            )
        dictionary_partition = dictionary_writer.finish()
    except Exception:
        dictionary_writer.abort()
        raise

    cohort_binding = {
        "schema": cohort.get("schema"),
        "implementation_revision": cohort.get("implementation_revision"),
        "file_sha256": cohort_sha,
        "identity_and_membership_validation": "PASS",
        "deterministic_source_index_replay": (
            "NOT_REPEATED; episode and prototype artifacts bind these exact cohort bytes"
        ),
    }
    request_rows = [
        {
            "representative_rank": request.representative_rank,
            "source_identity": {
                "server": request.identity[0],
                "realm": request.identity[1],
                "player_guid": request.identity[2],
                "instance_id": request.identity[3],
                "build_segment_id": request.identity[4],
            },
            "request_sha256": request.request_sha256,
            "weapon_mode": request.weapon_mode,
            "similarity_signature_sha256": request.similarity_signature_sha256,
        }
        for request in requests
    ]
    statistics: JSONMap = {
        "source_episode_row_count": total_episode_rows,
        "source_episode_row_without_controllable_start_count": (
            total_episode_rows - len(scopes["all_source"].episodes)
        ),
        "all_source": scopes["all_source"].wire(),
        "prototype_member_union": scopes["prototype_member_union"].wire(),
        "by_prototype": {
            prototype_id: scopes[prototype_id].wire()
            for prototype_id in sorted(prototype_memberships)
        },
    }
    statistics["all_source"]["distinct_segment_distribution"] = (
        _distinct_segment_distribution(
            segment_meta_by_ref=segment_meta_by_ref,
            segment_support=segment_support,
            scope_name="all_source",
        )
    )
    statistics["prototype_member_union"]["distinct_segment_distribution"] = (
        _distinct_segment_distribution(
            segment_meta_by_ref=segment_meta_by_ref,
            segment_support=segment_support,
            scope_name="prototype_member_union",
        )
    )
    for prototype_id in sorted(prototype_memberships):
        statistics["by_prototype"][prototype_id][
            "distinct_segment_distribution"
        ] = _distinct_segment_distribution(
            segment_meta_by_ref=segment_meta_by_ref,
            segment_support=segment_support,
            scope_name=prototype_id,
        )
    exact_route_ranks = sorted(
        {
            _integer(route.get("representative_rank"), "route representative_rank")
            for meta in segment_meta_by_ref.values()
            for route in _array(meta.get("request_routes"), "request_routes")
            if _mapping(route, "request route").get("route") == ROUTE_EXACT
        }
    )
    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "frozen_cohort": cohort_binding,
            "fury_episode_manifest": episode_binding,
            "historical_build_catalog": catalog_binding,
            "behavior_prototype_manifest": prototype_binding,
            "current_development_bundle": bundle_binding,
            "network_request_count": 0,
            "simulator_run_count": 0,
        },
        "join_contract": {
            "identity": ["server", "realm", "player_guid", "instance_id"],
            "decision_anchor": ["timestamp_ms", "encounter_id", "event_index"],
            "selector": "historical_build_catalog_v1.select_prefix_segment",
            "policy": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY",
            "future_info_backfill_allowed": False,
            "missing_initial_build_preserved": True,
            "valid_until_used_as_policy_input": False,
            "per_decision_selector_call_count": total_decisions,
            "segment_changes_within_episode_or_wave_preserved": True,
            "mapping_representation": (
                "one compact timestamp/event/action/segment reference per controllable START; "
                "full source build objects are not copied"
            ),
        },
        "portability_contract": {
            "host_absolute_input_locators_stored": False,
            "input_resolution": "explicit producer/validator arguments only",
            "input_bytes_verified_before_portable_binding": True,
            "output_partition_paths": "basename relative to this manifest directory",
            "segment_content_binding": (
                "canonical source segment with host locator values replaced"
            ),
            "content_address_host_path_independent": True,
        },
        "route_contract": {
            "requests": request_rows,
            "exact": "same server/realm/GUID/instance/build_segment_id",
            "similar": (
                "different identity but exact semantic talent rank vector and weapon mode"
            ),
            "transplant": "known talent vector and weapon mode differ",
            "unknown": "no causal prefix build or incomplete build semantics",
            "equipment_equality_implied_by_similar": False,
            "behavior_policy_identity_implied_by_any_route": False,
            "current_request_exact_source_identity_overlap": {
                "representative_ranks": exact_route_ranks,
                "count": len(exact_route_ranks),
            },
        },
        "mapping_partitions": sorted(
            mapping_partitions, key=lambda row: str(row["instance_id"])
        ),
        "segment_dictionary": {
            "partition": dictionary_partition,
            "deduplication_key": (
                "canonical source segment after replacing host locators, SHA-256"
            ),
            "source_resolution": (
                "caller-supplied catalog matching the portable input binding"
            ),
            "lookup": (
                "catalog_line_number plus portable_source_segment_content_sha256"
            ),
        },
        "statistics": statistics,
        "scientific_boundaries": {
            "development_artifact_only": True,
            "historical_observation_only": True,
            "training_authorized": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
            "policy_quality_claim_authorized": False,
            "superiority_claim_authorized": False,
            "same_equipment_claim_authorized": False,
        },
    }
    manifest = _content_addressed(manifest_core)
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    address = _text(
        _mapping(manifest.get("content_address"), "content_address").get("sha256"),
        "content_address.sha256",
    )
    addressed = destination / (
        f"historical_fury_decision_build_join_v1.{address}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    all_stats = scopes["all_source"]
    return DecisionBuildJoinResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        decision_count=all_stats.decisions,
        joined_count=all_stats.joined,
        missing_count=all_stats.missing,
        selected_segment_count=len(segment_meta_by_ref),
        mapping_partition_count=len(mapping_partitions),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join exact Fury episode STARTs to causal historical build prefixes"
    )
    parser.add_argument("--cohort", default=str(DEFAULT_COHORT))
    parser.add_argument("--episodes", default=str(DEFAULT_EPISODE_MANIFEST))
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG_MANIFEST))
    parser.add_argument("--prototypes", default=str(DEFAULT_PROTOTYPE_MANIFEST))
    parser.add_argument("--bundle", default=str(DEFAULT_DEVELOPMENT_BUNDLE))
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_fury_decision_build_join_v1(
        cohort_path=args.cohort,
        episode_manifest_path=args.episodes,
        catalog_manifest_path=args.catalog,
        prototype_manifest_path=args.prototypes,
        development_bundle_path=args.bundle,
        output_directory=args.output_directory,
        data_root=args.data_root,
    )
    target = stdout if stdout is not None else __import__("sys").stdout
    target.write(_canonical_bytes(result.as_dict(), newline=True).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalFuryDecisionBuildJoinV1Error as error:
        print(str(error), file=__import__("sys").stderr)
        raise SystemExit(2)


__all__ = [
    "DecisionBuildJoinResult",
    "HistoricalFuryDecisionBuildJoinV1Error",
    "IMPLEMENTATION_REVISION",
    "JOINED",
    "MANIFEST_SCHEMA",
    "MAPPING_SCHEMA",
    "MISSING_IDENTITY",
    "MISSING_PREFIX",
    "ROUTE_EXACT",
    "ROUTE_SIMILAR",
    "ROUTE_TRANSPLANT",
    "ROUTE_UNKNOWN_BUILD",
    "ROUTE_UNKNOWN_PREFIX",
    "SEGMENT_DICTIONARY_SCHEMA",
    "STATUS",
    "build_historical_fury_decision_build_join_v1",
    "main",
    "request_routes_for_segment_v1",
]
