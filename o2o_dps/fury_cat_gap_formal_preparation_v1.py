"""Fail-closed preparation artifacts for the formal Cat-gap execution.

This module has no remote transport and never starts a search.  It turns an
already source-replayed adapter admission into an exact 343-scenario runner
template, binds the immutable Linux runtime closure, records the Windows v11
build/equivalence smoke, and preregisters a no-retention capacity pilot.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

from . import cat2_capability_manifest_v1 as cat2_manifest_v1
from . import chronicle_old50_exact_fury_dynamic_v3_adapter_v1 as adapter_v1
from . import chronicle_old50_warrior_slot_substitution_v2 as selector_v2
from . import contra260817_source_manifest_v1 as contra_manifest_v1
from .cat2new_fury_cat_gap_policy_v1 import (
    FuryCatGapPolicyParametersV1,
    build_cat_gap_policy_v1,
)
from .cat2new_fury_paired_lane_adapter_v3 import cat2new_lane_contract_v3
from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .contra260817_fury_paired_lane_adapter_v4 import (
    contra260817_runner_v4_lane_contract_v4,
)
from .fury_multiseed_evaluation_v2 import derive_seed_set
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_SCHEMA_V1 = "fury_cat_gap_exact_generic_baseline_template/v1"
TEMPLATE_STATUS_V1 = "PASS_STRICT_343_GENERIC_TEMPLATE"
RUNTIME_CLOSURE_SCHEMA_V1 = "fury_cat_gap_exact_linux_runtime_closure/v1"
RUNTIME_CLOSURE_STATUS_V1 = "PASS_EXACT_V11_AND_EXPERT_RUNTIME_CLOSURE"
WINDOWS_BUILD_RECEIPT_SCHEMA_V1 = "fury_cat_gap_windows_v11_build_receipt/v1"
WINDOWS_BUILD_RECEIPT_STATUS_V1 = "PASS_IMMUTABLE_WINDOWS_V11_EQUIVALENCE"
HEAVY_PREPARATION_SCHEMA_V1 = "fury_cat_gap_heavy_preparation_receipt/v1"
HEAVY_PREPARATION_STATUS_V1 = (
    "PASS_FORMAL_343_V11_RUNTIME_CLOSURE_PREPARED_NOT_EXECUTED"
)
PILOT_PLAN_SCHEMA_V1 = "fury_cat_gap_throughput_rss_pilot_plan/v1"
PILOT_PLAN_STATUS_V1 = "PREPARED_960_PROCESS_NO_RETENTION_NOT_EXECUTED"
PILOT_EXECUTION_KIND_V1 = "THROUGHPUT_RSS_PILOT_NO_RETENTION"

EXPECTED_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
EXPECTED_GENERIC_SCENARIOS = 343
EXPECTED_GENERIC_INSTANCES = 14
EXPECTED_PDF_SCENARIOS = 127
EXPECTED_PDF_INSTANCES = 6

EXPECTED_LINUX_V11_BRIDGE_SHA256 = (
    "b9a8cfdebfb715267326c4bf255323e428ec5d49a2e1e5a4c349faa87e186d4f"
)
EXPECTED_LINUX_V11_BRIDGE_SIZE = 17_793_159
EXPECTED_WINDOWS_V11_BRIDGE_SHA256 = (
    "c4b2b87786dd048ba06b7a8aab1af3aecec8a82544258f437c1711a1c1101cce"
)
EXPECTED_WINDOWS_V11_BRIDGE_SIZE = 18_092_544
EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256 = (
    "ebf85b4e64e82a67d7cba2ebd5d7c19d27c08dad8d40b5c694efab958946c8af"
)
EXPECTED_DYNAMIC_V5_RESPONSE_SHA256 = (
    "3842d365c2ebc1b907e0ac20ae03e2d2af290b37d0f77bede0f99073bea413b7"
)

CAT2NEW_RELEASE_ADDRESS = (
    "b58441a64c366cb19215c080086101e37bb177151e7bdacbbc21dcfac96ca700"
)
CAT2NEW_SOURCE_TREE_SHA256 = (
    "f7e659f9042d36b078d4c3ad6860742d9b2946bab455630c6db071c6ebaa6d81"
)
CAT2NEW_CAPABILITY_SHA256 = (
    "3948a01fc1a4dcd33590b8cc023a457526928b22e0f9e00ecd9890808230d164"
)
CAT2NEW_IDENTITY_FILE_SHA256 = (
    "ee40a1fa3993f2882d080e20632e33f788516ce30726aad264619d2a41a1610e"
)
CAT2_CONTEXT_RELEASE_ADDRESS = (
    "1d03fbb0cae173db88e66cb61b18b692a0fdfd3c0212cc9924cc032cf8ab8e8b"
)
CAT2_CONTEXT_IDENTITY_SHA256 = (
    "71bb6023d5eeaa4465268d8ee51644b08a11525b9bf1d3ded1c0065ceef236e1"
)
CAT2_CONTEXT_IDENTITY_FILE_SHA256 = (
    "e264940baa3a0f554c1c5eceebafe2c1001283aae8635d0c40533a1efcc423f5"
)
CAT2_INSTALLED_TREE_SHA256 = (
    "79b299c3028463a0f996d6622de80791f10e13994bddf6e2cd095c483e513cbf"
)
CAT2_SAVEDVARIABLES_SHA256 = (
    "25c3359de03361567a4a4e4bb9f88c711af6f9bebd550b5ac72bb18358c197a0"
)
CONTRA_RELEASE_ADDRESS = (
    "e7586fc95a77e5647fda7407b71ff587353c677a8e044819fef64e10da539fa2"
)
CONTRA_IDENTITY_FILE_SHA256 = (
    "77b269aab2b0db96c659c2fc0fce8ebf1b6d304260a93e0e50f1f520125d22e5"
)

CAT2_CAPABILITY_RELATIVE = (
    "configs/experts/cat2_capabilities_2026_09_10_f7e659f9.json"
)
TEMPLATE_SEED_NAMESPACE_V1 = "brainofcat.fury.cat-gap-template.v1.nonexecuting"
TEMPLATE_MASTER_SEED_V1 = 1
PILOT_SEED_NAMESPACE_V1 = (
    "brainofcat.fury.cat-gap-capacity-pilot.v1.2026-09-12"
)
PILOT_WORKERS_PER_NODE_V1 = 160
PILOT_TASK_COUNT_V1 = 960
PILOT_LANE_COUNT_V1 = 3
PILOT_EFFICIENCY_THRESHOLD_V1 = 0.70
PILOT_MEMORY_FRACTION_LIMIT_V1 = 0.80
PILOT_SERIAL_TIMEOUT_SECONDS_V1 = 900
PILOT_BATCH_TIMEOUT_SECONDS_V1 = 1_200

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class FuryCatGapFormalPreparationV1Error(RuntimeError):
    """A formal launch artifact was incomplete, inconsistent, or executable."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapFormalPreparationV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryCatGapFormalPreparationV1Error(f"{label} must be an array")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise FuryCatGapFormalPreparationV1Error(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryCatGapFormalPreparationV1Error(f"{label} must be positive")
    return value


def _canonical_bytes(value: Any, *, trailing_lf: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload + (b"\n" if trailing_lf else b"")


def _address(core: Mapping[str, Any]) -> JSONMap:
    return {
        **deepcopy(dict(core)),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _HashingRaw(io.RawIOBase):
    def __init__(self, source: Any) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:  # pragma: no cover - queried by BufferedReader
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self.source.read(len(buffer))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self.digest.update(chunk)
        self.bytes_read += len(chunk)
        return len(chunk)


def _validate_adapter_ready_plan(value: Mapping[str, Any]) -> JSONMap:
    from .fury_cat_gap_search_plan_v1 import (
        STATUS_ADAPTER_READY,
        validate_cat_gap_search_plan_v1,
    )

    checked = validate_cat_gap_search_plan_v1(value)
    if checked.get("status") != STATUS_ADAPTER_READY:
        raise FuryCatGapFormalPreparationV1Error(
            "exact template requires the strict adapter-ready plan"
        )
    admission = _mapping(checked.get("adapter_admission"), "adapter admission")
    if (
        admission.get("real_artifact_validated") is not True
        or admission.get("validated_generic_wave_count")
        != EXPECTED_GENERIC_SCENARIOS
        or admission.get("validated_generic_instance_count")
        != EXPECTED_GENERIC_INSTANCES
        or admission.get("validated_pdf_wave_count") != EXPECTED_PDF_SCENARIOS
        or admission.get("validated_pdf_instance_count") != EXPECTED_PDF_INSTANCES
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "adapter admission does not close to formal 343/127 and 14/6"
        )
    return checked


def _canonical_baselines() -> tuple[JSONMap, JSONMap]:
    # The core reconstructs these rows from source manifests and current adapter
    # files.  Import lazily to avoid a module-import cycle when the core calls us.
    from .fury_cat_gap_hpc_plan_v1 import _canonical_baseline_policy_rows_v1

    return tuple(deepcopy(row) for row in _canonical_baseline_policy_rows_v1())  # type: ignore[return-value]


def _load_materialized_manifest(path: Path) -> tuple[JSONMap, str]:
    resolved = path.expanduser().resolve()
    if resolved.name != "manifest.json":
        raise FuryCatGapFormalPreparationV1Error(
            "formal template must start from stable manifest.json"
        )
    try:
        payload = resolved.read_bytes()
        value = json.loads(payload.decode("utf-8"))
        manifest = adapter_v1._validate_materialized_manifest_document(
            _mapping(value, "materialized manifest")
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError,
    ) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"materialized manifest validation failed: {error}"
        ) from error
    if payload != _canonical_bytes(manifest, trailing_lf=True):
        raise FuryCatGapFormalPreparationV1Error(
            "materialized manifest is not canonical JSON with one LF"
        )
    addressed = resolved.parent / (
        f"{adapter_v1.MATERIALIZED_MANIFEST_PREFIX}."
        f"{manifest['content_address']['sha256']}.manifest.json"
    )
    try:
        if addressed.read_bytes() != payload:
            raise FuryCatGapFormalPreparationV1Error(
                "stable and addressed materialized manifests differ"
            )
    except OSError as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"addressed materialized manifest is unavailable: {error}"
        ) from error
    return manifest, hashlib.sha256(payload).hexdigest()


def _scan_generic_partitions_v1(
    manifest: Mapping[str, Any], manifest_path: Path
) -> tuple[tuple[JSONMap, ...], list[JSONMap], list[str], list[str]]:
    generic_entries = [
        _mapping(row, "materialized instance")
        for row in _array(manifest.get("instances"), "materialized instances")
        if _mapping(row, "materialized instance").get("selection_lane")
        == selector_v2.GENERIC_LANE
    ]
    pdf_entries = [
        row
        for row in _array(manifest.get("instances"), "materialized instances")
        if _mapping(row, "materialized instance").get("selection_lane")
        == selector_v2.PDF_LANE
    ]
    if len(generic_entries) != EXPECTED_GENERIC_INSTANCES or len(pdf_entries) != (
        EXPECTED_PDF_INSTANCES
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "manifest lane split is not exactly 14 generic and 6 PDF instances"
        )

    proofs: list[JSONMap] = []
    scenario_rows: list[tuple[str, str, JSONMap, str]] = []
    seen_source_rows: set[str] = set()
    for entry in generic_entries:
        descriptor = _mapping(entry.get("partition"), "partition descriptor")
        partition_path = manifest_path.parent / str(descriptor.get("path"))
        logical = hashlib.sha256()
        logical_size = 0
        record_count = 0
        raw_hashing: _HashingRaw | None = None
        try:
            with partition_path.open("rb") as raw_handle:
                raw_hashing = _HashingRaw(raw_handle)
                with io.BufferedReader(raw_hashing) as buffered:
                    with gzip.GzipFile(fileobj=buffered, mode="rb") as archive:
                        for raw_line in archive:
                            logical.update(raw_line)
                            logical_size += len(raw_line)
                            record_count += 1
                            value = json.loads(raw_line.decode("utf-8"))
                            artifact_map = _mapping(value, "generic adapter row")
                            if raw_line != _canonical_bytes(
                                artifact_map, trailing_lf=True
                            ):
                                raise FuryCatGapFormalPreparationV1Error(
                                    "generic partition contains noncanonical JSONL"
                                )
                            artifact = (
                                adapter_v1.validate_exact_fury_overlay_dynamic_v3_structure_v1(
                                    artifact_map
                                )
                            )
                            training = _mapping(
                                artifact.get("expert_training_projection"),
                                "training projection",
                            )
                            bindings = _mapping(
                                artifact.get("source_bindings"), "source bindings"
                            )
                            join = _mapping(
                                bindings.get("wave_identity_join"), "wave identity join"
                            )
                            join_key = _array(join.get("join_key"), "wave join key")
                            source_sha = _sha(
                                bindings.get("overlay_wave_content_sha256"),
                                "overlay wave SHA",
                            )
                            if (
                                training.get("selection_lane")
                                != selector_v2.GENERIC_LANE
                                or training.get("selected_guid")
                                != entry.get("selected_guid")
                                or not join_key
                                or join_key[0] != entry.get("instance_id")
                                or source_sha in seen_source_rows
                            ):
                                raise FuryCatGapFormalPreparationV1Error(
                                    "generic row lane, GUID, instance, or source membership differs"
                                )
                            seen_source_rows.add(source_sha)
                            scenario = _mapping(artifact.get("scenario"), "scenario")
                            normalized = normalize_runner_scenarios([scenario])[0]
                            if normalized != scenario or scenario.get(
                                "instance_id"
                            ) != entry.get("instance_id"):
                                raise FuryCatGapFormalPreparationV1Error(
                                    "generic row scenario is not canonical or instance-bound"
                                )
                            scenario_rows.append(
                                (
                                    str(scenario["instance_id"]),
                                    str(scenario["scenario_id"]),
                                    dict(scenario),
                                    _sha(
                                        _mapping(
                                            artifact.get("content_address"),
                                            "artifact content address",
                                        ).get("sha256"),
                                        "artifact SHA",
                                    ),
                                )
                            )
        except FuryCatGapFormalPreparationV1Error:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            gzip.BadGzipFile,
            adapter_v1.ChronicleOld50ExactFuryDynamicV3AdapterError,
        ) as error:
            raise FuryCatGapFormalPreparationV1Error(
                f"could not validate generic partition {partition_path}: {error}"
            ) from error
        assert raw_hashing is not None
        if (
            raw_hashing.bytes_read != descriptor.get("compressed_size_bytes")
            or raw_hashing.digest.hexdigest()
            != descriptor.get("compressed_file_sha256")
            or logical_size != descriptor.get("logical_size_bytes")
            or logical.hexdigest() != descriptor.get("logical_content_sha256")
            or record_count != descriptor.get("record_count")
        ):
            raise FuryCatGapFormalPreparationV1Error(
                "generic partition byte/count proof differs from manifest"
            )
        proofs.append(
            {
                "instance_id": entry["instance_id"],
                "manifest_entry_content_sha256": _sha(
                    _mapping(entry.get("content_address"), "entry address").get(
                        "sha256"
                    ),
                    "entry content SHA",
                ),
                "selected_guid": entry["selected_guid"],
                "selection_lane": entry["selection_lane"],
                "partition_path": descriptor["path"],
                "record_count": record_count,
                "logical_content_sha256": logical.hexdigest(),
                "logical_size_bytes": logical_size,
                "compressed_file_sha256": raw_hashing.digest.hexdigest(),
                "compressed_size_bytes": raw_hashing.bytes_read,
                "gzip_mtime": descriptor["gzip_mtime"],
            }
        )

    scenario_rows.sort(key=lambda row: (row[0], row[1]))
    scenarios = tuple(row[2] for row in scenario_rows)
    artifact_shas = [row[3] for row in scenario_rows]
    scenario_shas = [str(row[2]["scenario_contract_sha256"]) for row in scenario_rows]
    if (
        len(scenarios) != EXPECTED_GENERIC_SCENARIOS
        or len(seen_source_rows) != EXPECTED_GENERIC_SCENARIOS
        or len({(row[0], row[1]) for row in scenario_rows})
        != EXPECTED_GENERIC_SCENARIOS
        or len(set(artifact_shas)) != EXPECTED_GENERIC_SCENARIOS
        or sum(int(row["partition"]["record_count"]) for row in pdf_entries)
        != EXPECTED_PDF_SCENARIOS
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "generic reconstruction does not close to 343 unique scenarios"
        )
    return scenarios, proofs, artifact_shas, scenario_shas


