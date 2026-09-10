from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    invalid_decision,
)
from o2o_dps.expert_proposals import project_expert_decision
from o2o_dps.sim_bridge import ActionRef, AvailableAction


PROVENANCE = ExpertProvenance(
    expert_id="test",
    kind=ProvenanceKind.SOURCE_DERIVED,
    role=ExpertRole.DEPLOYED,
    authority_files=("test.lua",),
)


def _available(action: ActionRef, *, gcd: bool, legal: bool = True) -> AvailableAction:
    return AvailableAction(0, action, str(action), legal, 0, gcd)


class ExpertProposalProjectionTests(unittest.TestCase):
    def test_projects_queue_and_gcd_and_preserves_nonfaithful_lanes(self) -> None:
        decision = ExpertDecision(
            provenance=PROVENANCE,
            valid=True,
            gcd="warrior.bloodthirst",
            wait_ms=None,
            swing_queue=SwingQueueOp.HEROIC_STRIKE,
            off_gcd=("warrior.bloodrage",),
            eligible_for_independent_vote=True,
        )
        result = project_expert_decision(
            decision,
            [
                _available(ActionRef(spell_id=25286, tag=1), gcd=False),
                _available(ActionRef(23894), gcd=True),
            ],
        )

        self.assertTrue(result.accepted)
        self.assertFalse(result.faithful_replay)
        self.assertEqual(result.omitted_lanes, ("off_gcd",))
        self.assertEqual(
            result.decision.queue, ActionRef(spell_id=25286, tag=1)
        )
        self.assertEqual(result.decision.gcd, ActionRef(23894))

    def test_invalid_and_illegal_proposals_are_not_in_the_union(self) -> None:
        unavailable = invalid_decision(PROVENANCE, "no trace")
        self.assertFalse(project_expert_decision(unavailable, []).accepted)

        illegal = ExpertDecision(
            provenance=PROVENANCE,
            valid=True,
            gcd="warrior.whirlwind",
            wait_ms=None,
        )
        result = project_expert_decision(
            illegal,
            [_available(ActionRef(1680), gcd=True, legal=False)],
        )
        self.assertFalse(result.accepted)
        self.assertIn("not legal", result.reason)

    def test_cancel_requires_an_observed_active_queue(self) -> None:
        decision = ExpertDecision(
            provenance=PROVENANCE,
            valid=True,
            swing_queue=SwingQueueOp.CANCEL,
        )
        self.assertFalse(project_expert_decision(decision, []).accepted)
        projected = project_expert_decision(decision, [], queue_active=True)
        self.assertTrue(projected.accepted)
        self.assertTrue(projected.decision.cancel_queue)

    def test_matching_stance_is_an_idempotent_satisfied_lane(self) -> None:
        decision = ExpertDecision(
            provenance=PROVENANCE,
            valid=True,
            stance=StanceOp.BERSERKER,
        )
        result = project_expert_decision(
            decision, [], current_stance=StanceOp.BERSERKER
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.faithful_replay)

    def test_requested_target_change_is_never_reported_as_faithful(self) -> None:
        decision = ExpertDecision(
            provenance=PROVENANCE,
            valid=True,
            target=TargetOp.AUTO_SWITCH,
        )
        result = project_expert_decision(
            decision,
            [],
            target_is_usable=True,
        )

        self.assertTrue(result.accepted)
        self.assertFalse(result.faithful_replay)
        self.assertEqual(result.omitted_lanes, ("target",))


if __name__ == "__main__":
    unittest.main()
