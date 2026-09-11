from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_execution_source_identity_v2 import (
    PROJECT_ROOT,
    REQUIRED_PRODUCTION_PATHS,
    FuryExecutionSourceIdentityV2Error,
    build_fury_execution_source_identity_v2,
    canonical_file_bundle_sha256,
)


REQUIRED_NAMES = {
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
}


def _write(root: Path, relative: str, source: str) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="\n")


def _small_project(root: Path, *, leaf: str = "VALUE = 3\n") -> None:
    _write(root, "o2o_dps/__init__.py", "PACKAGE = 'fixture'\n")
    _write(
        root,
        "o2o_dps/a.py",
        "from . import b\nfrom .c import VALUE\nRESULT = b.VALUE + VALUE\n",
    )
    _write(root, "o2o_dps/b.py", "from o2o_dps.c import VALUE\n")
    _write(root, "o2o_dps/c.py", leaf)


class FuryExecutionSourceIdentityV2Tests(unittest.TestCase):
    def test_default_entrypoints_cover_every_production_module_named_by_contract(self):
        self.assertTrue(REQUIRED_NAMES.issubset(set(REQUIRED_PRODUCTION_PATHS)))
        self.assertEqual(len(REQUIRED_PRODUCTION_PATHS), len(set(REQUIRED_PRODUCTION_PATHS)))

    def test_actual_repo_closure_is_sorted_unique_and_self_verifying(self):
        report = build_fury_execution_source_identity_v2()

        self.assertEqual(report["status"], "PASS")
        paths = [row["relative_path"] for row in report["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(path.casefold() for path in paths)))
        self.assertTrue(REQUIRED_NAMES.issubset(set(paths)))
        self.assertEqual(
            report["canonical_bundle"]["sha256"],
            canonical_file_bundle_sha256(report["files"]),
        )
        rows = {row["relative_path"]: row for row in report["files"]}
        for relative_path in (
            "o2o_dps/sim_bridge.py",
            "o2o_dps/fury_full_policy_rollout_v2.py",
        ):
            self.assertEqual(
                rows[relative_path]["sha256"],
                hashlib.sha256((PROJECT_ROOT / relative_path).read_bytes()).hexdigest(),
            )
        self.assertFalse(report["claim_boundary"]["rollout_executed"])
        self.assertFalse(report["claim_boundary"]["dps_comparison_validated"])

    def test_recursive_local_imports_and_package_initializer_form_complete_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _small_project(root)
            report = build_fury_execution_source_identity_v2(
                project_root=root,
                required_relative_paths=("o2o_dps/a.py",),
            )

        self.assertEqual(
            [row["relative_path"] for row in report["files"]],
            [
                "o2o_dps/__init__.py",
                "o2o_dps/a.py",
                "o2o_dps/b.py",
                "o2o_dps/c.py",
            ],
        )
        self.assertEqual(report["file_count"], 4)

    def test_bundle_is_checkout_location_independent_and_content_sensitive(self):
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = Path(first_directory)
            second = Path(second_directory)
            _small_project(first)
            _small_project(second)
            one = build_fury_execution_source_identity_v2(
                project_root=first,
                required_relative_paths=("o2o_dps/a.py",),
            )
            two = build_fury_execution_source_identity_v2(
                project_root=second,
                required_relative_paths=("o2o_dps/a.py",),
            )
            _write(second, "o2o_dps/c.py", "VALUE = 4\n")
            changed = build_fury_execution_source_identity_v2(
                project_root=second,
                required_relative_paths=("o2o_dps/a.py",),
            )

        self.assertEqual(one["files"], two["files"])
        self.assertEqual(
            one["canonical_bundle"]["sha256"], two["canonical_bundle"]["sha256"]
        )
        self.assertNotEqual(
            one["canonical_bundle"]["sha256"],
            changed["canonical_bundle"]["sha256"],
        )
        self.assertNotIn(first_directory, json.dumps(one))

    def test_required_entrypoint_order_does_not_change_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _small_project(root)
            forward = build_fury_execution_source_identity_v2(
                project_root=root,
                required_relative_paths=("o2o_dps/a.py", "o2o_dps/b.py"),
            )
            reverse = build_fury_execution_source_identity_v2(
                project_root=root,
                required_relative_paths=("o2o_dps/b.py", "o2o_dps/a.py"),
            )
        self.assertEqual(forward, reverse)

    def test_missing_required_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "o2o_dps/__init__.py", "")
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "missing or inaccessible"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/missing.py",),
                )

    def test_missing_absolute_local_import_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "o2o_dps/__init__.py", "")
            _write(root, "o2o_dps/a.py", "import o2o_dps.missing\n")
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "imports missing local module"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py",),
                )

    def test_relative_import_cannot_escape_local_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "o2o_dps/__init__.py", "")
            _write(root, "o2o_dps/a.py", "from ..outside import VALUE\n")
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "relative import escapes"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py",),
                )

    def test_unparseable_python_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "o2o_dps/__init__.py", "")
            _write(root, "o2o_dps/a.py", "def broken(:\n")
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "not parseable Python"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py",),
                )

    def test_required_paths_reject_traversal_windows_form_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _small_project(root)
            for bad in (
                "../escape.py",
                "/absolute.py",
                "o2o_dps\\a.py",
                "C:/escape.py",
            ):
                with self.subTest(path=bad), self.assertRaises(
                    FuryExecutionSourceIdentityV2Error
                ):
                    build_fury_execution_source_identity_v2(
                        project_root=root,
                        required_relative_paths=(bad,),
                    )
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "duplicate required path"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py", "o2o_dps/a.py"),
                )
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error, "duplicate required path"
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py", "O2O_DPS/A.PY"),
                )

    def test_canonical_bundle_rejects_duplicate_unordered_and_extra_fields(self):
        row_a = {
            "relative_path": "o2o_dps/a.py",
            "size_bytes": 1,
            "sha256": "a" * 64,
        }
        row_b = {
            "relative_path": "o2o_dps/b.py",
            "size_bytes": 1,
            "sha256": "b" * 64,
        }
        with self.assertRaisesRegex(
            FuryExecutionSourceIdentityV2Error, "duplicate file identity"
        ):
            canonical_file_bundle_sha256((row_a, row_a))
        with self.assertRaisesRegex(
            FuryExecutionSourceIdentityV2Error, "strictly ordered"
        ):
            canonical_file_bundle_sha256((row_b, row_a))
        extra = dict(row_a, unexpected=True)
        with self.assertRaisesRegex(
            FuryExecutionSourceIdentityV2Error, "fields must be"
        ):
            canonical_file_bundle_sha256((extra,))

    def test_symlink_source_is_rejected_when_platform_allows_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "o2o_dps/__init__.py", "")
            _write(root, "real.py", "VALUE = 1\n")
            link = root / "o2o_dps" / "a.py"
            try:
                link.symlink_to(root / "real.py")
            except OSError:
                self.skipTest("test account cannot create symbolic links")
            with self.assertRaisesRegex(
                FuryExecutionSourceIdentityV2Error,
                "escapes the project root|symbolic link",
            ):
                build_fury_execution_source_identity_v2(
                    project_root=root,
                    required_relative_paths=("o2o_dps/a.py",),
                )


if __name__ == "__main__":
    unittest.main()