def _build_template_wrapper_v1(
    checked_plan: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_file_sha256: str,
    scenarios: Sequence[Mapping[str, Any]],
    partition_proofs: Sequence[Mapping[str, Any]],
    artifact_shas: Sequence[str],
    scenario_shas: Sequence[str],
    bridge_identity: Mapping[str, Any],
    execution_bundle_identity: Mapping[str, Any],
) -> JSONMap:
    policies = _canonical_baselines()
    lane_contracts = (
        cat_runner_v4_lane_contract_v6(),
        contra260817_runner_v4_lane_contract_v4(),
    )
    manifest_sha = str(manifest["content_address"]["sha256"])
    scenario_bundle_sha = runner_scenario_bundle_sha256(scenarios)
    proof_binding = sha256_json(
        {
            "materialized_manifest_content_sha256": manifest_sha,
            "partition_proofs": list(partition_proofs),
            "artifact_content_sha256s": list(artifact_shas),
            "scenario_contract_sha256s": list(scenario_shas),
            "scenario_bundle_sha256": scenario_bundle_sha,
        }
    )
    protocol = {
        "schema": TEMPLATE_SCHEMA_V1,
        "adapter_ready_plan_sha256": checked_plan["plan_sha256"],
        "materialized_manifest_content_sha256": manifest_sha,
        "proof_binding_sha256": proof_binding,
    }
    runner = build_runner_plan(
        protocol_id="fury-cat-gap-exact-generic-template-v1",
        protocol_sha256=sha256_json(protocol),
        phase="formal_generic_template_nonexecuting",
        corpus_manifest_sha256=manifest_sha,
        runner_inputs_sha256=sha256_json(
            {
                "source_bindings": manifest["source_bindings"],
                "materialization_parameters": manifest["materialization_parameters"],
                "baseline_policies": list(policies),
            }
        ),
        runner_scenario_bundle_sha256=scenario_bundle_sha,
        corpus_binding_sha256=proof_binding,
        master_seeds=(TEMPLATE_MASTER_SEED_V1,),
        scenarios=scenarios,
        policies=policies,
        shard_count=1,
        bridge_identity=bridge_identity,
        execution_bundle_identity=execution_bundle_identity,
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=TEMPLATE_SEED_NAMESPACE_V1,
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=lane_contracts,
    )
    runner["generated_at"] = "DETERMINISTIC_FORMAL_GENERIC_TEMPLATE_V1"
    core = {
        "schema": TEMPLATE_SCHEMA_V1,
        "status": TEMPLATE_STATUS_V1,
        "adapter_ready_plan_sha256": checked_plan["plan_sha256"],
        "materialized_manifest_identity": {
            "content_sha256": manifest_sha,
            "file_sha256": manifest_file_sha256,
        },
        "source_bindings": deepcopy(manifest["source_bindings"]),
        "materialization_parameters_sha256": sha256_json(
            manifest["materialization_parameters"]
        ),
        "lane_contract": {
            "selection_lane": selector_v2.GENERIC_LANE,
            "generic_instance_count": EXPECTED_GENERIC_INSTANCES,
            "generic_scenario_count": EXPECTED_GENERIC_SCENARIOS,
            "pdf_instance_count_excluded": EXPECTED_PDF_INSTANCES,
            "pdf_scenario_count_excluded": EXPECTED_PDF_SCENARIOS,
        },
        "partition_proofs": list(deepcopy(partition_proofs)),
        "artifact_content_sha256s": list(artifact_shas),
        "scenario_contract_sha256s": list(scenario_shas),
        "scenario_bundle_sha256": scenario_bundle_sha,
        "baseline_policy_binding": {
            "policies": list(policies),
            "lane_contracts": list(lane_contracts),
        },
        "bridge_identity": deepcopy(dict(runner["contract"]["bridge_identity"])),
        "execution_bundle_identity": deepcopy(
            dict(runner["contract"]["execution_bundle_identity"])
        ),
        "runner_plan": runner,
        "execution_started": False,
        "heavy_execution_started": False,
        "retention_allowed": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


def build_exact_generic_baseline_template_v1(
    adapter_ready_plan: Mapping[str, Any],
    *,
    materialized_manifest_path: str | Path,
    bridge_identity: Mapping[str, Any],
    execution_bundle_identity: Mapping[str, Any],
) -> JSONMap:
    """Stream the 14 formal generic partitions and build their exact template."""

    checked = _validate_adapter_ready_plan(adapter_ready_plan)
    path = Path(materialized_manifest_path).expanduser().resolve()
    manifest, file_sha = _load_materialized_manifest(path)
    admission = _mapping(checked.get("adapter_admission"), "adapter admission")
    if (
        manifest["content_address"]["sha256"]
        != admission.get("materialized_manifest_content_sha256")
        or manifest.get("source_bindings")
        != admission.get("validated_source_bindings")
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "materialized manifest differs from strict adapter admission"
        )
    scenarios, proofs, artifact_shas, scenario_shas = _scan_generic_partitions_v1(
        manifest, path
    )
    return _build_template_wrapper_v1(
        checked,
        manifest=manifest,
        manifest_file_sha256=file_sha,
        scenarios=scenarios,
        partition_proofs=proofs,
        artifact_shas=artifact_shas,
        scenario_shas=scenario_shas,
        bridge_identity=bridge_identity,
        execution_bundle_identity=execution_bundle_identity,
    )


def validate_exact_generic_baseline_template_v1(
    value: Mapping[str, Any],
    adapter_ready_plan: Mapping[str, Any],
    *,
    materialized_manifest_path: str | Path,
) -> JSONMap:
    """Reopen every source partition and require exact wrapper reconstruction."""

    observed = deepcopy(dict(_mapping(value, "formal generic template")))
    bridge = _mapping(observed.get("bridge_identity"), "bridge identity")
    bundle = _mapping(
        observed.get("execution_bundle_identity"), "execution bundle identity"
    )
    rebuilt = build_exact_generic_baseline_template_v1(
        adapter_ready_plan,
        materialized_manifest_path=materialized_manifest_path,
        bridge_identity=bridge,
        execution_bundle_identity=bundle,
    )
    if observed != rebuilt:
        raise FuryCatGapFormalPreparationV1Error(
            "formal generic template differs from its 14-partition reconstruction"
        )
    validate_runner_plan(_mapping(observed.get("runner_plan"), "runner plan"))
    return observed


def build_single_scenario_smoke_template_v1(
    formal_template: Mapping[str, Any],
    *,
    bridge_identity: Mapping[str, Any],
    execution_bundle_identity: Mapping[str, Any],
) -> JSONMap:
    """Project the cheapest canonical formal scenario for a bounded Windows smoke."""

    wrapper = _mapping(formal_template, "formal template")
    address = _mapping(wrapper.get("content_address"), "template address")
    core = {key: value for key, value in wrapper.items() if key != "content_address"}
    if (
        wrapper.get("schema") != TEMPLATE_SCHEMA_V1
        or wrapper.get("status") != TEMPLATE_STATUS_V1
        or address.get("sha256") != sha256_json(core)
        or wrapper.get("retention_allowed") is not False
    ):
        raise FuryCatGapFormalPreparationV1Error("formal template is not sealed")
    source_runner = validate_runner_plan(
        _mapping(wrapper.get("runner_plan"), "formal runner plan")
    )
    scenarios = list(source_runner["contract"]["scenarios"])
    if len(scenarios) != EXPECTED_GENERIC_SCENARIOS:
        raise FuryCatGapFormalPreparationV1Error(
            "formal template does not contain exactly 343 scenarios"
        )
    chosen = min(
        scenarios,
        key=lambda row: (
            int(row["estimated_cost_units"]),
            str(row["instance_id"]),
            str(row["scenario_id"]),
        ),
    )
    policies = _canonical_baselines()
    protocol = {
        "schema": "fury_cat_gap_windows_smoke_template/v1",
        "formal_template_sha256": address["sha256"],
        "scenario_contract_sha256": chosen["scenario_contract_sha256"],
    }
    runner = build_runner_plan(
        protocol_id="fury-cat-gap-windows-smoke-template-v1",
        protocol_sha256=sha256_json(protocol),
        phase="windows_smoke_template_nonexecuting",
        corpus_manifest_sha256=wrapper["materialized_manifest_identity"][
            "content_sha256"
        ],
        runner_inputs_sha256=address["sha256"],
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([chosen]),
        corpus_binding_sha256=sha256_json(protocol),
        master_seeds=(TEMPLATE_MASTER_SEED_V1,),
        scenarios=(chosen,),
        policies=policies,
        shard_count=1,
        bridge_identity=bridge_identity,
        execution_bundle_identity=execution_bundle_identity,
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=TEMPLATE_SEED_NAMESPACE_V1,
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(
            cat_runner_v4_lane_contract_v6(),
            contra260817_runner_v4_lane_contract_v4(),
        ),
    )
    runner["generated_at"] = "DETERMINISTIC_WINDOWS_SMOKE_TEMPLATE_V1"
    return runner


def validate_v11_activation_receipt_v1(value: Mapping[str, Any]) -> JSONMap:
    """Validate a complete six-node dynamic-v5 v11 activation semantically."""

    receipt = deepcopy(dict(_mapping(value, "v11 activation receipt")))
    from .hpc_dynamic_environment_v5 import (
        HpcDynamicEnvironmentV5Error,
        verify_receipt_v5,
    )

    try:
        verify_receipt_v5(receipt)
    except HpcDynamicEnvironmentV5Error as error:
        raise FuryCatGapFormalPreparationV1Error(str(error)) from error
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "activation address")
    bridge = _mapping(receipt.get("bridge"), "activation bridge")
    contract = _mapping(receipt.get("contract"), "activation contract")
    scope = _mapping(receipt.get("execution_scope"), "activation execution scope")
    route = _mapping(receipt.get("route"), "activation route")
    release = _mapping(receipt.get("release"), "activation release")
    probe = _mapping(receipt.get("probe"), "activation probe")
    if (
        receipt.get("schema") != "o2o_hpc_dynamic_environment_receipt/v5"
        or receipt.get("status")
        != "DYNAMIC_V5_V11_BRIDGE_READY_NONSCIENTIFIC"
        or address.get("sha256") != sha256_json(core)
        or bridge.get("sha256") != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or bridge.get("size_bytes") != EXPECTED_LINUX_V11_BRIDGE_SIZE
        or bridge.get("architecture") != "x86_64"
        or bridge.get("elf_class") != "ELF64"
        or bridge.get("pt_interp") is not False
        or contract.get("sha256")
        != "f40b8f0ee77c93b8414df4f524081722221313728f409ac43e085de1ddfc4fc9"
        or route.get("nodes") != list(EXPECTED_NODES)
        or route.get("control_plane") != "jtl110gpu2"
        or route.get("node_transport") != "scheduler_run_on"
        or probe.get("payload_sha256") != EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256
        or probe.get("command_count") != 13
        or probe.get("processes_per_node") != 1
        or probe.get("bridge_command") != "load_dynamic_v3"
        or scope
        != {
            "dynamic_v4_pointer_mutated": False,
            "formal_policy_runner_connected": False,
            "multiseed_started": False,
            "processes_per_node": 1,
            "scientific_experiment_started": False,
            "total_processes": 6,
            "training_started": False,
        }
        or release.get("status") != "READY"
        or release.get("v4_pointer_unchanged") is not True
        or release.get("release_relative_path")
        != (
            "scheduleurm_work/o2o-dps-hpc/releases/dynamic-v5/"
            + EXPECTED_LINUX_V11_BRIDGE_SHA256
        )
        or release.get("pointer_relative_path")
        != "scheduleurm_work/o2o-dps-hpc/current-bridge-dynamic-v5"
        or release.get("pointer_bridge_relative_path")
        != (
            "scheduleurm_work/o2o-dps-hpc/current-bridge-dynamic-v5/"
            "o2obridge.linux-amd64"
        )
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "dynamic-v5 activation is not the complete non-scientific v11 contract"
        )
    expected_pointer = "releases/dynamic-v5/" + EXPECTED_LINUX_V11_BRIDGE_SHA256
    for field in ("release_verification", "pointer_verification"):
        rows = _array(release.get(field), f"activation {field}")
        if (
            [row.get("name") for row in rows] != list(EXPECTED_NODES)
            or any(
                not isinstance(row, Mapping)
                or row.get("status") != "READY"
                or row.get("transport_returncode") != 0
                or row.get("verified_sha256")
                != EXPECTED_LINUX_V11_BRIDGE_SHA256
                or row.get("pointer_target")
                != (None if field == "release_verification" else expected_pointer)
                for row in rows
            )
        ):
            raise FuryCatGapFormalPreparationV1Error(
                f"activation {field} is not READY on all six nodes"
            )
    pre_load = _array(receipt.get("pre_activation_load"), "pre-activation load")
    post_load = _array(release.get("post_activation_load"), "post-activation load")
    for label, rows in (("pre", pre_load), ("post", post_load)):
        if (
            [row.get("name") for row in rows] != list(EXPECTED_NODES)
            or any(
                not isinstance(row, Mapping)
                or row.get("status") != "READY"
                or row.get("transport_returncode") != 0
                or not isinstance(row.get("v4_pointer_target"), str)
                or not row.get("v4_pointer_target")
                for row in rows
            )
        ):
            raise FuryCatGapFormalPreparationV1Error(
                f"activation {label}-load inventory is incomplete"
            )
    if (
        len({row["v4_pointer_target"] for row in pre_load}) != 1
        or [row["v4_pointer_target"] for row in pre_load]
        != [row["v4_pointer_target"] for row in post_load]
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "dynamic-v4 pointer changed during v11 activation"
        )
    smokes = _array(release.get("smoke"), "activation smoke")
    if [row.get("name") for row in smokes] != list(EXPECTED_NODES):
        raise FuryCatGapFormalPreparationV1Error(
            "activation smoke does not cover node001--node006 exactly"
        )
    for row in smokes:
        summary = _mapping(row.get("summary"), "activation smoke summary")
        if (
            row.get("status") != "READY"
            or row.get("transport_returncode") != 0
            or row.get("reason") is not None
            or summary.get("schema") != "o2o_hpc_dynamic_probe_summary/v5"
            or summary.get("status") != "READY"
            or summary.get("bridge_sha256")
            != EXPECTED_LINUX_V11_BRIDGE_SHA256
            or summary.get("response_sha256")
            != EXPECTED_DYNAMIC_V5_RESPONSE_SHA256
            or summary.get("process_count") != 1
            or summary.get("attackable_horizon_verified") is not True
            or summary.get("failures") != []
        ):
            raise FuryCatGapFormalPreparationV1Error(
                "activation v11 semantic smoke is incomplete"
            )
    return receipt


