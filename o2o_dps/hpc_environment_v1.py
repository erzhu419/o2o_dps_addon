"""Fail-closed Windows/WSL environment staging for the O2O-DPS CPU pool.

This module configures *execution infrastructure*, not an experiment.  The
default command is a read-only probe.  Mutating bootstrap/staging operations
require ``--apply`` and are restricted to user-owned directories.  Raw or
normalized Chronicle data is never an admissible transfer artifact.

Two transports are deliberately separated:

* the control plane is reached from WSL with an existing OpenSSH alias; and
* CPU nodes are preferably reached through the existing ``scheduleurm``
  ``scheduler.run_on`` transport.  The scheduler owns host, user, key and
  proxy details, so this repository never copies credentials or private site
  topology into source control.

The CPU-side simulator bridge is an x86-64, no-PT_INTERP ELF artifact.  It is
staged once into a content-addressed shared release, SHA-256 verified from
every requested node, and activated through an atomically replaced symlink.
No node Python is required to execute the bridge; an optional site Python is
only inventoried for later orchestration.
"""

from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import struct
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid


JSONMap = dict[str, Any]
SITE_SCHEMA = "o2o_hpc_site/v1"
RECEIPT_SCHEMA = "o2o_hpc_environment_receipt/v1"
RELEASE_SCHEMA = "o2o_hpc_transfer_release/v1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_CONFIG = PROJECT_ROOT / "configs" / "hpc" / "site.local.json"
DEFAULT_RECEIPT_DIRECTORY = PROJECT_ROOT / ".hpc-local" / "receipts"
DEFAULT_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64"
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_WSL_DISTRO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_RELATIVE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BRIDGE_NAME = re.compile(
    r"^o2obridge\.seedfix-v[1-9][0-9]*\.withdb\.goamd64v1\.linux-amd64$"
)
_CAPSULE_NAME = re.compile(
    r"^fury_offline_scenario_capsules_v2\.(?P<content_sha256>[0-9a-f]{64})\.json\.gz$"
)
_BINDING_NAME = re.compile(
    r"^fury_capsule_static_execution_binding_v2\.(?P<content_sha256>[0-9a-f]{64})\.json\.gz$"
)

_SITE_FIELDS = frozenset(
    {
        "schema",
        "wsl_distribution",
        "control_plane",
        "node_transport",
        "nodes",
        "node_shared_root",
        "node_python",
        "required_arch",
        "minimum_control_python",
        "pilot_workers_per_node",
        "maximum_workers_per_node_before_benchmark",
    }
)
_CONTROL_FIELDS = frozenset({"ssh_alias", "remote_root"})
_TRANSPORT_FIELDS = frozenset({"kind", "scheduler_skill_dir"})
_NODE_FIELDS = frozenset({"name", "transport_name"})

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_RELEASE_BYTES = 1024 * 1024 * 1024
_MAX_EXPANDED_JSON_BYTES = 128 * 1024 * 1024


class HpcEnvironmentError(RuntimeError):
    """A site, transport, artifact or staging invariant failed."""


@dataclass(frozen=True)
class SiteNode:
    name: str
    transport_name: str


@dataclass(frozen=True)
class HpcSite:
    wsl_distribution: str
    control_ssh_alias: str
    control_remote_root: str
    node_transport_kind: str
    scheduler_skill_dir: str
    nodes: tuple[SiteNode, ...]
    node_shared_root: str
    node_python: str
    required_arch: str
    minimum_control_python: tuple[int, int]
    pilot_workers_per_node: int
    maximum_workers_per_node_before_benchmark: int


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ArtifactIdentity:
    role: str
    path: Path
    transfer_name: str
    size_bytes: int
    sha256: str


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HpcEnvironmentError(f"value is not canonical JSON: {exc}") from exc


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise HpcEnvironmentError(
            f"{label} fields do not match schema; missing={missing}, extra={extra}"
        )


