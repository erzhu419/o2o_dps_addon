from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_sparse_guard_policy_v2 import FEATURE_ORDER, SparseGuardV2
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.factored_cat_branch_router_v1 import MechanismRouteV1
from o2o_dps.factored_external_press_matrix_v1 import (
    MATRIX_RANKS,
    MATRIX_STRATA,
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
    SPARSE_SHORTLIST_SCHEMA,
)
from o2o_dps.factored_sparse_guard_full_wave_v1 import (
    AUTHORIZATION_SCHEMA,
    CASE_SCHEMA,
    EXECUTION_CONTRACT_SCHEMA,
    FRESH_PHASE,
    FRESH_SCHEMA,
    PHASE_SEED_BASES,
    SCHEMA,
    TRANSFER_PHASE,
    authorize_sparse_guard_transfer_v1,
    evaluate_sparse_guard_case_v1,
    reduce_sparse_guard_fresh_v1,
    sparse_full_wave_seed_v1,
    validate_sparse_shortlist_v5,
)
from o2o_dps.factored_sparse_guard_learner_v2 import SCHEMA as LEARNER_SCHEMA
from tests.test_cat_external_press_action_teacher_v1 import (
    FakeWholeWaveBridge,
    _fake_execute,
    _policy_state,
)
from tests.test_cat_external_press_pilot_v1 import TARGET


ROUTE = MechanismRouteV1(
    "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
)
GUARD = SparseGuardV2("DEFER_GCD", "hp_phase", "MIDDLE")


class AtomicFakeWholeWaveBridge(FakeWholeWaveBridge):
    def load_dynamic_v3_press_clock(
        self, request, seed, config, *, period_ms, phase_ms=0,
    ):
        del request, config
        self.commands.append("load_dynamic_v3_press_clock")
        self.period = period_ms
        self.next_time = phase_ms
        return SimpleNamespace(state=self._state(), receipt={"seed": seed})


def _semantic() -> dict[str, object]:
    return {
        "period_ms": 100,
        "max_presses": 400,
        "required_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "bridge_version_tag": "V19",
    }


def _shortlist(*guards: SparseGuardV2) -> dict[str, object]:
    guard_rows = [asdict(guard) for guard in guards]
    learner = {
        "schema": LEARNER_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "feature_order": list(FEATURE_ORDER),
        "authorization_claimed": False,
        "full_wave_candidate_policy_evaluated": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "cat_arm": {
            "role": "PERMANENT_FALLBACK",
            "residual_effective_damage_delta": 0.0,
        },
        "routes": [{
            "mechanism_route": asdict(ROUTE),
            "shortlist": guard_rows,
            "shortlist_count": len(guard_rows),
            "candidate_diagnostics": [
                {"guard": row, "shortlisted": True} for row in guard_rows
            ],
        }],
        "route_count": 1,
        "shortlisted_guard_count": len(guard_rows),
    }
    return {
        "schema": SPARSE_SHORTLIST_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "SPARSE_GUARD_SHORTLIST_READY_NONVOTING"
            if guards else "ABSTAIN_NO_STABLE_REACHABLE_SPARSE_GUARD"
        ),
        "training_seeds": list(range(261_000_000_000, 261_000_000_008)),
        "training_distinct_seed_count": 8,
        "execution_semantic_contract": _semantic(),
        "learner": learner,
        "raw_chronicle_rows_loaded": False,
        "full_wave_candidate_policy_evaluated": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _valid_semantics(case, artifact):
    del case, artifact
    return {
        "status": "VALID_CURRENT_SEMANTIC_RECEIPTS",
        "valid": True,
        "reason_codes": [],
        "policy_press_count": 3,
        "target_context_count": 1,
        "future_team_schedule_visible_to_policy": False,
    }


