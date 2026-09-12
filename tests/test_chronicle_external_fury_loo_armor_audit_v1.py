from __future__ import annotations

import unittest

from o2o_dps import chronicle_external_aura_attribution_v2 as aura_v2
from o2o_dps.chronicle_external_fury_loo_armor_audit_v1 import (
    audit_focal_wave_armor_lifecycles,
)


INSTANCE = "instance-1"
ENCOUNTER = "encounter-1"
FOCAL = "0x00000000000000AA"
OTHER = "0x00000000000000BB"
TARGET = "0xF130000000000001"


def _anchor(timestamp_ms: int, event_index: int, stream: str = "aura") -> dict:
    return {
        "timestamp_ms": timestamp_ms,
        "event_index": event_index,
        "offset_ms": timestamp_ms - 1_000,
        "stream_type": stream,
        "frame_index": 0,
        "frame_message_index": event_index,
    }


def _event(
    timestamp_ms: int,
    event_index: int,
    *,
    state: str,
    prior: int,
    current: int,
    source: str | None,
    attribution_status: str | None = None,
    debuff_id: str = "sunder_armor",
    spell_id: int = 11597,
    target: str = TARGET,
    is_buff: bool = False,
    aura_role: str = "REGISTERED_HOSTILE_ARMOR_AURA_DIAGNOSTIC",
) -> dict:
    if attribution_status is None:
        attribution_status = (
            "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED"
            if source is not None and state == "StateAdded"
            else "STATE_REMOVED_CASTER_NOT_IN_AURA_PROTO"
            if state == "StateRemoved"
            else "NO_PHYSICAL_EFFECT_CAST_WITHIN_WINDOW"
        )
    return {
        "schema": aura_v2.EVENT_SCHEMA,
        "status": aura_v2.STATUS,
        "instance": INSTANCE,
        "encounter": ENCOUNTER,
        "target_guid": target,
        "debuff_id": debuff_id,
        "spell_id": spell_id,
        "spell": debuff_id,
        "aura_state": state,
        "current_amount": current,
        "is_buff": is_buff,
        "aura_role": aura_role,
        "aura_anchor": _anchor(timestamp_ms, event_index),
        "factual_transition": {
            "prior_amount": prior,
            "current_amount": current,
            "stack_delta": current - prior,
            "unique_physical_cast_supported": (
                attribution_status == "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED"
            ),
        },
        "attribution": {
            "status": attribution_status,
            "source_guid": source,
        },
        "lifecycle": {
            "active_exact_source_counts": {},
            "active_unresolved_stack_count": 0,
            "saw_unattributed_transition": False,
            "saw_nonmonotonic_transition": False,
            "closed_exact_source_counts": None,
            "closed_unresolved_stack_count": None,
        },
        "projection_contract": {
            "focal_guid_loo_ready": False,
            "hostile_flat_armor_reduction_authorized": False,
            "bonereaver_hostile_reduction_forbidden": debuff_id
            == "bonereavers_edge",
        },
    }


def _action(
    timestamp_ms: int,
    event_index: int,
    *,
    source: str | None,
    amount_before: int,
    delta: int | None,
    matched_event: tuple[int, int] | None = None,
    nonexecutable: bool | None = None,
    debuff_id: str = "sunder_armor",
    spell_id: int = 11597,
) -> dict:
    if nonexecutable is None:
        nonexecutable = delta != 1
    matched_anchor = (
        _anchor(matched_event[0], matched_event[1])
        if matched_event is not None
        else None
    )
    if delta == 1:
        lifecycle_status = "OBSERVED_STATE_ADDED_STACK_INCREASE"
        action_status = "MATCHED_AURA_STACK_INCREASE"
        result_observed = True
    elif delta == 0:
        lifecycle_status = "CAST_ONLY_FULL_STACK_REFRESH_CANDIDATE"
        action_status = "SUNDER_FULL_STACK_REFRESH_CANDIDATE_PRESERVED"
        result_observed = False
    else:
        lifecycle_status = "CAST_ONLY_WITHOUT_AURA_TRANSITION"
        action_status = "UNMATCHED_CAST_NOT_EXECUTABLE_AS_REFRESH"
        result_observed = False
    return {
        "instance": INSTANCE,
        "encounter": ENCOUNTER,
        "target_guid": TARGET,
        "debuff_id": debuff_id,
        "spell_id": spell_id,
        "source_guid": source,
        "timestamp_ms": timestamp_ms,
        "anchor": _anchor(timestamp_ms, event_index, "aura_cast"),
        "duration_ms": 30_000,
        "cap_status": 0,
        "effect_tuple": [6, 22, 1],
        "factual_amount_before_cast": amount_before,
        "factual_stack_delta": delta,
        "factual_lifecycle_status": lifecycle_status,
        "factual_result_event_observed": result_observed,
        "matched_aura_state_anchor": matched_anchor,
        "action_status": action_status,
        "loo_counterfactual": {
            "status": "NONEXECUTABLE" if nonexecutable else "CONDITIONALLY_ADMISSIBLE",
            "counterfactual_nonexecutable": nonexecutable,
            "counterfactual_executable": False,
            "focal_context_evaluated": False,
            "reason": "AURA_CAST_HAS_NO_SUCCESS_RESULT" if nonexecutable else None,
            "requires_no_focal_predecessor_contribution": not nonexecutable,
        },
    }