def _expected_cat2_context_identity_v1() -> JSONMap:
    core = {
        "schema": "cat2_runtime_context_binding/v1",
        "release_content_address": CAT2_CONTEXT_RELEASE_ADDRESS,
        "installed_tree": {
            "algorithm": "sha256-canonical-file-manifest-v1",
            "relative_root": "Cat2",
            "file_count": 510,
            "byte_count": 1_374_050,
            "sha256": CAT2_INSTALLED_TREE_SHA256,
        },
        "savedvariables": {
            "relative_path": "Cat2.lua",
            "size_bytes": 1_898,
            "sha256": CAT2_SAVEDVARIABLES_SHA256,
        },
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": sha256_json(core),
        },
    }


def validate_runtime_node_identity_probe_v1(value: Mapping[str, Any]) -> JSONMap:
    probe = deepcopy(dict(_mapping(value, "runtime node identity probe")))
    core = deepcopy(probe)
    address = _mapping(core.pop("content_address", None), "node probe address")
    rows = _array(probe.get("nodes"), "node identity rows")
    exact = {
        "bridge_sha256": EXPECTED_LINUX_V11_BRIDGE_SHA256,
        "cat2new_identity_file_sha256": CAT2NEW_IDENTITY_FILE_SHA256,
        "cat2new_source_tree_sha256": CAT2NEW_SOURCE_TREE_SHA256,
        "cat2_capability_manifest_sha256": CAT2NEW_CAPABILITY_SHA256,
        "cat2_context_identity_file_sha256": CAT2_CONTEXT_IDENTITY_FILE_SHA256,
        "cat2_installed_tree_sha256": CAT2_INSTALLED_TREE_SHA256,
        "cat2_savedvariables_sha256": CAT2_SAVEDVARIABLES_SHA256,
        "contra_identity_file_sha256": CONTRA_IDENTITY_FILE_SHA256,
        "contra_code_manifest_sha256": contra_manifest_v1.CODE_MANIFEST_SHA256,
        "contra_manifest_sha256": contra_manifest_v1.EXPECTED_MANIFEST_SHA256,
    }
    if (
        set(core)
        != {
            "schema",
            "status",
            "nodes",
            "read_only",
            "remote_mutation_performed",
            "network_requests_made",
        }
        or probe.get("schema") != "fury_cat_gap_runtime_node_identity_probe/v1"
        or probe.get("status") != "PASS_EXACT_RUNTIME_IDENTITIES_ON_SIX_NODES"
        or address.get("sha256") != sha256_json(core)
        or [row.get("name") for row in rows] != list(EXPECTED_NODES)
        or any(
            not isinstance(row, Mapping)
            or set(row) != {"name", "status", *exact.keys()}
            or row.get("status") != "READY"
            or any(row.get(field) != expected for field, expected in exact.items())
            for row in rows
        )
        or probe.get("read_only") is not True
        or probe.get("remote_mutation_performed") is not False
        or probe.get("network_requests_made") != 0
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "runtime identity probe does not prove the exact closure on six nodes"
        )
    return probe


def _validate_runtime_release_identities_v1(value: Mapping[str, Any]) -> JSONMap:
    bundle = deepcopy(dict(_mapping(value, "runtime release identities")))
    if set(bundle) != {
        "python_source",
        "cat2new",
        "cat2_context",
        "contra260817",
        "node_identity_probe",
    }:
        raise FuryCatGapFormalPreparationV1Error(
            "runtime identity bundle field set differs"
        )
    source = _mapping(bundle["python_source"], "Python source release")
    source_closure_sha = _sha(
        source.get("source_closure_sha256"), "Python source closure SHA"
    )
    archive_sha = _sha(source.get("archive_sha256"), "Python source archive SHA")
    source_relative = _safe_home_relative(
        source.get("release_relative_path"), "Python source release path"
    )
    if not source_relative.endswith(source_closure_sha):
        raise FuryCatGapFormalPreparationV1Error(
            "Python source release path is not addressed by its closure"
        )
    expected_source_relative = _join_posix(
        PurePosixPath(source_relative).parts[0],
        *PurePosixPath(source_relative).parts[1:],
    )
    if expected_source_relative != source_relative:
        raise FuryCatGapFormalPreparationV1Error("Python source path is not canonical")

    cat2new = _mapping(bundle["cat2new"], "Cat2_new identity")
    cat2new_doc = _mapping(cat2new.get("document"), "Cat2_new identity document")
    if (
        set(cat2new) != {"file_sha256", "document"}
        or cat2new.get("file_sha256") != CAT2NEW_IDENTITY_FILE_SHA256
        or cat2new_doc.get("schema") != "cat2new_remote_runtime_binding/v1"
        or cat2new_doc.get("binding_sha256") != CAT2NEW_RELEASE_ADDRESS
        or _mapping(cat2new_doc.get("source_tree"), "Cat2_new tree").get(
            "sha256"
        )
        != CAT2NEW_SOURCE_TREE_SHA256
        or _mapping(cat2new_doc.get("manifest"), "Cat2_new manifest").get(
            "sha256"
        )
        != CAT2NEW_CAPABILITY_SHA256
    ):
        raise FuryCatGapFormalPreparationV1Error("Cat2_new release identity differs")

    context = _mapping(bundle["cat2_context"], "Cat2 context identity")
    if (
        set(context) != {"file_sha256", "document"}
        or context.get("file_sha256") != CAT2_CONTEXT_IDENTITY_FILE_SHA256
        or context.get("document") != _expected_cat2_context_identity_v1()
    ):
        raise FuryCatGapFormalPreparationV1Error("Cat2 runtime context differs")

    contra = _mapping(bundle["contra260817"], "Contra260817 identity")
    contra_doc = _mapping(contra.get("document"), "Contra identity document")
    if (
        set(contra) != {"file_sha256", "document"}
        or contra.get("file_sha256") != CONTRA_IDENTITY_FILE_SHA256
        or contra_doc.get("schema") != "contra260817_remote_runtime_binding/v1"
        or _mapping(contra_doc.get("content_address"), "Contra address").get(
            "sha256"
        )
        != CONTRA_RELEASE_ADDRESS
        or _mapping(contra_doc.get("code_manifest"), "Contra code manifest").get(
            "sha256"
        )
        != contra_manifest_v1.CODE_MANIFEST_SHA256
        or _mapping(contra_doc.get("manifest"), "Contra manifest").get("sha256")
        != contra_manifest_v1.EXPECTED_MANIFEST_SHA256
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "Contra260817 release identity differs"
        )
    validate_runtime_node_identity_probe_v1(
        _mapping(bundle["node_identity_probe"], "node identity probe")
    )
    return bundle


def _safe_home_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise FuryCatGapFormalPreparationV1Error(
            f"{label} must be a relative POSIX path"
        )
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise FuryCatGapFormalPreparationV1Error(
            f"{label} must stay under the remote home"
        )
    return parsed.as_posix()


def _join_posix(*parts: str) -> str:
    return PurePosixPath(*parts).as_posix()


def _activation_identity_v1(receipt: Mapping[str, Any]) -> JSONMap:
    checked = validate_v11_activation_receipt_v1(receipt)
    smokes = checked["release"]["smoke"]
    return {
        "content_sha256": checked["content_address"]["sha256"],
        "bridge_sha256": checked["bridge"]["sha256"],
        "contract_sha256": checked["contract"]["sha256"],
        "payload_sha256": checked["probe"]["payload_sha256"],
        "response_sha256": smokes[0]["summary"]["response_sha256"],
        "node_count": len(smokes),
    }


def build_exact_linux_runtime_closure_v1(
    *,
    shared_root: str,
    activation_receipt: Mapping[str, Any],
    runtime_release_identities: Mapping[str, Any],
) -> JSONMap:
    """Bind all expert runtime paths and their six-node read-only identity proof."""

    shared = _safe_home_relative(shared_root, "shared root")
    identities = _validate_runtime_release_identities_v1(
        runtime_release_identities
    )
    source = identities["python_source"]
    expected_source_release = _join_posix(
        shared,
        "releases/fury-multiseed-source",
        source["source_closure_sha256"],
    )
    if source["release_relative_path"] != expected_source_release:
        raise FuryCatGapFormalPreparationV1Error(
            "Python source release is outside the exact addressed release path"
        )
    cat2new_release = _join_posix(
        shared, "releases", "cat2new-runtime", CAT2NEW_RELEASE_ADDRESS
    )
    context_release = _join_posix(
        shared, "releases", "cat2-runtime-context", CAT2_CONTEXT_RELEASE_ADDRESS
    )
    contra_release = _join_posix(
        shared, "releases", "contra260817-runtime", CONTRA_RELEASE_ADDRESS
    )
    environment = {
        "PYTHONPATH": source["release_relative_path"],
        "BOC_CAT2NEW_ROOT": _join_posix(cat2new_release, "Cat2_new"),
        "BOC_CAT2_CAPABILITY_MANIFEST": _join_posix(
            cat2new_release, CAT2_CAPABILITY_RELATIVE
        ),
        "BOC_CAT2_INSTALLED_ROOT": _join_posix(context_release, "Cat2"),
        "BOC_CAT2_SAVEDVARIABLES": _join_posix(context_release, "Cat2.lua"),
        "BOC_CONTRA260817_ROOT": _join_posix(contra_release, "Contra_new"),
        "BOC_CONTRA260817_MANIFEST": _join_posix(contra_release, "manifest.json"),
        "GOMAXPROCS": "1",
    }
    identity_paths = {
        "cat2new": _join_posix(cat2new_release, "cat2new-runtime-identity.json"),
        "cat2_context": _join_posix(
            shared,
            "derived/cat-gap-runtime-context/v1/cat2-runtime-context-identity.json",
        ),
        "contra260817": _join_posix(
            contra_release, "contra260817-runtime-identity.json"
        ),
    }
    activation = _activation_identity_v1(activation_receipt)
    node_probe = identities["node_identity_probe"]
    core = {
        "schema": RUNTIME_CLOSURE_SCHEMA_V1,
        "status": RUNTIME_CLOSURE_STATUS_V1,
        "shared_root": shared,
        "python_source_identity": {
            "source_closure_sha256": source["source_closure_sha256"],
            "archive_sha256": source["archive_sha256"],
            "release_relative_path": source["release_relative_path"],
        },
        "activation_identity": activation,
        "runtime_release_identities": {
            "cat2new_release_address": CAT2NEW_RELEASE_ADDRESS,
            "cat2new_source_tree_sha256": CAT2NEW_SOURCE_TREE_SHA256,
            "cat2_capability_manifest_sha256": CAT2NEW_CAPABILITY_SHA256,
            "cat2_context_release_address": CAT2_CONTEXT_RELEASE_ADDRESS,
            "cat2_context_identity_sha256": CAT2_CONTEXT_IDENTITY_SHA256,
            "cat2_installed_tree_sha256": CAT2_INSTALLED_TREE_SHA256,
            "cat2_savedvariables_sha256": CAT2_SAVEDVARIABLES_SHA256,
            "contra_release_address": CONTRA_RELEASE_ADDRESS,
            "contra_code_manifest_sha256": contra_manifest_v1.CODE_MANIFEST_SHA256,
            "contra_manifest_sha256": contra_manifest_v1.EXPECTED_MANIFEST_SHA256,
        },
        "identity_document_paths": identity_paths,
        "identity_document_file_sha256": {
            "cat2new": CAT2NEW_IDENTITY_FILE_SHA256,
            "cat2_context": CAT2_CONTEXT_IDENTITY_FILE_SHA256,
            "contra260817": CONTRA_IDENTITY_FILE_SHA256,
        },
        "environment": environment,
        "environment_binding_sha256": sha256_json(environment),
        "node_identity_probe_sha256": node_probe["content_address"]["sha256"],
        "node_identity_probe": node_probe,
        "node_count": len(EXPECTED_NODES),
        "ambient_runtime_fallback_allowed": False,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


def validate_exact_linux_runtime_closure_v1(value: Mapping[str, Any]) -> JSONMap:
    closure = deepcopy(dict(_mapping(value, "Linux runtime closure")))
    core = deepcopy(closure)
    address = _mapping(core.pop("content_address", None), "runtime closure address")
    environment = _mapping(closure.get("environment"), "runtime environment")
    identities = _mapping(
        closure.get("runtime_release_identities"), "runtime identities"
    )
    paths = _mapping(
        closure.get("identity_document_paths"), "runtime identity paths"
    )
    files = _mapping(
        closure.get("identity_document_file_sha256"), "identity file SHAs"
    )
    source = _mapping(
        closure.get("python_source_identity"), "Python source identity"
    )
    probe = validate_runtime_node_identity_probe_v1(
        _mapping(closure.get("node_identity_probe"), "node probe")
    )
    expected_identity = {
        "cat2new_release_address": CAT2NEW_RELEASE_ADDRESS,
        "cat2new_source_tree_sha256": CAT2NEW_SOURCE_TREE_SHA256,
        "cat2_capability_manifest_sha256": CAT2NEW_CAPABILITY_SHA256,
        "cat2_context_release_address": CAT2_CONTEXT_RELEASE_ADDRESS,
        "cat2_context_identity_sha256": CAT2_CONTEXT_IDENTITY_SHA256,
        "cat2_installed_tree_sha256": CAT2_INSTALLED_TREE_SHA256,
        "cat2_savedvariables_sha256": CAT2_SAVEDVARIABLES_SHA256,
        "contra_release_address": CONTRA_RELEASE_ADDRESS,
        "contra_code_manifest_sha256": contra_manifest_v1.CODE_MANIFEST_SHA256,
        "contra_manifest_sha256": contra_manifest_v1.EXPECTED_MANIFEST_SHA256,
    }
    required_env = {
        "PYTHONPATH",
        "BOC_CAT2NEW_ROOT",
        "BOC_CAT2_CAPABILITY_MANIFEST",
        "BOC_CAT2_INSTALLED_ROOT",
        "BOC_CAT2_SAVEDVARIABLES",
        "BOC_CONTRA260817_ROOT",
        "BOC_CONTRA260817_MANIFEST",
        "GOMAXPROCS",
    }
    activation = _mapping(
        closure.get("activation_identity"), "activation identity"
    )
    shared = _safe_home_relative(closure.get("shared_root"), "shared root")
    expected_source_release = _join_posix(
        shared,
        "releases/fury-multiseed-source",
        str(source.get("source_closure_sha256")),
    )
    cat2new_release = _join_posix(
        shared, "releases/cat2new-runtime", CAT2NEW_RELEASE_ADDRESS
    )
    context_release = _join_posix(
        shared, "releases/cat2-runtime-context", CAT2_CONTEXT_RELEASE_ADDRESS
    )
    contra_release = _join_posix(
        shared, "releases/contra260817-runtime", CONTRA_RELEASE_ADDRESS
    )
    expected_environment = {
        "PYTHONPATH": expected_source_release,
        "BOC_CAT2NEW_ROOT": _join_posix(cat2new_release, "Cat2_new"),
        "BOC_CAT2_CAPABILITY_MANIFEST": _join_posix(
            cat2new_release, CAT2_CAPABILITY_RELATIVE
        ),
        "BOC_CAT2_INSTALLED_ROOT": _join_posix(context_release, "Cat2"),
        "BOC_CAT2_SAVEDVARIABLES": _join_posix(context_release, "Cat2.lua"),
        "BOC_CONTRA260817_ROOT": _join_posix(contra_release, "Contra_new"),
        "BOC_CONTRA260817_MANIFEST": _join_posix(contra_release, "manifest.json"),
        "GOMAXPROCS": "1",
    }
    expected_paths = {
        "cat2new": _join_posix(cat2new_release, "cat2new-runtime-identity.json"),
        "cat2_context": _join_posix(
            shared,
            "derived/cat-gap-runtime-context/v1/cat2-runtime-context-identity.json",
        ),
        "contra260817": _join_posix(
            contra_release, "contra260817-runtime-identity.json"
        ),
    }
    expected_closure_fields = {
        "schema",
        "status",
        "shared_root",
        "python_source_identity",
        "activation_identity",
        "runtime_release_identities",
        "identity_document_paths",
        "identity_document_file_sha256",
        "environment",
        "environment_binding_sha256",
        "node_identity_probe_sha256",
        "node_identity_probe",
        "node_count",
        "ambient_runtime_fallback_allowed",
        "execution_started",
        "heavy_execution_started",
        "simulator_only",
        "scientific_result_available",
        "deployment_allowed",
        "content_address",
    }
    if (
        set(closure) != expected_closure_fields
        or set(source)
        != {"source_closure_sha256", "archive_sha256", "release_relative_path"}
        or set(activation)
        != {
            "content_sha256",
            "bridge_sha256",
            "contract_sha256",
            "payload_sha256",
            "response_sha256",
            "node_count",
        }
        or closure.get("schema") != RUNTIME_CLOSURE_SCHEMA_V1
        or closure.get("status") != RUNTIME_CLOSURE_STATUS_V1
        or address.get("sha256") != sha256_json(core)
        or identities != expected_identity
        or set(environment) != required_env
        or environment != expected_environment
        or any(
            _safe_home_relative(value, field) != value
            for field, value in environment.items()
            if field != "GOMAXPROCS"
        )
        or closure.get("environment_binding_sha256") != sha256_json(environment)
        or files
        != {
            "cat2new": CAT2NEW_IDENTITY_FILE_SHA256,
            "cat2_context": CAT2_CONTEXT_IDENTITY_FILE_SHA256,
            "contra260817": CONTRA_IDENTITY_FILE_SHA256,
        }
        or paths != expected_paths
        or activation.get("bridge_sha256") != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or activation.get("contract_sha256")
        != "f40b8f0ee77c93b8414df4f524081722221313728f409ac43e085de1ddfc4fc9"
        or activation.get("payload_sha256") != EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256
        or activation.get("response_sha256") != EXPECTED_DYNAMIC_V5_RESPONSE_SHA256
        or activation.get("node_count") != len(EXPECTED_NODES)
        or not _SHA256_RE.fullmatch(str(activation.get("content_sha256")))
        or closure.get("node_identity_probe_sha256")
        != probe["content_address"]["sha256"]
        or closure.get("node_count") != len(EXPECTED_NODES)
        or not _SHA256_RE.fullmatch(str(source.get("source_closure_sha256")))
        or not _SHA256_RE.fullmatch(str(source.get("archive_sha256")))
        or source.get("release_relative_path") != expected_source_release
        or closure.get("ambient_runtime_fallback_allowed") is not False
        or any(
            closure.get(field) is not expected
            for field, expected in (
                ("execution_started", False),
                ("heavy_execution_started", False),
                ("simulator_only", True),
                ("scientific_result_available", False),
                ("deployment_allowed", False),
            )
        )
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "Linux runtime closure differs from its exact v11/expert contract"
        )
    return closure


def linux_runtime_environment_lines_v1(value: Mapping[str, Any]) -> list[str]:
    closure = validate_exact_linux_runtime_closure_v1(value)
    lines = []
    for name, relative in closure["environment"].items():
        if name == "GOMAXPROCS":
            lines.append("export GOMAXPROCS=1")
        else:
            lines.append(f"export {name}=\"$HOME/{relative}\"")
    return lines


def linux_runtime_preflight_command_v1(value: Mapping[str, Any]) -> str:
    closure = validate_exact_linux_runtime_closure_v1(value)
    env = closure["environment"]
    paths = closure["identity_document_paths"]

    def home(relative: str) -> str:
        return f'"$HOME/{relative}"'

    bridge = _join_posix(
        closure["shared_root"],
        "releases/dynamic-v5",
        EXPECTED_LINUX_V11_BRIDGE_SHA256,
        "o2obridge.linux-amd64",
    )
    source_marker = _join_posix(env["PYTHONPATH"], ".archive-sha256")
    checks = [
        "set -eu",
        f"test -x {home(bridge)}",
        f"test \"$(sha256sum {home(bridge)} | cut -d ' ' -f1)\" = {EXPECTED_LINUX_V11_BRIDGE_SHA256}",
        f"test -f {home(paths['cat2new'])}",
        f"test \"$(sha256sum {home(paths['cat2new'])} | cut -d ' ' -f1)\" = {CAT2NEW_IDENTITY_FILE_SHA256}",
        f"test -f {home(env['BOC_CAT2_CAPABILITY_MANIFEST'])}",
        f"test \"$(sha256sum {home(env['BOC_CAT2_CAPABILITY_MANIFEST'])} | cut -d ' ' -f1)\" = {CAT2NEW_CAPABILITY_SHA256}",
        f"test -f {home(paths['cat2_context'])}",
        f"test \"$(sha256sum {home(paths['cat2_context'])} | cut -d ' ' -f1)\" = {CAT2_CONTEXT_IDENTITY_FILE_SHA256}",
        f"test -f {home(env['BOC_CAT2_SAVEDVARIABLES'])}",
        f"test \"$(sha256sum {home(env['BOC_CAT2_SAVEDVARIABLES'])} | cut -d ' ' -f1)\" = {CAT2_SAVEDVARIABLES_SHA256}",
        f"test -f {home(paths['contra260817'])}",
        f"test \"$(sha256sum {home(paths['contra260817'])} | cut -d ' ' -f1)\" = {CONTRA_IDENTITY_FILE_SHA256}",
        f"test -f {home(env['BOC_CONTRA260817_MANIFEST'])}",
        f"test \"$(sha256sum {home(env['BOC_CONTRA260817_MANIFEST'])} | cut -d ' ' -f1)\" = {contra_manifest_v1.EXPECTED_MANIFEST_SHA256}",
        f"test \"$(cat {home(source_marker)})\" = {closure['python_source_identity']['archive_sha256']}",
    ]
    return "; ".join(checks)


def _go_source_tree_identity_v1(source_root: Path) -> JSONMap:
    root = source_root.expanduser().resolve()
    if root.name != "wowsims-turtle" or not (root / "go.mod").is_file():
        raise FuryCatGapFormalPreparationV1Error(
            "Windows build source root must be the wowsims-turtle module"
        )
    paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and (path.suffix == ".go" or path.name in {"go.mod", "go.sum"})
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    rows = []
    for path in paths:
        payload = path.read_bytes()
        rows.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "algorithm": "sha256-canonical-all-go-mod-sum-tree-v1",
        "selection": "all **/*.go plus go.mod and go.sum",
        "file_count": len(rows),
        "byte_count": sum(row["size_bytes"] for row in rows),
        "sha256": sha256_json(rows),
    }


