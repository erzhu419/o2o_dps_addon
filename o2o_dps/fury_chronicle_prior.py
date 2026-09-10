"""Consume the bounded Chronicle Fury behavior model as a search prior.

The loaded model describes server-observed successful ``START`` labels from a
partial history.  It is useful for ordering search proposals, but it does not
provide action legality, timing, reward, or a deployable policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = (
    PROJECT_ROOT
    / "offline_data"
    / "behavior_models"
    / "fury_partial_markov_v1"
    / "model.json"
)

MODEL_KIND = "chronicle_fury_partial_observation_behavior_prior"
PROPOSAL_SCHEMA = "fury_chronicle_prior_proposal/v1"
PROPOSAL_KIND = "fury_chronicle_partial_server_behavior_prior_proposal"
PRIOR_ROLE = "PARTIAL_SERVER_OBSERVED_BEHAVIOR_PRIOR_ONLY"

LAST_AUTO_ATTACK_BUCKETS = frozenset(
    {
        "MISSING",
        "OBSERVED_LT_1000_MS",
        "OBSERVED_1000_1999_MS",
        "OBSERVED_2000_2999_MS",
        "OBSERVED_GE_3000_MS",
    }
)

_REQUIRED_EXCLUDED_CLAIMS = frozenset(
    {
        "full-state behavior cloning",
        "offline reinforcement learning",
        "DPS improvement",
        "deployable WoW policy",
    }
)


class FuryChroniclePriorError(RuntimeError):
    """The model or query violates the partial-prior contract."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryChroniclePriorError(
            f"cannot read Chronicle prior model {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryChroniclePriorError("Chronicle prior model is not a JSON object")
    return value


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise FuryChroniclePriorError(f"{label} must be a positive number")
    return float(value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FuryChroniclePriorError(f"{label} must be a positive integer")
    return value


def _counts(
    value: object, labels: frozenset[str], label: str
) -> dict[str, int]:
    if not isinstance(value, dict):
        raise FuryChroniclePriorError(f"{label} must be an object")
    result: dict[str, int] = {}
    for action_key, count in value.items():
        if action_key not in labels:
            raise FuryChroniclePriorError(
                f"{label} contains action outside label_vocabulary: {action_key!r}"
            )
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise FuryChroniclePriorError(
                f"{label} contains a nonnegative-integer violation"
            )
        if count:
            result[action_key] = count
    if not result:
        raise FuryChroniclePriorError(f"{label} has no positive support")
    return result


class FuryChroniclePrior:
    """Validated order-2 -> order-1 -> global Chronicle proposal prior."""

    def __init__(self, model_path: str | Path = DEFAULT_MODEL) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        document = _load_object(self.model_path)
        if document.get("schema_version") != 1 or document.get("kind") != MODEL_KIND:
            raise FuryChroniclePriorError("unsupported Chronicle prior schema or kind")
        if document.get("analysis_only") is not True:
            raise FuryChroniclePriorError(
                "Chronicle prior must remain explicitly analysis_only"
            )

        training = document.get("training_contract")
        if not isinstance(training, dict):
            raise FuryChroniclePriorError("Chronicle prior lacks training_contract")
        if training.get("label_semantics") != (
            "next uniquely linked successful server START candidate"
        ):
            raise FuryChroniclePriorError("unexpected Chronicle prior label semantics")
        excluded = training.get("claims_excluded")
        if not isinstance(excluded, list) or not _REQUIRED_EXCLUDED_CLAIMS.issubset(
            {str(value) for value in excluded}
        ):
            raise FuryChroniclePriorError(
                "Chronicle prior does not preserve its excluded-claims boundary"
            )

        model = document.get("model")
        if not isinstance(model, dict) or model.get("max_order") != 2:
            raise FuryChroniclePriorError("Chronicle prior must be an order-2 model")
        self.minimum_context_count = _positive_int(
            model.get("minimum_context_count"), "minimum_context_count"
        )
        self.alpha = _positive_number(
            model.get("additive_smoothing_alpha"), "additive_smoothing_alpha"
        )

        vocabulary = model.get("label_vocabulary")
        if (
            not isinstance(vocabulary, list)
            or not vocabulary
            or any(not isinstance(value, str) or not value for value in vocabulary)
            or len(set(vocabulary)) != len(vocabulary)
        ):
            raise FuryChroniclePriorError(
                "label_vocabulary must contain unique nonempty strings"
            )
        self.labels = tuple(vocabulary)
        label_set = frozenset(self.labels)
        self.global_counts = _counts(
            model.get("global_counts"), label_set, "global_counts"
        )

        mapping = document.get("action_mapping")
        if not isinstance(mapping, dict):
            raise FuryChroniclePriorError("Chronicle prior lacks action_mapping")
        cards = mapping.get("policy_action_key_to_cat2_card_id")
        if (
            mapping.get("mapping_complete") is not True
            or mapping.get("deployment_allowed") is not False
            or not isinstance(cards, dict)
        ):
            raise FuryChroniclePriorError(
                "Chronicle prior action mapping violates its analysis-only contract"
            )
        if set(cards) != set(self.labels) or any(
            not isinstance(cards[label], str) or not cards[label]
            for label in self.labels
        ):
            raise FuryChroniclePriorError(
                "Chronicle prior lacks a complete Cat2 card mapping"
            )
        self.card_ids = {label: cards[label] for label in self.labels}

        contexts = model.get("contexts")
        if not isinstance(contexts, dict):
            raise FuryChroniclePriorError("Chronicle prior lacks context tables")
        self.context_counts: dict[int, dict[tuple[str, ...], dict[str, int]]] = {}
        for order in (1, 2):
            rows = contexts.get(f"order_{order}")
            if not isinstance(rows, list):
                raise FuryChroniclePriorError(
                    f"Chronicle prior lacks order_{order} context rows"
                )
            table: dict[tuple[str, ...], dict[str, int]] = {}
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("context"), dict):
                    raise FuryChroniclePriorError(
                        f"order_{order} context row {row_index} is invalid"
                    )
                context = row["context"]
                bucket = context.get("last_auto_attack_elapsed_bucket")
                tokens = context.get("recent_action_tokens")
                if bucket not in LAST_AUTO_ATTACK_BUCKETS:
                    raise FuryChroniclePriorError(
                        f"order_{order} context row {row_index} has invalid swing bucket"
                    )
                if (
                    not isinstance(tokens, list)
                    or len(tokens) != order
                    or any(not isinstance(token, str) or not token for token in tokens)
                ):
                    raise FuryChroniclePriorError(
                        f"order_{order} context row {row_index} has invalid action tokens"
                    )
                key = (bucket, *tokens)
                if key in table:
                    raise FuryChroniclePriorError(
                        f"order_{order} contains a duplicate context"
                    )
                table[key] = _counts(
                    row.get("counts"),
                    label_set,
                    f"order_{order} context row {row_index} counts",
                )
            self.context_counts[order] = table

    def propose(
        self,
        recent_successful_action_tokens: Sequence[str],
        last_auto_attack_elapsed_bucket: str,
        *,
        top_k: int = 3,
    ) -> dict[str, Any]:
        """Return ranked action/card proposals for one partial-history context.

        Tokens use the training model's canonical form, for example
        ``id:1680:succeeded``.  Only the last two tokens are model features.
        """

        if isinstance(recent_successful_action_tokens, (str, bytes)):
            raise FuryChroniclePriorError(
                "recent_successful_action_tokens must be a sequence of tokens"
            )
        tokens = tuple(recent_successful_action_tokens)
        if any(
            not isinstance(token, str)
            or not token
            or not token.endswith(":succeeded")
            for token in tokens
        ):
            raise FuryChroniclePriorError(
                "recent successful action tokens must be nonempty and end in :succeeded"
            )
        effective_tokens = tokens[-2:]
        if last_auto_attack_elapsed_bucket not in LAST_AUTO_ATTACK_BUCKETS:
            raise FuryChroniclePriorError(
                "last_auto_attack_elapsed_bucket is not a model bucket"
            )
        top_k = _positive_int(top_k, "top_k")

        selected = self.global_counts
        selected_order = 0
        for order in (2, 1):
            if len(effective_tokens) < order:
                continue
            key = (
                last_auto_attack_elapsed_bucket,
                *effective_tokens[-order:],
            )
            candidate = self.context_counts[order].get(key)
            if candidate is not None and sum(candidate.values()) >= (
                self.minimum_context_count
            ):
                selected = candidate
                selected_order = order
                break

        context_support = sum(selected.values())
        denominator = context_support + self.alpha * len(self.labels)
        probabilities = {
            action_key: (selected.get(action_key, 0) + self.alpha) / denominator
            for action_key in self.labels
        }
        ranked = sorted(
            self.labels, key=lambda action_key: (-probabilities[action_key], action_key)
        )
        proposals = [
            {
                "rank": rank,
                "action_key": action_key,
                "cat2_card_id": self.card_ids[action_key],
                "support_count": selected.get(action_key, 0),
                "probability": probabilities[action_key],
            }
            for rank, action_key in enumerate(ranked[:top_k], start=1)
        ]
        matched_tokens = (
            list(effective_tokens[-selected_order:]) if selected_order else []
        )
        return {
            "schema_version": 1,
            "schema": PROPOSAL_SCHEMA,
            "kind": PROPOSAL_KIND,
            "prior_role": PRIOR_ROLE,
            "analysis_only": True,
            "deployment_allowed": False,
            "source_model": str(self.model_path),
            "requested_context": {
                "last_auto_attack_elapsed_bucket": (
                    last_auto_attack_elapsed_bucket
                ),
                "recent_successful_action_tokens": list(effective_tokens),
            },
            "matched_context": {
                "last_auto_attack_elapsed_bucket": (
                    last_auto_attack_elapsed_bucket if selected_order else None
                ),
                "recent_successful_action_tokens": matched_tokens,
            },
            "fallback_layer": "global" if not selected_order else f"order_{selected_order}",
            "fallback_order": selected_order,
            "context_support": context_support,
            "minimum_context_count": self.minimum_context_count,
            "additive_smoothing_alpha": self.alpha,
            "label_vocabulary_size": len(self.labels),
            "returned_probability_mass": sum(
                proposal["probability"] for proposal in proposals
            ),
            "proposals": proposals,
            "does_not_provide": [
                "action legality or when to act",
                "queue intent or client keypress timing",
                "full simulator state",
                "causal reward or Q values",
                "DPS improvement evidence",
            ],
        }


def propose_fury_actions(
    recent_successful_action_tokens: Sequence[str],
    last_auto_attack_elapsed_bucket: str,
    *,
    top_k: int = 3,
    model_path: str | Path = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Load the published model and return one bounded proposal response."""

    return FuryChroniclePrior(model_path).propose(
        recent_successful_action_tokens,
        last_auto_attack_elapsed_bucket,
        top_k=top_k,
    )
