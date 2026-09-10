from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.beam_search import (
    ActionLane,
    FactorizedDecision,
    SearchError,
    _replay,
    beam_search,
    classify_action,
    factorized_decisions,
)
from o2o_dps.sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    CancelQueueResult,
)


HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)
CLEAVE = ActionRef(spell_id=20569, tag=1)
BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
DEATH_WISH = ActionRef(spell_id=12328)
UNKNOWN_TAGGED_NON_GCD = ActionRef(spell_id=99999, tag=1)


def _available(
    action: ActionRef, *, triggers_gcd: bool, legal: bool = True
) -> AvailableAction:
    return AvailableAction(
        index=0,
        action=action,
        label=str(action),
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=triggers_gcd,
    )


class _FakeBridge:
    """Deterministic in-memory implementation of the search bridge calls."""

    def __init__(self, *, initially_queued: bool = False) -> None:
        self.load_records: list[tuple[dict[str, object], int]] = []
        self.episodes: list[list[ActionRef | tuple[str, int]]] = []
        self.initially_queued = initially_queued
        self.phase = 0
        self.damage = 0.0
        self.queued = self.initially_queued
        self.needs_input = True
        self.finished = False

    def load(self, request, seed: int):
        self.load_records.append((dict(request), seed))
        self.episodes.append([])
        self.phase = 0
        self.damage = 0.0
        self.queued = self.initially_queued
        self.needs_input = True
        self.finished = False
        return self._state()

    def actions(self):
        if self.finished:
            return []
        return [
            _available(HEROIC_STRIKE, triggers_gcd=False),
            _available(DEATH_WISH, triggers_gcd=False),
            _available(UNKNOWN_TAGGED_NON_GCD, triggers_gcd=False),
            _available(BLOODTHIRST, triggers_gcd=True),
            _available(WHIRLWIND, triggers_gcd=True),
        ]

    def act(self, action: ActionRef):
        self.episodes[-1].append(action)
        if action == HEROIC_STRIKE:
            self.queued = True
            return ActResult(
                casted=True,
                consumes_decision=False,
                finished=False,
                needs_input=True,
                state=self._state(),
            )
        if action not in (BLOODTHIRST, WHIRLWIND):
            return ActResult(
                casted=False,
                consumes_decision=False,
                finished=False,
                needs_input=True,
                state=self._state(),
            )

        base_damage = 30.0 if action == BLOODTHIRST else 20.0
        self.damage += base_damage + (10.0 if self.queued else 0.0)
        self.queued = False
        self.phase += 1
        self.needs_input = False
        return ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state=self._state(),
        )

    def wait(self, wait_ms: int):
        self.episodes[-1].append(("wait", wait_ms))
        self.needs_input = False
        self.phase += 1
        return self._state()

    def cancel_queue(self):
        self.episodes[-1].append(("cancel_queue", 0))
        canceled = self.queued
        self.queued = False
        return CancelQueueResult(
            canceled=canceled,
            consumes_decision=False,
            finished=False,
            needs_input=True,
            state=self._state(),
        )

    def advance(self):
        self.finished = self.phase >= 2
        self.needs_input = not self.finished
        return self._state()

    def _state(self):
        return {
            "time_ms": self.phase * 1500,
            "remaining_ms": max(0, 3000 - self.phase * 1500),
            "finished": self.finished,
            "needs_input": self.needs_input,
            "damage_done": self.damage,
        }