def run_windows_v11_equivalence_smoke_v1(
    binary_path: str | Path,
) -> JSONMap:
    """Run the bounded 13-command payload and compare bytes with Linux v11."""

    from .hpc_dynamic_environment_v5 import (
        build_dynamic_probe_payload_v5,
        load_dynamic_v5_contract,
    )

    binary = Path(binary_path).expanduser().resolve()
    if (
        _file_sha256(binary) != EXPECTED_WINDOWS_V11_BRIDGE_SHA256
        or binary.stat().st_size != EXPECTED_WINDOWS_V11_BRIDGE_SIZE
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "Windows equivalence smoke binary is not the immutable v11 build"
        )
    contract = load_dynamic_v5_contract()
    payload, metadata = build_dynamic_probe_payload_v5(contract)
    environment = os.environ.copy()
    environment["GOMAXPROCS"] = "1"
    try:
        result = subprocess.run(
            [str(binary)],
            input=payload,
            capture_output=True,
            timeout=contract.process_timeout_seconds,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"Windows v11 equivalence smoke failed to run: {error}"
        ) from error
    lines = result.stdout.splitlines()
    response_sha = hashlib.sha256(result.stdout).hexdigest()
    evidence = {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size_bytes": len(payload),
        "command_count": metadata["command_count"],
        "response_count": len(lines),
        "response_sha256": response_sha,
        "returncode": result.returncode,
        "stderr_empty": result.stderr == b"",
        "gomaxprocs": 1,
        "process_count": 1,
        "linux_v11_response_byte_equivalent": response_sha
        == EXPECTED_DYNAMIC_V5_RESPONSE_SHA256,
    }
    if (
        evidence["payload_sha256"] != EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256
        or evidence["command_count"] != 13
        or evidence["response_count"] != 13
        or evidence["response_sha256"] != EXPECTED_DYNAMIC_V5_RESPONSE_SHA256
        or evidence["returncode"] != 0
        or evidence["stderr_empty"] is not True
        or evidence["linux_v11_response_byte_equivalent"] is not True
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "Windows v11 response is not byte-equivalent to Linux v11"
        )
    return evidence


def build_windows_v11_build_receipt_v1(
    *,
    source_root: str | Path,
    go_executable: str | Path,
    binary_path: str | Path,
    equivalence_evidence: Mapping[str, Any],
) -> JSONMap:
    source = _go_source_tree_identity_v1(Path(source_root))
    go = Path(go_executable).expanduser().resolve()
    binary = Path(binary_path).expanduser().resolve()
    try:
        version = subprocess.run(
            [str(go), "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"portable Go is unavailable: {error}"
        ) from error
    evidence = deepcopy(dict(_mapping(equivalence_evidence, "equivalence evidence")))
    core = {
        "schema": WINDOWS_BUILD_RECEIPT_SCHEMA_V1,
        "status": WINDOWS_BUILD_RECEIPT_STATUS_V1,
        "source_tree": source,
        "go_tool": {
            "version": version.stdout.strip(),
            "sha256": _file_sha256(go),
            "size_bytes": go.stat().st_size,
        },
        "go_environment": {
            "GOOS": "windows",
            "GOARCH": "amd64",
            "GOAMD64": "v1",
            "CGO_ENABLED": "0",
            "GOFLAGS": "",
            "GOTOOLCHAIN": "local",
            "GOMAXPROCS": "1",
        },
        "test_command": (
            "go test -p=1 --tags=with_db ./sim/core ./sim/o2o ./cmd/o2obridge"
        ),
        "test_status": "PASS",
        "build_command": (
            "go build -p=1 -buildvcs=false -trimpath --tags=with_db "
            "-ldflags=-s -w -buildid= -o <immutable-temp> ./cmd/o2obridge"
        ),
        "binary": {
            "filename": binary.name,
            "sha256": _file_sha256(binary),
            "size_bytes": binary.stat().st_size,
            "platform": "windows-amd64",
        },
        "equivalence_smoke": evidence,
        "legacy_or_prior_binary_overwritten": False,
        "process_count": 1,
        "heavy_execution_started": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    receipt = _address(core)
    return validate_windows_v11_build_receipt_v1(
        receipt, source_root=source_root, binary_path=binary
    )


def validate_windows_v11_build_receipt_v1(
    value: Mapping[str, Any],
    *,
    source_root: str | Path | None = None,
    binary_path: str | Path | None = None,
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "Windows v11 build receipt")))
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "build receipt address")
    source = _mapping(receipt.get("source_tree"), "Go source tree")
    go = _mapping(receipt.get("go_tool"), "Go tool")
    binary = _mapping(receipt.get("binary"), "Windows binary")
    smoke = _mapping(receipt.get("equivalence_smoke"), "equivalence smoke")
    expected_environment = {
        "GOOS": "windows",
        "GOARCH": "amd64",
        "GOAMD64": "v1",
        "CGO_ENABLED": "0",
        "GOFLAGS": "",
        "GOTOOLCHAIN": "local",
        "GOMAXPROCS": "1",
    }
    if (
        receipt.get("schema") != WINDOWS_BUILD_RECEIPT_SCHEMA_V1
        or receipt.get("status") != WINDOWS_BUILD_RECEIPT_STATUS_V1
        or address.get("sha256") != sha256_json(core)
        or source
        != {
            "algorithm": "sha256-canonical-all-go-mod-sum-tree-v1",
            "selection": "all **/*.go plus go.mod and go.sum",
            "file_count": 439,
            "byte_count": 3_755_925,
            "sha256": "94232c51834e55af44c0240fa6a85cc04654f44377932c12687d4df91d7208ea",
        }
        or go
        != {
            "version": "go version go1.23.4 windows/amd64",
            "sha256": "8e8356be1603abff526f52c0d5fe887fb4d77b000f310d298ad10cd33e7471e6",
            "size_bytes": 13_765_632,
        }
        or receipt.get("go_environment") != expected_environment
        or receipt.get("test_status") != "PASS"
        or receipt.get("test_command")
        != "go test -p=1 --tags=with_db ./sim/core ./sim/o2o ./cmd/o2obridge"
        or receipt.get("build_command")
        != (
            "go build -p=1 -buildvcs=false -trimpath --tags=with_db "
            "-ldflags=-s -w -buildid= -o <immutable-temp> ./cmd/o2obridge"
        )
        or binary.get("filename")
        != (
            "o2obridge.seedfix-v11.dynamicv3horizonround.withdb."
            "goamd64v1.windows-amd64.exe"
        )
        or binary.get("sha256") != EXPECTED_WINDOWS_V11_BRIDGE_SHA256
        or binary.get("size_bytes") != EXPECTED_WINDOWS_V11_BRIDGE_SIZE
        or binary.get("platform") != "windows-amd64"
        or smoke
        != {
            "payload_sha256": EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256,
            "payload_size_bytes": 4_376,
            "command_count": 13,
            "response_count": 13,
            "response_sha256": EXPECTED_DYNAMIC_V5_RESPONSE_SHA256,
            "returncode": 0,
            "stderr_empty": True,
            "gomaxprocs": 1,
            "process_count": 1,
            "linux_v11_response_byte_equivalent": True,
        }
        or receipt.get("legacy_or_prior_binary_overwritten") is not False
        or receipt.get("process_count") != 1
        or any(
            receipt.get(field) is not expected
            for field, expected in (
                ("heavy_execution_started", False),
                ("simulator_only", True),
                ("scientific_result_available", False),
                ("deployment_allowed", False),
            )
        )
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "Windows v11 build/equivalence receipt differs"
        )
    if source_root is not None and _go_source_tree_identity_v1(Path(source_root)) != source:
        raise FuryCatGapFormalPreparationV1Error("Go source tree drifted after build")
    if binary_path is not None:
        path = Path(binary_path).expanduser().resolve()
        if (
            path.name != binary["filename"]
            or path.stat().st_size != binary["size_bytes"]
            or _file_sha256(path) != binary["sha256"]
        ):
            raise FuryCatGapFormalPreparationV1Error(
                "Windows v11 binary differs from build receipt"
            )
    return receipt


def _validate_mechanics_ready_plan_v1(value: Mapping[str, Any]) -> JSONMap:
    from .fury_cat_gap_search_plan_v1 import (
        STATUS_MECHANICS_READY,
        validate_cat_gap_search_plan_v1,
    )

    checked = validate_cat_gap_search_plan_v1(value)
    if checked.get("status") != STATUS_MECHANICS_READY:
        raise FuryCatGapFormalPreparationV1Error(
            "heavy preparation requires the mechanics-ready post-smoke plan"
        )
    return checked


def _execution_source_closure_sha256_v1(
    mechanics_ready_plan: Mapping[str, Any],
) -> str:
    """Return the recursively enumerated Python closure admitted by mechanics."""

    executor = _mapping(
        mechanics_ready_plan.get("candidate_executor_contract"),
        "candidate executor",
    )
    admission = _mapping(
        executor.get("execution_surface_admission"),
        "execution surface admission",
    )
    source_binding = _mapping(
        admission.get("execution_surface_source_binding"),
        "execution surface source binding",
    )
    python_closure = _mapping(
        source_binding.get("python_dependency_closure"),
        "Python dependency closure",
    )
    canonical_bundle = _mapping(
        python_closure.get("canonical_bundle"),
        "Python dependency closure canonical bundle",
    )
    return _sha(
        canonical_bundle.get("sha256"),
        "mechanics execution source closure SHA",
    )


