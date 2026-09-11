"""Build compact, attributable Chronicle team timelines for capsule waves.

The compiler joins four local evidence sources: the main-comparison scenario
capsule, the manual export queue, optional CombatantInfo sidecars, and the
normalized Chronicle JSONL stream.  It never opens or copies a raw CSV.  Each
normalized instance is scanned once into a temporary SQLite projection so
large raids do not have to be retained in memory; output is partitioned by
instance and committed before the manifest (manifest-last).

The result is descriptive historical evidence.  It does not infer client
keypresses, Cat/Contra authorship, target health, or causal action value.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping, Sequence


JSONMap = dict[str, Any]
SCHEMA = "chronicle_team_wave_timeline/v1"
SCHEMA_VERSION = 1
IMPLEMENTATION_REVISION = "v1.3_target_activity_range_bug_boundary_20260903_noon"
CAPSULE_SCHEMA = "fury_offline_scenario_capsules/v2"
SIDECAR_SCHEMA = "chronicle_combatant_info_sidecar/v1"
SIDECAR_RECORD_SCHEMA = "chronicle_combatant_info/v1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_CAPSULE_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "fury_offline_scenario_capsules" / "v2"
)
DEFAULT_EXPORT_QUEUE = DEFAULT_DATA_ROOT / "chronicle_raw" / "export_queue.json"
DEFAULT_SIDECAR_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_combatant_info_sidecar"
    / "v1"
    / "manifest.json"
)
DEFAULT_NORMALIZED_DIRECTORY = DEFAULT_DATA_ROOT / "normalized"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_team_wave_timeline" / "v1"
)

TIMELINE_SOURCE_TYPES = frozenset(("START", "GO", "FAIL", "DMG", "DEAD", "HEAL"))
ACTION_SOURCE_TYPES = frozenset(("START", "GO", "FAIL"))
DAMAGE_SOURCE_TYPES = frozenset(("DMG", "DEAD"))
ATTRIBUTION_KINDS = ("DIRECT_PLAYER", "OWNED_ENTITY", "UNATTRIBUTED")
OWNER_RE = re.compile(r"(?:^|\s)owner=([0-9A-F]+)(?:\s|$)", re.IGNORECASE)
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")

# China has used UTC+08:00 without daylight-saving transitions throughout the
# complete Chronicle observation period.  A fixed offset keeps the compiler
# functional on stock Windows Python installations that do not ship tzdata.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
LEGACY_SUSPECT_BEFORE_LOCAL = datetime(2026, 9, 3, 0, 0, 0, tzinfo=SHANGHAI)
LEGACY_POSTFIX_AT_OR_AFTER_LOCAL = datetime(
    2026, 9, 3, 12, 0, 0, tzinfo=SHANGHAI
)
LEGACY_GUILD = "南北"
LEGACY_GUILD_IDS = frozenset(("65a8fe4c-8023-4ed2-bcef-d14d0feacb6b",))
CONTAMINATION_RULE_VERSION = "south_north_36yd_started_at_v2_20260903_noon"


class ChronicleTeamWaveTimelineError(RuntimeError):
    """An input or derived timeline violates the V1 evidence contract."""


@dataclass(frozen=True)
class TargetSpec:
    guid: str
    target_index: int
    display_name: str | None
    activity_first_order: tuple[int, int, int]
    capsule_summary: Mapping[str, Any]
    capsule_kill_budget: Mapping[str, Any] | None


@dataclass(frozen=True)
class WaveSpec:
    instance_id: str
    encounter_id: str
    wave_id: str
    wave_ordinal: int
    scenario_id: str
    capsule_sha256: str
    absolute_start_ms: int
    absolute_end_ms: int
    targets: tuple[TargetSpec, ...]

    @property
    def key(self) -> str:
        return f"{self.encounter_id}\x1f{self.wave_id}"

    @property
    def identity(self) -> JSONMap:
        return {
            "instance_id": self.instance_id,
            "encounter_id": self.encounter_id,
            "wave_id": self.wave_id,
            "wave_ordinal": self.wave_ordinal,
        }


@dataclass(frozen=True)
class InstanceJob:
    instance_id: str
    queue_entry: Mapping[str, Any]
    normalized_path: Path
    expected_normalized_sha256: str | None
    sidecar_path: Path | None
    waves: tuple[WaveSpec, ...]
    capsule_file_sha256: str
    queue_file_sha256: str
    sidecar_manifest_file_sha256: str | None


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: JSONMap


@dataclass(frozen=True)
class ChronicleTeamWaveTimelineResult:
    manifest: Path
    content_addressed_manifest: Path
    partitions: tuple[Path, ...]
    instance_count: int
    wave_count: int
    event_count: int

    def as_dict(self) -> JSONMap:
        return {
            "status": "ok",
            "schema": SCHEMA,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "partitions": [str(path) for path in self.partitions],
            "instance_count": self.instance_count,
            "wave_count": self.wave_count,
            "event_count": self.event_count,
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
        raise ChronicleTeamWaveTimelineError(
            f"value is not canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleTeamWaveTimelineError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        opener = gzip.open if path.name.endswith(".gz") else path.open
        if path.name.endswith(".gz"):
            with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[arg-type]
                value = json.load(handle)
        else:
            with opener("r", encoding="utf-8") as handle:  # type: ignore[call-arg]
                value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleTeamWaveTimelineError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleTeamWaveTimelineError(f"{label} is not a JSON object: {path}")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleTeamWaveTimelineError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleTeamWaveTimelineError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleTeamWaveTimelineError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ChronicleTeamWaveTimelineError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ChronicleTeamWaveTimelineError(f"{label} must be an integer") from error


def _anchor_order(value: Any, label: str) -> tuple[int, int, int]:
    anchor = _mapping(value, label)
    return (
        _integer(anchor.get("offset_ms"), f"{label}.offset_ms"),
        _integer(anchor.get("event_index"), f"{label}.event_index"),
        _integer(anchor.get("csv_line"), f"{label}.csv_line"),
    )


def _nonnegative_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        rendered = str(value).strip().replace(",", "")
        if not rendered or rendered in {"-", "—"}:
            return None
        try:
            parsed = float(rendered)
        except ValueError:
            return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _guid(value: Any) -> str | None:
    rendered = _optional_text(value)
    if rendered is None:
        return None
    if rendered[:2].casefold() == "0x":
        return "0x" + rendered[2:].upper()
    return rendered.upper()


def _guid_key(value: Any) -> str:
    return (_guid(value) or "").casefold()


def _safe_component(value: str) -> str:
    rendered = SAFE_COMPONENT_RE.sub("-", value).strip("-._")
    if not rendered:
        raise ChronicleTeamWaveTimelineError(f"unsafe empty filename from {value!r}")
    return rendered


def _portable_path(path: Path) -> str:
    """Render a path without binding artifact identity to one workstation root."""

    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        # External test/site inputs retain a useful basename while deliberately
        # excluding the machine-specific parent directory from content identity.
        return f"$EXTERNAL/{resolved.name}"


def _resolve_default_capsule(directory: Path = DEFAULT_CAPSULE_DIRECTORY) -> Path:
    candidates = sorted(directory.glob("fury_offline_scenario_capsules_v2.*.json.gz"))
    if len(candidates) != 1:
        raise ChronicleTeamWaveTimelineError(
            f"expected exactly one current main capsule in {directory}, found {len(candidates)}; "
            "pass --capsule explicitly"
        )
    return candidates[0].resolve()


def _verify_declared_content_address(document: Mapping[str, Any], label: str) -> None:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    declared = _text(address.get("sha256"), f"{label}.content_address.sha256")
    core = {key: value for key, value in document.items() if key != "content_address"}
    actual = _sha256_json(core)
    if declared != actual:
        raise ChronicleTeamWaveTimelineError(
            f"{label} content address mismatch: declared {declared}, computed {actual}"
        )


def _wave_absolute_window(scenario: Mapping[str, Any]) -> tuple[int, int]:
    horizon = _integer(
        _mapping(scenario.get("horizon"), "scenario.horizon").get("milliseconds"),
        "scenario.horizon.milliseconds",
    )
    if horizon < 0:
        raise ChronicleTeamWaveTimelineError("scenario horizon cannot be negative")
    starts: set[int] = set()
    for target_index, raw_target in enumerate(
        _array(scenario.get("targets"), "scenario.targets")
    ):
        target = _mapping(raw_target, f"scenario.targets[{target_index}]")
        activity = _mapping(
            target.get("observed_hostile_activity_proxy"),
            f"scenario.targets[{target_index}].observed_hostile_activity_proxy",
        )
        first = _mapping(
            activity.get("first_anchor"),
            f"scenario.targets[{target_index}].activity.first_anchor",
        )
        absolute_first = _integer(
            first.get("offset_ms"),
            f"scenario.targets[{target_index}].activity.first_anchor.offset_ms",
        )
        relative_first = _integer(
            activity.get("start_ms"),
            f"scenario.targets[{target_index}].activity.start_ms",
        )
        starts.add(absolute_first - relative_first)
    if len(starts) != 1:
        raise ChronicleTeamWaveTimelineError(
            "target activity anchors do not agree on one absolute wave start: "
            f"{sorted(starts)}"
        )
    start = next(iter(starts))
    return start, start + horizon


def _capsule_waves(capsule: Mapping[str, Any]) -> tuple[WaveSpec, ...]:
    if capsule.get("schema") != CAPSULE_SCHEMA:
        raise ChronicleTeamWaveTimelineError(
            f"capsule schema must be {CAPSULE_SCHEMA}"
        )
    if capsule.get("bucket") != "main_comparison":
        raise ChronicleTeamWaveTimelineError(
            "only capsule bucket=main_comparison can build the team timeline"
        )
    _verify_declared_content_address(capsule, "capsule")
    waves: list[WaveSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for scenario_index, raw_scenario in enumerate(
        _array(capsule.get("scenarios"), "capsule.scenarios")
    ):
        scenario = _mapping(raw_scenario, f"capsule.scenarios[{scenario_index}]")
        identity = _mapping(
            scenario.get("source_identity"),
            f"capsule.scenarios[{scenario_index}].source_identity",
        )
        instance_id = _text(identity.get("instance_id"), "source_identity.instance_id")
        encounter_id = _text(
            identity.get("encounter_id"), "source_identity.encounter_id"
        )
        wave_id = _text(identity.get("wave_id"), "source_identity.wave_id")
        identity_key = (instance_id, encounter_id, wave_id)
        if identity_key in seen:
            raise ChronicleTeamWaveTimelineError(
                f"duplicate main scenario wave identity: {identity_key}"
            )
        seen.add(identity_key)
        targets: list[TargetSpec] = []
        target_guids: set[str] = set()
        for target_position, raw_target in enumerate(
            _array(scenario.get("targets"), "scenario.targets")
        ):
            target = _mapping(raw_target, f"scenario.targets[{target_position}]")
            target_guid = _guid(target.get("target_guid"))
            if target_guid is None or _guid_key(target_guid) in target_guids:
                raise ChronicleTeamWaveTimelineError(
                    f"missing or duplicate target GUID in scenario {wave_id}"
                )
            target_guids.add(_guid_key(target_guid))
            activity = _mapping(
                target.get("observed_hostile_activity_proxy"),
                f"scenario.targets[{target_position}].observed_hostile_activity_proxy",
            )
            max_health_family = target.get("max_health_hypothesis_family")
            kill_budget: Mapping[str, Any] | None = None
            if isinstance(max_health_family, Mapping):
                candidate = max_health_family.get("observed_kill_budget_proxy")
                if isinstance(candidate, Mapping):
                    kill_budget = candidate
            targets.append(
                TargetSpec(
                    guid=target_guid,
                    target_index=_integer(
                        target.get("target_index"), "target.target_index"
                    ),
                    display_name=_optional_text(target.get("display_name")),
                    activity_first_order=_anchor_order(
                        activity.get("first_anchor"),
                        f"scenario.targets[{target_position}].activity.first_anchor",
                    ),
                    capsule_summary=_mapping(
                        target.get("observed_combat_summaries"),
                        "target.observed_combat_summaries",
                    ),
                    capsule_kill_budget=kill_budget,
                )
            )
        start, end = _wave_absolute_window(scenario)
        declared_scenario_sha = _text(
            scenario.get("capsule_sha256"), "scenario.capsule_sha256"
        )
        scenario_core = {
            key: value for key, value in scenario.items() if key != "capsule_sha256"
        }
        if _sha256_json(scenario_core) != declared_scenario_sha:
            raise ChronicleTeamWaveTimelineError(
                f"scenario capsule hash mismatch for {wave_id}"
            )
        waves.append(
            WaveSpec(
                instance_id=instance_id,
                encounter_id=encounter_id,
                wave_id=wave_id,
                wave_ordinal=_integer(
                    identity.get("wave_ordinal"), "source_identity.wave_ordinal"
                ),
                scenario_id=_text(scenario.get("scenario_id"), "scenario.scenario_id"),
                capsule_sha256=declared_scenario_sha,
                absolute_start_ms=start,
                absolute_end_ms=end,
                targets=tuple(sorted(targets, key=lambda item: item.target_index)),
            )
        )
    if not waves:
        raise ChronicleTeamWaveTimelineError("capsule has no main scenarios")
    return tuple(
        sorted(
            waves,
            key=lambda wave: (
                wave.instance_id,
                wave.encounter_id,
                wave.absolute_start_ms,
                wave.wave_ordinal,
                wave.wave_id,
            ),
        )
    )


def _resolve_normalized(
    entry: Mapping[str, Any], instance_id: str, normalized_directory: Path
) -> Path:
    receipt = entry.get("import_receipt")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    supplied_text = _optional_text(receipt.get("normalized"))
    if supplied_text:
        supplied = Path(supplied_text).expanduser()
        if supplied.is_file():
            return supplied.resolve()
    candidates = sorted(normalized_directory.glob(f"{instance_id}__*.jsonl"))
    if len(candidates) != 1:
        raise ChronicleTeamWaveTimelineError(
            f"expected exactly one normalized JSONL for {instance_id} in "
            f"{normalized_directory}, found {len(candidates)}"
        )
    return candidates[0].resolve()


def _resolve_sidecar(
    sidecar_manifest: Mapping[str, Any] | None,
    sidecar_manifest_path: Path | None,
    instance_id: str,
) -> Path | None:
    if sidecar_manifest is None or sidecar_manifest_path is None:
        return None
    rows = [
        row
        for row in _array(sidecar_manifest.get("instances"), "sidecar.instances")
        if isinstance(row, Mapping) and row.get("instance_ref") == instance_id
    ]
    if not rows:
        return None
    if len(rows) != 1:
        raise ChronicleTeamWaveTimelineError(
            f"sidecar manifest has {len(rows)} rows for instance {instance_id}"
        )
    row = rows[0]
    if row.get("availability") != "available":
        return None
    supplied_text = _optional_text(row.get("sidecar_path"))
    candidates: list[Path] = []
    if supplied_text:
        candidates.append(Path(supplied_text).expanduser())
        candidates.append(sidecar_manifest_path.parent / Path(supplied_text).name)
    candidates.append(
        sidecar_manifest_path.parent
        / f"{instance_id}.combatant_info.jsonl.gz"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ChronicleTeamWaveTimelineError(
        f"available sidecar for {instance_id} cannot be resolved"
    )


def _expected_normalized_hashes(capsule: Mapping[str, Any]) -> dict[str, str]:
    provenance = capsule.get("source_instance_provenance")
    if not isinstance(provenance, Mapping):
        return {}
    hashes: dict[str, str] = {}
    for raw in provenance.get("entries") or []:
        if not isinstance(raw, Mapping):
            continue
        instance_id = _optional_text(raw.get("instance_id"))
        digest = _optional_text(raw.get("normalized_byte_sha256"))
        if instance_id and digest:
            if instance_id in hashes and hashes[instance_id] != digest:
                raise ChronicleTeamWaveTimelineError(
                    f"conflicting capsule normalized hashes for {instance_id}"
                )
            hashes[instance_id] = digest
    return hashes


def _jobs(
    *,
    capsule: Mapping[str, Any],
    capsule_file_sha256: str,
    queue: Mapping[str, Any],
    queue_file_sha256: str,
    sidecar_manifest: Mapping[str, Any] | None,
    sidecar_manifest_path: Path | None,
    sidecar_manifest_file_sha256: str | None,
    normalized_directory: Path,
) -> tuple[InstanceJob, ...]:
    waves = _capsule_waves(capsule)
    queue_rows: dict[str, Mapping[str, Any]] = {}
    for raw in _array(queue.get("entries"), "export_queue.entries"):
        row = _mapping(raw, "export_queue entry")
        instance_id = _text(row.get("instance_id"), "queue instance_id")
        if instance_id in queue_rows:
            raise ChronicleTeamWaveTimelineError(
                f"duplicate export queue instance: {instance_id}"
            )
        queue_rows[instance_id] = row
    expected_hashes = _expected_normalized_hashes(capsule)
    by_instance: dict[str, list[WaveSpec]] = defaultdict(list)
    for wave in waves:
        by_instance[wave.instance_id].append(wave)
    result: list[InstanceJob] = []
    for instance_id in sorted(by_instance):
        entry = queue_rows.get(instance_id)
        if entry is None:
            raise ChronicleTeamWaveTimelineError(
                f"capsule instance {instance_id} is absent from export queue"
            )
        result.append(
            InstanceJob(
                instance_id=instance_id,
                queue_entry=entry,
                normalized_path=_resolve_normalized(
                    entry, instance_id, normalized_directory
                ),
                expected_normalized_sha256=expected_hashes.get(instance_id),
                sidecar_path=_resolve_sidecar(
                    sidecar_manifest, sidecar_manifest_path, instance_id
                ),
                waves=tuple(by_instance[instance_id]),
                capsule_file_sha256=capsule_file_sha256,
                queue_file_sha256=queue_file_sha256,
                sidecar_manifest_file_sha256=sidecar_manifest_file_sha256,
            )
        )
    return tuple(result)


def contamination_classification(entry: Mapping[str, Any]) -> JSONMap:
    """Classify the known South/North pre-fix window using raid start time."""

    guild = entry.get("guild")
    guild_name = (
        _optional_text(guild.get("name")) if isinstance(guild, Mapping) else None
    )
    guild_id = (
        _optional_text(guild.get("id")) if isinstance(guild, Mapping) else None
    )
    if guild_name == LEGACY_GUILD:
        guild_match_evidence = "EXACT_NAME"
    elif guild_id in LEGACY_GUILD_IDS:
        # The exact Chronicle guild ID is an explicit, console/transcoding-safe
        # identity fallback.  No fuzzy or name-based recovery is attempted.
        guild_match_evidence = "KNOWN_GUILD_ID"
    else:
        guild_match_evidence = None
    is_legacy_guild = guild_match_evidence is not None
    started_at = _optional_text(entry.get("started_at"))
    parsed: datetime | None = None
    if started_at:
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = None
            else:
                parsed = parsed.astimezone(SHANGHAI)
        except ValueError:
            parsed = None
    if (guild_name is None and guild_id is None) or parsed is None:
        status = "UNKNOWN_NONVOTING"
        eligible = False
        reason = "guild or parseable started_at is missing"
    elif is_legacy_guild and parsed < LEGACY_SUSPECT_BEFORE_LOCAL:
        status = "SUSPECT_36YD_RANGE_BUG"
        eligible = False
        reason = "known guild raid started before the reported fix date"
    elif is_legacy_guild and parsed < LEGACY_POSTFIX_AT_OR_AFTER_LOCAL:
        status = "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING"
        eligible = False
        reason = "known guild raid falls within the conservative fix-morning boundary"
    elif is_legacy_guild:
        status = "POSTFIX_KNOWN_CLEAN"
        eligible = True
        reason = "known guild raid started at or after the conservative noon safe floor"
    else:
        status = "NO_KNOWN_RULE_MATCH"
        eligible = True
        reason = "known guild/time do not match the scoped legacy rule"
    return {
        "status": status,
        "historical_policy_voting_eligible": eligible,
        "reason": reason,
        "guild_name": LEGACY_GUILD if is_legacy_guild else guild_name,
        "guild_name_observed": guild_name,
        "guild_id": guild_id,
        "guild_match_evidence": guild_match_evidence,
        "fuzzy_name_match_used": False,
        "started_at_source": started_at,
        "started_at_asia_shanghai": parsed.isoformat() if parsed else None,
        "rule": {
            "version": CONTAMINATION_RULE_VERSION,
            "guild": LEGACY_GUILD,
            "known_equivalent_guild_ids": sorted(LEGACY_GUILD_IDS),
            "suspect_before_local": LEGACY_SUSPECT_BEFORE_LOCAL.isoformat(),
            "boundary_uncertain_before_local": (
                LEGACY_POSTFIX_AT_OR_AFTER_LOCAL.isoformat()
            ),
            "postfix_at_or_after_local": (
                LEGACY_POSTFIX_AT_OR_AFTER_LOCAL.isoformat()
            ),
            "safe_floor_is_exact_patch_time": False,
            "timezone": "Asia/Shanghai",
            "time_field": "started_at",
            "uploaded_at_used": False,
        },
    }


def _leaderboard_memberships(entry: Mapping[str, Any]) -> list[JSONMap]:
    memberships: list[JSONMap] = []
    for row_index, raw in enumerate(entry.get("leaderboard_rows") or []):
        if not isinstance(raw, Mapping):
            continue
        memberships.append(
            {
                "row_index": row_index,
                "character": _optional_text(raw.get("character")),
                "board_class": _optional_text(raw.get("board_class")),
                "board_spec": _optional_text(raw.get("board_spec")),
                "observed_spec": _optional_text(raw.get("observed_spec")),
                "rank": raw.get("rank"),
                "dps": raw.get("dps"),
                "raid_date": _optional_text(raw.get("raid_date")),
                "source_file": _optional_text(raw.get("source_file")),
            }
        )
    return memberships


def _read_sidecar(
    path: Path | None,
    instance_id: str,
    encounters: set[str],
) -> tuple[dict[tuple[str, str], JSONMap], str | None, int]:
    if path is None:
        return {}, None, 0
    digest = _sha256_file(path)
    selected: dict[tuple[str, str], tuple[tuple[int, int], JSONMap]] = {}
    count = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ChronicleTeamWaveTimelineError(
                        f"invalid sidecar JSON {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(row, dict) or row.get("schema") != SIDECAR_RECORD_SCHEMA:
                    raise ChronicleTeamWaveTimelineError(
                        f"invalid sidecar record {path}:{line_number}"
                    )
                if row.get("instance_ref") != instance_id:
                    raise ChronicleTeamWaveTimelineError(
                        f"sidecar instance mismatch {path}:{line_number}"
                    )
                encounter = _optional_text(row.get("encounter_id"))
                if encounter not in encounters:
                    continue
                player = row.get("player")
                if not isinstance(player, Mapping):
                    continue
                player_guid = _guid(player.get("guid"))
                if player_guid is None:
                    continue
                anchor = row.get("anchor")
                anchor = anchor if isinstance(anchor, Mapping) else {}
                order = (
                    _integer(anchor.get("offset_ms", 0), "sidecar anchor offset_ms"),
                    _integer(anchor.get("event_index", 0), "sidecar anchor event_index"),
                )
                compact = {
                    "player": {
                        "guid": player_guid,
                        "name": _optional_text(player.get("name")),
                        "guild_name": _optional_text(player.get("guild_name")),
                        "hero_class": _optional_text(player.get("hero_class")),
                        "race": _optional_text(player.get("race")),
                    },
                    "gear": row.get("gear") if isinstance(row.get("gear"), list) else [],
                    "talents": (
                        row.get("talents")
                        if isinstance(row.get("talents"), Mapping)
                        else None
                    ),
                    "anchor": {
                        "offset_ms": order[0],
                        "event_index": order[1],
                    },
                }
                key = (encounter, _guid_key(player_guid))
                previous = selected.get(key)
                if previous is None or order < previous[0]:
                    selected[key] = (order, compact)
                count += 1
    except (OSError, UnicodeError) as error:
        raise ChronicleTeamWaveTimelineError(f"cannot read sidecar {path}: {error}") from error
    return {key: value[1] for key, value in selected.items()}, digest, count


def _classification_row(
    row: Mapping[str, Any],
) -> tuple[str, str, str | None, str | None] | None:
    guid = _guid(row.get("target_guid"))
    outcome = _optional_text(row.get("outcome"))
    if guid is None or outcome is None:
        return None
    owner = OWNER_RE.search(outcome)
    return (
        guid,
        outcome,
        owner.group(1).upper() if owner else None,
        _optional_text(row.get("target")),
    )


def _matching_waves(
    waves: Iterable[WaveSpec], offset_ms: int
) -> Iterator[WaveSpec]:
    for wave in waves:
        if wave.absolute_start_ms <= offset_ms <= wave.absolute_end_ms:
            yield wave


def _create_event_database(output_directory: Path, instance_id: str) -> tuple[Path, sqlite3.Connection]:
    with tempfile.NamedTemporaryFile(
        prefix=f".{_safe_component(instance_id)}.events.",
        suffix=".sqlite.tmp",
        dir=output_directory,
        delete=False,
    ) as handle:
        database_path = Path(handle.name)
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE events (
            wave_key TEXT NOT NULL,
            offset_ms INTEGER NOT NULL,
            event_index INTEGER NOT NULL,
            csv_line INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(wave_key, offset_ms, event_index, csv_line)
        )
        """
    )
    connection.execute(
        "CREATE INDEX events_order ON events(wave_key, offset_ms, event_index, csv_line)"
    )
    return database_path, connection