def _audit(
    events: list[dict],
    actions: list[dict] | None = None,
    *,
    start: tuple[int, int] = (1_000, 0),
    end: tuple[int, int] = (2_000, 100),
    scan_complete: bool = True,
) -> dict:
    return audit_focal_wave_armor_lifecycles(
        focal_guid=FOCAL,
        wave={
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "encounter_ordinal": 0,
            "wave_id": "wave-1",
            "wave_ordinal": 1,
        },
        first_anchor=_anchor(start[0], start[1], "damage"),
        last_context_anchor=_anchor(end[0], end[1], "damage"),
        target_registry=[
            {
                "target_index": 0,
                "target_guid": TARGET,
                "lane": "HOSTILE_CREATURE",
            }
        ],
        attributed_state_events=events,
        cast_actions=actions or [],
        encounter_scan_complete=scan_complete,
    )


class ChronicleExternalFuryLooArmorAuditV1Tests(unittest.TestCase):
    def test_exact_nonfocal_closed_lifecycle_is_state_and_action_executable(self) -> None:
        events = [
            _event(1_100, 10, state="StateAdded", prior=0, current=1, source=OTHER),
            _event(1_500, 20, state="StateRemoved", prior=1, current=0, source=None),
        ]
        actions = [
            _action(
                1_090,
                9,
                source=OTHER,
                amount_before=0,
                delta=1,
                matched_event=(1_100, 10),
            )
        ]
        result = _audit(events, actions)

        self.assertTrue(result["loo_counterfactual"]["state_trajectory_executable"])
        self.assertTrue(result["loo_counterfactual"]["action_projection_executable"])
        lifecycle = result["factual_lifecycles"][0]
        self.assertEqual(lifecycle["factual_lifecycle"]["exact_source_counts"], {OTHER: 1})
        self.assertTrue(lifecycle["factual_lifecycle"]["closed_by_state_removed"])
        self.assertFalse(lifecycle["factual_lifecycle"]["state_removed_caster_inferred"])

    def test_focal_predecessor_is_not_subtracted_and_blocks_lifecycle(self) -> None:
        events = [
            _event(900, 1, state="StateAdded", prior=0, current=1, source=FOCAL),
            _event(1_500, 20, state="StateRemoved", prior=1, current=0, source=None),
        ]
        result = _audit(events)
        lifecycle = result["factual_lifecycles"][0]

        self.assertEqual(lifecycle["factual_lifecycle"]["initial_amount_at_wave_start"], 1)
        self.assertIn(
            "FOCAL_PREDECESSOR_CONTRIBUTION",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertFalse(lifecycle["loo_counterfactual"]["focal_stack_subtraction_used"])
        self.assertFalse(result["loo_counterfactual"]["state_trajectory_executable"])

    def test_focal_refresh_has_zero_factual_delta_but_blocks_loo_duration(self) -> None:
        events = [
            _event(900, 1, state="StateAdded", prior=0, current=1, source=OTHER),
            _event(920, 2, state="StateAdded", prior=1, current=2, source=OTHER),
            _event(940, 3, state="StateAdded", prior=2, current=3, source=OTHER),
            _event(960, 4, state="StateAdded", prior=3, current=4, source=OTHER),
            _event(980, 5, state="StateAdded", prior=4, current=5, source=OTHER),
            _event(1_700, 30, state="StateRemoved", prior=5, current=0, source=None),
        ]
        actions = [
            _action(
                1_200,
                10,
                source=FOCAL,
                amount_before=5,
                delta=0,
                nonexecutable=True,
            )
        ]
        result = _audit(events, actions)
        lifecycle = result["factual_lifecycles"][0]

        refresh = lifecycle["factual_lifecycle"]["refresh_candidates"][0]
        self.assertEqual(refresh["factual_stack_delta"], 0)
        self.assertTrue(refresh["counterfactual_nonexecutable"])
        self.assertNotIn(1_200, [
            row["anchor"]["timestamp_ms"]
            for row in lifecycle["factual_lifecycle"]["transitions_inside_wave"]
        ])
        self.assertIn(
            "FOCAL_ACTIVE_AURA_REFRESH_COUNTERFACTUAL_UNKNOWN",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertFalse(result["loo_counterfactual"]["state_trajectory_executable"])

    def test_ambiguous_addition_blocks_but_unmatched_zero_cast_does_not_make_stack(self) -> None:
        events = [
            _event(
                1_100,
                10,
                state="StateAdded",
                prior=0,
                current=1,
                source=None,
                attribution_status="MULTIPLE_PHYSICAL_EFFECT_CASTS_REJECTED",
            ),
            _event(1_500, 20, state="StateRemoved", prior=1, current=0, source=None),
        ]
        actions = [
            _action(
                1_050,
                5,
                source=FOCAL,
                amount_before=0,
                delta=None,
                nonexecutable=True,
            )
        ]
        result = _audit(events, actions)
        lifecycle = result["factual_lifecycles"][0]

        self.assertIn(
            "UNRESOLVED_STACK_SOURCE",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertEqual(result["summary"]["unassigned_cast_action_count"], 1)
        self.assertEqual(
            lifecycle["factual_lifecycle"]["transitions_inside_wave"][0]["current_amount"],
            1,
        )

    def test_state_removed_resets_focal_taint_for_a_new_lifecycle(self) -> None:
        events = [
            _event(1_050, 1, state="StateAdded", prior=0, current=1, source=FOCAL),
            _event(1_200, 2, state="StateRemoved", prior=1, current=0, source=None),
            _event(1_300, 3, state="StateAdded", prior=0, current=1, source=OTHER),
            _event(1_500, 4, state="StateRemoved", prior=1, current=0, source=None),
        ]
        result = _audit(events)

        self.assertEqual(len(result["factual_lifecycles"]), 2)
        self.assertFalse(
            result["factual_lifecycles"][0]["loo_counterfactual"][
                "state_trajectory_executable"
            ]
        )
        self.assertTrue(
            result["factual_lifecycles"][1]["loo_counterfactual"][
                "state_trajectory_executable"
            ]
        )
        later_only = _audit(events, start=(1_250, 0), end=(1_600, 20))
        self.assertEqual(len(later_only["factual_lifecycles"]), 1)
        self.assertTrue(later_only["loo_counterfactual"]["state_trajectory_executable"])

    def test_focal_contribution_after_wave_end_does_not_taint_earlier_prefix(self) -> None:
        events = [
            _event(900, 1, state="StateAdded", prior=0, current=1, source=OTHER),
            _event(2_200, 2, state="StateAdded", prior=1, current=2, source=FOCAL),
            _event(2_500, 3, state="StateRemoved", prior=2, current=0, source=None),
        ]
        actions = [
            _action(
                2_300,
                4,
                source=FOCAL,
                amount_before=2,
                delta=0,
                nonexecutable=True,
            )
        ]

        result = _audit(events, actions, end=(2_000, 100))
        lifecycle = result["factual_lifecycles"][0]

        self.assertTrue(result["loo_counterfactual"]["state_trajectory_executable"])
        self.assertNotIn(
            "FOCAL_PREDECESSOR_CONTRIBUTION",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertEqual(
            lifecycle["factual_lifecycle"]["exact_source_counts_through_wave_end"],
            {OTHER: 1},
        )
        self.assertEqual(
            lifecycle["factual_lifecycle"]["complete_lifecycle_exact_source_counts"],
            {FOCAL: 1, OTHER: 1},
        )
        self.assertEqual(lifecycle["factual_lifecycle"]["refresh_candidate_count"], 0)

    def test_relevant_unassigned_active_cast_blocks_even_with_a_lifecycle(self) -> None:
        events = [
            _event(1_100, 10, state="StateAdded", prior=0, current=1, source=OTHER),
            _event(1_200, 20, state="StateRemoved", prior=1, current=0, source=None),
        ]
        actions = [
            _action(
                1_300,
                30,
                source=FOCAL,
                amount_before=1,
                delta=0,
                nonexecutable=True,
            )
        ]

        result = _audit(events, actions)

        self.assertEqual(result["summary"]["unassigned_cast_action_count"], 1)
        self.assertEqual(
            result["summary"]["unassigned_active_cast_count_through_wave_end"], 1
        )
        self.assertIn(
            "UNASSIGNED_ACTIVE_AURA_CAST_PREVENTS_ZERO_PROOF",
            result["loo_counterfactual"]["blocker_counts"],
        )
        self.assertFalse(result["loo_counterfactual"]["state_trajectory_executable"])

    def test_right_censored_lifecycle_is_factual_but_nonexecutable(self) -> None:
        result = _audit(
            [_event(1_100, 10, state="StateAdded", prior=0, current=1, source=OTHER)]
        )
        lifecycle = result["factual_lifecycles"][0]

        self.assertTrue(lifecycle["factual_lifecycle"]["right_censored"])
        self.assertFalse(lifecycle["factual_lifecycle"]["closed_by_state_removed"])
        self.assertIn(
            "RIGHT_CENSORED_WITHOUT_STATE_REMOVED",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertFalse(result["loo_counterfactual"]["state_trajectory_executable"])

    def test_bonereaver_is_excluded_and_does_not_defeat_hostile_empty_zero(self) -> None:
        bonereaver = _event(
            1_200,
            10,
            state="StateAdded",
            prior=0,
            current=1,
            source=FOCAL,
            debuff_id="bonereavers_edge",
            spell_id=21153,
            target=FOCAL,
            is_buff=True,
            aura_role="BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE",
        )
        result = _audit([bonereaver])

        self.assertEqual(result["factual_lifecycles"], [])
        self.assertEqual(
            result["summary"]["bonereaver_self_ignore_event_count_excluded"], 1
        )
        self.assertTrue(result["empty_zero_proof"]["proven"])
        self.assertFalse(
            result["claim_boundary"]["bonereaver_projected_as_hostile_reduction"]
        )

    def test_empty_zero_requires_a_complete_encounter_scan(self) -> None:
        complete = _audit([])
        incomplete = _audit([], scan_complete=False)

        self.assertEqual(complete["empty_zero_proof"]["status"], "EMPTY_ZERO_PROVEN")
        self.assertTrue(complete["loo_counterfactual"]["state_trajectory_executable"])
        self.assertFalse(incomplete["empty_zero_proof"]["proven"])
        self.assertFalse(incomplete["loo_counterfactual"]["state_trajectory_executable"])
        self.assertIn(
            "ENCOUNTER_AURA_SCAN_INCOMPLETE",
            incomplete["empty_zero_proof"]["blocker_codes"],
        )

    def test_orphan_state_removed_blocks_earlier_zero_but_resets_later_wave(self) -> None:
        orphan_remove = _event(
            1_200,
            10,
            state="StateRemoved",
            prior=0,
            current=0,
            source=None,
        )
        earlier = _audit([orphan_remove], end=(1_100, 9))
        lifecycle = earlier["factual_lifecycles"][0]
        self.assertIsNone(
            lifecycle["factual_lifecycle"]["initial_amount_at_wave_start"]
        )
        self.assertIn(
            "ORPHAN_STATE_REMOVED_HAS_UNKNOWN_PREDECESSOR",
            lifecycle["loo_counterfactual"]["blocker_codes"],
        )
        self.assertFalse(earlier["empty_zero_proof"]["proven"])

        later = _audit([orphan_remove], start=(1_300, 20), end=(1_500, 30))
        self.assertEqual(later["factual_lifecycles"], [])
        self.assertTrue(later["empty_zero_proof"]["proven"])

    def test_numeric_armor_and_all_downstream_uses_remain_unauthorized(self) -> None:
        result = _audit([])
        self.assertEqual(
            result["claim_boundary"],
            {
                "policy_input_authorized": False,
                "comparison_input_authorized": False,
                "training_authorized": False,
                "dynamic_armor_projection_authorized": False,
                "numeric_armor_value_authorized": False,
                "simulator_action_authorized": False,
                "bonereaver_projected_as_hostile_reduction": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
