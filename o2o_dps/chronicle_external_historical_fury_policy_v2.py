"""Compile a non-promoting External-V2 historical Fury policy candidate.

This module consumes only the current ``chronicle_external_team_wave_model/v2``
artifact.  It deliberately keeps two different leakage graphs separate:

* the upstream full-roster guild/player/instance connected component is the
  only outer held-out split with future promotion authority;
* a smaller instance/Fury-player graph is evaluated as a diagnostic only.  It
  does not isolate shared guilds, team-mates, or background processes and can
  therefore never rescue a failed outer gate.

The compiler fits a compact categorical backoff model from strict-prefix
``state_before`` records.  Only direct player START or unpaired GO events are
action labels.  FAIL events and owner/controller actions remain diagnostics.
The prefix leave-one-player-out state is verified to exclude the exact focal
player (including officially owned/controller-attributed damage) before a row
can enter the model.

Building and internally evaluating this artifact never makes it a voting
baseline.  This revision has no independently verified full-scenario runtime
adapter, ordered execution-fidelity artifact, or paired-rollout identity
artifact, so it emits a typed blocked admission manifest and refuses to mint
the receipt accepted by ``fury_paired_multiseed_runner_v3``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, BinaryIO

from . import chronicle_external_team_wave_model_v2 as team_model_v2


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy/v2"
SCHEMA_VERSION = 2
IMPLEMENTATION_REVISION = (
    "v2.0_external84_outer_component_focal_diagnostic_nonpromoting"
)
MODEL_KIND = "chronicle_external_historical_fury_policy_model"
EVALUATION_KIND = "chronicle_external_historical_fury_policy_evaluation"
PREFIX_RECEIPT_KIND = "chronicle_external_historical_fury_prefix_receipt"
ADMISSION_KIND = "chronicle_external_historical_fury_admission_manifest"

POLICY_ID = "chronicle.external_v2.fury.historical_player_policy"
RUNNER_RECEIPT_SCHEMA = "fury_historical_external_v2_artifact_admission_receipt/v1"
RUNNER_REQUIRED_CONDITIONS = (
    "external_v2_candidate_mask_exactly_bound",
    "prefix_causality_verified",
    "policy_model_content_addressed",
    "full_scenario_adapter_verified",
    "ordered_execution_fidelity_closed",
    "paired_rollout_identity_closed",
)

FURY_LANE = "WARRIOR_FURY"
ARMS_LANE = "WARRIOR_ARMS"
LANES = (FURY_LANE, ARMS_LANE)
CONTEXT_LEVELS = ("coarse", "tactical", "full")
START_GO_PAIR_MAX_MS = 10_000
DEFAULT_FOLD_COUNT = 5
DEFAULT_SPLIT_SEED = 20260911
DEFAULT_SMOOTHING_ALPHA = 0.5
DEFAULT_BACKOFF_STRENGTH = 8.0

CURRENT_EXPECTED_DESCRIPTIVE_INSTANCES = 84
CURRENT_EXPECTED_TRAINING_INSTANCES = 68
CURRENT_EXPECTED_NONTRAINING_INSTANCES = 16

OUTER_FIDELITY_THRESHOLDS: Mapping[str, int | float] = {
    "minimum_independent_components": 20,
    "minimum_heldout_decisions": 1000,
    "minimum_known_action_coverage": 0.90,
    "minimum_top1_accuracy": 0.45,
    "minimum_top3_accuracy": 0.80,
    "minimum_contextual_log_loss_improvement": 0.0,
    "maximum_expected_calibration_error": 0.20,
}

STATUS_NONVOTING = "MODEL_FIT_DESCRIPTIVE_NONVOTING"
STATUS_EVALUATION = "HELDOUT_DIAGNOSTIC_NONVOTING"
STATUS_BLOCKED = "BLOCKED_NOT_COMPARISON_READY"
UNKNOWN_ACTION_KEY = "__HELDOUT_UNKNOWN_ACTION__"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_wave_model"
    / "v2"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "behavior_models"
    / "chronicle_external_historical_fury_policy"
    / "v2"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PREFIX_KEYS = frozenset(
    {
        "death_clock",
        "death_markers",
        "descriptive_outcome",
        "event_accounting",
        "final_totals",
        "future_events",
        "future_teammate_actions",
        "next_action",
        "next_event",
        "reconstruction_binding",
        "remaining_wave_ms",
        "summary",
        "target_summaries",
        "wave_duration_ms",
        "wave_end_ms",
        "wave_summary",
    }
)


class ExternalHistoricalFuryPolicyV2Error(RuntimeError):
    """A source, artifact, or admission claim violates the V2 contract."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        self.code = code
        self.details = deepcopy(dict(details or {}))
        super().__init__(f"{code}: {message}")


@dataclass
class _Aggregate:
    decision_cells: Counter[tuple[str, str, str, str]] = field(
        default_factory=Counter
    )
    action_counts: Counter[str] = field(default_factory=Counter)
    context_counts: dict[str, dict[str, Counter[str]]] = field(
        default_factory=lambda: {
            level: defaultdict(Counter) for level in CONTEXT_LEVELS
        }
    )
    episode_count: int = 0
    player_nodes: set[str] = field(default_factory=set)
    instance_nodes: set[str] = field(default_factory=set)

    @property
    def decision_count(self) -> int:
        return sum(self.action_counts.values())

    def observe_episode(self, *, player_node: str, instance_node: str) -> None:
        self.episode_count += 1
        self.player_nodes.add(player_node)
        self.instance_nodes.add(instance_node)

    def add(self, contexts: Mapping[str, str], action_key: str) -> None:
        keys = tuple(contexts[level] for level in CONTEXT_LEVELS)
        self.decision_cells[(*keys, action_key)] += 1
        self.action_counts[action_key] += 1
        for level, context_key in zip(CONTEXT_LEVELS, keys, strict=True):
            self.context_counts[level][context_key][action_key] += 1

    def merge(self, other: "_Aggregate") -> None:
        self.decision_cells.update(other.decision_cells)
        self.action_counts.update(other.action_counts)
        for level in CONTEXT_LEVELS:
            for context_key, counts in other.context_counts[level].items():
                self.context_counts[level][context_key].update(counts)
        self.episode_count += other.episode_count
        self.player_nodes.update(other.player_nodes)
        self.instance_nodes.update(other.instance_nodes)


@dataclass(frozen=True)
class HistoricalFuryPolicyV2Result:
    model_path: Path
    model_content_addressed_path: Path
    evaluation_path: Path
    evaluation_content_addressed_path: Path
    prefix_receipt_path: Path
    prefix_receipt_content_addressed_path: Path
    admission_path: Path
    admission_content_addressed_path: Path
    admission_status: str
    runner_receipt_emitted: bool
    blockers: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> JSONMap:
        return {
            "status": "ok",
            "schema": SCHEMA,
            "model_path": str(self.model_path),
            "model_content_addressed_path": str(
                self.model_content_addressed_path
            ),
            "evaluation_path": str(self.evaluation_path),
            "evaluation_content_addressed_path": str(
                self.evaluation_content_addressed_path
            ),
            "prefix_receipt_path": str(self.prefix_receipt_path),
            "prefix_receipt_content_addressed_path": str(
                self.prefix_receipt_content_addressed_path
            ),
            "admission_path": str(self.admission_path),
            "admission_content_addressed_path": str(
                self.admission_content_addressed_path
            ),
            "admission_status": self.admission_status,
            "runner_receipt_emitted": self.runner_receipt_emitted,
            "blockers": [deepcopy(dict(value)) for value in self.blockers],
        }


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
        raise ExternalHistoricalFuryPolicyV2Error(
            "NON_CANONICAL_JSON", f"value is not strict canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise ExternalHistoricalFuryPolicyV2Error(
            "FILE_HASH_FAILED", f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be an integer"
        )
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    result = _integer(value, label)
    if result < 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be nonnegative"
        )
    return result


def _finite_nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be numeric"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be finite and nonnegative"
        )
    return result


