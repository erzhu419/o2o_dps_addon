"""Development-only paired full-wave replay of the d900 three-target trash case.

The target route, armor, HP and reachability remain hypotheses.  This command
measures bridge completion and source-program behavior, not historical DPS.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramOriginV1,
)
from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.contra260817_fury_full_policy_rollout_v4 import Contra260817SimulatorInputsV4
from o2o_dps.contra260817_incantagos_v4_reactive_session_v1 import (
    build_contra260817_incantagos_v4_imported_binding_v1,
)
from o2o_dps.contra_incantagos_v4_policy_binding_v1 import bind_contra_incantagos_v4_policy_v1
from o2o_dps.deployed_contra_incantagos_v4_session_v1 import (
    build_deployed_contra_incantagos_v4_reactive_binding_v1,
)
from o2o_dps.deployed_contra_runtime_binding_v1 import load_deployed_contra_runtime_binding_v1
from o2o_dps.responsive_action_program_replay_v1 import NativeDynamicV4ResponsiveActionProgramReplayV1
from o2o_dps.responsive_incantagos_driven_bridge_v1 import IncantagosDevelopmentDrivenBridgeV1
from o2o_dps.responsive_team_frozen_validation_source_v1 import verify_frozen_validation_source_v1
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ATTACKABILITY_MODES,
    OBSERVED_ACTIVITY_WINDOWS,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import (
    ENCOUNTER_ID, FOCAL_GUID, INSTANCE_ID, WAVE_ID, _find_wave, build_case,
)


SNAPSHOT_TIMES_MS = (0, 3000, 6000, 9093, 11000, 12796, 15531)


def _historical_first_actor_delays(wave: Mapping[str, Any], teammates: tuple[str, ...]) -> dict[str, int]:
    """Descriptive same-wave first-event offsets, never policy observations."""

    origin = wave["descriptive_outcome"]["reconstruction_binding"]["window"]["start_offset_ms"]
    trace = wave["exact_trace"]
    wanted = set(teammates)
    delays: dict[str, int] = {}
    for row in wave["players"]:
        guid = row["player"]["guid"]
        if guid not in wanted:
            continue
        offsets = [
            trace[index]["anchor"]["offset_ms"] - origin
            for index in row["exact_trace_indices"]
            if trace[index]["trace_kind"] == "EXACT_PLAYER_EVENT"
        ]
        if offsets:
            first = min(offsets)
            if type(first) is not int or first < 0:
                raise ValueError("historical first actor event precedes the wave")
            delays[guid] = first
    return delays


class _DiagnosticFirstDelayModel:
    """Replace only each listed actor's first delay draw; keep later draws."""

    def __init__(self, base: Any, first_delay_ms_by_actor: Mapping[str, int]) -> None:
        self._base = base
        self._first = dict(first_delay_ms_by_actor)
        self.applied: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def sample_delay(self, *, actor, timing_state, rng):
        sampled = dict(self._base.sample_delay(actor=actor, timing_state=timing_state, rng=rng))
        guid = actor["player_guid"]
        if guid in self._first and guid not in self.applied:
            sampled["delay_ms"] = self._first[guid]
            sampled["context_level"] = "DIAGNOSTIC_SAME_WAVE_FIRST_EVENT_DELAY"
            self.applied[guid] = self._first[guid]
        return sampled


