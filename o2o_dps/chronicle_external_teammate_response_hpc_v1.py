"""Server-side map/reduce surface for the teammate-response development model.

Each worker streams one existing Stage-5 gzip partition once.  The stream is
compiled wave-by-wave into mergeable B/C/D empirical count tables and compact
validation strata.  Arm A retains only source-bound descriptors of the
unchanged historical EventMeta trace; it never copies the event corpus.

The reducer is development-validation only.  Missing, duplicate, divergent,
or source-mismatched worker output aborts reduction and cannot authorize model
adoption, comparison, voting, or deployment.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_wave_model_v2 as wave_model_v2
from . import chronicle_external_teammate_response_model_v1 as response_v1


SCHEMA = "chronicle_external_teammate_response_hpc/v1"
REVISION = "single_scan_joint_sufficient_statistics_source_runtime_bound_v3"
DISPATCH_SCHEMA = f"{SCHEMA}/dispatch"
WORKER_SCHEMA = f"{SCHEMA}/worker"
RESULT_SCHEMA = f"{SCHEMA}/development_validation"
REDUCER_REPLAY_SCHEMA = f"{SCHEMA}/reducer_replay"
SOURCE_CONTRACT_SCHEMA = f"{SCHEMA}/implementation_source_closure"
RUNTIME_CONTRACT_SCHEMA = f"{SCHEMA}/remote_runtime"
WORKER_MODULE = "o2o_dps.chronicle_external_teammate_response_hpc_v1"
NODES = tuple(f"node{index:03d}" for index in range(1, 7))
DYNAMIC_VARIANTS = (
    response_v1.ABLATION_B,
    response_v1.ABLATION_C,
    response_v1.ABLATION_D,
)
TABLE_FIELDS = (
    "mark_counts",
    "delay_counts",
    "target_counts",
    "damage_counts",
    "spell_name_counts",
    "source_guid_counts",
)
CONTEXT_TABLE_FIELDS = (
    "mark_counts",
    "delay_counts",
    "target_counts",
    "damage_counts",
)
JOINT_TRAINING_SCHEMA = f"{SCHEMA}/joint_dynamic_training_counts_v3"
D_SPECIFIC_CONTEXT_LEVELS = frozenset({"GUID", "CLASS_SPEC"})
JOINT_BUILD_COMPONENTS = [
    "C_BASE_ALL_CONTEXTS_SHARED_SPELL_AND_SOURCE",
    "D_GUID_CLASS_SPEC_CONTEXT_DELTA",
    "B_EXACT_C_PROJECTION_DROP_GUID_AND_SOURCE",
]
OBSERVATION_HEADS = ("mark", "delay", "target", "positive_damage")
VALIDATION_FRACTION = 0.20
SOURCE_ENTRY_MODULE_PATHS = (
    "o2o_dps/chronicle_external_teammate_response_hpc_v1.py",
    "o2o_dps/chronicle_external_teammate_response_model_v1.py",
)
SOURCE_MODULE_PATHS = (
    "o2o_dps/__init__.py",
    "o2o_dps/chronicle_combatant_sidecar.py",
    "o2o_dps/chronicle_encounter_reconstruction_v1.py",
    "o2o_dps/chronicle_external_api_ingest_v1.py",
    "o2o_dps/chronicle_external_api_manifest_union_v1.py",
    "o2o_dps/chronicle_external_character_dps_index_v1.py",
    "o2o_dps/chronicle_external_character_history_v1.py",
    "o2o_dps/chronicle_external_encounter_reconstruction_v2.py",
    "o2o_dps/chronicle_external_event_normalizer_v1.py",
    "o2o_dps/chronicle_external_reconstruction_admission_v1.py",
    "o2o_dps/chronicle_external_team_timeline_v2.py",
    "o2o_dps/chronicle_external_team_wave_model_v2.py",
    *SOURCE_ENTRY_MODULE_PATHS,
    "o2o_dps/chronicle_unit_classification_semantics_v1.py",
)


class TeammateResponseHpcV1Error(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TeammateResponseHpcV1Error(
            f"value is not canonical JSON: {error}"
        ) from error


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical(value))


def _is_canonical_json_plus_lf(value: Any, raw: bytes) -> bool:
    """Validate the exact canonical bytes without constructing a second LF copy."""

    canonical = _canonical(value)
    return (
        len(raw) == len(canonical) + 1
        and raw[-1] == 0x0A
        and memoryview(raw)[:-1] == canonical
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TeammateResponseHpcV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TeammateResponseHpcV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TeammateResponseHpcV1Error(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TeammateResponseHpcV1Error(f"{label} must be an integer")
    if nonnegative and value < 0:
        raise TeammateResponseHpcV1Error(f"{label} must be nonnegative")
    return value


def _sha256_text(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise TeammateResponseHpcV1Error(f"{label} must be a SHA-256 value")
    return text


def _discover_recursive_source_module_paths(package_directory: Path) -> tuple[str, ...]:
    package = package_directory.resolve()
    pending = [PurePosixPath(path).stem for path in SOURCE_ENTRY_MODULE_PATHS]
    discovered = {"o2o_dps/__init__.py"}
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        path = package / f"{module}.py"
        if not path.is_file():
            raise TeammateResponseHpcV1Error(
                f"implementation source module is absent: o2o_dps/{module}.py"
            )
        seen.add(module)
        discovered.add(f"o2o_dps/{module}.py")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            raise TeammateResponseHpcV1Error(
                f"cannot inspect implementation source imports: {path.name}"
            ) from error
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level < 1:
                continue
            candidates = (
                [node.module.split(".", 1)[0]]
                if node.module
                else [alias.name.split(".", 1)[0] for alias in node.names]
            )
            for candidate in candidates:
                if (package / f"{candidate}.py").is_file() and candidate not in seen:
                    pending.append(candidate)
    return tuple(sorted(discovered))


def _implementation_source_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    discovered = _discover_recursive_source_module_paths(
        Path(__file__).resolve().parent
    )
    if discovered != SOURCE_MODULE_PATHS:
        raise TeammateResponseHpcV1Error(
            "declared implementation source closure differs from recursive imports"
        )
    source = _mapping(value, "implementation source closure")
    if set(source) != {
        "schema",
        "source_archive_sha256",
        "release_locator",
        "module_sha256",
    } or source.get("schema") != SOURCE_CONTRACT_SCHEMA:
        raise TeammateResponseHpcV1Error(
            "implementation source closure fields or schema differ"
        )
    closure_sha = _sha256_text(
        source.get("source_archive_sha256"), "source archive sha256"
    )
    release_locator = _safe_relative(
        source.get("release_locator"), "source release locator"
    ).as_posix()
    if PurePosixPath(release_locator).name != closure_sha:
        raise TeammateResponseHpcV1Error(
            "source release locator is not named by its archive closure"
        )
    raw_modules = _mapping(source.get("module_sha256"), "source module sha256")
    if set(raw_modules) != set(SOURCE_MODULE_PATHS):
        raise TeammateResponseHpcV1Error("source module closure is incomplete")
    modules = {
        path: _sha256_text(raw_modules.get(path), f"source module {path}")
        for path in SOURCE_MODULE_PATHS
    }
    return {
        "schema": SOURCE_CONTRACT_SCHEMA,
        "source_archive_sha256": closure_sha,
        "release_locator": release_locator,
        "module_sha256": modules,
    }


def _remote_runtime_contract(
    value: Mapping[str, Any], *, source_release_locator: str
) -> dict[str, Any]:
    runtime = _mapping(value, "remote runtime contract")
    if set(runtime) != {
        "schema",
        "python_absolute_path",
        "pythonpath_locator",
        "module",
        "python_flags",
        "environment",
    } or runtime.get("schema") != RUNTIME_CONTRACT_SCHEMA:
        raise TeammateResponseHpcV1Error(
            "remote runtime contract fields or schema differ"
        )
    python_path = Path(
        _text(runtime.get("python_absolute_path"), "runtime absolute Python path")
    ).expanduser()
    if not python_path.is_absolute():
        raise TeammateResponseHpcV1Error(
            "runtime Python path must be absolute on the worker host"
        )
    python_absolute_path = str(python_path.resolve())
    pythonpath_locator = _safe_relative(
        runtime.get("pythonpath_locator"), "runtime PYTHONPATH locator"
    ).as_posix()
    if pythonpath_locator != source_release_locator:
        raise TeammateResponseHpcV1Error(
            "runtime PYTHONPATH is not the bound source release"
        )
    if runtime.get("module") != WORKER_MODULE:
        raise TeammateResponseHpcV1Error("runtime worker module differs")
    if runtime.get("python_flags") != ["-B"]:
        raise TeammateResponseHpcV1Error("runtime Python flags differ")
    environment = _mapping(runtime.get("environment"), "runtime environment")
    if dict(environment) != {
        "GOMAXPROCS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }:
        raise TeammateResponseHpcV1Error("runtime environment differs")
    return {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "python_absolute_path": python_absolute_path,
        "pythonpath_locator": pythonpath_locator,
        "module": WORKER_MODULE,
        "python_flags": ["-B"],
        "environment": dict(environment),
    }


def _validate_materialized_source_runtime(
    shared_root: Path,
    source: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> tuple[Path, Path]:
    release = _resolve(
        shared_root, source["release_locator"], "source release locator"
    )
    python_path = Path(runtime["python_absolute_path"]).resolve()
    marker = release / "source-archive.sha256"
    if (
        not release.is_dir()
        or not marker.is_file()
        or marker.read_text(encoding="ascii").strip()
        != source["source_archive_sha256"]
    ):
        raise TeammateResponseHpcV1Error("source release closure marker differs")
    for relative in SOURCE_MODULE_PATHS:
        path = release.joinpath(*PurePosixPath(relative).parts)
        if (
            not path.is_file()
            or _sha256(path.read_bytes()) != source["module_sha256"][relative]
        ):
            raise TeammateResponseHpcV1Error(
                f"materialized source module differs: {relative}"
            )
    if not python_path.is_file():
        raise TeammateResponseHpcV1Error(
            "materialized runtime Python executable is absent"
        )
    return release, python_path


def _validate_dispatch_source_runtime_contract(
    dispatch: Mapping[str, Any], *, shared_root: Path, validate_ambient: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        dispatch.get("schema") != DISPATCH_SCHEMA
        or dispatch.get("revision") != REVISION
        or dispatch.get("status") != "PREPARED_NOT_LAUNCHED"
    ):
        raise TeammateResponseHpcV1Error(
            "dispatch is not the current source/runtime-bound revision"
        )
    bindings = _mapping(dispatch.get("source_bindings"), "dispatch source bindings")
    source = _implementation_source_contract(
        _mapping(bindings.get("implementation_source"), "implementation source")
    )
    runtime = _remote_runtime_contract(
        _mapping(
            _mapping(dispatch.get("execution"), "dispatch execution").get(
                "remote_runtime"
            ),
            "dispatch remote runtime",
        ),
        source_release_locator=source["release_locator"],
    )
    if validate_ambient:
        _validate_ambient_source_runtime(shared_root, source, runtime)
    return source, runtime


def _validate_ambient_source_runtime(
    shared_root: Path,
    source: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> None:
    expected_release, expected_python = _validate_materialized_source_runtime(
        shared_root, source, runtime
    )
    if Path(sys.executable).resolve() != expected_python:
        raise TeammateResponseHpcV1Error(
            "ambient Python executable differs from dispatch runtime"
        )
    if os.environ.get("PYTHONPATH") != str(expected_release):
        raise TeammateResponseHpcV1Error(
            "ambient PYTHONPATH differs from dispatch runtime"
        )
    if any(os.environ.get(key) != expected for key, expected in runtime["environment"].items()):
        raise TeammateResponseHpcV1Error(
            "ambient worker environment differs from dispatch runtime"
        )
    if not sys.dont_write_bytecode:
        raise TeammateResponseHpcV1Error("ambient Python did not apply -B")
    if (
        os.environ.get("BOC_TEAMMATE_SOURCE_CLOSURE_SHA256")
        != source["source_archive_sha256"]
    ):
        raise TeammateResponseHpcV1Error(
            "ambient source closure identity differs from dispatch"
        )
    loaded_modules = {
        SOURCE_ENTRY_MODULE_PATHS[0]: Path(__file__).resolve(),
        SOURCE_ENTRY_MODULE_PATHS[1]: Path(response_v1.__file__).resolve(),
        "o2o_dps/chronicle_external_team_wave_model_v2.py": Path(
            wave_model_v2.__file__
        ).resolve(),
    }
    for relative, loaded in loaded_modules.items():
        expected = (expected_release / Path(relative)).resolve()
        if loaded != expected or _sha256(loaded.read_bytes()) != source["module_sha256"][relative]:
            raise TeammateResponseHpcV1Error(
                f"loaded implementation module differs from source closure: {relative}"
            )


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    materialized = dict(core)
    materialized["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(core),
    }
    return materialized


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    address = _mapping(value.get("content_address"), f"{label}.content_address")
    core = dict(value)
    core.pop("content_address", None)
    expected = _canonical_sha256(core)
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document without content_address"
        or address.get("sha256") != expected
    ):
        raise TeammateResponseHpcV1Error(f"{label} content address differs")
    return expected


def _read_canonical_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TeammateResponseHpcV1Error(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise TeammateResponseHpcV1Error(f"{label} is not canonical JSON plus LF")
    return value, payload


def _safe_relative(locator: Any, label: str) -> PurePosixPath:
    pure = PurePosixPath(_text(locator, label))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise TeammateResponseHpcV1Error(f"{label} must be a safe relative path")
    return pure


def _resolve(root: Path, locator: Any, label: str) -> Path:
    pure = _safe_relative(locator, label)
    candidate = root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise TeammateResponseHpcV1Error(f"{label} escapes shared root") from error
    return candidate


def _relative(path: Path, root: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise TeammateResponseHpcV1Error(f"{label} is outside shared root") from error


def _write_once(path: Path, payload: bytes) -> str:
    """Atomically publish immutable bytes or resume the identical result."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise TeammateResponseHpcV1Error(f"divergent duplicate output: {path}")
        return "RESUMED"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise TeammateResponseHpcV1Error(
                    f"divergent duplicate output: {path}"
                )
            return "RESUMED"
        return "PUBLISHED"
    finally:
        temporary.unlink(missing_ok=True)


