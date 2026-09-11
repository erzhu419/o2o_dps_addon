from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_capsule_execution_binding_v2 import (
    FuryCapsuleExecutionBindingV2Error,
    compile_capsule_static_execution_binding_v2,
    enumerate_capsule_scenarios_v2,
    extract_runner_scenarios_v2,
    load_capsule_bundle_v2,
    validate_capsule_static_execution_binding_v2,
    write_capsule_static_execution_binding_v2,
)
from o2o_dps.fury_offline_corpus_v2 import sha256_json
from o2o_dps.fury_offline_scenario_capsule_v2 import IMPLEMENTATION_REVISION
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    FuryPairedRunnerError,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _fixture_source() -> dict:
    catalog = {
        "path": "offline_data/catalog.json.gz",
        "size_bytes": 101,
        "sha256": _digest("catalog"),
    }
    feature = {
        "path": "offline_data/feature.json.gz",
        "size_bytes": 102,
        "sha256": _digest("feature"),
        "complete_normalized_file_scanned": True,
        "row_level_copy_written": False,
    }
    provenance = {
        "path": "offline_data/provenance.json",
        "size_bytes": 103,
        "sha256": _digest("provenance"),
    }
    normalized = {
        "path": "offline_data/normalized.jsonl",
        "size_bytes": 104,
        "byte_sha256": _digest("normalized-bytes"),
        "byte_hash_status": "COMPUTED_LOCALLY_BY_COMPACT_COMPILER",
        "uploaded_with_capsule": False,
    }
    raw = {
        "path": "offline_data/raw.csv",
        "size_bytes": 105,
        "row_count": 7,
        "byte_sha256": _digest("raw-bytes"),
        "byte_hash_status": "COMPUTED_LOCALLY_BY_COMPACT_COMPILER",
        "uploaded_with_capsule": False,
    }
    reference = sha256_json(
        {
            "raw_csv_source": raw,
            "normalized_source": normalized,
            "raw_provenance_sha256": provenance["sha256"],
        }
    )
    source_id = sha256_json(
        {
            "catalog_sha256": catalog["sha256"],
            "feature_sha256": feature["sha256"],
            "provenance_sha256": provenance["sha256"],
            "raw_source_reference_sha256": reference,
        }
    )
    return {
        "source_bundle_id": source_id,
        "catalog": catalog,
        "reconstruction_feature": feature,
        "raw_provenance": provenance,
        "normalized_source": normalized,
        "raw_csv_source": raw,
        "raw_source_reference_sha256": reference,
    }


def _fixture_source_instance_provenance(source: dict) -> dict:
    entry = {
        "instance_id": "instance",
        "source_bundle_id": source["source_bundle_id"],
        "raw_source_reference_sha256": source["raw_source_reference_sha256"],
        "raw_provenance_sha256": source["raw_provenance"]["sha256"],
        "raw_csv_byte_sha256": source["raw_csv_source"]["byte_sha256"],
        "normalized_byte_sha256": source["normalized_source"]["byte_sha256"],
        "catalog_sha256": source["catalog"]["sha256"],
        "reconstruction_feature_sha256": source["reconstruction_feature"][
            "sha256"
        ],
    }
    return {
        "schema": "fury_development_source_instance_provenance/v2",
        "entry_count": 1,
        "entries": [entry],
        "source_instance_provenance_sha256": sha256_json([entry]),
        "raw_and_normalized_byte_identity_claimed": True,
        "raw_or_normalized_bytes_embedded": False,
    }


