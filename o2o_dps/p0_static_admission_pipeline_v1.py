"""Run the four-stage P0 static build-admission pipeline.

This command only composes existing admission builders.  It does not start a
simulator, search job, training job, or baseline comparison.  The registry's
historical catalogue input is deliberately separate from the catalogue output:
the registry may inspect the existing compact catalogue before this run rebuilds
that catalogue with the newly admitted registry.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .historical_build_catalog_v1 import (
    DEFAULT_ADMISSION_MANIFEST,
    DEFAULT_DATA_ROOT,
    DEFAULT_OUTPUT_DIRECTORY as DEFAULT_CATALOG_OUTPUT_DIRECTORY,
    SCHEMA as CATALOG_SCHEMA,
    build_historical_build_catalog,
)
from .historical_representative_build_selector_v1 import (
    DEFAULT_OUTPUT_DIRECTORY as DEFAULT_REPRESENTATIVE_OUTPUT_DIRECTORY,
    SCHEMA as REPRESENTATIVE_MANIFEST_SCHEMA,
    select_historical_representative_builds,
)
from .turtle_talent_position_map_v1 import (
    ADMISSION_STATUS as TALENT_MAP_ADMISSION_STATUS,
    DEFAULT_SERIALIZATION_CONTRACT,
    build_turtle_talent_position_map_from_files,
    write_turtle_talent_position_map,
)
from .wowsims_mechanics_coverage_registry_v1 import (
    COVERAGE_SCHEMA,
    DEFAULT_CATALOG as DEFAULT_REGISTRY_HISTORICAL_CATALOG,
    DEFAULT_DATABASE,
    DEFAULT_OUTPUT as DEFAULT_REGISTRY_OUTPUT,
    DEFAULT_WOWSIMS_ROOT,
    write_registry,
)


SCHEMA = "p0_static_admission_pipeline_receipt/v1"
IMPLEMENTATION_REVISION = "v1.1_expected_character_build_bound_no_training"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATIC_PROFILE = Path(r"D:\WOW\CustomData\BrainOfCatStaticProfiles.jsonl")
DEFAULT_CHRONICLE_EVIDENCE = DEFAULT_REGISTRY_HISTORICAL_CATALOG
DEFAULT_TALENT_MAP_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "turtle_talent_position_map"
    / "v1"
    / "current.json"
)
DEFAULT_RECEIPT = (
    PROJECT_ROOT / "offline_data" / "reports" / "p0_static_admission_pipeline_v1.json"
)

STAGE_NAMES = (
    "TURTLE_TALENT_POSITION_MAP",
    "WOWSIMS_MECHANICS_COVERAGE_REGISTRY",
    "HISTORICAL_BUILD_CATALOG",
    "HISTORICAL_REPRESENTATIVE_BUILD_SELECTOR",
)


class P0StaticAdmissionPipelineError(RuntimeError):
    """A stage returned an internally inconsistent success artifact."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolved(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _require_file(path: str | Path, *, label: str) -> Path:
    resolved = _resolved(path)
    if not resolved.is_file():
        raise P0StaticAdmissionPipelineError(
            f"{label} did not write its declared file: {resolved}"
        )
    return resolved


def _stage_rows() -> list[dict[str, Any]]:
    return [
        {"name": name, "status": "NOT_RUN", "output": None, "summary": None}
        for name in STAGE_NAMES
    ]


