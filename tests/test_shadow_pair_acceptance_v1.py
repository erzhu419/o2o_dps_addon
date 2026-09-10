from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.shadow_pair_acceptance_v1 import (
    SCHEMA,
    audit_shadow_pairs,
    build_shadow_pair_acceptance,
)


def _row(
    session_id: str,
    decision: int,
    *,
    exact: bool = True,
    telemetry: bool = True,
    family: str = "bloodthirst",
    captured_at: float | None = None,
    include_client_cast: bool = True,
) -> dict[str, object]:
    decision_id = f"decision-{decision}"
    spell_ids = {
        "bloodthirst": 23894,
        "whirlwind": 1680,
        "heroic_strike": 25286,
        "cleave": 20569,
        "bloodrage": 2687,
    }
    spell_id = spell_ids[family]
    generation = f"{decision_id}:sink-{decision}"
    captured = float(decision * 10 if captured_at is None else captured_at)
    cast_at = captured + 0.1
    go_at = captured + 0.2
    result_at = captured + 0.21
    attribution = "decision_linked_sink" if exact else "server_observed_interval_lower_confidence"
    sink_actions: list[dict[str, object]] = []
    trace_links: list[dict[str, object]] = []
    if exact:
        sink_actions = [
            {
                "seq": decision,
                "decisionId": decision_id,
                "api": "CastSpellByName",
                "channel": "cast",
                "generation": generation,
            }
        ]
        trace_links = [
            {
                "sequence": decision,
                "expert": "Cat2",
                "completed": True,
                "errored": False,
            }
        ]
    return {
        "schemaVersion": 2,
        "decisionId": decision_id,
        "capturedAt": captured,
        "state": {
            "rage": 25,
            "targetPercentHealth": 75,
            "nearbyEnemies": 1,
            "inCombat": True,
            "targetGUID": "target-1",
            "queuedSwing": "KEEP",
        },
        "activePolicy": {
            "proposal": {"gcd": ["warrior_bloodthirst"], "off_gcd": [], "queue": []}
        },
        "candidateShadow": {
            "executed": False,
            "proposalAvailable": True,
            "proposal": {"gcd": ["warrior_bloodthirst"], "off_gcd": [], "queue": []},
        },
        "expertActual": {
            "observed": True,
            "executed": True,
            "materialized": True,
            "materializedAt": result_at,
            "attribution": attribution,
            "sinkActions": sink_actions,
            "expertTraceLinks": trace_links,
        },
        "shadowSample": {
            "contractVersion": 3,
            "status": "confirmed",
            "materialized": True,
            "counted": True,
            "exportSessionId": session_id,
            "actionFamily": family,
            "armedAt": captured,
            "boundAt": go_at,
            "confirmedAt": result_at,
            "confirmedEvent": "SPELL_GO_SELF",
            "coalescedMacroEvaluations": decision,
        },
        "observedActualOutcome": {
            "status": "complete" if telemetry else "telemetry_unavailable",
            "endReason": "all_actions_terminal" if telemetry else "telemetry_unavailable",
            "actionAttribution": attribution,
            "actions": [
                {
                    "family": family,
                    "status": "result",
                    "sinkSeq": decision,
                    "generation": generation,
                    "expectedSpellIds": [spell_id],
                    "clientCastCount": 1,
                    "clientAccepted": True,
                    "queueEventCount": 0,
                    "queuePopCount": 0,
                }
            ],
            "compactEvents": (
                [
                    {
                        "event": "SPELL_CAST_EVENT",
                        "time": cast_at,
                        "spellID": spell_id,
                        "castSucceeded": True,
                        "castType": 2 if family in {"heroic_strike", "cleave"} else 0,
                    }
                ]
                if include_client_cast
                else []
            )
            + [
                {"event": "SPELL_GO_SELF", "time": go_at, "spellID": spell_id},
                {
                    "event": "SPELL_DAMAGE_EVENT_SELF",
                    "time": result_at,
                    "spellID": spell_id,
                    "amount": 800,
                },
            ],
            "lastEventAt": result_at,
            "telemetry": {
                "available": telemetry,
                "reason": "available" if telemetry else "typed_event_cvars_disabled",
                "missingCVars": [] if telemetry else ["NP_EnableSpellDamageEvents"],
            },
        },
        "provenance": {
            "source_identity": f"brainofcat-shadow-session:{session_id}",
        },
    }


