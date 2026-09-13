from __future__ import annotations

import unittest

from o2o_dps.development_wave_stratified_v1 import (
    WAVE_STRATA, build_stratified_wave_case_v1,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import validate_dynamic_load_request_v3
from o2o_dps.fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from o2o_dps.fury_paired_multiseed_runner_v4 import normalize_runner_scenarios


class DevelopmentWaveStratifiedV1Tests(unittest.TestCase):
    def test_fixed_strata_have_complete_target_models_and_matching_provenance(self) -> None:
        for stratum in WAVE_STRATA:
            with self.subTest(stratum=stratum):
                case, scenario = build_stratified_wave_case_v1(20260913, stratum)
                spec = case.case_spec
                count = len(spec["required_target_ids"])
                self.assertEqual(WAVE_STRATA[stratum], spec["source_wave_ref"])
                self.assertNotIn("source_instance_slug", spec)
                self.assertEqual(2 if stratum == "multi_two" else 1, count)
                self.assertFalse(spec["historical_exact"])
                self.assertFalse(spec["real_superiority_authorized"])
                self.assertEqual(
                    spec["source_evidence"]["per_target_observed_incoming_damage_to_death"],
                    spec["initial_state"]["target_max_hp"],
                )
                self.assertEqual(count, len(case.request["encounter"]["targets"]))
                self.assertEqual(list(range(count)), spec["required_target_indices"])
                self.assertEqual(
                    spec["source_evidence"]["wave_capsule_bundle_sha256"],
                    scenario["catalog_sha256"],
                )
                self.assertEqual(
                    spec["source_evidence"]["wave_capsule_sha256"],
                    case.target_contexts[0].target_max_health_evidence.corpus_sha256,
                )
                self.assertFalse(spec["team_background"]["player_conditioned"])
                self.assertFalse(spec["team_background"]["future_schedule_policy_visible"])
                validate_dynamic_load_request_v3(case.dynamic_load, case.request)
                self.assertEqual(
                    case.target_contexts,
                    target_contexts_from_runner_v4(scenario["target_context_bundle"]),
                )
                normalized = normalize_runner_scenarios([scenario])[0]
                self.assertEqual(case.dynamic_load.request_sha256, normalized["request_sha256"])

    def test_multi_target_uses_both_death_budgets_and_optional_attackability_branch(self) -> None:
        case, scenario = build_stratified_wave_case_v1(
            19, "multi_two", attackability_branch="observed_hostile_activity_proxy"
        )
        self.assertEqual([234, 0], case.case_spec["initial_state"]["target_attackable_at_ms"])
        self.assertEqual(2, len(case.dynamic_load.config.attackability_events))
        self.assertEqual(0, case.dynamic_load.config.attackability_events[0].target_index)
        self.assertFalse(case.dynamic_load.config.attackability_events[0].attackable)
        self.assertEqual(234, case.dynamic_load.config.attackability_events[1].time_ms)
        self.assertEqual(2, scenario["target_context_bundle"]["target_count"])
        self.assertEqual(2, len(case.dynamic_load.config.target_health))
        self.assertEqual(2, len(case.case_spec["team_background"]["per_target"]))

    def test_unknown_stratum_and_attackability_branch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_stratified_wave_case_v1(1, "not_a_stratum")
        with self.assertRaises(ValueError):
            build_stratified_wave_case_v1(1, "single_short", attackability_branch="future_truth")


if __name__ == "__main__":
    unittest.main()
