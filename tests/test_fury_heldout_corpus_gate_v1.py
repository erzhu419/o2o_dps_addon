from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_heldout_corpus_gate_v1 import (
    CompletedCatalog,
    FuryHeldoutCorpusError,
    InstanceSplit,
    ManifestSnapshot,
    SelectedCorpus,
    SelectedFamily,
    evaluate_heldout_candidate,
    extract_candidate_training_instances,
    load_candidate_parameters,
    load_manifest_snapshot,
    select_heldout_families,
    split_completed_instances,
)
from o2o_dps.fury_policy_optimization_v1 import FuryPolicyParameters, PolicyScenario


def _request(target_count: int, *, duration_ms: int = 10_000) -> dict[str, object]:
    return {
        "raid": {"parties": [{"players": [{}]}]},
        "encounter": {
            "duration": duration_ms / 1000.0,
            "targets": [
                {"level": 60, "name": f"target-{index}"}
                for index in range(target_count)
            ],
        },
        "simOptions": {"iterations": 1, "interactive": True},
    }


def _catalog_row(
    instance: str,
    family: str,
    target_count: int,
    duration_ms: int,
    *,
    side: str = "evidence_bounded",
    armor: int = 1721,
    level: int = 60,
) -> dict[str, object]:
    return {
        "scenario_id": f"{instance}__encounter__{family}__armor-{armor}__level-{level}",
        "source": {"instance": instance, "wave_id": family},
        "pile": {
            "layout_side": side,
            "sensitivity_family": family,
            "target_count": target_count,
        },
        "duration": {"observed_span_ms": duration_ms},
        "target_hypotheses": {
            "armor": {"value": armor},
            "level": {"value": level},
        },
        "request": _request(target_count, duration_ms=duration_ms),
    }


def _write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(value, handle)


