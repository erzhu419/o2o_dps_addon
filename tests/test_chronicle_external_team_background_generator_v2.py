from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_team_background_generator_v2 as generator_v2
from o2o_dps.chronicle_external_team_timeline_v2 import (
    build_external_team_timeline,
)
from o2o_dps.chronicle_external_team_wave_model_v2 import (
    build_external_team_wave_model,
)
from o2o_dps.chronicle_external_team_background_generator_v2 import (
    ChronicleExternalTeamBackgroundGeneratorV2Error,
    RETARGET_NEXT_ALIVE_CYCLIC,
    ExternalDynamicScheduleAdapterV1,
    TargetHealthHypothesisV1,
    adapt_draw_to_load_dynamic_v1,
    build_external_team_background_generator,
    draw_external_team_background_schedule,
    load_external_team_background_generator_manifest,
    validate_external_team_background_draw,
)
from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
)
from tests.test_chronicle_external_encounter_reconstruction_v2 import (
    PLAYER_1,
    PLAYER_2,
)
from tests.test_chronicle_external_team_timeline_v2 import (
    _complete_input_closure,
    _timeline_rows,
)
from tests.test_chronicle_external_team_wave_model_v2 import (
    _fake_receipt_audit,
    _multi_timeline,
    _write_cohort_receipt,
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_addressed(core: dict[str, object]) -> dict[str, object]:
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _raid_request(target_count: int) -> dict[str, object]:
    return {
        "raid": {"parties": []},
        "encounter": {
            "duration": 60,
            "targets": [
                {
                    "level": 63,
                    "mobType": "MobTypeHumanoid",
                    "stats": [0.0] * 34 + [77.0 + index],
                }
                for index in range(target_count)
            ],
        },
        "simOptions": {"iterations": 1, "randomSeed": 1},
    }


def _single_model(
    base: Path,
    *,
    negative_damage: bool = False,
    receipt_nontraining: bool = False,
) -> Path:
    rows = _timeline_rows()
    # Keep PLAYER_2 friendly, so its later DMG is an exact teammate event.  The
    # event intentionally remains after the historical DEAD marker.
    del rows[12]
    if negative_damage:
        rows[6]["value"] = -254
        rows[6]["official"]["message"]["amount"] = -254
    closure = _complete_input_closure(base, rows)
    timeline = build_external_team_timeline(
        normalization_manifest_path=closure["normalization"],
        admission_manifest_path=closure["admission"],
        reconstruction_manifest_path=closure["reconstruction"],
        output_directory=(
            base / "offline_data" / "derived" / "external_timeline" / "single"
        ),
    )
    timeline_manifest = json.loads(
        Path(timeline["manifest_path"]).read_text(encoding="utf-8")
    )
    receipt = _write_cohort_receipt(
        Path(timeline["manifest_path"]),
        nontraining_instance_ids=(
            {timeline_manifest["instance_order"][0]}
            if receipt_nontraining
            else None
        ),
    )
    model = build_external_team_wave_model(
        timeline_manifest_path=timeline["manifest_path"],
        cohort_receipt_path=receipt,
        output_directory=(
            base / "offline_data" / "derived" / "external_model" / "single"
        ),
    )
    return Path(model["manifest_path"])


def _multi_model(base: Path) -> Path:
    timeline = _multi_timeline(base)
    model = build_external_team_wave_model(
        timeline_manifest_path=timeline["manifest_path"],
        cohort_receipt_path=timeline["cohort_receipt_path"],
        output_directory=(
            base / "offline_data" / "derived" / "external_model" / "multi"
        ),
        workers=2,
    )
    return Path(model["manifest_path"])


def _blocks(manifest_path: Path) -> list[dict[str, object]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for entry in manifest["instances"]:
        partition = manifest_path.parent / entry["partition"]["path"]
        with gzip.open(partition, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def _model_rows(manifest_path: Path) -> list[dict[str, object]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for entry in manifest["instances"]:
        partition = manifest_path.parent / entry["partition"]["path"]
        with gzip.open(partition, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for child in value.values():
            result.update(_recursive_keys(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_recursive_keys(child))
        return result
    return set()


class ChronicleExternalTeamBackgroundGeneratorV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1,
            "audit_manifest_union_receipt",
            side_effect=_fake_receipt_audit,
        )
        self.receipt_audit = patcher.start()
        self.addCleanup(patcher.stop)

    def test_schedule_semantics_are_invariant_to_player_display_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model_path = _single_model(base)
            manifest = json.loads(model_path.read_text(encoding="utf-8"))
            component_by_node = {
                row["node_id"]: row["component_id"]
                for row in manifest["split_graph"]["node_to_component"]
            }
            original = _model_rows(model_path)[0]
            renamed = deepcopy(original)
            replacement_names = ["托尼牛", "桃姬儿"]
            for player, replacement in zip(
                renamed["players"], replacement_names, strict=True
            ):
                player_core = {
                    key: value
                    for key, value in player.items()
                    if key != "content_address"
                }
                player_core["player"]["name"] = replacement
                player.clear()
                player.update(_content_addressed(player_core))
            wave_core = {
                key: value
                for key, value in renamed.items()
                if key != "content_address"
            }
            renamed = _content_addressed(wave_core)
            first = generator_v2._compile_block(
                original,
                component_by_node=component_by_node,
                expected_contamination=original["raid_provenance"][
                    "contamination"
                ],
            )
            second = generator_v2._compile_block(
                renamed,
                component_by_node=component_by_node,
                expected_contamination=renamed["raid_provenance"][
                    "contamination"
                ],
            )
            self.assertEqual(
                first["schedule_semantic_sha256"],
                second["schedule_semantic_sha256"],
            )
            self.assertEqual(
                first["runtime_candidate_schedule"],
                second["runtime_candidate_schedule"],
            )
            self.assertEqual(first["component_id"], second["component_id"])
            self.assertEqual(
                first["contamination_lane"], second["contamination_lane"]
            )
            # Full provenance correctly notices that the source evidence changed.
            self.assertNotEqual(
                first["content_address"]["sha256"],
                second["content_address"]["sha256"],
            )

    def test_whole_wave_actor_target_schedule_loo_and_nonvoting_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base)
            result = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base
                    / "offline_data"
                    / "derived"
                    / "external_background"
                    / "single"
                ),
            )
            manifest, stable = load_external_team_background_generator_manifest(
                result["manifest_path"]
            )
            model_manifest = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(
                model_manifest["input_closure"]["cohort_receipt"],
                manifest["input_closure"]["cohort_receipt"],
            )
            self.assertEqual(Path(result["manifest_path"]), stable)
            self.assertFalse(manifest["scientific_boundaries"]["comparison_ready"])
            self.assertFalse(manifest["scientific_boundaries"]["voting_eligible"])
            self.assertFalse(
                manifest["identity_and_contamination_contract"]["player_name_used"]
            )

            first = _blocks(stable)[0]
            self.assertEqual(
                [100, 50, 10],
                [row["damage"] for row in first["runtime_candidate_schedule"]],
            )
            self.assertEqual(1, len(first["target_registry"]))
            self.assertEqual(0, first["target_registry"][0]["target_index"])
            self.assertEqual(1, len(first["dead_marker_diagnostics"]))
            self.assertEqual(2, len(first["nonruntime_damage_diagnostics"]))
            self.assertTrue(
                all(
                    row["runtime_eligible"] is False
                    for row in first["nonruntime_damage_diagnostics"]
                )
            )
            rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("Alice", rendered)
            self.assertNotIn("Bob", rendered)
            self.assertNotIn("托尼牛", rendered)
            self.assertNotIn("桃姬儿", rendered)

            component = first["component_id"]
            draw = draw_external_team_background_schedule(
                generator_manifest_path=stable,
                component_id=component,
                focal_player_guid=PLAYER_1,
                seed=20260911,
                draw_index=0,
            )
            validate_external_team_background_draw(draw)
            self.assertEqual([10], [row["damage"] for row in draw["schedule"]])
            self.assertEqual(
                ["DIRECT_FRIENDLY_PLAYER", "EXACT_OFFICIAL_OWNER"],
                sorted(
                    row["attribution_kind"]
                    for row in draw["focal_leave_one_out"]["excluded_events"]
                ),
            )
            self.assertEqual(
                150, draw["focal_leave_one_out"]["excluded_damage"]
            )
            # Historical DEAD is diagnostic only: it does not pre-cancel a
            # later official DMG event. Runtime death receipts own cancellation.
            self.assertGreater(
                draw["schedule"][0]["time_ms"],
                first["dead_marker_diagnostics"][0]["time_ms"],
            )
            self.assertNotIn(
                "descriptive_outcome", _recursive_keys(draw["schedule"])
            )
            self.assertNotIn("state_before", _recursive_keys(draw["schedule"]))
            self.assertTrue(draw["source_block"]["whole_wave_selected"])

    def test_negative_dmg_is_signed_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base, negative_damage=True)
            result = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "negative"
                ),
            )
            first = _blocks(Path(result["manifest_path"]))[0]
            self.assertEqual(
                [-254],
                [
                    row["signed_amount"]
                    for row in first["negative_damage_diagnostics"]
                ],
            )
            self.assertEqual(
                [50, 10],
                [row["damage"] for row in first["runtime_candidate_schedule"]],
            )
            self.assertNotIn(
                254, [row["damage"] for row in first["runtime_candidate_schedule"]]
            )
            self.assertEqual(0, first["summary"]["negative_damage_added"])

    def test_dynamic_adapter_requires_external_health_hypothesis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base)
            built = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "adapter"
                ),
            )
            first = _blocks(Path(built["manifest_path"]))[0]
            draw = draw_external_team_background_schedule(
                generator_manifest_path=built["manifest_path"],
                component_id=first["component_id"],
                focal_player_guid=PLAYER_2,
                seed=17,
            )
            hypothesis = TargetHealthHypothesisV1(
                hypothesis_id="fixture-health-v1",
                provenance={
                    "kind": "EXTERNAL_TEST_HYPOTHESIS",
                    "source": "fixture only",
                },
                target_health_by_index={0: 1000.0},
            )
            request = _raid_request(1)
            original_request = deepcopy(request)
            adapter = adapt_draw_to_load_dynamic_v1(
                draw=draw,
                request=request,
                seed=17,
                target_health_hypothesis=hypothesis,
                retarget_mode=RETARGET_NEXT_ALIVE_CYCLIC,
            )
            self.assertIsInstance(adapter, ExternalDynamicScheduleAdapterV1)
            wire = adapter.wire_request
            self.assertEqual("load_dynamic_v1", wire["command"])
            self.assertEqual(
                "BACKGROUND_BEFORE_CANDIDATE",
                wire["dynamic"]["same_timestamp_order"],
            )
            self.assertEqual(
                sorted(
                    wire["dynamic"]["background_damage_events"],
                    key=lambda row: (row["time_ms"], row["schedule_index"]),
                ),
                wire["dynamic"]["background_damage_events"],
            )
            self.assertEqual(
                list(range(len(wire["dynamic"]["background_damage_events"]))),
                [
                    row["schedule_index"]
                    for row in wire["dynamic"]["background_damage_events"]
                ],
            )
            self.assertEqual(
                [{"target_index": 0, "health": 1000.0}],
                wire["dynamic"]["target_health"],
            )
            self.assertIs(wire["request"]["encounter"]["useHealth"], True)
            self.assertEqual(original_request, request)
            self.assertEqual(
                struct.pack(">d", 1000.0),
                struct.pack(
                    ">d", wire["request"]["encounter"]["targets"][0]["stats"][34]
                ),
            )
            self.assertNotIn("hypothesis_id", wire["dynamic"])
            self.assertEqual(
                "fixture-health-v1",
                adapter.provenance["target_health_hypothesis"]["hypothesis_id"],
            )
            self.assertFalse(adapter.comparison_ready)
            self.assertEqual(
                wire["dynamic"]["content_sha256"],
                adapter.dynamic_config.content_sha256,
            )
            self.assertEqual(
                hashlib.sha256(_canonical_bytes(wire["request"])).hexdigest(),
                adapter.provenance["request_sha256"],
            )
            typed_config = DynamicTeamBackgroundConfigV1(
                target_health=tuple(
                    DynamicTargetHealthV1(row["target_index"], row["health"])
                    for row in wire["dynamic"]["target_health"]
                ),
                background_damage_events=tuple(
                    BackgroundDamageEventV1(
                        row["schedule_index"],
                        row["time_ms"],
                        row["target_index"],
                        row["event_id"],
                        row["damage"],
                    )
                    for row in wire["dynamic"]["background_damage_events"]
                ),
                same_timestamp_order=wire["dynamic"]["same_timestamp_order"],
                retarget_mode=wire["dynamic"]["retarget_mode"],
            )
            self.assertEqual(
                typed_config.content_sha256,
                wire["dynamic"]["content_sha256"],
            )
            self.assertEqual(
                "12ea50a52e1302de7167da3dfc82b7edf736647d25134c399e0aaa043d310cc4",
                wire["dynamic"]["content_sha256"],
            )
            self.assertEqual(
                "IEEE754_BINARY64_HEX",
                adapter.provenance["dynamic_digest_float_encoding"],
            )
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "encounter.targets count",
            ):
                adapt_draw_to_load_dynamic_v1(
                    draw=draw,
                    request=_raid_request(2),
                    seed=17,
                    target_health_hypothesis=hypothesis,
                )
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "complete target index coverage",
            ):
                adapt_draw_to_load_dynamic_v1(
                    draw=draw,
                    request={},
                    seed=17,
                    target_health_hypothesis=TargetHealthHypothesisV1(
                        hypothesis_id="missing",
                        provenance={"kind": "MISSING"},
                        target_health_by_index={},
                    ),
                )

    def test_serial_and_parallel_are_byte_deterministic_and_component_whole_wave(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _multi_model(base)
            serial = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "serial"
                ),
                workers=1,
            )
            parallel = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "parallel"
                ),
                workers=2,
            )
            self.assertEqual(serial["content_sha256"], parallel["content_sha256"])
            self.assertEqual(
                Path(serial["manifest_path"]).read_bytes(),
                Path(parallel["manifest_path"]).read_bytes(),
            )
            manifest, _ = load_external_team_background_generator_manifest(
                serial["manifest_path"]
            )
            block_descriptors = {
                block["block_content_sha256"]: block
                for component in manifest["component_pools"]
                for block in component["blocks"]
            }
            for component in manifest["component_pools"]:
                for seed in range(8):
                    draw = draw_external_team_background_schedule(
                        generator_manifest_path=serial["manifest_path"],
                        component_id=component["component_id"],
                        focal_player_guid=PLAYER_1,
                        seed=seed,
                    )
                    selected = block_descriptors[
                        draw["source_block"]["block_content_sha256"]
                    ]
                    self.assertEqual(component["component_id"], selected["component_id"])
                    self.assertEqual(
                        selected["wave"], draw["source_block"]["wave"]
                    )
                    repeated = draw_external_team_background_schedule(
                        generator_manifest_path=serial["manifest_path"],
                        component_id=component["component_id"],
                        focal_player_guid=PLAYER_1,
                        seed=seed,
                    )
                    self.assertEqual(draw, repeated)
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "component",
            ):
                draw_external_team_background_schedule(
                    generator_manifest_path=serial["manifest_path"],
                    component_id="f" * 64,
                    focal_player_guid=PLAYER_1,
                    seed=1,
                )

    def test_raw_clean_receipt_nontraining_mask_propagates_and_cannot_draw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base, receipt_nontraining=True)
            result = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base
                    / "offline_data"
                    / "derived"
                    / "background"
                    / "receipt_nontraining"
                ),
            )
            manifest, stable = load_external_team_background_generator_manifest(
                result["manifest_path"]
            )
            self.assertEqual(
                0, manifest["summary"]["training_candidate_instance_count"]
            )
            self.assertEqual(
                1, manifest["summary"]["descriptive_nontraining_instance_count"]
            )
            receipt_sha = manifest["input_closure"]["cohort_receipt"][
                "content_sha256"
            ]
            blocks = _blocks(stable)
            self.assertTrue(blocks)
            for block in blocks:
                lane = block["contamination_lane"]
                self.assertTrue(lane["raw_label_candidate_filter_passed"])
                self.assertFalse(lane["training_candidate"])
                self.assertEqual("DESCRIPTIVE_NONTRAINING", lane["cohort_assignment"])
                self.assertEqual(receipt_sha, lane["cohort_receipt_content_sha256"])
            component_id = blocks[0]["component_id"]
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "no clean whole-wave block",
            ):
                draw_external_team_background_schedule(
                    generator_manifest_path=stable,
                    component_id=component_id,
                    focal_player_guid=PLAYER_1,
                    seed=1,
                )

    def test_old_revision_stale_receipt_binding_and_replay_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base)
            result = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "fail_closed"
                ),
            )
            stable = Path(result["manifest_path"])
            original = json.loads(stable.read_text(encoding="utf-8"))

            old_core = {
                key: deepcopy(value)
                for key, value in original.items()
                if key != "content_address"
            }
            old_core["implementation_revision"] = "v2.1_pre_receipt_binding"
            old_revision = _content_addressed(old_core)
            old_path = stable.parent / "old-revision.manifest.json"
            old_path.write_bytes(_canonical_bytes(old_revision) + b"\n")
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "unsupported background manifest",
            ):
                load_external_team_background_generator_manifest(old_path)

            address_tamper = deepcopy(original)
            address_tamper["content_address"]["unexpected"] = True
            address_tamper_path = stable.parent / "address-tamper.manifest.json"
            address_tamper_path.write_bytes(_canonical_bytes(address_tamper) + b"\n")
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "unsupported content-address contract",
            ):
                load_external_team_background_generator_manifest(address_tamper_path)

            with mock.patch.object(
                union_v1,
                "audit_manifest_union_receipt",
                side_effect=union_v1.ChronicleExternalManifestUnionError(
                    "synthetic bound-source tamper"
                ),
            ):
                with self.assertRaisesRegex(
                    ChronicleExternalTeamBackgroundGeneratorV2Error,
                    "strict full-source replay failed.*bound-source tamper",
                ):
                    load_external_team_background_generator_manifest(stable)

            stale_core = {
                key: deepcopy(value)
                for key, value in original.items()
                if key != "content_address"
            }
            stale_core["input_closure"]["cohort_receipt"]["file_sha256"] = "e" * 64
            stale = _content_addressed(stale_core)
            stale_payload = _canonical_bytes(stale) + b"\n"
            stale_addressed = stable.with_name(
                "chronicle_external_team_background_generator_v2."
                f"{stale['content_address']['sha256']}.manifest.json"
            )
            stable.write_bytes(stale_payload)
            stale_addressed.write_bytes(stale_payload)
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "cohort receipt binding differs",
            ):
                load_external_team_background_generator_manifest(stable)

    def test_input_partition_tamper_and_output_path_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = _single_model(base)
            model_manifest = json.loads(model.read_text(encoding="utf-8"))
            model_partition = model.parent / model_manifest["instances"][0][
                "partition"
            ]["path"]
            original = model_partition.read_bytes()
            model_partition.write_bytes(original + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "hash|size|partition",
            ):
                build_external_team_background_generator(
                    team_model_manifest_path=model,
                    output_directory=(
                        base / "offline_data" / "derived" / "background" / "tamper"
                    ),
                )
            model_partition.write_bytes(original)

            result = build_external_team_background_generator(
                team_model_manifest_path=model,
                output_directory=(
                    base / "offline_data" / "derived" / "background" / "escape"
                ),
            )
            manifest_path = Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            old_addressed = manifest_path.with_name(
                "chronicle_external_team_background_generator_v2."
                f"{manifest['content_address']['sha256']}.manifest.json"
            )
            entry_core = {
                key: value
                for key, value in manifest["instances"][0].items()
                if key != "content_address"
            }
            entry_core["partition"]["path"] = "../escaped.jsonl.gz"
            manifest["instances"][0] = _content_addressed(entry_core)
            manifest_core = {
                key: value for key, value in manifest.items() if key != "content_address"
            }
            rewritten = _content_addressed(manifest_core)
            payload = _canonical_bytes(rewritten) + b"\n"
            new_addressed = manifest_path.with_name(
                "chronicle_external_team_background_generator_v2."
                f"{rewritten['content_address']['sha256']}.manifest.json"
            )
            old_addressed.unlink()
            manifest_path.write_bytes(payload)
            new_addressed.write_bytes(payload)
            with self.assertRaisesRegex(
                ChronicleExternalTeamBackgroundGeneratorV2Error,
                "safe relative path|stay under",
            ):
                load_external_team_background_generator_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
