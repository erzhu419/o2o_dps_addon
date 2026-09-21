"""Remote worker phases for the V8 Cat action-plan residual campaign.

The teacher phase evaluates one proposal seed and loadout.  Candidate
materialization then consumes the complete 128-result proposal cohort for one
loadout, distils portable Cat-relative residual wires, and publishes the
ordinary compact-v2 candidate manifest used by training and freezing.  Train
and held-out evaluation rebuild every searched residual from that portable
bundle; the empty residual is therefore the same exact Cat arm in every
phase.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import CausalActionProgramV1
from .development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from .upper_kara_cat_action_plan_distiller_v8 import (
    distill_upper_kara_cat_action_plan_teacher_v8,
    load_upper_kara_cat_action_plan_distillation_v8,
)
from .upper_kara_cat_action_plan_teacher_v8 import (
    SCHEMA as TEACHER_SCHEMA,
    run_upper_kara_cat_action_plan_teacher_v8,
)
from .upper_kara_causal_program_remote_compact_v2 import (
    build_candidate_manifest_v2,
    write_candidate_manifest_v2,
)
from .upper_kara_causal_program_remote_contract_v1 import (
    CatActionPlanResidualSequenceSearchV8,
    TeacherPlanShardV8,
    assign_teacher_plan_shards_v8,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
)
from .upper_kara_causal_program_remote_worker_v1 import (
    DEFAULT_REPLAY_WORKERS,
    RemoteCausalProgramRuntimeV1,
    _atomic_create_json,
    _build_cases_v1,
    _campaign_contract_v1,
    _load_frozen_v1,
    _program_receipt_v1,
    _read_json,
    run_evaluation_shard_v1,
    summarize_evaluation_v1,
)
from .upper_kara_causal_program_remote_worker_v2 import (
    freeze_training_winner_compact_v2,
    run_training_shard_compact_v2,
)
from .upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from .upper_kara_two_wave_cat_residual_sequence_replay_v1 import (
    build_two_wave_cat_residual_sequence_program_replay_factory_v1,
)
from .upper_kara_two_wave_train_eval_v1 import imported_incumbent_programs_v1


JSONMap = dict[str, Any]
STDOUT_RECEIPT_SCHEMA_V8 = (
    "upper_kara_cat_action_plan_remote_worker_receipt/v8"
)
WAVES_V8 = (
    CatResidualSequenceWaveV1("multi_two", (0, 1)),
    CatResidualSequenceWaveV1("single_long", (2,)),
)


def _v8_spec(campaign: Any) -> CatActionPlanResidualSequenceSearchV8:
    spec = getattr(campaign, "search_spec", None)
    if not isinstance(spec, CatActionPlanResidualSequenceSearchV8):
        raise ValueError("campaign is not a Cat action-plan residual V8 campaign")
    return spec


def _safe_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    if Path(value).name != value or any(mark in value for mark in ("/", "\\")):
        raise ValueError(f"{label} cannot contain a path separator")
    return value


def teacher_terminal_name_v8(shard: TeacherPlanShardV8) -> str:
    if not isinstance(shard, TeacherPlanShardV8):
        raise TypeError("shard must be TeacherPlanShardV8")
    return f"{shard.work_id}.json"


def residual_policy_bundle_name_v8(loadout_id: str) -> str:
    return f"residual-bundle--{_safe_name(loadout_id, 'loadout_id')}.json"


def _teacher_shard_v8(
    campaign: Any, loadout_id: str, teacher_shard_index: int
) -> TeacherPlanShardV8:
    matches = tuple(
        row
        for row in assign_teacher_plan_shards_v8(campaign)
        if row.loadout_id == loadout_id
        and row.teacher_shard_index == teacher_shard_index
    )
    if len(matches) != 1:
        raise ValueError(
            "loadout_id/teacher_shard_index is not one V8 teacher task"
        )
    return matches[0]


def _forbidden_generator_v8(**_: Any) -> Sequence[CausalActionProgramV1]:
    raise AssertionError("candidate generation is forbidden during V8 replay")


def _forbidden_replay_factory_v8(*_: Any, **__: Any) -> Any:
    raise AssertionError("the V8 teacher case runtime cannot replay programs")


def _teacher_case_runtime_v8() -> RemoteCausalProgramRuntimeV1:
    return RemoteCausalProgramRuntimeV1(
        case_builder=build_upper_kara_heterogeneous_two_wave_burst_case_v7,
        searched_program_generator=_forbidden_generator_v8,
        program_replay_factory=_forbidden_replay_factory_v8,
    )


def run_teacher_plan_shard_v8(
    *,
    campaign_path: str | Path,
    loadout_id: str,
    teacher_shard_index: int,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    teacher_workers: int = 2,
    case_runtime: RemoteCausalProgramRuntimeV1 | None = None,
    teacher_runner: Callable[..., Mapping[str, Any]] = (
        run_upper_kara_cat_action_plan_teacher_v8
    ),
) -> JSONMap:
    """Run one proposal-only teacher task with the common derived sim seed."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    _v8_spec(campaign)
    if (
        isinstance(teacher_workers, bool)
        or not isinstance(teacher_workers, int)
        or teacher_workers < 1
    ):
        raise ValueError("teacher_workers must be a positive integer")
    shard = _teacher_shard_v8(
        campaign, loadout_id, teacher_shard_index
    )
    runtime = case_runtime or _teacher_case_runtime_v8()
    cases, _, simulator_by_master = _build_cases_v1(
        runtime, campaign, loadout_id, (shard.example,)
    )
    if len(cases) != 1:
        raise AssertionError("one V8 teacher task must materialize one case")
    simulator_seed = simulator_by_master[shard.example.seed]
    result = dict(
        teacher_runner(
            cases[0],
            build_id=campaign.build_id,
            loadout_id=loadout_id,
            simulator_seed=simulator_seed,
            max_states=shard.teacher_max_states,
            plan_start_index=0,
            max_plans_per_state=shard.teacher_plan_shard_size,
            max_decisions=campaign.max_decisions,
            branch_workers=teacher_workers,
            bridge_path=bridge_path,
            bridge_cwd=bridge_cwd,
            runtime_binding_path=runtime_binding_path,
        )
    )
    if (
        result.get("schema") != TEACHER_SCHEMA
        or result.get("master_seed") != shard.example.seed
        or result.get("simulator_seed") != simulator_seed
        or result.get("build_id") != campaign.build_id
        or result.get("loadout_id") != loadout_id
    ):
        raise ValueError("teacher result identity differs from its assigned shard")
    result["remote_teacher_task"] = {
        "campaign_id": campaign.campaign_id,
        "work_id": shard.work_id,
        "teacher_shard_index": shard.teacher_shard_index,
        "simulator_seed": simulator_seed,
        "first_wave_arrival_ms": shard.example.first_wave_arrival_ms,
        "proposal_cohort_only": True,
        "selection_or_heldout_seed_used": False,
    }
    _atomic_create_json(output_path, result)
    return result