def _projection(rank: int, stratum: str, seed: int) -> dict[str, object]:
    return {
        "schema": "factored_external_press_matrix_case_projection/v1",
        "rank": rank,
        "stratum": stratum,
        "seed": seed,
        "request_sha256": f"request-{rank}-{stratum}-{seed}",
        "dynamic_load_contract_sha256": f"load-{rank}-{stratum}-{seed}",
        "case_spec": {"source_wave_ref": f"wave-{stratum}"},
        "request": {
            "raid": {"parties": [{"players": [{
                "equipment": {"items": [{} for _ in range(16)]},
                "talentsString": "",
            }]}]},
            "encounter": {"targets": [{}]},
        },
    }


def _pair(seed: int, projection: dict[str, object], effect: float | None) -> dict[str, object]:
    if effect is None:
        status = "UNKNOWN_INCOMPLETE_SPARSE_GUARD_PAIR"
        effect_class = "UNKNOWN_NOT_IMPUTED"
        intervention_count = 1
        strict = accepted = fallback = ready = False
        identity = False
    elif effect == 0.0:
        status = "EXACT_CAT_NO_TRIGGER_FALLBACK"
        effect_class = "EXACT_CAT_FALLBACK_ZERO"
        intervention_count = 0
        strict = accepted = False
        fallback = ready = identity = True
    else:
        status = "COMPLETE_TRIGGERED_SPARSE_GUARD_PAIR"
        effect_class = "TRIGGERED_INTERVENTION"
        intervention_count = 1
        strict = accepted = ready = True
        fallback = identity = False
    intervention = ({
        "decision_index": 4,
        "kind": GUARD.kind,
        "guard": asdict(GUARD),
        "matched_features": {},
    } if intervention_count == 1 else None)
    return {
        "schema": "factored_sparse_guard_full_wave_pair/v1",
        "seed": seed,
        "source_wave_ref": projection["case_spec"]["source_wave_ref"],
        "request_sha256": projection["request_sha256"],
        "dynamic_load_contract_sha256": projection[
            "dynamic_load_contract_sha256"
        ],
        "phase": None,
        "mechanism_route": asdict(ROUTE),
        "guard": asdict(GUARD),
        "status": status,
        "effect_class": effect_class,
        "candidate_intervention_count": intervention_count,
        "candidate_intervention": intervention,
        "accepted_prefix_presses_verified": 4 if strict else None,
        "strict_single_intervention_verified": strict,
        "candidate_branch_action_accepted": accepted,
        "active_cat_fallback_verified": fallback,
        "active_cat_fallback_identity_gate": {"exact": identity},
        "candidate_terminal": {"status": "COMPLETED"},
        "candidate_semantic_receipt": {"valid": True},
        "candidate_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "paired_effective_damage_delta": effect,
        "technical_receipts_ready": ready,
        "comparison_ready": ready,
        "full_press_lane_retained": False,
    }


def _case_artifacts(
    phase: str, policy_schema: str, effects: list[float],
) -> list[dict[str, object]]:
    artifacts = []
    for sample_index, effect in enumerate(effects):
        for rank in MATRIX_RANKS:
            for stratum in MATRIX_STRATA:
                seed = sparse_full_wave_seed_v1(
                    rank, stratum, phase, sample_index,
                )
                projection = _projection(rank, stratum, seed)
                pair = _pair(seed, projection, effect)
                pair["phase"] = phase
                result = {
                    "schema": SCHEMA,
                    "seed": seed,
                    "source_wave_ref": projection["case_spec"]["source_wave_ref"],
                    "request_sha256": projection["request_sha256"],
                    "dynamic_load_contract_sha256": projection[
                        "dynamic_load_contract_sha256"
                    ],
                    "phase": phase,
                    "mechanism_route": asdict(ROUTE),
                    "guard_count": 1,
                    "shared_cat_no_op": {
                        "status": "COMPLETE_SHARED_CAT_NOOP_BASELINE",
                        "technical_receipts_ready": True,
                        "cat_terminal": {"status": "COMPLETED"},
                        "no_op_terminal": {"status": "COMPLETED"},
                        "semantic_receipts": {
                            "cat": {"valid": True},
                            "no_op": {"valid": True},
                        },
                        "no_op_identity_gate": {"exact": True},
                        "press_clock_configuration_modes": {
                            "cat": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
                            "no_op": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
                        },
                    },
                    "guard_pairs": [pair],
                }
                artifacts.append({
                    "schema": CASE_SCHEMA,
                    "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
                    "status": "COMPLETE_SPARSE_FULL_WAVE_CASE_ARTIFACT_NONVOTING",
                    "matrix": {
                        "rank": rank,
                        "stratum": stratum,
                        "phase": phase,
                        "sample_index": sample_index,
                        "seed": seed,
                    },
                    "case_projection": projection,
                    "mechanism_route": asdict(ROUTE),
                    "execution_contract": {
                        "schema": EXECUTION_CONTRACT_SCHEMA,
                        "semantic": {
                            **_semantic(),
                            "shared_cat_no_op_once_per_case": True,
                            "each_guard_independent_fresh_lane": True,
                        },
                        "policy_lineage": {
                            "source_schema": policy_schema,
                            "mechanism_route": asdict(ROUTE),
                            "guards": [asdict(GUARD)],
                        },
                    },
                    "result": result,
                    "full_press_lanes_retained": False,
                    "raw_chronicle_rows_loaded": False,
                    "voting_eligible": False,
                    "deployment_eligible": False,
                })
    return artifacts


