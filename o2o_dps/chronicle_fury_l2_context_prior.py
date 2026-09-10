"""Fit an analysis-only Fury behavior prior from Chronicle L2 context.

The input is the compact L2 temporal sidecar plus its original decision
partition.  Both gzip streams are consumed in lockstep and every row is joined
by ``source_instance_ref``, ``encounter_id``, and ``decision_id`` before the
action label is accepted.  Only prefix evidence available at decision START is
used.  The resulting empirical prior is intentionally not a live policy.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
from itertools import zip_longest
import json
import math
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .chronicle_fury_behavior_prior import (
    BehaviorPriorError,
    EmpiricalBackoffPrior,
    Example,
    MIN_CONTEXT_COUNT,
    SMOOTHING_ALPHA,
    _example_from_record,
)


SCHEMA_VERSION = 1
FOLD_COUNT = 5
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_L2_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_fury_l2_temporal_join"
    / "v1"
    / "manifest.json"
)
DEFAULT_REFERENCE_EVALUATION = (
    PROJECT_ROOT
    / "offline_data"
    / "behavior_models"
    / "fury_partial_markov_v1"
    / "evaluation.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "behavior_models"
    / "fury_l2_context_prior_v1"
)

L2_MANIFEST_KIND = "chronicle_fury_l2_temporal_join_manifest_v1"
L2_ROW_SCHEMA = "chronicle_fury_l2_temporal_context/v1"
MODEL_KIND = "chronicle_fury_l2_context_behavior_prior"
EVALUATION_KIND = "chronicle_fury_l2_context_behavior_prior_evaluation"

CONSUMED_L2_FIELDS = (
    "target_count",
    "pile_cohit_components",
    "wave_elapsed_ms",
    "recent_action_history",
)
NEVER_USED = (
    "final wave target count",
    "current or final wave duration",
    "previous wave duration",
    "kill budget or inferred target HP",
    "alive-target final state or per-target death future",
    "absolute rage",
    "queue intent",
    "GCD, cooldown, cast, or swing timers",
    "player or target HP",
    "future events, next-decision outcomes, or causal reward",
)
TARGET_BUCKETS = (
    "MISSING",
    "OBSERVED_0",
    "OBSERVED_1",
    "OBSERVED_2",
    "OBSERVED_3_4",
    "OBSERVED_GE_5",
)
WAVE_ELAPSED_BUCKETS = (
    "MISSING",
    "OBSERVED_LT_5000_MS",
    "OBSERVED_5000_14999_MS",
    "OBSERVED_15000_29999_MS",
    "OBSERVED_30000_59999_MS",
    "OBSERVED_GE_60000_MS",
)
PILE_EVIDENCE_VALUES = (
    "NO_POSITIVE_COHIT_EVIDENCE",
    "POSITIVE_COHIT_EVIDENCE",
    "MISSING_CONTEXT",
)


class L2ContextPriorError(BehaviorPriorError):
    """The L2 sidecar cannot support the bounded context prior."""


@dataclass(frozen=True)
class L2ContextExample:
    source_instance_ref: str
    player_guid: str
    encounter_id: str
    decision_id: str
    label: str
    successful_action_suffix: tuple[str, ...]
    target_count_bucket: str
    pile_evidence: str
    wave_elapsed_bucket: str
    legacy_example: Example

    @property
    def structural_context(self) -> tuple[str, str, str]:
        return (
            self.target_count_bucket,
            self.pile_evidence,
            self.wave_elapsed_bucket,
        )


@dataclass(frozen=True)
class L2ContextPriorResult:
    model_path: Path
    report_path: Path
    model_card_path: Path
    example_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise L2ContextPriorError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise L2ContextPriorError(f"{label} is not a JSON object: {path}")
    return value


def _resolve_path(raw: Any, base: Path, label: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise L2ContextPriorError(f"missing {label} path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise L2ContextPriorError(f"{path} must be an object")
    return value


def _iter_gzip_objects(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise L2ContextPriorError(
                        f"partition row is not an object at {path}:{line_number}"
                    )
                yield line_number, value
    except L2ContextPriorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise L2ContextPriorError(f"cannot stream partition {path}: {error}") from error


def _decision_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = _mapping(record.get("identity"), "decision.identity")
    action = _mapping(record.get("action"), "decision.action")
    key = (
        str(identity.get("source_instance_ref") or "").strip(),
        str(identity.get("encounter_id") or "").strip(),
        str(action.get("decision_id") or "").strip(),
    )
    if not all(key):
        raise L2ContextPriorError(
            "decision row lacks source_instance_ref/encounter_id/decision_id"
        )
    return key


def _l2_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    if row.get("schema") != L2_ROW_SCHEMA or row.get("schema_version") != 1:
        raise L2ContextPriorError(f"L2 row must use {L2_ROW_SCHEMA}")
    decision = _mapping(row.get("decision"), "l2.decision")
    key = (
        str(decision.get("source_instance_ref") or "").strip(),
        str(decision.get("encounter_id") or "").strip(),
        str(decision.get("decision_id") or "").strip(),
    )
    if not all(key):
        raise L2ContextPriorError(
            "L2 row lacks source_instance_ref/encounter_id/decision_id"
        )
    join = _mapping(row.get("join"), "l2.join")
    join_key = _mapping(join.get("key"), "l2.join.key")
    if (
        str(join_key.get("instance") or "").strip() != key[0]
        or str(join_key.get("encounter") or "").strip() != key[1]
    ):
        raise L2ContextPriorError("L2 join key disagrees with L2 decision identity")
    return key


def _start_anchor(record: Mapping[str, Any]) -> tuple[int, int]:
    source = _mapping(record.get("source"), "decision.source")
    anchor = _mapping(source.get("start_anchor"), "decision.source.start_anchor")
    offset = anchor.get("offset_ms")
    event = anchor.get("event_index")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(event, int)
        or isinstance(event, bool)
    ):
        raise L2ContextPriorError("decision START offset/event index must be integers")
    return offset, event


def _validate_pair(record: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[str, str, str]:
    decision_key = _decision_key(record)
    l2_key = _l2_key(row)
    if decision_key != l2_key:
        raise L2ContextPriorError(
            "exact L2 join mismatch: "
            f"decision={decision_key!r}, sidecar={l2_key!r}"
        )
    offset, event = _start_anchor(record)
    l2_decision = _mapping(row.get("decision"), "l2.decision")
    l2_anchor = _mapping(l2_decision.get("start_anchor"), "l2.decision.start_anchor")
    if l2_anchor.get("offset_ms") != offset or l2_anchor.get("event_index") != event:
        raise L2ContextPriorError(
            f"L2 START anchor mismatch for decision {decision_key[2]}"
        )
    join = _mapping(row.get("join"), "l2.join")
    join_key = _mapping(join.get("key"), "l2.join.key")
    if join_key.get("offset_ms") != offset or join_key.get("event_index") != event:
        raise L2ContextPriorError(
            f"L2 join cutoff mismatch for decision {decision_key[2]}"
        )
    original_action = _mapping(record.get("action"), "decision.action")
    if (
        l2_decision.get("action_spell_id") != original_action.get("spell_id")
        or l2_decision.get("player_guid") != _mapping(
            record.get("identity"), "decision.identity"
        ).get("player_guid")
    ):
        raise L2ContextPriorError(
            f"L2 action/player identity mismatch for decision {decision_key[2]}"
        )
    return decision_key


def _field(row: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    fields = _mapping(row.get("fields"), "l2.fields")
    value = _mapping(fields.get(name), f"l2.fields.{name}")
    if value.get("mask") not in ("OBSERVED", "RECONSTRUCTED", "MISSING"):
        raise L2ContextPriorError(f"l2.fields.{name}.mask is invalid")
    if value.get("mask") == "MISSING" and value.get("value") is not None:
        raise L2ContextPriorError(f"MISSING l2.fields.{name} must be null")
    return value


def _target_count_bucket(field: Mapping[str, Any]) -> str:
    if field.get("mask") == "MISSING":
        return "MISSING"
    value = field.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise L2ContextPriorError(
            "reconstructed target_count must be a nonnegative integer"
        )
    if value == 0:
        return "OBSERVED_0"
    if value == 1:
        return "OBSERVED_1"
    if value == 2:
        return "OBSERVED_2"
    if value <= 4:
        return "OBSERVED_3_4"
    return "OBSERVED_GE_5"


def _wave_elapsed_bucket(field: Mapping[str, Any]) -> str:
    if field.get("mask") == "MISSING":
        return "MISSING"
    value = field.get("value")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
    ):
        raise L2ContextPriorError(
            "reconstructed wave_elapsed_ms must be nonnegative numeric"
        )
    if value < 5_000:
        return "OBSERVED_LT_5000_MS"
    if value < 15_000:
        return "OBSERVED_5000_14999_MS"
    if value < 30_000:
        return "OBSERVED_15000_29999_MS"
    if value < 60_000:
        return "OBSERVED_30000_59999_MS"
    return "OBSERVED_GE_60000_MS"


def _pile_evidence(row: Mapping[str, Any], field: Mapping[str, Any]) -> str:
    if field.get("mask") == "MISSING":
        join = _mapping(row.get("join"), "l2.join")
        if join.get("status") != "MATCHED":
            return "MISSING_CONTEXT"
        # This is explicitly an absence of positive prefix evidence.  It is
        # not interpreted as evidence that targets were separated.
        return "NO_POSITIVE_COHIT_EVIDENCE"
    value = _mapping(field.get("value"), "l2.fields.pile_cohit_components.value")
    components = value.get("components")
    if not isinstance(components, list) or not components:
        raise L2ContextPriorError(
            "reconstructed pile_cohit_components requires nonempty components"
        )
    return "POSITIVE_COHIT_EVIDENCE"


def _history_identity(item: Mapping[str, Any]) -> str:
    spell_id = item.get("spell_id")
    if isinstance(spell_id, int) and not isinstance(spell_id, bool):
        return f"id:{spell_id}"
    name = str(item.get("spell_name") or "").strip().casefold()
    return f"name:{name}" if name else "unknown"


def _successful_suffix(
    values: Any,
    *,
    cutoff_event_index: int,
    path: str,
    source_tail_limit: int | None = None,
) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise L2ContextPriorError(f"{path} must be a list")
    source = values[-source_tail_limit:] if source_tail_limit else values
    tokens: list[str] = []
    for index, raw_item in enumerate(source):
        item = _mapping(raw_item, f"{path}[{index}]")
        result_event_index = item.get("result_event_index")
        if (
            not isinstance(result_event_index, int)
            or isinstance(result_event_index, bool)
            or result_event_index >= cutoff_event_index
        ):
            raise L2ContextPriorError(
                f"{path}[{index}] is not strictly pre-decision"
            )
        if str(item.get("result") or "").casefold() == "succeeded":
            tokens.append(_history_identity(item))
    return tuple(tokens[-2:])


def _joined_example_from_records(
    record: Mapping[str, Any], row: Mapping[str, Any]
) -> L2ContextExample | None:
    key = _validate_pair(record, row)
    legacy = _example_from_record(dict(record))
    if legacy is None:
        return None
    fields = _mapping(row.get("fields"), "l2.fields")
    for name in CONSUMED_L2_FIELDS:
        if name not in fields:
            raise L2ContextPriorError(f"L2 row lacks consumed field {name}")

    _, cutoff_event_index = _start_anchor(record)
    history_field = _field(row, "recent_action_history")
    if history_field.get("mask") == "MISSING":
        raise L2ContextPriorError(
            "eligible decision has MISSING recent_action_history in L2 sidecar"
        )
    l2_suffix = _successful_suffix(
        history_field.get("value"),
        cutoff_event_index=cutoff_event_index,
        path="l2.fields.recent_action_history.value",
    )
    original_state = _mapping(record.get("state_before"), "decision.state_before")
    original_suffix = _successful_suffix(
        original_state.get("recent_uniquely_linked_server_actions"),
        cutoff_event_index=cutoff_event_index,
        path="decision.state_before.recent_uniquely_linked_server_actions",
        source_tail_limit=8,
    )
    if l2_suffix != original_suffix:
        raise L2ContextPriorError(
            f"L2 successful-action suffix mismatch for decision {key[2]}"
        )

    return L2ContextExample(
        source_instance_ref=legacy.source_instance_ref,
        player_guid=legacy.player_guid,
        encounter_id=legacy.encounter_id,
        decision_id=legacy.decision_id,
        label=legacy.label,
        successful_action_suffix=l2_suffix,
        target_count_bucket=_target_count_bucket(_field(row, "target_count")),
        pile_evidence=_pile_evidence(row, _field(row, "pile_cohit_components")),
        wave_elapsed_bucket=_wave_elapsed_bucket(_field(row, "wave_elapsed_ms")),
        legacy_example=legacy,
    )


def load_joined_examples(
    l2_manifest_path: str | Path,
) -> tuple[list[L2ContextExample], dict[str, Any]]:
    path = Path(l2_manifest_path).expanduser().resolve()
    manifest = _load_object(path, "L2 temporal-join manifest")
    if (
        manifest.get("kind") != L2_MANIFEST_KIND
        or manifest.get("schema_version") != 1
    ):
        raise L2ContextPriorError("unsupported L2 temporal-join manifest")
    inputs = _mapping(manifest.get("inputs"), "manifest.inputs")
    if inputs.get("normalized_or_raw_rows_read") != 0:
        raise L2ContextPriorError(
            "L2 manifest must attest normalized_or_raw_rows_read == 0"
        )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise L2ContextPriorError("L2 manifest has no partitions")

    examples: list[L2ContextExample] = []
    decision_rows_read = 0
    l2_rows_read = 0
    exact_key_matches = 0
    join_status_counts: Counter[str] = Counter()
    partition_info: list[dict[str, Any]] = []
    for partition_index, raw_entry in enumerate(partitions):
        entry = _mapping(raw_entry, f"manifest.partitions[{partition_index}]")
        decision_path = _resolve_path(
            entry.get("decision_partition"), path.parent, "decision partition"
        )
        l2_path = _resolve_path(
            entry.get("output_partition"), path.parent, "L2 partition"
        )
        rows_in_partition = 0
        paired = zip_longest(
            _iter_gzip_objects(decision_path),
            _iter_gzip_objects(l2_path),
        )
        for pair_index, pair in enumerate(paired, start=1):
            decision_item, l2_item = pair
            if decision_item is None or l2_item is None:
                raise L2ContextPriorError(
                    "decision/L2 partition length mismatch at "
                    f"{decision_path.name}/{l2_path.name} row {pair_index}"
                )
            _, decision = decision_item
            _, l2_row = l2_item
            decision_rows_read += 1
            l2_rows_read += 1
            rows_in_partition += 1
            _validate_pair(decision, l2_row)
            exact_key_matches += 1
            join = _mapping(l2_row.get("join"), "l2.join")
            join_status_counts[str(join.get("status") or "MISSING_STATUS")] += 1
            example = _joined_example_from_records(decision, l2_row)
            if example is not None:
                examples.append(example)
        declared = entry.get("record_count")
        if isinstance(declared, int) and declared != rows_in_partition:
            raise L2ContextPriorError(
                f"partition row count {rows_in_partition} != manifest {declared}"
            )
        partition_info.append(
            {
                "decision_partition": decision_path.name,
                "l2_partition": l2_path.name,
                "rows": rows_in_partition,
            }
        )

    summary = _mapping(manifest.get("summary"), "manifest.summary")
    declared_total = summary.get("record_count")
    if isinstance(declared_total, int) and declared_total != l2_rows_read:
        raise L2ContextPriorError(
            f"L2 row count {l2_rows_read} != manifest {declared_total}"
        )
    if not examples:
        raise L2ContextPriorError("joined dataset has no eligible behavior labels")

    coverage = {
        "target_count_bucket": dict(
            sorted(Counter(value.target_count_bucket for value in examples).items())
        ),
        "pile_evidence": dict(
            sorted(Counter(value.pile_evidence for value in examples).items())
        ),
        "wave_elapsed_bucket": dict(
            sorted(Counter(value.wave_elapsed_bucket for value in examples).items())
        ),
        "successful_suffix_length": dict(
            sorted(
                Counter(
                    str(len(value.successful_action_suffix)) for value in examples
                ).items()
            )
        ),
    }
    return examples, {
        "l2_manifest": str(path),
        "decision_manifest": str(inputs.get("decision_manifest") or ""),
        "partitions": len(partitions),
        "decision_rows_read": decision_rows_read,
        "l2_rows_read": l2_rows_read,
        "exact_three_key_matches": exact_key_matches,
        "eligible_examples": len(examples),
        "join_status_counts": dict(sorted(join_status_counts.items())),
        "coverage": coverage,
        "partition_audit": partition_info,
        "normalized_or_raw_rows_read": 0,
    }


class ContextBackoffPrior:
    """Empirical counts over successful suffixes with optional L2 context."""

    FULL_LEVELS = (
        "l2_plus_success_suffix_2",
        "l2_plus_success_suffix_1",
        "success_suffix_2",
        "l2_context",
        "success_suffix_1",
    )
    SUFFIX_LEVELS = ("success_suffix_2", "success_suffix_1")

    def __init__(
        self,
        labels: Sequence[str],
        *,
        include_l2: bool,
        min_context_count: int = MIN_CONTEXT_COUNT,
        alpha: float = SMOOTHING_ALPHA,
    ) -> None:
        if not labels:
            raise L2ContextPriorError("label vocabulary must not be empty")
        self.labels = tuple(sorted(set(labels)))
        self.include_l2 = include_l2
        self.min_context_count = min_context_count
        self.alpha = alpha
        self.levels = self.FULL_LEVELS if include_l2 else self.SUFFIX_LEVELS
        self.global_counts: Counter[str] = Counter()
        self.context_counts: dict[
            str, dict[tuple[str, ...], Counter[str]]
        ] = {level: defaultdict(Counter) for level in self.levels}

    @staticmethod
    def _context(
        example: L2ContextExample, level: str
    ) -> tuple[str, ...] | None:
        suffix = example.successful_action_suffix
        structural = example.structural_context
        if level == "l2_plus_success_suffix_2":
            return (*structural, *suffix[-2:]) if len(suffix) >= 2 else None
        if level == "l2_plus_success_suffix_1":
            return (*structural, suffix[-1]) if suffix else None
        if level == "success_suffix_2":
            return tuple(suffix[-2:]) if len(suffix) >= 2 else None
        if level == "l2_context":
            return structural
        if level == "success_suffix_1":
            return (suffix[-1],) if suffix else None
        raise L2ContextPriorError(f"unknown context level {level}")

    def fit(self, examples: Iterable[L2ContextExample]) -> None:
        for example in examples:
            self.global_counts[example.label] += 1
            for level in self.levels:
                key = self._context(example, level)
                if key is not None:
                    self.context_counts[level][key][example.label] += 1
        if not self.global_counts:
            raise L2ContextPriorError("training fold has no examples")

    def probabilities(
        self, example: L2ContextExample
    ) -> tuple[dict[str, float], str]:
        selected = self.global_counts
        selected_level = "global"
        for level in self.levels:
            key = self._context(example, level)
            if key is None:
                continue
            counts = self.context_counts[level].get(key)
            if counts is not None and sum(counts.values()) >= self.min_context_count:
                selected = counts
                selected_level = level
                break
        total = sum(selected.values())
        denominator = total + self.alpha * len(self.labels)
        return (
            {
                label: (selected.get(label, 0) + self.alpha) / denominator
                for label in self.labels
            },
            selected_level,
        )

    def to_document(self) -> dict[str, Any]:
        tables: dict[str, list[dict[str, Any]]] = {}
        for level in self.levels:
            rows = []
            for key, counts in sorted(self.context_counts[level].items()):
                support = sum(counts.values())
                if support < self.min_context_count:
                    continue
                rows.append(
                    {
                        "key": list(key),
                        "support": support,
                        "counts": dict(sorted(counts.items())),
                    }
                )
            tables[level] = rows
        return {
            "levels_in_backoff_order": list(self.levels),
            "minimum_context_count": self.min_context_count,
            "additive_smoothing_alpha": self.alpha,
            "label_vocabulary": list(self.labels),
            "global_counts": dict(sorted(self.global_counts.items())),
            "retained_contexts": {
                level: len(rows) for level, rows in tables.items()
            },
            "contexts": tables,
        }


class _LegacyAdapter:
    def __init__(self, labels: Sequence[str]) -> None:
        self.model = EmpiricalBackoffPrior(labels, max_order=2)

    def fit(self, examples: Iterable[L2ContextExample]) -> None:
        self.model.fit(value.legacy_example for value in examples)

    def probabilities(
        self, example: L2ContextExample
    ) -> tuple[dict[str, float], str]:
        probabilities, order = self.model.probabilities(example.legacy_example)
        return probabilities, f"legacy_order_{order}"


class _CompactMetrics:
    def __init__(self, labels: Sequence[str]) -> None:
        self.labels = tuple(labels)
        self.support: Counter[str] = Counter()
        self.correct: Counter[str] = Counter()
        self.backoff: Counter[str] = Counter()
        self.total = 0
        self.top1 = 0
        self.top3 = 0
        self.log_loss_sum = 0.0

    def add(self, model: Any, example: L2ContextExample) -> None:
        probabilities, level = model.probabilities(example)
        ranked = sorted(probabilities, key=lambda key: (-probabilities[key], key))
        self.total += 1
        self.support[example.label] += 1
        self.backoff[level] += 1
        if ranked[0] == example.label:
            self.top1 += 1
            self.correct[example.label] += 1
        if example.label in ranked[:3]:
            self.top3 += 1
        self.log_loss_sum += -math.log(
            max(probabilities.get(example.label, 0.0), 1e-15)
        )

    def document(self, *, per_class: bool) -> dict[str, Any]:
        if not self.total:
            raise L2ContextPriorError("evaluation fold has no examples")
        supported = [label for label in self.labels if self.support[label]]
        recalls = {
            label: self.correct[label] / self.support[label] for label in supported
        }
        result: dict[str, Any] = {
            "examples": self.total,
            "top1_accuracy": self.top1 / self.total,
            "top3_accuracy": self.top3 / self.total,
            "log_loss": self.log_loss_sum / self.total,
            "macro_recall": sum(recalls.values()) / len(recalls),
            "backoff_coverage": {
                "counts": dict(sorted(self.backoff.items())),
                "fractions": {
                    name: count / self.total
                    for name, count in sorted(self.backoff.items())
                },
            },
        }
        if per_class:
            result["per_class"] = {
                label: {
                    "support": self.support[label],
                    "top1_correct": self.correct[label],
                    "recall": recalls[label],
                }
                for label in supported
            }
        return result


def assign_instance_folds(
    examples: Sequence[L2ContextExample], fold_count: int = FOLD_COUNT
) -> tuple[list[list[L2ContextExample]], list[dict[str, Any]]]:
    groups: dict[str, list[L2ContextExample]] = defaultdict(list)
    for example in examples:
        groups[example.source_instance_ref].append(example)
    if len(groups) < fold_count:
        raise L2ContextPriorError(
            f"need at least {fold_count} source instances; found {len(groups)}"
        )
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    folds: list[list[L2ContextExample]] = [[] for _ in range(fold_count)]
    fold_instances: list[list[str]] = [[] for _ in range(fold_count)]
    for instance, group in ordered:
        destination = min(range(fold_count), key=lambda index: (len(folds[index]), index))
        folds[destination].extend(group)
        fold_instances[destination].append(instance)
    audit = [
        {
            "fold": index,
            "example_count": len(fold),
            "instance_count": len(fold_instances[index]),
            "source_instance_refs": sorted(fold_instances[index]),
        }
        for index, fold in enumerate(folds)
    ]
    return folds, audit


def _models(labels: Sequence[str]) -> dict[str, Any]:
    return {
        "legacy_markov_2_replay": _LegacyAdapter(labels),
        "successful_suffix_2": ContextBackoffPrior(labels, include_l2=False),
        "l2_context_successful_suffix_2": ContextBackoffPrior(
            labels, include_l2=True
        ),
    }


def _metric_delta(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, float]:
    return {
        "top1_accuracy": candidate["top1_accuracy"] - baseline["top1_accuracy"],
        "top3_accuracy": candidate["top3_accuracy"] - baseline["top3_accuracy"],
        "log_loss_reduction": baseline["log_loss"] - candidate["log_loss"],
        "macro_recall": candidate["macro_recall"] - baseline["macro_recall"],
    }


def cross_validate(
    examples: Sequence[L2ContextExample], fold_count: int = FOLD_COUNT
) -> dict[str, Any]:
    labels = sorted({example.label for example in examples})
    folds, fold_assignment = assign_instance_folds(examples, fold_count)
    aggregate = {name: _CompactMetrics(labels) for name in _models(labels)}
    fold_documents: list[dict[str, Any]] = []
    for fold_index, held_out in enumerate(folds):
        training = [
            value
            for index, fold in enumerate(folds)
            if index != fold_index
            for value in fold
        ]
        models = _models(labels)
        fold_metrics = {name: _CompactMetrics(labels) for name in models}
        for model in models.values():
            model.fit(training)
        for example in held_out:
            for name, model in models.items():
                aggregate[name].add(model, example)
                fold_metrics[name].add(model, example)

        train_instances = {value.source_instance_ref for value in training}
        held_out_instances = {value.source_instance_ref for value in held_out}
        instance_overlap = sorted(train_instances & held_out_instances)
        if instance_overlap:
            raise L2ContextPriorError("instance-disjoint split leaked")
        train_players = {value.player_guid for value in training}
        held_out_players = {value.player_guid for value in held_out}
        fold_documents.append(
            {
                "fold": fold_index,
                "training_examples": len(training),
                "held_out_examples": len(held_out),
                "instance_overlap": instance_overlap,
                "player_overlap_count": len(train_players & held_out_players),
                "metrics": {
                    name: metric.document(per_class=False)
                    for name, metric in fold_metrics.items()
                },
            }
        )

    aggregate_document = {
        name: metric.document(per_class=True) for name, metric in aggregate.items()
    }
    candidate = aggregate_document["l2_context_successful_suffix_2"]
    return {
        "protocol": {
            "folds": fold_count,
            "grouping": "source_instance_ref",
            "instance_disjoint": True,
            "row_random_split": False,
            "player_disjoint": False,
            "player_overlap_is_recorded_not_hidden": True,
            "minimum_context_count": MIN_CONTEXT_COUNT,
            "additive_smoothing_alpha": SMOOTHING_ALPHA,
            "threshold_tuning": "none",
        },
        "fold_assignment": fold_assignment,
        "folds": fold_documents,
        "aggregate_held_out": aggregate_document,
        "paired_deltas": {
            "l2_minus_legacy_markov_2_replay": _metric_delta(
                candidate, aggregate_document["legacy_markov_2_replay"]
            ),
            "l2_minus_successful_suffix_2": _metric_delta(
                candidate, aggregate_document["successful_suffix_2"]
            ),
        },
    }


def _reference_metrics(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    document = _load_object(path.resolve(), "legacy behavior-prior evaluation")
    try:
        metrics = document["cross_validation"]["aggregate_held_out"]["markov_2"]
        protocol = document["cross_validation"]["protocol"]
    except (KeyError, TypeError) as error:
        raise L2ContextPriorError(
            "legacy behavior-prior evaluation lacks markov_2 metrics"
        ) from error
    return {
        "path": str(path.resolve()),
        "protocol": protocol,
        "metrics": {
            name: metrics[name]
            for name in (
                "examples",
                "top1_accuracy",
                "top3_accuracy",
                "log_loss",
                "macro_recall",
            )
        },
        "direct_pairing_note": (
            "reference used source/player connected components; use the paired "
            "legacy_markov_2_replay result for same-fold deltas"
        ),
    }


def build_documents(
    examples: Sequence[L2ContextExample],
    *,
    input_info: Mapping[str, Any],
    reference_evaluation: Path | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    labels = sorted({example.label for example in examples})
    evaluation = cross_validate(examples)
    final_model = ContextBackoffPrior(labels, include_l2=True)
    final_model.fit(examples)
    generated_at = _utc_now()
    feature_contract = {
        "consumed_l2_fields": list(CONSUMED_L2_FIELDS),
        "target_count_semantics": (
            "observed-so-far hostile GUID bucket; not final simultaneous count"
        ),
        "pile_semantics": (
            "positive strictly-prior melee-reach co-hit evidence only; absence "
            "does not mean separated"
        ),
        "wave_elapsed_semantics": "decision START offset minus prefix wave start",
        "history_semantics": "last two prior uniquely-linked succeeded server actions",
        "never_used": list(NEVER_USED),
        "missing_policy": "explicit category or positive-evidence absence; no value imputation",
    }
    model = {
        "schema_version": SCHEMA_VERSION,
        "kind": MODEL_KIND,
        "generated_at": generated_at,
        "analysis_only": True,
        "deployment_allowed": False,
        "feature_contract": feature_contract,
        "training_examples": len(examples),
        "model": final_model.to_document(),
    }
    reference = _reference_metrics(reference_evaluation)
    aggregate = evaluation["aggregate_held_out"]
    legacy = aggregate["legacy_markov_2_replay"]
    suffix = aggregate["successful_suffix_2"]
    candidate = aggregate["l2_context_successful_suffix_2"]
    candidate_improves_legacy = (
        candidate["top1_accuracy"] > legacy["top1_accuracy"]
        and candidate["log_loss"] < legacy["log_loss"]
    )
    comparison_contract = {
        "legacy_markov_2_replay": (
            "frozen comparator only: prior successful/failed action tokens plus "
            "the existing auto-attack-elapsed mask bucket"
        ),
        "successful_suffix_2": "last two prior successful actions only",
        "l2_context_successful_suffix_2": (
            "the four allowed prefix features in feature_contract"
        ),
        "same_instance_folds_for_all_three": True,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "generated_at": generated_at,
        "input": dict(input_info),
        "feature_contract": feature_contract,
        "comparison_feature_contract": comparison_contract,
        "label_support": dict(sorted(Counter(value.label for value in examples).items())),
        "cross_validation": evaluation,
        "existing_fury_partial_markov_v1_reference": reference,
        "interpretation": {
            "unit": "held-out uniquely linked successful server START label",
            "promotion_status": "ANALYSIS_ONLY_NOT_DEPLOYABLE",
            "context_gain_is_behavior_prediction_not_dps_gain": True,
            "offline_rl_ready": False,
            "candidate_improves_paired_legacy_top1_and_log_loss": (
                candidate_improves_legacy
            ),
            "replacement_judgment": (
                "KEEP_LEGACY_PRIOR; L2 CONTEXT DID NOT IMPROVE HELD-OUT "
                "TOP1 AND LOG LOSS"
                if not candidate_improves_legacy
                else "L2 CONTEXT IMPROVED PAIRED TOP1 AND LOG LOSS; STILL ANALYSIS ONLY"
            ),
        },
    }
    result_sentence = (
        "Held-out result: keep the legacy prior; adding this L2 context reduced "
        "Top-1 accuracy and increased log loss."
        if not candidate_improves_legacy
        else "Held-out result: L2 context improved paired Top-1 and log loss, but remains analysis-only."
    )
    model_card = "\n".join(
        [
            "# Fury Chronicle L2 context behavior prior",
            "",
            "Analysis-only empirical backoff prior over successful server START labels.",
            "It is not offline RL, a DPS reward model, or a deployable Cat2 policy.",
            "",
            f"Eligible exact-joined examples: {len(examples)} across {input_info['partitions']} partitions.",
            "Evaluation: deterministic five-fold source-instance-disjoint CV.",
            "",
            "| Model | Top-1 | Top-3 | Log loss | Macro recall |",
            "|---|---:|---:|---:|---:|",
            (
                "| legacy Markov2 replay | "
                f"{legacy['top1_accuracy']:.4f} | {legacy['top3_accuracy']:.4f} | "
                f"{legacy['log_loss']:.4f} | {legacy['macro_recall']:.4f} |"
            ),
            (
                "| successful suffix2 | "
                f"{suffix['top1_accuracy']:.4f} | {suffix['top3_accuracy']:.4f} | "
                f"{suffix['log_loss']:.4f} | {suffix['macro_recall']:.4f} |"
            ),
            (
                "| L2 context + successful suffix2 | "
                f"{candidate['top1_accuracy']:.4f} | {candidate['top3_accuracy']:.4f} | "
                f"{candidate['log_loss']:.4f} | {candidate['macro_recall']:.4f} |"
            ),
            "",
            "Consumed context is limited to observed-so-far target-count bucket,",
            "strictly-prior positive co-hit evidence, wave-elapsed bucket, and the",
            "last two successful server-observed actions. Missing values are not",
            "filled. Final wave facts, HP/kill budgets, rage, timers, queue intent,",
            "future outcomes, and causal reward are never consumed.",
            "",
            result_sentence,
            "",
            "Deployment remains disabled regardless of predictive metrics.",
            "",
        ]
    )
    return model, report, model_card


def _write_json(path: Path, value: Mapping[str, Any], *, compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if compact:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def train_l2_context_prior(
    l2_manifest: str | Path = DEFAULT_L2_MANIFEST,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT,
    reference_evaluation: str | Path | None = DEFAULT_REFERENCE_EVALUATION,
) -> L2ContextPriorResult:
    examples, input_info = load_joined_examples(l2_manifest)
    reference = (
        None
        if reference_evaluation is None
        else Path(reference_evaluation).expanduser().resolve()
    )
    model, report, model_card = build_documents(
        examples,
        input_info=input_info,
        reference_evaluation=reference,
    )
    output = Path(output_dir).expanduser().resolve()
    model_path = output / "model.json"
    report_path = output / "evaluation.json"
    card_path = output / "MODEL_CARD.md"
    _write_json(model_path, model, compact=True)
    _write_json(report_path, report, compact=False)
    card_path.write_text(model_card, encoding="utf-8")
    return L2ContextPriorResult(
        model_path=model_path,
        report_path=report_path,
        model_card_path=card_path,
        example_count=len(examples),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit the analysis-only Fury Chronicle L2 context prior."
    )
    parser.add_argument("--l2-manifest", type=Path, default=DEFAULT_L2_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--reference-evaluation",
        type=Path,
        default=DEFAULT_REFERENCE_EVALUATION,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = train_l2_context_prior(
            args.l2_manifest,
            output_dir=args.output_dir,
            reference_evaluation=args.reference_evaluation,
        )
    except (L2ContextPriorError, OSError) as error:
        print(f"L2 context prior failed: {error}")
        return 2
    print(
        json.dumps(
            {
                "model": str(result.model_path),
                "evaluation": str(result.report_path),
                "model_card": str(result.model_card_path),
                "eligible_examples": result.example_count,
                "analysis_only": True,
                "deployment_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