class ManifestAndSplitTests(unittest.TestCase):
    def test_completed_only_snapshot_and_require_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalogs" / "inst-a.catalog.json.gz"
            _write_gzip_json(
                catalog_path,
                {"kind": "fury_encounter_scenario_catalog_v1", "scenarios": []},
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "kind": "chronicle_encounter_batch_manifest_v1",
                        "updated_at": "snapshot",
                        "output": {"directory": str(root)},
                        "entries": [
                            {
                                "source": {"name": "inst-a__stamp.jsonl"},
                                "processing_status": "COMPLETED",
                                "outputs": {
                                    "scenario_catalog": {
                                        "path": "catalogs/inst-a.catalog.json.gz"
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
            snapshot = load_manifest_snapshot(manifest_path)
            self.assertEqual(snapshot.completed_instance_ids, ("inst-a",))
            self.assertEqual(snapshot.status_counts, {"COMPLETED": 1, "PENDING": 1})
            with self.assertRaisesRegex(FuryHeldoutCorpusError, "require-complete"):
                load_manifest_snapshot(manifest_path, require_complete=True)

    def test_instance_split_is_reproducible_and_forces_training_instance_out(self) -> None:
        catalogs = tuple(
            CompletedCatalog(
                instance_id=value,
                source_name=f"{value}__stamp.jsonl",
                relative_path=f"catalogs/{value}.json.gz",
                path=Path(f"{value}.json.gz"),
            )
            for value in ("a", "b", "c", "d", "e")
        )
        snapshot = ManifestSnapshot(
            manifest_path=Path("manifest.json"),
            manifest_updated_at=None,
            output_directory=Path("."),
            status_counts={"COMPLETED": 5},
            completed_catalogs=catalogs,
            require_complete=False,
        )
        first = split_completed_instances(
            snapshot,
            split_seed=17,
            candidate_training_instances=("a",),
            heldout_count=3,
        )
        second = split_completed_instances(
            snapshot,
            split_seed=17,
            candidate_training_instances=("a",),
            heldout_count=3,
        )
        self.assertEqual(first, second)
        self.assertNotIn("a", first.heldout_instances)
        self.assertIn("a", first.non_heldout_completed_instances)
        self.assertTrue(
            set(first.heldout_instances).isdisjoint(first.candidate_training_instances)
        )


class CandidateAndSelectionTests(unittest.TestCase):
    def test_loads_robust_selected_parameters_and_extracts_instance_lineage(self) -> None:
        document = {
            "kind": "fury_policy_robust_optimization_v1",
            "selected_parameters": {
                "heroic_strike_threshold": 50,
                "cleave_threshold": 55,
                "use_death_wish": False,
            },
            "training_branches": [
                {
                    "scenario_ids": [
                        "043b4d65-9c58-4643-b601-62f1684af04a__enc__wave"
                    ]
                }
            ],
        }
        parameters = load_candidate_parameters(document)
        self.assertEqual(parameters.heroic_strike_threshold, 50)
        self.assertEqual(
            extract_candidate_training_instances(document),
            ("043b4d65-9c58-4643-b601-62f1684af04a",),
        )

    def test_selection_is_bounded_diverse_and_evidence_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = "heldout-a"
            rows = []
            index = 0
            for target_count in (1, 2, 3, 6):
                for duration in (8_000, 20_000, 45_000):
                    index += 1
                    rows.append(
                        _catalog_row(
                            instance,
                            f"family-{index}",
                            target_count,
                            duration,
                        )
                    )
            rows.append(
                _catalog_row(instance, "upper", 8, 12_000, side="upper")
            )
            rows.append(
                _catalog_row(instance, "other-armor", 2, 12_000, armor=9999)
            )
            catalog_path = root / "catalog.json.gz"
            _write_gzip_json(
                catalog_path,
                {"kind": "fury_encounter_scenario_catalog_v1", "scenarios": rows},
            )
            snapshot = ManifestSnapshot(
                manifest_path=root / "manifest.json",
                manifest_updated_at="snapshot",
                output_directory=root,
                status_counts={"COMPLETED": 1},
                completed_catalogs=(
                    CompletedCatalog(
                        instance,
                        f"{instance}__stamp.jsonl",
                        "catalog.json.gz",
                        catalog_path,
                    ),
                ),
                require_complete=False,
            )
            split = InstanceSplit(1, ("training",), (instance,), ("training",))
            selected = select_heldout_families(
                snapshot,
                split,
                armor_hypothesis=1721,
                level_hypothesis=60,
                selection_seed=19,
                max_families_per_instance=8,
            )
            self.assertEqual(len(selected.families), 8)
            self.assertEqual(
                {value.target_count_stratum for value in selected.families},
                {"1", "2", "3-4", "5+"},
            )
            self.assertEqual(
                {value.duration_stratum for value in selected.families},
                {"short_le_10s", "medium_10_30s", "long_gt_30s"},
            )
            self.assertTrue(
                all(
                    value.scenario.provenance["layout_side"] == "evidence_bounded"
                    for value in selected.families
                )
            )
            self.assertTrue(all(value.scenario.weight > 0 for value in selected.families))


def _family(name: str, target_count: int, *, weight: float = 1.0) -> SelectedFamily:
    scenario = PolicyScenario(
        scenario_id=name,
        request=_request(target_count),
        horizon_ms=10_000,
        weight=weight,
        provenance={"instance_id": "heldout"},
    )
    return SelectedFamily(
        scenario=scenario,
        instance_id="heldout",
        family_id=name,
        catalog_relative_path="catalog.json.gz",
        target_count=target_count,
        target_count_stratum="1" if target_count == 1 else "2",
        duration_stratum="short_le_10s",
    )


class EvaluationGateTests(unittest.TestCase):
    @staticmethod
    def _rollout(
        dps: float,
        horizon_ms: int,
        *,
        omissions: int = 0,
        horizon_complete: bool = True,
    ):
        return {
            "damage_delta": dps * horizon_ms / 1000.0,
            "dps": dps,
            "decision_count": 1,
            "configured_horizon_complete": horizon_complete,
            "all_lane_projections_faithful": omissions == 0,
            "omitted_lane_count": omissions,
            "omitted_lane_counts": {"gcd": omissions} if omissions else {},
            "nonfaithful_reason_counts": (
                {"gcd:action_not_legal": omissions} if omissions else {}
            ),
            "source_execution": False,
            "exact_lua_replay": False,
        }

    def test_each_represented_target_count_stratum_must_pass(self) -> None:
        corpus = SelectedCorpus(
            families=(
                _family("single-a", 1),
                _family("single-b", 1),
                _family("double", 2),
            ),
            instance_summaries=(),
        )

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            count = len(request["encounter"]["targets"])
            if adapter.expert_id == "cat.fury.profile1":
                dps = 100.0
            elif adapter.expert_id == "contra.deployed.fury.raid_a":
                dps = 90.0
            else:
                dps = 120.0 if count == 1 else 99.0
            return self._rollout(dps, horizon_ms)

        with patch(
            "o2o_dps.fury_heldout_corpus_gate_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            result = evaluate_heldout_candidate(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                validation_seeds=(1, 2),
            )
        self.assertTrue(result["overall"]["gate_passed"])
        self.assertTrue(result["target_count_strata"]["1"]["gate_passed"])
        self.assertFalse(result["target_count_strata"]["2"]["gate_passed"])
        self.assertFalse(result["heldout_gate_passed"])
        self.assertNotIn("rollouts", result)

    def test_candidate_omission_fails_gate_even_when_dps_wins(self) -> None:
        corpus = SelectedCorpus((_family("single", 1),), ())

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            if adapter.expert_id == "cat.fury.profile1":
                return self._rollout(100.0, horizon_ms)
            if adapter.expert_id == "contra.deployed.fury.raid_a":
                return self._rollout(90.0, horizon_ms)
            return self._rollout(130.0, horizon_ms, omissions=1)

        with patch(
            "o2o_dps.fury_heldout_corpus_gate_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            result = evaluate_heldout_candidate(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                validation_seeds=(1, 2),
            )
        self.assertFalse(result["overall"]["candidate_zero_omissions"])
        self.assertFalse(result["heldout_gate_passed"])

    def test_baseline_omission_fails_gate_even_when_candidate_wins(self) -> None:
        corpus = SelectedCorpus((_family("single", 1),), ())

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            if adapter.expert_id == "cat.fury.profile1":
                return self._rollout(100.0, horizon_ms)
            if adapter.expert_id == "contra.deployed.fury.raid_a":
                return self._rollout(90.0, horizon_ms, omissions=1)
            return self._rollout(130.0, horizon_ms)

        with patch(
            "o2o_dps.fury_heldout_corpus_gate_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            result = evaluate_heldout_candidate(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                validation_seeds=(1, 2),
            )

        overall = result["overall"]
        self.assertTrue(overall["candidate_zero_omissions"])
        self.assertFalse(overall["all_experts_zero_omissions"])
        self.assertFalse(
            overall["zero_omissions_by_expert"][
                "contra.deployed.fury.raid_a"
            ]
        )
        self.assertFalse(result["heldout_gate_passed"])

    def test_baseline_incomplete_horizon_fails_gate(self) -> None:
        corpus = SelectedCorpus((_family("single", 1),), ())

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            if adapter.expert_id == "cat.fury.profile1":
                return self._rollout(100.0, horizon_ms)
            if adapter.expert_id == "contra.deployed.fury.raid_a":
                return self._rollout(
                    90.0,
                    horizon_ms,
                    horizon_complete=False,
                )
            return self._rollout(130.0, horizon_ms)

        with patch(
            "o2o_dps.fury_heldout_corpus_gate_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            result = evaluate_heldout_candidate(
                object(),
                corpus,
                candidate_parameters=FuryPolicyParameters(use_death_wish=False),
                validation_seeds=(1, 2),
            )

        overall = result["overall"]
        self.assertTrue(overall["candidate_all_horizons_complete"])
        self.assertFalse(overall["all_experts_all_horizons_complete"])
        self.assertFalse(
            overall["complete_horizons_by_expert"][
                "contra.deployed.fury.raid_a"
            ]
        )
        self.assertFalse(result["heldout_gate_passed"])


if __name__ == "__main__":
    unittest.main()
