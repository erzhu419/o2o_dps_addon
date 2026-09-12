from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from o2o_dps.deployed_contra_runtime_binding_v1 import (
    build_deployed_contra_runtime_binding_v1,
)
from o2o_dps.fury_runtime_bound_deployed_contra_adapter_v7 import (
    FuryRuntimeBoundDeployedContraAdapterV7Error,
    RAID_B_BLOCKER,
    RuntimeBoundContraDeployedFuryAdapterV7,
    UNSUPPORTED_CVAR_BLOCKER,
    UNSUPPORTED_HELPER_BLOCKER,
    raid_a_helper_call_inputs_v7,
)
from tests.test_deployed_contra_runtime_binding_v1 import _manifest, _snapshot
from tests.test_fury_contra_adapter_v2 import _state


def _binding(*, gates: bool = False, cvar_drift: bool = False):
    snapshot = _snapshot(uppercase_runtime_gates=gates)
    if cvar_drift:
        from o2o_dps.fury_expert_runtime_snapshot_v1 import sha256_json

        snapshot["nampower_cvars"]["NP_QueueSpellsOnCooldown"] = "1"
        snapshot["nampower_cvars_semantic_sha256"] = "6" * 64
        snapshot["snapshot_sha256"] = sha256_json(
            {
                key: value
                for key, value in snapshot.items()
                if key != "snapshot_sha256"
            }
        )
    directory = tempfile.TemporaryDirectory()
    manifest = _manifest(Path(directory.name))
    result = build_deployed_contra_runtime_binding_v1(
        source_manifest=manifest,
        runtime_snapshot=snapshot,
    )
    directory.cleanup()
    return result


class FuryRuntimeBoundDeployedContraAdapterV7Tests(unittest.TestCase):
    def test_f894_case_sensitive_gates_disable_burst_and_survival_inputs(self) -> None:
        binding = _binding()
        helper_inputs = raid_a_helper_call_inputs_v7(binding)

        self.assertFalse(helper_inputs["burst"])
        self.assertFalse(helper_inputs["survival"])
        self.assertFalse(helper_inputs["interrupt"])
        self.assertTrue(helper_inputs["support"])
        self.assertEqual(
            helper_inputs["source_order"],
            ["interrupt", "burst", "support", "survival"],
        )

        decision = RuntimeBoundContraDeployedFuryAdapterV7(binding).propose(
            _state()
        )
        self.assertTrue(decision.valid)
        self.assertEqual(
            decision.metadata["runtime_helper_call_inputs"], helper_inputs
        )
        self.assertEqual(
            decision.metadata["runtime_binding_sha256"],
            binding["binding_sha256"],
        )

    def test_enabling_untranslated_helper_gates_changes_inputs_and_fails_closed(
        self,
    ) -> None:
        disabled = raid_a_helper_call_inputs_v7(_binding())
        enabled_binding = _binding(gates=True)
        enabled = raid_a_helper_call_inputs_v7(enabled_binding)

        self.assertNotEqual(disabled, enabled)
        self.assertTrue(enabled["burst"])
        self.assertTrue(enabled["survival"])
        decision = RuntimeBoundContraDeployedFuryAdapterV7(
            enabled_binding
        ).propose(_state())
        self.assertFalse(decision.valid)
        self.assertIn(UNSUPPORTED_HELPER_BLOCKER, decision.reason)

    def test_raid_b_is_an_explicit_unimplemented_controller(self) -> None:
        with self.assertRaisesRegex(
            FuryRuntimeBoundDeployedContraAdapterV7Error, RAID_B_BLOCKER
        ):
            RuntimeBoundContraDeployedFuryAdapterV7(
                _binding(), controller="raid_b"
            )

    def test_unsupported_cvar_profile_has_identity_but_cannot_execute(self) -> None:
        with self.assertRaisesRegex(
            FuryRuntimeBoundDeployedContraAdapterV7Error,
            UNSUPPORTED_CVAR_BLOCKER,
        ):
            RuntimeBoundContraDeployedFuryAdapterV7(_binding(cvar_drift=True))


if __name__ == "__main__":
    unittest.main()
