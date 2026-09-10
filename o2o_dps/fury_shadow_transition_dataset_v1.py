"""Materialize one accepted Fury Shadow session as bounded transition fragments.

The source journal contains an exact state snapshot, the action actually sent by
Cat2's post-Brain profile cards, two policy proposals that remained inactive in
Shadow mode, and typed events causally attributed to the actual action.  The
first inactive proposal is the currently registered hand-written Brain policy;
the second is the research candidate.  Neither proposal is the executor of the
observed Cat2 sink action.  The journal also does *not* contain either policy's
counterfactual result or a complete post-action state.  This consumer therefore
emits behavior labels and observed immediate outcomes while keeping offline-RL
episode/TD eligibility closed.

The builder is deliberately fail-closed.  It only consumes the exact session
named by a current three-gate PASS acceptance report, revalidates the journal
and manifest counts, requires causal contract v4, and refuses any candidate
execution.  Rows are ordered by capture time and causal ordinal, never by JSONL
completion/export order.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .shadow_pair_acceptance_v1 import (
    SCHEMA as ACCEPTANCE_SCHEMA,
    audit_v4_outcome_integrity,
    build_shadow_pair_acceptance,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOURNAL = (
    PROJECT_ROOT
    / "offline_data"
    / "online_decisions"
    / "brainofcat_shadow_pairs_v2.jsonl"
)
DEFAULT_JOURNAL_MANIFEST = DEFAULT_JOURNAL.with_suffix(".manifest.json")
DEFAULT_ACCEPTANCE = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_shadow_pair_acceptance_v1.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "online_training"
    / "fury_shadow_transitions_v1.jsonl"
)
DEFAULT_OUTPUT_MANIFEST = DEFAULT_OUTPUT.with_suffix(".manifest.json")
DEFAULT_REPORT = (
    PROJECT_ROOT
    / "offline_data"
    / "reports"
    / "fury_shadow_transition_dataset_v1.json"
)

RECORD_SCHEMA = "fury_shadow_transition_fragment/v1"
MANIFEST_SCHEMA = "fury_shadow_transition_dataset_manifest/v1"
REPORT_SCHEMA = "fury_shadow_transition_dataset_report/v1"
SOURCE_MANIFEST_KIND = "brainofcat_shadow_pair_journal"
EXPECTED_CANDIDATE_POLICY_ID = "fury_combined_candidate_shadow_v1"
EXPECTED_RECORDED_POLICY_ID = "fury_warrior_rule_baseline_v1"
LANES = ("off_gcd", "queue", "gcd")
POLICY_ACTION_IDS = frozenset(
    {
        "warrior_bloodrage",
        "warrior_bloodthirst",
        "warrior_whirlwind",
        "warrior_execute",
        "warrior_heroic_strike",
        "warrior_cleave",
        "warrior_o2o_cancel_queue",
        "warrior_o2o_cleave_40",
    }
)
CAUSAL_EVENTS = frozenset(
    {
        "SPELL_CAST_EVENT",
        "SPELL_QUEUE_EVENT",
        "SPELL_GO_SELF",
        "SPELL_FAILED_SELF",
        "SPELL_DAMAGE_EVENT_SELF",
        "SPELL_MISS_SELF",
        "UNIT_CASTEVENT",
        "AUTO_ATTACK_SELF",
    }
)
FAMILY_ACTION_IDS = {
    "bloodrage": "warrior_bloodrage",
    "bloodthirst": "warrior_bloodthirst",
    "whirlwind": "warrior_whirlwind",
    "execute": "warrior_execute",
    "heroic_strike": "warrior_heroic_strike",
    "cleave": "warrior_cleave",
    "slam": "warrior_slam",
    "auto_attack": "warrior_auto_attack",
}
CAST_TYPE_LANES = {0: "gcd", 1: "off_gcd", 2: "queue"}


class FuryShadowTransitionDatasetError(RuntimeError):
    """An accepted Shadow input or requested output violates the v1 contract."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise FuryShadowTransitionDatasetError(
            f"cannot read {label} {path}: {error}"
        ) from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryShadowTransitionDatasetError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryShadowTransitionDatasetError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryShadowTransitionDatasetError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FuryShadowTransitionDatasetError(f"{label} must be an integer")
    if positive and value <= 0:
        raise FuryShadowTransitionDatasetError(f"{label} must be positive")
    return value


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise FuryShadowTransitionDatasetError(f"{label} must be a finite number")
    result = float(value)
    if nonnegative and result < 0:
        raise FuryShadowTransitionDatasetError(f"{label} must not be negative")
    return result


def _optional_number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return float(value)
    return None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite_json
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise FuryShadowTransitionDatasetError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryShadowTransitionDatasetError(f"{label} must contain an object")
    return value


