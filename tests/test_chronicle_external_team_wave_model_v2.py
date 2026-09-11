from __future__ import annotations

import gzip
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_external_team_timeline_v2 import (
    build_external_team_timeline,
)
from o2o_dps.chronicle_external_team_wave_model_v2 import (
    ChronicleExternalTeamWaveModelV2Error,
    PARTITION_RECORD_SCHEMA,
    SCHEMA,
    STATUS,
    _contamination,
    build_external_team_wave_model,
    load_external_team_wave_model_manifest,
)
from tests.test_chronicle_external_encounter_reconstruction_v2 import (
    ENCOUNTER_2,
    MOB_1,
    MOB_2,
    OBJECT,
    PLAYER_1,
    PLAYER_2,
)
from tests.test_chronicle_external_team_timeline_v2 import (
    _action_row,
    _complete_input_closure,
    _complete_multi_instance_closure,
    _timeline_rows,
)


def _single_timeline(
    base: Path, rows: list[dict[str, object]] | None = None
) -> dict[str, object]:
    closure = _complete_input_closure(base, rows or _timeline_rows())
    output = base / "offline_data" / "derived" / "external_timeline" / "single"
    return build_external_team_timeline(
        normalization_manifest_path=closure["normalization"],
        admission_manifest_path=closure["admission"],
        reconstruction_manifest_path=closure["reconstruction"],
        output_directory=output,
    )


def _multi_timeline(base: Path) -> dict[str, object]:
    closure = _complete_multi_instance_closure(base)
    output = base / "offline_data" / "derived" / "external_timeline" / "multi"
    return build_external_team_timeline(
        normalization_manifest_path=closure["normalization"],
        admission_manifest_path=closure["admission"],
        reconstruction_manifest_path=closure["reconstruction"],
        output_directory=output,
        workers=2,
    )