def _scan_normalized(
    job: InstanceJob,
    connection: sqlite3.Connection,
) -> tuple[
    str,
    int,
    dict[tuple[str, str], tuple[JSONMap, ...]],
    dict[tuple[str, str], str],
    Counter[str],
]:
    by_encounter: dict[str, list[WaveSpec]] = defaultdict(list)
    target_sets: dict[str, set[str]] = {}
    for wave in job.waves:
        by_encounter[wave.encounter_id].append(wave)
        target_sets[wave.key] = {_guid_key(target.guid) for target in wave.targets}
    classification_by_order: dict[
        tuple[str, str], dict[tuple[int, int, int], JSONMap]
    ] = defaultdict(dict)
    info_outcomes: dict[tuple[str, str], str] = {}
    event_types: Counter[str] = Counter()
    digest = hashlib.sha256()
    lines_scanned = 0
    inserts: list[tuple[str, int, int, int, str]] = []
    try:
        with job.normalized_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                digest.update(raw_line)
                lines_scanned += 1
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ChronicleTeamWaveTimelineError(
                        f"invalid normalized JSON {job.normalized_path}:{line_number}: {error}"
                    ) from error
                if not isinstance(row, dict):
                    raise ChronicleTeamWaveTimelineError(
                        f"normalized row is not an object {job.normalized_path}:{line_number}"
                    )
                if row.get("instance") != job.instance_id:
                    raise ChronicleTeamWaveTimelineError(
                        f"normalized instance mismatch {job.normalized_path}:{line_number}"
                    )
                encounter = _optional_text(row.get("encounter"))
                waves = by_encounter.get(encounter or "")
                if not waves:
                    continue
                source_type = str(row.get("type") or "").strip().upper()
                if source_type == "CLASS":
                    classified = _classification_row(row)
                    if classified is not None:
                        guid, outcome, owner_suffix, target_name = classified
                        key = (encounter or "", _guid_key(guid))
                        offset_ms = _integer(row.get("offset_ms"), "CLASS.offset_ms")
                        event_index = _integer(row.get("event_index"), "CLASS.event_index")
                        provenance = _mapping(row.get("provenance"), "CLASS.provenance")
                        csv_line = _integer(provenance.get("csv_line"), "CLASS.csv_line")
                        order = (offset_ms, event_index, csv_line)
                        value = {
                            "order_key": list(order),
                            "outcome": outcome,
                            "owner_suffix": owner_suffix,
                            "target_name": target_name,
                        }
                        previous = classification_by_order[key].get(order)
                        if previous is not None and previous != value:
                            raise ChronicleTeamWaveTimelineError(
                                f"conflicting CLASS evidence at identical order key "
                                f"{order} for {guid}@{encounter}"
                            )
                        classification_by_order[key][order] = value
                    continue
                if source_type == "INFO":
                    player_guid = _guid(row.get("target_guid"))
                    outcome = _optional_text(row.get("outcome"))
                    if player_guid and outcome:
                        info_outcomes.setdefault(
                            (encounter or "", _guid_key(player_guid)), outcome
                        )
                    continue
                if source_type not in TIMELINE_SOURCE_TYPES:
                    continue
                offset_ms = _integer(row.get("offset_ms"), "event.offset_ms")
                event_index = _integer(row.get("event_index"), "event.event_index")
                provenance = _mapping(row.get("provenance"), "event.provenance")
                csv_line = _integer(provenance.get("csv_line"), "event.csv_line")
                matches = list(_matching_waves(waves, offset_ms))
                if source_type not in ACTION_SOURCE_TYPES:
                    target_guid = _guid_key(row.get("target_guid"))
                    matches = [
                        wave
                        for wave in matches
                        if target_guid and target_guid in target_sets[wave.key]
                    ]
                if not matches:
                    continue
                projected = {
                    "source_type": source_type,
                    "source": _optional_text(row.get("source")),
                    "source_guid": _guid(row.get("source_guid")),
                    "target": _optional_text(row.get("target")),
                    "target_guid": _guid(row.get("target_guid")),
                    "spell": _optional_text(row.get("spell")),
                    "spell_id": row.get("spell_id"),
                    "value": row.get("value"),
                    "outcome": _optional_text(row.get("outcome")),
                    "flags": row.get("flags") if isinstance(row.get("flags"), list) else [],
                    "synthetic": row.get("synthetic") is True,
                }
                payload = _canonical_bytes(projected).decode("utf-8")
                for wave in matches:
                    inserts.append((wave.key, offset_ms, event_index, csv_line, payload))
                event_types[source_type] += len(matches)
                if len(inserts) >= 10_000:
                    try:
                        connection.executemany(
                            "INSERT INTO events VALUES (?, ?, ?, ?, ?)", inserts
                        )
                    except sqlite3.IntegrityError as error:
                        raise ChronicleTeamWaveTimelineError(
                            f"duplicate event order key in {job.normalized_path}: {error}"
                        ) from error
                    inserts.clear()
        if inserts:
            try:
                connection.executemany(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?)", inserts
                )
            except sqlite3.IntegrityError as error:
                raise ChronicleTeamWaveTimelineError(
                    f"duplicate event order key in {job.normalized_path}: {error}"
                ) from error
        connection.commit()
    except OSError as error:
        raise ChronicleTeamWaveTimelineError(
            f"cannot scan normalized input {job.normalized_path}: {error}"
        ) from error
    actual = digest.hexdigest()
    if job.expected_normalized_sha256 and actual != job.expected_normalized_sha256:
        raise ChronicleTeamWaveTimelineError(
            f"normalized SHA-256 mismatch for {job.instance_id}: expected "
            f"{job.expected_normalized_sha256}, got {actual}"
        )
    classifications = {
        key: tuple(rows[order] for order in sorted(rows))
        for key, rows in classification_by_order.items()
    }
    return actual, lines_scanned, classifications, info_outcomes, event_types