def _sha(value: Any, label: str) -> str:
    rendered = _text(value, label)
    if _SHA256_RE.fullmatch(rendered) is None:
        raise ExternalHistoricalFuryPolicyV2Error(
            "SCHEMA_MISMATCH", f"{label} must be a lowercase SHA-256"
        )
    return rendered


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": _sha256_json(core),
        },
    }


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    address = _mapping(value.get("content_address"), f"{label}.content_address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "CONTENT_ADDRESS_CONTRACT_MISMATCH",
            f"{label} has an unsupported content-address contract",
        )
    expected = _sha(address.get("sha256"), f"{label}.content_address.sha256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    actual = _sha256_json(core)
    if expected != actual:
        raise ExternalHistoricalFuryPolicyV2Error(
            "CONTENT_ADDRESS_MISMATCH", f"{label} content address differs"
        )
    return actual


def _assert_no_future_keys(value: Any, path: str = "state_before") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered = str(key)
            if rendered in _FORBIDDEN_PREFIX_KEYS:
                raise ExternalHistoricalFuryPolicyV2Error(
                    "FUTURE_FEATURE_DETECTED",
                    f"forbidden future/outcome key at {path}.{rendered}",
                )
            _assert_no_future_keys(child, f"{path}.{rendered}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_future_keys(child, f"{path}[{index}]")


def _data_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name == "derived":
            return parent.parent
    raise ExternalHistoricalFuryPolicyV2Error(
        "INPUT_LOCATION_INVALID",
        "team-wave manifest must be below offline_data/derived",
    )


def _under(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ExternalHistoricalFuryPolicyV2Error(
            "PATH_ESCAPE", f"{label} must stay below the input offline_data root"
        )
    return resolved


def _resolve_partition(manifest_path: Path, raw: Any, data_root: Path) -> Path:
    relative = Path(_text(raw, "partition.path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ExternalHistoricalFuryPolicyV2Error(
            "PATH_ESCAPE", "partition path must be safe and relative"
        )
    path = _under(manifest_path.parent / relative, data_root, "partition")
    if not path.is_file() or path.is_symlink():
        raise ExternalHistoricalFuryPolicyV2Error(
            "PARTITION_MISSING", f"partition is not a regular file: {path}"
        )
    return path


def _load_current_input(path_value: str | Path) -> tuple[JSONMap, Path, JSONMap]:
    requested = Path(path_value).expanduser().resolve()
    try:
        manifest, stable = team_model_v2.load_external_team_wave_model_manifest(
            requested
        )
    except Exception as error:
        raise ExternalHistoricalFuryPolicyV2Error(
            "TEAM_WAVE_MODEL_VALIDATION_FAILED", str(error)
        ) from error
    if (
        manifest.get("schema") != team_model_v2.SCHEMA
        or manifest.get("implementation_revision")
        != team_model_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != team_model_v2.STATUS
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "STALE_TEAM_WAVE_MODEL", "input is not the current External-V2 model"
        )
    content_sha = _verify_content_address(manifest, "team-wave model manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    if stable.read_bytes() != payload:
        raise ExternalHistoricalFuryPolicyV2Error(
            "TEAM_WAVE_MODEL_BYTES_CHANGED",
            "validated team-wave stable manifest changed or is noncanonical",
        )
    summary = _mapping(manifest.get("summary"), "team-wave model summary")
    receipt = _mapping(
        _mapping(manifest.get("input_closure"), "input_closure").get(
            "cohort_receipt"
        ),
        "input_closure.cohort_receipt",
    )
    source_binding = {
        "schema": team_model_v2.SCHEMA,
        "implementation_revision": team_model_v2.IMPLEMENTATION_REVISION,
        "manifest_content_sha256": content_sha,
        "manifest_file_sha256": _sha256_bytes(payload),
        "manifest_size_bytes": len(payload),
        "cohort_receipt_content_sha256": _sha(
            receipt.get("content_sha256"), "cohort receipt content SHA"
        ),
        "cohort_receipt_file_sha256": _sha(
            receipt.get("file_sha256"), "cohort receipt file SHA"
        ),
        "raw_union_file_sha256": _sha(
            receipt.get("raw_union_file_sha256"), "raw union file SHA"
        ),
        "descriptive_instance_count": _nonnegative_integer(
            summary.get("instance_count"), "summary.instance_count"
        ),
        "training_instance_count": _nonnegative_integer(
            summary.get("training_candidate_instance_count"),
            "summary.training_candidate_instance_count",
        ),
        "descriptive_nontraining_instance_count": _nonnegative_integer(
            summary.get("descriptive_nontraining_instance_count"),
            "summary.descriptive_nontraining_instance_count",
        ),
        "network_requests_made": 0,
    }
    if (
        source_binding["training_instance_count"]
        + source_binding["descriptive_nontraining_instance_count"]
        != source_binding["descriptive_instance_count"]
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "COHORT_COUNT_MISMATCH",
            "training and nontraining counts do not partition descriptive instances",
        )
    source_binding["source_bundle_sha256"] = _sha256_json(source_binding)
    return deepcopy(dict(manifest)), stable, source_binding


def _load_published_input_shallow(
    path_value: str | Path,
) -> tuple[JSONMap, Path, JSONMap]:
    """Bind a committed Stage-5 publication without reopening its partitions.

    Stage-5 workers already validate every partition before publishing the
    addressed manifest and commit the byte-identical stable manifest last.  An
    HPC policy worker therefore binds that publication pair and validates the
    small manifest/entry contracts here; its selected partition is validated
    exactly once later by ``_scan_training_evidence``.
    """

    requested = Path(path_value).expanduser().resolve()
    try:
        payload = requested.read_bytes()
        manifest = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExternalHistoricalFuryPolicyV2Error(
            "TEAM_WAVE_MODEL_MANIFEST_READ_FAILED", str(error)
        ) from error
    manifest_map = _mapping(manifest, "team-wave model manifest")
    if (
        manifest_map.get("schema") != team_model_v2.SCHEMA
        or manifest_map.get("kind") != team_model_v2.KIND
        or manifest_map.get("implementation_revision")
        != team_model_v2.IMPLEMENTATION_REVISION
        or manifest_map.get("status") != team_model_v2.STATUS
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "STALE_TEAM_WAVE_MODEL", "input is not the current External-V2 model"
        )
    content_sha = _verify_content_address(manifest_map, "team-wave model manifest")
    canonical = _canonical_bytes(manifest_map) + b"\n"
    if payload != canonical:
        raise ExternalHistoricalFuryPolicyV2Error(
            "TEAM_WAVE_MODEL_BYTES_CHANGED", "team-wave manifest is noncanonical"
        )
    stable = requested.parent / "manifest.json"
    addressed = requested.parent / (
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    for candidate, label in ((stable, "stable"), (addressed, "addressed")):
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or candidate.read_bytes() != canonical
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TEAM_WAVE_MODEL_PUBLICATION_INCOMPLETE",
                f"team-wave {label} manifest is missing or differs",
            )

    order = _array(manifest_map.get("instance_order"), "instance_order")
    entries = _array(manifest_map.get("instances"), "instances")
    if len(order) != len(entries) or not entries:
        raise ExternalHistoricalFuryPolicyV2Error(
            "INSTANCE_SET_MISMATCH", "instance order and entries differ"
        )
    observed: list[str] = []
    for position, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"instances[{position}]")
        _verify_content_address(entry, f"instances[{position}]")
        instance_id = _text(entry.get("instance_id"), "instance_id")
        if order[position] != instance_id:
            raise ExternalHistoricalFuryPolicyV2Error(
                "INSTANCE_ORDER_MISMATCH", "instance order is not deterministic"
            )
        observed.append(instance_id)
    if len(set(observed)) != len(observed):
        raise ExternalHistoricalFuryPolicyV2Error(
            "INSTANCE_SET_MISMATCH", "instance IDs are not unique"
        )
    if manifest_map.get("summary") != team_model_v2._manifest_summary(entries):
        raise ExternalHistoricalFuryPolicyV2Error(
            "TEAM_WAVE_MODEL_SUMMARY_MISMATCH",
            "manifest summary differs from its published entries",
        )
    _outer_component_index(manifest_map)

    summary = _mapping(manifest_map.get("summary"), "team-wave model summary")
    receipt = _mapping(
        _mapping(manifest_map.get("input_closure"), "input_closure").get(
            "cohort_receipt"
        ),
        "input_closure.cohort_receipt",
    )
    source_binding = {
        "schema": team_model_v2.SCHEMA,
        "implementation_revision": team_model_v2.IMPLEMENTATION_REVISION,
        "manifest_content_sha256": content_sha,
        "manifest_file_sha256": _sha256_bytes(canonical),
        "manifest_size_bytes": len(canonical),
        "cohort_receipt_content_sha256": _sha(
            receipt.get("content_sha256"), "cohort receipt content SHA"
        ),
        "cohort_receipt_file_sha256": _sha(
            receipt.get("file_sha256"), "cohort receipt file SHA"
        ),
        "raw_union_file_sha256": _sha(
            receipt.get("raw_union_file_sha256"), "raw union file SHA"
        ),
        "descriptive_instance_count": _nonnegative_integer(
            summary.get("instance_count"), "summary.instance_count"
        ),
        "training_instance_count": _nonnegative_integer(
            summary.get("training_candidate_instance_count"),
            "summary.training_candidate_instance_count",
        ),
        "descriptive_nontraining_instance_count": _nonnegative_integer(
            summary.get("descriptive_nontraining_instance_count"),
            "summary.descriptive_nontraining_instance_count",
        ),
        "network_requests_made": 0,
    }
    if (
        source_binding["training_instance_count"]
        + source_binding["descriptive_nontraining_instance_count"]
        != source_binding["descriptive_instance_count"]
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "COHORT_COUNT_MISMATCH",
            "training and nontraining counts do not partition descriptive instances",
        )
    source_binding["source_bundle_sha256"] = _sha256_json(source_binding)
    return deepcopy(dict(manifest_map)), stable, source_binding


def _outer_component_index(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, str], tuple[str, ...]]:
    graph = _mapping(manifest.get("split_graph"), "split_graph")
    if (
        graph.get("required_split_unit") != "connected component"
        or graph.get("row_random_split_allowed") is not False
        or graph.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "OUTER_COMPONENT_CONTRACT_WEAKENED",
            "upstream full-roster component isolation is not intact",
        )
    index: dict[str, str] = {}
    for position, raw in enumerate(
        _array(graph.get("node_to_component"), "split_graph.node_to_component")
    ):
        row = _mapping(raw, f"node_to_component[{position}]")
        node = _text(row.get("node_id"), "node_id")
        component = _sha(row.get("component_id"), "component_id")
        previous = index.setdefault(node, component)
        if previous != component:
            raise ExternalHistoricalFuryPolicyV2Error(
                "OUTER_COMPONENT_AMBIGUOUS",
                f"node {node} maps to two outer components",
            )
    components: list[str] = []
    for position, raw in enumerate(
        _array(graph.get("connected_components"), "connected_components")
    ):
        row = _mapping(raw, f"connected_components[{position}]")
        component = _sha(row.get("component_id"), "component_id")
        nodes = [_text(value, "component node") for value in _array(row.get("node_ids"), "node_ids")]
        if component in components or any(index.get(node) != component for node in nodes):
            raise ExternalHistoricalFuryPolicyV2Error(
                "OUTER_COMPONENT_INDEX_INVALID",
                "connected-component rows disagree with node mapping",
            )
        components.append(component)
    if not components or set(index.values()) != set(components):
        raise ExternalHistoricalFuryPolicyV2Error(
            "OUTER_COMPONENT_INDEX_INCOMPLETE",
            "full-roster component index is incomplete",
        )
    return index, tuple(sorted(components))


def _episode_nodes_and_outer_component(
    player: Mapping[str, Any], node_index: Mapping[str, str]
) -> tuple[str, str, str]:
    membership = _mapping(player.get("component_membership"), "component_membership")
    if (
        membership.get("row_random_split_allowed") is not False
        or membership.get("required_split_unit")
        != "connected component of instance, guild, and player nodes"
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "EPISODE_COMPONENT_CONTRACT_WEAKENED",
            "player episode does not preserve full-roster component isolation",
        )
    instance_node = _text(membership.get("instance_node_id"), "instance_node_id")
    player_node = _text(membership.get("player_node_id"), "player_node_id")
    guild_nodes = [
        _text(value, "guild_node_id")
        for value in _array(membership.get("guild_node_ids"), "guild_node_ids")
    ]
    components = {node_index.get(node) for node in (instance_node, player_node, *guild_nodes)}
    if None in components or len(components) != 1:
        raise ExternalHistoricalFuryPolicyV2Error(
            "EPISODE_CROSSES_OUTER_COMPONENTS",
            "player episode nodes do not share exactly one outer component",
        )
    return instance_node, player_node, str(next(iter(components)))


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, value: str) -> str:
        parent = self.parent.setdefault(value, value)
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _log_bucket(value: float) -> str:
    if value <= 0:
        return "0"
    return str(min(20, int(math.log2(value)) + 1))


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2_3"
    if value <= 7:
        return "4_7"
    if value <= 15:
        return "8_15"
    return "16_plus"


def _elapsed_bucket(value_ms: int) -> str:
    if value_ms < 2_000:
        return "0_2s"
    if value_ms < 5_000:
        return "2_5s"
    if value_ms < 10_000:
        return "5_10s"
    if value_ms < 20_000:
        return "10_20s"
    return "20s_plus"


def _spell_identity(spell_value: Any) -> JSONMap:
    spell = _mapping(spell_value, "spell")
    spell_id = spell.get("id")
    if isinstance(spell_id, int) and not isinstance(spell_id, bool) and spell_id > 0:
        return {"spell_id": spell_id}
    name = spell.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ExternalHistoricalFuryPolicyV2Error(
            "ACTION_IDENTITY_MISSING", "action spell has neither positive ID nor name"
        )
    return {"spell_name": name.strip().casefold()}


def _target_role(
    *, label: Mapping[str, Any], state: Mapping[str, Any], focal_guid: str
) -> tuple[str, bool]:
    target = _mapping(label.get("target"), "current_event_label.target")
    target_guid = target.get("guid")
    lane = target.get("lane")
    if target_guid is None:
        return "NO_EXPLICIT_TARGET", True
    if target_guid == focal_guid:
        return "SELF", True
    if target.get("voting_enemy_target") is True and lane == "HOSTILE_CREATURE":
        if state.get("actor_last_observed_target_guid") == target_guid:
            return "CURRENT_ENEMY", True
        return "OTHER_OR_NEW_ENEMY", True
    if lane == "FRIENDLY_PLAYER":
        return "OTHER_FRIENDLY", True
    return "UNSUPPORTED_NONVOTING_TARGET", False


def _last_prefix_action_spell(
    *,
    trace: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    focal_guid: str,
) -> str:
    recent = _array(
        state.get("actor_recent_exact_trace_indices"),
        "actor_recent_exact_trace_indices",
    )
    cutoff = _nonnegative_integer(
        state.get("prefix_trace_exclusive_index"), "prefix_trace_exclusive_index"
    )
    for raw_index in reversed(recent):
        index = _nonnegative_integer(raw_index, "actor recent trace index")
        if index >= cutoff or index >= len(trace):
            raise ExternalHistoricalFuryPolicyV2Error(
                "NON_PREFIX_ACTOR_REFERENCE",
                "actor recent trace reference is not strictly before the label",
            )
        row = _mapping(trace[index], "actor recent trace row")
        if row.get("trace_kind") != "EXACT_PLAYER_EVENT" or row.get("player_guid") != focal_guid:
            raise ExternalHistoricalFuryPolicyV2Error(
                "ACTOR_REFERENCE_MISMATCH",
                "actor recent trace reference crosses actor lanes",
            )
        event = _mapping(row.get("event"), "actor recent event")
        attribution = _mapping(event.get("attribution"), "actor recent attribution")
        if (
            event.get("event_type") in {"START", "GO"}
            and attribution.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
        ):
            return _canonical_bytes(_spell_identity(event.get("spell"))).decode("utf-8")
    return "NONE"


def _contexts_from_prefix(
    *,
    state: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    focal_guid: str,
) -> dict[str, str]:
    _assert_no_future_keys(state)
    if state.get("cutoff_semantics") != "strictly before current exact EventMeta order_key":
        raise ExternalHistoricalFuryPolicyV2Error(
            "PREFIX_SEMANTICS_WEAKENED", "state cutoff semantics are not strict"
        )
    loo = _mapping(
        state.get("leave_one_player_out_background_before"),
        "leave_one_player_out_background_before",
    )
    if (
        loo.get("focal_player_guid") != focal_guid
        or loo.get("name_or_guid_suffix_inference_used") is not False
        or loo.get("unattributed_retained_as_explicit_unknown") is not True
        or loo.get("filter_contract")
        != (
            "exclude strict-prefix direct/official-owner/official-controller "
            "DMG attributed to the exact focal player GUID"
        )
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "LEAVE_PLAYER_OUT_CONTRACT_MISMATCH",
            "prefix background is not exact-GUID direct+owned/controller leave-out",
        )
    included = _nonnegative_integer(
        loo.get("included_background_damage"), "included_background_damage"
    )
    explicit = _nonnegative_integer(
        loo.get("included_explicit_other_player_damage"),
        "included_explicit_other_player_damage",
    )
    unattributed = _nonnegative_integer(
        loo.get("included_unattributed_damage"), "included_unattributed_damage"
    )
    if included != explicit + unattributed:
        raise ExternalHistoricalFuryPolicyV2Error(
            "LEAVE_PLAYER_OUT_ACCOUNTING_MISMATCH",
            "prefix background damage does not equal explicit-other plus unattributed",
        )
    dps_raw = loo.get("prefix_elapsed_average_background_dps")
    background_dps = 0.0 if dps_raw is None else _finite_nonnegative_number(
        dps_raw, "prefix_elapsed_average_background_dps"
    )
    target_ref = _mapping(
        state.get("observed_target_prefix_state_ref"),
        "observed_target_prefix_state_ref",
    )
    _sha(target_ref.get("content_sha256"), "target prefix state SHA")
    target_count = _nonnegative_integer(
        target_ref.get("observed_target_count"), "observed_target_count"
    )
    dead_count = _nonnegative_integer(
        target_ref.get("observed_dead_target_count"), "observed_dead_target_count"
    )
    if dead_count > target_count:
        raise ExternalHistoricalFuryPolicyV2Error(
            "PREFIX_TARGET_ACCOUNTING_MISMATCH",
            "observed dead target count exceeds observed target count",
        )
    elapsed = _nonnegative_integer(state.get("wave_elapsed_ms"), "wave_elapsed_ms")
    prefix_events = _nonnegative_integer(
        state.get("prefix_player_or_unattributed_event_count"),
        "prefix_player_or_unattributed_event_count",
    )
    prefix_damage = _nonnegative_integer(
        state.get("prefix_damage_amount_total"), "prefix_damage_amount_total"
    )
    coarse = {
        "wave_elapsed_bucket": _elapsed_bucket(elapsed),
        "observed_target_count_bucket": _count_bucket(target_count),
        "observed_dead_target_count_bucket": _count_bucket(dead_count),
    }
    tactical = {
        **coarse,
        "background_dps_bucket": _log_bucket(background_dps),
        "actor_has_last_target": state.get("actor_last_observed_target_guid") is not None,
        "last_prefix_action_spell": _last_prefix_action_spell(
            trace=trace, state=state, focal_guid=focal_guid
        ),
    }
    full = {
        **tactical,
        "prefix_event_count_bucket": _count_bucket(prefix_events),
        "prefix_damage_bucket": _log_bucket(float(prefix_damage)),
        "background_damage_bucket": _log_bucket(float(included)),
    }
    return {
        "coarse": _canonical_bytes(coarse).decode("utf-8"),
        "tactical": _canonical_bytes(tactical).decode("utf-8"),
        "full": _canonical_bytes(full).decode("utf-8"),
    }


def _action_key(
    *, label: Mapping[str, Any], state: Mapping[str, Any], focal_guid: str
) -> tuple[str, bool]:
    target_role, usable = _target_role(label=label, state=state, focal_guid=focal_guid)
    identity = {
        **_spell_identity(label.get("spell")),
        "target_role": target_role,
    }
    return _canonical_bytes(identity).decode("utf-8"), usable


def _transition_identity(label: Mapping[str, Any]) -> tuple[str, str]:
    spell = _canonical_bytes(_spell_identity(label.get("spell"))).decode("utf-8")
    target = _mapping(label.get("target"), "current_event_label.target")
    guid = target.get("guid")
    return spell, guid if isinstance(guid, str) else ""


def _process_player_transitions(
    *,
    player: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    aggregate: _Aggregate,
    audit: Counter[str],
    logical_digest: "hashlib._Hash",
    accepted_decision_writer: BinaryIO | None,
    instance_id: str,
    wave_id: str,
    outer_component_id: str,
    player_node: str,
) -> None:
    metadata = _mapping(player.get("player"), "player metadata")
    focal_guid = _text(metadata.get("guid"), "player guid")
    pending_starts: dict[tuple[str, str], int] = {}
    prior_order: tuple[int, ...] | None = None
    transitions = _array(player.get("prefix_transitions"), "prefix_transitions")
    for position, raw_transition in enumerate(transitions):
        transition = _mapping(raw_transition, f"prefix_transitions[{position}]")
        if (
            transition.get("feature_cutoff_is_strict_prefix") is not True
            or transition.get("current_event_present_in_state_before") is not False
            or transition.get("future_outcomes_in_state_before") is not False
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TRANSITION_NOT_STRICT_PREFIX",
                "training transition weakened its strict-prefix boundary",
            )
        trace_index = _nonnegative_integer(transition.get("trace_index"), "trace_index")
        order_values = _array(transition.get("order_key"), "order_key")
        order = tuple(_nonnegative_integer(value, "order component") for value in order_values)
        if len(order) != 5 or (prior_order is not None and order <= prior_order):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TRANSITION_ORDER_INVALID", "player transitions are not strictly ordered"
            )
        prior_order = order
        if trace_index >= len(trace):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TRANSITION_TRACE_REFERENCE_INVALID", "transition trace index is out of range"
            )
        source = _mapping(trace[trace_index], "transition trace source")
        if (
            source.get("trace_kind") != "EXACT_PLAYER_EVENT"
            or source.get("player_guid") != focal_guid
            or source.get("order_key") != list(order)
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TRANSITION_TRACE_REFERENCE_MISMATCH",
                "transition does not point to the exact focal actor trace row",
            )
        state = _mapping(transition.get("state_before"), "state_before")
        if (
            state.get("prefix_trace_exclusive_index") != trace_index
            or state.get("prefix_trace_event_count") != trace_index
            or state.get("cutoff_exclusive_order_key") != list(order)
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "TRANSITION_PREFIX_INDEX_MISMATCH",
                "state_before does not end exactly before the current trace row",
            )
        label = _mapping(transition.get("current_event_label"), "current_event_label")
        audit["transitions_inspected"] += 1
        event_type = _text(label.get("event_type"), "action event_type")
        if event_type == "FAIL":
            pending_starts.pop(_transition_identity(label), None)
            audit["failed_actions_excluded"] += 1
            continue
        if event_type not in {"START", "GO"}:
            raise ExternalHistoricalFuryPolicyV2Error(
                "UNSUPPORTED_ACTION_EVENT", f"unsupported action event {event_type}"
            )
        if (
            label.get("attribution_kind") != "DIRECT_FRIENDLY_PLAYER"
            or label.get("source_lane") != "FRIENDLY_PLAYER"
        ):
            audit["owned_or_controller_actions_excluded"] += 1
            continue
        identity = _transition_identity(label)
        elapsed = _nonnegative_integer(state.get("wave_elapsed_ms"), "wave_elapsed_ms")
        pending_starts = {
            key: start
            for key, start in pending_starts.items()
            if elapsed - start <= START_GO_PAIR_MAX_MS
        }
        if event_type == "GO" and identity in pending_starts:
            start = pending_starts.pop(identity)
            if 0 <= elapsed - start <= START_GO_PAIR_MAX_MS:
                audit["paired_go_deduplicated"] += 1
                continue
        contexts = _contexts_from_prefix(
            state=state, trace=trace, focal_guid=focal_guid
        )
        action_key, usable = _action_key(
            label=label, state=state, focal_guid=focal_guid
        )
        if not usable:
            audit["unsupported_nonvoting_targets_excluded"] += 1
            continue
        if event_type == "START":
            pending_starts[identity] = elapsed
            audit["direct_start_labels"] += 1
        else:
            audit["direct_unpaired_go_labels"] += 1
        aggregate.add(contexts, action_key)
        audit["accepted_action_labels"] += 1
        logical_row = _canonical_bytes(
            {
                "instance_id": instance_id,
                "wave_id": wave_id,
                "outer_component_id": outer_component_id,
                "player_node_id": player_node,
                "trace_index": trace_index,
                "contexts": contexts,
                "action_key": action_key,
            }
        ) + b"\n"
        logical_digest.update(logical_row)
        if accepted_decision_writer is not None:
            accepted_decision_writer.write(logical_row)


