"""One sparse current-observation guard around the native Cat controller.

The guard changes at most one currently available ActionPlan.  Before and
after that intervention the same Cat adapter instance remains the controller.
An abstaining guard is therefore exactly Cat, not a separately implemented
approximation of Cat.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping

from .cat_action_branch_search_v1 import (
    BRANCH_KINDS,
    apply_action_branch_v1,
    available_branches_v1,
)
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from .expert_policy import ExpertDecision, SwingQueueOp
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .sim_bridge import ActionRef, AvailableAction


POLICY_ID = "cat_sparse_guard_policy/v2"
SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2 = (
    "FULL_BASELINE_CURRENT_OBSERVATION_EXACT_ACTION_REF_LEGAL_READY_"
    "EXPECTED_GCD_LANE_PRESS_X_BRANCH_KIND_SPARSE_FEATURES_V2;"
    "INDEPENDENT_OF_MAX_STATES_AND_BRANCH_OUTCOMES"
)

_CANDIDATE_ACTION_REF_BY_BRANCH = {
    "ADD_HS_QUEUE": QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
    "BT_TO_WW": ACTION_KEY_TO_REF["warrior.whirlwind"],
    "WW_TO_BT": ACTION_KEY_TO_REF["warrior.bloodthirst"],
}

FEATURE_ORDER = (
    "hp_phase",
    "rage_band",
    "live_target_count",
    "flurry_state",
    "queued_swing_state",
    "swing_timing_band",
    "bloodthirst_cooldown_band",
    "whirlwind_cooldown_band",
    "cooldown_relation",
    "execution_phase",
    "combat_elapsed_band",
    "weapon_mode",
)

_FEATURE_VALUES = {
    "hp_phase": frozenset({"EARLY", "MIDDLE", "LATE"}),
    "rage_band": frozenset({"LOW", "MID", "HIGH"}),
    "live_target_count": frozenset({"NONE", "ONE", "TWO", "THREE_TO_FOUR", "FIVE_PLUS"}),
    "flurry_state": frozenset({"NO_TALENT", "ACTIVE", "INACTIVE"}),
    "queued_swing_state": frozenset({"KEEP", "HEROIC_STRIKE", "CLEAVE", "CANCEL"}),
    "swing_timing_band": frozenset({"IMMINENT", "WITHIN_1_5", "LATER"}),
    "bloodthirst_cooldown_band": frozenset({"READY", "WITHIN_1_5", "LATER"}),
    "whirlwind_cooldown_band": frozenset({"READY", "WITHIN_1_5", "LATER"}),
    "cooldown_relation": frozenset({"BT_FIRST", "TIED", "WW_FIRST"}),
    "execution_phase": frozenset({"SLAM_CAST", "GCD_READY", "GCD_LOCKED"}),
    "combat_elapsed_band": frozenset({"OPENING", "ESTABLISHED"}),
    "weapon_mode": frozenset({"TWO_HAND", "DUAL_WIELD"}),
}


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _state_parts(
    state: CatFuryFullPolicyStateV4 | Mapping[str, Any],
) -> tuple[Any, Any]:
    if isinstance(state, CatFuryFullPolicyStateV4):
        return state, state.combat
    if not isinstance(state, Mapping):
        raise TypeError("state must be CatFuryFullPolicyStateV4 or a complete observation mapping")
    combat = state.get("combat")
    if not isinstance(combat, Mapping):
        raise ValueError("observation mapping must contain combat")
    return state, combat


def _read(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        if name not in source:
            raise ValueError(f"observation mapping lacks {name}")
        return source[name]
    return getattr(source, name)


def _cooldown_band(value: float) -> str:
    if value <= 0.0:
        return "READY"
    if value <= 1.5:
        return "WITHIN_1_5"
    return "LATER"


def _base_action(
    base: ExpertDecision | Mapping[str, Any], kind: str,
) -> tuple[ActionRef | None, bool]:
    """Return the exact current action whose lane the branch changes."""

    candidate = _CANDIDATE_ACTION_REF_BY_BRANCH.get(kind)
    if candidate is not None:
        return candidate, kind != "ADD_HS_QUEUE"
    if isinstance(base, ExpertDecision):
        gcd = base.gcd
        queue = base.swing_queue
    elif isinstance(base, Mapping):
        raw_gcd = base.get("gcd")
        gcd = raw_gcd.get("action") if isinstance(raw_gcd, Mapping) else None
        queue = base.get("swing_queue")
    else:
        raise TypeError("base must be ExpertDecision or its wire mapping")
    if kind == "DEFER_GCD":
        return ACTION_KEY_TO_REF.get(str(gcd)), True
    if kind == "SUPPRESS_QUEUE":
        try:
            queue_op = queue if isinstance(queue, SwingQueueOp) else SwingQueueOp(str(queue))
        except ValueError:
            return None, False
        return QUEUE_REFS.get(queue_op), False
    return None, False


def _exact_legal_ready_action(
    available_actions: Any, expected: ActionRef, *, triggers_gcd: bool,
) -> bool:
    for row in available_actions:
        if isinstance(row, AvailableAction):
            action = row.action
            legal = row.legal
            ready_in_ms = row.ready_in_ms
            row_triggers_gcd = row.triggers_gcd
        elif isinstance(row, Mapping):
            raw_action = row.get("action")
            if not isinstance(raw_action, Mapping):
                continue
            fields = ("spell_id", "item_id", "other_id", "tag")
            if not all(
                type(raw_action.get(field, 0)) is int for field in fields
            ):
                continue
            action = ActionRef(**{
                field: raw_action.get(field, 0) for field in fields
            })
            legal = row.get("legal")
            ready_in_ms = row.get("ready_in_ms")
            row_triggers_gcd = row.get("triggers_gcd")
        else:
            continue
        if (
            action == expected
            and legal is True
            and type(ready_in_ms) is int and ready_in_ms == 0
            and row_triggers_gcd is triggers_gcd
        ):
            return True
    return False


def exact_current_branch_kinds_v2(
    offered_kinds: Any,
    base: ExpertDecision | Mapping[str, Any],
    available_actions: Any,
) -> tuple[str, ...]:
    """Filter broad Cat branches by the same current bridge action snapshot.

    No executor result or future suffix enters this check.  Every retained kind
    has the exact action identity, legality, zero ready time, and expected GCD
    lane needed either by the replacement/addition or by the Cat action being
    removed.
    """

    if not isinstance(offered_kinds, (tuple, list)):
        raise TypeError("offered_kinds must be a tuple or list")
    if not isinstance(available_actions, (tuple, list)):
        raise TypeError("available_actions must be a tuple or list")
    offered = frozenset(offered_kinds)
    return tuple(
        kind for kind in BRANCH_KINDS
        if kind in offered
        and (
            (required := _base_action(base, kind))[0] is not None
            and _exact_legal_ready_action(
                available_actions, required[0], triggers_gcd=required[1],
            )
        )
    )


def sparse_guard_features_v2(
    state: CatFuryFullPolicyStateV4 | Mapping[str, Any],
) -> dict[str, str]:
    """Return the canonical, current-only categorical observation features."""

    outer, combat = _state_parts(state)
    health = _finite_number(_read(combat, "target_health_pct"), "target_health_pct")
    rage = _finite_number(_read(combat, "rage"), "rage")
    enemies_raw = _read(combat, "nearby_enemies")
    if isinstance(enemies_raw, bool) or not isinstance(enemies_raw, int):
        raise TypeError("nearby_enemies must be an integer")
    enemies = enemies_raw if _read(combat, "target_exists") else 0
    swing = _finite_number(
        _read(combat, "mainhand_swing_remaining_s"), "mainhand_swing_remaining_s",
    )
    bt = _finite_number(
        _read(combat, "bloodthirst_ready_in_s"), "bloodthirst_ready_in_s",
    )
    ww = _finite_number(
        _read(combat, "whirlwind_ready_in_s"), "whirlwind_ready_in_s",
    )
    elapsed = _finite_number(_read(outer, "combat_elapsed_s"), "combat_elapsed_s")

    if health > 70.0:
        hp_phase = "EARLY"
    elif health >= 20.0:
        hp_phase = "MIDDLE"
    else:
        hp_phase = "LATE"

    if enemies <= 0:
        live_target_count = "NONE"
    elif enemies == 1:
        live_target_count = "ONE"
    elif enemies == 2:
        live_target_count = "TWO"
    elif enemies <= 4:
        live_target_count = "THREE_TO_FOUR"
    else:
        live_target_count = "FIVE_PLUS"

    flurry_talent = _read(combat, "flurry_talent")
    flurry_active = _read(combat, "flurry_active")
    if flurry_talent is not True:
        flurry_state = "NO_TALENT"
    else:
        flurry_state = "ACTIVE" if flurry_active is True else "INACTIVE"

    queued = str(_enum_value(_read(combat, "queued_swing")))
    weapon = str(_enum_value(_read(combat, "weapon_mode")))
    if queued not in _FEATURE_VALUES["queued_swing_state"]:
        raise ValueError("queued_swing is not a supported state")
    if weapon not in _FEATURE_VALUES["weapon_mode"]:
        raise ValueError("weapon_mode is not supported")

    features = {
        "hp_phase": hp_phase,
        "rage_band": "LOW" if rage < 40.0 else "MID" if rage < 70.0 else "HIGH",
        "live_target_count": live_target_count,
        "flurry_state": flurry_state,
        "queued_swing_state": queued,
        "swing_timing_band": (
            "IMMINENT" if swing <= 0.8 else "WITHIN_1_5" if swing <= 1.5 else "LATER"
        ),
        "bloodthirst_cooldown_band": _cooldown_band(bt),
        "whirlwind_cooldown_band": _cooldown_band(ww),
        "cooldown_relation": "BT_FIRST" if bt < ww else "WW_FIRST" if ww < bt else "TIED",
        "execution_phase": (
            "SLAM_CAST" if _read(combat, "casting_slam") is True
            else "GCD_READY" if _read(combat, "gcd_ready") is True
            else "GCD_LOCKED"
        ),
        "combat_elapsed_band": "OPENING" if elapsed <= 3.0 else "ESTABLISHED",
        "weapon_mode": weapon,
    }
    # Dict insertion order is part of the exported deterministic contract.
    assert tuple(features) == FEATURE_ORDER
    return features


@dataclass(frozen=True)
class SparseGuardV2:
    """A one- or two-predicate guard; ``kind=None`` is exact Cat."""

    kind: str | None = None
    first_feature: str | None = None
    first_value: str | None = None
    second_feature: str | None = None
    second_value: str | None = None

    def __post_init__(self) -> None:
        first_complete = self.first_feature is not None and self.first_value is not None
        second_complete = self.second_feature is not None and self.second_value is not None
        if (self.first_feature is None) != (self.first_value is None):
            raise ValueError("first predicate feature and value must be supplied together")
        if (self.second_feature is None) != (self.second_value is None):
            raise ValueError("second predicate feature and value must be supplied together")
        if self.kind is None:
            if first_complete or second_complete:
                raise ValueError("abstaining guard must not contain predicates")
            return
        if self.kind not in BRANCH_KINDS:
            raise ValueError("unknown ActionPlan kind")
        if not first_complete:
            raise ValueError("active guard requires at least one predicate")
        if second_complete and self.first_feature == self.second_feature:
            raise ValueError("guard predicates must use distinct features")
        for feature, value in (
            (self.first_feature, self.first_value),
            (self.second_feature, self.second_value),
        ):
            if feature is None:
                continue
            if feature not in _FEATURE_VALUES:
                raise ValueError(f"unknown sparse guard feature {feature}")
            if value not in _FEATURE_VALUES[feature]:
                raise ValueError(f"invalid value {value} for sparse guard feature {feature}")
        if second_complete and FEATURE_ORDER.index(self.first_feature) >= FEATURE_ORDER.index(self.second_feature):
            raise ValueError("guard predicates must follow canonical feature ordering")


def guard_matches_v2(
    guard: SparseGuardV2,
    state: CatFuryFullPolicyStateV4 | Mapping[str, Any],
) -> bool:
    """Match a validated guard using only the current observation."""

    if not isinstance(guard, SparseGuardV2):
        raise TypeError("guard must be SparseGuardV2")
    if guard.kind is None:
        return False
    features = sparse_guard_features_v2(state)
    if features[guard.first_feature] != guard.first_value:
        return False
    return (
        guard.second_feature is None
        or features[guard.second_feature] == guard.second_value
    )


class CatSparseGuardPolicyV2:
    """Native Cat with one latched sparse-guard ActionPlan intervention."""

    expert_id = POLICY_ID

    def __init__(self, guard: SparseGuardV2) -> None:
        if not isinstance(guard, SparseGuardV2):
            raise TypeError("guard must be SparseGuardV2")
        self.guard = guard
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.intervention_receipts: list[dict[str, Any]] = []
        self._current_available_actions: tuple[AvailableAction | Mapping[str, Any], ...] | None = None

    @property
    def intervention_latched(self) -> bool:
        return bool(self.intervention_receipts)

    def bind_current_available_actions_v2(
        self, available_actions: Any,
    ) -> None:
        """Bind exactly one current bridge action snapshot to the next proposal."""

        if not isinstance(available_actions, (tuple, list)):
            raise TypeError("available_actions must be a tuple or list")
        self._current_available_actions = tuple(available_actions)

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        if not isinstance(state, CatFuryFullPolicyStateV4):
            raise TypeError("state must be CatFuryFullPolicyStateV4")
        decision_index = self.decision_count
        self.decision_count += 1
        available_actions = self._current_available_actions
        self._current_available_actions = None
        cat_proposal = validate_source_decision_v4(self.cat.propose(state))
        kind = self.guard.kind
        if (
            self.intervention_latched
            or not guard_matches_v2(self.guard, state)
            or available_actions is None
            or kind not in exact_current_branch_kinds_v2(
                list(available_branches_v1(state, cat_proposal)),
                cat_proposal,
                list(available_actions),
            )
        ):
            return cat_proposal

        candidate_proposal = apply_action_branch_v1(state, cat_proposal, kind)
        self.intervention_receipts.append({
            "decision_index": decision_index,
            "kind": kind,
            "guard": asdict(self.guard),
            "matched_features": sparse_guard_features_v2(state),
            "policy_observation": asdict(state),
            "cat_proposal": cat_proposal.to_dict(),
            "candidate_proposal": candidate_proposal.to_dict(),
        })
        return candidate_proposal


__all__ = (
    "FEATURE_ORDER",
    "POLICY_ID",
    "SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2",
    "SparseGuardV2",
    "CatSparseGuardPolicyV2",
    "exact_current_branch_kinds_v2",
    "sparse_guard_features_v2",
    "guard_matches_v2",
)
