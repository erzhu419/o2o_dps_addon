"""Version-isolated live-health extension for Fury full-policy rollouts.

The v2 executor is an immutable input to the already published v2 protocol.
This module reuses that audited function body without changing its source
bytes, while replacing only the target-context validator, resolver, and
receipt projection in a private function namespace.  The new
``SIMULATOR_HYPOTHESIS`` mode reads current target health from the bridge at
every decision, but the inferred initial health remains a named hypothesis and
therefore permanently blocks comparison eligibility by itself.

This overlay is intentionally diagnostic until a separately versioned runner
and executable dynamic-schedule admission bind it into a production closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v2 as _v2


JSONMap = dict[str, Any]
IMPLEMENTATION_REVISION = "v3.1_live_counterfactual_health_version_isolated"
EXPECTED_V2_SOURCE_SHA256 = (
    "57f691f44185b85b58f154ee91ff55f2fd23acd3d4f7753c29e2a03582b504f6"
)


class FuryFullPolicyRolloutV3Error(_v2.FuryFullPolicyRolloutV2Error):
    """The v3 overlay or its immutable v2 base violates its contract."""


class TargetSemanticsModeV3(str, Enum):
    """How target truth or an explicit simulator hypothesis is consumed."""

    DECLARED_EXACT = "DECLARED_EXACT"
    SENSITIVITY = "SENSITIVITY"
    SIMULATOR_HYPOTHESIS = "SIMULATOR_HYPOTHESIS"


@dataclass(frozen=True)
class TargetSemanticsContextV3:
    """Versioned target context with live counterfactual health support."""

    context_id: str
    mode: TargetSemanticsModeV3
    target_index: int
    target_classification: _v2.ContraTargetClassificationV2
    target_name: str
    equipped_item_names: tuple[str, ...]
    target_classification_evidence: _v2.ContraFieldEvidenceV2
    target_name_evidence: _v2.ContraFieldEvidenceV2
    equipment_evidence: _v2.ContraFieldEvidenceV2
    target_position_evidence: _v2.ContraFieldEvidenceV2
    target_health_pct_evidence: _v2.ContraFieldEvidenceV2
    target_max_health_evidence: _v2.ContraFieldEvidenceV2
    target_max_health: int | None = None
    health_pct_schedule: tuple[_v2.HealthPercentPointV2, ...] = ()

    def __post_init__(self) -> None:
        _v2._nonempty(self.context_id, "context_id")
        if not isinstance(self.mode, TargetSemanticsModeV3):
            raise TypeError("mode must be TargetSemanticsModeV3")
        _v2._nonnegative_int(self.target_index, "target_index")
        if not isinstance(
            self.target_classification, _v2.ContraTargetClassificationV2
        ):
            raise TypeError(
                "target_classification must be ContraTargetClassificationV2"
            )
        _v2._nonempty(self.target_name, "target_name")
        if not isinstance(self.equipped_item_names, tuple):
            raise TypeError("equipped_item_names must be a tuple")
        for index, name in enumerate(self.equipped_item_names):
            _v2._nonempty(name, f"equipped_item_names[{index}]")

        evidences = self._evidences()
        for label, evidence in evidences.items():
            if not isinstance(evidence, _v2.ContraFieldEvidenceV2):
                raise TypeError(f"{label} must be ContraFieldEvidenceV2")
            if evidence.kind is _v2.ContraEvidenceKindV2.MISSING:
                raise FuryFullPolicyRolloutV3Error(
                    f"{label} cannot be MISSING in an executable target context"
                )

        if self.mode is TargetSemanticsModeV3.DECLARED_EXACT:
            if self.target_max_health is not None:
                raise FuryFullPolicyRolloutV3Error(
                    "DECLARED_EXACT target_max_health must come from bridge state"
                )
            if self.health_pct_schedule:
                raise FuryFullPolicyRolloutV3Error(
                    "DECLARED_EXACT must not supply a health sensitivity schedule"
                )
            if any(evidence.sensitivity_only for evidence in evidences.values()):
                raise FuryFullPolicyRolloutV3Error(
                    "DECLARED_EXACT cannot carry sensitivity-only evidence"
                )
            for field in (
                self.target_health_pct_evidence,
                self.target_max_health_evidence,
            ):
                if field.kind is not _v2.ContraEvidenceKindV2.SIMULATOR_STATE:
                    raise FuryFullPolicyRolloutV3Error(
                        "DECLARED_EXACT health requires SIMULATOR_STATE evidence"
                    )
            return

        if (
            isinstance(self.target_max_health, bool)
            or not isinstance(self.target_max_health, int)
            or self.target_max_health <= 0
        ):
            raise FuryFullPolicyRolloutV3Error(
                f"{self.mode.value} target_max_health must be a positive integer"
            )
        for field in (
            self.target_health_pct_evidence,
            self.target_max_health_evidence,
        ):
            if field.kind is not _v2.ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
                raise FuryFullPolicyRolloutV3Error(
                    f"{self.mode.value} health requires SENSITIVITY_HYPOTHESIS evidence"
                )

        if self.mode is TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS:
            if self.health_pct_schedule:
                raise FuryFullPolicyRolloutV3Error(
                    "SIMULATOR_HYPOTHESIS reads live health and cannot carry a health schedule"
                )
            return

        if not self.health_pct_schedule:
            raise FuryFullPolicyRolloutV3Error(
                "SENSITIVITY requires a health_pct_schedule"
            )
        prior = -1
        for point in self.health_pct_schedule:
            if not isinstance(point, _v2.HealthPercentPointV2):
                raise TypeError(
                    "health_pct_schedule entries must be HealthPercentPointV2"
                )
            if point.time_ms <= prior:
                raise FuryFullPolicyRolloutV3Error(
                    "health_pct_schedule times must be strictly increasing"
                )
            prior = point.time_ms

    def _evidences(self) -> dict[str, _v2.ContraFieldEvidenceV2]:
        return {
            "target_classification_evidence": self.target_classification_evidence,
            "target_name_evidence": self.target_name_evidence,
            "equipment_evidence": self.equipment_evidence,
            "target_position_evidence": self.target_position_evidence,
            "target_health_pct_evidence": self.target_health_pct_evidence,
            "target_max_health_evidence": self.target_max_health_evidence,
        }


def validate_v2_execution_base_identity_v3() -> str:
    """Verify that the overlay still wraps the published v2 source bytes."""

    path = Path(_v2.__file__).resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise FuryFullPolicyRolloutV3Error(
            "the Fury v2 execution base must be a regular non-symlink file"
        )
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FuryFullPolicyRolloutV3Error(
            "the Fury v2 execution base changed while it was read"
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_V2_SOURCE_SHA256:
        raise FuryFullPolicyRolloutV3Error(
            "the Fury v2 execution base differs from the published protocol"
        )
    return digest


def _validate_context_map_v3(
    contexts: Mapping[int, TargetSemanticsContextV3] | None,
) -> dict[int, TargetSemanticsContextV3]:
    if contexts is None:
        return {}
    if not isinstance(contexts, Mapping):
        raise TypeError("target_contexts must be a mapping or None")
    result: dict[int, TargetSemanticsContextV3] = {}
    for key, context in contexts.items():
        _v2._nonnegative_int(key, "target_contexts key")
        if not isinstance(context, TargetSemanticsContextV3):
            raise TypeError("target_contexts values must be TargetSemanticsContextV3")
        if key != context.target_index:
            raise FuryFullPolicyRolloutV3Error(
                "target_contexts key must equal context.target_index"
            )
        result[key] = context
    return result


def _resolve_target_semantics_v3(
    state: Mapping[str, Any],
    request: Mapping[str, Any],
    contexts: Mapping[int, TargetSemanticsContextV3],
) -> JSONMap:
    raw_index = state.get("target_index", 0)
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
        raise _v2._TargetResolutionBlocked(
            "TARGET_INDEX_UNKNOWN_OR_INVALID", "bridge target_index is invalid"
        )
    request_targets = _v2._request_targets(request)
    if raw_index >= len(request_targets):
        raise _v2._TargetResolutionBlocked(
            "TARGET_INDEX_OUT_OF_REQUEST_RANGE",
            f"bridge target index {raw_index} is outside the request target array",
        )
    raw_num_targets = state.get("num_targets")
    dynamic_state = state.get("dynamic_team_background")
    if dynamic_state is None:
        if raw_num_targets is not None and (
            isinstance(raw_num_targets, bool)
            or not isinstance(raw_num_targets, int)
            or raw_num_targets != len(request_targets)
        ):
            raise _v2._TargetResolutionBlocked(
                "BRIDGE_REQUEST_TARGET_COUNT_MISMATCH",
                "bridge num_targets does not match RaidSimRequest encounter.targets",
            )
    else:
        if not isinstance(dynamic_state, Mapping):
            raise _v2._TargetResolutionBlocked(
                "DYNAMIC_TARGET_STATE_INVALID",
                "bridge dynamic_team_background must be an object",
            )
        total_target_count = state.get("total_target_count")
        if (
            isinstance(total_target_count, bool)
            or not isinstance(total_target_count, int)
            or total_target_count != len(request_targets)
        ):
            raise _v2._TargetResolutionBlocked(
                "BRIDGE_REQUEST_TARGET_COUNT_MISMATCH",
                "bridge total_target_count does not match RaidSimRequest encounter.targets",
            )
        dynamic_targets = dynamic_state.get("targets")
        if not isinstance(dynamic_targets, list) or len(dynamic_targets) != total_target_count:
            raise _v2._TargetResolutionBlocked(
                "DYNAMIC_TARGET_STATE_INVALID",
                "bridge dynamic target rows do not match total_target_count",
            )
        live_target_count = 0
        for index, target in enumerate(dynamic_targets):
            if (
                not isinstance(target, Mapping)
                or target.get("target_index") != index
                or not isinstance(target.get("dead"), bool)
            ):
                raise _v2._TargetResolutionBlocked(
                    "DYNAMIC_TARGET_STATE_INVALID",
                    "bridge dynamic target rows are not indexed typed lifecycle rows",
                )
            if target["dead"] is False:
                live_target_count += 1
        if (
            isinstance(raw_num_targets, bool)
            or not isinstance(raw_num_targets, int)
            or raw_num_targets != live_target_count
        ):
            raise _v2._TargetResolutionBlocked(
                "DYNAMIC_LIVE_TARGET_COUNT_MISMATCH",
                "bridge num_targets does not match dynamic live target rows",
            )

    context = contexts.get(raw_index)
    if context is None:
        raise _v2._TargetResolutionBlocked(
            "TARGET_CONTEXT_FOR_INDEX_MISSING",
            f"no explicit target context exists for target index {raw_index}",
        )
    request_name = request_targets[raw_index].get("name")
    if isinstance(request_name, str) and request_name and request_name != context.target_name:
        raise _v2._TargetResolutionBlocked(
            "TARGET_CONTEXT_REQUEST_NAME_MISMATCH",
            f"context name {context.target_name!r} does not match request target {request_name!r}",
        )

    if context.mode in {
        TargetSemanticsModeV3.DECLARED_EXACT,
        TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
    }:
        if state.get("target_health_known") is not True:
            raise _v2._TargetResolutionBlocked(
                "EXACT_TARGET_HEALTH_NOT_EXPOSED",
                f"{context.mode.value} requires target_health_known=true from bridge state",
            )
        health_pct = _v2._bounded_percent(
            state.get("target_health_percent"), "bridge target_health_percent"
        )
        max_number = _v2._finite_number(
            state.get("target_health_max"), "bridge target_health_max"
        )
        if max_number <= 0 or not max_number.is_integer():
            raise _v2._TargetResolutionBlocked(
                "EXACT_TARGET_MAX_HEALTH_INVALID",
                "bridge target_health_max must be a positive integral value",
            )
        max_health = int(max_number)
        if (
            context.mode is TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS
            and max_health != context.target_max_health
        ):
            raise _v2._TargetResolutionBlocked(
                "SIMULATOR_HYPOTHESIS_MAX_HEALTH_MISMATCH",
                "bridge target_health_max differs from the named target-health hypothesis",
            )
    else:
        health_pct = _v2._interpolate_health(
            context.health_pct_schedule, int(state["time_ms"])
        )
        assert context.target_max_health is not None
        max_health = context.target_max_health

    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": raw_index,
        "target_health_pct": health_pct,
        "target_max_health": max_health,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "target_distance_yards": _v2._player_distance(request),
        "equipped_item_names": list(context.equipped_item_names),
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence,
            "target_max_health": context.target_max_health_evidence,
            "target_classification": context.target_classification_evidence,
            "target_name": context.target_name_evidence,
            "equipped_item_names": context.equipment_evidence,
            "target_position": context.target_position_evidence,
        },
        "exact_by_declared_contract": (
            context.mode is TargetSemanticsModeV3.DECLARED_EXACT
        ),
    }


def target_semantics_context_receipt_v3(
    context: TargetSemanticsContextV3,
) -> JSONMap:
    if not isinstance(context, TargetSemanticsContextV3):
        raise TypeError("context must be TargetSemanticsContextV3")
    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": context.target_index,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "equipped_item_count": len(context.equipped_item_names),
        "target_max_health": context.target_max_health,
        "health_pct_schedule": [
            {"time_ms": point.time_ms, "health_pct": point.health_pct}
            for point in context.health_pct_schedule
        ],
        "exact_by_declared_contract": (
            context.mode is TargetSemanticsModeV3.DECLARED_EXACT
        ),
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        },
    }


def _clone_v2_executor() -> Any:
    namespace = dict(_v2.run_fury_full_policy_rollout_v2.__globals__)
    namespace.update(
        {
            "TargetSemanticsModeV2": TargetSemanticsModeV3,
            "_validate_context_map": _validate_context_map_v3,
            "_resolve_target_semantics": _resolve_target_semantics_v3,
            "_context_receipt": target_semantics_context_receipt_v3,
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v3_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V3_CORE = _clone_v2_executor()


def run_fury_full_policy_rollout_v3(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: Any,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3] | None,
    dynamic_load: _v2.DynamicRolloutLoadV1 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Run v2 mechanics with the isolated v3 target resolver."""

    validate_v2_execution_base_identity_v3()
    result = _RUN_V3_CORE(
        bridge,
        raid_sim_request,
        adapter,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=retain_steps,
    )
    contexts = _validate_context_map_v3(target_contexts)
    if any(
        context.mode is TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS
        for context in contexts.values()
    ):
        blockers = result.get("blockers")
        if not isinstance(blockers, list):
            raise FuryFullPolicyRolloutV3Error("v2 result blockers are malformed")
        code = "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL"
        if not any(isinstance(row, Mapping) and row.get("code") == code for row in blockers):
            blockers.append(
                _v2._blocker(
                    code,
                    "live target health is simulated from a named initial-HP hypothesis, not exact historical UnitHealthMax",
                    execution_fatal=False,
                )
            )
        result["blocker_summary"] = _v2._blocker_summary(blockers)
        if result.get("status") == "COMPLETE_FAITHFUL":
            result["status"] = "COMPLETE_NONFAITHFUL"
        if "ordered_projection_faithful" in result:
            result["ordered_projection_faithful"] = False
        if "simulator_dps_comparison_eligible" in result:
            result["simulator_dps_comparison_eligible"] = False
    return result


