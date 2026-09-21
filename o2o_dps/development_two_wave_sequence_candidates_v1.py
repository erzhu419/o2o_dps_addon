"""Fixed v7 candidate family for one development-only two-wave segment.

Candidate zero is an explicit exact-Cat/no-sequence control.  The searched
family is the Cartesian product of all permutations of Bloodthirst,
Whirlwind, and Turtle Slam for wave one and wave two.  The waves own separate
target routes and next-swing queues.  A caller-supplied finite-resource action
is attempted behind a causal wave-one HP/remaining-time guard; when that guard
does not pass, the same resource ID is retried on wave two.

Expert and offline sources are proposal provenance only.  Their IDs remain on
the candidate-set receipt, while ``freeze_two_wave_segment_policy_v1`` removes
all guide metadata from deployable schedules.  This is a finite three-GCD
prefix family for one continuous two-wave segment, not a full Upper Kara route
policy or a claim that the three actions form a complete rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product
from typing import Any, Sequence

from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .development_two_wave_segment_policy_v1 import (
    DevelopmentTwoWaveSegmentPolicyV1,
    SegmentWaveV1,
    WaveConditionedSequenceStepV1,
    freeze_two_wave_segment_policy_v1,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .sim_bridge import ActionRef
from .wave_action_schedule_v1 import QueueLaneOp, ScheduledActionPlan


JSONMap = dict[str, Any]
SCHEMA = "development_two_wave_sequence_candidate_set/v1"
SCOPE = "DEVELOPMENT_TWO_WAVE_SEGMENT"
PAIRED_ZERO_ROLE = "PAIRED_EXACT_CAT_NO_SEQUENCE_ZERO"
SEARCHED_ROLE = "SEARCHED_WAVE_CONDITIONED_THREE_GCD_PREFIX"

BLOODTHIRST_V1 = ActionRef(spell_id=23_894)
WHIRLWIND_V1 = ActionRef(spell_id=1_680)
TURTLE_SLAM_V1 = ActionRef(spell_id=45_961)
CLEAVE_QUEUE_V1 = ActionRef(spell_id=20_569, tag=1)
HEROIC_STRIKE_QUEUE_V1 = ActionRef(spell_id=25_286, tag=1)

GCD_ACTIONS_BY_KEY_V1: dict[str, ActionRef] = {
    "bloodthirst": BLOODTHIRST_V1,
    "whirlwind": WHIRLWIND_V1,
    "slam": TURTLE_SLAM_V1,
}
GCD_ORDER_KEYS_V1 = tuple(
    permutations(tuple(GCD_ACTIONS_BY_KEY_V1))
)
PAIRED_CANDIDATE_COUNT_V1 = 1 + len(GCD_ORDER_KEYS_V1) ** 2

FRESH_TRAIN_SEED_START_V1 = 920_001
FRESH_EVALUATION_SEED_START_V1 = 1_020_001
FRESH_SEED_COUNT_V1 = 256
FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V1 = (0, 1_000, 3_000, 5_000, 7_000, 9_000)


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _validate_action(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if (
        any(isinstance(row, bool) or not isinstance(row, int) or row < 0 for row in (*identities, value.tag))
        or sum(row > 0 for row in identities) != 1
    ):
        raise ValueError(f"{label} must have one positive identity and a nonnegative tag")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class DevelopmentTwoWaveFreshSeedProtocolV1:
    """Predeclared disjoint seeds for the first v7 development campaign."""

    train_seed_start: int = FRESH_TRAIN_SEED_START_V1
    evaluation_seed_start: int = FRESH_EVALUATION_SEED_START_V1
    seed_count: int = FRESH_SEED_COUNT_V1
    environment_arrival_nuisance_schedule_ms: tuple[int, ...] = (
        FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V1
    )

    def __post_init__(self) -> None:
        train_start = _positive_int(self.train_seed_start, "train_seed_start")
        evaluation_start = _positive_int(
            self.evaluation_seed_start, "evaluation_seed_start"
        )
        count = _positive_int(self.seed_count, "seed_count")
        train_stop = train_start + count
        evaluation_stop = evaluation_start + count
        if max(train_start, evaluation_start) < min(train_stop, evaluation_stop):
            raise ValueError("fresh train and evaluation seed ranges overlap")
        nuisance = self.environment_arrival_nuisance_schedule_ms
        if (
            not isinstance(nuisance, tuple)
            or not nuisance
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in nuisance
            )
            or len(set(nuisance)) != len(nuisance)
        ):
            raise ValueError(
                "environment_arrival_nuisance_schedule_ms must contain unique "
                "nonnegative integers"
            )

    @property
    def train_seeds(self) -> tuple[int, ...]:
        return tuple(range(self.train_seed_start, self.train_seed_start + self.seed_count))

    @property
    def evaluation_seeds(self) -> tuple[int, ...]:
        return tuple(
            range(
                self.evaluation_seed_start,
                self.evaluation_seed_start + self.seed_count,
            )
        )

    def to_dict(self) -> JSONMap:
        return {
            "train": {
                "first": self.train_seed_start,
                "last": self.train_seed_start + self.seed_count - 1,
                "count": self.seed_count,
            },
            "evaluation": {
                "first": self.evaluation_seed_start,
                "last": self.evaluation_seed_start + self.seed_count - 1,
                "count": self.seed_count,
            },
            "disjoint": True,
            "frozen_before_search": True,
            "environment_arrival_nuisance_schedule_ms": list(
                self.environment_arrival_nuisance_schedule_ms
            ),
            "arrival_nuisance_is_policy_input": False,
        }


@dataclass(frozen=True)
class DevelopmentTwoWaveSequenceCandidateV1:
    """One paired control or one frozen searched two-wave prefix."""

    candidate_index: int
    candidate_id: str
    role: str
    wave_one_gcd_order: tuple[str, ...] = ()
    wave_two_gcd_order: tuple[str, ...] = ()
    policy: DevelopmentTwoWaveSegmentPolicyV1 | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.candidate_index, bool)
            or not isinstance(self.candidate_index, int)
            or self.candidate_index < 0
        ):
            raise ValueError("candidate_index must be a nonnegative integer")
        object.__setattr__(
            self, "candidate_id", _nonempty(self.candidate_id, "candidate_id")
        )
        if self.role == PAIRED_ZERO_ROLE:
            if self.candidate_index != 0:
                raise ValueError("paired exact-Cat zero must have index zero")
            if self.policy is not None or self.wave_one_gcd_order or self.wave_two_gcd_order:
                raise ValueError("exact-Cat zero must not contain a sequence policy")
            return
        if self.role != SEARCHED_ROLE:
            raise ValueError("candidate role is not recognized")
        if self.candidate_index == 0:
            raise ValueError("searched candidates cannot use index zero")
        expected = set(GCD_ACTIONS_BY_KEY_V1)
        if (
            len(self.wave_one_gcd_order) != 3
            or set(self.wave_one_gcd_order) != expected
            or len(self.wave_two_gcd_order) != 3
            or set(self.wave_two_gcd_order) != expected
        ):
            raise ValueError("each searched wave must permute the three v1 GCD actions")
        if not isinstance(self.policy, DevelopmentTwoWaveSegmentPolicyV1):
            raise TypeError("searched candidate requires a frozen segment policy")

    def semantic_key(self) -> tuple[tuple[str, ...], tuple[str, ...]] | tuple[str]:
        if self.role == PAIRED_ZERO_ROLE:
            return ("EXACT_CAT_NO_SEQUENCE",)
        return (self.wave_one_gcd_order, self.wave_two_gcd_order)

    def to_dict(self) -> JSONMap:
        result: JSONMap = {
            "candidate_index": self.candidate_index,
            "candidate_id": self.candidate_id,
            "role": self.role,
            "wave_one_gcd_order": list(self.wave_one_gcd_order),
            "wave_two_gcd_order": list(self.wave_two_gcd_order),
        }
        if self.policy is None:
            result["execution"] = {
                "policy_id": CAT_POLICY_ID,
                "sequence": None,
                "paired_zero": True,
            }
        else:
            result["execution"] = {
                "policy_id": self.policy.policy_id,
                "sequence": self.policy.to_dict(),
                "paired_zero": False,
            }
        return result


@dataclass(frozen=True)
class DevelopmentTwoWaveSequenceCandidateSetV1:
    """The fixed exact-Cat zero plus 6-by-6 independent wave permutations."""

    exact_build_id: str
    waves: tuple[SegmentWaveV1, SegmentWaveV1]
    long_cooldown_action: ActionRef
    long_cooldown_triggers_gcd: bool
    resource_id: str
    wave_one_min_target_hp_pct: float
    wave_one_min_estimated_remaining_ms: int
    proposal_guide_ids: tuple[str, ...]
    seed_protocol: DevelopmentTwoWaveFreshSeedProtocolV1
    candidates: tuple[DevelopmentTwoWaveSequenceCandidateV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "exact_build_id", _nonempty(self.exact_build_id, "exact_build_id")
        )
        if (
            not isinstance(self.waves, tuple)
            or len(self.waves) != 2
            or any(not isinstance(row, SegmentWaveV1) for row in self.waves)
        ):
            raise TypeError("waves must contain exactly two SegmentWaveV1 rows")
        if len(self.waves[0].target_indexes) < 2:
            raise ValueError("wave one must declare at least two target indexes")
        if len(self.waves[1].target_indexes) != 1:
            raise ValueError("wave two must declare exactly one target index")
        _validate_action(self.long_cooldown_action, "long_cooldown_action")
        if not isinstance(self.long_cooldown_triggers_gcd, bool):
            raise TypeError("long_cooldown_triggers_gcd must be boolean")
        object.__setattr__(self, "resource_id", _nonempty(self.resource_id, "resource_id"))
        if isinstance(self.wave_one_min_target_hp_pct, bool) or not isinstance(
            self.wave_one_min_target_hp_pct, (int, float)
        ) or not 0 <= float(self.wave_one_min_target_hp_pct) <= 100:
            raise ValueError("wave_one_min_target_hp_pct must be in [0, 100]")
        _positive_int(
            self.wave_one_min_estimated_remaining_ms,
            "wave_one_min_estimated_remaining_ms",
        )
        if (
            not isinstance(self.proposal_guide_ids, tuple)
            or not self.proposal_guide_ids
            or any(not isinstance(row, str) or not row.strip() for row in self.proposal_guide_ids)
            or len(set(self.proposal_guide_ids)) != len(self.proposal_guide_ids)
        ):
            raise ValueError("proposal_guide_ids must be unique nonempty strings")
        if not isinstance(self.seed_protocol, DevelopmentTwoWaveFreshSeedProtocolV1):
            raise TypeError("seed_protocol has the wrong type")
        if len(self.candidates) != PAIRED_CANDIDATE_COUNT_V1:
            raise ValueError(
                f"candidate set must contain exactly {PAIRED_CANDIDATE_COUNT_V1} rows"
            )
        if tuple(row.candidate_index for row in self.candidates) != tuple(
            range(PAIRED_CANDIDATE_COUNT_V1)
        ):
            raise ValueError("candidate indexes must be contiguous from zero")
        if self.candidates[0].role != PAIRED_ZERO_ROLE:
            raise ValueError("candidate zero must be the exact-Cat/no-sequence control")
        ids = [row.candidate_id for row in self.candidates]
        semantics = [row.semantic_key() for row in self.candidates]
        if len(ids) != len(set(ids)) or len(semantics) != len(set(semantics)):
            raise ValueError("candidates must have unique identities and semantics")
        expected_pairs = set(product(GCD_ORDER_KEYS_V1, repeat=2))
        actual_pairs = {
            (row.wave_one_gcd_order, row.wave_two_gcd_order)
            for row in self.candidates[1:]
        }
        if actual_pairs != expected_pairs:
            raise ValueError("searched rows must cover the complete 6-by-6 order product")
        for row in self.candidates[1:]:
            assert row.policy is not None
            if row.policy.exact_build_id != self.exact_build_id or row.policy.waves != self.waves:
                raise ValueError("searched policy build or wave registry drifted")
            if any(
                step.plan.guide_provenance or step.plan.guide_priority != 0.0
                for step in row.policy.steps
            ):
                raise ValueError("frozen policies must not retain proposal guides")

    def to_dict(self) -> JSONMap:
        return {
            "schema": SCHEMA,
            "scope": SCOPE,
            "exact_build_id": self.exact_build_id,
            "waves": [row.to_dict() for row in self.waves],
            "candidate_count": len(self.candidates),
            "paired_zero_candidate_id": self.candidates[0].candidate_id,
            "searched_candidate_count": len(self.candidates) - 1,
            "gcd_action_catalog": {
                key: action.to_wire() for key, action in GCD_ACTIONS_BY_KEY_V1.items()
            },
            "wave_queues": {
                "wave_one": CLEAVE_QUEUE_V1.to_wire(),
                "wave_two": HEROIC_STRIKE_QUEUE_V1.to_wire(),
            },
            "finite_resource": {
                "resource_id": self.resource_id,
                "action": self.long_cooldown_action.to_wire(),
                "execution_lane": (
                    "GCD" if self.long_cooldown_triggers_gcd else "OFF_GCD"
                ),
                "wave_one_guard": {
                    "attackable_target_count_gte": 2,
                    "target_hp_pct_gte": float(self.wave_one_min_target_hp_pct),
                    "estimated_remaining_attackable_gte_ms": (
                        self.wave_one_min_estimated_remaining_ms
                    ),
                },
                "wave_two_retry_same_resource_id": True,
                "failed_wave_one_guard_consumes_resource": False,
            },
            "proposal_guide_ids": list(self.proposal_guide_ids),
            "fresh_seed_protocol": self.seed_protocol.to_dict(),
            "candidates": [row.to_dict() for row in self.candidates],
            "contract": {
                "candidate_zero_is_exact_cat_no_sequence": True,
                "wave_orders_independently_enumerated": True,
                "wave_one_multitarget_cleave_queue": True,
                "wave_two_single_target_heroic_strike_queue": True,
                "runtime_guard_uses_arrival_or_wave_control_id": False,
                "runtime_guard_uses_current_observation_only": True,
                "arrival_schedule_is_environment_nuisance_only": True,
                "proposal_guides_removed_from_frozen_policies": True,
                "full_upper_kara_route_claim": False,
                "prefix_gcd_count_per_wave": 3,
            },
            "limitations": [
                "The searched object is a finite three-GCD prefix per wave.",
                "Only one caller-declared continuous two-wave segment is represented.",
                "Candidate generation alone is not a train, held-out, or real-environment result.",
            ],
        }


def _resource_step_v1(
    *,
    step_id: str,
    wave: SegmentWaveV1,
    long_cooldown_action: ActionRef,
    resource_id: str,
    proposal_guide_ids: tuple[str, ...],
    long_cooldown_triggers_gcd: bool,
    first_wave: bool,
    wave_one_min_target_hp_pct: float,
    wave_one_min_estimated_remaining_ms: int,
) -> WaveConditionedSequenceStepV1:
    guard_kwargs: JSONMap = {
        "target_index": wave.target_indexes[0],
        "target_attackable_is": True,
        "action_ready": long_cooldown_action,
        "false_semantics": SKIP_PLAN,
    }
    if first_wave:
        guard_kwargs.update(
            target_hp_pct_gte=float(wave_one_min_target_hp_pct),
            attackable_target_count_gte=2,
            estimated_remaining_attackable_gte_ms=(
                wave_one_min_estimated_remaining_ms
            ),
        )
    else:
        guard_kwargs.update(
            attackable_target_count_gte=1,
            attackable_target_count_lte=1,
        )
    lane_kwargs = (
        {"gcd_action": long_cooldown_action}
        if long_cooldown_triggers_gcd
        else {"off_gcd_actions": (long_cooldown_action,)}
    )
    return WaveConditionedSequenceStepV1(
        step_id=step_id,
        wave_id=wave.wave_id,
        resource_id=resource_id,
        plan=ScheduledActionPlan(
            at_or_after_ms=0,
            **lane_kwargs,
            guard=ObservableCausalGuardV1(**guard_kwargs),
            guide_provenance=proposal_guide_ids,
            guide_priority=1.0,
        ),
    )


def _gcd_steps_v1(
    *,
    wave: SegmentWaveV1,
    wave_label: str,
    gcd_order: tuple[str, ...],
    queue_action: ActionRef,
    proposal_guide_ids: tuple[str, ...],
) -> tuple[WaveConditionedSequenceStepV1, ...]:
    result: list[WaveConditionedSequenceStepV1] = []
    for index, action_key in enumerate(gcd_order):
        target_index = wave.target_indexes[index % len(wave.target_indexes)]
        gcd_action = GCD_ACTIONS_BY_KEY_V1[action_key]
        result.append(
            WaveConditionedSequenceStepV1(
                step_id=f"{wave_label}-gcd-{index + 1}-{action_key}",
                wave_id=wave.wave_id,
                plan=ScheduledActionPlan(
                    at_or_after_ms=index * 1_500,
                    target_index=target_index,
                    queue_op=QueueLaneOp.SET,
                    queue_action=queue_action,
                    gcd_action=gcd_action,
                    guard=ObservableCausalGuardV1(
                        target_index=target_index,
                        target_attackable_is=True,
                        action_ready=gcd_action,
                        false_semantics=SKIP_PLAN,
                    ),
                    guide_provenance=proposal_guide_ids,
                    guide_priority=1.0,
                ),
            )
        )
    return tuple(result)


def build_development_two_wave_sequence_candidate_set_v1(
    *,
    exact_build_id: str,
    waves: tuple[SegmentWaveV1, SegmentWaveV1],
    long_cooldown_action: ActionRef,
    long_cooldown_triggers_gcd: bool = True,
    resource_id: str,
    proposal_guide_ids: Sequence[str],
    wave_one_min_target_hp_pct: float = 50.0,
    wave_one_min_estimated_remaining_ms: int = 4_500,
    seed_protocol: DevelopmentTwoWaveFreshSeedProtocolV1 | None = None,
) -> DevelopmentTwoWaveSequenceCandidateSetV1:
    """Build the fixed 37-row exact-Cat plus independent-order family."""

    build_id = _nonempty(exact_build_id, "exact_build_id")
    if (
        not isinstance(waves, tuple)
        or len(waves) != 2
        or any(not isinstance(row, SegmentWaveV1) for row in waves)
    ):
        raise TypeError("waves must contain exactly two SegmentWaveV1 rows")
    action = _validate_action(long_cooldown_action, "long_cooldown_action")
    if not isinstance(long_cooldown_triggers_gcd, bool):
        raise TypeError("long_cooldown_triggers_gcd must be boolean")
    if action in {
        *GCD_ACTIONS_BY_KEY_V1.values(),
        CLEAVE_QUEUE_V1,
        HEROIC_STRIKE_QUEUE_V1,
    }:
        raise ValueError("long_cooldown_action must be distinct from prefix and queue actions")
    resource = _nonempty(resource_id, "resource_id")
    if isinstance(proposal_guide_ids, (str, bytes)):
        raise TypeError("proposal_guide_ids must be a sequence of IDs, not text")
    guide_ids = tuple(_nonempty(row, "proposal_guide_ids entry") for row in proposal_guide_ids)
    if not guide_ids or len(guide_ids) != len(set(guide_ids)):
        raise ValueError("proposal_guide_ids must be nonempty and unique")
    if isinstance(wave_one_min_target_hp_pct, bool) or not isinstance(
        wave_one_min_target_hp_pct, (int, float)
    ) or not 0 <= float(wave_one_min_target_hp_pct) <= 100:
        raise ValueError("wave_one_min_target_hp_pct must be in [0, 100]")
    remaining_ms = _positive_int(
        wave_one_min_estimated_remaining_ms,
        "wave_one_min_estimated_remaining_ms",
    )
    resolved_seeds = seed_protocol or DevelopmentTwoWaveFreshSeedProtocolV1()

    candidates: list[DevelopmentTwoWaveSequenceCandidateV1] = [
        DevelopmentTwoWaveSequenceCandidateV1(
            candidate_index=0,
            candidate_id="development-two-wave-v1::exact-cat-zero",
            role=PAIRED_ZERO_ROLE,
        )
    ]
    wave_one, wave_two = waves
    for wave_one_order, wave_two_order in product(GCD_ORDER_KEYS_V1, repeat=2):
        ordinal = len(candidates)
        order_one_id = "-".join(wave_one_order)
        order_two_id = "-".join(wave_two_order)
        policy_id = (
            "development-two-wave-v1::"
            f"w1-{order_one_id}::w2-{order_two_id}"
        )
        proposed_steps = (
            _resource_step_v1(
                step_id="wave-one-long-cooldown-opportunity",
                wave=wave_one,
                long_cooldown_action=action,
                long_cooldown_triggers_gcd=long_cooldown_triggers_gcd,
                resource_id=resource,
                proposal_guide_ids=guide_ids,
                first_wave=True,
                wave_one_min_target_hp_pct=float(wave_one_min_target_hp_pct),
                wave_one_min_estimated_remaining_ms=remaining_ms,
            ),
            *_gcd_steps_v1(
                wave=wave_one,
                wave_label="wave-one",
                gcd_order=wave_one_order,
                queue_action=CLEAVE_QUEUE_V1,
                proposal_guide_ids=guide_ids,
            ),
            _resource_step_v1(
                step_id="wave-two-long-cooldown-retry",
                wave=wave_two,
                long_cooldown_action=action,
                long_cooldown_triggers_gcd=long_cooldown_triggers_gcd,
                resource_id=resource,
                proposal_guide_ids=guide_ids,
                first_wave=False,
                wave_one_min_target_hp_pct=float(wave_one_min_target_hp_pct),
                wave_one_min_estimated_remaining_ms=remaining_ms,
            ),
            *_gcd_steps_v1(
                wave=wave_two,
                wave_label="wave-two",
                gcd_order=wave_two_order,
                queue_action=HEROIC_STRIKE_QUEUE_V1,
                proposal_guide_ids=guide_ids,
            ),
        )
        policy = freeze_two_wave_segment_policy_v1(
            policy_id=policy_id,
            exact_build_id=build_id,
            waves=waves,
            steps=proposed_steps,
        )
        candidates.append(
            DevelopmentTwoWaveSequenceCandidateV1(
                candidate_index=ordinal,
                candidate_id=policy_id,
                role=SEARCHED_ROLE,
                wave_one_gcd_order=wave_one_order,
                wave_two_gcd_order=wave_two_order,
                policy=policy,
            )
        )

    return DevelopmentTwoWaveSequenceCandidateSetV1(
        exact_build_id=build_id,
        waves=waves,
        long_cooldown_action=action,
        long_cooldown_triggers_gcd=long_cooldown_triggers_gcd,
        resource_id=resource,
        wave_one_min_target_hp_pct=float(wave_one_min_target_hp_pct),
        wave_one_min_estimated_remaining_ms=remaining_ms,
        proposal_guide_ids=guide_ids,
        seed_protocol=resolved_seeds,
        candidates=tuple(candidates),
    )


__all__ = (
    "BLOODTHIRST_V1",
    "CLEAVE_QUEUE_V1",
    "DevelopmentTwoWaveFreshSeedProtocolV1",
    "DevelopmentTwoWaveSequenceCandidateSetV1",
    "DevelopmentTwoWaveSequenceCandidateV1",
    "FRESH_EVALUATION_SEED_START_V1",
    "FRESH_ARRIVAL_NUISANCE_SCHEDULE_MS_V1",
    "FRESH_SEED_COUNT_V1",
    "FRESH_TRAIN_SEED_START_V1",
    "GCD_ACTIONS_BY_KEY_V1",
    "GCD_ORDER_KEYS_V1",
    "HEROIC_STRIKE_QUEUE_V1",
    "PAIRED_CANDIDATE_COUNT_V1",
    "PAIRED_ZERO_ROLE",
    "SCHEMA",
    "SCOPE",
    "SEARCHED_ROLE",
    "TURTLE_SLAM_V1",
    "WHIRLWIND_V1",
    "build_development_two_wave_sequence_candidate_set_v1",
)
