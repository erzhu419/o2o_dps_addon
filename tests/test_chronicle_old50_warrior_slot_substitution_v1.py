from __future__ import annotations

from copy import deepcopy
import unittest
from unittest import mock

from o2o_dps import chronicle_external_team_background_generator_v2 as background_v2
from o2o_dps import chronicle_external_team_wave_model_v2 as model_v2
from o2o_dps import chronicle_old50_warrior_slot_substitution_v1 as slots
from o2o_dps import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1


INSTANCE = "00000000-0000-0000-0000-000000000001"
ENCOUNTER = "00000000-0000-0000-0000-000000000002"
CAPSULE_WAVE = f"{ENCOUNTER}:wave:3"
EXTERNAL_WAVE = f"{ENCOUNTER}:external-v2-wave:3"
COMPONENT = "c" * 64
FURY_EXACT = "0x00000000000000F1"
FURY_STAGE5 = "0x00000000000000F2"
ARMS = "0x00000000000000A1"
UNKNOWN = "0x00000000000000A2"
MAGE = "0x00000000000000E1"
TARGET_A = "0xF130000001000001"
TARGET_B = "0xF130000002000002"
TARGET_C = "0xF130000003000003"


def _raw_contamination() -> dict:
    return {
        "label": "NO_KNOWN_RULE_MATCH",
        "candidate_filter_passed": True,
        "raw_label_candidate_filter_passed": True,
        "candidate_authority": "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP",
        "cohort_assignment": "TRAINING_CANDIDATE",
        "cohort_reason": "fixture",
        "cohort_receipt_content_sha256": "d" * 64,
        "time_field": "started_at",
        "started_at": "2026-09-04T12:00:00Z",
        "uploaded_at_used": False,
        "player_name_used": False,
    }


def _normalized_contamination() -> dict:
    return {
        "label": "NO_KNOWN_RULE_MATCH",
        "training_candidate": True,
        "raw_label_candidate_filter_passed": True,
        "candidate_authority": "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP",
        "cohort_assignment": "TRAINING_CANDIDATE",
        "cohort_reason": "fixture",
        "cohort_receipt_content_sha256": "d" * 64,
        "time_field": "started_at",
        "started_at": "2026-09-04T12:00:00Z",
        "uploaded_at_used": False,
        "player_name_used": False,
        "named_player_blacklist_or_weighting_used": False,
        "scope": "raid_instance",
    }


def _spec(partition: str) -> dict:
    if partition == "WARRIOR_FURY":
        observed = "Fury"
        admitted = True
        exact = True
    elif partition == "WARRIOR_ARMS":
        observed = "Arms"
        admitted = True
        exact = True
    else:
        observed = None
        admitted = False
        exact = False
    return {
        "partition_key": partition,
        "role": "fixture",
        "observed_spec": observed,
        "evidence_status": "OBSERVED" if admitted else "MISSING",
        "exact_guid_match": exact,
        "fury_or_arms_conflict_free_observation": admitted,
        "voting_authorized": False,
        "source_evidence": {
            "status": "OBSERVED" if admitted else "MISSING",
            "player_spec": observed if admitted else "Unknown",
            "field_conflicts": {},
            "exact_player_guid_match": exact,
            "voting_for_fury_or_arms_lane": admitted,
            "inference_used": False,
            "fixture": True,
        },
    }


def _player(guid: str, hero_class: str, partition: str) -> dict:
    return model_v2._content_addressed(
        {
            "player": {"guid": guid, "class": hero_class, "race": None},
            "warrior_spec_lane": _spec(partition),
            "leave_one_player_out_background": {
                "status": model_v2.STATUS,
                "focal_player_guid": guid,
                "filter_contract": {
                    "excluded_attribution_kinds": list(slots.ATTRIBUTION_KINDS),
                    "exact_player_guid_match_required": True,
                    "unattributed_events_retained": True,
                    "name_or_guid_suffix_inference_used": False,
                    "damage_amount_source": "DMG_ONLY",
                    "dead_marker_damage_added": 0,
                },
                "included_event_count": 100,
                "excluded_focal_event_count": 20,
                "included_damage_amount_by_attribution": {},
                "included_damage_amount": 10000,
                "excluded_focal_damage_amount_by_attribution": {},
                "excluded_focal_damage_amount": 5000,
                "unattributed_branch": {},
                "event_materialization": "filter exact_trace by exact focal player_guid",
                "voting_authorized": False,
            },
        }
    )