def _model_rows(manifest_path: Path) -> list[dict[str, object]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for entry in manifest["instances"]:
        path = manifest_path.parent / entry["partition"]["path"]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for child in value.values():
            result.update(_all_keys(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_all_keys(child))
        return result
    return set()


class ChronicleExternalTeamWaveModelV2Tests(unittest.TestCase):
    def test_raid_contamination_is_invariant_to_named_player_examples(self) -> None:
        provenance = {
            "temporal_and_guild_provenance": {
                "started_at": "2026-09-03T20:48:06.919+08:00",
                "contamination": {
                    "label": "POSTFIX_KNOWN_CLEAN",
                    "time_field": "started_at",
                    "uploaded_at_used": False,
                    "guild_context": "南北",
                    "guild_evidence": "metadata.guild.name",
                },
            }
        }
        first = deepcopy(provenance)
        first["irrelevant_player_name"] = "托尼牛"
        second = deepcopy(provenance)
        second["irrelevant_player_name"] = "桃姬儿"
        self.assertEqual(_contamination(first), _contamination(second))
        resolved = _contamination(first)
        self.assertEqual(resolved["label"], "POSTFIX_KNOWN_CLEAN")
        self.assertFalse(resolved["player_name_used"])
        self.assertTrue(resolved["candidate_filter_passed"])

    def test_signed_negative_damage_is_context_only_and_never_damage_loo_or_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            rows = _timeline_rows()
            rows[6]["value"] = -254
            rows[6]["official"]["message"]["amount"] = -254
            timeline = _single_timeline(base, rows)
            output = base / "offline_data" / "derived" / "external_model" / "negative"
            result = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            manifest, _ = load_external_team_wave_model_manifest(
                result["manifest_path"]
            )
            first = _model_rows(Path(result["manifest_path"]))[0]
            diagnostics = [
                row
                for row in first["exact_trace"]
                if row["trace_kind"] == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT"
            ]
            self.assertEqual(len(diagnostics), 1)
            diagnostic = diagnostics[0]
            self.assertIsNone(diagnostic["player_guid"])
            self.assertEqual(
                diagnostic["event"]["attribution"]["player_guid"], PLAYER_1
            )
            self.assertEqual(
                diagnostic["event"]["attribution"]["attribution_kind"],
                "DIRECT_FRIENDLY_PLAYER",
            )
            self.assertNotIn("damage", diagnostic["event"])
            self.assertEqual(
                diagnostic["event"]["negative_damage_diagnostic"]["signed_amount"],
                -254,
            )
            all_transitions = [
                transition
                for player in first["players"]
                for transition in player["prefix_transitions"]
            ] + first["unattributed_episode"]["prefix_transitions"]
            all_outcome_indices = [
                index
                for player in first["players"]
                for index in player["outcome_context_trace_indices"]
            ] + first["unattributed_episode"]["outcome_context_trace_indices"]
            self.assertFalse(
                any(
                    transition["trace_index"] == diagnostic["trace_index"]
                    for transition in all_transitions
                )
            )
            self.assertNotIn(diagnostic["trace_index"], all_outcome_indices)
            alice = next(
                player
                for player in first["players"]
                if player["player"]["guid"] == PLAYER_1
            )
            self.assertEqual(alice["summary"]["damage_amount"], 80)
            self.assertEqual(
                alice["leave_one_player_out_background"][
                    "excluded_focal_damage_amount"
                ],
                80,
            )
            self.assertEqual(
                alice["leave_one_player_out_background"]["included_damage_amount"],
                30,
            )
            later_action = next(
                transition
                for transition in first["unattributed_episode"]["prefix_transitions"]
                if transition["current_event_label"]["spell"]["name"]
                == "Hostile Player Action"
            )
            self.assertEqual(
                later_action["state_before"]["prefix_damage_amount_total"], 110
            )
            self.assertEqual(first["summary"]["damage_amount"], 110)
            self.assertEqual(
                first["summary"]["negative_damage_diagnostic_count"], 1
            )
            self.assertEqual(
                first["summary"]["negative_damage_signed_amount_excluded"], -254
            )
            self.assertEqual(
                manifest["summary"]["negative_damage_absolute_amount_excluded"],
                254,
            )

    def test_build_is_content_addressed_deterministic_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            timeline = _single_timeline(base)
            output = base / "offline_data" / "derived" / "external_model" / "single"
            first = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            first_bytes = Path(first["manifest_path"]).read_bytes()
            second = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first_bytes, Path(second["manifest_path"]).read_bytes())
            manifest, resolved = load_external_team_wave_model_manifest(
                first["manifest_path"]
            )
            self.assertEqual(resolved, Path(first["manifest_path"]))
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(manifest["summary"]["instance_count"], 1)
            self.assertEqual(manifest["summary"]["wave_count"], 2)
            self.assertFalse(manifest["scientific_boundaries"]["comparison_authorized"])
            self.assertFalse(manifest["scientific_boundaries"]["policy_training_authorized"])
            self.assertFalse(
                manifest["spec_and_contamination_contract"][
                    "named_player_blacklist_or_weighting_allowed"
                ]
            )
            self.assertEqual(
                manifest["spec_and_contamination_contract"][
                    "contamination_rules"
                ]["missing_guild_or_started_at_evidence"],
                "UNKNOWN_NONVOTING",
            )
            self.assertEqual(
                first_bytes,
                Path(first["content_addressed_manifest_path"]).read_bytes(),
            )

    def test_exact_trace_prefix_loo_spec_and_nonvoting_target_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            timeline = _single_timeline(base)
            output = base / "offline_data" / "derived" / "external_model" / "semantics"
            result = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            first = _model_rows(Path(result["manifest_path"]))[0]
            self.assertEqual(first["schema"], PARTITION_RECORD_SCHEMA)
            trace = first["exact_trace"]
            self.assertEqual(
                [row["trace_index"] for row in trace], list(range(len(trace)))
            )
            death = next(row for row in trace if row["trace_kind"] == "DEATH_MARKER")
            self.assertNotIn("damage", death["event"])
            self.assertEqual(first["summary"]["dead_marker_damage_added"], 0)
            action_trace_count = sum(
                1
                for row in trace
                if row["trace_kind"]
                in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}
                and row["event"]["event_type"] in {"START", "GO", "FAIL"}
            )
            self.assertEqual(first["summary"]["transition_count"], action_trace_count)
            object_target = next(
                row
                for row in trace
                if row["trace_kind"] == "EXACT_PLAYER_EVENT"
                and row["event"]["target"]["guid"] == OBJECT
            )
            self.assertFalse(object_target["event"]["target"]["voting_enemy_target"])
            self.assertTrue(
                object_target["event"]["target"][
                    "hostile_object_preserved_nonvoting"
                ]
            )

            alice = next(
                player for player in first["players"] if player["player"]["guid"] == PLAYER_1
            )
            bob = next(
                player for player in first["players"] if player["player"]["guid"] == PLAYER_2
            )
            self.assertEqual(alice["player"]["class"], "WARRIOR")
            self.assertEqual(alice["player"]["race"], "Human")
            self.assertEqual(
                alice["warrior_spec_lane"]["partition_key"], "WARRIOR_FURY"
            )
            self.assertEqual(
                bob["warrior_spec_lane"]["partition_key"],
                "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING",
            )
            self.assertFalse(bob["warrior_spec_lane"]["voting_authorized"])
            self.assertEqual(
                alice["contamination_lane"]["label"], "POSTFIX_KNOWN_CLEAN"
            )
            self.assertFalse(alice["contamination_lane"]["player_name_used"])
            self.assertEqual(
                alice["leave_one_player_out_background"][
                    "excluded_focal_damage_amount"
                ],
                180,
            )
            self.assertEqual(
                alice["leave_one_player_out_background"]["included_damage_amount"],
                30,
            )
            self.assertEqual(
                alice["leave_one_player_out_background"][
                    "unattributed_branch"
                ]["damage_amount"],
                30,
            )
            first_transition = alice["prefix_transitions"][0]
            self.assertEqual(first_transition["state_before"]["prefix_damage_amount_total"], 0)
            self.assertEqual(
                first_transition["state_before"]["prefix_trace_event_count"],
                first_transition["trace_index"],
            )
            forbidden = {
                "death_markers",
                "descriptive_outcome",
                "event_accounting",
                "final_totals",
                "future_events",
                "reconstruction_binding",
                "summary",
                "wave_summary",
            }
            for player in first["players"]:
                self.assertTrue(
                    all(
                        transition["current_event_label"]["event_type"]
                        in {"START", "GO", "FAIL"}
                        for transition in player["prefix_transitions"]
                    )
                )
                self.assertTrue(
                    all(
                        trace[index]["event"]["event_type"] in {"DMG", "HEAL"}
                        for index in player["outcome_context_trace_indices"]
                    )
                )
                for transition in player["prefix_transitions"]:
                    self.assertTrue(
                        forbidden.isdisjoint(_all_keys(transition["state_before"]))
                    )
            hostile_action = next(
                transition
                for transition in first["unattributed_episode"]["prefix_transitions"]
                if transition["current_event_label"]["spell"]["name"]
                == "Hostile Player Action"
            )
            target_state = hostile_action["state_before"][
                "observed_target_prefix_state_ref"
            ]
            self.assertEqual(target_state["observed_dead_target_count"], 1)
            self.assertRegex(target_state["content_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                hostile_action["state_before"]["prefix_damage_amount_total"], 210
            )

    def test_later_classification_does_not_backfill_earlier_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            rows = _timeline_rows()
            rows.append(
                _action_row(
                    4,
                    encounter=ENCOUNTER_2,
                    ordinal=1,
                    origin=int(rows[-1]["first_timestamp_ms"]),
                    event_type="GO",
                    source_guid=PLAYER_1,
                    target_guid=MOB_2,
                    spell_id=2001,
                    spell_name="Action After Outcomes",
                )
            )
            timeline = _single_timeline(base, rows)
            output = base / "offline_data" / "derived" / "external_model" / "prefix"
            result = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            second = _model_rows(Path(result["manifest_path"]))[1]
            alice = next(
                player for player in second["players"] if player["player"]["guid"] == PLAYER_1
            )
            self.assertEqual(len(alice["prefix_transitions"]), 1)
            self.assertEqual(
                len(second["unattributed_episode"]["prefix_transitions"]), 0
            )
            self.assertEqual(
                len(
                    second["unattributed_episode"][
                        "outcome_context_trace_indices"
                    ]
                ),
                1,
            )
            transition = alice["prefix_transitions"][0]
            self.assertEqual(
                transition["state_before"]["prefix_damage_amount_total"], 100
            )
            self.assertEqual(
                transition["state_before"]["leave_one_player_out_background_before"][
                    "included_unattributed_damage"
                ],
                40,
            )
            self.assertFalse(
                second["scientific_boundaries"][
                    "future_classification_backfill_allowed"
                ]
            )

    def test_multi_instance_serial_parallel_bytes_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            timeline = _multi_timeline(base)
            serial_output = (
                base / "offline_data" / "derived" / "external_model" / "serial"
            )
            parallel_output = (
                base / "offline_data" / "derived" / "external_model" / "parallel"
            )
            serial = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=serial_output,
                workers=1,
            )
            parallel = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=parallel_output,
                workers=2,
            )
            self.assertEqual(serial["content_sha256"], parallel["content_sha256"])
            self.assertEqual(serial["workers_used"], 1)
            self.assertEqual(parallel["workers_used"], 2)
            self.assertEqual(
                Path(serial["manifest_path"]).read_bytes(),
                Path(parallel["manifest_path"]).read_bytes(),
            )
            manifest = json.loads(Path(serial["manifest_path"]).read_text("utf-8"))
            self.assertEqual(manifest["instance_order"], sorted(manifest["instance_order"]))
            self.assertEqual(manifest["summary"]["instance_count"], 2)
            for entry in manifest["instances"]:
                relative = entry["partition"]["path"]
                self.assertEqual(
                    (serial_output / relative).read_bytes(),
                    (parallel_output / relative).read_bytes(),
                )

    def test_tampered_output_partition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            timeline = _single_timeline(base)
            output = base / "offline_data" / "derived" / "external_model" / "tamper"
            result = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                output_directory=output,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            partition = output / manifest["instances"][0]["partition"]["path"]
            partition.write_bytes(partition.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalTeamWaveModelV2Error,
                "compressed size mismatch",
            ):
                load_external_team_wave_model_manifest(result["manifest_path"])

    def test_workers_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            timeline = _single_timeline(base)
            output = base / "offline_data" / "derived" / "external_model" / "workers"
            with self.assertRaisesRegex(
                ChronicleExternalTeamWaveModelV2Error, "workers must"
            ):
                build_external_team_wave_model(
                    timeline_manifest_path=timeline["manifest_path"],
                    output_directory=output,
                    workers=0,
                )


if __name__ == "__main__":
    unittest.main()
