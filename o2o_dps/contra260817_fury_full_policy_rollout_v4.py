"""Contra260817 source-oracle rollout over native dynamic-v3 semantics.

This is an additive diagnostic lane.  It binds the v3 source translation and
the v4 ordered executor to the existing native ``load_dynamic_v3`` lifecycle;
armor, attackability, background damage, candidate damage, and central idle
advancement therefore come from the same simulator load.  The source-default
profile and incomplete addon package remain explicit blockers, so this module
does not claim a runtime Contra configuration or WoW-client fidelity.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import json
import math
import types
from typing import Any, Mapping, Sequence

from . import cat_fury_full_policy_rollout_v5 as _cat_v5
from . import fury_full_policy_rollout_v2 as _v2
from . import fury_full_policy_rollout_v5 as _dynamic_v5
from .contra260817_fury_full_policy_v3 import (
    POLICY_ID,
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
    Contra260817FuryFullPolicyStateV3,
    Contra260817FuryTalentV3,
    TargetSelectionStateV3,
    validate_source_decision_v3,
)
from .contra260817_fury_ordered_sink_executor_v4 import (
    EXECUTION_SCHEMA_V4,
    POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4,
    POST_GCD_REJECTION_REASON_V4,
    POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
    SOURCE_REENTRY_CLOCK_SCHEMA_V4,
    SOURCE_REENTRY_RETRY_MS_V4,
    SOURCE_REENTRY_TIMING_AUTHORITY_V4,
    _source_reentry_trigger_v4,
    Contra260817ItemActionBindingV4,
    Contra260817SimulatorControlFacadeV4,
    Contra260817TargetBindingV4,
    execute_contra260817_ordered_sinks_v4,
)
from .expert_policy import SwingQueueOp, WAIT_ACTION
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
)
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    validate_dynamic_load_request_v3,
)
from .fury_expert_adapters import WeaponMode
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    validate_v2_execution_base_identity_v3,
)
from .sim_bridge import AvailableAction
from .sim_bridge_dynamic_v3 import DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V4 = "contra260817_fury_full_policy_simulator_rollout/v4"
ROLLOUT_CONTENT_SCHEMA_V4 = "contra260817_fury_full_policy_rollout_content/v4"
RECEIPT_BUNDLE_SCHEMA_V4 = "contra260817_source_simulator_receipt_bundle/v4"
IMPLEMENTATION_REVISION = "v4.3_contra260817_accepted_queue_reentry"


class Contra260817FuryFullPolicyRolloutV4Error(RuntimeError):
    """The native Contra260817 diagnostic artifact is malformed."""


@dataclass(frozen=True)
class Contra260817SimulatorInputsV4:
    """Simulator hypotheses for source reads not exposed by the bridge."""

    authorization_gate_passed: bool = True
    initial_autoattack_active: bool = False
    current_target_in_front: bool = True
    switch_throttle_open: bool = True
    current_target_friendly: bool = False
    current_target_is_player: bool = False
    target_banished_indices: tuple[int, ...] = ()
    attack_actionbar_present: bool = True
    start_attack_banish_branch_active: bool = False
    heroic_strike_probe_texture_present: bool = True
    cleave_probe_texture_present: bool = True
    target_affecting_combat: bool = True
    target_casting_spell: str | None = None
    target_sunder_stacks: int = 5
    player_health_pct: float = 100.0
    target_of_target_is_player: bool = False
    offhand_is_shield_after_prelude: bool = False
    equipped_mainhand_name: str | None = "SIMULATOR_MAINHAND"
    equipped_offhand_name: str | None = "SIMULATOR_OFFHAND"
    player_buffs: frozenset[str] = frozenset({"战斗怒吼", "狂怒"})
    target_debuffs: frozenset[str] = frozenset({"挫志怒吼"})
    cooldowns_s: tuple[tuple[str, float], ...] = (
        ("血性狂暴", 10.0),
        ("震荡猛击", 10.0),
    )
    spell_rage_costs: tuple[tuple[str, float], ...] = ()
    five_yard_guid_candidate: str | None = None
    five_yard_guid_is_current: bool | None = None
    previous_target_guid: str | None = None
    nearest_enemy_changed_target: bool | None = None
    nearest_enemy_target_within_five_yards: bool | None = None
    target_bindings: tuple[Contra260817TargetBindingV4, ...] = ()
    item_action_bindings: tuple[Contra260817ItemActionBindingV4, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "authorization_gate_passed",
            "initial_autoattack_active",
            "current_target_in_front",
            "switch_throttle_open",
            "current_target_friendly",
            "current_target_is_player",
            "attack_actionbar_present",
            "start_attack_banish_branch_active",
            "heroic_strike_probe_texture_present",
            "cleave_probe_texture_present",
            "target_affecting_combat",
            "target_of_target_is_player",
            "offhand_is_shield_after_prelude",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if (
            isinstance(self.player_health_pct, bool)
            or not isinstance(self.player_health_pct, (int, float))
            or not math.isfinite(float(self.player_health_pct))
            or not 0 <= float(self.player_health_pct) <= 100
        ):
            raise ValueError("player_health_pct must be a finite percent")
        if type(self.target_sunder_stacks) is not int or not 0 <= self.target_sunder_stacks <= 5:
            raise ValueError("target_sunder_stacks must be in [0,5]")
        if tuple(sorted(set(self.target_banished_indices))) != self.target_banished_indices or any(
            type(index) is not int or index < 0 for index in self.target_banished_indices
        ):
            raise ValueError("target_banished_indices must be sorted unique indices")
        for name in (
            "target_casting_spell",
            "five_yard_guid_candidate",
            "previous_target_guid",
            "equipped_mainhand_name",
            "equipped_offhand_name",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise TypeError(f"{name} must be nonempty or None")
        for name in (
            "five_yard_guid_is_current",
            "nearest_enemy_changed_target",
            "nearest_enemy_target_within_five_yards",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean or None")
        if any(not isinstance(value, str) or not value for value in self.player_buffs):
            raise TypeError("player_buffs must contain nonempty strings")
        if any(not isinstance(value, str) or not value for value in self.target_debuffs):
            raise TypeError("target_debuffs must contain nonempty strings")
        for rows_name in ("cooldowns_s", "spell_rage_costs"):
            rows = getattr(self, rows_name)
            keys: set[str] = set()
            for key, value in rows:
                if not isinstance(key, str) or not key or key in keys:
                    raise ValueError(f"{rows_name} has an invalid/duplicate key")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError(f"{rows_name}[{key!r}] must be finite nonnegative")
                keys.add(key)
        if {key for key, _ in self.cooldowns_s} != {"血性狂暴", "震荡猛击"}:
            raise ValueError("cooldowns_s must contain the two source reads")
        if any(not isinstance(row, Contra260817TargetBindingV4) for row in self.target_bindings):
            raise TypeError("target_bindings must contain v4 bindings")
        if any(not isinstance(row, Contra260817ItemActionBindingV4) for row in self.item_action_bindings):
            raise TypeError("item_action_bindings must contain v4 bindings")

    def receipt(self) -> JSONMap:
        return {
            "authorization_gate_passed": self.authorization_gate_passed,
            "initial_autoattack_active": self.initial_autoattack_active,
            "target_sunder_stacks": self.target_sunder_stacks,
            "player_health_pct": float(self.player_health_pct),
            "equipped_mainhand_name": self.equipped_mainhand_name,
            "equipped_offhand_name": self.equipped_offhand_name,
            "player_buffs": sorted(self.player_buffs),
            "target_debuffs": sorted(self.target_debuffs),
            "cooldowns_s": {key: value for key, value in self.cooldowns_s},
            "spell_rage_costs": {key: value for key, value in self.spell_rage_costs},
            "target_bindings": [asdict(row) for row in self.target_bindings],
            "item_action_bindings": [
                {"locator": row.locator, "action": row.action.to_wire()}
                for row in self.item_action_bindings
            ],
            "evidence_kind": "SIMULATOR_INPUT_HYPOTHESIS",
            "runtime_contradb_observed": False,
            "game_client_observed": False,
        }


def _clone_core_v4(
    controls: Contra260817SimulatorControlFacadeV4,
    inputs: Contra260817SimulatorInputsV4,
    evidence_sha256: str,
) -> Any:
    namespace = dict(_dynamic_v5._RUN_V5_CORE.__globals__)
    namespace.update(
        {
            "_supported_adapter": _supported_adapter_v4,
            "_combat_state": _contra_state_mapper(controls, inputs, evidence_sha256),
            "_proposal": _proposal_v4,
            "execute_ordered_sinks_v2": execute_contra260817_ordered_sinks_v4,
            "_audit_ordered_execution": _audit_ordered_execution_v4,
            "_annotate_attempt_ids": _annotate_attempt_ids_v4,
            "_accepted_gcd_actions_from_events": _cat_v5._accepted_gcd_actions_v5,
            "_RESULT_BEARING_GCD_ACTIONS": _RESULT_BEARING_GCD_ACTIONS_V4,
            "_attach_result_followups": _cat_v5._attach_result_followups_v5,
            "_attach_registered_result_followups": _cat_v5._attach_registered_result_followups_v5,
            "_audit_gcd_result_followups": _cat_v5._audit_result_followups_v5,
            "_annotate_immediate_state_deltas": _cat_v5._annotate_immediate_state_deltas_v5,
            "_observe_server_boundary": _cat_v5._observe_simulator_boundary_v5,
            "_unknown_server_boundary": _cat_v5._unknown_simulator_boundary_v5,
            "_right_censor_terminal_active_hardcast": _cat_v5._right_censor_v5,
            "_jsonable": _jsonable_v4,
        }
    )
    source = _v2.run_fury_full_policy_rollout_v2
    clone = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_contra260817_full_policy_rollout_v4_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    clone.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return clone


def _jsonable_v4(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable_v4(item) for key, item in value.items()}
    if isinstance(value, frozenset):
        return sorted(_jsonable_v4(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable_v4(item) for item in value]
    return _v2._jsonable(value)


def _supported_adapter_v4(adapter: Any) -> bool:
    return (
        type(adapter) is Contra260817FuryFullPolicyAdapterV3
        and adapter.expert_id == POLICY_ID
    )


def _sim_evidence(digest: str) -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SIMULATOR_STATE,
        source_sha256=digest,
    )


def _contra_state_mapper(
    controls: Contra260817SimulatorControlFacadeV4,
    inputs: Contra260817SimulatorInputsV4,
    evidence_sha256: str,
):
    def mapper(
        state: Mapping[str, Any],
        available: Sequence[AvailableAction],
        request: Mapping[str, Any],
        target: Mapping[str, Any],
        *,
        last_gcd_action: str,
    ) -> Contra260817FuryFullPolicyStateV3:
        combat = _v2._combat_state(
            state,
            available,
            request,
            target,
            last_gcd_action=last_gcd_action,
        )
        target_index = int(target["target_index"])
        distance = float(target["target_distance_yards"])
        dynamic = state.get("dynamic_target_semantics")
        rows = dynamic.get("targets") if isinstance(dynamic, Mapping) else None
        live_attackable = (
            sum(
                isinstance(row, Mapping)
                and row.get("attackable") is True
                and row.get("dead") is False
                for row in rows
            )
            if isinstance(rows, list)
            else int(state.get("num_targets", 0))
        )
        current_dead = False
        current_attackable = combat.target_exists
        if isinstance(rows, list) and target_index < len(rows) and isinstance(rows[target_index], Mapping):
            current_dead = rows[target_index].get("dead") is True
            current_attackable = rows[target_index].get("attackable") is True
        in_five = current_attackable and not current_dead and distance <= 5.0
        target_state = TargetSelectionStateV3(
            current_target_exists=combat.target_exists,
            current_target_dead=current_dead,
            current_target_friendly=inputs.current_target_friendly,
            current_target_is_player=inputs.current_target_is_player,
            current_target_banished=target_index in inputs.target_banished_indices,
            current_target_in_melee_range=combat.in_melee_range,
            current_target_in_front=inputs.current_target_in_front,
            switch_throttle_open=inputs.switch_throttle_open,
            five_yard_guid_candidate=inputs.five_yard_guid_candidate,
            five_yard_guid_is_current=inputs.five_yard_guid_is_current,
            previous_target_guid=inputs.previous_target_guid,
            nearest_enemy_changed_target=inputs.nearest_enemy_changed_target,
            nearest_enemy_target_within_five_yards=inputs.nearest_enemy_target_within_five_yards,
            post_target_exists=combat.target_exists,
            start_attack_banish_branch_active=inputs.start_attack_banish_branch_active,
            attack_actionbar_present=inputs.attack_actionbar_present,
            autoattack_current=controls.autoattack_active,
        )
        classification = str(target["target_classification"])
        max_health = int(target["target_max_health"])
        name = str(target["target_name"])
        equipment = tuple(str(value) for value in target["equipped_item_names"])
        mainhand = controls.equipped_name(16) or inputs.equipped_mainhand_name
        offhand = controls.equipped_name(17) or inputs.equipped_offhand_name
        evidence = _sim_evidence(evidence_sha256)
        talent = (
            Contra260817FuryTalentV3.DUAL_WIELD
            if combat.weapon_mode is WeaponMode.DUAL_WIELD
            else Contra260817FuryTalentV3.TWO_HAND
        )
        return Contra260817FuryFullPolicyStateV3(
            entry_combat=combat,
            post_target_combat=combat,
            talent=talent,
            authorization_gate_passed=inputs.authorization_gate_passed,
            attackable_units_within_five_yards=(live_attackable if distance <= 5.0 else 0),
            current_target_within_five_yards=in_five,
            entry_target_classification=classification,
            entry_target_max_health=max_health,
            entry_target_name=name,
            post_target_classification=classification,
            post_target_max_health=max_health,
            post_target_name=name,
            target_selection=target_state,
            heroic_strike_probe_texture_present=inputs.heroic_strike_probe_texture_present,
            cleave_probe_texture_present=inputs.cleave_probe_texture_present,
            target_affecting_combat=inputs.target_affecting_combat,
            target_casting_spell=inputs.target_casting_spell,
            target_sunder_stacks=inputs.target_sunder_stacks,
            player_health_pct=float(inputs.player_health_pct),
            target_of_target_is_player=inputs.target_of_target_is_player,
            offhand_is_shield_after_prelude=inputs.offhand_is_shield_after_prelude,
            equipped_item_names=equipment,
            equipped_mainhand_name=mainhand,
            equipped_offhand_name=offhand,
            player_buffs=inputs.player_buffs,
            target_debuffs=inputs.target_debuffs,
            cooldowns_s={key: float(value) for key, value in inputs.cooldowns_s},
            spell_rage_costs={key: float(value) for key, value in inputs.spell_rage_costs},
            authorization_evidence=evidence,
            route_evidence=evidence,
            entry_snapshot_evidence=evidence,
            post_target_snapshot_evidence=evidence,
            target_selection_evidence=evidence,
            actionbar_evidence=evidence,
            equipment_evidence=evidence,
            profile_runtime_evidence=None,
        )

    return mapper


def _proposal_v4(
    adapter: Contra260817FuryFullPolicyAdapterV3,
    state: Contra260817FuryFullPolicyStateV3,
    target: Mapping[str, Any],
) -> Any:
    del target
    return validate_source_decision_v3(adapter.propose(state))


_RESULT_BEARING_GCD_ACTIONS_V4 = frozenset(
    set(_v2._RESULT_BEARING_GCD_ACTIONS)
    | {
        "warrior.overpower",
        "warrior.shield_bash",
        "warrior.concussion_blow",
    }
)


def _annotate_attempt_ids_v4(
    execution: JSONMap, decision_index: int
) -> tuple[str, ...]:
    """Bind result IDs only to accepted, submitted result-bearing GCDs."""

    events = execution.get("sink_events")
    if not isinstance(events, list):
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "ordered execution lacks sink_events"
        )
    result: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            raise Contra260817FuryFullPolicyRolloutV4Error(
                "sink event must be an object"
            )
        order = event.get("order")
        if type(order) is not int or order <= 0:
            raise Contra260817FuryFullPolicyRolloutV4Error(
                "sink event order is invalid"
            )
        attempt = event.get("source_attempt")
        if not isinstance(attempt, dict):
            raise Contra260817FuryFullPolicyRolloutV4Error(
                "source attempt is missing"
            )
        source = event.get("source_sink")
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        operation = event.get("operation_contract")
        result_bearing = (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and isinstance(submission, Mapping)
            and submission.get("status") == "SUBMITTED"
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
            and isinstance(operation, Mapping)
            and isinstance(operation.get("action_ref"), Mapping)
            and submission.get("action") == operation.get("action_ref")
            and operation.get("canonical_action")
            in _RESULT_BEARING_GCD_ACTIONS_V4
        )
        expected = f"decision-{decision_index}:sink-{order}"
        existing = attempt.get("attempt_id")
        if result_bearing:
            if existing not in {None, expected}:
                raise Contra260817FuryFullPolicyRolloutV4Error(
                    "result attempt ID differs from executor submission"
                )
            attempt["attempt_id"] = expected
            result.append(expected)
        elif existing is not None:
            raise Contra260817FuryFullPolicyRolloutV4Error(
                "non-accepted source sink carries a result attempt ID"
            )
    return tuple(result)


def _valid_post_gcd_rejection_v4(
    event: Mapping[str, Any],
    raw: Mapping[str, Any],
    prior_consuming_gcd: tuple[int, str] | None,
) -> bool:
    submission = event.get("simulator_submission")
    acceptance = event.get("simulator_acceptance")
    consumption = event.get("decision_consumption")
    outcome = event.get("simulator_outcome")
    traversal = event.get("traversal")
    attempt = event.get("source_attempt")
    return (
        prior_consuming_gcd is not None
        and (
            raw.get("channel") == "gcd"
            or (raw.get("channel") == "swing_queue" and raw.get("value") == "顺劈斩")
        )
        and raw.get("operation") == "CastSpellByName"
        and isinstance(submission, Mapping)
        and submission.get("status")
        == POST_GCD_REJECTION_SUBMISSION_STATUS_V4
        and submission.get("reason") == POST_GCD_REJECTION_REASON_V4
        and submission.get("bridge_call_made") is False
        and "action" not in submission
        and isinstance(acceptance, Mapping)
        and acceptance.get("status")
        == POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4
        and acceptance.get("reason") == POST_GCD_REJECTION_REASON_V4
        and acceptance.get("evidence")
        == "earlier_sink_simulator_acceptance_and_decision_consumption"
        and acceptance.get("evidence_scope") == "SIMULATOR_EXECUTION_LEDGER"
        and isinstance(consumption, Mapping)
        and consumption.get("status") == "ALREADY_CONSUMED_BY_EARLIER_GCD"
        and consumption.get("consumes_decision") is False
        and consumption.get("expected_for_lane") is True
        and consumption.get("consumed_by_sink_order") == prior_consuming_gcd[0]
        and consumption.get("consumed_by_action") == prior_consuming_gcd[1]
        and isinstance(outcome, Mapping)
        and outcome.get("status") == "NOT_APPLICABLE_REJECTED"
        and outcome.get("acceptance_is_not_outcome") is True
        and isinstance(traversal, Mapping)
        and traversal.get("executor_state")
        == "SOURCE_ATTEMPT_RECORDED_AS_POST_GCD_SIMULATOR_REJECTION"
        and isinstance(attempt, Mapping)
        and attempt.get("attempt_id") is None
    )


def _accepted_consuming_gcd_v4(
    event: Mapping[str, Any], raw: Mapping[str, Any]
) -> str | None:
    acceptance = event.get("simulator_acceptance")
    consumption = event.get("decision_consumption")
    operation = event.get("operation_contract")
    if (
        raw.get("channel") == "gcd"
        and isinstance(acceptance, Mapping)
        and acceptance.get("status") == "ACCEPTED"
        and isinstance(consumption, Mapping)
        and consumption.get("consumes_decision") is True
        and isinstance(operation, Mapping)
        and isinstance(operation.get("canonical_action"), str)
    ):
        return str(operation["canonical_action"])
    return None


def _all_sinks_typed_rejected_nonconsuming_v4(events: Any) -> bool:
    if not isinstance(events, list) or not events:
        return False
    return any(
        isinstance(event, Mapping)
        and isinstance(event.get("simulator_submission"), Mapping)
        and event["simulator_submission"].get("status") == "SUBMITTED"
        for event in events
    ) and all(
        isinstance(event, Mapping)
        and isinstance(event.get("simulator_submission"), Mapping)
        and event["simulator_submission"].get("status")
        in {"SUBMITTED", "NOT_SUBMITTED_SOURCE_DECLARED_NOOP"}
        and isinstance(event.get("simulator_acceptance"), Mapping)
        and event["simulator_acceptance"].get("status")
        in {"REJECTED", "REJECTED_SOURCE_DECLARED_NOOP"}
        and isinstance(event.get("decision_consumption"), Mapping)
        and event["decision_consumption"].get("consumes_decision") is False
        for event in events
    )


def _audit_ordered_execution_v4(
    execution: Mapping[str, Any],
    proposal: Any,
    *,
    decision_index: int,
) -> list[JSONMap]:
    blockers: list[JSONMap] = []

    def add(code: str, message: str, *, fatal: bool, evidence: Mapping[str, Any] | None = None) -> None:
        blockers.append(
            _v2._blocker(
                code,
                message,
                execution_fatal=fatal,
                decision_index=decision_index,
                evidence=evidence,
            )
        )

    if execution.get("schema") != EXECUTION_SCHEMA_V4:
        add("CONTRA260817_V4_EXECUTION_SCHEMA_MISMATCH", "ordered executor schema differs", fatal=True)
    fallback = execution.get("fallback")
    if not isinstance(fallback, Mapping) or fallback.get("used") is not False:
        add("CONTRA260817_V4_FALLBACK_PRESENT", "source execution used a fallback", fatal=True)
    expected = [sink.to_dict() for sink in proposal.raw_sink_order]
    events = execution.get("sink_events")
    if execution.get("raw_sink_order") != expected or not isinstance(events, list) or len(events) != len(expected):
        add("CONTRA260817_V4_RAW_LEDGER_MISMATCH", "raw source ledger/cardinality differs", fatal=True)
        return blockers
    prior_consuming_gcd: tuple[int, str] | None = None
    for order, (raw, event) in enumerate(zip(expected, events), start=1):
        if not isinstance(event, Mapping) or event.get("order") != order or event.get("source_sink") != raw:
            add("CONTRA260817_V4_SOURCE_ORDER_MISMATCH", f"sink {order} changed source order", fatal=True)
            continue
        attempt = event.get("source_attempt")
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        if not isinstance(attempt, Mapping) or attempt.get("status") != "ATTEMPTED":
            add("CONTRA260817_V4_SOURCE_ATTEMPT_MISSING", f"sink {order} lacks source attempt", fatal=True)
        submission_status = submission.get("status") if isinstance(submission, Mapping) else None
        acceptance_status = acceptance.get("status") if isinstance(acceptance, Mapping) else None
        post_gcd_rejection = (
            submission_status == POST_GCD_REJECTION_SUBMISSION_STATUS_V4
            or acceptance_status == POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4
        )
        if post_gcd_rejection:
            if not _valid_post_gcd_rejection_v4(
                event, raw, prior_consuming_gcd
            ):
                add(
                    "CONTRA260817_V4_POST_GCD_REJECTION_INVALID",
                    f"sink {order} post-GCD rejection ledger is invalid",
                    fatal=True,
                )
        elif submission_status not in {
            "SUBMITTED",
            "NOT_SUBMITTED_SOURCE_DECLARED_NOOP",
        }:
            add(
                "CONTRA260817_V4_SIMULATOR_SUBMISSION_INCOMPLETE",
                f"sink {order} disposition is {submission_status!r}",
                fatal=True,
                evidence={"order": order, "status": submission_status},
            )
        if acceptance_status not in {
            "ACCEPTED",
            "REJECTED",
            "REJECTED_SOURCE_DECLARED_NOOP",
            POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4,
        }:
            add("CONTRA260817_V4_SIMULATOR_ACCEPTANCE_UNTYPED", f"sink {order} acceptance is untyped", fatal=True)
        operation = event.get("operation_contract")
        result_bearing = (
            raw.get("channel") == "gcd"
            and submission_status == "SUBMITTED"
            and acceptance_status == "ACCEPTED"
            and isinstance(operation, Mapping)
            and operation.get("canonical_action")
            in _RESULT_BEARING_GCD_ACTIONS_V4
        )
        attempt_id = attempt.get("attempt_id") if isinstance(attempt, Mapping) else None
        expected_attempt_id = f"decision-{decision_index}:sink-{order}"
        if result_bearing and attempt_id != expected_attempt_id:
            add(
                "CONTRA260817_V4_RESULT_ATTEMPT_ID_MISSING",
                f"sink {order} accepted result-bearing action lacks its result ID",
                fatal=True,
            )
        elif not result_bearing and attempt_id is not None:
            add(
                "CONTRA260817_V4_SPURIOUS_RESULT_ATTEMPT_ID",
                f"sink {order} is not an accepted result-bearing action",
                fatal=True,
            )
        if event.get("client_observation") != {
            "status": "NOT_OBSERVED_NO_WOW_CLIENT",
            "accepted": None,
            "evidence": None,
        }:
            add("CONTRA260817_V4_CLIENT_BOUNDARY_VIOLATED", "simulator event claimed client evidence", fatal=True)
        if event.get("server_outcome") != {
            "status": "NOT_OBSERVED_NO_GAME_SERVER_LOG",
            "outcome": None,
            "damage": None,
            "evidence": None,
        }:
            add("CONTRA260817_V4_SERVER_BOUNDARY_VIOLATED", "simulator event claimed server evidence", fatal=True)
        accepted_action = _accepted_consuming_gcd_v4(event, raw)
        if accepted_action is not None:
            prior_consuming_gcd = (order, accepted_action)
    skipped_cleave_guard = any(
        isinstance(row, Mapping)
        and row.get("operation") == "Contra.IsHeroicStrikActive"
        and row.get("value") == "顺劈斩"
        and row.get("reason") == "IsCurrentAction_returned_true"
        for row in proposal.metadata.get("helper_no_sink_attempts", [])
    )
    accepted_whirlwind = any(
        isinstance(event, Mapping)
        and isinstance(event.get("source_sink"), Mapping)
        and event["source_sink"].get("channel") == "gcd"
        and event["source_sink"].get("value") == "旋风斩"
        and isinstance(event.get("simulator_acceptance"), Mapping)
        and event["simulator_acceptance"].get("status") == "ACCEPTED"
        for event in events
    )
    if skipped_cleave_guard and accepted_whirlwind:
        add(
            "CONTRA260817_INTRA_INVOCATION_QUEUE_GUARD_NOT_REEVALUATED",
            "source adapter froze pre-key IsCurrentAction for Cleave after accepted Whirlwind; the equivalent client probe observed it change within the key",
            fatal=False,
        )
    reentry_trigger = _source_reentry_trigger_v4(events)
    reentry_expected = (
        proposal.gcd != WAIT_ACTION
        or reentry_trigger == "ACCEPTED_SWING_QUEUE_NONCONSUMING"
    ) and (
        execution.get("execution_blocked") is False
        and execution.get("decision_consumed") is False
        and reentry_trigger is not None
    )
    reentry = execution.get("source_reentry_clock")
    if reentry_expected != isinstance(reentry, Mapping):
        add(
            "CONTRA260817_V4_SOURCE_REENTRY_CLOCK_MISMATCH",
            "nonconsuming source invocation did not produce exactly one source reentry clock",
            fatal=True,
        )
    elif isinstance(reentry, Mapping):
        final_state = execution.get("final_state")
        scheduled_at = reentry.get("scheduled_at_time_ms")
        idle = final_state.get("dynamic_idle_advance") if isinstance(final_state, Mapping) else None
        horizon_ms = idle.get("horizon_ms") if isinstance(idle, Mapping) else None
        nominal_wake = (
            scheduled_at + SOURCE_REENTRY_RETRY_MS_V4
            if type(scheduled_at) is int
            else None
        )
        expected_boundary = (
            min(nominal_wake, horizon_ms)
            if nominal_wake is not None and type(horizon_ms) is int
            else nominal_wake
        )
        valid_reentry = (
            reentry.get("schema") == SOURCE_REENTRY_CLOCK_SCHEMA_V4
            and reentry.get("trigger") == reentry_trigger
            and reentry.get("timing_authority") == SOURCE_REENTRY_TIMING_AUTHORITY_V4
            and reentry.get("requested_ms") == SOURCE_REENTRY_RETRY_MS_V4
            and reentry.get("exact_client_cadence") is False
            and reentry.get("policy_action") is False
            and reentry.get("source_sink") is False
            and execution.get("wait_event") is None
            and isinstance(final_state, Mapping)
            and final_state.get("time_ms") == scheduled_at
            and final_state.get("needs_input") is False
            and reentry.get("nominal_wake_time_ms") == nominal_wake
            and reentry.get("expected_next_boundary_time_ms") == expected_boundary
        )
        if not valid_reentry:
            add(
                "CONTRA260817_V4_SOURCE_REENTRY_CLOCK_INVALID",
                "source reentry clock violates the fixed non-policy scheduling contract",
                fatal=True,
            )
    if execution.get("execution_blocked") is True:
        add(
            "CONTRA260817_V4_PREFLIGHT_OR_RUNTIME_BLOCKED",
            "a reached source channel could not be represented exactly",
            fatal=True,
            evidence={"reasons": execution.get("nonfaithful_reasons")},
        )
    return blockers


def _append_blocker_once(
    blockers: list[Any],
    code: str,
    message: str,
    *,
    execution_fatal: bool,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    if any(isinstance(row, Mapping) and row.get("code") == code for row in blockers):
        return
    blockers.append(
        _v2._blocker(code, message, execution_fatal=execution_fatal, evidence=evidence)
    )


def _receipt_bundle_v4(
    result: Mapping[str, Any],
    inputs: Contra260817SimulatorInputsV4,
    controls: Contra260817SimulatorControlFacadeV4,
    closure: Mapping[str, Any],
) -> JSONMap:
    events: list[Mapping[str, Any]] = []
    reentry_clocks: list[Mapping[str, Any]] = []
    for step in result.get("steps") or []:
        execution = step.get("ordered_execution") if isinstance(step, Mapping) else None
        rows = execution.get("sink_events") if isinstance(execution, Mapping) else None
        if isinstance(rows, list):
            events.extend(row for row in rows if isinstance(row, Mapping))
        reentry = execution.get("source_reentry_clock") if isinstance(execution, Mapping) else None
        if isinstance(reentry, Mapping):
            reentry_clocks.append(reentry)
    dispositions = {
        "SUBMITTED",
        "NOT_SUBMITTED_SOURCE_DECLARED_NOOP",
        POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
    }
    ordered_complete = all(
        isinstance(row.get("simulator_submission"), Mapping)
        and row["simulator_submission"].get("status") in dispositions
        for row in events
    )
    lifecycle_checks = closure.get("cursor_and_lifecycle_checks")
    lifecycle_complete = (
        closure.get("status") == "COMPLETE_BOUND"
        and isinstance(lifecycle_checks, Mapping)
        and bool(lifecycle_checks)
        and all(value is True for value in lifecycle_checks.values())
    )
    pairs = sorted(
        {
            (str(row["source_sink"]["channel"]), str(row["source_sink"]["operation"]))
            for row in events
            if isinstance(row.get("source_sink"), Mapping)
        }
    )
    return {
        "schema": RECEIPT_BUNDLE_SCHEMA_V4,
        "operation": {
            "run_raw_attempt_count": len(events),
            "run_typed_disposition_count": sum(
                isinstance(row.get("simulator_submission"), Mapping)
                and row["simulator_submission"].get("status") in dispositions
                for row in events
            ),
            "run_operation_pairs": [
                {"channel": channel, "operation": operation}
                for channel, operation in pairs
            ],
            "run_order_and_disposition_complete": ordered_complete,
            "source_reentry_count": len(reentry_clocks),
            "accepted_queue_reentry_count": sum(
                row.get("trigger") == "ACCEPTED_SWING_QUEUE_NONCONSUMING"
                for row in reentry_clocks
            ),
            "source_reentry_timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V4,
            "source_reentry_retry_ms": SOURCE_REENTRY_RETRY_MS_V4,
            "source_reentry_exact_client_cadence": False,
            "source_reentry_policy_action_count": 0,
            "post_gcd_source_rejection_count": sum(
                isinstance(row.get("simulator_submission"), Mapping)
                and row["simulator_submission"].get("status")
                == POST_GCD_REJECTION_SUBMISSION_STATUS_V4
                for row in events
            ),
            "generic_action_scorer_used": False,
            "deployed_contra_substitution_used": False,
        },
        "lifecycle": {
            "dynamic_v3_runtime_receipt_closure": copy.deepcopy(dict(closure)),
            "all_cursor_and_lifecycle_checks_complete": lifecycle_complete,
        },
        "inputs": inputs.receipt(),
        "controls": controls.control_receipt(),
        "source_profile": SOURCE_DEFAULT_PROFILE_V3.to_dict(),
        "runtime_contradb_profile_observed": False,
        "runner_v4_diagnostic_registration_ready": True,
        "comparison_ready": False,
    }


def _version_isolation_v4() -> JSONMap:
    return {
        "frozen_v2_source_sha256": validate_v2_execution_base_identity_v3(),
        "frozen_v4_overlay_sha256": _dynamic_v5._frozen_v4_source_sha256(),
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
    }


def run_contra260817_fury_full_policy_rollout_v4(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: Contra260817FuryFullPolicyAdapterV3,
    *,
    seed: int,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    dynamic_load: DynamicRolloutLoadV3,
    simulator_inputs: Contra260817SimulatorInputsV4 | None = None,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Run the source-default Contra diagnostic on one native-v3 load."""

    if type(adapter) is not Contra260817FuryFullPolicyAdapterV3:
        raise TypeError("adapter must be exact Contra260817FuryFullPolicyAdapterV3")
    if adapter.profile != SOURCE_DEFAULT_PROFILE_V3:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "runner-v4 diagnostic accepts only the pinned source-default profile"
        )
    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    validate_dynamic_load_request_v3(dynamic_load, raid_sim_request)
    inputs = simulator_inputs or Contra260817SimulatorInputsV4()
    if not isinstance(inputs, Contra260817SimulatorInputsV4):
        raise TypeError("simulator_inputs must be Contra260817SimulatorInputsV4 or None")
    if retain_steps is not True:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra v4 requires retain_steps=True for ordered receipts"
        )
    required = (
        "load_dynamic_v3",
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "dynamic_idle_advance_receipts",
        "parsed_dynamic_state",
        "set_target",
        "start_attack",
        "stop_cast",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    if missing:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra v4 bridge capabilities missing: " + ", ".join(missing)
        )
    controls = Contra260817SimulatorControlFacadeV4(
        bridge,
        target_bindings=inputs.target_bindings,
        item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_equipment={
            16: inputs.equipped_mainhand_name,
            17: inputs.equipped_offhand_name,
        },
    )
    facade = _dynamic_v5._DynamicV3ExecutorBridgeFacade(controls)
    core = _clone_core_v4(controls, inputs, dynamic_load.contract_sha256)
    result = core(
        facade,
        raid_sim_request,
        adapter,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=retain_steps,
    )
    if not isinstance(result, dict):
        raise Contra260817FuryFullPolicyRolloutV4Error("private core returned non-object")
    _dynamic_v5._rewrite_v5_identity(result, dynamic_load, facade.last_load_result)
    result["schema"] = ROLLOUT_SCHEMA_V4
    result["implementation_revision"] = IMPLEMENTATION_REVISION
    result["expert_id"] = POLICY_ID
    result["version_isolation"] = _version_isolation_v4()
    result["contra260817_source_identity"] = copy.deepcopy(adapter.source_verification)
    capabilities = result.get("bridge_capabilities")
    if isinstance(capabilities, dict):
        capabilities.update(
            {
                "contra260817_source_oracle_v3": True,
                "contra260817_ordered_sink_executor_v4": True,
                "native_equipment_api": callable(getattr(bridge, "equip_item_by_name", None)),
                "native_stop_attack": callable(getattr(bridge, "stop_attack", None)),
                "native_toggle_attack": callable(getattr(bridge, "toggle_attack", None)),
            }
        )
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        raise Contra260817FuryFullPolicyRolloutV4Error("bridge command contract missing")
    command.update(
        {
            "source_raw_order_preserved_within_invocation": True,
            "contra260817_full_policy_source_adapter": "Contra260817FuryFullPolicyAdapterV3",
            "contra260817_ordered_sink_executor": "execute_contra260817_ordered_sinks_v4",
            "generic_action_scorer_used": False,
            "deployed_contra_substitution_used": False,
            "path_dependent_binding_preflight_before_mutation": True,
            "source_reentry_clock": SOURCE_REENTRY_CLOCK_SCHEMA_V4,
            "source_reentry_timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V4,
            "source_reentry_retry_ms": SOURCE_REENTRY_RETRY_MS_V4,
            "source_reentry_is_policy_wait": False,
            "post_gcd_source_rejection_without_bridge_resubmission": True,
            "native_actions": [
                "set_target",
                "start_attack",
                "stop_cast",
                "act",
                "wait",
                "advance",
            ],
            "simulator_acceptance_is_client_acceptance": False,
            "simulator_result_is_game_server_outcome": False,
        }
    )
    closure = _dynamic_v5._collect_runtime_receipts_v5(
        bridge, dynamic_load, facade.last_load_result, result.get("final_state")
    )
    result["dynamic_v3_runtime_receipt_closure"] = closure
    result["contra260817_v4_receipts"] = _receipt_bundle_v4(
        result, inputs, controls, closure
    )
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise Contra260817FuryFullPolicyRolloutV4Error("rollout blockers malformed")
    permanent = (
        ("TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL", "live health is a simulator hypothesis"),
        ("TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL", "effective armor is a simulator hypothesis"),
        ("TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL", "attackability is a simulator hypothesis"),
        ("CENTRAL_IDLE_ADVANCE_MECHANISM_NOT_COMPARISON_ADMITTED", "central idle closure is not real-client fidelity"),
        ("CONTRA260817_PACKAGE_INCOMPLETE", "Contra.toc names absent files and the pinned package has zero-byte members"),
        ("CONTRA260817_PER_CHARACTER_CONTRADB_PROFILE_MISSING", "only source defaults are available; the runtime per-character ContraDB is absent"),
        ("CONTRA260817_GAME_CLIENT_LOAD_ATTESTATION_MISSING", "this package/profile lacks a post-/reload load receipt"),
        ("CONTRA260817_GAME_CLIENT_ORDERED_TRACE_MISSING", "simulator order is not an observed WoW client sink trace"),
        ("CONTRA260817_CLIENT_ACCEPTANCE_TRACE_MISSING", "simulator acceptance is not WoW client acceptance"),
        ("CONTRA260817_GAME_SERVER_OUTCOME_TRACE_MISSING", "simulator results are not Turtle WoW server outcomes"),
        ("CONTRA260817_EQUIPMENT_PATH_REQUIRES_NATIVE_API", "a reached equipment sink requires bridge.equip_item_by_name and otherwise fails before mutation"),
        ("CONTRA260817_ITEM_PATH_REQUIRES_ACTION_BINDING", "a reached item sink requires an exact item ActionRef binding and otherwise fails before mutation"),
        ("CONTRA260817_TARGET_PATH_REQUIRES_INDEX_BINDING", "a reached GUID/nearest target sink requires an explicit simulator target-index binding"),
    )
    for code, message in permanent:
        _append_blocker_once(blockers, code, message, execution_fatal=False)
    source_reentry_count = result["contra260817_v4_receipts"]["operation"][
        "source_reentry_count"
    ]
    if source_reentry_count:
        _append_blocker_once(
            blockers,
            "CONTRA260817_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY",
            "nonconsuming source invocations use a fixed runner-side 100 ms reentry cadence",
            execution_fatal=False,
            evidence={
                "count": source_reentry_count,
                "accepted_queue_count": result["contra260817_v4_receipts"]["operation"]["accepted_queue_reentry_count"],
                "timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V4,
                "requested_ms": SOURCE_REENTRY_RETRY_MS_V4,
            },
        )
    if closure.get("status") != "COMPLETE_BOUND":
        _append_blocker_once(
            blockers,
            "CONTRA260817_DYNAMIC_V3_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE",
            "dynamic-v3 armor/attackability/background/candidate/idle streams did not close",
            execution_fatal=True,
            evidence={"closure_status": closure.get("status")},
        )
    result["blocker_summary"] = _v2._blocker_summary(blockers)
    if result.get("scenario_complete") is True:
        result["status"] = "COMPLETE_SIMULATOR_ONLY_NONVOTING"
    operation = result["contra260817_v4_receipts"]["operation"]
    result["source_to_simulator_order_faithful"] = operation[
        "run_order_and_disposition_complete"
    ]
    result["ordered_projection_faithful"] = False
    result["simulator_dps_comparison_eligible"] = False
    result["historical_truth"] = False
    result["voting_eligible"] = False
    result["live_fidelity"] = False
    result["comparison_ready"] = False
    result["formal_runner_registration_authorized"] = False
    result["formal_runner_registry_modified"] = False
    result["runner_v4_diagnostic_registration_ready"] = True
    result["scientific_run_launched"] = False
    result["game_client_load_observed"] = False
    result["game_client_ordered_trace_observed"] = False
    result["client_acceptance_observed"] = False
    result["game_server_outcome_observed"] = False
    result["claims_excluded"] = list(
        dict.fromkeys(
            list(result.get("claims_excluded") or [])
            + [
                "source-default profile as the player's runtime ContraDB",
                "simulator acceptance as WoW client acceptance",
                "simulator result as Turtle WoW server outcome",
                "Contra260817 v4 diagnostic as a formal/live-fidelity baseline",
            ]
        )
    )
    for step in result.get("steps") or []:
        state = step.get("simulator_state_before") if isinstance(step, dict) else None
        if isinstance(state, Mapping):
            step["dynamic_v3_live_target_state_before"] = copy.deepcopy(
                state["dynamic_target_semantics"]
            )
            step["dynamic_v3_idle_state_before"] = copy.deepcopy(
                state["dynamic_idle_advance"]
            )
    result.pop("content_address", None)
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V4,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: value for key, value in result.items() if key != "content_address"}
        ),
    }
    return validate_contra260817_fury_full_policy_rollout_v4(
        result, dynamic_load=dynamic_load
    )


