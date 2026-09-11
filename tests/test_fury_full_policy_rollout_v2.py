from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_contra_adapter_v2 import (
    ContraDeployedFuryAdapterV2,
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from o2o_dps.fury_expert_adapters import (
    CatFurySourceAdapter,
    ContraNewCandidateAdapter,
)
from o2o_dps.fury_full_policy_rollout_v2 import (
    CONTRA260817_POLICY_ID,
    DynamicRolloutLoadV1,
    HealthPercentPointV2,
    ServerResultBatchV2,
    ServerResultEventV2,
    ServerTargetResultV2,
    TargetSemanticsContextV2,
    TargetSemanticsModeV2,
    _combat_state,
    dynamic_rollout_load_from_adapter_wire_v1,
    _resolve_target_semantics,
    _right_censor_terminal_active_hardcast,
    contra260817_required_baseline_status_v2,
    run_fury_full_policy_rollout_v2,
)
from o2o_dps.fury_ordered_sink_executor_v2 import ControlSinkResultV2
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    FuryPairedRunnerError,
    SCENARIO_MODEL_KIND,
    SINGLE_BRIDGE_MODE,
    TARGET_CONTEXT_BUNDLE_KIND,
    build_exact_static_request_semantics_receipt,
    build_runner_plan,
    execute_shard,
    reduce_shards,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_shard,
    validate_reduction_receipt,
)
from o2o_dps.sim_bridge import (
    ActResult,
    AvailableAction,
    BackgroundDamageEventV1,
    DynamicLoadReceiptV1,
    DynamicLoadResultV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
    CancelQueueResult,
    SetTargetResult,
)


SOURCE_SHA = "1" * 64
CORPUS_SHA = "2" * 64


def _evidence(
    kind: ContraEvidenceKindV2 = ContraEvidenceKindV2.PINNED_STATIC_INPUT,
    *,
    hypothesis_id: str | None = None,
) -> ContraFieldEvidenceV2:
    if kind is ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS:
        return ContraFieldEvidenceV2(
            kind,
            corpus_sha256=CORPUS_SHA,
            hypothesis_id=hypothesis_id or "fixture-sensitivity-v1",
        )
    return ContraFieldEvidenceV2(kind, source_sha256=SOURCE_SHA)


def _request(*, targets: int = 1) -> dict[str, object]:
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "distanceFromTarget": 3,
                            "equipment": {"items": [{}, {}]},
                        }
                    ]
                }
            ]
        },
        "encounter": {
            "duration": 2,
            "targets": [
                {"level": 63, "name": f"Target {index}"}
                for index in range(targets)
            ],
        },
        "simOptions": {"iterations": 1},
    }


def _exact_context(index: int = 0) -> TargetSemanticsContextV2:
    return TargetSemanticsContextV2(
        context_id=f"exact-target-{index}",
        mode=TargetSemanticsModeV2.DECLARED_EXACT,
        target_index=index,
        target_classification=ContraTargetClassificationV2.WORLDBOSS,
        target_name=f"Target {index}",
        equipped_item_names=(),
        target_classification_evidence=_evidence(
            ContraEvidenceKindV2.OBSERVED_SOURCE
        ),
        target_name_evidence=_evidence(ContraEvidenceKindV2.OBSERVED_SOURCE),
        equipment_evidence=_evidence(),
        target_position_evidence=_evidence(),
        target_health_pct_evidence=_evidence(
            ContraEvidenceKindV2.SIMULATOR_STATE
        ),
        target_max_health_evidence=_evidence(
            ContraEvidenceKindV2.SIMULATOR_STATE
        ),
    )


def _exact_context_receipt(context: TargetSemanticsContextV2) -> dict[str, object]:
    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": context.target_index,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "equipped_item_count": len(context.equipped_item_names),
        "target_max_health": context.target_max_health,
        "health_pct_schedule": [
            {"time_ms": point.time_ms, "health_pct": point.health_pct}
            for point in context.health_pct_schedule
        ],
        "exact_by_declared_contract": True,
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        },
    }


def _sensitivity_context(index: int = 0) -> TargetSemanticsContextV2:
    return TargetSemanticsContextV2(
        context_id=f"low-hp-sensitivity-{index}",
        mode=TargetSemanticsModeV2.SENSITIVITY,
        target_index=index,
        target_classification=ContraTargetClassificationV2.ELITE,
        target_name=f"Target {index}",
        equipped_item_names=(),
        target_classification_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            hypothesis_id="fixture-classification-v1",
        ),
        target_name_evidence=_evidence(ContraEvidenceKindV2.OBSERVED_SOURCE),
        equipment_evidence=_evidence(),
        target_position_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            hypothesis_id="fixture-position-v1",
        ),
        target_health_pct_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            hypothesis_id="fixture-health-curve-v1",
        ),
        target_max_health_evidence=_evidence(
            ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
            hypothesis_id="fixture-max-health-v1",
        ),
        target_max_health=20_000,
        health_pct_schedule=(
            HealthPercentPointV2(0, 100.0),
            HealthPercentPointV2(2_000, 0.0),
        ),
    )


