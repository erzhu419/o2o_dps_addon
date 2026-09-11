from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_offline_corpus_v2 import sha256_json
from o2o_dps.fury_offline_scenario_capsule_v2 import (
    ARMOR_DEBUFF_REGISTRY,
    FuryOfflineScenarioCapsuleError,
    build_scenario_capsule_bundle_v2,
    validate_scenario_capsule_bundle_v2,
    verify_capsule_bundle_sources_v2,
    write_scenario_capsule_bundle_v2,
)
from o2o_dps.chronicle_encounter_reconstruction_v1 import ARMOR_AURA_NAMES


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _anchor(*, offset: int, event: int, kind: str, raw_ref: str) -> dict[str, object]:
    return {
        "encounter": "encounter-a",
        "event_index": event,
        "offset_ms": offset,
        "type": kind,
        "source_file": "activity.csv",
        "raw_file": raw_ref,
        "csv_line": event + 2,
        "export_row": event,
    }


def _write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(value, handle)


class CapsuleFixture:
    def __init__(self, root: Path, *, aura_name: str = "Sunder Armor") -> None:
        self.root = root
        self.raw_ref = "chronicle_raw/import-a/activity.csv"
        self.raw_path = root / "offline_data" / self.raw_ref
        self.raw_path.parent.mkdir(parents=True)
        self.raw_path.write_bytes(b"raw rows are deliberately not parsed\n")
        self.normalized_path = root / "offline_data" / "normalized" / "instance-a.jsonl"
        self.normalized_path.parent.mkdir(parents=True)
        self.normalized_path.write_bytes(b"not-json-on-purpose\n")
        provenance = {
            "schema_version": 1,
            "source_format": "chronicle_all_activity_csv",
            "instance": "instance-a",
            "source": {
                "absolute_path": str(self.raw_path),
                "filename": self.raw_path.name,
                "size_bytes": self.raw_path.stat().st_size,
            },
            "row_count": 17,
            "raw_copy": self.raw_ref,
            "normalized": "normalized/instance-a.jsonl",
        }
        self.provenance_path = self.raw_path.parent / "provenance.json"
        self.provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

        self.batch_dir = root / "offline_data" / "derived" / "batch" / "v1"
        self.catalog_path = self.batch_dir / "catalogs" / "instance.catalog.json.gz"
        self.feature_path = self.batch_dir / "features" / "instance.feature.json.gz"
        first = _anchor(offset=100, event=1, kind="DMG", raw_ref=self.raw_ref)
        activity_first = _anchor(offset=300, event=2, kind="DMG", raw_ref=self.raw_ref)
        aura_set = _anchor(offset=500, event=3, kind="AURA", raw_ref=self.raw_ref)
        aura_remove = _anchor(offset=6000, event=4, kind="AURA", raw_ref=self.raw_ref)
        activity_last = _anchor(offset=6000, event=5, kind="DEAD", raw_ref=self.raw_ref)
        last = _anchor(offset=6100, event=6, kind="CLASS", raw_ref=self.raw_ref)
        self.source_scenario = {
            "scenario_id": "scenario-a",
            "source": {
                "instance": "instance-a",
                "encounter": "encounter-a",
                "wave_id": "encounter-a:wave:1",
                "wave_ordinal": 1,
                "wave_first_anchor": first,
                "wave_last_anchor": last,
                "target_anchors": [
                    {
                        "target_guid": "target-guid-a",
                        "first_anchor": activity_first,
                        "last_anchor": activity_last,
                    }
                ],
            },
            "pile": {
                "layout_variant": "single_target",
                "sensitivity_family": "encounter-a:wave:1",
                "layout_side": "evidence_bounded",
                "pile_ordinal": 1,
                "target_guids": ["target-guid-a"],
                "target_count": 1,
                "spatial_assumption": {
                    "value": "single_target",
                    "status": "OBSERVED",
                    "basis": "one hostile target",
                    "coordinates": None,
                    "not_observed": True,
                },
            },
            "duration": {
                "seconds": 6,
                "observed_span_ms": 6000,
                "status": "RECONSTRUCTED",
                "not_exact_pull_duration": True,
            },
            "target_hypotheses": {
                "armor": {
                    "value": 1721,
                    "status": "INFERRED",
                    "role": "explicit_sensitivity_parameter",
                    "not_identified_by_chronicle": True,
                },
                "level": {
                    "value": 60,
                    "status": "INFERRED",
                    "not_identified_by_chronicle": True,
                },
            },
            "kill_budget_proxies": [],
            "weight": {"value": 120000, "status": "RECONSTRUCTED"},
            "request": {
                "raid": {"parties": []},
                "encounter": {
                    "duration": 6,
                    "useHealth": False,
                    "targets": [{"name": "target-a"}],
                },
                "simOptions": {"iterations": 1, "randomSeed": "7"},
            },
        }
        catalog = {
            "schema_version": 1,
            "kind": "fury_encounter_scenario_catalog_v1",
            "source": {
                "normalized_file": str(self.normalized_path),
                "raw_rows_copied": False,
            },
            "summary": {"scenario_count": 1},
            "scenarios": [self.source_scenario],
        }
        _write_gzip_json(self.catalog_path, catalog)
        feature_target = {
            "target_guid": "target-guid-a",
            "creature_entry_id": 59957,
            "target_name": "target-a",
            "classification": {
                "value": "Hostile Creature",
                "status": "OBSERVED",
                "evidence": "CLASS_HOSTILE_CREATURE",
                "anchor": last,
            },
            "activity_interval": {
                "first_offset_ms": 300,
                "last_offset_ms": 6000,
                "status": "OBSERVED",
                "first_anchor": activity_first,
                "last_anchor": activity_last,
            },
            "confirmed_single_hit_health_lower_bound": {
                "value": 5000,
                "status": "RECONSTRUCTED",
            },
            "kill_budget_proxy": {
                "value": 120000,
                "status": "OBSERVED",
                "not_equal_to": "exact maximum or initial health",
            },
            "observed_incoming_damage_sum": {
                "value": 120000,
                "event_count": 42,
                "status": "OBSERVED",
            },
            "observed_outgoing_damage_sum": {
                "value": 900,
                "event_count": 3,
                "status": "OBSERVED",
            },
            "observed_healing_received_sum": {
                "value": 0,
                "event_count": 0,
                "status": "OBSERVED",
            },
            "armor_debuff_evidence": {
                "status": "RECONSTRUCTED",
                "transitions": [
                    {
                        "aura": aura_name,
                        "operation": "set",
                        "stacks": 1,
                        "status": "OBSERVED",
                        "anchor": aura_set,
                    },
                    {
                        "aura": aura_name,
                        "operation": "remove",
                        "stacks": 0,
                        "status": "OBSERVED",
                        "anchor": aura_remove,
                    },
                ],
                "strata": [
                    {
                        "start_offset_ms": 500,
                        "end_offset_ms": 6000,
                        "observed_active_armor_auras": [
                            {"name": aura_name, "stacks": 1}
                        ],
                        "effective_armor": {"value": None, "status": "MISSING"},
                    }
                ],
                "effective_armor": {"value": None, "status": "MISSING"},
            },
        }
        feature = {
            "schema_version": 1,
            "kind": "chronicle_encounter_reconstruction_v1",
            "source": {
                "normalized_file": str(self.normalized_path),
                "complete_file_scanned": True,
                "row_level_copy_written": False,
            },
            "summary": {
                "encounter_count": 1,
                "source_rows_included": 17,
                "wave_count": 1,
                "target_observation_count": 1,
            },
            "encounters": [
                {
                    "instance": "instance-a",
                    "encounter": "encounter-a",
                    "first_anchor": first,
                    "last_anchor": last,
                    "waves": [
                        {
                            "wave_id": "encounter-a:wave:1",
                            "ordinal": 1,
                            "start_offset_ms": 100,
                            "end_offset_ms": 6100,
                            "duration_ms": 6000,
                            "targets": [feature_target],
                        }
                    ],
                }
            ],
            "npc_identity_aggregates": [
                {
                    "identity": {"creature_entry_id": 59957},
                    "health_scenario": {
                        "status": "INFERRED",
                        "center": 100000,
                        "range": [90000, 110000],
                        "method": "median and IQR of repeated observed kill-budget proxies",
                        "not_equal_to": "exact NPC health distribution",
                    },
                }
            ],
        }
        _write_gzip_json(self.feature_path, feature)
        self.batch_path = self.batch_dir / "manifest.json"
        batch = {
            "kind": "chronicle_encounter_batch_manifest_v1",
            "entries": [
                {
                    "source": {
                        "path": str(self.normalized_path),
                        "name": self.normalized_path.name,
                        "size_bytes": self.normalized_path.stat().st_size,
                        "mtime_ns": self.normalized_path.stat().st_mtime_ns,
                    },
                    "processing_status": "COMPLETED",
                    "outputs": {
                        "feature_report": {
                            "path": "features/instance.feature.json.gz",
                            "size_bytes": self.feature_path.stat().st_size,
                        },
                        "scenario_catalog": {
                            "path": "catalogs/instance.catalog.json.gz",
                            "size_bytes": self.catalog_path.stat().st_size,
                        },
                    },
                }
            ],
            "summary": {"completed_file_count": 1},
        }
        self.batch_path.write_text(json.dumps(batch), encoding="utf-8")
        catalog_relative = self.catalog_path.relative_to(root).as_posix()
        catalog_sha = _file_sha(self.catalog_path)
        entry = {
            "scenario_id": "scenario-a",
            "bucket": "main_comparison",
            "source": deepcopy(self.source_scenario["source"]),
            "pile": deepcopy(self.source_scenario["pile"]),
            "target_hypotheses": deepcopy(self.source_scenario["target_hypotheses"]),
            "kill_budget_proxies": [],
            "weight": deepcopy(self.source_scenario["weight"]),
            "observed_span_ms": 6000,
            "layout_variant": "single_target",
            "layout_side": "evidence_bounded",
            "leakage_component": {
                "component_id": "leakage-a",
                "link_status": "resolved",
            },
            "catalog_locator": {
                "path": catalog_relative,
                "catalog_sha256": catalog_sha,
                "scenario_index": 0,
            },
            "hashes": {
                "scenario_sha256": sha256_json(self.source_scenario),
                "source_sha256": sha256_json(self.source_scenario["source"]),
                "request_sha256": sha256_json(self.source_scenario["request"]),
                "pile_sha256": sha256_json(self.source_scenario["pile"]),
                "target_hypotheses_sha256": sha256_json(
                    self.source_scenario["target_hypotheses"]
                ),
                "kill_budget_proxies_sha256": sha256_json([]),
                "weight_sha256": sha256_json(self.source_scenario["weight"]),
            },
            "provenance_guard": {},
        }
        core = {
            "schema_version": 2,
            "schema": "fury_offline_corpus/v2",
            "kind": "fury_offline_corpus_manifest_v2",
            "corpus_role": "development",
            "inputs": {
                "batch_manifest": {
                    "path": self.batch_path.relative_to(root).as_posix(),
                    "sha256": _file_sha(self.batch_path),
                    "size_bytes": self.batch_path.stat().st_size,
                },
                "catalogs": [
                    {
                        "path": catalog_relative,
                        "sha256": catalog_sha,
                        "size_bytes": self.catalog_path.stat().st_size,
                        "scenario_count": 1,
                    }
                ],
            },
            "summary": {"buckets": {"main_comparison": {"scenario_count": 1}}},
            "scenarios": [entry],
        }
        core["content_address"] = {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        }
        self.manifest_path = root / "offline_data" / "corpus.json"
        self.manifest_path.write_text(json.dumps(core), encoding="utf-8")