def _stage_scenario_bundle_sha256s_v1(
    formal_template: Mapping[str, Any],
) -> JSONMap:
    """Project the exact no-outcome round-robin corpus for every frozen stage."""

    from .fury_cat_gap_hpc_plan_v1 import _round_robin_scenarios

    runner = validate_runner_plan(
        _mapping(formal_template.get("runner_plan"), "formal template runner")
    )
    scenarios = _array(
        _mapping(runner.get("contract"), "formal runner contract").get("scenarios"),
        "formal runner scenarios",
    )
    counts = {
        "successive_halving_1": 42,
        "successive_halving_2": 112,
        "successive_halving_3": EXPECTED_GENERIC_SCENARIOS,
        "selection_validation": EXPECTED_GENERIC_SCENARIOS,
    }
    return {
        stage_id: runner_scenario_bundle_sha256(
            _round_robin_scenarios(scenarios, count)
        )
        for stage_id, count in counts.items()
    }


def build_heavy_preparation_receipt_v1(
    mechanics_ready_plan: Mapping[str, Any],
    *,
    adapter_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
    materialized_manifest_path: str | Path,
    activation_receipt: Mapping[str, Any],
    runtime_closure: Mapping[str, Any],
    windows_build_receipt: Mapping[str, Any],
) -> JSONMap:
    mechanics = _validate_mechanics_ready_plan_v1(mechanics_ready_plan)
    adapter = _validate_adapter_ready_plan(adapter_ready_plan)
    execution_admission = _mapping(
        _mapping(
            mechanics.get("candidate_executor_contract"), "candidate executor"
        ).get("execution_surface_admission"),
        "execution surface admission",
    )
    if execution_admission.get("input_adapter_ready_plan_sha256") != adapter[
        "plan_sha256"
    ]:
        raise FuryCatGapFormalPreparationV1Error(
            "mechanics-ready plan is not descended from the supplied adapter plan"
        )
    template = validate_exact_generic_baseline_template_v1(
        generic_template,
        adapter,
        materialized_manifest_path=materialized_manifest_path,
    )
    activation = _activation_identity_v1(activation_receipt)
    runtime = validate_exact_linux_runtime_closure_v1(runtime_closure)
    windows = validate_windows_v11_build_receipt_v1(windows_build_receipt)
    execution_source_sha = _execution_source_closure_sha256_v1(mechanics)
    if (
        runtime["activation_identity"] != activation
        or template["bridge_identity"].get("sha256")
        != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or template["bridge_identity"].get("platform") != "linux-amd64"
        or runtime["python_source_identity"]["source_closure_sha256"]
        != execution_source_sha
        or template["execution_bundle_identity"]["python_source_closure_sha256"]
        != execution_source_sha
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "formal template, activation, and runtime source closure do not join"
        )
    template_identity = {
        "content_sha256": template["content_address"]["sha256"],
        "runner_plan_sha256": template["runner_plan"]["plan_sha256"],
        "materialized_manifest_content_sha256": template[
            "materialized_manifest_identity"
        ]["content_sha256"],
        "materialized_manifest_file_sha256": template[
            "materialized_manifest_identity"
        ]["file_sha256"],
        "scenario_bundle_sha256": template["scenario_bundle_sha256"],
        "stage_scenario_bundle_sha256s": _stage_scenario_bundle_sha256s_v1(
            template
        ),
        "partition_proof_bundle_sha256": sha256_json(template["partition_proofs"]),
        "generic_instance_count": template["lane_contract"][
            "generic_instance_count"
        ],
        "generic_scenario_count": template["lane_contract"][
            "generic_scenario_count"
        ],
    }
    runtime_identity = {
        "content_sha256": runtime["content_address"]["sha256"],
        "environment_binding_sha256": runtime["environment_binding_sha256"],
        "node_identity_probe_sha256": runtime["node_identity_probe_sha256"],
        "python_source_closure_sha256": runtime["python_source_identity"][
            "source_closure_sha256"
        ],
        "bridge_sha256": runtime["activation_identity"]["bridge_sha256"],
    }
    windows_identity = {
        "content_sha256": windows["content_address"]["sha256"],
        "binary_sha256": windows["binary"]["sha256"],
        "source_tree_sha256": windows["source_tree"]["sha256"],
        "payload_sha256": windows["equivalence_smoke"]["payload_sha256"],
        "response_sha256": windows["equivalence_smoke"]["response_sha256"],
    }
    core = {
        "schema": HEAVY_PREPARATION_SCHEMA_V1,
        "status": HEAVY_PREPARATION_STATUS_V1,
        "mechanics_ready_plan_sha256": mechanics["plan_sha256"],
        "adapter_ready_plan_sha256": adapter["plan_sha256"],
        "execution_source_closure_sha256": execution_source_sha,
        "generic_template_identity": template_identity,
        "activation_identity": activation,
        "runtime_closure_identity": runtime_identity,
        "windows_build_identity": windows_identity,
        "ready_for_capacity_pilot": True,
        "ready_for_sh1": False,
        "heavy_execution_started": False,
        "capacity_pilot_started": False,
        "search_started": False,
        "retention_allowed": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return validate_heavy_preparation_receipt_v1(
        _address(core), mechanics_ready_plan_sha256=mechanics["plan_sha256"]
    )


def validate_heavy_preparation_receipt_v1(
    value: Mapping[str, Any], *, mechanics_ready_plan_sha256: str
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "heavy preparation receipt")))
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "heavy receipt address")
    template = _mapping(
        receipt.get("generic_template_identity"), "generic template identity"
    )
    activation = _mapping(
        receipt.get("activation_identity"), "activation identity"
    )
    runtime = _mapping(
        receipt.get("runtime_closure_identity"), "runtime identity"
    )
    windows = _mapping(
        receipt.get("windows_build_identity"), "Windows build identity"
    )
    exact_manifest_content = (
        "827332833877788e63d42b648a1fdcb3ef393960842346f38f61661638fee77c"
    )
    exact_manifest_file = (
        "1621f6a748addf4855c9a80e6a2cf5d0f0c51c240d9a1be02b667a773ba397ee"
    )
    sha_fields = (
        receipt.get("adapter_ready_plan_sha256"),
        receipt.get("execution_source_closure_sha256"),
        template.get("content_sha256"),
        template.get("runner_plan_sha256"),
        template.get("scenario_bundle_sha256"),
        *tuple(
            _mapping(
                template.get("stage_scenario_bundle_sha256s"),
                "stage scenario bundle SHAs",
            ).values()
        ),
        template.get("partition_proof_bundle_sha256"),
        activation.get("content_sha256"),
        runtime.get("content_sha256"),
        runtime.get("environment_binding_sha256"),
        runtime.get("node_identity_probe_sha256"),
        runtime.get("python_source_closure_sha256"),
        windows.get("content_sha256"),
    )
    expected_receipt_fields = {
        "schema",
        "status",
        "mechanics_ready_plan_sha256",
        "adapter_ready_plan_sha256",
        "execution_source_closure_sha256",
        "generic_template_identity",
        "activation_identity",
        "runtime_closure_identity",
        "windows_build_identity",
        "ready_for_capacity_pilot",
        "ready_for_sh1",
        "heavy_execution_started",
        "capacity_pilot_started",
        "search_started",
        "retention_allowed",
        "simulator_only",
        "scientific_result_available",
        "deployment_allowed",
        "content_address",
    }
    expected_template_fields = {
        "content_sha256",
        "runner_plan_sha256",
        "materialized_manifest_content_sha256",
        "materialized_manifest_file_sha256",
        "scenario_bundle_sha256",
        "stage_scenario_bundle_sha256s",
        "partition_proof_bundle_sha256",
        "generic_instance_count",
        "generic_scenario_count",
    }
    expected_activation_fields = {
        "content_sha256",
        "bridge_sha256",
        "contract_sha256",
        "payload_sha256",
        "response_sha256",
        "node_count",
    }
    expected_runtime_fields = {
        "content_sha256",
        "environment_binding_sha256",
        "node_identity_probe_sha256",
        "python_source_closure_sha256",
        "bridge_sha256",
    }
    expected_windows_fields = {
        "content_sha256",
        "binary_sha256",
        "source_tree_sha256",
        "payload_sha256",
        "response_sha256",
    }
    stage_bundles = _mapping(
        template.get("stage_scenario_bundle_sha256s"),
        "stage scenario bundle SHAs",
    )
    if (
        set(receipt) != expected_receipt_fields
        or set(template) != expected_template_fields
        or set(activation) != expected_activation_fields
        or set(runtime) != expected_runtime_fields
        or set(windows) != expected_windows_fields
        or set(stage_bundles)
        != {
            "successive_halving_1",
            "successive_halving_2",
            "successive_halving_3",
            "selection_validation",
        }
        or stage_bundles.get("successive_halving_3")
        != template.get("scenario_bundle_sha256")
        or stage_bundles.get("selection_validation")
        != template.get("scenario_bundle_sha256")
        or receipt.get("schema") != HEAVY_PREPARATION_SCHEMA_V1
        or receipt.get("status") != HEAVY_PREPARATION_STATUS_V1
        or address.get("sha256") != sha256_json(core)
        or receipt.get("mechanics_ready_plan_sha256")
        != _sha(mechanics_ready_plan_sha256, "mechanics-ready plan SHA")
        or any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in sha_fields)
        or template.get("materialized_manifest_content_sha256")
        != exact_manifest_content
        or template.get("materialized_manifest_file_sha256") != exact_manifest_file
        or template.get("generic_instance_count") != EXPECTED_GENERIC_INSTANCES
        or template.get("generic_scenario_count") != EXPECTED_GENERIC_SCENARIOS
        or activation.get("bridge_sha256") != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or activation.get("contract_sha256")
        != "f40b8f0ee77c93b8414df4f524081722221313728f409ac43e085de1ddfc4fc9"
        or activation.get("payload_sha256") != EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256
        or activation.get("response_sha256") != EXPECTED_DYNAMIC_V5_RESPONSE_SHA256
        or activation.get("node_count") != len(EXPECTED_NODES)
        or runtime.get("bridge_sha256") != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or runtime.get("python_source_closure_sha256")
        != receipt.get("execution_source_closure_sha256")
        or windows.get("binary_sha256") != EXPECTED_WINDOWS_V11_BRIDGE_SHA256
        or windows.get("source_tree_sha256")
        != "94232c51834e55af44c0240fa6a85cc04654f44377932c12687d4df91d7208ea"
        or windows.get("payload_sha256") != EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256
        or windows.get("response_sha256") != EXPECTED_DYNAMIC_V5_RESPONSE_SHA256
        or receipt.get("ready_for_capacity_pilot") is not True
        or receipt.get("ready_for_sh1") is not False
        or any(
            receipt.get(field) is not expected
            for field, expected in (
                ("heavy_execution_started", False),
                ("capacity_pilot_started", False),
                ("search_started", False),
                ("retention_allowed", False),
                ("simulator_only", True),
                ("scientific_result_available", False),
                ("deployment_allowed", False),
            )
        )
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "heavy preparation receipt differs from the formal pre-pilot boundary"
        )
    return receipt


def _scientific_master_seeds_v1(search_plan: Mapping[str, Any]) -> set[int]:
    phases = _mapping(
        _mapping(search_plan.get("seed_contract"), "seed contract").get("phases"),
        "seed phases",
    )
    return {
        int(seed)
        for row in phases.values()
        for seed in _array(_mapping(row, "seed phase").get("master_seeds"), "seeds")
    }


def _pilot_candidate_policy_v1(
    mechanics_ready_plan: Mapping[str, Any],
) -> tuple[str, JSONMap, JSONMap]:
    from .fury_cat_gap_hpc_plan_v1 import _candidate_policy_rows
    from .fury_cat_gap_search_plan_v1 import build_candidate_design_v1

    canonical = build_candidate_design_v1()
    candidate_id = str(canonical[0]["candidate_id"])
    row = deepcopy(_candidate_policy_rows(mechanics_ready_plan, (candidate_id,))[0])
    design = deepcopy(canonical[0])
    return candidate_id, row, design


