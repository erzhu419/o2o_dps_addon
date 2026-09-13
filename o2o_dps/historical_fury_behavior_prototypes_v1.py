"""Prepare five performance-routed Fury behavior observation prototypes.

This module consumes the frozen post-fix Fury cohort and the exact-window
observation episodes.  It materializes four repeat-player memberships whose
raid-local performance is consistently above the raid median, plus one pooled
top-quartile membership.  Selection is fixed before any simulator result is
seen.

Only recognized server-observed START events are weighted observations.  GO
and FAIL remain outcomes, client/next-swing queue intent remains unknown, and
the final interval after the last recognized controllable START of every wave
is emitted as an explicit right-censored inter-decision tail.  Unmapped or
passive START observations never shorten that interval.  The output is not a
trained or executable policy and does not authorize comparison, deployment,
or a superiority claim.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence, TextIO

from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import historical_behavior_clone_v1 as pooled_v1
from . import historical_fury_expert_cohort_v2 as cohort_v2
from . import historical_fury_expert_episode_adapter_v1 as episode_v1


JSONMap = dict[str, Any]
MANIFEST_SCHEMA = "historical_fury_behavior_prototypes/v1"
RECORD_SCHEMA = "historical_fury_behavior_prototype_observation/v1"
IMPLEMENTATION_REVISION = (
    "v1.2_immutable_episode_closure_and_explicit_delay_observation_status"
)
STATUS = "WEIGHTED_HISTORICAL_OBSERVATION_PREPARATION_ONLY_NOT_POLICY"
STABLE_REPEAT_COUNT = 4
STABLE_REPEAT_MIN_RAIDS = 2
STABLE_RAID_RATIO_EXCLUSIVE_FLOOR = 1.0
Q4_QUANTILE_NUMERATOR = 3
Q4_QUANTILE_DENOMINATOR = 4
WEIGHT_CONTRACT = (
    "PLAYER_EQUAL_THEN_RAID_EQUAL_WITHIN_PLAYER_THEN_RECOGNIZED_START_EQUAL_"
    "WITHIN_PLAYER_RAID"
)
QUEUE_INTENT_STATUS = "UNKNOWN_NOT_OBSERVED_OR_INFERRED"
DELAY_COMPLETED = "COMPLETED"
DELAY_LEFT_TRUNCATED = "LEFT_TRUNCATED_NOT_COMPLETED"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_COHORT = episode_v1.DEFAULT_COHORT
DEFAULT_EPISODE_MANIFEST = episode_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_behavior_prototypes"
    / "v1"
)

ACTION_KEYS = frozenset(spec.action_key for spec in policy_v3.ACTION_ONTOLOGY)


class HistoricalFuryBehaviorPrototypesV1Error(RuntimeError):
    """The frozen membership, episode, or weighting contract is violated."""


@dataclass(frozen=True)
class PrototypeBuildResult:
    manifest: Path
    content_addressed_manifest: Path
    prototype_count: int
    prepared_start_observation_count: int
    right_censored_tail_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": MANIFEST_SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "prototype_count": self.prototype_count,
            "prepared_start_observation_count": (
                self.prepared_start_observation_count
            ),
            "right_censored_tail_count": self.right_censored_tail_count,
        }


@dataclass(frozen=True)
class _Member:
    candidate_id: str
    character_guid: str
    latest_character_name: str
    raid_ids: tuple[str, ...]
    equal_raid_performance_ratio: float
    raid_performance: tuple[tuple[str, float], ...]
    right_censored_after_last_observed_raid: bool


@dataclass(frozen=True)
class _Prototype:
    prototype_id: str
    family: str
    selection_rule: str
    members: tuple[_Member, ...]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFuryBehaviorPrototypesV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryBehaviorPrototypesV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFuryBehaviorPrototypesV1Error(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"{label} must be at least {minimum}"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFuryBehaviorPrototypesV1Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise HistoricalFuryBehaviorPrototypesV1Error(f"{label} must be finite")
    return result


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"cannot read {path}: {error}"
        ) from error
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"cannot read {label}: {error}"
        ) from error
    return deepcopy(dict(_mapping(value, label)))


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


def _validate_episode_manifest_identity(
    path: Path, manifest: Mapping[str, Any]
) -> str:
    content_address = _mapping(
        manifest.get("content_address"), "episode manifest content_address"
    )
    observed = _text(
        content_address.get("sha256"), "episode manifest content_address.sha256"
    )
    core = deepcopy(dict(manifest))
    core.pop("content_address", None)
    expected = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    if observed != expected:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "episode manifest content_address does not bind its canonical content"
        )

    stable = path.parent / "manifest.json"
    addressed = path.parent / (
        f"historical_fury_expert_episode_adapter_v1.{observed}.manifest.json"
    )
    if stable.is_file() and addressed.is_file():
        try:
            stable_bytes = stable.read_bytes()
            addressed_bytes = addressed.read_bytes()
        except OSError as error:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"cannot compare stable/addressed episode manifests: {error}"
            ) from error
        if stable_bytes != addressed_bytes:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "stable and content-addressed episode manifest bytes differ"
            )
    return observed


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _under_offline_data(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "generated prototype output must stay under ignored offline_data"
        )
    return resolved


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _nearest_rank_q75(values: Sequence[float]) -> float:
    if not values:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "Q4 routing requires at least one comparable player"
        )
    ordered = sorted(values)
    rank = math.ceil(
        len(ordered) * Q4_QUANTILE_NUMERATOR / Q4_QUANTILE_DENOMINATOR
    )
    return ordered[rank - 1]


def _member_from_candidate(candidate: Mapping[str, Any]) -> _Member | None:
    candidate_id = _text(candidate.get("candidate_id"), "candidate_id")
    guid = _text(candidate.get("character_guid"), "character_guid").casefold()
    latest_name = _text(
        candidate.get("latest_character_name"), "latest_character_name"
    )
    raids = _array(candidate.get("raids"), "candidate.raids")
    declared_raid_count = _integer(
        candidate.get("raid_count"), "candidate.raid_count", minimum=1
    )
    if declared_raid_count != len(raids):
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"{candidate_id} raid_count differs from raids"
        )
    raid_rows: list[tuple[str, float]] = []
    seen_raids: set[str] = set()
    for raw_raid in raids:
        raid = _mapping(raw_raid, "candidate raid")
        instance_id = _text(raid.get("instance_id"), "raid.instance_id")
        if instance_id in seen_raids:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"{candidate_id} has duplicate raid membership"
            )
        seen_raids.add(instance_id)
        raw_ratio = raid.get("median_local_fury_dps_ratio")
        if raw_ratio is None:
            continue
        raid_rows.append(
            (
                instance_id,
                _finite_number(raw_ratio, "raid.median_local_fury_dps_ratio"),
            )
        )
    if not raid_rows:
        return None
    derived_ratio = _median([ratio for _, ratio in raid_rows])
    declared_ratio = candidate.get("median_equal_raid_local_fury_dps_ratio")
    if declared_ratio is None or not math.isclose(
        derived_ratio,
        _finite_number(
            declared_ratio, "candidate.median_equal_raid_local_fury_dps_ratio"
        ),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"{candidate_id} equal-raid performance statistic differs from raids"
        )
    return _Member(
        candidate_id=candidate_id,
        character_guid=guid,
        latest_character_name=latest_name,
        raid_ids=tuple(
            sorted(
                _text(_mapping(row, "candidate raid").get("instance_id"), "raid.instance_id")
                for row in raids
            )
        ),
        equal_raid_performance_ratio=derived_ratio,
        raid_performance=tuple(sorted(raid_rows)),
        right_censored_after_last_observed_raid=(
            candidate.get("right_censored_after_last_observed_raid") is True
        ),
    )


def derive_prototype_memberships_v1(
    cohort: Mapping[str, Any],
) -> tuple[list[JSONMap], JSONMap]:
    """Derive exactly four stable repeat profiles and one player-equal Q4 pool."""

    candidates = [
        _mapping(value, "player candidate")
        for value in _array(cohort.get("player_candidates"), "player_candidates")
    ]
    members: list[_Member] = []
    candidate_by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        candidate_id = _text(candidate.get("candidate_id"), "candidate_id")
        if candidate_id in candidate_by_id:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "duplicate player candidate_id"
            )
        candidate_by_id[candidate_id] = candidate
        member = _member_from_candidate(candidate)
        if member is not None:
            members.append(member)
    members.sort(key=lambda member: member.candidate_id)

    stable = []
    for member in members:
        comparable = dict(member.raid_performance)
        if (
            len(member.raid_ids) >= STABLE_REPEAT_MIN_RAIDS
            and len(comparable) == len(member.raid_ids)
            and all(
                comparable[raid_id] > STABLE_RAID_RATIO_EXCLUSIVE_FLOOR
                for raid_id in member.raid_ids
            )
        ):
            stable.append(member)
    if len(stable) != STABLE_REPEAT_COUNT:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "frozen per-raid stability rule must derive exactly four repeat players; "
            f"derived {len(stable)}"
        )

    q75 = _nearest_rank_q75(
        [member.equal_raid_performance_ratio for member in members]
    )
    q4 = tuple(
        member
        for member in members
        if member.equal_raid_performance_ratio >= q75
    )
    if not q4:
        raise HistoricalFuryBehaviorPrototypesV1Error("Q4 membership is empty")

    prototypes: list[_Prototype] = []
    for member in stable:
        suffix = hashlib.sha256(member.candidate_id.encode("utf-8")).hexdigest()[:16]
        prototypes.append(
            _Prototype(
                prototype_id=f"stable_repeat_player_{suffix}",
                family="PLAYER_CONDITIONED_STABLE_REPEAT_RAID",
                selection_rule=(
                    "raid_count>=2 AND every selected raid has a non-null "
                    "median_local_fury_dps_ratio strictly greater than 1.0"
                ),
                members=(member,),
            )
        )
    prototypes.append(
        _Prototype(
            prototype_id="player_equal_q4_pooled",
            family="PERFORMANCE_STRATIFIED_PLAYER_EQUAL_Q4_POOL",
            selection_rule=(
                "derived equal-raid player median >= inclusive nearest-rank Q75; "
                "ties at the cutoff retained"
            ),
            members=q4,
        )
    )
    prototypes.sort(key=lambda prototype: prototype.prototype_id)
    if len(prototypes) != STABLE_REPEAT_COUNT + 1:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "exactly five new prototype memberships are required"
        )

    def member_wire(member: _Member) -> JSONMap:
        return {
            "candidate_id": member.candidate_id,
            "character_guid": member.character_guid,
            "latest_character_name": member.latest_character_name,
            "raid_ids": list(member.raid_ids),
            "raid_count": len(member.raid_ids),
            "derived_median_equal_raid_local_fury_dps_ratio": (
                member.equal_raid_performance_ratio
            ),
            "per_raid_median_local_fury_dps_ratio": [
                {"instance_id": instance_id, "ratio": ratio}
                for instance_id, ratio in member.raid_performance
            ],
            "right_censored_after_last_observed_raid": (
                member.right_censored_after_last_observed_raid
            ),
            "performance_role": "PRE_SIMULATOR_ROUTING_ONLY_NOT_SAMPLE_WEIGHT",
        }

    return (
        [
            {
                "prototype_id": prototype.prototype_id,
                "prototype_family": prototype.family,
                "selection_rule": prototype.selection_rule,
                "member_count": len(prototype.members),
                "members": [member_wire(member) for member in prototype.members],
            }
            for prototype in prototypes
        ],
        {
            "performance_source": (
                "per-raid median_local_fury_dps_ratio from frozen cohort"
            ),
            "stable_repeat_exclusive_floor": (
                STABLE_RAID_RATIO_EXCLUSIVE_FLOOR
            ),
            "q4_quantile": "inclusive nearest-rank Q75 with cutoff ties retained",
            "q4_comparable_player_count": len(members),
            "q4_cutoff": q75,
            "simulator_results_used": False,
            "names_used_for_selection": False,
        },
    )


def _internal_prototypes(rows: Sequence[Mapping[str, Any]]) -> list[_Prototype]:
    result = []
    for row in rows:
        members = []
        for raw_member in _array(row.get("members"), "prototype.members"):
            member = _mapping(raw_member, "prototype member")
            performance = tuple(
                (
                    _text(_mapping(value, "raid performance").get("instance_id"), "instance_id"),
                    _finite_number(
                        _mapping(value, "raid performance").get("ratio"), "ratio"
                    ),
                )
                for value in _array(
                    member.get("per_raid_median_local_fury_dps_ratio"),
                    "per-raid performance",
                )
            )
            members.append(
                _Member(
                    candidate_id=_text(member.get("candidate_id"), "candidate_id"),
                    character_guid=_text(
                        member.get("character_guid"), "character_guid"
                    ).casefold(),
                    latest_character_name=_text(
                        member.get("latest_character_name"), "latest_character_name"
                    ),
                    raid_ids=tuple(
                        _text(value, "raid_id")
                        for value in _array(member.get("raid_ids"), "raid_ids")
                    ),
                    equal_raid_performance_ratio=_finite_number(
                        member.get(
                            "derived_median_equal_raid_local_fury_dps_ratio"
                        ),
                        "derived player ratio",
                    ),
                    raid_performance=performance,
                    right_censored_after_last_observed_raid=(
                        member.get("right_censored_after_last_observed_raid") is True
                    ),
                )
            )
        result.append(
            _Prototype(
                prototype_id=_text(row.get("prototype_id"), "prototype_id"),
                family=_text(row.get("prototype_family"), "prototype_family"),
                selection_rule=_text(row.get("selection_rule"), "selection_rule"),
                members=tuple(members),
            )
        )
    return result


def _episode_partition_path(
    episode_manifest_path: Path, partition: Mapping[str, Any]
) -> Path:
    raw = Path(_text(partition.get("path"), "partition.path"))
    return raw.resolve() if raw.is_absolute() else (episode_manifest_path.parent / raw).resolve()


def _iter_episode_partition(path: Path):
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise HistoricalFuryBehaviorPrototypesV1Error(
                        f"cannot decode {path}:{line_number}: {error}"
                    ) from error
                yield raw_line, _mapping(value, f"{path}:{line_number}")
    except (OSError, EOFError) as error:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"cannot read episode partition {path}: {error}"
        ) from error


def _validate_transition(
    raw: Any, *, label: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    transition = _mapping(raw, label)
    observed = _mapping(transition.get("observed_event"), f"{label}.observed_event")
    phase = _text(observed.get("phase"), f"{label}.phase").upper()
    if phase not in {"START", "GO", "FAIL"}:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            f"{label} has unsupported action phase {phase!r}"
        )
    policy_label = observed.get("policy_decision_label") is True
    if policy_label and phase != "START":
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "GO/FAIL can never become a policy label"
        )
    if observed.get("client_action_request_observed") is not False:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "client action request must remain unobserved"
        )
    if observed.get("client_next_swing_queue_intent_observed") is not False:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "next-swing queue intent must remain unobserved"
        )
    if policy_label:
        if observed.get("server_observed_start_proxy") is not True:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "a prepared START label is not a server-observed START proxy"
            )
        action_key = _text(observed.get("action_key"), f"{label}.action_key")
        if action_key not in ACTION_KEYS:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"prepared START is outside the Fury ontology: {action_key}"
            )
        if (
            transition.get("feature_cutoff_is_strict_prefix") is not True
            or transition.get("current_event_present_in_state_before") is not False
            or transition.get("future_outcomes_in_state_before") is not False
        ):
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "prepared START does not preserve the strict-prefix cutoff"
            )
        _mapping(transition.get("state_before"), f"{label}.state_before")
    return transition, observed


@dataclass
class _SourceAudit:
    start_labels: Counter[tuple[str, str]]
    episode_counts: Counter[tuple[str, str]]
    wave_counts: Counter[tuple[str, str]]
    phase_counts: Counter[str]
    recognized_action_counts: Counter[str]
    completed_delay_counts: Counter[tuple[str, str]]
    left_truncated_first_start_counts: Counter[tuple[str, str]]
    usable_right_censor_counts: Counter[tuple[str, str]]
    unusable_left_right_truncated_tail_counts: Counter[tuple[str, str]]
    source_episode_count: int = 0
    source_wave_count: int = 0
    partition_count: int = 0
    complete_wave_coverage_episode_count: int = 0
    partial_wave_coverage_episode_count: int = 0
    unknown_or_conflicting_timeline_player_wave_count: int = 0
    left_unobserved_ms_across_episodes: int = 0
    right_unobserved_ms_across_episodes: int = 0
    maximum_left_unobserved_ms: int = 0
    maximum_right_unobserved_ms: int = 0


def _new_source_audit() -> _SourceAudit:
    return _SourceAudit(
        start_labels=Counter(),
        episode_counts=Counter(),
        wave_counts=Counter(),
        phase_counts=Counter(),
        recognized_action_counts=Counter(),
        completed_delay_counts=Counter(),
        left_truncated_first_start_counts=Counter(),
        usable_right_censor_counts=Counter(),
        unusable_left_right_truncated_tail_counts=Counter(),
    )


def _selected_membership_index(
    prototypes: Sequence[_Prototype],
) -> tuple[dict[str, _Member], set[tuple[str, str]]]:
    members: dict[str, _Member] = {}
    memberships: set[tuple[str, str]] = set()
    for prototype in prototypes:
        for member in prototype.members:
            existing = members.get(member.character_guid)
            if existing is not None and existing.candidate_id != member.candidate_id:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "one GUID maps to conflicting candidate identities"
                )
            members[member.character_guid] = member
            memberships.update(
                (member.character_guid, instance_id)
                for instance_id in member.raid_ids
            )
    return members, memberships


def _episode_identity(
    episode: Mapping[str, Any],
) -> tuple[str, str, str, Mapping[str, Any]]:
    if episode.get("schema") != episode_v1.SCHEMA:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "episode record schema is unsupported"
        )
    instance_id = _text(episode.get("instance_id"), "episode.instance_id")
    player = _mapping(episode.get("player"), "episode.player")
    guid = _text(player.get("guid"), "episode.player.guid").casefold()
    episode_id = _text(episode.get("episode_id"), "episode.episode_id")
    return guid, instance_id, episode_id, player


def _wave_begins_left_truncated(
    episode: Mapping[str, Any], wave: Mapping[str, Any]
) -> bool:
    coverage = _mapping(
        _mapping(episode.get("window_join"), "episode.window_join").get(
            "coverage"
        ),
        "episode.window_join.coverage",
    )
    left_unobserved = _integer(
        coverage.get("left_unobserved_ms"),
        "episode coverage left_unobserved_ms",
        minimum=0,
    )
    wave_ordinal = _integer(
        wave.get("wave_ordinal"), "wave.wave_ordinal", minimum=1
    )
    return left_unobserved > 0 or wave_ordinal > 1


def _scan_episode_sources(
    episode_manifest_path: Path,
    episode_manifest: Mapping[str, Any],
    selected_memberships: set[tuple[str, str]],
) -> _SourceAudit:
    audit = _new_source_audit()
    seen_instances: set[str] = set()
    seen_episode_ids: set[str] = set()
    for raw_partition in sorted(
        _array(episode_manifest.get("partitions"), "episode partitions"),
        key=lambda value: str(_mapping(value, "partition").get("instance_id")),
    ):
        partition = _mapping(raw_partition, "episode partition")
        instance_id = _text(partition.get("instance_id"), "partition.instance_id")
        if instance_id in seen_instances:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "duplicate episode partition instance_id"
            )
        seen_instances.add(instance_id)
        if partition.get("record_schema") != episode_v1.SCHEMA:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "episode partition record_schema differs"
            )
        audit.partition_count += 1
        path = _episode_partition_path(episode_manifest_path, partition)
        logical = hashlib.sha256()
        logical_size = 0
        record_count = 0
        partition_phase_counts: Counter[str] = Counter()
        partition_label_count = 0
        partition_complete_coverage = 0
        partition_partial_coverage = 0
        partition_unknown_spec_waves = 0
        partition_left_unobserved_ms = 0
        partition_right_unobserved_ms = 0
        for raw_line, episode in _iter_episode_partition(path):
            logical.update(raw_line)
            logical_size += len(raw_line)
            record_count += 1
            guid, observed_instance, episode_id, player = _episode_identity(episode)
            if observed_instance != instance_id:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "episode instance differs from its partition"
                )
            if episode_id in seen_episode_ids:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    f"duplicate global episode_id: {episode_id}"
                )
            seen_episode_ids.add(episode_id)
            audit.source_episode_count += 1
            audit.episode_counts[(guid, instance_id)] += 1
            selected = (guid, instance_id) in selected_memberships
            if selected:
                if str(player.get("window_level_spec") or "") != "Fury":
                    raise HistoricalFuryBehaviorPrototypesV1Error(
                        "selected episode is not exact window-level Fury"
                    )
            coverage = _mapping(
                _mapping(episode.get("window_join"), "episode.window_join").get(
                    "coverage"
                ),
                "episode.window_join.coverage",
            )
            partial = coverage.get("partial")
            if partial is True:
                partition_partial_coverage += 1
                audit.partial_wave_coverage_episode_count += 1
            elif partial is False:
                partition_complete_coverage += 1
                audit.complete_wave_coverage_episode_count += 1
            else:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "episode window coverage partial flag is invalid"
                )
            left_unobserved = _integer(
                coverage.get("left_unobserved_ms"),
                "episode coverage left_unobserved_ms",
                minimum=0,
            )
            right_unobserved = _integer(
                coverage.get("right_unobserved_ms"),
                "episode coverage right_unobserved_ms",
                minimum=0,
            )
            partition_left_unobserved_ms += left_unobserved
            partition_right_unobserved_ms += right_unobserved
            audit.left_unobserved_ms_across_episodes += left_unobserved
            audit.right_unobserved_ms_across_episodes += right_unobserved
            audit.maximum_left_unobserved_ms = max(
                audit.maximum_left_unobserved_ms, left_unobserved
            )
            audit.maximum_right_unobserved_ms = max(
                audit.maximum_right_unobserved_ms, right_unobserved
            )
            assessment = player.get("timeline_spec_assessment")
            if isinstance(assessment, Mapping):
                unknown_waves = _integer(
                    assessment.get("unknown_or_conflicting_timeline_wave_count", 0),
                    "timeline unknown/conflicting wave count",
                    minimum=0,
                )
                partition_unknown_spec_waves += unknown_waves
                audit.unknown_or_conflicting_timeline_player_wave_count += (
                    unknown_waves
                )
            waves = _array(episode.get("wave_observations"), "wave_observations")
            audit.source_wave_count += len(waves)
            if selected:
                audit.wave_counts[(guid, instance_id)] += len(waves)
            for wave_index, raw_wave in enumerate(waves):
                wave = _mapping(raw_wave, "wave observation")
                wave_begins_left_truncated = _wave_begins_left_truncated(
                    episode, wave
                )
                recognized_start_in_wave = 0
                transitions = _array(
                    wave.get("prefix_transitions"), "wave.prefix_transitions"
                )
                for transition_index, raw_transition in enumerate(transitions):
                    _, observed = _validate_transition(
                        raw_transition,
                        label=(
                            f"wave[{wave_index}].prefix_transitions[{transition_index}]"
                        ),
                    )
                    phase = _text(observed.get("phase"), "observed phase").upper()
                    audit.phase_counts[phase] += 1
                    partition_phase_counts[phase] += 1
                    if observed.get("policy_decision_label") is True:
                        action_key = _text(observed.get("action_key"), "action_key")
                        audit.recognized_action_counts[action_key] += 1
                        partition_label_count += 1
                        if selected:
                            audit.start_labels[(guid, instance_id)] += 1
                            if (
                                recognized_start_in_wave == 0
                                and wave_begins_left_truncated
                            ):
                                audit.left_truncated_first_start_counts[
                                    (guid, instance_id)
                                ] += 1
                            else:
                                audit.completed_delay_counts[
                                    (guid, instance_id)
                                ] += 1
                        recognized_start_in_wave += 1
                if selected:
                    if recognized_start_in_wave > 0 or not wave_begins_left_truncated:
                        audit.usable_right_censor_counts[(guid, instance_id)] += 1
                    else:
                        audit.unusable_left_right_truncated_tail_counts[
                            (guid, instance_id)
                        ] += 1
        expected_count = _integer(
            partition.get("record_count"), "partition.record_count", minimum=0
        )
        if record_count != expected_count:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"episode partition record_count differs for {instance_id}"
            )
        expected_size = _integer(
            partition.get("logical_size_bytes"),
            "partition.logical_size_bytes",
            minimum=0,
        )
        if logical_size != expected_size:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"episode partition logical size differs for {instance_id}"
            )
        if logical.hexdigest() != _text(
            partition.get("logical_content_sha256"),
            "partition.logical_content_sha256",
        ):
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"episode partition logical digest differs for {instance_id}"
            )
        declared_partition_totals = {
            "server_observed_start_count": partition_phase_counts["START"],
            "controllable_policy_label_count": partition_label_count,
            "unknown_or_conflicting_timeline_player_wave_count": (
                partition_unknown_spec_waves
            ),
            "complete_wave_coverage_episode_count": partition_complete_coverage,
            "partial_wave_coverage_episode_count": partition_partial_coverage,
            "left_unobserved_ms_across_episodes": partition_left_unobserved_ms,
            "right_unobserved_ms_across_episodes": partition_right_unobserved_ms,
        }
        for field, observed in declared_partition_totals.items():
            if _integer(
                partition.get(field), f"partition.{field}", minimum=0
            ) != observed:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    f"episode partition {field} differs for {instance_id}"
                )
    return audit


def _validate_episode_aggregate_closure(
    cohort: Mapping[str, Any],
    episode_manifest: Mapping[str, Any],
    audit: _SourceAudit,
) -> None:
    unresolved = _array(
        episode_manifest.get("unresolved_observations"),
        "episode unresolved_observations",
    )
    unresolved_count = len(unresolved)
    summary = _mapping(episode_manifest.get("summary"), "episode manifest summary")
    observed_totals = {
        "candidate_nonnull_encounter_episode_count": audit.source_episode_count,
        "instance_partition_count": audit.partition_count,
        "server_observed_start_count": audit.phase_counts["START"],
        "controllable_policy_label_count": sum(
            audit.recognized_action_counts.values()
        ),
        "complete_wave_coverage_episode_count": (
            audit.complete_wave_coverage_episode_count
        ),
        "partial_wave_coverage_episode_count": (
            audit.partial_wave_coverage_episode_count
        ),
        "unknown_or_conflicting_timeline_player_wave_count": (
            audit.unknown_or_conflicting_timeline_player_wave_count
        ),
        "maximum_left_unobserved_ms": audit.maximum_left_unobserved_ms,
        "maximum_right_unobserved_ms": audit.maximum_right_unobserved_ms,
        "unresolved_null_encounter_observation_count": unresolved_count,
        "selected_exact_fury_observation_count": (
            audit.source_episode_count + unresolved_count
        ),
    }
    for field, observed in observed_totals.items():
        if _integer(
            summary.get(field), f"episode summary.{field}", minimum=0
        ) != observed:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"episode manifest aggregate {field} differs from streamed rows"
            )

    cohort_summary = _mapping(cohort.get("summary"), "frozen cohort summary")
    cohort_selected = _integer(
        cohort_summary.get("selected_exact_fury_encounter_observation_count"),
        "cohort selected exact Fury observation count",
        minimum=0,
    )
    episode_selected = _integer(
        summary.get("selected_exact_fury_observation_count"),
        "episode selected exact Fury observation count",
        minimum=0,
    )
    if cohort_selected != episode_selected:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "episode aggregate no longer closes to the frozen Fury cohort"
        )

    expected_memberships: Counter[tuple[str, str]] = Counter()
    for raw_candidate in _array(
        cohort.get("player_candidates"), "cohort player_candidates"
    ):
        candidate = _mapping(raw_candidate, "cohort player candidate")
        guid = _text(
            candidate.get("character_guid"), "cohort character_guid"
        ).casefold()
        for raw_raid in _array(candidate.get("raids"), "cohort candidate raids"):
            raid = _mapping(raw_raid, "cohort candidate raid")
            expected_memberships[
                (guid, _text(raid.get("instance_id"), "cohort raid instance_id"))
            ] += _integer(
                raid.get("encounter_observation_count"),
                "cohort raid encounter_observation_count",
                minimum=0,
            )

    observed_memberships = Counter(audit.episode_counts)
    for raw_row in unresolved:
        row = _mapping(raw_row, "unresolved observation")
        observed_memberships[
            (
                _text(row.get("player_guid"), "unresolved player_guid").casefold(),
                _text(row.get("instance_id"), "unresolved instance_id"),
            )
        ] += 1
    if observed_memberships != expected_memberships:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "resolved plus unresolved episode membership counts differ from the "
            "frozen Fury cohort"
        )


class _PrototypeWriter:
    def __init__(self, directory: Path, prototype_id: str) -> None:
        self.directory = directory
        self.prototype_id = prototype_id
        self.raw = tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=f".{prototype_id}.",
            suffix=".jsonl.gz",
            delete=False,
        )
        self.temporary = Path(self.raw.name)
        self.compressed = gzip.GzipFile(
            filename="", mode="wb", fileobj=self.raw, mtime=0
        )
        self.logical = hashlib.sha256()
        self.logical_size = 0
        self.record_count = 0
        self.start_count = 0
        self.tail_count = 0
        self.completed_delay_count = 0
        self.left_truncated_first_start_count = 0
        self.usable_right_censor_count = 0
        self.unusable_left_right_truncated_tail_count = 0
        self.action_counts: Counter[str] = Counter()
        self.weight_mass = Fraction(0, 1)
        self.closed = False

    def write(self, row: Mapping[str, Any], *, denominator: int | None = None) -> None:
        payload = _canonical_bytes(row, newline=True)
        self.compressed.write(payload)
        self.logical.update(payload)
        self.logical_size += len(payload)
        self.record_count += 1
        record_type = row.get("record_type")
        if record_type == "weighted_server_observed_start":
            if denominator is None:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "weighted START has no exact denominator"
                )
            self.start_count += 1
            self.weight_mass += Fraction(1, denominator)
            delay_status = row.get("delay_observation_status")
            if delay_status == DELAY_COMPLETED:
                self.completed_delay_count += 1
            elif delay_status == DELAY_LEFT_TRUNCATED:
                self.left_truncated_first_start_count += 1
            else:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "weighted START has invalid delay observation status"
                )
            observed = _mapping(row.get("observed_start"), "observed_start")
            self.action_counts[str(observed["action_key"])] += 1
        elif record_type == "right_censored_wave_tail":
            if denominator is not None:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "right-censored tail cannot receive a START weight"
                )
            self.tail_count += 1
            delay_status = row.get("delay_observation_status")
            if delay_status == "RIGHT_CENSORED":
                self.usable_right_censor_count += 1
            elif delay_status == "LEFT_AND_RIGHT_TRUNCATED_NOT_USABLE":
                self.unusable_left_right_truncated_tail_count += 1
            else:
                raise HistoricalFuryBehaviorPrototypesV1Error(
                    "tail has invalid delay observation status"
                )
        else:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"unsupported output record_type {record_type!r}"
            )

    def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.compressed.close()
        finally:
            self.raw.close()
            self.temporary.unlink(missing_ok=True)

    def finish(self) -> JSONMap:
        if self.closed:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "prototype writer is already closed"
            )
        self.closed = True
        self.compressed.close()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        digest = self.logical.hexdigest()
        final = self.directory / f"{self.prototype_id}.{digest}.jsonl.gz"
        os.replace(self.temporary, final)
        return {
            "path": final.name,
            "record_schema": RECORD_SCHEMA,
            "record_count": self.record_count,
            "prepared_start_observation_count": self.start_count,
            "right_censored_tail_count": self.tail_count,
            "completed_delay_count": self.completed_delay_count,
            "left_truncated_first_start_count": (
                self.left_truncated_first_start_count
            ),
            "usable_right_censor_count": self.usable_right_censor_count,
            "unusable_left_right_truncated_tail_count": (
                self.unusable_left_right_truncated_tail_count
            ),
            "prepared_start_action_counts": dict(sorted(self.action_counts.items())),
            "prepared_start_weight_mass": {
                "numerator": self.weight_mass.numerator,
                "denominator": self.weight_mass.denominator,
                "value": float(self.weight_mass),
            },
            "logical_size_bytes": self.logical_size,
            "logical_content_sha256": digest,
            "compressed_size_bytes": final.stat().st_size,
            "compressed_file_sha256": _sha256_file(final),
            "gzip_mtime": 0,
        }


def _prototype_lookup(
    prototypes: Sequence[_Prototype],
) -> dict[str, list[_Prototype]]:
    result: dict[str, list[_Prototype]] = defaultdict(list)
    for prototype in prototypes:
        for member in prototype.members:
            result[member.character_guid].append(prototype)
    for values in result.values():
        values.sort(key=lambda value: value.prototype_id)
    return result


def _source_ref(
    episode: Mapping[str, Any], wave: Mapping[str, Any], guid: str
) -> JSONMap:
    exact = _mapping(episode.get("exact_dps_window"), "exact_dps_window")
    window_join = _mapping(episode.get("window_join"), "window_join")
    coverage = _mapping(window_join.get("coverage"), "window_join.coverage")
    return {
        "episode_id": _text(episode.get("episode_id"), "episode_id"),
        "instance_id": _text(episode.get("instance_id"), "instance_id"),
        "encounter_id": episode.get("encounter_id"),
        "ranking_record_id": exact.get("ranking_record_id"),
        "player_guid": guid,
        "wave_id": wave.get("wave_id"),
        "wave_ordinal": wave.get("wave_ordinal"),
        "encounter_ordinal": wave.get("encounter_ordinal"),
        "source_wave_content_sha256": wave.get("source_wave_content_sha256"),
        "episode_window_coverage": deepcopy(dict(coverage)),
    }


def _weight_wire(
    *, player_count: int, raid_count: int, start_count: int
) -> JSONMap:
    denominator = player_count * raid_count * start_count
    return {
        "contract": WEIGHT_CONTRACT,
        "numerator": 1,
        "denominator": denominator,
        "player_denominator": player_count,
        "raid_denominator_within_player": raid_count,
        "recognized_start_denominator_within_player_raid": start_count,
        "value": 1.0 / denominator,
        "performance_or_segment_multiplier_used": False,
        "training_application_authorized": False,
    }


def _start_row(
    *,
    prototype: _Prototype,
    member: _Member,
    episode: Mapping[str, Any],
    wave: Mapping[str, Any],
    transition: Mapping[str, Any],
    observed: Mapping[str, Any],
    start_count: int,
    delay_observation_status: str,
) -> tuple[JSONMap, int]:
    player_count = len(prototype.members)
    raid_count = len(member.raid_ids)
    denominator = player_count * raid_count * start_count
    return (
        {
            "schema": RECORD_SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "record_type": "weighted_server_observed_start",
            "status": STATUS,
            "prototype_id": prototype.prototype_id,
            "prototype_family": prototype.family,
            "source": _source_ref(episode, wave, member.character_guid),
            "player_candidate_id": member.candidate_id,
            "strict_prefix_state": deepcopy(
                dict(_mapping(transition.get("state_before"), "state_before"))
            ),
            "observed_start": deepcopy(dict(observed)),
            "delay_observation_status": delay_observation_status,
            "prepared_training_weight": _weight_wire(
                player_count=player_count,
                raid_count=raid_count,
                start_count=start_count,
            ),
            "observation_boundaries": {
                "policy_label_semantics": (
                    "recognized server-observed START proxy only"
                ),
                "go_fail_are_labels": False,
                "client_action_request_observed": False,
                "client_next_swing_queue_intent": QUEUE_INTENT_STATUS,
                "target_switch_intent": QUEUE_INTENT_STATUS,
                "strict_prefix": True,
                "actions_outside_reconstruction_waves": (
                    "MISSING_NOT_INFERRED; see source.episode_window_coverage"
                ),
            },
            "scientific_boundaries": {
                "weighted_observation_prepared": True,
                "training_authorized": False,
                "comparison_authorized": False,
                "full_policy_claim_authorized": False,
            },
        },
        denominator,
    )


def _tail_row(
    *,
    prototype: _Prototype,
    member: _Member,
    episode: Mapping[str, Any],
    wave: Mapping[str, Any],
    transitions: Sequence[Any],
) -> JSONMap:
    window = _mapping(wave.get("window"), "wave.window")
    first = _mapping(window.get("first_anchor"), "window.first_anchor")
    last = _mapping(
        window.get("last_context_anchor"), "window.last_context_anchor"
    )
    first_ms = _integer(first.get("timestamp_ms"), "first_anchor.timestamp_ms")
    end_ms = _integer(last.get("timestamp_ms"), "last_context_anchor.timestamp_ms")
    last_controllable_start: Mapping[str, Any] | None = None
    for index, raw_transition in enumerate(transitions):
        _, observed = _validate_transition(raw_transition, label=f"transition[{index}]")
        if observed.get("policy_decision_label") is True:
            last_controllable_start = observed
    if last_controllable_start is None:
        origin_ms = first_ms
        origin = "WAVE_FIRST_ANCHOR"
        preceding = None
    else:
        anchor = _mapping(
            last_controllable_start.get("anchor"),
            "last recognized controllable START anchor",
        )
        origin_ms = _integer(
            anchor.get("timestamp_ms"),
            "last recognized controllable START timestamp_ms",
        )
        origin = "LAST_RECOGNIZED_CONTROLLABLE_START"
        preceding = {
            "order_key": deepcopy(last_controllable_start.get("order_key")),
            "action_key": last_controllable_start.get("action_key"),
            "action_lane": last_controllable_start.get("action_lane"),
            "ontology_status": last_controllable_start.get("ontology_status"),
            "policy_decision_label": True,
        }
    tail_delay_status = (
        "LEFT_AND_RIGHT_TRUNCATED_NOT_USABLE"
        if last_controllable_start is None
        and _wave_begins_left_truncated(episode, wave)
        else "RIGHT_CENSORED"
    )
    if origin_ms < first_ms or origin_ms > end_ms:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "right-censored tail origin is outside the wave window"
        )
    return {
        "schema": RECORD_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "record_type": "right_censored_wave_tail",
        "status": STATUS,
        "prototype_id": prototype.prototype_id,
        "prototype_family": prototype.family,
        "source": _source_ref(episode, wave, member.character_guid),
        "player_candidate_id": member.candidate_id,
        "right_censored_tail": {
            "origin": origin,
            "origin_timestamp_ms": origin_ms,
            "window_end_timestamp_ms": end_ms,
            "duration_ms": end_ms - origin_ms,
            "preceding_recognized_controllable_start": preceding,
            "next_recognized_controllable_start_before_window_end": False,
            "later_unmapped_or_passive_starts_shorten_tail": False,
            "right_censored": True,
            "next_action_policy_label_observed": False,
        },
        "delay_observation_status": tail_delay_status,
        "prepared_training_weight": None,
        "tail_weight_status": "UNASSIGNED_PENDING_CENSOR_AWARE_COMPILER",
        "observation_boundaries": {
            "go_fail_are_labels": False,
            "client_action_request_observed": False,
            "client_next_swing_queue_intent": QUEUE_INTENT_STATUS,
            "target_switch_intent": QUEUE_INTENT_STATUS,
            "actions_outside_reconstruction_waves": (
                "MISSING_NOT_INFERRED; see source.episode_window_coverage"
            ),
        },
        "scientific_boundaries": {
            "right_censoring_observation_only": True,
            "training_authorized": False,
            "comparison_authorized": False,
            "full_policy_claim_authorized": False,
        },
    }


def _emit_prototype_partitions(
    *,
    output_directory: Path,
    episode_manifest_path: Path,
    episode_manifest: Mapping[str, Any],
    prototypes: Sequence[_Prototype],
    audit: _SourceAudit,
) -> dict[str, JSONMap]:
    writers = {
        prototype.prototype_id: _PrototypeWriter(
            output_directory, prototype.prototype_id
        )
        for prototype in prototypes
    }
    prototype_by_guid = _prototype_lookup(prototypes)
    member_by_guid, selected_memberships = _selected_membership_index(prototypes)
    try:
        for raw_partition in sorted(
            _array(episode_manifest.get("partitions"), "episode partitions"),
            key=lambda value: str(_mapping(value, "partition").get("instance_id")),
        ):
            partition = _mapping(raw_partition, "episode partition")
            path = _episode_partition_path(episode_manifest_path, partition)
            for _, episode in _iter_episode_partition(path):
                guid, instance_id, _, _ = _episode_identity(episode)
                if (guid, instance_id) not in selected_memberships:
                    continue
                member = member_by_guid[guid]
                matching = prototype_by_guid[guid]
                waves = _array(episode.get("wave_observations"), "wave_observations")
                for raw_wave in waves:
                    wave = _mapping(raw_wave, "wave observation")
                    wave_begins_left_truncated = _wave_begins_left_truncated(
                        episode, wave
                    )
                    recognized_start_in_wave = 0
                    transitions = _array(
                        wave.get("prefix_transitions"), "wave.prefix_transitions"
                    )
                    for index, raw_transition in enumerate(transitions):
                        transition, observed = _validate_transition(
                            raw_transition, label=f"prefix_transitions[{index}]"
                        )
                        if observed.get("policy_decision_label") is not True:
                            continue
                        delay_observation_status = (
                            DELAY_LEFT_TRUNCATED
                            if recognized_start_in_wave == 0
                            and wave_begins_left_truncated
                            else DELAY_COMPLETED
                        )
                        recognized_start_in_wave += 1
                        start_count = audit.start_labels[(guid, instance_id)]
                        for prototype in matching:
                            row, denominator = _start_row(
                                prototype=prototype,
                                member=member,
                                episode=episode,
                                wave=wave,
                                transition=transition,
                                observed=observed,
                                start_count=start_count,
                                delay_observation_status=(
                                    delay_observation_status
                                ),
                            )
                            writers[prototype.prototype_id].write(
                                row, denominator=denominator
                            )
                    for prototype in matching:
                        writers[prototype.prototype_id].write(
                            _tail_row(
                                prototype=prototype,
                                member=member,
                                episode=episode,
                                wave=wave,
                                transitions=transitions,
                            )
                        )
        return {
            prototype_id: writers[prototype_id].finish()
            for prototype_id in sorted(writers)
        }
    except Exception:
        for writer in writers.values():
            writer.abort()
        raise


def _member_support_wire(
    prototype: _Prototype, audit: _SourceAudit
) -> list[JSONMap]:
    player_count = len(prototype.members)
    rows = []
    for member in prototype.members:
        raids = []
        for instance_id in member.raid_ids:
            key = (member.character_guid, instance_id)
            labels = audit.start_labels[key]
            raids.append(
                {
                    "instance_id": instance_id,
                    "episode_count": audit.episode_counts[key],
                    "wave_count": audit.wave_counts[key],
                    "recognized_start_count": labels,
                    "completed_delay_count": audit.completed_delay_counts[key],
                    "left_truncated_first_start_count": (
                        audit.left_truncated_first_start_counts[key]
                    ),
                    "usable_right_censor_count": (
                        audit.usable_right_censor_counts[key]
                    ),
                    "unusable_left_right_truncated_tail_count": (
                        audit.unusable_left_right_truncated_tail_counts[key]
                    ),
                    "prepared_raid_weight_mass": {
                        "numerator": 1,
                        "denominator": player_count * len(member.raid_ids),
                        "value": 1.0 / (player_count * len(member.raid_ids)),
                    },
                }
            )
        rows.append(
            {
                "candidate_id": member.candidate_id,
                "character_guid": member.character_guid,
                "prepared_player_weight_mass": {
                    "numerator": 1,
                    "denominator": player_count,
                    "value": 1.0 / player_count,
                },
                "raids": raids,
            }
        )
    return rows


def build_historical_fury_behavior_prototypes_v1(
    *,
    cohort_path: str | Path = DEFAULT_COHORT,
    episode_manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> PrototypeBuildResult:
    """Materialize memberships and weighted START observations, but no policy."""

    cohort_file = Path(cohort_path).expanduser().resolve()
    episode_file = Path(episode_manifest_path).expanduser().resolve()
    destination = _under_offline_data(Path(output_directory))
    destination.mkdir(parents=True, exist_ok=True)

    cohort = _load_json(cohort_file, "frozen Fury cohort")
    try:
        cohort_v2.validate_cohort_document(cohort)
    except cohort_v2.HistoricalFuryExpertCohortError as error:
        raise HistoricalFuryBehaviorPrototypesV1Error(str(error)) from error
    membership_rows, selection_contract = derive_prototype_memberships_v1(cohort)
    prototypes = _internal_prototypes(membership_rows)

    episode_manifest = _load_json(episode_file, "Fury episode manifest")
    if episode_manifest.get("schema") != episode_v1.MANIFEST_SCHEMA:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "episode manifest schema is unsupported"
        )
    episode_content_sha = _validate_episode_manifest_identity(
        episode_file, episode_manifest
    )
    episode_boundaries = _mapping(
        episode_manifest.get("scientific_boundaries"),
        "episode scientific_boundaries",
    )
    for key in (
        "comparison_authorized",
        "training_authorized",
        "closed_loop_baseline_authorized",
        "superiority_claim_authorized",
    ):
        if episode_boundaries.get(key) is not False:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"episode boundary {key} must remain false"
            )
    episode_inputs = _mapping(
        episode_manifest.get("input_closure"), "episode input_closure"
    )
    frozen = _mapping(episode_inputs.get("frozen_cohort"), "frozen_cohort")
    cohort_sha = _sha256_file(cohort_file)
    if frozen.get("schema") != cohort_v2.SCHEMA or frozen.get("file_sha256") != cohort_sha:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "episode manifest is not bound to the supplied frozen cohort"
        )

    _, memberships = _selected_membership_index(prototypes)
    audit = _scan_episode_sources(episode_file, episode_manifest, memberships)
    _validate_episode_aggregate_closure(cohort, episode_manifest, audit)
    for membership in memberships:
        if audit.start_labels[membership] != (
            audit.completed_delay_counts[membership]
            + audit.left_truncated_first_start_counts[membership]
        ):
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "START delay-status accounting does not close"
            )
        if audit.wave_counts[membership] != (
            audit.usable_right_censor_counts[membership]
            + audit.unusable_left_right_truncated_tail_counts[membership]
        ):
            raise HistoricalFuryBehaviorPrototypesV1Error(
                "tail censor-status accounting does not close"
            )
    unsupported = [
        {"character_guid": guid, "instance_id": instance_id}
        for guid, instance_id in sorted(memberships)
        if audit.start_labels[(guid, instance_id)] == 0
    ]
    if unsupported:
        raise HistoricalFuryBehaviorPrototypesV1Error(
            "every selected player-raid needs at least one recognized START; "
            f"missing={unsupported}"
        )

    partitions = _emit_prototype_partitions(
        output_directory=destination,
        episode_manifest_path=episode_file,
        episode_manifest=episode_manifest,
        prototypes=prototypes,
        audit=audit,
    )
    materialized = []
    membership_by_id = {
        _text(row.get("prototype_id"), "prototype_id"): deepcopy(dict(row))
        for row in membership_rows
    }
    for prototype in prototypes:
        partition = partitions[prototype.prototype_id]
        if partition["prepared_start_weight_mass"] != {
            "numerator": 1,
            "denominator": 1,
            "value": 1.0,
        }:
            raise HistoricalFuryBehaviorPrototypesV1Error(
                f"prepared START weights do not close for {prototype.prototype_id}"
            )
        materialized.append(
            {
                **membership_by_id[prototype.prototype_id],
                "observation_support": _member_support_wire(prototype, audit),
                "partition": partition,
                "executable_policy_materialized": False,
            }
        )

    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "frozen_cohort": {
                "path": str(cohort_file),
                "schema": cohort.get("schema"),
                "file_sha256": cohort_sha,
            },
            "fury_episode_manifest": {
                "path": str(episode_file),
                "schema": episode_manifest.get("schema"),
                "file_sha256": _sha256_file(episode_file),
                "content_sha256": episode_content_sha,
            },
            "network_request_count": 0,
            "streaming_pass_count": 2,
            "episode_rows_retained_in_memory": 0,
        },
        "selection_contract": selection_contract,
        "prototype_count_contract": {
            "new_materialized_prototype_count": len(materialized),
            "stable_repeat_player_profile_count": STABLE_REPEAT_COUNT,
            "player_equal_q4_pool_count": 1,
            "selection_fixed_before_simulator_evaluation": True,
        },
        "weighting_contract": {
            "name": WEIGHT_CONTRACT,
            "per_start_formula": (
                "1 / prototype_player_count / player_raid_count / "
                "recognized_START_count_in_player_raid"
            ),
            "player_equal": True,
            "raid_equal_within_player": True,
            "recognized_start_equal_within_player_raid": True,
            "encounter_or_wave_multiplier_used": False,
            "performance_multiplier_used": False,
            "go_fail_weighted_as_labels": False,
            "right_censored_tail_weight": (
                "UNASSIGNED_PENDING_CENSOR_AWARE_COMPILER"
            ),
        },
        "action_and_censoring_contract": {
            "policy_label": "recognized server-observed START proxy only",
            "go_fail": "outcome audit only; never emitted as labels",
            "client_action_request": "not observed",
            "next_swing_queue_intent": QUEUE_INTENT_STATUS,
            "target_switch_intent": QUEUE_INTENT_STATUS,
            "start_delay_observation": (
                "COMPLETED except the first recognized START in a wave is "
                "LEFT_TRUNCATED_NOT_COMPLETED when left_unobserved_ms>0 or "
                "wave_ordinal>1; mark/target START weights are unchanged"
            ),
            "right_censored_tail": (
                "one explicit tail per selected player-wave, beginning at the "
                "last recognized controllable START (policy_decision_label=true) "
                "or wave start; later unmapped/passive START observations do not "
                "shorten the inter-decision censor interval"
            ),
            "tail_delay_observation": (
                "RIGHT_CENSORED when a recognized preceding START exists, or "
                "when an untruncated first-wave origin is the authoritative "
                "window start; otherwise LEFT_AND_RIGHT_TRUNCATED_NOT_USABLE"
            ),
            "outside_reconstruction_wave_actions": (
                "missing left/right episode-window coverage is preserved on every "
                "record and never inferred"
            ),
        },
        "external_controls": [
            {
                "control_id": pooled_v1.COHORT_ID,
                "schema": pooled_v1.MODEL_SCHEMA,
                "producer_module": "o2o_dps.historical_behavior_clone_v1",
                "role": "EXTERNAL_POOLED_V1_CONTROL",
                "materialized_by_this_artifact": False,
                "included_in_new_prototype_count": False,
                "modified_by_this_artifact": False,
            }
        ],
        "prototypes": materialized,
        "source_audit": {
            "episode_count": audit.source_episode_count,
            "globally_unique_episode_id_count": audit.source_episode_count,
            "wave_count": audit.source_wave_count,
            "episode_manifest_content_address_verified": True,
            "episode_manifest_stable_addressed_bytes_checked_if_both_present": True,
            "action_phase_counts": dict(sorted(audit.phase_counts.items())),
            "recognized_start_action_counts": dict(
                sorted(audit.recognized_action_counts.items())
            ),
        },
        "summary": {
            "new_prototype_count": len(materialized),
            "prepared_start_observation_count": sum(
                row["partition"]["prepared_start_observation_count"]
                for row in materialized
            ),
            "right_censored_tail_count": sum(
                row["partition"]["right_censored_tail_count"]
                for row in materialized
            ),
            "completed_delay_count": sum(
                row["partition"]["completed_delay_count"]
                for row in materialized
            ),
            "left_truncated_first_start_count": sum(
                row["partition"]["left_truncated_first_start_count"]
                for row in materialized
            ),
            "usable_right_censor_count": sum(
                row["partition"]["usable_right_censor_count"]
                for row in materialized
            ),
            "unusable_left_right_truncated_tail_count": sum(
                row["partition"][
                    "unusable_left_right_truncated_tail_count"
                ]
                for row in materialized
            ),
        },
        "scientific_boundaries": {
            "weighted_historical_observation_preparation_only": True,
            "executable_policy_materialized": False,
            "training_authorized": False,
            "comparison_authorized": False,
            "same_equipment_matched_seed_comparison_authorized": False,
            "closed_loop_baseline_authorized": False,
            "full_policy_claim_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
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
        f"historical_fury_behavior_prototypes_v1.{address}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return PrototypeBuildResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        prototype_count=len(materialized),
        prepared_start_observation_count=manifest_core["summary"][
            "prepared_start_observation_count"
        ],
        right_censored_tail_count=manifest_core["summary"][
            "right_censored_tail_count"
        ],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare four stable repeat-player plus one player-equal Q4 Fury "
            "behavior observation prototypes"
        )
    )
    parser.add_argument("--cohort", type=Path, default=DEFAULT_COHORT)
    parser.add_argument(
        "--episode-manifest", type=Path, default=DEFAULT_EPISODE_MANIFEST
    )
    parser.add_argument(
        "--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_fury_behavior_prototypes_v1(
        cohort_path=args.cohort,
        episode_manifest_path=args.episode_manifest,
        output_directory=args.output_directory,
    )
    rendered = _canonical_bytes(result.as_dict(), newline=True).decode("utf-8")
    if stdout is None:
        print(rendered, end="")
    else:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalFuryBehaviorPrototypesV1Error as error:
        print(str(error), file=os.sys.stderr)
        raise SystemExit(2)


__all__ = [
    "DEFAULT_COHORT",
    "DEFAULT_EPISODE_MANIFEST",
    "DEFAULT_OUTPUT_DIRECTORY",
    "HistoricalFuryBehaviorPrototypesV1Error",
    "IMPLEMENTATION_REVISION",
    "MANIFEST_SCHEMA",
    "PrototypeBuildResult",
    "QUEUE_INTENT_STATUS",
    "RECORD_SCHEMA",
    "STATUS",
    "WEIGHT_CONTRACT",
    "build_historical_fury_behavior_prototypes_v1",
    "derive_prototype_memberships_v1",
    "main",
]
