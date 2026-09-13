"""Build and evaluate a bounded Fury expert-guided first-decision union.

The runner combines source-derived Cat/Contra proposals, an explicitly curated
Cat2 candidate, the Chronicle partial behavior prior, and a small exploration
set.  Every candidate is masked by o2obridge and evaluated at a fixed simulator
 horizon over paired seeds.  This is a candidate-evaluation pipeline artifact,
 not a state/action teacher set, exact expert rollout, or deployable policy.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

from .beam_search import FactorizedDecision
from .cat2_saved_profile_adapter_v1 import Cat2SavedProfileSourceAdapterV1
from .expert_policy import StanceOp
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS, project_expert_decision
from .fury_chronicle_prior import FuryChroniclePrior
from .fury_expert_adapters import (
    Cat2ProfileAdapter,
    CatFurySourceAdapter,
    ContraDeployedSourceAdapter,
    ContraNewCandidateAdapter,
    CuratedCat2Card,
    FuryExpertState,
    WeaponMode,
)
from .fury_prefix_benchmark import PrefixCandidate, benchmark_prefix_candidates
from .sim_bridge import ActionRef, AvailableAction, SimBridgeError, SimulatorBridge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUEST = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
DEFAULT_PROFILE_METADATA = (
    PROJECT_ROOT
    / "configs"
    / "wowsims"
    / "fury_warrior_clean_dual.metadata.json"
)
DEFAULT_EXPERT_MANIFEST = (
    PROJECT_ROOT / "configs" / "experts" / "fury_experts_v1.json"
)
DEFAULT_CAT2_PROFILE_SNAPSHOT = (
    PROJECT_ROOT / "offline_data" / "reports" / "cat2_saved_profile_v1.json"
)
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_expert_guided_search_v1.json"
)
DEFAULT_EVALUATION_ROWS = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_expert_guided_search_v1.trials.jsonl"
)
DEFAULT_SEEDS = tuple(range(2026090101, 2026090133))

ROTATION_GCD_KEYS = (
    "warrior.bloodthirst",
    "warrior.whirlwind",
    "warrior.slam",
)

# An absent spell has no cooldown clock.  Keep its readiness strictly beyond
# every supported fight while leaving the separate `bloodthirst_known` bit
# authoritative; unlike infinity, this can be retained in native JSON traces.
UNAVAILABLE_COOLDOWN_S = 1_000_000_000.0


class ExpertGuidedSearchError(RuntimeError):
    """The bounded proposal/benchmark pipeline could not complete."""


def fury_state_from_simulator(
    state: Mapping[str, Any],
    available_actions: Iterable[AvailableAction],
    raid_sim_request: Mapping[str, Any],
) -> FuryExpertState:
    """Project a bridge root state into fields used by the bounded adapters."""

    actions = {available.action: available for available in available_actions}
    player = _player(raid_sim_request)
    encounter = raid_sim_request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise ExpertGuidedSearchError("RaidSimRequest lacks encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ExpertGuidedSearchError("RaidSimRequest lacks encounter targets")

    power = state.get("power")
    if not isinstance(power, Mapping):
        raise ExpertGuidedSearchError("bridge state lacks power")
    simulator_rage = _number(power.get("current"), "power.current")
    # Addon policies read UnitMana("player"), whose Vanilla Warrior value is
    # the integer floor of the underlying rage-in-tenths field. Wowsims keeps
    # fractional rage internally, so passing it through directly makes source
    # checks such as Cat's ``rage > 29`` request a 30-rage Bloodthirst at 29.x.
    rage = float(math.floor(simulator_rage))
    mh_remaining_ms = _optional_number(state.get("mh_swing_remaining_ms"))
    mh_duration_ms = _optional_number(state.get("mh_swing_duration_ms"))
    oh_remaining_ms = _optional_number(state.get("oh_swing_remaining_ms"))
    if mh_remaining_ms is None or mh_duration_ms is None:
        raise ExpertGuidedSearchError("bridge state lacks main-hand swing timing")

    aura_rows = state.get("auras")
    if not isinstance(aura_rows, list):
        aura_rows = []
    active_auras = {
        str(row.get("label", ""))
        for row in aura_rows
        if isinstance(row, Mapping)
        and (
            _optional_number(row.get("remaining_ms"), default=-1.0) >= 0
            or _optional_number(row.get("stacks"), default=0.0) > 0
        )
    }
    all_aura_labels = {
        str(row.get("label", "")) for row in aura_rows if isinstance(row, Mapping)
    }
    current_stance = StanceOp.BERSERKER
    if "Battle Stance" in all_aura_labels:
        current_stance = StanceOp.BATTLE
    elif "Defensive Stance" in all_aura_labels:
        current_stance = StanceOp.DEFENSIVE

    distance = _number(player.get("distanceFromTarget", 5), "distanceFromTarget")
    execute_phase = bool(state.get("execute_phase_20"))
    target = targets[0]
    target_level = target.get("level") if isinstance(target, Mapping) else None
    target_level_value = (
        int(target_level)
        if isinstance(target_level, (int, float)) and not isinstance(target_level, bool)
        else None
    )
    target_is_boss = target_level_value is not None and target_level_value >= 63
    bt_ready = _ready_seconds(actions, ACTION_KEY_TO_REF["warrior.bloodthirst"])
    ww_ready = _ready_seconds(actions, ACTION_KEY_TO_REF["warrior.whirlwind"])

    return FuryExpertState(
        rage=rage,
        target_health_pct=19.0 if execute_phase else 100.0,
        weapon_mode=(
            WeaponMode.DUAL_WIELD
            if oh_remaining_ms is not None
            else WeaponMode.TWO_HAND
        ),
        target_exists=True,
        target_is_boss=target_is_boss,
        target_level=target_level_value,
        in_combat=True,
        in_melee_range=distance < 8.0,
        target_distance_yards=distance,
        nearby_enemies=len(targets),
        gcd_ready=_number(state.get("gcd_remaining_ms", 0), "gcd_remaining_ms") <= 0,
        current_stance=current_stance,
        has_battle_shout=any("Battle Shout" in label for label in active_auras),
        battle_shout_remaining_s=_aura_remaining_seconds(aura_rows, "Battle Shout"),
        flurry_talent=True,
        flurry_active=any(
            label.startswith("Flurry") and "Trigger" not in label
            for label in active_auras
        ),
        bloodthirst_known=ACTION_KEY_TO_REF["warrior.bloodthirst"] in actions,
        bloodthirst_ready_in_s=bt_ready,
        whirlwind_ready_in_s=ww_ready,
        bloodrage_ready=_is_legal(actions, ACTION_KEY_TO_REF["warrior.bloodrage"]),
        death_wish_ready=_is_legal(actions, ACTION_KEY_TO_REF["warrior.death_wish"]),
        mainhand_swing_remaining_s=mh_remaining_ms / 1000.0,
        mainhand_swing_duration_s=mh_duration_ms / 1000.0,
        execute_cost=10.0,
        heroic_strike_cost=12.0,
        cleave_cost=18.0,
        whirlwind_cost=25.0,
        contra_st_s=mh_remaining_ms / 1000.0,
        contra_ss_s=max(0.0, (mh_duration_ms - mh_remaining_ms) / 1000.0),
        contra_sd_s=mh_duration_ms / 1000.0,
        contra_zssdw=3,
        nampower=True,
    )


def build_expert_guided_artifact(
    bridge: SimulatorBridge,
    raid_sim_request: Mapping[str, Any],
    *,
    seeds: Sequence[int],
    horizon_ms: int,
    profile_metadata: Mapping[str, Any],
    expert_manifest: Mapping[str, Any] | None,
    cat2_profile_snapshot: Mapping[str, Any] | None = None,
    cat2_profile_snapshot_file_sha256: str | None = None,
    cat2_profile_snapshot_path: str | None = None,
) -> dict[str, Any]:
    if not seeds:
        raise ValueError("seeds must not be empty")

    cat2_snapshot_input = _cat2_snapshot_input_receipt(
        cat2_profile_snapshot,
        file_sha256=cat2_profile_snapshot_file_sha256,
        path=cat2_profile_snapshot_path,
    )

    cat = CatFurySourceAdapter()
    contra = ContraDeployedSourceAdapter()
    contra_new = ContraNewCandidateAdapter()
    cat2_current = (
        Cat2ProfileAdapter()
        if cat2_profile_snapshot is None
        else Cat2SavedProfileSourceAdapterV1(cat2_profile_snapshot)
    )
    cat2_curated = Cat2ProfileAdapter(
        (
            CuratedCat2Card("warrior_heroic_strike", {"rageThreshold": 50.0}),
            CuratedCat2Card("warrior_execute"),
            CuratedCat2Card("warrior_bloodthirst"),
            CuratedCat2Card("warrior_whirlwind"),
            CuratedCat2Card(
                "warrior_slam", {"minimumSwingTime": 1.5}
            ),
        ),
        profile_name="curated_fury_rotation_probe_v1",
    )
    adapters = (cat, contra, contra_new, cat2_current, cat2_curated)
    prior = FuryChroniclePrior()

    proposal_rows: list[dict[str, Any]] = []
    candidate_sources: dict[tuple[Any, ...], set[str]] = {}
    candidate_source_seeds: dict[
        tuple[Any, ...], dict[str, set[int]]
    ] = {}
    candidate_decisions: dict[tuple[Any, ...], FactorizedDecision] = {}
    chronicle_candidate_keys: set[tuple[Any, ...]] = set()
    chronicle_fallback_layers: set[str] = set()
    cat2_current_expert_id = (
        Cat2SavedProfileSourceAdapterV1.expert_id
        if cat2_profile_snapshot is not None
        else None
    )
    cat2_proposal_row_count = 0
    cat2_candidate_keys: set[tuple[Any, ...]] = set()
    legal_intersection: set[ActionRef] | None = None

    for seed in seeds:
        sim_state = bridge.load(raid_sim_request, seed)
        available = bridge.actions()
        legal_now = {row.action for row in available if row.legal}
        legal_intersection = (
            legal_now
            if legal_intersection is None
            else legal_intersection.intersection(legal_now)
        )
        expert_state = fury_state_from_simulator(sim_state, available, raid_sim_request)
        seed_row: dict[str, Any] = {
            "seed": seed,
            "simulator_state": _compact_sim_state(sim_state),
            "expert_state": _jsonable(asdict(expert_state)),
            "experts": [],
        }
        for adapter in adapters:
            result = adapter.propose(expert_state)
            if result.expert_id == cat2_current_expert_id:
                cat2_proposal_row_count += 1
            projected = project_expert_decision(
                result,
                available,
                current_stance=expert_state.current_stance,
                target_is_usable=expert_state.target_exists,
                casting=expert_state.casting_slam,
            )
            seed_row["experts"].append(projected.to_dict())
            if projected.accepted and projected.decision is not None:
                _add_candidate(
                    candidate_sources,
                    candidate_decisions,
                    projected.decision,
                    result.expert_id,
                    source_seeds=candidate_source_seeds,
                    seed=seed,
                )
                if result.expert_id == cat2_current_expert_id:
                    cat2_candidate_keys.add(_decision_key(projected.decision))

        last_auto_bucket = "MISSING"
        prior_result = prior.propose([], last_auto_bucket, top_k=3)
        chronicle_fallback_layers.add(str(prior_result["fallback_layer"]))
        seed_row["chronicle_prior"] = prior_result
        for proposal in prior_result["proposals"]:
            action = ACTION_KEY_TO_REF.get(str(proposal["action_key"]))
            available_row = next(
                (row for row in available if row.action == action), None
            )
            if (
                action is not None
                and available_row is not None
                and available_row.legal
                and available_row.triggers_gcd
            ):
                prior_decision = FactorizedDecision(gcd=action)
                _add_candidate(
                    candidate_sources,
                    candidate_decisions,
                    prior_decision,
                    "chronicle.partial_markov_v1",
                    source_seeds=candidate_source_seeds,
                    seed=seed,
                )
                chronicle_candidate_keys.add(_decision_key(prior_decision))
        proposal_rows.append(seed_row)

    if legal_intersection is None:
        raise ExpertGuidedSearchError("no root legal action set was collected")
    _add_exploration_candidates(
        candidate_sources,
        candidate_decisions,
        legal_intersection,
        source_seeds=candidate_source_seeds,
    )
    candidate_entries = [
        (
            key,
            PrefixCandidate(
            candidate_id=_factorized_candidate_id(decision),
            decision=decision,
            proposed_by=tuple(sorted(candidate_sources[key])),
            ),
        )
        for key, decision in candidate_decisions.items()
    ]
    candidate_entries.sort(key=lambda value: value[1].candidate_id)
    candidates = [candidate for _, candidate in candidate_entries]
    candidate_union = []
    for key, candidate in candidate_entries:
        row = candidate.to_dict()
        seed_map = candidate_source_seeds.get(key, {})
        row["proposal_seed_ids"] = {
            source: sorted(source_seeds)
            for source, source_seeds in sorted(seed_map.items())
        }
        row["proposal_seed_counts"] = {
            source: len(source_seeds)
            for source, source_seeds in sorted(seed_map.items())
        }
        row["proposed_by_semantics"] = (
            "source proposed this decision for at least one listed root seed; "
            "it is not a per-state policy label"
        )
        candidate_union.append(row)

    chronicle_exclusive_final_count = sum(
        candidate_sources[key] == {"chronicle.partial_markov_v1"}
        for key in chronicle_candidate_keys
    )
    cat2_exclusive_final_count = (
        sum(
            candidate_sources[key] == {cat2_current_expert_id}
            for key in cat2_candidate_keys
        )
        if cat2_current_expert_id is not None
        else 0
    )
    wait_candidate_id = _factorized_candidate_id(FactorizedDecision(wait_ms=100))
    benchmark = benchmark_prefix_candidates(
        bridge,
        raid_sim_request,
        seeds=seeds,
        candidates=candidates,
        horizon_ms=horizon_ms,
        reference_candidate_id=wait_candidate_id,
    )

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return {
        "schema_version": 1,
        "kind": "fury_expert_guided_search_v1",
        "generated_at": generated_at,
        "validation_status": "SIMULATOR_FIXED_HORIZON_PREFIX_BENCHMARK_ONLY",
        "deployment_allowed": False,
        "request_scope": {
            "profile_kind": profile_metadata.get("kind"),
            "clean_weapon_ids": [18832, 19866],
            "single_target": True,
            "horizon_ms": horizon_ms,
            "seed_count": len(seeds),
        },
        "content_addressed_inputs": {
            "cat2_profile_snapshot": cat2_snapshot_input,
        },
        "expert_contract": {
            "selection_rule": "candidate union; never majority vote",
            "exact_runtime_expert_count": 0,
            "live_blackbox_trace_count": 0,
            "source_derived_experts": [
                "cat.fury.profile1",
                "contra.deployed.fury.raid_a",
                *(
                    [Cat2SavedProfileSourceAdapterV1.expert_id]
                    if cat2_profile_snapshot is not None
                    else []
                ),
            ],
            "deployed_non_voting_sources": (
                [Cat2SavedProfileSourceAdapterV1.expert_id]
                if cat2_profile_snapshot is not None
                else []
            ),
            "candidate_only_sources": [
                "contra_new.fury.candidate",
                "cat2.fury.curated_candidate",
                "chronicle.partial_markov_v1",
                "explore",
            ],
            "cat2_live_status": (
                "CURRENT_UNSEALED_SOURCE_PROFILE"
                if cat2_profile_snapshot is not None
                else "MISSING_NO_PROFILE"
            ),
            "cat2_current_profile": (
                {
                    "expert_id": Cat2SavedProfileSourceAdapterV1.expert_id,
                    "role": "DEPLOYED",
                    "provenance_kind": "SOURCE_DERIVED",
                    "authority_state": "CURRENT_UNSEALED_SOURCE_PROFILE",
                    "eligible_for_independent_vote": False,
                    "source_execution": False,
                    "exact_runtime": False,
                }
                if cat2_profile_snapshot is not None
                else None
            ),
            "exact_expert_comparison_complete": False,
        },
        "profile_metadata": profile_metadata,
        "expert_manifest": expert_manifest,
        "proposal_rows": proposal_rows,
        "candidate_union_scope": {
            "kind": "GLOBAL_UNION_ACROSS_ROOT_SEEDS",
            "per_state_policy_label": False,
            "proposed_by_semantics": (
                "a source proposed the candidate for at least one root seed"
            ),
        },
        "chronicle_prior_usage": {
            "requested_recent_action_context": [],
            "requested_last_auto_attack_bucket": "MISSING",
            "fallback_layers": sorted(chronicle_fallback_layers),
            "legal_candidate_count": len(chronicle_candidate_keys),
            "exclusive_final_candidate_count": chronicle_exclusive_final_count,
            "search_guidance_effect": (
                "PROVENANCE_ONLY_NO_UNIQUE_FINAL_CANDIDATE"
                if chronicle_exclusive_final_count == 0
                else "ADDED_UNIQUE_FINAL_CANDIDATE"
            ),
            "ranking_weight_applied": False,
        },
        "cat2_profile_usage": {
            "expert_id": cat2_current_expert_id,
            "authority_state": (
                "CURRENT_UNSEALED_SOURCE_PROFILE"
                if cat2_current_expert_id is not None
                else "MISSING_NO_PROFILE"
            ),
            "proposal_row_count": cat2_proposal_row_count,
            "distinct_candidate_count": len(cat2_candidate_keys),
            "exclusive_final_candidate_count": cat2_exclusive_final_count,
            "search_guidance_effect": (
                "NOT_AVAILABLE_NO_PROFILE"
                if cat2_current_expert_id is None
                else (
                    "PROVENANCE_ONLY_NO_UNIQUE_FINAL_CANDIDATE"
                    if cat2_exclusive_final_count == 0
                    else "ADDED_UNIQUE_FINAL_CANDIDATE"
                )
            ),
            "ranking_weight_applied": False,
            "independent_vote_applied": False,
        },
        "candidate_union": candidate_union,
        "benchmark": benchmark,
        "promotion_gates": {
            "full_policy_comparison_complete": False,
            "real_game_superiority_demonstrated": False,
            "simulator_mechanics_complete": False,
            "policy_compile_allowed": False,
        },
        "known_limitations": [
            "only the first factorized decision is supplied; continuation is passive auto attack",
            "Cat and Contra proposals are source-derived translations, not Lua runtime execution",
            (
                "Cat2's current profile is replayed from an unsealed source/SavedVariables "
                "snapshot; it is not Lua runtime execution and cannot vote independently"
                if cat2_profile_snapshot is not None
                else "Cat2 has no supplied current Fury profile and its curated stack is candidate-only"
            ),
            "contra_new is incomplete and cannot vote independently",
            "Chronicle prior lacks legality, queue intent, timing state, and causal reward",
            "the current root-state query has no recent-action context and uses only the Chronicle global fallback",
            "the candidate union is global across seeds; proposed_by is not a per-state expert label",
            "candidate-evaluation rows do not contain state/action*/next-state teacher tuples",
            "same seed is paired but the simulator does not expose labeled random streams for true CRN",
            "white-rage holdout, dual-wield miss, multi-target, incoming rage, target switching, and several proc/haste mechanisms remain gated",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    parser.add_argument("--profile-metadata", type=Path, default=DEFAULT_PROFILE_METADATA)
    parser.add_argument("--expert-manifest", type=Path, default=DEFAULT_EXPERT_MANIFEST)
    parser.add_argument(
        "--cat2-profile-snapshot",
        type=Path,
        default=DEFAULT_CAT2_PROFILE_SNAPSHOT,
    )
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--horizon-ms", type=int, default=6000)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--candidate-evaluation-output",
        type=Path,
        default=DEFAULT_EVALUATION_ROWS,
    )
    args = parser.parse_args(argv)

    try:
        request = _read_object(args.request)
        metadata = _read_object(args.profile_metadata)
        manifest = (
            _read_object(args.expert_manifest)
            if args.expert_manifest.exists()
            else None
        )
        cat2_snapshot, cat2_snapshot_file_sha256 = _read_object_with_sha256(
            args.cat2_profile_snapshot
        )
        seeds = tuple(args.seeds) if args.seeds else DEFAULT_SEEDS
        if args.horizon_ms <= 0:
            raise ValueError("horizon_ms must be positive")
        with SimulatorBridge(args.bridge) as bridge:
            artifact = build_expert_guided_artifact(
                bridge,
                request,
                seeds=seeds,
                horizon_ms=args.horizon_ms,
                profile_metadata=metadata,
                expert_manifest=manifest,
                cat2_profile_snapshot=cat2_snapshot,
                cat2_profile_snapshot_file_sha256=cat2_snapshot_file_sha256,
                cat2_profile_snapshot_path=str(args.cat2_profile_snapshot.resolve()),
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _write_candidate_evaluation_jsonl(
            args.candidate_evaluation_output,
            artifact,
        )
        print(json.dumps(_console_summary(artifact), ensure_ascii=False, indent=2))
        return 0
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        ExpertGuidedSearchError,
        SimBridgeError,
    ) as error:
        print(f"Fury expert-guided search failed: {error}", file=sys.stderr)
        return 2


def _add_exploration_candidates(
    sources: dict[tuple[Any, ...], set[str]],
    decisions: dict[tuple[Any, ...], FactorizedDecision],
    legal: set[ActionRef],
    *,
    source_seeds: dict[tuple[Any, ...], dict[str, set[int]]] | None = None,
) -> None:
    terminals: list[tuple[ActionRef | None, int | None]] = [(None, 100)]
    terminals.extend(
        (ACTION_KEY_TO_REF[key], None)
        for key in ROTATION_GCD_KEYS
        if ACTION_KEY_TO_REF[key] in legal
    )
    queues: tuple[ActionRef | None, ...] = (
        None,
        *(queue for queue in QUEUE_REFS.values() if queue in legal),
    )
    for queue in queues:
        for gcd, wait_ms in terminals:
            _add_candidate(
                sources,
                decisions,
                FactorizedDecision(queue=queue, gcd=gcd, wait_ms=wait_ms),
                "explore",
                source_seeds=source_seeds,
            )


def _add_candidate(
    sources: dict[tuple[Any, ...], set[str]],
    decisions: dict[tuple[Any, ...], FactorizedDecision],
    decision: FactorizedDecision,
    source: str,
    *,
    source_seeds: dict[tuple[Any, ...], dict[str, set[int]]] | None = None,
    seed: int | None = None,
) -> None:
    key = _decision_key(decision)
    decisions.setdefault(key, decision)
    sources.setdefault(key, set()).add(source)
    if source_seeds is not None and seed is not None:
        source_seeds.setdefault(key, {}).setdefault(source, set()).add(seed)


def _decision_key(decision: FactorizedDecision) -> tuple[Any, ...]:
    return (
        decision.queue,
        decision.gcd,
        decision.wait_ms,
        decision.cancel_queue,
    )


def _factorized_candidate_id(decision: FactorizedDecision) -> str:
    if decision.cancel_queue:
        queue = "cancel"
    elif decision.queue is None:
        queue = "keep"
    elif decision.queue.spell_id == 25286:
        queue = "heroic_strike"
    elif decision.queue.spell_id == 20569:
        queue = "cleave"
    else:
        queue = f"spell_{decision.queue.spell_id}"
    if decision.gcd is None:
        terminal = f"wait_{decision.wait_ms}ms"
    else:
        terminal = next(
            (
                key.rsplit(".", 1)[-1]
                for key, action in ACTION_KEY_TO_REF.items()
                if action == decision.gcd
            ),
            f"spell_{decision.gcd.spell_id}",
        )
    return f"queue_{queue}__gcd_{terminal}"


def _ready_seconds(
    actions: Mapping[ActionRef, AvailableAction], action: ActionRef
) -> float:
    available = actions.get(action)
    return (
        UNAVAILABLE_COOLDOWN_S
        if available is None
        else available.ready_in_ms / 1000.0
    )


def _is_legal(
    actions: Mapping[ActionRef, AvailableAction], action: ActionRef
) -> bool:
    available = actions.get(action)
    return available is not None and available.legal


def _aura_remaining_seconds(rows: list[Any], needle: str) -> float:
    values = [
        _optional_number(row.get("remaining_ms"), default=0.0) / 1000.0
        for row in rows
        if isinstance(row, Mapping) and needle in str(row.get("label", ""))
    ]
    return max(values, default=0.0)


def _player(request: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        player = request["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise ExpertGuidedSearchError("RaidSimRequest lacks the first player") from error
    if not isinstance(player, Mapping):
        raise ExpertGuidedSearchError("RaidSimRequest first player is invalid")
    return player


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpertGuidedSearchError(f"{label} must be numeric")
    return float(value)


def _optional_number(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _compact_sim_state(state: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "time_ms",
        "power",
        "gcd_remaining_ms",
        "mh_swing_remaining_ms",
        "mh_swing_duration_ms",
        "oh_swing_remaining_ms",
        "target_health_known",
        "target_health_percent",
        "execute_phase_20",
        "damage_done",
    )
    return {key: state.get(key) for key in keys}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _read_object_with_sha256(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def _cat2_snapshot_input_receipt(
    snapshot: Mapping[str, Any] | None,
    *,
    file_sha256: str | None,
    path: str | None,
) -> dict[str, Any]:
    if snapshot is None:
        if file_sha256 is not None or path is not None:
            raise ValueError(
                "Cat2 snapshot hash/path cannot be supplied without a snapshot"
            )
        return {
            "provided": False,
            "authority_state": "MISSING_NO_PROFILE",
        }
    if not isinstance(snapshot, Mapping):
        raise TypeError("cat2_profile_snapshot must be a mapping or None")
    try:
        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Cat2 profile snapshot is not strict JSON data: {error}") from error
    canonical_sha256 = hashlib.sha256(canonical).hexdigest()
    if file_sha256 is not None and not _is_lower_sha256(file_sha256):
        raise ValueError("cat2_profile_snapshot_file_sha256 must be lowercase SHA-256")
    primary_sha256 = file_sha256 or canonical_sha256
    primary_hash_scope = (
        "EXACT_FILE_BYTES" if file_sha256 is not None else "CANONICAL_JSON_OBJECT"
    )
    profile = snapshot.get("profile")
    steps = profile.get("steps") if isinstance(profile, Mapping) else None
    step_order = (
        [str(step.get("id")) for step in steps if isinstance(step, Mapping)]
        if isinstance(steps, list)
        else []
    )
    return {
        "provided": True,
        "path": path,
        "content_sha256": primary_sha256,
        "content_hash_scope": primary_hash_scope,
        "canonical_json_sha256": canonical_sha256,
        "artifact_type": snapshot.get("artifact_type"),
        "schema_version": snapshot.get("schema_version"),
        "authority_state": snapshot.get("authority_state"),
        "raw_savedvariables_sha256": snapshot.get("raw_savedvariables_sha256"),
        "profile_semantic_sha256": snapshot.get("profile_semantic_sha256"),
        "source_bundle_sha256": snapshot.get("source_bundle_sha256"),
        "profile_id": profile.get("id") if isinstance(profile, Mapping) else None,
        "profile_name": profile.get("name") if isinstance(profile, Mapping) else None,
        "profile_step_order": step_order,
        "source_execution": False,
        "exact_runtime": False,
        "eligible_for_independent_vote": False,
    }


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_candidate_evaluation_jsonl(
    path: Path,
    artifact: Mapping[str, Any],
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "schema_version": 1,
            "kind": "fury_prefix_candidate_evaluation_header",
            "generated_at": artifact["generated_at"],
            "deployment_allowed": False,
            "training_eligible": False,
            "contains_state_action_next_state_tuples": False,
            "candidate_union_scope": artifact["candidate_union_scope"],
            "validation_status": artifact["validation_status"],
            "content_addressed_inputs": artifact.get("content_addressed_inputs", {}),
            "cat2_profile_usage": artifact.get("cat2_profile_usage", {}),
        }
    ]
    benchmark = artifact["benchmark"]
    rows.extend(
        {"schema_version": 1, "kind": "prefix_candidate_summary", **row}
        for row in benchmark["ranking"]
    )
    rows.extend(
        {"schema_version": 1, "kind": "prefix_candidate_trial", **row}
        for row in benchmark["trials"]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _console_summary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    benchmark = artifact["benchmark"]
    return {
        "output_kind": artifact["kind"],
        "validation_status": artifact["validation_status"],
        "candidate_count": benchmark["candidate_count"],
        "trial_count": benchmark["trial_count"],
        "horizon_ms": benchmark["horizon_ms"],
        "top_candidates": benchmark["ranking"][:5],
        "deployment_allowed": artifact["deployment_allowed"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
