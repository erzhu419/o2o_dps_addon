from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_team_background_generator_v2 as background_v2
from o2o_dps import chronicle_external_team_wave_model_v2 as model_v2
from o2o_dps import chronicle_old50_exact_fury_slot_overlay_v1 as overlay
from o2o_dps import chronicle_old50_warrior_slot_substitution_v1 as slots_v1
from o2o_dps import chronicle_old50_warrior_slot_substitution_v2 as selector_v2


INSTANCE = "00000000-0000-0000-0000-000000000001"
ENCOUNTER = "00000000-0000-0000-0000-000000000002"
GUID = "0x00000000000000F1"
TEAMMATE = "0x00000000000000E1"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"
WAVE_ORDINAL = 3
WAVE_ID = f"{ENCOUNTER}:external-v2-wave:{WAVE_ORDINAL}"


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _selection(*, pdf: bool) -> dict:
    lane = selector_v2.PDF_LANE if pdf else selector_v2.GENERIC_LANE
    semantics = {
        "leaderboard_membership_used_for_selection": pdf,
        "leaderboard_dps_rank_used_for_selection": pdf,
        "outcome_conditioned_discovery": pdf,
        "same_instance_ranking_dps_values_used": False,
        "generic_selection_outcome_free": not pdf,
        "purpose": (
            "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY"
            if pdf
            else "OUTCOME_FREE_EXACT_FURY_IDENTITY_DISCOVERY_ONLY"
        ),
        "heldout_performance_evidence_eligible": False,
        "comparison_outcome_eligible": False,
    }
    return slots_v1._content_addressed(
        {
            "instance_id": INSTANCE,
            "selected_guid": GUID,
            "selection_lane": lane,
            "selection_rule": (
                selector_v2.PDF_RULE if pdf else selector_v2.GENERIC_RULE
            ),
            "selection_semantics": semantics,
            "selected_stage5_compatibility": {
                "eligible": True,
                "relationship": (
                    "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_FURY_PROMOTION"
                ),
                "arms_or_conflict_overwritten": False,
            },
            "accepted_wave_count": 1,
        }
    )


def _target(guid: str) -> dict:
    return {
        "guid": guid,
        "lane": "HOSTILE_CREATURE",
        "voting_enemy_target": True,
    }


def _trace(index: int, event_type: str, target_guid: str) -> dict:
    event = {
        "event_type": event_type,
        "target": _target(target_guid),
    }
    return {
        "trace_index": index,
        "order_key": [0, 0, 0, 0, index],
        "trace_kind": "EXACT_PLAYER_EVENT",
        "player_guid": GUID,
        "event": event,
    }


def _player() -> dict:
    state_before = {
        "cutoff_semantics": "strictly before current exact EventMeta order_key",
        "cutoff_exclusive_order_key": [0, 0, 0, 0, 0],
        "prefix_trace_event_count": 0,
        "prefix_trace_exclusive_index": 0,
        "classification_prefix_state_ref": {
            "content_sha256": _sha("classification"),
        },
        "observed_target_prefix_state_ref": {
            "content_sha256": _sha("target"),
        },
        "actor_recent_exact_trace_indices": [],
    }
    return model_v2._content_addressed(
        {
            "player": {"guid": GUID, "class": "Warrior", "race": None},
            "warrior_spec_lane": {
                "partition_key": "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING",
                "observed_spec": "Unknown",
                "evidence_status": "UNKNOWN_NONVOTING",
                "exact_guid_match": False,
                "fury_or_arms_conflict_free_observation": False,
                "voting_authorized": False,
                "source_evidence": {
                    "player_spec": "Unknown",
                    "field_conflicts": {},
                },
            },
            "eligibility_observation": {
                "historical_fury_candidate_filter_passed": False,
                "voting_authorized": False,
            },
            "component_membership": {
                "instance_node_id": "instance-node",
                "guild_node_ids": ["guild-node"],
                "player_node_id": "player-node",
                "required_split_unit": "connected component",
                "row_random_split_allowed": False,
            },
            "exact_trace_indices": [0, 1],
            "outcome_context_trace_indices": [1],
            "prefix_transitions": [
                {
                    "trace_index": 0,
                    "order_key": [0, 0, 0, 0, 0],
                    "state_before": state_before,
                    "current_event_label": {
                        "event_type": "START",
                        "learning_role": "OBSERVED_ACTION_LABEL",
                        "target": _target(TARGET_A),
                    },
                    "feature_cutoff_is_strict_prefix": True,
                    "current_event_present_in_state_before": False,
                    "future_outcomes_in_state_before": False,
                }
            ],
            "leave_one_player_out_background": {
                "focal_player_guid": GUID,
                "filter_contract": {
                    "excluded_attribution_kinds": list(slots_v1.ATTRIBUTION_KINDS),
                    "exact_player_guid_match_required": True,
                    "name_or_guid_suffix_inference_used": False,
                },
                "excluded_focal_event_count": 10,
                "excluded_focal_damage_amount": 1000,
            },
        }
    )


