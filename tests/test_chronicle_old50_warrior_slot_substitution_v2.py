from __future__ import annotations

from copy import deepcopy
import unittest
from unittest import mock

from o2o_dps import chronicle_external_team_wave_model_v2 as model_v2
from o2o_dps import chronicle_old50_warrior_slot_substitution_v1 as v1
from o2o_dps import chronicle_old50_warrior_slot_substitution_v2 as v2


def _observation(
    guid: str,
    *,
    spec: str = "Unknown",
    status: str = "UNKNOWN_NONVOTING",
    conflicts: dict | None = None,
) -> dict:
    return {
        "player_guid": guid,
        "player_class": "Warrior",
        "player_name": "must-not-enter-v2",
        "player_spec": spec,
        "spec_evidence_status": status,
        "field_conflicts": {} if conflicts is None else conflicts,
    }


class ChronicleOld50WarriorSlotSubstitutionV2Tests(unittest.TestCase):
    def test_pdf_multiple_admissible_ranks_remains_top_player_but_is_truthful(self) -> None:
        rank_one = "0x0000000000000002"
        rank_twelve = "0x0000000000000001"
        selected = v2.select_instance_v2(
            instance_id="instance-pdf",
            source_lane="PDF_INTENDED_FURY_EXACT_GUID",
            source_selected_guid=rank_one,
            source_candidates=[
                {
                    "pdf_rank": 12,
                    "board_manifest_row_sha256": "b" * 64,
                    "exact_chain_status": "ADMISSIBLE_EXACT_GUID_CHAIN",
                    "character_guid": rank_twelve,
                },
                {
                    "pdf_rank": 1,
                    "board_manifest_row_sha256": "a" * 64,
                    "exact_chain_status": "ADMISSIBLE_EXACT_GUID_CHAIN",
                    "character_guid": rank_one,
                },
            ],
            stage5_observations={
                rank_one: _observation(rank_one),
                rank_twelve: _observation(rank_twelve),
            },
        )
        self.assertEqual(rank_one, selected["selected_guid"])
        semantics = selected["selection_semantics"]
        self.assertTrue(semantics["leaderboard_dps_rank_used_for_selection"])
        self.assertTrue(semantics["outcome_conditioned_discovery"])
        self.assertFalse(semantics["heldout_performance_evidence_eligible"])
        self.assertEqual(
            "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY", semantics["purpose"]
        )

    def test_generic_filters_arms_and_conflict_before_outcome_free_selection(self) -> None:
        arms = "0x0000000000000001"
        conflict = "0x0000000000000002"
        unknown = "0x0000000000000003"
        selected = v2.select_instance_v2(
            instance_id="instance-generic",
            source_lane="GENERIC_EXACT_FURY_FULL_SCOPE",
            source_selected_guid=arms,
            source_candidates=[
                {
                    "character_guid": guid,
                    "full_scope_exact_fury": True,
                    "exact_chain_status": None,
                }
                for guid in (arms, conflict, unknown)
            ],
            stage5_observations={
                arms: _observation(arms, spec="Arms", status="OBSERVED"),
                conflict: _observation(
                    conflict,
                    conflicts={"player_spec": ["Arms", "Fury"]},
                ),
                unknown: _observation(unknown),
            },
        )
        self.assertEqual(unknown, selected["selected_guid"])
        self.assertTrue(selected["selected_guid_differs_from_v1"])
        by_guid = {
            row["character_guid"]: row for row in selected["candidate_population"]
        }
        self.assertEqual(
            "STAGE5_ARMS_EXCLUDED",
            by_guid[arms]["stage5_compatibility"]["relationship"],
        )
        self.assertEqual(
            "STAGE5_SPEC_CONFLICT_EXCLUDED",
            by_guid[conflict]["stage5_compatibility"]["relationship"],
        )
        self.assertEqual(
            "STAGE5_UNKNOWN_NONCONFLICTING_EXTERNAL_FURY_PROMOTION",
            by_guid[unknown]["stage5_compatibility"]["relationship"],
        )
        self.assertTrue(
            selected["selection_semantics"]["generic_selection_outcome_free"]
        )
        self.assertNotIn(
            "player_name", selected["selected_stage5_observation"]
        )

    def test_observed_fury_is_corroborated_not_described_as_unknown_promotion(self) -> None:
        fury = "0x0000000000000004"
        selected = v2.select_instance_v2(
            instance_id="instance-fury",
            source_lane="GENERIC_EXACT_FURY_FULL_SCOPE",
            source_selected_guid=fury,
            source_candidates=[
                {
                    "character_guid": fury,
                    "full_scope_exact_fury": True,
                    "exact_chain_status": None,
                }
            ],
            stage5_observations={
                fury: _observation(fury, spec="Fury", status="OBSERVED")
            },
        )
        compatibility = selected["selected_stage5_compatibility"]
        self.assertEqual(
            "STAGE5_OBSERVED_FURY_CORROBORATED", compatibility["relationship"]
        )
        self.assertFalse(compatibility["unknown_promoted"])

    def test_all_arms_or_conflicting_candidates_fail_closed(self) -> None:
        arms = "0x0000000000000005"
        with self.assertRaisesRegex(
            v2.ChronicleOld50WarriorSlotSubstitutionV2Error,
            "no Stage-5-compatible exact Fury candidate",
        ):
            v2.select_instance_v2(
                instance_id="instance-blocked",
                source_lane="GENERIC_EXACT_FURY_FULL_SCOPE",
                source_selected_guid=arms,
                source_candidates=[
                    {
                        "character_guid": arms,
                        "full_scope_exact_fury": True,
                        "exact_chain_status": None,
                    }
                ],
                stage5_observations={
                    arms: _observation(arms, spec="Arms", status="OBSERVED")
                },
            )

    def test_twenty_instance_overlay_is_content_addressed_and_nonvoting(self) -> None:
        stage5_entries = []
        source_rows = []
        for index in range(20):
            instance_id = f"instance-{index:02d}"
            guid = f"0x{index + 1:016X}"
            observation = _observation(guid)
            entry = v1._content_addressed(
                {
                    "instance_id": instance_id,
                    "exact_roster_evidence_sha256": f"{index + 1:064x}",
                    "instance_provenance": {
                        "warrior_spec_evidence": {"observations": [observation]}
                    },
                }
            )
            stage5_entries.append(entry)
            if index < 6:
                source_lane = "PDF_INTENDED_FURY_EXACT_GUID"
                selection = {
                    "pdf_selector_receipt": {
                        "candidate_population": [
                            {
                                "pdf_rank": index + 1,
                                "board_manifest_row_sha256": f"{index + 21:064x}",
                                "exact_chain_status": "ADMISSIBLE_EXACT_GUID_CHAIN",
                                "character_guid": guid,
                                "chain_receipt_sha256": f"{index + 41:064x}",
                            }
                        ]
                    },
                    "generic_selector_receipt": None,
                }
            else:
                source_lane = "GENERIC_EXACT_FURY_FULL_SCOPE"
                selection = {
                    "pdf_selector_receipt": None,
                    "generic_selector_receipt": {"eligible_guids": [guid]},
                }
            evidence = v1._content_addressed({"selection": selection})
            source_rows.append(
                {
                    "instance_id": instance_id,
                    "selection_lane": source_lane,
                    "player_guid": guid,
                    "accepted_wave_count": 1,
                    "exact_fury_identity_evidence": evidence,
                }
            )
        stage5 = v1._content_addressed(
            {
                "schema": model_v2.SCHEMA,
                "implementation_revision": model_v2.IMPLEMENTATION_REVISION,
                "status": model_v2.STATUS,
                "instances": stage5_entries,
            }
        )
        stage5_file_sha = "e" * 64
        source = v1._content_addressed(
            {
                "source_bindings": {
                    "stage5_manifest": {
                        "content_sha256": stage5["content_address"]["sha256"],
                        "file_sha256": stage5_file_sha,
                    }
                },
                "rows": source_rows,
            }
        )
        with mock.patch.object(
            v1,
            "validate_exact_fury_selector_receipt_v1",
            return_value=deepcopy(source),
        ):
            result = v2.build_exact_fury_selector_receipt_v2(
                v1_selector_receipt=source,
                v1_selector_receipt_file_sha256="f" * 64,
                stage5_manifest=stage5,
                stage5_manifest_file_sha256=stage5_file_sha,
            )
        v2.validate_exact_fury_selector_receipt_v2(result)
        self.assertEqual(20, result["summary"]["instance_count"])
        self.assertEqual(6, result["summary"]["pdf_outcome_conditioned_discovery_count"])
        self.assertEqual(14, result["summary"]["generic_outcome_free_discovery_count"])
        self.assertEqual(0, result["summary"]["arms_or_conflict_selected_count"])
        self.assertFalse(result["scientific_boundary"]["comparison_ready"])
        self.assertFalse(result["execution_boundary"]["policy_training_started"])


if __name__ == "__main__":
    unittest.main()