def _target(index: int, horizon_ms: int, *, dynamic_armor: bool) -> dict:
    transitions = []
    if dynamic_armor:
        transitions.append(
            {
                "debuff_id": "sunder_armor",
                "observed_aura_name": "Sunder Armor",
                "observed_spell_id": None,
                "spell_id_status": "MISSING_IN_COMPACT_FEATURE_V1",
                "operation": "set",
                "stacks": 1,
                "offset_ms": 100,
                "status": "OBSERVED_AURA_TRANSITION",
                "anchor": {"type": "AURA", "offset_ms": 100},
            }
        )
    return {
        "target_index": index,
        "target_guid": f"0xF13000000{index}",
        "creature_entry_id": 10_000 + index,
        "display_name": f"Target {index}",
        "observed_hostile_activity_proxy": {
            "start_ms": 0,
            "end_ms": horizon_ms - 50,
            "status": "OBSERVED_ACTIVITY_INTERVAL",
            "not_equal_to": "exact WoW attackability window",
            "first_anchor": {"type": "DMG", "offset_ms": 0},
            "last_anchor": {"type": "DMG", "offset_ms": horizon_ms - 50},
        },
        "attackable_window_hypothesis_family": [
            {
                "branch_id": "full_wave",
                "windows": [[0, horizon_ms]],
                "status": "SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "none_assumed",
            },
            {
                "branch_id": "observed_hostile_activity_proxy",
                "windows": [[0, horizon_ms - 50]],
                "status": "EVIDENCE_BOUNDED_SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "UNIDENTIFIED",
            },
        ],
        "attackability_cause_exact": {"value": None, "status": "MISSING"},
        "max_health_hypothesis_family": {
            "exact_max_health": {"value": None, "status": "MISSING"},
            "confirmed_single_hit_lower_bound": {"value": 100, "status": "RECONSTRUCTED"},
            "observed_kill_budget_proxy": {"value": 10_000, "status": "OBSERVED"},
            "repeated_creature_entry_health_prior": {"value": None, "status": "MISSING"},
            "shared_branch_family": [
                {
                    "branch_id": "duration_only",
                    "use_health": False,
                    "max_health": None,
                    "status": "CONTROL_BRANCH",
                },
                {
                    "branch_id": "proxy_center",
                    "use_health": True,
                    "max_health": 10_000,
                    "status": "SENSITIVITY_HYPOTHESIS",
                },
            ],
            "health_enabled_endogenous_ttk_execution_eligible": False,
            "endogenous_ttk_blocker": "background_team_damage_model_missing",
            "historical_truth": False,
        },
        "observed_combat_summaries": {},
        "background_team_damage_model": {
            "value": None,
            "status": "MISSING_REQUIRES_PLAYER_CONDITIONING",
        },
        "classification": {
            "observed_coarse_value": "Hostile Creature",
            "observed_coarse_status": "OBSERVED",
            "wow_unit_classification_exact": {"value": None, "status": "MISSING"},
            "simulator_hypothesis_family": ["non_worldboss", "worldboss"],
        },
        "armor": {
            "observed_transitions": transitions,
            "reconstructed_partial_prefix_strata": [],
            "base_armor_exact": {"value": None, "status": "MISSING"},
            "effective_armor_exact": {"value": None, "status": "MISSING"},
        },
    }


def _scenario(index: int, targets: int) -> dict:
    horizon_ms = 5_000 + index
    scenario_id = f"instance__encounter-{index}__wave-1__fixture__armor-1721__level-60"
    original_request_sha = _digest(f"original-request-{index}")
    source_id = _fixture_source()["source_bundle_id"]
    core = {
        "scenario_id": scenario_id,
        "source_bundle_id": source_id,
        "historical_truth": False,
        "claim_scope": "PREREGISTERED_SIMULATOR_SCENARIO_MODEL_ONLY",
        "source_identity": {
            "instance_id": "instance",
            "encounter_id": f"encounter-{index}",
            "wave_id": f"wave-{index}",
            "wave_ordinal": 1,
        },
        "horizon": {
            "milliseconds": horizon_ms,
            "status": "RECONSTRUCTED_HOSTILE_ACTIVITY_SPAN",
            "not_equal_to": "exact pull duration or exact attackability duration",
        },
        "wave_role": {},
        "layout": {},
        "targets": [
            _target(target_index, horizon_ms, dynamic_armor=index == 1 and target_index == 0)
            for target_index in range(targets)
        ],
        "base_armor_hypothesis_family": [
            {
                "branch_id": "catalog_inferred_1721",
                "base_armor": 1721,
                "status": "SENSITIVITY_HYPOTHESIS",
                "not_identified_by_chronicle": True,
            }
        ],
        "provenance_hashes": {
            "corpus_entry_sha256": _digest(f"corpus-entry-{index}"),
            "source_scenario_sha256": _digest(f"source-scenario-{index}"),
            "source_sha256": _digest(f"source-{index}"),
            "request_sha256": original_request_sha,
            "pile_sha256": _digest(f"pile-{index}"),
            "target_hypotheses_sha256": _digest(f"target-hypotheses-{index}"),
            "kill_budget_proxies_sha256": _digest(f"kill-budget-{index}"),
        },
        "runner_projection": {
            "instance_id": "instance",
            "component_id": "component",
            "family_id": f"family-{index}",
            "stratum": "single_target" if targets == 1 else "multi_target",
            "scenario_weight": 1.0,
            "request_sha256": original_request_sha,
        },
        "execution_mode_eligibility": {
            "fixed_observed_horizon_duration_model": True,
            "health_enabled_endogenous_ttk": False,
            "health_enabled_endogenous_ttk_blocker": "background model missing",
        },
    }
    return {**core, "capsule_sha256": sha256_json(core)}