def _validate_teacher_result_v8(
    result: Mapping[str, Any], campaign: Any, shard: TeacherPlanShardV8
) -> JSONMap:
    task = result.get("remote_teacher_task")
    if (
        result.get("schema") != TEACHER_SCHEMA
        or result.get("build_id") != campaign.build_id
        or result.get("loadout_id") != shard.loadout_id
        or result.get("master_seed") != shard.example.seed
        or isinstance(result.get("simulator_seed"), bool)
        or not isinstance(result.get("simulator_seed"), int)
        or not isinstance(task, Mapping)
        or task.get("campaign_id") != campaign.campaign_id
        or task.get("work_id") != shard.work_id
        or task.get("teacher_shard_index") != shard.teacher_shard_index
        or task.get("simulator_seed") != result.get("simulator_seed")
        or task.get("first_wave_arrival_ms")
        != shard.example.first_wave_arrival_ms
        or task.get("proposal_cohort_only") is not True
        or task.get("selection_or_heldout_seed_used") is not False
    ):
        raise ValueError(f"teacher result identity mismatch: {shard.work_id}")
    return dict(result)


def _load_all_teacher_results_v8(
    campaign: Any, loadout_id: str, teacher_root: str | Path
) -> tuple[JSONMap, ...]:
    root = Path(teacher_root).expanduser()
    shards = tuple(
        row
        for row in assign_teacher_plan_shards_v8(campaign)
        if row.loadout_id == loadout_id
    )
    proposal, selection = split_training_examples_for_selection_v1(campaign)
    if len(shards) != 128 or len(proposal) != 128 or len(selection) != 128:
        raise ValueError("V8 candidate phase requires the fixed 128/128 split")
    results = tuple(
        _validate_teacher_result_v8(
            _read_json(
                root / teacher_terminal_name_v8(shard),
                "V8 teacher terminal",
            ),
            campaign,
            shard,
        )
        for shard in shards
    )
    expected = {row.example.seed for row in shards}
    observed = {row["master_seed"] for row in results}
    if observed != expected or len(observed) != len(results):
        raise ValueError("V8 teacher terminal seed coverage is not exact")
    simulator_seeds = [row["simulator_seed"] for row in results]
    if len(simulator_seeds) != len(set(simulator_seeds)):
        raise ValueError("V8 teacher terminals repeat a derived simulator seed")
    return results


