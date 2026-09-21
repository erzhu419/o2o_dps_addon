"""Compact remote worker for the causal-program campaign.

The candidate phase materializes one immutable program manifest per loadout.
Training shards only reconstruct that manifest, replay their assigned seeds,
and publish a lane sidecar followed by a small terminal commit.  The freeze
phase restores the exact ``programs, lanes`` input consumed by the v1 reducer;
evaluation and summarization retain the established v1 scientific semantics.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from .development_two_wave_cat_residual_sequence_v1 import (
    ZERO_RESIDUAL_SOURCE_REF_V1 as SEQUENCE_ZERO_RESIDUAL_SOURCE_REF_V1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    ZERO_RESIDUAL_SOURCE_REF_V1 as OVERLAY_ZERO_RESIDUAL_SOURCE_REF_V1,
)
from .upper_kara_causal_program_remote_compact_v2 import (
    CANDIDATE_MANIFEST_SCHEMA_V2,
    TRAIN_TERMINAL_COMMIT_SCHEMA_V2,
    artifact_path_ref_v2,
    build_candidate_manifest_v2,
    build_training_lane_sidecar_v2,
    build_training_terminal_commit_v2,
    read_candidate_manifest_v2,
    read_training_lane_sidecar_v2,
    read_training_terminal_commit_v2,
    restore_freeze_inputs_v2,
    write_candidate_manifest_v2,
    write_lane_sidecar_then_terminal_v2,
)
from .upper_kara_causal_program_remote_contract_v1 import (
    HeterogeneousTwoWaveSequenceSearchV7,
    TERMINAL_COMPLETE,
    TrainingShardV1,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
    terminal_status_for_lanes_v1,
)
from .upper_kara_causal_program_remote_worker_v1 import (
    DEFAULT_REPLAY_WORKERS,
    RemoteCausalProgramRuntimeV1,
    _build_cases_v1,
    _campaign_contract_v1,
    _failed_lane_v1,
    _lane_from_outcome_v1,
    _is_fixed_parent_search_v1,
    _is_searched_origin_v1,
    _paired_zero_program_v1,
    _program_receipt_v1,
    _utc_now,
    build_default_remote_runtime_v1,
    freeze_training_winner_v1,
    run_evaluation_shard_v1,
    summarize_evaluation_v1,
    train_terminal_name_v1,
)
from .upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from .upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_heterogeneous_two_wave_candidate_set_v7,
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
    policies_by_program_id_v7,
)


JSONMap = dict[str, Any]
STDOUT_RECEIPT_SCHEMA_V2 = (
    "upper_kara_causal_program_remote_worker_receipt/v2"
)


def candidate_manifest_name_v2(loadout_id: str) -> str:
    if not isinstance(loadout_id, str) or not loadout_id:
        raise ValueError("loadout_id must be non-empty")
    if Path(loadout_id).name != loadout_id or any(
        separator in loadout_id for separator in ("/", "\\")
    ):
        raise ValueError("loadout_id cannot contain a path separator")
    return f"candidate--{loadout_id}.json"


def train_lane_sidecar_name_v2(shard: TrainingShardV1) -> str:
    return f"{shard.work_id}.lanes.json"


def _campaign_shard_v2(
    campaign: Any, loadout_id: str, seed_shard_index: int
) -> TrainingShardV1:
    matches = [
        row
        for row in assign_training_shards_v1(campaign)
        if row.loadout_id == loadout_id
        and row.seed_shard_index == seed_shard_index
    ]
    if len(matches) != 1:
        raise ValueError("loadout_id/seed_shard_index is not one campaign shard")
    return matches[0]


def _candidate_programs_v2(
    campaign: Any,
    loadout_id: str,
    runtime: RemoteCausalProgramRuntimeV1,
) -> tuple[tuple[CausalActionProgramV1, ...], tuple[str, ...]]:
    if loadout_id not in campaign.loadout_ids:
        raise ValueError("loadout_id is not in the campaign")
    proposal_examples, _ = split_training_examples_for_selection_v1(campaign)
    proposal_cases, _, _ = _build_cases_v1(
        runtime, campaign, loadout_id, proposal_examples
    )
    searched = tuple(
        runtime.searched_program_generator(
            loadout_id=loadout_id,
            train_examples=proposal_examples,
            train_cases=proposal_cases,
        )
    )
    generation_result = getattr(
        runtime.searched_program_generator, "results_by_loadout", {}
    ).get(loadout_id)
    source_generation_result = getattr(
        generation_result, "source_candidate_set", generation_result
    )
    guide_ids = tuple(getattr(source_generation_result, "guide_ids", ()))
    incumbents = tuple(runtime.incumbent_program_factory(campaign.build_id))
    programs = (*incumbents, *searched)
    receipts = tuple(_program_receipt_v1(program) for program in programs)
    ids = [row["program_id"] for row in receipts]
    keys = [row["program_key"] for row in receipts]
    if not programs or len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise ValueError("candidate programs must have unique IDs and semantic keys")
    return tuple(programs), guide_ids


def generate_candidate_manifest_v2(
    *,
    campaign_path: str | Path,
    loadout_id: str,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    offline_guide_artifact_path: str | Path,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
) -> JSONMap:
    """Generate and publish one candidate family for one loadout exactly once."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    resolved_runtime = runtime or build_default_remote_runtime_v1(
        campaign,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
        offline_guide_artifact_path=offline_guide_artifact_path,
    )
    programs, guide_ids = _candidate_programs_v2(
        campaign, loadout_id, resolved_runtime
    )
    manifest = build_candidate_manifest_v2(
        campaign_id=campaign.campaign_id,
        build_id=campaign.build_id,
        campaign_contract=_campaign_contract_v1(campaign),
        loadout_id=loadout_id,
        proposal_guide_ids=guide_ids,
        programs=tuple(_program_receipt_v1(program) for program in programs),
    )
    # The compact writer is create-only.  A duplicate candidate task therefore
    # cannot silently replace the family consumed by any training shard.
    write_candidate_manifest_v2(output_path, manifest)
    return manifest


