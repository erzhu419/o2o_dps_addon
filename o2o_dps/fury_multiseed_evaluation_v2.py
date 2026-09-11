"""Preregistered multi-seed comparison gate for Fury simulator policies.

Version 1 optimization artifacts used a handful of seeds and a win-count
heuristic.  This module leaves those historical artifacts untouched and adds a
stricter, versioned contract:

* development, selection-validation, and final-confirmation seeds are
  deterministic and disjoint;
* every candidate is paired against every required baseline on the same
  scenario/seed keys;
* one simulator bridge belongs to one worker (concurrency is not statistical
  evidence);
* victory requires a simultaneous one-sided confidence bound above zero in
  the overall, single-target, and multi-target strata, plus a preregistered
  practical mean improvement;
* a missing or nonfaithful required baseline blocks the claim instead of being
  silently removed from the comparison.

The analyzer consumes compact JSONL rollout summaries.  It deliberately does
not launch simulator workers; scheduling and result collection are separate
so that Windows-local and scheduler-backed runs share the same statistical
contract.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import random
import re
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

try:
    # Python 3.12+ executes homogeneous numeric dot products in C.  Keeping
    # this optional (rather than depending on NumPy) lets the same evaluator
    # run on lean scheduler images; the fallback preserves the algorithm on
    # older interpreters, albeit without the accelerated inner loop.
    from math import sumprod as _sumprod
except ImportError:  # pragma: no cover - exercised only on Python < 3.12
    from operator import mul as _mul

    def _sumprod(left: Iterable[float], right: Iterable[float]) -> float:
        return float(sum(map(_mul, left, right)))

from .fury_execution_source_identity_v2 import (
    REQUIRED_PRODUCTION_PATHS,
    SCHEMA as EXECUTION_SOURCE_IDENTITY_SCHEMA,
    FuryExecutionSourceIdentityV2Error,
    build_fury_execution_source_identity_v2,
)
from .fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    DIAGNOSTIC_INTENT,
    FuryPairedRunnerError,
    ROLLOUT_KIND,
    SEED_DERIVATION_ALGORITHM as SIMULATOR_SEED_DERIVATION_ALGORITHM,
    SINGLE_BRIDGE_MODE,
    SYNTHETIC_MODE,
    derive_simulator_seed as _runner_simulator_seed,
    runner_scenario_model_bundle_sha256,
    runner_target_context_bundle_set_sha256,
    reduce_shards,
    validate_reduction_receipt,
    validate_runner_plan,
)
from .fury_selection_admission_v2 import (
    FurySelectionAdmissionV2Error,
    validate_candidate_seal,
    validate_final_corpus_admission_receipt,
    validate_frozen_selection_shortlist,
    validate_selection_receipt,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = (
    PROJECT_ROOT / "configs" / "evaluation" / "fury_multiseed_protocol_v2.json"
)
DEFAULT_PLAN = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_multiseed_protocol_v2.plan.json"
)
DEFAULT_ANALYSIS = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_multiseed_evaluation_v2.json"
)

PROTOCOL_KIND = "fury_multiseed_evaluation_protocol_v2"
PLAN_KIND = "fury_multiseed_evaluation_plan_v2"
ANALYSIS_KIND = "fury_multiseed_evaluation_v2"
SEED_ALGORITHM = "sha256_namespace_phase_counter_u63_v1"
BASELINE_READINESS_RECEIPT_SCHEMA = (
    "fury_baseline_comparison_readiness_receipt/v2"
)
BASELINE_READINESS_EVIDENCE_CLASS = "SELF_REPORTED_DIGEST_INDEX"
BASELINE_READINESS_PROMOTION_STATUS = "NONPROMOTING"
BASELINE_READINESS_FIELDS = (
    "policy_profile",
    "runtime_load",
    "ordered_sink_trace",
    "client_acceptance_trace",
    "server_outcome_trace",
    "full_scenario_adapter",
)
LEGACY_FINAL_CORPUS_ADMISSION_SCHEMA = "fury_final_corpus_admission_receipt/v1"
REQUIRED_PHASES = (
    "development",
    "selection_validation",
    "final_confirmation",
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
ROLLOUT_REQUIRED_FIELDS = (
    "schema_version",
    "kind",
    "plan_sha256",
    "group_id",
    "protocol_id",
    "protocol_sha256",
    "corpus_manifest_sha256",
    "runner_inputs_sha256",
    "corpus_binding_sha256",
    "phase",
    "plan_intent",
    "shard_index",
    "instance_id",
    "component_id",
    "scenario_id",
    "scenario_contract_sha256",
    "scenario_model_sha256",
    "target_context_bundle_sha256",
    "corpus_entry_sha256",
    "source_scenario_sha256",
    "catalog_sha256",
    "stratum",
    "master_seed",
    "seed",
    "simulator_seed",
    "request_sha256",
    "policy_id",
    "policy_source_sha256",
    "policy_adapter_sha256",
    "policy_profile_sha256",
    "bridge_sha256",
    "execution_bundle_sha256",
    "execution_mode",
    "full_policy_rollout_sha256",
    "scenario_weight",
    "damage",
    "elapsed_ms",
    "horizon_ms",
    "dps",
    "completion_criterion_met",
    "contract_complete",
    "evaluation_eligible",
    "omitted_lane_count",
    "fatal_error_count",
    "nonfaithful_reason_counts",
    "end_state_sha256",
    "row_sha256",
)


class FuryMultiseedProtocolError(RuntimeError):
    """A protocol, rollout, or statistical-gate invariant was violated."""


@dataclass(frozen=True)
class NormalizedRollout:
    plan_sha256: str
    group_id: str
    protocol_id: str
    protocol_sha256: str
    corpus_manifest_sha256: str
    runner_inputs_sha256: str
    corpus_binding_sha256: str
    plan_intent: str
    scenario_contract_sha256: str
    scenario_model_sha256: str
    target_context_bundle_sha256: str
    corpus_entry_sha256: str
    source_scenario_sha256: str
    catalog_sha256: str
    phase: str
    instance_id: str
    component_id: str
    scenario_id: str
    stratum: str
    master_seed: int
    simulator_seed: int
    request_sha256: str
    policy_id: str
    policy_source_sha256: str
    policy_adapter_sha256: str
    policy_profile_sha256: str
    bridge_sha256: str
    execution_bundle_sha256: str
    execution_mode: str
    full_policy_rollout_sha256: str | None
    scenario_weight: float
    damage: float
    elapsed_ms: int
    horizon_ms: int
    dps: float
    completion_criterion_met: bool
    contract_complete: bool
    evaluation_eligible: bool
    omitted_lane_count: int
    fatal_error_count: int
    nonfaithful_reason_counts: tuple[tuple[str, int], ...]
    end_state_sha256: str

    @property
    def pair_key(self) -> tuple[str, str, int]:
        return (self.instance_id, self.scenario_id, self.master_seed)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize strict JSON deterministically for task and seed receipts."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


CORPUS_BINDING_CORE_FIELDS = (
    "corpus_manifest_sha256",
    "runner_inputs_sha256",
    "runner_scenario_bundle_sha256",
    "scenario_model_bundle_sha256",
    "target_context_bundle_set_sha256",
    "source_instance_provenance_sha256",
    "runner_scenario_count",
)


def corpus_binding_sha256(binding: Mapping[str, Any]) -> str:
    """Hash the immutable runner-facing corpus core, excluding final evidence."""

    core: JSONMap = {}
    for field in CORPUS_BINDING_CORE_FIELDS:
        if field not in binding:
            raise FuryMultiseedProtocolError(
                f"frozen corpus binding lacks {field}"
            )
        core[field] = binding[field]
    return sha256_json(core)


SELECTION_METRIC_ID = (
    "minimum_crossed_seed_component_lower_bound_across_required_cells_v1"
)
SELECTION_METRIC_DEFINITION: JSONMap = {
    "metric_id": SELECTION_METRIC_ID,
    "direction": "maximize",
    "input": "complete typed selection-validation analysis artifact",
    "value": (
        "minimum crossed_seed_component_lower_bound_pct over the exact "
        "required-baseline by required-stratum family"
    ),
    "ties": "candidate_identity_sha256 ascending",
    "blocked_analysis_allowed": False,
    "post_hoc_baseline_or_stratum_removal_allowed": False,
}
SELECTION_METRIC_DEFINITION_SHA256 = sha256_json(
    SELECTION_METRIC_DEFINITION
)
SELECTION_EVIDENCE_BUNDLE_SCHEMA = "fury_selection_evidence_bundle/v2"
SELECTION_PHYSICAL_REPLAY_RECEIPT_SCHEMA = (
    "fury_selection_physical_replay_receipt/v2"
)


def selection_design_sha256(
    protocol: Mapping[str, Any], shortlist: Mapping[str, Any]
) -> str:
    """Hash the pre-result selection design without post-selection artifacts."""

    selection_binding = protocol["corpus_contract"]["phase_corpus_bindings"][
        "selection_validation"
    ]
    baseline_rows = [
        {
            "policy_id": row["policy_id"],
            "source_bundle_sha256": row["source_bundle_sha256"],
            "policy_adapter_sha256": row["policy_adapter_sha256"],
            "policy_profile_sha256": row["policy_profile_sha256"],
            "comparison_eligible": row["comparison_eligible"],
            "comparison_eligibility_receipt": row.get(
                "comparison_eligibility_receipt"
            ),
        }
        for row in protocol["baseline_contract"]["required_baselines"]
    ]
    core = {
        "schema": "fury_selection_design/v2",
        "shortlist_sha256": shortlist["shortlist_sha256"],
        "selection_seed_contract": protocol["seed_contract"]["phases"][
            "selection_validation"
        ],
        "selection_corpus_binding": {
            field: selection_binding.get(field)
            for field in (*CORPUS_BINDING_CORE_FIELDS, "corpus_binding_sha256")
        },
        "evaluation_contract": protocol["evaluation_contract"],
        "required_baselines": baseline_rows,
        "execution_implementation_identity": protocol["execution_contract"][
            "implementation_identity"
        ],
        "required_runner_execution_mode": protocol["execution_contract"][
            "required_runner_execution_mode"
        ],
        "selection_metric_definition": SELECTION_METRIC_DEFINITION,
    }
    return sha256_json(core)


def build_baseline_comparison_readiness_receipt(
    baseline: Mapping[str, Any],
    *,
    runtime_snapshot_sha256: str,
    execution_bundle_identity: Mapping[str, Any],
    evidence_sha256_by_field: Mapping[str, str],
) -> JSONMap:
    """Build a non-promoting index of claimed baseline evidence digests.

    A digest proves only that a caller supplied a string with SHA-256 syntax.
    It does not prove that a runtime load, client acceptance, server outcome, or
    full-scenario adapter artifact physically exists or satisfies its typed
    contract.  This legacy-shaped builder is retained for diagnostics and test
    fixtures, but its output can never promote a baseline into a scientific
    comparison.
    """

    evidence = dict(evidence_sha256_by_field)
    if set(evidence) != set(BASELINE_READINESS_FIELDS):
        raise FuryMultiseedProtocolError(
            "baseline readiness evidence must exactly cover every readiness field"
        )
    for field, digest in evidence.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"baseline readiness evidence {field} must be lowercase SHA-256"
            )
    core: JSONMap = {
        "schema": BASELINE_READINESS_RECEIPT_SCHEMA,
        "evidence_class": BASELINE_READINESS_EVIDENCE_CLASS,
        "promotion_status": BASELINE_READINESS_PROMOTION_STATUS,
        "comparison_eligible": False,
        "policy_id": baseline.get("policy_id"),
        "source_bundle_sha256": baseline.get("source_bundle_sha256"),
        "policy_adapter_sha256": baseline.get("policy_adapter_sha256"),
        "policy_profile_sha256": baseline.get("policy_profile_sha256"),
        "runtime_snapshot_sha256": runtime_snapshot_sha256,
        "execution_bundle_sha256": sha256_json(dict(execution_bundle_identity)),
        "readiness": {field: True for field in BASELINE_READINESS_FIELDS},
        "evidence_sha256_by_field": {
            field: evidence[field] for field in BASELINE_READINESS_FIELDS
        },
    }
    return {**core, "receipt_sha256": sha256_json(core)}


def _validate_baseline_comparison_readiness_receipt(
    baseline: Mapping[str, Any],
    receipt: Any,
    *,
    runtime_snapshot_sha256: str,
    execution_bundle_identity: Mapping[str, Any],
    allow_nonpromoting_test_fixture: bool = False,
) -> str:
    row = _mapping(receipt, "baseline comparison eligibility receipt")
    expected_fields = {
        "schema",
        "evidence_class",
        "promotion_status",
        "comparison_eligible",
        "policy_id",
        "source_bundle_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
        "runtime_snapshot_sha256",
        "execution_bundle_sha256",
        "readiness",
        "evidence_sha256_by_field",
        "receipt_sha256",
    }
    if set(row) != expected_fields:
        raise FuryMultiseedProtocolError(
            "baseline comparison eligibility receipt fields mismatch"
        )
    expected_receipt = build_baseline_comparison_readiness_receipt(
        baseline,
        runtime_snapshot_sha256=runtime_snapshot_sha256,
        execution_bundle_identity=execution_bundle_identity,
        evidence_sha256_by_field=_mapping(
            row.get("evidence_sha256_by_field"),
            "baseline readiness evidence",
        ),
    )
    if dict(row) != expected_receipt:
        raise FuryMultiseedProtocolError(
            "baseline comparison eligibility receipt is inconsistent or not content-addressed"
        )
    if (
        row.get("evidence_class") != BASELINE_READINESS_EVIDENCE_CLASS
        or row.get("promotion_status") != BASELINE_READINESS_PROMOTION_STATUS
        or row.get("comparison_eligible") is not False
    ):
        raise FuryMultiseedProtocolError(
            "baseline readiness receipt attempted to widen its non-promoting authority"
        )
    if not allow_nonpromoting_test_fixture:
        raise FuryMultiseedProtocolError(
            "self-reported baseline readiness digest receipt is explicitly "
            "non-promoting; comparison_eligible=true requires a typed physical "
            "production evidence verifier that is not implemented"
        )
    return "NONPROMOTING_TEST_FIXTURE"


def _utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryMultiseedProtocolError(f"{label} must be a nonempty timestamp")
    rendered = value.strip()
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as error:
        raise FuryMultiseedProtocolError(f"{label} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FuryMultiseedProtocolError(f"{label} must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _legacy_normalized_final_instance_v1(value: Any, index: int) -> JSONMap:
    row = _mapping(value, f"final selected_instances[{index}]")
    expected = {
        "instance_id",
        "uploaded_at",
        "complete_nine_bosses_plus_all_activity",
        "instance_metadata_sha256",
        "ranking_records_sha256",
        "all_activity_stream_sha256",
        "component_ids",
    }
    if set(row) != expected:
        raise FuryMultiseedProtocolError(
            f"final selected_instances[{index}] fields mismatch"
        )
    instance_id = row.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise FuryMultiseedProtocolError(
            f"final selected_instances[{index}].instance_id must be nonempty"
        )
    if row.get("complete_nine_bosses_plus_all_activity") is not True:
        raise FuryMultiseedProtocolError(
            "every final instance must attest nine bosses plus All Activity"
        )
    digests: dict[str, str] = {}
    for field in (
        "instance_metadata_sha256",
        "ranking_records_sha256",
        "all_activity_stream_sha256",
    ):
        digest = row.get(field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"final selected_instances[{index}].{field} must be lowercase SHA-256"
            )
        digests[field] = digest
    components = _sequence(
        row.get("component_ids"),
        f"final selected_instances[{index}].component_ids",
    )
    if (
        not components
        or any(not isinstance(item, str) or not item.strip() for item in components)
        or list(components) != sorted(set(components))
    ):
        raise FuryMultiseedProtocolError(
            "final instance component_ids must be a nonempty sorted unique list"
        )
    return {
        "instance_id": instance_id.strip(),
        "uploaded_at": _utc_timestamp(
            row.get("uploaded_at"),
            f"final selected_instances[{index}].uploaded_at",
        ),
        "complete_nine_bosses_plus_all_activity": True,
        **digests,
        "component_ids": list(components),
    }


def _build_legacy_final_corpus_admission_receipt_v1(
    *,
    candidate_contract: Mapping[str, Any],
    candidate_frozen_at: str,
    development_completed_instance_ids_sha256: str,
    development_instance_id_sha256s: Sequence[str],
    selected_instances: Sequence[Mapping[str, Any]],
    selection_query_sha256: str,
    discovery_snapshot_sha256: str,
    corpus_manifest_sha256: str,
    runner_inputs_sha256: str,
    runner_scenario_bundle_sha256: str,
) -> JSONMap:
    """Build the independently content-addressed future-corpus admission body."""

    if candidate_contract.get("status") != "FROZEN":
        raise FuryMultiseedProtocolError(
            "final admission requires a frozen candidate"
        )
    candidate_identity: JSONMap = {}
    for field in (
        "policy_id",
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
    ):
        value = candidate_contract.get(field)
        if field == "policy_id":
            if not isinstance(value, str) or not value.strip():
                raise FuryMultiseedProtocolError(
                    "final admission candidate policy_id must be nonempty"
                )
            candidate_identity[field] = value.strip()
        else:
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise FuryMultiseedProtocolError(
                    f"final admission candidate {field} must be lowercase SHA-256"
                )
            candidate_identity[field] = value
    exclusion_digest = development_completed_instance_ids_sha256
    if not isinstance(exclusion_digest, str) or not _SHA256.fullmatch(
        exclusion_digest
    ):
        raise FuryMultiseedProtocolError(
            "development completed-instance digest must be lowercase SHA-256"
        )
    exclusion_hashes = list(development_instance_id_sha256s)
    if (
        not exclusion_hashes
        or exclusion_hashes != sorted(set(exclusion_hashes))
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in exclusion_hashes)
    ):
        raise FuryMultiseedProtocolError(
            "development instance ID hashes must be a nonempty sorted unique list"
        )
    normalized_instances = [
        _legacy_normalized_final_instance_v1(row, index)
        for index, row in enumerate(selected_instances)
    ]
    if not normalized_instances:
        raise FuryMultiseedProtocolError(
            "final admission selected_instances must not be empty"
        )
    if normalized_instances != sorted(
        normalized_instances,
        key=lambda row: (str(row["uploaded_at"]), str(row["instance_id"])),
    ):
        raise FuryMultiseedProtocolError(
            "final instances must be selected in uploaded_at then instance_id order"
        )
    selected_ids = [str(row["instance_id"]) for row in normalized_instances]
    if len(set(selected_ids)) != len(selected_ids):
        raise FuryMultiseedProtocolError("final instance IDs must be unique")
    selected_hashes = sorted(
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in selected_ids
    )
    overlap = set(selected_hashes).intersection(exclusion_hashes)
    if overlap:
        raise FuryMultiseedProtocolError(
            "final instance IDs overlap the development exclusion set"
        )
    frozen_at = _utc_timestamp(candidate_frozen_at, "candidate_frozen_at")
    if frozen_at >= min(str(row["uploaded_at"]) for row in normalized_instances):
        raise FuryMultiseedProtocolError(
            "candidate must be frozen before the first selected instance upload"
        )
    named_digests = {
        "selection_query_sha256": selection_query_sha256,
        "discovery_snapshot_sha256": discovery_snapshot_sha256,
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "runner_inputs_sha256": runner_inputs_sha256,
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256,
    }
    for field, digest in named_digests.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"final admission {field} must be lowercase SHA-256"
            )
    core: JSONMap = {
        "schema": LEGACY_FINAL_CORPUS_ADMISSION_SCHEMA,
        "candidate_identity": candidate_identity,
        "candidate_frozen_at": frozen_at,
        "development_exclusion": {
            "completed_instance_ids_sha256": exclusion_digest,
            "instance_id_sha256s": exclusion_hashes,
            "instance_id_sha256s_sha256": sha256_json(exclusion_hashes),
        },
        "selection_contract": {
            "order": "uploaded_at_then_instance_id",
            "selection_query_sha256": selection_query_sha256,
            "discovery_snapshot_sha256": discovery_snapshot_sha256,
            "selected_instances": normalized_instances,
        },
        "corpus_binding": {
            "corpus_manifest_sha256": corpus_manifest_sha256,
            "runner_inputs_sha256": runner_inputs_sha256,
            "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256,
        },
    }
    return {**core, "receipt_sha256": sha256_json(core)}


def _validate_legacy_final_corpus_admission_receipt_v1(
    receipt: Any,
    *,
    candidate_contract: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
    phase_binding: Mapping[str, Any],
) -> JSONMap:
    row = _mapping(receipt, "final corpus admission receipt")
    expected_fields = {
        "schema",
        "candidate_identity",
        "candidate_frozen_at",
        "development_exclusion",
        "selection_contract",
        "corpus_binding",
        "receipt_sha256",
    }
    if set(row) != expected_fields:
        raise FuryMultiseedProtocolError(
            "final corpus admission receipt fields mismatch"
        )
    selection = _mapping(
        row.get("selection_contract"), "final admission selection_contract"
    )
    exclusion = _mapping(
        row.get("development_exclusion"), "final admission development_exclusion"
    )
    binding = _mapping(
        row.get("corpus_binding"), "final admission corpus_binding"
    )
    expected = _build_legacy_final_corpus_admission_receipt_v1(
        candidate_contract=candidate_contract,
        candidate_frozen_at=row.get("candidate_frozen_at"),
        development_completed_instance_ids_sha256=current_snapshot.get(
            "completed_instance_ids_sha256"
        ),
        development_instance_id_sha256s=_sequence(
            current_snapshot.get("completed_instance_id_sha256s"),
            "current snapshot completed_instance_id_sha256s",
        ),
        selected_instances=_sequence(
            selection.get("selected_instances"),
            "final admission selected_instances",
        ),
        selection_query_sha256=selection.get("selection_query_sha256"),
        discovery_snapshot_sha256=selection.get("discovery_snapshot_sha256"),
        corpus_manifest_sha256=binding.get("corpus_manifest_sha256"),
        runner_inputs_sha256=binding.get("runner_inputs_sha256"),
        runner_scenario_bundle_sha256=binding.get(
            "runner_scenario_bundle_sha256"
        ),
    )
    if dict(row) != expected:
        raise FuryMultiseedProtocolError(
            "final corpus admission receipt is inconsistent or not content-addressed"
        )
    expected_binding = {
        "corpus_manifest_sha256": phase_binding.get("corpus_manifest_sha256"),
        "runner_inputs_sha256": phase_binding.get("runner_inputs_sha256"),
        "runner_scenario_bundle_sha256": phase_binding.get(
            "runner_scenario_bundle_sha256"
        ),
    }
    if binding != expected_binding:
        raise FuryMultiseedProtocolError(
            "final admission corpus binding differs from the phase binding"
        )
    if exclusion.get("instance_id_sha256s_sha256") != current_snapshot.get(
        "completed_instance_id_sha256s_sha256"
    ):
        raise FuryMultiseedProtocolError(
            "final admission development exclusion list identity mismatch"
        )
    return expected


def derive_seed_set(
    namespace: str,
    phase: str,
    count: int,
    *,
    counter_start: int = 0,
) -> tuple[int, ...]:
    """Derive stable positive int64 simulator seeds without global RNG state."""

    if not isinstance(namespace, str) or not namespace.strip():
        raise TypeError("seed namespace must be a nonempty string")
    if not isinstance(phase, str) or not phase.strip():
        raise TypeError("seed phase must be a nonempty string")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise TypeError("seed count must be a positive integer")
    if (
        isinstance(counter_start, bool)
        or not isinstance(counter_start, int)
        or counter_start < 0
    ):
        raise TypeError("seed counter_start must be a nonnegative integer")

    values: list[int] = []
    seen: set[int] = set()
    for counter in range(counter_start, counter_start + count):
        payload = f"{namespace}\0{phase}\0{counter}".encode("utf-8")
        value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        value &= (1 << 63) - 1
        if value == 0 or value in seen:
            raise FuryMultiseedProtocolError(
                "seed derivation produced zero or a collision; change the namespace"
            )
        values.append(value)
        seen.add(value)
    return tuple(values)


def derive_simulator_seed(
    namespace: str,
    master_seed: int,
    request_sha256: str,
) -> int:
    """Use the runner's sole canonical request-specific seed derivation."""

    try:
        return _runner_simulator_seed(
            master_seed,
            request_sha256,
            namespace=namespace,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise FuryMultiseedProtocolError(
            f"invalid request-specific simulator seed input: {exc}"
        ) from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryMultiseedProtocolError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FuryMultiseedProtocolError(f"{label} must be a list")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryMultiseedProtocolError(f"{label} must be a positive integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryMultiseedProtocolError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise FuryMultiseedProtocolError(f"{label} must be finite")
    return result


def _trusted_anchor_hashes(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise FuryMultiseedProtocolError(
            "trusted external anchor seals must be an iterable of SHA-256 strings"
        )
    normalized = tuple(values)
    if len(normalized) != len(set(normalized)):
        raise FuryMultiseedProtocolError(
            "trusted external anchor seal SHA-256 values must be unique"
        )
    for digest in normalized:
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                "trusted external anchor seals must be lowercase SHA-256 values"
            )
    return tuple(sorted(normalized))


def _normalize_candidate_identity(value: Any, label: str) -> JSONMap:
    row = _mapping(value, label)
    expected = {
        "policy_id",
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
    }
    if set(row) != expected:
        raise FuryMultiseedProtocolError(f"{label} fields mismatch")
    policy_id = row.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise FuryMultiseedProtocolError(f"{label}.policy_id must be nonempty")
    normalized: JSONMap = {"policy_id": policy_id.strip()}
    for field in (
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
    ):
        digest = row.get(field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"{label}.{field} must be lowercase SHA-256"
            )
        normalized[field] = digest
    return normalized


def _validate_development_candidate_registry(value: Any) -> JSONMap:
    registry = _mapping(value, "development_candidate_registry")
    expected = {"status", "candidates", "registry_sha256", "blocker"}
    if set(registry) != expected:
        raise FuryMultiseedProtocolError(
            "development_candidate_registry fields mismatch"
        )
    status = registry.get("status")
    if status not in {"NOT_FROZEN", "FROZEN"}:
        raise FuryMultiseedProtocolError(
            "development_candidate_registry.status must be NOT_FROZEN or FROZEN"
        )
    candidates = [
        _normalize_candidate_identity(item, f"development candidates[{index}]")
        for index, item in enumerate(
            _sequence(registry.get("candidates"), "development candidates")
        )
    ]
    if status == "NOT_FROZEN":
        if candidates or registry.get("registry_sha256") is not None:
            raise FuryMultiseedProtocolError(
                "an unfrozen development registry must be empty and unhashed"
            )
        return dict(registry)
    if not candidates:
        raise FuryMultiseedProtocolError(
            "a frozen development registry must contain candidates"
        )
    candidates.sort(key=lambda row: str(row["policy_id"]))
    if len({row["policy_id"] for row in candidates}) != len(candidates):
        raise FuryMultiseedProtocolError(
            "development candidate policy IDs must be unique"
        )
    expected_digest = sha256_json(candidates)
    if registry.get("registry_sha256") != expected_digest:
        raise FuryMultiseedProtocolError(
            "development candidate registry SHA-256 mismatch"
        )
    return {**dict(registry), "candidates": candidates}


def _validate_selection_contract(
    raw_contract: Any,
    *,
    candidate_contract: Mapping[str, Any],
    trusted_external_anchor_seal_sha256s: Sequence[str],
) -> JSONMap:
    contract = _mapping(raw_contract, "selection_contract")
    expected_fields = {
        "status",
        "frozen_shortlist",
        "selection_evidence_bundle",
        "selection_receipt",
        "candidate_seal",
        "blocker",
    }
    if set(contract) != expected_fields:
        raise FuryMultiseedProtocolError(
            "selection_contract fields mismatch"
        )
    status = contract.get("status")
    if status not in {"NOT_FROZEN", "SHORTLIST_FROZEN", "CANDIDATE_SEALED"}:
        raise FuryMultiseedProtocolError(
            "selection_contract.status must be NOT_FROZEN, SHORTLIST_FROZEN, "
            "or CANDIDATE_SEALED"
        )
    shortlist_raw = contract.get("frozen_shortlist")
    evidence_raw = contract.get("selection_evidence_bundle")
    receipt_raw = contract.get("selection_receipt")
    seal_raw = contract.get("candidate_seal")
    if status == "NOT_FROZEN":
        if any(
            item is not None
            for item in (shortlist_raw, evidence_raw, receipt_raw, seal_raw)
        ):
            raise FuryMultiseedProtocolError(
                "an unfrozen selection contract cannot carry selection artifacts"
            )
        if candidate_contract.get("status") != "NOT_FROZEN":
            raise FuryMultiseedProtocolError(
                "a frozen candidate requires a sealed selection contract"
            )
        return dict(contract)
    try:
        shortlist = validate_frozen_selection_shortlist(shortlist_raw)
    except FurySelectionAdmissionV2Error as error:
        raise FuryMultiseedProtocolError(
            f"invalid frozen selection shortlist: {error}"
        ) from error
    if status == "SHORTLIST_FROZEN":
        if evidence_raw is not None or receipt_raw is not None or seal_raw is not None:
            raise FuryMultiseedProtocolError(
                "a shortlist-only contract cannot carry a receipt or candidate seal"
            )
        if candidate_contract.get("status") != "NOT_FROZEN":
            raise FuryMultiseedProtocolError(
                "the candidate cannot be frozen before the one-time selection receipt"
            )
        return {**dict(contract), "frozen_shortlist": shortlist}
    try:
        if not isinstance(evidence_raw, Mapping):
            raise FurySelectionAdmissionV2Error(
                "sealed selection lacks a typed selection evidence bundle"
            )
        receipt = validate_selection_receipt(receipt_raw)
        seal = validate_candidate_seal(
            seal_raw,
            trusted_external_anchor_seal_sha256s=(
                trusted_external_anchor_seal_sha256s
            ),
        )
    except FurySelectionAdmissionV2Error as error:
        raise FuryMultiseedProtocolError(
            f"invalid sealed candidate selection: {error}"
        ) from error
    if receipt["frozen_shortlist"] != shortlist:
        raise FuryMultiseedProtocolError(
            "selection receipt does not use the protocol's frozen shortlist"
        )
    if seal["selection_receipt"] != receipt:
        raise FuryMultiseedProtocolError(
            "candidate seal does not bind the protocol's selection receipt"
        )
    if candidate_contract.get("status") != "FROZEN":
        raise FuryMultiseedProtocolError(
            "a sealed selection must materialize one frozen candidate"
        )
    identity = seal["candidate_identity"]
    for field in (
        "policy_id",
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
    ):
        if candidate_contract.get(field) != identity[field]:
            raise FuryMultiseedProtocolError(
                f"candidate_contract.{field} differs from the candidate seal"
            )
    expected_receipts = {
        "selection_receipt_sha256": receipt["selection_receipt_sha256"],
        "candidate_seal_sha256": seal["candidate_seal_sha256"],
        "external_anchor_seal_sha256": seal["external_anchor_seal"][
            "external_anchor_seal_sha256"
        ],
        "frozen_at": seal["frozen_at"],
    }
    for field, expected in expected_receipts.items():
        if candidate_contract.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"candidate_contract.{field} differs from the sealed selection"
            )
    return {
        **dict(contract),
        "frozen_shortlist": shortlist,
        "selection_receipt": receipt,
        "candidate_seal": seal,
    }


def _validate_final_admission_against_protocol(
    raw_receipt: Any,
    *,
    discovery_capture_manifest_path: str | Path | None,
    selection_contract: Mapping[str, Any],
    trusted_external_anchor_seal_sha256s: Sequence[str],
    current_snapshot: Mapping[str, Any],
    phase_binding: Mapping[str, Any],
    final_seed_contract: Mapping[str, Any],
    source_closure_sha256: str,
    implementation_identity: Mapping[str, Any],
    execution_contract: Mapping[str, Any],
    required_take_first_n: int,
) -> JSONMap:
    try:
        receipt = validate_final_corpus_admission_receipt(
            raw_receipt,
            trusted_external_anchor_seal_sha256s=(
                trusted_external_anchor_seal_sha256s
            ),
            discovery_capture_manifest_path=discovery_capture_manifest_path,
        )
    except FurySelectionAdmissionV2Error as error:
        raise FuryMultiseedProtocolError(
            f"invalid final-corpus admission receipt: {error}"
        ) from error
    if selection_contract.get("status") != "CANDIDATE_SEALED":
        raise FuryMultiseedProtocolError(
            "final admission requires the protocol's externally sealed candidate"
        )
    if receipt["candidate_seal"] != selection_contract.get("candidate_seal"):
        raise FuryMultiseedProtocolError(
            "final admission candidate seal differs from the protocol selection"
        )
    replay = receipt["selection_replay"]
    discovery_query = receipt["discovery_query"]
    if (
        discovery_query.get("take_first_n") != required_take_first_n
        or replay.get("take_first_n") != required_take_first_n
        or len(replay.get("selected_instances", [])) != required_take_first_n
    ):
        raise FuryMultiseedProtocolError(
            "final admission must select exactly the protocol's required_take_first_n"
        )
    exclusion = receipt["development_exclusion_snapshot"]
    expected_exclusion = {
        "development_instance_count": current_snapshot.get(
            "completed_instance_count"
        ),
        "development_instance_id_sha256s": current_snapshot.get(
            "completed_instance_id_sha256s"
        ),
        "development_guild_player_component_sha256s": current_snapshot.get(
            "completed_guild_player_component_sha256s"
        ),
    }
    for field, expected in expected_exclusion.items():
        if exclusion.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"final admission development exclusion {field} mismatch"
            )
    identities = receipt["final_input_identities"]
    seed_identity = identities["seed"]
    if (
        seed_identity.get("seed_count") != final_seed_contract.get("count")
        or seed_identity.get("seed_list_sha256")
        != final_seed_contract.get("seed_list_sha256")
    ):
        raise FuryMultiseedProtocolError(
            "final admission seed identity differs from the protocol"
        )
    corpus_identity = identities["corpus"]
    expected_corpus = {
        "corpus_manifest_sha256": phase_binding.get(
            "corpus_manifest_sha256"
        ),
        "corpus_binding_sha256": phase_binding.get("corpus_binding_sha256"),
        "source_instance_provenance_sha256": phase_binding.get(
            "source_instance_provenance_sha256"
        ),
        "scenario_count": phase_binding.get("runner_scenario_count"),
    }
    for field, expected in expected_corpus.items():
        if corpus_identity.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"final admission corpus identity {field} mismatch"
            )
    runner_identity = identities["runner"]
    expected_runner = {
        "runner_source_identity_sha256": source_closure_sha256,
        "runner_inputs_sha256": phase_binding.get("runner_inputs_sha256"),
        "runner_scenario_bundle_sha256": phase_binding.get(
            "runner_scenario_bundle_sha256"
        ),
        "execution_bundle_sha256": sha256_json(dict(implementation_identity)),
    }
    for field, expected in expected_runner.items():
        if runner_identity.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"final admission runner identity {field} mismatch"
            )
    allowed_bridges = {
        execution_contract["windows_local"].get("bridge_sha256"),
        execution_contract["scheduler_hpc"].get("linux_bridge_sha256"),
    }
    allowed_bridges.discard(None)
    if runner_identity.get("bridge_sha256") not in allowed_bridges:
        raise FuryMultiseedProtocolError(
            "final admission bridge identity is not pinned by the protocol"
        )
    return receipt