def _focal_component_index(
    units: Mapping[tuple[str, str], "_Aggregate"],
) -> dict[str, str]:
    graph = _DisjointSet()
    for instance_node, player_node in units:
        graph.union(instance_node, player_node)
    nodes_by_root: dict[str, list[str]] = defaultdict(list)
    for node in sorted(graph.parent):
        nodes_by_root[graph.find(node)].append(node)
    result: dict[str, str] = {}
    for nodes in nodes_by_root.values():
        component_id = _sha256_json(
            {"graph": "training instance plus exact Fury player", "nodes": nodes}
        )
        for node in nodes:
            result[node] = component_id
    return result


def _new_lane_map() -> dict[str, dict[str, _Aggregate]]:
    return {lane: defaultdict(_Aggregate) for lane in LANES}


def _scan_training_evidence(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    instance_ids: set[str] | None = None,
    accepted_decision_writer: BinaryIO | None = None,
) -> tuple[
    dict[str, dict[str, _Aggregate]],
    dict[tuple[str, str], _Aggregate],
    dict[str, str],
    JSONMap,
]:
    node_index, outer_components = _outer_component_index(manifest)
    by_outer = _new_lane_map()
    focal_units: dict[tuple[str, str], _Aggregate] = defaultdict(_Aggregate)
    audit: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    logical_digest = hashlib.sha256()
    data_root = _data_root(manifest_path)
    instance_order = _array(manifest.get("instance_order"), "instance_order")
    entries = _array(manifest.get("instances"), "instances")
    if len(instance_order) != len(entries):
        raise ExternalHistoricalFuryPolicyV2Error(
            "INSTANCE_SET_MISMATCH", "instance order and entries differ"
        )
    observed_training_ids: list[str] = []
    observed_nontraining_ids: list[str] = []
    selected_ids = set(instance_ids) if instance_ids is not None else None
    if selected_ids is not None:
        unknown = selected_ids.difference(str(value) for value in instance_order)
        if unknown:
            raise ExternalHistoricalFuryPolicyV2Error(
                "INSTANCE_SELECTION_UNKNOWN",
                f"selected instance is absent from manifest: {sorted(unknown)[0]}",
            )
    for entry_position, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"instances[{entry_position}]")
        instance_id = _text(entry.get("instance_id"), "instance_id")
        if instance_order[entry_position] != instance_id:
            raise ExternalHistoricalFuryPolicyV2Error(
                "INSTANCE_ORDER_MISMATCH", "instance order is not deterministic"
            )
        if selected_ids is not None and instance_id not in selected_ids:
            continue
        contamination = _mapping(entry.get("contamination_lane"), "contamination_lane")
        candidate = contamination.get("candidate_filter_passed") is True
        (observed_training_ids if candidate else observed_nontraining_ids).append(instance_id)
        partition = _mapping(entry.get("partition"), "instance partition")
        path = _resolve_partition(manifest_path, partition.get("path"), data_root)
        if path.stat().st_size != _nonnegative_integer(
            partition.get("compressed_size_bytes"), "partition compressed size"
        ) or _sha256_file(path) != _sha(
            partition.get("compressed_file_sha256"), "partition compressed SHA"
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "PARTITION_IDENTITY_MISMATCH", f"partition identity differs: {path}"
            )
        try:
            with gzip.open(path, "rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    if not raw_line.strip():
                        continue
                    try:
                        wave = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ExternalHistoricalFuryPolicyV2Error(
                            "PARTITION_JSON_INVALID",
                            f"invalid JSON in {path}:{line_number}: {error}",
                        ) from error
                    if raw_line != _canonical_bytes(wave) + b"\n":
                        raise ExternalHistoricalFuryPolicyV2Error(
                            "PARTITION_ROW_NONCANONICAL",
                            f"noncanonical JSONL row in {path}:{line_number}",
                        )
                    wave_map = _mapping(wave, "team-wave record")
                    trace = [
                        _mapping(value, "exact_trace row")
                        for value in _array(wave_map.get("exact_trace"), "exact_trace")
                    ]
                    wave_identity = _mapping(wave_map.get("wave"), "wave identity")
                    if wave_identity.get("instance_id") != instance_id:
                        raise ExternalHistoricalFuryPolicyV2Error(
                            "WAVE_INSTANCE_MISMATCH", "wave crossed its instance partition"
                        )
                    wave_id = _text(wave_identity.get("wave_id"), "wave_id")
                    for raw_player in _array(wave_map.get("players"), "wave players"):
                        player = _mapping(raw_player, "wave player")
                        metadata = _mapping(player.get("player"), "player metadata")
                        if str(metadata.get("class") or "").upper() != "WARRIOR":
                            audit["nonwarrior_episodes_descriptive_only"] += 1
                            continue
                        spec = _mapping(player.get("warrior_spec_lane"), "warrior_spec_lane")
                        lane = spec.get("partition_key")
                        if lane not in LANES:
                            audit["unknown_warrior_spec_episodes_descriptive_only"] += 1
                            continue
                        instance_node, player_node, outer_component = (
                            _episode_nodes_and_outer_component(player, node_index)
                        )
                        eligibility = _mapping(
                            player.get("eligibility_observation"),
                            "eligibility_observation",
                        )
                        expected_flag = (
                            "historical_fury_candidate_filter_passed"
                            if lane == FURY_LANE
                            else "arms_diagnostic_candidate_filter_passed"
                        )
                        eligible = eligibility.get(expected_flag) is True
                        if eligible != candidate:
                            raise ExternalHistoricalFuryPolicyV2Error(
                                "CANDIDATE_MASK_MISMATCH",
                                "player eligibility differs from receipt-bound instance mask",
                            )
                        if not eligible:
                            exclusions[f"{lane}:DESCRIPTIVE_NONTRAINING"] += 1
                            continue
                        episode_aggregate = _Aggregate()
                        episode_aggregate.observe_episode(
                            player_node=player_node, instance_node=instance_node
                        )
                        _process_player_transitions(
                            player=player,
                            trace=trace,
                            aggregate=episode_aggregate,
                            audit=audit,
                            logical_digest=logical_digest,
                            accepted_decision_writer=accepted_decision_writer,
                            instance_id=instance_id,
                            wave_id=wave_id,
                            outer_component_id=outer_component,
                            player_node=player_node,
                        )
                        by_outer[str(lane)][outer_component].merge(
                            episode_aggregate
                        )
                        if lane == FURY_LANE:
                            focal_units[(instance_node, player_node)].merge(
                                episode_aggregate
                            )
        except ExternalHistoricalFuryPolicyV2Error:
            raise
        except OSError as error:
            raise ExternalHistoricalFuryPolicyV2Error(
                "PARTITION_READ_FAILED", f"cannot stream {path}: {error}"
            ) from error
    summary = _mapping(manifest.get("summary"), "team-wave summary")
    if selected_ids is None and (
        len(observed_training_ids)
        != _nonnegative_integer(
            summary.get("training_candidate_instance_count"),
            "summary.training_candidate_instance_count",
        )
        or len(observed_nontraining_ids)
        != _nonnegative_integer(
            summary.get("descriptive_nontraining_instance_count"),
            "summary.descriptive_nontraining_instance_count",
        )
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "CANDIDATE_MASK_COUNT_MISMATCH",
            "streamed training/nontraining mask differs from manifest summary",
        )

    focal_component_by_node = _focal_component_index(focal_units)
    accounting: JSONMap = {
        "outer_component_count_total": len(outer_components),
        "training_instance_count": len(observed_training_ids),
        "descriptive_nontraining_instance_count": len(observed_nontraining_ids),
        "training_instance_ids_sha256": _sha256_json(observed_training_ids),
        "descriptive_nontraining_instance_ids_sha256": _sha256_json(
            observed_nontraining_ids
        ),
        "action_label_audit": dict(sorted(audit.items())),
        "excluded_episode_reasons": dict(sorted(exclusions.items())),
        "accepted_decision_logical_sha256": logical_digest.hexdigest(),
    }
    return by_outer, focal_units, focal_component_by_node, accounting


def _merge_aggregates(values: Iterable[_Aggregate]) -> _Aggregate:
    result = _Aggregate()
    for value in values:
        result.merge(value)
    return result


def _focal_component_aggregates(
    units: Mapping[tuple[str, str], _Aggregate],
    component_by_node: Mapping[str, str],
) -> dict[str, _Aggregate]:
    result: dict[str, _Aggregate] = defaultdict(_Aggregate)
    for (instance_node, player_node), aggregate in sorted(units.items()):
        left = component_by_node.get(instance_node)
        right = component_by_node.get(player_node)
        if left is None or left != right:
            raise ExternalHistoricalFuryPolicyV2Error(
                "FOCAL_COMPONENT_INDEX_INVALID",
                "focal instance/player unit crosses diagnostic components",
            )
        result[left].merge(aggregate)
    return dict(result)


def _sorted_counts(counter: Mapping[str, int]) -> list[JSONMap]:
    return [
        {"key": key, "count": int(counter[key])}
        for key in sorted(counter)
        if counter[key] > 0
    ]


def _fit_aggregate(
    aggregate: _Aggregate, *, alpha: float, backoff_strength: float
) -> JSONMap:
    if not math.isfinite(alpha) or alpha <= 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "INVALID_HYPERPARAMETER", "smoothing alpha must be positive and finite"
        )
    if not math.isfinite(backoff_strength) or backoff_strength <= 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "INVALID_HYPERPARAMETER", "backoff strength must be positive and finite"
        )
    return {
        "decision_count": aggregate.decision_count,
        "episode_count": aggregate.episode_count,
        "observed_player_node_count": len(aggregate.player_nodes),
        "observed_instance_node_count": len(aggregate.instance_nodes),
        "action_catalog": sorted(aggregate.action_counts),
        "global_action_counts": _sorted_counts(aggregate.action_counts),
        "context_action_counts": {
            level: [
                {
                    "context_key": context_key,
                    "counts": _sorted_counts(counts),
                }
                for context_key, counts in sorted(
                    aggregate.context_counts[level].items()
                )
            ]
            for level in CONTEXT_LEVELS
        },
        "smoothing": {
            "alpha": alpha,
            "backoff_strength": backoff_strength,
            "algorithm": (
                "global symmetric Dirichlet then coarse/tactical/full posterior "
                "mixtures in fixed order"
            ),
        },
    }


