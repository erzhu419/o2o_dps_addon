"""Low-cardinality, prefix-causal feature view for historical Fury policy V4.

V4 is additive and does not alter the frozen V2/V3 policy artifacts.  It keeps
the strict V3 controllable-action labels, removes exact inventory from model
keys, and represents each observed state family independently so an unseen
combination falls back instead of destroying the whole context match.

The current Stage5 ``state_before`` schema does not contain rage, exact
GCD/cooldowns, auras, target health, or swing timers.  V4 therefore accepts a
small optional prefix-state envelope for future enriched inputs.  Missing
fields remain explicit and are never inferred from the chosen action.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v3 as policy_v3


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy_v4_feature_view/v1"
STATUS = "FIXTURE_VALIDATED_NOT_HPC_EXECUTED"
PREFIX_STATE_KEY = "fury_observable_state_v1"
PREFIX_STATE_SCHEMA = "chronicle_fury_observable_prefix_state/v1"
MISSING = "MISSING_NOT_OBSERVED"

ARM_A = "A_FROZEN_V2_FEATURE_VIEW"
ARM_B = "B_V4_LOW_CARD_PREFIX"
ARM_C = "C_V4_OBSERVED_DYNAMIC"
ARMS = (ARM_A, ARM_B, ARM_C)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ABLATION_PLAN = (
    PROJECT_ROOT
    / "configs"
    / "evaluation"
    / "chronicle_external_historical_fury_policy_v4_ablation.json"
)

TRACKED_COOLDOWNS = (
    "warrior.bloodrage",
    "warrior.bloodthirst",
    "warrior.death_wish",
    "warrior.pummel",
    "warrior.whirlwind",
)
TRACKED_AURAS = (
    "battle_shout",
    "death_wish",
    "flurry",
    "enrage",
)
QUEUE_ACTIONS = frozenset({"warrior.heroic_strike", "warrior.cleave"})


class HistoricalFuryPolicyV4Error(ValueError):
    """A V4 feature or fixed ablation contract is malformed."""


@dataclass(frozen=True)
class FeatureAtom:
    """One independently matched, low-cardinality context family."""

    family: str
    value: str


@dataclass(frozen=True)
class ObservedDynamicState:
    buckets: Mapping[str, str]
    atoms: tuple[FeatureAtom, ...]
    missing_fields: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class FeatureView:
    v2_contexts: Mapping[str, str]
    prefix_atoms: tuple[FeatureAtom, ...]
    dynamic: ObservedDynamicState
    provenance: Mapping[str, Any]

    def contexts_for_arm(self, arm: str) -> tuple[FeatureAtom, ...]:
        if arm == ARM_A:
            return tuple(
                FeatureAtom(f"v2.{level}", self.v2_contexts[level])
                for level in policy_v2.CONTEXT_LEVELS
            )
        if arm == ARM_B:
            return self.prefix_atoms
        if arm == ARM_C:
            return self.prefix_atoms + self.dynamic.atoms
        raise HistoricalFuryPolicyV4Error(f"unknown ablation arm: {arm}")


@dataclass(frozen=True)
class V4Decision:
    action_key: str
    action_label: str
    event_role: str
    trace_index: int
    voting_usable: bool
    feature_view: FeatureView


@dataclass
class PlayerDiagnostic:
    decisions: list[V4Decision] = field(default_factory=list)
    audit: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class PrefixQueueState:
    action_key: str
    started_at_ms: int


@dataclass
class AdditiveAggregate:
    """Counts for the fixed global -> independent-family V4 backoff."""

    decision_cells: Counter[tuple[tuple[tuple[str, str], ...], str]] = field(
        default_factory=Counter
    )
    action_counts: Counter[str] = field(default_factory=Counter)
    context_counts: dict[str, dict[str, Counter[str]]] = field(default_factory=dict)

    @property
    def decision_count(self) -> int:
        return sum(self.action_counts.values())

    def add(self, view: FeatureView, action_label: str, *, arm: str) -> None:
        contexts = view.contexts_for_arm(arm)
        self.decision_cells[
            (tuple((atom.family, atom.value) for atom in contexts), action_label)
        ] += 1
        self.action_counts[action_label] += 1
        seen_families: set[str] = set()
        for atom in contexts:
            if atom.family in seen_families:
                raise HistoricalFuryPolicyV4Error(
                    f"duplicate context family in one feature view: {atom.family}"
                )
            seen_families.add(atom.family)
            values = self.context_counts.setdefault(atom.family, {})
            values.setdefault(atom.value, Counter())[action_label] += 1

    def merge(self, other: "AdditiveAggregate") -> None:
        self.decision_cells.update(other.decision_cells)
        self.action_counts.update(other.action_counts)
        for family, values in other.context_counts.items():
            destination = self.context_counts.setdefault(family, {})
            for value, counts in values.items():
                destination.setdefault(value, Counter()).update(counts)


def _canonical_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise HistoricalFuryPolicyV4Error(
            f"feature value is not canonical JSON: {error}"
        ) from error


def _number(value: Any, label: str, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFuryPolicyV4Error(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise HistoricalFuryPolicyV4Error(f"{label} must be finite and nonnegative")
    if maximum is not None and result > maximum:
        raise HistoricalFuryPolicyV4Error(f"{label} must be <= {maximum:g}")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalFuryPolicyV4Error(f"{label} must be a nonnegative integer")
    return value


def _rage_bucket(value: float) -> str:
    if value < 10:
        return "0_9"
    if value < 15:
        return "10_14"
    if value < 20:
        return "15_19"
    if value < 25:
        return "20_24"
    if value < 30:
        return "25_29"
    if value < 40:
        return "30_39"
    return "40_100"


def _readiness_bucket(value_ms: float) -> str:
    if value_ms == 0:
        return "READY"
    if value_ms <= 500:
        return "WITHIN_500MS"
    if value_ms <= 1_500:
        return "WITHIN_1500MS"
    if value_ms <= 5_000:
        return "WITHIN_5000MS"
    return "LATER_THAN_5000MS"


def _remaining_bucket(value_ms: float) -> str:
    if value_ms == 0:
        return "ZERO"
    if value_ms <= 500:
        return "WITHIN_500MS"
    if value_ms <= 1_500:
        return "WITHIN_1500MS"
    if value_ms <= 5_000:
        return "WITHIN_5000MS"
    return "MORE_THAN_5000MS"


def _stack_bucket(value: int) -> str:
    if value == 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2_3"
    return "4_PLUS"


def _execute_bucket(health_percent: float) -> str:
    return "EXECUTE_AT_OR_BELOW_20" if health_percent <= 20 else "ABOVE_20"


def _put_observed(
    *,
    buckets: JSONMap,
    atoms: list[FeatureAtom],
    field_name: str,
    family: str,
    bucket: str,
) -> None:
    buckets[field_name] = bucket
    atoms.append(FeatureAtom(family, bucket))


def _put_missing(
    *, buckets: JSONMap, missing: list[str], field_name: str
) -> None:
    buckets[field_name] = MISSING
    missing.append(field_name)


def _all_dynamic_field_names() -> tuple[str, ...]:
    result = ["rage", "gcd_remaining_ms"]
    result.extend(f"cooldowns.{key}" for key in TRACKED_COOLDOWNS)
    for aura in TRACKED_AURAS:
        result.extend(
            (
                f"auras.{aura}.active",
                f"auras.{aura}.stacks",
                f"auras.{aura}.remaining_ms",
            )
        )
    result.extend(
        (
            "target_health_percent",
            "main_hand_swing_remaining_ms",
            "off_hand_swing_remaining_ms",
            "next_swing_queue",
        )
    )
    return tuple(result)


DYNAMIC_FIELD_NAMES = _all_dynamic_field_names()


def extract_observed_dynamic_state(
    state_before: Mapping[str, Any],
) -> ObservedDynamicState:
    """Read only an explicitly captured strict-prefix dynamic-state envelope."""

    raw = state_before.get(PREFIX_STATE_KEY)
    if raw is None:
        return ObservedDynamicState(
            buckets={field_name: MISSING for field_name in DYNAMIC_FIELD_NAMES},
            atoms=(),
            missing_fields=DYNAMIC_FIELD_NAMES,
            source="CURRENT_STAGE5_FIELD_ABSENT",
        )
    if not isinstance(raw, Mapping):
        raise HistoricalFuryPolicyV4Error(f"{PREFIX_STATE_KEY} must be an object")
    if raw.get("schema") != PREFIX_STATE_SCHEMA:
        raise HistoricalFuryPolicyV4Error(
            f"{PREFIX_STATE_KEY}.schema must be {PREFIX_STATE_SCHEMA}"
        )
    if raw.get("cutoff_exclusive_order_key") != state_before.get(
        "cutoff_exclusive_order_key"
    ):
        raise HistoricalFuryPolicyV4Error(
            "dynamic state cutoff must equal the enclosing strict-prefix cutoff"
        )

    buckets: JSONMap = {}
    atoms: list[FeatureAtom] = []
    missing: list[str] = []

    if "rage" in raw:
        _put_observed(
            buckets=buckets,
            atoms=atoms,
            field_name="rage",
            family="dynamic.rage_bucket",
            bucket=_rage_bucket(_number(raw["rage"], "rage", maximum=100)),
        )
    else:
        _put_missing(buckets=buckets, missing=missing, field_name="rage")

    if "gcd_remaining_ms" in raw:
        _put_observed(
            buckets=buckets,
            atoms=atoms,
            field_name="gcd_remaining_ms",
            family="dynamic.gcd_readiness",
            bucket=_readiness_bucket(
                _number(raw["gcd_remaining_ms"], "gcd_remaining_ms")
            ),
        )
    else:
        _put_missing(
            buckets=buckets, missing=missing, field_name="gcd_remaining_ms"
        )

    cooldowns = raw.get("cooldowns")
    if cooldowns is not None and not isinstance(cooldowns, Mapping):
        raise HistoricalFuryPolicyV4Error("cooldowns must be an object")
    for action_key in TRACKED_COOLDOWNS:
        field_name = f"cooldowns.{action_key}"
        if isinstance(cooldowns, Mapping) and action_key in cooldowns:
            _put_observed(
                buckets=buckets,
                atoms=atoms,
                field_name=field_name,
                family=f"dynamic.cooldown_readiness.{action_key}",
                bucket=_readiness_bucket(
                    _number(cooldowns[action_key], field_name)
                ),
            )
        else:
            _put_missing(buckets=buckets, missing=missing, field_name=field_name)

    auras = raw.get("auras")
    if auras is not None and not isinstance(auras, Mapping):
        raise HistoricalFuryPolicyV4Error("auras must be an object")
    for aura_name in TRACKED_AURAS:
        aura = auras.get(aura_name) if isinstance(auras, Mapping) else None
        if aura is not None and not isinstance(aura, Mapping):
            raise HistoricalFuryPolicyV4Error(
                f"auras.{aura_name} must be an object"
            )
        active_field = f"auras.{aura_name}.active"
        stacks_field = f"auras.{aura_name}.stacks"
        remaining_field = f"auras.{aura_name}.remaining_ms"
        if isinstance(aura, Mapping) and "active" in aura:
            active = aura["active"]
            if not isinstance(active, bool):
                raise HistoricalFuryPolicyV4Error(f"{active_field} must be boolean")
            _put_observed(
                buckets=buckets,
                atoms=atoms,
                field_name=active_field,
                family=f"dynamic.aura_active.{aura_name}",
                bucket="ACTIVE" if active else "INACTIVE",
            )
        else:
            _put_missing(buckets=buckets, missing=missing, field_name=active_field)
        if isinstance(aura, Mapping) and "stacks" in aura:
            _put_observed(
                buckets=buckets,
                atoms=atoms,
                field_name=stacks_field,
                family=f"dynamic.aura_stacks.{aura_name}",
                bucket=_stack_bucket(_integer(aura["stacks"], stacks_field)),
            )
        else:
            _put_missing(buckets=buckets, missing=missing, field_name=stacks_field)
        if isinstance(aura, Mapping) and "remaining_ms" in aura:
            _put_observed(
                buckets=buckets,
                atoms=atoms,
                field_name=remaining_field,
                family=f"dynamic.aura_remaining.{aura_name}",
                bucket=_remaining_bucket(
                    _number(aura["remaining_ms"], remaining_field)
                ),
            )
        else:
            _put_missing(
                buckets=buckets, missing=missing, field_name=remaining_field
            )

    if "target_health_percent" in raw:
        _put_observed(
            buckets=buckets,
            atoms=atoms,
            field_name="target_health_percent",
            family="dynamic.execute_phase",
            bucket=_execute_bucket(
                _number(
                    raw["target_health_percent"],
                    "target_health_percent",
                    maximum=100,
                )
            ),
        )
    else:
        _put_missing(
            buckets=buckets, missing=missing, field_name="target_health_percent"
        )

    for field_name, family in (
        ("main_hand_swing_remaining_ms", "dynamic.main_hand_swing_readiness"),
        ("off_hand_swing_remaining_ms", "dynamic.off_hand_swing_readiness"),
    ):
        if field_name in raw:
            _put_observed(
                buckets=buckets,
                atoms=atoms,
                field_name=field_name,
                family=family,
                bucket=_readiness_bucket(_number(raw[field_name], field_name)),
            )
        else:
            _put_missing(buckets=buckets, missing=missing, field_name=field_name)

    if "next_swing_queue" in raw:
        queue = raw["next_swing_queue"]
        if queue not in {"NONE", *QUEUE_ACTIONS}:
            raise HistoricalFuryPolicyV4Error(
                "next_swing_queue must be NONE, warrior.heroic_strike, or warrior.cleave"
            )
        _put_observed(
            buckets=buckets,
            atoms=atoms,
            field_name="next_swing_queue",
            family="dynamic.next_swing_queue",
            bucket=str(queue),
        )
    else:
        _put_missing(
            buckets=buckets, missing=missing, field_name="next_swing_queue"
        )

    return ObservedDynamicState(
        buckets=buckets,
        atoms=tuple(atoms),
        missing_fields=tuple(missing),
        source="EXPLICIT_STRICT_PREFIX_ENVELOPE",
    )


def _prefix_queue_atoms(
    queue_state: PrefixQueueState | None, *, current_timestamp_ms: int
) -> tuple[FeatureAtom, ...]:
    if queue_state is None:
        return (
            FeatureAtom(
                "queue.prefix_observation", "NO_PENDING_QUEUE_START_OBSERVED"
            ),
        )
    return (
        FeatureAtom("queue.prefix_observation", queue_state.action_key),
        FeatureAtom(
            "queue.prefix_pending_age",
            policy_v3._age_bucket(current_timestamp_ms, queue_state.started_at_ms),
        ),
    )


def build_feature_view(
    *,
    state: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    focal_guid: str,
    current_timestamp_ms: int,
    history: Sequence[policy_v3.PrefixAction],
    queue_state: PrefixQueueState | None,
    inventory: policy_v3.CharacterInventorySnapshot | None,
    inventory_join_outcome: str,
) -> FeatureView:
    """Build an A/B/C-compatible view using only state before the label."""

    v2_contexts = policy_v2._contexts_from_prefix(
        state=state, trace=trace, focal_guid=focal_guid
    )
    coarse = json.loads(v2_contexts["coarse"])
    tactical = json.loads(v2_contexts["tactical"])
    last = history[-1] if history else None
    last_gcd = next((row for row in reversed(history) if row.lane == "gcd"), None)
    stance = next(
        (
            row.action_key.removeprefix("warrior.")
            for row in reversed(history)
            if row.action_key
            in {
                "warrior.battle_stance",
                "warrior.defensive_stance",
                "warrior.berserker_stance",
            }
        ),
        "NOT_OBSERVED_IN_PREFIX",
    )
    prefix_atoms = (
        FeatureAtom("base.wave_coarse", _canonical_text(coarse)),
        FeatureAtom(
            "team.background_dps_bucket", str(tactical["background_dps_bucket"])
        ),
        FeatureAtom(
            "target.has_last_observed_target",
            "YES" if tactical["actor_has_last_target"] else "NO",
        ),
        FeatureAtom(
            "sequence.last_controllable_action",
            last.action_key if last is not None else "NONE",
        ),
        FeatureAtom(
            "sequence.last_controllable_lane",
            last.lane if last is not None else "NONE",
        ),
        FeatureAtom(
            "sequence.last_gcd_action_age",
            policy_v3._age_bucket(
                current_timestamp_ms,
                last_gcd.timestamp_ms if last_gcd is not None else None,
            ),
        ),
        FeatureAtom("sequence.observed_stance", stance),
        *_prefix_queue_atoms(
            queue_state, current_timestamp_ms=current_timestamp_ms
        ),
    )
    dynamic = extract_observed_dynamic_state(state)
    provenance = {
        "inventory_join_outcome": inventory_join_outcome,
        "exact_inventory": inventory.feature_profile() if inventory else None,
        "exact_inventory_used_as_model_context": False,
        "v2_full_key_used_only_by_arm_a": True,
        "dynamic_state_source": dynamic.source,
    }
    return FeatureView(
        v2_contexts=v2_contexts,
        prefix_atoms=prefix_atoms,
        dynamic=dynamic,
        provenance=provenance,
    )


def process_player_transitions(
    *,
    player: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    instance_ref: str,
    encounter_id: str,
    inventory_index: policy_v3.CharacterInventoryIndex | None = None,
) -> PlayerDiagnostic:
    """Retain V3 labels while constructing V4 state before each decision."""

    metadata = policy_v2._mapping(player.get("player"), "player metadata")
    focal_guid = policy_v2._text(metadata.get("guid"), "player guid")
    result = PlayerDiagnostic()
    history: list[policy_v3.PrefixAction] = []
    pending_starts: dict[tuple[str, str], int] = {}
    queue_state: PrefixQueueState | None = None
    prior_order: tuple[int, ...] | None = None
    transitions = policy_v2._array(
        player.get("prefix_transitions"), "prefix_transitions"
    )
    for raw_transition in transitions:
        transition = policy_v2._mapping(raw_transition, "prefix transition")
        trace_index, order, state, label = policy_v3._validate_transition(
            transition,
            trace=trace,
            focal_guid=focal_guid,
            prior_order=prior_order,
        )
        prior_order = order
        result.audit["transitions_inspected"] += 1
        event_type = policy_v2._text(label.get("event_type"), "event_type")
        spell = policy_v2._mapping(label.get("spell"), "spell")
        classification = policy_v3.classify_action_event(
            event_type=event_type, spell=spell
        )
        if (
            label.get("attribution_kind") != "DIRECT_FRIENDLY_PLAYER"
            or label.get("source_lane") != "FRIENDLY_PLAYER"
        ):
            result.audit[f"classified:{classification.role}"] += 1
            result.audit["non_direct_player_event_excluded"] += 1
            continue

        timestamp_ms = order[1]
        pending_starts = {
            key: start
            for key, start in pending_starts.items()
            if 0 <= timestamp_ms - start <= policy_v2.START_GO_PAIR_MAX_MS
        }
        identity = (
            policy_v3._action_identity(classification.action_key, label)
            if classification.action_key is not None
            else None
        )
        if event_type == "FAIL":
            result.audit[f"classified:{classification.role}"] += 1
            if identity is not None:
                pending_starts.pop(identity, None)
            if (
                queue_state is not None
                and classification.action_key == queue_state.action_key
            ):
                queue_state = None
            result.audit["failed_attempts_excluded"] += 1
            continue
        if event_type == "GO" and identity is not None and identity in pending_starts:
            pending_starts.pop(identity)
            if (
                queue_state is not None
                and classification.action_key == queue_state.action_key
            ):
                queue_state = None
            result.audit[f"classified:{policy_v3.ROLE_PAIRED_RESULT}"] += 1
            result.audit["paired_go_deduplicated"] += 1
            continue
        if classification.role not in {
            policy_v3.ROLE_CONTROLLABLE_START,
            policy_v3.ROLE_CONTROLLABLE_INSTANT_GO,
        }:
            result.audit[f"classified:{classification.role}"] += 1
            if (
                event_type == "GO"
                and queue_state is not None
                and classification.action_key == queue_state.action_key
            ):
                queue_state = None
            result.audit["nondecision_events_excluded"] += 1
            continue

        assert classification.action_key is not None
        assert classification.lane is not None
        result.audit[f"classified:{classification.role}"] += 1
        event_index = order[2]
        inventory: policy_v3.CharacterInventorySnapshot | None = None
        join_outcome = "inventory_index_unavailable"
        if inventory_index is not None:
            inventory, join_outcome = inventory_index.select(
                instance_ref=instance_ref,
                encounter_id=encounter_id,
                player_guid=focal_guid,
                event_index=event_index,
            )
        view = build_feature_view(
            state=state,
            trace=trace,
            focal_guid=focal_guid,
            current_timestamp_ms=timestamp_ms,
            history=history,
            queue_state=queue_state,
            inventory=inventory,
            inventory_join_outcome=join_outcome,
        )
        target_role, usable = policy_v2._target_role(
            label=label, state=state, focal_guid=focal_guid
        )
        action_label = _canonical_text(
            {
                "action_key": classification.action_key,
                "target_role": target_role,
            }
        )
        result.decisions.append(
            V4Decision(
                action_key=classification.action_key,
                action_label=action_label,
                event_role=classification.role,
                trace_index=trace_index,
                voting_usable=usable,
                feature_view=view,
            )
        )
        if event_type == "START":
            assert identity is not None
            pending_starts[identity] = timestamp_ms
        history.append(
            policy_v3.PrefixAction(
                action_key=classification.action_key,
                lane=classification.lane,
                timestamp_ms=timestamp_ms,
                trace_index=trace_index,
            )
        )
        if classification.action_key in QUEUE_ACTIONS:
            queue_state = PrefixQueueState(
                action_key=classification.action_key,
                started_at_ms=timestamp_ms,
            )
        result.audit["controllable_decisions_retained"] += 1
        if usable:
            result.audit["accepted_voting_labels"] += 1
        else:
            result.audit["unsupported_nonvoting_targets_excluded"] += 1
    return result


def distribution(
    aggregate: AdditiveAggregate,
    view: FeatureView,
    *,
    arm: str,
    alpha: float = policy_v2.DEFAULT_SMOOTHING_ALPHA,
    strength: float = policy_v2.DEFAULT_BACKOFF_STRENGTH,
) -> tuple[dict[str, float], tuple[str, ...], float]:
    """Apply fixed ordered backoff; unmatched atoms leave the prior unchanged."""

    return distribution_from_atoms(
        aggregate,
        view.contexts_for_arm(arm),
        alpha=alpha,
        strength=strength,
    )


def distribution_from_atoms(
    aggregate: AdditiveAggregate,
    atoms: Sequence[FeatureAtom],
    *,
    alpha: float = policy_v2.DEFAULT_SMOOTHING_ALPHA,
    strength: float = policy_v2.DEFAULT_BACKOFF_STRENGTH,
) -> tuple[dict[str, float], tuple[str, ...], float]:
    """Evaluate a persisted decision cell without reconstructing its view."""

    if not math.isfinite(alpha) or alpha <= 0:
        raise HistoricalFuryPolicyV4Error("alpha must be positive and finite")
    if not math.isfinite(strength) or strength <= 0:
        raise HistoricalFuryPolicyV4Error("strength must be positive and finite")
    catalog = sorted(aggregate.action_counts)
    total = sum(aggregate.action_counts.values())
    denominator = total + alpha * (len(catalog) + 1)
    if denominator <= 0:
        return {}, (), 1.0
    probabilities = {
        action: (aggregate.action_counts[action] + alpha) / denominator
        for action in catalog
    }
    unknown_probability = alpha / denominator
    matched: list[str] = []
    for atom in atoms:
        context_counts = aggregate.context_counts.get(atom.family, {}).get(
            atom.value
        )
        if not context_counts:
            continue
        matched.append(atom.family)
        context_total = sum(context_counts.values())
        posterior_denominator = context_total + strength
        probabilities = {
            action: (
                context_counts.get(action, 0) + strength * probabilities[action]
            )
            / posterior_denominator
            for action in catalog
        }
        unknown_probability = (
            strength * unknown_probability / posterior_denominator
        )
    return probabilities, tuple(matched), unknown_probability


def summarize_feature_availability(
    decisions: Sequence[V4Decision],
) -> JSONMap:
    """Small diagnostic; it does not assert coverage beyond supplied rows."""

    observed: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for decision in decisions:
        for field_name, value in decision.feature_view.dynamic.buckets.items():
            (missing if value == MISSING else observed)[field_name] += 1
    return {
        "schema": SCHEMA + "/availability",
        "decision_count": len(decisions),
        "observed_count_by_field": dict(sorted(observed.items())),
        "missing_count_by_field": dict(sorted(missing.items())),
        "missing_values_imputed": False,
    }


def load_fixed_ablation_plan(
    path: Path = DEFAULT_ABLATION_PLAN,
) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalFuryPolicyV4Error(
            f"cannot read fixed V4 ablation plan {path}: {error}"
        ) from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA + "/ablation_plan"
    ):
        raise HistoricalFuryPolicyV4Error("unexpected V4 ablation plan schema")
    variants = value.get("variants")
    if not isinstance(variants, list) or [
        row.get("id") for row in variants if isinstance(row, dict)
    ] != list(ARMS):
        raise HistoricalFuryPolicyV4Error("V4 ablation plan must contain fixed A/B/C arms")
    if value.get("hyperparameter_search") is not False:
        raise HistoricalFuryPolicyV4Error("V4 ablation plan must forbid hyperparameter search")
    return value
