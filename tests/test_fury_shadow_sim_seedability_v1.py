from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_shadow_sim_seedability_v1 import (
    CLASS_APPROX_ONLY,
    CLASS_EXACT,
    CLASS_REJECT,
    FuryShadowSimSeedabilityError,
    build_fury_shadow_sim_seedability,
    materialize_fury_shadow_sim_seedability,
)


SESSION = "shadow-seedability-test"


def _lanes(
    *,
    off_gcd: list[str] | None = None,
    queue: list[str] | None = None,
    gcd: list[str] | None = None,
) -> dict[str, list[str]]:
    return {
        "off_gcd": list(off_gcd or []),
        "queue": list(queue or []),
        "gcd": list(gcd or []),
    }


def _transition(
    sequence: int,
    *,
    actual: dict[str, list[str]],
    candidate: dict[str, list[str]] | None = None,
    gcd: float = 0.0,
    queued: str = "KEEP",
) -> dict[str, object]:
    action_rows = [
        {
            "action_id": action,
            "lane": lane,
            "client_accepted": True,
            "status": "result",
        }
        for lane in ("off_gcd", "queue", "gcd")
        for action in actual[lane]
    ]
    return {
        "schema": "fury_shadow_transition_fragment/v1",
        "identity": {
            "export_session_id": SESSION,
            "decision_id": f"boc-decision-{100 + sequence}",
            "sequence_in_session": sequence,
        },
        "temporal": {
            "captured_at": 1000.0 + sequence,
            "observed_window_seconds": 0.25 if not actual["queue"] else 2.0,
        },
        "state_before": {
            "bloodrageCooldown": 0.0,
            "bloodrageCooldownKnown": True,
            "bloodthirstCooldown": 1.0,
            "bloodthirstCooldownKnown": True,
            "buffCount": 2,
            "classFile": "WARRIOR",
            "gcd": gcd,
            "health": 4408,
            "inCombat": True,
            "level": 60,
            "mainHandSwingRemaining": 1.25,
            "mainHandSwingRemainingKnown": True,
            "maximumHealth": 4609,
            "maximumPower": 100,
            "maximumRage": 100,
            "nearbyEnemies": 1,
            "nearbyEnemiesKnown": True,
            "percentHealth": 95.638967238013,
            "power": 40,
            "powerType": 1,
            "queuedSwing": queued,
            "queuedSwingKnown": True,
            "rage": 40,
            "targetBuffCount": 1,
            "targetCanAttack": True,
            "targetClassification": "worldboss",
            "targetExists": True,
            "targetHealth": 52_000_000,
            "targetInCombat": True,
            "targetIsBoss": True,
            "targetIsDead": False,
            "targetLevelKnown": False,
            "targetName": "Training Dummy",
            "targetPercentHealth": 52.0,
            "whirlwindCooldown": 0.0,
            "whirlwindCooldownKnown": True,
        },
        "actual": {
            "source": "Cat2_exact_sink_trace",
            "executor": {
                "kind": "cat2_configuration_card_stack",
                "expert": "Cat2",
                "entry": "Cat2.ExecuteConfiguration",
                "profile_id": 1,
                "profile_name": "BrainOfCat Shadow",
                "executed_policy_id": None,
                "executed_policy_proposal_available": False,
            },
            "factorized_action": actual,
            "actions": action_rows,
        },
        "recorded_active_policy": {
            "policy_id": "fury_warrior_rule_baseline_v1",
            "executed": False,
            "proposal": _lanes(),
            "counterfactual_outcome": None,
        },
        "candidate": {
            "policy_id": "fury_combined_candidate_shadow_v1",
            "executed": False,
            "proposal": candidate or _lanes(),
            "counterfactual_outcome": None,
        },
        "observed_actual_immediate_outcome": {
            "status": "complete",
            "damage": 300.0,
            "scalar_reward": None,
            "scalar_reward_reason": "no_preregistered_scalar_reward",
        },
        "eligibility": {
            "behavior_label_eligible": True,
            "observed_actual_immediate_outcome_usable": True,
            "scalar_reward_available": False,
            "recorded_active_policy_counterfactual_reward_available": False,
            "candidate_counterfactual_reward_available": False,
            "full_next_state_available": False,
            "td_transition_eligible": False,
            "offline_rl_episode_eligible": False,
            "deployment_allowed": False,
        },
        "provenance": {
            "kind": "OBSERVED",
            "source_semantics": (
                "exact_cat2_action_with_causally_linked_typed_immediate_outcome"
            ),
        },
    }