def _load_journal(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, parse_constant=_reject_nonfinite_json)
                except (json.JSONDecodeError, ValueError) as error:
                    raise FuryShadowTransitionDatasetError(
                        f"invalid JSON at {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(value, dict):
                    raise FuryShadowTransitionDatasetError(
                        f"{path}:{line_number} must contain an object"
                    )
                value = dict(value)
                value["_source_journal_line"] = line_number
                rows.append(value)
    except (OSError, UnicodeError) as error:
        raise FuryShadowTransitionDatasetError(
            f"cannot read Shadow journal {path}: {error}"
        ) from error
    if not rows:
        raise FuryShadowTransitionDatasetError("Shadow journal is empty")
    return rows


def _declared_path(value: Any, owner: Path, label: str) -> Path:
    supplied = _text(value, label)
    path = Path(supplied).expanduser()
    if not path.is_absolute():
        path = owner.parent / path
    return path.resolve()


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    provenance = _mapping(row.get("provenance"), "row.provenance")
    return (
        _text(provenance.get("source_identity"), "row.provenance.source_identity"),
        _text(row.get("decisionId"), "row.decisionId"),
    )


def _manifest_identities(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    values = _list(manifest.get("identities"), "journal manifest identities")
    identities: list[tuple[str, str]] = []
    for index, value in enumerate(values, start=1):
        item = _mapping(value, f"journal manifest identities[{index}]")
        identities.append(
            (
                _text(item.get("source"), f"journal manifest identities[{index}].source"),
                _text(
                    item.get("decision_id"),
                    f"journal manifest identities[{index}].decision_id",
                ),
            )
        )
    return identities


def _normalized_proposal(value: Any, label: str) -> dict[str, list[str]]:
    proposal = _mapping(value, label)
    normalized: dict[str, list[str]] = {}
    for lane in LANES:
        actions = _list(proposal.get(lane), f"{label}.{lane}")
        if any(not isinstance(action, str) or not action for action in actions):
            raise FuryShadowTransitionDatasetError(
                f"{label}.{lane} action IDs must be non-empty strings"
            )
        if len(set(actions)) != len(actions):
            raise FuryShadowTransitionDatasetError(
                f"{label}.{lane} contains a duplicate action ID"
            )
        unknown = sorted(set(actions) - POLICY_ACTION_IDS)
        if unknown:
            raise FuryShadowTransitionDatasetError(
                f"{label}.{lane} contains unsupported action IDs {unknown!r}"
            )
        normalized[lane] = list(actions)
    flattened = [action for lane in LANES for action in normalized[lane]]
    if len(set(flattened)) != len(flattened):
        raise FuryShadowTransitionDatasetError(
            f"{label} repeats an action ID across execution lanes"
        )
    return normalized


def _validate_state(state: Mapping[str, Any], decision_id: str) -> None:
    if state.get("classFile") != "WARRIOR":
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: state.classFile must equal WARRIOR"
        )
    for field, upper in (("rage", 100.0), ("targetPercentHealth", 100.0)):
        value = _number(state.get(field), f"{decision_id}.state.{field}")
        if value < 0 or value > upper:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: state.{field} is outside [0, {upper:g}]"
            )
    target_health = _number(
        state.get("targetHealth"), f"{decision_id}.state.targetHealth"
    )
    if target_health < 0:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: state.targetHealth must not be negative"
        )
    nearby = state.get("nearbyEnemies")
    if not isinstance(nearby, int) or isinstance(nearby, bool) or nearby < 0:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: state.nearbyEnemies must be a nonnegative integer"
        )
    for field in ("inCombat", "targetExists", "targetCanAttack", "targetIsDead"):
        if not isinstance(state.get(field), bool):
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: state.{field} must be boolean"
            )


def _proposal_agreement(
    actual_proposal: Mapping[str, Sequence[str]],
    proposed: Mapping[str, Sequence[str]],
) -> dict[str, bool]:
    actual_ids = [action_id for lane in LANES for action_id in actual_proposal[lane]]
    proposed_ids = [action_id for lane in LANES for action_id in proposed[lane]]
    actual_id_set = set(actual_ids)
    proposed_id_set = set(proposed_ids)
    return {
        "contains_any_actual_action": bool(actual_id_set & proposed_id_set),
        "contains_all_actual_actions": actual_id_set <= proposed_id_set,
        "exact_factorized_action_match": actual_proposal == proposed,
        "single_action_exact_match": (
            len(actual_ids) == 1
            and len(proposed_ids) == 1
            and actual_ids == proposed_ids
        ),
    }


def _state_bucket(value: float | None, bounds: Sequence[tuple[float, str]]) -> str:
    if value is None:
        return "unknown"
    for upper, label in bounds:
        if value <= upper:
            return label
    return bounds[-1][1]


