from __future__ import annotations

from pathlib import Path
import unittest

from o2o_dps.sim_bridge_dynamic_v4 import (
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from o2o_dps.sim_bridge_dynamic_v5 import (
    CooldownCheckpointV1,
    PlayerCheckpointError,
    PlayerStateCheckpointV1,
    SimulatorBridgeDynamicV5,
    player_checkpoint_from_shadow_v1,
)
from tests.test_sim_bridge_dynamic_v3 import external_request_v3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.v15.playercheckpoint.withdb.goamd64v1.windows-amd64.exe"


class TestPlayerCheckpointV1(unittest.TestCase):
    def test_nonrestorable_queue_or_proc_rejected(self) -> None:
        base = dict(
            rage_current=47,
            stance="BATTLE",
            gcd_remaining_ms=700,
            cooldowns=(),
            mh_swing_remaining_ms=350,
            oh_swing_remaining_ms=650,
            queued_next_swing="NONE",
        )
        with self.assertRaisesRegex(PlayerCheckpointError, "queued next-swing"):
            PlayerStateCheckpointV1(**(base | {"queued_next_swing": "HEROIC_STRIKE"}))
        with self.assertRaisesRegex(PlayerCheckpointError, "aura/proc"):
            PlayerStateCheckpointV1(**(base | {"self_auras_and_procs": ({"spellId": 12970},)}))

    def test_current_shadow_importer_cannot_promote_visible_target_to_exact_registry(self) -> None:
        # The importer itself adds target.registry_scope to every current v1
        # row; the bridge conversion must not accept that single target as a
        # complete historical window.
        from tests.test_brainofcat_shadow_checkpoint_v1 import _base, _field

        record = _base()
        fields = record["fields"]
        for name, value in {
            "timers.gcd_remaining_ms": 700,
            "timers.cooldowns_remaining_ms": [],
            "timers.main_hand_swing_remaining_ms": 350,
            "timers.off_hand_swing_remaining_ms": 650,
            "queue.next_swing": "NONE",
            "player.self_auras_and_procs": [],
            "target.candidate_owned_existing_debuffs": [],
        }.items():
            fields[name] = _field("EXACT", value)
        with self.assertRaisesRegex(PlayerCheckpointError, "target.registry_scope"):
            player_checkpoint_from_shadow_v1(record)

    @unittest.skipUnless(BRIDGE.is_file(), "v15 Windows bridge binary is not built")
    def test_real_bridge_load_restores_pre_event_state(self) -> None:
        config = DynamicTargetSemanticsConfigV4(
            target_health=(DynamicTargetHealthV4(0, 1_000_000.0, 500_000.0),),
            idle_advance_horizon_ms=3000,
        )
        request = external_request_v3()
        request["encounter"]["duration"] = 3.0
        request["encounter"]["targets"][0]["stats"][34] = 1_000_000.0
        checkpoint = PlayerStateCheckpointV1(
            rage_current=47.0,
            stance="BATTLE",
            gcd_remaining_ms=700,
            cooldowns=(CooldownCheckpointV1(23894, 800),),
            mh_swing_remaining_ms=350,
            oh_swing_remaining_ms=650,
            queued_next_swing="NONE",
        )
        bridge = SimulatorBridgeDynamicV5(BRIDGE, cwd=SIM_ROOT)
        try:
            loaded = bridge.load_dynamic_v5(request, 101, config, checkpoint)
            state = loaded.state
            self.assertEqual(state["time_ms"], 0)
            self.assertEqual(state["target_health"], 500_000)
            self.assertEqual(state["power"]["current"], 47)
            self.assertEqual(state["gcd_remaining_ms"], 700)
            self.assertEqual(state["mh_swing_remaining_ms"], 350)
            self.assertEqual(state["oh_swing_remaining_ms"], 650)
            advanced = bridge.advance()
            self.assertGreaterEqual(advanced["time_ms"], 700)
        finally:
            bridge.close()
            bridge._process.stdout.close()
            bridge._process.stderr.close()


if __name__ == "__main__":
    unittest.main()
