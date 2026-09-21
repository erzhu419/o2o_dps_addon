"""Run one exact d900 Chronicle player-wave controller without Cat.

This is the D1 expression test from ``GPT_diagnosis.md``.  The focal action
program comes from one observed player-wave and its exact historical build;
the target HP/armor/attackability and responsive teammates remain explicit
development hypotheses, so the result is not yet a DPS comparison.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.offline_team_wave_policy_v1 import (
    compile_offline_team_wave_feedback_policy_v1,
    load_offline_team_wave_record_v1,
)
from o2o_dps.offline_wave_policy_v1 import (
    OfflineWaveRuntimeBindingV1,
    build_offline_wave_policy_runtime_v1,
    offline_policy_contribution_receipt_v1,
    offline_policy_execution_contribution_receipt_v1,
)
from o2o_dps.responsive_action_program_replay_v1 import (
    NativeDynamicV4ResponsiveActionProgramReplayV1,
)
from o2o_dps.responsive_incantagos_driven_bridge_v1 import (
    IncantagosDevelopmentDrivenBridgeV1,
)
from o2o_dps.responsive_team_frozen_validation_source_v1 import (
    verify_frozen_validation_source_v1,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
)
from o2o_dps.responsive_team_runtime_store_v1 import (
    open_responsive_team_runtime_store_v1,
)
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
)
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ATTACKABILITY_MODES,
    OBSERVED_ACTIVITY_WINDOWS,
)
from scripts.development_responsive_upper_kara_trash_full_wave_v1 import (
    _SparseTargetTimelineBridge,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import (
    ENCOUNTER_ID,
    FOCAL_GUID,
    INSTANCE_ID,
    WAVE_ID,
    build_case,
)


SCHEMA = "development_offline_wave_policy_d900/v1"


def _exact_build_segment_ref(exact_build: Mapping[str, Any]) -> str:
    source = exact_build.get("source_identity")
    valid_from = exact_build.get("source_valid_from")
    if not isinstance(source, Mapping) or not isinstance(valid_from, Mapping):
        raise ValueError("exact build lacks source identity or validity boundary")
    if (
        source.get("instance_id") != INSTANCE_ID
        or str(source.get("player_guid", "")).casefold() != FOCAL_GUID.casefold()
        or valid_from.get("encounter_id") != ENCOUNTER_ID
        or exact_build.get("historical_runtime_executable") is not True
    ):
        raise ValueError("exact build does not bind the selected d900 player-wave")
    segment_id = source.get("build_segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("exact build lacks build_segment_id")
    return f"exact-build-segment:{INSTANCE_ID}:{FOCAL_GUID.casefold()}:{segment_id}"


def _runtime_binding(policy: Any, fixed_case: Any) -> OfflineWaveRuntimeBindingV1:
    by_guid = {
        row.target_guid.casefold(): row.target_index
        for row in fixed_case.occurrence_index_registry
    }
    missing = [
        guid for guid in policy.source_target_guids if guid.casefold() not in by_guid
    ]
    if missing:
        raise ValueError(f"source targets are absent from the native registry: {missing}")
    return OfflineWaveRuntimeBindingV1(
        runtime_wave_id=WAVE_ID,
        source_target_guids=policy.source_target_guids,
        target_indexes=tuple(by_guid[guid.casefold()] for guid in policy.source_target_guids),
    )


def _cat_resolver_bomb_factory():
    def fail_if_called(*_args: Any, **_kwargs: Any):
        raise AssertionError("CAT_RESOLVER_BOMB_CALLED")

    return fail_if_called


def _compact_audit(audit_events: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    counts = Counter(row.get("kind", "UNKNOWN") for row in audit_events)
    action_events = [
        row
        for row in audit_events
        if (
            row.get("kind")
            in {
                "SOURCE_DEMONSTRATION_ACTION_SELECTED",
                "SOURCE_ACTION_MASKED_BY_EXACT_NATIVE_SURFACE",
                "SOURCE_ACTION_MASKED_AFTER_EXECUTION_WINDOW",
                "OFFLINE_PROPOSAL_ACTION_NOT_EXECUTED",
            }
            or (
                row.get("kind") == "OFFLINE_PROPOSAL_EXECUTION_CONFIRMED"
                and bool(row.get("accepted_actions"))
            )
        )
    ]
    return {
        "event_kind_counts": dict(sorted(counts.items())),
        "action_events": action_events,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage5_parts = args.stage5.parts
    try:
        locator = Path(
            *stage5_parts[stage5_parts.index("offline_data") :]
        ).as_posix()
    except ValueError as error:
        raise ValueError("stage5 path must include offline_data") from error
    frozen = verify_frozen_validation_source_v1(
        json.loads(args.frozen_dispatch.read_text(encoding="utf-8")),
        instance_id=INSTANCE_ID,
        component_id=args.component_id,
        partition_locator=locator,
        stage5_content_sha256=args.stage5_content_sha256,
        partition_compressed_file_sha256=args.partition_compressed_file_sha256,
    )
    fixed, case = build_case(args)
    exact_build = json.loads(args.exact_build.read_text(encoding="utf-8"))
    team_wave = load_offline_team_wave_record_v1(
        args.stage5,
        wave_id=args.wave_id,
    )
    policy = compile_offline_team_wave_feedback_policy_v1(
        team_wave,
        player_guid=FOCAL_GUID,
        build_segment_ref=_exact_build_segment_ref(exact_build),
        encounter_name="Upper Tower of Karazhan d900 trash wave 1",
    )
    runtime_binding = _runtime_binding(policy, fixed)
    program, source_binding = build_offline_wave_policy_runtime_v1(
        policy,
        runtime_binding,
        domain_fallback_factory=_cat_resolver_bomb_factory,
    )

    rows: list[dict[str, Any]] = []
    for seed_offset in range(args.seed_count):
        seed = args.simulator_seed + seed_offset
        teammate_seed = args.teammate_seed + seed_offset
        loaded = open_responsive_team_runtime_store_v1(
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
        session_refs: list[Any] = []

        def open_tracked_session():
            session = source_binding.open_session()
            session_refs.append(session)
            return session

        tracked_binding = replace(
            source_binding,
            resolver_factory=open_tracked_session,
        )
        focus_refs: list[Any] = []
        timeline_refs: list[Any] = []

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
                if args.route_focus
                else driven
            )
            if args.route_focus:
                focus_refs.append(focused)
            timeline = _SparseTargetTimelineBridge(focused)
            timeline_refs.append(timeline)
            return timeline

        replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
            bridge_factory=bridge_factory,
            case_factory=lambda _seed: case,
            observation_projector_factory=lambda current: (
                build_incantagos_v4_prefix_projector_v1(fixed, current)
            ),
            imported_bindings=(tracked_binding,),
        )
        started = perf_counter()
        try:
            outcome = replay.replay(
                seed,
                program,
                max_decisions=args.max_decisions,
                stop_at_or_after_ms=args.stop_ms,
            )
        finally:
            with closing(loaded.model):
                pass
        wall_seconds = round(perf_counter() - started, 3)
        session = session_refs[-1] if session_refs else None
        audit = session.audit_events if session is not None else ()
        execution_contribution = offline_policy_execution_contribution_receipt_v1(
            audit
        )
        semantics = outcome.state.get("dynamic_target_semantics", {})
        targets = semantics.get("targets", ()) if isinstance(semantics, Mapping) else ()
        rows.append(
            {
                "seed": seed,
                "teammate_seed": teammate_seed,
                "status": outcome.status.value,
                "invalid_reason": outcome.invalid_reason,
                "elapsed_ms": outcome.elapsed_ms,
                "effective_damage": outcome.effective_damage,
                "wall_seconds": wall_seconds,
                "cat_resolver_bomb_calls": (
                    session.domain_fallback_calls if session is not None else None
                ),
                "committed_source_ordinals": (
                    list(session.committed_source_ordinals)
                    if session is not None
                    else []
                ),
                "execution_contribution": execution_contribution,
                "offline_audit": _compact_audit(audit),
                "terminal_finished": outcome.state.get("finished"),
                "terminal_all_targets_dead": bool(targets)
                and all(row.get("dead") is True for row in targets),
                "terminal_targets": [
                    {
                        "target_index": row.get("target_index"),
                        "current_health": row.get("current_health"),
                        "maximum_health": row.get("maximum_health"),
                        "dead": row.get("dead"),
                        "attackable": row.get("attackable"),
                    }
                    for row in targets
                ],
                "route_focus_receipts": (
                    focus_refs[-1].route_focus_receipts
                    if args.route_focus and focus_refs
                    else []
                ),
                "target_timeline_snapshots": (
                    timeline_refs[-1].snapshots if timeline_refs else []
                ),
            }
        )

    independent_complete = bool(rows) and all(
        row["status"] == "COMPLETE"
        and row["cat_resolver_bomb_calls"] == 0
        and len(
            row["execution_contribution"]["offline_accepted_execution"][
                "accepted_action_sequence"
            ]
        )
        > 0
        for row in rows
    )
    return {
        "schema": SCHEMA,
        "status": (
            "COMPLETE_INDEPENDENT_OFFLINE_CONTROLLER_D1"
            if independent_complete
            else "D1_ACCEPTANCE_NOT_MET"
        ),
        "comparison_authorized": False,
        "deployment_authorized": False,
        "instance_id": INSTANCE_ID,
        "encounter_id": ENCOUNTER_ID,
        "wave_id": args.wave_id,
        "focal_player_guid": FOCAL_GUID,
        "frozen_source_binding": frozen,
        "build_segment_ref": policy.build_segment_refs[0],
        "runtime_binding": runtime_binding.to_dict(),
        "compiled_policy_contribution": offline_policy_contribution_receipt_v1(
            policy
        ),
        "compiled_action_count": len(policy.actions),
        "compiled_action_sequence": [row.action_key for row in policy.actions],
        "compiled_target_sequence": [
            row.source_target_ordinal for row in policy.actions
        ],
        "source_target_guids": list(policy.source_target_guids),
        "cat_resolver_in_supported_path": False,
        "cat_resolver_replaced_by_fail_fast_bomb": True,
        "route_focus_hypothesis_applied": args.route_focus,
        "source_route_priority_observed": False,
        "target_hp_armor_attackability_are_model_hypotheses": True,
        "responsive_team_is_model_not_historical_replay": True,
        "paired_replays": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "metadata",
        "stage5",
        "exact-build",
        "frozen-dispatch",
        "runtime-store",
        "bridge",
        "simulator-root",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "component-id",
        "stage5-content-sha256",
        "partition-compressed-file-sha256",
        "result-sha256",
        "model-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--wave-id", default=WAVE_ID)
    parser.add_argument("--simulator-seed", type=int, default=2026092001)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--max-decisions", type=int, default=1000)
    parser.add_argument("--stop-ms", type=int)
    parser.add_argument("--route-focus", action="store_true")
    parser.add_argument(
        "--attackability-mode",
        choices=ATTACKABILITY_MODES,
        default=OBSERVED_ACTIVITY_WINDOWS,
    )
    args = parser.parse_args()
    if not 1 <= args.seed_count <= 3:
        parser.error("development D1 probe accepts 1-3 seeds")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