def _validate_acceptance(
    acceptance: Mapping[str, Any],
    *,
    acceptance_path: Path,
    journal_path: Path,
    manifest_path: Path,
    journal_rows: Sequence[Mapping[str, Any]],
    journal_manifest: Mapping[str, Any],
    requested_session_id: str | None,
) -> str:
    if acceptance.get("schema") != ACCEPTANCE_SCHEMA:
        raise FuryShadowTransitionDatasetError(
            f"acceptance report must use schema {ACCEPTANCE_SCHEMA}"
        )
    if acceptance.get("status") != "PASS":
        raise FuryShadowTransitionDatasetError("acceptance report status is not PASS")
    for gate_name in (
        "transport_action_gate",
        "causal_state_action_gate",
        "outcome_reward_gate",
    ):
        gate = _mapping(acceptance.get(gate_name), f"acceptance.{gate_name}")
        if gate.get("status") != "PASS" or gate.get("passed") is not True:
            raise FuryShadowTransitionDatasetError(
                f"acceptance {gate_name} is not an explicit PASS"
            )
    if acceptance.get("deployment_allowed") is not False:
        raise FuryShadowTransitionDatasetError(
            "accepted Shadow evidence must keep deployment_allowed=false"
        )

    acceptance_input = _mapping(acceptance.get("input"), "acceptance.input")
    if _declared_path(
        acceptance_input.get("journal"), acceptance_path,
        "acceptance.input.journal",
    ) != journal_path:
        raise FuryShadowTransitionDatasetError(
            "acceptance report refers to a different journal"
        )
    if _declared_path(
        acceptance_input.get("manifest"), acceptance_path,
        "acceptance.input.manifest",
    ) != manifest_path:
        raise FuryShadowTransitionDatasetError(
            "acceptance report refers to a different journal manifest"
        )
    if acceptance_input.get("journal_sha256") != _sha256_bytes(
        _read_bytes(journal_path, "journal")
    ):
        raise FuryShadowTransitionDatasetError(
            "acceptance report is not content-bound to the current journal"
        )
    if acceptance_input.get("manifest_sha256") != _sha256_bytes(
        _read_bytes(manifest_path, "journal manifest")
    ):
        raise FuryShadowTransitionDatasetError(
            "acceptance report is not content-bound to the current journal manifest"
        )
    selected_session = _text(
        acceptance_input.get("selected_export_session_id"),
        "acceptance.input.selected_export_session_id",
    )
    if requested_session_id is not None and requested_session_id != selected_session:
        raise FuryShadowTransitionDatasetError(
            "requested session does not match the accepted session"
        )

    if journal_manifest.get("schema_version") != 2:
        raise FuryShadowTransitionDatasetError(
            "journal manifest schema_version must equal 2"
        )
    if journal_manifest.get("kind") != SOURCE_MANIFEST_KIND:
        raise FuryShadowTransitionDatasetError(
            f"journal manifest kind must equal {SOURCE_MANIFEST_KIND}"
        )
    if _declared_path(
        journal_manifest.get("output"), manifest_path, "journal manifest output"
    ) != journal_path:
        raise FuryShadowTransitionDatasetError(
            "journal manifest output does not match the selected journal"
        )
    declared_total = _integer(
        journal_manifest.get("journal_pair_total"),
        "journal manifest journal_pair_total",
    )
    if declared_total != len(journal_rows):
        raise FuryShadowTransitionDatasetError(
            "journal row count does not match its manifest"
        )
    journal_identities = [_row_identity(row) for row in journal_rows]
    declared_identities = _manifest_identities(journal_manifest)
    if len(set(journal_identities)) != len(journal_identities):
        raise FuryShadowTransitionDatasetError(
            "journal contains duplicate composite identities"
        )
    if Counter(journal_identities) != Counter(declared_identities):
        raise FuryShadowTransitionDatasetError(
            "journal identities do not match its manifest"
        )

    transport = _mapping(
        acceptance.get("transport_action_gate"), "acceptance.transport_action_gate"
    )
    evidence = _mapping(transport.get("evidence"), "acceptance transport evidence")
    accepted_total = _integer(
        evidence.get("journal_pair_total"),
        "acceptance transport evidence journal_pair_total",
    )
    if accepted_total != len(journal_rows):
        raise FuryShadowTransitionDatasetError(
            "acceptance report is stale relative to the journal"
        )
    selected_count = sum(
        1
        for row in journal_rows
        if _mapping(row.get("shadowSample"), "row.shadowSample").get(
            "exportSessionId"
        )
        == selected_session
    )
    accepted_selected = _integer(
        evidence.get("selected_pair_count"),
        "acceptance transport evidence selected_pair_count",
    )
    expected_selected = _integer(
        evidence.get("expected_pair_count"),
        "acceptance transport evidence expected_pair_count",
    )
    if selected_count != accepted_selected or selected_count != expected_selected:
        raise FuryShadowTransitionDatasetError(
            "accepted session count does not match the current journal"
        )
    if selected_count <= 0:
        raise FuryShadowTransitionDatasetError("accepted session contains no rows")
    recomputed = build_shadow_pair_acceptance(
        journal_path,
        manifest_path,
        expected_pairs=selected_count,
        export_session_id=selected_session,
    )
    if (
        recomputed.get("status") != "PASS"
        or recomputed.get("deployment_allowed") is not False
        or any(
            recomputed.get(gate_name, {}).get("status") != "PASS"
            or recomputed.get(gate_name, {}).get("passed") is not True
            for gate_name in (
                "transport_action_gate",
                "causal_state_action_gate",
                "outcome_reward_gate",
            )
        )
    ):
        raise FuryShadowTransitionDatasetError(
            "current journal no longer reproduces the accepted three-gate PASS"
        )
    return selected_session


