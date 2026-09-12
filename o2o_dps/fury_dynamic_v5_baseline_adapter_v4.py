"""Project-specific baseline adapter for the native dynamic-v5 runner.

Only the deployed Contra lane currently has a complete source-adapter ->
ordered-sink -> generic Fury-v5 rollout path.  Cat's richer audited v5 path is
still bound to ``load_dynamic_v2``.  Contra260817 has a source oracle but lacks
the target/item/equipment ordered sinks and a full-policy rollout.  Those two
lanes therefore fail before the bridge is mutated instead of being replaced by
the older generic Cat adapter or deployed Contra.
"""

from __future__ import annotations

from typing import Any, Mapping

from .fury_contra_adapter_v2 import (
    ContraDeployedFuryAdapterV2,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from .fury_full_policy_rollout_v2 import HealthPercentPointV2
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
)
from .fury_full_policy_rollout_v5 import run_fury_full_policy_rollout_v5
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    FuryPairedRunnerV4Error,
    bind_dynamic_v5_load,
    build_fury_v5_lane_result_v4,
)


JSONMap = dict[str, Any]

CAT_BLOCKERS = ("CAT_V5_DYNAMIC_V3_FULL_POLICY_ADAPTER_MISSING",)
CONTRA260817_BLOCKERS = (
    "CONTRA260817_DYNAMIC_V3_FULL_POLICY_EXECUTOR_MISSING",
    "CONTRA260817_TARGET_ITEM_EQUIPMENT_ORDERED_SINK_MISSING",
)


class FuryDynamicV5BaselineAdapterV4Error(FuryPairedRunnerV4Error):
    """A baseline cannot be represented by its audited dynamic-v5 path."""

    def __init__(self, blocker_codes: tuple[str, ...], detail: str) -> None:
        self.blocker_codes = blocker_codes
        super().__init__(f"{','.join(blocker_codes)}: {detail}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",), f"{label} must be an object"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
            f"{label} must be nonempty text",
        )
    return value


def _evidence(value: Any, label: str) -> ContraFieldEvidenceV2:
    row = _mapping(value, label)
    if set(row) != {
        "schema",
        "kind",
        "source_sha256",
        "corpus_sha256",
        "hypothesis_id",
    } or row.get("schema") != "contra_field_evidence/v2":
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXT_EVIDENCE_MALFORMED",),
            f"{label} field set or schema mismatch",
        )
    try:
        return ContraFieldEvidenceV2(
            kind=ContraEvidenceKindV2(row.get("kind")),
            source_sha256=row.get("source_sha256"),
            corpus_sha256=row.get("corpus_sha256"),
            hypothesis_id=row.get("hypothesis_id"),
        )
    except (TypeError, ValueError) as error:
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXT_EVIDENCE_MALFORMED",),
            f"{label}: {error}",
        ) from error