def _validate_manifest_for_campaign_v2(
    manifest: Mapping[str, Any], campaign: Any, loadout_id: str
) -> None:
    if (
        manifest.get("schema") != CANDIDATE_MANIFEST_SCHEMA_V2
        or manifest.get("campaign_id") != campaign.campaign_id
        or manifest.get("build_id") != campaign.build_id
        or manifest.get("campaign_contract") != _campaign_contract_v1(campaign)
        or manifest.get("loadout_id") != loadout_id
    ):
        raise ValueError("candidate manifest does not match the campaign/loadout")


def _programs_from_manifest_v2(
    manifest: Mapping[str, Any],
) -> tuple[tuple[CausalActionProgramV1, ...], dict[int, str]]:
    programs = tuple(
        causal_action_program_from_dict_v1(row["program"])
        for row in manifest["programs"]
    )
    identity = {
        id(program): receipt["program_ref"]
        for program, receipt in zip(
            programs, manifest["programs"], strict=True
        )
    }
    return programs, identity


def _training_contract_v2(
    campaign: Any,
    loadout_id: str,
    programs: Sequence[CausalActionProgramV1],
    lanes: Sequence[Mapping[str, Any]],
) -> JSONMap:
    searched = tuple(
        program
        for program in programs
        if _is_searched_origin_v1(program.origin)
    )
    sequence_zero_count = sum(
        SEQUENCE_ZERO_RESIDUAL_SOURCE_REF_V1 in program.source_refs
        for program in searched
    )
    return {
        "candidate_generation_used_full_training_panel": False,
        "candidate_generation_used_proposal_cohort_only": True,
        "selection_validation_examples_not_passed_to_generator": True,
        "searched_candidates_are_cat_residual_overlays": (
            campaign.search_spec is None
            and all(
                program.origin is ProgramOriginV1.SEARCHED
                and getattr(program.selector, "imported_fallback", None)
                is not None
                for program in searched
            )
        ),
        "searched_candidates_are_cat_residual_sequences": (
            bool(searched)
            and all(
                program.origin is ProgramOriginV1.SEARCHED_REACTIVE
                for program in searched
            )
            and sequence_zero_count == 1
        ),
        "searched_candidates_share_fixed_burst_parent": (
            _is_fixed_parent_search_v1(campaign)
            and bool(searched)
            and searched[0] == _paired_zero_program_v1(campaign, loadout_id)
        ),
        "paired_zero_program_included_by_full_identity": (
            sequence_zero_count == 1
            if sequence_zero_count
            else (
                sum(
                    program == _paired_zero_program_v1(campaign, loadout_id)
                    for program in searched
                )
                == 1
            )
        ),
        "zero_residual_exact_cat_included": (
            any(
                OVERLAY_ZERO_RESIDUAL_SOURCE_REF_V1 in program.source_refs
                or SEQUENCE_ZERO_RESIDUAL_SOURCE_REF_V1 in program.source_refs
                for program in searched
            )
        ),
        "replay_used_only_assigned_seed_shard": True,
        "evaluation_cases_materialized": False,
        "invalid_or_failed_lane_metrics_null": all(
            row["own_effective_damage"] is None
            and row["own_effective_dps"] is None
            and row["ttk_ms"] is None
            for row in lanes
            if row["status"] != TERMINAL_COMPLETE
        ),
        "candidate_manifest_generated_in_training_shard": False,
    }