def _jsonl_text(rows: list[dict[str, object]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_bundle(
    root: Path,
    rows: list[dict[str, object]],
) -> tuple[Path, Path, Path, Path]:
    transitions = root / "transitions.jsonl"
    manifest = root / "transitions.manifest.json"
    cat2_rows = root / "cat2.rows.jsonl"
    cat2_report = root / "cat2.report.json"

    transition_text = _jsonl_text(rows)
    transitions.write_text(transition_text, encoding="utf-8", newline="\n")
    eligibility = {
        "behavior_label_eligible": True,
        "observed_actual_immediate_outcome_usable": True,
        "scalar_reward_available": False,
        "recorded_active_policy_counterfactual_reward_available": False,
        "candidate_counterfactual_reward_available": False,
        "full_next_state_available": False,
        "td_transition_eligible": False,
        "offline_rl_episode_eligible": False,
        "deployment_allowed": False,
    }
    manifest_document: dict[str, object] = {
        "schema": "fury_shadow_transition_dataset_manifest/v1",
        "kind": "fury_shadow_transition_dataset",
        "record_schema": "fury_shadow_transition_fragment/v1",
        "output": str(transitions.resolve()),
        "output_sha256": hashlib.sha256(transition_text.encode("utf-8")).hexdigest(),
        "row_count": len(rows),
        "export_session_id": SESSION,
        "identities": [
            {
                "export_session_id": row["identity"]["export_session_id"],
                "decision_id": row["identity"]["decision_id"],
            }
            for row in rows
        ],
        "eligibility_contract": eligibility,
        "commit": {
            "state": "complete",
            "manifest_written_last": True,
            "content_addressed": True,
        },
    }
    _write_json(manifest, manifest_document)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()

    conformance_rows: list[dict[str, object]] = []
    for row in rows:
        actual = copy.deepcopy(row["actual"]["factorized_action"])
        conformance_rows.append(
            {
                "schema": "fury_cat2_profile_conformance_row/v1",
                "identity": copy.deepcopy(row["identity"]),
                "profile_identity_match": True,
                "strict": {
                    "input_complete": False,
                    "missing_pre_action_fields": [
                        "current_stance",
                        "target_melee_range",
                        "channel_or_cast_gate",
                        "auto_attack_active",
                        "effective_execute_cost",
                        "effective_whirlwind_cost",
                    ],
                    "status": "NOT_EVALUABLE",
                },
                "assumption_conditioned": {
                    "predicted_factorized_action": actual,
                    "observed_factorized_action": actual,
                    "exact_factorized_action_match": True,
                    "card_trace": [],
                },
                "boundary_sensitivity": {},
                "outcome_or_reward_used_for_replay": False,
            }
        )
    cat2_rows_text = _jsonl_text(conformance_rows)
    cat2_rows.write_text(cat2_rows_text, encoding="utf-8", newline="\n")
    cat2_rows_hash = hashlib.sha256(cat2_rows_text.encode("utf-8")).hexdigest()
    cat2_report_document: dict[str, object] = {
        "schema": "fury_cat2_profile_conformance_report/v1",
        "status": "BOUNDED_DIAGNOSTIC_ONLY",
        "inputs": {
            "transition_dataset": str(transitions.resolve()),
            "transition_dataset_sha256": hashlib.sha256(
                transition_text.encode("utf-8")
            ).hexdigest(),
            "transition_manifest": str(manifest.resolve()),
            "transition_manifest_sha256": manifest_hash,
            "transition_manifest_commit": "PASS",
            "export_session_id": SESSION,
        },
        "profile": {
            "profile_id": 1,
            "profile_name": "BrainOfCat Shadow",
            "execution_semantic_sha256": "a" * 64,
            "source_bundle_sha256": "b" * 64,
            "authority_state": "CURRENT_UNSEALED_SOURCE_PROFILE",
            "shape_gate": "PASS",
            "all_transition_profile_id_name_match": True,
            "transition_profile_id_name_match_rows": len(rows),
            "capture_time_binding": "UNSEALED_ID_NAME_ONLY",
            "capture_time_profile_hash_rows": 0,
            "capture_time_profile_hash_present": False,
        },
        "evidence_boundary": {
            "runtime_label_provenance": "OBSERVED_EXACT_CAT2_SINK_TRACE",
            "replay_provenance": "SOURCE_DERIVED",
            "lua_executed_offline": False,
            "source_derived_is_exact_runtime": False,
            "sample_design": "CLIENT_ACCEPTED_TERMINAL_MAPPED_ACTION_CONDITIONED",
            "no_action_or_wait_rows_available": 0,
            "decision_denominator_available": False,
            "candidate_counterfactual_reward_available": False,
            "performance_comparison_available": False,
        },
        "strict_conformance": {
            "status": "NOT_EVALUABLE",
            "total_rows": len(rows),
            "input_complete_rows": 0,
        },
        "assumption_conditioned_replay": {
            "status": "PASS",
            "evaluated_rows": len(rows),
            "exact_factorized_action_match_rows": len(rows),
            "mismatch_rows": 0,
        },
        "eligibility": {
            "cat2_profile_identity_as_baseline": True,
            "independent_expert_vote_allowed": False,
            "offline_rl_episode_eligible": False,
            "performance_claim_allowed": False,
            "deployment_allowed": False,
        },
        "output": {
            "rows": str(cat2_rows.resolve()),
            "rows_sha256": cat2_rows_hash,
            "row_count": len(rows),
            "row_schema": "fury_cat2_profile_conformance_row/v1",
        },
        "commit": {
            "state": "complete",
            "report_written_last": True,
            "content_addressed": True,
        },
    }
    _write_json(cat2_report, cat2_report_document)
    return transitions, manifest, cat2_report, cat2_rows


def _golden_rows() -> list[dict[str, object]]:
    return [
        _transition(
            1,
            actual=_lanes(off_gcd=["warrior_bloodrage"]),
            candidate=_lanes(off_gcd=["warrior_bloodrage"]),
        ),
        _transition(
            2,
            actual=_lanes(gcd=["warrior_whirlwind"]),
            candidate=_lanes(gcd=["warrior_whirlwind"]),
            gcd=0.73,
        ),
    ]


class FuryShadowSimSeedabilityV1Tests(unittest.TestCase):
    def test_valid_bundle_is_approximation_only_and_all_authority_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            report, rows = build_fury_shadow_sim_seedability(
                *paths,
                rows_output_path=root / "seedability.rows.jsonl",
            )

        self.assertEqual(report["status"], "EXACT_SEEDING_UNAVAILABLE")
        self.assertEqual(
            report["classification_counts"],
            {CLASS_EXACT: 0, CLASS_APPROX_ONLY: 2, CLASS_REJECT: 0, "total": 2},
        )
        self.assertEqual([row["classification"] for row in rows], [
            CLASS_APPROX_ONLY,
            CLASS_APPROX_ONLY,
        ])
        self.assertEqual(
            report["coverage"]["positive_cat2_gcd_with_accepted_gcd_action_rows"],
            1,
        )
        self.assertFalse(report["eligibility"]["counterfactual_reward_eligible"])
        self.assertFalse(report["eligibility"]["training_eligible"])
        self.assertFalse(report["eligibility"]["deployment_allowed"])
        self.assertFalse(rows[0]["eligibility"]["training_eligible"])
        self.assertFalse(rows[0]["bridge_projection"]["approximation_is_restore"])

    def test_fields_keep_gcd_aura_and_proc_unknowns_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            report, rows = build_fury_shadow_sim_seedability(
                *paths,
                rows_output_path=root / "seedability.rows.jsonl",
            )

        second = rows[1]
        self.assertEqual(
            second["field_provenance"]["timers.cat2_reconstructed_gcd_remaining_s"][
                "status"
            ],
            "OBSERVED_CAT2_RECONSTRUCTED",
        )
        self.assertFalse(
            second["field_provenance"]["timers.cat2_reconstructed_gcd_remaining_s"][
                "exact_seed_equivalent"
            ]
        )
        self.assertTrue(second["unknown_mask"]["timers.simulator_gcd_remaining_ms"])
        self.assertTrue(
            second["unknown_mask"]["player.aura_identity_stacks_remaining"]
        )
        self.assertTrue(
            second["unknown_mask"]["target.aura_identity_stacks_remaining"]
        )
        self.assertTrue(
            second["unknown_mask"]["proc.bonereavers_edge_stacks_remaining"]
        )
        self.assertTrue(
            second["unknown_mask"]["proc.crusader_strength_remaining"]
        )
        self.assertIn(
            "POSITIVE_CAT2_GCD_WHILE_GCD_ACTION_WAS_CLIENT_ACCEPTED",
            second["conflicts"],
        )
        self.assertEqual(
            report["mechanic_sensitivity"]["bonereavers_edge"]["capture_status"],
            "UNRESOLVED",
        )
        self.assertEqual(
            report["mechanic_sensitivity"]["crusader"]["capture_status"],
            "UNRESOLVED",
        )

    def test_target_maximum_is_derived_and_never_upgraded_to_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            _, rows = build_fury_shadow_sim_seedability(
                *paths,
                rows_output_path=root / "seedability.rows.jsonl",
            )

        target_maximum = rows[0]["field_provenance"]["target.health_maximum"]
        self.assertEqual(target_maximum["status"], "DERIVED")
        self.assertEqual(target_maximum["value"], 100_000_000.0)
        self.assertFalse(target_maximum["exact_seed_equivalent"])

    def test_unknown_minimum_conditioning_field_classifies_reject(self) -> None:
        rows = _golden_rows()[:1]
        rows[0]["state_before"]["mainHandSwingRemainingKnown"] = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, rows)
            report, projected = build_fury_shadow_sim_seedability(
                *paths,
                rows_output_path=root / "seedability.rows.jsonl",
            )

        self.assertEqual(projected[0]["classification"], CLASS_REJECT)
        self.assertIn(
            "UNKNOWN_MAINHANDSWINGREMAINING",
            projected[0]["hard_reject_reasons"],
        )
        self.assertEqual(report["classification_counts"][CLASS_REJECT], 1)
        self.assertFalse(projected[0]["eligibility"]["approximation_conditioning_eligible"])

    def test_supported_action_in_wrong_lane_is_rejected(self) -> None:
        rows = _golden_rows()[:1]
        rows[0]["actual"]["factorized_action"] = _lanes(
            off_gcd=["warrior_bloodthirst"]
        )
        rows[0]["actual"]["actions"] = [
            {
                "action_id": "warrior_bloodthirst",
                "lane": "off_gcd",
                "client_accepted": True,
                "status": "result",
            }
        ]
        rows[0]["candidate"]["proposal"] = _lanes(
            off_gcd=["warrior_bloodthirst"]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, rows)
            report, projected = build_fury_shadow_sim_seedability(
                *paths,
                rows_output_path=root / "seedability.rows.jsonl",
            )

        self.assertEqual(projected[0]["classification"], CLASS_REJECT)
        self.assertIn(
            "INVALID_ACTUAL_ACTION_LANE",
            projected[0]["hard_reject_reasons"],
        )
        self.assertIn(
            "off_gcd:warrior_bloodthirst",
            projected[0]["action_lane_mismatches"]["actual"],
        )
        self.assertEqual(report["classification_counts"][CLASS_REJECT], 1)
        self.assertFalse(
            projected[0]["eligibility"]["approximation_conditioning_eligible"]
        )

    def test_tampered_transition_dataset_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            paths[0].write_bytes(paths[0].read_bytes() + b"\n")
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "transition dataset SHA256 mismatch"
            ):
                materialize_fury_shadow_sim_seedability(
                    *paths,
                    root / "report.json",
                    root / "rows.jsonl",
                )
            self.assertFalse((root / "report.json").exists())
            self.assertFalse((root / "rows.jsonl").exists())

    def test_incomplete_transition_manifest_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            manifest = json.loads(paths[1].read_text(encoding="utf-8"))
            manifest["commit"]["manifest_written_last"] = False
            _write_json(paths[1], manifest)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "transition manifest commit"
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_tampered_cat2_rows_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            paths[3].write_bytes(paths[3].read_bytes() + b"\n")
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError,
                "Cat2 conformance rows SHA256 mismatch",
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_incomplete_cat2_report_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            report = json.loads(paths[2].read_text(encoding="utf-8"))
            report["commit"]["report_written_last"] = False
            _write_json(paths[2], report)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "Cat2 conformance commit"
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_cat2_transition_hash_binding_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            report = json.loads(paths[2].read_text(encoding="utf-8"))
            report["inputs"]["transition_dataset_sha256"] = "0" * 64
            _write_json(paths[2], report)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError,
                "transition_dataset_sha256 mismatch",
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_unverifiable_sealed_profile_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            report = json.loads(paths[2].read_text(encoding="utf-8"))
            report["profile"]["capture_time_binding"] = (
                "SEALED_SEMANTIC_HASH_MATCH"
            )
            report["profile"]["capture_time_profile_hash_rows"] = 2
            report["profile"]["capture_time_profile_hash_present"] = True
            _write_json(paths[2], report)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError,
                "cannot verify a sealed Cat2 capture-time binding",
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_cat2_assumption_observed_action_must_match_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            rows = [json.loads(line) for line in paths[3].read_text().splitlines()]
            rows[0]["assumption_conditioned"]["observed_factorized_action"] = (
                _lanes(gcd=["warrior_whirlwind"])
            )
            text = _jsonl_text(rows)
            paths[3].write_text(text, encoding="utf-8", newline="\n")
            report = json.loads(paths[2].read_text(encoding="utf-8"))
            report["output"]["rows_sha256"] = hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()
            _write_json(paths[2], report)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError,
                "observed action does not match the transition actual",
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_cat2_identity_order_mismatch_is_rejected_even_with_fresh_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            conformance_rows = [
                json.loads(line)
                for line in paths[3].read_text(encoding="utf-8").splitlines()
            ]
            conformance_rows.reverse()
            text = _jsonl_text(conformance_rows)
            paths[3].write_text(text, encoding="utf-8", newline="\n")
            report = json.loads(paths[2].read_text(encoding="utf-8"))
            report["output"]["rows_sha256"] = hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()
            _write_json(paths[2], report)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "identity/order mismatch"
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_observed_actual_outcome_cannot_be_promoted_to_scalar_reward(self) -> None:
        rows = _golden_rows()[:1]
        rows[0]["observed_actual_immediate_outcome"]["scalar_reward"] = 300.0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, rows)
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "invents a scalar reward"
            ):
                build_fury_shadow_sim_seedability(
                    *paths,
                    rows_output_path=root / "rows.jsonl",
                )

    def test_materialization_is_content_addressed_and_report_is_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            output_report = root / "seedability.report.json"
            output_rows = root / "seedability.rows.jsonl"
            result = materialize_fury_shadow_sim_seedability(
                *paths,
                output_report,
                output_rows,
            )
            report = json.loads(output_report.read_text(encoding="utf-8"))
            actual_rows_hash = hashlib.sha256(output_rows.read_bytes()).hexdigest()

        self.assertEqual(result["exact_rows"], 0)
        self.assertEqual(result["approx_only_rows"], 2)
        self.assertEqual(report["output"]["rows_sha256"], actual_rows_hash)
        self.assertEqual(result["rows_sha256"], actual_rows_hash)
        self.assertEqual(
            report["commit"],
            {
                "state": "complete",
                "report_written_last": True,
                "content_addressed": True,
            },
        )
        self.assertFalse(result["counterfactual_reward_eligible"])
        self.assertFalse(result["training_eligible"])
        self.assertFalse(result["deployment_allowed"])

    def test_outputs_must_not_overwrite_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_bundle(root, _golden_rows())
            with self.assertRaisesRegex(
                FuryShadowSimSeedabilityError, "must not overwrite an input"
            ):
                materialize_fury_shadow_sim_seedability(
                    *paths,
                    paths[0],
                    root / "rows.jsonl",
                )

    def test_current_committed_bundle_remains_twelve_approximation_only(self) -> None:
        report, rows = build_fury_shadow_sim_seedability()
        self.assertEqual(len(rows), 12)
        self.assertEqual(report["classification_counts"][CLASS_EXACT], 0)
        self.assertEqual(report["classification_counts"][CLASS_APPROX_ONLY], 12)
        self.assertEqual(report["classification_counts"][CLASS_REJECT], 0)
        self.assertEqual(report["eligibility"]["exact_state_seed_rows"], 0)
        self.assertFalse(report["eligibility"]["counterfactual_reward_eligible"])
        self.assertFalse(report["eligibility"]["training_eligible"])
        self.assertFalse(report["eligibility"]["deployment_allowed"])


if __name__ == "__main__":
    unittest.main()