def target_contexts_from_runner_v4(
    target_context_bundle: Mapping[str, Any],
) -> dict[int, TargetSemanticsContextV3]:
    """Reconstruct policy inputs, including item names lost by runner-v2."""

    bundle = _mapping(target_context_bundle, "target_context_bundle")
    contexts = bundle.get("contexts")
    if not isinstance(contexts, list):
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXTS_MISSING",),
            "target_context_bundle.contexts must be a list",
        )
    expected_count = bundle.get("target_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
            "target_context_bundle.target_count must be an integer",
        )
    if len(contexts) != expected_count:
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXTS_INCOMPLETE",),
            "target contexts do not cover every dynamic target",
        )
    result: dict[int, TargetSemanticsContextV3] = {}
    for ordinal, raw in enumerate(contexts):
        row = _mapping(raw, f"target context {ordinal}")
        expected_fields = {
            "context_id",
            "mode",
            "target_index",
            "target_classification",
            "target_name",
            "equipped_item_names",
            "target_max_health",
            "health_pct_schedule",
            "field_evidence",
        }
        if set(row) != expected_fields:
            missing = sorted(expected_fields.difference(row))
            if "equipped_item_names" in missing and "equipped_item_count" in row:
                raise FuryDynamicV5BaselineAdapterV4Error(
                    ("RUNNER_V2_EQUIPPED_ITEM_NAMES_NOT_RECOVERABLE",),
                    "an item count cannot reconstruct Contra.HasEquipItem branches",
                )
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
                f"target context {ordinal} field set mismatch; missing={missing}",
            )
        names = row.get("equipped_item_names")
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not name.strip() for name in names
        ):
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_EQUIPPED_ITEM_NAMES_MISSING",),
                "complete equipped_item_names are required",
            )
        schedule = row.get("health_pct_schedule")
        if not isinstance(schedule, list):
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
                "health_pct_schedule must be a list",
            )
        evidence = _mapping(row.get("field_evidence"), "field_evidence")
        expected_evidence = {
            "target_health_pct",
            "target_max_health",
            "target_classification",
            "target_name",
            "equipped_item_names",
            "target_position",
        }
        if set(evidence) != expected_evidence:
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_TARGET_CONTEXT_EVIDENCE_MALFORMED",),
                "field_evidence does not cover every policy input",
            )
        try:
            target_index = row.get("target_index")
            if isinstance(target_index, bool) or not isinstance(target_index, int):
                raise TypeError("target_index must be an integer")
            context = TargetSemanticsContextV3(
                context_id=_text(row.get("context_id"), "context_id"),
                mode=TargetSemanticsModeV3(row.get("mode")),
                target_index=target_index,
                target_classification=ContraTargetClassificationV2(
                    row.get("target_classification")
                ),
                target_name=_text(row.get("target_name"), "target_name"),
                equipped_item_names=tuple(names),
                target_classification_evidence=_evidence(
                    evidence["target_classification"],
                    "target_classification evidence",
                ),
                target_name_evidence=_evidence(
                    evidence["target_name"], "target_name evidence"
                ),
                equipment_evidence=_evidence(
                    evidence["equipped_item_names"], "equipment evidence"
                ),
                target_position_evidence=_evidence(
                    evidence["target_position"], "target_position evidence"
                ),
                target_health_pct_evidence=_evidence(
                    evidence["target_health_pct"], "target_health_pct evidence"
                ),
                target_max_health_evidence=_evidence(
                    evidence["target_max_health"], "target_max_health evidence"
                ),
                target_max_health=row.get("target_max_health"),
                health_pct_schedule=tuple(
                    HealthPercentPointV2(
                        time_ms=point["time_ms"], health_pct=point["health_pct"]
                    )
                    for point in schedule
                    if isinstance(point, Mapping)
                ),
            )
        except (TypeError, ValueError, KeyError, FuryPairedRunnerV4Error) as error:
            if isinstance(error, FuryDynamicV5BaselineAdapterV4Error):
                raise
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
                f"target context {ordinal}: {error}",
            ) from error
        if target_index in result:
            raise FuryDynamicV5BaselineAdapterV4Error(
                ("DYNAMIC_V5_TARGET_CONTEXT_MALFORMED",),
                "target_index is duplicated",
            )
        result[target_index] = context
    if set(result) != set(range(expected_count)):
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_TARGET_CONTEXTS_INCOMPLETE",),
            "target context indexes are not contiguous",
        )
    return result


def execute_dynamic_v5_baseline_v4(
    bridge: Any,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> JSONMap:
    """Execute deployed Contra or reject the two incomplete audited lanes."""

    policy_id = policy.get("policy_id")
    if policy_id == CAT_POLICY_ID:
        raise FuryDynamicV5BaselineAdapterV4Error(
            CAT_BLOCKERS,
            "Cat full-policy v5 invokes load_dynamic_v2, so armor/attackability/idle v3 semantics would be lost",
        )
    if policy_id == CONTRA260817_POLICY_ID:
        raise FuryDynamicV5BaselineAdapterV4Error(
            CONTRA260817_BLOCKERS,
            "source traversal exists but its target/item/equipment sinks and full rollout do not",
        )
    if policy_id != CONTRA_DEPLOYED_POLICY_ID:
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("RUNNER_V4_BASELINE_POLICY_UNSUPPORTED",),
            f"unsupported baseline policy: {policy_id!r}",
        )
    request = _mapping(scenario.get("request"), "scenario.request")
    config = _mapping(
        scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
    )
    seed = group.get("simulator_seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_GROUP_BINDING_MALFORMED",),
            "group.simulator_seed must be an integer",
        )
    dynamic_load = bind_dynamic_v5_load(request, seed, config)
    if group.get("dynamic_load_contract_sha256") != dynamic_load.contract_sha256:
        raise FuryDynamicV5BaselineAdapterV4Error(
            ("DYNAMIC_V5_GROUP_BINDING_MISMATCH",),
            "group dynamic-load contract differs from request/config/seed",
        )
    contexts = target_contexts_from_runner_v4(
        _mapping(
            scenario.get("target_context_bundle"),
            "scenario.target_context_bundle",
        )
    )
    artifact = run_fury_full_policy_rollout_v5(
        bridge,
        request,
        ContraDeployedFuryAdapterV2(),
        seed=seed,
        target_contexts=contexts,
        dynamic_load=dynamic_load,
    )
    return {
        "lane_result": build_fury_v5_lane_result_v4(
            artifact,
            policy_id=CONTRA_DEPLOYED_POLICY_ID,
            group=group,
            scenario=scenario,
        )
    }


__all__ = (
    "CAT_BLOCKERS",
    "CONTRA260817_BLOCKERS",
    "FuryDynamicV5BaselineAdapterV4Error",
    "execute_dynamic_v5_baseline_v4",
    "target_contexts_from_runner_v4",
)
