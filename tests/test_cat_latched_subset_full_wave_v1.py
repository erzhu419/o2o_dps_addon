from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
)
from o2o_dps.cat_latched_subset_full_wave_v1 import (
    SCHEMA,
    TARGET_ROUTE,
    evaluate_latched_subset_full_wave_case_v1,
    subset_resolution_receipt_valid_v1,
)
from o2o_dps.cat_latched_subset_policy_v1 import (
    POLICY_ID,
    PARENT_DECISION_CONTRACT,
    RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
    RESOLUTION_SCHEMA,
)
from o2o_dps.development_wave_case_v1 import DevelopmentWaveCaseV1
from o2o_dps.factored_external_press_matrix_v1 import (
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
)


def _guard(*predicates: tuple[str, str]) -> dict[str, object]:
    return {
        "decision_contract": PARENT_DECISION_CONTRACT,
        "action": "ADD_HS_QUEUE",
        "predicate_count": len(predicates),
        "predicates": [
            {"feature": feature, "value": value}
            for feature, value in predicates
        ],
        "nonmatch_action": "EXACT_CAT_FALLBACK",
    }


def _features(*, flurry: str = "INACTIVE", rage: str = "MID") -> dict[str, str]:
    return {
        "hp_phase": "MIDDLE",
        "rage_band": rage,
        "live_target_count": "ONE",
        "flurry_state": flurry,
        "queued_swing_state": "KEEP",
        "swing_timing_band": "WITHIN_1_5",
        "bloodthirst_cooldown_band": "READY",
        "whirlwind_cooldown_band": "LATER",
        "cooldown_relation": "BT_FIRST",
        "execution_phase": "GCD_READY",
        "combat_elapsed_band": "ESTABLISHED",
        "weapon_mode": "DUAL_WIELD",
    }


def _receipt(
    resolution: str,
    guard: dict[str, object],
    *,
    candidate_id: str = "subset-0000",
) -> dict[str, object]:
    if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY:
        return {
            "schema": RESOLUTION_SCHEMA,
            "policy_id": POLICY_ID,
            "selected_candidate_id": candidate_id,
            "frozen_guard": deepcopy(guard),
            "decision_index": None,
            "resolution": resolution,
            "reason_code": "NO_EXACT_PARENT_OPPORTUNITY_BEFORE_TERMINAL",
            "decisions_observed": 3,
            "guard_evaluated": False,
            "guard_matched": None,
            "exact_hs_available_row": None,
        }
    active = resolution == RESOLUTION_ABSTAINED_ACTIVE
    nonmatch = resolution == RESOLUTION_ABSTAINED_SUBSET_NONMATCH
    features = _features(
        flurry="ACTIVE" if active else "INACTIVE",
        rage="MID" if nonmatch else "LOW",
    )
    reason = {
        RESOLUTION_INTERVENED: "FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_MATCH",
        RESOLUTION_ABSTAINED_ACTIVE: "FIRST_EXACT_OPPORTUNITY_FLURRY_ACTIVE",
        RESOLUTION_ABSTAINED_SUBSET_NONMATCH: (
            "FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_NONMATCH"
        ),
    }[resolution]
    cat_proposal = {"lane": "cat"}
    return {
        "schema": RESOLUTION_SCHEMA,
        "policy_id": POLICY_ID,
        "selected_candidate_id": candidate_id,
        "frozen_guard": deepcopy(guard),
        "decision_index": 0,
        "resolution": resolution,
        "reason_code": reason,
        "current_features": features,
        "guard_evaluated": not active,
        "guard_matched": None if active else not nonmatch,
        "exact_hs_available_row": {
            "index": 7,
            "action": HS_ACTION_REF.to_wire(),
            "label": "warrior.heroic_strike_queue",
            "legal": True,
            "ready_in_ms": 0,
            "triggers_gcd": False,
        },
        "cat_proposal": cat_proposal,
        "candidate_proposal": (
            {"lane": "candidate"}
            if resolution == RESOLUTION_INTERVENED else cat_proposal
        ),
    }