def _safe_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise HpcEnvironmentError(f"{label} must be a credential-free SSH/node alias")
    return value


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HpcEnvironmentError(f"{label} must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("~"):
        raise HpcEnvironmentError(f"{label} must be relative to the remote home")
    if any(
        segment in {"", ".", ".."} or not _SAFE_RELATIVE_SEGMENT.fullmatch(segment)
        for segment in path.parts
    ):
        raise HpcEnvironmentError(f"{label} contains an unsafe path segment")
    return path.as_posix()


def _safe_scheduler_skill_dir(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise HpcEnvironmentError("scheduler_skill_dir must be nonempty")
    candidate = value[2:] if value.startswith("~/") else value
    path = PurePosixPath(candidate)
    if not value.startswith("~/") and not path.is_absolute():
        raise HpcEnvironmentError(
            "scheduler_skill_dir must be an absolute path or ~/ relative path"
        )
    segments = path.parts[1:] if path.is_absolute() else path.parts
    if any(segment in {"", ".", ".."} for segment in segments):
        raise HpcEnvironmentError("scheduler_skill_dir must not traverse directories")
    if not all(
        _SAFE_RELATIVE_SEGMENT.fullmatch(segment) for segment in segments
    ):
        raise HpcEnvironmentError("scheduler_skill_dir contains unsafe characters")
    return value


def load_site_config(path: Path) -> HpcSite:
    resolved = path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HpcEnvironmentError(f"could not load site config {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise HpcEnvironmentError("site config must be a JSON object")
    _exact_fields(value, _SITE_FIELDS, "site config")
    if value["schema"] != SITE_SCHEMA:
        raise HpcEnvironmentError(f"site config schema must be {SITE_SCHEMA}")

    distro = value["wsl_distribution"]
    if not isinstance(distro, str) or not _SAFE_WSL_DISTRO.fullmatch(distro):
        raise HpcEnvironmentError("wsl_distribution is invalid")

    control = value["control_plane"]
    if not isinstance(control, dict):
        raise HpcEnvironmentError("control_plane must be an object")
    _exact_fields(control, _CONTROL_FIELDS, "control_plane")

    transport = value["node_transport"]
    if not isinstance(transport, dict):
        raise HpcEnvironmentError("node_transport must be an object")
    _exact_fields(transport, _TRANSPORT_FIELDS, "node_transport")
    kind = transport["kind"]
    if kind not in {"scheduler_run_on", "nested_ssh"}:
        raise HpcEnvironmentError(
            "node_transport.kind must be scheduler_run_on or nested_ssh"
        )
    scheduler_skill_dir = _safe_scheduler_skill_dir(
        transport["scheduler_skill_dir"]
    )

    rows = value["nodes"]
    if not isinstance(rows, list) or not rows:
        raise HpcEnvironmentError("nodes must be a nonempty array")
    nodes: list[SiteNode] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise HpcEnvironmentError(f"nodes[{index}] must be an object")
        _exact_fields(row, _NODE_FIELDS, f"nodes[{index}]")
        name = _safe_name(row["name"], f"nodes[{index}].name")
        target = _safe_name(
            row["transport_name"], f"nodes[{index}].transport_name"
        )
        if name in seen:
            raise HpcEnvironmentError(f"duplicate node name: {name}")
        seen.add(name)
        nodes.append(SiteNode(name=name, transport_name=target))

    minimum = value["minimum_control_python"]
    if (
        not isinstance(minimum, list)
        or len(minimum) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in minimum)
        or minimum[0] < 3
        or minimum[1] < 0
    ):
        raise HpcEnvironmentError("minimum_control_python must be [major, minor]")

    required_arch = value["required_arch"]
    if required_arch != "x86_64":
        raise HpcEnvironmentError("only the x86_64 bridge is currently admitted")

    pilot = value["pilot_workers_per_node"]
    maximum = value["maximum_workers_per_node_before_benchmark"]
    if (
        isinstance(pilot, bool)
        or not isinstance(pilot, int)
        or pilot <= 0
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < pilot
    ):
        raise HpcEnvironmentError("worker limits are invalid")

    return HpcSite(
        wsl_distribution=distro,
        control_ssh_alias=_safe_name(control["ssh_alias"], "control ssh_alias"),
        control_remote_root=_safe_relative_path(
            control["remote_root"], "control remote_root"
        ),
        node_transport_kind=kind,
        scheduler_skill_dir=scheduler_skill_dir,
        nodes=tuple(nodes),
        node_shared_root=_safe_relative_path(
            value["node_shared_root"], "node_shared_root"
        ),
        node_python=_safe_relative_path(value["node_python"], "node_python"),
        required_arch=required_arch,
        minimum_control_python=(minimum[0], minimum[1]),
        pilot_workers_per_node=pilot,
        maximum_workers_per_node_before_benchmark=maximum,
    )


def apply_node_overrides(site: HpcSite, overrides: Sequence[str]) -> HpcSite:
    if not overrides:
        return site
    replacements: dict[str, str] = {}
    for raw in overrides:
        if raw.count("=") != 1:
            raise HpcEnvironmentError("--node must use NAME=TRANSPORT_NAME")
        name_raw, target_raw = raw.split("=", 1)
        name = _safe_name(name_raw, "node override name")
        target = _safe_name(target_raw, "node override transport name")
        if name in replacements:
            raise HpcEnvironmentError(f"duplicate --node override: {name}")
        replacements[name] = target
    known = {node.name for node in site.nodes}
    unknown = sorted(set(replacements) - known)
    if unknown:
        raise HpcEnvironmentError(f"node overrides are not present in site config: {unknown}")
    return replace(
        site,
        nodes=tuple(
            replace(node, transport_name=replacements.get(node.name, node.transport_name))
            for node in site.nodes
        ),
    )


def _sha256_file(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    after = path.stat()
    before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_id != after_id or size != after.st_size:
        raise HpcEnvironmentError(f"artifact changed while hashing: {path}")
    return size, digest.hexdigest()


def inspect_bridge(path: Path) -> JSONMap:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise HpcEnvironmentError("symlink bridge artifacts are forbidden")
    resolved = candidate.resolve()
    try:
        with resolved.open("rb") as handle:
            header = handle.read(4096)
        size, digest = _sha256_file(resolved)
    except OSError as exc:
        raise HpcEnvironmentError(f"could not read bridge {resolved}: {exc}") from exc
    if len(header) < 64 or header[:4] != b"\x7fELF":
        raise HpcEnvironmentError("bridge is not an ELF executable")
    if header[4] != 2 or header[5] != 1:
        raise HpcEnvironmentError("bridge must be little-endian ELF64")
    machine = struct.unpack_from("<H", header, 18)[0]
    if machine != 62:
        raise HpcEnvironmentError("bridge ELF machine must be x86-64")
    phoff = struct.unpack_from("<Q", header, 32)[0]
    phentsize = struct.unpack_from("<H", header, 54)[0]
    phnum = struct.unpack_from("<H", header, 56)[0]
    table_bytes = phentsize * phnum
    required = phoff + phentsize * phnum
    if (
        phoff < 64
        or phentsize < 56
        or phnum <= 0
        or table_bytes > 16 * 1024 * 1024
        or required > size
    ):
        raise HpcEnvironmentError("bridge has an invalid ELF program-header table")
    if required > len(header):
        try:
            with resolved.open("rb") as handle:
                handle.seek(phoff)
                program_headers = handle.read(phentsize * phnum)
        except OSError as exc:
            raise HpcEnvironmentError(f"could not inspect bridge headers: {exc}") from exc
    else:
        program_headers = header[phoff:required]
    has_interp = any(
        struct.unpack_from("<I", program_headers, index * phentsize)[0] == 3
        for index in range(phnum)
        if (index + 1) * phentsize <= len(program_headers)
    )
    if has_interp:
        raise HpcEnvironmentError("bridge has PT_INTERP and is not admitted as static")
    return {
        "path": str(resolved),
        "size_bytes": size,
        "sha256": digest,
        "elf_class": "ELF64",
        "architecture": "x86_64",
        "pt_interp": False,
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_json_fields(pairs: Sequence[tuple[str, Any]]) -> JSONMap:
    result: JSONMap = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _load_bounded_json_artifact(path: Path) -> JSONMap:
    """Load one transfer document without permitting zip bombs or loose JSON."""

    try:
        opener = gzip.open if path.suffix.casefold() == ".gz" else Path.open
        if opener is gzip.open:
            with gzip.open(path, mode="rb") as handle:
                payload = handle.read(_MAX_EXPANDED_JSON_BYTES + 1)
        else:
            with path.open(mode="rb") as handle:
                payload = handle.read(_MAX_EXPANDED_JSON_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise HpcEnvironmentError(f"could not read compact JSON artifact {path}: {exc}") from exc
    if len(payload) > _MAX_EXPANDED_JSON_BYTES:
        raise HpcEnvironmentError(f"expanded JSON artifact exceeds safety bound: {path}")
    try:
        value = json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HpcEnvironmentError(f"transfer artifact is not strict JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HpcEnvironmentError(f"transfer artifact must contain one JSON object: {path}")
    return value


def _filename_content_sha256(path: Path, pattern: re.Pattern[str]) -> str:
    match = pattern.fullmatch(path.name)
    if match is None:
        raise HpcEnvironmentError(f"content-addressed artifact filename is invalid: {path.name}")
    return match.group("content_sha256")


def _validate_artifact_document(
    *, role: str, path: Path, size_bytes: int, byte_sha256: str
) -> JSONMap | None:
    """Validate role semantics before any artifact can enter a release."""

    if role == "static_bridge":
        inspected = inspect_bridge(path)
        if (
            inspected.get("sha256") != byte_sha256
            or inspected.get("size_bytes") != size_bytes
        ):
            raise HpcEnvironmentError("bridge identity changed during validation")
        return None

    document = _load_bounded_json_artifact(path)
    try:
        if role == "protocol":
            from .fury_multiseed_evaluation_v2 import materialize_protocol

            materialize_protocol(document)
        elif role == "compact_capsule":
            from .fury_offline_scenario_capsule_v2 import (
                validate_scenario_capsule_bundle_v2,
            )

            validate_scenario_capsule_bundle_v2(document)
            declared = document.get("content_address", {}).get("sha256")
            filename_sha = _filename_content_sha256(path, _CAPSULE_NAME)
            if declared != filename_sha:
                raise HpcEnvironmentError(
                    "compact capsule filename/content-address SHA-256 mismatch"
                )
        elif role == "compact_binding":
            from .fury_capsule_execution_binding_v2 import (
                validate_capsule_static_execution_binding_v2,
            )

            validate_capsule_static_execution_binding_v2(document)
            declared = document.get("content_address", {}).get("sha256")
            filename_sha = _filename_content_sha256(path, _BINDING_NAME)
            if declared != filename_sha:
                raise HpcEnvironmentError(
                    "compact binding filename/content-address SHA-256 mismatch"
                )
        else:  # pragma: no cover - caller owns the closed role enum
            raise HpcEnvironmentError(f"unsupported transfer role: {role}")
    except HpcEnvironmentError:
        raise
    except Exception as exc:
        raise HpcEnvironmentError(
            f"{role} schema/content validation failed: {type(exc).__name__}: {exc}"
        ) from exc
    return document


def _classify_transfer_artifact_identity(path: Path) -> ArtifactIdentity:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise HpcEnvironmentError("symlink transfer artifacts are forbidden")
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError as exc:
        raise HpcEnvironmentError("transfer artifact must be inside this repository") from exc
    if relative == "configs/evaluation/fury_multiseed_protocol_v2.json":
        role, transfer_name = "protocol", "protocol.json"
    elif relative.startswith("bin/") and _BRIDGE_NAME.fullmatch(resolved.name):
        role, transfer_name = "static_bridge", "bridge.linux-amd64"
    elif (
        relative.startswith("offline_data/derived/fury_offline_scenario_capsules/v2/")
        and _CAPSULE_NAME.fullmatch(resolved.name)
    ):
        role, transfer_name = "compact_capsule", "capsule.json.gz"
    elif (
        relative.startswith("offline_data/derived/fury_capsule_execution_bindings/v2/")
        and _BINDING_NAME.fullmatch(resolved.name)
    ):
        role, transfer_name = "compact_binding", "binding.json.gz"
    else:
        raise HpcEnvironmentError(
            "artifact is outside the four-role HPC transfer allowlist; raw and "
            "normalized Chronicle files are forbidden"
        )
    try:
        size, digest = _sha256_file(resolved)
    except OSError as exc:
        raise HpcEnvironmentError(f"could not hash artifact {resolved}: {exc}") from exc
    if size <= 0 or size > _MAX_ARTIFACT_BYTES:
        raise HpcEnvironmentError(
            f"artifact size is outside the compact-transfer bound: {relative}: {size}"
        )
    return ArtifactIdentity(
        role=role,
        path=resolved,
        transfer_name=transfer_name,
        size_bytes=size,
        sha256=digest,
    )


def classify_transfer_artifact(path: Path) -> ArtifactIdentity:
    identity = _classify_transfer_artifact_identity(path)
    _validate_artifact_document(
        role=identity.role,
        path=identity.path,
        size_bytes=identity.size_bytes,
        byte_sha256=identity.sha256,
    )
    return identity


def _mapping_field(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HpcEnvironmentError(f"{label} must be an object")
    return value


def _validate_transfer_release_closure(
    identities: Mapping[str, ArtifactIdentity],
    *,
    expected_bridge: Mapping[str, Any] | None,
) -> None:
    protocol = _validate_artifact_document(
        role="protocol",
        path=identities["protocol"].path,
        size_bytes=identities["protocol"].size_bytes,
        byte_sha256=identities["protocol"].sha256,
    )
    capsule = _validate_artifact_document(
        role="compact_capsule",
        path=identities["compact_capsule"].path,
        size_bytes=identities["compact_capsule"].size_bytes,
        byte_sha256=identities["compact_capsule"].sha256,
    )
    binding = _validate_artifact_document(
        role="compact_binding",
        path=identities["compact_binding"].path,
        size_bytes=identities["compact_binding"].size_bytes,
        byte_sha256=identities["compact_binding"].sha256,
    )
    if protocol is None or capsule is None or binding is None:  # pragma: no cover
        raise HpcEnvironmentError("release document validation returned no document")

    try:
        phase_bindings = _mapping_field(
            _mapping_field(protocol["corpus_contract"], "protocol.corpus_contract")[
                "phase_corpus_bindings"
            ],
            "protocol phase_corpus_bindings",
        )
        development = _mapping_field(
            phase_bindings["development"], "protocol development corpus binding"
        )
        capsule_pin = _mapping_field(
            development["scenario_model_capsule"], "protocol scenario capsule pin"
        )
        binding_pin = _mapping_field(
            capsule_pin["static_control_binding"], "protocol static binding pin"
        )
        scheduler_pin = _mapping_field(
            _mapping_field(
                protocol["execution_contract"], "protocol.execution_contract"
            )["scheduler_hpc"],
            "protocol scheduler_hpc",
        )
    except KeyError as exc:
        raise HpcEnvironmentError(
            f"protocol is missing required HPC release closure field: {exc.args[0]}"
        ) from exc

    capsule_identity = identities["compact_capsule"]
    binding_identity = identities["compact_binding"]
    bridge_identity = identities["static_bridge"]
    capsule_content_sha = capsule.get("content_address", {}).get("sha256")
    binding_content_sha = binding.get("content_address", {}).get("sha256")
    expected_capsule = {
        "schema": capsule.get("schema"),
        "canonical_content_sha256": capsule_content_sha,
        "gzip_file_sha256": capsule_identity.sha256,
        "size_bytes": capsule_identity.size_bytes,
    }
    expected_binding = {
        "schema": binding.get("schema"),
        "canonical_content_sha256": binding_content_sha,
        "gzip_file_sha256": binding_identity.sha256,
        "size_bytes": binding_identity.size_bytes,
    }
    for field, expected in expected_capsule.items():
        if capsule_pin.get(field) != expected:
            raise HpcEnvironmentError(
                f"protocol/capsule release closure mismatch for {field}"
            )
    for field, expected in expected_binding.items():
        if binding_pin.get(field) != expected:
            raise HpcEnvironmentError(
                f"protocol/binding release closure mismatch for {field}"
            )
    capsule_filename_sha = _filename_content_sha256(
        capsule_identity.path, _CAPSULE_NAME
    )
    binding_filename_sha = _filename_content_sha256(
        binding_identity.path, _BINDING_NAME
    )
    if capsule_filename_sha != capsule_content_sha:
        raise HpcEnvironmentError("capsule filename does not name its validated content")
    if binding_filename_sha != binding_content_sha:
        raise HpcEnvironmentError("binding filename does not name its validated content")
    capsule_reference = _mapping_field(
        binding.get("capsule_reference"), "binding.capsule_reference"
    )
    if capsule_reference.get("content_sha256") != capsule_content_sha:
        raise HpcEnvironmentError("binding does not reference the transferred capsule")
    if scheduler_pin.get("linux_bridge_sha256") != bridge_identity.sha256:
        raise HpcEnvironmentError("protocol does not pin the transferred Linux bridge")
    expected_bridge_name = str(scheduler_pin.get("linux_bridge", "")).replace("\\", "/")
    if not expected_bridge_name.endswith("/" + bridge_identity.path.name):
        raise HpcEnvironmentError("protocol Linux bridge filename does not match transfer")
    if expected_bridge is not None and (
        expected_bridge.get("sha256") != bridge_identity.sha256
        or expected_bridge.get("size_bytes") != bridge_identity.size_bytes
    ):
        raise HpcEnvironmentError(
            "control release bridge differs from the shared bridge selected for activation"
        )


def build_transfer_release(
    paths: Sequence[Path],
    *,
    expected_bridge: Mapping[str, Any] | None = None,
) -> tuple[str, JSONMap, tuple[ArtifactIdentity, ...]]:
    identities = tuple(_classify_transfer_artifact_identity(path) for path in paths)
    by_role = {identity.role: identity for identity in identities}
    required = {"protocol", "static_bridge", "compact_capsule", "compact_binding"}
    if len(identities) != len(by_role) or set(by_role) != required:
        raise HpcEnvironmentError(
            "a transfer release requires exactly one protocol, static bridge, "
            "compact capsule and compact binding"
        )
    if sum(identity.size_bytes for identity in identities) > _MAX_RELEASE_BYTES:
        raise HpcEnvironmentError("transfer release exceeds the compact-transfer bound")
    _validate_transfer_release_closure(by_role, expected_bridge=expected_bridge)
    rows = [
        {
            "role": identity.role,
            "name": identity.transfer_name,
            "size_bytes": identity.size_bytes,
            "sha256": identity.sha256,
        }
        for identity in sorted(identities, key=lambda item: item.role)
    ]
    release_id = hashlib.sha256(canonical_bytes(rows)).hexdigest()
    return (
        release_id,
        {"schema": RELEASE_SCHEMA, "release_id": release_id, "artifacts": rows},
        identities,
    )


def windows_path_to_wsl(path: Path) -> str:
    resolved = path.expanduser().resolve()
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise HpcEnvironmentError(f"WSL transfer requires a drive-letter path: {resolved}")
    suffix = resolved.as_posix()[3:]
    return f"/mnt/{drive[0].lower()}/{suffix}"


def _run(
    args: Sequence[str],
    *,
    timeout: int,
    stdin_utf8: str | None = None,
) -> CommandResult:
    try:
        if stdin_utf8 is None:
            completed = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        else:
            # Keep LF bytes exact.  Windows text-mode stdin rewrites LF to CRLF,
            # which makes a remote POSIX shell read e.g. ``set -eu\r``.  Sending
            # the script over stdin also prevents WSL's command-line boundary
            # from expanding remote ``$variables`` before ssh receives them.
            completed = subprocess.run(
                list(args),
                input=stdin_utf8.encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(255, "", f"{type(exc).__name__}")
    return CommandResult(completed.returncode, stdout, stderr)


def _ssh_options() -> tuple[str, ...]:
    return (
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "LogLevel=ERROR",
    )


class WslControlTransport:
    def __init__(self, site: HpcSite) -> None:
        self.site = site

    def run(self, command: str, *, timeout: int = 30) -> CommandResult:
        return _run(
            (
                "wsl.exe",
                "-d",
                self.site.wsl_distribution,
                "--",
                "ssh",
                *_ssh_options(),
                self.site.control_ssh_alias,
                "sh",
                "-s",
            ),
            timeout=timeout,
            stdin_utf8=command + "\n",
        )

    def copy(self, local_path: Path, remote_path: str, *, timeout: int = 120) -> CommandResult:
        if not remote_path.startswith("~/"):
            raise HpcEnvironmentError("control copy destination must be under ~/ remote root")
        _safe_relative_path(remote_path[2:], "control copy destination")
        return _run(
            (
                "wsl.exe",
                "-d",
                self.site.wsl_distribution,
                "--",
                "scp",
                *_ssh_options(),
                windows_path_to_wsl(local_path),
                f"{self.site.control_ssh_alias}:{remote_path}",
            ),
            timeout=timeout,
        )


_SCHEDULER_HELPER = r"""
import base64, json, pathlib, subprocess, sys
skill = pathlib.Path(sys.argv[1]).expanduser().resolve()
if str(skill) not in sys.path:
    sys.path.insert(0, str(skill))
import scheduler
action, node = sys.argv[2], sys.argv[3]
timeout = int(sys.argv[-1])
if action == "run":
    command = base64.b64decode(sys.argv[4]).decode("utf-8")
    try:
        rc, out, err = scheduler.run_on(node, command, timeout=timeout, check=False)
    except Exception as exc:
        rc, out, err = 255, "", type(exc).__name__
elif action == "copy":
    source, destination = sys.argv[4], sys.argv[5]
    try:
        ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
        target = scheduler._ssh_target_for_node(node)
        proc = subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", ssh_shell, source, target + ":" + destination],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        rc, out, err = 255, "", type(exc).__name__
else:
    rc, out, err = 255, "", "unknown_action"
print("O2O_SCHEDULER_RESULT=" + json.dumps(
    {"returncode": int(rc), "stdout": out, "stderr": err},
    ensure_ascii=False, separators=(",", ":"),
))
""".strip()


class SchedulerNodeTransport:
    def __init__(self, site: HpcSite) -> None:
        self.site = site

    def _helper(self, arguments: Sequence[str], *, timeout: int) -> CommandResult:
        result = _run(
            (
                "wsl.exe",
                "-d",
                self.site.wsl_distribution,
                "--",
                "python3",
                "-c",
                _SCHEDULER_HELPER,
                self.site.scheduler_skill_dir,
                *arguments,
                str(timeout),
            ),
            timeout=timeout + 10,
        )
        marker = "O2O_SCHEDULER_RESULT="
        payload_line = next(
            (line[len(marker) :] for line in result.stdout.splitlines() if line.startswith(marker)),
            None,
        )
        if result.returncode != 0 or payload_line is None:
            return CommandResult(255, "", "scheduler_transport_unavailable")
        try:
            payload = json.loads(payload_line)
            return CommandResult(
                int(payload["returncode"]),
                str(payload["stdout"]),
                str(payload["stderr"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return CommandResult(255, "", "scheduler_transport_invalid_result")

    def run(self, node: SiteNode, command: str, *, timeout: int = 30) -> CommandResult:
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        return self._helper(("run", node.transport_name, encoded), timeout=timeout)

    def copy(
        self,
        node: SiteNode,
        local_path: Path,
        remote_path: str,
        *,
        timeout: int = 180,
    ) -> CommandResult:
        if not remote_path.startswith("~/"):
            raise HpcEnvironmentError("node copy destination must be under ~/ shared root")
        relative_destination = _safe_relative_path(
            remote_path[2:], "node copy destination"
        )
        home_result = self.run(node, "cd || exit 90; pwd", timeout=30)
        home = home_result.stdout.strip()
        if (
            home_result.returncode != 0
            or not home.startswith("/home/")
            or not re.fullmatch(r"/[A-Za-z0-9._/-]+", home)
            or "/../" in home
        ):
            return CommandResult(255, "", "node_home_discovery_failed")
        destination = home.rstrip("/") + "/" + relative_destination
        return self._helper(
            (
                "copy",
                node.transport_name,
                windows_path_to_wsl(local_path),
                destination,
            ),
            timeout=timeout,
        )


class NestedSshNodeTransport:
    """Credential-free fallback for sites where the control host resolves nodes."""

    def __init__(self, site: HpcSite, control: WslControlTransport) -> None:
        self.site = site
        self.control = control

    def run(self, node: SiteNode, command: str, *, timeout: int = 30) -> CommandResult:
        inner = " ".join(
            [
                "ssh",
                *[shlex.quote(part) for part in _ssh_options()],
                shlex.quote(node.transport_name),
                shlex.quote(command),
            ]
        )
        return self.control.run(inner, timeout=timeout)

    def copy(
        self,
        node: SiteNode,
        local_path: Path,
        remote_path: str,
        *,
        timeout: int = 180,
    ) -> CommandResult:
        del node, local_path, remote_path, timeout
        return CommandResult(255, "", "nested_ssh_copy_not_supported")


def _parse_key_values(stdout: str) -> JSONMap:
    result: JSONMap = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[a-z][a-z0-9_]*", key):
            result[key] = value.strip()
    return result


def _transport_failure_class(result: CommandResult) -> str:
    marker = result.stderr.strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,95}", marker):
        return marker
    return f"TRANSPORT_RC_{result.returncode}"


def _fail_fast(*commands: str) -> str:
    """Compose one POSIX-shell dependency chain whose first failure is final."""

    return "; ".join(("set -eu", *commands))


def _remote_hash_test(path: str, expected_sha256: str) -> str:
    if not _SHA256.fullmatch(expected_sha256):
        raise HpcEnvironmentError("remote hash expectation is invalid")
    quoted = shlex.quote(path)
    return (
        f"actual_sha256=$(sha256sum -- {quoted}); "
        f"actual_sha256=${{actual_sha256%% *}}; "
        f"test \"$actual_sha256\" = {shlex.quote(expected_sha256)}"
    )


def _control_probe_command(site: HpcSite) -> str:
    root = shlex.quote(site.control_remote_root)
    return _fail_fast(
            "export LC_ALL=C",
            "cd || exit 90",
            "printf 'probe_status=OK\\n'",
            "printf 'hostname='; hostname",
            "printf 'arch='; uname -m",
            "printf 'kernel='; uname -sr",
            "printf 'logical_cpus='; getconf _NPROCESSORS_ONLN",
            "printf 'memtotal_kib='; grep '^MemTotal:' /proc/meminfo | tr -s ' ' | cut -d' ' -f2",
            "printf 'python='; python3 -c 'import sys; print(\"%d.%d.%d\" % sys.version_info[:3])'",
            "printf 'home_free_kib='; df -Pk . | tail -n1 | tr -s ' ' | cut -d' ' -f4",
            "printf 'sha256sum='; command -v sha256sum || true",
            "printf 'find='; command -v find || true",
            "printf 'readlink='; command -v readlink || true",
            f"if test -d {root}; then printf 'remote_root=EXISTS\\n'; else printf 'remote_root=ABSENT\\n'; fi",
    )


def _node_probe_command(site: HpcSite) -> str:
    root = shlex.quote(site.node_shared_root)
    node_python = shlex.quote(site.node_python)
    work_parent = shlex.quote(str(PurePosixPath(site.node_shared_root).parent))
    return _fail_fast(
            "export LC_ALL=C",
            "cd || exit 90",
            "printf 'probe_status=OK\\n'",
            "printf 'hostname='; hostname",
            "printf 'arch='; uname -m",
            "printf 'kernel='; uname -sr",
            "printf 'logical_cpus='; getconf _NPROCESSORS_ONLN",
            "printf 'physical_cores='; lscpu -p=SOCKET,CORE 2>/dev/null | grep -v '^#' | sort -u | wc -l",
            "printf 'memtotal_kib='; grep '^MemTotal:' /proc/meminfo | tr -s ' ' | cut -d' ' -f2",
            f"printf 'shared_fs_device='; stat -c %d {work_parent}",
            f"printf 'shared_free_kib='; df -Pk {work_parent} | tail -n1 | tr -s ' ' | cut -d' ' -f4",
            f"printf 'node_python='; {node_python} -c 'import sys; print(\"%d.%d.%d\" % sys.version_info[:3])'",
            "printf 'sha256sum='; command -v sha256sum || true",
            "printf 'base64='; command -v base64 || true",
            "printf 'rsync='; command -v rsync || true",
            "printf 'tar='; command -v tar || true",
            "printf 'od='; command -v od || true",
            "printf 'tr='; command -v tr || true",
            "printf 'sed='; command -v sed || true",
            "printf 'grep='; command -v grep || true",
            "printf 'wc='; command -v wc || true",
            "printf 'mktemp='; command -v mktemp || true",
            "printf 'readlink='; command -v readlink || true",
            f"if test -d {root}; then printf 'remote_root=EXISTS\\n'; else printf 'remote_root=ABSENT\\n'; fi",
    )


def _bootstrap_command(root: str) -> str:
    quoted = shlex.quote(root)
    children = " ".join(
        shlex.quote(f"{root}/{name}")
        for name in ("incoming", "releases", "receipts", "runs")
    )
    return _fail_fast(
            "umask 077",
            "cd || exit 90",
            f"mkdir -p -- {quoted} {children}",
            f"test -d {quoted} && test -w {quoted}",
            "printf 'bootstrap_status=READY\\n'",
    )


def _python_tuple(text: Any) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", text.strip())
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def probe_control(site: HpcSite, transport: WslControlTransport) -> JSONMap:
    result = transport.run(_control_probe_command(site), timeout=30)
    values = _parse_key_values(result.stdout)
    python = _python_tuple(values.get("python"))
    ready = (
        result.returncode == 0
        and values.get("probe_status") == "OK"
        and values.get("arch") == site.required_arch
        and python is not None
        and python[:2] >= site.minimum_control_python
        and bool(values.get("sha256sum"))
        and bool(values.get("find"))
        and bool(values.get("readlink"))
    )
    return {
        "name": "control_plane",
        "ssh_alias": site.control_ssh_alias,
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "CONTROL_PROBE_OR_RUNTIME_REQUIREMENT_FAILED",
        "inventory": values,
        "transport_returncode": result.returncode,
    }


def probe_node(site: HpcSite, transport: Any, node: SiteNode) -> JSONMap:
    result = transport.run(node, _node_probe_command(site), timeout=35)
    values = _parse_key_values(result.stdout)
    ready = (
        result.returncode == 0
        and values.get("probe_status") == "OK"
        and values.get("arch") == site.required_arch
        and bool(values.get("sha256sum"))
        and bool(values.get("base64"))
        and bool(values.get("rsync"))
        and bool(values.get("tar"))
        and all(
            bool(values.get(tool))
            for tool in ("od", "tr", "sed", "grep", "wc", "mktemp", "readlink")
        )
        and _python_tuple(values.get("node_python")) is not None
    )
    return {
        "name": node.name,
        "transport_name": node.transport_name,
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "NODE_PROBE_OR_RUNTIME_REQUIREMENT_FAILED",
        "inventory": values,
        "transport_returncode": result.returncode,
    }


def bootstrap_control(site: HpcSite, transport: WslControlTransport) -> JSONMap:
    result = transport.run(_bootstrap_command(site.control_remote_root), timeout=30)
    values = _parse_key_values(result.stdout)
    ready = result.returncode == 0 and values.get("bootstrap_status") == "READY"
    return {
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "CONTROL_BOOTSTRAP_FAILED",
        "transport_returncode": result.returncode,
    }


def bootstrap_node(site: HpcSite, transport: Any, node: SiteNode) -> JSONMap:
    result = transport.run(node, _bootstrap_command(site.node_shared_root), timeout=35)
    values = _parse_key_values(result.stdout)
    ready = result.returncode == 0 and values.get("bootstrap_status") == "READY"
    return {
        "name": node.name,
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "NODE_BOOTSTRAP_FAILED",
        "transport_returncode": result.returncode,
    }


def _bridge_remote_relative(site: HpcSite, bridge: Mapping[str, Any]) -> str:
    digest = bridge["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise HpcEnvironmentError("bridge digest is invalid")
    return (
        f"{site.node_shared_root}/releases/bridge/{digest}/"
        "o2obridge.linux-amd64"
    )


def _bridge_smoke_payload_base64() -> str:
    request_path = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
    request = _load_bounded_json_artifact(request_path)
    encounter = request.get("encounter")
    if not isinstance(request.get("raid"), Mapping) or not isinstance(
        encounter, dict
    ):
        raise HpcEnvironmentError("bridge smoke request lacks raid/encounter objects")
    targets = encounter.get("targets")
    if (
        not isinstance(targets, list)
        or len(targets) != 1
        or not isinstance(targets[0], Mapping)
    ):
        raise HpcEnvironmentError(
            "bridge smoke request requires exactly one clonable encounter target"
        )
    # Exercise the wire contract that motivated this bridge release. Whirlwind
    # is deterministic in target cardinality even though the actual outcomes
    # remain simulator-derived.
    targets.append(copy.deepcopy(targets[0]))
    attempt_id = "hpc-target-results-smoke"
    messages = (
        {"command": "load", "request": request, "seed": 20260911},
        {"command": "actions"},
        {
            "command": "act",
            "action": {"spell_id": 1680},
            "attempt_id": attempt_id,
        },
        {"command": "server_results", "attempt_ids": [attempt_id]},
        {"command": "close"},
    )
    payload = b"".join(canonical_bytes(message) + b"\n" for message in messages)
    return base64.b64encode(payload).decode("ascii")


def _bridge_verification_command(
    bridge_path: str,
    digest: str,
    *,
    current_link: str | None = None,
    expected_link_target: str | None = None,
) -> str:
    quoted_bridge = shlex.quote(bridge_path)
    commands = ["export LC_ALL=C", "cd || exit 90"]
    if current_link is not None:
        if expected_link_target is None:
            raise HpcEnvironmentError("current bridge verification needs an exact target")
        commands.extend(
            (
                f"test -L {shlex.quote(current_link)}",
                f"pointer_target=$(readlink -- {shlex.quote(current_link)})",
                f"test \"$pointer_target\" = {shlex.quote(expected_link_target)}",
            )
        )
    commands.extend(
        (
            f"test -f {quoted_bridge} && test -x {quoted_bridge} && test ! -L {quoted_bridge}",
            _remote_hash_test(bridge_path, digest),
            f"elf_magic=$(od -An -t x1 -N 4 {quoted_bridge} | tr -d '[:space:]')",
            "test \"$elf_magic\" = 7f454c46",
            f"elf_class=$(od -An -t u1 -j 4 -N 1 {quoted_bridge} | tr -d '[:space:]')",
            "test \"$elf_class\" = 2",
            f"elf_machine=$(od -An -t u2 -j 18 -N 2 {quoted_bridge} | tr -d '[:space:]')",
            "test \"$elf_machine\" = 62",
            "smoke_dir=$(mktemp -d \"${TMPDIR:-/tmp}/o2o-bridge-smoke.XXXXXX\")",
            "trap 'rm -rf -- \"$smoke_dir\"' EXIT HUP INT TERM",
            f"printf '%s' {shlex.quote(_bridge_smoke_payload_base64())} > \"$smoke_dir/request.b64\"",
            "base64 -d \"$smoke_dir/request.b64\" > \"$smoke_dir/request.jsonl\"",
            f"{quoted_bridge} < \"$smoke_dir/request.jsonl\" > \"$smoke_dir/response.jsonl\"",
            "smoke_lines=$(wc -l < \"$smoke_dir/response.jsonl\")",
            "test \"$smoke_lines\" -eq 5",
            "smoke_load=$(sed -n '1p' \"$smoke_dir/response.jsonl\")",
            "smoke_actions=$(sed -n '2p' \"$smoke_dir/response.jsonl\")",
            "smoke_act=$(sed -n '3p' \"$smoke_dir/response.jsonl\")",
            "smoke_results=$(sed -n '4p' \"$smoke_dir/response.jsonl\")",
            "smoke_close=$(sed -n '5p' \"$smoke_dir/response.jsonl\")",
            "printf '%s\\n' \"$smoke_load\" | grep -Fq '\"ok\":true'",
            "printf '%s\\n' \"$smoke_load\" | grep -Fq '\"command\":\"load\"'",
            "printf '%s\\n' \"$smoke_load\" | grep -Fq '\"state\":{'",
            "printf '%s\\n' \"$smoke_actions\" | grep -Fq '\"ok\":true'",
            "printf '%s\\n' \"$smoke_actions\" | grep -Fq '\"command\":\"actions\"'",
            "printf '%s\\n' \"$smoke_actions\" | grep -Fq '\"actions\":[{'",
            "printf '%s\\n' \"$smoke_act\" | grep -Fq '\"ok\":true'",
            "printf '%s\\n' \"$smoke_act\" | grep -Fq '\"command\":\"act\"'",
            "printf '%s\\n' \"$smoke_act\" | grep -Fq '\"casted\":true'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"ok\":true'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"command\":\"server_results\"'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"attempt_id\":\"hpc-target-results-smoke\"'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"target_results\":[{\"target_index\":0'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"target_index\":1'",
            "printf '%s\\n' \"$smoke_results\" | grep -Fq '\"pending_attempt_ids\":[]'",
            "test \"$smoke_close\" = '{\"ok\":true,\"command\":\"close\"}'",
            f"printf 'verified_sha256={digest}\\n'",
            "printf 'elf_magic=ELF64_X86_64\\n'",
            "printf 'smoke_status=REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY\\n'",
        )
    )
    if current_link is not None:
        commands.append("printf 'pointer_target=%s\\n' \"$pointer_target\"")
    return _fail_fast(*commands)


def _verify_bridge_on_nodes(
    site: HpcSite,
    transport: Any,
    *,
    bridge_path: str,
    digest: str,
    current_link: str | None = None,
    expected_link_target: str | None = None,
) -> list[JSONMap]:
    rows: list[JSONMap] = []
    for node in site.nodes:
        verify = transport.run(
            node,
            _bridge_verification_command(
                bridge_path,
                digest,
                current_link=current_link,
                expected_link_target=expected_link_target,
            ),
            timeout=60,
        )
        values = _parse_key_values(verify.stdout)
        ready = (
            verify.returncode == 0
            and values.get("verified_sha256") == digest
            and values.get("elf_magic") == "ELF64_X86_64"
            and values.get("smoke_status")
            == "REAL_LOAD_ACTIONS_ACT_TARGET_RESULTS_CLOSE_READY"
            and (
                current_link is None
                or values.get("pointer_target") == expected_link_target
            )
        )
        rows.append(
            {
                "name": node.name,
                "status": "READY" if ready else "BLOCKED",
                "verified_sha256": values.get("verified_sha256"),
                "elf": values.get("elf_magic"),
                "real_simulator_smoke": values.get("smoke_status"),
                "pointer_target": values.get("pointer_target"),
                "transport_returncode": verify.returncode,
                "transport_failure_class": (
                    None if ready else _transport_failure_class(verify)
                ),
            }
        )
    return rows


def _cleanup_remote_directory(
    transport: Any,
    target: str,
    *,
    node: SiteNode | None = None,
) -> None:
    command = _fail_fast(
        "cd || exit 90",
        f"test -n {shlex.quote(target)}",
        f"rm -rf -- {shlex.quote(target)}",
    )
    if node is None:
        transport.run(command, timeout=35)
    else:
        transport.run(node, command, timeout=35)


def stage_shared_bridge(
    site: HpcSite,
    transport: Any,
    bridge_path: Path,
    bridge: Mapping[str, Any],
) -> JSONMap:
    if site.node_transport_kind != "scheduler_run_on":
        return {
            "status": "BLOCKED",
            "reason": "NODE_TRANSPORT_DOES_NOT_SUPPORT_SAFE_COPY",
        }
    primary = site.nodes[0]
    digest = str(bridge["sha256"])
    final_rel = _bridge_remote_relative(site, bridge)
    release_dir = str(PurePosixPath(final_rel).parent)
    existing = transport.run(
        primary,
        _fail_fast(
            "cd || exit 90",
            f"test -x {shlex.quote(final_rel)} && test ! -L {shlex.quote(final_rel)}",
            _remote_hash_test(final_rel, digest),
        ),
        timeout=35,
    )
    install_status = "REUSED_IDENTICAL"
    if existing.returncode != 0:
        absence = transport.run(
            primary,
            _fail_fast(
                "cd || exit 90",
                f"test ! -e {shlex.quote(release_dir)} && test ! -L {shlex.quote(release_dir)}",
            ),
            timeout=35,
        )
        if absence.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "BRIDGE_EXISTING_IMMUTABLE_RELEASE_INVALID",
                "transport_failure_class": _transport_failure_class(existing),
            }
        install_status = "INSTALLED_NEW"
        stage_token = uuid.uuid4().hex
        incoming_dir = f"{site.node_shared_root}/incoming/bridge-{digest}-{stage_token}"
        incoming_file = f"{incoming_dir}/o2obridge.linux-amd64"
        prepare = transport.run(
            primary,
            _fail_fast(
                "umask 077",
                "cd || exit 90",
                f"test ! -e {shlex.quote(incoming_dir)} && test ! -L {shlex.quote(incoming_dir)}",
                f"mkdir -- {shlex.quote(incoming_dir)}",
            ),
            timeout=35,
        )
        if prepare.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "BRIDGE_STAGING_PREPARE_FAILED",
                "transport_failure_class": _transport_failure_class(prepare),
            }
        copied = transport.copy(
            primary,
            bridge_path,
            f"~/{incoming_file}",
            timeout=240,
        )
        if copied.returncode != 0:
            _cleanup_remote_directory(transport, incoming_dir, node=primary)
            return {
                "status": "BLOCKED",
                "reason": "BRIDGE_COPY_FAILED",
                "transport_failure_class": _transport_failure_class(copied),
            }
        install = transport.run(
            primary,
            _fail_fast(
                "umask 077",
                "cd || exit 90",
                f"test -f {shlex.quote(incoming_file)} && test ! -L {shlex.quote(incoming_file)}",
                _remote_hash_test(incoming_file, digest),
                f"chmod 700 {shlex.quote(incoming_file)}",
                f"mkdir -p -- {shlex.quote(str(PurePosixPath(release_dir).parent))}",
                f"test ! -e {shlex.quote(release_dir)} && test ! -L {shlex.quote(release_dir)}",
                f"mv -- {shlex.quote(incoming_dir)} {shlex.quote(release_dir)}",
            ),
            timeout=45,
        )
        if install.returncode != 0:
            _cleanup_remote_directory(transport, incoming_dir, node=primary)
            return {
                "status": "BLOCKED",
                "reason": "BRIDGE_VERIFY_OR_INSTALL_FAILED",
                "transport_failure_class": _transport_failure_class(install),
            }

    current_link = f"{site.node_shared_root}/current-bridge"
    relative_target = f"releases/bridge/{digest}"
    pre_activation = _verify_bridge_on_nodes(
        site,
        transport,
        bridge_path=final_rel,
        digest=digest,
    )
    if not all(row["status"] == "READY" for row in pre_activation):
        return {
            "status": "BLOCKED",
            "reason": "BRIDGE_FINAL_NOT_VERIFIED_ON_EVERY_NODE_BEFORE_ACTIVATION",
            "install_status": install_status,
            "release_relative_path": release_dir,
            "current_pointer_relative_path": current_link,
            "sha256": digest,
            "pre_activation_verification": pre_activation,
            "per_node_verification": [],
        }

    activation = uuid.uuid4().hex
    temporary_link = f"{site.node_shared_root}/.current-bridge-{activation}"
    activate = transport.run(
        primary,
        _fail_fast(
            "cd || exit 90",
            f"test ! -e {shlex.quote(temporary_link)} && test ! -L {shlex.quote(temporary_link)}",
            _remote_hash_test(final_rel, digest),
            f"ln -s -- {shlex.quote(relative_target)} {shlex.quote(temporary_link)}",
            (
                f"mv -Tf -- {shlex.quote(temporary_link)} {shlex.quote(current_link)} || "
                f"{{ activation_rc=$?; rm -f -- {shlex.quote(temporary_link)}; "
                "exit \"$activation_rc\"; }"
            ),
        ),
        timeout=45,
    )
    if activate.returncode != 0:
        return {
            "status": "BLOCKED",
            "reason": "BRIDGE_ATOMIC_ACTIVATION_FAILED",
            "transport_failure_class": _transport_failure_class(activate),
        }

    verifications = _verify_bridge_on_nodes(
        site,
        transport,
        bridge_path=current_link + "/o2obridge.linux-amd64",
        digest=digest,
        current_link=current_link,
        expected_link_target=relative_target,
    )
    ready = all(row["status"] == "READY" for row in verifications)
    return {
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "BRIDGE_NOT_VERIFIED_ON_EVERY_NODE",
        "install_status": install_status,
        "release_relative_path": release_dir,
        "current_pointer_relative_path": current_link,
        "sha256": digest,
        "pre_activation_verification": pre_activation,
        "per_node_verification": verifications,
    }


def _release_sidecars(
    manifest: Mapping[str, Any], identities: Sequence[ArtifactIdentity]
) -> tuple[bytes, bytes]:
    manifest_bytes = canonical_bytes(manifest) + b"\n"
    sums_bytes = (
        "".join(
            f"{identity.sha256}  {identity.transfer_name}\n"
            for identity in sorted(identities, key=lambda item: item.transfer_name)
        )
        + f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n"
    ).encode("ascii")
    return manifest_bytes, sums_bytes


def _remote_release_verification_command(
    directory: str,
    expected_hashes: Mapping[str, str],
    *,
    current_link: str | None = None,
    expected_link_target: str | None = None,
) -> str:
    if not expected_hashes:
        raise HpcEnvironmentError("release verification needs expected files")
    commands = ["export LC_ALL=C", "cd || exit 90"]
    if current_link is not None:
        if expected_link_target is None:
            raise HpcEnvironmentError("current release verification needs an exact target")
        commands.extend(
            (
                f"test -L {shlex.quote(current_link)}",
                f"pointer_target=$(readlink -- {shlex.quote(current_link)})",
                f"test \"$pointer_target\" = {shlex.quote(expected_link_target)}",
            )
        )
    directory_test = f"test -d {shlex.quote(directory)}"
    if current_link is None:
        directory_test += f" && test ! -L {shlex.quote(directory)}"
    commands.extend(
        (
            directory_test,
            # ``current`` is intentionally a directory symlink.  GNU find does
            # not traverse a command-line symlink unless -H is explicit, which
            # would otherwise make the exact entry count appear to be zero.
            f"entry_marks=$(find -H {shlex.quote(directory)} -mindepth 1 -maxdepth 1 -printf x)",
            f"test \"${{#entry_marks}}\" -eq {len(expected_hashes)}",
        )
    )
    for name, digest in sorted(expected_hashes.items()):
        if not _SAFE_NAME.fullmatch(name) or not _SHA256.fullmatch(digest):
            raise HpcEnvironmentError("release verification identity is invalid")
        path = f"{directory}/{name}"
        commands.extend(
            (
                f"test -f {shlex.quote(path)} && test ! -L {shlex.quote(path)}",
                _remote_hash_test(path, digest),
            )
        )
    commands.append("printf 'release_verification=EXACT_MANIFEST_AND_FILES_READY\\n'")
    if current_link is not None:
        commands.append("printf 'pointer_target=%s\\n' \"$pointer_target\"")
    return _fail_fast(*commands)


def stage_control_release(
    site: HpcSite,
    transport: WslControlTransport,
    artifact_paths: Sequence[Path],
    *,
    expected_bridge: Mapping[str, Any] | None = None,
) -> JSONMap:
    release_id, manifest, identities = build_transfer_release(
        artifact_paths, expected_bridge=expected_bridge
    )
    manifest_bytes, sums_bytes = _release_sidecars(manifest, identities)
    expected_hashes = {
        identity.transfer_name: identity.sha256 for identity in identities
    }
    expected_hashes["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    expected_hashes["SHA256SUMS"] = hashlib.sha256(sums_bytes).hexdigest()
    root = site.control_remote_root
    release_dir = f"{root}/releases/{release_id}"
    existing = transport.run(
        _remote_release_verification_command(release_dir, expected_hashes),
        timeout=45,
    )
    install_status = "REUSED_IDENTICAL"
    if existing.returncode != 0:
        absence = transport.run(
            _fail_fast(
                "cd || exit 90",
                f"test ! -e {shlex.quote(release_dir)} && test ! -L {shlex.quote(release_dir)}",
            ),
            timeout=35,
        )
        if absence.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "CONTROL_EXISTING_IMMUTABLE_RELEASE_INVALID",
                "release_id": release_id,
            }
        install_status = "INSTALLED_NEW"
        token = uuid.uuid4().hex
        incoming = f"{root}/incoming/release-{release_id}-{token}"
        prepare = transport.run(
            _fail_fast(
                "umask 077",
                "cd || exit 90",
                f"test ! -e {shlex.quote(incoming)} && test ! -L {shlex.quote(incoming)}",
                f"mkdir -- {shlex.quote(incoming)}",
            ),
            timeout=35,
        )
        if prepare.returncode != 0:
            return {"status": "BLOCKED", "reason": "CONTROL_RELEASE_PREPARE_FAILED"}
        with tempfile.TemporaryDirectory(prefix="o2o-hpc-manifest-") as temporary:
            temporary_root = Path(temporary)
            manifest_path = temporary_root / "manifest.json"
            sums_path = temporary_root / "SHA256SUMS"
            manifest_path.write_bytes(manifest_bytes)
            sums_path.write_bytes(sums_bytes)
            uploads = [
                (identity.path, identity.transfer_name) for identity in identities
            ] + [(manifest_path, "manifest.json"), (sums_path, "SHA256SUMS")]
            for local, name in uploads:
                copied = transport.copy(local, f"~/{incoming}/{name}", timeout=240)
                if copied.returncode != 0:
                    _cleanup_remote_directory(transport, incoming)
                    return {
                        "status": "BLOCKED",
                        "reason": "CONTROL_RELEASE_COPY_FAILED",
                        "failed_role": name,
                    }
        install = transport.run(
            _fail_fast(
                _remote_release_verification_command(incoming, expected_hashes),
                f"test ! -e {shlex.quote(release_dir)} && test ! -L {shlex.quote(release_dir)}",
                f"mv -- {shlex.quote(incoming)} {shlex.quote(release_dir)}",
            ),
            timeout=90,
        )
        if install.returncode != 0:
            _cleanup_remote_directory(transport, incoming)
            return {
                "status": "BLOCKED",
                "reason": "CONTROL_RELEASE_VERIFY_OR_INSTALL_FAILED",
                "release_id": release_id,
            }

    pre_activation = transport.run(
        _remote_release_verification_command(release_dir, expected_hashes),
        timeout=60,
    )
    pre_values = _parse_key_values(pre_activation.stdout)
    if (
        pre_activation.returncode != 0
        or pre_values.get("release_verification")
        != "EXACT_MANIFEST_AND_FILES_READY"
    ):
        return {
            "status": "BLOCKED",
            "reason": "CONTROL_RELEASE_NOT_EXACT_BEFORE_ACTIVATION",
            "release_id": release_id,
        }

    token = uuid.uuid4().hex
    temporary_link = f"{root}/.current-{token}"
    current_link = f"{root}/current"
    relative_target = "releases/" + release_id
    activate = transport.run(
        _fail_fast(
            _remote_release_verification_command(release_dir, expected_hashes),
            f"test ! -e {shlex.quote(temporary_link)} && test ! -L {shlex.quote(temporary_link)}",
            f"ln -s -- {shlex.quote(relative_target)} {shlex.quote(temporary_link)}",
            (
                f"mv -Tf -- {shlex.quote(temporary_link)} {shlex.quote(current_link)} || "
                f"{{ activation_rc=$?; rm -f -- {shlex.quote(temporary_link)}; "
                "exit \"$activation_rc\"; }"
            ),
        ),
        timeout=60,
    )
    if activate.returncode != 0:
        return {
            "status": "BLOCKED",
            "reason": "CONTROL_RELEASE_ACTIVATION_FAILED",
            "release_id": release_id,
        }
    post_activation = transport.run(
        _remote_release_verification_command(
            current_link,
            expected_hashes,
            current_link=current_link,
            expected_link_target=relative_target,
        ),
        timeout=60,
    )
    post_values = _parse_key_values(post_activation.stdout)
    if (
        post_activation.returncode != 0
        or post_values.get("release_verification")
        != "EXACT_MANIFEST_AND_FILES_READY"
        or post_values.get("pointer_target") != relative_target
    ):
        return {
            "status": "BLOCKED",
            "reason": "CONTROL_CURRENT_POST_ACTIVATION_VERIFICATION_FAILED",
            "release_id": release_id,
        }
    return {
        "status": "READY",
        "reason": None,
        "install_status": install_status,
        "release_id": release_id,
        "release_relative_path": release_dir,
        "current_pointer_relative_path": current_link,
        "manifest": manifest,
    }


def _site_identity(path: Path) -> JSONMap:
    size, digest = _sha256_file(path.expanduser().resolve())
    return {"path": str(path.expanduser().resolve()), "size_bytes": size, "sha256": digest}


def _write_receipt(receipt: Mapping[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
    target = directory / f"hpc-environment-{stamp}-{digest[:16]}.json"
    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(canonical_bytes(receipt) + b"\n")
    temporary.replace(target)
    return target


def run_environment(
    *,
    site_path: Path,
    bridge_path: Path,
    node_overrides: Sequence[str] = (),
    apply: bool = False,
    artifact_paths: Sequence[Path] = (),
    receipt_directory: Path = DEFAULT_RECEIPT_DIRECTORY,
) -> tuple[JSONMap, Path]:
    site = apply_node_overrides(load_site_config(site_path), node_overrides)
    bridge = inspect_bridge(bridge_path)
    if artifact_paths:
        # This preflight deliberately precedes even read-only remote probes so a
        # malformed or raw-disguised release can never coincide with mutation.
        build_transfer_release(artifact_paths, expected_bridge=bridge)
    control_transport = WslControlTransport(site)
    if site.node_transport_kind == "scheduler_run_on":
        node_transport: Any = SchedulerNodeTransport(site)
    else:
        node_transport = NestedSshNodeTransport(site, control_transport)

    control = probe_control(site, control_transport)
    nodes = [probe_node(site, node_transport, node) for node in site.nodes]
    shared_devices = {
        row["inventory"].get("shared_fs_device")
        for row in nodes
        if row["status"] == "READY"
    }
    shared_filesystem_ready = (
        len(nodes) > 0
        and all(row["status"] == "READY" for row in nodes)
        and len(shared_devices) == 1
        and None not in shared_devices
    )

    mutation: JSONMap = {
        "requested": apply,
        "control_bootstrap": {"status": "NOT_REQUESTED"},
        "node_bootstrap": [],
        "shared_bridge": {"status": "NOT_REQUESTED"},
        "control_release": {"status": "NOT_REQUESTED"},
    }
    if apply:
        if control["status"] == "READY":
            mutation["control_bootstrap"] = bootstrap_control(site, control_transport)
        else:
            mutation["control_bootstrap"] = {
                "status": "BLOCKED",
                "reason": "CONTROL_NOT_READY",
            }
        if shared_filesystem_ready:
            mutation["node_bootstrap"] = [
                bootstrap_node(site, node_transport, node) for node in site.nodes
            ]
        else:
            mutation["node_bootstrap"] = [
                {
                    "name": node.name,
                    "status": "BLOCKED",
                    "reason": "NODE_INVENTORY_OR_SHARED_FILESYSTEM_NOT_READY",
                }
                for node in site.nodes
            ]
        bootstraps_ready = (
            mutation["control_bootstrap"].get("status") == "READY"
            and mutation["node_bootstrap"]
            and all(row.get("status") == "READY" for row in mutation["node_bootstrap"])
        )
        if bootstraps_ready:
            mutation["shared_bridge"] = stage_shared_bridge(
                site, node_transport, bridge_path.expanduser().resolve(), bridge
            )
        else:
            mutation["shared_bridge"] = {
                "status": "BLOCKED",
                "reason": "BOOTSTRAP_NOT_READY",
            }
        if artifact_paths:
            if (
                mutation["control_bootstrap"].get("status") == "READY"
                and mutation["shared_bridge"].get("status") == "READY"
            ):
                mutation["control_release"] = stage_control_release(
                    site,
                    control_transport,
                    artifact_paths,
                    expected_bridge=bridge,
                )
            else:
                mutation["control_release"] = {
                    "status": "BLOCKED",
                    "reason": "CONTROL_BOOTSTRAP_OR_SHARED_BRIDGE_NOT_READY",
                }

    base_ready = (
        control["status"] == "READY"
        and all(row["status"] == "READY" for row in nodes)
        and shared_filesystem_ready
    )
    if not apply:
        status = "DRY_RUN_READY" if base_ready else "BLOCKED"
    else:
        requested_mutations = [
            mutation["control_bootstrap"],
            mutation["shared_bridge"],
            *mutation["node_bootstrap"],
        ]
        if artifact_paths:
            requested_mutations.append(mutation["control_release"])
        status = (
            "ENVIRONMENT_READY_NO_EXPERIMENT"
            if base_ready
            and all(row.get("status") == "READY" for row in requested_mutations)
            else "BLOCKED"
        )

    receipt: JSONMap = {
        "schema": RECEIPT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "site_config": _site_identity(site_path),
        "transport": {
            "control": "wsl_openssh_alias",
            "nodes": site.node_transport_kind,
            "credentials_embedded": False,
        },
        "control_plane": control,
        "nodes": nodes,
        "shared_filesystem": {
            "status": "READY" if shared_filesystem_ready else "BLOCKED",
            "device_ids": sorted(value for value in shared_devices if value is not None),
            "root_relative_to_node_home": site.node_shared_root,
        },
        "bridge": bridge,
        "mutation": mutation,
        "transfer_policy": {
            "allowed_roles": [
                "protocol",
                "static_bridge",
                "compact_capsule",
                "compact_binding",
            ],
            "raw_chronicle_bytes_allowed": False,
            "normalized_chronicle_bytes_allowed": False,
            "per_artifact_max_bytes": _MAX_ARTIFACT_BYTES,
            "release_max_bytes": _MAX_RELEASE_BYTES,
        },
        "execution": {
            "scientific_experiment_started": False,
            "scheduler_task_submitted": False,
            "pilot_workers_per_node": site.pilot_workers_per_node,
            "maximum_workers_per_node_before_benchmark": (
                site.maximum_workers_per_node_before_benchmark
            ),
        },
    }
    receipt_path = _write_receipt(receipt, receipt_directory.expanduser().resolve())
    return receipt, receipt_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument(
        "--node",
        action="append",
        default=[],
        metavar="NAME=TRANSPORT_NAME",
        help="override one configured node transport name without embedding credentials",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="one of exactly four allowlisted compact release artifacts",
    )
    parser.add_argument(
        "--receipt-directory", type=Path, default=DEFAULT_RECEIPT_DIRECTORY
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create user directories, stage/verify the bridge, and optionally stage a compact release",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt, receipt_path = run_environment(
            site_path=args.site_config,
            bridge_path=args.bridge,
            node_overrides=args.node,
            apply=args.apply,
            artifact_paths=args.artifact,
            receipt_directory=args.receipt_directory,
        )
    except HpcEnvironmentError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(f"status={receipt['status']}")
    print(f"receipt={receipt_path}")
    print("scientific_experiment_started=false")
    return 0 if receipt["status"] != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
