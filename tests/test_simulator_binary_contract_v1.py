from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from o2o_dps.simulator_binary_contract_v1 import (
    ABSOLUTE_SEED_V1_BRIDGE_PATH,
    ABSOLUTE_SEED_V1_BRIDGE_SHA256,
    FROZEN_PHASE1_FIXTURE_PATH,
    FROZEN_PHASE1_FIXTURE_SHA256,
    LEGACY_BRIDGE_PATH,
    LEGACY_BRIDGE_SHA256,
    SimulatorBinaryContractError,
    _identity,
    verify_simulator_binary_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_simulator_windows.ps1"


class SimulatorBinaryContractV1Tests(unittest.TestCase):
    def test_workspace_identities_are_pinned_and_distinct(self) -> None:
        report = verify_simulator_binary_contract()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["legacy"]["sha256"], LEGACY_BRIDGE_SHA256)
        self.assertEqual(
            report["absolute_seed_v1"]["sha256"],
            ABSOLUTE_SEED_V1_BRIDGE_SHA256,
        )
        self.assertEqual(
            report["frozen_fixture"]["sha256"], FROZEN_PHASE1_FIXTURE_SHA256
        )
        self.assertNotEqual(LEGACY_BRIDGE_PATH, ABSOLUTE_SEED_V1_BRIDGE_PATH)
        self.assertNotEqual(LEGACY_BRIDGE_SHA256, ABSOLUTE_SEED_V1_BRIDGE_SHA256)
        self.assertEqual(
            hashlib.sha256(FROZEN_PHASE1_FIXTURE_PATH.read_bytes()).hexdigest(),
            FROZEN_PHASE1_FIXTURE_SHA256,
        )

    def test_identity_fails_closed_on_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.exe"
            path.write_bytes(b"different")
            with self.assertRaises(SimulatorBinaryContractError):
                _identity(path, "0" * 64, "test_bridge")

    def test_windows_builder_protects_frozen_names(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("BridgeFileName = 'o2obridge.seedfix-v1.exe'", source)
        self.assertIn("$BridgeFileName -ieq 'o2obridge.exe'", source)
        self.assertIn("$FixtureFileName -ieq 'fury_warrior_phase1.json'", source)
        self.assertIn("Install-ImmutableArtifact", source)
        self.assertNotIn("-o $bridge .\\cmd\\o2obridge", source)


if __name__ == "__main__":
    unittest.main()
