from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr
from dataclasses import replace
import gzip
import hashlib
import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_old50_exact_fury_dynamic_v3_adapter_v1 as adapter
from o2o_dps import chronicle_old50_exact_fury_slot_overlay_v1 as overlay_v1
from o2o_dps import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1
from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json
from tests.test_chronicle_old50_exact_fury_slot_overlay_v1 import (
    GUID,
    TARGET_A,
    TARGET_B,
    _build as _overlay_wave,
    _manifest_fixture,
)
from tests.test_fury_capsule_execution_binding_v2 import (
    _base_request,
    _bundle,
    _fixture_source,
    _reseal_scenario_and_bundle,
)


MANIFEST_SHA = "a" * 64


def _addressed(value: dict) -> dict:
    core = deepcopy(value)
    core.pop("content_address", None)
    return overlap_v1._content_addressed(core)


def _aligned_inputs(*, pdf: bool = False) -> tuple[dict, dict]:
    wave = _overlay_wave(pdf=pdf)
    wave["identity"].update(
        {
            "instance_id": "instance",
            "encounter_id": "encounter-2",
            "encounter_ordinal": 2,
            "external_wave_id": "encounter-2:external-v2-wave:1",
            "wave_ordinal": 1,
            "overlay_key": [
                "instance",
                "encounter-2",
                1,
                wave["identity"]["selected_guid"],
            ],
        }
    )
    bundle = _bundle()
    for scenario_index, candidate in enumerate(bundle["scenarios"]):
        source = candidate["source_identity"]
        source["wave_id"] = (
            f"{source['encounter_id']}:wave:{source['wave_ordinal']}"
        )
        _reseal_scenario_and_bundle(bundle, scenario_index)
    scenario = bundle["scenarios"][1]
    scenario["layout"] = {
        "spatial_assumption": {
            "value": "single_pile",
            "status": "RECONSTRUCTED",
            "coordinates": None,
            "not_observed": True,
        }
    }
    scenario["targets"][0]["target_guid"] = TARGET_B
    scenario["targets"][1]["target_guid"] = TARGET_A
    _reseal_scenario_and_bundle(bundle, 1)
    wave["source_bindings"]["old50_capsule_content_sha256"] = bundle[
        "content_address"
    ]["sha256"]
    wave = _addressed(wave)
    overlay_v1.validate_wave_overlay_v1(wave)
    return wave, bundle


def _compile(*, pdf: bool = False):
    wave, bundle = _aligned_inputs(pdf=pdf)
    return adapter.compile_exact_fury_overlay_dynamic_v3_v1(
        overlay_wave=wave,
        overlay_manifest_content_sha256=MANIFEST_SHA,
        capsule_bundle=bundle,
        base_request=_base_request(),
        equipped_item_names=("削骨之刃", "十字军附魔"),
        target_level=63,
        initial_base_armor=4211,
        health_branch_selection_policy=adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1,
        attackability_branch_id="full_wave",
        target_classification="elite",
    )