def _bundle() -> dict:
    scenarios = [_scenario(1, 1), _scenario(2, 2)]
    source = _fixture_source()
    core = {
        "schema_version": 2,
        "schema": "fury_offline_scenario_capsules/v2",
        "kind": "fury_offline_scenario_capsule_bundle_v2",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "historical_truth": False,
        "corpus_role": "development_only",
        "bucket": "main_comparison",
        "claim_boundary": {
            "portable_simulator_scenario_model": True,
            "exact_historical_replay": False,
            "exact_target_health": False,
            "exact_base_or_effective_armor": False,
            "exact_attackability": False,
            "exact_wow_unit_classification": False,
            "exact_coordinates": False,
            "simulator_execution_started": False,
            "superiority_result": False,
        },
        "source_contract": {
            "corpus_manifest_path": "offline_data/corpus-manifest.json.gz",
            "corpus_manifest_sha256": _digest("corpus-manifest"),
            "batch_manifest_path": "offline_data/batch-manifest.json",
            "batch_manifest_sha256": _digest("batch-manifest"),
            "raw_csv_or_normalized_rows_opened": True,
            "raw_csv_or_normalized_rows_parsed": False,
            "raw_csv_or_normalized_rows_embedded": False,
            "raw_upload_required": False,
            "feature_files_content_hashed": True,
            "raw_csv_and_normalized_bytes_content_hashed": True,
        },
        "transport_contract": {
            "upload_capsule_only": True,
            "raw_upload_required": False,
            "raw_csv_uploaded": False,
            "normalized_jsonl_uploaded": False,
        },
        "model_registry": {},
        "sources": [source],
        "source_instance_provenance": _fixture_source_instance_provenance(source),
        "summary": {
            "source_bundle_count": 1,
            "scenario_count": 2,
            "target_count": 3,
            "stratum_counts": {"multi_target": 1, "single_target": 1},
            "raw_csv_or_normalized_byte_count_uploaded": 0,
        },
        "scenarios": scenarios,
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _base_request() -> dict:
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "name": "Pinned Fury",
                            "equipment": {"items": [{"id": 1}]},
                            "distanceFromTarget": 5,
                        }
                    ]
                }
            ]
        },
        "encounter": {
            "duration": 300,
            "targets": [
                {
                    "id": 99,
                    "level": 63,
                    "mobType": "MobTypeDemon",
                    "stats": [7] * 46,
                    "minBaseDamage": 100,
                    "swingSpeed": 2,
                }
            ],
        },
        "simOptions": {"iterations": 1, "randomSeed": "fixture"},
    }


def _reseal_scenario_and_bundle(bundle: dict, scenario_index: int) -> None:
    scenario = bundle["scenarios"][scenario_index]
    scenario_core = deepcopy(scenario)
    scenario_core.pop("capsule_sha256", None)
    scenario["capsule_sha256"] = sha256_json(scenario_core)
    bundle_core = deepcopy(bundle)
    bundle_core.pop("content_address", None)
    bundle["content_address"]["sha256"] = sha256_json(bundle_core)