def build_capacity_pilot_plan_v1(
    heavy_preparation_receipt: Mapping[str, Any],
    *,
    mechanics_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
) -> JSONMap:
    """Prepare 6x160 isolated one-process tasks plus six serial controls."""

    mechanics = _validate_mechanics_ready_plan_v1(mechanics_ready_plan)
    heavy = validate_heavy_preparation_receipt_v1(
        heavy_preparation_receipt,
        mechanics_ready_plan_sha256=mechanics["plan_sha256"],
    )
    wrapper = deepcopy(dict(_mapping(generic_template, "generic template")))
    wrapper_core = {key: value for key, value in wrapper.items() if key != "content_address"}
    if (
        wrapper.get("schema") != TEMPLATE_SCHEMA_V1
        or wrapper.get("status") != TEMPLATE_STATUS_V1
        or _mapping(wrapper.get("content_address"), "template address").get("sha256")
        != sha256_json(wrapper_core)
        or wrapper["content_address"]["sha256"]
        != heavy["generic_template_identity"]["content_sha256"]
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot template differs from heavy preparation"
        )
    template_runner = validate_runner_plan(
        _mapping(wrapper.get("runner_plan"), "template runner")
    )
    scenarios = list(template_runner["contract"]["scenarios"])
    if len(scenarios) != EXPECTED_GENERIC_SCENARIOS:
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot requires all 343 generic scenarios before cost selection"
        )
    selected = max(
        scenarios,
        key=lambda row: (
            int(row["estimated_cost_units"]),
            str(row["instance_id"]),
            str(row["scenario_id"]),
        ),
    )
    candidate_id, candidate_policy, candidate_design = _pilot_candidate_policy_v1(
        mechanics
    )
    policies = (*_canonical_baselines(), candidate_policy)
    concurrent_seeds = derive_seed_set(
        PILOT_SEED_NAMESPACE_V1,
        "concurrent-6x160",
        PILOT_TASK_COUNT_V1,
        counter_start=0,
    )
    control_seeds = derive_seed_set(
        PILOT_SEED_NAMESPACE_V1,
        "serial-controls",
        len(EXPECTED_NODES),
        counter_start=PILOT_TASK_COUNT_V1,
    )
    all_seeds = (*concurrent_seeds, *control_seeds)
    forbidden = _scientific_master_seeds_v1(mechanics) | {17} | set(
        range(513, 769)
    )
    if (
        len(set(all_seeds)) != PILOT_TASK_COUNT_V1 + len(EXPECTED_NODES)
        or forbidden.intersection(all_seeds)
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot seed family overlaps smoke, Horizon, or scientific seeds"
        )
    protocol = {
        "schema": PILOT_PLAN_SCHEMA_V1,
        "heavy_preparation_receipt_sha256": heavy["content_address"]["sha256"],
        "selected_scenario_contract_sha256": selected["scenario_contract_sha256"],
        "candidate_id": candidate_id,
        "seed_namespace": PILOT_SEED_NAMESPACE_V1,
    }
    runner = build_runner_plan(
        protocol_id="fury-cat-gap-throughput-rss-pilot-v1",
        protocol_sha256=sha256_json(protocol),
        phase="capacity_pilot_no_retention",
        corpus_manifest_sha256=wrapper["materialized_manifest_identity"][
            "content_sha256"
        ],
        runner_inputs_sha256=heavy["content_address"]["sha256"],
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([selected]),
        corpus_binding_sha256=wrapper["content_address"]["sha256"],
        master_seeds=all_seeds,
        scenarios=(selected,),
        policies=policies,
        shard_count=len(all_seeds),
        bridge_identity=template_runner["contract"]["bridge_identity"],
        execution_bundle_identity=template_runner["contract"][
            "execution_bundle_identity"
        ],
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=PILOT_SEED_NAMESPACE_V1,
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(
            cat_runner_v4_lane_contract_v6(),
            contra260817_runner_v4_lane_contract_v4(),
            {**cat2new_lane_contract_v3(), "policy_id": candidate_id},
        ),
    )
    runner["generated_at"] = "DETERMINISTIC_CAPACITY_PILOT_V1"
    groups_by_seed = {
        int(row["master_seed"]): row for row in runner["contract"]["groups"]
    }
    shards_by_group = {
        str(group_id): int(row["shard_index"])
        for row in runner["contract"]["shards"]
        for group_id in row["group_ids"]
    }
    concurrent_tasks = []
    for index, master_seed in enumerate(concurrent_seeds):
        node = EXPECTED_NODES[index // PILOT_WORKERS_PER_NODE_V1]
        group = groups_by_seed[int(master_seed)]
        identity = {
            "execution_kind": PILOT_EXECUTION_KIND_V1,
            "role": "CONCURRENT_CAPACITY_TASK",
            "node": node,
            "group_id": group["group_id"],
            "master_seed": master_seed,
        }
        task_id = sha256_json(identity)
        concurrent_tasks.append(
            {
                "task_id": task_id,
                **identity,
                "shard_index": shards_by_group[str(group["group_id"])],
                "output_relative_path": f"tasks/{node}/{task_id}",
            }
        )
    controls = []
    for node, master_seed in zip(EXPECTED_NODES, control_seeds, strict=True):
        group = groups_by_seed[int(master_seed)]
        identity = {
            "execution_kind": PILOT_EXECUTION_KIND_V1,
            "role": "SERIAL_NODE_CONTROL",
            "node": node,
            "group_id": group["group_id"],
            "master_seed": master_seed,
        }
        task_id = sha256_json(identity)
        controls.append(
            {
                "task_id": task_id,
                **identity,
                "shard_index": shards_by_group[str(group["group_id"])],
                "output_relative_path": f"controls/{node}/{task_id}",
            }
        )
    if (
        len({row["task_id"] for row in (*concurrent_tasks, *controls)})
        != len(all_seeds)
        or len({row["output_relative_path"] for row in (*concurrent_tasks, *controls)})
        != len(all_seeds)
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot task/output identities are not unique"
        )
    node_rows = []
    for node in EXPECTED_NODES:
        task_ids = [row["task_id"] for row in concurrent_tasks if row["node"] == node]
        control = [row["task_id"] for row in controls if row["node"] == node]
        if len(task_ids) != PILOT_WORKERS_PER_NODE_V1 or len(control) != 1:
            raise FuryCatGapFormalPreparationV1Error(
                "capacity pilot does not assign exactly 160+1 processes per node"
            )
        node_rows.append(
            {
                "name": node,
                "workers": PILOT_WORKERS_PER_NODE_V1,
                "concurrent_task_ids": task_ids,
                "serial_control_task_id": control[0],
            }
        )
    core = {
        "schema": PILOT_PLAN_SCHEMA_V1,
        "status": PILOT_PLAN_STATUS_V1,
        "execution_kind": PILOT_EXECUTION_KIND_V1,
        "heavy_preparation_receipt_sha256": heavy["content_address"]["sha256"],
        "mechanics_ready_plan_sha256": mechanics["plan_sha256"],
        "formal_template_sha256": wrapper["content_address"]["sha256"],
        "scenario_selection": {
            "rule": "MAX_ESTIMATED_COST_UNITS_THEN_ID_DESC_NO_OUTCOME",
            "scenario_contract_sha256": selected["scenario_contract_sha256"],
            "estimated_cost_units": selected["estimated_cost_units"],
            "generic_corpus_size_considered": EXPECTED_GENERIC_SCENARIOS,
            "outcome_fields_used": False,
        },
        "candidate_design": candidate_design,
        "runner_plan": runner,
        "seed_contract": {
            "namespace": PILOT_SEED_NAMESPACE_V1,
            "concurrent_master_seeds": list(concurrent_seeds),
            "serial_control_master_seeds": list(control_seeds),
            "disjoint_from_all_frozen_scientific_and_smoke_seeds": True,
        },
        "concurrent_tasks": concurrent_tasks,
        "serial_control_tasks": controls,
        "nodes": node_rows,
        "concurrent_task_count": PILOT_TASK_COUNT_V1,
        "serial_control_task_count": len(EXPECTED_NODES),
        "expected_concurrent_lane_count": PILOT_TASK_COUNT_V1
        * PILOT_LANE_COUNT_V1,
        "worker_contract": {
            "module": "o2o_dps.fury_cat_gap_formal_preparation_v1",
            "command": "pilot-task",
            "node_batch_command": "pilot-node-batch",
            "required_cli_inputs": [
                "--pilot-plan",
                "--heavy-preparation-receipt",
                "--mechanics-ready-plan",
                "--formal-template",
                "--runtime-closure",
                "--node",
                "--task-id",
                "--bridge",
                "--bridge-cwd",
                "--output-directory",
            ],
            "one_bridge_process_per_task": True,
            "serial_control_timeout_seconds": PILOT_SERIAL_TIMEOUT_SECONDS_V1,
            "concurrent_batch_timeout_seconds": PILOT_BATCH_TIMEOUT_SECONDS_V1,
            "lanes_executed_sequentially": [
                CAT_POLICY_ID,
                CONTRA260817_POLICY_ID,
                candidate_id,
            ],
            "gomaxprocs": 1,
            "max_rss_kib_semantics": (
                "linux_getrusage_self_ru_maxrss_plus_children_ru_maxrss"
            ),
            "result_or_dps_persistence_allowed": False,
            "only_exit_timing_rss_receipt_allowed": True,
        },
        "admission_gate": {
            "required_concurrent_task_pass_count": PILOT_TASK_COUNT_V1,
            "required_concurrent_lane_validation_count": PILOT_TASK_COUNT_V1
            * PILOT_LANE_COUNT_V1,
            "required_serial_control_pass_count": len(EXPECTED_NODES),
            "minimum_parallel_efficiency_each_node": PILOT_EFFICIENCY_THRESHOLD_V1,
            "rss_extrapolation": "sum_task_max_rss_kib * 192 / 160",
            "maximum_extrapolated_rss_fraction_of_initial_mem_available": (
                PILOT_MEMORY_FRACTION_LIMIT_V1
            ),
            "candidate_retention_allowed": False,
            "scientific_seed_use_allowed": False,
        },
        "capacity_pilot_started": False,
        "search_started": False,
        "retention_allowed": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


def validate_capacity_pilot_plan_v1(
    value: Mapping[str, Any],
    heavy_preparation_receipt: Mapping[str, Any],
    *,
    mechanics_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
) -> JSONMap:
    observed = deepcopy(dict(_mapping(value, "capacity pilot plan")))
    rebuilt = build_capacity_pilot_plan_v1(
        heavy_preparation_receipt,
        mechanics_ready_plan=mechanics_ready_plan,
        generic_template=generic_template,
    )
    if observed != rebuilt:
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot plan differs from exact 6x160 reconstruction"
        )
    return observed


def _validate_pilot_task_receipt_v1(
    value: Mapping[str, Any], task: Mapping[str, Any], pilot_plan_sha256: str
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "capacity pilot task receipt")))
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "task receipt address")
    expected_fields = {
        "schema",
        "status",
        "pilot_plan_sha256",
        "execution_kind",
        "task_id",
        "role",
        "node",
        "group_id",
        "master_seed",
        "shard_index",
        "exit_code",
        "validated_lane_count",
        "wall_seconds",
        "max_rss_kib",
        "gomaxprocs",
        "bridge_process_count",
        "result_fields_persisted",
        "raw_output_retained",
        "retention_allowed",
        "scientific_result_available",
        "deployment_allowed",
    }
    wall = receipt.get("wall_seconds")
    rss = receipt.get("max_rss_kib")
    if (
        set(core) != expected_fields
        or receipt.get("schema") != "fury_cat_gap_capacity_pilot_task_receipt/v1"
        or receipt.get("status") != "PASS_THREE_LANES_VALIDATED_NO_RETENTION"
        or address.get("sha256") != sha256_json(core)
        or receipt.get("pilot_plan_sha256") != pilot_plan_sha256
        or any(receipt.get(field) != task.get(field) for field in (
            "task_id", "role", "node", "group_id", "master_seed", "shard_index"
        ))
        or receipt.get("execution_kind") != PILOT_EXECUTION_KIND_V1
        or receipt.get("exit_code") != 0
        or receipt.get("validated_lane_count") != PILOT_LANE_COUNT_V1
        or isinstance(wall, bool)
        or not isinstance(wall, (int, float))
        or float(wall) <= 0
        or isinstance(rss, bool)
        or not isinstance(rss, int)
        or rss <= 0
        or receipt.get("gomaxprocs") != 1
        or receipt.get("bridge_process_count") != 1
        or receipt.get("result_fields_persisted") != []
        or receipt.get("raw_output_retained") is not False
        or receipt.get("retention_allowed") is not False
        or receipt.get("scientific_result_available") is not False
        or receipt.get("deployment_allowed") is not False
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot task receipt is incomplete or retained results"
        )
    return receipt


def build_capacity_pilot_admission_v1(
    pilot_plan: Mapping[str, Any], *, output_directory: str | Path
) -> JSONMap:
    plan = deepcopy(dict(_mapping(pilot_plan, "capacity pilot plan")))
    plan_core = {key: value for key, value in plan.items() if key != "content_address"}
    plan_sha = _mapping(plan.get("content_address"), "pilot plan address").get("sha256")
    if (
        plan.get("schema") != PILOT_PLAN_SCHEMA_V1
        or plan.get("status") != PILOT_PLAN_STATUS_V1
        or plan_sha != sha256_json(plan_core)
        or plan.get("execution_kind") != PILOT_EXECUTION_KIND_V1
        or plan.get("retention_allowed") is not False
    ):
        raise FuryCatGapFormalPreparationV1Error("capacity pilot plan is not sealed")
    root = Path(output_directory).expanduser().resolve()
    task_receipts: dict[str, JSONMap] = {}
    all_tasks = [*plan["concurrent_tasks"], *plan["serial_control_tasks"]]
    for task in all_tasks:
        path = root / task["output_relative_path"] / "receipt.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FuryCatGapFormalPreparationV1Error(
                f"pilot task receipt unavailable for {task['task_id']}: {error}"
            ) from error
        task_receipts[task["task_id"]] = _validate_pilot_task_receipt_v1(
            _mapping(value, "task receipt"), task, str(plan_sha)
        )
    node_summaries = []
    for node in plan["nodes"]:
        try:
            batch = json.loads(
                (root / "nodes" / node["name"] / "batch.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FuryCatGapFormalPreparationV1Error(
                f"pilot node batch receipt unavailable for {node['name']}: {error}"
            ) from error
        batch_core = deepcopy(dict(_mapping(batch, "node batch receipt")))
        batch_address = _mapping(
            batch_core.pop("content_address", None), "node batch address"
        )
        task_rows = [task_receipts[task_id] for task_id in node["concurrent_task_ids"]]
        control = task_receipts[node["serial_control_task_id"]]
        initial_mem = batch.get("initial_mem_available_kib")
        batch_wall = batch.get("concurrent_batch_wall_seconds")
        expected_batch_fields = {
            "schema",
            "status",
            "pilot_plan_sha256",
            "node",
            "logical_cpus",
            "workers",
            "initial_mem_available_kib",
            "concurrent_batch_wall_seconds",
            "task_ids_sha256",
            "execution_started_after_serial_control",
            "content_address",
        }
        if (
            set(batch) != expected_batch_fields
            or batch.get("schema") != "fury_cat_gap_capacity_pilot_node_batch/v1"
            or batch.get("status") != "PASS_BATCH_COMPLETE"
            or batch_address.get("sha256") != sha256_json(batch_core)
            or batch.get("pilot_plan_sha256") != plan_sha
            or batch.get("node") != node["name"]
            or batch.get("logical_cpus") != 192
            or batch.get("workers") != PILOT_WORKERS_PER_NODE_V1
            or isinstance(initial_mem, bool)
            or not isinstance(initial_mem, int)
            or initial_mem <= 0
            or isinstance(batch_wall, bool)
            or not isinstance(batch_wall, (int, float))
            or float(batch_wall) <= 0
            or batch.get("task_ids_sha256")
            != sha256_json(node["concurrent_task_ids"])
            or batch.get("execution_started_after_serial_control") is not True
        ):
            raise FuryCatGapFormalPreparationV1Error(
                f"pilot node batch receipt differs for {node['name']}"
            )
        efficiency = float(control["wall_seconds"]) / float(batch_wall)
        rss_sum = sum(int(row["max_rss_kib"]) for row in task_rows)
        extrapolated = rss_sum * 192.0 / PILOT_WORKERS_PER_NODE_V1
        rss_fraction = extrapolated / int(initial_mem)
        if (
            efficiency < PILOT_EFFICIENCY_THRESHOLD_V1
            or rss_fraction > PILOT_MEMORY_FRACTION_LIMIT_V1
        ):
            raise FuryCatGapFormalPreparationV1Error(
                f"pilot throughput/RSS admission failed on {node['name']}"
            )
        node_summaries.append(
            {
                "name": node["name"],
                "concurrent_task_pass_count": len(task_rows),
                "validated_lane_count": len(task_rows) * PILOT_LANE_COUNT_V1,
                "serial_control_pass_count": 1,
                "serial_control_wall_seconds": control["wall_seconds"],
                "concurrent_batch_wall_seconds": batch_wall,
                "parallel_efficiency": efficiency,
                "initial_mem_available_kib": initial_mem,
                "sum_task_max_rss_kib": rss_sum,
                "extrapolated_192_process_rss_kib": extrapolated,
                "extrapolated_rss_fraction": rss_fraction,
                "throughput_gate_pass": True,
                "rss_gate_pass": True,
            }
        )
    task_addresses = [
        task_receipts[task["task_id"]]["content_address"]["sha256"]
        for task in all_tasks
    ]
    core = {
        "schema": "fury_cat_gap_throughput_rss_pilot_admission/v1",
        "status": "PASS_960_PROCESS_THROUGHPUT_RSS_NO_RETENTION",
        "pilot_plan_sha256": plan_sha,
        "heavy_preparation_receipt_sha256": plan[
            "heavy_preparation_receipt_sha256"
        ],
        "concurrent_task_pass_count": PILOT_TASK_COUNT_V1,
        "concurrent_lane_validation_count": PILOT_TASK_COUNT_V1
        * PILOT_LANE_COUNT_V1,
        "serial_control_pass_count": len(EXPECTED_NODES),
        "task_receipt_bundle_sha256": sha256_json(task_addresses),
        "nodes": node_summaries,
        "all_nodes_throughput_gate_pass": True,
        "all_nodes_rss_gate_pass": True,
        "result_or_dps_fields_persisted": False,
        "raw_outputs_retained": False,
        "candidate_retention_performed": False,
        "scientific_seed_used": False,
        "search_started": False,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return validate_capacity_pilot_admission_v1(
        _address(core),
        pilot_plan_sha256=str(plan_sha),
        heavy_preparation_receipt_sha256=plan[
            "heavy_preparation_receipt_sha256"
        ],
    )


def validate_capacity_pilot_admission_v1(
    value: Mapping[str, Any],
    *,
    pilot_plan_sha256: str,
    heavy_preparation_receipt_sha256: str,
) -> JSONMap:
    receipt = deepcopy(dict(_mapping(value, "capacity pilot admission")))
    core = deepcopy(receipt)
    address = _mapping(core.pop("content_address", None), "pilot admission address")
    nodes = _array(receipt.get("nodes"), "pilot admission nodes")
    expected_fields = {
        "schema",
        "status",
        "pilot_plan_sha256",
        "heavy_preparation_receipt_sha256",
        "concurrent_task_pass_count",
        "concurrent_lane_validation_count",
        "serial_control_pass_count",
        "task_receipt_bundle_sha256",
        "nodes",
        "all_nodes_throughput_gate_pass",
        "all_nodes_rss_gate_pass",
        "result_or_dps_fields_persisted",
        "raw_outputs_retained",
        "candidate_retention_performed",
        "scientific_seed_used",
        "search_started",
        "simulator_only",
        "scientific_result_available",
        "deployment_allowed",
        "content_address",
    }
    expected_node_fields = {
        "name",
        "concurrent_task_pass_count",
        "validated_lane_count",
        "serial_control_pass_count",
        "serial_control_wall_seconds",
        "concurrent_batch_wall_seconds",
        "parallel_efficiency",
        "initial_mem_available_kib",
        "sum_task_max_rss_kib",
        "extrapolated_192_process_rss_kib",
        "extrapolated_rss_fraction",
        "throughput_gate_pass",
        "rss_gate_pass",
    }

    def valid_node_summary(row: Any) -> bool:
        if not isinstance(row, Mapping) or set(row) != expected_node_fields:
            return False
        control_wall = row.get("serial_control_wall_seconds")
        batch_wall = row.get("concurrent_batch_wall_seconds")
        initial_mem = row.get("initial_mem_available_kib")
        rss_sum = row.get("sum_task_max_rss_kib")
        extrapolated = row.get("extrapolated_192_process_rss_kib")
        efficiency = row.get("parallel_efficiency")
        rss_fraction = row.get("extrapolated_rss_fraction")
        numeric = (control_wall, batch_wall, extrapolated, efficiency, rss_fraction)
        if (
            any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in numeric)
            or any(float(item) <= 0 for item in numeric)
            or isinstance(initial_mem, bool)
            or not isinstance(initial_mem, int)
            or initial_mem <= 0
            or isinstance(rss_sum, bool)
            or not isinstance(rss_sum, int)
            or rss_sum <= 0
        ):
            return False
        recomputed_efficiency = float(control_wall) / float(batch_wall)
        recomputed_extrapolated = (
            int(rss_sum) * 192.0 / PILOT_WORKERS_PER_NODE_V1
        )
        recomputed_fraction = recomputed_extrapolated / int(initial_mem)
        return (
            row.get("concurrent_task_pass_count")
            == PILOT_WORKERS_PER_NODE_V1
            and row.get("validated_lane_count")
            == PILOT_WORKERS_PER_NODE_V1 * PILOT_LANE_COUNT_V1
            and row.get("serial_control_pass_count") == 1
            and float(efficiency) == recomputed_efficiency
            and float(extrapolated) == recomputed_extrapolated
            and float(rss_fraction) == recomputed_fraction
            and recomputed_efficiency >= PILOT_EFFICIENCY_THRESHOLD_V1
            and recomputed_fraction <= PILOT_MEMORY_FRACTION_LIMIT_V1
            and row.get("throughput_gate_pass") is True
            and row.get("rss_gate_pass") is True
        )

    if (
        set(receipt) != expected_fields
        or receipt.get("schema")
        != "fury_cat_gap_throughput_rss_pilot_admission/v1"
        or receipt.get("status")
        != "PASS_960_PROCESS_THROUGHPUT_RSS_NO_RETENTION"
        or address.get("sha256") != sha256_json(core)
        or receipt.get("pilot_plan_sha256")
        != _sha(pilot_plan_sha256, "pilot plan SHA")
        or receipt.get("heavy_preparation_receipt_sha256")
        != _sha(heavy_preparation_receipt_sha256, "heavy receipt SHA")
        or receipt.get("concurrent_task_pass_count") != PILOT_TASK_COUNT_V1
        or receipt.get("concurrent_lane_validation_count")
        != PILOT_TASK_COUNT_V1 * PILOT_LANE_COUNT_V1
        or receipt.get("serial_control_pass_count") != len(EXPECTED_NODES)
        or not _SHA256_RE.fullmatch(str(receipt.get("task_receipt_bundle_sha256")))
        or any(not isinstance(row, Mapping) for row in nodes)
        or [row.get("name") for row in nodes] != list(EXPECTED_NODES)
        or any(not valid_node_summary(row) for row in nodes)
        or receipt.get("all_nodes_throughput_gate_pass") is not True
        or receipt.get("all_nodes_rss_gate_pass") is not True
        or any(
            receipt.get(field) is not expected
            for field, expected in (
                ("result_or_dps_fields_persisted", False),
                ("raw_outputs_retained", False),
                ("candidate_retention_performed", False),
                ("scientific_seed_used", False),
                ("search_started", False),
                ("simulator_only", True),
                ("scientific_result_available", False),
                ("deployment_allowed", False),
            )
        )
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot admission is incomplete, unsafe, or below gate"
        )
    return receipt


def _read_json_document_v1(path: str | Path, label: str) -> JSONMap:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"could not read {label}: {error}"
        ) from error
    return dict(_mapping(value, label))