def _stage5_wave() -> dict:
    exact_trace = [_trace(0, "START", TARGET_A), _trace(1, "DMG", TARGET_A)]
    return model_v2._content_addressed(
        {
            "schema": model_v2.PARTITION_RECORD_SCHEMA,
            "implementation_revision": model_v2.IMPLEMENTATION_REVISION,
            "status": model_v2.STATUS,
            "wave": {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "encounter_ordinal": 1,
                "wave_id": WAVE_ID,
                "wave_ordinal": WAVE_ORDINAL,
            },
            "exact_trace": exact_trace,
            "players": [_player()],
        }
    )


def _schedule_event(
    index: int, actor: str, kind: str, target_guid: str, target_index: int, damage: int
) -> dict:
    return {
        "time_ms": index * 10,
        "target_index": target_index,
        "target_guid": target_guid,
        "event_id": f"event-{index}",
        "damage": damage,
        "actor_player_guid": actor,
        "attribution_kind": kind,
        "source_guid": (
            actor if kind == "DIRECT_FRIENDLY_PLAYER" else f"source-{index}"
        ),
        "source_lane": "FRIENDLY_PLAYER",
        "trace_index": index,
        "source_eventmeta_order_key": [0, 0, 0, 0, index],
        "official_message_sha256": _sha(f"message-{index}"),
        "evidence_status": "POSITIVE_OFFICIAL_DMG_EXACT_PLAYER_AND_TARGET",
        "runtime_eligible": True,
    }


def _stage6_block(stage5: dict) -> dict:
    runtime = [
        _schedule_event(1, GUID, "DIRECT_FRIENDLY_PLAYER", TARGET_A, 0, 10),
        _schedule_event(2, GUID, "EXACT_OFFICIAL_OWNER", TARGET_B, 1, 20),
        _schedule_event(3, GUID, "EXACT_OFFICIAL_CONTROLLER", TARGET_A, 0, 30),
        _schedule_event(4, TEAMMATE, "DIRECT_FRIENDLY_PLAYER", TARGET_A, 0, 40),
    ]
    return background_v2._content_addressed(
        {
            "schema": background_v2.PARTITION_RECORD_SCHEMA,
            "implementation_revision": background_v2.IMPLEMENTATION_REVISION,
            "record_type": "component_whole_wave_background_block",
            "status": background_v2.STATUS,
            "wave": deepcopy(stage5["wave"]),
            "component_id": _sha("component"),
            "source_model": {
                "wave_content_sha256": stage5["content_address"]["sha256"],
                "exact_trace_content_sha256": slots_v1._sha256(
                    stage5["exact_trace"]
                ),
            },
            "roster_player_guids": sorted([GUID, TEAMMATE]),
            "target_registry": [
                {"target_guid": TARGET_A, "target_index": 0},
                {"target_guid": TARGET_B, "target_index": 1},
            ],
            "runtime_candidate_schedule": runtime,
            "schedule_semantic_sha256": _sha("schedule"),
        }
    )


def _overlap(block: dict) -> dict:
    return {
        "classification": "EXACT",
        "key": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "wave_ordinal": WAVE_ORDINAL,
        },
        "target_join": {
            "capsule_target_guids": [TARGET_B, TARGET_A],
            "stage6_target_guids": [TARGET_A, TARGET_B],
            "guid_relation": "EQUAL",
            "index_remap": [
                {
                    "target_guid": TARGET_B,
                    "stage6_target_index": 1,
                    "capsule_target_index": 0,
                },
                {
                    "target_guid": TARGET_A,
                    "stage6_target_index": 0,
                    "capsule_target_index": 1,
                },
            ],
        },
        "component_join": {
            "old50_component_id": "old-component",
            "stage6_component_id": _sha("component"),
            "relation": "OVERLAP_PROJECTION_EXACT",
            "split_authority": "EQUIVALENT_ON_OVERLAP",
        },
        "source_bindings": {
            "stage6_block_content_sha256": block["content_address"]["sha256"],
        },
    }