def run_training_shard_compact_v2(
    *,
    campaign_path: str | Path,
    candidate_manifest_path: str | Path,
    loadout_id: str,
    seed_shard_index: int,
    lane_sidecar_path: str | Path,
    output_path: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    bridge_path: str | Path | None = None,
    bridge_cwd: str | Path | None = None,
    runtime_binding_path: str | Path | None = None,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
) -> JSONMap:
    """Replay a frozen candidate manifest; candidate generation is forbidden."""

    if isinstance(replay_workers, bool) or not isinstance(replay_workers, int):
        raise ValueError("replay_workers must be a positive integer")
    if replay_workers <= 0:
        raise ValueError("replay_workers must be a positive integer")
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    shard = _campaign_shard_v2(campaign, loadout_id, seed_shard_index)
    manifest = read_candidate_manifest_v2(candidate_manifest_path)
    _validate_manifest_for_campaign_v2(manifest, campaign, loadout_id)
    if runtime is None:
        if bridge_path is None or bridge_cwd is None or runtime_binding_path is None:
            raise ValueError("native replay paths are required")
        resolved_runtime = build_default_replay_runtime_v2(
            campaign,
            bridge_path=bridge_path,
            bridge_cwd=bridge_cwd,
            runtime_binding_path=runtime_binding_path,
        )
    else:
        resolved_runtime = runtime
    programs, program_identity = _programs_from_manifest_v2(manifest)
    shard_cases, cases_by_simulator, simulator_by_master = _build_cases_v1(
        resolved_runtime, campaign, loadout_id, shard.examples
    )
    case_by_master = dict(
        zip((row.seed for row in shard.examples), shard_cases, strict=True)
    )
    replay = resolved_runtime.program_replay_factory(
        loadout_id, cases_by_simulator
    )

    def evaluate(job: tuple[CausalActionProgramV1, Any]) -> JSONMap:
        program, example = job
        simulator_seed = simulator_by_master[example.seed]
        try:
            outcome = replay.replay(
                simulator_seed, program, max_decisions=campaign.max_decisions
            )
            lane = _lane_from_outcome_v1(
                outcome,
                case=case_by_master[example.seed],
                master_seed=example.seed,
                simulator_seed=simulator_seed,
            )
        except Exception as error:
            lane = _failed_lane_v1(
                master_seed=example.seed,
                simulator_seed=simulator_seed,
                error=error,
            )
        lane.update(
            {
                "program_ref": program_identity[id(program)],
                "first_wave_arrival_ms": example.first_wave_arrival_ms,
            }
        )
        return lane

    jobs = [
        (program, example)
        for program in programs
        for example in shard.examples
    ]
    with ThreadPoolExecutor(
        max_workers=min(replay_workers, len(jobs))
    ) as executor:
        lanes = list(executor.map(evaluate, jobs))
    terminal_status = terminal_status_for_lanes_v1(lanes)
    status_counts = dict(sorted(Counter(row["status"] for row in lanes).items()))
    metric = (
        {
            "mean_own_effective_damage_across_all_program_lanes": mean(
                float(row["own_effective_damage"]) for row in lanes
            )
        }
        if terminal_status == TERMINAL_COMPLETE
        else None
    )
    sidecar = build_training_lane_sidecar_v2(
        manifest,
        candidate_manifest_ref=artifact_path_ref_v2(candidate_manifest_path),
        seed_shard_index=seed_shard_index,
        examples=tuple(row.to_dict() for row in shard.examples),
        lanes=lanes,
    )
    terminal = build_training_terminal_commit_v2(
        manifest,
        sidecar,
        lane_sidecar_ref=artifact_path_ref_v2(lane_sidecar_path),
        terminal_status=terminal_status,
        lane_status_counts=status_counts,
        metric=metric,
        completed_at=_utc_now(),
        training_contract=_training_contract_v2(
            campaign, loadout_id, programs, lanes
        ),
    )
    write_lane_sidecar_then_terminal_v2(
        candidate_manifest_path=candidate_manifest_path,
        lane_sidecar_path=lane_sidecar_path,
        lane_sidecar=sidecar,
        terminal_path=output_path,
        terminal=terminal,
    )
    return terminal


