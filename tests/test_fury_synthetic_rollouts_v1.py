from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.fury_policy_optimization_v1 import (
    FuryPolicyParameters,
    PolicyScenario,
)
from o2o_dps.fury_synthetic_rollouts_v1 import (
    generate_fury_synthetic_rollouts,
)
from o2o_dps.sim_bridge import ActResult, AvailableAction, CancelQueueResult


BLOODTHIRST = "warrior.bloodthirst"


def _request() -> dict[str, object]:
    return {
        "raid": {
            "parties": [
                {"players": [{"distanceFromTarget": 3, "equipment": {"items": []}}]}
            ]
        },
        "encounter": {
            "duration": 300,
            "targets": [{"level": 63, "name": "Synthetic Fixture"}],
        },
        "simOptions": {"iterations": 1},
    }


class _WaitBridge:
    def __init__(self, *, bloodthirst_legal: bool = False) -> None:
        self.bloodthirst_legal = bloodthirst_legal
        self.duration_ms = 0
        self.time_ms = 0
        self.pending_wait_ms = 0
        self.needs_input = True
        self.finished = False
        self.loaded_requests = []

    def load(self, request, seed):
        self.loaded_requests.append((seed, request))
        self.duration_ms = round(float(request["encounter"]["duration"]) * 1000)
        self.time_ms = 0
        self.pending_wait_ms = 0
        self.needs_input = True
        self.finished = False
        return self._state()

    def actions(self):
        return [
            AvailableAction(
                index=0,
                action=ACTION_KEY_TO_REF[BLOODTHIRST],
                label="Bloodthirst",
                legal=self.needs_input and self.bloodthirst_legal,
                ready_in_ms=0 if self.bloodthirst_legal else 1000,
                triggers_gcd=True,
            ),
            AvailableAction(
                index=1,
                action=ACTION_KEY_TO_REF["warrior.whirlwind"],
                label="Whirlwind",
                legal=False,
                ready_in_ms=1000,
                triggers_gcd=True,
            ),
        ]

    def act(self, action):
        if action != ACTION_KEY_TO_REF[BLOODTHIRST] or not self.bloodthirst_legal:
            return ActResult(False, False, self.finished, self.needs_input, self._state())
        self.needs_input = False
        self.pending_wait_ms = 1500
        return ActResult(True, True, self.finished, self.needs_input, self._state())

    def cancel_queue(self):
        return CancelQueueResult(False, False, self.finished, self.needs_input, self._state())

    def wait(self, wait_ms):
        self.pending_wait_ms = wait_ms
        self.needs_input = False
        return self._state()

    def advance(self):
        self.time_ms = min(self.duration_ms, self.time_ms + self.pending_wait_ms)
        self.pending_wait_ms = 0
        self.finished = self.time_ms >= self.duration_ms
        self.needs_input = not self.finished
        return self._state()

    def _state(self):
        return {
            "time_ms": self.time_ms,
            "remaining_ms": max(0, self.duration_ms - self.time_ms),
            "finished": self.finished,
            "needs_input": self.needs_input,
            "power": {"type": "Rage", "current": 0.0, "maximum": 100.0},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1000,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": 800,
            "current_cast": None,
            "target_health_known": False,
            "target_health_percent": 0.0,
            "execute_phase_20": False,
            "target_armor": 3000.0,
            "effective_target_armor": 3000.0,
            "damage_done": 0.0,
            "auras": [
                {"label": "Battle Shout", "remaining_ms": 600_000, "stacks": 0},
                {"label": "Berserker Stance", "remaining_ms": 1, "stacks": 0},
            ],
        }


class _IllegalBloodthirstAdapter:
    expert_id = "test.illegal_bloodthirst"

    def propose(self, state):
        return ExpertDecision(
            provenance=ExpertProvenance(
                expert_id=self.expert_id,
                kind=ProvenanceKind.SOURCE_DERIVED,
                role=ExpertRole.CANDIDATE,
                authority_files=("unit-test",),
            ),
            valid=True,
            gcd=BLOODTHIRST,
            wait_ms=None,
            eligible_for_independent_vote=False,
            reason="unit-test illegal action",
        )


