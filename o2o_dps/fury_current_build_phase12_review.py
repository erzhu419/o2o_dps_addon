"""Review Phase-12 Fury evidence without changing a registry or simulator.

Phase 12 deliberately ends at collection/audit.  This module performs the
separate evidence review required by the preregistration.  It may retain a
completed exact stage as an observed fact even when a sibling stage leaves the
mechanism gate partial, but it never converts that fact into an unmeasured
parameter or a simulator override.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_audit.json"
)
DEFAULT_PREREGISTRATION = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_preregistration.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_review.json"
)

SCHEMA_VERSION = 1
REVIEWER = "fury_current_build_phase12_evidence_review_v1"
EXPECTED_AUDIT_KIND = "fury_current_build_phase12_audit"
EXPECTED_PREREGISTRATION_KIND = "fury_current_build_phase12_preregistration"
EXPECTED_SUMMARY_KIND = "brainofcat_calibration_summary"


class FuryPhase12ReviewError(RuntimeError):
    """The supplied Phase-12 artifacts cannot support a bounded review."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FuryPhase12ReviewError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise FuryPhase12ReviewError(f"{label} is not a JSON object: {path}")
    return value


def _linked_action_state_evidence(
    summary: Mapping[str, Any], summary_path: Path
) -> dict[str, Any]:
    """Retain only action-state rows needed for historical contract review."""

    declared = _mapping(summary.get("source")).get("calibration_jsonl")
    if not isinstance(declared, str) or not declared:
        return {
            "source": None,
            "rows_scanned": 0,
            "retained_row_count": 0,
            "stages": {},
        }
    source = Path(declared).expanduser()
    if not source.is_absolute():
        source = summary_path.parent / source
    source = source.resolve()
    if not source.is_file():
        raise FuryPhase12ReviewError(
            f"summary-linked calibration JSONL does not exist: {source}"
        )

    stages: dict[str, list[dict[str, Any]]] = {}
    rows_scanned = 0
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                rows_scanned += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FuryPhase12ReviewError(
                        f"invalid summary-linked calibration JSONL at "
                        f"{source}:{line_number}: {error}"
                    ) from error
                if not isinstance(row, Mapping) or row.get("event") != "CALIBRATION_ACTION_REQUESTED":
                    continue
                task = _mapping(row.get("task"))
                marker = _mapping(row.get("marker"))
                stage_id = (
                    task.get("phase12StageId")
                    or task.get("phase12StageID")
                    or marker.get("phase12StageId")
                    or marker.get("phase12StageID")
                )
                if stage_id not in {"slam_moving", "ww_outside_8"}:
                    continue
                state = _mapping(row.get("state"))
                field_provenance = _mapping(state.get("fieldProvenance"))
                sequence = row.get("sequence")
                if type(sequence) is not int:
                    continue
                stages.setdefault(str(stage_id), []).append(
                    {
                        "sequence": sequence,
                        "event": row.get("event"),
                        "task_stage_id": task.get("phase12StageId")
                        or task.get("phase12StageID"),
                        "marker_stage_id": marker.get("phase12StageId")
                        or marker.get("phase12StageID"),
                        "state_moving": state.get("moving"),
                        "marker_moving": marker.get("moving"),
                        "moving_field_provenance": field_provenance.get("moving"),
                        "state_target_melee_distance": state.get(
                            "targetMeleeDistance"
                        ),
                        "marker_target_melee_distance": marker.get(
                            "targetMeleeDistance"
                        ),
                    }
                )
    except OSError as error:
        raise FuryPhase12ReviewError(
            f"cannot read summary-linked calibration JSONL {source}: {error}"
        ) from error
    return {
        "source": str(source),
        "rows_scanned": rows_scanned,
        "retained_row_count": sum(len(rows) for rows in stages.values()),
        "stages": stages,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _validate_review_inputs(
    audit: Mapping[str, Any],
    summary: Mapping[str, Any],
    preregistration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if audit.get("kind") != EXPECTED_AUDIT_KIND:
        raise FuryPhase12ReviewError("input audit is not a Phase-12 Fury audit")
    if preregistration.get("kind") != EXPECTED_PREREGISTRATION_KIND:
        raise FuryPhase12ReviewError("input is not the Phase-12 preregistration")
    if summary.get("kind") != EXPECTED_SUMMARY_KIND:
        raise FuryPhase12ReviewError("input is not a BrainOfCat calibration summary")
    if audit.get("campaign_id") != preregistration.get("campaign_id"):
        raise FuryPhase12ReviewError("audit campaign does not match preregistration")
    if audit.get("analyzer") != preregistration.get("analyzer"):
        raise FuryPhase12ReviewError("audit analyzer does not match preregistration")
    if audit.get("scope") != preregistration.get("scope"):
        raise FuryPhase12ReviewError("audit scope does not match preregistration")

    publication = _mapping(preregistration.get("publication_gate"))
    if (
        publication.get("simulator_patch_allowed") is not False
        or publication.get("simulator_patch") is not None
    ):
        raise FuryPhase12ReviewError(
            "Phase-12 preregistration does not preserve the no-patch boundary"
        )
    conclusion = _mapping(audit.get("conclusion_gate"))
    if (
        conclusion.get("simulator_patch_allowed") is not False
        or conclusion.get("simulator_patch") is not None
    ):
        raise FuryPhase12ReviewError(
            "Phase-12 audit attempts to authorize a simulator patch"
        )
    if audit.get("simulator_overrides") != []:
        raise FuryPhase12ReviewError("Phase-12 audit contains simulator overrides")

    prereg_gates = preregistration.get("mechanism_gates")
    audit_gates = audit.get("mechanism_gates")
    if not isinstance(prereg_gates, Mapping) or not isinstance(audit_gates, Mapping):
        raise FuryPhase12ReviewError("Phase-12 mechanism gates are missing")
    if set(prereg_gates) != set(audit_gates):
        raise FuryPhase12ReviewError(
            "audit mechanism-gate inventory does not match preregistration"
        )

    prereg_boundaries = preregistration.get("external_mechanism_boundaries")
    audit_boundaries = audit.get("external_mechanism_boundaries")
    if not isinstance(prereg_boundaries, Mapping) or not isinstance(
        audit_boundaries, Mapping
    ):
        raise FuryPhase12ReviewError("Phase-12 external boundaries are missing")
    if set(prereg_boundaries) != set(audit_boundaries):
        raise FuryPhase12ReviewError(
            "audit external-boundary inventory does not match preregistration"
        )
    for name, boundary in audit_boundaries.items():
        if _mapping(boundary).get("simulator_patch_allowed") is not False:
            raise FuryPhase12ReviewError(
                f"external boundary {name!r} attempts to authorize a simulator patch"
            )

    expected_run_ids = {
        value
        for value in (
            audit.get("campaign_run_id"),
            audit.get("repair_campaign_run_id"),
        )
        if isinstance(value, str) and value
    }
    campaigns = summary.get("specialized_campaigns")
    if not isinstance(campaigns, list):
        raise FuryPhase12ReviewError("summary has no specialized campaign inventory")
    matched = [
        campaign
        for campaign in campaigns
        if isinstance(campaign, dict)
        and campaign.get("campaign_run_id") in expected_run_ids
    ]
    if not matched:
        raise FuryPhase12ReviewError(
            "summary is not bound to the audited base or repair campaign run"
        )
    for campaign in matched:
        if (
            campaign.get("simulator_patch_allowed") is not False
            or campaign.get("simulator_patch") is not None
        ):
            raise FuryPhase12ReviewError(
                "summary campaign does not preserve the no-patch boundary"
            )
    return matched


def _compact_step(step: Mapping[str, Any]) -> dict[str, Any]:
    sequences = step.get("sequences")
    sequence_values = (
        [value for value in sequences if type(value) is int]
        if isinstance(sequences, list)
        else []
    )
    return {
        "name": step.get("name"),
        "satisfied": step.get("satisfied") is True,
        "evidence_count": len(sequence_values),
        "sequences": sequence_values,
        "first_sequence": sequence_values[0] if sequence_values else None,
        "last_sequence": sequence_values[-1] if sequence_values else None,
    }


def _stage_index(audit: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    gates = _mapping(audit.get("mechanism_gates"))
    for gate_name, raw_gate in gates.items():
        gate = _mapping(raw_gate)
        tasks = gate.get("current_tasks")
        if not isinstance(tasks, list):
            continue
        for raw_task in tasks:
            task = _mapping(raw_task)
            trials = task.get("trials")
            if not isinstance(trials, list):
                continue
            for raw_trial in trials:
                trial = _mapping(raw_trial)
                stage_id = trial.get("stage_id")
                if not isinstance(stage_id, str) or not stage_id:
                    continue
                chain = _mapping(trial.get("exact_action_chain"))
                steps = chain.get("steps")
                environment = _mapping(trial.get("environment_evidence"))
                observed_environment = _mapping(environment.get("observed"))
                anchor = {
                    "gate": gate_name,
                    "gate_status": gate.get("status"),
                    "task_id": task.get("task_id"),
                    "task_run_id": task.get("task_run_id"),
                    "stage_id": stage_id,
                    "stage_status": trial.get("status"),
                    "exact_action_chain_complete": (
                        trial.get("exact_action_chain_complete") is True
                        and chain.get("exact_action_chain_complete") is True
                    ),
                    "completion_sequence": trial.get("completion_sequence"),
                    "combined_evidence_source": trial.get(
                        "combined_evidence_source"
                    ),
                    "failure_reasons": list(chain.get("failure_reasons", []))
                    if isinstance(chain.get("failure_reasons"), list)
                    else [],
                    "environment_evidence": {
                        "moving": list(observed_environment.get("moving", []))
                        if isinstance(observed_environment.get("moving"), list)
                        else [],
                    },
                    "steps": [
                        _compact_step(step)
                        for step in steps
                        if isinstance(step, Mapping)
                    ]
                    if isinstance(steps, list)
                    else [],
                }
                result.setdefault(stage_id, []).append(anchor)
    return result


def _complete_anchor(anchor: Mapping[str, Any]) -> bool:
    return (
        anchor.get("stage_status") == "completed"
        and anchor.get("exact_action_chain_complete") is True
    )


def _step_map(anchor: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    steps = anchor.get("steps")
    if not isinstance(steps, list):
        return {}
    return {
        str(step.get("name")): step
        for step in steps
        if isinstance(step, Mapping) and isinstance(step.get("name"), str)
    }


def _legacy_terminal_only_anchor(
    anchors: Sequence[Mapping[str, Any]], required_steps: Sequence[str]
) -> Mapping[str, Any] | None:
    for anchor in anchors:
        if anchor.get("stage_status") != "incomplete":
            continue
        if anchor.get("failure_reasons") != [
            "terminal_marker_reports_partial_or_deferred_coverage"
        ]:
            continue
        steps = _step_map(anchor)
        terminal = steps.get("terminal_coverage_observed")
        if not isinstance(terminal, Mapping) or terminal.get("satisfied") is not False:
            continue
        if all(
            isinstance(steps.get(name), Mapping)
            and steps[name].get("satisfied") is True
            for name in required_steps
        ):
            return anchor
    return None


def _historical_contract_adjudication(
    stage_index: Mapping[str, list[dict[str, Any]]],
    action_state_evidence: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    evidence_stages = _mapping(action_state_evidence.get("stages"))
    results: dict[str, dict[str, Any]] = {}

    slam_anchor = _legacy_terminal_only_anchor(
        stage_index.get("slam_moving", []),
        (
            "action_requested",
            "client_cast_succeeded",
            "spell_start_observed",
            "server_go_observed",
            "result_observed",
        ),
    )
    slam_steps = _step_map(slam_anchor or {})
    action_step = slam_steps.get("action_requested")
    action_sequences = {
        sequence
        for sequence in (
            action_step.get("sequences", [])
            if isinstance(action_step, Mapping)
            and isinstance(action_step.get("sequences"), list)
            else []
        )
        if type(sequence) is int
    }
    slam_action_rows = [
        row
        for row in evidence_stages.get("slam_moving", [])
        if isinstance(row, Mapping) and row.get("sequence") in action_sequences
    ] if isinstance(evidence_stages.get("slam_moving"), list) else []
    moving_action_rows = [
        row
        for row in slam_action_rows
        if row.get("state_moving") is True and row.get("marker_moving") is True
    ]
    environment_moving = (
        slam_anchor is not None
        and True
        in _mapping(slam_anchor.get("environment_evidence")).get("moving", [])
    )
    slam_accepted = (
        slam_anchor is not None and environment_moving and bool(moving_action_rows)
    )
    results["slam_moving"] = {
        "source_stage_status": (
            slam_anchor.get("stage_status") if slam_anchor is not None else None
        ),
        "source_failure_reasons": (
            list(slam_anchor.get("failure_reasons", []))
            if slam_anchor is not None
            else []
        ),
        "corrected_action_contract_satisfied": slam_anchor is not None,
        "environment_moving_true_observed": environment_moving,
        "associated_action_moving_true": bool(moving_action_rows),
        "associated_action_evidence": moving_action_rows,
        "decision": (
            "ADJUDICATE_OBSERVED_FACT_NOT_RETEST"
            if slam_accepted
            else "RETEST_REMAINS"
        ),
        "observed_fact": (
            "Slam executed successfully while moving=true was confirmed on the associated action request."
            if slam_accepted
            else None
        ),
        "source_audit_modified": False,
        "simulator_override": None,
    }

    whirlwind_anchor = _legacy_terminal_only_anchor(
        stage_index.get("ww_outside_8", []),
        (
            "action_requested",
            "cast_succeeded",
            "server_go_observed",
            "zero_targets_hit_observed",
        ),
    )
    whirlwind_accepted = whirlwind_anchor is not None
    results["ww_outside_8"] = {
        "source_stage_status": (
            whirlwind_anchor.get("stage_status")
            if whirlwind_anchor is not None
            else None
        ),
        "source_failure_reasons": (
            list(whirlwind_anchor.get("failure_reasons", []))
            if whirlwind_anchor is not None
            else []
        ),
        "corrected_action_contract_satisfied": whirlwind_accepted,
        "decision": (
            "ADJUDICATE_OBSERVED_FACT_NOT_RETEST"
            if whirlwind_accepted
            else "RETEST_REMAINS"
        ),
        "observed_fact": (
            "Whirlwind cast and server execution succeeded while the server reported zero targets hit."
            if whirlwind_accepted
            else None
        ),
        "exact_hit_radius_identified": False,
        "source_audit_modified": False,
        "simulator_override": None,
    }
    return results


def _review_item(
    *,
    key: str,
    question: str,
    gate_name: str,
    stage_index: Mapping[str, list[dict[str, Any]]],
    support_groups: Sequence[Sequence[str]],
    related_stage_ids: Sequence[str],
    promoted_claims: Sequence[str],
    unresolved: Sequence[str],
    scope_limit: str,
    historical_adjudications: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    historical_adjudications = historical_adjudications or {}
    adjudicated_stage_ids = {
        stage_id
        for stage_id, adjudication in historical_adjudications.items()
        if adjudication.get("decision") == "ADJUDICATE_OBSERVED_FACT_NOT_RETEST"
    }

    def stage_supported(stage_id: str) -> bool:
        return stage_id in adjudicated_stage_ids or any(
            _complete_anchor(anchor) for anchor in stage_index.get(stage_id, [])
        )

    all_stage_ids = list(
        dict.fromkeys(
            stage_id
            for group in support_groups
            for stage_id in group
        )
    )
    all_stage_ids.extend(
        stage_id for stage_id in related_stage_ids if stage_id not in all_stage_ids
    )
    anchors = [
        anchor
        for stage_id in all_stage_ids
        for anchor in stage_index.get(stage_id, [])
    ]
    completed_stage_ids = list(
        dict.fromkeys(
            anchor["stage_id"] for anchor in anchors if _complete_anchor(anchor)
        )
    )
    groups_satisfied = [
        any(stage_supported(stage_id) for stage_id in group)
        for group in support_groups
    ]
    supported = bool(groups_satisfied) and all(groups_satisfied)
    gate_statuses = list(
        dict.fromkeys(
            anchor.get("gate_status")
            for anchor in anchors
            if anchor.get("gate") == gate_name
        )
    )
    gate_status = gate_statuses[0] if len(gate_statuses) == 1 else None
    incomplete_or_missing = [
        stage_id
        for stage_id in all_stage_ids
        if not stage_supported(stage_id)
    ]
    observed_status = (
        "OBSERVED_AFTER_HISTORICAL_CONTRACT_ADJUDICATION"
        if supported and adjudicated_stage_ids.intersection(all_stage_ids)
        else "OBSERVED"
        if supported and not incomplete_or_missing
        else "OBSERVED_WITH_UNRESOLVED_SIBLING_STAGE"
        if supported
        else "UNRESOLVED"
    )
    claims = list(promoted_claims) if supported else []
    decision = (
        "PROMOTE_OBSERVED_FACT_ONLY" if supported else "HOLD_UNRESOLVED"
    )
    return {
        "question": question,
        "observed_fact": {
            "status": observed_status,
            "claims": claims,
            "gate": gate_name,
            "gate_status": gate_status,
            "completed_support_stage_ids": completed_stage_ids,
            "historically_adjudicated_support_stage_ids": [
                stage_id
                for stage_id in all_stage_ids
                if stage_id in adjudicated_stage_ids
            ],
            "incomplete_or_missing_stage_ids": incomplete_or_missing,
            "stage_evidence": anchors,
            "scope_limit": scope_limit,
        },
        "simulator_comparison": {
            "status": "NOT_EVALUATED",
            "simulator_input_present": False,
            "observed_claims": claims,
            "simulator_value": None,
            "comparison_result": None,
            "reason": (
                "The Phase-12 audit, summary, and preregistration contain no "
                "simulator result for this question."
            ),
        },
        "promotion_decision": {
            "decision": decision,
            "promoted_scope": "review_artifact_observed_fact" if supported else None,
            "promoted_claims": claims,
            "unresolved": list(unresolved),
            "stage_level_promotion_despite_partial_gate": (
                supported and gate_status == "PARTIAL"
            ),
            "historical_contract_adjudication_stage_ids": [
                stage_id
                for stage_id in all_stage_ids
                if stage_id in adjudicated_stage_ids
            ],
            "mechanics_registry_modified": False,
            "simulator_modified": False,
            "simulator_override": None,
            "reason": (
                "Completed exact stages are reusable as facts; incomplete sibling "
                "stages and unmeasured parameters remain unresolved."
                if supported
                else "The required completed exact stage evidence is absent."
            ),
        },
    }


def _external_boundary_review(audit: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, raw_boundary in _mapping(
        audit.get("external_mechanism_boundaries")
    ).items():
        boundary = _mapping(raw_boundary)
        unresolved = boundary.get("unresolved")
        result[name] = {
            "status": boundary.get("status"),
            "unresolved": list(unresolved) if isinstance(unresolved, list) else [],
            "evidence_roles": list(boundary.get("evidence_roles", []))
            if isinstance(boundary.get("evidence_roles"), list)
            else [],
            "collection_complete_contribution": False,
            "simulator_patch_allowed": False,
            "promotion_decision": "RETAIN_BOUNDARY_WITHOUT_PROMOTION",
        }
    return result


def build_phase12_review(
    audit: dict[str, Any],
    summary: dict[str, Any],
    preregistration: dict[str, Any],
    *,
    audit_path: Path,
    summary_path: Path,
    preregistration_path: Path,
    action_state_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded Phase-12 evidence review document."""

    matched_campaigns = _validate_review_inputs(audit, summary, preregistration)
    stages = _stage_index(audit)
    action_state_evidence = action_state_evidence or {
        "source": None,
        "rows_scanned": 0,
        "retained_row_count": 0,
        "stages": {},
    }
    historical_adjudications = _historical_contract_adjudication(
        stages, action_state_evidence
    )
    slam_moving_adjudicated = (
        historical_adjudications["slam_moving"]["decision"]
        == "ADJUDICATE_OBSERVED_FACT_NOT_RETEST"
    )

    reviews = {
        "movement_slam": _review_item(
            key="movement_slam",
            question="What is established about Slam while stationary versus moving?",
            gate_name="movement_range",
            stage_index=stages,
            support_groups=(("slam_stationary",),),
            related_stage_ids=("slam_moving",),
            promoted_claims=(
                "A stationary Slam action was observed from request through client cast, server execution, and result.",
                *(
                    (
                        "Slam successfully reached client cast, spell start, server execution, and result while the associated action request recorded moving=true.",
                    )
                    if slam_moving_adjudicated
                    else ()
                ),
            ),
            unresolved=(
                ()
                if slam_moving_adjudicated
                else (
                    "whether Turtle Slam is castable while moving",
                    "the exact movement interruption or rejection rule",
                )
            ),
            scope_limit=(
                "The source terminal remains incomplete because it was emitted under the "
                "historical failure-expectation contract; the corrected action chain and "
                "associated moving=true request support the observed fact."
                if slam_moving_adjudicated
                else "The moving-stage context is not sufficient for historical-contract adjudication."
            ),
            historical_adjudications=historical_adjudications,
        ),
        "whirlwind_no_current_target_hit": _review_item(
            key="whirlwind_no_current_target_hit",
            question=(
                "Can Whirlwind execute when the selected target is outside its hit area?"
            ),
            gate_name="movement_range",
            stage_index=stages,
            support_groups=(("ww_outside_8",),),
            related_stage_ids=("ww_inside_8",),
            promoted_claims=(
                "Whirlwind client cast and server execution succeeded while the server reported zero targets hit, so this window is not a target-cast failure.",
            ),
            unresolved=(
                "exact Whirlwind hit radius",
                "target eligibility and selection inside the self-centered area",
            ),
            scope_limit=(
                "Zero targets hit establishes cast-versus-hit separation; it does not identify an exact radius."
            ),
            historical_adjudications=historical_adjudications,
        ),
        "stance_rage_retention": _review_item(
            key="stance_rage_retention",
            question="Do stance changes preserve a specific amount of rage?",
            gate_name="burst_stance",
            stage_index=stages,
            support_groups=(
                ("stance_battle",),
                ("stance_defensive",),
                ("stance_berserker",),
            ),
            related_stage_ids=(),
            promoted_claims=(
                "Battle, Defensive, and Berserker stance cast/server/aura event chains were each observed.",
            ),
            unresolved=(
                "rage before and after each stance transition",
                "the exact retained-rage cap for the observed talent build",
            ),
            scope_limit=(
                "The exact-action contract validates stance transitions but contains no "
                "rage-delta step."
            ),
        ),
        "death_wish_recklessness_duration": _review_item(
            key="death_wish_recklessness_duration",
            question="What durations are established for Death Wish and Recklessness?",
            gate_name="burst_stance",
            stage_index=stages,
            support_groups=(
                ("death_wish_window", "death_wish_dedup"),
                ("recklessness_dedup",),
            ),
            related_stage_ids=("death_wish_window", "death_wish_dedup"),
            promoted_claims=(
                "Death Wish and Recklessness successful activation/aura event chains were observed.",
                "The second Death Wish request was deduplicated against its prior observed activation.",
            ),
            unresolved=(
                "Death Wish aura duration",
                "Recklessness aura duration",
                "expiration timing and refresh behavior",
            ),
            scope_limit=(
                "No required exact stage observes aura removal or a duration value."
            ),
        ),
        "queue_cross_stance": _review_item(
            key="queue_cross_stance",
            question="Does a queued Heroic Strike survive a stance transition?",
            gate_name="queue",
            stage_index=stages,
            support_groups=(("hs_stance_preserve", "stance_queue_swing"),),
            related_stage_ids=("hs_cancel",),
            promoted_claims=(
                "Heroic Strike was queued before a stance transition and produced server execution plus a result after the stance server/aura events.",
            ),
            unresolved=(
                "queue cancellation semantics from the incomplete hs_cancel stage",
                "queue behavior on target switch",
            ),
            scope_limit=(
                "This is a same-target cross-stance fact, not evidence for every queue-cancel path."
            ),
        ),
        "bloodrage_event_contract": _review_item(
            key="bloodrage_event_contract",
            question="Which Bloodrage event families are observed?",
            gate_name="burst_stance",
            stage_index=stages,
            support_groups=(("bloodrage",),),
            related_stage_ids=(),
            promoted_claims=(
                "Bloodrage spell 2687 produced an immediate energize event family and spell 29131 produced the periodic energize event family.",
            ),
            unresolved=(
                "unique periodic tick count after mirrored-event deduplication",
                "rage amount per immediate or periodic event",
                "total Bloodrage duration",
            ),
            scope_limit=(
                "The exact contract proves event-family presence and at least ten periodic event rows, not unique ticks or applied rage."
            ),
        ),
        "flurry_death_wish_qualitative": _review_item(
            key="flurry_death_wish_qualitative",
            question="What qualitative Flurry/Death Wish interaction evidence exists?",
            gate_name="flurry_deep_wounds",
            stage_index=stages,
            support_groups=(
                ("death_wish_window",),
                ("flurry_dw_post_death_wish",),
                ("flurry_refresh",),
                ("flurry_timer_rescale",),
            ),
            related_stage_ids=(),
            promoted_claims=(
                "Death Wish aura presence and later main-hand swing windows were observed in the same controlled task.",
                "A critical swing followed by Flurry aura evidence, and a later swing after the Flurry aura evidence, were observed.",
            ),
            unresolved=(
                "Death Wish damage multiplier",
                "Flurry haste multiplier",
                "numerical swing-timer rescaling rule",
                "causal interaction between Death Wish and Flurry",
            ),
            scope_limit=(
                "The exact stages establish event ordering only; they do not compare a numeric simulator prediction."
            ),
        ),
        "dual_wield_qualitative": _review_item(
            key="dual_wield_qualitative",
            question="What qualitative dual-wield and queued-HS evidence exists?",
            gate_name="dual_wield",
            stage_index=stages,
            support_groups=(
                ("dual_unqueued_1", "dual_unqueued_2", "dual_unqueued_3"),
                ("dual_hs_queued_1", "dual_hs_queued_2", "dual_hs_queued_3"),
            ),
            related_stage_ids=("dual_hs_cancel", "dual_hs_cleave_replace"),
            promoted_claims=(
                "Main-hand and off-hand swings were both observed in unqueued dual-wield windows.",
                "Off-hand swings were observed in windows where Heroic Strike was queued and executed.",
            ),
            unresolved=(
                "dual-wield miss penalty",
                "whether queued next-swing attacks alter the off-hand miss rule",
                "the incomplete dual_hs_cancel path",
            ),
            scope_limit=(
                "Swing presence is qualitative evidence; no hit-rate or miss-penalty parameter is fitted."
            ),
            historical_adjudications=historical_adjudications,
        ),
    }

    decision_counts = Counter(
        review["promotion_decision"]["decision"] for review in reviews.values()
    )
    completion = _mapping(audit.get("completion_gate"))
    source_retest_stage_ids = (
        list(completion.get("retest_required_stage_ids", []))
        if isinstance(completion.get("retest_required_stage_ids"), list)
        else []
    )
    adjudicated_not_retest_stage_ids = [
        stage_id
        for stage_id in source_retest_stage_ids
        if _mapping(historical_adjudications.get(stage_id)).get("decision")
        == "ADJUDICATE_OBSERVED_FACT_NOT_RETEST"
    ]
    review_remaining_retest_stage_ids = [
        stage_id
        for stage_id in source_retest_stage_ids
        if stage_id not in set(adjudicated_not_retest_stage_ids)
    ]
    external_boundaries = _external_boundary_review(audit)
    mechanic_unresolved = [
        {
            "review": name,
            "questions": review["promotion_decision"]["unresolved"],
        }
        for name, review in reviews.items()
        if review["promotion_decision"]["unresolved"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fury_current_build_phase12_evidence_review",
        "created_at": _utc_now(),
        "reviewer": REVIEWER,
        "status": (
            "OBSERVED_FACTS_PROMOTED_WITH_UNRESOLVED_MECHANICS"
            if decision_counts["PROMOTE_OBSERVED_FACT_ONLY"]
            else "NO_FACT_PROMOTION"
        ),
        "sources": {
            "audit": str(audit_path.resolve()),
            "calibration_summary": str(summary_path.resolve()),
            "preregistration": str(preregistration_path.resolve()),
            "summary_linked_calibration_action_evidence": {
                "source": action_state_evidence.get("source"),
                "rows_scanned": action_state_evidence.get("rows_scanned", 0),
                "retained_row_count": action_state_evidence.get(
                    "retained_row_count", 0
                ),
            },
            "matched_summary_campaigns": [
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_run_id": campaign.get("campaign_run_id"),
                    "terminal_event": campaign.get("terminal_event"),
                    "status": campaign.get("status"),
                }
                for campaign in matched_campaigns
            ],
        },
        "source_gate_context": {
            "audit_status": audit.get("status"),
            "campaign_terminal": _mapping(audit.get("conclusion_gate")).get(
                "campaign_terminal"
            ),
            "collection_complete": _mapping(audit.get("conclusion_gate")).get(
                "collection_complete"
            ),
            "source_retest_required_stage_ids": source_retest_stage_ids,
            "review_adjudicated_not_retest_stage_ids": (
                adjudicated_not_retest_stage_ids
            ),
            "review_remaining_retest_required_stage_ids": (
                review_remaining_retest_stage_ids
            ),
        },
        "historical_contract_adjudication": {
            "source_audit_status_preserved": audit.get("status"),
            "source_audit_modified": False,
            "stages": historical_adjudications,
            "review_adjudicated_not_retest_stage_ids": (
                adjudicated_not_retest_stage_ids
            ),
        },
        "evidence_reviews": reviews,
        "decision_counts": dict(sorted(decision_counts.items())),
        "unresolved": {
            "mechanic_questions": mechanic_unresolved,
            "source_retest_required_stage_ids": source_retest_stage_ids,
            "review_adjudicated_not_retest_stage_ids": (
                adjudicated_not_retest_stage_ids
            ),
            "review_remaining_retest_required_stage_ids": (
                review_remaining_retest_stage_ids
            ),
            "external_mechanism_boundaries": external_boundaries,
        },
        "publication_gate": {
            "observed_fact_promotion_scope": "review_artifact_only",
            "observed_fact_promotion_count": decision_counts[
                "PROMOTE_OBSERVED_FACT_ONLY"
            ],
            "simulator_comparison_complete": False,
            "replacement_formula_identified": False,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "reason": (
                "The review retains exact observed facts but has no simulator comparison "
                "or independently passed identification/holdout gate."
            ),
        },
        "mechanics_registry_mutations": [],
        "simulator_overrides": [],
        "mechanics_registry_modified": False,
        "simulator_modified": False,
    }


def run_from_path(
    audit_path: str | Path = DEFAULT_AUDIT,
    *,
    summary_path: str | Path | None = None,
    preregistration_path: str | Path = DEFAULT_PREREGISTRATION,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Load Phase-12 artifacts, review them, and optionally write the review."""

    audit_file = Path(audit_path).expanduser().resolve()
    audit = _load_object(audit_file, "Phase-12 audit")
    if summary_path is None:
        declared = _mapping(audit.get("sources")).get("calibration_summary")
        if not isinstance(declared, str) or not declared:
            raise FuryPhase12ReviewError(
                "audit has no calibration summary source; pass summary_path explicitly"
            )
        summary_file = Path(declared).expanduser().resolve()
    else:
        summary_file = Path(summary_path).expanduser().resolve()
    prereg_file = Path(preregistration_path).expanduser().resolve()
    summary = _load_object(summary_file, "calibration summary")
    action_state_evidence = _linked_action_state_evidence(summary, summary_file)
    document = build_phase12_review(
        audit,
        summary,
        _load_object(prereg_file, "Phase-12 preregistration"),
        audit_path=audit_file,
        summary_path=summary_file,
        preregistration_path=prereg_file,
        action_state_evidence=action_state_evidence,
    )
    if output is not None:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    return document


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--summary",
        type=Path,
        help="calibration summary (default: audit.sources.calibration_summary)",
    )
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_PREREGISTRATION
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        document = run_from_path(
            args.audit,
            summary_path=args.summary,
            preregistration_path=args.preregistration,
            output=args.output,
        )
    except FuryPhase12ReviewError as error:
        print(f"Phase-12 evidence review failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": document["status"],
                "output": str(args.output.resolve()),
                "decision_counts": document["decision_counts"],
                "simulator_patch_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__ = [
    "DEFAULT_AUDIT",
    "DEFAULT_OUTPUT",
    "DEFAULT_PREREGISTRATION",
    "FuryPhase12ReviewError",
    "REVIEWER",
    "build_phase12_review",
    "run_from_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