def _tokenize_row(row: dict[str, object]) -> None:
    decision_id = str(row["decisionId"])
    actual = row["expertActual"]
    outcome = row["observedActualOutcome"]
    action = outcome["actions"][0]
    events = outcome["compactEvents"]
    next_ordinal = int(row["decisionId"].split("-")[-1]) * 100
    for event in events:
        if "causalOrdinal" not in event:
            event["causalOrdinal"] = next_ordinal
        next_ordinal = max(next_ordinal + 1, int(event["causalOrdinal"]) + 1)
        event["actionDecisionId"] = decision_id
        event["sinkSeq"] = action["sinkSeq"]
        event["generation"] = action["generation"]
        event["actionSinkSeq"] = action["sinkSeq"]
        event["actionGeneration"] = action["generation"]

    sink = actual["sinkActions"][0]
    first_ordinal = min(event["causalOrdinal"] for event in events)
    issued_at = float(row["capturedAt"]) + 0.01
    issued_ordinal = first_ordinal - 1
    sink["issuedAt"] = issued_at
    sink["issuedOrdinal"] = issued_ordinal
    action["decisionId"] = decision_id
    action["api"] = sink["api"]
    action["issuedAt"] = issued_at
    action["issuedOrdinal"] = issued_ordinal

    cast_events = [event for event in events if event["event"] == "SPELL_CAST_EVENT"]
    successful_casts = [event for event in cast_events if event.get("castSucceeded") is True]
    rejected_casts = [event for event in cast_events if event.get("castSucceeded") is not True]
    go_events = [event for event in events if event["event"] == "SPELL_GO_SELF"]
    damage_events = [event for event in events if event["event"] == "SPELL_DAMAGE_EVENT_SELF"]
    miss_events = [event for event in events if event["event"] == "SPELL_MISS_SELF"]
    failed_events = [event for event in events if event["event"] == "SPELL_FAILED_SELF"]
    queue_events = [event for event in events if event["event"] == "SPELL_QUEUE_EVENT"]
    queue_pops = [event for event in queue_events if event.get("queueEventCode") == 1]
    accepted = successful_casts[-1]
    action.update(
        {
            "clientCastCount": len(cast_events),
            "clientAccepted": True,
            "clientAcceptedAt": accepted["time"],
            "clientAcceptedOrdinal": accepted["causalOrdinal"],
            "clientCastAt": accepted["time"],
            "clientCastOrdinal": accepted["causalOrdinal"],
            "clientRejected": bool(rejected_casts),
            "queueEventCount": len(queue_events),
            "queuePopCount": len(queue_pops),
            "damage": sum(event.get("amount", 0) for event in damage_events),
            "resultCount": len(damage_events) + len(miss_events),
            "missCount": len(miss_events),
            "failedCount": len(failed_events),
            "serverGoCount": len(go_events),
            "lastEventAt": max(event["time"] for event in events),
        }
    )
    if rejected_casts:
        action["clientRejectedAt"] = rejected_casts[-1]["time"]
        action["clientRejectedOrdinal"] = rejected_casts[-1]["causalOrdinal"]
    if go_events:
        action["serverGoAt"] = go_events[-1]["time"]
        action["serverGoOrdinal"] = go_events[-1]["causalOrdinal"]
    result_events = damage_events + miss_events
    if result_events:
        result_ordinals = sorted(event["causalOrdinal"] for event in result_events)
        action["resultFirstOrdinal"] = result_ordinals[0]
        action["resultLastOrdinal"] = result_ordinals[-1]

    actual["sourceKind"] = "expert_trace"
    actual["proposalAvailable"] = False
    actual["proposal"] = {"off_gcd": [], "queue": [], "gcd": []}
    actual["clientAcceptedActionCount"] = 1
    actual["expertTraceLinks"][0]["entry"] = "Cat2.ExecuteConfiguration"
    outcome.update(
        {
            "acceptedActionCount": 1,
            "materializedActionCount": 1,
            "damage": action["damage"],
            "resultCount": action["resultCount"],
            "missCount": action["missCount"],
            "failedCount": action["failedCount"],
            "serverGoCount": action["serverGoCount"],
            "queueEventCount": action["queueEventCount"],
            "queuePopCount": action["queuePopCount"],
            "rageAtDecision": row["state"]["rage"],
            "rageLast": row["state"]["rage"],
            "targetHealthAtDecision": row["state"].setdefault("targetHealth", 10_000),
            "targetHealthLast": row["state"]["targetHealth"] - action["damage"],
            "unsupportedSinkCount": 0,
            "unsupportedSinks": [],
        }
    )