def run_p0_static_admission_pipeline(
    *,
    expected_player_guid: str,
    expected_client_build: str | int,
    static_profile_path: str | Path = DEFAULT_STATIC_PROFILE,
    chronicle_evidence_path: str | Path = DEFAULT_CHRONICLE_EVIDENCE,
    talent_map_output_path: str | Path = DEFAULT_TALENT_MAP_OUTPUT,
    database_path: str | Path = DEFAULT_DATABASE,
    wowsims_root: str | Path = DEFAULT_WOWSIMS_ROOT,
    registry_historical_catalog_path: str | Path = DEFAULT_REGISTRY_HISTORICAL_CATALOG,
    registry_output_path: str | Path = DEFAULT_REGISTRY_OUTPUT,
    admission_manifest_path: str | Path = DEFAULT_ADMISSION_MANIFEST,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    catalog_output_directory: str | Path = DEFAULT_CATALOG_OUTPUT_DIRECTORY,
    representative_output_directory: str | Path = DEFAULT_REPRESENTATIVE_OUTPUT_DIRECTORY,
    max_representatives: int = 64,
    receipt_path: str | Path = DEFAULT_RECEIPT,
) -> tuple[dict[str, Any], int]:
    """Execute static admission only and persist a compact receipt.

    Exit code 0 means all four admission stages returned READY.  Exit code 2
    means a stage raised or returned an invalid artifact; exit code 3 means the
    representative selector produced an explicit blocking result.
    """

    started_at = _utc_now()
    stages = _stage_rows()
    receipt_output = _resolved(receipt_path)
    inputs = {
        "expected_player_guid": str(expected_player_guid).strip(),
        "expected_client_build": str(expected_client_build).strip(),
        "static_profile": str(_resolved(static_profile_path)),
        "chronicle_evidence": str(_resolved(chronicle_evidence_path)),
        "chronicle_talent_serialization_contract": str(
            _resolved(DEFAULT_SERIALIZATION_CONTRACT)
        ),
        "wowsims_database": str(_resolved(database_path)),
        "wowsims_root": str(_resolved(wowsims_root)),
        "registry_historical_catalog": str(
            _resolved(registry_historical_catalog_path)
        ),
        "admission_manifest": str(_resolved(admission_manifest_path)),
        "data_root": str(_resolved(data_root)),
        "max_representatives": max_representatives,
    }
    outputs = {
        "talent_position_map": str(_resolved(talent_map_output_path)),
        "mechanics_coverage_registry": str(_resolved(registry_output_path)),
        "historical_build_catalog_directory": str(
            _resolved(catalog_output_directory)
        ),
        "representative_build_directory": str(
            _resolved(representative_output_directory)
        ),
        "receipt": str(receipt_output),
    }
    pipeline_status = "FAILED_P0_STATIC_ADMISSION"
    exit_code = 2
    failure: dict[str, Any] | None = None

    try:
        talent_artifact = build_turtle_talent_position_map_from_files(
            static_profile_path=static_profile_path,
            chronicle_evidence_path=chronicle_evidence_path,
            expected_player_guid=expected_player_guid,
            expected_client_build=expected_client_build,
        )
        if talent_artifact.get("admission_status") != TALENT_MAP_ADMISSION_STATUS:
            raise P0StaticAdmissionPipelineError(
                "talent position map did not return the admitted status"
            )
        talent_output = write_turtle_talent_position_map(
            talent_artifact, output_path=talent_map_output_path
        )
        talent_output = _require_file(
            talent_output, label="turtle talent position map"
        )
        stages[0].update(
            {
                "status": "SUCCEEDED",
                "output": str(talent_output),
                "summary": {
                    "client_build": talent_artifact.get("client_build"),
                    "admission_status": talent_artifact.get("admission_status"),
                },
            }
        )

        registry = write_registry(
            output_path=registry_output_path,
            database_path=database_path,
            wowsims_root=wowsims_root,
            historical_catalog_path=registry_historical_catalog_path,
            talent_position_map_paths=[talent_output],
        )
        registry_output = _require_file(
            registry_output_path, label="wowsims mechanics coverage registry"
        )
        client_build = str(talent_artifact.get("client_build"))
        position_maps = (
            registry.get("talent_translations", {})
            .get("WARRIOR", {})
            .get("client_build_position_maps", {})
        )
        if registry.get("schema") != COVERAGE_SCHEMA or client_build not in position_maps:
            raise P0StaticAdmissionPipelineError(
                "coverage registry did not contain the admitted client-build talent map"
            )
        stages[1].update(
            {
                "status": "SUCCEEDED",
                "output": str(registry_output),
                "summary": {
                    "schema": registry.get("schema"),
                    "pinned_client_build": client_build,
                },
            }
        )

        catalog = build_historical_build_catalog(
            admission_manifest=admission_manifest_path,
            data_root=data_root,
            output_directory=catalog_output_directory,
            coverage_registry_path=registry_output,
        )
        if catalog.get("schema") != CATALOG_SCHEMA:
            raise P0StaticAdmissionPipelineError(
                "historical build catalog returned the wrong schema"
            )
        catalog_manifest = _require_file(
            catalog.get("manifest_path", ""), label="historical build catalog manifest"
        )
        catalog_data = _require_file(
            catalog.get("catalog_path", ""), label="historical build catalog"
        )
        catalog_summary = catalog.get("summary", {})
        warrior_summary = catalog_summary.get("by_hero_class", {}).get("WARRIOR", {})
        stages[2].update(
            {
                "status": "SUCCEEDED",
                "output": str(catalog_manifest),
                "summary": {
                    "catalog_path": str(catalog_data),
                    "warrior_development_build_segment_count": warrior_summary.get(
                        "development_build_segment_count"
                    ),
                },
            }
        )

        representatives = select_historical_representative_builds(
            catalog_manifest=catalog_manifest,
            output_directory=representative_output_directory,
            max_representatives=max_representatives,
        )
        if representatives.get("schema") != REPRESENTATIVE_MANIFEST_SCHEMA:
            raise P0StaticAdmissionPipelineError(
                "representative selector returned the wrong schema"
            )
        representative_manifest = _require_file(
            representatives.get("manifest_path", ""),
            label="historical representative selector manifest",
        )
        representative_status = representatives.get("status")
        representative_count = representatives.get("summary", {}).get(
            "selected_representative_count"
        )
        stages[3].update(
            {
                "status": (
                    "SUCCEEDED" if representative_status == "READY" else "BLOCKED"
                ),
                "output": str(representative_manifest),
                "summary": {
                    "selector_status": representative_status,
                    "selected_representative_count": representative_count,
                    "blockers": representatives.get("blockers", []),
                },
            }
        )
        if representative_status == "READY":
            pipeline_status = "P0_STATIC_ADMISSION_READY"
            exit_code = 0
        else:
            pipeline_status = "BLOCKED_P0_STATIC_ADMISSION"
            exit_code = 3
            failure = {
                "stage": STAGE_NAMES[3],
                "type": "ADMISSION_BLOCKED",
                "message": f"representative selector status={representative_status!r}",
            }
    except Exception as error:
        failed_index = next(
            (index for index, row in enumerate(stages) if row["status"] == "NOT_RUN"),
            len(stages) - 1,
        )
        stages[failed_index]["status"] = "FAILED"
        failure = {
            "stage": stages[failed_index]["name"],
            "type": type(error).__name__,
            "message": str(error),
        }

    receipt = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "status": pipeline_status,
        "exit_code": exit_code,
        "inputs": inputs,
        "outputs": outputs,
        "stages": stages,
        "failure": failure,
        "execution_boundary": {
            "training_invoked": False,
            "simulator_rollouts_invoked": False,
            "search_invoked": False,
            "baseline_comparison_invoked": False,
            "superiority_claimed": False,
        },
    }
    _write_json_atomic(receipt_output, receipt)
    return receipt, exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-player-guid", required=True)
    parser.add_argument("--expected-client-build", required=True)
    parser.add_argument("--static-profile", default=str(DEFAULT_STATIC_PROFILE))
    parser.add_argument(
        "--chronicle-evidence", default=str(DEFAULT_CHRONICLE_EVIDENCE)
    )
    parser.add_argument("--talent-map-output", default=str(DEFAULT_TALENT_MAP_OUTPUT))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--wowsims-root", default=str(DEFAULT_WOWSIMS_ROOT))
    parser.add_argument(
        "--registry-historical-catalog",
        default=str(DEFAULT_REGISTRY_HISTORICAL_CATALOG),
        help="existing compact catalog inspected while building the registry",
    )
    parser.add_argument("--registry-output", default=str(DEFAULT_REGISTRY_OUTPUT))
    parser.add_argument(
        "--admission-manifest", default=str(DEFAULT_ADMISSION_MANIFEST)
    )
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument(
        "--catalog-output-directory",
        default=str(DEFAULT_CATALOG_OUTPUT_DIRECTORY),
    )
    parser.add_argument(
        "--representative-output-directory",
        default=str(DEFAULT_REPRESENTATIVE_OUTPUT_DIRECTORY),
    )
    parser.add_argument("--max-representatives", type=int, default=64)
    parser.add_argument("--receipt", default=str(DEFAULT_RECEIPT))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt, exit_code = run_p0_static_admission_pipeline(
        expected_player_guid=args.expected_player_guid,
        expected_client_build=args.expected_client_build,
        static_profile_path=args.static_profile,
        chronicle_evidence_path=args.chronicle_evidence,
        talent_map_output_path=args.talent_map_output,
        database_path=args.database,
        wowsims_root=args.wowsims_root,
        registry_historical_catalog_path=args.registry_historical_catalog,
        registry_output_path=args.registry_output,
        admission_manifest_path=args.admission_manifest,
        data_root=args.data_root,
        catalog_output_directory=args.catalog_output_directory,
        representative_output_directory=args.representative_output_directory,
        max_representatives=args.max_representatives,
        receipt_path=args.receipt,
    )
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "exit_code": exit_code,
                "receipt": receipt["outputs"]["receipt"],
                "failure": receipt["failure"],
                "training_invoked": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