def _counts_from_rows(rows: Any, label: str) -> Counter[str]:
    result: Counter[str] = Counter()
    for position, raw in enumerate(_array(rows, label)):
        row = _mapping(raw, f"{label}[{position}]")
        key = _text(row.get("key"), f"{label}[{position}].key")
        count = _nonnegative_integer(row.get("count"), f"{label}[{position}].count")
        if key in result or count <= 0:
            raise ExternalHistoricalFuryPolicyV2Error(
                "FITTED_COUNT_INVALID", f"{label} contains duplicate/zero counts"
            )
        result[key] = count
    return result


def _fitted_indexes(fitted: Mapping[str, Any]) -> tuple[Counter[str], dict[str, dict[str, Counter[str]]]]:
    global_counts = _counts_from_rows(
        fitted.get("global_action_counts"), "global_action_counts"
    )
    levels_raw = _mapping(
        fitted.get("context_action_counts"), "context_action_counts"
    )
    if set(levels_raw) != set(CONTEXT_LEVELS):
        raise ExternalHistoricalFuryPolicyV2Error(
            "FITTED_CONTEXT_LEVEL_MISMATCH", "fitted context levels differ"
        )
    levels: dict[str, dict[str, Counter[str]]] = {}
    for level in CONTEXT_LEVELS:
        index: dict[str, Counter[str]] = {}
        for position, raw in enumerate(_array(levels_raw[level], f"{level} contexts")):
            row = _mapping(raw, f"{level} contexts[{position}]")
            context_key = _text(row.get("context_key"), "context_key")
            if context_key in index:
                raise ExternalHistoricalFuryPolicyV2Error(
                    "FITTED_CONTEXT_DUPLICATE", f"duplicate {level} context"
                )
            index[context_key] = _counts_from_rows(
                row.get("counts"), f"{level} context counts"
            )
        levels[level] = index
    return global_counts, levels


def _distribution_from_indexes(
    *,
    global_counts: Mapping[str, int],
    levels: Mapping[str, Mapping[str, Mapping[str, int]]],
    alpha: float,
    strength: float,
    contexts: Mapping[str, str],
) -> tuple[dict[str, float], tuple[str, ...], float]:
    if alpha <= 0 or strength <= 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "INVALID_FITTED_SMOOTHING", "fitted smoothing must be positive"
        )
    catalog = sorted(global_counts)
    total = sum(global_counts.values())
    denominator = total + alpha * (len(catalog) + 1)
    if denominator <= 0:
        return {}, (), 1.0
    probabilities = {
        action: (global_counts[action] + alpha) / denominator
        for action in catalog
    }
    unknown_probability = alpha / denominator
    matched: list[str] = []
    for level in CONTEXT_LEVELS:
        context_counts = levels[level].get(contexts[level])
        if not context_counts:
            continue
        matched.append(level)
        count_total = sum(context_counts.values())
        posterior_denominator = count_total + strength
        probabilities = {
            action: (
                context_counts.get(action, 0) + strength * probabilities[action]
            )
            / posterior_denominator
            for action in catalog
        }
        unknown_probability = (
            strength * unknown_probability / posterior_denominator
        )
    return probabilities, tuple(matched), unknown_probability


def _distribution(
    fitted: Mapping[str, Any], contexts: Mapping[str, str]
) -> tuple[dict[str, float], tuple[str, ...], float]:
    global_counts, levels = _fitted_indexes(fitted)
    alpha = _finite_nonnegative_number(
        _mapping(fitted.get("smoothing"), "smoothing").get("alpha"), "alpha"
    )
    strength = _finite_nonnegative_number(
        _mapping(fitted.get("smoothing"), "smoothing").get("backoff_strength"),
        "backoff_strength",
    )
    return _distribution_from_indexes(
        global_counts=global_counts,
        levels=levels,
        alpha=alpha,
        strength=strength,
        contexts=contexts,
    )


def _fold_assignment(
    component_ids: Sequence[str], *, fold_count: int, split_seed: int
) -> tuple[int, dict[str, int]]:
    if isinstance(fold_count, bool) or not isinstance(fold_count, int) or fold_count < 2:
        raise ExternalHistoricalFuryPolicyV2Error(
            "INVALID_FOLD_COUNT", "fold_count must be an integer of at least two"
        )
    if isinstance(split_seed, bool) or not isinstance(split_seed, int):
        raise ExternalHistoricalFuryPolicyV2Error(
            "INVALID_SPLIT_SEED", "split_seed must be an integer"
        )
    if not component_ids:
        return 0, {}
    effective = min(fold_count, len(component_ids))
    ranked = sorted(
        component_ids,
        key=lambda component: (
            _sha256_json({"split_seed": split_seed, "component_id": component}),
            component,
        ),
    )
    return effective, {
        component: position % effective for position, component in enumerate(ranked)
    }