def _stage5_wave() -> dict:
    return model_v2._content_addressed(
        {
            "schema": model_v2.PARTITION_RECORD_SCHEMA,
            "implementation_revision": model_v2.IMPLEMENTATION_REVISION,
            "status": model_v2.STATUS,
            "wave": {
                "instance_id": INSTANCE,
                "encounter_id": ENCOUNTER,
                "encounter_ordinal": 1,
                "wave_id": EXTERNAL_WAVE,
                "wave_ordinal": 3,
            },
            "raid_provenance": {"contamination": _raw_contamination()},
            "players": [
                _player(UNKNOWN, "Warrior", "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING"),
                _player(MAGE, "Mage", "MAGE_UNSPECIFIED"),
                _player(FURY_STAGE5, "Warrior", "WARRIOR_FURY"),
                _player(ARMS, "Warrior", "WARRIOR_ARMS"),
                _player(FURY_EXACT, "Warrior", "WARRIOR_FURY"),
            ],
        }
    )


def _event(
    index: int,
    actor: str,
    kind: str,
    target_guid: str,
    target_index: int,
    damage: int,
) -> dict:
    return {
        "time_ms": index * 10,
        "target_index": target_index,
        "target_guid": target_guid,
        "event_id": f"event-{index}",
        "damage": damage,
        "actor_player_guid": actor,
        "attribution_kind": kind,
        "source_guid": actor,
        "source_lane": "FRIENDLY_PLAYER",
        "trace_index": index,
        "source_eventmeta_order_key": [0, 0, 0, 0, index],
        "official_message_sha256": f"{index + 100:064x}",
        "evidence_status": "POSITIVE_OFFICIAL_DMG_EXACT_PLAYER_AND_TARGET",
        "runtime_eligible": True,
    }


def _stage6_block(stage5: dict) -> dict:
    events = [
        _event(1, FURY_EXACT, "DIRECT_FRIENDLY_PLAYER", TARGET_A, 0, 10),
        _event(2, FURY_EXACT, "EXACT_OFFICIAL_OWNER", TARGET_B, 1, 20),
        # LOO must happen before the old-50 pile filter, so this focal event on
        # unselected TARGET_C is still explicitly removed by exact GUID.
        _event(3, FURY_EXACT, "EXACT_OFFICIAL_CONTROLLER", TARGET_C, 2, 30),
        _event(4, ARMS, "DIRECT_FRIENDLY_PLAYER", TARGET_B, 1, 40),
        _event(5, UNKNOWN, "DIRECT_FRIENDLY_PLAYER", TARGET_A, 0, 50),
        _event(6, MAGE, "DIRECT_FRIENDLY_PLAYER", TARGET_C, 2, 60),
    ]
    return background_v2._content_addressed(
        {
            "schema": background_v2.PARTITION_RECORD_SCHEMA,
            "implementation_revision": background_v2.IMPLEMENTATION_REVISION,
            "record_type": "component_whole_wave_background_block",
            "status": background_v2.STATUS,
            "wave": deepcopy(stage5["wave"]),
            "component_id": COMPONENT,
            "source_model": {
                "wave_content_sha256": stage5["content_address"]["sha256"],
                "exact_trace_content_sha256": "e" * 64,
            },
            "contamination_lane": _normalized_contamination(),
            "roster_player_guids": sorted(
                [FURY_EXACT, FURY_STAGE5, ARMS, UNKNOWN, MAGE]
            ),
            "target_registry": [
                {"target_guid": TARGET_A, "target_index": 0},
                {"target_guid": TARGET_B, "target_index": 1},
                {"target_guid": TARGET_C, "target_index": 2},
            ],
            "runtime_candidate_schedule": events,
        }
    )