def _source_bindings() -> dict:
    return {
        "selector_v2_content_sha256": _sha("selector-v2"),
        "selector_v2_file_sha256": _sha("selector-v2-file"),
        "selector_v1_content_sha256": _sha("selector-v1"),
        "overlap_audit_content_sha256": _sha("overlap"),
        "stage5_manifest_content_sha256": _sha("stage5"),
        "stage5_instance_content_sha256": _sha("stage5-instance"),
        "stage6_manifest_content_sha256": _sha("stage6"),
        "stage6_instance_content_sha256": _sha("stage6-instance"),
        "old50_capsule_content_sha256": _sha("old50"),
    }


def _build(*, pdf: bool = True) -> dict:
    stage5 = _stage5_wave()
    stage6 = _stage6_block(stage5)
    return overlay.build_wave_overlay_v1(
        selector_row=_selection(pdf=pdf),
        overlap_row=_overlap(stage6),
        stage5_wave=stage5,
        stage6_block=stage6,
        source_bindings=_source_bindings(),
    )


def _manifest_fixture() -> dict:
    entries = []
    for index in range(overlay.EXPECTED_INSTANCE_COUNT):
        instance_id = f"fixture-instance-{index:02d}"
        lane = selector_v2.PDF_LANE if index < 6 else selector_v2.GENERIC_LANE
        wave_count = 33 if index == overlay.EXPECTED_INSTANCE_COUNT - 1 else 23
        logical_sha = _sha(f"overlay-logical-{index}")
        summary = {
            "wave_overlay_count": wave_count,
            "prefix_transition_count": wave_count,
            "action_trace_ref_count": wave_count,
            "outcome_context_trace_ref_count": wave_count,
            "target_trace_ref_count": wave_count * 2,
            "projected_teammate_event_count": wave_count,
            "projected_teammate_damage": wave_count * 10,
            "excluded_focal_event_count": wave_count,
            "excluded_focal_damage": wave_count * 5,
            "zero_transition_wave_count": 0,
            "zero_teammate_schedule_wave_count": 0,
            "selected_guid_missing_wave_count": 0,
        }
        source_record_count = wave_count + 1
        input_partitions = {}
        for stage in ("stage5", "stage6"):
            logical_bytes = 1000 + index
            compressed_bytes = 500 + index
            input_partitions[stage] = {
                "instance_entry_content_sha256": _sha(
                    f"{stage}-instance-entry-{index}"
                ),
                "partition": {
                    "path": f"{stage}-{instance_id}.jsonl.gz",
                    "record_count": source_record_count,
                    "logical_content_sha256": _sha(f"{stage}-logical-{index}"),
                    "logical_size_bytes": logical_bytes,
                    "compressed_file_sha256": _sha(f"{stage}-compressed-{index}"),
                    "compressed_size_bytes": compressed_bytes,
                },
                "single_scan": {
                    "partition_open_count": 1,
                    "partition_sequential_scan_count": 1,
                    "compressed_bytes": compressed_bytes,
                    "logical_bytes": logical_bytes,
                    "source_record_count": source_record_count,
                    "selected_record_count": wave_count,
                    "selected_record_semantic_validation_count": wave_count,
                    "unselected_record_semantic_validation_count": 0,
                },
            }
        entries.append(
            slots_v1._content_addressed(
                {
                    "instance_id": instance_id,
                    "status": overlay.STATUS,
                    "selector": {
                        "selector_row_content_sha256": _sha(
                            f"selector-row-{index}"
                        ),
                        "selected_guid": f"0x{index + 1:016X}",
                        "selection_lane": lane,
                        "outcome_conditioned_discovery": lane == selector_v2.PDF_LANE,
                        "accepted_wave_count": wave_count,
                    },
                    "input_partitions": input_partitions,
                    "partition": {
                        "path": f"{instance_id}.{logical_sha}.jsonl.gz",
                        "record_schema": overlay.RECORD_SCHEMA,
                        "record_count": wave_count,
                        "logical_content_sha256": logical_sha,
                        "logical_size_bytes": 2000 + index,
                        "compressed_file_sha256": _sha(
                            f"overlay-compressed-{index}"
                        ),
                        "compressed_size_bytes": 1000 + index,
                        "gzip_mtime": 0,
                    },
                    "summary": summary,
                }
            )
        )
    summary = overlay._manifest_summary(entries)
    return slots_v1._content_addressed(
        {
            "schema": overlay.MANIFEST_SCHEMA,
            "revision": overlay.REVISION,
            "status": overlay.STATUS,
            "source_bindings": {
                "selector_v2": {
                    "content_sha256": overlay.EXPECTED_SELECTOR_V2_CONTENT_SHA256,
                    "file_sha256": _sha("selector-v2-file"),
                },
                "selector_v1_closure_carrier": {
                    "content_sha256": _sha("selector-v1"),
                    "file_sha256": _sha("selector-v1-file"),
                    "selection_semantics_superseded_by_v2": True,
                },
                "overlap_audit": {
                    "content_sha256": _sha("overlap"),
                    "file_sha256": _sha("overlap-file"),
                },
                "stage5_manifest": {
                    "content_sha256": _sha("stage5"),
                    "file_sha256": _sha("stage5-file"),
                },
                "stage6_manifest": {
                    "content_sha256": _sha("stage6"),
                    "file_sha256": _sha("stage6-file"),
                },
                "old50_capsule_content_sha256": _sha("old50"),
            },
            "instance_order": [entry["instance_id"] for entry in entries],
            "instances": entries,
            "split_contract": {
                "stage5_split_graph_preserved": {"nodes": [], "edges": []},
                "stage6_split_graph_equal": True,
                "required_split_unit": (
                    "connected component of instance, guild, and player nodes"
                ),
                "row_random_split_allowed": False,
                "same_player_or_guild_can_cross_folds": False,
            },
            "selection_and_episode_contract": {
                "one_selected_guid_per_instance": True,
                "selected_guid_applied_only_to_same_instance": True,
                "join_key": [
                    "instance_id",
                    "encounter_id",
                    "wave_ordinal",
                    "selected_guid",
                ],
                "stage5_stage6_full_wave_identity_equal_required": True,
                "accepted_wave_authority": (
                    "FROZEN_OVERLAP_AUDIT_EXACT_OR_SUBSET_ROWS"
                ),
                "prefix_transitions_preserved": True,
                "action_outcome_target_trace_refs_preserved": True,
                "exact_guid_loo_before_target_projection": True,
            },
            "streaming_contract": {
                "selected_instance_count": overlay.EXPECTED_INSTANCE_COUNT,
                "unselected_instance_partitions_opened": 0,
                "stage5_partition_open_count": overlay.EXPECTED_INSTANCE_COUNT,
                "stage6_partition_open_count": overlay.EXPECTED_INSTANCE_COUNT,
                "single_process": True,
                "one_instance_materialized_at_a_time": True,
                "gzip_mtime": 0,
            },
            "scientific_boundary": {
                "expert_training_discovery_overlay_complete": True,
                "pdf_lane_training_only_and_outcome_conditioned": True,
                "generic_lane_outcome_free": True,
                "heldout_performance_evidence_eligible": False,
                "comparison_outcome_eligible": False,
                "historical_policy_voting_eligible": False,
                "future_teammate_schedule_visible_to_policy": False,
                "policy_training_started": False,
                "simulator_run_started": False,
                "comparison_started": False,
                "deployment_started": False,
            },
            "execution_boundary": {
                "network_requests_made": 0,
                "heavy_jobs_started": False,
                "policy_training_started": False,
                "simulator_runs_started": False,
                "comparison_started": False,
                "deployment_started": False,
                "frozen_historical_policy_v2_v5_modified": False,
            },
            "summary": summary,
        }
    )