def _gzip_payload(document: Mapping[str, Any]) -> bytes:
    canonical = _canonical(document)
    buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buffer,
        mode="wb",
        compresslevel=6,
        mtime=0,
        filename="",
    ) as handle:
        handle.write(canonical)
        handle.write(b"\n")
    return buffer.getvalue()


def _read_gzip_document(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        raw = gzip.decompress(payload)
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TeammateResponseHpcV1Error(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or not _is_canonical_json_plus_lf(value, raw):
        raise TeammateResponseHpcV1Error(
            f"{label} is not canonical gzip JSON plus LF"
        )
    return value, payload


def _read_gzip_document_low_copy(path: Path, label: str) -> dict[str, Any]:
    """Read a reducer input without retaining its compressed representation."""

    try:
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TeammateResponseHpcV1Error(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or not _is_canonical_json_plus_lf(value, raw):
        raise TeammateResponseHpcV1Error(
            f"{label} is not canonical gzip JSON plus LF"
        )
    return value


def _files_equal(left: Path, right: Path, *, chunk_size: int = 1 << 20) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(chunk_size)
            right_chunk = right_handle.read(chunk_size)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _freeze(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _json_key(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_key(child) for child in value]
    return value


def _serialize_counter_table(table: Mapping[Any, Counter[Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, counts in table.items():
        count_rows = [
            {"value": _json_key(value), "count": count}
            for value, count in counts.items()
        ]
        count_rows.sort(key=lambda row: _canonical(row["value"]))
        rows.append({"key": _json_key(key), "counts": count_rows})
    rows.sort(key=lambda row: _canonical(row["key"]))
    return rows


def _load_counter_table(rows: Any, label: str) -> dict[Any, Counter[Any]]:
    result: dict[Any, Counter[Any]] = {}
    for raw in _array(rows, label):
        row = _mapping(raw, f"{label} row")
        key = _freeze(row.get("key"))
        if key in result:
            raise TeammateResponseHpcV1Error(f"{label} has a duplicate key")
        counts: Counter[Any] = Counter()
        for raw_count in _array(row.get("counts"), f"{label} counts"):
            count_row = _mapping(raw_count, f"{label} count")
            value = _freeze(count_row.get("value"))
            count = _integer(count_row.get("count"), f"{label} count", nonnegative=True)
            if count <= 0 or value in counts:
                raise TeammateResponseHpcV1Error(
                    f"{label} counts must be positive and unique"
                )
            counts[value] = count
        if not counts:
            raise TeammateResponseHpcV1Error(f"{label} counter is empty")
        result[key] = counts
    return result


def serialize_model_v1(
    model: response_v1.HierarchicalMarkedSemiMarkovV1,
) -> dict[str, Any]:
    return _serialize_model_v1(model, consume=False)


def _serialize_model_v1(
    model: response_v1.HierarchicalMarkedSemiMarkovV1,
    *,
    consume: bool,
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for field in TABLE_FIELDS:
        source = getattr(model, field)
        tables[field] = _serialize_counter_table(source)
        if consume:
            source.clear()
    return {
        "schema": response_v1.MODEL_SCHEMA,
        "variant": response_v1.ablation_variant_v1(model.variant_id),
        "minimums": deepcopy(model.minimums),
        "row_count": model.row_count,
        "tables": tables,
    }


def deserialize_model_v1(value: Mapping[str, Any]) -> response_v1.HierarchicalMarkedSemiMarkovV1:
    document = _mapping(value, "serialized model")
    if document.get("schema") != response_v1.MODEL_SCHEMA:
        raise TeammateResponseHpcV1Error("unsupported serialized model")
    variant = _mapping(document.get("variant"), "serialized model variant")
    variant_id = _text(variant.get("variant_id"), "serialized model variant_id")
    if dict(variant) != response_v1.ablation_variant_v1(variant_id):
        raise TeammateResponseHpcV1Error("serialized model variant differs")
    minimums = _mapping(document.get("minimums"), "serialized model minimums")
    model = response_v1.HierarchicalMarkedSemiMarkovV1(
        variant_id=variant_id,
        min_guid_events=_integer(minimums.get("GUID"), "GUID minimum", nonnegative=True),
        min_class_spec_events=_integer(
            minimums.get("CLASS_SPEC"), "CLASS_SPEC minimum", nonnegative=True
        ),
        min_class_events=_integer(
            minimums.get("CLASS"), "CLASS minimum", nonnegative=True
        ),
    )
    if dict(model.minimums) != dict(minimums):
        raise TeammateResponseHpcV1Error("serialized model minimums differ")
    tables = _mapping(document.get("tables"), "serialized model tables")
    if set(tables) != set(TABLE_FIELDS):
        raise TeammateResponseHpcV1Error("serialized model table set differs")
    for field in TABLE_FIELDS:
        loaded = _load_counter_table(tables[field], f"serialized {field}")
        destination = getattr(model, field)
        for key, counts in loaded.items():
            destination[key].update(counts)
    model.row_count = _integer(document.get("row_count"), "model row_count", nonnegative=True)
    if sum(model.mark_counts[("GLOBAL",)].values()) != model.row_count:
        raise TeammateResponseHpcV1Error("serialized model row count differs")
    return model


class _JointTrainingCountsV3:
    """Exact B/C/D sufficient statistics without three duplicate models."""

    def __init__(
        self,
        *,
        min_guid_events: int = 25,
        min_class_spec_events: int = 50,
        min_class_events: int = 100,
    ) -> None:
        self.base_c = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_C,
            min_guid_events=min_guid_events,
            min_class_spec_events=min_class_spec_events,
            min_class_events=min_class_events,
        )
        self.d_specific: dict[str, dict[Any, Counter[Any]]] = {
            field: defaultdict(Counter) for field in CONTEXT_TABLE_FIELDS
        }
        self.row_count = 0

    def update(self, row: Mapping[str, Any]) -> None:
        if row.get("schema") not in {
            response_v1.ROW_SCHEMA,
            response_v1.SUFFICIENT_ROW_SCHEMA,
        }:
            raise TeammateResponseHpcV1Error(
                "unsupported response transition row"
            )
        actor = _mapping(row.get("actor"), "row actor")
        emission = _mapping(
            row.get("emission_state_before_current_event"), "emission state"
        )
        timing = _mapping(
            row.get("timing_state_after_previous_actor_event"), "timing state"
        )
        label = _mapping(row.get("label"), "row label")
        token = _text(label.get("mark_token"), "mark token")
        delay = _integer(
            label.get("inter_event_delay_ms"), "inter-event delay", nonnegative=True
        )
        damage = _integer(label.get("damage_amount"), "damage amount", nonnegative=True)
        target_mode = _text(label.get("target_mode"), "target mode")
        delay_bucket = response_v1._delay_bucket(delay)
        damage_bucket = response_v1._damage_bucket(damage)

        for context in response_v1._context_keys(
            actor, emission, response_v1.ABLATION_C
        ):
            self.base_c.mark_counts[context][token] += 1
            self.base_c.target_counts[(context, token)][target_mode] += 1
            self.base_c.damage_counts[(context, token)][damage_bucket] += 1
        for context in response_v1._context_keys(
            actor, timing, response_v1.ABLATION_C
        ):
            self.base_c.delay_counts[context][delay_bucket] += 1

        for context in response_v1._context_keys(
            actor, emission, response_v1.ABLATION_D
        ):
            if context[0] in D_SPECIFIC_CONTEXT_LEVELS:
                self.d_specific["mark_counts"][context][token] += 1
                self.d_specific["target_counts"][(context, token)][target_mode] += 1
                self.d_specific["damage_counts"][(context, token)][damage_bucket] += 1
        for context in response_v1._context_keys(
            actor, timing, response_v1.ABLATION_D
        ):
            if context[0] in D_SPECIFIC_CONTEXT_LEVELS:
                self.d_specific["delay_counts"][context][delay_bucket] += 1

        spell_name = label.get("spell_name")
        self.base_c.spell_name_counts[token][
            spell_name if isinstance(spell_name, str) else None
        ] += 1
        self.base_c.source_guid_counts[
            (_text(actor.get("player_guid"), "actor guid"), token)
        ][response_v1._optional_text(label.get("exact_source_guid"))] += 1
        self.base_c.row_count += 1
        self.row_count += 1

    def merge(self, other: "_JointTrainingCountsV3") -> None:
        self.base_c.merge(other.base_c)
        for field in CONTEXT_TABLE_FIELDS:
            destination = self.d_specific[field]
            for key, counts in other.d_specific[field].items():
                destination[key].update(counts)
        self.row_count += other.row_count


def serialize_joint_training_v3(
    model: _JointTrainingCountsV3, *, consume: bool = False
) -> dict[str, Any]:
    if model.row_count != model.base_c.row_count:
        raise TeammateResponseHpcV1Error("joint/base row counts differ")
    expected_delta_total = 2 * model.row_count
    for field in CONTEXT_TABLE_FIELDS:
        if sum(sum(counts.values()) for counts in model.d_specific[field].values()) != expected_delta_total:
            raise TeammateResponseHpcV1Error(
                f"D-specific {field} total differs from two contexts per row"
            )
    base_c = _serialize_model_v1(model.base_c, consume=consume)
    delta_tables: dict[str, Any] = {}
    for field in CONTEXT_TABLE_FIELDS:
        delta_tables[field] = _serialize_counter_table(model.d_specific[field])
        if consume:
            model.d_specific[field].clear()
    return {
        "schema": JOINT_TRAINING_SCHEMA,
        "row_count": model.row_count,
        "base_c": base_c,
        "d_specific_context_delta": {
            "variant": response_v1.ablation_variant_v1(response_v1.ABLATION_D),
            "context_levels": ["GUID", "CLASS_SPEC"],
            "tables": delta_tables,
        },
    }


def deserialize_joint_training_v3(value: Mapping[str, Any]) -> _JointTrainingCountsV3:
    document = _mapping(value, "joint training counts")
    if set(document) != {
        "schema",
        "row_count",
        "base_c",
        "d_specific_context_delta",
    } or document.get("schema") != JOINT_TRAINING_SCHEMA:
        raise TeammateResponseHpcV1Error("unsupported joint training counts")
    base_c = deserialize_model_v1(_mapping(document.get("base_c"), "joint base C"))
    if base_c.variant_id != response_v1.ABLATION_C:
        raise TeammateResponseHpcV1Error("joint base is not variant C")
    row_count = _integer(document.get("row_count"), "joint row_count", nonnegative=True)
    if base_c.row_count != row_count:
        raise TeammateResponseHpcV1Error("joint/base row counts differ")
    delta = _mapping(
        document.get("d_specific_context_delta"), "D-specific context delta"
    )
    if (
        set(delta) != {"variant", "context_levels", "tables"}
        or dict(_mapping(delta.get("variant"), "D delta variant"))
        != response_v1.ablation_variant_v1(response_v1.ABLATION_D)
        or delta.get("context_levels") != ["GUID", "CLASS_SPEC"]
    ):
        raise TeammateResponseHpcV1Error("D-specific delta contract differs")
    tables = _mapping(delta.get("tables"), "D-specific tables")
    if set(tables) != set(CONTEXT_TABLE_FIELDS):
        raise TeammateResponseHpcV1Error("D-specific table set differs")
    result = _JointTrainingCountsV3(
        min_guid_events=base_c.minimums["GUID"],
        min_class_spec_events=base_c.minimums["CLASS_SPEC"],
        min_class_events=base_c.minimums["CLASS"],
    )
    result.base_c = base_c
    result.row_count = row_count
    expected_delta_total = 2 * row_count
    for field in CONTEXT_TABLE_FIELDS:
        loaded = _load_counter_table(tables[field], f"D-specific {field}")
        if any(key[0][0] not in D_SPECIFIC_CONTEXT_LEVELS if field in {"target_counts", "damage_counts"} else key[0] not in D_SPECIFIC_CONTEXT_LEVELS for key in loaded):
            raise TeammateResponseHpcV1Error(
                f"D-specific {field} contains a shared context"
            )
        if sum(sum(counts.values()) for counts in loaded.values()) != expected_delta_total:
            raise TeammateResponseHpcV1Error(
                f"D-specific {field} total differs from two contexts per row"
            )
        for key, counts in loaded.items():
            result.d_specific[field][key].update(counts)
    return result


def _merge_serialized_counter_table_v3(
    destination: dict[Any, Counter[Any]],
    rows: Any,
    label: str,
    *,
    key_validator: Any = None,
) -> tuple[int, int]:
    """Validate and merge one serialized table without a duplicate table model.

    Returns ``(all_count, global_context_count)``.  A malformed worker aborts
    reduction, so partial in-memory updates are deliberately not rolled back.
    No result is published after such an abort.
    """

    seen_keys: set[Any] = set()
    all_count = 0
    global_context_count = 0
    for raw in _array(rows, label):
        row = _mapping(raw, f"{label} row")
        key = _freeze(row.get("key"))
        if key in seen_keys:
            raise TeammateResponseHpcV1Error(f"{label} has a duplicate key")
        seen_keys.add(key)
        if key_validator is not None:
            key_validator(key)
        counts = destination.setdefault(key, Counter())
        seen_values: set[Any] = set()
        row_count = 0
        for raw_count in _array(row.get("counts"), f"{label} counts"):
            count_row = _mapping(raw_count, f"{label} count")
            item = _freeze(count_row.get("value"))
            count = _integer(
                count_row.get("count"), f"{label} count", nonnegative=True
            )
            if count <= 0 or item in seen_values:
                raise TeammateResponseHpcV1Error(
                    f"{label} counts must be positive and unique"
                )
            seen_values.add(item)
            counts[item] += count
            row_count += count
        if not seen_values:
            raise TeammateResponseHpcV1Error(f"{label} counter is empty")
        all_count += row_count
        context = key[0] if label.endswith(("target_counts", "damage_counts")) else key
        if isinstance(context, tuple) and context == ("GLOBAL",):
            global_context_count += row_count
    return all_count, global_context_count


def _merge_serialized_model_v1(
    destination: response_v1.HierarchicalMarkedSemiMarkovV1,
    value: Mapping[str, Any],
    *,
    expected_variant_id: str,
) -> int:
    document = _mapping(value, "serialized model")
    if document.get("schema") != response_v1.MODEL_SCHEMA:
        raise TeammateResponseHpcV1Error("unsupported serialized model")
    variant = _mapping(document.get("variant"), "serialized model variant")
    variant_id = _text(variant.get("variant_id"), "serialized model variant_id")
    if (
        variant_id != expected_variant_id
        or dict(variant) != response_v1.ablation_variant_v1(variant_id)
    ):
        raise TeammateResponseHpcV1Error("serialized model variant differs")
    minimums = _mapping(document.get("minimums"), "serialized model minimums")
    if dict(destination.minimums) != dict(minimums):
        raise TeammateResponseHpcV1Error("serialized model minimums differ")
    tables = _mapping(document.get("tables"), "serialized model tables")
    if set(tables) != set(TABLE_FIELDS):
        raise TeammateResponseHpcV1Error("serialized model table set differs")
    global_mark_count = 0
    for field in TABLE_FIELDS:
        _, global_count = _merge_serialized_counter_table_v3(
            getattr(destination, field),
            tables[field],
            f"serialized {field}",
        )
        if field == "mark_counts":
            global_mark_count = global_count
    row_count = _integer(
        document.get("row_count"), "model row_count", nonnegative=True
    )
    if global_mark_count != row_count:
        raise TeammateResponseHpcV1Error("serialized model row count differs")
    destination.row_count += row_count
    return row_count


def _merge_serialized_joint_training_v3(
    destination: _JointTrainingCountsV3,
    value: Mapping[str, Any],
    *,
    expected_row_count: int,
) -> None:
    """Absorb one worker receipt directly into the aggregate joint model."""

    document = _mapping(value, "joint training counts")
    if set(document) != {
        "schema",
        "row_count",
        "base_c",
        "d_specific_context_delta",
    } or document.get("schema") != JOINT_TRAINING_SCHEMA:
        raise TeammateResponseHpcV1Error("unsupported joint training counts")
    row_count = _integer(
        document.get("row_count"), "joint row_count", nonnegative=True
    )
    if row_count != expected_row_count:
        raise TeammateResponseHpcV1Error(
            "worker joint row count differs from compiled rows"
        )
    merged_base_rows = _merge_serialized_model_v1(
        destination.base_c,
        _mapping(document.get("base_c"), "joint base C"),
        expected_variant_id=response_v1.ABLATION_C,
    )
    if merged_base_rows != row_count:
        raise TeammateResponseHpcV1Error("joint/base row counts differ")
    delta = _mapping(
        document.get("d_specific_context_delta"), "D-specific context delta"
    )
    if (
        set(delta) != {"variant", "context_levels", "tables"}
        or dict(_mapping(delta.get("variant"), "D delta variant"))
        != response_v1.ablation_variant_v1(response_v1.ABLATION_D)
        or delta.get("context_levels") != ["GUID", "CLASS_SPEC"]
    ):
        raise TeammateResponseHpcV1Error("D-specific delta contract differs")
    tables = _mapping(delta.get("tables"), "D-specific tables")
    if set(tables) != set(CONTEXT_TABLE_FIELDS):
        raise TeammateResponseHpcV1Error("D-specific table set differs")
    expected_delta_total = 2 * row_count
    for field in CONTEXT_TABLE_FIELDS:
        def validate_key(key: Any, *, table_field: str = field) -> None:
            context = key[0] if table_field in {"target_counts", "damage_counts"} else key
            if not isinstance(context, tuple) or context[0] not in D_SPECIFIC_CONTEXT_LEVELS:
                raise TeammateResponseHpcV1Error(
                    f"D-specific {table_field} contains a shared context"
                )

        merged_count, _ = _merge_serialized_counter_table_v3(
            destination.d_specific[field],
            tables[field],
            f"D-specific {field}",
            key_validator=validate_key,
        )
        if merged_count != expected_delta_total:
            raise TeammateResponseHpcV1Error(
                f"D-specific {field} total differs from two contexts per row"
            )
    destination.row_count += row_count
    if destination.row_count != destination.base_c.row_count:
        raise TeammateResponseHpcV1Error("joint/base row counts differ")


def materialize_joint_variant_v3(
    joint: _JointTrainingCountsV3, variant_id: str
) -> response_v1.HierarchicalMarkedSemiMarkovV1:
    variant = response_v1.ablation_variant_v1(variant_id)
    if variant_id not in DYNAMIC_VARIANTS:
        raise TeammateResponseHpcV1Error("joint training variant is not dynamic")
    if joint.row_count != joint.base_c.row_count:
        raise TeammateResponseHpcV1Error("joint/base row counts differ")
    if variant_id == response_v1.ABLATION_C:
        return joint.base_c
    model = response_v1.HierarchicalMarkedSemiMarkovV1(
        variant_id=variant_id,
        min_guid_events=joint.base_c.minimums["GUID"],
        min_class_spec_events=joint.base_c.minimums["CLASS_SPEC"],
        min_class_events=joint.base_c.minimums["CLASS"],
    )
    retained_levels = (
        frozenset(variant["context_levels"])
        if variant_id == response_v1.ABLATION_B
        else frozenset({"CLASS", "GLOBAL"})
    )
    for field in CONTEXT_TABLE_FIELDS:
        destination = getattr(model, field)
        for key, counts in getattr(joint.base_c, field).items():
            context = key[0] if field in {"target_counts", "damage_counts"} else key
            if context[0] in retained_levels:
                destination[key].update(counts)
        if variant_id == response_v1.ABLATION_D:
            for key, counts in joint.d_specific[field].items():
                destination[key].update(counts)
    for key, counts in joint.base_c.spell_name_counts.items():
        model.spell_name_counts[key].update(counts)
    if variant_id == response_v1.ABLATION_D:
        for key, counts in joint.base_c.source_guid_counts.items():
            model.source_guid_counts[key].update(counts)
    model.row_count = joint.row_count
    return model


def _serialize_observations(
    values: Mapping[str, Counter[str]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        head: [
            {"signature": json.loads(signature), "count": count}
            for signature, count in sorted(values[head].items())
        ]
        for head in OBSERVATION_HEADS
    }


def _load_observations(value: Mapping[str, Any]) -> dict[str, Counter[str]]:
    if set(value) != set(OBSERVATION_HEADS):
        raise TeammateResponseHpcV1Error("observation head set differs")
    result = {head: Counter() for head in OBSERVATION_HEADS}
    for head in OBSERVATION_HEADS:
        for raw in _array(value[head], f"{head} observations"):
            row = _mapping(raw, f"{head} observation")
            signature = _canonical(row.get("signature")).decode("utf-8")
            count = _integer(row.get("count"), f"{head} count", nonnegative=True)
            if count <= 0 or signature in result[head]:
                raise TeammateResponseHpcV1Error(
                    f"{head} observations must be positive and unique"
                )
            result[head][signature] = count
    return result


def _merge_serialized_observations_v3(
    destination: dict[str, Counter[str]],
    value: Mapping[str, Any],
    *,
    expected_row_count: int,
) -> None:
    if set(value) != set(OBSERVATION_HEADS):
        raise TeammateResponseHpcV1Error("observation head set differs")
    for head in OBSERVATION_HEADS:
        seen_signatures: set[str] = set()
        total = 0
        for raw in _array(value[head], f"{head} observations"):
            row = _mapping(raw, f"{head} observation")
            signature = json.dumps(
                row.get("signature"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            count = _integer(row.get("count"), f"{head} count", nonnegative=True)
            if count <= 0 or signature in seen_signatures:
                raise TeammateResponseHpcV1Error(
                    f"{head} observations must be positive and unique"
                )
            seen_signatures.add(signature)
            destination[head][signature] += count
            total += count
        if (
            head in {"mark", "delay", "target"}
            and total != expected_row_count
        ) or (head == "positive_damage" and total > expected_row_count):
            raise TeammateResponseHpcV1Error(
                "validation observation counts differ from compiled rows"
            )


def _add_observation(
    counters: dict[str, Counter[str]],
    variant_id: str,
    row: Mapping[str, Any],
) -> None:
    actor = _mapping(row.get("actor"), "response actor")
    emission = _mapping(row.get("emission_state_before_current_event"), "emission")
    timing = _mapping(row.get("timing_state_after_previous_actor_event"), "timing")
    label = _mapping(row.get("label"), "response label")
    emission_contexts = [
        list(context)
        for context in response_v1._context_keys(actor, emission, variant_id)
    ]
    timing_contexts = [
        list(context)
        for context in response_v1._context_keys(actor, timing, variant_id)
    ]
    token = _text(label.get("mark_token"), "mark token")
    mark = {"contexts": emission_contexts, "token": token}
    delay = {
        "contexts": timing_contexts,
        "bucket": response_v1._delay_bucket(
            _integer(label.get("inter_event_delay_ms"), "delay", nonnegative=True)
        ),
    }
    target = {
        "contexts": emission_contexts,
        "token": token,
        "target_mode": _text(label.get("target_mode"), "target mode"),
    }
    counters["mark"][_canonical(mark).decode("utf-8")] += 1
    counters["delay"][_canonical(delay).decode("utf-8")] += 1
    counters["target"][_canonical(target).decode("utf-8")] += 1
    damage = _integer(label.get("damage_amount"), "damage amount", nonnegative=True)
    if damage > 0:
        positive = {
            "contexts": emission_contexts,
            "token": token,
            "bucket": response_v1._damage_bucket(damage),
        }
        counters["positive_damage"][_canonical(positive).decode("utf-8")] += 1


class _DigestingRaw(io.RawIOBase):
    def __init__(self, handle: io.BufferedReader) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()
        self.byte_count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        data = self.handle.read(len(buffer))
        size = len(data)
        if size:
            buffer[:size] = data
            self.digest.update(data)
            self.byte_count += size
        return size


def _stage5_identity(stage5: Mapping[str, Any]) -> str:
    if (
        stage5.get("schema") != wave_model_v2.SCHEMA
        or stage5.get("implementation_revision") != wave_model_v2.IMPLEMENTATION_REVISION
        or stage5.get("status") != wave_model_v2.STATUS
    ):
        raise TeammateResponseHpcV1Error("input is not the current Stage-5 artifact")
    return wave_model_v2._verify_content_address(stage5, label="Stage-5 manifest")


def build_dispatch_v1(
    *,
    stage5_manifest_path: str | Path,
    old50_overlay: Mapping[str, Any],
    split: Mapping[str, Any],
    response_plan: Mapping[str, Any],
    implementation_source: Mapping[str, Any],
    remote_runtime: Mapping[str, Any],
    shared_root: str | Path,
    output_root_locator: str,
) -> dict[str, Any]:
    root = Path(shared_root).expanduser().resolve()
    manifest_path = Path(stage5_manifest_path).expanduser().resolve()
    stage5, stage5_payload = _read_canonical_json(manifest_path, "Stage-5 manifest")
    stage5_sha = _stage5_identity(stage5)
    if manifest_path.stat().st_size != len(stage5_payload):
        raise TeammateResponseHpcV1Error("Stage-5 manifest size changed while read")
    split_doc = deepcopy(dict(_mapping(split, "component split")))
    overlay_doc = deepcopy(dict(_mapping(old50_overlay, "old50 overlay")))
    source_contract = _implementation_source_contract(implementation_source)
    runtime_contract = _remote_runtime_contract(
        remote_runtime,
        source_release_locator=source_contract["release_locator"],
    )
    _validate_materialized_source_runtime(root, source_contract, runtime_contract)
    overlay_sha = _verify_content_address(
        overlay_doc, "old50 overlay manifest"
    )
    try:
        bound_old50 = response_v1.old50_instance_ids_from_overlay_manifest_v1(
            overlay_doc, stage5
        )
    except response_v1.ChronicleExternalTeammateResponseModelV1Error as error:
        raise TeammateResponseHpcV1Error(str(error)) from error
    recomputed_split = response_v1.build_component_split_v1(
        stage5,
        old50_instance_ids=bound_old50,
        validation_fraction=VALIDATION_FRACTION,
    )
    if (
        split_doc != recomputed_split
        or _canonical(split_doc) != _canonical(recomputed_split)
    ):
        raise TeammateResponseHpcV1Error(
            "component split is not the exact deterministic Stage-5/old50 split"
        )
    plan_doc = deepcopy(dict(_mapping(response_plan, "response plan")))
    expected_plan = response_v1.build_remote_training_plan_v1(
        stage5,
        split_doc,
        nodes=NODES,
        root_protocol_reviewed=True,
        exact_dynamic_adapter_materialized_pass=True,
    )
    if plan_doc != expected_plan or plan_doc.get("status") != "PREPARED_NOT_EXECUTED":
        raise TeammateResponseHpcV1Error(
            "response plan is not the exact approved unexecuted plan"
        )
    split_rule = _mapping(split_doc.get("split_rule"), "split rule")
    boundary = _mapping(split_doc.get("scientific_boundary"), "split boundary")
    if (
        split_rule.get("old50_components_forced_to_train") is not True
        or split_rule.get("requested_validation_fraction") != VALIDATION_FRACTION
        or split_rule.get("old50_is_heldout_or_comparison") is not False
        or split_rule.get("row_random_split_allowed") is not False
        or split_rule.get("same_player_or_guild_can_cross_folds") is not False
        or boundary.get("comparison_eligible") is not False
        or boundary.get("voting_eligible") is not False
        or boundary.get("deployment_eligible") is not False
    ):
        raise TeammateResponseHpcV1Error("component split scientific boundary differs")
    components = [
        _mapping(row, "split component")
        for row in _array(split_doc.get("components"), "split components")
    ]
    validation_components = [
        row for row in components if row.get("split") == "VALIDATION"
    ]
    if not validation_components:
        raise TeammateResponseHpcV1Error(
            "exact response split must retain at least one validation component"
        )
    fold_by_instance: dict[str, str] = {}
    for component in components:
        fold = _text(component.get("split"), "component split")
        if fold not in {"TRAIN", "VALIDATION"}:
            raise TeammateResponseHpcV1Error("unsupported component split")
        for instance_id in _array(component.get("instance_ids"), "component instances"):
            key = _text(instance_id, "component instance")
            if key in fold_by_instance:
                raise TeammateResponseHpcV1Error("instance crosses split components")
            fold_by_instance[key] = fold
    old50 = [
        _text(value, "old50 instance")
        for value in _array(split_doc.get("old50_instance_ids"), "old50 instances")
    ]
    if not old50 or any(fold_by_instance.get(value) != "TRAIN" for value in old50):
        raise TeammateResponseHpcV1Error("old50 is not wholly nonheldout TRAIN")

    entries = {
        _text(entry.get("instance_id"), "Stage-5 instance_id"): _mapping(
            entry, "Stage-5 instance"
        )
        for entry in _array(stage5.get("instances"), "Stage-5 instances")
    }
    if len(entries) != len(_array(stage5.get("instances"), "Stage-5 instances")):
        raise TeammateResponseHpcV1Error("duplicate Stage-5 instance")
    assignment_nodes = [
        _text(row.get("node"), "assignment node")
        for row in _array(plan_doc.get("assignments"), "response assignments")
    ]
    if tuple(assignment_nodes) != NODES:
        raise TeammateResponseHpcV1Error("dispatch does not cover node001--node006 exactly")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for assignment in plan_doc["assignments"]:
        node = assignment["node"]
        for raw_task in _array(assignment.get("tasks"), "assignment tasks"):
            planned = _mapping(raw_task, "planned task")
            instance_id = _text(planned.get("instance_id"), "task instance_id")
            if instance_id in seen or instance_id not in entries:
                raise TeammateResponseHpcV1Error("duplicate or unknown planned instance")
            seen.add(instance_id)
            entry = entries[instance_id]
            wave_model_v2._verify_content_address(
                entry, label=f"Stage-5 instance {instance_id}"
            )
            partition = _mapping(entry.get("partition"), "Stage-5 partition")
            contamination = _mapping(
                entry.get("contamination_lane"), "candidate contamination lane"
            )
            if contamination.get("candidate_filter_passed") is not True:
                raise TeammateResponseHpcV1Error(
                    "planned task is not an exact Stage-5 training candidate"
                )
            partition_path = (manifest_path.parent / _text(partition.get("path"), "partition path")).resolve()
            locator = _relative(partition_path, root, "Stage-5 partition")
            if planned.get("split") != fold_by_instance.get(instance_id):
                raise TeammateResponseHpcV1Error("plan and component split differ")
            tasks.append(
                {
                    "instance_id": instance_id,
                    "node": node,
                    "split": fold_by_instance[instance_id],
                    "component_id": next(
                        row["component_id"]
                        for row in components
                        if instance_id in row["instance_ids"]
                    ),
                    "partition_locator": locator,
                    "partition": deepcopy(dict(partition)),
                    "expected_summary": deepcopy(
                        dict(_mapping(entry.get("summary"), "instance summary"))
                    ),
                    "contamination_lane": deepcopy(dict(contamination)),
                    "instance_entry_content_sha256": wave_model_v2._verify_content_address(
                        entry, label=f"Stage-5 instance {instance_id}"
                    ),
                }
            )
    if seen != set(fold_by_instance):
        raise TeammateResponseHpcV1Error("dispatch task set is incomplete or unexpected")
    excluded = {
        _text(value, "excluded instance")
        for value in _array(
            split_doc.get("excluded_descriptive_nontraining_instance_ids"),
            "excluded descriptive instances",
        )
    }
    if set(entries) - seen != excluded or any(
        _mapping(entries[value].get("contamination_lane"), "excluded contamination").get(
            "candidate_filter_passed"
        )
        is not False
        for value in excluded
    ):
        raise TeammateResponseHpcV1Error(
            "excluded descriptive Stage-5 instances differ from the split"
        )
    tasks.sort(key=lambda row: row["instance_id"])
    output_locator = _safe_relative(output_root_locator, "output_root_locator").as_posix()
    core = {
        "schema": DISPATCH_SCHEMA,
        "revision": REVISION,
        "status": "PREPARED_NOT_LAUNCHED",
        "source_bindings": {
            "implementation_source": source_contract,
            "stage5": {
                "manifest_locator": _relative(manifest_path, root, "Stage-5 manifest"),
                "content_sha256": stage5_sha,
                "file_sha256": _sha256(stage5_payload),
            },
            "component_split_sha256": _sha256(_canonical(split_doc)),
            "old50_stage5_overlap_overlay": {
                "content_sha256": overlay_sha,
                "instance_ids_sha256": _sha256(_canonical(bound_old50)),
                "scope": "old50 instances present in Stage-5",
            },
            "approved_response_plan_sha256": _sha256(_canonical(plan_doc)),
        },
        "split_contract": {
            "old50_stage5_overlap_instance_ids": sorted(old50),
            "old50_stage5_overlap_instance_count": len(old50),
            "old50_stage5_overlap_split": "TRAIN_NONHELDOUT",
            "old50_absent_from_stage5_not_tasked": True,
            "validation_component_ids": sorted(
                _text(row.get("component_id"), "validation component_id")
                for row in validation_components
            ),
            "validation_component_count": len(validation_components),
        },
        "execution": {
            "launched": False,
            "nodes": list(NODES),
            "gomaxprocs": 1,
            "threads_per_task": 1,
            "whole_instance_task_count": len(tasks),
            "partition_scans_per_task": 1,
            "dynamic_variants_built_in_same_scan": list(DYNAMIC_VARIANTS),
            "arm_a_full_trace_copied": False,
            "arm_a_execution_status": "DESCRIPTOR_ONLY_UNEXECUTED",
            "remote_runtime": runtime_contract,
            "worker_command_template": (
                "env GOMAXPROCS=1 PYTHONDONTWRITEBYTECODE=1 "
                "BOC_TEAMMATE_SOURCE_CLOSURE_SHA256="
                f"{source_contract['source_archive_sha256']} "
                f"PYTHONPATH={{shared_root}}/{runtime_contract['pythonpath_locator']} "
                f"{runtime_contract['python_absolute_path']} -B -m "
                f"{runtime_contract['module']} worker "
                "--dispatch {dispatch} --shared-root {shared_root} "
                "--instance-id {instance_id} --node {node}"
            ),
            "reducer_command_template": (
                "env GOMAXPROCS=1 PYTHONDONTWRITEBYTECODE=1 "
                "BOC_TEAMMATE_SOURCE_CLOSURE_SHA256="
                f"{source_contract['source_archive_sha256']} "
                f"PYTHONPATH={{shared_root}}/{runtime_contract['pythonpath_locator']} "
                f"{runtime_contract['python_absolute_path']} -B -m "
                f"{runtime_contract['module']} reduce "
                "--dispatch {dispatch} --shared-root {shared_root}"
            ),
        },
        "output_root_locator": output_locator,
        "tasks": tasks,
        "scientific_boundary": {
            "development_validation_only": True,
            "old50_stage5_overlap_nonheldout": True,
            "old50_absent_from_stage5_not_tasked": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
            "failure_authorizes_model_adoption": False,
            "heavy_training_started": False,
        },
    }
    return _content_addressed(core)


def save_dispatch_v1(dispatch: Mapping[str, Any], directory: str | Path) -> Path:
    document = deepcopy(dict(_mapping(dispatch, "dispatch")))
    digest = _verify_content_address(document, "dispatch")
    destination = Path(directory).expanduser().resolve()
    payload = _canonical(document) + b"\n"
    addressed = destination / f"dispatch.{digest}.json"
    stable = destination / "dispatch.json"
    _write_once(addressed, payload)
    _write_once(stable, payload)
    return stable


def _task_index(dispatch: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw in _array(dispatch.get("tasks"), "dispatch tasks"):
        task = _mapping(raw, "dispatch task")
        instance_id = _text(task.get("instance_id"), "task instance_id")
        if instance_id in result:
            raise TeammateResponseHpcV1Error("dispatch has duplicate instance task")
        result[instance_id] = task
    return result


def _expected_worker_source_binding(
    dispatch: Mapping[str, Any], task: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "stage5_content_sha256": dispatch["source_bindings"]["stage5"][
            "content_sha256"
        ],
        "instance_entry_content_sha256": task["instance_entry_content_sha256"],
        "partition_locator": task["partition_locator"],
        "compressed_file_sha256": task["partition"]["compressed_file_sha256"],
        "logical_content_sha256": task["partition"]["logical_content_sha256"],
    }


def _wave_descriptor(
    record: Mapping[str, Any], *, partition_locator: str, line_number: int
) -> dict[str, Any]:
    wave = deepcopy(dict(_mapping(record.get("wave"), "wave identity")))
    trace = _array(record.get("exact_trace"), "exact_trace")
    reconstruction = _mapping(
        _mapping(record.get("descriptive_outcome"), "descriptive outcome").get(
            "reconstruction_binding"
        ),
        "reconstruction binding",
    )
    window = _mapping(reconstruction.get("window"), "reconstruction window")
    return {
        "wave": wave,
        "source_partition_locator": partition_locator,
        "source_line_number": line_number,
        "exact_trace_count": len(trace),
        "exact_trace_content_sha256": _sha256(_canonical(trace)),
        "first_order_key": deepcopy(trace[0]["order_key"]) if trace else None,
        "last_order_key": deepcopy(trace[-1]["order_key"]) if trace else None,
        "historical_window": {
            "start_offset_ms": window.get("start_offset_ms"),
            "end_offset_ms": window.get("end_offset_ms"),
        },
        "historical_eventmeta_schedule": "REPLAY_UNCHANGED_FROM_BOUND_SOURCE",
        "full_trace_copied": False,
    }


def run_worker_v1(
    *,
    dispatch_path: str | Path,
    shared_root: str | Path,
    instance_id: str,
    node: str,
) -> dict[str, Any]:
    dispatch_file = Path(dispatch_path).expanduser().resolve()
    dispatch, _ = _read_canonical_json(dispatch_file, "dispatch")
    dispatch_sha = _verify_content_address(dispatch, "dispatch")
    root = Path(shared_root).expanduser().resolve()
    source_contract, runtime_contract = _validate_dispatch_source_runtime_contract(
        dispatch, shared_root=root, validate_ambient=True
    )
    task = _task_index(dispatch).get(instance_id)
    if task is None or task.get("node") != node or node not in NODES:
        raise TeammateResponseHpcV1Error("worker instance/node assignment differs")
    output = _resolve(root, dispatch.get("output_root_locator"), "output root") / "workers"
    stable = output / f"{instance_id}.json.gz"
    if stable.is_file():
        existing, payload = _read_gzip_document(stable, "existing worker output")
        existing_sha = _verify_content_address(existing, "existing worker output")
        addressed = output / f"{instance_id}.{existing_sha}.json.gz"
        if (
            existing.get("schema") != WORKER_SCHEMA
            or existing.get("revision") != REVISION
            or existing.get("dispatch_content_sha256") != dispatch_sha
            or existing.get("instance_id") != instance_id
            or existing.get("node") != node
            or existing.get("split") != task.get("split")
            or existing.get("component_id") != task.get("component_id")
            or existing.get("source_binding")
            != _expected_worker_source_binding(dispatch, task)
            or existing.get("producer_contract")
            != {
                "implementation_source": source_contract,
                "remote_runtime": runtime_contract,
            }
            or not addressed.is_file()
            or addressed.read_bytes() != payload
        ):
            raise TeammateResponseHpcV1Error(
                "existing worker output is not an exact resumable result"
            )
        return {
            "status": "RESUMED",
            "worker_output": str(stable),
            "content_sha256": existing_sha,
            "partition_scan_count": 0,
        }
    partition_path = _resolve(root, task.get("partition_locator"), "partition locator")
    partition = _mapping(task.get("partition"), "partition binding")
    expected_size = _integer(
        partition.get("compressed_size_bytes"), "compressed size", nonnegative=True
    )
    if not partition_path.is_file() or partition_path.stat().st_size != expected_size:
        raise TeammateResponseHpcV1Error("partition is absent or its size differs")
    joint_training = _JointTrainingCountsV3() if task["split"] == "TRAIN" else None
    observations = (
        {
            variant: {head: Counter() for head in OBSERVATION_HEADS}
            for variant in DYNAMIC_VARIANTS
        }
        if task["split"] == "VALIDATION"
        else None
    )
    descriptors: list[dict[str, Any]] = []
    logical = hashlib.sha256()
    logical_size = 0
    record_count = 0
    trace_kind_counts: Counter[str] = Counter()
    compiled_row_count = 0
    prior_wave: tuple[int, int, str] | None = None
    raw_handle = partition_path.open("rb")
    digesting = _DigestingRaw(raw_handle)
    try:
        with io.BufferedReader(digesting) as buffered, gzip.GzipFile(
            fileobj=buffered, mode="rb"
        ) as handle:
            for line_number, raw_line in enumerate(handle, 1):
                logical.update(raw_line)
                logical_size += len(raw_line)
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise TeammateResponseHpcV1Error(
                        f"invalid Stage-5 row {line_number}: {error}"
                    ) from error
                record = _mapping(value, "Stage-5 partition row")
                if raw_line != wave_model_v2._canonical_bytes(record) + b"\n":
                    raise TeammateResponseHpcV1Error(
                        "Stage-5 row is not canonical JSONL"
                    )
                if (
                    record.get("schema") != wave_model_v2.PARTITION_RECORD_SCHEMA
                    or record.get("implementation_revision")
                    != wave_model_v2.IMPLEMENTATION_REVISION
                    or record.get("status") != wave_model_v2.STATUS
                ):
                    raise TeammateResponseHpcV1Error("unsupported Stage-5 row")
                wave_model_v2._verify_content_address(
                    record, label="Stage-5 partition row"
                )
                wave_model_v2._validate_model_wave(
                    record,
                    instance_id=instance_id,
                    expected_contamination=_mapping(
                        task.get("contamination_lane"), "task contamination lane"
                    ),
                )
                wave = _mapping(record.get("wave"), "wave identity")
                if wave.get("instance_id") != instance_id:
                    raise TeammateResponseHpcV1Error(
                        "Stage-5 row crossed its instance partition"
                    )
                order = (
                    _integer(wave.get("encounter_ordinal"), "encounter ordinal", nonnegative=True),
                    _integer(wave.get("wave_ordinal"), "wave ordinal", nonnegative=True),
                    _text(wave.get("wave_id"), "wave_id"),
                )
                if prior_wave is not None and order <= prior_wave:
                    raise TeammateResponseHpcV1Error("partition wave order is not strict")
                prior_wave = order
                trace = _array(record.get("exact_trace"), "exact_trace")
                trace_kind_counts.update(
                    _text(row.get("trace_kind"), "trace kind") for row in trace
                )
                for row in response_v1.iter_wave_response_sufficient_rows_v1(record):
                    compiled_row_count += 1
                    if joint_training is not None:
                        joint_training.update(row)
                    else:
                        assert observations is not None
                        for variant in DYNAMIC_VARIANTS:
                            _add_observation(observations[variant], variant, row)
                descriptors.append(
                    _wave_descriptor(
                        record,
                        partition_locator=task["partition_locator"],
                        line_number=line_number,
                    )
                )
                record_count += 1
    except (OSError, EOFError) as error:
        raise TeammateResponseHpcV1Error(
            f"cannot stream Stage-5 partition: {error}"
        ) from error
    finally:
        if not raw_handle.closed:
            raw_handle.close()
    if digesting.byte_count != expected_size:
        raise TeammateResponseHpcV1Error("compressed stream byte count differs")
    if digesting.digest.hexdigest() != partition.get("compressed_file_sha256"):
        raise TeammateResponseHpcV1Error("compressed partition identity differs")
    if (
        logical.hexdigest() != partition.get("logical_content_sha256")
        or logical_size != partition.get("logical_size_bytes")
        or record_count != partition.get("record_count")
    ):
        raise TeammateResponseHpcV1Error(
            "logical partition identity, size, or record count differs"
        )
    expected_summary = _mapping(task.get("expected_summary"), "expected summary")
    exact_player_event_count = trace_kind_counts["EXACT_PLAYER_EVENT"]
    unattributed_event_count = trace_kind_counts["UNATTRIBUTED_EVENT"]
    death_marker_count = trace_kind_counts["DEATH_MARKER"]
    negative_damage_diagnostic_count = trace_kind_counts[
        "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT"
    ]
    classification_context_count = trace_kind_counts["CLASSIFICATION_CONTEXT"]
    exact_trace_count = sum(trace_kind_counts.values())
    stage5_total_event_count = (
        exact_player_event_count
        + unattributed_event_count
        + death_marker_count
        + negative_damage_diagnostic_count
    )
    if (
        record_count != expected_summary.get("wave_count")
        or stage5_total_event_count != expected_summary.get("exact_event_count")
        or exact_trace_count != expected_summary.get("exact_trace_count")
        or classification_context_count
        != expected_summary.get("classification_context_count")
        or death_marker_count != expected_summary.get("death_marker_count")
        or negative_damage_diagnostic_count
        != expected_summary.get("negative_damage_diagnostic_count")
    ):
        raise TeammateResponseHpcV1Error(
            "worker counts differ from the Stage-5 instance summary"
        )
    if compiled_row_count != exact_player_event_count:
        raise TeammateResponseHpcV1Error(
            "compiled response rows differ from exact-player event count"
        )
    core = {
        "schema": WORKER_SCHEMA,
        "revision": REVISION,
        "status": "COMPLETE_NO_ADOPTION_AUTHORITY",
        "dispatch_content_sha256": dispatch_sha,
        "instance_id": instance_id,
        "node": node,
        "split": task["split"],
        "component_id": task["component_id"],
        "source_binding": {
            "stage5_content_sha256": dispatch["source_bindings"]["stage5"][
                "content_sha256"
            ],
            "instance_entry_content_sha256": task[
                "instance_entry_content_sha256"
            ],
            "partition_locator": task["partition_locator"],
            "compressed_file_sha256": partition["compressed_file_sha256"],
            "logical_content_sha256": partition["logical_content_sha256"],
        },
        "producer_contract": {
            "implementation_source": source_contract,
            "remote_runtime": runtime_contract,
        },
        "single_scan_receipt": {
            "partition_open_count": 1,
            "partition_scan_count": 1,
            "compressed_bytes": digesting.byte_count,
            "logical_bytes": logical_size,
            "record_count": record_count,
            "exact_player_event_count": exact_player_event_count,
            "unattributed_event_count": unattributed_event_count,
            "death_marker_count": death_marker_count,
            "negative_damage_diagnostic_count": negative_damage_diagnostic_count,
            "classification_context_count": classification_context_count,
            "exact_trace_count": exact_trace_count,
            "stage5_total_event_count": stage5_total_event_count,
            "compiled_exact_player_row_count": compiled_row_count,
            "dynamic_sufficient_statistics_built_together": JOINT_BUILD_COMPONENTS,
        },
        "arm_a_fixed_schedule_descriptors": descriptors,
        "arm_a_execution_status": "DESCRIPTOR_ONLY_UNEXECUTED",
        "joint_dynamic_training_counts": (
            serialize_joint_training_v3(joint_training)
            if joint_training is not None
            else None
        ),
        "compact_evaluation_observations": (
            {
                variant: _serialize_observations(values)
                for variant, values in observations.items()
            }
            if observations is not None
            else None
        ),
        "scientific_boundary": {
            "development_training_or_validation_only": True,
            "old50_stage5_overlap_heldout_or_comparison": False,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
            "model_adoption_authorized": False,
        },
    }
    receipt = _content_addressed(core)
    receipt_sha = receipt["content_address"]["sha256"]
    payload = _gzip_payload(receipt)
    addressed = output / f"{instance_id}.{receipt_sha}.json.gz"
    _write_once(addressed, payload)
    publish_status = _write_once(stable, payload)
    return {
        "status": publish_status,
        "worker_output": str(stable),
        "content_sha256": receipt_sha,
        "partition_scan_count": 1,
    }


def _merge_observations(
    destination: dict[str, Counter[str]], source: Mapping[str, Counter[str]]
) -> None:
    for head in OBSERVATION_HEADS:
        destination[head].update(source[head])


def _select_context(
    model: response_v1.HierarchicalMarkedSemiMarkovV1,
    table: Mapping[tuple[str, ...], Counter[Any]],
    contexts: Sequence[Any],
) -> tuple[str, ...] | None:
    for raw in contexts:
        context = _freeze(raw)
        if not isinstance(context, tuple) or not context:
            raise TeammateResponseHpcV1Error("evaluation context is malformed")
        if sum(table.get(context, Counter()).values()) >= model.minimums[context[0]]:
            return context
    return None


def _bucket_midpoint_delay(bucket: int) -> float:
    if bucket == 0:
        return 0.0
    lower = response_v1.DELAY_UPPER_BOUNDS_MS[bucket - 1] + 1
    upper = (
        response_v1.DELAY_UPPER_BOUNDS_MS[bucket]
        if bucket < len(response_v1.DELAY_UPPER_BOUNDS_MS)
        else response_v1.DELAY_UPPER_BOUNDS_MS[-1] * 2
    )
    return (lower + upper) / 2.0


def _bucket_midpoint_damage(bucket: int) -> float:
    if bucket <= 0:
        return 0.0
    return ((1 << (bucket - 1)) + ((1 << bucket) - 1)) / 2.0


class _JointContextTableViewV3:
    """Read-only route from a D context to shared C or D-specific counts."""

    def __init__(
        self,
        shared: Mapping[Any, Counter[Any]],
        specific: Mapping[Any, Counter[Any]],
        *,
        token_keyed: bool,
    ) -> None:
        self.shared = shared
        self.specific = specific
        self.token_keyed = token_keyed

    def _specific_for(self, key: Any) -> bool:
        context = key[0] if self.token_keyed else key
        return context[0] in D_SPECIFIC_CONTEXT_LEVELS

    def get(self, key: Any, default: Any = None) -> Any:
        source = self.specific if self._specific_for(key) else self.shared
        return source.get(key, default)

    def __getitem__(self, key: Any) -> Counter[Any]:
        source = self.specific if self._specific_for(key) else self.shared
        return source[key]


class _JointEvaluationModelViewV3:
    """Evaluator-compatible B/C/D view sharing one joint model's counters."""

    def __init__(self, joint: _JointTrainingCountsV3, variant_id: str) -> None:
        if variant_id not in DYNAMIC_VARIANTS:
            raise TeammateResponseHpcV1Error("joint training variant is not dynamic")
        self.variant_id = variant_id
        self.minimums = joint.base_c.minimums
        for field in CONTEXT_TABLE_FIELDS:
            shared = getattr(joint.base_c, field)
            if variant_id == response_v1.ABLATION_D:
                table: Any = _JointContextTableViewV3(
                    shared,
                    joint.d_specific[field],
                    token_keyed=field in {"target_counts", "damage_counts"},
                )
            else:
                # B validation signatures only contain CLASS/GLOBAL contexts;
                # extra C keys are unreachable and therefore semantically inert.
                table = shared
            setattr(self, field, table)


def _evaluate_joint_variant_v3(
    joint: _JointTrainingCountsV3,
    variant_id: str,
    observations: Mapping[str, Counter[str]],
) -> dict[str, Any]:
    return evaluate_development_validation_v1(
        _JointEvaluationModelViewV3(joint, variant_id),  # type: ignore[arg-type]
        observations,
    )


def evaluate_development_validation_v1(
    model: response_v1.HierarchicalMarkedSemiMarkovV1,
    observations: Mapping[str, Counter[str]],
) -> dict[str, Any]:
    """Evaluate the frozen aggregate metrics without expanding validation rows."""

    missing_support = Counter()
    mark_nll = 0.0
    mark_weight = 0
    for signature, count in observations["mark"].items():
        item = _mapping(json.loads(signature), "mark signature")
        context = _select_context(model, model.mark_counts, item["contexts"])
        if context is None:
            missing_support["mark"] += count
            continue
        counts = model.mark_counts[context]
        vocabulary = len(counts) + 1
        probability = (counts[item["token"]] + 1.0) / (
            sum(counts.values()) + vocabulary
        )
        mark_nll -= count * math.log(probability)
        mark_weight += count

    delay_log_mae = 0.0
    delay_brier = 0.0
    delay_weight = 0
    for signature, count in observations["delay"].items():
        item = _mapping(json.loads(signature), "delay signature")
        context = _select_context(model, model.delay_counts, item["contexts"])
        if context is None:
            missing_support["delay"] += count
            continue
        counts = model.delay_counts[context]
        total = sum(counts.values())
        actual = _integer(item["bucket"], "delay bucket", nonnegative=True)
        actual_log = math.log1p(_bucket_midpoint_delay(actual))
        probabilities = {bucket: amount / total for bucket, amount in counts.items()}
        delay_log_mae += count * sum(
            probability
            * abs(math.log1p(_bucket_midpoint_delay(bucket)) - actual_log)
            for bucket, probability in probabilities.items()
        )
        categories = set(probabilities) | {actual}
        delay_brier += count * sum(
            (probabilities.get(bucket, 0.0) - (1.0 if bucket == actual else 0.0))
            ** 2
            for bucket in categories
        )
        delay_weight += count

    target_correct = 0
    target_weight = 0
    dead_mode_mass = 0.0
    for signature, count in observations["target"].items():
        item = _mapping(json.loads(signature), "target signature")
        context = _select_context(model, model.mark_counts, item["contexts"])
        if context is None:
            missing_support["target"] += count
            continue
        token = item["token"]
        counts = model.target_counts.get((context, token), Counter())
        if not counts:
            missing_support["target"] += count
            continue
        prediction = min(counts, key=lambda key: (-counts[key], repr(key)))
        target_correct += count * (prediction == item["target_mode"])
        target_weight += count
        dead_mode_mass += count * counts.get(
            "DEAD_TARGET_OBSERVED_DIAGNOSTIC", 0
        ) / sum(counts.values())

    damage_log_mae = 0.0
    damage_weight = 0
    for signature, count in observations["positive_damage"].items():
        item = _mapping(json.loads(signature), "damage signature")
        context = _select_context(model, model.mark_counts, item["contexts"])
        if context is None:
            missing_support["positive_damage"] += count
            continue
        counts = model.damage_counts.get((context, item["token"]), Counter())
        positive_counts = Counter(
            {bucket: amount for bucket, amount in counts.items() if bucket > 0}
        )
        if not positive_counts:
            missing_support["positive_damage"] += count
            continue
        actual = _integer(item["bucket"], "damage bucket", nonnegative=True)
        actual_log = math.log1p(_bucket_midpoint_damage(actual))
        total = sum(positive_counts.values())
        damage_log_mae += count * sum(
            amount
            / total
            * abs(math.log1p(_bucket_midpoint_damage(bucket)) - actual_log)
            for bucket, amount in positive_counts.items()
        )
        damage_weight += count

    return {
        "schema": f"{RESULT_SCHEMA}/metrics",
        "variant_id": model.variant_id,
        "status": "DEVELOPMENT_METRICS_INCOMPLETE_NO_ADOPTION",
        "frozen_metric_definitions": {
            "evaluation_population": (
                "validation rows with training support; unsupported rows are "
                "reported separately and excluded from metric denominators"
            ),
            "next_mark_negative_log_likelihood": (
                "add-one categorical NLL at causal backoff context with one "
                "collapsed unseen-token bucket"
            ),
            "inter_event_log_mae": "distributional expected absolute log1p midpoint error",
            "inter_event_bucket_calibration": "multiclass Brier score",
            "target_mode_accuracy": "deterministic empirical-mode accuracy",
            "generated_dead_target_rate": "live runtime structural rate; only alive targets resolve",
            "positive_damage_log_mae": "distributional expected absolute log1p midpoint error",
            "dynamic_rollout_team_kill_clock_calibration": (
                "requires exact source-bound generated/historical rollout pairs; "
                "not materialized by this count-table reducer"
            ),
        },
        "metrics": {
            "next_mark_negative_log_likelihood": (
                mark_nll / mark_weight if mark_weight else None
            ),
            "inter_event_log_mae": (
                delay_log_mae / delay_weight if delay_weight else None
            ),
            "inter_event_bucket_brier": (
                delay_brier / delay_weight if delay_weight else None
            ),
            "target_mode_accuracy": (
                target_correct / target_weight if target_weight else None
            ),
            "predicted_dead_target_diagnostic_mode_mass": (
                dead_mode_mass / target_weight if target_weight else None
            ),
            "generated_dead_target_rate": 0.0,
            "positive_damage_log_mae": (
                damage_log_mae / damage_weight if damage_weight else None
            ),
            "dynamic_rollout_team_kill_clock_log_mae": None,
        },
        "weights": {
            "mark": mark_weight,
            "delay": delay_weight,
            "target": target_weight,
            "positive_damage": damage_weight,
            "dynamic_kill_clock": 0,
        },
        "missing_training_support": dict(sorted(missing_support.items())),
        "model_adoption_authorized": False,
    }


def _reduce_development_impl_v1(
    *,
    dispatch_path: str | Path,
    shared_root: str | Path,
    validate_dispatch_ambient: bool,
    reducer_replay_contract: Mapping[str, Any] | None = None,
    reference_content_sha256: str | None = None,
    output_stem: str = "development-validation",
) -> dict[str, Any]:
    reduce_started = time.perf_counter()
    dispatch, _ = _read_canonical_json(
        Path(dispatch_path).expanduser().resolve(), "dispatch"
    )
    dispatch_sha = _verify_content_address(dispatch, "dispatch")
    root = Path(shared_root).expanduser().resolve()
    source_contract, runtime_contract = _validate_dispatch_source_runtime_contract(
        dispatch, shared_root=root, validate_ambient=validate_dispatch_ambient
    )
    if not validate_dispatch_ambient:
        # Replay runs under a newer reducer closure, but the frozen worker
        # producer named by the dispatch must still exist byte-for-byte.
        _validate_materialized_source_runtime(root, source_contract, runtime_contract)
    tasks = _task_index(dispatch)
    worker_root = _resolve(root, dispatch.get("output_root_locator"), "output root") / "workers"
    stable_ids = {
        path.name[: -len(".json.gz")]
        for path in worker_root.glob("*.json.gz")
        if path.name.count(".") == 2
    }
    if stable_ids != set(tasks):
        raise TeammateResponseHpcV1Error(
            "worker receipt set is incomplete or unexpected"
        )
    train_joint = _JointTrainingCountsV3()
    validation_observations = {
        variant: {head: Counter() for head in OBSERVATION_HEADS}
        for variant in DYNAMIC_VARIANTS
    }
    descriptors = {"TRAIN": [], "VALIDATION": []}
    seen_waves: set[tuple[str, str, int, str]] = set()
    scan_count = 0
    total_rows = 0
    total_stream_counts: Counter[str] = Counter()
    for instance_id in sorted(tasks):
        path = worker_root / f"{instance_id}.json.gz"
        receipt = _read_gzip_document_low_copy(path, f"worker {instance_id}")
        receipt_sha = _verify_content_address(receipt, f"worker {instance_id}")
        addressed = worker_root / f"{instance_id}.{receipt_sha}.json.gz"
        if not addressed.is_file() or not _files_equal(path, addressed):
            raise TeammateResponseHpcV1Error(
                "worker stable/addressed outputs differ"
            )
        task = tasks[instance_id]
        expected_source = _expected_worker_source_binding(dispatch, task)
        if (
            receipt.get("schema") != WORKER_SCHEMA
            or receipt.get("revision") != REVISION
            or receipt.get("dispatch_content_sha256") != dispatch_sha
            or receipt.get("instance_id") != instance_id
            or receipt.get("node") != task.get("node")
            or receipt.get("split") != task.get("split")
            or receipt.get("component_id") != task.get("component_id")
            or receipt.get("source_binding") != expected_source
            or receipt.get("producer_contract")
            != {
                "implementation_source": source_contract,
                "remote_runtime": runtime_contract,
            }
            or receipt.get("scientific_boundary", {}).get(
                "model_adoption_authorized"
            )
            is not False
        ):
            raise TeammateResponseHpcV1Error("worker identity or boundary differs")
        scan = _mapping(receipt.get("single_scan_receipt"), "single scan receipt")
        if (
            scan.get("partition_open_count") != 1
            or scan.get("partition_scan_count") != 1
            or scan.get("dynamic_sufficient_statistics_built_together")
            != JOINT_BUILD_COMPONENTS
        ):
            raise TeammateResponseHpcV1Error("worker did not use one shared scan")
        scan_count += 1
        compressed_bytes = _integer(
            scan.get("compressed_bytes"), "compressed bytes", nonnegative=True
        )
        logical_bytes = _integer(
            scan.get("logical_bytes"), "logical bytes", nonnegative=True
        )
        record_count = _integer(
            scan.get("record_count"), "record count", nonnegative=True
        )
        worker_compiled_rows = _integer(
            scan.get("compiled_exact_player_row_count"),
            "compiled row count",
            nonnegative=True,
        )
        exact_player_events = _integer(
            scan.get("exact_player_event_count"),
            "exact-player event count",
            nonnegative=True,
        )
        unattributed_events = _integer(
            scan.get("unattributed_event_count"),
            "unattributed event count",
            nonnegative=True,
        )
        death_markers = _integer(
            scan.get("death_marker_count"),
            "death marker count",
            nonnegative=True,
        )
        negative_diagnostics = _integer(
            scan.get("negative_damage_diagnostic_count"),
            "negative diagnostic count",
            nonnegative=True,
        )
        classification_contexts = _integer(
            scan.get("classification_context_count"),
            "classification context count",
            nonnegative=True,
        )
        exact_trace_count = _integer(
            scan.get("exact_trace_count"),
            "exact trace count",
            nonnegative=True,
        )
        stage5_total_events = _integer(
            scan.get("stage5_total_event_count"),
            "Stage-5 total event count",
            nonnegative=True,
        )
        expected_summary = _mapping(task.get("expected_summary"), "task summary")
        expected_partition = _mapping(task.get("partition"), "task partition")
        if (
            compressed_bytes != expected_partition.get("compressed_size_bytes")
            or logical_bytes != expected_partition.get("logical_size_bytes")
            or record_count != expected_partition.get("record_count")
            or record_count != expected_summary.get("wave_count")
            or worker_compiled_rows != exact_player_events
            or stage5_total_events
            != exact_player_events
            + unattributed_events
            + death_markers
            + negative_diagnostics
            or exact_trace_count != stage5_total_events + classification_contexts
            or stage5_total_events != expected_summary.get("exact_event_count")
            or exact_trace_count != expected_summary.get("exact_trace_count")
            or death_markers != expected_summary.get("death_marker_count")
            or negative_diagnostics
            != expected_summary.get("negative_damage_diagnostic_count")
            or classification_contexts
            != expected_summary.get("classification_context_count")
        ):
            raise TeammateResponseHpcV1Error(
                "worker stream accounting or event categories differ from Stage-5"
            )
        total_stream_counts.update(
            {
                "compressed_bytes": compressed_bytes,
                "logical_bytes": logical_bytes,
                "wave_count": record_count,
                "exact_player_event_count": exact_player_events,
                "unattributed_event_count": unattributed_events,
                "death_marker_count": death_markers,
                "negative_damage_diagnostic_count": negative_diagnostics,
                "classification_context_count": classification_contexts,
                "exact_trace_count": exact_trace_count,
                "stage5_total_event_count": stage5_total_events,
            }
        )
        total_rows += worker_compiled_rows
        worker_descriptors = _array(
            receipt.get("arm_a_fixed_schedule_descriptors"), "arm A descriptors"
        )
        if (
            receipt.get("arm_a_execution_status")
            != "DESCRIPTOR_ONLY_UNEXECUTED"
            or len(worker_descriptors) != scan.get("record_count")
        ):
            raise TeammateResponseHpcV1Error(
                "arm A descriptor-only status or wave count differs"
            )
        for raw in worker_descriptors:
            descriptor = dict(_mapping(raw, "arm A descriptor"))
            wave = _mapping(descriptor.get("wave"), "descriptor wave")
            key = (
                _text(wave.get("instance_id"), "wave instance"),
                _text(wave.get("encounter_id"), "wave encounter"),
                _integer(wave.get("wave_ordinal"), "wave ordinal", nonnegative=True),
                _text(wave.get("wave_id"), "wave id"),
            )
            if key in seen_waves or key[0] != instance_id:
                raise TeammateResponseHpcV1Error("duplicate or cross-instance wave")
            seen_waves.add(key)
            if (
                descriptor.get("historical_eventmeta_schedule")
                != "REPLAY_UNCHANGED_FROM_BOUND_SOURCE"
                or descriptor.get("full_trace_copied") is not False
            ):
                raise TeammateResponseHpcV1Error("arm A descriptor changed schedule")
            descriptors[task["split"]].append(descriptor)
        joint_document = receipt.get("joint_dynamic_training_counts")
        compact_document = receipt.get("compact_evaluation_observations")
        if task["split"] == "TRAIN":
            if compact_document is not None:
                raise TeammateResponseHpcV1Error(
                    "training worker retained validation observations"
                )
            _merge_serialized_joint_training_v3(
                train_joint,
                _mapping(joint_document, "worker joint training counts"),
                expected_row_count=worker_compiled_rows,
            )
        else:
            if joint_document is not None:
                raise TeammateResponseHpcV1Error(
                    "validation worker materialized training model tables"
                )
            compact = _mapping(
                compact_document, "compact evaluation observations"
            )
            if set(compact) != set(DYNAMIC_VARIANTS):
                raise TeammateResponseHpcV1Error(
                    "worker observation variant set differs"
                )
            for variant in DYNAMIC_VARIANTS:
                _merge_serialized_observations_v3(
                    validation_observations[variant],
                    _mapping(compact[variant], variant),
                    expected_row_count=worker_compiled_rows,
                )
        # Release this receipt before the next gzip/JSON tree is parsed.  Python
        # evaluates the next assignment's right-hand side first, so waiting for
        # reassignment would transiently retain two complete worker documents.
        receipt = None
        worker_descriptors = None
        compact_document = None
        joint_document = None
        if task["split"] == "VALIDATION":
            compact = None
    # Loop locals otherwise retain the final receipt's compact validation tree
    # until the large result has also been materialized.
    receipt = None
    worker_descriptors = None
    compact_document = None
    joint_document = None
    for fold in descriptors:
        descriptors[fold].sort(
            key=lambda row: (
                row["wave"]["instance_id"],
                row["wave"]["encounter_ordinal"],
                row["wave"]["wave_ordinal"],
                row["wave"]["wave_id"],
            )
        )
    evaluations = {}
    for variant in DYNAMIC_VARIANTS:
        evaluations[variant] = _evaluate_joint_variant_v3(
            train_joint, variant, validation_observations[variant]
        )
    validation_observations = None
    seen_waves = None
    serialized_train_joint = serialize_joint_training_v3(train_joint, consume=True)
    train_joint = None
    core = {
        "schema": RESULT_SCHEMA,
        "revision": REVISION,
        "status": "DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION",
        "dispatch_content_sha256": dispatch_sha,
        "producer_contract": {
            "implementation_source": source_contract,
            "remote_runtime": runtime_contract,
        },
        "source_stage5_content_sha256": dispatch["source_bindings"]["stage5"][
            "content_sha256"
        ],
        "completeness": {
            "expected_worker_count": len(tasks),
            "observed_worker_count": scan_count,
            "partition_scan_count": scan_count,
            "duplicate_wave_count": 0,
            "compiled_exact_player_row_count": total_rows,
            "stream_totals": dict(sorted(total_stream_counts.items())),
            "validation_component_ids": dispatch["split_contract"][
                "validation_component_ids"
            ],
            "validation_component_count": dispatch["split_contract"][
                "validation_component_count"
            ],
            "old50_stage5_overlap_split": "TRAIN_NONHELDOUT",
            "old50_absent_from_stage5_not_tasked": True,
        },
        "arm_a_fixed_schedule_descriptor_only_unexecuted": {
            "variant": response_v1.ablation_variant_v1(response_v1.ABLATION_A),
            "execution_status": "DESCRIPTOR_ONLY_UNEXECUTED",
            "train_descriptors": descriptors["TRAIN"],
            "validation_descriptors": descriptors["VALIDATION"],
            "full_event_rows_stored": 0,
            "evaluation_metrics_materialized": False,
        },
        "train_joint_sufficient_statistics": serialized_train_joint,
        "train_model_views": {
            response_v1.ABLATION_B: {
                "materialization": "PROJECT_C_DROP_GUID_AND_SOURCE_GUID",
                "exact": True,
            },
            response_v1.ABLATION_C: {
                "materialization": "BASE_C",
                "exact": True,
            },
            response_v1.ABLATION_D: {
                "materialization": "C_CLASS_GLOBAL_PLUS_D_GUID_CLASS_SPEC_DELTA",
                "shared_spell_and_source_guid_tables_from_c": True,
                "exact": True,
            },
        },
        "development_validation": evaluations,
        "model_adoption": {
            "authorized": False,
            "failure_authorizes_adoption": False,
            "reason": "development validation is noncomparison and dynamic kill-clock rollouts are separate",
        },
        "remaining_gate": {
            "status": "EXACT_SOURCE_BOUND_DYNAMIC_ROLLOUT_KILL_CLOCK_NOT_MATERIALIZED",
            "required_input": (
                "exact generated/historical rollout pairs bound to the same Stage-5 "
                "wave and runtime contract"
            ),
            "arbitrary_unbound_pair_input_allowed": False,
        },
        "scientific_boundary": {
            "development_validation_only": True,
            "old50_stage5_overlap_nonheldout": True,
            "old50_absent_from_stage5_not_tasked": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
            "heavy_training_started": True,
            "heavy_training_complete": True,
        },
    }
    core_ready_at = time.perf_counter()
    equivalence_started = core_ready_at
    if reference_content_sha256 is not None:
        if _canonical_sha256(core) != reference_content_sha256:
            raise TeammateResponseHpcV1Error(
                "reducer replay full canonical core differs from reference"
            )
        if reducer_replay_contract is None:
            raise TeammateResponseHpcV1Error("reducer replay contract is absent")
        core["reducer_replay_contract"] = dict(reducer_replay_contract)
    equivalence_finished = time.perf_counter()
    result = _content_addressed(core)
    result_sha = result["content_address"]["sha256"]
    output = _resolve(root, dispatch.get("output_root_locator"), "output root")
    payload = _gzip_payload(result)
    addressed = output / f"{output_stem}.{result_sha}.json.gz"
    stable = output / f"{output_stem}.json.gz"
    _write_once(addressed, payload)
    status = _write_once(stable, payload)
    publish_finished = time.perf_counter()
    return {
        "status": status,
        "result_path": str(stable),
        "content_sha256": result_sha,
        "model_adoption_authorized": False,
        "phase_seconds": {
            "worker_reduce_and_core_materialization": core_ready_at
            - reduce_started,
            "reference_core_equivalence": equivalence_finished
            - equivalence_started,
            "content_address_gzip_and_publish": publish_finished
            - equivalence_finished,
            "internal_total": publish_finished - reduce_started,
        },
    }


def reduce_development_v1(
    *, dispatch_path: str | Path, shared_root: str | Path
) -> dict[str, Any]:
    """Reduce under the exact source/runtime closure bound by the dispatch."""

    return _reduce_development_impl_v1(
        dispatch_path=dispatch_path,
        shared_root=shared_root,
        validate_dispatch_ambient=True,
    )


def reduce_development_replay_v1(
    *,
    dispatch_path: str | Path,
    shared_root: str | Path,
    reducer_implementation_source: Mapping[str, Any],
    reducer_runtime: Mapping[str, Any],
    reference_result_path: str | Path,
    reference_content_sha256: str,
) -> dict[str, Any]:
    """Replay frozen worker receipts under a separately identified reducer.

    The old dispatch source remains the worker producer of record.  The current
    reducer closure is independently validated and recorded.  The frozen old
    addressed result names its complete canonical core digest; publication is
    conditional on the newly reduced pre-provenance core having that same digest.
    """

    replay_started = time.perf_counter()
    root = Path(shared_root).expanduser().resolve()
    reducer_source = _implementation_source_contract(reducer_implementation_source)
    reducer_runtime_contract = _remote_runtime_contract(
        reducer_runtime,
        source_release_locator=reducer_source["release_locator"],
    )
    _validate_ambient_source_runtime(
        root, reducer_source, reducer_runtime_contract
    )
    dispatch, _ = _read_canonical_json(
        Path(dispatch_path).expanduser().resolve(), "dispatch"
    )
    _verify_content_address(dispatch, "dispatch")
    _validate_dispatch_source_runtime_contract(
        dispatch, shared_root=root, validate_ambient=False
    )
    output = _resolve(root, dispatch.get("output_root_locator"), "output root")
    reference_path = Path(reference_result_path).expanduser().resolve()
    reference_sha = _sha256_text(
        reference_content_sha256, "reference result content sha256"
    )
    match = re.fullmatch(
        r"development-validation\.([0-9a-f]{64})\.json\.gz",
        reference_path.name,
    )
    if (
        not reference_path.is_file()
        or reference_path.parent != output.resolve()
        or match is None
        or match.group(1) != reference_sha
    ):
        raise TeammateResponseHpcV1Error(
            "reference result is not the addressed output named by its frozen sha256"
        )
    stable_reference = output / "development-validation.json.gz"
    if (
        not stable_reference.is_file()
        or not _files_equal(stable_reference, reference_path)
    ):
        raise TeammateResponseHpcV1Error(
            "reference stable/addressed outputs differ"
        )
    preflight_finished = time.perf_counter()
    replay_contract = {
        "schema": REDUCER_REPLAY_SCHEMA,
        "mode": "FROZEN_WORKER_RECEIPT_REPLAY",
        "implementation_source": reducer_source,
        "remote_runtime": reducer_runtime_contract,
        "reference_result_content_sha256": reference_sha,
        "scientific_payload_equivalence": "FULL_CANONICAL_CORE_SHA256_EQUAL",
    }
    reduced = _reduce_development_impl_v1(
        dispatch_path=dispatch_path,
        shared_root=root,
        validate_dispatch_ambient=False,
        reducer_replay_contract=replay_contract,
        reference_content_sha256=reference_sha,
        output_stem=(
            "development-validation-replay-"
            + reducer_source["source_archive_sha256"]
        ),
    )
    replay_finished = time.perf_counter()
    reduced["phase_seconds"] = {
        "replay_preflight": preflight_finished - replay_started,
        **reduced["phase_seconds"],
        "end_to_end": replay_finished - replay_started,
    }
    return reduced


def _load_json_argument(path: str | Path, label: str) -> dict[str, Any]:
    value, _ = _read_canonical_json(Path(path).expanduser().resolve(), label)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--stage5-manifest", required=True)
    plan_parser.add_argument("--old50-overlay", required=True)
    plan_parser.add_argument("--split", required=True)
    plan_parser.add_argument("--response-plan", required=True)
    plan_parser.add_argument("--implementation-source", required=True)
    plan_parser.add_argument("--remote-runtime", required=True)
    plan_parser.add_argument("--shared-root", required=True)
    plan_parser.add_argument("--output-root-locator", required=True)
    plan_parser.add_argument("--dispatch-directory", required=True)
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--dispatch", required=True)
    worker_parser.add_argument("--shared-root", required=True)
    worker_parser.add_argument("--instance-id", required=True)
    worker_parser.add_argument("--node", required=True)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--dispatch", required=True)
    reduce_parser.add_argument("--shared-root", required=True)
    replay_parser = subparsers.add_parser("replay-reduce")
    replay_parser.add_argument("--dispatch", required=True)
    replay_parser.add_argument("--shared-root", required=True)
    replay_parser.add_argument("--reducer-implementation-source", required=True)
    replay_parser.add_argument("--reducer-runtime", required=True)
    replay_parser.add_argument("--reference-result", required=True)
    replay_parser.add_argument("--reference-content-sha256", required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        dispatch = build_dispatch_v1(
            stage5_manifest_path=args.stage5_manifest,
            old50_overlay=_load_json_argument(args.old50_overlay, "old50 overlay"),
            split=_load_json_argument(args.split, "component split"),
            response_plan=_load_json_argument(args.response_plan, "response plan"),
            implementation_source=_load_json_argument(
                args.implementation_source, "implementation source"
            ),
            remote_runtime=_load_json_argument(
                args.remote_runtime, "remote runtime"
            ),
            shared_root=args.shared_root,
            output_root_locator=args.output_root_locator,
        )
        path = save_dispatch_v1(dispatch, args.dispatch_directory)
        print(json.dumps({"status": "PREPARED_NOT_LAUNCHED", "dispatch": str(path)}))
    elif args.command == "worker":
        print(
            json.dumps(
                run_worker_v1(
                    dispatch_path=args.dispatch,
                    shared_root=args.shared_root,
                    instance_id=args.instance_id,
                    node=args.node,
                )
            )
        )
    elif args.command == "reduce":
        print(
            json.dumps(
                reduce_development_v1(
                    dispatch_path=args.dispatch, shared_root=args.shared_root
                )
            )
        )
    else:
        print(
            json.dumps(
                reduce_development_replay_v1(
                    dispatch_path=args.dispatch,
                    shared_root=args.shared_root,
                    reducer_implementation_source=_load_json_argument(
                        args.reducer_implementation_source,
                        "reducer implementation source",
                    ),
                    reducer_runtime=_load_json_argument(
                        args.reducer_runtime, "reducer runtime"
                    ),
                    reference_result_path=args.reference_result,
                    reference_content_sha256=args.reference_content_sha256,
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
