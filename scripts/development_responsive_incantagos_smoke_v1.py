"""Run one real Incantagos dynamic-v4 responsive-team protocol smoke.

The historical attempt supplies the target universe, target lifecycle, and
observed actor roster.  The teammate sampler is intentionally deterministic
and synthetic: this script proves the runtime/wire integration only and never
produces performance-comparison evidence.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_external_compact_target_reducer_v1 import (
    reduce_external_team_wave_record,
)
from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.responsive_team_bridge_adapter_v1 import (
    LoadedResponsiveTeammateModelV1,
    ResponsiveTeamBridgeAdapterV1Error,
    TeammateModelProvenanceV1,
)
from o2o_dps.responsive_incantagos_development_session_v1 import (
    bind_responsive_incantagos_development_session_v1,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
)
from o2o_dps.responsive_team_runtime_store_v1 import (
    open_responsive_team_runtime_store_v1,
)
from o2o_dps.responsive_team_frozen_validation_source_v1 import (
    verify_frozen_validation_source_v1,
)
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_compact_encounter_model_v1 import (
    build_compact_encounter_development_model_v1,
)
from o2o_dps.upper_kara_incantagos_actionability_contract_v1 import (
    build_incantagos_target_actionability_contract_v1,
)
from o2o_dps.upper_kara_resolved_dynamic_v4_adapter_v1 import (
    compile_resolved_incantagos_dynamic_v4_case_v1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    compile_responsive_incantagos_case_v1,
)
from o2o_dps.upper_kara_route_wave_registry_v1 import (
    build_route_wave_registry_v1,
)
from o2o_dps.upper_kara_target_universe_audit_v1 import (
    audit_upper_kara_target_universe_v1,
)
from o2o_dps.upper_kara_target_universe_resolution_v1 import (
    resolve_upper_kara_target_universe_v1,
)


SIMULATOR_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
INSTANCE_ID = "f5f15188-c75c-41d1-baa1-196c2926d409"
ENCOUNTER_ID = "a65e8699-7d2b-4cb1-b054-b146ee447e0a"
WAVE_ID = f"{ENCOUNTER_ID}:external-v2-wave:1"
FOCAL_PLAYER_GUID = "0x00000000002E976B"

METADATA_PATH = (
    PROJECT_ROOT
    / "offline_data/chronicle_raw/external_api/v1/objects/sha256/f9"
    / "f9de260bea9dd9f172f07a7b369e7a160f25931d228a94de6b6f246216bf0fc1.json"
)
STAGE5_PATH = (
    PROJECT_ROOT
    / "offline_data/derived/chronicle_external_team_wave_model/v2"
    / "utk_postfix_dev_20260901"
    / "f5f15188-c75c-41d1-baa1-196c2926d409."
    "ffc185c4c31d65216cd5f96799a02e7487f06b105eb1503f8d9e49da8fd74c1e.jsonl.gz"
)
BASE_REQUEST_PATH = PROJECT_ROOT / "configs/wowsims/fury_warrior_clean_dual.json"
BRIDGE_PATH = (
    PROJECT_ROOT
    / "bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.windows-amd64.exe"
)

DEFAULT_COMPONENT_ID = (
    "669946f2520a9fe260188d4a4bac0af6028c6bb7ea8d4478cc76f4efa4be37be"
)
DEFAULT_STAGE5_CONTENT_SHA256 = (
    "8acc5c95e38ed2683136824d53e5f2776cb553ba1f42860ffa17de38ac76ce64"
)


class _FixedDevelopmentSampler:
    """A protocol witness, not a learned or expert teammate model."""

    variant_id = ABLATION_D
    model_content_sha256 = "e" * 64

    def sample_delay(
        self, *, actor: Mapping[str, Any], timing_state: Mapping[str, Any], rng: Any
    ) -> dict[str, Any]:
        return {
            "delay_ms": 100,
            "delay_bucket": 1,
            "context_level": "GLOBAL",
            "context": ["GLOBAL"],
            "support": 1,
        }

    def sample_emission(
        self, *, actor: Mapping[str, Any], emission_state: Mapping[str, Any], rng: Any
    ) -> dict[str, Any]:
        return {
            "event_type": "DMG",
            "spell_id": 1337,
            "spell_name": "development-smoke-damage",
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "attributed_player_guid": actor["player_guid"],
            "exact_source_guid": actor["player_guid"],
            "target_mode": "STAY_ALIVE",
            "sampled_damage": 7,
            "damage_bucket": 3,
            "context_level": "GLOBAL",
            "context": ["GLOBAL"],
            "support": 1,
        }


def _reach_initial_wake(bridge: Any, wake: Mapping[str, Any]) -> Mapping[str, Any]:
    """Reach an armed absolute-time wake from the bridge's current time."""

    wake_time_ms = wake["time_ms"]
    state = bridge.state()
    while state.get("wake_ready") is None and not state.get("finished", False):
        current_time_ms = state["time_ms"]
        if current_time_ms > wake_time_ms:
            raise RuntimeError("bridge advanced beyond the armed responsive wake")
        if current_time_ms == wake_time_ms:
            return state
        # Empirical delay bucket zero is valid.  The Go runtime marks such a
        # wake ready while arming it, whereas SimulatorBridge.wait deliberately
        # rejects a zero-duration policy wait.
        had_candidate_input = state.get("needs_input", True)
        if had_candidate_input:
            bridge.wait(wake_time_ms - current_time_ms)
        next_state = bridge.advance()
        if (
            next_state.get("wake_ready") is None
            and not next_state.get("finished", False)
            and next_state["time_ms"] <= current_time_ms
        ):
            # A teammate wake can suspend a candidate wait at the exact same
            # timestamp.  After the entire teammate cohort is drained, one
            # advance resumes that already-scheduled candidate deadline and
            # returns input without moving the clock.  The next loop then
            # submits the remaining positive wait.
            if not had_candidate_input and next_state.get("needs_input") is True:
                state = next_state
                continue
            raise RuntimeError("bridge made no progress toward the responsive wake")
        state = next_state
    return state