def _empty_metrics() -> JSONMap:
    return {
        "heldout_decision_count": 0,
        "known_action_count": 0,
        "known_action_coverage": None,
        "top1_accuracy": None,
        "top3_accuracy": None,
        "contextual_log_loss": None,
        "global_log_loss": None,
        "contextual_log_loss_improvement": None,
        "expected_calibration_error": None,
    }


def _evaluate_component_split(
    components: Mapping[str, _Aggregate],
    *,
    fold_count: int,
    split_seed: int,
    alpha: float,
    backoff_strength: float,
) -> JSONMap:
    ids = sorted(components)
    effective, assignment = _fold_assignment(
        ids, fold_count=fold_count, split_seed=split_seed
    )
    if effective < 2:
        return {
            "component_count": len(ids),
            "effective_fold_count": effective,
            "component_to_fold": [
                {"component_id": key, "fold_index": assignment[key]}
                for key in sorted(assignment)
            ],
            "each_component_held_out_exactly_once": bool(ids),
            "row_random_split": False,
            "folds": [],
            "metrics": _empty_metrics(),
        }
    total_n = 0
    known_n = 0
    top1_n = 0
    top3_n = 0
    contextual_loss = 0.0
    global_loss = 0.0
    calibration_rows: list[tuple[float, bool, int]] = []
    folds: list[JSONMap] = []
    for fold_index in range(effective):
        test_ids = [key for key in ids if assignment[key] == fold_index]
        train_ids = [key for key in ids if assignment[key] != fold_index]
        train = _merge_aggregates(components[key] for key in train_ids)
        test = _merge_aggregates(components[key] for key in test_ids)
        fitted = _fit_aggregate(
            train, alpha=alpha, backoff_strength=backoff_strength
        )
        global_counts, fitted_levels = _fitted_indexes(fitted)
        catalog = sorted(global_counts)
        global_total = sum(global_counts.values())
        global_denominator = global_total + alpha * (len(catalog) + 1)
        fold_top1 = 0
        fold_n = 0
        for cell, count in sorted(test.decision_cells.items()):
            coarse, tactical, full, action = cell
            contexts = {"coarse": coarse, "tactical": tactical, "full": full}
            probabilities, _matched, unknown = _distribution_from_indexes(
                global_counts=global_counts,
                levels=fitted_levels,
                alpha=alpha,
                strength=backoff_strength,
                contexts=contexts,
            )
            ranked = sorted(probabilities, key=lambda key: (-probabilities[key], key))
            known = action in probabilities
            probability = probabilities.get(action, unknown)
            if probability <= 0 or not math.isfinite(probability):
                raise ExternalHistoricalFuryPolicyV2Error(
                    "EVALUATION_ZERO_PROBABILITY", "held-out action has invalid probability"
                )
            global_probability = (
                (global_counts.get(action, 0) + alpha) / global_denominator
                if global_denominator > 0
                else 1.0
            )
            predicted = ranked[0] if ranked else UNKNOWN_ACTION_KEY
            correct = predicted == action
            total_n += count
            fold_n += count
            known_n += count if known else 0
            top1_n += count if correct else 0
            fold_top1 += count if correct else 0
            top3_n += count if action in ranked[:3] else 0
            contextual_loss -= count * math.log(probability)
            global_loss -= count * math.log(global_probability)
            confidence = probabilities.get(predicted, unknown)
            calibration_rows.append((confidence, correct, count))
        folds.append(
            {
                "fold_index": fold_index,
                "train_component_ids_sha256": _sha256_json(train_ids),
                "test_component_ids_sha256": _sha256_json(test_ids),
                "train_component_count": len(train_ids),
                "test_component_count": len(test_ids),
                "train_test_component_overlap_count": 0,
                "heldout_decision_count": fold_n,
                "top1_accuracy": fold_top1 / fold_n if fold_n else None,
            }
        )
    if total_n == 0:
        metrics = _empty_metrics()
    else:
        ece = 0.0
        for bin_index in range(10):
            low = bin_index / 10.0
            high = (bin_index + 1) / 10.0
            rows = [
                row
                for row in calibration_rows
                if row[0] >= low and (row[0] < high or bin_index == 9)
            ]
            weight = sum(row[2] for row in rows)
            if weight:
                average_confidence = sum(row[0] * row[2] for row in rows) / weight
                average_accuracy = sum(bool(row[1]) * row[2] for row in rows) / weight
                ece += (weight / total_n) * abs(average_confidence - average_accuracy)
        contextual_mean = contextual_loss / total_n
        global_mean = global_loss / total_n
        metrics = {
            "heldout_decision_count": total_n,
            "known_action_count": known_n,
            "known_action_coverage": known_n / total_n,
            "top1_accuracy": top1_n / total_n,
            "top3_accuracy": top3_n / total_n,
            "contextual_log_loss": contextual_mean,
            "global_log_loss": global_mean,
            "contextual_log_loss_improvement": global_mean - contextual_mean,
            "expected_calibration_error": ece,
        }
    return {
        "component_count": len(ids),
        "effective_fold_count": effective,
        "component_to_fold": [
            {"component_id": key, "fold_index": assignment[key]}
            for key in sorted(assignment)
        ],
        "each_component_held_out_exactly_once": True,
        "row_random_split": False,
        "folds": folds,
        "metrics": metrics,
    }


def _outer_rejections(evaluation: Mapping[str, Any]) -> list[JSONMap]:
    result: list[JSONMap] = []
    count = _nonnegative_integer(
        evaluation.get("component_count"), "outer component_count"
    )
    minimum_components = int(OUTER_FIDELITY_THRESHOLDS["minimum_independent_components"])
    if count < minimum_components:
        result.append(
            {
                "code": "INSUFFICIENT_INDEPENDENT_OUTER_COMPONENTS",
                "stage": "heldout_fidelity",
                "observed": count,
                "required": minimum_components,
                "message": (
                    "full-roster guild/player/instance components are structurally "
                    "insufficient; raids or player-wave rows are not substitutes"
                ),
            }
        )
    metrics = _mapping(evaluation.get("metrics"), "outer metrics")
    checks = (
        ("heldout_decision_count", "minimum_heldout_decisions", "below"),
        ("known_action_coverage", "minimum_known_action_coverage", "below"),
        ("top1_accuracy", "minimum_top1_accuracy", "below"),
        ("top3_accuracy", "minimum_top3_accuracy", "below"),
        (
            "contextual_log_loss_improvement",
            "minimum_contextual_log_loss_improvement",
            "below",
        ),
        (
            "expected_calibration_error",
            "maximum_expected_calibration_error",
            "above",
        ),
    )
    for metric, threshold_name, direction in checks:
        observed = metrics.get(metric)
        threshold = OUTER_FIDELITY_THRESHOLDS[threshold_name]
        failed = (
            observed is None
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
            or (direction == "below" and float(observed) < float(threshold))
            or (direction == "above" and float(observed) > float(threshold))
        )
        if failed:
            result.append(
                {
                    "code": f"OUTER_{metric.upper()}_THRESHOLD_NOT_MET",
                    "stage": "heldout_fidelity",
                    "observed": observed,
                    "required": threshold,
                    "message": f"outer held-out metric {metric} did not satisfy its frozen threshold",
                }
            )
    return result


def _blocker(code: str, stage: str, message: str, **details: Any) -> JSONMap:
    return {
        "code": code,
        "stage": stage,
        "message": message,
        "blocking": True,
        "details": deepcopy(details),
    }


def _validate_fitted_policy(fitted: Mapping[str, Any], label: str) -> None:
    decision_count = _nonnegative_integer(fitted.get("decision_count"), f"{label}.decision_count")
    global_counts, levels = _fitted_indexes(fitted)
    if decision_count != sum(global_counts.values()):
        raise ExternalHistoricalFuryPolicyV2Error(
            "FITTED_DECISION_COUNT_MISMATCH", f"{label} decision count differs"
        )
    catalog = _array(fitted.get("action_catalog"), f"{label}.action_catalog")
    if catalog != sorted(global_counts):
        raise ExternalHistoricalFuryPolicyV2Error(
            "FITTED_ACTION_CATALOG_MISMATCH", f"{label} action catalog differs"
        )
    for level in CONTEXT_LEVELS:
        for counts in levels[level].values():
            if any(key not in global_counts for key in counts):
                raise ExternalHistoricalFuryPolicyV2Error(
                    "FITTED_CONTEXT_ACTION_UNKNOWN",
                    f"{label} context contains an action outside the catalog",
                )


def validate_policy_model(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != MODEL_KIND
        or document.get("implementation_revision") != IMPLEMENTATION_REVISION
        or document.get("status") != STATUS_NONVOTING
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_MODEL_SCHEMA_MISMATCH", "unsupported policy model"
        )
    _verify_content_address(document, "historical policy model")
    source = _mapping(document.get("source"), "model.source")
    if (
        source.get("schema") != team_model_v2.SCHEMA
        or source.get("implementation_revision") != team_model_v2.IMPLEMENTATION_REVISION
        or source.get("network_requests_made") != 0
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_MODEL_SOURCE_MISMATCH", "model source is not current offline External-V2"
        )
    source_core = dict(source)
    declared_source_bundle = _sha(
        source_core.pop("source_bundle_sha256", None), "source_bundle_sha256"
    )
    if declared_source_bundle != _sha256_json(source_core):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_SOURCE_BUNDLE_MISMATCH",
            "model source bundle digest does not cover its exact binding",
        )
    features = _mapping(document.get("feature_contract"), "feature_contract")
    if (
        features.get("strict_prefix_state_before_only") is not True
        or features.get("future_outcomes_allowed") is not False
        or features.get("leave_one_player_out_exact_guid_direct_owned_controller") is not True
        or features.get("named_player_feature_allowed") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_FEATURE_CONTRACT_WEAKENED", "policy feature contract was widened"
        )
    split = _mapping(document.get("split_contract"), "split_contract")
    if (
        split.get("promotion_authority")
        != "UPSTREAM_FULL_ROSTER_GUILD_PLAYER_INSTANCE_COMPONENT_ONLY"
        or split.get("focal_fury_component_is_diagnostic_only") is not True
        or split.get("focal_fury_component_can_promote") is not False
        or split.get("row_random_split_allowed") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_SPLIT_CONTRACT_WEAKENED", "policy split contract was widened"
        )
    profile = _mapping(document.get("profile_descriptor"), "profile_descriptor")
    if document.get("policy_profile_sha256") != _sha256_json(profile):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_PROFILE_BINDING_MISMATCH",
            "policy profile digest differs from its descriptor",
        )
    adapter_spec = _mapping(
        document.get("adapter_interface_spec"), "adapter_interface_spec"
    )
    if (
        document.get("adapter_interface_spec_sha256")
        != _sha256_json(adapter_spec)
        or adapter_spec.get("full_scenario_adapter_verified") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_ADAPTER_SPEC_BINDING_MISMATCH",
            "adapter interface digest/status differs from its descriptor",
        )
    lanes = _mapping(document.get("lanes"), "model.lanes")
    if set(lanes) != set(LANES):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_LANE_MISMATCH", "Fury and Arms lanes must remain separate"
        )
    for lane in LANES:
        lane_value = _mapping(lanes[lane], lane)
        if lane_value.get("can_vote") is not False:
            raise ExternalHistoricalFuryPolicyV2Error(
                "POLICY_PREMATURE_PROMOTION", f"{lane} cannot vote in this artifact"
            )
        _validate_fitted_policy(
            _mapping(lane_value.get("fitted_policy"), f"{lane}.fitted_policy"), lane
        )
    boundary = _mapping(document.get("claim_boundary"), "model.claim_boundary")
    if (
        boundary.get("comparison_ready") is not False
        or boundary.get("runner_receipt_emitted") is not False
        or boundary.get("superiority_claim") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_CLAIM_BOUNDARY_WIDENED", "model made an unauthorized claim"
        )


