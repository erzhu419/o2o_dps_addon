from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_prefix_reconstruction_gate_v1 import (
    DEFAULT_BRIDGE,
    DEFAULT_CONFIG,
    DEFAULT_SEEDS,
    EXPECTED_BRIDGE_SHA256,
    EXPECTED_CONFIG_SHA256,
    LANES,
    canonical_bytes,
    derive_requests,
    first_difference,
    run_gate,
    sha256_bytes,
    sha256_file,
)


class FuryPrefixReconstructionGateV1Tests(unittest.TestCase):
    def test_canonical_bytes_are_key_order_independent(self) -> None:
        left = {"z": [3, {"b": False, "a": 1.25}], "a": "x"}
        right = {"a": "x", "z": [3, {"a": 1.25, "b": False}]}
        self.assertEqual(canonical_bytes(left), canonical_bytes(right))
        self.assertEqual(sha256_bytes(canonical_bytes(left)), sha256_bytes(canonical_bytes(right)))

    def test_derive_requests_is_pure_and_fixes_the_lane_controls(self) -> None:
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        before = deepcopy(config)
        requests = derive_requests(config, config_sha256=EXPECTED_CONFIG_SHA256)

        self.assertEqual(config, before)
        self.assertEqual(set(requests), {"single_target", "three_target"})
        self.assertEqual(len(requests["single_target"]["encounter"]["targets"]), 1)
        self.assertEqual(len(requests["three_target"]["encounter"]["targets"]), 3)
        for request in requests.values():
            self.assertEqual(request["encounter"]["duration"], 12)
            self.assertEqual(request["simOptions"]["iterations"], 1)
            self.assertIs(request["simOptions"]["interactive"], True)

    def test_lane_contract_covers_the_three_requested_prefix_shapes(self) -> None:
        by_id = {lane["lane_id"]: lane for lane in LANES}
        self.assertEqual(
            set(by_id),
            {
                "queue_cancel_wait_advance",
                "stance_offgcd_gcd_swing",
                "multitarget_settarget_queue_gcd",
            },
        )
        commands = {
            lane_id: [step["command"] for step in lane["steps"]]
            for lane_id, lane in by_id.items()
        }
        self.assertEqual(
            commands["queue_cancel_wait_advance"],
            [
                "load",
                "act",
                "wait",
                "advance",
                "cancel_queue",
                "wait",
                "advance",
            ],
        )
        self.assertEqual(
            commands["stance_offgcd_gcd_swing"],
            ["load", "act", "act", "act", "advance", "wait", "advance"],
        )
        self.assertEqual(
            commands["multitarget_settarget_queue_gcd"],
            ["load", "set_target", "act", "act", "advance", "wait", "advance"],
        )

    def test_first_difference_names_the_first_nested_mismatch(self) -> None:
        left = {"steps": [{"state": {"damage_done": 10.0}}]}
        right = {"steps": [{"state": {"damage_done": 11.0}}]}
        difference = first_difference(left, right)
        self.assertEqual(difference["path"], "$.steps[0].state.damage_done")
        self.assertEqual(difference["reference"], 10.0)
        self.assertEqual(difference["replay"], 11.0)

    @unittest.skipUnless(DEFAULT_BRIDGE.exists(), "seed-fixed bridge is not present")
    def test_real_bridge_replays_each_prefix_twice_from_time_zero(self) -> None:
        if sha256_file(DEFAULT_BRIDGE) != EXPECTED_BRIDGE_SHA256:
            self.skipTest("seed-fixed bridge bytes do not match the gate lock")
        if sha256_file(DEFAULT_CONFIG) != EXPECTED_CONFIG_SHA256:
            self.skipTest("base configuration bytes do not match the gate lock")

        with tempfile.TemporaryDirectory() as directory:
            report = run_gate(
                bridge_path=DEFAULT_BRIDGE,
                config_path=DEFAULT_CONFIG,
                input_directory=Path(directory) / "inputs",
                seeds=DEFAULT_SEEDS,
            )

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["prefix_reconstruction_gate_passed"])
        self.assertEqual(report["execution"]["expected_episode_count"], 18)
        self.assertEqual(report["execution"]["completed_episode_count"], 18)
        self.assertEqual(report["execution"]["matched_replay_count"], 12)
        self.assertEqual(report["execution"]["mismatch_count"], 0)
        self.assertTrue(report["execution"]["all_invariants_passed"])
        self.assertEqual(report["scope"]["reconstruction_mode"], "REPLAY_FROM_TIME_ZERO")
        self.assertEqual(report["scope"]["state_source"], "SIMULATED_PREFIX_ONLY")
        self.assertFalse(report["capabilities"]["snapshot_restore"])
        self.assertFalse(report["capabilities"]["historical_midstate_seeding"])
        self.assertFalse(report["capabilities"]["shadow_backfill"])
        self.assertFalse(report["authorization"]["training_allowed"])
        self.assertFalse(report["authorization"]["voting_allowed"])
        self.assertFalse(report["authorization"]["deployment_allowed"])
        self.assertFalse(report["authorization"]["real_game_superiority_claimed"])


if __name__ == "__main__":
    unittest.main()