def _identity_only_cat_factory_v8() -> Any:
    raise AssertionError("identity-only residual construction opened Cat")


def _programs_from_policy_registry_v8(
    build_id: str,
    policies: Mapping[str, DevelopmentTwoWaveCatResidualSequenceV1],
    *,
    incumbent_program_factory: Callable[[str], Sequence[CausalActionProgramV1]],
) -> tuple[CausalActionProgramV1, ...]:
    incumbents = tuple(incumbent_program_factory(build_id))
    if len(incumbents) != 3:
        raise ValueError("V8 candidate manifest requires exactly three incumbents")
    residuals = tuple(
        build_two_wave_cat_residual_sequence_runtime_v1(
            policy, cat_resolver_factory=_identity_only_cat_factory_v8
        )[0]
        for policy in policies.values()
    )
    if not residuals or sum(not policy.steps for policy in policies.values()) != 1:
        raise ValueError("V8 residual registry lacks its unique exact-Cat zero")
    programs = (*incumbents, *residuals)
    if len({row.program_id for row in programs}) != len(programs):
        raise ValueError("V8 candidate program IDs repeat")
    if len({row.program_key() for row in programs}) != len(programs):
        raise ValueError("V8 candidate program semantics repeat")
    return programs


def generate_candidate_manifest_v8(
    *,
    campaign_path: str | Path,
    teacher_root: str | Path,
    loadout_id: str,
    policy_bundle_path: str | Path,
    output_path: str | Path,
    distiller: Callable[..., Mapping[str, Any]] = (
        distill_upper_kara_cat_action_plan_teacher_v8
    ),
    incumbent_program_factory: Callable[
        [str], Sequence[CausalActionProgramV1]
    ] = imported_incumbent_programs_v1,
) -> JSONMap:
    """Distil all 128 teacher results and publish bundle then manifest."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    spec = _v8_spec(campaign)
    if loadout_id not in campaign.loadout_ids:
        raise ValueError("loadout_id is not in the V8 campaign")
    results = _load_all_teacher_results_v8(
        campaign, loadout_id, teacher_root
    )
    proposal, _ = split_training_examples_for_selection_v1(campaign)
    # The contract cap includes exact Cat.  The distiller's parameter counts
    # only nonzero policies, leaving one slot for the required empty residual.
    max_nonzero = spec.max_distilled_candidates_per_loadout - 1
    bundle = dict(
        distiller(
            results,
            proposal_seeds=(row.seed for row in proposal),
            exact_build_id=campaign.build_id,
            loadout_id=loadout_id,
            waves=WAVES_V8,
            policy_id_prefix=(
                f"v8::{campaign.campaign_id}::{loadout_id}"
            ),
            max_nonzero_candidates=max_nonzero,
        )
    )
    bundle["campaign_id"] = campaign.campaign_id
    bundle["campaign_contract"] = _campaign_contract_v1(campaign)
    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    if (
        bundle.get("campaign_id") != campaign.campaign_id
        or bundle.get("campaign_contract") != _campaign_contract_v1(campaign)
        or bundle.get("loadout_id") != loadout_id
        or bundle.get("exact_build_id") != campaign.build_id
        or len(policies) > spec.max_distilled_candidates_per_loadout
    ):
        raise ValueError("distilled policy bundle differs from the V8 campaign")
    programs = _programs_from_policy_registry_v8(
        campaign.build_id,
        policies,
        incumbent_program_factory=incumbent_program_factory,
    )
    manifest = build_candidate_manifest_v2(
        campaign_id=campaign.campaign_id,
        build_id=campaign.build_id,
        campaign_contract=_campaign_contract_v1(campaign),
        loadout_id=loadout_id,
        proposal_guide_ids=(),
        programs=tuple(_program_receipt_v1(row) for row in programs),
    )
    # The manifest is the phase commit marker.  A consumer never treats an
    # unaccompanied bundle as a completed candidate phase.
    _atomic_create_json(policy_bundle_path, bundle)
    write_candidate_manifest_v2(output_path, manifest)
    return manifest


def _load_policy_bundle_v8(
    path: str | Path,
    *,
    campaign: Any,
    loadout_id: str,
) -> tuple[JSONMap, dict[str, DevelopmentTwoWaveCatResidualSequenceV1]]:
    bundle = _read_json(path, "V8 residual policy bundle")
    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    spec = _v8_spec(campaign)
    if (
        bundle.get("campaign_id") != campaign.campaign_id
        or bundle.get("campaign_contract") != _campaign_contract_v1(campaign)
        or bundle.get("loadout_id") != loadout_id
        or bundle.get("exact_build_id") != campaign.build_id
        or len(policies) > spec.max_distilled_candidates_per_loadout
    ):
        raise ValueError("V8 residual bundle does not match campaign/loadout")
    return bundle, policies


def build_v8_replay_runtime(
    campaign: Any,
    policies: Mapping[str, DevelopmentTwoWaveCatResidualSequenceV1],
    *,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    incumbent_program_factory: Callable[
        [str], Sequence[CausalActionProgramV1]
    ] = imported_incumbent_programs_v1,
) -> RemoteCausalProgramRuntimeV1:
    _v8_spec(campaign)
    replay_factory = (
        build_two_wave_cat_residual_sequence_program_replay_factory_v1(
            campaign.build_id,
            policies,
            bridge_path=bridge_path,
            bridge_cwd=bridge_cwd,
            runtime_binding_path=runtime_binding_path,
        )
    )
    return RemoteCausalProgramRuntimeV1(
        case_builder=build_upper_kara_heterogeneous_two_wave_burst_case_v7,
        searched_program_generator=_forbidden_generator_v8,
        program_replay_factory=replay_factory,
        incumbent_program_factory=incumbent_program_factory,
    )


def run_training_shard_v8(
    *,
    campaign_path: str | Path,
    candidate_manifest_path: str | Path,
    policy_bundle_path: str | Path,
    loadout_id: str,
    seed_shard_index: int,
    lane_sidecar_path: str | Path,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
    compact_runner: Callable[..., JSONMap] = run_training_shard_compact_v2,
) -> JSONMap:
    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    _, policies = _load_policy_bundle_v8(
        policy_bundle_path, campaign=campaign, loadout_id=loadout_id
    )
    resolved_runtime = runtime or build_v8_replay_runtime(
        campaign,
        policies,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )
    return compact_runner(
        campaign_path=campaign_path,
        candidate_manifest_path=candidate_manifest_path,
        loadout_id=loadout_id,
        seed_shard_index=seed_shard_index,
        lane_sidecar_path=lane_sidecar_path,
        output_path=output_path,
        replay_workers=replay_workers,
        runtime=resolved_runtime,
    )


def run_evaluation_shard_v8(
    *,
    campaign_path: str | Path,
    frozen_path: str | Path,
    policy_bundle_root: str | Path,
    seed_shard_index: int,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    runtime: RemoteCausalProgramRuntimeV1 | None = None,
    evaluation_runner: Callable[..., JSONMap] = run_evaluation_shard_v1,
) -> JSONMap:
    """Evaluate the frozen residual and exactly three imported incumbents."""

    campaign = load_continuous_two_wave_remote_campaign_v1(campaign_path)
    _v8_spec(campaign)
    frozen, frozen_program = _load_frozen_v1(frozen_path, campaign)
    loadout_id = frozen["winner"]["loadout_id"]
    bundle_path = (
        Path(policy_bundle_root).expanduser()
        / residual_policy_bundle_name_v8(loadout_id)
    )
    _, policies = _load_policy_bundle_v8(
        bundle_path, campaign=campaign, loadout_id=loadout_id
    )
    try:
        frozen_policy = policies[frozen_program.program_id]
    except KeyError as error:
        raise ValueError("frozen residual is absent from its loadout bundle") from error
    rebuilt = build_two_wave_cat_residual_sequence_runtime_v1(
        frozen_policy, cat_resolver_factory=_identity_only_cat_factory_v8
    )[0]
    if rebuilt.program_key() != frozen_program.program_key():
        raise ValueError("frozen residual differs from its portable bundle")
    resolved_runtime = runtime or build_v8_replay_runtime(
        campaign,
        policies,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )
    return evaluation_runner(
        campaign_path=campaign_path,
        frozen_path=frozen_path,
        seed_shard_index=seed_shard_index,
        output_path=output_path,
        replay_workers=replay_workers,
        runtime=resolved_runtime,
    )


def _print_receipt_v8(
    *, phase: str, output_path: str | Path, payload: Mapping[str, Any]
) -> None:
    terminal_status = payload.get("terminal_status")
    receipt = {
        "schema": STDOUT_RECEIPT_SCHEMA_V8,
        "phase": phase,
        "status": (
            terminal_status
            if terminal_status in {"COMPLETE", "INVALID", "FAILED"}
            else "FAILED" if payload.get("status") == "FAILED" else "COMPLETE"
        ),
        "output": str(output_path),
    }
    if receipt["status"] == "FAILED":
        receipt["error_type"] = payload.get("error_type")
        receipt["reason"] = payload.get("reason")
    wire = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    if len(wire.encode("utf-8")) >= 1_024:
        raise AssertionError("V8 worker stdout receipt exceeds 1 KiB")
    print(wire)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)

    def campaign_output(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--campaign", type=Path, required=True)
        subparser.add_argument("--output", type=Path, required=True)

    def native(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--bridge", type=Path, required=True)
        subparser.add_argument("--bridge-cwd", type=Path, required=True)
        subparser.add_argument("--runtime-binding", type=Path, required=True)

    teacher = subparsers.add_parser("teacher")
    campaign_output(teacher)
    native(teacher)
    teacher.add_argument("--loadout-id", required=True)
    teacher.add_argument("--teacher-shard-index", type=int, required=True)
    teacher.add_argument("--teacher-workers", type=int, default=2)

    candidate = subparsers.add_parser("candidate")
    campaign_output(candidate)
    candidate.add_argument("--teacher-root", type=Path, required=True)
    candidate.add_argument("--loadout-id", required=True)
    candidate.add_argument("--policy-bundle", type=Path, required=True)

    train = subparsers.add_parser("train")
    campaign_output(train)
    native(train)
    train.add_argument("--candidate-manifest", type=Path, required=True)
    train.add_argument("--policy-bundle", type=Path, required=True)
    train.add_argument("--loadout-id", required=True)
    train.add_argument("--seed-shard-index", type=int, required=True)
    train.add_argument("--lane-sidecar", type=Path, required=True)
    train.add_argument("--replay-workers", type=int, default=DEFAULT_REPLAY_WORKERS)

    freeze = subparsers.add_parser("freeze")
    campaign_output(freeze)
    freeze.add_argument("--candidate-root", type=Path, required=True)
    freeze.add_argument("--training-root", type=Path, required=True)

    evaluation = subparsers.add_parser("eval")
    campaign_output(evaluation)
    native(evaluation)
    evaluation.add_argument("--frozen", type=Path, required=True)
    evaluation.add_argument("--policy-bundle-root", type=Path, required=True)
    evaluation.add_argument("--seed-shard-index", type=int, required=True)
    evaluation.add_argument(
        "--replay-workers", type=int, default=DEFAULT_REPLAY_WORKERS
    )

    summary = subparsers.add_parser("summarize")
    campaign_output(summary)
    summary.add_argument("--frozen", type=Path, required=True)
    summary.add_argument("--evaluation-root", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.phase == "teacher":
            payload = run_teacher_plan_shard_v8(
                campaign_path=args.campaign,
                loadout_id=args.loadout_id,
                teacher_shard_index=args.teacher_shard_index,
                output_path=args.output,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
                teacher_workers=args.teacher_workers,
            )
        elif args.phase == "candidate":
            payload = generate_candidate_manifest_v8(
                campaign_path=args.campaign,
                teacher_root=args.teacher_root,
                loadout_id=args.loadout_id,
                policy_bundle_path=args.policy_bundle,
                output_path=args.output,
            )
        elif args.phase == "train":
            payload = run_training_shard_v8(
                campaign_path=args.campaign,
                candidate_manifest_path=args.candidate_manifest,
                policy_bundle_path=args.policy_bundle,
                loadout_id=args.loadout_id,
                seed_shard_index=args.seed_shard_index,
                lane_sidecar_path=args.lane_sidecar,
                output_path=args.output,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
                replay_workers=args.replay_workers,
            )
        elif args.phase == "freeze":
            payload = freeze_training_winner_compact_v2(
                campaign_path=args.campaign,
                candidate_root=args.candidate_root,
                training_root=args.training_root,
                output_path=args.output,
            )
        elif args.phase == "eval":
            payload = run_evaluation_shard_v8(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                policy_bundle_root=args.policy_bundle_root,
                seed_shard_index=args.seed_shard_index,
                output_path=args.output,
                bridge_path=args.bridge,
                bridge_cwd=args.bridge_cwd,
                runtime_binding_path=args.runtime_binding,
                replay_workers=args.replay_workers,
            )
        else:
            payload = summarize_evaluation_v1(
                campaign_path=args.campaign,
                frozen_path=args.frozen,
                evaluation_root=args.evaluation_root,
                output_path=args.output,
            )
    except Exception as error:
        _print_receipt_v8(
            phase=args.phase,
            output_path=args.output,
            payload={
                "status": "FAILED",
                "error_type": type(error).__name__,
                "reason": str(error)[:600],
            },
        )
        raise SystemExit(1)
    _print_receipt_v8(
        phase=args.phase, output_path=args.output, payload=payload
    )


if __name__ == "__main__":
    main()


__all__ = (
    "STDOUT_RECEIPT_SCHEMA_V8",
    "WAVES_V8",
    "build_v8_replay_runtime",
    "generate_candidate_manifest_v8",
    "residual_policy_bundle_name_v8",
    "run_evaluation_shard_v8",
    "run_teacher_plan_shard_v8",
    "run_training_shard_v8",
    "teacher_terminal_name_v8",
)