def _physical_json_path(value: Any, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise FuryMultiseedProtocolError(f"{label} must be a physical JSON path")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FuryMultiseedProtocolError(f"{label} does not exist: {path}")
    return path


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise FuryMultiseedProtocolError(
            f"could not read {label} for physical content addressing: {error}"
        ) from error


def _validate_selection_physical_replay_receipts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    expected_candidates: Mapping[str, Mapping[str, Any]],
    analyses_by_candidate: Mapping[str, Mapping[str, Any]],
) -> list[JSONMap]:
    expected_fields = {
        "schema",
        "candidate_id",
        "candidate_identity_sha256",
        "protocol_sha256",
        "shortlist_protocol_file_sha256",
        "runner_plan_sha256",
        "runner_plan_file_sha256",
        "analysis_file_sha256",
        "shard_manifest_file_sha256s",
        "shard_manifest_file_set_sha256",
        "reduction_sha256",
        "reduction_file_sha256",
        "reduction_receipt_sha256",
        "canonical_rollout_set_sha256",
        "shard_physical_evidence_sha256",
        "full_policy_artifact_set_sha256",
        "physical_artifact_validation",
        "replayed_analysis_sha256",
        "physical_replay_receipt_sha256",
    }
    normalized: list[JSONMap] = []
    seen: set[str] = set()
    for index, raw in enumerate(receipts):
        row = dict(_mapping(raw, f"selection physical replay receipts[{index}]"))
        if set(row) != expected_fields:
            raise FuryMultiseedProtocolError(
                "selection physical replay receipt fields mismatch"
            )
        digest = row.pop("physical_replay_receipt_sha256", None)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                "selection physical replay receipt lacks a SHA-256 identity"
            )
        if digest != sha256_json(row):
            raise FuryMultiseedProtocolError(
                "selection physical replay receipt is not content-addressed"
            )
        row["physical_replay_receipt_sha256"] = digest
        candidate_id = str(row.get("candidate_id"))
        candidate = expected_candidates.get(candidate_id)
        analysis = analyses_by_candidate.get(candidate_id)
        if candidate is None or analysis is None or candidate_id in seen:
            raise FuryMultiseedProtocolError(
                "selection physical replay receipts contain an unknown or duplicate candidate"
            )
        seen.add(candidate_id)
        if row.get("schema") != SELECTION_PHYSICAL_REPLAY_RECEIPT_SCHEMA:
            raise FuryMultiseedProtocolError(
                "selection physical replay receipt schema mismatch"
            )
        if row.get("candidate_identity_sha256") != candidate.get(
            "candidate_identity_sha256"
        ):
            raise FuryMultiseedProtocolError(
                "selection physical replay candidate identity mismatch"
            )
        linked = {
            "protocol_sha256": analysis.get("protocol_sha256"),
            "runner_plan_sha256": analysis.get("runner_plan_sha256"),
            "reduction_receipt_sha256": analysis.get(
                "reduction_receipt_sha256"
            ),
            "canonical_rollout_set_sha256": analysis.get(
                "canonical_rollout_set_sha256"
            ),
            "replayed_analysis_sha256": analysis.get("analysis_sha256"),
        }
        for field, expected in linked.items():
            if row.get(field) != expected:
                raise FuryMultiseedProtocolError(
                    f"selection physical replay {field} differs from its analysis"
                )
        for field in (
            "runner_plan_file_sha256",
            "analysis_file_sha256",
            "shortlist_protocol_file_sha256",
            "reduction_sha256",
            "reduction_file_sha256",
            "shard_physical_evidence_sha256",
            "full_policy_artifact_set_sha256",
        ):
            value = row.get(field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise FuryMultiseedProtocolError(
                    f"selection physical replay {field} must be SHA-256"
                )
        manifest_hashes = _sequence(
            row.get("shard_manifest_file_sha256s"),
            "selection physical replay shard manifest hashes",
        )
        if not manifest_hashes or any(
            not isinstance(value, str) or not _SHA256.fullmatch(value)
            for value in manifest_hashes
        ):
            raise FuryMultiseedProtocolError(
                "selection physical replay requires content-addressed shard manifests"
            )
        if list(manifest_hashes) != sorted(manifest_hashes):
            raise FuryMultiseedProtocolError(
                "selection physical replay shard manifest hashes must be sorted"
            )
        if row.get("shard_manifest_file_set_sha256") != sha256_json(
            list(manifest_hashes)
        ):
            raise FuryMultiseedProtocolError(
                "selection physical replay manifest set is not content-addressed"
            )
        if (
            row.get("physical_artifact_validation")
            != "VALIDATED_PHYSICAL_SHARD_MANIFESTS_AND_ARTIFACTS"
        ):
            raise FuryMultiseedProtocolError(
                "selection replay did not validate physical shard manifests and artifacts"
            )
        normalized.append(row)
    if seen != set(expected_candidates):
        raise FuryMultiseedProtocolError(
            "selection physical replay receipts do not exactly cover the frozen shortlist"
        )
    normalized.sort(key=lambda row: str(row["candidate_identity_sha256"]))
    return normalized


def build_selection_evidence_bundle(
    protocol: Mapping[str, Any],
    analyses: Sequence[Mapping[str, Any]],
    *,
    materialized_protocol: Mapping[str, Any],
    physical_replays: Sequence[Mapping[str, Any]],
    _allow_nonpromoting_test_selection_replay_authority: bool = False,
) -> JSONMap:
    """Replay physical selection artifacts before binding the selection metric.

    Each replay input must contain exactly ``candidate_id``,
    ``shortlist_protocol_path``, ``runner_plan_path``,
    ``shard_manifest_paths``, ``reduction_path``, and ``analysis_path``.
    The persisted reduction and claimed analysis are treated as untrusted: the
    runner validates every referenced full-policy artifact, rebuilds the
    reduction from the manifests, and the analyzer recomputes the complete
    selection result before any metric row is emitted.
    """

    selection = _mapping(protocol.get("selection_contract"), "selection_contract")
    if selection.get("status") != "SHORTLIST_FROZEN":
        raise FuryMultiseedProtocolError(
            "selection evidence can only be built from a SHORTLIST_FROZEN protocol"
        )
    shortlist = validate_frozen_selection_shortlist(
        selection.get("frozen_shortlist")
    )
    expected_candidates = {
        str(row["policy_id"]): dict(row) for row in shortlist["candidates"]
    }
    analyses_by_candidate: dict[str, JSONMap] = {}
    for index, raw in enumerate(analyses):
        analysis = dict(_mapping(raw, f"selection analyses[{index}]"))
        candidate_id = analysis.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in analyses_by_candidate:
            raise FuryMultiseedProtocolError(
                "selection analyses contain a missing or duplicate candidate"
            )
        analyses_by_candidate[candidate_id] = analysis
    if set(analyses_by_candidate) != set(expected_candidates):
        raise FuryMultiseedProtocolError(
            "selection analyses do not exactly cover the frozen shortlist"
        )
    if not isinstance(physical_replays, Sequence) or isinstance(
        physical_replays, (str, bytes, bytearray)
    ):
        raise FuryMultiseedProtocolError(
            "selection evidence requires physical replay inputs for every candidate"
        )
    if len(physical_replays) != len(expected_candidates):
        raise FuryMultiseedProtocolError(
            "selection evidence requires exactly one physical replay per candidate"
        )

    # A selection run is bound to the externally materialized shortlist-stage
    # protocol.  ``analyze_rollouts`` independently rematerializes and checks
    # this envelope before it accepts any row.  Sealing the chosen candidate
    # necessarily changes the outer protocol hash, so physical replay happens
    # here, before that one-time state transition.
    materialized = dict(
        _mapping(materialized_protocol, "selection materialized protocol")
    )
    if materialized.get("protocol") != dict(protocol):
        raise FuryMultiseedProtocolError(
            "selection materialized envelope does not contain the supplied shortlist protocol"
        )
    replay_receipts: list[JSONMap] = []
    replayed_analyses: list[JSONMap] = []
    seen_replays: set[str] = set()
    replay_fields = {
        "candidate_id",
        "shortlist_protocol_path",
        "runner_plan_path",
        "shard_manifest_paths",
        "reduction_path",
        "analysis_path",
    }
    for index, raw_replay in enumerate(physical_replays):
        replay = _mapping(raw_replay, f"selection physical_replays[{index}]")
        if set(replay) != replay_fields:
            raise FuryMultiseedProtocolError(
                "selection physical replay input fields mismatch"
            )
        candidate_id = replay.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in expected_candidates
            or candidate_id in seen_replays
        ):
            raise FuryMultiseedProtocolError(
                "selection physical replay contains an unknown or duplicate candidate"
            )
        seen_replays.add(candidate_id)
        shortlist_protocol_path = _physical_json_path(
            replay.get("shortlist_protocol_path"),
            f"selection physical replay {candidate_id} shortlist protocol",
        )
        physical_shortlist_protocol = _read_json_object(
            shortlist_protocol_path, "selection shortlist protocol"
        )
        if physical_shortlist_protocol != dict(protocol):
            raise FuryMultiseedProtocolError(
                "selection physical shortlist protocol differs from the supplied protocol"
            )
        plan_path = _physical_json_path(
            replay.get("runner_plan_path"),
            f"selection physical replay {candidate_id} runner plan",
        )
        reduction_path = _physical_json_path(
            replay.get("reduction_path"),
            f"selection physical replay {candidate_id} reduction",
        )
        analysis_path = _physical_json_path(
            replay.get("analysis_path"),
            f"selection physical replay {candidate_id} analysis",
        )
        physical_analysis = _read_json_object(
            analysis_path, "selection claimed analysis"
        )
        if physical_analysis != analyses_by_candidate[candidate_id]:
            raise FuryMultiseedProtocolError(
                "selection physical analysis differs from the supplied analysis"
            )
        raw_manifest_paths = _sequence(
            replay.get("shard_manifest_paths"),
            f"selection physical replay {candidate_id} shard manifests",
        )
        if not raw_manifest_paths:
            raise FuryMultiseedProtocolError(
                "selection physical replay requires shard manifest paths"
            )
        manifest_paths = tuple(
            _physical_json_path(
                value,
                f"selection physical replay {candidate_id} shard manifest[{position}]",
            )
            for position, value in enumerate(raw_manifest_paths)
        )
        runner_plan = _read_json_object(plan_path, "selection runner plan")
        persisted_reduction = _read_json_object(
            reduction_path, "selection reduction"
        )
        try:
            replayed_reduction = reduce_shards(runner_plan, manifest_paths)
        except (FuryPairedRunnerError, TypeError, ValueError) as error:
            raise FuryMultiseedProtocolError(
                f"selection physical replay failed runner validation: {error}"
            ) from error
        if (
            replayed_reduction.get("duplicate_shard_copy_count") != 0
            or replayed_reduction.get("input_shard_copy_count")
            != replayed_reduction.get("unique_shard_count")
            or replayed_reduction.get("unique_shard_count")
            != replayed_reduction.get("required_shard_count")
        ):
            raise FuryMultiseedProtocolError(
                "selection physical replay requires exactly one manifest per planned shard"
            )
        persisted_generated_at = _utc_timestamp(
            persisted_reduction.get("generated_at"),
            "selection persisted reduction generated_at",
        )
        if persisted_reduction.get("generated_at") != persisted_generated_at:
            raise FuryMultiseedProtocolError(
                "selection persisted reduction timestamp is not canonical UTC"
            )
        canonical_reduction = dict(replayed_reduction)
        canonical_reduction["generated_at"] = persisted_generated_at
        if persisted_reduction != canonical_reduction:
            raise FuryMultiseedProtocolError(
                "selection persisted reduction differs from physical manifest replay"
            )
        claimed_analysis = analyses_by_candidate[candidate_id]
        claimed_generated_at = _utc_timestamp(
            claimed_analysis.get("generated_at"),
            "selection claimed analysis generated_at",
        )
        if claimed_analysis.get("generated_at") != claimed_generated_at:
            raise FuryMultiseedProtocolError(
                "selection claimed analysis timestamp is not canonical UTC"
            )
        replayed_analysis = analyze_rollouts(
            materialized,
            replayed_reduction["rollout_rows"],
            runner_plan=runner_plan,
            reduction_receipt=replayed_reduction["reduction_receipt"],
            expected_protocol_sha256=materialized["protocol_sha256"],
            phase="selection_validation",
            candidate_id=candidate_id,
            manifest_paths=manifest_paths,
            _allow_nonpromoting_test_selection_replay_authority=(
                _allow_nonpromoting_test_selection_replay_authority
            ),
        )
        replayed_core = dict(replayed_analysis)
        replayed_core.pop("analysis_sha256")
        replayed_core["generated_at"] = claimed_generated_at
        replayed_analysis = {
            **replayed_core,
            "analysis_sha256": sha256_json(replayed_core),
        }
        if claimed_analysis != replayed_analysis:
            raise FuryMultiseedProtocolError(
                "selection claimed analysis differs from physical replay recomputation"
            )
        reduction_receipt = replayed_reduction["reduction_receipt"]
        manifest_file_hashes = sorted(
            _file_sha256(path, "selection shard manifest")
            for path in manifest_paths
        )
        receipt_core: JSONMap = {
            "schema": SELECTION_PHYSICAL_REPLAY_RECEIPT_SCHEMA,
            "candidate_id": candidate_id,
            "candidate_identity_sha256": expected_candidates[candidate_id][
                "candidate_identity_sha256"
            ],
            "protocol_sha256": materialized["protocol_sha256"],
            "shortlist_protocol_file_sha256": _file_sha256(
                shortlist_protocol_path, "selection shortlist protocol"
            ),
            "runner_plan_sha256": runner_plan["plan_sha256"],
            "runner_plan_file_sha256": _file_sha256(
                plan_path, "selection runner plan"
            ),
            "analysis_file_sha256": _file_sha256(
                analysis_path, "selection analysis"
            ),
            "shard_manifest_file_sha256s": manifest_file_hashes,
            "shard_manifest_file_set_sha256": sha256_json(
                manifest_file_hashes
            ),
            "reduction_sha256": replayed_reduction["reduction_sha256"],
            "reduction_file_sha256": _file_sha256(
                reduction_path, "selection reduction"
            ),
            "reduction_receipt_sha256": reduction_receipt["receipt_sha256"],
            "canonical_rollout_set_sha256": reduction_receipt[
                "canonical_rollout_set_sha256"
            ],
            "shard_physical_evidence_sha256": reduction_receipt[
                "shard_physical_evidence_sha256"
            ],
            "full_policy_artifact_set_sha256": reduction_receipt[
                "full_policy_artifact_set_sha256"
            ],
            "physical_artifact_validation": reduction_receipt[
                "physical_artifact_validation"
            ],
            "replayed_analysis_sha256": replayed_analysis["analysis_sha256"],
        }
        replay_receipts.append(
            {
                **receipt_core,
                "physical_replay_receipt_sha256": sha256_json(receipt_core),
            }
        )
        replayed_analyses.append(replayed_analysis)
    if seen_replays != set(expected_candidates):
        raise FuryMultiseedProtocolError(
            "selection physical replays do not exactly cover the frozen shortlist"
        )
    return _build_selection_evidence_bundle_core(
        protocol,
        replayed_analyses,
        physical_replay_receipts=replay_receipts,
    )


