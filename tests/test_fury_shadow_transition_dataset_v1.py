from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_shadow_transition_dataset_v1 import (
    FuryShadowTransitionDatasetError,
    RECORD_SCHEMA,
    build_fury_shadow_transition_dataset,
    materialize_fury_shadow_transition_dataset,
)
from o2o_dps.shadow_pair_acceptance_v1 import build_shadow_pair_acceptance


SESSION = "shadow-test-v4"


def _proposal(*, off_gcd: list[str] | None = None, queue: list[str] | None = None,
              gcd: list[str] | None = None) -> dict[str, list[str]]:
    return {
        "off_gcd": list(off_gcd or []),
        "queue": list(queue or []),
        "gcd": list(gcd or []),
    }


def _row(
    decision: int,
    *,
    captured_at: float,
    family: str,
    candidate_proposal: dict[str, list[str]],
    coalesced: int = 1,
) -> dict[str, object]:
    decision_id = f"decision-{decision}"
    family_contract = {
        "whirlwind": ("warrior_whirlwind", 1680, 0),
        "bloodrage": ("warrior_bloodrage", 2687, 1),
        "heroic_strike": ("warrior_heroic_strike", 25286, 2),
    }
    _, spell_id, cast_type = family_contract[family]
    issued_at = captured_at + 0.01
    cast_at = captured_at + 0.02
    go_at = captured_at + 0.03
    result_at = captured_at + 0.04
    base_ordinal = decision * 100
    generation = f"{decision_id}:sink-1:ord-{base_ordinal}"
    damage = 0 if family == "bloodrage" else 800
    action_status = "go_no_result" if family == "bloodrage" else "result"
    recorded_proposal = _proposal(gcd=["warrior_whirlwind"])
    compact_events: list[dict[str, object]] = [
        {
            "event": "SPELL_CAST_EVENT",
            "time": cast_at,
            "spellID": spell_id,
            "castSucceeded": True,
            "castType": cast_type,
            "causalOrdinal": base_ordinal + 1,
            "actionDecisionId": decision_id,
            "sinkSeq": 1,
            "generation": generation,
            "telemetrySource": "always_on_typed",
        },
        {
            "event": "SPELL_GO_SELF",
            "time": go_at,
            "spellID": spell_id,
            "causalOrdinal": base_ordinal + 2,
            "actionDecisionId": decision_id,
            "sinkSeq": 1,
            "generation": generation,
            "telemetrySource": "always_on_typed",
        },
    ]
    if family != "bloodrage":
        compact_events.append(
            {
                "event": "SPELL_DAMAGE_EVENT_SELF",
                "time": result_at,
                "spellID": spell_id,
                "amount": damage,
                "causalOrdinal": base_ordinal + 3,
                "actionDecisionId": decision_id,
                "sinkSeq": 1,
                "generation": generation,
                "telemetrySource": "always_on_typed",
            }
        )
    end_at = go_at if family == "bloodrage" else result_at
    target_before = 10_000
    target_after = target_before - damage
    return {
        "schemaVersion": 2,
        "decisionId": decision_id,
        "runtimeSessionId": "runtime-test",
        "capturedAt": captured_at,
        "mode": "shadow",
        "state": {
            "classFile": "WARRIOR",
            "rage": 25 + decision,
            "targetHealth": target_before,
            "targetPercentHealth": 75,
            "nearbyEnemies": 1,
            "targetGUID": "target-1",
            "inCombat": True,
            "targetExists": True,
            "targetCanAttack": True,
            "targetIsDead": False,
        },
        "proposal": recorded_proposal,
        "attempts": [],
        "stopped": False,
        "profileId": "profile-test",
        "profileName": "BrainOfCat Shadow",
        "policyId": "fury_warrior_rule_baseline_v1",
        "policyKind": "hand_authored_baseline_not_trained",
        "activePolicy": {
            "requestedPolicyId": "fury_warrior_rule_baseline_v1",
            "policyId": "fury_warrior_rule_baseline_v1",
            "policyKind": "hand_authored_baseline_not_trained",
            "proposalAvailable": True,
            "proposal": recorded_proposal,
            "executionRequested": False,
            "attempts": [],
            "stopped": False,
        },
        "candidateShadow": {
            "requestedPolicyId": "fury_combined_candidate_shadow_v1",
            "policyId": "fury_combined_candidate_shadow_v1",
            "proposalAvailable": True,
            "proposal": candidate_proposal,
            "executed": False,
        },
        "expertActual": {
            "observed": True,
            "sourceKind": "expert_trace",
            "proposalAvailable": False,
            "proposal": _proposal(),
            "executed": True,
            "clientAcceptedActionCount": 1,
            "materialized": True,
            "materializedAt": end_at,
            "attribution": "expert_sink_causal_v4",
            "sinkActions": [
                {
                    "seq": 1,
                    "decisionId": decision_id,
                    "api": "CastSpellByName",
                    "channel": "cast",
                    "args": {"arg1": family},
                    "generation": generation,
                    "issuedAt": issued_at,
                    "issuedOrdinal": base_ordinal,
                }
            ],
            "serverObservedActions": [],
            "expertTraceLinks": [
                {
                    "sequence": decision,
                    "expert": "Cat2",
                    "entry": "Cat2.ExecuteConfiguration",
                    "completed": True,
                    "errored": False,
                }
            ],
        },
        "shadowSample": {
            "contractVersion": 4,
            "status": "confirmed",
            "materialized": True,
            "counted": True,
            "actionAttribution": "expert_sink_causal_v4",
            "actionFamily": family,
            "armedAt": captured_at,
            "boundAt": cast_at,
            "confirmedAt": end_at,
            "confirmedEvent": (
                "SPELL_GO_SELF"
                if family == "bloodrage"
                else "SPELL_DAMAGE_EVENT_SELF"
            ),
            "outcomeKind": "server_go" if family == "bloodrage" else "damage",
            "coalescedMacroEvaluations": coalesced,
            "exportSessionId": SESSION,
        },
        "observedActualOutcome": {
            "observedOnly": True,
            "attribution": "expert_actual_only_not_candidate_counterfactual",
            "actionAttribution": "expert_sink_causal_v4",
            "status": "complete",
            "endReason": "all_actions_terminal_causal",
            "causalValidated": True,
            "windowSeconds": 6,
            "actions": [
                {
                    "family": family,
                    "status": action_status,
                    "decisionId": decision_id,
                    "sinkSeq": 1,
                    "generation": generation,
                    "expectedSpellIds": [spell_id],
                    "issuedAt": issued_at,
                    "issuedOrdinal": base_ordinal,
                    "api": "CastSpellByName",
                    "clientCastCount": 1,
                    "clientCastType": cast_type,
                    "clientAccepted": True,
                    "clientAcceptedAt": cast_at,
                    "clientAcceptedOrdinal": base_ordinal + 1,
                    "clientCastAt": cast_at,
                    "clientCastOrdinal": base_ordinal + 1,
                    "clientRejected": False,
                    "queueEventCount": 0,
                    "queuePopCount": 0,
                    "damage": damage,
                    "resultCount": int(family != "bloodrage"),
                    "missCount": 0,
                    "failedCount": 0,
                    "serverGoCount": 1,
                    "serverGoAt": go_at,
                    "serverGoOrdinal": base_ordinal + 2,
                    "lastEventAt": end_at,
                    **(
                        {
                            "resultFirstOrdinal": base_ordinal + 3,
                            "resultLastOrdinal": base_ordinal + 3,
                        }
                        if family != "bloodrage"
                        else {}
                    ),
                }
            ],
            "eventSequences": [],
            "compactEvents": compact_events,
            "lastEventAt": end_at,
            "damage": damage,
            "acceptedActionCount": 1,
            "materializedActionCount": 1,
            "resultCount": int(family != "bloodrage"),
            "missCount": 0,
            "failedCount": 0,
            "serverGoCount": 1,
            "queueEventCount": 0,
            "queuePopCount": 0,
            "rageAtDecision": 25 + decision,
            "rageLast": 25 + decision,
            "targetHealthAtDecision": target_before,
            "targetHealthLast": target_after,
            "unsupportedSinkCount": 0,
            "unsupportedSinks": [],
            "telemetry": {
                "available": True,
                "source": "always_on_typed",
                "reason": "available",
                "registeredEventCount": 10,
                "missingCVars": [],
            },
        },
        "provenance": {
            "source_identity": f"brainofcat-shadow-session:{SESSION}",
        },
    }