def _overlap_audit(block: dict, *, classification: str = "SUBSET") -> dict:
    row = {
        "classification": classification,
        "reason_codes": ["fixture"],
        "scenario_id": "scenario-3",
        "key": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "wave_ordinal": 3,
        },
        "target_join": {
            "capsule_target_guids": [TARGET_B, TARGET_A],
            "stage6_target_guids": [TARGET_A, TARGET_B, TARGET_C],
            "guid_relation": "STRICT_SUBSET",
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
            "stage6_component_id": COMPONENT,
            "relation": "OVERLAP_PROJECTION_EXACT",
            "split_authority": "EQUIVALENT_ON_OVERLAP",
        },
        "contamination": {
            "stage6_label": "NO_KNOWN_RULE_MATCH",
            "stage6_training_candidate": True,
            "exact_stage5_wave_normalized_lane_equal": True,
            "authority": "EXACT_STAGE5_WAVE_BOUND_COHORT_RECEIPT_VIA_STAGE6",
            "raw_instance_lane_compared_directly": False,
            "old50_capsule_field": "ABSENT",
            "player_name_used": False,
        },
        "source_bindings": {
            "capsule_sha256": "1" * 64,
            "capsule_wave_id": CAPSULE_WAVE,
            "stage6_wave_id": EXTERNAL_WAVE,
            "stage6_block_content_sha256": block["content_address"]["sha256"],
        },
    }
    return overlap_v1._content_addressed(
        {
            "schema": overlap_v1.AUDIT_SCHEMA,
            "revision": overlap_v1.REVISION,
            "status": "COMPLETE_DESCRIPTIVE_NONVOTING",
            "input_bindings": {
                "old50_capsule": {
                    "locator": "capsule.json.gz",
                    "content_sha256": "1" * 64,
                    "file_sha256": "2" * 64,
                },
                "stage5_manifest": {
                    "locator": "stage5/manifest.json",
                    "content_sha256": "3" * 64,
                    "file_sha256": "4" * 64,
                },
                "stage6_manifest": {
                    "locator": "stage6/manifest.json",
                    "content_sha256": "5" * 64,
                    "file_sha256": "6" * 64,
                },
            },
            "rows": [row],
            "scientific_boundary": {
                "nonvoting": True,
                "comparison_ready": False,
                "formal_runner_registered": False,
                "multiseed_comparison_started": False,
            },
        }
    )


def _exact_fury_evidence(
    *, lane: str = "PDF_INTENDED_FURY_EXACT_GUID", guid: str = FURY_EXACT
) -> dict:
    pdf_lane = lane == "PDF_INTENDED_FURY_EXACT_GUID"
    return slots.build_exact_fury_identity_evidence_v1(
        selection_lane=lane,
        instance_id=INSTANCE,
        character_guid=guid,
        accepted_wave_count=1,
        player_name="托尼牛" if pdf_lane else None,
        board_rank=2 if pdf_lane else None,
        leaderboard_manifest_file_sha256="9" * 64 if pdf_lane else None,
        pdf_file_sha256="a" * 64 if pdf_lane else None,
        board_manifest_row_sha256="b" * 64 if pdf_lane else None,
        character_query_record_sha256="c" * 64 if pdf_lane else None,
        character_identity_record_sha256="d" * 64 if pdf_lane else None,
        character_history_manifest_content_sha256="e" * 64 if pdf_lane else None,
        character_history_manifest_file_sha256="f" * 64 if pdf_lane else None,
        character_inventory_manifest_content_sha256="7" * 64 if pdf_lane else None,
        character_inventory_manifest_file_sha256="8" * 64 if pdf_lane else None,
        exact_dps_index_manifest_content_sha256="1" * 64 if pdf_lane else None,
        exact_dps_index_manifest_file_sha256="2" * 64 if pdf_lane else None,
        stage5_manifest_content_sha256="3" * 64,
        stage6_manifest_content_sha256="5" * 64,
        old50_capsule_content_sha256="1" * 64,
        raw_ranking_snapshot_manifest_content_sha256="6" * 64,
        raw_ranking_capture_receipt_content_sha256="0" * 64,
        raw_ranking_instance_object_sha256="7" * 64,
        stage5_instance_content_sha256=None if pdf_lane else "8" * 64,
        exact_roster_evidence_sha256=(
            None if pdf_lane else slots._stage5_roster_evidence_sha(_stage5_wave())
        ),
        generic_eligible_guids=() if pdf_lane else (guid, "0x00000000000000F9"),
        pdf_candidate_population=(
            (
                {
                    "pdf_rank": 1,
                    "board_manifest_row_sha256": "0" * 64,
                    "exact_chain_status": "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN",
                    "character_guid": None,
                    "chain_receipt_sha256": "3" * 64,
                },
                {
                    "pdf_rank": 2,
                    "board_manifest_row_sha256": "b" * 64,
                    "exact_chain_status": "ADMISSIBLE_EXACT_GUID_CHAIN",
                    "character_guid": guid,
                    "chain_receipt_sha256": "4" * 64,
                },
                {
                    "pdf_rank": 5,
                    "board_manifest_row_sha256": "c" * 64,
                    "exact_chain_status": "ADMISSIBLE_EXACT_GUID_CHAIN",
                    "character_guid": "0x00000000000000F9",
                    "chain_receipt_sha256": "5" * 64,
                },
            )
            if pdf_lane
            else ()
        ),
        same_instance_ranking_rows=[
            {
                "ranking_row_sha256": f"{index:064x}",
                "player_class": "WARRIOR",
                "spec": "Fury",
                "encounter_id": (
                    f"00000000-0000-0000-0000-{index:012d}" if index <= 9 else None
                ),
                "is_trash": index == 10,
                "role": "dps",
            }
            for index in range(1, 11)
        ],
    )