def _project_row(
    row: Mapping[str, Any],
    *,
    session_id: str,
    candidate_policy_id: str,
) -> tuple[dict[str, Any], int, float, float]:
    journal_line = _integer(row.get("_source_journal_line"), "source journal line")
    decision_id = _text(row.get("decisionId"), "row.decisionId")
    if row.get("schemaVersion") != 2:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: top-level schemaVersion must equal 2"
        )
    if row.get("mode") != "shadow":
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: transition source must be a Shadow decision"
        )
    state = dict(_mapping(row.get("state"), f"{decision_id}.state"))
    if not state:
        raise FuryShadowTransitionDatasetError(f"{decision_id}: state is empty")
    _validate_state(state, decision_id)
    captured_at = _number(row.get("capturedAt"), f"{decision_id}.capturedAt")

    sample = _mapping(row.get("shadowSample"), f"{decision_id}.shadowSample")
    if sample.get("contractVersion") != 4:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: shadow contractVersion must equal 4"
        )
    if sample.get("exportSessionId") != session_id:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: export session mismatch"
        )
    if not (
        sample.get("status") == "confirmed"
        and sample.get("materialized") is True
        and sample.get("counted") is True
        and sample.get("actionAttribution") == "expert_sink_causal_v4"
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: Shadow sample is not confirmed/countable causal v4"
        )
    integrity = audit_v4_outcome_integrity(row)
    if integrity.get("applicable") is not True or integrity.get("passed") is not True:
        reasons = integrity.get("reasons") or ["v4_integrity_not_applicable"]
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: v4 sink/action/outcome integrity failed: "
            + ", ".join(str(reason) for reason in reasons)
        )

    recorded_policy = _mapping(
        row.get("activePolicy"), f"{decision_id}.activePolicy"
    )
    if not (
        recorded_policy.get("requestedPolicyId") == EXPECTED_RECORDED_POLICY_ID
        and recorded_policy.get("policyId") == EXPECTED_RECORDED_POLICY_ID
        and recorded_policy.get("proposalAvailable") is True
        and recorded_policy.get("executionRequested") is False
        and recorded_policy.get("stopped") is False
        and recorded_policy.get("attempts") == []
        and row.get("policyId") == EXPECTED_RECORDED_POLICY_ID
        and row.get("attempts") == []
        and row.get("stopped") is False
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: recorded Brain policy was not a clean inactive Shadow proposal"
        )
    recorded_policy_kind = _text(
        row.get("policyKind"), f"{decision_id}.policyKind"
    )
    if recorded_policy.get("policyKind") != recorded_policy_kind:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: recorded Brain policy kind mismatch"
        )
    recorded_policy_proposal = _normalized_proposal(
        recorded_policy.get("proposal"), f"{decision_id}.activePolicy.proposal"
    )
    if _normalized_proposal(row.get("proposal"), f"{decision_id}.proposal") != (
        recorded_policy_proposal
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: top-level and active Brain proposals differ"
        )

    candidate = _mapping(
        row.get("candidateShadow"), f"{decision_id}.candidateShadow"
    )
    if (
        candidate.get("requestedPolicyId") != candidate_policy_id
        or candidate.get("policyId") != candidate_policy_id
        or candidate.get("proposalAvailable") is not True
        or candidate.get("executed") is not False
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: inactive candidate policy contract mismatch"
        )
    candidate_proposal = _normalized_proposal(
        candidate.get("proposal"), f"{decision_id}.candidateShadow.proposal"
    )

    actual = _mapping(row.get("expertActual"), f"{decision_id}.expertActual")
    sink_actions = _list(
        actual.get("sinkActions"), f"{decision_id}.expertActual.sinkActions"
    )
    trace_links = _list(
        actual.get("expertTraceLinks"),
        f"{decision_id}.expertActual.expertTraceLinks",
    )
    if not (
        actual.get("observed") is True
        and actual.get("sourceKind") == "expert_trace"
        and actual.get("proposalAvailable") is False
        and _normalized_proposal(
            actual.get("proposal"), f"{decision_id}.expertActual.proposal"
        )
        == {lane: [] for lane in LANES}
        and actual.get("executed") is True
        and actual.get("materialized") is True
        and actual.get("attribution") == "expert_sink_causal_v4"
        and sink_actions
        and trace_links
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: exact actual action/trace contract mismatch"
        )
    completed_links = [
        link
        for link in trace_links
        if isinstance(link, Mapping)
        and link.get("completed") is True
        and link.get("errored") is not True
        and link.get("expert") == "Cat2"
        and link.get("entry") == "Cat2.ExecuteConfiguration"
    ]
    if not completed_links:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: no completed Cat2.ExecuteConfiguration trace link"
        )
    sink_tokens: set[tuple[int, str]] = set()
    for sink_index, raw_sink in enumerate(sink_actions, start=1):
        sink = _mapping(raw_sink, f"{decision_id}.expertActual.sinkActions[{sink_index}]")
        if sink.get("decisionId") != decision_id:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: sink action decision token mismatch"
            )
        sink_tokens.add(
            (
                _integer(
                    sink.get("seq"),
                    f"{decision_id}.sinkActions[{sink_index}].seq",
                    positive=True,
                ),
                _text(
                    sink.get("generation"),
                    f"{decision_id}.sinkActions[{sink_index}].generation",
                ),
            )
        )

    outcome = _mapping(
        row.get("observedActualOutcome"),
        f"{decision_id}.observedActualOutcome",
    )
    telemetry = _mapping(
        outcome.get("telemetry"),
        f"{decision_id}.observedActualOutcome.telemetry",
    )
    if not (
        outcome.get("status") == "complete"
        and outcome.get("endReason") == "all_actions_terminal_causal"
        and outcome.get("causalValidated") is True
        and outcome.get("observedOnly") is True
        and outcome.get("attribution")
        == "expert_actual_only_not_candidate_counterfactual"
        and telemetry.get("available") is True
        and telemetry.get("reason") == "available"
        and telemetry.get("missingCVars") == []
    ):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: observed actual outcome is not usable causal telemetry"
        )

    raw_actions = _list(
        outcome.get("actions"), f"{decision_id}.observedActualOutcome.actions"
    )
    if not raw_actions:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: observed outcome has no actions"
        )
    projected_actions: list[dict[str, Any]] = []
    actual_proposal = {lane: [] for lane in LANES}
    action_tokens: set[tuple[int, str]] = set()
    issued_ordinals: list[int] = []
    for action_index, raw_action in enumerate(raw_actions, start=1):
        action = _mapping(
            raw_action, f"{decision_id}.observedActualOutcome.actions[{action_index}]"
        )
        family = _text(action.get("family"), f"{decision_id}.action.family")
        action_id = FAMILY_ACTION_IDS.get(family)
        if action_id is None:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: unsupported exact action family {family!r}"
            )
        cast_type = _integer(
            action.get("clientCastType"), f"{decision_id}.{family}.clientCastType"
        )
        lane = CAST_TYPE_LANES.get(cast_type)
        if lane is None:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: unsupported client cast type {cast_type}"
            )
        sink_seq = _integer(
            action.get("sinkSeq"), f"{decision_id}.{family}.sinkSeq", positive=True
        )
        generation = _text(
            action.get("generation"), f"{decision_id}.{family}.generation"
        )
        if action.get("decisionId") != decision_id:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: action decision token mismatch"
            )
        token = (sink_seq, generation)
        if token in action_tokens:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: duplicate action sink/generation token"
            )
        action_tokens.add(token)
        if token not in sink_tokens:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: outcome action has no matching exact sink token"
            )
        if action.get("clientAccepted") is not True:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: outcome action lacks final client acceptance"
            )
        issued_ordinal = _integer(
            action.get("issuedOrdinal"),
            f"{decision_id}.{family}.issuedOrdinal",
            positive=True,
        )
        issued_ordinals.append(issued_ordinal)
        expected_spell_ids = _list(
            action.get("expectedSpellIds"),
            f"{decision_id}.{family}.expectedSpellIds",
        )
        if any(
            not isinstance(spell_id, int)
            or isinstance(spell_id, bool)
            or spell_id <= 0
            for spell_id in expected_spell_ids
        ):
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: expected spell IDs must be positive integers"
            )
        actual_proposal[lane].append(action_id)
        projected_actions.append(
            {
                "action_id": action_id,
                "family": family,
                "lane": lane,
                "spell_ids": list(expected_spell_ids),
                "sink_seq": sink_seq,
                "generation": generation,
                "issued_at": _number(
                    action.get("issuedAt"), f"{decision_id}.{family}.issuedAt"
                ),
                "issued_ordinal": issued_ordinal,
                "client_accepted": action.get("clientAccepted") is True,
                "status": _text(
                    action.get("status"), f"{decision_id}.{family}.status"
                ),
                "damage": _number(
                    action.get("damage", 0),
                    f"{decision_id}.{family}.damage",
                    nonnegative=True,
                ),
                "result_count": _integer(
                    action.get("resultCount", 0),
                    f"{decision_id}.{family}.resultCount",
                ),
                "miss_count": _integer(
                    action.get("missCount", 0),
                    f"{decision_id}.{family}.missCount",
                ),
                "failed_count": _integer(
                    action.get("failedCount", 0),
                    f"{decision_id}.{family}.failedCount",
                ),
            }
        )
    projected_actions.sort(key=lambda item: (item["issued_ordinal"], item["sink_seq"]))

    causal_ordinals: list[int] = []
    compact_events = _list(
        outcome.get("compactEvents"),
        f"{decision_id}.observedActualOutcome.compactEvents",
    )
    if not compact_events:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: compact causal event evidence is empty"
        )
    for event_index, raw_event in enumerate(compact_events, start=1):
        event = _mapping(raw_event, f"{decision_id}.compactEvents[{event_index}]")
        event_name = _text(event.get("event"), f"{decision_id}.compact event name")
        if event_name not in CAUSAL_EVENTS:
            continue
        causal_ordinal = _integer(
            event.get("causalOrdinal"),
            f"{decision_id}.{event_name}.causalOrdinal",
            positive=True,
        )
        causal_ordinals.append(causal_ordinal)
        event_token = (
            _integer(
                event.get("sinkSeq"), f"{decision_id}.{event_name}.sinkSeq", positive=True
            ),
            _text(event.get("generation"), f"{decision_id}.{event_name}.generation"),
        )
        if event.get("actionDecisionId") != decision_id or event_token not in action_tokens:
            raise FuryShadowTransitionDatasetError(
                f"{decision_id}: compact event action token mismatch"
            )
    if not causal_ordinals or len(set(causal_ordinals)) != len(causal_ordinals):
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: causal event ordinals are missing or duplicated"
        )

    end_at = _number(outcome.get("lastEventAt"), f"{decision_id}.outcome.lastEventAt")
    if end_at < captured_at:
        raise FuryShadowTransitionDatasetError(
            f"{decision_id}: outcome ends before its state snapshot"
        )
    damage = _number(
        outcome.get("damage", 0), f"{decision_id}.outcome.damage", nonnegative=True
    )
    rage_before = _optional_number(state.get("rage"))
    rage_after = _optional_number(outcome.get("rageLast"))
    target_health_before = _optional_number(state.get("targetHealth"))
    target_health_after = _optional_number(outcome.get("targetHealthLast"))

    recorded_policy_agreement = _proposal_agreement(
        actual_proposal, recorded_policy_proposal
    )
    candidate_agreement = _proposal_agreement(actual_proposal, candidate_proposal)

    projected = {
        "schema": RECORD_SCHEMA,
        "identity": {
            "export_session_id": session_id,
            "decision_id": decision_id,
        },
        "temporal": {
            "captured_at": captured_at,
            "first_issued_ordinal": min(issued_ordinals),
            "first_causal_ordinal": min(causal_ordinals),
            "last_causal_ordinal": max(causal_ordinals),
            "outcome_end_at": end_at,
            "observed_window_seconds": end_at - captured_at,
        },
        "state_before": state,
        "actual": {
            "source": "Cat2_exact_sink_trace",
            "executor": {
                "kind": "cat2_configuration_card_stack",
                "expert": "Cat2",
                "entry": "Cat2.ExecuteConfiguration",
                "profile_id": row.get("profileId"),
                "profile_name": _text(
                    row.get("profileName"), f"{decision_id}.profileName"
                ),
                "executed_policy_id": None,
                "executed_policy_proposal_available": False,
            },
            "factorized_action": actual_proposal,
            "actions": projected_actions,
        },
        "recorded_active_policy": {
            "policy_id": EXPECTED_RECORDED_POLICY_ID,
            "policy_kind": recorded_policy_kind,
            "execution_requested": False,
            "executed": False,
            "proposal": recorded_policy_proposal,
            "counterfactual_outcome": None,
        },
        "candidate": {
            "policy_id": candidate_policy_id,
            "executed": False,
            "proposal": candidate_proposal,
            "counterfactual_outcome": None,
        },
        "agreement": {
            "recorded_active_policy_vs_actual": recorded_policy_agreement,
            "candidate_vs_actual": candidate_agreement,
        },
        "observed_actual_immediate_outcome": {
            "status": outcome.get("status"),
            "end_reason": outcome.get("endReason"),
            "damage": damage,
            "result_count": _integer(
                outcome.get("resultCount", 0), f"{decision_id}.outcome.resultCount"
            ),
            "miss_count": _integer(
                outcome.get("missCount", 0), f"{decision_id}.outcome.missCount"
            ),
            "failed_count": _integer(
                outcome.get("failedCount", 0), f"{decision_id}.outcome.failedCount"
            ),
            "rage_before": rage_before,
            "rage_after": rage_after,
            "rage_observed_delta": (
                rage_after - rage_before
                if rage_before is not None and rage_after is not None
                else None
            ),
            "target_health_before": target_health_before,
            "target_health_after": target_health_after,
            "target_health_observed_delta": (
                target_health_after - target_health_before
                if target_health_before is not None
                and target_health_after is not None
                else None
            ),
            "telemetry_source": telemetry.get("source"),
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
            "source_journal": None,
            "source_journal_line": journal_line,
            "source_identity": _row_identity(row)[0],
            "shadow_contract_version": 4,
            "coalesced_macro_evaluations": _integer(
                sample.get("coalescedMacroEvaluations"),
                f"{decision_id}.coalescedMacroEvaluations",
                positive=True,
            ),
            "coalesced_macro_evaluations_used_as_weight": False,
        },
    }
    return projected, journal_line, captured_at, float(min(causal_ordinals))


