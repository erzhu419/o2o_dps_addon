"""Run one bounded native pilot of the independent wave schedule search.

This entry point is intentionally small: it proves the exact-build cell,
dynamic action grammar, fresh-seed replay, and auditable operation receipts
before a search plan is split across the six CPU nodes.  Cat, both Contra
sources, and the pooled Chronicle prior are proposal-order guides by default;
none can remove a legal native action or become a candidate fallback.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_guard_v1 import ObservableCausalGuardV1
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    build_development_wave_case_v1,
)
from .development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
    build_development_burst_precombat_case_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING
from .deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from .fury_chronicle_prior import DEFAULT_MODEL as DEFAULT_OFFLINE_GUIDE
from .offline_action_sequence_guide_v1 import (
    SOURCE_BUILD_POOLED,
    OfflineGuideContextV1,
    load_offline_action_sequence_guide_v1,
    offline_action_key_for_ref_v1,
)
from .sim_bridge import ActionRef
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .wave_action_guides_v1 import (
    OfflineActionSequenceSearchGuideV1,
    action_guide_audit_payload_v1,
)
from .wave_action_schedule_v1 import ScheduledActionPlan, SearchCellIdentity
from .wave_expert_action_guides_v1 import (
    cat_wave_action_guide_v1,
    contra260817_wave_action_guide_v1,
    deployed_contra_wave_action_guide_v1,
    make_contextual_fury_state_factory_v1,
)
from .wave_action_sequence_search_v1 import (
    ActionGuideV1,
    NativeDynamicV3ScheduleReplayV1,
    ScheduleReplayOutcomeV1,
    search_wave_action_sequences_v1,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = (
    PROJECT_ROOT
    / "bin/o2obridge.seedfix-v22.precombat-late-arrival.cooldownmeta.rapidgrowth.withdb.goamd64v1.windows-amd64.exe"
)
DEFAULT_BRIDGE_CWD = PROJECT_ROOT.parent / "wowsims-turtle"
EXPERT_GUIDE_NAMES = ("cat", "deployed_contra", "contra260817", "offline")
DEFAULT_EXPERT_GUIDES = EXPERT_GUIDE_NAMES
OFFLINE_CONTEXT_CONTRACT = {
    "history_source": "ACCEPTED_ACTION_RECEIPTS_FROM_COMPLETED_PREFIX_STEPS",
    "included_operation_kinds": ["ACT_OFF_GCD", "ACT_GCD"],
    "next_swing_queue_request_used_as_successful_action": False,
    "policy_observable_only": True,
    "future_schedule_suffix_used": False,
    "future_environment_events_used": False,
    "last_auto_attack_elapsed_bucket": "MISSING",
}


def _player(case: Any) -> Mapping[str, Any]:
    return case.request["raid"]["parties"][0]["players"][0]


def exact_build_identity_v1(case: Any) -> JSONMap:
    """Return the full configured build identity used by comparison gates."""

    player = _player(case)
    raid = case.request["raid"]
    party = raid["parties"][0]
    return {
        "class": player.get("class"),
        "race": player.get("race"),
        "talents_string": player.get("talentsString"),
        "equipment_items": deepcopy(
            player.get("equipment", {}).get("items", [])
        ),
        "consumes": deepcopy(player.get("consumes")),
        "individual_buffs": deepcopy(player.get("buffs")),
        "party_buffs": deepcopy(party.get("buffs")),
        "raid_buffs": deepcopy(raid.get("buffs")),
        "raid_debuffs": deepcopy(raid.get("debuffs")),
        "warrior_options": deepcopy(
            player.get("warrior", {}).get("options", {})
        ),
        "reaction_time_ms": player.get("reactionTimeMs"),
        "distance_from_target": player.get("distanceFromTarget"),
    }


def search_cell_from_case_v1(case: Any) -> SearchCellIdentity:
    player = _player(case)
    talents = str(player.get("talentsString", ""))
    talent_trees = tuple(
        (f"tree_{index}", int(value or "0"))
        for index, value in enumerate(talents.split("-"), start=1)
    )


    equipment = tuple(
        (f"slot_{index:02d}", int(row["id"]))
        for index, row in enumerate(
            player.get("equipment", {}).get("items", [])
        )
        if isinstance(row, Mapping) and int(row.get("id", 0)) > 0
    )
    initial = case.case_spec["initial_state"]
    exact_build = exact_build_identity_v1(case)
    return SearchCellIdentity(
        scenario_id=str(case.case_spec["source_instance_slug"]),
        wave_or_boss_id=str(case.case_spec["source_wave_ref"]),
        exact_build_id=str(case.case_spec["build_ref"]),
        talents=talent_trees,
        equipment=equipment,
        derived_mechanics=(
            ("starting_rage", int(initial["rage"])),
            ("target_base_armor", int(initial["target_base_armor"])),
            ("target_current_hp", int(initial["target_current_hp"])),
            ("target_max_hp", int(initial["target_max_hp"])),
            ("target_count", len(case.case_spec["required_target_indices"])),
            (
                "exact_build_context_json",
                json.dumps(
                    exact_build,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        ),
        environment_branch_id=str(case.case_spec["team_background"]["branch"]),
    )


def _normalize_expert_guides(
    values: Sequence[str],
) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in EXPERT_GUIDE_NAMES:
            raise ValueError(
                f"unknown expert guide {value!r}; expected one of "
                f"{', '.join(EXPERT_GUIDE_NAMES)}"
            )
        if value in result:
            raise ValueError(f"duplicate expert guide {value!r}")
        result.append(value)
    return tuple(result)


def completed_prefix_chronicle_action_history_v1(
    outcome: ScheduleReplayOutcomeV1,
    prefix: Sequence[ScheduledActionPlan],
) -> tuple[str, ...]:
    """Project only already accepted prefix actions into Chronicle tokens.

    The current/future simulator state is deliberately ignored.  In particular,
    a queued next-swing request is not treated as a successful Heroic Strike or
    Cleave observation merely because the client accepted the queue request.
    """

    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("outcome must be ScheduleReplayOutcomeV1")
    if any(not isinstance(step, ScheduledActionPlan) for step in prefix):
        raise TypeError("prefix must contain ScheduledActionPlan values")
    completed_step_count = len(prefix)
    history: list[str] = []
    for receipt in outcome.receipts:
        if not isinstance(receipt, Mapping):
            raise TypeError("outcome receipts must be mappings")
        if receipt.get("kind") not in {"ACT_OFF_GCD", "ACT_GCD"}:
            continue
        if receipt.get("accepted") is not True:
            continue
        step_index = receipt.get("step_index")
        if type(step_index) is not int:
            raise ValueError("accepted action receipt lacks integer step_index")
        if step_index < 0 or step_index >= completed_step_count:
            continue
        action_wire = receipt.get("action")
        if not isinstance(action_wire, Mapping):
            raise ValueError("accepted action receipt lacks action identity")
        action_key = offline_action_key_for_ref_v1(
            ActionRef.from_wire(action_wire)
        )
        if action_key is not None:
            history.append(action_key)
    return tuple(history)


def make_chronicle_offline_context_factory_v1(case: Any):
    """Bind pooled Chronicle history to one evaluation build receipt."""

    evaluation_build_identity = exact_build_identity_v1(case)

    def context_factory(
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> OfflineGuideContextV1:
        return OfflineGuideContextV1(
            recent_successful_action_history=(
                completed_prefix_chronicle_action_history_v1(outcome, prefix)
            ),
            last_auto_attack_elapsed_bucket="MISSING",
            evaluation_build_identity=evaluation_build_identity,
        )

    return context_factory


def build_expert_action_guides_v1(
    case: Any,
    *,
    guide_names: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding_path: Path = DEFAULT_BINDING,
    offline_guide_artifact_path: Path = DEFAULT_OFFLINE_GUIDE,
) -> tuple[ActionGuideV1, ...]:
    """Bind selected source adapters to the pilot's exact-case observation."""

    normalized = _normalize_expert_guides(guide_names)
    state_factory = make_contextual_fury_state_factory_v1(
        case.request,
        case.target_contexts,
    )
    runtime_binding = (
        load_deployed_contra_runtime_binding_v1(runtime_binding_path)
        if "deployed_contra" in normalized
        else None
    )
    guides: list[ActionGuideV1] = []
    for name in normalized:
        if name == "cat":
            guide = cat_wave_action_guide_v1(state_factory)
        elif name == "deployed_contra":
            # The loader validates that this is the captured deployed runtime
            # binding.  A readable source substitute is not accepted here.
            guide = deployed_contra_wave_action_guide_v1(
                state_factory,
                runtime_binding=runtime_binding,
            )
        elif name == "contra260817":
            guide = contra260817_wave_action_guide_v1(state_factory)
        else:
            offline = load_offline_action_sequence_guide_v1(
                offline_guide_artifact_path,
                source_build_scope=SOURCE_BUILD_POOLED,
            )
            guide = OfflineActionSequenceSearchGuideV1(
                "offline_chronicle:pooled_partial_server_observations_v1",
                offline,
                make_chronicle_offline_context_factory_v1(case),
                context_contract=OFFLINE_CONTEXT_CONTRACT,
            )
        guides.append(guide)
    return tuple(guides)


