from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_policy_optimization_v1 import FuryPolicyParameters
import o2o_dps.fury_singleton_health_sensitivity_v1 as health_module
from o2o_dps.fury_singleton_health_sensitivity_v1 import (
    CompletedFeature,
    FurySingletonHealthError,
    HealthFeatureSnapshot,
    HealthInstanceSplit,
    HealthProfile,
    SingletonHealthCorpus,
    SingletonHealthFamily,
    build_artifact,
    evaluate_singleton_health_sensitivity,
    load_health_feature_snapshot,
    select_singleton_health_families,
    split_health_instances,
)


def _write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(value, handle)


def _base_request() -> dict[str, object]:
    return {
        "raid": {"parties": [{"players": [{"name": "Fury"}]}]},
        "encounter": {
            "duration": 300,
            "durationVariation": 30,
            "useHealth": False,
            "targets": [
                {
                    "id": 1,
                    "name": "Template",
                    "level": 63,
                    "stats": [0.0] * 46,
                    "minBaseDamage": 1000,
                    "tankIndex": 0,
                }
            ],
        },
        "simOptions": {"iterations": 1, "interactive": True},
    }


def _target(guid: str, entry_id: int, name: str) -> dict[str, object]:
    return {
        "target_guid": guid,
        "creature_entry_id": entry_id,
        "target_name": name,
        "activity_interval": {
            "first_anchor": {"offset_ms": 0, "normalized_row_index": 1},
            "last_anchor": {"offset_ms": 1000, "normalized_row_index": 2},
        },
        "kill_budget_proxy": {
            "value": 1000,
            "status": "OBSERVED",
            "death_anchor": {"offset_ms": 1000, "normalized_row_index": 2},
        },
    }


def _wave(
    ordinal: int,
    targets: list[dict[str, object]],
    duration_ms: int,
) -> dict[str, object]:
    guids = [str(value["target_guid"]) for value in targets]
    return {
        "wave_id": f"enc:wave:{ordinal}",
        "ordinal": ordinal,
        "start_offset_ms": 0,
        "end_offset_ms": duration_ms,
        "duration_ms": duration_ms,
        "target_count": len(targets),
        "targets": targets,
        "target_groups": [
            {
                "group_id": f"group-{index}",
                "target_guids": [guid],
                "position_assumption": {
                    "value": "unknown",
                    "status": "MISSING",
                    "basis": "no positive co-hit evidence",
                    "coordinates": None,
                },
            }
            for index, guid in enumerate(guids, start=1)
        ],
        "position_assumption": {
            "value": "unknown",
            "status": "MISSING",
            "basis": "no positive co-hit evidence",
            "coordinates": None,
        },
        "first_anchor": {"offset_ms": 0, "normalized_row_index": ordinal * 10},
        "last_anchor": {
            "offset_ms": duration_ms,
            "normalized_row_index": ordinal * 10 + 1,
        },
    }


def _feature_report(instance: str, waves: list[dict[str, object]]) -> dict[str, object]:
    return {
        "kind": "chronicle_encounter_reconstruction_v1",
        "encounters": [
            {
                "instance": instance,
                "encounter": "enc",
                "waves": waves,
            }
        ],
    }


def _health_item(entry_id: int, name: str) -> dict[str, object]:
    return {
        "identity": {
            "creature_entry_id": entry_id,
            "target_name": name,
        },
        "kill_budget_proxy_summary": {
            "count": 5,
            "source_file_count": 3,
        },
        "health_scenario": {
            "status": "INFERRED",
            "center": 1000,
            "range": [800, 1200],
            "not_equal_to": "exact NPC health distribution",
        },
    }


def _family() -> SingletonHealthFamily:
    profile = HealthProfile(100, "Mob", 5, 3, 800.0, 1000.0, 1200.0)
    return SingletonHealthFamily(
        instance_id="heldout",
        feature_relative_path="features/heldout.json.gz",
        encounter_id="enc",
        wave_id="enc:wave:1",
        observed_duration_ms=20_000,
        duration_stratum="medium_10_30s",
        creature_entry_id=100,
        target_name="Mob",
        health_profile=profile,
        source_wave=_wave(1, [_target("Creature-0-0-0-0-100-1", 100, "Mob")], 20_000),
        sampling_weight=2.0,
    )