def validate_prefix_receipt(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != PREFIX_RECEIPT_KIND
        or document.get("implementation_revision") != IMPLEMENTATION_REVISION
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "PREFIX_RECEIPT_SCHEMA_MISMATCH", "unsupported prefix receipt"
        )
    _verify_content_address(document, "prefix receipt")
    contract = _mapping(document.get("verified_contract"), "verified_contract")
    if (
        contract.get("strict_prefix_state_before_only") is not True
        or contract.get("current_event_is_label_not_feature") is not True
        or contract.get("future_outcomes_allowed") is not False
        or contract.get("leave_one_player_out_verified_per_accepted_label") is not True
        or contract.get("direct_player_action_labels_only") is not True
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "PREFIX_RECEIPT_CONTRACT_WEAKENED", "prefix receipt contract was weakened"
        )
    if document.get("prefix_causality_verified") is not True:
        raise ExternalHistoricalFuryPolicyV2Error(
            "PREFIX_CAUSALITY_NOT_VERIFIED", "prefix receipt did not close causality"
        )


def validate_policy_evaluation(
    document: Mapping[str, Any], *, model_content_sha256: str | None = None
) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != EVALUATION_KIND
        or document.get("implementation_revision") != IMPLEMENTATION_REVISION
        or document.get("status") != STATUS_EVALUATION
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "POLICY_EVALUATION_SCHEMA_MISMATCH", "unsupported policy evaluation"
        )
    _verify_content_address(document, "historical policy evaluation")
    declared_model = _sha(document.get("model_content_sha256"), "model_content_sha256")
    if model_content_sha256 is not None and declared_model != _sha(
        model_content_sha256, "expected model SHA"
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "EVALUATION_MODEL_BINDING_MISMATCH", "evaluation points to another model"
        )
    outer = _mapping(document.get("outer_full_roster_evaluation"), "outer evaluation")
    if (
        outer.get("promotion_authority") is not True
        or outer.get("split_unit")
        != "upstream full-roster guild/player/instance connected component"
        or outer.get("row_random_split") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "OUTER_EVALUATION_CONTRACT_WEAKENED", "outer evaluation is not component-safe"
        )
    diagnostic = _mapping(
        document.get("focal_fury_component_diagnostic"), "focal diagnostic"
    )
    if (
        diagnostic.get("promotion_authority") is not False
        or diagnostic.get("can_override_outer_failure") is not False
        or diagnostic.get("shared_teammate_or_guild_leakage_blocked") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "FOCAL_DIAGNOSTIC_PREMATURE_PROMOTION",
            "focal diagnostic attempted to replace the outer gate",
        )
    boundary = _mapping(document.get("claim_boundary"), "evaluation.claim_boundary")
    if (
        boundary.get("comparison_ready") is not False
        or boundary.get("fourth_baseline_ready") is not False
        or boundary.get("superiority_claim") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "EVALUATION_CLAIM_BOUNDARY_WIDENED",
            "evaluation made an unauthorized claim",
        )


def validate_admission_manifest(
    document: Mapping[str, Any],
    *,
    model_content_sha256: str | None = None,
    evaluation_content_sha256: str | None = None,
    prefix_receipt_content_sha256: str | None = None,
) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != ADMISSION_KIND
        or document.get("implementation_revision") != IMPLEMENTATION_REVISION
        or document.get("status") != STATUS_BLOCKED
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "ADMISSION_SCHEMA_MISMATCH", "unsupported admission manifest"
        )
    _verify_content_address(document, "historical policy admission")
    bindings = _mapping(document.get("artifact_bindings"), "artifact_bindings")
    expected = {
        "model_content_sha256": model_content_sha256,
        "evaluation_content_sha256": evaluation_content_sha256,
        "prefix_receipt_content_sha256": prefix_receipt_content_sha256,
    }
    for field, value in expected.items():
        _sha(bindings.get(field), f"artifact_bindings.{field}")
        if value is not None and bindings.get(field) != _sha(value, f"expected {field}"):
            raise ExternalHistoricalFuryPolicyV2Error(
                "ADMISSION_ARTIFACT_BINDING_MISMATCH",
                f"admission {field} points to another artifact",
            )
    blockers = _array(document.get("typed_blockers"), "typed_blockers")
    if not blockers:
        raise ExternalHistoricalFuryPolicyV2Error(
            "ADMISSION_FALSE_READY", "this revision must retain its real blockers"
        )
    codes = {_mapping(value, "typed blocker").get("code") for value in blockers}
    required = {
        "TEAM_BACKGROUND_RUNTIME_ADMISSION_NOT_CLOSED",
        "FULL_SCENARIO_ADAPTER_NOT_IMPLEMENTED",
        "ORDERED_EXECUTION_FIDELITY_NOT_CLOSED",
        "PAIRED_ROLLOUT_IDENTITY_NOT_CLOSED",
    }
    if not required.issubset(codes):
        raise ExternalHistoricalFuryPolicyV2Error(
            "ADMISSION_RUNTIME_BLOCKERS_MISSING",
            "blocked admission omitted mandatory runtime/fidelity blockers",
        )
    if (
        document.get("runner_receipt_schema") != RUNNER_RECEIPT_SCHEMA
        or document.get("runner_receipt") is not None
        or document.get("runner_receipt_emitted") is not False
        or document.get("artifact_ready") is not False
        or document.get("readiness_conditions_satisfied") is not False
        or document.get("comparison_ready_by_itself") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "ADMISSION_PREMATURE_PROMOTION",
            "blocked admission attempted to mint/promote a runner receipt",
        )
    boundary = _mapping(document.get("claim_boundary"), "claim_boundary")
    if (
        boundary.get("historical_policy_fit_executed") is not True
        or boundary.get("candidate_training_executed") is not False
        or boundary.get("policy_comparison_executed") is not False
        or boundary.get("runner_dispatch_authorized") is not False
        or boundary.get("superiority_claim") is not False
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "ADMISSION_CLAIM_BOUNDARY_WIDENED",
            "admission execution/claim boundary is inaccurate",
        )


def _atomic_write(path: Path, document: Mapping[str, Any]) -> Path:
    payload = _canonical_bytes(document) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    import os

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(raw_path).unlink(missing_ok=True)
        raise
    return Path(raw_path)


def _publish_pair(
    output: Path, *, stem: str, document: Mapping[str, Any]
) -> tuple[Path, Path]:
    content_sha = _verify_content_address(document, stem)
    stable = output / f"{stem}.json"
    addressed = output / f"{stem}.{content_sha}.json"
    addressed_temp = _atomic_write(addressed, document)
    stable_temp = _atomic_write(stable, document)
    try:
        payload = addressed_temp.read_bytes()
        if addressed.exists():
            if addressed.is_symlink() or addressed.read_bytes() != payload:
                raise ExternalHistoricalFuryPolicyV2Error(
                    "IMMUTABLE_ARTIFACT_COLLISION",
                    f"content-addressed artifact differs: {addressed}",
                )
            addressed_temp.unlink()
        else:
            addressed_temp.replace(addressed)
        stable_temp.replace(stable)
    finally:
        addressed_temp.unlink(missing_ok=True)
        stable_temp.unlink(missing_ok=True)
    return stable, addressed