class FactorizedActionTests(unittest.TestCase):
    def test_only_known_tagged_swing_action_is_a_queue(self) -> None:
        self.assertEqual(
            classify_action(_available(HEROIC_STRIKE, triggers_gcd=False)),
            ActionLane.QUEUE,
        )
        self.assertEqual(
            classify_action(_available(CLEAVE, triggers_gcd=False)),
            ActionLane.QUEUE,
        )
        self.assertEqual(
            classify_action(_available(DEATH_WISH, triggers_gcd=False)),
            ActionLane.OFF_GCD,
        )
        self.assertEqual(
            classify_action(
                _available(UNKNOWN_TAGGED_NON_GCD, triggers_gcd=False)
            ),
            ActionLane.OFF_GCD,
        )
        self.assertEqual(
            classify_action(_available(HEROIC_STRIKE, triggers_gcd=True)),
            ActionLane.GCD,
        )

    def test_factorization_emits_queue_then_gcd_and_ignores_off_gcd(self) -> None:
        decisions = factorized_decisions(
            [
                _available(HEROIC_STRIKE, triggers_gcd=False),
                _available(DEATH_WISH, triggers_gcd=False),
                _available(BLOODTHIRST, triggers_gcd=True),
            ]
        )
        flattened = [
            [command.action for command in decision.commands()]
            for decision in decisions
        ]
        self.assertEqual(
            flattened,
            [[BLOODTHIRST], [HEROIC_STRIKE, BLOODTHIRST]],
        )
        self.assertNotIn(DEATH_WISH, [item for row in flattened for item in row])

    def test_wait_is_the_fallback_when_no_gcd_is_reachable(self) -> None:
        decisions = factorized_decisions(
            [_available(HEROIC_STRIKE, triggers_gcd=False)], fallback_wait_ms=75
        )
        self.assertEqual(len(decisions), 2)
        self.assertEqual(
            [[command.action for command in decision.commands()] for decision in decisions],
            [[None], [HEROIC_STRIKE, None]],
        )
        self.assertTrue(all(decision.wait_ms == 75 for decision in decisions))

    def test_queue_can_share_an_epoch_with_an_explicit_wait(self) -> None:
        decision = FactorizedDecision(queue=HEROIC_STRIKE, wait_ms=125)
        self.assertEqual(
            [command.to_dict() for command in decision.commands()],
            [
                {"command": "act", "action": HEROIC_STRIKE.to_wire()},
                {"command": "wait", "wait_ms": 125},
            ],
        )

    def test_active_queue_adds_cancel_variants_without_changing_defaults(self) -> None:
        available = [
            _available(HEROIC_STRIKE, triggers_gcd=False),
            _available(BLOODTHIRST, triggers_gcd=True),
        ]
        default = factorized_decisions(available)
        with_cancel = factorized_decisions(available, queue_active=True)

        self.assertEqual(
            default,
            (
                FactorizedDecision(gcd=BLOODTHIRST),
                FactorizedDecision(queue=HEROIC_STRIKE, gcd=BLOODTHIRST),
            ),
        )
        self.assertIn(
            FactorizedDecision(gcd=BLOODTHIRST, cancel_queue=True),
            with_cancel,
        )
        self.assertEqual(
            FactorizedDecision(
                gcd=BLOODTHIRST,
                cancel_queue=True,
            ).commands()[0].to_dict(),
            {"command": "cancel_queue"},
        )

    def test_explicit_wait_can_be_compared_while_gcd_is_available(self) -> None:
        decisions = factorized_decisions(
            [_available(BLOODTHIRST, triggers_gcd=True)],
            fallback_wait_ms=75,
            include_wait_when_gcd_available=True,
        )

        self.assertIn(FactorizedDecision(gcd=BLOODTHIRST), decisions)
        self.assertIn(FactorizedDecision(wait_ms=75), decisions)

    def test_active_queue_without_gcd_can_cancel_then_wait(self) -> None:
        decisions = factorized_decisions(
            [_available(HEROIC_STRIKE, triggers_gcd=False)],
            fallback_wait_ms=75,
            queue_active=True,
        )

        self.assertIn(FactorizedDecision(wait_ms=75), decisions)
        self.assertIn(
            FactorizedDecision(wait_ms=75, cancel_queue=True),
            decisions,
        )


class BeamSearchTests(unittest.TestCase):
    def test_replay_executes_queue_cancel_before_gcd(self) -> None:
        bridge = _FakeBridge(initially_queued=True)

        state, teachers = _replay(
            bridge,
            {"raid": {}, "encounter": {}},
            42,
            (FactorizedDecision(gcd=BLOODTHIRST, cancel_queue=True),),
        )

        self.assertEqual(
            bridge.episodes[-1],
            [("cancel_queue", 0), BLOODTHIRST],
        )
        self.assertFalse(bridge.queued)
        self.assertEqual(len(teachers), 1)
        self.assertEqual(state["damage_done"], 30.0)

    def test_replay_rejects_cancel_when_no_queue_is_active(self) -> None:
        bridge = _FakeBridge()

        with self.assertRaisesRegex(SearchError, "queue cancellation failed"):
            _replay(
                bridge,
                {"raid": {}, "encounter": {}},
                42,
                (FactorizedDecision(gcd=BLOODTHIRST, cancel_queue=True),),
            )

    def test_search_reloads_every_branch_and_returns_teacher_trajectory(self) -> None:
        bridge = _FakeBridge()
        request = {"raid": {"parties": [{"players": [{}]}]}, "encounter": {}}

        result = beam_search(
            bridge,
            request,
            seed=42,
            depth=2,
            beam_width=2,
        )

        action_sequence = [
            command.action
            for command in result.best_action_sequence
            if command.kind == "act"
        ]
        self.assertEqual(
            action_sequence,
            [HEROIC_STRIKE, BLOODTHIRST, HEROIC_STRIKE, BLOODTHIRST],
        )
        self.assertEqual(result.score, 80.0)
        self.assertTrue(result.frontier_state["finished"])
        self.assertEqual(len(result.teacher_states), 2)
        self.assertEqual(result.teacher_states[0].state["damage_done"], 0.0)
        self.assertEqual(result.teacher_states[0].next_state["damage_done"], 40.0)
        self.assertEqual(result.teacher_states[1].state["damage_done"], 40.0)
        self.assertEqual(result.teacher_states[1].next_state["damage_done"], 80.0)

        self.assertGreater(len(bridge.load_records), 4)
        self.assertTrue(all(seed == 42 for _, seed in bridge.load_records))
        self.assertTrue(all(record == request for record, _ in bridge.load_records))
        flattened_episodes = [item for episode in bridge.episodes for item in episode]
        self.assertNotIn(DEATH_WISH, flattened_episodes)
        self.assertNotIn(UNKNOWN_TAGGED_NON_GCD, flattened_episodes)

        serialized = result.to_dict()
        self.assertEqual(serialized["score"], 80.0)
        self.assertEqual(len(serialized["teacher_states"]), 2)

    def test_spell_allowlist_limits_normal_reachable_actions(self) -> None:
        bridge = _FakeBridge()
        result = beam_search(
            bridge,
            {"raid": {}, "encounter": {}},
            depth=1,
            beam_width=3,
            spell_id_allowlist={BLOODTHIRST.spell_id},
        )
        self.assertEqual(len(result.best_action_sequence), 1)
        self.assertEqual(result.best_action_sequence[0].action, BLOODTHIRST)
        self.assertEqual(result.score, 30.0)


if __name__ == "__main__":
    unittest.main()