def _build_selection_evidence_bundle_core(
    protocol: Mapping[str, Any],
    analyses: Sequence[Mapping[str, Any]],
    *,
    physical_replay_receipts: Sequence[Mapping[str, Any]],
) -> JSONMap:
    """Validate replayed analyses and bind the preregistered metric."""

    selection = _mapping(protocol.get("selection_contract"), "selection_contract")
    shortlist = validate_frozen_selection_shortlist(
        selection.get("frozen_shortlist")
    )
    expected_candidates = {
        str(row["policy_id"]): dict(row) for row in shortlist["candidates"]
    }
    if not analyses:
        raise FuryMultiseedProtocolError(
            "selection evidence must contain every shortlisted analysis"
        )
    expected_baselines = [
        {
            "policy_id": row["policy_id"],
            "source_bundle_sha256": row["source_bundle_sha256"],
            "policy_adapter_sha256": row["policy_adapter_sha256"],
            "policy_profile_sha256": row["policy_profile_sha256"],
        }
        for row in protocol["baseline_contract"]["required_baselines"]
    ]
    unavailable_baselines = [
        str(row["policy_id"])
        for row in protocol["baseline_contract"]["required_baselines"]
        if row.get("comparison_eligible") is not True
        or not isinstance(row.get("comparison_eligibility_receipt"), Mapping)
    ]
    if unavailable_baselines:
        raise FuryMultiseedProtocolError(
            "selection evidence cannot be built while required baselines are "
            "not comparison eligible: " + ", ".join(unavailable_baselines)
        )
    expected_cells = {
        (row["policy_id"], stratum)
        for row in protocol["baseline_contract"]["required_baselines"]
        for stratum in protocol["evaluation_contract"]["required_strata"]
    }
    design_digest = selection_design_sha256(protocol, shortlist)
    normalized_analyses: list[JSONMap] = []
    metric_rows: list[JSONMap] = []
    seen_candidates: set[str] = set()
    for index, raw in enumerate(analyses):
        analysis = dict(_mapping(raw, f"selection analyses[{index}]"))
        digest = analysis.pop("analysis_sha256", None)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                "selection analysis must carry analysis_sha256"
            )
        if digest != sha256_json(analysis):
            raise FuryMultiseedProtocolError(
                "selection analysis content address mismatch"
            )
        analysis["analysis_sha256"] = digest
        if (
            analysis.get("schema_version") != 2
            or analysis.get("kind") != ANALYSIS_KIND
            or analysis.get("phase") != "selection_validation"
        ):
            raise FuryMultiseedProtocolError(
                "selection evidence must use typed selection-validation analyses"
            )
        if analysis.get("protocol_id") != protocol.get("protocol_id"):
            raise FuryMultiseedProtocolError(
                "selection analysis protocol identity mismatch"
            )
        for field in (
            "protocol_sha256",
            "runner_plan_sha256",
            "reduction_receipt_sha256",
            "canonical_rollout_set_sha256",
        ):
            value = analysis.get(field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise FuryMultiseedProtocolError(
                    f"selection analysis {field} must be a retained SHA-256 identity"
                )
        candidate_id = analysis.get("candidate_id")
        candidate = expected_candidates.get(str(candidate_id))
        if candidate is None or candidate_id in seen_candidates:
            raise FuryMultiseedProtocolError(
                "selection analyses contain an unknown or duplicate candidate"
            )
        seen_candidates.add(str(candidate_id))
        if analysis.get("candidate_identity") != candidate:
            raise FuryMultiseedProtocolError(
                "selection analysis candidate identity differs from the shortlist"
            )
        if analysis.get("selection_design_sha256") != design_digest:
            raise FuryMultiseedProtocolError(
                "selection analysis was not produced under the frozen design"
            )
        if (
            analysis.get("selection_metric_definition_sha256")
            != SELECTION_METRIC_DEFINITION_SHA256
        ):
            raise FuryMultiseedProtocolError(
                "selection analysis metric definition mismatch"
            )
        if analysis.get("required_baseline_identities") != expected_baselines:
            raise FuryMultiseedProtocolError(
                "selection analysis baseline identities mismatch"
            )
        expected_seed = shortlist["selection_input_identities"]["seed"]
        observed_seed = _mapping(
            analysis.get("seed_identity"), "selection analysis seed_identity"
        )
        if observed_seed != {
            "phase": "selection_validation",
            "seed_count": expected_seed["seed_count"],
            "seed_list_sha256": expected_seed["seed_list_sha256"],
        }:
            raise FuryMultiseedProtocolError(
                "selection analysis seed identity differs from the shortlist"
            )
        expected_corpus = shortlist["selection_input_identities"]["corpus"]
        observed_corpus = _mapping(
            analysis.get("corpus_identity"),
            "selection analysis corpus_identity",
        )
        if observed_corpus != {
            "corpus_manifest_sha256": expected_corpus[
                "corpus_manifest_sha256"
            ],
            "corpus_binding_sha256": expected_corpus[
                "corpus_binding_sha256"
            ],
            "runner_scenario_count": expected_corpus["scenario_count"],
            "source_instance_provenance_sha256": expected_corpus[
                "source_instance_provenance_sha256"
            ],
        }:
            raise FuryMultiseedProtocolError(
                "selection analysis corpus identity differs from the shortlist"
            )
        expected_runner = shortlist["selection_input_identities"]["runner"]
        selection_binding = protocol["corpus_contract"][
            "phase_corpus_bindings"
        ]["selection_validation"]
        observed_runner = _mapping(
            analysis.get("runner_identity"),
            "selection analysis runner_identity",
        )
        if observed_runner != {
            "runner_source_identity_sha256": expected_runner[
                "runner_source_identity_sha256"
            ],
            "runner_inputs_sha256": expected_runner["runner_inputs_sha256"],
            "runner_scenario_bundle_sha256": expected_runner[
                "runner_scenario_bundle_sha256"
            ],
            "scenario_model_bundle_sha256": selection_binding[
                "scenario_model_bundle_sha256"
            ],
            "target_context_bundle_set_sha256": selection_binding[
                "target_context_bundle_set_sha256"
            ],
            "bridge_sha256": expected_runner["bridge_sha256"],
            "execution_bundle_sha256": expected_runner[
                "execution_bundle_sha256"
            ],
        }:
            raise FuryMultiseedProtocolError(
                "selection analysis runner identity differs from the shortlist"
            )
        if analysis.get("blocked_reasons") != []:
            raise FuryMultiseedProtocolError(
                "a blocked analysis cannot participate in candidate selection"
            )
        if analysis.get("victory_claim_allowed") is not False:
            raise FuryMultiseedProtocolError(
                "selection-validation evidence cannot claim final victory"
            )
        statistical_contract = _mapping(
            analysis.get("statistical_contract"),
            "selection analysis statistical_contract",
        )
        expected_test_count = len(expected_cells)
        statistics = protocol["evaluation_contract"]["statistics"]
        if (
            statistical_contract.get("estimand")
            != "finite-corpus wave-family-weighted paired relative DPS improvement"
            or statistical_contract.get("population_generalization_claimed")
            is not False
            or
            statistical_contract.get("bootstrap_replicates")
            != statistics["bootstrap_replicates"]
            or statistical_contract.get("simultaneous_test_count")
            != expected_test_count
            or statistical_contract.get("voting_bound_per_comparison")
            != "crossed_seed_x_guild_player_component_lower_bound"
            or statistical_contract.get("marginal_cluster_bounds")
            != "diagnostic_nonvoting"
        ):
            raise FuryMultiseedProtocolError(
                "selection analysis statistical contract differs from the protocol"
            )
        cells: dict[tuple[str, str], float] = {}
        for raw_comparison in _sequence(
            analysis.get("comparisons"), "selection analysis comparisons"
        ):
            comparison = _mapping(raw_comparison, "selection comparison")
            required_comparison_fields = {
                "candidate_id",
                "baseline_id",
                "stratum",
                "pair_count",
                "instance_count",
                "component_count",
                "scenario_count",
                "seed_count",
                "paired_relative_dps_improvement_pct",
                "crossed_seed_component_lower_bound_pct",
                "crossed_seed_component_bootstrap",
                "diagnostic_nonvoting",
                "minimum_practical_mean_improvement_pct",
                "required_lower_bound_pct",
                "comparison_passed",
            }
            missing_comparison_fields = required_comparison_fields.difference(
                comparison
            )
            if missing_comparison_fields:
                raise FuryMultiseedProtocolError(
                    "selection comparison lacks retained statistical evidence: "
                    + ", ".join(sorted(missing_comparison_fields))
                )
            key = (
                str(comparison.get("baseline_id")),
                str(comparison.get("stratum")),
            )
            if key in cells:
                raise FuryMultiseedProtocolError(
                    "selection analysis contains a duplicate comparison cell"
                )
            if comparison.get("candidate_id") != candidate_id:
                raise FuryMultiseedProtocolError(
                    "selection comparison candidate identity mismatch"
                )
            lower_bound = _finite_number(
                comparison.get("crossed_seed_component_lower_bound_pct"),
                "selection comparison crossed lower bound",
            )
            point = _finite_number(
                comparison.get("paired_relative_dps_improvement_pct"),
                "selection comparison point estimate",
            )
            practical = _finite_number(
                comparison.get("minimum_practical_mean_improvement_pct"),
                "selection comparison practical threshold",
            )
            required_lower = _finite_number(
                comparison.get("required_lower_bound_pct"),
                "selection comparison lower-bound threshold",
            )
            for count_field in (
                "pair_count",
                "instance_count",
                "component_count",
                "scenario_count",
            ):
                _positive_int(
                    comparison.get(count_field),
                    f"selection comparison {count_field}",
                )
            if comparison.get("seed_count") != expected_seed["seed_count"]:
                raise FuryMultiseedProtocolError(
                    "selection comparison seed count differs from the shortlist"
                )
            crossed = _mapping(
                comparison.get("crossed_seed_component_bootstrap"),
                "selection comparison crossed bootstrap",
            )
            if (
                crossed.get("method")
                != "crossed_pigeonhole_seed_x_component_multiplicity_product"
                or crossed.get("estimand")
                != "ratio_of_scenario_weighted_candidate_and_reference_sums"
            ):
                raise FuryMultiseedProtocolError(
                    "selection comparison did not retain the registered crossed bootstrap"
                )
            diagnostic = _mapping(
                comparison.get("diagnostic_nonvoting"),
                "selection comparison diagnostic_nonvoting",
            )
            if (
                diagnostic.get("noninferential") is not True
                or diagnostic.get("used_by_gate") is not False
            ):
                raise FuryMultiseedProtocolError(
                    "selection comparison marginal diagnostics must remain nonvoting"
                )
            expected_passed = point >= practical and lower_bound > required_lower
            if comparison.get("comparison_passed") is not expected_passed:
                raise FuryMultiseedProtocolError(
                    "selection comparison pass flag differs from its registered thresholds"
                )
            cells[key] = lower_bound
        if set(cells) != expected_cells:
            raise FuryMultiseedProtocolError(
                "selection analysis does not exactly cover every required cell"
            )
        observed_gate = analysis.get("simulator_multiseed_gate_passed")
        expected_gate = all(
            bool(comparison.get("comparison_passed"))
            for comparison in analysis["comparisons"]
        )
        if observed_gate is not expected_gate:
            raise FuryMultiseedProtocolError(
                "selection analysis gate flag differs from its complete cells"
            )
        metric_rows.append(
            {
                "candidate_identity_sha256": candidate[
                    "candidate_identity_sha256"
                ],
                "metric_value": min(cells.values()),
                "metric_evidence_sha256": digest,
            }
        )
        normalized_analyses.append(analysis)
    if seen_candidates != set(expected_candidates):
        raise FuryMultiseedProtocolError(
            "selection analyses do not exactly cover the frozen shortlist"
        )
    normalized_analyses.sort(
        key=lambda row: str(row["candidate_identity"]["candidate_identity_sha256"])
    )
    metric_rows.sort(key=lambda row: str(row["candidate_identity_sha256"]))
    normalized_replay_receipts = _validate_selection_physical_replay_receipts(
        physical_replay_receipts,
        expected_candidates=expected_candidates,
        analyses_by_candidate={
            str(row["candidate_id"]): row for row in normalized_analyses
        },
    )
    core: JSONMap = {
        "schema": SELECTION_EVIDENCE_BUNDLE_SCHEMA,
        "shortlist_sha256": shortlist["shortlist_sha256"],
        "selection_design_sha256": design_digest,
        "metric_definition": SELECTION_METRIC_DEFINITION,
        "analysis_artifacts": normalized_analyses,
        "physical_replay_receipts": normalized_replay_receipts,
        "metric_rows": metric_rows,
    }
    return {**core, "selection_evidence_bundle_sha256": sha256_json(core)}


def _normalize_selection_physical_replays(
    values: Iterable[Mapping[str, Any]],
) -> list[JSONMap]:
    expected_fields = {
        "candidate_id",
        "shortlist_protocol_path",
        "runner_plan_path",
        "shard_manifest_paths",
        "reduction_path",
        "analysis_path",
    }
    normalized: list[JSONMap] = []
    try:
        buffered = tuple(values)
    except TypeError as error:
        raise FuryMultiseedProtocolError(
            "selection physical replays must be an iterable of replay packages"
        ) from error
    for index, raw in enumerate(buffered):
        replay = _mapping(raw, f"selection physical replays[{index}]")
        if set(replay) != expected_fields:
            raise FuryMultiseedProtocolError(
                "selection physical replay input fields mismatch"
            )
        candidate_id = replay.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise FuryMultiseedProtocolError(
                "selection physical replay candidate_id must be nonempty"
            )
        manifest_values = _sequence(
            replay.get("shard_manifest_paths"),
            "selection physical replay shard_manifest_paths",
        )
        normalized.append(
            {
                "candidate_id": candidate_id,
                "shortlist_protocol_path": str(
                    _physical_json_path(
                        replay.get("shortlist_protocol_path"),
                        "selection physical replay shortlist protocol",
                    )
                ),
                "runner_plan_path": str(
                    _physical_json_path(
                        replay.get("runner_plan_path"),
                        "selection physical replay runner plan",
                    )
                ),
                "shard_manifest_paths": [
                    str(
                        _physical_json_path(
                            path,
                            f"selection physical replay shard manifest[{position}]",
                        )
                    )
                    for position, path in enumerate(manifest_values)
                ],
                "reduction_path": str(
                    _physical_json_path(
                        replay.get("reduction_path"),
                        "selection physical replay reduction",
                    )
                ),
                "analysis_path": str(
                    _physical_json_path(
                        replay.get("analysis_path"),
                        "selection physical replay analysis",
                    )
                ),
            }
        )
    return normalized


def _validate_selection_stage_transition(
    stage_protocol: Mapping[str, Any],
    sealed_protocol: Mapping[str, Any],
) -> None:
    stage_selection = _mapping(
        stage_protocol.get("selection_contract"),
        "physical selection-stage selection_contract",
    )
    sealed_selection = _mapping(
        sealed_protocol.get("selection_contract"),
        "sealed selection_contract",
    )
    if stage_selection.get("status") != "SHORTLIST_FROZEN":
        raise FuryMultiseedProtocolError(
            "physical selection replay protocol is not SHORTLIST_FROZEN"
        )
    if any(
        stage_selection.get(field) is not None
        for field in (
            "selection_evidence_bundle",
            "selection_receipt",
            "candidate_seal",
        )
    ):
        raise FuryMultiseedProtocolError(
            "physical selection-stage protocol already contains post-selection artifacts"
        )
    if stage_selection.get("frozen_shortlist") != sealed_selection.get(
        "frozen_shortlist"
    ):
        raise FuryMultiseedProtocolError(
            "physical selection-stage shortlist differs from the sealed protocol"
        )
    stage_candidate = _mapping(
        stage_protocol.get("candidate_contract"),
        "physical selection-stage candidate_contract",
    )
    if stage_candidate.get("status") != "NOT_FROZEN":
        raise FuryMultiseedProtocolError(
            "physical selection-stage candidate must not already be frozen"
        )

    # Candidate/selection records and the as-yet-unseen final corpus binding
    # are the only legitimate post-selection transitions.  Everything that
    # defines shortlist scoring must remain byte-for-byte identical.
    stage_invariants = copy.deepcopy(dict(stage_protocol))
    sealed_invariants = copy.deepcopy(dict(sealed_protocol))
    for value in (stage_invariants, sealed_invariants):
        value.pop("selection_contract", None)
        value.pop("candidate_contract", None)
        phase_bindings = value.get("corpus_contract", {}).get(
            "phase_corpus_bindings", {}
        )
        if isinstance(phase_bindings, dict):
            phase_bindings.pop("final_confirmation", None)
    if stage_invariants != sealed_invariants:
        raise FuryMultiseedProtocolError(
            "sealed protocol changed preregistered selection-stage invariants"
        )


def _validate_selection_inputs_and_evidence_against_protocol(
    protocol: Mapping[str, Any],
    *,
    source_closure_sha256: str,
    implementation_identity: Mapping[str, Any],
    selection_physical_replays: Sequence[Mapping[str, Any]],
    allow_nonpromoting_test_baseline_receipts: bool,
) -> None:
    selection = protocol["selection_contract"]
    if selection.get("status") == "NOT_FROZEN":
        if selection_physical_replays:
            raise FuryMultiseedProtocolError(
                "an unfrozen selection cannot consume physical replay packages"
            )
        return
    unavailable_baselines = [
        str(row["policy_id"])
        for row in protocol["baseline_contract"]["required_baselines"]
        if row.get("comparison_eligible") is not True
        or not isinstance(row.get("comparison_eligibility_receipt"), Mapping)
    ]
    if unavailable_baselines:
        raise FuryMultiseedProtocolError(
            "a frozen selection requires every baseline to be comparison eligible"
        )
    shortlist = selection["frozen_shortlist"]
    metric = shortlist["selection_contract"]["metric"]
    if metric != {
        "metric_id": SELECTION_METRIC_ID,
        "definition_sha256": SELECTION_METRIC_DEFINITION_SHA256,
        "direction": "maximize",
    }:
        raise FuryMultiseedProtocolError(
            "frozen shortlist metric differs from the registered selection metric"
        )
    selection_binding = protocol["corpus_contract"]["phase_corpus_bindings"][
        "selection_validation"
    ]
    identities = shortlist["selection_input_identities"]
    seed_identity = identities["seed"]
    seed_contract = protocol["seed_contract"]["phases"]["selection_validation"]
    if (
        seed_identity["seed_count"] != seed_contract["count"]
        or seed_identity["seed_list_sha256"] != seed_contract["seed_list_sha256"]
    ):
        raise FuryMultiseedProtocolError(
            "frozen shortlist seed identity differs from selection-validation"
        )
    corpus_identity = identities["corpus"]
    expected_corpus = {
        "corpus_manifest_sha256": selection_binding.get(
            "corpus_manifest_sha256"
        ),
        "corpus_binding_sha256": selection_binding.get(
            "corpus_binding_sha256"
        ),
        "scenario_count": selection_binding.get("runner_scenario_count"),
        "source_instance_provenance_sha256": selection_binding.get(
            "source_instance_provenance_sha256"
        ),
    }
    for field, expected in expected_corpus.items():
        if corpus_identity.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"frozen shortlist corpus identity {field} mismatch"
            )
    runner_identity = identities["runner"]
    expected_runner = {
        "runner_source_identity_sha256": source_closure_sha256,
        "runner_inputs_sha256": selection_binding.get("runner_inputs_sha256"),
        "runner_scenario_bundle_sha256": selection_binding.get(
            "runner_scenario_bundle_sha256"
        ),
        "execution_bundle_sha256": sha256_json(dict(implementation_identity)),
    }
    for field, expected in expected_runner.items():
        if runner_identity.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"frozen shortlist runner identity {field} mismatch"
            )
    allowed_bridges = {
        protocol["execution_contract"]["windows_local"].get("bridge_sha256"),
        protocol["execution_contract"]["scheduler_hpc"].get(
            "linux_bridge_sha256"
        ),
    }
    if runner_identity.get("bridge_sha256") not in allowed_bridges:
        raise FuryMultiseedProtocolError(
            "frozen shortlist bridge identity is not pinned by the protocol"
        )
    if selection.get("status") == "CANDIDATE_SEALED":
        if not selection_physical_replays:
            raise FuryMultiseedProtocolError(
                "sealed selection requires out-of-band physical replay packages"
            )
        shortlist_paths = {
            str(row.get("shortlist_protocol_path"))
            for row in selection_physical_replays
        }
        if len(shortlist_paths) != 1:
            raise FuryMultiseedProtocolError(
                "sealed selection replay packages disagree on shortlist protocol"
            )
        stage_protocol = _read_json_object(
            Path(next(iter(shortlist_paths))),
            "physical selection-stage protocol",
        )
        _validate_selection_stage_transition(stage_protocol, protocol)
        stage_materialized = materialize_protocol(
            stage_protocol,
            selection_physical_replays=(),
            _allow_nonpromoting_test_baseline_receipts=(
                allow_nonpromoting_test_baseline_receipts
            ),
        )
        expected_bundle = build_selection_evidence_bundle(
            stage_protocol,
            _sequence(
                selection["selection_evidence_bundle"].get(
                    "analysis_artifacts"
                ),
                "selection evidence analysis_artifacts",
            ),
            materialized_protocol=stage_materialized,
            physical_replays=selection_physical_replays,
            _allow_nonpromoting_test_selection_replay_authority=(
                allow_nonpromoting_test_baseline_receipts
            ),
        )
        if selection["selection_evidence_bundle"] != expected_bundle:
            raise FuryMultiseedProtocolError(
                "selection evidence bundle is inconsistent or not content-addressed"
            )
        if (
            selection["selection_receipt"]["complete_metric_rows"]
            != expected_bundle["metric_rows"]
        ):
            raise FuryMultiseedProtocolError(
                "one-time selection receipt metric rows differ from typed evidence"
            )