def freeze_training_winner_compact_v2(
    *,
    campaign_path: str | Path,
    candidate_root: str | Path,
    training_root: str | Path,
    output_path: str | Path,
) -> JSONMap:
    """Restore compact bundles and run the unchanged v1 winner reducer."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    candidate_directory = Path(candidate_root).expanduser()
    training_directory = Path(training_root).expanduser()
    manifest_paths = {
        loadout_id: candidate_directory / candidate_manifest_name_v2(loadout_id)
        for loadout_id in campaign.loadout_ids
    }
    manifests = {
        loadout_id: read_candidate_manifest_v2(path)
        for loadout_id, path in manifest_paths.items()
    }
    for loadout_id, manifest in manifests.items():
        _validate_manifest_for_campaign_v2(manifest, campaign, loadout_id)

    def load_shard(
        requested_campaign: Any, shard: TrainingShardV1
    ) -> tuple[tuple[JSONMap, ...], tuple[JSONMap, ...]]:
        if requested_campaign != campaign:
            raise ValueError("freeze loader received a different campaign")
        manifest_path = manifest_paths[shard.loadout_id]
        sidecar_path = training_directory / train_lane_sidecar_name_v2(shard)
        terminal_path = training_directory / train_terminal_name_v1(shard)
        sidecar = read_training_lane_sidecar_v2(sidecar_path)
        terminal = read_training_terminal_commit_v2(terminal_path)
        expected_manifest_ref = artifact_path_ref_v2(manifest_path)
        expected_sidecar_ref = artifact_path_ref_v2(sidecar_path)
        if (
            sidecar.get("candidate_manifest_ref") != expected_manifest_ref
            or terminal.get("candidate_manifest_ref") != expected_manifest_ref
            or terminal.get("lane_sidecar_ref") != expected_sidecar_ref
        ):
            raise ValueError("compact training artifact path reference mismatch")
        if (
            terminal.get("schema") != TRAIN_TERMINAL_COMMIT_SCHEMA_V2
            or terminal.get("loadout_id") != shard.loadout_id
            or terminal.get("seed_shard_index") != shard.seed_shard_index
            or terminal.get("examples")
            != [row.to_dict() for row in shard.examples]
        ):
            raise ValueError(f"training commit identity mismatch: {shard.work_id}")
        return restore_freeze_inputs_v2(
            manifests[shard.loadout_id], sidecar, terminal
        )

    return freeze_training_winner_v1(
        campaign_path=campaign_path,
        training_root=training_directory,
        output_path=output_path,
        _training_shard_loader=load_shard,
    )


def _candidate_regeneration_forbidden_v2(**_: Any) -> Sequence[CausalActionProgramV1]:
    raise AssertionError("candidate generation is forbidden in replay-only workers")


def build_default_replay_runtime_v2(
    campaign: Any,
    *,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
) -> RemoteCausalProgramRuntimeV1:
    if isinstance(campaign.search_spec, HeterogeneousTwoWaveSequenceSearchV7):
        from .upper_kara_heterogeneous_two_wave_case_v1 import (
            build_heterogeneous_two_wave_observation_projector_v1,
        )
        from .upper_kara_two_wave_segment_replay_v1 import (
            build_two_wave_segment_program_replay_factory_v1,
        )

        candidate_set = build_heterogeneous_two_wave_candidate_set_v7(
            campaign.build_id, campaign.search_spec
        )
        replay_factory = build_two_wave_segment_program_replay_factory_v1(
            campaign.build_id,
            policies_by_program_id_v7(candidate_set),
            bridge_path=Path(bridge_path),
            bridge_cwd=Path(bridge_cwd),
            runtime_binding_path=Path(runtime_binding_path),
            observation_projector_factory=(
                build_heterogeneous_two_wave_observation_projector_v1
            ),
        )
        return RemoteCausalProgramRuntimeV1(
            case_builder=build_upper_kara_heterogeneous_two_wave_burst_case_v7,
            searched_program_generator=_candidate_regeneration_forbidden_v2,
            program_replay_factory=replay_factory,
        )

    from .upper_kara_imported_incumbent_program_v1 import (
        build_imported_incumbent_program_replay_factory_v1,
    )

    replay_factory = build_imported_incumbent_program_replay_factory_v1(
        campaign.build_id,
        bridge_path=Path(bridge_path),
        bridge_cwd=Path(bridge_cwd),
        runtime_binding_path=Path(runtime_binding_path),
    )
    return RemoteCausalProgramRuntimeV1(
        case_builder=build_upper_kara_continuous_two_wave_burst_case_v1,
        searched_program_generator=_candidate_regeneration_forbidden_v2,
        program_replay_factory=replay_factory,
    )


def _stdout_receipt_v2(
    *, phase: str, output_path: str | Path, payload: Mapping[str, Any]
) -> JSONMap:
    return {
        "schema": STDOUT_RECEIPT_SCHEMA_V2,
        "phase": phase,
        "status": payload.get("terminal_status", "COMPLETE"),
        "output": str(output_path),
        "candidate_count": (
            len(payload["programs"])
            if isinstance(payload.get("programs"), list)
            else payload.get("program_count")
        ),
        "replay_count": payload.get("lane_count"),
    }


def _print_receipt_v2(value: Mapping[str, Any]) -> None:
    wire = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(wire.encode("utf-8")) >= 1_024:
        raise AssertionError("worker stdout receipt exceeds 1 KiB")
    print(wire)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)

    def common_runtime(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--campaign", type=Path, required=True)
        subparser.add_argument("--bridge", type=Path, required=True)
        subparser.add_argument("--bridge-cwd", type=Path, required=True)
        subparser.add_argument("--runtime-binding", type=Path, required=True)
        subparser.add_argument(
            "--replay-workers", type=int, default=DEFAULT_REPLAY_WORKERS
        )
        subparser.add_argument("--output", type=Path, required=True)

    candidate = subparsers.add_parser("candidate")
    common_runtime(candidate)
    candidate.add_argument("--offline-guide-artifact", type=Path, required=True)
    candidate.add_argument("--loadout-id", required=True)

    train = subparsers.add_parser("train")
    common_runtime(train)
    train.add_argument("--candidate-manifest", type=Path, required=True)
    train.add_argument("--lane-sidecar", type=Path, required=True)
    train.add_argument("--loadout-id", required=True)
    train.add_argument("--seed-shard-index", type=int, required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--campaign", type=Path, required=True)
    freeze.add_argument("--candidate-root", type=Path, required=True)
    freeze.add_argument("--training-root", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)

    evaluation = subparsers.add_parser("eval")
    common_runtime(evaluation)
    evaluation.add_argument("--frozen", type=Path, required=True)
    evaluation.add_argument("--seed-shard-index", type=int, required=True)

    summary = subparsers.add_parser("summarize")
    summary.add_argument("--campaign", type=Path, required=True)
    summary.add_argument("--frozen", type=Path, required=True)
    summary.add_argument("--evaluation-root", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.phase == "candidate":
            payload = generate_candidate_manifest_v2(
                campaign_path=args.campaign,
                loadout_id=args.loadout_id,
                output_path=args.output,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
                offline_guide_artifact_path=args.offline_guide_artifact,
            )
        elif args.phase == "train":
            payload = run_training_shard_compact_v2(
                campaign_path=args.campaign,
                candidate_manifest_path=args.candidate_manifest,
                loadout_id=args.loadout_id,
                seed_shard_index=args.seed_shard_index,
                lane_sidecar_path=args.lane_sidecar,
                output_path=args.output,
                replay_workers=args.replay_workers,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
            )
        elif args.phase == "freeze":
            payload = freeze_training_winner_compact_v2(
                campaign_path=args.campaign,
                candidate_root=args.candidate_root,
                training_root=args.training_root,
                output_path=args.output,
            )
        elif args.phase == "eval":
            campaign_value = load_continuous_two_wave_remote_campaign_v1(
                args.campaign
            )
            payload = run_evaluation_shard_v1(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                seed_shard_index=args.seed_shard_index,
                output_path=args.output,
                replay_workers=args.replay_workers,
                runtime=build_default_replay_runtime_v2(
                    campaign_value,
                    bridge_path=args.bridge,
                    bridge_cwd=args.bridge_cwd,
                    runtime_binding_path=args.runtime_binding,
                ),
            )
        else:
            payload = summarize_evaluation_v1(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                evaluation_root=args.evaluation_root,
                output_path=args.output,
            )
    except Exception as error:
        _print_receipt_v2(
            {
                "schema": STDOUT_RECEIPT_SCHEMA_V2,
                "phase": args.phase,
                "status": "FAILED",
                "error_type": type(error).__name__,
                "reason": str(error)[:600],
            }
        )
        raise SystemExit(1)
    _print_receipt_v2(
        _stdout_receipt_v2(
            phase=args.phase, output_path=args.output, payload=payload
        )
    )


if __name__ == "__main__":
    main()


__all__ = (
    "STDOUT_RECEIPT_SCHEMA_V2",
    "build_default_replay_runtime_v2",
    "candidate_manifest_name_v2",
    "freeze_training_winner_compact_v2",
    "generate_candidate_manifest_v2",
    "run_training_shard_compact_v2",
    "train_lane_sidecar_name_v2",
)