def run_wave_action_sequence_pilot_v1(
    *,
    seeds: Sequence[int],
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    max_steps: int = 2,
    beam_width: int = 4,
    max_expansions_per_node: int | None = 12,
    max_off_gcd_actions: int = 0,
    max_prefix_permutations: int | None = 1,
    replay_workers: int = 1,
    continuation_max_steps: int = 0,
    precombat_burst: bool = False,
    precombat_ms: int = 3_000,
    expert_guide_names: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding_path: Path = DEFAULT_BINDING,
    offline_guide_artifact_path: Path = DEFAULT_OFFLINE_GUIDE,
    guard_options: Sequence[ObservableCausalGuardV1] = (),
    case_factory: Callable[[int], Any] | None = None,
    search_cell_factory: Callable[[Any], SearchCellIdentity] = search_cell_from_case_v1,
) -> JSONMap:
    normalized_seeds = tuple(seeds)
    if not normalized_seeds:
        raise ValueError("seeds must not be empty")
    if not isinstance(precombat_burst, bool):
        raise TypeError("precombat_burst must be boolean")
    if type(precombat_ms) is not int or precombat_ms <= 0:
        raise ValueError("precombat_ms must be a positive integer")
    if case_factory is not None and precombat_burst:
        raise ValueError(
            "an explicit case_factory cannot be combined with precombat_burst"
        )
    if case_factory is not None and not callable(case_factory):
        raise TypeError("case_factory must be callable or None")
    if not callable(search_cell_factory):
        raise TypeError("search_cell_factory must be callable")
    if any(
        not isinstance(guard, ObservableCausalGuardV1)
        for guard in guard_options
    ):
        raise TypeError(
            "guard_options must contain ObservableCausalGuardV1 values"
        )
    normalized_guards = tuple(guard_options)
    selected_guides = _normalize_expert_guides(expert_guide_names)
    explicit_case_factory = case_factory
    resolved_case_factory = (
        explicit_case_factory
        if explicit_case_factory is not None
        else (
            (
                lambda seed: build_development_burst_precombat_case_v1(
                    seed,
                    pull_time_ms=precombat_ms,
                )
            )
            if precombat_burst
            else build_development_wave_case_v1
        )
    )
    first_case = resolved_case_factory(normalized_seeds[0])
    is_precombat = isinstance(first_case, DevelopmentPrecombatWaveCaseV1)
    cell = search_cell_factory(first_case)
    if not isinstance(cell, SearchCellIdentity):
        raise TypeError("search_cell_factory must return SearchCellIdentity")
    guides = build_expert_action_guides_v1(
        first_case,
        guide_names=selected_guides,
        runtime_binding_path=runtime_binding_path,
        offline_guide_artifact_path=offline_guide_artifact_path,
    )
    replay = NativeDynamicV3ScheduleReplayV1(
        lambda: (
            SimulatorBridgePrecombatV1(bridge_path, cwd=bridge_cwd)
            if is_precombat
            else SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd)
        ),
        resolved_case_factory,
    )
    result = search_wave_action_sequences_v1(
        replay,
        cell,
        seeds=normalized_seeds,
        max_steps=max_steps,
        beam_width=beam_width,
        max_off_gcd_actions=max_off_gcd_actions,
        max_prefix_permutations=max_prefix_permutations,
        max_expansions_per_node=max_expansions_per_node,
        replay_workers=replay_workers,
        action_guides=guides,
        continuation_max_steps=continuation_max_steps,
        guard_options=normalized_guards,
    )
    return {
        "schema": "wave_action_sequence_native_pilot/v1",
        "scope": (
            "MODEL_DEFINED_DEVELOPMENT_ONLY"
            if explicit_case_factory is None
            else "EXPLICIT_CALLER_BOUND_EXACT_CELL"
        ),
        "candidate": "INDEPENDENT_FINITE_SCENARIO_ACTION_SCHEDULE",
        "candidate_is_cat_or_contra_residual": False,
        "bridge": str(bridge_path.resolve()),
        "bridge_cwd": str(bridge_cwd.resolve()),
        "seeds": list(normalized_seeds),
        "timeline": {
            "precombat_burst_enabled": is_precombat,
            "pull_time_ms": (
                first_case.timeline.pull_time_ms if is_precombat else 0
            ),
            "schedule_time_origin": (
                "PRECOMBAT_WINDOW_START" if is_precombat else "PULL"
            ),
        },
        "guard_options": [guard.to_dict() for guard in normalized_guards],
        "exact_build_identity": exact_build_identity_v1(first_case),
        "expert_guides": {
            "selected_names": list(selected_guides),
            "resolved_guide_ids": [guide.guide_id for guide in guides],
            "runtime_binding": (
                str(runtime_binding_path.resolve())
                if "deployed_contra" in selected_guides
                else None
            ),
            "offline_chronicle": (
                {
                    "artifact": str(offline_guide_artifact_path.resolve()),
                    "source_build_scope": SOURCE_BUILD_POOLED,
                    "role": "GUIDE_ONLY_NOT_SAME_EQUIPMENT_BASELINE",
                    "context_contract": deepcopy(OFFLINE_CONTEXT_CONTRACT),
                }
                if "offline" in selected_guides
                else None
            ),
            "role": "PROPOSAL_ORDER_ONLY_NOT_ACTION_MEMBERSHIP_OR_RUNTIME_FALLBACK",
            "all_legal_simulator_actions_remain_searchable": True,
            "audit": list(action_guide_audit_payload_v1(guides)),
        },
        "search": result.to_dict(),
        "operation_receipts": [
            {"seed": outcome.seed, "receipts": list(outcome.receipts)}
            for outcome in result.outcomes
        ],
        "claim_boundary": {
            "native_execution_wiring_proven": True,
            "superiority_over_cat_contra_or_offline_proven": False,
            "formal_heavy_search_completed": False,
        },
    }


