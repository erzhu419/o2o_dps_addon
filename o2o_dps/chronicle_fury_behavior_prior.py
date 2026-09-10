"""Fit an analysis-only Fury action prior from partial Chronicle observations.

This module deliberately does not estimate rewards, action legality, queue
intent, or a full game state.  Its label is the next uniquely linked,
successful server START candidate in the compact Fury decision dataset.  The
model is a small empirical backoff Markov prior implemented with the standard
library so its evidence boundary remains easy to inspect.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
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
    PROJECT_ROOT / "offline_data" / "behavior_models" / "fury_partial_markov_v1"
)
DEFAULT_CAT2_CARDS = PROJECT_ROOT.parent / "Cat2" / "Cards" / "Warrior"

# This mapping is intentionally explicit.  Mechanics keys and Cat2 card IDs
# are separate published interfaces; punctuation replacement is not a contract.
ACTION_TO_CAT2_CARD = {
    "warrior.battle_shout": "warrior_battle_shout",
    "warrior.battle_stance": "warrior_battle_stance",
    "warrior.berserker_stance": "warrior_berserker_stance",
    "warrior.bloodrage": "warrior_bloodrage",
    "warrior.bloodthirst": "warrior_bloodthirst",
    "warrior.death_wish": "warrior_death_wish",
    "warrior.defensive_stance": "warrior_defensive_stance",
    "warrior.execute": "warrior_execute",
    "warrior.slam": "warrior_slam",
    "warrior.sunder_armor": "warrior_sunder_armor",
    "warrior.whirlwind": "warrior_whirlwind",
}

MODEL_NAMES = ("global", "last_action", "markov_2")
FOLD_COUNT = 5
MIN_CONTEXT_COUNT = 3
SMOOTHING_ALPHA = 0.5
CARD_ID_PATTERN = re.compile(r'\bid\s*=\s*"([a-z][a-z0-9_]*)"')


class BehaviorPriorError(RuntimeError):
    """The supplied dataset cannot support the bounded behavior prior."""


@dataclass(frozen=True)
class Example:
    source_instance_ref: str
    player_guid: str
    encounter_id: str
    decision_id: str
    label: str
    recent_action_tokens: tuple[str, ...]
    swing_bucket: str


@dataclass(frozen=True)
class BehaviorPriorResult:
    model_path: Path
    report_path: Path
    model_card_path: Path
    example_count: int


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


class EmpiricalBackoffPrior:
    """Additively smoothed counts with suffix backoff to a global prior."""

    def __init__(
        self,
        labels: Sequence[str],
        *,
        max_order: int,
        min_context_count: int = MIN_CONTEXT_COUNT,
        alpha: float = SMOOTHING_ALPHA,
    ) -> None:
        if max_order not in (0, 1, 2):
            raise BehaviorPriorError("max_order must be 0, 1, or 2")
        if not labels:
            raise BehaviorPriorError("label vocabulary must not be empty")
        self.labels = tuple(sorted(set(labels)))
        self.max_order = max_order
        self.min_context_count = min_context_count
        self.alpha = alpha
        self.global_counts: Counter[str] = Counter()
        self.context_counts: dict[int, dict[tuple[str, ...], Counter[str]]] = {
            order: defaultdict(Counter) for order in range(1, max_order + 1)
        }

    @staticmethod
    def context(example: Example, order: int) -> tuple[str, ...]:
        history = example.recent_action_tokens[-order:] if order else ()
        return (example.swing_bucket, *history)

    def fit(self, examples: Iterable[Example]) -> None:
        for example in examples:
            self.global_counts[example.label] += 1
            for order in range(1, self.max_order + 1):
                if len(example.recent_action_tokens) < order:
                    continue
                self.context_counts[order][self.context(example, order)][
                    example.label
                ] += 1
        if not self.global_counts:
            raise BehaviorPriorError("training fold has no eligible examples")

    def probabilities(self, example: Example) -> tuple[dict[str, float], int]:
        selected = self.global_counts
        selected_order = 0
        for order in range(self.max_order, 0, -1):
            if len(example.recent_action_tokens) < order:
                continue
            counts = self.context_counts[order].get(self.context(example, order))
            if counts is not None and sum(counts.values()) >= self.min_context_count:
                selected = counts
                selected_order = order
                break
        total = sum(selected.values())
        denominator = total + self.alpha * len(self.labels)
        return (
            {
                label: (selected.get(label, 0) + self.alpha) / denominator
                for label in self.labels
            },
            selected_order,
        )

    def to_document(self) -> dict[str, Any]:
        contexts: dict[str, list[dict[str, Any]]] = {}
        for order, values in self.context_counts.items():
            contexts[f"order_{order}"] = [
                {
                    "context": {
                        "last_auto_attack_elapsed_bucket": key[0],
                        "recent_action_tokens": list(key[1:]),
                    },
                    "counts": dict(sorted(counts.items())),
                }
                for key, counts in sorted(values.items())
            ]
        return {
            "max_order": self.max_order,
            "minimum_context_count": self.min_context_count,
            "additive_smoothing_alpha": self.alpha,
            "label_vocabulary": list(self.labels),
            "global_counts": dict(sorted(self.global_counts.items())),
            "contexts": contexts,
        }


class _Metrics:
    def __init__(self, labels: Sequence[str]) -> None:
        self.labels = tuple(labels)
        self.support: Counter[str] = Counter()
        self.correct: Counter[str] = Counter()
        self.confusion: dict[str, Counter[str]] = defaultdict(Counter)
        self.context_coverage: Counter[str] = Counter()
        self.total = 0
        self.top1 = 0
        self.top3 = 0
        self.log_loss_sum = 0.0

    def add(self, model: EmpiricalBackoffPrior, example: Example) -> None:
        probabilities, order = model.probabilities(example)
        ranked = sorted(probabilities, key=lambda key: (-probabilities[key], key))
        predicted = ranked[0]
        self.total += 1
        self.support[example.label] += 1
        self.confusion[example.label][predicted] += 1
        self.context_coverage[f"order_{order}"] += 1
        if predicted == example.label:
            self.top1 += 1
            self.correct[example.label] += 1
        if example.label in ranked[:3]:
            self.top3 += 1
        probability = probabilities.get(example.label, 0.0)
        self.log_loss_sum += -math.log(max(probability, 1e-15))

    def document(self) -> dict[str, Any]:
        if not self.total:
            raise BehaviorPriorError("evaluation fold has no eligible examples")
        supported = [label for label in self.labels if self.support[label] > 0]
        recalls = {
            label: self.correct[label] / self.support[label] for label in supported
        }
        coverage_counts = dict(sorted(self.context_coverage.items()))
        return {
            "examples": self.total,
            "top1_accuracy": self.top1 / self.total,
            "top3_accuracy": self.top3 / self.total,
            "log_loss": self.log_loss_sum / self.total,
            "macro_recall": sum(recalls.values()) / len(recalls),
            "per_class": {
                label: {
                    "support": self.support[label],
                    "top1_correct": self.correct[label],
                    "recall": recalls[label],
                }
                for label in supported
            },
            "confusion": {
                actual: dict(sorted(predictions.items()))
                for actual, predictions in sorted(self.confusion.items())
            },
            "context_coverage": {
                "counts": coverage_counts,
                "fractions": {
                    name: count / self.total for name, count in coverage_counts.items()
                },
            },
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BehaviorPriorError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise BehaviorPriorError(f"{label} is not a JSON object: {path}")
    return value


def _swing_bucket(state: dict[str, Any], mask: dict[str, Any]) -> str:
    observed = mask.get("last_auto_attack_elapsed_ms")
    if not isinstance(observed, bool):
        raise BehaviorPriorError(
            "last_auto_attack_elapsed_ms requires an explicit boolean state mask"
        )
    if not observed:
        return "MISSING"
    value = state.get("last_auto_attack_elapsed_ms")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise BehaviorPriorError(
            "last_auto_attack_elapsed_ms is masked present but is not nonnegative numeric"
        )
    if value < 1000:
        return "OBSERVED_LT_1000_MS"
    if value < 2000:
        return "OBSERVED_1000_1999_MS"
    if value < 3000:
        return "OBSERVED_2000_2999_MS"
    return "OBSERVED_GE_3000_MS"


def _history_tokens(
    state: dict[str, Any], start_event_index: int
) -> tuple[str, ...]:
    history = state.get("recent_uniquely_linked_server_actions")
    if not isinstance(history, list):
        raise BehaviorPriorError("recent action history must be a list")
    tokens: list[str] = []
    for item in history[-2:]:
        if not isinstance(item, dict):
            raise BehaviorPriorError("recent action history contains a non-object")
        result_index = item.get("result_event_index")
        if not isinstance(result_index, int) or result_index >= start_event_index:
            raise BehaviorPriorError("recent action history is not strictly pre-decision")
        result = str(item.get("result") or "unknown").casefold()
        spell_id = item.get("spell_id")
        if isinstance(spell_id, int):
            identity = f"id:{spell_id}"
        else:
            name = str(item.get("spell_name") or "").strip().casefold()
            identity = f"name:{name}" if name else "unknown"
        tokens.append(f"{identity}:{result}")
    return tuple(tokens)


def _example_from_record(record: dict[str, Any]) -> Example | None:
    eligibility = record.get("eligibility")
    if not isinstance(eligibility, dict):
        raise BehaviorPriorError("decision record lacks eligibility object")
    if eligibility.get("partial_observation_bc") is not True:
        return None
    action = record.get("action")
    result = record.get("result")
    identity = record.get("identity")
    source = record.get("source")
    state = record.get("state_before")
    mask = record.get("state_mask")
    window = record.get("window_until_next_start_candidate")
    if not all(
        isinstance(value, dict)
        for value in (action, result, identity, source, state, mask, window)
    ):
        raise BehaviorPriorError("eligible decision record lacks required objects")
    if mask.get("recent_uniquely_linked_server_actions") is not True:
        raise BehaviorPriorError("recent action history must have an explicit true mask")
    if (
        result.get("association") != "unique"
        or result.get("status") != "succeeded"
        or action.get("catalog_status") != "MAPPED_ACTIVE"
        or action.get("lane") == "on_swing_unknown_intent"
    ):
        raise BehaviorPriorError("partial-observation label violates dataset gate")
    if (
        eligibility.get("offline_rl") is not False
        or eligibility.get("queue_intent_supervision") is not False
        or eligibility.get("causal_reward_supervision") is not False
        or window.get("causal_reward") is not None
        or window.get("causal_attribution") != "MISSING"
    ):
        raise BehaviorPriorError("eligible row invents queue intent or causal reward")
    anchor = source.get("start_anchor")
    if not isinstance(anchor, dict) or not isinstance(anchor.get("event_index"), int):
        raise BehaviorPriorError("eligible row lacks integer START event anchor")
    label = str(action.get("policy_action_key") or "").strip()
    source_ref = str(identity.get("source_instance_ref") or "").strip()
    player_guid = str(identity.get("player_guid") or "").strip().casefold()
    encounter = str(identity.get("encounter_id") or "").strip()
    decision_id = str(action.get("decision_id") or "").strip()
    if not all((label, source_ref, player_guid, encounter, decision_id)):
        raise BehaviorPriorError("eligible row lacks label or grouping identity")
    return Example(
        source_instance_ref=source_ref,
        player_guid=player_guid,
        encounter_id=encounter,
        decision_id=decision_id,
        label=label,
        recent_action_tokens=_history_tokens(state, anchor["event_index"]),
        swing_bucket=_swing_bucket(state, mask),
    )


def load_examples(manifest_path: str | Path) -> tuple[list[Example], dict[str, Any]]:
    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_object(path, "decision-dataset manifest")
    if manifest.get("schema") != "chronicle_fury_decision_dataset/v1":
        raise BehaviorPriorError("unsupported decision-dataset schema")
    quality = manifest.get("quality")
    if not isinstance(quality, dict) or any(
        (
            quality.get("offline_rl_ready") is not False,
            quality.get("queue_intent_labels") != 0,
            quality.get("causal_reward_rows") != 0,
        )
    ):
        raise BehaviorPriorError("dataset manifest violates partial-observation boundary")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise BehaviorPriorError("dataset manifest has no partitions")
    examples: list[Example] = []
    decisions_read = 0
    for entry in partitions:
        if not isinstance(entry, dict):
            raise BehaviorPriorError("partition manifest entry is not an object")
        partition = Path(str(entry.get("partition") or ""))
        if not partition.is_absolute():
            partition = (path.parent / partition).resolve()
        try:
            with gzip.open(partition, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    decisions_read += 1
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise BehaviorPriorError(
                            f"partition row is not an object at {partition}:{line_number}"
                        )
                    example = _example_from_record(record)
                    if example is not None:
                        examples.append(example)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BehaviorPriorError(f"cannot stream partition {partition}: {error}") from error
    if not examples:
        raise BehaviorPriorError("dataset has no partial_observation_bc=true examples")
    declared = (manifest.get("output") or {}).get("observable_behavior_labels")
    if isinstance(declared, int) and declared != len(examples):
        raise BehaviorPriorError(
            f"eligible label count {len(examples)} does not match manifest {declared}"
        )
    return examples, {
        "manifest": str(path),
        "partitions": len(partitions),
        "decision_rows_read": decisions_read,
        "eligible_examples": len(examples),
    }


def connected_components(examples: Sequence[Example]) -> list[list[Example]]:
    disjoint = _DisjointSet()
    for example in examples:
        disjoint.union(
            f"source:{example.source_instance_ref}", f"player:{example.player_guid}"
        )
    grouped: dict[str, list[Example]] = defaultdict(list)
    for example in examples:
        grouped[disjoint.find(f"source:{example.source_instance_ref}")].append(example)
    components = list(grouped.values())
    components.sort(
        key=lambda values: (
            -len(values),
            min(value.source_instance_ref for value in values),
            min(value.player_guid for value in values),
        )
    )
    return components


def assign_folds(
    examples: Sequence[Example], fold_count: int = FOLD_COUNT
) -> tuple[list[list[Example]], list[dict[str, Any]]]:
    components = connected_components(examples)
    if len(components) < fold_count:
        raise BehaviorPriorError(
            f"need at least {fold_count} source/player components; found {len(components)}"
        )
    folds: list[list[Example]] = [[] for _ in range(fold_count)]
    component_counts = [0] * fold_count
    for component in components:
        destination = min(range(fold_count), key=lambda index: (len(folds[index]), index))
        folds[destination].extend(component)
        component_counts[destination] += 1
    audit = []
    for index, fold in enumerate(folds):
        audit.append(
            {
                "fold": index,
                "component_count": component_counts[index],
                "example_count": len(fold),
                "source_instance_refs": sorted(
                    {value.source_instance_ref for value in fold}
                ),
                "player_guids": sorted({value.player_guid for value in fold}),
            }
        )
    return folds, audit


def cross_validate(examples: Sequence[Example]) -> dict[str, Any]:
    labels = sorted({example.label for example in examples})
    folds, fold_audit = assign_folds(examples)
    aggregate = {name: _Metrics(labels) for name in MODEL_NAMES}
    fold_documents: list[dict[str, Any]] = []
    for fold_index, held_out in enumerate(folds):
        training = [
            example
            for index, fold in enumerate(folds)
            if index != fold_index
            for example in fold
        ]
        models = {
            "global": EmpiricalBackoffPrior(labels, max_order=0),
            "last_action": EmpiricalBackoffPrior(labels, max_order=1),
            "markov_2": EmpiricalBackoffPrior(labels, max_order=2),
        }
        fold_metrics = {name: _Metrics(labels) for name in MODEL_NAMES}
        for model in models.values():
            model.fit(training)
        for example in held_out:
            for name, model in models.items():
                aggregate[name].add(model, example)
                fold_metrics[name].add(model, example)

        train_sources = {value.source_instance_ref for value in training}
        test_sources = {value.source_instance_ref for value in held_out}
        train_players = {value.player_guid for value in training}
        test_players = {value.player_guid for value in held_out}
        source_overlap = sorted(train_sources & test_sources)
        player_overlap = sorted(train_players & test_players)
        if source_overlap or player_overlap:
            raise BehaviorPriorError("source/player connected-component split leaked")
        fold_documents.append(
            {
                "fold": fold_index,
                "training_examples": len(training),
                "held_out_examples": len(held_out),
                "source_overlap": source_overlap,
                "player_overlap": player_overlap,
                "metrics": {
                    name: fold_metrics[name].document() for name in MODEL_NAMES
                },
            }
        )
    return {
        "protocol": {
            "folds": FOLD_COUNT,
            "grouping": "connected components of source_instance_ref and player_guid",
            "row_random_split": False,
            "label_filter": "eligibility.partial_observation_bc == true",
        },
        "fold_assignment": fold_audit,
        "folds": fold_documents,
        "aggregate_held_out": {
            name: aggregate[name].document() for name in MODEL_NAMES
        },
    }


def load_cat2_card_ids(cards_root: str | Path) -> set[str]:
    root = Path(cards_root).expanduser().resolve()
    if not root.is_dir():
        raise BehaviorPriorError(f"Cat2 Warrior cards directory does not exist: {root}")
    card_ids: set[str] = set()
    for path in sorted(root.glob("*.lua")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise BehaviorPriorError(f"cannot read Cat2 card {path}: {error}") from error
        match = CARD_ID_PATTERN.search(text)
        if match:
            card_ids.add(match.group(1))
    return card_ids


def action_mapping_contract(
    labels: Sequence[str], available_card_ids: set[str]
) -> dict[str, Any]:
    unmapped = sorted(set(labels) - ACTION_TO_CAT2_CARD.keys())
    mapped = {
        label: ACTION_TO_CAT2_CARD[label]
        for label in sorted(set(labels) & ACTION_TO_CAT2_CARD.keys())
    }
    missing_cards = sorted(
        card_id for card_id in mapped.values() if card_id not in available_card_ids
    )
    blockers = [
        "analysis-only partial-observation features are not exported by the current PolicyBrain runtime",
        "the prior does not estimate action legality or when to act",
    ]
    if unmapped:
        blockers.append("observed policy_action_key lacks an explicit Cat2 mapping")
    if missing_cards:
        blockers.append("an explicitly mapped Cat2 card ID is not present in the card registry source")
    return {
        "policy_action_key_to_cat2_card_id": mapped,
        "unmapped_action_keys": unmapped,
        "missing_cat2_card_ids": missing_cards,
        "mapping_complete": not unmapped and not missing_cards,
        "deployment_allowed": False,
        "deployment_blockers": blockers,
    }


def build_documents(
    examples: Sequence[Example],
    *,
    input_info: dict[str, Any],
    available_card_ids: set[str],
    cat2_card_source: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    labels = sorted({example.label for example in examples})
    evaluation = cross_validate(examples)
    final_model = EmpiricalBackoffPrior(labels, max_order=2)
    final_model.fit(examples)
    mapping = action_mapping_contract(labels, available_card_ids)
    mapping["cat2_card_source"] = (
        str(Path(cat2_card_source).expanduser().resolve())
        if cat2_card_source is not None
        else None
    )
    generated_at = _utc_now()
    label_support = Counter(example.label for example in examples)
    model = {
        "schema_version": SCHEMA_VERSION,
        "kind": "chronicle_fury_partial_observation_behavior_prior",
        "generated_at": generated_at,
        "analysis_only": True,
        "input": input_info,
        "training_contract": {
            "label_semantics": "next uniquely linked successful server START candidate",
            "eligible_only": "eligibility.partial_observation_bc == true",
            "features": [
                "up to two prior uniquely linked server action IDs and result statuses",
                "explicitly masked bucket of elapsed time since prior observed Auto Attack",
            ],
            "never_used": [
                "absolute rage",
                "target or player HP",
                "GCD or cooldown remaining",
                "queue intent",
                "window damage as reward",
                "causal reward",
                "player, source, leaderboard rank, or encounter identity as a feature",
            ],
            "claims_excluded": [
                "full-state behavior cloning",
                "offline reinforcement learning",
                "DPS improvement",
                "deployable WoW policy",
            ],
        },
        "label_support": dict(sorted(label_support.items())),
        "action_mapping": mapping,
        "model": final_model.to_document(),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "chronicle_fury_partial_observation_behavior_prior_evaluation",
        "generated_at": generated_at,
        "input": input_info,
        "label_support": dict(sorted(label_support.items())),
        "action_mapping": mapping,
        "cross_validation": evaluation,
        "interpretation": {
            "unit": "held-out successful server START label",
            "top3_is_ranked_action_recall_not_a_three-action_execution_plan": True,
            "promotion_status": "ANALYSIS_BASELINE_ONLY",
        },
    }
    metrics = evaluation["aggregate_held_out"]
    card = "\n".join(
        [
            "# Fury partial-observation empirical behavior prior",
            "",
            "This is an analysis-only empirical/backoff Markov prior over Chronicle",
            "server-observed successful START labels. It is not full-state behavior",
            "cloning, offline RL, evidence of DPS improvement, or a deployable policy.",
            "",
            f"Eligible examples: {len(examples)} across {input_info['partitions']} partitions.",
            "Evaluation: deterministic five-fold source/player connected-component CV.",
            "",
            "| Model | Top-1 | Top-3 | Log loss | Macro recall |",
            "|---|---:|---:|---:|---:|",
            *[
                "| {name} | {top1:.4f} | {top3:.4f} | {loss:.4f} | {macro:.4f} |".format(
                    name=name,
                    top1=metrics[name]["top1_accuracy"],
                    top3=metrics[name]["top3_accuracy"],
                    loss=metrics[name]["log_loss"],
                    macro=metrics[name]["macro_recall"],
                )
                for name in MODEL_NAMES
            ],
            "",
            "The model never consumes queue intent or window outcomes as causal reward.",
            "The current PolicyBrain runtime does not expose this model's action-history",
            "features, so deployment remains disabled even when all explicit action-to-card",
            "mappings validate.",
            "",
        ]
    )
    return model, report, card


def train_behavior_prior(
    manifest: str | Path = DEFAULT_MANIFEST,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT,
    cat2_cards: str | Path = DEFAULT_CAT2_CARDS,
) -> BehaviorPriorResult:
    examples, input_info = load_examples(manifest)
    card_ids = load_cat2_card_ids(cat2_cards)
    model, report, model_card = build_documents(
        examples,
        input_info=input_info,
        available_card_ids=card_ids,
        cat2_card_source=cat2_cards,
    )
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "model.json"
    report_path = output / "evaluation.json"
    model_card_path = output / "MODEL_CARD.md"
    model_path.write_text(
        json.dumps(model, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    model_card_path.write_text(model_card, encoding="utf-8", newline="\n")
    return BehaviorPriorResult(model_path, report_path, model_card_path, len(examples))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cat2-cards", type=Path, default=DEFAULT_CAT2_CARDS)
    args = parser.parse_args(argv)
    try:
        result = train_behavior_prior(
            args.dataset_manifest,
            output_dir=args.output_dir,
            cat2_cards=args.cat2_cards,
        )
    except BehaviorPriorError as error:
        print(f"Fury behavior-prior training failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "analysis_baseline_written",
                "examples": result.example_count,
                "model": str(result.model_path),
                "evaluation": str(result.report_path),
                "model_card": str(result.model_card_path),
                "deployment_allowed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
