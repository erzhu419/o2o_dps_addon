from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_old50_exact_fury_build_binding_v1 as binding
from o2o_dps import chronicle_old50_exact_fury_dynamic_v3_adapter_v1 as adapter
from o2o_dps import chronicle_old50_exact_fury_slot_overlay_v1 as overlay_v1
from o2o_dps.historical_build_catalog_v1 import (
    InstanceBuildInput,
    build_catalog_from_instances,
)
from tests.test_chronicle_old50_exact_fury_dynamic_v3_adapter_v1 import (
    MANIFEST_SHA,
    _aligned_inputs,
)
from tests.test_historical_build_catalog_v1 import _coverage, _record
from tests.test_fury_capsule_execution_binding_v2 import _base_request


GUID = "0x00000000000000F1"
INSTANCE = "instance"
ENCOUNTER = "encounter-2"


def _destination_request() -> dict:
    request = _base_request()
    player = request["raid"]["parties"][0]["players"][0]
    player.update(
        {
            "name": "Caller Template Must Disappear",
            "race": "RaceHuman",
            "class": "ClassWarrior",
            "warrior": {
                "options": {
                    "startingRage": 50,
                    "stance": "WarriorStanceBerserker",
                    "ravagerRank": 5,
                }
            },
            "buffs": {"blessingOfKings": True},
            "consumes": {"defaultPotion": "PotionMightyRage"},
            "database": {},
            "rotation": {"type": "RotationTypeAuto"},
            "cooldowns": {"hpPercentForDefensives": 20},
            "reactionTimeMs": 175,
            "channelClipDelayMs": 0,
            "inFrontOfTarget": False,
        }
    )
    request["raid"].update(
        {
            "buffs": {"arcaneBrilliance": True},
            "debuffs": {"sunderArmor": True},
            "numActiveParties": 1,
        }
    )
    request["raid"]["parties"][0]["buffs"] = {"battleShout": "TristateEffectRegular"}
    request["raid"]["parties"][0]["players"].append(
        {"name": "Teammate Must Remain", "class": "ClassMage"}
    )
    request["simOptions"] = {
        "iterations": 1,
        "randomSeed": "123456",
        "interactive": True,
    }
    return request


def _adapter_artifact() -> tuple[dict, dict]:
    wave, capsule = _aligned_inputs()
    compiled = adapter.compile_exact_fury_overlay_dynamic_v3_v1(
        overlay_wave=wave,
        overlay_manifest_content_sha256=MANIFEST_SHA,
        capsule_bundle=capsule,
        base_request=_destination_request(),
        equipped_item_names=("Caller Template Weapon",),
        target_level=63,
        initial_base_armor=4211,
        health_branch_selection_policy=adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1,
        attackability_branch_id="full_wave",
        target_classification="elite",
    )
    return compiled.artifact, wave


def _zero_focal_adapter_artifact() -> tuple[dict, dict]:
    wave, capsule = _aligned_inputs()
    episode = wave["focal_player_episode"]
    for field in (
        "exact_trace_indices",
        "action_trace_refs",
        "outcome_context_trace_refs",
        "target_trace_refs",
        "prefix_transitions",
    ):
        episode[field] = []
    for field in (
        "prefix_transition_count",
        "action_trace_ref_count",
        "outcome_context_trace_ref_count",
        "target_trace_ref_count",
    ):
        wave["summary"][field] = 0
    wave.pop("content_address")
    wave = overlay_v1._content_addressed(wave)
    overlay_v1.validate_wave_overlay_v1(wave)
    compiled = adapter.compile_exact_fury_overlay_dynamic_v3_v1(
        overlay_wave=wave,
        overlay_manifest_content_sha256=MANIFEST_SHA,
        capsule_bundle=capsule,
        base_request=_destination_request(),
        equipped_item_names=("Caller Template Weapon",),
        target_level=63,
        initial_base_armor=4211,
        health_branch_selection_policy=adapter.HEALTH_BRANCH_FALLBACK_POLICY_V1,
        attackability_branch_id="full_wave",
        target_classification="elite",
    )
    return compiled.artifact, wave


def _catalog_manifest(
    directory: Path,
    records: list[dict],
) -> Path:
    for record in records:
        record["instance_ref"] = INSTANCE
        record["slug"] = "fixture-old50"
        record["encounter_id"] = ENCOUNTER
    instance = InstanceBuildInput(
        server="Capybara",
        realm="Basin of Stars",
        realm_id="realm-1",
        instance_id=INSTANCE,
        instance_name="Upper Tower of Karazhan",
        slug="fixture-old50",
        metadata_versions={"wow_build": "7272", "wow_client": "1.18.1"},
        game_flavor="vanilla",
        game_format="1.12a-cc-addon",
        players=(
            {
                "guid": GUID,
                "metadata": {
                    "name": "Exact Historical Fury",
                    "class": "WARRIOR",
                    "race": "Orc",
                    "level": 60,
                },
            },
        ),
        records=tuple(records),
        source={"admission_status": "ADMITTED_FOR_VERSIONED_RECONSTRUCTION_INPUT"},
    )
    result = build_catalog_from_instances(
        (instance,), output_directory=directory, coverage_registry=_coverage()
    )
    return Path(result["manifest_path"])


