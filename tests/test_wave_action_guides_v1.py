from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import historical_behavior_clone_simulator_adapter_v2 as clone_adapter_v2
from o2o_dps.offline_action_sequence_guide_v1 import (
    SOURCE_BUILD_EXACT,
    ExactBuildIdentityV1,
    OfflineActionSequenceGuideV1,
    OfflineGuideContextV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_guides_v1 import (
    CallableActionGuideV1,
    OfflineActionSequenceSearchGuideV1,
    StaticActionGuideV1,
    action_guide_audit_payload_v1,
)
from o2o_dps.wave_action_schedule_v1 import SearchCellIdentity
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    search_wave_action_sequences_v1,
)


SLAM = clone_adapter_v2.ACTION_SINK_BINDINGS_V2["warrior.slam"].action_ref
EXECUTE = clone_adapter_v2.ACTION_SINK_BINDINGS_V2["warrior.execute"].action_ref
UNKNOWN = ActionRef(item_id=99999)
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
        triggers_gcd=True,
    )


def _outcome(*actions: AvailableAction) -> ScheduleReplayOutcomeV1:
    return ScheduleReplayOutcomeV1(
        seed=7,
        status=ReplayStatusV1.FRONTIER,
        state={"time_ms": 0, "damage_done": 0.0},
        available_actions=tuple(actions),
    )


def _write_prior(root: Path) -> Path:
    document = {
        "schema_version": 1,
        "kind": "chronicle_fury_partial_observation_behavior_prior",
        "analysis_only": True,
        "training_contract": {
            "label_semantics": "next uniquely linked successful server START candidate",
            "claims_excluded": [
                "full-state behavior cloning",
                "offline reinforcement learning",
                "DPS improvement",
                "deployable WoW policy",
            ],
        },
        "action_mapping": {
            "policy_action_key_to_cat2_card_id": {
                "warrior.slam": "warrior_slam",
                "warrior.execute": "warrior_execute",
            },
            "mapping_complete": True,
            "deployment_allowed": False,
        },
        "model": {
            "max_order": 2,
            "minimum_context_count": 3,
            "additive_smoothing_alpha": 0.5,
            "label_vocabulary": ["warrior.slam", "warrior.execute"],
            "global_counts": {"warrior.slam": 9, "warrior.execute": 1},
            "contexts": {"order_1": [], "order_2": []},
        },
    }
    path = root / "prior.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class _TwoStepReplay:
    """Execute->Slam wins although the offline guide prefers Slam."""

    def replay(self, seed, schedule):
        if any(step.gcd_action is None for step in schedule):
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.INVALID,
                {"time_ms": len(schedule) * 1000, "damage_done": 0.0},
                invalid_reason="fixture excludes wait",
            )
        actions = [step.gcd_action for step in schedule]
        damage = 0.0
        if actions:
            damage += 10.0 if actions[0] == EXECUTE else 4.0
        if len(actions) == 2:
            damage += 20.0 if actions == [EXECUTE, SLAM] else 3.0
        state = {"time_ms": len(actions) * 1000, "damage_done": damage}
        if len(actions) == 2:
            return ScheduleReplayOutcomeV1(seed, ReplayStatusV1.COMPLETE, state)
        return ScheduleReplayOutcomeV1(
            seed,
            ReplayStatusV1.FRONTIER,
            state,
            available_actions=(_available(SLAM, 0), _available(EXECUTE, 1)),
        )


class WaveActionGuidesV1Tests(unittest.TestCase):
    def test_offline_adapter_returns_every_legal_action_and_exposes_receipt(self):
        identity = {"talents": "exact-fury", "equipment": [{"id": 19364}]}
        with tempfile.TemporaryDirectory() as temporary_directory:
            offline = OfflineActionSequenceGuideV1.from_chronicle_prior(
                _write_prior(Path(temporary_directory)),
                source_build_identity=ExactBuildIdentityV1(identity),
                source_build_scope=SOURCE_BUILD_EXACT,
            )
            guide = OfflineActionSequenceSearchGuideV1(
                "chronicle-exact-build",
                offline,
                lambda outcome, prefix: OfflineGuideContextV1(
                    recent_successful_action_history=tuple(
                        "warrior.execute"
                        for step in prefix
                        if step.gcd_action == EXECUTE
                    ),
                    evaluation_build_identity=identity,
                ),
            )
            priorities = guide.action_priorities(
                _outcome(
                    _available(EXECUTE, 0),
                    _available(UNKNOWN, 1),
                    _available(ILLEGAL, 2, legal=False),
                    _available(SLAM, 3),
                ),
                (),
            )

        self.assertEqual({EXECUTE, UNKNOWN, SLAM}, set(priorities))
        self.assertGreater(priorities[SLAM], priorities[EXECUTE])
        self.assertEqual(0.0, priorities[UNKNOWN])
        self.assertNotIn(ILLEGAL, priorities)
        self.assertTrue(
            guide.build_match_receipts[0].same_build_comparison_eligible
        )
        audit = guide.audit_snapshot().to_dict()
        self.assertEqual("ALL_EXACT_BUILD_MATCH", audit["same_build_comparison_eligibility"])
        self.assertFalse(audit["contract"]["guide_can_filter_search_action_universe"])
        self.assertEqual((audit,), action_guide_audit_payload_v1((guide,)))

    def test_offline_bias_only_orders_and_cannot_remove_better_sequence(self):
        identity = {"talents": "exact-fury", "equipment": [{"id": 19364}]}
        with tempfile.TemporaryDirectory() as temporary_directory:
            offline = OfflineActionSequenceGuideV1.from_chronicle_prior(
                _write_prior(Path(temporary_directory)),
                source_build_identity=identity,
                source_build_scope=SOURCE_BUILD_EXACT,
            )
            guide = OfflineActionSequenceSearchGuideV1(
                "offline-guide",
                offline,
                lambda outcome, prefix: OfflineGuideContextV1(
                    evaluation_build_identity=identity
                ),
            )
            result = search_wave_action_sequences_v1(
                _TwoStepReplay(),
                SearchCellIdentity("upper-kara", "wave-1", "exact-build-1"),
                seeds=(11, 13),
                max_steps=2,
                beam_width=4,
                action_guides=(guide,),
                max_off_gcd_actions=0,
            )

        self.assertEqual([EXECUTE, SLAM], [step.gcd_action for step in result.schedule])
        self.assertEqual("COMPLETE_PAIRED_SEED_SCHEDULE", result.status)
        self.assertGreater(guide.audit_snapshot().invocation_count, 0)
        self.assertTrue(
            all(
                receipt.same_build_comparison_eligible
                for receipt in guide.build_match_receipts
            )
        )

    def test_callable_and_static_guides_project_onto_complete_legal_set(self):
        outcome = _outcome(
            _available(SLAM, 0),
            _available(EXECUTE, 1),
            _available(ILLEGAL, 2, legal=False),
        )
        callable_guide = CallableActionGuideV1(
            "contra-new",
            lambda current, prefix: {SLAM: 5.0, ILLEGAL: 999.0, UNKNOWN: 20.0},
        )
        static_guide = StaticActionGuideV1(
            "cat",
            {EXECUTE: 4.0, UNKNOWN: 8.0},
        )

        self.assertEqual(
            {SLAM: 5.0, EXECUTE: 0.0},
            callable_guide.action_priorities(outcome, ()),
        )
        self.assertEqual(
            {SLAM: 0.0, EXECUTE: 4.0},
            static_guide.action_priorities(outcome, ()),
        )


if __name__ == "__main__":
    unittest.main()