def _add_queue_lifecycle(row: dict[str, object], *, include_pop: bool = True) -> None:
    outcome = row["observedActualOutcome"]
    action = outcome["actions"][0]
    events = outcome["compactEvents"]
    successful_cast = events[0]
    precursor_cast = copy.deepcopy(successful_cast)
    precursor_cast["castSucceeded"] = False
    queue_events = [
        {
            "event": "SPELL_QUEUE_EVENT",
            "time": precursor_cast["time"] + 0.02,
            "spellID": precursor_cast["spellID"],
            "queueEventCode": 0,
        }
    ]
    if include_pop:
        queue_events.append(
            {
                "event": "SPELL_QUEUE_EVENT",
                "time": precursor_cast["time"] + 0.05,
                "spellID": precursor_cast["spellID"],
                "queueEventCode": 1,
            }
        )
    successful_cast["time"] = precursor_cast["time"] + 0.07
    events[0:1] = [precursor_cast, *queue_events, successful_cast]
    action["clientCastCount"] = 2
    action["queueEventCount"] = len(queue_events)
    action["queuePopCount"] = int(include_pop)


def _write_inputs(
    root: Path, rows: list[dict[str, object]]
) -> tuple[Path, Path]:
    journal = root / "pairs.jsonl"
    journal.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    identities = [
        {
            "source": row["provenance"]["source_identity"],
            "decision_id": row["decisionId"],
        }
        for row in rows
    ]
    manifest = root / "pairs.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "brainofcat_shadow_pair_journal",
                "output": str(journal.resolve()),
                "journal_pair_total": len(rows),
                "identities": identities,
            }
        ),
        encoding="utf-8",
    )
    return journal, manifest


