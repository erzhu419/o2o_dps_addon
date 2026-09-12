from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.historical_build_catalog_v1 import SCHEMA as CATALOG_SCHEMA
from o2o_dps.historical_representative_build_selector_v1 import (
    SCHEMA as REPRESENTATIVE_MANIFEST_SCHEMA,
)
from o2o_dps.p0_static_admission_pipeline_v1 import (
    STAGE_NAMES,
    run_p0_static_admission_pipeline,
)
from o2o_dps.turtle_talent_position_map_v1 import ADMISSION_STATUS
from o2o_dps.wowsims_mechanics_coverage_registry_v1 import COVERAGE_SCHEMA


class P0StaticAdmissionPipelineV1Tests(unittest.TestCase):
    def _paths(self, root: Path) -> dict[str, object]:
        return {
            "expected_player_guid": "0x00000000000000A1",
            "expected_client_build": "7272",
            "static_profile_path": root / "CustomData" / "static.jsonl",
            "chronicle_evidence_path": root / "old" / "catalog.jsonl.gz",
            "talent_map_output_path": root / "talent" / "current.json",
            "database_path": root / "wowsims" / "db.json",
            "wowsims_root": root / "wowsims",
            "registry_historical_catalog_path": root
            / "old"
            / "catalog.jsonl.gz",
            "registry_output_path": root / "registry" / "registry.json",
            "admission_manifest_path": root / "admission" / "manifest.json",
            "data_root": root / "offline_data",
            "catalog_output_directory": root / "new-catalog",
            "representative_output_directory": root / "representatives",
            "receipt_path": root / "reports" / "receipt.json",
        }

    @staticmethod
    def _write_talent(artifact: dict[str, object], *, output_path: Path) -> Path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact), encoding="utf-8")
        return path

    @staticmethod
    def _write_registry(*, output_path: Path, **_: object) -> dict[str, object]:
        path = Path(output_path).resolve()
        registry = {
            "schema": COVERAGE_SCHEMA,
            "talent_translations": {
                "WARRIOR": {"client_build_position_maps": {"7272": {}}}
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry), encoding="utf-8")
        return registry

    @staticmethod
    def _write_catalog(*, output_directory: Path, **_: object) -> dict[str, object]:
        directory = Path(output_directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        catalog = directory / "catalog.jsonl.gz"
        manifest.write_text("{}\n", encoding="utf-8")
        catalog.write_bytes(b"catalog")
        return {
            "schema": CATALOG_SCHEMA,
            "manifest_path": str(manifest),
            "catalog_path": str(catalog),
            "summary": {
                "by_hero_class": {
                    "WARRIOR": {"development_build_segment_count": 17}
                }
            },
        }

    @staticmethod
    def _write_representatives(
        *, output_directory: Path, **_: object
    ) -> dict[str, object]:
        directory = Path(output_directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        return {
            "schema": REPRESENTATIVE_MANIFEST_SCHEMA,
            "status": "READY",
            "blockers": [],
            "manifest_path": str(manifest),
            "summary": {"selected_representative_count": 5},
        }

    def test_success_composes_existing_stages_in_order_and_never_trains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            talent = {
                "admission_status": ADMISSION_STATUS,
                "client_build": "7272",
            }
            calls: list[str] = []

            def build_talent(**kwargs: object) -> dict[str, object]:
                calls.append("talent")
                self.assertEqual(
                    kwargs["expected_player_guid"], "0x00000000000000A1"
                )
                self.assertEqual(kwargs["expected_client_build"], "7272")
                return talent

            def write_registry(**kwargs: object) -> dict[str, object]:
                calls.append("registry")
                return self._write_registry(**kwargs)

            def write_catalog(**kwargs: object) -> dict[str, object]:
                calls.append("catalog")
                return self._write_catalog(**kwargs)

            def write_representatives(**kwargs: object) -> dict[str, object]:
                calls.append("representatives")
                return self._write_representatives(**kwargs)

            with (
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_turtle_talent_position_map_from_files",
                    side_effect=build_talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "write_turtle_talent_position_map",
                    side_effect=self._write_talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1.write_registry",
                    side_effect=write_registry,
                ) as registry_mock,
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_historical_build_catalog",
                    side_effect=write_catalog,
                ) as catalog_mock,
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "select_historical_representative_builds",
                    side_effect=write_representatives,
                ) as representative_mock,
            ):
                receipt, exit_code = run_p0_static_admission_pipeline(**paths)

            self.assertEqual(exit_code, 0)
            self.assertEqual(receipt["status"], "P0_STATIC_ADMISSION_READY")
            self.assertEqual(calls, ["talent", "registry", "catalog", "representatives"])
            self.assertEqual(
                [row["status"] for row in receipt["stages"]],
                ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"],
            )
            self.assertTrue(
                all(value is False for value in receipt["execution_boundary"].values())
            )
            self.assertEqual(
                registry_mock.call_args.kwargs["talent_position_map_paths"],
                [paths["talent_map_output_path"].resolve()],
            )
            self.assertEqual(
                catalog_mock.call_args.kwargs["coverage_registry_path"],
                paths["registry_output_path"].resolve(),
            )
            self.assertEqual(
                representative_mock.call_args.kwargs["catalog_manifest"],
                (paths["catalog_output_directory"] / "manifest.json").resolve(),
            )
            persisted = json.loads(paths["receipt_path"].read_text(encoding="utf-8"))
            self.assertEqual(persisted, receipt)

    def test_first_stage_failure_is_nonzero_and_later_stages_are_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            with (
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_turtle_talent_position_map_from_files",
                    side_effect=RuntimeError("no static profile"),
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1.write_registry"
                ) as registry_mock,
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_historical_build_catalog"
                ) as catalog_mock,
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "select_historical_representative_builds"
                ) as representative_mock,
            ):
                receipt, exit_code = run_p0_static_admission_pipeline(**paths)

            self.assertEqual(exit_code, 2)
            self.assertEqual(receipt["status"], "FAILED_P0_STATIC_ADMISSION")
            self.assertEqual(receipt["failure"]["stage"], STAGE_NAMES[0])
            self.assertEqual(
                [row["status"] for row in receipt["stages"]],
                ["FAILED", "NOT_RUN", "NOT_RUN", "NOT_RUN"],
            )
            registry_mock.assert_not_called()
            catalog_mock.assert_not_called()
            representative_mock.assert_not_called()

    def test_registry_failure_does_not_claim_catalog_or_selector_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            talent = {
                "admission_status": ADMISSION_STATUS,
                "client_build": "7272",
            }
            with (
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_turtle_talent_position_map_from_files",
                    return_value=talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "write_turtle_talent_position_map",
                    side_effect=self._write_talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1.write_registry",
                    side_effect=RuntimeError("unsupported client talent"),
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_historical_build_catalog"
                ) as catalog_mock,
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "select_historical_representative_builds"
                ) as representative_mock,
            ):
                receipt, exit_code = run_p0_static_admission_pipeline(**paths)

            self.assertEqual(exit_code, 2)
            self.assertEqual(
                [row["status"] for row in receipt["stages"]],
                ["SUCCEEDED", "FAILED", "NOT_RUN", "NOT_RUN"],
            )
            self.assertEqual(receipt["failure"]["stage"], STAGE_NAMES[1])
            catalog_mock.assert_not_called()
            representative_mock.assert_not_called()

    def test_blocked_selector_returns_nonzero_without_training_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            talent = {
                "admission_status": ADMISSION_STATUS,
                "client_build": "7272",
            }

            def blocked_selector(*, output_directory: Path, **_: object) -> dict[str, object]:
                directory = Path(output_directory).resolve()
                directory.mkdir(parents=True, exist_ok=True)
                manifest = directory / "manifest.json"
                manifest.write_text("{}\n", encoding="utf-8")
                return {
                    "schema": REPRESENTATIVE_MANIFEST_SCHEMA,
                    "status": "BLOCKED_NO_ELIGIBLE_BUILDS",
                    "blockers": ["BLOCKED_NO_ELIGIBLE_BUILDS"],
                    "manifest_path": str(manifest),
                    "summary": {"selected_representative_count": 0},
                }

            with (
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_turtle_talent_position_map_from_files",
                    return_value=talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "write_turtle_talent_position_map",
                    side_effect=self._write_talent,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1.write_registry",
                    side_effect=self._write_registry,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "build_historical_build_catalog",
                    side_effect=self._write_catalog,
                ),
                patch(
                    "o2o_dps.p0_static_admission_pipeline_v1."
                    "select_historical_representative_builds",
                    side_effect=blocked_selector,
                ),
            ):
                receipt, exit_code = run_p0_static_admission_pipeline(**paths)

            self.assertEqual(exit_code, 3)
            self.assertEqual(receipt["status"], "BLOCKED_P0_STATIC_ADMISSION")
            self.assertEqual(receipt["stages"][-1]["status"], "BLOCKED")
            self.assertFalse(receipt["execution_boundary"]["training_invoked"])


if __name__ == "__main__":
    unittest.main()