def _replace_string(value, old: str, new: str):
    if isinstance(value, dict):
        return {key: _replace_string(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_string(item, old, new) for item in value]
    return new if value == old else value


def _reseal_compiled_artifact(value: dict) -> None:
    core = deepcopy(value)
    core.pop("content_address", None)
    value["content_address"]["sha256"] = sha256_json(core)


def _recook_compiled_artifact(value: dict) -> None:
    scenario = value["scenario"]
    model = scenario["scenario_model"]
    source = model["source"]
    hypotheses = value["hypothesis_selection"]
    scenario["target_context_bundle_sha256"] = sha256_json(
        scenario["target_context_bundle"]
    )
    compilation_sha = sha256_json(
        {
            "overlay_wave_content_sha256": value["source_bindings"][
                "overlay_wave_content_sha256"
            ],
            "capsule_scenario_sha256": value["source_bindings"][
                "capsule_scenario_sha256"
            ],
            "wave_identity_join": value["source_bindings"]["wave_identity_join"],
            "selected_guid": value["expert_training_projection"]["selected_guid"],
            "selection_lane": value["expert_training_projection"]["selection_lane"],
            "request_sha256": scenario["request_sha256"],
            "dynamic_config_content_sha256": scenario["dynamic_load_config"][
                "content_sha256"
            ],
            "target_context_bundle_sha256": scenario[
                "target_context_bundle_sha256"
            ],
            "hypothesis_selection": hypotheses,
        }
    )
    source["compilation_identity_sha256"] = compilation_sha
    scenario["scenario_id"] = (
        f"old50-exact-fury-"
        f"{value['source_bindings']['overlay_wave_content_sha256'][:16]}"
        f"-compile-{compilation_sha[:24]}"
    )
    value["scenario"] = adapter.normalize_runner_scenarios([scenario])[0]
    _reseal_compiled_artifact(value)


def _write_formal_overlay_fixture(
    directory: Path, *, capsule_sha256: str | None = None
) -> Path:
    manifest = _manifest_fixture()
    sources = manifest["source_bindings"]
    if capsule_sha256 is not None:
        sources["old50_capsule_content_sha256"] = capsule_sha256
    entries = []
    for instance_index, entry_template in enumerate(manifest["instances"]):
        entry = deepcopy(entry_template)
        instance_id = entry["instance_id"]
        lane = entry["selector"]["selection_lane"]
        selected_guid = entry["selector"]["selected_guid"]
        rows = []
        for wave_ordinal in range(entry["selector"]["accepted_wave_count"]):
            row = _replace_string(
                _overlay_wave(pdf=lane == overlay_v1.selector_v2.PDF_LANE),
                GUID,
                selected_guid,
            )
            encounter_id = f"fixture-encounter-{instance_index:02d}"
            row["identity"].update(
                {
                    "instance_id": instance_id,
                    "encounter_id": encounter_id,
                    "encounter_ordinal": 1,
                    "external_wave_id": (
                        f"{encounter_id}:external-v2-wave:{wave_ordinal}"
                    ),
                    "wave_ordinal": wave_ordinal,
                    "selected_guid": selected_guid,
                    "overlay_key": [
                        instance_id,
                        encounter_id,
                        wave_ordinal,
                        selected_guid,
                    ],
                }
            )
            row["slot_selection"]["selector_row_content_sha256"] = entry[
                "selector"
            ]["selector_row_content_sha256"]
            row["source_bindings"].update(
                {
                    "selector_v2_content_sha256": sources["selector_v2"][
                        "content_sha256"
                    ],
                    "stage5_manifest_content_sha256": sources[
                        "stage5_manifest"
                    ]["content_sha256"],
                    "stage6_manifest_content_sha256": sources[
                        "stage6_manifest"
                    ]["content_sha256"],
                    "old50_capsule_content_sha256": sources[
                        "old50_capsule_content_sha256"
                    ],
                }
            )
            rows.append(_addressed(row))
        logical_payload = b"".join(overlay_v1._canonical(row) + b"\n" for row in rows)
        logical_sha = hashlib.sha256(logical_payload).hexdigest()
        partition_name = f"{instance_id}.{logical_sha}.jsonl.gz"
        partition_path = directory / partition_name
        with partition_path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
                handle.write(logical_payload)
        compressed = partition_path.read_bytes()
        summaries = [row["summary"] for row in rows]
        entry["partition"] = {
            "path": partition_name,
            "record_schema": overlay_v1.RECORD_SCHEMA,
            "record_count": len(rows),
            "logical_content_sha256": logical_sha,
            "logical_size_bytes": len(logical_payload),
            "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_size_bytes": len(compressed),
            "gzip_mtime": 0,
        }
        entry["summary"] = {
            "wave_overlay_count": len(rows),
            "prefix_transition_count": sum(
                row["prefix_transition_count"] for row in summaries
            ),
            "action_trace_ref_count": sum(
                row["action_trace_ref_count"] for row in summaries
            ),
            "outcome_context_trace_ref_count": sum(
                row["outcome_context_trace_ref_count"] for row in summaries
            ),
            "target_trace_ref_count": sum(
                row["target_trace_ref_count"] for row in summaries
            ),
            "projected_teammate_event_count": sum(
                row["projected_teammate_event_count"] for row in summaries
            ),
            "projected_teammate_damage": sum(
                row["projected_teammate_damage"] for row in summaries
            ),
            "excluded_focal_event_count": sum(
                row["excluded_focal_event_count"] for row in summaries
            ),
            "excluded_focal_damage": sum(
                row["excluded_focal_damage"] for row in summaries
            ),
            "zero_transition_wave_count": sum(
                row["prefix_transition_count"] == 0 for row in summaries
            ),
            "zero_teammate_schedule_wave_count": sum(
                row["projected_teammate_event_count"] == 0 for row in summaries
            ),
            "selected_guid_missing_wave_count": 0,
        }
        entries.append(_addressed(entry))
    manifest["instances"] = entries
    manifest["summary"] = overlay_v1._manifest_summary(entries)
    manifest = _addressed(manifest)
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return path


def _formal_capsule_fixture() -> dict:
    bundle = _bundle()
    template = deepcopy(bundle["scenarios"][1])
    scenarios = []
    manifest = _manifest_fixture()
    sources = []
    provenance_entries = []
    source_by_instance = {}
    for entry in manifest["instances"]:
        instance_id = entry["instance_id"]
        source = _fixture_source()
        source["catalog"]["sha256"] = hashlib.sha256(
            f"catalog-{instance_id}".encode()
        ).hexdigest()
        source["reconstruction_feature"]["sha256"] = hashlib.sha256(
            f"feature-{instance_id}".encode()
        ).hexdigest()
        source["raw_provenance"]["sha256"] = hashlib.sha256(
            f"provenance-{instance_id}".encode()
        ).hexdigest()
        source["normalized_source"]["byte_sha256"] = hashlib.sha256(
            f"normalized-{instance_id}".encode()
        ).hexdigest()
        source["raw_csv_source"]["byte_sha256"] = hashlib.sha256(
            f"raw-{instance_id}".encode()
        ).hexdigest()
        source["raw_source_reference_sha256"] = sha256_json(
            {
                "raw_csv_source": source["raw_csv_source"],
                "normalized_source": source["normalized_source"],
                "raw_provenance_sha256": source["raw_provenance"]["sha256"],
            }
        )
        source["source_bundle_id"] = sha256_json(
            {
                "catalog_sha256": source["catalog"]["sha256"],
                "feature_sha256": source["reconstruction_feature"]["sha256"],
                "provenance_sha256": source["raw_provenance"]["sha256"],
                "raw_source_reference_sha256": source[
                    "raw_source_reference_sha256"
                ],
            }
        )
        sources.append(source)
        source_by_instance[instance_id] = source
        provenance_entries.append(
            {
                "instance_id": instance_id,
                "source_bundle_id": source["source_bundle_id"],
                "raw_source_reference_sha256": source[
                    "raw_source_reference_sha256"
                ],
                "raw_provenance_sha256": source["raw_provenance"]["sha256"],
                "raw_csv_byte_sha256": source["raw_csv_source"]["byte_sha256"],
                "normalized_byte_sha256": source["normalized_source"][
                    "byte_sha256"
                ],
                "catalog_sha256": source["catalog"]["sha256"],
                "reconstruction_feature_sha256": source[
                    "reconstruction_feature"
                ]["sha256"],
            }
        )
    for instance_index, entry in enumerate(manifest["instances"]):
        instance_id = entry["instance_id"]
        encounter_id = f"fixture-encounter-{instance_index:02d}"
        for wave_ordinal in range(entry["selector"]["accepted_wave_count"]):
            scenario = deepcopy(template)
            scenario["scenario_id"] = (
                f"{instance_id}__{encounter_id}__wave-{wave_ordinal:04d}"
            )
            scenario["source_identity"] = {
                "instance_id": instance_id,
                "encounter_id": encounter_id,
                "wave_id": f"{encounter_id}:wave:{wave_ordinal}",
                "wave_ordinal": wave_ordinal,
            }
            scenario["runner_projection"]["instance_id"] = instance_id
            scenario["runner_projection"]["component_id"] = "component"
            scenario["source_bundle_id"] = source_by_instance[instance_id][
                "source_bundle_id"
            ]
            scenario["layout"] = {
                "spatial_assumption": {
                    "value": "single_pile",
                    "status": "RECONSTRUCTED",
                    "coordinates": None,
                    "not_observed": True,
                }
            }
            scenario["targets"][0]["target_guid"] = TARGET_B
            scenario["targets"][1]["target_guid"] = TARGET_A
            core = deepcopy(scenario)
            core.pop("capsule_sha256", None)
            scenario["capsule_sha256"] = sha256_json(core)
            scenarios.append(scenario)
    scenarios.sort(
        key=lambda row: (
            row["runner_projection"]["instance_id"], row["scenario_id"]
        )
    )
    bundle["scenarios"] = scenarios
    bundle["sources"] = sources
    provenance_entries.sort(key=lambda row: row["instance_id"])
    bundle["source_instance_provenance"] = {
        "schema": "fury_development_source_instance_provenance/v2",
        "entry_count": len(provenance_entries),
        "entries": provenance_entries,
        "source_instance_provenance_sha256": sha256_json(provenance_entries),
        "raw_and_normalized_byte_identity_claimed": True,
        "raw_or_normalized_bytes_embedded": False,
    }
    bundle["summary"].update(
        {
            "source_bundle_count": 20,
            "scenario_count": 470,
            "target_count": 940,
            "stratum_counts": {"multi_target": 470},
        }
    )
    core = deepcopy(bundle)
    core.pop("content_address", None)
    bundle["content_address"]["sha256"] = sha256_json(core)
    adapter.build_validated_old50_capsule_index_v1(bundle)
    return bundle


class ChronicleOld50ExactFuryDynamicV3AdapterV1Tests(unittest.TestCase):
    def test_formal_loader_scans_and_closes_all_20_instances_470_waves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_formal_overlay_fixture(Path(directory))
            manifest = json.loads(path.read_text(encoding="utf-8"))
            with mock.patch.multiple(
                adapter,
                FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256=manifest[
                    "content_address"
                ]["sha256"],
                FORMAL_OVERLAY_MANIFEST_FILE_SHA256=hashlib.sha256(
                    path.read_bytes()
                ).hexdigest(),
            ):
                corpus = adapter.load_formal_exact_fury_overlay_corpus_v1(path)
        self.assertEqual(20, corpus.manifest["summary"]["instance_count"])
        self.assertEqual(470, len(corpus.rows))
        self.assertEqual(
            470,
            len({tuple(row["identity"]["overlay_key"]) for row in corpus.rows}),
        )

    def test_formal_loader_rejects_an_unpinned_valid_20_by_470_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_formal_overlay_fixture(Path(directory))
            with self.assertRaisesRegex(
                adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
                "pinned published",
            ):
                adapter.load_formal_exact_fury_overlay_corpus_v1(path)

    def test_materializes_and_validates_manifest_last_20_by_470_corpus(self) -> None:
        capsule = _formal_capsule_fixture()
        equipped = {"equipped_item_names": ["Fixture Two-Hand Weapon"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overlay_directory = root / "overlay"
            overlay_directory.mkdir()
            overlay_path = _write_formal_overlay_fixture(
                overlay_directory,
                capsule_sha256=capsule["content_address"]["sha256"],
            )
            output = root / "compiled"
            overlay_manifest = json.loads(overlay_path.read_text(encoding="utf-8"))
            pins = {
                "FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256": overlay_manifest[
                    "content_address"
                ]["sha256"],
                "FORMAL_OVERLAY_MANIFEST_FILE_SHA256": hashlib.sha256(
                    overlay_path.read_bytes()
                ).hexdigest(),
                "FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1": {
                    "proxy_center": 940
                },
            }
            with mock.patch.multiple(adapter, **pins):
                result = adapter.materialize_exact_fury_overlay_dynamic_v3_v1(
                    overlay_manifest_path=overlay_path,
                    capsule_bundle=capsule,
                    base_request=_base_request(),
                    equipped_names_receipt=equipped,
                    output_directory=output,
                    target_level=63,
                    initial_base_armor=4211,
                    health_branch_selection_policy=(
                        adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                    ),
                    attackability_branch_id="full_wave",
                    target_classification="elite",
                    armor_magnitude_by_debuff=(
                        adapter.FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1
                    ),
                )
                reused = adapter.materialize_exact_fury_overlay_dynamic_v3_v1(
                    overlay_manifest_path=overlay_path,
                    capsule_bundle=capsule,
                    base_request=_base_request(),
                    equipped_names_receipt=equipped,
                    output_directory=output,
                    target_level=63,
                    initial_base_armor=4211,
                    health_branch_selection_policy=(
                        adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                    ),
                    attackability_branch_id="full_wave",
                    target_classification="elite",
                    armor_magnitude_by_debuff=(
                        adapter.FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1
                    ),
                )
                validated = reused.manifest
                self.assertEqual(result.manifest_path, reused.manifest_path)
                self.assertEqual(
                    result.addressed_manifest_path, reused.addressed_manifest_path
                )
        self.assertEqual(20, validated["summary"]["instance_count"])
        self.assertEqual(470, validated["summary"]["compiled_wave_count"])
        self.assertEqual(20, len(validated["instances"]))
        self.assertEqual(
            470,
            validated["summary"]["pdf_outcome_conditioned_wave_count"]
            + validated["summary"]["generic_outcome_free_wave_count"],
        )

    def test_compiles_exact_loo_schedule_and_strict_prefix_training(self) -> None:
        compiled = _compile()
        validated = adapter.validate_exact_fury_overlay_dynamic_v3_structure_v1(
            compiled.artifact
        )
        training = validated["expert_training_projection"]
        self.assertEqual(1, training["sample_count"])
        self.assertFalse(training["descriptive_outcome_context_included"])
        self.assertFalse(training["future_teammate_schedule_included"])
        self.assertNotIn("outcome_context_trace_refs", training)
        self.assertEqual(
            [0, 0, 0, 0, 0],
            training["samples"][0]["input_state_strict_prefix"][
                "cutoff_exclusive_order_key"
            ],
        )
        self.assertEqual(1, len(compiled.dynamic_config.background_damage_events))
        event = compiled.dynamic_config.background_damage_events[0]
        self.assertEqual(1, event.target_index)
        self.assertEqual(40, event.damage)
        environment = validated["environment_projection"]
        self.assertTrue(environment["exact_guid_leave_one_out"])
        self.assertFalse(environment["future_schedule_exported_to_policy_context"])
        self.assertTrue(environment["policy_runtime_receives_bridge_state_only"])
        model = validated["scenario"]["scenario_model"]
        self.assertEqual(
            ["instance", "encounter-2", 1],
            validated["source_bindings"]["wave_identity_join"]["join_key"],
        )
        context = validated["scenario"]["target_context_bundle"]
        for boundary in (model, context):
            self.assertFalse(boundary["comparison_eligible"])
            self.assertFalse(boundary["heldout_performance_evidence_eligible"])
            self.assertFalse(boundary["historical_policy_voting_eligible"])
            self.assertFalse(boundary["deployment_allowed"])
            self.assertFalse(boundary["future_team_schedule_visible_to_policy"])

    def test_pdf_lane_remains_outcome_conditioned_training_only(self) -> None:
        compiled = _compile(pdf=True)
        training = compiled.artifact["expert_training_projection"]
        self.assertTrue(training["selection_is_outcome_conditioned"])
        self.assertFalse(training["heldout_performance_evidence_eligible"])
        self.assertFalse(training["comparison_eligible"])
        self.assertFalse(training["deployment_allowed"])

    def test_versioned_health_policy_records_exact_per_target_fallbacks(self) -> None:
        wave, bundle = _aligned_inputs()
        scenario = bundle["scenarios"][1]
        families = [
            (
                "repeat_center",
                12000,
                "PRIMARY_MISSING_REPEAT_CENTER_FALLBACK",
            ),
            (
                "legacy_threshold_25000",
                25000,
                "PRIMARY_AND_REPEAT_MISSING_LEGACY_THRESHOLD_25000_FALLBACK",
            ),
        ]
        for target, (branch_id, max_health, _) in zip(
            scenario["targets"], families, strict=True
        ):
            original = target["max_health_hypothesis_family"][
                "shared_branch_family"
            ]
            duration_only = next(
                row for row in original if row["branch_id"] == "duration_only"
            )
            target["max_health_hypothesis_family"]["shared_branch_family"] = [
                duration_only,
                {
                    "branch_id": branch_id,
                    "use_health": True,
                    "max_health": max_health,
                    "status": "SENSITIVITY_HYPOTHESIS",
                }
            ]
        _reseal_scenario_and_bundle(bundle, 1)
        wave["source_bindings"]["old50_capsule_content_sha256"] = bundle[
            "content_address"
        ]["sha256"]
        wave = _addressed(wave)
        compiled = adapter.compile_exact_fury_overlay_dynamic_v3_v1(
            overlay_wave=wave,
            overlay_manifest_content_sha256=MANIFEST_SHA,
            capsule_bundle=bundle,
            base_request=_base_request(),
            equipped_item_names=("Fixture Weapon",),
            target_level=63,
            initial_base_armor=4211,
            health_branch_selection_policy=(
                adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
            ),
            attackability_branch_id="full_wave",
            target_classification="elite",
        )
        rows = compiled.artifact["hypothesis_selection"]["health_by_target"]
        policy = compiled.artifact["hypothesis_selection"][
            "health_branch_selection_policy"
        ]
        self.assertTrue(policy["historical_outcome_proxy_availability_used"])
        self.assertFalse(policy["candidate_or_simulator_outcome_used_for_selection"])
        self.assertFalse(policy["future_information_used_for_selection"])
        self.assertFalse(policy["heldout_performance_evidence_eligible"])
        self.assertFalse(policy["comparison_eligible"])
        self.assertEqual(
            [branch_id for branch_id, _, _ in families],
            [row["selected_branch_id"] for row in rows],
        )
        self.assertEqual(
            [reason for _, _, reason in families],
            [row["selection_reason"] for row in rows],
        )
        self.assertTrue(
            all(row["selected_branch_id"] in row["available_branch_ids"] for row in rows)
        )
        for row in rows:
            provenance = row["available_branch_provenance"]
            self.assertEqual(
                row["available_branch_ids"],
                [entry["branch_id"] for entry in provenance],
            )
            self.assertTrue(
                all(
                    entry["availability_from_historical_capsule"]
                    and not entry["historical_truth"]
                    for entry in provenance
                )
            )

    def test_global_expose_choice_is_projected_only_to_waves_that_observe_it(self) -> None:
        def compile_inputs(wave: dict, bundle: dict):
            return adapter.compile_exact_fury_overlay_dynamic_v3_v1(
                overlay_wave=wave,
                overlay_manifest_content_sha256=MANIFEST_SHA,
                capsule_bundle=bundle,
                base_request=_base_request(),
                equipped_item_names=("Fixture Weapon",),
                target_level=63,
                initial_base_armor=4211,
                health_branch_selection_policy=(
                    adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                ),
                attackability_branch_id="full_wave",
                target_classification="elite",
                armor_magnitude_by_debuff=(
                    adapter.FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1
                ),
            )

        no_expose_wave, no_expose_bundle = _aligned_inputs()
        without_expose = compile_inputs(no_expose_wave, no_expose_bundle)
        without_projection = without_expose.artifact["hypothesis_selection"][
            "armor"
        ]["magnitude_choice_projection"]
        self.assertEqual(
            adapter.FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1,
            without_projection["corpus_global_magnitude_choices"],
        )
        self.assertEqual([], without_projection["wave_observed_debuff_ids"])
        self.assertEqual({}, without_projection["projected_magnitude_choices"])

        expose_wave, expose_bundle = _aligned_inputs()
        scenario = expose_bundle["scenarios"][1]
        scenario["targets"][0]["armor"]["observed_transitions"] = [
            {
                "debuff_id": "expose_armor",
                "observed_aura_name": "Expose Armor",
                "observed_spell_id": None,
                "spell_id_status": "MISSING_IN_COMPACT_FEATURE_V1",
                "operation": "set",
                "stacks": 1,
                "offset_ms": 300,
                "status": "OBSERVED_AURA_TRANSITION",
                "anchor": {"type": "AURA", "offset_ms": 300, "event_index": 9},
            }
        ]
        _reseal_scenario_and_bundle(expose_bundle, 1)
        expose_wave["source_bindings"]["old50_capsule_content_sha256"] = (
            expose_bundle["content_address"]["sha256"]
        )
        expose_wave = _addressed(expose_wave)
        with_expose = compile_inputs(expose_wave, expose_bundle)
        with_projection = with_expose.artifact["hypothesis_selection"]["armor"][
            "magnitude_choice_projection"
        ]
        self.assertEqual(["expose_armor"], with_projection["wave_observed_debuff_ids"])
        self.assertEqual(
            adapter.FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1,
            with_projection["projected_magnitude_choices"],
        )

        tampered = deepcopy(with_expose.artifact)
        tampered["hypothesis_selection"]["armor"]["magnitude_choice_projection"][
            "projected_magnitude_choices"
        ] = {}
        tampered["scenario"]["scenario_model"]["hypothesis_selection"] = deepcopy(
            tampered["hypothesis_selection"]
        )
        _recook_compiled_artifact(tampered)
        with self.assertRaisesRegex(
            adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
            "not projected by observed wave debuffs",
        ):
            adapter.validate_exact_fury_overlay_dynamic_v3_structure_v1(tampered)

    def test_capsule_content_binding_mismatch_fails_closed(self) -> None:
        wave, bundle = _aligned_inputs()
        bundle["content_address"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(
            adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
            "invalid old-50 compact capsule|not bound",
        ):
            adapter.compile_exact_fury_overlay_dynamic_v3_v1(
                overlay_wave=wave,
                overlay_manifest_content_sha256=MANIFEST_SHA,
                capsule_bundle=bundle,
                base_request=_base_request(),
                equipped_item_names=(),
                target_level=63,
                initial_base_armor=4211,
                health_branch_selection_policy=(
                    adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                ),
                attackability_branch_id="full_wave",
                target_classification="elite",
            )

    def test_capsule_external_wave_namespace_is_not_silently_joined(self) -> None:
        wave, bundle = _aligned_inputs()
        scenario = bundle["scenarios"][1]
        scenario["source_identity"]["wave_id"] = (
            "encounter-2:external-v2-wave:1"
        )
        _reseal_scenario_and_bundle(bundle, 1)
        wave["source_bindings"]["old50_capsule_content_sha256"] = bundle[
            "content_address"
        ]["sha256"]
        wave = _addressed(wave)
        with self.assertRaisesRegex(
            adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
            "frozen v1 derived namespace",
        ):
            adapter.compile_exact_fury_overlay_dynamic_v3_v1(
                overlay_wave=wave,
                overlay_manifest_content_sha256=MANIFEST_SHA,
                capsule_bundle=bundle,
                base_request=_base_request(),
                equipped_item_names=(),
                target_level=63,
                initial_base_armor=4211,
                health_branch_selection_policy=(
                    adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                ),
                attackability_branch_id="full_wave",
                target_classification="elite",
            )

    def test_validated_capsule_index_is_reused_for_multiple_wave_compiles(self) -> None:
        wave, bundle = _aligned_inputs()
        with mock.patch.object(
            adapter,
            "enumerate_capsule_scenarios_v2",
            wraps=adapter.enumerate_capsule_scenarios_v2,
        ) as enumerate_scenarios:
            index = adapter.build_validated_old50_capsule_index_v1(bundle)
            for _ in range(2):
                adapter.compile_exact_fury_overlay_dynamic_v3_v1(
                    overlay_wave=wave,
                    overlay_manifest_content_sha256=MANIFEST_SHA,
                    capsule_bundle=bundle,
                    capsule_index=index,
                    base_request=_base_request(),
                    equipped_item_names=(),
                    target_level=63,
                    initial_base_armor=4211,
                    health_branch_selection_policy=(
                        adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                    ),
                    attackability_branch_id="full_wave",
                    target_classification="elite",
                )
        self.assertEqual(1, enumerate_scenarios.call_count)

    def test_teammate_event_beyond_capsule_horizon_is_not_silently_trimmed(self) -> None:
        wave, bundle = _aligned_inputs()
        event = wave["exact_guid_loo_teammate_schedule"]["projected_schedule"][0]
        event["time_ms"] = 6000
        projection = wave["exact_guid_loo_teammate_schedule"]
        projection["projected_schedule_content_sha256"] = sha256_json(
            projection["projected_schedule"]
        )
        wave = _addressed(wave)
        with self.assertRaisesRegex(
            adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
            "exceeds the capsule horizon",
        ):
            adapter.compile_exact_fury_overlay_dynamic_v3_v1(
                overlay_wave=wave,
                overlay_manifest_content_sha256=MANIFEST_SHA,
                capsule_bundle=bundle,
                base_request=_base_request(),
                equipped_item_names=(),
                target_level=63,
                initial_base_armor=4211,
                health_branch_selection_policy=(
                    adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1
                ),
                attackability_branch_id="full_wave",
                target_classification="elite",
            )

    def test_output_tamper_cannot_enable_outcomes_comparison_or_deployment(self) -> None:
        compiled = _compile()
        cases = (
            ("expert_training_projection", "descriptive_outcome_context_included"),
            ("scientific_boundary", "comparison_eligible"),
            ("scientific_boundary", "deployment_allowed"),
        )
        for section, field in cases:
            with self.subTest(section=section, field=field):
                tampered = deepcopy(compiled.artifact)
                tampered[section][field] = True
                core = deepcopy(tampered)
                core.pop("content_address", None)
                tampered["content_address"]["sha256"] = sha256_json(core)
                with self.assertRaises(
                    adapter.ChronicleOld50ExactFuryDynamicV3AdapterError
                ):
                    adapter.validate_exact_fury_overlay_dynamic_v3_structure_v1(
                        tampered
                    )

    def test_readdressed_cross_section_tampering_fails_closed(self) -> None:
        compiled = _compile().artifact

        def prefix_outcome(value: dict) -> None:
            value["expert_training_projection"]["samples"][0][
                "input_state_strict_prefix"
            ]["descriptive_outcome"] = 999

        def selected_guid(value: dict) -> None:
            value["expert_training_projection"]["selected_guid"] = "fake-guid"

        def environment_focal(value: dict) -> None:
            value["environment_projection"]["focal_player_guid"] = "fake-guid"

        def environment_damage(value: dict) -> None:
            value["environment_projection"]["kept_background_damage"] += 1

        def classification(value: dict) -> None:
            value["hypothesis_selection"]["target_classification"] = "boss"

        for label, mutate in (
            ("prefix_outcome", prefix_outcome),
            ("selected_guid", selected_guid),
            ("environment_focal", environment_focal),
            ("environment_damage", environment_damage),
            ("classification", classification),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(compiled)
                mutate(tampered)
                _reseal_compiled_artifact(tampered)
                with self.assertRaises(
                    adapter.ChronicleOld50ExactFuryDynamicV3AdapterError
                ):
                    adapter.validate_exact_fury_overlay_dynamic_v3_structure_v1(
                        tampered
                    )

    def test_coupled_readdressed_tampering_fails_source_bound_validation(self) -> None:
        wave, _ = _aligned_inputs()
        compiled = _compile()

        def target_guid(value: dict) -> None:
            value["hypothesis_selection"]["health_by_target"][0][
                "target_guid"
            ] = "fake-target-guid"
            value["scenario"]["scenario_model"]["hypothesis_selection"] = deepcopy(
                value["hypothesis_selection"]
            )

        def retarget(value: dict) -> None:
            changed = replace(
                compiled.dynamic_config, retarget_mode="REQUIRE_EXPLICIT"
            )
            value["scenario"]["dynamic_load_config"] = changed.to_wire()

        def classification_truth(value: dict) -> None:
            value["hypothesis_selection"][
                "classification_is_historical_truth"
            ] = True
            value["scenario"]["scenario_model"]["hypothesis_selection"] = deepcopy(
                value["hypothesis_selection"]
            )

        def model_truth(value: dict) -> None:
            value["scenario"]["scenario_model"]["historical_truth"] = True

        def attackability_truth(value: dict) -> None:
            value["hypothesis_selection"]["attackability"][
                "attackability_is_historical_truth"
            ] = True
            value["scenario"]["scenario_model"]["hypothesis_selection"] = deepcopy(
                value["hypothesis_selection"]
            )

        def target_context_status(value: dict) -> None:
            value["scenario"]["target_context_bundle"]["status"] = (
                "HISTORICAL_CONTEXT"
            )

        for label, mutate in (
            ("target_guid", target_guid),
            ("retarget_mode", retarget),
            ("classification_truth", classification_truth),
            ("scenario_model_truth", model_truth),
            ("attackability_truth", attackability_truth),
            ("target_context_status", target_context_status),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(compiled.artifact)
                mutate(tampered)
                _recook_compiled_artifact(tampered)
                with self.assertRaises(
                    adapter.ChronicleOld50ExactFuryDynamicV3AdapterError
                ):
                    adapter.validate_exact_fury_overlay_dynamic_v3_structure_v1(
                        tampered, source_overlay_wave=wave
                    )

    def test_formal_manifest_guard_rejects_incomplete_overlay(self) -> None:
        incomplete = {
            "summary": {
                "instance_count": 19,
                "wave_overlay_count": 469,
            }
        }
        with mock.patch.object(
            overlay_v1, "validate_overlay_manifest_v1", return_value=incomplete
        ):
            with self.assertRaisesRegex(
                adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
                "20-instance / 470-wave",
            ):
                adapter._validate_formal_manifest(incomplete)

    def test_partition_retry_reuses_identical_and_rejects_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = root / "partition.jsonl.gz"
            completed.write_bytes(b"same")
            identical = root / "identical.tmp"
            identical.write_bytes(b"same")
            self.assertTrue(
                adapter._publish_partition_or_reuse(identical, completed)
            )
            self.assertFalse(identical.exists())
            different = root / "different.tmp"
            different.write_bytes(b"different")
            with self.assertRaisesRegex(
                adapter.ChronicleOld50ExactFuryDynamicV3AdapterError,
                "partition differs",
            ):
                adapter._publish_partition_or_reuse(different, completed)

    def test_cli_reports_blocked_without_traceback(self) -> None:
        output = StringIO()
        with redirect_stderr(output):
            code = adapter.main(
                [
                    "validate",
                    "--overlay-manifest",
                    "missing-overlay.json",
                    "--capsule",
                    "missing-capsule.json.gz",
                    "--base-request",
                    "missing-request.json",
                    "--equipped-names",
                    "missing-equipped.json",
                    "--manifest",
                    "missing-manifest.json",
                ]
            )
        self.assertEqual(2, code)
        diagnostic = json.loads(output.getvalue())
        self.assertEqual("BLOCKED", diagnostic["status"])
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