def build_fury_shadow_transition_dataset(
    journal_path: str | Path = DEFAULT_JOURNAL,
    manifest_path: str | Path = DEFAULT_JOURNAL_MANIFEST,
    acceptance_path: str | Path = DEFAULT_ACCEPTANCE,
    *,
    session_id: str | None = None,
    candidate_policy_id: str = EXPECTED_CANDIDATE_POLICY_ID,
    output_path: str | Path = DEFAULT_OUTPUT,
    output_manifest_path: str | Path = DEFAULT_OUTPUT_MANIFEST,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Build deterministic transition fragments and their manifest/report."""

    journal = Path(journal_path).expanduser().resolve()
    journal_manifest_path = Path(manifest_path).expanduser().resolve()
    acceptance_report_path = Path(acceptance_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output_manifest = Path(output_manifest_path).expanduser().resolve()
    candidate_policy_id = _text(candidate_policy_id, "candidate policy ID")
    requested_session = str(session_id).strip() if session_id is not None else None
    if requested_session == "":
        raise FuryShadowTransitionDatasetError("requested session ID is empty")
    if len({journal, journal_manifest_path, acceptance_report_path, output, output_manifest}) != 5:
        raise FuryShadowTransitionDatasetError("input and output paths must be distinct")

    input_paths = {
        "journal": journal,
        "journal_manifest": journal_manifest_path,
        "acceptance_report": acceptance_report_path,
    }
    input_hashes = {
        label: _sha256_bytes(_read_bytes(path, label))
        for label, path in input_paths.items()
    }

    rows = _load_journal(journal)
    manifest = _load_json(journal_manifest_path, "journal manifest")
    acceptance = _load_json(acceptance_report_path, "acceptance report")
    selected_session = _validate_acceptance(
        acceptance,
        acceptance_path=acceptance_report_path,
        journal_path=journal,
        manifest_path=journal_manifest_path,
        journal_rows=rows,
        journal_manifest=manifest,
        requested_session_id=requested_session,
    )
    final_input_hashes = {
        label: _sha256_bytes(_read_bytes(path, label))
        for label, path in input_paths.items()
    }
    if final_input_hashes != input_hashes:
        raise FuryShadowTransitionDatasetError(
            "a transition source changed during validation; refusing a TOCTOU build"
        )

    selected_rows = [
        row
        for row in rows
        if _mapping(row.get("shadowSample"), "row.shadowSample").get(
            "exportSessionId"
        )
        == selected_session
    ]
    projected_with_order = [
        _project_row(
            row,
            session_id=selected_session,
            candidate_policy_id=candidate_policy_id,
        )
        for row in selected_rows
    ]

    global_ordinals: list[int] = []
    for row in selected_rows:
        outcome = _mapping(row.get("observedActualOutcome"), "row outcome")
        for event in _list(outcome.get("compactEvents"), "compact events"):
            event_map = _mapping(event, "compact event")
            if event_map.get("event") in CAUSAL_EVENTS:
                global_ordinals.append(
                    _integer(event_map.get("causalOrdinal"), "causalOrdinal", positive=True)
                )
    if len(set(global_ordinals)) != len(global_ordinals):
        raise FuryShadowTransitionDatasetError(
            "accepted session reuses a causal ordinal across physical events"
        )

    original_order = [
        (item[1], item[2], item[0]["identity"]["decision_id"])
        for item in projected_with_order
    ]
    projected_with_order.sort(key=lambda item: (item[2], item[3], item[1]))
    projected_rows = [item[0] for item in projected_with_order]
    for sequence, projected in enumerate(projected_rows, start=1):
        projected["identity"]["sequence_in_session"] = sequence
        projected["provenance"]["source_journal"] = str(journal)

    capture_inversions = 0
    for left_index, left in enumerate(original_order):
        for right in original_order[left_index + 1 :]:
            capture_inversions += int(right[1] < left[1])

    family_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    agreement_counts = {
        "recorded_active_policy_vs_actual": Counter(),
        "candidate_vs_actual": Counter(),
    }
    rage_buckets: Counter[str] = Counter()
    health_buckets: Counter[str] = Counter()
    enemy_counts: Counter[str] = Counter()
    for row in projected_rows:
        for action in row["actual"]["actions"]:
            family_counts[action["family"]] += 1
            lane_counts[action["lane"]] += 1
        for source_name, source_agreement in row["agreement"].items():
            for metric, matched in source_agreement.items():
                agreement_counts[source_name][metric] += int(matched)
        state = row["state_before"]
        rage_buckets[
            _state_bucket(
                _optional_number(state.get("rage")),
                ((19, "00-19"), (39, "20-39"), (59, "40-59"), (79, "60-79"), (100, "80-100")),
            )
        ] += 1
        health_buckets[
            _state_bucket(
                _optional_number(state.get("targetPercentHealth")),
                ((20, "00-20"), (50, "21-50"), (100, "51-100")),
            )
        ] += 1
        nearby = state.get("nearbyEnemies")
        enemy_counts[str(nearby) if isinstance(nearby, int) else "unknown"] += 1

    source_generated_at = acceptance.get("generated_at")
    source_generated_at = (
        source_generated_at if isinstance(source_generated_at, str) else None
    )
    identities = [
        {
            "export_session_id": row["identity"]["export_session_id"],
            "decision_id": row["identity"]["decision_id"],
        }
        for row in projected_rows
    ]
    dataset_text = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
        for row in projected_rows
    )
    dataset_sha256 = _sha256_bytes(dataset_text.encode("utf-8"))
    output_manifest_document = {
        "schema": MANIFEST_SCHEMA,
        "kind": "fury_shadow_transition_dataset",
        "built_from_acceptance_generated_at": source_generated_at,
        "record_schema": RECORD_SCHEMA,
        "output": str(output),
        "output_sha256": dataset_sha256,
        "row_count": len(projected_rows),
        "export_session_id": selected_session,
        "recorded_active_policy_id": EXPECTED_RECORDED_POLICY_ID,
        "candidate_policy_id": candidate_policy_id,
        "identities": identities,
        "temporal_order": ["captured_at", "first_causal_ordinal", "source_journal_line"],
        "inputs": {
            "journal": str(journal),
            "journal_sha256": input_hashes["journal"],
            "journal_manifest": str(journal_manifest_path),
            "journal_manifest_sha256": input_hashes["journal_manifest"],
            "acceptance_report": str(acceptance_report_path),
            "acceptance_report_sha256": input_hashes["acceptance_report"],
            "journal_pair_total": len(rows),
        },
        "eligibility_contract": {
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
    }
    report = {
        "schema": REPORT_SCHEMA,
        "status": "ok",
        "built_from_acceptance_generated_at": source_generated_at,
        "input": output_manifest_document["inputs"],
        "accepted_session": {
            "export_session_id": selected_session,
            "row_count": len(projected_rows),
            "recorded_active_policy_id": EXPECTED_RECORDED_POLICY_ID,
            "candidate_policy_id": candidate_policy_id,
            "transport_action_gate": "PASS",
            "causal_state_action_gate": "PASS",
            "outcome_reward_gate": "PASS",
        },
        "output": {
            "dataset": str(output),
            "dataset_sha256": dataset_sha256,
            "manifest": str(output_manifest),
            "record_schema": RECORD_SCHEMA,
            "row_count": len(projected_rows),
        },
        "coverage": {
            "actual_action_family": dict(sorted(family_counts.items())),
            "actual_action_lane": dict(sorted(lane_counts.items())),
            "proposal_agreement": {
                source_name: {
                    "contains_any_actual_action_rows": counts[
                        "contains_any_actual_action"
                    ],
                    "contains_all_actual_actions_rows": counts[
                        "contains_all_actual_actions"
                    ],
                    "missing_actual_action_rows": len(projected_rows)
                    - counts["contains_all_actual_actions"],
                    "exact_factorized_action_match_rows": counts[
                        "exact_factorized_action_match"
                    ],
                    "factorized_action_disagreement_rows": len(projected_rows)
                    - counts["exact_factorized_action_match"],
                    "single_action_exact_match_rows": counts[
                        "single_action_exact_match"
                    ],
                }
                for source_name, counts in agreement_counts.items()
            },
            "candidate_contains_any_actual_action_rows": agreement_counts[
                "candidate_vs_actual"
            ]["contains_any_actual_action"],
            "candidate_contains_all_actual_actions_rows": agreement_counts[
                "candidate_vs_actual"
            ]["contains_all_actual_actions"],
            "candidate_missing_actual_action_rows": len(projected_rows)
            - agreement_counts["candidate_vs_actual"]["contains_all_actual_actions"],
            "candidate_factorized_action_disagreement_rows": len(projected_rows)
            - agreement_counts["candidate_vs_actual"]["exact_factorized_action_match"],
            "exact_factorized_action_match_rows": agreement_counts[
                "candidate_vs_actual"
            ]["exact_factorized_action_match"],
            "single_action_exact_match_rows": agreement_counts[
                "candidate_vs_actual"
            ]["single_action_exact_match"],
            "state": {
                "rage_bucket": dict(sorted(rage_buckets.items())),
                "target_health_percent_bucket": dict(sorted(health_buckets.items())),
                "nearby_enemies": dict(sorted(enemy_counts.items())),
            },
        },
        "quality": {
            "source_and_manifest_identity_match": True,
            "accepted_count_matches_current_journal": True,
            "composite_identity_unique": True,
            "causal_ordinal_unique": True,
            "sorted_by_capture_then_causal_order": True,
            "source_journal_capture_order_inversion_count": capture_inversions,
            "coalesced_macro_evaluations_used_as_weight": False,
        },
        "eligibility": output_manifest_document["eligibility_contract"],
        "interpretation": {
            "actual_outcome": (
                "causally linked typed immediate outcome for the post-Brain Cat2 "
                "profile card action actually sent"
            ),
            "recorded_active_policy": (
                "proposal computed by the first Brain card but not executed in Shadow mode"
            ),
            "candidate_outcome": "unobserved counterfactual; no reward is copied from actual",
            "next_state": "not present; post-action telemetry is not a complete state snapshot",
            "superiority_claim": False,
        },
        "next_gate": {
            "name": "state_seeded_simulator_counterfactual",
            "ready": False,
            "blockers": [
                "wowsims O2O bridge has no canonical mid-encounter state seed/restore",
                "neither recorded Brain proposal was executed; both lack observed counterfactual outcomes",
                "this smoke session is not a complete encounter episode",
            ],
        },
        "deployment_allowed": False,
    }
    return projected_rows, output_manifest_document, report


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    except (OSError, UnicodeError) as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise FuryShadowTransitionDatasetError(f"cannot write {path}: {error}") from error


def materialize_fury_shadow_transition_dataset(
    journal_path: str | Path = DEFAULT_JOURNAL,
    manifest_path: str | Path = DEFAULT_JOURNAL_MANIFEST,
    acceptance_path: str | Path = DEFAULT_ACCEPTANCE,
    *,
    session_id: str | None = None,
    candidate_policy_id: str = EXPECTED_CANDIDATE_POLICY_ID,
    output_path: str | Path = DEFAULT_OUTPUT,
    output_manifest_path: str | Path = DEFAULT_OUTPUT_MANIFEST,
    report_path: str | Path = DEFAULT_REPORT,
) -> dict[str, Any]:
    """Validate, materialize, and return a compact deterministic receipt."""

    output = Path(output_path).expanduser().resolve()
    output_manifest = Path(output_manifest_path).expanduser().resolve()
    report_output = Path(report_path).expanduser().resolve()
    protected_inputs = {
        Path(journal_path).expanduser().resolve(),
        Path(manifest_path).expanduser().resolve(),
        Path(acceptance_path).expanduser().resolve(),
    }
    if (
        len({output, output_manifest, report_output}) != 3
        or {output, output_manifest, report_output} & protected_inputs
    ):
        raise FuryShadowTransitionDatasetError(
            "dataset outputs must be distinct and must not overwrite an input"
        )
    rows, manifest, report = build_fury_shadow_transition_dataset(
        journal_path,
        manifest_path,
        acceptance_path,
        session_id=session_id,
        candidate_policy_id=candidate_policy_id,
        output_path=output,
        output_manifest_path=output_manifest,
    )
    dataset_text = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    )
    dataset_sha256 = _sha256_bytes(dataset_text.encode("utf-8"))
    if manifest.get("output_sha256") != dataset_sha256:
        raise FuryShadowTransitionDatasetError(
            "serialized dataset hash differs from the validated build"
        )
    report["output"]["report"] = str(report_output)
    report_text = (
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    )
    manifest["report"] = str(report_output)
    manifest["report_sha256"] = _sha256_bytes(report_text.encode("utf-8"))
    manifest["commit"] = {
        "state": "complete",
        "manifest_written_last": True,
        "content_addressed": True,
    }
    manifest_text = (
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    )
    # The manifest is the commit marker.  A crash before its final replacement
    # leaves an old manifest whose hashes cannot validate partially new outputs.
    _atomic_write(report_output, report_text)
    _atomic_write(output, dataset_text)
    _atomic_write(output_manifest, manifest_text)
    candidate_coverage = report["coverage"]["proposal_agreement"][
        "candidate_vs_actual"
    ]
    recorded_coverage = report["coverage"]["proposal_agreement"][
        "recorded_active_policy_vs_actual"
    ]
    return {
        "status": "ok",
        "export_session_id": manifest["export_session_id"],
        "row_count": len(rows),
        "dataset": str(output),
        "manifest": str(output_manifest),
        "report": str(report_output),
        "dataset_sha256": dataset_sha256,
        "recorded_active_policy_contains_actual_rows": recorded_coverage[
            "contains_all_actual_actions_rows"
        ],
        "recorded_active_policy_factorized_disagreement_rows": recorded_coverage[
            "factorized_action_disagreement_rows"
        ],
        "candidate_contains_actual_rows": candidate_coverage[
            "contains_all_actual_actions_rows"
        ],
        "candidate_missing_actual_action_rows": candidate_coverage[
            "missing_actual_action_rows"
        ],
        "candidate_factorized_disagreement_rows": candidate_coverage[
            "factorized_action_disagreement_rows"
        ],
        "scalar_reward_available": False,
        "recorded_active_policy_counterfactual_reward_available": False,
        "candidate_counterfactual_reward_available": False,
        "full_next_state_available": False,
        "td_transition_eligible": False,
        "offline_rl_episode_eligible": False,
        "deployment_allowed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_JOURNAL_MANIFEST)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--session-id")
    parser.add_argument(
        "--candidate-policy-id", default=EXPECTED_CANDIDATE_POLICY_ID
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--output-manifest", type=Path, default=DEFAULT_OUTPUT_MANIFEST
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = materialize_fury_shadow_transition_dataset(
            args.journal,
            args.manifest,
            args.acceptance,
            session_id=args.session_id,
            candidate_policy_id=args.candidate_policy_id,
            output_path=args.output,
            output_manifest_path=args.output_manifest,
            report_path=args.report,
        )
    except FuryShadowTransitionDatasetError as error:
        print(f"Fury Shadow transition dataset failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "DEFAULT_ACCEPTANCE",
    "DEFAULT_JOURNAL",
    "DEFAULT_JOURNAL_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_OUTPUT_MANIFEST",
    "DEFAULT_REPORT",
    "EXPECTED_CANDIDATE_POLICY_ID",
    "FuryShadowTransitionDatasetError",
    "MANIFEST_SCHEMA",
    "RECORD_SCHEMA",
    "REPORT_SCHEMA",
    "build_fury_shadow_transition_dataset",
    "materialize_fury_shadow_transition_dataset",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