class SnapshotAndSelectionTests(unittest.TestCase):
    def test_completed_only_feature_snapshot_and_require_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "base.json"
            base_path.write_text(json.dumps(_base_request()), encoding="utf-8")
            feature_path = root / "features" / "inst-a.json.gz"
            _write_gzip_json(feature_path, _feature_report("inst-a", []))
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "kind": "chronicle_encounter_batch_manifest_v1",
                        "updated_at": "snapshot",
                        "output": {"directory": str(root)},
                        "artifact_contract": {
                            "base_request": {"path": str(base_path)}
                        },
                        "npc_identity_aggregates": {
                            "items": [_health_item(100, "Mob")]
                        },
                        "entries": [
                            {
                                "source": {"name": "inst-a__stamp.jsonl"},
                                "processing_status": "COMPLETED",
                                "outputs": {
                                    "feature_report": {
                                        "path": "features/inst-a.json.gz"
                                    }
                                },
                            },
                            {
                                "source": {"name": "inst-b__stamp.jsonl"},
                                "processing_status": "PENDING",
                                "outputs": None,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_health_feature_snapshot(manifest_path)
            self.assertEqual(snapshot.completed_instance_ids, ("inst-a",))
            self.assertEqual(snapshot.status_counts, {"COMPLETED": 1, "PENDING": 1})
            self.assertEqual(snapshot.health_profiles[100].median, 1000)
            with self.assertRaisesRegex(FurySingletonHealthError, "require-complete"):
                load_health_feature_snapshot(manifest_path, require_complete=True)

    def test_instance_split_is_seeded_and_excludes_training_instance(self) -> None:
        features = tuple(
            CompletedFeature(value, f"{value}__stamp.jsonl", f"{value}.gz", Path(f"{value}.gz"))
            for value in ("a", "b", "c", "d")
        )
        snapshot = HealthFeatureSnapshot(
            Path("manifest.json"),
            None,
            Path("."),
            {"COMPLETED": 4},
            features,
            {100: HealthProfile(100, "Mob", 5, 3, 800, 1000, 1200)},
            Path("base.json"),
            True,
        )
        first = split_health_instances(
            snapshot,
            split_seed=17,
            candidate_training_instances=("a",),
            heldout_count=2,
        )
        second = split_health_instances(
            snapshot,
            split_seed=17,
            candidate_training_instances=("a",),
            heldout_count=2,
        )
        self.assertEqual(first, second)
        self.assertNotIn("a", first.heldout_instances)

    def test_selection_reads_feature_only_and_rejects_multi_target_singletons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feature_path = root / "heldout.json.gz"
            waves = [
                _wave(1, [_target("Creature-1", 100, "A")], 5_000),
                _wave(2, [_target("Creature-2", 101, "B")], 20_000),
                _wave(3, [_target("Creature-3", 102, "C")], 50_000),
                _wave(
                    4,
                    [
                        _target("Creature-4", 100, "A"),
                        _target("Creature-5", 101, "B"),
                    ],
                    8_000,
                ),
            ]
            _write_gzip_json(feature_path, _feature_report("heldout", waves))
            profiles = {
                entry: HealthProfile(entry, name, 5, 3, 800, 1000, 1200)
                for entry, name in ((100, "A"), (101, "B"), (102, "C"))
            }
            snapshot = HealthFeatureSnapshot(
                root / "manifest.json",
                None,
                root,
                {"COMPLETED": 1},
                (
                    CompletedFeature(
                        "heldout",
                        "heldout__stamp.jsonl",
                        "heldout.json.gz",
                        feature_path,
                    ),
                ),
                profiles,
                root / "base.json",
                True,
            )
            split = HealthInstanceSplit(1, ("train",), ("heldout",), ("train",))
            corpus = select_singleton_health_families(
                snapshot,
                split,
                selection_seed=9,
                max_families_per_instance=3,
            )

            self.assertEqual(len(corpus.families), 3)
            self.assertEqual(corpus.completed_feature_gzip_read_count, 1)
            self.assertEqual(
                {value.duration_stratum for value in corpus.families},
                {"short_le_10s", "medium_10_30s", "long_gt_30s"},
            )
            self.assertTrue(
                all(value.source_wave["target_count"] == 1 for value in corpus.families)
            )


class EvaluationTests(unittest.TestCase):
    @staticmethod
    def _fake_rollout(adapter, request, *, omission=False, incomplete=False):
        health = float(request["encounter"]["targets"][0]["stats"][34])
        if adapter.expert_id == "cat.fury.profile1":
            elapsed = 1000
        elif adapter.expert_id == "contra.deployed.fury.raid_a":
            elapsed = 1100
        else:
            elapsed = 900
        return {
            "source_execution": False,
            "exact_lua_replay": False,
            "completion_mode": "singleton_health",
            "completion_criterion_met": not incomplete,
            "health_depleted": not incomplete,
            "termination_reason": (
                "watchdog_cap_reached_without_health_depletion"
                if incomplete
                else "health_depleted"
            ),
            "finished": not incomplete,
            "root_time_ms": 0,
            "final_time_ms": elapsed,
            "elapsed_ms": elapsed,
            "damage_delta": health,
            "dps": health / (elapsed / 1000),
            "all_lane_projections_faithful": not omission,
            "omitted_lane_count": 1 if omission else 0,
            "nonfaithful_reason_counts": {"gcd:test": 1} if omission else {},
        }

    def test_q1_median_q3_matched_gate_is_compact_and_passes(self) -> None:
        corpus = SingletonHealthCorpus((_family(),), (), 1)

        def fake_run(bridge, request, adapter, **kwargs):
            self.assertTrue(request["encounter"]["useHealth"])
            self.assertEqual(len(request["encounter"]["targets"]), 1)
            self.assertFalse(kwargs["retain_steps"])
            return self._fake_rollout(adapter, request)

        with patch(
            "o2o_dps.fury_singleton_health_sensitivity_v1.run_fury_expert_closed_loop",
            side_effect=fake_run,
        ):
            result = evaluate_singleton_health_sensitivity(
                object(),
                corpus,
                _base_request(),
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                armor_hypothesis=1721,
                level_hypothesis=60,
                validation_seeds=(1, 2),
                watchdog_cap_ms=5000,
            )

        self.assertEqual(result["rollout_count"], 18)
        self.assertEqual(set(result["health_variant_scopes"]), {"q1", "median", "q3"})
        self.assertTrue(result["health_sensitivity_gate_passed"])
        self.assertFalse(result["retained_rollout_rows"])
        self.assertNotIn("rollouts", result)
        self.assertEqual(result["overall"]["primary_comparison_metric"], "paired_ttk")
        self.assertEqual(
            result["overall"]["ranking"][0]["expert_id"],
            result["candidate_expert_id"],
        )

    def test_paired_ttk_is_primary_when_overkill_makes_candidate_dps_lower(self) -> None:
        corpus = SingletonHealthCorpus((_family(),), (), 1)

        def fake_run(bridge, request, adapter, **kwargs):
            row = self._fake_rollout(adapter, request)
            if adapter.expert_id in health_module.BASELINE_IDS:
                row["damage_delta"] *= 3
                row["dps"] *= 3
            return row

        with patch(
            "o2o_dps.fury_singleton_health_sensitivity_v1.run_fury_expert_closed_loop",
            side_effect=fake_run,
        ):
            result = evaluate_singleton_health_sensitivity(
                object(),
                corpus,
                _base_request(),
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                armor_hypothesis=1721,
                level_hypothesis=60,
                validation_seeds=(1, 2),
                watchdog_cap_ms=5000,
            )

        self.assertTrue(result["health_sensitivity_gate_passed"])
        for comparison in result["overall"]["comparisons_vs_each_baseline"].values():
            self.assertTrue(comparison["more_paired_ttk_wins_than_losses"])
            self.assertFalse(comparison["higher_weighted_dps"])
            self.assertTrue(
                comparison["dps_is_secondary_overkill_sensitive_diagnostic"]
            )

    def test_any_expert_omission_or_incomplete_health_fails_gate(self) -> None:
        corpus = SingletonHealthCorpus((_family(),), (), 1)

        def fake_run(bridge, request, adapter, **kwargs):
            return self._fake_rollout(
                adapter,
                request,
                omission=adapter.expert_id == "contra.deployed.fury.raid_a",
                incomplete=adapter.expert_id == "cat.fury.profile1",
            )

        with patch(
            "o2o_dps.fury_singleton_health_sensitivity_v1.run_fury_expert_closed_loop",
            side_effect=fake_run,
        ):
            result = evaluate_singleton_health_sensitivity(
                object(),
                corpus,
                _base_request(),
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                armor_hypothesis=1721,
                level_hypothesis=60,
                validation_seeds=(1,),
                watchdog_cap_ms=5000,
            )

        overall = result["overall"]
        self.assertFalse(overall["all_experts_zero_omissions_and_faithful"])
        self.assertFalse(overall["all_experts_all_health_horizons_complete"])
        self.assertFalse(result["health_sensitivity_gate_passed"])

    def test_artifact_retains_only_compact_inferred_lineage(self) -> None:
        family = _family()
        corpus = SingletonHealthCorpus((family,), (), 1)
        snapshot = HealthFeatureSnapshot(
            Path("manifest.json"),
            "snapshot",
            Path("."),
            {"COMPLETED": 50},
            (),
            {100: family.health_profile},
            Path("base.json"),
            True,
        )
        split = HealthInstanceSplit(1, ("train",), ("heldout",), ("train",))
        artifact = build_artifact(
            snapshot=snapshot,
            split=split,
            corpus=corpus,
            candidate_document={"kind": "candidate"},
            candidate_path=Path("candidate.json"),
            candidate_parameters=FuryPolicyParameters(use_death_wish=False),
            base_request_path=Path("base.json"),
            armor_hypothesis=1721,
            level_hypothesis=60,
            selection_seed=2,
            max_families_per_instance=8,
            evaluation={
                "status": "NOT_RUN",
                "health_sensitivity_gate_passed": False,
            },
        )

        self.assertFalse(artifact["deployment_allowed"])
        self.assertFalse(artifact["duration_heldout_gate_modified"])
        self.assertEqual(
            artifact["input_contract"]["scenario_catalog_gzip_read_count"], 0
        )
        self.assertTrue(artifact["health_claim_boundary"]["not_exact_npc_health"])
        self.assertFalse(
            artifact["health_claim_boundary"]["individual_multi_target_death_modeled"]
        )
        self.assertEqual(
            artifact["health_claim_boundary"]["primary_comparison_metric"],
            "paired TTK",
        )
        self.assertNotIn("request", artifact["selection"]["families"][0])


if __name__ == "__main__":
    unittest.main()
