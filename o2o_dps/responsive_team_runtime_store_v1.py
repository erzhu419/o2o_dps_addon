"""Disk-backed runtime view of one learned teammate-response model.

The authoritative HPC result is intentionally large: it contains the joint
B/C/D sufficient statistics, validation descriptors, and provenance needed to
reconstruct every development view.  Rollout workers do not need that result.
This module converts one already-validated selected model into a normalized
SQLite store once, then samples only the few context distributions needed by a
live actor.  Independent worker processes therefore share the host page cache
instead of each materializing the reducer JSON and Python counter tables.

Building this store does not change the reducer's development-only boundary or
prove that a current source is held out.  The current Stage-5 digest and
component are checked again whenever the store is opened.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import sqlite3
import tempfile
from typing import Any, Mapping

from . import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from . import chronicle_external_teammate_response_model_v1 as response_v1
from .responsive_team_bridge_adapter_v1 import TeammateModelProvenanceV1
from .responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
    LoadedResponsiveTeammateModelV1,
    load_responsive_teammate_model_from_hpc_path_v1,
)


STORE_SCHEMA = "o2o_responsive_team_runtime_store/v1"
STORE_REVISION = "sqlite_indexed_selected_model_v1"
_STORE_FORMAT = "SQLITE_NORMALIZED_DISTRIBUTIONS_READ_ONLY"
_HEADS = tuple(hpc_v1.TABLE_FIELDS)


class ResponsiveTeamRuntimeStoreV1Error(RuntimeError):
    """The selected model cannot be stored or sampled under this contract."""


def _canonical_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ResponsiveTeamRuntimeStoreV1Error(
        f"runtime model contains an unsupported value type {type(value).__name__}"
    )


def _lookup_key(value: Any) -> str:
    return _canonical_text(_jsonable(value))


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponsiveTeamRuntimeStoreV1Error(f"{label} must be an object")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsiveTeamRuntimeStoreV1Error(f"{label} must be nonempty text")
    return value


def _require_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResponsiveTeamRuntimeStoreV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _validate_build_input(
    loaded: LoadedResponsiveTeammateModelV1,
) -> tuple[Any, TeammateModelProvenanceV1, str, tuple[str, ...]]:
    model = loaded.model
    provenance = loaded.provenance
    if not isinstance(provenance, TeammateModelProvenanceV1):
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store requires loader-validated teammate provenance"
        )
    if provenance.source_artifact_schema != hpc_v1.RESULT_SCHEMA:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store source is not the teammate-response HPC result"
        )
    if getattr(model, "variant_id", None) != provenance.variant_id:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "selected model variant differs from its provenance"
        )
    if provenance.variant_id not in hpc_v1.DYNAMIC_VARIANTS:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store requires a learned B/C/D model"
        )
    evidence = _require_mapping(
        loaded.current_source_evidence, "current source evidence"
    )
    result_stage5_sha = _require_text(
        evidence.get("result_source_stage5_content_sha256"),
        "result source Stage-5 content SHA-256",
    )
    raw_components = evidence.get("validation_component_ids")
    if not isinstance(raw_components, list):
        raise ResponsiveTeamRuntimeStoreV1Error(
            "validation component ids must be an array"
        )
    validation_components = tuple(
        _require_text(value, "validation component_id") for value in raw_components
    )
    if (
        not validation_components
        or tuple(sorted(set(validation_components))) != validation_components
    ):
        raise ResponsiveTeamRuntimeStoreV1Error(
            "validation component ids must be nonempty, unique, and sorted"
        )
    same_source = (
        evidence.get("current_source_stage5_content_sha256") == result_stage5_sha
    )
    source_component = evidence.get("current_source_component_id")
    proven_held_out = same_source and source_component in validation_components
    if (
        evidence.get("current_source_held_out") is not proven_held_out
        or provenance.current_source_held_out is not proven_held_out
    ):
        raise ResponsiveTeamRuntimeStoreV1Error(
            "loader provenance and held-out evidence differ"
        )
    if loaded.result_content_sha256 != provenance.source_artifact_content_sha256:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "loader result identity differs from model provenance"
        )
    return model, provenance, result_stage5_sha, validation_components


def build_responsive_team_runtime_store_v1(
    destination: str | Path,
    loaded: LoadedResponsiveTeammateModelV1,
) -> Mapping[str, Any]:
    """Write one selected model to a new immutable-style SQLite artifact.

    The expensive reducer JSON is expected to have been loaded exactly once by
    :mod:`responsive_team_hpc_result_loader_v1`.  This function walks the
    selected in-memory model without creating another serialized model tree.
    """

    model, provenance, result_stage5_sha, validation_components = (
        _validate_build_input(loaded)
    )
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store destination already exists"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".building", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE distribution_lookup (
                distribution_id INTEGER PRIMARY KEY,
                head TEXT NOT NULL,
                key_json TEXT NOT NULL,
                support INTEGER NOT NULL CHECK (support > 0),
                UNIQUE (head, key_json)
            );
            CREATE TABLE distribution_choice (
                distribution_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                value_json TEXT NOT NULL,
                count INTEGER NOT NULL CHECK (count > 0),
                PRIMARY KEY (distribution_id, ordinal)
            ) WITHOUT ROWID;
            """
        )
        distribution_id = 0
        choice_count = 0
        head_counts: dict[str, dict[str, int]] = {}
        with connection:
            for head in _HEADS:
                table = getattr(model, head, None)
                if not isinstance(table, Mapping):
                    raise ResponsiveTeamRuntimeStoreV1Error(
                        f"selected model table {head} is absent"
                    )
                head_distribution_count = 0
                head_choice_count = 0
                for key in sorted(table, key=repr):
                    counts = table[key]
                    ordered = sorted(counts.items(), key=lambda item: repr(item[0]))
                    support = sum(count for _, count in ordered)
                    if support <= 0 or any(
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count <= 0
                        for _, count in ordered
                    ):
                        raise ResponsiveTeamRuntimeStoreV1Error(
                            f"selected model table {head} has invalid counts"
                        )
                    distribution_id += 1
                    connection.execute(
                        "INSERT INTO distribution_lookup "
                        "(distribution_id, head, key_json, support) VALUES (?, ?, ?, ?)",
                        (distribution_id, head, _lookup_key(key), support),
                    )
                    connection.executemany(
                        "INSERT INTO distribution_choice "
                        "(distribution_id, ordinal, value_json, count) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            (
                                distribution_id,
                                ordinal,
                                _canonical_text(_jsonable(value)),
                                count,
                            )
                            for ordinal, (value, count) in enumerate(ordered)
                        ),
                    )
                    head_distribution_count += 1
                    head_choice_count += len(ordered)
                    choice_count += len(ordered)
                head_counts[head] = {
                    "distribution_count": head_distribution_count,
                    "choice_count": head_choice_count,
                }

            row_count = _require_integer(
                getattr(model, "row_count", None), "selected model row_count", minimum=1
            )
            global_mark_support = sum(
                getattr(model, "mark_counts").get(("GLOBAL",), {}).values()
            )
            if global_mark_support != row_count:
                raise ResponsiveTeamRuntimeStoreV1Error(
                    "selected model global mark support differs from row_count"
                )
            minimums = dict(
                _require_mapping(getattr(model, "minimums", None), "model minimums")
            )
            manifest = {
                "schema": STORE_SCHEMA,
                "revision": STORE_REVISION,
                "store_format": _STORE_FORMAT,
                "source_artifact_schema": provenance.source_artifact_schema,
                "source_artifact_content_sha256": provenance.source_artifact_content_sha256,
                "model_content_sha256": provenance.model_content_sha256,
                "model_content_scope": (
                    "canonical selected serialized B/C/D runtime model"
                ),
                "variant_id": provenance.variant_id,
                "training_scope": provenance.training_scope,
                "source_stage5_content_sha256": result_stage5_sha,
                "validation_component_ids": list(validation_components),
                "minimums": minimums,
                "row_count": row_count,
                "distribution_count": distribution_id,
                "choice_count": choice_count,
                "head_counts": head_counts,
                "runtime_contract": {
                    "reducer_result_loaded_at_store_build_only": True,
                    "rollout_worker_loads_reducer_json": False,
                    "per_context_indexed_reads": True,
                    "one_read_only_connection_per_long_lived_worker": True,
                    "host_page_cache_shareable_across_processes": True,
                },
                "scientific_boundary": {
                    "development_only": True,
                    "held_out_derived_per_current_source": True,
                    "store_build_proves_held_out": False,
                    "comparison_eligible": False,
                    "voting_eligible": False,
                    "deployment_eligible": False,
                },
            }
            connection.execute(
                "INSERT INTO metadata (key, value_json) VALUES (?, ?)",
                ("manifest", _canonical_text(manifest)),
            )
        connection.close()
        connection = None
        temporary.replace(target)
        return manifest
    except Exception:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _read_manifest(connection: sqlite3.Connection) -> Mapping[str, Any]:
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key = 'manifest'"
    ).fetchone()
    if row is None:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store manifest is absent"
        )
    try:
        manifest = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store manifest is invalid JSON"
        ) from error
    document = _require_mapping(manifest, "runtime store manifest")
    boundary = _require_mapping(
        document.get("scientific_boundary"), "runtime store scientific boundary"
    )
    runtime = _require_mapping(
        document.get("runtime_contract"), "runtime store contract"
    )
    if (
        document.get("schema") != STORE_SCHEMA
        or document.get("revision") != STORE_REVISION
        or document.get("store_format") != _STORE_FORMAT
        or boundary.get("development_only") is not True
        or boundary.get("held_out_derived_per_current_source") is not True
        or boundary.get("store_build_proves_held_out") is not False
        or boundary.get("comparison_eligible") is not False
        or boundary.get("voting_eligible") is not False
        or boundary.get("deployment_eligible") is not False
        or runtime.get("rollout_worker_loads_reducer_json") is not False
        or runtime.get("per_context_indexed_reads") is not True
    ):
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store schema or scientific boundary differs"
        )
    return document