def _canonical_sha256(value: Any) -> str:
    return _dynamic_v5._canonical_sha256_v5(value)


def _validate_dynamic_projection(
    raw: Mapping[str, Any], dynamic_load: DynamicRolloutLoadV3 | None
) -> None:
    projected = copy.deepcopy(dict(raw))
    projected["schema"] = _dynamic_v5.ROLLOUT_SCHEMA_V5
    projected["implementation_revision"] = _dynamic_v5.IMPLEMENTATION_REVISION
    projected["content_address"] = {
        "schema": _dynamic_v5.ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _dynamic_v5._canonical_sha256_v5(
            {key: value for key, value in projected.items() if key != "content_address"}
        ),
    }
    _dynamic_v5.validate_fury_full_policy_rollout_v5(
        projected, dynamic_load=dynamic_load
    )


def validate_contra260817_fury_full_policy_rollout_v4(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
) -> JSONMap:
    try:
        raw = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            f"Contra v4 rollout is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict) or (
        raw.get("schema") != ROLLOUT_SCHEMA_V4
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("expert_id") != POLICY_ID
        or raw.get("runner_v4_diagnostic_registration_ready") is not True
    ):
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra v4 identity mismatch")
    for field in (
        "ordered_projection_faithful",
        "simulator_dps_comparison_eligible",
        "historical_truth",
        "voting_eligible",
        "live_fidelity",
        "comparison_ready",
        "formal_runner_registration_authorized",
        "formal_runner_registry_modified",
        "scientific_run_launched",
        "game_client_load_observed",
        "game_client_ordered_trace_observed",
        "client_acceptance_observed",
        "game_server_outcome_observed",
    ):
        if raw.get(field) is not False:
            raise Contra260817FuryFullPolicyRolloutV4Error(f"{field} must remain false")
    if raw.get("version_isolation") != _version_isolation_v4():
        raise Contra260817FuryFullPolicyRolloutV4Error("dynamic-v3 isolation mismatch")
    command = raw.get("bridge_command_contract")
    if not isinstance(command, Mapping) or (
        command.get("initial_load_command") != "load_dynamic_v3"
        or command.get("dynamic_config_schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
        or command.get("source_raw_order_preserved_within_invocation") is not True
        or command.get("contra260817_full_policy_source_adapter") != "Contra260817FuryFullPolicyAdapterV3"
        or command.get("contra260817_ordered_sink_executor") != "execute_contra260817_ordered_sinks_v4"
        or command.get("generic_action_scorer_used") is not False
        or command.get("deployed_contra_substitution_used") is not False
        or command.get("path_dependent_binding_preflight_before_mutation") is not True
        or command.get("source_reentry_clock") != SOURCE_REENTRY_CLOCK_SCHEMA_V4
        or command.get("source_reentry_timing_authority") != SOURCE_REENTRY_TIMING_AUTHORITY_V4
        or command.get("source_reentry_retry_ms") != SOURCE_REENTRY_RETRY_MS_V4
        or command.get("source_reentry_is_policy_wait") is not False
        or command.get("post_gcd_source_rejection_without_bridge_resubmission")
        is not True
    ):
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra bridge command boundary mismatch")
    receipt = raw.get("contra260817_v4_receipts")
    closure = raw.get("dynamic_v3_runtime_receipt_closure")
    if not isinstance(receipt, Mapping) or (
        receipt.get("schema") != RECEIPT_BUNDLE_SCHEMA_V4
        or receipt.get("runtime_contradb_profile_observed") is not False
        or receipt.get("comparison_ready") is not False
    ):
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra receipt identity mismatch")
    lifecycle = receipt.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("dynamic_v3_runtime_receipt_closure") != closure:
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra runtime closure binding mismatch")
    operation = receipt.get("operation")
    if not isinstance(operation, Mapping) or (
        operation.get("generic_action_scorer_used") is not False
        or operation.get("deployed_contra_substitution_used") is not False
        or operation.get("run_order_and_disposition_complete") is not raw.get("source_to_simulator_order_faithful")
        or operation.get("source_reentry_timing_authority") != SOURCE_REENTRY_TIMING_AUTHORITY_V4
        or operation.get("source_reentry_retry_ms") != SOURCE_REENTRY_RETRY_MS_V4
        or operation.get("source_reentry_exact_client_cadence") is not False
        or operation.get("source_reentry_policy_action_count") != 0
    ):
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra operation receipt mismatch")
    observed_reentry_count = 0
    observed_accepted_queue_reentry_count = 0
    observed_post_gcd_rejection_count = 0
    for index, step in enumerate(raw.get("steps") or []):
        if not isinstance(step, Mapping):
            raise Contra260817FuryFullPolicyRolloutV4Error("Contra step malformed")
        proposal = step.get("proposal")
        execution = step.get("ordered_execution")
        if not isinstance(proposal, Mapping) or not isinstance(execution, Mapping) or (
            execution.get("expert_id") != POLICY_ID
            or execution.get("source_decision") != proposal
            or execution.get("raw_sink_order") != proposal.get("raw_sink_order")
        ):
            raise Contra260817FuryFullPolicyRolloutV4Error(f"Contra step {index} lost source binding")
        events = execution.get("sink_events")
        raw_order = proposal.get("raw_sink_order")
        if not isinstance(events, list) or not isinstance(raw_order, list) or [
            event.get("source_sink") for event in events if isinstance(event, Mapping)
        ] != raw_order:
            raise Contra260817FuryFullPolicyRolloutV4Error(
                f"Contra step {index} sink ledger is not source ordered"
            )
        prior_consuming_gcd: tuple[int, str] | None = None
        for order, (source_sink, event) in enumerate(
            zip(raw_order, events), start=1
        ):
            if not isinstance(source_sink, Mapping) or not isinstance(event, Mapping):
                raise Contra260817FuryFullPolicyRolloutV4Error(
                    f"Contra step {index} sink event {order} is malformed"
                )
            submission = event.get("simulator_submission")
            acceptance = event.get("simulator_acceptance")
            submission_status = (
                submission.get("status") if isinstance(submission, Mapping) else None
            )
            acceptance_status = (
                acceptance.get("status") if isinstance(acceptance, Mapping) else None
            )
            post_gcd_rejection = (
                submission_status == POST_GCD_REJECTION_SUBMISSION_STATUS_V4
                or acceptance_status == POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4
            )
            if post_gcd_rejection:
                if not _valid_post_gcd_rejection_v4(
                    event, source_sink, prior_consuming_gcd
                ):
                    raise Contra260817FuryFullPolicyRolloutV4Error(
                        f"Contra step {index} post-GCD rejection {order} is invalid"
                    )
                observed_post_gcd_rejection_count += 1
            operation_contract = event.get("operation_contract")
            source_attempt = event.get("source_attempt")
            result_bearing = (
                source_sink.get("channel") == "gcd"
                and submission_status == "SUBMITTED"
                and acceptance_status == "ACCEPTED"
                and isinstance(operation_contract, Mapping)
                and operation_contract.get("canonical_action")
                in _RESULT_BEARING_GCD_ACTIONS_V4
            )
            attempt_id = (
                source_attempt.get("attempt_id")
                if isinstance(source_attempt, Mapping)
                else None
            )
            expected_attempt_id = f"decision-{index}:sink-{order}"
            if (
                result_bearing and attempt_id != expected_attempt_id
            ) or (not result_bearing and attempt_id is not None):
                raise Contra260817FuryFullPolicyRolloutV4Error(
                    f"Contra step {index} result attempt ID contract mismatch"
                )
            accepted_action = _accepted_consuming_gcd_v4(event, source_sink)
            if accepted_action is not None:
                prior_consuming_gcd = (order, accepted_action)
        reentry_trigger = _source_reentry_trigger_v4(events)
        reentry_expected = (
            proposal.get("gcd", {}).get("action") != WAIT_ACTION
            or reentry_trigger == "ACCEPTED_SWING_QUEUE_NONCONSUMING"
        ) and (
            execution.get("execution_blocked") is False
            and execution.get("decision_consumed") is False
            and reentry_trigger is not None
        )
        reentry = execution.get("source_reentry_clock")
        if reentry_expected != isinstance(reentry, Mapping):
            raise Contra260817FuryFullPolicyRolloutV4Error(
                f"Contra step {index} source reentry clock mismatch"
            )
        if isinstance(reentry, Mapping):
            observed_reentry_count += 1
            observed_accepted_queue_reentry_count += (
                reentry.get("trigger") == "ACCEPTED_SWING_QUEUE_NONCONSUMING"
            )
            final_state = execution.get("final_state")
            scheduled_at = reentry.get("scheduled_at_time_ms")
            idle = final_state.get("dynamic_idle_advance") if isinstance(final_state, Mapping) else None
            horizon_ms = idle.get("horizon_ms") if isinstance(idle, Mapping) else None
            nominal_wake = (
                scheduled_at + SOURCE_REENTRY_RETRY_MS_V4
                if type(scheduled_at) is int
                else None
            )
            expected_boundary = (
                min(nominal_wake, horizon_ms)
                if nominal_wake is not None and type(horizon_ms) is int
                else nominal_wake
            )
            if (
                reentry.get("schema") != SOURCE_REENTRY_CLOCK_SCHEMA_V4
                or reentry.get("trigger") != reentry_trigger
                or reentry.get("timing_authority") != SOURCE_REENTRY_TIMING_AUTHORITY_V4
                or reentry.get("requested_ms") != SOURCE_REENTRY_RETRY_MS_V4
                or reentry.get("exact_client_cadence") is not False
                or reentry.get("policy_action") is not False
                or reentry.get("source_sink") is not False
                or execution.get("wait_event") is not None
                or not isinstance(final_state, Mapping)
                or final_state.get("time_ms") != scheduled_at
                or final_state.get("needs_input") is not False
                or reentry.get("nominal_wake_time_ms") != nominal_wake
                or reentry.get("expected_next_boundary_time_ms") != expected_boundary
            ):
                raise Contra260817FuryFullPolicyRolloutV4Error(
                    f"Contra step {index} source reentry clock invalid"
                )
        if not isinstance(step.get("dynamic_v3_live_target_state_before"), Mapping) or not isinstance(
            step.get("dynamic_v3_idle_state_before"), Mapping
        ):
            raise Contra260817FuryFullPolicyRolloutV4Error(
                f"Contra step {index} lacks live dynamic-v3 state"
            )
    if operation.get("source_reentry_count") != observed_reentry_count:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra source reentry receipt count mismatch"
        )
    if operation.get("accepted_queue_reentry_count") != observed_accepted_queue_reentry_count:
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra accepted queue reentry receipt count mismatch"
        )
    if (
        operation.get("post_gcd_source_rejection_count")
        != observed_post_gcd_rejection_count
    ):
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra post-GCD rejection receipt count mismatch"
        )
    required_codes = {
        "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
        "CENTRAL_IDLE_ADVANCE_MECHANISM_NOT_COMPARISON_ADMITTED",
        "CONTRA260817_PACKAGE_INCOMPLETE",
        "CONTRA260817_PER_CHARACTER_CONTRADB_PROFILE_MISSING",
        "CONTRA260817_EQUIPMENT_PATH_REQUIRES_NATIVE_API",
        "CONTRA260817_ITEM_PATH_REQUIRES_ACTION_BINDING",
        "CONTRA260817_TARGET_PATH_REQUIRES_INDEX_BINDING",
    }
    blockers = raw.get("blockers")
    codes = {
        row.get("code") for row in blockers if isinstance(row, Mapping)
    } if isinstance(blockers, list) else set()
    if not required_codes.issubset(codes):
        raise Contra260817FuryFullPolicyRolloutV4Error("mandatory Contra blockers missing")
    reentry_blocker = "CONTRA260817_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY"
    if (observed_reentry_count > 0) != (reentry_blocker in codes):
        raise Contra260817FuryFullPolicyRolloutV4Error(
            "Contra source reentry blocker mismatch"
        )
    _validate_dynamic_projection(raw, dynamic_load)
    expected_content = {
        "schema": ROLLOUT_CONTENT_SCHEMA_V4,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: value for key, value in raw.items() if key != "content_address"}
        ),
    }
    if raw.get("content_address") != expected_content:
        raise Contra260817FuryFullPolicyRolloutV4Error("Contra content address mismatch")
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if "load_dynamic_v1" in encoded or "load_dynamic_v2" in encoded:
        raise Contra260817FuryFullPolicyRolloutV4Error("older dynamic load leaked")
    return raw


__all__ = (
    "Contra260817FuryFullPolicyRolloutV4Error",
    "Contra260817SimulatorInputsV4",
    "IMPLEMENTATION_REVISION",
    "RECEIPT_BUNDLE_SCHEMA_V4",
    "ROLLOUT_SCHEMA_V4",
    "run_contra260817_fury_full_policy_rollout_v4",
    "validate_contra260817_fury_full_policy_rollout_v4",
)
