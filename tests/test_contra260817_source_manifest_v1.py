from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra260817_source_manifest_v1 import (
    CODE_MANIFEST_SHA256,
    DEFAULT_MANIFEST,
    DEFAULT_SOURCE_ROOT,
    EXPECTED_MANIFEST_SHA256,
    Contra260817ManifestError,
    audit_toc,
    build_code_manifest,
    verify_contra260817_source_package,
)


class Contra260817SourceManifestV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        DEFAULT_SOURCE_ROOT.is_dir(), "external Contra_new source tree is not available"
    )
    def test_code_manifest_pins_all_33_code_files(self) -> None:
        identity, entries = build_code_manifest(DEFAULT_SOURCE_ROOT)

        self.assertEqual(identity.file_count, 33)
        self.assertEqual(identity.byte_count, 4_285_993)
        self.assertEqual(identity.sha256, CODE_MANIFEST_SHA256)
        import hashlib
        self.assertEqual(
            hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(),
            EXPECTED_MANIFEST_SHA256,
        )
        self.assertEqual(
            [entry["relative_path"] for entry in entries],
            sorted(entry["relative_path"] for entry in entries),
        )
        self.assertEqual(
            [
                entry["relative_path"]
                for entry in entries
                if entry["size_bytes"] == 0
            ],
            [
                "Contra_PaladinRetribution.lua",
                "Contra_TrinketManager.lua",
            ],
        )

    @unittest.skipUnless(
        DEFAULT_SOURCE_ROOT.is_dir(), "external Contra_new source tree is not available"
    )
    def test_toc_missing_and_empty_members_are_explicit(self) -> None:
        metadata, audit = audit_toc(DEFAULT_SOURCE_ROOT)

        self.assertEqual(metadata["interface"], "11200")
        self.assertEqual(metadata["version"], "4.04")
        self.assertEqual(metadata["savedvariablespercharacter"], "ContraDB")
        self.assertEqual(audit["load_entry_count"], 31)
        self.assertEqual(audit["present_entry_count"], 29)
        self.assertEqual(
            audit["missing_members"],
            ["Contra_Debuff.lua", "Contra_UI_DB.lua"],
        )
        self.assertEqual(
            audit["empty_members"], ["Contra_TrinketManager.lua"]
        )
        self.assertEqual(
            audit["unlisted_lua_files"],
            ["Contra_PaladinRetribution.lua", "Contra_log.lua"],
        )

    @unittest.skipUnless(
        DEFAULT_SOURCE_ROOT.is_dir(), "external Contra_new source tree is not available"
    )
    def test_verified_identity_remains_known_incomplete_and_non_comparable(self) -> None:
        report = verify_contra260817_source_package()

        self.assertTrue(report["source_identity_verified"])
        self.assertEqual(
            report["result"], "IDENTITY_VERIFIED_PACKAGE_INCOMPLETE"
        )
        self.assertFalse(report["package_complete"])
        self.assertFalse(report["comparison_eligible"])
        self.assertFalse(report["eligible_for_independent_vote"])
        self.assertFalse(report["baseline_sealed"])
        self.assertEqual(
            report["package_blockers"],
            [
                "missing_toc_member:Contra_Debuff.lua",
                "missing_toc_member:Contra_UI_DB.lua",
                "empty_code_file:Contra_PaladinRetribution.lua",
                "empty_code_file:Contra_TrinketManager.lua",
            ],
        )
        self.assertTrue(all(report["source_anchor_checks"].values()))

    def test_manifest_records_fury_entry_config_state_and_sink_contracts(self) -> None:
        fury = self.manifest["fury"]

        self.assertEqual(
            fury["entrypoints"]["single_target"]["two_hand_raid"],
            "Contra_SSKBZ_A",
        )
        self.assertEqual(
            fury["entrypoints"]["multi_target"]["dual_wield"],
            "Contra_SCKBZ_B",
        )
        self.assertEqual(
            fury["entrypoints"]["automatic_single_multi_route"][
                "group_condition"
            ],
            "nearbyCount > 1",
        )
        self.assertIn("xuanfeng", fury["configuration_inputs"]["routing"])
        self.assertIn(
            "SunderMode", fury["configuration_inputs"]["prelude_switches"]
        )
        self.assertIn("max_health", fury["state_inputs"]["target"])
        self.assertTrue(
            any(
                "baofa/shengcun" in value and "Burst/Survive" in value
                for value in fury["configuration_inputs"][
                    "source_configuration_hazards"
                ]
            )
        )
        sinks = fury["ordered_sink_semantics"]
        self.assertIn("multiple ordered sinks", sinks["decision_shape"])
        self.assertIn("preserve every raw sink", sinks["normalization_rule"])
        self.assertIn("not general cast-success", sinks["return_rule"])

    def test_remote_runtime_can_bind_source_and_manifest_independently(self) -> None:
        source_root = PROJECT_ROOT / "remote-fixture" / "Contra_new"
        manifest = PROJECT_ROOT / "remote-fixture" / "manifest.json"
        environment = os.environ.copy()
        environment["BOC_CONTRA260817_ROOT"] = str(source_root)
        environment["BOC_CONTRA260817_MANIFEST"] = str(manifest)
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                (
                    "from o2o_dps.contra260817_source_manifest_v1 import "
                    "DEFAULT_MANIFEST,DEFAULT_SOURCE_ROOT; "
                    "print(DEFAULT_SOURCE_ROOT); print(DEFAULT_MANIFEST)"
                ),
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [str(source_root), str(manifest)],
        )

    def test_reuse_map_is_bounded_and_not_an_equivalence_claim(self) -> None:
        reuse = self.manifest["deployed_v2_reuse_map"]

        self.assertEqual(
            reuse["reusable_two_hand_raid_a_branch_skeleton"]["status"],
            "SOURCE_SIMILAR_NOT_RUNTIME_EQUIVALENT",
        )
        self.assertIn(
            "loadout-derived ZSSDW", reuse["reusable_state_fields"]
        )
        self.assertTrue(
            any(
                "CastSpellByName" in value
                for value in reuse["differences_requiring_new_adapter_evidence"]
            )
        )
        self.assertIn("do not substitute", reuse["reuse_prohibition"])

    def test_manifest_cannot_promote_or_drift_the_pinned_identity(self) -> None:
        mutations = []

        promoted = copy.deepcopy(self.manifest)
        promoted["closure"]["comparison_eligible"] = True
        mutations.append((promoted, "comparison_eligible"))

        sealed = copy.deepcopy(self.manifest)
        sealed["closure"]["baseline_sealed"] = True
        mutations.append((sealed, "baseline_sealed"))

        digest = copy.deepcopy(self.manifest)
        digest["identity"]["code_manifest"]["sha256"] = "0" * 64
        mutations.append((digest, "code_manifest"))

        equivalence = copy.deepcopy(self.manifest)
        equivalence["deployed_v2_reuse_map"][
            "reusable_two_hand_raid_a_branch_skeleton"
        ]["status"] = "EXACT_RUNTIME_EQUIVALENT"
        mutations.append((equivalence, "reuse status"))

        for document, expected_error in mutations:
            with self.subTest(expected_error=expected_error):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "manifest.json"
                    path.write_text(
                        json.dumps(document, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        Contra260817ManifestError, "manifest SHA-256 mismatch"
                    ):
                        verify_contra260817_source_package(
                            manifest_path=path,
                            source_root=DEFAULT_SOURCE_ROOT,
                        )


if __name__ == "__main__":
    unittest.main()
