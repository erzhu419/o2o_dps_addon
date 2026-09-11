from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_simulator_linux.ps1"


class BuildSimulatorLinuxScriptV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BUILD_SCRIPT.read_text(encoding="utf-8")

    def test_default_name_carries_every_build_semantic(self) -> None:
        self.assertIn(
            "BridgeFileName = 'o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64'",
            self.source,
        )
        self.assertIn("$versionedNamePattern", self.source)
        self.assertIn("seedfix-v[1-9][0-9]*", self.source)
        self.assertIn("withdb", self.source)
        self.assertIn("goamd64v1", self.source)

    def test_failed_legacy_and_unversioned_names_are_forbidden(self) -> None:
        self.assertIn("'o2obridge'", self.source)
        self.assertIn("'o2obridge.linux-amd64'", self.source)
        self.assertIn("'o2obridge.seedfix-v1.linux-amd64'", self.source)
        self.assertIn("$forbiddenBridgeNames -icontains $BridgeFileName", self.source)

    def test_cross_compile_environment_is_exact(self) -> None:
        self.assertIn("$env:CGO_ENABLED = '0'", self.source)
        self.assertIn("$env:GOOS = 'linux'", self.source)
        self.assertIn("$env:GOARCH = 'amd64'", self.source)
        self.assertIn("$env:GOAMD64 = 'v1'", self.source)
        self.assertIn("$env:GOFLAGS = ''", self.source)
        self.assertNotIn("$env:GOAMD64 = 'v2'", self.source)

    def test_build_has_database_trimpath_and_empty_build_id(self) -> None:
        build_line = next(
            line.strip()
            for line in self.source.splitlines()
            if line.strip().startswith("& $goExecutable build")
        )
        self.assertIn("-buildvcs=false", build_line)
        self.assertIn("-trimpath", build_line)
        self.assertIn("--tags=with_db", build_line)
        self.assertIn("'-ldflags=-s -w -buildid='", build_line)
        self.assertIn("-o $temporaryBridge", build_line)
        self.assertNotIn("-o $bridge", build_line)

    def test_staging_is_reused_only_for_identical_sha256(self) -> None:
        self.assertIn("Install-ImmutableArtifact", self.source)
        self.assertIn("Get-FileHash -Algorithm SHA256", self.source)
        self.assertIn("$existingHash -cne $stagedHash", self.source)
        self.assertIn("already exists with different bytes", self.source)
        self.assertIn("return 'REUSED_IDENTICAL'", self.source)
        self.assertIn("return 'INSTALLED_NEW'", self.source)
        self.assertIn(".o2obridge-linux-build-", self.source)
        self.assertIn("Remove-Item -LiteralPath $temporaryBridge", self.source)

    def test_output_exposes_identity_and_full_build_contract(self) -> None:
        self.assertIn('Write-Output "bridge_install=$installStatus"', self.source)
        self.assertIn('Write-Output "bridge_size_bytes=', self.source)
        self.assertIn('Write-Output "bridge_sha256=', self.source)
        self.assertIn(
            "build_contract=GOOS=linux;GOARCH=amd64;GOAMD64=v1;CGO_ENABLED=0;GOFLAGS=empty;buildvcs=false;trimpath=true;tags=with_db;buildid=empty",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
