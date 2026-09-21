"""Rank a complete legal simulator action set with offline Fury evidence.

This module is deliberately a *guide* for sequence search.  It projects either
the weighted Chronicle behavior-clone V2 mark head or the older partial
Markov prior onto exact ``ActionRef`` rows reported legal by the simulator.
Every legal row remains in the returned order, including actions that have no
offline support.  Consequently the guide changes proposal order, never the
search action universe.

Chronicle labels are successful server-observed action proxies.  They do not
recover the player's client-side next-swing queue request, target-switch
intent, failed keypresses, or a deployable historical policy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import historical_behavior_clone_simulator_adapter_v2 as clone_adapter_v2
from . import historical_behavior_clone_v2 as clone_v2
from .fury_chronicle_prior import (
    DEFAULT_MODEL as DEFAULT_CHRONICLE_PRIOR_MODEL,
    MODEL_KIND as CHRONICLE_PRIOR_MODEL_KIND,
    FuryChroniclePrior,
)
from .historical_behavior_clone_full_rollout_v2 import (
    load_behavior_clone_model_v2,
)
from .sim_bridge import ActionRef, AvailableAction


JSONMap = dict[str, Any]
RESULT_SCHEMA = "offline_action_sequence_guide/v1"
PROPOSAL_SCHEMA = "offline_action_sequence_proposal/v1"
BUILD_MATCH_RECEIPT_SCHEMA = "offline_action_sequence_build_match_receipt/v1"
GUIDE_ROLE = "OFFLINE_EXPERT_SEQUENCE_SEARCH_ORDER_ONLY"

SOURCE_BUILD_EXACT = "EXACT_SINGLE_BUILD"
SOURCE_BUILD_POOLED = "POOLED_OR_CROSS_BUILD"
SOURCE_BUILD_UNKNOWN = "UNKNOWN_OR_NOT_RECORDED"
SOURCE_BUILD_SCOPES = frozenset(
    {SOURCE_BUILD_EXACT, SOURCE_BUILD_POOLED, SOURCE_BUILD_UNKNOWN}
)

BACKEND_CLONE_V2 = "HISTORICAL_BEHAVIOR_CLONE_V2"
BACKEND_CHRONICLE_PRIOR = "FURY_CHRONICLE_PRIOR"


class OfflineActionSequenceGuideV1Error(ValueError):
    """An offline artifact, build identity, context, or action row is invalid."""


def _canonical_object(value: Mapping[str, Any], label: str) -> str:
    if not isinstance(value, Mapping):
        raise OfflineActionSequenceGuideV1Error(f"{label} must be an object")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise OfflineActionSequenceGuideV1Error(
            f"{label} must be strict JSON: {error}"
        ) from error


@dataclass(frozen=True, init=False)
class ExactBuildIdentityV1:
    """Exact structural identity used only for matched-build adjudication."""

    canonical_json: str

    def __init__(self, identity: Mapping[str, Any]) -> None:
        object.__setattr__(
            self,
            "canonical_json",
            _canonical_object(identity, "exact build identity"),
        )

    def to_dict(self) -> JSONMap:
        value = json.loads(self.canonical_json)
        assert isinstance(value, dict)
        return value


def _build_identity(
    value: ExactBuildIdentityV1 | Mapping[str, Any] | None,
) -> ExactBuildIdentityV1 | None:
    if value is None or isinstance(value, ExactBuildIdentityV1):
        return value
    return ExactBuildIdentityV1(value)


@dataclass(frozen=True)
class BuildMatchReceiptV1:
    source_build_scope: str
    evaluation_build_identity: ExactBuildIdentityV1 | None
    source_build_identity: ExactBuildIdentityV1 | None
    same_build_comparison_eligible: bool
    reason: str

    def to_dict(self) -> JSONMap:
        return {
            "schema": BUILD_MATCH_RECEIPT_SCHEMA,
            "source_build_scope": self.source_build_scope,
            "evaluation_build_identity": (
                None
                if self.evaluation_build_identity is None
                else self.evaluation_build_identity.to_dict()
            ),
            "source_build_identity": (
                None
                if self.source_build_identity is None
                else self.source_build_identity.to_dict()
            ),
            "identity_match_rule": "EXACT_CANONICAL_JSON_STRUCTURAL_EQUALITY",
            "same_build_comparison_eligible": (
                self.same_build_comparison_eligible
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OfflineGuideContextV1:
    """Observable inputs for one sequence-search proposal epoch."""

    observable_context: Mapping[str, Any] = field(default_factory=dict)
    recent_successful_action_history: tuple[str, ...] = ()
    last_auto_attack_elapsed_bucket: str = "MISSING"
    evaluation_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observable_context, Mapping):
            raise OfflineActionSequenceGuideV1Error(
                "observable_context must be an object"
            )
        history = self.recent_successful_action_history
        if isinstance(history, (str, bytes)) or any(
            not isinstance(value, str) or not value.strip() for value in history
        ):
            raise OfflineActionSequenceGuideV1Error(
                "recent_successful_action_history must contain nonempty strings"
            )
        if (
            not isinstance(self.last_auto_attack_elapsed_bucket, str)
            or not self.last_auto_attack_elapsed_bucket
        ):
            raise OfflineActionSequenceGuideV1Error(
                "last_auto_attack_elapsed_bucket must be nonempty text"
            )
        object.__setattr__(self, "observable_context", deepcopy(dict(self.observable_context)))
        object.__setattr__(
            self,
            "recent_successful_action_history",
            tuple(value.strip() for value in history),
        )
        object.__setattr__(
            self,
            "evaluation_build_identity",
            _build_identity(self.evaluation_build_identity),
        )


@dataclass(frozen=True)
class OfflineActionProposalV1:
    rank: int
    available_action_index: int
    available_action_label: str
    action_ref: ActionRef
    action_key: str | None
    probability: float
    guide_supported: bool
    model: Mapping[str, Any]
    prototype: Mapping[str, Any]
    build_match_receipt: BuildMatchReceiptV1

    def to_dict(self) -> JSONMap:
        return {
            "schema": PROPOSAL_SCHEMA,
            "rank": self.rank,
            "available_action_index": self.available_action_index,
            "available_action_label": self.available_action_label,
            "action_ref": self.action_ref.to_wire(),
            "action_key": self.action_key,
            "probability": self.probability,
            "guide_supported": self.guide_supported,
            "provenance": {
                "action_key": self.action_key,
                "probability": self.probability,
                "model": deepcopy(dict(self.model)),
                "prototype": deepcopy(dict(self.prototype)),
                "build_match": self.build_match_receipt.to_dict(),
            },
        }


@dataclass(frozen=True)
class OfflineActionGuideResultV1:
    proposals: tuple[OfflineActionProposalV1, ...]
    model: Mapping[str, Any]
    prototype: Mapping[str, Any]
    build_match_receipt: BuildMatchReceiptV1
    prediction_context: Mapping[str, Any]

    @property
    def ordered_action_refs(self) -> tuple[ActionRef, ...]:
        return tuple(proposal.action_ref for proposal in self.proposals)

    def to_dict(self) -> JSONMap:
        return {
            "schema": RESULT_SCHEMA,
            "guide_role": GUIDE_ROLE,
            "model": deepcopy(dict(self.model)),
            "prototype": deepcopy(dict(self.prototype)),
            "prediction_context": deepcopy(dict(self.prediction_context)),
            "build_match_receipt": self.build_match_receipt.to_dict(),
            "legal_search_action_count": len(self.proposals),
            "ranked_proposal_count": len(self.proposals),
            "ordered_legal_action_refs": [
                proposal.action_ref.to_wire() for proposal in self.proposals
            ],
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "contract": {
                "input_legality_source": "SIMULATOR_AVAILABLE_ACTIONS",
                "guide_only_reorders_legal_actions": True,
                "guide_filters_search_action_universe": False,
                "unsupported_legal_actions_retained_with_zero_probability": True,
                "true_historical_client_policy_reconstructed": False,
                "client_next_swing_queue_intent_reconstructed": False,
                "target_switch_intent_reconstructed": False,
                "offline_probability_is_reward_or_q_value": False,
            },
        }


_ACTION_KEY_BY_EXACT_REF: Mapping[ActionRef, str] = {
    binding.action_ref: action_key
    for action_key, binding in clone_adapter_v2.ACTION_SINK_BINDINGS_V2.items()
}


def offline_action_key_for_ref_v1(action: ActionRef) -> str | None:
    """Map one exact executed action onto the Chronicle guide vocabulary."""

    if not isinstance(action, ActionRef):
        raise TypeError("action must be ActionRef")
    return _ACTION_KEY_BY_EXACT_REF.get(action)


def _source_build_receipt(
    *,
    source_build_scope: str,
    source_build_identity: ExactBuildIdentityV1 | None,
    evaluation_build_identity: ExactBuildIdentityV1 | None,
) -> BuildMatchReceiptV1:
    if source_build_scope == SOURCE_BUILD_POOLED:
        eligible = False
        reason = "SOURCE_IS_POOLED_OR_CROSS_BUILD_GUIDE"
    elif source_build_scope == SOURCE_BUILD_UNKNOWN:
        eligible = False
        reason = "SOURCE_BUILD_SCOPE_UNKNOWN"
    elif source_build_identity is None:
        eligible = False
        reason = "SOURCE_BUILD_IDENTITY_MISSING"
    elif evaluation_build_identity is None:
        eligible = False
        reason = "EVALUATION_BUILD_IDENTITY_MISSING"
    elif source_build_identity != evaluation_build_identity:
        eligible = False
        reason = "EXACT_BUILD_IDENTITY_MISMATCH"
    else:
        eligible = True
        reason = "EXACT_SOURCE_AND_EVALUATION_BUILD_IDENTITIES_MATCH"
    return BuildMatchReceiptV1(
        source_build_scope=source_build_scope,
        evaluation_build_identity=evaluation_build_identity,
        source_build_identity=source_build_identity,
        same_build_comparison_eligible=eligible,
        reason=reason,
    )


def _successful_tokens(history: Sequence[str]) -> tuple[str, ...]:
    tokens: list[str] = []
    for value in history:
        if value.endswith(":succeeded"):
            tokens.append(value)
            continue
        binding = clone_adapter_v2.ACTION_SINK_BINDINGS_V2.get(value)
        if binding is None or not binding.action_ref.spell_id:
            raise OfflineActionSequenceGuideV1Error(
                f"cannot encode successful action history value {value!r}"
            )
        tokens.append(f"id:{binding.action_ref.spell_id}:succeeded")
    return tuple(tokens)


class OfflineActionSequenceGuideV1:
    """Offline probability backend projected onto a complete legal action set."""

    def __init__(
        self,
        *,
        backend: str,
        model_path: Path,
        model: Mapping[str, Any] | FuryChroniclePrior,
        model_binding: Mapping[str, Any],
        source_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None,
        source_build_scope: str,
    ) -> None:
        if source_build_scope not in SOURCE_BUILD_SCOPES:
            raise OfflineActionSequenceGuideV1Error(
                f"unsupported source_build_scope {source_build_scope!r}"
            )
        self.backend = backend
        self.model_path = model_path
        self._model = model
        self._model_binding = deepcopy(dict(model_binding))
        self.source_build_identity = _build_identity(source_build_identity)
        self.source_build_scope = source_build_scope

    @classmethod
    def from_behavior_clone_v2(
        cls,
        model_path: str | Path,
        *,
        source_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None = None,
        source_build_scope: str = SOURCE_BUILD_POOLED,
    ) -> "OfflineActionSequenceGuideV1":
        path = Path(model_path).expanduser().resolve()
        model, binding = load_behavior_clone_model_v2(path)
        return cls(
            backend=BACKEND_CLONE_V2,
            model_path=path,
            model=model,
            model_binding={
                "backend": BACKEND_CLONE_V2,
                "kind": clone_v2.MODEL_SCHEMA,
                "model_id": binding["policy_id"],
                "model_path": str(path),
                "model_content_sha256": binding["model_sha256"],
                "prototype_id": binding["prototype_id"],
                "prototype_family": binding["prototype_family"],
                "expected_model_binding": binding[
                    "external_exact_validation_receipt"
                ],
            },
            source_build_identity=source_build_identity,
            source_build_scope=source_build_scope,
        )

    @classmethod
    def from_chronicle_prior(
        cls,
        model_path: str | Path = DEFAULT_CHRONICLE_PRIOR_MODEL,
        *,
        source_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None = None,
        source_build_scope: str = SOURCE_BUILD_POOLED,
    ) -> "OfflineActionSequenceGuideV1":
        path = Path(model_path).expanduser().resolve()
        prior = FuryChroniclePrior(path)
        return cls(
            backend=BACKEND_CHRONICLE_PRIOR,
            model_path=path,
            model=prior,
            model_binding={
                "backend": BACKEND_CHRONICLE_PRIOR,
                "kind": CHRONICLE_PRIOR_MODEL_KIND,
                "model_id": "chronicle.fury.partial_markov_v1",
                "model_path": str(path),
                "prototype_id": None,
                "prototype_family": "POOLED_PARTIAL_SERVER_OBSERVATIONS",
            },
            source_build_identity=source_build_identity,
            source_build_scope=source_build_scope,
        )

    @classmethod
    def from_artifact(
        cls,
        artifact_path: str | Path,
        *,
        source_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None = None,
        source_build_scope: str = SOURCE_BUILD_POOLED,
    ) -> "OfflineActionSequenceGuideV1":
        path = Path(artifact_path).expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise OfflineActionSequenceGuideV1Error(
                f"cannot read offline guide artifact: {error}"
            ) from error
        if not isinstance(document, Mapping):
            raise OfflineActionSequenceGuideV1Error(
                "offline guide artifact must be an object"
            )
        if document.get("schema") == clone_v2.MODEL_SCHEMA:
            return cls.from_behavior_clone_v2(
                path,
                source_build_identity=source_build_identity,
                source_build_scope=source_build_scope,
            )
        if document.get("kind") == CHRONICLE_PRIOR_MODEL_KIND:
            return cls.from_chronicle_prior(
                path,
                source_build_identity=source_build_identity,
                source_build_scope=source_build_scope,
            )
        raise OfflineActionSequenceGuideV1Error(
            "artifact is neither historical_behavior_clone/v2 nor FuryChroniclePrior"
        )

    def _probabilities(
        self,
        context: OfflineGuideContextV1,
        legal_action_keys: tuple[str, ...],
    ) -> tuple[dict[str, float], JSONMap]:
        if self.backend == BACKEND_CLONE_V2:
            assert isinstance(self._model, Mapping)
            prediction = clone_adapter_v2.predict_mark_distribution_v2(
                self._model,
                deepcopy(dict(context.observable_context)),
                legal_action_keys,
                expected_model_binding=self._model_binding[
                    "expected_model_binding"
                ],
            )
            probabilities = {
                str(key): float(value)
                for key, value in prediction["probabilities"].items()
            }
            prediction_context = {
                "backend": BACKEND_CLONE_V2,
                "matched_context_families": prediction[
                    "matched_context_families"
                ],
                "context_combination": prediction["context_combination"],
                "legal_mask_applied_before_sampling": prediction[
                    "legal_mask_applied_before_sampling"
                ],
            }
            return probabilities, prediction_context

        assert isinstance(self._model, FuryChroniclePrior)
        prediction = self._model.propose(
            _successful_tokens(context.recent_successful_action_history),
            context.last_auto_attack_elapsed_bucket,
            top_k=len(self._model.labels),
        )
        raw = {
            str(row["action_key"]): float(row["probability"])
            for row in prediction["proposals"]
        }
        legal_mass = sum(raw.get(key, 0.0) for key in legal_action_keys)
        probabilities = (
            {}
            if legal_mass <= 0
            else {
                key: raw[key] / legal_mass
                for key in legal_action_keys
                if raw.get(key, 0.0) > 0
            }
        )
        prediction_context = {
            "backend": BACKEND_CHRONICLE_PRIOR,
            "fallback_layer": prediction["fallback_layer"],
            "fallback_order": prediction["fallback_order"],
            "context_support": prediction["context_support"],
            "requested_context": prediction["requested_context"],
            "probabilities_conditioned_on_current_legal_known_actions": True,
        }
        return probabilities, prediction_context

    def rank_available_actions(
        self,
        context: OfflineGuideContextV1,
        available_actions: Iterable[AvailableAction],
    ) -> OfflineActionGuideResultV1:
        """Return every legal exact action, ordered by offline probability."""

        if not isinstance(context, OfflineGuideContextV1):
            raise OfflineActionSequenceGuideV1Error(
                "context must be OfflineGuideContextV1"
            )
        rows = tuple(available_actions)
        if any(not isinstance(row, AvailableAction) for row in rows):
            raise OfflineActionSequenceGuideV1Error(
                "available_actions must contain AvailableAction rows"
            )
        legal_rows = tuple(row for row in rows if row.legal)
        keyed_rows = tuple(
            (row, _ACTION_KEY_BY_EXACT_REF.get(row.action)) for row in legal_rows
        )
        legal_action_keys = tuple(
            dict.fromkeys(key for _, key in keyed_rows if key is not None)
        )
        probabilities, prediction_context = self._probabilities(
            context, legal_action_keys
        )
        if any(
            not math.isfinite(value) or value < 0 or value > 1.0 + 1e-12
            for value in probabilities.values()
        ):
            raise OfflineActionSequenceGuideV1Error(
                "offline backend returned an invalid probability"
            )

        receipt = _source_build_receipt(
            source_build_scope=self.source_build_scope,
            source_build_identity=self.source_build_identity,
            evaluation_build_identity=context.evaluation_build_identity,
        )
        model = {
            key: deepcopy(value)
            for key, value in self._model_binding.items()
            if key != "expected_model_binding"
            and key not in {"prototype_id", "prototype_family"}
        }
        prototype = {
            "prototype_id": self._model_binding.get("prototype_id"),
            "prototype_family": self._model_binding.get("prototype_family"),
        }
        ordered = sorted(
            keyed_rows,
            key=lambda pair: (
                -probabilities.get(pair[1] or "", 0.0),
                pair[1] is None,
                pair[1] or "",
                pair[0].index,
            ),
        )
        proposals = tuple(
            OfflineActionProposalV1(
                rank=rank,
                available_action_index=row.index,
                available_action_label=row.label,
                action_ref=row.action,
                action_key=action_key,
                probability=probabilities.get(action_key or "", 0.0),
                guide_supported=(
                    action_key is not None
                    and probabilities.get(action_key, 0.0) > 0
                ),
                model=model,
                prototype=prototype,
                build_match_receipt=receipt,
            )
            for rank, (row, action_key) in enumerate(ordered, start=1)
        )
        return OfflineActionGuideResultV1(
            proposals=proposals,
            model=model,
            prototype=prototype,
            build_match_receipt=receipt,
            prediction_context=prediction_context,
        )


def load_offline_action_sequence_guide_v1(
    artifact_path: str | Path = DEFAULT_CHRONICLE_PRIOR_MODEL,
    *,
    source_build_identity: ExactBuildIdentityV1 | Mapping[str, Any] | None = None,
    source_build_scope: str = SOURCE_BUILD_POOLED,
) -> OfflineActionSequenceGuideV1:
    """Load either supported offline model by inspecting its declared kind."""

    return OfflineActionSequenceGuideV1.from_artifact(
        artifact_path,
        source_build_identity=source_build_identity,
        source_build_scope=source_build_scope,
    )


__all__ = (
    "BACKEND_CHRONICLE_PRIOR",
    "BACKEND_CLONE_V2",
    "BUILD_MATCH_RECEIPT_SCHEMA",
    "BuildMatchReceiptV1",
    "ExactBuildIdentityV1",
    "GUIDE_ROLE",
    "OfflineActionGuideResultV1",
    "OfflineActionProposalV1",
    "OfflineActionSequenceGuideV1",
    "OfflineActionSequenceGuideV1Error",
    "OfflineGuideContextV1",
    "RESULT_SCHEMA",
    "SOURCE_BUILD_EXACT",
    "SOURCE_BUILD_POOLED",
    "SOURCE_BUILD_UNKNOWN",
    "load_offline_action_sequence_guide_v1",
    "offline_action_key_for_ref_v1",
)