class ChronicleOld50ExactFurySlotOverlayV1Tests(unittest.TestCase):
    def test_manifest_fixture_closes_frozen_scope(self) -> None:
        result = overlay.validate_overlay_manifest_v1(_manifest_fixture())
        self.assertEqual(20, result["summary"]["instance_count"])
        self.assertEqual(470, result["summary"]["wave_overlay_count"])
        self.assertEqual(
            {selector_v2.GENERIC_LANE: 14, selector_v2.PDF_LANE: 6},
            result["summary"]["selection_lane_instance_counts"],
        )

    def test_empty_manifest_fails_after_readdress(self) -> None:
        result = _manifest_fixture()
        result["instances"] = []
        result["instance_order"] = []
        result["summary"] = overlay._manifest_summary([])
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "instance order/set differs",
        ):
            overlay.validate_overlay_manifest_v1(result)

    def test_manifest_partition_scan_tamper_fails_after_readdress(self) -> None:
        result = _manifest_fixture()
        entry = result["instances"][0]
        entry["input_partitions"]["stage5"]["single_scan"][
            "selected_record_count"
        ] -= 1
        result["instances"][0] = slots_v1._content_addressed(entry)
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "scanned exactly once",
        ):
            overlay.validate_overlay_manifest_v1(result)

    def test_manifest_positive_boundary_tamper_fails_after_readdress(self) -> None:
        result = _manifest_fixture()
        result["scientific_boundary"]["generic_lane_outcome_free"] = False
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "scientific boundary widened",
        ):
            overlay.validate_overlay_manifest_v1(result)

    def test_pdf_episode_is_training_only_and_exact_guid_loo(self) -> None:
        result = _build(pdf=True)
        overlay.validate_wave_overlay_v1(result)
        self.assertEqual(
            [INSTANCE, ENCOUNTER, WAVE_ORDINAL, GUID],
            result["identity"]["overlay_key"],
        )
        boundary = result["scientific_boundary"]
        self.assertTrue(boundary["pdf_top_player_training_only_outcome_conditioned"])
        self.assertFalse(boundary["heldout_performance_evidence_eligible"])
        self.assertFalse(boundary["comparison_outcome_eligible"])
        projection = result["exact_guid_loo_teammate_schedule"]
        excluded = projection["exact_guid_leave_one_out"]["excluded_events"]
        self.assertEqual(3, len(excluded))
        self.assertEqual(
            {
                "DIRECT_FRIENDLY_PLAYER",
                "EXACT_OFFICIAL_OWNER",
                "EXACT_OFFICIAL_CONTROLLER",
            },
            {row["attribution_kind"] for row in excluded},
        )
        self.assertEqual(TEAMMATE, projection["projected_schedule"][0]["actor_player_guid"])
        self.assertEqual(1, projection["projected_schedule"][0]["target_index"])

    def test_loo_attribution_tamper_fails_after_readdress(self) -> None:
        result = _build(pdf=True)
        loo = result["exact_guid_loo_teammate_schedule"]["exact_guid_leave_one_out"]
        loo["excluded_events"][0]["attribution_kind"] = "UNATTRIBUTED"
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "exact-GUID LOO schedule differs",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_loo_accounting_tamper_fails_after_readdress(self) -> None:
        result = _build(pdf=True)
        loo = result["exact_guid_loo_teammate_schedule"]["exact_guid_leave_one_out"]
        loo["excluded_event_count"] += 1
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "exact-GUID LOO schedule differs",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_loo_upper_bound_tamper_fails_after_readdress(self) -> None:
        result = _build(pdf=True)
        loo = result["exact_guid_loo_teammate_schedule"]["exact_guid_leave_one_out"]
        loo["stage5_all_event_count_upper_bound"] = len(loo["excluded_events"]) - 1
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "exact-GUID LOO schedule differs",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_generic_episode_is_explicitly_outcome_free(self) -> None:
        result = _build(pdf=False)
        boundary = result["scientific_boundary"]
        self.assertTrue(boundary["generic_identity_discovery_outcome_free"])
        self.assertFalse(boundary["pdf_top_player_training_only_outcome_conditioned"])
        self.assertEqual(
            [0],
            [row["trace_index"] for row in result["focal_player_episode"]["action_trace_refs"]],
        )
        self.assertEqual(
            [1],
            [
                row["trace_index"]
                for row in result["focal_player_episode"][
                    "outcome_context_trace_refs"
                ]
            ],
        )

    def test_generic_leaderboard_conditioning_tamper_fails_after_readdress(self) -> None:
        result = _build(pdf=False)
        semantics = result["slot_selection"]["selection_semantics"]
        semantics["leaderboard_membership_used_for_selection"] = True
        semantics["leaderboard_dps_rank_used_for_selection"] = True
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "selector semantics differ",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_exact_fury_relationship_is_revalidated_after_readdress(self) -> None:
        result = _build(pdf=False)
        spec = result["focal_player_episode"]["warrior_spec_lane"]
        spec["partition_key"] = "WARRIOR_ARMS"
        spec["observed_spec"] = "Arms"
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "spec conflicts",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_action_target_ref_tamper_fails_after_readdress(self) -> None:
        result = _build(pdf=False)
        replacement = deepcopy(result["focal_player_episode"]["action_trace_refs"][0])
        replacement["target"]["guid"] = TARGET_B
        result["focal_player_episode"]["action_trace_refs"][0] = replacement
        result["focal_player_episode"]["target_trace_refs"][0] = deepcopy(replacement)
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "prefix transition differs",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_full_stage5_stage6_wave_identity_is_required(self) -> None:
        stage5 = _stage5_wave()
        stage6 = _stage6_block(stage5)
        tampered = deepcopy(stage6)
        tampered["wave"]["encounter_ordinal"] = 2
        tampered = background_v2._content_addressed(tampered)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "full wave identities differ",
        ):
            overlay.build_wave_overlay_v1(
                selector_row=_selection(pdf=False),
                overlap_row=_overlap(tampered),
                stage5_wave=stage5,
                stage6_block=tampered,
                source_bindings=_source_bindings(),
            )

    def test_selected_guid_must_exist_in_every_wave(self) -> None:
        stage5 = _stage5_wave()
        stage5["players"] = []
        stage5 = model_v2._content_addressed(stage5)
        stage6 = _stage6_block(stage5)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "absent or duplicated",
        ):
            overlay.build_wave_overlay_v1(
                selector_row=_selection(pdf=False),
                overlap_row=_overlap(stage6),
                stage5_wave=stage5,
                stage6_block=stage6,
                source_bindings=_source_bindings(),
            )

    def test_scientific_gate_tamper_fails_validation(self) -> None:
        result = _build(pdf=True)
        result["scientific_boundary"]["comparison_outcome_eligible"] = True
        result = slots_v1._content_addressed(result)
        with self.assertRaisesRegex(
            overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
            "scientific boundary widened",
        ):
            overlay.validate_wave_overlay_v1(result)

    def test_selected_partition_is_scanned_once_and_filters_exact_keys(self) -> None:
        first = {
            "wave": {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "encounter_ordinal": 1,
                "wave_id": WAVE_ID,
                "wave_ordinal": WAVE_ORDINAL,
            }
        }
        second = deepcopy(first)
        second["wave"]["wave_ordinal"] = 4
        second["wave"]["wave_id"] = f"{ENCOUNTER}:external-v2-wave:4"
        rows = [first, second]
        logical_payload = b"".join(overlay._canonical(row) + b"\n" for row in rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jsonl.gz"
            with path.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
                    handle.write(logical_payload)
            compressed = path.read_bytes()
            entry = {
                "contamination_lane": {},
                "partition": {
                    "compressed_size_bytes": len(compressed),
                    "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
                    "record_count": 2,
                    "logical_size_bytes": len(logical_payload),
                    "logical_content_sha256": hashlib.sha256(logical_payload).hexdigest(),
                },
            }
            with mock.patch.object(model_v2, "_validate_model_wave") as validate:
                selected, scan = overlay._stream_selected_partition(
                    path=path,
                    entry=entry,
                    instance_id=INSTANCE,
                    accepted_keys={(INSTANCE, ENCOUNTER, WAVE_ORDINAL)},
                    stage=5,
                )
            self.assertEqual(1, validate.call_count)
            self.assertEqual(1, scan["partition_open_count"])
            self.assertEqual(2, scan["source_record_count"])
            self.assertEqual(1, scan["selected_record_count"])
            self.assertEqual(1, scan["selected_record_semantic_validation_count"])
            self.assertEqual(0, scan["unselected_record_semantic_validation_count"])
            self.assertEqual(
                {(INSTANCE, ENCOUNTER, WAVE_ORDINAL)}, set(selected)
            )

    def test_partition_hash_mismatch_fails_closed(self) -> None:
        row = {
            "wave": {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "encounter_ordinal": 1,
                "wave_id": WAVE_ID,
                "wave_ordinal": WAVE_ORDINAL,
            }
        }
        logical_payload = overlay._canonical(row) + b"\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jsonl.gz"
            with path.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0
                ) as handle:
                    handle.write(logical_payload)
            entry = {
                "contamination_lane": {},
                "partition": {
                    "compressed_size_bytes": path.stat().st_size,
                    "compressed_file_sha256": "0" * 64,
                    "record_count": 1,
                    "logical_size_bytes": len(logical_payload),
                    "logical_content_sha256": hashlib.sha256(logical_payload).hexdigest(),
                },
            }
            with (
                mock.patch.object(model_v2, "_validate_model_wave"),
                self.assertRaisesRegex(
                    overlay.ChronicleOld50ExactFurySlotOverlayV1Error,
                    "count/size/hash differs",
                ),
            ):
                overlay._stream_selected_partition(
                    path=path,
                    entry=entry,
                    instance_id=INSTANCE,
                    accepted_keys={(INSTANCE, ENCOUNTER, WAVE_ORDINAL)},
                    stage=5,
                )


if __name__ == "__main__":
    unittest.main()