class FuryOfflineScenarioCapsuleV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_portable_feature_backed_hypothesis_capsule(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        validate_scenario_capsule_bundle_v2(bundle)
        verify_capsule_bundle_sources_v2(bundle, project_root=self.root)
        self.assertFalse(bundle["historical_truth"])
        self.assertTrue(bundle["source_contract"]["raw_csv_or_normalized_rows_opened"])
        self.assertFalse(bundle["source_contract"]["raw_csv_or_normalized_rows_parsed"])
        self.assertTrue(
            bundle["source_contract"][
                "raw_csv_and_normalized_bytes_content_hashed"
            ]
        )
        self.assertEqual(0, bundle["summary"]["raw_csv_or_normalized_byte_count_uploaded"])
        source = bundle["sources"][0]
        self.assertEqual(_file_sha(fixture.feature_path), source["reconstruction_feature"]["sha256"])
        self.assertEqual(
            _file_sha(fixture.raw_path), source["raw_csv_source"]["byte_sha256"]
        )
        self.assertEqual(
            _file_sha(fixture.normalized_path),
            source["normalized_source"]["byte_sha256"],
        )
        self.assertEqual(
            "COMPUTED_LOCALLY_BY_COMPACT_COMPILER",
            source["raw_csv_source"]["byte_hash_status"],
        )
        provenance = bundle["source_instance_provenance"]
        self.assertEqual(1, provenance["entry_count"])
        self.assertTrue(provenance["raw_and_normalized_byte_identity_claimed"])
        self.assertEqual(
            source["raw_csv_source"]["byte_sha256"],
            provenance["entries"][0]["raw_csv_byte_sha256"],
        )
        target = bundle["scenarios"][0]["targets"][0]
        self.assertEqual(200, target["observed_hostile_activity_proxy"]["start_ms"])
        self.assertEqual(5900, target["observed_hostile_activity_proxy"]["end_ms"])
        self.assertEqual(
            [
                "full_wave",
                "observed_hostile_activity_proxy",
                "tactical_delay_until_activity_proxy",
                "mechanic_gate_until_activity_proxy",
            ],
            [row["branch_id"] for row in target["attackable_window_hypothesis_family"]],
        )
        health = target["max_health_hypothesis_family"]
        branch_ids = {row["branch_id"] for row in health["shared_branch_family"]}
        self.assertIn("duration_only", branch_ids)
        self.assertIn("proxy_center", branch_ids)
        self.assertIn("repeat_iqr_high", branch_ids)
        self.assertFalse(health["historical_truth"])
        self.assertFalse(health["health_enabled_endogenous_ttk_execution_eligible"])
        self.assertEqual(
            "MISSING_REQUIRES_PLAYER_CONDITIONING",
            target["background_team_damage_model"]["status"],
        )
        self.assertEqual(
            120000,
            target["observed_combat_summaries"]["observed_incoming_damage_sum"][
                "value"
            ],
        )
        self.assertEqual(
            ["non_worldboss", "worldboss"],
            target["classification"]["simulator_hypothesis_family"],
        )
        transition = target["armor"]["observed_transitions"][0]
        self.assertEqual("sunder_armor", transition["debuff_id"])
        self.assertEqual("OBSERVED_AURA_TRANSITION", transition["status"])
        self.assertIsNone(transition["observed_spell_id"])
        self.assertEqual(
            "SIMULATOR_MECHANIC_HYPOTHESIS",
            bundle["model_registry"]["armor_debuffs"]["Sunder Armor"][
                "magnitude_hypotheses"
            ][0]["status"],
        )
        scenario = bundle["scenarios"][0]
        self.assertEqual(
            ["trash_pack", "boss_or_worldboss"],
            scenario["wave_role"]["simulator_hypothesis_family"],
        )
        self.assertFalse(
            scenario["execution_mode_eligibility"]["health_enabled_endogenous_ttk"]
        )

    def test_registry_covers_every_aura_the_feature_compiler_can_emit(self) -> None:
        self.assertTrue(
            ARMOR_AURA_NAMES.issubset(
                {name.casefold() for name in ARMOR_DEBUFF_REGISTRY}
            )
        )

    def test_unknown_armor_aura_fails_closed(self) -> None:
        fixture = CapsuleFixture(self.root, aura_name="Unregistered Armor Curse")
        with self.assertRaisesRegex(
            FuryOfflineScenarioCapsuleError, "unregistered armor-debuff aura"
        ):
            build_scenario_capsule_bundle_v2(
                fixture.manifest_path, project_root=self.root
            )

    def test_claim_widening_fails_even_with_resealed_content_address(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        widened = deepcopy(bundle)
        widened["historical_truth"] = True
        widened.pop("content_address")
        widened["content_address"] = {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(widened),
        }
        with self.assertRaisesRegex(FuryOfflineScenarioCapsuleError, "historical_truth"):
            validate_scenario_capsule_bundle_v2(widened)

    def test_resealed_extra_top_level_payload_is_rejected(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        attacked = deepcopy(bundle)
        attacked["raw_payload_injected"] = {"untrusted": True}
        attacked.pop("content_address")
        attacked["content_address"] = {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(attacked),
        }
        with self.assertRaisesRegex(
            FuryOfflineScenarioCapsuleError, "top-level fields mismatch"
        ):
            validate_scenario_capsule_bundle_v2(attacked)

    def test_resealed_handwritten_model_is_rejected_by_physical_rebuild(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        attacked = deepcopy(bundle)
        scenario = attacked["scenarios"][0]
        scenario["targets"][0]["observed_hostile_activity_proxy"]["start_ms"] += 1
        scenario_core = deepcopy(scenario)
        scenario_core.pop("capsule_sha256")
        scenario["capsule_sha256"] = sha256_json(scenario_core)
        attacked_core = deepcopy(attacked)
        attacked_core.pop("content_address")
        attacked["content_address"]["sha256"] = sha256_json(attacked_core)
        validate_scenario_capsule_bundle_v2(attacked)
        with self.assertRaisesRegex(
            FuryOfflineScenarioCapsuleError,
            "differs from a complete rebuild",
        ):
            verify_capsule_bundle_sources_v2(attacked, project_root=self.root)

    def test_sealed_feature_tampering_is_detected_without_reading_raw_rows(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        fixture.feature_path.write_bytes(fixture.feature_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(FuryOfflineScenarioCapsuleError, "byte size mismatch"):
            verify_capsule_bundle_sources_v2(bundle, project_root=self.root)

    def test_same_size_raw_or_normalized_tampering_is_detected(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        for path, expected_label in (
            (fixture.raw_path, "raw_csv_source"),
            (fixture.normalized_path, "normalized_source"),
        ):
            original = path.read_bytes()
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with self.assertRaisesRegex(
                FuryOfflineScenarioCapsuleError,
                rf"sealed {expected_label} SHA-256 mismatch",
            ):
                verify_capsule_bundle_sources_v2(bundle, project_root=self.root)
            path.write_bytes(original)

    def test_deterministic_gzip_output(self) -> None:
        fixture = CapsuleFixture(self.root)
        bundle = build_scenario_capsule_bundle_v2(
            fixture.manifest_path, project_root=self.root
        )
        first = write_scenario_capsule_bundle_v2(
            bundle, self.root / "out-a", project_root=self.root
        )
        second = write_scenario_capsule_bundle_v2(
            bundle, self.root / "out-b", project_root=self.root
        )
        self.assertEqual(first.name, second.name)
        self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