def _parse_seeds(value: str) -> tuple[int, ...]:
    try:
        seeds = tuple(int(row.strip()) for row in value.split(",") if row.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from error
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return seeds


def _parse_expert_guides(value: str) -> tuple[str, ...]:
    normalized = value.strip().lower()
    if normalized == "all":
        return DEFAULT_EXPERT_GUIDES
    if normalized == "none":
        return ()
    values = tuple(row.strip() for row in normalized.split(",") if row.strip())
    if not values:
        raise argparse.ArgumentTypeError(
            "expert guides must be all, none, or a comma-separated guide list"
        )
    try:
        return _normalize_expert_guides(values)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=_parse_seeds, default=(2026091417,))
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=DEFAULT_BRIDGE_CWD)
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument("--beam-width", type=int, default=4)
    parser.add_argument("--max-expansions-per-node", type=int, default=12)
    parser.add_argument("--max-off-gcd-actions", type=int, default=0)
    parser.add_argument("--max-prefix-permutations", type=int, default=1)
    parser.add_argument("--replay-workers", type=int, default=1)
    parser.add_argument(
        "--continuation-max-steps",
        type=int,
        default=0,
        help=(
            "guide-completion steps used only to rank partial beam prefixes; "
            "heavy search should use a value long enough to finish the wave"
        ),
    )
    parser.add_argument(
        "--precombat-burst",
        action="store_true",
        help="search the declared potion/Death Wish/Recklessness window before pull",
    )
    parser.add_argument("--precombat-ms", type=int, default=3_000)
    parser.add_argument(
        "--expert-guides",
        type=_parse_expert_guides,
        default=DEFAULT_EXPERT_GUIDES,
        help=(
            "proposal-order guides: all (default), none (isolated smoke), or "
            "comma-separated cat,deployed_contra,contra260817,offline"
        ),
    )
    parser.add_argument(
        "--runtime-binding",
        type=Path,
        default=DEFAULT_BINDING,
        help="validated deployed-Contra runtime binding used when selected",
    )
    parser.add_argument(
        "--offline-guide-artifact",
        type=Path,
        default=DEFAULT_OFFLINE_GUIDE,
        help="pooled Chronicle prior used only for proposal ordering",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run_wave_action_sequence_pilot_v1(
        seeds=args.seeds,
        bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd,
        max_steps=args.max_steps,
        beam_width=args.beam_width,
        max_expansions_per_node=args.max_expansions_per_node,
        max_off_gcd_actions=args.max_off_gcd_actions,
        max_prefix_permutations=args.max_prefix_permutations,
        replay_workers=args.replay_workers,
        continuation_max_steps=args.continuation_max_steps,
        precombat_burst=args.precombat_burst,
        precombat_ms=args.precombat_ms,
        expert_guide_names=args.expert_guides,
        runtime_binding_path=args.runtime_binding,
        offline_guide_artifact_path=args.offline_guide_artifact,
    )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["search"]["status"],
                "evaluated_schedule_count": payload["search"][
                    "evaluated_schedule_count"
                ],
                "completed_depth": payload["search"]["completed_depth"],
                "mean_dps": payload["search"]["mean_dps"],
                "guide_ids": payload["expert_guides"]["resolved_guide_ids"],
                "output": str(args.output.resolve()) if args.output else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