class SqliteResponsiveTeammateModelV1:
    """Read-only sampler with the learned model's public sampling surface."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        manifest: Mapping[str, Any],
    ) -> None:
        self._connection = connection
        self.variant_id = _require_text(manifest.get("variant_id"), "variant_id")
        self.model_content_sha256 = _require_text(
            manifest.get("model_content_sha256"), "model_content_sha256"
        )
        self.variant = response_v1.ablation_variant_v1(self.variant_id)
        self.minimums = dict(
            _require_mapping(manifest.get("minimums"), "model minimums")
        )
        self.row_count = _require_integer(
            manifest.get("row_count"), "model row_count", minimum=1
        )
        if set(self.minimums) != {"GUID", "CLASS_SPEC", "CLASS", "GLOBAL"}:
            raise ResponsiveTeamRuntimeStoreV1Error(
                "runtime store model minimum set differs"
            )
        for level, value in self.minimums.items():
            _require_integer(value, f"{level} minimum", minimum=1)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteResponsiveTeammateModelV1":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _distribution(
        self, head: str, key: Any
    ) -> tuple[int, tuple[tuple[Any, int], ...]] | None:
        row = self._connection.execute(
            "SELECT distribution_id, support FROM distribution_lookup "
            "WHERE head = ? AND key_json = ?",
            (head, _lookup_key(key)),
        ).fetchone()
        if row is None:
            return None
        distribution_id, support = row
        raw_choices = self._connection.execute(
            "SELECT value_json, count FROM distribution_choice "
            "WHERE distribution_id = ? ORDER BY ordinal",
            (distribution_id,),
        ).fetchall()
        choices = tuple((json.loads(value), count) for value, count in raw_choices)
        if sum(count for _, count in choices) != support:
            raise ResponsiveTeamRuntimeStoreV1Error(
                "runtime store distribution support differs"
            )
        return support, choices

    @staticmethod
    def _weighted_choice(
        distribution: tuple[int, tuple[tuple[Any, int], ...]], rng: random.Random
    ) -> Any:
        support, choices = distribution
        draw = rng.randrange(support)
        cumulative = 0
        for value, count in choices:
            cumulative += count
            if draw < cumulative:
                return value
        raise AssertionError("disk-backed weighted choice fell through")

    def _select_context(
        self,
        head: str,
        actor: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], tuple[int, tuple[tuple[Any, int], ...]]]:
        for context in response_v1._context_keys(actor, state, self.variant_id):
            distribution = self._distribution(head, context)
            if (
                distribution is not None
                and distribution[0] >= self.minimums[context[0]]
            ):
                return context, distribution
        raise ResponsiveTeamRuntimeStoreV1Error(
            "runtime store model has no global empirical support"
        )

    def _required_distribution(
        self, head: str, key: Any
    ) -> tuple[int, tuple[tuple[Any, int], ...]]:
        distribution = self._distribution(head, key)
        if distribution is None:
            raise ResponsiveTeamRuntimeStoreV1Error(
                f"runtime store has no {head} distribution for selected context"
            )
        return distribution

    def sample_delay(
        self,
        *,
        actor: Mapping[str, Any],
        timing_state: Mapping[str, Any],
        rng: random.Random,
    ) -> Mapping[str, Any]:
        context, distribution = self._select_context(
            "delay_counts", actor, timing_state
        )
        bucket = self._weighted_choice(distribution, rng)
        return {
            "delay_ms": response_v1._sample_delay_bucket(bucket, rng),
            "delay_bucket": bucket,
            "context_level": context[0],
            "context": list(context),
            "support": distribution[0],
        }

    def sample_emission(
        self,
        *,
        actor: Mapping[str, Any],
        emission_state: Mapping[str, Any],
        rng: random.Random,
    ) -> Mapping[str, Any]:
        context, mark_distribution = self._select_context(
            "mark_counts", actor, emission_state
        )
        token = self._weighted_choice(mark_distribution, rng)
        event_type, spell_id, attribution_kind = json.loads(token)
        target_mode = self._weighted_choice(
            self._required_distribution("target_counts", (context, token)), rng
        )
        damage_bucket = self._weighted_choice(
            self._required_distribution("damage_counts", (context, token)), rng
        )
        damage = (
            response_v1._sample_damage_bucket(damage_bucket, rng)
            if event_type == "DMG"
            else 0
        )
        actor_guid = _require_text(actor.get("player_guid"), "actor guid")
        source_guid = None
        if "GUID" in self.variant["context_levels"]:
            source_distribution = self._distribution(
                "source_guid_counts", (actor_guid, token)
            )
            if source_distribution is not None:
                source_guid = self._weighted_choice(source_distribution, rng)
        spell_name = self._weighted_choice(
            self._required_distribution("spell_name_counts", token), rng
        )
        return {
            "event_type": event_type,
            "spell_id": spell_id,
            "spell_name": spell_name,
            "attribution_kind": attribution_kind,
            "attributed_player_guid": actor_guid,
            "exact_source_guid": source_guid,
            "target_mode": target_mode,
            "sampled_damage": damage,
            "damage_bucket": damage_bucket,
            "context_level": context[0],
            "context": list(context),
            "support": mark_distribution[0],
        }


def open_responsive_team_runtime_store_v1(
    path: str | Path,
    *,
    expected_result_content_sha256: str,
    expected_model_content_sha256: str,
    variant_id: str,
    current_source: CurrentSourceDeclarationV1,
) -> LoadedResponsiveTeammateModelV1:
    """Open one selected model without loading the HPC reducer result."""

    source = Path(path).expanduser().resolve()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            source.as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA mmap_size=268435456")
        manifest = _read_manifest(connection)
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise ResponsiveTeamRuntimeStoreV1Error(
            f"cannot open responsive-team runtime store: {error}"
        ) from error
    except Exception:
        if connection is not None:
            connection.close()
        raise
    try:
        expected = {
            "source_artifact_content_sha256": expected_result_content_sha256,
            "model_content_sha256": expected_model_content_sha256,
            "variant_id": variant_id,
        }
        differing = [
            key for key, value in expected.items() if manifest.get(key) != value
        ]
        if differing:
            raise ResponsiveTeamRuntimeStoreV1Error(
                "runtime store identity differs: " + ", ".join(differing)
            )
        result_stage5_sha = _require_text(
            manifest.get("source_stage5_content_sha256"),
            "result source Stage-5 content SHA-256",
        )
        raw_components = manifest.get("validation_component_ids")
        if not isinstance(raw_components, list):
            raise ResponsiveTeamRuntimeStoreV1Error(
                "runtime store validation components must be an array"
            )
        validation_components = tuple(
            _require_text(value, "validation component_id")
            for value in raw_components
        )
        if tuple(sorted(set(validation_components))) != validation_components:
            raise ResponsiveTeamRuntimeStoreV1Error(
                "runtime store validation components differ"
            )
        same_source = current_source.stage5_content_sha256 == result_stage5_sha
        in_validation_component = (
            same_source and current_source.component_id in validation_components
        )
        if current_source.declared_held_out is not in_validation_component:
            raise ResponsiveTeamRuntimeStoreV1Error(
                "current source held-out declaration is not proven by the runtime store"
            )
        provenance = TeammateModelProvenanceV1(
            source_artifact_schema=_require_text(
                manifest.get("source_artifact_schema"), "source artifact schema"
            ),
            source_artifact_content_sha256=expected_result_content_sha256,
            model_content_sha256=expected_model_content_sha256,
            variant_id=variant_id,
            training_scope=_require_text(
                manifest.get("training_scope"), "training scope"
            ),
            current_source_held_out=in_validation_component,
        )
        model = SqliteResponsiveTeammateModelV1(connection, manifest)
        connection = None
        evidence = {
            "schema": f"{STORE_SCHEMA}/current_source_evidence",
            "result_source_stage5_content_sha256": result_stage5_sha,
            "current_source_stage5_content_sha256": current_source.stage5_content_sha256,
            "current_source_component_id": current_source.component_id,
            "validation_component_ids": list(validation_components),
            "same_stage5_source": same_source,
            "component_in_validation_split": in_validation_component,
            "current_source_held_out": in_validation_component,
            "evidence_status": (
                "BOUND_SOURCE_VALIDATION_COMPONENT"
                if in_validation_component
                else "NOT_PROVEN_HELD_OUT_BY_THIS_RESULT"
            ),
            "rollout_worker_loaded_reducer_json": False,
        }
        return LoadedResponsiveTeammateModelV1(
            model=model,
            provenance=provenance,
            result_content_sha256=expected_result_content_sha256,
            current_source_evidence=evidence,
        )
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Build one disk-backed responsive teammate runtime model"
    )
    parser.add_argument("--result", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--variant", required=True, choices=hpc_v1.DYNAMIC_VARIANTS)
    parser.add_argument("--current-stage5-sha256", required=True)
    parser.add_argument("--current-component-id", required=True)
    held_out = parser.add_mutually_exclusive_group(required=True)
    held_out.add_argument("--current-source-held-out", action="store_true")
    held_out.add_argument("--current-source-not-held-out", action="store_true")
    args = parser.parse_args()
    current_source = CurrentSourceDeclarationV1(
        stage5_content_sha256=args.current_stage5_sha256,
        component_id=args.current_component_id,
        declared_held_out=bool(args.current_source_held_out),
    )
    loaded = load_responsive_teammate_model_from_hpc_path_v1(
        args.result,
        variant_id=args.variant,
        current_source=current_source,
    )
    manifest = build_responsive_team_runtime_store_v1(args.output, loaded)
    print(_canonical_text(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "ResponsiveTeamRuntimeStoreV1Error",
    "STORE_REVISION",
    "STORE_SCHEMA",
    "SqliteResponsiveTeammateModelV1",
    "build_responsive_team_runtime_store_v1",
    "open_responsive_team_runtime_store_v1",
]