class _TemporalClassificationState:
    """Encounter-local CLASS prefix state with static sidecar player identity."""

    def __init__(
        self,
        encounter_id: str,
        classifications: Mapping[tuple[str, str], tuple[JSONMap, ...]],
        sidecars: Mapping[tuple[str, str], JSONMap],
    ) -> None:
        self.encounter_id = encounter_id
        self.latest: dict[str, JSONMap] = {}
        self.static_player_keys: set[str] = set()
        self.players: dict[str, JSONMap] = {}
        observations: list[tuple[tuple[int, int, int], str, JSONMap]] = []
        for (encounter, guid_key), rows in classifications.items():
            if encounter != encounter_id:
                continue
            for row in rows:
                order = tuple(_integer(value, "CLASS order key") for value in row["order_key"])
                observations.append((order, guid_key, row))
        self.observations = sorted(observations, key=lambda value: (value[0], value[1]))
        self.cursor = 0
        for (encounter, guid_key), record in sidecars.items():
            if encounter != encounter_id:
                continue
            player = _mapping(record.get("player"), "sidecar player")
            self.static_player_keys.add(guid_key)
            self.players[guid_key] = {
                "guid": _guid(player.get("guid")),
                "name": _optional_text(player.get("name")),
                "evidence": ["OFFICIAL_COMBATANT_INFO_PRE_ENCOUNTER"],
            }

    def advance(self, order: tuple[int, int, int]) -> None:
        while self.cursor < len(self.observations):
            observation_order, guid_key, observation = self.observations[self.cursor]
            if observation_order > order:
                break
            self.latest[guid_key] = observation
            if str(observation.get("outcome") or "").casefold() == "friendly player":
                current = self.players.setdefault(
                    guid_key,
                    {
                        "guid": _guid(guid_key),
                        "name": observation.get("target_name"),
                        "evidence": [],
                    },
                )
                if "NORMALIZED_CLASS_FRIENDLY_PLAYER_AS_OF_EVENT" not in current["evidence"]:
                    current["evidence"].append(
                        "NORMALIZED_CLASS_FRIENDLY_PLAYER_AS_OF_EVENT"
                    )
                if current.get("name") is None:
                    current["name"] = observation.get("target_name")
            elif guid_key not in self.static_player_keys:
                self.players.pop(guid_key, None)
            self.cursor += 1


def _final_friendly_players(
    encounter_id: str,
    classifications: Mapping[tuple[str, str], tuple[JSONMap, ...]],
    sidecars: Mapping[tuple[str, str], JSONMap],
) -> dict[str, JSONMap]:
    state = _TemporalClassificationState(encounter_id, classifications, sidecars)
    state.advance((sys.maxsize, sys.maxsize, sys.maxsize))
    return state.players