def _reseal_binding(binding: dict) -> None:
    core = deepcopy(binding)
    core.pop("content_address", None)
    binding["content_address"]["sha256"] = sha256_json(core)


class FuryCapsuleExecutionBindingV2Tests(unittest.TestCase):
    def test_static_binding_is_runner_accepted_and_claims_remain_bounded(self) -> None:
        binding = compile_capsule_static_execution_binding_v2(
            _bundle(), _base_request(), target_level=60
        )
        rows = extract_runner_scenarios_v2(binding)
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(normalize_runner_scenarios(rows)), rows)
        self.assertEqual(binding["summary"]["target_count"], 3)
        self.assertRegex(
            binding["runner_projection"]["scenario_model_bundle_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(
            binding["runner_projection"]["target_context_bundle_set_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(binding["summary"]["dynamic_armor_transition_count_excluded"], 1)
        self.assertEqual(binding["summary"]["non_full_wave_attackability_branch_count_excluded"], 3)
        self.assertFalse(binding["historical_truth"])
        self.assertFalse(binding["capability_boundary"]["exact_historical_replay"])
        self.assertFalse(binding["capability_boundary"]["superiority_result"])
        self.assertFalse(
            binding["capability_boundary"]["exact_full_policy_target_context_compiled"]
        )
        self.assertFalse(binding["capability_boundary"]["comparison_eligible"])
        self.assertTrue(
            all(
                not row["execution_eligibility"]["comparison_eligible"]
                for row in binding["scenario_semantics"]
            )
        )
        first = rows[0]
        self.assertFalse(first["request"]["encounter"]["useHealth"])
        self.assertEqual(first["request"]["encounter"]["duration"], 5.001)
        request_target = first["request"]["encounter"]["targets"][0]
        self.assertEqual(request_target["level"], 60)
        self.assertEqual(request_target["mobType"], "MobTypeUnknown")
        self.assertEqual(request_target["stats"][26], 1721)
        self.assertEqual(request_target["stats"][34], 0)
        self.assertNotIn("minBaseDamage", request_target)
        self.assertFalse(first["scenario_model"]["comparison_eligible"])
        self.assertEqual(
            first["scenario_model"]["dynamic_armor_schedule_status"],
            "OBSERVED_RECEIPTS_RETAINED_NOT_EXECUTED",
        )
        self.assertEqual(
            len(
                first["scenario_model"]["dynamic_semantics_receipt"][
                    "target_models"
                ][0]["armor"]["observed_transitions"]
            ),
            1,
        )

    def test_static_capsule_cannot_enter_a_comparison_plan(self) -> None:
        rows = extract_runner_scenarios_v2(
            compile_capsule_static_execution_binding_v2(
                _bundle(), _base_request(), target_level=60
            )
        )
        with self.assertRaisesRegex(FuryPairedRunnerError, "non-comparison"):
            build_runner_plan(
                protocol_id="static-capsule-attack-test",
                protocol_sha256=_digest("protocol"),
                phase="development",
                corpus_manifest_sha256=_digest("corpus"),
                runner_inputs_sha256=_digest("inputs"),
                runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(rows),
                corpus_binding_sha256=_digest("binding"),
                master_seeds=(1,),
                scenarios=rows,
                policies=[
                    {
                        "policy_id": "cat.fury.profile1",
                        "source_sha256": _digest("source"),
                        "adapter_sha256": _digest("adapter"),
                        "profile_sha256": _digest("profile"),
                        "role": "BASELINE",
                    }
                ],
                shard_count=1,
                bridge_identity={
                    "sha256": _digest("bridge"),
                    "platform": "windows-amd64",
                },
                execution_bundle_identity={
                    "python_source_closure_sha256": _digest("closure"),
                    "ordered_sink_executor_sha256": _digest("ordered"),
                    "full_policy_rollout_executor_sha256": _digest("full"),
                    "paired_runner_source_sha256": _digest("runner"),
                    "evaluation_source_sha256": _digest("evaluation"),
                    "runtime_snapshot_sha256": _digest("runtime"),
                },
                execution_mode=SINGLE_BRIDGE_MODE,
                seed_namespace="static-capsule-attack-test",
                plan_intent=COMPARISON_INTENT,
            )

    def test_dynamic_armor_request_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error,
            "DYNAMIC_ARMOR_SCHEDULE_UNSUPPORTED",
        ):
            compile_capsule_static_execution_binding_v2(
                _bundle(),
                _base_request(),
                target_level=60,
                armor_mode="OBSERVED_TRANSITION_REPLAY",
            )

    def test_dynamic_attackability_request_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error,
            "DYNAMIC_ATTACKABILITY_SCHEDULE_UNSUPPORTED",
        ):
            compile_capsule_static_execution_binding_v2(
                _bundle(),
                _base_request(),
                target_level=60,
                attackability_mode="OBSERVED_HOSTILE_ACTIVITY_PROXY",
            )

    def test_health_enabled_request_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error,
            "HEALTH_ENABLED_ENDOGENOUS_TTK_UNSUPPORTED",
        ):
            compile_capsule_static_execution_binding_v2(
                _bundle(),
                _base_request(),
                target_level=60,
                health_mode="HEALTH_ENABLED_PROXY_CENTER",
            )

    def test_target_level_conflict_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error,
            "conflicts with the versioned scenario level",
        ):
            compile_capsule_static_execution_binding_v2(
                _bundle(), _base_request(), target_level=63
            )

    def test_strict_enumerator_catches_resealed_target_index_drift(self) -> None:
        bundle = _bundle()
        bundle["scenarios"][1]["targets"][1]["target_index"] = 9
        _reseal_scenario_and_bundle(bundle, 1)
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error, "target indices are not contiguous"
        ):
            enumerate_capsule_scenarios_v2(bundle)

    def test_binding_content_tamper_is_rejected(self) -> None:
        binding = compile_capsule_static_execution_binding_v2(
            _bundle(), _base_request(), target_level=60
        )
        binding["capability_boundary"]["dynamic_armor_schedule_supported"] = True
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error, "content address mismatch"
        ):
            validate_capsule_static_execution_binding_v2(binding)

    def test_resealed_invalid_base_request_hash_is_rejected(self) -> None:
        binding = compile_capsule_static_execution_binding_v2(
            _bundle(), _base_request(), target_level=60
        )
        binding["runner_projection"]["base_request_sha256"] = "not-a-hash"
        _reseal_binding(binding)
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error, "base-request SHA"
        ):
            validate_capsule_static_execution_binding_v2(binding)

    def test_resealed_summary_fabrication_is_rejected(self) -> None:
        binding = compile_capsule_static_execution_binding_v2(
            _bundle(), _base_request(), target_level=60
        )
        binding["summary"]["dynamic_armor_transition_count_excluded"] = 999
        _reseal_binding(binding)
        with self.assertRaisesRegex(
            FuryCapsuleExecutionBindingV2Error, "summary mismatch"
        ):
            validate_capsule_static_execution_binding_v2(binding)

    def test_capsule_and_binding_gzip_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            capsule_path = directory / "capsule.json.gz"
            capsule_bytes = json.dumps(
                _bundle(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            with capsule_path.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                    zipped.write(capsule_bytes)
            loaded = load_capsule_bundle_v2(capsule_path)
            self.assertEqual(len(enumerate_capsule_scenarios_v2(loaded)), 2)
            binding = compile_capsule_static_execution_binding_v2(
                loaded, _base_request(), target_level=60
            )
            output = directory / "binding.json.gz"
            write_capsule_static_execution_binding_v2(binding, output)
            self.assertTrue(output.is_file())
            with gzip.open(output, mode="rt", encoding="utf-8") as handle:
                restored = json.load(handle)
            validate_capsule_static_execution_binding_v2(restored)
            self.assertEqual(restored["content_address"], binding["content_address"])


if __name__ == "__main__":
    unittest.main()
