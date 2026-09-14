from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import unittest
from unittest.mock import patch

from o2o_dps.cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    CatSparseGuardPolicyV2,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import QUEUE_REFS
from o2o_dps.factored_cat_branch_router_v1 import MechanismRouteV1
from o2o_dps.factored_external_press_matrix_v1 import SPARSE_SHORTLIST_SCHEMA
from o2o_dps.factored_sparse_guard_full_wave_v1 import (
    FRESH_PHASE,
    TRANSFER_PHASE,
    _policy_routes,
    authorize_sparse_guard_transfer_v1,
)
from o2o_dps.factored_sparse_guard_post_transfer_refinement_v1 import (
    FRESH_RESULT_SCHEMA,
    SCHEMA,
    TARGET_PARENT_GUARD,
    TARGET_REFINED_GUARD,
    build_post_transfer_refinement_v1,
    reduce_refined_sparse_guard_fresh_v1,
    validate_post_transfer_refinement_v1,
)
from o2o_dps.fury_expert_adapters import WeaponMode
from o2o_dps.sim_bridge import AvailableAction
from tests.test_cat_sparse_guard_policy_v2 import _available_state
from tests.test_factored_sparse_guard_full_wave_v1 import (
    _case_artifacts,
    _shortlist,
)


TARGET_ROUTE = MechanismRouteV1(
    "DUAL_WIELD", "slow_2s_plus", "yes", "one", "50k_to_200k",
)


def _replace_route_and_guard(
    artifact: dict[str, object], *, guard=TARGET_PARENT_GUARD,
) -> None:
    route = asdict(TARGET_ROUTE)
    guard_wire = asdict(guard)
    artifact["mechanism_route"] = route
    artifact["execution_contract"]["policy_lineage"].update({
        "mechanism_route": route,
        "guards": [guard_wire],
    })
    result = artifact["result"]
    result["mechanism_route"] = route
    result["guard_count"] = 1
    pair = result["guard_pairs"][0]
    pair["mechanism_route"] = route
    pair["guard"] = guard_wire
    intervention = pair.get("candidate_intervention")
    if isinstance(intervention, dict):
        intervention["kind"] = guard.kind
        intervention["guard"] = guard_wire


def _target_shortlist() -> dict[str, object]:
    artifact = _shortlist(TARGET_PARENT_GUARD)
    row = artifact["learner"]["routes"][0]
    row["mechanism_route"] = asdict(TARGET_ROUTE)
    return artifact


def _features(flurry: str) -> dict[str, str]:
    values = {
        "hp_phase": "MIDDLE",
        "rage_band": "MID",
        "live_target_count": "ONE",
        "flurry_state": flurry,
        "queued_swing_state": "KEEP",
        "swing_timing_band": "LATER",
        "bloodthirst_cooldown_band": "READY",
        "whirlwind_cooldown_band": "LATER",
        "cooldown_relation": "BT_FIRST",
        "execution_phase": "GCD_READY",
        "combat_elapsed_band": "ESTABLISHED",
        "weapon_mode": "DUAL_WIELD",
    }
    return {name: values[name] for name in FEATURE_ORDER}


def _transfer_fixture(*, make_unknown: bool = False):
    shortlist = _target_shortlist()
    # The parent is negative over all seeds, but the six INACTIVE intervention
    # seeds have a positive exploratory split projection.
    artifacts = _case_artifacts(
        TRANSFER_PHASE, SPARSE_SHORTLIST_SCHEMA, [20.0] * 6 + [-100.0] * 2,
    )
    for artifact in artifacts:
        _replace_route_and_guard(artifact)
        sample = artifact["matrix"]["sample_index"]
        pair = artifact["result"]["guard_pairs"][0]
        pair["candidate_intervention"]["matched_features"] = _features(
            "INACTIVE" if sample < 6 else "ACTIVE"
        )
    if make_unknown:
        artifact = artifacts[0]
        pair = artifact["result"]["guard_pairs"][0]
        pair["technical_receipts_ready"] = False
        pair["comparison_ready"] = False
        pair["paired_effective_damage_delta"] = None
    with patch(
        "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
        return_value=TARGET_ROUTE,
    ):
        authorization = authorize_sparse_guard_transfer_v1(
            shortlist, artifacts, item_database={},
        )
    return shortlist, authorization, artifacts