def _resolve_owner(
    suffix: str,
    players: Mapping[str, JSONMap],
) -> tuple[str | None, str]:
    normalized_suffix = suffix.upper().lstrip("0") or "0"
    matches = []
    for guid_key, player in players.items():
        compact = guid_key.removeprefix("0x").upper().lstrip("0") or "0"
        if compact.endswith(normalized_suffix):
            matches.append((_guid(player.get("guid")), guid_key))
    if len(matches) == 1:
        return matches[0][0], "EXPLICIT_CLASS_OWNER_SUFFIX_UNIQUE_PLAYER_MATCH"
    if not matches:
        return None, "EXPLICIT_CLASS_OWNER_SUFFIX_NO_PLAYER_MATCH"
    return None, "EXPLICIT_CLASS_OWNER_SUFFIX_AMBIGUOUS_PLAYER_MATCH"


def _attribution(
    source_guid: str | None,
    state: _TemporalClassificationState,
) -> JSONMap:
    source_key = _guid_key(source_guid)
    classified = state.latest.get(source_key)
    classification_order = classified.get("order_key") if classified else None
    if source_key and source_key in state.players:
        return {
            "kind": "DIRECT_PLAYER",
            "player_guid": _guid(state.players[source_key].get("guid")),
            "evidence": (
                "NORMALIZED_CLASS_FRIENDLY_PLAYER_AS_OF_EVENT"
                if classified
                and str(classified.get("outcome") or "").casefold() == "friendly player"
                else "OFFICIAL_COMBATANT_INFO_PRE_ENCOUNTER"
            ),
            "owner_suffix": None,
            "name_inference_used": False,
            "classification_as_of_order_key": classification_order,
            "temporal_cutoff_enforced": True,
            "future_classification_backfill_used": False,
        }
    owner_suffix = classified.get("owner_suffix") if classified else None
    if owner_suffix:
        owner_guid, evidence = _resolve_owner(str(owner_suffix), state.players)
        if owner_guid:
            return {
                "kind": "OWNED_ENTITY",
                "player_guid": owner_guid,
                "evidence": evidence,
                "owner_suffix": owner_suffix,
                "name_inference_used": False,
                "classification_as_of_order_key": classification_order,
                "temporal_cutoff_enforced": True,
                "future_classification_backfill_used": False,
            }
        unresolved_evidence = evidence
    elif classified:
        unresolved_evidence = "CLASS_HAS_NO_EXPLICIT_OWNER_SUFFIX"
    else:
        unresolved_evidence = "NO_SOURCE_CLASS_OR_EXPLICIT_OWNER_SUFFIX"
    return {
        "kind": "UNATTRIBUTED",
        "player_guid": None,
        "evidence": unresolved_evidence,
        "owner_suffix": owner_suffix,
        "name_inference_used": False,
        "classification_as_of_order_key": classification_order,
        "temporal_cutoff_enforced": True,
        "future_classification_backfill_used": False,
    }


def _classification_counts(
    classifications: Mapping[tuple[str, str], tuple[JSONMap, ...]],
    encounter_ids: set[str],
    *,
    window: tuple[int, int] | None = None,
) -> tuple[int, int]:
    observations = 0
    transitions = 0
    for (encounter_id, _guid_key_value), rows in classifications.items():
        if encounter_id not in encounter_ids:
            continue
        previous: tuple[str, str | None] | None = None
        for row in rows:
            semantic = (
                str(row.get("outcome") or ""),
                _optional_text(row.get("owner_suffix")),
            )
            offset_ms = _integer(row["order_key"][0], "CLASS order offset_ms")
            in_window = window is None or window[0] <= offset_ms <= window[1]
            if in_window:
                observations += 1
                if previous is not None and semantic != previous:
                    transitions += 1
            previous = semantic
    return observations, transitions