def _event(*, deferred: bool = False, accepted: bool = True) -> dict[str, object]:
    action = HS_ACTION_REF.to_wire()
    return {
        "source_sink": {
            "channel": "swing_queue",
            "operation": "QueueSpellByName",
            "value": "英勇打击",
            "source_ref": "cat_action_branch_candidate/v1:ADD_HS_QUEUE",
        },
        "operation_contract": {"recognized": True, "action_ref": action},
        "simulator_submission": {
            "status": "SUBMITTED",
            "action": action,
            "available_action": {
                "index": 7,
                "action": action,
                "label": "warrior.heroic_strike_queue",
                "legal": True,
                "ready_in_ms": 0,
                "triggers_gcd": False,
            },
        },
        "simulator_acceptance": {
            "status": "ACCEPTED" if accepted else "REJECTED",
        },
        "decision_consumption": {
            "consumes_decision": False,
            "expected_for_lane": False,
            "available_action_triggers_gcd": False,
        },
        "queue_transition": {
            "kind": (
                "ACCEPTED_QUEUE_STATE_NOT_OBSERVED"
                if deferred else "QUEUED" if accepted else "REJECTED_UNCHANGED"
            ),
            "before": "KEEP",
            "requested": "HEROIC_STRIKE",
            "after": "KEEP" if deferred or not accepted else "HEROIC_STRIKE",
            "cancel_requested": False,
        },
        "simulator_state_before": {
            "time_ms": 2_000,
            "mh_swing_remaining_ms": 1_748,
            "auras": [],
        },
    }


def _case() -> DevelopmentWaveCaseV1:
    return DevelopmentWaveCaseV1(
        case_spec={"source_wave_ref": "wave"},
        request={},
        dynamic_load=SimpleNamespace(
            seed=267_000_000_000,
            request_sha256="request",
            contract_sha256="load",
        ),
        target_contexts={},
    )


