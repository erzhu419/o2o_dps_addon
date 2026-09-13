"""Development-only typed simulator adapter for weighted Fury clone V2.

Runtime probability arithmetic reads only the compiler-owned
``runtime_bounded_float_projection``.  Exact rational numerators and
denominators are deliberately outside this module's runtime interface.

The executable reconstruction is intentionally narrow.  A Chronicle server
START is used as a proxy for action timing; Heroic Strike and Cleave STARTs are
submitted as queue requests at that proxy time even though the original client
queue request is unknown.  Sampled target roles are retained as diagnostics,
but the current ActionRef sink cannot reconstruct the original target switch.
The simulator's typed availability rows provide the complete legal mask.  No
raid context or unsupported action is synthesized, and this is not a claim to
the historical player's true policy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import random
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import historical_behavior_clone_v2 as clone_v2
from . import historical_behavior_clone_simulator_adapter_v1 as adapter_v1
from . import historical_fury_behavior_prototypes_v1 as prototypes_v1
from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    WAIT_ACTION,
)
from .sim_bridge import ActionRef, ActResult, AvailableAction


JSONMap = dict[str, Any]
ADAPTER_SCHEMA = "historical_behavior_clone_simulator_adapter/v2"
OBSERVATION_SCHEMA = "historical_behavior_clone_runtime_observation/v2"
EXECUTION_SCHEMA = "historical_behavior_clone_typed_sink_execution/v2"
EPOCH_SCHEMA = "historical_behavior_clone_decision_epoch/v2"
RUNTIME_CONTRACT_SCHEMA = "historical_behavior_clone_runtime_contract/v2"
EXACT_VALIDATION_RECEIPT_SCHEMA = (
    "historical_behavior_clone_exact_validation_receipt/v2"
)
DECISION_IDENTITY_SCHEMA = "historical_behavior_clone_decision_identity/v2"
SOURCE_REF = "historical_behavior_clone_v2.runtime_bounded_float_projection"
POLICY_ID_PREFIX = "chronicle.historical_fury.behavior_clone_v2."

# Weighted prototypes have unit total mass, not V1 integer-count scale.  Mark
# and target smoothing is therefore exactly zero.  Contexts are combined as an
# equal mixture of the normalized global head and every matched normalized
# marginal.  A context may redistribute only actions with positive global
# source mass.  Delay uses a previous-action product-limit cell when present
# and falls back to the global cell; it does not invent a cross-cell prior.
MARK_SMOOTHING_MASS = 0.0
TARGET_SMOOTHING_MASS = 0.0
MARK_CONTEXT_COMBINATION = "EQUAL_MIXTURE_GLOBAL_AND_MATCHED_MARGINALS"
DELAY_CELL_SELECTION = "PREVIOUS_ACTION_CELL_ELSE_GLOBAL_NO_CROSS_CELL_BACKOFF"
FLOAT_TOLERANCE = 1e-9

ACTION_SINK_BINDINGS_V2 = adapter_v1.ACTION_SINK_BINDINGS_V1

# The bridge accepts attempt IDs only for actions that can later produce a
# server-result receipt.  Battle Shout consumes the GCD but is state-only, so
# GCD membership alone is not the right condition.
RESULT_BEARING_ACTION_KEYS_V2 = frozenset(
    (
        "warrior.bloodthirst",
        "warrior.whirlwind",
        "warrior.slam",
        "warrior.execute",
        "warrior.hamstring",
        "warrior.pummel",
        "warrior.sunder_armor",
    )
)

EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2: JSONMap = {
    "policy_label_timing": "SERVER_OBSERVED_START_PROXY",
    "heroic_strike_and_cleave": (
        "EXECUTABLE_QUEUE_REQUEST_AT_SERVER_START_PROXY_TIME_NOT_OBSERVED_"
        "CLIENT_QUEUE_INTENT"
    ),
    "original_client_queue_intent": clone_v2.QUEUE_INTENT_STATUS,
    "original_target_switch_intent": clone_v2.QUEUE_INTENT_STATUS,
    "target_sink": (
        "SAMPLED_ROLE_RETAINED_DIAGNOSTICALLY; ACTION_REF_EXECUTES_ON_CURRENT_"
        "SIMULATOR_TARGET_WITHOUT_RECONSTRUCTED_SWITCH"
    ),
    "legality": "CONDITION_ONLY_ON_TYPED_SIMULATOR_AVAILABLE_ACTIONS",
    "raid_context": "NOT_SYNTHESIZED",
    "loadout_binding": (
        "CROSS_PLAYER_CROSS_BUILD_BEHAVIOR_TRANSPLANT_DEVELOPMENT_ONLY"
    ),
    "mark_smoothing_mass": MARK_SMOOTHING_MASS,
    "target_smoothing_mass": TARGET_SMOOTHING_MASS,
    "mark_context_combination": MARK_CONTEXT_COMBINATION,
    "delay_cell_selection": DELAY_CELL_SELECTION,
    "residual_survival": "EXPLICIT_WAIT_TO_SCENARIO_BOUNDARY_NO_DECISION",
    "true_player_policy_claim": False,
    "same_build_historical_expert_claim": False,
    "comparison_authorized": False,
}


class HistoricalBehaviorCloneSimulatorAdapterV2Error(RuntimeError):
    """The float projection, typed bridge, or pending proposal is malformed."""


class BehaviorCloneBridgeV2(Protocol):
    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def wait(self, wait_ms: int) -> Mapping[str, Any]: ...


def policy_id_for_prototype_v2(prototype_id: str) -> str:
    if not isinstance(prototype_id, str) or not prototype_id.strip():
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "prototype_id must be nonempty"
        )
    return POLICY_ID_PREFIX + prototype_id


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be an array"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _weight(value: Any, label: str, *, at_most_one: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be a binary64 number"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be finite and nonnegative"
        )
    if at_most_one and result > 1.0 + FLOAT_TOLERANCE:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} exceeds one"
        )
    return result


def _sha256_hex(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} must be a 64-character sha256 hex digest"
        )
    return value.lower()


def exact_validation_receipt_v2(model: Mapping[str, Any]) -> JSONMap:
    """Validate exact heads once, before entering the float-only runtime.

    The returned receipt is the external pin consumed by runtime entry points.
    Runtime validation deliberately does not call this function implicitly.
    """

    try:
        clone_v2.validate_model_v2(model)
    except (clone_v2.HistoricalBehaviorCloneV2Error, TypeError, ValueError) as error:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"exact clone-v2 validation failed: {error}"
        ) from error
    identity = _mapping(model.get("prototype_identity"), "prototype_identity")
    prototype_id = identity.get("prototype_id")
    policy_id = policy_id_for_prototype_v2(prototype_id)
    address = _mapping(model.get("content_address"), "content_address")
    return {
        "schema": EXACT_VALIDATION_RECEIPT_SCHEMA,
        "validator": "historical_behavior_clone_v2.validate_model_v2",
        "model_schema": clone_v2.MODEL_SCHEMA,
        "model_implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
        "model_status": clone_v2.STATUS,
        "prototype_id": str(prototype_id),
        "policy_id": policy_id,
        "model_sha256": _sha256_hex(
            address.get("sha256"), "exact validation model sha256"
        ),
        "exact_fraction_heads_validated": True,
        "runtime_projection_validated_against_exact_heads": True,
    }


def _expected_exact_validation_receipt(value: Any) -> JSONMap:
    receipt = _mapping(value, "expected exact validation receipt")
    if (
        receipt.get("schema") != EXACT_VALIDATION_RECEIPT_SCHEMA
        or receipt.get("validator")
        != "historical_behavior_clone_v2.validate_model_v2"
        or receipt.get("model_schema") != clone_v2.MODEL_SCHEMA
        or receipt.get("model_implementation_revision")
        != clone_v2.IMPLEMENTATION_REVISION
        or receipt.get("model_status") != clone_v2.STATUS
        or receipt.get("exact_fraction_heads_validated") is not True
        or receipt.get("runtime_projection_validated_against_exact_heads") is not True
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "expected exact validation receipt contract differs"
        )
    prototype_id = receipt.get("prototype_id")
    policy_id = policy_id_for_prototype_v2(prototype_id)
    if receipt.get("policy_id") != policy_id:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "expected exact validation receipt policy_id differs"
        )
    return {
        "schema": EXACT_VALIDATION_RECEIPT_SCHEMA,
        "validator": receipt["validator"],
        "model_schema": receipt["model_schema"],
        "model_implementation_revision": receipt[
            "model_implementation_revision"
        ],
        "model_status": receipt["model_status"],
        "prototype_id": str(prototype_id),
        "policy_id": policy_id,
        "model_sha256": _sha256_hex(
            receipt.get("model_sha256"),
            "expected exact validation receipt model_sha256",
        ),
        "exact_fraction_heads_validated": True,
        "runtime_projection_validated_against_exact_heads": True,
    }


def _source_binding(value: Any, *, prototype_id: str) -> JSONMap:
    source = _mapping(value, "source_binding")
    manifest = _mapping(source.get("prototype_manifest"), "source prototype_manifest")
    if manifest.get("schema") != prototypes_v1.MANIFEST_SCHEMA:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source prototype manifest schema differs"
        )
    if manifest.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source prototype manifest implementation revision differs"
        )
    manifest_binding = {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "file_sha256": _sha256_hex(
            manifest.get("file_sha256"), "source manifest file_sha256"
        ),
        "content_sha256": _sha256_hex(
            manifest.get("content_sha256"), "source manifest content_sha256"
        ),
    }
    partition = _mapping(source.get("prototype_partition"), "source prototype_partition")
    if partition.get("record_schema") != prototypes_v1.RECORD_SCHEMA:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source prototype partition schema differs"
        )
    if partition.get("implementation_revision") != prototypes_v1.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source prototype partition implementation revision differs"
        )
    partition_binding = {
        "record_schema": partition["record_schema"],
        "implementation_revision": partition["implementation_revision"],
        "record_count": _integer(
            partition.get("record_count"), "source partition record_count"
        ),
        "logical_size_bytes": _integer(
            partition.get("logical_size_bytes"),
            "source partition logical_size_bytes",
        ),
        "logical_content_sha256": _sha256_hex(
            partition.get("logical_content_sha256"),
            "source partition logical_content_sha256",
        ),
    }
    if source.get("prototype_id") != prototype_id:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source binding prototype_id differs from model identity"
        )
    if source.get("network_request_count") != 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source binding network_request_count differs"
        )
    if "path" in manifest or "path" in partition:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source binding contains a location-dependent path"
        )
    return {
        "prototype_manifest": manifest_binding,
        "prototype_partition": partition_binding,
        "prototype_id": prototype_id,
        "network_request_count": 0,
    }


def _counter(
    value: Any, keys: Iterable[str], label: str
) -> dict[str, float]:
    row = _mapping(value, label)
    expected = tuple(keys)
    if set(row) != set(expected):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} keys differ from the runtime ontology"
        )
    return {key: _weight(row[key], f"{label}[{key}]") for key in expected}


def _expected_delay_buckets() -> tuple[tuple[str, int, int | None], ...]:
    rows = []
    lower = 0
    for upper in clone_v2.DELAY_UPPER_BOUNDS_MS:
        rows.append((f"LE_{upper}", lower, upper))
        lower = upper + 1
    rows.append((f"GT_{clone_v2.DELAY_UPPER_BOUNDS_MS[-1]}", lower, None))
    return tuple(rows)


def _validate_delay_cell(value: Any, label: str) -> JSONMap:
    cell = _mapping(value, label)
    event_weight = _weight(cell.get("event_weight"), f"{label}.event_weight")
    censor_weight = _weight(cell.get("censor_weight"), f"{label}.censor_weight")
    residual = _weight(
        cell.get("residual_survival_after_last_bucket"),
        f"{label}.residual_survival_after_last_bucket",
        at_most_one=True,
    )
    raw_rows = _array(cell.get("buckets"), f"{label}.buckets")
    expected = _expected_delay_buckets()
    if len(raw_rows) != len(expected):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} bucket count differs"
        )
    rows = []
    event_mass = 0.0
    for index, ((bucket, lower, upper), raw) in enumerate(
        zip(expected, raw_rows, strict=True)
    ):
        row = _mapping(raw, f"{label}.buckets[{index}]")
        if (
            row.get("bucket") != bucket
            or row.get("lower_bound_ms") != lower
            or row.get("upper_bound_ms") != upper
        ):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                f"{label} bucket identity differs at {index}"
            )
        parsed = {
            "bucket": bucket,
            "lower_bound_ms": lower,
            "upper_bound_ms": upper,
        }
        for key in (
            "at_risk_weight",
            "event_weight",
            "censor_weight",
            "event_hazard",
            "survival_before",
            "event_probability_mass",
            "survival_after",
        ):
            parsed[key] = _weight(
                row.get(key),
                f"{label}.buckets[{index}].{key}",
                at_most_one=key
                in {
                    "event_hazard",
                    "survival_before",
                    "event_probability_mass",
                    "survival_after",
                },
            )
        representative = row.get("event_representative_ms")
        if representative is not None:
            representative = _integer(
                representative,
                f"{label}.buckets[{index}].event_representative_ms",
            )
        if parsed["event_probability_mass"] > 0 and representative is None:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                f"{label} positive event mass lacks a representative delay"
            )
        parsed["event_representative_ms"] = representative
        event_mass += parsed["event_probability_mass"]
        rows.append(parsed)
    if event_weight + censor_weight <= 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} has no source interval mass"
        )
    if abs(event_mass + residual - 1.0) > FLOAT_TOLERANCE:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} event and residual survival mass do not close"
        )
    return {
        "event_weight": event_weight,
        "censor_weight": censor_weight,
        "residual_survival_after_last_bucket": residual,
        "buckets": rows,
    }


def _runtime_model_binding(
    model: Mapping[str, Any],
    *,
    expected_model_binding: Mapping[str, Any],
) -> JSONMap:
    """Validate runtime-only surfaces without decoding exact rational heads."""

    expected = _expected_exact_validation_receipt(expected_model_binding)
    if model.get("schema") != clone_v2.MODEL_SCHEMA:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "unsupported clone-v2 model schema"
        )
    if model.get("implementation_revision") != clone_v2.IMPLEMENTATION_REVISION:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "clone-v2 implementation revision differs"
        )
    if model.get("status") != clone_v2.STATUS:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "clone-v2 status differs"
        )
    identity = _mapping(model.get("prototype_identity"), "prototype_identity")
    prototype_id = identity.get("prototype_id")
    policy_id = policy_id_for_prototype_v2(prototype_id)
    if (
        expected["prototype_id"] != prototype_id
        or expected["policy_id"] != policy_id
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "model identity differs from the expected exact validation receipt"
        )
    source_binding = _source_binding(
        model.get("source_binding"), prototype_id=str(prototype_id)
    )
    uncertainty = _mapping(model.get("uncertainty_contract"), "uncertainty_contract")
    if (
        uncertainty.get("client_next_swing_queue_intent")
        != clone_v2.QUEUE_INTENT_STATUS
        or uncertainty.get("target_switch_intent") != clone_v2.QUEUE_INTENT_STATUS
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "queue or target-switch uncertainty was weakened"
        )
    claims = _mapping(model.get("claim_boundary"), "claim_boundary")
    if any(
        claims.get(key) is not False
        for key in (
            "comparison_authorized",
            "same_equipment_matched_seed_comparison_authorized",
            "deployment_authorized",
            "full_expert_policy_claim",
            "superiority_claim_authorized",
        )
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "source model claim boundary is not development-only"
        )
    address = _mapping(model.get("content_address"), "content_address")
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "model content-address contract differs"
        )
    model_sha256 = _sha256_hex(address.get("sha256"), "model content sha256")
    if model_sha256 != expected["model_sha256"]:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "model sha256 differs from the expected exact validation receipt"
        )
    core = deepcopy(dict(model))
    core.pop("content_address", None)
    try:
        canonical = json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"model is not strict canonical JSON: {error}"
        ) from error
    if hashlib.sha256(canonical).hexdigest() != model_sha256:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "model content address does not close"
        )
    projection = _mapping(
        model.get("runtime_bounded_float_projection"),
        "runtime_bounded_float_projection",
    )
    if (
        projection.get("schema") != clone_v2.RUNTIME_PROJECTION_SCHEMA
        or projection.get("numeric_type") != "IEEE_754_BINARY64"
        or projection.get("finite_values_only") is not True
        or projection.get(
            "exact_fraction_numerators_and_denominators_runtime_decode_forbidden"
        )
        is not True
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "runtime bounded-float projection contract differs"
        )
    mark_global = _counter(
        projection.get("mark_global_weights"),
        clone_v2.ACTION_KEYS,
        "runtime mark global",
    )
    if sum(mark_global.values()) <= 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "runtime mark global is empty"
        )
    supported = {key for key, value in mark_global.items() if value > 0}
    mark_contexts: JSONMap = {}
    for family, raw_values in _mapping(
        projection.get("mark_context_weights"), "runtime mark contexts"
    ).items():
        values = _mapping(raw_values, f"runtime mark context {family}")
        mark_contexts[str(family)] = {}
        for value, raw_counts in values.items():
            counts = _counter(
                raw_counts,
                clone_v2.ACTION_KEYS,
                f"runtime mark context {family}:{value}",
            )
            if any(counts[key] > 0 for key in set(clone_v2.ACTION_KEYS) - supported):
                raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                    "a context assigns mass to a globally unsupported action"
                )
            mark_contexts[str(family)][str(value)] = counts
    targets: JSONMap = {}
    raw_targets = _mapping(
        projection.get("target_by_action_weights"), "runtime targets"
    )
    if set(raw_targets) != set(clone_v2.ACTION_KEYS):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "runtime target action ontology differs"
        )
    for action in clone_v2.ACTION_KEYS:
        targets[action] = _counter(
            raw_targets[action], clone_v2.TARGET_ROLES, f"runtime target {action}"
        )
        if action in supported and sum(targets[action].values()) <= 0:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                f"supported action {action} has no runtime target mass"
            )
    delay = _mapping(projection.get("delay_product_limit"), "runtime delay")
    global_delay = _validate_delay_cell(delay.get("global"), "runtime delay global")
    previous: JSONMap = {}
    for action, raw_cell in _mapping(
        delay.get("by_previous_action"), "runtime delay by previous action"
    ).items():
        if action != clone_v2.START_ACTION and action not in clone_v2.ACTION_KEYS:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "runtime delay previous action is outside the ontology"
            )
        previous[str(action)] = _validate_delay_cell(
            raw_cell, f"runtime delay previous {action}"
        )
    return {
        "prototype_id": str(prototype_id),
        "prototype_family": identity.get("prototype_family"),
        "policy_id": policy_id,
        "model_sha256": model_sha256,
        "exact_validation_receipt": expected,
        "source_binding": source_binding,
        "projection": {
            "schema": projection.get("schema"),
            "mark_global_weights": mark_global,
            "mark_context_weights": mark_contexts,
            "target_by_action_weights": targets,
            "delay_product_limit": {
                "global": global_delay,
                "by_previous_action": previous,
            },
        },
        "uncertainty_contract": deepcopy(dict(uncertainty)),
        "runtime_contract": deepcopy(EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2),
    }


def runtime_model_binding_v2(
    model: Mapping[str, Any],
    *,
    expected_model_binding: Mapping[str, Any],
) -> JSONMap:
    """Return the immutable runtime binding for one prototype model."""

    binding = _runtime_model_binding(
        model, expected_model_binding=expected_model_binding
    )
    result = {key: value for key, value in binding.items() if key != "projection"}
    result["runtime_projection_schema"] = binding["projection"]["schema"]
    return deepcopy(result)


def _normalize(weights: Mapping[str, float], label: str) -> dict[str, float]:
    positive = {key: value for key, value in weights.items() if value > 0}
    total = sum(positive.values())
    if total <= 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"{label} has no positive mass"
        )
    return {key: value / total for key, value in positive.items()}


def _runtime_context_values(observation: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    if observation.get("schema") != OBSERVATION_SCHEMA:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "unsupported clone-v2 runtime observation"
        )
    elapsed = _integer(
        observation.get("scenario_elapsed_ms"), "scenario_elapsed_ms"
    )
    target_count = _integer(
        observation.get("observed_target_count"), "observed_target_count"
    )
    dead_count = _integer(
        observation.get("observed_dead_target_count"),
        "observed_dead_target_count",
    )
    if dead_count > target_count:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "dead target count exceeds target count"
        )
    last_action = observation.get("last_controllable_action")
    if last_action is None:
        last_action = "NONE"
    elif last_action not in clone_v2.ACTION_SPEC_BY_KEY:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "last action is outside the ontology"
        )
    expected_lane = (
        "NONE"
        if last_action == "NONE"
        else clone_v2.ACTION_SPEC_BY_KEY[str(last_action)].lane
    )
    last_lane = observation.get("last_controllable_lane")
    if last_lane is None:
        last_lane = expected_lane
    elif last_lane != expected_lane:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "last action lane differs from the ontology"
        )
    age = observation.get("last_gcd_action_age_ms")
    age_bucket = (
        "NEVER_OBSERVED"
        if age is None
        else policy_v2._elapsed_bucket(_integer(age, "last_gcd_action_age_ms"))
    )
    stance = observation.get("observed_stance")
    if stance is None:
        stance = "UNKNOWN_BEFORE_OBSERVED_STANCE_ACTION"
    elif stance in clone_v2.clone_v1.STANCE_ACTION_KEYS:
        stance = str(stance).removeprefix("warrior.")
    elif stance not in {"battle_stance", "defensive_stance", "berserker_stance"}:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "unsupported observed stance"
        )
    coverage = observation.get("prefix_observation_status")
    if coverage != "OBSERVED_FROM_WAVE_ANCHOR":
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "simulator observation cannot invent left-truncated raid context"
        )
    coarse = {
        "wave_elapsed_bucket": policy_v2._elapsed_bucket(elapsed),
        "observed_target_count_bucket": policy_v2._count_bucket(target_count),
        "observed_dead_target_count_bucket": policy_v2._count_bucket(dead_count),
    }
    return (
        ("base.wave_coarse", json.dumps(coarse, sort_keys=True, separators=(",", ":"))),
        (
            "target.has_last_observed_target",
            "YES" if observation.get("actor_has_last_target") is True else "NO",
        ),
        ("coverage.prefix_observation_status", str(coverage)),
        ("sequence.last_controllable_action", str(last_action)),
        ("sequence.last_controllable_lane", str(last_lane)),
        ("sequence.last_gcd_action_age", age_bucket),
        ("sequence.observed_stance", str(stance)),
    )


def _predict_mark_distribution(
    projection: Mapping[str, Any],
    observation: Mapping[str, Any],
    legal_actions: Iterable[str],
) -> JSONMap:
    global_weights = _mapping(projection.get("mark_global_weights"), "mark global")
    supported = {key for key, weight in global_weights.items() if float(weight) > 0}
    legal = set(legal_actions)
    unknown = legal.difference(clone_v2.ACTION_KEYS)
    if unknown:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            f"legal mask has unknown actions: {sorted(unknown)}"
        )
    distributions = [_normalize({key: float(global_weights[key]) for key in supported}, "mark global")]
    matched = []
    contexts = _mapping(projection.get("mark_context_weights"), "mark contexts")
    for family, value in _runtime_context_values(observation):
        family_rows = contexts.get(family)
        counts = family_rows.get(value) if isinstance(family_rows, Mapping) else None
        if not isinstance(counts, Mapping):
            continue
        local = {key: float(counts[key]) for key in supported}
        if sum(local.values()) <= 0:
            continue
        distributions.append(_normalize(local, f"mark context {family}"))
        matched.append(family)
    mixture = {
        key: sum(row.get(key, 0.0) for row in distributions) / len(distributions)
        for key in supported
    }
    legal_mass = sum(mixture[key] for key in supported if key in legal)
    conditioned = (
        {}
        if legal_mass <= 0
        else {
            key: mixture[key] / legal_mass
            for key in clone_v2.ACTION_KEYS
            if key in legal and key in supported
        }
    )
    return {
        "probabilities": conditioned,
        "globally_supported_actions": [
            key for key in clone_v2.ACTION_KEYS if key in supported
        ],
        "matched_context_families": matched,
        "context_combination": MARK_CONTEXT_COMBINATION,
        "mark_smoothing_mass": MARK_SMOOTHING_MASS,
        "legal_mask_applied_before_sampling": True,
    }


def predict_mark_distribution_v2(
    model: Mapping[str, Any],
    observation: Mapping[str, Any],
    legal_actions: Iterable[str],
    *,
    expected_model_binding: Mapping[str, Any],
) -> JSONMap:
    binding = _runtime_model_binding(
        model, expected_model_binding=expected_model_binding
    )
    return _predict_mark_distribution(binding["projection"], observation, legal_actions)


def _delay_outcomes(
    projection: Mapping[str, Any], previous_action_key: str | None
) -> JSONMap:
    previous = clone_v2.START_ACTION if previous_action_key is None else previous_action_key
    if previous != clone_v2.START_ACTION and previous not in clone_v2.ACTION_KEYS:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "previous action is outside the ontology"
        )
    delay = _mapping(projection.get("delay_product_limit"), "delay product limit")
    by_previous = _mapping(delay.get("by_previous_action"), "delay by previous")
    if previous in by_previous:
        cell = _mapping(by_previous[previous], f"delay previous {previous}")
        source = "PREVIOUS_ACTION_PRODUCT_LIMIT_CELL"
    else:
        cell = _mapping(delay.get("global"), "delay global")
        source = "GLOBAL_PRODUCT_LIMIT_FALLBACK"
    outcomes = []
    for raw in _array(cell.get("buckets"), "delay buckets"):
        row = _mapping(raw, "delay bucket")
        mass = float(row["event_probability_mass"])
        if mass <= 0:
            continue
        outcomes.append(
            {
                "kind": "FINITE_NEXT_START_PROXY_DELAY",
                "bucket": row["bucket"],
                "probability_mass": mass,
                "representative_ms": row["event_representative_ms"],
            }
        )
    residual = float(cell["residual_survival_after_last_bucket"])
    return {
        "previous_action_key": previous,
        "cell_source": source,
        "cell_selection": DELAY_CELL_SELECTION,
        "finite_outcomes": outcomes,
        "residual_survival_mass": residual,
        "total_probability_mass": sum(
            row["probability_mass"] for row in outcomes
        )
        + residual,
        "residual_survival_converted_to_action_or_finite_delay": False,
    }


def project_delay_outcomes_v2(
    model: Mapping[str, Any],
    previous_action_key: str | None,
    *,
    expected_model_binding: Mapping[str, Any],
) -> JSONMap:
    binding = _runtime_model_binding(
        model, expected_model_binding=expected_model_binding
    )
    return _delay_outcomes(binding["projection"], previous_action_key)


def _weighted_choice(weights: Mapping[str, float], rng: random.Random) -> str:
    positive = [(key, value) for key, value in weights.items() if value > 0]
    if not positive:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "cannot sample empty runtime mass"
        )
    if len(positive) == 1:
        return positive[0][0]
    total = sum(value for _, value in positive)
    draw = rng.random() * total
    cumulative = 0.0
    for key, value in positive:
        cumulative += value
        if draw < cumulative:
            return key
    return positive[-1][0]


@dataclass
class HistoricalBehaviorCloneRuntimeV2:
    """Semi-Markov runtime over compiler-projected binary64 weights only."""

    projection: Mapping[str, Any]
    prototype_id: str
    seed: int
    start_ms: int
    horizon_ms: int
    last_action_key: str | None = field(init=False, default=None)
    next_action_at_ms: int | None = field(init=False, default=None)
    last_schedule_outcome: JSONMap = field(init=False, default_factory=dict)
    _pending: JSONMap | None = field(init=False, default=None)
    _proposal_ordinal: int = field(init=False, default=0)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "runtime seed must be an integer"
            )
        self.start_ms = _integer(self.start_ms, "runtime start_ms")
        self.horizon_ms = _integer(self.horizon_ms, "runtime horizon_ms", minimum=1)
        self._rng = random.Random(self.seed)
        self._schedule(self.start_ms, None)

    @property
    def boundary_ms(self) -> int:
        return self.start_ms + self.horizon_ms

    @property
    def globally_supported_actions(self) -> tuple[str, ...]:
        values = _mapping(self.projection.get("mark_global_weights"), "mark global")
        return tuple(key for key in clone_v2.ACTION_KEYS if float(values[key]) > 0)

    def _schedule(self, now_ms: int, previous_action: str | None) -> JSONMap:
        projected = _delay_outcomes(self.projection, previous_action)
        weights = {
            f"finite:{index}": row["probability_mass"]
            for index, row in enumerate(projected["finite_outcomes"])
        }
        weights["residual"] = projected["residual_survival_mass"]
        selected = _weighted_choice(weights, self._rng)
        if selected == "residual":
            self.next_action_at_ms = None
            outcome = {
                "kind": "NO_FURTHER_OBSERVED_START_PROXY_BEFORE_BOUNDARY",
                "cause": "RESIDUAL_PRODUCT_LIMIT_SURVIVAL",
                "scheduled_from_ms": now_ms,
                "boundary_ms": self.boundary_ms,
                "residual_survival_mass": projected["residual_survival_mass"],
                "delay_projection": projected,
                "converted_to_action_or_finite_delay": False,
            }
        else:
            index = int(selected.removeprefix("finite:"))
            finite = projected["finite_outcomes"][index]
            due = now_ms + int(finite["representative_ms"])
            if due >= self.boundary_ms:
                self.next_action_at_ms = None
                outcome = {
                    "kind": "NO_START_PROXY_BEFORE_BOUNDARY",
                    "cause": "FINITE_PROJECTED_DELAY_AT_OR_AFTER_BOUNDARY",
                    "scheduled_from_ms": now_ms,
                    "projected_action_at_ms": due,
                    "boundary_ms": self.boundary_ms,
                    "selected_finite_outcome": deepcopy(finite),
                    "delay_projection": projected,
                    "converted_to_action_or_finite_delay": False,
                }
            else:
                self.next_action_at_ms = due
                outcome = {
                    "kind": "FINITE_NEXT_START_PROXY_DELAY",
                    "scheduled_from_ms": now_ms,
                    "next_action_at_ms": due,
                    "selected_finite_outcome": deepcopy(finite),
                    "delay_projection": projected,
                }
        self.last_schedule_outcome = outcome
        return deepcopy(outcome)

    def propose(
        self,
        *,
        now_ms: int,
        observation: Mapping[str, Any],
        legal_actions: Iterable[str],
        no_legal_wait_ms: int,
        supported_action_present: bool,
    ) -> JSONMap:
        now = _integer(now_ms, "runtime now_ms")
        if now >= self.boundary_ms:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "simulator requested a decision at or after the configured horizon"
            )
        if self._pending is not None:
            return deepcopy(self._pending)
        if self.next_action_at_ms is None:
            return {
                "kind": "WAIT_TO_BOUNDARY",
                "wait_ms": self.boundary_ms - now,
                "reason": self.last_schedule_outcome["cause"],
                "delay_outcome": deepcopy(self.last_schedule_outcome),
                "no_decision": True,
            }
        if now < self.next_action_at_ms:
            return {
                "kind": "WAIT",
                "wait_ms": min(self.next_action_at_ms, self.boundary_ms) - now,
                "reason": "FINITE_SEMI_MARKOV_CLOCK_NOT_DUE",
                "next_action_at_ms": self.next_action_at_ms,
                "delay_outcome": deepcopy(self.last_schedule_outcome),
                "no_decision": True,
            }
        prediction = _predict_mark_distribution(
            self.projection, observation, legal_actions
        )
        probabilities = prediction["probabilities"]
        if not probabilities:
            wait_ms = self.boundary_ms - now
            if supported_action_present:
                wait_ms = min(wait_ms, max(1, no_legal_wait_ms))
                kind = "WAIT"
                reason = "NO_LEGAL_SOURCE_SUPPORTED_ACTION"
            else:
                kind = "WAIT_TO_BOUNDARY"
                reason = "NO_SOURCE_SUPPORTED_ACTION_PRESENT"
            return {
                "kind": kind,
                "wait_ms": wait_ms,
                "reason": reason,
                "no_decision": True,
                "legal_mark_prediction": prediction,
            }
        action_key = _weighted_choice(probabilities, self._rng)
        target_weights = _mapping(
            _mapping(
                self.projection.get("target_by_action_weights"), "runtime targets"
            )[action_key],
            f"runtime target {action_key}",
        )
        target_distribution = _normalize(
            {key: float(target_weights[key]) for key in clone_v2.TARGET_ROLES},
            f"runtime target {action_key}",
        )
        target_role = _weighted_choice(target_distribution, self._rng)
        spec = clone_v2.ACTION_SPEC_BY_KEY[action_key]
        proposal = {
            "kind": "ACTION",
            "proposal_id": self._proposal_ordinal,
            "action_key": action_key,
            "lane": spec.lane,
            "target_role": target_role,
            "conditioned_probability": probabilities[action_key],
            "target_probability": target_distribution[target_role],
            "mark_prediction": prediction,
            "server_start_proxy_timing": True,
            "client_queue_intent_observed": False,
            "target_switch_intent_observed": False,
        }
        self._proposal_ordinal += 1
        self._pending = proposal
        return deepcopy(proposal)

    def record_submission(
        self, *, proposal_id: int, accepted: bool, now_ms: int
    ) -> JSONMap | None:
        now = _integer(now_ms, "submission now_ms")
        if self._pending is None or self._pending.get("proposal_id") != proposal_id:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "submission does not match the pending proposal"
            )
        if not isinstance(accepted, bool):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "accepted must be boolean"
            )
        action_key = str(self._pending["action_key"])
        self._pending = None
        if not accepted:
            self.next_action_at_ms = now
            return None
        self.last_action_key = action_key
        return self._schedule(now, action_key)


@dataclass(frozen=True)
class BehaviorCloneFrameV2:
    state: Mapping[str, Any]
    observation: Mapping[str, Any]
    available_actions: tuple[AvailableAction, ...]

    def __post_init__(self) -> None:
        now = _integer(self.state.get("time_ms"), "frame state.time_ms")
        if self.observation.get("schema") != OBSERVATION_SCHEMA:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "frame has unsupported clone-v2 observation"
            )
        if self.observation.get("simulator_time_ms") != now:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "observation simulator time differs from state"
            )
        if any(not isinstance(row, AvailableAction) for row in self.available_actions):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "available_actions must contain AvailableAction values"
            )

    @property
    def now_ms(self) -> int:
        return int(self.state["time_ms"])


class HistoricalBehaviorCloneSimulatorAdapterV2:
    """Bind one V2 prototype identity to the existing typed action sinks."""

    def __init__(
        self,
        model: Mapping[str, Any],
        *,
        expected_model_binding: Mapping[str, Any],
        simulator_seed: int,
        start_ms: int,
        horizon_ms: int,
    ) -> None:
        binding = _runtime_model_binding(
            model, expected_model_binding=expected_model_binding
        )
        self.binding = {
            key: deepcopy(value)
            for key, value in binding.items()
            if key != "projection"
        }
        self.expert_id = str(binding["policy_id"])
        self.prototype_id = str(binding["prototype_id"])
        self.runtime = HistoricalBehaviorCloneRuntimeV2(
            projection=deepcopy(binding["projection"]),
            prototype_id=self.prototype_id,
            seed=simulator_seed,
            start_ms=start_ms,
            horizon_ms=horizon_ms,
        )
        self._pending_decision: ExpertDecision | None = None
        self._pending_wait_decision: ExpertDecision | None = None
        self._decision_ordinal = 0

    def _issue_decision_identity(self, *, kind: str, now_ms: int) -> JSONMap:
        identity = {
            "schema": DECISION_IDENTITY_SCHEMA,
            "decision_ordinal": self._decision_ordinal,
            "issued_at_ms": now_ms,
            "proposal_kind": kind,
            "prototype_id": self.prototype_id,
            "policy_id": self.expert_id,
            "model_sha256": self.binding["model_sha256"],
        }
        self._decision_ordinal += 1
        return identity

    def _validate_decision_identity(
        self,
        decision: ExpertDecision,
        *,
        proposal_kinds: set[str],
        now_ms: int | None = None,
    ) -> Mapping[str, Any]:
        metadata = _mapping(decision.metadata, "decision metadata")
        proposal = _mapping(metadata.get("runtime_proposal"), "runtime proposal")
        kind = proposal.get("kind")
        identity = _mapping(
            metadata.get("decision_identity"), "decision identity"
        )
        if (
            decision.valid is not True
            or decision.expert_id != self.expert_id
            or decision.provenance.role is not ExpertRole.CANDIDATE
            or metadata.get("schema") != ADAPTER_SCHEMA
            or metadata.get("prototype_id") != self.prototype_id
            or metadata.get("policy_id") != self.expert_id
            or metadata.get("model_sha256") != self.binding["model_sha256"]
            or kind not in proposal_kinds
            or identity.get("schema") != DECISION_IDENTITY_SCHEMA
            or identity.get("proposal_kind") != kind
            or identity.get("prototype_id") != self.prototype_id
            or identity.get("policy_id") != self.expert_id
            or identity.get("model_sha256") != self.binding["model_sha256"]
        ):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "decision identity is not bound to this adapter model and policy"
            )
        _integer(identity.get("decision_ordinal"), "decision identity ordinal")
        issued_at = _integer(
            identity.get("issued_at_ms"), "decision identity issued_at_ms"
        )
        if now_ms is not None and issued_at != now_ms:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "decision was not issued for the executor's current time"
            )
        return proposal

    @property
    def provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                "o2o_dps/historical_behavior_clone_v2.py",
                "explicit weighted-prototype model artifact",
            ),
            source_refs=(
                clone_v2.MODEL_SCHEMA,
                clone_v2.RUNTIME_PROJECTION_SCHEMA,
                ADAPTER_SCHEMA,
                self.prototype_id,
                str(self.binding["model_sha256"]),
            ),
        )

    def propose(self, frame: BehaviorCloneFrameV2) -> ExpertDecision:
        if not isinstance(frame, BehaviorCloneFrameV2):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "adapter requires BehaviorCloneFrameV2"
            )
        if frame.observation.get("scenario_elapsed_ms") != (
            frame.now_ms - self.runtime.start_ms
        ):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "observation elapsed time differs from runtime start"
            )
        legal, legal_status = adapter_v1.legal_action_mask_v1(
            frame.available_actions
        )
        available_by_ref = {row.action: row for row in frame.available_actions}
        supported_rows = [
            available_by_ref.get(ACTION_SINK_BINDINGS_V2[key].action_ref)
            for key in self.runtime.globally_supported_actions
        ]
        supported_rows = [row for row in supported_rows if row is not None]
        ready_waits = [
            max(1, int(row.ready_in_ms))
            for row in supported_rows
            if isinstance(row.ready_in_ms, int) and not isinstance(row.ready_in_ms, bool)
        ]
        proposal = self.runtime.propose(
            now_ms=frame.now_ms,
            observation=frame.observation,
            legal_actions=legal,
            no_legal_wait_ms=min(ready_waits, default=1),
            supported_action_present=bool(supported_rows),
        )
        if proposal["kind"] in {"WAIT", "WAIT_TO_BOUNDARY"}:
            if self._pending_decision is not None:
                raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                    "runtime returned WAIT while an action is pending"
                )
            if self._pending_wait_decision is not None:
                pending = self._validate_decision_identity(
                    self._pending_wait_decision,
                    proposal_kinds={"WAIT", "WAIT_TO_BOUNDARY"},
                    now_ms=frame.now_ms,
                )
                if (
                    pending.get("kind") != proposal.get("kind")
                    or pending.get("wait_ms") != proposal.get("wait_ms")
                    or pending.get("reason") != proposal.get("reason")
                ):
                    raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                        "runtime changed an unexecuted pending WAIT proposal"
                    )
                return self._pending_wait_decision
            decision = ExpertDecision(
                provenance=self.provenance,
                valid=True,
                gcd=WAIT_ACTION,
                wait_ms=int(proposal["wait_ms"]),
                eligible_for_independent_vote=False,
                reason=str(proposal["reason"]),
                metadata={
                    "schema": ADAPTER_SCHEMA,
                    "prototype_id": self.prototype_id,
                    "policy_id": self.expert_id,
                    "model_sha256": self.binding["model_sha256"],
                    "decision_identity": self._issue_decision_identity(
                        kind=str(proposal["kind"]), now_ms=frame.now_ms
                    ),
                    "runtime_proposal": deepcopy(proposal),
                    "legal_action_status": legal_status,
                    "runtime_contract": deepcopy(
                        EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2
                    ),
                    "comparison_authorized": False,
                    "voting_eligible": False,
                },
            )
            self._pending_wait_decision = decision
            return decision
        if self._pending_wait_decision is not None:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "runtime returned ACTION while a WAIT is pending"
            )
        proposal_id = int(proposal["proposal_id"])
        if self._pending_decision is not None:
            pending = self._pending_decision.metadata["runtime_proposal"]
            if (
                pending.get("proposal_id") != proposal_id
                or pending.get("action_key") != proposal.get("action_key")
            ):
                raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                    "runtime changed an unsubmitted pending proposal"
                )
            return self._pending_decision
        action_key = str(proposal["action_key"])
        binding = ACTION_SINK_BINDINGS_V2[action_key]
        raw = RawSink(
            binding.sink_channel,
            binding.sink_operation,
            action_key,
            SOURCE_REF,
        )
        kwargs: JSONMap = {
            "gcd": WAIT_ACTION,
            "wait_ms": 1,
            "swing_queue": binding.queue,
            "stance": binding.stance,
            "off_gcd": ((action_key,) if binding.sink_channel == "off_gcd" else ()),
        }
        if binding.sink_channel == "gcd":
            kwargs["gcd"] = action_key
            kwargs["wait_ms"] = None
        queue_proxy = (
            "QUEUE_REQUEST_AT_SERVER_START_PROXY_TIME_CLIENT_INTENT_UNKNOWN"
            if binding.sink_channel == "swing_queue"
            else "NOT_APPLICABLE"
        )
        decision = ExpertDecision(
            provenance=self.provenance,
            valid=True,
            raw_sink_order=(raw,),
            eligible_for_independent_vote=False,
            reason="weighted Fury prototype START-proxy reconstruction",
            metadata={
                "schema": ADAPTER_SCHEMA,
                "prototype_id": self.prototype_id,
                "policy_id": self.expert_id,
                "model_sha256": self.binding["model_sha256"],
                "decision_identity": self._issue_decision_identity(
                    kind="ACTION", now_ms=frame.now_ms
                ),
                "runtime_proposal": deepcopy(proposal),
                "legal_action_status": legal_status,
                "raw_gcd_calls": [action_key] if binding.sink_channel == "gcd" else [],
                "queue_proxy_semantics": queue_proxy,
                "target_role": proposal["target_role"],
                "target_routing": EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2[
                    "target_sink"
                ],
                "runtime_contract": deepcopy(
                    EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2
                ),
                "normalized_wait_placeholder_submitted": False,
                "comparison_authorized": False,
                "voting_eligible": False,
            },
            **kwargs,
        )
        self._pending_decision = decision
        return decision

    def _preflight_pending(
        self, decision: ExpertDecision, *, now_ms: int | None = None
    ) -> Mapping[str, Any]:
        if self._pending_decision is None or decision is not self._pending_decision:
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "decision is not the adapter's current pending proposal"
            )
        return self._validate_decision_identity(
            decision, proposal_kinds={"ACTION"}, now_ms=now_ms
        )

    def _preflight_wait(
        self, decision: ExpertDecision, *, now_ms: int
    ) -> Mapping[str, Any]:
        if (
            self._pending_wait_decision is None
            or decision is not self._pending_wait_decision
        ):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "WAIT decision is not the adapter's current pending proposal"
            )
        proposal = self._validate_decision_identity(
            decision,
            proposal_kinds={"WAIT", "WAIT_TO_BOUNDARY"},
            now_ms=now_ms,
        )
        if (
            decision.gcd != WAIT_ACTION
            or proposal.get("wait_ms") != decision.wait_ms
        ):
            raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
                "WAIT decision payload differs from its runtime proposal"
            )
        return proposal

    def _consume_wait(self, decision: ExpertDecision, *, now_ms: int) -> None:
        self._preflight_wait(decision, now_ms=now_ms)
        self._pending_wait_decision = None

    def record_action_result(
        self, decision: ExpertDecision, *, accepted: bool, now_ms: int
    ) -> JSONMap | None:
        proposal = self._preflight_pending(decision, now_ms=now_ms)
        schedule = self.runtime.record_submission(
            proposal_id=int(proposal["proposal_id"]),
            accepted=accepted,
            now_ms=now_ms,
        )
        self._pending_decision = None
        return schedule


def _decision_binding(
    adapter: HistoricalBehaviorCloneSimulatorAdapterV2,
    decision: ExpertDecision,
) -> adapter_v1.ActionSinkBindingV1:
    adapter._validate_decision_identity(decision, proposal_kinds={"ACTION"})
    if (
        decision.expert_id != adapter.expert_id
        or decision.provenance.role is not ExpertRole.CANDIDATE
        or decision.metadata.get("prototype_id") != adapter.prototype_id
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "decision is not bound to this clone-v2 prototype"
        )
    proposal = decision.metadata.get("runtime_proposal")
    if not isinstance(proposal, Mapping) or proposal.get("kind") != "ACTION":
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "decision does not carry an ACTION proposal"
        )
    action_key = proposal.get("action_key")
    if action_key not in ACTION_SINK_BINDINGS_V2:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "decision action is outside typed sink coverage"
        )
    binding = ACTION_SINK_BINDINGS_V2[str(action_key)]
    if len(decision.raw_sink_order) != 1:
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "clone-v2 action must have one raw sink"
        )
    sink = decision.raw_sink_order[0]
    if (
        sink.channel != binding.sink_channel
        or sink.operation != binding.sink_operation
        or sink.value != binding.action_key
        or sink.source_ref != SOURCE_REF
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "decision raw sink differs from its typed binding"
        )
    return binding


def execute_historical_behavior_clone_decision_v2(
    adapter: HistoricalBehaviorCloneSimulatorAdapterV2,
    bridge: BehaviorCloneBridgeV2,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id: str | None = None,
) -> JSONMap:
    if not isinstance(adapter, HistoricalBehaviorCloneSimulatorAdapterV2):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "executor requires a clone-v2 adapter"
        )
    now = _integer(state.get("time_ms"), "executor state.time_ms")
    proposal = decision.metadata.get("runtime_proposal")
    if (
        decision.gcd == WAIT_ACTION
        and isinstance(proposal, Mapping)
        and proposal.get("kind") in {"WAIT", "WAIT_TO_BOUNDARY"}
    ):
        adapter._preflight_wait(decision, now_ms=now)
        after = bridge.wait(int(decision.wait_ms))
        adapter._consume_wait(decision, now_ms=now)
        return {
            "schema": EXECUTION_SCHEMA,
            "kind": str(proposal["kind"]),
            "prototype_id": adapter.prototype_id,
            "policy_id": adapter.expert_id,
            "proposal": decision.to_dict(),
            "wait_ms": decision.wait_ms,
            "no_decision": True,
            "residual_survival_outcome": (
                proposal.get("reason") == "RESIDUAL_PRODUCT_LIMIT_SURVIVAL"
            ),
            "bridge_submission": "SUBMITTED",
            "client_acceptance": "ACCEPTED",
            "decision_consumed": True,
            "final_state": deepcopy(dict(after)),
            "comparison_authorized": False,
        }
    binding = _decision_binding(adapter, decision)
    adapter._preflight_pending(decision, now_ms=now)
    available = adapter_v1._available_by_ref(bridge.actions())
    row = available.get(binding.action_ref)
    if row is None or not row.legal or row.triggers_gcd is not binding.triggers_gcd:
        schedule = adapter.record_action_result(
            decision, accepted=False, now_ms=now
        )
        return {
            "schema": EXECUTION_SCHEMA,
            "kind": "ACTION",
            "prototype_id": adapter.prototype_id,
            "policy_id": adapter.expert_id,
            "proposal": decision.to_dict(),
            "typed_sink_binding": binding.to_dict(),
            "bridge_submission": "NOT_SUBMITTED_NO_LONGER_LEGAL",
            "client_acceptance": "REJECTED_PRE_SUBMISSION",
            "decision_consumed": False,
            "final_state": deepcopy(dict(state)),
            "next_delay_outcome": schedule,
            "comparison_authorized": False,
        }
    result = (
        bridge.act(binding.action_ref)
        if attempt_id is None
        else bridge.act(binding.action_ref, attempt_id=attempt_id)
    )
    if not isinstance(result, ActResult):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "bridge.act must return ActResult"
        )
    schedule = adapter.record_action_result(
        decision, accepted=result.casted, now_ms=now
    )
    expected_consumption = binding.triggers_gcd and result.casted
    return {
        "schema": EXECUTION_SCHEMA,
        "kind": "ACTION",
        "prototype_id": adapter.prototype_id,
        "policy_id": adapter.expert_id,
        "proposal": decision.to_dict(),
        "typed_sink_binding": binding.to_dict(),
        "bridge_submission": "SUBMITTED",
        "client_acceptance": "ACCEPTED" if result.casted else "REJECTED",
        "decision_consumed": result.consumes_decision,
        "consumption_matches_lane": result.consumes_decision is expected_consumption,
        "final_state": deepcopy(dict(result.state)),
        "next_delay_outcome": schedule,
        "queue_proxy_semantics": decision.metadata.get("queue_proxy_semantics"),
        "target_routing": decision.metadata.get("target_routing"),
        "attempt_id": attempt_id,
        "comparison_authorized": False,
    }


ObservationFactoryV2 = Callable[
    [Mapping[str, Any], tuple[AvailableAction, ...], HistoricalBehaviorCloneSimulatorAdapterV2],
    Mapping[str, Any],
]


def run_historical_behavior_clone_epoch_v2(
    adapter: HistoricalBehaviorCloneSimulatorAdapterV2,
    bridge: BehaviorCloneBridgeV2,
    initial_state: Mapping[str, Any],
    observation_factory: ObservationFactoryV2,
    *,
    max_substeps: int = 32,
    attempt_id_prefix: str | None = None,
) -> JSONMap:
    max_substeps = _integer(max_substeps, "max_substeps", minimum=1)
    if attempt_id_prefix is not None and (
        not isinstance(attempt_id_prefix, str) or not attempt_id_prefix.strip()
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
            "attempt_id_prefix must be nonempty or None"
        )
    state = deepcopy(dict(initial_state))
    steps = []
    for substep_index in range(max_substeps):
        available = tuple(bridge.actions())
        frame = BehaviorCloneFrameV2(
            state=state,
            observation=observation_factory(state, available, adapter),
            available_actions=available,
        )
        decision = adapter.propose(frame)
        runtime_proposal = decision.metadata.get("runtime_proposal", {})
        binding = (
            _decision_binding(adapter, decision)
            if runtime_proposal.get("kind") == "ACTION"
            else None
        )
        attempt_id = (
            f"{attempt_id_prefix}:substep-{substep_index}"
            if attempt_id_prefix is not None
            and binding is not None
            and binding.action_key in RESULT_BEARING_ACTION_KEYS_V2
            else None
        )
        execution = execute_historical_behavior_clone_decision_v2(
            adapter, bridge, decision, state, attempt_id=attempt_id
        )
        steps.append(execution)
        state = deepcopy(dict(execution["final_state"]))
        if execution["decision_consumed"] is True:
            return {
                "schema": EPOCH_SCHEMA,
                "status": "COMPLETE_DEVELOPMENT_ONLY",
                "prototype_id": adapter.prototype_id,
                "policy_id": adapter.expert_id,
                "substep_count": len(steps),
                "steps": steps,
                "final_state": state,
                "residual_survival_outcome_count": sum(
                    step.get("residual_survival_outcome") is True for step in steps
                ),
                "wait_to_boundary_count": sum(
                    step.get("kind") == "WAIT_TO_BOUNDARY" for step in steps
                ),
                "comparison_authorized": False,
                "voting_eligible": False,
            }
    raise HistoricalBehaviorCloneSimulatorAdapterV2Error(
        "clone-v2 epoch exceeded max_substeps without consuming input"
    )


__all__ = [
    "ACTION_SINK_BINDINGS_V2",
    "ADAPTER_SCHEMA",
    "BehaviorCloneFrameV2",
    "DELAY_CELL_SELECTION",
    "DECISION_IDENTITY_SCHEMA",
    "EPOCH_SCHEMA",
    "EXECUTABLE_RECONSTRUCTION_ASSUMPTIONS_V2",
    "EXECUTION_SCHEMA",
    "EXACT_VALIDATION_RECEIPT_SCHEMA",
    "HistoricalBehaviorCloneRuntimeV2",
    "HistoricalBehaviorCloneSimulatorAdapterV2",
    "HistoricalBehaviorCloneSimulatorAdapterV2Error",
    "MARK_CONTEXT_COMBINATION",
    "MARK_SMOOTHING_MASS",
    "OBSERVATION_SCHEMA",
    "POLICY_ID_PREFIX",
    "RESULT_BEARING_ACTION_KEYS_V2",
    "RUNTIME_CONTRACT_SCHEMA",
    "TARGET_SMOOTHING_MASS",
    "execute_historical_behavior_clone_decision_v2",
    "exact_validation_receipt_v2",
    "policy_id_for_prototype_v2",
    "predict_mark_distribution_v2",
    "project_delay_outcomes_v2",
    "run_historical_behavior_clone_epoch_v2",
    "runtime_model_binding_v2",
]
