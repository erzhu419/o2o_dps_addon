from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_offline_corpus_v2 import sha256_json
from o2o_dps.fury_offline_runner_inputs_v2 import (
    FuryOfflineRunnerInputsError,
    main,
    materialize_runner_inputs_v2,
)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_scenario(
    scenario_id: str,
    *,
    family: str = "family-a",
    horizon_ms: int = 6_000,
    variant: str = "single_target",
    layout_side: str = "evidence_bounded",
    target_count: int = 1,
    armor: int = 1721,
) -> dict[str, object]:
    targets = [{"name": f"target-{index}"} for index in range(target_count)]
    return {
        "scenario_id": scenario_id,
        "source": {
            "instance": "instance-a",
            "encounter": "encounter-a",
            "wave_id": family,
        },
        "pile": {
            "layout_variant": variant,
            "layout_side": layout_side,
            "sensitivity_family": family,
            "target_count": target_count,
            "target_guids": [f"guid-{index}" for index in range(target_count)],
            "spatial_assumption": {"coordinates": None},
        },
        "duration": {
            "observed_span_ms": horizon_ms,
            "seconds": horizon_ms / 1000,
            "status": "RECONSTRUCTED",
        },
        "target_hypotheses": {
            "armor": {"value": armor, "status": "INFERRED"},
            "level": {"value": 60, "status": "INFERRED"},
        },
        "kill_budget_proxies": [],
        "weight": {"value": None, "status": "MISSING"},
        "request": {
            "raid": {"parties": []},
            "encounter": {
                "duration": horizon_ms / 1000,
                "useHealth": False,
                "targets": targets,
            },
            "simOptions": {"iterations": 1, "randomSeed": "7"},
        },
    }


def _corpus_entry(
    source: dict[str, object],
    *,
    catalog_path: str,
    catalog_sha: str,
    index: int,
    bucket: str,
) -> dict[str, object]:
    pile = source["pile"]
    return {
        "scenario_id": source["scenario_id"],
        "bucket": bucket,
        "source": deepcopy(source["source"]),
        "pile": deepcopy(pile),
        "target_hypotheses": deepcopy(source["target_hypotheses"]),
        "kill_budget_proxies": deepcopy(source["kill_budget_proxies"]),
        "weight": deepcopy(source["weight"]),
        "observed_span_ms": source["duration"]["observed_span_ms"],
        "layout_variant": pile["layout_variant"],
        "layout_side": pile["layout_side"],
        "leakage_component": {
            "component_id": "leakage-component-a",
            "link_status": "resolved",
        },
        "catalog_locator": {
            "path": catalog_path,
            "catalog_sha256": catalog_sha,
            "scenario_index": index,
        },
        "hashes": {
            "scenario_sha256": sha256_json(source),
            "source_sha256": sha256_json(source["source"]),
            "request_sha256": sha256_json(source["request"]),
            "pile_sha256": sha256_json(source["pile"]),
            "target_hypotheses_sha256": sha256_json(source["target_hypotheses"]),
            "kill_budget_proxies_sha256": sha256_json(source["kill_budget_proxies"]),
            "weight_sha256": sha256_json(source["weight"]),
        },
        "provenance_guard": {},
    }


class CorpusFixture:
    def __init__(
        self,
        root: Path,
        scenarios: list[dict[str, object]],
        *,
        buckets: list[str],
    ) -> None:
        self.root = root
        self.catalog_path = root / "catalogs" / "fixture.catalog.json.gz"
        self.catalog_path.parent.mkdir(parents=True)
        catalog = {
            "kind": "fury_encounter_scenario_catalog_v1",
            "summary": {"scenario_count": len(scenarios)},
            "scenarios": scenarios,
        }
        with gzip.open(self.catalog_path, "wt", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        relative = self.catalog_path.relative_to(root).as_posix()
        catalog_sha = _file_sha(self.catalog_path)
        entries = [
            _corpus_entry(
                scenario,
                catalog_path=relative,
                catalog_sha=catalog_sha,
                index=index,
                bucket=buckets[index],
            )
            for index, scenario in enumerate(scenarios)
        ]
        counts = {bucket: buckets.count(bucket) for bucket in set(buckets)}
        core = {
            "schema_version": 2,
            "schema": "fury_offline_corpus/v2",
            "kind": "fury_offline_corpus_manifest_v2",
            "inputs": {
                "catalogs": [
                    {
                        "path": relative,
                        "size_bytes": self.catalog_path.stat().st_size,
                        "sha256": catalog_sha,
                        "scenario_count": len(scenarios),
                    }
                ]
            },
            "summary": {
                "buckets": {
                    bucket: {"scenario_count": count}
                    for bucket, count in counts.items()
                }
            },
            "scenarios": entries,
        }
        self.manifest_path = root / "corpus.json"
        self.write_core(core)

    def write_core(self, core: dict[str, object]) -> None:
        sealed = deepcopy(core)
        sealed.pop("content_address", None)
        sealed["content_address"] = {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(sealed),
        }
        self.manifest_path.write_text(json.dumps(sealed), encoding="utf-8")

    def manifest(self) -> dict[str, object]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))