class _FullBridge:
    def __init__(
        self,
        *,
        omit_actions: set[object] | None = None,
        reject_actions: set[object] | None = None,
        two_hand: bool = False,
        casting_slam: bool = False,
        rage: float = 100.0,
        health_known: bool = True,
    ) -> None:
        self.omit_actions = omit_actions or set()
        self.reject_actions = reject_actions or set()
        self.two_hand = two_hand
        self.casting_slam = casting_slam
        self.rage = rage
        self.health_known = health_known
        self.calls: list[tuple[str, object]] = []
        self.duration_ms = 2_000
        self.num_targets = 1
        self.time_ms = 0
        self.damage = 0.0
        self.needs_input = True
        self.finished = False
        self.target_index = 0
        self.pending_action = None
        self.completed_action = None
        self.outstanding_results: list[tuple[str, object]] = []
        self.act_attempt_ids: list[str | None] = []
        self.pending_wait = 0
        self.decision_index = 0
        self.auras: list[dict[str, object]] = []

    def load(self, request, seed):
        self.calls.append(("load", seed))
        self.duration_ms = round(float(request["encounter"]["duration"]) * 1000)
        self.num_targets = len(request["encounter"]["targets"])
        self.time_ms = 0
        self.damage = 0.0
        self.needs_input = True
        self.finished = False
        self.target_index = 0
        self.pending_action = None
        self.completed_action = None
        self.outstanding_results = []
        self.act_attempt_ids = []
        self.pending_wait = 0
        self.decision_index = 0
        self.auras = [
            {"label": "Battle Shout", "remaining_ms": 600_000, "stacks": 0},
            {"label": "Berserker Stance", "remaining_ms": 1, "stacks": 0},
            {"label": "Flurry", "remaining_ms": 1_000, "stacks": 3},
        ]
        return self._state()

    def state(self):
        self.calls.append(("state", None))
        return self._state()

    def actions(self):
        self.calls.append(("actions", None))
        gcd = {
            "warrior.bloodthirst",
            "warrior.whirlwind",
            "warrior.slam",
            "warrior.execute",
            "warrior.hamstring",
            "warrior.pummel",
            "warrior.battle_shout",
            "warrior.sunder_armor",
        }
        keys = (
            *sorted(gcd),
            "warrior.berserker_stance",
            "warrior.battle_stance",
            "warrior.defensive_stance",
            "warrior.bloodrage",
            "warrior.death_wish",
        )
        refs = [ACTION_KEY_TO_REF[key] for key in keys]
        refs.extend(
            (
                QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
                QUEUE_REFS[SwingQueueOp.CLEAVE],
            )
        )
        return [
            AvailableAction(
                index=index,
                action=ref,
                label=f"action-{ref.spell_id}-{ref.tag}",
                legal=self.needs_input,
                ready_in_ms=0,
                triggers_gcd=(ref in {ACTION_KEY_TO_REF[key] for key in gcd}),
            )
            for index, ref in enumerate(refs)
            if ref not in self.omit_actions
        ]

    def act(self, action, *, attempt_id=None):
        self.calls.append(("act", action))
        self.act_attempt_ids.append(attempt_id)
        row = next((row for row in self.actions() if row.action == action), None)
        if row is None or not row.legal or action in self.reject_actions:
            return ActResult(False, False, self.finished, self.needs_input, self._state())
        if action in {
            QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
            QUEUE_REFS[SwingQueueOp.CLEAVE],
        }:
            self.auras = [
                aura
                for aura in self.auras
                if not (
                    isinstance(aura.get("action"), dict)
                    and aura["action"].get("tag") == 1
                )
            ]
            self.auras.append(
                {
                    "label": "queue",
                    "action": {"spell_id": action.spell_id, "tag": 1},
                    "remaining_ms": 10_000,
                    "stacks": 0,
                }
            )
            return ActResult(True, False, False, True, self._state())
        if row.triggers_gcd:
            result_bearing_refs = {
                ACTION_KEY_TO_REF[action_key]
                for action_key in (
                    "warrior.bloodthirst",
                    "warrior.whirlwind",
                    "warrior.slam",
                    "warrior.execute",
                    "warrior.hamstring",
                    "warrior.pummel",
                    "warrior.sunder_armor",
                )
            }
            if action in result_bearing_refs:
                if not isinstance(attempt_id, str) or not attempt_id:
                    raise AssertionError("result-bearing fixture action lacks attempt_id")
                self.outstanding_results.append((attempt_id, action))
            elif attempt_id is not None:
                raise AssertionError("non-result-bearing fixture action has attempt_id")
            self.pending_action = action
            self.needs_input = False
            return ActResult(True, True, False, False, self._state())
        return ActResult(True, False, False, True, self._state())

    def cancel_queue(self):
        self.calls.append(("cancel_queue", None))
        before = len(self.auras)
        self.auras = [
            aura
            for aura in self.auras
            if not (
                isinstance(aura.get("action"), dict)
                and aura["action"].get("tag") == 1
            )
        ]
        return CancelQueueResult(
            len(self.auras) != before,
            False,
            self.finished,
            self.needs_input,
            self._state(),
        )

    def set_target(self, target_index):
        self.calls.append(("set_target", target_index))
        changed = target_index != self.target_index
        self.target_index = target_index
        return SetTargetResult(
            changed, target_index, self.finished, self.needs_input, self._state()
        )

    def wait(self, wait_ms):
        self.calls.append(("wait", wait_ms))
        self.pending_wait = wait_ms
        self.needs_input = False
        return self._state()

    def advance(self):
        self.calls.append(("advance", None))
        amount = self.pending_wait or 1_000
        self.time_ms = min(self.duration_ms, self.time_ms + amount)
        self.completed_action = self.pending_action
        if self.pending_action is not None:
            self.damage += 100.0
        else:
            self.damage += amount / 20.0
        self.pending_action = None
        self.pending_wait = 0
        self.finished = self.time_ms >= self.duration_ms
        self.needs_input = not self.finished
        self.casting_slam = False
        self.decision_index += 1
        return self._state()

    def start_attack(self):
        self.calls.append(("start_attack", None))
        return ControlSinkResultV2(True, False, self._state())

    def stop_cast(self):
        self.calls.append(("stop_cast", None))
        self.casting_slam = False
        return ControlSinkResultV2(True, False, self._state())

    def server_results_since_last_decision(self, attempt_ids=()):
        attempt_ids = tuple(attempt_ids)
        self.calls.append(("server_results_since_last_decision", attempt_ids))
        if attempt_ids != tuple(row[0] for row in self.outstanding_results):
            raise AssertionError("fixture did not receive the complete outstanding ledger")
        resolved = [
            row
            for row in self.outstanding_results
            if self.completed_action is not None and row[1] == self.completed_action
        ]
        pending = [row for row in self.outstanding_results if row not in resolved]
        events = tuple(
            ServerResultEventV2(
                time_ms=self.time_ms,
                outcome="HIT",
                attempt_id=attempt_id,
                damage=100.0,
                action=action,
                target_results=(
                    ServerTargetResultV2(
                        target_index=0,
                        outcome="HIT",
                        damage=100.0,
                    ),
                ),
            )
            for attempt_id, action in resolved
        )
        self.outstanding_results = pending
        self.completed_action = None
        return ServerResultBatchV2(
            complete_through_time_ms=self.time_ms,
            events=events,
            pending_attempt_ids=tuple(row[0] for row in pending),
        )

    def _state(self):
        health_pct = max(0.0, 100.0 * (1.0 - self.time_ms / self.duration_ms))
        state = {
            "time_ms": self.time_ms,
            "remaining_ms": max(0, self.duration_ms - self.time_ms),
            "finished": self.finished,
            "needs_input": self.needs_input,
            "power": {"type": "rage", "current": self.rage, "maximum": 100.0},
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1_000,
            "mh_swing_duration_ms": 2_400,
            "oh_swing_remaining_ms": None if self.two_hand else 800,
            "current_cast": (
                {
                    "action": ACTION_KEY_TO_REF["warrior.slam"].to_wire(),
                    "remaining_ms": 800,
                    "duration_ms": 1_500,
                }
                if self.casting_slam
                else None
            ),
            "target_index": self.target_index,
            "num_targets": self.num_targets,
            "target_health_known": self.health_known,
            "target_health": 50_000 * health_pct / 100.0,
            "target_health_max": 50_000.0,
            "target_health_percent": health_pct,
            "execute_phase_20": health_pct <= 20.0,
            "target_armor": 3_000.0,
            "effective_target_armor": 3_000.0,
            "damage_done": self.damage,
            "auras": copy.deepcopy(self.auras),
        }
        return state


