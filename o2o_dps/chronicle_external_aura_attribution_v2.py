"""Attribute Chronicle physical-armor aura actions by their exact DBC effect lane.

This additive Stage-7 v2 diagnostic corrects a material ambiguity in v1:
``AURA_CAST`` contains one row per spell effect, so a single Curse of
Recklessness cast can produce three rows with the same caster, target and time.
Only the pinned physical-resistance effect is an armor candidate:

``EffectApplyAura(6), AuraEffectModResistance(22), physical mask(1)``.

The spell ID and effect tuple must both match the fixed contract below.  The
scanner still fails closed on zero or multiple physical-effect candidates.
Unmatched Sunder casts observed while the factual stack count is already five
are retained as refresh *candidates* with factual ``stack_delta=0``; they never
add a sixth factual stack.  ``AuraCast`` has no result field, so such a cast is
not declared counterfactually executable: after removing an earlier focal
Sunder it could have become a stack increase, and the factual stream cannot say
whether it landed.  Bonereaver's Edge remains a player self-buff/target-
resistance-ignore observation and is never projected as a hostile flat armor
reduction.

This module produces attribution and executability diagnostics only.  It does
not authorize policy, comparison, training, or dynamic-armor use.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import chronicle_external_aura_attribution_v1 as v1
from . import chronicle_external_state_event_normalizer_v1 as state_v1


SCHEMA = "chronicle_external_aura_attribution/v2"
EVENT_SCHEMA = "chronicle_external_aura_attribution_event/v2"
STATUS = "STAGE7_V2_PHYSICAL_EFFECT_ATTRIBUTION_DIAGNOSTIC_NONVOTING"
IMPLEMENTATION_REVISION = "fixed_rank_spell_effect_lane_factual_loo_layered_v5"
DEFAULT_JOIN_WINDOW_MS = 100

# Chronicle's pinned source maps these integer fields directly from Turtle's
# Spell DBC.  At commit 004acd948773b7c914bb358ce09e3bc759650883:
#   chrondbc.EffectApplyAura == 6
#   chrondbc.AuraEffectModResistance == 22
# EffectMiscValue is a school mask; bit 0 (value 1) is physical/armor.
PHYSICAL_ARMOR_EFFECT_TUPLE = (6, 22, 1)
BONEREAVER_SELF_IGNORE_EFFECT_TUPLE = (6, 123, 1)
SUNDER_MAX_STACKS = 5

# A name match is not sufficient.  The spell ID must be a pinned Turtle rank
# and its Chronicle AURA_CAST row must also carry the physical-armor effect
# tuple.  The complete rank families come from the repository's frozen Turtle
# spell catalogue; the Chronicle proto/source defines the three tuple fields.
HOSTILE_ARMOR_SPELL_IDS: dict[str, tuple[int, ...]] = {
    "annihilator": (16928,),
    "crystal_yield": (15235,),
    "curse_of_recklessness": (704, 7658, 7659, 11717),
    "expose_armor": (8647, 8649, 8650, 11197, 11198),
    "faerie_fire": (770, 778, 9749, 9907),
    "faerie_fire_feral": (16857, 17390, 17391, 17392),
    "sunder_armor": (7386, 7405, 8380, 11596, 11597),
}
HOSTILE_ARMOR_SPELL_EFFECT_CONTRACTS: dict[tuple[str, int], tuple[int, int, int]] = {
    (debuff_id, spell_id): PHYSICAL_ARMOR_EFFECT_TUPLE
    for debuff_id, spell_ids in HOSTILE_ARMOR_SPELL_IDS.items()
    for spell_id in spell_ids
}

NONSTACKING_DEBUFFS = frozenset(
    {
        "crystal_yield",
        "curse_of_recklessness",
        "expose_armor",
        "faerie_fire",
        "faerie_fire_feral",
    }
)


class ChronicleExternalAuraAttributionV2Error(RuntimeError):
    """The physical-effect attribution contract cannot be applied exactly."""


@dataclass
class _CastCandidate:
    key: tuple[str, str, str, str, int]
    order_key: tuple[int, int, int, int, int]
    timestamp_ms: int
    source_guid: str | None
    anchor: Mapping[str, Any]
    duration_ms: int
    cap_status: int
    amount_before_cast: int
    effect_tuple: tuple[int, int, int]
    matched_addition: bool = False
    matched_current_amount: int | None = None
    matched_prior_amount: int | None = None
    matched_aura_anchor: Mapping[str, Any] | None = None


@dataclass
class _Lifecycle:
    current_amount: int = 0
    saw_unattributed_transition: bool = False
    saw_nonmonotonic_transition: bool = False
    active_sources: list[str | None] = field(default_factory=list)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalAuraAttributionV2Error(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalAuraAttributionV2Error(f"{label} must be nonempty text")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalAuraAttributionV2Error(f"{label} must be an integer")
    return value


def _identity(row: Mapping[str, Any]) -> tuple[str, int] | None:
    return v1._identity(row)


def _state_name(row: Mapping[str, Any]) -> str:
    return v1._state_name(row)


def _is_buff(row: Mapping[str, Any]) -> bool:
    return v1._is_buff(row)


def _order_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    try:
        return v1._order_key(row)
    except v1.ChronicleExternalAuraAttributionError as error:
        raise ChronicleExternalAuraAttributionV2Error(str(error)) from error


def _anchor(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return v1._anchor(row)
    except v1.ChronicleExternalAuraAttributionError as error:
        raise ChronicleExternalAuraAttributionV2Error(str(error)) from error


def _effect_tuple(row: Mapping[str, Any]) -> tuple[int, int, int]:
    payload = _mapping(row.get("state_payload"), "state_payload")
    return (
        _integer(payload.get("effect"), "state_payload.effect"),
        _integer(payload.get("effect_aura_name"), "state_payload.effect_aura_name"),
        _integer(payload.get("effect_misc_value"), "state_payload.effect_misc_value"),
    )


def _join_key(
    row: Mapping[str, Any], identity: tuple[str, int]
) -> tuple[str, str, str, str, int]:
    return (
        _text(row.get("instance"), "instance"),
        _text(row.get("encounter"), "encounter"),
        _text(row.get("target_guid"), "target_guid"),
        identity[0],
        identity[1],
    )


def _within_window(
    candidates: deque[_CastCandidate],
    order: tuple[int, int, int, int, int],
    window_ms: int,
) -> list[_CastCandidate]:
    while candidates and order[0] - candidates[0].timestamp_ms > window_ms:
        candidates.popleft()
    return [
        candidate
        for candidate in candidates
        if candidate.order_key < order
        and 0 <= order[0] - candidate.timestamp_ms <= window_ms
    ]


class AuraAttributionScannerV2:
    """Streaming fixed-effect AuraCast/Aura join with refresh accounting."""

    def __init__(self, *, join_window_ms: int = DEFAULT_JOIN_WINDOW_MS) -> None:
        if isinstance(join_window_ms, bool) or not isinstance(join_window_ms, int):
            raise TypeError("join_window_ms must be an integer")
        if join_window_ms < 1:
            raise ChronicleExternalAuraAttributionV2Error(
                "join_window_ms must be positive"
            )
        self.join_window_ms = join_window_ms
        self._scope: tuple[str, str] | None = None
        self._closed_scopes: set[tuple[str, str]] = set()
        self._last_order: tuple[int, int, int, int, int] | None = None
        self._legacy_casts: dict[
            tuple[str, str, str, str, int], deque[_CastCandidate]
        ] = defaultdict(deque)
        self._contract_casts: dict[
            tuple[str, str, str, str, int], deque[_CastCandidate]
        ] = defaultdict(deque)
        self._lifecycles: dict[
            tuple[str, str, str, str, int], _Lifecycle
        ] = {}
        self._all_contract_casts: list[_CastCandidate] = []
        self._counts: Counter[str] = Counter()
        self._by_debuff: Counter[tuple[str, str]] = Counter()
        self._observed_tuples: Counter[tuple[str, int, int, int, int]] = Counter()

    def _enter_scope(self, row: Mapping[str, Any]) -> None:
        scope = (
            _text(row.get("instance"), "instance"),
            _text(row.get("encounter"), "encounter"),
        )
        if scope == self._scope:
            return
        if scope in self._closed_scopes:
            raise ChronicleExternalAuraAttributionV2Error(
                "an encounter reappeared after its streaming scope closed"
            )
        if self._scope is not None:
            self._record_closed_scope_censoring()
            self._closed_scopes.add(self._scope)
        self._scope = scope
        self._last_order = None
        self._legacy_casts.clear()
        self._contract_casts.clear()
        self._lifecycles.clear()

    def _record_closed_scope_censoring(self) -> None:
        active = [row for row in self._lifecycles.values() if row.current_amount > 0]
        self._counts["right_censored_active_lifecycle_at_scope_close"] += len(active)
        self._counts["right_censored_active_stack_at_scope_close"] += sum(
            row.current_amount for row in active
        )

    def consume(self, raw_row: Mapping[str, Any]) -> dict[str, Any] | None:
        row = _mapping(raw_row, "state event row")
        stream = row.get("stream_type")
        if stream not in {"aura", "aura_cast"}:
            return None
        identity = _identity(row)
        if identity is None:
            return None
        self._enter_scope(row)
        order = _order_key(row)
        if self._last_order is not None and order <= self._last_order:
            raise ChronicleExternalAuraAttributionV2Error(
                "aura sidecar rows are not in strict EventMeta order"
            )
        self._last_order = order
        target = _optional_text(row.get("target_guid"))
        if target is None:
            self._counts["registered_event_missing_target_rejected"] += 1
            self._by_debuff[(identity[0], "missing_target_rejected")] += 1
            return None
        key = _join_key(row, identity)
        if stream == "aura_cast":
            self._consume_cast(row, identity, key, order)
            return None
        return self._consume_aura(row, identity, key, order)

    def _consume_cast(
        self,
        row: Mapping[str, Any],
        identity: tuple[str, int],
        key: tuple[str, str, str, str, int],
        order: tuple[int, int, int, int, int],
    ) -> None:
        effect_tuple = _effect_tuple(row)
        self._observed_tuples[
            (identity[0], identity[1], *effect_tuple)
        ] += 1
        lifecycle = self._lifecycles.setdefault(key, _Lifecycle())
        payload = _mapping(row.get("state_payload"), "state_payload")
        candidate = _CastCandidate(
            key=key,
            order_key=order,
            timestamp_ms=order[0],
            source_guid=_optional_text(row.get("source_guid")),
            anchor=_anchor(row),
            duration_ms=_integer(payload.get("duration_ms"), "duration_ms"),
            cap_status=_integer(payload.get("cap_status"), "cap_status"),
            amount_before_cast=lifecycle.current_amount,
            effect_tuple=effect_tuple,
        )
        self._legacy_casts[key].append(candidate)
        self._counts["registered_aura_cast_seen"] += 1
        self._by_debuff[(identity[0], "registered_aura_cast_seen")] += 1

        if identity == ("bonereavers_edge", v1.BONEREAVER_SPELL_ID):
            if effect_tuple == BONEREAVER_SELF_IGNORE_EFFECT_TUPLE:
                self._counts["bonereaver_self_ignore_effect_seen"] += 1
            else:
                self._counts["bonereaver_effect_tuple_conflict"] += 1
            return

        expected = HOSTILE_ARMOR_SPELL_EFFECT_CONTRACTS.get(identity)
        if expected is None:
            self._counts["unsupported_spell_id_rejected"] += 1
            self._by_debuff[(identity[0], "unsupported_spell_id_rejected")] += 1
            return
        if effect_tuple != expected:
            self._counts["nonarmor_or_conflicting_effect_lane_rejected"] += 1
            self._by_debuff[
                (identity[0], "nonarmor_or_conflicting_effect_lane_rejected")
            ] += 1
            return
        self._contract_casts[key].append(candidate)
        self._all_contract_casts.append(candidate)
        self._counts["physical_armor_effect_cast_admitted"] += 1
        self._by_debuff[(identity[0], "physical_armor_effect_cast_admitted")] += 1

    def _consume_aura(
        self,
        row: Mapping[str, Any],
        identity: tuple[str, int],
        key: tuple[str, str, str, str, int],
        order: tuple[int, int, int, int, int],
    ) -> dict[str, Any]:
        state = _state_name(row)
        current = _integer(row.get("value"), "aura current_amount")
        if current < 0:
            raise ChronicleExternalAuraAttributionV2Error(
                "aura current_amount cannot be negative"
            )
        lifecycle = self._lifecycles.setdefault(key, _Lifecycle())
        prior = lifecycle.current_amount
        is_buff = _is_buff(row)
        self._counts["registered_aura_state_seen"] += 1
        self._by_debuff[(identity[0], f"state::{state}")] += 1
        if is_buff:
            self._counts["registered_aura_state_marked_buff"] += 1
            self._by_debuff[(identity[0], "state_marked_buff")] += 1

        attribution_status = "NONADDITION_STATE_CASTER_NOT_ATTRIBUTED"
        source_guid: str | None = None
        chosen: _CastCandidate | None = None
        legacy_candidates: list[_CastCandidate] = []
        contract_candidates: list[_CastCandidate] = []
        closed_exact_source_counts: dict[str, int] | None = None
        closed_unresolved_stack_count: int | None = None
        if state == "StateAdded":
            legacy_candidates = _within_window(
                self._legacy_casts[key], order, self.join_window_ms
            )
            contract_candidates = _within_window(
                self._contract_casts[key], order, self.join_window_ms
            )
            self._record_candidate_decomposition(
                identity[0], legacy_candidates, contract_candidates
            )
            if len(contract_candidates) == 1 and contract_candidates[0].source_guid:
                chosen = contract_candidates[0]
                source_guid = chosen.source_guid
                chosen.matched_addition = True
                chosen.matched_prior_amount = prior
                chosen.matched_current_amount = current
                chosen.matched_aura_anchor = _anchor(row)
                self._contract_casts[key].remove(chosen)
                if len(legacy_candidates) == 1:
                    self._legacy_casts[key].remove(legacy_candidates[0])
                attribution_status = "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED"
                self._counts["state_added_attributed_unique_physical_effect"] += 1
                self._by_debuff[
                    (identity[0], "state_added_attributed_unique_physical_effect")
                ] += 1
            elif len(contract_candidates) == 1:
                attribution_status = "UNIQUE_PHYSICAL_EFFECT_CAST_MISSING_CASTER"
                self._counts["state_added_missing_caster_rejected"] += 1
                self._by_debuff[
                    (identity[0], "state_added_missing_caster_rejected")
                ] += 1
            elif not contract_candidates:
                attribution_status = "NO_PHYSICAL_EFFECT_CAST_WITHIN_WINDOW"
                self._counts["state_added_no_physical_effect_candidate"] += 1
                self._by_debuff[
                    (identity[0], "state_added_no_physical_effect_candidate")
                ] += 1
            else:
                attribution_status = "MULTIPLE_PHYSICAL_EFFECT_CASTS_REJECTED"
                self._counts["state_added_multiple_physical_effect_rejected"] += 1
                self._by_debuff[
                    (identity[0], "state_added_multiple_physical_effect_rejected")
                ] += 1
            self._apply_added(
                lifecycle,
                prior=prior,
                current=current,
                source_guid=source_guid,
            )
        elif state == "StateRemoved":
            attribution_status = "STATE_REMOVED_CASTER_NOT_IN_AURA_PROTO"
            closed_exact_source_counts = dict(
                sorted(
                    Counter(
                        source
                        for source in lifecycle.active_sources
                        if source is not None
                    ).items()
                )
            )
            closed_unresolved_stack_count = sum(
                source is None for source in lifecycle.active_sources
            )
            lifecycle.current_amount = 0
            lifecycle.active_sources.clear()
            self._counts["state_removed_no_caster_guess"] += 1
        elif state in {"StateModified", "StateUnknown"}:
            lifecycle.current_amount = current
            lifecycle.active_sources = [None] * current
            lifecycle.saw_unattributed_transition = True
            self._counts["nonaddition_state_unattributed"] += 1
        else:
            raise ChronicleExternalAuraAttributionV2Error(
                f"unsupported AuraState name {state!r}"
            )

        role = "REGISTERED_HOSTILE_ARMOR_AURA_DIAGNOSTIC"
        if identity == ("bonereavers_edge", v1.BONEREAVER_SPELL_ID):
            role = "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE"
        effective_current = 0 if state == "StateRemoved" else current
        return {
            "schema": EVENT_SCHEMA,
            "status": STATUS,
            "instance": key[0],
            "encounter": key[1],
            "target_guid": key[2],
            "debuff_id": identity[0],
            "spell_id": identity[1],
            "spell": row.get("spell"),
            "aura_state": state,
            "current_amount": current,
            "is_buff": is_buff,
            "aura_role": role,
            "aura_anchor": _anchor(row),
            "factual_transition": {
                "prior_amount": prior,
                # AuraState is authoritative for lifecycle closure.  Chronicle
                # can retain the removed stack count in current_amount on a
                # StateRemoved record; treating that payload as post-event
                # state leaves a phantom active stack.
                "reported_current_amount": current,
                "current_amount": effective_current,
                "stack_delta": effective_current - prior,
                "unique_physical_cast_supported": chosen is not None,
            },
            "attribution": {
                "status": attribution_status,
                "source_guid": source_guid,
                "legacy_candidate_row_count": len(legacy_candidates),
                "physical_effect_candidate_row_count": len(contract_candidates),
                "physical_effect_candidate_source_guids": sorted(
                    {
                        candidate.source_guid
                        for candidate in contract_candidates
                        if candidate.source_guid is not None
                    }
                ),
                "matched_aura_cast_anchor": chosen.anchor if chosen else None,
                "owner_or_controller_resolved": False,
            },
            "lifecycle": {
                "prior_amount": prior,
                "current_amount": lifecycle.current_amount,
                "active_exact_source_counts": dict(
                    sorted(
                        Counter(
                            source
                            for source in lifecycle.active_sources
                            if source is not None
                        ).items()
                    )
                ),
                "active_unresolved_stack_count": sum(
                    source is None for source in lifecycle.active_sources
                ),
                "saw_unattributed_transition": lifecycle.saw_unattributed_transition,
                "saw_nonmonotonic_transition": lifecycle.saw_nonmonotonic_transition,
                "closed_exact_source_counts": closed_exact_source_counts,
                "closed_unresolved_stack_count": closed_unresolved_stack_count,
            },
            "projection_contract": {
                "focal_guid_loo_ready": False,
                "hostile_flat_armor_reduction_authorized": False,
                "bonereaver_hostile_reduction_forbidden": (
                    identity == ("bonereavers_edge", v1.BONEREAVER_SPELL_ID)
                ),
            },
        }

    def _record_candidate_decomposition(
        self,
        debuff_id: str,
        legacy: list[_CastCandidate],
        contract: list[_CastCandidate],
    ) -> None:
        old = "zero" if not legacy else "unique" if len(legacy) == 1 else "ambiguous"
        new = (
            "zero"
            if not contract
            else "unique"
            if len(contract) == 1
            else "ambiguous"
        )
        metric = f"candidate_decomposition::{old}_to_{new}"
        self._counts[metric] += 1
        self._by_debuff[(debuff_id, metric)] += 1

    @staticmethod
    def _apply_added(
        lifecycle: _Lifecycle,
        *,
        prior: int,
        current: int,
        source_guid: str | None,
    ) -> None:
        if current <= 0 or current < prior:
            lifecycle.current_amount = max(current, 0)
            lifecycle.active_sources = [None] * max(current, 0)
            lifecycle.saw_nonmonotonic_transition = True
            lifecycle.saw_unattributed_transition = True
            return
        delta = current - prior
        if delta > 0:
            if source_guid is not None:
                lifecycle.active_sources.append(source_guid)
                if delta > 1:
                    lifecycle.active_sources.extend([None] * (delta - 1))
                    lifecycle.saw_unattributed_transition = True
            else:
                lifecycle.active_sources.extend([None] * delta)
                lifecycle.saw_unattributed_transition = True
        elif source_guid is None:
            lifecycle.saw_unattributed_transition = True
        lifecycle.current_amount = current

    def cast_actions(self) -> list[dict[str, Any]]:
        """Return deterministic factual-lifecycle and LOO diagnostics.

        A Sunder cast at factual five stacks is retained as a factual refresh
        candidate with zero stack delta.  It is deliberately nonexecutable in
        a counterfactual replay because ``AuraCast`` contains no hit/result and
        deleting an earlier focal stack can turn the cast into an attempted
        stack increase.  An unmatched cast below five is likewise not silently
        promoted to a refresh.
        """

        actions: list[dict[str, Any]] = []
        for cast in self._all_contract_casts:
            debuff_id = cast.key[3]
            if cast.matched_addition:
                prior = cast.matched_prior_amount
                current = cast.matched_current_amount
                assert prior is not None and current is not None
                delta = current - prior
                if delta > 0:
                    action_status = "MATCHED_AURA_STACK_INCREASE"
                    factual_lifecycle_status = "OBSERVED_STATE_ADDED_STACK_INCREASE"
                    factual_result_observed = True
                    counterfactual_nonexecutable = delta != 1
                    if counterfactual_nonexecutable:
                        counterfactual_reason = "MULTISTACK_DELTA_HAS_UNRESOLVED_CONTRIBUTION"
                        loo_status = "NONEXECUTABLE"
                    else:
                        counterfactual_reason = None
                        loo_status = (
                            "CONDITIONALLY_ADMISSIBLE_REQUIRES_NO_FOCAL_"
                            "PREDECESSOR_CONTRIBUTION"
                        )
                    factual_lifecycle_supported = delta == 1
                    factual_refresh_candidate = False
                    factual_refresh_observed = False
                elif delta == 0:
                    action_status = "MATCHED_AURA_REFRESH_ZERO_STACK_DELTA"
                    factual_lifecycle_status = "OBSERVED_STATE_ADDED_ZERO_STACK_DELTA"
                    factual_result_observed = True
                    counterfactual_nonexecutable = True
                    counterfactual_reason = (
                        "ZERO_DELTA_REFRESH_CANNOT_REPAIR_REMOVED_FOCAL_PREDECESSOR"
                    )
                    loo_status = "NONEXECUTABLE"
                    factual_lifecycle_supported = True
                    factual_refresh_candidate = False
                    factual_refresh_observed = True
                else:
                    action_status = "MATCHED_NONMONOTONIC_AURA_TRANSITION_REJECTED"
                    factual_lifecycle_status = "OBSERVED_NONMONOTONIC_STATE_TRANSITION"
                    factual_result_observed = True
                    counterfactual_nonexecutable = True
                    counterfactual_reason = "NONMONOTONIC_AURA_TRANSITION"
                    loo_status = "NONEXECUTABLE"
                    factual_lifecycle_supported = False
                    factual_refresh_candidate = False
                    factual_refresh_observed = False
            elif debuff_id == "sunder_armor" and cast.amount_before_cast >= SUNDER_MAX_STACKS:
                delta = 0
                action_status = "SUNDER_FULL_STACK_REFRESH_CANDIDATE_PRESERVED"
                factual_lifecycle_status = "CAST_ONLY_FULL_STACK_REFRESH_CANDIDATE"
                factual_result_observed = False
                counterfactual_nonexecutable = True
                counterfactual_reason = "AURA_CAST_HAS_NO_SUCCESS_RESULT"
                loo_status = "NONEXECUTABLE"
                factual_lifecycle_supported = False
                factual_refresh_candidate = True
                factual_refresh_observed = False
            elif debuff_id in NONSTACKING_DEBUFFS and cast.amount_before_cast >= 1:
                delta = 0
                action_status = "NONSTACKING_ACTIVE_AURA_REFRESH_CANDIDATE_PRESERVED"
                factual_lifecycle_status = "CAST_ONLY_ACTIVE_AURA_REFRESH_CANDIDATE"
                factual_result_observed = False
                counterfactual_nonexecutable = True
                counterfactual_reason = "AURA_CAST_HAS_NO_SUCCESS_RESULT"
                loo_status = "NONEXECUTABLE"
                factual_lifecycle_supported = False
                factual_refresh_candidate = True
                factual_refresh_observed = False
            else:
                delta = None
                action_status = "UNMATCHED_CAST_NOT_EXECUTABLE_AS_REFRESH"
                factual_lifecycle_status = "CAST_ONLY_WITHOUT_AURA_TRANSITION"
                factual_result_observed = False
                counterfactual_nonexecutable = True
                counterfactual_reason = "AURA_CAST_HAS_NO_SUCCESS_RESULT"
                loo_status = "NONEXECUTABLE"
                factual_lifecycle_supported = False
                factual_refresh_candidate = False
                factual_refresh_observed = False
            actions.append(
                {
                    "instance": cast.key[0],
                    "encounter": cast.key[1],
                    "target_guid": cast.key[2],
                    "debuff_id": debuff_id,
                    "spell_id": cast.key[4],
                    "source_guid": cast.source_guid,
                    "timestamp_ms": cast.timestamp_ms,
                    "anchor": dict(cast.anchor),
                    "duration_ms": cast.duration_ms,
                    "cap_status": cast.cap_status,
                    "effect_tuple": list(cast.effect_tuple),
                    "factual_amount_before_cast": cast.amount_before_cast,
                    "factual_stack_delta": delta,
                    "factual_lifecycle_status": factual_lifecycle_status,
                    "factual_result_event_observed": factual_result_observed,
                    "factual_aura_lifecycle_supported": factual_lifecycle_supported,
                    "factual_refresh_candidate": factual_refresh_candidate,
                    "factual_refresh_observed": factual_refresh_observed,
                    "matched_aura_state_anchor": (
                        dict(cast.matched_aura_anchor)
                        if cast.matched_aura_anchor is not None
                        else None
                    ),
                    "action_status": action_status,
                    "loo_counterfactual": {
                        "status": loo_status,
                        "counterfactual_nonexecutable": counterfactual_nonexecutable,
                        "counterfactual_executable": False,
                        "focal_context_evaluated": False,
                        "reason": counterfactual_reason,
                        "requires_no_focal_predecessor_contribution": (
                            not counterfactual_nonexecutable
                        ),
                    },
                }
            )
        return actions

    def finish(self, *, include_cast_actions: bool = False) -> dict[str, Any]:
        actions = self.cast_actions()
        action_counts = Counter(action["action_status"] for action in actions)
        factual_result_observed = sum(
            bool(action["factual_result_event_observed"]) for action in actions
        )
        factual_refresh_candidates = sum(
            bool(action["factual_refresh_candidate"]) for action in actions
        )
        factual_lifecycle_supported = sum(
            bool(action["factual_aura_lifecycle_supported"]) for action in actions
        )
        counterfactual_nonexecutable = sum(
            bool(action["loo_counterfactual"]["counterfactual_nonexecutable"])
            for action in actions
        )
        current_right_censored = [
            row for row in self._lifecycles.values() if row.current_amount > 0
        ]
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "status": STATUS,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "join_window_ms": self.join_window_ms,
            "fixed_effect_contract": {
                "physical_armor_effect_tuple": list(PHYSICAL_ARMOR_EFFECT_TUPLE),
                "tuple_fields": ["effect", "effect_aura_name", "effect_misc_value"],
                "semantic_names": [
                    "EffectApplyAura",
                    "AuraEffectModResistance",
                    "physical_school_mask",
                ],
                "spell_effect_contracts": [
                    {
                        "debuff_id": key[0],
                        "spell_id": key[1],
                        "effect_tuple": list(value),
                    }
                    for key, value in sorted(HOSTILE_ARMOR_SPELL_EFFECT_CONTRACTS.items())
                ],
                "bonereaver_self_ignore_effect_tuple": list(
                    BONEREAVER_SELF_IGNORE_EFFECT_TUPLE
                ),
            },
            "counts": dict(sorted(self._counts.items())),
            "counts_by_debuff": {
                debuff_id: {
                    metric: self._by_debuff[(debuff_id, metric)]
                    for metric in sorted(
                        child_metric
                        for child_debuff, child_metric in self._by_debuff
                        if child_debuff == debuff_id
                    )
                }
                for debuff_id in sorted({key[0] for key in self._by_debuff})
            },
            "observed_spell_effect_tuples": [
                {
                    "debuff_id": debuff_id,
                    "spell_id": spell_id,
                    "effect": effect,
                    "effect_aura_name": aura_name,
                    "effect_misc_value": misc,
                    "count": count,
                }
                for (debuff_id, spell_id, effect, aura_name, misc), count in sorted(
                    self._observed_tuples.items()
                )
            ],
            "cast_action_counts": dict(sorted(action_counts.items())),
            "physical_effect_cast_count": len(actions),
            "factual_result_event_observed_count": factual_result_observed,
            "factual_aura_lifecycle_supported_count": factual_lifecycle_supported,
            "factual_refresh_candidate_count": factual_refresh_candidates,
            "loo_conditionally_admissible_action_count": (
                len(actions) - counterfactual_nonexecutable
            ),
            "loo_counterfactual_nonexecutable_count": counterfactual_nonexecutable,
            "lifecycle_end_diagnostics": {
                "right_censored_active_lifecycle_count": (
                    self._counts["right_censored_active_lifecycle_at_scope_close"]
                    + len(current_right_censored)
                ),
                "right_censored_active_stack_count": (
                    self._counts["right_censored_active_stack_at_scope_close"]
                    + sum(row.current_amount for row in current_right_censored)
                ),
            },
            "claim_boundary": {
                "policy_input_authorized": False,
                "comparison_input_authorized": False,
                "training_authorized": False,
                "dynamic_armor_projection_authorized": False,
                "state_removed_caster_inferred": False,
                "direct_source_guid_only": True,
                "owner_or_controller_resolution_attempted": False,
                "factual_shared_stack_schedule_is_counterfactual_loo": False,
                "conditional_action_is_loo_schedule_without_focal_audit": False,
                "bonereaver_is_hostile_flat_armor_reduction": False,
            },
        }
        if include_cast_actions:
            result["cast_actions"] = actions
        return result


def attribute_state_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    join_window_ms: int = DEFAULT_JOIN_WINDOW_MS,
    include_cast_actions: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scanner = AuraAttributionScannerV2(join_window_ms=join_window_ms)
    events: list[dict[str, Any]] = []
    for row in rows:
        event = scanner.consume(row)
        if event is not None:
            events.append(event)
    return events, scanner.finish(include_cast_actions=include_cast_actions)


def _merge_tuple_rows(
    total: Counter[tuple[str, int, int, int, int]], rows: Iterable[Mapping[str, Any]]
) -> None:
    for row in rows:
        total[
            (
                _text(row.get("debuff_id"), "tuple.debuff_id"),
                _integer(row.get("spell_id"), "tuple.spell_id"),
                _integer(row.get("effect"), "tuple.effect"),
                _integer(row.get("effect_aura_name"), "tuple.effect_aura_name"),
                _integer(row.get("effect_misc_value"), "tuple.effect_misc_value"),
            )
        ] += _integer(row.get("count"), "tuple.count")


def scan_raw_manifest_shard(
    *,
    source_manifest_path: str | Path,
    data_root: str | Path,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    """Scan a deterministic raw-manifest shard without copying raw objects."""

    if not 0 <= shard_index < shard_count:
        raise ChronicleExternalAuraAttributionV2Error("invalid shard index/count")
    source_path = Path(source_manifest_path).expanduser().resolve()
    source, _source_bytes, source_sha = state_v1._load_source_manifest(source_path)
    raw_root = state_v1._resolve_raw_root(Path(data_root))
    instances = sorted(
        (_mapping(row, "source instance") for row in source["instances"]),
        key=lambda row: _text(row.get("instance_id"), "instance_id"),
    )
    selected = instances[shard_index::shard_count]
    totals: Counter[str] = Counter()
    by_debuff: Counter[tuple[str, str]] = Counter()
    tuples: Counter[tuple[str, int, int, int, int]] = Counter()
    action_counts: Counter[str] = Counter()
    per_instance: list[dict[str, Any]] = []
    for instance in selected:
        scanner = AuraAttributionScannerV2()
        try:
            rows: Iterator[Mapping[str, Any]] = v1._raw_rows(
                instance, raw_root=raw_root
            )
            for row in rows:
                scanner.consume(row)
        except v1.ChronicleExternalAuraAttributionError as error:
            raise ChronicleExternalAuraAttributionV2Error(str(error)) from error
        summary = scanner.finish()
        totals.update(summary["counts"])
        for debuff_id, metrics in summary["counts_by_debuff"].items():
            for metric, count in metrics.items():
                by_debuff[(debuff_id, metric)] += count
        _merge_tuple_rows(tuples, summary["observed_spell_effect_tuples"])
        action_counts.update(summary["cast_action_counts"])
        per_instance.append(
            {
                "instance_id": instance["instance_id"],
                "counts": summary["counts"],
                "cast_action_counts": summary["cast_action_counts"],
                "physical_effect_cast_count": summary["physical_effect_cast_count"],
                "factual_result_event_observed_count": summary[
                    "factual_result_event_observed_count"
                ],
                "factual_aura_lifecycle_supported_count": summary[
                    "factual_aura_lifecycle_supported_count"
                ],
                "factual_refresh_candidate_count": summary[
                    "factual_refresh_candidate_count"
                ],
                "loo_conditionally_admissible_action_count": summary[
                    "loo_conditionally_admissible_action_count"
                ],
                "loo_counterfactual_nonexecutable_count": summary[
                    "loo_counterfactual_nonexecutable_count"
                ],
                "lifecycle_end_diagnostics": summary["lifecycle_end_diagnostics"],
            }
        )
    return {
        "schema": SCHEMA + "/raw_manifest_shard_scan",
        "status": STATUS,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "source_manifest_sha256": source_sha,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "instance_count": len(selected),
        "instance_ids": [str(row["instance_id"]) for row in selected],
        "counts": dict(sorted(totals.items())),
        "counts_by_debuff": {
            debuff_id: {
                metric: by_debuff[(debuff_id, metric)]
                for metric in sorted(
                    child_metric
                    for child_debuff, child_metric in by_debuff
                    if child_debuff == debuff_id
                )
            }
            for debuff_id in sorted({key[0] for key in by_debuff})
        },
        "observed_spell_effect_tuples": [
            {
                "debuff_id": debuff_id,
                "spell_id": spell_id,
                "effect": effect,
                "effect_aura_name": aura_name,
                "effect_misc_value": misc,
                "count": count,
            }
            for (debuff_id, spell_id, effect, aura_name, misc), count in sorted(
                tuples.items()
            )
        ],
        "cast_action_counts": dict(sorted(action_counts.items())),
        "per_instance": per_instance,
        "network_request_count": 0,
        "raw_object_copy_count": 0,
        "claim_boundary": {
            "policy_input_authorized": False,
            "comparison_input_authorized": False,
            "training_authorized": False,
            "dynamic_armor_projection_authorized": False,
            "direct_source_guid_only": True,
            "owner_or_controller_resolution_attempted": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    partition = sub.add_parser("scan-partition")
    partition.add_argument("--partition", type=Path, required=True)
    partition.add_argument("--join-window-ms", type=int, default=DEFAULT_JOIN_WINDOW_MS)
    raw = sub.add_parser("scan-raw-manifest")
    raw.add_argument("--source-manifest", type=Path, required=True)
    raw.add_argument("--data-root", type=Path, required=True)
    raw.add_argument("--shard-index", type=int, default=0)
    raw.add_argument("--shard-count", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan-partition":
            result = attribute_state_rows(
                v1.iter_state_partition(args.partition),
                join_window_ms=args.join_window_ms,
            )[1]
        else:
            result = scan_raw_manifest_shard(
                source_manifest_path=args.source_manifest,
                data_root=args.data_root,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
            )
    except (ChronicleExternalAuraAttributionV2Error, state_v1.ChronicleExternalStateEventNormalizerError) as error:
        print(f"Chronicle Stage-7 v2 aura attribution failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BONEREAVER_SELF_IGNORE_EFFECT_TUPLE",
    "DEFAULT_JOIN_WINDOW_MS",
    "EVENT_SCHEMA",
    "HOSTILE_ARMOR_SPELL_EFFECT_CONTRACTS",
    "HOSTILE_ARMOR_SPELL_IDS",
    "IMPLEMENTATION_REVISION",
    "PHYSICAL_ARMOR_EFFECT_TUPLE",
    "SCHEMA",
    "STATUS",
    "SUNDER_MAX_STACKS",
    "AuraAttributionScannerV2",
    "ChronicleExternalAuraAttributionV2Error",
    "attribute_state_rows",
    "scan_raw_manifest_shard",
]
