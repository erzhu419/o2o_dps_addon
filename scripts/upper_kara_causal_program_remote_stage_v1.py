"""Plan or stage the minimal continuous two-wave causal-program closure.

Planning is local-only.  ``--execute`` copies only this project's Python and
configuration, the campaign JSON, one Linux bridge, its single item database,
and one compact runtime-binding JSON.  Contra260817 contributes only its small
``.lua/.toc/.xml`` identity closure; no external project is copied wholesale,
Only the compact Chronicle action prior is copied from ``offline_data`` for
legacy campaigns.  The heterogeneous v7/v8 campaigns additionally copy their
one small, pinned derived scenario capsule; raw Chronicle rows, checkpoints,
and the rest of that directory remain excluded.
This script never submits a job.
"""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    CatActionPlanResidualSequenceSearchV8,
    HeterogeneousTwoWaveSequenceSearchV7,
    assign_evaluation_shards_v1,
    assign_teacher_plan_shards_v8,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.development_wave_coverage_v1 import (
    DEFAULT_MANIFEST as DEFAULT_SCENARIO_CAPSULE,
)
from o2o_dps.fury_chronicle_prior import DEFAULT_MODEL as DEFAULT_OFFLINE_GUIDE
from o2o_dps.offline_action_sequence_guide_v1 import (
    SOURCE_BUILD_POOLED,
    load_offline_action_sequence_guide_v1,
)
from scripts.factored_press_remote_stage_v1 import NODE_NAMES, _scheduler, _shared_home


JSONMap = dict[str, Any]
RUN_FAMILY = "upper-kara-causal-program-v1"
ITEM_DATABASE = BRAIN_ROOT / "wowsims-turtle/assets/database/db.json"
CONTRA260817_SOURCE_ROOT = BRAIN_ROOT / "Contra_new"
CONTRA260817_CODE_EXTENSIONS = (".lua", ".toc", ".xml")
DEFAULT_RUNTIME_BINDING = (
    PROJECT_ROOT
    / ".hpc-local/smokes/cat-gap-three-baseline-v1"
    / "deployed-contra-runtime-binding-v1.951b8faa.json"
)
REMOTE_RUNTIME_BINDING_NAME = "deployed-contra-runtime-binding-v1.json"
REMOTE_OFFLINE_GUIDE_NAME = "fury_partial_markov_v1.model.json"
REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH = PurePosixPath(
    "offline_data/derived/fury_offline_scenario_capsules/v2"
) / DEFAULT_SCENARIO_CAPSULE.name
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _valid_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise ValueError("invalid run ID")
    return value