class _SparseTargetTimelineBridge:
    """Observe only reached native states at a few prespecified time anchors."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self._next_anchor = 0
        self.snapshots: list[dict] = []
        self.first_observed_dead_ms: dict[int, int] = {}

    def __enter__(self):
        self._bridge.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._bridge.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name):
        return getattr(self._bridge, name)

    def _observe(self, state: Mapping[str, Any]) -> None:
        time_ms = state.get("time_ms")
        semantics = state.get("dynamic_target_semantics")
        targets = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if type(time_ms) is not int or not isinstance(targets, list):
            return
        compact = [
            {
                "target_index": row.get("target_index"),
                "current_health": row.get("current_health"),
                "dead": row.get("dead"),
                "attackable": row.get("attackable"),
            }
            for row in targets
        ]
        for row in compact:
            index = row["target_index"]
            if type(index) is int and row["dead"] is True:
                self.first_observed_dead_ms.setdefault(index, time_ms)
        while self._next_anchor < len(SNAPSHOT_TIMES_MS) and time_ms >= SNAPSHOT_TIMES_MS[self._next_anchor]:
            self.snapshots.append({
                "requested_at_or_after_ms": SNAPSHOT_TIMES_MS[self._next_anchor],
                "observed_time_ms": time_ms,
                "finished": state.get("finished"),
                "targets": compact,
            })
            self._next_anchor += 1

    def _result(self, result):
        self._observe(result.state)
        return result

    def load_dynamic_v4(self, *args, **kwargs):
        return self._result(self._bridge.load_dynamic_v4(*args, **kwargs))

    def advance(self):
        state = self._bridge.advance()
        self._observe(state)
        return state

    def wait(self, *args, **kwargs):
        state = self._bridge.wait(*args, **kwargs)
        self._observe(state)
        return state

    def act(self, *args, **kwargs):
        return self._result(self._bridge.act(*args, **kwargs))

    def start_attack(self):
        return self._result(self._bridge.start_attack())

    def stop_cast(self):
        return self._result(self._bridge.stop_cast())

    def cancel_queue(self):
        return self._result(self._bridge.cancel_queue())

    def set_target(self, *args, **kwargs):
        return self._result(self._bridge.set_target(*args, **kwargs))


def run(args: argparse.Namespace) -> dict:
    parts = args.stage5.parts
    locator = Path(*parts[parts.index("offline_data"):]).as_posix()
    frozen = verify_frozen_validation_source_v1(
        json.loads(args.frozen_dispatch.read_text(encoding="utf-8")),
        instance_id=INSTANCE_ID,
        component_id=args.component_id,
        partition_locator=locator,
        stage5_content_sha256=args.stage5_content_sha256,
        partition_compressed_file_sha256=args.partition_compressed_file_sha256,
    )
    fixed, case = build_case(args)
    first_delays = (
        _historical_first_actor_delays(
            _find_wave(args.stage5, args.wave_id), tuple(case.teammate_player_guids)
        )
        if args.historical_first_wake_delay_diagnostic else {}
    )
    exact_build = json.loads(args.exact_build.read_text(encoding="utf-8"))
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    names = tuple(exact_build["equipped_item_names"])
    classifications = {row.occurrence_id: "elite" for row in fixed.occurrence_index_registry}
    shared = dict(
        equipment_request=case.request,
        equipped_item_names=names,
        classification_hypotheses_by_occurrence_id=classifications,
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
    )
    cat = build_incantagos_v4_cat_binding_v1(fixed, case, metadata, **shared)
    contra = bind_contra_incantagos_v4_policy_v1(fixed, case, metadata, **shared)
    inputs = replace(
        Contra260817SimulatorInputsV4(),
        equipped_mainhand_name=names[14],
        equipped_offhand_name=names[15],
        target_bindings=contra.exact_guid_target_bindings(),
    )
    bindings = [
        ImportedReactiveProgramBindingV1(
            binding_id=cat.receipt["source_policy_id"],
            source_policy_id=cat.receipt["source_policy_id"],
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
            resolver_factory=cat.open_session,
        ),
        build_contra260817_incantagos_v4_imported_binding_v1(contra, inputs),
    ]
    if args.deployed_runtime_binding is not None:
        bindings.append(build_deployed_contra_incantagos_v4_reactive_binding_v1(
            contra,
            load_deployed_contra_runtime_binding_v1(args.deployed_runtime_binding),
        ))
    if args.source != "all":
        selected = {
            "cat": 0, "contra260817": 1, "deployed_contra": 2,
        }[args.source]
        if selected >= len(bindings):
            raise RuntimeError(f"source {args.source} was not bound")
        bindings = [bindings[selected]]
    rows = []
    for seed_offset in range(args.seed_count):
            seed = args.simulator_seed + seed_offset
            teammate_seed = args.teammate_seed + seed_offset
            for binding in bindings:
                # Each paired lane owns its model connection and source session.
                base_loaded = open_responsive_team_runtime_store_v1(
                    args.runtime_store,
                    expected_result_content_sha256=args.result_sha256,
                    expected_model_content_sha256=args.model_sha256,
                    variant_id=ABLATION_D,
                    current_source=CurrentSourceDeclarationV1(
                        stage5_content_sha256=args.stage5_content_sha256,
                        component_id=args.component_id,
                        declared_held_out=True,
                    ),
                )
                diagnostic_model = (
                    _DiagnosticFirstDelayModel(base_loaded.model, first_delays)
                    if args.historical_first_wake_delay_diagnostic else None
                )
                loaded = (
                    replace(base_loaded, model=diagnostic_model)
                    if diagnostic_model is not None else base_loaded
                )
                focus_refs = []
                timeline_refs = []

                def bridge_factory():
                    driven = IncantagosDevelopmentDrivenBridgeV1(
                        bridge=SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root),
                        case=case,
                        loaded_model=loaded,
                        teammate_seed=teammate_seed,
                    )
                    focused = (
                        DoomguardCurrentStateRouteFocusedBridgeV1(
                            driven, case.native_target_guids
                        )
                        if args.route_focus else driven
                    )
                    if args.route_focus:
                        focus_refs.append(focused)
                    timeline = _SparseTargetTimelineBridge(focused)
                    timeline_refs.append(timeline)
                    return timeline

                replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
                    bridge_factory=bridge_factory,
                    case_factory=lambda _seed: case,
                    observation_projector_factory=lambda current: build_incantagos_v4_prefix_projector_v1(
                        fixed, current
                    ),
                    imported_bindings=(binding,),
                )
                program = CausalActionProgramV1(
                    program_id=f"trash-v4-{binding.source_policy_id}-development-probe",
                    selector=ImportedReactiveSelectorV1(
                        binding.binding_id,
                        binding.source_policy_id,
                        OBSERVATION_CONTRACT_ID_V1,
                    ),
                    origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
                )
                started = perf_counter()
                try:
                    outcome = replay.replay(
                        seed, program, max_decisions=args.max_decisions,
                        stop_at_or_after_ms=args.stop_ms,
                    )
                finally:
                    base_loaded.model.close()
                wall_seconds = round(perf_counter() - started, 3)
                selected = [row for row in outcome.receipts if row.get("kind") == "IMPORTED_REACTIVE_INCUMBENT_SELECTED"]
                semantics = outcome.state.get("dynamic_target_semantics", {})
                targets = semantics.get("targets", ()) if isinstance(semantics, dict) else ()
                terminal_targets = [
                    {
                        "target_index": row.get("target_index"),
                        "current_health": row.get("current_health"),
                        "maximum_health": row.get("maximum_health"),
                        "dead": row.get("dead"),
                        "attackable": row.get("attackable"),
                    }
                    for row in targets
                ]
                rows.append({
                    "seed": seed,
                    "teammate_seed": teammate_seed,
                    "source_policy_id": binding.source_policy_id,
                    "status": outcome.status.value,
                    "invalid_reason": outcome.invalid_reason,
                    "elapsed_ms": outcome.elapsed_ms,
                    "effective_damage": outcome.effective_damage,
                    "valid_development_completion": outcome.status.value == "COMPLETE",
                    "wall_seconds": wall_seconds,
                    "decision_count": len(selected),
                    "diagnostic_first_delay_overrides_applied": (
                        dict(diagnostic_model.applied) if diagnostic_model is not None else {}
                    ),
                    "terminal_finished": outcome.state.get("finished"),
                    "terminal_horizon_ms": case.dynamic_config.idle_advance_horizon_ms,
                    "terminal_all_targets_dead": bool(terminal_targets) and all(
                        row["dead"] is True for row in terminal_targets
                    ),
                    "terminal_targets": terminal_targets,
                    "target_timeline_snapshots": (
                        timeline_refs[-1].snapshots if timeline_refs else []
                    ),
                    "first_observed_dead_ms": (
                        timeline_refs[-1].first_observed_dead_ms if timeline_refs else {}
                    ),
                    "route_focus_receipts": (
                        focus_refs[-1].route_focus_receipts
                        if args.route_focus and focus_refs else []
                    ),
                    "first_decisions": [
                        {"time_ms": row.get("state_time_ms"), "gcd_action": row.get("gcd_action"),
                         "queue_op": row.get("queue_op"), "wait_ms": row.get("wait_ms")}
                        for row in selected[:6]
                    ],
                    "responsive_drive": next((row for row in reversed(outcome.receipts)
                                               if row.get("kind") == "DEVELOPMENT_RESPONSIVE_V4_DRIVE"), None),
                })
    return {
        "schema": "development_responsive_upper_kara_trash_full_wave/v1",
        "comparison_authorized": False,
        "status": "DEVELOPMENT_ONLY_ROUTE_PRIORITY_UNRESOLVED",
        "instance_id": INSTANCE_ID,
        "encounter_id": ENCOUNTER_ID,
        "wave_id": args.wave_id,
        "focal_player_guid": FOCAL_GUID,
        "historical_build_segment_id": "segment-0042",
        "frozen_source_binding": frozen,
        "target_contract_status": fixed.receipt["status"],
        "target_level_hypothesis": 60,
        "target_armor_hypothesis": 1721,
        "attackability_mode": args.attackability_mode,
        "current_state_route_focus_applied": args.route_focus,
        "historical_first_wake_delay_diagnostic": args.historical_first_wake_delay_diagnostic,
        "historical_first_wake_delay_actor_count": len(first_delays),
        "historical_first_wake_delay_source": (
            "SAME_HELDOUT_WAVE_FIRST_EXACT_PLAYER_EVENT_NOT_POLICY_OBSERVATION"
            if args.historical_first_wake_delay_diagnostic else None
        ),
        "source_policy_ids": [binding.source_policy_id for binding in bindings],
        "paired_replays": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "stage5", "exact-build", "frozen-dispatch", "runtime-store", "bridge", "simulator-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("component-id", "stage5-content-sha256", "partition-compressed-file-sha256", "result-sha256", "model-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--wave-id", default=WAVE_ID)
    parser.add_argument("--simulator-seed", type=int, default=2026092001)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--max-decisions", type=int, default=1000)
    parser.add_argument("--stop-ms", type=int)
    parser.add_argument("--source", choices=("all", "cat", "contra260817", "deployed_contra"), default="all")
    parser.add_argument("--route-focus", action="store_true")
    parser.add_argument("--attackability-mode", choices=ATTACKABILITY_MODES, default=OBSERVED_ACTIVITY_WINDOWS)
    parser.add_argument("--historical-first-wake-delay-diagnostic", action="store_true")
    parser.add_argument("--deployed-runtime-binding", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seed_count <= 3:
        parser.error("development probe accepts 1-3 paired seeds")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
