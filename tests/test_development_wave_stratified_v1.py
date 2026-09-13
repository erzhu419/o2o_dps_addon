from __future__ import annotations

import csv
from pathlib import Path
import unittest

from o2o_dps.development_wave_stratified_v1 import (
    SOURCE_FOCAL_ACTORS, WAVE_STRATA, _source_rows,
    build_stratified_wave_case_v1, reduce_stratified_wave_panels_v1,
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
                self.assertEqual(
                    "EXOGENOUS_PER_TARGET_DIRECT_GUID_LEAVE_ONE_OUT_RATE_EXTRAPOLATED",
                    spec["team_background"]["model"],
                )
                self.assertEqual(
                    SOURCE_FOCAL_ACTORS[stratum]["guid"],
                    spec["team_background"]["source_focal_actor"]["guid"],
                )
                for row in spec["team_background"]["per_target"]:
                    index = row["target_index"]
                    self.assertEqual(
                        row["modeled_team_damage_budget"]
                        + row["excluded_focal_direct_guid_damage"],
                        spec["initial_state"]["target_max_hp"][index],
                    )
                    events = [
                        event for event in case.dynamic_load.config.background_damage_events
                        if event.target_index == index
                    ]
                    self.assertEqual(row["scheduled_hit_count_to_watchdog"], len(events))
                    self.assertGreater(len(events), row["source_fit_hit_count"])
                    self.assertTrue(row["post_source_death_rate_extrapolated"])
                    self.assertAlmostEqual(
                        row["modeled_team_damage_budget"],
                        sum(event.damage for event in events[:row["source_fit_hit_count"]]),
                        delta=1e-6,
                    )
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

    def test_direct_guid_leave_one_out_matches_source_csv(self) -> None:
        # Source CSVs stay local; remote execution stages only these audited
        # small aggregates. DEAD carries the final damaging hit in this export.
        source_rows = _source_rows()
        root = Path(__file__).resolve().parents[1] / "offline_data"
        if not (root / next(iter(source_rows.values()))["targets"][0]
                ["max_health_hypothesis_family"]["observed_kill_budget_proxy"]
                ["death_anchor"]["raw_file"]).exists():
            self.skipTest("raw Chronicle CSVs are not staged on compute nodes")
        markers = {"Bloodthirst", "Mortal Strike", "Whirlwind", "Slam", "Execute"}
        for stratum, wave_id in WAVE_STRATA.items():
            with self.subTest(stratum=stratum):
                source = source_rows[wave_id]
                encounter = source["source_identity"]["encounter_id"]
                targets = {target["target_guid"]: target for target in source["targets"]}
                death_index = {
                    guid: int(target["max_health_hypothesis_family"]
                              ["observed_kill_budget_proxy"]["death_anchor"]["event_index"])
                    for guid, target in targets.items()
                }
                raw_file = (
                    root / source["targets"][0]["max_health_hypothesis_family"]
                    ["observed_kill_budget_proxy"]["death_anchor"]["raw_file"]
                )
                eligible = set()
                names = {}
                observed = {guid: 0 for guid in targets}
                actor_damage = {}
                actor_counts = {}
                with raw_file.open(encoding="utf-8-sig", newline="") as stream:
                    for row in csv.DictReader(stream):
                        if row["Encounter"] != encounter:
                            continue
                        event_index = int(row["Event Index"])
                        if event_index > max(death_index.values()):
                            break
                        actor = row["Source GUID"]
                        target = row["Target GUID"]
                        if actor.startswith("0x0000") and row["Action / Ability"] in markers:
                            eligible.add(actor)
                            names[actor] = row["Source"]
                        if row["Type"] not in {"DMG", "DEAD"} or target not in targets:
                            continue
                        if event_index > death_index[target]:
                            continue
                        damage = int(row["Value"].replace(",", ""))
                        observed[target] += damage
                        actor_damage.setdefault(actor, {}).setdefault(target, 0)
                        actor_damage[actor][target] += damage
                        actor_counts.setdefault(actor, {}).setdefault(target, 0)
                        actor_counts[actor][target] += 1
                selected = min(actor for actor in eligible if sum(actor_damage.get(actor, {}).values()) > 0)
                focal = SOURCE_FOCAL_ACTORS[stratum]
                self.assertEqual(focal["guid"], selected)
                self.assertEqual(focal["name"], names[selected])
                self.assertEqual(
                    [target["max_health_hypothesis_family"]["observed_kill_budget_proxy"]["value"]
                     for target in targets.values()],
                    [observed[guid] for guid in targets],
                )
                self.assertEqual(focal["direct_damage_by_target"],
                                 [actor_damage[selected].get(guid, 0) for guid in targets])
                self.assertEqual(focal["direct_hit_count_by_target"],
                                 [actor_counts[selected].get(guid, 0) for guid in targets])

    def test_pre_correction_panels_cannot_be_reduced_with_new_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "pre-extrapolation"):
            reduce_stratified_wave_panels_v1(
                [{"schema": "development_wave_stratified_panel/v2", "seed": 1}],
                expected_seed_count=1,
            )


if __name__ == "__main__":
    unittest.main()
