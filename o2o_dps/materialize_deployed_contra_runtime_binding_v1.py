"""Write one validated deployed-Contra runtime binding from pinned local inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deployed_contra_runtime_binding_v1 import (
    build_deployed_contra_runtime_binding_v1,
    load_deployed_contra_runtime_binding_v1,
    load_runtime_snapshot,
    validate_deployed_contra_runtime_binding_v1,
)
from .deployed_contra_source_manifest_v1 import (
    DEFAULT_ARCHIVE,
    DEFAULT_SOURCE_ROOT,
    build_deployed_contra_source_manifest_v1,
    verify_deployed_contra_archive_equivalence_v1,
)


def materialize_runtime_binding_v1(
    *,
    snapshot_path: str | Path,
    output_path: str | Path,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    archive_path: str | Path = DEFAULT_ARCHIVE,
) -> dict:
    manifest = build_deployed_contra_source_manifest_v1(source_root)
    verify_deployed_contra_archive_equivalence_v1(manifest, archive_path)
    snapshot = load_runtime_snapshot(snapshot_path)
    binding = validate_deployed_contra_runtime_binding_v1(
        build_deployed_contra_runtime_binding_v1(
            source_manifest=manifest,
            runtime_snapshot=snapshot,
        )
    )
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        output.write(json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    if load_deployed_contra_runtime_binding_v1(destination) != binding:
        raise RuntimeError("serialized deployed-Contra runtime binding changed")
    return binding


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    args = parser.parse_args()
    binding = materialize_runtime_binding_v1(
        snapshot_path=args.snapshot,
        output_path=args.output,
        source_root=args.source_root,
        archive_path=args.archive,
    )
    print(binding["binding_sha256"])


if __name__ == "__main__":
    main()