class FactoredSparseGuardFullWaveV1Tests(unittest.TestCase):
    def test_final_shortlist_schema_and_route_relative_duplicates_are_rejected(self):
        parsed = validate_sparse_shortlist_v5(_shortlist(GUARD))
        self.assertEqual((GUARD,), parsed[ROUTE])

        redundant = SparseGuardV2(
            "DEFER_GCD", "hp_phase", "MIDDLE", "weapon_mode", "TWO_HAND",
        )
        with self.assertRaisesRegex(ValueError, "duplicate route-relative"):
            validate_sparse_shortlist_v5(_shortlist(GUARD, redundant))

    def test_shared_cat_noop_and_independent_triggered_guard_are_compact(self):
        case = build_development_wave_case_v1(PHASE_SEED_BASES[TRANSFER_PHASE])
        created = []

        def factory():
            created.append(1)
            return AtomicFakeWholeWaveBridge()

        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
            patch(
                "o2o_dps.factored_sparse_guard_full_wave_v1._semantic_receipt",
                side_effect=_valid_semantics,
            ),
            patch(
                "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
                return_value=ROUTE,
            ),
        ):
            result = evaluate_sparse_guard_case_v1(
                case, ROUTE, [GUARD], factory, phase=TRANSFER_PHASE,
                item_database={}, max_presses=10,
            )

        self.assertEqual(3, len(created))
        self.assertEqual(2, result["cat_no_op_lane_count"])
        self.assertEqual(1, result["candidate_lane_count"])
        self.assertTrue(result["shared_cat_no_op"]["technical_receipts_ready"])
        pair = result["guard_pairs"][0]
        self.assertEqual("COMPLETE_TRIGGERED_SPARSE_GUARD_PAIR", pair["status"])
        self.assertTrue(pair["strict_single_intervention_verified"])
        self.assertEqual(10.0, pair["paired_effective_damage_delta"])
        self.assertNotIn("presses", result["shared_cat_no_op"])
        self.assertNotIn("presses", pair)

    def test_active_no_trigger_is_zero_only_with_exact_cat_identity(self):
        case = build_development_wave_case_v1(PHASE_SEED_BASES[TRANSFER_PHASE])
        no_match = SparseGuardV2("DEFER_GCD", "hp_phase", "EARLY")
        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
            patch(
                "o2o_dps.factored_sparse_guard_full_wave_v1._semantic_receipt",
                side_effect=_valid_semantics,
            ),
            patch(
                "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
                return_value=ROUTE,
            ),
        ):
            result = evaluate_sparse_guard_case_v1(
                case, ROUTE, [no_match], AtomicFakeWholeWaveBridge,
                phase=TRANSFER_PHASE, item_database={}, max_presses=10,
            )
        pair = result["guard_pairs"][0]
        self.assertEqual("EXACT_CAT_NO_TRIGGER_FALLBACK", pair["status"])
        self.assertTrue(pair["active_cat_fallback_verified"])
        self.assertEqual(0.0, pair["paired_effective_damage_delta"])

    def test_transfer_gate_uses_balanced_seed_means_and_not_75_percent_wins(self):
        shortlist = _shortlist(GUARD)
        # Five positive and three exact-Cat zero seeds: 62.5% positive, with a
        # still-positive expected-effect lower confidence bound.
        artifacts = _case_artifacts(
            TRANSFER_PHASE, SPARSE_SHORTLIST_SCHEMA,
            [10.0] * 5 + [0.0] * 3,
        )
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=ROUTE,
        ):
            authorized = authorize_sparse_guard_transfer_v1(
                shortlist, artifacts, item_database={},
            )

        self.assertEqual(AUTHORIZATION_SCHEMA, authorized["schema"])
        self.assertEqual(1, authorized["authorized_guard_count"])
        decision = authorized["guard_authorizations"][0]
        self.assertAlmostEqual(
            0.625,
            decision["expected_route_policy_effect_statistics"][
                "positive_seed_fraction"
            ],
        )
        self.assertTrue(decision["authorized_for_fresh"])
        self.assertEqual(8, decision["complete_seed_count"])

    def test_one_invalid_case_makes_the_shared_seed_unknown_not_zero(self):
        shortlist = _shortlist(GUARD)
        artifacts = _case_artifacts(
            TRANSFER_PHASE, SPARSE_SHORTLIST_SCHEMA, [10.0] * 8,
        )
        pair = artifacts[0]["result"]["guard_pairs"][0]
        pair.update(_pair(
            pair["seed"], artifacts[0]["case_projection"], None,
        ))
        pair["phase"] = TRANSFER_PHASE
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=ROUTE,
        ):
            authorized = authorize_sparse_guard_transfer_v1(
                shortlist, artifacts, item_database={},
            )
        decision = authorized["guard_authorizations"][0]
        self.assertEqual(1, decision["unknown_seed_count"])
        self.assertEqual(7, decision["complete_seed_count"])
        self.assertFalse(decision["authorized_for_fresh"])
        self.assertEqual(
            "REJECTED_UNKNOWN_TRANSFER_EFFECTS_NOT_IMPUTED",
            decision["authorization_status"],
        )

    def test_unbalanced_matrix_is_rejected(self):
        shortlist = _shortlist(GUARD)
        artifacts = _case_artifacts(
            TRANSFER_PHASE, SPARSE_SHORTLIST_SCHEMA, [10.0] * 8,
        )
        artifacts.pop()
        with (
            patch(
                "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
                return_value=ROUTE,
            ),
            self.assertRaisesRegex(ValueError, "incomplete across 12 cells"),
        ):
            authorize_sparse_guard_transfer_v1(
                shortlist, artifacts, item_database={},
            )

    def test_untouched_fresh_reducer_keeps_gate_nonvoting(self):
        shortlist = _shortlist(GUARD)
        transfer = _case_artifacts(
            TRANSFER_PHASE, SPARSE_SHORTLIST_SCHEMA, [10.0] * 8,
        )
        with patch(
            "o2o_dps.factored_sparse_guard_full_wave_v1.mechanism_route_v1",
            return_value=ROUTE,
        ):
            authorization = authorize_sparse_guard_transfer_v1(
                shortlist, transfer, item_database={},
            )
            fresh = _case_artifacts(
                FRESH_PHASE, AUTHORIZATION_SCHEMA, [3.0] * 8,
            )
            result = reduce_sparse_guard_fresh_v1(
                authorization, fresh, item_database={},
            )

        self.assertEqual(FRESH_SCHEMA, result["schema"])
        self.assertEqual(1, result["passed_fresh_guard_count"])
        self.assertTrue(result["comparison_ready"])
        self.assertFalse(result["voting_eligible"])
        self.assertFalse(result["deployment_eligible"])
        self.assertTrue(all(
            seed >= PHASE_SEED_BASES[FRESH_PHASE] for seed in result["fresh_seeds"]
        ))


if __name__ == "__main__":
    unittest.main()