def _write_source_bundle(
    root: Path, rows: list[dict[str, object]]
) -> tuple[Path, Path, Path]:
    journal = root / "pairs.jsonl"
    journal.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = root / "pairs.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "brainofcat_shadow_pair_journal",
                "output": str(journal.resolve()),
                "journal_pair_total": len(rows),
                "identities": [
                    {
                        "source": row["provenance"]["source_identity"],
                        "decision_id": row["decisionId"],
                    }
                    for row in rows
                ],
            }
        ),
        encoding="utf-8",
    )
    acceptance_document = build_shadow_pair_acceptance(
        journal,
        manifest,
        expected_pairs=len(rows),
        export_session_id=SESSION,
    )
    if acceptance_document["status"] != "PASS":
        raise AssertionError(acceptance_document)
    acceptance = root / "acceptance.json"
    acceptance.write_text(
        json.dumps(acceptance_document, indent=2) + "\n", encoding="utf-8"
    )
    return journal, manifest, acceptance


class FuryShadowTransitionDatasetV1Tests(unittest.TestCase):
    def test_materializes_sorted_bounded_fragments_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [
                _row(
                    2,
                    captured_at=20,
                    family="bloodrage",
                    candidate_proposal=_proposal(),
                    coalesced=9,
                ),
                _row(
                    1,
                    captured_at=10,
                    family="whirlwind",
                    candidate_proposal=_proposal(gcd=["warrior_whirlwind"]),
                ),
            ]
            journal, manifest, acceptance = _write_source_bundle(root, rows)
            source_before = journal.read_bytes()
            output = root / "online_training" / "transitions.jsonl"
            output_manifest = root / "online_training" / "transitions.manifest.json"
            report = root / "reports" / "transitions.json"

            first = materialize_fury_shadow_transition_dataset(
                journal,
                manifest,
                acceptance,
                session_id=SESSION,
                output_path=output,
                output_manifest_path=output_manifest,
                report_path=report,
            )
            first_bytes = (output.read_bytes(), output_manifest.read_bytes(), report.read_bytes())
            second = materialize_fury_shadow_transition_dataset(
                journal,
                manifest,
                acceptance,
                session_id=SESSION,
                output_path=output,
                output_manifest_path=output_manifest,
                report_path=report,
            )

            self.assertEqual(first, second)
            self.assertEqual(
                first_bytes,
                (output.read_bytes(), output_manifest.read_bytes(), report.read_bytes()),
            )
            self.assertEqual(journal.read_bytes(), source_before)
            projected = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["schema"] for row in projected], [RECORD_SCHEMA] * 2)
            self.assertEqual(
                [row["identity"]["decision_id"] for row in projected],
                ["decision-1", "decision-2"],
            )
            self.assertEqual(projected[0]["actual"]["actions"][0]["lane"], "gcd")
            self.assertEqual(projected[1]["actual"]["actions"][0]["lane"], "off_gcd")
            self.assertNotIn("policy_id", projected[0]["actual"])
            self.assertEqual(
                projected[0]["actual"]["executor"]["kind"],
                "cat2_configuration_card_stack",
            )
            self.assertIsNone(
                projected[0]["actual"]["executor"]["executed_policy_id"]
            )
            self.assertFalse(projected[0]["recorded_active_policy"]["executed"])
            self.assertIsNone(
                projected[0]["recorded_active_policy"]["counterfactual_outcome"]
            )
            self.assertEqual(
                projected[1]["observed_actual_immediate_outcome"]["damage"], 0
            )
            self.assertIsNone(projected[0]["candidate"]["counterfactual_outcome"])
            self.assertFalse(
                projected[0]["eligibility"]["candidate_counterfactual_reward_available"]
            )
            self.assertFalse(projected[0]["eligibility"]["offline_rl_episode_eligible"])
            self.assertFalse(projected[0]["eligibility"]["deployment_allowed"])
            self.assertFalse(
                projected[1]["provenance"][
                    "coalesced_macro_evaluations_used_as_weight"
                ]
            )
            report_document = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                report_document["coverage"]["proposal_agreement"]
                ["candidate_vs_actual"]["contains_all_actual_actions_rows"],
                1,
            )
            self.assertEqual(
                report_document["coverage"]["proposal_agreement"]
                ["candidate_vs_actual"]["missing_actual_action_rows"],
                1,
            )
            self.assertEqual(
                report_document["quality"][
                    "source_journal_capture_order_inversion_count"
                ],
                1,
            )

    def test_rejects_any_failed_acceptance_gate_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            journal, manifest, acceptance = _write_source_bundle(
                root,
                [
                    _row(
                        1,
                        captured_at=10,
                        family="whirlwind",
                        candidate_proposal=_proposal(gcd=["warrior_whirlwind"]),
                    )
                ],
            )
            document = json.loads(acceptance.read_text(encoding="utf-8"))
            document["outcome_reward_gate"]["status"] = "FAIL"
            document["outcome_reward_gate"]["passed"] = False
            acceptance.write_text(json.dumps(document), encoding="utf-8")
            output = root / "output.jsonl"

            with self.assertRaises(FuryShadowTransitionDatasetError) as raised:
                materialize_fury_shadow_transition_dataset(
                    journal,
                    manifest,
                    acceptance,
                    output_path=output,
                    output_manifest_path=root / "output.manifest.json",
                    report_path=root / "report.json",
                )

            self.assertIn("outcome_reward_gate", str(raised.exception))
            self.assertFalse(output.exists())

    def test_revalidates_v4_candidate_and_causal_tokens_after_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original = _row(
                1,
                captured_at=10,
                family="whirlwind",
                candidate_proposal=_proposal(gcd=["warrior_whirlwind"]),
            )
            journal, manifest, acceptance = _write_source_bundle(root, [original])

            mutations = [
                ("contract_version", lambda row: row[
                    "shadowSample"
                ].__setitem__("contractVersion", 3)),
                ("candidate_execution", lambda row: row[
                    "candidateShadow"
                ].__setitem__("executed", True)),
                ("missing_ordinal", lambda row: row["observedActualOutcome"][
                    "compactEvents"
                ][0].pop("causalOrdinal")),
            ]
            for mutation_name, mutate in mutations:
                with self.subTest(mutation_name=mutation_name):
                    mutated = copy.deepcopy(original)
                    mutate(mutated)
                    journal.write_text(json.dumps(mutated) + "\n", encoding="utf-8")
                    with self.assertRaises(FuryShadowTransitionDatasetError) as raised:
                        build_fury_shadow_transition_dataset(
                            journal, manifest, acceptance
                        )
                    self.assertIn(
                        "content-bound to the current journal", str(raised.exception)
                    )

    def test_rejects_stale_acceptance_when_journal_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = _row(
                1,
                captured_at=10,
                family="whirlwind",
                candidate_proposal=_proposal(gcd=["warrior_whirlwind"]),
            )
            journal, manifest, acceptance = _write_source_bundle(root, [first])
            second = _row(
                2,
                captured_at=20,
                family="bloodrage",
                candidate_proposal=_proposal(off_gcd=["warrior_bloodrage"]),
            )
            journal.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n",
                encoding="utf-8",
            )
            manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_document["journal_pair_total"] = 2
            manifest_document["identities"].append(
                {
                    "source": second["provenance"]["source_identity"],
                    "decision_id": second["decisionId"],
                }
            )
            manifest.write_text(json.dumps(manifest_document), encoding="utf-8")

            with self.assertRaises(FuryShadowTransitionDatasetError) as raised:
                build_fury_shadow_transition_dataset(journal, manifest, acceptance)

            self.assertIn("content-bound to the current journal", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