class _CurrentBridge(_FullBridge):
    start_attack = None
    stop_cast = None
    server_results_since_last_decision = None


class _DynamicFullBridge(_FullBridge):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dynamic_config = None
        self.dynamic_generation = 0

    def load(self, request, seed):
        raise AssertionError("dynamic rollout silently called static load")

    def load_dynamic_v1(self, request, seed, config):
        self.calls.append(("load_dynamic_v1", (seed, config.content_sha256)))
        self.duration_ms = round(float(request["encounter"]["duration"]) * 1000)
        self.num_targets = len(request["encounter"]["targets"])
        self.time_ms = 0
        self.damage = 0.0
        self.needs_input = True
        self.finished = False
        self.target_index = 0
        self.pending_action = None
        self.completed_action = None
        self.outstanding_results = []
        self.act_attempt_ids = []
        self.pending_wait = 0
        self.decision_index = 0
        self.auras = [
            {"label": "Battle Shout", "remaining_ms": 600_000, "stacks": 0},
            {"label": "Berserker Stance", "remaining_ms": 1, "stacks": 0},
            {"label": "Flurry", "remaining_ms": 1_000, "stacks": 3},
        ]
        self.dynamic_config = config
        self.dynamic_generation += 1
        receipt = DynamicLoadReceiptV1(
            schema="o2o_dynamic_team_background/v1",
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            target_count=len(config.target_health),
            background_event_count=len(config.background_damage_events),
            same_timestamp_order=config.same_timestamp_order,
            retarget_mode=config.retarget_mode,
        )
        return DynamicLoadResultV1(receipt=receipt, state=self._state())

    def _state(self):
        state = super()._state()
        if self.dynamic_config is None:
            return state
        initial = self.dynamic_config.target_health[0].health
        applied = min(initial, self.damage)
        current = initial - applied
        dead = current == 0
        state.update(
            {
                "num_targets": 0 if dead else 1,
                "total_target_count": 1,
                "target_health": current,
                "target_health_max": initial,
                "target_health_percent": 100.0 * current / initial,
                "execute_phase_20": current / initial <= 0.2,
                "encounter_damage_taken": applied,
                "encounter_health_target": initial,
                "dynamic_team_background": {
                    "schema": "o2o_dynamic_team_background/v1",
                    "config_digest": self.dynamic_config.content_sha256,
                    "environment_generation": self.dynamic_generation,
                    "same_timestamp_order": self.dynamic_config.same_timestamp_order,
                    "retarget_mode": self.dynamic_config.retarget_mode,
                    "retarget_required": False,
                    "simulated_damage_applied": applied,
                    "background_damage_applied": 0.0,
                    "combined_damage_applied": applied,
                    "background_events_processed": 0,
                    "background_events_total": 0,
                    "background_events_canceled": 0,
                    "candidate_events_processed": self.decision_index,
                    "candidate_events_canceled": 0,
                    "damage_applications_total": self.decision_index,
                    "targets": [
                        {
                            "target_index": 0,
                            "initial_health": initial,
                            "current_health": current,
                            "dead": dead,
                            **({"death_time_ms": self.time_ms} if dead else {}),
                            "simulated_damage_applied": applied,
                            "background_damage_applied": 0.0,
                        }
                    ],
                },
            }
        )
        return state


class _DynamicRuntimeDriftBridge(_DynamicFullBridge):
    def advance(self):
        state = super().advance()
        state["total_target_count"] = 2
        return state


class _NoStopBridge(_FullBridge):
    stop_cast = None


class _WrongResultActionBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        batch = super().server_results_since_last_decision(attempt_ids)
        if not batch.events:
            return batch
        event = batch.events[0]
        return ServerResultBatchV2(
            complete_through_time_ms=batch.complete_through_time_ms,
            events=(
                ServerResultEventV2(
                    time_ms=event.time_ms,
                    outcome=event.outcome,
                    attempt_id=event.attempt_id,
                    damage=event.damage,
                    action=ACTION_KEY_TO_REF["warrior.whirlwind"],
                    target_results=event.target_results,
                ),
            ),
            pending_attempt_ids=batch.pending_attempt_ids,
        )


class _WrongAttemptOrderBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        batch = super().server_results_since_last_decision(attempt_ids)
        if not batch.events:
            return batch
        event = batch.events[0]
        return ServerResultBatchV2(
            complete_through_time_ms=batch.complete_through_time_ms,
            events=(
                ServerResultEventV2(
                    time_ms=event.time_ms,
                    outcome=event.outcome,
                    attempt_id="wrong-attempt-id",
                    damage=event.damage,
                    action=event.action,
                    target_results=event.target_results,
                ),
            ),
            pending_attempt_ids=batch.pending_attempt_ids,
        )


class _OneBoundaryDelayedBridge(_FullBridge):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.delayed_once = False

    def server_results_since_last_decision(self, attempt_ids=()):
        attempt_ids = tuple(attempt_ids)
        if attempt_ids and not self.delayed_once:
            self.delayed_once = True
            self.calls.append(("server_results_since_last_decision", attempt_ids))
            return ServerResultBatchV2(
                complete_through_time_ms=self.time_ms,
                events=(),
                pending_attempt_ids=attempt_ids,
            )
        return super().server_results_since_last_decision(attempt_ids)


class _AlwaysPendingBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        attempt_ids = tuple(attempt_ids)
        self.calls.append(("server_results_since_last_decision", attempt_ids))
        return ServerResultBatchV2(
            complete_through_time_ms=self.time_ms,
            events=(),
            pending_attempt_ids=attempt_ids,
        )


class _FutureCompletenessBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        batch = super().server_results_since_last_decision(attempt_ids)
        return ServerResultBatchV2(
            complete_through_time_ms=batch.complete_through_time_ms + 1,
            events=batch.events,
            pending_attempt_ids=batch.pending_attempt_ids,
        )


class _ResultBeforeAcceptanceBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        batch = super().server_results_since_last_decision(attempt_ids)
        if self.time_ms < self.duration_ms or not batch.events:
            return batch
        return ServerResultBatchV2(
            complete_through_time_ms=batch.complete_through_time_ms,
            events=tuple(
                ServerResultEventV2(
                    time_ms=0,
                    outcome=event.outcome,
                    attempt_id=event.attempt_id,
                    damage=event.damage,
                    action=event.action,
                    target_results=event.target_results,
                )
                for event in batch.events
            ),
            pending_attempt_ids=batch.pending_attempt_ids,
        )


class _ResultAfterStateBridge(_FullBridge):
    def server_results_since_last_decision(self, attempt_ids=()):
        batch = super().server_results_since_last_decision(attempt_ids)
        if not batch.events:
            return batch
        return ServerResultBatchV2(
            complete_through_time_ms=batch.complete_through_time_ms,
            events=tuple(
                ServerResultEventV2(
                    time_ms=batch.complete_through_time_ms + 1,
                    outcome=event.outcome,
                    attempt_id=event.attempt_id,
                    damage=event.damage,
                    action=event.action,
                    target_results=event.target_results,
                )
                for event in batch.events
            ),
            pending_attempt_ids=batch.pending_attempt_ids,
        )


class _EarlyFinishedZeroRemainingBridge(_FullBridge):
    def advance(self):
        state = super().advance()
        if self.time_ms < self.duration_ms:
            self.finished = True
            self.needs_input = False
            state = self._state()
            state["remaining_ms"] = 0
        return state

    def state(self):
        state = super().state()
        if self.finished and self.time_ms < self.duration_ms:
            state["remaining_ms"] = 0
        return state


class _CallSentinel:
    expert_id = CONTRA260817_POLICY_ID

    def __init__(self) -> None:
        self.called = False

    def propose(self, state):
        self.called = True
        raise AssertionError("Contra260817 proposal must not be called")


