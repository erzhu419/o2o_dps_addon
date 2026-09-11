from __future__ import annotations

from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_offline_corpus_v2 import (
    FuryOfflineCorpusError,
    compile_fury_offline_corpus_v2,
    main,
    verify_content_address,
    write_content_addressed_manifest,
)


def scenario(
    scenario_id: str,
    instance: str,
    *,
    span_ms: int,
    variant: str,
    layout_side: str,
    target_count: int,
    seed: str = "7",
) -> dict[str, object]:
    spatial_status = "RECONSTRUCTED" if variant == "single_target" else "INFERRED"
    return {
        "scenario_id": scenario_id,
        "source": {
            "instance": instance,
            "encounter": f"enc-{instance}",
            "wave_id": f"enc-{instance}:wave:1",
            "wave_ordinal": 1,
            "wave_first_anchor": {"event_index": 1, "offset_ms": 10},
            "wave_last_anchor": {"event_index": 2, "offset_ms": span_ms + 10},
            "target_anchors": [],
        },
        "pile": {
            "layout_variant": variant,
            "sensitivity_family": f"family-{instance}",
            "layout_side": layout_side,
            "pile_ordinal": 1,
            "target_guids": [f"target-{index}" for index in range(target_count)],
            "target_count": target_count,
            "spatial_assumption": {
                "value": "single_target" if target_count == 1 else "stacked",
                "status": spatial_status,
                "coordinates": None,
                "not_observed": True,
            },
        },
        "duration": {
            "seconds": span_ms / 1000,
            "observed_span_ms": span_ms,
            "status": "RECONSTRUCTED",
        },
        "target_hypotheses": {
            "armor": {
                "value": 1721,
                "status": "INFERRED",
                "not_identified_by_chronicle": True,
            },
            "level": {"value": 60, "status": "INFERRED"},
        },
        "kill_budget_proxies": [
            {
                "target_guid": "target-0",
                "value": 52_000 if target_count > 1 else 20_000,
                "status": "OBSERVED",
                "role": "sampling_weight_metadata_only",
                "not_exact_health": True,
            }
        ],
        "weight": {"value": None, "status": "MISSING"},
        "request": {
            "raid": {"parties": []},
            "encounter": {"duration": span_ms / 1000, "useHealth": False},
            "simOptions": {"iterations": 1, "randomSeed": seed},
        },
    }


class Fixture:
    def __init__(self, root: Path, scenarios: list[dict[str, object]]) -> None:
        self.root = root
        self.catalog = root / "catalogs" / "fixture.catalog.json.gz"
        self.catalog.parent.mkdir(parents=True)
        document = {
            "schema_version": 1,
            "kind": "fury_encounter_scenario_catalog_v1",
            "summary": {
                "scenario_count": len(scenarios),
                "encounter_count": len(scenarios),
                "wave_count": len(scenarios),
                "variant_counts": {},
            },
            "scenarios": scenarios,
        }
        with gzip.open(self.catalog, "wt", encoding="utf-8") as handle:
            json.dump(document, handle)
        self.batch_manifest = root / "manifest.json"
        batch = {
            "schema_version": 1,
            "kind": "chronicle_encounter_batch_manifest_v1",
            "summary": {
                "completed_file_count": 1,
                "catalog_totals": {"scenario_count": len(scenarios)},
            },
            "entries": [
                {
                    "processing_status": "COMPLETED",
                    "outputs": {
                        "scenario_catalog": {
                            "path": "catalogs/fixture.catalog.json.gz",
                            "size_bytes": self.catalog.stat().st_size,
                        }
                    },
                    "catalog_summary": {"scenario_count": len(scenarios)},
                }
            ],
        }
        self.batch_manifest.write_text(json.dumps(batch), encoding="utf-8")
        self.links = root / "links.json"
        unique_instances = sorted({str(item["source"]["instance"]) for item in scenarios})
        sidecar = {
            "schema_version": 2,
            "kind": "fury_offline_leakage_links_v2",
            "links": [
                {
                    "instance_ref": instance,
                    "player_guid": f"player-{instance}",
                    "guild_id": "shared-guild",
                }
                for instance in unique_instances
            ],
        }
        self.links.write_text(json.dumps(sidecar), encoding="utf-8")