def _atomic_write_new_json_v1(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise FuryCatGapFormalPreparationV1Error(
            f"pilot output already exists: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value, trailing_lf=True))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _heavy_runtime_projection_v1(runtime: Mapping[str, Any]) -> JSONMap:
    return {
        "content_sha256": runtime["content_address"]["sha256"],
        "environment_binding_sha256": runtime["environment_binding_sha256"],
        "node_identity_probe_sha256": runtime["node_identity_probe_sha256"],
        "python_source_closure_sha256": runtime["python_source_identity"][
            "source_closure_sha256"
        ],
        "bridge_sha256": runtime["activation_identity"]["bridge_sha256"],
    }


def _home_path_v1(relative: str) -> Path:
    return Path.home().joinpath(*PurePosixPath(relative).parts).resolve()


def _validate_live_capacity_pilot_runtime_v1(
    runtime_closure: Mapping[str, Any],
    heavy_preparation_receipt: Mapping[str, Any],
    *,
    node_name: str,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
) -> tuple[JSONMap, Path, Path]:
    """Require the admitted release paths in this exact pilot process."""

    if not sys.platform.startswith("linux"):
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot task is Linux-node only"
        )
    if node_name not in EXPECTED_NODES:
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot node is outside node001--node006"
        )
    hostname = socket.gethostname().split(".", 1)[0]
    if hostname != node_name:
        raise FuryCatGapFormalPreparationV1Error(
            f"capacity pilot task declared {node_name} but runs on {hostname}"
        )
    runtime = validate_exact_linux_runtime_closure_v1(runtime_closure)
    heavy_runtime = _mapping(
        heavy_preparation_receipt.get("runtime_closure_identity"),
        "heavy runtime identity",
    )
    if _heavy_runtime_projection_v1(runtime) != dict(heavy_runtime):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot runtime closure differs from heavy preparation"
        )
    expected_environment = {
        name: (
            "1" if name == "GOMAXPROCS" else str(_home_path_v1(relative))
        )
        for name, relative in runtime["environment"].items()
    }
    if any(
        os.environ.get(name) != expected
        for name, expected in expected_environment.items()
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot process environment differs from the exact runtime closure"
        )
    expected_source_root = _home_path_v1(runtime["environment"]["PYTHONPATH"])
    try:
        Path(__file__).resolve().relative_to(expected_source_root)
    except ValueError as error:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot module was not imported from the admitted Python source release"
        ) from error
    expected_bridge = _home_path_v1(
        _join_posix(
            runtime["shared_root"],
            "releases/dynamic-v5",
            EXPECTED_LINUX_V11_BRIDGE_SHA256,
            "o2obridge.linux-amd64",
        )
    )
    observed_bridge = Path(bridge_path).expanduser().resolve()
    observed_cwd = Path(bridge_cwd).expanduser().resolve()
    if observed_bridge != expected_bridge or observed_cwd != expected_bridge.parent:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot bridge path/cwd differs from the exact v11 release"
        )
    try:
        if (
            observed_bridge.stat().st_size != EXPECTED_LINUX_V11_BRIDGE_SIZE
            or _file_sha256(observed_bridge) != EXPECTED_LINUX_V11_BRIDGE_SHA256
        ):
            raise FuryCatGapFormalPreparationV1Error(
                "pilot bridge bytes differ from admitted Linux v11"
            )
        small_file_checks = {
            runtime["identity_document_paths"]["cat2new"]: (
                CAT2NEW_IDENTITY_FILE_SHA256
            ),
            runtime["identity_document_paths"]["cat2_context"]: (
                CAT2_CONTEXT_IDENTITY_FILE_SHA256
            ),
            runtime["identity_document_paths"]["contra260817"]: (
                CONTRA_IDENTITY_FILE_SHA256
            ),
            runtime["environment"]["BOC_CAT2_CAPABILITY_MANIFEST"]: (
                CAT2NEW_CAPABILITY_SHA256
            ),
            runtime["environment"]["BOC_CAT2_SAVEDVARIABLES"]: (
                CAT2_SAVEDVARIABLES_SHA256
            ),
            runtime["environment"]["BOC_CONTRA260817_MANIFEST"]: (
                contra_manifest_v1.EXPECTED_MANIFEST_SHA256
            ),
        }
        for relative, expected_sha in small_file_checks.items():
            if _file_sha256(_home_path_v1(relative)) != expected_sha:
                raise FuryCatGapFormalPreparationV1Error(
                    f"pilot runtime artifact drifted: {relative}"
                )
        for name in (
            "BOC_CAT2NEW_ROOT",
            "BOC_CAT2_INSTALLED_ROOT",
            "BOC_CONTRA260817_ROOT",
        ):
            if not _home_path_v1(runtime["environment"][name]).is_dir():
                raise FuryCatGapFormalPreparationV1Error(
                    f"pilot runtime directory is unavailable: {name}"
                )
        marker = expected_source_root / ".archive-sha256"
        if marker.read_text(encoding="ascii").strip() != runtime[
            "python_source_identity"
        ]["archive_sha256"]:
            raise FuryCatGapFormalPreparationV1Error(
                "pilot Python source release marker differs"
            )
    except (OSError, UnicodeError) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"pilot runtime artifact is unavailable: {error}"
        ) from error
    return runtime, observed_bridge, observed_cwd


def _capacity_pilot_task_context_v1(
    pilot_plan: Mapping[str, Any], *, task_id: str, node_name: str
) -> tuple[JSONMap, JSONMap, JSONMap, dict[str, JSONMap]]:
    tasks = [
        *pilot_plan["concurrent_tasks"],
        *pilot_plan["serial_control_tasks"],
    ]
    selected_tasks = [row for row in tasks if row.get("task_id") == task_id]
    if len(selected_tasks) != 1:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task ID is not declared exactly once"
        )
    task = deepcopy(dict(selected_tasks[0]))
    if task.get("node") != node_name:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task is not assigned to the declared node"
        )
    runner = validate_runner_plan(
        _mapping(pilot_plan.get("runner_plan"), "pilot runner plan")
    )
    contract = _mapping(runner.get("contract"), "pilot runner contract")
    groups = [
        row
        for row in _array(contract.get("groups"), "pilot groups")
        if row.get("group_id") == task.get("group_id")
    ]
    if len(groups) != 1 or groups[0].get("master_seed") != task.get("master_seed"):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task seed/group is not the exact declared runner group"
        )
    group = deepcopy(dict(groups[0]))
    scenarios = [
        row
        for row in _array(contract.get("scenarios"), "pilot scenarios")
        if row.get("instance_id") == group.get("instance_id")
        and row.get("scenario_id") == group.get("scenario_id")
    ]
    if len(scenarios) != 1:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task scenario is not declared exactly once"
        )
    policies = {
        str(row["policy_id"]): deepcopy(dict(row))
        for row in _array(contract.get("policies"), "pilot policies")
    }
    ordered_policy_ids = pilot_plan["worker_contract"][
        "lanes_executed_sequentially"
    ]
    if (
        len(ordered_policy_ids) != PILOT_LANE_COUNT_V1
        or set(ordered_policy_ids) != set(policies)
        or ordered_policy_ids[:2] != [CAT_POLICY_ID, CONTRA260817_POLICY_ID]
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task does not contain exactly Cat, Contra260817, and candidate"
        )
    return task, group, deepcopy(dict(scenarios[0])), policies


def _execute_capacity_pilot_three_lanes_v1(
    bridge: Any,
    pilot_plan: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policies: Mapping[str, Mapping[str, Any]],
) -> int:
    """Validate three real lane artifacts and return only their count."""

    from .cat2new_fury_paired_lane_adapter_v3 import (
        PRODUCER as CANDIDATE_PRODUCER,
        Cat2NewFuryPairedLaneAdapterV3,
        validate_cat2new_fury_paired_artifact_v3,
    )
    from .cat_fury_paired_lane_adapter_v6 import (
        CAT_V6_PRODUCER,
        execute_cat_runner_v4_lane_v6,
        validate_cat_runner_v4_artifact_v6,
    )
    from .contra260817_fury_paired_lane_adapter_v4 import (
        CONTRA260817_V4_PRODUCER,
        execute_contra260817_runner_v4_lane_v4,
        validate_contra260817_runner_v4_artifact_v4,
    )
    from .fury_cat_gap_hpc_worker_v1 import _validate_candidate_profile
    from .fury_paired_multiseed_runner_v4 import validate_lane_result_v4

    design = _mapping(pilot_plan.get("candidate_design"), "pilot candidate design")
    candidate_id = str(design.get("candidate_id"))
    parameters = FuryCatGapPolicyParametersV1.from_mapping(
        _mapping(design.get("parameters"), "pilot candidate parameters")
    )
    feedback_policy = build_cat_gap_policy_v1(candidate_id, asdict(parameters))
    candidate_policy = _mapping(policies.get(candidate_id), "pilot candidate policy")
    if (
        feedback_policy.policy_id != candidate_id
        or sha256_json(asdict(feedback_policy.config))
        != candidate_policy.get("profile_sha256")
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot candidate implementation differs from its planned profile"
        )
    executors = {
        CAT_POLICY_ID: (
            lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
                bridge, group=group, scenario=scenario, policy=policy
            )
        ),
        CONTRA260817_POLICY_ID: (
            lambda *, group, scenario, policy: execute_contra260817_runner_v4_lane_v4(
                bridge, group=group, scenario=scenario, policy=policy
            )
        ),
        candidate_id: Cat2NewFuryPairedLaneAdapterV3(
            bridge=bridge,
            feedback_policy=feedback_policy,
            optimizer_parameters=asdict(feedback_policy.config),
        ),
    }
    producer_by_policy = {
        CAT_POLICY_ID: CAT_V6_PRODUCER,
        CONTRA260817_POLICY_ID: CONTRA260817_V4_PRODUCER,
        candidate_id: CANDIDATE_PRODUCER,
    }
    validators = {
        CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
        CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
        CANDIDATE_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
    }
    validated_count = 0
    for policy_id in pilot_plan["worker_contract"][
        "lanes_executed_sequentially"
    ]:
        policy = _mapping(policies.get(policy_id), f"pilot policy {policy_id}")
        raw = executors[policy_id](
            group=dict(group), scenario=dict(scenario), policy=dict(policy)
        )
        if not isinstance(raw, Mapping) or set(raw) != {"lane_result"}:
            raise FuryCatGapFormalPreparationV1Error(
                "pilot executor returned data outside lane_result"
            )
        lane = _mapping(raw.get("lane_result"), "pilot lane result")
        producer = producer_by_policy[policy_id]
        if lane.get("producer") != producer:
            raise FuryCatGapFormalPreparationV1Error(
                "pilot lane producer differs from the planned policy"
            )
        validated = validate_lane_result_v4(
            lane,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validators[producer],
        )
        if policy_id == candidate_id:
            _validate_candidate_profile(validated, policy, design)
        validated_count += 1
        del raw, lane, validated
    return validated_count


