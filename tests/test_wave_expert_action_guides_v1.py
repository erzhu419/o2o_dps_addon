from __future__ import annotations

import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import _state as _cat_state
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_expert_action_guides_v1 import (
    cat_wave_action_guide_v1,
    contra260817_wave_action_guide_v1,
    deployed_contra_wave_action_guide_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)
from tests.test_contra260817_fury_full_policy_v3 import (
    _state as _contra260817_state,
)
from tests.test_fury_contra_adapter_v2 import _state as _deployed_state
from tests.test_fury_runtime_bound_deployed_contra_adapter_v7 import _binding


BLOODTHIRST = ACTION_KEY_TO_REF["warrior.bloodthirst"]
BERSERKER_STANCE = ACTION_KEY_TO_REF["warrior.berserker_stance"]
HEROIC_STRIKE = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]
SEARCH_ONLY = ActionRef(item_id=99999)
ILLEGAL = ActionRef(spell_id=99998)


def _available(
    action: ActionRef,
    index: int,
    *,
    legal: bool = True,
) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=action,
        label=str(action),
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=action.tag == 0,
    )


def _outcome() -> ScheduleReplayOutcomeV1:
    return ScheduleReplayOutcomeV1(
        seed=20260914,
        status=ReplayStatusV1.FRONTIER,
        state={"time_ms": 0, "damage_done": 0.0},
        available_actions=(
            _available(BLOODTHIRST, 0),
            _available(BERSERKER_STANCE, 1),
            _available(HEROIC_STRIKE, 2),
            _available(SEARCH_ONLY, 3),
            _available(ILLEGAL, 4, legal=False),
        ),
    )


class WaveExpertActionGuidesV1Tests(unittest.TestCase):
    def test_three_real_expert_adapters_rank_without_filtering_membership(self):
        cat = cat_wave_action_guide_v1(
            lambda outcome, prefix: _cat_state(
                combat={
                    "rage": 100.0,
                    "bloodthirst_ready_in_s": 0.0,
                    "whirlwind_ready_in_s": 0.0,
                }
            )
        )
        deployed = deployed_contra_wave_action_guide_v1(
            lambda outcome, prefix: _deployed_state(
                rage=100.0,
                bloodthirst_ready_in_s=0.0,
                whirlwind_ready_in_s=0.0,
            ),
            runtime_binding=_binding(),
        )
        contra260817 = contra260817_wave_action_guide_v1(
            lambda outcome, prefix: _contra260817_state(
                rage=100.0,
                bloodthirst_ready_in_s=0.0,
                whirlwind_ready_in_s=0.0,
            )
        )

        legal_membership = {
            BLOODTHIRST,
            BERSERKER_STANCE,
            HEROIC_STRIKE,
            SEARCH_ONLY,
        }
        for guide in (cat, deployed, contra260817):
            with self.subTest(guide=guide.guide_id):
                priorities = guide.action_priorities(_outcome(), ())
                self.assertEqual(legal_membership, set(priorities))
                self.assertGreater(priorities[BLOODTHIRST], 0.0)
                self.assertGreater(priorities[HEROIC_STRIKE], 0.0)
                self.assertEqual(0.0, priorities[SEARCH_ONLY])
                self.assertNotIn(ILLEGAL, priorities)
                self.assertTrue(guide.last_decision.valid)
                audit = guide.audit_snapshot().to_dict()
                self.assertEqual(1, audit["valid_proposal_count"])
                self.assertTrue(
                    audit["contract"][
                        "all_legal_simulator_actions_receive_a_weight"
                    ]
                )

        self.assertTrue(cat.guide_id.startswith("cat:"))
        self.assertTrue(deployed.guide_id.startswith("deployed_contra:"))
        self.assertTrue(contra260817.guide_id.startswith("contra260817:"))
        self.assertEqual(
            "contra.deployed.fury.raid_a.v7.runtime_bound",
            deployed.last_decision.expert_id,
        )
        self.assertEqual(
            "contra260817.fury.full_policy.source_diagnostic.v3",
            contra260817.last_decision.expert_id,
        )

    def test_invalid_expert_proposal_becomes_all_zero_not_empty_membership(self):
        guide = cat_wave_action_guide_v1(
            lambda outcome, prefix: object()
        )
        priorities = guide.action_priorities(_outcome(), ())

        self.assertEqual(
            {BLOODTHIRST, BERSERKER_STANCE, HEROIC_STRIKE, SEARCH_ONLY},
            set(priorities),
        )
        self.assertTrue(all(value == 0.0 for value in priorities.values()))
        self.assertFalse(guide.last_decision.valid)
        self.assertEqual(1, guide.audit_snapshot().invalid_proposal_count)

    def test_targetless_precombat_does_not_invent_a_cat_combat_state(self):
        def forbidden_state_factory(outcome, prefix):
            raise AssertionError("combat state factory must not run before pull")

        guide = cat_wave_action_guide_v1(forbidden_state_factory)
        outcome = ScheduleReplayOutcomeV1(
            seed=7,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 0,
                "damage_done": 0.0,
                "num_targets": 0,
                "precombat": {"active": True},
            },
            available_actions=(_available(SEARCH_ONLY, 0),),
        )

        self.assertEqual(
            guide.action_priorities(outcome, ()),
            {SEARCH_ONLY: 0.0},
        )
        audit = guide.audit_snapshot()
        self.assertEqual(audit.invocation_count, 1)
        self.assertEqual(audit.precombat_skipped_count, 1)
        self.assertEqual(audit.valid_proposal_count, 0)
        self.assertEqual(audit.invalid_proposal_count, 0)
        self.assertIsNone(guide.last_decision)


if __name__ == "__main__":
    unittest.main()
