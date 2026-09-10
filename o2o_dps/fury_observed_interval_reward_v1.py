"""Audit a bounded observed-interval reward contract for compact Fury rows.

The reward is the raw outgoing damage observed between two consecutive START
candidates for the same player and encounter.  It is a transition outcome,
not a direct or counterfactual attribution to the preceding action.  This
module streams the compact partitions once, keeps only the preceding row for
each trajectory key, and writes one aggregate audit rather than another row
dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_fury_decision_dataset"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_observed_interval_reward_v1.json"
)
SCHEMA = "fury_observed_interval_reward_audit/v1"
DATASET_SCHEMA = "chronicle_fury_decision_dataset/v1"
RECORD_SCHEMA = "chronicle_fury_decision/v1"
ELIGIBLE_LANES = frozenset(("gcd", "off_gcd"))
EXCLUSION_REASONS = (
    "no_next_start",
    "invalid_boundary",
    "observation_missing",
    "damage_stream_unverified",
    "unmapped_action",
    "on_swing_unknown_intent",
    "unsupported_lane",
    "result_failed_unique",
    "result_ambiguous",
    "result_missing",
    "result_other",
)


class FuryObservedIntervalRewardError(RuntimeError):
    """The compact manifest or one of its partitions violates the contract."""


@dataclass
class _PendingDecision:
    record: dict[str, Any]
    start_anchor: dict[str, int]
    damage_stream_verified: bool


@dataclass
class _DurationStats:
    count: int = 0
    total_ms: int = 0
    minimum_ms: int | None = None
    maximum_ms: int | None = None

    def add(self, duration_ms: int) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.minimum_ms = (
            duration_ms
            if self.minimum_ms is None
            else min(self.minimum_ms, duration_ms)
        )
        self.maximum_ms = (
            duration_ms
            if self.maximum_ms is None
            else max(self.maximum_ms, duration_ms)
        )

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "count": self.count,
            "sum_ms": self.total_ms,
            "min_ms": self.minimum_ms,
            "max_ms": self.maximum_ms,
            "mean_ms": self.total_ms / self.count if self.count else None,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryObservedIntervalRewardError(
            f"cannot read compact dataset manifest {path}: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schema") != DATASET_SCHEMA:
        raise FuryObservedIntervalRewardError(
            f"compact manifest must use schema {DATASET_SCHEMA}"
        )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise FuryObservedIntervalRewardError("compact manifest has no partitions")
    return manifest


def _partition_path(entry: Mapping[str, Any], manifest_path: Path) -> Path:
    supplied = str(entry.get("partition") or "").strip()
    if not supplied:
        raise FuryObservedIntervalRewardError("partition manifest entry has no path")
    path = Path(supplied)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryObservedIntervalRewardError(f"compact row lacks {label} object")
    return value


def _start_anchor(record: Mapping[str, Any]) -> dict[str, int]:
    source = _required_mapping(record.get("source"), "source")
    anchor = _required_mapping(source.get("start_anchor"), "source.start_anchor")
    output: dict[str, int] = {}
    for name in ("event_index", "csv_line", "offset_ms"):
        value = anchor.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise FuryObservedIntervalRewardError(
                f"source.start_anchor.{name} must be an integer"
            )
        output[name] = value
    return output


def _trajectory_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = _required_mapping(record.get("identity"), "identity")
    values = (
        str(identity.get("source_instance_ref") or "").strip(),
        str(identity.get("encounter_id") or "").strip(),
        str(identity.get("player_guid") or "").strip().casefold(),
    )
    if not all(values):
        raise FuryObservedIntervalRewardError(
            "compact row lacks source instance, encounter, or player GUID"
        )
    return values


def _window(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _required_mapping(
        record.get("window_until_next_start_candidate"),
        "window_until_next_start_candidate",
    )


def _reward_value(record: Mapping[str, Any]) -> float:
    value = _window(record).get("outgoing_damage_observed")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise FuryObservedIntervalRewardError(
            "outgoing_damage_observed must be one finite non-negative number"
        )
    return float(value)


def _observation_exists(record: Mapping[str, Any]) -> bool:
    return all(
        isinstance(record.get(name), Mapping)
        for name in ("state_before", "state_mask", "state_provenance")
    )


def _declared_next_anchor(record: Mapping[str, Any]) -> dict[str, int] | None:
    value = _window(record).get("next_start_anchor")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    output: dict[str, int] = {}
    for name in ("event_index", "csv_line", "offset_ms"):
        item = value.get(name)
        if not isinstance(item, int) or isinstance(item, bool):
            return None
        output[name] = item
    return output


def _boundary_valid(
    previous: _PendingDecision, actual_next: Mapping[str, Any]
) -> bool:
    declared = _declared_next_anchor(previous.record)
    actual = _start_anchor(actual_next)
    if declared != actual:
        return False
    if actual["event_index"] <= previous.start_anchor["event_index"]:
        return False
    if actual["offset_ms"] < previous.start_anchor["offset_ms"]:
        return False
    return True


def _delta_t_ms(previous: _PendingDecision, actual_next: Mapping[str, Any]) -> int:
    actual = _start_anchor(actual_next)
    return actual["offset_ms"] - previous.start_anchor["offset_ms"]


def _causal_fields_missing(record: Mapping[str, Any]) -> bool:
    window = _window(record)
    return (
        window.get("causal_reward") is None
        and window.get("causal_attribution") == "MISSING"
    )


def _classify(
    previous: _PendingDecision,
    actual_next: Mapping[str, Any],
) -> tuple[str, bool, float]:
    reward = _reward_value(previous.record)
    if not _boundary_valid(previous, actual_next):
        return "invalid_boundary", False, reward
    interval_observed = previous.damage_stream_verified
    if not _observation_exists(previous.record) or not _observation_exists(actual_next):
        return "observation_missing", interval_observed, reward
    if not previous.damage_stream_verified:
        return "damage_stream_unverified", False, reward

    action = _required_mapping(previous.record.get("action"), "action")
    if action.get("catalog_status") != "MAPPED_ACTIVE":
        return "unmapped_action", True, reward
    lane = str(action.get("lane") or "")
    if lane == "on_swing_unknown_intent":
        return "on_swing_unknown_intent", True, reward
    if lane not in ELIGIBLE_LANES:
        return "unsupported_lane", True, reward

    result = _required_mapping(previous.record.get("result"), "result")
    association = str(result.get("association") or "").casefold()
    status = str(result.get("status") or "").casefold()
    if association == "ambiguous":
        return "result_ambiguous", True, reward
    if association == "unique" and status == "failed":
        return "result_failed_unique", True, reward
    if association in ("", "missing", "unlinked") or status in ("", "missing"):
        return "result_missing", True, reward
    if association == "unique" and status == "succeeded":
        return "reward_eligible_success", True, reward
    return "result_other", True, reward


def _damage_stream_verified(entry: Mapping[str, Any]) -> bool:
    counts = entry.get("selected_event_type_counts")
    if not isinstance(counts, Mapping):
        return False
    damage = counts.get("DMG")
    return isinstance(damage, int) and not isinstance(damage, bool) and damage > 0


def build_fury_observed_interval_reward_audit(
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Stream each compact partition once and return an aggregate V1 audit."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(path)
    pending: dict[tuple[str, str, str], _PendingDecision] = {}
    excluded: Counter[str] = Counter({name: 0 for name in EXCLUSION_REASONS})
    eligible_success = 0
    decision_rows = 0
    observed_interval_rows = 0
    observed_interval_reward_sum = 0.0
    observed_interval_zero_rows = 0
    eligible_reward_sum = 0.0
    eligible_zero_reward_rows = 0
    observed_interval_durations = _DurationStats()
    eligible_interval_durations = _DurationStats()
    direct_causal_nonmissing_rows = 0
    compressed_bytes = 0
    partition_reports: list[dict[str, Any]] = []

    for partition_index, entry_value in enumerate(manifest["partitions"]):
        if not isinstance(entry_value, Mapping):
            raise FuryObservedIntervalRewardError(
                f"partition manifest entry {partition_index} is not an object"
            )
        partition = _partition_path(entry_value, path)
        stream_verified = _damage_stream_verified(entry_value)
        try:
            partition_bytes = partition.stat().st_size
        except OSError as error:
            raise FuryObservedIntervalRewardError(
                f"cannot stat compact partition {partition}: {error}"
            ) from error
        compressed_bytes += partition_bytes
        partition_rows = 0
        try:
            with gzip.open(partition, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise FuryObservedIntervalRewardError(
                            f"invalid JSON at {partition}:{line_number}: {error}"
                        ) from error
                    if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA:
                        raise FuryObservedIntervalRewardError(
                            f"unsupported compact row at {partition}:{line_number}"
                        )
                    key = _trajectory_key(record)
                    anchor = _start_anchor(record)
                    _reward_value(record)
                    if not _causal_fields_missing(record):
                        direct_causal_nonmissing_rows += 1

                    previous = pending.get(key)
                    if previous is not None:
                        outcome, interval_observed, reward = _classify(previous, record)
                        if interval_observed:
                            duration_ms = _delta_t_ms(previous, record)
                            observed_interval_durations.add(duration_ms)
                            observed_interval_rows += 1
                            observed_interval_reward_sum += reward
                            if reward == 0.0:
                                observed_interval_zero_rows += 1
                        if outcome == "reward_eligible_success":
                            eligible_interval_durations.add(_delta_t_ms(previous, record))
                            eligible_success += 1
                            eligible_reward_sum += reward
                            if reward == 0.0:
                                eligible_zero_reward_rows += 1
                        else:
                            excluded[outcome] += 1

                    pending[key] = _PendingDecision(
                        record=record,
                        start_anchor=anchor,
                        damage_stream_verified=stream_verified,
                    )
                    partition_rows += 1
                    decision_rows += 1
        except (OSError, UnicodeError) as error:
            raise FuryObservedIntervalRewardError(
                f"cannot stream compact partition {partition}: {error}"
            ) from error
        partition_reports.append(
            {
                "partition": str(partition),
                "compressed_bytes": partition_bytes,
                "decision_rows": partition_rows,
                "damage_event_rows_declared": (
                    entry_value.get("selected_event_type_counts", {}).get("DMG")
                    if isinstance(entry_value.get("selected_event_type_counts"), Mapping)
                    else None
                ),
                "damage_observation_present": stream_verified,
            }
        )

    for value in pending.values():
        if _window(value.record).get("next_start_anchor") is None:
            excluded["no_next_start"] += 1
        else:
            excluded["invalid_boundary"] += 1

    excluded_total = sum(excluded.values())
    classified_rows = eligible_success + excluded_total
    classification_closes = classified_rows == decision_rows
    manifest_output = manifest.get("output")
    manifest_output = manifest_output if isinstance(manifest_output, Mapping) else {}
    declared_rows = manifest_output.get("decision_count")
    declared_rows = declared_rows if isinstance(declared_rows, int) else None
    row_count_matches = declared_rows is None or declared_rows == decision_rows
    structural_ok = (
        classification_closes
        and row_count_matches
        and excluded["invalid_boundary"] == 0
        and direct_causal_nonmissing_rows == 0
    )
    reward_contract_usable = (
        structural_ok
        and eligible_success > 0
        and excluded["damage_stream_unverified"] == 0
    )
    return {
        "schema": SCHEMA,
        "generated_at": _utc_now(),
        "status": "ok" if structural_ok else "failed",
        "input": {
            "manifest": str(path),
            "declared_decision_rows": declared_rows,
            "partition_count": len(partition_reports),
            "compressed_bytes": compressed_bytes,
            "partitions": partition_reports,
            "partitions_streamed_once": True,
        },
        "contract": {
            "reward_name": "observed_interval_outgoing_damage",
            "objective": "raw_outgoing_damage_all_targets",
            "interval": "[current START, next START) for the same instance x encounter x player",
            "reward_source_field": (
                "window_until_next_start_candidate.outgoing_damage_observed"
            ),
            "reward_provenance": "OBSERVED",
            "time_model": "variable-duration event-indexed SMDP/POMDP transition",
            "eligible_action": (
                "MAPPED_ACTIVE lane in {gcd, off_gcd} with unique succeeded result"
            ),
            "direct_causal_attribution": "MISSING",
            "causal_reward_rows": 0,
        },
        "coverage": {
            "decision_rows": decision_rows,
            "observed_interval_rows": observed_interval_rows,
            "reward_eligible_success_rows": eligible_success,
            "excluded_rows": dict(excluded),
            "excluded_rows_total": excluded_total,
            "classified_rows": classified_rows,
            "classification_closes": classification_closes,
            "observed_interval_reward_sum": observed_interval_reward_sum,
            "observed_interval_zero_reward_rows": observed_interval_zero_rows,
            "observed_interval_delta_t_ms": observed_interval_durations.as_dict(),
            "eligible_reward_sum": eligible_reward_sum,
            "eligible_zero_reward_rows": eligible_zero_reward_rows,
            "eligible_interval_delta_t_ms": eligible_interval_durations.as_dict(),
        },
        "quality": {
            "row_count_matches_manifest": row_count_matches,
            "invalid_boundary_rows": excluded["invalid_boundary"],
            "direct_causal_nonmissing_rows": direct_causal_nonmissing_rows,
            "reward_contract_usable_for_eligible_intervals": reward_contract_usable,
            "line_level_output_materialized": False,
            "compact_manifest_modified": False,
        },
        "offline_rl_gate": {
            "offline_rl_ready": False,
            "blockers": [
                "final START rows lack an explicit terminal observation and encounter outcome",
                "raw all-target damage lacks boss/add importance and effective-damage weighting",
                "queue intent is unavailable for on-swing actions",
                "the observation contract remains partial and omits required rage and timer state",
                "failed-action legality and failure causes are not reconstructed",
                "damage-stream completeness is not independently attested by the compact manifest",
                "separate GCD/off-GCD START events are not yet grouped into the roadmap factorized multi-lane decision epoch",
            ],
        },
    }


def write_fury_observed_interval_reward_audit(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, UnicodeError) as error:
        raise FuryObservedIntervalRewardError(
            f"cannot write observed interval reward audit {path}: {error}"
        ) from error
    return path


def audit_fury_observed_interval_reward(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> tuple[dict[str, Any], Path]:
    report = build_fury_observed_interval_reward_audit(manifest_path)
    return report, write_fury_observed_interval_reward_audit(report, output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, output = audit_fury_observed_interval_reward(
            args.dataset_manifest,
            output_path=args.output,
        )
    except FuryObservedIntervalRewardError as error:
        print(f"Fury observed interval reward audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "decision_rows": report["coverage"]["decision_rows"],
                "observed_interval_rows": report["coverage"]["observed_interval_rows"],
                "reward_eligible_success_rows": report["coverage"][
                    "reward_eligible_success_rows"
                ],
                "classification_closes": report["coverage"]["classification_closes"],
                "causal_reward_rows": report["contract"]["causal_reward_rows"],
                "offline_rl_ready": report["offline_rl_gate"]["offline_rl_ready"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 1


__all__ = [
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "EXCLUSION_REASONS",
    "FuryObservedIntervalRewardError",
    "SCHEMA",
    "audit_fury_observed_interval_reward",
    "build_fury_observed_interval_reward_audit",
    "main",
    "write_fury_observed_interval_reward_audit",
]


if __name__ == "__main__":
    raise SystemExit(main())