def materialize_protocol(
    document: Mapping[str, Any],
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str] = (),
    selection_physical_replays: Iterable[Mapping[str, Any]] = (),
    final_discovery_capture_manifest_path: str | Path | None = None,
    _allow_nonpromoting_test_baseline_receipts: bool = False,
) -> JSONMap:
    """Validate a protocol and attach its exact seed lists and content hash.

    ``_allow_nonpromoting_test_baseline_receipts`` exists only so unit tests can
    exercise downstream reduction/statistics code. The resulting envelope is
    permanently marked non-promoting and is rejected by every victory gate.
    The CLI and normal API path never enable it.
    """

    value = copy.deepcopy(dict(document))
    normalized_selection_replays = _normalize_selection_physical_replays(
        selection_physical_replays
    )
    normalized_final_discovery_capture_path: str | None = None
    if final_discovery_capture_manifest_path is not None:
        normalized_final_discovery_capture_path = str(
            _physical_json_path(
                final_discovery_capture_manifest_path,
                "final discovery capture manifest",
            )
        )
    trusted_anchor_hashes = _trusted_anchor_hashes(
        trusted_external_anchor_seal_sha256s
    )
    if value.get("schema_version") != 2 or value.get("kind") != PROTOCOL_KIND:
        raise FuryMultiseedProtocolError(
            f"protocol must be schema_version=2 and kind={PROTOCOL_KIND}"
        )
    protocol_id = value.get("protocol_id")
    if not isinstance(protocol_id, str) or not protocol_id.strip():
        raise FuryMultiseedProtocolError("protocol_id must be a nonempty string")
    protocol_state = value.get("protocol_state")
    if not isinstance(protocol_state, str) or not protocol_state.strip():
        raise FuryMultiseedProtocolError("protocol_state must be a nonempty string")

    runtime_identity = _mapping(
        value.get("runtime_identity_contract"), "runtime_identity_contract"
    )
    if runtime_identity.get("status") != "LOCAL_CONTENT_ADDRESSED_DEVELOPMENT_CAPTURE":
        raise FuryMultiseedProtocolError(
            "runtime_identity_contract.status must preserve the local development boundary"
        )
    if runtime_identity.get("snapshot_schema") != "fury_expert_runtime_snapshot/v1":
        raise FuryMultiseedProtocolError(
            "runtime_identity_contract.snapshot_schema must be fury_expert_runtime_snapshot/v1"
        )
    for field in (
        "snapshot_sha256",
        "cat_savedvariables_sha256",
        "cat_profile1_semantic_sha256",
        "contra_savedvariables_sha256",
        "contra_buttons_semantic_sha256",
        "fixed_player_semantic_sha256",
        "nampower_dll_sha256",
        "superwow_hook_dll_sha256",
    ):
        digest = runtime_identity.get(field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"runtime_identity_contract.{field} must be lowercase SHA-256"
            )
    if runtime_identity.get("comparison_eligible_by_itself") is not False:
        raise FuryMultiseedProtocolError(
            "a runtime identity capture cannot promote itself to comparison evidence"
        )

    seed_contract = _mapping(value.get("seed_contract"), "seed_contract")
    if seed_contract.get("algorithm") != SEED_ALGORITHM:
        raise FuryMultiseedProtocolError(
            f"seed_contract.algorithm must be {SEED_ALGORITHM}"
        )
    namespace = seed_contract.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        raise FuryMultiseedProtocolError(
            "seed_contract.namespace must be a nonempty string"
        )
    raw_phases = _mapping(seed_contract.get("phases"), "seed_contract.phases")
    if set(raw_phases) != set(REQUIRED_PHASES):
        raise FuryMultiseedProtocolError(
            f"seed phases must be exactly {list(REQUIRED_PHASES)}"
        )

    seed_sets: dict[str, list[int]] = {}
    occupied: dict[int, str] = {}
    for phase in REQUIRED_PHASES:
        phase_contract = _mapping(raw_phases[phase], f"seed phase {phase}")
        count = _positive_int(phase_contract.get("count"), f"{phase}.count")
        if count < 200 or count > 1000:
            raise FuryMultiseedProtocolError(
                f"{phase}.count must be between 200 and 1000"
            )
        counter_start = phase_contract.get("counter_start", 0)
        if (
            isinstance(counter_start, bool)
            or not isinstance(counter_start, int)
            or counter_start < 0
        ):
            raise FuryMultiseedProtocolError(
                f"{phase}.counter_start must be a nonnegative integer"
            )
        seeds = derive_seed_set(
            namespace,
            phase,
            count,
            counter_start=counter_start,
        )
        digest = sha256_json(list(seeds))
        expected = phase_contract.get("seed_list_sha256")
        if expected != digest:
            raise FuryMultiseedProtocolError(
                f"{phase}.seed_list_sha256 mismatch: expected {expected!r}, got {digest}"
            )
        for seed in seeds:
            previous = occupied.get(seed)
            if previous is not None:
                raise FuryMultiseedProtocolError(
                    f"seed {seed} overlaps phases {previous} and {phase}"
                )
            occupied[seed] = phase
        seed_sets[phase] = list(seeds)

    corpus_contract = _mapping(value.get("corpus_contract"), "corpus_contract")
    final_corpus_contract = _mapping(
        corpus_contract.get("final_confirmation_corpus"),
        "corpus_contract.final_confirmation_corpus",
    )
    _positive_int(
        final_corpus_contract.get("required_take_first_n"),
        "final_confirmation_corpus.required_take_first_n",
    )
    _positive_int(
        final_corpus_contract.get(
            "minimum_guild_player_leakage_component_count"
        ),
        "final_confirmation_corpus.minimum_guild_player_leakage_component_count",
    )
    current_snapshot = _mapping(
        corpus_contract.get("current_local_snapshot"),
        "corpus_contract.current_local_snapshot",
    )
    completed_instance_count = _positive_int(
        current_snapshot.get("completed_instance_count"),
        "current_local_snapshot.completed_instance_count",
    )
    completed_instance_ids_digest = current_snapshot.get(
        "completed_instance_ids_sha256"
    )
    if (
        not isinstance(completed_instance_ids_digest, str)
        or not _SHA256.fullmatch(completed_instance_ids_digest)
    ):
        raise FuryMultiseedProtocolError(
            "current_local_snapshot.completed_instance_ids_sha256 must be "
            "lowercase SHA-256"
        )
    completed_instance_id_hashes = _sequence(
        current_snapshot.get("completed_instance_id_sha256s"),
        "current_local_snapshot.completed_instance_id_sha256s",
    )
    if (
        len(completed_instance_id_hashes) != completed_instance_count
        or list(completed_instance_id_hashes)
        != sorted(set(completed_instance_id_hashes))
        or any(
            not isinstance(item, str) or not _SHA256.fullmatch(item)
            for item in completed_instance_id_hashes
        )
    ):
        raise FuryMultiseedProtocolError(
            "current_local_snapshot.completed_instance_id_sha256s must be a "
            "sorted unique SHA-256 list matching completed_instance_count"
        )
    completed_instance_hash_list_digest = current_snapshot.get(
        "completed_instance_id_sha256s_sha256"
    )
    if completed_instance_hash_list_digest != sha256_json(
        list(completed_instance_id_hashes)
    ):
        raise FuryMultiseedProtocolError(
            "current_local_snapshot.completed_instance_id_sha256s_sha256 mismatch"
        )
    completed_component_hashes = _sequence(
        current_snapshot.get("completed_guild_player_component_sha256s"),
        "current_local_snapshot.completed_guild_player_component_sha256s",
    )
    if (
        not completed_component_hashes
        or list(completed_component_hashes)
        != sorted(set(completed_component_hashes))
        or any(
            not isinstance(item, str) or not _SHA256.fullmatch(item)
            for item in completed_component_hashes
        )
    ):
        raise FuryMultiseedProtocolError(
            "current_local_snapshot.completed_guild_player_component_sha256s "
            "must be a nonempty sorted unique SHA-256 list"
        )
    if current_snapshot.get(
        "completed_guild_player_component_sha256s_sha256"
    ) != sha256_json(list(completed_component_hashes)):
        raise FuryMultiseedProtocolError(
            "current_local_snapshot completed component-list identity mismatch"
        )
    phase_bindings = _mapping(
        corpus_contract.get("phase_corpus_bindings"),
        "corpus_contract.phase_corpus_bindings",
    )
    if set(phase_bindings) != set(REQUIRED_PHASES):
        raise FuryMultiseedProtocolError(
            "phase_corpus_bindings must exactly cover all seed phases"
        )
    for phase in REQUIRED_PHASES:
        binding = _mapping(
            phase_bindings[phase], f"phase_corpus_bindings.{phase}"
        )
        status = binding.get("status")
        if status not in {"NOT_AVAILABLE", "NOT_FROZEN", "FROZEN"}:
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} has unsupported status {status!r}"
            )
        claim_eligible = binding.get("can_support_final_victory_claim")
        if not isinstance(claim_eligible, bool):
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} final-claim flag must be boolean"
            )
        comparison_eligible = binding.get("comparison_eligible")
        if not isinstance(comparison_eligible, bool):
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} comparison_eligible must be boolean"
            )
        allowed_plan_intents = _sequence(
            binding.get("allowed_plan_intents"),
            f"phase corpus {phase} allowed_plan_intents",
        )
        if (
            len(allowed_plan_intents) != len(set(allowed_plan_intents))
            or any(
                intent not in {COMPARISON_INTENT, DIAGNOSTIC_INTENT}
                for intent in allowed_plan_intents
            )
        ):
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} allowed_plan_intents is invalid"
            )
        if status != "FROZEN" and allowed_plan_intents:
            raise FuryMultiseedProtocolError(
                f"unfrozen phase corpus {phase} cannot allow a runner plan"
            )
        if comparison_eligible and COMPARISON_INTENT not in allowed_plan_intents:
            raise FuryMultiseedProtocolError(
                f"comparison-eligible phase corpus {phase} must allow scientific comparison"
            )
        if not comparison_eligible and COMPARISON_INTENT in allowed_plan_intents:
            raise FuryMultiseedProtocolError(
                f"non-comparison phase corpus {phase} cannot allow scientific comparison"
            )
        digest = binding.get("corpus_manifest_sha256")
        if status == "FROZEN":
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise FuryMultiseedProtocolError(
                    f"frozen phase corpus {phase} must pin a manifest SHA-256"
                )
            runner_inputs_digest = binding.get("runner_inputs_sha256")
            if not isinstance(runner_inputs_digest, str) or not _SHA256.fullmatch(
                runner_inputs_digest
            ):
                raise FuryMultiseedProtocolError(
                    f"frozen phase corpus {phase} must pin runner_inputs_sha256"
                )
            runner_scenario_digest = binding.get(
                "runner_scenario_bundle_sha256"
            )
            if not isinstance(
                runner_scenario_digest, str
            ) or not _SHA256.fullmatch(runner_scenario_digest):
                raise FuryMultiseedProtocolError(
                    f"frozen phase corpus {phase} must pin "
                    "runner_scenario_bundle_sha256"
                )
            for field in (
                "scenario_model_bundle_sha256",
                "target_context_bundle_set_sha256",
                "source_instance_provenance_sha256",
            ):
                field_digest = binding.get(field)
                if (
                    not isinstance(field_digest, str)
                    or not _SHA256.fullmatch(field_digest)
                ):
                    raise FuryMultiseedProtocolError(
                        f"frozen phase corpus {phase} must pin {field}"
                    )
            _positive_int(
                binding.get("runner_scenario_count"),
                f"phase corpus {phase} runner_scenario_count",
            )
            binding_digest = binding.get("corpus_binding_sha256")
            if (
                not isinstance(binding_digest, str)
                or not _SHA256.fullmatch(binding_digest)
                or binding_digest != corpus_binding_sha256(binding)
            ):
                raise FuryMultiseedProtocolError(
                    f"frozen phase corpus {phase} corpus_binding_sha256 mismatch"
                )
        elif digest is not None:
            raise FuryMultiseedProtocolError(
                f"unfrozen phase corpus {phase} must not pin a manifest SHA-256"
            )
        if phase != "final_confirmation" and claim_eligible:
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} cannot support a final victory claim"
            )
        final_evidence = binding.get("final_evidence")
        if phase != "final_confirmation" and final_evidence is not None:
            raise FuryMultiseedProtocolError(
                f"phase corpus {phase} must not carry final_evidence"
            )
        if phase == "final_confirmation":
            if claim_eligible and (
                status != "FROZEN" or not isinstance(final_evidence, Mapping)
            ):
                raise FuryMultiseedProtocolError(
                    "a final-claim-eligible corpus must be frozen and carry a "
                    "typed admission receipt"
                )
            if not claim_eligible and final_evidence is not None:
                raise FuryMultiseedProtocolError(
                    "a non-final-claim corpus must not carry final_evidence"
                )

    baseline_contract = _mapping(
        value.get("baseline_contract"), "baseline_contract"
    )
    entries = _sequence(
        baseline_contract.get("required_baselines"),
        "baseline_contract.required_baselines",
    )
    baseline_ids: list[str] = []
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"required_baselines[{index}]")
        policy_id = entry.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise FuryMultiseedProtocolError(
                f"required_baselines[{index}].policy_id must be nonempty"
            )
        eligible = entry.get("comparison_eligible")
        if not isinstance(eligible, bool):
            raise FuryMultiseedProtocolError(
                f"required_baselines[{index}].comparison_eligible must be boolean"
            )
        readiness_receipt = entry.get("comparison_eligibility_receipt")
        if eligible and not isinstance(readiness_receipt, Mapping):
            raise FuryMultiseedProtocolError(
                f"eligible required_baselines[{index}] must carry a typed "
                "comparison_eligibility_receipt"
            )
        if not eligible and readiness_receipt is not None:
            raise FuryMultiseedProtocolError(
                f"ineligible required_baselines[{index}] must not carry a "
                "comparison_eligibility_receipt"
            )
        source_digest = entry.get("source_bundle_sha256")
        if not isinstance(source_digest, str) or not _SHA256.fullmatch(source_digest):
            raise FuryMultiseedProtocolError(
                f"required_baselines[{index}].source_bundle_sha256 must be lowercase SHA-256"
            )
        for field, digest in entry.items():
            optional_missing_identity = (
                field in {"policy_adapter_sha256", "policy_profile_sha256"}
                and digest is None
                and eligible is False
            )
            if field.endswith("_sha256") and not optional_missing_identity and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            ):
                raise FuryMultiseedProtocolError(
                    f"required_baselines[{index}].{field} must be lowercase SHA-256"
                )
        if eligible:
            for field in ("policy_adapter_sha256", "policy_profile_sha256"):
                digest = entry.get(field)
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise FuryMultiseedProtocolError(
                        f"eligible required_baselines[{index}].{field} must be lowercase SHA-256"
                    )
        baseline_ids.append(policy_id)
    if not baseline_ids or len(set(baseline_ids)) != len(baseline_ids):
        raise FuryMultiseedProtocolError(
            "required baseline IDs must be nonempty and unique"
        )

    candidate_contract = _mapping(
        value.get("candidate_contract"), "candidate_contract"
    )
    if not isinstance(candidate_contract.get("selected_without_final_confirmation_seeds"), bool):
        raise FuryMultiseedProtocolError(
            "candidate_contract.selected_without_final_confirmation_seeds must be boolean"
        )
    candidate_status = candidate_contract.get("status")
    if candidate_status not in {"NOT_FROZEN", "FROZEN"}:
        raise FuryMultiseedProtocolError(
            "candidate_contract.status must be NOT_FROZEN or FROZEN"
        )
    if candidate_status == "FROZEN":
        candidate_policy_id = candidate_contract.get("policy_id")
        candidate_digest = candidate_contract.get("policy_source_sha256")
        if not isinstance(candidate_policy_id, str) or not candidate_policy_id.strip():
            raise FuryMultiseedProtocolError(
                "frozen candidate_contract.policy_id must be nonempty"
            )
        if not isinstance(candidate_digest, str) or not _SHA256.fullmatch(candidate_digest):
            raise FuryMultiseedProtocolError(
                "frozen candidate_contract.policy_source_sha256 must be lowercase SHA-256"
            )
        for field in ("policy_adapter_sha256", "policy_profile_sha256"):
            digest = candidate_contract.get(field)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise FuryMultiseedProtocolError(
                    f"frozen candidate_contract.{field} must be lowercase SHA-256"
                )
        if candidate_contract["selected_without_final_confirmation_seeds"] is not True:
            raise FuryMultiseedProtocolError(
                "frozen candidate must be selected without final-confirmation seeds"
            )
        for field in (
            "selection_receipt_sha256",
            "candidate_seal_sha256",
            "external_anchor_seal_sha256",
        ):
            digest = candidate_contract.get(field)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise FuryMultiseedProtocolError(
                    f"frozen candidate_contract.{field} must be lowercase SHA-256"
                )
        if not isinstance(candidate_contract.get("frozen_at"), str):
            raise FuryMultiseedProtocolError(
                "frozen candidate_contract.frozen_at must come from the external anchor"
            )
    else:
        for field in (
            "policy_id",
            "policy_source_sha256",
            "policy_adapter_sha256",
            "policy_profile_sha256",
            "selection_receipt_sha256",
            "candidate_seal_sha256",
            "external_anchor_seal_sha256",
            "frozen_at",
        ):
            if candidate_contract.get(field) is not None:
                raise FuryMultiseedProtocolError(
                    f"unfrozen candidate_contract.{field} must be null"
                )

    selection_contract = _validate_selection_contract(
        value.get("selection_contract"),
        candidate_contract=candidate_contract,
        trusted_external_anchor_seal_sha256s=trusted_anchor_hashes,
    )
    value["selection_contract"] = selection_contract
    development_candidate_registry = _validate_development_candidate_registry(
        value.get("development_candidate_registry")
    )
    value["development_candidate_registry"] = development_candidate_registry

    final_binding = _mapping(
        phase_bindings["final_confirmation"],
        "phase_corpus_bindings.final_confirmation",
    )
    evaluation = _mapping(value.get("evaluation_contract"), "evaluation_contract")
    if evaluation.get("full_policy") is not True:
        raise FuryMultiseedProtocolError("full_policy must be true")
    if evaluation.get("complete_scenario") is not True:
        raise FuryMultiseedProtocolError("complete_scenario must be true")
    _positive_int(
        evaluation.get("primary_minimum_horizon_ms"),
        "evaluation_contract.primary_minimum_horizon_ms",
    )
    minimum_components = _mapping(
        evaluation.get("minimum_component_count_by_phase_and_stratum"),
        "evaluation_contract.minimum_component_count_by_phase_and_stratum",
    )
    if set(minimum_components) != set(REQUIRED_PHASES):
        raise FuryMultiseedProtocolError(
            "minimum_component_count_by_phase_and_stratum must cover all seed phases"
        )
    for phase in REQUIRED_PHASES:
        phase_minimums = _mapping(
            minimum_components[phase],
            f"minimum component counts for {phase}",
        )
        if set(phase_minimums) != {"overall", "single_target", "multi_target"}:
            raise FuryMultiseedProtocolError(
                f"minimum component counts for {phase} must cover every stratum"
            )
        for stratum, minimum in phase_minimums.items():
            _positive_int(
                minimum,
                f"minimum component count for {phase}/{stratum}",
            )
    strata = _sequence(evaluation.get("required_strata"), "required_strata")
    if strata != ["overall", "single_target", "multi_target"]:
        raise FuryMultiseedProtocolError(
            "required_strata must be overall, single_target, multi_target"
        )
    rollout_fields = _sequence(
        evaluation.get("rollout_row_required_fields"),
        "evaluation_contract.rollout_row_required_fields",
    )
    if (
        len(rollout_fields) != len(set(rollout_fields))
        or set(rollout_fields) != set(ROLLOUT_REQUIRED_FIELDS)
    ):
        raise FuryMultiseedProtocolError(
            "rollout_row_required_fields must exactly match the paired-runner v2 row contract"
        )
    statistics = _mapping(evaluation.get("statistics"), "statistics")
    confidence = _finite_number(
        statistics.get("confidence_level"), "statistics.confidence_level"
    )
    if not 0.5 < confidence < 1.0:
        raise FuryMultiseedProtocolError("confidence_level must be in (0.5, 1)")
    if statistics.get("familywise_method") != "bonferroni_one_sided":
        raise FuryMultiseedProtocolError(
            "familywise_method must be bonferroni_one_sided"
        )
    bootstrap_replicates = _positive_int(
        statistics.get("bootstrap_replicates"),
        "statistics.bootstrap_replicates",
    )
    minimum_bootstrap_replicates = _positive_int(
        statistics.get("minimum_bootstrap_replicates"),
        "statistics.minimum_bootstrap_replicates",
    )
    if bootstrap_replicates < minimum_bootstrap_replicates:
        raise FuryMultiseedProtocolError(
            "bootstrap_replicates cannot be below minimum_bootstrap_replicates"
        )
    expected_simultaneous_test_count = len(entries) * len(strata)
    if statistics.get("simultaneous_test_count") != expected_simultaneous_test_count:
        raise FuryMultiseedProtocolError(
            "statistics.simultaneous_test_count must equal required baselines x strata"
        )
    if (
        statistics.get("voting_bound_per_comparison")
        != "crossed_seed_x_guild_player_component_lower_bound"
        or statistics.get("marginal_cluster_bounds") != "diagnostic_nonvoting"
    ):
        raise FuryMultiseedProtocolError(
            "statistics must use the crossed voting bound with nonvoting marginals"
        )
    _positive_int(statistics.get("bootstrap_seed"), "statistics.bootstrap_seed")
    practical_by_stratum = _mapping(
        statistics.get("minimum_practical_mean_improvement_pct_by_stratum"),
        "statistics.minimum_practical_mean_improvement_pct_by_stratum",
    )
    if set(practical_by_stratum) != set(strata):
        raise FuryMultiseedProtocolError(
            "practical-improvement thresholds must exactly cover required_strata"
        )
    lower = _finite_number(
        statistics.get("required_simultaneous_lower_bound_pct"),
        "statistics.required_simultaneous_lower_bound_pct",
    )
    for stratum in strata:
        practical = _finite_number(
            practical_by_stratum[stratum],
            f"practical improvement for {stratum}",
        )
        if practical < lower:
            raise FuryMultiseedProtocolError(
                "minimum practical mean improvement cannot be below the CI lower-bound gate"
            )

    execution = _mapping(value.get("execution_contract"), "execution_contract")
    if (
        execution.get("simulator_seed_derivation_algorithm")
        != SIMULATOR_SEED_DERIVATION_ALGORITHM
    ):
        raise FuryMultiseedProtocolError(
            "execution_contract simulator seed derivation does not match runner"
        )
    if execution.get("one_bridge_process_per_worker") is not True:
        raise FuryMultiseedProtocolError(
            "one_bridge_process_per_worker must be true"
        )
    if execution.get("shared_mutable_bridge_between_workers") is not False:
        raise FuryMultiseedProtocolError(
            "shared_mutable_bridge_between_workers must be false"
        )
    if execution.get("required_runner_execution_mode") != SINGLE_BRIDGE_MODE:
        raise FuryMultiseedProtocolError(
            "required_runner_execution_mode must require one real bridge per worker"
        )
    source_identity_contract = _mapping(
        execution.get("python_source_identity_contract"),
        "execution_contract.python_source_identity_contract",
    )
    expected_source_identity_fields = {
        "schema",
        "verifier",
        "verify_at_materialization",
        "required_entrypoints",
        "file_count",
        "canonical_bundle_sha256",
    }
    if set(source_identity_contract) != expected_source_identity_fields:
        raise FuryMultiseedProtocolError(
            "python source identity contract fields mismatch"
        )
    if (
        source_identity_contract.get("schema")
        != EXECUTION_SOURCE_IDENTITY_SCHEMA
        or source_identity_contract.get("verifier")
        != "python_ast_recursive_local_imports"
        or source_identity_contract.get("verify_at_materialization") is not True
        or source_identity_contract.get("required_entrypoints")
        != sorted(REQUIRED_PRODUCTION_PATHS)
    ):
        raise FuryMultiseedProtocolError(
            "python source identity contract differs from the production verifier"
        )
    try:
        observed_source_identity = build_fury_execution_source_identity_v2()
    except (FuryExecutionSourceIdentityV2Error, OSError, ValueError) as error:
        raise FuryMultiseedProtocolError(
            f"could not verify the live Python execution closure: {error}"
        ) from error
    if (
        source_identity_contract.get("file_count")
        != observed_source_identity["file_count"]
        or source_identity_contract.get("canonical_bundle_sha256")
        != observed_source_identity["canonical_bundle"]["sha256"]
    ):
        raise FuryMultiseedProtocolError(
            "live Python execution closure differs from the protocol identity"
        )
    implementation_identity = _mapping(
        execution.get("implementation_identity"),
        "execution_contract.implementation_identity",
    )
    expected_implementation_fields = {
        "python_source_closure_sha256",
        "ordered_sink_executor_sha256",
        "full_policy_rollout_executor_sha256",
        "paired_runner_source_sha256",
        "evaluation_source_sha256",
        "runtime_snapshot_sha256",
    }
    if set(implementation_identity) != expected_implementation_fields:
        raise FuryMultiseedProtocolError(
            "execution implementation identity fields mismatch"
        )
    for field, digest in implementation_identity.items():
        if digest is None and protocol_state != "SEALED_FOR_EXECUTION":
            continue
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"execution_contract.implementation_identity.{field} must be lowercase SHA-256"
            )
    if implementation_identity.get("runtime_snapshot_sha256") != runtime_identity.get(
        "snapshot_sha256"
    ):
        raise FuryMultiseedProtocolError(
            "execution implementation runtime snapshot identity mismatch"
        )
    if implementation_identity.get("python_source_closure_sha256") != (
        source_identity_contract.get("canonical_bundle_sha256")
    ):
        raise FuryMultiseedProtocolError(
            "execution implementation Python closure identity mismatch"
        )
    nonpromoting_test_baseline_receipts_used = False
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"required_baselines[{index}]")
        if entry.get("comparison_eligible") is True:
            _validate_baseline_comparison_readiness_receipt(
                entry,
                entry.get("comparison_eligibility_receipt"),
                runtime_snapshot_sha256=str(runtime_identity["snapshot_sha256"]),
                execution_bundle_identity=implementation_identity,
                allow_nonpromoting_test_fixture=(
                    _allow_nonpromoting_test_baseline_receipts
                ),
            )
            nonpromoting_test_baseline_receipts_used = True

    _validate_selection_inputs_and_evidence_against_protocol(
        value,
        source_closure_sha256=str(
            source_identity_contract["canonical_bundle_sha256"]
        ),
        implementation_identity=implementation_identity,
        selection_physical_replays=normalized_selection_replays,
        allow_nonpromoting_test_baseline_receipts=(
            _allow_nonpromoting_test_baseline_receipts
        ),
    )

    if final_binding.get("can_support_final_victory_claim") is True:
        required_take_first_n = _positive_int(
            final_corpus_contract.get("required_take_first_n"),
            "final_confirmation_corpus.required_take_first_n",
        )
        final_binding["final_evidence"] = (
            _validate_final_admission_against_protocol(
                final_binding.get("final_evidence"),
                discovery_capture_manifest_path=(
                    normalized_final_discovery_capture_path
                ),
                selection_contract=selection_contract,
                trusted_external_anchor_seal_sha256s=trusted_anchor_hashes,
                current_snapshot=current_snapshot,
                phase_binding=final_binding,
                final_seed_contract=raw_phases["final_confirmation"],
                source_closure_sha256=str(
                    source_identity_contract["canonical_bundle_sha256"]
                ),
                implementation_identity=implementation_identity,
                execution_contract=execution,
                required_take_first_n=required_take_first_n,
            )
        )
    elif normalized_final_discovery_capture_path is not None:
        raise FuryMultiseedProtocolError(
            "a non-final protocol cannot consume a final discovery capture manifest"
        )

    protocol_hash = sha256_json(value)
    return {
        "protocol": value,
        "protocol_sha256": protocol_hash,
        "seed_sets": seed_sets,
        "seed_set_sha256": {
            phase: sha256_json(seeds) for phase, seeds in seed_sets.items()
        },
        "required_baseline_ids": baseline_ids,
        "baseline_comparison_authority": {
            "status": (
                "NONPROMOTING_TEST_FIXTURE"
                if nonpromoting_test_baseline_receipts_used
                else "NO_PHYSICAL_BASELINE_EVIDENCE_VERIFIED"
            ),
            "typed_physical_production_evidence_verified": False,
            "victory_claim_capable": False,
        },
        "selection_replay_trust": {
            "status": (
                "PHYSICAL_REPLAY_VERIFIED"
                if selection_contract.get("status") == "CANDIDATE_SEALED"
                else "NOT_REQUIRED"
            ),
            "physical_replays": normalized_selection_replays,
            "physical_replay_package_count": len(normalized_selection_replays),
            "embedded_in_protocol": False,
        },
        "final_discovery_replay_trust": {
            "status": (
                "PHYSICAL_REPLAY_VERIFIED"
                if final_binding.get("can_support_final_victory_claim") is True
                else "NOT_REQUIRED"
            ),
            "physical_capture_manifest_path": (
                normalized_final_discovery_capture_path
            ),
            "physical_capture_manifest_file_sha256": (
                _file_sha256(
                    Path(normalized_final_discovery_capture_path),
                    "final discovery capture manifest",
                )
                if normalized_final_discovery_capture_path is not None
                else None
            ),
            "embedded_in_protocol": False,
        },
        "external_trust": {
            "trusted_external_anchor_seal_sha256s": list(
                trusted_anchor_hashes
            ),
            "trusted_external_anchor_seal_set_sha256": sha256_json(
                list(trusted_anchor_hashes)
            ),
            "embedded_in_protocol": False,
        },
    }