def _compact_emitted_event(
    emitted: Mapping[str, Any], *, event_number: int
) -> dict[str, Any]:
    sampled = emitted["sampled_emission"]
    wire_event = emitted["wire_event"]
    receipt = emitted["wire_receipt"]
    return {
        "event_number": event_number,
        "time_ms": receipt["time_ms"],
        "actor_guid": receipt["actor_guid"],
        "actor_event_sequence": emitted["scheduler_order_key"][2],
        "event_type": sampled["event_type"],
        "spell_id": sampled.get("spell_id"),
        "spell_name": sampled.get("spell_name"),
        "target_mode": sampled.get("target_mode"),
        "runtime_actionability_projection": sampled.get(
            "runtime_actionability_projection"
        ),
        "damage_bucket": sampled.get("damage_bucket"),
        "sampled_damage": sampled["sampled_damage"],
        "target_selection_basis": emitted["target_selection_basis"],
        "model_visible_alive_target_count": len(
            emitted["model_input_live_prefix"]["target_state"][
                "alive_target_guids"
            ]
        ),
        "wire_target_index": wire_event["target_index"],
        "wire_observed_damage": wire_event["observed_damage"],
        "wire_requested_damage": wire_event["requested_damage"],
        "wire_status": receipt["status"],
        "wire_applied_damage": receipt["applied_damage"],
        "wire_damage_ordinal": receipt["damage_ordinal"],
        "wire_current_health": receipt["current_health"],
        "wire_killed": receipt["killed"],
    }


