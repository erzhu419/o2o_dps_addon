"""Plan or stage the compact finite-schedule search closure on shared CPU storage.

The default CLI is a local dry-run.  ``--execute`` is the only mode that
connects to node001--node006 or writes the shared filesystem.  The staged
closure contains Python/configuration, one explicit exact-cell manifest, one
explicit Linux bridge, compact derived/model inputs, and the wowsims item
database.  Raw Chronicle CSVs and checkpoints are never copied.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.wave_action_sequence_remote_contract_v1 import (
    DEVELOPMENT_CASE_BUILDER,
    MANIFEST_SCHEMA,
    MIN_SEEDS_PER_CELL,
    SHARD_COUNT,
    UPPER_KARA_CASE_BUILDER,
    assign_exact_cell_shards_v1,
    load_exact_cell_manifest_v1,
)
from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    _scheduler,
    _shared_home,
)


JSONMap = dict[str, Any]
RUN_FAMILY = "wave-action-sequence-v1"
MAX_COMPACT_INPUT_BYTES = 128 * 1024 * 1024
MAX_COMPACT_CLOSURE_BYTES = 256 * 1024 * 1024
ITEM_DATABASE = BRAIN_ROOT / "wowsims-turtle/assets/database/db.json"
DEPLOYED_CONTRA_RUNTIME_BINDING = (
    PROJECT_ROOT
    / ".hpc-local/smokes/cat-gap-three-baseline-v1"
    / "deployed-contra-runtime-binding-v1.951b8faa.json"
)
CONTRA260817_SOURCE_ROOT = BRAIN_ROOT / "Contra_new"
CONTRA260817_CODE_EXTENSIONS = (".lua", ".toc", ".xml")
OFFLINE_GUIDE_ARTIFACT = (
    PROJECT_ROOT
    / "offline_data/behavior_models/fury_partial_markov_v1/model.json"
)
UPPER_KARA_SCENARIO_CAPSULE = (
    PROJECT_ROOT
    / "offline_data/derived/fury_offline_scenario_capsules/v2"
    / (
        "fury_offline_scenario_capsules_v2."
        "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e."
        "json.gz"
    )
)
UPPER_KARA_SELECTOR_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data/derived/historical_representative_build_selector/v1"
)
UPPER_KARA_CATALOG_DIRECTORY = (
    PROJECT_ROOT / "offline_data/derived/historical_build_catalog/v1"
)
UPPER_KARA_COMPACT_INPUTS = (
    UPPER_KARA_SCENARIO_CAPSULE,
    UPPER_KARA_SELECTOR_DIRECTORY,
    UPPER_KARA_CATALOG_DIRECTORY,
)
DEFAULT_COMPACT_INPUTS = (
    OFFLINE_GUIDE_ARTIFACT,
    *UPPER_KARA_COMPACT_INPUTS,
)
_COMPACT_INPUT_ROOTS = (
    PROJECT_ROOT / "offline_data/behavior_models",
    PROJECT_ROOT / "offline_data/derived",
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _valid_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run ID")
    return run_id


def _validated_bridge(path: str | Path) -> Path:
    bridge = Path(path).expanduser().resolve(strict=True)
    if not bridge.is_file() or not bridge.name.endswith(".linux-amd64"):
        raise ValueError("bridge must be an explicit *.linux-amd64 file")
    return bridge


def _compact_inputs(values: Iterable[str | Path] | None) -> tuple[Path, ...]:
    sources = DEFAULT_COMPACT_INPUTS if values is None else tuple(Path(row) for row in values)
    result: list[Path] = []
    closure_bytes = 0
    for value in sources:
        path = value.expanduser().resolve(strict=True)
        if not any(
            path != root.resolve() and path.is_relative_to(root.resolve())
            for root in _COMPACT_INPUT_ROOTS
        ):
            raise ValueError(f"compact input is outside the derived/model allowlist: {path}")
        files = (path,) if path.is_file() else tuple(row for row in path.rglob("*") if row.is_file())
        if any(row.suffix.lower() in {".csv", ".tsv", ".parquet"} for row in files):
            raise ValueError(f"raw tabular offline data is forbidden in compact closure: {path}")
        input_bytes = sum(row.stat().st_size for row in files)
        if input_bytes > MAX_COMPACT_INPUT_BYTES:
            raise ValueError(f"compact input exceeds 128 MiB: {path}")
        closure_bytes += input_bytes
        result.append(path)
    if closure_bytes > MAX_COMPACT_CLOSURE_BYTES:
        raise ValueError("compact input closure exceeds 256 MiB")
    if len(set(result)) != len(result):
        raise ValueError("compact inputs must be unique")
    return tuple(result)


def _manifest_case_builders(manifest: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        str(row.get("case_builder") or DEVELOPMENT_CASE_BUILDER)
        for row in manifest["cells"]
    )


def compact_inputs_for_manifest_v1(
    manifest: Mapping[str, Any],
    values: Iterable[str | Path] | None,
) -> tuple[Path, ...]:
    """Add the files required by the selected guide and case builders."""

    selected = list(_compact_inputs(values))
    required = [OFFLINE_GUIDE_ARTIFACT]
    if UPPER_KARA_CASE_BUILDER in _manifest_case_builders(manifest):
        required.extend(UPPER_KARA_COMPACT_INPUTS)
    selected_set = set(selected)
    selected.extend(path for path in required if path.resolve() not in selected_set)
    return _compact_inputs(selected)


def remote_wave_action_sequence_input_paths_v1(
    project: PurePosixPath,
) -> JSONMap:
    """Return the staged compact file locators consumed by the worker."""

    selector = project / PurePosixPath(
        UPPER_KARA_SELECTOR_DIRECTORY.relative_to(PROJECT_ROOT).as_posix()
    )
    catalog = project / PurePosixPath(
        UPPER_KARA_CATALOG_DIRECTORY.relative_to(PROJECT_ROOT).as_posix()
    )
    return {
        "offline_guide_artifact": str(
            project
            / PurePosixPath(
                OFFLINE_GUIDE_ARTIFACT.relative_to(PROJECT_ROOT).as_posix()
            )
        ),
        "scenario_capsule": str(
            project
            / PurePosixPath(
                UPPER_KARA_SCENARIO_CAPSULE.relative_to(PROJECT_ROOT).as_posix()
            )
        ),
        "selector_manifest": str(selector / "manifest.json"),
        "selector_representatives": str(selector / "representatives.jsonl"),
        "catalog_manifest": str(catalog / "manifest.json"),
        "catalog_data": str(catalog / "catalog.jsonl.gz"),
    }


def _remote_layout(shared_home: str, run_id: str) -> tuple[PurePosixPath, PurePosixPath]:
    if not isinstance(shared_home, str) or not shared_home.startswith("/home/") or "\n" in shared_home:
        raise ValueError("shared_home must be one absolute remote home")
    run_root = PurePosixPath(shared_home) / "scheduleurm_work/o2o-dps-hpc/runs" / RUN_FAMILY / run_id
    project = run_root / "AddOns/BrainOfCat/o2o-dps"
    return run_root, project


def build_wave_action_sequence_stage_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    bridge: str | Path,
    cell_manifest: str | Path,
    compact_inputs: Sequence[str | Path] | None = None,
) -> JSONMap:
    """Build a local-only staging receipt without contacting any node."""

    _valid_run_id(run_id)
    bridge_path = _validated_bridge(bridge)
    manifest_path = Path(cell_manifest).expanduser().resolve(strict=True)
    manifest = load_exact_cell_manifest_v1(manifest_path)
    shards = assign_exact_cell_shards_v1(manifest)
    compact = compact_inputs_for_manifest_v1(manifest, compact_inputs)
    case_builders = _manifest_case_builders(manifest)
    if not ITEM_DATABASE.is_file():
        raise FileNotFoundError(ITEM_DATABASE)
    if not DEPLOYED_CONTRA_RUNTIME_BINDING.is_file():
        raise FileNotFoundError(DEPLOYED_CONTRA_RUNTIME_BINDING)
    if not CONTRA260817_SOURCE_ROOT.is_dir():
        raise FileNotFoundError(CONTRA260817_SOURCE_ROOT)
    run_root, project = _remote_layout(shared_home, run_id)
    wowsims = run_root / "AddOns/BrainOfCat/wowsims-turtle"
    runtime_inputs = remote_wave_action_sequence_input_paths_v1(project)

    copy_specs: list[JSONMap] = [
        {
            "source": str(PROJECT_ROOT / "o2o_dps"),
            "destination": str(project / "o2o_dps"),
            "kind": "directory",
            "python_source_only": True,
        },
        {
            "source": str(PROJECT_ROOT / "configs"),
            "destination": str(project / "configs"),
            "kind": "directory",
            "python_source_only": False,
        },
        {
            "source": str(manifest_path),
            "destination": str(project / "inputs/exact-cells.json"),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(ITEM_DATABASE.resolve()),
            "destination": str(wowsims / "assets/database/db.json"),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(bridge_path),
            "destination": str(project / "bin" / bridge_path.name),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(DEPLOYED_CONTRA_RUNTIME_BINDING.resolve()),
            "destination": str(
                project
                / ".hpc-local/smokes/cat-gap-three-baseline-v1"
                / DEPLOYED_CONTRA_RUNTIME_BINDING.name
            ),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(CONTRA260817_SOURCE_ROOT.resolve()),
            "destination": str(run_root / "AddOns/BrainOfCat/Contra_new"),
            "kind": "directory",
            "python_source_only": False,
            "include_extensions": list(CONTRA260817_CODE_EXTENSIONS),
        },
    ]
    for source in compact:
        relative = source.relative_to(PROJECT_ROOT)
        copy_specs.append(
            {
                "source": str(source),
                "destination": str(project / PurePosixPath(relative.as_posix())),
                "kind": "file" if source.is_file() else "directory",
                "python_source_only": False,
            }
        )

    return {
        "schema": "wave_action_sequence_remote_stage_plan/v1",
        "status": "DRY_RUN_NO_REMOTE_IO",
        "run_id": run_id,
        "run_root": str(run_root),
        "project_root": str(project),
        "bridge": str(project / "bin" / bridge_path.name),
        "bridge_source": str(bridge_path),
        "cell_manifest": str(project / "inputs/exact-cells.json"),
        "cell_count": len(manifest["cells"]),
        "case_builders": sorted(case_builders),
        "shard_cell_counts": [len(shard) for shard in shards],
        "compact_inputs": [str(path) for path in compact],
        "offline_guide_artifact": runtime_inputs[
            "offline_guide_artifact"
        ],
        "upper_kara_compact_closure": (
            runtime_inputs
            if UPPER_KARA_CASE_BUILDER in case_builders
            else None
        ),
        "copy_specs": copy_specs,
        "raw_offline_data_staged": False,
        "checkpoint_staged": False,
        "shared_filesystem_single_copy": True,
    }


def stage_wave_action_sequence_v1(
    *,
    staging_node: str,
    run_id: str,
    bridge: str | Path,
    cell_manifest: str | Path,
    compact_inputs: Sequence[str | Path] | None = None,
) -> JSONMap:
    """Execute the compact stage only; never launch or submit a task."""

    if staging_node not in NODE_NAMES:
        raise ValueError("staging node must be node001--node006")
    scheduler = _scheduler()
    shared_home, receipts = _shared_home(scheduler)
    plan = build_wave_action_sequence_stage_plan_v1(
        shared_home=shared_home,
        run_id=run_id,
        bridge=bridge,
        cell_manifest=cell_manifest,
        compact_inputs=compact_inputs,
    )
    project = plan["project_root"]
    rc, _, stderr = scheduler.run_on(
        staging_node, f"test ! -e {shlex.quote(project)}", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(
            f"run directory already exists; choose a new run_id: {project}: {stderr[-500:]}"
        )

    destinations = set()
    for row in plan["copy_specs"]:
        destination = PurePosixPath(row["destination"])
        destinations.add(
            str(destination if row["kind"] == "directory" else destination.parent)
        )
    rc, _, stderr = scheduler.run_on(
        staging_node,
        "mkdir -p " + " ".join(shlex.quote(row) for row in sorted(destinations)),
        timeout=30,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote run directory creation failed: {stderr[-500:]}")

    ssh_shell = scheduler._ssh_rsync_shell_for_node(staging_node)
    ssh_target = scheduler._ssh_target_for_node(staging_node)
    for row in plan["copy_specs"]:
        source = Path(row["source"])
        command = ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms"]
        if row["python_source_only"]:
            command.extend(["--include=*/", "--include=*.py", "--exclude=*"])
        elif row.get("include_extensions"):
            command.append("--include=*/")
            command.extend(
                f"--include=*{extension}"
                for extension in row["include_extensions"]
            )
            command.append("--exclude=*")
        source_arg = str(source) + ("/" if row["kind"] == "directory" else "")
        destination = row["destination"] + ("/" if row["kind"] == "directory" else "")
        command.extend(["-e", ssh_shell, source_arg, f"{ssh_target}:{destination}"])
        subprocess.run(command, check=True, timeout=240)

    rc, _, stderr = scheduler.run_on(
        staging_node, f"chmod u+x {shlex.quote(plan['bridge'])}", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge executable bit failed: {stderr[-500:]}")
    return {
        **plan,
        "status": "STAGED_NO_JOB_LAUNCHED",
        "staging_node": staging_node,
        "shared_home_receipts": receipts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--cell-manifest", type=Path, required=True)
    parser.add_argument("--compact-input", type=Path, action="append")
    parser.add_argument("--shared-home", default="/home/zhengliang01")
    parser.add_argument("--staging-node", choices=NODE_NAMES, default="node001")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform rsync to the shared home; without this flag no remote I/O occurs",
    )
    args = parser.parse_args()
    compact = None if args.compact_input is None else tuple(args.compact_input)
    if args.execute:
        payload = stage_wave_action_sequence_v1(
            staging_node=args.staging_node,
            run_id=args.run_id,
            bridge=args.bridge,
            cell_manifest=args.cell_manifest,
            compact_inputs=compact,
        )
    else:
        payload = build_wave_action_sequence_stage_plan_v1(
            shared_home=args.shared_home,
            run_id=args.run_id,
            bridge=args.bridge,
            cell_manifest=args.cell_manifest,
            compact_inputs=compact,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
