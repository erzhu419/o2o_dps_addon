from __future__ import annotations

import unittest

from scripts.development_d900_v6_followup_plan_v1 import build_plan_v1


class D900V6FollowupPlanTests(unittest.TestCase):
    def test_new_identity_is_required_in_both_read_only_commands(self) -> None:
        new_result = "a" * 64
        new_model = "b" * 64
        new_store = "/server/new-v6.sqlite3"
        result = build_plan_v1(
            code_root="/server/current-source",
            simulator_root="/server/simulator",
            frozen_dispatch="/server/frozen-old-split.json",
            runtime_store=new_store,
            result_sha=new_result,
            model_sha=new_model,
        )
        self.assertEqual("COMMANDS_ONLY_NOT_EXECUTED", result["status"])
        for key in ("score", "full_wave"):
            command = result[key]["argv"]
            self.assertIn(new_store, command)
            self.assertIn(new_result, command)
            self.assertIn(new_model, command)
            self.assertNotIn("1389ce06506b89d7c2040befdf24320bb0eb557d798dded4b0edb9dfa935bfe4", command)
            self.assertNotIn("dc4022d86393823d8df67f890805348438126735e73c5d40234cd915e45da7f2", command)
        wave = result["full_wave"]["argv"]
        self.assertEqual("all", wave[wave.index("--source") + 1])
        self.assertIn("--route-focus", wave)
        self.assertEqual("OBSERVED_ONSET_UNTIL_SIM_DEATH", wave[wave.index("--attackability-mode") + 1])
        self.assertEqual("2026092001", wave[wave.index("--simulator-seed") + 1])
        self.assertEqual("2026092002", wave[wave.index("--teammate-seed") + 1])
        self.assertEqual("1", wave[wave.index("--seed-count") + 1])
        self.assertNotIn("--historical-first-wake-delay-diagnostic", wave)
        self.assertIn("o2o_dps/responsive_team_bridge_adapter_v1.py", result["source_files_to_stage_before_execution"])
        self.assertIn("o2o_dps/sim_bridge_dynamic_v4.py", result["source_files_to_stage_before_execution"])

    def test_old_frozen_result_cannot_be_reused_as_new_head(self) -> None:
        with self.assertRaisesRegex(ValueError, "old v4"):
            build_plan_v1(
                code_root="/server/current-source",
                simulator_root="/server/simulator",
                frozen_dispatch="/server/frozen-old-split.json",
                runtime_store="/server/old.sqlite3",
                result_sha="1389ce06506b89d7c2040befdf24320bb0eb557d798dded4b0edb9dfa935bfe4",
                model_sha="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