class CatLatchedSubsetFullWaveV1Tests(unittest.TestCase):
    def test_receipt_validator_binds_identity_guard_and_match_semantics(self) -> None:
        guard = _guard(("rage_band", "LOW"))
        valid = _receipt(RESOLUTION_INTERVENED, guard)
        self.assertTrue(subset_resolution_receipt_valid_v1(
            valid,
            resolution=RESOLUTION_INTERVENED,
            resolution_count=1,
            selected_candidate_id="subset-0000",
            frozen_guard=guard,
        ))
        for field, value in (
            ("policy_id", "old-broad-policy"),
            ("selected_candidate_id", "subset-0001"),
            ("frozen_guard", _guard(("rage_band", "HIGH"))),
            ("guard_matched", False),
            ("reason_code", "WRONG_REASON"),
        ):
            tampered = deepcopy(valid)
            tampered[field] = value
            with self.subTest(field=field):
                self.assertFalse(subset_resolution_receipt_valid_v1(
                    tampered,
                    resolution=RESOLUTION_INTERVENED,
                    resolution_count=1,
                    selected_candidate_id="subset-0000",
                    frozen_guard=guard,
                ))

    def _evaluate(
        self,
        resolution: str,
        guard: dict[str, object],
        *,
        deferred: bool = False,
        observe_aura: bool = False,
        later_retry: bool = False,
        fallback_exact: bool = False,
        receipt_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        receipt = _receipt(resolution, guard)
        receipt.update(receipt_override or {})
        cat = {
            "presses": ([{
                "decision_index": 0,
                "proposal": {"lane": "cat"},
            }] if resolution != RESOLUTION_NO_PARENT_OPPORTUNITY else []),
            "press_clock_configuration_mode": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
        }
        candidate = deepcopy(cat)
        if resolution == RESOLUTION_INTERVENED:
            candidate["presses"] = [{
                "decision_index": 0,
                "proposal": {"lane": "candidate"},
                "ordered_execution": {"sink_events": [_event(deferred=deferred)]},
                "simulator_state_after_press": {"time_ms": 2_000, "auras": []},
            }]
            if observe_aura:
                candidate["presses"].append({
                    "decision_index": 1,
                    "proposal": {"lane": "cat"},
                    "ordered_execution": {"sink_events": []},
                    "simulator_state_before": {
                        "time_ms": 2_100,
                        "auras": [{"action": HS_ACTION_REF.to_wire()}],
                    },
                    "simulator_state_after_press": {
                        "time_ms": 2_100,
                        "auras": [{"action": HS_ACTION_REF.to_wire()}],
                    },
                })
            elif later_retry:
                retry = _event(deferred=False)
                retry["source_sink"]["source_ref"] = "cat_fury_full_policy/v4"
                candidate["presses"].append({
                    "decision_index": 1,
                    "proposal": {"lane": "cat"},
                    "ordered_execution": {"sink_events": [retry]},
                    "simulator_state_before": {"time_ms": 2_100, "auras": []},
                    "simulator_state_after_press": {
                        "time_ms": 2_200,
                        "auras": [{"action": HS_ACTION_REF.to_wire()}],
                    },
                })

        def fake_lane(_case, adapter, _factory, **_kwargs):
            if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY:
                adapter.decision_count = 3
                adapter.resolution = None
                adapter.resolution_receipts = []
            else:
                adapter.resolution = resolution
                adapter.resolution_receipts = [deepcopy(receipt)]
            return deepcopy(candidate)

        with (
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1.mechanism_route_v1",
                return_value=TARGET_ROUTE,
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1._shared_baseline",
                return_value=(cat, {}, {
                    "technical_receipts_ready": True,
                    "cat_terminal": {"own_effective_damage": 100.0},
                }),
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1._lane",
                side_effect=fake_lane,
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1._terminal_receipt",
                return_value={
                    "status": "COMPLETED",
                    "own_effective_damage": (
                        110.0 if resolution == RESOLUTION_INTERVENED else 100.0
                    ),
                },
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1._semantic_receipt",
                return_value={"valid": True},
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1._same_press_prefix",
                return_value=0,
            ),
            patch(
                "o2o_dps.cat_latched_subset_full_wave_v1."
                "_exact_active_fallback_identity",
                return_value={"exact": fallback_exact},
            ),
        ):
            return evaluate_latched_subset_full_wave_case_v1(
                _case(),
                lambda: None,
                "subset-0000",
                guard,
                item_database={},
            )

    def test_empty_guard_uses_subset_identity_and_accepts_immediate_queue(self) -> None:
        result = self._evaluate(RESOLUTION_INTERVENED, _guard())
        self.assertEqual(SCHEMA, result["schema"])
        self.assertEqual(POLICY_ID, result["policy_id"])
        self.assertEqual("subset-0000", result["selected_candidate_id"])
        self.assertEqual(_guard(), result["frozen_guard"])
        self.assertTrue(result["comparison_ready"])
        self.assertEqual(10.0, result["paired_effective_damage_delta"])
        self.assertEqual(POLICY_ID, result["resolution_receipt"]["policy_id"])

    def test_deferred_confirmation_rejects_later_retry_attribution(self) -> None:
        confirmed = self._evaluate(
            RESOLUTION_INTERVENED,
            _guard(),
            deferred=True,
            observe_aura=True,
        )
        self.assertTrue(confirmed["comparison_ready"])
        self.assertEqual(
            "DEFERRED_QUEUE_AURA_CONFIRMED_BEFORE_MH_SWING",
            confirmed["candidate_deferred_queue_confirmation"]["status"],
        )

        retry = self._evaluate(
            RESOLUTION_INTERVENED,
            _guard(),
            deferred=True,
            later_retry=True,
        )
        self.assertFalse(retry["comparison_ready"])
        self.assertIsNone(retry["paired_effective_damage_delta"])
        self.assertEqual(
            "UNKNOWN_AMBIGUOUS_LATER_HS_SUBMISSION",
            retry["candidate_deferred_queue_confirmation"]["status"],
        )

    def test_all_terminal_abstentions_require_exact_cat_fallback(self) -> None:
        cases = (
            (RESOLUTION_ABSTAINED_ACTIVE, _guard()),
            (
                RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
                _guard(("rage_band", "HIGH")),
            ),
            (RESOLUTION_NO_PARENT_OPPORTUNITY, _guard()),
        )
        for resolution, guard in cases:
            with self.subTest(resolution=resolution):
                exact = self._evaluate(
                    resolution, guard, fallback_exact=True,
                )
                self.assertTrue(exact["comparison_ready"])
                self.assertTrue(exact["cat_fallback_verified"])
                self.assertEqual(0.0, exact["paired_effective_damage_delta"])

                mismatch = self._evaluate(
                    resolution, guard, fallback_exact=False,
                )
                self.assertFalse(mismatch["comparison_ready"])
                self.assertIsNone(mismatch["paired_effective_damage_delta"])

    def test_invalid_subset_receipt_is_unknown_not_zero(self) -> None:
        result = self._evaluate(
            RESOLUTION_ABSTAINED_ACTIVE,
            _guard(),
            fallback_exact=True,
            receipt_override={"policy_id": "cat_latched_first_opportunity_policy/v1"},
        )
        self.assertFalse(result["resolution_receipt_valid"])
        self.assertFalse(result["comparison_ready"])
        self.assertIsNone(result["paired_effective_damage_delta"])


if __name__ == "__main__":
    unittest.main()