def _drive_bounded_responsive_events(
    *,
    bridge: Any,
    session: Any,
    max_responsive_events: int,
    max_simulator_time_ms: int | None = None,
) -> dict[str, Any]:
    """Drive a wait-only candidate suffix through several real team wakes."""

    if (
        isinstance(max_responsive_events, bool)
        or not isinstance(max_responsive_events, int)
        or max_responsive_events <= 0
    ):
        raise ValueError("max_responsive_events must be a positive integer")
    if max_simulator_time_ms is not None and (
        isinstance(max_simulator_time_ms, bool)
        or not isinstance(max_simulator_time_ms, int)
        or max_simulator_time_ms < 0
    ):
        raise ValueError("max_simulator_time_ms must be a nonnegative integer")
    wake = session.initial_wake
    trace: list[dict[str, Any]] = []
    termination_reason = "NO_RESPONSIVE_WAKE_BEFORE_EXCLUSIVE_HORIZON"
    while wake is not None and len(trace) < max_responsive_events:
        if (
            max_simulator_time_ms is not None
            and wake["time_ms"] > max_simulator_time_ms
        ):
            termination_reason = "SIMULATOR_TIME_LIMIT_WITH_PENDING_WAKE"
            break
        at_wake = _reach_initial_wake(bridge, wake)
        if at_wake.get("finished", False):
            termination_reason = "SIMULATOR_FINISHED_BEFORE_ARMED_WAKE"
            break
        ready = at_wake.get("wake_ready")
        if not isinstance(ready, Mapping) or ready.get("wake_id") != wake["wake_id"]:
            raise RuntimeError("bridge exposed a different responsive wake than armed")
        try:
            step = session.adapter.emit_global_ready_and_rearm()
        except ResponsiveTeamBridgeAdapterV1Error as error:
            raise RuntimeError(
                f"responsive emission failed after {len(trace)} accepted events: "
                f"{error}"
            ) from error
        emitted = step["emitted"]
        trace.append(
            _compact_emitted_event(emitted, event_number=len(trace) + 1)
        )
        wake = step["next_wake"]
        if step["status"] == "EMITTED_TEAM_KILL_CLOCK_COMPLETE":
            termination_reason = "TEAM_KILL_CLOCK_COMPLETE"
            break
        if wake is None:
            termination_reason = "NO_RESPONSIVE_WAKE_BEFORE_EXCLUSIVE_HORIZON"
            break
    else:
        if len(trace) >= max_responsive_events:
            termination_reason = "RESPONSIVE_EVENT_LIMIT_WITH_PENDING_WAKE"

    # Include candidate white-hit and any other authoritative applications
    # that occurred after the final sampled wake in the local causal prefix.
    final_prefix_sync = session.adapter.sync_authoritative_damage_prefix()
    final_state = bridge.state()
    status_counts: dict[str, int] = {}
    event_type_counts: dict[str, int] = {}
    actor_counts: dict[str, int] = {}
    time_counts: dict[int, int] = {}
    for row in trace:
        status = row["wire_status"]
        event_type = row["event_type"]
        actor_guid = row["actor_guid"]
        time_ms = row["time_ms"]
        status_counts[status] = status_counts.get(status, 0) + 1
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        actor_counts[actor_guid] = actor_counts.get(actor_guid, 0) + 1
        time_counts[time_ms] = time_counts.get(time_ms, 0) + 1
    zero_observed_damage_count = sum(
        row["wire_observed_damage"] == 0 for row in trace
    )
    zero_hostile_requested_damage_count = sum(
        row["wire_requested_damage"] == 0 for row in trace
    )
    untargeted_zero_observed_damage_count = sum(
        row["event_type"] == "DMG"
        and row["wire_observed_damage"] == 0
        and row["wire_target_index"] is None
        for row in trace
    )
    targeted_zero_damage_count = sum(
        row["event_type"] == "DMG"
        and row["wire_requested_damage"] == 0
        and row["wire_target_index"] is not None
        for row in trace
    )
    target_rows = final_state["dynamic_team_background"]["targets"]
    projected_events = [
        row["runtime_actionability_projection"]
        for row in trace
        if isinstance(row["runtime_actionability_projection"], Mapping)
    ]
    return {
        "schema": "development_responsive_incantagos_continuous_trace/v2",
        "status": "COMPLETE_BOUNDED_RESPONSIVE_DEVELOPMENT_TRACE",
        "termination_reason": termination_reason,
        "responsive_event_limit": max_responsive_events,
        "simulator_time_limit_ms": max_simulator_time_ms,
        "responsive_event_count": len(trace),
        "actionable_projection_event_count": len(projected_events),
        "diagnostic_support_excluded_event_count": sum(
            row["excluded_diagnostic_support"] > 0 for row in projected_events
        ),
        "distinct_responsive_actor_count": len(actor_counts),
        "responsive_actor_event_counts": dict(sorted(actor_counts.items())),
        "same_timestamp_cohort_count": sum(
            count > 1 for count in time_counts.values()
        ),
        "maximum_same_timestamp_cohort_size": max(
            time_counts.values(), default=0
        ),
        "scheduler_order_keys_strictly_increasing": all(
            (
                trace[index - 1]["time_ms"],
                trace[index - 1]["actor_guid"],
                trace[index - 1]["actor_event_sequence"],
            )
            < (
                trace[index]["time_ms"],
                trace[index]["actor_guid"],
                trace[index]["actor_event_sequence"],
            )
            for index in range(1, len(trace))
        ),
        "event_time_min_ms": trace[0]["time_ms"] if trace else None,
        "event_time_max_ms": trace[-1]["time_ms"] if trace else None,
        "final_bridge_time_ms": final_state["time_ms"],
        "final_bridge_finished": final_state["finished"],
        "responsive_status_counts": dict(sorted(status_counts.items())),
        "responsive_event_type_counts": dict(sorted(event_type_counts.items())),
        "responsive_observed_damage_total": sum(
            row["wire_observed_damage"] for row in trace
        ),
        "responsive_requested_damage_total": sum(
            row["wire_requested_damage"] for row in trace
        ),
        "responsive_applied_damage_total": sum(
            row["wire_applied_damage"] for row in trace
        ),
        "zero_observed_damage_event_count": zero_observed_damage_count,
        "zero_hostile_requested_damage_event_count": (
            zero_hostile_requested_damage_count
        ),
        "targeted_zero_damage_dmg_count": targeted_zero_damage_count,
        "untargeted_zero_observed_damage_dmg_count": (
            untargeted_zero_observed_damage_count
        ),
        "positive_non_hostile_observed_damage_count": sum(
            row["event_type"] == "DMG"
            and row["wire_observed_damage"] > 0
            and row["wire_requested_damage"] == 0
            and row["wire_target_index"] is None
            for row in trace
        ),
        "positive_damage_application_count": sum(
            row["wire_damage_ordinal"] > 0
            and row["wire_applied_damage"] > 0
            for row in trace
        ),
        "targeted_zero_damage_application_count": sum(
            row["wire_damage_ordinal"] > 0
            and row["wire_requested_damage"] == 0
            for row in trace
        ),
        "untargeted_zero_damage_has_zero_ordinal": all(
            row["wire_damage_ordinal"] == 0
            for row in trace
            if row["event_type"] == "DMG"
            and row["wire_observed_damage"] == 0
            and row["wire_target_index"] is None
        ),
        "positive_non_hostile_damage_has_zero_ordinal": all(
            row["wire_damage_ordinal"] == 0
            for row in trace
            if row["event_type"] == "DMG"
            and row["wire_observed_damage"] > 0
            and row["wire_requested_damage"] == 0
            and row["wire_target_index"] is None
        ),
        "candidate_damage_receipt_cursor": session.adapter.candidate_cursor,
        "final_prefix_sync_count": len(final_prefix_sync),
        "alive_target_count": sum(not row["dead"] for row in target_rows),
        "dead_target_count": sum(row["dead"] for row in target_rows),
        "pending_wake_at_stop": dict(wake) if wake is not None else None,
        "event_trace": trace,
        "comparison_eligible": False,
        "training_authorized": False,
        "deployment_eligible": False,
    }