def build_external_historical_fury_policy_v2(
    *,
    team_wave_model_manifest_path: str | Path = DEFAULT_INPUT_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    fold_count: int = DEFAULT_FOLD_COUNT,
    split_seed: int = DEFAULT_SPLIT_SEED,
    smoothing_alpha: float = DEFAULT_SMOOTHING_ALPHA,
    backoff_strength: float = DEFAULT_BACKOFF_STRENGTH,
    _training_evidence: tuple[
        dict[str, dict[str, _Aggregate]],
        dict[tuple[str, str], _Aggregate],
        dict[str, str],
        JSONMap,
    ]
    | None = None,
    _loaded_input: tuple[JSONMap, Path, JSONMap] | None = None,
) -> HistoricalFuryPolicyV2Result:
    """Build model/evaluation/prefix/admission artifacts without promotion."""

    manifest, manifest_path, source = (
        _load_current_input(team_wave_model_manifest_path)
        if _loaded_input is None
        else _loaded_input
    )
    data_root = _data_root(manifest_path)
    output = _under(Path(output_directory), data_root, "policy output")
    output.mkdir(parents=True, exist_ok=True)
    by_outer, focal_units, focal_component_by_node, accounting = (
        _scan_training_evidence(manifest, manifest_path)
        if _training_evidence is None
        else _training_evidence
    )
    focal_components = _focal_component_aggregates(
        focal_units, focal_component_by_node
    )
    all_lanes = {
        lane: _merge_aggregates(by_outer[lane].values()) for lane in LANES
    }

    prefix_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": PREFIX_RECEIPT_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "policy_id": POLICY_ID,
        "source_bundle_sha256": source["source_bundle_sha256"],
        "team_wave_model_manifest_sha256": source["manifest_content_sha256"],
        "cohort_receipt_sha256": source["cohort_receipt_content_sha256"],
        "accepted_decision_logical_sha256": accounting[
            "accepted_decision_logical_sha256"
        ],
        "verified_contract": {
            "strict_prefix_state_before_only": True,
            "current_event_is_label_not_feature": True,
            "future_outcomes_allowed": False,
            "leave_one_player_out_verified_per_accepted_label": True,
            "leave_out_attribution": [
                "DIRECT_FRIENDLY_PLAYER",
                "EXACT_OFFICIAL_OWNER",
                "EXACT_OFFICIAL_CONTROLLER",
            ],
            "unattributed_retained_as_unknown": True,
            "direct_player_action_labels_only": True,
            "owner_or_controller_actions_are_policy_labels": False,
            "start_go_dedup_is_forward_causal": True,
            "negative_damage_enters_features_or_rewards": False,
        },
        "accounting": deepcopy(accounting),
        "prefix_causality_verified": True,
        "comparison_authorized": False,
    }
    prefix_receipt = _content_addressed(prefix_core)
    validate_prefix_receipt(prefix_receipt)
    prefix_sha = _verify_content_address(prefix_receipt, "prefix receipt")

    profile_descriptor = {
        "policy_id": POLICY_ID,
        "lane": FURY_LANE,
        "observation_contract": "strict-prefix External-V2 discretized context/v1",
        "action_contract": "direct START or unpaired GO spell plus relative target role/v1",
        "legal_action_conditioning_required": True,
        "simulator_runtime_adapter_present": False,
    }
    policy_adapter_spec = {
        "policy_id": POLICY_ID,
        "model_api": "predict_action_distribution/sample_legal_action",
        "input": "caller-supplied strict-prefix observation and legal actions",
        "full_scenario_adapter_verified": False,
        "claim": "INTERFACE_SPEC_ONLY_NOT_RUNTIME_ADMISSION",
    }
    model_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": MODEL_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS_NONVOTING,
        "policy_id": POLICY_ID,
        "source": source,
        "prefix_receipt_content_sha256": prefix_sha,
        "feature_contract": {
            "strict_prefix_state_before_only": True,
            "current_event_is_label_not_feature": True,
            "future_outcomes_allowed": False,
            "final_wave_or_death_clock_allowed": False,
            "leave_one_player_out_exact_guid_direct_owned_controller": True,
            "unattributed_background_retained": True,
            "named_player_feature_allowed": False,
            "exact_target_guid_feature_allowed": False,
            "context_levels": list(CONTEXT_LEVELS),
        },
        "action_contract": {
            "direct_friendly_player_only": True,
            "labels": "START plus direct unpaired GO",
            "paired_go_deduplicated_from_prior_start": True,
            "failed_cast_is_action_label": False,
            "owner_or_controller_event_is_action_label": False,
            "target_identity": "relative role only",
            "unsupported_nonvoting_target_excluded": True,
            "legal_action_conditioning_required_at_runtime": True,
        },
        "split_contract": {
            "promotion_authority": (
                "UPSTREAM_FULL_ROSTER_GUILD_PLAYER_INSTANCE_COMPONENT_ONLY"
            ),
            "outer_component_count_total": accounting[
                "outer_component_count_total"
            ],
            "focal_fury_component_is_diagnostic_only": True,
            "focal_fury_component_can_promote": False,
            "focal_fury_component_graph": "training instance plus exact Fury player",
            "focal_fury_component_count": len(focal_components),
            "focal_graph_omits_guild_and_other_teammates": True,
            "row_random_split_allowed": False,
        },
        "training_contract": {
            "candidate_mask_authority": "bound External-V2 cohort receipt",
            "raw_contamination_label_may_expand_mask": False,
            "descriptive_nontraining_enters_counts": False,
            "fury_and_arms_counts_shared": False,
            "arms_policy_role": "CROSS_SPEC_DIAGNOSTIC_ONLY",
            "named_player_weighting_allowed": False,
        },
        "profile_descriptor": profile_descriptor,
        "policy_profile_sha256": _sha256_json(profile_descriptor),
        "adapter_interface_spec": policy_adapter_spec,
        "adapter_interface_spec_sha256": _sha256_json(policy_adapter_spec),
        "lanes": {
            lane: {
                "policy_role": (
                    "FUTURE_FOURTH_BASELINE_CANDIDATE_NONVOTING"
                    if lane == FURY_LANE
                    else "CROSS_SPEC_DIAGNOSTIC_ONLY_NONVOTING"
                ),
                "can_vote": False,
                "outer_decision_component_count": len(by_outer[lane]),
                "fitted_policy": _fit_aggregate(
                    all_lanes[lane],
                    alpha=smoothing_alpha,
                    backoff_strength=backoff_strength,
                ),
            }
            for lane in LANES
        },
        "claim_boundary": {
            "comparison_ready": False,
            "runner_receipt_emitted": False,
            "full_scenario_adapter_verified": False,
            "ordered_execution_fidelity_closed": False,
            "paired_rollout_identity_closed": False,
            "superiority_claim": False,
        },
    }
    model = _content_addressed(model_core)
    validate_policy_model(model)
    model_sha = _verify_content_address(model, "historical policy model")

    outer_evaluation = _evaluate_component_split(
        by_outer[FURY_LANE],
        fold_count=fold_count,
        split_seed=split_seed,
        alpha=smoothing_alpha,
        backoff_strength=backoff_strength,
    )
    outer_rejections = _outer_rejections(outer_evaluation)
    focal_evaluation = _evaluate_component_split(
        focal_components,
        fold_count=fold_count,
        split_seed=split_seed,
        alpha=smoothing_alpha,
        backoff_strength=backoff_strength,
    )
    evaluation_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS_EVALUATION,
        "policy_id": POLICY_ID,
        "source_bundle_sha256": source["source_bundle_sha256"],
        "model_content_sha256": model_sha,
        "prefix_receipt_content_sha256": prefix_sha,
        "outer_full_roster_evaluation": {
            "promotion_authority": True,
            "split_unit": (
                "upstream full-roster guild/player/instance connected component"
            ),
            "minimum_independent_components": OUTER_FIDELITY_THRESHOLDS[
                "minimum_independent_components"
            ],
            **outer_evaluation,
            "fidelity_status": (
                "PASS" if not outer_rejections else "FAIL"
            ),
            "typed_rejections": outer_rejections,
        },
        "focal_fury_component_diagnostic": {
            "promotion_authority": False,
            "can_override_outer_failure": False,
            "split_unit": "training instance plus exact Fury player component",
            "guild_nodes_included": False,
            "other_teammate_nodes_included": False,
            "shared_teammate_or_guild_leakage_blocked": False,
            "interpretation": (
                "diagnoses action learnability only; cannot authorize comparison"
            ),
            **focal_evaluation,
        },
        "thresholds": deepcopy(dict(OUTER_FIDELITY_THRESHOLDS)),
        "claim_boundary": {
            "comparison_ready": False,
            "fourth_baseline_ready": False,
            "focal_diagnostic_can_promote": False,
            "superiority_claim": False,
        },
    }
    evaluation = _content_addressed(evaluation_core)
    validate_policy_evaluation(evaluation, model_content_sha256=model_sha)
    evaluation_sha = _verify_content_address(
        evaluation, "historical policy evaluation"
    )

    blockers: list[JSONMap] = [
        _blocker(
            rejection["code"],
            "heldout_fidelity",
            rejection["message"],
            observed=rejection.get("observed"),
            required=rejection.get("required"),
        )
        for rejection in outer_rejections
    ]
    current_shape = (
        source["descriptive_instance_count"]
        == CURRENT_EXPECTED_DESCRIPTIVE_INSTANCES
        and source["training_instance_count"]
        == CURRENT_EXPECTED_TRAINING_INSTANCES
        and source["descriptive_nontraining_instance_count"]
        == CURRENT_EXPECTED_NONTRAINING_INSTANCES
    )
    if not current_shape:
        blockers.append(
            _blocker(
                "CURRENT_EXTERNAL_V2_84_68_16_COHORT_NOT_PRESENT",
                "source_admission",
                "source is valid for testing but is not the frozen current 84/68/16 cohort",
                observed={
                    "descriptive": source["descriptive_instance_count"],
                    "training": source["training_instance_count"],
                    "nontraining": source[
                        "descriptive_nontraining_instance_count"
                    ],
                },
                required={"descriptive": 84, "training": 68, "nontraining": 16},
            )
        )
    blockers.extend(
        [
            _blocker(
                "TEAM_BACKGROUND_RUNTIME_ADMISSION_NOT_CLOSED",
                "runtime_environment",
                (
                    "External-V2 team-background generator remains descriptive/nonvoting "
                    "and has no closed held-out response/dynamic simulator gate"
                ),
                required_source_schema=(
                    "chronicle_external_team_background_generator/v2"
                ),
                current_adapter_command="load_dynamic_v1",
                future_full_policy_interface="fury_full_policy_rollout_v3",
            ),
            _blocker(
                "FULL_SCENARIO_ADAPTER_NOT_IMPLEMENTED",
                "runtime_adapter",
                "no independently validated simulator full-scenario adapter exists",
            ),
            _blocker(
                "ORDERED_EXECUTION_FIDELITY_NOT_CLOSED",
                "runtime_fidelity",
                "no held-out ordered action/legality/acceptance/result fidelity artifact exists",
            ),
            _blocker(
                "PAIRED_ROLLOUT_IDENTITY_NOT_CLOSED",
                "paired_runner",
                "no independent paired-rollout identity closure exists for this policy",
            ),
        ]
    )
    satisfied = [
        "external_v2_candidate_mask_exactly_bound",
        "prefix_causality_verified",
        "policy_model_content_addressed",
    ]
    admission_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": ADMISSION_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS_BLOCKED,
        "policy_id": POLICY_ID,
        "runner_receipt_schema": RUNNER_RECEIPT_SCHEMA,
        "runner_required_conditions": list(RUNNER_REQUIRED_CONDITIONS),
        "artifact_bindings": {
            "source_bundle_sha256": source["source_bundle_sha256"],
            "cohort_receipt_sha256": source[
                "cohort_receipt_content_sha256"
            ],
            "team_wave_model_manifest_sha256": source[
                "manifest_content_sha256"
            ],
            "model_content_sha256": model_sha,
            "evaluation_content_sha256": evaluation_sha,
            "prefix_receipt_content_sha256": prefix_sha,
            "policy_profile_sha256": model["policy_profile_sha256"],
            "adapter_interface_spec_sha256": model[
                "adapter_interface_spec_sha256"
            ],
            "team_background_generator_manifest_sha256": None,
            "full_scenario_adapter_sha256": None,
            "ordered_execution_fidelity_sha256": None,
            "paired_rollout_identity_sha256": None,
        },
        "satisfied_conditions": satisfied,
        "unsatisfied_conditions": [
            value for value in RUNNER_REQUIRED_CONDITIONS if value not in satisfied
        ],
        "typed_blockers": blockers,
        "runner_receipt": None,
        "runner_receipt_emitted": False,
        "artifact_ready": False,
        "readiness_conditions_satisfied": False,
        "comparison_ready_by_itself": False,
        "promotion_rule": (
            "a later revision must independently load/recompute runtime adapter, "
            "ordered fidelity, and paired identity artifacts; booleans or digest-only "
            "self-reports are never accepted"
        ),
        "claim_boundary": {
            "historical_policy_fit_executed": True,
            "candidate_training_executed": False,
            "policy_comparison_executed": False,
            "runner_dispatch_authorized": False,
            "superiority_claim": False,
        },
    }
    admission = _content_addressed(admission_core)
    validate_admission_manifest(
        admission,
        model_content_sha256=model_sha,
        evaluation_content_sha256=evaluation_sha,
        prefix_receipt_content_sha256=prefix_sha,
    )

    prefix_path, prefix_addressed = _publish_pair(
        output, stem="prefix_receipt", document=prefix_receipt
    )
    model_path, model_addressed = _publish_pair(
        output, stem="model", document=model
    )
    evaluation_path, evaluation_addressed = _publish_pair(
        output, stem="evaluation", document=evaluation
    )
    admission_path, admission_addressed = _publish_pair(
        output, stem="admission", document=admission
    )
    return HistoricalFuryPolicyV2Result(
        model_path=model_path,
        model_content_addressed_path=model_addressed,
        evaluation_path=evaluation_path,
        evaluation_content_addressed_path=evaluation_addressed,
        prefix_receipt_path=prefix_path,
        prefix_receipt_content_addressed_path=prefix_addressed,
        admission_path=admission_path,
        admission_content_addressed_path=admission_addressed,
        admission_status=STATUS_BLOCKED,
        runner_receipt_emitted=False,
        blockers=tuple(blockers),
    )


def _load_document(path_value: str | Path, *, kind: str, label: str) -> JSONMap:
    path = Path(path_value).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExternalHistoricalFuryPolicyV2Error(
            "ARTIFACT_READ_FAILED", f"cannot read {label} {path}: {error}"
        ) from error
    value = deepcopy(dict(_mapping(document, label)))
    if value.get("kind") != kind:
        raise ExternalHistoricalFuryPolicyV2Error(
            "ARTIFACT_KIND_MISMATCH", f"{label} has the wrong kind"
        )
    if path.read_bytes() != _canonical_bytes(value) + b"\n":
        raise ExternalHistoricalFuryPolicyV2Error(
            "ARTIFACT_NONCANONICAL", f"{label} is not canonical JSON"
        )
    return value


def load_policy_model(path_value: str | Path) -> JSONMap:
    value = _load_document(path_value, kind=MODEL_KIND, label="policy model")
    validate_policy_model(value)
    return value


def load_admission_manifest(path_value: str | Path) -> JSONMap:
    value = _load_document(
        path_value, kind=ADMISSION_KIND, label="admission manifest"
    )
    validate_admission_manifest(value)
    return value


def _verify_published_pair(
    directory: Path, *, stem: str, document: Mapping[str, Any]
) -> tuple[Path, Path]:
    content_sha = _verify_content_address(document, stem)
    stable = directory / f"{stem}.json"
    addressed = directory / f"{stem}.{content_sha}.json"
    payload = _canonical_bytes(document) + b"\n"
    for path, label in ((stable, "stable"), (addressed, "content-addressed")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.read_bytes() != payload
        ):
            raise ExternalHistoricalFuryPolicyV2Error(
                "ARTIFACT_PAIR_MISSING_OR_DIFFERENT",
                f"{stem} {label} artifact is missing, symlinked, or differs",
            )
    return stable, addressed