def _capacity_pilot_max_rss_kib_v1() -> int:
    import resource

    own = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    children = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    total = own + children
    if total <= 0:
        raise FuryCatGapFormalPreparationV1Error(
            "capacity pilot RSS telemetry is unavailable"
        )
    return total


def run_capacity_pilot_task_v1(
    pilot_plan: Mapping[str, Any],
    heavy_preparation_receipt: Mapping[str, Any],
    *,
    mechanics_ready_plan: Mapping[str, Any],
    generic_template: Mapping[str, Any],
    runtime_closure: Mapping[str, Any],
    node_name: str,
    task_id: str,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    output_directory: str | Path,
) -> JSONMap:
    """Run one declared pilot process and persist telemetry, never results."""

    started = time.perf_counter()
    mechanics = _validate_mechanics_ready_plan_v1(mechanics_ready_plan)
    plan = validate_capacity_pilot_plan_v1(
        pilot_plan,
        heavy_preparation_receipt,
        mechanics_ready_plan=mechanics,
        generic_template=generic_template,
    )
    heavy = validate_heavy_preparation_receipt_v1(
        heavy_preparation_receipt,
        mechanics_ready_plan_sha256=mechanics["plan_sha256"],
    )
    _, bridge, cwd = _validate_live_capacity_pilot_runtime_v1(
        runtime_closure,
        heavy,
        node_name=node_name,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
    )
    task, group, scenario, policies = _capacity_pilot_task_context_v1(
        plan, task_id=task_id, node_name=node_name
    )
    from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3

    with SimulatorBridgeDynamicV3(bridge, cwd=cwd) as simulator:
        validated_count = _execute_capacity_pilot_three_lanes_v1(
            simulator,
            plan,
            group=group,
            scenario=scenario,
            policies=policies,
        )
    wall_seconds = time.perf_counter() - started
    core = {
        "schema": "fury_cat_gap_capacity_pilot_task_receipt/v1",
        "status": "PASS_THREE_LANES_VALIDATED_NO_RETENTION",
        "pilot_plan_sha256": plan["content_address"]["sha256"],
        "execution_kind": PILOT_EXECUTION_KIND_V1,
        "task_id": task["task_id"],
        "role": task["role"],
        "node": task["node"],
        "group_id": task["group_id"],
        "master_seed": task["master_seed"],
        "shard_index": task["shard_index"],
        "exit_code": 0,
        "validated_lane_count": validated_count,
        "wall_seconds": wall_seconds,
        "max_rss_kib": _capacity_pilot_max_rss_kib_v1(),
        "gomaxprocs": 1,
        "bridge_process_count": 1,
        "result_fields_persisted": [],
        "raw_output_retained": False,
        "retention_allowed": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    receipt = _validate_pilot_task_receipt_v1(
        _address(core), task, plan["content_address"]["sha256"]
    )
    root = Path(output_directory).expanduser().resolve()
    expected_output = root.joinpath(*PurePosixPath(task["output_relative_path"]).parts)
    if expected_output.exists() and any(expected_output.iterdir()):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot task output directory is not empty"
        )
    _atomic_write_new_json_v1(expected_output / "receipt.json", receipt)
    return receipt


def _linux_mem_available_kib_v1() -> int:
    try:
        rows = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"could not read Linux MemAvailable: {error}"
        ) from error
    for row in rows:
        fields = row.split()
        if len(fields) == 3 and fields[0] == "MemAvailable:" and fields[2] == "kB":
            value = int(fields[1])
            if value > 0:
                return value
    raise FuryCatGapFormalPreparationV1Error(
        "Linux MemAvailable is missing or nonpositive"
    )


def _pilot_task_subprocess_command_v1(
    *,
    pilot_plan_path: Path,
    heavy_preparation_receipt_path: Path,
    mechanics_ready_plan_path: Path,
    formal_template_path: Path,
    runtime_closure_path: Path,
    node_name: str,
    task_id: str,
    bridge_path: Path,
    bridge_cwd: Path,
    output_directory: Path,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "o2o_dps.fury_cat_gap_formal_preparation_v1",
        "pilot-task",
        "--pilot-plan",
        str(pilot_plan_path),
        "--heavy-preparation-receipt",
        str(heavy_preparation_receipt_path),
        "--mechanics-ready-plan",
        str(mechanics_ready_plan_path),
        "--formal-template",
        str(formal_template_path),
        "--runtime-closure",
        str(runtime_closure_path),
        "--node",
        node_name,
        "--task-id",
        task_id,
        "--bridge",
        str(bridge_path),
        "--bridge-cwd",
        str(bridge_cwd),
        "--output-directory",
        str(output_directory),
    ]


def _stop_pilot_processes_v1(processes: Sequence[subprocess.Popen[Any]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    for process in processes:
        if process.poll() is None:
            process.wait()


def run_capacity_pilot_node_batch_v1(
    *,
    pilot_plan_path: str | Path,
    heavy_preparation_receipt_path: str | Path,
    mechanics_ready_plan_path: str | Path,
    formal_template_path: str | Path,
    runtime_closure_path: str | Path,
    node_name: str,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    output_directory: str | Path,
) -> JSONMap:
    """Run one serial control, then exactly 160 node-local pilot processes."""

    paths = {
        "pilot": Path(pilot_plan_path).expanduser().resolve(),
        "heavy": Path(heavy_preparation_receipt_path).expanduser().resolve(),
        "mechanics": Path(mechanics_ready_plan_path).expanduser().resolve(),
        "template": Path(formal_template_path).expanduser().resolve(),
        "runtime": Path(runtime_closure_path).expanduser().resolve(),
        "bridge": Path(bridge_path).expanduser().resolve(),
        "bridge_cwd": Path(bridge_cwd).expanduser().resolve(),
        "output": Path(output_directory).expanduser().resolve(),
    }
    pilot_raw = _read_json_document_v1(paths["pilot"], "pilot plan")
    heavy_raw = _read_json_document_v1(paths["heavy"], "heavy preparation receipt")
    mechanics = _validate_mechanics_ready_plan_v1(
        _read_json_document_v1(paths["mechanics"], "mechanics-ready plan")
    )
    template = _read_json_document_v1(paths["template"], "formal template")
    runtime = _read_json_document_v1(paths["runtime"], "runtime closure")
    plan = validate_capacity_pilot_plan_v1(
        pilot_raw,
        heavy_raw,
        mechanics_ready_plan=mechanics,
        generic_template=template,
    )
    heavy = validate_heavy_preparation_receipt_v1(
        heavy_raw, mechanics_ready_plan_sha256=mechanics["plan_sha256"]
    )
    _validate_live_capacity_pilot_runtime_v1(
        runtime,
        heavy,
        node_name=node_name,
        bridge_path=paths["bridge"],
        bridge_cwd=paths["bridge_cwd"],
    )
    node_rows = [row for row in plan["nodes"] if row.get("name") == node_name]
    if len(node_rows) != 1:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot node batch is not declared exactly once"
        )
    node = node_rows[0]
    if os.cpu_count() != 192 or node.get("workers") != PILOT_WORKERS_PER_NODE_V1:
        raise FuryCatGapFormalPreparationV1Error(
            "pilot node must expose exactly 192 logical CPUs for the 160-worker gate"
        )
    tasks = {
        row["task_id"]: row
        for row in [*plan["concurrent_tasks"], *plan["serial_control_tasks"]]
    }
    control = tasks[node["serial_control_task_id"]]
    common = {
        "pilot_plan_path": paths["pilot"],
        "heavy_preparation_receipt_path": paths["heavy"],
        "mechanics_ready_plan_path": paths["mechanics"],
        "formal_template_path": paths["template"],
        "runtime_closure_path": paths["runtime"],
        "node_name": node_name,
        "bridge_path": paths["bridge"],
        "bridge_cwd": paths["bridge_cwd"],
        "output_directory": paths["output"],
    }
    try:
        control_result = subprocess.run(
            _pilot_task_subprocess_command_v1(
                **common, task_id=control["task_id"]
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PILOT_SERIAL_TIMEOUT_SECONDS_V1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FuryCatGapFormalPreparationV1Error(
            f"pilot serial control failed to finish: {error}"
        ) from error
    if control_result.returncode != 0:
        raise FuryCatGapFormalPreparationV1Error(
            f"pilot serial control exited {control_result.returncode}"
        )
    control_receipt_path = paths["output"].joinpath(
        *PurePosixPath(control["output_relative_path"]).parts,
        "receipt.json",
    )
    control_receipt = _validate_pilot_task_receipt_v1(
        _read_json_document_v1(control_receipt_path, "serial control receipt"),
        control,
        plan["content_address"]["sha256"],
    )
    if control_receipt.get("role") != "SERIAL_NODE_CONTROL":
        raise FuryCatGapFormalPreparationV1Error(
            "pilot serial control receipt has the wrong role"
        )

    initial_mem = _linux_mem_available_kib_v1()
    concurrent_tasks = [tasks[task_id] for task_id in node["concurrent_task_ids"]]
    if (
        len(concurrent_tasks) != PILOT_WORKERS_PER_NODE_V1
        or len({row["task_id"] for row in concurrent_tasks})
        != PILOT_WORKERS_PER_NODE_V1
        or any(row.get("role") != "CONCURRENT_CAPACITY_TASK" for row in concurrent_tasks)
    ):
        raise FuryCatGapFormalPreparationV1Error(
            "pilot node batch does not contain exactly 160 unique concurrent tasks"
        )
    processes: list[subprocess.Popen[Any]] = []
    batch_started = time.perf_counter()
    try:
        for task in concurrent_tasks:
            processes.append(
                subprocess.Popen(
                    _pilot_task_subprocess_command_v1(
                        **common, task_id=task["task_id"]
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = batch_started + PILOT_BATCH_TIMEOUT_SECONDS_V1
        for process in processes:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("pilot-node-batch", 0)
            process.wait(timeout=remaining)
        failures = [process.returncode for process in processes if process.returncode != 0]
        if failures:
            raise FuryCatGapFormalPreparationV1Error(
                f"pilot concurrent batch has {len(failures)} failed tasks"
            )
    except (OSError, subprocess.TimeoutExpired, FuryCatGapFormalPreparationV1Error):
        _stop_pilot_processes_v1(processes)
        raise
    batch_wall = time.perf_counter() - batch_started
    for task in concurrent_tasks:
        receipt_path = paths["output"].joinpath(
            *PurePosixPath(task["output_relative_path"]).parts,
            "receipt.json",
        )
        _validate_pilot_task_receipt_v1(
            _read_json_document_v1(receipt_path, "concurrent task receipt"),
            task,
            plan["content_address"]["sha256"],
        )
    core = {
        "schema": "fury_cat_gap_capacity_pilot_node_batch/v1",
        "status": "PASS_BATCH_COMPLETE",
        "pilot_plan_sha256": plan["content_address"]["sha256"],
        "node": node_name,
        "logical_cpus": 192,
        "workers": PILOT_WORKERS_PER_NODE_V1,
        "initial_mem_available_kib": initial_mem,
        "concurrent_batch_wall_seconds": batch_wall,
        "task_ids_sha256": sha256_json(node["concurrent_task_ids"]),
        "execution_started_after_serial_control": True,
    }
    receipt = _address(core)
    output_path = paths["output"] / "nodes" / node_name / "batch.json"
    _atomic_write_new_json_v1(output_path, receipt)
    return receipt


def _capacity_pilot_parser_v1() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    task = commands.add_parser(
        "pilot-task", help="run one declared no-retention capacity task"
    )
    task.add_argument("--pilot-plan", type=Path, required=True)
    task.add_argument("--heavy-preparation-receipt", type=Path, required=True)
    task.add_argument("--mechanics-ready-plan", type=Path, required=True)
    task.add_argument("--formal-template", type=Path, required=True)
    task.add_argument("--runtime-closure", type=Path, required=True)
    task.add_argument("--node", required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--bridge", type=Path, required=True)
    task.add_argument("--bridge-cwd", type=Path, required=True)
    task.add_argument("--output-directory", type=Path, required=True)
    batch = commands.add_parser(
        "pilot-node-batch",
        help="run one serial control then exactly 160 concurrent task processes",
    )
    batch.add_argument("--pilot-plan", type=Path, required=True)
    batch.add_argument("--heavy-preparation-receipt", type=Path, required=True)
    batch.add_argument("--mechanics-ready-plan", type=Path, required=True)
    batch.add_argument("--formal-template", type=Path, required=True)
    batch.add_argument("--runtime-closure", type=Path, required=True)
    batch.add_argument("--node", required=True)
    batch.add_argument("--bridge", type=Path, required=True)
    batch.add_argument("--bridge-cwd", type=Path, required=True)
    batch.add_argument("--output-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _capacity_pilot_parser_v1().parse_args(argv)
    try:
        if args.command == "pilot-task":
            receipt = run_capacity_pilot_task_v1(
                _read_json_document_v1(args.pilot_plan, "pilot plan"),
                _read_json_document_v1(
                    args.heavy_preparation_receipt, "heavy preparation receipt"
                ),
                mechanics_ready_plan=_read_json_document_v1(
                    args.mechanics_ready_plan, "mechanics-ready plan"
                ),
                generic_template=_read_json_document_v1(
                    args.formal_template, "formal template"
                ),
                runtime_closure=_read_json_document_v1(
                    args.runtime_closure, "runtime closure"
                ),
                node_name=args.node,
                task_id=args.task_id,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                output_directory=args.output_directory,
            )
        elif args.command == "pilot-node-batch":
            receipt = run_capacity_pilot_node_batch_v1(
                pilot_plan_path=args.pilot_plan,
                heavy_preparation_receipt_path=args.heavy_preparation_receipt,
                mechanics_ready_plan_path=args.mechanics_ready_plan,
                formal_template_path=args.formal_template,
                runtime_closure_path=args.runtime_closure,
                node_name=args.node,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                output_directory=args.output_directory,
            )
        else:  # pragma: no cover - argparse owns choices
            raise FuryCatGapFormalPreparationV1Error("unknown preparation command")
        print(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            flush=True,
        )
        return 0
    except (
        FuryCatGapFormalPreparationV1Error,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "FuryCatGapFormalPreparationV1Error",
    "HEAVY_PREPARATION_SCHEMA_V1",
    "HEAVY_PREPARATION_STATUS_V1",
    "PILOT_EXECUTION_KIND_V1",
    "PILOT_PLAN_SCHEMA_V1",
    "PILOT_PLAN_STATUS_V1",
    "RUNTIME_CLOSURE_SCHEMA_V1",
    "RUNTIME_CLOSURE_STATUS_V1",
    "TEMPLATE_SCHEMA_V1",
    "TEMPLATE_STATUS_V1",
    "WINDOWS_BUILD_RECEIPT_SCHEMA_V1",
    "WINDOWS_BUILD_RECEIPT_STATUS_V1",
    "build_capacity_pilot_admission_v1",
    "build_capacity_pilot_plan_v1",
    "build_exact_generic_baseline_template_v1",
    "build_exact_linux_runtime_closure_v1",
    "build_heavy_preparation_receipt_v1",
    "build_single_scenario_smoke_template_v1",
    "build_windows_v11_build_receipt_v1",
    "linux_runtime_environment_lines_v1",
    "linux_runtime_preflight_command_v1",
    "run_capacity_pilot_task_v1",
    "run_capacity_pilot_node_batch_v1",
    "run_windows_v11_equivalence_smoke_v1",
    "validate_capacity_pilot_admission_v1",
    "validate_capacity_pilot_plan_v1",
    "validate_exact_generic_baseline_template_v1",
    "validate_exact_linux_runtime_closure_v1",
    "validate_heavy_preparation_receipt_v1",
    "validate_runtime_node_identity_probe_v1",
    "validate_v11_activation_receipt_v1",
    "validate_windows_v11_build_receipt_v1",
)