class FuryFullPolicyRolloutV2Tests(unittest.TestCase):
    def test_dynamic_load_identity_changes_with_request_seed_or_event(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        base_config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),),
            background_damage_events=(
                BackgroundDamageEventV1(0, 10, 0, "team-hit", 1.0),
            ),
        )
        changed_config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),),
            background_damage_events=(
                BackgroundDamageEventV1(0, 10, 0, "team-hit", 2.0),
            ),
        )
        changed_request = copy.deepcopy(request)
        changed_request["raid"]["parties"][0]["players"][0][
            "distanceFromTarget"
        ] = 4

        identities = {
            DynamicRolloutLoadV1.bind(request, 7, base_config).contract_sha256,
            DynamicRolloutLoadV1.bind(changed_request, 7, base_config).contract_sha256,
            DynamicRolloutLoadV1.bind(request, 8, base_config).contract_sha256,
            DynamicRolloutLoadV1.bind(request, 7, changed_config).contract_sha256,
        }
        self.assertEqual(4, len(identities))

    def test_generator_wire_is_consumed_without_manual_dynamic_reconstruction(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),)
        )
        wire = {
            "command": "load_dynamic_v1",
            "request": request,
            "seed": 77,
            "dynamic": config.to_wire(),
        }

        parsed_request, contract = dynamic_rollout_load_from_adapter_wire_v1(wire)

        self.assertEqual(request, parsed_request)
        self.assertEqual(77, contract.seed)
        self.assertEqual(config, contract.config)
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            contract.request_sha256,
        )
        with self.assertRaisesRegex(Exception, "field set mismatch"):
            dynamic_rollout_load_from_adapter_wire_v1({**wire, "ignored": True})

    def test_dynamic_full_policy_uses_typed_load_and_records_exact_binding(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),)
        )
        dynamic_load = DynamicRolloutLoadV1.bind(request, 2026091101, config)
        bridge = _DynamicFullBridge()

        result = run_fury_full_policy_rollout_v2(
            bridge,
            request,
            CatFurySourceAdapter(),
            seed=2026091101,
            target_contexts={0: _exact_context()},
            dynamic_load=dynamic_load,
        )

        self.assertEqual("COMPLETE_FAITHFUL", result["status"])
        self.assertEqual("DYNAMIC_ALL_TARGETS_DEAD", result["configured_completion"]["mode"])
        self.assertTrue(result["configured_completion"]["all_targets_dead"])
        self.assertEqual(
            ["load_dynamic_v1"],
            [name for name, _ in bridge.calls if name in {"load", "load_dynamic_v1"}],
        )
        binding = result["dynamic_load_binding"]
        self.assertEqual(dynamic_load.contract_sha256, binding["contract_sha256"])
        self.assertNotIn("contract", binding)
        self.assertEqual(2026091101, binding["simulator_seed"])
        self.assertTrue(binding["load_succeeded"])
        self.assertEqual(config.content_sha256, binding["bridge_receipt"]["config_digest"])
        self.assertEqual(
            ["4069000000000000"],
            binding["target_health_ieee754_binary64_hex"],
        )

    def test_dynamic_full_policy_rejects_runtime_target_binding_drift(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),)
        )
        dynamic_load = DynamicRolloutLoadV1.bind(request, 1, config)

        result = run_fury_full_policy_rollout_v2(
            _DynamicRuntimeDriftBridge(),
            request,
            CatFurySourceAdapter(),
            seed=1,
            target_contexts={0: _exact_context()},
            dynamic_load=dynamic_load,
        )

        self.assertEqual("INCOMPLETE_BLOCKED", result["status"])
        self.assertIn(
            "BRIDGE_ADVANCE_AFTER_DECISION_ERROR",
            {row["code"] for row in result["blockers"]},
        )

    def test_dynamic_full_policy_rejects_request_or_health_rebinding_before_bridge(self):
        request = _request()
        request["encounter"]["useHealth"] = True
        request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        config = DynamicTeamBackgroundConfigV1(
            target_health=(DynamicTargetHealthV1(0, 200.0),)
        )
        dynamic_load = DynamicRolloutLoadV1.bind(request, 1, config)
        request["encounter"]["targets"][0]["stats"][34] = 201.0
        bridge = _DynamicFullBridge()

        with self.assertRaisesRegex(
            Exception, "request SHA-256 differs|health bits differ"
        ):
            run_fury_full_policy_rollout_v2(
                bridge,
                request,
                CatFurySourceAdapter(),
                seed=1,
                target_contexts={0: _exact_context()},
                dynamic_load=dynamic_load,
            )
        self.assertEqual([], bridge.calls)

        clean_request = _request()
        clean_request["encounter"]["useHealth"] = True
        clean_request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
        seed_bound = DynamicRolloutLoadV1.bind(clean_request, 1, config)
        with self.assertRaisesRegex(Exception, "seed differs"):
            run_fury_full_policy_rollout_v2(
                bridge,
                clean_request,
                CatFurySourceAdapter(),
                seed=2,
                target_contexts={0: _exact_context()},
                dynamic_load=seed_bound,
            )
        self.assertEqual([], bridge.calls)

    def test_dynamic_target_semantics_accepts_live_count_after_first_death(self):
        request = _request(targets=2)
        state = {
            "time_ms": 500,
            "target_index": 1,
            "num_targets": 1,
            "total_target_count": 2,
            "target_health_known": True,
            "target_health_percent": 75.0,
            "target_health_max": 50_000.0,
            "dynamic_team_background": {
                "targets": [
                    {"target_index": 0, "dead": True, "death_time_ms": 400},
                    {"target_index": 1, "dead": False},
                ]
            },
        }
        resolved = _resolve_target_semantics(
            state,
            request,
            {0: _exact_context(0), 1: _exact_context(1)},
        )
        self.assertEqual(resolved["target_index"], 1)
        self.assertEqual(resolved["target_health_pct"], 75.0)

    def test_dynamic_expert_uses_live_not_initial_target_count_after_death(self):
        request = _request(targets=2)
        bridge = _FullBridge(rage=100.0)
        bridge.load(request, 1)
        bridge.num_targets = 1
        bridge.target_index = 1
        state = bridge._state()
        state.update(
            {
                "total_target_count": 2,
                "dynamic_team_background": {
                    "targets": [
                        {"target_index": 0, "dead": True, "death_time_ms": 400},
                        {"target_index": 1, "dead": False},
                    ]
                },
            }
        )
        target = _resolve_target_semantics(
            state,
            request,
            {0: _exact_context(0), 1: _exact_context(1)},
        )

        dynamic_combat = _combat_state(
            state,
            bridge.actions(),
            request,
            target,
            last_gcd_action="",
        )
        dynamic_decision = CatFurySourceAdapter().propose(dynamic_combat)
        self.assertEqual(dynamic_combat.nearby_enemies, 1)
        self.assertEqual(dynamic_decision.swing_queue, SwingQueueOp.HEROIC_STRIKE)

        static_state = dict(state)
        static_state.pop("dynamic_team_background")
        static_state.pop("total_target_count")
        static_state["num_targets"] = 2
        static_combat = _combat_state(
            static_state,
            bridge.actions(),
            request,
            target,
            last_gcd_action="",
        )
        static_decision = CatFurySourceAdapter().propose(static_combat)
        self.assertEqual(static_combat.nearby_enemies, 2)
        self.assertEqual(static_decision.swing_queue, SwingQueueOp.CLEAVE)

    def test_full_artifact_is_accepted_by_the_production_runner_boundary(self):
        request = _request()
        request["encounter"]["useHealth"] = False
        scenario = {
            "instance_id": "raid-a",
            "component_id": "component-a",
            "scenario_id": "boss-one",
            "stratum": "single_target",
            "scenario_weight": 1.0,
            "horizon_ms": 2_000,
            "estimated_cost_units": 2_000,
            "corpus_entry_sha256": hashlib.sha256(b"entry").hexdigest(),
            "source_scenario_sha256": hashlib.sha256(b"scenario").hexdigest(),
            "catalog_sha256": hashlib.sha256(b"catalog").hexdigest(),
            "request": request,
        }
        request_sha = sha256_json(request)
        target_bundle = {
            "schema_version": 2,
            "kind": TARGET_CONTEXT_BUNDLE_KIND,
            "binding_status": "EXACT_COMPARISON",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [_exact_context_receipt(_exact_context())],
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "limitation_codes": [],
        }
        target_bundle_sha = sha256_json(target_bundle)
        scenario["target_context_bundle"] = target_bundle
        scenario["target_context_bundle_sha256"] = target_bundle_sha
        scenario["scenario_model"] = {
            "schema_version": 2,
            "kind": SCENARIO_MODEL_KIND,
            "model_status": "COMPARISON_BOUND",
            "request_sha256": request_sha,
            "target_context_bundle_sha256": target_bundle_sha,
            "historical_truth": False,
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "dynamic_armor_schedule_status": "EXACT_STATIC_SCENARIO",
            "dynamic_attackability_schedule_status": "EXACT_STATIC_SCENARIO",
            "health_or_horizon_status": "EXACT_FIXED_DURATION_SCENARIO",
            "dynamic_semantics_receipt": (
                build_exact_static_request_semantics_receipt(request)
            ),
            "limitation_codes": [],
        }
        scenario["scenario_model_sha256"] = sha256_json(
            scenario["scenario_model"]
        )
        digest = lambda label: hashlib.sha256(label.encode("utf-8")).hexdigest()
        plan = build_runner_plan(
            protocol_id="full-policy-runner-boundary-v2",
            protocol_sha256=digest("protocol"),
            phase="development",
            corpus_manifest_sha256=digest("corpus"),
            runner_inputs_sha256=digest("runner-inputs"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                [scenario]
            ),
            corpus_binding_sha256=digest("binding"),
            master_seeds=(101,),
            scenarios=[scenario],
            policies=[
                {
                    "policy_id": "cat.fury.profile1",
                    "source_sha256": digest("cat-source"),
                    "adapter_sha256": digest("cat-adapter"),
                    "profile_sha256": digest("cat-profile"),
                    "role": "BASELINE",
                }
            ],
            shard_count=1,
            bridge_identity={
                "sha256": digest("bridge"),
                "platform": "windows-amd64",
            },
            execution_bundle_identity={
                "python_source_closure_sha256": digest("python-closure"),
                "ordered_sink_executor_sha256": digest("ordered"),
                "full_policy_rollout_executor_sha256": digest("full"),
                "paired_runner_source_sha256": digest("runner"),
                "evaluation_source_sha256": digest("evaluation"),
                "runtime_snapshot_sha256": digest("runtime"),
            },
            execution_mode=SINGLE_BRIDGE_MODE,
            seed_namespace="full-policy-runner-boundary-v2",
            plan_intent=COMPARISON_INTENT,
        )

        def executor(*, group, scenario, policy):
            return {
                "full_policy_rollout": run_fury_full_policy_rollout_v2(
                    _FullBridge(),
                    scenario["request"],
                    CatFurySourceAdapter(),
                    seed=group["simulator_seed"],
                    target_contexts={0: _exact_context()},
                )
            }

        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(
                plan,
                0,
                executor,
                Path(temporary),
                execution_mode=SINGLE_BRIDGE_MODE,
                worker_id="full-policy-fixture",
            )
            validated = validate_shard(plan, manifest)
            artifact_index = validated.manifest["full_policy_artifacts"]
            self.assertEqual(artifact_index["entry_count"], 1)
            artifact_path = Path(temporary) / artifact_index["entries"][0]["file"]
            self.assertTrue(artifact_path.is_file())
            reduction = reduce_shards(plan, (manifest,))
            self.assertEqual(
                reduction["reduction_receipt"]["full_policy_artifact_count"], 1
            )
            self.assertRegex(
                reduction["reduction_receipt"][
                    "full_policy_artifact_set_sha256"
                ],
                r"^[0-9a-f]{64}$",
            )
            with self.assertRaisesRegex(
                FuryPairedRunnerError, "requires physical shard manifests"
            ):
                validate_reduction_receipt(
                    plan,
                    reduction["reduction_receipt"],
                    reduction["rollout_rows"],
                )
            validate_reduction_receipt(
                plan,
                reduction["reduction_receipt"],
                reduction["rollout_rows"],
                manifest_paths=(manifest,),
            )
            artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
            with self.assertRaisesRegex(
                FuryPairedRunnerError,
                "full-policy artifact (file SHA-256|size) mismatch",
            ):
                validate_shard(plan, manifest)
        self.assertEqual(1, len(validated.rows))
        self.assertRegex(
            validated.rows[0]["full_policy_rollout_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertTrue(validated.rows[0]["evaluation_eligible"])

    def test_cat_complete_exact_rollout_keeps_attempt_acceptance_and_result_separate(self):
        bridge = _FullBridge()
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            CatFurySourceAdapter(),
            seed=2026091001,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "COMPLETE_FAITHFUL")
        self.assertTrue(result["scenario_complete"])
        self.assertTrue(result["simulator_dps_comparison_eligible"])
        self.assertFalse(result["fallback"]["used"])
        self.assertEqual(result["decision_count"], 2)
        first = result["steps"][0]
        sinks = first["ordered_execution"]["sink_events"]
        self.assertGreaterEqual(len(sinks), 2)
        self.assertEqual(
            sinks[0]["source_attempt"]["attempt_id"], "decision-0:sink-1"
        )
        self.assertEqual(sinks[0]["client_acceptance"]["status"], "ACCEPTED")
        self.assertEqual(
            sinks[0]["server_result"]["status"], "PENDING_OR_NOT_EXPOSED"
        )
        matched = [
            sink
            for sink in sinks
            if sink["server_result_followup"]["status"]
            == "OBSERVED_TYPED_RESULT_EVENT"
        ]
        self.assertEqual(len(matched), 1)
        self.assertEqual(
            matched[0]["server_result_followup"]["events"][0]["target_results"],
            [{"target_index": 0, "outcome": "HIT", "damage": 100.0}],
        )
        self.assertEqual(
            first["server_observation_after_advance"]["state_delta"][
                "individual_sink_attribution"
            ],
            "NOT_AVAILABLE_FROM_AGGREGATE_STATE",
        )
        result_calls = [
            value
            for name, value in bridge.calls
            if name == "server_results_since_last_decision"
        ]
        self.assertEqual(result_calls[0], ("decision-0:sink-3",))
        self.assertTrue(all(isinstance(value, tuple) for value in result_calls))
        self.assertFalse(any(name in {"cancel_queue", "set_target"} for name, _ in bridge.calls))

    def test_result_action_must_match_the_accepted_result_bearing_sink(self):
        result = run_fury_full_policy_rollout_v2(
            _WrongResultActionBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091002,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "ACCEPTED_GCD_SERVER_RESULT_ACTION_MISMATCH",
            {row["code"] for row in result["blockers"]},
        )

    def test_result_batch_attempt_ids_must_equal_requested_ids_in_order(self):
        result = run_fury_full_policy_rollout_v2(
            _WrongAttemptOrderBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091003,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "BRIDGE_SERVER_RESULT_ATTEMPT_ALIGNMENT_MISMATCH",
            {row["code"] for row in result["blockers"]},
        )

    def test_pending_result_crosses_a_decision_and_attaches_to_original_sink(self):
        bridge = _OneBoundaryDelayedBridge()
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            CatFurySourceAdapter(),
            seed=2026091004,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "COMPLETE_FAITHFUL")
        result_calls = [
            value
            for name, value in bridge.calls
            if name == "server_results_since_last_decision"
        ]
        self.assertEqual(result_calls[0], ("decision-0:sink-3",))
        self.assertEqual(
            result_calls[1],
            ("decision-0:sink-3", "decision-1:sink-3"),
        )
        first_observation = result["steps"][0][
            "server_observation_after_advance"
        ]["result_stream"]
        self.assertEqual(
            first_observation["pending_attempt_ids"],
            ["decision-0:sink-3"],
        )
        first_gcd = next(
            sink
            for sink in result["steps"][0]["ordered_execution"]["sink_events"]
            if sink["source_sink"]["channel"] == "gcd"
        )
        self.assertEqual(
            first_gcd["server_result_followup"]["status"],
            "OBSERVED_TYPED_RESULT_EVENT",
        )

    def test_pending_result_only_blocks_if_still_unresolved_at_terminal(self):
        result = run_fury_full_policy_rollout_v2(
            _AlwaysPendingBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091005,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "SERVER_RESULT_ATTEMPTS_UNRESOLVED_AT_SCENARIO_TERMINAL",
            {row["code"] for row in result["blockers"]},
        )

    def test_result_completeness_cannot_claim_time_after_returned_state(self):
        result = run_fury_full_policy_rollout_v2(
            _FutureCompletenessBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091006,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "BRIDGE_SERVER_RESULT_COMPLETENESS_TIME_MISMATCH",
            {row["code"] for row in result["blockers"]},
        )

    def test_result_event_cannot_precede_its_acceptance_timestamp(self):
        result = run_fury_full_policy_rollout_v2(
            _ResultBeforeAcceptanceBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091007,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "BRIDGE_SERVER_RESULT_TIME_BEFORE_ACCEPTANCE",
            {row["code"] for row in result["blockers"]},
        )

    def test_result_event_cannot_follow_the_returned_state(self):
        result = run_fury_full_policy_rollout_v2(
            _ResultAfterStateBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091009,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertIn(
            "BRIDGE_SERVER_RESULT_TIME_AFTER_STATE",
            {row["code"] for row in result["blockers"]},
        )

    def test_duration_completion_requires_the_exact_scoring_horizon(self):
        result = run_fury_full_policy_rollout_v2(
            _EarlyFinishedZeroRemainingBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=2026091008,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertFalse(result["configured_completion"]["criterion_met"])
        self.assertFalse(result["configured_completion"]["exact_end_time"])
        self.assertIn(
            "CONFIGURED_SCENARIO_COMPLETION_NOT_MET",
            {row["code"] for row in result["blockers"]},
        )

    def test_only_the_matching_active_hardcast_is_right_censored_at_horizon(self):
        attempt_id = "decision-7:sink-2"
        slam = ACTION_KEY_TO_REF["warrior.slam"].to_wire()
        sink = {
            "operation_contract": {"action_ref": slam},
            "source_attempt": {
                "attempt_id": attempt_id,
                "accepted_at_time_ms": 1_900,
            },
        }
        observation, blocker, pending = _right_censor_terminal_active_hardcast(
            {
                "time_ms": 2_000,
                "damage_done": 321.0,
                "current_cast": {"action": slam, "remaining_ms": 1_400},
            },
            (attempt_id,),
            {attempt_id: sink},
        )

        self.assertIsNone(blocker)
        self.assertEqual(pending, ())
        self.assertEqual(
            observation["result_stream"]["status"],
            "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
        )
        self.assertFalse(observation["result_stream"]["post_horizon_damage_scored"])
        self.assertEqual(
            sink["server_result_followup"]["status"],
            "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
        )

    def test_nonmatching_terminal_pending_result_remains_fatal(self):
        attempt_id = "decision-7:sink-2"
        bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"].to_wire()
        slam = ACTION_KEY_TO_REF["warrior.slam"].to_wire()
        observation, blocker, pending = _right_censor_terminal_active_hardcast(
            {
                "time_ms": 2_000,
                "damage_done": 321.0,
                "current_cast": {"action": slam, "remaining_ms": 1_400},
            },
            (attempt_id,),
            {
                attempt_id: {
                    "operation_contract": {"action_ref": bloodthirst},
                    "source_attempt": {
                        "attempt_id": attempt_id,
                        "accepted_at_time_ms": 1_900,
                    },
                }
            },
        )

        self.assertIsNone(observation)
        self.assertIsNone(blocker)
        self.assertEqual(pending, (attempt_id,))

    def test_current_bridge_capability_gaps_are_typed_not_silently_faithful(self):
        result = run_fury_full_policy_rollout_v2(
            _CurrentBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=7,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "COMPLETE_NONFAITHFUL")
        self.assertTrue(result["scenario_complete"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        codes = {row["code"] for row in result["blockers"]}
        self.assertIn("BRIDGE_START_ATTACK_CONTROL_ABSENT", codes)
        self.assertIn("BRIDGE_STOP_CAST_CONTROL_ABSENT", codes)
        self.assertIn("BRIDGE_SERVER_RESULT_STREAM_ABSENT", codes)
        first_sink = result["steps"][0]["ordered_execution"]["sink_events"][0]
        self.assertEqual(
            first_sink["client_acceptance"]["status"],
            "UNKNOWN_BRIDGE_CAPABILITY_ABSENT",
        )
        self.assertEqual(
            first_sink["server_result_followup"]["status"],
            "UNKNOWN_RESULT_STREAM_NOT_EXPOSED",
        )

    def test_source_wait_is_driven_without_a_fallback_wait(self):
        bridge = _FullBridge(rage=0.0)
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            CatFurySourceAdapter(),
            seed=71,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "COMPLETE_FAITHFUL")
        self.assertTrue(any(name == "wait" for name, _ in bridge.calls))
        first = result["steps"][0]["ordered_execution"]
        self.assertEqual(first["wait_event"]["kind"], "SOURCE_WAIT_DECISION")
        self.assertFalse(first["wait_event"]["fallback"])
        self.assertFalse(result["fallback"]["used"])
        result_calls = [
            value
            for name, value in bridge.calls
            if name == "server_results_since_last_decision"
        ]
        self.assertEqual(result_calls[0], ())

    def test_exact_context_fails_closed_when_bridge_health_is_unknown(self):
        bridge = _FullBridge(health_known=False)
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            CatFurySourceAdapter(),
            seed=72,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertEqual(result["decision_count"], 0)
        self.assertIn(
            "EXACT_TARGET_HEALTH_NOT_EXPOSED",
            {row["code"] for row in result["blockers"]},
        )
        self.assertFalse(any(name in {"act", "wait"} for name, _ in bridge.calls))

    def test_encountered_stop_cast_gap_blocks_without_fallback(self):
        bridge = _NoStopBridge(two_hand=True, casting_slam=True)
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(targets=2),
            CatFurySourceAdapter(),
            seed=8,
            target_contexts={0: _exact_context(), 1: _exact_context(1)},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertFalse(result["fallback"]["used"])
        self.assertEqual(result["decision_count"], 1)
        execution = result["steps"][0]["ordered_execution"]
        self.assertTrue(execution["execution_blocked"])
        stop = next(
            row
            for row in execution["sink_events"]
            if row["source_sink"]["channel"] == "cast_control"
        )
        self.assertEqual(
            stop["simulator_submission"]["status"],
            "NOT_SUBMITTED_BRIDGE_CAPABILITY_ABSENT",
        )
        self.assertFalse(any(name == "wait" for name, _ in bridge.calls))

    def test_absent_gcd_action_blocks_or_stops_without_synthetic_wait(self):
        bridge = _FullBridge(
            reject_actions={ACTION_KEY_TO_REF["warrior.bloodthirst"]}
        )
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            CatFurySourceAdapter(),
            seed=9,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertFalse(result["fallback"]["used"])
        self.assertFalse(any(name == "wait" for name, _ in bridge.calls))
        codes = {row["code"] for row in result["blockers"]}
        self.assertTrue(
            {"ORDERED_SINK_EXECUTION_BLOCKED", "DECISION_NOT_CONSUMED_NO_FALLBACK"}
            & codes
        )

    def test_contra_v2_requires_context_before_bridge_mutation(self):
        bridge = _FullBridge()
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            ContraDeployedFuryAdapterV2(),
            seed=10,
            target_contexts=None,
        )

        self.assertEqual(result["status"], "TARGET_CONTEXT_REQUIRED")
        self.assertFalse(result["bridge_mutated"])
        self.assertEqual(bridge.calls, [])

    def test_contra_v2_receives_exact_max_health_classification_and_loadout(self):
        result = run_fury_full_policy_rollout_v2(
            _FullBridge(),
            _request(),
            ContraDeployedFuryAdapterV2(),
            seed=11,
            target_contexts={0: _exact_context()},
        )

        proposal = result["steps"][0]["proposal"]
        self.assertEqual(proposal["expert_id"], "contra.deployed.fury.raid_a.v2")
        metadata = proposal["metadata"]
        self.assertEqual(metadata["target_max_health"], 50_000)
        self.assertEqual(metadata["target_classification"], "worldboss")
        self.assertEqual(metadata["derived_zssdw"], 0)
        self.assertNotEqual(result["expert_id"], CONTRA260817_POLICY_ID)

    def test_sensitivity_context_is_explicit_and_never_comparison_eligible(self):
        result = run_fury_full_policy_rollout_v2(
            _FullBridge(),
            _request(),
            CatFurySourceAdapter(),
            seed=12,
            target_contexts={0: _sensitivity_context()},
        )

        self.assertEqual(result["status"], "COMPLETE_NONFAITHFUL")
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        self.assertEqual(
            result["steps"][1]["target_semantics"]["target_health_pct"], 50.0
        )
        self.assertEqual(
            result["target_context_receipts"][0]["mode"], "SENSITIVITY"
        )
        self.assertIn(
            "TARGET_SEMANTICS_SENSITIVITY_NOT_EXACT",
            {row["code"] for row in result["blockers"]},
        )

    def test_contra_sensitivity_evidence_reaches_adapter_and_disables_vote(self):
        result = run_fury_full_policy_rollout_v2(
            _FullBridge(),
            _request(),
            ContraDeployedFuryAdapterV2(),
            seed=121,
            target_contexts={0: _sensitivity_context()},
        )

        proposal = result["steps"][0]["proposal"]
        self.assertTrue(proposal["valid"])
        self.assertFalse(proposal["eligible_for_independent_vote"])
        self.assertTrue(proposal["metadata"]["sensitivity_only"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])

    def test_contra260817_is_required_not_implemented_and_never_substituted(self):
        bridge = _FullBridge()
        adapter = _CallSentinel()
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            adapter,
            seed=13,
            target_contexts={0: _exact_context()},
        )

        self.assertEqual(result["status"], "NOT_IMPLEMENTED_REQUIRED_BASELINE")
        self.assertTrue(result["required_baseline"])
        self.assertFalse(result["bridge_mutated"])
        self.assertFalse(adapter.called)
        self.assertEqual(bridge.calls, [])
        direct = contra260817_required_baseline_status_v2(seed=13)
        self.assertEqual(direct["expert_id"], CONTRA260817_POLICY_ID)

    def test_contra_new_candidate_maps_to_required_unimplemented_status(self):
        result = run_fury_full_policy_rollout_v2(
            _FullBridge(),
            _request(),
            ContraNewCandidateAdapter(),
            seed=14,
            target_contexts={0: _exact_context()},
        )
        self.assertEqual(result["status"], "NOT_IMPLEMENTED_REQUIRED_BASELINE")
        self.assertEqual(result["expert_id"], CONTRA260817_POLICY_ID)

    def test_same_expert_id_cannot_masquerade_as_cat(self):
        class Impostor:
            expert_id = CatFurySourceAdapter.expert_id

        bridge = _FullBridge()
        result = run_fury_full_policy_rollout_v2(
            bridge,
            _request(),
            Impostor(),
            seed=15,
            target_contexts={0: _exact_context()},
        )
        self.assertEqual(result["status"], "UNSUPPORTED_POLICY_ADAPTER")
        self.assertEqual(bridge.calls, [])

    def test_eligibility_is_rederived_when_executor_summary_lies(self):
        from o2o_dps import fury_full_policy_rollout_v2 as module

        real = module.execute_ordered_sinks_v2

        def corrupt_summary(bridge, proposal, state, **kwargs):
            execution = real(bridge, proposal, state, **kwargs)
            execution["ordered_projection_faithful"] = True
            execution["execution_blocked"] = False
            execution["nonfaithful_reasons"] = []
            execution["sink_events"][0]["simulator_submission"] = {
                "status": "NOT_SUBMITTED_FAIL_CLOSED"
            }
            return execution

        with patch.object(module, "execute_ordered_sinks_v2", corrupt_summary):
            result = run_fury_full_policy_rollout_v2(
                _FullBridge(),
                _request(),
                CatFurySourceAdapter(),
                seed=73,
                target_contexts={0: _exact_context()},
            )

        self.assertEqual(result["status"], "INCOMPLETE_BLOCKED")
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        self.assertIn(
            "ORDERED_TRACE_SINK_NOT_SUBMITTED",
            {row["code"] for row in result["blockers"]},
        )
        audit = result["steps"][0]["independent_ordered_audit"]
        self.assertFalse(audit["executor_faithful_flag_trusted"])

    def test_context_contract_rejects_exact_values_and_unsorted_sensitivity(self):
        with self.assertRaisesRegex(ValueError, "must come from bridge"):
            TargetSemanticsContextV2(
                context_id="bad-exact",
                mode=TargetSemanticsModeV2.DECLARED_EXACT,
                target_index=0,
                target_classification=ContraTargetClassificationV2.NORMAL,
                target_name="Target 0",
                equipped_item_names=(),
                target_classification_evidence=_evidence(),
                target_name_evidence=_evidence(),
                equipment_evidence=_evidence(),
                target_position_evidence=_evidence(),
                target_health_pct_evidence=_evidence(
                    ContraEvidenceKindV2.SIMULATOR_STATE
                ),
                target_max_health_evidence=_evidence(
                    ContraEvidenceKindV2.SIMULATOR_STATE
                ),
                target_max_health=1,
            )
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            TargetSemanticsContextV2(
                context_id="bad-sensitivity",
                mode=TargetSemanticsModeV2.SENSITIVITY,
                target_index=0,
                target_classification=ContraTargetClassificationV2.NORMAL,
                target_name="Target 0",
                equipped_item_names=(),
                target_classification_evidence=_evidence(
                    ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS
                ),
                target_name_evidence=_evidence(),
                equipment_evidence=_evidence(),
                target_position_evidence=_evidence(
                    ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS
                ),
                target_health_pct_evidence=_evidence(
                    ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS
                ),
                target_max_health_evidence=_evidence(
                    ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS
                ),
                target_max_health=1,
                health_pct_schedule=(
                    HealthPercentPointV2(1, 50),
                    HealthPercentPointV2(0, 100),
                ),
            )


if __name__ == "__main__":
    unittest.main()
