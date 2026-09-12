"""Supplement the frozen Fury held-out gate with the current Cat2 profile.

This module deliberately does not modify or reinterpret the historical
``fury_policy_heldout_corpus_gate_v1`` artifact.  It reconstructs that
artifact's scenario selection, verifies it byte-for-byte at the metadata
boundary, and then runs one additional matched-seed simulator diagnostic with
the content-addressed current Cat2 saved profile.

The Cat2 profile is mutable and source-derived, not an independently sealed
expert.  Consequently every result emitted here is non-voting and all
real-game/deployment gates remain false regardless of simulator DPS.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import shutil
from math import isfinite
from statistics import fmean
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .cat2_saved_profile_adapter_v1 import Cat2SavedProfileSourceAdapterV1
from .fury_expert_adapters import CatFurySourceAdapter, ContraDeployedSourceAdapter
from .fury_expert_closed_loop import ClosedLoopBridgeLike, run_fury_expert_closed_loop
from .fury_heldout_corpus_gate_v1 import (
    BASELINE_IDS,
    SelectedCorpus,
    load_candidate_parameters,
    load_manifest_snapshot,
    select_heldout_families,
    split_completed_instances,
)
from .fury_policy_optimization_v1 import (
    DEFAULT_BRIDGE,
    FuryPolicyParameters,
    FuryTunedPolicyAdapter,
    _seed_tuple,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FROZEN_GATE = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_heldout_corpus_gate_v1.json"
)
DEFAULT_CAT2_PROFILE = (
    PROJECT_ROOT / "offline_data" / "reports" / "cat2_saved_profile_v1.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_supplemental_heldout_replay_v1.full.json"
)
DEFAULT_SMOKE_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_supplemental_heldout_replay_v1.smoke.json"
)
DEFAULT_PLAN_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_cat2_supplemental_heldout_replay_v1.plan.json"
)
DEFAULT_STAGE_DIRECTORY = (
    PROJECT_ROOT / "offline_data" / "sim_validation" / "input_stage_v1"
)

# These defaults name the exact artifacts audited when this supplemental
# protocol was introduced.  A caller may deliberately select another object,
# but must supply its expected digest explicitly.
DEFAULT_FROZEN_GATE_SHA256 = (
    "d70b58829eabe2846af70a3b1cf219f610ec871dfdeb050d62b2fd1f6d08b0ba"
)
DEFAULT_CAT2_PROFILE_SHA256 = (
    "e5c17c343a46f14565a413920e635e455ad161654fb3c9aab6d9db8efff8875a"
)
# These fields were added to FuryPolicyParameters after the v1 held-out gate
# was frozen.  Their defaults exactly preserve the old candidate's action
# rules, but they are not part of the frozen parameter document or identity.
_POST_FREEZE_BEHAVIOR_PRESERVING_DEFAULTS = {
    "single_target_priority": "BLOODTHIRST_FIRST",
    "two_hand_slam_mode": "DISABLED",
}
CAT2_ID = Cat2SavedProfileSourceAdapterV1.expert_id


class FuryCurrentCat2HeldoutReplayError(RuntimeError):
    """A frozen-input, profile-provenance, or rollout contract was violated."""


@dataclass(frozen=True)
class InputFileIdentity:
    """One immutable identity captured before simulator execution."""

    role: str
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RunInputSnapshot:
    """The full file and reconstructed-request boundary for one run."""

    files: tuple[InputFileIdentity, ...]
    file_bundle_sha256: str
    request_contract: Mapping[str, Any]


class SameByteDocumentStore:
    """Parse JSON from the exact raw bytes used to create each identity."""

    def __init__(self) -> None:
        self._documents: dict[str, JSONMap] = {}
        self._identities: dict[str, InputFileIdentity] = {}
        self._loads: Counter[str] = Counter()

    def capture(self, path: Path, role: str) -> InputFileIdentity:
        identity, raw = _read_file_snapshot(path, role)
        try:
            payload = gzip.decompress(raw) if Path(identity.path).suffix.casefold() == ".gz" else raw
            value = json.loads(payload)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FuryCurrentCat2HeldoutReplayError(
                f"could not parse same-byte {role} JSON {identity.path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise FuryCurrentCat2HeldoutReplayError(
                f"same-byte {role} JSON is not an object: {identity.path}"
            )
        self._documents[identity.path] = value
        self._identities[identity.path] = identity
        return identity

    def load(self, path: Path) -> Mapping[str, Any]:
        resolved = str(path.expanduser().resolve())
        if resolved not in self._documents:
            raise FuryCurrentCat2HeldoutReplayError(
                f"same-byte document was not captured before use: {resolved}"
            )
        self._loads[resolved] += 1
        return copy.deepcopy(self._documents[resolved])

    def identity(self, path: Path) -> InputFileIdentity:
        resolved = str(path.expanduser().resolve())
        try:
            return self._identities[resolved]
        except KeyError as exc:
            raise FuryCurrentCat2HeldoutReplayError(
                f"same-byte document identity is absent: {resolved}"
            ) from exc

    def load_count(self, path: Path) -> int:
        return self._loads[str(path.expanduser().resolve())]

    def release_documents(self) -> None:
        self._documents.clear()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_file_snapshot(
    path: Path,
    role: str,
) -> tuple[InputFileIdentity, bytes]:
    resolved = path.expanduser().resolve()
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise FuryCurrentCat2HeldoutReplayError(
            f"could not snapshot {role} input {resolved}: {exc}"
        ) from exc
    identity = InputFileIdentity(
        role=role,
        path=str(resolved),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return identity, data


def _snapshot_file(path: Path, role: str) -> InputFileIdentity:
    return _read_file_snapshot(path, role)[0]


def _snapshot_json_file(
    path: Path,
    role: str,
) -> tuple[InputFileIdentity, JSONMap]:
    identity, data = _read_file_snapshot(path, role)
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryCurrentCat2HeldoutReplayError(
            f"could not parse snapshotted {role} JSON {identity.path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FuryCurrentCat2HeldoutReplayError(
            f"snapshotted {role} JSON is not an object"
        )
    return identity, value


def stage_bridge_binary(
    source: InputFileIdentity,
    stage_directory: Path,
) -> InputFileIdentity:
    """Copy the bridge once to a SHA-named executable and verify its bytes."""

    directory = stage_directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(source.path).suffix or ".bin"
    staged = directory / f"o2obridge-{source.sha256}{suffix}"
    if not staged.exists():
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=f".{staged.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                with Path(source.path).open("rb") as reader:
                    shutil.copyfileobj(reader, handle, length=1024 * 1024)
                handle.flush()
            temporary.replace(staged)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    identity = _snapshot_file(staged, "staged_simulator_bridge_binary")
    if identity.sha256 != source.sha256 or identity.size_bytes != source.size_bytes:
        raise FuryCurrentCat2HeldoutReplayError(
            "SHA-named staged bridge does not match source bridge identity"
        )
    return identity


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _file_bundle_sha256(files: Sequence[InputFileIdentity]) -> str:
    rows = [asdict(value) for value in files]
    return hashlib.sha256(_canonical_bytes(rows)).hexdigest()


def capture_file_inputs(
    *,
    frozen_gate: InputFileIdentity,
    cat2_profile: InputFileIdentity,
    manifest: InputFileIdentity,
    catalogs: Iterable[InputFileIdentity],
    bridge_source: InputFileIdentity,
    staged_bridge: InputFileIdentity,
    evaluator_sources: Iterable[InputFileIdentity],
) -> tuple[tuple[InputFileIdentity, ...], str]:
    """Capture every file that can affect reconstruction or simulator output."""

    rows = [
        frozen_gate,
        cat2_profile,
        manifest,
    ]
    rows.extend(
        sorted(catalogs, key=lambda value: value.path.casefold())
    )
    rows.append(bridge_source)
    rows.append(staged_bridge)
    rows.extend(sorted(evaluator_sources, key=lambda value: value.path.casefold()))
    identities = tuple(rows)
    return identities, _file_bundle_sha256(identities)


DIRECT_EXECUTION_MODULES: tuple[str, ...] = (
    "o2o_dps.fury_current_cat2_heldout_replay_v1",
    "o2o_dps.fury_expert_closed_loop",
    "o2o_dps.cat2_saved_profile_adapter_v1",
    "o2o_dps.fury_heldout_corpus_gate_v1",
    "o2o_dps.fury_policy_optimization_v1",
    "o2o_dps.fury_expert_adapters",
    "o2o_dps.sim_bridge",
    "o2o_dps.expert_policy",
    "o2o_dps.expert_proposals",
    "o2o_dps.fury_expert_guided_search_v1",
    "o2o_dps.fury_encounter_scenarios_v1",
)


def capture_direct_execution_sources() -> tuple[InputFileIdentity, ...]:
    paths: set[Path] = set()
    for module_name in DIRECT_EXECUTION_MODULES:
        module = importlib.import_module(module_name)
        raw_path = getattr(module, "__file__", None)
        if not isinstance(raw_path, str):
            raise FuryCurrentCat2HeldoutReplayError(
                f"direct execution module has no filesystem source: {module_name}"
            )
        path = Path(raw_path).resolve()
        if path.suffix == ".pyc":
            path = path.with_suffix(".py")
        paths.add(path)
    return tuple(
        _snapshot_file(path, "evaluator_python_source")
        for path in sorted(paths, key=lambda value: str(value).casefold())
    )


def build_request_contract(corpus: SelectedCorpus) -> JSONMap:
    """Content-address the current reconstructed request bytes, not history."""

    full_rows: list[JSONMap] = []
    persisted_rows: list[JSONMap] = []
    for family in corpus.families:
        try:
            request_sha = hashlib.sha256(
                _canonical_bytes(family.scenario.request)
            ).hexdigest()
        except (TypeError, ValueError) as exc:
            raise FuryCurrentCat2HeldoutReplayError(
                f"scenario {family.scenario.scenario_id} request is not finite strict JSON"
            ) from exc
        full_row = {
            "scenario_id": family.scenario.scenario_id,
            "request": family.scenario.request,
            "horizon_ms": family.scenario.horizon_ms,
            "sampling_weight": family.scenario.weight,
        }
        try:
            row_sha = hashlib.sha256(_canonical_bytes(full_row)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise FuryCurrentCat2HeldoutReplayError(
                f"scenario {family.scenario.scenario_id} contract is not finite strict JSON"
            ) from exc
        full_rows.append(full_row)
        persisted_rows.append(
            {
                "scenario_id": family.scenario.scenario_id,
                "request_sha256": request_sha,
                "horizon_ms": family.scenario.horizon_ms,
                "sampling_weight": family.scenario.weight,
                "row_contract_sha256": row_sha,
            }
        )
    return {
        "kind": "current_reconstructed_request_contract_v1",
        "canonicalization": "UTF-8 canonical JSON sorted keys compact separators",
        "historical_request_byte_equality_claimed": False,
        "historical_metadata_selection_reconstructed_exactly": True,
        "current_request_bytes_content_addressed": True,
        "family_count": len(full_rows),
        "request_bundle_sha256": hashlib.sha256(
            _canonical_bytes(full_rows)
        ).hexdigest(),
        "rows": persisted_rows,
    }


def make_run_input_snapshot(
    files: Sequence[InputFileIdentity],
    corpus: SelectedCorpus,
) -> RunInputSnapshot:
    identities = tuple(files)
    return RunInputSnapshot(
        files=identities,
        file_bundle_sha256=_file_bundle_sha256(identities),
        request_contract=build_request_contract(corpus),
    )


def verify_run_input_snapshot(
    snapshot: RunInputSnapshot,
    corpus: SelectedCorpus,
) -> None:
    """Fail closed if any input or in-memory request changed after capture."""

    observed: list[InputFileIdentity] = []
    for expected in snapshot.files:
        current = _snapshot_file(Path(expected.path), expected.role)
        if current != expected:
            raise FuryCurrentCat2HeldoutReplayError(
                f"run input changed after snapshot: {expected.role} {expected.path}"
            )
        observed.append(current)
    if _file_bundle_sha256(observed) != snapshot.file_bundle_sha256:
        raise FuryCurrentCat2HeldoutReplayError(
            "run input file bundle changed after snapshot"
        )
    current_request_contract = build_request_contract(corpus)
    if current_request_contract != snapshot.request_contract:
        raise FuryCurrentCat2HeldoutReplayError(
            "current reconstructed request contract changed after snapshot"
        )


def run_input_snapshot_document(snapshot: RunInputSnapshot) -> JSONMap:
    return {
        "schema_version": 1,
        "kind": "fury_current_cat2_heldout_input_lock_v1",
        "file_bundle_sha256": snapshot.file_bundle_sha256,
        "files": [asdict(value) for value in snapshot.files],
        "request_contract": copy.deepcopy(dict(snapshot.request_contract)),
    }


def parse_input_lock(document: Mapping[str, Any]) -> RunInputSnapshot:
    if document.get("schema_version") != 1 or document.get("kind") != (
        "fury_current_cat2_heldout_input_lock_v1"
    ):
        raise FuryCurrentCat2HeldoutReplayError("invalid supplemental input lock")
    raw_files = document.get("files")
    request_contract = document.get("request_contract")
    if not isinstance(raw_files, list) or not isinstance(request_contract, Mapping):
        raise FuryCurrentCat2HeldoutReplayError("input lock lacks files or requests")
    try:
        files = tuple(InputFileIdentity(**dict(value)) for value in raw_files)
    except (TypeError, ValueError) as exc:
        raise FuryCurrentCat2HeldoutReplayError(
            "input lock contains invalid file identities"
        ) from exc
    bundle = _file_bundle_sha256(files)
    if document.get("file_bundle_sha256") != bundle:
        raise FuryCurrentCat2HeldoutReplayError(
            "input lock file bundle hash is inconsistent"
        )
    return RunInputSnapshot(files, bundle, copy.deepcopy(dict(request_contract)))


def require_matching_input_lock(
    lock_path: Path,
    expected_sha256: str,
    current: RunInputSnapshot,
) -> InputFileIdentity:
    identity, document = _snapshot_json_file(lock_path, "supplemental_input_lock")
    if identity.sha256 != expected_sha256.strip().casefold():
        raise FuryCurrentCat2HeldoutReplayError(
            "supplemental input lock SHA-256 does not match expected content address"
        )
    locked = parse_input_lock(document)
    if locked != current:
        raise FuryCurrentCat2HeldoutReplayError(
            "current files or request bytes do not match supplemental input lock"
        )
    return identity


def _atomic_write_bytes(path: Path, data: bytes) -> None:
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


def write_artifact_with_receipt(
    path: Path,
    artifact: Mapping[str, Any],
    *,
    run_input_snapshot: RunInputSnapshot,
) -> JSONMap:
    """Atomically commit one artifact and then its verified final receipt."""

    evaluation = artifact.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise FuryCurrentCat2HeldoutReplayError("artifact lacks evaluation receipt data")
    expected_rollout_count = evaluation.get("expected_rollout_count")
    actual_rollout_count = evaluation.get("rollout_count")
    if (
        isinstance(expected_rollout_count, bool)
        or not isinstance(expected_rollout_count, int)
        or isinstance(actual_rollout_count, bool)
        or not isinstance(actual_rollout_count, int)
        or actual_rollout_count != expected_rollout_count
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            "actual rollout count does not equal expected rollout count"
        )
    evaluation_scope = evaluation.get("evaluation_scope")
    if not isinstance(evaluation_scope, str) or not evaluation_scope:
        raise FuryCurrentCat2HeldoutReplayError("evaluation scope is missing")
    full_completed = bool(evaluation.get("full_replay_completed", False))
    raw_lock = artifact.get("run_input_snapshot", {}).get("input_lock")
    verified_lock_sha: str | None = None
    if isinstance(raw_lock, Mapping):
        value = raw_lock.get("sha256")
        if isinstance(value, str):
            verified_lock_sha = value
    if full_completed and (
        evaluation_scope != "FROZEN_FULL" or verified_lock_sha is None
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            "full replay receipt requires FROZEN_FULL scope and verified input lock"
        )
    data = _artifact_bytes(artifact)
    _atomic_write_bytes(path, data)
    identity, observed = _read_file_snapshot(path, "supplemental_output_artifact")
    if observed != data:
        raise FuryCurrentCat2HeldoutReplayError(
            "committed supplemental artifact bytes failed verification"
        )
    receipt = {
        "schema_version": 1,
        "kind": "fury_current_cat2_heldout_output_receipt_v1",
        "artifact": asdict(identity),
        "artifact_status": evaluation.get("status"),
        "evaluation_scope": evaluation_scope,
        "expected_rollout_count": expected_rollout_count,
        "actual_rollout_count": actual_rollout_count,
        "full_replay_completed": full_completed,
        "verified_input_lock_sha256": verified_lock_sha,
        "run_input_file_bundle_sha256": run_input_snapshot.file_bundle_sha256,
        "request_bundle_sha256": run_input_snapshot.request_contract.get(
            "request_bundle_sha256"
        ),
    }
    receipt_path = Path(str(path.expanduser().resolve()) + ".receipt.json")
    _atomic_write_bytes(receipt_path, _artifact_bytes(receipt))
    committed_receipt = json.loads(receipt_path.read_bytes())
    if committed_receipt != receipt:
        raise FuryCurrentCat2HeldoutReplayError(
            "supplemental artifact receipt failed verification"
        )
    return receipt


def _require_digest(path: Path, expected: str, label: str) -> str:
    expected_normalized = expected.strip().casefold()
    if len(expected_normalized) != 64 or any(
        value not in "0123456789abcdef" for value in expected_normalized
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            f"{label} expected SHA-256 is not 64 lowercase hex characters"
        )
    observed = _sha256_file(path)
    if observed != expected_normalized:
        raise FuryCurrentCat2HeldoutReplayError(
            f"{label} SHA-256 mismatch: expected {expected_normalized}, got {observed}"
        )
    return observed


def _selected_metadata(corpus: SelectedCorpus) -> list[JSONMap]:
    return [
        {
            "scenario_id": value.scenario.scenario_id,
            "instance_id": value.instance_id,
            "family_id": value.family_id,
            "catalog": value.catalog_relative_path,
            "target_count": value.target_count,
            "target_count_stratum": value.target_count_stratum,
            "duration_ms": value.scenario.horizon_ms,
            "duration_stratum": value.duration_stratum,
            "sampling_weight": value.scenario.weight,
        }
        for value in corpus.families
    ]


def reconstruct_frozen_corpus(
    frozen_gate: Mapping[str, Any],
    *,
    manifest_snapshot: Any | None = None,
    document_loader: Callable[[Path], Mapping[str, Any]] | None = None,
) -> tuple[SelectedCorpus, Mapping[str, Any]]:
    """Rebuild and exactly match the frozen scenario/seed/candidate contract."""

    if frozen_gate.get("kind") != "fury_policy_heldout_corpus_gate_v1":
        raise FuryCurrentCat2HeldoutReplayError("invalid frozen held-out artifact kind")
    if frozen_gate.get("schema_version") != 1:
        raise FuryCurrentCat2HeldoutReplayError("unsupported frozen held-out schema")
    manifest_meta = frozen_gate.get("manifest_snapshot")
    split_meta = frozen_gate.get("instance_split")
    selection = frozen_gate.get("selection_contract")
    selected = frozen_gate.get("selected_corpus")
    evaluation = frozen_gate.get("evaluation")
    candidate = frozen_gate.get("candidate")
    if not all(
        isinstance(value, Mapping)
        for value in (manifest_meta, split_meta, selection, selected, evaluation, candidate)
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen held-out artifact lacks required contract objects"
        )
    if evaluation.get("status") != "COMPLETED":
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen held-out artifact does not contain a completed evaluation"
        )
    seeds = evaluation.get("validation_seeds")
    if not isinstance(seeds, list) or not seeds:
        raise FuryCurrentCat2HeldoutReplayError("frozen validation seeds are missing")
    _seed_tuple(seeds, "frozen validation_seeds")

    manifest_path_value = manifest_meta.get("path")
    if not isinstance(manifest_path_value, str) or not manifest_path_value:
        raise FuryCurrentCat2HeldoutReplayError("frozen manifest path is missing")
    snapshot = manifest_snapshot
    if snapshot is None:
        snapshot = load_manifest_snapshot(
            Path(manifest_path_value),
            require_complete=bool(manifest_meta.get("require_complete")),
        )
    elif snapshot.manifest_path != Path(manifest_path_value).expanduser().resolve():
        raise FuryCurrentCat2HeldoutReplayError(
            "provided manifest snapshot path differs from frozen metadata"
        )
    training = split_meta.get("candidate_training_instances")
    heldout = split_meta.get("heldout_instances")
    if not isinstance(training, list) or not isinstance(heldout, list) or not heldout:
        raise FuryCurrentCat2HeldoutReplayError("frozen instance split is incomplete")
    split_seed = split_meta.get("split_seed")
    if isinstance(split_seed, bool) or not isinstance(split_seed, int):
        raise FuryCurrentCat2HeldoutReplayError("frozen split seed is invalid")
    split = split_completed_instances(
        snapshot,
        split_seed=split_seed,
        candidate_training_instances=tuple(str(value) for value in training),
        heldout_count=len(heldout),
    )
    expected_split = {
        "candidate_training_instances": list(split.candidate_training_instances),
        "heldout_instances": list(split.heldout_instances),
        "non_heldout_completed_instances": list(
            split.non_heldout_completed_instances
        ),
    }
    for key, observed in expected_split.items():
        if split_meta.get(key) != observed:
            raise FuryCurrentCat2HeldoutReplayError(
                f"current manifest no longer reconstructs frozen {key}"
            )

    try:
        armor = selection["armor_hypothesis"]
        level = selection["level_hypothesis"]
        selection_seed = selection["selection_seed"]
        maximum = selection["max_families_per_heldout_instance"]
    except KeyError as exc:
        raise FuryCurrentCat2HeldoutReplayError(
            f"frozen selection contract lacks {exc.args[0]}"
        ) from exc
    corpus = select_heldout_families(
        snapshot,
        split,
        armor_hypothesis=armor,
        level_hypothesis=int(level),
        selection_seed=int(selection_seed),
        max_families_per_instance=int(maximum),
        document_loader=document_loader,
    )
    actual_metadata = _selected_metadata(corpus)
    if selected.get("families") != actual_metadata:
        raise FuryCurrentCat2HeldoutReplayError(
            "reconstructed scenario metadata differs from frozen held-out input"
        )
    if selected.get("family_count") != len(corpus.families):
        raise FuryCurrentCat2HeldoutReplayError("frozen family count is inconsistent")
    frozen_candidate_parameters = candidate.get("parameters")
    if not isinstance(frozen_candidate_parameters, Mapping):
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen candidate parameters are missing"
        )
    candidate_parameters = load_candidate_parameters(candidate)
    canonical_frozen_parameters = asdict(candidate_parameters)
    for name, expected in _POST_FREEZE_BEHAVIOR_PRESERVING_DEFAULTS.items():
        if (
            name in frozen_candidate_parameters
            or canonical_frozen_parameters.pop(name, None) != expected
        ):
            raise FuryCurrentCat2HeldoutReplayError(
                "post-freeze candidate defaults no longer preserve v1 behavior"
            )
    if dict(frozen_candidate_parameters) != canonical_frozen_parameters:
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen candidate parameters are not canonical"
        )
    frozen_candidate_policy_id = candidate.get("policy_id")
    if not isinstance(frozen_candidate_policy_id, str) or not frozen_candidate_policy_id:
        raise FuryCurrentCat2HeldoutReplayError("frozen candidate policy ID is missing")

    input_contract = {
        "manifest_snapshot": copy.deepcopy(dict(manifest_meta)),
        "instance_split": copy.deepcopy(dict(split_meta)),
        "selection_contract": copy.deepcopy(dict(selection)),
        "selected_families": actual_metadata,
        "candidate_policy_id": frozen_candidate_policy_id,
        "candidate_parameters": copy.deepcopy(dict(frozen_candidate_parameters)),
        "validation_seeds": list(seeds),
    }
    return corpus, input_contract


class _DiagnosticAccumulator:
    def __init__(self, expert_ids: Sequence[str], focal_id: str) -> None:
        self.expert_ids = tuple(expert_ids)
        self.focal_id = focal_id
        if focal_id not in self.expert_ids or len(set(self.expert_ids)) != len(
            self.expert_ids
        ):
            raise FuryCurrentCat2HeldoutReplayError("invalid diagnostic expert IDs")
        self.metrics: dict[str, JSONMap] = {
            expert_id: {
                "weighted_damage": 0.0,
                "weighted_seconds": 0.0,
                "dps_sum": 0.0,
                "rollout_count": 0,
                "omitted_lane_count": 0,
                "incomplete_horizon_count": 0,
                "nonfaithful_reason_counts": Counter(),
            }
            for expert_id in self.expert_ids
        }
        self.pairs: dict[str, JSONMap] = {
            expert_id: {"deltas": [], "wins": 0, "ties": 0, "losses": 0}
            for expert_id in self.expert_ids
            if expert_id != focal_id
        }

    def add(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        weight: float,
        horizon_ms: int,
    ) -> None:
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not isfinite(float(weight))
            or float(weight) <= 0
        ):
            raise FuryCurrentCat2HeldoutReplayError(
                "diagnostic sampling weight must be finite and positive"
            )
        if (
            isinstance(horizon_ms, bool)
            or not isinstance(horizon_ms, int)
            or horizon_ms <= 0
        ):
            raise FuryCurrentCat2HeldoutReplayError(
                "diagnostic horizon_ms must be a positive integer"
            )
        validated: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], float, float]] = {}
        for expert_id in self.expert_ids:
            row = rows.get(expert_id)
            if not isinstance(row, Mapping):
                raise FuryCurrentCat2HeldoutReplayError(
                    f"rollout row is missing for {expert_id}"
                )
            if bool(row.get("source_execution")) or bool(row.get("exact_lua_replay")):
                raise FuryCurrentCat2HeldoutReplayError(
                    "supplemental replay received an invalid exact-source claim"
                )
            reasons = row.get("nonfaithful_reason_counts")
            if not isinstance(reasons, Mapping):
                raise FuryCurrentCat2HeldoutReplayError(
                    f"rollout lacks nonfaithful reason counts for {expert_id}"
                )
            try:
                damage = float(row["damage_delta"])
                dps = float(row["dps"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FuryCurrentCat2HeldoutReplayError(
                    f"rollout lacks numeric damage/DPS for {expert_id}"
                ) from exc
            if not isfinite(damage) or not isfinite(dps):
                raise FuryCurrentCat2HeldoutReplayError(
                    f"rollout damage/DPS must be finite for {expert_id}"
                )
            if not isfinite(float(weight) * damage):
                raise FuryCurrentCat2HeldoutReplayError(
                    f"weighted rollout damage is not finite for {expert_id}"
                )
            validated[expert_id] = (row, reasons, damage, dps)

        focal_dps = validated[self.focal_id][3]
        pair_deltas: dict[str, float] = {}
        for reference_id in self.pairs:
            delta = focal_dps - validated[reference_id][3]
            if not isfinite(delta):
                raise FuryCurrentCat2HeldoutReplayError(
                    f"paired DPS delta is not finite for {reference_id}"
                )
            pair_deltas[reference_id] = delta

        for expert_id in self.expert_ids:
            row, reasons, damage, dps = validated[expert_id]
            metric = self.metrics[expert_id]
            metric["weighted_damage"] += weight * damage
            metric["weighted_seconds"] += weight * horizon_ms / 1000.0
            metric["dps_sum"] += dps
            metric["rollout_count"] += 1
            metric["omitted_lane_count"] += int(row["omitted_lane_count"])
            metric["incomplete_horizon_count"] += int(
                not bool(row["configured_horizon_complete"])
            )
            metric["nonfaithful_reason_counts"].update(
                {str(key): int(value) for key, value in reasons.items()}
            )

        for reference_id, pair in self.pairs.items():
            delta = pair_deltas[reference_id]
            pair["deltas"].append(delta)
            if delta > 1e-9:
                pair["wins"] += 1
            elif delta < -1e-9:
                pair["losses"] += 1
            else:
                pair["ties"] += 1

    def finish(self) -> JSONMap:
        ranking: list[JSONMap] = []
        by_id: dict[str, JSONMap] = {}
        for expert_id in self.expert_ids:
            metric = self.metrics[expert_id]
            seconds = float(metric["weighted_seconds"])
            count = int(metric["rollout_count"])
            if seconds <= 0 or count <= 0:
                raise FuryCurrentCat2HeldoutReplayError("empty diagnostic scope")
            row = {
                "expert_id": expert_id,
                "rollout_count": count,
                "weighted_mean_dps": float(metric["weighted_damage"]) / seconds,
                "unweighted_mean_dps": float(metric["dps_sum"]) / count,
                "omitted_lane_count": int(metric["omitted_lane_count"]),
                "incomplete_horizon_count": int(
                    metric["incomplete_horizon_count"]
                ),
                "nonfaithful_reason_counts": dict(
                    sorted(metric["nonfaithful_reason_counts"].items())
                ),
            }
            ranking.append(row)
            by_id[expert_id] = row
        ranking.sort(
            key=lambda value: (-float(value["weighted_mean_dps"]), value["expert_id"])
        )
        focal_dps = float(by_id[self.focal_id]["weighted_mean_dps"])
        comparisons: dict[str, JSONMap] = {}
        for reference_id, pair in self.pairs.items():
            deltas = pair["deltas"]
            comparisons[reference_id] = {
                "pair_count": len(deltas),
                "weighted_mean_dps_margin": focal_dps
                - float(by_id[reference_id]["weighted_mean_dps"]),
                "mean_paired_dps": fmean(deltas),
                "minimum_paired_dps": min(deltas),
                "maximum_paired_dps": max(deltas),
                "wins": int(pair["wins"]),
                "ties": int(pair["ties"]),
                "losses": int(pair["losses"]),
            }
        return {
            "ranking": ranking,
            "current_cat2_vs_each_reference": comparisons,
            "all_experts_zero_omissions": all(
                int(value["omitted_lane_count"]) == 0 for value in ranking
            ),
            "all_experts_all_horizons_complete": all(
                int(value["incomplete_horizon_count"]) == 0 for value in ranking
            ),
            "simulator_diagnostic_only": True,
            "voting_result": False,
            "deployment_gate_passed": False,
            "real_game_superiority_gate_passed": False,
        }


def evaluate_supplemental_replay(
    bridge: ClosedLoopBridgeLike,
    corpus: SelectedCorpus,
    *,
    candidate_parameters: FuryPolicyParameters,
    candidate_policy_id: str | None = None,
    cat2_snapshot: Mapping[str, Any],
    validation_seeds: Iterable[int],
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> JSONMap:
    """Run Cat, Contra, frozen candidate, and current Cat2 on matched inputs."""

    seeds = _seed_tuple(validation_seeds, "validation_seeds")
    candidate = FuryTunedPolicyAdapter(candidate_parameters)
    if candidate_policy_id is not None:
        if not candidate_policy_id:
            raise FuryCurrentCat2HeldoutReplayError("candidate policy ID is empty")
        candidate.expert_id = candidate_policy_id
    cat2 = Cat2SavedProfileSourceAdapterV1(cat2_snapshot)
    adapters = (
        CatFurySourceAdapter(),
        ContraDeployedSourceAdapter(),
        candidate,
        cat2,
    )
    expert_ids = tuple(adapter.expert_id for adapter in adapters)
    overall = _DiagnosticAccumulator(expert_ids, CAT2_ID)
    by_target: dict[str, _DiagnosticAccumulator] = {}
    for family_ordinal, family in enumerate(corpus.families, start=1):
        scope = by_target.setdefault(
            family.target_count_stratum,
            _DiagnosticAccumulator(expert_ids, CAT2_ID),
        )
        for seed in seeds:
            rows: dict[str, Mapping[str, Any]] = {}
            for adapter in adapters:
                rows[adapter.expert_id] = run_fury_expert_closed_loop(
                    bridge,
                    family.scenario.request,
                    adapter,
                    seed=seed,
                    horizon_ms=family.scenario.horizon_ms,
                    retain_steps=False,
                )
            overall.add(
                rows,
                weight=family.scenario.weight,
                horizon_ms=family.scenario.horizon_ms,
            )
            scope.add(
                rows,
                weight=family.scenario.weight,
                horizon_ms=family.scenario.horizon_ms,
            )
        if progress is not None:
            progress(
                {
                    "family_ordinal": family_ordinal,
                    "family_count": len(corpus.families),
                    "scenario_id": family.scenario.scenario_id,
                    "completed_rollouts": family_ordinal * len(seeds) * len(adapters),
                    "total_rollouts": len(corpus.families)
                    * len(seeds)
                    * len(adapters),
                }
            )
    overall_result = overall.finish()
    target_results = {key: by_target[key].finish() for key in sorted(by_target)}
    quality_passed = bool(overall_result["all_experts_zero_omissions"]) and bool(
        overall_result["all_experts_all_horizons_complete"]
    ) and all(not row["nonfaithful_reason_counts"] for row in overall_result["ranking"])
    return {
        "status": "COMPLETED",
        "evaluation_scope": "FROZEN_FULL",
        "validation_seeds": list(seeds),
        "matched_seed_contract": True,
        "retained_rollout_rows": False,
        "rollout_count": len(corpus.families) * len(seeds) * len(adapters),
        "expected_rollout_count": len(corpus.families)
        * len(seeds)
        * len(adapters),
        "overall": overall_result,
        "target_count_strata": target_results,
        "full_replay_completed": True,
        "diagnostic_quality_passed": quality_passed,
        "known_proxy_limitations": [
            "bridge legality rows do not expose the game-client rejection cause; "
            "bounded Cat2.Cast retry waits are not classified as cooldown-only or "
            "exact Lua behavior"
        ],
        "simulator_diagnostic_only": True,
        "voting_result": False,
        "deployment_gate_passed": False,
        "real_game_superiority_gate_passed": False,
    }


def build_artifact(
    *,
    frozen_gate_path: Path,
    frozen_gate_sha256: str,
    frozen_input_contract: Mapping[str, Any],
    cat2_profile_path: Path,
    cat2_profile_sha256: str,
    cat2_snapshot: Mapping[str, Any],
    run_input_snapshot: RunInputSnapshot,
    evaluation: Mapping[str, Any],
    input_lock_identity: InputFileIdentity | None = None,
) -> JSONMap:
    profile = cat2_snapshot.get("profile")
    if not isinstance(profile, Mapping):
        raise FuryCurrentCat2HeldoutReplayError("Cat2 snapshot lacks profile")
    frozen_input_sha = hashlib.sha256(
        _canonical_bytes(frozen_input_contract)
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "fury_current_cat2_supplemental_heldout_replay_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "protocol_relationship": {
            "mode": "SUPPLEMENTAL_DIAGNOSTIC_ONLY",
            "historical_frozen_gate_modified": False,
            "historical_frozen_gate_reinterpreted": False,
            "frozen_gate_path": str(frozen_gate_path.resolve()),
            "frozen_gate_file_sha256": frozen_gate_sha256,
            "frozen_input_contract_sha256": frozen_input_sha,
            "historical_metadata_selection_reconstructed_exactly": True,
            "historical_request_byte_equality_claimed": False,
            "current_reconstructed_request_bytes_content_addressed": True,
        },
        "run_input_snapshot": {
            "capture_phase": "PRE_RUN_WITH_POST_RUN_REVERIFICATION",
            "post_run_reverification_passed": True,
            "file_count": len(run_input_snapshot.files),
            "file_bundle_sha256": run_input_snapshot.file_bundle_sha256,
            "files": [asdict(value) for value in run_input_snapshot.files],
            "request_contract": copy.deepcopy(
                dict(run_input_snapshot.request_contract)
            ),
            "input_lock": (
                asdict(input_lock_identity)
                if input_lock_identity is not None
                else None
            ),
            "full_run_required_preexisting_content_addressed_lock": True,
            "evaluator_python_source_scope": "DIRECT_EXECUTION_SOURCES",
            "evaluator_python_transitive_dependency_closure_claimed": False,
        },
        "frozen_input_contract": copy.deepcopy(dict(frozen_input_contract)),
        "current_cat2": {
            "expert_id": CAT2_ID,
            "artifact": str(cat2_profile_path.resolve()),
            "artifact_file_sha256": cat2_profile_sha256,
            "authority_state": "CURRENT_UNSEALED_SOURCE_PROFILE",
            "provenance_kind": "SOURCE_DERIVED",
            "role_can_vote": False,
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "raw_savedvariables_sha256": cat2_snapshot.get(
                "raw_savedvariables_sha256"
            ),
            "profile_semantic_sha256": cat2_snapshot.get(
                "profile_semantic_sha256"
            ),
            "source_bundle_sha256": cat2_snapshot.get("source_bundle_sha256"),
            "source_bundle_validation_scope": (
                "artifact identities were structurally checked against adapter-pinned "
                "constants; current addon source file bytes were not re-read"
            ),
            "current_addon_source_files_re_read": False,
            "source_execution": False,
            "exact_lua_replay": False,
            "current_profile_unsealed": True,
        },
        "comparison_experts": {
            "cat_id": BASELINE_IDS[0],
            "contra_id": BASELINE_IDS[1],
            "frozen_candidate_id": frozen_input_contract["candidate_policy_id"],
            "current_cat2_id": CAT2_ID,
        },
        "evaluation": copy.deepcopy(dict(evaluation)),
        "simulator_diagnostic_only": True,
        "independent_expert_vote_available": False,
        "heldout_gate_modified": False,
        "heldout_gate_passed": False,
        "deployment_allowed": False,
        "real_game_superiority_claimed": False,
        "claims_excluded": [
            "independent or sealed Cat2 expert evidence",
            "exact Cat, Cat2, or Contra Lua execution",
            "real-game DPS superiority",
            "deployment authorization",
            "modification or extension of the historical frozen held-out gate",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-gate", type=Path, default=DEFAULT_FROZEN_GATE)
    parser.add_argument(
        "--expected-frozen-gate-sha256", default=DEFAULT_FROZEN_GATE_SHA256
    )
    parser.add_argument("--cat2-profile", type=Path, default=DEFAULT_CAT2_PROFILE)
    parser.add_argument(
        "--expected-cat2-profile-sha256", default=DEFAULT_CAT2_PROFILE_SHA256
    )
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument(
        "--stage-directory", type=Path, default=DEFAULT_STAGE_DIRECTORY
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--input-lock",
        type=Path,
        help="required content-addressed input lock for a full replay",
    )
    parser.add_argument("--expected-input-lock-sha256")
    parser.add_argument(
        "--write-input-lock",
        type=Path,
        help="plan-only destination for a newly captured input lock",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="verify and persist the frozen input contract without simulator rollouts",
    )
    mode.add_argument(
        "--smoke-only",
        action="store_true",
        help="run one family/seed as a non-evaluative closed-loop integration smoke",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.plan_only and not args.smoke_only and (
        args.input_lock is None or not args.expected_input_lock_sha256
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            "full replay requires --input-lock and --expected-input-lock-sha256"
        )
    if args.write_input_lock is not None and not args.plan_only:
        raise FuryCurrentCat2HeldoutReplayError(
            "--write-input-lock is allowed only with --plan-only"
        )
    frozen_path = args.frozen_gate.expanduser().resolve()
    cat2_path = args.cat2_profile.expanduser().resolve()
    bridge_path = args.bridge.expanduser().resolve()
    bridge_source_identity = _snapshot_file(
        bridge_path, "simulator_bridge_binary"
    )
    staged_bridge_identity = stage_bridge_binary(
        bridge_source_identity,
        args.stage_directory,
    )
    staged_bridge_path = Path(staged_bridge_identity.path)
    frozen_identity, frozen_gate = _snapshot_json_file(
        frozen_path, "frozen_heldout_gate"
    )
    profile_identity, cat2_snapshot = _snapshot_json_file(
        cat2_path, "cat2_profile_artifact"
    )
    if frozen_identity.sha256 != args.expected_frozen_gate_sha256.casefold():
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen held-out gate SHA-256 does not match the expected content address"
        )
    if profile_identity.sha256 != args.expected_cat2_profile_sha256.casefold():
        raise FuryCurrentCat2HeldoutReplayError(
            "Cat2 profile SHA-256 does not match the expected content address"
        )
    frozen_sha = frozen_identity.sha256
    profile_sha = profile_identity.sha256
    manifest_meta = frozen_gate.get("manifest_snapshot")
    if not isinstance(manifest_meta, Mapping) or not isinstance(
        manifest_meta.get("path"), str
    ):
        raise FuryCurrentCat2HeldoutReplayError(
            "frozen held-out gate lacks its manifest path"
        )
    manifest_path = Path(manifest_meta["path"]).expanduser().resolve()
    document_store = SameByteDocumentStore()
    manifest_identity = document_store.capture(
        manifest_path, "chronicle_manifest"
    )
    manifest_snapshot = load_manifest_snapshot(
        manifest_path,
        require_complete=bool(manifest_meta.get("require_complete")),
        document_loader=document_store.load,
    )
    catalog_identities = tuple(
        document_store.capture(value.path, "completed_scenario_catalog")
        for value in manifest_snapshot.completed_catalogs
    )
    evaluator_sources = capture_direct_execution_sources()
    files, _ = capture_file_inputs(
        frozen_gate=frozen_identity,
        cat2_profile=profile_identity,
        manifest=manifest_identity,
        catalogs=catalog_identities,
        bridge_source=bridge_source_identity,
        staged_bridge=staged_bridge_identity,
        evaluator_sources=evaluator_sources,
    )
    # Construction validates the snapshot's inner semantic/source identity
    # records against adapter-pinned constants.  It does not re-read the live
    # Cat2 addon source files.
    Cat2SavedProfileSourceAdapterV1(cat2_snapshot)
    corpus, input_contract = reconstruct_frozen_corpus(
        frozen_gate,
        manifest_snapshot=manifest_snapshot,
        document_loader=document_store.load,
    )
    document_store.release_documents()
    run_snapshot = make_run_input_snapshot(files, corpus)
    # Verify immediately after catalog reads, then again after all simulator
    # work.  No formal artifact is opened before both checks succeed.
    verify_run_input_snapshot(run_snapshot, corpus)
    lock_identity: InputFileIdentity | None = None
    if args.input_lock is not None:
        if not args.expected_input_lock_sha256:
            raise FuryCurrentCat2HeldoutReplayError(
                "--input-lock requires --expected-input-lock-sha256"
            )
        lock_identity = require_matching_input_lock(
            args.input_lock,
            args.expected_input_lock_sha256,
            run_snapshot,
        )
    if args.write_input_lock is not None:
        _atomic_write_bytes(
            args.write_input_lock,
            _artifact_bytes(run_input_snapshot_document(run_snapshot)),
        )
    candidate_parameters = FuryPolicyParameters(
        **dict(input_contract["candidate_parameters"])
    )
    seeds = tuple(input_contract["validation_seeds"])
    if args.plan_only:
        evaluation: Mapping[str, Any] = {
            "status": "NOT_RUN",
            "evaluation_scope": "NOT_RUN",
            "validation_seeds": list(seeds),
            "matched_seed_contract": True,
            "retained_rollout_rows": False,
            "rollout_count": 0,
            "expected_rollout_count": 0,
            "full_replay_completed": False,
            "diagnostic_quality_passed": False,
            "simulator_diagnostic_only": True,
            "voting_result": False,
            "deployment_gate_passed": False,
            "real_game_superiority_gate_passed": False,
        }
    else:
        evaluation_corpus = corpus
        evaluation_seeds = seeds
        if args.smoke_only:
            evaluation_corpus = SelectedCorpus(
                families=corpus.families[:1],
                instance_summaries=(),
            )
            evaluation_seeds = seeds[:1]
        with SimulatorBridge(staged_bridge_path) as bridge:
            evaluation = evaluate_supplemental_replay(
                bridge,
                evaluation_corpus,
                candidate_parameters=candidate_parameters,
                candidate_policy_id=str(input_contract["candidate_policy_id"]),
                cat2_snapshot=cat2_snapshot,
                validation_seeds=evaluation_seeds,
                progress=lambda value: print(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    flush=True,
                ),
            )
        if args.smoke_only:
            evaluation = dict(evaluation)
            evaluation.update(
                {
                    "status": "SMOKE_COMPLETED",
                    "evaluation_scope": "NON_EVALUATIVE_SMOKE_SUBSET",
                    "frozen_full_family_count": len(corpus.families),
                    "frozen_full_validation_seed_count": len(seeds),
                    "full_replay_completed": False,
                    "diagnostic_quality_passed": False,
                    "voting_result": False,
                    "deployment_gate_passed": False,
                    "real_game_superiority_gate_passed": False,
                }
            )
    verify_run_input_snapshot(run_snapshot, corpus)
    if lock_identity is not None and _snapshot_file(
        Path(lock_identity.path), lock_identity.role
    ) != lock_identity:
        raise FuryCurrentCat2HeldoutReplayError(
            "supplemental input lock changed during simulator execution"
        )
    artifact = build_artifact(
        frozen_gate_path=frozen_path,
        frozen_gate_sha256=frozen_sha,
        frozen_input_contract=input_contract,
        cat2_profile_path=cat2_path,
        cat2_profile_sha256=profile_sha,
        cat2_snapshot=cat2_snapshot,
        run_input_snapshot=run_snapshot,
        input_lock_identity=lock_identity,
        evaluation=evaluation,
    )
    output = args.output
    if output is None:
        output = (
            DEFAULT_PLAN_OUTPUT
            if args.plan_only
            else DEFAULT_SMOKE_OUTPUT
            if args.smoke_only
            else DEFAULT_OUTPUT
        )
    write_artifact_with_receipt(
        output,
        artifact,
        run_input_snapshot=run_snapshot,
    )
    print(
        json.dumps(
            {
                "status": evaluation["status"],
                "family_count": len(corpus.families),
                "validation_seed_count": len(seeds),
                "rollout_count": evaluation["rollout_count"],
                "simulator_diagnostic_only": True,
                "heldout_gate_passed": False,
                "deployment_allowed": False,
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "CAT2_ID",
    "DEFAULT_CAT2_PROFILE_SHA256",
    "DEFAULT_FROZEN_GATE_SHA256",
    "FuryCurrentCat2HeldoutReplayError",
    "InputFileIdentity",
    "RunInputSnapshot",
    "build_artifact",
    "build_request_contract",
    "capture_file_inputs",
    "evaluate_supplemental_replay",
    "main",
    "make_run_input_snapshot",
    "parse_input_lock",
    "reconstruct_frozen_corpus",
    "require_matching_input_lock",
    "run_input_snapshot_document",
    "stage_bridge_binary",
    "verify_run_input_snapshot",
    "write_artifact_with_receipt",
)


if __name__ == "__main__":
    raise SystemExit(main())