def _roster(
    encounter_id: str,
    players: Mapping[str, JSONMap],
    sidecars: Mapping[tuple[str, str], JSONMap],
    info_outcomes: Mapping[tuple[str, str], str],
    memberships: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    membership_by_name: dict[str, list[JSONMap]] = defaultdict(list)
    for membership in memberships:
        name = _optional_text(membership.get("character"))
        if name:
            membership_by_name[name.casefold()].append(dict(membership))
    result: list[JSONMap] = []
    for guid_key in sorted(players):
        player = players[guid_key]
        sidecar = sidecars.get((encounter_id, guid_key))
        sidecar_player = (
            sidecar.get("player")
            if isinstance(sidecar, Mapping) and isinstance(sidecar.get("player"), Mapping)
            else {}
        )
        name = _optional_text(sidecar_player.get("name")) or _optional_text(
            player.get("name")
        )
        rows = membership_by_name.get(name.casefold(), []) if name else []
        result.append(
            {
                "player_guid": _guid(player.get("guid")),
                "player_name": name,
                "hero_class": _optional_text(sidecar_player.get("hero_class")),
                "race": _optional_text(sidecar_player.get("race")),
                "guild_name": _optional_text(sidecar_player.get("guild_name")),
                "talents": sidecar.get("talents") if sidecar else None,
                "gear": sidecar.get("gear") if sidecar else None,
                "normalized_info_outcome": info_outcomes.get(
                    (encounter_id, guid_key)
                ),
                "leaderboard_memberships": rows,
                "leaderboard_specs_recorded_separately": sorted(
                    {
                        str(row.get("board_spec"))
                        for row in rows
                        if row.get("board_spec") is not None
                    }
                ),
                "fury_arms_rows_merged": False,
                "player_evidence": sorted(player.get("evidence") or []),
            }
        )
    return result


class _CanonicalGzipWriter:
    def __init__(self, path: Path):
        self.path = path
        self.digest = hashlib.sha256()
        self.record_count = 0
        self._raw = path.open("wb")
        self._gzip = gzip.GzipFile(
            filename="", fileobj=self._raw, mode="wb", compresslevel=9, mtime=0
        )
        self._text = io.TextIOWrapper(self._gzip, encoding="utf-8", newline="\n")
        self._closed = False

    def write(self, record: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(record) + b"\n"
        self.digest.update(payload)
        self._text.write(payload.decode("utf-8"))
        self.record_count += 1

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._text.flush()
            self._text.close()
        finally:
            self._raw.close()
            self._closed = True

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()


def _capsule_observed_value(target: TargetSpec, kind: str, field: str) -> Any:
    raw = target.capsule_summary.get(kind)
    raw = raw if isinstance(raw, Mapping) else {}
    return raw.get(field)


def _match_numeric_lane(
    expected: Any,
    candidates: Sequence[tuple[str, float]],
    *,
    label: str,
) -> tuple[float | None, str]:
    if expected is None:
        return None, "NOT_DECLARED"
    declared = _nonnegative_number(expected)
    if declared is None:
        raise ChronicleTeamWaveTimelineError(f"{label} must be nonnegative numeric")
    matches = [name for name, value in candidates if float(value) == declared]
    if not matches:
        rendered = ", ".join(
            f"{name}={_render_number(float(value))}" for name, value in candidates
        )
        raise ChronicleTeamWaveTimelineError(
            f"{label} mismatch: expected {_render_number(declared)}; {rendered}"
        )
    if len(matches) == 1:
        return declared, matches[0]
    return declared, "EQUIVALENT_ZERO_DIFFERENCE[" + "|".join(matches) + "]"


def _match_integer_lane(
    expected: Any,
    candidates: Sequence[tuple[str, int]],
    *,
    label: str,
) -> tuple[int | None, str]:
    if expected is None:
        return None, "NOT_DECLARED"
    declared = _integer(expected, label)
    matches = [name for name, value in candidates if value == declared]
    if not matches:
        rendered = ", ".join(f"{name}={value}" for name, value in candidates)
        raise ChronicleTeamWaveTimelineError(
            f"{label} mismatch: declared {declared}; {rendered}"
        )
    if len(matches) == 1:
        return declared, matches[0]
    return declared, "EQUIVALENT_ZERO_DIFFERENCE[" + "|".join(matches) + "]"


def _write_partition(job: InstanceJob, output_directory: Path) -> PartitionBuild:
    database_path, connection = _create_event_database(
        output_directory, job.instance_id
    )
    temporary_path: Path | None = None
    writer: _CanonicalGzipWriter | None = None
    succeeded = False
    try:
        sidecars, sidecar_sha256, sidecar_rows = _read_sidecar(
            job.sidecar_path,
            job.instance_id,
            {wave.encounter_id for wave in job.waves},
        )
        (
            normalized_sha256,
            lines_scanned,
            classifications,
            info_outcomes,
            input_event_types,
        ) = _scan_normalized(job, connection)
        memberships = _leaderboard_memberships(job.queue_entry)
        contamination = contamination_classification(job.queue_entry)
        classification_observation_count, classification_transition_count = (
            _classification_counts(
                classifications, {wave.encounter_id for wave in job.waves}
            )
        )
        with tempfile.NamedTemporaryFile(
            prefix=f".{_safe_component(job.instance_id)}.timeline.",
            suffix=".jsonl.gz.tmp",
            dir=output_directory,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        writer = _CanonicalGzipWriter(temporary_path)
        total_events = 0
        total_damage = 0.0
        total_canonical_dmg_value = 0.0
        total_capsule_compatible_damage_events = 0
        total_dmg_rows = 0
        total_numeric_damage_bearing_rows = 0
        total_lethal_dead_damage_events = 0
        total_lethal_dead_damage_value = 0.0
        total_unparsed_damage = 0
        total_event_types: Counter[str] = Counter()
        total_attribution_values: Counter[str] = Counter()
        total_attribution_numeric_damage_rows: Counter[str] = Counter()
        total_attribution_dmg_rows: Counter[str] = Counter()
        total_attribution_unparsed_dmg_rows: Counter[str] = Counter()
        total_attribution_lethal_dead_events: Counter[str] = Counter()
        total_attribution_lethal_dead_values: Counter[str] = Counter()
        total_temporally_attributed_events = 0
        per_wave_summaries: list[JSONMap] = []

        for wave in job.waves:
            players = _final_friendly_players(
                wave.encounter_id, classifications, sidecars
            )
            temporal_state = _TemporalClassificationState(
                wave.encounter_id, classifications, sidecars
            )
            wave_class_observations, wave_class_transitions = _classification_counts(
                classifications,
                {wave.encounter_id},
                window=(wave.absolute_start_ms, wave.absolute_end_ms),
            )
            roster = _roster(
                wave.encounter_id,
                players,
                sidecars,
                info_outcomes,
                memberships,
            )
            header = {
                "schema": SCHEMA,
                "record_type": "wave_header",
                "wave": wave.identity,
                "scenario": {
                    "scenario_id": wave.scenario_id,
                    "capsule_sha256": wave.capsule_sha256,
                    "main_comparison_only": True,
                },
                "window": {
                    "absolute_start_ms": wave.absolute_start_ms,
                    "absolute_end_ms": wave.absolute_end_ms,
                    "duration_ms": wave.absolute_end_ms - wave.absolute_start_ms,
                    "boundary_policy": "inclusive capsule wave endpoints",
                    "derivation": "first_anchor.offset_ms - observed activity start_ms; target consensus",
                },
                "targets": [
                    {
                        "target_guid": target.guid,
                        "target_index": target.target_index,
                        "display_name": target.display_name,
                    }
                    for target in wave.targets
                ],
                "roster": roster,
                "instance_leaderboard_memberships": memberships,
                "specialization_contract": {
                    "fury_and_arms_are_recorded_as_distinct_membership_rows": True,
                    "fury_arms_rows_merged": False,
                    "talent_summary_used_to_infer_board_spec": False,
                },
                "classification_contract": {
                    "mode": "EVENT_PREFIX_LATEST_CLASS",
                    "sidecar_player_identity_scope": "PRE_ENCOUNTER_STATIC",
                    "owner_link_requires_prior_or_same_order_class": True,
                    "future_classification_backfill_allowed": False,
                    "classification_observation_count": wave_class_observations,
                    "classification_transition_count": wave_class_transitions,
                },
                "contamination": contamination,
                "source_hashes": {
                    "capsule_file_sha256": job.capsule_file_sha256,
                    "export_queue_file_sha256": job.queue_file_sha256,
                    "normalized_file_sha256": normalized_sha256,
                    "combatant_sidecar_manifest_file_sha256": job.sidecar_manifest_file_sha256,
                    "combatant_sidecar_file_sha256": sidecar_sha256,
                },
            }
            writer.write(header)

            target_by_key = {_guid_key(target.guid): target for target in wave.targets}
            deaths: dict[str, JSONMap] = {}
            target_damage: Counter[str] = Counter()
            target_dmg_values: Counter[str] = Counter()
            target_dmg_rows: Counter[str] = Counter()
            target_numeric_dmg_events: Counter[str] = Counter()
            target_numeric_damage_bearing_rows: Counter[str] = Counter()
            target_unparsed_damage: Counter[str] = Counter()
            target_lethal_dead_damage_events: Counter[str] = Counter()
            target_lethal_dead_damage_values: Counter[str] = Counter()
            target_activity_dmg_values: Counter[str] = Counter()
            target_activity_dmg_rows: Counter[str] = Counter()
            target_activity_numeric_dmg_events: Counter[str] = Counter()
            target_activity_unparsed_damage: Counter[str] = Counter()
            target_activity_dead_events: Counter[str] = Counter()
            target_activity_dead_values: Counter[str] = Counter()
            target_kill_dmg_values: Counter[str] = Counter()
            target_kill_dmg_rows: Counter[str] = Counter()
            target_kill_numeric_dmg_events: Counter[str] = Counter()
            target_kill_unparsed_damage: Counter[str] = Counter()
            target_kill_dead_events: Counter[str] = Counter()
            target_kill_dead_values: Counter[str] = Counter()
            target_pre_activity_dmg_values: Counter[str] = Counter()
            target_pre_activity_dmg_rows: Counter[str] = Counter()
            target_pre_activity_dead_values: Counter[str] = Counter()
            target_pre_activity_dead_rows: Counter[str] = Counter()
            target_pre_activity_first_order: dict[str, tuple[int, int, int]] = {}
            target_pre_activity_last_order: dict[str, tuple[int, int, int]] = {}
            target_post_death_dmg_values: Counter[str] = Counter()
            target_post_death_dmg_rows: Counter[str] = Counter()
            target_post_death_dead_values: Counter[str] = Counter()
            target_post_death_dead_rows: Counter[str] = Counter()
            target_post_death_first_order: dict[str, tuple[int, int, int]] = {}
            target_post_death_last_order: dict[str, tuple[int, int, int]] = {}
            target_healing: Counter[str] = Counter()
            target_healing_events: Counter[str] = Counter()
            target_numeric_healing_events: Counter[str] = Counter()
            target_activity_healing: Counter[str] = Counter()
            target_activity_healing_rows: Counter[str] = Counter()
            target_activity_numeric_healing_events: Counter[str] = Counter()
            attribution_values: Counter[str] = Counter()
            attribution_numeric_damage_rows: Counter[str] = Counter()
            attribution_dmg_rows: Counter[str] = Counter()
            attribution_unparsed_dmg_rows: Counter[str] = Counter()
            attribution_lethal_dead_events: Counter[str] = Counter()
            attribution_lethal_dead_values: Counter[str] = Counter()
            player_damage: Counter[str] = Counter()
            player_damage_events: Counter[str] = Counter()
            player_lethal_dead_damage_events: Counter[str] = Counter()
            player_lethal_dead_damage_values: Counter[str] = Counter()
            wave_event_types: Counter[str] = Counter()
            wave_event_count = 0
            last_order: tuple[int, int, int] | None = None

            cursor = connection.execute(
                """
                SELECT offset_ms, event_index, csv_line, payload
                FROM events
                WHERE wave_key = ?
                ORDER BY offset_ms, event_index, csv_line
                """,
                (wave.key,),
            )
            for absolute_offset, event_index, csv_line, payload_text in cursor:
                row = json.loads(payload_text)
                source_type = row["source_type"]
                order = (int(absolute_offset), int(event_index), int(csv_line))
                temporal_state.advance(order)
                attribution = _attribution(row.get("source_guid"), temporal_state)
                if source_type in ACTION_SOURCE_TYPES and attribution["kind"] == "UNATTRIBUTED":
                    continue
                if last_order is not None and order <= last_order:
                    raise ChronicleTeamWaveTimelineError(
                        f"non-strict event ordering for {wave.wave_id}: {last_order} then {order}"
                    )
                last_order = order
                numeric = (
                    _nonnegative_number(row.get("value"))
                    if source_type in ("DMG", "DEAD", "HEAL")
                    else None
                )
                target_key = _guid_key(row.get("target_guid"))
                target_spec = target_by_key.get(target_key)
                activity_eligible = (
                    target_spec is not None
                    and order >= target_spec.activity_first_order
                )
                after_first_dead = target_key in deaths
                event_type = "CAST" if source_type == "GO" else source_type
                event = {
                    "schema": SCHEMA,
                    "record_type": "event",
                    "wave": wave.identity,
                    "order_key": [order[0], order[1], order[2]],
                    "offset_ms": order[0],
                    "wave_offset_ms": order[0] - wave.absolute_start_ms,
                    "event_index": order[1],
                    "csv_line": order[2],
                    "event_type": event_type,
                    "source_event_type": source_type,
                    "source": {
                        "guid": row.get("source_guid"),
                        "name": row.get("source"),
                    },
                    "target": {
                        "guid": row.get("target_guid"),
                        "name": row.get("target"),
                    },
                    "spell": {
                        "id": row.get("spell_id"),
                        "name": row.get("spell"),
                    },
                    "amount": _render_number(numeric) if numeric is not None else None,
                    "amount_status": (
                        "PARSED_NONNEGATIVE_NUMERIC"
                        if numeric is not None
                        else (
                            "NOT_NUMERIC_OR_ABSENT"
                            if source_type in ("DMG", "DEAD", "HEAL")
                            else "NOT_APPLICABLE"
                        )
                    ),
                    "amount_semantics": "OBSERVED_NORMALIZED_VALUE_NOT_CALIBRATED_EFFECTIVE_DAMAGE",
                    "outcome": row.get("outcome"),
                    "flags": row.get("flags"),
                    "synthetic": row.get("synthetic"),
                    "attribution": attribution,
                }
                if source_type in DAMAGE_SOURCE_TYPES:
                    event["damage_accounting"] = {
                        "canonical_prefix_lane": (
                            "NUMERIC_DMG_ONLY"
                            if source_type == "DMG"
                            else "DEAD_MARKER_EXCLUDED_FROM_CANONICAL_DAMAGE"
                        ),
                        "capsule_target_activity_eligible": activity_eligible,
                        "capsule_reconstruction_included": activity_eligible,
                        "capsule_reconstruction_exclusion_reason": (
                            None
                            if activity_eligible
                            else (
                                "BEFORE_TARGET_HOSTILITY_ADMISSION"
                                if target_spec is not None
                                else "TARGET_NOT_IN_CAPSULE_WAVE"
                            )
                        ),
                        "relative_to_first_dead": (
                            "AFTER_FIRST_DEAD"
                            if after_first_dead
                            else (
                                "AT_FIRST_DEAD"
                                if source_type == "DEAD" and target_spec is not None
                                else "BEFORE_FIRST_DEAD_OR_RIGHT_CENSORED"
                            )
                        ),
                        "derived_from_future_totals": False,
                    }
                writer.write(event)
                wave_event_count += 1
                wave_event_types[event_type] += 1
                # Exact rows remain in the trace.  Capsule summaries begin at
                # each target's first observed-hostile-activity anchor, so
                # pre-anchor AoE rows are diagnostic rather than silently
                # folded into the frozen target total.  Numeric DEAD values
                # remain a separate lane because old capsules used more than
                # one count convention even when their value included DEAD.
                if source_type == "DMG":
                    kind = attribution["kind"]
                    attribution_dmg_rows[kind] += 1
                    target_dmg_rows[target_key] += 1
                    if numeric is None:
                        attribution_unparsed_dmg_rows[kind] += 1
                        target_unparsed_damage[target_key] += 1
                    else:
                        attribution_numeric_damage_rows[kind] += 1
                        target_numeric_dmg_events[target_key] += 1
                        target_numeric_damage_bearing_rows[target_key] += 1
                        attribution_values[kind] += numeric
                        target_damage[target_key] += numeric
                        target_dmg_values[target_key] += numeric
                        player_guid = _guid_key(attribution.get("player_guid"))
                        if player_guid:
                            player_damage[player_guid] += numeric
                            player_damage_events[player_guid] += 1
                    if target_spec is not None:
                        if activity_eligible:
                            target_activity_dmg_rows[target_key] += 1
                            if numeric is None:
                                target_activity_unparsed_damage[target_key] += 1
                            else:
                                target_activity_numeric_dmg_events[target_key] += 1
                                target_activity_dmg_values[target_key] += numeric
                            if not after_first_dead:
                                target_kill_dmg_rows[target_key] += 1
                                if numeric is None:
                                    target_kill_unparsed_damage[target_key] += 1
                                else:
                                    target_kill_numeric_dmg_events[target_key] += 1
                                    target_kill_dmg_values[target_key] += numeric
                            else:
                                target_post_death_dmg_rows[target_key] += 1
                                if numeric is not None:
                                    target_post_death_dmg_values[target_key] += numeric
                                target_post_death_first_order.setdefault(
                                    target_key, order
                                )
                                target_post_death_last_order[target_key] = order
                        else:
                            target_pre_activity_dmg_rows[target_key] += 1
                            if numeric is not None:
                                target_pre_activity_dmg_values[target_key] += numeric
                            target_pre_activity_first_order.setdefault(
                                target_key, order
                            )
                            target_pre_activity_last_order[target_key] = order
                elif source_type == "DEAD" and numeric is not None:
                    kind = attribution["kind"]
                    attribution_numeric_damage_rows[kind] += 1
                    attribution_lethal_dead_events[kind] += 1
                    attribution_lethal_dead_values[kind] += numeric
                    target_numeric_damage_bearing_rows[target_key] += 1
                    target_lethal_dead_damage_events[target_key] += 1
                    target_lethal_dead_damage_values[target_key] += numeric
                    attribution_values[kind] += numeric
                    target_damage[target_key] += numeric
                    player_guid = _guid_key(attribution.get("player_guid"))
                    if player_guid:
                        player_damage[player_guid] += numeric
                        player_damage_events[player_guid] += 1
                        player_lethal_dead_damage_events[player_guid] += 1
                        player_lethal_dead_damage_values[player_guid] += numeric
                    if target_spec is not None:
                        if activity_eligible:
                            target_activity_dead_events[target_key] += 1
                            target_activity_dead_values[target_key] += numeric
                            if not after_first_dead:
                                target_kill_dead_events[target_key] += 1
                                target_kill_dead_values[target_key] += numeric
                            else:
                                target_post_death_dead_rows[target_key] += 1
                                target_post_death_dead_values[target_key] += numeric
                                target_post_death_first_order.setdefault(
                                    target_key, order
                                )
                                target_post_death_last_order[target_key] = order
                        else:
                            target_pre_activity_dead_rows[target_key] += 1
                            target_pre_activity_dead_values[target_key] += numeric
                            target_pre_activity_first_order.setdefault(
                                target_key, order
                            )
                            target_pre_activity_last_order[target_key] = order
                elif source_type == "HEAL":
                    target_healing_events[target_key] += 1
                    if numeric is not None:
                        target_numeric_healing_events[target_key] += 1
                        target_healing[target_key] += numeric
                    if target_spec is not None and activity_eligible:
                        target_activity_healing_rows[target_key] += 1
                        if numeric is not None:
                            target_activity_numeric_healing_events[target_key] += 1
                            target_activity_healing[target_key] += numeric
                if source_type == "DEAD" and target_key in target_by_key:
                    deaths.setdefault(
                        target_key,
                        {
                            "offset_ms": order[0],
                            "wave_offset_ms": order[0] - wave.absolute_start_ms,
                            "event_index": order[1],
                            "csv_line": order[2],
                        },
                    )

            target_records: list[JSONMap] = []
            for target in wave.targets:
                key = _guid_key(target.guid)
                raw_combined_damage = target_damage[key]
                canonical_dmg_value = target_dmg_values[key]
                observed_dmg_rows = target_dmg_rows[key]
                observed_damage_events = target_numeric_dmg_events[key]
                observed_numeric_damage_rows = target_numeric_damage_bearing_rows[key]
                observed_unparsed = target_unparsed_damage[key]
                lethal_dead_events = target_lethal_dead_damage_events[key]
                lethal_dead_value = target_lethal_dead_damage_values[key]
                activity_dmg_value = target_activity_dmg_values[key]
                activity_dead_value = target_activity_dead_values[key]
                activity_combined_value = activity_dmg_value + activity_dead_value
                activity_dmg_events = target_activity_numeric_dmg_events[key]
                activity_dead_events = target_activity_dead_events[key]
                activity_combined_events = activity_dmg_events + activity_dead_events
                expected_damage = _capsule_observed_value(
                    target, "observed_incoming_damage_sum", "value"
                )
                expected_events = _capsule_observed_value(
                    target, "observed_incoming_damage_sum", "event_count"
                )
                expected_unparsed = _capsule_observed_value(
                    target, "observed_incoming_damage_sum", "unparsed_event_count"
                )
                declared_damage, capsule_value_match = _match_numeric_lane(
                    expected_damage,
                    (
                        ("TARGET_ACTIVITY_NUMERIC_DMG_ONLY", activity_dmg_value),
                        (
                            "TARGET_ACTIVITY_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                            activity_combined_value,
                        ),
                    ),
                    label=(
                        f"capsule damage total for {wave.wave_id}/{target.guid}"
                    ),
                )
                declared_events, capsule_event_count_match = _match_integer_lane(
                    expected_events,
                    (
                        ("TARGET_ACTIVITY_NUMERIC_DMG_ONLY", activity_dmg_events),
                        (
                            "TARGET_ACTIVITY_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                            activity_combined_events,
                        ),
                    ),
                    label=(
                        f"capsule damage event_count for {wave.wave_id}/{target.guid}"
                    ),
                )
                if expected_unparsed is not None and _integer(
                    expected_unparsed, "capsule unparsed_event_count"
                ) != target_activity_unparsed_damage[key]:
                    raise ChronicleTeamWaveTimelineError(
                        f"capsule unparsed damage mismatch for {wave.wave_id}/{target.guid}"
                    )
                expected_healing = _capsule_observed_value(
                    target, "observed_healing_received_sum", "value"
                )
                expected_healing_events = _capsule_observed_value(
                    target, "observed_healing_received_sum", "event_count"
                )
                if expected_healing is not None and _nonnegative_number(
                    expected_healing
                ) != float(target_activity_healing[key]):
                    raise ChronicleTeamWaveTimelineError(
                        f"capsule healing total mismatch for {wave.wave_id}/{target.guid}"
                    )
                if expected_healing_events is not None and _integer(
                    expected_healing_events, "capsule healing event_count"
                ) != target_activity_numeric_healing_events[key]:
                    raise ChronicleTeamWaveTimelineError(
                        f"capsule healing event-count mismatch for {wave.wave_id}/{target.guid}"
                    )
                death = deaths.get(key)
                kill_dmg_value = target_kill_dmg_values[key]
                kill_dead_value = target_kill_dead_values[key]
                kill_combined_value = kill_dmg_value + kill_dead_value
                kill_expected = (
                    target.capsule_kill_budget.get("value")
                    if target.capsule_kill_budget is not None
                    else None
                )
                declared_kill_value, kill_value_match = _match_numeric_lane(
                    kill_expected,
                    (
                        (
                            "TARGET_ACTIVITY_THROUGH_FIRST_DEAD_NUMERIC_DMG_ONLY",
                            kill_dmg_value,
                        ),
                        (
                            "TARGET_ACTIVITY_THROUGH_FIRST_DEAD_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                            kill_combined_value,
                        ),
                    ),
                    label=f"capsule kill budget for {wave.wave_id}/{target.guid}",
                )
                kill_expected_unparsed = (
                    target.capsule_kill_budget.get("unparsed_damage_event_count")
                    if target.capsule_kill_budget is not None
                    else None
                )
                if kill_expected_unparsed is not None and _integer(
                    kill_expected_unparsed, "capsule kill unparsed_damage_event_count"
                ) != target_kill_unparsed_damage[key]:
                    raise ChronicleTeamWaveTimelineError(
                        f"capsule kill unparsed damage mismatch for {wave.wave_id}/{target.guid}"
                    )
                target_record = {
                    "schema": SCHEMA,
                    "record_type": "target_summary",
                    "wave": wave.identity,
                    "target_guid": target.guid,
                    "target_index": target.target_index,
                    "observed_damage": {
                        "value": _render_number(
                            float(
                                declared_damage
                                if declared_damage is not None
                                else activity_dmg_value
                            )
                        ),
                        "value_semantics": "frozen capsule-compatible target-activity lane",
                        "canonical_dmg_value": _render_number(
                            float(canonical_dmg_value)
                        ),
                        "numeric_dead_value": _render_number(float(lethal_dead_value)),
                        "combined_value": _render_number(float(raw_combined_damage)),
                        "capsule_activity_dmg_value": _render_number(
                            float(activity_dmg_value)
                        ),
                        "capsule_activity_combined_value": _render_number(
                            float(activity_combined_value)
                        ),
                        "capsule_declared_value": (
                            _render_number(float(declared_damage))
                            if declared_damage is not None
                            else None
                        ),
                        "capsule_value_match_semantics": capsule_value_match,
                        "event_count": observed_damage_events,
                        "event_count_semantics": (
                            "full normalized wave parsed numeric DMG rows only; excludes DEAD rows"
                        ),
                        "canonical_dmg_event_count": observed_damage_events,
                        "dmg_row_count": observed_dmg_rows,
                        "numeric_damage_bearing_row_count": observed_numeric_damage_rows,
                        "unparsed_event_count": observed_unparsed,
                        "capsule_activity_unparsed_dmg_row_count": (
                            target_activity_unparsed_damage[key]
                        ),
                        "lethal_dead_damage_event_count": lethal_dead_events,
                        "lethal_dead_damage_value": _render_number(
                            float(lethal_dead_value)
                        ),
                        "capsule_activity_dmg_event_count": activity_dmg_events,
                        "capsule_activity_numeric_damage_bearing_row_count": (
                            activity_combined_events
                        ),
                        "capsule_declared_event_count": declared_events,
                        "capsule_event_count_match_semantics": capsule_event_count_match,
                        "capsule_conservation_checked": True,
                    },
                    "observed_healing": {
                        "value": _render_number(float(target_activity_healing[key])),
                        "event_count": target_activity_numeric_healing_events[key],
                        "row_count": target_activity_healing_rows[key],
                        "raw_wave_value": _render_number(float(target_healing[key])),
                        "raw_wave_event_count": target_numeric_healing_events[key],
                        "raw_wave_row_count": target_healing_events[key],
                    },
                    "kill_budget_damage": {
                        "status": (
                            "OBSERVED_THROUGH_FIRST_DEAD"
                            if death is not None
                            else "RIGHT_CENSORED_AT_WAVE_END"
                        ),
                        "canonical_dmg_value": _render_number(float(kill_dmg_value)),
                        "numeric_dead_value": _render_number(float(kill_dead_value)),
                        "combined_value": _render_number(float(kill_combined_value)),
                        "canonical_dmg_event_count": target_kill_numeric_dmg_events[key],
                        "dmg_row_count": target_kill_dmg_rows[key],
                        "unparsed_dmg_row_count": target_kill_unparsed_damage[key],
                        "numeric_dead_event_count": target_kill_dead_events[key],
                        "capsule_declared_value": (
                            _render_number(float(declared_kill_value))
                            if declared_kill_value is not None
                            else None
                        ),
                        "capsule_value_match_semantics": kill_value_match,
                        "decision_feature_eligible": False,
                    },
                    "pre_activity_damage_diagnostic": {
                        "status": (
                            "PRESENT"
                            if target_pre_activity_dmg_rows[key]
                            or target_pre_activity_dead_rows[key]
                            else "EMPTY"
                        ),
                        "activity_first_order_key": list(target.activity_first_order),
                        "canonical_dmg_value": _render_number(
                            float(target_pre_activity_dmg_values[key])
                        ),
                        "dmg_row_count": target_pre_activity_dmg_rows[key],
                        "numeric_dead_value": _render_number(
                            float(target_pre_activity_dead_values[key])
                        ),
                        "numeric_dead_row_count": target_pre_activity_dead_rows[key],
                        "first_order_key": (
                            list(target_pre_activity_first_order[key])
                            if key in target_pre_activity_first_order
                            else None
                        ),
                        "last_order_key": (
                            list(target_pre_activity_last_order[key])
                            if key in target_pre_activity_last_order
                            else None
                        ),
                        "exact_events_retained": True,
                    },
                    "post_first_dead_damage_diagnostic": {
                        "status": (
                            "PRESENT"
                            if target_post_death_dmg_rows[key]
                            or target_post_death_dead_rows[key]
                            else "EMPTY"
                        ),
                        "first_dead_order_key": (
                            [
                                death["offset_ms"],
                                death["event_index"],
                                death["csv_line"],
                            ]
                            if death is not None
                            else None
                        ),
                        "canonical_dmg_value": _render_number(
                            float(target_post_death_dmg_values[key])
                        ),
                        "dmg_row_count": target_post_death_dmg_rows[key],
                        "numeric_dead_value": _render_number(
                            float(target_post_death_dead_values[key])
                        ),
                        "numeric_dead_row_count": target_post_death_dead_rows[key],
                        "first_order_key": (
                            list(target_post_death_first_order[key])
                            if key in target_post_death_first_order
                            else None
                        ),
                        "last_order_key": (
                            list(target_post_death_last_order[key])
                            if key in target_post_death_last_order
                            else None
                        ),
                        "excluded_from_kill_budget": True,
                        "exact_events_retained": True,
                    },
                    "death_clock": {
                        "status": "OBSERVED_DEAD" if death else "RIGHT_CENSORED_AT_WAVE_END",
                        "censored": death is None,
                        "observed": death,
                        "censor_offset_ms": wave.absolute_end_ms if death is None else None,
                        "censor_wave_offset_ms": (
                            wave.absolute_end_ms - wave.absolute_start_ms
                            if death is None
                            else None
                        ),
                    },
                }
                writer.write(target_record)
                target_records.append(target_record)

            damage_value = sum(attribution_values.values())
            canonical_damage_value = sum(target_dmg_values.values())
            capsule_damage_events = sum(target_numeric_dmg_events.values())
            dmg_rows = sum(attribution_dmg_rows.values())
            numeric_damage_rows = sum(attribution_numeric_damage_rows.values())
            lethal_dead_events = sum(attribution_lethal_dead_events.values())
            lethal_dead_value = sum(attribution_lethal_dead_values.values())
            target_value = sum(target_damage.values())
            target_dmg_row_count = sum(target_dmg_rows.values())
            target_numeric_damage_row_count = sum(
                target_numeric_damage_bearing_rows.values()
            )
            target_lethal_dead_event_count = sum(
                target_lethal_dead_damage_events.values()
            )
            target_lethal_dead_value = sum(target_lethal_dead_damage_values.values())
            if (
                damage_value != target_value
                or dmg_rows != target_dmg_row_count
                or numeric_damage_rows != target_numeric_damage_row_count
                or lethal_dead_events != target_lethal_dead_event_count
                or lethal_dead_value != target_lethal_dead_value
            ):
                raise ChronicleTeamWaveTimelineError(
                    f"attribution conservation failed for {wave.wave_id}"
                )
            duration_seconds = max(
                (wave.absolute_end_ms - wave.absolute_start_ms) / 1000.0, 0.0
            )
            wave_summary = {
                "schema": SCHEMA,
                "record_type": "wave_summary",
                "wave": wave.identity,
                "event_count": wave_event_count,
                "event_type_counts": dict(sorted(wave_event_types.items())),
                "classification_provenance": {
                    "observation_count": wave_class_observations,
                    "transition_count": wave_class_transitions,
                    "temporally_attributed_event_count": wave_event_count,
                    "future_classification_backfill_count": 0,
                    "mode": "LATEST_CLASS_AT_OR_BEFORE_EVENT_ORDER_KEY",
                },
                "team_damage": {
                    "value": _render_number(float(damage_value)),
                    "value_semantics": (
                        "full normalized wave numeric DMG plus numeric DEAD; descriptive exact-trace conservation lane"
                    ),
                    "canonical_dmg_value": _render_number(
                        float(canonical_damage_value)
                    ),
                    "canonical_dmg_value_semantics": (
                        "full normalized wave numeric DMG only; causal non-double-count lane"
                    ),
                    "event_count": capsule_damage_events,
                    "event_count_semantics": (
                        "parsed numeric DMG rows only; excludes DEAD rows"
                    ),
                    "dmg_row_count": dmg_rows,
                    "numeric_damage_bearing_row_count": numeric_damage_rows,
                    "unparsed_event_count": sum(target_unparsed_damage.values()),
                    "lethal_dead_damage_event_count": lethal_dead_events,
                    "lethal_dead_damage_value": _render_number(
                        float(lethal_dead_value)
                    ),
                    "duration_seconds": duration_seconds,
                    "dps": damage_value / duration_seconds if duration_seconds > 0 else None,
                },
                "damage_attribution_conservation": {
                    "direct_player": {
                        "value": _render_number(
                            float(attribution_values["DIRECT_PLAYER"])
                        ),
                        "event_count": attribution_numeric_damage_rows[
                            "DIRECT_PLAYER"
                        ],
                        "event_count_semantics": (
                            "numeric DMG plus numeric DEAD rows"
                        ),
                        "dmg_row_count": attribution_dmg_rows["DIRECT_PLAYER"],
                        "unparsed_dmg_row_count": attribution_unparsed_dmg_rows[
                            "DIRECT_PLAYER"
                        ],
                        "numeric_damage_bearing_row_count": (
                            attribution_numeric_damage_rows["DIRECT_PLAYER"]
                        ),
                        "lethal_dead_damage_event_count": (
                            attribution_lethal_dead_events["DIRECT_PLAYER"]
                        ),
                        "lethal_dead_damage_value": _render_number(
                            float(attribution_lethal_dead_values["DIRECT_PLAYER"])
                        ),
                    },
                    "owned_entity": {
                        "value": _render_number(
                            float(attribution_values["OWNED_ENTITY"])
                        ),
                        "event_count": attribution_numeric_damage_rows[
                            "OWNED_ENTITY"
                        ],
                        "event_count_semantics": (
                            "numeric DMG plus numeric DEAD rows"
                        ),
                        "dmg_row_count": attribution_dmg_rows["OWNED_ENTITY"],
                        "unparsed_dmg_row_count": attribution_unparsed_dmg_rows[
                            "OWNED_ENTITY"
                        ],
                        "numeric_damage_bearing_row_count": (
                            attribution_numeric_damage_rows["OWNED_ENTITY"]
                        ),
                        "lethal_dead_damage_event_count": (
                            attribution_lethal_dead_events["OWNED_ENTITY"]
                        ),
                        "lethal_dead_damage_value": _render_number(
                            float(attribution_lethal_dead_values["OWNED_ENTITY"])
                        ),
                    },
                    "unattributed": {
                        "value": _render_number(
                            float(attribution_values["UNATTRIBUTED"])
                        ),
                        "event_count": attribution_numeric_damage_rows[
                            "UNATTRIBUTED"
                        ],
                        "event_count_semantics": (
                            "numeric DMG plus numeric DEAD rows"
                        ),
                        "dmg_row_count": attribution_dmg_rows["UNATTRIBUTED"],
                        "unparsed_dmg_row_count": attribution_unparsed_dmg_rows[
                            "UNATTRIBUTED"
                        ],
                        "numeric_damage_bearing_row_count": (
                            attribution_numeric_damage_rows["UNATTRIBUTED"]
                        ),
                        "lethal_dead_damage_event_count": (
                            attribution_lethal_dead_events["UNATTRIBUTED"]
                        ),
                        "lethal_dead_damage_value": _render_number(
                            float(attribution_lethal_dead_values["UNATTRIBUTED"])
                        ),
                    },
                    "sum_equals_team_damage": True,
                    "owner_policy": "only an explicit CLASS owner suffix at or before the event, uniquely resolving to a player known by that event",
                    "name_inference_used": False,
                },
                "per_player_observed_damage": [
                    {
                        "player_guid": _guid(guid_key),
                        "value": _render_number(float(player_damage[guid_key])),
                        "event_count": player_damage_events[guid_key],
                        "event_count_semantics": (
                            "numeric DMG plus numeric DEAD rows"
                        ),
                        "numeric_damage_bearing_row_count": (
                            player_damage_events[guid_key]
                        ),
                        "lethal_dead_damage_event_count": (
                            player_lethal_dead_damage_events[guid_key]
                        ),
                        "lethal_dead_damage_value": _render_number(
                            float(player_lethal_dead_damage_values[guid_key])
                        ),
                    }
                    for guid_key in sorted(player_damage)
                ],
                "target_count": len(target_records),
                "death_observed_target_count": len(deaths),
                "death_censored_target_count": len(wave.targets) - len(deaths),
            }
            writer.write(wave_summary)
            per_wave_summaries.append(wave_summary)
            total_events += wave_event_count
            total_damage += damage_value
            total_canonical_dmg_value += canonical_damage_value
            total_capsule_compatible_damage_events += capsule_damage_events
            total_dmg_rows += dmg_rows
            total_numeric_damage_bearing_rows += numeric_damage_rows
            total_lethal_dead_damage_events += lethal_dead_events
            total_lethal_dead_damage_value += lethal_dead_value
            total_unparsed_damage += sum(target_unparsed_damage.values())
            total_event_types.update(wave_event_types)
            total_attribution_values.update(attribution_values)
            total_attribution_numeric_damage_rows.update(
                attribution_numeric_damage_rows
            )
            total_attribution_dmg_rows.update(attribution_dmg_rows)
            total_attribution_unparsed_dmg_rows.update(
                attribution_unparsed_dmg_rows
            )
            total_attribution_lethal_dead_events.update(
                attribution_lethal_dead_events
            )
            total_attribution_lethal_dead_values.update(
                attribution_lethal_dead_values
            )
            total_temporally_attributed_events += wave_event_count

        writer.close()
        logical_sha256 = writer.sha256
        final_path = output_directory / (
            f"{_safe_component(job.instance_id)}.{logical_sha256}.jsonl.gz"
        )
        compressed_sha256 = _sha256_file(temporary_path)
        entry = {
            "instance_id": job.instance_id,
            "partition": final_path.name,
            "logical_content_sha256": logical_sha256,
            "compressed_file_sha256": compressed_sha256,
            "compressed_size_bytes": temporary_path.stat().st_size,
            "record_count": writer.record_count,
            "wave_count": len(job.waves),
            "event_count": total_events,
            "event_type_counts": dict(sorted(total_event_types.items())),
            "team_damage_value": _render_number(total_damage),
            "team_canonical_dmg_value": _render_number(
                total_canonical_dmg_value
            ),
            "team_damage_event_count": total_capsule_compatible_damage_events,
            "team_damage_event_count_semantics": (
                "parsed numeric DMG rows only; excludes DEAD rows"
            ),
            "team_dmg_row_count": total_dmg_rows,
            "team_numeric_damage_bearing_row_count": (
                total_numeric_damage_bearing_rows
            ),
            "team_lethal_dead_damage_event_count": (
                total_lethal_dead_damage_events
            ),
            "team_lethal_dead_damage_value": _render_number(
                total_lethal_dead_damage_value
            ),
            "unparsed_damage_event_count": total_unparsed_damage,
            "damage_attribution_values": {
                kind: _render_number(float(total_attribution_values[kind]))
                for kind in ATTRIBUTION_KINDS
            },
            "damage_attribution_event_counts": {
                kind: total_attribution_numeric_damage_rows[kind]
                for kind in ATTRIBUTION_KINDS
            },
            "damage_attribution_event_count_semantics": (
                "numeric DMG plus numeric DEAD rows"
            ),
            "damage_attribution_dmg_row_counts": {
                kind: total_attribution_dmg_rows[kind]
                for kind in ATTRIBUTION_KINDS
            },
            "damage_attribution_unparsed_dmg_row_counts": {
                kind: total_attribution_unparsed_dmg_rows[kind]
                for kind in ATTRIBUTION_KINDS
            },
            "damage_attribution_lethal_dead_event_counts": {
                kind: total_attribution_lethal_dead_events[kind]
                for kind in ATTRIBUTION_KINDS
            },
            "damage_attribution_lethal_dead_values": {
                kind: _render_number(
                    float(total_attribution_lethal_dead_values[kind])
                )
                for kind in ATTRIBUTION_KINDS
            },
            "classification_provenance": {
                "observation_count": classification_observation_count,
                "transition_count": classification_transition_count,
                "temporally_attributed_event_count": total_temporally_attributed_events,
                "future_classification_backfill_count": 0,
                "same_order_conflicts_fail_closed": True,
            },
            "normalized_input": {
                "path": _portable_path(job.normalized_path),
                "size_bytes": job.normalized_path.stat().st_size,
                "sha256": normalized_sha256,
                "lines_scanned": lines_scanned,
                "scan_count": 1,
            },
            "combatant_sidecar_input": {
                "path": _portable_path(job.sidecar_path) if job.sidecar_path else None,
                "sha256": sidecar_sha256,
                "selected_encounter_message_count": sidecar_rows,
                "availability": "AVAILABLE" if job.sidecar_path else "UNAVAILABLE",
            },
            "input_candidate_event_type_counts": dict(sorted(input_event_types.items())),
            "contamination_status": contamination["status"],
            "raw_file_open_count": 0,
            "raw_or_normalized_file_copy_count": 0,
        }
        succeeded = True
        return PartitionBuild(temporary_path, final_path, entry)
    finally:
        connection.close()
        database_path.unlink(missing_ok=True)
        if writer is not None:
            try:
                writer.close()
            except (OSError, ValueError):
                pass
        if temporary_path is not None and not succeeded:
            temporary_path.unlink(missing_ok=True)


def _atomic_json_temporary(path: Path, document: Mapping[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_canonical_bytes(document) + b"\n")
    return temporary


def build_chronicle_team_wave_timeline(
    *,
    capsule_path: str | Path | None = None,
    export_queue_path: str | Path = DEFAULT_EXPORT_QUEUE,
    sidecar_manifest_path: str | Path | None = DEFAULT_SIDECAR_MANIFEST,
    normalized_directory: str | Path = DEFAULT_NORMALIZED_DIRECTORY,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> ChronicleTeamWaveTimelineResult:
    """Compile content-addressed per-instance team timelines locally."""

    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ChronicleTeamWaveTimelineError("workers must be a positive integer")
    capsule_file = (
        Path(capsule_path).expanduser().resolve()
        if capsule_path is not None
        else _resolve_default_capsule()
    )
    queue_file = Path(export_queue_path).expanduser().resolve()
    normalized_dir = Path(normalized_directory).expanduser().resolve()
    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    capsule = _load_json(capsule_file, "scenario capsule")
    queue = _load_json(queue_file, "export queue")
    capsule_file_sha256 = _sha256_file(capsule_file)
    queue_file_sha256 = _sha256_file(queue_file)

    sidecar_file: Path | None = None
    sidecar_manifest: JSONMap | None = None
    sidecar_manifest_sha256: str | None = None
    if sidecar_manifest_path is not None:
        candidate = Path(sidecar_manifest_path).expanduser().resolve()
        if candidate.is_file():
            sidecar_file = candidate
            sidecar_manifest = _load_json(candidate, "combatant sidecar manifest")
            if sidecar_manifest.get("schema") != SIDECAR_SCHEMA:
                raise ChronicleTeamWaveTimelineError(
                    f"sidecar manifest schema must be {SIDECAR_SCHEMA}"
                )
            sidecar_manifest_sha256 = _sha256_file(candidate)

    jobs = _jobs(
        capsule=capsule,
        capsule_file_sha256=capsule_file_sha256,
        queue=queue,
        queue_file_sha256=queue_file_sha256,
        sidecar_manifest=sidecar_manifest,
        sidecar_manifest_path=sidecar_file,
        sidecar_manifest_file_sha256=sidecar_manifest_sha256,
        normalized_directory=normalized_dir,
    )
    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        if workers == 1 or len(jobs) <= 1:
            for job in jobs:
                builds.append(_write_partition(job, output_dir))
        else:
            indexed: dict[int, PartitionBuild] = {}
            failures: dict[int, BaseException] = {}
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
                futures = {
                    executor.submit(_write_partition, job, output_dir): index
                    for index, job in enumerate(jobs)
                }
                for future in as_completed(futures):
                    try:
                        indexed[futures[future]] = future.result()
                    except BaseException as error:
                        # Continue draining the executor: successful futures that
                        # finish after a failure also own temporary files. Retain
                        # every failure by stable job index so a six-way pass does
                        # not reveal input incompatibilities one rerun at a time.
                        failures[futures[future]] = error
            if failures:
                for completed in indexed.values():
                    completed.temporary_path.unlink(missing_ok=True)
                details: list[str] = []
                for index in sorted(failures):
                    job = jobs[index]
                    instance_id = _optional_text(getattr(job, "instance_id", None))
                    error = failures[index]
                    message = " ".join(str(error).split())
                    details.append(
                        f"index={index} instance_id={instance_id or '<unknown>'} "
                        f"{type(error).__name__}: {message}"
                    )
                raise ChronicleTeamWaveTimelineError(
                    f"parallel partition failures ({len(failures)}): "
                    + " | ".join(details)
                )
            builds = [indexed[index] for index in range(len(jobs))]

        manifest_core: JSONMap = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "chronicle_team_wave_timeline_manifest",
            "implementation_revision": IMPLEMENTATION_REVISION,
            "inputs": {
                "capsule": {
                    "path": _portable_path(capsule_file),
                    "file_sha256": capsule_file_sha256,
                    "declared_content_sha256": _mapping(
                        capsule.get("content_address"), "capsule.content_address"
                    ).get("sha256"),
                    "bucket": "main_comparison",
                },
                "export_queue": {
                    "path": _portable_path(queue_file),
                    "file_sha256": queue_file_sha256,
                    "time_field_used": "started_at",
                },
                "combatant_sidecar_manifest": {
                    "path": _portable_path(sidecar_file) if sidecar_file else None,
                    "file_sha256": sidecar_manifest_sha256,
                    "availability": "AVAILABLE" if sidecar_file else "NOT_PROVIDED",
                },
                "normalized_directory": _portable_path(normalized_dir),
                "workers": workers,
            },
            "output_contract": {
                "format": "deterministic gzip-compressed canonical JSON Lines",
                "partition_grain": "instance",
                "record_sequence": "wave_header, strictly ordered events, target_summary, wave_summary",
                "event_order_key": ["offset_ms", "event_index", "csv_line"],
                "event_types": ["START", "CAST(GO)", "FAIL", "DMG", "DEAD", "HEAL"],
                "raw_csv_opened": False,
                "raw_files_copied": 0,
                "normalized_files_copied": 0,
                "raw_30gb_upload_required": False,
                "manifest_committed_last": True,
                "damage_count_semantics": {
                    "event_count": "full normalized wave parsed numeric DMG rows only; excludes DEAD",
                    "dmg_row_count": "all DMG rows including unparsed values",
                    "numeric_damage_bearing_row_count": "numeric DMG plus numeric DEAD rows",
                    "lethal_dead_damage": "numeric DEAD rows and values reported separately",
                    "capsule_compatible": "target rows at or after the frozen first hostile-activity anchor; value/count DEAD inclusion is matched and labeled independently",
                    "kill_budget": "target-activity rows through the strict first DEAD order; later rows are retained only as diagnostics",
                },
            },
            "evidence_boundaries": {
                "owner_attribution": "latest explicit CLASS owner suffix at or before event only; unique player GUID suffix match among players known by that event",
                "classification_attribution_mode": "event-prefix temporal; no future CLASS backfill",
                "combatant_sidecar_player_identity": "pre-encounter static",
                "name_based_owner_inference": False,
                "damage_categories": list(ATTRIBUTION_KINDS),
                "damage_conservation_checked_per_wave": True,
                "full_trace_numeric_dead_value_included_in_damage_and_attribution": True,
                "canonical_prefix_damage_lane": "full normalized wave numeric DMG only; numeric DEAD is a terminal marker and cannot double-count damage",
                "capsule_reconstruction_inclusion_marked_per_damage_event": True,
                "pre_hostility_admission_rows_retained_as_diagnostic": True,
                "post_first_dead_rows_retained_as_diagnostic": True,
                "marker_only_dead_counted_as_damage": False,
                "numeric_amount_parser_accepts_grouping_commas": True,
                "missing_death_is_right_censored": True,
                "fury_arms_memberships_merged": False,
                "historical_policy_or_addon_authorship_inferred": False,
                "causal_action_value_claimed": False,
            },
            "contamination_rule": {
                "version": CONTAMINATION_RULE_VERSION,
                "guild": LEGACY_GUILD,
                "known_equivalent_guild_ids": sorted(LEGACY_GUILD_IDS),
                "guild_identity_match_evidence": ["EXACT_NAME", "KNOWN_GUILD_ID"],
                "fuzzy_name_matching": False,
                "suspect_before_local": LEGACY_SUSPECT_BEFORE_LOCAL.isoformat(),
                "boundary_uncertain_before_local": (
                    LEGACY_POSTFIX_AT_OR_AFTER_LOCAL.isoformat()
                ),
                "postfix_at_or_after_local": (
                    LEGACY_POSTFIX_AT_OR_AFTER_LOCAL.isoformat()
                ),
                "safe_floor_is_exact_patch_time": False,
                "timezone": "Asia/Shanghai",
                "time_field": "started_at",
                "statuses": [
                    "SUSPECT_36YD_RANGE_BUG",
                    "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
                    "POSTFIX_KNOWN_CLEAN",
                    "NO_KNOWN_RULE_MATCH",
                    "UNKNOWN_NONVOTING",
                ],
            },
            "summary": {
                "instance_count": len(builds),
                "wave_count": sum(row.manifest_entry["wave_count"] for row in builds),
                "event_count": sum(row.manifest_entry["event_count"] for row in builds),
                "team_damage_value": _render_number(
                    sum(float(row.manifest_entry["team_damage_value"]) for row in builds)
                ),
                "raw_file_open_count": 0,
                "raw_or_normalized_file_copy_count": 0,
            },
            "partitions": [row.manifest_entry for row in builds],
        }
        manifest = {
            **manifest_core,
            "content_address": {
                "algorithm": "sha256",
                "scope": "canonical JSON document excluding content_address",
                "sha256": _sha256_json(manifest_core),
            },
        }
        manifest_name = (
            f"chronicle_team_wave_timeline_v1."
            f"{manifest['content_address']['sha256']}.manifest.json"
        )
        addressed_path = output_dir / manifest_name
        stable_path = output_dir / "manifest.json"
        addressed_temporary = _atomic_json_temporary(addressed_path, manifest)
        stable_temporary = _atomic_json_temporary(stable_path, manifest)

        for build in builds:
            build.temporary_path.replace(build.final_path)
        addressed_temporary.replace(addressed_path)
        addressed_temporary = None
        # This stable pointer is the sole commit marker and is deliberately last.
        stable_temporary.replace(stable_path)
        stable_temporary = None
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)

    return ChronicleTeamWaveTimelineResult(
        manifest=stable_path,
        content_addressed_manifest=addressed_path,
        partitions=tuple(build.final_path for build in builds),
        instance_count=len(builds),
        wave_count=sum(build.manifest_entry["wave_count"] for build in builds),
        event_count=sum(build.manifest_entry["event_count"] for build in builds),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build local, content-addressed per-wave Chronicle team timelines "
            "without opening or copying raw CSV files."
        )
    )
    parser.add_argument("--capsule", type=Path, default=None)
    parser.add_argument("--export-queue", type=Path, default=DEFAULT_EXPORT_QUEUE)
    parser.add_argument(
        "--sidecar-manifest", type=Path, default=DEFAULT_SIDECAR_MANIFEST
    )
    parser.add_argument(
        "--normalized-dir", type=Path, default=DEFAULT_NORMALIZED_DIRECTORY
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_chronicle_team_wave_timeline(
            capsule_path=args.capsule,
            export_queue_path=args.export_queue,
            sidecar_manifest_path=args.sidecar_manifest,
            normalized_directory=args.normalized_dir,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleTeamWaveTimelineError as error:
        print(f"Chronicle team-wave timeline build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "ChronicleTeamWaveTimelineError",
    "ChronicleTeamWaveTimelineResult",
    "SCHEMA",
    "build_chronicle_team_wave_timeline",
    "contamination_classification",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