class ChronicleOld50WarriorSlotSubstitutionV1Tests(unittest.TestCase):
    def _build(self, *, classification: str = "SUBSET", evidence=None) -> dict:
        stage5 = _stage5_wave()
        block = _stage6_block(stage5)
        with (
            mock.patch.object(model_v2, "_validate_model_wave") as stage5_validate,
            mock.patch.object(background_v2, "_validate_block") as stage6_validate,
        ):
            bundle = slots.build_warrior_slot_substitution_bundle_v1(
                overlap_audit=_overlap_audit(block, classification=classification),
                stage5_wave=stage5,
                stage6_block=block,
                exact_fury_identity_evidence=(
                    [_exact_fury_evidence()] if evidence is None else evidence
                ),
            )
        stage5_validate.assert_called_once()
        stage6_validate.assert_called_once()
        return bundle

    def test_enumerates_all_warrior_specs_and_privileges_exact_fury(self) -> None:
        bundle = self._build()
        labels = [row["slot_evidence"]["slot_label"] for row in bundle["inputs"]]
        self.assertEqual(
            [
                "EXACT_FURY",
                "STAGE5_OBSERVED_FURY",
                "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY",
                "UNKNOWN_ENVIRONMENT_SUBSTITUTION_ONLY",
            ],
            labels,
        )
        self.assertEqual(4, bundle["summary"]["warrior_slot_count"])
        self.assertEqual(1, bundle["summary"]["exact_fury_identity_evidence_count"])
        self.assertEqual(2, bundle["summary"]["environment_substitution_only_count"])
        exact, _, arms, unknown = bundle["inputs"]
        self.assertEqual(FURY_EXACT, exact["identity"]["slot_player_guid"])
        self.assertTrue(
            exact["slot_evidence"]["future_historical_fury_adapter_eligible"]
        )
        selector = exact["slot_evidence"]["exact_fury_identity_evidence"][
            "selection"
        ]["pdf_selector_receipt"]
        self.assertEqual(3, selector["candidate_population_count"])
        self.assertEqual(
            "INADMISSIBLE_NO_CAPTURED_EXACT_CHAIN",
            selector["candidate_population"][0]["exact_chain_status"],
        )
        self.assertEqual(FURY_EXACT, selector["selected_guid"])
        for fallback in (arms, unknown):
            self.assertTrue(fallback["slot_evidence"]["environment_substitution_only"])
            self.assertEqual(
                "NOT_HISTORICAL_FURY",
                fallback["slot_evidence"]["historical_fury_identity_tier"],
            )
            self.assertFalse(
                fallback["slot_evidence"]["historical_fury_comparison_eligible"]
            )

    def test_stage6_loo_removes_direct_owner_and_controller_before_subset_projection(self) -> None:
        exact = self._build()["inputs"][0]
        projection = exact["causal_schedule_projection"]
        loo = projection["exact_guid_leave_one_out"]
        self.assertEqual(3, loo["excluded_event_count"])
        self.assertEqual(60, loo["excluded_damage"])
        self.assertEqual(
            {
                "DIRECT_FRIENDLY_PLAYER": 1,
                "EXACT_OFFICIAL_CONTROLLER": 1,
                "EXACT_OFFICIAL_OWNER": 1,
            },
            loo["excluded_event_count_by_attribution"],
        )
        self.assertIn(TARGET_C, [row["target_guid"] for row in loo["excluded_events"]])
        self.assertEqual(1, projection["target_projection"]["unselected_nonfocal_event_count"])
        schedule = projection["projected_schedule"]
        self.assertNotIn(FURY_EXACT, [row["actor_player_guid"] for row in schedule])
        self.assertEqual([0, 1], [row["target_index"] for row in schedule])
        self.assertEqual([1, 0], [row["stage6_target_index"] for row in schedule])

    def test_outputs_are_deterministic_and_content_addressed(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)
        slots.validate_warrior_slot_substitution_bundle_v1(first)
        slots.validate_exact_fury_identity_evidence_v1(_exact_fury_evidence())

    def test_generic_exact_fury_lane_uses_guid_full_scope_not_pdf_or_outcomes(self) -> None:
        evidence = _exact_fury_evidence(lane="GENERIC_EXACT_FURY_FULL_SCOPE")
        self.assertEqual(
            "LEXICOGRAPHICALLY_SMALLEST_CANONICAL_GUID_AFTER_FULL_SCOPE_FILTER",
            evidence["selection"]["selection_rule"],
        )
        self.assertIsNone(evidence["board_provenance_only"]["player_name"])
        self.assertIsNone(evidence["board_provenance_only"]["pdf_rank"])
        self.assertFalse(evidence["selection"]["name_used"])
        self.assertFalse(evidence["selection"]["outcome_fields_used"])
        self.assertEqual(10, evidence["spec_conclusion"]["ranking_record_count_for_guid"])
        self.assertEqual(9, evidence["spec_conclusion"]["distinct_encounter_count"])
        self.assertTrue(evidence["spec_conclusion"]["trash_record_present"])
        bundle = self._build(evidence=[evidence])
        exact = bundle["inputs"][0]
        self.assertEqual("EXACT_FURY", exact["slot_evidence"]["slot_label"])
        self.assertEqual(
            "STAGE5_ROSTER_FULL_SCOPE_RANKING_BOUND_EXACT_FURY",
            exact["slot_evidence"]["historical_fury_identity_tier"],
        )

    def test_external_exact_fury_promotes_exact_stage5_unknown_guid_only(self) -> None:
        evidence = _exact_fury_evidence(
            lane="GENERIC_EXACT_FURY_FULL_SCOPE", guid=UNKNOWN
        )
        bundle = self._build(evidence=[evidence])
        exact = next(
            row
            for row in bundle["inputs"]
            if row["identity"]["slot_player_guid"] == UNKNOWN
        )
        self.assertEqual("EXACT_FURY", exact["slot_evidence"]["slot_label"])
        self.assertEqual(
            "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_EXACT_FURY",
            exact["slot_evidence"]["stage5_spec_relationship_to_slot_label"],
        )
        self.assertFalse(exact["slot_evidence"]["environment_substitution_only"])

    def test_external_exact_fury_does_not_overwrite_stage5_spec_conflict(self) -> None:
        spec = _spec("WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING")
        spec["source_evidence"]["field_conflicts"] = {
            "player_spec": ["Arms", "Fury"]
        }
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "cannot overwrite Stage-5 Arms or conflicting spec evidence",
        ):
            slots._slot_label(spec=spec, exact_evidence=_exact_fury_evidence())

    def test_generic_selector_rejects_hand_picked_nonminimum_guid(self) -> None:
        evidence = _exact_fury_evidence(lane="GENERIC_EXACT_FURY_FULL_SCOPE")
        core = deepcopy(evidence)
        core.pop("content_address")
        eligible = ["0x00000000000000F0", FURY_EXACT]
        receipt = core["selection"]["generic_selector_receipt"]
        receipt["eligible_guids"] = eligible
        receipt["eligible_guid_count"] = len(eligible)
        receipt["eligible_guids_sha256"] = slots._sha256(eligible)
        tampered = slots._content_addressed(core)
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "selector receipt",
        ):
            slots.validate_exact_fury_identity_evidence_v1(tampered)

    def test_exact_fury_requires_complete_bound_rows_all_fury_or_equivalent(self) -> None:
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "all 10/10 full-scope same-instance ranking records",
        ):
            slots.build_exact_fury_identity_evidence_v1(
                selection_lane="PDF_INTENDED_FURY_EXACT_GUID",
                instance_id=INSTANCE,
                character_guid=FURY_EXACT,
                accepted_wave_count=1,
                player_name="托尼牛",
                board_rank=1,
                leaderboard_manifest_file_sha256="9" * 64,
                pdf_file_sha256="a" * 64,
                board_manifest_row_sha256="b" * 64,
                character_query_record_sha256="c" * 64,
                character_identity_record_sha256="d" * 64,
                character_history_manifest_content_sha256="e" * 64,
                character_history_manifest_file_sha256="f" * 64,
                character_inventory_manifest_content_sha256="7" * 64,
                character_inventory_manifest_file_sha256="8" * 64,
                exact_dps_index_manifest_content_sha256="1" * 64,
                exact_dps_index_manifest_file_sha256="2" * 64,
                stage5_manifest_content_sha256="3" * 64,
                stage6_manifest_content_sha256="5" * 64,
                old50_capsule_content_sha256="1" * 64,
                raw_ranking_snapshot_manifest_content_sha256="6" * 64,
                raw_ranking_capture_receipt_content_sha256="0" * 64,
                raw_ranking_instance_object_sha256="7" * 64,
                same_instance_ranking_rows=[
                    {
                        "ranking_row_sha256": f"{index:064x}",
                        "player_class": "WARRIOR",
                        "spec": "Arms" if index == 10 else "Fury",
                        "encounter_id": (
                            f"00000000-0000-0000-0000-{index:012d}"
                            if index <= 9
                            else None
                        ),
                        "is_trash": index == 10,
                        "role": "dps",
                    }
                    for index in range(1, 11)
                ],
            )

    def test_validator_rejects_readdressed_arms_historical_promotion(self) -> None:
        bundle = self._build()
        arms = next(
            row
            for row in bundle["inputs"]
            if row["slot_evidence"]["slot_label"]
            == "ARMS_ENVIRONMENT_SUBSTITUTION_ONLY"
        )
        core = deepcopy(arms)
        core.pop("content_address")
        core["slot_evidence"]["historical_fury_identity_tier"] = (
            "QUERY_IDENTITY_HISTORY_RANKING_BOUND_EXACT_FURY"
        )
        tampered = slots._content_addressed(core)
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "identity tier differs",
        ):
            slots.validate_warrior_slot_substitution_input_v1(tampered)

    def test_validator_rejects_readdressed_wave_or_manifest_closure(self) -> None:
        exact = self._build()["inputs"][0]
        core = deepcopy(exact)
        core.pop("content_address")
        core["same_wave_identity"]["stage6_wave_id"] = f"{ENCOUNTER}:wave:3"
        tampered_wave = slots._content_addressed(core)
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "exact-wave join",
        ):
            slots.validate_warrior_slot_substitution_input_v1(tampered_wave)

        core = deepcopy(exact)
        core.pop("content_address")
        core["source_bindings"]["stage6_manifest"]["content_sha256"] = "0" * 64
        tampered_closure = slots._content_addressed(core)
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "identity-evidence closure",
        ):
            slots.validate_warrior_slot_substitution_input_v1(tampered_closure)

    def test_rejected_overlap_and_external_evidence_spec_conflict_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "rejected overlap row",
        ):
            self._build(classification="REJECT")
        conflicting = _exact_fury_evidence(guid=ARMS)
        with self.assertRaisesRegex(
            slots.ChronicleOld50WarriorSlotSubstitutionError,
            "conflicts with the Stage-5.*lane",
        ):
            self._build(evidence=[conflicting])


if __name__ == "__main__":
    unittest.main()