class FuryOfflineCorpusV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_buckets_are_disjoint_and_content_address_is_deterministic(self) -> None:
        rows = [
            scenario(
                "main-single", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
            scenario(
                "main-cohit", "instance-a", span_ms=6_000,
                variant="cohit_stacked", layout_side="evidence_bounded", target_count=3,
            ),
            scenario(
                "sensitivity", "instance-b", span_ms=7_000,
                variant="stacked_upper", layout_side="sensitivity_upper", target_count=4,
            ),
            scenario(
                "short", "instance-b", span_ms=4_999,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
        ]
        fixture = Fixture(self.root, rows)
        first = compile_fury_offline_corpus_v2(
            fixture.batch_manifest,
            link_source_path=fixture.links,
            source_cohort="future_post_freeze",
        )
        second = compile_fury_offline_corpus_v2(
            fixture.batch_manifest,
            link_source_path=fixture.links,
            source_cohort="future_post_freeze",
        )
        self.assertEqual(first, second)
        self.assertTrue(verify_content_address(first))
        self.assertEqual(64, len(first["content_address"]["sha256"]))
        summary = first["summary"]["buckets"]
        self.assertEqual(2, summary["main_comparison"]["scenario_count"])
        self.assertEqual(1, summary["unknown_layout_sensitivity"]["scenario_count"])
        self.assertEqual(1, summary["short_auxiliary"]["scenario_count"])
        hashes = first["scenarios"][0]["hashes"]
        self.assertTrue(all(len(value) == 64 for value in hashes.values()))
        self.assertFalse(first["claim_boundary"]["final_holdout_claim"])
        field_summary = first["summary"]["field_availability"]
        self.assertEqual("UNKNOWN", field_summary["target_max_health_threshold"])
        self.assertFalse(field_summary["kill_budget_proxy_is_usable_as_unit_health_max"])
        self.assertIn(
            "wow_unit_classification_missing",
            field_summary["environment_contract_blockers"],
        )
        cohit = next(item for item in first["scenarios"] if item["scenario_id"] == "main-cohit")
        self.assertEqual(52_000, cohit["kill_budget_proxies"][0]["value"])
        threshold = cohit["provenance_guard"]["observed_kill_budget_proxy"]["thresholds"]["51000"]
        self.assertTrue(threshold["observed_kill_budget_at_or_above"])
        self.assertFalse(threshold["identifies_target_max_health_at_or_above"])

    def test_current_local_50_cannot_be_relabelled_as_validation(self) -> None:
        fixture = Fixture(
            self.root,
            [scenario(
                "main", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            )],
        )
        with self.assertRaisesRegex(FuryOfflineCorpusError, "development-only"):
            compile_fury_offline_corpus_v2(
                fixture.batch_manifest,
                link_source_path=fixture.links,
                corpus_role="selection_validation",
                source_cohort="current_local_50",
            )

    def test_validation_fails_closed_when_any_instance_has_no_link(self) -> None:
        rows = [
            scenario(
                "a", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
            scenario(
                "b", "instance-b", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
        ]
        fixture = Fixture(self.root, rows)
        fixture.links.write_text(
            json.dumps({
                "kind": "fury_offline_leakage_links_v2",
                "links": [{"instance_ref": "instance-a", "player_guid": "player-a"}],
            }),
            encoding="utf-8",
        )
        development = compile_fury_offline_corpus_v2(
            fixture.batch_manifest,
            link_source_path=fixture.links,
            source_cohort="future_post_freeze",
        )
        self.assertEqual(1, development["leakage_components"]["unresolved_instance_count"])
        unresolved = [
            entry for entry in development["scenarios"]
            if entry["source"]["instance"] == "instance-b"
        ]
        self.assertEqual(
            "unresolved_conservative_shared",
            unresolved[0]["leakage_component"]["link_status"],
        )
        with self.assertRaisesRegex(FuryOfflineCorpusError, "without player/guild"):
            compile_fury_offline_corpus_v2(
                fixture.batch_manifest,
                link_source_path=fixture.links,
                corpus_role="final_confirmation",
                source_cohort="future_post_freeze",
            )

    def test_export_queue_discovery_provenance_is_not_a_player_link(self) -> None:
        rows = [
            scenario(
                "a", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
            scenario(
                "b", "instance-b", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            ),
        ]
        fixture = Fixture(self.root, rows)
        queue = {
            "kind": "chronicle_manual_export_queue",
            "entries": [
                {
                    "instance_id": "instance-a",
                    "slug": "slug-a",
                    "guild": None,
                    "leaderboard_rows": [{"character": "A", "realm": "Realm"}],
                    "discovered_from": [{"character": "Same", "realm": "Realm"}],
                },
                {
                    "instance_id": "instance-b",
                    "slug": "slug-b",
                    "guild": None,
                    "leaderboard_rows": [{"character": "B", "realm": "Realm"}],
                    "discovered_from": [{"character": "Same", "realm": "Realm"}],
                },
            ],
        }
        fixture.links.write_text(json.dumps(queue), encoding="utf-8")
        manifest = compile_fury_offline_corpus_v2(
            fixture.batch_manifest,
            link_source_path=fixture.links,
            source_cohort="future_post_freeze",
        )
        self.assertEqual(2, manifest["leakage_components"]["component_count"])
        self.assertEqual(0, manifest["leakage_components"]["unresolved_instance_count"])

    def test_hashes_change_when_request_changes_without_fabricating_provenance(self) -> None:
        original = scenario(
            "main", "instance-a", span_ms=5_000,
            variant="single_target", layout_side="evidence_bounded", target_count=1,
        )
        fixture_a = Fixture(self.root / "a", [original])
        first = compile_fury_offline_corpus_v2(
            fixture_a.batch_manifest,
            link_source_path=fixture_a.links,
            source_cohort="future_post_freeze",
        )
        changed = deepcopy(original)
        changed["request"]["simOptions"]["randomSeed"] = "8"
        fixture_b = Fixture(self.root / "b", [changed])
        second = compile_fury_offline_corpus_v2(
            fixture_b.batch_manifest,
            link_source_path=fixture_b.links,
            source_cohort="future_post_freeze",
        )
        self.assertNotEqual(
            first["scenarios"][0]["hashes"]["request_sha256"],
            second["scenarios"][0]["hashes"]["request_sha256"],
        )
        guard = first["scenarios"][0]["provenance_guard"]
        self.assertEqual("MISSING", guard["target_max_health"]["status"])
        self.assertEqual("MISSING", guard["unit_classification"]["status"])
        self.assertIsNone(guard["positions"]["coordinates"])

    def test_catalog_count_mismatch_is_rejected(self) -> None:
        fixture = Fixture(
            self.root,
            [scenario(
                "main", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            )],
        )
        batch = json.loads(fixture.batch_manifest.read_text(encoding="utf-8"))
        batch["summary"]["catalog_totals"]["scenario_count"] = 2
        fixture.batch_manifest.write_text(json.dumps(batch), encoding="utf-8")
        with self.assertRaisesRegex(FuryOfflineCorpusError, "total scenario count"):
            compile_fury_offline_corpus_v2(
                fixture.batch_manifest,
                link_source_path=fixture.links,
                source_cohort="future_post_freeze",
            )

    def test_content_addressed_writer_and_plan_only_cli(self) -> None:
        fixture = Fixture(
            self.root,
            [scenario(
                "main", "instance-a", span_ms=5_000,
                variant="single_target", layout_side="evidence_bounded", target_count=1,
            )],
        )
        manifest = compile_fury_offline_corpus_v2(
            fixture.batch_manifest,
            link_source_path=fixture.links,
            source_cohort="future_post_freeze",
        )
        output = write_content_addressed_manifest(manifest, self.root / "output")
        self.assertIn(manifest["content_address"]["sha256"], output.name)
        self.assertEqual(".gz", output.suffix)
        with gzip.open(output, "rt", encoding="utf-8") as handle:
            self.assertEqual(manifest, json.load(handle))

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = main([
                "--batch-manifest", str(fixture.batch_manifest),
                "--link-source", str(fixture.links),
                "--source-cohort", "future_post_freeze",
                "--plan-only",
            ])
        plan = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertFalse(plan["execution_started"])
        self.assertFalse(plan["final_holdout_claim"])
        self.assertFalse((self.root / "output").joinpath("unexpected").exists())


if __name__ == "__main__":
    unittest.main()