class FuryOfflineRunnerInputsV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_main_materializes_exact_requests_and_equal_family_weight(self) -> None:
        scenarios = [
            _source_scenario("single-armor-a", armor=1721),
            _source_scenario("single-armor-b", armor=2500),
            _source_scenario(
                "cohit",
                family="family-b",
                variant="cohit_stacked",
                target_count=3,
            ),
        ]
        fixture = CorpusFixture(
            self.root, scenarios, buckets=["main_comparison"] * len(scenarios)
        )
        result = materialize_runner_inputs_v2(
            fixture.manifest_path, project_root=self.root
        )
        self.assertEqual(3, result["selection"]["scenario_count"])
        self.assertEqual(2, result["selection"]["family_count"])
        self.assertEqual(
            {"multi_target": 1, "single_target": 2},
            result["selection"]["stratum_counts"],
        )
        by_id = {row["scenario_id"]: row for row in result["scenarios"]}
        self.assertEqual(0.5, by_id["single-armor-a"]["scenario_weight"])
        self.assertEqual(0.5, by_id["single-armor-b"]["scenario_weight"])
        self.assertEqual(1.0, by_id["cohit"]["scenario_weight"])
        self.assertEqual(18_000, by_id["cohit"]["estimated_cost_units"])
        self.assertEqual(scenarios[0]["request"], by_id["single-armor-a"]["request"])
        self.assertTrue(
            all(
                row["scenario_model"]["model_status"]
                == "OFFLINE_CATALOG_DYNAMIC_SEMANTICS_UNBOUND"
                for row in result["scenarios"]
            )
        )
        self.assertTrue(
            all(
                row["scenario_model"]["comparison_eligible"] is False
                and row["target_context_bundle"]["bridge_execution_eligible"]
                is False
                for row in result["scenarios"]
            )
        )
        self.assertEqual(
            result["selection"]["comparison_eligible_scenario_count"], 0
        )
        self.assertFalse(result["selection"]["dynamic_armor_schedules_bound"])
        self.assertFalse(result["selection"]["endogenous_ttk_bound"])
        self.assertRegex(result["scenario_model_bundle_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            result["target_context_bundle_set_sha256"], r"^[0-9a-f]{64}$"
        )
        for field in (
            "corpus_entry_sha256",
            "source_scenario_sha256",
            "catalog_sha256",
            "request_sha256",
        ):
            self.assertRegex(by_id["cohit"][field], r"^[0-9a-f]{64}$")

    def test_tampered_catalog_bytes_are_rejected(self) -> None:
        scenario = _source_scenario("single")
        fixture = CorpusFixture(self.root, [scenario], buckets=["main_comparison"])
        fixture.catalog_path.write_bytes(fixture.catalog_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "byte size mismatch"):
            materialize_runner_inputs_v2(fixture.manifest_path, project_root=self.root)

    def test_locator_index_and_scenario_id_are_reverified(self) -> None:
        scenarios = [_source_scenario("first"), _source_scenario("second")]
        fixture = CorpusFixture(
            self.root, scenarios, buckets=["main_comparison", "main_comparison"]
        )
        manifest = fixture.manifest()
        manifest["scenarios"][0]["catalog_locator"]["scenario_index"] = 1
        fixture.write_core(manifest)
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "scenario ID"):
            materialize_runner_inputs_v2(fixture.manifest_path, project_root=self.root)

    def test_source_scenario_and_request_hashes_are_reverified(self) -> None:
        scenario = _source_scenario("single")
        fixture = CorpusFixture(self.root, [scenario], buckets=["main_comparison"])
        manifest = fixture.manifest()
        manifest["scenarios"][0]["hashes"]["request_sha256"] = "0" * 64
        fixture.write_core(manifest)
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "request_sha256"):
            materialize_runner_inputs_v2(fixture.manifest_path, project_root=self.root)

    def test_main_comparison_rejects_unresolved_leakage_component(self) -> None:
        scenario = _source_scenario("single")
        fixture = CorpusFixture(self.root, [scenario], buckets=["main_comparison"])
        manifest = fixture.manifest()
        manifest["scenarios"][0]["leakage_component"][
            "link_status"
        ] = "unresolved_conservative_shared"
        fixture.write_core(manifest)
        with self.assertRaisesRegex(
            FuryOfflineRunnerInputsError, "resolved leakage-component"
        ):
            materialize_runner_inputs_v2(
                fixture.manifest_path, project_root=self.root
            )

    def test_main_rejects_short_or_unknown_layout_contamination(self) -> None:
        unknown = _source_scenario(
            "unknown",
            variant="stacked_upper",
            layout_side="upper",
            target_count=3,
        )
        fixture = CorpusFixture(self.root, [unknown], buckets=["main_comparison"])
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "contains unknown_layout"):
            materialize_runner_inputs_v2(fixture.manifest_path, project_root=self.root)

        short_root = self.root / "short"
        short = _source_scenario("short", horizon_ms=4_999)
        short_fixture = CorpusFixture(short_root, [short], buckets=["main_comparison"])
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "contains short_auxiliary"):
            materialize_runner_inputs_v2(
                short_fixture.manifest_path, project_root=short_root
            )

    def test_sensitivity_alternatives_share_one_total_family_weight(self) -> None:
        scenarios = [
            _source_scenario(
                "upper",
                family="unknown-family",
                variant="stacked_upper",
                layout_side="upper",
                target_count=4,
            ),
            _source_scenario(
                "lower-a",
                family="unknown-family",
                variant="separated_singleton",
                layout_side="lower",
                target_count=1,
            ),
            _source_scenario(
                "lower-b",
                family="unknown-family",
                variant="separated_component",
                layout_side="lower",
                target_count=2,
            ),
        ]
        fixture = CorpusFixture(
            self.root,
            scenarios,
            buckets=["unknown_layout_sensitivity"] * 3,
        )
        result = materialize_runner_inputs_v2(
            fixture.manifest_path,
            bucket="unknown_layout_sensitivity",
            project_root=self.root,
        )
        weights = [row["scenario_weight"] for row in result["scenarios"]]
        self.assertAlmostEqual(1.0, sum(weights), places=12)
        self.assertTrue(all(row["family_weight_contract"]["alternative_count"] == 3 for row in result["scenarios"]))

    def test_manifest_content_address_and_root_escape_fail_closed(self) -> None:
        scenario = _source_scenario("single")
        fixture = CorpusFixture(self.root, [scenario], buckets=["main_comparison"])
        manifest = fixture.manifest()
        manifest["scenarios"][0]["scenario_id"] = "changed-without-reseal"
        fixture.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "content address mismatch"):
            materialize_runner_inputs_v2(fixture.manifest_path, project_root=self.root)

        fixture = CorpusFixture(self.root / "escape", [scenario], buckets=["main_comparison"])
        manifest = fixture.manifest()
        manifest["inputs"]["catalogs"][0]["path"] = "../outside.json.gz"
        manifest["scenarios"][0]["catalog_locator"]["path"] = "../outside.json.gz"
        fixture.write_core(manifest)
        with self.assertRaisesRegex(FuryOfflineRunnerInputsError, "escapes the project root"):
            materialize_runner_inputs_v2(
                fixture.manifest_path, project_root=fixture.root
            )

    def test_plan_only_cli_never_executes_simulator(self) -> None:
        scenario = _source_scenario("single")
        fixture = CorpusFixture(self.root, [scenario], buckets=["main_comparison"])
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = main(
                [
                    "--manifest",
                    str(fixture.manifest_path),
                    "--project-root",
                    str(self.root),
                    "--plan-only",
                ]
            )
        plan = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertFalse(plan["simulator_execution_started"])
        self.assertEqual(1, plan["selection"]["scenario_count"])


if __name__ == "__main__":
    unittest.main()