class FactoredSparseGuardPostTransferRefinementV1Tests(unittest.TestCase):
    def test_freezes_only_the_predeclared_refinement_and_marks_projection_nonexact(self):
        shortlist, authorization, artifacts = _transfer_fixture()
        self.assertEqual(0, authorization["authorized_guard_count"])
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=TARGET_ROUTE,
        ):
            result = build_post_transfer_refinement_v1(
                shortlist, authorization, artifacts, item_database={},
            )

        self.assertEqual(SCHEMA, result["schema"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(1, result["frozen_refined_guard_count"])
        self.assertFalse(result["prior_full_wave_authorization_claimed"])
        self.assertFalse(result["standard_refined_guard_actual_full_wave_evaluated"])
        candidate = result["candidate_diagnostics"][0]
        self.assertEqual(asdict(TARGET_REFINED_GUARD), candidate["refined_guard"])
        self.assertEqual(6, candidate["matching_parent_intervention_seed_count"])
        self.assertGreater(
            candidate["exploratory_projected_effect_statistics"][
                "lower_95_normal_effective_damage_delta_bound"
            ],
            0,
        )
        self.assertFalse(
            candidate["exploratory_projection_is_exact_refined_policy_effect"]
        )
        self.assertEqual(
            {TARGET_ROUTE: (TARGET_REFINED_GUARD,)},
            validate_post_transfer_refinement_v1(result),
        )
        # Existing full-wave workers accept this only as an untouched-fresh
        # policy source, without converting it into an authorization artifact.
        self.assertEqual(
            {TARGET_ROUTE: (TARGET_REFINED_GUARD,)},
            _policy_routes(result, FRESH_PHASE),
        )

    def test_unknown_parent_case_is_not_imputed_and_blocks_freeze(self):
        shortlist, authorization, artifacts = _transfer_fixture(make_unknown=True)
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=TARGET_ROUTE,
        ):
            result = build_post_transfer_refinement_v1(
                shortlist, authorization, artifacts, item_database={},
            )
        self.assertEqual(0, result["frozen_refined_guard_count"])
        candidate = result["candidate_diagnostics"][0]
        self.assertEqual(1, candidate["unknown_seed_count"])
        self.assertEqual(
            "REJECTED_UNKNOWN_PARENT_EFFECTS_NOT_IMPUTED",
            candidate["refinement_gate_status"],
        )

    def test_actual_refined_policy_skips_active_then_triggers_on_inactive(self):
        policy = CatSparseGuardPolicyV2(TARGET_REFINED_GUARD)
        queue = AvailableAction(
            index=0,
            action=QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
            label="Heroic Strike",
            legal=True,
            ready_in_ms=0,
            triggers_gcd=False,
        )
        active = _available_state(
            weapon_mode=WeaponMode.DUAL_WIELD,
            flurry_talent=True,
            flurry_active=True,
            rage=70.0,
        )
        policy.bind_current_available_actions_v2([queue])
        policy.propose(active)
        self.assertEqual([], policy.intervention_receipts)

        inactive = _available_state(
            weapon_mode=WeaponMode.DUAL_WIELD,
            flurry_talent=True,
            flurry_active=False,
            rage=70.0,
        )
        policy.bind_current_available_actions_v2([queue])
        policy.propose(inactive)
        self.assertEqual(1, len(policy.intervention_receipts))
        self.assertEqual(1, policy.intervention_receipts[0]["decision_index"])
        self.assertEqual(
            "INACTIVE",
            policy.intervention_receipts[0]["matched_features"]["flurry_state"],
        )

    def test_fresh_reducer_requires_64_and_uses_actual_full_wave_effects(self):
        shortlist, authorization, transfer = _transfer_fixture()
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=TARGET_ROUTE,
        ):
            refinement = build_post_transfer_refinement_v1(
                shortlist, authorization, transfer, item_database={},
            )
        fresh = _case_artifacts(FRESH_PHASE, SCHEMA, [3.0] * 64)
        for artifact in fresh:
            _replace_route_and_guard(artifact, guard=TARGET_REFINED_GUARD)
        with self.assertRaisesRegex(ValueError, "64 seeds"):
            reduce_refined_sparse_guard_fresh_v1(
                refinement, fresh, item_database={}, min_distinct_seeds=63,
            )
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=TARGET_ROUTE,
        ):
            result = reduce_refined_sparse_guard_fresh_v1(
                refinement, fresh, item_database={},
            )
        self.assertEqual(FRESH_RESULT_SCHEMA, result["schema"])
        self.assertEqual(1, result["passed_fresh_guard_count"])
        self.assertTrue(result["standard_refined_guard_actual_full_wave_evaluated"])
        self.assertFalse(result["prior_full_wave_authorization_claimed"])
        self.assertTrue(
            result["guard_results"][0][
                "passed_untouched_fresh_actual_policy_gate"
            ]
        )


if __name__ == "__main__":
    unittest.main()