class ShadowPairAcceptanceV1Tests(unittest.TestCase):
    def test_old_row_without_client_acceptance_anchor_fails_causal_reward_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("old-session", 1, include_client_cast=False)]
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            self.assertEqual(report["transport_action_gate"]["status"], "PASS")
            self.assertEqual(report["causal_state_action_gate"]["status"], "FAIL")
            self.assertIn(
                "missing_successful_client_cast_acceptance_anchor",
                report["causal_state_action_gate"]["rejection_reasons"],
            )
            outcome = report["outcome_reward_gate"]
            self.assertEqual(outcome["status"], "FAIL")
            self.assertEqual(outcome["evidence"]["telemetry_complete_reward_rows"], 1)
            self.assertEqual(outcome["evidence"]["causally_usable_reward_rows"], 0)
            self.assertIn(
                "causally_attributed_reward_rows_not_usable",
                outcome["rejection_reasons"],
            )

    def test_untokened_same_family_overlap_is_causally_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [
                _row("overlap", 1, captured_at=10.0),
                _row("overlap", 2, captured_at=10.05),
            ]
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=2)
            causal = report["causal_state_action_gate"]

            self.assertEqual(causal["status"], "FAIL")
            self.assertIn(
                "same_family_overlapping_outcome_windows",
                causal["rejection_reasons"],
            )
            self.assertEqual(
                causal["evidence"]["unresolved_same_family_overlap_pair_count"], 1
            )

    def test_tokened_overlapping_queued_next_swing_rows_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [
                _row("tokened-overlap", 1, family="heroic_strike", captured_at=10.0),
                _row("tokened-overlap", 2, family="heroic_strike", captured_at=10.05),
            ]
            for row in rows:
                row["shadowSample"]["contractVersion"] = 4
                _add_queue_lifecycle(row)
                _tokenize_row(row)
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=2)
            causal = report["causal_state_action_gate"]

            self.assertEqual(causal["status"], "PASS")
            self.assertEqual(
                causal["evidence"]["unresolved_same_family_overlap_pair_count"], 0
            )
            self.assertEqual(
                causal["evidence"]["token_disambiguated_same_family_overlap_pair_count"],
                1,
            )
            for path in causal["evidence"]["next_swing_path_diagnostics"]:
                self.assertEqual(path["path"], "queued_lifecycle")
                self.assertEqual(path["client_cast_count"], 2)
                self.assertEqual(path["client_cast_event_count"], 2)
                self.assertEqual(path["successful_client_cast_event_count"], 1)
            self.assertEqual(report["outcome_reward_gate"]["status"], "PASS")

    def test_immediate_next_swing_client_acceptance_needs_no_queue_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("immediate-hs", 1, family="heroic_strike")]
            rows[0]["shadowSample"]["contractVersion"] = 4
            _tokenize_row(rows[0])
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            self.assertEqual(report["causal_state_action_gate"]["status"], "PASS")
            paths = report["causal_state_action_gate"]["evidence"][
                "next_swing_path_diagnostics"
            ]
            self.assertEqual(paths[0]["path"], "immediate_client_acceptance")
            self.assertEqual(paths[0]["code0_count"], 0)

    def test_v4_nonoverlapping_row_requires_every_causal_event_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("untokened-v4", 1)]
            rows[0]["shadowSample"]["contractVersion"] = 4
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)
            causal = report["causal_state_action_gate"]

            self.assertEqual(causal["status"], "FAIL")
            self.assertIn(
                "causal_v4_event_tokens_incomplete", causal["rejection_reasons"]
            )
            self.assertEqual(causal["evidence"]["causal_v4_rows"], 1)
            self.assertEqual(causal["evidence"]["causal_v4_token_complete_rows"], 0)

    def test_queued_next_swing_without_pop_has_specific_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("broken-queue", 1, family="heroic_strike")]
            _add_queue_lifecycle(rows[0], include_pop=False)
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)
            causal = report["causal_state_action_gate"]

            self.assertEqual(causal["status"], "FAIL")
            reasons = causal["evidence"]["next_swing_queue_diagnostics"][0][
                "reasons"
            ]
            self.assertIn("queued_next_swing_missing_code1_pop", reasons)

    def test_queued_next_swing_rejects_successful_cast_before_pop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("wrong-queue-order", 1, family="heroic_strike")]
            rows[0]["shadowSample"]["contractVersion"] = 4
            _add_queue_lifecycle(rows[0])
            events = rows[0]["observedActualOutcome"]["compactEvents"]
            pop = next(
                event
                for event in events
                if event.get("event") == "SPELL_QUEUE_EVENT"
                and event.get("queueEventCode") == 1
            )
            successful = next(
                event
                for event in events
                if event.get("event") == "SPELL_CAST_EVENT"
                and event.get("castSucceeded") is True
            )
            successful["time"] = pop["time"] - 0.01
            _tokenize_row(rows[0])
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)
            diagnostics = report["causal_state_action_gate"]["evidence"][
                "next_swing_queue_diagnostics"
            ]

            self.assertEqual(report["causal_state_action_gate"]["status"], "FAIL")
            self.assertEqual(diagnostics, [])
            self.assertIn(
                "compact_event_time_nonmonotonic",
                report["causal_state_action_gate"]["evidence"]
                ["causal_ambiguity_by_decision_id"][rows[0]["decisionId"]],
            )

    def test_v4_causal_ordinal_orders_same_millisecond_queue_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("same-ms-queue", 1, family="heroic_strike")]
            row = rows[0]
            row["shadowSample"]["contractVersion"] = 4
            row["observedActualOutcome"]["endReason"] = (
                "all_actions_terminal_causal"
            )
            _add_queue_lifecycle(row)
            events = row["observedActualOutcome"]["compactEvents"]
            precursor = events[0]
            code_zero = events[1]
            precursor["time"] = code_zero["time"]
            for causal_ordinal, event in enumerate(events, start=100):
                event["causalOrdinal"] = causal_ordinal
            _tokenize_row(row)
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            self.assertEqual(report["causal_state_action_gate"]["status"], "PASS")
            self.assertEqual(report["outcome_reward_gate"]["status"], "PASS")
            self.assertEqual(
                report["causal_state_action_gate"]["evidence"][
                    "next_swing_queue_diagnostics"
                ],
                [],
            )

    def test_v4_recomputes_damage_instead_of_trusting_action_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            row = _row("forged-damage", 1)
            row["shadowSample"]["contractVersion"] = 4
            _tokenize_row(row)
            row["observedActualOutcome"]["actions"][0]["damage"] = 888_888
            row["observedActualOutcome"]["damage"] = 999_999
            journal, manifest = _write_inputs(root, [row])

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            self.assertEqual(report["causal_state_action_gate"]["status"], "FAIL")
            row_reasons = report["causal_state_action_gate"]["evidence"][
                "causal_ambiguity_by_decision_id"
            ][row["decisionId"]]
            self.assertIn("action_damage_mismatch", row_reasons)
            self.assertIn("outcome_damage_mismatch", row_reasons)
            self.assertEqual(report["outcome_reward_gate"]["status"], "FAIL")

    def test_v4_rejects_reverse_causal_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            row = _row("reverse-ordinal", 1)
            row["shadowSample"]["contractVersion"] = 4
            _tokenize_row(row)
            events = row["observedActualOutcome"]["compactEvents"]
            events[0]["causalOrdinal"], events[-1]["causalOrdinal"] = (
                events[-1]["causalOrdinal"],
                events[0]["causalOrdinal"],
            )
            journal, manifest = _write_inputs(root, [row])

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            row_reasons = report["causal_state_action_gate"]["evidence"][
                "causal_ambiguity_by_decision_id"
            ][row["decisionId"]]
            self.assertIn("compact_event_causal_ordinal_nonmonotonic", row_reasons)
            self.assertEqual(report["outcome_reward_gate"]["status"], "FAIL")

    def test_v4_requires_sink_action_token_bijection_and_state_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            row = _row("bijection", 1)
            row["shadowSample"]["contractVersion"] = 4
            _tokenize_row(row)
            extra_sink = copy.deepcopy(row["expertActual"]["sinkActions"][0])
            extra_sink["seq"] = 99
            extra_sink["generation"] = "unmatched-generation"
            row["expertActual"]["sinkActions"].append(extra_sink)
            row["observedActualOutcome"]["rageAtDecision"] = 99
            journal, manifest = _write_inputs(root, [row])

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=1)

            row_reasons = report["causal_state_action_gate"]["evidence"][
                "causal_ambiguity_by_decision_id"
            ][row["decisionId"]]
            self.assertIn("sink_action_token_bijection_failed", row_reasons)
            self.assertIn("outcome_rage_decision_anchor_mismatch", row_reasons)

    def test_duplicate_physical_event_fails_even_with_conflicting_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("duplicate-event", 1), _row("duplicate-event", 2)]
            for row in rows:
                _tokenize_row(row)
            duplicate = copy.deepcopy(rows[0]["observedActualOutcome"]["compactEvents"][1])
            duplicate["actionDecisionId"] = rows[1]["decisionId"]
            duplicate["sinkSeq"] = rows[1]["observedActualOutcome"]["actions"][0]["sinkSeq"]
            duplicate["generation"] = rows[1]["observedActualOutcome"]["actions"][0]["generation"]
            rows[1]["observedActualOutcome"]["compactEvents"].append(duplicate)
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(journal, manifest, expected_pairs=2)

            self.assertIn(
                "duplicate_compact_event_across_rows",
                report["causal_state_action_gate"]["rejection_reasons"],
            )

    def test_separates_transport_pass_from_causal_and_outcome_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [
                _row("weak-session", decision, exact=False, telemetry=False)
                for decision in range(1, 4)
            ]
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(
                journal, manifest, expected_pairs=3
            )

            self.assertEqual(report["schema"], SCHEMA)
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["transport_action_gate"]["status"], "PASS")
            self.assertEqual(report["causal_state_action_gate"]["status"], "FAIL")
            self.assertEqual(report["outcome_reward_gate"]["status"], "FAIL")
            self.assertFalse(report["deployment_allowed"])
            causal = report["causal_state_action_gate"]
            self.assertEqual(
                causal["evidence"]["lower_confidence_actual_attribution_rows"], 3
            )
            self.assertEqual(causal["evidence"]["exact_sink_and_trace_rows"], 0)
            self.assertIn(
                "missing_exact_sink_actions", causal["rejection_reasons"]
            )
            self.assertIn(
                "missing_exact_expert_trace_links", causal["rejection_reasons"]
            )
            outcome = report["outcome_reward_gate"]
            self.assertEqual(outcome["evidence"]["typed_telemetry_available_rows"], 0)
            self.assertEqual(outcome["evidence"]["reward_usable_rows"], 0)
            self.assertEqual(
                report["distributions"]["telemetry_reason"],
                {"typed_event_cvars_disabled": 3},
            )
            self.assertEqual(
                report["distributions"]["missing_cvar"],
                {"NP_EnableSpellDamageEvents": 3},
            )

    def test_exact_typed_rows_pass_all_evidence_gates_but_not_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("exact-session", decision) for decision in range(1, 3)]
            journal, manifest = _write_inputs(root, rows)

            report, output = audit_shadow_pairs(
                journal,
                manifest,
                output_path=root / "report.json",
                expected_pairs=2,
            )

            self.assertEqual(report["status"], "PASS")
            self.assertTrue(report["transport_action_gate"]["passed"])
            self.assertTrue(report["causal_state_action_gate"]["passed"])
            self.assertTrue(report["outcome_reward_gate"]["passed"])
            self.assertFalse(report["deployment_allowed"])
            self.assertEqual(
                report["deployment_rejection_reasons"][0],
                "shadow_journal_does_not_establish_candidate_superiority",
            )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["schema"], SCHEMA
            )

    def test_defaults_to_latest_session_while_validating_global_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [
                _row("old-session", 1, exact=False, telemetry=False),
                _row("new-session", 2),
                _row("new-session", 3),
            ]
            journal, manifest = _write_inputs(root, rows)

            report = build_shadow_pair_acceptance(
                journal, manifest, expected_pairs=2
            )

            self.assertEqual(
                report["input"]["selected_export_session_id"], "new-session"
            )
            self.assertEqual(report["transport_action_gate"]["status"], "PASS")
            self.assertEqual(
                report["transport_action_gate"]["evidence"]["journal_pair_total"], 3
            )
            self.assertEqual(report["causal_state_action_gate"]["status"], "PASS")

    def test_transport_rejects_candidate_execution_and_manifest_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rows = [_row("bad-session", 1)]
            rows[0]["candidateShadow"]["executed"] = True
            journal, manifest_path = _write_inputs(root, rows)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["identities"][0]["decision_id"] = "different-decision"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = build_shadow_pair_acceptance(
                journal, manifest_path, expected_pairs=1
            )

            gate = report["transport_action_gate"]
            self.assertEqual(gate["status"], "FAIL")
            self.assertIn("candidate_execution_detected", gate["rejection_reasons"])
            self.assertIn("manifest_identity_set_mismatch", gate["rejection_reasons"])
            self.assertFalse(report["deployment_allowed"])


if __name__ == "__main__":
    unittest.main()
