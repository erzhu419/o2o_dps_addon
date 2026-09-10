"""Non-voting short-horizon sensitivity for the frozen Cat2 supplemental replay.

The completed supplemental replay intentionally retained only aggregate rows.
This module audits the 1--52 ms families that can dominate unweighted DPS and
paired extrema.  It pins and verifies the completed full artifact, its receipt,
its input lock, and the reconstructed request bundle; replays only the matched
short families with the same four policy adapters and 16 seeds; then subtracts
their sufficient statistics from the published full aggregate.

The analysis module and its contract test are also explicit material inputs.
Their path, size, and SHA-256 must be supplied by the caller and are verified
both before and after replay.  This prevents a canonical result from silently
outliving the exact analysis implementation that produced it.

Nothing here modifies or extends the historical held-out gate.  The result is
diagnostic-only, non-voting, and cannot authorize deployment or a real-game
superiority claim.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite, ulp
from pathlib import Path
from statistics import fmean
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .cat2_saved_profile_adapter_v1 import Cat2SavedProfileSourceAdapterV1
from .fury_current_cat2_heldout_replay_v1 import (
    CAT2_ID,
    FuryCurrentCat2HeldoutReplayError,
    InputFileIdentity,
    RunInputSnapshot,
    SameByteDocumentStore,
    build_request_contract,
    parse_input_lock,
    reconstruct_frozen_corpus,
    verify_run_input_snapshot,
)
from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_expert_closed_loop import ClosedLoopBridgeLike, run_fury_expert_closed_loop
from .fury_heldout_corpus_gate_v1 import (
    SelectedCorpus,
    SelectedFamily,
    load_manifest_snapshot,
)
from .fury_policy_optimization_v1 import (
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    _seed_tuple,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_THRESHOLDS_MS: tuple[int, ...] = (10, 100, 1000)

DEFAULT_FULL_ARTIFACT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_supplemental_heldout_replay_v1.full.json"
)
DEFAULT_FULL_RECEIPT = Path(str(DEFAULT_FULL_ARTIFACT) + ".receipt.json")
DEFAULT_INPUT_LOCK = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_supplemental_heldout_replay_v1.input-lock.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_short_horizon_sensitivity_v1.json"
)
DEFAULT_ANALYSIS_MODULE = Path(__file__).resolve()
DEFAULT_ANALYSIS_TEST = (
    PROJECT_ROOT / "tests" / "test_fury_current_cat2_short_horizon_sensitivity_v1.py"
).resolve()

DEFAULT_FULL_ARTIFACT_SHA256 = (
    "0336e3183ade86f860cfbe21c57f026c078551b75123128f3fa392fb41571fcc"
)
DEFAULT_FULL_RECEIPT_SHA256 = (
    "65129722218bf154922c2b5d7584b0f0b84ec21a8291d8dd3923c8e77be75e83"
)
DEFAULT_INPUT_LOCK_SHA256 = (
    "60a77786b0e6e6fa73dbcb00e622a3a9e515d5b2ad14ebf2485173214d867ec7"
)
DEFAULT_REQUEST_BUNDLE_SHA256 = (
    "6ec213d5e700eda39fc8b279a83750270005ecbc0506a65072b8d033ab502c1a"
)
DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256 = (
    "8d2cfd221c7cb1f158c2fdfdf0d5de33a586ccc63b50cbfeea6596f2bf8c7f89"
)


class ShortHorizonSensitivityError(RuntimeError):
    """An upstream provenance or sensitivity arithmetic contract failed."""


@dataclass(frozen=True)
class UpstreamEvidence:
    full_identity: InputFileIdentity
    receipt_identity: InputFileIdentity
    lock_identity: InputFileIdentity
    full_artifact: Mapping[str, Any]
    receipt: Mapping[str, Any]
    lock_snapshot: RunInputSnapshot


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _artifact_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _read_pinned_json(
    path: Path,
    *,
    role: str,
    expected_sha256: str,
) -> tuple[InputFileIdentity, JSONMap]:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ShortHorizonSensitivityError(
            f"could not read {role} {resolved}: {exc}"
        ) from exc
    identity = InputFileIdentity(
        role=role,
        path=str(resolved),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    normalized = expected_sha256.strip().casefold()
    if len(normalized) != 64 or any(value not in "0123456789abcdef" for value in normalized):
        raise ShortHorizonSensitivityError(f"invalid expected SHA-256 for {role}")
    if identity.sha256 != normalized:
        raise ShortHorizonSensitivityError(
            f"{role} SHA-256 mismatch: expected {normalized}, got {identity.sha256}"
        )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ShortHorizonSensitivityError(f"could not parse {role}: {exc}") from exc
    if not isinstance(value, dict):
        raise ShortHorizonSensitivityError(f"{role} is not a JSON object")
    return identity, value


def _read_pinned_file(
    path: Path,
    *,
    role: str,
    expected_path: Path,
    expected_size_bytes: int,
    expected_sha256: str,
) -> InputFileIdentity:
    """Read one material source only when every expected identity field matches."""

    resolved = path.expanduser().resolve()
    resolved_expected = expected_path.expanduser().resolve()
    if resolved != resolved_expected:
        raise ShortHorizonSensitivityError(
            f"{role} path mismatch: expected {resolved_expected}, got {resolved}"
        )
    if (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or expected_size_bytes < 0
    ):
        raise ShortHorizonSensitivityError(f"invalid expected size for {role}")
    normalized = expected_sha256.strip().casefold()
    if len(normalized) != 64 or any(value not in "0123456789abcdef" for value in normalized):
        raise ShortHorizonSensitivityError(f"invalid expected SHA-256 for {role}")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ShortHorizonSensitivityError(
            f"could not read {role} {resolved}: {exc}"
        ) from exc
    identity = InputFileIdentity(
        role=role,
        path=str(resolved),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if identity.size_bytes != expected_size_bytes:
        raise ShortHorizonSensitivityError(
            f"{role} size mismatch: expected {expected_size_bytes}, "
            f"got {identity.size_bytes}"
        )
    if identity.sha256 != normalized:
        raise ShortHorizonSensitivityError(
            f"{role} SHA-256 mismatch: expected {normalized}, got {identity.sha256}"
        )
    return identity


def _source_bundle_sha256(values: Sequence[InputFileIdentity]) -> str:
    return hashlib.sha256(
        _canonical_bytes([asdict(value) for value in values])
    ).hexdigest()


def verify_analysis_sources(
    *,
    expected_module_path: Path,
    expected_module_size_bytes: int,
    expected_module_sha256: str,
    expected_test_path: Path,
    expected_test_size_bytes: int,
    expected_test_sha256: str,
) -> tuple[InputFileIdentity, InputFileIdentity]:
    """Fail closed unless the designated analysis module and test match exactly."""

    return (
        _read_pinned_file(
            DEFAULT_ANALYSIS_MODULE,
            role="short_horizon_analysis_module_source",
            expected_path=expected_module_path,
            expected_size_bytes=expected_module_size_bytes,
            expected_sha256=expected_module_sha256,
        ),
        _read_pinned_file(
            DEFAULT_ANALYSIS_TEST,
            role="short_horizon_analysis_test_source",
            expected_path=expected_test_path,
            expected_size_bytes=expected_test_size_bytes,
            expected_sha256=expected_test_sha256,
        ),
    )


def analysis_source_provenance(
    values: Sequence[InputFileIdentity],
) -> JSONMap:
    expected_roles = (
        "short_horizon_analysis_module_source",
        "short_horizon_analysis_test_source",
    )
    if tuple(value.role for value in values) != expected_roles:
        raise ShortHorizonSensitivityError(
            "analysis source identities must contain the module and test in canonical order"
        )
    return {
        "files": [asdict(value) for value in values],
        "file_count": len(values),
        "bundle_sha256": _source_bundle_sha256(values),
        "caller_supplied_path_size_sha256_contract": True,
        "verified_before_and_after_replay": True,
    }


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShortHorizonSensitivityError(f"{label} is missing or not an object")
    return value


def _require_false(document: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    for key in keys:
        if document.get(key) is not False:
            raise ShortHorizonSensitivityError(f"{label}.{key} must remain false")


def verify_upstream_evidence(
    *,
    full_artifact_path: Path = DEFAULT_FULL_ARTIFACT,
    full_receipt_path: Path = DEFAULT_FULL_RECEIPT,
    input_lock_path: Path = DEFAULT_INPUT_LOCK,
    expected_full_sha256: str = DEFAULT_FULL_ARTIFACT_SHA256,
    expected_receipt_sha256: str = DEFAULT_FULL_RECEIPT_SHA256,
    expected_lock_sha256: str = DEFAULT_INPUT_LOCK_SHA256,
    expected_run_input_file_bundle_sha256: str = DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
    expected_request_bundle_sha256: str = DEFAULT_REQUEST_BUNDLE_SHA256,
) -> UpstreamEvidence:
    """Verify the complete receipt -> artifact -> lock -> request hash chain."""

    full_identity, full = _read_pinned_json(
        full_artifact_path,
        role="completed_supplemental_full_artifact",
        expected_sha256=expected_full_sha256,
    )
    receipt_identity, receipt = _read_pinned_json(
        full_receipt_path,
        role="completed_supplemental_full_receipt",
        expected_sha256=expected_receipt_sha256,
    )
    lock_identity, lock = _read_pinned_json(
        input_lock_path,
        role="supplemental_input_lock",
        expected_sha256=expected_lock_sha256,
    )
    try:
        lock_snapshot = parse_input_lock(lock)
    except FuryCurrentCat2HeldoutReplayError as exc:
        raise ShortHorizonSensitivityError(str(exc)) from exc

    if full.get("schema_version") != 1 or full.get("kind") != (
        "fury_current_cat2_supplemental_heldout_replay_v1"
    ):
        raise ShortHorizonSensitivityError("unexpected completed full artifact schema")
    if receipt.get("schema_version") != 1 or receipt.get("kind") != (
        "fury_current_cat2_heldout_output_receipt_v1"
    ):
        raise ShortHorizonSensitivityError("unexpected completed full receipt schema")

    evaluation = _require_mapping(full.get("evaluation"), "full.evaluation")
    run_snapshot = _require_mapping(
        full.get("run_input_snapshot"), "full.run_input_snapshot"
    )
    request_contract = _require_mapping(
        run_snapshot.get("request_contract"),
        "full.run_input_snapshot.request_contract",
    )
    embedded_lock = _require_mapping(
        run_snapshot.get("input_lock"), "full.run_input_snapshot.input_lock"
    )
    receipt_artifact = _require_mapping(receipt.get("artifact"), "receipt.artifact")

    if receipt_artifact.get("sha256") != full_identity.sha256:
        raise ShortHorizonSensitivityError("receipt does not address the pinned full artifact")
    if receipt_artifact.get("size_bytes") != full_identity.size_bytes:
        raise ShortHorizonSensitivityError("receipt full artifact size is inconsistent")
    receipt_artifact_path = receipt_artifact.get("path")
    if not isinstance(receipt_artifact_path, str) or Path(
        receipt_artifact_path
    ).expanduser().resolve() != Path(full_identity.path):
        raise ShortHorizonSensitivityError("receipt full artifact path is inconsistent")

    expected_count = 392 * 16 * 4
    if (
        evaluation.get("status") != "COMPLETED"
        or evaluation.get("evaluation_scope") != "FROZEN_FULL"
        or evaluation.get("full_replay_completed") is not True
        or evaluation.get("rollout_count") != expected_count
        or evaluation.get("expected_rollout_count") != expected_count
    ):
        raise ShortHorizonSensitivityError("pinned full evaluation is not a complete 25,088-rollout receipt")
    if (
        receipt.get("artifact_status") != evaluation.get("status")
        or receipt.get("evaluation_scope") != evaluation.get("evaluation_scope")
        or receipt.get("full_replay_completed") is not True
        or receipt.get("actual_rollout_count") != expected_count
        or receipt.get("expected_rollout_count") != expected_count
    ):
        raise ShortHorizonSensitivityError("receipt and full evaluation completion fields differ")

    if embedded_lock.get("sha256") != lock_identity.sha256:
        raise ShortHorizonSensitivityError("full artifact does not embed the pinned input-lock SHA-256")
    if receipt.get("verified_input_lock_sha256") != lock_identity.sha256:
        raise ShortHorizonSensitivityError("receipt does not verify the pinned input lock")
    if run_snapshot.get("file_bundle_sha256") != lock_snapshot.file_bundle_sha256:
        raise ShortHorizonSensitivityError("full artifact and input lock file bundles differ")
    expected_file_bundle = expected_run_input_file_bundle_sha256.strip().casefold()
    if lock_snapshot.file_bundle_sha256 != expected_file_bundle:
        raise ShortHorizonSensitivityError(
            "input-lock file bundle differs from the explicitly expected bundle"
        )
    if len(lock_snapshot.files) != 66:
        raise ShortHorizonSensitivityError("input lock does not contain the frozen 66 files")
    if receipt.get("run_input_file_bundle_sha256") != lock_snapshot.file_bundle_sha256:
        raise ShortHorizonSensitivityError("receipt and input-lock file bundles differ")
    if run_snapshot.get("files") != lock.get("files"):
        raise ShortHorizonSensitivityError("full artifact and input lock file identities differ")
    if request_contract != lock_snapshot.request_contract:
        raise ShortHorizonSensitivityError("full artifact and input lock request contracts differ")

    request_bundle = request_contract.get("request_bundle_sha256")
    expected_request = expected_request_bundle_sha256.strip().casefold()
    if request_bundle != expected_request:
        raise ShortHorizonSensitivityError("input-lock request bundle SHA-256 mismatch")
    if receipt.get("request_bundle_sha256") != request_bundle:
        raise ShortHorizonSensitivityError("receipt and input-lock request bundles differ")
    if request_contract.get("family_count") != 392:
        raise ShortHorizonSensitivityError("request contract does not contain 392 families")
    rows = request_contract.get("rows")
    if not isinstance(rows, list) or len(rows) != 392:
        raise ShortHorizonSensitivityError("request contract row count is not 392")
    if len({row.get("scenario_id") for row in rows if isinstance(row, Mapping)}) != 392:
        raise ShortHorizonSensitivityError("request contract scenario IDs are not unique")

    frozen_contract = _require_mapping(
        full.get("frozen_input_contract"), "full.frozen_input_contract"
    )
    selected = frozen_contract.get("selected_families")
    seeds = frozen_contract.get("validation_seeds")
    if not isinstance(selected, list) or len(selected) != 392:
        raise ShortHorizonSensitivityError("full frozen contract lacks 392 selected families")
    if not isinstance(seeds, list) or len(_seed_tuple(seeds, "validation_seeds")) != 16:
        raise ShortHorizonSensitivityError("full frozen contract lacks 16 validation seeds")
    if evaluation.get("validation_seeds") != seeds:
        raise ShortHorizonSensitivityError("full evaluation seeds differ from frozen contract")

    _require_false(
        full,
        (
            "independent_expert_vote_available",
            "heldout_gate_modified",
            "heldout_gate_passed",
            "deployment_allowed",
            "real_game_superiority_claimed",
        ),
        "full",
    )
    _require_false(
        evaluation,
        (
            "voting_result",
            "deployment_gate_passed",
            "real_game_superiority_gate_passed",
        ),
        "full.evaluation",
    )
    return UpstreamEvidence(
        full_identity=full_identity,
        receipt_identity=receipt_identity,
        lock_identity=lock_identity,
        full_artifact=full,
        receipt=receipt,
        lock_snapshot=lock_snapshot,
    )


def _identities_by_role(snapshot: RunInputSnapshot, role: str) -> tuple[InputFileIdentity, ...]:
    return tuple(value for value in snapshot.files if value.role == role)


def _single_identity(snapshot: RunInputSnapshot, role: str) -> InputFileIdentity:
    values = _identities_by_role(snapshot, role)
    if len(values) != 1:
        raise ShortHorizonSensitivityError(
            f"input lock must contain exactly one {role}; observed {len(values)}"
        )
    return values[0]


def reconstruct_locked_inputs(
    evidence: UpstreamEvidence,
) -> tuple[SelectedCorpus, JSONMap, FuryPolicyParameters, JSONMap, Path]:
    """Reconstruct the same current request corpus from the pinned input lock."""

    frozen_identity = _single_identity(evidence.lock_snapshot, "frozen_heldout_gate")
    profile_identity = _single_identity(evidence.lock_snapshot, "cat2_profile_artifact")
    manifest_identity = _single_identity(evidence.lock_snapshot, "chronicle_manifest")
    bridge_identity = _single_identity(
        evidence.lock_snapshot, "staged_simulator_bridge_binary"
    )
    if evidence.full_artifact.get("protocol_relationship", {}).get(
        "frozen_gate_file_sha256"
    ) != frozen_identity.sha256:
        raise ShortHorizonSensitivityError("full artifact and locked frozen gate differ")
    if evidence.full_artifact.get("current_cat2", {}).get(
        "artifact_file_sha256"
    ) != profile_identity.sha256:
        raise ShortHorizonSensitivityError("full artifact and locked Cat2 profile differ")

    _, frozen_gate = _read_pinned_json(
        Path(frozen_identity.path),
        role="frozen_heldout_gate",
        expected_sha256=frozen_identity.sha256,
    )
    _, cat2_snapshot = _read_pinned_json(
        Path(profile_identity.path),
        role="cat2_profile_artifact",
        expected_sha256=profile_identity.sha256,
    )
    Cat2SavedProfileSourceAdapterV1(cat2_snapshot)

    store = SameByteDocumentStore()
    observed_manifest = store.capture(Path(manifest_identity.path), "chronicle_manifest")
    if observed_manifest != manifest_identity:
        raise ShortHorizonSensitivityError("locked manifest identity changed")
    manifest_meta = _require_mapping(
        frozen_gate.get("manifest_snapshot"), "frozen_gate.manifest_snapshot"
    )
    manifest_snapshot = load_manifest_snapshot(
        Path(manifest_identity.path),
        require_complete=bool(manifest_meta.get("require_complete")),
        document_loader=store.load,
    )
    locked_catalogs = {
        value.path: value
        for value in _identities_by_role(
            evidence.lock_snapshot, "completed_scenario_catalog"
        )
    }
    for catalog in manifest_snapshot.completed_catalogs:
        observed = store.capture(catalog.path, "completed_scenario_catalog")
        expected = locked_catalogs.get(observed.path)
        if expected is None or expected != observed:
            raise ShortHorizonSensitivityError(
                f"completed catalog is absent from or differs from lock: {observed.path}"
            )
    if set(locked_catalogs) != {
        str(value.path.expanduser().resolve()) for value in manifest_snapshot.completed_catalogs
    }:
        raise ShortHorizonSensitivityError("locked catalog set differs from completed manifest catalogs")

    try:
        corpus, reconstructed_contract = reconstruct_frozen_corpus(
            frozen_gate,
            manifest_snapshot=manifest_snapshot,
            document_loader=store.load,
        )
    except FuryCurrentCat2HeldoutReplayError as exc:
        raise ShortHorizonSensitivityError(str(exc)) from exc
    finally:
        store.release_documents()

    if build_request_contract(corpus) != evidence.lock_snapshot.request_contract:
        raise ShortHorizonSensitivityError("reconstructed requests differ from pinned input lock")
    try:
        verify_run_input_snapshot(evidence.lock_snapshot, corpus)
    except FuryCurrentCat2HeldoutReplayError as exc:
        raise ShortHorizonSensitivityError(str(exc)) from exc
    if reconstructed_contract != evidence.full_artifact.get("frozen_input_contract"):
        raise ShortHorizonSensitivityError("reconstructed frozen contract differs from full artifact")
    try:
        candidate = FuryPolicyParameters(
            **dict(reconstructed_contract["candidate_parameters"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ShortHorizonSensitivityError("invalid frozen candidate parameters") from exc
    return (
        corpus,
        reconstructed_contract,
        candidate,
        cat2_snapshot,
        Path(bridge_identity.path),
    )


def _validate_rollout_row(row: Mapping[str, Any], expert_id: str) -> JSONMap:
    if bool(row.get("source_execution")) or bool(row.get("exact_lua_replay")):
        raise ShortHorizonSensitivityError("short replay received an invalid exact-source claim")
    reasons = row.get("nonfaithful_reason_counts")
    if not isinstance(reasons, Mapping):
        raise ShortHorizonSensitivityError(
            f"short replay lacks nonfaithful reason counts for {expert_id}"
        )
    try:
        damage = float(row["damage_delta"])
        dps = float(row["dps"])
        omitted = int(row["omitted_lane_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShortHorizonSensitivityError(
            f"short replay lacks numeric fields for {expert_id}"
        ) from exc
    if not isfinite(damage) or not isfinite(dps):
        raise ShortHorizonSensitivityError(
            f"short replay contains non-finite damage/DPS for {expert_id}"
        )
    return {
        "damage_delta": damage,
        "dps": dps,
        "configured_horizon_complete": bool(row.get("configured_horizon_complete")),
        "omitted_lane_count": omitted,
        "nonfaithful_reason_counts": {
            str(key): int(value) for key, value in sorted(reasons.items())
        },
        "source_execution": False,
        "exact_lua_replay": False,
    }


def replay_short_families(
    bridge: ClosedLoopBridgeLike,
    corpus: SelectedCorpus,
    *,
    candidate_parameters: FuryPolicyParameters,
    cat2_snapshot: Mapping[str, Any],
    validation_seeds: Iterable[int],
    maximum_horizon_ms: int = 1000,
) -> tuple[tuple[str, ...], list[JSONMap]]:
    """Replay each matched family below ``maximum_horizon_ms`` exactly once."""

    seeds = _seed_tuple(validation_seeds, "validation_seeds")
    adapters = (
        CatFurySourceAdapter(),
        ContraDeployedSourceAdapter(),
        FuryTunedPolicyAdapter(candidate_parameters),
        Cat2SavedProfileSourceAdapterV1(cat2_snapshot),
    )
    expert_ids = tuple(adapter.expert_id for adapter in adapters)
    if CAT2_ID not in expert_ids or len(set(expert_ids)) != 4:
        raise ShortHorizonSensitivityError("short replay does not have four unique policies")
    families = tuple(
        family
        for family in corpus.families
        if family.scenario.horizon_ms < maximum_horizon_ms
    )
    rows: list[JSONMap] = []
    for family_ordinal, family in enumerate(families, start=1):
        for seed in seeds:
            policies: JSONMap = {}
            for adapter in adapters:
                raw = run_fury_expert_closed_loop(
                    bridge,
                    family.scenario.request,
                    adapter,
                    seed=seed,
                    horizon_ms=family.scenario.horizon_ms,
                    retain_steps=False,
                )
                policies[adapter.expert_id] = _validate_rollout_row(
                    raw, adapter.expert_id
                )
            rows.append(
                {
                    "scenario_id": family.scenario.scenario_id,
                    "horizon_ms": family.scenario.horizon_ms,
                    "sampling_weight": family.scenario.weight,
                    "target_count_stratum": family.target_count_stratum,
                    "seed": seed,
                    "policies": policies,
                }
            )
        print(
            json.dumps(
                {
                    "short_family_ordinal": family_ordinal,
                    "short_family_count": len(families),
                    "scenario_id": family.scenario.scenario_id,
                    "horizon_ms": family.scenario.horizon_ms,
                    "completed_rollouts": family_ordinal * len(seeds) * len(adapters),
                    "total_rollouts": len(families) * len(seeds) * len(adapters),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
    return expert_ids, rows


def _extreme_witness(
    row: Mapping[str, Any],
    *,
    focal_id: str,
    reference_id: str,
    delta: float,
) -> JSONMap:
    return {
        "scenario_id": row["scenario_id"],
        "horizon_ms": row["horizon_ms"],
        "sampling_weight": row["sampling_weight"],
        "seed": row["seed"],
        "focal_dps": row["policies"][focal_id]["dps"],
        "reference_dps": row["policies"][reference_id]["dps"],
        "paired_dps_delta": delta,
    }


def aggregate_short_rows(
    rows: Sequence[Mapping[str, Any]],
    expert_ids: Sequence[str],
    *,
    threshold_ms: int,
    focal_id: str = CAT2_ID,
) -> JSONMap:
    selected = [row for row in rows if int(row["horizon_ms"]) < threshold_ms]
    if not selected:
        raise ShortHorizonSensitivityError(f"no rows below {threshold_ms} ms")
    metrics: dict[str, JSONMap] = {
        expert_id: {
            "weighted_damage": 0.0,
            "weighted_seconds": 0.0,
            "dps_sum": 0.0,
            "rollout_count": 0,
            "omitted_lane_count": 0,
            "incomplete_horizon_count": 0,
            "nonfaithful_reason_counts": Counter(),
        }
        for expert_id in expert_ids
    }
    pair_values: dict[str, list[tuple[float, Mapping[str, Any]]]] = {
        expert_id: [] for expert_id in expert_ids if expert_id != focal_id
    }
    for row in selected:
        weight = float(row["sampling_weight"])
        horizon_ms = int(row["horizon_ms"])
        policies = _require_mapping(row.get("policies"), "short row policies")
        for expert_id in expert_ids:
            policy = _require_mapping(policies.get(expert_id), f"short row {expert_id}")
            metric = metrics[expert_id]
            metric["weighted_damage"] += weight * float(policy["damage_delta"])
            metric["weighted_seconds"] += weight * horizon_ms / 1000.0
            metric["dps_sum"] += float(policy["dps"])
            metric["rollout_count"] += 1
            metric["omitted_lane_count"] += int(policy["omitted_lane_count"])
            metric["incomplete_horizon_count"] += int(
                not bool(policy["configured_horizon_complete"])
            )
            metric["nonfaithful_reason_counts"].update(
                policy["nonfaithful_reason_counts"]
            )
        focal_dps = float(policies[focal_id]["dps"])
        for reference_id in pair_values:
            pair_values[reference_id].append(
                (focal_dps - float(policies[reference_id]["dps"]), row)
            )

    policies_result: JSONMap = {}
    for expert_id in expert_ids:
        metric = metrics[expert_id]
        seconds = float(metric["weighted_seconds"])
        count = int(metric["rollout_count"])
        policies_result[expert_id] = {
            "weighted_damage": float(metric["weighted_damage"]),
            "weighted_seconds": seconds,
            "weighted_mean_dps": float(metric["weighted_damage"]) / seconds,
            "dps_sum": float(metric["dps_sum"]),
            "unweighted_mean_dps": float(metric["dps_sum"]) / count,
            "rollout_count": count,
            "omitted_lane_count": int(metric["omitted_lane_count"]),
            "incomplete_horizon_count": int(metric["incomplete_horizon_count"]),
            "nonfaithful_reason_counts": dict(
                sorted(metric["nonfaithful_reason_counts"].items())
            ),
        }

    comparisons: JSONMap = {}
    for reference_id, pairs in pair_values.items():
        minimum = min(pairs, key=lambda value: value[0])
        maximum = max(pairs, key=lambda value: value[0])
        deltas = [value[0] for value in pairs]
        comparisons[reference_id] = {
            "pair_count": len(deltas),
            "dps_delta_sum": sum(deltas),
            "mean_paired_dps": fmean(deltas),
            "minimum_paired_dps": minimum[0],
            "minimum_witness": _extreme_witness(
                minimum[1],
                focal_id=focal_id,
                reference_id=reference_id,
                delta=minimum[0],
            ),
            "maximum_paired_dps": maximum[0],
            "maximum_witness": _extreme_witness(
                maximum[1],
                focal_id=focal_id,
                reference_id=reference_id,
                delta=maximum[0],
            ),
            "wins": sum(delta > 1e-9 for delta in deltas),
            "ties": sum(abs(delta) <= 1e-9 for delta in deltas),
            "losses": sum(delta < -1e-9 for delta in deltas),
        }
    family_rows: dict[str, Mapping[str, Any]] = {}
    for row in selected:
        family_rows.setdefault(str(row["scenario_id"]), row)
    family_contract = [
        {
            "scenario_id": value["scenario_id"],
            "horizon_ms": int(value["horizon_ms"]),
            "sampling_weight": float(value["sampling_weight"]),
            "target_count_stratum": value["target_count_stratum"],
        }
        for value in family_rows.values()
    ]
    return {
        "threshold_contract": f"horizon_ms < {threshold_ms}",
        "threshold_ms_exclusive": threshold_ms,
        "family_count": len(family_rows),
        "matched_seed_row_count": len(selected),
        "rollout_count": len(selected) * len(expert_ids),
        "horizons_ms": sorted(int(value["horizon_ms"]) for value in family_rows.values()),
        "scenario_set_sha256": hashlib.sha256(
            _canonical_bytes(family_contract)
        ).hexdigest(),
        "families": family_contract,
        "total_sampling_weight": sum(
            float(value["sampling_weight"]) for value in family_rows.values()
        ),
        "policies": policies_result,
        "current_cat2_vs_each_reference": comparisons,
    }


def _full_policy_rows(full: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    evaluation = _require_mapping(full.get("evaluation"), "full.evaluation")
    overall = _require_mapping(evaluation.get("overall"), "full.evaluation.overall")
    ranking = overall.get("ranking")
    if not isinstance(ranking, list) or len(ranking) != 4:
        raise ShortHorizonSensitivityError("full evaluation ranking is not four-way")
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in ranking:
        if not isinstance(row, Mapping) or not isinstance(row.get("expert_id"), str):
            raise ShortHorizonSensitivityError("invalid full ranking row")
        by_id[str(row["expert_id"])] = row
    if len(by_id) != 4 or CAT2_ID not in by_id:
        raise ShortHorizonSensitivityError("full ranking expert IDs are invalid")
    comparisons = _require_mapping(
        overall.get("current_cat2_vs_each_reference"),
        "full overall comparisons",
    )
    if set(comparisons) != set(by_id) - {CAT2_ID}:
        raise ShortHorizonSensitivityError("full pair comparisons do not match ranking")
    return by_id, comparisons


def _weighted_seconds(
    families: Sequence[SelectedFamily], seeds: Sequence[int]
) -> float:
    seconds = 0.0
    for family in families:
        for _ in seeds:
            seconds += float(family.scenario.weight) * family.scenario.horizon_ms / 1000.0
    return seconds


def subtract_short_scope(
    full: Mapping[str, Any],
    corpus: SelectedCorpus,
    validation_seeds: Sequence[int],
    short: Mapping[str, Any],
    *,
    focal_id: str = CAT2_ID,
) -> JSONMap:
    """Subtract short sufficient statistics from the published full aggregate."""

    full_by_id, full_pairs = _full_policy_rows(full)
    short_policies = _require_mapping(short.get("policies"), "short policies")
    if set(full_by_id) != set(short_policies):
        raise ShortHorizonSensitivityError("short and full policy IDs differ")
    full_count = len(corpus.families) * len(validation_seeds)
    full_seconds = _weighted_seconds(corpus.families, validation_seeds)
    threshold = int(short["threshold_ms_exclusive"])
    included_families = tuple(
        value for value in corpus.families if value.scenario.horizon_ms < threshold
    )
    expected_short_seconds = _weighted_seconds(included_families, validation_seeds)
    expected_short_count = len(included_families) * len(validation_seeds)

    policies: JSONMap = {}
    for expert_id, full_row in full_by_id.items():
        short_row = _require_mapping(short_policies.get(expert_id), f"short {expert_id}")
        if full_row.get("rollout_count") != full_count:
            raise ShortHorizonSensitivityError("full per-policy rollout count is inconsistent")
        if short_row.get("rollout_count") != expected_short_count:
            raise ShortHorizonSensitivityError("short per-policy rollout count is inconsistent")
        short_seconds = float(short_row["weighted_seconds"])
        if abs(short_seconds - expected_short_seconds) > max(1e-12, abs(expected_short_seconds) * 1e-12):
            raise ShortHorizonSensitivityError("short weighted seconds are inconsistent")
        full_weighted_dps = float(full_row["weighted_mean_dps"])
        full_unweighted_dps = float(full_row["unweighted_mean_dps"])
        full_damage = full_weighted_dps * full_seconds
        full_dps_sum = full_unweighted_dps * full_count
        full_damage_error_bound = (
            0.5 * ulp(full_weighted_dps) * full_seconds
            + 0.5 * ulp(full_damage)
        )
        full_dps_sum_error_bound = (
            0.5 * ulp(full_unweighted_dps) * full_count
            + 0.5 * ulp(full_dps_sum)
        )
        excluded_damage = full_damage - float(short_row["weighted_damage"])
        excluded_seconds = full_seconds - short_seconds
        excluded_count = full_count - expected_short_count
        excluded_dps_sum = full_dps_sum - float(short_row["dps_sum"])
        if excluded_seconds <= 0 or excluded_count <= 0:
            raise ShortHorizonSensitivityError("short subtraction leaves an empty scope")
        remaining_weighted_dps = excluded_damage / excluded_seconds
        remaining_unweighted_dps = excluded_dps_sum / excluded_count
        policies[expert_id] = {
            "full_reported_weighted_mean_dps": full_weighted_dps,
            "full_weighted_seconds": full_seconds,
            "full_weighted_damage_algebraically_reconstructed": full_damage,
            "full_weighted_damage_reconstruction_absolute_error_bound_binary64": full_damage_error_bound,
            "subtracted_short_weighted_damage": float(short_row["weighted_damage"]),
            "subtracted_short_weighted_seconds": short_seconds,
            "remaining_weighted_damage": excluded_damage,
            "remaining_weighted_seconds": excluded_seconds,
            "remaining_weighted_mean_dps": remaining_weighted_dps,
            "remaining_weighted_mean_dps_absolute_error_bound_from_upstream_rounding": (
                full_damage_error_bound / excluded_seconds
                + 0.5 * ulp(remaining_weighted_dps)
            ),
            "full_reported_unweighted_mean_dps": full_unweighted_dps,
            "full_rollout_count": full_count,
            "subtracted_short_rollout_count": expected_short_count,
            "remaining_rollout_count": excluded_count,
            "full_dps_sum_reconstruction_absolute_error_bound_binary64": full_dps_sum_error_bound,
            "remaining_unweighted_mean_dps": remaining_unweighted_dps,
            "remaining_unweighted_mean_dps_absolute_error_bound_from_upstream_rounding": (
                full_dps_sum_error_bound / excluded_count
                + 0.5 * ulp(remaining_unweighted_dps)
            ),
        }
    ranking = [
        {
            "expert_id": expert_id,
            "weighted_mean_dps": row["remaining_weighted_mean_dps"],
            "unweighted_mean_dps": row["remaining_unweighted_mean_dps"],
        }
        for expert_id, row in policies.items()
    ]
    ranking.sort(key=lambda value: (-float(value["weighted_mean_dps"]), value["expert_id"]))
    focal_dps = float(policies[focal_id]["remaining_weighted_mean_dps"])

    short_pairs = _require_mapping(
        short.get("current_cat2_vs_each_reference"), "short comparisons"
    )
    comparisons: JSONMap = {}
    for reference_id, full_pair_raw in full_pairs.items():
        full_pair = _require_mapping(full_pair_raw, f"full pair {reference_id}")
        short_pair = _require_mapping(short_pairs.get(reference_id), f"short pair {reference_id}")
        full_pair_count = int(full_pair["pair_count"])
        short_pair_count = int(short_pair["pair_count"])
        remaining_pair_count = full_pair_count - short_pair_count
        full_delta_sum = float(full_pair["mean_paired_dps"]) * full_pair_count
        full_delta_sum_error_bound = (
            0.5 * ulp(float(full_pair["mean_paired_dps"])) * full_pair_count
            + 0.5 * ulp(full_delta_sum)
        )
        remaining_delta_sum = full_delta_sum - float(short_pair["dps_delta_sum"])
        remaining_pair_mean = remaining_delta_sum / remaining_pair_count
        comparisons[reference_id] = {
            "remaining_weighted_mean_dps_margin": focal_dps
            - float(policies[reference_id]["remaining_weighted_mean_dps"]),
            "full_pair_count": full_pair_count,
            "subtracted_short_pair_count": short_pair_count,
            "remaining_pair_count": remaining_pair_count,
            "remaining_mean_paired_dps": remaining_pair_mean,
            "remaining_mean_paired_dps_source": "DERIVED_FROM_REPORTED_AGGREGATE_FLOAT",
            "remaining_mean_paired_dps_exact": False,
            "remaining_mean_paired_dps_absolute_error_bound_from_upstream_rounding": (
                full_delta_sum_error_bound / remaining_pair_count
                + 0.5 * ulp(remaining_pair_mean)
            ),
            "remaining_wins": int(full_pair["wins"]) - int(short_pair["wins"]),
            "remaining_ties": int(full_pair["ties"]) - int(short_pair["ties"]),
            "remaining_losses": int(full_pair["losses"]) - int(short_pair["losses"]),
            "full_minimum_paired_dps": float(full_pair["minimum_paired_dps"]),
            "full_maximum_paired_dps": float(full_pair["maximum_paired_dps"]),
            "full_minimum_reproduced_in_short_replay": abs(
                float(full_pair["minimum_paired_dps"])
                - float(short_pair["minimum_paired_dps"])
            ) <= 1e-9,
            "full_maximum_reproduced_in_short_replay": abs(
                float(full_pair["maximum_paired_dps"])
                - float(short_pair["maximum_paired_dps"])
            ) <= 1e-9,
            "remaining_minimum_and_maximum_not_identifiable_from_aggregate_only": True,
            "remaining_extrema_status": "NOT_DERIVABLE_WITHOUT_RETAINED_FULL_ROWS",
        }
    return {
        "excluded_threshold_contract": short["threshold_contract"],
        "subtraction_contract": (
            "Full sufficient statistics are algebraically reconstructed from the "
            "published binary64 mean and deterministic matched denominator; short "
            "replay sufficient statistics are then subtracted without rerunning the "
            "remaining full corpus. This is not a claim that the upstream unretained "
            "numerator was recovered bit-for-bit."
        ),
        "full_additive_totals_source": "DERIVED_FROM_REPORTED_AGGREGATE_FLOAT",
        "exact_additive_subtraction": False,
        "rounding_error_bound_contract": (
            "Bounds assume finite IEEE-754 binary64 round-to-nearest division and "
            "multiplication and the exactly reconstructed upstream denominator."
        ),
        "policies": policies,
        "ranking": ranking,
        "current_cat2_vs_each_reference": comparisons,
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_gate_passed": False,
        "real_game_superiority_gate_passed": False,
    }


def full_scope_summary(full: Mapping[str, Any]) -> JSONMap:
    by_id, comparisons = _full_policy_rows(full)
    ranking = [
        {
            "expert_id": expert_id,
            "weighted_mean_dps": float(row["weighted_mean_dps"]),
            "unweighted_mean_dps": float(row["unweighted_mean_dps"]),
            "rollout_count": int(row["rollout_count"]),
        }
        for expert_id, row in by_id.items()
    ]
    ranking.sort(key=lambda value: (-value["weighted_mean_dps"], value["expert_id"]))
    return {
        "scope": "PINNED_FULL_392_FAMILIES_X_16_SEEDS",
        "ranking": ranking,
        "current_cat2_vs_each_reference": copy.deepcopy(dict(comparisons)),
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_gate_passed": False,
        "real_game_superiority_gate_passed": False,
    }


def build_sensitivity_artifact(
    *,
    evidence: UpstreamEvidence,
    corpus: SelectedCorpus,
    validation_seeds: Sequence[int],
    expert_ids: Sequence[str],
    short_rows: Sequence[Mapping[str, Any]],
    analysis_sources: Sequence[InputFileIdentity],
) -> JSONMap:
    short_scopes = {
        str(threshold): aggregate_short_rows(
            short_rows,
            expert_ids,
            threshold_ms=threshold,
        )
        for threshold in VALID_THRESHOLDS_MS
    }
    exclusions = {
        "exclude_lt_10ms": subtract_short_scope(
            evidence.full_artifact,
            corpus,
            validation_seeds,
            short_scopes["10"],
        ),
        "exclude_lt_100ms": subtract_short_scope(
            evidence.full_artifact,
            corpus,
            validation_seeds,
            short_scopes["100"],
        ),
    }
    same_100_1000 = {
        str(row["scenario_id"])
        for row in short_rows
        if int(row["horizon_ms"]) < 100
    } == {
        str(row["scenario_id"])
        for row in short_rows
        if int(row["horizon_ms"]) < 1000
    }
    short_structural_completeness = all(
        int(policy["omitted_lane_count"]) == 0
        and int(policy["incomplete_horizon_count"]) == 0
        for scope in short_scopes.values()
        for policy in scope["policies"].values()
    )
    short_quality = short_structural_completeness and all(
        not policy["nonfaithful_reason_counts"]
        for scope in short_scopes.values()
        for policy in scope["policies"].values()
    )
    return {
        "schema_version": 1,
        "kind": "fury_current_cat2_short_horizon_sensitivity_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "upstream_provenance": {
            "completed_full_artifact": asdict(evidence.full_identity),
            "completed_full_receipt": asdict(evidence.receipt_identity),
            "supplemental_input_lock": asdict(evidence.lock_identity),
            "run_input_file_bundle_sha256": evidence.lock_snapshot.file_bundle_sha256,
            "locked_file_count": len(evidence.lock_snapshot.files),
            "request_bundle_sha256": evidence.lock_snapshot.request_contract[
                "request_bundle_sha256"
            ],
            "receipt_artifact_lock_request_chain_verified": True,
            "current_locked_files_and_reconstructed_requests_reverified_before_and_after_replay": True,
        },
        "analysis_source_provenance": analysis_source_provenance(analysis_sources),
        "sensitivity_contract": {
            "mode": "NON_VOTING_POST_HOC_DIAGNOSTIC",
            "four_policy_matched_seed_short_replay": True,
            "validation_seeds": list(validation_seeds),
            "full_family_count": len(corpus.families),
            "thresholds_ms_exclusive": list(VALID_THRESHOLDS_MS),
            "lt_100ms_and_lt_1000ms_family_sets_identical": same_100_1000,
            "band_100ms_to_999ms_family_count": 0 if same_100_1000 else None,
            "retained_short_matched_rows": True,
            "short_matched_row_count": len(short_rows),
            "short_rollout_count": len(short_rows) * len(expert_ids),
        },
        "pathology_explanation": [
            "DPS divides discrete damage by horizon seconds; at 1--7 ms a single event is magnified by roughly 143x--1000x per point of damage.",
            "An unweighted mean gives every family-seed row equal influence regardless of observed duration, while paired minima/maxima are single-row extrema.",
            "Pooled weighted DPS aggregates weighted damage over weighted exposure seconds, so removing the short sufficient statistics is the relevant duration sensitivity.",
            "The full artifact did not retain additive numerators or rollout rows; complement means are conservative reconstructions from reported binary64 aggregates, and complement paired extrema are not derivable.",
            "The subtraction is a post-hoc simulator diagnostic; it is not a new held-out gate, an independent expert vote, or real-game evidence.",
        ],
        "short_subsets": short_scopes,
        "pooled_weighted_sensitivity": {
            "full": full_scope_summary(evidence.full_artifact),
            **exclusions,
        },
        "short_replay_structural_completeness_passed": short_structural_completeness,
        "short_replay_quality_passed": short_quality,
        "subtraction_is_exact": False,
        "complement_pair_extrema_derivable": False,
        "stratum_scope_note": (
            "All excluded families are target_count=1. Other target-count strata are "
            "unchanged; this artifact reports pooled overall sensitivity and does not "
            "rewrite any upstream stratum result."
        ),
        "short_rollout_rows": copy.deepcopy(list(short_rows)),
        "historical_heldout_gate_modified": False,
        "historical_heldout_gate_passed": False,
        "independent_expert_vote_available": False,
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_allowed": False,
        "real_game_superiority_claimed": False,
        "claims_excluded": [
            "a modification, rerun, or extension of the historical frozen gate",
            "a voting result from the unsealed current Cat2 profile",
            "exact Cat, Cat2, or Contra Lua execution",
            "real-game DPS superiority",
            "deployment authorization",
        ],
    }


def _atomic_write(path: Path, data: bytes) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
        temporary.replace(resolved)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_sensitivity_with_receipt(
    path: Path,
    artifact: Mapping[str, Any],
    *,
    evidence: UpstreamEvidence,
    analysis_sources: Sequence[InputFileIdentity],
) -> JSONMap:
    _require_false(
        artifact,
        (
            "historical_heldout_gate_modified",
            "historical_heldout_gate_passed",
            "independent_expert_vote_available",
            "voting_result",
            "deployment_allowed",
            "real_game_superiority_claimed",
        ),
        "sensitivity",
    )
    source_provenance = analysis_source_provenance(analysis_sources)
    if artifact.get("analysis_source_provenance") != source_provenance:
        raise ShortHorizonSensitivityError(
            "artifact analysis source provenance differs from verified inputs"
        )
    data = _artifact_bytes(artifact)
    _atomic_write(path, data)
    committed = path.expanduser().resolve().read_bytes()
    if committed != data:
        raise ShortHorizonSensitivityError("committed sensitivity artifact bytes differ")
    identity = InputFileIdentity(
        role="short_horizon_sensitivity_artifact",
        path=str(path.expanduser().resolve()),
        size_bytes=len(committed),
        sha256=hashlib.sha256(committed).hexdigest(),
    )
    receipt: JSONMap = {
        "schema_version": 1,
        "kind": "fury_current_cat2_short_horizon_sensitivity_receipt_v1",
        "artifact": asdict(identity),
        "completed_full_artifact_sha256": evidence.full_identity.sha256,
        "completed_full_receipt_sha256": evidence.receipt_identity.sha256,
        "supplemental_input_lock_sha256": evidence.lock_identity.sha256,
        "run_input_file_bundle_sha256": evidence.lock_snapshot.file_bundle_sha256,
        "request_bundle_sha256": evidence.lock_snapshot.request_contract[
            "request_bundle_sha256"
        ],
        "analysis_source_files": source_provenance["files"],
        "analysis_source_bundle_sha256": source_provenance["bundle_sha256"],
        "analysis_sources_verified_before_and_after_replay": True,
        "short_replay_completed": True,
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_allowed": False,
        "real_game_superiority_claimed": False,
    }
    receipt_path = Path(str(path.expanduser().resolve()) + ".receipt.json")
    _atomic_write(receipt_path, _artifact_bytes(receipt))
    if json.loads(receipt_path.read_bytes()) != receipt:
        raise ShortHorizonSensitivityError("sensitivity receipt verification failed")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-artifact", type=Path, default=DEFAULT_FULL_ARTIFACT)
    parser.add_argument("--full-receipt", type=Path, default=DEFAULT_FULL_RECEIPT)
    parser.add_argument("--input-lock", type=Path, default=DEFAULT_INPUT_LOCK)
    parser.add_argument("--expected-full-sha256", default=DEFAULT_FULL_ARTIFACT_SHA256)
    parser.add_argument("--expected-receipt-sha256", default=DEFAULT_FULL_RECEIPT_SHA256)
    parser.add_argument("--expected-input-lock-sha256", default=DEFAULT_INPUT_LOCK_SHA256)
    parser.add_argument(
        "--expected-run-input-file-bundle-sha256",
        default=DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256,
    )
    parser.add_argument(
        "--expected-request-bundle-sha256", default=DEFAULT_REQUEST_BUNDLE_SHA256
    )
    parser.add_argument("--expected-analysis-module-path", type=Path, required=True)
    parser.add_argument("--expected-analysis-module-size-bytes", type=int, required=True)
    parser.add_argument("--expected-analysis-module-sha256", required=True)
    parser.add_argument("--expected-analysis-test-path", type=Path, required=True)
    parser.add_argument("--expected-analysis-test-size-bytes", type=int, required=True)
    parser.add_argument("--expected-analysis-test-sha256", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    analysis_sources = verify_analysis_sources(
        expected_module_path=args.expected_analysis_module_path,
        expected_module_size_bytes=args.expected_analysis_module_size_bytes,
        expected_module_sha256=args.expected_analysis_module_sha256,
        expected_test_path=args.expected_analysis_test_path,
        expected_test_size_bytes=args.expected_analysis_test_size_bytes,
        expected_test_sha256=args.expected_analysis_test_sha256,
    )
    evidence = verify_upstream_evidence(
        full_artifact_path=args.full_artifact,
        full_receipt_path=args.full_receipt,
        input_lock_path=args.input_lock,
        expected_full_sha256=args.expected_full_sha256,
        expected_receipt_sha256=args.expected_receipt_sha256,
        expected_lock_sha256=args.expected_input_lock_sha256,
        expected_run_input_file_bundle_sha256=(
            args.expected_run_input_file_bundle_sha256
        ),
        expected_request_bundle_sha256=args.expected_request_bundle_sha256,
    )
    corpus, contract, candidate, cat2_snapshot, bridge_path = reconstruct_locked_inputs(
        evidence
    )
    seeds = tuple(contract["validation_seeds"])
    with SimulatorBridge(bridge_path) as bridge:
        expert_ids, rows = replay_short_families(
            bridge,
            corpus,
            candidate_parameters=candidate,
            cat2_snapshot=cat2_snapshot,
            validation_seeds=seeds,
            maximum_horizon_ms=max(VALID_THRESHOLDS_MS),
        )
    try:
        verify_run_input_snapshot(evidence.lock_snapshot, corpus)
    except FuryCurrentCat2HeldoutReplayError as exc:
        raise ShortHorizonSensitivityError(str(exc)) from exc
    # The three upstream wrapper artifacts are outside the original input lock,
    # so verify their identities explicitly after the replay as well.
    verify_upstream_evidence(
        full_artifact_path=args.full_artifact,
        full_receipt_path=args.full_receipt,
        input_lock_path=args.input_lock,
        expected_full_sha256=args.expected_full_sha256,
        expected_receipt_sha256=args.expected_receipt_sha256,
        expected_lock_sha256=args.expected_input_lock_sha256,
        expected_run_input_file_bundle_sha256=(
            args.expected_run_input_file_bundle_sha256
        ),
        expected_request_bundle_sha256=args.expected_request_bundle_sha256,
    )
    post_replay_analysis_sources = verify_analysis_sources(
        expected_module_path=args.expected_analysis_module_path,
        expected_module_size_bytes=args.expected_analysis_module_size_bytes,
        expected_module_sha256=args.expected_analysis_module_sha256,
        expected_test_path=args.expected_analysis_test_path,
        expected_test_size_bytes=args.expected_analysis_test_size_bytes,
        expected_test_sha256=args.expected_analysis_test_sha256,
    )
    if post_replay_analysis_sources != analysis_sources:
        raise ShortHorizonSensitivityError(
            "analysis module or test identity changed during replay"
        )
    artifact = build_sensitivity_artifact(
        evidence=evidence,
        corpus=corpus,
        validation_seeds=seeds,
        expert_ids=expert_ids,
        short_rows=rows,
        analysis_sources=analysis_sources,
    )
    receipt = write_sensitivity_with_receipt(
        args.output,
        artifact,
        evidence=evidence,
        analysis_sources=analysis_sources,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "short_family_count_lt_10ms": artifact["short_subsets"]["10"][
                    "family_count"
                ],
                "short_family_count_lt_100ms": artifact["short_subsets"]["100"][
                    "family_count"
                ],
                "short_family_count_lt_1000ms": artifact["short_subsets"]["1000"][
                    "family_count"
                ],
                "short_rollout_count": artifact["sensitivity_contract"][
                    "short_rollout_count"
                ],
                "output": str(args.output.expanduser().resolve()),
                "output_sha256": receipt["artifact"]["sha256"],
                "simulator_diagnostic_only": True,
                "voting_result": False,
                "deployment_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "DEFAULT_ANALYSIS_MODULE",
    "DEFAULT_ANALYSIS_TEST",
    "DEFAULT_FULL_ARTIFACT_SHA256",
    "DEFAULT_FULL_RECEIPT_SHA256",
    "DEFAULT_INPUT_LOCK_SHA256",
    "DEFAULT_REQUEST_BUNDLE_SHA256",
    "DEFAULT_RUN_INPUT_FILE_BUNDLE_SHA256",
    "ShortHorizonSensitivityError",
    "UpstreamEvidence",
    "aggregate_short_rows",
    "analysis_source_provenance",
    "build_sensitivity_artifact",
    "full_scope_summary",
    "main",
    "reconstruct_locked_inputs",
    "replay_short_families",
    "subtract_short_scope",
    "verify_upstream_evidence",
    "verify_analysis_sources",
    "write_sensitivity_with_receipt",
)


if __name__ == "__main__":
    raise SystemExit(main())