def _find_wave_record(
    path: Path, *, instance_id: str, encounter_id: str, wave_id: str
) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            identity = row.get("wave", {})
            if (
                identity.get("instance_id") == instance_id
                and identity.get("encounter_id") == encounter_id
                and identity.get("wave_id") == wave_id
            ):
                return row
    raise RuntimeError("selected Incantagos Stage-5 wave is absent")


def _build_case(
    *,
    metadata_path: Path,
    stage5_path: Path,
    stage5_content_sha256: str,
    component_id: str,
    instance_id: str = INSTANCE_ID,
    encounter_id: str = ENCOUNTER_ID,
    wave_id: str = WAVE_ID,
    focal_player_guid: str = FOCAL_PLAYER_GUID,
    source_split: str = "TRAIN",
) -> Any:
    if source_split not in {"TRAIN", "VALIDATION"}:
        raise ValueError("source_split must be TRAIN or VALIDATION")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    registry = build_route_wave_registry_v1([metadata])
    instance = next(
        row for row in registry["instances"] if row["instance_id"] == instance_id
    )
    encounter = next(
        row
        for row in instance["encounters"]
        if row["encounter_id"] == encounter_id
    )
    reduction = reduce_external_team_wave_record(
        stage5_path,
        instance_id=instance_id,
        encounter_id=encounter_id,
        wave_id=wave_id,
    )
    audit = audit_upper_kara_target_universe_v1(
        encounter,
        reduction,
        metadata_units=metadata["units"],
        metadata_players=metadata["players"],
    )
    resolution = resolve_upper_kara_target_universe_v1(encounter, audit)
    actionability = build_incantagos_target_actionability_contract_v1(
        resolution, candidate_damage_schools=["PHYSICAL"]
    )
    compact = build_compact_encounter_development_model_v1(
        encounter,
        reduction,
        resolution,
        model_id="incantagos-responsive-development-v1",
        focal_player_guid=focal_player_guid,
        armor_hypotheses=[0, 1721, 1961, 2861, 3761, 4211],
    )
    selected_armor = {
        occurrence_id: 1721
        for occurrence_id in actionability["collateral_candidate_occurrence_ids"]
    }
    base_request = json.loads(BASE_REQUEST_PATH.read_text(encoding="utf-8"))
    fixed = compile_resolved_incantagos_dynamic_v4_case_v1(
        compact,
        actionability,
        base_request,
        selected_armor_by_occurrence_id=selected_armor,
        horizon_ms=encounter["duration_ms"],
        target_level=63,
    )
    source_membership_evidence = {
        "component_id": component_id,
        "split": source_split,
        "model_training_held_out": source_split == "VALIDATION",
        "stage5_content_sha256": stage5_content_sha256,
        "heldout_performance_evidence_eligible": False,
        "comparison_authorized": False,
    }
    responsive = compile_responsive_incantagos_case_v1(
        fixed,
        _find_wave_record(
            stage5_path,
            instance_id=instance_id,
            encounter_id=encounter_id,
            wave_id=wave_id,
        ),
        source_membership_evidence=source_membership_evidence,
    )
    return fixed, responsive


