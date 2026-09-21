"""Derive separate first/follow-up delay heads from a frozen pooled SQLite store.

The old CLASS head contains exactly one observation per event and carries the
pre-event last mark.  ``__NONE__`` therefore identifies wave-start delays;
all other marks identify delays since the previous actor event.  No Stage-5
rows or held-out wave are read by this transformation.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .responsive_team_runtime_store_v1 import (
    STORE_REVISION,
    STORE_SCHEMA,
    ResponsiveTeamRuntimeStoreV1Error,
    _canonical_text,
)


OLD_REVISION = "sqlite_indexed_joint_target_damage_model_v2"
DERIVATION = "exact_class_last_mark_delay_origin_partition_v1"
_ORIGINS = ("WAVE_START", "PREVIOUS_ACTOR_EVENT")
_LAST_MARK_INDEX = {"GUID": 2, "CLASS_SPEC": 3, "CLASS": 2}


def _key(value: Any) -> str:
    return _canonical_text(value)


def _choices(connection: sqlite3.Connection, distribution_id: int) -> Counter[int]:
    counts: Counter[int] = Counter()
    for value_json, count in connection.execute(
        "SELECT value_json, count FROM distribution_choice "
        "WHERE distribution_id = ? ORDER BY ordinal", (distribution_id,)
    ):
        bucket = json.loads(value_json)
        if isinstance(bucket, bool) or not isinstance(bucket, int) or count <= 0:
            raise ResponsiveTeamRuntimeStoreV1Error("old delay choices are invalid")
        counts[bucket] += count
    return counts


def _insert_distribution(
    connection: sqlite3.Connection, key: tuple[str, ...], counts: Counter[int]
) -> None:
    support = sum(counts.values())
    if support <= 0:
        raise ResponsiveTeamRuntimeStoreV1Error("derived delay head is empty")
    cursor = connection.execute(
        "INSERT INTO distribution_lookup (head, key_json, support) VALUES (?, ?, ?)",
        ("delay_counts", _key(key), support),
    )
    connection.executemany(
        "INSERT INTO distribution_choice "
        "(distribution_id, ordinal, value_json, count) VALUES (?, ?, ?, ?)",
        (
            (cursor.lastrowid, ordinal, _key(bucket), count)
            for ordinal, (bucket, count) in enumerate(sorted(counts.items()))
        ),
    )


def derive_delay_origin_runtime_store_v1(
    source: str | Path, destination: str | Path
) -> dict[str, Any]:
    """Copy an old store to a new path and deterministically split delay heads."""

    old_path = Path(source).expanduser().resolve()
    new_path = Path(destination).expanduser().resolve()
    if new_path.exists() or new_path == old_path:
        raise ResponsiveTeamRuntimeStoreV1Error("derived destination must be new")
    old = sqlite3.connect(old_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    descriptor: int | None = None
    temporary: Path | None = None
    new: sqlite3.Connection | None = None
    try:
        row = old.execute(
            "SELECT value_json FROM metadata WHERE key = 'manifest'"
        ).fetchone()
        if row is None:
            raise ResponsiveTeamRuntimeStoreV1Error("old store lacks manifest")
        manifest = json.loads(row[0])
        if manifest.get("schema") != STORE_SCHEMA or manifest.get("revision") != OLD_REVISION:
            raise ResponsiveTeamRuntimeStoreV1Error("source is not the frozen pooled-delay store")
        row_count = manifest["row_count"]
        new_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=new_path.name + ".", suffix=".deriving", dir=new_path.parent
        )
        os.close(descriptor)
        descriptor = None
        temporary = Path(name)
        new = sqlite3.connect(temporary)
        old.backup(new)
        with new:
            new.execute(
                "CREATE TEMP TABLE old_delay AS SELECT distribution_id, key_json, support "
                "FROM distribution_lookup WHERE head = 'delay_counts'"
            )
            class_by_origin = {origin: Counter() for origin in _ORIGINS}
            class_total: Counter[int] = Counter()
            global_old: Counter[int] | None = None
            for distribution_id, key_json, support in new.execute(
                "SELECT distribution_id, key_json, support FROM old_delay"
            ):
                key = json.loads(key_json)
                counts = _choices(new, distribution_id)
                if sum(counts.values()) != support:
                    raise ResponsiveTeamRuntimeStoreV1Error("old delay support differs")
                if not isinstance(key, list) or not key:
                    raise ResponsiveTeamRuntimeStoreV1Error("old delay context is invalid")
                level = key[0]
                if level == "GLOBAL":
                    if key != ["GLOBAL"] or global_old is not None:
                        raise ResponsiveTeamRuntimeStoreV1Error("old GLOBAL delay is ambiguous")
                    global_old = counts
                    continue
                index = _LAST_MARK_INDEX.get(level)
                if index is None or len(key) <= index or not isinstance(key[index], str):
                    raise ResponsiveTeamRuntimeStoreV1Error("old delay last-mark context is invalid")
                origin = _ORIGINS[0] if key[index] == "__NONE__" else _ORIGINS[1]
                if level == "CLASS":
                    class_by_origin[origin].update(counts)
                    class_total.update(counts)
                _insert_distribution(new, (level, origin, *key[1:]), counts)
            if global_old is None or class_total != global_old:
                raise ResponsiveTeamRuntimeStoreV1Error(
                    "CLASS delay distribution does not reconstruct old GLOBAL"
                )
            if sum(global_old.values()) != row_count:
                raise ResponsiveTeamRuntimeStoreV1Error("old GLOBAL delay differs from row_count")
            for origin, counts in class_by_origin.items():
                if counts:
                    _insert_distribution(new, ("GLOBAL", origin), counts)
            new.execute(
                "DELETE FROM distribution_choice WHERE distribution_id IN "
                "(SELECT distribution_id FROM old_delay)"
            )
            new.execute(
                "DELETE FROM distribution_lookup WHERE distribution_id IN "
                "(SELECT distribution_id FROM old_delay)"
            )
            source_model_sha = manifest["model_content_sha256"]
            identity = {
                "source_model_content_sha256": source_model_sha,
                "derivation": DERIVATION,
            }
            manifest["model_content_sha256"] = hashlib.sha256(
                _key(identity).encode("utf-8")
            ).hexdigest()
            manifest["model_content_scope"] = "source model plus exact delay-origin derivation"
            manifest["revision"] = STORE_REVISION
            manifest["derivation"] = {
                **identity,
                "first_global_support": sum(class_by_origin[_ORIGINS[0]].values()),
                "followup_global_support": sum(class_by_origin[_ORIGINS[1]].values()),
                "class_reconstructs_old_global": True,
                "source_store_revision": OLD_REVISION,
            }
            manifest["distribution_count"] = new.execute(
                "SELECT COUNT(*) FROM distribution_lookup"
            ).fetchone()[0]
            manifest["choice_count"] = new.execute(
                "SELECT COUNT(*) FROM distribution_choice"
            ).fetchone()[0]
            manifest["head_counts"]["delay_counts"] = {
                "distribution_count": new.execute(
                    "SELECT COUNT(*) FROM distribution_lookup WHERE head = 'delay_counts'"
                ).fetchone()[0],
                "choice_count": new.execute(
                    "SELECT COUNT(*) FROM distribution_choice AS choice "
                    "JOIN distribution_lookup AS lookup USING (distribution_id) "
                    "WHERE lookup.head = 'delay_counts'"
                ).fetchone()[0],
            }
            new.execute(
                "UPDATE metadata SET value_json = ? WHERE key = 'manifest'",
                (_key(manifest),),
            )
        new.close()
        new = None
        temporary.replace(new_path)
        return manifest
    finally:
        old.close()
        if new is not None:
            new.close()
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    manifest = derive_delay_origin_runtime_store_v1(args.source, args.destination)
    print(_key({
        "destination": str(Path(args.destination).resolve()),
        "model_content_sha256": manifest["model_content_sha256"],
        "derivation": manifest["derivation"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