def load_external_historical_fury_policy_v2_bundle(
    admission_manifest_path: str | Path,
) -> JSONMap:
    """Reload the entire four-artifact physical closure from one directory."""

    requested = Path(admission_manifest_path).expanduser().resolve()
    admission = load_admission_manifest(requested)
    directory = requested.parent
    _verify_published_pair(directory, stem="admission", document=admission)
    model = _load_document(directory / "model.json", kind=MODEL_KIND, label="policy model")
    prefix = _load_document(
        directory / "prefix_receipt.json",
        kind=PREFIX_RECEIPT_KIND,
        label="prefix receipt",
    )
    evaluation = _load_document(
        directory / "evaluation.json",
        kind=EVALUATION_KIND,
        label="policy evaluation",
    )
    validate_policy_model(model)
    validate_prefix_receipt(prefix)
    model_sha = _verify_content_address(model, "policy model")
    prefix_sha = _verify_content_address(prefix, "prefix receipt")
    validate_policy_evaluation(evaluation, model_content_sha256=model_sha)
    evaluation_sha = _verify_content_address(evaluation, "policy evaluation")
    validate_admission_manifest(
        admission,
        model_content_sha256=model_sha,
        evaluation_content_sha256=evaluation_sha,
        prefix_receipt_content_sha256=prefix_sha,
    )
    _verify_published_pair(directory, stem="model", document=model)
    _verify_published_pair(directory, stem="prefix_receipt", document=prefix)
    _verify_published_pair(directory, stem="evaluation", document=evaluation)
    if (
        model.get("prefix_receipt_content_sha256") != prefix_sha
        or evaluation.get("prefix_receipt_content_sha256") != prefix_sha
        or prefix.get("source_bundle_sha256")
        != model.get("source", {}).get("source_bundle_sha256")
        or evaluation.get("source_bundle_sha256")
        != model.get("source", {}).get("source_bundle_sha256")
    ):
        raise ExternalHistoricalFuryPolicyV2Error(
            "ARTIFACT_BUNDLE_CROSS_BINDING_MISMATCH",
            "model/evaluation/prefix receipts do not share one source closure",
        )
    return {
        "admission": admission,
        "model": model,
        "evaluation": evaluation,
        "prefix_receipt": prefix,
        "bundle_content_sha256": _sha256_json(
            {
                "admission": _verify_content_address(admission, "admission"),
                "model": model_sha,
                "evaluation": evaluation_sha,
                "prefix_receipt": prefix_sha,
            }
        ),
    }


def materialize_runner_artifact_admission_receipt(
    admission_manifest_or_path: Mapping[str, Any] | str | Path,
) -> JSONMap:
    """Refuse digest/boolean self-promotion while independent evidence is absent."""

    if isinstance(admission_manifest_or_path, Mapping):
        admission = deepcopy(dict(admission_manifest_or_path))
        validate_admission_manifest(admission)
    else:
        admission = load_external_historical_fury_policy_v2_bundle(
            admission_manifest_or_path
        )["admission"]
    blockers = [
        deepcopy(dict(_mapping(value, "typed blocker")))
        for value in _array(admission.get("typed_blockers"), "typed_blockers")
    ]
    raise ExternalHistoricalFuryPolicyV2Error(
        "HISTORICAL_ARTIFACT_ADMISSION_BLOCKED",
        "runner-compatible historical receipt was not minted",
        details={"typed_blockers": blockers},
    )


def validate_runner_artifact_admission_receipt(
    receipt: Mapping[str, Any],
    *,
    admission_manifest_or_path: Mapping[str, Any] | str | Path,
) -> None:
    """Reject detached/self-reported runner receipts against this blocked build.

    A future revision may delegate to the runner-v3 receipt validator only
    after it can independently reload all three missing runtime artifacts.
    This revision intentionally has no code path which trusts the supplied
    receipt's booleans or digests.
    """

    _mapping(receipt, "runner artifact admission receipt")
    if isinstance(admission_manifest_or_path, Mapping):
        admission = deepcopy(dict(admission_manifest_or_path))
        validate_admission_manifest(admission)
    else:
        admission = load_external_historical_fury_policy_v2_bundle(
            admission_manifest_or_path
        )["admission"]
    raise ExternalHistoricalFuryPolicyV2Error(
        "DETACHED_RUNNER_RECEIPT_REJECTED",
        "this admission manifest did not independently mint a runner receipt",
        details={
            "admission_content_sha256": _verify_content_address(
                admission, "admission manifest"
            ),
            "typed_blockers": deepcopy(admission["typed_blockers"]),
        },
    )


def _runtime_contexts(observation: Mapping[str, Any]) -> dict[str, str]:
    if observation.get("schema") != "chronicle_external_v2_fury_prefix_observation/v1":
        raise ExternalHistoricalFuryPolicyV2Error(
            "RUNTIME_OBSERVATION_SCHEMA_MISMATCH",
            "runtime observation must use the frozen prefix schema",
        )
    _assert_no_future_keys(observation, "runtime_observation")
    target_count = _nonnegative_integer(
        observation.get("observed_target_count"), "observed_target_count"
    )
    dead_count = _nonnegative_integer(
        observation.get("observed_dead_target_count"), "observed_dead_target_count"
    )
    if dead_count > target_count:
        raise ExternalHistoricalFuryPolicyV2Error(
            "RUNTIME_TARGET_ACCOUNTING_MISMATCH", "dead targets exceed observed targets"
        )
    elapsed = _nonnegative_integer(
        observation.get("wave_elapsed_ms"), "wave_elapsed_ms"
    )
    background_damage = _nonnegative_integer(
        observation.get("background_damage"), "background_damage"
    )
    background_dps = _finite_nonnegative_number(
        observation.get("background_dps"), "background_dps"
    )
    prefix_events = _nonnegative_integer(
        observation.get("prefix_event_count"), "prefix_event_count"
    )
    prefix_damage = _nonnegative_integer(
        observation.get("prefix_damage"), "prefix_damage"
    )
    last_spell = observation.get("last_prefix_action_spell")
    if last_spell != "NONE":
        last_spell = _canonical_bytes(_spell_identity(last_spell)).decode("utf-8")
    coarse = {
        "wave_elapsed_bucket": _elapsed_bucket(elapsed),
        "observed_target_count_bucket": _count_bucket(target_count),
        "observed_dead_target_count_bucket": _count_bucket(dead_count),
    }
    tactical = {
        **coarse,
        "background_dps_bucket": _log_bucket(background_dps),
        "actor_has_last_target": observation.get("actor_has_last_target") is True,
        "last_prefix_action_spell": last_spell,
    }
    full = {
        **tactical,
        "prefix_event_count_bucket": _count_bucket(prefix_events),
        "prefix_damage_bucket": _log_bucket(float(prefix_damage)),
        "background_damage_bucket": _log_bucket(float(background_damage)),
    }
    return {
        "coarse": _canonical_bytes(coarse).decode("utf-8"),
        "tactical": _canonical_bytes(tactical).decode("utf-8"),
        "full": _canonical_bytes(full).decode("utf-8"),
    }


def predict_action_distribution(
    model_or_path: Mapping[str, Any] | str | Path,
    *,
    observation: Mapping[str, Any],
    legal_actions: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Condition the nonvoting Fury model on caller-supplied legal actions."""

    if isinstance(model_or_path, Mapping):
        model = deepcopy(dict(model_or_path))
        validate_policy_model(model)
    else:
        model = load_policy_model(model_or_path)
    contexts = _runtime_contexts(observation)
    fury = _mapping(_mapping(model.get("lanes"), "model.lanes").get(FURY_LANE), FURY_LANE)
    fitted = _mapping(fury.get("fitted_policy"), "Fury fitted policy")
    if _nonnegative_integer(fitted.get("decision_count"), "decision_count") == 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "EMPTY_FURY_POLICY", "Fury model has no eligible action decisions"
        )
    learned, matched, unknown = _distribution(fitted, contexts)
    candidates: list[JSONMap] = []
    for index, raw in enumerate(legal_actions):
        candidate = _mapping(raw, f"legal_actions[{index}]")
        if candidate.get("legal") is False:
            continue
        spell = candidate.get("spell")
        target_role = candidate.get("target_role")
        if target_role not in {
            "NO_EXPLICIT_TARGET",
            "SELF",
            "CURRENT_ENEMY",
            "OTHER_OR_NEW_ENEMY",
            "OTHER_FRIENDLY",
        }:
            raise ExternalHistoricalFuryPolicyV2Error(
                "RUNTIME_TARGET_ROLE_UNSUPPORTED",
                f"legal action {index} has unsupported target_role",
            )
        action_key = _canonical_bytes(
            {**_spell_identity(spell), "target_role": target_role}
        ).decode("utf-8")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            candidate_id = _sha256_json({"index": index, "action_key": action_key})
        candidates.append(
            {
                "candidate_index": index,
                "candidate_id": candidate_id,
                "action_key": action_key,
                "command": deepcopy(candidate.get("command")),
            }
        )
    if not candidates:
        raise ExternalHistoricalFuryPolicyV2Error(
            "NO_LEGAL_ACTIONS", "no legal action candidates were supplied"
        )
    multiplicity = Counter(value["action_key"] for value in candidates)
    weights = [
        learned.get(value["action_key"], unknown) / multiplicity[value["action_key"]]
        for value in candidates
    ]
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        raise ExternalHistoricalFuryPolicyV2Error(
            "LEGAL_ACTION_MASS_ZERO", "legal action conditioning has no probability mass"
        )
    rows = [
        {**candidate, "probability": weight / total}
        for candidate, weight in zip(candidates, weights, strict=True)
    ]
    return {
        "schema": SCHEMA,
        "kind": "chronicle_external_historical_fury_action_distribution",
        "policy_id": POLICY_ID,
        "model_content_sha256": _verify_content_address(model, "policy model"),
        "comparison_status": "NOT_COMPARISON_READY",
        "context_match_levels": list(matched),
        "candidate_distribution": rows,
        "probability_sum": sum(row["probability"] for row in rows),
    }


def sample_legal_action(
    model_or_path: Mapping[str, Any] | str | Path,
    *,
    observation: Mapping[str, Any],
    legal_actions: Sequence[Mapping[str, Any]],
    seed: Any,
) -> JSONMap:
    distribution = predict_action_distribution(
        model_or_path, observation=observation, legal_actions=legal_actions
    )
    seed_material = {
        "model_content_sha256": distribution["model_content_sha256"],
        "policy_id": POLICY_ID,
        "seed": seed,
        "observation": deepcopy(dict(observation)),
        "candidate_ids": [
            row["candidate_id"] for row in distribution["candidate_distribution"]
        ],
    }
    digest = hashlib.sha256(_canonical_bytes(seed_material)).digest()
    uniform = int.from_bytes(digest, "big") / float(1 << (8 * len(digest)))
    selected = distribution["candidate_distribution"][-1]
    cumulative = 0.0
    for row in distribution["candidate_distribution"]:
        cumulative += float(row["probability"])
        if uniform < cumulative:
            selected = row
            break
    return {
        "schema": SCHEMA,
        "kind": "chronicle_external_historical_fury_action_sample",
        "policy_id": POLICY_ID,
        "model_content_sha256": distribution["model_content_sha256"],
        "comparison_status": "NOT_COMPARISON_READY",
        "seed_sha256": _sha256_json(seed_material),
        "candidate_id": selected["candidate_id"],
        "candidate_index": selected["candidate_index"],
        "probability": selected["probability"],
        "command": deepcopy(selected["command"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile the current External-V2 historical Fury policy candidate "
            "and its fail-closed admission manifest"
        )
    )
    parser.add_argument(
        "--team-wave-model-manifest", type=Path, default=DEFAULT_INPUT_MANIFEST
    )
    parser.add_argument(
        "--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY
    )
    parser.add_argument("--fold-count", type=int, default=DEFAULT_FOLD_COUNT)
    parser.add_argument("--split-seed", type=int, default=DEFAULT_SPLIT_SEED)
    parser.add_argument(
        "--smoothing-alpha", type=float, default=DEFAULT_SMOOTHING_ALPHA
    )
    parser.add_argument(
        "--backoff-strength", type=float, default=DEFAULT_BACKOFF_STRENGTH
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_external_historical_fury_policy_v2(
        team_wave_model_manifest_path=args.team_wave_model_manifest,
        output_directory=args.output_directory,
        fold_count=args.fold_count,
        split_seed=args.split_seed,
        smoothing_alpha=args.smoothing_alpha,
        backoff_strength=args.backoff_strength,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