def run(
    *,
    simulator_seed: int,
    teammate_seed: int,
    max_responsive_events: int = 1,
    max_simulator_time_ms: int | None = None,
    bridge_path: Path = BRIDGE_PATH,
    simulator_root: Path = SIMULATOR_ROOT,
    metadata_path: Path = METADATA_PATH,
    stage5_path: Path = STAGE5_PATH,
    stage5_content_sha256: str = DEFAULT_STAGE5_CONTENT_SHA256,
    component_id: str = DEFAULT_COMPONENT_ID,
    instance_id: str = INSTANCE_ID,
    encounter_id: str = ENCOUNTER_ID,
    wave_id: str = WAVE_ID,
    focal_player_guid: str = FOCAL_PLAYER_GUID,
    source_split: str = "TRAIN",
    frozen_dispatch_path: Path | None = None,
    partition_compressed_file_sha256: str | None = None,
    loaded_model: LoadedResponsiveTeammateModelV1 | None = None,
) -> dict[str, Any]:
    if source_split == "VALIDATION" and loaded_model is None:
        raise ValueError(
            "validation source requires a formal runtime-store model binding"
        )
    frozen_source_binding = None
    if source_split == "VALIDATION":
        if frozen_dispatch_path is None or partition_compressed_file_sha256 is None:
            raise ValueError(
                "validation source requires frozen dispatch and partition identity"
            )
        partition_parts = stage5_path.parts
        if "offline_data" not in partition_parts:
            raise ValueError("Stage-5 partition must be inside offline_data")
        locator = Path(
            *partition_parts[partition_parts.index("offline_data") :]
        ).as_posix()
        frozen_source_binding = verify_frozen_validation_source_v1(
            json.loads(frozen_dispatch_path.read_text(encoding="utf-8")),
            instance_id=instance_id,
            component_id=component_id,
            partition_locator=locator,
            stage5_content_sha256=stage5_content_sha256,
            partition_compressed_file_sha256=partition_compressed_file_sha256,
        )
    _fixed_case, case = _build_case(
        metadata_path=metadata_path,
        stage5_path=stage5_path,
        stage5_content_sha256=stage5_content_sha256,
        component_id=component_id,
        instance_id=instance_id,
        encounter_id=encounter_id,
        wave_id=wave_id,
        focal_player_guid=focal_player_guid,
        source_split=source_split,
    )
    if loaded_model is None:
        model = _FixedDevelopmentSampler()
        provenance = TeammateModelProvenanceV1(
            source_artifact_schema="development_fixed_sampler/v1",
            source_artifact_content_sha256="a" * 64,
            model_content_sha256=model.model_content_sha256,
            variant_id=model.variant_id,
            training_scope="SYNTHETIC_PROTOCOL_SMOKE_ONLY",
            current_source_held_out=source_split == "VALIDATION",
        )
        source_evidence = {
            "schema": "development-current-source-evidence/v1",
            "current_source_stage5_content_sha256": stage5_content_sha256,
            "current_source_component_id": component_id,
            "current_source_held_out": source_split == "VALIDATION",
            "comparison_authorized": False,
        }
        loaded_model = LoadedResponsiveTeammateModelV1(
            model=model,
            provenance=provenance,
            result_content_sha256=provenance.source_artifact_content_sha256,
            current_source_evidence=source_evidence,
        )
        model_kind = "SYNTHETIC_FIXED_D_PROTOCOL_SMOKE_NOT_TRAINED_ARTIFACT"
    else:
        provenance = loaded_model.provenance
        model_kind = (
            "FORMAL_HPC_D_RUNTIME_STORE_VALIDATION_SOURCE_DEVELOPMENT_SMOKE"
            if source_split == "VALIDATION"
            else "FORMAL_HPC_D_RUNTIME_STORE_TRAIN_OVERLAP_DEVELOPMENT_SMOKE"
        )
    bridge = SimulatorBridgeDynamicV4(
        bridge_path.expanduser().resolve(),
        cwd=simulator_root.expanduser().resolve(),
    )
    try:
        session = bind_responsive_incantagos_development_session_v1(
            bridge=bridge,
            case=case,
            loaded_model=loaded_model,
            simulator_seed=simulator_seed,
            teammate_seed=teammate_seed,
            pair_id="incantagos-development-smoke",
            branch_id="bounded-responsive-development-branch",
            candidate_suffix_id="candidate-waits-between-responsive-wakes",
        )
        wake = session.initial_wake
        if wake is None:
            raise RuntimeError("the responsive model produced no legal first wake")
        continuous = _drive_bounded_responsive_events(
            bridge=bridge,
            session=session,
            max_responsive_events=max_responsive_events,
            max_simulator_time_ms=max_simulator_time_ms,
        )
    finally:
        bridge.close()
        if bridge._process.stdout is not None:
            bridge._process.stdout.close()
        if bridge._process.stderr is not None:
            bridge._process.stderr.close()

    introductions = case.target_introduced_at_ms_by_guid.values()
    first_event = continuous["event_trace"][0]
    return {
        "schema": "development_responsive_incantagos_smoke/v2",
        "status": "REAL_INCANTAGOS_RESPONSIVE_WIRE_SMOKE_OK",
        "fixed_config_digest": case.receipt["fixed_dynamic_config_digest"],
        "responsive_config_digest": case.dynamic_config.content_sha256,
        "fixed_background_event_count_removed": case.receipt[
            "fixed_background_event_count_removed"
        ],
        "responsive_fixed_background_event_count": len(
            case.dynamic_config.background_damage_events
        ),
        "native_target_count": len(case.native_target_guids),
        "introduced_at_zero_count": sum(value == 0 for value in introductions),
        "future_target_count": sum(value > 0 for value in introductions),
        "first_future_target_intro_ms": min(
            value
            for value in case.target_introduced_at_ms_by_guid.values()
            if value > 0
        ),
        "observed_actor_count": len(case.actors),
        "responsive_teammate_count": len(case.teammate_player_guids),
        "wake_time_ms": wake["time_ms"],
        "wake_actor_guid": wake["actor_guid"],
        "bridge_time_ms": first_event["time_ms"],
        "model_visible_alive_target_count": first_event[
            "model_visible_alive_target_count"
        ],
        "sampled_event_type": first_event["event_type"],
        "sampled_spell_id": first_event["spell_id"],
        "sampled_spell_name": first_event["spell_name"],
        "sampled_damage_bucket": first_event["damage_bucket"],
        "sampled_damage": first_event["sampled_damage"],
        "wire_status": first_event["wire_status"],
        "wire_target_index": first_event["wire_target_index"],
        "wire_observed_damage": first_event["wire_observed_damage"],
        "wire_requested_damage": first_event["wire_requested_damage"],
        "wire_applied_damage": first_event["wire_applied_damage"],
        "wire_damage_ordinal": first_event["wire_damage_ordinal"],
        "wire_current_health": first_event["wire_current_health"],
        "continuous_trace": continuous,
        "comparison_eligible": False,
        "source_instance_id": instance_id,
        "source_encounter_id": encounter_id,
        "source_wave_id": wave_id,
        "source_focal_player_guid": focal_player_guid,
        "source_split": source_split,
        "model_training_held_out": source_split == "VALIDATION",
        "frozen_source_binding": frozen_source_binding,
        "model_kind": model_kind,
        "teammate_model_variant": provenance.variant_id,
        "teammate_model_content_sha256": provenance.model_content_sha256,
        "teammate_result_content_sha256": loaded_model.result_content_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator-seed", type=int, default=2_026_092_001)
    parser.add_argument("--teammate-seed", type=int, default=2_026_092_002)
    parser.add_argument("--max-responsive-events", type=int, default=1)
    parser.add_argument("--max-simulator-time-ms", type=int)
    parser.add_argument("--bridge", type=Path, default=BRIDGE_PATH)
    parser.add_argument("--simulator-root", type=Path, default=SIMULATOR_ROOT)
    parser.add_argument("--metadata", type=Path, default=METADATA_PATH)
    parser.add_argument("--stage5", type=Path, default=STAGE5_PATH)
    parser.add_argument(
        "--stage5-content-sha256", default=DEFAULT_STAGE5_CONTENT_SHA256
    )
    parser.add_argument("--component-id", default=DEFAULT_COMPONENT_ID)
    parser.add_argument("--instance-id", default=INSTANCE_ID)
    parser.add_argument("--encounter-id", default=ENCOUNTER_ID)
    parser.add_argument("--wave-id", default=WAVE_ID)
    parser.add_argument("--focal-player-guid", default=FOCAL_PLAYER_GUID)
    parser.add_argument(
        "--source-split", choices=("TRAIN", "VALIDATION"), default="TRAIN"
    )
    parser.add_argument("--frozen-dispatch", type=Path)
    parser.add_argument("--partition-compressed-file-sha256")
    parser.add_argument("--runtime-store", type=Path)
    parser.add_argument("--result-sha256")
    parser.add_argument("--model-sha256")
    arguments = parser.parse_args()
    store_arguments = (
        arguments.runtime_store,
        arguments.result_sha256,
        arguments.model_sha256,
    )
    if any(value is not None for value in store_arguments) and not all(
        value is not None for value in store_arguments
    ):
        parser.error(
            "--runtime-store, --result-sha256, and --model-sha256 are required together"
        )
    loaded_model = None
    if arguments.runtime_store is not None:
        loaded_model = open_responsive_team_runtime_store_v1(
            arguments.runtime_store,
            expected_result_content_sha256=arguments.result_sha256,
            expected_model_content_sha256=arguments.model_sha256,
            variant_id=ABLATION_D,
            current_source=CurrentSourceDeclarationV1(
                stage5_content_sha256=arguments.stage5_content_sha256,
                component_id=arguments.component_id,
                declared_held_out=arguments.source_split == "VALIDATION",
            ),
        )
    try:
        result = run(
            simulator_seed=arguments.simulator_seed,
            teammate_seed=arguments.teammate_seed,
            max_responsive_events=arguments.max_responsive_events,
            max_simulator_time_ms=arguments.max_simulator_time_ms,
            bridge_path=arguments.bridge,
            simulator_root=arguments.simulator_root,
            metadata_path=arguments.metadata,
            stage5_path=arguments.stage5,
            stage5_content_sha256=arguments.stage5_content_sha256,
            component_id=arguments.component_id,
            instance_id=arguments.instance_id,
            encounter_id=arguments.encounter_id,
            wave_id=arguments.wave_id,
            focal_player_guid=arguments.focal_player_guid,
            source_split=arguments.source_split,
            frozen_dispatch_path=arguments.frozen_dispatch,
            partition_compressed_file_sha256=(
                arguments.partition_compressed_file_sha256
            ),
            loaded_model=loaded_model,
        )
    finally:
        if loaded_model is not None:
            close = getattr(loaded_model.model, "close", None)
            if callable(close):
                close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