def _scenario(scenario_id: str, horizon_ms: int) -> PolicyScenario:
    return PolicyScenario(
        scenario_id=scenario_id,
        request=_request(),
        horizon_ms=horizon_ms,
        provenance={
            "source": "unit_fixture",
            "chronicle_derived": False,
        },
    )


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FurySyntheticRolloutsV1Tests(unittest.TestCase):
    def test_streams_compact_simulated_transitions_and_small_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transitions = root / "transitions.jsonl"
            manifest_path = root / "manifest.json"
            bridge = _WaitBridge()
            manifest = generate_fury_synthetic_rollouts(
                bridge,
                FuryPolicyParameters(use_death_wish=False),
                (_scenario("single", 200),),
                seeds=(1, 2),
                transitions_path=transitions,
                manifest_path=manifest_path,
            )

            rows = _read_jsonl(transitions)
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["data_origin"] == "SIMULATED" for row in rows))
            self.assertTrue(all(row["training_eligible"] for row in rows))
            self.assertTrue(all(not row["source_execution"] for row in rows))
            self.assertTrue(all(not row["exact_lua_replay"] for row in rows))
            self.assertEqual(rows[0]["scenario_provenance_ref"]["scenario_id"], "single")
            self.assertIn("state_before", rows[0])
            self.assertIn("expert_state", rows[0])
            self.assertIn("proposal", rows[0])
            self.assertIn("executed_commands", rows[0])
            self.assertIn("next_state", rows[0])
            self.assertEqual(manifest["counts"]["transitions"], 4)
            self.assertEqual(manifest["counts"]["rollouts"], 2)
            self.assertFalse(manifest["generation_contract"]["full_rollout_steps_retained"])
            self.assertFalse(manifest["generation_contract"]["chronicle_raw_data_read"])
            self.assertTrue(
                all("steps" not in row for row in manifest["rollout_summaries"])
            )
            self.assertLess(manifest_path.stat().st_size, 1_000_000)
            self.assertTrue(
                all(
                    request["encounter"]["durationVariation"] == 0.0
                    for _, request in bridge.loaded_requests
                )
            )

    def test_terminal_wait_cap_is_boundary_not_lane_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transitions = root / "transitions.jsonl"
            manifest_path = root / "manifest.json"
            manifest = generate_fury_synthetic_rollouts(
                _WaitBridge(),
                FuryPolicyParameters(use_death_wish=False),
                (_scenario("cap", 150),),
                seeds=(3,),
                transitions_path=transitions,
                manifest_path=manifest_path,
            )

            rows = _read_jsonl(transitions)
            self.assertEqual(len(rows), 2)
            boundary = rows[-1]
            self.assertTrue(boundary["terminal"])
            self.assertTrue(boundary["boundary_event"])
            self.assertEqual(boundary["boundary_event_kind"], "TERMINAL_WAIT_CAP")
            self.assertTrue(boundary["lane_projection_faithful"])
            self.assertEqual(boundary["lane_nonfaithful_reasons"], [])
            self.assertFalse(boundary["training_eligible"])
            self.assertEqual(
                boundary["training_exclusion_reason"],
                "terminal_horizon_wait_cap_boundary",
            )
            self.assertEqual(manifest["counts"]["terminal_boundary_events"], 1)
            self.assertEqual(manifest["counts"]["lane_nonfaithful_transitions"], 0)

    def test_illegal_lane_is_excluded_and_omission_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transitions = root / "transitions.jsonl"
            manifest_path = root / "manifest.json"
            manifest = generate_fury_synthetic_rollouts(
                _WaitBridge(),
                _IllegalBloodthirstAdapter(),
                (_scenario("illegal", 100),),
                seeds=(4,),
                transitions_path=transitions,
                manifest_path=manifest_path,
            )

            row = _read_jsonl(transitions)[0]
            self.assertFalse(row["training_eligible"])
            self.assertFalse(row["lane_projection_faithful"])
            self.assertEqual(row["training_exclusion_reason"], "omitted_lane")
            self.assertEqual(row["omissions"][0]["lane"], "gcd")
            self.assertEqual(manifest["counts"]["omitted_lanes"], 1)
            self.assertEqual(manifest["policy"]["policy_id"], "test.illegal_bloodthirst")
            self.assertIsNone(manifest["policy"]["parameters"])

    def test_max_rollouts_caps_cross_product_without_copying_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = generate_fury_synthetic_rollouts(
                _WaitBridge(),
                FuryPolicyParameters(use_death_wish=False),
                (_scenario("one", 100), _scenario("two", 100)),
                seeds=(1, 2),
                transitions_path=root / "transitions.jsonl",
                manifest_path=root / "manifest.json",
                max_rollouts=3,
            )

            self.assertEqual(manifest["limits"]["possible_rollouts"], 4)
            self.assertEqual(manifest["limits"]["emitted_rollouts"], 3)
            self.assertEqual(manifest["counts"]["rollouts"], 3)
            self.assertEqual(manifest["counts"]["transitions"], 3)
            self.assertTrue(
                all("request" not in row for row in manifest["scenarios"])
            )


if __name__ == "__main__":
    unittest.main()
