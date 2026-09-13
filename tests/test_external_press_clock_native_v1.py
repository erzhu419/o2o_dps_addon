"""Small native Windows smoke for the opt-in dynamic-v3 press clock."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_team_retarget_v1 import (
    SourceFittedTeamRetargetBridgeV1,
    V14ProjectedDynamicV3Bridge,
    build_retarget_wave_case_v1,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v18.exe"


@unittest.skipUnless(os.name == "nt" and BRIDGE.is_file(), "native v18 bridge required")
class NativeExternalPressClockTests(unittest.TestCase):
    def test_dynamic_wave_uses_only_external_key_grid(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            loaded = bridge.load_dynamic_v3(case.request, seed, case.dynamic_load.config)
            self.assertFalse(loaded.state["finished"])
            configured = bridge.configure_press_clock(100, 0)
            self.assertFalse(configured["press_clock"]["ready"])
            for index, expected_time in enumerate((0, 100, 200, 300), start=1):
                state = bridge.advance()
                self.assertEqual(expected_time, state["time_ms"])
                self.assertTrue(state["needs_input"])
                self.assertEqual(index, state["press_clock"]["press_index"])
                self.assertTrue(state["press_clock"]["ready"])
                closed = bridge.finish_press()
                self.assertFalse(closed["needs_input"])
                self.assertFalse(closed["press_clock"]["ready"])

    def test_responsive_wakes_do_not_create_extra_keys(self) -> None:
        seed = 2026091401
        case, _, events = build_retarget_wave_case_v1(seed)
        identity = case.case_spec["team_background"]["source_schedule_content_sha256"]
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as native:
            bridge = SourceFittedTeamRetargetBridgeV1(native, events, identity)
            bridge.load_dynamic_v3(case.request, seed, case.dynamic_load.config)
            native.configure_press_clock(100, 0)
            for index in range(1, 7):
                state = bridge.advance()
                self.assertEqual((index - 1) * 100, state["time_ms"])
                self.assertEqual(index, state["press_clock"]["press_index"])
                self.assertTrue(state["press_clock"]["ready"])
                native.finish_press()
            self.assertEqual(2, len(bridge._receipts))
            self.assertEqual([500, 500], [row["time_ms"] for row in bridge._receipts])


if __name__ == "__main__":
    unittest.main()
