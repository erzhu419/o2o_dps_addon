"""Fail-closed identities for frozen legacy and absolute-seed simulators."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BRIDGE_PATH = PROJECT_ROOT / "bin" / "o2obridge.exe"
ABSOLUTE_SEED_V1_BRIDGE_PATH = (
    PROJECT_ROOT / "bin" / "o2obridge.seedfix-v1.exe"
)
FROZEN_PHASE1_FIXTURE_PATH = (
    PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_phase1.json"
)

LEGACY_BRIDGE_SHA256 = (
    "7055b9a44e8296a5888b1122a4193130e6b5c99e49f99101f3982d5d7ba26065"
)
ABSOLUTE_SEED_V1_BRIDGE_SHA256 = (
    "3f455eada0cf962f10294cc0a0db1f5e698d9211cffe5a6b715028be6ed53a9d"
)
FROZEN_PHASE1_FIXTURE_SHA256 = (
    "1e578b15b2ccb497e137ad6de0555856612488eb86802481764e3c0e37b3728e"
)


class SimulatorBinaryContractError(RuntimeError):
    """A pinned simulator or historical fixture differs from its identity."""


def _identity(path: Path, expected_sha256: str, role: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise SimulatorBinaryContractError(
            f"cannot read pinned {role} {resolved}: {error}"
        ) from error
    observed = hashlib.sha256(raw).hexdigest()
    if observed != expected_sha256.casefold():
        raise SimulatorBinaryContractError(
            f"pinned {role} SHA-256 mismatch: expected {expected_sha256.casefold()}, "
            f"observed {observed}"
        )
    return {
        "role": role,
        "path": str(resolved),
        "size_bytes": len(raw),
        "sha256": observed,
    }


def verify_simulator_binary_contract() -> dict[str, Any]:
    """Verify both replay generations plus the immutable phase-1 fixture."""

    legacy = _identity(
        LEGACY_BRIDGE_PATH, LEGACY_BRIDGE_SHA256, "legacy_double_seed_bridge"
    )
    absolute = _identity(
        ABSOLUTE_SEED_V1_BRIDGE_PATH,
        ABSOLUTE_SEED_V1_BRIDGE_SHA256,
        "absolute_seed_v1_bridge",
    )
    fixture = _identity(
        FROZEN_PHASE1_FIXTURE_PATH,
        FROZEN_PHASE1_FIXTURE_SHA256,
        "frozen_phase1_fixture",
    )
    return {
        "schema_version": 1,
        "kind": "simulator_binary_contract_v1",
        "status": "PASS",
        "legacy": legacy,
        "absolute_seed_v1": absolute,
        "frozen_fixture": fixture,
        "semantics": {
            "legacy_reported_nonzero_seed_is_literal_absolute_seed": False,
            "legacy_effective_seed_rule": "2 * reported_seed",
            "legacy_paired_stream_comparisons_preserved": True,
            "absolute_seed_v1_reported_seed_is_literal_absolute_seed": True,
            "build_may_overwrite_frozen_artifacts": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Verify frozen legacy and absolute-seed simulator identities."
    )


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    print(json.dumps(verify_simulator_binary_contract(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