def _record_for(
    timestamp_ms: int,
    event_index: int,
    item_id: int = 100,
    *,
    complete: bool = True,
) -> dict:
    return _record(
        guid=GUID,
        timestamp_ms=timestamp_ms,
        event_index=event_index,
        ordinal=event_index,
        item_id=item_id,
        complete=complete,
        representative=True,
    )


def _query(*keys: list[int], destination: dict | None = None) -> dict:
    return {
        "schema": binding.QUERY_SCHEMA,
        "identity": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "encounter_ordinal": 0,
            "wave_ordinal": 1,
            "selected_guid": GUID,
        },
        "focal_order_keys": list(keys),
        "anchor_source": "TEST_EXACT_EVENTMETA",
        "source_ref": {"schema": "test"},
        "destination_request": destination,
    }


class ChronicleOld50ExactFuryBuildBindingV1Tests(unittest.TestCase):
    def test_zero_action_adapter_takes_only_missing_ordinal_from_overlay(self) -> None:
        common = {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "wave_ordinal": 1,
            "selected_guid": GUID,
            "anchor_source": "test",
            "source_ref": {},
        }
        empty_adapter = binding.Old50WaveBuildQueryV1(
            **common, encounter_ordinal=0, focal_order_keys=()
        )
        overlay = binding.Old50WaveBuildQueryV1(
            **common,
            encounter_ordinal=58,
            focal_order_keys=((58, 100, 1, 0, 0),),
        )
        self.assertTrue(
            binding._adapter_overlay_identity_compatible(empty_adapter, overlay)
        )
        action_adapter = binding.Old50WaveBuildQueryV1(
            **common,
            encounter_ordinal=57,
            focal_order_keys=((57, 100, 1, 0, 0),),
        )
        self.assertFalse(
            binding._adapter_overlay_identity_compatible(action_adapter, overlay)
        )

    def test_windows_catalog_manifest_path_relocates_on_another_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["catalog_path"] = r"D:\staged_elsewhere\catalog.jsonl.gz"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            index = binding.load_historical_build_catalog_index_v1(manifest_path)
        self.assertEqual(
            "MANIFEST_SIBLING_BASENAME_RELOCATION", index.resolution_mode
        )
        self.assertEqual(1, index.catalog_line_count)

    def test_exact_build_replaces_only_character_fields(self) -> None:
        artifact, _ = _adapter_artifact()
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            index = binding.load_historical_build_catalog_index_v1(
                manifest, memberships={(INSTANCE, GUID)}
            )
            result = binding.bind_old50_wave_historical_build_v1(artifact, index)
        self.assertEqual("BOUND_EXECUTABLE_REQUEST", result["status"])
        self.assertTrue(result["coverage"]["equipment_evidence_complete"])
        self.assertTrue(result["coverage"]["talent_semantics_exact"])
        self.assertFalse(
            result["destination_preservation"][
                "caller_base_equipment_or_talents_retained"
            ]
        )
        before = artifact["scenario"]["request"]
        after = result["request"]
        self.assertEqual(before["encounter"], after["encounter"])
        self.assertEqual(before["simOptions"], after["simOptions"])
        self.assertEqual(
            "Teammate Must Remain",
            after["raid"]["parties"][0]["players"][1]["name"],
        )
        player = after["raid"]["parties"][0]["players"][0]
        self.assertEqual("Exact Historical Fury", player["name"])
        self.assertEqual("RaceOrc", player["race"])
        self.assertNotIn(
            1, [row.get("id") for row in player["equipment"]["items"] if row]
        )
        self.assertEqual(
            0, player["warrior"]["options"]["ravagerRank"]
        )
        self.assertEqual(
            {"defaultPotion": "PotionMightyRage"}, player["consumes"]
        )

    def test_overlay_supplies_all_focal_anchors_and_is_content_joined(self) -> None:
        artifact, overlay = _adapter_artifact()
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                artifact, index, overlay_wave=overlay
            )
        self.assertEqual("BOUND_EXECUTABLE_REQUEST", result["status"])
        self.assertEqual("OVERLAY_ALL_EXACT_FOCAL_EVENTS", result["anchor_source"])
        self.assertEqual(2, result["focal_anchor_count"])
        self.assertTrue(result["source_ref"]["overlay_verified"])

    def test_zero_focal_wave_uses_exact_team_event_span_as_build_anchor(self) -> None:
        artifact, overlay = _zero_focal_adapter_artifact()
        self.assertEqual([], artifact["expert_training_projection"]["samples"])
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                artifact, index, overlay_wave=overlay
            )
        self.assertEqual("BOUND_EXECUTABLE_REQUEST", result["status"])
        self.assertEqual(
            "OVERLAY_EXACT_TEAM_EVENT_SPAN_FALLBACK", result["anchor_source"]
        )
        self.assertEqual(1, result["focal_anchor_count"])

    def test_future_info_is_not_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(100, 10)])
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                _query([0, 99, 999, 3, 0]), index
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(
            ["NO_PRIOR_COMBATANT_INFO"],
            [row["code"] for row in result["coverage"]["blockers"]],
        )
        self.assertIsNone(result["request"])

    def test_build_change_inside_wave_rejects_one_static_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(
                Path(directory),
                [_record_for(100, 10, 100), _record_for(200, 20, 200)],
            )
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                _query([0, 150, 1, 3, 0], [0, 250, 1, 3, 0]), index
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(
            "HISTORICAL_BUILD_CHANGED_WITHIN_WAVE",
            result["coverage"]["blockers"][0]["code"],
        )

    def test_missing_equipment_is_visible_and_never_uses_caller_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(
                Path(directory), [_record_for(0, 0, complete=False)]
            )
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                _query([0, 0, 0, 3, 0], destination=_destination_request()), index
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertTrue(
            any(
                row["code"] == "HISTORICAL_EQUIPMENT_SLOT_NOT_EXACT"
                for row in result["coverage"]["blockers"]
            )
        )
        self.assertIsNone(result["request"])
        self.assertFalse(result["coverage"]["equipment_evidence_complete"])
        self.assertTrue(result["coverage"]["talent_semantics_exact"])
        self.assertNotIn(
            1,
            [
                row.get("item_id")
                for row in result["portable_character"]["equipment"]["slots"]
            ],
        )

    def test_runtime_gap_does_not_erase_exact_historical_build_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            catalog_path = Path(directory) / "catalog.jsonl.gz"
            with gzip.open(catalog_path, "rt", encoding="utf-8") as handle:
                segment = json.loads(next(handle))
            segment["coverage"]["runtime_executable"] = False
            segment["coverage"]["uncertainty"] = ["ITEM_EFFECT_NOT_COVERED:100"]
            with gzip.open(catalog_path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(segment) + "\n")
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                _query([0, 0, 0, 3, 0], destination=_destination_request()), index
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertTrue(result["coverage"]["equipment_evidence_complete"])
        self.assertTrue(result["coverage"]["talent_semantics_exact"])
        self.assertFalse(result["coverage"]["simulator_runtime_executable"])
        self.assertEqual(
            "HISTORICAL_BUILD_SIMULATOR_COVERAGE_INCOMPLETE",
            result["coverage"]["blockers"][0]["code"],
        )

    def test_instance_manifest_row_has_no_causal_wave_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(
                {"instance_id": INSTANCE, "selected_guid": GUID}, index
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(
            "NO_CAUSAL_FOCAL_EVENT_ANCHOR",
            result["coverage"]["blockers"][0]["code"],
        )

    def test_plain_wave_identity_is_preserved_but_not_used_as_a_time_anchor(self) -> None:
        identity = {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "encounter_ordinal": 12,
            "wave_ordinal": 3,
            "selected_guid": GUID,
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = _catalog_manifest(Path(directory), [_record_for(0, 0)])
            index = binding.load_historical_build_catalog_index_v1(manifest)
            result = binding.bind_old50_wave_historical_build_v1(identity, index)
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(identity, result["wave_identity"])
        self.assertEqual("WAVE_IDENTITY_HAS_NO_EVENTMETA", result["anchor_source"])
        self.assertEqual(
            "NO_CAUSAL_FOCAL_EVENT_ANCHOR",
            result["coverage"]["blockers"][0]["code"],
        )

    def test_one_row_manifest_audit_reports_real_request_coverage(self) -> None:
        artifact, _ = _adapter_artifact()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_manifest = _catalog_manifest(root / "catalog", [_record_for(0, 0)])
            adapter_root = root / "adapter"
            adapter_root.mkdir()
            partition = adapter_root / "instance.jsonl.gz"
            with gzip.open(partition, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(artifact, ensure_ascii=False) + "\n")
            manifest = {
                "schema": adapter.MATERIALIZED_MANIFEST_SCHEMA,
                "instances": [
                    {
                        "instance_id": INSTANCE,
                        "selected_guid": GUID,
                        "partition": {
                            "path": partition.name,
                            "record_schema": adapter.SCHEMA,
                            "record_count": 1,
                        },
                    }
                ],
                "summary": {"compiled_wave_count": 1},
            }
            manifest_path = adapter_root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = binding.audit_old50_adapter_build_coverage_v1(
                adapter_manifest_path=manifest_path,
                catalog_manifest_path=catalog_manifest,
            )
        self.assertEqual(1, result["summary"]["wave_count"])
        self.assertEqual(1, result["summary"]["executable_request_count"])
        self.assertEqual(0, result["summary"]["caller_base_request_used_as_build_count"])


if __name__ == "__main__":
    unittest.main()