# Stable v2 wire types intentionally remain the transport types for v3.  They
# are aliases, not copied implementations, and the base source hash is checked
# before every execution.
ContraEvidenceKindV2 = _v2.ContraEvidenceKindV2
ContraFieldEvidenceV2 = _v2.ContraFieldEvidenceV2
ContraTargetClassificationV2 = _v2.ContraTargetClassificationV2
DynamicRolloutLoadV1 = _v2.DynamicRolloutLoadV1
HealthPercentPointV2 = _v2.HealthPercentPointV2
dynamic_rollout_load_from_config_wire_v1 = (
    _v2.dynamic_rollout_load_from_config_wire_v1
)


__all__ = (
    "ContraEvidenceKindV2",
    "ContraFieldEvidenceV2",
    "ContraTargetClassificationV2",
    "DynamicRolloutLoadV1",
    "EXPECTED_V2_SOURCE_SHA256",
    "FuryFullPolicyRolloutV3Error",
    "HealthPercentPointV2",
    "IMPLEMENTATION_REVISION",
    "TargetSemanticsContextV3",
    "TargetSemanticsModeV3",
    "dynamic_rollout_load_from_config_wire_v1",
    "run_fury_full_policy_rollout_v3",
    "target_semantics_context_receipt_v3",
    "validate_v2_execution_base_identity_v3",
)
