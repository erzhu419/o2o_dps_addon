"""Content-address the complete local Python closure for Fury v2 execution.

The identity is intentionally independent of checkout location.  Required
production entrypoints are resolved inside one project root, their local
``o2o_dps`` imports are followed recursively with Python's AST, and every
source file is stable-read before a canonical bundle digest is produced.

This is source identity only.  It does not attest a simulator binary, runtime
profile, offline corpus, target context, completed rollout, or DPS result.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tokenize
from typing import Any, Mapping, Sequence


SCHEMA = "fury_execution_source_identity/v2"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PRODUCTION_PATHS: tuple[str, ...] = (
    "o2o_dps/sim_bridge.py",
    "o2o_dps/expert_policy.py",
    "o2o_dps/expert_proposals.py",
    "o2o_dps/fury_expert_adapters.py",
    "o2o_dps/fury_contra_adapter_v2.py",
    "o2o_dps/fury_expert_guided_search_v1.py",
    "o2o_dps/fury_capsule_execution_binding_v2.py",
    "o2o_dps/fury_ordered_sink_executor_v2.py",
    "o2o_dps/fury_full_policy_rollout_v2.py",
    "o2o_dps/fury_paired_multiseed_runner_v2.py",
    "o2o_dps/fury_multiseed_evaluation_v2.py",
    "o2o_dps/fury_selection_admission_v2.py",
)

_LOCAL_PACKAGE = "o2o_dps"


class FuryExecutionSourceIdentityV2Error(RuntimeError):
    """The requested source closure cannot be identified without ambiguity."""


def build_fury_execution_source_identity_v2(
    *,
    project_root: str | Path = PROJECT_ROOT,
    required_relative_paths: Sequence[str] = REQUIRED_PRODUCTION_PATHS,
) -> dict[str, Any]:
    """Return deterministic file identities and a canonical closure digest.

    ``required_relative_paths`` is exposed for isolated verification and tests;
    production callers should use the frozen default entrypoint tuple.
    """

    root = _project_root(project_root)
    required = _required_paths(required_relative_paths)
    pending = list(required)
    queued = set(required)
    snapshots: dict[str, tuple[dict[str, Any], tuple[int, int, int, int]]] = {}

    while pending:
        relative = pending.pop(0)
        if relative in snapshots:
            continue
        path = _resolve_local_file(root, relative, label=f"source[{relative}]")
        payload, stat_key = _stable_read(path, label=f"source[{relative}]")
        row = {
            "relative_path": relative,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        snapshots[relative] = (row, stat_key)

        module_name = _module_name(relative)
        for imported in _local_import_paths(
            payload,
            module_name=module_name,
            project_root=root,
            source_relative_path=relative,
        ):
            if imported not in queued:
                queued.add(imported)
                pending.append(imported)
        for package_init in _package_initializers(relative, root):
            if package_init not in queued:
                queued.add(package_init)
                pending.append(package_init)

    _verify_snapshot_unchanged(root, snapshots)
    rows = [snapshots[path][0] for path in sorted(snapshots)]
    canonical = _canonical_bytes(rows)
    return {
        "schema": SCHEMA,
        "status": "PASS",
        "scope": "production_fury_v2_python_static_local_import_closure",
        "required_entrypoints": list(required),
        "dependency_resolution": {
            "method": "python_ast_recursive_local_imports",
            "local_package": _LOCAL_PACKAGE,
            "type_checking_imports_included": True,
            "function_local_imports_included": True,
            "standard_library_and_external_packages_excluded": True,
            "package_initializers_included": True,
            "symbol_imports_are_not_misclassified_as_modules": True,
        },
        "file_count": len(rows),
        "byte_count": sum(int(row["size_bytes"]) for row in rows),
        "files": rows,
        "canonical_bundle": {
            "algorithm": "sha256",
            "payload": "canonical_json(files)",
            "json": {
                "utf8": True,
                "ensure_ascii": False,
                "sort_keys": True,
                "separators": [",", ":"],
                "allow_nan": False,
                "trailing_newline": False,
            },
            "ordering": "relative_path_unicode_codepoint",
            "sha256": hashlib.sha256(canonical).hexdigest(),
        },
        "path_contract": {
            "root_embedded_in_identity": False,
            "relative_path_format": "normalized_posix",
            "symlinks_allowed": False,
            "path_escape_allowed": False,
            "casefold_duplicates_allowed": False,
        },
        "claim_boundary": {
            "source_identity_only": True,
            "simulator_binary_attested": False,
            "runtime_profile_attested": False,
            "offline_corpus_attested": False,
            "target_context_attested": False,
            "rollout_executed": False,
            "dps_comparison_validated": False,
        },
    }


def canonical_file_bundle_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    """Recompute the public canonical digest after strict row validation."""

    if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
        raise TypeError("files must be a sequence of mappings")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_folded: set[str] = set()
    prior: str | None = None
    for index, raw in enumerate(files):
        if not isinstance(raw, Mapping):
            raise FuryExecutionSourceIdentityV2Error(
                f"files[{index}] must be an object"
            )
        if set(raw) != {"relative_path", "size_bytes", "sha256"}:
            raise FuryExecutionSourceIdentityV2Error(
                f"files[{index}] fields must be relative_path, size_bytes, sha256"
            )
        relative = _safe_relative(raw["relative_path"], f"files[{index}].relative_path")
        size = raw["size_bytes"]
        digest = raw["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FuryExecutionSourceIdentityV2Error(
                f"files[{index}].size_bytes must be a non-negative integer"
            )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise FuryExecutionSourceIdentityV2Error(
                f"files[{index}].sha256 must be a lowercase SHA-256"
            )
        folded = relative.casefold()
        if relative in seen or folded in seen_folded:
            raise FuryExecutionSourceIdentityV2Error(
                f"duplicate file identity path: {relative}"
            )
        if prior is not None and relative <= prior:
            raise FuryExecutionSourceIdentityV2Error(
                "file identities must be strictly ordered by relative_path"
            )
        seen.add(relative)
        seen_folded.add(folded)
        prior = relative
        normalized.append(
            {
                "relative_path": relative,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if not normalized:
        raise FuryExecutionSourceIdentityV2Error("files must not be empty")
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def _project_root(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise FuryExecutionSourceIdentityV2Error(
            "symbolic-link project roots are not permitted"
        )
    try:
        root = path.resolve(strict=True)
    except OSError as error:
        raise FuryExecutionSourceIdentityV2Error(
            f"project root cannot be resolved: {path}"
        ) from error
    if not root.is_dir():
        raise FuryExecutionSourceIdentityV2Error(
            f"project root is not a directory: {root}"
        )
    if root.is_symlink():
        raise FuryExecutionSourceIdentityV2Error(
            "symbolic-link project roots are not permitted"
        )
    return root


def _required_paths(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("required_relative_paths must be a sequence of strings")
    if not values:
        raise FuryExecutionSourceIdentityV2Error(
            "required_relative_paths must not be empty"
        )
    result: list[str] = []
    exact: set[str] = set()
    folded: set[str] = set()
    for index, raw in enumerate(values):
        relative = _safe_relative(raw, f"required_relative_paths[{index}]")
        key = relative.casefold()
        if relative in exact or key in folded:
            raise FuryExecutionSourceIdentityV2Error(
                f"duplicate required path: {relative}"
            )
        if not relative.startswith(f"{_LOCAL_PACKAGE}/") or not relative.endswith(".py"):
            raise FuryExecutionSourceIdentityV2Error(
                f"required_relative_paths[{index}] must be a Python file under {_LOCAL_PACKAGE}/"
            )
        exact.add(relative)
        folded.add(key)
        result.append(relative)
    return tuple(sorted(result))


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} must be a nonempty string"
        )
    if "\\" in value or ":" in value:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} must be a normalized POSIX relative path"
        )
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} contains path traversal or a non-normalized segment"
        )
    normalized = pure.as_posix()
    if normalized != value:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} is not normalized"
        )
    return normalized


def _resolve_local_file(root: Path, relative: str, *, label: str) -> Path:
    safe = _safe_relative(relative, label)
    unresolved = root.joinpath(*PurePosixPath(safe).parts)
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError as error:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} is missing or inaccessible: {safe}"
        ) from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} escapes the project root: {safe}"
        ) from error
    if unresolved.is_symlink() or resolved.is_symlink():
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} is a symbolic link: {safe}"
        )
    if not resolved.is_file():
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} is not a regular file: {safe}"
        )
    return resolved


def _stable_read(path: Path, *, label: str) -> tuple[bytes, tuple[int, int, int, int]]:
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise FuryExecutionSourceIdentityV2Error(f"{label} cannot be read") from error
    before_key = _stat_key(before)
    after_key = _stat_key(after)
    if before_key != after_key or len(payload) != after.st_size:
        raise FuryExecutionSourceIdentityV2Error(
            f"{label} changed while it was being read"
        )
    return payload, after_key


def _verify_snapshot_unchanged(
    root: Path,
    snapshots: Mapping[str, tuple[Mapping[str, Any], tuple[int, int, int, int]]],
) -> None:
    for relative in sorted(snapshots):
        path = _resolve_local_file(root, relative, label=f"source[{relative}]")
        payload, current = _stable_read(path, label=f"source[{relative}]")
        original_row, original_stat = snapshots[relative]
        if (
            current != original_stat
            or len(payload) != original_row["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != original_row["sha256"]
        ):
            raise FuryExecutionSourceIdentityV2Error(
                f"source[{relative}] changed before closure finalization"
            )


def _stat_key(value: Any) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _module_name(relative: str) -> str:
    pure = PurePosixPath(relative)
    if pure.name == "__init__.py":
        return ".".join(pure.parent.parts)
    return ".".join((*pure.parent.parts, pure.stem))


def _package_initializers(relative: str, root: Path) -> list[str]:
    parts = list(PurePosixPath(relative).parent.parts)
    result: list[str] = []
    for length in range(1, len(parts) + 1):
        candidate = PurePosixPath(*parts[:length], "__init__.py").as_posix()
        unresolved = root.joinpath(*PurePosixPath(candidate).parts)
        if unresolved.is_file():
            result.append(candidate)
    return result


def _local_import_paths(
    payload: bytes,
    *,
    module_name: str,
    project_root: Path,
    source_relative_path: str,
) -> list[str]:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(payload).readline)
        source = payload.decode(encoding)
        tree = ast.parse(source, filename=source_relative_path)
    except (SyntaxError, UnicodeDecodeError, LookupError) as error:
        raise FuryExecutionSourceIdentityV2Error(
            f"source[{source_relative_path}] is not parseable Python: {type(error).__name__}"
        ) from error

    discovered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _LOCAL_PACKAGE or alias.name.startswith(
                    f"{_LOCAL_PACKAGE}."
                ):
                    relative = _module_to_relative(alias.name, project_root)
                    if relative is None:
                        raise FuryExecutionSourceIdentityV2Error(
                            f"source[{source_relative_path}] imports missing local module {alias.name}"
                        )
                    discovered.add(relative)
        elif isinstance(node, ast.ImportFrom):
            base_name = _import_from_base(node, module_name, source_relative_path)
            if base_name is None or not (
                base_name == _LOCAL_PACKAGE
                or base_name.startswith(f"{_LOCAL_PACKAGE}.")
            ):
                continue
            base_relative = _module_to_relative(base_name, project_root)
            if base_relative is None:
                raise FuryExecutionSourceIdentityV2Error(
                    f"source[{source_relative_path}] imports missing local module {base_name}"
                )
            discovered.add(base_relative)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate_name = f"{base_name}.{alias.name}"
                candidate = _module_to_relative(candidate_name, project_root)
                if candidate is not None:
                    discovered.add(candidate)
    return sorted(discovered)


def _import_from_base(
    node: ast.ImportFrom,
    module_name: str,
    source_relative_path: str,
) -> str | None:
    if node.level == 0:
        return node.module
    module_parts = module_name.split(".")
    source_is_package = PurePosixPath(source_relative_path).name == "__init__.py"
    package_parts = module_parts if source_is_package else module_parts[:-1]
    parents_to_remove = node.level - 1
    if parents_to_remove > len(package_parts) - 1:
        raise FuryExecutionSourceIdentityV2Error(
            f"source[{source_relative_path}] relative import escapes {_LOCAL_PACKAGE}"
        )
    base_parts = package_parts[: len(package_parts) - parents_to_remove]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _module_to_relative(module_name: str, root: Path) -> str | None:
    parts = module_name.split(".")
    if not parts or parts[0] != _LOCAL_PACKAGE or any(not part for part in parts):
        return None
    module_file = root.joinpath(*parts).with_suffix(".py")
    package_init = root.joinpath(*parts, "__init__.py")
    matches = [path for path in (module_file, package_init) if path.is_file()]
    if len(matches) > 1:
        raise FuryExecutionSourceIdentityV2Error(
            f"ambiguous local module has file and package forms: {module_name}"
        )
    if not matches:
        return None
    path = matches[0]
    try:
        relative = path.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise FuryExecutionSourceIdentityV2Error(
            f"local module escapes project root: {module_name}"
        ) from error
    _safe_relative(relative, f"module[{module_name}]")
    return relative


__all__ = (
    "PROJECT_ROOT",
    "REQUIRED_PRODUCTION_PATHS",
    "SCHEMA",
    "FuryExecutionSourceIdentityV2Error",
    "build_fury_execution_source_identity_v2",
    "canonical_file_bundle_sha256",
)
