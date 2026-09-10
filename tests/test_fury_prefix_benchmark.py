from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.beam_search import FactorizedDecision
from o2o_dps.fury_prefix_benchmark import (
    PrefixBenchmarkError,
    PrefixCandidate,
    benchmark_prefix_candidates,
    evaluate_prefix_candidate,
)
from o2o_dps.sim_bridge import ActionRef, ActResult, CancelQueueResult


HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)
BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)


class _Bridge:
    def __init__(self) -> None:
        self.time = 0
        self.damage = 0.0
        self.queued = False
        self.waiting = 0

    def load(self, request, seed):
        self.time = 0
        self.damage = float(seed)
        self.queued = False
        self.waiting = 0
        return self._state(True)

    def actions(self):
        return []

    def act(self, action):
        if action == HEROIC_STRIKE:
            self.queued = True
            return ActResult(True, False, False, True, self._state(True))
        if action not in (BLOODTHIRST, WHIRLWIND):
            return ActResult(False, False, False, True, self._state(True))
        self.damage += 30 if action == BLOODTHIRST else 20
        if self.queued:
            self.damage += 10
            self.queued = False
        self.waiting = 1500
        return ActResult(True, True, False, False, self._state(False))

    def cancel_queue(self):
        had_queue = self.queued
        self.queued = False
        return CancelQueueResult(had_queue, False, False, True, self._state(True))

    def wait(self, wait_ms):
        self.waiting = wait_ms
        return self._state(False)

    def advance(self):
        self.time += self.waiting
        self.damage += self.waiting / 1000.0
        self.waiting = 0
        return self._state(True)

    def _state(self, needs_input):
        return {
            "time_ms": self.time,
            "damage_done": self.damage,
            "needs_input": needs_input,
            "finished": False,
        }


class FuryPrefixBenchmarkTests(unittest.TestCase):
    def test_queue_and_gcd_are_scored_at_the_exact_same_horizon(self) -> None:
        bridge = _Bridge()
        plain = PrefixCandidate(
            "bt", FactorizedDecision(gcd=BLOODTHIRST), ("cat",)
        )
        queued = PrefixCandidate(
            "hs_bt",
            FactorizedDecision(queue=HEROIC_STRIKE, gcd=BLOODTHIRST),
            ("contra", "cat2_curated"),
        )
        plain_result = evaluate_prefix_candidate(
            bridge, {}, seed=1, candidate=plain, horizon_ms=4000
        )
        queued_result = evaluate_prefix_candidate(
            bridge, {}, seed=1, candidate=queued, horizon_ms=4000
        )

        self.assertEqual(plain_result["final_time_ms"], 4000)
        self.assertEqual(queued_result["final_time_ms"], 4000)
        self.assertEqual(queued_result["damage_delta"] - plain_result["damage_delta"], 10)

    def test_paired_summary_uses_the_same_seed_reference(self) -> None:
        result = benchmark_prefix_candidates(
            _Bridge(),
            {},
            seeds=[3, 7],
            candidates=[
                PrefixCandidate(
                    "wait",
                    FactorizedDecision(wait_ms=100),
                    ("explore",),
                ),
                PrefixCandidate(
                    "bt",
                    FactorizedDecision(gcd=BLOODTHIRST),
                    ("cat",),
                ),
            ],
            horizon_ms=3000,
            reference_candidate_id="wait",
        )

        self.assertEqual(result["trial_count"], 4)
        self.assertEqual(result["ranking"][0]["candidate_id"], "bt")
        self.assertEqual(result["ranking"][0]["mean_paired_delta_vs_reference"], 30)
        self.assertEqual(result["ranking"][0]["paired_wins"], 2)

    def test_rejects_a_gcd_that_the_simulator_did_not_cast(self) -> None:
        candidate = PrefixCandidate(
            "unknown",
            FactorizedDecision(gcd=ActionRef(spell_id=99999)),
            ("explore",),
        )
        with self.assertRaisesRegex(PrefixBenchmarkError, "GCD action was rejected"):
            evaluate_prefix_candidate(
                _Bridge(), {}, seed=1, candidate=candidate, horizon_ms=1000
            )


if __name__ == "__main__":
    unittest.main()