def load_protocol(
    path: Path,
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str] = (),
    selection_physical_replay_package_paths: Iterable[str | Path] = (),
    final_discovery_capture_manifest_path: str | Path | None = None,
) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryMultiseedProtocolError(f"could not load protocol {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise FuryMultiseedProtocolError("protocol JSON root must be an object")
    replay_packages: list[JSONMap] = []
    for package_path_value in selection_physical_replay_package_paths:
        package_path = _physical_json_path(
            package_path_value, "selection physical replay package"
        )
        package = _read_json_object(
            package_path, "selection physical replay package"
        )
        for field in (
            "shortlist_protocol_path",
            "runner_plan_path",
            "reduction_path",
            "analysis_path",
        ):
            raw_path = package.get(field)
            if isinstance(raw_path, str) and not Path(raw_path).is_absolute():
                package[field] = str(package_path.parent / raw_path)
        raw_manifests = package.get("shard_manifest_paths")
        if isinstance(raw_manifests, list):
            package["shard_manifest_paths"] = [
                (
                    str(package_path.parent / raw_path)
                    if isinstance(raw_path, str)
                    and not Path(raw_path).is_absolute()
                    else raw_path
                )
                for raw_path in raw_manifests
            ]
        replay_packages.append(package)
    return materialize_protocol(
        value,
        trusted_external_anchor_seal_sha256s=(
            trusted_external_anchor_seal_sha256s
        ),
        selection_physical_replays=replay_packages,
        final_discovery_capture_manifest_path=(
            final_discovery_capture_manifest_path
        ),
    )


def _nonpromoting_baseline_comparison_authority(
    materialized: Mapping[str, Any],
) -> JSONMap:
    authority = _mapping(
        materialized.get("baseline_comparison_authority"),
        "materialized.baseline_comparison_authority",
    )
    expected_fields = {
        "status",
        "typed_physical_production_evidence_verified",
        "victory_claim_capable",
    }
    if set(authority) != expected_fields:
        raise FuryMultiseedProtocolError(
            "baseline comparison authority field set mismatch"
        )
    if authority.get("status") not in {
        "NONPROMOTING_TEST_FIXTURE",
        "NO_PHYSICAL_BASELINE_EVIDENCE_VERIFIED",
    }:
        raise FuryMultiseedProtocolError(
            "unknown baseline comparison authority status"
        )
    if (
        authority.get("typed_physical_production_evidence_verified") is not False
        or authority.get("victory_claim_capable") is not False
    ):
        raise FuryMultiseedProtocolError(
            "current baseline receipt path cannot claim physical evidence or victory authority"
        )
    return dict(authority)


def build_plan(materialized: Mapping[str, Any]) -> JSONMap:
    """Build a non-executing receipt suitable for local or scheduler staging."""

    protocol = _mapping(materialized.get("protocol"), "materialized.protocol")
    seeds = _mapping(materialized.get("seed_sets"), "materialized.seed_sets")
    execution = _mapping(protocol.get("execution_contract"), "execution_contract")
    corpus_bindings = protocol["corpus_contract"]["phase_corpus_bindings"]
    baseline_readiness = [
        {
            "policy_id": entry["policy_id"],
            "comparison_eligible": entry["comparison_eligible"],
            "blocker": entry.get("blocker"),
        }
        for entry in protocol["baseline_contract"]["required_baselines"]
    ]
    baseline_authority = _nonpromoting_baseline_comparison_authority(
        materialized
    )
    common_blockers: list[str] = []
    if protocol.get("protocol_state") != "SEALED_FOR_EXECUTION":
        common_blockers.append("protocol_not_sealed_for_execution")
    common_blockers.extend(
        f"required_baseline_not_comparison_eligible:{entry['policy_id']}"
        for entry in baseline_readiness
        if entry["comparison_eligible"] is not True
    )
    if baseline_authority["typed_physical_production_evidence_verified"] is not True:
        common_blockers.append(
            "typed_physical_baseline_production_evidence_not_verified"
        )
    phase_readiness: dict[str, JSONMap] = {}
    for phase in REQUIRED_PHASES:
        binding = corpus_bindings[phase]
        blockers = list(common_blockers)
        selection_status = protocol["selection_contract"].get("status")
        if phase == "development":
            if protocol["development_candidate_registry"].get("status") != "FROZEN":
                blockers.append("development_candidate_registry_not_frozen")
        elif phase == "selection_validation":
            if selection_status not in {"SHORTLIST_FROZEN", "CANDIDATE_SEALED"}:
                blockers.append("selection_shortlist_not_frozen")
        elif protocol["candidate_contract"].get("status") != "FROZEN":
            blockers.append("candidate_policy_identity_not_frozen")
        if phase == "final_confirmation" and selection_status != "CANDIDATE_SEALED":
            blockers.append("candidate_selection_not_externally_sealed")
        if binding.get("status") != "FROZEN":
            blockers.append(f"phase_corpus_not_frozen:{phase}")
        if binding.get("comparison_eligible") is not True:
            blockers.append(f"phase_corpus_not_comparison_eligible:{phase}")
        if COMPARISON_INTENT not in binding.get("allowed_plan_intents", []):
            blockers.append(f"phase_corpus_disallows_comparison_plan:{phase}")
        if (
            phase == "final_confirmation"
            and binding.get("can_support_final_victory_claim") is not True
        ):
            blockers.append("final_corpus_not_untouched_claim_eligible")
        phase_readiness[phase] = {
            "corpus_status": binding.get("status"),
            "corpus_manifest_sha256": binding.get("corpus_manifest_sha256"),
            "dispatch_allowed": not blockers,
            "blockers": blockers,
        }
    blocked_reasons: list[str] = []
    for phase in REQUIRED_PHASES:
        for blocker in phase_readiness[phase]["blockers"]:
            if blocker not in blocked_reasons:
                blocked_reasons.append(blocker)
    return {
        "schema_version": 2,
        "kind": PLAN_KIND,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": materialized["protocol_sha256"],
        "protocol_state": protocol.get("protocol_state"),
        "seed_contract": {
            phase: {
                "count": len(values),
                "seed_list_sha256": sha256_json(values),
                "first_seed": values[0],
                "last_seed": values[-1],
            }
            for phase, values in seeds.items()
        },
        "baseline_contract": copy.deepcopy(protocol["baseline_contract"]),
        "corpus_contract": copy.deepcopy(protocol["corpus_contract"]),
        "evaluation_contract": copy.deepcopy(protocol["evaluation_contract"]),
        "execution_contract": copy.deepcopy(execution),
        "readiness": {
            "protocol_sealed_for_execution": (
                protocol.get("protocol_state") == "SEALED_FOR_EXECUTION"
            ),
            "candidate_status": protocol["candidate_contract"].get("status"),
            "selection_status": protocol["selection_contract"].get("status"),
            "development_candidate_registry_status": protocol[
                "development_candidate_registry"
            ].get("status"),
            "required_baselines": baseline_readiness,
            "phases": phase_readiness,
            "hpc_status": execution["scheduler_hpc"].get("status"),
            "hpc_is_optional_transport_not_statistical_evidence": True,
        },
        "execution_started": False,
        "rollout_count": 0,
        "victory_claim_allowed": False,
        "blocked_reasons": blocked_reasons,
        "next_gate": "close ordered full-policy baseline execution before dispatch",
    }


def _normalize_rollout(
    row: Mapping[str, Any],
    *,
    protocol_id: str,
    protocol_sha256: str,
    seed_namespace: str,
    phase: str,
) -> NormalizedRollout:
    missing_fields = [field for field in ROLLOUT_REQUIRED_FIELDS if field not in row]
    if missing_fields:
        raise FuryMultiseedProtocolError(
            f"rollout lacks required paired-runner fields: {missing_fields}"
        )
    if row.get("schema_version") != 2 or row.get("kind") != ROLLOUT_KIND:
        raise FuryMultiseedProtocolError(
            "rollout must be a v2 paired-runner row"
        )
    observed_row_digest = row.get("row_sha256")
    if not isinstance(observed_row_digest, str) or not _SHA256.fullmatch(
        observed_row_digest
    ):
        raise FuryMultiseedProtocolError("rollout row_sha256 must be lowercase SHA-256")
    unsigned_row = dict(row)
    unsigned_row.pop("row_sha256", None)
    if observed_row_digest != sha256_json(unsigned_row):
        raise FuryMultiseedProtocolError("rollout row SHA-256 mismatch")
    if row.get("protocol_id") != protocol_id:
        raise FuryMultiseedProtocolError("rollout protocol_id mismatch")
    if row.get("phase") != phase:
        raise FuryMultiseedProtocolError("rollout phase mismatch")
    if row.get("protocol_sha256") != protocol_sha256:
        raise FuryMultiseedProtocolError("rollout protocol_sha256 mismatch")
    text_fields: dict[str, str] = {}
    for field in (
        "instance_id",
        "component_id",
        "scenario_id",
        "stratum",
        "policy_id",
    ):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise FuryMultiseedProtocolError(f"rollout {field} must be nonempty")
        text_fields[field] = value
    if text_fields["stratum"] not in {"single_target", "multi_target"}:
        raise FuryMultiseedProtocolError(
            "rollout stratum must be single_target or multi_target"
        )
    execution_mode = row.get("execution_mode")
    if execution_mode not in {SINGLE_BRIDGE_MODE, SYNTHETIC_MODE}:
        raise FuryMultiseedProtocolError(
            "rollout execution_mode is unsupported"
        )
    master_seed = row.get("master_seed")
    simulator_seed = row.get("simulator_seed")
    for field, seed in (
        ("master_seed", master_seed),
        ("simulator_seed", simulator_seed),
    ):
        if isinstance(seed, bool) or not isinstance(seed, int) or seed <= 0:
            raise FuryMultiseedProtocolError(
                f"rollout {field} must be a positive integer"
            )
    digest_fields: dict[str, str] = {}
    for field in (
        "plan_sha256",
        "group_id",
        "corpus_manifest_sha256",
        "runner_inputs_sha256",
        "corpus_binding_sha256",
        "scenario_contract_sha256",
        "scenario_model_sha256",
        "target_context_bundle_sha256",
        "corpus_entry_sha256",
        "source_scenario_sha256",
        "catalog_sha256",
        "request_sha256",
        "policy_source_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
        "bridge_sha256",
        "execution_bundle_sha256",
        "end_state_sha256",
    ):
        digest = row.get(field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise FuryMultiseedProtocolError(
                f"rollout {field} must be lowercase SHA-256"
            )
        digest_fields[field] = digest
    full_policy_digest = row.get("full_policy_rollout_sha256")
    if execution_mode == SINGLE_BRIDGE_MODE:
        if not isinstance(full_policy_digest, str) or not _SHA256.fullmatch(
            full_policy_digest
        ):
            raise FuryMultiseedProtocolError(
                "production rollout must bind a full-policy artifact SHA-256"
            )
    elif full_policy_digest is not None:
        raise FuryMultiseedProtocolError(
            "synthetic rollout cannot claim a full-policy artifact SHA-256"
        )
    expected_simulator_seed = derive_simulator_seed(
        seed_namespace,
        master_seed,
        digest_fields["request_sha256"],
    )
    if simulator_seed != expected_simulator_seed:
        raise FuryMultiseedProtocolError(
            "rollout simulator_seed does not match master_seed/request derivation"
        )
    if row.get("seed") != master_seed:
        raise FuryMultiseedProtocolError(
            "rollout seed must preserve master_seed as the pairing label"
        )
    shard_index = row.get("shard_index")
    if (
        isinstance(shard_index, bool)
        or not isinstance(shard_index, int)
        or shard_index < 0
    ):
        raise FuryMultiseedProtocolError(
            "rollout shard_index must be a nonnegative integer"
        )
    expected_group_id = sha256_json(
        {
            "protocol_sha256": protocol_sha256,
            "corpus_manifest_sha256": digest_fields[
                "corpus_manifest_sha256"
            ],
            "phase": phase,
            "instance_id": text_fields["instance_id"],
            "scenario_id": text_fields["scenario_id"],
            "scenario_contract_sha256": digest_fields[
                "scenario_contract_sha256"
            ],
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
        }
    )
    if digest_fields["group_id"] != expected_group_id:
        raise FuryMultiseedProtocolError(
            "rollout group_id does not match the paired-group identity"
        )
    weight = _finite_number(row.get("scenario_weight"), "rollout scenario_weight")
    if weight <= 0:
        raise FuryMultiseedProtocolError("rollout scenario_weight must be positive")
    damage = _finite_number(row.get("damage"), "rollout damage")
    dps = _finite_number(row.get("dps"), "rollout dps")
    elapsed_ms = _positive_int(row.get("elapsed_ms"), "rollout elapsed_ms")
    horizon_ms = _positive_int(row.get("horizon_ms"), "rollout horizon_ms")
    if damage < 0 or dps < 0:
        raise FuryMultiseedProtocolError("rollout damage and dps must be nonnegative")
    if elapsed_ms > horizon_ms:
        raise FuryMultiseedProtocolError(
            "rollout elapsed_ms cannot exceed its planned horizon_ms"
        )
    computed_dps = damage * 1000.0 / elapsed_ms
    if abs(dps - computed_dps) > max(1e-6, abs(computed_dps) * 1e-9):
        raise FuryMultiseedProtocolError(
            "rollout dps is inconsistent with damage and elapsed_ms"
        )
    if row.get("plan_intent") != COMPARISON_INTENT:
        raise FuryMultiseedProtocolError(
            "statistical analysis requires an exact-comparison runner plan"
        )
    for field in (
        "completion_criterion_met",
        "contract_complete",
        "evaluation_eligible",
    ):
        if not isinstance(row.get(field), bool):
            raise FuryMultiseedProtocolError(f"rollout {field} must be boolean")
    omitted = row.get("omitted_lane_count")
    fatal = row.get("fatal_error_count")
    if (
        isinstance(omitted, bool)
        or not isinstance(omitted, int)
        or omitted < 0
        or isinstance(fatal, bool)
        or not isinstance(fatal, int)
        or fatal < 0
    ):
        raise FuryMultiseedProtocolError(
            "rollout omission and fatal-error counts must be nonnegative integers"
        )
    raw_reasons = row.get("nonfaithful_reason_counts")
    if not isinstance(raw_reasons, Mapping):
        raise FuryMultiseedProtocolError(
            "rollout nonfaithful_reason_counts must be an object"
        )
    reason_counts: list[tuple[str, int]] = []
    for raw_reason, raw_count in raw_reasons.items():
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            raise FuryMultiseedProtocolError(
                "rollout nonfaithful reason keys must be nonempty strings"
            )
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise FuryMultiseedProtocolError(
                "rollout nonfaithful reason counts must be nonnegative integers"
            )
        reason_counts.append((raw_reason, raw_count))
    completed_exact_horizon = (
        row["completion_criterion_met"] is True and elapsed_ms == horizon_ms
    )
    if row["completion_criterion_met"] is True and not completed_exact_horizon:
        raise FuryMultiseedProtocolError(
            "a completed duration-mode rollout must reach its exact planned horizon_ms"
        )
    derived_eligible = (
        completed_exact_horizon
        and omitted == 0
        and fatal == 0
        and not any(count > 0 for _, count in reason_counts)
    )
    if row["contract_complete"] != (
        completed_exact_horizon and omitted == 0 and fatal == 0
    ):
        raise FuryMultiseedProtocolError(
            "rollout contract_complete differs from its exact-horizon contract"
        )
    if row["evaluation_eligible"] is not derived_eligible:
        raise FuryMultiseedProtocolError(
            "rollout evaluation_eligible differs from the analyzer-derived "
            "completion/omission/fatal/nonfaithful contract"
        )
    return NormalizedRollout(
        plan_sha256=digest_fields["plan_sha256"],
        group_id=digest_fields["group_id"],
        protocol_id=protocol_id,
        protocol_sha256=protocol_sha256,
        corpus_manifest_sha256=digest_fields["corpus_manifest_sha256"],
        runner_inputs_sha256=digest_fields["runner_inputs_sha256"],
        corpus_binding_sha256=digest_fields["corpus_binding_sha256"],
        plan_intent=COMPARISON_INTENT,
        scenario_contract_sha256=digest_fields["scenario_contract_sha256"],
        scenario_model_sha256=digest_fields["scenario_model_sha256"],
        target_context_bundle_sha256=digest_fields[
            "target_context_bundle_sha256"
        ],
        corpus_entry_sha256=digest_fields["corpus_entry_sha256"],
        source_scenario_sha256=digest_fields["source_scenario_sha256"],
        catalog_sha256=digest_fields["catalog_sha256"],
        phase=phase,
        instance_id=text_fields["instance_id"],
        component_id=text_fields["component_id"],
        scenario_id=text_fields["scenario_id"],
        stratum=text_fields["stratum"],
        master_seed=master_seed,
        simulator_seed=simulator_seed,
        request_sha256=digest_fields["request_sha256"],
        policy_id=text_fields["policy_id"],
        policy_source_sha256=digest_fields["policy_source_sha256"],
        policy_adapter_sha256=digest_fields["policy_adapter_sha256"],
        policy_profile_sha256=digest_fields["policy_profile_sha256"],
        bridge_sha256=digest_fields["bridge_sha256"],
        execution_bundle_sha256=digest_fields["execution_bundle_sha256"],
        execution_mode=str(execution_mode),
        full_policy_rollout_sha256=(
            None if full_policy_digest is None else str(full_policy_digest)
        ),
        scenario_weight=weight,
        damage=damage,
        elapsed_ms=elapsed_ms,
        horizon_ms=horizon_ms,
        dps=dps,
        completion_criterion_met=bool(row["completion_criterion_met"]),
        contract_complete=bool(row["contract_complete"]),
        evaluation_eligible=bool(row["evaluation_eligible"]),
        omitted_lane_count=omitted,
        fatal_error_count=fatal,
        nonfaithful_reason_counts=tuple(sorted(reason_counts)),
        end_state_sha256=digest_fields["end_state_sha256"],
    )


def _relative_improvement_pct(rows: Sequence[tuple[float, float, float]]) -> float:
    candidate = sum(weight * value for weight, value, _ in rows)
    reference = sum(weight * value for weight, _, value in rows)
    if reference <= 0:
        raise FuryMultiseedProtocolError(
            "reference weighted DPS must be positive for relative comparison"
        )
    return 100.0 * (candidate - reference) / reference


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise FuryMultiseedProtocolError("cannot take a quantile of no values")
    if probability <= 0:
        return float(sorted_values[0])
    if probability >= 1:
        return float(sorted_values[-1])
    position = probability * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return float(sorted_values[low]) * (1.0 - fraction) + float(
        sorted_values[high]
    ) * fraction


def _cluster_bootstrap_lower_bound(
    pairs: Sequence[tuple[NormalizedRollout, NormalizedRollout]],
    *,
    cluster: str,
    replicates: int,
    alpha: float,
    bootstrap_seed: int,
) -> float:
    """Resample preaggregated master-seed or leakage-component clusters.

    A scenario-by-seed row is not an independent observation.  All scenarios
    attached to a cluster are reduced before resampling, so no bootstrap loop
    scales as ``replicates * scenario rows``.
    """

    if cluster not in {"master_seed", "component"}:
        raise FuryMultiseedProtocolError(f"unsupported bootstrap cluster: {cluster}")
    totals: dict[int | str, list[float]] = {}
    for candidate, reference in pairs:
        key: int | str = (
            candidate.master_seed
            if cluster == "master_seed"
            else candidate.component_id
        )
        summary = totals.setdefault(key, [0.0, 0.0])
        summary[0] += candidate.scenario_weight * candidate.dps
        summary[1] += candidate.scenario_weight * reference.dps
    keys = sorted(totals, key=str)
    if not keys:
        raise FuryMultiseedProtocolError(f"bootstrap has no {cluster} clusters")
    rng = random.Random(bootstrap_seed)
    estimates: list[float] = []
    for _ in range(replicates):
        candidate_total = 0.0
        reference_total = 0.0
        for key in rng.choices(keys, k=len(keys)):
            candidate_value, reference_value = totals[key]
            candidate_total += candidate_value
            reference_total += reference_value
        if reference_total <= 0:
            raise FuryMultiseedProtocolError(
                f"{cluster} bootstrap sampled nonpositive reference DPS"
            )
        estimates.append(
            100.0 * (candidate_total - reference_total) / reference_total
        )
    estimates.sort()
    return _quantile(estimates, alpha)


def _bootstrap_lower_bound_summary(
    estimates: Sequence[float],
    *,
    alpha: float,
) -> JSONMap:
    """Summarize one fixed bootstrap run without adapting the voting gate.

    The odd/even split is a deterministic, non-voting Monte Carlo diagnostic.
    It is deliberately reported after the full-run bound has been fixed and
    never changes the replicate count, alpha, lower bound, or pass/fail rule.
    """

    if not estimates:
        raise FuryMultiseedProtocolError("bootstrap produced no estimates")
    ordered = sorted(float(value) for value in estimates)
    first_partition = sorted(float(value) for value in estimates[0::2])
    second_partition = sorted(float(value) for value in estimates[1::2])
    first_bound = _quantile(first_partition, alpha)
    second_bound = (
        _quantile(second_partition, alpha) if second_partition else None
    )
    stability: JSONMap = {
        "noninferential": True,
        "used_by_gate": False,
        "method": "fixed_interleaved_odd_even_replicate_split",
        "first_partition_replicates": len(first_partition),
        "second_partition_replicates": len(second_partition),
        "first_partition_lower_bound_pct": first_bound,
        "second_partition_lower_bound_pct": second_bound,
        "absolute_split_lower_bound_difference_pct": (
            abs(first_bound - second_bound)
            if second_bound is not None
            else None
        ),
    }
    return {
        "lower_bound_pct": _quantile(ordered, alpha),
        # B * alpha is the expected number of Monte Carlo draws in the
        # estimated lower tail.  It is descriptive and never a hidden gate.
        "effective_tail_draws": len(ordered) * alpha,
        "monte_carlo_stability_diagnostic": stability,
    }


def _crossed_pigeonhole_bootstrap_summary(
    pairs: Sequence[tuple[NormalizedRollout, NormalizedRollout]],
    *,
    replicates: int,
    alpha: float,
    bootstrap_seed: int,
) -> JSONMap:
    """Jointly resample seed and corpus-component clusters.

    Master-seed labels and guild/player leakage-component labels are sampled
    independently with replacement.  Every observed seed-by-component cell is
    then assigned the *product* of those two cluster multiplicities.  Each
    replicate retains the primary ratio-of-scenario-weighted-sums estimand;
    it never averages row-level percentage changes or treats scenario rows as
    independent observations.  This is the crossed/pigeonhole bootstrap used
    by the voting lower bound.
    """

    cell_totals: dict[tuple[int, str], list[float]] = {}
    seed_keys: set[int] = set()
    component_keys: set[str] = set()
    for candidate, reference in pairs:
        seed = candidate.master_seed
        component = candidate.component_id
        seed_keys.add(seed)
        component_keys.add(component)
        summary = cell_totals.setdefault((seed, component), [0.0, 0.0])
        summary[0] += candidate.scenario_weight * candidate.dps
        summary[1] += candidate.scenario_weight * reference.dps
    ordered_seeds = sorted(seed_keys)
    ordered_components = sorted(component_keys)
    if not ordered_seeds or not ordered_components:
        raise FuryMultiseedProtocolError(
            "crossed bootstrap requires seed and component clusters"
        )

    seed_index = {seed: index for index, seed in enumerate(ordered_seeds)}
    component_index = {
        component: index
        for index, component in enumerate(ordered_components)
    }
    seed_count = len(ordered_seeds)
    component_count = len(ordered_components)

    # The scalar definition is m_seed' A m_component.  Scanning every
    # observed cell in Python for every replicate makes a 100,000-replicate
    # production run scale disastrously.  Materialize A once and project its
    # longer axis in the C-level sumprod kernel, leaving only the smaller
    # cluster axis in Python.  Missing cells remain exact zeroes, so sparse and
    # unbalanced crossed tables retain the same multiplicity-product estimand.
    project_over_components = component_count <= seed_count
    outer_count = component_count if project_over_components else seed_count
    inner_count = seed_count if project_over_components else component_count
    candidate_vectors = [[0.0] * inner_count for _ in range(outer_count)]
    reference_vectors = [[0.0] * inner_count for _ in range(outer_count)]
    for (seed, component), totals in cell_totals.items():
        seed_position = seed_index[seed]
        component_position = component_index[component]
        if project_over_components:
            outer_position = component_position
            inner_position = seed_position
        else:
            outer_position = seed_position
            inner_position = component_position
        candidate_vectors[outer_position][inner_position] = totals[0]
        reference_vectors[outer_position][inner_position] = totals[1]

    seed_population = range(seed_count)
    component_population = range(component_count)
    rng = random.Random(bootstrap_seed)
    estimates: list[float] = []
    for _ in range(replicates):
        # Sampling integer positions consumes the same Random.choices stream
        # as sampling the ordered labels directly, while dense multiplicity
        # vectors avoid a dictionary lookup for every crossed cell.
        seed_multiplicity = [0] * seed_count
        for position in rng.choices(seed_population, k=seed_count):
            seed_multiplicity[position] += 1
        component_multiplicity = [0] * component_count
        for position in rng.choices(
            component_population, k=component_count
        ):
            component_multiplicity[position] += 1

        if project_over_components:
            outer_multiplicity = component_multiplicity
            inner_multiplicity = seed_multiplicity
        else:
            outer_multiplicity = seed_multiplicity
            inner_multiplicity = component_multiplicity
        candidate_total = 0.0
        reference_total = 0.0
        for position, multiplicity in enumerate(outer_multiplicity):
            if multiplicity:
                candidate_total += multiplicity * _sumprod(
                    inner_multiplicity,
                    candidate_vectors[position],
                )
                reference_total += multiplicity * _sumprod(
                    inner_multiplicity,
                    reference_vectors[position],
                )
        if reference_total <= 0:
            raise FuryMultiseedProtocolError(
                "crossed bootstrap sampled nonpositive reference DPS"
            )
        estimates.append(
            100.0 * (candidate_total - reference_total) / reference_total
        )

    summary = _bootstrap_lower_bound_summary(estimates, alpha=alpha)
    summary.update(
        {
            "method": "crossed_pigeonhole_seed_x_component_multiplicity_product",
            "estimand": "ratio_of_scenario_weighted_candidate_and_reference_sums",
            "seed_cluster_count": len(ordered_seeds),
            "component_cluster_count": len(ordered_components),
            "observed_seed_component_cell_count": len(cell_totals),
        }
    )
    return summary


def _comparison_seed(base: int, candidate: str, baseline: str, stratum: str) -> int:
    payload = f"{base}\0{candidate}\0{baseline}\0{stratum}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def analyze_rollouts(
    materialized: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    runner_plan: Mapping[str, Any],
    reduction_receipt: Mapping[str, Any],
    expected_protocol_sha256: str | None,
    phase: str,
    candidate_id: str,
    manifest_paths: Iterable[str | Path] | None = None,
    _allow_nonpromoting_test_selection_replay_authority: bool = False,
) -> JSONMap:
    """Apply the frozen paired multi-seed gate to compact rollout rows."""

    if phase not in REQUIRED_PHASES:
        raise FuryMultiseedProtocolError(f"unknown evaluation phase: {phase}")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise FuryMultiseedProtocolError("candidate_id must be nonempty")
    protocol = _mapping(materialized.get("protocol"), "materialized.protocol")
    if materialized.get("protocol_sha256") != sha256_json(dict(protocol)):
        raise FuryMultiseedProtocolError(
            "materialized protocol envelope was modified after validation"
        )
    seed_namespace = str(protocol["seed_contract"]["namespace"])
    phase_seed_contracts = _mapping(
        protocol["seed_contract"].get("phases"), "seed_contract.phases"
    )
    observed_seed_sets = _mapping(
        materialized.get("seed_sets"), "materialized.seed_sets"
    )
    observed_seed_hashes = _mapping(
        materialized.get("seed_set_sha256"), "materialized.seed_set_sha256"
    )
    if set(observed_seed_sets) != set(REQUIRED_PHASES) or set(
        observed_seed_hashes
    ) != set(REQUIRED_PHASES):
        raise FuryMultiseedProtocolError(
            "materialized protocol envelope was modified after validation"
        )
    for registered_phase in REQUIRED_PHASES:
        registered_seed = _mapping(
            phase_seed_contracts.get(registered_phase),
            f"seed_contract.phases.{registered_phase}",
        )
        counter_start = registered_seed.get("counter_start", 0)
        if (
            isinstance(counter_start, bool)
            or not isinstance(counter_start, int)
            or counter_start < 0
        ):
            raise FuryMultiseedProtocolError(
                "materialized protocol envelope was modified after validation"
            )
        expected_seed_set = list(
            derive_seed_set(
                seed_namespace,
                registered_phase,
                _positive_int(
                    registered_seed.get("count"),
                    f"seed_contract.phases.{registered_phase}.count",
                ),
                counter_start=counter_start,
            )
        )
        if (
            observed_seed_sets.get(registered_phase) != expected_seed_set
            or observed_seed_hashes.get(registered_phase)
            != sha256_json(expected_seed_set)
        ):
            raise FuryMultiseedProtocolError(
                "materialized protocol envelope was modified after validation"
            )
    external_trust = _mapping(
        materialized.get("external_trust"), "materialized.external_trust"
    )
    selection_replay_trust = _mapping(
        materialized.get("selection_replay_trust"),
        "materialized.selection_replay_trust",
    )
    final_discovery_replay_trust = _mapping(
        materialized.get("final_discovery_replay_trust"),
        "materialized.final_discovery_replay_trust",
    )
    baseline_authority = _nonpromoting_baseline_comparison_authority(
        materialized
    )
    test_only_selection_replay_authority = (
        _allow_nonpromoting_test_selection_replay_authority is True
    )
    if test_only_selection_replay_authority and (
        phase != "selection_validation"
        or baseline_authority["status"] != "NONPROMOTING_TEST_FIXTURE"
        or baseline_authority["typed_physical_production_evidence_verified"]
        is not False
        or baseline_authority["victory_claim_capable"] is not False
    ):
        raise FuryMultiseedProtocolError(
            "private non-promoting selection replay authority is restricted "
            "to selection-validation unit-test envelopes"
        )
    canonical_materialized = materialize_protocol(
        copy.deepcopy(dict(protocol)),
        trusted_external_anchor_seal_sha256s=_sequence(
            external_trust.get("trusted_external_anchor_seal_sha256s"),
            "materialized trusted external anchor seals",
        ),
        selection_physical_replays=_sequence(
            selection_replay_trust.get("physical_replays"),
            "materialized selection physical replays",
        ),
        final_discovery_capture_manifest_path=(
            final_discovery_replay_trust.get("physical_capture_manifest_path")
        ),
        _allow_nonpromoting_test_baseline_receipts=(
            baseline_authority["status"] == "NONPROMOTING_TEST_FIXTURE"
        ),
    )
    if dict(materialized) != canonical_materialized:
        raise FuryMultiseedProtocolError(
            "materialized protocol envelope was modified after validation"
        )
    if (
        not isinstance(expected_protocol_sha256, str)
        or not _SHA256.fullmatch(expected_protocol_sha256)
    ):
        raise FuryMultiseedProtocolError(
            "analysis requires an externally pinned expected_protocol_sha256"
        )
    if expected_protocol_sha256 != canonical_materialized["protocol_sha256"]:
        raise FuryMultiseedProtocolError(
            "protocol differs from the externally pinned execution seal"
        )
    protocol_id = str(protocol["protocol_id"])
    protocol_sha256 = str(materialized["protocol_sha256"])
    expected_seeds = set(materialized["seed_sets"][phase])
    phase_corpus_binding = protocol["corpus_contract"][
        "phase_corpus_bindings"
    ][phase]
    if (
        phase_corpus_binding.get("comparison_eligible") is not True
        or COMPARISON_INTENT
        not in phase_corpus_binding.get("allowed_plan_intents", [])
    ):
        raise FuryMultiseedProtocolError(
            f"phase {phase} corpus is not eligible for a scientific comparison plan"
        )
    baseline_entries = protocol["baseline_contract"]["required_baselines"]
    baseline_ids = [str(entry["policy_id"]) for entry in baseline_entries]
    if candidate_id in baseline_ids:
        raise FuryMultiseedProtocolError("candidate_id is also a required baseline")

    try:
        validated_runner_plan = validate_runner_plan(runner_plan)
        validated_row_maps = validate_reduction_receipt(
            validated_runner_plan,
            reduction_receipt,
            rows,
            manifest_paths=manifest_paths,
        )
    except (FuryPairedRunnerError, TypeError, ValueError) as error:
        raise FuryMultiseedProtocolError(
            "runner plan, reduction receipt, or exact rollout Cartesian product "
            f"is invalid: {error}"
        ) from error
    runner_contract = _mapping(
        validated_runner_plan.get("contract"), "runner_plan.contract"
    )
    expected_runner_values = {
        "protocol_id": protocol_id,
        "protocol_sha256": protocol_sha256,
        "phase": phase,
        "corpus_manifest_sha256": phase_corpus_binding.get(
            "corpus_manifest_sha256"
        ),
        "runner_inputs_sha256": phase_corpus_binding.get("runner_inputs_sha256"),
        "runner_scenario_bundle_sha256": phase_corpus_binding.get(
            "runner_scenario_bundle_sha256"
        ),
        "scenario_model_bundle_sha256": phase_corpus_binding.get(
            "scenario_model_bundle_sha256"
        ),
        "target_context_bundle_set_sha256": phase_corpus_binding.get(
            "target_context_bundle_set_sha256"
        ),
        "corpus_binding_sha256": phase_corpus_binding.get(
            "corpus_binding_sha256"
        ),
        "execution_mode": protocol["execution_contract"][
            "required_runner_execution_mode"
        ],
        "plan_intent": COMPARISON_INTENT,
    }
    for field, expected in expected_runner_values.items():
        if runner_contract.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"runner plan {field} differs from the frozen protocol binding"
            )
    expected_seed_list = list(materialized["seed_sets"][phase])
    if runner_contract["seed_derivation"].get("master_seeds") != expected_seed_list:
        raise FuryMultiseedProtocolError(
            "runner plan does not contain the exact phase master-seed list"
        )
    expected_scenario_count = phase_corpus_binding.get("runner_scenario_count")
    if (
        isinstance(expected_scenario_count, bool)
        or not isinstance(expected_scenario_count, int)
        or expected_scenario_count <= 0
        or len(runner_contract["scenarios"]) != expected_scenario_count
    ):
        raise FuryMultiseedProtocolError(
            "runner plan scenario count differs from the frozen runner inputs"
        )

    candidate_contract = protocol["candidate_contract"]
    if phase == "development":
        registry = protocol["development_candidate_registry"]
        if registry.get("status") != "FROZEN":
            raise FuryMultiseedProtocolError(
                "development analysis requires a frozen candidate registry"
            )
        matching_candidates = [
            row for row in registry["candidates"] if row["policy_id"] == candidate_id
        ]
        if len(matching_candidates) != 1:
            raise FuryMultiseedProtocolError(
                "development candidate is absent from the frozen registry"
            )
        analyzed_candidate_identity = matching_candidates[0]
    elif phase == "selection_validation":
        selection_contract = protocol["selection_contract"]
        if selection_contract.get("status") not in {
            "SHORTLIST_FROZEN",
            "CANDIDATE_SEALED",
        }:
            raise FuryMultiseedProtocolError(
                "selection-validation requires a frozen complete shortlist"
            )
        shortlist_candidates = selection_contract["frozen_shortlist"][
            "candidates"
        ]
        matching_candidates = [
            row for row in shortlist_candidates if row["policy_id"] == candidate_id
        ]
        if len(matching_candidates) != 1:
            raise FuryMultiseedProtocolError(
                "selection-validation candidate is absent from the frozen shortlist"
            )
        analyzed_candidate_identity = matching_candidates[0]
    else:
        if candidate_contract.get("status") != "FROZEN":
            raise FuryMultiseedProtocolError(
                "final analysis requires a sealed candidate identity"
            )
        if candidate_contract.get("policy_id") != candidate_id:
            raise FuryMultiseedProtocolError(
                "analyzed candidate differs from the sealed candidate"
            )
        analyzed_candidate_identity = candidate_contract
    expected_policies: list[dict[str, str]] = []
    for entry in baseline_entries:
        expected_policies.append(
            {
                "policy_id": str(entry["policy_id"]),
                "source_sha256": str(entry["source_bundle_sha256"]),
                "adapter_sha256": str(entry["policy_adapter_sha256"]),
                "profile_sha256": str(entry["policy_profile_sha256"]),
                "role": "BASELINE",
            }
        )
    expected_policies.append(
        {
            "policy_id": candidate_id,
            "source_sha256": str(
                analyzed_candidate_identity["policy_source_sha256"]
            ),
            "adapter_sha256": str(
                analyzed_candidate_identity["policy_adapter_sha256"]
            ),
            "profile_sha256": str(
                analyzed_candidate_identity["policy_profile_sha256"]
            ),
            "role": "CANDIDATE",
        }
    )
    if runner_contract.get("policies") != expected_policies:
        raise FuryMultiseedProtocolError(
            "runner plan policy source/adapter/profile identities differ from the frozen protocol"
        )
    implementation_identity = protocol["execution_contract"].get(
        "implementation_identity"
    )
    if runner_contract.get("execution_bundle_identity") != implementation_identity:
        raise FuryMultiseedProtocolError(
            "runner plan execution bundle differs from the frozen protocol"
        )

    normalized = [
        _normalize_rollout(
            row,
            protocol_id=protocol_id,
            protocol_sha256=protocol_sha256,
            seed_namespace=seed_namespace,
            phase=phase,
        )
        for row in validated_row_maps
    ]
    if not normalized:
        raise FuryMultiseedProtocolError("rollout input is empty")
    required_policy_ids = {candidate_id, *baseline_ids}
    selected = [row for row in normalized if row.policy_id in required_policy_ids]
    if {row.policy_id for row in selected} != required_policy_ids:
        missing = sorted(required_policy_ids.difference(row.policy_id for row in selected))
        raise FuryMultiseedProtocolError(
            f"rollout input lacks required policies: {missing}"
        )

    by_policy: dict[str, dict[tuple[str, str, int], NormalizedRollout]] = {}
    for row in selected:
        policy_rows = by_policy.setdefault(row.policy_id, {})
        if row.pair_key in policy_rows:
            raise FuryMultiseedProtocolError(
                f"duplicate rollout key for {row.policy_id}: {row.pair_key}"
            )
        policy_rows[row.pair_key] = row
    candidate = by_policy[candidate_id]
    for policy_id in baseline_ids:
        if by_policy[policy_id].keys() != candidate.keys():
            raise FuryMultiseedProtocolError(
                f"paired rollout keys do not match for {candidate_id} and {policy_id}"
            )

    scenario_seeds: dict[tuple[str, str, str], set[int]] = {}
    scenario_contract: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in selected:
        scenario_seeds.setdefault(
            (row.policy_id, row.instance_id, row.scenario_id), set()
        ).add(row.master_seed)
        key = (row.instance_id, row.scenario_id)
        contract = (
            row.component_id,
            row.stratum,
            row.scenario_weight,
            row.request_sha256,
            row.horizon_ms,
            row.corpus_manifest_sha256,
            row.corpus_binding_sha256,
            row.scenario_contract_sha256,
            row.scenario_model_sha256,
            row.target_context_bundle_sha256,
            row.corpus_entry_sha256,
            row.source_scenario_sha256,
            row.catalog_sha256,
            row.bridge_sha256,
            row.plan_intent,
        )
        previous = scenario_contract.setdefault(key, contract)
        if previous != contract:
            raise FuryMultiseedProtocolError(
                f"scenario metadata differs across policies for {key}"
            )
    for key in candidate:
        group = [by_policy[policy_id][key] for policy_id in required_policy_ids]
        if len({row.request_sha256 for row in group}) != 1:
            raise FuryMultiseedProtocolError(
                f"request hash differs inside paired group {key}"
            )
        if len({row.simulator_seed for row in group}) != 1:
            raise FuryMultiseedProtocolError(
                f"simulator seed differs inside paired group {key}"
            )
        if len({row.bridge_sha256 for row in group}) != 1:
            raise FuryMultiseedProtocolError(
                f"bridge hash differs inside paired group {key}"
            )
        if len({row.group_id for row in group}) != 1:
            raise FuryMultiseedProtocolError(
                f"group identity differs inside paired group {key}"
            )
    incomplete = [
        key
        for key, seeds in scenario_seeds.items()
        if seeds != expected_seeds
    ]
    if incomplete:
        raise FuryMultiseedProtocolError(
            f"{len(incomplete)} policy/scenario rows do not contain the exact phase seed set"
        )
    present_strata = {row.stratum for row in candidate.values()}
    if present_strata != {"single_target", "multi_target"}:
        raise FuryMultiseedProtocolError(
            "evaluation corpus must contain both single_target and multi_target rows"
        )

    minimum_horizon = int(
        protocol["evaluation_contract"]["primary_minimum_horizon_ms"]
    )
    short_rows = [row for row in candidate.values() if row.horizon_ms < minimum_horizon]
    if short_rows:
        raise FuryMultiseedProtocolError(
            f"primary DPS corpus contains {len(short_rows)} rows below "
            f"minimum horizon {minimum_horizon} ms"
        )

    blocked_reasons: list[str] = []
    if (
        baseline_authority["typed_physical_production_evidence_verified"] is not True
        and not test_only_selection_replay_authority
    ):
        blocked_reasons.append(
            "typed physical baseline production evidence is not verified"
        )
    observed_plan_digests = {row.plan_sha256 for row in selected}
    if len(observed_plan_digests) != 1:
        blocked_reasons.append("one evaluation mixes multiple runner plans")
    if protocol.get("protocol_state") != "SEALED_FOR_EXECUTION":
        blocked_reasons.append(
            "evaluation protocol is not SEALED_FOR_EXECUTION"
        )
    corpus_status = phase_corpus_binding.get("status")
    expected_corpus_digest = phase_corpus_binding.get("corpus_manifest_sha256")
    observed_corpus_digests = {
        row.corpus_manifest_sha256 for row in selected
    }
    if len(observed_corpus_digests) != 1:
        blocked_reasons.append("one evaluation mixes multiple corpus manifests")
    if corpus_status != "FROZEN":
        blocked_reasons.append(
            f"phase {phase} corpus is not frozen ({corpus_status})"
        )
    elif observed_corpus_digests != {expected_corpus_digest}:
        blocked_reasons.append(
            f"phase {phase} rollout corpus differs from its frozen manifest"
        )
    if (
        phase == "final_confirmation"
        and phase_corpus_binding.get("can_support_final_victory_claim") is not True
    ):
        blocked_reasons.append(
            "final-confirmation corpus is not authorized as untouched final evidence"
        )
    if phase == "final_confirmation":
        final_evidence = phase_corpus_binding.get("final_evidence")
        final_contract = protocol["corpus_contract"]["final_confirmation_corpus"]
        if not isinstance(final_evidence, Mapping):
            blocked_reasons.append(
                "final-confirmation corpus lacks a typed admission receipt"
            )
        else:
            # materialize_protocol has already replayed and content-validated
            # the complete closed discovery universe using the caller's
            # out-of-band trusted-anchor allowlist.
            admitted_rows = final_evidence["selection_replay"][
                "selected_instances"
            ]
            admitted_components_by_instance = {
                str(item["instance_id"]): set(
                    item["guild_player_component_sha256s"]
                )
                for item in admitted_rows
            }
            planned_components_by_instance: dict[str, set[str]] = {}
            for item in runner_contract["scenarios"]:
                planned_components_by_instance.setdefault(
                    str(item["instance_id"]), set()
                ).add(
                    hashlib.sha256(
                        str(item["component_id"]).encode("utf-8")
                    ).hexdigest()
                )
            if admitted_components_by_instance != planned_components_by_instance:
                blocked_reasons.append(
                    "final corpus admitted instance/component membership differs "
                    "from the runner plan"
                )
            planned_instances = len(planned_components_by_instance)
            required_instances = int(final_contract["required_take_first_n"])
            if planned_instances != required_instances:
                blocked_reasons.append(
                    "final corpus instance count does not equal required_take_first_n"
                )
            planned_components = len(
                {
                    component_id
                    for component_ids in planned_components_by_instance.values()
                    for component_id in component_ids
                }
            )
            minimum_final_components = int(
                final_contract["minimum_guild_player_leakage_component_count"]
            )
            if planned_components < minimum_final_components:
                blocked_reasons.append(
                    "final corpus leakage-component count does not match the plan or minimum"
                )
    component_minimums = protocol["evaluation_contract"][
        "minimum_component_count_by_phase_and_stratum"
    ][phase]
    for stratum in protocol["evaluation_contract"]["required_strata"]:
        stratum_rows = (
            selected
            if stratum == "overall"
            else [row for row in selected if row.stratum == stratum]
        )
        observed_component_count = len(
            {row.component_id for row in stratum_rows}
        )
        minimum_component_count = int(component_minimums[stratum])
        if observed_component_count < minimum_component_count:
            blocked_reasons.append(
                f"phase {phase}/{stratum} has {observed_component_count} corpus "
                f"components; protocol requires {minimum_component_count}"
            )
    candidate_contract = protocol["candidate_contract"]
    if phase == "development":
        if protocol["development_candidate_registry"].get("status") != "FROZEN":
            blocked_reasons.append(
                "development candidate registry is not frozen"
            )
    elif phase == "selection_validation":
        if protocol["selection_contract"].get("status") not in {
            "SHORTLIST_FROZEN",
            "CANDIDATE_SEALED",
        }:
            blocked_reasons.append(
                "selection-validation shortlist is not frozen"
            )
    elif candidate_contract["status"] != "FROZEN":
        blocked_reasons.append(
            "candidate policy/source identity is not frozen"
        )
    elif candidate_contract["policy_id"] != candidate_id:
        blocked_reasons.append(
            f"analyzed candidate {candidate_id} differs from frozen candidate "
            f"{candidate_contract['policy_id']}"
        )
    for entry in baseline_entries:
        if not bool(entry["comparison_eligible"]):
            blocked_reasons.append(
                f"required baseline {entry['policy_id']} is not comparison-eligible: "
                f"{entry.get('blocker') or 'unspecified blocker'}"
            )
    expected_policy_sources = {
        str(entry["policy_id"]): str(entry["source_bundle_sha256"])
        for entry in baseline_entries
    }
    expected_policy_sources[candidate_id] = str(
        analyzed_candidate_identity["policy_source_sha256"]
    )
    for policy_id, expected_source in expected_policy_sources.items():
        observed_sources = {
            row.policy_source_sha256 for row in by_policy[policy_id].values()
        }
        if observed_sources != {expected_source}:
            blocked_reasons.append(
                f"policy {policy_id} source hash differs from its frozen identity"
            )

    observed_bridges = {row.bridge_sha256 for row in selected}
    allowed_bridges = {
        str(protocol["execution_contract"]["windows_local"]["bridge_sha256"])
    }
    linux_bridge = protocol["execution_contract"]["scheduler_hpc"].get(
        "linux_bridge_sha256"
    )
    if isinstance(linux_bridge, str) and _SHA256.fullmatch(linux_bridge):
        allowed_bridges.add(linux_bridge)
    if len(observed_bridges) != 1:
        blocked_reasons.append(
            "one evaluation mixes multiple simulator bridge binaries"
        )
    elif not observed_bridges.issubset(allowed_bridges):
        blocked_reasons.append(
            "rollout bridge hash is not pinned by the protocol"
        )
    for policy_id in sorted(required_policy_ids):
        bad = [
            row
            for row in by_policy[policy_id].values()
            if not row.completion_criterion_met
            or not row.evaluation_eligible
            or row.omitted_lane_count
            or row.fatal_error_count
            or any(count for _, count in row.nonfaithful_reason_counts)
        ]
        if bad:
            blocked_reasons.append(
                f"policy {policy_id} has {len(bad)} incomplete or nonfaithful rollouts"
            )

    statistics = protocol["evaluation_contract"]["statistics"]
    strata = protocol["evaluation_contract"]["required_strata"]
    # Each baseline/stratum comparison contributes exactly one voting bound:
    # one crossed bootstrap jointly reweights seed and leakage-component
    # clusters.  Marginal one-axis resamples below are diagnostics only and do
    # not enlarge the preregistered Bonferroni family.
    test_count = len(baseline_ids) * len(strata)
    familywise_alpha = 1.0 - float(statistics["confidence_level"])
    per_test_alpha = familywise_alpha / test_count
    replicates = int(statistics["bootstrap_replicates"])
    base_bootstrap_seed = int(statistics["bootstrap_seed"])
    practical_by_stratum = statistics[
        "minimum_practical_mean_improvement_pct_by_stratum"
    ]
    required_lower = float(statistics["required_simultaneous_lower_bound_pct"])

    comparisons: list[JSONMap] = []
    for baseline_id in baseline_ids:
        for stratum in strata:
            practical = float(practical_by_stratum[stratum])
            pairs = [
                (candidate[key], by_policy[baseline_id][key])
                for key in sorted(candidate)
                if stratum == "overall" or candidate[key].stratum == stratum
            ]
            values = [
                (left.scenario_weight, left.dps, right.dps)
                for left, right in pairs
            ]
            point = _relative_improvement_pct(values)
            crossed_bootstrap = _crossed_pigeonhole_bootstrap_summary(
                pairs,
                replicates=replicates,
                alpha=per_test_alpha,
                bootstrap_seed=_comparison_seed(
                    base_bootstrap_seed,
                    candidate_id,
                    baseline_id,
                    stratum,
                ),
            )
            crossed_lower_bound = float(crossed_bootstrap["lower_bound_pct"])
            # These one-axis bounds can help diagnose whether simulator noise
            # or corpus leakage-component variation dominates.  They are not
            # inferential outputs and are never consulted by comparison_passed.
            seed_diagnostic_lower_bound = _cluster_bootstrap_lower_bound(
                pairs,
                cluster="master_seed",
                replicates=replicates,
                alpha=per_test_alpha,
                bootstrap_seed=_comparison_seed(
                    base_bootstrap_seed + 1,
                    candidate_id,
                    baseline_id,
                    stratum,
                ),
            )
            component_diagnostic_lower_bound = _cluster_bootstrap_lower_bound(
                pairs,
                cluster="component",
                replicates=replicates,
                alpha=per_test_alpha,
                bootstrap_seed=_comparison_seed(
                    base_bootstrap_seed + 2,
                    candidate_id,
                    baseline_id,
                    stratum,
                ),
            )
            deltas = [left.dps - right.dps for left, right in pairs]
            comparison_passed = (
                point >= practical
                and crossed_lower_bound > required_lower
                and not blocked_reasons
            )
            comparisons.append(
                {
                    "candidate_id": candidate_id,
                    "baseline_id": baseline_id,
                    "stratum": stratum,
                    "pair_count": len(pairs),
                    "instance_count": len({left.instance_id for left, _ in pairs}),
                    "component_count": len({left.component_id for left, _ in pairs}),
                    "scenario_count": len(
                        {(left.instance_id, left.scenario_id) for left, _ in pairs}
                    ),
                    "seed_count": len({left.master_seed for left, _ in pairs}),
                    "paired_relative_dps_improvement_pct": point,
                    "crossed_seed_component_lower_bound_pct": crossed_lower_bound,
                    "simultaneous_one_sided_lower_bound_pct": crossed_lower_bound,
                    "crossed_seed_component_bootstrap": crossed_bootstrap,
                    "diagnostic_nonvoting": {
                        "noninferential": True,
                        "used_by_gate": False,
                        "simulator_seed_marginal_lower_bound_pct": (
                            seed_diagnostic_lower_bound
                        ),
                        "corpus_component_marginal_lower_bound_pct": (
                            component_diagnostic_lower_bound
                        ),
                        "reference_alpha": per_test_alpha,
                    },
                    "minimum_practical_mean_improvement_pct": practical,
                    "required_lower_bound_pct": required_lower,
                    "descriptive_pair_row_noninferential": True,
                    "descriptive_pair_row_wins": sum(
                        value > 1e-9 for value in deltas
                    ),
                    "descriptive_pair_row_ties": sum(
                        abs(value) <= 1e-9 for value in deltas
                    ),
                    "descriptive_pair_row_losses": sum(
                        value < -1e-9 for value in deltas
                    ),
                    "descriptive_pair_row_mean_unweighted_dps_delta": fmean(
                        deltas
                    ),
                    "comparison_passed": comparison_passed,
                }
            )

    gate_passed = not blocked_reasons and all(
        row["comparison_passed"] for row in comparisons
    )
    candidate_identity_core = {
        field: analyzed_candidate_identity[field]
        for field in (
            "policy_id",
            "policy_source_sha256",
            "policy_adapter_sha256",
            "policy_profile_sha256",
        )
    }
    selection_design_digest = None
    if phase == "selection_validation":
        selection_design_digest = selection_design_sha256(
            protocol,
            protocol["selection_contract"]["frozen_shortlist"],
        )
    artifact: JSONMap = {
        "schema_version": 2,
        "kind": ANALYSIS_KIND,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "protocol_id": protocol_id,
        "protocol_sha256": materialized["protocol_sha256"],
        "runner_plan_sha256": (
            next(iter(observed_plan_digests))
            if len(observed_plan_digests) == 1
            else None
        ),
        "reduction_receipt_sha256": reduction_receipt["receipt_sha256"],
        "canonical_rollout_set_sha256": reduction_receipt[
            "canonical_rollout_set_sha256"
        ],
        "phase": phase,
        "candidate_id": candidate_id,
        "candidate_identity": {
            **candidate_identity_core,
            "candidate_identity_sha256": sha256_json(candidate_identity_core),
        },
        "required_baseline_ids": baseline_ids,
        "required_baseline_identities": [
            {
                "policy_id": entry["policy_id"],
                "source_bundle_sha256": entry["source_bundle_sha256"],
                "policy_adapter_sha256": entry["policy_adapter_sha256"],
                "policy_profile_sha256": entry["policy_profile_sha256"],
            }
            for entry in baseline_entries
        ],
        "selection_design_sha256": selection_design_digest,
        "selection_metric_definition_sha256": (
            SELECTION_METRIC_DEFINITION_SHA256
            if phase == "selection_validation"
            else None
        ),
        "seed_identity": {
            "phase": phase,
            "seed_count": len(expected_seeds),
            "seed_list_sha256": materialized["seed_set_sha256"][phase],
        },
        "corpus_identity": {
            "corpus_manifest_sha256": phase_corpus_binding.get(
                "corpus_manifest_sha256"
            ),
            "corpus_binding_sha256": phase_corpus_binding.get(
                "corpus_binding_sha256"
            ),
            "runner_scenario_count": phase_corpus_binding.get(
                "runner_scenario_count"
            ),
            "source_instance_provenance_sha256": phase_corpus_binding.get(
                "source_instance_provenance_sha256"
            ),
        },
        "runner_identity": {
            "runner_source_identity_sha256": protocol["execution_contract"][
                "python_source_identity_contract"
            ]["canonical_bundle_sha256"],
            "runner_inputs_sha256": runner_contract["runner_inputs_sha256"],
            "runner_scenario_bundle_sha256": runner_contract[
                "runner_scenario_bundle_sha256"
            ],
            "scenario_model_bundle_sha256": runner_contract[
                "scenario_model_bundle_sha256"
            ],
            "target_context_bundle_set_sha256": runner_contract[
                "target_context_bundle_set_sha256"
            ],
            "bridge_sha256": next(iter(observed_bridges)),
            "execution_bundle_sha256": runner_contract[
                "execution_bundle_sha256"
            ],
        },
        "seed_count": len(expected_seeds),
        "rollout_count": len(selected),
        "instance_count": len({row.instance_id for row in selected}),
        "component_count": len({row.component_id for row in selected}),
        "scenario_count": len(
            {(row.instance_id, row.scenario_id) for row in selected}
        ),
        "statistical_contract": {
            "estimand": (
                "finite-corpus wave-family-weighted paired relative DPS "
                "improvement"
            ),
            "population_generalization_claimed": False,
            "resampling": (
                "one crossed/pigeonhole bootstrap per comparison: independently "
                "resample master-seed and guild/player-component labels, apply the "
                "product of their multiplicities, then recompute the ratio of "
                "scenario-weighted candidate and reference sums"
            ),
            "independent_simulator_unit": "master seed",
            "independent_corpus_unit": "guild/player leakage component",
            "bootstrap_replicates": replicates,
            "familywise_method": "Bonferroni one-sided",
            "familywise_confidence_level": float(statistics["confidence_level"]),
            "simultaneous_test_count": test_count,
            "voting_bound_per_comparison": (
                "crossed_seed_x_guild_player_component_lower_bound"
            ),
            "per_test_alpha": per_test_alpha,
            "effective_tail_draws": replicates * per_test_alpha,
            "effective_tail_draws_definition": (
                "bootstrap_replicates multiplied by Bonferroni per-test alpha; "
                "descriptive only and not an adaptive gate"
            ),
            "marginal_cluster_bounds": "diagnostic_nonvoting",
            "monte_carlo_stability_diagnostic": (
                "fixed interleaved odd/even replicate split, reported per "
                "comparison as noninferential and never used by the gate"
            ),
            "same_seed_caveat": (
                "seed labels are paired scenario inputs; divergent action paths need not "
                "consume identical random draws"
            ),
        },
        "blocked_reasons": blocked_reasons,
        "comparisons": comparisons,
        "simulator_multiseed_gate_passed": gate_passed,
        "test_only_nonpromoting_selection_replay_authority_used": (
            test_only_selection_replay_authority
        ),
        "victory_claim_allowed": (
            gate_passed
            and phase == "final_confirmation"
            and not test_only_selection_replay_authority
        ),
        "claims_excluded": [
            "real-game DPS superiority",
            "population-wide performance beyond the frozen finite corpus",
            "exact Lua-runtime equivalence for source-derived adapters",
            "final confirmation from development or selection-validation seeds",
            "independent evidence merely from high worker concurrency",
        ],
    }
    return {**artifact, "analysis_sha256": sha256_json(artifact)}