def _validated_file(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser().resolve(strict=True)
    if not value.is_file():
        raise ValueError(f"{label} must be a file")
    return value


def _remote_layout(shared_home: str, run_id: str) -> tuple[PurePosixPath, PurePosixPath]:
    if (
        not isinstance(shared_home, str)
        or not shared_home.startswith("/home/")
        or "\n" in shared_home
    ):
        raise ValueError("shared_home must be one absolute remote home")
    run_root = (
        PurePosixPath(shared_home)
        / "scheduleurm_work/o2o-dps-hpc/runs"
        / RUN_FAMILY
        / run_id
    )
    return run_root, run_root / "AddOns/BrainOfCat/o2o-dps"


def build_upper_kara_causal_program_stage_plan_v1(
    *,
    shared_home: str,
    run_id: str,
    bridge: str | Path,
    campaign: str | Path,
    runtime_binding: str | Path = DEFAULT_RUNTIME_BINDING,
    item_database: str | Path = ITEM_DATABASE,
    offline_guide_artifact: str | Path = DEFAULT_OFFLINE_GUIDE,
    scenario_capsule_artifact: str | Path = DEFAULT_SCENARIO_CAPSULE,
) -> JSONMap:
    _valid_run_id(run_id)
    bridge_path = _validated_file(bridge, "bridge")
    if not bridge_path.name.endswith(".linux-amd64"):
        raise ValueError("bridge must explicitly name one *.linux-amd64 file")
    campaign_path = _validated_file(campaign, "campaign")
    parsed = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    binding_path = _validated_file(runtime_binding, "runtime_binding")
    database_path = _validated_file(item_database, "item_database")
    offline_guide_path = _validated_file(
        offline_guide_artifact, "offline_guide_artifact"
    )
    offline_guide = load_offline_action_sequence_guide_v1(
        offline_guide_path, source_build_scope=SOURCE_BUILD_POOLED
    )
    scenario_capsule_path: Path | None = None
    if isinstance(
        parsed.search_spec,
        (
            HeterogeneousTwoWaveSequenceSearchV7,
            CatActionPlanResidualSequenceSearchV8,
        ),
    ):
        scenario_capsule_path = _validated_file(
            scenario_capsule_artifact, "scenario_capsule_artifact"
        )
        if scenario_capsule_path.name != DEFAULT_SCENARIO_CAPSULE.name:
            raise ValueError(
                "heterogeneous scenario capsule filename differs from the pinned source"
            )
    contra_source = CONTRA260817_SOURCE_ROOT.resolve(strict=True)
    if not contra_source.is_dir():
        raise ValueError("Contra260817 source closure root must be a directory")
    contra_code_files = tuple(
        path
        for path in contra_source.rglob("*")
        if path.is_file() and path.suffix.lower() in CONTRA260817_CODE_EXTENSIONS
    )
    if not contra_code_files:
        raise ValueError("Contra260817 source code closure is empty")
    run_root, project = _remote_layout(shared_home, run_id)
    runtime_root = run_root / "runtime/wowsims"
    remote_campaign = project / "inputs/campaign.json"
    remote_bridge = project / "bin" / bridge_path.name
    remote_binding = project / "runtime" / REMOTE_RUNTIME_BINDING_NAME
    remote_database = runtime_root / "assets/database/db.json"
    remote_offline_guide = (
        project / "runtime/guides" / REMOTE_OFFLINE_GUIDE_NAME
    )
    copy_specs = [
        {
            "source": str((PROJECT_ROOT / "o2o_dps").resolve()),
            "destination": str(project / "o2o_dps"),
            "kind": "directory",
            "python_source_only": True,
        },
        {
            "source": str((PROJECT_ROOT / "configs").resolve()),
            "destination": str(project / "configs"),
            "kind": "directory",
            "python_source_only": False,
        },
        {
            "source": str(campaign_path),
            "destination": str(remote_campaign),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(bridge_path),
            "destination": str(remote_bridge),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(binding_path),
            "destination": str(remote_binding),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(database_path),
            "destination": str(remote_database),
            "kind": "file",
            "python_source_only": False,
        },
        {
            "source": str(offline_guide_path),
            "destination": str(remote_offline_guide),
            "kind": "file",
            "python_source_only": False,
            "compact_offline_guide": True,
        },
        {
            "source": str(contra_source),
            "destination": str(run_root / "AddOns/BrainOfCat/Contra_new"),
            "kind": "directory",
            "python_source_only": False,
            "include_extensions": list(CONTRA260817_CODE_EXTENSIONS),
        },
    ]
    if scenario_capsule_path is not None:
        copy_specs.append(
            {
                "source": str(scenario_capsule_path),
                "destination": str(
                    project / REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH
                ),
                "kind": "file",
                "python_source_only": False,
                "derived_scenario_capsule": True,
            }
        )
    forbidden_sources = tuple(
        (BRAIN_ROOT / name).resolve()
        for name in (
            "cat",
            "cat2",
            "Cat2_new",
            "contra",
            "Contra_new",
            "wowsims-turtle",
            "DPSSim",
        )
    )
    for spec in copy_specs:
        source = Path(spec["source"]).resolve()
        if spec["kind"] == "directory" and any(
            source == forbidden for forbidden in forbidden_sources
        ) and not (
            source == contra_source
            and tuple(spec.get("include_extensions", ()))
            == CONTRA260817_CODE_EXTENSIONS
        ):
            raise AssertionError("external project directory entered stage closure")
        if (
            "offline_data" in {part.lower() for part in source.parts}
            and source
            not in {
                offline_guide_path,
                scenario_capsule_path,
            }
        ):
            raise AssertionError("offline_data entered stage closure")
    v8_search = isinstance(
        parsed.search_spec, CatActionPlanResidualSequenceSearchV8
    )
    return {
        "schema": "upper_kara_causal_program_remote_stage_plan/v1",
        "status": "DRY_RUN_NO_REMOTE_IO",
        "run_id": run_id,
        "run_root": str(run_root),
        "project_root": str(project),
        "campaign": str(remote_campaign),
        "bridge": str(remote_bridge),
        "bridge_cwd": str(runtime_root),
        "runtime_binding": str(remote_binding),
        "item_database": str(remote_database),
        "offline_guide_artifact": str(remote_offline_guide),
        "training_task_count": len(assign_training_shards_v1(parsed)),
        "evaluation_task_count": len(assign_evaluation_shards_v1(parsed)),
        "teacher_task_count": (
            len(assign_teacher_plan_shards_v8(parsed)) if v8_search else 0
        ),
        "distill_task_count": len(parsed.loadout_ids) if v8_search else 0,
        "copy_specs": copy_specs,
        "raw_offline_data_staged": False,
        "offline_data_staged": scenario_capsule_path is not None,
        "compact_offline_guide_staged": True,
        "compact_offline_guide_bytes": offline_guide_path.stat().st_size,
        "compact_offline_guide_backend": offline_guide.backend,
        "compact_offline_guide_source_build_scope": (
            offline_guide.source_build_scope
        ),
        "derived_scenario_capsule_staged": scenario_capsule_path is not None,
        "derived_scenario_capsule_bytes": (
            scenario_capsule_path.stat().st_size
            if scenario_capsule_path is not None
            else 0
        ),
        "derived_scenario_capsule_remote_path": (
            str(project / REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH)
            if scenario_capsule_path is not None
            else None
        ),
        "external_project_directories_staged": False,
        "external_source_code_closure_staged": True,
        "contra260817_source_code_extensions": list(
            CONTRA260817_CODE_EXTENSIONS
        ),
        "contra260817_source_code_file_count": len(contra_code_files),
        "contra260817_source_code_bytes": sum(
            path.stat().st_size for path in contra_code_files
        ),
        "contra260817_source_verification_mode": "LIVE_TREE",
        "job_submitted": False,
    }


def stage_upper_kara_causal_program_v1(
    *,
    staging_node: str,
    run_id: str,
    bridge: str | Path,
    campaign: str | Path,
    runtime_binding: str | Path = DEFAULT_RUNTIME_BINDING,
    item_database: str | Path = ITEM_DATABASE,
    offline_guide_artifact: str | Path = DEFAULT_OFFLINE_GUIDE,
    scenario_capsule_artifact: str | Path = DEFAULT_SCENARIO_CAPSULE,
) -> JSONMap:
    if staging_node not in NODE_NAMES:
        raise ValueError("staging_node must be node001--node006")
    scheduler = _scheduler()
    shared_home, receipts = _shared_home(scheduler)
    plan = build_upper_kara_causal_program_stage_plan_v1(
        shared_home=shared_home,
        run_id=run_id,
        bridge=bridge,
        campaign=campaign,
        runtime_binding=runtime_binding,
        item_database=item_database,
        offline_guide_artifact=offline_guide_artifact,
        scenario_capsule_artifact=scenario_capsule_artifact,
    )
    rc, _, stderr = scheduler.run_on(
        staging_node,
        f"test ! -e {shlex.quote(plan['run_root'])}",
        timeout=20,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote run already exists: {stderr[-500:]}")
    directories = sorted(
        {
            str(
                PurePosixPath(spec["destination"])
                if spec["kind"] == "directory"
                else PurePosixPath(spec["destination"]).parent
            )
            for spec in plan["copy_specs"]
        }
    )
    rc, _, stderr = scheduler.run_on(
        staging_node,
        "mkdir -p " + " ".join(shlex.quote(row) for row in directories),
        timeout=30,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote directory creation failed: {stderr[-500:]}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(staging_node)
    ssh_target = scheduler._ssh_target_for_node(staging_node)
    for spec in plan["copy_specs"]:
        command = ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms"]
        if spec["python_source_only"]:
            command.extend(["--include=*/", "--include=*.py", "--exclude=*"])
        elif spec.get("include_extensions"):
            command.append("--include=*/")
            command.extend(
                f"--include=*{extension}"
                for extension in spec["include_extensions"]
            )
            command.append("--exclude=*")
        source = spec["source"] + ("/" if spec["kind"] == "directory" else "")
        destination = spec["destination"] + (
            "/" if spec["kind"] == "directory" else ""
        )
        command.extend(["-e", ssh_shell, source, f"{ssh_target}:{destination}"])
        subprocess.run(command, check=True, timeout=240)
    rc, _, stderr = scheduler.run_on(
        staging_node,
        f"chmod u+x {shlex.quote(plan['bridge'])}",
        timeout=20,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge chmod failed: {stderr[-500:]}")
    return {
        **plan,
        "status": "STAGED_NO_JOB_SUBMITTED",
        "staging_node": staging_node,
        "shared_home_receipts": receipts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_RUNTIME_BINDING)
    parser.add_argument("--item-database", type=Path, default=ITEM_DATABASE)
    parser.add_argument(
        "--offline-guide-artifact", type=Path, default=DEFAULT_OFFLINE_GUIDE
    )
    parser.add_argument(
        "--scenario-capsule-artifact",
        type=Path,
        default=DEFAULT_SCENARIO_CAPSULE,
    )
    parser.add_argument("--shared-home", default="/home/erzhu419")
    parser.add_argument("--staging-node", choices=NODE_NAMES, default="node001")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        payload = stage_upper_kara_causal_program_v1(
            staging_node=args.staging_node,
            run_id=args.run_id,
            bridge=args.bridge,
            campaign=args.campaign,
            runtime_binding=args.runtime_binding,
            item_database=args.item_database,
            offline_guide_artifact=args.offline_guide_artifact,
            scenario_capsule_artifact=args.scenario_capsule_artifact,
        )
    else:
        payload = build_upper_kara_causal_program_stage_plan_v1(
            shared_home=args.shared_home,
            run_id=args.run_id,
            bridge=args.bridge,
            campaign=args.campaign,
            runtime_binding=args.runtime_binding,
            item_database=args.item_database,
            offline_guide_artifact=args.offline_guide_artifact,
            scenario_capsule_artifact=args.scenario_capsule_artifact,
        )
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = (
    "DEFAULT_RUNTIME_BINDING",
    "CONTRA260817_CODE_EXTENSIONS",
    "CONTRA260817_SOURCE_ROOT",
    "ITEM_DATABASE",
    "REMOTE_RUNTIME_BINDING_NAME",
    "REMOTE_OFFLINE_GUIDE_NAME",
    "REMOTE_SCENARIO_CAPSULE_RELATIVE_PATH",
    "RUN_FAMILY",
    "build_upper_kara_causal_program_stage_plan_v1",
    "stage_upper_kara_causal_program_v1",
)