def _read_jsonl(path: Path) -> list[JSONMap]:
    rows: list[JSONMap] = []
    try:
        with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise FuryMultiseedProtocolError(
                        f"JSONL line {line_number} is not an object"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryMultiseedProtocolError(f"could not read rollout JSONL: {exc}") from exc
    return rows


def _read_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryMultiseedProtocolError(f"could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise FuryMultiseedProtocolError(f"{label} JSON root must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        + b"\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--phase", choices=REQUIRED_PHASES)
    parser.add_argument("--reduction", type=Path)
    parser.add_argument("--runner-plan", type=Path)
    parser.add_argument(
        "--shard-manifest",
        type=Path,
        action="append",
        default=[],
        help=(
            "physical production shard manifest; repeat for every planned shard"
        ),
    )
    parser.add_argument(
        "--trusted-external-anchor-seal-sha256",
        action="append",
        default=[],
        help=(
            "out-of-band trusted external anchor seal SHA-256; repeat as needed"
        ),
    )
    parser.add_argument(
        "--selection-replay-package",
        type=Path,
        action="append",
        default=[],
        help=(
            "out-of-band physical selection replay package JSON; repeat once "
            "for every shortlisted candidate when loading a sealed selection"
        ),
    )
    parser.add_argument(
        "--final-discovery-capture-manifest",
        type=Path,
        help=(
            "out-of-band Chronicle final-discovery capture manifest; required "
            "whenever the protocol admits a final-confirmation corpus"
        ),
    )
    parser.add_argument("--expected-protocol-sha256")
    parser.add_argument("--candidate-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    materialized = load_protocol(
        args.protocol,
        trusted_external_anchor_seal_sha256s=(
            args.trusted_external_anchor_seal_sha256
        ),
        selection_physical_replay_package_paths=(
            args.selection_replay_package
        ),
        final_discovery_capture_manifest_path=(
            args.final_discovery_capture_manifest
        ),
    )
    if args.plan_only:
        if (
            args.phase
            or args.reduction
            or args.runner_plan
            or args.candidate_id
            or args.expected_protocol_sha256
            or args.shard_manifest
        ):
            parser.error("--plan-only cannot be combined with rollout analysis arguments")
        output = args.output or DEFAULT_PLAN
        artifact = build_plan(materialized)
    else:
        if (
            not args.phase
            or args.reduction is None
            or args.runner_plan is None
            or not args.candidate_id
            or not args.expected_protocol_sha256
            or not args.shard_manifest
        ):
            parser.error(
                "analysis requires --phase, --runner-plan, --reduction, "
                "--candidate-id, --expected-protocol-sha256, and every "
                "--shard-manifest"
            )
        output = args.output or DEFAULT_ANALYSIS
        reduction = _read_json_object(args.reduction, "reduction artifact")
        reduction_rows = reduction.get("rollout_rows")
        reduction_receipt = reduction.get("reduction_receipt")
        if not isinstance(reduction_rows, list) or not isinstance(
            reduction_receipt, Mapping
        ):
            raise FuryMultiseedProtocolError(
                "reduction artifact must contain rollout_rows and reduction_receipt"
            )
        artifact = analyze_rollouts(
            materialized,
            reduction_rows,
            runner_plan=_read_json_object(args.runner_plan, "runner plan"),
            reduction_receipt=reduction_receipt,
            manifest_paths=args.shard_manifest,
            expected_protocol_sha256=args.expected_protocol_sha256,
            phase=args.phase,
            candidate_id=args.candidate_id,
        )
    _write_json(output, artifact)
    print(
        json.dumps(
            {
                "kind": artifact["kind"],
                "protocol_sha256": artifact["protocol_sha256"],
                "output": str(output.expanduser().resolve()),
                "execution_started": artifact.get("execution_started"),
                "simulator_multiseed_gate_passed": artifact.get(
                    "simulator_multiseed_gate_passed"
                ),
                "victory_claim_allowed": artifact.get("victory_claim_allowed"),
                "readiness": artifact.get("readiness"),
                "blocked_reasons": artifact.get("blocked_reasons"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ANALYSIS_KIND",
    "FuryMultiseedProtocolError",
    "PLAN_KIND",
    "PROTOCOL_KIND",
    "REQUIRED_PHASES",
    "ROLLOUT_REQUIRED_FIELDS",
    "SEED_ALGORITHM",
    "SELECTION_PHYSICAL_REPLAY_RECEIPT_SCHEMA",
    "analyze_rollouts",
    "build_plan",
    "build_selection_evidence_bundle",
    "corpus_binding_sha256",
    "derive_seed_set",
    "derive_simulator_seed",
    "load_protocol",
    "materialize_protocol",
    "selection_design_sha256",
    "sha256_json",
]
